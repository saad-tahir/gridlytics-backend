"""The access requests (UX backend asks K2 and K3; plan B4, B20.4, B21.2 items 1, 2 and 4): what a visitor sends from the contact page ("Request
access", `/contact`), kept for the founder, who reads them and sets their state in the founder console's Access requests (`/admin/requests`).

A request is a request to the founder, never an account (the owner's rule: preassigned accounts only; no self-signup, no invitation). Nothing here makes,
finds or names an account, and the public answer is the same 202 with no body whatever the email, so it never says whether an email has an account. The
requests are the platform's, not a company's: they are kept in the registry (platform.db, schema 4, beside the founder's audit), with their times in UTC,
and answered in Pakistan time.

- **The public send** (`routes_access.access_request_new`): no sign-in; a body of at most BODY_BYTES (api.BODY_CAPS); the hidden field HONEYPOT filled
  means a machine filled the form, and the request is dropped silently (the same 202, nothing kept); RATE_LIMIT requests an hour from one address
  (platform.client_address), then 429 with Retry-After and the time it works again in Karachi time. A request sent while signed in records who sent it
  (`sent_by`), so the founder can tell an administrator's "Another person in my company" from anyone's.
- **The founder's list and change** (`GET` and `PATCH /api/founder/access-requests`), behind the founder's own session: newest first, filtered by state,
  with the count of each state for the console's filter; each change of state or note is written to the founder's audit.
- **Keeping** (B21.2 item 4): a request is kept KEEP_DAYS from when it came, and removed at the next send or the founder's next read, with one line in the
  founder's audit. The owner's question Q30 (how long access requests are kept) has no recommended answer in the plan; the privacy notice's example, 12
  months, ships until the owner decides (one number here).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import Field, field_validator, model_validator

from . import auth, sites
from . import platform as platformdb
from .schemas import Strict

PKT = timezone(timedelta(hours=5))

#: What a request is about (plan B4's radio cards, and `/contact?topic=`): access for my company, another person in my company, help with my account,
#: something else
TOPICS = ("access", "person", "support", "other")
#: The topics that need the company's name, and those that need a message (plan B4: "for the first two topics", "for the last two topics")
NEEDS_COMPANY = ("access", "person")
NEEDS_MESSAGE = ("support", "other")
#: A request's states, as the founder sets them (plan B20.4's filter: New, Contacted, Done, Declined)
STATES = ("new", "contacted", "done", "declined")
#: The optional site facts (plan B4's disclosure "Tell us about your site")
SITES = ("1", "2-5", "6+")
EQUIPMENT = ("solar_pv", "battery", "diesel_genset", "gas_genset", "grid")
INVERTERS = (*sites.BRANDS, "not_sure")
DISCOS = (*sites.DISCOS, "off_grid", "not_sure")
#: The most a send's body may hold (plan B21.2 item 1: 8 KB); api.BODY_CAPS holds the path to it
BODY_BYTES = 8 * 1024
#: Requests an hour from one address (plan B21.2 item 1: "for example 5 an hour"); GRIDLYTICS_RATE_ACCESS_REQUESTS changes it, 0 turns it off
RATE_LIMIT = auth._env_int("GRIDLYTICS_RATE_ACCESS_REQUESTS", 5)
RATE_WINDOW = 3600
#: How long a request is kept from when it came (the owner's question Q30; the privacy notice's "for example 12 months" until the owner decides)
KEEP_DAYS = 365
#: Requests a page of the founder's list
PAGE = 300
#: The hidden field people never see (plan B4: labelled "Leave this empty"): anything in it means a machine filled the form
HONEYPOT = "website"
#: The longest name, company, city, phone, email and message the form takes
MOST = {"name": 80, "company": 120, "city": 80, "phone": 32, "email": 254, "message": 1000, "note": 1000}

_SITES_WORDS = {"1": "1 site", "2-5": "2 to 5 sites", "6+": "6 or more sites"}
_BRAND_WORDS = {"deye": "Deye", "huawei": "Huawei", "sungrow": "Sungrow", "growatt": "Growatt", "voltronic": "Voltronic", "solis": "Solis",
                "other": "Another brand"}
#: A work email's shape (plan B4: "checked for shape only"): something, an @, and a domain with a dot
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+")
_PHONE = re.compile(r"\+?[0-9][0-9 ()-]{5,30}")
#: Control characters: none in a one-line field, and none but a line end or a tab in the message or the note
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _line(value: str, words: str, most: int) -> str:
    if len(value) > most:
        raise ValueError(f"{words} must be at most {most:,} characters")
    if _CONTROL.search(value):
        raise ValueError(f"{words} must be one line of text")
    return value


def _text(value: str, words: str, most: int) -> str:
    if len(value) > most:
        raise ValueError(f"{words} must be at most {most:,} characters")
    if _CONTROL_TEXT.search(value):
        raise ValueError(f"{words} must be plain text")
    return value


class AccessRequestIn(Strict):
    """`POST /api/public/access-requests`: the contact form (plan B4). The topic, the name and the work email always; the company for access and for
    another person; a message for help with an account and for something else; the site facts optional. `website` is the hidden field (HONEYPOT), which
    the route reads before this model: a filled one never gets here."""

    topic: str | None = Field(None, title="What this is about")
    # the two needed fields are checked when they are not sent, too (validate_default), so a missing one is refused in the same words
    name: str = Field("", title="The name", validate_default=True)
    email: str = Field("", title="The work email", validate_default=True)
    company: str = Field("", title="The company")
    phone: str = Field("", title="The phone number")
    city: str = Field("", title="The city")
    sites: str | None = Field(None, title="The number of sites")
    equipment: list[str] = Field(default_factory=list, title="What the site has")
    inverter: str | None = Field(None, title="The inverter brand")
    disco: str | None = Field(None, title="The DISCO")
    message: str = Field("", title="The message")
    website: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v:
            raise ValueError("Give your full name")
        return _line(v, "The name", MOST["name"])

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if len(v) > MOST["email"] or not _EMAIL.fullmatch(v):
            raise ValueError("Give a work email, such as name@company.com")
        return v

    @field_validator("company")
    @classmethod
    def _company(cls, v: str) -> str:
        return _line(v, "The company's name", MOST["company"])

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        if v and (len(v) > MOST["phone"] or not _PHONE.fullmatch(v)):
            raise ValueError("Give a phone number in digits, such as +92 300 1234567")
        return v

    @field_validator("city")
    @classmethod
    def _city(cls, v: str) -> str:
        return _line(v, "The city", MOST["city"])

    @field_validator("sites")
    @classmethod
    def _sites(cls, v: str | None) -> str | None:
        if v not in (None, *SITES):
            raise ValueError("The number of sites is 1, 2 to 5, or 6 or more")
        return v

    @field_validator("equipment")
    @classmethod
    def _equipment(cls, v: list[str]) -> list[str]:
        if any(e not in EQUIPMENT for e in v) or len(set(v)) != len(v):
            raise ValueError("What the site has is chosen once each from solar PV, battery, diesel genset, gas genset and grid connection")
        return v

    @field_validator("inverter")
    @classmethod
    def _inverter(cls, v: str | None) -> str | None:
        if v not in (None, *INVERTERS):
            raise ValueError("Choose the inverter brand from the list: Deye, Huawei, Sungrow, Growatt, Voltronic, Solis, another brand, or not sure")
        return v

    @field_validator("disco")
    @classmethod
    def _disco(cls, v: str | None) -> str | None:
        if v not in (None, *DISCOS):
            raise ValueError("Choose the DISCO from the list: one of the DISCOs, K-Electric, off-grid, or not sure")
        return v

    @field_validator("message")
    @classmethod
    def _message(cls, v: str) -> str:
        return _text(v, "The message", MOST["message"])

    @model_validator(mode="after")
    def _topic(self):
        if self.topic not in TOPICS:
            raise ValueError("Choose what this is about: access for your company, another person in your company, help with your account, or something "
                             "else")
        if self.topic in NEEDS_COMPANY and not self.company:
            raise ValueError("Give your company's name")
        if self.topic in NEEDS_MESSAGE and not self.message:
            raise ValueError("Write your message: say what you need")
        return self


class AccessRequestChange(Strict):
    """`PATCH /api/founder/access-requests/{id}`: the request's new state, the founder's note (an empty one clears it), or both."""

    state: str | None = Field(None, title="The state")
    note: str | None = Field(None, title="The note")

    @field_validator("state")
    @classmethod
    def _state(cls, v: str | None) -> str | None:
        if v not in (None, *STATES):
            raise ValueError("A request's state is new, contacted, done or declined")
        return v

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str | None:
        return None if v is None else _text(v, "The note", MOST["note"])

    @model_validator(mode="after")
    def _something(self):
        if self.state is None and self.note is None:
            raise ValueError("Send the request's new state, its note, or both")
        return self


# ------------------------------------------------------------------------------------------------ times and words

def _now() -> datetime:
    return datetime.fromtimestamp(platformdb._clock(), timezone.utc)


def karachi(stamp: str | datetime | None) -> str | None:
    """A stored UTC time as people read it: ISO 8601 in Pakistan time ("2026-11-11T14:13:00+05:00")."""
    if not stamp:
        return None
    t = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(stamp)
    return t.astimezone(PKT).isoformat(timespec="seconds")


def about(sites_: str | None, inverter: str | None, disco: str | None) -> str | None:
    """The table's "About the site" from the optional facts (plan B20.4: "2 to 5 sites · Deye · LESCO"); None when none was given (the page draws a
    dash). "Not sure" says nothing, and is left out."""
    parts = [_SITES_WORDS.get(sites_ or ""), _BRAND_WORDS.get(inverter or ""),
             "Off-grid" if disco == "off_grid" else disco if disco and disco != "not_sure" else None]
    said = [p for p in parts if p]
    return " · ".join(said) if said else None


def view(r) -> dict:
    """A request as the founder reads it: every field the visitor sent, the line about the site, its state and note, and its times in Karachi time."""
    return {"id": r["id"], "received_at": karachi(r["received_at"]), "topic": r["topic"], "name": r["name"], "email": r["email"], "company": r["company"],
            "phone": r["phone"], "city": r["city"], "sites": r["sites"], "equipment": json.loads(r["equipment"] or "[]"), "inverter": r["inverter"],
            "disco": r["disco"], "message": r["message"], "about": about(r["sites"], r["inverter"], r["disco"]), "state": r["state"], "note": r["note"],
            "updated_at": karachi(r["updated_at"]), "sent_by": json.loads(r["sent_by"]) if r["sent_by"] else None}


# ------------------------------------------------------------------------------------------------ the rate and the keeping

def wait(address: str) -> int:
    """Seconds before another request from this address is taken; 0 when it is (and it is counted). In memory, as the API's other rates: a restart
    forgets them."""
    return auth.limiter().hit(("access_request", address or "unknown"), RATE_LIMIT, RATE_WINDOW) if RATE_LIMIT else 0


def again(seconds: int) -> datetime:
    """When a send works again, as a whole minute in Karachi time, never before the limiter's wait is over (so "after 14:40" is true)."""
    t = (_now() + timedelta(seconds=seconds)).astimezone(PKT)
    return (t.replace(second=0, microsecond=0) + timedelta(minutes=1)) if (t.second or t.microsecond) else t


def prune() -> int:
    """Remove the requests kept longer than KEEP_DAYS from when they came; how many went. The founder's audit says so, in one line for each removal."""
    now = _now()
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    with platformdb._read() as con:
        if con.execute("SELECT 1 FROM access_requests WHERE received_at < ? LIMIT 1", (cutoff,)).fetchone() is None:
            return 0
    with platformdb._write() as con:
        gone = con.execute("DELETE FROM access_requests WHERE received_at < ?", (cutoff,)).rowcount
    if gone:
        platformdb.founder_audit("removed access requests past their keeping time", "",
                                 {"removed": gone, "kept_days": KEEP_DAYS, "received_before": karachi(cutoff)}, actor="server")
    return gone


# ------------------------------------------------------------------------------------------------ the store

def add(request: AccessRequestIn, sent_by: dict | None = None) -> str:
    """Keep a request, state new; its id. The ones past their keeping go first."""
    prune()
    request_id = str(uuid.uuid4())
    with platformdb._write() as con:
        con.execute("""INSERT INTO access_requests (id, received_at, topic, name, email, company, phone, city, sites, equipment, inverter, disco, message,
                       state, note, updated_at, sent_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'new','',NULL,?)""",
                    (request_id, _now().isoformat(timespec="seconds"), request.topic, request.name, request.email, request.company, request.phone,
                     request.city, request.sites, json.dumps(request.equipment), request.inverter, request.disco, request.message,
                     json.dumps(sent_by) if sent_by else None))
    return request_id


def listing(state: str = "all", offset: int = 0) -> dict:
    """The founder's list: the requests in `state` (or all), newest first, PAGE of them from `offset`, and how many there are in each state."""
    prune()
    with platformdb._read() as con:
        counts = dict.fromkeys(STATES, 0)
        for r in con.execute("SELECT state, COUNT(*) AS n FROM access_requests GROUP BY state"):
            counts[r["state"]] = r["n"]
        where, args = ("", ()) if state == "all" else ("WHERE state = ?", (state,))
        rows = con.execute(f"SELECT * FROM access_requests {where} ORDER BY received_at DESC, rowid DESC LIMIT ? OFFSET ?", (*args, PAGE, offset)).fetchall()
    return {"requests": [view(r) for r in rows], "counts": {**counts, "all": sum(counts.values())}}


def change(request_id: str, state: str | None, note: str | None) -> tuple[dict, dict] | None:
    """Set a request's state, its note, or both: (before, after); None when there is no such request. Nothing is written when nothing changes."""
    with platformdb._write() as con:
        r = con.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
        if r is None:
            return None
        fields = {k: v for k, v in (("state", state), ("note", note)) if v is not None and v != r[k]}
        if fields:
            fields["updated_at"] = _now().isoformat(timespec="seconds")
            con.execute(f"UPDATE access_requests SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), request_id))
        after = con.execute("SELECT * FROM access_requests WHERE id = ?", (request_id,)).fetchone()
    return view(r), view(after)
