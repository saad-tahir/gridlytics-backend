"""Scoped API keys (plan Part 10.5), lifted from solarlytics-backend at c853130 (apikeys.py) and cut down to Gridlytics' scopes.

A key is made by an administrator (`manage_users`) for one organisation and one of its sites, with a name, its kind and its scopes. It is shown once,
when it is made, and kept as the SHA-256 of the key (as the sign-in's refresh tokens are), with the first characters to recognise it by. A request carries
it as `Authorization: Bearer <key>`; the API's guard then works in the company that holds the key (platform.db's index, written here when the key is made
in a company's database: stage B4a of the sign-in) and the key's organisation, as a user named after the key (acting_user). A machine key carries scopes,
not a role: the role it was made with is kept for the record only, so it holds no role permission and is served no money field, whatever that role
(auth.can, money.for_reader). A key reaches only what its scopes name, on its own site only (plan Part 10.5; the words of each are SCOPES', the paths
RESOURCES'):

* a scope is a resource and an access: `telemetry:write` (the edge paths), `plans:read` (the plan in force, its versions, its comparison and the control
  state) and `sites:read` (the site's settings); the site's data is in no scope (audit security-roles-07);
* a read is GET (or HEAD); anything else is a write; `x:write` does not include `x:read`, so a key that writes and reads is given both;
* a key is an edge device's or a partner tool's (`kind`): only an edge device's key sends the capabilities, events, acks and heartbeats and pulls the
  signed plan (EDGE_ONLY), whatever its scopes;
* every other path is outside every scope, so a key never reaches the settings of the company, the users or the platform's own routes.

A key that is not known, or was revoked, is answered 401; a known key outside its scopes 403. Each use records the key's last-used time.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid

from . import auth, store
from . import platform as platformdb

#: The scopes a key may hold, with their words
SCOPES = {
    "telemetry:write": "Send telemetry for its site; an edge device's key also sends its capabilities, events, acks and heartbeats",
    "plans:read": "Read its site's current plan, its versions and the control state (no money fields); an edge device's key also pulls the signed plan",
    "sites:read": "Read its site's settings: the site with its equipment and connections, its native settings and its overrides (no money fields)",
}
#: The paths of each resource, under /api (the guard sees /api/v1/x as /api/x). Every one names a site: a key is bound to one site (plan Part 10.5), so the
#: bare collection /api/sites, which lists every site of the company, is outside every scope. A scope reaches what its words say and no more (audit
#: security-roles-07): plans:read the plan in force, its versions, its comparison with the native settings and the control state; sites:read the site's
#: settings. The site's data (telemetry, the battery, outages, forecasts, savings, events, the pilot) is in no scope: a key sends its site's telemetry, it
#: never reads the site's history back (`reports:read` is a later scope, plan Part 2.6)
RESOURCES = (("telemetry", re.compile(r"^/api/edge/[^/]+/")),
             ("plans", re.compile(r"^/api/sites/[^/]+/(?:plan|plans|control)(?:/|$)")),
             ("sites", re.compile(r"^/api/sites/[^/]+(?:/(?:native|overrides))?$")))
#: The two kinds of key (plan Part 10.5): an edge device's, which runs the site, and a partner's tool's, which may send telemetry batches (plan Part 7.5:
#: partners' gateways, Enerlytics push) and read, but never acts as the site's controller (audit security-roles-07)
KINDS = {"edge": "Edge device", "partner": "Partner tool"}
#: What only an edge device's key does, whatever its scopes: the capabilities (the control tier the source asserts, plan Part 2.3), the events (an unknown
#: state pauses control), the acks of the plan it runs and its heartbeats, and the signed plan message (each pull is recorded as a send to the edge)
EDGE_ONLY = re.compile(r"^/api/edge/[^/]+/(?:capabilities|events|ack|heartbeat)$|^/api/sites/[^/]+/plans/current/message$")
#: The words a key starts with, then 43 URL-safe characters (32 random bytes)
PREFIX = "glk_"
TOKEN = re.compile(r"glk_[A-Za-z0-9_-]{43}")
NAME_MAX = 80
#: The role a key records when none is given (for the record only: a key holds no role permission)
DEFAULT_ROLE = "operator"
#: Keys one organisation may hold, revoked ones included (a list that is read whole)
MAX_KEYS = 200


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """A key and the hash it is kept as."""
    token = PREFIX + secrets.token_urlsafe(32)
    return token, token_hash(token)


def resource_of(path: str) -> str | None:
    return next((name for name, pattern in RESOURCES if pattern.match(path)), None)


def scope_needed(method: str, path: str) -> str | None:
    """The scope a request needs, or None when its path is outside every scope."""
    resource = resource_of(path)
    if resource is None:
        return None
    return f"{resource}:{'read' if method in ('GET', 'HEAD') else 'write'}"


def kind_refusal(kind: str, path: str) -> str | None:
    """Why a key of this kind may not make a request on `path` (its scopes allowing it), in words; None when it may."""
    if kind != "edge" and EDGE_ONLY.match(path):
        return ("Only an edge device's key sends a site's capabilities, events, acks and heartbeats and pulls its signed plan: this is a partner tool's key, "
                "which may send telemetry and read the plan and the site")
    return None


def fields(inp) -> dict:
    """A new key's fields, checked: a name, a role of auth.ROLES (optional: DEFAULT_ROLE) and one or more scopes. Raises ValueError, in words."""
    if not isinstance(inp, dict):
        raise ValueError("The key is sent as a set of named values")
    extra = sorted(set(inp) - {"name", "role", "scopes", "site_id", "kind"})
    if extra:
        raise ValueError(f"Unknown field '{extra[0]}': a key has a name, a role, its scopes, its site and its kind")
    site_id = inp.get("site_id")
    if not isinstance(site_id, str) or not site_id.strip():
        raise ValueError("A key is bound to one site: give its site_id (plan Part 10.5)")
    kind = inp.get("kind", "edge")
    if kind not in KINDS:
        raise ValueError("A key's kind is edge or partner")
    name = inp.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= NAME_MAX:
        raise ValueError(f"Give the key a name of 1 to {NAME_MAX} characters, to know it by")
    # the role is carried for the record only (acting_user: a key holds no role permission), so it may be left out: a key then records DEFAULT_ROLE
    role = inp.get("role")
    if role is None or role == "":
        role = DEFAULT_ROLE
    if not isinstance(role, str) or role not in auth.ROLES:
        raise ValueError("The key acts with the rights of a role: " + ", ".join(auth.ROLES))
    scopes = inp.get("scopes")
    if not isinstance(scopes, list) or not scopes or not all(isinstance(s, str) for s in scopes):
        raise ValueError("Give the key one or more scopes, such as plans:read")
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise ValueError(f"Unknown scope '{unknown[0][:40]}': the scopes are " + ", ".join(SCOPES))
    return {"name": name.strip(), "role": role, "scopes": [s for s in SCOPES if s in scopes], "site_id": site_id.strip(), "kind": kind}


def _row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "prefix": r["prefix"], "role": r["role"], "scopes": json.loads(r["scopes"] or "[]"), "created": r["created"],
            "by": r["by"], "last_used": r["last_used"], "revoked": r["revoked"], "revoked_by": r["revoked_by"], "site_id": r["site_id"], "kind": r["kind"]}


def add(name: str, role: str, scopes: list[str], by: str, site_id: str = "", kind: str = "edge") -> tuple[dict, str]:
    """A new key of the current organisation: its record (never the key or its hash) and the key, which is shown this once. Made in a company's database, the
    registry's index says which company holds it (written first, so no key is ever without it), since a request with the key names no company."""
    token, digest = new_token()
    key_id = uuid.uuid4().hex[:12]
    if (held := auth.company.get()) is not None:
        platformdb.index_add("api_key_index", digest, held["id"])
    with store._connect() as con:
        if con.execute("SELECT COUNT(*) FROM api_keys WHERE org_id = ?", (store.org(),)).fetchone()[0] >= MAX_KEYS:
            raise ValueError(f"An organisation holds at most {MAX_KEYS} keys")
        con.execute("INSERT INTO api_keys (id, org_id, name, prefix, token_hash, role, scopes, created, by, site_id, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (key_id, store.org(), name, token[:len(PREFIX) + 6], digest, role, json.dumps(scopes), store._now(), by, site_id, kind))
        r = con.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return _row(r), token


def listing() -> list[dict]:
    with store._connect() as con:
        rows = con.execute("SELECT * FROM api_keys WHERE org_id = ? ORDER BY created DESC, rowid DESC", (store.org(),)).fetchall()
    return [_row(r) for r in rows]


def revoke(key_id: str, by: str) -> dict | None:
    """Revokes a key of the current organisation (once; a second time changes nothing); None when it has no such key."""
    with store._connect() as con:
        con.execute("UPDATE api_keys SET revoked = ?, revoked_by = ? WHERE id = ? AND org_id = ? AND revoked IS NULL", (store._now(), by, key_id, store.org()))
        r = con.execute("SELECT * FROM api_keys WHERE id = ? AND org_id = ?", (key_id, store.org())).fetchone()
    # a revoked key is refused wherever it is looked for: the company's index forgets it
    if r is not None and r["revoked"] and auth.company.get() is not None:
        platformdb.index_remove("api_key_index", r["token_hash"])
    return _row(r) if r else None


def revoke_site(site_id: str, by: str) -> list[dict]:
    """Revoke every key of a site being removed (round 7, audit flow-reports-alerts-members-keys-14: a key is bound to one site, and a removed site's
    key stayed active, its requests answered 404); the keys revoked."""
    with store._connect() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM api_keys WHERE org_id = ? AND site_id = ? AND revoked IS NULL", (store.org(), site_id))]
    return [k for k in (revoke(i, by) for i in ids) if k is not None]


def by_token(token: str) -> dict | None:
    """The key a request carries, with its organisation, when it is known and not revoked; its last use is recorded. A key is found by its hash in whatever
    organisation holds it: the key names the organisation, the request never does."""
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return None
    with store._connect() as con:
        r = con.execute("SELECT * FROM api_keys WHERE token_hash = ? AND revoked IS NULL", (token_hash(token),)).fetchone()
        if r is None:
            return None
        con.execute("UPDATE api_keys SET last_used = ? WHERE id = ?", (store._now(), r["id"]))
    return {**_row(r), "org_id": r["org_id"]}


def acting_user(key: dict) -> dict:
    """The user a key acts as: named after the key; never an administrator's account, and no user row (so it signs nothing and holds no seat). The role
    the key was made with is carried for the record only: a machine key carries scopes, not a role (plan Part 10.5), so `auth.can` grants a key no
    permission and `money.for_reader` gives it no money field."""
    return {"id": f"key:{key['id']}", "email": "", "name": f"API key '{key['name']}'", "role": key["role"], "created": key["created"], "disabled": False,
            "licence": "", "has_seal": False, "mfa": False, "api_key": key["id"], "scopes": list(key["scopes"]), "site_id": key.get("site_id", "")}
