"""Regression tests of the API group's confirmed review findings (6 November 2026): the edge paths take a key, keys see no money and no other site,
override clearing follows its kind, money settings are an administrator's, the CSV upload stays off the event loop, reports never read site names as
markup, made-up keys meet the address limit, exports are audited, and the alert rules (acting on old alerts, genset requests, the unconfirmed-apply
alert, per-source staleness) behave.

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own, so no person or row reaches the shared test database."""
from __future__ import annotations

import asyncio
import json
import secrets
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gridlytics import alerts, api, auth, delivery, money, plans, reports, sites, store, telemetry, verify
from gridlytics import platform as platformdb

PKT = timezone(timedelta(hours=5))
ROLES = ("admin", "operator", "analyst", "viewer")


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    import identities
    co = identities.company("Fix company", own_file=True)
    db = platformdb.company_file(co)
    db.parent.mkdir(parents=True, exist_ok=True)
    with store.use_company(db):
        one = sites.create({"name": "Mill One", "disco": "FESCO", "genset_start_control": "manual", "critical_load_kw": [20.0] * 24}, "test")["id"]
        two = sites.create({"name": "Secret Site Two", "address": "12 Hidden Road"}, "test")["id"]
        sites.put_connection(one, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "test")
        sites.put_asset(one, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "test")
        sites.put_asset(one, "genset", {"rated_kw": 150}, "test")
    clients = {}
    for role in ROLES:
        owner = role == "admin"
        clients[role] = identities.client(f"{role}@fix.test", None if owner else role, co, "owner" if owner else "member", raise_server_exceptions=False)
    w = types.SimpleNamespace(db=db, site=one, other=two, c=clients, company=co)

    def use():
        return store.use_company(db)

    def key(scopes, role="admin", site=None):
        r = clients["admin"].post("/api/platform/keys", json={"name": f"k{secrets.token_hex(3)}", "role": role, "scopes": scopes, "site_id": site or one,
                                                               "kind": "edge"})
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["token"]}

    w.use, w.key = use, key
    w.anon = TestClient(api.app, raise_server_exceptions=False)
    return w


def _batch(seq: int, rows=None, columns=("ts", "grid_present", "pv_power_w")) -> dict:
    return {"contract": "ems-edge/1.0", "seq": seq, "sent_at": "2026-11-06T10:00:00Z", "columns": list(columns),
            "rows": rows if rows is not None else [[_now() // 60 * 60 - 120, 1, 900]]}


def _audit_actions(w) -> list[str]:
    with w.use():
        return [a["action"] for a in store.list_audit()]


# ------------------------------------------------------------------------------------------------ security-edge-routes-no-role-check

def test_the_edge_paths_refuse_every_persons_session_and_take_the_sites_key(world):
    sid = world.site
    caps = {"contract": "ems-edge/1.0", "seq": 1, "sent_at": "2026-11-06T10:00:00Z", "edge_version": "1.0", "tier": "closed_loop"}
    events = {"contract": "ems-edge/1.0", "seq": 1, "sent_at": "2026-11-06T10:00:00Z", "events": [{"ts": "2026-11-06T10:00:00Z", "type": "readback_mismatch"}]}
    beat = {"contract": "ems-edge/1.0", "seq": 1, "sent_at": "2026-11-06T10:00:00Z", "uptime_s": 5}
    ack = {"contract": "ems-edge/1.0", "seq": 1, "sent_at": "2026-11-06T10:00:00Z", "plan_id": "p", "status": "received"}
    for role in ROLES:
        c = world.c[role]
        huge = _batch(2 ** 62)
        assert c.post(f"/api/edge/{sid}/telemetry", json=huge).status_code == 403, role
        assert c.post(f"/api/v1/edge/{sid}/telemetry", json=huge).status_code == 403, role
        assert c.post(f"/api/edge/{sid}/capabilities", json=caps).status_code == 403, role
        assert c.post(f"/api/edge/{sid}/events", json=events).status_code == 403, role
        assert c.post(f"/api/edge/{sid}/heartbeat", json=beat).status_code == 403, role
        assert c.post(f"/api/edge/{sid}/ack", json=ack).status_code == 403, role
        r = c.get(f"/api/sites/{sid}/plans/current/message")
        assert r.status_code == 403, (role, r.status_code)
    assert world.anon.post(f"/api/edge/{sid}/telemetry", json=_batch(1)).status_code == 401
    with world.use():
        assert sites.get(sid)["tier_capable"] == "monitor"
        assert sites.overrides(sid) == []
        assert telemetry.latest(sid) is None
    # the site's own key is not black-holed by anything a session sent: its first batch is stored
    kh = world.key(["telemetry:write"], role="viewer")
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(2))
    assert r.status_code == 200 and r.json().get("accepted") == 1, r.text
    r = world.anon.post(f"/api/edge/{sid}/capabilities", headers=kh, json=caps)
    assert r.status_code == 200 and r.json()["tier_capable"] == "closed_loop"


# ------------------------------------------------------------------------------------------------ data-edge-seq-before-write

def test_an_edge_batch_that_failed_is_stored_when_sent_again_not_acked_as_a_duplicate(world, monkeypatch):
    sid = world.site
    kh = world.key(["telemetry:write"])
    # refused for its columns: the same sequence, corrected, is stored
    bad = _batch(7, columns=("ts", "pv_power_w", "no_such_column"), rows=[[_now() // 60 * 60 - 60, 1, 2]])
    assert world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=bad).status_code == 422
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(7))
    assert r.json().get("accepted") == 1 and not r.json().get("duplicate"), r.text
    # the write fails (a locked file): the edge is told, and its retry of the same sequence is written
    real = telemetry.ingest

    def failing(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(telemetry, "ingest", failing)
    assert world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(8)).status_code == 500
    monkeypatch.setattr(telemetry, "ingest", real)
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(8))
    assert r.status_code == 200 and r.json().get("accepted") == 1 and not r.json().get("duplicate"), r.text
    # and a true repeat is still a duplicate
    assert world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(8)).json()["duplicate"] is True
    assert world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(2 ** 62)).status_code == 422


# ------------------------------------------------------------------------------------------------ security-plan-message-leaks-money-to-viewer, security-keys-see-money

def _fake_plan(w, objective=435519.42) -> str:
    t0 = _now() // 900 * 900
    steps = [{"t": t0 + 900 * i, "local": "10:00", "reason": "idle", "detail": "", "c_imp": 40.5, "cost_rs": 12.0, "genset_on": False, "peak": False,
              "discharge_kw": 0.0, "charge_kw": 0.0} for i in range(4)]
    pid = "plan-" + secrets.token_hex(4)
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
    with w.use(), store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, objective_rs, summary, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (pid, w.site, iso(t0), iso(t0), iso(t0), iso(t0 + 3600), "a", "active", objective,
                                                         json.dumps({"status_label": "Following", "cost_rs": 99.0, "breakdown": {"energy_rs": 1.0}}),
                                                         json.dumps(steps), json.dumps({"days": [], "fallback": None})))
    return pid


def test_the_signed_plan_message_is_for_keys_only_and_carries_no_money(world, monkeypatch):
    sid = world.site
    calls = []

    def signed(site_id, *a, **k):
        calls.append(site_id)
        return delivery.sign(site_id, {"contract": "ems-edge/1.0", "type": "plan", "plan_id": "p1", "site_id": site_id,
                                       "notes": {"objective_rs": 435519.42, "reason": "Following"}})

    monkeypatch.setattr(plans, "signed_message", signed)
    for role in ROLES:
        assert world.c[role].get(f"/api/sites/{sid}/plans/current/message").status_code == 403
    assert calls == []                                         # no person's read is recorded as a send
    kh = world.key(["plans:read"], role="viewer")
    msg = world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh).json()
    assert calls == [sid]
    assert "objective_rs" not in json.dumps(msg) and msg["notes"] == {"reason": "Following"}
    with world.use():
        assert delivery.verify(msg, delivery.public_key(sid)["public_key"])


def test_a_key_gets_no_money_whatever_its_role(world):
    sid = world.site
    _fake_plan(world)
    kh = world.key(["sites:read", "plans:read"], role="admin")
    blob = json.dumps(world.anon.get(f"/api/sites/{sid}/plan", headers=kh).json()) + json.dumps(world.anon.get(f"/api/sites/{sid}", headers=kh).json())
    for k in ("c_imp", "cost_rs", "objective_rs", "energy_rs", "replacement_cost_per_kwh", "voll_rs_per_kwh", "c_start_rs"):
        assert f'"{k}"' not in blob, k
    assert '_rs"' not in blob
    assert '"steps"' in blob and '"Mill One"' in blob                  # the energy answer itself is there
    assert world.anon.get(f"/api/sites/{sid}/prices", headers=kh).status_code == 403
    assert world.anon.get(f"/api/sites/{sid}/bills", headers=kh).status_code == 403
    assert world.anon.get(f"/api/sites/{sid}/telemetry/export.csv", headers=kh).status_code == 403
    assert world.anon.get(f"/api/sites/{sid}/savings/export.csv", headers=kh).status_code == 403
    # an administrator still sees the plan's money
    assert '"objective_rs"' in json.dumps(world.c["admin"].get(f"/api/sites/{sid}/plan").json())


# ------------------------------------------------------------------------------------------------ security-key-lists-all-sites

def test_a_site_bound_key_cannot_list_the_companys_sites(world):
    kh = world.key(["sites:read"], role="viewer")
    for path in ("/api/sites", "/api/v1/sites", "/api/sites/", "/api/fleet"):
        r = world.anon.get(path, headers=kh)
        assert r.status_code == 403, (path, r.status_code)
        assert "Hidden Road" not in r.text
    assert world.anon.get(f"/api/sites/{world.site}", headers=kh).status_code == 200
    assert world.anon.get(f"/api/sites/{world.other}", headers=kh).json()["code"] == "wrong_site"


# ------------------------------------------------------------------------------------------------ security-operator-resumes-via-override-delete, data-operator-clears-admin-pause

def test_clearing_an_override_needs_the_permission_its_kind_needs(world):
    sid = world.site
    until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    with world.use():
        pause = sites.add_override(sid, {"kind": "pause_control", "until": until, "reason": "Edge reported an unknown device state"}, "edge")
    admin, operator = world.c["admin"], world.c["operator"]
    force = admin.post(f"/api/sites/{sid}/overrides", json={"kind": "force_charge", "until": until}).json()
    hold = operator.post(f"/api/sites/{sid}/overrides", json={"kind": "reserve", "value": 60, "until": until}).json()
    assert operator.delete(f"/api/sites/{sid}/overrides/{pause['id']}").status_code == 403
    assert operator.delete(f"/api/sites/{sid}/overrides/{force['id']}").status_code == 403
    assert operator.get(f"/api/sites/{sid}/control").json()["control"]["key"] == "paused"
    assert operator.delete(f"/api/sites/{sid}/overrides/no-such-id").status_code == 404
    assert world.c["viewer"].delete(f"/api/sites/{sid}/overrides/{hold['id']}").status_code == 403
    assert operator.delete(f"/api/sites/{sid}/overrides/{hold['id']}").status_code == 200
    assert operator.delete(f"/api/sites/{sid}/overrides/{hold['id']}").status_code == 404     # already cleared
    left = admin.delete(f"/api/sites/{sid}/overrides/{pause['id']}")
    assert left.status_code == 200 and [o["kind"] for o in left.json()] == ["force_charge"]
    assert admin.delete(f"/api/sites/{sid}/overrides/{force['id']}").json() == []
    assert "cleared an override" in _audit_actions(world)


# ------------------------------------------------------------------------------------------------ security-operator-edits-money-settings

def test_only_an_administrator_changes_money_settings(world):
    sid = world.site
    operator, admin = world.c["operator"], world.c["admin"]
    pack = {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}
    assert operator.put(f"/api/sites/{sid}/assets/battery", json={**pack, "replacement_cost_per_kwh": 500000, "warranted_cycles": 100}).status_code == 403
    assert operator.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 150, "c_start_rs": 0}).status_code == 403
    assert operator.put(f"/api/sites/{sid}", json={"voll_rs_per_kwh": 0}).status_code == 403
    assert operator.post("/api/sites", json={"name": "Cheap", "voll_rs_per_kwh": 1}).status_code == 403
    # the administrator sets them; an operator's edit of other fields keeps them, sent back unchanged or left out
    assert admin.put(f"/api/sites/{sid}/assets/battery", json={**pack, "replacement_cost_per_kwh": 60000}).status_code == 200
    assert admin.put(f"/api/sites/{sid}", json={"voll_rs_per_kwh": 800}).status_code == 200
    r = operator.put(f"/api/sites/{sid}/assets/battery", json={**pack, "soh": 0.9})
    assert r.status_code == 200, r.text
    bat = r.json()["assets"]["battery"]
    assert bat["soh"] == 0.9 and bat["replacement_cost_per_kwh"] == 60000
    assert operator.put(f"/api/sites/{sid}/assets/battery", json={**bat, "soh": 0.85}).status_code == 200
    r = operator.put(f"/api/sites/{sid}", json={"name": "Mill One East", "voll_rs_per_kwh": 800})
    assert r.status_code == 200 and r.json()["data"]["voll_rs_per_kwh"] == 800
    assert operator.post("/api/sites", json={"name": "New site"}).status_code == 200
    with world.use():
        assert sites.assets(sid)["battery"]["replacement_cost_per_kwh"] == 60000


# ------------------------------------------------------------------------------------------------ security-switchback-rupees-to-viewer

def test_the_switchback_result_hides_rupees_from_a_viewer(world, monkeypatch):
    res = {"blocks": [], "days": 12, "result": {"saving_rs_per_day_unadjusted": -2700.0, "saving_rs_per_day_adjusted": -2700.0, "plan_days": 6,
                                                  "native_days": 6}}
    monkeypatch.setattr(verify, "switchback_result", lambda site_id: json.loads(json.dumps(res)))
    seen = world.c["viewer"].get(f"/api/sites/{world.site}/switchback").json()
    assert "saving_rs" not in json.dumps(seen) and seen["result"]["plan_days"] == 6
    assert world.c["analyst"].get(f"/api/sites/{world.site}/switchback").json()["result"]["saving_rs_per_day_adjusted"] == -2700.0
    assert money.is_money("verified_saving_rs_per_month") and money.is_money("rs_per_kw") and not money.is_money("plan_days")


# ------------------------------------------------------------------------------------------------ security-report-markup-file-read

def test_a_site_name_is_text_in_a_report_never_markup(world, tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.chdir(tmp_path)                                # a short relative path, so the name fits its 120 characters
    Image.new("RGB", (40, 30), (200, 10, 10)).save(tmp_path / "srv.png")
    with world.use():
        for name in ('<img src="srv.png" width="40" height="30"/>', "Mill <b>East", '<img src="missing.ini"/>'):
            sites.update(world.site, {"name": name}, "test")
            rep = reports.generate("energy_only", world.site, datetime.now(PKT).strftime("%Y-%m"), "test")
            pdf = reports.file_path(rep["id"], "pdf").read_bytes()
            assert pdf[:4] == b"%PDF" and b"/Subtype /Image" not in pdf, name


# ------------------------------------------------------------------------------------------------ security-csv-upload-blocks-event-loop

def test_the_csv_upload_runs_off_the_event_loop_and_refuses_wild_dates(world, monkeypatch):
    sid = world.site
    op = world.c["operator"]
    headers = {"Content-Type": "text/csv"}
    wild = "ts,grid_present,pv_power_w\n1971-01-02T00:00:00Z,1,5\n2026-11-01T00:00:00Z,1,5\n"
    r = op.post(f"/api/sites/{sid}/telemetry/upload", content=wild, headers=headers)
    assert r.status_code == 422 and "before 2015" in r.json()["detail"], r.text
    r = op.post(f"/api/sites/{sid}/telemetry/upload", content="ts,grid_present\n9999-01-01T00:00:00Z,1\n", headers=headers)
    assert r.status_code == 422 and "future" in r.json()["detail"]
    long = "ts,grid_present\n2016-01-01T00:00:00Z,1\n2018-01-01T00:00:00Z,1\n"
    r = op.post(f"/api/sites/{sid}/telemetry/upload", content=long, headers=headers)
    assert r.status_code == 422 and "spans" in r.json()["detail"]
    with world.use():
        assert telemetry.latest(sid) is None                   # nothing of a refused file is stored
    seen = {}
    real = telemetry.import_csv

    def spy(*a, **k):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return real(*a, **k)

    monkeypatch.setattr(telemetry, "import_csv", spy)
    good = "ts,grid_present,pv_power_w\n2026-11-01T00:00:00Z,1,5\n2026-11-01T00:01:00Z,1,6\n"
    r = op.post(f"/api/sites/{sid}/telemetry/upload", content=good, headers=headers)
    assert r.status_code == 200 and r.json()["rows"] == 2, r.text
    assert seen == {"on_loop": False}
    assert "uploaded telemetry" in _audit_actions(world)


# ------------------------------------------------------------------------------------------------ security-invalid-keys-skip-rate-limits

def test_made_up_keys_meet_the_address_limit_and_are_not_cached(world, monkeypatch):
    monkeypatch.setitem(auth.RATE_LIMITS, "address", 5)
    before = len(api._KEY_CACHE)
    codes = []
    for _ in range(12):
        fake = "glk_" + secrets.token_urlsafe(32)
        codes.append(world.anon.post(f"/api/edge/{world.site}/telemetry", headers={"Authorization": f"Bearer {fake}"}, json=_batch(1)).status_code)
    assert codes[:5] == [401] * 5 and set(codes[5:]) == {429}, codes
    assert len(api._KEY_CACHE) == before
    # a real key is still served by its own budget, not the address's
    monkeypatch.setitem(auth.RATE_LIMITS, "address", 0)
    kh = world.key(["telemetry:write"])
    monkeypatch.setitem(auth.RATE_LIMITS, "address", 1)
    auth.limiter().reset()
    for seq in (1, 2, 3):
        assert world.anon.post(f"/api/edge/{world.site}/telemetry", headers=kh, json=_batch(seq)).status_code == 200


# ------------------------------------------------------------------------------------------------ security-exports-not-audited

def test_exports_and_report_downloads_are_audited(world):
    sid = world.site
    analyst = world.c["analyst"]
    assert analyst.get(f"/api/sites/{sid}/telemetry/export.csv").status_code == 200
    assert analyst.get(f"/api/sites/{sid}/savings/export.csv").status_code == 200
    rep = analyst.post("/api/reports", json={"kind": "energy_only", "site_id": sid})
    assert rep.status_code == 200, rep.text
    assert analyst.get(f"/api/reports/{rep.json()['id']}/csv").status_code == 200
    actions = _audit_actions(world)
    for done in ("exported telemetry", "exported savings", "downloaded a report (csv)"):
        assert done in actions, (done, actions)


# ------------------------------------------------------------------------------------------------ security-alert-action-500-after-300

def test_acting_on_an_alert_outside_the_inbox_first_rows_answers_it(world):
    with world.use():
        with store.connect() as con:
            for i in range(301):
                con.execute("INSERT INTO alerts (id, site_id, rule, category, severity, title, detail, state, opened_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (f"w{i}", world.site, f"r{i}", "Data", "warning", "old", "{}", "resolved", store._now()))
        alerts._open(world.site, "genset_request_1", "Genset", "info", "Please start the genset", {"start": 1})
        info = next(a for a in alerts.listing("active", world.site))
    r = world.c["operator"].post(f"/api/alerts/{info['id']}/acknowledge", json={})
    assert r.status_code == 200 and r.json()["state"] == "acknowledged" and r.json()["site_name"] == "Mill One", r.text
    # an alert is assigned to a member who acts on alerts (round 7, role-operator-31), not to any typed name
    assert world.c["operator"].post(f"/api/alerts/{info['id']}/assign", json={"assignee": "Operator"}).json()["assigned"] == "Operator"


# ------------------------------------------------------------------------------------------------ data-genset-request-every-tick

def _genset_plan(t0: int, on: range, n: int = 16) -> dict:
    steps = [{"t": t0 + 900 * i, "genset_on": i in on, "genset_kw": 45.0 if i in on else 0.0, "reason": "genset_outage" if i in on else "idle",
              "detail": "", "local": datetime.fromtimestamp(t0 + 900 * i, PKT).strftime("%H:%M")} for i in range(n)]
    return {"id": f"p{t0}", "steps": steps}


def test_a_genset_run_is_asked_for_once_and_stale_requests_are_withdrawn(world):
    sid = world.site
    t0 = _now() // 900 * 900
    with world.use():
        site = sites.full(sid)
        telemetry.ingest(sid, [{"ts": t0 - 60, "grid_present": 0, "genset_running": 0}], "edge")
        for k in range(3):                                     # three ticks of one continuous planned run; the set has not started
            alerts.genset_requests(site, _genset_plan(t0 + 900 * k, range(0, 8)), t0 + 900 * k + 30)
        assert len(alerts.requests_for(sid)) == 1
        assert len([a for a in alerts.listing("active", sid) if a["rule"].startswith("genset_request_")]) == 1
        # the set starts: the request is fulfilled and no new one is raised while it runs
        telemetry.ingest(sid, [{"ts": t0 + 2700 - 60, "grid_present": 0, "genset_running": 1}], "edge")
        alerts.genset_requests(site, _genset_plan(t0 + 2700, range(0, 5)), t0 + 2730)
        assert len(alerts.requests_for(sid)) == 1
        assert [a for a in alerts.listing("active", sid) if a["rule"].startswith("genset_request_")] == []
        # a later run is asked for, then withdrawn when a re-solve no longer plans it
        telemetry.ingest(sid, [{"ts": t0 + 3600 - 60, "grid_present": 1, "genset_running": 0}], "edge")
        alerts.genset_requests(site, _genset_plan(t0 + 3600, range(4, 8)), t0 + 3630)
        assert len(alerts.requests_for(sid)) == 2
        alerts.genset_requests(site, _genset_plan(t0 + 4500, range(0)), t0 + 4530)
        assert [a for a in alerts.listing("active", sid) if a["rule"].startswith("genset_request_")] == []
        status = {r["payload"]["start"]: r["status"] for r in alerts.requests_for(sid)}
        assert status == {t0: "pending", t0 + 7200: "withdrawn"}


# ------------------------------------------------------------------------------------------------ data-apply-unconfirmed-never-fires

def test_the_unconfirmed_apply_alert_reads_the_plan_in_force_at_the_peak(world, monkeypatch):
    sid = world.site
    d = datetime.now(PKT).date()
    day0 = int(datetime(d.year, d.month, d.day, tzinfo=PKT).timestamp())
    peak_start = day0 + 18 * 3600
    now = peak_start + 2 * 3600 + 120                          # 20:02 PKT, two hours into the peak
    with world.use():
        sites.set_tier_capable(sid, "advisory_writes")
        sites.set_tier_allowed(sid, "advisory_writes", "test")

    def step(t, peak):
        return {"t": t, "peak": peak, "discharge_kw": 50.0 if peak else 0.0, "date": d.isoformat(), "genset_on": False, "reason": "peak_discharge",
                "detail": "", "local": "", "genset_kw": 0.0}

    def plan_from(t):
        return {"id": f"p{t}", "steps": [step(t + 900 * i, day0 + 18 * 3600 <= t + 900 * i < day0 + 22 * 3600) for i in range(16)]}

    monkeypatch.setattr(plans, "current", lambda site_id: plan_from(now // 900 * 900))
    monkeypatch.setattr(plans, "active_at", lambda site_id, ts: plan_from(int(ts) // 900 * 900))
    monkeypatch.setattr(plans, "peak_mask", lambda conn, ts: (np.asarray(ts) >= day0 + 18 * 3600) & (np.asarray(ts) < day0 + 22 * 3600))
    with world.use():
        # since round 3 a Control alert waits for a plan and 7 days of data (plan 9.3): the site's data starts 8 days back
        telemetry.ingest(sid, [{"ts": now // 60 * 60 - 8 * 86400, "grid_present": 1}], "edge")
        alerts.evaluate(sid, now)
        assert [a["rule"] for a in alerts.listing("active", sid)].count("apply_unconfirmed") == 1
        with store.connect() as con:
            con.execute("INSERT INTO applied (id, site_id, plan_id, day_local, by, method, created) VALUES (?,?,?,?,?,?,?)",
                        ("a1", sid, "p", d.isoformat(), "test", "installer_keyed", store._now()))
        alerts.evaluate(sid, now)
        assert "apply_unconfirmed" not in [a["rule"] for a in alerts.listing("active", sid)]


# ------------------------------------------------------------------------------------------------ data-stale-alert-csv-sources

def test_staleness_follows_the_data_source(world):
    sid = world.site
    now = _now()
    with world.use():
        telemetry.ingest(sid, [{"ts": now - 3 * 3600, "grid_present": 1}], "csv")
        alerts._stale(sid, telemetry.latest(sid), now)
        assert [a for a in alerts.listing("active", sid) if a["rule"] == "data_stale"] == []
        alerts._stale(sid, telemetry.latest(sid), now + 20 * 3600)  # 23 hours since the last upload's data
        assert [a for a in alerts.listing("active", sid) if a["rule"] == "data_stale"] == []
        alerts._stale(sid, telemetry.latest(sid), now + 22 * 3600)  # 25 hours
        stale = [a for a in alerts.listing("active", sid) if a["rule"] == "data_stale"]
        assert len(stale) == 1 and stale[0]["severity"] == "warning" and "upload" in stale[0]["title"]
        telemetry.ingest(sid, [{"ts": now - 90 * 60, "grid_present": 1}], "edge")
        alerts._stale(sid, telemetry.latest(sid), now)
        stale = [a for a in alerts.listing("active", sid) if a["rule"] == "data_stale"]
        assert len(stale) == 1 and stale[0]["severity"] == "warning" and "minutes" in stale[0]["title"]
        alerts._stale(sid, telemetry.latest(sid), now + 3600)
        assert [a["severity"] for a in alerts.listing("active", sid) if a["rule"] == "data_stale"] == ["critical"]
