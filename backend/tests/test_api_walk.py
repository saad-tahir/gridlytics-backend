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
        h = _login(client, password, email)
        r = client.post("/api/public/access-requests", json=body, headers=h)
        assert r.status_code == 202 and r.content == b"", (email, r.text)
        # no role's session reaches the founder's Access requests, the analyst's too (the role walk has no analyst: UX K2 check)
        for method, path in (("GET", "/api/founder/access-requests"), ("PATCH", "/api/founder/access-requests/00000000-0000-4000-8000-000000000000")):
            r = client.request(method, path, headers=h, json={"state": "done"} if method == "PATCH" else None)
            assert r.status_code == 401 and r.json()["code"] == "founder_session_required", (email, method, r.text[:200])
    founder_h = {"x-founder-session": founder.new_token()[0]}
    got = client.get("/api/founder/access-requests?state=new", headers=founder_h).json()
    mine = [r for r in got["requests"] if r["email"] == "api-walk@example.pk"]
    assert sorted((r["sent_by"] or {}).get("role", "signed out") for r in mine) == ["admin", "analyst", "operator", "signed out", "viewer"]
    assert {r["about"] for r in mine} == {"1 site · IESCO"} and got["counts"]["new"] >= 5
    r = client.patch(f"/api/founder/access-requests/{mine[0]['id']}", json={"state": "contacted", "note": "Rang them"}, headers=founder_h)
    assert r.status_code == 200 and r.json()["request"]["state"] == "contacted", r.text


def test_the_plan_section_reads_alike_for_every_role_in_energy(world):
    """UX backend asks K14, K17 and K19 (plan step 7): every role reads the latest plan's tariff (`summary.tariff`) and fuel unit
    (`summary.fuel_unit`), the Overview's `fuel_unit` and the control answer (the Inverter table), the tariff and the units the same for each; the
    viewer's control answer carries no gain. The Karachi feed mill's genset burns captive gas, the others' diesel; the Rahim Yar Khan farm is off-grid."""
    client, password, _ = world
    seen: dict[str, dict] = {}
    for email in ("owner@onyxleaf.demo", "operator@onyxleaf.demo", "analyst@onyxleaf.demo", "viewer@onyxleaf.demo"):
        h = _login(client, password, email)
        for row in client.get("/api/fleet", headers=h).json()["sites"]:
            sid = row["id"]
            summary = client.get(f"/api/sites/{sid}/plans?limit=1", headers=h).json()[0]["summary"]
            now = client.get(f"/api/sites/{sid}/now", headers=h).json()
            control = client.get(f"/api/sites/{sid}/control", headers=h)
            assert control.status_code == 200, (email, row["name"], control.text[:200])
            if email.startswith("viewer"):
                assert "gain_rs" not in control.text, row["name"]
            got = {"tariff": summary["tariff"], "plan_unit": summary["fuel_unit"], "now_unit": now["fuel_unit"]}
            assert seen.setdefault(row["name"], got) == got, (email, row["name"])
    assert {n: (g["plan_unit"], g["now_unit"]) for n, g in seen.items()} == {
        "Faisalabad textile mill (demo)": ("L", "L"), "Lahore mall (demo)": ("L", "L"), "Karachi feed mill (demo)": ("MMBtu", "MMBtu"),
        "Rahim Yar Khan farm (demo)": ("L", "L")}
    assert [n for n, g in seen.items() if g["tariff"] is None] == ["Rahim Yar Khan farm (demo)"]


def test_the_overview_and_fleet_read_alike_for_every_role_in_energy_and_the_grid_bill_for_see_costs(world):
    """UX backend asks K12, K13 and K18 (plan step 8): every role reads the Overview's plan facts (`plan`, `plan_expired_at`), today's apply mark
    (`applied_today`), the This month tiles' energy and each spark day's energy (`peak_kwh_avoided`, `litres_saved`) on the Overview and on Fleet, the
    same for each; the grid bill so far (`month.grid_bill_rs`, `month.grid_bill_rs_basis`) reaches the readers with see_costs only."""
    client, password, _ = world
    seen: dict[str, dict] = {}
    for email, costs in (("owner@onyxleaf.demo", True), ("operator@onyxleaf.demo", True), ("analyst@onyxleaf.demo", True),
                         ("viewer@onyxleaf.demo", False)):
        h = _login(client, password, email)
        for row in client.get("/api/fleet", headers=h).json()["sites"]:
            now = client.get(f"/api/sites/{row['id']}/now", headers=h).json()
            month = now["month"]
            got = {"plan": now["plan"], "expired": now["plan_expired_at"], "applied": now["applied_today"],
                   "month": {k: month[k] for k in ("peak_import_kwh", "pv_self_pct", "outages", "outages_covered", "unserved_kwh", "battery_cycles")},
                   "bars": [(p["day"], p["peak_kwh_avoided"], p["litres_saved"]) for p in row["spark"]],
                   "now_bars": [(p["day"], p["peak_kwh_avoided"], p["litres_saved"]) for p in now["spark"]]}
            assert got["bars"] == got["now_bars"], (email, row["name"])
            assert seen.setdefault(row["name"], got) == got, (email, row["name"])
            assert ("grid_bill_rs" in month and "grid_bill_rs_basis" in month) is costs, (email, row["name"])
    assert len(seen) == 4


def test_the_tariff_editor_answers_each_role_by_its_permissions(world):
    """UX backend ask K32 (plan step 13, Tariffs and the editor): drafting (validate, drafts, a draft's prices, submit, discard) is the administrator's
    and the analyst's (edit_tariffs); a waiting version's prices and simulated bill are every see_costs reader's; a viewer is refused every one. A
    draft is its author's: the analyst's list holds the analyst's alone, the administrator's everyone's. A version is withdrawn by its author (UX K32
    check: every route of the editor is walked here)."""
    client, password, _ = world
    draft = {"effective_from": "2026-12-01", "source": {"ref": "walk", "verified": "primary"},
             "versions": [{"kind": "adder", "key": "fca", "body": {"rate": 1.38, "billing_months": ["2026-12"]}}]}
    owner = _login(client, password, "owner@onyxleaf.demo")
    fleet = client.get("/api/fleet", headers=owner).json()["sites"]
    sid = next(s["id"] for s in fleet if s["name"] == "Faisalabad textile mill (demo)")
    waiting = next(v for v in client.get("/api/tariffs/versions?kind=adder", headers=owner).json() if v["status"] == "pending")
    bill = next(b for s in fleet for b in client.get(f"/api/sites/{s['id']}/bills", headers=owner).json())
    for email, drafts, prices in (("owner@onyxleaf.demo", 200, 200), ("operator@onyxleaf.demo", 403, 200), ("analyst@onyxleaf.demo", 200, 200),
                                  ("viewer@onyxleaf.demo", 403, 403)):
        h = _login(client, password, email)
        who = email.split("@")[0]
        assert client.put(f"/api/tariffs/drafts/api-walk-{who}", headers=h, json=draft).status_code == drafts, email
        assert client.post("/api/tariffs/versions/validate", headers=h, json=draft).status_code == drafts, email
        assert client.post("/api/tariffs/preview", headers=h, json={"site_id": sid, "draft": draft, "days": 1}).status_code == drafts, email
        assert client.post("/api/tariffs/preview", headers=h, json={"site_id": sid, "version_id": waiting["id"], "days": 1}).status_code == prices, email
        listed = client.get("/api/tariffs/drafts", headers=h)
        assert listed.status_code == drafts, email
        if drafts == 200:
            mine = {d["id"] for d in listed.json()["drafts"]}
            assert f"api-walk-{who}" in mine and (who == "owner" or mine == {f"api-walk-{who}"}), (email, mine)
        simulated = client.post("/api/tariffs/simulate", headers=h, json={"site_id": bill["site_id"], "bill_id": bill["id"], "version_id": waiting["id"]})
        assert simulated.status_code == prices, (email, simulated.text[:200])
        url = f"/api/tariffs/drafts/api-walk-{who}"
        assert client.get(url, headers=h).status_code == drafts, email
        if drafts == 200:
            # submitted by its author (one version waits, the draft is gone), the version withdrawn by its author, a draft saved again discarded
            r = client.post(f"{url}/submit", headers=h)
            assert r.status_code == 200, (email, r.text[:200])
            assert client.get(url, headers=h).status_code == 404, email
            vid = r.json()["submitted"][0]["id"]
            assert client.post(f"/api/tariffs/versions/{vid}/withdraw", headers=h, json={"reason": "api walk"}).json()["status"] == "withdrawn"
            assert client.put(url, headers=h, json=draft).status_code == 200, email
            assert client.delete(url, headers=h).status_code == 200, email
        else:
            assert client.post(f"{url}/submit", headers=h).status_code == 403, email
            assert client.delete(url, headers=h).status_code == 403, email
            assert client.post(f"/api/tariffs/versions/{waiting['id']}/withdraw", headers=h, json={}).status_code == 403, email
    assert client.get("/api/tariffs", headers=owner).json()["schedule"]["template"] is True


def test_set_up_s_checks_answer_each_role_that_opens_set_up(world):
    """UX backend ask K6 (plan step 9, Set-up B to D and Review): `POST /sites/:id/validate` answers the administrator, the operator and the analyst
    (every role that opens Set-up: see_costs) with the same problems, warnings and figures, the wear cost for see_money_settings only; a viewer is
    refused. Nothing is saved."""
    client, password, _ = world
    owner = _login(client, password, "owner@onyxleaf.demo")
    sid = next(s["id"] for s in client.get("/api/fleet", headers=owner).json()["sites"] if s["name"] == "Faisalabad textile mill (demo)")
    body = {"assets": {"battery": {"entry_unit": "ah", "capacity_ah": 1000, "nominal_v": 51.2, "p_ch_kw": 50, "p_dis_kw": 50}}}
    seen = {}
    for email, status, wear in (("owner@onyxleaf.demo", 200, True), ("operator@onyxleaf.demo", 200, False), ("analyst@onyxleaf.demo", 200, True),
                                ("viewer@onyxleaf.demo", 403, False)):
        h = _login(client, password, email)
        r = client.post(f"/api/sites/{sid}/validate", headers=h, json=body)
        assert r.status_code == status, (email, r.text[:200])
        if status == 200:
            got = r.json()
            assert ("wear_cost_rs_per_kwh" in got["derived"]["battery"]) is wear, email
            got["derived"]["battery"].pop("wear_cost_rs_per_kwh", None)
            assert seen.setdefault("answer", got) == got, email
    assert seen["answer"]["derived"]["battery"]["usable_kwh"] == 43.52
    assert client.get(f"/api/sites/{sid}", headers=owner).json()["assets"]["battery"]["entry_unit"] == "kwh"


def test_set_up_s_price_preview_answers_the_readers_of_costs(world):
    """UX backend ask K5 (plan step 9b, Set-up C's TARIFF DAY): `POST /sites/:id/prices/preview` prices the connection's fields as typed for every
    see_costs reader alike (the TARIFF DAY needs see_costs), with the DISCO group's peak windows; a viewer is refused. Nothing is saved."""
    client, password, _ = world
    owner = _login(client, password, "owner@onyxleaf.demo")
    sid = next(s["id"] for s in client.get("/api/fleet", headers=owner).json()["sites"] if s["name"] == "Lahore mall (demo)")
    held = client.get(f"/api/sites/{sid}", headers=owner).json()["connections"]
    body = {"tariff_category": "B-2(b)", "sanctioned_load_kw": 900, "regime": "none"}
    seen = {}
    for email, status in (("owner@onyxleaf.demo", 200), ("operator@onyxleaf.demo", 200), ("analyst@onyxleaf.demo", 200), ("viewer@onyxleaf.demo", 403)):
        h = _login(client, password, email)
        r = client.post(f"/api/sites/{sid}/prices/preview", headers=h, json=body)
        assert r.status_code == status, (email, r.text[:200])
        if status == 200:
            got = {k: v for k, v in r.json().items() if k != "as_of"}
            assert seen.setdefault("answer", got) == got, email
    assert seen["answer"]["peak_windows"]["disco_group"] == "xwdisco" and len(seen["answer"]["c_imp"]) == 96
    assert client.get(f"/api/sites/{sid}", headers=owner).json()["connections"] == held


def test_the_source_test_shows_its_first_rows_to_the_roles_that_edit_the_site(world):
    """UX backend ask K9 (plan step 9a, Set-up A's result card): `POST /sites/:id/source/test` answers the administrator and the operator (edit_sites)
    with up to five rows of what the source sent, in Pakistan time; the analyst and the viewer are refused."""
    client, password, _ = world
    owner = _login(client, password, "owner@onyxleaf.demo")
    sid = next(s["id"] for s in client.get("/api/fleet", headers=owner).json()["sites"] if s["name"] == "Faisalabad textile mill (demo)")
    for email, status in (("owner@onyxleaf.demo", 200), ("operator@onyxleaf.demo", 200), ("analyst@onyxleaf.demo", 403), ("viewer@onyxleaf.demo", 403)):
        r = client.post(f"/api/sites/{sid}/source/test", headers=_login(client, password, email))
        assert r.status_code == status, (email, r.text[:200])
        if status == 200:
            t = r.json()
            assert t["grid_signal"] == "found" and 1 <= len(t["sample"]) <= 5, (email, t)
            assert all(row["at"].endswith("+05:00") and list(row["values"]) == t["points"] for row in t["sample"]), email


def test_every_role_reads_whether_the_box_s_key_was_made(world):
    """UX backend ask K8 (plan step 9d, the edge checklist's item 1): GET /sites/:id/edge carries `key` ({exists, made_at}) for every role alike, the
    operator too, who may not list the company's keys; a key made for the site reads as made, in Karachi time."""
    client, password, _ = world
    owner = _login(client, password, "owner@onyxleaf.demo")
    sid = next(s["id"] for s in client.get("/api/fleet", headers=owner).json()["sites"] if s["name"] == "Lahore mall (demo)")
    made = client.post("/api/platform/keys", headers=owner, json={"name": "api walk box", "scopes": ["telemetry:write"], "site_id": sid, "kind": "edge"})
    assert made.status_code == 200, made.text
    for email in ("owner@onyxleaf.demo", "operator@onyxleaf.demo", "analyst@onyxleaf.demo", "viewer@onyxleaf.demo"):
        key = client.get(f"/api/sites/{sid}/edge", headers=_login(client, password, email)).json()["key"]
        assert key["exists"] is True and key["made_at"].endswith("+05:00") and set(key) == {"exists", "made_at"}, (email, key)


def test_every_role_reads_the_alert_thresholds_defaults_and_ranges(world):
    """UX backend ask K16 (plan step 14, Alerts › Rules: "Default 15 · 5 to 1,440"): every rule of the company's and of a site's rules carries `limits`,
    the same for every role that receives its category (a viewer the five energy kinds)."""
    from gridlytics import alerts
    client, password, _ = world
    for email in ("owner@onyxleaf.demo", "operator@onyxleaf.demo", "analyst@onyxleaf.demo", "viewer@onyxleaf.demo"):
        h = _login(client, password, email)
        sid = client.get("/api/fleet", headers=h).json()["sites"][0]["id"]
        for rules in (client.get("/api/alerts/rules", headers=h).json(), client.get(f"/api/alerts/rules/sites/{sid}", headers=h).json()["rules"]):
            assert rules and all(r["limits"] == alerts.threshold_limits(c) for c, r in rules.items()), email
            assert email != "viewer@onyxleaf.demo" or set(rules) == set(alerts.ENERGY_CATEGORIES), email
        assert client.get("/api/alerts/rules", headers=h).json()["Data"]["limits"]["gap_hours"] == {"default": 1.0, "min": 0.25, "max": 24}, email


def test_the_roles_that_upload_preview_a_file_s_first_rows(world):
    """UX backend ask K15 (plan step 9d, the CSV upload's stage 2): POST /telemetry/mapping/preview answers the first five rows as the upload will store
    them, in Karachi time, to the roles that upload (the administrator, the operator), and refuses the analyst and the viewer; nothing is stored."""
    client, password, _ = world
    head = "Time,Grid,Load (kW)\n2026-03-01 00:00,on,30.1\n2026-03-01 00:01,on,30.0\n"
    body = {"head": head, "mapping": {"columns": {"Time": {"key": "ts"}, "Grid": {"key": "grid_present"},
                                                  "Load (kW)": {"key": "load_power_w", "unit": "kW"}}}, "tz_offset_h": 5}
    for email, status in (("owner@onyxleaf.demo", 200), ("operator@onyxleaf.demo", 200), ("analyst@onyxleaf.demo", 403), ("viewer@onyxleaf.demo", 403)):
        h = _login(client, password, email)
        sid = client.get("/api/fleet", headers=h).json()["sites"][0]["id"]
        imports = len(client.get(f"/api/sites/{sid}/telemetry/quality", headers=h).json().get("imports") or [])
        r = client.post(f"/api/sites/{sid}/telemetry/mapping/preview", json=body, headers=h)
        assert r.status_code == status, (email, r.text[:200])
        if status == 200:
            rows = r.json()["rows"]
            assert [x["at"] for x in rows] == ["2026-03-01T00:00:00+05:00", "2026-03-01T00:01:00+05:00"], rows
            assert rows[0]["values"] == {"grid_present": True, "load_power_w": 30100.0}, rows
        assert len(client.get(f"/api/sites/{sid}/telemetry/quality", headers=h).json().get("imports") or []) == imports, email


def test_my_account_offers_each_role_the_alert_kinds_it_receives(world):
    """UX backend ask K33 (plan step 14b): the email card's kinds are those each role receives (an operator eight, a viewer five); a kind not received
    is refused in words; ticking kinds for email leaves the inbox's own list as it was."""
    client, password, _ = world
    for email, n in (("owner@onyxleaf.demo", 9), ("operator@onyxleaf.demo", 8), ("analyst@onyxleaf.demo", 9), ("viewer@onyxleaf.demo", 5)):
        h = _login(client, password, email)
        got = client.get("/api/platform/notifications", headers=h).json()
        assert len(got["categories"]) == n and got["categories"] == client.get("/api/alerts/counts", headers=h).json()["categories"], email
    h = _login(client, password, "viewer@onyxleaf.demo")
    r = client.put("/api/platform/notifications", json={"email_kinds": ["Savings"]}, headers=h)
    assert r.status_code == 422 and r.json()["detail"] == "You do not receive Savings alerts", r.text
    r = client.put("/api/platform/notifications", json={"email_kinds": ["Reserve", "Data"]}, headers=h)
    assert r.status_code == 200 and r.json()["email_kinds"] == ["Data", "Reserve"] and r.json()["email"] is True, r.text
    assert r.json()["follow"] == r.json()["categories"]


def _founder(monkeypatch) -> dict:
    from gridlytics import founder
    for name, value in (("GRIDLYTICS_FOUNDER_USERNAME", "founder"), ("GRIDLYTICS_FOUNDER_PASSWORD_HASH", founder.hash_password("a made-up walk password")),
                        ("GRIDLYTICS_FOUNDER_TOTP_SECRET", "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"),
                        ("GRIDLYTICS_FOUNDER_SESSION_SECRET", "a made-up founder session secret of the api walk")):
        monkeypatch.setenv(name, value)
    return {"x-founder-session": founder.new_token()[0]}


def test_the_administrator_filters_and_exports_the_audit_trail_and_the_founder_filters_theirs(world, monkeypatch):
    """UX backend ask K34 (plan step 14): the company trail filtered by who, site and days with its total and its CSV, for the administrator alone;
    the founder's audit filtered by company, each line with its company."""
    client, password, _ = world
    owner = _login(client, password, "owner@onyxleaf.demo")
    page = client.get("/api/audit/search", headers=owner).json()
    assert page["total"] >= len(page["entries"]) > 0 and [g["key"] for g in page["groups"]][0] == "people"
    sid = client.get("/api/fleet", headers=owner).json()["sites"][0]["id"]
    on_site = client.get("/api/audit/search", params={"site": sid}, headers=owner).json()
    assert all(e["site_id"] == sid for e in on_site["entries"]) and on_site["total"] <= page["total"]
    demo = client.get("/api/audit/search", params={"who": "demo"}, headers=owner).json()          # the demo set-up's lines (the seed's too)
    assert demo["total"] >= 1 and {e["who"] for e in demo["entries"]} == {"demo"} and "demo" in {p["who"] for p in page["people"]}
    csv_text = client.get("/api/audit/export.csv", params={"site": sid}, headers=owner).text
    assert csv_text.splitlines()[0] == "at_karachi,who,email,action,group,target,site,detail"
    for email in ("operator@onyxleaf.demo", "analyst@onyxleaf.demo", "viewer@onyxleaf.demo"):
        h = _login(client, password, email)
        assert client.get("/api/audit/search", headers=h).status_code == 403 and client.get("/api/audit/export.csv", headers=h).status_code == 403
    fh = _founder(monkeypatch)
    cid = client.get("/api/founder/companies", headers=fh).json()["companies"][0]["id"]
    audit = client.get("/api/founder/audit", params={"company": cid}, headers=fh).json()
    assert audit["total"] >= 1 and all(e["company_id"] == cid for e in audit["entries"]), audit


def test_the_founder_and_the_administrator_read_when_a_company_was_last_copied(world, monkeypatch):
    """UX backend ask K35: each of the founder's companies carries `last_backup`; GET /platform/org carries it for the administrator alone."""
    client, password, _ = world
    owner = _login(client, password, "owner@onyxleaf.demo")
    assert "last_backup" in client.get("/api/platform/org", headers=owner).json()
    for email in ("operator@onyxleaf.demo", "analyst@onyxleaf.demo", "viewer@onyxleaf.demo"):
        assert "last_backup" not in client.get("/api/platform/org", headers=_login(client, password, email)).json(), email
    companies = client.get("/api/founder/companies", headers=_founder(monkeypatch)).json()["companies"]
    assert companies and all("last_backup" in c for c in companies)


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
