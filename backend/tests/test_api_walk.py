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


def test_the_viewer_gets_no_rupees(world):
    client, password, _ = world
    h = _login(client, password, "viewer@onyxleaf.demo")
    sid = client.get("/api/fleet", headers=h).json()["sites"][0]["id"]
    blob = "".join(json.dumps(client.get(p.format(s=sid), headers=h).json()) for p in ("/api/sites/{s}/plan", "/api/fleet", "/api/sites/{s}/savings",
                                                                                         "/api/sites/{s}/genset", "/api/sites/{s}"))
    for key in ("c_imp", "cost_rs", "saving_rs", "objective_rs", "levers", "mtd_saving_rs", "c_fuel", "replacement_cost_per_kwh", "fuel_cost_rs"):
        assert f'"{key}"' not in blob, key
    assert client.get("/api/tariffs", headers=h).status_code == 403
    assert client.post(f"/api/sites/{sid}/plan/solve", headers=h).status_code == 403


def test_the_owner_solves_reports_holds_and_resumes(world):
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
    msg = client.get(f"/api/sites/{ids[0]}/plans/current/message", headers=kh).json()
    assert msg["signature"]["alg"] == "Ed25519"
    bad = dict(batch, seq=2, columns=["ts", "pv_power_w"], rows=[[1794000060, 1]])
    assert client.post(f"/api/edge/{ids[0]}/telemetry", headers=kh, json=bad).status_code == 422
