"""The sign-in half of the request guard, carried over from Solarlytics' `tests/test_signin_guard.py` less its share links, which Gridlytics does not
have (round 7, audit parity-architecture-10): the guard reads the Bearer token, works in the company's own database as the person's member row there,
refuses a token, a company or a key that is not good by its code, and keeps a site's API key to its own site. Gridlytics has no open mode: a request
without a token is told to sign in, on this machine too.

Each test has its own data folder, registry and companies' files; the tokens are signed with the tests' made-up key (conftest.py)."""
from __future__ import annotations

import base64
import json
import logging

import jwt
import pytest
from fastapi.testclient import TestClient

import identities
from gridlytics import api, auth, identity, sites, store
from gridlytics import platform as platformdb


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A data folder of the test's own: its registry and its companies' files."""
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    return tmp_path


def _client(**kw) -> TestClient:
    return TestClient(api.app, raise_server_exceptions=False, **kw)


def _bearer(access_token: str, **extra) -> dict:
    return {"Authorization": f"Bearer {access_token}", **extra}


def _expired(access_token: str) -> str:
    """The same person's token, signed with the same key, an hour past its end."""
    claims = jwt.decode(access_token, options={"verify_signature": False})
    claims.update(iat=claims["iat"] - 7200, exp=claims["iat"] - 3600)
    return jwt.encode(claims, identity.local_secret(), algorithm="HS256")


def _forged(access_token: str) -> tuple[str, str]:
    """The same claims unsigned (alg none), and signed with another key: neither is the sign-in's."""
    claims = jwt.decode(access_token, options={"verify_signature": False})
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()      # noqa: E731
    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(claims)}.", jwt.encode(claims, "another made-up signing key of the tests 0002", algorithm="HS256")


def _two_companies():
    """Two companies with a file each, as hosted companies have, and the owner of each signed in."""
    alpha, beta = identities.company("Alpha Energy", own_file=True), identities.company("Beta Energy", own_file=True)
    return (alpha, identities.client("owner@alpha.pk", None, alpha, "owner", name="Alpha Owner", raise_server_exceptions=False),
            beta, identities.client("owner@beta.pk", None, beta, "owner", name="Beta Owner", raise_server_exceptions=False))


# ================================================================== a person's token: their company's own database

def test_a_token_works_in_its_companys_database_as_the_person_with_the_actor_set(db):
    alpha, a, beta, b = _two_companies()
    r = a.post("/api/sites", json={"name": "Alpha mill", "disco": "FESCO"})
    assert r.status_code == 200, r.text
    alpha_site = r.json()["id"]
    assert b.post("/api/sites", json={"name": "Beta mall", "disco": "LESCO"}).status_code == 200
    a_file, b_file = platformdb.company_file(alpha), platformdb.company_file(beta)
    assert a_file != b_file and store.DB_PATH not in (a_file, b_file)
    with store.use_company(a_file):
        assert [s["name"] for s in sites.listing()] == ["Alpha mill"]
    with store.use_company(b_file):
        assert [s["name"] for s in sites.listing()] == ["Beta mall"]
    # each sees only its own, by list and by id
    assert [s["name"] for s in a.get("/api/sites").json()] == ["Alpha mill"]
    assert b.get(f"/api/sites/{alpha_site}").status_code == 404
    # the person is their member row there: the owner an administrator, named in the answer's role header, the audit's actor by id and address
    r = a.get("/api/auth/state")
    me = r.json()["user"]
    assert me["role"] == "admin" and me["company_role"] == "owner" and me["name"] == "Alpha Owner" and r.headers["x-gridlytics-role"] == "admin"
    assert me["id"] == platformdb.account_by_email("owner@alpha.pk")["id"]
    with store.use_company(a_file):
        added = [e for e in store.list_audit() if e["action"] == "added a site"]
    assert [(e["actor_id"], e["actor_email"], e["target"]) for e in added] == [(me["id"], "owner@alpha.pk", "Alpha mill")]


def test_the_request_log_names_the_person_and_never_the_token(db, caplog):
    alpha = identities.company("Alpha Energy", own_file=True)
    sent = identities.token("owner@alpha.pk", None, alpha, "owner")
    caplog.set_level(logging.INFO, logger="gridlytics")
    r = _client().post("/api/sites", json={"name": "Logged", "disco": "FESCO"}, headers=identities.headers(sent, alpha))
    assert r.status_code == 200, r.text
    line = next(m for m in caplog.messages if m.startswith("request ") and "POST /api/sites -> 200" in m)
    assert line.endswith(f" user {platformdb.account_by_email('owner@alpha.pk')['id']}")
    assert not any(sent in m for m in caplog.messages)


def test_a_role_set_in_the_company_is_the_one_the_request_has(db):
    identities.client("owner@acme.pk", None, company_role="owner")
    viewer = identities.client("viewer@acme.pk", "viewer", raise_server_exceptions=False)
    r = viewer.get("/api/sites")
    assert r.status_code == 200 and r.headers["x-gridlytics-role"] == "viewer"
    r = viewer.post("/api/sites", json={"name": "Not mine to add", "disco": "FESCO"})
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


def test_the_company_is_chosen_by_the_header_or_refused_by_its_code(db):
    alpha, beta = identities.company("Alpha Energy", own_file=True), identities.company("Beta Energy", own_file=True)
    both = identities.token("both@acme.pk", company=alpha)
    identities.token("both@acme.pk", company=beta)
    # two companies and no header: the page must choose
    r = _client().get("/api/sites", headers=_bearer(both))
    assert r.status_code == 409 and r.json()["code"] == "company_required"
    assert _client().get("/api/sites", headers=_bearer(both, **{"X-Gridlytics-Company": beta["id"]})).status_code == 200
    # a company that is not theirs, one switched off, and none at all
    other = identities.company("Gamma Energy", own_file=True)
    r = _client().get("/api/sites", headers=_bearer(both, **{"X-Gridlytics-Company": other["id"]}))
    assert r.status_code == 403 and r.json()["code"] == "not_a_member"
    platformdb.update_company(beta["id"], enabled=False)
    r = _client().get("/api/sites", headers=_bearer(both, **{"X-Gridlytics-Company": beta["id"]}))
    assert r.status_code == 403 and r.json()["code"] == "company_disabled" and "Beta Energy" in r.json()["detail"]
    lone = platformdb.create_account("lone@acme.pk", "Lone", identities.PASSWORD)
    r = _client().get("/api/sites", headers=_bearer(identity.issue_local(lone["id"], lone["email"], lone["name"])))
    assert r.status_code == 403 and r.json()["code"] == "no_company" and r.json()["request_id"]


def test_a_token_that_is_not_good_is_refused_with_its_code_but_never_on_a_public_path(db):
    good = identities.token("owner@acme.pk", None, company_role="owner")
    unsigned, other_key = _forged(good)
    for sent, code in ((_expired(good), "token_expired"), ("not.a.token", "token_invalid"), (good[:-3] + "abc", "token_invalid"),
                       (unsigned, "token_invalid"), (other_key, "token_invalid")):
        r = _client().get("/api/sites", headers=_bearer(sent))
        assert r.status_code == 401 and r.json()["code"] == code and r.headers["www-authenticate"] == "Bearer", (code, r.text)
    # the sign-in state answers it too, so that the page renews the token first
    r = _client().get("/api/auth/state", headers=_bearer(_expired(good)))
    assert r.status_code == 401 and r.json()["code"] == "token_expired"
    # a public path answers as it answers anyone
    assert _client().get("/api/health", headers=_bearer(_expired(good))).status_code == 200
    assert _client().post("/api/local/logout", json={}, headers=_bearer("not.a.token")).status_code == 200
    # a person whose account is switched off is refused at the next request with a token made before
    platformdb.set_account_disabled(platformdb.account_by_email("owner@acme.pk")["id"], True)
    r = _client().get("/api/sites", headers=_bearer(good))
    assert r.status_code == 401 and r.json()["code"] == "session_ended"


# ================================================================== no open mode, and the request without a token

def test_a_request_without_a_token_is_told_to_sign_in_on_this_machine_too(db):
    assert auth.open_mode() is False
    for c in (_client(), _client(client=("127.0.0.1", 50000)), _client(client=("203.0.113.9", 50000))):
        r = c.get("/api/sites")
        assert r.status_code == 401 and r.json()["code"] == "sign_in_required" and r.json()["detail"] == "Sign in first"
        assert c.get("/api/health").status_code == 200
    state = _client().get("/api/auth/state")
    assert state.status_code == 200 and state.json()["user"] is None


# ================================================================== paths that need the person but no company

def test_a_person_with_no_company_reads_their_context_and_changes_their_password(db):
    identities.client("owner@acme.pk", None, company_role="owner")                 # the workspace has people
    lone = platformdb.create_account("lone@acme.pk", "Lone Person", identities.PASSWORD)
    sent = identity.issue_local(lone["id"], lone["email"], lone["name"])
    c = _client(headers=_bearer(sent))
    assert c.get("/api/sites").json()["code"] == "no_company"
    ctx = c.get("/api/local/context")
    assert ctx.status_code == 200 and ctx.json()["profile"]["id"] == lone["id"] and ctx.json()["memberships"] == []
    state = c.get("/api/auth/state")
    assert state.status_code == 200 and state.json()["user"] is None
    r = c.post("/api/local/password", json={"current": identities.PASSWORD, "new": "another long password 2"})
    assert r.status_code == 200 and r.json()["changed"] is True and r.json()["access_token"]
    # without a token, or with one run out, these are refused as any signed-in path is
    assert _client().get("/api/local/context").json()["code"] == "sign_in_required"
    assert _client().get("/api/local/context", headers=_bearer(_expired(sent))).json()["code"] == "token_expired"
    # an account switched off: its sign-in state is refused too, though no company was ever chosen for it
    platformdb.set_account_disabled(lone["id"], True)
    r = _client(headers=_bearer(r.json()["access_token"])).get("/api/auth/state")
    assert r.status_code == 401 and r.json()["code"] == "session_ended"


def test_a_person_with_no_company_has_the_rate_limit_of_a_person(db, monkeypatch):
    identities.account("first@acme.pk")
    lone = platformdb.create_account("lone@acme.pk", "Lone Person", identities.PASSWORD)
    c = _client(headers=_bearer(identity.issue_local(lone["id"], lone["email"], lone["name"])))
    monkeypatch.setitem(auth.RATE_LIMITS, "user", 2)
    assert [c.get("/api/local/context").status_code for _ in range(2)] == [200, 200]
    r = c.get("/api/local/context")
    assert r.status_code == 429 and "from this account" in r.json()["detail"] and r.headers["retry-after"]


def test_local_sign_in_is_public_and_its_session_works_through_the_guard(db):
    identities.token("owner@acme.pk", None, company_role="owner", name="Acme Owner")
    r = _client().post("/api/local/session", json={"email": "owner@acme.pk", "password": identities.PASSWORD})
    assert r.status_code == 200, r.text
    me = _client().get("/api/auth/state", headers=_bearer(r.json()["access_token"])).json()["user"]
    assert me["email"] == "owner@acme.pk" and me["role"] == "admin"
    r = _client().post("/api/local/session", json={"email": "owner@acme.pk", "password": "not the password"})
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


# ================================================================== a site's API key: its own site, its scopes, its company

def _key(c: TestClient, site: str, scopes: list[str], kind: str = "partner") -> str:
    r = c.post("/api/platform/keys", json={"name": f"key {len(scopes)} {kind}", "scopes": scopes, "site_id": site, "kind": kind})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_an_api_key_reaches_its_own_sites_paths_within_its_scopes_and_nothing_else(db):
    alpha = identities.company("Alpha Energy", own_file=True)
    owner = identities.client("owner@alpha.pk", None, alpha, "owner", raise_server_exceptions=False)
    mill = owner.post("/api/sites", json={"name": "Mill", "disco": "FESCO"}).json()["id"]
    mall = owner.post("/api/sites", json={"name": "Mall", "disco": "LESCO"}).json()["id"]
    token = _key(owner, mill, ["sites:read"])
    k = _client(headers=_bearer(token))
    assert k.get(f"/api/sites/{mill}").status_code == 200
    r = k.get(f"/api/sites/{mall}")
    assert r.status_code == 403 and r.json()["code"] == "wrong_site"
    for path in ("/api/sites", "/api/fleet", "/api/alerts", "/api/users"):              # the company's collections: never a key's
        r = k.get(path)
        assert r.status_code == 403 and r.json()["code"] == "out_of_scope", (path, r.text)
    r = k.get(f"/api/sites/{mill}/plan")                                                   # a scope it does not hold
    assert r.status_code == 403 and r.json()["code"] == "out_of_scope"
    r = k.put(f"/api/sites/{mill}", json={"name": "Renamed"})                              # a change, which no read scope makes
    assert r.status_code == 403 and r.json()["code"] == "out_of_scope"
    assert "x-gridlytics-role" not in k.get(f"/api/sites/{mill}").headers                 # a key holds no role
    # a key that is not known, made up or revoked
    for sent in (token[:-4] + "abcd", "glk_" + "0" * 40):
        r = _client().get(f"/api/sites/{mill}", headers=_bearer(sent))
        assert r.status_code == 401 and r.json()["code"] == "invalid_key" and r.headers["www-authenticate"] == "Bearer", sent
    keys = owner.get("/api/platform/keys").json()
    held = next(x for x in (keys["keys"] if isinstance(keys, dict) else keys) if x.get("site_id") == mill)
    assert owner.delete(f"/api/platform/keys/{held['id']}").status_code == 200
    r = k.get(f"/api/sites/{mill}")
    assert r.status_code == 401 and r.json()["code"] == "invalid_key"


def test_the_edge_paths_take_a_sites_key_and_a_key_of_a_company_switched_off_is_refused(db):
    alpha = identities.company("Alpha Energy", own_file=True)
    owner = identities.client("owner@alpha.pk", None, alpha, "owner", raise_server_exceptions=False)
    mill = owner.post("/api/sites", json={"name": "Mill", "disco": "FESCO"}).json()["id"]
    # a person's session never writes the edge paths, the owner's neither; no token at all is told the edge needs a key
    r = owner.post(f"/api/edge/{mill}/heartbeat", json={})
    assert r.status_code == 403 and r.json()["code"] == "key_required"
    r = _client().post(f"/api/edge/{mill}/heartbeat", json={})
    assert r.status_code == 401 and r.json()["code"] == "key_required" and r.headers["www-authenticate"] == "Bearer"
    token = _key(owner, mill, ["sites:read"])
    platformdb.update_company(alpha["id"], enabled=False)
    api._KEY_CACHE.clear()
    r = _client().get(f"/api/sites/{mill}", headers=_bearer(token))
    assert r.status_code == 403 and r.json()["code"] == "company_disabled" and "Alpha Energy" in r.json()["detail"]
