"""Forecasts (UI-08): the latest issue per kind with its actuals, the accuracy scores and a manual refresh (an administrator's or an analyst's, saved
through the tick's observed-outage protection)."""
from __future__ import annotations

from fastapi import APIRouter

from . import auth, forecast, plans
from .routes_common import full_or_404, now_ts
from .schemas import Refusal

router = APIRouter()


@router.get("/api/sites/{site_id}/forecasts/scores")
def scores(site_id: str, days: int = 30):
    s = full_or_404(site_id)
    return forecast.scores(site_id, max(1, min(days, 120)), site=s)


@router.get("/api/sites/{site_id}/forecasts/{kind}")
def latest(site_id: str, kind: str):
    s = full_or_404(site_id)
    if kind not in ("pv", "load", "outage"):
        raise Refusal(404, "The forecast kinds are pv, load and outage", "not_found")
    return forecast.page(s, kind)


@router.post("/api/sites/{site_id}/forecasts/refresh")
def refresh(site_id: str):
    auth.need("refresh_forecasts", "Refreshing forecasts")
    s = full_or_404(site_id)
    now = now_ts()
    t0 = forecast.align(now)
    # the tick's rule (DESIGN_LOG rounds 1-2): a same-quarter issue that observed an outage is never replaced by a blind one, so the refresh saves
    # through the same protection as plans.tick
    prior = plans._saved_outage(site_id, t0)
    b = plans._keep_observed_outage(site_id, t0, now, forecast.issue(s, now), prior, True)
    # a kind that does not apply (no PV, an off-grid site's outages) has no stage: null
    return {"belief_time": b["t0"], "stages": {"pv": b["pv"]["stage"], "load": b["load"]["stage"], "outage": b["outage"]["stage"]}}
