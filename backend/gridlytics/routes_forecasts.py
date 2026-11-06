"""Forecasts (UI-08): the latest issue per kind with its actuals, the accuracy scores and a manual refresh."""
from __future__ import annotations

from fastapi import APIRouter

from . import auth, forecast
from .routes_common import full_or_404, now_ts
from .schemas import Refusal

router = APIRouter()


@router.get("/api/sites/{site_id}/forecasts/scores")
def scores(site_id: str, days: int = 30):
    full_or_404(site_id)
    return forecast.scores(site_id, max(1, min(days, 120)))


@router.get("/api/sites/{site_id}/forecasts/{kind}")
def latest(site_id: str, kind: str):
    s = full_or_404(site_id)
    if kind not in ("pv", "load", "outage"):
        raise Refusal(404, "The forecast kinds are pv, load and outage", "not_found")
    return forecast.page(s, kind)


@router.post("/api/sites/{site_id}/forecasts/refresh")
def refresh(site_id: str):
    auth.need("run_jobs", "Refreshing forecasts")
    s = full_or_404(site_id)
    b = forecast.issue(s, now_ts())
    return {"belief_time": b["t0"], "stages": {"pv": b["pv"]["stage"], "load": b["load"]["stage"], "outage": b["outage"]["stage"]}}
