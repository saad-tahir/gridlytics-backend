"""Every page's API answers for each role on the demo company, the viewer gets no rupees, and a site-bound edge key reaches only its own site.

One seeded data folder for the module (the four demo twin sites with three days of history), in a temporary folder of its own."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    import os
    folder = tmp_path_factory.mktemp("walk")
    old = os.environ.get("GRIDLYTICS_DATA_DIR")
    os.environ["GRIDLYTICS_DATA_DIR"] = str(folder)
    from gridlytics import api, seed_demo
    done = seed_demo.seed(out=folder / "ACCOUNTS.md")
    seed_demo.load_demo_sites(4)
    text = (folder / "ACCOUNTS.md").read_text(encoding="utf-8")

    def password(email):
        for line in text.splitlines():
            cells = [c.strip().strip("`") for c in line.split("|")]
            if email in cells:
                return cells[cells.index(email) + 1]

    client = TestClient(api.app, base_url="http://127.0.0.1")
    yield client, password, done
    if old is None:
        os.environ.pop("GRIDLYTICS_DATA_DIR", None)
    else:
        os.environ["GRIDLYTICS_DATA_DIR"] = old


def _login(client, password, email):
    r = client.post("/api/local/session", json={"email": email, "password": password(email)})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


PAGES = ["/api/sites/{s}", "/api/sites/{s}/now", "/api/sites/{s}/overview/chart", "/api/sites/{s}/plan", "/api/sites/{s}/plans", "/api/sites/{s}/plan/compare",
         "/api/sites/{s}/control", "/api/sites/{s}/telemetry?names=pv_power_w,load_power_w", "/api/sites/{s}/telemetry/days", "/api/sites/{s}/telemetry/heatmap",
         "/api/sites/{s}/telemetry/quality", "/api/sites/{s}/outages", "/api/sites/{s}/battery", "/api/sites/{s}/genset", "/api/sites/{s}/forecasts/pv",
         "/api/sites/{s}/forecasts/load", "/api/sites/{s}/forecasts/outage", "/api/sites/{s}/forecasts/scores", "/api/sites/{s}/savings",
         "/api/sites/{s}/switchback", "/api/sites/{s}/pilot", "/api/sites/{s}/edge", "/api/sites/{s}/native", "/api/sites/{s}/events", "/api/meta", "/api/fleet",
         "/api/sites", "/api/alerts", "/api/alerts/counts", "/api/reports", "/api/demo", "/api/fleet/savings", "/api/v1/fleet"]
PAGES += ["/api/sites/{s}/plan/past"]  # the plan table's past rows (round 7, pages-B-07)
# the Savings page's measures and dated periods, and the pilot's scorecard (round 7, savings-page: brief-vs-build-13, pages-D-23, pages-D-12); a
# recompute's job is read by the role that started one (test_role_walk)
PAGES += ["/api/sites/{s}/savings/kpis", "/api/sites/{s}/savings/periods", "/api/sites/{s}/pilot/scorecard"]


@pytest.mark.parametrize("email", ["owner@onyxleaf.demo", "operator@onyxleaf.demo", "analyst@onyxleaf.demo", "viewer@onyxleaf.demo"])
def test_every_page_answers_for_every_role(world, email):
    client, password, _ = world
    h = _login(client, password, email)
    fleet = client.get("/api/fleet", headers=h).json()
    assert len(fleet["sites"]) == 4
    for s in fleet["sites"]:
        for page in PAGES:
            r = client.get(page.format(s=s["id"]), headers=h)
            assert r.status_code == 200, (email, page, r.status_code, r.text[:200])


def test_the_stage_track_and_the_role_matrix_reach_every_role(world):
    """UX backend asks K1 and K4: every fleet row and every site answer carries the same `lifecycle` (six stages, a viewer's states only), and GET
    /api/meta the role matrix, for each role."""
    from gridlytics import auth, lifecycle
    client, password, _ = world
    for email in ("owner@onyxleaf.demo", "operator@onyxleaf.demo", "analyst@onyxleaf.demo", "viewer@onyxleaf.demo"):
        h = _login(client, password, email)
        meta = client.get("/api/meta", headers=h).json()
        assert meta["role_matrix"] == {p: [r for r in auth.ROLES if r in who] for p, who in auth.PERMISSIONS.items()}, email
        for row in client.get("/api/fleet", headers=h).json()["sites"]:
            lc = row["lifecycle"]
            assert [st["key"] for st in lc["stages"]] == list(lifecycle.STAGES) and lc["stage"] in lifecycle.STAGES, (email, lc)
            assert {st["state"] for st in lc["stages"]} <= set(lifecycle.STATES) and lc["words"], (email, lc)
            assert {st["next"] for st in lc["stages"]} <= set(lifecycle.NEXT) | {None}, (email, lc)
            site = client.get(f"/api/sites/{row['id']}", headers=h).json()["lifecycle"]
            assert [(st["key"], st["state"]) for st in site["stages"]] == [(st["key"], st["state"]) for st in lc["stages"]], (email, row["name"])
            if email.startswith("viewer"):
                assert [st["next"] for st in lc["stages"]][:5] == [None] * 5 and lc["next"] == "energy_report", (row["name"], lc)


def test_a_request_for_access_reaches_the_founder_from_anyone_signed_in_or_not(world, monkeypatch):
    """UX backend asks K2 and K3: the contact page's request for access is sent signed out, or signed in from People's "Ask OnyxLeaf to add someone"
    (each role's session is recorded as `sent_by`), answered 202 with no body; the founder lists it and sets its state."""
    from gridlytics import access_requests, founder
    client, password, _ = world
    monkeypatch.setattr(access_requests, "RATE_LIMIT", 100)
    for name, value in (("GRIDLYTICS_FOUNDER_USERNAME", "founder"), ("GRIDLYTICS_FOUNDER_PASSWORD_HASH", founder.hash_password("a made-up walk password")),
                        ("GRIDLYTICS_FOUNDER_TOTP_SECRET", "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"),
                        ("GRIDLYTICS_FOUNDER_SESSION_SECRET", "a made-up founder session secret of the api walk")):
        monkeypatch.setenv(name, value)
    body = {"topic": "access", "name": "Api Walk", "email": "api-walk@example.pk", "company": "Walk Mills", "sites": "1", "disco": "IESCO"}
    r = client.post("/api/public/access-requests", json=body)
    assert r.status_code == 202 and r.content == b"", r.text
    for email in ("owner@onyxleaf.demo", "operator@onyxleaf.demo", "analyst@onyxleaf.demo", "viewer@onyxleaf.demo"):
        r = client.post("/api/public/access-requests", json=body, headers=_login(client, password, email))
        assert r.status_code == 202 and r.content == b"", (email, r.text)
    founder_h = {"x-founder-session": founder.new_token()[0]}
    got = client.get("/api/founder/access-requests?state=new", headers=founder_h).json()
    mine = [r for r in got["requests"] if r["email"] == "api-walk@example.pk"]
    assert sorted((r["sent_by"] or {}).get("role", "signed out") for r in mine) == ["admin", "analyst", "operator", "signed out", "viewer"]
    assert {r["about"] for r in mine} == {"1 site · IESCO"} and got["counts"]["new"] >= 5
    r = client.patch(f"/api/founder/access-requests/{mine[0]['id']}", json={"state": "contacted", "note": "Rang them"}, headers=founder_h)
    assert r.status_code == 200 and r.json()["request"]["state"] == "contacted", r.text


def test_the_viewer_gets_no_rupees(world):
    client, password, _ = world
    h = _login(client, password, "viewer@onyxleaf.demo")
    sid = client.get("/api/fleet", headers=h).json()["sites"][0]["id"]
    blob = "".join(json.dumps(client.get(p.format(s=sid), headers=h).json()) for p in ("/api/sites/{s}/plan", "/api/fleet", "/api/sites/{s}/savings",
                                                                                         "/api/sites/{s}/genset", "/api/sites/{s}",
                                                                                         # round 7: the pilot (pages-D-26) and savings-page's routes
                                                                                         "/api/sites/{s}/pilot", "/api/sites/{s}/pilot/scorecard",
                                                                                         "/api/sites/{s}/savings/kpis"))
    for key in ("c_imp", "cost_rs", "saving_rs", "objective_rs", "levers", "mtd_saving_rs", "c_fuel", "replacement_cost_per_kwh", "fuel_cost_rs",
                "fee_multiple", "claimed_rs"):
        assert f'"{key}"' not in blob, key
    assert client.get("/api/tariffs", headers=h).status_code == 403
    assert client.post(f"/api/sites/{sid}/plan/solve", headers=h).status_code == 403


def test_the_owner_solves_reports_holds_and_resumes(world, monkeypatch):
    from gridlytics import reports
    # four days of demo history: R8 2.14 refuses a monthly savings report under a week of data (round 7, savings-page), which this walk is not about
    monkeypatch.setattr(reports, "REPORT_DAYS_NEEDED", 0)
    client, password, _ = world
    h = _login(client, password, "owner@onyxleaf.demo")
    sid = client.get("/api/fleet", headers=h).json()["sites"][0]["id"]
    assert client.post(f"/api/sites/{sid}/plan/solve", headers=h).json()["status"] == "active"
    rep = client.post("/api/reports", headers=h, json={"kind": "monthly_savings", "site_id": sid}).json()
    assert client.get(f"/api/reports/{rep['id']}/pdf", headers=h).content[:4] == b"%PDF"
    assert client.post(f"/api/sites/{sid}/control/hold", headers=h, json={"hours": 2}).json()["control"]["key"] == "hold"
    assert client.post(f"/api/sites/{sid}/control/resume", headers=h, json={}).json()["control"]["key"] != "hold"
    assert client.get("/api/tariffs", headers=h).json()["cards"]


def test_an_edge_key_reaches_only_its_own_site(world):
    client, password, _ = world
    h = _login(client, password, "owner@onyxleaf.demo")
    ids = [s["id"] for s in client.get("/api/fleet", headers=h).json()["sites"]]
    k = client.post("/api/platform/keys", headers=h, json={"name": "edge", "role": "operator", "scopes": ["telemetry:write", "plans:read"],
                                                           "site_id": ids[0], "kind": "edge"})
    assert k.status_code == 200, k.text
    kh = {"Authorization": "Bearer " + k.json()["token"]}
    batch = {"contract": "ems-edge/1.0", "seq": 1, "sent_at": "2026-11-06T10:00:00Z", "columns": ["ts", "grid_present", "pv_power_w"], "rows": [[1794000000, 1, 900]]}
    assert client.post(f"/api/edge/{ids[0]}/telemetry", headers=kh, json=batch).json()["accepted"] == 1
    assert client.post(f"/api/edge/{ids[0]}/telemetry", headers=kh, json=batch).json()["duplicate"] is True
    assert client.post(f"/api/edge/{ids[1]}/telemetry", headers=kh, json=batch).status_code == 403
    # only a closed-loop site's edge is sent a plan (round 7): the first site is advisory, so its key gets a refusal and no table
    tiers = {s["id"]: s["tier"] for s in client.get("/api/fleet", headers=h).json()["sites"]}
    r = client.get(f"/api/sites/{ids[0]}/plans/current/message", headers=kh)
    if tiers[ids[0]] == "closed_loop":
        assert r.json()["signature"]["alg"] == "Ed25519"
    else:
        assert r.status_code == 409 and r.json()["code"] == "not_closed_loop", r.text
    loop = next(i for i, t in tiers.items() if t == "closed_loop")
    k2 = client.post("/api/platform/keys", headers=h, json={"name": "edge2", "role": "operator", "scopes": ["plans:read"], "site_id": loop, "kind": "edge"})
    msg = client.get(f"/api/sites/{loop}/plans/current/message", headers={"Authorization": "Bearer " + k2.json()["token"]}).json()
    assert msg["signature"]["alg"] == "Ed25519"
    bad = dict(batch, seq=2, columns=["ts", "pv_power_w"], rows=[[1794000060, 1]])
    assert client.post(f"/api/edge/{ids[0]}/telemetry", headers=kh, json=bad).status_code == 422
