"""Roles, passwords and the API's limits (master PS-01, PS-03, PS-04, PS-07; the role table of master 5.5).

Until the first account is made the workspace is open on this machine, as in v0.1; from then on every API call needs a token of the sign-in service or an
API key (the owner's decisions of 24 October 2026, section 1). Passwords are kept as PBKDF2-SHA256 hashes with a salt (local sign-in's accounts, platform.py).
The old sign-in's cookie sessions, its first administrator, its second factor, its invitations and its password resets were retired in stage B8 of the
sign-in and split (design B.8); their tables stay in each database, read only to link a user of before (design F.4).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from contextvars import ContextVar

from fastapi import HTTPException

ROLES = {"admin": "Administrator", "operator": "Operator", "analyst": "Analyst", "viewer": "Viewer"}

#: Who may do what (plan Part 10.5). A viewer reads energy figures only: every money field is stripped from its answers by name (`money.for_reader`, which
#: the API's guard runs on every JSON answer: api._without_money).
PERMISSIONS = {
    "manage_members": {"admin"},
    "manage_users": {"admin"},
    "edit_workspace": {"admin"},
    "edit_money_settings": {"admin"},
    "edit_sites": {"admin", "operator"},
    "edit_tariffs": {"admin", "analyst"},
    "approve_tariff": {"admin"},
    "change_tier": {"admin"},
    "hold_release": {"admin", "operator"},
    "resume_control": {"admin"},
    "pin_genset": {"admin"},
    "apply_plan": {"admin", "operator"},
    "ack_alerts": {"admin", "operator"},
    "upload_data": {"admin", "operator"},
    "export": {"admin", "analyst"},
    "see_costs": {"admin", "operator", "analyst"},
    "see_fee": {"admin", "analyst"},
    #: the money settings (the value of lost load, the battery's replacement cost and the wear price made from it, the genset's start, run-hour and
    #: upkeep costs): plan 9.4 gives the operator "costs but not money settings or the fee", and UI-10 the battery's economics card to owner and analyst
    #: (audit role-operator-06). Changing one is still edit_money_settings.
    "see_money_settings": {"admin", "analyst"},
    "start_pilot": {"admin"},
    "run_jobs": {"admin", "operator", "analyst"},
    #: the energy-only report carries no money (plan Part 8.5: the viewer-safe report), so every member may generate it, a viewer included
    "energy_report": {"admin", "operator", "analyst", "viewer"},
}
#: What an administrator may grant a named member beyond the role (plan Part 10.5: "A named viewer may be granted see_costs by an admin"; audit
#: security-roles-16), and the role a grant is for: every other role holds it already. A grant is kept on the member's row (`members.grants`) and ends when
#: the person's role changes (store_members.update_member); a machine key never holds one.
GRANTS = {"see_costs": "viewer"}
#: A grant in words, for the Members page and the refusals
GRANT_WORDS = {"see_costs": "see costs"}

#: A session of local sign-in (its refresh tokens, platform.py) ends this many hours unused, and however active, this many days after sign-in (review of batch
#: 1, for the cookie sessions of before)
SESSION_HOURS = 12
SESSION_DAYS = 7
#: Clients on this machine. Until the first account is made the workspace is open to them only; "testclient" is the test client's name, which no network
#: address can have.
LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}
ITERATIONS = 200_000
#: Wrong passwords, or wrong codes, for one account (or the founder) from one address before that address is stopped, and for how long (review of batch 1;
#: the codes since package P20)
MAX_TRIES = 5
LOCK_MINUTES = 15
#: Wrong passwords for one account from every address together, in LOCK_MINUTES, before the account's sign-in is stopped for as long (audit
#: security-roles-10): the per-address count alone let N addresses buy 5N guesses. Well under the 100 consecutive failures NIST SP 800-63B (section 5.2.2)
#: caps an account at [recalled; verify]. An address the account signed in from in the last SESSION_DAYS is not stopped by it (its own count still holds),
#: so a stranger guessing cannot lock the person out where they work; a good sign-in, or an administrator or the founder clearing the lockout, ends it
ACCOUNT_TRIES = 20
#: Wrong codes from every address together, in LOCK_MINUTES, before the code step is stopped for as long (P20's repairs; the founder's code step since stage
#: B6): without it, N addresses would buy 5N guesses. The program's choice, well under the 100 consecutive failures NIST SP 800-63B (section 5.2.2) caps an
#: account at [recalled; verify]
MFA_ACCOUNT_TRIES = 20
#: How long a sign-in's password step waits for its code (package P20's challenge; the founder's since stage B6)
CHALLENGE_MINUTES = 5
#: A challenge's token: 32 random bytes, URL-safe Base64 without padding (43 characters)
LINK_TOKEN = r"[A-Za-z0-9_-]{43}"


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


#: The API's rate limits (master PS-07): requests in a sliding window of RATE_WINDOW seconds, kept in memory (a restart forgets them). Per signed-in user;
#: per address, of every request to /api from it; and per address, of the requests without a session to the public paths (signing in). Generous, so a
#: person working, a page polling a job and the tests never meet them: they stop a script. GRIDLYTICS_RATE_USER, GRIDLYTICS_RATE_ADDRESS and
#: GRIDLYTICS_RATE_PUBLIC change them; 0 turns one off.
RATE_WINDOW = 60
RATE_LIMITS = {"user": _env_int("GRIDLYTICS_RATE_USER", 6000), "address": _env_int("GRIDLYTICS_RATE_ADDRESS", 20000),
               "public": _env_int("GRIDLYTICS_RATE_PUBLIC", 1200)}
_limiter = None


def limiter():
    """The rate limits' counter: the sliding window of the acceptance routes (routes_accept.Limiter), one for the whole API."""
    global _limiter
    if _limiter is None:
        from .ratelimit import Limiter
        _limiter = Limiter()
    return _limiter


def rate_wait(kind: str, key: str) -> int:
    """Seconds to wait before this request may go on (0 when it may), counting it."""
    limit = RATE_LIMITS.get(kind, 0)
    return limiter().hit(("api", kind, key), limit, RATE_WINDOW) if limit and key else 0


def new_link() -> tuple[str, str]:
    """A challenge's token and the hash it is kept as."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)

current: ContextVar[dict | None] = ContextVar("current_user", default=None)
#: The company the request works in, {id, name}: set by the guard from the person's token, or from the index of the API key or the share link the request
#: carries (stage B4a of the sign-in). None in open mode, which works on the default database
company: ContextVar[dict | None] = ContextVar("current_company", default=None)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, rounds, salt, digest = stored.split("$")
        mine = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(mine.hex(), digest)
    except (ValueError, TypeError):
        return False


def password_problem(password) -> str | None:
    if not isinstance(password, str) or len(password) < 10:
        return "A password needs at least 10 characters"
    if len(password) > 200:
        return "A password may have at most 200 characters"
    return None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def open_mode() -> bool:
    """Open mode is not lifted (plan Part 10.4 step 2): Gridlytics never opens a workspace without a sign-in, even on this machine."""
    return False


def can(permission: str) -> bool:
    user = current.get()
    if user is None:
        return open_mode()
    if user.get("api_key"):
        # a machine key carries scopes, not a role (plan Part 10.5): the guard limits it to its scopes and its site, and it holds no role permission, so
        # no key sees costs or the fee, exports or changes a setting, whatever role it was made with
        return False
    if user["role"] in PERMISSIONS[permission]:
        return True
    # a grant an administrator gave this person (plan Part 10.5), good only for the role it is for
    return GRANTS.get(permission) == user["role"] and permission in (user.get("grants") or ())


def roles_words(permission: str) -> str:
    """The roles that hold a permission, in a phrase: "an administrator or an engineer"; a permission that can be granted names who it is granted to ("an
    administrator, an operator, an analyst or a viewer allowed to see costs")."""
    names = [ROLES[r].lower() for r in ROLES if r in PERMISSIONS[permission]]
    named = [f"{'an' if n[0] in 'aeiou' else 'a'} {n}" for n in names]
    if permission in GRANTS:
        named.append(f"a {ROLES[GRANTS[permission]].lower()} allowed to {GRANT_WORDS[permission]}")
    return named[0] if len(named) == 1 else f"{', '.join(named[:-1])} or {named[-1]}"


def need(permission: str, doing: str | None = None) -> None:
    """Refuse with a 403 when the signed-in role does not hold the permission. `doing`, when given, is what was asked in a few words ("Fetching climate data"), and the
    refusal then says who may: "Fetching climate data is for an administrator or an engineer" (R-341)."""
    if not can(permission):
        raise HTTPException(status_code=403, detail=f"{doing} is for {roles_words(permission)}" if doing else "Your role may not do this")


def actor() -> str:
    """Who is acting, for the audit trail and for signatures: the signed-in user, or "workspace" in open mode."""
    user = current.get()
    return user["name"] if user else "workspace (no sign-in yet)"
