"""The company's own platform routes: who the company is, and its site-bound API keys (plan Part 10.4 step 4: org and keys only); and each person's
notification choices in the company (UI-18 My account; audit role-analyst-32).

A key is made by an administrator for one site and kind (edge or partner); it is shown once, kept as its hash, and listed with its prefix only.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from . import alerts, apikeys, auth, store, store_members
from . import platform as platformdb
from .schemas import Refusal

router = APIRouter()


@router.get("/api/platform/org")
def org_mine():
    """Who the company is, the caller's role, and its seats: `seats` (the registry's limit) and `seats_used` (members not switched off, the owner
    included), both null when the request works in no company of the registry."""
    company = auth.company.get()
    seats = used = None
    if company and company.get("id"):
        row = platformdb.company(company["id"])
        if row is not None and row.get("seats") is not None:
            seats, used = int(row["seats"]), store_members.count_members()
    return {"company": company, "role": (auth.current.get() or {}).get("role"), "seats": seats, "seats_used": used}


@router.get("/api/platform/keys")
def keys_list():
    auth.need("manage_users", "Listing API keys")
    return apikeys.listing()


@router.post("/api/platform/keys")
def keys_add(inp: dict = Body(...)):
    auth.need("manage_users", "Making an API key")
    try:
        fields = apikeys.fields(inp)
    except ValueError as exc:
        raise Refusal(422, str(exc), "invalid") from None
    from . import sites
    if sites.get(fields["site_id"]) is None:
        raise Refusal(422, "No such site for this key", "invalid")
    record, token = apikeys.add(fields["name"], fields["role"], fields["scopes"], auth.actor(), fields["site_id"], fields["kind"])
    store.audit(auth.actor(), "made an API key", record["name"], {"site_id": fields["site_id"], "scopes": fields["scopes"]})
    return {**record, "token": token}


@router.delete("/api/platform/keys/{key_id}")
def keys_revoke(key_id: str):
    auth.need("manage_users", "Revoking an API key")
    record = apikeys.revoke(key_id, auth.actor())
    if record is None:
        raise HTTPException(status_code=404, detail="No such key")
    store.audit(auth.actor(), "revoked an API key", record["name"])
    return record


# ------------------------------------------------------------------------------------------------ the person's notification choices (UI-18 My account)

def _me() -> str:
    me = auth.current.get()
    if not me or me.get("api_key"):
        raise Refusal(403, "Notification choices are a signed-in person's own", "forbidden")
    return me["id"]


def _notify_key(user_id: str) -> str:
    return alerts.notify_key(user_id)     # the inbox and its badge read the same setting (alerts.followed_categories)


def _notify_out(kept: dict | None) -> dict:
    """The person's choices: the kinds of alert they follow (every kind until they choose), and whether they want them by email too."""
    kept = kept if isinstance(kept, dict) else {}
    follow = kept.get("follow")
    follow = [c for c in alerts.CATEGORIES if c in follow] if isinstance(follow, list) else list(alerts.CATEGORIES)
    return {"categories": list(alerts.CATEGORIES), "follow": follow, "email": kept.get("email") is True}


@router.get("/api/platform/notifications")
def notifications_mine():
    """The signed-in person's notification choices in this company (plan UI-18: "notification preferences"), kept per person in the company's file: the
    kinds of alert they follow, and email besides the app. Any role; never a key."""
    return _notify_out(store.get_setting(_notify_key(_me())))


@router.put("/api/platform/notifications")
def notifications_put(inp=Body(...)):
    """Change the signed-in person's notification choices: `follow` (a list of alert kinds, of `categories`), `email` (true or false); a field left out
    keeps its value."""
    me = _me()
    if not isinstance(inp, dict):
        raise Refusal(422, "Send your choices as a set of named values: follow and email", "invalid")
    extra = sorted(set(inp) - {"follow", "email"})
    if extra:
        raise Refusal(422, f"Unknown field '{extra[0]}': the choices are follow (the kinds of alert) and email", "invalid")
    now = _notify_out(store.get_setting(_notify_key(me)))
    if "follow" in inp:
        follow = inp["follow"]
        if not isinstance(follow, list) or any(not isinstance(c, str) or c not in alerts.CATEGORIES for c in follow):
            raise Refusal(422, f"Follow is a list of the kinds of alert: {', '.join(alerts.CATEGORIES)}", "invalid")
        now["follow"] = [c for c in alerts.CATEGORIES if c in follow]
    if "email" in inp:
        if not isinstance(inp["email"], bool):
            raise Refusal(422, "Say whether you want alerts by email too: true or false", "invalid")
        now["email"] = inp["email"]
    store.set_setting(_notify_key(me), {"follow": now["follow"], "email": now["email"]})
    return now
