"""Routes of local sign-in (/api/local): the backend as the sign-in service in local mode (the owner's decisions of 24 October 2026, section 1; stage P1).

    POST /api/local/session   {email, password} -> {access_token, token_type, expires_in, expires_at, refresh_token, user}
    POST /api/local/refresh   {refresh_token}   -> the same, with a new refresh token (the one sent is used up); 409 session_renewed when another window
                                                   renewed it a moment ago
    POST /api/local/logout    {refresh_token}   -> {signed_out: true}; the session of that token ends, its access tokens with it, and the session of the
                                                   access token sent, when one is
    GET  /api/local/context                     -> {profile, memberships, pending_invites_for_email}, the shape of Supabase's get_my_context
    POST /api/local/password  {current, new}    -> {changed: true, ...a new session}; every other session ends, and a one-time password is replaced

`expires_at` is in unix seconds. A refresh token travels only in a JSON body, never in a path or a query, which the access log keeps. The access token is
identity.issue_local's (stage B1), signed with GRIDLYTICS_LOCAL_JWT_SECRET, for one hour.

Mounted by api.py through PROGRAM_ROUTES (stage B4a), above the page catch-all and behind the same guard. The session, refresh and logout posts are public
(B4a adds them to the guard's public posts, so the per-address rate limit for public paths counts them); the context and the password need a signed-in
person but no company. For those two the guard checks the bearer token and leaves its identity on `request.state.identity`: these routes read its `user_id`
(the token's `sub`) and its `claims["iat"]`, and ask the registry whether the account may still act.
"""

from __future__ import annotations

import math
import secrets
import time

from fastapi import APIRouter, Body, HTTPException, Request

from . import api as _api
from . import auth, schemas
from . import platform as platformdb

router = APIRouter()

_WRONG = "The email or the password is wrong"
_ENDED = "This session has ended; sign in again"
_dummy_hash: str | None = None


def _session(account: dict, refresh_token: str) -> dict:
    """A session's answer, in the shape of a Supabase session: a one-hour access token and the refresh token that renews it."""
    from . import identity              # stage B1's, imported when a token is made
    started = int(time.time())
    token = identity.issue_local(account["id"], account["email"], account["name"], platformdb.ACCESS_MINUTES, session_id=platformdb.session_id(refresh_token))
    return {"access_token": token, "token_type": "bearer", "expires_in": platformdb.ACCESS_MINUTES * 60,
            "expires_at": started + platformdb.ACCESS_MINUTES * 60, "refresh_token": refresh_token,
            "user": {k: account[k] for k in ("id", "email", "name", "created")}}


def _locked(email: str, address: str) -> None:
    """Five wrong passwords for one account from one address stop that address for 15 minutes from the fifth (auth.MAX_TRIES, auth.LOCK_MINUTES)."""
    wait = platformdb.lock_wait(email, address)
    if wait:
        minutes = math.ceil(wait / 60)
        raise HTTPException(status_code=429, detail=f"Too many attempts from here; wait {minutes} minute{'s' if minutes != 1 else ''}",
                            headers={"Retry-After": str(wait)})


def _person(request: Request) -> dict:
    """The signed-in person's account, from the identity the guard checked; 401 when there is none, or the account is disabled or its sessions were ended
    after the token was made. A person signed in with a one-time password (plan Part 9.3) is let in here: these are the paths that change it."""
    identity = getattr(request.state, "identity", None)
    iat = (getattr(identity, "claims", None) or {}).get("iat")
    # A token without its issue time could never be told from one made before a password change: refused
    found = platformdb.signed_in(getattr(identity, "user_id", None), iat, one_time_ok=True) if identity is not None and iat is not None else None
    if found is None:
        raise HTTPException(status_code=401, detail="Sign in first")
    return found


def _refresh_token(inp: dict, needed: bool) -> str | None:
    token = inp.get("refresh_token")
    if (token is None and needed) or (token is not None and (not isinstance(token, str) or not 1 <= len(token) <= 200)):
        _api._fail("Send the refresh token in the body" + ("" if needed else ", or nothing"))
    return token


@router.post("/api/local/session")
def local_session(request: Request, inp: dict = Body(...)):
    """Sign in with an email and a password. A wrong password, an email with no account and a disabled account get one answer, and take as long. Tries for
    an email with no account are not recorded; five refused for one account from one address stop that address."""
    global _dummy_hash
    email, password = inp.get("email"), inp.get("password")
    # A body without the two texts is refused before any account is looked at, so its answer and its time are the same for every email
    if not isinstance(email, str) or not isinstance(password, str):
        _api._fail("Give your email address and your password")
    email = email.strip().lower()
    address = platformdb.client_address(request)
    _locked(email, address)
    found = platformdb.account_by_email(email) if email else None
    account = platformdb.check_sign_in(email, password) if found else None
    if account is None:
        if found:
            platformdb.record(platformdb.REFUSED, email, address, found["id"])
        else:
            # The same PBKDF2 work as a real check, so that the time of the answer does not say whether the email has an account
            _dummy_hash = _dummy_hash or auth.hash_password(secrets.token_urlsafe(16))
            auth.check_password(password, _dummy_hash)
        raise schemas.Refusal(401, _WRONG, "invalid_credentials")
    platformdb.record(platformdb.SIGNED_IN, email, address, account["id"])
    return _session(account, platformdb.new_refresh(account["id"]))


@router.post("/api/local/refresh")
def local_refresh(inp: dict = Body(...)):
    """A new access token for a refresh token, which is used up and replaced. A token used, revoked or ended, or of an account disabled since, is refused.
    One sent again a moment after its first use is another window of the same session renewing it at once: 409 session_renewed, and the session goes on
    (that window takes the session the other stored, and does not sign out)."""
    try:
        found = platformdb.rotate_refresh(_refresh_token(inp, needed=True))
    except platformdb.Renewed:
        raise schemas.Refusal(409, "This session was renewed a moment ago in another window; use the session it stored", "session_renewed") from None
    if found is None:
        raise schemas.Refusal(401, _ENDED, "session_ended")
    return _session(*found)


@router.post("/api/local/logout")
def local_logout(request: Request, inp: dict | None = Body(None)):
    """Sign out: the session of the refresh token sent ends, and its access tokens with it (audit security-roles-09); so does the session of the access token
    sent, when one is. Without either, or with one already ended, there is nothing to end, and the answer is the same."""
    token = _refresh_token(inp, needed=False) if inp is not None else None
    if token:
        platformdb.end_session(token)
    ident = getattr(request.state, "identity", None)
    if ident is not None:
        platformdb.end_session_id(ident.user_id, (getattr(ident, "claims", None) or {}).get("session_id"))
    return {"signed_out": True}


@router.get("/api/local/context")
def local_context(request: Request):
    """The signed-in person's profile and memberships, each with its company and whether Gridlytics is on for it (get_my_context's shape)."""
    return platformdb.context(_person(request)["id"])


@router.post("/api/local/password")
def local_password(request: Request, inp: dict = Body(...)):
    """The signed-in person changes their own password (review R-129, carried over): the current one is asked for, the new one has the rule of every password
    and differs from it, and every session of the account ends. The answer carries a new session, so this device stays signed in. A wrong current password
    counts as a refused sign-in from the address: a session left open cannot be used to guess the password."""
    me = _person(request)
    if set(inp) - {"current", "new"}:
        _api._fail("Send the current password and the new one")
    current, new = inp.get("current"), inp.get("new")
    if not isinstance(current, str) or not current:
        _api._fail("Give your current password")
    address = platformdb.client_address(request)
    _locked(me["email"], address)
    if platformdb.check_sign_in(me["email"], current) is None:
        platformdb.record(platformdb.REFUSED, me["email"], address, me["id"], {"to": "change the password"})
        _api._fail("The current password is wrong")
    if (bad := platformdb.password_problem(new)):
        _api._fail(bad)
    if new == current:
        _api._fail("The new password must be different from the current one")
    account = platformdb.set_password(me["id"], new)
    platformdb.record("changed their password", me["email"], address, me["id"])
    return {"changed": True, **_session(account, platformdb.new_refresh(me["id"]))}
