"""The tariff library (UI-03): versions with sources and status, the rate cards in force, approvals, fuel prices, and the bill engine on demand."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Body

from . import auth, tariff_data, tariffs
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
            "adders": [{"key": a["key"], "name": a["name"], "rate": a["rate"], "billing_months": a["billing_months"]} for a in adders],
            "tax": (tariffs.resolve("tax", "national", d) or {}).get("body"), "icp": (tariffs.resolve("icp", "national", d) or {}).get("body"),
            "prosumer": {k: (tariffs.resolve("prosumer", k, d) or {}).get("body") for k in ("net_billing", "net_metering_legacy")},
            "versions": tariffs.provenance(), "pending": sum(1 for v in tariffs.versions() if v["status"] in ("pending", "awaiting_notification"))}


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


@router.post("/api/tariffs/fuel")
def fuel_add(inp: dict = Body(...)):
    auth.need("edit_money_settings", "Setting a fuel price")
    try:
        tariffs.add_fuel_price(str(inp.get("kind", "")), str(inp.get("valid_from", "")), float(inp.get("price", 0)), auth.actor())
    except (ValueError, TypeError) as exc:
        raise bad(exc) from None
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
