"""Routes of the access requests (UX backend asks K2 and K3; plan B4, B20.4, B21.2): the contact page's public send, and the founder console's list and
change (access_requests.py holds the rules and the store).

    POST  /api/public/access-requests                {topic, name, email, company?, phone?, city?, sites?, equipment?, inverter?, disco?, message?, website?}
                                                      -> 202 with no body, whatever the email; 429 with Retry-After past the rate
    GET   /api/founder/access-requests?state=&offset= -> {requests, counts}
    PATCH /api/founder/access-requests/{request_id}   {state?, note?}  -> {request}

The send needs no sign-in (api.PUBLIC_POSTS) and takes at most 8 KB (api.BODY_CAPS). It makes a request to the founder, never an account, and never
says whether an email has one. The founder's two routes check the founder's own session (routes_founder._session) before they read a field, as every
/api/founder route does, and each change of a request is written to the founder's audit as it is made, before the answer is sent.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import Response

from . import access_requests, auth, routes_founder, schemas
from . import api as _api
from . import platform as platformdb

router = APIRouter()
log = logging.getLogger("gridlytics")
#: A page's offset: ASCII digits only (str.isdigit takes "²", which int() cannot read, and "٣", which it reads as 3: UX K2 check)
_OFFSET = re.compile(r"[0-9]{1,9}")


def _filled(value) -> bool:
    """Whether the hidden field holds anything: a person never sees it, so any value at all is a machine's."""
    return value is not None and not (isinstance(value, str) and not value.strip())


def _limit(address: str) -> None:
    """RATE_LIMIT requests an hour from one address, then 429 with Retry-After and the time a send works again, in Karachi time (plan B4: "Try again
    after 14:40"; the time is absolute, so nothing on the page ticks)."""
    wait = access_requests.wait(address)
    if wait:
        at = access_requests.again(wait)
        when = at.strftime("%H:%M") + ("" if at.date() == access_requests._now().astimezone(access_requests.PKT).date() else " tomorrow")
        raise schemas.Refusal(429, f"Too many requests from this connection; try again after {when}", "too_many_requests", {"Retry-After": str(wait)},
                              {"retry_at": at.isoformat(timespec="seconds")})


def _sender() -> dict | None:
    """Who sent it, when the request carried a person's session (the guard read it, as on any public path): their email, name, company and role there.
    None when signed out."""
    user, company = auth.current.get(), auth.company.get()
    if not user or user.get("api_key"):
        return None
    return {"email": user.get("email") or "", "name": user.get("name") or "", "company": (company or {}).get("name") or "", "role": user.get("role") or ""}


@router.post("/api/public/access-requests", status_code=202)
def access_request_new(request: Request, inp: Any = Body(None)):
    """A request to the founder from the contact page (plan B4): kept as new, answered 202 with no body. The hidden field filled is a machine's: the same
    202, and nothing is kept. A refusal is one sentence the page shows under the form, with what the person typed kept."""
    if not isinstance(inp, dict):
        _api._fail("Send the request as a set of named values: what it is about, your name, your work email and the rest of the form")
    address = platformdb.client_address(request)
    if _filled(inp.get(access_requests.HONEYPOT)):
        _limit(address)                     # counted too, so a machine learns nothing from the rate
        log.info("an access request with the hidden field filled was dropped")
        return Response(status_code=202)
    sent = schemas.check(access_requests.AccessRequestIn, inp)
    _limit(address)
    made = access_requests.add(sent, _sender())
    log.info("access request %s received (topic %s)", made, sent.topic)
    return Response(status_code=202)


@router.get("/api/founder/access-requests", dependencies=[Depends(routes_founder._session)])
def founder_access_requests(state: str = "all", offset: str = "0"):
    """The requests, newest first, in one state or all, a page (access_requests.PAGE) at a time from `offset`, with how many there are in each state."""
    if state not in (*access_requests.STATES, "all"):
        _api._fail("Filter the requests by state: new, contacted, done, declined or all")
    if not _OFFSET.fullmatch(offset):
        _api._fail("The offset is a whole number from 0")
    return access_requests.listing(state, int(offset))


@router.patch("/api/founder/access-requests/{request_id}", dependencies=[Depends(routes_founder._session)])
def founder_access_request_change(request_id: str, inp: Any = Body(None)):
    """Set a request's state (new, contacted, done, declined), the founder's note, or both. A change is written to the founder's audit: the state from
    and to, and whether the note was added, changed or cleared, never its text (access_requests.audit_detail); a change that changes nothing writes
    nothing. The line's target is the requester's email until the request is removed past its keeping (access_requests.prune)."""
    request_id = routes_founder._id(request_id, "access request")
    if not isinstance(inp, dict):
        _api._fail("Send the change as a set of named values: the request's state, its note, or both")
    wanted = schemas.check(access_requests.AccessRequestChange, inp)
    done = access_requests.change(request_id, wanted.state, wanted.note)
    if done is None:
        routes_founder._missing("access request")
    before, after = done
    if detail := access_requests.audit_detail(before, after):
        platformdb.founder_audit(access_requests.CHANGED, after["email"], detail)
    return {"request": after}
