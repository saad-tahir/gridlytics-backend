"""Round 7 (the end-to-end audit), the founder console: a short session secret is refused, and its session ends on the server. One test per audit
finding; the finding's id is in each test's docstring."""
from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from gridlytics import founder
from gridlytics import platform as platformdb
from test_founder import (PASSWORD, SESSION_SECRET, _audit, _client, _code, _ok, _sentence, clock, fdb)  # noqa: F401  (fixtures)

REPO = Path(__file__).resolve().parents[2]


def _headers(token: str) -> dict:
    return {"x-founder-session": token}


# ================================================================== security-roles-12: the session secret's length

def test_a_short_founder_session_secret_is_refused_by_the_console(fdb, monkeypatch):
    """security-roles-12: a 3-character GRIDLYTICS_FOUNDER_SESSION_SECRET was taken, and one token let it be guessed offline."""
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", "abc")
    assert "GRIDLYTICS_FOUNDER_SESSION_SECRET" in founder.malformed()
    for method, path in (("POST", "/api/founder/login"), ("GET", "/api/founder/session")):
        r = _client().request(method, path, json={"username": "founder", "password": PASSWORD} if method == "POST" else None,
                              headers=_headers("x.y"))
        assert r.status_code == 503 and r.json()["code"] == "unavailable", r.text
        assert "GRIDLYTICS_FOUNDER_SESSION_SECRET" in r.json()["detail"] and "32 characters" in r.json()["detail"] and "abc" not in r.text
    with pytest.raises(founder.Refused):
        founder.new_token()
    with pytest.raises(RuntimeError, match="GRIDLYTICS_FOUNDER_SESSION_SECRET is shorter than 32 characters"):
        founder.check_startup()
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", " " * 40)          # not set: the console is off (503), the API starts
    founder.check_startup()
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", "x" * 32)
    founder.check_startup()
    assert founder.malformed() == []


def test_the_founders_command_line_makes_a_session_secret_long_enough(fdb, monkeypatch):
    """security-roles-12: the refusal names `python -m gridlytics.founder session-secret`, which makes a secret the console takes."""
    out, err = io.StringIO(), io.StringIO()
    assert founder.main(["session-secret"], stdout=out, stderr=err) == 0 and err.getvalue() == ""
    secret = out.getvalue().strip()
    assert len(secret) >= founder.SESSION_SECRET_CHARS and secret != SESSION_SECRET
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", secret)
    founder.check_startup()
    assert founder.malformed() == []


def _start_api(tmp_path: Path, secret: str | None) -> subprocess.CompletedProcess:
    env = {**os.environ, "GRIDLYTICS_DB": str(tmp_path / "start.db"), "GRIDLYTICS_LOGS": "0", "PYTHONPATH": str(REPO / "backend")}
    env.pop("GRIDLYTICS_FOUNDER_SESSION_SECRET", None)
    if secret is not None:
        env["GRIDLYTICS_FOUNDER_SESSION_SECRET"] = secret
    return subprocess.run([sys.executable, "-c", "import gridlytics.api"], cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180)


def test_the_api_does_not_start_with_a_short_founder_session_secret(tmp_path):
    """security-roles-12: the API started with a short session secret; it is refused at start-up, as a short GRIDLYTICS_LOCAL_JWT_SECRET is."""
    refused = _start_api(tmp_path, "abc")
    assert refused.returncode != 0 and "GRIDLYTICS_FOUNDER_SESSION_SECRET is shorter than 32 characters" in refused.stderr
    assert "abc" not in refused.stderr.replace("GRIDLYTICS", "")
    started = _start_api(tmp_path, None)                                       # no console: the API starts, the console answers 503
    assert started.returncode == 0, started.stderr[-2000:]


# ================================================================== founder-console-09: the session ends on the server

def _signed_in(c, clock) -> str:
    """A founder session made by the two-step sign-in, as the console makes one; the clock moves on, so the next one is a session of its own."""
    challenge = _ok(c.post("/api/founder/login", json={"username": "founder", "password": PASSWORD}))["challenge"]
    token = _ok(c.post("/api/founder/login/code", json={"challenge": challenge, "code": _code()}))["token"]
    clock.step()
    return token


def test_signing_out_ends_the_founder_session_on_the_server_and_only_that_one(fdb, clock):
    """founder-console-09: Sign out sent the server nothing, and the same token still answered 200 for the rest of its 8 hours."""
    c = _client()
    mine, other = _signed_in(c, clock), _signed_in(c, clock)
    assert _ok(c.get("/api/founder/session", headers=_headers(mine)))["authenticated"]
    assert _ok(c.post("/api/founder/logout", json={}, headers=_headers(mine))) == {"ended": True, "everywhere": False}
    for path in ("/api/founder/session", "/api/founder/companies"):
        r = c.get(path, headers=_headers(mine))
        assert r.status_code == 401 and r.json()["code"] == "founder_session_ended" and _sentence(r), r.text
    assert _ok(c.get("/api/founder/session", headers=_headers(other)))["authenticated"]         # the other tab's session holds
    # signing out again with the ended token is refused as any ended session is; with no session at all it is the usual 401
    assert c.post("/api/founder/logout", json={}, headers=_headers(mine)).status_code == 401
    r = c.post("/api/founder/logout", json={})
    assert r.status_code == 401 and r.json()["code"] == "founder_session_required"
    r = c.post("/api/founder/logout", json={"everywhere": "yes"}, headers=_headers(other))
    assert r.status_code == 422 and _sentence(r)
    r = c.post("/api/founder/logout", json={"tab": 1}, headers=_headers(other))
    assert r.status_code == 422 and _sentence(r)
    assert [e["action"] for e in _audit()][:1] == ["signed out"]
    # nothing of the token itself is kept on the server, only its hash
    with closing(sqlite3.connect(platformdb.db_path())) as con:
        kept = " ".join(r[0] for r in con.execute("SELECT detail FROM account_events"))
    assert mine not in kept and hashlib.sha256(mine.encode()).hexdigest() in kept


def test_signing_out_everywhere_ends_every_founder_session(fdb, clock):
    """founder-console-09: there was no way to end every founder session short of rotating the session secret and restarting."""
    c = _client()
    first, second = _signed_in(c, clock), _signed_in(c, clock)
    assert _ok(c.post("/api/founder/logout", json={"everywhere": True}, headers=_headers(second))) == {"ended": True, "everywhere": True}
    for token in (first, second):
        r = c.get("/api/founder/session", headers=_headers(token))
        assert r.status_code == 401 and r.json()["code"] == "founder_session_ended", r.text
    clock.step()
    fresh = _signed_in(c, clock)                                                     # a sign-in after it holds
    assert _ok(c.get("/api/founder/session", headers=_headers(fresh)))["authenticated"]
    assert [e["action"] for e in _audit()][:2] == ["signed in", "ended every founder session"]


def test_a_new_founder_password_ends_every_session_made_with_the_old_one(fdb, clock, monkeypatch):
    """founder-console-09: changing GRIDLYTICS_FOUNDER_PASSWORD_HASH left every session made with the old password valid."""
    c = _client()
    old = _signed_in(c, clock)
    assert _ok(c.get("/api/founder/session", headers=_headers(old)))["authenticated"]
    clock.step(3600)
    new_password = "a new made-up founder password"
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_PASSWORD_HASH", founder.hash_password(new_password))
    r = c.get("/api/founder/session", headers=_headers(old))
    assert r.status_code == 401 and r.json()["code"] == "founder_session_ended" and "password" in r.json()["detail"], r.text
    assert "ended every founder session" in [e["action"] for e in _audit()]
    # the new password signs in at once, in the very second the change was seen, and its session holds
    challenge = _ok(c.post("/api/founder/login", json={"username": "founder", "password": new_password}))["challenge"]
    fresh = _ok(c.post("/api/founder/login/code", json={"challenge": challenge, "code": _code()}))["token"]
    assert _ok(c.get("/api/founder/session", headers=_headers(fresh)))["authenticated"]
    # seen once: the next request ends nothing more
    assert [e["action"] for e in _audit()].count("ended every founder session") == 1


def test_a_new_password_seen_first_by_a_sign_in_ends_the_old_sessions_but_not_the_new_one(fdb, clock, monkeypatch):
    """founder-console-09: the change is noticed before a new session is made, so the first session of the new password is not ended with the old ones."""
    c = _client()
    old = _signed_in(c, clock)
    new_password = "another made-up founder password"
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_PASSWORD_HASH", founder.hash_password(new_password))
    challenge = _ok(c.post("/api/founder/login", json={"username": "founder", "password": new_password}))["challenge"]
    fresh = _ok(c.post("/api/founder/login/code", json={"challenge": challenge, "code": _code()}))["token"]
    assert _ok(c.get("/api/founder/session", headers=_headers(fresh)))["authenticated"]
    assert c.get("/api/founder/session", headers=_headers(old)).status_code == 401
