"""Forecasts (UI-08): the latest issue per kind with its actuals, the accuracy scores and a manual refresh (an administrator's or an analyst's, saved
through the tick's observed-outage protection)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from . import auth, forecast, plans, store
from .routes_common import full_or_404, now_ts
from .schemas import Refusal

router = APIRouter()

PKT = timezone(timedelta(hours=5))
#: a past issue's time as the address gives it (UX backend ask K20): epoch seconds as ASCII digits (the forecast page's own `belief_time`), or an ISO
#: 8601 date and time (a plan's `belief_time`; with no offset it is Karachi time, the time people read). int() reads "٣" as 3 and str.isdigit takes
#: "²", so the digits are matched, at most ten (to the year 2286)
_EPOCH = re.compile(r"[0-9]{1,10}")
_ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?(Z|[+-][0-9]{2}:?[0-9]{2})?")
KIND_WORDS = {"pv": "solar", "load": "load", "outage": "outage"}
AT_WORDS = "The time of a forecast is a date and time such as 2026-11-12T14:15:00+05:00, or seconds since 1970"


def at_time(value: str | None) -> int | None:
    """The `at` of a past issue's address as epoch seconds, None when not given; refused in one sentence when it is neither form or is still to
    come. Sent URL-encoded: a raw "+" of an offset reads as a space, which no form takes."""
    if value is None or value == "":
        return None
    text = value if isinstance(value, str) else ""
    if _EPOCH.fullmatch(text):
        ts = int(text)
    elif _ISO.fullmatch(text):
        try:
            t = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise Refusal(422, AT_WORDS, "invalid") from None
        ts = int((t if t.tzinfo else t.replace(tzinfo=PKT)).timestamp())
    else:
        raise Refusal(422, AT_WORDS, "invalid")
    if ts > now_ts() + 60:
        raise Refusal(422, "The time of a forecast must not be in the future", "invalid")
    return ts


def _when(ts: int) -> str:
    """A time as people read it in a refusal: "14:15 on 12 Nov 2026" (Karachi time)."""
    d = datetime.fromtimestamp(int(ts), PKT)
    return f"{d:%H:%M} on {d.day} {d:%b %Y}"


@router.get("/api/sites/{site_id}/forecasts/scores")
def scores(site_id: str, days: int = 30):
    s = full_or_404(site_id)
    return forecast.scores(site_id, max(1, min(days, 120)), site=s)


@router.get("/api/sites/{site_id}/forecasts/{kind}")
def latest(site_id: str, kind: str, at: str | None = None):
    """The latest issue of a kind, or with `at` the issue held at that time (UX backend ask K20, plan E6 item 1: "the forecast this plan used", a
    plan's `belief_time`): the latest issued at or before it that is still kept. 404 `no_issue` when none is kept from then."""
    s = full_or_404(site_id)
    if kind not in ("pv", "load", "outage"):
        raise Refusal(404, "The forecast kinds are pv, load and outage", "not_found")
    ts = at_time(at)
    got = forecast.page(s, kind, ts)
    if got is None:
        raise Refusal(404, f"No {KIND_WORDS[kind]} forecast issued at or before {_when(ts)} is kept", "no_issue")
    return got


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
    stages = {"pv": b["pv"]["stage"], "load": b["load"]["stage"], "outage": b["outage"]["stage"]}
    # a refresh replaces the forecasts the next plan reads: audited (round 7, audit security-roles-04)
    store.audit(auth.actor(), "refreshed the forecasts", site_id, {"belief_time": b["t0"], "stages": stages})
    return {"belief_time": b["t0"], "stages": stages}
