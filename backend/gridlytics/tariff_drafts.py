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
            add(i, kind, key, path, message)
        words = tariffs.override_key_problem(kind, key) or tariffs.coverage_problem(kind, v.get("body"))
        if words:
            add(i, kind, key, ("key",) if kind == "override" else ("body", "cards"), words)
        if (kind, key) in seen:
            add(i, kind, key, ("key",), "This draft already changes that: one version a kind and key")
        seen.add((kind, key))
    return out


def _leaves(obj, path=()) -> dict[tuple, object]:
    """A body's values by their path; a list is one value (the seasons of a calendar, the months an adder is billed in)."""
    if isinstance(obj, dict) and obj:
        out: dict[tuple, object] = {}
        for k, v in obj.items():
            out.update(_leaves(v, (*path, k)))
        return out
    return {path: obj}


def changes(before: dict | None, after: dict) -> list[dict]:
    """What a version changes against the one it follows, field by field: {field, path, before, after, change_pct} (the change of a number in
    percent of what it was, to 0.1; null where either is not a number or it was 0). A field only one of them has reads null on the other side."""
    old, new = _leaves(before or {}), _leaves(after or {})
    out = []
    for path in sorted(set(old) | set(new), key=lambda p: [str(x) for x in p]):
        a, b = old.get(path), new.get(path)
        if a == b or not path:
            continue
        pct = round((b - a) / abs(a) * 100, 1) if tariffs._num(a) and tariffs._num(b) and a else None
        out.append({"field": ".".join(["body", *(str(x) for x in path)]), "path": ["body", *path], "before": a, "after": b, "change_pct": pct})
    return out[:CHANGES_LISTED]


def against(body: dict, on: date) -> list[dict]:
    """Each version of a draft beside the version of its kind and key in force on its start (`in_force`: {id, name, effective_from}, null where
    none is, or where the start cannot be read) and what it changes against it (`changes`)."""
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
                    "changes": changes(cur["body"] if cur else None, it["body"])})
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
    body = json.loads(r["body"])
    return {"id": r["id"], "status": "draft", "name": name_of(body), "effective_from": body.get("effective_from") or None,
            "kinds": [v["kind"] for v in body.get("versions") or []], "author": r["person_name"] or None, "mine": r["person"] == me,
            "created": karachi(r["created"]), "updated": karachi(r["updated"]), "body": body}


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
            raise Taken("Another person's draft has this id: save the draft under a new id")
        now = store._now()
        con.execute("INSERT INTO tariff_drafts (id, person, person_name, body, created, updated) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "body = excluded.body, person_name = excluded.person_name, updated = excluded.updated", (draft_id, me, my_name, text, now, now))
    return get(draft_id, me)


def make(person: str, person_name: str, body: dict) -> str:
    """A new draft for a person (a version sent back to its author: tariffs.withdraw's `redraft`), under a new id."""
    did = "returned-" + store.new_id()
    now = store._now()
    with store.connect() as con:
        con.execute("INSERT INTO tariff_drafts (id, person, person_name, body, created, updated) VALUES (?,?,?,?,?,?)",
                    (did, person, person_name, json.dumps(body), now, now))
    return did


def delete(draft_id: str) -> None:
    with store.connect() as con:
        con.execute("DELETE FROM tariff_drafts WHERE id = ?", (draft_id,))


def from_version(v: dict) -> dict:
    """A draft holding a version (sent back to its author): its start, its source and its one kind."""
    src = v.get("source") or {}
    return {"effective_from": v["effective_from"], "source": {"ref": src.get("ref") or "", "url": src.get("url") or "",
                                                               "verified": src.get("verified") if src.get("verified") in tariffs.VERIFIED else None},
            "versions": [{"kind": v["kind"], "key": v["key"], "body": v["body"]}], "editor": {}}
