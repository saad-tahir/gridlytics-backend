"""The tariff library (UI-03): versions with sources and status, the rate cards in force, approvals, fuel prices, and the bill engine on demand."""
from __future__ import annotations

import json
import re
from datetime import date

from fastapi import APIRouter, Body

from . import auth, store, tariff_data, tariffs
from .routes_common import bad, out, parse_day, today

router = APIRouter()


@router.get("/api/tariffs")
def tariffs_index(on: str | None = None):
    auth.need("see_costs", "Seeing the tariff library")
    d = parse_day(on, today())
    try:
        card_v = tariffs.resolve("schedule", "national", d)
    except Exception as exc:  # pragma: no cover - a corrupt library
        raise bad(exc) from None
    cards = (card_v or {}).get("body", {}).get("cards", {})
    cal_x = tariffs.resolve("tou", "xwdisco", d)
    cal_k = tariffs.resolve("tou", "ke", d)
    adders = tariffs.adders_for(d.strftime("%Y-%m"))
    return {"on": d.isoformat(), "schedule": {k: card_v[k] for k in ("id", "effective_from", "source", "status", "hash")} if card_v else None,
            "cards": [{"category": k, "class": tariff_data.category_class(k), **v} for k, v in sorted(cards.items())],
            "calendars": {"xwdisco": (cal_x or {}).get("body"), "ke": (cal_k or {}).get("body")},
            "adders": [{"key": a["key"], "name": a["name"], "rate": a["rate"], "billing_months": a["billing_months"], "fca": tariffs.is_fca(a)}
                       for a in adders],
            "tax": (tariffs.resolve("tax", "national", d) or {}).get("body"), "icp": (tariffs.resolve("icp", "national", d) or {}).get("body"),
            "prosumer": {k: (tariffs.resolve("prosumer", k, d) or {}).get("body") for k in ("net_billing", "net_metering_legacy")},
            # pending counts the versions an administrator can approve: a placeholder awaiting its notification is not one (approving it is a 422)
            "versions": tariffs.provenance(), "pending": sum(1 for v in tariffs.versions() if v["status"] == "pending")}


@router.get("/api/tariffs/versions")
def versions(kind: str | None = None):
    auth.need("see_costs", "Seeing tariff versions")
    return tariffs.versions(kind)


@router.post("/api/tariffs/versions")
def propose(inp: dict = Body(...)):
    auth.need("edit_tariffs", "Drafting a tariff version")
    try:
        return tariffs.propose(inp.get("kind", ""), inp.get("key", ""), inp.get("effective_from", ""), inp.get("body"), inp.get("source"), auth.actor())
    except (ValueError, TypeError) as exc:
        raise bad(exc) from None


@router.post("/api/tariffs/versions/{version_id}/approve")
def approve(version_id: str):
    auth.need("approve_tariff", "Approving a tariff version")
    try:
        return tariffs.approve(version_id, auth.actor())
    except KeyError:
        raise bad(ValueError("No such tariff version")) from None
    except ValueError as exc:
        raise bad(exc) from None


@router.get("/api/tariffs/fuel")
def fuel(kind: str | None = None):
    auth.need("see_costs", "Seeing fuel prices")
    return {"prices": tariffs.fuel_prices(kind)}


FUEL_REF_MAX = 200
FUEL_URL_MAX = 500
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _fuel_source(src, by: str) -> dict | None:
    """A typed fuel price's optional `source: {ref, url}` (the notification or price list it was read from) as the row keeps it: {kind manual, ref (the
    reference, or the person when none is given), url, by (the person), verified}; None when neither is given. Raises ValueError, in words."""
    if src is None:
        return None
    if not isinstance(src, dict) or set(src) - {"ref", "url"}:
        raise ValueError("A fuel price's source is {ref, url}: the reference it was read from and its web address")
    ref, url = src.get("ref"), src.get("url")
    if ref is not None and (not isinstance(ref, str) or len(ref.strip()) > FUEL_REF_MAX):
        raise ValueError(f"A fuel price's source reference is text of at most {FUEL_REF_MAX} characters")
    if url is not None and (not isinstance(url, str) or (url.strip() and (len(url.strip()) > FUEL_URL_MAX or not _URL.fullmatch(url.strip())))):
        raise ValueError(f"A fuel price's source url is an http or https address of at most {FUEL_URL_MAX} characters")
    ref, url = (ref or "").strip(), (url or "").strip()
    if not ref and not url:
        return None
    return {"kind": "manual", "ref": ref or by, "url": url or None, "by": by, "verified": "primary"}


@router.post("/api/tariffs/fuel")
def fuel_add(inp: dict = Body(...)):
    """Sets a fuel price from a day on; an optional `source: {ref, url}` records where it was read (the row's source then carries ref, url and by)."""
    auth.need("edit_money_settings", "Setting a fuel price")
    try:
        source = _fuel_source(inp.get("source"), auth.actor())
        kind, valid_from = str(inp.get("kind", "")), str(inp.get("valid_from", ""))
        tariffs.add_fuel_price(kind, valid_from, float(inp.get("price", 0)), auth.actor())
    except (ValueError, TypeError) as exc:
        raise bad(exc) from None
    if source is not None:
        canonical = getattr(tariffs, "_canonical_day", None)
        day = canonical(valid_from, "A fuel price's start") if callable(canonical) else valid_from[:10]
        with store.connect() as con:
            con.execute("UPDATE fuel_prices SET source = ? WHERE kind = ? AND valid_from = ?", (json.dumps(source), kind, day))
        store.audit(auth.actor(), "recorded a fuel price's source", kind, {"valid_from": day, "ref": source["ref"], "url": source["url"]})
    return {"prices": tariffs.fuel_prices(inp.get("kind"))}


@router.get("/api/tariffs/card")
def card(category: str, on: str | None = None):
    auth.need("see_costs", "Seeing a rate card")
    d = parse_day(on, today())
    try:
        c, v = tariffs.card_for(category, d)
    except LookupError as exc:
        raise bad(exc) from None
    return out({"category": category, "card": c, "version": v["id"], "source": v["source"], "on": d.isoformat()}) if auth.can("see_costs") else {}


_ = date
