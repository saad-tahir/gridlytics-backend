"""Round-7 regressions for the demo seed and its story (audit package demo-seed; plan Part 9.6, 2.3, 7.6 and PLT-05): every demo site records the data
source its tier rests on, and the closed-loop mill's edge is the twin, reporting as it runs; an advisory site's inverter runs one table a day, the one
the demo's installer keys and marks, and only a marked day is confirmed; the off-grid farm says it is off grid; an outage and the genset run covering it
go on past midnight; one company's demo build never blocks another's; the demo tab names the days a build makes and only an administrator loads it;
the account seed names only templates the repository holds, and a second run keeps what the first made. Each test fails on the code before its fix.
Test data: the demo's own site specs, the twin's generated days, and plans written straight to the table; nothing reads the network, and every test
works in a data folder of its own."""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from test_fix_api import world  # noqa: F401 (a fixture: a company of its own with a site and a client per role)
from test_fix_savings import WEEKDAY, _insert_plan, _mark, _slots_plan, company, make_site, twin_day  # noqa: F401 (company is a fixture)

from gridlytics import alerts, demo, plans, routes_demo, seed_demo, sites, store, telemetry, twin, verify
from gridlytics import platform as platformdb

PKT = timezone(timedelta(hours=5))


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


def _demo(key: str) -> str:
    """The demo site of a DEMO_SITES key (textile, mall, feedmill, farm), made as a load makes it."""
    name = next(s for s in demo.DEMO_SITES if s["key"] == key)["site"]["name"]
    return next(i for i in demo.create_sites("t") if sites.full(i)["name"] == name)


def _plan(sid: str, made: int, mode: str, days: int = 3, status: str = "active") -> str:
    """A plan made at `made` (its belief time) whose table runs one mode from then on, for the local days it covers, as compile_mode_a stores it."""
    pid = store.new_id()
    first = datetime.fromtimestamp(made, PKT).date()
    slot = {"start_ts": made, "end_ts": made + days * 86400, "mode": mode, "soc_floor_pct": 20.0, "soc_target_pct": 100.0,
            "grid_charge": mode == "charge_grid", "max_discharge_w": None, "max_charge_w": None, "genset": None}
    table = {"days": [{"date": (first + timedelta(days=k)).isoformat(), "slots": [slot] if k == 0 else []} for k in range(days)]}
    with store.connect() as con:
        if status == "active":
            con.execute("UPDATE plans SET status = 'superseded' WHERE site_id = ? AND status = 'active'", (sid,))
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (pid, sid, _iso(made), _iso(made), _iso(made), _iso(made + days * 86400), "a", status, "[]",
                                                     json.dumps(table)))
    return pid


def _marks(sid: str) -> list[tuple]:
    with store.connect() as con:
        return [tuple(r) for r in con.execute("SELECT day_local, plan_id, by, method FROM applied WHERE site_id = ? ORDER BY day_local", (sid,))]


# ------------------------------------------------------------------------------------------------ the farm is off grid (engine-plans-live-20)

def test_the_demo_farm_records_that_it_is_off_grid(company):
    ids = demo.create_sites("t")
    farm = next(sites.full(i) for i in ids if "farm" in sites.full(i)["name"].lower())
    assert farm["connections"] == [] and farm["data"]["off_grid"] is True        # before: False, at a site with no grid connection
    assert all(sites.full(i)["data"]["off_grid"] is False for i in ids if i != farm["id"])


# ------------------------------------------------------------------------------------------------ the data source and the edge (role-owner-15)

def test_every_demo_site_records_the_data_source_its_tier_rests_on(company):
    ids = demo.create_sites("t")
    for sid in ids:
        s = sites.full(sid)
        src = s["data"].get("data_source")
        # before: no source at all ('No data source is recorded for this site yet') beside 'The data source supports Closed loop'
        assert src and src["kind"] in sites.DATA_SOURCE_KINDS and src["grid_signal_confirmed"] is True, s["name"]
        with store.connect() as con:
            cap = con.execute("SELECT body FROM capabilities WHERE site_id = ?", (sid,)).fetchone()
        native = sites.native_latest(sid)
        if src["kind"] == "edge":
            # the edge asserts its own tier in its capabilities message (plan Part 2.3), and reads the native settings back
            assert cap is not None and json.loads(cap["body"])["tier"] == s["tier_capable"] == "closed_loop", s["name"]
            assert native["source"] == "read_back"
        else:
            # a cloud source has no write path: its plans reach the site as tables a person keys in (advisory writes at most)
            assert cap is None and s["tier_capable"] == "advisory_writes", s["name"]
            assert native["source"] != "read_back", s["name"]                      # before: the mall's 'Read back' with no edge to read it
    tiers = {sites.full(i)["name"]: sites.full(i)["tier"] for i in ids}
    assert tiers == {"Faisalabad textile mill (demo)": "advisory_writes", "Lahore mall (demo)": "monitor", "Karachi feed mill (demo)": "closed_loop",
                     "Rahim Yar Khan farm (demo)": "advisory_writes"}


def test_the_closed_loop_demo_sites_edge_is_the_twin_and_reports_as_it_runs(world, monkeypatch):
    from gridlytics import routes_edge

    now = int(time.time()) // 60 * 60
    with world.use():
        mill = _demo("feedmill")
        plan = {"id": "plan-edge", "valid_until": _iso(now + 86400), "slots": {"days": []}, "steps": []}
        demo._simulate_span(sites.full(mill), now - 1800, now, plan)
        demo._simulate_span(sites.full(mill), now - 1800, now, plan)                # the same plan again: acknowledged once
        st = sites.state(mill)
        with store.connect() as con:
            acks = [tuple(r) for r in con.execute("SELECT plan_id, status FROM acks WHERE site_id = ?", (mill,))]
        farm = _demo("farm")
    assert st["edge_last_seen"] == now and st["edge_heartbeat"]["control_state"] == "following_plan"
    assert st["edge_heartbeat"]["active_plan"] == "plan-edge" and acks == [("plan-edge", "applied")]
    e = world.c["operator"].get(f"/api/sites/{mill}/edge").json()
    # before: last_seen null ('No edge device: plans are delivered as daily slot tables') at a closed-loop site
    assert e["last_seen"] == now and e["online"] is True and e["capabilities"]["tier"] == "closed_loop"
    assert [a["plan_id"] for a in e["acks"]] == ["plan-edge"] and e["heartbeat"]["control_state"] == "following_plan"
    # the twin reports when the 15-minute schedule runs it, so ten minutes on its edge is still online
    monkeypatch.setattr(routes_edge, "now_ts", lambda: now + 600)
    assert world.c["operator"].get(f"/api/sites/{mill}/edge").json()["online"] is True
    monkeypatch.setattr(routes_edge, "now_ts", lambda: now + 3600)
    assert world.c["operator"].get(f"/api/sites/{mill}/edge").json()["online"] is False
    # a site with a cloud source has no edge, and the twin reports none for it
    assert world.c["operator"].get(f"/api/sites/{farm}/edge").json()["last_seen"] is None
    # removing the demo removes its edge's records with it
    with world.use():
        demo.remove("t")
        with store.connect() as con:
            assert [con.execute(f"SELECT count(*) FROM {t} WHERE site_id = ?", (mill,)).fetchone()[0] for t in ("acks", "capabilities")] == [0, 0]


# ------------------------------------------------------------------------------------------------ one keyed table a day (engine-delivery-18)

def test_an_advisory_demo_site_runs_the_table_keyed_for_the_day_not_every_new_plan(company):
    farm = _demo("farm")
    day = datetime.now(PKT).date()
    d0 = twin.day_start_utc(day)
    nightly = _plan(farm, d0 - 900, "discharge", status="superseded")      # made at 23:45 the night before: the table in force at the day's start
    later = _plan(farm, d0 + 3 * 3600, "charge_grid")                     # a re-plan at 03:00, the current plan
    site = sites.full(farm)
    ts = np.arange(d0 + 4 * 3600, d0 + 5 * 3600, 60, dtype=np.int64)
    _, modes = demo._policy_for(site, day, ts, plans.get(farm, later))
    assert (modes == twin.MODE_CODE["discharge"]).all()                    # before: the 03:00 plan's grid charge, as if keyed in at once
    # a person marks another table applied for the day: the inverter runs that one
    plans.mark_applied(farm, later, day.isoformat(), "t", "installer_keyed")
    _, modes = demo._policy_for(site, day, ts, plans.get(farm, later))
    assert (modes == twin.MODE_CODE["charge_grid"]).all()
    assert nightly != later
    # an advisory demo site whose installer keys no table (not one of the demo's own) runs its native settings, whatever plan is current
    other = make_site(name="Demo advisory r7", tier="advisory_writes", demo_link=demo.DEMO_LINK)
    _plan(other, d0 - 900, "discharge")
    native = twin.native_policy(sites.full(other), ts)["mode"]
    _, modes = demo._policy_for(sites.full(other), day, ts, plans.current(other))
    assert (modes == native).all()


def test_the_demo_installer_marks_one_table_a_day_and_the_mill_marks_it_the_next_morning(company):
    farm, mill = _demo("farm"), _demo("textile")
    today = datetime.now(PKT).date()
    d0 = twin.day_start_utc(today)
    ids = {}
    for sid in (farm, mill):
        ids[sid] = (_plan(sid, d0 - 86400 - 900, "self_use", status="superseded"), _plan(sid, d0 - 900, "discharge", status="superseded"))
        _plan(sid, d0 + 900, "charge_grid")                                  # today's later plans are not keyed in
        demo.advance(sid)
        demo.advance(sid)                                                    # a second run marks nothing twice
    yesterday = (today - timedelta(days=1)).isoformat()
    # the farm's installer keys the nightly table and marks it the same day: one table a day, within the write budget
    assert _marks(farm) == [(today.isoformat(), ids[farm][1], demo.INSTALLER, "vendor_app")]
    # the Faisalabad mill's marks come the next morning: yesterday's is in, today's is not yet (its alert says so)
    assert _marks(mill) == [(yesterday, ids[mill][0], demo.INSTALLER, "vendor_app")]
    written = telemetry.raw(mill, d0, d0 + 86400, ("inverter_mode_active",))["inverter_mode_active"]
    assert len(written) and set(written) == {"discharge"}                  # the table keyed for today, before anyone marks it


def test_the_demo_history_keys_the_nightly_table_once_a_day_and_runs_it(company, monkeypatch):
    """build_history with the solver replaced by plans written straight to the table: each 00:00 plan discharges all day, every later re-plan
    charges from the grid. Before: the twin ran every 3-hourly plan at once and no day carried a mark."""
    from gridlytics import alerts as alerts_mod, forecast

    made = []

    def tick(sid, at=None, steps=192, **k):
        at = int(at if at is not None else time.time()) // 900 * 900
        midnight = at == twin.day_start_utc(datetime.fromtimestamp(at, PKT).date())
        pid = _plan(sid, at, "discharge" if midnight else "charge_grid")
        made.append((at, pid))
        return {"plan_id": pid, "status": "active"}

    monkeypatch.setattr(plans, "tick", tick)
    monkeypatch.setattr(verify, "compute_day", lambda *a, **k: None)
    monkeypatch.setattr(forecast, "score_day", lambda *a, **k: None)
    monkeypatch.setattr(alerts_mod, "evaluate", lambda *a, **k: None)
    farm, mill = _demo("farm"), _demo("textile")
    days = demo.OBSERVE_DAYS + 2
    today = datetime.now(PKT).date()
    plan_days = [today - timedelta(days=k) for k in (2, 1)]
    for sid in (farm, mill):
        made.clear()
        demo.build_history(sid, days)
        nightly = {datetime.fromtimestamp(at, PKT).date(): pid for at, pid in made if at == twin.day_start_utc(datetime.fromtimestamp(at, PKT).date())}
        marked = [d for d, *_ in _marks(sid)]
        if sid == farm:
            assert marked == [d.isoformat() for d in plan_days + [today]]
        else:
            assert marked == [d.isoformat() for d in plan_days]              # today's mark comes tomorrow morning
        assert all(pid == nightly[date.fromisoformat(d)] and by == demo.INSTALLER for d, pid, by, _ in _marks(sid))
        for d in plan_days:                                                  # 05:00 to 06:00, after the 03:00 re-plan: still the nightly table
            t = twin.day_start_utc(d) + 5 * 3600
            modes = telemetry.raw(sid, t, t + 3600, ("inverter_mode_active",))["inverter_mode_active"]
            assert set(modes) == {"discharge"}, (sid, d, set(modes))
        first = twin.day_start_utc(today - timedelta(days=days))             # the observation days run the native settings, unmarked
        assert set(telemetry.raw(sid, first, first + 3600, ("inverter_mode_active",))["inverter_mode_active"]) == {"self_use"}


def test_an_advisory_demo_day_is_confirmed_only_from_its_mark_and_telemetry(company):
    prev, day = WEEKDAY - timedelta(days=1), WEEKDAY
    sid = make_site(name="Demo advisory day", tier="advisory_writes", demo_link=demo.DEMO_LINK,
                    native={"source": "entered", "work_mode": "backup", "backup_soc_pct": 90})
    _, sp, _ = twin_day(sid, prev, 0.9, seed=1)
    twin_day(sid, day, float(sp["soc"][-1]), seed=2)
    r = verify.compute_day(sid, day, with_bound=False)["native"]
    assert r["fidelity"] == "not_applied" and r["claimed_rs"] == 0.0        # before: 'confirmed' at any demo site, with no table marked
    # the table marked and followed: confirmed from telemetry, as at any advisory site
    slots = _slots_plan(day)
    site = sites.full(sid)
    tr = twin.truth(site, day)
    followed = twin.simulate(tr, twin.slots_policy(slots, tr["ts"]), twin.plant_of(site), float(sp["soc"][-1]))
    _mark(sid, _insert_plan(sid, day, slots, followed), day)
    telemetry.delete_site(sid)
    twin_day(sid, prev, 0.9, seed=1)
    twin_day(sid, day, float(sp["soc"][-1]), seed=2, policy=lambda s, ts: twin.slots_policy(slots, ts))
    r = verify.compute_day(sid, day, with_bound=False)["native"]
    assert r["fidelity"] == "confirmed" and r["claimed_rs"] == r["saving_rs"]


def test_the_mills_unconfirmed_apply_alert_clears_once_todays_table_is_marked(company):
    mill = _demo("textile")
    now = int(time.time())
    telemetry.write_rows([(mill, now // 60 * 60 - 8 * 86400, "twin", 1.0, 1000.0)], ("site_id", "ts", "source_id", "grid_present", "load_power_w"))
    pid = _plan(mill, now // 900 * 900 - 900, "discharge")
    alerts.evaluate(mill, now)
    assert [a["rule"] for a in alerts.listing("open", mill) if a["rule"] == "apply_unconfirmed"] == ["apply_unconfirmed"]
    plans.mark_applied(mill, pid, datetime.now(PKT).date().isoformat(), "t", "installer_keyed")
    alerts.evaluate(mill, now)
    assert not [a for a in alerts.listing("open", mill) if a["rule"] == "apply_unconfirmed"]  # before: raised for the demo mill whatever was marked


# ------------------------------------------------------------------------------------------------ outages run past midnight (role-operator-29)

def test_an_outage_running_at_midnight_goes_on_into_the_next_day(company):
    sid = make_site(name="Night trips", twin_extra={"windows": [], "trips_per_day": 40.0, "trip_minutes": 300.0})
    site = sites.full(sid)
    days = [WEEKDAY + timedelta(days=k) for k in range(8)]
    grids = [twin.truth(site, d)["grid"] for d in days]
    # every hour trips for hours at this feeder, so each night's outage is still on at 23:59 and must still be on at 00:00
    assert all(g[-1] == 0 for g in grids[:-1])
    assert all(g[0] == 0 for g in grids[1:]), [int(g[0]) for g in grids[1:]]  # before: every outage ended at 00:00 sharp
    assert all((twin.truth(site, d)["grid"] == g).all() for d, g in zip(days, grids))  # still the same day for the same (site, day)
    # a quiet feeder's day is unchanged by the carry: nothing ran past the night before
    quiet = make_site(name="No trips", twin_extra={"windows": [], "trips_per_day": 0.0})
    assert twin.truth(sites.full(quiet), WEEKDAY)["grid"].all()


def test_the_genset_run_covering_a_midnight_outage_runs_on_with_it(company, monkeypatch):
    sid = make_site(name="Night set", control="auto_on_grid_loss", twin_extra={"windows": [], "trips_per_day": 40.0, "trip_minutes": 300.0},
                    demo_link=demo.DEMO_LINK)
    d = WEEKDAY + timedelta(days=1)
    t0 = twin.day_start_utc(d)
    monkeypatch.setattr(demo, "_clock", lambda: t0 + 3600)
    demo._simulate_span(sites.full(sid), t0 - 3600, t0 + 3600, None)
    g = telemetry.raw(sid, t0 - 120, t0 + 120, ("grid_present", "genset_running"))
    assert list(g["grid_present"]) == [0, 0, 0, 0] and list(g["genset_running"]) == [1, 1, 1, 1]


# ------------------------------------------------------------------------------------------------ the build lock is per company (security-roles-15)

def test_one_companys_demo_build_neither_blocks_nor_shows_in_anothers(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    a, b = tmp_path / "companies" / "a.db", tmp_path / "companies" / "b.db"
    a.parent.mkdir(parents=True)
    monkeypatch.setattr(demo, "create_sites", lambda by, **k: [])
    monkeypatch.setattr(demo, "DEMO_SITES", [])
    with store.use_company(a):
        held = demo._lock_for(store.db_path())
    assert held.acquire(blocking=False)                                      # company A's build is running
    try:
        with store.use_company(a):
            assert demo.load("t", 2, background=False)["message"] == "The demo is already being built"
        with store.use_company(b):
            st = demo.load("t", 2, background=False)                         # before: refused, "already being built", by A's build
            assert st["state"] == "ready" and demo.status()["state"] == "ready"
    finally:
        held.release()


# ------------------------------------------------------------------------------------------------ the days of history (pages-A-04), the load gate

def test_the_demo_status_names_the_days_of_history_a_build_makes(company, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DEMO_DAYS", "9")
    assert routes_demo.status()["days"] == 9                                 # nothing loaded: what a load would build
    monkeypatch.delenv("GRIDLYTICS_DEMO_DAYS")
    assert routes_demo.status()["days"] == 21                                # the default (DESIGN_LOG: the plan's 90 took too long to seed)
    demo._set_status(state="ready", progress=56, total=56)                   # a build recorded before its days were kept: 14 days of 4 sites
    assert routes_demo.status()["days"] == 14
    monkeypatch.setattr(demo, "create_sites", lambda by, **k: [])
    store.set_setting("demo_status", {"state": "none"})
    demo.load("t", 5, background=False)
    assert demo.status()["days"] == 5 and routes_demo.status()["days"] == 5


def test_the_demo_status_still_answers_when_the_days_setting_is_not_a_positive_number(company, monkeypatch):
    """GET /api/demo now reads GRIDLYTICS_DEMO_DAYS on every call (every empty state polls it), so a bad value must not fail the read, nor promise a
    history of no days or of negative days: the default stands (review of pages-A-04)."""
    for bad in ("ninety", "", "0", "-3", "2.5"):
        monkeypatch.setenv("GRIDLYTICS_DEMO_DAYS", bad)
        assert demo.default_days() == 21 and routes_demo.status()["days"] == 21, bad   # before: ValueError (a 500) or 0 / -3 days


def test_only_an_administrator_loads_the_demo_sites(world, monkeypatch):
    calls = []
    monkeypatch.setattr(demo, "load", lambda by, days=None, background=True: calls.append(by) or {"state": "running"})
    for role in ("operator", "analyst", "viewer"):
        r = world.c[role].post("/api/demo")
        assert r.status_code == 403, (role, r.text)                          # before: an operator loaded them (edit_sites)
    assert world.c["admin"].post("/api/demo").status_code == 200 and len(calls) == 1


# ------------------------------------------------------------------------------------------------ the account seed (tracker-and-docs-21)

def test_the_seed_names_only_templates_the_repository_holds_and_they_are_a_valid_list(capsys):
    with pytest.raises(SystemExit):
        seed_demo.main(["--help"])
    named = set(re.findall(r"demo/[\w.-]+\.csv", seed_demo.__doc__ + capsys.readouterr().out))
    assert named >= {"demo/companies.template.csv", "demo/users.template.csv"}
    missing = [n for n in named if not (seed_demo.ROOT / n).is_file()]
    assert missing == []                                                     # before: neither template was in the repository
    companies, people = seed_demo.read_list(seed_demo.ROOT / "demo/companies.template.csv", seed_demo.ROOT / "demo/users.template.csv")
    assert companies and {p["company_role"] for p in people} >= {"owner", "member"}
    assert {p["role"] for p in people if p["company_role"] != "owner"} == {"operator", "analyst", "viewer"}


def test_running_the_seed_again_keeps_what_the_first_run_made(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    out = tmp_path / "ACCOUNTS.md"
    first = seed_demo.seed(out=out)
    text = out.read_text(encoding="utf-8")
    env = (tmp_path / seed_demo.ENV_NAME).read_text(encoding="utf-8")
    companies = sorted((c["id"], c["name"], c["seats"]) for c in platformdb.list_companies())
    accounts = sorted((a["id"], a["email"]) for a in (platformdb.account_by_email(p["email"]) for p in seed_demo.DEMO_PEOPLE))
    assert len(first["accounts"]) == len(seed_demo.DEMO_PEOPLE) and len(companies) == len(seed_demo.DEMO_COMPANIES)
    again = seed_demo.seed(out=out)
    assert again["accounts"] == [] and again["memberships"] == [] and not any(c["made"] for c in again["companies"])
    assert sorted(again["kept"]) == sorted(p["email"] for p in seed_demo.DEMO_PEOPLE) and again["unknown"] == []
    assert sorted((c["id"], c["name"], c["seats"]) for c in platformdb.list_companies()) == companies
    assert sorted((a["id"], a["email"]) for a in (platformdb.account_by_email(p["email"]) for p in seed_demo.DEMO_PEOPLE)) == accounts
    pw = lambda t: sorted(re.findall(r"^\|.*?\| `([^`|]+@[^`|]+)` \| `([^`|]+)` \|", t, re.M))
    assert pw(out.read_text(encoding="utf-8")) == pw(text) and len(pw(text)) == len(seed_demo.DEMO_PEOPLE)  # the same passwords listed again
    assert (tmp_path / seed_demo.ENV_NAME).read_text(encoding="utf-8") == env                            # the founder's login kept
    # the demo sites already in the demo company are kept, not built a second time
    row = next(c for c in platformdb.list_companies() if c["name"] == seed_demo.DEMO_COMPANIES[0]["name"])
    with store.use_company(platformdb.company_file(row)):
        made = demo.create_sites("t")
    monkeypatch.setattr(demo, "load", lambda *a, **k: pytest.fail("the demo sites were built again"))
    seed_demo.load_demo_sites(2)
    with store.use_company(platformdb.company_file(row)):
        assert sorted(s["id"] for s in demo.demo_sites()) == sorted(made)
