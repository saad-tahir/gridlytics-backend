"""Round 7 (the end-to-end audit), the founder console: its session ends on the server, a short session secret is refused, and a company's backups are
listed, copied, pruned and restored as one set (workspace, telemetry and signing keys), with a restore script that exists. One test per audit finding;
the finding's id is in each test's docstring."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import identities
from gridlytics import backups, delivery, founder, identity, sites, store, store_members, telemetry
from gridlytics import platform as platformdb
import test_companies as tc
from test_companies import data, supa  # noqa: F401  (fixtures: supabase mode with a stand-in rpc/get_my_context)
from test_founder import (OWNER, PASSWORD, SESSION_SECRET, _audit, _client, _code, _company, _console, _ok, _sentence, clock, fdb)  # noqa: F401  (fixtures)

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "restore_backup.py"


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


# ================================================================== the backups: one set per copy

def _company_with_data(c) -> tuple[str, Path]:
    """A company whose file holds a site, its telemetry a minute of data, and its keys folder a signing key."""
    cid = _company(c)["company"]["id"]
    path = platformdb.company_file(cid)
    with store.use_company(path):
        site = sites.create({"name": "Mill A"}, "test")["id"]
        telemetry.write_rows([(site, 1_800_000_000, 12_000.0)], columns=("site_id", "ts", "pv_power_w"))
        delivery.public_key(site)
    return cid, path


def _site_names(path: Path) -> list[str]:
    with store.use_company(path):
        return [s["name"] for s in sites.listing()]


def _minutes(path: Path) -> int:
    with closing(sqlite3.connect(store.telemetry_path(path))) as con:
        return con.execute("SELECT COUNT(*) FROM minute").fetchone()[0]


def _keys(path: Path) -> list[str]:
    folder = platformdb.data_dir() / "keys" / path.stem
    return sorted(f.name for f in folder.iterdir()) if folder.exists() else []


def test_a_copy_lists_its_telemetry_its_keys_and_their_whole_size_and_is_pruned_as_one(fdb):
    """founder-console-12: the list hid each copy's telemetry file (and its size), and the telemetry copies were never pruned."""
    c = _console()
    cid, path = _company_with_data(c)
    listed = _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))["backups"]
    assert len(listed) == 1
    one = listed[0]
    folder = backups.folder(path)
    workspace, tele = folder / one["name"], folder / one["telemetry"]
    assert one["telemetry"] == one["name"].replace(f"{cid}-", f"{cid}-telemetry-", 1) and tele.is_file()
    assert one["workspace_bytes"] == workspace.stat().st_size and one["telemetry_bytes"] == tele.stat().st_size
    assert one["bytes"] == one["workspace_bytes"] + one["telemetry_bytes"]
    assert one["keys"] == 1 and len(_keys(path)) == 1
    assert datetime.fromisoformat(one["modified"]).utcoffset().total_seconds() == 0
    # the manual copies beyond KEEP_MANUAL go with their telemetry and their keys
    for _ in range(backups.KEEP_MANUAL):
        _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))
    left = {f.name for f in folder.iterdir()}
    assert one["name"] not in left and one["telemetry"] not in left and not any(n.endswith(one["name"][len(cid) + 1:-3]) for n in left)
    assert len([n for n in left if "-telemetry-" in n]) == backups.KEEP_MANUAL
    assert len([n for n in left if "-keys-" in n]) == backups.KEEP_MANUAL


def test_the_backup_list_says_its_times_in_utc(fdb):
    """frontend-states-formats-17: backups.list_backups wrote the server's local time with no zone, which a browser reads as UTC."""
    c = _console()
    cid, path = _company_with_data(c)
    _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))
    with store.use_company(path):
        listed = backups.list_backups()
    assert listed and all(datetime.fromisoformat(b["modified"]).utcoffset().total_seconds() == 0 for b in listed)


def test_a_restore_puts_back_the_workspace_its_telemetry_and_its_keys_together(fdb):
    """founder-console-01: backups.restore put back only the workspace file, leaving telemetry (and the signing keys) out of step with it."""
    c = _console()
    cid, path = _company_with_data(c)
    copy = _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))["made"]
    key_before = _keys(path)
    # after the copy: another site, more telemetry, and the signing key lost (a folder moved without it)
    with store.use_company(path):
        site = sites.create({"name": "Mill B"}, "test")["id"]
        telemetry.write_rows([(site, 1_800_000_060 + 60 * i, 9_000.0) for i in range(5)], columns=("site_id", "ts", "pv_power_w"))
    for f in (platformdb.data_dir() / "keys" / path.stem).iterdir():
        f.unlink()
    assert _site_names(path) == ["Mill A", "Mill B"] or sorted(_site_names(path)) == ["Mill A", "Mill B"]
    assert _minutes(path) == 6 and _keys(path) == []
    with store.use_company(path):
        done = backups.restore(copy)
    assert _site_names(path) == ["Mill A"] and _minutes(path) == 1 and _keys(path) == key_before
    assert done["workspace"] == copy and done["telemetry"] == copy.replace(f"{cid}-", f"{cid}-telemetry-", 1) and done["keys"] == 1
    # the files as they were are kept first, as one set too
    with store.use_company(path):
        points = [b for b in backups.list_backups() if b["kind"] == "restore point"]
    assert [p["name"] for p in points] == [done["restore_point"]] and points[0]["telemetry"]
    with closing(sqlite3.connect(backups.folder(path) / points[0]["telemetry"])) as con:
        assert con.execute("SELECT COUNT(*) FROM minute").fetchone()[0] == 6
    # a telemetry copy, or a name of no copy, is not restored
    with store.use_company(path):
        for name in (done["telemetry"], "nothing.db", "../platform.db"):
            with pytest.raises(FileNotFoundError):
                backups.restore(name)


def test_a_copy_made_with_no_telemetry_restores_an_empty_telemetry_file(fdb):
    """founder-console-01: a copy taken before the company had telemetry leaves no later minute behind when it is put back."""
    c = _console()
    cid = _company(c)["company"]["id"]
    path = platformdb.company_file(cid)
    with store.use_company(path):
        site = sites.create({"name": "Mill A"}, "test")["id"]
    copy = _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))["backups"][0]
    assert copy["telemetry"] is None and copy["telemetry_bytes"] == 0 and copy["keys"] == 0
    with store.use_company(path):
        telemetry.write_rows([(site, 1_800_000_000, 1.0)], columns=("site_id", "ts", "pv_power_w"))
        backups.restore(copy["name"])
    assert _minutes(path) == 0


def _script(data: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GRIDLYTICS_DATA_DIR": str(data)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180)


def test_the_restore_script_the_backups_page_names_exists_and_restores_a_company(fdb):
    """founder-console-01: the Backups page said to run scripts/restore_backup.py, which did not exist."""
    assert SCRIPT.is_file()
    c = _console()
    cid, path = _company_with_data(c)
    made = _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))
    copy, when = made["made"], made["backups"][0]["modified"]
    with store.use_company(path):
        sites.create({"name": "Mill B"}, "test")
    data = platformdb.data_dir()
    listed = _script(data, "--company", cid)
    assert listed.returncode == 0, listed.stderr
    assert copy in listed.stdout and "Pakistan time" in listed.stdout and "MB" in listed.stdout
    pkt = datetime.fromisoformat(when).astimezone(timezone(timedelta(hours=5)))          # the copy's time in Pakistan time, as the page says it
    assert pkt.strftime("%Y-%m-%d %H:%M") in listed.stdout
    companies = _script(data)
    assert companies.returncode == 0 and cid in companies.stdout and "Boxlytics Internal" in companies.stdout
    done = _script(data, "--company", "Boxlytics Internal", copy)
    assert done.returncode == 0, done.stderr
    assert "telemetry" in done.stdout and "restore point" in done.stdout
    assert _site_names(path) == ["Mill A"] and _minutes(path) == 1
    for args in (("--company", cid, "no-such-copy.db"), ("--company", "No such company")):
        refused = _script(data, *args)
        assert refused.returncode == 2 and refused.stderr.strip() and "Traceback" not in refused.stderr, refused.stderr


def test_the_restore_script_refuses_while_the_launcher_holds_the_data_folder(fdb):
    """founder-console-01: restoring under a running server would put files back under its feet; the launcher's lock on the folder refuses it."""
    c = _console()
    cid, path = _company_with_data(c)
    copy = _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))["made"]
    data = platformdb.data_dir()
    key = hashlib.sha256(os.path.normcase(str(data.resolve())).encode("utf-8")).hexdigest()[:20]
    lock = open(Path(tempfile.gettempdir()) / f"gridlytics-data-{key}.lock", "a+b")
    try:
        try:                                                                       # as the launcher takes it (start_local.hold)
            import msvcrt
        except ImportError:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        refused = _script(data, "--company", cid, copy)
    finally:
        lock.close()
    assert refused.returncode == 2 and "stop" in refused.stderr.lower(), refused.stderr


# ================================================================== company and user management (batch 2)

def _token(who: dict) -> str:
    return identity.issue_local(who["id"], who["email"], who["name"], 60)


def _as(who: dict) -> TestClient:
    """A client signed in as the person, as their page sends its requests."""
    c = _client()
    c.headers.update(identities.headers(_token(who)))
    return c


def _person(c, email: str, name: str, cid: str, company_role: str = "member") -> dict:
    # the person keeps the first password (one_time false), as these tests work with it; a one-time first password is test_fix_r7_auth_security's
    return _ok(c.post("/api/founder/users", json={"email": email, "name": name, "password": f"{name.split()[0].lower()} first password",
                                                   "company_id": cid, "company_role": company_role, "one_time": False}), 201)["user"]


def _role_on_users(c, user_id: str, cid: str) -> str | None:
    """The Gridlytics role the founder's Users page shows for the person in the company."""
    user = next(u for u in _ok(c.get("/api/founder/users"))["users"] if u["id"] == user_id)
    return next(m for m in user["memberships"] if m["company_id"] == cid)["gridlytics_role"]


def test_the_founders_answers_carry_the_gridlytics_role_and_not_the_lifted_name(fdb):
    """founder-console-18: every membership and member carried 'solarlytics_role' beside 'gridlytics_role', kept for a console page that no longer read it."""
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    _ok(_as(owner).get("/api/sites"))
    members = _ok(c.get(f"/api/founder/companies/{cid}/members"))["members"]
    users = _ok(c.get("/api/founder/users"))["users"]
    assert members[0]["gridlytics_role"] == "admin" and users[0]["memberships"][0]["gridlytics_role"] == "admin"
    assert "solarlytics" not in json.dumps(members) + json.dumps(users)


def test_a_company_admin_is_a_gridlytics_administrator_and_a_role_set_in_gridlytics_stays(fdb):
    """founder-console-04: the console offered the company role Admin and said the person 'is admin', but it gave nothing: auth/state still said viewer,
    with manage_members false. A company admin is now an administrator in Gridlytics, as its owner is (not pinned); a member starts as a viewer."""
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    boss = _as(owner)
    _ok(boss.get("/api/sites"))
    maaz = _person(c, "maaz@meow.test", "Maaz Ali", cid, "admin")
    # before the first sign-in, the company's own Users list says what the company role gives
    assert {u["email"]: u["role"] for u in _ok(boss.get("/api/users"))}["maaz@meow.test"] == "admin"
    state = _ok(_as(maaz).get("/api/auth/state"))
    assert state["user"]["role"] == "admin" and state["company_role"] == "admin" and state["can"]["manage_members"] is True
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "member"}))
    state = _ok(_as(maaz).get("/api/auth/state"))
    assert state["user"]["role"] == "viewer" and state["can"]["manage_members"] is False
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "admin"}))
    assert _ok(_as(maaz).get("/api/auth/state"))["user"]["role"] == "admin"
    # a role an administrator set in Gridlytics stays whatever the company role
    sunia = _person(c, "sunia@meow.test", "Sunia Ahmed", cid)
    _ok(_as(sunia).get("/api/sites"))
    _ok(boss.put(f"/api/users/{sunia['id']}", json={"role": "operator"}))
    _ok(c.patch(f"/api/founder/users/{sunia['id']}/memberships/{cid}", json={"company_role": "admin"}))
    assert _ok(_as(sunia).get("/api/auth/state"))["user"]["role"] == "operator"
    # the owner who hands the ownership over and becomes an admin stays an administrator, no longer pinned
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "owner"}))
    _ok(c.patch(f"/api/founder/users/{owner['id']}/memberships/{cid}", json={"company_role": "admin"}))
    me = next(u for u in _ok(_as(owner).get("/api/users")) if u["id"] == owner["id"])
    assert (me["role"], me["role_source"], me["company_role"]) == ("admin", "default", "admin")


def test_the_users_page_shows_the_gridlytics_role_a_company_role_change_gives_at_once(fdb):
    """founder-console-16: after the founder made an owner a member again, Users still read 'Administrator' until the person's next request, because the
    member row in the company's file followed the membership only then. The founder's change now reaches the row at once."""
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    _ok(_as(owner).get("/api/sites"))
    maaz = _person(c, "maaz@meow.test", "Maaz Ali", cid)
    _ok(_as(maaz).get("/api/sites"))
    assert _role_on_users(c, maaz["id"], cid) == "viewer"
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "owner"}))
    assert _role_on_users(c, maaz["id"], cid) == "admin"
    answer = _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "member"}))["membership"]
    assert answer["company_role"] == "member" and answer["gridlytics_role"] == "viewer"                 # no request of his in between
    assert _role_on_users(c, maaz["id"], cid) == "viewer"
    members = {m["user_id"]: m for m in _ok(c.get(f"/api/founder/companies/{cid}/members"))["members"]}
    assert members[maaz["id"]]["gridlytics_role"] == "viewer"
    # a membership added again with another company role is followed at once too
    _ok(c.delete(f"/api/founder/users/{maaz['id']}/memberships/{cid}"))
    added = _ok(c.post(f"/api/founder/users/{maaz['id']}/memberships", json={"company_id": cid, "company_role": "admin"}), 201)["membership"]
    assert added["gridlytics_role"] == "admin" and _role_on_users(c, maaz["id"], cid) == "admin"
    # someone who has not signed in yet has no row: the answer says so (null), and nothing is made
    sunia = _person(c, "sunia@meow.test", "Sunia Ahmed", cid)
    answer = _ok(c.patch(f"/api/founder/users/{sunia['id']}/memberships/{cid}", json={"company_role": "admin"}))["membership"]
    assert answer["gridlytics_role"] is None and _role_on_users(c, sunia["id"], cid) is None


def test_disabling_a_companys_only_owner_is_refused_unless_the_founder_says_so_and_the_list_shows_it(fdb):
    """founder-console-10: the founder could disable a company's only owner with no warning (removing, demoting or suspending that owner is refused), and
    Companies still showed that owner, unmarked, with '1 admin'."""
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    _ok(_as(owner).get("/api/sites"))
    r = c.patch(f"/api/founder/users/{owner['id']}", json={"disabled": True})
    assert r.status_code == 409 and _sentence(r), r.text
    assert "only owner of Boxlytics Internal" in r.json()["detail"] and "leave_without_owner" not in r.json()["detail"]
    assert platformdb.account(owner["id"])["disabled"] is False
    _ok(c.patch(f"/api/founder/users/{owner['id']}", json={"name": "Saad K."}))                  # any other change is not held up
    r = c.patch(f"/api/founder/users/{owner['id']}", json={"disabled": True, "leave_without_owner": "yes"})
    assert r.status_code == 422 and _sentence(r)
    # said knowingly: the account is disabled, the audit says which company is left without an owner, and Companies shows it
    off = _ok(c.patch(f"/api/founder/users/{owner['id']}", json={"disabled": True, "leave_without_owner": True}))["user"]
    assert off["disabled"] is True
    assert _audit()[0]["action"] == "changed an account" and _audit()[0]["detail"]["left_without_owner"] == ["Boxlytics Internal"]
    box = _ok(c.get("/api/founder/companies"))["companies"][0]
    assert box["owner"] == {"id": owner["id"], "email": OWNER["email"], "name": "Saad K.", "disabled": True} and box["admins"] == 0
    _ok(c.patch(f"/api/founder/users/{owner['id']}", json={"disabled": False}))
    box = _ok(c.get("/api/founder/companies"))["companies"][0]
    assert box["owner"]["disabled"] is False and box["admins"] == 1
    # with a second owner who can sign in (a hand-over), disabling one is not refused, and the list shows the owner who can sign in
    maaz = _person(c, "maaz@meow.test", "Maaz Ali", cid, "owner")
    _ok(c.patch(f"/api/founder/users/{owner['id']}", json={"disabled": True}))
    box = _ok(c.get("/api/founder/companies"))["companies"][0]
    assert box["owner"]["id"] == maaz["id"] and box["owner"]["disabled"] is False
    # but not when that second owner's account is disabled too
    r = c.patch(f"/api/founder/users/{maaz['id']}", json={"disabled": True})
    assert r.status_code == 409 and "Boxlytics Internal" in r.json()["detail"]
    # a member is disabled without a word
    sunia = _person(c, "sunia@meow.test", "Sunia Ahmed", cid)
    _ok(c.patch(f"/api/founder/users/{sunia['id']}", json={"disabled": True}))


def test_the_companies_list_counts_the_sites_a_switch_off_takes_out_of_closed_loop_control(fdb):
    """founder-console-05: the Switch off dialog could not say that switching a company off ends its sites' control; the list now counts the sites under
    closed-loop control (capable and allowed), which the dialog names."""
    c = _console()
    cid = _company(c)["company"]["id"]
    assert _ok(c.get("/api/founder/companies"))["companies"][0]["sites_closed_loop"] == 0          # no file yet: nothing, and none is made
    with store.use_company(platformdb.company_file(cid)):
        steered, capable, allowed, gone = (sites.create({"name": n}, "test")["id"] for n in ("Mill A", "Mill B", "Mill C", "Mill D"))
        for site in (steered, capable, gone):
            sites.set_tier_capable(site, "closed_loop")
        for site in (steered, allowed, gone):
            sites.set_tier_allowed(site, "closed_loop", "test")
        sites.delete(gone, "test")
    box = _ok(c.get("/api/founder/companies"))["companies"][0]
    assert box["sites"] == 3 and box["sites_closed_loop"] == 1
    assert _ok(c.post(f"/api/founder/companies/{cid}/access", json={"enabled": False}))["company"]["sites_closed_loop"] == 1


def test_a_company_is_archived_with_a_last_copy_and_brought_back(fdb):
    """founder-console-13: a test company or a departed customer could not be removed, only switched off, and stayed in every list and count for good.
    Archiving switches it off, takes a last copy of its files, and leaves it out of the overview's figures; it is listed as archived (the page hides it
    until asked), cannot be switched on while archived, and can be brought back. Nothing of its data is deleted."""
    c = _console()
    cid, path = _company_with_data(c)
    owner = platformdb.account_by_email(OWNER["email"])
    _company(c, "Development", {"email": "dev@localhost", "name": "Local Tester", "password": "dev first password 1"})
    for body in ({}, {"archived": "yes"}, {"archived": True, "note": "x"}):
        r = c.post(f"/api/founder/companies/{cid}/archive", json=body)
        assert r.status_code == 422 and _sentence(r), (body, r.text)
    assert c.post(f"/api/founder/companies/{uuid.uuid4()}/archive", json={"archived": True}).status_code == 404
    done = _ok(c.post(f"/api/founder/companies/{cid}/archive", json={"archived": True}))
    company = done["company"]
    assert company["archived"] is True and company["enabled"] is False and company["disabled_reason"] == "Archived"
    copies = _ok(c.get(f"/api/founder/companies/{cid}/backups"))["backups"]
    assert done["made"] == copies[0]["name"] and copies[0]["kind"] == "before archiving" and copies[0]["telemetry"] and copies[0]["keys"] == 1
    assert _site_names(path) == ["Mill A"]                                                    # nothing of its data is gone
    r = _as(owner).get("/api/sites")
    assert r.status_code == 403 and r.json()["code"] == "company_disabled"
    r = c.post(f"/api/founder/companies/{cid}/access", json={"enabled": True})
    assert r.status_code == 409 and "archive" in r.json()["detail"] and _sentence(r)
    rows = {x["name"]: x for x in _ok(c.get("/api/founder/companies"))["companies"]}
    assert rows["Boxlytics Internal"]["archived"] is True and rows["Development"]["archived"] is False
    view = _ok(c.get("/api/founder/overview"))
    assert view["companies"] == {"total": 1, "on": 1, "off": 0, "archived": 1}
    # archiving it again changes nothing and makes no second copy
    assert _ok(c.post(f"/api/founder/companies/{cid}/archive", json={"archived": True}))["made"] is None
    back = _ok(c.post(f"/api/founder/companies/{cid}/archive", json={"archived": False}))["company"]
    assert back["archived"] is False and back["enabled"] is False
    _ok(c.post(f"/api/founder/companies/{cid}/access", json={"enabled": True}))
    _ok(_as(owner).get("/api/sites"))
    actions = [e["action"] for e in _audit()]
    assert actions.index("brought a company back from the archive") < actions.index("archived a company")
    assert actions.count("archived a company") == 1
    assert _audit()[actions.index("archived a company")]["detail"] == {"switched_off": True, "copy": done["made"]}


def test_an_account_is_archived_disabled_with_its_seats_freed_and_brought_back(fdb):
    """founder-console-13: a departed person's account could only be disabled, and kept its seats and its place in every list. Archiving disables it,
    frees its seats in its companies and leaves it out of the overview's figures; it cannot be enabled while archived, and can be brought back."""
    c = _console()
    made = _company(c, seats=2)
    cid, owner = made["company"]["id"], made["owner"]
    _ok(_as(owner).get("/api/sites"))
    sunia = _person(c, "sunia@meow.test", "Sunia Ahmed", cid)
    _ok(_as(sunia).get("/api/sites"))
    assert _ok(c.get("/api/founder/companies"))["companies"][0]["seats_taken"] == 2
    for body in ({}, {"archived": 1}, {"archived": True, "leave_without_owner": "yes"}, {"archived": True, "name": "x"}):
        r = c.post(f"/api/founder/users/{sunia['id']}/archive", json=body)
        assert r.status_code == 422 and _sentence(r), (body, r.text)
    assert c.post(f"/api/founder/users/{uuid.uuid4()}/archive", json={"archived": True}).status_code == 404
    user = _ok(c.post(f"/api/founder/users/{sunia['id']}/archive", json={"archived": True}))["user"]
    assert user["archived"] is True and user["disabled"] is True
    assert _as(sunia).get("/api/sites").status_code == 401
    assert _ok(c.get("/api/founder/companies"))["companies"][0]["seats_taken"] == 1                  # her seat is free
    with store.use_company(platformdb.company_file(cid)):
        assert store_members.member(sunia["id"])["disabled_reason"] == "account archived"
    r = c.patch(f"/api/founder/users/{sunia['id']}", json={"disabled": False})
    assert r.status_code == 409 and "archive" in r.json()["detail"] and _sentence(r)
    view = _ok(c.get("/api/founder/overview"))
    assert view["accounts"] == {"total": 1, "disabled": 0, "archived": 1} and view["memberships"]["active"] == 1
    users = {u["email"]: u for u in _ok(c.get("/api/founder/users"))["users"]}
    assert users["sunia@meow.test"]["archived"] is True and users[OWNER["email"]]["archived"] is False
    # the company's only owner is not archived without the founder saying so, as for disabling
    r = c.post(f"/api/founder/users/{owner['id']}/archive", json={"archived": True})
    assert r.status_code == 409 and "only owner" in r.json()["detail"]
    # brought back: still disabled until enabled, its rows back under the seat rule; enabled, she signs in again and takes a seat
    back = _ok(c.post(f"/api/founder/users/{sunia['id']}/archive", json={"archived": False}))["user"]
    assert back["archived"] is False and back["disabled"] is True
    with store.use_company(platformdb.company_file(cid)):
        assert store_members.member(sunia["id"])["disabled_reason"] == "no seat"
    _ok(c.patch(f"/api/founder/users/{sunia['id']}", json={"disabled": False}))
    _ok(_client().post("/api/local/session", json={"email": "sunia@meow.test", "password": "sunia first password"}))
    actions = [e["action"] for e in _audit()]
    assert actions[:3] == ["changed an account", "brought an account back from the archive", "archived an account"]


# ================================================================== batch 3: the console's words

def _plan_row(con, site: str, at: datetime) -> None:
    t = at.isoformat(timespec="seconds")
    con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status) VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, site, t, t, t, t, "a", "active"))


def test_the_console_has_no_commercial_plan_and_counts_the_dispatch_plans_of_the_sites_in_use(fdb):
    """founder-console-11: Gridlytics v1 has no commercial plans, yet every company carried a 'plan' that always read 'No commercial plan in v1', and the
    overview counted companies by it ('Plans due', 'with no plan') beside the dispatch plans issued. Those plans also counted the plans of deleted sites,
    which 'Sites' leaves out."""
    c = _console()
    cid = _company(c)["company"]["id"]
    now = datetime.now(timezone.utc)
    with store.use_company(platformdb.company_file(cid)):
        kept, gone = (sites.create({"name": n}, "test")["id"] for n in ("Mill A", "Mill B"))
        with store.connect() as con:
            for site in (kept, gone):
                _plan_row(con, site, now - timedelta(minutes=5))
                _plan_row(con, site, now - timedelta(days=40))
        sites.delete(gone, "test")
    box = _ok(c.get("/api/founder/companies"))["companies"][0]
    assert "plan" not in box and box["sites"] == 1 and box["plans_issued"] == 2
    view = _ok(c.get("/api/founder/overview"))
    assert "plans" not in view
    assert view["activity"]["sites"] == 1 and view["activity"]["plans_30"] == 1
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    assert view["activity"]["plans_month"] == int(now - timedelta(minutes=5) >= month)


# ================================================================== batch 4 (late): membership states, the owner-applied Supabase mode


def test_a_suspended_or_pending_membership_is_told_its_state_and_who_can_change_it(fdb):
    """parity-supabase-auth-09: a membership the founder suspended, or left pending, was told 'You are not a member of that company in Gridlytics', and a
    person with only that company 'Your account is not set up for Gridlytics'. The refusal now names the state and who changes it; its code stays, so the
    page reads the person's companies again as before."""
    c = _console()
    nid = _company(c, "Northwind Solar")["company"]["id"]
    sid = _company(c, "Southwind Energy", owner={"email": "olga@south.test", "name": "Olga Shah", "password": "olga first password"})["company"]["id"]
    maaz = _person(c, "maaz@meow.test", "Maaz Ali", nid)
    _ok(c.post(f"/api/founder/users/{maaz['id']}/memberships", json={"company_id": sid, "company_role": "member"}), 201)
    me = _as(maaz)
    assert _ok(me.get("/api/platform/org", headers={"X-Gridlytics-Company": sid}))["company"]["id"] == sid
    for status, said in (("suspended", "is suspended: its owner can ask us to make it active again"),
                         ("pending", "is not active yet: its owner can ask us to make it active")):
        _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{sid}", json={"status": status}))
        r = me.get("/api/platform/org", headers={"X-Gridlytics-Company": sid})
        assert (r.status_code, r.json()["code"]) == (403, "not_a_member"), r.text
        assert r.json()["detail"] == f"Your membership of Southwind Energy {said}"
        # with no company asked for, the one membership still active opens
        assert _ok(me.get("/api/platform/org"))["company"]["id"] == nid
    # a company that is not theirs at all is still 'not a member'
    r = me.get("/api/platform/org", headers={"X-Gridlytics-Company": str(uuid.uuid4())})
    assert r.json()["detail"] == "You are not a member of that company in Gridlytics; choose one of yours"
    # with no membership active, the person is told their memberships' states, not that their account is not set up
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{nid}", json={"status": "suspended"}))
    r = me.get("/api/platform/org")
    assert (r.status_code, r.json()["code"]) == (403, "no_company"), r.text
    assert r.json()["detail"] == ("Your membership of Northwind Solar is suspended and your membership of Southwind Energy is not active yet: their owners "
                                  "can ask us to make them active")
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{sid}", json={"status": "suspended"}))
    assert me.get("/api/platform/org").json()["detail"] == ("Your memberships of Northwind Solar and Southwind Energy are suspended: their owners can ask us "
                                                           "to make them active again")
    # removed, a membership is gone: then the account is not set up
    for cid in (nid, sid):
        _ok(c.delete(f"/api/founder/users/{maaz['id']}/memberships/{cid}"))
    assert me.get("/api/platform/org").json()["detail"] == "Your account is not set up for Gridlytics. Ask us to add you"


def test_supabase_mode_names_a_pending_or_suspended_membership_too(supa):
    """parity-supabase-auth-09, supabase mode: get_my_context gives every membership with its status, so the same sentences are said from the project's
    context; a company the project has not verified is still 'not a member'."""
    token = tc._supa_token()
    assert tc._refused(403, "not_a_member", token, tc.PENDING).detail == ("Your membership of Pending Panels is not active yet: its owner can ask us to make "
                                                                           "it active")
    assert tc._refused(403, "not_a_member", token, tc.SUSPENDED).detail == ("Your membership of Suspended Systems is suspended: its owner can ask us to make "
                                                                             "it active again")
    assert tc._refused(403, "not_a_member", token, tc.UNVERIFIED).detail == tc.companies.NOT_A_MEMBER
    # the project's context without a company name: the registry's name is said
    ctx = copy.deepcopy(tc.FIXTURE)
    ctx["memberships"] = [{**tc._membership(tc.SUSPENDED, "", status="suspended")}]
    supa.rpc.answer = ctx
    supa.clock[0] += 120
    assert tc._refused(403, "no_company", tc._supa_token()).detail == ("Your membership of Suspended Systems is suspended: its owner can ask us to make it "
                                                                        "active again")


def _reports(path: Path) -> dict[str, bytes]:
    folder = platformdb.data_dir() / "reports" / path.stem
    return {f.name: f.read_bytes() for f in sorted(folder.iterdir())} if folder.exists() else {}


def test_a_copy_takes_the_companys_reports_and_a_restore_puts_them_back(fdb):
    """parity-architecture-11 and -16 (the backup half, the founder console's): the reports a company made (data folder's reports/<company>) were in no
    copy, so a restore on another machine left every stored report's download broken. A copy now takes them beside the signing keys, the list counts them
    and their size, the copies beyond those kept go with them, and a restore (and scripts/restore_backup.py) puts back those the folder lacks."""
    c = _console()
    cid, path = _company_with_data(c)
    made = platformdb.data_dir() / "reports" / path.stem
    made.mkdir(parents=True)
    (made / "r1.pdf").write_bytes(b"%PDF-1.4 made up")
    (made / "r1.csv").write_text("day,kwh\n2026-11-01,12\n", encoding="utf-8")
    listed = _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))
    one = listed["backups"][0]
    assert one["reports"] == 2 and one["reports_bytes"] == sum(len(b) for b in _reports(path).values())
    assert one["bytes"] == one["workspace_bytes"] + one["telemetry_bytes"] + one["reports_bytes"]
    kept = backups.folder(path) / one["name"].replace(f"{cid}-", f"{cid}-reports-", 1)[:-len(".db")]
    assert sorted(f.name for f in kept.iterdir()) == ["r1.csv", "r1.pdf"]
    # a report lost after the copy is put back; one made since is kept
    before = _reports(path)
    (made / "r1.pdf").unlink()
    (made / "r2.pdf").write_bytes(b"%PDF-1.4 made later")
    listing = _script(platformdb.data_dir(), "--company", cid)
    assert listing.returncode == 0 and "2 report files" in listing.stdout, listing.stdout + listing.stderr
    done = _script(platformdb.data_dir(), "--company", cid, one["name"])
    assert done.returncode == 0 and "2 report files" in done.stdout, done.stdout + done.stderr
    assert _reports(path) == {**before, "r2.pdf": b"%PDF-1.4 made later"}
    with store.use_company(path):
        assert backups.restore(one["name"])["reports"] == 2
    # the copies beyond those kept go with their reports
    for _ in range(backups.KEEP_MANUAL + 1):
        _ok(c.post(f"/api/founder/companies/{cid}/backups", json={}))
    assert not kept.exists()
    with store.use_company(path):
        held = {backups.companions(b["name"])[2] for b in backups.list_backups() if b["reports"]}
    assert {f.name for f in backups.folder(path).iterdir() if "-reports-" in f.name} == held and len(held) == backups.KEEP_MANUAL + 2   # 2 restore points


def test_the_daily_copy_leaves_a_default_database_outside_the_data_folder_in_use_alone(fdb, monkeypatch, tmp_path):
    """performance-live-07 (the backup half): a scratch run sets GRIDLYTICS_DATA_DIR only, so store.DB_PATH stays the owner's data/gridlytics.db, and
    backups.daily_all copied it into the owner's data/backup every day. The default database is copied only when it lies in the data folder in use."""
    owner = tmp_path / "owner-data"
    owner.mkdir()
    with closing(sqlite3.connect(owner / "gridlytics.db")) as con:
        con.execute("CREATE TABLE sites (id TEXT)")
        con.commit()
    before = sorted(p.name for p in owner.iterdir())
    monkeypatch.setattr(store, "DB_PATH", owner / "gridlytics.db")
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path / "scratch"))
    assert backups.daily_all(lambda: []) == []
    assert sorted(p.name for p in owner.iterdir()) == before and not (owner / "backup").exists()
    # with the data folder its own (no GRIDLYTICS_DATA_DIR), the default database is copied as before
    monkeypatch.delenv("GRIDLYTICS_DATA_DIR")
    made = backups.daily_all(lambda: [])
    assert [p.parent for p in made] == [owner / "backup"] and made[0].name.startswith("gridlytics-")


# ================================================================== review: where a Gridlytics role came from

def _membership_on_users(c, user_id: str, cid: str) -> dict:
    user = next(u for u in _ok(c.get("/api/founder/users"))["users"] if u["id"] == user_id)
    return next(m for m in user["memberships"] if m["company_id"] == cid)


def test_the_users_list_and_the_membership_answers_say_where_the_gridlytics_role_came_from(fdb):
    """founder-console-03 (review): the Change dialog says, before saving, what a change of the company role gives; with only the role to go by, it took a
    company admin whose administrator role was set in Gridlytics for one the company role gave, and said that making them a member ends it, though a role
    set in Gridlytics stays (companies.role_follows). The users list and the membership answers now say the role's source as the company's Users list
    does (role_source: owner, default, set or legacy; null before the first sign-in)."""
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    boss = _as(owner)
    _ok(boss.get("/api/sites"))
    assert _membership_on_users(c, owner["id"], cid)["role_source"] == "owner"
    maaz = _person(c, "maaz@meow.test", "Maaz Ali", cid, "admin")
    assert _membership_on_users(c, maaz["id"], cid)["role_source"] is None                # no row before the first sign-in
    _ok(_as(maaz).get("/api/sites"))
    assert {k: _membership_on_users(c, maaz["id"], cid)[k] for k in ("gridlytics_role", "role_source")} == {"gridlytics_role": "admin",
                                                                                                             "role_source": "default"}
    _ok(boss.put(f"/api/users/{maaz['id']}", json={"role": "admin"}))                       # set in Gridlytics: the same role, now the company's own choice
    assert _membership_on_users(c, maaz["id"], cid)["role_source"] == "set"
    answer = _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "member"}))["membership"]
    assert (answer["company_role"], answer["gridlytics_role"], answer["role_source"]) == ("member", "admin", "set")
    sunia = _person(c, "sunia@meow.test", "Sunia Ahmed", cid)
    added = _ok(c.patch(f"/api/founder/users/{sunia['id']}/memberships/{cid}", json={"company_role": "admin"}))["membership"]
    assert added["gridlytics_role"] is None and added["role_source"] is None
