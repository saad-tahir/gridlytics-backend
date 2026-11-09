"""The company's own platform routes: who the company is, and its site-bound API keys (plan Part 10.4 step 4: org and keys only).

A key is made by an administrator for one site and kind (edge or partner); it is shown once, kept as its hash, and listed with its prefix only.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from . import apikeys, auth, store, store_members
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
