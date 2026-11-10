"""Reserve adequacy (plan Part 8.3; R8 2.7, the Outages page's reserve KPI; the pilot's reserve_adequacy_pct gate, Part 15.3): the reserve target the plan
in force held at the start of each outage, whether the outage was forecastable, and the share of forecastable outages entered with the battery at or
above its target. The Outages page reads it through `annotate` and `summary`; a report or the pilot's gate reads the same figure through `adequacy`."""
from __future__ import annotations

from . import plans, telemetry

STEP_S = 900
#: points of state of charge an outage may start under its target and still count as met: the Reserve alert's tolerance (alerts.py, reserve_short),
#: for a reading's rounding and the estimator's noise
TOLERANCE_PCT = 2.0
#: the chance of a start in the outage's step from which the plan expected it: the optimiser holds a reserve row from this chance on (optimise.py)
P_START_MIN = 0.01


def has_battery(site: dict) -> bool:
    """A battery on a grid-connected site: an off-grid site has no outage to hold a reserve for."""
    conns = site.get("connections") or []
    on_grid = bool(conns) and (conns[0].get("data") or {}).get("regime") != "off_grid"
    return on_grid and bool((site.get("assets") or {}).get("battery"))


def target_at(site_id: str, start: int) -> dict | None:
    """The reserve the plan held for an outage starting at `start`: {reserve_pct, forecastable}, or None with no plan in force then (or none covering the
    step). The target is the floor at the end of the step before the outage's step, from the plan in force during that step: optimise.py holds the
    reserve on E[t + 1] for an outage starting in step t + 1, so the outage's own step already holds the next step's reserve. The outage was
    forecastable when that plan gave a start in its step a chance of at least P_START_MIN (the step before's p_start) or modelled the step as an
    outage (an announced window it trusted: grid 0)."""
    k = int(start) // STEP_S * STEP_S
    plan = plans.active_at(site_id, k - 1)
    before = own = None
    for s in (plan or {}).get("steps") or []:
        if s.get("t") == k - STEP_S:
            before = s
        elif s.get("t") == k:
            own = s
            break
    if before is None or before.get("reserve_pct") is None:
        return None
    modelled = own is not None and own.get("grid_present") is not None and float(own["grid_present"]) < 0.5
    return {"reserve_pct": float(before["reserve_pct"]), "forecastable": float(before.get("p_start") or 0.0) >= P_START_MIN or modelled}


def annotate(site: dict, log: list[dict]) -> list[dict]:
    """Adds to each outage of `log` (telemetry.outages' rows): `reserve_pct`, the target at its start (None without a battery or a plan then);
    `forecastable`; `reserve_met`, whether its state of charge at the start was at the target or at most TOLERANCE_PCT under it (None unless
    forecastable with a known state of charge); and `battery_minutes`, the minutes the genset did not run, which the battery and the PV carried
    (energy they could not serve is the row's unserved_kwh; None without a battery). Returns the log."""
    bat = has_battery(site)
    for o in log:
        tgt = target_at(site["id"], o["start"]) if bat else None
        o["reserve_pct"] = tgt["reserve_pct"] if tgt else None
        o["forecastable"] = bool(tgt and tgt["forecastable"])
        soc = o.get("soc_start_pct")
        o["reserve_met"] = bool(soc + TOLERANCE_PCT >= o["reserve_pct"]) if o["forecastable"] and soc is not None else None
        o["battery_minutes"] = max(0, int(o["minutes"]) - int(o.get("genset_minutes") or 0)) if bat else None
    return log


def summary(log: list[dict]) -> dict:
    """The reserve KPI of an annotated log: {outages: the forecastable ones with a known state of charge at the start, met, pct (None with none), short:
    those entered under their target, [{start, soc_start_pct, reserve_pct}]}."""
    judged = [o for o in log if o.get("reserve_met") is not None]
    met = sum(1 for o in judged if o["reserve_met"])
    return {"outages": len(judged), "met": met, "pct": round(100.0 * met / len(judged), 1) if judged else None,
            "short": [{"start": o["start"], "soc_start_pct": o["soc_start_pct"], "reserve_pct": o["reserve_pct"]} for o in judged if not o["reserve_met"]]}


def adequacy(site: dict, t0: int, t1: int) -> dict:
    """Reserve adequacy over [t0, t1) (plan Part 8.3): the `summary` of the period's outages, as the Outages page reads them. For the pilot's
    reserve_adequacy_pct gate (Part 15.3) and the reports."""
    return summary(annotate(site, telemetry.outages(site["id"], t0, t1)))
