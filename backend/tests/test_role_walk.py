"""The role walk (plan Part 10.5; audit security-roles-02 and tracker-and-docs-03): every GET route the API mounts is read by the administrator, an operator,
a viewer and an edge device's site key (sites:read and plans:read), on the demo company with a few days of history, and the answers are compared.

- The viewer's answer holds no money key at any depth (`money.is_money`), no rupee figure inside a sentence, and no money value of the administrator's
  answer: a figure the administrator reads under a money key, and under no other key, appears nowhere in the viewer's answer.
- The operator's answer holds no fee and no money setting (plan 9.4: "costs but not money settings or the fee"; audit role-operator-06).
- A key's answer holds no money key at all, whatever its scopes (plan Part 10.5), and the key reaches exactly the routes its scopes name (KEY_READS; audit
  security-roles-07): the site's settings and its plan, never its data.
- The walk covers every GET route: the routes are read from the app, so a new route is walked without being listed here; a path parameter the walk cannot
  fill fails the walk until it is taught one.

One seeded data folder for the module, in a temporary folder of its own."""
from __future__ import annotations

import itertools
import json
import re
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

READERS = ("admin", "operator", "viewer", "key")
#: GET routes the walk does not read, with the reason
NOT_WALKED = {
    "/api/health": "public, outside every company",
}
#: Path prefixes outside the company's data: the founder console has its own session and never reads a company's sites (plan Part 10.5)
NOT_WALKED_PREFIXES = ("/api/founder/",)
#: Answers that are files, not JSON (the money filter reads JSON; these are refused to a role without see_costs or carry no money)
FILES = ("/export.csv", "/calendar.ics", "/api/reports/{rid}/{which}")
#: A rupee figure inside a sentence ("Rs 1,500", "Rs. 12", "PKR 300")
#: The query a route needs to answer at all
QUERY = {"/api/tariffs/card": "?category=B-2(b)"}
#: The GET routes a key with sites:read and plans:read reaches (apikeys.RESOURCES; the signed plan message is an edge device's key's only, apikeys.EDGE_ONLY,
#: at a closed-loop site): the control state is the read-back and the write log too (control-edge, audit role-operator-11), which carry no money; the
#: plan table's past rows are the plan's (optimiser-plans, audit pages-B-07), their rupees stripped for a key
KEY_READS = {"/api/sites/{site_id}", "/api/sites/{site_id}/native", "/api/sites/{site_id}/overrides", "/api/sites/{site_id}/plan",
             "/api/sites/{site_id}/plan/compare", "/api/sites/{site_id}/plan/past", "/api/sites/{site_id}/plans", "/api/sites/{site_id}/plans/{plan_id}", "/api/sites/{site_id}/plans/current/message",
             "/api/sites/{site_id}/control", "/api/sites/{site_id}/control/readback", "/api/sites/{site_id}/control/writes"}
RUPEES = re.compile(r"\bRs\.?\s*-?\d|\bPKR\s*-?\d|\brupees?\b", re.IGNORECASE)


def get_routes() -> list[str]:
    from gridlytics import api
    routes = [*api.app.routes, *(r for m in api.FEATURE_ROUTES for r in m.router.routes)]
    paths = {r.path for r in routes if isinstance(r, APIRoute) and "GET" in r.methods and r.path.startswith("/api/")}
    return sorted(p for p in paths if p not in NOT_WALKED and not p.startswith(NOT_WALKED_PREFIXES))


@pytest.fixture(scope="module")
def walk(tmp_path_factory):
    import os
    folder = tmp_path_factory.mktemp("rolewalk")
    old = os.environ.get("GRIDLYTICS_DATA_DIR")
    os.environ["GRIDLYTICS_DATA_DIR"] = str(folder)
    from gridlytics import api, seed_demo
    seed_demo.seed(out=folder / "ACCOUNTS.md")
    seed_demo.load_demo_sites(4)
    # four days of demo history: the walk reads a monthly savings report all the same, which R8 2.14 refuses under a week of data (round 7,
    # savings-page; that rule is tested in test_fix_r7_savings_page)
    week = pytest.MonkeyPatch()
    from gridlytics import reports
    week.setattr(reports, "REPORT_DAYS_NEEDED", 0)
    text = (folder / "ACCOUNTS.md").read_text(encoding="utf-8")

    def password(email):
        for line in text.splitlines():
            cells = [c.strip().strip("`") for c in line.split("|")]
            if email in cells:
                return cells[cells.index(email) + 1]

    client = TestClient(api.app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    def login(email):
        r = client.post("/api/local/session", json={"email": email, "password": password(email)})
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["access_token"]}

    h = {"admin": login("owner@onyxleaf.demo"), "operator": login("operator@onyxleaf.demo"), "viewer": login("viewer@onyxleaf.demo")}
    fleet = client.get("/api/fleet", headers=h["admin"]).json()["sites"]
    sids = [s["id"] for s in fleet]
    assert len(sids) == 4
    # the edge device's key is the closed-loop demo site's: only a closed-loop site's edge is sent a plan (control-edge, flow-telemetry-plan-control-03)
    edge_site = next(s["id"] for s in fleet if s["tier"] == "closed_loop")
    # money in places a page reads rarely: a fuel log's cost, a monthly savings report and an energy-only one, a draft
    assert client.post(f"/api/sites/{sids[0]}/genset/fuel", headers=h["operator"], json={"litres": 50, "cost_rs": 14321.5, "note": "tanker"}).status_code == 200
    jobs_ = []
    for kind in ("monthly_savings", "energy_only"):
        r = client.post("/api/reports", headers=h["admin"], json={"kind": kind, "site_id": sids[0]})
        assert r.status_code == 200, r.text
        jobs_.append(r.json()["job_id"])                # a report is made as a job (round 7, storage-perf): GET /api/reports/jobs/{job_id}
    r = client.post(f"/api/sites/{sids[0]}/plan/solve", headers=h["admin"])
    assert r.status_code in (200, 202), r.text           # 202 while a slow solve still runs: its job is read all the same
    jobs_.append(r.json()["job_id"])                    # and a re-solve (round 7, storage-perf): GET /api/sites/{site_id}/jobs/{job_id}
    assert client.put("/api/drafts/walk-draft", headers=h["admin"], json={"name": "Half a site"}).status_code == 200
    # a tariff draft under the same id (UX K32): GET /api/tariffs/drafts/{draft_id}, whose body holds a rate
    assert client.put("/api/tariffs/drafts/walk-draft", headers=h["admin"], json={
        "effective_from": "2026-12-01", "source": {"ref": "walk", "verified": "primary"},
        "versions": [{"kind": "adder", "key": "fca", "body": {"rate": 1.38, "billing_months": ["2026-12"]}}]}).status_code == 200
    k = client.post("/api/platform/keys", headers=h["admin"], json={"name": "walk", "scopes": ["sites:read", "plans:read"], "site_id": edge_site,
                                                                    "kind": "edge"})
    assert k.status_code == 200, k.text
    h["key"] = {"Authorization": "Bearer " + k.json()["token"]}

    plan_ids = {}
    for sid in sids:
        rows = client.get(f"/api/sites/{sid}/plans", headers=h["admin"]).json()
        rows = rows.get("plans", rows) if isinstance(rows, dict) else rows
        plan_ids[sid] = [rows[0]["id"]] if rows else []
    fill = {"kind": ["pv", "load", "outage"], "draft_id": ["walk-draft"], "job_id": jobs_}

    answers: dict[str, dict[str, dict[str, tuple]]] = {}
    unfilled = []
    # a recompute runs as a job (round 7, savings-page, audit pr-review-05): GET /api/sites/{site_id}/savings/recompute/{job_id}, over a day with no data
    r = client.post(f"/api/sites/{sids[0]}/savings/recompute", headers=h["admin"], json={"start": "2020-01-01", "end": "2020-01-01"})
    assert r.status_code == 200, r.text
    fill.setdefault("job_id", []).append(r.json()["job_id"])
    for template in get_routes():
        if any(f in template for f in FILES):
            continue
        names = re.findall(r"{(\w+)}", template)
        if not names:
            urls = [(template, None)]
        elif names[0] == "site_id":
            urls = []
            for sid in sids:
                rest = [plan_ids[sid] if n == "plan_id" else fill.get(n) for n in names[1:]]
                if any(v is None for v in rest):
                    unfilled.append(template)
                    break
                for combo in itertools.product(*rest):
                    urls.append((template.format(site_id=sid, **dict(zip(names[1:], combo))), sid))
        else:
            values = [fill.get(n) for n in names]
            if any(v is None for v in values):
                unfilled.append(template)
                continue
            urls = [(template.format(**dict(zip(names, combo))), None) for combo in itertools.product(*values)]
        for url, sid in urls:
            url += QUERY.get(template, "")
            got = answers.setdefault(template, {}).setdefault(url, {})
            for reader in READERS:
                if reader == "key" and sid != edge_site:
                    continue                                   # a key is bound to one site; another site's path is refused (test_api_walk)
                r = client.get(url, headers=h[reader])
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
                got[reader] = (r.status_code, body)
    yield {"answers": answers, "unfilled": unfilled, "sites": sids, "edge_site": edge_site, "client": client, "headers": h}
    week.undo()
    if old is None:
        os.environ.pop("GRIDLYTICS_DATA_DIR", None)
    else:
        os.environ["GRIDLYTICS_DATA_DIR"] = old


def _items(obj, under_money=False, path=""):
    """(path, key, value, under a money key) for every leaf of an answer."""
    from gridlytics import money
    if isinstance(obj, dict):
        for k, v in obj.items():
            inside = under_money or (isinstance(k, str) and money.is_money(k))
            yield path + "/" + str(k), k, v, inside
            yield from _items(v, inside, path + "/" + str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _items(v, under_money, f"{path}[{i}]")


def _numbers(obj, *, money_only: bool | None = None) -> set[float]:
    out = set()
    for _, _, v, inside in _items({"": obj}):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and (money_only is None or inside == money_only):
            out.add(round(float(v), 2))
    return out


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _read(walk, reader):
    for template, urls in walk["answers"].items():
        for url, got in urls.items():
            if reader in got:
                yield template, url, got


def test_the_walk_reads_every_get_route(walk):
    assert walk["unfilled"] == [], f"teach the walk these routes' path parameters: {walk['unfilled']}"
    # read by the administrator, or by the key where the route takes a key only (the signed plan message)
    read = {t for t, urls in walk["answers"].items() if any(g[who][0] == 200 and g[who][1] is not None for g in urls.values() for who in ("admin", "key")
                                                             if who in g)}
    missing = [t for t in get_routes() if t not in read and not any(f in t for f in FILES)]
    assert missing == [], f"routes neither the administrator nor the key could read in the walk: {missing}"
    # the routes the docs name are among them
    for page in ("/api/sites/{site_id}/events", "/api/sites/{site_id}/pilot", "/api/sites/{site_id}/plans", "/api/sites/{site_id}/plan/compare",
                 "/api/sites/{site_id}/battery", "/api/reports", "/api/fleet/savings", "/api/alerts/rules", "/api/drafts"):
        assert page in read, page


def test_the_viewer_holds_no_money_on_any_route(walk):
    from gridlytics import money
    problems = []
    for template, url, got in _read(walk, "viewer"):
        status, body = got["viewer"]
        if body is None:
            continue
        keys = sorted({k for _, k, _, _ in _items(body) if isinstance(k, str) and money.is_money(k)})
        if keys:
            problems.append(f"{url}: money keys {keys[:6]}")
        words = [s[:80] for s in _strings(body) if RUPEES.search(s)]
        if words:
            problems.append(f"{url}: rupees in words {words[:2]}")
        a_status, a_body = got["admin"]
        if status == 200 and a_status == 200 and a_body is not None:
            shared = {x for x in _numbers(a_body, money_only=True) if abs(x) >= 50} - _numbers(a_body, money_only=False)
            leaked = sorted(shared & _numbers(body))
            if leaked:
                problems.append(f"{url}: the administrator's money figures {leaked[:5]} are in the viewer's answer")
    assert problems == [], "\n".join(problems)


def test_the_operator_holds_no_fee_and_no_money_setting_on_any_route(walk):
    from gridlytics import money
    problems = []
    for _, url, got in _read(walk, "operator"):
        body = got["operator"][1]
        keys = sorted({k for _, k, _, _ in _items(body) if k in money.FEE_KEYS | money.MONEY_SETTING_KEYS})
        if keys:
            problems.append(f"{url}: {keys}")
    assert problems == [], "\n".join(problems)


def test_a_site_key_holds_no_money_on_any_route_it_reaches(walk):
    from gridlytics import money
    problems, reached, refused = [], set(), set()
    for template, url, got in _read(walk, "key"):
        status, body = got["key"]
        if status == 200:
            reached.add(template)
        elif status == 403 and (body or {}).get("code") == "out_of_scope":
            refused.add(template)
        keys = sorted({k for _, k, _, _ in _items(body) if isinstance(k, str) and money.is_money(k)})
        if keys:
            problems.append(f"{url}: {keys[:6]}")
        if any(RUPEES.search(s) for s in _strings(body)):
            problems.append(f"{url}: rupees in words")
    # the key reaches its site's settings and plan, and nothing else of the site: every other route of the site is out of its scopes
    assert reached == KEY_READS, (sorted(reached - KEY_READS), sorted(KEY_READS - reached))
    others = {t for t, urls in walk["answers"].items() if any("key" in g for g in urls.values())} - KEY_READS
    assert others and others <= refused, sorted(others - refused)
    assert problems == [], "\n".join(problems)


def test_a_route_that_forgets_the_filter_is_filtered_all_the_same(walk):
    """The filter is the API's, not each route's (audit security-roles-02): an answer built without out() reaches a viewer without its money."""
    from gridlytics import api
    client, h = walk["client"], walk["headers"]

    def forgetful():
        return {"litres": 12.5, "cost_rs": 9876.5, "detail": {"price": 41.0, "fee_ratio": 0.3, "kwh": 3.0}, "rows": [{"saving_rs": 5.0, "day": "2026-11-01"}]}

    # under the plan's path, so a plans:read key reaches it
    api.app.add_api_route("/api/sites/{site_id}/plan/walk-forgetful", lambda site_id: forgetful(), methods=["GET"])
    added = api.app.router.routes[-1]
    try:
        sid = walk["edge_site"]                                # the key's own site
        url = f"/api/sites/{sid}/plan/walk-forgetful"
        assert client.get(url, headers=h["admin"]).json() == forgetful()
        for reader in ("viewer", "key"):
            r = client.get(url, headers=h[reader])
            assert r.status_code == 200 and r.json() == {"litres": 12.5, "detail": {"kwh": 3.0}, "rows": [{"day": "2026-11-01"}]}, (reader, r.text)
            assert int(r.headers["content-length"]) == len(r.content)
        # the operator sees costs but not the fee
        assert client.get(url, headers=h["operator"]).json()["detail"] == {"price": 41.0, "kwh": 3.0}
    finally:
        api.app.router.routes.remove(added)


def test_the_stage_track_offers_a_viewer_only_the_energy_report_and_a_key_nothing(walk):
    """UX backend ask K1: `lifecycle` on the site and on each fleet row holds no money (the walks above) and, for a viewer, states only: no stage offers
    a next step but the energy report (plan §0.3.1, D2.4). A machine key is not served it at all: its `sites:read` is the site's settings, and the
    track speaks of the site's data, its pilot and its statements, which are in no scope (audit security-roles-07; UX K1 check)."""
    seen = keys = 0
    for template in ("/api/fleet", "/api/sites/{site_id}"):
        for url, got in walk["answers"][template].items():
            for reader in ("viewer", "key"):
                if reader not in got or got[reader][0] != 200:
                    continue
                body = got[reader][1]
                if reader == "key":
                    assert "lifecycle" not in json.dumps(body), (url, body)
                    keys += 1
                    continue
                for lc in ([r["lifecycle"] for r in body["sites"]] if template == "/api/fleet" else [body["lifecycle"]]):
                    nexts = [st["next"] for st in lc["stages"]] + [lc["next"]]
                    assert set(nexts) <= {"energy_report", None}, (url, reader, lc)
                    seen += 1
    assert seen >= 5 and keys >= 1


def test_the_control_answer_is_the_administrators_without_its_money_for_a_viewer_and_a_key(walk):
    """UX backend ask K17 (plan D9.7, D3.0; owner's question Q20): the Inverter table is listed for a viewer, read only and in energy, from GET /control,
    which needs no permission. At every walked site the viewer's answer, and the edge key's at its own site, are the administrator's with every money key
    removed at any depth and nothing else changed. The gains of the issued tables and of a held re-issue are named `gain_rs` in the administrator's
    answer, which is what the filter strips, and the viewer's and the key's issued rows and re-issue hold no number at all: day, plan, time, reason and
    decision only (UX K17 check: equal to the filtered answer alone held when a gain was named so the filter left it). test_ux_k17_control_viewer
    proves the same with gains made for it."""
    from gridlytics import money
    seen, gains = 0, 0

    def rows(body):
        return [*(body.get("issued") or []), *([body["reissue"]] if body.get("reissue") else [])]

    def numbers(row):
        return [v for v in row.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]

    for url, got in walk["answers"]["/api/sites/{site_id}/control"].items():
        a_status, admin = got["admin"]
        assert a_status == 200, url
        for row in rows(admin):
            # a row of an issued table, or the held re-issue: its one figure is its gain, named as money
            assert set(k for k, v in row.items() if isinstance(v, (int, float)) and not isinstance(v, bool)) <= {"gain_rs"}, (url, row)
            if row.get("plan_id") is not None:
                assert "gain_rs" in row, (url, row)
            gains += row.get("gain_rs") is not None
        for reader in ("viewer", "key"):
            if reader in got:
                status, body = got[reader]
                assert status == 200 and body == money.for_key(admin), (url, reader)
                assert not [r for r in rows(body) if numbers(r) or any("gain" in k for k in r)], (url, reader)
                seen += 1
    assert seen == 5                                        # four sites read by the viewer, the key's own site by the key
    assert gains >= 1                                       # the walk's advisory and closed-loop sites issued tables for a gain


def test_the_plan_names_its_tariff_and_its_fuel_unit_to_every_reader(walk):
    """UX backend asks K14 and K19 (plan D9.4, D9.9): the plan's `summary.tariff` (the schedule version it was priced on) and `summary.fuel_unit`, and
    the Overview's `fuel_unit`, are words, ids and dates: the money filter leaves them whole for a viewer and a key. The Karachi feed mill's genset
    burns captive gas (MMBtu), the others' diesel (L); the Rahim Yar Khan farm is off-grid, priced on no tariff."""
    fleet = {s["id"]: s for s in walk["client"].get("/api/fleet", headers=walk["headers"]["admin"]).json()["sites"]}
    units, offgrid = {}, []
    # the latest plan made at each site (the plan list: a site whose plan is held for the moment has none in force)
    for url, got in walk["answers"]["/api/sites/{site_id}/plans"].items():
        sid = url.split("/")[3]
        summary = got["admin"][1][0]["summary"]
        for reader in ("viewer", "key"):
            if reader in got:
                other = got[reader][1][0]["summary"]
                assert (other["tariff"], other["fuel_unit"]) == (summary["tariff"], summary["fuel_unit"]), (url, reader)
        units[fleet[sid]["name"]] = summary["fuel_unit"]
        if summary["tariff"] is None:
            offgrid.append(fleet[sid]["name"])
        else:
            assert summary["tariff"]["disco"] and summary["tariff"]["category"] and summary["tariff"]["versions"], url
    assert units == {"Faisalabad textile mill (demo)": "L", "Lahore mall (demo)": "L", "Karachi feed mill (demo)": "MMBtu",
                     "Rahim Yar Khan farm (demo)": "L"}
    assert offgrid == ["Rahim Yar Khan farm (demo)"]
    for url, got in walk["answers"]["/api/sites/{site_id}/now"].items():
        assert got["viewer"][1]["fuel_unit"] == got["admin"][1]["fuel_unit"] == units[fleet[url.split("/")[3]]["name"]], url


#: The Overview's This month tiles' energy (UX backend ask K12): no money, every reader reads them
MONTH_ENERGY = ("peak_import_kwh", "pv_self_pct", "outages", "outages_covered", "unserved_kwh", "battery_cycles")


def test_the_overview_s_band_facts_and_this_month_tiles_reach_a_viewer_whole(walk):
    """UX backend ask K12 (plan D9.2, D2.3, D2.9): the plan in force with its times (`plan`), when the last plan ran out (`plan_expired_at`), today's
    apply mark (`applied_today`: who, when, how) and the month's energy figures are words, times and energy: the viewer's are the administrator's at
    every walked site. A machine key reaches no Overview (the site's data is in no scope)."""
    seen = 0
    for url, got in walk["answers"]["/api/sites/{site_id}/now"].items():
        (a_status, admin), (v_status, viewer) = got["admin"], got["viewer"]
        assert a_status == v_status == 200, url
        for k in ("plan", "plan_expired_at", "applied_today"):
            assert k in admin and viewer[k] == admin[k], (url, k)
        for k in MONTH_ENERGY:
            assert k in admin["month"] and viewer["month"][k] == admin["month"][k], (url, k)
        if admin["plan"]:
            assert admin["plan"]["generated_at"].endswith("+05:00") and admin["plan"]["valid_until"].endswith("+05:00"), url
        seen += 1
    assert seen == 4


def test_the_energy_bars_reach_a_viewer_and_the_rupee_bars_do_not(walk):
    """UX backend ask K13 (plan D9.3, D1.7): each spark day's `peak_kwh_avoided` and `litres_saved` (the energy it claims) are no money: the viewer's
    bars are the administrator's without `saving_rs` and `claimed_rs`, on Fleet and on the Overview."""
    client, h = walk["client"], walk["headers"]
    energy = ("day", "peak_kwh_avoided", "litres_saved")
    admin = {s["id"]: s["spark"] for s in client.get("/api/fleet", headers=h["admin"]).json()["sites"]}
    viewer = {s["id"]: s["spark"] for s in client.get("/api/fleet", headers=h["viewer"]).json()["sites"]}
    assert admin.keys() == viewer.keys()
    for sid, bars in admin.items():
        assert viewer[sid] == [{k: b[k] for k in energy} for b in bars], sid
    for url, got in walk["answers"]["/api/sites/{site_id}/now"].items():
        assert got["viewer"][1]["spark"] == [{k: b[k] for k in energy} for b in got["admin"][1]["spark"]], url


def test_the_grid_bill_so_far_is_named_as_money_and_reaches_no_viewer(walk):
    """UX backend ask K18 (plan D9.8, D2.10): `month.grid_bill_rs` and `month.grid_bill_rs_basis` are money names (money.is_money), so the API's
    filter strips both for a viewer (and a machine key, which reaches no Overview); the administrator reads a figure at every grid site once a whole
    day of its billing period has passed (the demo's connections are billed by the calendar month, and the bill so far starts after the reading of
    a bill entered since: the demo's sample bill), and none at the off-grid farm."""
    from datetime import date, timedelta

    from gridlytics import money
    from gridlytics.routes_common import today
    client, h = walk["client"], walk["headers"]
    assert money.is_money("grid_bill_rs") and money.is_money("grid_bill_rs_basis")
    names = {s["id"]: s["name"] for s in client.get("/api/fleet", headers=h["admin"]).json()["sites"]}
    bills = {}
    for url, got in walk["answers"]["/api/sites/{site_id}/now"].items():
        admin, viewer = got["admin"][1]["month"], got["viewer"][1]["month"]
        assert "grid_bill_rs" in admin and "grid_bill_rs_basis" in admin, url
        assert not [k for k in viewer if "grid_bill" in k], url
        bills[names[url.split("/")[3]]] = admin["grid_bill_rs"]
        if admin["grid_bill_rs"] is not None:
            basis = admin["grid_bill_rs_basis"]
            # the calendar month's first day, or the day after the reading of a bill entered since (the demo's sample bill, read two days ago:
            # the bill so far starts after the DISCO's own reading, K18 check)
            read = [b["period_end"] for b in client.get(url.replace("/now", "/bills"), headers=h["admin"]).json()
                    if today().replace(day=1).isoformat() <= b["period_end"] <= today().isoformat()]
            first = (date.fromisoformat(max(read)) + timedelta(days=1)).isoformat() if read else today().replace(day=1).isoformat()
            assert basis["first_day"] == first and basis["computed"].endswith("+05:00"), url
    assert bills["Rahim Yar Khan farm (demo)"] is None
    grid = [n for n in bills if n != "Rahim Yar Khan farm (demo)"]
    if today().day > 1:
        assert all(isinstance(bills[n], float) and bills[n] > 0 for n in grid), bills


def test_a_request_for_access_is_anyones_to_send_and_only_the_founders_to_read(walk, monkeypatch):
    """UX backend asks K2 and K3: the contact page's request for access goes to the founder from anyone signed in (202 with no body: nothing to filter;
    a site key is refused, as on every path that names no site), and none of the company's readers reaches the founder's Access requests, whose
    session is the founder's own (401). The founder's answer holds no money key and no rupee: a request is no account and no company's data."""
    from gridlytics import access_requests, founder, money
    client, h = walk["client"], walk["headers"]
    monkeypatch.setattr(access_requests, "RATE_LIMIT", 100)
    for name, value in (("GRIDLYTICS_FOUNDER_USERNAME", "founder"), ("GRIDLYTICS_FOUNDER_PASSWORD_HASH", founder.hash_password("a made-up walk password")),
                        ("GRIDLYTICS_FOUNDER_TOTP_SECRET", "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"),
                        ("GRIDLYTICS_FOUNDER_SESSION_SECRET", "a made-up founder session secret of the role walk")):
        monkeypatch.setenv(name, value)
    body = {"topic": "person", "name": "Walk", "email": "walk@onyxleaf.demo", "company": "OnyxLeaf demo", "message": "Add Bilal as an operator."}
    for reader in READERS:
        r = client.post("/api/public/access-requests", json=body, headers=h[reader])
        assert (r.status_code, r.content) == ((403, r.content) if reader == "key" else (202, b"")), (reader, r.status_code, r.text[:200])
        for method, path in (("GET", "/api/founder/access-requests"), ("PATCH", f"/api/founder/access-requests/{uuid.uuid4()}")):
            r = client.request(method, path, headers=h[reader], json={"state": "done"} if method == "PATCH" else None)
            assert r.status_code == 401 and r.json()["code"] == "founder_session_required", (reader, method, r.text[:200])
    token, _ = founder.new_token()
    got = client.get("/api/founder/access-requests", headers={"x-founder-session": token}).json()
    sent = [r for r in got["requests"] if r["email"] == "walk@onyxleaf.demo"]
    assert sorted(r["sent_by"]["role"] for r in sent) == ["admin", "operator", "viewer"]
    assert not {k for _, k, _, _ in _items(got) if isinstance(k, str) and money.is_money(k)} and not any(RUPEES.search(s) for s in _strings(got))


def test_the_tariff_editor_reaches_no_viewer_and_no_key_and_names_its_rupees_as_money(walk):
    """UX backend ask K32 (plan F11 item 6): the editor's routes need see_costs at least (rates are money, R8 2.3), drafting needs edit_tariffs; a
    viewer and a machine key are refused every one, an operator reads a waiting version's prices and simulated bill but drafts nothing. Every answer
    carries its rupees under money names (the week of prices, the simulated bill, what a version changes, a draft's body), so the API's filter would
    leave a reader without see_costs none of them (UX K32 check: the validate answer and the drafts too)."""
    from gridlytics import money
    client, h = walk["client"], walk["headers"]
    sid = next(s["id"] for s in client.get("/api/fleet", headers=h["admin"]).json()["sites"] if s["name"] == "Faisalabad textile mill (demo)")
    waiting = next(v for v in client.get("/api/tariffs/versions?kind=adder", headers=h["admin"]).json() if v["status"] == "pending")
    draft = {"effective_from": "2026-11-01", "source": {"ref": "walk", "verified": "primary"},
             "versions": [{"kind": "adder", "key": "fca", "body": {"rate": 3.1, "billing_months": ["2026-11"]}}]}
    # the demo's sample bill (the mall's): rebuilt under the draft
    bill = next(b for s in walk["sites"] for b in client.get(f"/api/sites/{s}/bills", headers=h["admin"]).json())
    calls = {"validate": ("POST", "/api/tariffs/versions/validate", draft), "drafts": ("GET", "/api/tariffs/drafts", None),
             "draft": ("GET", "/api/tariffs/drafts/walk-draft", None), "save": ("PUT", "/api/tariffs/drafts/walk-k32", draft),
             "preview": ("POST", "/api/tariffs/preview", {"site_id": sid, "version_id": waiting["id"], "days": 1}),
             "preview_draft": ("POST", "/api/tariffs/preview", {"site_id": sid, "draft": draft, "days": 1}),
             "simulate": ("POST", "/api/tariffs/simulate", {"site_id": bill["site_id"], "bill_id": bill["id"], "draft": draft}),
             "simulate_version": ("POST", "/api/tariffs/simulate", {"site_id": bill["site_id"], "bill_id": bill["id"], "version_id": waiting["id"]}),
             "withdraw": ("POST", f"/api/tariffs/versions/{waiting['id']}/withdraw", {"reason": "walk"}),
             "submit": ("POST", "/api/tariffs/drafts/walk-draft/submit", None), "discard": ("DELETE", "/api/tariffs/drafts/walk-draft", None)}
    #: tested on their refusals alone: each changes what the others read
    changing = {"withdraw", "submit", "discard"}
    allowed = {"admin": set(calls) - changing, "operator": {"preview", "simulate_version"}, "viewer": set(), "key": set()}
    #: the money names each answer carries its rupees under
    named = {"validate": {"tariff_changes"}, "drafts": {"tariff_body"}, "draft": {"tariff_body"}, "save": {"tariff_body"},
             "preview": {"c_imp", "c_exp", "rates"}, "preview_draft": {"c_imp", "c_exp", "rates"}, "simulate": {"bill"}, "simulate_version": {"bill"}}
    answers = {}
    for reader in READERS:
        for name, (method, url, body) in calls.items():
            if name in changing:
                continue
            r = client.request(method, url, json=body, headers=h[reader])
            want = 200 if name in allowed[reader] else 403
            assert r.status_code == want, (reader, name, r.status_code, r.text[:200])
            if r.status_code == 200:
                answers[(reader, name)] = r.json()
            else:
                assert not any(RUPEES.search(s) for s in _strings(r.json())), (reader, name)
    assert {name for _, name in answers} == set(calls) - changing
    for (reader, name), body in answers.items():
        names = {k for _, k, _, _ in _items(body) if isinstance(k, str) and money.is_money(k)}
        assert named[name] <= names, (reader, name, names)
        kept = money.for_key(body)
        assert not (_numbers(body, money_only=True) - _numbers(body, money_only=False)) & _numbers(kept), (reader, name)
        if name in ("validate", "drafts", "draft", "save"):
            assert _numbers(kept) == set(), (reader, name, kept)       # no figure of a rate is left outside a money name
    assert answers[("operator", "preview")]["in_force"]["c_imp"] == answers[("admin", "preview")]["in_force"]["c_imp"]
    assert answers[("operator", "simulate_version")]["bill"] == answers[("admin", "simulate_version")]["bill"]
    # a viewer, a key and an operator never withdraw, submit or discard; the shipped waiting version has no author, so only an administrator withdraws
    for reader in ("viewer", "key", "operator"):
        for name in sorted(changing):
            method, url, _ = calls[name]
            assert client.request(method, url, json={} if method == "POST" else None, headers=h[reader]).status_code == 403, (reader, name)


def test_set_up_s_check_names_the_wear_cost_as_a_money_setting_and_reaches_no_viewer_and_no_key(walk):
    """UX backend ask K6 (plan C19.2 item 2): the check's battery wear cost (`wear_cost_rs_per_kwh`) is a money setting by name (money.MONEY_SETTING_KEYS),
    so the API's filter strips it for an operator (plan 9.4: costs but not the money settings) and for a key; the route needs see_costs, so a viewer and
    a key are refused, and the operator's answer is the administrator's without it."""
    from gridlytics import money
    client, h = walk["client"], walk["headers"]
    sid = walk["edge_site"]
    body = {"assets": {"battery": {"nameplate_kwh": 100, "p_ch_kw": 50, "p_dis_kw": 50}}}
    answers = {}
    for reader in READERS:
        r = client.post(f"/api/sites/{sid}/validate", json=body, headers=h[reader])
        assert r.status_code == (200 if reader in ("admin", "operator") else 403), (reader, r.status_code, r.text[:200])
        answers[reader] = r.json()
        if r.status_code != 200:
            assert not any(RUPEES.search(s) for s in _strings(r.json())), reader
    admin, operator = answers["admin"], answers["operator"]
    assert "wear_cost_rs_per_kwh" in money.MONEY_SETTING_KEYS and money.is_money("wear_cost_rs_per_kwh")
    assert admin["derived"]["battery"]["wear_cost_rs_per_kwh"] > 0
    assert not {k for _, k, _, _ in _items(operator) if k in money.FEE_KEYS | money.MONEY_SETTING_KEYS}
    # a key's filter would leave no figure of the administrator's that is held only under a money name
    assert not (_numbers(admin, money_only=True) - _numbers(admin, money_only=False)) & _numbers(money.for_key(admin))
    admin["derived"]["battery"].pop("wear_cost_rs_per_kwh")
    assert operator == admin


def test_answers_that_are_files_pass_unchanged(walk):
    """A CSV or a calendar file is not JSON: the filter leaves it as the route made it."""
    client, h = walk["client"], walk["headers"]
    sid = walk["sites"][0]
    # unpacked (Accept-Encoding: identity): a file over a kilobyte goes gzipped to a client that takes it (audit R7 performance-live-10), so its length
    # is then the packed one
    r = client.get(f"/api/sites/{sid}/pilot/calendar.ics", headers={**h["viewer"], "Accept-Encoding": "identity"})
    assert r.status_code in (200, 404), r.text
    if r.status_code == 200:
        assert not r.headers["content-type"].startswith("application/json") and int(r.headers["content-length"]) == len(r.content)


def test_the_walk_keeps_every_answer_json(walk):
    """Every walked answer that was JSON for the administrator is JSON for the others too (the filter never breaks an answer)."""
    for _, url, got in _read(walk, "admin"):
        for reader, (status, body) in got.items():
            if got["admin"][1] is not None and status == 200:
                assert body is not None, (url, reader)
                json.dumps(body)
