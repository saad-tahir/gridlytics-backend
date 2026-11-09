"""The plan (UI-09) and the control plane (UI-15): the current plan with its reasons, versions, re-solve, apply, compare, hold and release."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone

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


class ResumeFields(Strict):
    """POST /control/resume reads no field."""


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
                   "pause": ("resume_control", "Pausing control", TimedFields), "resume": (None, "Resuming control", ResumeFields)}
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
    return out({"status": r["status"], "plan_id": r.get("plan_id"), "reason": r.get("reason"), "solve_seconds": r.get("solve_seconds")})


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
        return plans.mark_applied(site_id, f.plan_id, f.day or today().isoformat(), auth.actor(), f.method, f.photo)
    except KeyError:
        raise bad(ValueError("No such plan")) from None
    except ValueError as exc:
        raise bad(exc) from None


@router.get("/api/sites/{site_id}/plan/compare")
def plan_compare(site_id: str):
    """The active plan against the inverter's native settings, both replayed through the twin on the plan's own forecast for its horizon."""
    s = full_or_404(site_id)
    p = plans.current(site_id)
    if p is None or not (s.get("assets") or {}).get("battery"):
        return {"compare": None}
    steps = p["steps"]
    ts = np.repeat([st["t"] for st in steps], 15) + np.tile(np.arange(15) * 60, len(steps))
    rep = lambda k: np.repeat([st[k] for st in steps], 15).astype(float)
    series = {"ts": ts, "pv": rep("pv_forecast_kw"), "load": rep("load_kw"), "crit": rep("critical_kw"), "grid": rep("grid_present"), "temp": np.full(len(ts), 30.0)}
    plant = twin.plant_of(s)
    soc0 = (steps[0]["soc_start_pct"] or 50) / 100
    arms = {}
    for name, pol in (("native", twin.native_policy(s, ts)), ("plan", twin.slots_policy([x for d in p["slots"]["days"] for x in d["slots"]], ts))):
        sim = twin.simulate(series, pol, plant, soc0)
        c_imp = rep("c_imp")
        c_exp = rep("c_exp")
        arms[name] = {"import_kwh": round(float(sim["imp"].sum() / 60), 1), "peak_import_kwh": round(float(sim["imp"][rep("peak") > 0.5].sum() / 60), 1),
                      "energy_rs": round(float((c_imp * sim["imp"] - c_exp * sim["exp"]).sum() / 60), 0), "litres": round(float(sim["fuel"].sum()), 1),
                      "unserved_kwh": round(float(sim["unserved"].sum() / 60), 1), "min_soc_pct": round(float(sim["soc"].min() * 100), 1),
                      "soc": [round(float(x) * 100, 1) for x in sim["soc"][::15]]}
    return out({"compare": arms, "t": [st["t"] for st in steps]})


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
    return out({"site": {k: s[k] for k in ("id", "name", "tier", "tier_capable", "tier_allowed")}, "control": control_state(s, telemetry.latest(site_id), now_ts()),
                "brand": brand, "grammar": delivery.BRANDS.get(brand, delivery.BRANDS["other"]), "compiled": comp,
                "vendor_view": [delivery.vendor_view(comp, i) for i in range(len((comp or {}).get("days", [])))] if comp else [],
                "plan_id": (p or {}).get("id"), "applied": plans.applied_rows(site_id), "overrides": sites.overrides(site_id),
                "budget": {"table_writes_per_day": plans.table_budget(s), "used_today": plans.writes_used(site_id, today(), s)},
                "native": sites.native_latest(site_id), "fallback": (comp or {}).get("fallback"),
                "issued": issued, "reissue": plans.reissue_held(site_id, (p or {}).get("id"), today()) if issued is not None else None})


@router.post("/api/sites/{site_id}/control/{action}")
def control_action(site_id: str, action: str, inp: dict = Body(default={})):
    """Hold, release, pause and resume. The action, then the permission, then the body are checked (a 404, a 403, then a 422 in one sentence)
    before anything is written. Each takes effect at once: the plan is re-solved now (a resume only when it cleared something), so a hold is in the
    plan and in the edge's signed message straight away, and under a pause or a release the tick holds its plan and the plan of before stops being
    active. The answer is the control answer with `resolve`, how that re-solve went; a failed one is said, the action stands."""
    site_or_404(site_id)
    if action not in CONTROL_ACTIONS:
        raise Refusal(404, "The control actions are hold, release, pause and resume", "not_found")
    perm, doing, model = CONTROL_ACTIONS[action]
    if action == "resume":
        kinds = {o["kind"] for o in sites.overrides(site_id) if o["kind"] in RESUMED_KINDS}
        if "pause_control" in kinds:
            auth.need("resume_control", "Resuming control after a pause")
        auth.need("hold_release", doing)
    else:
        auth.need(perm, doing)
    f = check(model, inp)
    who = auth.actor()
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


#: what a failed re-solve names as its cause
_AFTER = {"hold": "the hold", "release": "the release", "pause": "the pause", "resume": "resuming"}


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
    _issue(site_id, force=action if action in ("hold", "resume") else None)
    return {"status": r["status"], "plan_id": r.get("plan_id"), "reason": r.get("reason")}


_ = verify
