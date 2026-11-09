"""Stage B3 of the sign-in (decisions-v2 sections 2, 3 and 5; design B.3 to B.5, with a database per company; the addendum's item 17): `companies.resolve`
takes a request's access token, and the company it asks for (the X-Gridlytics-Company header), to the person's company, that company's own database file and
the person's role in it.

Local mode reads the person, the membership and the company from platform.db on every request, so a disabled account, a suspended membership or a company
switched off is refused at once, with a token made before the change. Supabase mode reads the person's context from the project's rpc/get_my_context with the
person's own token: here `companies._fetch_context` is replaced by a stub that answers fixtures/get_my_context.json (the shape of the get_my_context of
supabase/migrations), and the HTTP call itself is tested against a stand-in opener, so no Supabase host is ever called. Every secret, key and address here is
made up for the tests."""
import copy
import http.client
import io
import json
import sqlite3
import time
import types
import urllib.error
import uuid
from contextlib import closing
from pathlib import Path

import jwt
import pytest

from gridlytics import auth, companies, identity, store, store_members
from gridlytics import platform as platformdb

#: Made up for the tests: never real secrets or keys
SECRET = "test-only-local-secret-0123456789abcdefgh"
PROJECT_SECRET = "test-only-project-secret-0123456789abcdefgh"
ANON = "anon-key-made-up-for-the-tests"
#: A name under .invalid, which no resolver answers (RFC 2606); nothing is fetched from it, since the call is replaced
URL = "https://gridlytics-test.invalid"
PASSWORD = "a long enough password"
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "get_my_context.json").read_text(encoding="utf-8"))
USER = FIXTURE["profile"]["id"]
NORTH, PENDING, SOUTH, SUSPENDED, UNVERIFIED = (m["company"]["id"] for m in FIXTURE["memberships"])
#: the grants an administrator gave the person beyond the role since round 7 (audit security-roles-16)
USER_KEYS = {"id", "email", "name", "role", "created", "disabled", "licence", "has_seal", "mfa", "company_role", "legacy_id", "grants"}


@pytest.fixture
def data(tmp_path, monkeypatch):
    """A data folder of the test's own: the workspace's default file, the registry and the companies' files are all under tmp_path."""
    monkeypatch.delenv("GRIDLYTICS_DATA_DIR", raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "gridlytics.db")
    monkeypatch.setattr(auth, "ITERATIONS", 1000)          # the accounts' hashes: what a sign-in costs is not what is tested here
    companies.clear_cache()
    yield tmp_path
    companies.clear_cache()


@pytest.fixture
def local(data, monkeypatch):
    monkeypatch.setattr(identity, "MODE", "local")
    monkeypatch.setattr(identity, "LOCAL_JWT_SECRET", SECRET)
    monkeypatch.setattr(identity, "AUDIENCE", "authenticated")
    identity.clear_cache()
    yield data
    identity.clear_cache()


def _refused(status: int, code: str, token: str, wanted=None) -> identity.Refused:
    with pytest.raises(identity.Refused) as caught:
        companies.resolve(token, wanted)
    assert (caught.value.status, caught.value.code) == (status, code), caught.value.detail
    assert caught.value.detail
    return caught.value


def _company(name: str, *, enabled: bool = True, seats: int = 5, **kw) -> dict:
    return platformdb.create_company(name, enabled=enabled, seats=seats, **kw)


def _account(email: str, name: str, *memberships) -> dict:
    """An account of local sign-in with its memberships, each (company, company role[, status])."""
    acct = platformdb.create_account(email, name, PASSWORD)
    for company, role, *status in memberships:
        platformdb.add_membership(acct["id"], company["id"], role, *status)
    return acct


def _token(acct: dict) -> str:
    return identity.issue_local(acct["id"], acct["email"], acct["name"])


def _aged(token: str, seconds: int) -> str:
    """The same local token, made `seconds` earlier (signed again with the test's secret)."""
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], audience="authenticated")
    claims.update(iat=claims["iat"] - seconds, exp=claims["exp"] - seconds)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _file(company: dict) -> Path:
    return platformdb.company_file(company["id"])


def _rows(path: Path, sql: str, *args) -> list[dict]:
    """Rows read straight from a company's file, never through the store, so the store's context cannot pick another file."""
    with closing(sqlite3.connect(path)) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, args)]


def _member(company: dict, acct: dict) -> dict | None:
    with store.use_company(_file(company)):
        return store_members.member(acct["id"])


# ============================================================================================================ local mode: which company

def test_one_company_and_no_header_open_that_company_and_its_own_database_made_on_first_use(local):
    north = _company("Northwind Solar")
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"))
    path = platformdb.companies_dir() / f"{north['id']}.db"
    assert not path.exists()
    who = companies.resolve(_token(saad), None)
    assert who.company == {"id": north["id"], "name": "Northwind Solar", "verified": True}
    assert who.db_path == path and path.exists()
    assert who.identity.user_id == saad["id"] and who.identity.email == "saad@example.test"
    # the user `auth.current` holds: today's shape, with the company role and the linked id of before (design B.5), built by store_members.as_user
    assert set(who.user) == USER_KEYS
    assert {k: who.user[k] for k in ("id", "email", "name", "role", "disabled", "licence", "has_seal", "mfa", "company_role", "legacy_id")} == {
        "id": saad["id"], "email": "saad@example.test", "name": "Saad Khan", "role": "admin", "disabled": False, "licence": "", "has_seal": False,
        "mfa": False, "company_role": "owner", "legacy_id": None}
    # the company's file is a full workspace at the current schema, with the member in its organisation `default`
    assert _rows(path, "PRAGMA user_version")[0]["user_version"] == store.SCHEMA_VERSION
    assert [(r["org_id"], r["user_id"], r["role"], r["role_source"]) for r in _rows(path, "SELECT * FROM members")] == [
        ("default", saad["id"], "admin", "owner")]
    assert who.user["created"] == _rows(path, "SELECT first_seen FROM members")[0]["first_seen"]
    # the default workspace file is never opened, and the request's context is left as it was
    assert not (local / "gridlytics.db").exists()
    assert store.db_path() == store.DB_PATH and store.org() == store.DEFAULT_ORG
    # an empty header is no header
    for empty in ("", "   "):
        assert companies.resolve(_token(saad), empty).company["id"] == north["id"]


def test_several_companies_need_the_header_and_each_opens_its_own_file(local):
    north, south = _company("Northwind Solar"), _company("Southwind Energy")
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"), (south, "member"))
    token = _token(saad)
    refusal = _refused(409, "company_required", token)
    assert "choose" in refusal.detail
    a, b = companies.resolve(token, north["id"]), companies.resolve(token, south["id"])
    assert (a.company["id"], a.db_path, a.user["role"]) == (north["id"], _file(north), "admin")
    assert (b.company["id"], b.db_path, b.user["role"]) == (south["id"], _file(south), "viewer")
    assert a.db_path != b.db_path
    # the header is a uuid in any of its spellings
    for spelling in (south["id"].upper(), "{" + south["id"] + "}", south["id"].replace("-", ""), f"  {south['id']} "):
        assert companies.resolve(token, spelling).company["id"] == south["id"]


def test_no_company_and_not_a_member(local):
    north, south = _company("Northwind Solar"), _company("Southwind Energy")
    nobody = _account("nobody@example.test", "No Company")
    assert _refused(403, "no_company", _token(nobody)).detail == "Your account is not set up for Gridlytics. Ask us to add you"
    sunia = _account("sunia@example.test", "Sunia Ahmed", (north, "member"))
    token = _token(sunia)
    for wanted in (south["id"], str(uuid.uuid4()), "not-a-uuid", "x" * 5000):
        _refused(403, "not_a_member", token, wanted)
    # nothing was made in the company the person is not a member of
    assert not _file(south).exists()


def test_only_active_memberships_count(local):
    north, south = _company("Northwind Solar"), _company("Southwind Energy")
    maaz = _account("maaz@example.test", "Maaz Ali", (north, "member", "pending"), (south, "member", "suspended"))
    token = _token(maaz)
    _refused(403, "no_company", token)
    _refused(403, "not_a_member", token, north["id"])
    _refused(403, "not_a_member", token, south["id"])
    platformdb.set_membership(maaz["id"], north["id"], status="active")
    assert companies.resolve(token, None).company["id"] == north["id"]


def test_a_company_switched_off_is_refused_with_the_decisions_sentence(local):
    north = _company("Northwind Solar", enabled=False)
    south = _company("Southwind Energy", enabled=False)
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"))
    token = _token(saad)
    for wanted in (None, north["id"]):
        assert _refused(403, "company_disabled", token, wanted).detail == "Gridlytics is not switched on for Northwind Solar"
    # with no header, a company switched on is the one, though the person has others switched off
    sunia = _account("sunia@example.test", "Sunia Ahmed", (north, "member"), (south, "member"))
    assert _refused(403, "company_disabled", _token(sunia)).detail == "Gridlytics is not switched on for Northwind Solar and Southwind Energy"
    platformdb.update_company(south["id"], enabled=True)
    assert companies.resolve(_token(sunia), None).company["id"] == south["id"]
    _refused(403, "company_disabled", _token(sunia), north["id"])
    # a company switched off never has its file made
    assert not _file(north).exists()


def test_revocation_in_local_mode_is_at_once_for_a_token_made_before_the_change(local, monkeypatch):
    north = _company("Northwind Solar")
    sunia = _account("sunia@example.test", "Sunia Ahmed", (north, "member"))
    token = _token(sunia)
    assert companies.resolve(token, None).user["id"] == sunia["id"]
    platformdb.set_membership(sunia["id"], north["id"], status="suspended")
    _refused(403, "not_a_member", token, north["id"])
    _refused(403, "no_company", token)
    platformdb.set_membership(sunia["id"], north["id"], status="active")
    assert companies.resolve(token, north["id"])
    platformdb.remove_membership(sunia["id"], north["id"])
    _refused(403, "not_a_member", token, north["id"])
    platformdb.add_membership(sunia["id"], north["id"], "member")
    platformdb.update_company(north["id"], enabled=False)
    _refused(403, "company_disabled", token)
    platformdb.update_company(north["id"], enabled=True)
    assert companies.resolve(token, None)
    # a password changed, or the account disabled, ends the tokens made before it (platform.signed_in, the token's iat against sessions_from)
    before = _aged(token, 10)
    assert companies.resolve(before, None)
    platformdb.set_password(sunia["id"], "another long password")
    assert _refused(401, "session_ended", before).detail == identity.ENDED
    fresh = _token(sunia)
    assert companies.resolve(fresh, None)
    later = time.time() + 5
    monkeypatch.setattr(platformdb, "_clock", lambda: later)
    platformdb.set_account_disabled(sunia["id"], True)
    _refused(401, "session_ended", fresh)
    platformdb.set_account_disabled(sunia["id"], False)
    _refused(401, "session_ended", fresh)                 # enabled again, its old tokens stay ended


def test_a_token_identity_refuses_is_refused_as_it_is(local):
    _company("Northwind Solar")
    _refused(401, "token_invalid", "not.a.token")
    forged = jwt.encode({"sub": str(uuid.uuid4())}, "another-secret-of-forty-characters-0123456", algorithm="HS256")
    _refused(401, "token_invalid", forged)


def test_a_company_with_no_file_is_refused_and_the_default_file_is_never_opened(local, monkeypatch):
    north = _company("Northwind Solar")
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"))
    monkeypatch.setattr(platformdb, "company_file", lambda which: None)
    _refused(403, "company_disabled", _token(saad))
    assert not (local / "gridlytics.db").exists() and not (local / "companies").exists()


def test_a_company_may_name_its_file_by_an_absolute_path(local):
    """Tests and the browser check register their company with the workspace's own file (addendum item 3): open-mode data and signed-in data are one."""
    north = _company("Northwind Solar", db_file=store.DB_PATH)
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"))
    who = companies.resolve(_token(saad), None)
    assert who.db_path == store.DB_PATH
    assert [r["user_id"] for r in _rows(store.DB_PATH, "SELECT user_id FROM members")] == [saad["id"]]


# ============================================================================================================ local mode: the person's role

def test_the_owner_is_an_administrator_and_stays_one(local):
    north = _company("Northwind Solar")
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"))
    token = _token(saad)
    assert (companies.resolve(token, None).user["role"], _member(north, saad)["role_source"]) == ("admin", "owner")
    # pinned: a row changed by mistake is put back at the next request (decisions-v2 section 3: never demoted, never switched off)
    with store.use_company(_file(north)):
        store_members.update_member(saad["id"], role="viewer", role_source="set", disabled=True, disabled_reason="by mistake")
    who = companies.resolve(token, None)
    assert (who.user["role"], who.user["disabled"]) == ("admin", False)
    assert {k: _member(north, saad)[k] for k in ("role", "role_source", "disabled", "disabled_reason")} == {
        "role": "admin", "role_source": "owner", "disabled": False, "disabled_reason": ""}


def test_everyone_else_starts_as_a_viewer(local):
    north = _company("Northwind Solar")
    sunia = _account("sunia@example.test", "Sunia Ahmed", (north, "admin"))
    maaz = _account("maaz@example.test", "Maaz Ali", (north, "member"))
    for acct, company_role in ((sunia, "admin"), (maaz, "member")):
        who = companies.resolve(_token(acct), None)
        assert (who.user["role"], who.user["company_role"]) == ("viewer", company_role)
        assert _member(north, acct)["role_source"] == "default"


def test_a_preset_gives_its_role_once(local):
    north = _company("Northwind Solar")
    with store.use_company(_file(north)):
        store_members.set_preset("Sunia@Example.test", "operator", by="owner")
        store_members.set_preset("saad@example.test", "analyst", by="owner")
    sunia = _account("sunia@example.test", "Sunia Ahmed", (north, "member"))
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"))
    who = companies.resolve(_token(sunia), None)
    assert who.user["role"] == "operator" and _member(north, sunia)["role_source"] == "set"
    with store.use_company(_file(north)):
        assert store_members.preset("sunia@example.test") is None
        assert store_members.use_preset("sunia@example.test") is None
    # the owner's rule comes first: the owner's preset is not taken, and stays for the owner to delete
    assert companies.resolve(_token(saad), None).user["role"] == "admin"
    with store.use_company(_file(north)):
        assert [p["email"] for p in store_members.list_presets()] == ["saad@example.test"]
    # a role the owner sets later stays the person's (the preset is not used again)
    with store.use_company(_file(north)):
        store_members.update_member(sunia["id"], role="analyst", role_source="set")
    assert companies.resolve(_token(sunia), None).user["role"] == "analyst"


def test_a_preset_is_spent_only_by_the_row_that_took_it(local, monkeypatch):
    """Two first requests of one person in two workers: the row made first is the one kept (store_members.add_member). When the other worker made it without
    the preset (set a moment after that worker looked), the preset is not spent: it stays listed, unused."""
    north = _company("Northwind Solar")
    sunia = _account("sunia@example.test", "Sunia Ahmed", (north, "member"))
    with store.use_company(_file(north)):
        store_members.set_preset("sunia@example.test", "operator")
    add = store_members.add_member

    def made_first_elsewhere(user_id, **kw):
        add(user_id, **{**kw, "role": "viewer", "role_source": "default"})
        return add(user_id, **kw)

    monkeypatch.setattr(store_members, "add_member", made_first_elsewhere)
    who = companies.resolve(_token(sunia), None)
    assert who.user["role"] == "viewer" and _member(north, sunia)["role_source"] == "default"
    with store.use_company(_file(north)):
        assert store_members.preset("sunia@example.test")["role"] == "operator"


def test_later_requests_follow_the_account_and_the_membership(local):
    north = _company("Northwind Solar")
    maaz = _account("maaz@example.test", "Maaz Ali", (north, "member"))
    token = _token(maaz)
    assert companies.resolve(token, None).user["role"] == "viewer"
    assert _member(north, maaz)["last_seen"]
    platformdb.rename_account(maaz["id"], "Maaz A. Ali")
    assert companies.resolve(token, None).user["name"] == "Maaz A. Ali"
    # made the company's owner: the pinned administrator; no longer the owner: derived again, a viewer
    platformdb.set_membership(maaz["id"], north["id"], role="owner")
    assert (companies.resolve(token, None).user["role"], _member(north, maaz)["role_source"]) == ("admin", "owner")
    platformdb.set_membership(maaz["id"], north["id"], role="admin")
    who = companies.resolve(token, None)
    assert (who.user["role"], who.user["company_role"], _member(north, maaz)["role_source"]) == ("viewer", "admin", "default")
    # a role set in Gridlytics stays when the company role moves between admin and member
    with store.use_company(_file(north)):
        store_members.update_member(maaz["id"], role="estimator", role_source="set")
    platformdb.set_membership(maaz["id"], north["id"], role="member")
    who = companies.resolve(token, None)
    assert (who.user["role"], who.user["company_role"]) == ("estimator", "member")
    assert len(_rows(_file(north), "SELECT * FROM members")) == 1


def test_seats_come_from_the_registry(local):
    north = _company("Northwind Solar", seats=2)
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"))
    sunia = _account("sunia@example.test", "Sunia Ahmed", (north, "member"))
    maaz = _account("maaz@example.test", "Maaz Ali", (north, "member"))
    assert companies.resolve(_token(saad), None) and companies.resolve(_token(sunia), None)
    refusal = _refused(403, "no_seat", _token(maaz))
    assert "Northwind Solar" in refusal.detail and "2 seats" in refusal.detail
    row = _member(north, maaz)
    assert (row["disabled"], row["disabled_reason"], row["role"]) == (True, "no seat", "viewer")
    with store.use_company(_file(north)):
        assert store_members.count_members() == 2
    # the plan block of the workspace, which its administrators may edit, does not give seats: only the founder's registry does
    _refused(403, "no_seat", _token(maaz))
    platformdb.update_company(north["id"], seats=3)
    who = companies.resolve(_token(maaz), None)
    assert who.user["disabled"] is False and _member(north, maaz)["disabled_reason"] == ""
    # a person switched off by an administrator is not let in by a free seat
    with store.use_company(_file(north)):
        store_members.update_member(sunia["id"], disabled=True, disabled_reason="")
    platformdb.update_company(north["id"], seats=10)
    assert "Northwind Solar" in _refused(403, "member_disabled", _token(sunia)).detail
    # the owner always has a seat
    south = _company("Southwind Energy", seats=0)
    owner = _account("owner@example.test", "South Owner", (south, "owner"))
    assert companies.resolve(_token(owner), None).user["role"] == "admin"
    other = _account("other@example.test", "South Member", (south, "member"))
    _refused(403, "no_seat", _token(other))


# ============================================================================================================ local mode: two companies

def test_two_companies_resolve_in_isolation(local):
    north, south = _company("Northwind Solar"), _company("Southwind Energy")
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"), (south, "member"))
    sunia = _account("sunia@example.test", "Sunia Ahmed", (north, "member"))
    with store.use_company(_file(south)):
        store_members.set_preset("saad@example.test", "analyst")
        store_members.set_preset("sunia@example.test", "approver")
    a, b = companies.resolve(_token(saad), north["id"]), companies.resolve(_token(saad), south["id"])
    # one person, two companies, two rows in two files: the owner's admin in A, the preset of B in B
    assert (a.user["role"], b.user["role"]) == ("admin", "analyst")
    assert [(r["user_id"], r["role"]) for r in _rows(a.db_path, "SELECT * FROM members")] == [(saad["id"], "admin")]
    assert [(r["user_id"], r["role"]) for r in _rows(b.db_path, "SELECT * FROM members")] == [(saad["id"], "analyst")]
    # a preset of B does not reach A: Sunia is a viewer in A, and B's preset for her is still unused
    assert companies.resolve(_token(sunia), None).user["role"] == "viewer"
    with store.use_company(b.db_path):
        assert store_members.preset("sunia@example.test")["role"] == "approver"
    _refused(403, "not_a_member", _token(sunia), south["id"])
    assert sunia["id"] not in {r["user_id"] for r in _rows(b.db_path, "SELECT * FROM members")}
    # what is saved in A's file is not in B's
    with store.use_company(a.db_path):
        run = store.save_run("structure", "North's run", "", {}, {})
    with store.use_company(b.db_path):
        assert store.get_run(run["id"]) is None and store.list_runs() == []
    with store.use_company(a.db_path):
        assert [r["id"] for r in store.list_runs()] == [run["id"]]


# ============================================================================================================ supabase mode

class _Rpc:
    """A stand-in for `companies._fetch_context`: the fixture's context (or `answer`) given to the token's person, as get_my_context answers for auth.uid(),
    unless `as_caller` is off; or `fail` raised. `calls` are the tokens it was called with."""

    def __init__(self):
        self.calls, self.answer, self.fail, self.as_caller = [], ..., None, True

    def __call__(self, token: str):
        self.calls.append(token)
        if self.fail is not None:
            raise self.fail
        ctx = copy.deepcopy(FIXTURE if self.answer is ... else self.answer)
        if ctx is not None and self.as_caller:
            ctx["profile"]["id"] = jwt.decode(token, options={"verify_signature": False})["sub"]
        return ctx


@pytest.fixture
def supa(data, monkeypatch):
    """Supabase mode with the project's HS256 secret, a clock of the test's, the stub of rpc/get_my_context, and the fixture's companies in the registry (all
    switched on, by the same uuids): whether a membership counts is the context's to say."""
    monkeypatch.setattr(identity, "MODE", "supabase")
    monkeypatch.setattr(identity, "SUPABASE_URL", URL)
    monkeypatch.setattr(identity, "SUPABASE_ANON_KEY", ANON)
    monkeypatch.setattr(identity, "SUPABASE_JWT_SECRET", PROJECT_SECRET)
    monkeypatch.setattr(identity, "AUDIENCE", "authenticated")
    identity.clear_cache()
    clock = [1000.0]
    monkeypatch.setattr(companies, "_clock", lambda: clock[0])
    rpc = _Rpc()
    monkeypatch.setattr(companies, "_fetch_context", rpc)
    for m in FIXTURE["memberships"]:
        _company(m["company"]["name"], company_id=m["company"]["id"])
    yield types.SimpleNamespace(rpc=rpc, clock=clock)
    identity.clear_cache()


def _supa_token(sub: str = USER, email: str = "saad@example.test") -> str:
    """An access token of the project, as Supabase Auth writes it for an email-and-password sign-in."""
    now = int(time.time())
    claims = {"iss": f"{URL}/auth/v1", "aud": "authenticated", "sub": sub, "email": email, "phone": "", "role": "authenticated", "iat": now,
              "exp": now + 3600, "aal": "aal1", "amr": [{"method": "password", "timestamp": now}], "session_id": str(uuid.uuid4()), "is_anonymous": False,
              "app_metadata": {"provider": "email", "providers": ["email"]}, "user_metadata": {}}
    return jwt.encode(claims, PROJECT_SECRET, algorithm="HS256")


def _membership(company_id: str, name: str, role: str = "member", status: str = "active", verified: bool = True) -> dict:
    return {"id": str(uuid.uuid4()), "role": role, "status": status, "created_at": "2026-10-24T10:00:00+00:00", "activated_at": "2026-10-24T10:00:00+00:00",
            "company": {"id": company_id, "name": name, "verified": verified}}


def test_supabase_mode_reads_the_context_with_the_persons_own_token(supa):
    token = _supa_token()
    # the fixture's active memberships of verified companies are Northwind (owner) and Southwind (member)
    _refused(409, "company_required", token)
    north, south = companies.resolve(token, NORTH), companies.resolve(token, SOUTH)
    assert supa.rpc.calls == [token]                       # fetched once, then kept
    assert (north.user["role"], north.user["company_role"], north.company) == ("admin", "owner", {"id": NORTH, "name": "Northwind Solar", "verified": True})
    assert (south.user["role"], south.user["company_role"]) == ("viewer", "member")
    # the name and the address are the profile's (auth.actor prints the name)
    assert (north.user["name"], north.user["email"], north.user["id"]) == ("Saad Khan", "saad@example.test", USER)
    assert north.db_path == platformdb.companies_dir() / f"{NORTH}.db" and north.db_path.exists()
    assert south.db_path == platformdb.companies_dir() / f"{SOUTH}.db"


def test_pending_suspended_and_unverified_memberships_do_not_count(supa):
    token = _supa_token()
    for wanted in (PENDING, SUSPENDED, UNVERIFIED):
        _refused(403, "not_a_member", token, wanted)
    for company_id in (PENDING, SUSPENDED, UNVERIFIED):
        assert not (platformdb.companies_dir() / f"{company_id}.db").exists()


def test_the_context_is_kept_for_60_seconds_per_person(supa):
    token, other = _supa_token(), _supa_token(str(uuid.uuid4()), "sunia@example.test")
    companies.resolve(token, NORTH)
    supa.clock[0] += 59
    companies.resolve(token, NORTH)
    assert len(supa.rpc.calls) == 1
    companies.resolve(other, SOUTH)                        # another person has a context of their own
    assert supa.rpc.calls[-1] == other and len(supa.rpc.calls) == 2
    supa.clock[0] += 1
    companies.resolve(token, NORTH)
    assert supa.rpc.calls[-1] == token and len(supa.rpc.calls) == 3


def test_while_the_service_is_down_a_context_up_to_five_minutes_old_is_used_then_503(supa):
    token = _supa_token()
    companies.resolve(token, NORTH)
    supa.rpc.fail = companies.Unreachable("the project cannot be reached")
    supa.clock[0] = 1100                                   # past the 60 s: fetched again, which fails, and the context kept is used
    assert companies.resolve(token, NORTH).company["id"] == NORTH
    assert len(supa.rpc.calls) == 2
    supa.clock[0] = 1105                                   # the service was just found down: not asked again at once
    assert companies.resolve(token, NORTH)
    assert len(supa.rpc.calls) == 2
    supa.clock[0] = 1299.5
    assert companies.resolve(token, NORTH)
    supa.clock[0] = 1300                                   # five minutes old: no longer used
    refusal = _refused(503, "identity_unavailable", token, NORTH)
    assert refusal.detail == identity.UNAVAILABLE
    # someone with nothing kept gets 503 at once
    _refused(503, "identity_unavailable", _supa_token(str(uuid.uuid4()), "maaz@example.test"), NORTH)
    # the service back: fetched again
    supa.rpc.fail = None
    supa.clock[0] = 1400
    assert companies.resolve(token, NORTH)


def test_a_null_context_is_no_context(supa):
    """get_my_context answers null for a caller it does not know: an account banned (the founder's "disable") or deleted."""
    supa.rpc.answer = None
    assert _refused(401, "session_ended", _supa_token(), NORTH).detail == identity.ENDED
    # banned after a context was kept: the refetch for a company not in it answers null, and the session ends there
    sunia = _supa_token(str(uuid.uuid4()), "sunia@example.test")
    supa.rpc.answer = ...
    assert companies.resolve(sunia, NORTH)
    supa.rpc.answer = None
    supa.clock[0] += 1
    assert _refused(401, "session_ended", sunia, str(uuid.uuid4())).detail == identity.ENDED
    assert len(supa.rpc.calls) == 3


def test_a_context_of_another_person_is_not_taken(supa):
    supa.rpc.as_caller = False
    _refused(503, "identity_unavailable", _supa_token(str(uuid.uuid4())), NORTH)
    supa.clock[0] += companies.RETRY_SECONDS
    supa.rpc.answer = {**copy.deepcopy(FIXTURE), "profile": "not a profile"}
    _refused(503, "identity_unavailable", _supa_token(), NORTH)


def test_a_token_the_service_refuses_is_invalid_and_nothing_is_kept(supa):
    supa.rpc.fail = identity.Refused(401, "token_invalid", identity.INVALID)
    _refused(401, "token_invalid", _supa_token(), NORTH)
    supa.rpc.fail = None
    assert companies.resolve(_supa_token(), NORTH)
    assert len(supa.rpc.calls) == 2


def test_a_company_not_in_the_context_is_fetched_once_more_then_refused(supa):
    token = _supa_token()
    companies.resolve(token, NORTH)
    # the founder adds the person to a new company: its membership shows at the refetch, without waiting out the 60 s
    east = _company("Eastwind Power")
    added = copy.deepcopy(FIXTURE)
    added["memberships"].append(_membership(east["id"], "Eastwind Power"))
    supa.rpc.answer = added
    supa.clock[0] += 1
    assert companies.resolve(token, east["id"]).company["id"] == east["id"]
    assert len(supa.rpc.calls) == 2
    # a company that is not theirs: refused; the next refetch waits 10 s
    elsewhere = str(uuid.uuid4())
    supa.clock[0] += 1
    _refused(403, "not_a_member", token, elsewhere)
    assert len(supa.rpc.calls) == 2
    supa.clock[0] += 9
    _refused(403, "not_a_member", token, elsewhere)
    assert len(supa.rpc.calls) == 3
    # the wait is each person's own: another person's refetch is not held up by this one's
    other = _supa_token(str(uuid.uuid4()), "sunia@example.test")
    assert companies.resolve(other, NORTH)
    supa.clock[0] += 1
    _refused(403, "not_a_member", other, elsewhere)
    assert supa.rpc.calls[-1] == other and len(supa.rpc.calls) == 5
    # a header that is not a uuid names no company: refused without asking again, though a refetch would be allowed now
    supa.clock[0] += companies.REFETCH_SECONDS
    _refused(403, "not_a_member", token, "not-a-uuid")
    assert len(supa.rpc.calls) == 5
    # only a company missing from the context is asked for again: several companies and no header (409), or a company switched off, is not the context's
    _refused(409, "company_required", token)
    platformdb.update_company(NORTH, enabled=False)
    _refused(403, "company_disabled", token, NORTH)
    assert len(supa.rpc.calls) == 5


def test_no_company_is_fetched_once_more_too(supa):
    token = _supa_token()
    supa.rpc.answer = {**copy.deepcopy(FIXTURE), "memberships": []}
    _refused(403, "no_company", token)
    assert len(supa.rpc.calls) == 1                        # fetched just now: no refetch
    supa.rpc.answer = {**copy.deepcopy(FIXTURE), "memberships": [_membership(SOUTH, "Southwind Energy")]}
    supa.clock[0] += 10
    assert companies.resolve(token, None).company["id"] == SOUTH
    assert len(supa.rpc.calls) == 2


def test_a_super_admin_or_an_unknown_company_role_gives_no_company(supa):
    """Design B.3 and owner decision K.4: a super-admin with no membership has no special access; a company role other than owner, admin or member is not
    one of Gridlytics'."""
    ctx = copy.deepcopy(FIXTURE)
    ctx["profile"]["is_super_admin"] = True
    ctx["memberships"] = [{**_membership(NORTH, "Northwind Solar"), "role": "superuser"}]
    supa.rpc.answer = ctx
    _refused(403, "no_company", _supa_token())
    _refused(403, "not_a_member", _supa_token(), NORTH)


def test_enabled_and_seats_come_from_the_registry_by_the_same_uuid(supa):
    token = _supa_token()
    platformdb.update_company(NORTH, enabled=False)
    assert _refused(403, "company_disabled", token, NORTH).detail == "Gridlytics is not switched on for Northwind Solar"
    # with no header, the one company switched on is the one
    assert companies.resolve(token, None).company["id"] == SOUTH
    # a company of the project the founder never registered is not switched on
    stranger = str(uuid.uuid4())
    unknown = copy.deepcopy(FIXTURE)
    unknown["memberships"].append(_membership(stranger, "Westwind Ltd"))
    supa.rpc.answer = unknown
    supa.clock[0] += 60
    assert _refused(403, "company_disabled", token, stranger).detail == "Gridlytics is not switched on for Westwind Ltd"
    # seats: the registry's, by the company's uuid
    platformdb.update_company(SOUTH, seats=0)
    other = _supa_token(str(uuid.uuid4()), "maaz@example.test")
    _refused(403, "no_seat", other, SOUTH)
    platformdb.update_company(SOUTH, seats=5)
    assert companies.resolve(other, SOUTH).user["disabled"] is False
    # the company's name is the context's; the registry's when the context has none
    nameless = copy.deepcopy(FIXTURE)
    nameless["memberships"][2]["company"]["name"] = ""
    supa.rpc.answer = nameless
    supa.clock[0] += 60
    assert companies.resolve(token, SOUTH).company == {"id": SOUTH, "name": "Southwind Energy", "verified": True}


def test_the_members_address_and_name_are_the_accounts_now_tidied(supa):
    """The address and the name are the account's as the sign-in service keeps them now (the profile), not the token's, which may be an hour old; the token's
    only when the profile has none."""
    ctx = copy.deepcopy(FIXTURE)
    ctx["profile"].update(email=" Saad.Khan@Example.test ", name="  Saad \t Khan  " + "x" * 100)
    supa.rpc.answer = ctx
    token = _supa_token(email="saad.before@example.test")
    who = companies.resolve(token, NORTH)
    assert (who.user["email"], who.user["name"]) == ("saad.khan@example.test", ("Saad Khan " + "x" * 100)[:80])
    ctx["profile"].update(email=None, name="  ")
    supa.clock[0] += 60
    who = companies.resolve(token, NORTH)
    assert (who.user["email"], who.user["name"]) == ("saad.before@example.test", "saad.before@example.test")


def test_the_kept_contexts_are_bounded(supa, monkeypatch):
    monkeypatch.setattr(companies, "MAX_KEPT", 2)
    tokens = [_supa_token(str(uuid.uuid4()), f"p{i}@example.test") for i in range(3)]
    for token in tokens:
        companies.resolve(token, NORTH)
    assert len(supa.rpc.calls) == 3
    companies.resolve(tokens[2], NORTH)
    assert len(supa.rpc.calls) == 3
    companies.resolve(tokens[0], NORTH)                    # the oldest was let go
    assert len(supa.rpc.calls) == 4
    companies.clear_cache()
    companies.resolve(tokens[2], NORTH)
    assert len(supa.rpc.calls) == 5


def test_local_mode_never_asks_the_service(local, monkeypatch):
    rpc = _Rpc()
    monkeypatch.setattr(companies, "_fetch_context", rpc)
    north = _company("Northwind Solar")
    saad = _account("saad@example.test", "Saad Khan", (north, "owner"))
    companies.resolve(_token(saad), None)
    _refused(403, "not_a_member", _token(saad), str(uuid.uuid4()))
    assert rpc.calls == []


# ============================================================================================================ the call to rpc/get_my_context

class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def opener(data, monkeypatch):
    """`companies._urlopen` replaced: the requests it was given, and what it answers (bytes, or an exception to raise)."""
    monkeypatch.setattr(identity, "SUPABASE_URL", URL)
    monkeypatch.setattr(identity, "SUPABASE_ANON_KEY", ANON)
    monkeypatch.setenv("GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY", "service-role-key-made-up-for-the-tests")
    seen = types.SimpleNamespace(requests=[], answer=json.dumps(FIXTURE).encode())

    def urlopen(request, timeout):
        seen.requests.append((request, timeout))
        if isinstance(seen.answer, BaseException):
            raise seen.answer
        return _Answer(seen.answer)

    monkeypatch.setattr(companies, "_urlopen", urlopen)
    return seen


def test_the_call_is_a_post_with_the_anon_key_and_the_persons_own_token(opener):
    assert companies._fetch_context("the.persons.token") == FIXTURE
    (request, timeout), = opener.requests
    assert (request.full_url, request.get_method(), request.data, timeout) == (f"{URL}/rest/v1/rpc/get_my_context", "POST", b"{}", 5)
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers == {"apikey": ANON, "authorization": "Bearer the.persons.token", "content-type": "application/json", "accept": "application/json"}
    # the service-role key is the founder console's, never sent with a person's request
    assert not any("service-role" in v for v in headers.values())
    opener.answer = b"null"
    assert companies._fetch_context("the.persons.token") is None


def test_the_call_answers_401_as_an_invalid_token_and_anything_else_as_unreachable(opener, monkeypatch):
    def http_error(code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(f"{URL}/rest/v1/rpc/get_my_context", code, "refused", {}, io.BytesIO(b"{}"))

    opener.answer = http_error(401)
    with pytest.raises(identity.Refused) as caught:
        companies._fetch_context("t")
    assert (caught.value.status, caught.value.code) == (401, "token_invalid")
    for answer in (http_error(500), http_error(404), http_error(403), http_error(302), urllib.error.URLError("no route"), TimeoutError("slow"),
                   http.client.IncompleteRead(b"{"), http.client.InvalidURL("nonnumeric port: 'x'"), b"not json", b"\xff\xfe", b"[]", b'{"profile": {}}',
                   b'"text"', b"{" + b" " * (companies.MAX_CONTEXT_BYTES + 1) + b"}"):
        opener.answer = answer
        with pytest.raises(companies.Unreachable):
            companies._fetch_context("t")
    # a project address the request cannot be made to is the project unreachable (503), not a fault of the app (500)
    monkeypatch.setattr(identity, "SUPABASE_URL", "")
    opener.answer = json.dumps(FIXTURE).encode()
    with pytest.raises(companies.Unreachable):
        companies._fetch_context("t")
