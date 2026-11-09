"""Telemetry, outages, battery and genset pages (UI-05, 06, 07, 10, 11), the CSV import and export, and the event log."""
from __future__ import annotations

import inspect
import json
import math
import re
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, Body, Request
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

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
    # telemetry.imports adds the range and file-name columns to an older workspace file before reading them, and always gives log as a list
    imports = telemetry.imports(site_id)
    return {"days": [{"day": r["day"], "completeness": r.get("completeness")} for r in rows], "first_data": first, "imports": imports}


#: An upload's rows must lie between this day and a day from now, and one file may span at most UPLOAD_MAX_DAYS: the import aggregates every day it covers,
#: so a row dated 1970 or 9999 (a logger with an unset clock, or a crafted file) would otherwise hold a worker for hours (plan Part 2.8: uploaded CSV is
#: untrusted input, parsed with size and time caps)
UPLOAD_EARLIEST = datetime(2015, 1, 1, tzinfo=timezone.utc)
UPLOAD_MAX_DAYS = 400


def _upload_window(content: bytes, tz_offset_h: float) -> None:
    """Refuses (ValueError, in words) a file whose header names a column twice, a file with a row before 2015, a row more than a day ahead, or rows
    spanning more than UPLOAD_MAX_DAYS. Reads only the ts column, as the import reads it; rows whose ts does not parse are left to the import, which skips
    them. The import reads rows by column name and keeps the last of two columns with one name, so a repeated name is refused outright: this check and
    the import then always read the same ts column."""
    import csv
    import io
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    head = next(reader, None) or []
    twice = sorted({h for h in head if h.strip() and head.count(h) > 1})
    if twice:
        raise ValueError(f"The header names {', '.join(twice[:5])} more than once: give each column once and upload again")
    if "ts" not in head:
        return
    col = head.index("ts")
    try:
        tz = timezone(timedelta(hours=tz_offset_h))
    except (ValueError, OverflowError):
        raise ValueError("The timezone offset is hours from UTC, between -23 and 23") from None
    latest = datetime.now(timezone.utc) + timedelta(days=1)
    first = last = None
    early = late = 0
    for row in reader:
        cell = (row[col] if col < len(row) else "") or ""
        try:
            t = datetime.fromisoformat(cell.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        try:
            if t.tzinfo is None:
                t = t.replace(tzinfo=tz)
            if t < UPLOAD_EARLIEST:
                early += 1
                continue
            if t > latest:
                late += 1
                continue
        except OverflowError:                       # a time at the very edge of the calendar
            early += 1
            continue
        first = t if first is None or t < first else first
        last = t if last is None or t > last else last
    if early or late:
        raise ValueError(f"{early + late} row{'s are' if early + late != 1 else ' is'} dated "
                         + ("before 2015" if early and not late else "in the future" if late and not early else "before 2015 or in the future")
                         + ": check the logger's clock (and the timezone) and upload again")
    if first is not None and (last - first).days > UPLOAD_MAX_DAYS:
        raise ValueError(f"The file spans {(last - first).days} days; upload at most {UPLOAD_MAX_DAYS} days per file")


def _takes_file_name() -> bool:
    try:
        return "file_name" in inspect.signature(telemetry.import_csv).parameters
    except (TypeError, ValueError):
        return False


def _import(site_id: str, body: bytes, tz_offset_h: float, file_name: str | None = None) -> dict:
    _upload_window(body, tz_offset_h)
    try:
        # the uploaded file's name goes with the import (the quality page's imports rows) where telemetry.import_csv takes it
        if file_name and _takes_file_name():
            return telemetry.import_csv(site_id, body, tz_offset_h, file_name=file_name)
        return telemetry.import_csv(site_id, body, tz_offset_h)
    except (OverflowError, OSError) as exc:
        raise ValueError(f"A row's time cannot be read: {exc}") from None


#: An uploaded file's name as kept: its last path part, without control characters, at most this long
FILE_NAME_MAX = 200


def _clean_file_name(name) -> str | None:
    if not isinstance(name, str):
        return None
    name = "".join(ch for ch in name.replace("\\", "/").rsplit("/", 1)[-1] if ch.isprintable()).strip()
    return name[:FILE_NAME_MAX] or None


@router.post("/api/sites/{site_id}/telemetry/upload")
async def upload(site_id: str, request: Request, tz_offset_h: float = 0.0, file_name: str | None = None):
    """A CSV upload: the file as the request body (its name in `file_name`), or as the file part of a multipart form (its name from the part)."""
    auth.need("upload_data", "Uploading data")
    await run_in_threadpool(site_or_404, site_id)
    body = await request.body()
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        boundary = ctype.split("boundary=")[-1].strip().strip('"').encode()
        parts = body.split(b"--" + boundary)
        part = next((p.split(b"\r\n\r\n", 1) for p in parts if b"\r\n\r\n" in p and b"filename=" in p.split(b"\r\n\r\n", 1)[0]), None)
        body = part[1].rsplit(b"\r\n", 1)[0] if part else b""
        if part and not file_name:
            m = re.search(rb'filename="([^"]*)"', part[0])
            file_name = m.group(1).decode("utf-8", errors="replace") if m else None
    if not body:
        raise Refusal(422, "Send the CSV file as the request body", "invalid")
    file_name = _clean_file_name(file_name)
    try:
        # parsed and aggregated on a worker thread, never on the event loop that answers every company's requests
        res = await run_in_threadpool(_import, site_id, body, tz_offset_h, file_name)
    except ValueError as exc:
        raise bad(exc) from None
    await run_in_threadpool(store.audit, auth.actor(), "uploaded telemetry", site_id, {"rows": res["rows"], "gaps": res["gaps"], "file_name": file_name})
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
    text = telemetry.export_csv(site_id, t0, t1)
    store.audit(auth.actor(), "exported telemetry", site_id, {"from": t0, "to": t1})
    return PlainTextResponse(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{site_id}-telemetry.csv"'})


@router.get("/api/sites/{site_id}/events")
def events(site_id: str, limit: int = 200):
    """The site's events, newest first. A fuel log's detail carries what the fuel cost (cost_rs), so the answer goes through the money filter: a viewer and
    a machine key read the litres, never the rupees (audit security-roles-01)."""
    site_or_404(site_id)
    with store.connect() as con:
        rows = con.execute("SELECT * FROM events WHERE site_id = ? ORDER BY ts DESC LIMIT ?", (site_id, max(1, min(limit, 1000)))).fetchall()
    return out([{**dict(r), "detail": json.loads(r["detail"])} for r in rows])


# ------------------------------------------------------------------------------------------------ outages (UI-07)

#: How far back the outages page looks for the source's latest grid reading (`signal`)
SIGNAL_WINDOW_S = 7 * 86400


def _grid_signal(site_id: str, now: int) -> dict:
    """The source's grid signal: the latest grid-present reading of the last SIGNAL_WINDOW_S, {grid_present: bool, last_seen: its ts}, or
    {grid_present: null, last_seen: null} when the source sent none (the page's blocking notice: a source without a grid signal)."""
    with telemetry.connect() as con:
        r = con.execute("SELECT ts, grid_present FROM minute WHERE site_id = ? AND ts >= ? AND grid_present IS NOT NULL ORDER BY ts DESC LIMIT 1",
                        (site_id, now - SIGNAL_WINDOW_S)).fetchone()
    if r is None or not isinstance(r["grid_present"], (int, float)) or not math.isfinite(r["grid_present"]):
        return {"grid_present": None, "last_seen": None}
    return {"grid_present": bool(r["grid_present"] >= 0.5), "last_seen": int(r["ts"])}

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
    return {"off_grid": off_grid, "signal": _grid_signal(site_id, now), "kpis": kpis, "log": list(reversed(log)), "heatmap": {"days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "minutes": heat},
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


@router.get("/api/sites/{site_id}/genset/fuel")
def fuel_logs(site_id: str, limit: int = 200):
    """The fuel log, newest first: {id, ts (when it was logged), day, litres, cost_rs (when given; money, so a viewer does not see it), note, by}."""
    site_or_404(site_id)
    with store.connect() as con:
        rows = con.execute("SELECT id, ts, source, detail FROM events WHERE site_id = ? AND type = 'fuel_log' ORDER BY ts DESC, id DESC LIMIT ?",
                           (site_id, max(1, min(int(limit), 1000)))).fetchall()
    logs = []
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            d = {}
        logs.append({"id": r["id"], "ts": r["ts"], "day": d.get("day"), "litres": d.get("litres"), "cost_rs": d.get("cost_rs"), "note": d.get("note", ""),
                     "by": r["source"]})
    return out({"logs": logs})


@router.post("/api/sites/{site_id}/genset/fuel")
def fuel_log(site_id: str, inp: dict = Body(...)):
    """Logs fuel put in the genset: litres, the day (today by default), an optional cost_rs (what it cost, as billed) and a note."""
    auth.need("upload_data", "Logging fuel")
    site_or_404(site_id)
    try:
        litres = float(inp.get("litres", 0))
        if not (0 < litres < 100000):
            raise ValueError("Litres must be a positive number")
        day = parse_day(inp.get("day"), today())
        cost = inp.get("cost_rs")
        if cost is not None:
            cost = float(cost)
            if not (0 <= cost < 1e9) or isinstance(inp.get("cost_rs"), bool):
                raise ValueError("The cost is a number of rupees from 0")
    except (TypeError, ValueError) as exc:
        raise bad(exc) from None
    detail = {"day": day.isoformat(), "litres": litres, "note": str(inp.get("note", ""))[:200]}
    if cost is not None:
        detail["cost_rs"] = round(cost, 2)
    with store.connect() as con:
        con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)",
                    (site_id, store._now(), "fuel_log", auth.actor(), json.dumps(detail)))
    return {"logged": litres, "day": day.isoformat()}


_ = (sites, timezone)
