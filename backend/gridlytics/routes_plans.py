"""The plan (UI-09) and the control plane (UI-15): the current plan with its reasons, versions, re-solve, apply, compare, hold and release."""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, Body
from pydantic import Field, field_validator

from . import auth, delivery, plans, sites, store, twin, verify
from .routes_common import full_or_404, now_ts, out, site_or_404, today
from .schemas import Refusal, Strict, check
from .routes_common import bad

router = APIRouter()
log = logging.getLogger("gridlytics.plans")
_last_solve: dict[str, float] = {}


# ------------------------------------------------------------------------------------------------ request models (plan Part 2.8: untrusted input)

def _number(v, lo: float, hi: float, sentence: str) -> float:
    """A JSON number (or a numeric string, as a lax model takes it) from lo to hi, else the field's one sentence: never a bare float() that turns
    "four" or a list into a 500."""
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise ValueError(sentence)
    try:
        x = float(v)
    except ValueError:
        raise ValueError(sentence) from None
    if not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError(sentence)
    return x


class TimedFields(Strict):
    """POST /control/release and /control/pause: for how many hours (0.25 to 72, the page's own bounds; 4 when left out) and an optional reason
    (a blank one gets the action's default sentence). A value out of bounds is refused, not clamped or cut short."""
    hours: float = 4.0
    reason: str | None = Field(None, max_length=300)

    @field_validator("hours", mode="before")
    @classmethod
    def _hours(cls, v):
        return 4.0 if v is None else _number(v, 0.25, 72, "The hours must be a number from 0.25 to 72")


class HoldFields(TimedFields):
    """POST /control/hold: also the state of charge to hold, a percent from 1 to 100; left out, the present level (a 0 was silently that)."""
    soc_pct: float | None = None

    @field_validator("soc_pct", mode="before")
    @classmethod
    def _soc(cls, v):
        return None if v is None else _number(v, 1, 100, "The state of charge to hold must be a percent from 1 to 100 (leave it out to hold the "
                                                          "present level)")


class ForceChargeFields(TimedFields):
    """POST /control/force_charge: also the state of charge to charge to, a percent from 1 to 100; left out, a full battery (the force_charge
    override's own default, plans.build_problem)."""
    soc_pct: float | None = None

    @field_validator("soc_pct", mode="before")
    @classmethod
    def _soc(cls, v):
        return None if v is None else _number(v, 1, 100, "The state of charge to charge to must be a percent from 1 to 100 (leave it out to charge "
                                                          "to full)")


class ResumeFields(Strict):
    """POST /control/resume reads no field."""


class FallbackTestFields(Strict):
    """POST /control/test_fallback reads no field: the test's release lasts plans.FALLBACK_TEST_MIN."""


class ApplyFields(Strict):
    """POST /plan/apply: the plan, the day (Karachi's today when left out; plans.mark_applied checks it is a date of the plan's table), the method
    and an optional photograph."""
    plan_id: str = Field(min_length=1, max_length=40)
    day: str | None = Field(None, max_length=10)
    method: str = Field("installer_keyed", max_length=40)
    photo: str = ""


#: Each control action: the permission it needs, what a refusal says is being done, and its body. A resume needs hold_release, and resume_control
#: too while a pause is in force: as clearing an override needs the permission of its kind (sites.OVERRIDE_PERMISSION), an operator may end a hold
#: or a release, and a pause is an administrator's (plan Part 10.5)
CONTROL_ACTIONS = {"hold": ("hold_release", "Holding the battery", HoldFields), "release": ("hold_release", "Releasing to native", TimedFields),
                   "pause": ("resume_control", "Pausing control", TimedFields), "resume": (None, "Resuming control", ResumeFields),
                   # round 7 (audit flow-telemetry-plan-control-17): the overrides the solver bounds (plan Part 4.8) but no page placed, each behind
                   # the permission its kind needs (sites.OVERRIDE_PERMISSION: a forced charge is an administrator's)
                   "genset_hold": ("hold_release", "Holding the genset off", TimedFields),
                   "charge_block": ("hold_release", "Blocking battery charging", TimedFields),
                   "force_charge": ("pin_genset", "Forcing a charge", ForceChargeFields),
                   # UI-15's "Test fallback": an administrator's, closed loop only (plan Part 9.3 and 10.5; audit flow-telemetry-plan-control-19)
                   "test_fallback": ("resume_control", "Testing the fallback", FallbackTestFields)}
#: The actions that place an override of their own name: the asset the site needs for it, the reason when none is given, and the refusal without
#: the asset (409 no_equipment)
OVERRIDE_ACTIONS = {"genset_hold": ("genset", "Genset held off", "The site has no genset to hold off"),
                    "charge_block": ("battery", "Charging blocked", "The site has no battery to block charging of"),
                    "force_charge": ("battery", "Forced charge", "The site has no battery to charge")}
#: the overrides a resume clears
RESUMED_KINDS = ("pause_control", "force_native", "reserve")


@router.get("/api/sites/{site_id}/plan")
def plan_current(site_id: str):
    """The plan in force, or `{plan: null, held}` when none is: `held` says why ({reason, until, plan_id}: control paused or released to native until
    a time, a native-block day of the switchback calendar until the block ends, the latest plan held by its guard, or the last plan past its
    valid_until, with `since` its end), and is null when no plan was made yet (or the last solve failed).
    A pause or a release to native in force answers `{plan: null, held}` even while the plan of before it is still stored as active: no plan is in force
    under one (plans.active_at), and the tick stores its plan as held only at the next quarter."""
    site_or_404(site_id)
    rel = plans.control_release(site_id, now_ts())
    if rel is not None:
        held = _held(site_id) or {"reason": rel.get("reason"), "until": rel.get("until"), "plan_id": None}
        return {"plan": None, "held": held}
    p = plans.current(site_id)
    if p is None:
        return {"plan": None, "held": _held(site_id)}
    p["now_next"] = plans.now_and_next(p)
    return out({"plan": p})


def _held(site_id: str) -> dict | None:
    """Why no plan is in force: plans.held_info where the optimiser provides it, else derived here from the pause or release in force (its reason and
    end) and the latest plan row (held: its id, and its guard's reason when no pause or release is in force)."""
    now = now_ts()
    info = getattr(plans, "held_info", None)
    if callable(info):
        try:
            try:
                h = info(site_id, now)
            except TypeError:
                h = info(site_id)
            if h is None:
                return None
            out_h = {"reason": h.get("reason"), "until": h.get("until"), "plan_id": h.get("plan_id")}
            if h.get("since"):
                out_h["since"] = h["since"]                 # an expired plan: when it ran out
            return out_h
        except Exception:
            log.exception("plans.held_info failed for site %s; the held answer is derived here", site_id)
    rel = plans.control_release(site_id, now)
    last = (plans.history(site_id, 1) or [None])[0]
    held_id = last["id"] if last and last.get("status") == "held" else None
    if rel:
        return {"reason": rel["reason"], "until": rel.get("until"), "plan_id": held_id if held_id and last.get("reason") == rel["reason"] else None}
    if held_id:
        return {"reason": last.get("reason") or "held", "until": None, "plan_id": held_id}
    return None


@router.get("/api/sites/{site_id}/plans")
def plan_history(site_id: str, limit: int = 50):
    site_or_404(site_id)
    return out(plans.history(site_id, max(1, min(limit, 200))))


@router.get("/api/sites/{site_id}/plan/past")
def plan_past(site_id: str, hours: int = 24):
    """The plan table's last `hours` (1 to 48; round 7, pages-B-07): each past quarter as the plan in force then planned it and as measured
    (plans.past_steps), money for the roles that see it."""
    site_or_404(site_id)
    if not plans.PAST_HOURS[0] <= hours <= plans.PAST_HOURS[1]:
        raise Refusal(422, "The hours are a whole number from 1 to 48", "invalid")
    return out(plans.past_steps(site_id, now_ts(), hours))


@router.get("/api/sites/{site_id}/plans/{plan_id}")
def plan_get(site_id: str, plan_id: str):
    site_or_404(site_id)
    p = plans.get(site_id, plan_id)
    if p is None:
        raise Refusal(404, "No such plan", "not_found")
    return out({"plan": p})


@router.post("/api/sites/{site_id}/plan/solve")
def plan_solve(site_id: str):
    auth.need("run_jobs", "Re-solving the plan")
    s = site_or_404(site_id)
    if time.monotonic() - _last_solve.get(site_id, -1e9) < 30:
        raise Refusal(429, "The plan was re-solved less than 30 seconds ago", "rate_limited")
    _last_solve[site_id] = time.monotonic()
    if s.get("demo_link"):
        from . import demo
        demo.advance(site_id)
    r = plans.tick(site_id)
    store.audit(auth.actor(), "re-solved the plan", site_id, {"status": r["status"]})
    _issue(site_id)
    return out({"status": r["status"], "plan_id": r.get("plan_id"), "reason": r.get("reason"), "solve_seconds": r.get("solve_seconds"),
                "in_force": r.get("in_force")})


def _issue(site_id: str, force: str | None = None) -> None:
    """After a solve, today's table as the scheduler's tick issues it (plans.issue_tables; a hold or a resume issues it at once). A failure is logged:
    the solve and the action stand."""
    try:
        plans.issue_tables(site_id, force=force)
    except Exception:
        log.exception("issuing the table failed for site %s", site_id)


@router.post("/api/sites/{site_id}/plan/apply")
def plan_apply(site_id: str, inp: dict = Body(...)):
    auth.need("apply_plan", "Marking a plan as applied")
    site_or_404(site_id)
    f = check(ApplyFields, inp)
    try:
        res = plans.mark_applied(site_id, f.plan_id, f.day or today().isoformat(), auth.actor(), f.method, f.photo)
    except KeyError:
        raise bad(ValueError("No such plan")) from None
    except ValueError as exc:
        raise bad(exc) from None
    # a mark for today: the answer says at once when the "it took" card comes (plan Part 9.5); a failed check never undoes the mark
    res["early_check"] = None
    if res["day_local"] == today().isoformat():
        try:
            res["early_check"] = plans.early_check(site_id, now_ts())
        except Exception:
            log.exception("the early check after a mark failed for site %s", site_id)
    return res


#: R8 2.9: the compare drawer needs the native-settings capture and a day of data
COMPARE_MIN_DATA_S = 86400


def _short_price(s: dict, t0: int) -> tuple[float, str]:
    """The price of a kWh of critical load the battery is short of when an outage starts, as the plan prices its reserve (plans.build_problem's
    c_short): the genset's all-in cost at three-quarters load where a genset starts at the site ("genset"), else the site's value of lost load
    ("lost_load")."""
    from . import optimise, tariffs
    d = s.get("data") or {}
    g = (s.get("assets") or {}).get("genset")
    if g and d.get("genset_start_control", "none") != "none":
        fuel, _src = tariffs.fuel_price(g.get("fuel_kind", "hsd"), datetime.fromtimestamp(int(t0), twin.PKT).date())
        c_om = g["c_om_rs_per_kwh"] + tariffs.selfgen_duty(s, datetime.fromtimestamp(int(t0), twin.PKT).date())
        return optimise._gen_marginal({"S": g["rated_kw"], "a": g["a"], "b": g["b"], "c_fuel": fuel, "c_om": c_om, "c_runhr": g["c_runhr_rs"]}), "genset"
    return float(d.get("voll_rs_per_kwh", 500)), "lost_load"


@router.get("/api/sites/{site_id}/plan/compare")
def plan_compare(site_id: str):
    """The active plan against the inverter's native settings, both replayed through the twin on the plan's own forecast for its horizon.

    `{compare: null, reason}` until it can be made (round 7): no_plan, no_battery, no_capture (the native arm replays the captured settings, never
    an assumed self-use table) or too_little_data (under a day of readings; R8 2.9: "Needs the native-settings capture and 1 day of data"). `native`
    names the capture replayed. The replay runs on the plan's grid series, where an outage is only a chance, so each arm also carries what the outage
    reserve is worth, as the plan prices it: `reserve_short_kwh`, the critical load the battery would be short of the reserve the plan keeps at
    each quarter an outage may start, weighted by that chance, and `reserve_short_rs` at `short_rs_per_kwh` (`short_basis`: genset or lost_load)."""
    from . import telemetry
    s = full_or_404(site_id)
    p = plans.current(site_id)
    if p is None or not (s.get("assets") or {}).get("battery"):
        return {"compare": None, "reason": "no_plan" if p is None else "no_battery"}
    if not s.get("native"):
        return {"compare": None, "reason": "no_capture"}
    first = telemetry.first_ts(site_id)
    if first is None or now_ts() - int(first) < COMPARE_MIN_DATA_S:
        return {"compare": None, "reason": "too_little_data"}
    steps = p["steps"]
    ts = np.repeat([st["t"] for st in steps], 15) + np.tile(np.arange(15) * 60, len(steps))
    rep = lambda k: np.repeat([st[k] for st in steps], 15).astype(float)
    series = {"ts": ts, "pv": rep("pv_forecast_kw"), "load": rep("load_kw"), "crit": rep("critical_kw"), "grid": rep("grid_present"), "temp": np.full(len(ts), 30.0)}
    plant = twin.plant_of(s)
    soc0 = (steps[0]["soc_start_pct"] or 50) / 100
    # where an outage may start at a step's end: the chance, and the reserve the plan keeps for it (its floor, per cent)
    risk = np.array([float(st.get("p_start") or 0.0) if st.get("grid_present", 1.0) >= 0.5 else 0.0 for st in steps])
    risk = np.where(risk >= 0.01, risk, 0.0)
    reserve = np.array([float(st.get("reserve_pct") or 0.0) for st in steps])
    price, basis = _short_price(s, steps[0]["t"])
    arms = {}
    for name, pol in (("native", twin.native_policy(s, ts)), ("plan", twin.slots_policy([x for d in p["slots"]["days"] for x in d["slots"]], ts))):
        sim = twin.simulate(series, pol, plant, soc0)
        c_imp = rep("c_imp")
        c_exp = rep("c_exp")
        short = float(np.sum(risk * np.maximum(0.0, reserve - sim["soc"][14::15] * 100.0)) / 100.0 * plant.e_nom)
        arms[name] = {"import_kwh": round(float(sim["imp"].sum() / 60), 1), "peak_import_kwh": round(float(sim["imp"][rep("peak") > 0.5].sum() / 60), 1),
                      "energy_rs": round(float((c_imp * sim["imp"] - c_exp * sim["exp"]).sum() / 60), 0), "litres": round(float(sim["fuel"].sum()), 1),
                      "unserved_kwh": round(float(sim["unserved"].sum() / 60), 1), "min_soc_pct": round(float(sim["soc"].min() * 100), 1),
                      "reserve_short_kwh": round(short, 1), "reserve_short_rs": round(short * price, 0),
                      "soc": [round(float(x) * 100, 1) for x in sim["soc"][::15]]}
    return out({"compare": arms, "t": [st["t"] for st in steps], "native": {"version": s["native"]["version"], "captured_at": s["native"]["captured_at"]},
                "short_rs_per_kwh": round(price, 1), "short_basis": basis})


# ------------------------------------------------------------------------------------------------ control (UI-15)

@router.get("/api/sites/{site_id}/control")
def control(site_id: str, day: int = 0):
    """The control plane. Where the plan reaches the inverter as a Mode A table (the advisory tier and closed loop), `compiled` and `vendor_view` are
    the table issued for each day (Part 7.1 step 7: the nightly table, re-issued only when worth a write), the plan in force's draft where none is
    issued yet; `issued` says per day which plan's table it is and when and why it was issued (null at the monitor tier and at a Mode B site), and
    `reissue` the plan in force's re-issue when it was held back (below the gain, the day's writes used, or the last issue under an hour old).
    `budget.used_today` counts the Karachi day's table writes (plans.writes_used), never the edge's pulls."""
    s = full_or_404(site_id)
    p = plans.current(site_id)
    comp = (p or {}).get("slots")
    issued = None
    if comp and plans.issues_tables(s):
        comp, issued = plans.issued_view(site_id, comp)
    brand = ((s.get("assets") or {}).get("inverter") or {}).get("brand", "other")
    from .routes_sites import control_state
    from . import telemetry
    # per day, the table in the vendor's layout: its rows, the compiled slots each row keys, the values keyed once, and what the brand cannot hold
    tables = [delivery.vendor_table(comp, i) for i in range(len((comp or {}).get("days", [])))]
    return out({"site": {k: s[k] for k in ("id", "name", "tier", "tier_capable", "tier_allowed")}, "control": control_state(s, telemetry.latest(site_id), now_ts()),
                "brand": brand, "grammar": delivery.BRANDS.get(brand, delivery.BRANDS["other"]), "compiled": comp,
                "vendor_view": [t["rows"] for t in tables], "vendor_slots": [t["slots"] for t in tables],
                "vendor_settings": [t["settings"] for t in tables], "vendor_notes": [t["notes"] for t in tables],
                "plan_id": (p or {}).get("id"), "applied": plans.applied_rows(site_id), "overrides": sites.overrides(site_id),
                "budget": {"table_writes_per_day": plans.table_budget(s), "used_today": plans.writes_used(site_id, today(), s)},
                "native": sites.native_latest(site_id), "fallback": (comp or {}).get("fallback"),
                "issued": issued, "reissue": plans.reissue_held(site_id, (p or {}).get("id"), today()) if issued is not None else None,
                # the overrides the site has the equipment for (a genset hold needs a genset, a charging block and a forced charge a battery)
                "placeable": [k for k, (asset, _, _) in OVERRIDE_ACTIONS.items() if (s.get("assets") or {}).get(asset)],
                # the last fallback test with its status (closed loop; audit flow-telemetry-plan-control-19), else null
                "fallback_test": plans.fallback_test(site_id)})


@router.get("/api/sites/{site_id}/control/readback")
def control_readback(site_id: str):
    """The read-back panel of a closed-loop site (plan Part 9.3, UI-15; audit role-operator-11): the last plan sent to the edge, the edge's latest ack
    of it per primitive (asked, written, read back, match, status, codes) and today's fidelity (plans.readback). No money."""
    site_or_404(site_id)
    return plans.readback(site_id)


@router.get("/api/sites/{site_id}/control/writes")
def control_writes(site_id: str, day: str | None = None, primitive: str | None = None):
    """The write log of a Karachi day (today when left out), newest first: the persons' overrides and marks, the tables issued, the sends that changed
    what the inverter runs, the edge's acks and writes per primitive and its events (plans.write_log; audit role-operator-11). `primitive` keeps that
    primitive's rows. No money: a table issue's rupee gain stays on GET /control."""
    site_or_404(site_id)
    try:
        d = date.fromisoformat(day) if day else today()
    except ValueError:
        raise Refusal(422, "The day is a date written YYYY-MM-DD", "invalid") from None
    if primitive is not None and len(primitive) > 60:
        raise Refusal(422, "The primitive is a name of at most 60 characters", "invalid")
    return {"day": d.isoformat(), "rows": plans.write_log(site_id, d, primitive)}


@router.post("/api/sites/{site_id}/control/{action}")
def control_action(site_id: str, action: str, inp: dict = Body(default={})):
    """Hold, release, pause and resume, and the genset hold, the charging block and the forced charge. The action, then the permission, then the body
    are checked (a 404, a 403, then a 422 in one sentence), then that the site has the equipment the override bounds (a 409), before anything is
    written. Each takes effect at once: the plan is re-solved now (a resume only when it cleared something), so a hold is in the
    plan and in the edge's signed message straight away, and under a pause or a release the tick holds its plan and the plan of before stops being
    active. The answer is the control answer with `resolve`, how that re-solve went; a failed one is said, the action stands."""
    site_or_404(site_id)
    if action not in CONTROL_ACTIONS:
        raise Refusal(404, "The control actions are hold, release, pause, resume, genset_hold, charge_block, force_charge and test_fallback",
                      "not_found")
    perm, doing, model = CONTROL_ACTIONS[action]
    if action == "resume":
        kinds = {o["kind"] for o in sites.overrides(site_id) if o["kind"] in RESUMED_KINDS}
        if "pause_control" in kinds:
            auth.need("resume_control", "Resuming control after a pause")
        auth.need("hold_release", doing)
    else:
        auth.need(perm, doing)
    f = check(model, inp)
    if action in OVERRIDE_ACTIONS and not (sites.assets(site_id) or {}).get(OVERRIDE_ACTIONS[action][0]):
        raise Refusal(409, OVERRIDE_ACTIONS[action][2], "no_equipment")
    who = auth.actor()
    if action == "test_fallback":
        return _test_fallback(site_id, who)
    if action == "resume":
        cleared = 0
        for o in sites.overrides(site_id):
            if o["kind"] in RESUMED_KINDS:
                sites.clear_override(site_id, o["id"], who)
                cleared += 1
        if not cleared:
            return control(site_id)
    else:
        until = (datetime.now(timezone.utc) + timedelta(hours=f.hours)).isoformat()
        if action == "hold":
            from . import battery
            st = battery.state(sites.full(site_id)) or {}
            fields = {"kind": "reserve", "value": float(f.soc_pct or st.get("soc_pct") or 50), "until": until, "reason": f.reason or "Operator hold"}
        elif action == "release":
            fields = {"kind": "force_native", "until": until, "reason": f.reason or "Released to native"}
        elif action in OVERRIDE_ACTIONS:
            fields = {"kind": action, "until": until, "reason": f.reason or OVERRIDE_ACTIONS[action][1]}
            if action == "force_charge":
                fields["value"] = float(f.soc_pct or 100)
        else:
            fields = {"kind": "pause_control", "until": until, "reason": f.reason or "Paused"}
        try:
            sites.add_override(site_id, fields, who)
        except ValueError as exc:
            raise bad(exc) from None
    resolve = _resolve_now(site_id, action)
    res = control(site_id)
    res["resolve"] = resolve
    return res


def _test_fallback(site_id: str, who: str) -> dict:
    """POST /control/test_fallback (plan Part 9.3 UI-15: it releases and verifies the restore; audit flow-telemetry-plan-control-19): at a closed-loop
    site whose edge was heard from in the last 5 minutes, with no test running and no release in force (each a 409 in one sentence), a release to
    native for plans.FALLBACK_TEST_MIN, re-solved at once so the edge is sent it; the edge's heartbeats then say whether it handed the inverter to its
    own settings and took the plan again after (plans.fallback_test, GET /control's `fallback_test`)."""
    if (sites.get(site_id) or {}).get("tier") != "closed_loop":
        raise Refusal(409, "The fallback is tested at a closed-loop site: below it no edge writes to the inverter", "not_closed_loop")
    seen = sites.state(site_id).get("edge_last_seen")
    if not seen or now_ts() - int(seen) >= plans.HEARTBEAT_FRESH_S:
        raise Refusal(409, "The edge has not been heard from in the last 5 minutes, so the fallback cannot be tested", "edge_offline")
    held = plans.fallback_test(site_id)
    if held and held["status"] in ("running", "released", "restoring"):
        raise Refusal(409, "A fallback test is already running", "test_running")
    if plans.control_release(site_id) is not None:
        raise Refusal(409, "The inverter already runs its own settings (a pause, a release or a native block), so there is nothing to test",
                      "already_native")
    plans.start_fallback_test(site_id, who)
    resolve = _resolve_now(site_id, "test_fallback")
    res = control(site_id)
    res["resolve"] = resolve
    return res


#: what a failed re-solve names as its cause
_AFTER = {"hold": "the hold", "release": "the release", "pause": "the pause", "resume": "resuming", "genset_hold": "the genset hold",
          "charge_block": "the charging block", "force_charge": "the forced charge", "test_fallback": "the fallback test"}


def _resolve_now(site_id: str, action: str) -> dict:
    """The tick after a control action, so it takes effect at once rather than at the next quarter. A failed tick is reported, not raised: the
    action itself stands (the next scheduled tick tries again)."""
    _last_solve[site_id] = time.monotonic()
    try:
        if (sites.get(site_id) or {}).get("demo_link"):    # as the manual re-solve: a demo site's twin first catches up to now
            from . import demo
            demo.advance(site_id)
        r = plans.tick(site_id)
    except Exception as exc:
        log.exception("the re-solve after %s failed for site %s", action, site_id)
        return {"status": "failed", "plan_id": None,
                "reason": f"The re-solve after {_AFTER[action]} failed ({type(exc).__name__}); the next scheduled tick tries again"}
    store.audit(auth.actor(), "re-solved the plan", site_id, {"status": r["status"], "after": action})
    # a hold of any kind (the battery's, the genset's, a charging block, a forced charge) and a resume issue today's table at once
    _issue(site_id, force="hold" if action == "hold" or action in OVERRIDE_ACTIONS else "resume" if action == "resume" else None)
    reason = r.get("reason")
    if r["status"] == "held" and not reason:
        # why the tick held its plan (it answers that only in the plan's row): the pause or release in force, else the wrong-plan guard's rule, and
        # then the plan of before stays in force (a hold the site can keep only by shedding load with the grid up, say)
        rel = plans.control_release(site_id)
        reason = rel["reason"] if rel else (r.get("summary") or {}).get("guard")
    # the plan in force after it (null: none), as POST /plan/solve answers it, so the page says "the plan of before stays in force" only when one
    # does; a failed solve's reason is a code (plans.failure_code), as there
    return {"status": r["status"], "plan_id": r.get("plan_id"), "reason": reason, "in_force": r.get("in_force")}


_ = verify
