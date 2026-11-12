"""The tariff library (UI-03): versions with sources and status, the rate cards in force, approvals, fuel prices, and the bill engine on demand."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta

from fastapi import APIRouter, BackgroundTasks, Body

from . import auth, sites, store, tariff_data, tariffs
from .routes_common import bad, out, parse_day, today

router = APIRouter()


def _agreements(on: date) -> list[dict]:
    """Each connection under net billing or legacy net metering, with its agreement's dates, its state on the day (tariffs.agreement_state:
    active, not_started, ended or unreadable) and the regime it is billed under that day (`billed_as`, tariffs.regime_on): its own while the
    agreement is in force, net billing after a legacy agreement ends, else none, whose export earns nothing (plan 9.3 UI-03, "Regime expired")."""
    rows = []
    for s in sites.listing():
        for c in sites.connections(s["id"]):
            state = tariffs.agreement_state(c, on)
            if state is None:
                continue
            d = c.get("data") or {}
            rows.append({"site_id": s["id"], "site_name": s["name"], "connection_id": c["id"], "meter_ref": d.get("meter_ref") or "",
                         "tariff_category": d.get("tariff_category"), "regime": d.get("regime"), "agreement_start": d.get("agreement_start"),
                         "agreement_end": d.get("agreement_end"), "state": state, "billed_as": tariffs.regime_on(c, on)})
    return sorted(rows, key=lambda x: (x["site_name"], x["connection_id"]))


def _connections() -> list[dict]:
    """Every grid connection of the company, by site: what a site override (kind "override", keyed by the connection) can name."""
    rows = []
    for s in sites.listing():
        for c in sites.connections(s["id"]):
            d = c.get("data") or {}
            if d.get("regime") == "off_grid":
                continue
            rows.append({"connection_id": c["id"], "site_id": s["id"], "site_name": s["name"], "meter_ref": d.get("meter_ref") or "",
                         "tariff_category": d.get("tariff_category")})
    return sorted(rows, key=lambda x: (x["site_name"], x["connection_id"]))


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
            "adders": [{"key": a["key"], "name": a["name"], "rate": a["rate"], "billing_months": a["billing_months"], "exempt": a.get("exempt") or [],
                        "category_rates": a.get("category_rates") or {}, "fca": tariffs.is_fca(a)} for a in adders],
            "tax": (tariffs.resolve("tax", "national", d) or {}).get("body"), "icp": (tariffs.resolve("icp", "national", d) or {}).get("body"),
            "prosumer": {k: (tariffs.resolve("prosumer", k, d) or {}).get("body") for k in ("net_billing", "net_metering_legacy")},
            # pending counts the versions an administrator can approve: a placeholder awaiting its notification is not one (approving it is a 422)
            "versions": tariffs.provenance(), "pending": sum(1 for v in tariffs.versions() if v["status"] == "pending"),
            "agreements": _agreements(d), "connections": _connections(),
            # a version awaiting its notification whose start has passed by the day (engine-tariffs-06): priced with the one still in force
            "notices": tariffs.awaiting_notices(d)}


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


log = logging.getLogger("gridlytics.tariffs")
#: A version starting within this many days of today reaches the plans in force (a 192-step horizon is two days), so approving it re-solves them
RESOLVE_HORIZON_DAYS = 2


def _resolve_sites(site_ids: list[str]) -> None:
    """The re-solve after a tariff version is approved, site by site (a failure is logged; the version is approved either way)."""
    from . import plans  # plans imports the routes' helpers at module load
    for sid in site_ids:
        try:
            plans.tick(sid)
        except Exception:
            log.exception("re-solve after a tariff approval failed for site %s", sid)


@router.post("/api/tariffs/versions/{version_id}/approve")
def approve(version_id: str, background: BackgroundTasks):
    """Approve a pending version. One that starts by the end of the plans' horizon re-solves every active grid site's plan once the answer is sent
    (round 7 stage 2, audit brief-vs-build-15; OPT-15 lists a tariff version among the events that re-plan): a plan priced on the version it replaces
    waited for the next quarter's tick. `re_solving`: the sites re-solved."""
    auth.need("approve_tariff", "Approving a tariff version")
    try:
        v = tariffs.approve(version_id, auth.actor())
    except KeyError:
        raise bad(ValueError("No such tariff version")) from None
    except ValueError as exc:
        raise bad(exc) from None
    start = str((v or {}).get("effective_from") or "")[:10]
    ids: list[str] = []
    if start and start <= (today() + timedelta(days=RESOLVE_HORIZON_DAYS)).isoformat():
        ids = [x["id"] for x in sites.listing() if x.get("status") == "active" and sites.grid_connected(sites.full(x["id"]) or {})]
        if ids:
            background.add_task(_resolve_sites, ids)
    return {**v, "re_solving": len(ids)} if isinstance(v, dict) else v


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
