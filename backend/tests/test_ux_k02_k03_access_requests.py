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
from contextlib import closing
from datetime import datetime, timedelta, timezone

import identities
import pytest
from test_founder import _audit, _client, _sentence, clock, fdb  # noqa: F401  (fixtures)

from gridlytics import access_requests, api, auth
from gridlytics import platform as platformdb

PKT = timezone(timedelta(hours=5))
URL = "/api/public/access-requests"
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
