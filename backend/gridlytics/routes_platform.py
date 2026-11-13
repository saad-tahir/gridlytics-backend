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
    # each key's site by name, a removed site's too, marked removed (round 7, audit flow-reports-alerts-members-keys-14: the page named a removed
    # site's key by its raw id)
    from . import sites
    names = sites.names()
    return [{**k, "site_name": (names.get(k["site_id"]) or {}).get("name", ""), "site_removed": bool((names.get(k["site_id"]) or {}).get("removed"))}
            for k in apikeys.listing()]


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
    """The person's choices, of the kinds of alert they receive (`categories`: alerts.reader_categories(), as the inbox gives them; UX backend ask K33,
    plan G9 item 2: a viewer was offered the money kinds it never receives): the kinds the inbox and its badge list (`follow`, every kind until they
    choose), whether they want alerts by email (`email`), and which kinds by email (`email_kinds`, My account's email card; plan G3.4). Until the card
    saved its own, the kinds by email were the kinds followed when `email` was true, and none when it was false (plan G3.4's compatibility rule). A kind
    kept from a role held before is not answered."""
    kept = kept if isinstance(kept, dict) else {}
    cats = alerts.reader_categories()
    follow = kept.get("follow")
    follow = [c for c in cats if c in follow] if isinstance(follow, list) else list(cats)
    email = kept.get("email") is True
    by_email = kept.get("email_kinds")
    by_email = [c for c in cats if c in by_email] if isinstance(by_email, list) else (list(follow) if email else [])
    return {"categories": list(cats), "follow": follow, "email": email, "email_kinds": by_email}


def _kinds(value, field: str, cats: tuple) -> list[str]:
    """A list of kinds of alert from a save, each one the person receives: an unknown kind, or a list that is not one, is refused with the kinds they
    receive; a known kind they do not receive is refused as such ("You do not receive Savings alerts"; plan G9 item 2)."""
    asked = ("Follow is a list of the kinds of alert you receive: " if field == "follow"
             else "Choose the kinds of alert to email from those you receive: ") + ", ".join(cats)
    if not isinstance(value, list) or any(not isinstance(c, str) or c not in alerts.CATEGORIES for c in value):
        raise Refusal(422, asked, "invalid")
    not_mine = [c for c in alerts.CATEGORIES if c in value and c not in cats]
    if not_mine:
        kinds = not_mine[0] if len(not_mine) == 1 else ", ".join(not_mine[:-1]) + " or " + not_mine[-1]
        raise Refusal(422, f"You do not receive {kinds} alerts", "invalid")
    return [c for c in cats if c in value]


@router.get("/api/platform/notifications")
def notifications_mine():
    """The signed-in person's notification choices in this company (plan UI-18: "notification preferences"), kept per person in the company's file: the
    kinds of alert they receive, the kinds the inbox lists, and those they want by email. Any role; never a key."""
    return _notify_out(store.get_setting(_notify_key(_me())))


@router.put("/api/platform/notifications")
def notifications_put(inp=Body(...)):
    """Change the signed-in person's notification choices: `follow` (the kinds of alert the inbox lists), `email_kinds` (the kinds to email: at least one
    sets `email` true, none sets it false; UX K33), or `email` (true or false: today's "Email them to me as well", the kinds followed by email); a field
    left out keeps its value. Each kind must be one the person receives (`categories`)."""
    me = _me()
    if not isinstance(inp, dict):
        raise Refusal(422, "Send your choices as a set of named values: follow, email_kinds and email", "invalid")
    extra = sorted(set(inp) - {"follow", "email", "email_kinds"})
    if extra:
        raise Refusal(422, f"Unknown field '{extra[0]}': the choices are follow (the kinds of alert the inbox lists), email_kinds (the kinds to email) "
                           f"and email", "invalid")
    if "email_kinds" in inp and "email" in inp:
        raise Refusal(422, "Send the kinds of alert to email, or whether to email, not both: choosing a kind to email turns email on", "invalid")
    held = store.get_setting(_notify_key(me))
    held = held if isinstance(held, dict) else {}
    now = _notify_out(held)
    before = dict(now)
    cats = alerts.reader_categories()
    if "follow" in inp:
        now["follow"] = _kinds(inp["follow"], "follow", cats)
    if "email" in inp and not isinstance(inp["email"], bool):
        raise Refusal(422, "Say whether you want alerts by email too: true or false", "invalid")
    # what is kept: a field the save gives, else as it was kept (a person who never chose the inbox's kinds still follows every kind, a kind
    # added to their role later too)
    kept = {k: held[k] for k in ("follow", "email", "email_kinds") if k in held}
    if "follow" in inp:
        kept["follow"] = now["follow"]
    if "email_kinds" in inp:
        # the email card (plan G3.4): the kinds to email are its own, apart from the inbox's, and email is on while one is chosen
        now["email_kinds"] = _kinds(inp["email_kinds"], "email_kinds", cats)
        now["email"] = bool(now["email_kinds"])
        kept.update(email=now["email"], email_kinds=now["email_kinds"])
    elif "email" in inp:
        # today's checkbox: the kinds followed, by email too, or none; the email card's own kinds give way to it
        now["email"] = inp["email"]
        now["email_kinds"] = list(now["follow"]) if now["email"] else []
        kept["email"] = now["email"]
        kept.pop("email_kinds", None)
    elif not isinstance(held.get("email_kinds"), list) and now["email"]:
        now["email_kinds"] = list(now["follow"])         # on today's rule, the kinds by email follow the kinds followed
    store.set_setting(_notify_key(me), kept)
    store.audit(auth.actor(), "changed their notification choices", "", {"changes": store.changes(before, now, ("follow", "email", "email_kinds"))})
    return now
