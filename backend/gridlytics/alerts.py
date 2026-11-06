"""Alerts and genset requests (plan Part 9.3 UI-16 and UI-11): one list of what needs a human, raised and cleared by rules each tick.

Categories: Data, Control, Reserve, Battery, Genset, Forecast, Tariff, Savings, Product. An alert is raised once per (site, rule) while its condition
holds and closed automatically when it clears; people acknowledge, assign and close them. Advice to start a genset is a request with a reason; the
product never starts or stops a genset itself.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import battery as battery_service
from . import plans, sites, store, telemetry, twin

PKT = timezone(timedelta(hours=5))
CATEGORIES = ("Data", "Control", "Reserve", "Battery", "Genset", "Forecast", "Tariff", "Savings", "Product")
SEVERITIES = ("info", "warning", "critical")
DEFAULT_RULES = {c: {"channels": ["in_app"], "quiet_hours": None, "escalate_after_min": None} for c in CATEGORIES}


def _open(site_id: str, rule: str, category: str, severity: str, title: str, detail: dict | None = None) -> None:
    with store.connect() as con:
        r = con.execute("SELECT id FROM alerts WHERE site_id = ? AND rule = ? AND state IN ('open','acknowledged')", (site_id, rule)).fetchone()
        if r:
            con.execute("UPDATE alerts SET title = ?, detail = ?, severity = ? WHERE id = ?", (title, json.dumps(detail or {}), severity, r["id"]))
        else:
            con.execute("INSERT INTO alerts (id, site_id, rule, category, severity, title, detail, opened_at) VALUES (?,?,?,?,?,?,?,?)",
                        (store.new_id(), site_id, rule, category, severity, title, json.dumps(detail or {}), store._now()))


def _clear(site_id: str, rule: str) -> None:
    with store.connect() as con:
        con.execute("UPDATE alerts SET state = 'resolved', closed_at = ?, note = 'Condition cleared' WHERE site_id = ? AND rule = ? AND state IN ('open','acknowledged')",
                    (store._now(), site_id, rule))


def _set(cond: bool, site_id: str, rule: str, category: str, severity: str, title: str, detail: dict | None = None) -> None:
    (_open(site_id, rule, category, severity, title, detail) if cond else _clear(site_id, rule))


#: Staleness by data source (plan Part 2.4), as minutes before a warning and before a critical: an edge, HTTPS batch or twin source must be younger than 30
#: minutes and an OEM-cloud source younger than 20; a CSV source is fed by uploads, so it is late only after 24 hours without one
STALE_MINUTES = {"csv": (24 * 60, 48 * 60), "oem_cloud": (20, 60), "oem": (20, 60)}
STALE_DEFAULT = (30, 120)


def _last_upload(site_id: str) -> int | None:
    with store.connect() as con:
        r = con.execute("SELECT created FROM imports WHERE site_id = ? ORDER BY created DESC LIMIT 1", (site_id,)).fetchone()
    try:
        return int(datetime.fromisoformat(r["created"]).timestamp()) if r else None
    except (TypeError, ValueError):
        return None


def _stale(site_id: str, last: dict | None, now: int) -> None:
    source = str((last or {}).get("source_id") or "")
    warn, crit = STALE_MINUTES.get(source, STALE_DEFAULT)
    if last is None:
        age, title = None, ""
    elif source == "csv":
        up = _last_upload(site_id)
        age = (now - max(last["ts"], up or 0)) / 60
        title = f"No CSV upload for {int(age // 60)} hours"
    else:
        age = (now - last["ts"]) / 60
        title = f"No data for {int(age)} minutes"
    _set(age is not None and age > warn, site_id, "data_stale", "Data", "critical" if (age or 0) > crit else "warning", title,
         {"last_ts": last["ts"] if last else None, "source": source or None})


def _peak_unconfirmed(site: dict, now: int, today: str) -> bool:
    """An advisory (tier 2) site whose plan discharged into today's peak, 30 minutes or more after that first peak step, with no 'applied' mark for today.
    The step is looked up in the plan that was in force at it (the current plan always starts at this quarter, so it never holds a step that old)."""
    with store.connect() as con:
        if con.execute("SELECT 1 FROM applied WHERE site_id = ? AND day_local = ?", (site["id"], today)).fetchone() is not None:
            return False
    conn = (site.get("connections") or [None])[0]
    if conn is None:
        return False
    d = date.fromisoformat(today)
    t0 = int(datetime(d.year, d.month, d.day, tzinfo=PKT).timestamp())
    quarters = np.arange(t0, t0 + 86400, 900, dtype=np.int64)
    try:
        peak = np.asarray(plans.peak_mask(conn, quarters), dtype=bool)
    except (LookupError, ValueError, TypeError):
        return False
    for t in quarters[peak & (quarters <= now - 1800)]:
        p = plans.active_at(site["id"], int(t))
        st = next((s for s in (p or {}).get("steps", []) if s["t"] <= t < s["t"] + 900), None)
        if st and st.get("peak") and (st.get("discharge_kw") or 0) > 0.1:
            return True
    return False


def evaluate(site_id: str, now: int | None = None) -> None:
    site = sites.full(site_id)
    if site is None:
        return
    now = int(now or datetime.now(timezone.utc).timestamp())
    last = telemetry.latest(site_id)
    _stale(site_id, last, now)
    plan = plans.current(site_id)
    latest_attempt = (plan or {}).get("latest_attempt")
    _set(bool(latest_attempt), site_id, "plan_failed", "Control", "warning",
         "The last solve did not produce a plan; the previous plan stays active", {"reason": (latest_attempt or {}).get("reason")})
    # reserve: outages in the last day entered below the reserve the plan asked for, and unserved critical load
    outs = telemetry.outages(site_id, now - 86400, now)
    short = []
    if plan and (site.get("assets") or {}).get("battery"):
        for o in outs:
            step = plans.active_at(site_id, o["start"])
            st = next((s for s in (step or {}).get("steps", []) if s["t"] <= o["start"] < s["t"] + 900), None)
            if st and st.get("reserve_pct") and o["soc_start_pct"] is not None and o["soc_start_pct"] + 2 < st["reserve_pct"]:
                short.append({"start": o["start"], "soc": o["soc_start_pct"], "reserve": st["reserve_pct"]})
    _set(bool(short), site_id, "reserve_short", "Reserve", "warning", "An outage started with the battery below the planned reserve", {"outages": short[:5]})
    unserved = sum(o["unserved_kwh"] for o in outs)
    _set(unserved > 0.5, site_id, "unserved", "Reserve", "critical", f"{unserved:.1f} kWh of critical load went unserved in the last day")
    # battery
    bs = battery_service.state(site, now)
    held = {a[0] for a in (bs or {}).get("alerts", [])}
    for rule in ("trust_low", "soc_disagreement", "sigma_high", "hot_high_soc", "imbalance", "capacity_drop", "derate"):
        text = next((a[1] for a in (bs or {}).get("alerts", []) if a[0] == rule), "")
        _set(rule in held, site_id, f"battery_{rule}", "Battery", "warning", text)
    # control: advisory tier with a peak slot today and no "applied" mark 30 minutes after it started
    tier = site.get("tier", "monitor")
    today = datetime.fromtimestamp(now, PKT).date().isoformat()
    unconfirmed = False
    if tier == "advisory_writes" and plan and not site.get("demo_link"):
        unconfirmed = _peak_unconfirmed(site, now, today)
    if site.get("demo_link") and site["data"].get("twin", {}).get("unconfirmed_alert"):
        unconfirmed = True
    _set(unconfirmed, site_id, "apply_unconfirmed", "Control", "warning",
         "Today's slot table is not marked as applied: tier 2 has no read-back, so the saving is not claimed until it is")
    # tariff: a regime ending within 60 days
    for c in site.get("connections") or []:
        end = c["data"].get("agreement_end")
        soon = False
        if end:
            try:
                soon = (date.fromisoformat(end) - datetime.fromtimestamp(now, PKT).date()).days <= 60
            except ValueError:
                soon = False
        _set(soon, site_id, f"regime_expiring_{c['id']}", "Tariff", "warning", f"The {c['data']['regime'].replace('_', ' ')} agreement ends on {end}",
             {"connection": c["id"]})
    genset_requests(site, plan, now)


def _runs(steps: list[dict]) -> list[tuple[int, int]]:
    """The plan's genset runs as (first step, step after the last)."""
    out, i = [], 0
    while i < len(steps):
        if steps[i]["genset_on"]:
            j = i
            while j < len(steps) and steps[j]["genset_on"]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def _withdraw_requests(site_id: str, spans: list[tuple[int, int]], running: bool, now: int) -> None:
    """Closes the open genset requests that no longer stand: their run is over, the set is running, or the current plan no longer runs it."""
    with store.connect() as con:
        rows = con.execute("SELECT id, detail FROM alerts WHERE site_id = ? AND rule LIKE 'genset_request_%' AND state IN ('open','acknowledged')",
                           (site_id,)).fetchall()
        for r in rows:
            p = json.loads(r["detail"] or "{}")
            start, end = p.get("start"), p.get("end")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            lo = max(start, now)
            if now >= end:
                note = "The planned run is over"
            elif running and start <= now:
                note = "The genset is running"
            elif not any(a < end and b > lo for a, b in spans):
                note = "No longer planned"
            else:
                continue
            con.execute("UPDATE alerts SET state = 'resolved', closed_at = ?, note = ? WHERE id = ?", (store._now(), note, r["id"]))
            if note == "No longer planned":
                con.execute("UPDATE notifications SET status = 'withdrawn' WHERE site_id = ? AND kind = 'genset_request' AND status = 'pending' AND payload LIKE ?",
                            (site_id, f'%"genset_request:{start}"%'))


def genset_requests(site: dict, plan: dict | None, now: int) -> None:
    """At a manual-start genset site, one request per planned run starting in the next two hours. The plan is re-solved every quarter, so its first step
    moves forward each time: a run the set is already doing (telemetry says it runs) or one already asked about (an earlier request's window holds its
    start) is not asked again, and open requests whose run is over, is running or is no longer planned are closed."""
    if not plan or site["data"].get("genset_start_control") != "manual" or not (site.get("assets") or {}).get("genset"):
        return
    sid = site["id"]
    steps = plan.get("steps") or []
    last = telemetry.latest(sid)
    running = bool(last and now - last["ts"] <= 1800 and (last.get("genset_running") or 0) >= 0.5)
    runs = _runs(steps)
    spans = [(steps[i]["t"], steps[j - 1]["t"] + 900) for i, j in runs]
    _withdraw_requests(sid, spans, running, now)
    with store.connect() as con:
        held = [(r["status"], json.loads(r["payload"])) for r in con.execute(
            "SELECT status, payload FROM notifications WHERE site_id = ? AND kind = 'genset_request' ORDER BY ts DESC LIMIT 100", (sid,)).fetchall()]
    for (i, j), (start, end) in zip(runs, spans):
        s = steps[i]
        if start < now - 900 or start > now + 7200:
            continue
        if i == 0 and running:
            continue                                  # the plan carries on a run the set is already doing: not a start
        key = f"genset_request:{start}"
        if any(status != "withdrawn" and (p.get("key") == key or (isinstance(p.get("start"), (int, float)) and isinstance(p.get("end"), (int, float))
                                                                  and p["start"] <= start < p["end"])) for status, p in held):
            continue                                  # this run was asked about already
        payload = {"key": key, "start": start, "end": end, "kw": s["genset_kw"], "reason": s["reason"], "detail": s["detail"], "plan_id": plan["id"]}
        with store.connect() as con:
            con.execute("INSERT INTO notifications (id, site_id, ts, kind, payload) VALUES (?,?,?,?,?)",
                        (store.new_id(), sid, store._now(), "genset_request", json.dumps(payload)))
        held.append(("pending", payload))
        _open(sid, f"genset_request_{start}", "Genset", "info", f"Please start the genset at {s['local']} ({s['genset_kw']:.0f} kW)", payload)


def requests_for(site_id: str, limit: int = 50) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM notifications WHERE site_id = ? AND kind = 'genset_request' ORDER BY ts DESC LIMIT ?", (site_id, limit)).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def answer_request(site_id: str, nid: str, outcome: str, by: str) -> dict:
    if outcome not in ("done", "not_now"):
        raise ValueError("The answer is done or not_now")
    with store.connect() as con:
        r = con.execute("SELECT * FROM notifications WHERE id = ? AND site_id = ?", (nid, site_id)).fetchone()
        if r is None:
            raise KeyError(nid)
        con.execute("UPDATE notifications SET status = 'answered', outcome = ?, scored_at = ? WHERE id = ?", (outcome, store._now(), nid))
        payload = json.loads(r["payload"])
        con.execute("UPDATE alerts SET state = 'closed', closed_at = ?, note = ? WHERE site_id = ? AND rule = ? AND state != 'closed'",
                    (store._now(), f"Answered {outcome} by {by}", site_id, f"genset_request_{payload['start']}"))
    store.audit(by, f"answered a genset request: {outcome}", site_id, {"notification": nid})
    return {"id": nid, "outcome": outcome}


# ------------------------------------------------------------------------------------------------ the inbox

def listing(state: str | None = None, site_id: str | None = None, category: str | None = None, limit: int = 300) -> list[dict]:
    q, args = "SELECT * FROM alerts WHERE 1=1", []
    if state == "active":
        q += " AND state IN ('open','acknowledged')"
    elif state:
        q += " AND state = ?"
        args.append(state)
    if site_id:
        q += " AND site_id = ?"
        args.append(site_id)
    if category:
        q += " AND category = ?"
        args.append(category)
    q += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, opened_at DESC LIMIT ?"
    args.append(limit)
    with store.connect() as con:
        rows = con.execute(q, args).fetchall()
    names = {s["id"]: s["name"] for s in sites.listing()}
    return [{**dict(r), "detail": json.loads(r["detail"]), "site_name": names.get(r["site_id"], "")} for r in rows]


def counts() -> dict:
    with store.connect() as con:
        rows = con.execute("SELECT severity, COUNT(*) n FROM alerts WHERE state IN ('open','acknowledged') GROUP BY severity").fetchall()
        by_site = con.execute("SELECT site_id, COUNT(*) n FROM alerts WHERE state IN ('open','acknowledged') GROUP BY site_id").fetchall()
    out = {r["severity"]: r["n"] for r in rows}
    return {"total": sum(out.values()), "by_severity": out, "by_site": {r["site_id"]: r["n"] for r in by_site}}


def act(alert_id: str, action: str, by: str, note: str = "", assignee: str = "") -> dict:
    with store.connect() as con:
        r = con.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if r is None:
            raise KeyError(alert_id)
        if action == "acknowledge":
            con.execute("UPDATE alerts SET state = 'acknowledged', note = ? WHERE id = ?", (note or f"Acknowledged by {by}", alert_id))
        elif action == "assign":
            con.execute("UPDATE alerts SET assigned = ? WHERE id = ?", (assignee[:120], alert_id))
        elif action == "close":
            con.execute("UPDATE alerts SET state = 'closed', closed_at = ?, note = ? WHERE id = ?", (store._now(), note or f"Closed by {by}", alert_id))
        else:
            raise ValueError("The action is acknowledge, assign or close")
    store.audit(by, f"{action}d an alert" if action != "assign" else "assigned an alert", r["site_id"], {"alert": alert_id})
    if action == "close":
        return {"id": alert_id, "state": "closed"}
    # the alert itself, read by its id (the inbox listing is capped, so an alert outside its first rows would not be found there)
    with store.connect() as con:
        r2 = con.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    return {**dict(r2), "detail": json.loads(r2["detail"] or "{}"), "site_name": (sites.get(r2["site_id"]) or {}).get("name", "")}


def rules() -> dict:
    return store.get_setting("alert_rules", DEFAULT_RULES)


def set_rules(body: dict, by: str) -> dict:
    out = dict(DEFAULT_RULES)
    for cat, v in (body or {}).items():
        if cat not in CATEGORIES or not isinstance(v, dict):
            raise ValueError(f"Unknown alert category: {cat}")
        ch = [c for c in v.get("channels", ["in_app"]) if c in ("in_app", "email", "sms", "whatsapp")]
        out[cat] = {"channels": ch or ["in_app"], "quiet_hours": v.get("quiet_hours"), "escalate_after_min": v.get("escalate_after_min")}
    store.set_setting("alert_rules", out)
    store.audit(by, "changed the alert rules")
    return out


def tariff_pending_count() -> int:
    with store.connect() as con:
        return con.execute("SELECT COUNT(*) FROM tariff_versions WHERE status = 'pending'").fetchone()[0]


def nightly(site_id: str, day: date) -> None:
    """Forecast-bust and savings checks after the nightly jobs."""
    from . import forecast
    rows = [r for r in forecast.score_day(sites.full(site_id), day)] if sites.get(site_id) else []
    pv = next((r for r in rows if r["kind"] == "pv"), None)
    _set(bool(pv and pv["value"] > 0.6), site_id, "forecast_bust", "Forecast", "info",
         f"Yesterday's PV forecast missed by {pv['value'] * 100:.0f} percent (nRMSE)" if pv else "")
    _ = twin
