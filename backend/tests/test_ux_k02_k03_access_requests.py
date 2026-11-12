"""UX backend asks K2 and K3 (plan H11; B4, B20.4, B21.2 items 1, 2 and 4; G2.2.1, G4.6): the access requests.

K3: `POST /api/public/access-requests` is the contact page's "Request access": no sign-in, a body of at most 8 KB, a rate per address answering 429
with Retry-After, a hidden field dropped silently, and 202 with no body. It makes a request to the founder, never an account, and its answer never
says whether an email has an account (the owner's rule: preassigned accounts only). K2: `GET` and `PATCH /api/founder/access-requests` are the founder
console's Access requests: the founder's session only, newest first, filtered by state with a count of each, and every change in the founder's audit.
The requests are kept in the registry (platform.db, schema 4, migrated with its copy first), never in a company's file, for 12 months (the owner's
question Q30, the privacy notice's example until the owner decides).

Each test has its own registry and the founder console set up with made-up secrets (test_founder's `fdb`)."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

import identities
import pytest
from test_founder import _audit, _client, _console, _ok, _sentence, clock, fdb  # noqa: F401  (fixtures)

from gridlytics import access_requests, api, auth, schemas
from gridlytics import platform as platformdb
from gridlytics.ratelimit import Limiter

PKT = timezone(timedelta(hours=5))
URL = "/api/public/access-requests"
FOUNDER = "/api/founder/access-requests"
#: Plan B4's form, every field filled: access for a company, with the site facts
AYESHA = {"topic": "access", "name": "Ayesha Rahim", "email": "ayesha@rahimtextiles.pk", "company": "Rahim Textiles", "phone": "+92 300 1234567",
          "city": "Faisalabad", "sites": "2-5", "equipment": ["solar_pv", "battery", "diesel_genset"], "inverter": "deye", "disco": "LESCO",
          "message": "Two weaving units on a B-3 connection.", "website": ""}


#: The rate the API ships with (plan B21.2 item 1: "for example 5 an hour")
SHIPPED_RATE = access_requests.RATE_LIMIT


@pytest.fixture(autouse=True)
def _roomy(monkeypatch):
    """The tests that are not about the rate send more than five requests from one address: the rate's own tests set it back."""
    monkeypatch.setattr(access_requests, "RATE_LIMIT", 100)


def _send(body, address: str = "testclient", **kw):
    return _client(address).post(URL, json=body, **kw)


def _rows() -> list[dict]:
    """The requests kept, read straight from the registry's file; none when nothing has made the registry yet."""
    if not platformdb.db_path().exists():
        return []
    with closing(sqlite3.connect(platformdb.db_path())) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM access_requests ORDER BY rowid")]


def _listed(c=None, query: str = "") -> dict:
    return _ok((c or _console()).get(FOUNDER + query))


# ================================================================== K3: the public send

def test_a_visitor_sends_a_request_with_no_sign_in_and_is_answered_202_with_no_body(fdb, clock):
    r = _send(AYESHA)
    assert r.status_code == 202 and r.content == b"", r.text
    rows = _rows()
    assert len(rows) == 1
    kept = rows[0]
    # kept as sent, state new, its time in UTC; the hidden field is not kept
    assert {k: kept[k] for k in ("topic", "name", "email", "company", "phone", "city", "sites", "inverter", "disco", "message", "state", "note")} == {
        **{k: AYESHA[k] for k in ("topic", "name", "email", "company", "phone", "city", "sites", "inverter", "disco", "message")}, "state": "new", "note": ""}
    assert json.loads(kept["equipment"]) == AYESHA["equipment"] and "website" not in kept and kept["sent_by"] is None
    assert kept["received_at"] == datetime.fromtimestamp(clock.now, timezone.utc).isoformat(timespec="seconds")
    # a request, never an account: no account, no company, no membership is made
    assert platformdb.count_accounts() == 0 and platformdb.list_companies() == []
    # the versioned path is the same route
    assert _send({**AYESHA, "email": "bilal@rahimtextiles.pk"}).status_code == 202 and len(_rows()) == 2
    assert _client().post("/api/v1" + URL[4:], json={**AYESHA, "email": "sara@rahimtextiles.pk"}).status_code == 202 and len(_rows()) == 3


def test_the_answer_never_says_whether_the_email_has_an_account(fdb, clock):
    """The same 202, with the same headers and no body, for an email that has an account and one that has none; nothing is looked up or made."""
    platformdb.create_account("ayesha@rahimtextiles.pk", "Ayesha Rahim", "a long made-up password 1")
    held = _send(AYESHA)
    unknown = _send({**AYESHA, "email": "nobody@rahimtextiles.pk"})
    assert held.status_code == unknown.status_code == 202 and held.content == unknown.content == b""
    names = lambda r: sorted(k.lower() for k in r.headers if k.lower() not in ("x-request-id", "date"))  # noqa: E731
    assert names(held) == names(unknown)
    assert platformdb.count_accounts() == 1 and platformdb.account_by_email("nobody@rahimtextiles.pk") is None


def test_a_filled_hidden_field_is_dropped_silently(fdb, clock):
    """The honeypot (plan B4: labelled "Leave this empty", hidden from people): anything in it is a machine's, which is told 202 and nothing is kept,
    whatever else it sent."""
    for value in ("http://cheap-pills.example", "x", 1, True, ["a"]):
        r = _send({**AYESHA, "website": value})
        assert r.status_code == 202 and r.content == b"", (value, r.text)
    assert _send({"website": "spam", "rubbish": 1}).status_code == 202
    assert _rows() == []
    # empty, spaces or null is a person's: kept
    for value in ("", "   ", None):
        assert _send({**AYESHA, "website": value}).status_code == 202
    assert len(_rows()) == 3


def test_the_body_is_held_to_8_kb_however_it_is_sent(fdb, clock):
    """Plan B21.2 item 1: 8 KB, on this path only (every other path keeps the API's own limit)."""
    long = json.dumps({**AYESHA, "message": "x" * 1000}).encode()
    assert len(long) < access_requests.BODY_BYTES and _client().post(URL, content=long, headers={"content-type": "application/json"}).status_code == 202
    big = json.dumps({**AYESHA, "message": "x" * 1000, "city": " " * 9000}).encode()
    r = _client().post(URL, content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "too_large" and "8 KB" in r.json()["detail"], r.text
    # a chunked body, which says no length, is counted as it arrives; the versioned path is held to the same
    for path in (URL, "/api/v1" + URL[4:]):
        r = _client().post(path, content=iter([big[:5000], big[5000:]]), headers={"content-type": "application/json"})
        assert r.status_code == 413 and r.json()["code"] == "too_large", (path, r.text)
    assert len(_rows()) == 1
    assert api._body_cap("/api/sites/x/outages/schedule") == api.MAX_BODY and api._body_cap(URL) == 8192


def test_five_requests_an_hour_from_one_address_then_429_with_the_time_in_karachi(fdb, clock, monkeypatch):
    assert SHIPPED_RATE == 5 and access_requests.RATE_WINDOW == 3600
    monkeypatch.setattr(access_requests, "RATE_LIMIT", SHIPPED_RATE)
    t = [1000.0]
    monkeypatch.setattr(auth.limiter(), "clock", lambda: t[0])
    for i in range(access_requests.RATE_LIMIT):
        assert _send({**AYESHA, "email": f"p{i}@rahimtextiles.pk"}).status_code == 202
    r = _send(AYESHA)
    assert r.status_code == 429 and r.json()["code"] == "too_many_requests", r.text
    wait = int(r.headers["Retry-After"])
    assert 3590 <= wait <= 3601
    # the time it works again, a whole minute in Karachi time, never before the wait is over (the clock: 2027-01-15 13:00 in Karachi)
    at = datetime.fromisoformat(r.json()["retry_at"])
    assert at.utcoffset() == timedelta(hours=5) and at.second == 0 and at >= datetime.fromtimestamp(clock.now + wait, PKT)
    assert r.json()["retry_at"] == "2027-01-15T14:01:00+05:00"
    assert r.json()["detail"] == "Too many requests from this connection; try again after 14:01" and _sentence(r)
    assert len(_rows()) == access_requests.RATE_LIMIT
    # another address is counted on its own; a machine's filled hidden field is counted too
    assert _send(AYESHA, address="10.0.0.9").status_code == 202
    assert _send({**AYESHA, "website": "x"}).status_code == 429
    # an hour on, the address may send again
    t[0] += access_requests.RATE_WINDOW + 1
    assert _send(AYESHA).status_code == 202


def test_a_refused_request_is_not_counted_and_a_late_one_says_tomorrow(fdb, clock, monkeypatch):
    monkeypatch.setattr(access_requests, "RATE_LIMIT", SHIPPED_RATE)
    t = [1000.0]
    monkeypatch.setattr(auth.limiter(), "clock", lambda: t[0])
    for _ in range(12):
        assert _send({**AYESHA, "email": "not an email"}).status_code == 422
    for i in range(access_requests.RATE_LIMIT):
        assert _send({**AYESHA, "email": f"p{i}@rahimtextiles.pk"}).status_code == 202
    clock.now = datetime(2027, 1, 15, 23, 30, tzinfo=PKT).timestamp()             # 23:30 in Karachi: a send works again after midnight
    r = _send(AYESHA)
    assert r.status_code == 429 and r.json()["detail"].endswith("try again after 00:31 tomorrow"), r.text


@pytest.mark.parametrize("change, words", [
    ({"topic": None}, "Choose what this is about: access for your company, another person in your company, help with your account, or something else"),
    ({"topic": "sales"}, "Choose what this is about"),
    ({"name": ""}, "Give your full name"),
    ({"name": "   "}, "Give your full name"),
    ({"name": "x" * 81}, "The name must be at most 80 characters"),
    ({"name": "Ayesha\nRahim"}, "The name must be one line of text"),
    ({"name": 5}, "The name must be text"),
    ({"email": "ayesha"}, "Give a work email, such as name@company.com"),
    ({"email": "ayesha@localhost"}, "Give a work email, such as name@company.com"),
    ({"email": "a" * 250 + "@x.pk"}, "Give a work email, such as name@company.com"),
    ({"company": ""}, "Give your company's name"),
    ({"topic": "person", "company": " "}, "Give your company's name"),
    ({"company": "c" * 121}, "The company's name must be at most 120 characters"),
    ({"phone": "call me"}, "Give a phone number in digits, such as +92 300 1234567"),
    ({"city": "c" * 81}, "The city must be at most 80 characters"),
    ({"sites": "3"}, "The number of sites is 1, 2 to 5, or 6 or more"),
    ({"sites": 2}, "The number of sites must be text"),
    ({"equipment": ["wind"]}, "What the site has is chosen once each from solar PV, battery, diesel genset, gas genset and grid connection"),
    ({"equipment": ["battery", "battery"]}, "What the site has is chosen once each"),
    ({"equipment": "battery"}, "What the site has must be a list"),
    ({"inverter": "tesla"}, "Choose the inverter brand from the list"),
    ({"disco": "WAPDA"}, "Choose the DISCO from the list"),
    ({"message": "m" * 1001}, "The message must be at most 1,000 characters"),
    ({"message": "a\x00b"}, "The message must be plain text"),
    ({"topic": "support", "message": ""}, "Write your message: say what you need"),
    ({"topic": "other", "message": "  "}, "Write your message"),
    ({"role": "admin"}, "Unknown field 'role'"),
    ({"password": "let me in please"}, "Unknown field 'password'"),
    # the check (UX K3): what the founder's console would show as something else, and the needed fields sent null
    ({"email": "a\x07b@rahimtextiles.pk"}, "Give a work email, such as name@company.com"),
    ({"email": "<b>a</b>@rahimtextiles.pk"}, "Give a work email, such as name@company.com"),
    ({"email": "ayesha@rahimtextiles.pk\u202e"}, "Give a work email, such as name@company.com"),
    ({"email": "ay\u200besha@rahimtextiles.pk"}, "Give a work email, such as name@company.com"),
    ({"email": "\"ayesha\"@rahimtextiles.pk"}, "Give a work email, such as name@company.com"),
    ({"name": "Ayesha\u202eRahim"}, "The name must be plain text"),
    ({"name": "Ayesha\u2066Rahim"}, "The name must be plain text"),
    ({"name": "Ayesha\x85Rahim"}, "The name must be one line of text"),
    ({"name": "Ayesha\u2028Rahim"}, "The name must be one line of text"),
    ({"company": "Rahim\u202dTextiles"}, "The company's name must be plain text"),
    ({"city": "Faisal\u202eabad"}, "The city must be plain text"),
    ({"message": "Two units \u202eon B-3"}, "The message must be plain text"),
    ({"message": "Two units\x9bon B-3"}, "The message must be plain text"),
    ({"phone": "1-----"}, "Give a phone number in digits, such as +92 300 1234567"),
    ({"phone": "((((((("}, "Give a phone number in digits, such as +92 300 1234567"),
    ({"phone": "12345"}, "Give a phone number in digits, such as +92 300 1234567"),
    ({"name": None}, "Give your full name"),
    ({"email": None}, "Give a work email, such as name@company.com"),
    ({"topic": ""}, "Choose what this is about"),
    ({"topic": "access", "company": None}, "Give your company's name"),
    ({"topic": "support", "message": None}, "Write your message: say what you need"),
])
def test_each_field_is_refused_in_one_plain_sentence(fdb, clock, change, words):
    body = {**AYESHA, **change}
    if change.get("topic") is None and "topic" in change:
        body.pop("topic")
    r = _send(body)
    assert r.status_code == 422 and r.json()["code"] == "invalid", r.text
    detail = r.json()["detail"]
    assert detail.startswith(words) and _sentence(r) and "Input should" not in detail and "Value error" not in detail, detail
    assert _rows() == []


def test_a_body_that_is_not_a_form_is_refused_in_a_sentence(fdb, clock):
    for body in ([AYESHA], "text", 5, None):
        r = _send(body)
        assert r.status_code == 422 and _sentence(r) and "Input should" not in r.text, (body, r.text)
    r = _client().post(URL, content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_body"
    assert _client().post(URL, content=b"name=x", headers={"content-type": "application/x-www-form-urlencoded"}).json()["code"] == "must_be_json"
    # the least a request needs: a topic, a name and a work email, and for help with an account a message
    assert _send({"topic": "support", "name": "Bilal", "email": "bilal@x.pk", "message": "Locked out"}).status_code == 202
    assert _send({"topic": "access", "name": "Bilal", "email": "bilal@x.pk", "company": "Bilal Foods"}).status_code == 202
    assert [r["company"] for r in _rows()] == ["", "Bilal Foods"]


def test_a_signed_in_sender_is_recorded_and_a_bad_token_is_a_visitor(fdb, clock):
    """A request sent while signed in records who sent it (People's "Ask OnyxLeaf to add someone", plan G2.2.1), so the founder can tell an
    administrator's "Another person in my company" from anyone's; a token that is not good is a visitor's, never a refusal."""
    company = identities.company("Rahim Textiles")
    token = identities.token("ayesha@rahimtextiles.pk", "admin", company, company_role="owner", name="Ayesha Rahim")
    body = {**AYESHA, "topic": "person", "message": "Please add Bilal Ahmed as an operator."}
    assert _client().post(URL, json=body, headers=identities.headers(token, company)).status_code == 202
    assert _client().post(URL, json=body, headers={"Authorization": "Bearer not-a-token"}).status_code == 202
    first, second = access_requests.listing()["requests"][::-1]
    assert first["sent_by"] == {"email": "ayesha@rahimtextiles.pk", "name": "Ayesha Rahim", "company": "Rahim Textiles", "role": "admin"}
    assert second["sent_by"] is None


# ================================================================== the store: a migration of the registry, and the keeping

def test_the_request_store_is_a_migration_of_the_registry_with_its_copy_made_first(fdb, clock):
    """Schema 4 of platform.db: a registry of schema 3 holding an account is copied to backup/ before the table is made, and keeps the account."""
    platformdb.create_account("kept@example.pk", "Kept", "a long made-up password 1")
    path = platformdb.db_path()
    with closing(sqlite3.connect(path)) as con:                                   # put it back to schema 3, as a server of before holds it
        con.execute("DROP TABLE access_requests")
        con.execute("PRAGMA user_version = 3")
        con.commit()
    assert not list((path.parent / "backup").glob("*.db"))
    assert _send(AYESHA).status_code == 202
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == platformdb.SCHEMA_VERSION == 4
        assert con.execute("SELECT COUNT(*) FROM access_requests").fetchone()[0] == 1
    copies = list((path.parent / "backup").glob("platform-*-schema-v3.db"))
    assert len(copies) == 1
    with closing(sqlite3.connect(copies[0])) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 3
        assert con.execute("SELECT email FROM accounts").fetchone()[0] == "kept@example.pk"
        assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'access_requests'").fetchone()[0] == 0
    assert platformdb.account_by_email("kept@example.pk") is not None
    # a company's file never holds a request
    assert not platformdb.companies_dir().exists() or not any(platformdb.companies_dir().glob("*.db"))
    # the state is held by the table too
    with closing(sqlite3.connect(path)) as con, pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE access_requests SET state = 'archived'")


def test_a_request_is_kept_twelve_months_then_removed_with_a_line_in_the_founders_audit(fdb, clock):
    """B21.2 item 4 and the owner's question Q30 (no recommended answer; the privacy notice's "for example 12 months" ships): removed at the next send
    or the founder's next read."""
    assert access_requests.KEEP_DAYS == 365
    assert _send(AYESHA).status_code == 202
    clock.step(200 * 86400)
    assert _send({**AYESHA, "email": "later@rahimtextiles.pk"}, address="10.0.0.2").status_code == 202
    clock.step(166 * 86400)                                                       # the first is 366 days old, the second 166
    got = access_requests.listing()                                              # the founder's read
    assert [r["email"] for r in got["requests"]] == ["later@rahimtextiles.pk"] and got["counts"]["all"] == 1
    line = next(e for e in _audit() if e["action"] == "removed access requests past their keeping time")
    assert line["actor"] == "server" and line["detail"]["removed"] == 1 and line["detail"]["kept_days"] == 365
    assert line["detail"]["received_before"].endswith("+05:00")
    assert len(_rows()) == 1
    # nothing to remove writes nothing
    count = len(_audit())
    access_requests.listing()
    assert len(_audit()) == count


# ================================================================== K2: the founder's list and change

def test_the_founder_reads_every_field_newest_first_in_karachi_time(fdb, clock):
    assert _send(AYESHA).status_code == 202
    clock.step(3600)
    assert _send({"topic": "support", "name": "Bilal Ahmed", "email": "bilal@kamalfoods.pk", "message": "Locked out"}, address="10.0.0.2").status_code == 202
    got = _listed()
    assert [r["name"] for r in got["requests"]] == ["Bilal Ahmed", "Ayesha Rahim"]
    assert got["counts"] == {"new": 2, "contacted": 0, "done": 0, "declined": 0, "all": 2}
    ayesha = got["requests"][1]
    assert set(ayesha) == {"id", "received_at", "topic", "name", "email", "company", "phone", "city", "sites", "equipment", "inverter", "disco", "message",
                           "about", "state", "note", "updated_at", "sent_by"}
    assert uuid.UUID(ayesha["id"]) and ayesha["received_at"] == "2027-01-15T13:00:00+05:00" and ayesha["updated_at"] is None
    assert ayesha["about"] == "2 to 5 sites · Deye · LESCO" and ayesha["equipment"] == ["solar_pv", "battery", "diesel_genset"]
    assert ayesha["state"] == "new" and ayesha["note"] == "" and ayesha["message"] == AYESHA["message"]
    bilal = got["requests"][0]
    assert bilal["received_at"] == "2027-01-15T14:00:00+05:00" and bilal["about"] is None and bilal["company"] == "" and bilal["sites"] is None
    assert access_requests.about("6+", "other", "off_grid") == "6 or more sites · Another brand · Off-grid"
    assert access_requests.about("1", "not_sure", "not_sure") == "1 site" and access_requests.about(None, None, "K-Electric") == "K-Electric"


def test_the_founder_sets_a_state_and_a_note_and_each_change_is_in_the_founders_audit(fdb, clock):
    assert _send(AYESHA).status_code == 202
    c = _console()
    rid = _listed(c)["requests"][0]["id"]
    clock.step(120)
    got = _ok(c.patch(f"{FOUNDER}/{rid}", json={"state": "contacted", "note": "Called her; a pilot at Unit 2 first."}))["request"]
    assert got["state"] == "contacted" and got["note"] == "Called her; a pilot at Unit 2 first." and got["updated_at"] == "2027-01-15T13:02:00+05:00"
    line = _audit()[0]
    assert line["action"] == "changed an access request" and line["actor"] == "founder" and line["target"] == "ayesha@rahimtextiles.pk"
    assert line["detail"] == {"request": rid, "topic": "access", "state": {"from": "new", "to": "contacted"},
                              "note": {"from": "", "to": "Called her; a pilot at Unit 2 first."}}
    # one at a time; the same again writes nothing
    _ok(c.patch(f"{FOUNDER}/{rid}", json={"state": "done"}))
    assert _audit()[0]["detail"] == {"request": rid, "topic": "access", "state": {"from": "contacted", "to": "done"}}
    count = len(_audit())
    assert _ok(c.patch(f"{FOUNDER}/{rid}", json={"state": "done", "note": "Called her; a pilot at Unit 2 first."}))["request"]["state"] == "done"
    assert len(_audit()) == count
    # an empty note clears it; back to new is allowed
    assert _ok(c.patch(f"{FOUNDER}/{rid}", json={"note": "", "state": "new"}))["request"]["note"] == ""
    assert _listed(c)["counts"] == {"new": 1, "contacted": 0, "done": 0, "declined": 0, "all": 1}


@pytest.mark.parametrize("body, status, words", [
    ({}, 422, "Send the request's new state, its note, or both"),
    ({"state": None, "note": None}, 422, "Send the request's new state, its note, or both"),
    ({"state": "archived"}, 422, "A request's state is new, contacted, done or declined"),
    ({"state": 1}, 422, "The state must be text"),
    ({"note": "n" * 1001}, 422, "The note must be at most 1,000 characters"),
    ({"state": "done", "email": "x@y.pk"}, 422, "Unknown field 'email'"),
    ([], 422, "Send the change as a set of named values"),
    ("done", 422, "Send the change as a set of named values"),
])
def test_a_change_of_the_wrong_shape_is_refused_in_a_sentence(fdb, clock, body, status, words):
    assert _send(AYESHA).status_code == 202
    c = _console()
    rid = _listed(c)["requests"][0]["id"]
    r = c.patch(f"{FOUNDER}/{rid}", json=body)
    assert r.status_code == status and r.json()["detail"].startswith(words) and _sentence(r), r.text
    assert _listed(c)["requests"][0]["state"] == "new" and not [e for e in _audit() if e["action"] == "changed an access request"]


def test_an_unknown_request_is_not_found(fdb, clock):
    c = _console()
    for rid in (str(uuid.uuid4()), "12", "x" * 80):
        r = c.patch(f"{FOUNDER}/{rid}", json={"state": "done"})
        assert r.status_code == 404 and r.json()["detail"] == "No such access request", r.text


def test_the_founder_filters_by_state_with_counts_and_pages(fdb, clock, monkeypatch):
    for i in range(5):
        assert _send({**AYESHA, "email": f"p{i}@rahimtextiles.pk"}, address=f"10.0.0.{i}").status_code == 202
        clock.step(60)
    c = _console()
    ids = [r["id"] for r in _listed(c)["requests"]]                               # newest first: p4 .. p0
    _ok(c.patch(f"{FOUNDER}/{ids[0]}", json={"state": "contacted"}))
    _ok(c.patch(f"{FOUNDER}/{ids[1]}", json={"state": "declined"}))
    _ok(c.patch(f"{FOUNDER}/{ids[2]}", json={"state": "done"}))
    counts = {"new": 2, "contacted": 1, "done": 1, "declined": 1, "all": 5}
    for state, want in (("new", ids[3:]), ("contacted", ids[:1]), ("declined", ids[1:2]), ("done", ids[2:3]), ("all", ids)):
        got = _listed(c, f"?state={state}")
        assert [r["id"] for r in got["requests"]] == want and got["counts"] == counts, state
    assert [r["id"] for r in _listed(c)["requests"]] == ids
    monkeypatch.setattr(access_requests, "PAGE", 2)
    assert [r["id"] for r in _listed(c, "?offset=2")["requests"]] == ids[2:4]
    assert [r["id"] for r in _listed(c, "?state=new&offset=1")["requests"]] == ids[4:]
    for query, words in (("?state=archived", "Filter the requests by state"), ("?offset=-1", "The offset is a whole number from 0"),
                         ("?offset=x", "The offset is a whole number from 0")):
        r = c.get(FOUNDER + query)
        assert r.status_code == 422 and r.json()["detail"].startswith(words) and _sentence(r), (query, r.text)


def test_only_the_founder_reads_or_changes_the_requests(fdb, clock, monkeypatch):
    """The founder's own session, whatever else the request carries: no session, a company member's token or a company header never stands in for it
    (the guard lets /api/founder through; the route checks x-founder-session before it reads a field)."""
    assert _send(AYESHA).status_code == 202
    rid = _listed()["requests"][0]["id"]
    member = identities.token("member@acme.test", "admin", company_role="owner")
    for prefix in ("/api", "/api/v1"):
        for headers in ({}, identities.headers(member, identities.company()), {"x-founder-session": "x.y"}):
            want = "founder_session_invalid" if "x-founder-session" in headers else "founder_session_required"
            for method, path, body in (("GET", "/founder/access-requests", None), ("PATCH", f"/founder/access-requests/{rid}", {"state": "done"}),
                                       ("PATCH", "/founder/access-requests/x", [1])):
                r = _client().request(method, prefix + path, headers=headers, json=body)
                assert r.status_code == 401 and r.json()["code"] == want, (method, path, headers.keys(), r.text)
    assert _listed()["requests"][0]["state"] == "new"
    # until the console is set up its routes answer 503, a session made before too; the public send still takes a request
    console = _console()
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_TOTP_SECRET", " ")
    r = console.get(FOUNDER)
    assert r.status_code == 503 and r.json()["code"] == "unavailable", r.text
    assert _send({**AYESHA, "email": "late@rahimtextiles.pk"}).status_code == 202 and len(_rows()) == 2


# ================================================================== the check (batch k2's checkers' findings)

def test_an_optional_field_left_empty_is_taken_whether_it_comes_empty_or_null(fdb, clock):
    """The contract says an optional field may be left out, or sent empty or null: a select left on its empty choice sends "", a cleared text field may
    send null. Each was refused (null for the text fields, "" for the choices, either for the equipment) with a sentence about a field nobody filled."""
    base = {"topic": "support", "name": "Bilal Ahmed", "email": "bilal@kamalfoods.pk", "message": "Locked out of my account"}
    optional = ("company", "phone", "city", "sites", "equipment", "inverter", "disco", "website")
    for empty in (None, "", "   "):
        r = _send({**base, **dict.fromkeys(optional, empty)})
        assert r.status_code == 202, (empty, r.text)
    rows = _rows()
    assert len(rows) == 3
    for row in rows:
        assert (row["company"], row["phone"], row["city"], row["sites"], row["inverter"], row["disco"]) == ("", "", "", None, None, None), row
        assert json.loads(row["equipment"]) == []
    # each alone, as a page sends it: the text fields null, the choices ""
    for field, empty in (("company", None), ("phone", None), ("city", None), ("message", None), ("sites", ""), ("inverter", ""), ("disco", ""),
                         ("equipment", None), ("equipment", "")):
        # the company is needed for access, the message for something else: each is left empty under a topic that does not need it
        body = {**AYESHA, "topic": "access" if field == "message" else "other", field: empty}
        assert _send(body).status_code == 202, (field, empty)
    # an empty choice reads as not given on the founder's side (the first three, oldest last)
    listed = access_requests.listing()["requests"]
    assert len(listed) == 12 and [r["about"] for r in listed[-3:]] == [None, None, None]


def test_a_phone_number_is_taken_as_pakistan_writes_it(fdb, clock):
    """"(042) 35761234", a landline as it is usually written, was refused although the contract takes brackets."""
    for phone in ("(042) 35761234", "+92 (42) 3576 1234", "042 3576 1234", "0300-1234567", "+923001234567", "+(92) 300 1234567"):
        r = _send({**AYESHA, "phone": phone})
        assert r.status_code == 202, (phone, r.text)
    assert [r["phone"] for r in _rows()][:1] == ["(042) 35761234"]


def test_an_urdu_name_with_its_joiner_is_taken(fdb, clock):
    """The zero-width non-joiner is how Urdu is typed: a name may hold it (the direction controls are what is refused); an email never holds one."""
    assert _send({**AYESHA, "name": "\u0639\u0627\u0626\u0634\u06c1\u200c\u0631\u062d\u06cc\u0645"}).status_code == 202
    assert _send({**AYESHA, "email": "ayesha\u200c@rahimtextiles.pk"}).status_code == 422


def test_a_form_missing_several_fields_is_told_each_in_one_sentence(fdb, clock):
    """Two capitalised clauses spliced with a semicolon were not one sentence, and the topic's refusal waited until the fields were right, so a person
    could be refused three times in turn."""
    r = _send({})
    assert r.status_code == 422 and r.json()["detail"] == (
        "Choose what this is about: access for your company, another person in your company, help with your account, or something else; "
        "give your full name; give a work email, such as name@company.com"), r.text
    r = _send({"topic": "access", "name": "Ayesha Rahim", "email": "ayesha"})
    assert r.json()["detail"] == "Give a work email, such as name@company.com", r.text
    # a clause that starts with an abbreviation or a name keeps its capital
    assert schemas._clause("DISCO must be one of the list") == "DISCO must be one of the list"
    assert schemas._clause("MMBtu must be a number") == "MMBtu must be a number"
    assert schemas._clause("Punjab's rate is missing") == "Punjab's rate is missing"
    assert schemas._clause("A request's state is new") == "a request's state is new"
    assert schemas._clause("Unknown field 'role'") == "unknown field 'role'"


def test_every_429_of_the_send_says_when_it_works_again(fdb, clock, monkeypatch):
    """The contract types `retry_at` on this path's 429; the API's own limits (per address, and per address without a session) answered it with no
    `retry_at`, so the page's "Try again after 14:01" read undefined. Every limit now says it on this path; another path's 429 is as before."""
    t = [1000.0]
    monkeypatch.setattr(auth.limiter(), "clock", lambda: t[0])
    for kind in ("public", "address"):
        auth.limiter().reset()
        with monkeypatch.context() as m:
            m.setitem(auth.RATE_LIMITS, kind, 2)
            for i in range(2):
                assert _send({**AYESHA, "email": f"{kind}{i}@rahimtextiles.pk"}).status_code == 202
            for path in (URL, "/api/v1" + URL[4:]):
                r = _client().post(path, json=AYESHA)
                assert r.status_code == 429 and r.json()["code"] == "too_many_requests", (kind, r.text)
                wait = int(r.headers["Retry-After"])
                at = datetime.fromisoformat(r.json()["retry_at"])
                assert at.utcoffset() == timedelta(hours=5) and at.second == 0 and at >= datetime.fromtimestamp(clock.now + wait, PKT), (kind, r.text)
            if kind == "address":
                other = _client().get("/api/auth/state")
                assert other.status_code == 429 and "retry_at" not in other.json(), other.text


def test_the_hourly_count_is_kept_for_its_hour_however_full_the_limiter(fdb, clock, monkeypatch):
    """The API's one limiter pruned every key by the caller's window once it held more than MAX_KEYS: the next minute's check forgot an address's
    requests for access after a minute, not an hour, and the address could send five more."""
    monkeypatch.setattr(access_requests, "RATE_LIMIT", SHIPPED_RATE)
    monkeypatch.setattr(Limiter, "MAX_KEYS", 50)
    t = [1000.0]
    monkeypatch.setattr(auth.limiter(), "clock", lambda: t[0])
    for i in range(access_requests.RATE_LIMIT):
        assert _send({**AYESHA, "email": f"p{i}@rahimtextiles.pk"}).status_code == 202
    assert _send(AYESHA).status_code == 429
    for i in range(60):                                   # a busy minute: many keys of the minute's window
        auth.limiter().hit(("api", "address", f"10.1.0.{i}"), 1000, 60)
    t[0] += 61
    assert auth.rate_wait("address", "10.2.0.1") == 0     # a minute's check over a full limiter prunes it
    assert _send(AYESHA).status_code == 429
    assert len(auth.limiter()._hits) < 10                 # the minute's keys went, the hour's stayed
    # and the Limiter alone: each key by its own window
    now = [0.0]
    lim = Limiter(clock=lambda: now[0])
    for _ in range(3):
        assert lim.hit(("hour",), 3, 3600) == 0
    for i in range(60):
        lim.hit(("minute", i), 10, 60)
    now[0] = 120.0
    lim.hit(("minute", "late"), 10, 60)
    assert lim.hit(("hour",), 3, 3600) > 0 and set(lim._hits) == {("hour",), ("minute", "late")}
    lim.reset()
    assert lim._hits == {} and lim._windows == {}


def test_an_ipv6_sender_is_counted_by_its_64(fdb, clock, monkeypatch):
    """One connection holds a whole IPv6 /64: counted by the address alone, a sender took a new address for each send."""
    monkeypatch.setattr(access_requests, "RATE_LIMIT", 2)
    assert _send(AYESHA, address="2001:db8:1:2::a").status_code == 202
    assert _send(AYESHA, address="2001:db8:1:2:ffff:ffff:ffff:fffe").status_code == 202
    assert _send(AYESHA, address="2001:db8:1:2::c").status_code == 429
    assert _send(AYESHA, address="2001:db8:1:3::a").status_code == 202
    assert access_requests.counted_as("2001:db8:1:2::a") == "2001:db8:1:2::/64"
    assert access_requests.counted_as("::ffff:10.0.0.7") == "10.0.0.7" == access_requests.counted_as("10.0.0.7")
    assert access_requests.counted_as("forwarded") == "forwarded" and access_requests.counted_as("") == "unknown"

