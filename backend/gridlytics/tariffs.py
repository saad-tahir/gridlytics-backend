"""The tariff and regime engine and the 1-minute bill engine (plan Part 6; R9 sections 11 to 13).

* **Versions.** Every tariff object (schedule, TOU calendar, monthly adder, tax rules, the ICP, the prosumer regimes) is an effective-dated, immutable row of
  `tariff_versions`, resolved by date (adders by billing month, one version per adder key), each with its source and a content hash. A new notification is
  proposed as `pending` (its body checked per kind) and goes live only when an administrator approves it (`approve`), which closes the version before it, or
  supersedes it outright when the new one corrects the same start.
* **The bill engine** (`rebuild`) prices a connection's 1-minute import and export series over a billing period in R9's thirteen steps and returns a line-item
  bill plus the aggregates verification needs. The counterfactual runs the same function on the replayed series; the saving is the difference line by line.
* **The vectors** (`vectors`) give the optimiser, for 192 steps of 15 minutes, the marginal rupee per kWh the bill engine itself would charge, on the
  connection's bearable-cost basis (GST and the s.235 advance excluded where the company recovers them; plan Part 4, the marginal-price rule). Each step is
  priced with the adders of the bill it will appear on (the billing month from the connection's `reading_day`, the calendar month when that is unset).
"""
from __future__ import annotations

import calendar as _calendar
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import store, tariff_data

PKT = timezone(timedelta(hours=5))


class NoTariffVersion(LookupError, ValueError):
    """No version of a tariff object is in force for the date asked. A LookupError for the callers that already catch one, and a ValueError so a route
    turns it into a refusal that says so (a 422), not a server error."""


def _hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ------------------------------------------------------------------------------------------------ versions

_SHIPPED_HASH: dict[int, str] = {}


def _shipped_hash(v: dict) -> str:
    h = _SHIPPED_HASH.get(id(v))
    if h is None:
        h = _SHIPPED_HASH[id(v)] = _hash(v["body"])
    return h


def _shipped_todo(con) -> list[tuple[dict, object]]:
    """The shipped versions a company file lacks, each with the shipped row it corrects (None for one the file has no row for at its start).

    A shipped body that changed since the file was seeded (a correction of shipped data for the same start, such as the debt-service surcharge's
    category rates) replaces the shipped row of the same start and status. The company's own decisions stand: a start the company versioned itself
    and the build never shipped, and a shipped row the company has since superseded, approved or closed out of that status, are left alone."""
    rows = con.execute("SELECT id, kind, key, effective_from, effective_to, status, hash, by FROM tariff_versions ORDER BY created, rowid").fetchall()
    at: dict[tuple, list] = {}
    for r in rows:
        at.setdefault((r["kind"], r["key"], r["effective_from"]), []).append(r)
    todo: list[tuple[dict, object]] = []
    for v in tariff_data.SHIPPED:
        same = at.get((v["kind"], v["key"], v["effective_from"]))
        if not same:
            todo.append((v, None))
            continue
        shipped = [r for r in same if r["by"] == "shipped"]
        if not shipped or any(r["hash"] == _shipped_hash(v) for r in shipped):
            continue
        old = next((r for r in reversed(shipped) if r["status"] == v["status"]), None)
        if old is not None:
            todo.append((v, old))
    return todo


def ensure_shipped() -> None:
    """Seed the shipped versions and fuel prices into the company's file the first time it is used, add a shipped version that a newer build ships
    and the file lacks (matched on kind, key and start; a start the company already holds a version for is never doubled), and apply a newer build's
    correction of a shipped body (`_shipped_todo`): the old shipped row is superseded, as an approved correction for the same start supersedes it.

    The cheap check runs unlocked; the seeding runs inside an immediate write transaction that checks again, so callers racing on a fresh file (two
    requests, a request and the scheduler tick) seed it once: the first seeds, the rest find the rows."""
    with store.connect() as con:
        todo = _shipped_todo(con)
        fuel = con.execute("SELECT 1 FROM fuel_prices LIMIT 1").fetchone() is not None
    if not todo and fuel:
        return
    with store.write_transaction() as con:
        for v, old in _shipped_todo(con):
            vid, effective_to = store.new_id(), None
            if old is not None:
                effective_to = old["effective_to"]
                day_before = (date.fromisoformat(v["effective_from"]) - timedelta(days=1)).isoformat()
                con.execute("UPDATE tariff_versions SET status = 'superseded', effective_to = ? WHERE id = ?", (day_before, old["id"]))
            con.execute("INSERT INTO tariff_versions (id, kind, key, effective_from, effective_to, known_from, status, supersedes, body, source, hash, "
                        "created, by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (vid, v["kind"], v["key"], v["effective_from"], effective_to, v["known_from"], v["status"], old["id"] if old is not None else None,
                         json.dumps(v["body"]), json.dumps(v["source"]), _shipped_hash(v), store._now(), "shipped"))
            if old is not None:
                store.audit("shipped", "corrected a shipped tariff version", f"{v['kind']}/{v['key']}",
                            {"version_id": vid, "corrects": old["id"], "effective_from": v["effective_from"]}, con=con)
        if con.execute("SELECT 1 FROM fuel_prices LIMIT 1").fetchone() is None:
            for kind, day, price in tariff_data.FUEL_PRICES:
                con.execute("INSERT OR IGNORE INTO fuel_prices (kind, valid_from, price, source) VALUES (?,?,?,?)",
                            (kind, day, price, json.dumps(tariff_data.FUEL_SOURCE[kind])))


def _ver(r) -> dict:
    d = dict(r)
    d["body"], d["source"] = json.loads(d["body"]), json.loads(d["source"])
    return d


def versions(kind: str | None = None) -> list[dict]:
    ensure_shipped()
    with store.connect() as con:
        if kind:
            rows = con.execute("SELECT * FROM tariff_versions WHERE kind = ? ORDER BY key, effective_from", (kind,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM tariff_versions ORDER BY kind, key, effective_from").fetchall()
    return [_ver(r) for r in rows]


def resolve(kind: str, key: str, on: date | str) -> dict | None:
    """The active version of (kind, key) in force on a date: the latest effective_from not after it and not closed before it (of two with the same
    start, the one created last)."""
    ensure_shipped()
    day = on.isoformat() if isinstance(on, date) else str(on)[:10]
    with store.connect() as con:
        r = con.execute("SELECT * FROM tariff_versions WHERE kind = ? AND key = ? AND status = 'active' AND effective_from <= ? "
                        "AND (effective_to IS NULL OR effective_to >= ?) ORDER BY effective_from DESC, created DESC, rowid DESC LIMIT 1",
                        (kind, key, day, day)).fetchone()
    return _ver(r) if r else None


# The keys the engine resolves for each kind (an adder's key is free: qta, fca, dss, nj, ...)
_KEYS = {"schedule": ("national",), "tou": ("xwdisco", "ke"), "tax": ("national",), "icp": ("national",), "prosumer": ("net_billing", "net_metering_legacy")}
_BASES = ("per_kw_month", "sanctioned_kw_month", "per_consumer_month")
_YM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_YMD = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
#: Rs/kWh a legacy net-metering export is priced below the import it nets: the bill is indifferent, but equal prices let the optimiser plan
#: circulating import and export in the same step at no cost (a degenerate plan with impossible flows and an inflated grid cap)
LEGACY_EXPORT_TIE_BREAK = 0.01
#: The regimes whose exports earn a credit while the connection's agreement is in force (agreement_start to agreement_end, both inclusive)
EXPORT_REGIMES = ("net_billing", "net_metering_legacy")
#: An adder whose key starts with fca or fpa is a fuel charges adjustment: the month's own "fca", or a late or bundled month's under its own key
_FCA_KEY = re.compile(r"^(fca|fpa)", re.IGNORECASE)


def _canonical_day(value, what: str = "The version's start") -> str:
    """A day as the canonical "YYYY-MM-DD". Every stored date is compared as a string, so a form Python also parses ("20261101", "2026-W45-1")
    would sort as another day: it is refused, never stored."""
    if not isinstance(value, str) or not _YMD.match(value):
        raise ValueError(f"{what} must be a date written YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"{what} must be a real date written YYYY-MM-DD") from None


def _month_bounds(ym: str) -> tuple[str, str]:
    """The first and last day ("YYYY-MM-DD") of a month "YYYY-MM"."""
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{ym}-01", f"{ym}-{_calendar.monthrange(y, m)[1]:02d}"


def validate_body(kind: str, key: str, body, effective_from: str | None = None) -> None:
    """Refuse (ValueError) a version body the engine could not price with: one approved malformed row would otherwise stop every plan, price and bill."""
    if not isinstance(body, dict):
        raise ValueError("A version's body is a JSON object")
    if kind in _KEYS and key not in _KEYS[kind]:
        raise ValueError(f"A {kind} version's key is one of: {', '.join(_KEYS[kind])}")
    if "name" in body and not isinstance(body["name"], str):
        raise ValueError("A version's name is text")
    if kind == "schedule":
        cards = body.get("cards")
        if not isinstance(cards, dict) or not cards:
            raise ValueError("A schedule needs its rate cards: a non-empty map of category to card")
        for cat, c in cards.items():
            if not isinstance(c, dict):
                raise ValueError(f"The card for {cat} is not an object")
            if c.get("fixed_basis") not in _BASES:
                raise ValueError(f"The card for {cat} needs fixed_basis: {', '.join(_BASES)}")
            if not _num(c.get("fixed")) or c["fixed"] < 0:
                raise ValueError(f"The card for {cat} needs a fixed charge (a number, 0 or more)")
            if not _num(c.get("floor_share")) or not 0 <= c["floor_share"] <= 1:
                raise ValueError(f"The card for {cat} needs floor_share between 0 and 1")
            for f in ("peak", "off_peak", "flat"):
                if c.get(f) is not None and (not _num(c[f]) or c[f] < 0):
                    raise ValueError(f"The card for {cat} has a {f} rate that is not a number of 0 or more")
            if c.get("tou") and c.get("peak") is not None:
                if not _num(c.get("off_peak")):
                    raise ValueError(f"The TOU card for {cat} needs peak and off_peak rates")
            elif not _num(c.get("flat")):
                raise ValueError(f"The card for {cat} needs a flat rate, or tou with peak and off_peak rates")
    elif kind == "adder":
        if not _num(body.get("rate")) or abs(body["rate"]) > 100:
            raise ValueError("An adder needs a rate in Rs/kWh (a number)")
        months = body.get("billing_months")
        if months != "all":
            if not isinstance(months, list) or not months or not all(isinstance(m, str) and _YM.match(m) for m in months):
                raise ValueError('An adder needs billing_months: "all" or a list of billing months as YYYY-MM')
            if effective_from and effective_from > _month_bounds(min(months))[1]:
                raise ValueError("An adder's start must fall on or before the end of its first billing month (use the first day of that month); "
                                 "it is resolved by billing month, so a later start would leave that bill without it")
        exempt = body.get("exempt", [])
        if not isinstance(exempt, list) or not all(isinstance(x, str) for x in exempt):
            raise ValueError("An adder's exempt is a list of exemptions (for example icp_units, lifeline, prepaid, or a tariff category or class)")
        rates = body.get("category_rates", {})
        if not isinstance(rates, dict) or not all(isinstance(k, str) and _num(r) and abs(r) <= 100 for k, r in rates.items()):
            raise ValueError("An adder's category_rates is a map of tariff category or class (for example agricultural) to a rate in Rs/kWh")
    elif kind == "tou":
        seasons = body.get("seasons")
        if not isinstance(seasons, list) or not seasons:
            raise ValueError("A TOU calendar needs seasons, each with months and a peak window")
        listed: list[int] = []
        for s in seasons:
            months = s.get("months") if isinstance(s, dict) else None
            peak = s.get("peak") if isinstance(s, dict) else None
            if not isinstance(months, list) or not months or not all(isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12 for m in months):
                raise ValueError("Each TOU season needs months: a list of month numbers 1 to 12")
            if not isinstance(peak, (list, tuple)) or len(peak) != 2 or not all(isinstance(x, str) and _HHMM.match(x) for x in peak):
                raise ValueError('Each TOU season needs a peak window: ["HH:MM", "HH:MM"]')
            if peak[0] == peak[1]:
                raise ValueError("Each TOU season needs a peak window that is not empty: its start and end differ")
            listed += months
        # peak membership is a function of the month for every month (R9 2.3): a month left out would have no peak at all, one listed twice two windows
        missing, twice = sorted(set(range(1, 13)) - set(listed)), sorted({m for m in listed if listed.count(m) > 1})
        if missing:
            raise ValueError(f"A TOU calendar gives every month one season: month {', '.join(map(str, missing))} has none")
        if twice:
            raise ValueError(f"A TOU calendar gives every month one season: month {', '.join(map(str, twice))} is in two seasons")
    elif kind == "icp":
        if not _num(body.get("rate")) or body["rate"] < 0:
            raise ValueError("The ICP needs its rate in Rs/kWh")
        for f in ("first_month", "last_month"):
            if not isinstance(body.get(f), str) or not _YM.match(body[f]):
                raise ValueError(f"The ICP needs {f} as YYYY-MM")
        if body["first_month"] > body["last_month"]:
            raise ValueError("The ICP's first month comes before its last")
        if not isinstance(body.get("categories"), list) or not all(isinstance(x, str) for x in body["categories"]):
            raise ValueError("The ICP needs the list of categories it covers")
    elif kind == "prosumer":
        if key == "net_billing" and (not _num(body.get("naepp")) or body["naepp"] < 0):
            raise ValueError("Net billing needs the export price naepp in Rs/kWh")
        if key == "net_metering_legacy":
            if "nappp" in body and (not _num(body["nappp"]) or body["nappp"] < 0):
                raise ValueError("Legacy net metering's nappp is the notified national average power purchase price in Rs/kWh")
            sr = body.get("surplus_rate", "nappp" if "nappp" in body else "off_peak")
            if not (sr in ("nappp", "off_peak", "peak") or (_num(sr) and sr >= 0)):
                raise ValueError('Legacy net metering\'s surplus_rate is "nappp" (the notified national average power purchase price), "off_peak", '
                                 '"peak" or a rate in Rs/kWh')
            if sr == "nappp" and "nappp" not in body:
                raise ValueError('Legacy net metering\'s surplus_rate "nappp" needs the notified rate: nappp in Rs/kWh')
            if body.get("netting", "by_period") != "by_period":
                raise ValueError("Legacy net metering is modelled with by-period netting only (netting: by_period)")
    elif kind == "tax":
        for f in ("gst", "further_tax_no_strn"):
            if f in body and (not _num(body[f]) or not 0 <= body[f] < 1):
                raise ValueError(f"The tax rule {f} is a fraction between 0 and 1")
        ed = body.get("electricity_duty", {})
        if not isinstance(ed, dict) or not all(isinstance(v, dict) and all(_num(x) and 0 <= x < 1 for x in v.values()) for v in ed.values()):
            raise ValueError("Electricity duty is a map of province to a map of class to a fraction")
        s235 = body.get("s235", {})
        if not isinstance(s235, dict) or not all(isinstance(v, dict) and all(_num(v.get(f)) for f in ("nil_upto", "mid_rate", "mid_upto", "base_above", "rate_above"))
                                                 for v in s235.values()):
            raise ValueError("Each s.235 slab needs nil_upto, mid_rate, mid_upto, base_above and rate_above")
        if not isinstance(body.get("ptv", {}), dict) or not all(_num(x) for x in body.get("ptv", {}).values()):
            raise ValueError("The PTV fee is a map of class to rupees")
        if "muct_ke" in body and not _num(body["muct_ke"]):
            raise ValueError("The MUCT fee is a number of rupees")


def propose(kind: str, key: str, effective_from: str, body: dict, source: dict, by: str) -> dict:
    """A new version from a watcher or a person, pending until an administrator approves it."""
    if kind not in ("schedule", "tou", "adder", "tax", "icp", "prosumer"):
        raise ValueError("Unknown tariff kind")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("A version needs its key")
    effective_from = _canonical_day(effective_from)
    if not isinstance(body, dict) or not isinstance(source, dict) or source.get("verified") not in ("primary", "secondary", "unverified"):
        raise ValueError("A version carries a body and a source with verified: primary, secondary or unverified")
    validate_body(kind, key, body, effective_from)
    ensure_shipped()
    vid = store.new_id()
    with store.connect() as con:
        con.execute("INSERT INTO tariff_versions (id, kind, key, effective_from, effective_to, known_from, status, supersedes, body, source, hash, created, by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (vid, kind, key, effective_from, None, store._now()[:10], "pending", None, json.dumps(body),
                                                         json.dumps(source), _hash(body), store._now(), by))
    store.audit(by, "proposed a tariff version", f"{kind}/{key}", {"effective_from": effective_from})
    return next(v for v in versions(kind) if v["id"] == vid)


def _check_schedule_covers_connections(con, cards: dict) -> None:
    """A dry run of the resolution: every category a grid connection of this company is on keeps a rate card under the new schedule."""
    used = set()
    for r in con.execute("SELECT data FROM connections").fetchall():
        try:
            d = json.loads(r[0])
        except (TypeError, ValueError):
            continue
        if isinstance(d, dict) and d.get("regime") != "off_grid" and d.get("tariff_category"):
            used.add(str(d["tariff_category"]))
    missing = sorted(used - set(cards))
    if missing:
        raise ValueError(f"The schedule has no rate card for {', '.join(missing)}, which this company's connections are on: add the card(s) before approving")


def approve(version_id: str, by: str) -> dict:
    """Make a pending version active. The active version of the same key it follows is closed the day before; one with the same start (a correction) is
    superseded outright, and a placeholder awaiting this notification is superseded. A placeholder itself is never approved: the notified version is
    drafted and approved in its place. The body is checked again before anything changes (the only mutations allowed, logged)."""
    with store.write_transaction() as con:
        r = con.execute("SELECT * FROM tariff_versions WHERE id = ?", (version_id,)).fetchone()
        if r is None:
            raise KeyError(version_id)
        if r["status"] == "awaiting_notification":
            raise ValueError("This version is a placeholder awaiting the notification: draft the notified version and approve that one")
        if r["status"] != "pending":
            raise ValueError("Only a pending version can be approved")
        # a draft stored before starts were checked: a non-canonical start would close the previous version on one day and sort as another
        _canonical_day(r["effective_from"])
        try:
            body = json.loads(r["body"])
        except (TypeError, ValueError):
            raise ValueError("The version's body is not valid JSON") from None
        validate_body(r["kind"], r["key"], body, r["effective_from"])
        if r["kind"] == "schedule":
            _check_schedule_covers_connections(con, body["cards"])
        kind, key, start = r["kind"], r["key"], r["effective_from"]
        day_before = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
        prev = con.execute("SELECT id, effective_from FROM tariff_versions WHERE kind = ? AND key = ? AND status = 'active' AND effective_from <= ? AND id != ? "
                           "ORDER BY effective_from DESC, created DESC, rowid DESC LIMIT 1", (kind, key, start, version_id)).fetchone()
        if prev and prev["effective_from"] == start:
            # a corrected notification for the same start: the version it corrects is in force on no day
            con.execute("UPDATE tariff_versions SET status = 'superseded', effective_to = ? WHERE id = ?", (day_before, prev["id"]))
        elif prev:
            con.execute("UPDATE tariff_versions SET effective_to = ? WHERE id = ? AND (effective_to IS NULL OR effective_to > ?)",
                        (day_before, prev["id"], day_before))
        nxt = con.execute("SELECT effective_from FROM tariff_versions WHERE kind = ? AND key = ? AND status = 'active' AND effective_from > ? AND id != ? "
                          "ORDER BY effective_from LIMIT 1", (kind, key, start, version_id)).fetchone()
        end = (date.fromisoformat(nxt["effective_from"]) - timedelta(days=1)).isoformat() if nxt else None
        con.execute("UPDATE tariff_versions SET status = 'superseded' WHERE kind = ? AND key = ? AND status = 'awaiting_notification' AND effective_from <= ?",
                    (kind, key, start))
        con.execute("UPDATE tariff_versions SET status = 'active', supersedes = ?, effective_to = ? WHERE id = ?",
                    (prev["id"] if prev else None, end, version_id))
    store.audit(by, "approved a tariff version", f"{r['kind']}/{r['key']}", {"version_id": version_id, "effective_from": r["effective_from"]})
    return next(v for v in versions(r["kind"]) if v["id"] == version_id)


def fuel_price(kind: str, on: date | str) -> tuple[float, dict]:
    ensure_shipped()
    day = on.isoformat() if isinstance(on, date) else str(on)[:10]
    with store.connect() as con:
        r = con.execute("SELECT price, source FROM fuel_prices WHERE kind = ? AND valid_from <= ? ORDER BY valid_from DESC LIMIT 1", (kind, day)).fetchone()
        if r is None:
            r = con.execute("SELECT price, source FROM fuel_prices WHERE kind = ? ORDER BY valid_from LIMIT 1", (kind,)).fetchone()
    return (float(r["price"]), json.loads(r["source"])) if r else (0.0, {})


def add_fuel_price(kind: str, valid_from: str, price: float, by: str) -> None:
    if kind not in ("hsd", "gas_captive", "lng_captive") or not (0 < price < 100000):
        raise ValueError("A fuel price needs a kind (hsd, gas_captive, lng_captive) and a positive price")
    valid_from = _canonical_day(valid_from, "A fuel price's start")
    with store.connect() as con:
        con.execute("INSERT INTO fuel_prices (kind, valid_from, price, source) VALUES (?,?,?,?) ON CONFLICT(kind, valid_from) DO UPDATE SET "
                    "price = excluded.price, source = excluded.source", (kind, valid_from, price, json.dumps({"kind": "manual", "ref": by, "verified": "primary"})))
    store.audit(by, "set a fuel price", kind, {"valid_from": valid_from, "price": price})


def fuel_prices(kind: str | None = None) -> list[dict]:
    ensure_shipped()
    with store.connect() as con:
        rows = con.execute("SELECT * FROM fuel_prices" + (" WHERE kind = ?" if kind else "") + " ORDER BY kind, valid_from",
                           ((kind,) if kind else ())).fetchall()
    return [{**dict(r), "source": json.loads(r["source"])} for r in rows]


# ------------------------------------------------------------------------------------------------ calendar and cards

def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def peak_window(calendar: dict, month: int) -> tuple[int, int]:
    for s in calendar["seasons"]:
        if month in s["months"]:
            return _minutes(s["peak"][0]), _minutes(s["peak"][1])
    return (-1, -1)


def is_peak_local(calendar: dict, local: datetime) -> bool:
    start, end = peak_window(calendar, local.month)
    m = local.hour * 60 + local.minute
    return start <= m < end if start <= end else (m >= start or m < end)


def calendar_for(conn: dict, on) -> dict:
    v = resolve("tou", "ke" if conn["data"].get("disco_group") == "ke" else "xwdisco", on)
    return v["body"] if v else {"seasons": []}


def card_for(category: str, on) -> tuple[dict, dict]:
    """The rate card of a category in force on a date, and the schedule version it came from."""
    v = resolve("schedule", "national", on)
    if v is None or category not in (v["body"].get("cards") or {}):
        raise NoTariffVersion(f"No rate card for {category} on {str(on)[:10]}: no tariff version in force for that day covers it")
    return v["body"]["cards"][category], v


def _energy_rate(card: dict, peak: bool) -> float:
    if card.get("tou") and card.get("peak") is not None:
        return card["peak"] if peak else card["off_peak"]
    return card["flat"]


def tax_rules(on) -> dict:
    v = resolve("tax", "national", on)
    return v["body"] if v else {}


def adders_for(billing_month: str) -> list[dict]:
    """The adders billed in a billing month ("YYYY-MM"): QTA, FCA and the debt service surcharge, one version per adder key.

    A version counts when it is active, starts on or before the month's last day, and covers the month: a version that lists its billing months covers
    exactly those (its `effective_to` is ignored: approving the next version with an early start closes it before its last listed month, which must
    not drop it from that bill), and an "all" version covers the months it is in force (not closed before the month's first day). Of several versions
    of one key that cover the month the latest start wins, and of two with the same start the one created last, so a new or corrected version
    replaces the one before it instead of stacking on it (a same-start correction also marks the old one superseded); two months' FCAs billed together
    are two keys. A malformed row is skipped rather than failing every bill."""
    ensure_shipped()
    first, last = _month_bounds(billing_month)
    with store.connect() as con:
        rows = con.execute("SELECT * FROM tariff_versions WHERE kind = 'adder' AND status = 'active' AND effective_from <= ? "
                           "ORDER BY key, effective_from, created, rowid", (last,)).fetchall()
    best: dict[str, dict] = {}
    for r in rows:
        try:
            v = _ver(r)
        except (TypeError, ValueError):
            continue
        body = v["body"] if isinstance(v["body"], dict) else {}
        months = body.get("billing_months", [])
        if months == "all":
            if v.get("effective_to") is not None and str(v["effective_to"]) < first:
                continue
        elif not (isinstance(months, list) and billing_month in months):
            continue
        if not _num(body.get("rate")):
            continue
        best[v["key"]] = v  # ordered by start, so the last one of a key wins
    return [{"key": v["key"], "id": v["id"], "name": v["key"], "exempt": [], **v["body"]} for v in best.values()]


def is_fca(adder: dict) -> bool:
    """A fuel charges adjustment (FCA, also printed FPA): an adder whose key starts with fca or fpa. Besides the month's own "fca", NEPRA bundles two
    months' FCAs on one bill or moves a late one to the next (R9 6.2), and each is stored under its own key ("fca_sep"); every one of them is an FCA."""
    return bool(_FCA_KEY.match(str(adder.get("key") or "")))


def fca_billed(billing_month: str) -> bool:
    """An FCA (under any key) is on the bill of the billing month: the condition plan 8.5 waits for before a saving is final."""
    return any(is_fca(a) for a in adders_for(billing_month))


def adder_rate(adder: dict, conn: dict | None) -> float | None:
    """An adder's Rs/kWh for a connection: None when its exemptions name the connection's tariff category or class (for example "agricultural");
    else its rate for that category, or for that class, from `category_rates` (the debt-service surcharge is 43 paisa for agricultural tube wells,
    R9 section 7); else its rate. Exemptions that name a consumer kind (lifeline, prepaid, protected, evcs) match no category or class."""
    cat = str((((conn or {}).get("data") or {}).get("tariff_category")) or "")
    cls = tariff_data.category_class(cat) if cat else ""
    exempt = adder.get("exempt") or []
    if cat and (cat in exempt or cls in exempt):
        return None
    rates = adder.get("category_rates") or {}
    for k in (cat, cls):
        if k and _num(rates.get(k)):
            return float(rates[k])
    return float(adder["rate"])


def _charged_on_icp_units(adder: dict, rate: float) -> bool:
    """An adder is charged on ICP incremental units unless it exempts them (QTA, DSS) or is a negative FCA (SRO 2409(I)/2025: positive FCAs apply to
    incremental consumption, negative FCAs do not; `negative_exempt_icp: false` keeps one on them where a bill shows it). The bill and the
    optimiser's ICP price both read this, so they agree."""
    if "icp_units" in (adder.get("exempt") or []):
        return False
    return not (is_fca(adder) and rate < 0 and adder.get("negative_exempt_icp", True))


def _reading_day(conn: dict | None) -> int | None:
    rd = ((conn or {}).get("data") or {}).get("reading_day")
    try:
        rd = int(rd)
    except (TypeError, ValueError):
        return None
    return rd if 1 <= rd <= 31 else None


def billing_month_for(conn: dict | None, day: date) -> str:
    """The billing month ("YYYY-MM", the month of the reading that closes the bill) a consumption day falls in. With the connection's `reading_day`
    set, a day after the reading belongs to the next month's bill; without it the bill is the calendar month."""
    rd = _reading_day(conn)
    if rd is None or day.day <= min(rd, _calendar.monthrange(day.year, day.month)[1]):
        return f"{day.year:04d}-{day.month:02d}"
    y, m = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return f"{y:04d}-{m:02d}"


def billing_period_for(conn: dict | None, day: date) -> tuple[date, date]:
    """The first and last consumption day of the bill a day falls in: the day after the previous reading, and the reading date (the calendar month
    without a `reading_day`). The month-to-date counters and the MDI month follow these bounds."""
    bm = billing_month_for(conn, day)
    y, m = int(bm[:4]), int(bm[5:7])
    rd = _reading_day(conn)
    if rd is None:
        return date(y, m, 1), date(y, m, _calendar.monthrange(y, m)[1])
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    reading = date(y, m, min(rd, _calendar.monthrange(y, m)[1]))
    previous = date(py, pm, min(rd, _calendar.monthrange(py, pm)[1]))
    return previous + timedelta(days=1), reading


def icp_rule(on) -> dict | None:
    v = resolve("icp", "national", on)
    if v is None:
        return None
    b = v["body"]
    month = str(on)[:7]
    return b if b["first_month"] <= month <= b["last_month"] else None


def prosumer(regime: str, on) -> dict | None:
    v = resolve("prosumer", "net_billing" if regime == "net_billing" else "net_metering_legacy", on)
    return v["body"] if v else None


def agreement_window(conn: dict | None) -> tuple[date | None, date | None] | None:
    """A connection's export agreement as (first day, last day), each None when it was not entered; None when a stored date is not a real date written
    YYYY-MM-DD."""
    data = (conn or {}).get("data") or {}
    days: list[date | None] = []
    for f in ("agreement_start", "agreement_end"):
        v = data.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            days.append(None)
            continue
        if not isinstance(v, str) or not _YMD.match(v.strip()):
            return None
        try:
            days.append(date.fromisoformat(v.strip()))
        except ValueError:
            return None
    return days[0], days[1]


def agreement_state(conn: dict | None, on: date) -> str | None:
    """The state on a day of the export agreement of a connection under net billing or legacy net metering: "active", "not_started" (before
    agreement_start), "ended" (after agreement_end) or "unreadable" (a stored date that is not a real date: exports earn nothing until it is
    corrected); None under another regime. A date not entered leaves that side open. What the connection is billed under on the day is
    `regime_on`."""
    if conn is None or (conn.get("data") or {}).get("regime") not in EXPORT_REGIMES:
        return None
    w = agreement_window(conn)
    if w is None:
        return "unreadable"
    start, end = w
    if start is not None and on < start:
        return "not_started"
    if end is not None and on > end:
        return "ended"
    return "active"


def regime_on(conn: dict | None, on: date) -> str:
    """The regime a connection is billed under on a day. Its own regime while its agreement is in force (or when it has no export regime). After a
    legacy net-metering agreement ends, net billing: the site moves to it on expiry (R9 section 8, SRO 547(I)/2026; KE's FAQ: after expiry the
    surplus is valued at NAEPP), so its exports are credited at the NAEPP and nothing is netted. Before an agreement starts, after a net-billing
    agreement ends, or with a date that cannot be read: "none", a connection whose export earns nothing (plan 9.3 UI-03: an expired regime's
    export value falls to zero until a new agreement is entered)."""
    regime = ((conn or {}).get("data") or {}).get("regime", "none")
    state = agreement_state(conn, on)
    if state is None or state == "active":
        return regime
    if state == "ended" and regime == "net_metering_legacy":
        return "net_billing"
    return "none"


def export_credited(conn: dict | None, on: date) -> bool:
    """Exports earn a credit on the day: the connection is billed under net billing or legacy net metering that day (`regime_on`)."""
    return regime_on(conn, on) in EXPORT_REGIMES


def _regime_minutes(conn: dict, ts: np.ndarray) -> np.ndarray:
    """The regime each minute (epoch seconds of its start) is billed under, by its Pakistan day (`regime_on`)."""
    out = np.full(len(ts), "none", dtype=object)
    day_of = (np.asarray(ts, dtype=np.int64) + 5 * 3600) // 86400  # Pakistan days (UTC+5 all year) since 1970-01-01
    for k in np.unique(day_of):
        out[day_of == k] = regime_on(conn, date(1970, 1, 1) + timedelta(days=int(k)))
    return out


def icp_baseline(conn: dict, month: int, year: int, period: str) -> float | None:
    """The connection's ICP baseline for a calendar month and period, escalated 2.8 percent a package year; None when not eligible."""
    icp = conn["data"].get("icp") or {}
    if not icp.get("eligible"):
        return None
    table = icp.get("peak" if period == "peak" else "off_peak") or {}
    base = table.get(str(month))
    if base is None:
        return None
    # package year n: Dec 2025-Nov 2026 is year 1
    n = 1 + max(0, (year * 12 + month - (2025 * 12 + 12)) // 12)
    return float(base) * (1.028 ** n)


# ------------------------------------------------------------------------------------------------ the bill engine (R9 section 13)

def _tax_factor_energy(conn: dict, on) -> dict:
    """The multipliers the bill applies to variable charges, split into what the company bears and what it recovers."""
    data = conn["data"]
    tax = tax_rules(on)
    cls = tariff_data.category_class(data["tariff_category"])
    prov = (data.get("tax") or {}).get("province", "punjab")
    ed = tax.get("electricity_duty", {}).get(prov, {}).get(cls, 0.015)
    gst = tax.get("gst", 0.18)
    flags = data.get("tax") or {}
    further = tax.get("further_tax_no_strn", 0.03) if not flags.get("strn", True) and cls in ("industrial", "commercial") else 0.0
    slab = tax.get("s235", {}).get("industrial" if cls == "industrial" else "commercial")
    s235 = (slab or {}).get("rate_above", 0.05)
    return {"ed": ed, "gst": gst, "further": further, "s235_marginal": s235, "gst_recoverable": bool(flags.get("gst_recoverable", True)),
            "s235_adjustable": bool(flags.get("s235_adjustable", True)), "class": cls,
            # the bill charges s.235 only to industrial and commercial connections (rebuild step 11)
            "s235_applies": bool(slab) and cls in ("industrial", "commercial")}


def _tax_stack(tf: dict) -> float:
    """The bearable rupees per rupee of a pre-tax charge, from the bill's own formula: GST and further tax both on the same base (added, not multiplied),
    and s.235 on the bill amount including them, where the class pays it; GST and s.235 dropped where the company recovers them."""
    f = 1.0 + tf["further"] + (0.0 if tf["gst_recoverable"] else tf["gst"])
    if tf["s235_applies"] and not tf["s235_adjustable"]:
        f += tf["s235_marginal"] * (1.0 + tf["gst"] + tf["further"])
    return f


def _seasonal_factor(data: dict, month: int) -> float:
    """Tariff F: the fixed charge times the seasonal multiplier in the running season and nothing outside it (R9 section 4); 1 for other connections."""
    season = [int(m) for m in (data.get("season_months") or [])]
    if not season:
        return 1.0
    return float(data.get("seasonal_multiplier", 1.0)) if int(month) in season else 0.0


def _legacy_surplus_rate(card: dict, pro: dict | None) -> float:
    """The rate a legacy net-metering surplus is credited at, from the prosumer version (no duty or tax on it): the notified NAPPP ("nappp", the
    shipped default from 2026: NEPRA accounts net-metering exports at it, SRO 1501(I)/2026 para 23), a fixed rate, or the off-peak (or peak) rate of
    the connection's tariff where a legacy bill shows that (the 2015 practice R9 8.3 reports, unverified)."""
    pro = pro or {}
    sr = pro.get("surplus_rate", "nappp" if _num(pro.get("nappp")) else "off_peak")
    if _num(sr):
        return float(sr)
    if sr == "nappp" and _num(pro.get("nappp")):
        return float(pro["nappp"])
    return _energy_rate(card, sr == "peak")


def rebuild(ts_utc: np.ndarray, import_kw: np.ndarray, export_kw: np.ndarray, conn: dict, period_start: date, period_end: date,
            energy_scalar: float = 1.0, mdi_scalar: float = 1.0, period_start_exclusive: bool = False) -> dict:
    """The connection's bill over the consumption days [period_start, period_end], both inclusive, from 1-minute series.

    `period_start` is the first day billed, the day after the previous meter reading, and `period_end` the reading date. A caller holding the two
    reading dates as printed on the DISCO bill (R9 section 13: start exclusive, end inclusive) passes `period_start_exclusive=True`, so the previous
    reading day is not billed twice and the proration counts the real reading interval.

    `ts_utc` holds epoch seconds of each minute's start (UTC); `import_kw` and `export_kw` the mean power of that minute. Returns the line items, the totals
    (as billed, and as the company bears them) and the aggregates verification reads."""
    data = conn["data"]
    on = period_end
    if period_start_exclusive:
        period_start = period_start + timedelta(days=1)
    card, schedule = card_for(data["tariff_category"], on)
    cal = calendar_for(conn, on)
    tf = _tax_factor_energy(conn, on)
    billing_month = period_end.strftime("%Y-%m")
    t0 = datetime(period_start.year, period_start.month, period_start.day, tzinfo=PKT).timestamp()
    t1 = (datetime(period_end.year, period_end.month, period_end.day, tzinfo=PKT) + timedelta(days=1)).timestamp()
    mask = (ts_utc >= t0) & (ts_utc < t1)
    ts, imp, exp = ts_utc[mask], np.nan_to_num(import_kw[mask]) * energy_scalar, np.nan_to_num(export_kw[mask]) * energy_scalar
    # 1. period tagging, by the calendar month of each minute in Pakistan time
    peak = np.zeros(len(ts), dtype=bool)
    if card.get("tou") and card.get("peak") is not None:
        for i, t in enumerate(ts):
            peak[i] = is_peak_local(cal, datetime.fromtimestamp(float(t), PKT))
    # 2. energy by period
    e_imp = {"peak": float(imp[peak].sum() / 60.0), "off_peak": float(imp[~peak].sum() / 60.0)}
    e_exp = {"peak": float(exp[peak].sum() / 60.0), "off_peak": float(exp[~peak].sum() / 60.0)}
    # the regime each day is billed under (regime_on): legacy netting takes the exports of days inside a legacy agreement, net billing credits those
    # of days under net billing (a legacy agreement's days after it ends among them), and an export on any other day earns nothing
    reg = _regime_minutes(conn, ts)
    exp_l, exp_nb = np.where(reg == "net_metering_legacy", exp, 0.0), np.where(reg == "net_billing", exp, 0.0)
    e_exp_l = {"peak": float(exp_l[peak].sum() / 60.0), "off_peak": float(exp_l[~peak].sum() / 60.0)}
    # 3. maximum demand: 30-minute fixed blocks aligned to the hour
    interval = int(data.get("mdi_interval_min", 30))
    mdi = 0.0
    if len(ts):
        blocks = ((ts - ts[0] // 3600 * 3600) // (interval * 60)).astype(np.int64)
        sums = np.bincount(blocks - blocks.min(), weights=imp)
        counts = np.bincount(blocks - blocks.min())
        means = sums[counts > 0] / interval
        mdi = float(means.max()) * mdi_scalar if len(means) else 0.0
    sl = float(data["sanctioned_load_kw"])
    # legacy net metering nets by period first (R9 section 8.3; marked unverified), the exports of days inside the agreement only
    surplus_kwh = 0.0
    if bool((reg == "net_metering_legacy").any()):
        for p in ("peak", "off_peak"):
            net = e_imp[p] - e_exp_l[p]
            surplus_kwh += max(0.0, -net)
            e_imp[p] = max(0.0, net)
    # 4. the ICP split per period by monthly totals
    icp = icp_rule(on)
    incremental = {"peak": 0.0, "off_peak": 0.0}
    if icp and data["tariff_category"] in icp["categories"]:
        for p in ("peak", "off_peak"):
            base = icp_baseline(conn, period_end.month, period_end.year, p)
            if base is not None:
                incremental[p] = max(0.0, e_imp[p] - base)
    inc_total = incremental["peak"] + incremental["off_peak"]
    # 5. energy charges
    lines = []
    energy = 0.0
    for p in ("peak", "off_peak") if card.get("tou") and card.get("peak") is not None else ("off_peak",):
        units = e_imp[p] if card.get("tou") and card.get("peak") is not None else e_imp["peak"] + e_imp["off_peak"]
        inc = incremental[p] if card.get("tou") and card.get("peak") is not None else inc_total
        base_units = units - inc
        rate = _energy_rate(card, p == "peak")
        energy += base_units * rate + inc * (icp["rate"] if icp else 0.0)
        lines.append({"line": f"Energy {p.replace('_', '-')}" if card.get("tou") and card.get("peak") is not None else "Energy", "units_kwh": round(base_units, 3),
                      "rate": rate, "amount_rs": round(base_units * rate, 2)})
        if inc > 0:
            lines.append({"line": f"ICP incremental {p.replace('_', '-')}", "units_kwh": round(inc, 3), "rate": icp["rate"], "amount_rs": round(inc * icp["rate"], 2)})
    # 6. fixed charges by basis, with Tariff F's seasonal multiplier
    days = (period_end - period_start).days + 1
    prorate = days / 30.0 if abs(days - 30) > 4 else 1.0
    if card["fixed_basis"] == "per_kw_month":
        billing_demand = max(card["floor_share"] * sl, mdi)
        fixed = card["fixed"] * billing_demand * prorate
    elif card["fixed_basis"] == "sanctioned_kw_month":
        billing_demand = sl
        fixed = card["fixed"] * sl * prorate
    else:
        billing_demand = 0.0
        fixed = card["fixed"] * prorate
    fixed *= _seasonal_factor(data, period_end.month)
    lines.append({"line": "Fixed charges", "billing_demand_kw": round(billing_demand, 2), "mdi_kw": round(mdi, 2), "rate": card["fixed"],
                  "amount_rs": round(fixed, 2)})
    # 7. adders by billing month, with their exemptions
    total_units = e_imp["peak"] + e_imp["off_peak"]
    adders = 0.0
    for a in adders_for(billing_month):
        rate = adder_rate(a, conn)
        if rate is None:  # the adder exempts the connection's category or class
            continue
        units = total_units if _charged_on_icp_units(a, rate) else total_units - inc_total
        amt = rate * units
        adders += amt
        lines.append({"line": a.get("name", a["key"]), "units_kwh": round(units, 3), "rate": rate, "amount_rs": round(amt, 2)})
    # 8. electricity duty on the variable subtotal
    duty = tf["ed"] * (energy + adders)
    lines.append({"line": "Electricity duty", "rate": tf["ed"], "amount_rs": round(duty, 2)})
    # 9. export credit: the legacy surplus at its rate, and the exports of days under net billing at the NAEPP
    credits: list[tuple[str, float, float]] = []  # (line, units, rate)
    if surplus_kwh > 0:
        credits.append(("legacy", surplus_kwh, _legacy_surplus_rate(card, prosumer("net_metering_legacy", on))))
    exported_nb = float(exp_nb.sum() / 60.0)
    if exported_nb > 0:
        credits.append(("net_billing", exported_nb, (prosumer("net_billing", on) or {"naepp": 0.0})["naepp"]))
    credit = sum(u * r for _, u, r in credits)
    # 10. GST (the credit is netted after taxes: DISCO practice, R9 section 13 step 10, to confirm on a prosumer bill)
    gst = tf["gst"] * (energy + fixed + adders + duty)
    lines.append({"line": "GST", "rate": tf["gst"], "amount_rs": round(gst, 2)})
    further = tf["further"] * (energy + fixed + adders + duty)
    if further:
        lines.append({"line": "Further tax", "rate": tf["further"], "amount_rs": round(further, 2)})
    # 11. income tax s.235 on the bill amount
    tax = tax_rules(on)
    slab = tax.get("s235", {}).get("industrial" if tf["class"] == "industrial" else "commercial")
    bill_amount = energy + fixed + adders + duty + gst + further
    s235 = 0.0
    if slab and tf["class"] in ("industrial", "commercial"):
        if bill_amount > slab["mid_upto"]:
            s235 = slab["base_above"] + slab["rate_above"] * (bill_amount - slab["mid_upto"])
        elif bill_amount > slab["nil_upto"]:
            s235 = slab["mid_rate"] * bill_amount
    lines.append({"line": "Income tax (s.235)", "amount_rs": round(s235, 2)})
    # 12. flat items
    flat = float(tax.get("ptv", {}).get(tf["class"], 0))
    if data.get("disco_group") == "ke" and tf["class"] in ("industrial", "commercial"):
        flat += float(tax.get("muct_ke", 0))
    if flat:
        lines.append({"line": "Flat fees (PTV, MUCT)", "amount_rs": round(flat, 2)})
    for kind, units, rate in credits:
        if units * rate:
            # one regime in the period: "Export credit"; a legacy agreement that ends inside it bills its surplus and the net-billing exports apart
            name = "Export credit" if len(credits) == 1 else ("Export credit (legacy net metering)" if kind == "legacy" else "Export credit (net billing)")
            lines.append({"line": name, "units_kwh": round(units, 3), "rate": rate, "amount_rs": round(-units * rate, 2)})
    total = max(0.0, bill_amount + s235 + flat - credit)
    recovered = (gst if tf["gst_recoverable"] else 0.0) + (s235 if tf["s235_adjustable"] else 0.0)
    return {"lines": lines, "total_rs": round(total, 2), "bearable_rs": round(total - recovered, 2), "recovered_taxes_rs": round(recovered, 2),
            "energy_rs": round(energy + adders, 2), "demand_rs": round(fixed, 2), "credits_rs": round(credit, 2),
            "import_kwh": e_imp, "export_kwh": e_exp, "icp_incremental_kwh": incremental, "mdi_kw": round(mdi, 2),
            "schedule_version": schedule["id"], "schedule_hash": schedule["hash"], "billing_month": billing_month, "days": days,
            "first_day": period_start.isoformat(), "last_day": period_end.isoformat()}


# ------------------------------------------------------------------------------------------------ the optimiser's vectors (plan Part 6.4)

def energy_factor(conn: dict, on) -> float:
    """The multiplier from the base rate to the bearable marginal rupee per kWh of variable charges: the duty, then the bill's tax stack."""
    tf = _tax_factor_energy(conn, on)
    return (1.0 + tf["ed"]) * _tax_stack(tf)


def fixed_factor(conn: dict, on) -> float:
    """The multiplier from a fixed charge to the bearable rupees it costs (no duty on fixed charges)."""
    return _tax_stack(_tax_factor_energy(conn, on))


def vectors(conn: dict | None, t0_utc: datetime, steps: int = 192, mtd_import: dict | None = None, rest_net_import: dict | None = None,
            g_max_kw: float = 1e6, mtd_export: dict | None = None, rest_net_export: dict | None = None) -> dict:
    """The 15-minute price vectors for the optimiser from `t0_utc`: import and export prices (Rs/kWh, bearable basis), the peak flag, the MDI terms and
    the ICP case per period. A site with no connection (off-grid) gets zero prices and grid availability 0.

    Each step carries the adders of its billing month (`billing_month`, from the connection's `reading_day`); the ICP case, the MDI rate and the legacy
    netting case are those of the current billing month, whose steps end at `billing_month_end_step`. `mtd_import` and `rest_net_import` are the
    current billing month's imports per period so far and forecast for the rest; for legacy net metering `mtd_export` and `rest_net_export` are the
    exports alike (left out, the periods are taken to end the month as net imports). A legacy export is priced LEGACY_EXPORT_TIE_BREAK below the
    import price of its step (the bill prices both alike), so equal prices never make circulating import and export free; `export_tie_break` holds
    that amount per step (0 where it does not apply), for the ledger to add back.

    Each day is priced under the regime it is billed under (`regime_on`): an export earns nothing before an agreement starts, after a net-billing
    agreement ends or with a date that cannot be read (as on a connection without a regime), and earns the NAEPP after a legacy agreement ends (the
    site moves to net billing). Legacy netting (the surplus credit and the ICP case on net imports) applies only while the legacy agreement is in
    force on the first step's day."""
    h = 0.25
    out = {"c_imp": np.zeros(steps), "c_exp": np.zeros(steps), "peak": np.zeros(steps, dtype=bool), "month": np.zeros(steps, dtype=int),
           "local_hour": np.zeros(steps), "grid": np.ones(steps), "icp": {}, "mdi": None, "billing_month": [], "billing_month_end_step": steps,
           "export_tie_break": np.zeros(steps)}
    times = [t0_utc + timedelta(minutes=15 * i) for i in range(steps)]
    locs = [t.astimezone(PKT) for t in times]
    for i, loc in enumerate(locs):
        out["month"][i] = loc.month
        out["local_hour"][i] = loc.hour + loc.minute / 60.0
    bm_by_day: dict[date, str] = {}
    for loc in locs:
        if loc.date() not in bm_by_day:
            bm_by_day[loc.date()] = billing_month_for(conn, loc.date())
    bms = [bm_by_day[loc.date()] for loc in locs]
    out["billing_month"] = bms
    if conn is None or conn["data"].get("regime") == "off_grid":
        out["grid"][:] = 0.0
        return out
    data = conn["data"]
    on = t0_utc.astimezone(PKT).date()
    card, _ = card_for(data["tariff_category"], on)
    cal = calendar_for(conn, on)
    ef = energy_factor(conn, on)
    tou = bool(card.get("tou") and card.get("peak") is not None)
    bm0 = bms[0] if bms else billing_month_for(conn, on)
    in_bm0 = [b == bm0 for b in bms]
    out["billing_month_end_step"] = next((i for i, x in enumerate(in_bm0) if not x), steps)
    # each adder at the connection's own rate (its category's, or none where it is exempt); on ICP units only those the bill charges there
    rated = {bm: [(a, r) for a in adders_for(bm) for r in (adder_rate(a, conn),) if r is not None] for bm in dict.fromkeys(bms or [bm0])}
    add_all = {bm: sum(r for _, r in ads) for bm, ads in rated.items()}
    add_icp = {bm: sum(r for a, r in ads if _charged_on_icp_units(a, r)) for bm, ads in rated.items()}
    bm0_day = date(int(bm0[:4]), int(bm0[5:7]), 1)
    icp = icp_rule(bm0_day)
    # the regime each day is billed under (regime_on): its own inside the agreement, net billing after a legacy agreement ends, else none
    reg_day = {d: regime_on(conn, d) for d in bm_by_day}
    legacy = regime_on(conn, on) == "net_metering_legacy"

    def q_net(p: str) -> float:
        """The current billing month's total for a period: imports so far plus the forecast rest, net of exports where the bill nets them."""
        q = (mtd_import or {}).get(p, 0.0) + (rest_net_import or {}).get(p, 0.0)
        if legacy:
            q -= (mtd_export or {}).get(p, 0.0) + (rest_net_export or {}).get(p, 0.0)
        return q

    # ICP case per period (plan Part 4.3): above, below, or the uncertain middle that gets a month-level binary; the baseline is the billing month's
    for p in ("peak", "off_peak"):
        base = icp_baseline(conn, bm0_day.month, bm0_day.year, p) if icp and data["tariff_category"] in icp["categories"] else None
        if base is None:
            continue
        q = q_net(p)
        steps_in_p = sum(1 for i, loc in enumerate(locs) if in_bm0[i] and is_peak_local(cal, loc) == (p == "peak"))
        swing = g_max_kw * steps_in_p * h
        case = "above" if q - base > swing else "below" if base - q > swing else "uncertain"
        out["icp"][p] = {"case": case, "baseline_kwh": base, "q_rest_kwh": q - (mtd_import or {}).get(p, 0.0) + (mtd_import or {}).get(p, 0.0),
                         "mtd_kwh": (mtd_import or {}).get(p, 0.0), "q_kwh": q, "rate": (icp["rate"] + add_icp[bm0]) * ef}
    # legacy net metering nets by period (rebuild step 3): a period that ends the month as a net import prices an exported kWh like an imported one,
    # with its adders, duty and taxes; one that ends in surplus moves only the credit, at the bare surplus rate (netted after taxes), both ways
    naepp = (prosumer("net_billing", on) or {"naepp": 0.0})["naepp"] if "net_billing" in reg_day.values() else 0.0
    surplus: dict[str, bool] = {}
    s_rate = 0.0
    if legacy:
        s_rate = _legacy_surplus_rate(card, prosumer("net_metering_legacy", on))
        if tou:
            surplus = {p: q_net(p) < 0 for p in ("peak", "off_peak")}
        else:
            total = q_net("peak") + q_net("off_peak")
            surplus = {"peak": total < 0, "off_peak": total < 0}
    for i, loc in enumerate(locs):
        pk = is_peak_local(cal, loc) if tou else False
        out["peak"][i] = pk
        p = "peak" if pk else "off_peak"
        bm = bms[i]
        icp_case = out["icp"].get(p, {}).get("case") if in_bm0[i] else None
        if icp_case == "above":
            c_imp = (icp["rate"] + add_icp[bm]) * ef
        else:
            c_imp = (_energy_rate(card, pk) + add_all[bm]) * ef
        regime = reg_day[loc.date()]
        if regime == "net_billing":
            c_exp = naepp
        elif regime != "net_metering_legacy":
            c_exp = 0.0  # no export regime, or no agreement in force that day
        else:
            # legacy: the bill prices both ways alike; the export side sits LEGACY_EXPORT_TIE_BREAK below so the optimiser never plans import and
            # export at once
            if legacy and in_bm0[i] and surplus.get(p):
                c_imp = s_rate
            c_exp = c_imp - LEGACY_EXPORT_TIE_BREAK
            out["export_tie_break"][i] = LEGACY_EXPORT_TIE_BREAK
        out["c_imp"][i], out["c_exp"][i] = c_imp, c_exp
    if card["fixed_basis"] == "per_kw_month":
        out["mdi"] = {"rate_rs_per_kw": card["fixed"] * fixed_factor(conn, on) * _seasonal_factor(data, bm0_day.month),
                      "floor_kw": card["floor_share"] * float(data["sanctioned_load_kw"]),
                      "interval_steps": max(1, int(data.get("mdi_interval_min", 30)) // 15)}
    return out


def provenance() -> list[dict]:
    """Every shipped and added number's source and status, for the tariff page's provenance view."""
    return [{"id": v["id"], "kind": v["kind"], "key": v["key"], "effective_from": v["effective_from"], "effective_to": v["effective_to"], "status": v["status"],
             "name": v["body"].get("name", v["key"]), "source": v["source"], "hash": v["hash"]} for v in versions()]
