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
    removed at any depth (the issued tables' and the held re-issue's `gain_rs` among them) and nothing else changed. test_ux_k17_control_viewer proves it
    with gains in the answer."""
    from gridlytics import money
    seen = 0
    for url, got in walk["answers"]["/api/sites/{site_id}/control"].items():
        a_status, admin = got["admin"]
        assert a_status == 200, url
        for reader in ("viewer", "key"):
            if reader in got:
                status, body = got[reader]
                assert status == 200 and body == money.for_key(admin), (url, reader)
                seen += 1
    assert seen == 5                                        # four sites read by the viewer, the key's own site by the key


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
