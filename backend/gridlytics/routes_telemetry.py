"""Telemetry, outages, battery and genset pages (UI-05, 06, 07, 10, 11), the CSV import and export, and the event log."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, Body, Request
from fastapi.responses import PlainTextResponse

from . import alerts, auth, battery, forecast, plans, sites, store, telemetry
from .routes_common import PKT, bad, full_or_404, now_ts, out, parse_day, parse_ts, site_or_404, today
from .schemas import Refusal

router = APIRouter()


@router.get("/api/sites/{site_id}/telemetry")
def series(site_id: str, start: str | None = None, end: str | None = None, names: str = "", res: str = "auto"):
    site_or_404(site_id)
    now = now_ts()
    t1 = parse_ts(end, now)
    t0 = parse_ts(start, t1 - 86400)
    if t1 <= t0 or t1 - t0 > 400 * 86400:
        raise Refusal(422, "The range runs forwards and is at most 400 days", "invalid")
    if res not in ("auto", "1m", "15m", "1h", "1d"):
        raise Refusal(422, "The resolution is auto, 1m, 15m, 1h or 1d", "invalid")
    return telemetry.series(site_id, t0, t1, [n for n in names.split(",") if n], res)


@router.get("/api/sites/{site_id}/telemetry/days")
def days(site_id: str, start: str | None = None, end: str | None = None):
    site_or_404(site_id)
    t = today()
    return telemetry.days(site_id, parse_day(start, t - timedelta(days=30)).isoformat(), parse_day(end, t).isoformat())


@router.get("/api/sites/{site_id}/telemetry/heatmap")
def heatmap(site_id: str, name: str = "load_power_w", days: int = 28):
    site_or_404(site_id)
    return telemetry.heatmap(site_id, name, max(1, min(days, 120)))


@router.get("/api/sites/{site_id}/telemetry/quality")
def quality(site_id: str, days: int = 14):
    site_or_404(site_id)
    t = today()
    rows = telemetry.days(site_id, (t - timedelta(days=days)).isoformat(), t.isoformat())
    first = telemetry.first_ts(site_id)
    with store.connect() as con:
        imports = [dict(r) for r in con.execute("SELECT id, source, rows, gaps, status, log, created FROM imports WHERE site_id = ? ORDER BY created DESC LIMIT 20",
                                                 (site_id,)).fetchall()]
    for i in imports:
        i["log"] = json.loads(i["log"])
    return {"days": [{"day": r["day"], "completeness": r.get("completeness")} for r in rows], "first_data": first, "imports": imports}


@router.post("/api/sites/{site_id}/telemetry/upload")
async def upload(site_id: str, request: Request, tz_offset_h: float = 0.0):
    auth.need("upload_data", "Uploading data")
    site_or_404(site_id)
    body = await request.body()
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        boundary = ctype.split("boundary=")[-1].strip().strip('"').encode()
        parts = body.split(b"--" + boundary)
        body = next((p.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0] for p in parts if b"\r\n\r\n" in p and b"filename=" in p.split(b"\r\n\r\n", 1)[0]), b"")
    if not body:
        raise Refusal(422, "Send the CSV file as the request body", "invalid")
    try:
        res = telemetry.import_csv(site_id, body, tz_offset_h)
    except ValueError as exc:
        raise bad(exc) from None
    store.audit(auth.actor(), "uploaded telemetry", site_id, {"rows": res["rows"], "gaps": res["gaps"]})
    return res


@router.get("/api/sites/{site_id}/telemetry/export.csv")
def export(site_id: str, start: str | None = None, end: str | None = None):
    auth.need("export", "Exporting data")
    site_or_404(site_id)
    now = now_ts()
    t1 = parse_ts(end, now)
    t0 = parse_ts(start, t1 - 86400)
    if t1 - t0 > 32 * 86400:
        raise Refusal(422, "Export at most 32 days at a time", "invalid")
    return PlainTextResponse(telemetry.export_csv(site_id, t0, t1), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{site_id}-telemetry.csv"'})


@router.get("/api/sites/{site_id}/events")
def events(site_id: str, limit: int = 200):
    site_or_404(site_id)
    with store.connect() as con:
        rows = con.execute("SELECT * FROM events WHERE site_id = ? ORDER BY ts DESC LIMIT ?", (site_id, max(1, min(limit, 1000)))).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


# ------------------------------------------------------------------------------------------------ outages (UI-07)

@router.get("/api/sites/{site_id}/outages")
def outages(site_id: str, days: int = 30):
    s = full_or_404(site_id)
    now = now_ts()
    days = max(1, min(days, 120))
    log = telemetry.outages(site_id, now - days * 86400, now)
    for o in log:
        o["scheduled"] = False
    sch = forecast.schedule(site_id)
    if sch:
        for o in log:
            m = forecast._window_mask(sch["windows"], np.array([o["start"] + 300]))
            o["scheduled"] = bool(m[0])
    with store.connect() as con:
        rows = con.execute("SELECT ts, detail FROM events WHERE site_id = ? AND type = 'outage_cause'", (site_id,)).fetchall()
    cause = {}
    for r in rows:
        d = json.loads(r["detail"])
        cause[d.get("start")] = d.get("cause")
    for o in log:
        o["cause"] = cause.get(o["start"], "scheduled" if o["scheduled"] else "")
    heat = [[0] * 24 for _ in range(7)]
    for o in log:
        t = o["start"]
        while t < o["end"]:
            lt = datetime.fromtimestamp(t, PKT)
            heat[lt.weekday()][lt.hour] += 1
            t += 60
    fc = forecast.page(s, "outage")
    plan = plans.current(site_id)
    n = len(log)
    minutes = sum(o["minutes"] for o in log)
    kpis = {"events": n, "hours": round(minutes / 60, 1), "per_day": round(n / days, 2), "median_minutes": int(np.median([o["minutes"] for o in log])) if log else None,
            "unserved_kwh": round(sum(o["unserved_kwh"] for o in log), 1), "with_genset": sum(1 for o in log if o["genset_minutes"] > 0),
            "adherence": (fc.get("issue") or {}).get("adherence")}
    off_grid = not s.get("connections") or s["connections"][0]["data"].get("regime") == "off_grid"
    return {"off_grid": off_grid, "kpis": kpis, "log": list(reversed(log)), "heatmap": {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "minutes": heat},
            "schedule": sch, "forecast": fc, "reserve": [{"t": st["t"], "reserve_pct": st["reserve_pct"], "soc_pct": st["soc_pct"], "p_down": st["p_down"]}
                                                        for st in (plan or {}).get("steps", [])], "feeder": s["data"].get("outage_unit")}


@router.put("/api/sites/{site_id}/outages/schedule")
def schedule_put(site_id: str, inp: dict = Body(...)):
    auth.need("edit_sites", "Setting the load-shedding schedule")
    site_or_404(site_id)
    try:
        return forecast.set_schedule(site_id, inp.get("windows") or [], str(inp.get("source", "entered"))[:40], auth.actor())
    except (ValueError, TypeError, IndexError) as exc:
        raise bad(exc) from None


@router.put("/api/sites/{site_id}/outages/{start}/cause")
def outage_cause(site_id: str, start: int, inp: dict = Body(...)):
    auth.need("ack_alerts", "Recording an outage cause")
    site_or_404(site_id)
    cause = str(inp.get("cause", ""))[:80]
    with store.connect() as con:
        con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)",
                    (site_id, store._now(), "outage_cause", auth.actor(), json.dumps({"start": start, "cause": cause})))
    return {"start": start, "cause": cause}


# ------------------------------------------------------------------------------------------------ battery (UI-10)

@router.get("/api/sites/{site_id}/battery")
def battery_page(site_id: str):
    s = full_or_404(site_id)
    if not (s.get("assets") or {}).get("battery"):
        return {"battery": None}
    now = now_ts()
    st = battery.state(s, now)
    t = today()
    daily = telemetry.days(site_id, (t - timedelta(days=30)).isoformat(), t.isoformat())
    e_nom = st["e_nom_kwh"] or 1
    cycles = [{"day": d["day"], "cycles": round((d.get("discharge_kwh") or 0) / e_nom, 2), "min_soc": d.get("min_soc"), "max_soc": d.get("max_soc")}
              for d in daily]
    soc = telemetry.series(site_id, now - 48 * 3600, now, ["battery_soc_est_pct", "battery_soc_bms_pct", "battery_temp_c", "battery_power_w"], "15m")
    plan = plans.current(site_id)
    return out({"battery": st, "pack": s["assets"]["battery"], "cycles": cycles, "cap_per_day": s["data"].get("cycle_cap_per_day"), "soc": soc,
                "plan": [{"t": x["t"], "soc_pct": x["soc_pct"], "reserve_pct": x["reserve_pct"]} for x in (plan or {}).get("steps", [])]})


# ------------------------------------------------------------------------------------------------ genset (UI-11)

@router.get("/api/sites/{site_id}/genset")
def genset_page(site_id: str, days: int = 14):
    s = full_or_404(site_id)
    g = (s.get("assets") or {}).get("genset")
    if not g:
        return {"genset": None}
    now = now_ts()
    days = max(1, min(days, 60))
    d = telemetry.raw(site_id, now - days * 86400, now, ("genset_running", "genset_power_w", "genset_fuel_l", "grid_present"))
    runs, cur = [], None
    for i, t in enumerate(d["ts"]):
        on = d["genset_running"][i] == 1
        if on and cur is None:
            cur = {"start": int(t), "kwh": 0.0, "litres": 0.0, "in_outage": bool(d["grid_present"][i] == 0)}
        if on and cur:
            cur["end"] = int(t) + 60
            cur["kwh"] += float(np.nan_to_num(d["genset_power_w"][i])) / 60000
            cur["litres"] += float(np.nan_to_num(d["genset_fuel_l"][i]))
        if not on and cur:
            runs.append(cur)
            cur = None
    if cur:
        cur["ongoing"] = True
        runs.append(cur)
    for r in runs:
        r["hours"] = round((r["end"] - r["start"]) / 3600, 2)
        r["kwh"], r["litres"] = round(r["kwh"], 1), round(r["litres"], 1)
        r["load_ratio"] = round(r["kwh"] / max(1e-6, r["hours"] * g["rated_kw"]), 2)
        p = plans.active_at(site_id, r["start"])
        stp = next((x for x in (p or {}).get("steps", []) if x["t"] <= r["start"] < x["t"] + 900), None)
        r["plan_requested"] = bool(stp and stp["genset_on"])
    from . import tariffs
    price, src = tariffs.fuel_price(g.get("fuel_kind", "hsd"), today())
    hours = sum(r["hours"] for r in runs)
    litres = sum(r["litres"] for r in runs)
    plan = plans.current(site_id)
    return out({"genset": g, "control": s["data"].get("genset_start_control"), "kpis": {"hours": round(hours, 1), "litres": round(litres, 1), "starts": len(runs),
                "avg_load_ratio": round(sum(r["load_ratio"] * r["hours"] for r in runs) / hours, 2) if hours else None,
                "fuel_cost_rs": round(litres * price, 0), "estimated": True},
                "fuel_price": {"price_per_l": price, "source": src, "kind": g.get("fuel_kind", "hsd")}, "runs": list(reversed(runs)),
                "curve": {"a": g["a"], "b": g["b"], "rated_kw": g["rated_kw"],
                          "points": [{"load_ratio": lr, "litres_per_hour": round(g["a"] * lr * g["rated_kw"] + g["b"] * g["rated_kw"], 2)}
                                     for lr in (0.25, 0.5, 0.75, 1.0)]},
                "requests": alerts.requests_for(site_id), "plan": [{"t": x["t"], "genset_kw": x["genset_kw"], "reason": x["reason"]}
                                                                    for x in (plan or {}).get("steps", []) if x["genset_on"]][:96],
                "policy": "Gridlytics never starts or stops the genset itself: it asks, with a reason, and records whether the advice was followed."})


@router.post("/api/sites/{site_id}/genset/requests/{nid}")
def genset_answer(site_id: str, nid: str, inp: dict = Body(...)):
    auth.need("ack_alerts", "Answering a genset request")
    site_or_404(site_id)
    try:
        return alerts.answer_request(site_id, nid, str(inp.get("outcome", "")), auth.actor())
    except KeyError:
        raise bad(ValueError("No such request")) from None
    except ValueError as exc:
        raise bad(exc) from None


@router.post("/api/sites/{site_id}/genset/fuel")
def fuel_log(site_id: str, inp: dict = Body(...)):
    auth.need("upload_data", "Logging fuel")
    site_or_404(site_id)
    try:
        litres = float(inp.get("litres", 0))
        if not (0 < litres < 100000):
            raise ValueError("Litres must be a positive number")
        day = parse_day(inp.get("day"), today())
    except (TypeError, ValueError) as exc:
        raise bad(exc) from None
    with store.connect() as con:
        con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)",
                    (site_id, store._now(), "fuel_log", auth.actor(), json.dumps({"day": day.isoformat(), "litres": litres, "note": str(inp.get("note", ""))[:200]})))
    return {"logged": litres, "day": day.isoformat()}


_ = (sites, timezone)
