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
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from . import alerts, auth, battery, forecast, plans, sites, store, telemetry
from .routes_common import PKT, bad, full_or_404, now_ts, out, parse_day, parse_ts, site_or_404, today
from .schemas import Refusal, Strict, check

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
    # today's share is of the minutes elapsed so far, marked in_progress (role-viewer-07), not of the whole day
    rows = telemetry.quality_days(site_id, t - timedelta(days=days), t, now_ts())
    first = telemetry.first_ts(site_id)
    # telemetry.imports adds the range and file-name columns to an older workspace file before reading them, and always gives log as a list
    imports = telemetry.imports(site_id)
    return {"days": rows, "first_data": first, "imports": imports}


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
    site_or_404(site_id)
    with store.connect() as con:
        rows = con.execute("SELECT * FROM events WHERE site_id = ? ORDER BY ts DESC LIMIT ?", (site_id, max(1, min(limit, 1000)))).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


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

#: the genset page's periods: the last `days` days (1 to GENSET_MAX_DAYS, 14 by default), or a calendar month in Karachi time (`month` YYYY-MM; the current
#: month runs to now), which also answers the month before it (R8 2.11: run hours and starts this month against last)
GENSET_MAX_DAYS = 60
#: the refit reads the metered runs of the last REFIT_DAYS days (R8 2.11: "refit from 30 days of meter data")
REFIT_DAYS = 30
#: a run is a measured point of the fuel curve when its litres are all metered, its output all read, and it ran at least POINT_MIN_H hours
POINT_MIN_H = 0.25
#: a refit needs at least REFIT_MIN_POINTS points whose loads span at least REFIT_MIN_SPREAD of the rating (else the slope is a guess)
REFIT_MIN_POINTS = 3
REFIT_MIN_SPREAD = 0.1


def _month_window(month: str, now: int) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", (month or "").strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise bad(ValueError("The month must be written as year and month, for example 2026-11."))
    y, mo = int(m.group(1)), int(m.group(2))
    t0 = int(datetime(y, mo, 1, tzinfo=PKT).timestamp())
    t1 = int(datetime(y + mo // 12, mo % 12 + 1, 1, tzinfo=PKT).timestamp())
    if t0 >= now:
        raise bad(ValueError("That month has not started yet."))
    return t0, min(t1, now)


def _previous_month(month: str) -> str:
    y, mo = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}"


def _run_kpis(res: dict) -> dict:
    """The KPI row's energy figures of telemetry.genset_runs' answer; without any genset reading in the period they are unknown (null), not zero."""
    runs = res["runs"]
    if not res["signal"]["present"]:
        return {"hours": None, "starts": None, "kwh": None, "litres": None, "litres_metered": None, "litres_curve": None, "litres_source": None,
                "estimated": False, "avg_load_ratio": None, "hours_below_min": None}
    hours = sum(r["hours"] for r in runs)
    curve = sum(r["litres_curve"] for r in runs)
    source = "curve" if curve > 0 else "meter" if (runs or res["fuel_read"]) else None
    ratios = [(r["load_ratio"], r["hours"]) for r in runs if r["load_ratio"] is not None]
    return {"hours": round(hours, 1), "starts": len(runs), "kwh": round(sum(r["kwh"] for r in runs), 1), "litres": round(sum(r["litres"] for r in runs), 1),
            "litres_metered": round(sum(r["litres_metered"] for r in runs), 1), "litres_curve": round(curve, 1), "litres_source": source,
            "estimated": source == "curve", "avg_load_ratio": round(sum(lr * h for lr, h in ratios) / hours, 2) if hours and ratios else None,
            "hours_below_min": round(sum(r["hours_below_min"] for r in runs), 1)}


def _measured_points(runs: list[dict]) -> list[dict]:
    """The runs that measure the fuel curve: litres all metered, output all read, at least POINT_MIN_H long."""
    return [{"start": r["start"], "load_ratio": r["load_ratio"], "litres_per_hour": round(r["litres"] / r["hours"], 2), "hours": r["hours"]}
            for r in runs if r["litres_source"] == "meter" and not r["kwh_estimated"] and r["hours"] >= POINT_MIN_H and r["litres"] > 0
            and r["load_ratio"] is not None]


def _fuel_logged(site_id: str, d0: str, d1: str) -> dict:
    """The fuel logged for the Karachi days d0..d1: {litres, entries}."""
    with store.connect() as con:
        rows = con.execute("SELECT detail FROM events WHERE site_id = ? AND type = 'fuel_log'", (site_id,)).fetchall()
    litres, n = 0.0, 0
    for r in rows:
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        if isinstance(d.get("litres"), (int, float)) and d0 <= str(d.get("day") or "") <= d1:
            litres, n = litres + float(d["litres"]), n + 1
    return {"litres": round(litres, 1), "entries": n}


@router.get("/api/sites/{site_id}/genset")
def genset_page(site_id: str, days: int = 14, month: str | None = None):
    """UI-11: the period's KPIs, runs, daily hours and litres, the fuel curve with its measured points, the price, the plan's requests and steps, the
    policy. Litres are metered where a fuel meter reads and from the fuel curve where not (`litres_source` as on the Savings page); each run's fuel is
    priced at the fuel price of its own Karachi day (`price_per_l`, `fuel_cost_rs`); without a genset reading in the period (`signal.present` false) the
    hours, starts and litres are unknown (null), not zero."""
    s = full_or_404(site_id)
    g = (s.get("assets") or {}).get("genset")
    if not g:
        return {"genset": None}
    now = now_ts()
    if month is not None:
        t0, t1 = _month_window(month, now)
        period = {"kind": "month", "month": month.strip(), "start": t0, "end": t1, "days": None}
    else:
        days = max(1, min(int(days), GENSET_MAX_DAYS))
        t0, t1 = now - days * 86400, now
        period = {"kind": "days", "month": None, "start": t0, "end": t1, "days": days}
    res = telemetry.genset_runs(site_id, t0, t1, g)
    runs = res["runs"]
    from . import tariffs
    kind = g.get("fuel_kind", "hsd")
    prices: dict[str, float] = {}

    def price_on(ts: int) -> float:
        day = datetime.fromtimestamp(ts, PKT).date().isoformat()
        if day not in prices:
            prices[day] = tariffs.fuel_price(kind, day)[0]
        return prices[day]

    for r in runs:
        p = plans.active_at(site_id, r["start"])
        stp = next((x for x in (p or {}).get("steps", []) if x["t"] <= r["start"] < x["t"] + 900), None)
        r["plan_requested"] = bool(stp and stp["genset_on"])
        r["price_per_l"] = price_on(r["start"])
        r["fuel_cost_rs"] = round(r["litres"] * r["price_per_l"], 0)
    kpis = _run_kpis(res)
    known = kpis["litres"] is not None
    kpis["fuel_cost_rs"] = round(sum(r["fuel_cost_rs"] for r in runs), 0) if known else None
    kpis["fuel_rs_per_kwh"] = round(kpis["fuel_cost_rs"] / kpis["kwh"], 2) if known and kpis["kwh"] else None
    price, src = tariffs.fuel_price(kind, today())
    used = [r["price_per_l"] for r in runs]
    answer = {"genset": g, "control": s["data"].get("genset_start_control"), "period": period, "signal": res["signal"], "kpis": kpis,
              "fuel_price": {"price_per_l": price, "source": src, "kind": kind, "range_per_l": [min(used), max(used)] if used else None},
              "runs": list(reversed(runs)), "daily": res["daily"],
              "curve": {"a": g["a"], "b": g["b"], "rated_kw": g["rated_kw"],
                        "points": [{"load_ratio": lr, "litres_per_hour": round(g["a"] * lr * g["rated_kw"] + g["b"] * g["rated_kw"], 2)}
                                   for lr in (0.25, 0.5, 0.75, 1.0)],
                        "measured": _measured_points(runs)},
              "fuel_logged": _fuel_logged(site_id, res["daily"][0]["day"], res["daily"][-1]["day"]) if res["daily"] else {"litres": 0.0, "entries": 0},
              "requests": alerts.requests_for(site_id), "plan": [{"t": x["t"], "genset_kw": x["genset_kw"], "reason": x["reason"]}
                                                                  for x in (plans.current(site_id) or {}).get("steps", []) if x["genset_on"]][:96],
              "policy": "Gridlytics never starts or stops the genset itself: it asks, with a reason, and records whether the advice was followed."}
    if period["kind"] == "month":
        pm = _previous_month(period["month"])
        p0, p1 = _month_window(pm, now)
        prev = _run_kpis(telemetry.genset_runs(site_id, p0, p1, g))
        answer["previous"] = {"month": pm, **{k: prev[k] for k in ("hours", "starts", "litres", "kwh")}}
    return out(answer)


class CurvePoint(Strict):
    """A point of the genset's data sheet: litres (or the gas unit) an hour at a load ratio (output over rating)."""
    load_ratio: float = Field(gt=0, le=1.1)
    litres_per_hour: float = Field(gt=0, le=100000)


class RefitIn(Strict):
    #: data-sheet points; none: the metered runs of the last REFIT_DAYS days
    points: list[CurvePoint] | None = Field(None, max_length=12)
    #: save the fitted a and b to the genset (edit_sites); otherwise the fit is only shown
    apply: bool = False


def fit_curve(points: list[tuple[float, float, float]], rated_kw: float) -> dict:
    """a and b of litres an hour = a x output kW + b x rated kW (HOMER's convention, sites.GensetFields) by weighted least squares over (load ratio,
    litres an hour, weight) points, b at least 0. Raises ValueError when the points cannot fix a slope or give one GensetFields refuses."""
    x = np.array([p[0] for p in points], dtype=float)
    y = np.array([p[1] for p in points], dtype=float) / rated_kw
    w = np.array([max(1e-6, p[2]) for p in points], dtype=float)
    if len(x) < 2 or float(x.max() - x.min()) < REFIT_MIN_SPREAD:
        raise ValueError(f"A fuel curve needs points at loads at least {int(REFIT_MIN_SPREAD * 100)} % of the rating apart.")
    xm, ym = np.average(x, weights=w), np.average(y, weights=w)
    a = float(np.sum(w * (x - xm) * (y - ym)) / np.sum(w * (x - xm) ** 2))
    b = ym - a * xm
    if b < 0:
        a, b = float(np.sum(w * x * y) / np.sum(w * x * x)), 0.0
    if not (0 < a <= 5) or not (0 <= b <= 1):
        raise ValueError("Those points do not give a fuel curve that rises with the load: check the litres an hour against the load.")
    rmse = float(np.sqrt(np.average((a * x + b - y) ** 2, weights=w))) * rated_kw
    return {"a": round(a, 4), "b": round(b, 4), "rmse_lph": round(rmse, 2)}


@router.post("/api/sites/{site_id}/genset/curve/refit")
def curve_refit(site_id: str, inp: dict | None = Body(None)):
    """Fits the genset's fuel curve (a, b) from data-sheet points, or, without points, from the metered runs of the last REFIT_DAYS days (their measured
    points as on the genset page; at least REFIT_MIN_POINTS); with `apply` the fit is saved to the genset (edit_sites), otherwise only shown. Answers
    {a, b, rmse_lph, rated_kw, from (data_sheet or meter), points_used, points (the fitted curve at 25 to 100 %), before {a, b}, applied}."""
    body = check(RefitIn, inp or {})
    if body.apply:
        auth.need("edit_sites", "Saving the genset's fuel curve")
    s = full_or_404(site_id)
    g = (s.get("assets") or {}).get("genset")
    if not g:
        raise bad(ValueError("This site has no genset."))
    s_kw = float(g["rated_kw"])
    if body.points:
        pts = [(p.load_ratio, p.litres_per_hour, 1.0) for p in body.points]
        source = "data_sheet"
    else:
        now = now_ts()
        measured = _measured_points(telemetry.genset_runs(site_id, now - REFIT_DAYS * 86400, now, g)["runs"])
        pts = [(p["load_ratio"], p["litres_per_hour"], p["hours"]) for p in measured]
        loads = [p[0] for p in pts]
        if len(pts) < REFIT_MIN_POINTS or max(loads) - min(loads) < REFIT_MIN_SPREAD:
            raise bad(ValueError(f"Too few metered runs to refit the curve: {len(pts)} in the last {REFIT_DAYS} days with a fuel meter and the output read "
                                 f"({REFIT_MIN_POINTS} needed, at loads at least {int(REFIT_MIN_SPREAD * 100)} % of the rating apart). Enter the data "
                                 "sheet's points instead."))
        source = "meter"
    try:
        fit = fit_curve(pts, s_kw)
    except ValueError as exc:
        raise bad(exc) from None
    applied = False
    if body.apply:
        fields = {k: v for k, v in g.items() if k != "id"}
        try:
            sites.put_asset(site_id, "genset", {**fields, "a": fit["a"], "b": fit["b"]}, auth.actor())
        except ValueError as exc:
            raise bad(exc) from None
        applied = True
    return {**fit, "rated_kw": s_kw, "from": source, "points_used": len(pts), "applied": applied, "before": {"a": g["a"], "b": g["b"]},
            "points": [{"load_ratio": lr, "litres_per_hour": round(fit["a"] * lr * s_kw + fit["b"] * s_kw, 2)} for lr in (0.25, 0.5, 0.75, 1.0)]}


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
