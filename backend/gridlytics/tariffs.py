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

import bisect
import calendar as _calendar
import hashlib
import json
import math
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
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

_SHIPPED_HASH: dict[int, tuple[dict, str]] = {}


def _shipped_hash(v: dict) -> str:
    """The hash of a shipped version's body, computed once per process (the shipped bodies never change while it runs)."""
    hit = _SHIPPED_HASH.get(id(v))
    if hit is None or hit[0] is not v:
        hit = _SHIPPED_HASH[id(v)] = (v, _hash(v["body"]))
    return hit[1]


def _shipped_todo(con) -> list[tuple[dict, object]]:
    """The shipped versions a company file lacks, each with the shipped row it corrects (None for one the file has no row for at its start).

    A shipped body that changed since the file was seeded (a correction of shipped data for the same start, such as the debt-service surcharge's
    category rates) replaces the shipped row of the same start and status. The company's own decisions stand: a start the company versioned itself
    and the build never shipped, and a shipped row the company has since superseded, approved, withdrawn or closed out of that status, are left alone.

    A version the company drafted and then withdrew (UX K32 check) is no decision about its start: it is in force on no day, so a newer build's
    shipped version at that start still reaches the company (else its bills would go without, for example, that month's FCA)."""
    rows = con.execute("SELECT id, kind, key, effective_from, effective_to, status, hash, by FROM tariff_versions ORDER BY created, rowid").fetchall()
    at: dict[tuple, list] = {}
    for r in rows:
        if r["status"] == "withdrawn" and r["by"] != "shipped":
            continue
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


def _fuel_todo(con) -> list[tuple[str, str, float]]:
    """The shipped fuel prices (tariff_data.FUEL_PRICES) the company file should take: every one on a fresh file, and on a file seeded by an earlier
    build each shipped day it holds no price for, unless the price in force on that day is one the company entered (source kind manual), which a
    shipped price never displaces from the days it prices (round 7, tariffs review: the prices went only into a file with no fuel price, so a later
    build's OGRA prices never reached an existing company). A row the file holds is never changed."""
    held: dict[str, list[tuple[str, bool]]] = {}
    for r in con.execute("SELECT kind, valid_from, source FROM fuel_prices ORDER BY kind, valid_from"):
        try:
            entered = (json.loads(r["source"]) or {}).get("kind") == "manual"
        except (TypeError, ValueError, AttributeError):
            entered = True                                  # a source that cannot be read is taken as the company's own
        held.setdefault(r["kind"], []).append((r["valid_from"], entered))
    todo = []
    for kind, day, price in sorted(tariff_data.FUEL_PRICES):
        rows = held.setdefault(kind, [])
        days = [d for d, _ in rows]
        if day in days:
            continue
        i = bisect.bisect_right(days, day) - 1
        if i >= 0 and rows[i][1]:
            continue                                        # the company's own price is in force that day
        todo.append((kind, day, price))
        bisect.insort(rows, (day, False))
    return todo


#: The company files whose shipped library this process has checked, each as (path, device, inode): a lookup checks a file the first time this
#: process reads it, not on every call (audit tests-and-perf-25: the check cost a connection and a scan of every version, 6 to 8 times a price
#: vector). The shipped library cannot change while a process runs, so a newer build checks each file again when it starts. The app never deletes a
#: company's file and a backup is restored with the server stopped; a file moved aside and made again at its path has a new inode and is checked
#: again. `ensure_shipped` itself always checks.
_SEEDED: set[tuple[str, int, int]] = set()


def _file_key() -> tuple[str, int, int] | None:
    path = store.db_path()
    try:
        st = os.stat(path)
    except OSError:
        return None  # no file yet: the check makes it
    return str(path), st.st_dev, st.st_ino


def _seeded() -> None:
    """Check the current company file's shipped library once per process (`ensure_shipped`), before a lookup reads it."""
    key = _file_key()
    if key is None or key not in _SEEDED:
        ensure_shipped()


def ensure_shipped() -> None:
    """Seed the shipped versions and fuel prices into the company's file the first time it is used, add a shipped version that a newer build ships
    and the file lacks (matched on kind, key and start; a start the company already holds a version for is never doubled), and apply a newer build's
    correction of a shipped body (`_shipped_todo`): the old shipped row is superseded, as an approved correction for the same start supersedes it.
    A newer build's fuel prices are added too, never over a price the company entered (`_fuel_todo`).
    The lookups run it once per file and process (`_seeded`); called directly it always checks.

    The cheap check runs unlocked; the seeding runs inside an immediate write transaction that checks again, so callers racing on a fresh file (two
    requests, a request and the scheduler tick) seed it once: the first seeds, the rest find the rows."""
    with store.connect() as con:
        todo = _shipped_todo(con)
        fuel = _fuel_todo(con)
    if not todo and not fuel:
        _remember_seeded()
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
        fresh = con.execute("SELECT 1 FROM fuel_prices LIMIT 1").fetchone() is None
        added = _fuel_todo(con)
        for kind, day, price in added:
            con.execute("INSERT OR IGNORE INTO fuel_prices (kind, valid_from, price, source) VALUES (?,?,?,?)",
                        (kind, day, price, json.dumps(tariff_data.FUEL_SOURCE[kind])))
        if added and not fresh:
            store.audit("shipped", "added the shipped fuel prices", ", ".join(sorted({k for k, _, _ in added})),
                        {"days": [[k, d] for k, d, _ in added]}, con=con)
    _remember_seeded()


def _remember_seeded() -> None:
    key = _file_key()
    if key is not None:
        _SEEDED.add(key)


def _ver(r) -> dict:
    d = dict(r)
    d["body"], d["source"] = json.loads(d["body"]), json.loads(d["source"])
    return d


def versions(kind: str | None = None) -> list[dict]:
    _seeded()
    with store.connect() as con:
        if kind:
            rows = con.execute("SELECT * FROM tariff_versions WHERE kind = ? ORDER BY key, effective_from", (kind,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM tariff_versions ORDER BY kind, key, effective_from").fetchall()
    return [_ver(r) for r in rows]


#: The versions priced as if they were approved, inside `what_if` (UX backend ask K32: the tariff editor's week of prices and its bill simulated on a
#: site under a draft, or under a version waiting for approval). Empty everywhere else: the library is read as it stands
_WHAT_IF: ContextVar[tuple[dict, ...]] = ContextVar("gridlytics_tariff_what_if", default=())


@contextmanager
def what_if(extra: list[dict]):
    """Inside the block every lookup (resolve, the day runs, the adders of a billing month, the notices) prices as if `extra` were approved now, in
    their order, by `approve`'s own rules and with nothing written: each is in force from its start up to the day before the next active version of
    its kind and key; the version it follows is closed the day before its start, or, with the same start (a correction), in force on no day; and a
    placeholder awaiting a notification it answers no longer waits. Each is a version as `versions` gives one: id, kind, key, effective_from, body,
    source, hash, created and by."""
    token = _WHAT_IF.set(tuple(_WHAT_IF.get()) + tuple(extra))
    try:
        yield
    finally:
        _WHAT_IF.reset(token)


def _what_if_of(kind: str, key: str | None = None) -> list[dict]:
    return [w for w in _WHAT_IF.get() if w["kind"] == kind and (key is None or w["key"] == key)]


def _active_versions(kind: str, key: str | None = None) -> list[dict]:
    """The active versions of a kind (of one key, or of every key) as approving the what-if versions of that kind would leave them (`what_if`), in
    the order of their making, a what-if version last. A row whose JSON cannot be read is left out."""
    _seeded()
    with store.connect() as con:
        if key is None:
            rows = con.execute("SELECT * FROM tariff_versions WHERE kind = ? AND status = 'active' ORDER BY created, rowid", (kind,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM tariff_versions WHERE kind = ? AND key = ? AND status = 'active' ORDER BY created, rowid",
                               (kind, key)).fetchall()
    vs: list[dict] = []
    for r in rows:
        try:
            vs.append(_ver(r))
        except (TypeError, ValueError):
            continue
    for w in _what_if_of(kind, key):
        start = w["effective_from"]
        day_before = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
        same = [(i, v) for i, v in enumerate(vs) if v["key"] == w["key"]]
        before = [(v["effective_from"], i) for i, v in same if v["effective_from"] <= start]
        if before:
            i = max(before)[1]
            prev = vs[i]
            if prev["effective_from"] == start:
                del vs[i]                                    # a correction for the same start: the version it corrects is in force on no day
            elif prev["effective_to"] is None or prev["effective_to"] > day_before:
                vs[i] = {**prev, "effective_to": day_before}
        later = [v["effective_from"] for _, v in same if v["effective_from"] > start]
        end = (date.fromisoformat(min(later)) - timedelta(days=1)).isoformat() if later else None
        vs.append({**w, "status": "active", "effective_to": end})
    return vs


def _in_force(vs: list[dict], day: str) -> dict | None:
    """Of versions in the order of their making, the one in force on a day: the latest start not after it, not closed before it, of two with the
    same start the one made last (resolve's order)."""
    best = None
    for i, v in enumerate(vs):
        if v["effective_from"] <= day and (v["effective_to"] is None or v["effective_to"] >= day):
            if best is None or (v["effective_from"], i) >= best[:2]:
                best = (v["effective_from"], i, v)
    return best[2] if best else None


def resolve(kind: str, key: str, on: date | str) -> dict | None:
    """The active version of (kind, key) in force on a date: the latest effective_from not after it and not closed before it (of two with the same
    start, the one created last)."""
    _seeded()
    day = on.isoformat() if isinstance(on, date) else str(on)[:10]
    if _what_if_of(kind, key):
        return _in_force(_active_versions(kind, key), day)
    with store.connect() as con:
        r = con.execute("SELECT * FROM tariff_versions WHERE kind = ? AND key = ? AND status = 'active' AND effective_from <= ? "
                        "AND (effective_to IS NULL OR effective_to >= ?) ORDER BY effective_from DESC, created DESC, rowid DESC LIMIT 1",
                        (kind, key, day, day)).fetchone()
    return _ver(r) if r else None


#: A version's kind, and the keys the engine resolves, in words (a body without a name is shown by these, never by its raw key)
_KIND_NAMES = {"schedule": "Uniform schedule", "tou": "Time-of-use calendar", "adder": "Adder", "tax": "Tax rules",
               "icp": "Incremental consumption package (ICP)", "prosumer": "Prosumer regime", "override": "Site override"}
_KEY_NAMES = {"xwdisco": "DISCOs except K-Electric", "ke": "K-Electric", "net_billing": "Net billing", "net_metering_legacy": "Legacy net metering"}
_ABBREVIATIONS = ("fca", "fpa", "qta", "dss", "nj", "ptv", "icp")


def version_name(v: dict, sites_by_connection: dict | None = None) -> str:
    """A version's name for people: its body's name; else its kind and what it covers in words ("Time-of-use calendar, K-Electric", "Net billing",
    "Tax rules"); an adder by its key with the abbreviations in capitals ("FCA late"); a site override by its site ("Site override, Mill One", the
    body's name after it in brackets), never by the connection's id."""
    body = v.get("body") if isinstance(v.get("body"), dict) else {}
    name = body.get("name")
    if v.get("kind") == "override":
        site = (sites_by_connection or {}).get(v.get("key"))
        words = "Site override" + (f", {site}" if site else "")
        return words + (f" ({name.strip()})" if isinstance(name, str) and name.strip() else "")
    if isinstance(name, str) and name.strip():
        return name.strip()
    kind, key = str(v.get("kind") or ""), str(v.get("key") or "")
    if kind == "prosumer" and key in _KEY_NAMES:
        return _KEY_NAMES[key]
    if kind == "adder":
        parts = [p.upper() if p.lower() in _ABBREVIATIONS else p.lower() for p in re.split(r"[_\s-]+", key) if p]
        if not parts:
            return "Adder"
        return " ".join([parts[0][:1].upper() + parts[0][1:], *parts[1:]])
    words = _KIND_NAMES.get(kind, kind.capitalize() or "Version")
    if key and key != "national":
        words += f", {_KEY_NAMES.get(key, key.replace('_', ' '))}"
    return words


#: A placeholder's kind as a notice names it
_KIND_WORDS = {"schedule": "uniform schedule", "tou": "time-of-use calendar", "adder": "adder", "tax": "tax rules",
               "icp": "incremental consumption package", "prosumer": "prosumer regime", "override": "site override"}


def _day_words(d: date) -> str:
    """A day as people read it: "1 Jan 2027"."""
    return f"{d.day} {d:%b} {d.year}"


def awaiting_notices(on) -> list[dict]:
    """The versions awaiting their notification whose start has passed by `on` (plan 6.1 and 6.2, TAR-09; R9 section 10): the shipped CY2027 schedule
    from 1 Jan 2027, until the notified one is drafted and approved (which supersedes the placeholder). The engine keeps pricing with the active
    version in force, so the price vector, a plan, a rebuilt bill and the tariff library carry these to say their rupees rest on a version that the
    notification may change. Each names the version still in use (`in_use`, None when none is in force). Words and dates only, no rate or rupee:
    every role may read it. The alert for it is the alert rules'."""
    _seeded()
    day = on.isoformat() if isinstance(on, date) else str(on)[:10]
    with store.connect() as con:
        rows = con.execute("SELECT * FROM tariff_versions WHERE status = 'awaiting_notification' AND effective_from <= ? "
                           "ORDER BY kind, key, effective_from", (day,)).fetchall()
    out = []
    for r in rows:
        v = _ver(r)
        if any(w["effective_from"] >= v["effective_from"] for w in _what_if_of(v["kind"], v["key"])):
            continue                                        # priced as if approved, a version answering it supersedes it (what_if)
        used = resolve(v["kind"], v["key"], day)
        start = _day_words(date.fromisoformat(v["effective_from"]))
        what = _KIND_WORDS.get(v["kind"], v["kind"])
        if used is not None:
            then = f"prices from that day still use {version_name(used)} until an administrator approves the notified version."
        else:
            then = "nothing is in force from that day until an administrator approves the notified version."
        out.append({"kind": v["kind"], "key": v["key"], "version_id": v["id"], "effective_from": v["effective_from"], "name": version_name(v),
                    "in_use": {"id": used["id"], "name": version_name(used), "effective_from": used["effective_from"]} if used else None,
                    "message": f"The {what} due from {start} is awaiting its notification: {then}"})
    return out


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


def _month_names(months: list[int]) -> str:
    """[3, 4, 11] as "March, April and November"."""
    names = [_calendar.month_name[m] for m in months]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


#: The kinds of version the library holds
KINDS = ("schedule", "tou", "adder", "tax", "icp", "prosumer", "override")
#: How far a version's source is checked
VERIFIED = ("primary", "secondary", "unverified")
#: The tariff editor's step each kind's body is entered on (plan F2.5, decision 10): A what changes (the kind, its key, the start and the source),
#: B rates and peak windows, C charges and MDI, D export and net billing. A site override's peak windows are B and the rest of it C
_STEP_OF = {"schedule": "B", "tou": "B", "adder": "C", "tax": "C", "icp": "C", "prosumer": "D", "override": "C"}


def step_of(kind: str, path: tuple) -> str:
    """The editor's step a field is entered on (UX K32): "A" for the kind, key, start and source; the body's by its kind."""
    if not path or path[0] != "body":
        return "A"
    if kind == "override" and len(path) > 1 and path[1] == "calendar":
        return "B"
    return _STEP_OF.get(kind, "A")


def _collector():
    """A list of (field path, sentence) and the function that adds to it: one problem a field, the first said of it."""
    found: list[tuple[tuple, str]] = []

    def add(path: tuple, message: str) -> None:
        if all(p != path for p, _ in found):
            found.append((tuple(path), message))
    return found, add


def body_problems(kind: str, key: str, body, effective_from: str | None = None) -> list[tuple[tuple, str]]:
    """Every problem of a version's key and body that the engine could not price with, each with the field it is about as a path (("key",),
    ("body", "cards", "B-3", "peak"), ("body", "seasons", 1, "months"), ("effective_from",)), in the order the checks run: one approved malformed row
    would otherwise stop every plan, price and bill. `validate_body` refuses with the first; the tariff editor's validate route says them all, each
    tied to its field (UX backend ask K32)."""
    found, add = _collector()
    if not isinstance(body, dict):
        add(("body",), "A version's body is a JSON object")
        return found
    if kind in _KEYS and key not in _KEYS[kind]:
        add(("key",), f"A {kind} version's key is one of: {', '.join(_KEYS[kind])}")
    if "name" in body and not isinstance(body["name"], str):
        add(("body", "name"), "A version's name is text")
    if kind == "schedule":
        cards = body.get("cards")
        if not isinstance(cards, dict) or not cards:
            add(("body", "cards"), "A schedule needs its rate cards: a non-empty map of category to card")
        else:
            for cat, c in cards.items():
                at = ("body", "cards", cat)
                if not isinstance(c, dict):
                    add(at, f"The card for {cat} is not an object")
                    continue
                if c.get("fixed_basis") not in _BASES:
                    add((*at, "fixed_basis"), f"The card for {cat} needs fixed_basis: {', '.join(_BASES)}")
                if not _num(c.get("fixed")) or c["fixed"] < 0:
                    add((*at, "fixed"), f"The card for {cat} needs a fixed charge (a number, 0 or more)")
                if not _num(c.get("floor_share")) or not 0 <= c["floor_share"] <= 1:
                    add((*at, "floor_share"), f"The card for {cat} needs floor_share between 0 and 1")
                for f in ("peak", "off_peak", "flat"):
                    if c.get(f) is not None and (not _num(c[f]) or c[f] < 0):
                        add((*at, f), f"The card for {cat} has a {f} rate that is not a number of 0 or more")
                if c.get("tou") and c.get("peak") is not None:
                    if not _num(c.get("off_peak")):
                        add((*at, "off_peak"), f"The TOU card for {cat} needs peak and off_peak rates")
                elif not _num(c.get("flat")):
                    add((*at, "flat"), f"The card for {cat} needs a flat rate, or tou with peak and off_peak rates")
    elif kind == "adder":
        if not _num(body.get("rate")) or abs(body["rate"]) > 100:
            add(("body", "rate"), "An adder needs a rate in Rs/kWh (a number)")
        months = body.get("billing_months")
        if months != "all":
            if not isinstance(months, list) or not months or not all(isinstance(m, str) and _YM.match(m) for m in months):
                add(("body", "billing_months"), 'An adder needs billing_months: "all" or a list of billing months as YYYY-MM')
            elif effective_from and effective_from > _month_bounds(min(months))[1]:
                add(("effective_from",), "An adder's start must fall on or before the end of its first billing month (use the first day of that "
                                         "month); it is resolved by billing month, so a later start would leave that bill without it")
        exempt = body.get("exempt", [])
        if not isinstance(exempt, list) or not all(isinstance(x, str) for x in exempt):
            add(("body", "exempt"), "An adder's exempt is a list of exemptions (for example icp_units, lifeline, prepaid, or a tariff category or class)")
        rates = body.get("category_rates", {})
        if not isinstance(rates, dict) or not all(isinstance(k, str) and _num(r) and abs(r) <= 100 for k, r in rates.items()):
            add(("body", "category_rates"), "An adder's category_rates is a map of tariff category or class (for example agricultural) to a rate in "
                                             "Rs/kWh")
    elif kind == "tou":
        seasons = body.get("seasons")
        if not isinstance(seasons, list) or not seasons:
            add(("body", "seasons"), "A TOU calendar needs seasons, each with months and a peak window")
        else:
            listed: list[int] = []
            whole = True
            for i, s in enumerate(seasons):
                months = s.get("months") if isinstance(s, dict) else None
                peak = s.get("peak") if isinstance(s, dict) else None
                if not isinstance(months, list) or not months or not all(isinstance(m, int) and not isinstance(m, bool) and 1 <= m <= 12 for m in months):
                    add(("body", "seasons", i, "months"), "Each TOU season needs months: a list of month numbers 1 to 12")
                    whole = False
                else:
                    listed += months
                if not isinstance(peak, (list, tuple)) or len(peak) != 2 or not all(isinstance(x, str) and _HHMM.match(x) for x in peak):
                    add(("body", "seasons", i, "peak"), 'Each TOU season needs a peak window: ["HH:MM", "HH:MM"]')
                elif peak[0] == peak[1]:
                    add(("body", "seasons", i, "peak"), "Each TOU season needs a peak window whose start and end differ")
            # peak membership is a function of the month for every month (R9 2.3): a month left out would have no peak at all, one listed twice two windows
            if whole:
                missing, twice = sorted(set(range(1, 13)) - set(listed)), sorted({m for m in listed if listed.count(m) > 1})
                if missing:
                    add(("body", "seasons"), f"A TOU calendar must give every month one season: {_month_names(missing)} "
                                             f"{'has' if len(missing) == 1 else 'have'} none")
                if twice:
                    add(("body", "seasons"), f"A TOU calendar must give every month one season: {_month_names(twice)} "
                                             f"{'is' if len(twice) == 1 else 'are'} listed twice")
    elif kind == "icp":
        if not _num(body.get("rate")) or body["rate"] < 0:
            add(("body", "rate"), "The ICP needs its rate in Rs/kWh")
        months_ok = True
        for f in ("first_month", "last_month"):
            if not isinstance(body.get(f), str) or not _YM.match(body[f]):
                add(("body", f), f"The ICP needs {f} as YYYY-MM")
                months_ok = False
        if months_ok and body["first_month"] > body["last_month"]:
            add(("body", "first_month"), "The ICP's first month comes before its last")
        if not isinstance(body.get("categories"), list) or not all(isinstance(x, str) for x in body["categories"]):
            add(("body", "categories"), "The ICP needs the list of categories it covers")
    elif kind == "prosumer":
        if key == "net_billing" and (not _num(body.get("naepp")) or body["naepp"] < 0):
            add(("body", "naepp"), "Net billing needs the export price naepp in Rs/kWh")
        sched = body.get("tou_export_schedule")
        if sched is not None and not (isinstance(sched, dict) and all(_num(sched.get(p)) and sched[p] >= 0 for p in ("peak", "off_peak"))):
            add(("body", "tou_export_schedule"), "A TOU export schedule gives the peak and off_peak export prices in Rs/kWh")
        if not isinstance(body.get("credit_before_tax", False), bool):
            add(("body", "credit_before_tax"), "credit_before_tax is true (the export credit comes off the bill before GST) or false (after taxes)")
        if key == "net_metering_legacy":
            if "nappp" in body and (not _num(body["nappp"]) or body["nappp"] < 0):
                add(("body", "nappp"), "Legacy net metering's nappp is the notified national average power purchase price in Rs/kWh")
            sr = body.get("surplus_rate", "nappp" if "nappp" in body else "off_peak")
            if not (sr in ("nappp", "off_peak", "peak") or (_num(sr) and sr >= 0)):
                add(("body", "surplus_rate"), 'Legacy net metering\'s surplus_rate is "nappp" (the notified national average power purchase price), '
                                              '"off_peak", "peak" or a rate in Rs/kWh')
            elif sr == "nappp" and "nappp" not in body:
                add(("body", "nappp"), 'Legacy net metering\'s surplus_rate "nappp" needs the notified rate: nappp in Rs/kWh')
            if body.get("netting", "by_period") != "by_period":
                add(("body", "netting"), "Legacy net metering is modelled with by-period netting only (netting: by_period)")
    elif kind == "tax":
        for f in ("gst", "further_tax_no_strn"):
            if f in body and (not _num(body[f]) or not 0 <= body[f] < 1):
                add(("body", f), f"The tax rule {f} is a fraction between 0 and 1")
        ed = body.get("electricity_duty", {})
        if not isinstance(ed, dict) or not all(isinstance(v, dict) and all(_num(x) and 0 <= x < 1 for x in v.values()) for v in ed.values()):
            add(("body", "electricity_duty"), "Electricity duty is a map of province to a map of class to a fraction")
        s235 = body.get("s235", {})
        if not isinstance(s235, dict) or not all(isinstance(v, dict) and all(_num(v.get(f)) for f in ("nil_upto", "mid_rate", "mid_upto", "base_above", "rate_above"))
                                                 for v in s235.values()):
            add(("body", "s235"), "Each s.235 slab needs nil_upto, mid_rate, mid_upto, base_above and rate_above")
        if not isinstance(body.get("ptv", {}), dict) or not all(_num(x) for x in body.get("ptv", {}).values()):
            add(("body", "ptv"), "The PTV fee is a map of class to rupees")
        if "muct_ke" in body and not _num(body["muct_ke"]):
            add(("body", "muct_ke"), "The MUCT fee is a number of rupees")
        if "punjab_selfgen_per_kwh" in body and (not _num(body["punjab_selfgen_per_kwh"]) or not 0 <= body["punjab_selfgen_per_kwh"] < 1):
            add(("body", "punjab_selfgen_per_kwh"), "Punjab's per-unit duty is in Rs/kWh (4 paisa is 0.04)")
        slabs = body.get("extra_tax_non_atl", [])
        ok = isinstance(slabs, list) and all(isinstance(x, dict) and _num(x.get("rate")) and 0 <= x["rate"] < 1
                                             and (x.get("upto") is None or (_num(x["upto"]) and x["upto"] > 0)) for x in slabs)
        bounds = [x["upto"] for x in slabs if isinstance(x, dict) and x.get("upto") is not None] if ok else []
        if not ok or bounds != sorted(bounds) or any(x.get("upto") is None for x in slabs[:-1]):
            add(("body", "extra_tax_non_atl"), "The extra tax slabs are a list of {upto (rupees, rising; none on the last), rate (a fraction between 0 "
                                               "and 1)}")
    elif kind == "override":
        _override_problems(body, add)
    return found


def validate_body(kind: str, key: str, body, effective_from: str | None = None) -> None:
    """Refuse (ValueError) a version body the engine could not price with, in the words of its first problem (`body_problems`)."""
    found = body_problems(kind, key, body, effective_from)
    if found:
        raise ValueError(found[0][1])


#: What a site override may set (R9 12.6 and 13 step 3): the figures a site's own bill shows that differ from the national objects, and the MDI
#: block mode and scope (R9 recommendation 3: site settings pending a bill)
OVERRIDE_FIELDS = ("electricity_duty", "adders", "calendar", "mdi_block", "mdi_scope")
MDI_BLOCKS = ("fixed", "sliding")
MDI_SCOPES = ("all_hours", "peak")


def _override_problems(body: dict, add) -> None:
    extra = sorted(set(body) - {"name", *OVERRIDE_FIELDS})
    if extra:
        add(("body",), "A site override sets only the electricity duty, adder rates, the peak window calendar, the MDI block mode and the MDI scope "
                       f"(not {', '.join(extra)})")
    if not set(body) & set(OVERRIDE_FIELDS):
        add(("body",), "A site override sets at least one figure the site's bill shows: electricity_duty, adders, calendar, mdi_block or mdi_scope")
    if "electricity_duty" in body and (not _num(body["electricity_duty"]) or not 0 <= body["electricity_duty"] < 1):
        add(("body", "electricity_duty"), "A site override's electricity_duty is a fraction between 0 and 1 (1.5 percent is 0.015)")
    ads = body.get("adders", {})
    if not isinstance(ads, dict) or not all(isinstance(k, str) and k.strip() and _num(r) and abs(r) <= 100 for k, r in ads.items()):
        add(("body", "adders"), "A site override's adders is a map of adder key (for example dss) to its rate on the site's bill in Rs/kWh")
    if "calendar" in body:
        # the site's own peak windows are a TOU calendar, checked as one
        for path, message in body_problems("tou", "xwdisco", body["calendar"]):
            add(("body", "calendar", *path[1:]), message)
    if body.get("mdi_block", "fixed") not in MDI_BLOCKS:
        add(("body", "mdi_block"), "A site override's mdi_block is fixed or sliding (the meter's demand blocks)")
    if body.get("mdi_scope", "all_hours") not in MDI_SCOPES:
        add(("body", "mdi_scope"), "A site override's mdi_scope is all hours (all_hours) or the peak hours only (peak)")


def override_key_problem(kind: str, key) -> str | None:
    """A site override's key must be one of this company's grid connections (its site's own bill): the refusal's words, else None."""
    if kind == "override" and isinstance(key, str) and key.strip() and key not in _sites_by_connection():
        return "A site override's key is the grid connection it applies to: one of this company's connections"
    return None


def version_problems(kind, key, effective_from, body, source) -> list[tuple[tuple, str]]:
    """Every problem `propose` refuses a version for, each with its field, in the order propose checks them: the kind, the key, the start, the
    body and its source, the body by its kind (`body_problems`), and a site override's connection (UX backend ask K32)."""
    found, add = _collector()
    if kind not in KINDS:
        add(("kind",), "Unknown tariff kind")
    if not isinstance(key, str) or not key.strip():
        add(("key",), "A version needs its key")
    start = None
    try:
        start = _canonical_day(effective_from)
    except ValueError as exc:
        add(("effective_from",), str(exc))
    if not isinstance(body, dict) or not isinstance(source, dict) or source.get("verified") not in VERIFIED:
        add(("body",) if not isinstance(body, dict) else ("source", "verified"),
            "A version carries a body and a source with verified: primary, secondary or unverified")
    if kind in KINDS and isinstance(body, dict):
        for path, message in body_problems(kind, key if isinstance(key, str) else "", body, start):
            add(path, message)
    words = override_key_problem(kind, key)
    if words:
        add(("key",), words)
    return found


def _insert_version(con, kind: str, key: str, effective_from: str, body: dict, source: dict, by: str, by_id: str) -> str:
    """A pending version, known from the day it was drafted in Karachi (UX K32 check: the UTC day named the day before until 05:00)."""
    vid = store.new_id()
    now = store._now()
    known = datetime.fromisoformat(now).astimezone(PKT).date().isoformat()
    con.execute("INSERT INTO tariff_versions (id, kind, key, effective_from, effective_to, known_from, status, supersedes, body, source, hash, created, by, "
                "by_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (vid, kind, key, effective_from, None, known, "pending", None, json.dumps(body),
                                                               json.dumps(source), _hash(body), now, by, by_id or ""))
    store.audit(by, "proposed a tariff version", f"{kind}/{key}", {"effective_from": effective_from}, con=con)
    return vid


def propose(kind: str, key: str, effective_from: str, body: dict, source: dict, by: str, by_id: str = "") -> dict:
    """A new version from a watcher or a person, pending until an administrator approves it. `by_id` is its author's sign-in id, which a second
    person's approval and its author's withdrawal are told by (UX K32; '' for a watcher and a row of before)."""
    found = version_problems(kind, key, effective_from, body, source)
    if found:
        raise ValueError(found[0][1])
    effective_from = _canonical_day(effective_from)
    _seeded()
    with store.write_transaction() as con:
        vid = _insert_version(con, kind, key, effective_from, body, source, by, by_id)
    return next(v for v in versions(kind) if v["id"] == vid)


def propose_many(items: list[dict], by: str, by_id: str = "", claim=None) -> list[dict]:
    """Several versions proposed together (a tariff draft submitted: one version a kind, the same start and source), all or none: each is checked as
    `propose` checks one, and they are stored in one write transaction (UX K32). Each item is {kind, key, effective_from, body, source}.

    `claim(con)`, when given, runs first in that transaction (UX K32 check: the draft they come from is taken there, so a second submit of it makes
    nothing); what it raises leaves nothing written."""
    for it in items:
        found = version_problems(it.get("kind"), it.get("key"), it.get("effective_from"), it.get("body"), it.get("source"))
        if found:
            raise ValueError(found[0][1])
    _seeded()
    with store.write_transaction() as con:
        if claim is not None:
            claim(con)
        ids = [_insert_version(con, it["kind"], it["key"], _canonical_day(it["effective_from"]), it["body"], it["source"], by, by_id) for it in items]
    every = {v["id"]: v for v in versions()}
    return [every[i] for i in ids]


def coverage_problem(kind: str, body) -> str | None:
    """A schedule that leaves a category this company's connections are on without a rate card: approving it is refused
    (`_check_schedule_covers_connections`), so the editor says so before it is submitted. The refusal's words, else None."""
    if kind != "schedule" or not isinstance(body, dict) or not isinstance(body.get("cards"), dict) or not body["cards"]:
        return None
    with store.connect() as con:
        try:
            _check_schedule_covers_connections(con, body["cards"])
        except ValueError as exc:
            return str(exc)
    return None


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


class OwnVersion(ValueError):
    """The approver is the version's author: a second person approves it (the owner's question Q65, recommended answer; UX K32)."""


class NotYours(ValueError):
    """A person who is neither a version's author nor an administrator asked to withdraw it (UX K32)."""


def approve(version_id: str, by: str, by_id: str = "") -> dict:
    """Make a pending version active. The active version of the same key it follows is closed the day before; one with the same start (a correction) is
    superseded outright, and a placeholder awaiting this notification is superseded. A placeholder itself is never approved: the notified version is
    drafted and approved in its place. The body is checked again before anything changes (the only mutations allowed, logged).

    A second person approves (the owner's question Q65, recommended answer; UX K32): `by_id`, the approver's sign-in id, may not be the author's
    (`OwnVersion`). A version of a watcher or of before, with no author's id, is approved by any administrator."""
    with store.write_transaction() as con:
        r = con.execute("SELECT * FROM tariff_versions WHERE id = ?", (version_id,)).fetchone()
        if r is None:
            raise KeyError(version_id)
        if r["status"] == "awaiting_notification":
            raise ValueError("This version is a placeholder awaiting the notification: draft the notified version and approve that one")
        if r["status"] != "pending":
            raise ValueError("Only a pending version can be approved")
        if by_id and (r["by_id"] or "") == by_id:
            raise OwnVersion("A second person approves a tariff version: you drafted this one, so another administrator approves it")
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


def withdraw(version_id: str, by: str, by_id: str = "", reason: str = "", administrator: bool = False, send_back=None) -> dict:
    """Take back a version waiting for approval (plan F2.2, F11 item 6; UX K32): its author (by sign-in id), or an administrator, withdraws it. Its
    status becomes "withdrawn": in force on no day, never counted as waiting, never approved; when, who and why are kept with it and in the audit
    trail. Raises KeyError for no such version, NotYours for someone else's (not an administrator), ValueError for one not waiting.

    `send_back(con, version)` (an administrator's "send back", H6.8) makes the draft that goes back to its author in the same transaction and
    returns (its id, whose it is): the audit line then names them (`sent_back_as`, `sent_back_to`), and if it fails the version still waits (UX
    K32 check)."""
    with store.write_transaction() as con:
        r = con.execute("SELECT * FROM tariff_versions WHERE id = ?", (version_id,)).fetchone()
        if r is None:
            raise KeyError(version_id)
        if not administrator and not (by_id and (r["by_id"] or "") == by_id):
            raise NotYours("Withdrawing a tariff version is for its author or an administrator")
        if r["status"] != "pending":
            raise ValueError("Only a version waiting for approval can be withdrawn")
        con.execute("UPDATE tariff_versions SET status = 'withdrawn', withdrawn_at = ?, withdrawn_by = ?, withdrawn_note = ? WHERE id = ?",
                    (store._now(), by, reason, version_id))
        detail = {"version_id": version_id, "effective_from": r["effective_from"], "reason": reason}
        if send_back is not None:
            detail["sent_back_as"], detail["sent_back_to"] = send_back(con, _ver(r))
        store.audit(by, "withdrew a tariff version", f"{r['kind']}/{r['key']}", detail, con=con)
    return next(v for v in versions(r["kind"]) if v["id"] == version_id)


def fuel_price(kind: str, on: date | str) -> tuple[float, dict]:
    """The fuel price in force on a day and its source: the latest entered on or before it (R9 section 9.1: OGRA prices diesel daily, and a day
    with no entry of its own keeps the one before). A day before the first entry has no fuel price (audit engine-money-live-15): the first price
    stands in, so that a plan never runs on free fuel, and its source says so (`stands_in`, `first_from` and a `note` in words); the savings ledger
    then claims nothing on fuel priced with it (verify.compute_day, claim_withheld "no_fuel_price")."""
    _seeded()
    day = on.isoformat() if isinstance(on, date) else str(on)[:10]
    with store.connect() as con:
        r = con.execute("SELECT price, source FROM fuel_prices WHERE kind = ? AND valid_from <= ? ORDER BY valid_from DESC LIMIT 1", (kind, day)).fetchone()
        if r is not None:
            return float(r["price"]), json.loads(r["source"])
        r = con.execute("SELECT price, source, valid_from FROM fuel_prices WHERE kind = ? ORDER BY valid_from LIMIT 1", (kind,)).fetchone()
    if r is None:
        return 0.0, {"stands_in": True, "first_from": None, "note": "No fuel price has been entered for this fuel"}
    first = date.fromisoformat(r["valid_from"])
    return float(r["price"]), {**json.loads(r["source"]), "stands_in": True, "first_from": r["valid_from"],
                               "note": f"No fuel price for this day: the first entered price, from {_day_words(first)}, stands in"}


def add_fuel_price(kind: str, valid_from: str, price: float, by: str) -> None:
    """Enter a fuel price from a day (a day already priced takes the new price). The shipped prices are seeded first, so the entered price is the
    one in force from its day on: a later build's shipped prices never displace it (`_fuel_todo`)."""
    if kind not in ("hsd", "gas_captive", "lng_captive") or not (0 < price < 100000):
        raise ValueError("A fuel price needs a kind (hsd, gas_captive, lng_captive) and a positive price")
    valid_from = _canonical_day(valid_from, "A fuel price's start")
    _seeded()
    with store.write_transaction() as con:
        held = con.execute("SELECT price FROM fuel_prices WHERE kind = ? AND valid_from = ?", (kind, valid_from)).fetchone()
        con.execute("INSERT INTO fuel_prices (kind, valid_from, price, source) VALUES (?,?,?,?) ON CONFLICT(kind, valid_from) DO UPDATE SET "
                    "price = excluded.price, source = excluded.source", (kind, valid_from, price, json.dumps({"kind": "manual", "ref": by, "verified": "primary"})))
        # a day already priced: the price it had (round 7, audit security-roles-04)
        store.audit(by, "set a fuel price", kind, {"valid_from": valid_from, "price": price,
                                                   "changes": store.changes({"price": None if held is None else float(held["price"])}, {"price": price})},
                    con=con)


def fuel_prices(kind: str | None = None) -> list[dict]:
    _seeded()
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


def peak_share_by_hour(calendar: dict, month: int) -> list[float] | None:
    """Each hour's share of the day's peak in a month, as the price vector marks it (is_peak_local at the start of each of the hour's four 15-minute
    steps: an hour whose half is in the window is 0.5), January being 1; None for a month in no season of the calendar. Set-up's PEAK WINDOWS THROUGH
    THE YEAR draws it (UX backend ask K5), so the strip and the prices read one rule."""
    if not any(month in (s.get("months") or []) for s in calendar.get("seasons") or []):
        return None
    return [sum(is_peak_local(calendar, datetime(2001, month, 1, h, q * 15)) for q in range(4)) / 4.0 for h in range(24)]


def site_override(conn: dict | None, on) -> dict:
    """The site override of a connection in force on a day (R9 12.6: the figures its own bills show that differ from the national objects, and its
    MDI block mode and scope), a versioned row keyed by the connection and approved like any version; {} when none is."""
    cid = (conn or {}).get("id")
    v = resolve("override", str(cid), on) if cid else None
    return v["body"] if v else {}


def calendar_version(conn: dict, on) -> tuple[dict, str, dict | None]:
    """The time-of-use calendar a connection is priced on, on a day, with where it comes from and its version: the peak window the site's own bill
    shows (a site override's calendar: "site_override"), else its DISCO group's calendar in force ("tou"); the version row, or None where no
    version is in force (no peak then)."""
    cid = (conn or {}).get("id")
    ov = resolve("override", str(cid), on) if cid else None
    if ov is not None and isinstance((ov["body"] or {}).get("calendar"), dict):
        return ov["body"]["calendar"], "site_override", ov  # the peak window the site's own bill shows
    v = resolve("tou", "ke" if conn["data"].get("disco_group") == "ke" else "xwdisco", on)
    return (v["body"] if v else {"seasons": []}), "tou", v


def calendar_for(conn: dict, on) -> dict:
    return calendar_version(conn, on)[0]


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
    _seeded()
    first, last = _month_bounds(billing_month)
    if _what_if_of("adder"):
        # sorted by key and start; Python's sort keeps the order of making within them, a what-if version last (UX K32)
        vs = sorted((v for v in _active_versions("adder") if v["effective_from"] <= last), key=lambda v: (v["key"], v["effective_from"]))
    else:
        with store.connect() as con:
            rows = con.execute("SELECT * FROM tariff_versions WHERE kind = 'adder' AND status = 'active' AND effective_from <= ? "
                               "ORDER BY key, effective_from, created, rowid", (last,)).fetchall()
        vs = []
        for r in rows:
            try:
                vs.append(_ver(r))
            except (TypeError, ValueError):
                continue
    best: dict[str, dict] = {}
    for v in vs:
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
    out = [{"key": v["key"], "id": v["id"], "name": v["key"], "exempt": [], **v["body"], "hash": v["hash"]} for v in best.values()]
    # in R9 section 7's order of a bill's third group: the FCAs (FPA), the QTA, then the surcharges (DSS, Neelum-Jhelum, ...), each by key
    return sorted(out, key=lambda a: (0 if is_fca(a) else 1 if str(a["key"]).lower().startswith("qta") else 2, a["key"]))


def is_fca(adder: dict) -> bool:
    """A fuel charges adjustment (FCA, also printed FPA): an adder whose key starts with fca or fpa. Besides the month's own "fca", NEPRA bundles two
    months' FCAs on one bill or moves a late one to the next (R9 6.2), and each is stored under its own key ("fca_sep"); every one of them is an FCA."""
    return bool(_FCA_KEY.match(str(adder.get("key") or "")))


def fca_billed(billing_month: str) -> bool:
    """An FCA (under any key) is on the bill of the billing month: the condition plan 8.5 waits for before a saving is final."""
    return any(is_fca(a) for a in adders_for(billing_month))


def adder_rate(adder: dict, conn: dict | None, override: dict | None = None) -> float | None:
    """An adder's Rs/kWh for a connection: None when its exemptions name the connection's tariff category or class (for example "agricultural");
    else its rate for that category, or for that class, from `category_rates` (the debt-service surcharge is 43 paisa for agricultural tube wells,
    R9 section 7); else its rate. Exemptions that name a consumer kind (lifeline, prepaid, protected, evcs) match no category or class. A site
    override's rate for the adder's key (`override["adders"]`, from the site's own bill) comes before all of these."""
    own = ((override or {}).get("adders") or {}).get(adder.get("key"))
    if _num(own):
        return float(own)
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

def _tax_factor(conn: dict, tax: dict, override: dict | None = None) -> dict:
    """The multipliers a tax version applies to variable charges, split into what the company bears and what it recovers; a site override's
    electricity duty (from the site's own bill) in place of its province's and class's."""
    data = conn["data"]
    cls = tariff_data.category_class(data["tariff_category"])
    prov = (data.get("tax") or {}).get("province", "punjab")
    ed = tax.get("electricity_duty", {}).get(prov, {}).get(cls, 0.015)
    if _num((override or {}).get("electricity_duty")):
        ed = float(override["electricity_duty"])
    gst = tax.get("gst", 0.18)
    flags = data.get("tax") or {}
    further = tax.get("further_tax_no_strn", 0.03) if not flags.get("strn", True) and cls in ("industrial", "commercial") else 0.0
    slab = tax.get("s235", {}).get("industrial" if cls == "industrial" else "commercial")
    s235 = (slab or {}).get("rate_above", 0.05)
    return {"ed": ed, "gst": gst, "further": further, "s235_marginal": s235, "gst_recoverable": bool(flags.get("gst_recoverable", True)),
            "s235_adjustable": bool(flags.get("s235_adjustable", True)), "class": cls,
            # the bill charges s.235 only to industrial and commercial connections (rebuild step 11)
            "s235_applies": bool(slab) and cls in ("industrial", "commercial"),
            # extra tax off the Active Taxpayer List at the top slab (a C&I bill is above every bound); None when the version has no slabs
            "extra": _extra_tax_rate(conn, tax, math.inf)}


def _tax_factor_energy(conn: dict, on) -> dict:
    """The multipliers the bill applies to variable charges on a day, split into what the company bears and what it recovers."""
    return _tax_factor(conn, tax_rules(on), site_override(conn, on))


def _tax_stack(tf: dict) -> float:
    """The bearable rupees per rupee of a pre-tax charge, from the bill's own formula: GST, further and extra tax all on the same base (added, not
    multiplied), and s.235 on the bill amount including them, where the class pays it; GST and s.235 dropped where the company recovers them."""
    extra = tf.get("extra") or 0.0
    f = 1.0 + tf["further"] + extra + (0.0 if tf["gst_recoverable"] else tf["gst"])
    if tf["s235_applies"] and not tf["s235_adjustable"]:
        f += tf["s235_marginal"] * (1.0 + tf["gst"] + tf["further"] + extra)
    return f


def recoverable_share(conn: dict, on) -> float:
    """The taxes a connection recovers per bearable rupee of a taxed charge on a day (UX backend ask K27, plan 8.2's "taxes you recover" line): GST
    where `gst_recoverable` and the s.235 advance where `s235_adjustable` (on the bill amount, GST, further and extra tax included, where the class pays
    it), over the bearable stack every price and the ledger carry (_tax_stack). A rupee of the ledger's grid energy or MDI lever was one of these
    bearable rupees, so its recovered taxes are this share of it; 0 where the company recovers neither (a no-STRN commercial site bears both)."""
    tf = _tax_factor_energy(conn, on)
    extra = tf.get("extra") or 0.0
    rec = tf["gst"] if tf["gst_recoverable"] else 0.0
    if tf["s235_applies"] and tf["s235_adjustable"]:
        rec += tf["s235_marginal"] * (1.0 + tf["gst"] + tf["further"] + extra)
    return rec / _tax_stack(tf)


def _unit_duty(conn: dict, tax: dict) -> float:
    """Punjab's additional electricity duty on heavy consumers, Rs per kWh of grid import (R9 section 7: 4 paisa a unit for industrial and commercial
    connections above 500 kVA, collected on the DISCO bill; Electricity Duty Rules 2026). The sanctioned load stands for the kVA where no
    `sanctioned_kva` is entered. 0 elsewhere. The same rate on self-generated units is collected by the electric inspector, not on this bill."""
    data = conn["data"]
    rate = tax.get("punjab_selfgen_per_kwh")
    if not _num(rate) or rate <= 0 or (data.get("tax") or {}).get("province", "punjab") != "punjab":
        return 0.0
    if tariff_data.category_class(data["tariff_category"]) not in ("industrial", "commercial"):
        return 0.0
    kva = data.get("sanctioned_kva") if _num(data.get("sanctioned_kva")) else data.get("sanctioned_load_kw")
    return float(rate) if _num(kva) and float(kva) > 500 else 0.0


#: Punjab's duty on self-generated units applies to sets above 500 kVA (R9 section 7, Electricity Duty Rules 2026); a set's kVA is its rated kW over
#: its power factor (0.8 where none is entered) where no kVA is entered
SELFGEN_KVA_MIN = 500.0
SELFGEN_PF = 0.8


def selfgen_duty(site: dict, on, conn: dict | None = None) -> float:
    """Punjab's duty on self-generated units, Rs per kWh the genset generates (R9 section 7: 4 paisa a unit for industrial and commercial consumers
    above 500 kVA, collected by the electric inspector, not on the DISCO bill): the tax version in force's `punjab_selfgen_per_kwh`, where the site
    is in Punjab (its connection's tax province, else its DISCO's), on an industrial or commercial footing (its connection's category; a site
    without one by its type, a farm being agricultural) and its genset is above 500 kVA. 0 elsewhere. The plan's genset cost (plans.build_problem)
    and the ledger's (verify._genset_cost) both read it (round 7, engine-tariffs-09: latitude > 28 stood for Punjab, from a constant in plans.py,
    and the ledger left the duty out)."""
    from . import sites as sites_mod  # sites imports tariffs at module load

    g = ((site or {}).get("assets") or {}).get("genset")
    if not g:
        return 0.0
    pf = g.get("power_factor") if _num(g.get("power_factor")) and g.get("power_factor") > 0 else SELFGEN_PF
    kva = g.get("rated_kva") if _num(g.get("rated_kva")) else float(g.get("rated_kw") or 0.0) / pf
    if not kva > SELFGEN_KVA_MIN:
        return 0.0
    if conn is None:
        conn = sites_mod.plant_connection(site)  # the plant's connection, as build_problem reads it
    data = (conn or {}).get("data") or {}
    prov = (data.get("tax") or {}).get("province")
    if not prov:
        disco = ((site or {}).get("data") or {}).get("disco")
        prov = (sites_mod.DISCO_PROVINCES.get(disco) or (None,))[0]
    if data.get("tariff_category"):
        cls = tariff_data.category_class(data["tariff_category"])
    else:
        cls = "agricultural" if ((site or {}).get("data") or {}).get("site_type") == "farm" else "industrial"
    if prov != "punjab" or cls not in ("industrial", "commercial"):
        return 0.0
    rate = tax_rules(on).get("punjab_selfgen_per_kwh")
    return float(rate) if _num(rate) and rate > 0 else 0.0


def _extra_tax_rate(conn: dict, tax: dict, amount: float) -> float | None:
    """Extra tax for an industrial or commercial connection not on the FBR's Active Taxpayer List (R9 section 7: 5 to 17 percent by bill-amount slab):
    the rate of the slab the amount falls in, from the tax version's `extra_tax_non_atl` ([{"upto": rupees or null, "rate": fraction}], ascending
    bounds, the last open). 0 where it does not apply; None where it applies and the version has no slabs (their bounds are not verified), so the
    bill says it is not priced rather than guess."""
    data = conn["data"]
    if (data.get("tax") or {}).get("atl", True) or tariff_data.category_class(data["tariff_category"]) not in ("industrial", "commercial"):
        return 0.0
    slabs = tax.get("extra_tax_non_atl") or []
    if not slabs:
        return None
    for s in slabs:
        if s.get("upto") is None or amount <= s["upto"]:
            return float(s["rate"])
    return float(slabs[-1]["rate"])


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


def _export_rate(nb: dict | None, peak: bool) -> float:
    """The Rs/kWh net billing credits an export at: the flat NAEPP, or by period where the version gives a `tou_export_schedule` (R9 13 step 9)."""
    nb = nb or {}
    sched = nb.get("tou_export_schedule")
    if isinstance(sched, dict):
        return float(sched["peak" if peak else "off_peak"])
    return float(nb.get("naepp", 0.0) or 0.0)


def _tou(card: dict) -> bool:
    return bool(card.get("tou") and card.get("peak") is not None)


def _resolve_days(kind: str, key: str, days: list[date]) -> list[dict | None]:
    """The active version of (kind, key) in force on each day, picked as `resolve` picks it, from one read of the versions."""
    _seeded()
    if _what_if_of(kind, key):
        vs = _active_versions(kind, key)
        return [_in_force(vs, d.isoformat()) for d in days]
    with store.connect() as con:
        rows = con.execute("SELECT * FROM tariff_versions WHERE kind = ? AND key = ? AND status = 'active' "
                           "ORDER BY effective_from DESC, created DESC, rowid DESC", (kind, key)).fetchall()
    vs = [_ver(r) for r in rows]
    out: list[dict | None] = []
    for d in days:
        s = d.isoformat()
        out.append(next((v for v in vs if v["effective_from"] <= s and (v["effective_to"] is None or v["effective_to"] >= s)), None))
    return out


def _day_span(first: date, last: date) -> str:
    """A run of days in words for a line's name: "1 Feb to 11 Feb 2026" (Pakistan days)."""
    a = f"{first.day} {first:%b}" + ("" if first.year == last.year else f" {first.year}")
    return f"{a} to {last.day} {last:%b} {last.year}" if first != last else f"{last.day} {last:%b} {last.year}"


def day_versions(conn: dict, days: list[date]) -> list[dict]:
    """The days of a period in runs priced alike (plan Part 4, the marginal-price rule; plan 6.1): each day takes the schedule card, TOU calendar, tax
    rules and prosumer versions in force that day, as the optimiser's vectors and the ledger price it, and a DISCO bills an interval across a new
    schedule pro rata by days. A run is consecutive days whose card, calendar, taxes and (for a connection with an export regime) prosumer bodies are
    the same, so two versions that repeat a card make one run. Raises NoTariffVersion for a day no schedule covers."""
    data = conn["data"]
    cat = data["tariff_category"]
    exporter = data.get("regime") in EXPORT_REGIMES
    cid = conn.get("id")
    found = {"schedule": _resolve_days("schedule", "national", days),
             "tou": _resolve_days("tou", "ke" if data.get("disco_group") == "ke" else "xwdisco", days),
             "tax": _resolve_days("tax", "national", days),
             "net_billing": _resolve_days("prosumer", "net_billing", days) if exporter else [None] * len(days),
             "net_metering_legacy": _resolve_days("prosumer", "net_metering_legacy", days) if exporter else [None] * len(days),
             # the site's own override (site_override), keyed by its connection
             "override": _resolve_days("override", str(cid), days) if cid else [None] * len(days)}
    others = ("tou", "tax", "net_billing", "net_metering_legacy", "override")
    runs: list[dict] = []
    for i, d in enumerate(days):
        s = found["schedule"][i]
        if s is None or cat not in (s["body"].get("cards") or {}):
            raise NoTariffVersion(f"No rate card for {cat} on {d.isoformat()}: no tariff version in force for that day covers it")
        bodies = {"card": s["body"]["cards"][cat], **{k: (found[k][i] or {}).get("body") for k in others}}
        key = json.dumps(bodies, sort_keys=True)
        used = [v for v in (s, *(found[k][i] for k in others)) if v is not None]
        if runs and runs[-1]["key"] == key:
            runs[-1]["last"], runs[-1]["days"] = d, runs[-1]["days"] + 1
            runs[-1]["versions"].update({v["id"]: v for v in used})
            runs[-1]["schedule_of"][d] = s
            continue
        ov = bodies["override"] or {}
        runs.append({"key": key, "first": d, "last": d, "days": 1, "schedule": s, "card": bodies["card"],
                     "cal": ov["calendar"] if isinstance(ov.get("calendar"), dict) else bodies["tou"] or {"seasons": []}, "tax": bodies["tax"] or {},
                     "nb": bodies["net_billing"], "lg": bodies["net_metering_legacy"], "ov": ov, "versions": {v["id"]: v for v in used},
                     # each day's own schedule version: two versions that repeat a card price alike and share a run (UX K14)
                     "schedule_of": {d: s}})
    return runs


def schedules_used(run_of: dict[date, dict]) -> list[dict]:
    """The schedule versions in force on a span of consecutive Karachi days (day_versions' runs by day), in order of the days: {version_id, name,
    effective_from, first_day, last_day} for each stretch of days on one schedule version. Each day's own version, not its run's first: two versions
    that repeat a card price alike and share a run, and the day still names the version in force on it. Words, ids and dates only: no rate (UX backend
    ask K14)."""
    out: list[dict] = []
    for d in sorted(run_of):
        run = run_of[d]
        s = (run.get("schedule_of") or {}).get(d) or run["schedule"]
        if out and out[-1]["version_id"] == s["id"] and date.fromisoformat(out[-1]["last_day"]) + timedelta(days=1) == d:
            out[-1]["last_day"] = d.isoformat()
            continue
        out.append({"version_id": s["id"], "name": version_name(s), "effective_from": s["effective_from"], "first_day": d.isoformat(),
                    "last_day": d.isoformat()})
    return out


def _mdi(ts: np.ndarray, imp: np.ndarray, peak: np.ndarray | None, interval: int, block: str) -> float:
    """The maximum demand (kW) of 1-minute imports: the largest mean over `interval` minutes, in fixed blocks aligned to the hour or in a window
    sliding minute by minute; with `peak` (a minute mask) only the peak hours count: a fixed block by its peak minutes, a sliding window only where
    every minute of it is a peak minute."""
    if not len(ts):
        return 0.0
    if block == "sliding":
        idx = ((ts - ts[0]) // 60).astype(np.int64)
        dense = np.zeros(int(idx[-1]) + 1)
        dense[idx] = imp
        ok = np.zeros(len(dense), dtype=bool)
        ok[idx] = True if peak is None else peak
        if len(dense) < interval:
            return 0.0
        sums = np.convolve(dense, np.ones(interval), "valid")
        full = np.convolve(ok.astype(float), np.ones(interval), "valid") >= interval - 0.5
        return float(sums[full].max() / interval) if full.any() else 0.0
    blocks = ((ts - ts[0] // 3600 * 3600) // (interval * 60)).astype(np.int64)
    blocks -= blocks.min()
    w = imp if peak is None else np.where(peak, imp, 0.0)
    sums = np.bincount(blocks, weights=w)
    counts = np.bincount(blocks, weights=np.ones(len(ts)) if peak is None else peak.astype(float))
    means = sums[counts > 0] / interval
    return float(means.max()) if len(means) else 0.0


#: The two lines step 9 adds when a credit is carried between bills
CREDIT_BROUGHT = "Credit brought forward"
CREDIT_CARRIED = "Credit carried to the next bill"


def settle(lines: list[dict], charges: float, credit_after_tax: float, brought: float) -> tuple[list[dict], float, float]:
    """Step 9's settlement: the export credit not netted before tax, plus a credit brought forward from the bill before, pays the charges; the bill
    never goes below zero and what is left is carried to the next bill (R9 13 step 9; under net billing, or paid out quarterly, Prosumer reg. 14(2)).
    Returns the lines with the brought-forward and carried lines (any old ones replaced), the total and the carried credit; the lines add up to it."""
    brought = max(0.0, float(brought or 0.0))
    avail = max(0.0, credit_after_tax) + brought
    applied = min(avail, max(0.0, charges))
    carried = avail - applied
    out = [ln for ln in lines if ln.get("line") not in (CREDIT_BROUGHT, CREDIT_CARRIED)]
    if brought > 0:
        out.append({"line": CREDIT_BROUGHT, "amount_rs": round(-brought, 2)})
    if carried >= 0.005:
        out.append({"line": CREDIT_CARRIED, "amount_rs": round(carried, 2)})
    return out, max(0.0, charges) - applied, carried


def rebuild(ts_utc: np.ndarray, import_kw: np.ndarray, export_kw: np.ndarray, conn: dict, period_start: date, period_end: date,
            energy_scalar: float = 1.0, mdi_scalar: float = 1.0, period_start_exclusive: bool = False, power_factor_pct: float | None = None,
            credit_brought_forward_rs: float = 0.0, billed_on: date | None = None) -> dict:
    """The connection's bill over the consumption days [period_start, period_end], both inclusive, from 1-minute series.

    `period_start` is the first day billed, the day after the previous meter reading, and `period_end` the reading date. A caller holding the two
    reading dates as printed on the DISCO bill (R9 section 13: start exclusive, end inclusive) passes `period_start_exclusive=True`, so the previous
    reading day is not billed twice and the proration counts the real reading interval.

    Each day is priced with the versions in force that day (`day_versions`; a reading interval across a new schedule bills its days pro rata, in
    lines named by their days); the adders, the ICP rule, s.235 and the flat fees are the billing month's (the reading date's). `power_factor_pct`
    (the bill's printed power factor) adds the PF penalty to a fixed charge set by maximum demand; `credit_brought_forward_rs` is the export credit
    the bill before carried (`credit_carried_rs` of its result).

    `billed_on` is the reading date that closes the bill when the days billed stop before it (the Overview's grid bill so far, verify.bill_so_far):
    its month is the billing month, so the adders, the ICP rule and its baseline month and the seasonal month are the bill's own, not those of the last
    day read; `period_end` by default.

    `ts_utc` holds epoch seconds of each minute's start (UTC); `import_kw` and `export_kw` the mean power of that minute. Returns the line items, the totals
    (as billed, and as the company bears them), the aggregates verification reads, and every version's hash with the series' (plan 6.3 step 13);
    `tariff_notices` names a version awaiting its notification whose start the period reaches (`awaiting_notices`)."""
    data = conn["data"]
    if period_start_exclusive:
        period_start = period_start + timedelta(days=1)
    n_days = (period_end - period_start).days + 1
    if n_days < 1:
        raise ValueError("A billing period runs forwards")
    days = [period_start + timedelta(days=i) for i in range(n_days)]
    runs = day_versions(conn, days)
    last = runs[-1]
    tax_end = last["tax"]
    tf_end = _tax_factor(conn, tax_end, last["ov"])
    closing = billed_on or period_end
    billing_month = closing.strftime("%Y-%m")
    t0 = datetime(period_start.year, period_start.month, period_start.day, tzinfo=PKT).timestamp()
    t1 = (datetime(period_end.year, period_end.month, period_end.day, tzinfo=PKT) + timedelta(days=1)).timestamp()
    mask = (ts_utc >= t0) & (ts_utc < t1)
    ts, imp, exp = ts_utc[mask], np.nan_to_num(import_kw[mask]) * energy_scalar, np.nan_to_num(export_kw[mask]) * energy_scalar
    series_hash = hashlib.sha256(np.ascontiguousarray(ts, dtype=np.int64).tobytes() + np.ascontiguousarray(imp, dtype=np.float64).tobytes()
                                 + np.ascontiguousarray(exp, dtype=np.float64).tobytes()).hexdigest()[:16]
    # 1. period tagging, by the calendar month of each minute in Pakistan time and the calendar of its day; each minute's run
    epoch0 = (period_start - date(1970, 1, 1)).days
    local = np.asarray(ts, dtype=np.int64) + 5 * 3600
    day_idx = (local // 86400 - epoch0).astype(np.int64)
    run_of_day = np.repeat(np.arange(len(runs)), [r["days"] for r in runs])
    run = run_of_day[day_idx] if len(ts) else np.zeros(0, dtype=np.int64)
    mod = (local % 86400) // 60
    cal_peak = np.zeros(len(ts), dtype=bool)
    for k in np.unique(day_idx):
        st, en = peak_window(runs[run_of_day[k]]["cal"], days[k].month)
        sel = day_idx == k
        m = mod[sel]
        cal_peak[sel] = ((st <= m) & (m < en)) if st <= en else ((m >= st) | (m < en))
    tou_run = np.array([_tou(r["card"]) for r in runs], dtype=bool)
    peak = cal_peak & tou_run[run] if len(ts) else cal_peak
    periods = (("peak", peak), ("off_peak", ~peak))
    # 2. energy by period, in total and by run
    e_imp = {p: float(imp[sel].sum() / 60.0) for p, sel in periods}
    e_exp = {p: float(exp[sel].sum() / 60.0) for p, sel in periods}
    e_gross = dict(e_imp)
    e_run = [{p: float(imp[sel & (run == i)].sum() / 60.0) for p, sel in periods} for i in range(len(runs))]
    # the regime each day is billed under (regime_on): legacy netting takes the imports and exports of days inside a legacy agreement, net billing
    # credits the exports of days under net billing (a legacy agreement's days after it ends among them), and an export on any other day earns nothing
    reg = _regime_minutes(conn, ts)
    in_legacy = reg == "net_metering_legacy"
    exp_nb = np.where(reg == "net_billing", exp, 0.0)
    # 3. maximum demand: blocks of the connection's interval, fixed and aligned to the hour (the default) or sliding minute by minute, over all
    # hours (the default) or the peak hours only, as the billing month's site override sets them (R9 13 step 3: configurable pending a bill)
    interval = int(data.get("mdi_interval_min", 30))
    block, scope = last["ov"].get("mdi_block", "fixed"), last["ov"].get("mdi_scope", "all_hours")
    mdi = _mdi(ts, imp, cal_peak if scope == "peak" else None, interval, block) * mdi_scalar
    sl = float(data["sanctioned_load_kw"])
    # legacy net metering nets by period first (R9 section 8.3; marked unverified), over the days inside the agreement only: an import on a day
    # after it ends is billed in full, as under net billing
    surplus_kwh = 0.0
    l_imp, l_exp = {"peak": 0.0, "off_peak": 0.0}, {"peak": 0.0, "off_peak": 0.0}
    legacy_any = bool(in_legacy.any())
    if legacy_any:
        for p, sel in periods:
            l_imp[p], l_exp[p] = float(imp[sel & in_legacy].sum() / 60.0), float(exp[sel & in_legacy].sum() / 60.0)
            surplus_kwh += max(0.0, l_exp[p] - l_imp[p])
            e_imp[p] = max(0.0, e_imp[p] - min(l_imp[p], l_exp[p]))
    # 4. the ICP split per period by monthly totals (the billing month's rule and baseline). At a legacy net-metering site the reference is imported
    # units: a period is eligible only with a net import in it, its increment is its imports over the baseline, and the increments are capped at
    # the month's imports less exports and split pro rata (SRO 2409(I)/2025, R9 section 5; plan 6.3 step 4); never more than the period's billed units
    icp = icp_rule(closing)
    incremental = {"peak": 0.0, "off_peak": 0.0}
    if icp and data["tariff_category"] in icp["categories"]:
        for p, _ in periods:
            base = icp_baseline(conn, closing.month, closing.year, p)
            if base is None:
                continue
            if legacy_any:
                incremental[p] = max(0.0, e_gross[p] - base) if e_gross[p] - l_exp[p] > 0 else 0.0
            else:
                incremental[p] = max(0.0, e_imp[p] - base)
        if legacy_any:
            cap = max(0.0, sum(e_gross.values()) - sum(l_exp.values()))
            inc = sum(incremental.values())
            if inc > cap:
                incremental = {p: v * cap / inc for p, v in incremental.items()}
            incremental = {p: min(v, e_imp[p]) for p, v in incremental.items()}
    inc_total = incremental["peak"] + incremental["off_peak"]

    def share(i: int, p: str) -> float:
        """Run i's part of the period's billed units: its share of the period's imports (of the days when nothing was imported)."""
        g = e_gross[p]
        return e_run[i][p] / g if g > 0 else runs[i]["days"] / n_days

    multi = len(runs) > 1

    def named(name: str, r: dict) -> str:
        return f"{name} ({_day_span(r['first'], r['last'])})" if multi else name

    # 5. energy charges: each run's part of the period's base units at its own rate; the ICP units at the billing month's ICP rate
    lines = []
    energy, icp_energy = 0.0, 0.0
    energy_run = [0.0] * len(runs)
    for p, _ in periods:
        base_units = e_imp[p] - incremental[p]
        for i, r in enumerate(runs):
            tou = _tou(r["card"])
            if not tou and p == "peak":
                continue  # a flat card has no peak units: its minutes are all in the one bucket
            units = base_units * share(i, p)
            rate = _energy_rate(r["card"], p == "peak")
            energy += units * rate
            energy_run[i] += units * rate
            lines.append({"line": named(f"Energy {p.replace('_', '-')}" if tou else "Energy", r), "units_kwh": round(units, 3), "rate": rate,
                          "amount_rs": round(units * rate, 2)})
        if incremental[p] > 0:
            icp_energy += incremental[p] * icp["rate"]
            lines.append({"line": f"ICP incremental {p.replace('_', '-')}", "units_kwh": round(incremental[p], 3), "rate": icp["rate"],
                          "amount_rs": round(incremental[p] * icp["rate"], 2)})
    energy += icp_energy
    # 6. fixed charges by basis, pro rata by days over the runs, with Tariff F's seasonal multiplier and the power-factor penalty (2 percent of a
    # fixed charge set by maximum demand per point of PF below 90 percent, R9 section 3); `days` of `of_days` is the proration the line shows
    prorate = n_days / 30.0 if abs(n_days - 30) > 4 else 1.0
    of_days = 30 if prorate != 1.0 else n_days
    seasonal = _seasonal_factor(data, closing.month)
    pf_factor = 1.0 + 0.02 * (90.0 - float(power_factor_pct)) if _num(power_factor_pct) and power_factor_pct < 90 else 1.0
    fixed = 0.0
    fixed_run = [0.0] * len(runs)
    for i, r in enumerate(runs):
        card = r["card"]
        part = prorate * r["days"] / n_days
        pf = 1.0
        if card["fixed_basis"] == "per_kw_month":
            billing_demand = max(card["floor_share"] * sl, mdi)
            pf = pf_factor
            f = card["fixed"] * billing_demand * part
        elif card["fixed_basis"] == "sanctioned_kw_month":
            billing_demand = sl
            f = card["fixed"] * sl * part
        else:
            billing_demand = 0.0
            f = card["fixed"] * part
        f *= seasonal * pf
        fixed += f
        fixed_run[i] = f
        line = {"line": named("Fixed charges", r), "billing_demand_kw": round(billing_demand, 2), "mdi_kw": round(mdi, 2), "rate": card["fixed"],
                "days": r["days"], "of_days": of_days, "amount_rs": round(f, 2)}
        if seasonal != 1.0:
            line["seasonal_factor"] = seasonal
        if pf != 1.0:
            line["power_factor_pct"], line["pf_factor"] = float(power_factor_pct), round(pf, 4)
        lines.append(line)
    # 7. adders by billing month, with their exemptions
    total_units = e_imp["peak"] + e_imp["off_peak"]
    adders = 0.0
    adder_versions = []
    for a in adders_for(billing_month):
        adder_versions.append({"kind": "adder", "key": a["key"], "id": a["id"], "hash": a.get("hash")})
        rate = adder_rate(a, conn, last["ov"])
        if rate is None:  # the adder exempts the connection's category or class
            continue
        units = total_units if _charged_on_icp_units(a, rate) else total_units - inc_total
        amt = rate * units
        adders += amt
        lines.append({"line": a.get("name", a["key"]), "units_kwh": round(units, 3), "rate": rate, "amount_rs": round(amt, 2)})
    # each run's part of the variable charges: its own energy, and of the ICP energy and the adders its share of the billed units
    w_run = [(sum(e_imp[p] * share(i, p) for p, _ in periods) / total_units) if total_units > 0 else runs[i]["days"] / n_days for i in range(len(runs))]
    var_run = [energy_run[i] + w_run[i] * (icp_energy + adders) for i in range(len(runs))]
    tf_run = [_tax_factor(conn, r["tax"], r["ov"]) for r in runs]
    # 8. electricity duty on the variable subtotal, and Punjab's per-unit duty above 500 kVA on the billed grid units
    duty_run = [tf_run[i]["ed"] * var_run[i] for i in range(len(runs))]
    duty = sum(duty_run)
    ed_rates = {tf["ed"] for tf in tf_run}
    lines.append({"line": "Electricity duty", **({"rate": tf_end["ed"]} if len(ed_rates) == 1 else {}), "amount_rs": round(duty, 2)})
    unit_rates = [_unit_duty(conn, r["tax"]) for r in runs]
    unit_duty_run = [unit_rates[i] * total_units * w_run[i] for i in range(len(runs))]
    unit_duty = sum(unit_duty_run)
    if unit_duty > 0:
        lines.append({"line": "Punjab electricity duty (per unit, above 500 kVA)", "units_kwh": round(total_units, 3),
                      **({"rate": unit_rates[-1]} if len(set(unit_rates)) == 1 else {}), "amount_rs": round(unit_duty, 2)})
    # 9. export credit: the legacy surplus at its rate, and the exports of days under net billing at the NAEPP (or by period where the version gives
    # a TOU export schedule), each day at its own version's rate; netted after taxes unless the regime's version says `credit_before_tax`
    credits: list[tuple[str, float, float, bool]] = []  # (kind, units, rupees, netted before tax)
    if surplus_kwh > 0:
        lx = [float(exp[in_legacy & (run == i)].sum() / 60.0) for i in range(len(runs))]
        tot = sum(lx)
        rate = sum((lx[i] / tot if tot > 0 else runs[i]["days"] / n_days) * _legacy_surplus_rate(r["card"], r["lg"]) for i, r in enumerate(runs))
        credits.append(("legacy", surplus_kwh, surplus_kwh * rate, bool((last["lg"] or {}).get("credit_before_tax"))))
    exported_nb = float(exp_nb.sum() / 60.0)
    if exported_nb > 0:
        rupees = sum(float(exp_nb[(run == i) & sel].sum() / 60.0) * _export_rate(r["nb"], p == "peak")
                     for i, r in enumerate(runs) for p, sel in (("peak", cal_peak), ("off_peak", ~cal_peak)))
        credits.append(("net_billing", exported_nb, rupees, bool((last["nb"] or {}).get("credit_before_tax"))))
    credit = sum(c[2] for c in credits)
    # 10. GST, further and extra tax on energy, fixed charges, adders and duty, each run at its own rates (a credit netted before tax off that base)
    base_run = [var_run[i] + fixed_run[i] + duty_run[i] + unit_duty_run[i] for i in range(len(runs))]
    base_total = sum(base_run)
    pre_tax = min(sum(c[2] for c in credits if c[3]), max(0.0, base_total))
    keep = 1.0 - pre_tax / base_total if base_total > 0 else 1.0
    taxable = base_total - pre_tax
    gst = sum(tf_run[i]["gst"] * base_run[i] * keep for i in range(len(runs)))
    lines.append({"line": "GST", "rate": tf_end["gst"], "amount_rs": round(gst, 2)})
    # further and extra tax are on the same base as GST, but a bill prints them after the income tax (R9 section 7's order: GST, s.235, then
    # further and extra tax), which is levied on the bill amount including them
    sales_lines: list[dict] = []
    further = sum(tf_run[i]["further"] * base_run[i] * keep for i in range(len(runs)))
    if further:
        sales_lines.append({"line": "Further tax", "rate": tf_end["further"], "amount_rs": round(further, 2)})
    unpriced: list[str] = []
    extra_rates = [_extra_tax_rate(conn, r["tax"], taxable) for r in runs]
    if any(x is None for x in extra_rates):
        unpriced.append("Extra tax for a connection not on the Active Taxpayer List: the tax version has no slabs for it, so the rebuilt bill leaves "
                        "it out")
    extra = sum((extra_rates[i] or 0.0) * base_run[i] * keep for i in range(len(runs)))
    if extra:
        sales_lines.append({"line": "Extra tax (not on the Active Taxpayer List)", **({"rate": extra_rates[-1]} if len(set(extra_rates)) == 1 else {}),
                            "amount_rs": round(extra, 2)})
    # 11. income tax s.235 on the bill amount (the billing month's slabs)
    slab = tax_end.get("s235", {}).get("industrial" if tf_end["class"] == "industrial" else "commercial")
    bill_amount = taxable + gst + further + extra
    s235 = 0.0
    if slab and tf_end["class"] in ("industrial", "commercial"):
        if bill_amount > slab["mid_upto"]:
            s235 = slab["base_above"] + slab["rate_above"] * (bill_amount - slab["mid_upto"])
        elif bill_amount > slab["nil_upto"]:
            s235 = slab["mid_rate"] * bill_amount
    lines.append({"line": "Income tax (s.235)", "amount_rs": round(s235, 2)})
    lines += sales_lines
    # 12. flat items
    flat = float(tax_end.get("ptv", {}).get(tf_end["class"], 0))
    if data.get("disco_group") == "ke" and tf_end["class"] in ("industrial", "commercial"):
        flat += float(tax_end.get("muct_ke", 0))
    if flat:
        lines.append({"line": "Flat fees (PTV, MUCT)", "amount_rs": round(flat, 2)})
    for kind, units, rupees, _ in credits:
        if rupees:
            # one regime in the period: "Export credit"; a legacy agreement that ends inside it bills its surplus and the net-billing exports apart
            name = "Export credit" if len(credits) == 1 else ("Export credit (legacy net metering)" if kind == "legacy" else "Export credit (net billing)")
            lines.append({"line": name, "units_kwh": round(units, 3), "rate": round(rupees / units, 4), "amount_rs": round(-rupees, 2)})
    charges = bill_amount + s235 + flat
    lines, total, carried = settle(lines, charges, credit - pre_tax, credit_brought_forward_rs)
    recovered = (gst if tf_end["gst_recoverable"] else 0.0) + (s235 if tf_end["s235_adjustable"] else 0.0)
    # what the company bears for the month: its charges less the taxes it recovers and this month's credit (used now or carried, it is worth the same;
    # a credit brought forward was the month before's)
    bearable = charges - recovered - (credit - pre_tax)
    versions = {v["id"]: v for r in runs for v in r["versions"].values()}
    icp_v = resolve("icp", "national", closing) if icp else None
    if icp_v:
        versions[icp_v["id"]] = icp_v
    out = {"lines": lines, "total_rs": round(total, 2), "bearable_rs": round(bearable, 2), "recovered_taxes_rs": round(recovered, 2),
           "energy_rs": round(energy + adders, 2), "demand_rs": round(fixed, 2), "credits_rs": round(credit, 2),
           "charges_rs": round(charges, 2), "credit_before_tax_rs": round(pre_tax, 2), "credit_brought_forward_rs": round(max(0.0, credit_brought_forward_rs or 0.0), 2),
           "credit_carried_rs": round(carried, 2),
           "import_kwh": e_imp, "import_gross_kwh": e_gross, "export_kwh": e_exp, "icp_incremental_kwh": incremental, "mdi_kw": round(mdi, 2),
           "schedule_version": last["schedule"]["id"], "schedule_hash": last["schedule"]["hash"], "billing_month": billing_month, "days": n_days,
           "first_day": period_start.isoformat(), "last_day": period_end.isoformat(),
           "versions": [{"kind": v["kind"], "key": v["key"], "id": v["id"], "hash": v["hash"], "effective_from": v["effective_from"]}
                        for v in sorted(versions.values(), key=lambda v: (v["kind"], v["key"], v["effective_from"]))] + adder_versions,
           "series_hash": series_hash, "unpriced": unpriced, "mdi_settings": {"interval_min": interval, "block": block, "scope": scope},
           # a version awaiting its notification whose start the period reaches: the bill is priced with the one still in force (engine-tariffs-06)
           "tariff_notices": awaiting_notices(period_end)}
    if _num(power_factor_pct):
        out["power_factor_pct"] = float(power_factor_pct)
    if multi:
        out["runs"] = [{"first_day": r["first"].isoformat(), "last_day": r["last"].isoformat(), "days": r["days"], "schedule_version": r["schedule"]["id"]}
                       for r in runs]
    return out


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
    force on the first step's day.

    `tariff_notices` lists the versions awaiting their notification whose start the horizon reaches (`awaiting_notices`): the steps from then on
    are priced with the version still in force.

    `schedules` names the schedule versions (the rate cards) the horizon's Karachi days were priced with, in order, each with the first and last
    day it priced (schedules_used; UX backend ask K14); empty off-grid."""
    h = 0.25
    out = {"c_imp": np.zeros(steps), "c_exp": np.zeros(steps), "peak": np.zeros(steps, dtype=bool), "month": np.zeros(steps, dtype=int),
           "local_hour": np.zeros(steps), "grid": np.ones(steps), "icp": {}, "mdi": None, "billing_month": [], "billing_month_end_step": steps,
           "export_tie_break": np.zeros(steps), "tariff_notices": [], "schedules": []}
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
    # a version awaiting its notification whose start the horizon reaches: priced with the version still in force, and said so (engine-tariffs-06)
    out["tariff_notices"] = awaiting_notices(max(bm_by_day))
    # each day priced with the versions in force that day, as the bill engine prices it (day_versions): a horizon across a new schedule changes card
    # at midnight; the per-unit duty is a rupee figure on every imported kWh
    run_of: dict[date, dict] = {}
    for r in day_versions(conn, sorted(set(bm_by_day) | {on})):
        d = r["first"]
        while d <= r["last"]:
            run_of[d] = r
            d += timedelta(days=1)
    out["schedules"] = schedules_used(run_of)
    day_price: dict[date, tuple[float, float]] = {}
    for d, r in run_of.items():
        tf = _tax_factor(conn, r["tax"], r["ov"])
        day_price[d] = ((1.0 + tf["ed"]) * _tax_stack(tf), _unit_duty(conn, r["tax"]) * _tax_stack(tf))
    card = run_of[on]["card"]
    ef, unit = day_price[on]
    tou = _tou(card)
    bm0 = bms[0] if bms else billing_month_for(conn, on)
    in_bm0 = [b == bm0 for b in bms]
    out["billing_month_end_step"] = next((i for i, x in enumerate(in_bm0) if not x), steps)
    # each adder at the connection's own rate (its category's, or none where it is exempt); on ICP units only those the bill charges there
    # (with the site override of the horizon's last day in that billing month, as the bill takes the billing month's)
    ov_bm = {bm: run_of[max(d for d, b in bm_by_day.items() if b == bm)]["ov"] for bm in dict.fromkeys(bms)} if bms else {bm0: run_of[on]["ov"]}
    rated = {bm: [(a, r) for a in adders_for(bm) for r in (adder_rate(a, conn, ov_bm.get(bm)),) if r is not None] for bm in dict.fromkeys(bms or [bm0])}
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

    def q_gross(p: str) -> float:
        """The current billing month's imports for a period, so far plus the forecast rest, before any netting."""
        return (mtd_import or {}).get(p, 0.0) + (rest_net_import or {}).get(p, 0.0)

    # ICP case per period (plan Part 4.3): above, below, or the uncertain middle that gets a month-level binary; the baseline is the billing month's.
    # At a legacy net-metering site the reference is imported units, as the bill counts them (SRO 2409(I)/2025, R9 section 5; tariffs.rebuild step 4;
    # round 7 stage 2, engine-tariffs-10's optimiser half, superseding round 3's "the ICP rows count net import"): a period's increment is its gross
    # imports over the baseline, it is eligible only while the period ends the month as a net import, and the increments are capped at the month's
    # imports less exports. `cap_kwh` (the month's net import so far and forecast) and `other_inc_kwh` (the other period's increment, settled or
    # forecast) let the optimiser's uncertain-case rows keep that cap (optimise.solve), so importing and exporting at once never earns the ICP rate
    bases = {p: icp_baseline(conn, bm0_day.month, bm0_day.year, p) if icp and data["tariff_category"] in icp["categories"] else None
             for p in ("peak", "off_peak")}
    gross_inc = {p: max(0.0, q_gross(p) - b) if b is not None and q_net(p) > 0 else 0.0 for p, b in bases.items()}
    for p in ("peak", "off_peak"):
        base = bases[p]
        if base is None:
            continue
        q = q_gross(p) if legacy else q_net(p)
        steps_in_p = sum(1 for i, loc in enumerate(locs) if in_bm0[i] and is_peak_local(run_of[loc.date()]["cal"], loc) == (p == "peak"))
        swing = g_max_kw * steps_in_p * h
        case = "above" if q - base > swing else "below" if base - q > swing else "uncertain"
        if legacy and q_net(p) < -swing:
            case = "below"                            # the period ends the month in surplus: no increment on it
        out["icp"][p] = {"case": case, "baseline_kwh": base, "q_rest_kwh": q - (mtd_import or {}).get(p, 0.0) + (mtd_import or {}).get(p, 0.0),
                         "mtd_kwh": (mtd_import or {}).get(p, 0.0), "q_kwh": q, "rate": (icp["rate"] + add_icp[bm0]) * ef + unit}
        if legacy:
            other = "off_peak" if p == "peak" else "peak"
            out["icp"][p].update(gross=True, cap_kwh=q_net("peak") + q_net("off_peak"), other_inc_kwh=gross_inc[other])
    # legacy net metering nets by period (rebuild step 3): a period that ends the month as a net import prices an exported kWh like an imported one,
    # with its adders, duty and taxes; one that ends in surplus moves only the credit, at the bare surplus rate (netted after taxes), both ways
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
        r = run_of[loc.date()]
        ef_d, unit_d = day_price[loc.date()]
        cal_pk = is_peak_local(r["cal"], loc)
        pk = cal_pk if _tou(r["card"]) else False
        out["peak"][i] = pk
        p = "peak" if pk else "off_peak"
        bm = bms[i]
        icp_case = out["icp"].get(p, {}).get("case") if in_bm0[i] else None
        if icp_case == "above":
            c_imp = (icp["rate"] + add_icp[bm]) * ef_d + unit_d
        else:
            c_imp = (_energy_rate(r["card"], pk) + add_all[bm]) * ef_d + unit_d
        regime = reg_day[loc.date()]
        if regime == "net_billing":
            c_exp = _export_rate(r["nb"], cal_pk)  # the NAEPP, or the version's TOU export schedule
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
                      "interval_steps": max(1, int(data.get("mdi_interval_min", 30)) // 15),
                      # the site's MDI block mode and scope (site override; fixed and all hours by default) for the optimiser's MDI rows
                      "block": run_of[on]["ov"].get("mdi_block", "fixed"), "scope": run_of[on]["ov"].get("mdi_scope", "all_hours")}
    return out


def _sites_by_connection() -> dict[str, str]:
    """Each grid connection's site name, by connection id (a site override is keyed by its connection)."""
    with store.connect() as con:
        rows = con.execute("SELECT c.id, s.name FROM connections c JOIN sites s ON s.id = c.site_id WHERE s.deleted IS NULL").fetchall()
    return {r[0]: r[1] for r in rows}


def provenance() -> list[dict]:
    """Every shipped and added number's source and status, for the tariff page's provenance view: who drafted it (`by`, "shipped" for the build's
    own; `by_id`, which the routes turn into `mine`), its withdrawal, and `template` (UX K32: a version the build ships from the notified schedules,
    not one the company drafted or checked)."""
    names = _sites_by_connection()
    return [{"id": v["id"], "kind": v["kind"], "key": v["key"], "effective_from": v["effective_from"], "effective_to": v["effective_to"], "status": v["status"],
             "name": version_name(v, names), "source": v["source"], "hash": v["hash"], "by": v.get("by"), "by_id": v.get("by_id") or "",
             "withdrawn_at": v.get("withdrawn_at"), "withdrawn_by": v.get("withdrawn_by") or "", "withdrawn_note": v.get("withdrawn_note") or "",
             "template": v.get("by") == "shipped"} for v in versions()]
