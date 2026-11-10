"""Stage B6 of the sign-in and split: the founder console's backend (decisions-v2 section 4; design E as the decisions keep it; the addendum's items 13
and 14; the `/api/founder/*` contract of docs/split/F5.md).

The founder signs in with a username, a password and a six-digit code, in two steps with a challenge between them, and gets an 8-hour session token in the
format of Boxlytics' `founder-auth.ts` (`boxlytics@c429bef`), sent in `x-founder-session`. The console makes companies (with their owner, R-405), renames
them, gives them seats and switches Gridlytics on or off for them; makes accounts with a first password, resets it, disables them and adds them to or
removes them from a company; makes and lists each company's backups; and writes every action to `founder_audit` in platform.db.

Every test has its own database and so its own registry (platform.db beside it). The secrets are made up. The supabase mode's project is a stand-in that
keeps its rows in memory: nothing reaches any host.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import re
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import identities
from gridlytics import api, auth, backups, founder, identity, mfa, routes_founder, store, store_founder, store_members
from gridlytics import platform as platformdb

#: Made-up secrets of these tests, never a real one
PASSWORD = "a made-up founder password"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
SESSION_SECRET = "made-up-founder-session-key-of-the-tests-01"
#: A token made by Boxlytics' own createSessionToken (a copy of supabase/functions/_shared/founder-auth.ts at c429bef, run under Node 22 with Date.now()
#: fixed at 1761400000 s and ADMIN_SESSION_SECRET set to SESSION_SECRET with spaces before it and a line feed after it, which its trim() removes)
BOXLYTICS_NOW = 1761400000
BOXLYTICS_TOKEN = ("eyJzdWIiOiJmb3VuZGVyIiwiaWF0IjoxNzYxNDAwMDAwLCJleHAiOjE3NjE0Mjg4MDB9"
                   ".ke8ohkRYfAArrjH98bRfy3v3bwK-u1oNGrKoSLf2MCk")
#: The same run's sha256Hex of PASSWORD: Boxlytics' form of the password hash, which the console still takes (with a warning)
BOXLYTICS_SHA256 = "f1d6c2ebf59b0ebad6d6f00328efb60873cff3bd2eba5c1bdd9f1d981dfb19b6"
_HASH = founder.hash_password(PASSWORD)
OWNER = {"email": "saad@meow.test", "name": "Saad Khan", "password": "owner first password 1"}


class Clock:
    """One clock for the token, the code's time steps and the registry's record, moved by the test."""

    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def step(self, seconds: float = mfa.PERIOD) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock(1_800_000_000.0)
    monkeypatch.setattr(founder, "_clock", c)
    monkeypatch.setattr(mfa, "clock", c)
    monkeypatch.setattr(platformdb, "_clock", c)
    return c


@pytest.fixture
def fdb(tmp_path, monkeypatch, clock):
    """The test's own database and registry, and the console set up with made-up secrets."""
    monkeypatch.delenv("GRIDLYTICS_DATA_DIR", raising=False)
    monkeypatch.delenv("GRIDLYTICS_TRUST_PROXY", raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "founder.db")
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_USERNAME", "founder")
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_PASSWORD_HASH", _HASH)
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_TOTP_SECRET", TOTP_SECRET)
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", SESSION_SECRET)
    auth.limiter().reset()
    return tmp_path


def _code() -> str:
    return mfa.totp(mfa.secret_bytes(TOTP_SECRET))


def _client(address: str = "testclient") -> TestClient:
    return TestClient(api.app, raise_server_exceptions=False, client=(address, 50000))


def _session() -> dict:
    token, _exp = founder.new_token()
    return {"x-founder-session": token}


def _console(address: str = "testclient") -> TestClient:
    c = _client(address)
    c.headers.update(_session())
    return c


def _ok(r, status: int = 200):
    assert r.status_code == status, (r.request.method, str(r.request.url), r.status_code, r.text[:400])
    return r.json()


def _sentence(r) -> bool:
    detail = r.json().get("detail")
    return isinstance(detail, str) and len(detail.split()) >= 3


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _boxlytics_token(secret: str, now: int, payload: dict | None = None) -> str:
    """createSessionToken as founder-auth.ts writes it, line by line: JSON.stringify of {sub, iat, exp} (no spaces), base64url without padding, a dot, and
    base64url(HMAC-SHA256(payloadB64, secret.trim()))."""
    body = json.dumps(payload if payload is not None else {"sub": "founder", "iat": now, "exp": now + 60 * 60 * 8}, separators=(",", ":"))
    payload_b64 = _b64(body.encode("utf-8"))
    return f"{payload_b64}.{_b64(hmac.new(secret.strip().encode(), payload_b64.encode(), hashlib.sha256).digest())}"


def _audit() -> list[dict]:
    return platformdb.list_founder_audit(1000)


def _founder_routes() -> list[tuple[str, str]]:
    return sorted((m, r.path) for r in routes_founder.router.routes if isinstance(r, APIRoute) for m in r.methods - {"HEAD", "OPTIONS"})


def _url(path: str) -> str:
    return path.replace("{company_id}", str(uuid.uuid4())).replace("{user_id}", str(uuid.uuid4()))


# ================================================================== the session token: Boxlytics' format (founder-auth.ts)

def test_a_token_made_exactly_as_createSessionToken_makes_it_is_accepted(fdb, clock, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", f"  {SESSION_SECRET}\n")
    clock.now = BOXLYTICS_NOW + 60
    assert founder.verify(BOXLYTICS_TOKEN) == BOXLYTICS_NOW + 8 * 3600
    # and this console makes the very same token at the same moment: one format, both ways
    clock.now = BOXLYTICS_NOW
    assert founder.new_token() == (BOXLYTICS_TOKEN, BOXLYTICS_NOW + 8 * 3600)
    clock.now = BOXLYTICS_NOW + 60
    assert _ok(_client().get("/api/founder/session", headers={"x-founder-session": BOXLYTICS_TOKEN})) == {"authenticated": True,
                                                                                                          "expires_at": BOXLYTICS_NOW + 8 * 3600}
    # the test's own port of createSessionToken, at another moment
    clock.now = 1_900_000_000
    mine = _boxlytics_token(SESSION_SECRET, 1_900_000_000 - 5)
    assert founder.verify(mine) == 1_900_000_000 - 5 + 8 * 3600


def test_a_wrong_secret_a_forged_payload_or_an_expired_token_gives_401(fdb, clock, monkeypatch):
    c = _client()
    now = int(clock.now)

    def refused(token, code):
        r = c.get("/api/founder/session", headers={} if token is None else {"x-founder-session": token})
        assert r.status_code == 401 and r.json()["code"] == code and _sentence(r), (token, r.text)

    refused(None, "founder_session_required")
    refused("", "founder_session_required")
    refused(_boxlytics_token("another made-up secret of 32 characters", now), "founder_session_invalid")      # a wrong secret
    good = _boxlytics_token(SESSION_SECRET, now)
    payload, signature = good.split(".")
    refused(f"{_b64(json.dumps({'sub': 'founder', 'iat': now, 'exp': now + 10 ** 9}).encode())}.{signature}", "founder_session_invalid")  # payload changed
    refused(f"{good}.extra", "founder_session_invalid")                     # Boxlytics' split ignores a third part; this console takes the format only
    refused(signature, "founder_session_invalid")
    refused("not a token at all", "founder_session_invalid")
    for forged in ({"sub": "admin", "iat": now, "exp": now + 60}, {"sub": "founder", "iat": "now", "exp": now + 60}, {"sub": "founder", "iat": now},
                   {"sub": "founder", "iat": True, "exp": now + 60}, [1, 2]):
        refused(_boxlytics_token(SESSION_SECRET, now, forged), "founder_session_invalid")
    nan = _b64(b'{"sub":"founder","iat":NaN,"exp":Infinity}')               # JSON.parse refuses these; Python reads them, so they are checked as finite
    refused(f"{nan}.{_b64(hmac.new(SESSION_SECRET.encode(), nan.encode(), hashlib.sha256).digest())}", "founder_session_invalid")
    huge = _b64(('{"sub":"founder","iat":1,"exp":1' + "0" * 400 + "}").encode())   # Infinity to JSON.parse; too large for a float here (was a 500)
    refused(f"{huge}.{_b64(hmac.new(SESSION_SECRET.encode(), huge.encode(), hashlib.sha256).digest())}", "founder_session_invalid")
    refused(_boxlytics_token(SESSION_SECRET, now - 8 * 3600), "founder_session_expired")                    # exp == now: over (exp > now holds it)
    assert _ok(c.get("/api/founder/session", headers={"x-founder-session": _boxlytics_token(SESSION_SECRET, now - 8 * 3600 + 1)}))["authenticated"]
    # the secret rotated: every session made before is refused at once
    token, _ = founder.new_token()
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", "a rotated made-up session secret of the tests")
    refused(token, "founder_session_invalid")


def test_every_founder_route_but_the_login_needs_the_session_whatever_else_the_request_carries(fdb, clock):
    """The guard lets /api/founder/* through with no Bearer and no company (the founder routes check x-founder-session themselves): never the guard's
    sign_in_required, and a member's token or a company header does not stand in for the founder's session."""
    member = identities.token("member@acme.test", "admin", company_role="owner")
    routes = _founder_routes()
    assert {("POST", "/api/founder/login"), ("POST", "/api/founder/login/code"), ("GET", "/api/founder/session"), ("POST", "/api/founder/logout"),
            ("GET", "/api/founder/overview"),
            ("GET", "/api/founder/companies"), ("POST", "/api/founder/companies"), ("PATCH", "/api/founder/companies/{company_id}"),
            ("POST", "/api/founder/companies/{company_id}/access"), ("GET", "/api/founder/companies/{company_id}/members"),
            ("POST", "/api/founder/companies/{company_id}/archive"), ("POST", "/api/founder/users/{user_id}/archive"),
            ("GET", "/api/founder/companies/{company_id}/backups"), ("POST", "/api/founder/companies/{company_id}/backups"),
            ("GET", "/api/founder/users"), ("POST", "/api/founder/users"), ("POST", "/api/founder/users/{user_id}/password"),
            ("PATCH", "/api/founder/users/{user_id}"), ("POST", "/api/founder/users/{user_id}/memberships"),
            ("PATCH", "/api/founder/users/{user_id}/memberships/{company_id}"), ("DELETE", "/api/founder/users/{user_id}/memberships/{company_id}"),
            ("GET", "/api/founder/audit")} == set(routes)
    for method, path in routes:
        if path.startswith("/api/founder/login"):
            continue
        for prefix in ("/api", "/api/v1"):
            for headers in ({}, identities.headers(member, identities.company()), {"x-founder-session": "x.y"}):
                r = _client().request(method, prefix + _url(path)[4:], headers=headers, json={} if method != "GET" else None)
                want = "founder_session_invalid" if "x-founder-session" in headers else "founder_session_required"
                assert r.status_code == 401 and r.json()["code"] == want, (method, path, headers.keys(), r.status_code, r.text[:200])
                assert "X-Gridlytics-Role" not in r.headers
    # the session is checked before a field of the body is read: a body of the wrong shape without it is the session's 401 (only one that is not JSON at
    # all is refused first, as on every route)
    assert _client().post("/api/founder/companies", json=[1]).json()["code"] == "founder_session_required"
    r = _client().post("/api/founder/companies", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_body" and "founder" not in r.text
    # The login routes are public to the guard too: a wrong password is the founder's own 401, never "Sign in first"
    r = _client().post("/api/founder/login", json={"username": "founder", "password": "wrong password here"}, headers=identities.headers(member))
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


def test_the_console_answers_503_until_it_is_set_up(fdb, monkeypatch):
    for name in ("GRIDLYTICS_FOUNDER_USERNAME", "GRIDLYTICS_FOUNDER_PASSWORD_HASH", "GRIDLYTICS_FOUNDER_TOTP_SECRET", "GRIDLYTICS_FOUNDER_SESSION_SECRET"):
        with monkeypatch.context() as m:
            m.setenv(name, "  ")
            for method, path in _founder_routes():
                r = _client().request(method, _url(path), json={"username": "founder", "password": PASSWORD} if method != "GET" else None,
                                      headers={"x-founder-session": _boxlytics_token(SESSION_SECRET, int(time.time()))})
                assert r.status_code == 503 and r.json()["code"] == "unavailable" and name in r.json()["detail"], (name, method, path, r.text)
                assert TOTP_SECRET not in r.text and SESSION_SECRET not in r.text
    # set, but in a form that cannot work: every sign-in would be refused (and the founder locked out) without saying why; the value is never said
    for name, value in (("GRIDLYTICS_FOUNDER_TOTP_SECRET", "not-base32-1890"), ("GRIDLYTICS_FOUNDER_PASSWORD_HASH", "a plain password, not a hash"),
                        ("GRIDLYTICS_FOUNDER_PASSWORD_HASH", "pbkdf2_sha256$0$00$" + "0" * 64)):
        with monkeypatch.context() as m:
            m.setenv(name, value)
            for path in ("/api/founder/login", "/api/founder/session"):
                r = _client().request("POST" if path.endswith("login") else "GET", path, json={"username": "founder", "password": PASSWORD}
                                      if path.endswith("login") else None, headers={"x-founder-session": _boxlytics_token(SESSION_SECRET, int(time.time()))})
                assert r.status_code == 503 and r.json()["code"] == "unavailable" and name in r.json()["detail"] and value not in r.text, (name, r.text)
    assert founder.malformed() == [] and _client().post("/api/founder/login", json={"username": "founder", "password": PASSWORD}).status_code == 200


# ================================================================== the login: two steps, the password sent once

def _login(c: TestClient, username: str = "founder", password: str = PASSWORD):
    return c.post("/api/founder/login", json={"username": username, "password": password})


def _sign_in(c: TestClient, clock: Clock) -> dict:
    challenge = _ok(_login(c))
    out = _ok(c.post("/api/founder/login/code", json={"challenge": challenge["challenge"], "code": _code()}))
    clock.step()                     # the next sign-in's code is of a later step (a code is never taken twice)
    return out


def test_the_login_takes_the_password_then_a_code_and_gives_an_8_hour_session(fdb, clock):
    c = _client()
    first = _ok(_login(c))
    assert set(first) == {"challenge", "expires_in", "tries"} and first["expires_in"] == 300 and first["tries"] == 5
    assert re.fullmatch(auth.LINK_TOKEN, first["challenge"])
    # only the challenge's hash is kept
    with closing(sqlite3.connect(platformdb.db_path())) as con:
        kept = " ".join(r[0] for r in con.execute("SELECT detail FROM account_events"))
    assert first["challenge"] not in kept and auth.token_hash(first["challenge"]) in kept
    # the code may come in Arabic-Indic digits, with a space
    code = _code().translate({ord(str(n)): chr(0x0660 + n) for n in range(10)})
    session = _ok(c.post("/api/founder/login/code", json={"challenge": first["challenge"], "code": f"{code[:3]} {code[3:]}"}))
    assert set(session) == {"token", "expires_at"} and session["expires_at"] == int(clock.now) + 8 * 3600
    assert founder.verify(session["token"]) == session["expires_at"]
    assert _ok(c.get("/api/founder/session", headers={"x-founder-session": session["token"]})) == {"authenticated": True,
                                                                                                    "expires_at": session["expires_at"]}
    # the challenge is good once
    r = c.post("/api/founder/login/code", json={"challenge": first["challenge"], "code": _code()})
    assert r.status_code == 401 and r.json()["code"] == "challenge_expired"
    # 8 hours later the session is over
    clock.now += 8 * 3600
    r = c.get("/api/founder/session", headers={"x-founder-session": session["token"]})
    assert r.status_code == 401 and r.json()["code"] == "founder_session_expired"
    actions = [e["action"] for e in _audit()]
    assert actions == ["signed in"] and PASSWORD not in json.dumps(_audit())


def test_a_wrong_username_or_password_is_one_answer_and_the_hash_is_checked_either_way(fdb, monkeypatch):
    checked, compared = [], []
    real_check, real_compare = founder.check_password, founder.hmac.compare_digest
    monkeypatch.setattr(founder, "check_password", lambda pw, stored: checked.append(pw) or real_check(pw, stored))
    monkeypatch.setattr(founder.hmac, "compare_digest", lambda a, b: compared.append((a, b)) or real_compare(a, b))
    answers = []
    for username, password in (("founder", "a wrong password 12"), ("someone", PASSWORD), ("Founder", PASSWORD), ("", "")):
        r = _login(_client(), username, password)
        assert r.status_code == 401 and r.json()["code"] == "invalid_credentials", r.text
        answers.append(r.json()["detail"])
    assert len(set(answers)) == 1 and "username" in answers[0] and "password" in answers[0]
    assert checked == ["a wrong password 12", PASSWORD, PASSWORD, ""]                  # the password's hash checked for a wrong username too
    assert (b"someone", b"founder") in compared                                         # the username compared in constant time
    assert [e["action"] for e in _audit()] == ["sign-in refused"] * 4
    assert all("someone" not in json.dumps(e) and PASSWORD not in json.dumps(e) for e in _audit())
    for body in ({"username": "founder"}, {"username": 5, "password": PASSWORD}, {"username": "founder", "password": PASSWORD, "code": "1"}):
        r = _client().post("/api/founder/login", json=body)
        assert r.status_code == 422 and _sentence(r), r.text


def test_a_wrong_code_keeps_the_challenge_while_it_has_tries_and_a_code_is_never_taken_twice(fdb, clock):
    c = _client()
    challenge = _ok(_login(c))["challenge"]
    right = _code()
    wrong = f"{(int(right) + 1) % 1_000_000:06d}"
    r = c.post("/api/founder/login/code", json={"challenge": challenge, "code": wrong})
    assert r.status_code == 401 and r.json()["code"] == "invalid_code" and "4 tries left" in r.json()["detail"]
    session = _ok(c.post("/api/founder/login/code", json={"challenge": challenge, "code": right}))
    assert founder.verify(session["token"])
    # the same code again, with a new challenge, in the same 30 seconds: refused (replay); the step it was taken at is kept in platform.db
    again = _ok(_login(c))["challenge"]
    r = c.post("/api/founder/login/code", json={"challenge": again, "code": right})
    assert r.status_code == 401 and r.json()["code"] == "invalid_code"
    assert store_founder.last_code_step() == mfa.step()
    # a code of the step before (the drift Boxlytics takes) is older than the one taken: refused too
    clock.step()
    r = c.post("/api/founder/login/code", json={"challenge": again, "code": mfa.totp(mfa.secret_bytes(TOTP_SECRET), clock.now - mfa.PERIOD)})
    assert r.status_code == 401 and r.json()["code"] == "invalid_code"
    assert _ok(c.post("/api/founder/login/code", json={"challenge": again, "code": _code()}))["token"]
    assert [e["action"] for e in _audit()][::-1] == ["code refused", "signed in", "code refused", "code refused", "signed in"]


def test_a_challenge_lasts_five_minutes_and_five_tries_and_an_unknown_one_is_refused(fdb, clock):
    challenge, _ = founder.login("founder", PASSWORD, "10.0.0.1")["challenge"], None
    for i in range(5):                                       # five wrong codes, each from another address, so no address's lock is met
        with pytest.raises(founder.Refused) as e:
            founder.code(challenge, "000000" if _code() != "000000" else "111111", f"10.0.1.{i}")
        assert e.value.code == "invalid_code"
    with pytest.raises(founder.Refused) as e:
        founder.code(challenge, _code(), "10.0.2.1")         # the right code, but the challenge's tries are spent
    assert e.value.status == 401 and e.value.code == "challenge_expired"
    late = founder.login("founder", PASSWORD, "10.0.0.2")["challenge"]
    clock.step(300)
    with pytest.raises(founder.Refused) as e:
        founder.code(late, _code(), "10.0.0.2")
    assert e.value.code == "challenge_expired"
    for unknown in ("x" * 43, "not one", "", 5, None):
        r = _client().post("/api/founder/login/code", json={"challenge": unknown, "code": _code()})
        assert r.status_code in (401, 422) and r.json()["code"] in ("challenge_expired", "invalid"), r.text


def test_wrong_passwords_lock_the_address_and_only_a_full_sign_in_clears_the_count(fdb, clock):
    c = _client()
    for _ in range(4):
        assert _login(c, password="a wrong password 12").status_code == 401
    _ok(_login(c))                                           # the right password: a challenge, but the count is not cleared until the code is taken
    assert _login(c, password="a wrong password 12").status_code == 401
    r = _login(c)                                            # the fifth refusal was the last: the right password is now refused too
    assert r.status_code == 429 and r.json()["code"] == "too_many_requests" and 0 < int(r.headers["Retry-After"]) <= 900
    assert "15 minutes" in r.json()["detail"]
    assert _login(_client("10.9.9.9")).status_code == 200    # another address is not stopped
    clock.step(901)
    _sign_in(c, clock)                                       # the wait is over; a full sign-in clears the count
    for _ in range(4):
        assert _login(c, password="a wrong password 12").status_code == 401
    assert _login(c).status_code == 200


def test_wrong_codes_stop_the_code_step_and_the_password_step_says_so_at_once(fdb, clock):
    """R-271: five wrong codes from one address stop it, and the next try (even with the right code, or the password again) is told to wait, never "sign in
    again" and then refused; twenty wrong codes from every address together stop the code step wherever it is tried from."""
    c = _client()
    challenge = _ok(_login(c))["challenge"]
    wrong = "000000" if _code() != "000000" else "111111"
    answers = [c.post("/api/founder/login/code", json={"challenge": challenge, "code": wrong}) for _ in range(5)]
    assert [r.status_code for r in answers] == [401, 401, 401, 401, 429]
    assert "wrong codes" in answers[-1].json()["detail"] and int(answers[-1].headers["Retry-After"]) > 0
    r = c.post("/api/founder/login/code", json={"challenge": challenge, "code": _code()})
    assert r.status_code == 429 and r.json()["code"] == "too_many_requests", r.text
    r = c.post("/api/founder/login/code", json={"challenge": "x" * 43, "code": _code()})
    assert r.status_code == 429                              # the lock is read before the challenge (R-271)
    r = _login(c)
    assert r.status_code == 429 and "wrong codes" in r.json()["detail"]
    clock.step(901)
    # every address together: twenty wrong codes, four from each of five addresses, stop the code step for the founder wherever it comes from
    for i in range(5):
        here = f"10.1.0.{i}"
        ch = founder.login("founder", PASSWORD, here)["challenge"]
        for _ in range(4):
            with pytest.raises(founder.Refused) as e:
                founder.code(ch, wrong, here)
        assert e.value.status == (429 if i == 4 else 401), (i, e.value.code)
    with pytest.raises(founder.Refused) as e:
        founder.login("founder", PASSWORD, "10.2.0.1")
    assert e.value.status == 429 and e.value.retry_after > 0
    assert [e["action"] for e in _audit()].count("code step stopped") == 1


def test_the_founders_paths_count_in_the_public_rate_limit_of_their_address(fdb, monkeypatch):
    """Besides the lockout, the guard's limit of requests without a session per address (master PS-07) counts the founder's paths, sign-in included."""
    monkeypatch.setitem(auth.RATE_LIMITS, "public", 3)
    c = _client("10.3.3.3")
    answers = [c.post("/api/founder/login", json={"username": "founder", "password": "a wrong password 12"}) for _ in range(2)]
    answers += [c.get("/api/founder/session"), c.get("/api/founder/session", headers=_session())]
    assert [r.status_code for r in answers] == [401, 401, 401, 429] and int(answers[-1].headers["Retry-After"]) > 0
    assert _client("10.3.3.4").get("/api/founder/session", headers=_session()).status_code == 200


# ================================================================== the password hash: PBKDF2, and Boxlytics' sha256 with a warning

def test_the_hash_is_pbkdf2_and_boxlytics_sha256_is_taken_with_a_warning(fdb, monkeypatch, caplog):
    stored = founder.hash_password(PASSWORD)
    assert stored.startswith(f"pbkdf2_sha256${auth.ITERATIONS}$") and founder.check_password(PASSWORD, stored) and not founder.check_password("x" + PASSWORD, stored)
    assert hashlib.sha256(PASSWORD.encode()).hexdigest() == BOXLYTICS_SHA256
    monkeypatch.setattr(founder, "_warned", False)
    with caplog.at_level(logging.WARNING, logger="gridlytics"):
        assert founder.check_password(PASSWORD, BOXLYTICS_SHA256) and founder.check_password(PASSWORD, BOXLYTICS_SHA256.upper())
        assert not founder.check_password("x" + PASSWORD, BOXLYTICS_SHA256)
    assert "sha256" in caplog.text and "python -m gridlytics.founder hash" in caplog.text and PASSWORD not in caplog.text
    for bad in ("", "plain text", "pbkdf2_sha256$x$y$z", "a" * 63):
        assert not founder.check_password(PASSWORD, bad)
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_PASSWORD_HASH", BOXLYTICS_SHA256)
    assert _login(_client()).status_code == 200


def test_the_command_line_makes_the_hash_the_code_secret_and_the_current_code(fdb, clock, monkeypatch):
    out = io.StringIO()
    assert founder.main(["hash"], stdin=io.StringIO(f"{PASSWORD}\n"), stdout=out) == 0
    made = out.getvalue().strip()
    assert founder.check_password(PASSWORD, made) and PASSWORD not in out.getvalue()
    err = io.StringIO()
    assert founder.main(["hash"], stdin=io.StringIO("short\n"), stdout=io.StringIO(), stderr=err) == 1 and "10 characters" in err.getvalue()
    out = io.StringIO()
    assert founder.main(["totp-secret"], stdout=out) == 0
    secret, link = out.getvalue().split()[:2]
    assert len(mfa.secret_bytes(secret)) == mfa.SECRET_BYTES and link.startswith("otpauth://totp/") and f"secret={secret}" in link
    out = io.StringIO()
    assert founder.main(["code"], stdout=out) == 0 and out.getvalue().strip() == _code()
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_TOTP_SECRET", "")
    err = io.StringIO()
    assert founder.main(["code"], stdout=io.StringIO(), stderr=err) == 1 and "GRIDLYTICS_FOUNDER_TOTP_SECRET" in err.getvalue()
    assert founder.main(["nonsense"], stdout=io.StringIO(), stderr=io.StringIO()) == 2


# ================================================================== companies (R-405: made with their owner)

def _company(c: TestClient, name: str = "Boxlytics Internal", owner=None, seats: int = 5, enabled: bool = True) -> dict:
    return _ok(c.post("/api/founder/companies", json={"name": name, "seats": seats, "enabled": enabled, "owner": owner or dict(OWNER)}), 201)


def test_a_company_is_made_with_its_owner_together_or_not_at_all(fdb):
    c = _console()
    for body, words in (({"name": "Acme", "seats": 5, "enabled": True}, "owner"),
                        ({"name": "Acme", "seats": 5, "enabled": True, "owner": {"email": "o@acme.test", "name": "", "password": "first password 1"}}, "owner's name"),
                        ({"name": "Acme", "seats": 5, "enabled": True, "owner": {"email": "nobody", "name": "Olga", "password": "first password 1"}}, "owner's email"),
                        ({"name": "Acme", "seats": 5, "enabled": True, "owner": {"email": "o@acme.test", "name": "Olga", "password": "short"}}, "owner's first password"),
                        ({"name": "", "seats": 5, "enabled": True, "owner": dict(OWNER)}, "company"),
                        ({"name": "Acme", "seats": -1, "enabled": True, "owner": dict(OWNER)}, "eats"),
                        ({"name": "Acme", "seats": 5, "enabled": "yes", "owner": dict(OWNER)}, "on"),
                        ({"name": "Acme", "seats": 5, "enabled": True, "owner": {"user_id": "x", "email": "o@acme.test"}}, "owner"),
                        ({"name": "Acme", "seats": 5, "enabled": True, "owner": dict(OWNER), "db_file": "/etc/passwd"}, "db_file")):
        r = c.post("/api/founder/companies", json=body)
        assert r.status_code == 422 and words in r.json()["detail"], (body, r.text)
    assert platformdb.list_companies() == [] and platformdb.count_accounts() == 0
    made = _company(c)
    assert set(made) == {"company", "owner", "membership"}
    company, owner = made["company"], made["owner"]
    assert company["name"] == "Boxlytics Internal" and company["enabled"] is True and company["seats"] == 5 and "db_file" not in company
    assert company["owner"] == {"id": owner["id"], "email": OWNER["email"], "name": OWNER["name"], "disabled": False} and company["members"] == 1
    assert made["membership"]["company_role"] == "owner" and made["membership"]["status"] == "active"
    assert platformdb.check_sign_in(OWNER["email"], OWNER["password"])["id"] == owner["id"]
    assert platformdb.company_file(company["id"]) == platformdb.companies_dir() / f"{company['id']}.db"
    assert not platformdb.company_file(company["id"]).exists()               # the file is made at the owner's first request, not by the founder's reads
    # an email that has an account is refused, never reused in silence; an existing account is chosen by its id
    r = c.post("/api/founder/companies", json={"name": "Second", "seats": 5, "enabled": True, "owner": dict(OWNER)})
    assert r.status_code == 409 and "choose it as the owner" in r.json()["detail"]
    r = c.post("/api/founder/companies", json={"name": "Second", "seats": 5, "enabled": True, "owner": {"user_id": str(uuid.uuid4())}})
    assert r.status_code == 404
    second = _company(c, "Second", {"user_id": owner["id"]})
    assert second["owner"]["id"] == owner["id"] and second["company"]["owner"]["id"] == owner["id"]
    r = c.post("/api/founder/companies", json={"name": "boxlytics internal", "seats": 5, "enabled": True, "owner": {"user_id": owner["id"]}})
    assert r.status_code == 409 and "name" in r.json()["detail"]                 # one company per name, whatever its case (as the project has it)
    assert [e["action"] for e in _audit()][::-1] == ["made a company", "made a company"]
    assert OWNER["password"] not in json.dumps(_audit())


def test_a_failure_while_the_company_is_made_leaves_nothing(fdb, monkeypatch):
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error (made up)")
    monkeypatch.setattr(store_founder, "_insert_membership", broken)
    r = _console().post("/api/founder/companies", json={"name": "Acme", "seats": 5, "enabled": True, "owner": dict(OWNER)})
    assert r.status_code == 500
    assert platformdb.list_companies() == [] and platformdb.count_accounts() == 0 and _audit() == []


def test_a_company_is_renamed_given_seats_and_switched_off_and_its_members_are_refused_at_once(fdb):
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    token = identity.issue_local(owner["id"], owner["email"], owner["name"], 60)
    member = _client()
    member.headers.update(identities.headers(token))
    _ok(member.get("/api/sites"))                                              # the owner's first request makes the company's file
    assert platformdb.company_file(cid).exists()
    renamed = _ok(c.patch(f"/api/founder/companies/{cid}", json={"name": "Boxlytics", "seats": 2}))["company"]
    assert renamed["name"] == "Boxlytics" and renamed["seats"] == 2 and renamed["seats_taken"] == 1
    for body in ({}, {"name": " "}, {"seats": 1.5}, {"seats": True}, {"enabled": False}):
        r = c.patch(f"/api/founder/companies/{cid}", json=body)
        assert r.status_code == 422 and _sentence(r), (body, r.text)
    assert c.patch(f"/api/founder/companies/{uuid.uuid4()}", json={"name": "X"}).status_code == 404
    off = _ok(c.post(f"/api/founder/companies/{cid}/access", json={"enabled": False, "reason": "Unpaid since October"}))["company"]
    assert off["enabled"] is False and off["disabled_reason"] == "Unpaid since October"
    r = member.get("/api/sites")
    assert r.status_code == 403 and r.json()["code"] == "company_disabled", r.text
    on = _ok(c.post(f"/api/founder/companies/{cid}/access", json={"enabled": True}))["company"]
    assert on["enabled"] is True and on["disabled_reason"] == ""
    _ok(member.get("/api/sites"))
    for body in ({}, {"enabled": "no"}, {"enabled": False, "reason": "x" * 201}, {"enabled": True, "note": "x"}):
        assert c.post(f"/api/founder/companies/{cid}/access", json=body).status_code == 422, body
    actions = [(e["action"], e["target"]) for e in _audit()][::-1]
    assert actions == [("made a company", "Boxlytics Internal"), ("changed a company", "Boxlytics"), ("switched Gridlytics off", "Boxlytics"),
                       ("switched Gridlytics on", "Boxlytics")]
    assert _audit()[2]["detail"] == {"name": {"from": "Boxlytics Internal", "to": "Boxlytics"}, "seats": {"from": 5, "to": 2}}


def test_the_companies_list_carries_the_files_figures_and_the_plan(fdb):
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    quiet = _company(c, "Development", {"email": "dev@localhost", "name": "Local Tester", "password": "dev first password 1"}, enabled=False)
    token = identity.issue_local(owner["id"], owner["email"], owner["name"], 60)
    _ok(_client().get("/api/sites", headers=identities.headers(token)))
    with store.use_company(platformdb.company_file(cid)):
        store.set_overrides({"plan": {"id": "pro", "name": "Installer Pro", "price": 100, "trial_days": 14, "started": datetime.now().date().isoformat()}})
    rows = {x["name"]: x for x in _ok(c.get("/api/founder/companies"))["companies"]}
    assert list(rows) == ["Boxlytics Internal", "Development"]
    box = rows["Boxlytics Internal"]
    assert {"id", "name", "enabled", "seats", "disabled_reason", "created", "owner", "members", "seats_taken", "admins", "last_seen", "projects",
            "quotes_issued", "plan"} == set(box)
    assert box["seats_taken"] == 1 and box["admins"] == 1 and box["projects"] == 0 and box["quotes_issued"] == 0 and box["last_seen"]
    assert datetime.fromisoformat(box["last_seen"]).utcoffset() is not None
    assert box["plan"]["state"] == "trial" and box["plan"]["name"] == "Installer Pro" and "On trial" in box["plan"]["sentence"]
    dev = rows["Development"]
    assert dev["seats_taken"] == 0 and dev["admins"] == 0 and dev["last_seen"] is None and dev["plan"]["state"] == "none" and dev["plan"]["name"] is None
    assert not platformdb.company_file(quiet["company"]["id"]).exists()          # the list made no file
    members = _ok(c.get(f"/api/founder/companies/{cid}/members"))["members"]
    assert members == [{"user_id": owner["id"], "email": OWNER["email"], "name": OWNER["name"], "company_role": "owner", "status": "active",
                        "disabled": False, "gridlytics_role": "admin", "seated": True,
                        "last_seen": members[0]["last_seen"]}] and members[0]["last_seen"]
    assert _ok(c.get(f"/api/founder/companies/{quiet['company']['id']}/members"))["members"][0]["gridlytics_role"] is None
    assert c.get(f"/api/founder/companies/{uuid.uuid4()}/members").status_code == 404


def test_a_company_file_that_cannot_be_read_is_said_and_left_out_of_the_sums(fdb):
    c = _console()
    made = _company(c)
    broken = platformdb.company_file(made["company"]["id"])
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"this is not a database" * 100)
    box = _ok(c.get("/api/founder/companies"))["companies"][0]
    assert box["seats_taken"] is None and box["plan"] is None and box["projects"] is None and box["owner"]["email"] == OWNER["email"]
    view = _ok(c.get("/api/founder/overview"))
    assert view["unread"] == [{"company_id": made["company"]["id"], "name": "Boxlytics Internal", "reason": "the file could not be read"}]
    assert view["seats"] == {"given": 5, "taken": 0}


def test_the_overview_adds_up_the_registry_and_the_files_of_the_companies_that_are_on(fdb):
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    _company(c, "Development", {"email": "dev@localhost", "name": "Local Tester", "password": "dev first password 1"}, enabled=False, seats=3)
    sunia = _ok(c.post("/api/founder/users", json={"email": "sunia@meow.test", "name": "Sunia Ahmed", "password": "sunia first password", "company_id": cid}), 201)
    for who in (owner, sunia["user"]):
        _ok(_client().get("/api/sites", headers=identities.headers(identity.issue_local(who["id"], who["email"], who["name"], 60))))
    with store.use_company(platformdb.company_file(cid)):
        store_members.update_member(sunia["user"]["id"], role="operator", role_source="set")
        store.create_project("Rooftop", "", {"a": 1}, {}, by="test")
    view = _ok(c.get("/api/founder/overview"))
    assert view["companies"] == {"total": 2, "on": 1, "off": 1}
    assert view["accounts"] == {"total": 3, "disabled": 0}
    assert view["memberships"] == {"active": 3, "pending": 0, "suspended": 0}
    assert view["seats"] == {"given": 5, "taken": 2}
    assert view["roles"] == {"admin": 1, "operator": 1}
    assert view["activity"]["seen_7"] == 2 and view["activity"]["seen_30"] == 2
    assert set(view["activity"]) == {"seen_7", "seen_30", "projects", "quotes_month", "quotes_30", "invoices_30", "acceptances_30", "calcs_30"}
    assert view["activity"]["projects"] == 1
    assert view["plans"] == {"none": 1, "trial": 0, "active": 0, "due": 0} and view["unread"] == []


# ================================================================== users and memberships

def test_an_account_is_made_with_a_first_password_reset_disabled_and_enabled(fdb):
    c = _console()
    cid = _company(c)["company"]["id"]
    made = _ok(c.post("/api/founder/users", json={"email": "Sunia@Meow.test", "name": "Sunia Ahmed", "password": "sunia first password",
                                                   "company_id": cid, "company_role": "member"}), 201)["user"]
    assert made["email"] == "sunia@meow.test" and made["disabled"] is False and set(made) == {"id", "email", "name", "disabled", "created", "archived", "memberships"}
    assert made["memberships"] == [{"company_id": cid, "company_name": "Boxlytics Internal", "company_role": "member", "status": "active"}]
    for body, words in (({"email": "sunia@meow.test", "name": "Sunia", "password": "another password 1"}, "account"),
                        ({"email": "x@meow.test", "name": "X", "password": "x first password", "company_id": str(uuid.uuid4())}, "company"),
                        ({"email": "x@meow.test", "name": "X", "password": "x first password", "company_role": "owner"}, "company"),
                        ({"email": "x@meow.test", "name": "X", "password": "          "}, "spaces"),
                        ({"email": "x@meow.test", "name": "X", "password": "x first password", "company_id": cid, "company_role": "boss"}, "role"),
                        ({"email": "x@meow.test", "name": "X", "password": "x first password", "role": "admin"}, "role")):
        r = c.post("/api/founder/users", json=body)
        assert r.status_code in (404, 409, 422) and words in r.json()["detail"], (body, r.text)
    assert platformdb.account_by_email("x@meow.test") is None                    # an account and its membership are made together, or neither
    # the person signs in with the first password
    signed = _ok(_client().post("/api/local/session", json={"email": "sunia@meow.test", "password": "sunia first password"}))
    old = signed["access_token"]
    assert _client().get("/api/sites", headers=identities.headers(old)).status_code == 200
    # a reset ends every session at once: the refresh token and the access token made before
    _ok(c.post(f"/api/founder/users/{made['id']}/password", json={"password": "sunia second password"}))
    assert _client().get("/api/sites", headers=identities.headers(old)).status_code == 401
    assert _client().post("/api/local/refresh", json={"refresh_token": signed["refresh_token"]}).status_code == 401
    assert platformdb.check_sign_in("sunia@meow.test", "sunia second password")
    assert c.post(f"/api/founder/users/{made['id']}/password", json={"password": "short"}).status_code == 422
    assert c.post(f"/api/founder/users/{uuid.uuid4()}/password", json={"password": "a good long password"}).status_code == 404
    # disabling refuses the next request of a token made before, and the sign-in; enabling lets a new sign-in in
    token = _ok(_client().post("/api/local/session", json={"email": "sunia@meow.test", "password": "sunia second password"}))["access_token"]
    off = _ok(c.patch(f"/api/founder/users/{made['id']}", json={"disabled": True}))["user"]
    assert off["disabled"] is True
    assert _client().get("/api/sites", headers=identities.headers(token)).status_code == 401
    assert _client().post("/api/local/session", json={"email": "sunia@meow.test", "password": "sunia second password"}).status_code == 401
    assert _ok(c.patch(f"/api/founder/users/{made['id']}", json={"disabled": False, "name": "Sunia A."}))["user"]["name"] == "Sunia A."
    assert _client().post("/api/local/session", json={"email": "sunia@meow.test", "password": "sunia second password"}).status_code == 200
    for body in ({}, {"disabled": "yes"}, {"name": ""}, {"email": "other@meow.test"}):
        assert c.patch(f"/api/founder/users/{made['id']}", json=body).status_code == 422, body
    actions = [e["action"] for e in _audit()][::-1]
    assert actions == ["made a company", "made an account", "set a password", "changed an account", "changed an account"]
    assert not any(p in json.dumps(_audit()) for p in ("sunia first password", "sunia second password"))


def test_memberships_are_added_changed_and_removed_and_a_company_keeps_its_owner(fdb):
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    other = _company(c, "Development", {"email": "dev@localhost", "name": "Local Tester", "password": "dev first password 1"})["company"]["id"]
    maaz = _ok(c.post("/api/founder/users", json={"email": "maaz@meow.test", "name": "Maaz Ali", "password": "maaz first password"}), 201)["user"]
    m = _ok(c.post(f"/api/founder/users/{maaz['id']}/memberships", json={"company_id": cid, "company_role": "admin"}), 201)["membership"]
    assert m == {"company_id": cid, "company_name": "Boxlytics Internal", "company_role": "admin", "status": "active"}
    r = c.post(f"/api/founder/users/{maaz['id']}/memberships", json={"company_id": cid, "company_role": "member"})
    assert r.status_code == 409 and _sentence(r)
    assert c.post(f"/api/founder/users/{maaz['id']}/memberships", json={"company_id": str(uuid.uuid4()), "company_role": "member"}).status_code == 404
    assert c.post(f"/api/founder/users/{uuid.uuid4()}/memberships", json={"company_id": cid, "company_role": "member"}).status_code == 404
    for body in ({"company_id": cid}, {"company_id": cid, "company_role": "boss"}, {"company_id": 5, "company_role": "member"}):
        assert c.post(f"/api/founder/users/{maaz['id']}/memberships", json=body).status_code == 422, body
    assert _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"status": "suspended"}))["membership"]["status"] == "suspended"
    assert _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "member", "status": "active"}))["membership"] == {
        **m, "company_role": "member"}
    for body in ({}, {"status": "gone"}, {"company_role": "boss"}, {"role": "admin"}):
        assert c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json=body).status_code == 422, body
    assert c.patch(f"/api/founder/users/{maaz['id']}/memberships/{other}", json={"status": "active"}).status_code == 404
    # the owner stays: demoting, suspending or removing the only active owner is refused; with a second owner (a hand-over) it is not
    for method, body in (("PATCH", {"company_role": "admin"}), ("PATCH", {"status": "suspended"}), ("PATCH", {"status": "pending"}), ("DELETE", None)):
        r = c.request(method, f"/api/founder/users/{owner['id']}/memberships/{cid}", json=body)
        assert r.status_code == 409 and "keeps its owner" in r.json()["detail"], (method, body, r.text)
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "owner"}))
    _ok(c.patch(f"/api/founder/users/{owner['id']}/memberships/{cid}", json={"company_role": "admin"}))
    # removing: a DELETE with no body, as the console sends it
    assert _ok(c.delete(f"/api/founder/users/{owner['id']}/memberships/{cid}")) == {"removed": True}
    assert c.delete(f"/api/founder/users/{owner['id']}/memberships/{cid}").status_code == 404
    users = {u["email"]: u for u in _ok(c.get("/api/founder/users"))["users"]}
    assert list(users) == ["dev@localhost", "maaz@meow.test", "saad@meow.test"] or sorted(users) == ["dev@localhost", "maaz@meow.test", "saad@meow.test"]
    assert users["saad@meow.test"]["memberships"] == [] and users["maaz@meow.test"]["memberships"][0]["company_role"] == "owner"
    assert {"gridlytics_role", "last_seen"} <= set(users["maaz@meow.test"]["memberships"][0])
    actions = [e["action"] for e in _audit()][::-1]
    assert actions.count("added a membership") == 1 and actions.count("changed a membership") == 4 and actions.count("removed a membership") == 1


def test_the_member_row_in_the_companys_file_follows_the_founders_change_of_the_membership(fdb):
    """B3's review: a seat is a member row not switched off in the company's file. When the founder suspends or removes a membership the row is switched off
    there (so its seat is free at once); when the membership is active again the row goes back to the seat rule, and the person is let in at their next
    request if a seat is free. A row a Gridlytics administrator switched off stays off."""
    c = _console()
    made = _company(c, seats=2)
    cid, owner = made["company"]["id"], made["owner"]
    sunia = _ok(c.post("/api/founder/users", json={"email": "sunia@meow.test", "name": "Sunia Ahmed", "password": "sunia first password", "company_id": cid}), 201)["user"]
    token = identity.issue_local(sunia["id"], sunia["email"], sunia["name"], 60)
    _ok(_client().get("/api/sites", headers=identities.headers(identity.issue_local(owner["id"], owner["email"], owner["name"], 60))))
    _ok(_client().get("/api/sites", headers=identities.headers(token)))
    path = platformdb.company_file(cid)

    def row():
        with store.use_company(path):
            return store_members.member(sunia["id"]), store_members.count_members()

    assert row()[0]["disabled"] is False and row()[1] == 2
    _ok(c.patch(f"/api/founder/users/{sunia['id']}/memberships/{cid}", json={"status": "suspended"}))
    found, seated = row()
    assert found["disabled"] is True and found["disabled_reason"] == "membership suspended" and seated == 1
    # a newcomer takes the seat she left
    maaz = _ok(c.post("/api/founder/users", json={"email": "maaz@meow.test", "name": "Maaz Ali", "password": "maaz first password", "company_id": cid}), 201)["user"]
    _ok(_client().get("/api/sites", headers=identities.headers(identity.issue_local(maaz["id"], maaz["email"], maaz["name"], 60))))
    _ok(c.patch(f"/api/founder/users/{sunia['id']}/memberships/{cid}", json={"status": "active"}))
    assert row()[0]["disabled_reason"] == "no seat"
    r = _client().get("/api/sites", headers=identities.headers(token))
    assert r.status_code == 403 and r.json()["code"] == "no_seat"                # both seats are taken
    _ok(c.patch(f"/api/founder/companies/{cid}", json={"seats": 3}))
    _ok(_client().get("/api/sites", headers=identities.headers(token)))             # a seat is free: she is let in again
    assert row()[0]["disabled"] is False
    _ok(c.delete(f"/api/founder/users/{sunia['id']}/memberships/{cid}"))
    assert row()[0]["disabled_reason"] == "membership removed"
    _ok(c.post(f"/api/founder/users/{sunia['id']}/memberships", json={"company_id": cid, "company_role": "member"}), 201)
    assert row()[0]["disabled_reason"] == "no seat"
    _ok(_client().get("/api/sites", headers=identities.headers(token)))
    # switched off by a Gridlytics administrator: the founder's change of the membership leaves that alone
    with store.use_company(path):
        store_members.update_member(sunia["id"], disabled=True, disabled_reason="left the team")
    _ok(c.patch(f"/api/founder/users/{sunia['id']}/memberships/{cid}", json={"status": "suspended"}))
    _ok(c.patch(f"/api/founder/users/{sunia['id']}/memberships/{cid}", json={"status": "active"}))
    assert row()[0]["disabled_reason"] == "left the team"
    # a row a follow-up could not reach (the file was busy) is set right by sending the membership's status again, though it did not change
    with store.use_company(path):
        store_members.update_member(sunia["id"], disabled=True, disabled_reason="membership suspended")
    _ok(c.patch(f"/api/founder/users/{sunia['id']}/memberships/{cid}", json={"status": "active"}))
    assert row()[0]["disabled_reason"] == "no seat"


# ================================================================== backups per company, the founder's only

def test_backups_are_per_company_and_reachable_only_by_the_founder(fdb):
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    other = _company(c, "Development", {"email": "dev@localhost", "name": "Local Tester", "password": "dev first password 1"})["company"]["id"]
    owner_token = identity.issue_local(owner["id"], owner["email"], owner["name"], 60)
    admin = _client()
    admin.headers.update(identities.headers(owner_token))
    _ok(admin.get("/api/sites"))
    assert _ok(c.get(f"/api/founder/companies/{cid}/backups")) == {"backups": []}
    first = _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))
    assert first["made"] and first["made"].startswith(cid) and [b["name"] for b in first["backups"]] == [first["made"]]
    backup = first["backups"][0]
    # a copy is the workspace, its telemetry, its signing keys and its reports (round 7, audit founder-console-12, parity-architecture-11)
    assert set(backup) == {"name", "kind", "modified", "bytes", "workspace_bytes", "telemetry", "telemetry_bytes", "keys", "reports", "reports_bytes"}
    assert backup["kind"] == "manual" and backup["bytes"] > 0
    assert datetime.fromisoformat(backup["modified"]).utcoffset().total_seconds() == 0
    for _ in range(backups.KEEP_MANUAL + 1):
        made_now = _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))
    assert len(made_now["backups"]) == backups.KEEP_MANUAL and made_now["made"] in {b["name"] for b in made_now["backups"]}
    # the other company's copies are its own; a company with no file yet has nothing to copy
    assert _ok(c.get(f"/api/founder/companies/{other}/backups")) == {"backups": []}
    assert _ok(c.post(f"/api/founder/companies/{other}/backups", json={})) == {"made": None, "backups": []}
    assert c.get(f"/api/founder/companies/{uuid.uuid4()}/backups").status_code == 404
    # the company's own administrator cannot reach them: the old routes are gone, and the founder's need the founder's session
    for r in (admin.get("/api/backups"), admin.post("/api/backups", json={})):
        assert r.status_code in (404, 405), r.text
    for r in (admin.get(f"/api/founder/companies/{cid}/backups"), admin.post(f"/api/founder/companies/{cid}/backups", json={})):
        assert r.status_code == 401 and r.json()["code"] == "founder_session_required"
    assert [e["action"] for e in _audit()].count("made a backup") == backups.KEEP_MANUAL + 2


def test_the_audit_lists_every_action_newest_first_a_page_at_a_time(fdb):
    c = _console()
    _company(c)
    for i in range(3):
        platformdb.founder_audit("made a backup", f"Company {i}", {"file": f"f{i}.db"})
    entries = _ok(c.get("/api/founder/audit"))["entries"]
    assert [e["target"] for e in entries] == ["Company 2", "Company 1", "Company 0", "Boxlytics Internal"]
    assert set(entries[0]) == {"at", "actor", "action", "target", "detail"} and entries[0]["actor"] == "founder"
    assert [e["target"] for e in _ok(c.get("/api/founder/audit?offset=3"))["entries"]] == ["Boxlytics Internal"]
    assert c.get("/api/founder/audit?offset=-1").status_code == 422


def test_every_founder_route_answers_with_the_session_and_refuses_a_body_of_the_wrong_shape_in_a_sentence(fdb, clock):
    """The role walk does not reach the founder's routes (walk_cases/founder.py): this is their walk. Each answers its good request with the founder's
    session, and each field of a good body sent as a list, null, a number or a long text, and each body sent empty or not an object, is refused in a
    sentence or answered, never a server error."""
    c = _console()
    made = _company(c)
    cid, uid = made["company"]["id"], made["owner"]["id"]
    sunia = _ok(c.post("/api/founder/users", json={"email": "sunia@meow.test", "name": "Sunia Ahmed", "password": "sunia first password", "company_id": cid}), 201)
    other = sunia["user"]["id"]
    goods = {("POST", "/api/founder/login"): {"username": "founder", "password": "a wrong password 12"},
             ("POST", "/api/founder/login/code"): {"challenge": "x" * 43, "code": "123456"},
             ("POST", "/api/founder/companies"): {"name": "Walk", "seats": 5, "enabled": True, "owner": {"user_id": uid}},
             ("PATCH", "/api/founder/companies/{company_id}"): {"name": "Walked", "seats": 4},
             ("POST", "/api/founder/companies/{company_id}/access"): {"enabled": True, "reason": ""},
             ("POST", "/api/founder/companies/{company_id}/archive"): {"archived": False},
             ("POST", "/api/founder/users/{user_id}/archive"): {"archived": False, "leave_without_owner": False},
             ("POST", "/api/founder/companies/{company_id}/backups"): {},
             ("POST", "/api/founder/users"): {"email": "walk@meow.test", "name": "Walk", "password": "walk first password"},
             ("POST", "/api/founder/users/{user_id}/password"): {"password": "a walk password 1"},
             ("PATCH", "/api/founder/users/{user_id}"): {"disabled": False, "name": "Walk"},
             ("POST", "/api/founder/users/{user_id}/memberships"): {"company_id": cid, "company_role": "member"},
             ("PATCH", "/api/founder/users/{user_id}/memberships/{company_id}"): {"status": "active"},
             ("POST", "/api/founder/logout"): {"everywhere": False}}
    problems = []
    logout = ("POST", "/api/founder/logout")
    # signing out ends the walk's session (round 7, audit founder-console-09): it is walked last, each body with a session of its own
    for method, path in sorted(_founder_routes(), key=lambda route: route == logout):
        url = path.replace("{company_id}", cid).replace("{user_id}", other)
        good = goods.get((method, path))
        if method == "GET" or good is None:
            r = c.request(method, url)
            if r.status_code != 200:
                problems.append(f"{method} {path}: answered {r.status_code}: {r.text[:160]}")
            continue
        bodies = [[], "text", 5, {}] + [{**good, k: v} for k in good for v in ([], None, 5, "x" * 500)] + [{**good, "unknown": 1}]
        for body in bodies:
            if (method, path) == logout:
                clock.step()
                c.headers.update(_session())
            r = c.request(method, url, json=body)
            if r.status_code >= 500 or (r.status_code >= 400 and not (_sentence(r) or r.json().get("code") == "invalid_body")):
                problems.append(f"{method} {path} with {json.dumps(body)[:80]}: answered {r.status_code}: {r.text[:160]}")
        if path == "/api/founder/users/{user_id}/memberships":
            c.delete(f"/api/founder/users/{other}/memberships/{cid}")
    assert not problems, chr(10).join(problems)


# ================================================================== supabase mode: the project's Admin API and tables, with the service-role key

class Project:
    """Gridlytics' own Supabase project as the console reaches it, kept in memory: the Admin API's users and the tables of supabase/migrations
    (companies, company_memberships, profiles), with the refusals the project makes."""

    def __init__(self):
        self.users, self.companies, self.memberships, self.profiles, self.calls = {}, {}, [], {}, []
        #: A test's hook, called with each call before it is answered: it may raise to make the project fail there
        self.fail = None

    def _refuse(self, status, body):
        raise store_founder.ProjectRefused(status, body)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.fail is not None:
            self.fail(method, path)
        base, _, query = path.partition("?")
        args = dict(p.split("=", 1) for p in query.split("&") if p)
        eq = {k: v[3:] for k, v in args.items() if v.startswith("eq.")}
        if base == "/auth/v1/admin/users" and method == "POST":
            if any(u["email"] == body["email"].lower() for u in self.users.values()):
                self._refuse(422, {"code": 422, "error_code": "email_exists", "msg": "A user with this email address has already been registered"})
            if len(body["password"].encode()) > 72:
                self._refuse(422, {"code": 422, "error_code": "weak_password", "msg": "Password cannot be longer than 72 characters"})
            uid = str(uuid.uuid4())
            self.users[uid] = {"id": uid, "email": body["email"].lower(), "created_at": "2026-10-25T09:00:00Z", "banned_until": None,
                               "user_metadata": dict(body.get("user_metadata") or {}), "password": body["password"],
                               "email_confirmed_at": "2026-10-25T09:00:00Z" if body.get("email_confirm") else None}
            self.profiles[uid] = {"id": uid}
            return {k: v for k, v in self.users[uid].items() if k != "password"}
        if base == "/auth/v1/admin/users" and method == "GET":
            page = int(args.get("page", 1))
            return {"users": [{k: v for k, v in u.items() if k != "password"} for u in list(self.users.values())[(page - 1) * 1000: page * 1000]]}
        if base.startswith("/auth/v1/admin/users/"):
            uid = base.rsplit("/", 1)[1]
            if uid not in self.users:
                self._refuse(404, {"code": 404, "error_code": "user_not_found", "msg": "User not found"})
            if method == "DELETE":
                del self.users[uid]
                return {}
            if method == "PUT":
                u = self.users[uid]
                if "password" in body:
                    u["password"] = body["password"]
                if "ban_duration" in body:
                    u["banned_until"] = None if body["ban_duration"] == "none" else "2126-01-01T00:00:00Z"
                if "user_metadata" in body:
                    u["user_metadata"].update(body["user_metadata"])
            return {k: v for k, v in self.users[uid].items() if k != "password"}
        if base == "/rest/v1/companies":
            if method in ("POST", "PATCH"):
                name = body.get("name")
                if name is not None and not 1 <= len(name.strip()) <= 80:
                    self._refuse(400, {"code": "23514", "message": 'new row for relation "companies" violates check constraint "companies_name_check"'})
                if name is not None and any(v["name"].lower() == name.lower() and k != eq.get("id", body.get("id")) for k, v in self.companies.items()):
                    self._refuse(409, {"code": "23505", "message": 'duplicate key value violates unique constraint "companies_name_unique_lower"'})
            if method == "POST":
                self.companies[body["id"]] = {"id": body["id"], "name": body["name"], "verified": True}
                return [self.companies[body["id"]]]
            if method == "PATCH":
                self.companies[eq["id"]].update(body)
                return [self.companies[eq["id"]]]
            if method == "DELETE":
                self.companies.pop(eq["id"], None)
                self.memberships = [m for m in self.memberships if m["company_id"] != eq["id"]]
                return []
        if base == "/rest/v1/company_memberships":
            pick = [m for m in self.memberships if all(m[k] == v for k, v in eq.items())]
            if method == "GET":
                offset, limit = int(args.get("offset", 0)), int(args.get("limit", 1000))
                return [dict(m) for m in pick[offset:offset + limit]]
            if method == "POST":
                if any(m["user_id"] == body["user_id"] and m["company_id"] == body["company_id"] for m in self.memberships):
                    self._refuse(409, {"code": "23505", "message": 'duplicate key value violates unique constraint "memberships_user_company_unique"'})
                if body.get("role") == "owner" and any(m["company_id"] == body["company_id"] and m["role"] == "owner" for m in self.memberships):
                    self._refuse(409, {"code": "23505", "message": 'duplicate key value violates unique constraint "memberships_one_owner_per_company"'})
                row = {"user_id": body["user_id"], "company_id": body["company_id"], "role": body.get("role", "member"), "status": body.get("status", "active"),
                       "created_at": "2026-10-25T09:00:00+00:00", "activated_at": "2026-10-25T09:00:00+00:00"}
                self.memberships.append(row)
                return [dict(row)]
            if method == "PATCH":
                if body.get("role") == "owner" and any(m["company_id"] == eq["company_id"] and m["role"] == "owner" and m["user_id"] != eq["user_id"]
                                                       for m in self.memberships):
                    self._refuse(409, {"code": "23505", "message": 'duplicate key value violates unique constraint "memberships_one_owner_per_company"'})
                for m in pick:
                    m.update(body)
                return [dict(m) for m in pick]
            if method == "DELETE":
                self.memberships = [m for m in self.memberships if m not in pick]
                return [dict(m) for m in pick]
        if base == "/rest/v1/profiles" and method == "PATCH":
            self.profiles.setdefault(eq["id"], {}).update(body)
            return [self.profiles[eq["id"]]]
        raise AssertionError(f"the stand-in project does not know {method} {path}")


@pytest.fixture
def project(fdb, monkeypatch):
    p = Project()
    monkeypatch.setattr(identity, "MODE", "supabase")
    monkeypatch.setenv("GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY", "made-up-service-role-key-of-the-tests")
    monkeypatch.setattr(store_founder, "_project", p)
    return p


def test_supabase_mode_makes_accounts_through_the_admin_api_and_the_company_with_the_registrys_id(project):
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    writes = [(m, p) for m, p, _ in project.calls if m != "GET"]
    assert writes == [("POST", "/auth/v1/admin/users"), ("POST", "/rest/v1/companies"), ("POST", "/rest/v1/company_memberships")]
    body = project.calls[0][2]
    assert body == {"email": OWNER["email"], "password": OWNER["password"], "email_confirm": True, "user_metadata": {"full_name": OWNER["name"]}}
    assert project.calls[1][2] == {"id": cid, "name": "Boxlytics Internal"} and platformdb.company(cid)["name"] == "Boxlytics Internal"
    assert project.calls[2][2] == {"user_id": owner["id"], "company_id": cid, "role": "owner", "status": "active"}
    assert platformdb.count_accounts() == 0                                       # no local account in supabase mode
    users = _ok(c.get("/api/founder/users"))["users"]
    assert [(u["email"], u["name"], u["disabled"]) for u in users] == [(OWNER["email"], OWNER["name"], False)]
    assert users[0]["memberships"][0]["company_role"] == "owner"
    assert _ok(c.get("/api/founder/companies"))["companies"][0]["owner"]["email"] == OWNER["email"]
    # disable is a ban; enable lifts it; a reset sets the password; a rename follows the profile (the company's only owner: said knowingly, round 7)
    _ok(c.patch(f"/api/founder/users/{owner['id']}", json={"disabled": True, "leave_without_owner": True}))
    assert project.users[owner["id"]]["banned_until"] and _ok(c.get("/api/founder/users"))["users"][0]["disabled"] is True
    _ok(c.patch(f"/api/founder/users/{owner['id']}", json={"disabled": False, "name": "Saad K. Khan"}))
    assert project.users[owner["id"]]["banned_until"] is None and project.profiles[owner["id"]] == {"id": owner["id"], "first_name": "Saad",
                                                                                                    "last_name": "K. Khan"}
    _ok(c.post(f"/api/founder/users/{owner['id']}/password", json={"password": "a new long password"}))
    assert project.users[owner["id"]]["password"] == "a new long password"
    # an account and its membership; then the membership changed and removed
    maaz = _ok(c.post("/api/founder/users", json={"email": "maaz@meow.test", "name": "Maaz Ali", "password": "maaz first password", "company_id": cid}), 201)["user"]
    assert maaz["memberships"][0]["company_role"] == "member"
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"status": "suspended"}))
    assert _ok(c.delete(f"/api/founder/users/{maaz['id']}/memberships/{cid}")) == {"removed": True}
    assert project.memberships == [m for m in project.memberships if m["user_id"] == owner["id"]]


def test_supabase_mode_maps_the_projects_refusals(project):
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    # a name another company has, whatever its case
    r = c.post("/api/founder/companies", json={"name": "BOXLYTICS internal", "seats": 5, "enabled": True, "owner": {"user_id": owner["id"]}})
    assert r.status_code == 409 and "name" in r.json()["detail"]
    project.companies["00000000-0000-0000-0000-000000000001"] = {"id": "00000000-0000-0000-0000-000000000001", "name": "Only In The Project"}
    calls = len(project.calls)
    r = c.post("/api/founder/companies", json={"name": "only in the project", "seats": 5, "enabled": True, "owner": dict(OWNER, email="new@meow.test")})
    assert r.status_code == 409 and "name" in r.json()["detail"]
    assert platformdb.list_companies() == [platformdb.company(cid)] and not any(u["email"] == "new@meow.test" for u in project.users.values())
    assert project.calls[-1][0] == "DELETE" and project.calls[-1][1].startswith("/auth/v1/admin/users/") and len(project.calls) == calls + 3   # undone
    # a second owner: the project keeps one, so making another member the owner hands the ownership over
    maaz = _ok(c.post("/api/founder/users", json={"email": "maaz@meow.test", "name": "Maaz Ali", "password": "maaz first password", "company_id": cid}), 201)["user"]
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "owner"}))
    roles = {m["user_id"]: m["role"] for m in project.memberships}
    assert roles == {owner["id"]: "admin", maaz["id"]: "owner"}
    r = c.post(f"/api/founder/users/{owner['id']}/memberships", json={"company_id": cid, "company_role": "owner"})
    assert r.status_code == 409
    # the ownership is handed only to an active member: the owner before becomes an admin, so a suspended one would leave the company with no owner
    _ok(c.patch(f"/api/founder/users/{owner['id']}/memberships/{cid}", json={"status": "suspended"}))
    r = c.patch(f"/api/founder/users/{owner['id']}/memberships/{cid}", json={"company_role": "owner"})
    assert r.status_code == 409 and "active member" in r.json()["detail"]
    assert {m["user_id"]: (m["role"], m["status"]) for m in project.memberships} == {owner["id"]: ("admin", "suspended"), maaz["id"]: ("owner", "active")}
    _ok(c.patch(f"/api/founder/users/{owner['id']}/memberships/{cid}", json={"company_role": "owner", "status": "active"}))
    assert {m["user_id"]: m["role"] for m in project.memberships} == {owner["id"]: "owner", maaz["id"]: "admin"}
    # a hand-over the project fails half-way is taken back: the owner before is the owner again
    def second_patch_fails(method, path, seen=[]):
        if method == "PATCH" and path.startswith("/rest/v1/company_memberships"):
            seen.append(path)
            if len(seen) == 2:
                raise store_founder.Unreachable("the project did not answer (made up)")
    project.fail = second_patch_fails
    r = c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "owner"})
    project.fail = None
    assert r.status_code == 503 and {m["user_id"]: m["role"] for m in project.memberships} == {owner["id"]: "owner", maaz["id"]: "admin"}
    # an email that has an account; a password over the project's 72 characters, refused before any call
    r = c.post("/api/founder/users", json={"email": OWNER["email"], "name": "Saad", "password": "another first password"})
    assert r.status_code == 409 and "account" in r.json()["detail"]
    calls = len(project.calls)
    r = c.post("/api/founder/users", json={"email": "long@meow.test", "name": "Long", "password": "p" * 73})
    assert r.status_code == 422 and "72" in r.json()["detail"] and len(project.calls) == calls
    assert c.post(f"/api/founder/users/{owner['id']}/password", json={"password": "p" * 73}).status_code == 422
    # a name the project's check refuses (the registry strips every space; the project trims only plain ones)
    assert _ok(c.patch(f"/api/founder/companies/{cid}", json={"name": " Boxlytics "}))["company"]["name"] == "Boxlytics"
    assert project.companies[cid]["name"] == "Boxlytics"


def test_every_change_is_audited_even_when_reading_it_back_fails(project):
    """A change the project made is written to the founder's audit before anything is read back: the project failing the reads after it gives 503, and the
    audit still has the change."""
    c = _console()
    cid, owner = (lambda made: (made["company"]["id"], made["owner"]))(_company(c))
    maaz = _ok(c.post("/api/founder/users", json={"email": "maaz@meow.test", "name": "Maaz Ali", "password": "maaz first password"}), 201)["user"]
    wrote = []

    def reads_fail_after_a_write(method, path):
        if method != "GET":
            wrote.append(path)
        elif wrote:
            raise store_founder.Unreachable("the project did not answer (made up)")

    for method, path, body, action in (
            ("POST", "/api/founder/companies", {"name": "Second", "seats": 5, "enabled": True, "owner": {"email": "o@meow.test", "name": "Olga", "password": "olga first password"}}, "made a company"),
            ("PATCH", f"/api/founder/companies/{cid}", {"name": "Renamed"}, "changed a company"),
            ("POST", "/api/founder/users", {"email": "n@meow.test", "name": "Nadia", "password": "nadia first password"}, "made an account"),
            ("POST", f"/api/founder/users/{maaz['id']}/password", {"password": "maaz second password"}, "set a password"),
            ("PATCH", f"/api/founder/users/{maaz['id']}", {"name": "Maaz A."}, "changed an account"),
            ("POST", f"/api/founder/users/{maaz['id']}/memberships", {"company_id": cid, "company_role": "member"}, "added a membership"),
            ("PATCH", f"/api/founder/users/{maaz['id']}/memberships/{cid}", {"status": "suspended"}, "changed a membership")):
        wrote.clear()
        project.fail = reads_fail_after_a_write
        r = c.request(method, path, json=body)
        project.fail = None
        assert r.status_code == 503 and wrote, (method, path, r.text)
        assert _audit()[0]["action"] == action, (action, _audit()[0])
    assert project.users[maaz["id"]]["password"] == "maaz second password" and project.companies[cid]["name"] == "Renamed"


def test_supabase_mode_says_when_the_project_cannot_be_reached_or_is_not_set_up(project, monkeypatch):
    def down(method, path, body=None):
        raise store_founder.Unreachable("the project did not answer (made up)")
    with monkeypatch.context() as m:
        m.setattr(store_founder, "_project", down)
        r = _console().get("/api/founder/users")
        assert r.status_code == 503 and r.json()["code"] == "identity_unavailable" and _sentence(r)
    monkeypatch.setattr(store_founder, "_project", store_founder._call_project)
    monkeypatch.delenv("GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY", raising=False)
    r = _console().get("/api/founder/users")
    assert r.status_code == 503 and "GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY" in r.json()["detail"]
