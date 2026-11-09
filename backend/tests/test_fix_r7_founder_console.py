"""Round 7 (the end-to-end audit), the founder console: its session ends on the server, a short session secret is refused, and a company's backups are
listed, copied, pruned and restored as one set (workspace, telemetry and signing keys), with a restore script that exists. One test per audit finding;
the finding's id is in each test's docstring."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import identities
from gridlytics import backups, delivery, founder, identity, sites, store, telemetry
from gridlytics import platform as platformdb
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
    return _ok(c.post("/api/founder/users", json={"email": email, "name": name, "password": f"{name.split()[0].lower()} first password",
                                                   "company_id": cid, "company_role": company_role}), 201)["user"]


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
