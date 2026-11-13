"""A site's lifecycle: the stage track of the redesigned web app (UX backend ask K1; plan §0.3.1, B21.2 item 3, C19.2 item 8, D9.1).

The owner's staged journey is drawn as a track of six stages, each Done, Now, Waiting or Not used, with one line of words and one next step: Set-up,
Collecting data, Recommending, Delivering, Verifying (the pilot, optional) and Reporting. After the first weeks the stages run side by side, so this is
a track of states, not a corridor, and it never moves the control tier by itself. The Overview's stage track, Fleet's Stage column and filter and the
site switcher read it from `lifecycle` on the site's answer (GET /sites/:id and every answer that is the site) and on each fleet row (GET /fleet), so
the browser derives nothing and Fleet makes no query per site.

The answer:
    {stage, words, next, target, all_done, stages: [{key, state, words, next, target}, ...six, in STAGES order]}
- `stage` is the current stage: the first whose state is "now", else the furthest of the first four that is done (a site whose every stage in use is
  done reads "Delivering · advisory"). `words` is its short form for a chip, a switcher row or a Fleet cell ("Draft: step C of 6", "Collecting data:
  day 6 of 14", "Recommending", "Delivering · advisory", "Pilot week 3 of 8", "October statement ready"); a stage's own `words` is the track's line.
- `next` is a key from NEXT (the web app's registry gives each its words and its address), chosen by the reader's permissions; `target` the step
  letter or the month it needs ("c", "2026-10"), else null. A viewer sees the track as states only: every stage's `next` is null but Reporting's,
  and the one action offered is the energy report (plan §0.3.1, D2.4). A machine key is not served the lifecycle at all: its `sites:read` scope is
  the site's settings, and the track speaks of the site's data, its pilot and its statements (audit security-roles-07; routes_sites._site_answer).
- `all_done`: every stage is done or not used, so the track folds to the chip.

Every date and time in the words is Pakistan time. No money: nothing here is priced."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from . import auth, store

PKT = timezone(timedelta(hours=5))

STAGES = ("setup", "collecting", "recommending", "delivering", "verifying", "reporting")
STATES = ("done", "now", "waiting", "not_used")
#: The next steps a stage offers (plan §0.3.1), each the web app's to word and to link:
#: continue_setup (Set-up at `target`'s step), activate (Review and activate), ask_to_activate ("An administrator activates it."), record_native
#: (Set-up E), check_source (Set-up › Data and devices), upload_csv (the same, its CSV upload), compare_native (Plan › Versus native), open_plan (Plan),
#: mark_applied (Plan › Inverter table), raise_tier (Set-up D), ask_to_raise_tier ("Ask your administrator to raise the tier"), start_pilot and
#: open_pilot (Savings › Pilot), generate_statement (Commercial's Generate, for `target`'s month), energy_report (the energy report of `target`'s month)
NEXT = ("continue_setup", "activate", "ask_to_activate", "record_native", "check_source", "upload_csv", "compare_native", "open_plan", "mark_applied",
        "raise_tier", "ask_to_raise_tier", "start_pilot", "open_pilot", "generate_statement", "energy_report")
#: Set-up's steps (plan C2.2): A to E, then Review and activate, so a draft reads "step C of 6"
SETUP_STEPS = ("a", "b", "c", "d", "e", "review")
#: The days of data the first forecasts learn from (the fleet's `collecting_day` counts the same 14)
COLLECTING_DAYS = 14
#: No reading for this long while data is being collected: the stage says since when (the fleet counts a site online with a reading in 30 minutes)
STALE_S = 1800
#: How long after its activation a site without a plan is promised its first plan "within 15 minutes" (plan §0.3.2: the sentence of the moment of
#: activation); after that the stage says what holds the plan back (UX K1 check)
FIRST_PLAN_PROMISE_S = 1800
TIER_WORDS = {"monitor": "monitor", "advisory_writes": "advisory", "closed_loop": "closed loop"}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")


# ------------------------------------------------------------------------------------------------ words


def _day_words(d: date, today: date, weekday: bool = False) -> str:
    """"30 Nov", "Mon 16 Nov", with the year when it is not this year's ("31 Jan 2027")."""
    out = f"{d.day} {_MONTHS[d.month - 1]}" + ("" if d.year == today.year else f" {d.year}")
    return f"{_WEEKDAYS[d.weekday()]} {out}" if weekday else out


def _since_words(ts: int, now: int) -> str:
    """A reading's time in Pakistan time: "09:10" today, else "Tue 10 Nov, 09:10"."""
    at, today = datetime.fromtimestamp(ts, PKT), datetime.fromtimestamp(now, PKT).date()
    return f"{at:%H:%M}" if at.date() == today else f"{_day_words(at.date(), today, weekday=True)}, {at:%H:%M}"


def _month_before(d: date) -> date:
    """The first day of the calendar month before d's."""
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def _month_after(d: date) -> date:
    """The first day of the calendar month after d's."""
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def _stage(key: str, state: str, words: str, nxt: str | None = None, target: str | None = None) -> dict:
    return {"key": key, "state": state, "words": words, "next": nxt, "target": target}


def collecting_day(first: int | None, now: int) -> int | None:
    """The day of data collection the site is in at `now`, counted from its first reading `first` (1 on the first day, COLLECTING_DAYS on the last);
    0 before any reading; None once COLLECTING_DAYS days of data are in. The fleet row's `collecting_day` and the track's "Collecting data: day 6 of
    14" are this one figure (UX K1 check)."""
    if not first:
        return 0
    days = (now - first) / 86400
    return None if days >= COLLECTING_DAYS else int(days) + 1


# ------------------------------------------------------------------------------------------------ the stages, from the facts


def _setup(f: dict, can) -> dict:
    if not f["active"]:
        step = f["setup_step"]
        if step:
            # the first step activation still asks for; an operator continues it too (A, B, C and E are edit_sites), an analyst only waits
            go = can("manage_sites") or can("edit_sites")
            return _stage("setup", "now", f"Draft: step {step.upper()} of {len(SETUP_STEPS)}", "continue_setup" if go else "ask_to_activate",
                          step if go else None)
        return _stage("setup", "now", "Set-up: not activated", "activate" if can("manage_sites") else "ask_to_activate",
                      "review" if can("manage_sites") else None)
    if not f["native"]:
        # step E is not needed to activate, but the plan's fallback and the savings baseline wait for it
        return _stage("setup", "done", "Active · native settings not recorded", "record_native" if can("edit_sites") else None,
                      "e" if can("edit_sites") else None)
    return _stage("setup", "done", "Active")


def _collecting(f: dict, now: int, can) -> dict:
    if not f["active"]:
        return _stage("collecting", "waiting", "Starts when the site is activated")
    first, last = f["first_ts"], f["last_ts"]
    day = collecting_day(first, now)
    if day is None:
        return _stage("collecting", "done", f"{COLLECTING_DAYS} days of data")
    nxt = "upload_csv" if f["source_kind"] == "csv" and can("upload_data") else ("check_source" if can("edit_sites") else None)
    if not first:
        words = "Waiting for the edge device" if f["source_kind"] == "edge" else "Waiting for the first data"
    elif last is not None and now - last > STALE_S:
        words = f"No data since {_since_words(last, now)}"
    else:
        words = f"Collecting data: day {day} of {COLLECTING_DAYS}"
    return _stage("collecting", "now", words, nxt)


def _no_plan_words(f: dict, now: int) -> str:
    """Recommending's line while no plan was ever made: the planning runs failing now say so (plans.tick_failing, as the Plan page's `failing`;
    round 7, parity-architecture-06: never "within 15 minutes" while they fail), the first half hour after activation promises the first plan (plan
    §0.3.2), and after that the Plan page says why none came (a plan held by its guard, or no run since)."""
    failing = f.get("failing")
    if failing:
        if failing.get("reason") == "no_tariff_version":
            return "No plan yet: a grid connection's tariff has no version in force"
        n = int(failing.get("ticks") or 1)
        return "No plan yet: the last planning run failed" if n == 1 else f"No plan yet: the last {n} planning runs failed"
    activated = f.get("activated_ts")
    if activated is not None and now - activated < FIRST_PLAN_PROMISE_S:
        return "The first plan arrives within 15 minutes"
    return "No plan yet: the Plan page says why"


def _recommending(f: dict, now: int, can) -> dict:
    if not f["active"]:
        return _stage("recommending", "waiting", "Starts when the site is activated")
    if f["first_plan_ts"] is None:
        # no plan was ever made, whatever tier the site is allowed: nothing is recommended or delivered yet (UX K review: a site raised to advisory
        # before its first plan read "Done: the plan runs at the advisory tier")
        words = _no_plan_words(f, now)
        return _stage("recommending", "now", words, None if words.startswith("The first plan") else "open_plan")
    if f["tier"] != "monitor":
        return _stage("recommending", "done", f"Done: the plan runs at the {TIER_WORDS.get(f['tier'], f['tier'])} tier")
    stage = f["forecast_stage"]
    words = "Recommendation" + ("" if stage is None else f" · forecasts at Stage {stage}") + (", Preliminary" if f["preliminary"] else "")
    if f["native"]:
        # Versus native is listed only at a site with a battery (D3.3): elsewhere the plan itself
        nxt = "compare_native" if f["battery"] else "open_plan"
    else:
        nxt = "record_native" if can("edit_sites") else "open_plan"
    return _stage("recommending", "now", words, nxt, "e" if nxt == "record_native" else None)


#: Delivering's line at the monitor tier while the tier cannot be raised (`tier_block`, the code PUT /tier refuses the advisory tier with:
#: routes_sites._tier_refusal, one rule for the refusal and the track; UX K1 check): the source supports monitor only ("above_capable": an edge device
#: says what it can control when it connects), or a CSV source none of whose files carries the inverter's mode ("no_inverter_mode")
MONITOR_ONLY = {"edge": "Waiting for the edge device to say what it can control"}
MONITOR_ONLY_OTHER = "Monitor only: this data source writes nothing"
NO_INVERTER_MODE = "Monitor only until a file carries the inverter's mode"
#: The control states (routes_sites.control_state) in which the plan does not reach the inverter for now, though the tier allows it, with the step
#: that looks into it: a pause or a release to native a person set and the clock demotion (Plan, where control is resumed and the clock's offset is
#: said), and what the edge itself reports (plans.edge_view: Set-up › Data and devices, for `edit_sites`). The switchback calendar's native-block day
#: is the pilot's own and the plan comes back after it, so it is not one of them, nor a forced charge or a hold, which run within the plan
OFF_PLAN = {"paused": "open_plan", "native": "open_plan", "clock_demoted": "open_plan", "edge_disabled": "check_source",
            "edge_fallback": "check_source", "edge_released": "check_source", "edge_hold": "check_source", "edge_refused": "check_source",
            "edge_partial": "check_source"}


def _delivering(f: dict, can) -> dict:
    tier = f["tier"]
    if not f["active"]:
        # a tier allowed before activation delivers nothing yet: the tick plans active sites only
        return _stage("delivering", "waiting", "Starts when the site is activated")
    if tier == "monitor":
        block = f.get("tier_block")
        if block == "above_capable":
            return _stage("delivering", "waiting", MONITOR_ONLY.get(f["source_kind"] or "", MONITOR_ONLY_OTHER))
        if block == "no_inverter_mode":
            return _stage("delivering", "waiting", NO_INVERTER_MODE, "upload_csv" if can("upload_data") else None)
        if not can("change_tier"):
            return _stage("delivering", "waiting", "Monitor tier: recommendations only", "ask_to_raise_tier")
        if block == "no_capture":
            return _stage("delivering", "waiting", "Monitor tier: recommendations only", "record_native", "e")
        return _stage("delivering", "waiting", "Monitor tier: recommendations only", "raise_tier", "d")
    name = TIER_WORDS.get(tier, tier)
    key = f["control_key"]
    if key == "unconfirmed":
        words = "Today's table not marked applied" if f["control_label"] == "Table not confirmed today" else f["control_label"]
        return _stage("delivering", "now", words, "mark_applied" if can("apply_plan") else None)
    if key == "edge_offline":
        return _stage("delivering", "now", f"Delivering · {name} · {f['control_label'].lower()}", "check_source" if can("edit_sites") else None)
    if key in OFF_PLAN:
        nxt = OFF_PLAN[key] if OFF_PLAN[key] != "check_source" or can("edit_sites") else "open_plan"
        return _stage("delivering", "now", f["control_label"], nxt)
    return _stage("delivering", "done", f"Delivering · {name}")


def _verifying(f: dict, today: date, can) -> dict:
    if not f["pilot_started"]:
        return _stage("verifying", "not_used", "No pilot (optional)", "start_pilot" if can("start_pilot") else None)
    # where the pilot stands in its switchback calendar: verify.pilot_progress, the one function the Pilot band reads too (plan Part F: "the
    # Overview's stage track shows the same words"; UX K1 check)
    p = f["pilot"]
    if p is None:
        return _stage("verifying", "now", "Pilot started", "open_pilot")
    if p["phase"] == "before":
        return _stage("verifying", "now", f"Pilot starts on {_day_words(date.fromisoformat(p['first']), today, weekday=True)}", "open_pilot")
    if p["phase"] == "over":
        return _stage("verifying", "done", "Pilot calendar complete", "open_pilot")
    arm = {"plan": " · today runs the plan", "native": " · today runs your own settings"}.get(p["arm"] or "", "")
    return _stage("verifying", "now", f"Pilot week {p['week']} of {p['weeks']}{arm}", "open_pilot")


def _reporting(f: dict, today: date, can) -> dict:
    report = "energy_report" if can("energy_report") else None
    this_month = today.strftime("%Y-%m")
    if f["first_plan_ts"] is None:
        return _stage("reporting", "waiting", "First statement a month after the first plan", report, this_month if report else None)
    first_day = datetime.fromtimestamp(f["first_plan_ts"], PKT).date()
    m = _month_before(today)
    if first_day <= m:
        # the month before this one ran on a plan from its first day: its statement can be made
        name, period = _MONTH_NAMES[m.month - 1], m.strftime("%Y-%m")
        if f["statement_made"]:
            return _stage("reporting", "done", f"{name} statement made", report, period if report else None)
        nxt = "generate_statement" if can("export") else report
        return _stage("reporting", "now", f"{name} statement ready", nxt, period if nxt else None)
    full = first_day if first_day.day == 1 else _month_after(first_day)
    end = _month_after(full) - timedelta(days=1)
    return _stage("reporting", "waiting", f"First statement after {_day_words(end, today)}", report, this_month if report else None)


#: The current stage's short words where they differ from its line on the track
def _short(st: dict, f: dict) -> str:
    if st["key"] == "recommending":
        return "Recommending"
    if st["key"] == "delivering":
        return f"Delivering · {TIER_WORDS.get(f['tier'], f['tier'])}"
    if st["key"] == "verifying":
        return st["words"].split(" · ")[0]
    return st["words"]


def assemble(f: dict, now: int, can, states_only: bool = False) -> dict:
    """The lifecycle from the site's facts (`facts`) at `now`, for a reader whose permissions `can(permission)` answers; `states_only` for a reader who
    holds no permission but the energy report (a viewer) or none at all (a machine key)."""
    today = datetime.fromtimestamp(now, PKT).date()
    stages = [_setup(f, can), _collecting(f, now, can), _recommending(f, now, can), _delivering(f, can), _verifying(f, today, can),
              _reporting(f, today, can)]
    if states_only:
        for st in stages:
            if st["key"] != "reporting":
                st["next"] = st["target"] = None
    cur = next((st for st in stages if st["state"] == "now"), None)
    if cur is None:
        cur = next((st for st in reversed(stages[:4]) if st["state"] == "done"), stages[0])
    report = stages[-1]
    nxt, target = (report["next"], report["target"]) if states_only else (cur["next"], cur["target"])
    return {"stage": cur["key"], "words": _short(cur, f), "next": nxt, "target": target,
            "all_done": all(st["state"] in ("done", "not_used") for st in stages), "stages": stages}


# ------------------------------------------------------------------------------------------------ the facts, read


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())


def facts(site: dict, now: int, *, first_ts: int | None, last_ts: int | None, plan: dict | None, control: dict, setup_step: str | None,
          battery: bool | None = None, tier_block: str | None = None) -> dict:
    """What the stages are worked out from. `site` is a site row (sites.get or sites.listing; full() too); `first_ts` and `last_ts` its first and newest
    reading; `plan` the plan in force (plans.current) or None; `control` the control chip (routes_sites.control_state); `setup_step` the first step of
    A to C that activation still asks for (None once they are complete); `battery` whether the site has one (read when not given); `tier_block` the
    code PUT /tier would refuse the advisory tier with (routes_sites._tier_refusal), else None. A few indexed reads of the company's file."""
    from . import forecast, plans, verify  # verify imports plans at module load

    sid = site["id"]
    today = datetime.fromtimestamp(now, PKT).date()
    with store.connect() as con:
        native = con.execute("SELECT 1 FROM native_settings WHERE site_id = ? LIMIT 1", (sid,)).fetchone() is not None
        if battery is None:
            battery = con.execute("SELECT 1 FROM assets WHERE site_id = ? AND role = 'battery' LIMIT 1", (sid,)).fetchone() is not None
        # every plan row is kept for good (plans.prune trims steps and tables only), so the first one made is the site's first plan
        first_plan = con.execute("SELECT belief_time FROM plans WHERE site_id = ? AND status IN ('active', 'superseded') ORDER BY belief_time LIMIT 1",
                                 (sid,)).fetchone()
        summary = (plan or {}).get("summary")
        if summary is None and first_plan is not None:
            r = con.execute("SELECT summary FROM plans WHERE site_id = ? AND status IN ('active', 'superseded') ORDER BY generated_at DESC LIMIT 1",
                            (sid,)).fetchone()
            summary = json.loads(r["summary"] or "{}") if r else {}
        pilot = con.execute("SELECT started FROM pilots WHERE site_id = ?", (sid,)).fetchone()
        started = bool(pilot is not None and pilot["started"])
        activated = None
        if first_plan is None and site.get("status") == "active":
            # when the site was last activated (its audit line), else when it was added, as the Data alert's wait for a first reading counts it
            r = con.execute("SELECT at FROM audit WHERE action = 'activated a site' AND target = ? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
            activated = _ts(r["at"]) if r else _ts(site.get("created"))
        made = con.execute("SELECT 1 FROM reports WHERE site_id = ? AND kind = 'monthly_savings' AND period = ? LIMIT 1",
                           (sid, _month_before(today).strftime("%Y-%m"))).fetchone() is not None
    stage = (summary or {}).get("stage")
    tier = site.get("tier") or "monitor"
    return {"active": site.get("status") == "active", "setup_step": setup_step, "native": native, "battery": battery,
            "source_kind": (((site.get("data") or {}).get("data_source")) or {}).get("kind"), "tier": tier, "tier_block": tier_block,
            "first_ts": first_ts, "last_ts": last_ts, "first_plan_ts": _ts(first_plan["belief_time"]) if first_plan else None,
            "failing": plans.tick_failing(sid) if first_plan is None and site.get("status") == "active" else None, "activated_ts": activated,
            "forecast_stage": stage, "preliminary": bool(first_plan) and tier == "monitor" and forecast.preliminary(sid, stage)["preliminary"],
            "control_key": (control or {}).get("key"), "control_label": (control or {}).get("label") or "", "pilot_started": started,
            "pilot": verify.pilot_progress(sid, today) if started else None, "statement_made": made}


def rights():
    """The reader's permissions as `assemble` takes them: (can, states_only)."""
    held = {p for p in auth.PERMISSIONS if auth.can(p)}
    return (lambda p: p in held), not (held - {"energy_report"})


def build(site: dict, now: int, *, first_ts: int | None, last_ts: int | None, plan: dict | None, control: dict, setup_step: str | None,
          battery: bool | None = None, tier_block: str | None = None) -> dict:
    """The site's lifecycle for the current reader (facts, then assemble)."""
    can, states_only = rights()
    f = facts(site, now, first_ts=first_ts, last_ts=last_ts, plan=plan, control=control, setup_step=setup_step, battery=battery, tier_block=tier_block)
    return assemble(f, now, can, states_only)
