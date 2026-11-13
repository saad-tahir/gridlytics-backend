"""The tariff editor's drafts and its checks (UX backend ask K32; plan F2.5, F11 item 6; HANDOFF's tariff editor).

A draft is the editor's whole work, saved on the server as its author types (`PUT /api/tariffs/drafts/{id}`): what the version changes (one version a
kind, each with its key and body), the day it starts and its source, and the editor's own state. It is seen by its author and by administrators,
never read by the engine and never counted as waiting; submitted, it becomes one pending version a kind with the same start and source, all or none
(tariffs.propose_many), and the draft is gone. The checks (`problems`) are the library's own (tariffs.body_problems, the start, the source and a
site override's connection), every one tied to its field and the editor's step, so the validate route, the submit and `POST /api/tariffs/versions`
refuse alike.

Stored in `tariff_drafts` (schema version 8, store._V8), keyed by the id the editor chose, with its author's sign-in id."""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone

from . import store, tariffs

PKT = timezone(timedelta(hours=5))
#: A draft's id, chosen by the editor (a uuid or a short random name)
DRAFT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: A draft's size, as stored (a site draft's limit)
DRAFT_MAX_BYTES = 200_000
#: The drafts `GET /api/tariffs/drafts` lists, newest first
DRAFTS_LISTED = 100
#: The fields a draft's changes are compared on, at most
CHANGES_LISTED = 200
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class Taken(ValueError):
    """Another person's draft has this id."""


class Gone(LookupError):
    """The draft being submitted is no longer there: submitted or discarded since it was read (UX K32 check)."""


class Changed(ValueError):
    """The draft being submitted was saved again since it was read and checked (UX K32 check)."""


def karachi(stamp: str | None) -> str | None:
    if not stamp:
        return None
    t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(PKT).isoformat(timespec="seconds")


def _key(kind: str, key: str) -> str:
    """A version's key as the editor sent it, or the one key its kind has (schedule, tax and the ICP are national)."""
    key = (key or "").strip()
    keys = tariffs._KEYS.get(kind)
    return keys[0] if not key and keys and len(keys) == 1 else key


def source_of(src: dict, on: date) -> dict:
    """The source a submitted draft's versions carry, as the raw-JSON dialog wrote one: {kind manual, ref, url, retrieved_on (the day it was
    submitted, Karachi), verified}."""
    return {"kind": "manual", "ref": (src.get("ref") or "").strip(), "url": (src.get("url") or "").strip(), "retrieved_on": on.isoformat(),
            "verified": src.get("verified")}


def items(body: dict, on: date) -> list[dict]:
    """A draft's versions as `tariffs.propose_many` takes them: {kind, key, effective_from, body, source}."""
    src = source_of(body.get("source") or {}, on)
    return [{"kind": v["kind"], "key": _key(v["kind"], v.get("key") or ""), "effective_from": body.get("effective_from") or "",
             "body": v.get("body") if isinstance(v.get("body"), dict) else {}, "source": src} for v in body.get("versions") or []]


def as_versions(body: dict, on: date, prefix: str = "draft") -> list[dict]:
    """A checked draft's versions as tariffs.what_if prices them (the shape `tariffs.versions` gives a row), ids "<prefix>:<n>"."""
    out = []
    for i, it in enumerate(items(body, on)):
        out.append({"id": f"{prefix}:{i + 1}", "kind": it["kind"], "key": it["key"], "effective_from": tariffs._canonical_day(it["effective_from"]),
                    "effective_to": None, "known_from": on.isoformat(), "status": "pending", "supersedes": None, "body": it["body"],
                    "source": it["source"], "hash": tariffs._hash(it["body"]), "created": store._now(), "by": "", "by_id": ""})
    return out


def problems(body: dict, on: date) -> list[dict]:
    """Every problem that stops a draft being submitted, each {version (its index, None for the start and the source), kind, key, field (dotted:
    "body.cards.B-3.peak"), path, step (the editor's: A to D), message}, in the order the editor's steps read: the start and the source first, then
    each version by the library's own checks (tariffs.body_problems, a site override's connection, a schedule that leaves a category in use without a
    rate card, which approving it would refuse)."""
    out: list[dict] = []

    def add(version, kind, key, path: tuple, message: str) -> None:
        if any(p["version"] == version and tuple(p["path"]) == tuple(path) for p in out):
            return
        out.append({"version": version, "kind": kind, "key": key, "field": ".".join(str(x) for x in path), "path": list(path),
                    "step": tariffs.step_of(kind or "", tuple(path)), "message": message})

    start = None
    try:
        start = tariffs._canonical_day(body.get("effective_from") or "", "The version's start")
    except ValueError as exc:
        add(None, None, None, ("effective_from",), str(exc))
    src = body.get("source") or {}
    if not (src.get("ref") or "").strip():
        add(None, None, None, ("source", "ref"), "Give the source reference: the SRO, NEPRA decision or bill it was read from")
    url = (src.get("url") or "").strip()
    if url and not _URL.fullmatch(url):
        add(None, None, None, ("source", "url"), "The source's web address is an http or https address")
    if src.get("verified") not in tariffs.VERIFIED:
        add(None, None, None, ("source", "verified"), "Say how far the source is checked: a primary source, a secondary source or unverified")
    vs = body.get("versions") or []
    if not vs:
        add(None, None, None, ("versions",), "Tick at least one thing this version changes")
    seen: set[tuple[str, str]] = set()
    for i, v in enumerate(vs):
        kind, key = v["kind"], _key(v["kind"], v.get("key") or "")
        if not key:
            add(i, kind, key, ("key",), "A site override names the grid connection it applies to" if kind == "override" else "A version needs its key")
        for path, message in tariffs.body_problems(kind, key, v.get("body") if isinstance(v.get("body"), dict) else {}, start):
            add(i, kind, key, path, editor_words(kind, tuple(path)) or message)
        words = tariffs.override_key_problem(kind, key) or tariffs.coverage_problem(kind, v.get("body"))
        if words:
            add(i, kind, key, ("key",) if kind == "override" else ("body", "cards"), words)
        if (kind, key) in seen:
            add(i, kind, key, ("key",), "This draft already changes that: one version a kind and key")
        seen.add((kind, key))
    return out


_RATE_WORDS = {"peak": "peak", "off_peak": "off-peak", "flat": "flat"}
#: The editor's words for a body's field, by kind and the field's last key (plan F2.5.2 to F2.5.4: the floor and the taxes are entered as percents,
#: "Percent entry, fraction stored"; the peak windows as times; the rates in Rs/kWh), where the library's own words name a stored key or a fraction.
#: `{cat}` is a rate card's category; a season's field is told by its number
_EDITOR_WORDS = {
    ("schedule", "fixed_basis"): "Choose how the fixed charge for {cat} is charged: per kW of billing demand, per sanctioned kW or per consumer, a month",
    ("schedule", "fixed"): "The fixed charge for {cat} is a number of rupees, 0 or more",
    ("schedule", "floor_share"): "The floor for {cat} is a percent of the sanctioned load, from 0 to 100",
    ("schedule", "peak"): "The peak rate for {cat} is a number of Rs/kWh, 0 or more",
    ("schedule", "off_peak"): "The off-peak rate for {cat} is a number of Rs/kWh, 0 or more",
    ("schedule", "flat"): "The card for {cat} needs a flat rate in Rs/kWh, 0 or more, or time of use with a peak and an off-peak rate",
    ("adder", "billing_months"): "Pick the billing months the adder is charged on, or every bill",
    ("adder", "exempt"): "Not charged on is a list of what is ticked: ICP incremental units, lifeline or prepaid consumers, EV charging, or a "
                         "category or class",
    ("adder", "category_rates"): "A rate by category gives a tariff category or class (for example agricultural) its rate in Rs/kWh",
    ("icp", "first_month"): "The ICP needs its first billing month",
    ("icp", "last_month"): "The ICP needs its last billing month",
    ("prosumer", "naepp"): "Net billing needs its export price, the NAEPP, in Rs/kWh",
    ("prosumer", "tou_export_schedule"): "A time-of-use export schedule gives a peak and an off-peak export price in Rs/kWh",
    ("prosumer", "credit_before_tax"): "Say whether the export credit comes off the bill before GST or after the taxes",
    ("prosumer", "nappp"): "Give the NAPPP: the notified national average power purchase price, in Rs/kWh",
    ("prosumer", "surplus_rate"): "Choose what the surplus is credited at: the notified NAPPP, the off-peak rate, the peak rate or a rate in Rs/kWh",
    ("prosumer", "netting"): "Legacy net metering nets by period only: peak against peak, off-peak against off-peak",
    ("tax", "gst"): "GST is a percent from 0 to under 100",
    ("tax", "further_tax_no_strn"): "The further tax without an STRN is a percent from 0 to under 100",
    ("tax", "electricity_duty"): "Electricity duty is a percent for each province and class, from 0 to under 100",
    ("tax", "s235"): "Each section 235 slab needs nil up to, the middle rate, its ceiling, the base and the rate above",
    ("tax", "extra_tax_non_atl"): "Each extra tax slab gives its ceiling in rupees, rising, with none on the last, and a percent from 0 to under 100",
    ("override", "electricity_duty"): "The site's electricity duty is a percent from 0 to under 100",
    ("override", "adders"): "The site's adders give each adder (for example DSS) its rate on the site's bill in Rs/kWh",
    ("override", "mdi_block"): "Choose the MDI blocks: fixed or sliding",
    ("override", "mdi_scope"): "Choose the MDI scope: all hours or the peak hours only",
}
_SEASON_WORDS = {"months": "Season {n} needs its months: one or more of January to December",
                 "peak": "Season {n} needs a peak window: a start and a different end, in 24-hour time"}


def editor_words(kind: str, path: tuple) -> str | None:
    """The tariff editor's words for a problem of a version's body by its field (UX K32 check), in the editor's units and without a stored key's name;
    None where the library's own words already read as the editor says them. The raw route (`POST /api/tariffs/versions`) and approval keep the
    library's words."""
    if len(path) < 2 or path[0] != "body":
        return None
    rest = path[2:] if kind == "override" and path[1] == "calendar" else path[1:]
    if len(rest) == 3 and rest[0] == "seasons" and isinstance(rest[1], int) and rest[2] in _SEASON_WORDS:
        return _SEASON_WORDS[rest[2]].format(n=rest[1] + 1)
    if kind == "schedule" and len(rest) == 3 and rest[0] == "cards":
        words = _EDITOR_WORDS.get((kind, rest[2]))
        return words.format(cat=rest[1]) if words else None
    if len(rest) == 1 and not (kind == "override" and path[1] == "calendar"):
        return _EDITOR_WORDS.get((kind, rest[0]))
    return None


def _leaves(obj, path=()) -> dict[tuple, object]:
    """A body's values by their path; a list is one value (the seasons of a calendar, the months an adder is billed in)."""
    if isinstance(obj, dict) and obj:
        out: dict[tuple, object] = {}
        for k, v in obj.items():
            out.update(_leaves(v, (*path, k)))
        return out
    return {path: obj}


def change_pct(before, after) -> float | None:
    """A number's change in percent of what it was, to 0.1: the one rule for every change the tariff editor shows (what a version changes, the
    preview's and the simulation's rates). None where there is nothing to compare (either is not a number, or it was 0) and where the change is past
    what a float holds (a rate the library takes, such as 1e308, against one of 2: an answer with inf in it cannot be written; UX K review)."""
    if not (tariffs._num(before) and tariffs._num(after)) or not before:
        return None
    pct = (after - before) / abs(before) * 100
    return round(pct, 1) if math.isfinite(pct) else None


def changes(before: dict | None, after: dict) -> list[dict]:
    """What a version changes against the one it follows, field by field: {field, path, before, after, change_pct} (`change_pct`). A field only one
    of them has reads null on the other side.
    Served under the money name `tariff_changes` (a rate before and after is money)."""
    old, new = _leaves(before or {}), _leaves(after or {})
    out = []
    for path in sorted(set(old) | set(new), key=lambda p: [str(x) for x in p]):
        a, b = old.get(path), new.get(path)
        if a == b or not path:
            continue
        out.append({"field": ".".join(["body", *(str(x) for x in path)]), "path": ["body", *path], "before": a, "after": b,
                    "change_pct": change_pct(a, b)})
    return out[:CHANGES_LISTED]


def against(body: dict, on: date) -> list[dict]:
    """Each version of a draft beside the version of its kind and key in force on its start (`in_force`: {id, name, effective_from}, null where
    none is, or where the start cannot be read) and what it changes against it (`tariff_changes`, a money name: its rates before and after are
    money, so the API's filter leaves a reader without see_costs none of them; UX K32 check)."""
    names = tariffs._sites_by_connection()
    try:
        start = tariffs._canonical_day(body.get("effective_from") or "")
    except ValueError:
        start = None
    out = []
    for it in items(body, on):
        cur = tariffs.resolve(it["kind"], it["key"], start) if start and it["key"] and it["kind"] in tariffs.KINDS else None
        out.append({"kind": it["kind"], "key": it["key"], "name": tariffs.version_name(it, names), "effective_from": start,
                    "in_force": {"id": cur["id"], "name": tariffs.version_name(cur, names), "effective_from": cur["effective_from"]} if cur else None,
                    "tariff_changes": changes(cur["body"] if cur else None, it["body"])})
    return out


def name_of(body: dict) -> str:
    """A draft's name for the list: its versions' names ("Adder, FCA" reads "FCA"), or words for an empty one."""
    names = tariffs._sites_by_connection()
    got = [tariffs.version_name({"kind": v["kind"], "key": _key(v["kind"], v.get("key") or ""), "body": v.get("body") or {}}, names)
           for v in body.get("versions") or []]
    if not got:
        return "A draft with nothing ticked yet"
    return got[0] if len(got) == 1 else f"{', '.join(got[:-1])} and {got[-1]}"


def _row(r, me: str) -> dict:
    """A draft as the routes serve it: its whole work under the money name `tariff_body` (its versions' rates are money under any key, such as an
    adder's `category_rates`; UX K32 check)."""
    body = json.loads(r["body"])
    return {"id": r["id"], "status": "draft", "name": name_of(body), "effective_from": body.get("effective_from") or None,
            "kinds": [v["kind"] for v in body.get("versions") or []], "author": r["person_name"] or None, "mine": r["person"] == me,
            "created": karachi(r["created"]), "updated": karachi(r["updated"]), "tariff_body": body}


def get(draft_id: str, me: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM tariff_drafts WHERE id = ?", (draft_id,)).fetchone()
    return _row(r, me) if r else None


def person_of(draft_id: str) -> str | None:
    with store.connect() as con:
        r = con.execute("SELECT person FROM tariff_drafts WHERE id = ?", (draft_id,)).fetchone()
    return r["person"] if r else None


def listing(me: str, everyone: bool) -> list[dict]:
    """The drafts of `me`, or everyone's (an administrator's list), newest first."""
    with store.connect() as con:
        if everyone:
            rows = con.execute("SELECT * FROM tariff_drafts ORDER BY updated DESC LIMIT ?", (DRAFTS_LISTED,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM tariff_drafts WHERE person = ? ORDER BY updated DESC LIMIT ?", (me, DRAFTS_LISTED)).fetchall()
    return [_row(r, me) for r in rows]


def put(draft_id: str, me: str, my_name: str, body: dict) -> dict:
    """Save a draft as its author typed it. An id another person's draft holds is refused (Taken), never written over; the check and the write are one
    write transaction."""
    text = json.dumps(body)
    if len(text) > DRAFT_MAX_BYTES:
        raise ValueError("A draft holds at most 200 KB")
    with store.write_transaction() as con:
        r = con.execute("SELECT person FROM tariff_drafts WHERE id = ?", (draft_id,)).fetchone()
        if r is not None and r["person"] != me:
            # said without whose it is: an analyst may not read another person's draft (UX K32 check)
            raise Taken("This draft id is taken: save the draft under a new id")
        now = store._now()
        con.execute("INSERT INTO tariff_drafts (id, person, person_name, body, created, updated) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "body = excluded.body, person_name = excluded.person_name, updated = excluded.updated", (draft_id, me, my_name, text, now, now))
    return get(draft_id, me)


def make(person: str, person_name: str, body: dict, con) -> str:
    """A new draft for a person (a version sent back to its author: tariffs.withdraw's `send_back`), under a new id, in the caller's transaction."""
    did = "returned-" + store.new_id()
    now = store._now()
    con.execute("INSERT INTO tariff_drafts (id, person, person_name, body, created, updated) VALUES (?,?,?,?,?,?)",
                (did, person, person_name, json.dumps(body), now, now))
    return did


def claim(draft_id: str, person: str, body: dict):
    """The step that takes a draft being submitted, run in the transaction that makes its versions (`tariffs.propose_many`'s `claim`; UX K32
    check): the draft is removed there, so two submits of it make its versions once. Raises Gone when it is no longer its author's (submitted or
    discarded since it was read) and Changed when it was saved again since it was read and checked: the versions are the draft that was checked."""

    def take(con) -> None:
        r = con.execute("SELECT body FROM tariff_drafts WHERE id = ? AND person = ?", (draft_id, person)).fetchone()
        if r is None:
            raise Gone("No such tariff draft")
        if json.loads(r["body"]) != body:
            raise Changed("The draft changed while it was being submitted: check it and submit it again")
        con.execute("DELETE FROM tariff_drafts WHERE id = ? AND person = ?", (draft_id, person))
    return take


def delete(draft_id: str, con) -> bool:
    """Remove a draft in the caller's transaction (its discard and the discard's audit line are one step; UX K review): False where it is already
    gone (submitted or discarded since it was read)."""
    return con.execute("DELETE FROM tariff_drafts WHERE id = ?", (draft_id,)).rowcount == 1


def from_version(v: dict) -> dict:
    """A draft holding a version (sent back to its author): its start, its source and its one kind."""
    src = v.get("source") or {}
    return {"effective_from": v["effective_from"], "source": {"ref": src.get("ref") or "", "url": src.get("url") or "",
                                                               "verified": src.get("verified") if src.get("verified") in tariffs.VERIFIED else None},
            "versions": [{"kind": v["kind"], "key": v["key"], "body": v["body"]}], "editor": {}}
