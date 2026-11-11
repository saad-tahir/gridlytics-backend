"""Alerts and genset requests (plan Part 9.3 UI-16 and UI-11): one list of what needs a human, raised and cleared by rules each tick (`evaluate`) and
after the nightly jobs (`nightly`), with each category's thresholds from the company's rules and a site's overrides (`site_rules`).

Categories: Data, Control, Reserve, Battery, Genset, Forecast, Tariff, Savings, Product. An alert is raised once per (site, rule) while its condition
holds and closed automatically when it clears; people acknowledge, assign and close them. A person's close holds while the condition does: the alert is
raised again only once the condition clears and comes back, changes (another day, event or bill) or becomes more severe (`_held_back`). A closed or
cleared alert takes no further action (`AlertClosed`). Advice to start a genset is a request with a reason; the product never starts or stops a genset
itself.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import numpy as np
from pydantic import Field, StrictInt, ValidationError, field_validator

from . import auth
from . import battery as battery_service
from . import plans, sites, store, store_members, tariffs, telemetry
from .schemas import Refusal, Strict, message

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


#: The SQL that adds one event (`_event`) at the end of an alert's history (audit pages-D-28)
_HISTORY = "history = json_insert(history, '$[#]', json(?))"


def _event(action: str, by: str = "", note: str = "", **more) -> str:
    """One event of an alert's history (audit pages-D-28), as JSON for `_HISTORY`: what was done (acknowledged, assigned, closed, answered, or resolved
    when the product closed it), who did it ("" for the product itself), when (UTC) and the note, so an acknowledgement keeps its person and its note
    and a later close or clear overwrites neither."""
    return json.dumps({"at": store._now(), "action": action, "by": by, "note": note, **more})


def _row(r, site_name: str) -> dict:
    """An alert as the API answers it: its detail and history read, with its site's name."""
    return {**dict(r), "detail": json.loads(r["detail"] or "{}"), "history": json.loads(r["history"] or "[]"), "site_name": site_name}


SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
#: The detail keys that date a condition: the day, month, event, gap, bill period, agreement end or start it is about. A rule that holds on while one of
#: these moves (a negative saving on the 3rd, then on the 4th; a second edge event; the next bill) holds a new condition, which a person's close of the
#: earlier one does not hold back (audit flow-reports-alerts-members-keys-09). The other keys are figures that move tick by tick (minutes, kWh).
CONDITION_KEYS = ("day", "month", "at", "gap_from", "gap_to", "ended_at", "agreement_end", "ended", "period_start", "period_end", "due_on", "ended_on",
                  "billing_month", "price_from", "requested_start", "start", "last_ts", "last_seen", "from_stage", "to_stage")


def _held_back(row, severity: str, detail: dict) -> bool:
    """True when the (site, rule)'s latest alert was closed by a person while its condition held, and the condition the rule sees now is that same one,
    no more severe (audit flow-reports-alerts-members-keys-09: the close was undone at the next tick by a new alert under a new id, without its note).
    The close holds until the condition clears (`_clear` then says so in its history), changes (`CONDITION_KEYS`) or becomes more severe. A close
    made before a close held the condition back carries no `held` mark, so it holds nothing back."""
    if row is None or row["state"] != "closed":
        return False
    events = json.loads(row["history"] or "[]")
    if not events or events[-1].get("action") != "closed" or not events[-1].get("held"):
        return False
    if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(row["severity"], 0):
        return False
    was = json.loads(row["detail"] or "{}")
    return all(was.get(k) == detail.get(k) for k in CONDITION_KEYS if k in was or k in detail)


def _open(site_id: str, rule: str, category: str, severity: str, title: str, detail: dict | None = None) -> None:
    detail = detail or {}
    with store.connect() as con:
        r = con.execute("SELECT id FROM alerts WHERE site_id = ? AND rule = ? AND state IN ('open','acknowledged')", (site_id, rule)).fetchone()
        if r:
            con.execute("UPDATE alerts SET title = ?, detail = ?, severity = ? WHERE id = ?", (title, json.dumps(detail), severity, r["id"]))
            return
        last = con.execute("SELECT state, severity, detail, history FROM alerts WHERE site_id = ? AND rule = ? ORDER BY opened_at DESC, rowid DESC "
                           "LIMIT 1", (site_id, rule)).fetchone()
        if _held_back(last, severity, detail):
            return
        con.execute("INSERT INTO alerts (id, site_id, rule, category, severity, title, detail, opened_at) VALUES (?,?,?,?,?,?,?,?)",
                    (store.new_id(), site_id, rule, category, severity, title, json.dumps(detail), store._now()))


#: An alert a person closed while its condition held, whose condition has not cleared since (`_held_back`): its last event is that close
_HELD = "state = 'closed' AND json_extract(history, '$[#-1].action') = 'closed' AND json_extract(history, '$[#-1].held') = 1"


def _clear(site_id: str, rule: str) -> None:
    with store.connect() as con:
        con.execute(f"UPDATE alerts SET state = 'resolved', closed_at = ?, note = 'Condition cleared', {_HISTORY} WHERE site_id = ? AND rule = ? "
                    "AND state IN ('open','acknowledged')", (store._now(), _event("resolved", note="Condition cleared"), site_id, rule))
        # a person's close held the condition back while it held: it has cleared, so its next start raises a new alert (audit
        # flow-reports-alerts-members-keys-09), and the closed alert's history says when it cleared
        con.execute(f"UPDATE alerts SET {_HISTORY} WHERE site_id = ? AND rule = ? AND {_HELD}",
                    (_event("condition_cleared", note="Condition cleared"), site_id, rule))


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
#: otherwise raise them on day one (an advisory site's unconfirmed apply before anyone was told to apply a table). The Product fee check reads the
#: verified saving, so it waits too.
SETTLED_CATEGORIES = ("Control", "Savings", "Product")
SETTLED_DAYS = 7


def settled(site_id: str, plan: dict | None, now: int) -> bool:
    """True once Control and Savings alerts may be raised for the site: it has an active plan and its data reaches back SETTLED_DAYS days."""
    if not plan:
        return False
    first = telemetry.first_ts(site_id)
    return first is not None and now - int(first) >= SETTLED_DAYS * 86400


def _when(ts: int) -> str:
    """A time in Karachi for an alert's title: "8 Nov 14:30"."""
    t = datetime.fromtimestamp(int(ts), PKT)
    return f"{t.day} {t:%b %H:%M}"


def _gap(site_id: str, now: int, gap_hours: float) -> None:
    """Data: the longest gap between two readings in the last day, once data came back after it, above the rules' hours (R8 2.16, "gap above 1
    hour"); a gap still going on is the staleness rule's."""
    t0 = now - 86400
    with telemetry.connect() as con:
        before = con.execute("SELECT MAX(ts) FROM minute WHERE site_id = ? AND ts < ?", (site_id, t0)).fetchone()[0]
        ts = [r[0] for r in con.execute("SELECT ts FROM minute WHERE site_id = ? AND ts >= ? AND ts <= ? ORDER BY ts", (site_id, t0, now))]
    if before is not None:
        ts.insert(0, before)
    best = None
    for a, b in zip(ts, ts[1:]):
        hours = (b - a - 60) / 3600                      # a reading covers its minute: the gap is the time no minute was read
        if hours > gap_hours and (best is None or hours > best[2]):
            best = (a + 60, b, hours)
    _set(best is not None, site_id, "data_gap", "Data", "warning",
         f"The data stopped for {best[2]:.1f} hours, from {_when(best[0])} to {_when(best[1])}" if best else "",
         {"gap_from": best[0], "gap_to": best[1], "hours": round(best[2], 1)} if best else {})


#: The edge's events (ems-edge/1.0) that need a person, with the category, severity and words of their alert; each holds for a day after the event,
#: and a later `fallback_cleared` ends a `fallback_engaged` (R8 2.16: read-back mismatch, write budget exhausted, local override detected, fallback
#: entered; clock drift)
EDGE_EVENTS = {
    "readback_mismatch": ("Control", "warning", "The edge read back inverter settings that differ from the plan's: control is paused until a person resumes it"),
    "local_override": ("Control", "warning", "Someone changed the inverter's settings on site: control is paused until a person resumes it"),
    "budget_exhausted": ("Control", "warning", "The edge used up its writes to the inverter for the day"),
    "write_failed": ("Control", "warning", "The edge could not write the plan to the inverter"),
    "plan_rejected": ("Control", "warning", "The edge refused a plan"),
    "fallback_engaged": ("Control", "critical", "The edge fell back to the inverter's own settings"),
    "clock_unsynced": ("Data", "warning", "The edge's clock is not set: its readings may carry the wrong times"),
}


def _event_ts(v) -> int | None:
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _edge_events(site_id: str, now: int, ready: bool) -> None:
    kinds = tuple(EDGE_EVENTS) + ("fallback_cleared",)
    with store.connect() as con:
        rows = con.execute(f"SELECT type, ts, detail FROM events WHERE site_id = ? AND source = 'edge' AND type IN ({','.join('?' * len(kinds))}) "
                           "ORDER BY id DESC LIMIT 500", (site_id, *kinds)).fetchall()
    latest: dict[str, int] = {}
    for r in rows:
        t = _event_ts(r["ts"])
        if t is not None and now - 86400 <= t <= now + 300 and t > latest.get(r["type"], -1):
            latest[r["type"]] = t
    if latest.get("fallback_cleared", -1) >= latest.get("fallback_engaged", 0):
        latest.pop("fallback_engaged", None)
    for kind, (cat, sev, words) in EDGE_EVENTS.items():
        on = kind in latest and (cat != "Control" or ready)
        _set(on, site_id, f"edge_{kind}", cat, sev, f"{words} ({_when(latest[kind])})" if on else "", {"at": latest.get(kind)})


def _plan_expired(site_id: str, plan: dict | None, now: int, ready: bool) -> None:
    """Control: the plan in force ran past its end and nothing replaced it (R8 2.16, "plan expired without replacement")."""
    until = _event_ts((plan or {}).get("valid_until"))
    _set(ready and until is not None and until <= now, site_id, "plan_expired", "Control", "critical",
         f"The plan in force ended at {_when(until)} and no new plan replaced it" if until else "", {"ended_at": until})


def _edge_offline(site: dict, now: int, ready: bool, minutes: float) -> None:
    """Control: a closed-loop site whose edge has not been heard from for the rules' minutes (critical after four times as long)."""
    seen = (site.get("state") or {}).get("edge_last_seen")
    closed = site.get("tier") == "closed_loop" and not site.get("demo_link")
    age = (now - int(seen)) / 60 if closed and isinstance(seen, (int, float)) else None
    on = ready and age is not None and age > minutes
    _set(on, site["id"], "edge_offline", "Control", "critical" if on and age > 4 * minutes else "warning",
         f"The edge has not been heard from for {int(age)} minutes" if on else "", {"last_seen": seen})


def _bill_residual(site_id: str, pct: float) -> None:
    """Tariff: the latest bill entered, rebuilt from telemetry, differs from the printed total by more than the rules' percent (R8 2.16)."""
    with store.connect() as con:
        r = con.execute("SELECT id, period_start, period_end, difference_pct FROM bills WHERE site_id = ? ORDER BY period_end DESC, created DESC LIMIT 1",
                        (site_id,)).fetchone()
    diff = r["difference_pct"] if r else None
    on = isinstance(diff, (int, float)) and abs(diff) > pct
    title = ""
    if on:
        a, b = date.fromisoformat(r["period_start"][:10]), date.fromisoformat(r["period_end"][:10])
        title = f"The bill for {a.day} {a:%b} to {b.day} {b:%b %Y}, rebuilt from the data, differs from the printed bill by {abs(diff):.1f} %"
    _set(on, site_id, "bill_residual", "Tariff", "warning", title,
         {"period_start": r["period_start"], "period_end": r["period_end"], "difference_pct": diff} if on else {})


def _forecast_stages(site_id: str, now: int) -> None:
    """Forecast: a forecast's stage changed in the last day (R8 2.16, "stage change"): the latest issue against the latest a day before."""
    for kind, words in (("pv", "PV"), ("load", "load")):
        cur = telemetry.latest_forecast(site_id, kind, now)
        before = telemetry.latest_forecast(site_id, kind, now - 86400)
        changed = bool(cur and before and cur.get("stage") != before.get("stage"))
        _set(changed, site_id, f"forecast_stage_{kind}", "Forecast", "info",
             f"The {words} forecast moved from stage {before['stage']} to stage {cur['stage']} in the day to {_when(now)}" if changed else "",
             {"from_stage": before["stage"], "to_stage": cur["stage"]} if changed else {})


#: The tariff library's kinds in words, for the staleness rule's titles
KIND_WORDS = {"schedule": "tariff schedule", "tou": "time-of-use calendar", "tax": "tax rules", "prosumer": "net billing or net metering rules",
              "icp": "incremental consumption package", "adder": "adjustment"}
FUEL_WORDS = {"hsd": "diesel (HSD)", "gas_captive": "captive gas", "lng_captive": "captive LNG"}
#: From this day of the month an FCA or QTA for the month's bills is looked for: NEPRA notifies them during the month they are billed in
ADDER_FROM_DAY = 15


def _long(d: date) -> str:
    return f"{d.day} {d:%B %Y}"


#: A connection's regime in words (the Site settings' words), for the agreement alert's title (audit role-viewer-17)
REGIME_WORDS = {"net_billing": "net billing", "net_metering_legacy": "legacy net metering", "none": "no export", "flat": "flat rate",
                "off_grid": "off-grid"}


def _regime(regime) -> str:
    return REGIME_WORDS.get(str(regime or ""), str(regime or "").replace("_", " "))


def _long_day(day) -> str:
    """"23 December 2026" for "2026-12-23"; the text itself when it is not a day."""
    try:
        return _long(date.fromisoformat(str(day)[:10]))
    except ValueError:
        return str(day)


def _connection_words(data: dict) -> str:
    """A grid connection as a person knows it: its meter reference, else its tariff and regime (never its internal id)."""
    return str(data.get("meter_ref") or "").strip() or f"{data.get('tariff_category') or 'Grid connection'}, {_regime(data.get('regime'))}"


def _connection_label(c: dict, i: int, conns: list[dict]) -> str:
    """Which of a site's connections, for a title that must tell two apart: its meter reference, else its place and tariff ("connection 2 (B-3)")."""
    ref = str(c["data"].get("meter_ref") or "").strip()
    return f"meter {ref}" if ref else f"connection {i + 1} ({c['data'].get('tariff_category') or 'no tariff'})"


def _agreements(site: dict, now: int, rules_: dict) -> None:
    """Tariff: a connection's agreement ending within the rules' days (60 by default), a warning that says the day it ends; once that day has passed
    (audit pages-D-16) a critical alert that says it ended, what the tariffs engine does with the site's export from the day after, and what to do. The
    agreement's state and the regime billed are the engine's (tariffs.agreement_state, tariffs.regime_on), as on the Tariffs page: after a net-billing
    agreement the plan, the bill and the savings price the export at zero; after a legacy net-metering one the site is billed under net billing. Only
    net billing and legacy net metering have an agreement the engine reads, so under another regime the dates change nothing and raise nothing. With
    more than one connection the title names which (flow-reports-alerts-members-keys-25)."""
    conns = site.get("connections") or []
    today = datetime.fromtimestamp(now, PKT).date()
    for i, c in enumerate(conns):
        end = c["data"].get("agreement_end")
        left = None
        if end and tariffs.agreement_state(c, today) is not None:
            try:
                left = (date.fromisoformat(end) - today).days
            except ValueError:
                left = None
        ended = left is not None and left < 0
        which = f" of {_connection_label(c, i, conns)}" if len(conns) > 1 else ""
        regime = _regime(c["data"].get("regime"))
        if ended:
            since = _long(date.fromisoformat(end) + timedelta(days=1))
            then = ("the site is billed under net billing, nothing is netted and its export earns the net-billing rate"
                    if tariffs.regime_on(c, today) == "net_billing" else "the plan, the bill and the savings price its export at zero")
            title = (f"The {regime} agreement{which} ended on {_long_day(end)}: since {since} {then}. Enter the agreement that follows it in the "
                     "site's settings")
        else:
            title = f"The {regime} agreement{which} ends on {_long_day(end)}"
        _set(left is not None and left <= _th(rules_, "Tariff", "agreement_days"), site["id"], f"regime_expiring_{c['id']}", "Tariff",
             "critical" if ended else "warning", title, {"connection": _connection_words(c["data"]), "agreement_end": end, "ended": ended})


def tariff_staleness(site: dict, now: int, rules_: dict) -> None:
    """Tariff (brief-vs-build-18): the stored tariff library has gone stale, read from its versions and fuel prices (nothing is fetched from
    NEPRA, OGRA or a DISCO: a new notification is still drafted and approved by a person, plan Part 6.2):
    - a placeholder awaiting NEPRA's notification whose date has passed (the CY2027 schedule from 1 January 2027): bills and plans still use the
      rates before it (tariff_awaiting);
    - a version in force that ended with no version after it, for the kinds the site's connection resolves (tariff_lapsed);
    - from the 15th of a month, no FCA or QTA approved for that month's bills, unless one is drafted and waiting for approval, which the counts'
      `tariff_pending` already says (adder_missing_fca, adder_missing_qta);
    - at a genset site, the latest price of its fuel older than the rules' days: diesel's (OGRA notifies it at least every fortnight) or captive
      gas and LNG's (revised about twice a year) (fuel_price_old)."""
    sid = site["id"]
    today = datetime.fromtimestamp(now, PKT).date()
    day = today.isoformat()
    tariffs.ensure_shipped()
    grid = [c for c in site.get("connections") or [] if (c.get("data") or {}).get("regime") != "off_grid"]
    with store.connect() as con:
        vers = [dict(r) for r in con.execute("SELECT kind, key, effective_from, effective_to, status, body FROM tariff_versions")]
        fuel = {r["kind"]: r["valid_from"] for r in con.execute("SELECT kind, MAX(valid_from) AS valid_from FROM fuel_prices GROUP BY kind")}
    for v in vers:
        try:
            v["body"] = json.loads(v["body"] or "{}")
        except ValueError:
            v["body"] = {}
    # a notification that was due
    due = sorted((v for v in vers if v["status"] == "awaiting_notification" and v["effective_from"] <= day), key=lambda v: v["effective_from"])
    on = bool(grid and due)
    title = ""
    if on:
        first = due[0]
        name = str(first["body"].get("name") or KIND_WORDS.get(first["kind"], first["kind"])).split(":")[0]
        name = name[:1].lower() + name[1:]
        title = (f"NEPRA's notification for the {name} was due on {_long(date.fromisoformat(first['effective_from']))} and is not in the library: "
                 "bills and plans still use the rates before it" + (f" ({len(due) - 1} more waiting)" if len(due) > 1 else ""))
    _set(on, sid, "tariff_awaiting", "Tariff", "warning", title, {"due_on": due[0]["effective_from"], "waiting": len(due)} if on else {})
    # a version in force that ended with nothing after it
    keys = set()
    for c in grid:
        d = c.get("data") or {}
        keys |= {("schedule", "national"), ("tax", "national"), ("tou", "ke" if d.get("disco_group") == "ke" else "xwdisco")}
        if d.get("regime") in ("net_billing", "net_metering_legacy"):
            keys.add(("prosumer", d["regime"]))
    lapsed = []
    for kind, key in sorted(keys):
        started = [v for v in vers if v["kind"] == kind and v["key"] == key and v["status"] == "active" and v["effective_from"] <= day]
        latest = max(started, key=lambda v: v["effective_from"], default=None)
        if latest and latest["effective_to"] and latest["effective_to"] < day:
            lapsed.append((kind, key, latest["effective_to"]))
    title = ""
    if lapsed:
        kind, _key, end = lapsed[0]
        title = (f"The {KIND_WORDS.get(kind, kind)} in force ended on {_long(date.fromisoformat(end[:10]))} and no version follows it"
                 + (f" ({len(lapsed) - 1} more)" if len(lapsed) > 1 else ""))
    _set(bool(lapsed), sid, "tariff_lapsed", "Tariff", "warning", title, {"ended_on": lapsed[0][2][:10], "waiting": len(lapsed)} if lapsed else {})
    # the month's FCA and QTA
    month = tariffs.billing_month_for(grid[0], today) if grid else None
    for key, words in (("fca", "FCA"), ("qta", "QTA")):
        missing = False
        if month and today.day >= ADDER_FROM_DAY:
            def covers(v):
                bm = v["body"].get("billing_months")
                return bm == "all" or (isinstance(bm, list) and month in bm)
            held = [v for v in vers if v["kind"] == "adder" and v["key"] == key and covers(v)]
            missing = not any(v["status"] in ("active", "pending") for v in held)
        when = f"{date.fromisoformat(month + '-01'):%B %Y}" if month else ""
        _set(missing, sid, f"adder_missing_{key}", "Tariff", "info",
             f"No {words} is in the library for the bills of {when}: they are rebuilt and planned without it" if missing else "",
             {"billing_month": month} if missing else {})
    # the fuel price
    g = (site.get("assets") or {}).get("genset")
    kind = (g or {}).get("fuel_kind", "hsd")
    last = fuel.get(kind) if g else None
    age = (today - date.fromisoformat(last[:10])).days if last else None
    old = age is not None and age > _th(rules_, "Tariff", "diesel_price_days" if kind == "hsd" else "gas_price_days")
    _set(old, sid, "fuel_price_old", "Tariff", "info",
         f"The latest {FUEL_WORDS.get(kind, kind)} price in the library is from {_long(date.fromisoformat(last[:10]))}, {age} days ago" if old else "",
         {"price_from": last[:10], "days": age} if old else {})


GENSET_RULES = ("genset_unrequested", "genset_low_load", "genset_request_missed")


def _genset_condition(site: dict, now: int, rules_: dict) -> None:
    """Genset (R8 2.16): a run the plan in force did not ask for while the grid was up (or at an off-grid site), a run below the set's minimum load,
    and a start request nobody answered and the set did not follow within the rules' minutes. A start on grid loss is the set's own (an AMF panel)."""
    sid = site["id"]
    g_asset = (site.get("assets") or {}).get("genset")
    if not g_asset:
        for rule in GENSET_RULES:
            _clear(sid, rule)
        return
    rated = float(g_asset.get("rated_kw") or 0.0)
    min_kw = float(g_asset.get("f_min", 0.3)) * rated
    window = float(_th(rules_, "Genset", "request_window_minutes"))
    d = telemetry.raw(sid, now - 3600, now + 1, ("grid_present", "genset_running", "genset_power_w"))
    run, pw = d["genset_running"], d["genset_power_w"]
    on = np.where(np.isnan(run), np.nan_to_num(pw) > 0.05 * rated * 1000, run >= 0.5)
    off_grid = all((c.get("data") or {}).get("regime") == "off_grid" for c in site.get("connections") or []) or bool(site["data"].get("off_grid"))
    grid_up = np.ones(len(on), dtype=bool) if off_grid else (np.nan_to_num(d["grid_present"], nan=0.0) >= 0.5)
    # unrequested: the step of the plan in force at each such minute did not run the set
    steps_on: dict[int, bool] = {}
    unasked = 0
    for t in d["ts"][on & grid_up]:
        q = int(t) // STEP_S * STEP_S
        if q not in steps_on:
            p = plans.active_at(sid, q)
            st = next((x for x in (p or {}).get("steps", []) if x["t"] <= q < x["t"] + STEP_S), None)
            steps_on[q] = p is None or st is None or bool(st.get("genset_on"))   # no plan in force at it: nothing to hold the run against
        unasked += int(not steps_on[q])
    on_unasked = unasked >= _th(rules_, "Genset", "unrequested_minutes")
    _set(on_unasked, sid, "genset_unrequested", "Genset", "warning",
         f"The genset ran for {unasked} minutes in the hour to {_when(now)} that the plan did not ask for" if on_unasked else "", {"minutes": unasked})
    low = int(np.sum(on & ~np.isnan(pw) & (pw < min_kw * 1000)))
    on_low = rated > 0 and low >= _th(rules_, "Genset", "low_load_minutes")
    _set(on_low, sid, "genset_low_load", "Genset", "warning",
         f"The genset ran below its minimum load of {min_kw:.0f} kW for {low} minutes in the hour to {_when(now)}" if on_low else "",
         {"minutes": low, "min_kw": round(min_kw, 1)})
    # a request nobody answered and the set did not follow: pending (not answered, not withdrawn), its start past by the window, in the last day
    with store.connect() as con:
        pend = [json.loads(r["payload"]) for r in con.execute(
            "SELECT payload FROM notifications WHERE site_id = ? AND kind = 'genset_request' AND status = 'pending' ORDER BY ts DESC LIMIT 50", (sid,))]
    missed = None
    for pl in pend:
        start = pl.get("start")
        if not isinstance(start, (int, float)) or not (now - 86400 <= start <= now - window * 60):
            continue
        r = telemetry.raw(sid, int(start) - STEP_S, int(start + window * 60) + 1, ("genset_running", "genset_power_w"))
        ran = np.any(np.where(np.isnan(r["genset_running"]), np.nan_to_num(r["genset_power_w"]) > 0.05 * rated * 1000, r["genset_running"] >= 0.5))
        if not ran and (missed is None or start > missed["start"]):
            missed = pl
    _set(missed is not None, sid, "genset_request_missed", "Genset", "warning",
         f"Nobody answered the request to start the genset at {_when(missed['start'])}, and it did not start within {window:g} minutes" if missed else "",
         {"requested_start": missed["start"], "kw": missed.get("kw")} if missed else {})


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
    _gap(site_id, now, float(_th(rules_, "Data", "gap_hours")))
    _edge_events(site_id, now, ready)
    _plan_expired(site_id, plan, now, ready)
    _edge_offline(site, now, ready, float(_th(rules_, "Control", "edge_offline_minutes")))
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
    _set(unserved > float(_th(rules_, "Reserve", "unserved_kwh")), site_id, "unserved", "Reserve", "critical",
         f"{unserved:.1f} kWh of critical load went unserved in the day to {_when(now)}")
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
        # the demo mill's story (plan Part 9.6): its installer marks a day's table the next morning, so it stays raised until today's is marked
        with store.connect() as con:
            unconfirmed = con.execute("SELECT 1 FROM applied WHERE site_id = ? AND day_local = ?", (site_id, today)).fetchone() is None
    # the title names the day (audit role-operator-31: "Today's slot table" was still read the next day)
    day = datetime.fromtimestamp(now, PKT).date()
    _set(ready and unconfirmed, site_id, "apply_unconfirmed", "Control", "warning",
         f"The slot table for {day:%A} {_long(day)} is not marked as applied: tier 2 has no read-back, so the saving is not claimed until it is",
         {"day": today})
    _agreements(site, now, rules_)
    _bill_residual(site_id, float(_th(rules_, "Tariff", "bill_difference_pct")))
    tariff_staleness(site, now, rules_)
    _forecast_stages(site_id, now)
    _genset_condition(site, now, rules_)
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
            con.execute(f"UPDATE alerts SET state = 'resolved', closed_at = ?, note = ?, {_HISTORY} WHERE id = ?",
                        (store._now(), note, _event("resolved", note=note), r["id"]))
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
                    con.execute(f"UPDATE alerts SET state = 'resolved', closed_at = ?, note = ?, {_HISTORY} WHERE site_id = ? AND rule = ? "
                                "AND state IN ('open','acknowledged')", (store._now(), note, _event("resolved", note=note), sid, f"genset_request_{old}"))
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


#: A genset request's answers in words (the genset page's buttons)
OUTCOME_WORDS = {"done": "Done", "not_now": "Not now"}


class RequestClosed(Refusal):
    """An answer to a genset request that is no longer pending (audit pages-C-11): it was answered already, and an answer is not changed, or it was
    withdrawn (the run was asked for again at another start, or no plan runs it), so it is not scored. 409 through the API's refusal handler; not a
    ValueError, so the route's 422 for a bad answer does not swallow it."""

    def __init__(self, row) -> None:
        if row["status"] == "withdrawn":
            super().__init__(409, "This request was withdrawn (the plan moved the run or no longer runs the genset), so it is not answered", "request_withdrawn")
        else:
            said = OUTCOME_WORDS.get(row["outcome"], "given")
            super().__init__(409, f"This request is already answered ({said}), and an answer is not changed", "request_answered")


def answer_request(site_id: str, nid: str, outcome: str, by: str) -> dict:
    """Answers a pending genset request, once: a request answered already, or withdrawn, is refused (`RequestClosed`), so the first answer stands and a
    withdrawn request never counts as asked (`genset_requests`) or followed."""
    if outcome not in ("done", "not_now"):
        raise ValueError("The answer is done or not_now")
    with store.connect() as con:
        r = con.execute("SELECT * FROM notifications WHERE id = ? AND site_id = ? AND kind = 'genset_request'", (nid, site_id)).fetchone()
        if r is None:
            raise KeyError(nid)
        if r["status"] != "pending" or con.execute("UPDATE notifications SET status = 'answered', outcome = ?, scored_at = ? WHERE id = ? AND status = 'pending'",
                                                   (outcome, store._now(), nid)).rowcount != 1:
            raise RequestClosed(con.execute("SELECT status, outcome FROM notifications WHERE id = ?", (nid,)).fetchone())
        payload = json.loads(r["payload"])
        said = OUTCOME_WORDS[outcome]
        con.execute(f"UPDATE alerts SET state = 'closed', closed_at = ?, note = ?, {_HISTORY} WHERE site_id = ? AND rule = ? AND state != 'closed'",
                    (store._now(), f"Answered: {said}, by {by}", _event("answered", by, said), site_id, f"genset_request_{payload['start']}"))
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
    return [_row(r, names.get(r["site_id"], "")) for r in rows]


def counts() -> dict:
    """The open and acknowledged alerts by severity and by site, of the categories the reader receives (named in `categories`)."""
    cats = reader_categories()
    cond, args = _in(cats)
    with store.connect() as con:
        rows = con.execute("SELECT severity, COUNT(*) n FROM alerts WHERE state IN ('open','acknowledged')" + cond + " GROUP BY severity", args).fetchall()
        by_site = con.execute("SELECT site_id, COUNT(*) n FROM alerts WHERE state IN ('open','acknowledged')" + cond + " GROUP BY site_id", args).fetchall()
    out = {r["severity"]: r["n"] for r in rows}
    return {"total": sum(out.values()), "by_severity": out, "by_site": {r["site_id"]: r["n"] for r in by_site}, "categories": list(cats)}


ASSIGNEE_WORDS = "Assign the alert to a member who acts on alerts (an administrator or an operator), picked from the list"


def assignees() -> list[dict]:
    """The members an alert may be assigned to (audit role-operator-31): the company's members, not switched off, whose role acts on alerts
    (`ack_alerts`: an administrator or an operator), as {id, name, role}, by name. A person appears once they have signed in (their member row is
    made at their first sight)."""
    if auth.company.get() is None:
        return []
    roles = auth.PERMISSIONS["ack_alerts"]
    people = [{"id": m["user_id"], "name": m["name"] or m["email"], "role": m["role"]} for m in store_members.members()
              if not m["disabled"] and m["role"] in roles]
    return sorted(people, key=lambda m: m["name"].lower())


def _assignee(member_id: str, name: str) -> tuple[str, str]:
    """The (id, name) of who an alert is assigned to: the member by id, else the one member whose name or address is `name` (an older page sends the
    name); ("", "") when neither is given. Anyone else, a member who does not act on alerts or one switched off, is refused (ValueError)."""
    member_id, name = (member_id or "").strip(), (name or "").strip()
    if not member_id and not name:
        return "", ""
    people = assignees()
    if member_id:
        hit = [m for m in people if m["id"] == member_id]
    else:
        mails = {m["user_id"]: (m["email"] or "").lower() for m in store_members.members()}
        hit = [m for m in people if name.lower() in (m["name"].lower(), mails.get(m["id"], ""))]
    if len(hit) > 1:
        raise ValueError("Two or more members are called that: pick one from the list")
    if not hit:
        raise ValueError(ASSIGNEE_WORDS)
    return hit[0]["id"], hit[0]["name"]


#: What each action does, for a refusal's sentence
ACTION_DONE = {"acknowledge": "acknowledged", "assign": "assigned", "close": "closed again"}


class AlertClosed(Refusal):
    """An action on an alert that is no longer open or acknowledged (audit flow-reports-alerts-members-keys-08): a person closed it, or its condition
    cleared by itself. Acknowledging it set it back to 'acknowledged' with its close kept, beside the alert its condition raised again. 409 through
    the API's refusal handler; not a ValueError, so the route's 422 does not swallow it."""

    def __init__(self, row, action: str) -> None:
        done = ACTION_DONE.get(action, "changed")
        when = ""
        try:
            t = datetime.fromisoformat(str(row["closed_at"])).astimezone(PKT)
            when = f" on {_long(t.date())} at {t:%H:%M} (Karachi time)"
        except (TypeError, ValueError):
            pass
        said = "cleared by itself" if row["state"] == "resolved" else "was closed"
        super().__init__(409, f"This alert {said}{when}, so it is not {done}", "alert_closed")


#: An alert people still act on
_ACTIVE = "state IN ('open','acknowledged')"


def act(alert_id: str, action: str, by: str, note: str = "", assignee: str = "", member_id: str = "") -> dict:
    with store.connect() as con:
        r = con.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if r is None or r["category"] not in reader_categories():
            raise KeyError(alert_id)               # an alert of a category the reader does not receive is not there for them
        if action not in ACTION_DONE:
            raise ValueError("The action is acknowledge, assign or close")
        if r["state"] not in ("open", "acknowledged"):
            raise AlertClosed(r, action)           # closed or cleared: nothing more is done to it (audit flow-reports-alerts-members-keys-08)
        # each action is an event of the alert's history, with who and when (audit pages-D-28); `note` is the latest note written, which an action
        # without one leaves as it was. Each update holds only while the alert is still open or acknowledged, so a close by someone else in between
        # is refused rather than undone.
        if action == "acknowledge":
            cur = con.execute(f"UPDATE alerts SET state = 'acknowledged', note = CASE WHEN ? = '' THEN note ELSE ? END, {_HISTORY} "
                              f"WHERE id = ? AND {_ACTIVE}", (note, note, _event("acknowledged", by, note), alert_id))
        elif action == "assign":
            # a member who acts on alerts, kept by id with their name (audit role-operator-31: any text was stored); empty takes the alert back
            who_id, who = _assignee(member_id, assignee)
            cur = con.execute(f"UPDATE alerts SET assigned = ?, assigned_id = ?, {_HISTORY} WHERE id = ? AND {_ACTIVE}",
                              (who, who_id, _event("assigned", by, assignee=who), alert_id))
        else:
            # a person's close holds while the condition does (`held`: audit flow-reports-alerts-members-keys-09, `_held_back`)
            cur = con.execute(f"UPDATE alerts SET state = 'closed', closed_at = ?, note = CASE WHEN ? = '' THEN note ELSE ? END, {_HISTORY} "
                              f"WHERE id = ? AND {_ACTIVE}", (store._now(), note, note, _event("closed", by, note, held=True), alert_id))
        if cur.rowcount != 1:
            raise AlertClosed(con.execute("SELECT state, closed_at FROM alerts WHERE id = ?", (alert_id,)).fetchone(), action)
    store.audit(by, f"{action}d an alert" if action != "assign" else "assigned an alert", r["site_id"], {"alert": alert_id})
    if action == "close":
        return {"id": alert_id, "state": "closed"}
    # the alert itself, read by its id (the inbox listing is capped, so an alert outside its first rows would not be found there)
    with store.connect() as con:
        r2 = con.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    return _row(r2, (sites.get(r2["site_id"]) or {}).get("name", ""))


# ------------------------------------------------------------------------------------------------ the rules (R8 2.16: per company, with site overrides)

CHANNELS = ("in_app", "email", "sms", "whatsapp")
#: Each category's thresholds as name: (default, lowest, highest). The company sets them in the rule editor and a site may override them; the evaluator
#: reads the site's (`site_rules`). A default of None means "by the data source" (Data's staleness: plan Part 2.4's limits per kind of source).
THRESHOLDS: dict[str, dict[str, tuple]] = {
    "Data": {"stale_minutes": (None, 5, 2880), "gap_hours": (1.0, 0.25, 24)},
    "Control": {"edge_offline_minutes": (15, 5, 1440)},
    "Reserve": {"reserve_margin_pct": (2.0, 0, 20), "unserved_kwh": (0.5, 0, 10000)},
    "Genset": {"unrequested_minutes": (10, 1, 120), "low_load_minutes": (15, 5, 240), "request_window_minutes": (30, 5, 240)},
    "Tariff": {"agreement_days": (60, 1, 365), "bill_difference_pct": (2.0, 0.5, 50), "diesel_price_days": (14, 1, 365),
               "gas_price_days": (183, 1, 730)},
    "Forecast": {"bust_pct": (30, 5, 100), "bust_hours": (2.0, 0.5, 12)},
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
    """A category's rule as the editor sends it (the request model of PUT /api/alerts/rules and of a site's rules: audit
    flow-reports-alerts-members-keys-10). Every field is checked together, so one sentence names each bad value: the channels a list of known ones,
    quiet hours two times of day, escalation a whole number of minutes from 1 to 10,080 (a week) and thresholds numbers by name."""
    channels: list[Literal[CHANNELS]] = Field(default_factory=lambda: ["in_app"], max_length=4)
    quiet_hours: str | None = None
    escalate_after_min: StrictInt | None = Field(None, ge=1, le=10080)
    thresholds: dict[str, float | None] = Field(default_factory=dict)

    @field_validator("quiet_hours", mode="before")
    @classmethod
    def _quiet(cls, v):
        return quiet_hours(v)


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
        channels, quiet, escalate = r.channels, r.quiet_hours, r.escalate_after_min
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
    """The body of a rules edit: known categories, each a rule (a set of named values). A category given as null is refused in words rather than read
    as unknown (audit flow-reports-alerts-members-keys-10: {"Data": null} read "Unknown alert category: Data"); left out, it takes its defaults."""
    if not isinstance(body, dict):
        raise ValueError("The alert rules are a set of categories, each with its rule")
    for cat, v in body.items():
        if cat not in CATEGORIES:
            raise ValueError(f"Unknown alert category: {str(cat)[:40]}")
        if not isinstance(v, dict):
            raise ValueError(f"{cat} alerts: the rule must be a set of named values")
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


def forecast_bust(site: dict, day: date, bust_pct: float = 30.0) -> dict | None:
    """How long the day-ahead PV forecast of `day` (the issue the nightly scores: the last at or before local midnight, at most 6 hours before)
    busted: plan Part 2.4 calls a PV error above 30 percent of the clear-sky ceiling over 30 minutes a forecast bust (engine-forecast-10; the
    daily nRMSE by nameplate the rule read before reaches 0.6 only when a plant makes nothing all day). Per 30-minute window of daylight (a
    ceiling above a tenth of the day's highest), the measured mean against the forecast's median, as a share of the window's ceiling. None when
    the day cannot be scored (no PV, no such issue, or under half the day's data)."""
    pv = (site.get("assets") or {}).get("pv")
    if not pv:
        return None
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    f = telemetry.latest_forecast(site["id"], "pv", t0)
    if f is None or t0 - f["belief_time"] > 6 * 3600 or not f.get("cs") or not f.get("q50"):
        return None
    off = (t0 - f["belief_time"]) // STEP_S
    pred = np.asarray(f["q50"][off:off + 96], dtype=float)
    cs = np.asarray(f["cs"][off:off + 96], dtype=float)
    if len(pred) < 96 or len(cs) < 96:
        return None
    g = telemetry.minute_grid(site["id"], t0, t0 + 86400)
    if g["completeness"] < 0.5:
        return None
    with np.errstate(invalid="ignore"):
        act = np.nanmean(g["pv"].reshape(96, 15), axis=1)
        a30 = np.nanmean(act.reshape(48, 2), axis=1)
    p30, c30 = pred.reshape(48, 2).mean(axis=1), cs.reshape(48, 2).mean(axis=1)
    day_ok = ~np.isnan(a30) & (c30 > 0.1 * max(float(np.max(c30)), 1e-6))
    err = np.where(day_ok, np.abs(p30 - a30) / np.maximum(c30, 1e-6), 0.0)
    busted = day_ok & (err > bust_pct / 100)
    ok = ~np.isnan(act) & (cs > 0.1 * max(float(np.max(cs)), 1e-6))
    kw = float(pv.get("kw_ac") or 1.0)
    worst = int(np.argmax(err))
    return {"day": day.isoformat(), "hours": round(float(busted.sum()) * 0.5, 1),
            "worst_at": t0 + worst * 1800 if busted.any() else None, "worst_pct": round(float(err[worst]) * 100) if busted.any() else None,
            "nrmse_pct": round(float(np.sqrt(np.mean((pred[ok] - act[ok]) ** 2)) / kw) * 100, 1) if ok.any() else None}


def nightly(site_id: str, day: date) -> None:
    """The checks after the nightly jobs: the day's forecast scores (forecast.score_day stores them) and the forecast bust."""
    from . import forecast
    site = sites.full(site_id)
    if site is None:
        return
    forecast.score_day(site, day)
    rules_ = site_rules(site_id)
    pct, hours = float(_th(rules_, "Forecast", "bust_pct")), float(_th(rules_, "Forecast", "bust_hours"))
    bust = forecast_bust(site, day, pct)
    _set(bool(bust and bust["hours"] >= hours), site_id, "forecast_bust", "Forecast", "info",
         f"The day-ahead PV forecast for {day.day} {day:%B} was off by more than {pct:g} % of the clear-sky output for "
         f"{bust['hours']:g} hours" if bust else "", bust)
    ready = settled(site_id, plans.current(site_id), int(datetime.now(timezone.utc).timestamp()))
    _savings_day(site_id, day, ready)
    _fee_check(site_id, day, ready)


#: Why a day claims nothing, in words (verify.compute_day's `claim_withheld`)
WITHHELD_WORDS = {"import_unknown": "the grid import was not metered for part of the day", "battery_flow_unknown": "the battery's flow could not be read",
                  "genset_unknown": "the genset may have run without being seen"}


def _savings_day(site_id: str, day: date, ready: bool) -> None:
    """Savings (R8 2.16): a scored day that claims a negative saving against the native baseline, and a day whose claim is withheld. The titles carry
    no rupees (an alert's title is not filtered for a reader); the detail's money keys are."""
    with store.connect() as con:
        r = con.execute("SELECT data FROM savings_days WHERE site_id = ? AND day_local = ? AND baseline = 'native'", (site_id, day.isoformat())).fetchone()
    d = json.loads(r["data"]) if r else {}
    from .verify import CLAIMING
    when = f"{day.day} {day:%B}"
    negative = ready and d.get("fidelity") in CLAIMING and not d.get("claim_withheld") and (d.get("saving_rs") or 0.0) < -1.0
    _set(negative, site_id, "savings_negative", "Savings", "warning",
         f"On {when} the site did worse than the inverter's own settings would have" if negative else "",
         {"day": day.isoformat(), "saving_rs": d.get("saving_rs")} if negative else {})
    why = d.get("claim_withheld")
    withheld = ready and bool(why)
    _set(withheld, site_id, "savings_withheld", "Savings", "info",
         f"No saving is claimed for {when}: {WITHHELD_WORDS.get(why, str(why).replace('_', ' '))}" if withheld else "",
         {"day": day.isoformat()} if withheld else {})


def _fee_check(site_id: str, day: date, ready: bool) -> None:
    """Product (R8 2.16, fee notices for the owner): the month's verified saving is under three times the fee (plan 8.1: the fee at most a third of
    the verified saving), from verify.summary's fee check of the month to `day`."""
    on = False
    if ready and float(store.get_setting(f"fee:{site_id}", 0) or 0) > 0:
        from . import verify
        fee = (verify.summary(site_id, day.replace(day=1).isoformat(), day.isoformat(), see_fee=True) or {}).get("fee") or {}
        on = fee.get("within_one_third") is False
    _set(on, site_id, "fee_above_third", "Product", "warning",
         f"The verified saving of {day:%B %Y} so far is under three times the fee" if on else "", {"month": day.strftime("%Y-%m")} if on else {})
