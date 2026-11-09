"""Alerts and genset requests (plan Part 9.3 UI-16 and UI-11): one list of what needs a human, raised and cleared by rules each tick.

Categories: Data, Control, Reserve, Battery, Genset, Forecast, Tariff, Savings, Product. An alert is raised once per (site, rule) while its condition
holds and closed automatically when it clears; people acknowledge, assign and close them. Advice to start a genset is a request with a reason; the
product never starts or stops a genset itself.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import numpy as np
from pydantic import Field, ValidationError

from . import auth
from . import battery as battery_service
from . import plans, sites, store, telemetry, twin
from .schemas import Strict, message

PKT = timezone(timedelta(hours=5))
STEP_S = 900  # a plan step
CATEGORIES = ("Data", "Control", "Reserve", "Battery", "Genset", "Forecast", "Tariff", "Savings", "Product")
SEVERITIES = ("info", "warning", "critical")
#: The categories every member receives: the energy kind (plan Part 10.5, "a viewer gets ... only energy-category alerts"; R8 2.16, "Viewer sees ...
#: alerts of the energy kind (outage, genset, data) but no Savings or Product alerts"). The others need a permission: Control, Tariff and Savings alerts
#: speak of plans, prices and claims (`see_costs`); Product alerts are fee and contract notices (`see_fee`)
ENERGY_CATEGORIES = ("Data", "Reserve", "Battery", "Genset", "Forecast")
CATEGORY_NEEDS = {"Control": "see_costs", "Tariff": "see_costs", "Savings": "see_costs", "Product": "see_fee"}


def reader_categories() -> tuple[str, ...]:
    """The categories the current reader receives in the inbox, the counts, a site's Now page and the fleet: every category when nobody reads (the
    scheduler, a job, a test calling the module), else those the reader's role holds the permission for. An API key holds no role permission, so it
    gets the energy categories only."""
    if auth.current.get() is None:
        return CATEGORIES
    return tuple(c for c in CATEGORIES if c not in CATEGORY_NEEDS or auth.can(CATEGORY_NEEDS[c]))


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


def _activated(site_id: str) -> int | None:
    """When the site was last activated (its audit line), else when it was added: the start of the wait for its first reading."""
    with store.connect() as con:
        r = con.execute("SELECT at FROM audit WHERE action = 'activated a site' AND target = ? ORDER BY id DESC LIMIT 1", (site_id,)).fetchone()
        if r is None:
            r = con.execute("SELECT created AS at FROM sites WHERE id = ?", (site_id,)).fetchone()
    try:
        return int(datetime.fromisoformat(r["at"]).timestamp()) if r else None
    except (TypeError, ValueError):
        return None


def _stale(site_id: str, last: dict | None, now: int, stale_minutes: float | None = None) -> None:
    """The Data alert of a source gone quiet, by its kind's limits, or after `stale_minutes` when the rules set it (critical after the same multiple
    of it as the kind's critical limit is of its warning); a site that never sent a reading waits from its activation (pages-D-27: on day one the
    Data category fires first, R8 2.16), under the limits of the kind of source it was set up with."""
    source = str((last or {}).get("source_id") or "")
    if last is None:
        source = str(((sites.get(site_id) or {}).get("data", {}).get("data_source") or {}).get("kind") or "")
    warn, crit = STALE_MINUTES.get(source, STALE_DEFAULT)
    if stale_minutes:
        warn, crit = float(stale_minutes), float(stale_minutes) * crit / warn
    if last is None:
        since = _activated(site_id)
        age = (now - since) / 60 if since is not None else None
        hours = (age or 0) / 60
        title = ("No data has arrived since the site was activated "
                 + (f"{int(age)} minutes ago" if (age or 0) < 120 else f"{int(hours)} hours ago" if hours < 48 else f"{int(hours // 24)} days ago"))
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


#: Control and Savings alerts wait until the site has an active plan and this many days of data (plan Part 9.3, UI-16): a new site's first days would
#: otherwise raise them on day one (an advisory site's unconfirmed apply before anyone was told to apply a table)
SETTLED_CATEGORIES = ("Control", "Savings")
SETTLED_DAYS = 7


def settled(site_id: str, plan: dict | None, now: int) -> bool:
    """True once Control and Savings alerts may be raised for the site: it has an active plan and its data reaches back SETTLED_DAYS days."""
    if not plan:
        return False
    first = telemetry.first_ts(site_id)
    return first is not None and now - int(first) >= SETTLED_DAYS * 86400


def evaluate(site_id: str, now: int | None = None) -> None:
    """Raises and clears the site's alerts. A Control or Savings condition counts only once the site is `settled`: before, it is not raised and an open
    one is closed."""
    site = sites.full(site_id)
    if site is None:
        return
    now = int(now or datetime.now(timezone.utc).timestamp())
    rules_ = site_rules(site_id)
    last = telemetry.latest(site_id)
    _stale(site_id, last, now, _th(rules_, "Data", "stale_minutes"))
    plan = plans.current(site_id)
    ready = settled(site_id, plan, now)
    latest_attempt = (plan or {}).get("latest_attempt")
    _set(ready and bool(latest_attempt), site_id, "plan_failed", "Control", "warning",
         "The last solve did not produce a plan; the previous plan stays active", {"reason": (latest_attempt or {}).get("reason")})
    # reserve: outages in the last day entered below the reserve the plan asked for, and unserved critical load
    outs = telemetry.outages(site_id, now - 86400, now)
    margin = float(_th(rules_, "Reserve", "reserve_margin_pct"))
    short = []
    if plan and (site.get("assets") or {}).get("battery"):
        for o in outs:
            step = plans.active_at(site_id, o["start"])
            st = next((s for s in (step or {}).get("steps", []) if s["t"] <= o["start"] < s["t"] + 900), None)
            if st and st.get("reserve_pct") and o["soc_start_pct"] is not None and o["soc_start_pct"] + margin < st["reserve_pct"]:
                short.append({"start": o["start"], "soc": o["soc_start_pct"], "reserve": st["reserve_pct"]})
    _set(bool(short), site_id, "reserve_short", "Reserve", "warning", "An outage started with the battery below the planned reserve", {"outages": short[:5]})
    unserved = sum(o["unserved_kwh"] for o in outs)
    _set(unserved > float(_th(rules_, "Reserve", "unserved_kwh")), site_id, "unserved", "Reserve", "critical", f"{unserved:.1f} kWh of critical load went unserved in the last day")
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
    _set(ready and unconfirmed, site_id, "apply_unconfirmed", "Control", "warning",
         "Today's slot table is not marked as applied: tier 2 has no read-back, so the saving is not claimed until it is")
    # tariff: a regime ending within the rules' days (60 by default)
    for c in site.get("connections") or []:
        end = c["data"].get("agreement_end")
        soon = False
        if end:
            try:
                soon = (date.fromisoformat(end) - datetime.fromtimestamp(now, PKT).date()).days <= _th(rules_, "Tariff", "agreement_days")
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


def _withdraw_requests(site_id: str, spans: list[tuple[int, int]], running: bool, now: int, unplanned: str = "No longer planned") -> None:
    """Closes the open genset requests that no longer stand: their run is over, the set is running, or the current plan no longer runs it (then the
    request is withdrawn, with `unplanned` as the note: no plan, or control paused or released, runs nothing)."""
    with store.connect() as con:
        rows = con.execute("SELECT id, detail FROM alerts WHERE site_id = ? AND rule LIKE 'genset_request_%' AND state IN ('open','acknowledged')",
                           (site_id,)).fetchall()
        for r in rows:
            p = json.loads(r["detail"] or "{}")
            start, end = p.get("start"), p.get("end")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            lo = max(start, now)
            withdraw = False
            if now >= end:
                note = "The planned run is over"
            elif running and start <= now:
                note = "The genset is running"
            elif not any(a < end and b > lo for a, b in spans):
                note, withdraw = unplanned, True
            else:
                continue
            con.execute("UPDATE alerts SET state = 'resolved', closed_at = ?, note = ? WHERE id = ?", (store._now(), note, r["id"]))
            if withdraw:
                con.execute("UPDATE notifications SET status = 'withdrawn' WHERE site_id = ? AND kind = 'genset_request' AND status = 'pending' AND payload LIKE ?",
                            (site_id, f'%"genset_request:{start}"%'))


def _overlaps(p: dict, start: int, end: int) -> bool:
    """True when a held request's planned window [start, end) overlaps the run [start, end)."""
    a, b = p.get("start"), p.get("end")
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and a < end and start < b


def genset_requests(site: dict, plan: dict | None, now: int) -> None:
    """At a manual-start genset site, one request per planned run starting in the next two hours. The plan is re-solved every quarter, so a run's start
    moves (forward as time passes, or earlier or later as the forecast changes): a run the set is already doing (telemetry says it runs) or one already
    asked about (a held request that was not withdrawn overlaps it) is not asked again, so one run never holds two pending requests. When the run's start
    moves a step or more earlier than its pending request's, the request would have the set started late: it is withdrawn ("Moved earlier to HH:MM")
    and the run asked for at its new start. Open requests whose run is over, is running or is no longer planned are closed; with no plan in force (none
    yet, or control paused or released to native) every open request is withdrawn."""
    if site["data"].get("genset_start_control") != "manual" or not (site.get("assets") or {}).get("genset"):
        return
    sid = site["id"]
    last = telemetry.latest(sid)
    running = bool(last and now - last["ts"] <= 1800 and (last.get("genset_running") or 0) >= 0.5)
    released = plans.control_release(sid, now)
    if not plan or released:
        why = {"control_paused": "Control is paused", "released_to_native": "Released to native"}.get((released or {}).get("reason"), "No plan in force")
        _withdraw_requests(sid, [], running, now, why)
        return
    steps = plan.get("steps") or []
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
        asked = [(status, p) for status, p in held if status != "withdrawn" and (p.get("key") == key or _overlaps(p, start, end))]
        # a pending request whose start is a step or more later than the run's start now would have the set started late (at a manual-start site the
        # operator starts it when asked): it is withdrawn and the run asked for again at its new start. Otherwise a run asked about already (pending at
        # this start or within a step of it, or answered) is not asked again, so one run still holds one pending request.
        later = [p for status, p in asked if status == "pending" and isinstance(p.get("start"), (int, float)) and p["start"] - start >= STEP_S]
        if len(later) < len(asked):
            continue
        if later:
            note = f"Moved earlier to {s['local']}"
            with store.connect() as con:
                for p in later:
                    old = int(p["start"])
                    old_key = p.get("key") or f"genset_request:{old}"
                    con.execute("UPDATE notifications SET status = 'withdrawn' WHERE site_id = ? AND kind = 'genset_request' AND status = 'pending' "
                                "AND payload LIKE ?", (sid, f'%"{old_key}"%'))
                    con.execute("UPDATE alerts SET state = 'resolved', closed_at = ?, note = ? WHERE site_id = ? AND rule = ? AND state IN ('open','acknowledged')",
                                (store._now(), note, sid, f"genset_request_{old}"))
            held = [("withdrawn", p) if any(p is q for q in later) else (status, p) for status, p in held]
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

def _in(cats: tuple[str, ...]) -> tuple[str, list]:
    return f" AND category IN ({','.join('?' * len(cats))})", list(cats)


def listing(state: str | None = None, site_id: str | None = None, category: str | None = None, limit: int = 300) -> list[dict]:
    """The inbox, most severe first: only the categories the reader receives (`reader_categories`)."""
    cond, args = _in(reader_categories())
    q = "SELECT * FROM alerts WHERE 1=1" + cond
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
    """The open and acknowledged alerts by severity and by site, of the categories the reader receives (named in `categories`)."""
    cats = reader_categories()
    cond, args = _in(cats)
    with store.connect() as con:
        rows = con.execute("SELECT severity, COUNT(*) n FROM alerts WHERE state IN ('open','acknowledged')" + cond + " GROUP BY severity", args).fetchall()
        by_site = con.execute("SELECT site_id, COUNT(*) n FROM alerts WHERE state IN ('open','acknowledged')" + cond + " GROUP BY site_id", args).fetchall()
    out = {r["severity"]: r["n"] for r in rows}
    return {"total": sum(out.values()), "by_severity": out, "by_site": {r["site_id"]: r["n"] for r in by_site}, "categories": list(cats)}


def act(alert_id: str, action: str, by: str, note: str = "", assignee: str = "") -> dict:
    with store.connect() as con:
        r = con.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if r is None or r["category"] not in reader_categories():
            raise KeyError(alert_id)               # an alert of a category the reader does not receive is not there for them
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


# ------------------------------------------------------------------------------------------------ the rules (R8 2.16: per company, with site overrides)

CHANNELS = ("in_app", "email", "sms", "whatsapp")
#: Each category's thresholds as name: (default, lowest, highest). The company sets them in the rule editor and a site may override them; the evaluator
#: reads the site's (`site_rules`). A default of None means "by the data source" (Data's staleness: plan Part 2.4's limits per kind of source).
THRESHOLDS: dict[str, dict[str, tuple]] = {
    "Data": {"stale_minutes": (None, 5, 2880)},
    "Reserve": {"reserve_margin_pct": (2.0, 0, 20), "unserved_kwh": (0.5, 0, 10000)},
    "Tariff": {"agreement_days": (60, 1, 365)},
}
_QUIET = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
QUIET_WORDS = "Quiet hours are two times of day in Karachi time, such as 22:00-07:00"


def quiet_hours(value) -> str | None:
    """Quiet hours as "HH:MM-HH:MM" in Karachi time (across midnight when the second time is the earlier), or None; anything else is refused."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(QUIET_WORDS)
    t = re.sub(r"\s+", "", value).replace("\u2013", "-")
    m = _QUIET.match(t)
    if not m or m.group(1, 2) == m.group(3, 4):
        raise ValueError(QUIET_WORDS)
    return t


def in_quiet_hours(rule: dict, ts: int) -> bool:
    """True when `ts` falls in the rule's quiet hours (Karachi time), for the sender of email, SMS and WhatsApp (not built: HANDOFF 8.C)."""
    q = rule.get("quiet_hours")
    if not q:
        return False
    a, b = (int(x[:2]) * 60 + int(x[3:]) for x in q.split("-"))
    local = datetime.fromtimestamp(int(ts), PKT)
    m = local.hour * 60 + local.minute
    return a <= m < b if a < b else (m >= a or m < b)


class RuleIn(Strict):
    """A category's rule as the editor sends it."""
    channels: list[Literal[CHANNELS]] = Field(default_factory=lambda: ["in_app"], max_length=4)
    quiet_hours: str | int | float | None = None
    escalate_after_min: int | None = Field(None, ge=1, le=10080)
    thresholds: dict[str, float | None] = Field(default_factory=dict)


def _threshold_values(cat: str, given: dict | None, strict: bool) -> dict:
    """The category's thresholds: each known name at the value given (None or absent: its default), checked against its range when `strict`
    (an edit), else a stored value out of range or of an unknown name is read as the default."""
    out = {}
    known = THRESHOLDS.get(cat, {})
    for name in given or {}:
        if name not in known and strict:
            raise ValueError(f"{cat} alerts: there is no threshold named '{name}'")
    for name, (default, lo, hi) in known.items():
        v = (given or {}).get(name)
        if v is None:
            out[name] = default
            continue
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi
        if not ok:
            if strict:
                raise ValueError(f"{cat} alerts: the {name.replace('_', ' ')} threshold must be from {lo:g} to {hi:g}")
            out[name] = default
            continue
        out[name] = int(v) if float(v).is_integer() and isinstance(default, int) else float(v)
    return out


def _rule(cat: str, v: dict | None, strict: bool) -> dict:
    """A category's rule in its stored shape: channels (in the app always first), quiet hours, escalation and thresholds. With `strict` (an edit) a
    problem is a ValueError of one sentence; otherwise a stored value that is no longer valid reads as its default (quiet hours typed as free text
    before they were checked read as none)."""
    v = v if isinstance(v, dict) else {}
    if strict:
        try:
            r = RuleIn.model_validate(v)
        except ValidationError as exc:
            words = message(exc).replace("The escalate after min", "The escalation").replace("The thresholds's ", "The threshold ")
            raise ValueError(f"{cat} alerts: {words}") from None
        try:
            quiet = quiet_hours(r.quiet_hours)
        except ValueError as exc:
            raise ValueError(f"{cat} alerts: {exc}") from None
        channels, escalate = r.channels, r.escalate_after_min
    else:
        try:
            quiet = quiet_hours(v.get("quiet_hours"))
        except ValueError:
            quiet = None
        channels = [c for c in v.get("channels") or [] if c in CHANNELS]
        e = v.get("escalate_after_min")
        escalate = e if isinstance(e, int) and not isinstance(e, bool) and 1 <= e <= 10080 else None
    return {"channels": [c for c in CHANNELS if c == "in_app" or c in channels], "quiet_hours": quiet, "escalate_after_min": escalate,
            "thresholds": _threshold_values(cat, v.get("thresholds"), strict)}


def rules() -> dict:
    """The company's rules, every category in its stored shape."""
    stored = store.get_setting("alert_rules", None) or {}
    return {c: _rule(c, stored.get(c), False) for c in CATEGORIES}


def _check_body(body) -> dict:
    if not isinstance(body, dict):
        raise ValueError("The alert rules are a set of categories, each with its rule")
    for cat in body:
        if cat not in CATEGORIES:
            raise ValueError(f"Unknown alert category: {cat}")
    return body


def set_rules(body: dict, by: str) -> dict:
    """The company's rules replaced by `body` ({category: rule}); a category left out is put back to its defaults."""
    body = _check_body(body)
    out = {c: _rule(c, body.get(c), True) for c in CATEGORIES}
    store.set_setting("alert_rules", out)
    store.audit(by, "changed the alert rules")
    return out


def _site_key(site_id: str) -> str:
    return f"alert_rules:site:{site_id}"


def _merge(base: dict, over: dict | None) -> dict:
    over = over or {}
    return {**base, **{k: v for k, v in over.items() if k != "thresholds"}, "thresholds": {**base["thresholds"], **(over.get("thresholds") or {})}}


def site_rules(site_id: str) -> dict:
    """The rules the evaluator applies at a site: the company's, with the site's overrides on top."""
    company = rules()
    over = store.get_setting(_site_key(site_id), None) or {}
    return {c: _rule(c, _merge(company[c], over.get(c)), False) for c in CATEGORIES}


_FIELDS = ("channels", "quiet_hours", "escalate_after_min")


def _diff(rule: dict, company: dict) -> dict:
    """What a site's rule of one category keeps apart from the company's: the fields, and the thresholds, that differ."""
    diff = {k: rule[k] for k in _FIELDS if rule[k] != company[k]}
    th = {k: v for k, v in rule["thresholds"].items() if v != company["thresholds"].get(k)}
    if th:
        diff["thresholds"] = th
    return diff


def site_overrides(site_id: str) -> dict:
    """What the site keeps apart from the company's rules, per category."""
    company, eff = rules(), site_rules(site_id)
    return {c: d for c in CATEGORIES if (d := _diff(eff[c], company[c]))}


def set_site_rules(site_id: str, body: dict, by: str) -> dict:
    """The site's rules from `body` ({category: rule, whole or in part}): each category's rule is the company's with what `body` gives on top, and only
    what then differs from the company's rules is kept for the site, so the rest follows the company. An empty body puts the site back on them."""
    body = _check_body(body)
    company = rules()
    over = {}
    for c, v in body.items():
        if not isinstance(v, dict):
            raise ValueError(f"{c} alerts: the rule must be a set of named values")
        th = v.get("thresholds") or {}
        if not isinstance(th, dict):
            raise ValueError(f"{c} alerts: the thresholds must be a set of named values")
        merged = {**{k: company[c][k] for k in _FIELDS}, **{k: x for k, x in v.items() if k != "thresholds"},
                  "thresholds": {**company[c]["thresholds"], **th}}
        if d := _diff(_rule(c, merged, True), company[c]):
            over[c] = d
    store.set_setting(_site_key(site_id), over)
    store.audit(by, "changed a site's alert rules", site_id, {"categories": sorted(over)})
    return {"site_id": site_id, "rules": site_rules(site_id), "overrides": site_overrides(site_id)}


def _th(rules_: dict, cat: str, name: str):
    """A threshold of the site's rules (its default when the rules lack it)."""
    return (rules_.get(cat) or {}).get("thresholds", {}).get(name, THRESHOLDS[cat][name][0])


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
