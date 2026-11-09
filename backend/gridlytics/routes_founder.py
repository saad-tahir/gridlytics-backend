"""Routes of the founder console (/api/founder): its own sign-in, the companies, the accounts and memberships, each company's backups and the founder's audit
(the owner's decisions of 24 October 2026, section 4; design E as the decisions keep it; the contract of docs/split/F5.md; stage B6).

    POST  /api/founder/login                         {username, password}  -> {challenge, expires_in, tries}
    POST  /api/founder/login/code                    {challenge, code}     -> {token, expires_at}
    GET   /api/founder/session                                             -> {authenticated, expires_at}
    GET   /api/founder/overview                                            -> the registry's and the files' figures
    GET   /api/founder/companies                                           -> {companies}
    POST  /api/founder/companies                     {name, seats, enabled, owner}  -> 201 {company, owner, membership}
    PATCH /api/founder/companies/{id}                {name?, seats?}       -> {company}
    POST  /api/founder/companies/{id}/access         {enabled, reason?}    -> {company}
    GET   /api/founder/companies/{id}/members                              -> {members}
    GET   /api/founder/companies/{id}/backups                              -> {backups}
    POST  /api/founder/companies/{id}/backups        {}                    -> {made, backups}
    GET   /api/founder/users                                               -> {users}
    POST  /api/founder/users                         {email, name, password, company_id?, company_role?}  -> 201 {user}
    POST  /api/founder/users/{id}/password           {password}            -> {user}
    PATCH /api/founder/users/{id}                    {disabled?, name?}    -> {user}
    POST  /api/founder/users/{id}/memberships        {company_id, company_role}  -> 201 {membership}
    PATCH /api/founder/users/{id}/memberships/{cid}  {company_role?, status?}    -> {membership}
    DELETE /api/founder/users/{id}/memberships/{cid}                       -> {removed: true}
    GET   /api/founder/audit?offset=                                       -> {entries}

The guard lets /api/founder through with no token and no company (the founder is no person of a company): every route but the two sign-in steps checks the
founder's own session, the header x-founder-session, before it reads a field of the body (only a body that is not JSON at all is refused first, 422
invalid_body, as on every route), and a refusal of it is the founder's own 401 (founder_session_required, _invalid, _expired). Until the console is set up
every route answers 503. Every change, and every sign-in, is written to the founder's audit in platform.db as soon as it is made, before anything is read
back; no answer and no line of the audit holds a password or its hash, and no answer names a company's file (a server's path is not the browser's).
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager

from fastapi import APIRouter, Body, Depends, Request

from . import api as _api
from . import founder, schemas, store_founder
from . import platform as platformdb

router = APIRouter()
log = logging.getLogger("gridlytics")
# A session secret too short to keep the founder's tokens from being forged stops the API as it loads this module, before it answers anyone (security-roles-12)
founder.check_startup()

#: The founder's audit, a page at a time (the console's AUDIT_PAGE)
AUDIT_PAGE = 300
_OWNER_NEEDED = "A company is made with its owner: choose an account, or give the new owner's email, name and first password"


def _refusal(refused: founder.Refused) -> schemas.Refusal:
    return schemas.Refusal(refused.status, refused.detail, refused.code, {"Retry-After": str(refused.retry_after)} if refused.retry_after else None)


def _set_up() -> None:
    """The sign-in steps' check, before a field of their body is read: 503 until the console is set up."""
    try:
        founder.need_set_up()
    except founder.Refused as refused:
        raise _refusal(refused) from None


def _session(request: Request) -> int:
    """The founder's session from x-founder-session, checked before the route reads a field of its body: when it ends (unix seconds)."""
    try:
        return founder.verify(request.headers.get("x-founder-session"))
    except founder.Refused as refused:
        raise _refusal(refused) from None


@contextmanager
def _said():
    """What the founder's change ran into, as a refusal of the console: 404, 409, 422, or 503 when the sign-in project cannot be asked."""
    try:
        yield
    except store_founder.NotFound as exc:
        raise schemas.Refusal(404, str(exc)) from None
    except store_founder.Conflict as exc:
        raise schemas.Refusal(409, str(exc)) from None
    except store_founder.NotSetUp as exc:
        raise schemas.Refusal(503, str(exc), "unavailable") from None
    except store_founder.Unreachable as exc:
        log.warning("the founder console could not reach the sign-in project: %s", exc)
        raise schemas.Refusal(503, "The sign-in project could not be reached; try again in a minute", "identity_unavailable") from None
    except ValueError as exc:
        raise schemas.Refusal(422, str(exc)) from None


def _object(inp, allowed: set[str], what: str) -> dict:
    if not isinstance(inp, dict):
        _api._fail(f"{what} is sent as a set of named values")
    extra = sorted(set(inp) - allowed)
    if extra:
        _api._fail(f"Unknown field '{extra[0]}': {what} holds {', '.join(sorted(allowed))}")
    return inp


def _id(value, what: str) -> str:
    """A company's or an account's id as the registry and the project keep it; 404 for anything that is not one (it names nothing)."""
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) and len(value) <= 64 else _missing(what)
    except ValueError:
        return _missing(what)


def _missing(what: str):
    raise schemas.Refusal(404, f"No such {what}")


def _text(value, words: str, most: int = 80) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= most:
        _api._fail(words)
    return value.strip()


def _email(value, words: str) -> str:
    try:
        return platformdb._email(value)
    except ValueError:
        _api._fail(words)


def _password(value, whose: str | None = None) -> str:
    problem = platformdb.password_problem(value) if isinstance(value, str) else "A password needs at least 10 characters"
    if problem:
        _api._fail(problem.replace("A password", whose, 1) if whose else problem)
    return value


def _seats(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000:
        _api._fail("Seats are a whole number from 0 to 100,000")
    return value


def _role(value) -> str:
    if value not in platformdb.COMPANY_ROLES:
        _api._fail(f"A company role is one of {', '.join(platformdb.COMPANY_ROLES[:-1])} or {platformdb.COMPANY_ROLES[-1]}")
    return value


def _yes_no(value, words: str) -> bool:
    if not isinstance(value, bool):
        _api._fail(words)
    return value


# ------------------------------------------------------------------------------------------------ the sign-in and the session

@router.post("/api/founder/login", dependencies=[Depends(_set_up)])
def founder_login(request: Request, inp: dict = Body(...)):
    """Step 1: the username and the password, sent this once. The answer is the challenge the code step takes (founder.login)."""
    _object(inp, {"username", "password"}, "The founder's sign-in")
    username, password = inp.get("username"), inp.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or len(username) > 200 or len(password) > 1000:
        _api._fail("Give the founder's username and password")
    try:
        return founder.login(username, password, platformdb.client_address(request))
    except founder.Refused as refused:
        raise _refusal(refused) from None


@router.post("/api/founder/login/code", dependencies=[Depends(_set_up)])
def founder_login_code(request: Request, inp: dict = Body(...)):
    """Step 2: the challenge and the six-digit code. The answer is the session: {token, expires_at}."""
    _object(inp, {"challenge", "code"}, "The founder's code")
    try:
        return founder.code(inp.get("challenge"), inp.get("code"), platformdb.client_address(request))
    except founder.Refused as refused:
        raise _refusal(refused) from None


@router.get("/api/founder/session")
def founder_session(ends: int = Depends(_session)):
    return {"authenticated": True, "expires_at": ends}


# ------------------------------------------------------------------------------------------------ the overview and the companies

@router.get("/api/founder/overview", dependencies=[Depends(_session)])
def founder_overview():
    """The registry's companies, accounts, memberships and seats, and what the files of the companies Gridlytics is on for hold (store_founder.overview)."""
    with _said():
        return store_founder.overview()


@router.get("/api/founder/companies", dependencies=[Depends(_session)])
def founder_companies():
    with _said():
        return {"companies": store_founder.companies()}


def _owner(owner) -> dict:
    """R-405: the owner a company is made with, named: an account that exists, by its id, or a new one with its email, name and first password."""
    if not isinstance(owner, dict) or not owner:
        _api._fail(_OWNER_NEEDED)
    if "user_id" in owner:
        if set(owner) != {"user_id"}:
            _api._fail("The owner is an account that exists (its user_id) or a new one (email, name and first password), not both")
        return {"user_id": _id(owner["user_id"], "account")}
    _object(owner, {"email", "name", "password"}, "The new owner")
    return {"email": _email(owner.get("email"), "Give the owner's email address"),
            "name": _text(owner.get("name"), "Give the owner's name, 1 to 80 characters"),
            "password": _password(owner.get("password"), "The owner's first password")}


@router.post("/api/founder/companies", status_code=201, dependencies=[Depends(_session)])
def founder_company_new(inp: dict = Body(...)):
    """A company with its owner (R-405), made together or not at all: the owner's account (an existing one, or a new one with its first password), the
    company with Gridlytics on or off and its seats, and the owner's membership (company role owner, active). Its file is made at its first request."""
    _object(inp, {"name", "seats", "enabled", "owner"}, "A new company")
    name = _text(inp.get("name"), "Give the company a name of 1 to 80 characters")
    seats = _seats(inp.get("seats", platformdb.SEATS))
    enabled = _yes_no(inp.get("enabled", True), "Say whether Gridlytics is on for the company from the start: true or false")
    if "owner" not in inp:
        _api._fail(_OWNER_NEEDED)
    owner = _owner(inp["owner"])
    with _said():
        company, account = store_founder.create_company(name, seats, enabled, owner)
        platformdb.founder_audit("made a company", company["name"], {"seats": seats, "enabled": enabled, "owner": account["email"],
                                                                      "new_owner": "user_id" not in owner})
        return {"company": store_founder.company_view(company["id"]), "owner": store_founder.user(account["id"]),
                "membership": store_founder.membership_view(account["id"], company["id"])}


@router.patch("/api/founder/companies/{company_id}", dependencies=[Depends(_session)])
def founder_company_change(company_id: str, inp: dict = Body(...)):
    """Rename a company, or set its seats (fewer than are taken is allowed: nobody is switched off, and nobody new gets in until a seat is free)."""
    company_id = _id(company_id, "company")
    _object(inp, {"name", "seats"}, "A change of a company")
    if not inp:
        _api._fail("Send the company's new name or its seats")
    name = _text(inp["name"], "Give the company a name of 1 to 80 characters") if "name" in inp else None
    seats = _seats(inp["seats"]) if "seats" in inp else None
    with _said():
        before, after = store_founder.update_company(company_id, name, seats)
        changes = {k: {"from": before[k], "to": after[k]} for k in ("name", "seats") if before[k] != after[k]}
        if changes:
            platformdb.founder_audit("changed a company", after["name"], changes)
        return {"company": store_founder.company_view(company_id)}


@router.post("/api/founder/companies/{company_id}/access", dependencies=[Depends(_session)])
def founder_company_access(company_id: str, inp: dict = Body(...)):
    """Switch Gridlytics on or off for a company (the console's "disable"): its members are refused (403 company_disabled) at their next request, and
    let in again when it is switched on. The reason is the founder's own note while it is off."""
    company_id = _id(company_id, "company")
    _object(inp, {"enabled", "reason"}, "Switching Gridlytics on or off")
    enabled = _yes_no(inp.get("enabled"), "Say whether Gridlytics is on for the company: enabled, true or false")
    reason = inp.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 200:
        _api._fail("The reason is text of at most 200 characters")
    if platformdb.company(company_id) is None:
        _missing("company")
    row = platformdb.update_company(company_id, enabled=enabled, disabled_reason="" if enabled else reason)
    platformdb.founder_audit(f"switched Gridlytics {'on' if enabled else 'off'}", row["name"], {} if enabled else {"reason": row["disabled_reason"]})
    with _said():
        return {"company": store_founder.company_view(company_id)}


@router.get("/api/founder/companies/{company_id}/members", dependencies=[Depends(_session)])
def founder_company_members(company_id: str):
    with _said():
        return {"members": store_founder.company_members(_id(company_id, "company"))}


@router.get("/api/founder/companies/{company_id}/backups", dependencies=[Depends(_session)])
def founder_backups(company_id: str):
    """The company's copies, newest first: daily, manual, before a schema change, before a reset, restore points. Restoring stays a step by hand on the
    server (stop it, scripts/restore_backup.py)."""
    with _said():
        return {"backups": store_founder.company_backups(_id(company_id, "company"))}


@router.post("/api/founder/companies/{company_id}/backups", dependencies=[Depends(_session)])
def founder_back_up(company_id: str, inp: dict | None = Body(None)):
    """A copy of the company's file now (master PS-12); its newest KEEP_MANUAL manual copies are kept (R-100)."""
    _object(inp or {}, set(), "Making a backup")
    company_id = _id(company_id, "company")
    with _said():
        made, listed = store_founder.back_up(company_id)
    if made:
        platformdb.founder_audit("made a backup", platformdb.company(company_id)["name"], {"file": made})
    return {"made": made, "backups": listed}


# ------------------------------------------------------------------------------------------------ accounts and memberships

@router.get("/api/founder/users", dependencies=[Depends(_session)])
def founder_users():
    with _said():
        return {"users": store_founder.users()}


@router.post("/api/founder/users", status_code=201, dependencies=[Depends(_session)])
def founder_user_new(inp: dict = Body(...)):
    """An account with a first password; with a company, it joins it at once, active, with the company role (member unless said), or neither is made."""
    _object(inp, {"email", "name", "password", "company_id", "company_role"}, "A new account")
    email = _email(inp.get("email"), "Give the person's email address")
    name = _text(inp.get("name"), "Give the person's name, 1 to 80 characters")
    password = _password(inp.get("password"))
    company_id = role = None
    if inp.get("company_id") is not None:
        if not isinstance(inp["company_id"], str):
            _api._fail("Give the company by its id")
        company_id = _id(inp["company_id"], "company")
        role = _role(inp.get("company_role", "member"))
    elif inp.get("company_role") is not None:
        _api._fail("A company role is given with the company it is in (company_id)")
    with _said():
        made = store_founder.create_account(email, name, password, company_id, role)
        platformdb.founder_audit("made an account", made["email"],
                                 {"company": platformdb.company(company_id)["name"], "company_role": role} if company_id else {})
        return {"user": store_founder.user(made["id"])}


@router.post("/api/founder/users/{user_id}/password", dependencies=[Depends(_session)])
def founder_user_password(user_id: str, inp: dict = Body(...)):
    """A new password for an account. In local mode every session of the account ends: its refresh tokens, and the access tokens made before now."""
    user_id = _id(user_id, "account")
    _object(inp, {"password"}, "A new password")
    password = _password(inp.get("password"))
    with _said():
        account = store_founder.set_password(user_id, password)
        platformdb.founder_audit("set a password", account["email"])
        return {"user": store_founder.user(user_id)}


@router.patch("/api/founder/users/{user_id}", dependencies=[Depends(_session)])
def founder_user_change(user_id: str, inp: dict = Body(...)):
    """Disable an account (its sessions end at once, and a token made before is refused at its next request; in supabase mode a ban) or enable it; rename
    it."""
    user_id = _id(user_id, "account")
    _object(inp, {"disabled", "name"}, "A change of an account")
    if not inp:
        _api._fail("Send whether the account is disabled, or its new name")
    disabled = _yes_no(inp["disabled"], "Say whether the account is disabled: true or false") if "disabled" in inp else None
    name = _text(inp["name"], "Give the person's name, 1 to 80 characters") if "name" in inp else None
    with _said():
        before, after = store_founder.update_account(user_id, disabled, name)
        changes = {k: {"from": before[k], "to": after[k]} for k in ("disabled", "name") if before[k] != after[k]}
        if changes:
            platformdb.founder_audit("changed an account", after["email"], changes)
        return {"user": store_founder.user(user_id)}


def _membership_body(inp: dict) -> tuple[str | None, str | None]:
    role = _role(inp["company_role"]) if "company_role" in inp else None
    status = inp.get("status")
    if "status" in inp and status not in platformdb.STATUSES:
        _api._fail(f"A membership's status is one of {', '.join(platformdb.STATUSES[:-1])} or {platformdb.STATUSES[-1]}")
    return role, status


@router.post("/api/founder/users/{user_id}/memberships", status_code=201, dependencies=[Depends(_session)])
def founder_membership_new(user_id: str, inp: dict = Body(...)):
    """The account joins a company, active, with a company role. A member row switched off in the company's file when the membership went is given back to
    the seat rule."""
    user_id = _id(user_id, "account")
    _object(inp, {"company_id", "company_role"}, "A membership")
    if not isinstance(inp.get("company_id"), str):
        _api._fail("Give the company by its id")
    if "company_role" not in inp:
        _api._fail("Give the company role: owner, admin or member")
    role, _status = _membership_body(inp)
    company_id = _id(inp["company_id"], "company")
    with _said():
        account = store_founder.add_membership(user_id, company_id, role)
        platformdb.founder_audit("added a membership", account["email"], {"company": platformdb.company(company_id)["name"], "company_role": role})
        store_founder.follow_membership(company_id, user_id, "active")
        return {"membership": store_founder.membership_view(user_id, company_id)}


@router.patch("/api/founder/users/{user_id}/memberships/{company_id}", dependencies=[Depends(_session)])
def founder_membership_change(user_id: str, company_id: str, inp: dict = Body(...)):
    """A membership's company role or status. A company keeps an active owner (409 otherwise); two owners are allowed for a hand-over (in supabase mode the
    project keeps one, so making a member the owner makes the owner before an admin)."""
    user_id, company_id = _id(user_id, "account"), _id(company_id, "company")
    _object(inp, {"company_role", "status"}, "A change of a membership")
    if not inp:
        _api._fail("Send the membership's company role or its status")
    role, status = _membership_body(inp)
    with _said():
        account, before, after = store_founder.change_membership(user_id, company_id, role, status)
        changes = {"company": platformdb.company(company_id)["name"],
                   **{k: {"from": before[w], "to": after[w]} for k, w in (("company_role", "role"), ("status", "status")) if before[w] != after[w]}}
        platformdb.founder_audit("changed a membership", account["email"], changes)
        if status is not None:
            # also when the status was already this one: a member row a failed follow-up left behind is set right by sending the status again
            store_founder.follow_membership(company_id, user_id, status)
        return {"membership": store_founder.membership_view(user_id, company_id)}


@router.delete("/api/founder/users/{user_id}/memberships/{company_id}", dependencies=[Depends(_session)])
def founder_membership_remove(user_id: str, company_id: str):
    """The account leaves the company; its member row in the company's file is switched off, so its seat is free at once."""
    user_id, company_id = _id(user_id, "account"), _id(company_id, "company")
    with _said():
        account, before = store_founder.remove_membership(user_id, company_id)
        platformdb.founder_audit("removed a membership", account["email"], {"company": platformdb.company(company_id)["name"], "company_role": before["role"]})
        store_founder.follow_membership(company_id, user_id, "removed")
    return {"removed": True}


# ------------------------------------------------------------------------------------------------ the audit

@router.get("/api/founder/audit", dependencies=[Depends(_session)])
def founder_audit(offset: int = 0):
    """The founder's audit, newest first, AUDIT_PAGE entries from `offset`."""
    if offset < 0:
        _api._fail("The offset is a whole number from 0")
    return {"entries": platformdb.list_founder_audit(AUDIT_PAGE, offset)}
