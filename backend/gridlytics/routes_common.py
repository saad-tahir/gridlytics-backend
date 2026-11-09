"""Small helpers the feature routes share: the site guard, one-sentence refusals, the money filter, time ranges and the site's carried state changed in
one write transaction."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from . import money, sites, store
from .schemas import Refusal

PKT = timezone(timedelta(hours=5))


def state_change(site_id: str, change) -> tuple[dict, object]:
    """Reads the site's carried state, applies `change` to it and writes what it changes, in one write transaction on the workspace file (BEGIN
    IMMEDIATE): `change(state)` answers (the fields to change, or None for none; a result). sites.put_state reads and then writes with no lock, so two
    requests changing the state at once (an outage event's pending re-solve, a tier sign-off) could each write over the other's; here the read, the
    comparison and the write are one step. The state as it then stands, and the result."""
    with store.write_transaction() as con:
        r = con.execute("SELECT data FROM site_state WHERE site_id = ?", (site_id,)).fetchone()
        cur = json.loads(r["data"]) if r else {}
        changes, result = change(dict(cur))
        if changes:
            cur.update(changes)
            con.execute("INSERT INTO site_state (site_id, data, updated) VALUES (?,?,?) ON CONFLICT(site_id) DO UPDATE SET data = excluded.data, "
                        "updated = excluded.updated", (site_id, json.dumps(cur, default=str), store._now()))
    return cur, result


def put_state(site_id: str, **changes) -> dict:
    """sites.put_state's change, made in one write transaction (`state_change`); the state as it now stands."""
    return state_change(site_id, lambda cur: (changes, None))[0]


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
    """Every answer that may carry money goes through the role filter (viewers get no rupees, only administrators and analysts see the fee; an API key gets
    no money field at all, whatever role it was made with: money.for_reader). The API's guard runs the same filter on every JSON answer as well
    (api._without_money), so a route that forgets this sends no money either."""
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
