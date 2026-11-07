"""The plan (UI-09) and the control plane (UI-15): the current plan with its reasons, versions, re-solve, apply, compare, hold and release."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, Body

from . import auth, delivery, plans, sites, store, twin, verify
from .routes_common import full_or_404, now_ts, out, site_or_404, today
from .schemas import Refusal
from .routes_common import bad

router = APIRouter()
log = logging.getLogger("gridlytics.plans")
_last_solve: dict[str, float] = {}


@router.get("/api/sites/{site_id}/plan")
def plan_current(site_id: str):
    """The plan in force, or `{plan: null, held}` when none is: `held` says why ({reason, until, plan_id}: control paused or released to native until
    a time, or the latest plan held by its guard), and is null when no plan was made yet (or the last solve failed).
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
            return None if h is None else {"reason": h.get("reason"), "until": h.get("until"), "plan_id": h.get("plan_id")}
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
    return out({"status": r["status"], "plan_id": r.get("plan_id"), "reason": r.get("reason"), "solve_seconds": r.get("solve_seconds")})


@router.post("/api/sites/{site_id}/plan/apply")
def plan_apply(site_id: str, inp: dict = Body(...)):
    auth.need("apply_plan", "Marking a plan as applied")
    site_or_404(site_id)
    try:
        return plans.mark_applied(site_id, str(inp.get("plan_id", "")), str(inp.get("day") or today().isoformat())[:10], auth.actor(),
                                  str(inp.get("method", "installer_keyed")), str(inp.get("photo", "")))
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
    s = full_or_404(site_id)
    p = plans.current(site_id)
    comp = (p or {}).get("slots")
    brand = ((s.get("assets") or {}).get("inverter") or {}).get("brand", "other")
    from .routes_sites import control_state
    from . import telemetry
    with store.connect() as con:
        writes_today = con.execute("SELECT COUNT(*) FROM plan_messages WHERE site_id = ? AND substr(sent_at,1,10) = ?", (site_id, today().isoformat())).fetchone()[0]
    return out({"site": {k: s[k] for k in ("id", "name", "tier", "tier_capable", "tier_allowed")}, "control": control_state(s, telemetry.latest(site_id), now_ts()),
                "brand": brand, "grammar": delivery.BRANDS.get(brand, delivery.BRANDS["other"]), "compiled": comp,
                "vendor_view": [delivery.vendor_view(comp, i) for i in range(len((comp or {}).get("days", [])))] if comp else [],
                "plan_id": (p or {}).get("id"), "applied": plans.applied_rows(site_id), "overrides": sites.overrides(site_id),
                "budget": {"table_writes_per_day": delivery.BRANDS.get(brand, delivery.BRANDS["other"])["table_writes_per_day"], "used_today": writes_today},
                "native": sites.native_latest(site_id), "fallback": (comp or {}).get("fallback")})


@router.post("/api/sites/{site_id}/control/{action}")
def control_action(site_id: str, action: str, inp: dict = Body(default={})):
    site_or_404(site_id)
    hours = float(inp.get("hours", 4) or 4)
    until = (datetime.now(timezone.utc) + timedelta(hours=max(0.25, min(hours, 72)))).isoformat()
    who = auth.actor()
    if action == "hold":
        auth.need("hold_release", "Holding the battery")
        from . import battery
        st = battery.state(sites.full(site_id)) or {}
        sites.add_override(site_id, {"kind": "reserve", "value": float(inp.get("soc_pct") or st.get("soc_pct") or 50), "until": until,
                                     "reason": str(inp.get("reason", "Operator hold"))[:300]}, who)
    elif action == "release":
        auth.need("hold_release", "Releasing to native")
        sites.add_override(site_id, {"kind": "force_native", "until": until, "reason": str(inp.get("reason", "Released to native"))[:300]}, who)
    elif action == "pause":
        auth.need("resume_control", "Pausing control")
        sites.add_override(site_id, {"kind": "pause_control", "until": until, "reason": str(inp.get("reason", "Paused"))[:300]}, who)
    elif action == "resume":
        auth.need("resume_control", "Resuming control")
        cleared = 0
        for o in sites.overrides(site_id):
            if o["kind"] in ("pause_control", "force_native", "reserve"):
                sites.clear_override(site_id, o["id"], who)
                cleared += 1
        if cleared:
            # while paused or released no plan is in force, so the plan and the edge's signed message come back at once, not at the next quarter
            resolve = _resolve_after_resume(site_id)
            res = control(site_id)
            res["resolve"] = resolve
            return res
    else:
        raise Refusal(404, "The control actions are hold, release, pause and resume", "not_found")
    return control(site_id)


def _resolve_after_resume(site_id: str) -> dict:
    """The tick after a resume. A failed tick is reported, not raised: the resume itself stands (the next scheduled tick tries again)."""
    _last_solve[site_id] = time.monotonic()
    try:
        if (sites.get(site_id) or {}).get("demo_link"):    # as the manual re-solve: a demo site's twin first catches up to now
            from . import demo
            demo.advance(site_id)
        r = plans.tick(site_id)
    except Exception as exc:
        log.exception("the re-solve after resuming failed for site %s", site_id)
        return {"status": "failed", "plan_id": None, "reason": f"The re-solve after resuming failed ({type(exc).__name__}); the next scheduled tick tries again"}
    store.audit(auth.actor(), "re-solved the plan", site_id, {"status": r["status"], "after": "resume"})
    return {"status": r["status"], "plan_id": r.get("plan_id"), "reason": r.get("reason")}


_ = verify
