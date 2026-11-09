"""Round 7 (audit), package auth-security: one regression test per finding, named by the audit's id.

Each test works in a data folder of its own (the `world` fixture of test_fix_api.py: a company file with two sites and a member of every role)."""
from __future__ import annotations

import json

from test_fix_api import world  # noqa: F401  (world is a fixture)


def _keys(obj) -> set[str]:
    """Every key at any depth of an answer."""
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


# ------------------------------------------------------------------------------------------------ security-roles-01 (tracker-and-docs-02)

def test_the_events_route_gives_no_fuel_cost_to_a_viewer_or_a_key(world):
    sid = world.site
    assert world.c["operator"].post(f"/api/sites/{sid}/genset/fuel", json={"litres": 50, "day": "2026-11-05", "cost_rs": 14000, "note": "tanker"}).status_code == 200
    # the administrator still sees what the fuel cost
    mine = world.c["admin"].get(f"/api/sites/{sid}/events").json()
    assert [e["detail"]["cost_rs"] for e in mine if e["type"] == "fuel_log"] == [14000]
    # a viewer and a site key holding sites:read see the litres, never the rupees
    seen = world.c["viewer"].get(f"/api/sites/{sid}/events")
    keyed = world.anon.get(f"/api/sites/{sid}/events", headers=world.key(["sites:read"]))
    for r in (seen, keyed):
        assert r.status_code == 200, r.text
        logs = [e for e in r.json() if e["type"] == "fuel_log"]
        assert [e["detail"]["litres"] for e in logs] == [50]
        assert "cost_rs" not in _keys(r.json()) and "14000" not in json.dumps(r.json())


# ------------------------------------------------------------------------------------------------ security-roles-02 (the filter is the API's)

def test_every_answer_is_filtered_for_a_viewer_and_a_count_is_not_money(world):
    """The guard filters every JSON answer (test_role_walk walks every route); a bare "total" is a count, so the viewer's alert badge keeps it."""
    from gridlytics import money
    assert not money.is_money("total") and money.is_money("total_rs")
    for role in ("admin", "viewer"):
        counts = world.c[role].get("/api/alerts/counts")
        assert counts.status_code == 200 and isinstance(counts.json()["total"], int), (role, counts.text)
    # the routes that never called out() are filtered all the same: the native capture's answer of a viewer, through a key as well
    sid = world.site
    assert world.c["viewer"].get(f"/api/sites/{sid}/native").status_code == 200
    assert world.anon.get(f"/api/sites/{sid}/overrides", headers=world.key(["sites:read"])).status_code == 200


# ------------------------------------------------------------------------------------------------ role-operator-06 (pages-C-20)

#: The money settings and the figures made from them alone (the wear price is the replacement cost over the warranty)
MONEY_SETTINGS = {"voll", "voll_rs_per_kwh", "replacement_cost_per_kwh", "c_cyc", "c_start", "c_start_rs", "c_runhr", "c_runhr_rs", "c_om", "c_om_rs_per_kwh",
                  "c_short"}


def test_an_operator_sees_costs_but_not_the_money_settings(world):
    sid, a, op, an = world.site, world.c["admin"], world.c["operator"], world.c["analyst"]
    assert a.put(f"/api/sites/{sid}", json={"voll_rs_per_kwh": 650}).status_code == 200
    battery = {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}
    assert a.put(f"/api/sites/{sid}/assets/battery", json={**battery, "replacement_cost_per_kwh": 61000}).status_code == 200
    assert a.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 150, "c_start_rs": 1700, "c_runhr_rs": 450, "c_om_rs_per_kwh": 9}).status_code == 200
    pages = (f"/api/sites/{sid}", f"/api/sites/{sid}/battery", f"/api/sites/{sid}/genset", "/api/sites")
    for url in pages:
        assert op.get(url).status_code == 200, url
        assert MONEY_SETTINGS & _keys(op.get(url).json()) == set(), url
    # the administrator and the analyst read them (plan 9.4: "Analyst: everything including money settings")
    for c in (a, an):
        assert {"replacement_cost_per_kwh", "c_cyc"} <= _keys(c.get(f"/api/sites/{sid}/battery").json())
        assert {"voll_rs_per_kwh", "c_start_rs"} <= _keys(c.get(f"/api/sites/{sid}").json())
    # costs stay the operator's: the genset's estimated fuel cost and the fuel price
    genset = op.get(f"/api/sites/{sid}/genset").json()
    assert "fuel_cost_rs" in genset["kpis"] and genset["fuel_price"]["price_per_l"] is not None
    assert {r: c.get("/api/meta").json()["permissions"]["see_money_settings"] for r, c in world.c.items()} == \
        {"admin": True, "operator": False, "analyst": True, "viewer": False}


def test_an_operator_saves_the_equipment_without_the_money_settings_it_cannot_see(world):
    """The settings form fills a field the answer left out with its default and sends the asset back whole: the stored money settings are kept, and the
    operator's change of what it may edit is saved."""
    sid, a, op = world.site, world.c["admin"], world.c["operator"]
    assert a.put(f"/api/sites/{sid}/assets/battery", json={"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100, "replacement_cost_per_kwh": 61000}).status_code == 200
    assert a.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 150, "c_start_rs": 1700, "c_runhr_rs": 450, "c_om_rs_per_kwh": 9}).status_code == 200
    r = op.put(f"/api/sites/{sid}/assets/battery", json={"nameplate_kwh": 220, "p_ch_kw": 100, "p_dis_kw": 100, "replacement_cost_per_kwh": 52000,
                                                         "warranted_cycles": 6000, "warranted_depth": 0.8})
    assert r.status_code == 200, r.text
    r = op.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 160, "c_start_rs": 1500, "c_runhr_rs": 400, "c_om_rs_per_kwh": 8})
    assert r.status_code == 200, r.text
    assets = a.get(f"/api/sites/{sid}").json()["assets"]
    assert (assets["battery"]["nameplate_kwh"], assets["battery"]["replacement_cost_per_kwh"]) == (220, 61000)
    assert (assets["genset"]["rated_kw"], assets["genset"]["c_start_rs"], assets["genset"]["c_runhr_rs"], assets["genset"]["c_om_rs_per_kwh"]) == (160, 1700, 450, 9)
    # a figure other than the form's default is a change, and a change is still an administrator's
    r = op.put(f"/api/sites/{sid}/assets/battery", json={"nameplate_kwh": 220, "p_ch_kw": 100, "p_dis_kw": 100, "replacement_cost_per_kwh": 40000})
    assert r.status_code == 403 and "administrator" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ role-viewer-19

def _saving_day(saving: float, litres: float) -> dict:
    return {"saving_rs": saving, "claimed_rs": saving, "levers": {"peak_window": saving}, "fidelity": "confirmed", "litres_saved": litres,
            "actual": {"cost_rs": 30000.0, "litres": 40.0, "peak_kw": 180.0}, "baseline": {"cost_rs": 30000.0 + saving, "litres": 40.0 + litres},
            "peak_kw_actual": 180.0, "peak_kw_baseline": 190.0, "mdi_rate": 0.0, "unserved_kwh_actual": 0.0, "unserved_kwh_baseline": 0.0}


def test_a_viewer_reads_the_litres_saved_and_the_verified_days_but_no_rupee(world):
    from gridlytics import money, store
    sid = world.site
    with world.use(), store.connect() as con:
        for day, (saving, litres) in {"2026-11-02": (4321.0, 12.5), "2026-11-03": (2345.0, 7.5)}.items():
            for base in ("native", "tuned"):
                con.execute("INSERT OR REPLACE INTO savings_days (site_id, day_local, baseline, data, computed) VALUES (?,?,?,?,?)",
                            (sid, day, base, json.dumps(_saving_day(saving, litres)), store._now()))
    url = f"/api/sites/{sid}/savings?start=2026-11-01&end=2026-11-05"
    mine = world.c["admin"].get(url).json()
    assert mine["baselines"]["native"]["saving_rs"] == 6666.0
    seen = world.c["viewer"].get(url).json()
    assert seen["hidden_for_role"] is True
    native = seen["baselines"]["native"]
    assert (native["litres_saved"], native["verified_days"]) == (20.0, 2)
    assert seen["final_conditions"]["verified_days"] == 2
    assert [(d["day"], d["litres_actual"], d["litres_native"]) for d in seen["days"]] == [("2026-11-02", 40.0, 52.5), ("2026-11-03", 40.0, 47.5)]
    assert {k for k in _keys(seen) if money.is_money(k)} == set()
    assert not {4321.0, 2345.0, 6666.0, 30000.0} & {v for d in seen["days"] for v in d.values()} and "6666" not in json.dumps(seen)


# ------------------------------------------------------------------------------------------------ security-roles-16

def _member(world, email: str) -> dict:
    return next(u for u in world.c["admin"].get("/api/users").json() if u["email"] == email)


def test_an_administrator_grants_a_named_viewer_the_costs_and_takes_them_back(world):
    """Plan Part 10.5: "A named viewer may be granted see_costs by an admin." The grant is the costs alone: the fee, the money settings, exports and every
    change stay the roles'; a grant is a viewer's, and a viewer made another role loses it."""
    from gridlytics import store
    sid, a, op, v = world.site, world.c["admin"], world.c["operator"], world.c["viewer"]
    assert a.put(f"/api/sites/{sid}", json={"voll_rs_per_kwh": 650}).status_code == 200
    assert op.post(f"/api/sites/{sid}/genset/fuel", json={"litres": 50, "day": "2026-11-05", "cost_rs": 14000, "note": "tanker"}).status_code == 200
    viewer = _member(world, "viewer@fix.test")
    assert viewer["grants"] == []

    def costs() -> bool:
        return v.get("/api/auth/state").json()["can"]["see_costs"]

    assert not costs() and v.get("/api/tariffs").status_code == 403
    # only an administrator grants, only the costs, and only as a list of grants
    assert op.put(f"/api/users/{viewer['id']}", json={"grants": ["see_costs"]}).status_code == 403
    for wrong in (["see_fee"], ["export"], "see_costs", [1]):
        r = a.put(f"/api/users/{viewer['id']}", json={"grants": wrong})
        assert r.status_code == 422 and "see costs" in r.json()["detail"], (wrong, r.text)
    r = a.put(f"/api/users/{viewer['id']}", json={"grants": ["see_costs"]})
    assert r.status_code == 200 and r.json()["grants"] == ["see_costs"] and r.json()["role"] == "viewer", r.text
    assert _member(world, "viewer@fix.test")["grants"] == ["see_costs"]
    # the viewer now reads the costs (a fuel log's rupees, the tariff library), and nothing else of the roles: no fee, no money setting, no export, no change
    can = v.get("/api/auth/state").json()["can"]
    assert can["see_costs"] and not any(can[p] for p in ("see_fee", "see_money_settings", "export", "edit_sites", "manage_users", "apply_plan"))
    assert v.get("/api/meta").json()["permissions"]["see_costs"] is True
    assert [e["detail"]["cost_rs"] for e in v.get(f"/api/sites/{sid}/events").json() if e["type"] == "fuel_log"] == [14000]
    assert v.get("/api/tariffs").status_code == 200
    site = v.get(f"/api/sites/{sid}").json()
    assert "voll_rs_per_kwh" not in _keys(site) and "fee" not in _keys(site)
    assert v.put(f"/api/sites/{sid}", json={"name": "Mill Renamed"}).status_code == 403
    # the trail says who granted what
    with world.use():
        trail = [x for x in store.list_audit() if x["action"] == "changed a user" and x["target"] == "viewer@fix.test"]
    assert trail and trail[0]["detail"]["grants"] == ["see_costs"] and trail[0]["detail"]["grants_before"] == []
    # taken back
    r = a.put(f"/api/users/{viewer['id']}", json={"grants": []})
    assert r.status_code == 200 and r.json()["grants"] == []
    assert not costs() and v.get("/api/tariffs").status_code == 403
    assert "cost_rs" not in _keys(v.get(f"/api/sites/{sid}/events").json())


def test_a_grant_is_a_viewers_and_ends_when_the_role_changes(world):
    a = world.c["admin"]
    operator = _member(world, "operator@fix.test")
    # the other roles see the costs already
    r = a.put(f"/api/users/{operator['id']}", json={"grants": ["see_costs"]})
    assert r.status_code == 422 and "viewer" in r.json()["detail"], r.text
    r = a.put(f"/api/users/{operator['id']}", json={"role": "viewer", "grants": ["see_costs"]})
    assert r.status_code == 200 and (r.json()["role"], r.json()["grants"]) == ("viewer", ["see_costs"]), r.text
    # made an analyst, the grant ends; made a viewer again, it does not come back by itself
    assert a.put(f"/api/users/{operator['id']}", json={"role": "analyst"}).json()["grants"] == []
    again = a.put(f"/api/users/{operator['id']}", json={"role": "viewer"}).json()
    assert (again["role"], again["grants"]) == ("viewer", [])
    assert world.c["operator"].get("/api/auth/state").json()["can"]["see_costs"] is False
    # a refusal names the viewers who were granted the costs
    r = world.c["operator"].get("/api/tariffs")
    assert r.status_code == 403 and "a viewer allowed to see costs" in r.json()["detail"], r.text


def test_a_grant_is_kept_in_the_company_file_and_a_key_never_holds_one(world, tmp_path):
    """The grant is a column of the member's row (schema version 3); a machine key carries scopes, never a role's permission or a grant."""
    from gridlytics import auth, store, store_members
    with world.use(), store.connect() as con:
        assert "grants" in {r[1] for r in con.execute("PRAGMA table_info(members)").fetchall()}
    with world.use():
        with __import__("pytest").raises(ValueError):
            store_members.update_member(_member(world, "viewer@fix.test")["id"], grants=["export"])
    token = auth.current.set({"id": "key:x", "role": "viewer", "api_key": True, "grants": ["see_costs"]})
    try:
        assert auth.can("see_costs") is False
    finally:
        auth.current.reset(token)


# ------------------------------------------------------------------------------------------------ pages-A-02 (parity-supabase-auth-02)

def _sign_in(email: str, password: str, address: str = "testclient"):
    from fastapi.testclient import TestClient
    from gridlytics import api
    return TestClient(api.app, raise_server_exceptions=False, client=(address, 50000)).post("/api/local/session", json={"email": email, "password": password})


def test_an_administrator_issues_a_one_time_password_that_opens_only_the_password_change(world):
    """Plan Part 9.3: until email exists, an administrator issues a one-time password with a forced change at the next sign-in. Every session of the account
    ends; with it only the sign-in's own paths answer, until the person chooses their own; the trail says so and never holds the password."""
    import identities
    from gridlytics import platform as platformdb
    from gridlytics import store
    a, anon = world.c["admin"], world.anon
    op = _member(world, "operator@fix.test")["id"]
    old = _sign_in("operator@fix.test", identities.PASSWORD).json()
    r = a.post(f"/api/users/{op}/password")
    assert r.status_code == 200, r.text
    one_time = r.json()["password"]
    assert len(one_time) == 19 and platformdb.password_problem(one_time) is None and r.json()["user_id"] == op
    assert platformdb.check_sign_in("operator@fix.test", identities.PASSWORD) is None                     # the old password is gone
    assert anon.post("/api/local/refresh", json={"refresh_token": old["refresh_token"]}).status_code == 401  # and every session with it
    s = _sign_in("operator@fix.test", one_time)
    assert s.status_code == 200, s.text
    h = identities.headers(s.json()["access_token"], world.company)
    r = anon.get("/api/sites", headers=h)
    assert r.status_code == 403 and r.json()["code"] == "password_change_required" and "one-time password" in r.json()["detail"], r.text
    assert anon.get("/api/local/context", headers=h).json()["profile"]["must_change_password"] is True
    state = anon.get("/api/auth/state", headers=h)
    assert state.status_code == 200 and state.json()["refusal"]["code"] == "password_change_required", state.text
    changed = anon.post("/api/local/password", headers=h, json={"current": one_time, "new": "the operator's own password"})
    assert changed.status_code == 200, changed.text
    h = identities.headers(changed.json()["access_token"], world.company)
    assert anon.get("/api/sites", headers=h).status_code == 200
    assert anon.get("/api/local/context", headers=h).json()["profile"]["must_change_password"] is False
    with world.use():
        trail = store.list_audit()
    assert any(e["action"] == "issued a one-time password" and e["target"] == "operator@fix.test" for e in trail)
    assert one_time not in json.dumps(trail)


def test_an_administrator_clears_a_sign_in_lockout(world):
    from gridlytics import auth
    import identities
    a = world.c["admin"]
    an = _member(world, "analyst@fix.test")["id"]
    for _ in range(auth.MAX_TRIES):
        assert _sign_in("analyst@fix.test", "a wrong password 1").status_code == 401
    assert _sign_in("analyst@fix.test", identities.PASSWORD).status_code == 429
    r = world.c["operator"].post(f"/api/users/{an}/unlock")
    assert r.status_code == 403 and "administrator" in r.json()["detail"], r.text
    r = a.post(f"/api/users/{an}/unlock")
    assert r.status_code == 200 and r.json() == {"cleared": True}, r.text
    assert _sign_in("analyst@fix.test", identities.PASSWORD).status_code == 200
    # a one-time password clears the lockout as well: the person can sign in with it at once
    for _ in range(auth.MAX_TRIES):
        assert _sign_in("analyst@fix.test", "a wrong password 1").status_code == 401
    one_time = a.post(f"/api/users/{an}/password").json()["password"]
    assert _sign_in("analyst@fix.test", one_time).status_code == 200


def test_sign_in_help_is_never_for_the_owner_oneself_or_a_person_also_in_another_company(world):
    import identities
    from gridlytics import platform as platformdb
    a = world.c["admin"]                                                           # the company's owner
    second = identities.client("second@fix.test", "admin", world.company, "admin", raise_server_exceptions=False)
    owner, me, viewer = (_member(world, e)["id"] for e in ("admin@fix.test", "second@fix.test", "viewer@fix.test"))
    assert world.c["operator"].post(f"/api/users/{viewer}/password").status_code == 403
    r = second.post(f"/api/users/{owner}/password")
    assert r.status_code == 409 and r.json()["code"] == "owner" and "OnyxLeaf" in r.json()["detail"], r.text
    assert second.post(f"/api/users/{owner}/unlock").status_code == 409
    r = second.post(f"/api/users/{me}/password")
    assert r.status_code == 422 and "My account" in r.json()["detail"], r.text
    elsewhere = identities.company("Elsewhere", own_file=True)
    platformdb.add_membership(viewer, elsewhere["id"], "member", "active")
    r = a.post(f"/api/users/{viewer}/password")
    assert r.status_code == 409 and r.json()["code"] == "other_company" and "OnyxLeaf" in r.json()["detail"], r.text
    assert platformdb.check_sign_in("viewer@fix.test", identities.PASSWORD)                                 # nothing changed
    assert a.post("/api/users/no-such-person/password").status_code == 404
    assert a.post(f"/api/users/{owner}/password", json={"password": "my choice of password"}).status_code == 422   # the server makes it


# ------------------------------------------------------------------------------------------------ founder-console-07 and founder-console-08

def _founder_world(monkeypatch, tmp_path):
    """The founder console of test_founder.py, set up with its made-up secrets, on a registry of the test's own; one company and one person in it."""
    import test_founder as tf
    from gridlytics import auth, store
    monkeypatch.delenv("GRIDLYTICS_DATA_DIR", raising=False)
    monkeypatch.delenv("GRIDLYTICS_TRUST_PROXY", raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "founder.db")
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_USERNAME", "founder")
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_PASSWORD_HASH", tf._HASH)
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_TOTP_SECRET", tf.TOTP_SECRET)
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", tf.SESSION_SECRET)
    auth.limiter().reset()
    c = tf._console()
    cid = tf._ok(c.post("/api/founder/companies", json={"name": "Meow Mills", "seats": 5, "enabled": True, "owner": tf.OWNER}), 201)["company"]["id"]
    sunia = tf._ok(c.post("/api/founder/users", json={"email": "sunia@meow.test", "name": "Sunia Ahmed", "password": "sunia first password",
                                                      "company_id": cid}), 201)["user"]
    return c, sunia


def test_a_password_the_founder_sets_clears_the_lockout(monkeypatch, tmp_path):
    """founder-console-07: a person locked out by five wrong passwords signs in at once with the password the founder sets (plan Part 9.3: the founder
    console does across companies what a company's administrator does)."""
    from gridlytics import auth
    c, sunia = _founder_world(monkeypatch, tmp_path)
    for _ in range(auth.MAX_TRIES):
        assert _sign_in("sunia@meow.test", "a wrong password 1").status_code == 401
    assert _sign_in("sunia@meow.test", "sunia first password").status_code == 429
    r = c.post(f"/api/founder/users/{sunia['id']}/password", json={"password": "sunia second password"})
    assert r.status_code == 200, r.text
    assert _sign_in("sunia@meow.test", "sunia second password").status_code == 200


def test_a_password_the_founder_sets_is_one_time_unless_said_otherwise(monkeypatch, tmp_path):
    """founder-console-08: the founder's reset is a one-time password: the person signs in with it and must choose their own before anything else opens.
    The founder can say it is not (one_time false), for an account of OnyxLeaf's own; the audit says which."""
    import identities
    from gridlytics import platform as platformdb
    c, sunia = _founder_world(monkeypatch, tmp_path)
    assert c.post(f"/api/founder/users/{sunia['id']}/password", json={"password": "sunia second password"}).status_code == 200
    assert platformdb.account(sunia["id"])["must_change"] is True
    s = _sign_in("sunia@meow.test", "sunia second password")
    assert s.status_code == 200, s.text
    h = identities.headers(s.json()["access_token"])
    r = _app().get("/api/sites", headers=h)
    assert r.status_code == 403 and r.json()["code"] == "password_change_required", r.text
    changed = _app().post("/api/local/password", headers=h, json={"current": "sunia second password", "new": "sunia's own password"})
    assert changed.status_code == 200, changed.text
    assert _app().get("/api/sites", headers=identities.headers(changed.json()["access_token"])).status_code == 200
    # one_time false: a password the person keeps
    r = c.post(f"/api/founder/users/{sunia['id']}/password", json={"password": "sunia third password", "one_time": False})
    assert r.status_code == 200 and platformdb.account(sunia["id"])["must_change"] is False, r.text
    for wrong in ({"password": "sunia third password", "one_time": "yes"}, {"password": "sunia third password", "temporary": True}):
        r = c.post(f"/api/founder/users/{sunia['id']}/password", json=wrong)
        assert r.status_code == 422, (wrong, r.text)
    sets = [e for e in platformdb.list_founder_audit() if e["action"] == "set a password"]
    assert [e["detail"].get("one_time") for e in sets] == [False, True]                      # newest first
    assert "sunia" not in json.dumps([e["detail"] for e in sets])


def _app():
    from fastapi.testclient import TestClient
    from gridlytics import api
    return TestClient(api.app, raise_server_exceptions=False)


# ------------------------------------------------------------------------------------------------ security-roles-09

def test_signing_out_ends_the_access_token_of_that_session_too(world):
    """Sign-out ended only the refresh token: the access token went on working for up to an hour. Now the access token names its session (the refresh
    token's family), and a session that ended refuses it at once, 401 session_ended; the person's other sessions go on."""
    import identities
    anon = world.anon
    one = _sign_in("operator@fix.test", identities.PASSWORD).json()
    two = _sign_in("operator@fix.test", identities.PASSWORD).json()
    h1, h2 = (identities.headers(s["access_token"], world.company) for s in (one, two))
    assert anon.get("/api/sites", headers=h1).status_code == 200
    assert anon.post("/api/local/logout", json={"refresh_token": one["refresh_token"]}).json() == {"signed_out": True}
    for path in ("/api/sites", "/api/local/context", "/api/auth/state"):
        r = anon.get(path, headers=h1)
        assert r.status_code == 401 and r.json()["code"] == "session_ended", (path, r.text)
    assert anon.get("/api/sites", headers=h2).status_code == 200                                  # the other session goes on
    # a renewed session's access tokens, the one of before the renewal too, end with it; a sign-out with the access token alone ends its session
    renewed = anon.post("/api/local/refresh", json={"refresh_token": two["refresh_token"]}).json()
    h3 = identities.headers(renewed["access_token"], world.company)
    assert anon.post("/api/local/logout", headers=h3).json() == {"signed_out": True}
    for h in (h2, h3):
        assert anon.get("/api/sites", headers=h).status_code == 401
    assert anon.post("/api/local/refresh", json={"refresh_token": renewed["refresh_token"]}).status_code == 401


def test_a_refresh_token_used_again_ends_the_access_tokens_of_its_session(world, monkeypatch):
    """A refresh token copied and used after its first use ends its whole session (RFC 9700): the access tokens of that session as well."""
    import identities
    from gridlytics import platform as platformdb
    anon = world.anon
    first = _sign_in("analyst@fix.test", identities.PASSWORD).json()
    renewed = anon.post("/api/local/refresh", json={"refresh_token": first["refresh_token"]}).json()
    h = identities.headers(renewed["access_token"], world.company)
    assert anon.get("/api/sites", headers=h).status_code == 200
    later = platformdb._clock() + platformdb.REUSE_SECONDS + 1
    monkeypatch.setattr(platformdb, "_clock", lambda: later)
    assert anon.post("/api/local/refresh", json={"refresh_token": first["refresh_token"]}).status_code == 401
    assert anon.get("/api/sites", headers=h).status_code == 401


# ------------------------------------------------------------------------------------------------ security-roles-10

def test_wrong_passwords_from_many_addresses_stop_the_account_but_not_where_it_signs_in(world):
    """The lockout counted per (account, address) only: N addresses bought 5N guesses. Now ACCOUNT_TRIES wrong passwords from every address together stop
    the account for LOCK_MINUTES (NIST SP 800-63B 5.2.2), except from an address it signed in from lately, so a stranger cannot lock the person out where
    they work; a good sign-in, or an administrator clearing the lockout, ends the count."""
    import identities
    from gridlytics import auth
    assert auth.MAX_TRIES < auth.ACCOUNT_TRIES <= 100
    assert _sign_in("viewer@fix.test", identities.PASSWORD, "10.0.0.1").status_code == 200        # the office, where the person signs in

    def guess_from_many(net: str):
        for n in range(auth.ACCOUNT_TRIES):
            assert _sign_in("viewer@fix.test", "a wrong password 1", f"{net}.{n // (auth.MAX_TRIES - 1)}").status_code == 401

    guess_from_many("203.0.113")
    r = _sign_in("viewer@fix.test", identities.PASSWORD, "198.51.100.9")
    assert r.status_code == 429 and int(r.headers["retry-after"]) == auth.LOCK_MINUTES * 60, r.text
    assert "this account" in r.json()["detail"] and "administrator" in r.json()["detail"]
    assert _sign_in("viewer@fix.test", identities.PASSWORD, "10.0.0.1").status_code == 200         # the office still signs in, which ends the count
    assert _sign_in("viewer@fix.test", identities.PASSWORD, "198.51.100.9").status_code == 200
    guess_from_many("192.0.2")
    assert _sign_in("viewer@fix.test", identities.PASSWORD, "198.51.100.10").status_code == 429
    viewer = _member(world, "viewer@fix.test")["id"]
    assert world.c["admin"].post(f"/api/users/{viewer}/unlock").status_code == 200
    assert _sign_in("viewer@fix.test", identities.PASSWORD, "198.51.100.10").status_code == 200


def test_a_forwarded_header_the_server_does_not_trust_is_said_once_in_the_log(world, caplog, monkeypatch):
    """Behind a proxy without GRIDLYTICS_TRUST_PROXY=1 every client counts as one address ("forwarded") for the lockout: the server says so in its log,
    once, naming the setting."""
    import logging
    from fastapi.testclient import TestClient
    from gridlytics import api
    from gridlytics import platform as platformdb
    monkeypatch.delenv("GRIDLYTICS_TRUST_PROXY", raising=False)
    monkeypatch.setattr(platformdb, "_proxy_said", False)
    c = TestClient(api.app, raise_server_exceptions=False)
    with caplog.at_level(logging.WARNING, logger="gridlytics"):
        for n in range(2):
            c.post("/api/local/session", json={"email": "viewer@fix.test", "password": "a wrong password 1"}, headers={"X-Forwarded-For": f"198.51.100.{n}"})
    said = [r.getMessage() for r in caplog.records if "GRIDLYTICS_TRUST_PROXY" in r.getMessage()]
    assert len(said) == 1 and "forwarded-allow-ips" in said[0], said


# ------------------------------------------------------------------------------------------------ role-analyst-32 (UI-18 My account: sessions and notifications)

def _signed_in_from(email: str, address: str, agent: str) -> dict:
    import identities
    from fastapi.testclient import TestClient
    from gridlytics import api
    c = TestClient(api.app, raise_server_exceptions=False, client=(address, 50000))
    r = c.post("/api/local/session", json={"email": email, "password": identities.PASSWORD}, headers={"User-Agent": agent})
    assert r.status_code == 200, r.text
    return r.json()


def test_my_account_lists_my_open_sessions_and_signs_out_the_others(world):
    """UI-18: My account shows the person's open sessions (where and when each started, when it was last renewed, this device marked) and signs out one, or
    every other; the access tokens of a session signed out end with it (security-roles-09). Nobody reads or ends another person's sessions."""
    import identities
    anon = world.anon
    office = _signed_in_from("analyst@fix.test", "10.0.0.7", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36")
    phone = _signed_in_from("analyst@fix.test", "10.0.0.8", "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1")
    laptop = _signed_in_from("analyst@fix.test", "10.0.0.9", "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Firefox/131.0")
    other = _signed_in_from("operator@fix.test", "10.0.0.10", "curl/8.0")
    h = {k: identities.headers(s["access_token"], world.company) for k, s in (("office", office), ("phone", phone), ("laptop", laptop), ("other", other))}
    r = anon.get("/api/local/sessions", headers=h["office"])
    assert r.status_code == 200, r.text
    listed = r.json()["sessions"]
    assert len(listed) == 3 and sorted(s["address"] for s in listed) == ["10.0.0.7", "10.0.0.8", "10.0.0.9"]
    mine = next(s for s in listed if s["current"])
    assert mine["address"] == "10.0.0.7" and "Chrome" in mine["device"] and [s["current"] for s in listed].count(True) == 1
    assert all(isinstance(s[k], (int, float)) for s in listed for k in ("started", "last_active", "ends")) and set(mine) == {
        "id", "current", "started", "last_active", "ends", "address", "device"}
    # sign out the phone by its id; a session of another person's is not found
    phone_id = next(s["id"] for s in listed if s["address"] == "10.0.0.8")
    other_id = next(s["id"] for s in anon.get("/api/local/sessions", headers=h["other"]).json()["sessions"])
    assert anon.post("/api/local/sessions/end", headers=h["office"], json={"id": other_id}).status_code == 404
    assert anon.get("/api/sites", headers=h["other"]).status_code == 200
    for wrong in ({}, {"id": 5}, {"id": phone_id, "x": 1}):
        assert anon.post("/api/local/sessions/end", headers=h["office"], json=wrong).status_code == 422, wrong
    r = anon.post("/api/local/sessions/end", headers=h["office"], json={"id": phone_id})
    assert r.status_code == 200 and r.json() == {"ended": 1}, r.text
    assert anon.get("/api/sites", headers=h["phone"]).status_code == 401
    assert anon.post("/api/local/refresh", json={"refresh_token": phone["refresh_token"]}).status_code == 401
    # every other session: the laptop ends, this device stays, the other person is untouched
    r = anon.post("/api/local/sessions/end-others", headers=h["office"])
    assert r.status_code == 200 and r.json() == {"ended": 1}, r.text
    assert anon.get("/api/sites", headers=h["laptop"]).status_code == 401
    assert anon.get("/api/sites", headers=h["office"]).status_code == 200 and anon.get("/api/sites", headers=h["other"]).status_code == 200
    assert [s["address"] for s in anon.get("/api/local/sessions", headers=h["office"]).json()["sessions"]] == ["10.0.0.7"]
    # a renewal keeps where the session started and moves when it was last active
    renewed = anon.post("/api/local/refresh", json={"refresh_token": office["refresh_token"]}).json()
    again = anon.get("/api/local/sessions", headers=identities.headers(renewed["access_token"], world.company)).json()["sessions"]
    assert len(again) == 1 and again[0]["current"] and again[0]["address"] == "10.0.0.7" and again[0]["id"] == mine["id"]
    assert anon.get("/api/local/sessions").status_code == 401


def test_my_account_keeps_my_notification_choices(world):
    """UI-18: notification preferences. Each person chooses, in their company, the kinds of alert they follow and whether they want them by email too;
    kept per person, any role, never a key's."""
    a, v = world.c["admin"], world.c["viewer"]
    first = v.get("/api/platform/notifications")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["categories"][:3] == ["Data", "Control", "Reserve"] and body["follow"] == body["categories"] and body["email"] is False
    r = v.put("/api/platform/notifications", json={"follow": ["Data", "Genset"], "email": True})
    assert r.status_code == 200 and (r.json()["follow"], r.json()["email"]) == (["Data", "Genset"], True), r.text
    assert (v.get("/api/platform/notifications").json()["follow"], a.get("/api/platform/notifications").json()["follow"]) == (
        ["Data", "Genset"], body["categories"])                                          # the viewer's own, not the administrator's
    for wrong in ({"follow": ["Weather"]}, {"follow": "Data"}, {"email": "yes"}, {"follow": [], "sms": True}, []):
        r = v.put("/api/platform/notifications", json=wrong)
        assert r.status_code == 422 and len(r.json()["detail"].split()) >= 3, (wrong, r.text)
    assert v.put("/api/platform/notifications", json={"follow": []}).json() == {**body, "follow": [], "email": True}
    assert world.anon.get("/api/platform/notifications", headers=world.key(["sites:read"])).status_code == 403


def test_the_members_refusal_of_a_name_email_or_password_says_who_changes_them(world):
    """parity-supabase-auth-02: PUT /api/users sent an administrator to My account for another member's name, email or password, which My account cannot
    change. OnyxLeaf changes a name or an email (accounts are preassigned); a forgotten password is Sign-in help's one-time password."""
    a = world.c["admin"]
    op = _member(world, "operator@fix.test")["id"]
    for field, value in (("name", "Someone Else"), ("email", "else@fix.test"), ("password", "another password 1")):
        r = a.put(f"/api/users/{op}", json={field: value})
        assert r.status_code == 422 and "OnyxLeaf" in r.json()["detail"] and "Sign-in help" in r.json()["detail"], (field, r.text)
        assert "My account" not in r.json()["detail"]
