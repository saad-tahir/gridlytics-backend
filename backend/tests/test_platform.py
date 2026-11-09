"""Stage P1 of the sign-in: the platform database (data/platform.db) and the local sign-in service (/api/local/*), decisions-v2 sections 1, 2, 3 and 5.

The routes are not mounted in api.py yet (stage B4a does that, with the guard that resolves the person). Here they are answered by a small app of their own, with a
stand-in for that guard: it reads the bearer token's claims and puts the person on `request.state.identity`, as B4a's guard will after checking the token. The
access token is minted by `identity.issue_local` (stage B1); where B1 is not merged yet, a stand-in with its documented signature takes its place.
"""
import base64
import hashlib
import hmac
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import types
import uuid
from contextlib import closing
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from gridlytics import api, auth, store
from gridlytics import platform as platformdb

REPO = Path(__file__).resolve().parents[2]
PASSWORD = "a long enough password"
#: A made-up secret for the stand-in token, never a real one
_STAND_IN_SECRET = b"stand-in-secret-for-the-p1-tests-only-0123456789"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _claims(token: str) -> dict:
    """The payload of a token, unchecked: the stand-in guard's and the tests' reading (checking it is stage B1's)."""
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


@pytest.fixture
def plat(tmp_path, monkeypatch):
    """A registry of the test's own: the workspace database moves to a scratch folder, and the registry follows it."""
    monkeypatch.delenv("GRIDLYTICS_DATA_DIR", raising=False)
    monkeypatch.delenv("GRIDLYTICS_TRUST_PROXY", raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "gridlytics.db")
    return platformdb


@pytest.fixture
def issued(monkeypatch):
    """The calls made to identity.issue_local, as (user_id, email, name, minutes). B1's module when it is there, else a stand-in of its signature."""
    calls = []
    try:
        from gridlytics import identity
    except ImportError:
        identity = types.ModuleType("gridlytics.identity")

        def issue_local(user_id: str, email: str, name: str, minutes: int = 60) -> str:
            now = int(time.time())
            head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            body = _b64(json.dumps({"sub": user_id, "email": email, "role": "authenticated", "aud": "authenticated", "iss": "gridlytics-local",
                                    "amr": [{"method": "password", "timestamp": now}], "user_metadata": {"full_name": name}, "iat": now,
                                    "exp": now + minutes * 60, "jti": uuid.uuid4().hex}).encode())
            sig = _b64(hmac.new(_STAND_IN_SECRET, f"{head}.{body}".encode(), hashlib.sha256).digest())
            return f"{head}.{body}.{sig}"

        identity.issue_local = issue_local
        monkeypatch.setitem(sys.modules, "gridlytics.identity", identity)
    monkeypatch.setenv("GRIDLYTICS_LOCAL_JWT_SECRET", "made-up-local-secret-for-tests-0123456789abcdef")
    real = identity.issue_local

    def recording(user_id, email, name, minutes=60, **session):
        calls.append((user_id, email, name, minutes))
        return real(user_id, email, name, minutes, **session)

    monkeypatch.setattr(identity, "issue_local", recording)
    return calls


@pytest.fixture
def client(plat, issued):
    from gridlytics import routes_local
    app = FastAPI()
    for route in routes_local.router.routes:
        app.add_api_route(route.path, route.endpoint, methods=sorted(route.methods), name=route.name)
    app.add_exception_handler(StarletteHTTPException, api._http_refusal)

    @app.middleware("http")
    async def stand_in_guard(request, call_next):
        # Stage B4a's guard checks the token (identity.verify) and leaves the person here; the stand-in only reads the claims
        bearer = request.headers.get("authorization") or ""
        if bearer[:7].lower() == "bearer ":
            claims = _claims(bearer[7:].strip())
            request.state.identity = types.SimpleNamespace(user_id=claims["sub"], email=claims.get("email", ""), claims=claims)
        return await call_next(request)

    return TestClient(app)


def _owner_company(plat, email="saad@example.pk", name="Saad Khan", enabled=True):
    company = plat.create_company("Acme Solar", enabled=enabled, seats=5)
    account = plat.create_account(email, name, PASSWORD)
    plat.add_membership(account["id"], company["id"], "owner", "active")
    return company, account


def _sign_in(client, email="saad@example.pk", password=PASSWORD, **headers):
    return client.post("/api/local/session", json={"email": email, "password": password}, headers=headers)


def _bearer(session: dict) -> dict:
    return {"Authorization": f"Bearer {session['access_token']}"}


# ------------------------------------------------------------------------------------------------ where the registry is (addendum items 1 and 3)

def test_the_registry_sits_beside_the_workspace_database_read_at_call_time(plat, tmp_path, monkeypatch):
    first, second = tmp_path / "one" / "a.db", tmp_path / "two" / "b.db"
    monkeypatch.setattr(store, "DB_PATH", first)
    assert plat.data_dir() == first.parent and plat.db_path() == first.parent / "platform.db"
    assert plat.secret_file() == first.parent / "platform.secret" and plat.companies_dir() == first.parent / "companies"
    assert plat.count_accounts() == 0 and not plat.db_path().exists()            # counting makes no file
    plat.create_account("one@example.pk", "One", PASSWORD)
    monkeypatch.setattr(store, "DB_PATH", second)
    assert plat.db_path() == second.parent / "platform.db"
    assert plat.count_accounts() == 0 and plat.account_by_email("one@example.pk") is None      # two workspace files, two registries
    plat.create_account("two@example.pk", "Two", PASSWORD)
    monkeypatch.setattr(store, "DB_PATH", first)
    assert plat.count_accounts() == 1 and plat.account_by_email("two@example.pk") is None
    # GRIDLYTICS_DATA_DIR, when set, is the data folder whatever the workspace file
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path / "data"))
    assert plat.data_dir() == tmp_path / "data" and plat.db_path() == tmp_path / "data" / "platform.db"
    assert plat.count_accounts() == 0
    assert not first.exists() and not second.exists()                             # the workspace files are never made by the registry


def test_the_registry_has_its_own_schema_version_and_is_copied_before_a_migration(plat):
    path = plat.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:                                            # a registry of an earlier version, holding something
        con.execute("CREATE TABLE notes (x TEXT)")
        con.execute("INSERT INTO notes VALUES ('kept')")
    assert plat.count_accounts() == 0
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == plat.SCHEMA_VERSION
        assert con.execute("SELECT x FROM notes").fetchone()[0] == "kept"
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    copies = sorted((path.parent / "backup").glob("*.db"))
    assert len(copies) == 1 and copies[0].name.startswith("platform-") and copies[0].name.endswith("-schema-v0.db")
    with closing(sqlite3.connect(copies[0])) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0 and con.execute("SELECT x FROM notes").fetchone()[0] == "kept"
    plat.create_account("a@example.pk", "A", PASSWORD)
    assert len(list((path.parent / "backup").glob("*.db"))) == 1                    # at the current version nothing more is copied
    assert not store.DB_PATH.exists()                                             # nor is the workspace's schema put in it
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'runs'").fetchone()[0] == 0


def test_a_new_registry_is_made_without_a_copy_and_backup_copies_it_for_the_daily_loop(plat):
    assert plat.backup("daily") is None                                           # nothing to copy yet
    plat.create_company("Acme Solar")
    assert not (plat.db_path().parent / "backup").exists()
    copy = plat.backup("daily")
    assert copy.parent == plat.db_path().parent / "backup" and copy.name.startswith("platform-") and copy.name.endswith("-daily.db")
    with closing(sqlite3.connect(copy)) as con:
        assert con.execute("SELECT name FROM companies").fetchone()[0] == "Acme Solar"


def test_a_company_has_its_own_file_by_default_or_any_absolute_path(plat, tmp_path):
    made = plat.create_company("Acme Solar", enabled=True, seats=3)
    assert uuid.UUID(made["id"]) and made["enabled"] is True and made["seats"] == 3 and made["disabled_reason"] == ""
    assert plat.company_file(made["id"]) == plat.companies_dir() / f"{made['id']}.db"
    shared = tmp_path / "elsewhere" / "workspace.db"
    other = plat.create_company("Test company", db_file=shared)
    assert plat.company_file(other["id"]) == shared and plat.company_file(other) == shared
    with pytest.raises(ValueError):
        plat.create_company("Relative", db_file="companies/x.db")
    off = plat.create_company("Switched off")
    assert plat.company_files() == [plat.company_file(made["id"])]                # the enabled companies, for the background loops
    assert plat.company_file(str(uuid.uuid4())) is None
    with pytest.raises(ValueError, match="id exists"):
        plat.create_company("Same id", company_id=made["id"])
    # the default file follows the data folder at call time
    assert plat.update_company(other["id"], enabled=True)["enabled"] is True
    assert set(plat.company_files()) == {plat.company_file(made["id"]), shared}
    changed = plat.update_company(off["id"], name="  Renamed  ", seats=7, disabled_reason="Not paid")
    assert changed["name"] == "Renamed" and changed["seats"] == 7 and changed["disabled_reason"] == "Not paid"
    for bad in ({"name": " "}, {"seats": -1}, {"seats": True}, {"enabled": "yes"}):
        with pytest.raises(ValueError):
            plat.update_company(off["id"], **bad)
    assert [c["name"] for c in plat.list_companies()] == ["Acme Solar", "Renamed", "Test company"]


# ------------------------------------------------------------------------------------------------ accounts and memberships

def test_accounts_keep_the_apps_pbkdf2_hash_and_its_password_rules(plat, monkeypatch):
    used = []
    real = auth.hash_password
    monkeypatch.setattr(auth, "hash_password", lambda p: used.append(p) or real(p))
    account = plat.create_account("  Saad@Example.PK ", " Saad Khan ", PASSWORD)
    assert used == [PASSWORD]                                                    # auth.py's hashing, imported, not a copy
    assert account["email"] == "saad@example.pk" and account["name"] == "Saad Khan" and "password" not in account
    assert uuid.UUID(account["id"]) and account["disabled"] is False
    with sqlite3.connect(plat.db_path()) as con:
        stored = con.execute("SELECT password FROM accounts").fetchone()[0]
    assert stored.startswith(f"pbkdf2_sha256${auth.ITERATIONS}$") and auth.check_password(PASSWORD, stored)
    assert plat.check_sign_in("SAAD@example.pk", PASSWORD)["id"] == account["id"]
    assert plat.check_sign_in("saad@example.pk", "not the password") is None
    for email, name, password in (("x@y.pk", "X", "short"), ("x@y.pk", "X", " " * 12), ("x@y.pk", "X", "p" * 201), ("not an email", "X", PASSWORD),
                                  ("x@y.pk", "", PASSWORD), ("x@y.pk", "n" * 81, PASSWORD), ("saad@EXAMPLE.pk", "Again", PASSWORD)):
        with pytest.raises(ValueError):
            plat.create_account(email, name, password)
    # the decisions' demo user has no dot in its domain
    assert plat.create_account("dev@localhost", "Local Tester", PASSWORD)["email"] == "dev@localhost"
    with pytest.raises(ValueError, match="already has an account"):
        plat.create_account("SAAD@example.pk", "Again", PASSWORD, account_id=str(uuid.uuid4()))
    with pytest.raises(ValueError, match="id exists"):
        plat.create_account("new@example.pk", "New", PASSWORD, account_id=account["id"])


def test_memberships_carry_a_company_role_and_a_status(plat):
    company, account = _owner_company(plat)
    member = plat.create_account("maaz@example.pk", "Maaz Ali", PASSWORD)
    m = plat.add_membership(member["id"], company["id"])
    assert m["role"] == "member" and m["status"] == "active" and m["activated_at"]
    pending = plat.add_membership(member["id"], plat.create_company("Other")["id"], "admin", "pending")
    assert pending["status"] == "pending" and pending["activated_at"] is None
    with pytest.raises(ValueError):
        plat.add_membership(member["id"], company["id"])                           # one membership per company
    for role, status in (("superuser", "active"), ("member", "gone")):
        with pytest.raises(ValueError):
            plat.add_membership(account["id"], plat.create_company("X")["id"], role, status)
    with pytest.raises(ValueError):
        plat.add_membership(str(uuid.uuid4()), company["id"])
    for role, status in ((None, "active"), ("member", None)):
        with pytest.raises(ValueError):
            plat.add_membership(account["id"], plat.create_company("Y")["id"], role, status)
    assert plat.set_membership(member["id"], company["id"], status="suspended")["status"] == "suspended"
    assert plat.set_membership(member["id"], company["id"], role="admin")["role"] == "admin"
    assert {(r["user_id"], r["role"]) for r in plat.company_members(company["id"])} == {(account["id"], "owner"), (member["id"], "admin")}
    assert plat.remove_membership(member["id"], company["id"]) is True and plat.membership(member["id"], company["id"]) is None
    assert plat.remove_membership(member["id"], company["id"]) is False


# ------------------------------------------------------------------------------------------------ signing in

def test_signing_in_answers_a_one_hour_access_token_and_a_refresh_token(client, plat, issued):
    _, account = _owner_company(plat)
    before = int(time.time())
    r = _sign_in(client, email="  SAAD@example.pk ")
    assert r.status_code == 200
    s = r.json()
    assert set(s) == {"access_token", "token_type", "expires_in", "expires_at", "refresh_token", "user"}
    assert s["token_type"] == "bearer" and s["expires_in"] == 3600 and isinstance(s["expires_at"], int)
    assert before + 3600 <= s["expires_at"] <= int(time.time()) + 3600              # unix seconds (addendum item 22)
    assert issued == [(account["id"], "saad@example.pk", "Saad Khan", 60)]          # identity.issue_local(user_id, email, name, minutes)
    claims = _claims(s["access_token"])
    assert claims["sub"] == account["id"] and claims["exp"] - claims["iat"] == 3600
    assert s["user"] == {"id": account["id"], "email": "saad@example.pk", "name": "Saad Khan", "created": account["created"]}
    # the refresh token is kept only as its hash
    raw = plat.db_path().read_bytes() + (plat.db_path().with_name("platform.db-wal").read_bytes() if plat.db_path().with_name("platform.db-wal").exists() else b"")
    assert s["refresh_token"].encode() not in raw
    with sqlite3.connect(plat.db_path()) as con:
        assert con.execute("SELECT COUNT(*) FROM refresh_tokens WHERE token_hash = ?", (auth.token_hash(s["refresh_token"]),)).fetchone()[0] == 1
    assert [e["event"] for e in plat.events("saad@example.pk")] == ["signed in"]


def test_a_wrong_password_an_unknown_email_and_a_disabled_account_get_one_refusal(client, plat, monkeypatch):
    _, account = _owner_company(plat)
    checked = []
    real = auth.check_password
    monkeypatch.setattr(auth, "check_password", lambda p, stored: checked.append(p) or real(p, stored))
    wrong = _sign_in(client, password="not the password")
    unknown = _sign_in(client, email="nobody@example.pk", password="a guess at it")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"] == "The email or the password is wrong"
    assert wrong.json()["code"] == unknown.json()["code"] == "invalid_credentials"
    assert checked == ["not the password", "a guess at it"]                      # an unknown email costs the same PBKDF2 work as a known one
    # a body without the two texts is refused before any account is looked at: one answer, and no hashing, for a known email and an unknown one
    checked.clear()
    for body in ({"email": "saad@example.pk", "password": 5}, {"email": "nobody@example.pk"}, {"email": ["x"], "password": PASSWORD}):
        r = client.post("/api/local/session", json=body)
        assert r.status_code == 422 and r.json()["detail"] == "Give your email address and your password", body
    assert checked == []
    plat.set_account_disabled(account["id"], True)
    disabled = _sign_in(client)
    assert disabled.status_code == 401 and disabled.json()["detail"] == wrong.json()["detail"]
    assert plat.events("nobody@example.pk") == []                                # tries for an address with no account are not recorded
    assert [e["event"] for e in plat.events("saad@example.pk")] == ["sign-in refused", "sign-in refused"]


def test_refresh_rotates_the_token_and_a_used_one_is_refused(client, plat, monkeypatch):
    _owner_company(plat)
    first = _sign_in(client).json()
    r = client.post("/api/local/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200
    second = r.json()
    assert set(second) == set(first) and second["refresh_token"] != first["refresh_token"] and second["user"] == first["user"]
    # within a few seconds a used token is a race of two tabs: refused with its own code (that tab must not sign out), and the session goes on
    again = client.post("/api/local/refresh", json={"refresh_token": first["refresh_token"]})
    assert again.status_code == 409 and again.json()["code"] == "session_renewed"
    third = client.post("/api/local/refresh", json={"refresh_token": second["refresh_token"]}).json()
    assert third["refresh_token"] not in (first["refresh_token"], second["refresh_token"])
    # later, a used token was taken: the whole session ends (OAuth 2.0 Security BCP, refresh token reuse) and is recorded
    later = time.time() + plat.REUSE_SECONDS + 1
    monkeypatch.setattr(plat, "_clock", lambda: later)
    stolen = client.post("/api/local/refresh", json={"refresh_token": second["refresh_token"]})
    assert stolen.status_code == 401 and stolen.json()["code"] == "session_ended"
    assert client.post("/api/local/refresh", json={"refresh_token": third["refresh_token"]}).status_code == 401
    assert "refresh token used again" in [e["event"] for e in plat.events("saad@example.pk")]
    # a missing or malformed token is the client's mistake, never put in a URL
    assert client.post("/api/local/refresh", json={}).status_code == 422
    assert client.post("/api/local/refresh", json={"refresh_token": 12}).status_code == 422
    assert client.post("/api/local/refresh", json={"refresh_token": "x" * 43}).status_code == 401


def test_two_refreshes_of_one_token_at_once_give_one_new_token(plat):
    _, account = _owner_company(plat)
    token = plat.new_refresh(account["id"])
    won, raced = [], []

    def go():
        try:
            got = plat.rotate_refresh(token)
        except plat.Renewed:
            raced.append(1)
        else:
            won.append(got)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1 and won[0][0]["id"] == account["id"] and len(raced) == 5          # the others were told, and the session goes on
    assert plat.rotate_refresh(won[0][1]) is not None


def test_the_tokens_of_ended_sessions_are_cleared_at_a_sign_in(plat, monkeypatch):
    _, account = _owner_company(plat)
    start = time.time()
    monkeypatch.setattr(plat, "_clock", lambda: start)
    old = plat.new_refresh(account["id"])
    plat.rotate_refresh(old)
    monkeypatch.setattr(plat, "_clock", lambda: start + plat.SESSION_DAYS * 86400 - 60)
    plat.new_refresh(account["id"])
    with closing(sqlite3.connect(plat.db_path())) as con:
        assert con.execute("SELECT COUNT(*) FROM refresh_tokens").fetchone()[0] == 3        # the first session has not ended yet
    monkeypatch.setattr(plat, "_clock", lambda: start + plat.SESSION_DAYS * 86400 + 1)
    plat.new_refresh(account["id"])
    with closing(sqlite3.connect(plat.db_path())) as con:
        assert con.execute("SELECT COUNT(*) FROM refresh_tokens").fetchone()[0] == 2        # its two tokens went; the second session and the new one stay


def test_a_refresh_token_ends_unused_after_its_hours_and_a_session_after_its_days(client, plat, monkeypatch):
    _owner_company(plat)
    start = time.time()
    s = _sign_in(client).json()
    monkeypatch.setattr(plat, "_clock", lambda: start + plat.REFRESH_HOURS * 3600 + 5)
    assert client.post("/api/local/refresh", json={"refresh_token": s["refresh_token"]}).status_code == 401
    monkeypatch.setattr(plat, "_clock", lambda: start)
    s = _sign_in(client).json()
    now = start
    for _ in range(plat.SESSION_DAYS * 3):                                       # used every 11 hours, it lasts the days of a session and no more
        now += 11 * 3600
        monkeypatch.setattr(plat, "_clock", lambda: now)
        r = client.post("/api/local/refresh", json={"refresh_token": s["refresh_token"]})
        if now - start >= plat.SESSION_DAYS * 86400:
            assert r.status_code == 401
            break
        assert r.status_code == 200
        s = r.json()
    else:
        pytest.fail("the session outlived its days")


def test_logout_ends_that_session_only_and_takes_the_token_in_its_body(client, plat):
    _owner_company(plat)
    one, two = _sign_in(client).json(), _sign_in(client).json()
    rotated = client.post("/api/local/refresh", json={"refresh_token": one["refresh_token"]}).json()
    r = client.post("/api/local/logout", json={"refresh_token": rotated["refresh_token"]})
    assert r.status_code == 200 and r.json() == {"signed_out": True}
    assert client.post("/api/local/refresh", json={"refresh_token": rotated["refresh_token"]}).status_code == 401
    assert client.post("/api/local/refresh", json={"refresh_token": two["refresh_token"]}).status_code == 200
    assert client.post("/api/local/logout", json={}).json() == {"signed_out": True}                       # nothing to end
    assert client.post("/api/local/logout", json={"refresh_token": "x" * 43}).json() == {"signed_out": True}
    assert client.post("/api/local/logout", json={"refresh_token": ["x"]}).status_code == 422


# ------------------------------------------------------------------------------------------------ the lockout (addendum item 13, R-235)

def test_five_wrong_passwords_from_one_address_stop_it_for_fifteen_minutes_from_the_fifth(client, plat, monkeypatch):
    _owner_company(plat)
    start = time.time()
    monkeypatch.setattr(plat, "_clock", lambda: start)
    for _ in range(auth.MAX_TRIES):
        assert _sign_in(client, password="not the password").status_code == 401
    r = _sign_in(client)
    assert r.status_code == 429 and r.headers["retry-after"] == str(auth.LOCK_MINUTES * 60) and "15 minutes" in r.json()["detail"]
    assert plat.lock_wait("saad@example.pk", "elsewhere") == 0                    # another address still signs in
    monkeypatch.setattr(plat, "_clock", lambda: start + 600)                      # ten minutes on: five are left
    r = _sign_in(client)
    assert r.status_code == 429 and r.headers["retry-after"] == "300" and "5 minutes" in r.json()["detail"]
    monkeypatch.setattr(plat, "_clock", lambda: start + auth.LOCK_MINUTES * 60 + 1)
    assert _sign_in(client).status_code == 200
    assert [e["event"] for e in plat.events("saad@example.pk")].count("sign-in refused") == auth.MAX_TRIES     # tries while stopped are not counted


def test_a_successful_sign_in_clears_the_count_of_its_address(client, plat):
    _owner_company(plat)
    for _ in range(auth.MAX_TRIES - 1):
        assert _sign_in(client, password="not the password").status_code == 401
    assert _sign_in(client).status_code == 200
    for _ in range(auth.MAX_TRIES - 1):
        assert _sign_in(client, password="not the password").status_code == 401
    assert _sign_in(client).status_code == 200                                    # eight wrong in a few minutes, but never five since a success


def test_a_forwarded_for_header_buys_no_tries_unless_the_server_trusts_its_proxy(client, plat, monkeypatch):
    _owner_company(plat)
    for n in range(auth.MAX_TRIES):
        assert _sign_in(client, password="not the password", **{"X-Forwarded-For": f"203.0.113.{n}"}).status_code == 401
    assert _sign_in(client, **{"X-Forwarded-For": "198.51.100.7"}).status_code == 429
    assert _sign_in(client, **{"Forwarded": "for=198.51.100.8"}).status_code == 429
    assert _sign_in(client).status_code == 200                                    # the connection's own address was never counted

    def request(host, headers):
        return types.SimpleNamespace(client=types.SimpleNamespace(host=host) if host else None, headers=headers)

    assert plat.client_address(request("10.0.0.5", {})) == "10.0.0.5"
    assert plat.client_address(request("203.0.113.9", {"x-forwarded-for": "203.0.113.9"})) == "forwarded"
    assert plat.client_address(request(None, {})) == ""
    monkeypatch.setenv("GRIDLYTICS_TRUST_PROXY", "1")                            # the server runs behind a proxy whose header uvicorn reads
    assert plat.client_address(request("203.0.113.9", {"x-forwarded-for": "203.0.113.9"})) == "203.0.113.9"


def test_the_lockout_counts_by_account_and_address_and_across_addresses_when_asked(plat, monkeypatch):
    start = time.time()
    monkeypatch.setattr(plat, "_clock", lambda: start)
    for n in range(4):
        plat.record("code refused", "founder", f"10.0.0.{n}")
    assert plat.lock_wait("founder", "10.0.0.1", refused="code refused") == 0
    assert plat.lock_wait("founder", None, refused="code refused", tries=4) == auth.LOCK_MINUTES * 60
    assert plat.lock_wait("founder", None, refused="code refused", tries=5) == 0


# ------------------------------------------------------------------------------------------------ the context (get_my_context's shape)

def test_the_context_has_the_shape_of_get_my_context(client, plat):
    company, account = _owner_company(plat)
    other = plat.create_company("Development", enabled=False, seats=2)
    plat.add_membership(account["id"], other["id"], "member", "pending")
    s = _sign_in(client).json()
    assert client.get("/api/local/context").status_code == 401
    r = client.get("/api/local/context", headers=_bearer(s))
    assert r.status_code == 200
    ctx = r.json()
    assert set(ctx) == {"profile", "memberships", "pending_invites_for_email"} and ctx["pending_invites_for_email"] == []
    p = ctx["profile"]
    assert p["id"] == account["id"] and p["email"] == "saad@example.pk" and p["name"] == "Saad Khan"
    assert p["first_name"] == "Saad" and p["last_name"] == "Khan" and p["is_super_admin"] is False and p["assignment_status"] == "none"
    assert {"avatar_url", "requested_company_name", "created_at", "updated_at"} <= set(p)
    by_company = {m["company"]["id"]: m for m in ctx["memberships"]}
    mine = by_company[company["id"]]
    assert set(mine) == {"id", "role", "status", "created_at", "activated_at", "company"}
    assert mine["role"] == "owner" and mine["status"] == "active" and mine["activated_at"]
    assert mine["company"] == {"id": company["id"], "name": "Acme Solar", "verified": True, "enabled": True, "seat_limit": 5}
    assert by_company[other["id"]]["status"] == "pending" and by_company[other["id"]]["company"]["enabled"] is False
    assert plat.context(str(uuid.uuid4())) is None
    # another company's member: neither context names the other's company
    elsewhere = plat.create_company("Elsewhere Energy", enabled=True)
    stranger = plat.create_account("sunia@example.pk", "Sunia   Ahmed", PASSWORD)
    plat.add_membership(stranger["id"], elsewhere["id"], "owner")
    theirs = client.get("/api/local/context", headers=_bearer(_sign_in(client, email="sunia@example.pk").json())).json()
    assert [m["company"]["id"] for m in theirs["memberships"]] == [elsewhere["id"]]
    assert (theirs["profile"]["first_name"], theirs["profile"]["last_name"]) == ("Sunia", "Ahmed")
    assert elsewhere["id"] not in {m["company"]["id"] for m in client.get("/api/local/context", headers=_bearer(s)).json()["memberships"]}
    # the identity the guard leaves must name an account and say when its token was made
    for claims in ({"sub": str(uuid.uuid4()), "iat": int(time.time())}, {"sub": account["id"]}):
        crafted = f"{_b64(b'{}')}.{_b64(json.dumps(claims).encode())}.x"           # the stand-in guard reads the claims unchecked
        assert client.get("/api/local/context", headers={"Authorization": f"Bearer {crafted}"}).status_code == 401, claims


def test_a_disabled_account_a_suspended_membership_and_a_company_switched_off(client, plat):
    company, account = _owner_company(plat)
    s = _sign_in(client).json()                                                   # every token minted before the change
    # a company switched off: still a member, and the context says it is off (stage B3 refuses it with company_disabled)
    plat.update_company(company["id"], enabled=False, disabled_reason="Not paid")
    m = client.get("/api/local/context", headers=_bearer(s)).json()["memberships"][0]
    assert m["status"] == "active" and m["company"]["enabled"] is False and plat.company(company["id"])["disabled_reason"] == "Not paid"
    assert plat.membership(account["id"], company["id"])["company"]["enabled"] is False
    # a suspended membership: the person is still signed in, and the membership says so
    plat.update_company(company["id"], enabled=True)
    plat.set_membership(account["id"], company["id"], status="suspended")
    assert client.get("/api/local/context", headers=_bearer(s)).json()["memberships"][0]["status"] == "suspended"
    s = client.post("/api/local/refresh", json={"refresh_token": s["refresh_token"]}).json()
    assert s["access_token"]
    # a disabled account: refused at once, its sessions ended, whatever token it holds
    plat.set_account_disabled(account["id"], True)
    assert client.get("/api/local/context", headers=_bearer(s)).status_code == 401
    assert client.post("/api/local/password", json={"current": PASSWORD, "new": "another long password"}, headers=_bearer(s)).status_code == 401
    assert client.post("/api/local/refresh", json={"refresh_token": s["refresh_token"]}).status_code == 401
    assert plat.signed_in(account["id"]) is None
    plat.set_account_disabled(account["id"], False)
    assert client.post("/api/local/refresh", json={"refresh_token": s["refresh_token"]}).status_code == 401      # ended, not paused
    assert plat.account(account["id"])["sessions_from"] >= _claims(s["access_token"])["iat"]                      # its access tokens too
    assert client.get("/api/local/context", headers=_bearer(_sign_in(client).json())).status_code == 200


# ------------------------------------------------------------------------------------------------ changing one's own password (R-129, carried over)

def test_a_person_changes_their_own_password_and_their_other_sessions_end(client, plat, monkeypatch):
    _, account = _owner_company(plat)
    me, other = _sign_in(client).json(), _sign_in(client).json()
    new = "a brand new password"

    def change(body, session=me):
        return client.post("/api/local/password", json=body, headers=_bearer(session) if session else {})

    assert change({"current": PASSWORD, "new": new}, session=None).status_code == 401
    for body, words in (({"current": "not it at all", "new": new}, "current password is wrong"), ({"current": PASSWORD, "new": "short"}, "at least 10"),
                        ({"current": PASSWORD, "new": " " * 12}, "spaces"), ({"current": PASSWORD, "new": PASSWORD}, "different"),
                        ({"new": new}, "current password"), ({"current": PASSWORD, "new": new, "email": "x@y.z"}, "current password and the new one")):
        r = change(body)
        assert r.status_code == 422 and words in r.json()["detail"], (body, r.json())
    assert _sign_in(client).status_code == 200                                    # nothing changed
    started = int(time.time())
    r = change({"current": PASSWORD, "new": new})
    assert r.status_code == 200
    out = r.json()
    assert out["changed"] is True and {"access_token", "refresh_token", "expires_at", "user"} <= set(out)        # this device stays signed in
    assert client.post("/api/local/refresh", json={"refresh_token": other["refresh_token"]}).status_code == 401
    assert client.post("/api/local/refresh", json={"refresh_token": me["refresh_token"]}).status_code == 401
    assert client.post("/api/local/refresh", json={"refresh_token": out["refresh_token"]}).status_code == 200
    assert _sign_in(client).status_code == 401 and _sign_in(client, password=new).status_code == 200
    # an access token minted before the change is no longer taken (stage B3 asks the same with signed_in)
    assert plat.account(account["id"])["sessions_from"] >= started
    assert plat.signed_in(account["id"], iat=started - 1) is None and plat.signed_in(account["id"], iat=_claims(out["access_token"])["iat"])
    events = [e["event"] for e in plat.events("saad@example.pk")]
    assert "changed their password" in events
    refused = [e for e in plat.events("saad@example.pk") if e["event"] == "sign-in refused"]
    assert refused and refused[0]["detail"] == {"to": "change the password"}


def test_wrong_current_passwords_are_counted_like_wrong_sign_ins(client, plat):
    _owner_company(plat)
    me = _sign_in(client).json()
    for _ in range(auth.MAX_TRIES):
        r = client.post("/api/local/password", json={"current": "guess guess guess", "new": "a brand new password"}, headers=_bearer(me))
        assert r.status_code == 422
    r = client.post("/api/local/password", json={"current": PASSWORD, "new": "a brand new password"}, headers=_bearer(me))
    assert r.status_code == 429 and _sign_in(client).status_code == 429             # a session left open cannot be used to guess the password


# ------------------------------------------------------------------------------------------------ the founder's audit and the index tables

def test_the_founder_audit_is_kept_in_the_registry(plat):
    plat.founder_audit("made a company", "Acme Solar", {"seats": 5})
    plat.founder_audit("switched Gridlytics on", "Acme Solar")
    rows = plat.list_founder_audit()
    assert [r["action"] for r in rows] == ["switched Gridlytics on", "made a company"]
    assert rows[1]["actor"] == "founder" and rows[1]["detail"] == {"seats": 5} and rows[1]["target"] == "Acme Solar"
    assert [r["action"] for r in plat.list_founder_audit(limit=1, offset=1)] == ["made a company"]


def test_share_links_and_api_keys_name_their_company_through_the_index_tables(plat):
    a, b = plat.create_company("A"), plat.create_company("B")
    for table in plat.INDEX_TABLES:
        plat.index_add(table, "h1", a["id"])
        plat.index_add(table, "h2", b["id"])
        assert plat.index_find(table, "h1") == a["id"] and plat.index_find(table, "h2") == b["id"] and plat.index_find(table, "h3") is None
        plat.index_add(table, "h1", a["id"])                                       # written again: nothing changes
        with pytest.raises(ValueError):
            plat.index_add(table, "h1", b["id"])                                   # a hash names one company only
        assert plat.index_remove(table, "h1") is True and plat.index_find(table, "h1") is None
        assert plat.index_remove(table, "h1") is False
    assert plat.index_find("share_index", "h2") == b["id"] and plat.index_find("api_key_index", "h2") == b["id"]
    with pytest.raises(ValueError):
        plat.index_add("accounts", "h1", a["id"])
    with pytest.raises(ValueError):
        plat.index_add("share_index", "h9", str(uuid.uuid4()))                     # an unknown company


# ------------------------------------------------------------------------------------------------ not mounted yet; the git-ignored files

def test_the_local_routes_are_mounted_through_program_routes(issued):
    from gridlytics import routes_local
    mine = {(r.path, m) for r in routes_local.router.routes for m in r.methods}
    assert mine == {("/api/local/session", "POST"), ("/api/local/refresh", "POST"), ("/api/local/logout", "POST"), ("/api/local/context", "GET"),
                    ("/api/local/password", "POST")}
    # stage B4a mounts them, in local mode (the mode of the tests), as the program's other routes are
    assert {(r.path, m) for r in api.app.routes if getattr(r, "path", "").startswith("/api/local") for m in r.methods} == mine
    assert routes_local in api.PROGRAM_ROUTES


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs git and the repository")
def test_the_registrys_secret_the_company_files_and_the_demo_list_are_ignored_by_git():
    for path in ("data/companies/0b5c1c35-7f4b-4a51-9b55-0f4b1e9c2a10.db", "data/companies/backup/x.db", "data/companies/x.db-wal", "data/platform.secret",
                 "data/platform.db", "demo/DEMO_ACCOUNTS.local.md"):
        assert subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=REPO).returncode == 0, path
    assert subprocess.run(["git", "check-ignore", "-q", "--no-index", "demo/users.template.csv"], cwd=REPO).returncode == 1
