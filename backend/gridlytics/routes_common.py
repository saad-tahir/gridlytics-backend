"""Small helpers the feature routes share: the site guard, one-sentence refusals, the money filter and time ranges."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from . import money, sites
from .schemas import Refusal

PKT = timezone(timedelta(hours=5))


def site_or_404(site_id: str) -> dict:
    s = sites.get(site_id)
    if s is None:
        raise HTTPException(status_code=404, detail="No such site")
    return s


def full_or_404(site_id: str) -> dict:
    s = sites.full(site_id)
    if s is None:
        raise HTTPException(status_code=404, detail="No such site")
    return s


def bad(exc: Exception, code: str = "invalid") -> Refusal:
    return Refusal(422, str(exc).strip() or "That request is not valid", code)


def out(obj):
    """Every answer that may carry money goes through the role filter (viewers get no rupees, only administrators and analysts see the fee)."""
    return money.for_reader(obj)


def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def today() -> date:
    return datetime.now(PKT).date()


def parse_day(s: str | None, default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        raise Refusal(422, "Dates are YYYY-MM-DD", "invalid") from None


def parse_ts(s: str | None, default: int) -> int:
    if not s:
        return default
    try:
        if s.isdigit():
            return int(s)
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int((t if t.tzinfo else t.replace(tzinfo=PKT)).timestamp())
    except ValueError:
        raise Refusal(422, "Times are ISO dates and times or epoch seconds", "invalid") from None
