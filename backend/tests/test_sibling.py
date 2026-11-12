"""The web app against the API it draws, through the web repository beside this one (round 7, audit parity-architecture-09 and its duplicate
tests-and-perf-18: Solarlytics' split mechanism, stage S0a, carried over).

Before, nothing read the other repository: the web app's permission names, sign-in refusal codes, money fields and test fixtures were copied by hand from
this one, and fields the API serves were missing from its types and fixtures (`today.import_completeness`, the plan steps' `grid_charge_permitted`, the plan
summary's `control_release`). Each test here holds one of those copies to the server's own:

- `sibling.py` finds the web repository (GRIDLYTICS_FRONTEND_DIR, else `gridlytics-frontend` beside this one; never a folder holding the engine too);
- the web app's `Permission` union is `auth.PERMISSIONS`, its sign-in refusal table is `api.SIGN_IN_CODES`, every header it sends the API is one CORS
  allows and every one it reads is one CORS exposes, and its money field lists are the server's (`routes_sites`), whose viewer filter (`money.is_money`)
  leaves nothing in its viewer fixtures;
- the fixtures carry the shape of the API's answers (`fixture_contract.py`: every key path the API serves, none it no longer serves), the role map the
  page tests use is the one `GET /api/meta` answers each role (`meta_roles.json`, read by `metaFor`), and the typed answers' every field is in
  `types/api.ts`;
- a document both repositories carry (TROUBLESHOOTING, ACCOUNTS) is the same in both.

Nothing here reads the owner's data folder: the fixture contract seeds a temporary one."""
from __future__ import annotations

import json
import re
from pathlib import Path

import fixture_contract as fc
import pytest
import sibling

HERE = Path(__file__).resolve().parent


def _make(root: Path, *files: str) -> Path:
    for rel in files:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x", encoding="utf-8")
    return root


def _web(rel: str) -> str:
    return sibling.frontend_file(rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def _fixture(name: str):
    return json.loads(_web(f"frontend/src/test/fixtures/{name}.json"))


# ------------------------------------------------------------------------------------------------ the mechanism (Solarlytics' test_sibling, re-aimed)

def test_the_web_repository_is_the_named_folder_else_gridlytics_frontend_beside_and_never_a_one_folder_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(sibling, "REPO", tmp_path / "gridlytics-backend")
    monkeypatch.delenv("GRIDLYTICS_FRONTEND_DIR", raising=False)
    monkeypatch.delenv("GRIDLYTICS_SKIP_SIBLING", raising=False)
    # only a folder holding both the engine and the web app is around: it is not looked at, and refused when named
    both = _make(tmp_path / "gridlytics", "frontend/package.json", "backend/gridlytics/api.py", "frontend/src/types/api.ts")
    with pytest.raises(FileNotFoundError, match="clone the web repository"):
        sibling.web_root()
    monkeypatch.setenv("GRIDLYTICS_FRONTEND_DIR", str(both))
    with pytest.raises(FileNotFoundError, match="is not the web repository"):
        sibling.web_root()
    monkeypatch.delenv("GRIDLYTICS_FRONTEND_DIR")
    web = _make(tmp_path / "gridlytics-frontend", "frontend/package.json", "frontend/src/types/api.ts")
    assert sibling.web_root() == web
    assert sibling.frontend_file("frontend/src/types/api.ts") == web / "frontend" / "src" / "types" / "api.ts"
    elsewhere = _make(tmp_path / "elsewhere", "frontend/package.json")
    monkeypatch.setenv("GRIDLYTICS_FRONTEND_DIR", str(elsewhere))
    assert sibling.web_root() == elsewhere.resolve()


def test_a_missing_file_or_folder_fails_and_a_missing_repository_skips_only_when_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(sibling, "REPO", tmp_path / "gridlytics-backend")
    monkeypatch.delenv("GRIDLYTICS_FRONTEND_DIR", raising=False)
    monkeypatch.setenv("GRIDLYTICS_SKIP_SIBLING", "1")
    with pytest.raises(pytest.skip.Exception):
        sibling.frontend_file("frontend/src/types/api.ts")
    _make(tmp_path / "gridlytics-frontend", "frontend/package.json")
    # a scan of a folder that is not there would pass with nothing scanned: it fails, whatever the skip switch says
    with pytest.raises(FileNotFoundError, match="has no frontend/src"):
        sibling.frontend_file("frontend/src")
    monkeypatch.delenv("GRIDLYTICS_SKIP_SIBLING")
    with pytest.raises(FileNotFoundError, match="has no frontend/src/types/api.ts"):
        sibling.frontend_file("frontend/src/types/api.ts")


def test_this_repository_holds_no_web_app_and_the_web_repository_holds_no_engine():
    for rel in ("frontend", "scripts/e2e", "scripts/start_local.py"):
        assert not (sibling.REPO / rel).exists(), rel
    assert (sibling.web_root() / "frontend" / "src").is_dir()
    assert not (sibling.web_root() / "backend").exists()


def test_no_backend_test_reads_the_web_files_but_through_sibling():
    direct = re.compile(r'''gridlytics-frontend["'/]|/ "frontend" /|"\.\./gridlytics-frontend''')
    readers = sorted(p.name for p in HERE.glob("*.py") if p.name not in {"sibling.py", "test_sibling.py"} and direct.search(p.read_text(encoding="utf-8")))
    assert readers == []


# ------------------------------------------------------------------------------------------------ the copies the web app keeps of the server's tables

def _union(text: str, name: str) -> set[str]:
    m = re.search(rf"export type {name} =(.*?);\n", text, re.S)
    assert m, f"no type {name}"
    return set(re.findall(r'"([a-z_]+)"', re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)))


def test_the_web_apps_permission_names_are_the_servers():
    from gridlytics import auth
    assert _union(_web("frontend/src/types/api.ts"), "Permission") == set(auth.PERMISSIONS)


def test_the_role_map_the_page_tests_use_is_the_one_the_meta_route_answers(tmp_path, monkeypatch):
    # tests-and-perf-18: render.tsx's metaFor kept its own copy of who may do what; it reads meta_roles.json now, which must be what GET /api/meta
    # answers each role (auth.PERMISSIONS), and meta.json (the administrator's answer) names every permission
    from gridlytics import auth
    roles = _fixture("meta_roles")
    assert set(roles) == set(auth.ROLES)
    for role, perms in roles.items():
        assert perms == {p: role in who for p, who in auth.PERMISSIONS.items()}, role
    assert _fixture("meta")["permissions"] == roles["admin"]
    render = _web("frontend/src/test/render.tsx")
    body = render[render.index("export function metaFor"):]
    body = body[:body.index("\n}\n")]
    assert "fx.metaRoles" in body and "fx.meta" in body and '"manage_members"' not in body, "metaFor builds the role map from the fixtures"


def test_the_web_apps_sign_in_refusal_table_is_the_servers():
    # api.SIGN_IN_CODES: each code and its status, as the web app's table of what a refusal does (authstate.test.ts) lists them; the table's other
    # rows are the page's own codes (forbidden, invalid)
    from gridlytics import api
    rows = re.findall(r'\[(\d{3}), "([a-z_]+)", (?:true|false), "[a-z-]+"\]', _web("frontend/src/features/auth/authstate.test.ts"))
    table = {code: int(status) for status, code in rows}
    assert {c: table.get(c) for c in api.SIGN_IN_CODES} == api.SIGN_IN_CODES
    assert set(table) - set(api.SIGN_IN_CODES) <= {"forbidden", "invalid"}


#: Files that call another service, not this API: Supabase's own sign-in endpoints take its `apikey` header
_OTHER_SERVICES = {"features/auth/supabase-auth.service.ts"}
#: Response headers a browser lets a page read without CORS exposing them (the CORS-safelisted response headers)
_SAFELISTED = {"cache-control", "content-language", "content-length", "content-type", "expires", "last-modified", "pragma"}


def _web_sources() -> dict[str, str]:
    src = sibling.frontend_file("frontend/src")
    return {p.relative_to(src).as_posix(): p.read_text(encoding="utf-8") for p in sorted(src.rglob("*.ts*"))
            if not re.search(r"\.test\.tsx?$", p.name) and "test" not in p.relative_to(src).parts}


def test_every_header_the_web_app_sends_is_one_cors_allows_and_every_one_it_reads_one_cors_exposes():
    from gridlytics import api
    sent, read = set(), set()
    for rel, text in _web_sources().items():
        if rel in _OTHER_SERVICES:
            continue
        sent |= {a or b for a, b in re.findall(r'headers\["([A-Za-z-]+)"\]\s*=|headers\.([A-Za-z]+)\s*=', text)}
        for literal in re.findall(r"headers: Record<string, string> = \{([^}]*)\}", text):
            sent |= {a or b for a, b in re.findall(r'"([A-Za-z-]+)":|\b([A-Z][A-Za-z-]*):', literal)}
        read |= set(re.findall(r'headers\.get\("([A-Za-z-]+)"\)', text))
    # the scan found what the client sends (the session's token and company, the request id, a body's type, the founder's session)
    assert {"Authorization", "X-Gridlytics-Company", "X-Request-ID", "Content-Type", "x-founder-session"} <= sent
    allowed = {h.lower() for h in api.CORS_ALLOW_HEADERS}
    assert sorted(h for h in sent if h.lower() not in allowed) == []
    assert "X-Gridlytics-Role" in read
    exposed = {h.lower() for h in api.CORS_EXPOSE_HEADERS} | _SAFELISTED
    assert sorted(h for h in read if h.lower() not in exposed) == []


def _ts_strings(text: str, start: str) -> list[str]:
    block = text[text.index(start):]
    block = block[:block.index(";")]
    return re.findall(r'"([a-z_]+)"', block)


def test_the_web_apps_money_fields_are_the_servers():
    from gridlytics import routes_sites
    form = _web("frontend/src/features/sites/onboarding/site-form.ts")
    block = form[form.index("export const ASSET_MONEY_FIELDS"):]
    block = block[:block.index("};")]
    web_assets = {role: tuple(re.findall(r'"([a-z_]+)"', names)) for role, names in re.findall(r"(\w+): \[([^\]]*)\]", block)}
    assert web_assets == routes_sites.ASSET_MONEY_FIELDS
    tabs = _web("frontend/src/features/sites/onboarding/onboarding-tabs.ts")
    site_keys = set(_ts_strings(tabs, "export const SITE_KEYS"))
    assert set(_ts_strings(tabs, "const MONEY_SITE_KEYS")) == set(routes_sites.SITE_MONEY_FIELDS) & site_keys


def _keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}", k
            yield from _keys(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v, f"{path}[]")


def test_the_viewer_fixtures_hold_nothing_the_servers_money_filter_strips():
    # the page tests draw a viewer's pages from viewer_*.json: a money key there (by money.is_money, the fee's and the money settings' names) is a
    # field the server never sends a viewer, so a page tested on it would be tested on an answer that cannot happen
    from gridlytics import money
    names = [p.name[:-5] for p in sibling.frontend_file("frontend/src/test/fixtures").glob("viewer_*.json")]
    assert len(names) >= 6
    for name in names:
        bad = sorted({p for p, k in _keys(_fixture(name)) if money.is_money(k) or k in money.FEE_KEYS | money.MONEY_SETTING_KEYS})
        assert bad == [], name


#: The documents of the family's set that both repositories carry, word for word (plan Part 10.1; the docs package writes them)
SHARED_DOCS = ("docs/TROUBLESHOOTING.md", "docs/ACCOUNTS.md")


def test_a_document_both_repositories_carry_is_the_same_in_both():
    for rel in SHARED_DOCS:
        here, there = sibling.REPO / rel, sibling.web_root() / rel
        if here.exists() or there.exists():
            assert here.exists() and there.exists(), f"{rel} is in one repository only"
            assert here.read_text(encoding="utf-8").replace("\r\n", "\n") == there.read_text(encoding="utf-8").replace("\r\n", "\n"), rel


# ------------------------------------------------------------------------------------------------ the fixtures against a seeded scratch backend

@pytest.fixture(scope="module")
def live(tmp_path_factory):
    folder = tmp_path_factory.mktemp("fixtures")
    variants: dict = {}
    with fc.seeded(folder) as (client, headers, ids):
        answers = fc.capture(client, headers, ids, variants)
    answers["__variants__"] = variants
    yield answers


def test_the_fixtures_carry_every_field_the_api_serves_and_none_it_no_longer_serves(live):
    names = fc.backend_names()
    wrong = {}
    for name in [*fc.FIXTURES, fc.META_ROLES]:
        served, stale = fc.differences(_fixture(name), live[name], names, live["__variants__"].get(name))
        if served or stale:
            wrong[name] = {"served and missing": sorted(served), "no longer served": sorted(stale)}
    assert wrong == {}, f"re-capture the fixtures' shape (python backend/tests/fixture_contract.py --write): {json.dumps(wrong)}"


def test_every_fixture_names_the_route_and_the_reader_it_is_the_answer_of():
    # each fixture file has its entry in FIXTURES (a new fixture is checked too), and each entry is a GET route the API mounts
    from fastapi.routing import APIRoute

    from gridlytics import api
    files = {p.name[:-5] for p in sibling.frontend_file("frontend/src/test/fixtures").glob("*.json")}
    assert files == set(fc.FIXTURES) | {fc.META_ROLES}
    routes = {r.path for r in [*api.app.routes, *(r for m in api.FEATURE_ROUTES for r in m.router.routes)] if isinstance(r, APIRoute) and "GET" in r.methods}
    for name, (reader, route) in fc.FIXTURES.items():
        assert reader in fc.PEOPLE, name
        template = re.sub(r"\{(mill|farm|mall|feedmill)\}", "{site_id}", route.split("?")[0])
        template = {"/api/sites/{site_id}/forecasts/load": "/api/sites/{site_id}/forecasts/{kind}",
                    "/api/sites/{site_id}/forecasts/outage": "/api/sites/{site_id}/forecasts/{kind}",
                    "/api/sites/{site_id}/forecasts/pv": "/api/sites/{site_id}/forecasts/{kind}"}.get(template, template)
        assert template in routes, (name, template)


#: The answers the pages read as typed objects (types/api.ts), whose every served field is typed there. The rest are typed loosely on purpose (a tariff
#: version's body, an edge's own messages, the forecasters' series, a bill's rebuild, the audit's details) and read field by field where a page reads one.
TYPED = ("now", "viewer_now", "plan", "viewer_plan", "plans", "overview_chart", "savings", "viewer_savings", "telemetry_days", "prices", "fleet",
         "viewer_fleet", "battery", "viewer_battery", "genset", "viewer_genset", "alerts", "alerts_counts", "pilot", "switchback", "quality", "fleet_savings",
         "compare", "demo", "meta", "reports", "users", "keys")
#: Objects inside a typed answer that are free-form there (a record of names to figures, or a detail whose fields depend on its kind)
LOOSE = {"detail", "breakdown", "levers", "levers_claimed_rs", "body", "permissions", "brand_grammars", "icp", "series"}


def test_every_field_of_a_typed_answer_is_in_the_web_apps_types(live):
    ts = _web("frontend/src/types/api.ts")
    typed = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\??\s*:", ts)) | set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', ts))
    missing = {}
    for name in TYPED:
        out = sorted(p for p in fc.paths(live[name]) if (n := fc._name(p)) and n not in typed
                     and not set(re.split(r"\.|\[\]|\{\}", p)[:-1]) & LOOSE)
        if out:
            missing[name] = out
    assert missing == {}, f"type these served fields in frontend/src/types/api.ts: {json.dumps(missing)}"


def test_the_fixture_contract_sees_a_missing_and_a_stale_field_and_re_captures_without_changing_a_value():
    # the contract itself: a field the API serves and the fixture lacks, one the fixture holds and the code no longer names, a list's elements, a map
    # whose keys are data, an empty list (nothing to compare under it), and the re-capture keeping every value the fixture holds
    live = {"plan": {"steps": [{"t": 1, "grid_charge_permitted": True}, {"t": 2, "grid_charge_permitted": False}],
                     "summary": {"control_release": None, "by_site": {"a1b2c3d4e5f6": {"x": 1}}}}, "alerts": []}
    fixture = {"plan": {"steps": [{"t": 9}], "summary": {"revert_after_hours_gone": 3, "by_site": {"0f0f0f0f0f0f": {"x": 5}}}},
               "alerts": [{"old_field_no_one_names": 1}]}
    names = fc.backend_names()
    served, stale = fc.differences(fixture, live, names)
    assert served == {"plan.steps[].grid_charge_permitted", "plan.summary.control_release"}
    assert stale == {"plan.summary.revert_after_hours_gone"}
    merged = fc.merge(fixture, live, names)
    assert merged == {"plan": {"steps": [{"t": 9, "grid_charge_permitted": True}],
                               "summary": {"by_site": {"0f0f0f0f0f0f": {"x": 5}}, "control_release": None}},
                      "alerts": [{"old_field_no_one_names": 1}]}
    assert fc.differences(merged, live, names) == (set(), set())


def test_the_fields_of_a_reason_detail_are_never_asked_of_a_fixture_whatever_the_hour():
    # UX step 0: at 18:00 in Karachi every demo site's current slot charged for reserve, so now_next.current.detail carried rs_per_kwh at all four,
    # and the contract asked the fixtures (captured on a slot that held for an outage) for it; a detail's fields follow the slot's reason, not the route
    live = {"now_next": {"current": {"reason": "charge_for_reserve", "detail": {"kwh": 78.0, "rs_per_kwh": 26.98}}, "next": [{"detail": {"p": 0.9}}]}}
    fixture = {"now_next": {"current": {"reason": "hold_for_outage", "detail": {"p": 0.03, "hours": 1}}, "next": [{"detail": {"p": 0.1}}]}}
    names = fc.backend_names()
    assert fc.differences(fixture, live, names, [live]) == (set(), set())
    # the detail itself is still asked for
    lacking = {"now_next": {"current": {"reason": "hold_for_outage"}, "next": [{"detail": {"p": 0.1}}]}}
    assert fc.differences(lacking, live, names) == ({"now_next.current.detail"}, set())


def test_a_detail_that_does_not_follow_a_slots_reason_keeps_its_fields_asked():
    # UX step 0 check: the first fix exempted the fields of every `detail` (an alert's, an audit entry's), so a backend rename of a field the web app reads
    # there would no longer show as missing from the fixtures; only a detail beside a `reason` follows the hour
    live = {"alerts": [{"kind": "outage_reserve", "detail": {"outages": [], "from_stage": 1}},
                       {"kind": "outage_reserve", "detail": {"outages": [], "from_stage": 2}}],
            "audit": {"detail": {"role_before": "viewer", "role": "analyst"}}}
    fixture = {"alerts": [{"kind": "outage_reserve", "detail": {"outages": []}}], "audit": {"detail": {"role": "analyst"}}}
    names = fc.backend_names()
    assert fc.differences(fixture, live, names) == ({"alerts[].detail.from_stage", "audit.detail.role_before"}, set())
    # beside a reason the same detail's fields are not asked
    assert fc.required({"reason": "hold_for_outage", "detail": {"p": 0.03}}) == {"reason", "detail"}


def test_a_re_capture_runs_on_a_scratch_database_whatever_the_shell_says(tmp_path):
    # review (round 7, tests-ci): `fixture_contract.py --write` seeds a demo; a shell that names the owner's database, another sign-in mode or a data
    # folder must not carry the seed there (before: setdefault kept a GRIDLYTICS_DB the shell already held)
    import inspect
    env = {"GRIDLYTICS_DB": str(sibling.REPO / "data" / "gridlytics.db"), "GRIDLYTICS_AUTH_MODE": "supabase", "GRIDLYTICS_DATA_DIR": str(sibling.REPO / "data"),
           "GRIDLYTICS_RESET_BACKUP": "1", "GRIDLYTICS_HOSTS": "app.example.com"}
    fc.scratch_env(env, tmp_path)
    assert env == {"GRIDLYTICS_DB": str(tmp_path / "scratch.db"), "GRIDLYTICS_AUTH_MODE": "local", "GRIDLYTICS_RESET_BACKUP": "0",
                   "GRIDLYTICS_HOSTS": "127.0.0.1"}
    source = inspect.getsource(fc.main)
    assert "scratch_env(os.environ" in source and "setdefault" not in source


def test_an_outage_in_progress_is_typed_whatever_the_hour_the_fixtures_are_checked_at(tmp_path, monkeypatch):
    # review (round 7, tests-ci): the typed-fields test reads a demo seeded at the current time, so a field served only while the grid is down (the plan
    # summary's outage_now: forecast.outage's `observed`) was checked only in a run that met a demo outage, and failed then. Here an outage in progress
    # is made at a fixed moment, and every field of `observed` must be typed in PlanSummary.outage_now
    from datetime import datetime, timedelta, timezone

    from gridlytics import forecast, store, telemetry
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    t0 = int(datetime(2026, 11, 2, 10, 0, tzinfo=timezone(timedelta(hours=5))).timestamp())
    site = {"id": "outage-site", "data": {"disco": "LESCO", "lat": 31.5, "lon": 74.3}, "connections": [{"id": "c1", "data": {"regime": "none"}}],
            "assets": {}}
    with store.use_company(db):
        telemetry.ingest(site["id"], [{"ts": t0 + 60 * i, "grid_present": 0 if i >= 3 else 1} for i in range(-10, 12)], "edge")
        observed = forecast.outage(site, t0, now=t0 + 750)["observed"]
    assert observed and set(observed) >= {"since", "elapsed_min", "expected_remaining_min"}
    ts = _web("frontend/src/types/api.ts")
    summary = ts[ts.index("export interface PlanSummary {"):]
    m = re.search(r"\n  outage_now\??: (.*?);\n", summary[:summary.index("\n}\n") + 1])
    assert m, "PlanSummary.outage_now"
    assert sorted(k for k in observed if not re.search(rf"\b{k}\??:", m.group(1))) == [], m.group(1)
