"""The web app's test fixtures against the API's own answers (round 7, audit parity-architecture-09 and its duplicate tests-and-perf-18).

The web app's page tests render real answers of the scratch backend (`frontend/src/test/fixtures/*.json` in the web repository). They were captured once
(6 November 2026) and newer fields were hand-added where a test needed one, so a page could pass its tests on a shape the API no longer serves, and a field
the API serves could be missing from them and from the page's types. Here each fixture is named with the route and the reader it is the answer of
(`FIXTURES`), a scratch data folder is seeded as the browser check seeds one (`seed_demo`: the demo companies and accounts, the four twin sites with a few
days of history), and each route is read as that reader. `test_sibling.py` compares every fixture's key paths with the live answer's; this module's
`main` re-captures the fixtures' shape:

    python backend/tests/fixture_contract.py [--write]

reads the same answers from a temporary folder (never `data/`), prints each fixture's differences, and with `--write` adds every key the API serves and the
fixture lacks (its live value), and removes every key the fixture holds and the API no longer serves; a value the fixture already holds is kept, so the
page tests that read it keep their figures. The web repository is found as the tests find it (`sibling.py`: GRIDLYTICS_FRONTEND_DIR, else
`gridlytics-frontend` beside this repository).

Key paths are written `plan.steps[].genset_dump_kw`: `[]` for every element of a list, `{}` for every key of a map whose keys are data (site ids, days,
months, hours, category names, alert rules)."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: The demo sites by the name the fixtures were captured on (demo.DEMO_SITES)
SITES = {"mill": "Faisalabad textile mill (demo)", "mall": "Lahore mall (demo)", "feedmill": "Karachi feed mill (demo)", "farm": "Rahim Yar Khan farm (demo)"}
#: Each fixture: the reader it was answered to and its route ({mill}, {farm}, {mall}, {feedmill}: that demo site's id), with the query the page sends
FIXTURES = {
    "alerts": ("admin", "/api/alerts?state="),
    "alerts_counts": ("admin", "/api/alerts/counts"),
    "alerts_rules": ("admin", "/api/alerts/rules"),
    "audit": ("admin", "/api/audit"),
    "battery": ("admin", "/api/sites/{mill}/battery"),
    "bills": ("admin", "/api/sites/{mall}/bills"),
    "compare": ("admin", "/api/sites/{mill}/plan/compare"),
    "control": ("admin", "/api/sites/{mill}/control"),
    "demo": ("admin", "/api/demo"),
    "edge": ("admin", "/api/sites/{feedmill}/edge"),
    "fc_load": ("admin", "/api/sites/{mill}/forecasts/load"),
    "fc_outage": ("admin", "/api/sites/{mill}/forecasts/outage"),
    "fc_pv": ("admin", "/api/sites/{mill}/forecasts/pv"),
    "fc_scores": ("admin", "/api/sites/{mill}/forecasts/scores?days=30"),
    "fc_scores_r7": ("admin", "/api/sites/{mill}/forecasts/scores?days=30"),
    "fleet": ("admin", "/api/fleet"),
    "fleet_savings": ("admin", "/api/fleet/savings"),
    "genset": ("admin", "/api/sites/{farm}/genset?days=14"),
    "heatmap": ("admin", "/api/sites/{mill}/telemetry/heatmap?name=load_power_w&days=28"),
    "keys": ("admin", "/api/platform/keys"),
    "meta": ("admin", "/api/meta"),
    "native": ("admin", "/api/sites/{mill}/native"),
    "now": ("admin", "/api/sites/{mill}/now"),
    "outages": ("admin", "/api/sites/{mill}/outages?days=30"),
    "overrides": ("admin", "/api/sites/{mill}/overrides"),
    "overview_chart": ("admin", "/api/sites/{mill}/overview/chart?hours=24"),
    "pilot": ("admin", "/api/sites/{mill}/pilot"),
    "plan": ("admin", "/api/sites/{mill}/plan"),
    "plans": ("admin", "/api/sites/{mill}/plans?limit=50"),
    "prices": ("admin", "/api/sites/{mill}/prices"),
    "quality": ("admin", "/api/sites/{mill}/telemetry/quality?days=14"),
    "reports": ("admin", "/api/reports"),
    "savings": ("admin", "/api/sites/{mill}/savings"),
    "site": ("admin", "/api/sites/{mill}"),
    "site_offgrid": ("admin", "/api/sites/{farm}"),
    "switchback": ("admin", "/api/sites/{mill}/switchback"),
    "tariffs": ("admin", "/api/tariffs"),
    "tariffs_fuel": ("admin", "/api/tariffs/fuel"),
    "tariffs_versions": ("admin", "/api/tariffs/versions"),
    "telemetry": ("admin", "/api/sites/{mill}/telemetry?names=load_power_w,pv_power_w&res=1m"),
    "telemetry_days": ("admin", "/api/sites/{mill}/telemetry/days"),
    "users": ("admin", "/api/users"),
    "viewer_battery": ("viewer", "/api/sites/{mill}/battery"),
    "viewer_fleet": ("viewer", "/api/fleet"),
    "viewer_genset": ("viewer", "/api/sites/{farm}/genset?days=14"),
    "viewer_now": ("viewer", "/api/sites/{mill}/now"),
    "viewer_plan": ("viewer", "/api/sites/{mill}/plan"),
    "viewer_savings": ("viewer", "/api/sites/{mill}/savings"),
}
#: The per-role answers of GET /api/meta's `permissions` (the web app's `metaFor`, audit tests-and-perf-18: the role map read from the API, not hand-kept)
META_ROLES = "meta_roles"
ROLES = ("admin", "operator", "analyst", "viewer")
#: The demo company's people (seed_demo.DEMO_PEOPLE) by the role they hold
PEOPLE = {"admin": "owner@onyxleaf.demo", "operator": "operator@onyxleaf.demo", "analyst": "analyst@onyxleaf.demo", "viewer": "viewer@onyxleaf.demo"}

#: Maps whose keys are data, not field names, by the path of the map (`{}` in a path): site ids, tariff categories. Any other object whose every key is
#: data (DATA_KEY) is taken as such a map too.
DATA_MAPS = re.compile(r"(^|\.)(by_site|by_severity|calendars|cards)$")
#: Keys that are data wherever they are: ids, days, months, hours, numbers, tariff categories and the alert rules' groups
DATA_KEY = re.compile(r"^([0-9a-f]{8,32}|\d{4}-\d{2}(-\d{2})?|\d{1,2}:\d{2}|-?\d+(\.\d+)?|[A-E]-\d(\([a-z]\))?|[A-Z][a-z]+)$")


def _is_map(obj: dict, prefix: str) -> bool:
    """A map whose keys are data (DATA_MAPS by its path, or every key DATA_KEY), not an object with field names."""
    return bool(DATA_MAPS.search(prefix)) or bool(obj) and all(DATA_KEY.match(str(k)) for k in obj)


def _key(prefix: str, k, is_map: bool) -> str:
    return f"{prefix}{{}}" if is_map else (f"{prefix}.{k}" if prefix else str(k))


def paths(obj, prefix: str = "") -> set[str]:
    """Every key path of an answer: `a.b`, `a.list[].c`, `a.map{}.c`."""
    out: set[str] = set()
    if isinstance(obj, dict):
        is_map = _is_map(obj, prefix)
        for k, v in obj.items():
            here = _key(prefix, k, is_map)
            out.add(here)
            out |= paths(v, here)
    elif isinstance(obj, list):
        for v in obj:
            out |= paths(v, f"{prefix}[]")
    return out


def required(obj, prefix: str = "") -> set[str]:
    """The key paths an answer carries wherever it can: every key of an object, and under a list (or a map whose keys are data) only what every one
    of its elements carries. A field only some elements carry (an alert's detail of its kind, a step's flags) is served where the data has it."""
    if isinstance(obj, dict):
        if _is_map(obj, prefix):
            subs = [required(v, f"{prefix}{{}}") for v in obj.values()]
            return ({f"{prefix}{{}}"} | set.intersection(*subs)) if subs else set()
        out: set[str] = set()
        for k, v in obj.items():
            here = _key(prefix, k, False)
            out |= {here} | required(v, here)
        return out
    if isinstance(obj, list):
        subs = [required(v, f"{prefix}[]") for v in obj]
        return set.intersection(*subs) if subs else set()
    return set()


def _empty(obj, prefix: str = "") -> set[str]:
    """The paths of the lists and maps that are empty, or null, in an answer: what is under them cannot be compared."""
    out: set[str] = set()
    if obj is None or obj == {} or obj == []:
        if prefix:
            out.add(prefix)
    if isinstance(obj, dict):
        is_map = _is_map(obj, prefix)
        for k, v in obj.items():
            out |= _empty(v, _key(prefix, k, is_map))
    elif isinstance(obj, list):
        for v in obj:
            out |= _empty(v, f"{prefix}[]")
    return out


def _under(path: str, roots: set[str]) -> bool:
    """Whether the path lies below one of `roots` (the root itself is compared: a field served as null or [] is still a field)."""
    return any(path.startswith(r + ".") or path.startswith(r + "[]") or path.startswith(r + "{}") for r in roots)


def _name(path: str) -> str:
    """A key path's last field name ('' for a map's entry or a list's element)."""
    last = re.split(r"\.|\[\]|\{\}", path)[-1]
    return last


def backend_names() -> set[str]:
    """Every name the backend's code writes as a string or a keyword ("x", 'x', x=): a field a fixture holds and the live answer lacks is still one the
    API may serve (a site with an edge, a pilot past its baseline) while its name is in the code; one whose name is nowhere in it is no longer served."""
    src = Path(__file__).resolve().parents[1] / "gridlytics"
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(src.glob("*.py")))
    return set(re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", text)) | set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)=", text))


def differences(fixture, live, names: set[str] | None = None, variants: list | None = None) -> tuple[set[str], set[str]]:
    """(served and missing from the fixture, in the fixture and no longer served): key paths.

    Served and missing: a path the live answer carries wherever it can (`required`), and so does the same route's answer for every other demo site
    (`variants`), that the fixture lacks; a field served only where the data has it (one site's, one alert kind's) is not asked of the fixture. No
    longer served: a path the fixture holds, the live answer lacks, and whose name the backend's code no longer has (`backend_names`). What lies under a
    list or a map that is empty or null on either side is not compared (nothing there to compare)."""
    names = backend_names() if names is None else names
    always = required(live)
    for v in variants or ():
        always &= required(v)
    f, s = paths(fixture), paths(live)
    blind = _empty(fixture) | _empty(live)
    return ({p for p in always - f if not _under(p, blind)},
            {p for p in f - s if not _under(p, blind) and _name(p) and _name(p) not in names})


#: When the fixtures were captured (6 November 2026, 19:45 in Pakistan: the first step of the mill's plan): a value added to them is moved by the time
#: between this and the live answer's own capture, so the days and moments it names line up with the fixture's
CAPTURED_AT = 1_793_976_300
#: Keys that hold a moment in epoch seconds (or a list of them), and the keys an answer's own moment is read from, where both answers have one, to line
#: an added series up with the fixture's exactly (a forecast's belief time, a chart's now)
TIME_KEYS = re.compile(r"^(t|ts|from|to|start|end|at|belief_time|last_seen|last_data|first_data|now|until|[a-z_]+_ts|[a-z_]+_at)$")
REF_KEYS = ("belief_time", "now")
_EPOCH = (1_600_000_000, 2_100_000_000)
_PKT = timezone(timedelta(hours=5))
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MOMENT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(\+00:00|Z|\+05:00)?$")


def _ref(obj: dict):
    for k in REF_KEYS:
        v = obj.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and _EPOCH[0] <= v <= _EPOCH[1]:
            return v
    return None


def _shifted(value, shift: int, key: str = ""):
    """The live value moved by `shift` seconds: every epoch moment under a time key, every ISO moment, and every day (a Pakistan day, moved as its
    midday is), so a value added to a fixture captured on another day names the fixture's days."""
    if not shift:
        return value
    if isinstance(value, dict):
        return {k: _shifted(v, shift, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_shifted(v, shift, key) for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and TIME_KEYS.match(key) and _EPOCH[0] <= value <= _EPOCH[1]:
        return value + shift
    if isinstance(value, str) and _DAY.match(value):
        midday = datetime.fromisoformat(value).replace(hour=12, tzinfo=_PKT)
        return (midday + timedelta(seconds=shift)).date().isoformat()
    if isinstance(value, str) and _MOMENT.match(value):
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (at + timedelta(seconds=shift)).isoformat(timespec="seconds") if at.tzinfo else value
    return value


def merge(fixture, live, names: set[str], prefix: str = "", shift: int = 0):
    """The fixture with the live answer's shape: keys the API serves and the fixture lacks are added with their live value, keys whose names the backend
    no longer has are removed, and every value the fixture holds is kept. A list's elements, and a data map's entries, take their new keys from the live
    element at the same place (or the last), and from the live map's first entry; an empty or null side leaves the fixture's value as it is. An added
    value's moments and days are moved by `shift` (the time from the live capture to the fixtures', CAPTURED_AT), or, inside an object where both
    answers carry their own moment (`REF_KEYS`), by the difference between those, so a series added to a forecast lines up with its issue."""
    if isinstance(fixture, dict) and isinstance(live, dict) and fixture and live:
        a, b = _ref(fixture), _ref(live)
        shift = a - b if a is not None and b is not None else shift
        if _is_map(fixture, prefix) or _is_map(live, prefix):
            first = next(iter(live.values()))
            return {k: merge(v, live.get(k, first), names, _key(prefix, k, True), shift) for k, v in fixture.items()}
        out = {k: (merge(v, live[k], names, _key(prefix, k, False), shift) if k in live else v) for k, v in fixture.items() if k in live or k in names}
        out.update({k: _shifted(v, shift, k) for k, v in live.items() if k not in fixture})
        return out
    if isinstance(fixture, list) and isinstance(live, list) and live:
        return [merge(v, live[min(i, len(live) - 1)], names, f"{prefix}[]", shift) for i, v in enumerate(fixture)]
    return fixture


@contextmanager
def seeded(folder: Path, days: int = 4, id_seed: int = 9):
    """A scratch data folder seeded as the browser check seeds one (the demo companies, their accounts and roles, the four twin sites with `days` days of
    history, re-planned once a day to keep it quick), and a client with each role's sign-in. Yields (client, headers by role, site ids by key)."""
    import random

    old = {k: os.environ.get(k) for k in ("GRIDLYTICS_DATA_DIR", "GRIDLYTICS_DEMO_REPLAN_HOURS")}
    os.environ["GRIDLYTICS_DATA_DIR"] = str(folder)
    os.environ["GRIDLYTICS_DEMO_REPLAN_HOURS"] = "24"
    from gridlytics import store
    new_id = store.new_id
    try:
        from fastapi.testclient import TestClient

        from gridlytics import api, seed_demo
        seed_demo.seed(out=folder / "ACCOUNTS.md")
        # the demo's ids from a seeded generator: the twin seeds a site's days by its id, so the same days (and the same fields, where a field is
        # served only where the data has it) on every run of the same day
        rng = random.Random(id_seed)
        store.new_id = lambda n=12: f"{rng.getrandbits(128):032x}"[:n]
        seed_demo.load_demo_sites(days)
        store.new_id = new_id
        text = (folder / "ACCOUNTS.md").read_text(encoding="utf-8")

        def password(email):
            for line in text.splitlines():
                cells = [c.strip().strip("`") for c in line.split("|")]
                if email in cells:
                    return cells[cells.index(email) + 1]

        client = TestClient(api.app, base_url="http://127.0.0.1", raise_server_exceptions=False)
        headers = {}
        for role, email in PEOPLE.items():
            r = client.post("/api/local/session", json={"email": email, "password": password(email)})
            assert r.status_code == 200, r.text
            headers[role] = {"Authorization": "Bearer " + r.json()["access_token"]}
        fleet = client.get("/api/fleet", headers=headers["admin"]).json()["sites"]
        ids = {key: next(s["id"] for s in fleet if s["name"] == name) for key, name in SITES.items()}
        # what the fixtures were captured with beyond the seed: an API key, a report, an override, a fuel fill
        mill = ids["mill"]
        assert client.post("/api/platform/keys", headers=headers["admin"],
                           json={"name": "fixtures", "scopes": ["sites:read", "plans:read"], "site_id": mill, "kind": "partner"}).status_code == 200
        # a monthly savings report, which the server makes only for a site with a week of data (R8 2.14; round 7, savings-page): the seed's few days
        # are let through here, as the fixtures were captured with one
        from gridlytics import reports
        week, reports.REPORT_DAYS_NEEDED = reports.REPORT_DAYS_NEEDED, 0
        try:
            assert client.post("/api/reports", headers=headers["admin"], json={"kind": "monthly_savings", "site_id": mill}).status_code == 200
        finally:
            reports.REPORT_DAYS_NEEDED = week
        until = datetime.now(timezone.utc) + timedelta(hours=4)
        assert client.post(f"/api/sites/{mill}/overrides", headers=headers["admin"],
                           json={"kind": "reserve", "value": 40, "until": until.isoformat(timespec="seconds"), "reason": "Operator hold"}).status_code == 200
        yield client, headers, ids
    finally:
        store.new_id = new_id
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def capture(client, headers: dict, ids: dict, variants: dict | None = None) -> dict:
    """Each fixture's live answer (FIXTURES), and META_ROLES: each role's permissions from GET /api/meta. Given a dict as `variants`, it is filled with
    each site route's answers for the other demo sites (those that answer it), for `differences`."""
    out = {}
    for name, (reader, route) in FIXTURES.items():
        r = client.get(route.format(**ids), headers=headers[reader])
        assert r.status_code == 200, f"{name}: {route} answered {r.status_code} to the {reader}: {r.text[:300]}"
        out[name] = r.json()
        site = next((k for k in SITES if "{" + k + "}" in route), None)
        if variants is not None and site:
            others = [client.get(route.format(**{**ids, site: ids[other]}), headers=headers[reader]) for other in SITES if other != site]
            variants[name] = [x.json() for x in others if x.status_code == 200]
    out[META_ROLES] = {role: client.get("/api/meta", headers=headers[role]).json()["permissions"] for role in ROLES}
    return out


def scratch_env(environ, scratch: Path) -> None:
    """The settings a re-capture runs under, whatever the shell holds: its own database in `scratch` (never the owner's data/), local sign-in, this
    machine's host, no copy before a reset; the data folder is the one `seeded` makes. Set, not defaulted: a GRIDLYTICS_DB or GRIDLYTICS_DATA_DIR the
    shell already names (the launcher's, the owner's) would otherwise take the seed."""
    environ["GRIDLYTICS_DB"] = str(scratch / "scratch.db")
    environ["GRIDLYTICS_AUTH_MODE"] = "local"
    environ["GRIDLYTICS_HOSTS"] = "127.0.0.1"
    environ["GRIDLYTICS_RESET_BACKUP"] = "0"
    environ.pop("GRIDLYTICS_DATA_DIR", None)


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Compare the web app's fixtures with a scratch backend's answers; --write re-captures their shape.")
    ap.add_argument("--write", action="store_true", help="add the served keys the fixtures lack, remove the ones no longer served, keep every value")
    ap.add_argument("--days", type=int, default=4, help="days of demo history (default 4)")
    args = ap.parse_args(argv)
    here = Path(__file__).resolve().parent
    sys.path[:0] = [str(here.parent), str(here)]
    scratch_env(os.environ, Path(tempfile.mkdtemp(prefix="gridlytics-fixtures-")))
    import sibling
    folder = Path(tempfile.mkdtemp(prefix="gridlytics-fixtures-"))
    root = sibling.frontend_file("frontend/src/test/fixtures")
    changed = 0
    names = backend_names()
    variants: dict = {}
    with seeded(folder, args.days) as (client, headers, ids):
        live = capture(client, headers, ids, variants)
        captured = int(datetime.now(timezone.utc).timestamp()) // 900 * 900
    for name, answer in live.items():
        path = root / f"{name}.json"
        fixture = json.loads(path.read_text(encoding="utf-8")) if path.exists() else answer
        served, stale = differences(fixture, answer, names, variants.get(name)) if path.exists() else (set(), set())
        if served or stale or not path.exists():
            changed += 1
            print(f"{name}: served and missing {sorted(served)}; no longer served {sorted(stale)}" if path.exists() else f"{name}: new")
            if args.write:
                path.write_text(json.dumps(merge(fixture, answer, names, shift=CAPTURED_AT - captured) if name != META_ROLES else answer, separators=(",", ":"), ensure_ascii=False),
                                encoding="utf-8", newline="\n")
    print(f"{changed} fixture(s) differ from the API's answers" + (" (re-captured)" if args.write and changed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
