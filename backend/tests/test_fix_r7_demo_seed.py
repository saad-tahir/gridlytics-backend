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
