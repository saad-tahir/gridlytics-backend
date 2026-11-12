"""Company isolation on the EMS routes and in the data folder (round 7, audit tests-and-perf-14).

Each company works in its own database file, chosen per request by the guard (a ContextVar: store.set_company), and keeps its sites' signing keys and
its report files in folders of its own (keys/<its file's stem>, reports/<its file's stem>). Until round 7 the only two-company test was a lifted one,
skipped; nothing held a member of one company away from another's sites, telemetry, plans or keys. Here two companies, Alpha and Beta, each with a site,
an owner (an administrator: the role that reaches the most) and an edge key, and:

- every route that names a site answers Alpha's owner 404 for Beta's site, a change to it changes nothing in Beta's file, and no collection Alpha reads
  names Beta's site; the same walk on Alpha's own site answers, so the 404s are the company boundary's, not the routes';
- Alpha's edge key is refused on Beta's site paths (wrong_site) and its telemetry never lands in Beta's file;
- each company's signing keys and report files are made in its own folders only, and their records are in its own file only;
- requests of both companies at once each see their own company only (the ContextVar does not leak between threads).

Every test works in a data folder of its own (GRIDLYTICS_DATA_DIR): never the owner's."""
from __future__ import annotations

import re
import sqlite3
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import identities
from gridlytics import api, sites, store
from gridlytics import platform as platformdb

#: Values for the other path parameters of a site's routes: made up, so a route that looks them up finds nothing in either company
PARAMS = {"kind": "pv", "plan_id": "0f0f0f0f0f0f", "role": "battery", "bill_id": "0f0f0f0f0f0f", "conn_id": "0f0f0f0f0f0f", "oid": "1",
          "action": "resume", "nid": "0f0f0f0f0f0f", "start": "1793976300"}
#: GET routes that answer 404 on a company's own site in this world too: they look up a made-up id
OWN_404 = {"/api/sites/{site_id}/plans/{plan_id}"}
#: GET routes a person is refused on any site (403, the same on their own): the signed plan message is pulled by the site's edge with its key
PERSON_REFUSED = {"/api/sites/{site_id}/plans/current/message"}


def _site(c: TestClient, name: str, disco: str) -> str:
    r = c.post("/api/sites", json={"name": name, "disco": disco, "genset_start_control": "manual", "critical_load_kw": [20.0] * 24})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    w = types.SimpleNamespace(root=tmp_path)
    for key, name, disco in (("a", "Alpha Energy", "FESCO"), ("b", "Beta Energy", "LESCO")):
        co = identities.company(name, own_file=True)
        c = identities.client(f"owner@{key}.example.test", None, co, "owner", name=f"{name} Owner", raise_server_exceptions=False)
        site = _site(c, f"{name} secret mill", disco)
        with store.use_company(platformdb.company_file(co)):
            sites.put_connection(site, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "test")
            sites.put_asset(site, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "test")
        r = c.post("/api/platform/keys", json={"name": f"{key} edge", "scopes": ["telemetry:write", "sites:read", "plans:read"], "site_id": site,
                                               "kind": "edge"})
        assert r.status_code == 200, r.text
        setattr(w, key, types.SimpleNamespace(company=co, c=c, site=site, name=f"{name} secret mill", file=platformdb.company_file(co),
                                              key={"Authorization": "Bearer " + r.json()["token"]}))
    w.anon = TestClient(api.app, raise_server_exceptions=False)
    return w


def _routes() -> list[tuple[str, str]]:
    return sorted({(m, r.path) for mod in api.FEATURE_ROUTES for r in mod.router.routes if isinstance(r, APIRoute)
                   for m in r.methods - {"HEAD", "OPTIONS"} if "{site_id}" in r.path})


def _url(path: str, site: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: site if m.group(1) == "site_id" else PARAMS[m.group(1)], path)


def _snapshot(path: Path) -> dict:
    """Every table's rows of a company's file and of its telemetry file beside it (store.telemetry_path), read straight from the files (never through
    the store, whose context could pick another file); a file not made yet reads as None."""
    out = {}
    for f in (path, store.telemetry_path(path)):
        if not f.exists():
            out[f.name] = None
            continue
        with closing(sqlite3.connect(f)) as con:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            out[f.name] = {t: con.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
    return out


def _minutes(path: Path) -> list[str]:
    """The sites whose telemetry minutes a company's telemetry file holds."""
    f = store.telemetry_path(path)
    if not f.exists():
        return []
    with closing(sqlite3.connect(f)) as con:
        return [r[0] for r in con.execute("SELECT DISTINCT site_id FROM minute")]


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} if root.exists() else set()


# ------------------------------------------------------------------------------------------------ the routes

def test_a_member_of_one_company_gets_404_for_every_route_of_another_companys_site(world):
    a, b = world.a, world.b
    gets = [p for m, p in _routes() if m == "GET"]
    assert len(gets) >= 35                                                            # the walk sees the site routes (37 in round 7)
    for path in gets:
        r = a.c.get(_url(path, b.site))
        assert r.status_code == (403 if path in PERSON_REFUSED else 404), (path, r.status_code, r.text[:200])
        assert b.name not in r.text, path
        if path in PERSON_REFUSED:
            assert a.c.get(_url(path, a.site)).status_code == 403
        elif path not in OWN_404:
            own = a.c.get(_url(path, a.site))
            assert own.status_code == 200, ("the walk must reach the route on the company's own site", path, own.status_code, own.text[:200])


def test_a_change_to_another_companys_site_changes_nothing_in_its_file(world):
    a, b = world.a, world.b
    before = _snapshot(b.file)
    changes = [(m, p) for m, p in _routes() if m != "GET" and not p.startswith("/api/edge/")]
    assert len(changes) >= 30
    for method, path in changes:
        body = {"name": "Taken over", "disco": "FESCO"} if path == "/api/sites/{site_id}" and method == "PUT" else {}
        r = a.c.request(method, _url(path, b.site), json=body if method != "DELETE" else None)
        # refused: the site is not in the person's company (404); a body the route refuses first (422) did nothing either
        assert r.status_code in (404, 422), (method, path, r.status_code, r.text[:200])
        assert b.name not in r.text, (method, path)
    r = a.c.post(f"/api/sites/{b.site}/telemetry/upload", content=b"ts,load_power_w\n2026-11-06 10:00,1000\n", headers={"content-type": "text/csv"})
    assert r.status_code == 404
    assert _snapshot(b.file) == before
    # and Beta's site is still Beta's, as its owner sees it
    assert b.c.get(f"/api/sites/{b.site}").json()["name"] == b.name


def test_no_collection_a_member_reads_names_another_companys_site(world):
    a, b = world.a, world.b
    for path in ("/api/sites", "/api/fleet", "/api/fleet/savings", "/api/alerts", "/api/alerts/counts", "/api/reports", "/api/audit", "/api/users",
                 "/api/platform/keys", f"/api/reports?site_id={b.site}"):
        r = a.c.get(path)
        assert r.status_code == 200, (path, r.text[:200])
        assert b.site not in r.text and b.name not in r.text, path
    assert [s["id"] for s in a.c.get("/api/sites").json()] == [a.site]
    assert [s["id"] for s in b.c.get("/api/sites").json()] == [b.site]


def test_a_companys_edge_key_is_refused_on_another_companys_site_and_its_telemetry_never_lands_there(world):
    a, b = world.a, world.b
    now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60 - 120
    batch = {"contract": "ems-edge/1.0", "seq": 1, "sent_at": "2026-11-06T10:00:00Z", "columns": ["ts", "grid_present", "pv_power_w"],
             "rows": [[now, 1, 900]]}
    before = _snapshot(b.file)
    for method, path in [(m, p) for m, p in _routes() if p.startswith("/api/edge/")] + [("GET", "/api/sites/{site_id}"),
                                                                                         ("GET", "/api/sites/{site_id}/plan")]:
        r = world.anon.request(method, _url(path, b.site), headers=a.key, json=batch if method == "POST" else None)
        assert r.status_code == 403 and r.json()["code"] == "wrong_site", (method, path, r.status_code, r.text[:200])
    assert _snapshot(b.file) == before
    # on its own site the same batch is taken, into its own company's file
    r = world.anon.post(f"/api/edge/{a.site}/telemetry", headers=a.key, json=batch)
    assert r.status_code == 200, r.text
    assert _minutes(a.file) == [a.site] and _minutes(b.file) == []


# ------------------------------------------------------------------------------------------------ the data folder: keys and reports

def test_each_companys_signing_keys_and_report_files_stay_in_its_own_folders(world):
    a, b = world.a, world.b
    keys = world.root / "keys"
    a_dir, b_dir = keys / a.file.stem, keys / b.file.stem
    assert a_dir != b_dir
    # the edge card makes the site's signing key, in its company's keys folder
    assert a.c.get(f"/api/sites/{a.site}/edge").json()["public_key"]["public_key"]
    assert _files(keys) and all(f.startswith(f"{a.file.stem}/{a.site}-") for f in _files(keys)), _files(keys)
    # Alpha's owner asking for Beta's site's card or a new key for it makes nothing anywhere
    assert a.c.get(f"/api/sites/{b.site}/edge").status_code == 404
    assert a.c.post(f"/api/sites/{b.site}/edge/key", json={"reason": "Taken over"}).status_code == 404
    assert not b_dir.exists() and len(_files(keys)) == 1
    assert b.c.get(f"/api/sites/{b.site}/edge").json()["public_key"]["public_key"]
    assert {f.split("/")[0] for f in _files(keys)} == {a.file.stem, b.file.stem}
    assert [f.split("/")[1].split("-")[0] for f in sorted(_files(b_dir.parent)) if f.startswith(f"{b.file.stem}/")] == [b.site]
    # a key file of one company is not found from the other's: its record is in its own company's file only
    with closing(sqlite3.connect(b.file)) as con:
        assert [r[0] for r in con.execute("SELECT DISTINCT site_id FROM site_keys")] == [b.site]
    with closing(sqlite3.connect(a.file)) as con:
        assert [r[0] for r in con.execute("SELECT DISTINCT site_id FROM site_keys")] == [a.site]
    # a report of Alpha's: in Alpha's reports folder, listed and downloaded by Alpha only
    r = a.c.post("/api/reports", json={"kind": "energy_monthly", "site_id": a.site})
    if r.status_code != 200:
        r = a.c.post("/api/reports", json={"kind": "monthly_savings", "site_id": a.site})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    reports = world.root / "reports"
    assert _files(reports) and all(f.startswith(f"{a.file.stem}/") for f in _files(reports)), _files(reports)
    assert rid not in b.c.get("/api/reports").text
    assert b.c.get(f"/api/reports/{rid}/csv").status_code == 404
    assert a.c.get(f"/api/reports/{rid}/csv").status_code == 200


# ------------------------------------------------------------------------------------------------ the company of each request

def test_requests_of_two_companies_at_once_each_see_their_own_company(world):
    a, b = world.a, world.b

    def ask(i: int) -> tuple[str, list[str], str]:
        who = a if i % 2 else b
        r = who.c.get("/api/sites") if i % 3 else who.c.get(f"/api/sites/{who.site}/plan")
        assert r.status_code == 200, r.text[:200]
        seen = [s["id"] for s in r.json()] if isinstance(r.json(), list) else [r.json()["plan"]["site_id"]] if r.json().get("plan") else [who.site]
        return who.site, seen, r.text

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(ask, range(48)))
    for mine, seen, text in answers:
        other = b if mine == a.site else a
        assert seen == [mine] and other.site not in text and other.name not in text
