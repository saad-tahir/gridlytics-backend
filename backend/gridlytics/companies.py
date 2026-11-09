"""From a request's access token to the company it works in, that company's own database and the person's role there (the owner's decisions of 24 October
2026, sections 2, 3 and 5; design B.3 to B.5, with a database per company; stage B3 of the sign-in and split).

`resolve(token, wanted)` checks the token (identity.verify), reads the person's context in the shape of Supabase's `get_my_context` (a profile and the
memberships, each with its company {id, name, verified}), chooses the company (`wanted` is the X-Gridlytics-Company header, a uuid), and finds or makes the
person's member row in that company's file, which is made, at the current schema, the first time it is opened.

* Local mode (identity.MODE `local`): the context is the registry's (platform.py), read on every request: the account through `platform.signed_in(sub, iat)`,
  the memberships and the companies from platform.db. A disabled account, a password changed, a membership suspended or removed, a company switched off: each
  is refused at the next request, for a token made before (addendum item 17). Nothing is kept.
* Supabase mode: the context is the project's `rpc/get_my_context`, asked with the person's own token and the project's public anon key (never the
  service-role key, which is the founder console's). It is kept per person for MEMBERSHIP_TTL seconds. While the project cannot be reached, a context up to
  GRACE_SECONDS old is used, and then the answer is 503 `identity_unavailable`. A company asked for that is not in a kept context is asked for once more (at
  most every REFETCH_SECONDS per person), so a membership the founder has just added works without waiting out the minute.

In both modes only an active membership of a verified company counts, and only for a company that platform.db has switched on (policy `listed`, design K.1):
whether it is on, its seats and its file are the registry's, found by the company's uuid, never the context's. The person's role (decisions, section 3): the
company's owner is an administrator, pinned (source `owner`); everyone else starts as a viewer (source `default`), or with the role a preset of their address
gives (source `set`, used once); a role set in Gridlytics stays. In a workspace linked to its company (link_company.py), the user of before with the
person's address is linked at the first sight (design F.4): their role (source `legacy`) unless the ownership or a preset gives one, their licence and seal,
whether they were switched off, and the rows they owned. Seats are the registry's, against the members not switched off; the owner always has one.

Every refusal is identity.Refused (status, code, sentence), the guard's to answer as `_refusal(...)`: 401 `session_ended`, 403 `not_a_member`, `no_company`,
`company_disabled`, `member_disabled` and `no_seat`, 409 `company_required`, 503 `identity_unavailable`, besides identity.verify's own.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from . import auth, identity, store, store_members
from . import platform as platformdb
from .identity import Refused

log = logging.getLogger("gridlytics")


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


#: How long a person's context from the project is used before it is asked for again (design B.1)
MEMBERSHIP_TTL = _env_int("GRIDLYTICS_MEMBERSHIP_TTL", 60)
#: While the project cannot be reached, a kept context is used up to this age; then 503 (design B.3)
GRACE_SECONDS = 300
#: A context without the company asked for is asked for again at most this often per person (design B.3: a membership just added works)
REFETCH_SECONDS = 10
#: After the project could not be reached, it is not asked again for this long: each request would otherwise wait out the timeout
RETRY_SECONDS = 10
#: The most contexts kept at once; the oldest goes first
MAX_KEPT = 10_000
FETCH_TIMEOUT = 5
#: A context is a few kilobytes; an answer far larger is not one
MAX_CONTEXT_BYTES = 256 * 1024
#: Everyone but the company's owner starts as this (decisions, section 3)
MEMBER_ROLE = "viewer"
#: Why a member's row is switched off when it was made with no seat left; a seat freed lets the member in again
NO_SEAT = "no seat"
#: Why a member's row is switched off when the user of before it was linked to was: only an administrator lets them in
LEGACY_OFF = "switched off before"
_clock = time.monotonic

NOT_A_MEMBER = "You are not a member of that company in Gridlytics; choose one of yours"
NO_COMPANY = "Your account is not set up for Gridlytics. Ask us to add you"
CHOOSE = "You are a member of several companies; choose the one to work in"


@dataclass(frozen=True)
class Who:
    """A request's person in its company: the user `auth.current` holds (store_members.as_user), the company {id, name, verified}, the company's database file
    (for store.set_company) and the checked token."""
    user: dict
    company: dict
    db_path: Path
    identity: identity.Identity


class Unreachable(Exception):
    """The project did not answer with a context: it could not be reached, was too slow, or answered something that is not one."""


def _uuid(value) -> str | None:
    """A company's id as the registry keeps it (lower case, with hyphens), from any spelling of a uuid; None for anything else."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        return None


def _switched_off(names: list[str]) -> Refused:
    named = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return Refused(403, "company_disabled", f"Gridlytics is not switched on for {named}")


def _ended() -> Refused:
    return Refused(401, "session_ended", identity.ENDED)


# ================================================================================================================ supabase mode: the project's context

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _urlopen(request: urllib.request.Request, timeout: float):
    # Not redirected: the context comes from the project's own address or not at all (as identity's keys)
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _fetch_context(token: str) -> dict | None:
    """One POST of the project's rpc/get_my_context with the person's own token: the context, or None (null: a caller the project does not know, an account
    banned or deleted). Refused 401 when the project refuses the token; Unreachable for anything else that is not a context."""
    try:
        request = urllib.request.Request(f"{identity.SUPABASE_URL}/rest/v1/rpc/get_my_context", data=b"{}", method="POST",
                                         headers={"apikey": identity.SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}",
                                                  "Content-Type": "application/json", "Accept": "application/json"})
        with _urlopen(request, FETCH_TIMEOUT) as res:
            body = res.read(MAX_CONTEXT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 401:
            raise Refused(401, "token_invalid", identity.INVALID) from None
        raise Unreachable(f"the project answered {exc.code}") from None
    # URLError and timeouts are OSErrors; a broken answer (IncompleteRead, BadStatusLine) an HTTPException, which is neither; an address the request cannot
    # be made to (no scheme, a port that is not a number: InvalidURL) a ValueError. Each would otherwise answer 500 rather than 503, as for identity's keys
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise Unreachable(str(exc) or type(exc).__name__) from None
    if len(body) > MAX_CONTEXT_BYTES:
        raise Unreachable("the answer is far larger than a context")
    try:
        found = json.loads(body.decode("utf-8"))
    except ValueError:
        raise Unreachable("the answer is not JSON") from None
    if found is not None and not (isinstance(found, dict) and isinstance(found.get("memberships"), list)):
        raise Unreachable("the answer is not a context")
    return found


_kept_lock = threading.Lock()
#: user id -> (when fetched, by _clock; the context or None), oldest first
_kept: OrderedDict[str, tuple[float, dict | None]] = OrderedDict()
#: user id -> when the context was last asked for again for a company it did not have
_refetched: dict[str, float] = {}
_down = {"until": None}


def clear_cache() -> None:
    """Forgets every context kept and that the project was down (the tests)."""
    with _kept_lock:
        _kept.clear()
        _refetched.clear()
        _down["until"] = None


def _keep(user_id: str, at: float, ctx: dict | None) -> None:
    with _kept_lock:
        _kept[user_id] = (at, ctx)
        _kept.move_to_end(user_id)
        while len(_kept) > MAX_KEPT:
            gone, _ = _kept.popitem(last=False)
            _refetched.pop(gone, None)
        _down["until"] = None


def _project_context(ident: identity.Identity, token: str, fresh: bool = False) -> tuple[dict | None, bool]:
    """The person's context from the project, and whether it was fetched by this call. Kept for MEMBERSHIP_TTL; `fresh` asks again though it is kept. While
    the project cannot be reached, a context up to GRACE_SECONDS old is used, else 503."""
    now = _clock()
    with _kept_lock:
        kept = _kept.get(ident.user_id)
        down = _down["until"] is not None and now < _down["until"]
    if kept is not None and not fresh and now - kept[0] < MEMBERSHIP_TTL:
        return kept[1], False
    if not down:
        try:
            ctx = _fetch_context(token)
            # get_my_context answers for auth.uid(), the token's sub: an answer for anyone else is not this person's, whatever went wrong
            if ctx is not None and not (isinstance(ctx.get("profile"), dict) and ctx["profile"].get("id") == ident.user_id):
                raise Unreachable("the context is not the token's person's")
        except Unreachable as exc:
            log.warning("the companies of a sign-in could not be read from %s: %s", identity.SUPABASE_URL, exc)
            with _kept_lock:
                _down["until"] = now + RETRY_SECONDS
        else:
            _keep(ident.user_id, now, ctx)
            return ctx, True
    if kept is not None and now - kept[0] < GRACE_SECONDS:
        return kept[1], False
    raise Refused(503, "identity_unavailable", identity.UNAVAILABLE)


def _may_refetch(user_id: str) -> bool:
    now = _clock()
    with _kept_lock:
        last = _refetched.get(user_id)
        if last is not None and now - last < REFETCH_SECONDS:
            return False
        _refetched[user_id] = now
        return True


# ================================================================================================================ local mode: the registry's context

def _local_context(ident: identity.Identity) -> dict | None:
    """The person's context from platform.db, read now: None when the account is gone or disabled, or the token is older than its last password change."""
    account = platformdb.signed_in(ident.user_id, ident.claims.get("iat"))
    if account is None:
        return None
    return {"profile": {"id": account["id"], "email": account["email"], "name": account["name"]}, "memberships": platformdb.memberships(account["id"])}


# ================================================================================================================ which company

def _counted(ctx: dict) -> list[dict]:
    """The memberships that count: active, of a verified company, with a company role the registry knows; each as {role, company: {id, name}}."""
    out = []
    for m in ctx.get("memberships") or []:
        company = m.get("company") if isinstance(m, dict) else None
        if not isinstance(company, dict) or m.get("status") != "active" or company.get("verified") is not True or m.get("role") not in platformdb.COMPANY_ROLES:
            continue
        company_id = _uuid(company.get("id"))
        if company_id is not None:
            out.append({"role": m["role"], "company": {"id": company_id, "name": company.get("name") if isinstance(company.get("name"), str) else ""}})
    return out


def _choose(ctx: dict, wanted: str | None) -> tuple[dict, dict]:
    """The membership the request works in and the company's registry row: the one `wanted` names, else the person's only company that is switched on."""
    mine = _counted(ctx)
    if wanted is not None:
        company_id = _uuid(wanted)
        m = next((m for m in mine if m["company"]["id"] == company_id), None)
        if m is None:
            raise Refused(403, "not_a_member", NOT_A_MEMBER)
        row = platformdb.company(company_id)
        if row is None or not row["enabled"]:
            raise _switched_off([m["company"]["name"] or (row or {}).get("name") or "this company"])
        return m, row
    on = [(m, row) for m in mine if (row := platformdb.company(m["company"]["id"])) is not None and row["enabled"]]
    if len(on) == 1:
        return on[0]
    if on:
        raise Refused(409, "company_required", CHOOSE)
    if mine:
        raise _switched_off([m["company"]["name"] or "a company" for m in mine])
    raise Refused(403, "no_company", NO_COMPANY)


# ================================================================================================================ the person's member row

#: First sights and seats freed are decided one at a time in this process, so two first requests at once cannot both take the last seat
_seat_lock = threading.Lock()


def _seated(row: dict, owner: bool) -> bool:
    return owner or store_members.count_members() < row["seats"]


def _first_sight(user_id: str, email: str, name: str, company_role: str, row: dict) -> dict:
    """The member row of a person's first request in the company (design B.5, with the decisions' mapping): the owner an administrator, pinned; else the
    role of an unused preset of the address, taken once; else the role of the user of before with the address, in a workspace linked to its company (design
    F.4); else a viewer. With no seat left, the row is made switched off."""
    owner = company_role == "owner"
    preset = None if owner else store_members.preset(email)
    # The user of before is linked whatever gives the role: their licence, seal and rows are theirs, and the owner of the workspace is most often its owner
    legacy = store_members.legacy_user(email)
    if owner:
        role, source = "admin", "owner"
    elif preset is not None:
        role, source = preset["role"], "set"
    elif legacy is not None and legacy["role"] in auth.ROLES:
        role, source = legacy["role"], "legacy"
    else:
        role, source = MEMBER_ROLE, "default"
    # Switched off before, switched off now (the owner is let in by _follow, pinned): by a person, so a seat freed does not let them in
    kept_off = legacy is not None and legacy["disabled"] and not owner
    seated = kept_off or _seated(row, owner)
    made = store_members.add_member(user_id, email=email, name=name, role=role, role_source=source, company_role=company_role,
                                    licence=legacy["licence"] if legacy else "", seal=legacy["seal"] if legacy else "", disabled=kept_off or not seated,
                                    disabled_reason=LEGACY_OFF if kept_off else "" if seated else NO_SEAT, legacy_user_id=legacy["id"] if legacy else None)
    # Two first requests may race: the row made first is the one kept, and the preset is spent only when that row took it
    if preset is not None and made["role_source"] == "set":
        store_members.use_preset(email)
    if legacy is not None and made["legacy_user_id"] == legacy["id"]:
        moved = store_members.adopt_legacy(legacy["id"], user_id)
        actor = store.set_actor(user_id, email)
        try:
            store.audit(name, "linked a user of before", email, {"legacy_id": legacy["id"], "role": made["role"], "role_before": legacy["role"], **moved})
        finally:
            store.reset_actor(actor)
    return made


def _follow(found: dict, email: str, name: str, company_role: str) -> dict:
    """A known member brought in step with the sign-in and the membership: the address, the name and the company role follow them; the owner is an enabled
    administrator whatever the row says; a role derived from the company role (or the owner's, once no longer owner) is derived again; a role set stays."""
    changes = {}
    for key, value in (("email", email), ("name", name), ("company_role", company_role)):
        if found[key] != value:
            changes[key] = value
    if company_role == "owner":
        if (found["role"], found["role_source"]) != ("admin", "owner"):
            changes.update(role="admin", role_source="owner")
        if found["disabled"] or found["disabled_reason"]:
            changes.update(disabled=False, disabled_reason="")
    elif found["role_source"] in ("owner", "default") and (found["role"], found["role_source"]) != (MEMBER_ROLE, "default"):
        changes.update(role=MEMBER_ROLE, role_source="default")
    return store_members.update_member(found["user_id"], **changes) if changes else found


def _member(ident: identity.Identity, profile: dict, m: dict, row: dict) -> dict:
    """The person's member row in the company's file (the current one), found or made, and let in: refused when switched off or without a seat."""
    # The account's address and name, as the sign-in service keeps them now (auth.actor prints the name); the token's when the profile lacks them
    email, name = profile.get("email"), profile.get("name")
    email = email.strip().lower() if isinstance(email, str) and email.strip() else ident.email
    name = " ".join(name.split())[:80] if isinstance(name, str) and name.strip() else ident.name
    found = store_members.member(ident.user_id)
    if found is None or (found["disabled"] and found["disabled_reason"] == NO_SEAT):
        with _seat_lock:
            found = store_members.member(ident.user_id)
            if found is None:
                found = _first_sight(ident.user_id, email, name, m["role"], row)
            elif found["disabled"] and found["disabled_reason"] == NO_SEAT and _seated(row, m["role"] == "owner"):
                found = store_members.update_member(ident.user_id, disabled=False, disabled_reason="")
    found = _follow(found, email, name, m["role"])
    if found["disabled"]:
        company = m["company"]["name"] or row["name"]
        if found["disabled_reason"] == NO_SEAT:
            seats = row["seats"]
            raise Refused(403, "no_seat", f"{company} has {seats} seat{'' if seats == 1 else 's'} in Gridlytics and every one is taken: ask its owner to "
                                          "switch someone off, or ask us for more seats")
        raise Refused(403, "member_disabled", f"Your access to Gridlytics in {company} is switched off; ask its owner")
    store_members.seen(ident.user_id)
    return found


# ================================================================================================================ the request

def resolve(token: str, wanted: str | None = None) -> Who:
    """The person a bearer token names, in the company they work in (`wanted`: the X-Gridlytics-Company header, or None), with that company's database file
    and their role in it; or Refused. The company's file is opened here only, and the store's context is left as it was: the guard sets it from `db_path`."""
    ident = identity.verify(token)
    wanted = wanted.strip() if isinstance(wanted, str) and wanted.strip() else None
    if identity.MODE == "local":
        ctx = _local_context(ident)
        if ctx is None:
            raise _ended()
        m, row = _choose(ctx, wanted)
    else:
        ctx, fetched = _project_context(ident, token)
        if ctx is None:
            raise _ended()
        try:
            m, row = _choose(ctx, wanted)
        except Refused as refusal:
            # A kept context may not have a membership the founder has just added: asked for once more, then refused
            if fetched or refusal.code not in ("not_a_member", "no_company") or (wanted is not None and _uuid(wanted) is None) \
                    or not _may_refetch(ident.user_id):
                raise
            ctx, _ = _project_context(ident, token, fresh=True)
            if ctx is None:
                raise _ended() from None
            m, row = _choose(ctx, wanted)
    path = platformdb.company_file(row["id"])
    if path is None:                                    # gone from the registry since: never the default file (store.set_company refuses None)
        raise _switched_off([m["company"]["name"] or row["name"]])
    with store.use_company(path), store.use_org(store.DEFAULT_ORG):
        member = _member(ident, ctx.get("profile") or {}, m, row)
    return Who(store_members.as_user(member), {"id": row["id"], "name": m["company"]["name"] or row["name"], "verified": True}, path, ident)
