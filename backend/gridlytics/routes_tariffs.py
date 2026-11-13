"""The tariff library (UI-03): versions with sources and status, the rate cards in force, approvals, fuel prices, and the bill engine on demand."""
from __future__ import annotations

import calendar as _calendar
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Body

from . import auth, sites, store, tariff_data, tariff_drafts, tariffs
from .routes_common import bad, full_or_404, karachi, out, parse_day, today
from .schemas import Refusal, TariffDraft, TariffPreviewIn, TariffSimulateIn, TariffWithdrawIn, check

router = APIRouter()


def _agreements(on: date) -> list[dict]:
    """Each connection under net billing or legacy net metering, with its agreement's dates, its state on the day (tariffs.agreement_state:
    active, not_started, ended or unreadable) and the regime it is billed under that day (`billed_as`, tariffs.regime_on): its own while the
    agreement is in force, net billing after a legacy agreement ends, else none, whose export earns nothing (plan 9.3 UI-03, "Regime expired").

    `ends_in_days` (UX backend ask K32; R8 2.3): the days from the day shown to the agreement's last day, for an agreement in force that ends within
    the site's alert rules' `agreement_days` (60 by default), the window the Tariff alert regime_expiring_<connection> is raised in, so the page's
    "Ends in 45 days" and the alert agree; 0 on its last day; null otherwise."""
    from . import alerts  # alerts imports the plans, which import the routes' helpers at module load
    rows = []
    for s in sites.listing():
        warn = None
        for c in sites.connections(s["id"]):
            state = tariffs.agreement_state(c, on)
            if state is None:
                continue
            d = c.get("data") or {}
            ends = None
            window = tariffs.agreement_window(c) if state == "active" else None
            if window and window[1] is not None:
                warn = warn if warn is not None else alerts._th(alerts.site_rules(s["id"]), "Tariff", "agreement_days")
                left = (window[1] - on).days
                ends = left if 0 <= left <= warn else None
            rows.append({"site_id": s["id"], "site_name": s["name"], "connection_id": c["id"], "meter_ref": d.get("meter_ref") or "",
                         "tariff_category": d.get("tariff_category"), "regime": d.get("regime"), "agreement_start": d.get("agreement_start"),
                         "agreement_end": d.get("agreement_end"), "state": state, "billed_as": tariffs.regime_on(c, on), "ends_in_days": ends})
    return sorted(rows, key=lambda x: (x["site_name"], x["connection_id"]))


def _me() -> str:
    """The caller's sign-in id: a version's author and a draft's are told by it ('' with none)."""
    return str((auth.current.get() or {}).get("id") or "")


def _row(v: dict) -> dict:
    """A version as the pages read it (UX K32): its author's id left out, and `template` (a version the build ships from the notified schedules, not
    one the company drafted: the In force view's "Template" mark), `mine` (the caller drafted it: Withdraw, and no Approve) and `withdrawn` ({at in
    Karachi time, by, reason}, null unless it was withdrawn)."""
    me = _me()
    row = {k: x for k, x in v.items() if k not in ("by_id", "withdrawn_at", "withdrawn_by", "withdrawn_note")}
    row["template"] = v.get("by") == "shipped"
    row["mine"] = bool(me) and v.get("by_id") == me
    row["withdrawn"] = ({"at": karachi(v["withdrawn_at"]), "by": v.get("withdrawn_by") or None, "reason": v.get("withdrawn_note") or None}
                        if v.get("withdrawn_at") else None)
    return row


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
    return {"on": d.isoformat(), "schedule": {**{k: card_v[k] for k in ("id", "effective_from", "source", "status", "hash")},
                                              "template": card_v.get("by") == "shipped"} if card_v else None,
            "cards": [{"category": k, "class": tariff_data.category_class(k), **v} for k, v in sorted(cards.items())],
            "calendars": {"xwdisco": (cal_x or {}).get("body"), "ke": (cal_k or {}).get("body")},
            "adders": [{"key": a["key"], "name": a["name"], "rate": a["rate"], "billing_months": a["billing_months"], "exempt": a.get("exempt") or [],
                        "category_rates": a.get("category_rates") or {}, "fca": tariffs.is_fca(a)} for a in adders],
            "tax": (tariffs.resolve("tax", "national", d) or {}).get("body"), "icp": (tariffs.resolve("icp", "national", d) or {}).get("body"),
            "prosumer": {k: (tariffs.resolve("prosumer", k, d) or {}).get("body") for k in ("net_billing", "net_metering_legacy")},
            # pending counts the versions an administrator can approve: a placeholder awaiting its notification is not one (approving it is a 422)
            "versions": [_row(v) for v in tariffs.provenance()], "pending": sum(1 for v in tariffs.versions() if v["status"] == "pending"),
            "agreements": _agreements(d), "connections": _connections(),
            # a version awaiting its notification whose start has passed by the day (engine-tariffs-06): priced with the one still in force
            "notices": tariffs.awaiting_notices(d)}


@router.get("/api/tariffs/versions")
def versions(kind: str | None = None):
    auth.need("see_costs", "Seeing tariff versions")
    return [_row(v) for v in tariffs.versions(kind)]


@router.post("/api/tariffs/versions")
def propose(inp: dict = Body(...)):
    auth.need("edit_tariffs", "Drafting a tariff version")
    try:
        return _row(tariffs.propose(inp.get("kind", ""), inp.get("key", ""), inp.get("effective_from", ""), inp.get("body"), inp.get("source"),
                                    auth.actor(), _me()))
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
        v = tariffs.approve(version_id, auth.actor(), _me())
    except KeyError:
        raise bad(ValueError("No such tariff version")) from None
    except tariffs.OwnVersion as exc:
        raise Refusal(403, str(exc), "own_version") from None
    except ValueError as exc:
        raise bad(exc) from None
    start = str((v or {}).get("effective_from") or "")[:10]
    ids: list[str] = []
    if start and start <= (today() + timedelta(days=RESOLVE_HORIZON_DAYS)).isoformat():
        ids = [x["id"] for x in sites.listing() if x.get("status") == "active" and sites.grid_connected(sites.full(x["id"]) or {})]
        if ids:
            background.add_task(_resolve_sites, ids)
    return {**_row(v), "re_solving": len(ids)} if isinstance(v, dict) else v


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
        day = tariffs._canonical_day(valid_from, "A fuel price's start")
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


# ------------------------------------------------------------------------------------------------ the tariff editor (UX backend ask K32)
#
# Plan F2.5 and F11 item 6: the editor checks a draft field by field (validate), saves it as its author types (drafts), prices a week and rebuilds a
# bill under it beside the version in force (preview, simulate), submits it as versions waiting for approval, and a waiting version is withdrawn by
# its author or an administrator. Every route needs see_costs at least (rates are money, R8 2.3): a viewer and a machine key are refused, and every
# rupee figure carries a money name all the same.


def _administrator() -> bool:
    """An administrator: who approves tariff versions, sees every draft and withdraws any waiting version."""
    return auth.can("approve_tariff")


def _draft_or_404(draft_id: str) -> dict:
    """A saved draft the caller may read: their own, or any for an administrator; another person's is no draft of theirs (404)."""
    d = tariff_drafts.get(draft_id, _me() or auth.actor())
    if d is None or not (d["mine"] or _administrator()):
        raise Refusal(404, "No such tariff draft", "not_found")
    return d


def _unfit(found: list[dict]) -> Refusal:
    n = len(found)
    return Refusal(422, f"{'One thing' if n == 1 else f'{n} things'} to fix first: {found[0]['message']}", "invalid", extra={"problems": found})


def _what_if(req) -> tuple[list[dict], dict]:
    """The versions a preview or a simulation prices as if approved, and what they are ({source: draft, draft_id or version_id, id, name}). The
    draft as typed and a saved draft are drafting (edit_tariffs) and must pass every check; a version waiting for approval is reading (see_costs)."""
    on = today()
    if req.version_id is not None:
        auth.need("see_costs", "Pricing a tariff version")
        v = next((x for x in tariffs.versions() if x["id"] == req.version_id), None)
        if v is None:
            raise Refusal(404, "No such tariff version", "not_found")
        if v["status"] != "pending":
            raise Refusal(422, "Only a version waiting for approval is priced beside the version in force", "invalid")
        return [v], {"source": "version_id", "id": v["id"], "name": tariffs.version_name(v, tariffs._sites_by_connection())}
    auth.need("edit_tariffs", "Pricing a tariff draft")
    if req.draft_id is not None:
        d = _draft_or_404(req.draft_id)
        body, ref, source = d["body"], req.draft_id, "draft_id"
    else:
        body, ref, source = req.draft.model_dump(), "draft", "draft"
    found = tariff_drafts.problems(body, on)
    if found:
        raise _unfit(found)
    return tariff_drafts.as_versions(body, on, ref), {"source": source, "id": None if source == "draft" else ref, "name": tariff_drafts.name_of(body)}


def _grid_connection(site: dict, connection_id: str | None) -> dict:
    conns = site.get("connections") or []
    if connection_id:
        conn = next((c for c in conns if c["id"] == connection_id), None)
        if conn is None:
            raise Refusal(404, "This site has no such connection", "not_found")
    else:
        conn = conns[0] if conns else None
    if conn is None or (conn.get("data") or {}).get("regime") == "off_grid":
        raise Refusal(422, "This site has no grid connection: there is no tariff to price", "invalid")
    return conn


def _pct(a, b) -> float | None:
    return round((b - a) / abs(a) * 100, 1) if a not in (None, 0) and b is not None else None


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 2) if xs else None


@router.post("/api/tariffs/versions/validate")
def validate_draft(inp: dict = Body(...)):
    """Checks a draft as the editor holds it, every field at once (plan F2.5.6): `problems` ({version, kind, key, field, path, step, message}; empty
    when it can be submitted), and each version beside the one in force on its start with what it changes (`versions`). Stores nothing."""
    auth.need("edit_tariffs", "Checking a tariff draft")
    body = check(TariffDraft, inp).model_dump()
    on = today()
    found = tariff_drafts.problems(body, on)
    return {"ok": not found, "problems": found, "versions": tariff_drafts.against(body, on)}


@router.get("/api/tariffs/drafts")
def drafts_list():
    """The caller's tariff drafts, newest first; an administrator's list holds everyone's (each says whose: `author`, `mine`). Never counted as waiting."""
    auth.need("edit_tariffs", "Seeing tariff drafts")
    return {"drafts": tariff_drafts.listing(_me() or auth.actor(), _administrator())}


@router.get("/api/tariffs/drafts/{draft_id}")
def draft_get(draft_id: str):
    auth.need("edit_tariffs", "Seeing a tariff draft")
    return _draft_or_404(draft_id)


@router.put("/api/tariffs/drafts/{draft_id}")
def draft_put(draft_id: str, inp: dict = Body(...)):
    """Saves the caller's draft as they type (the editor's whole work: `TariffDraft`), under the id the editor chose. A draft id another person's draft
    holds is refused (409 draft_taken), never written over: an administrator resuming someone else's draft saves it under an id of their own."""
    auth.need("edit_tariffs", "Saving a tariff draft")
    if not tariff_drafts.DRAFT_ID.match(draft_id):
        raise Refusal(422, "A draft's id is 1 to 64 letters, digits, dashes or underscores", "invalid")
    body = check(TariffDraft, inp).model_dump()
    try:
        return tariff_drafts.put(draft_id, _me() or auth.actor(), auth.actor(), body)
    except tariff_drafts.Taken as exc:
        raise Refusal(409, str(exc), "draft_taken") from None
    except ValueError as exc:
        raise bad(exc) from None


@router.delete("/api/tariffs/drafts/{draft_id}")
def draft_delete(draft_id: str):
    """Discards a draft: the caller's own, or anyone's for an administrator (Versions' "Your drafts" offers it beside Resume). Audited."""
    auth.need("edit_tariffs", "Discarding a tariff draft")
    d = _draft_or_404(draft_id)
    tariff_drafts.delete(draft_id)
    store.audit(auth.actor(), "discarded a tariff draft", draft_id, {"name": d["name"], "author": d["author"], "mine": d["mine"]})
    return {"deleted": draft_id}


@router.post("/api/tariffs/drafts/{draft_id}/submit")
def draft_submit(draft_id: str):
    """Submits the caller's own draft for approval: one version a kind it changes, with its start and source, waiting for an administrator, all or
    none (tariffs.propose_many; each audited "proposed a tariff version"); the draft is then gone. A draft with anything to fix is refused (422)
    with every problem (`problems`). A second person approves (the owner's question Q65): nothing here publishes."""
    auth.need("edit_tariffs", "Submitting a tariff draft")
    d = _draft_or_404(draft_id)
    if not d["mine"]:
        raise Refusal(403, "A draft is submitted by its author", "not_author")
    on = today()
    found = tariff_drafts.problems(d["body"], on)
    if found:
        raise _unfit(found)
    try:
        made = tariffs.propose_many(tariff_drafts.items(d["body"], on), auth.actor(), _me())
    except (ValueError, TypeError) as exc:
        raise bad(exc) from None
    tariff_drafts.delete(draft_id)
    start = date.fromisoformat(made[0]["effective_from"])
    return {"submitted": [_row(v) for v in made], "pending": sum(1 for v in tariffs.versions() if v["status"] == "pending"),
            "message": f"Submitted: {d['name']}, from {tariffs._day_words(start)}. It waits for an administrator's approval."}


@router.post("/api/tariffs/versions/{version_id}/withdraw")
def withdraw(version_id: str, inp: dict = Body(default={})):
    """Withdraws a version waiting for approval (plan F2.2): its author, or an administrator. Status "withdrawn", with when, by whom and why; audited.
    `redraft` (an administrator's "send back") makes a draft of it for its author to change and submit again (`draft_id`)."""
    auth.need("edit_tariffs", "Withdrawing a tariff version")
    req = check(TariffWithdrawIn, inp or {})
    try:
        v = tariffs.withdraw(version_id, auth.actor(), _me(), req.reason, administrator=_administrator())
    except KeyError:
        raise Refusal(404, "No such tariff version", "not_found") from None
    except tariffs.NotYours as exc:
        raise Refusal(403, str(exc), "not_author") from None
    except ValueError as exc:
        raise bad(exc) from None
    draft_id = None
    if req.redraft:
        with store.connect() as con:
            r = con.execute("SELECT by, by_id FROM tariff_versions WHERE id = ?", (version_id,)).fetchone()
        draft_id = tariff_drafts.make(r["by_id"] or _me() or auth.actor(), r["by"] if r["by_id"] else auth.actor(), tariff_drafts.from_version(v))
    return {**_row(v), "draft_id": draft_id}


@router.post("/api/tariffs/preview")
def preview(inp: dict = Body(...)):
    """A week of 15-minute import and export prices at a site under the version in force and under a draft (plan F2.5.5 WEEK'S PRICES; F11 item 6):
    the price vectors the optimiser would read (tariffs.vectors, on the bearable-cost basis, as `GET /sites/:id/prices`), from the draft's start by
    default, with the week's mean peak and off-peak import and export rates before and after (`rates`). Times in Karachi time."""
    auth.need("see_costs", "Pricing a tariff version")
    req = check(TariffPreviewIn, inp)
    extra, what = _what_if(req)
    s = full_or_404(req.site_id)
    conn = _grid_connection(s, req.connection_id)
    from .routes_sites import PKT, PRICES_AHEAD_DAYS, PRICES_EARLIEST
    d0 = date.fromisoformat(req.start) if req.start else min(date.fromisoformat(v["effective_from"]) for v in extra)
    if not PRICES_EARLIEST <= d0 <= today() + timedelta(days=PRICES_AHEAD_DAYS):
        raise Refusal(422, f"Prices are shown for days from {PRICES_EARLIEST.isoformat()} to a year from today", "invalid")
    from . import verify
    t0 = datetime(d0.year, d0.month, d0.day, tzinfo=PKT)
    steps = req.days * 96
    legacy = conn["data"].get("regime") == "net_metering_legacy"

    def week() -> dict:
        # the billing month's imports (and a legacy site's exports) per period settle the ICP case, as on GET /sites/:id/prices; split by the peak
        # windows of the arm being priced
        tot = verify.month_totals(s["id"], conn, d0)
        return tariffs.vectors(conn, t0.astimezone(timezone.utc), steps, tot["import"], None, 0.0, tot["export"] if legacy else None, None)

    try:
        base = week()
        with tariffs.what_if(extra):
            new = week()
    except (LookupError, ValueError) as exc:
        raise bad(exc) from None

    def arm(v: dict) -> dict:
        return {"c_imp": [round(float(x), 2) for x in v["c_imp"]], "c_exp": [round(float(x), 2) for x in v["c_exp"]],
                "peak": [bool(x) for x in v["peak"]], "schedules": v["schedules"], "tariff_notices": v["tariff_notices"]}

    a, b = arm(base), arm(new)
    rates = []
    toud = any(a["peak"]) or any(b["peak"])
    for line, words, on_peak in (("peak_import", "Peak import", True), ("off_peak_import", "Off-peak import" if toud else "Import", False)):
        x = _mean([p for p, pk in zip(a["c_imp"], a["peak"]) if pk == on_peak])
        y = _mean([p for p, pk in zip(b["c_imp"], b["peak"]) if pk == on_peak])
        if x is not None or y is not None:
            rates.append({"what": line, "words": words, "in_force_rs_per_kwh": x, "draft_rs_per_kwh": y, "change_pct": _pct(x, y)})
    x, y = _mean(a["c_exp"]), _mean(b["c_exp"])
    if x or y:
        rates.append({"what": "export", "words": "Export", "in_force_rs_per_kwh": x, "draft_rs_per_kwh": y, "change_pct": _pct(x, y)})
    d = conn.get("data") or {}
    return {"site_id": s["id"], "site_name": s["name"], "connection_id": conn["id"], "tariff_category": d.get("tariff_category"),
            "start": d0.isoformat(), "days": req.days, "step_minutes": 15,
            "times": [(t0 + timedelta(minutes=15 * i)).isoformat() for i in range(steps)], "what_if": what,
            "in_force": a, "draft": b, "rates": rates,
            "changed_steps": sum(1 for i in range(steps) if a["c_imp"][i] != b["c_imp"][i] or a["c_exp"][i] != b["c_exp"][i]),
            "peak_moved_steps": sum(1 for i in range(steps) if a["peak"][i] != b["peak"][i])}


@router.post("/api/tariffs/simulate")
def simulate(inp: dict = Body(...)):
    """A site's bill rebuilt under the version in force and under a draft (plan F2.5.5 "Simulate on a site"; R8 2.3): one of its entered bills, with
    the printed total beside (`bill_id`), or a billing month of its readings where none is entered (`month`). The Bills page's own rebuild
    (verify.rebuild_bill, priced by tariffs.rebuild) with the bill's period, printed power factor and the credit the bill before carried, so the
    version in force's figure is the stored rebuild's. `reaches`: whether any of the draft's versions prices a day of the bill (a version for
    another DISCO group, regime, connection or billing month changes nothing)."""
    auth.need("see_costs", "Simulating a tariff version on a site")
    req = check(TariffSimulateIn, inp)
    extra, what = _what_if(req)
    s = full_or_404(req.site_id)
    from . import verify
    bill_id = None
    if req.bill_id is not None:
        with store.connect() as con:
            r = con.execute("SELECT * FROM bills WHERE id = ? AND site_id = ?", (req.bill_id, s["id"])).fetchone()
        if r is None:
            raise Refusal(404, "No such bill at this site", "not_found")
        conn = _grid_connection(s, r["connection_id"])
        real = json.loads(r["real"])
        first, p1 = date.fromisoformat(r["period_start"]), date.fromisoformat(r["period_end"])
        exclusive = bool(real.get("previous_reading_date"))
        p0 = date.fromisoformat(real["previous_reading_date"]) if exclusive else first
        pf, printed, bill_id = real.get("power_factor_pct"), real.get("total_rs"), r["id"]
        carried = verify._carried_into(conn["id"], first, skip_id=r["id"])
    else:
        conn = _grid_connection(s, req.connection_id)
        y, m = int(req.month[:4]), int(req.month[5:7])
        rd = tariffs._reading_day(conn)
        reading = date(y, m, min(rd, _calendar.monthrange(y, m)[1])) if rd else date(y, m, _calendar.monthrange(y, m)[1])
        first, p1 = tariffs.billing_period_for(conn, reading)
        if p1 >= today():
            raise Refusal(422, f"The bill for {reading:%B %Y} is not read yet: pick a billing month that has ended", "invalid")
        p0, exclusive, pf, printed = first, False, None, None
        carried = verify._carried_into(conn["id"], first)
    if not conn.get("plant", True):
        raise Refusal(422, "The site's readings are not this connection's meter: its bill cannot be rebuilt", "invalid")
    try:
        base = verify.rebuild_bill(s["id"], conn, p0, p1, period_start_exclusive=exclusive, power_factor_pct=pf, credit_brought_forward_rs=carried)
        with tariffs.what_if(extra):
            new = verify.rebuild_bill(s["id"], conn, p0, p1, period_start_exclusive=exclusive, power_factor_pct=pf, credit_brought_forward_rs=carried)
    except (LookupError, ValueError) as exc:
        raise bad(exc) from None
    if not base.get("completeness"):
        raise Refusal(422, "The site has no grid readings in this bill's days: pick a bill or a month with readings", "invalid")
    ids = {v["id"] for v in extra}
    x, z = base["total_rs"], new["total_rs"]
    return {"site_id": s["id"], "site_name": s["name"], "connection_id": conn["id"], "bill_id": bill_id, "billing_month": base["billing_month"],
            "first_day": base["first_day"], "last_day": base["last_day"], "days": base["days"], "completeness": base["completeness"],
            "estimated": base["completeness"] < verify.BILL_COMPLETENESS_MIN, "what_if": what,
            "reaches": any(v.get("id") in ids for v in new["versions"]),
            "bill": {"in_force_rs": x, "draft_rs": z, "printed_rs": printed, "difference_rs": round(z - x, 2), "change_pct": _pct(x, z),
                     "in_force_lines": base["lines"], "draft_lines": new["lines"]},
            "tariff_notices": new.get("tariff_notices") or []}


_ = date
