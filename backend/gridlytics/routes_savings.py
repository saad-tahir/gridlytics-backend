"""Savings (UI-12), the pilot set-up (UI-13), switchback, bills and the fee check; the fleet roll-up."""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body
from fastapi.responses import PlainTextResponse

from . import auth, forecast, sites, store, telemetry, verify
from .routes_common import bad, full_or_404, out, parse_day, site_or_404, today
from .schemas import PilotEdit, PilotStart, Refusal, SwitchbackPaint, check

router = APIRouter()


@router.get("/api/sites/{site_id}/savings")
def savings(site_id: str, start: str | None = None, end: str | None = None):
    site_or_404(site_id)
    t = today()
    d0 = parse_day(start, t.replace(day=1))
    d1 = parse_day(end, t)
    summ = verify.summary(site_id, d0.isoformat(), d1.isoformat(), see_fee=auth.can("see_fee"))
    tdays = telemetry.days(site_id, d0.isoformat(), d1.isoformat())
    summ["energy"] = {k: round(sum((d.get(k) or 0) for d in tdays), 1) for k in ("import_kwh", "export_kwh", "pv_kwh", "load_kwh", "genset_kwh", "litres",
                                                                                  "genset_hours", "throughput_kwh", "unserved_kwh")}
    summ["energy"]["outage_hours"] = round(sum((d.get("outage_minutes") or 0) for d in tdays) / 60, 1)
    summ["energy"]["peak_kw"] = max([d.get("peak_kw") or 0 for d in tdays] or [0])
    if forecast.off_grid(sites.full(site_id) or {}):
        # a site without a grid has no outage and no grid demand: its grid reads 0 on every minute, which the day rows count a day-long outage (round 7
        # sweeps, audit role-operator-10: "Outage time 199 h" and "Peak import 0.0 kW" at the farm), so both are null, never the whole period
        summ["energy"].update(outage_hours=None, peak_kw=None)
    # the genset's litres as the actual arm reads them (metered, else the fitted curve) and where they came from, so the page can mark curve litres
    # "Estimated"; the day rows hold metered litres only, a blank reading summed as 0. Over a range of more than about two months the minutes the
    # genset ran with neither its power nor its fuel read are not replayed: such a range says its litres are "not_counted" (null), never the day rows'
    # zero (round 7 sweeps, audit frontend-states-formats-02: "Diesel burned 0.0 L" with no source over 62 days)
    summ["energy"]["litres_source"] = None
    if (d1 - d0).days >= 0:
        try:
            lu = verify.litres_used(site_id, d0.isoformat(), d1.isoformat(), simulate=(d1 - d0).days <= 62)
            if lu.get("unread_minutes"):
                summ["energy"].update(litres=None, litres_source="not_counted")
            elif lu.get("litres_source"):
                summ["energy"].update(litres=round(float(lu["litres"]), 1), litres_source=lu["litres_source"])
        except Exception:  # noqa: BLE001  (the figure is a label; never fail the page for it)
            pass
    if not auth.can("see_costs"):
        # a viewer reads the energy, the litres (saved and burnt) and the day counts, none of them money; the filter takes out every rupee figure, the
        # levers and the fee (audit role-viewer-19)
        summ["hidden_for_role"] = True
    return out(summ)


@router.post("/api/sites/{site_id}/savings/recompute")
def recompute(site_id: str, inp: dict = Body(default={})):
    auth.need("run_jobs", "Recomputing savings")
    site_or_404(site_id)
    t = today()
    d0 = parse_day(inp.get("start"), t - timedelta(days=7))
    d1 = parse_day(inp.get("end"), t - timedelta(days=1))
    if (d1 - d0).days > 62:
        raise Refusal(422, "Recompute at most 62 days at a time", "invalid")
    # round 7 (audit pr-review-02): the later days the forward walk re-scored, and those whose claim changed in a closed month, beside the count
    return verify.compute_range_detail(site_id, d0, d1, by=auth.actor())


@router.get("/api/sites/{site_id}/savings/export.csv")
def savings_csv(site_id: str, start: str | None = None, end: str | None = None):
    auth.need("export", "Exporting savings")
    site_or_404(site_id)
    t = today()
    d0, d1 = parse_day(start, t.replace(day=1)).isoformat(), parse_day(end, t).isoformat()
    summ = verify.summary(site_id, d0, d1)
    lines = ["day,saving_native_rs,saving_tuned_rs,fidelity,actual_cost_rs,native_cost_rs,litres_actual,litres_native,peak_kw,peak_import_kwh"]
    for d in summ["days"]:
        lines.append(",".join("" if d.get(k) is None else str(d[k]) for k in ("day", "saving_native_rs", "saving_tuned_rs", "fidelity", "actual_cost_rs",
                                                                               "native_cost_rs", "litres_actual", "litres_native", "peak_kw", "peak_import_kwh")))
    store.audit(auth.actor(), "exported savings", site_id, {"start": d0, "end": d1})
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{site_id}-savings.csv"'})


@router.get("/api/sites/{site_id}/switchback")
def switchback(site_id: str):
    site_or_404(site_id)
    return out(verify.switchback_result(site_id))


#: the control tiers in words, for a refusal
TIER_WORDS = {"monitor": "monitor", "advisory_writes": "advisory writes", "closed_loop": "closed loop"}


@router.put("/api/sites/{site_id}/switchback")
def switchback_paint(site_id: str, inp: dict = Body(...)):
    """Tab C: paint the calendar (edit_sites), with a strict model (schemas.SwitchbackPaint). Round 7: a monitor-tier site only recommends the plan,
    so it has no switchback (audit pages-D-11); the block length is one the site's control tier runs (verify.BLOCK_DAYS_BY_TIER; audit
    flow-savings-pilot-bill-25); the first day is after today, so no arm is drawn over days already run (audit security-roles-06)."""
    auth.need("edit_sites", "Painting the switchback calendar")
    site = site_or_404(site_id)
    req = check(SwitchbackPaint, inp)
    tier = site.get("tier", "monitor")
    allowed = verify.BLOCK_DAYS_BY_TIER.get(tier, ())
    if not allowed:
        raise Refusal(422, "A monitor-tier site only recommends the plan, so no switchback can run there: its saving is the counterfactual's alone",
                      "monitor_tier")
    if req.block_days not in allowed:
        raise Refusal(422, f"At the {TIER_WORDS.get(tier, tier)} tier each block must be {' or '.join(str(n) for n in allowed)} days long: the "
                           "installer changes the table once per switch", "invalid")
    start = date.fromisoformat(req.start)
    if start <= today():
        raise Refusal(422, "The calendar must start after today: no arm is drawn for a day that has begun", "invalid")
    return verify.plan_switchback(site_id, start, req.fortnights, req.block_days, auth.actor())


@router.get("/api/sites/{site_id}/bills")
def bills(site_id: str):
    auth.need("see_costs", "Seeing bills")
    site_or_404(site_id)
    return out(verify.bills(site_id))


@router.post("/api/sites/{site_id}/bills")
def bill_add(site_id: str, inp: dict = Body(...)):
    auth.need("upload_data", "Entering a bill")
    site_or_404(site_id)
    try:
        return out(verify.add_bill(site_id, inp, auth.actor()))
    except ValueError as exc:
        raise bad(exc) from None


@router.put("/api/sites/{site_id}/bills/{bill_id}")
def bill_correct(site_id: str, bill_id: str, inp: dict = Body(...)):
    """Correct a bill entered with a mistake (the fields of entering one); who may enter a bill may correct it, with an audit line."""
    auth.need("upload_data", "Correcting a bill")
    site_or_404(site_id)
    try:
        return out(verify.correct_bill(site_id, bill_id, inp, auth.actor()))
    except LookupError:
        raise Refusal(404, "No such bill", "not_found") from None
    except ValueError as exc:
        raise bad(exc) from None


@router.delete("/api/sites/{site_id}/bills/{bill_id}")
def bill_delete(site_id: str, bill_id: str):
    """Withdraw a bill entered by mistake, with an audit line."""
    auth.need("upload_data", "Deleting a bill")
    site_or_404(site_id)
    try:
        return verify.delete_bill(site_id, bill_id, auth.actor())
    except LookupError:
        raise Refusal(404, "No such bill", "not_found") from None


@router.put("/api/sites/{site_id}/fee")
def fee_put(site_id: str, inp: dict = Body(...)):
    auth.need("edit_money_settings", "Setting the fee")
    site_or_404(site_id)
    try:
        fee = float(inp.get("fee_rs_per_month", 0))
        if fee < 0:
            raise ValueError("The fee cannot be negative")
    except (TypeError, ValueError) as exc:
        raise bad(exc) from None
    store.set_setting(f"fee:{site_id}", fee)
    store.audit(auth.actor(), "set the fee", site_id, {"fee_rs_per_month": fee})
    return {"fee_rs_per_month": fee}


# ------------------------------------------------------------------------------------------------ pilot (UI-13)

PILOT_DEFAULT = {"baseline_start": None, "baseline_days": 14, "criteria": {"min_saving_pct": 8.0, "max_unserved_kwh": 0.0, "reserve_adequacy_pct": 90.0,
                                                                          "fidelity_pct": 80.0, "fee_multiple": 3.0, "forecast_pv_nrmse_max": 0.35},
                 "signoff": None, "amendments": [], "closures": []}


def _pilot_data(r) -> dict:
    """The stored set-up over the defaults. A row stored before the request model (round 7, audit pages-D-04) may hold criteria that are not a set of
    numbers (null crashed the page); each criterion that is not a number reads as its default."""
    stored = json.loads(r["data"]) if r else {}
    data = {**copy.deepcopy(PILOT_DEFAULT), **(stored if isinstance(stored, dict) else {})}
    got = data.get("criteria") if isinstance(data.get("criteria"), dict) else {}
    data["criteria"] = {k: float(got[k]) if isinstance(got.get(k), (int, float)) and not isinstance(got.get(k), bool) else v
                        for k, v in PILOT_DEFAULT["criteria"].items()}
    if not isinstance(data.get("closures"), list):
        data["closures"] = []
    # an amendment recorded before round 7 names the keys sent, with no reason and no values (audit flow-savings-pilot-bill-30, pages-D-17)
    data["amendments"] = [{**a, "reason": a.get("reason"), "changes": list(a.get("changes") or []), "values": list(a.get("values") or [])}
                          for a in (data["amendments"] if isinstance(data.get("amendments"), list) else []) if isinstance(a, dict)]
    return data


#: the pilot's fields only a role that sees the fee reads, in the set-up and in its amendments (owner's rule: no money ratio without the fee)
PILOT_FEE_FIELDS = frozenset({"criteria.fee_multiple"})


def _pilot_for_reader(ans: dict) -> dict:
    """The pilot answer as the reader may see it: without `see_fee`, no fee multiple in the criteria nor in an amendment's changes and values."""
    if auth.can("see_fee"):
        return ans
    p = ans["pilot"]
    crit = {k: v for k, v in p["criteria"].items() if f"criteria.{k}" not in PILOT_FEE_FIELDS}
    ams = [{**a, "changes": [c for c in a["changes"] if c not in PILOT_FEE_FIELDS],
            "values": [v for v in a["values"] if v.get("field") not in PILOT_FEE_FIELDS]} for a in p["amendments"]]
    return {**ans, "pilot": {**p, "criteria": crit, "amendments": ams}}


def _pilot_values(before: dict, after: dict) -> list[dict]:
    """Each changed field of the set-up with its value before and after ("criteria.min_saving_pct": 8.0 to 5.0); a field sent unchanged is no change."""
    out = []
    for k in ("baseline_start", "baseline_days"):
        if before.get(k) != after.get(k):
            out.append({"field": k, "before": before.get(k), "after": after.get(k)})
    for k in PILOT_DEFAULT["criteria"]:
        a, b = before["criteria"].get(k), after["criteria"].get(k)
        if a != b:
            out.append({"field": f"criteria.{k}", "before": a, "after": b})
    if before.get("closures") != after.get("closures"):
        out.append({"field": "closures", "before": before.get("closures"), "after": after.get("closures")})
    return out


def _adopt_capture(site_id: str, version: int) -> tuple[list[dict], dict]:
    """A started pilot's pins with a later capture adopted, from the day it was captured (it stood for that day's end; never before the last pin),
    and the amendment's value. Only a capture newer than the one the pilot replays now can be adopted (round 7, audit flow-savings-pilot-bill-12)."""
    pins = verify.pilot_capture_pins(site_id)
    cur = pins[-1]["version"] if pins else None
    row = next((v for v in sites.native_versions(site_id) if v["version"] == version), None)
    if row is None:
        raise Refusal(422, f"There is no capture version {version}", "invalid")
    if cur is not None and version <= cur:
        raise Refusal(422, f"The pilot replays version {cur}: only a later capture can be adopted", "invalid")
    since = max(verify.local_day(row["captured_at"]).isoformat(), pins[-1]["from"] if pins else "")
    return pins + [{"version": version, "from": since}], {"field": "capture_version", "before": cur, "after": version}


@router.get("/api/sites/{site_id}/pilot")
def pilot_get(site_id: str):
    site_or_404(site_id)
    return _pilot_for_reader(_pilot_answer(site_id))


def _pilot_answer(site_id: str) -> dict:
    """The set-up, its start, the capture the pilot replays and the calendar. Once started, `capture` is the pinned capture in force now
    (verify.pilot_capture_pins) and `capture_newer` a later capture no amendment has adopted yet (round 7, audit flow-savings-pilot-bill-12)."""
    with store.connect() as con:
        r = con.execute("SELECT * FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
    data = _pilot_data(r)
    latest, capture, newer = sites.native_latest(site_id), None, None
    pins = verify.pilot_capture_pins(site_id) if r is not None and r["started"] else []
    if pins:
        data["capture_pins"] = pins
        capture = verify.native_for_day(site_id, max(today(), date.fromisoformat(pins[-1]["from"])))
        newer = latest if latest and capture and latest["version"] > capture["version"] else None
    tier = (sites.get(site_id) or {}).get("tier", "monitor")
    gates, checks = verify.pilot_gates(site_id, data, today(), started=bool(r is not None and r["started"]))
    rec = verify.paint_record(site_id)
    return {"pilot": data, "started": r["started"] if r else None, "capture": capture or latest, "capture_newer": newer,
            "switchback": verify.switchback_blocks(site_id),
            # tab A: the window's completeness so far and the KPIs the pilot measures (round 7, audit pages-D-19)
            "baseline_quality": verify.baseline_quality(site_id, data["baseline_start"], data["baseline_days"], today()),
            "kpis": verify.pilot_kpis(site_id),
            # tab C and the start (round 7, audit pages-D-11, flow-savings-pilot-bill-25, security-roles-06, flow-savings-pilot-bill-11): the
            # control tier and the block lengths it runs, the calendar's paint as stored, its rule checks and what the start still needs
            "tier": tier, "block_days_allowed": list(verify.BLOCK_DAYS_BY_TIER.get(tier, ())),
            "calendar": {k: rec.get(k) for k in PAINT_FIELDS} if rec else None, "calendar_checks": checks, "start_gates": gates}


#: the paint record's fields the pilot answers (the blocks it drew are the calendar's own)
PAINT_FIELDS = ("painted_at", "painted_on", "by", "draw", "paint_count", "start", "pairs", "block_days")

#: a calendar rule check in words, for the start's refusal
CHECK_WORDS = {"balanced": "both arms equally often", "paired": "one block of each arm in each pair", "joined": "blocks back to back",
               "length": f"one block length of {' or '.join(map(str, verify.BLOCK_DAYS_OPTIONS))} days",
               "tier-length": "a block length the control tier runs",
               "enough": f"at least {verify.SWITCHBACK_MIN_DAYS_PER_ARM} days on each arm", "after-baseline": "a start after the baseline window",
               "future": "a start after today", "closures": "no block on a holiday closure"}


@router.put("/api/sites/{site_id}/pilot")
def pilot_put(site_id: str, inp: dict = Body(...)):
    """Tabs A and D: the baseline window and the success criteria, typed and in range (schemas.PilotEdit). The sign-off and the amendments are the
    server's: the start writes the one, a change after the start appends to the other (round 7, audit pages-D-04, security-roles-05,
    flow-savings-pilot-bill-10). Each change is audited with its fields' values before and after; once the pilot has started it is an amendment
    that needs a reason and records those values, and a save that changes nothing is refused rather than recorded (audit flow-savings-pilot-bill-30,
    pages-D-17)."""
    auth.need("start_pilot", "Editing the pilot")
    site_or_404(site_id)
    edit = check(PilotEdit, inp)
    sent = edit.model_dump(exclude_unset=True)
    reason = sent.pop("reason", "").strip()
    with store.write_transaction() as con:
        r = con.execute("SELECT * FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
        data = _pilot_data(r)
        started = r["started"] if r else None
        if started and "baseline_start" in sent and sent["baseline_start"] is None:
            raise Refusal(422, "A started pilot keeps its baseline window: change its first day instead", "invalid")
        if "criteria" in sent:
            sent["criteria"] = {**data["criteria"], **sent["criteria"]}
        if "closures" in sent:  # tab C's declared holiday closures, in date order (round 7, audit pages-D-19)
            sent["closures"] = sorted((c.model_dump() for c in edit.closures), key=lambda c: (c["start"], c["end"]))
        adopt = sent.pop("capture_version", None)
        new = {**data, **sent}
        values = _pilot_values(data, new)
        if adopt is not None:
            if not started:
                raise Refusal(422, "Until the pilot starts it uses the latest capture; a capture is adopted only by a started pilot", "not_started")
            new["capture_pins"], value = _adopt_capture(site_id, adopt)
            values.append(value)
        if started:
            if not values:
                raise Refusal(422, "Nothing changed, so there is no amendment to record", "no_change")
            if not reason:
                raise Refusal(422, "Give the amendment's reason: it is recorded with the change", "reason_needed")
            new["amendments"] = data["amendments"] + [{"at": store._now(), "by": auth.actor(), "reason": reason,
                                                       "changes": [v["field"] for v in values], "values": values}]
        if values:
            con.execute("INSERT INTO pilots (site_id, data, updated) VALUES (?,?,?) ON CONFLICT(site_id) DO UPDATE SET data = excluded.data, "
                        "updated = excluded.updated", (site_id, json.dumps(new), store._now()))
            store.audit(auth.actor(), "amended the pilot" if started else "changed the pilot set-up", site_id,
                        {"reason": reason, "values": values} if started else {"values": values}, con=con)
    return pilot_get(site_id)


def _start_gates(site_id: str, r) -> None:
    """Refuse the start at the first gate not met (round 7, audit flow-savings-pilot-bill-11, pages-D-18, pages-D-11): a tier that runs the plan,
    the capture, the baseline window, the calendar and its rule checks, in that order (before: the capture and any calendar, so a past calendar at a
    monitor site with no window started). Called inside the start's write transaction, so a repaint cannot slip between the checks and the lock."""
    gates = dict((g["id"], g) for g in verify.pilot_gates(site_id, _pilot_data(r), today())[0])
    if not gates["tier"]["ok"]:
        raise Refusal(422, "A monitor-tier site only recommends the plan, so no switchback can run there: raise its control tier to advisory writes "
                           "before the pilot starts", "monitor_tier")
    if not gates["capture"]["ok"]:
        raise Refusal(422, "Capture the inverter's native settings first (tab B)", "no_capture")
    if not gates["baseline"]["ok"]:
        raise Refusal(422, "Choose the baseline window first (tab A)", "no_baseline")
    if not gates["calendar"]["ok"]:
        raise Refusal(422, "Paint the switchback calendar first (tab C)", "no_calendar")
    if not gates["rules"]["ok"]:
        failing = gates["rules"]["failing"]
        raise Refusal(422, f"The switchback calendar does not meet {len(failing)} of its rule checks ({'; '.join(CHECK_WORDS.get(c, c) for c in failing)}): "
                           "fix it in tab C", "calendar_rules")


@router.post("/api/sites/{site_id}/pilot/start")
def pilot_start(site_id: str, inp: dict = Body(default={})):
    """Sign off and start the pilot, once: the start time and the sign-off are the pre-registration, so a second start is refused rather than
    written over them (round 7, audit flow-savings-pilot-bill-09); a later change is an amendment (PUT)."""
    auth.need("start_pilot", "Starting the pilot")
    site_or_404(site_id)
    name = check(PilotStart, inp).name
    with store.write_transaction() as con:
        r = con.execute("SELECT * FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
        if r is not None and r["started"]:
            raise Refusal(409, "The pilot has already started: its start and sign-off stand, and a change is an amendment", "already_started")
        _start_gates(site_id, r)
        now = store._now()
        # the capture the pilot is signed off on is pinned: a later one stands for a pilot day only once an amendment adopts it (round 7, audit
        # flow-savings-pilot-bill-12)
        pin = {"version": sites.native_latest(site_id)["version"], "from": verify.local_day(now).isoformat()}
        data = {**_pilot_data(r), "signoff": {"by": auth.actor(), "at": now, "name": name}, "capture_pins": [pin]}
        con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?) ON CONFLICT(site_id) DO UPDATE SET data = excluded.data, "
                    "started = excluded.started, updated = excluded.updated", (site_id, json.dumps(data), now, now))
        con.execute("UPDATE switchback SET locked = 1 WHERE site_id = ?", (site_id,))
        store.audit(auth.actor(), "started the pilot", site_id, {"name": name, "capture_version": pin["version"]}, con=con)
    return pilot_get(site_id)


def _ics_text(value: str) -> str:
    """An iCalendar TEXT value (RFC 5545 3.3.11): backslash, semicolon and comma escaped, a line break written as \\n, other control characters
    dropped, so typed text such as a site's name cannot start a property or an event of its own (round 7, audit security-roles-14)."""
    v = value.replace("\r\n", "\n").replace("\r", "\n")
    v = "".join(ch for ch in v if ch == "\n" or ch == "\t" or not (ord(ch) < 32 or ord(ch) == 127))
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> list[str]:
    """A content line folded at 75 octets (RFC 5545 3.1): each continuation starts with a space, and no UTF-8 character is split."""
    out, cur, size, limit = [], "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " ", 1, 75
        cur += ch
        size += n
    return out + [cur]


@router.get("/api/sites/{site_id}/pilot/calendar.ics")
def pilot_ics(site_id: str):
    """The calendar for a calendar app (tab C). Round 7 (audit security-roles-14, flow-savings-pilot-bill-26): the site's name is escaped as
    TEXT, each event carries DTSTAMP (when the file was made, UTC), long lines are folded, and the file comes with its name."""
    s = site_or_404(site_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Gridlytics//Switchback//EN"]
    for b in verify.switchback_blocks(site_id):
        d0 = date.fromisoformat(b["block_start"])
        d1 = date.fromisoformat(b["block_end"]) + timedelta(days=1)
        summary = _ics_text(f"{s['name']}: {'plan' if b['arm'] == 'plan' else 'native settings'} block")
        lines += ["BEGIN:VEVENT", f"UID:{site_id}-{b['block_start']}@gridlytics", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{d0:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{d1:%Y%m%d}", f"SUMMARY:{summary}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(part for line in lines for part in _ics_fold(line)) + "\r\n"
    return PlainTextResponse(body, media_type="text/calendar", headers={"Content-Disposition": f'attachment; filename="{site_id}-switchback.ics"'})


@router.get("/api/fleet/savings")
def fleet_savings(month: str | None = None):
    t = today()
    m = month or t.strftime("%Y-%m")
    try:
        d0 = date(int(m[:4]), int(m[5:7]), 1)
    except (ValueError, IndexError):
        raise Refusal(422, "The month is YYYY-MM", "invalid") from None
    d1 = (d0.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    rows = []
    for s in sites.listing():
        summ = verify.summary(s["id"], d0.isoformat(), d1.isoformat())
        n = summ["baselines"].get("native", {})
        # chain_stale_days: the site's days out of step until recomputed, so its figures may not add up (audit pr-review-06)
        rows.append({"site_id": s["id"], "name": s["name"], "saving_rs": n.get("saving_rs"), "claimed_rs": n.get("claimed_rs"),
                     "litres_saved": n.get("litres_saved"), "verified_days": n.get("verified_days"), "preliminary": summ["preliminary"],
                     "chain_stale_days": list(summ.get("chain_stale_days") or [])})
    return out({"month": m, "sites": rows, "total_claimed_rs": round(sum(r["claimed_rs"] or 0 for r in rows), 0),
                "stale_sites": sum(1 for r in rows if r["chain_stale_days"])})


_ = full_or_404
