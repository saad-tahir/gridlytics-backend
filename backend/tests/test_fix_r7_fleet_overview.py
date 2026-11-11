"""Round 7 (audit), the Fleet and site Overview: the month-to-date saving is the Savings page's month figure, MDI lever included, with its method and
computation date (flow-savings-pilot-bill-02, its duplicates role-owner-01, role-operator-01 and role-analyst-02, and pages-A-08), and off-grid in the
fleet is the site's own fact, so a grid site whose connection is not saved yet is not labelled Off-grid and its outage counts (pages-A-03,
role-viewer-09). The Overview carries the plan's KPI tiles (today's solar used on site, battery cycles and outages covered), the peak window of
its chart's actual steps and the genset start requests waiting for an answer, its quick action (pages-A-09). The Fleet's 7-day bars draw each day's
claimed saving, as its month column, so a monitor site's replay noise is not a loss (role-analyst-14 and its duplicates); a viewer reads the
month's energy behind the claim, peak kWh moved and diesel avoided, in place of the rupees (pages-A-11, role-viewer-18). The fleet says which sites
run a plan, which are handed back to their native settings and which have a genset, for the summary strip and the filters of R8 2.1 (pages-A-17)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np

from gridlytics import alerts, plans, routes_sites, sites, store, telemetry
from gridlytics.routes_common import PKT, today
from test_fix_api import _fake_plan, _genset_plan, _now, world  # noqa: F401  (world is a fixture)


def _row(fleet: dict, site_id: str) -> dict:
    return next(s for s in fleet["sites"] if s["id"] == site_id)


def _day(claimed: float, peak_a: float, peak_b: float, rate: float) -> dict:
    """A scored native day as verify stores it: a claimed saving, and the day's peaks behind the month's MDI lever."""
    return {"saving_rs": claimed, "claimed_rs": claimed, "levers": {"peak_window": claimed}, "fidelity": "confirmed", "litres_saved": 0.0,
            "actual": {"cost_rs": 1000.0, "e_end_kwh": 0.0, "v_end": 0.0}, "baseline": {"cost_rs": 1000.0 + claimed, "e_end_kwh": 0.0, "v_end": 0.0},
            "peak_kw_actual": peak_a, "peak_kw_baseline": peak_b, "mdi_rate": rate, "unserved_kwh_actual": 0.0, "unserved_kwh_baseline": 0.0}


def _put_days(site_id: str, days: dict[str, dict]) -> None:
    with store.connect() as con:
        for day, data in days.items():
            con.execute("INSERT OR REPLACE INTO savings_days (site_id, day_local, baseline, data, computed) VALUES (?,?,?,?,?)",
                        (site_id, day, "native", json.dumps(data), store._now()))


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-02, pages-A-08

def test_the_month_to_date_saving_on_fleet_and_overview_is_the_savings_page_s_month_figure_with_its_mdi_lever(world):
    a, sid = world.c["admin"], world.site
    t = today()
    first = t.replace(day=1)
    with world.use():
        # two scored days this month (one when today is the 1st): a daily claim, and a month peak 40 kW under the native replay's at Rs 1,500 a kW;
        # and a day of last month, which is not this month's
        _put_days(sid, {first.isoformat(): _day(700.0, 300.0, 340.0, 1500.0), t.isoformat(): _day(512.4, 290.0, 320.0, 1500.0),
                        (first - timedelta(days=1)).isoformat(): _day(9999.0, 100.0, 900.0, 1500.0)})
    sav = a.get(f"/api/sites/{sid}/savings").json()                                     # the Savings page's "This month"
    native = sav["baselines"]["native"]
    assert native["levers"]["mdi"] > 0                                                    # the month's MDI lever is part of the claim
    month = round(native["claimed_rs"], 0)
    now = a.get(f"/api/sites/{sid}/now").json()["month"]
    fleet = a.get("/api/fleet").json()
    row = _row(fleet, sid)
    assert now["saving_rs"] == month and row["mtd_saving_rs"] == month                    # before: the daily claims alone, without the MDI lever
    assert fleet["summary"]["mtd_saving_rs"] == sum(r["mtd_saving_rs"] or 0 for r in fleet["sites"])
    assert abs(fleet["summary"]["mtd_saving_rs"] - a.get("/api/fleet/savings").json()["total_claimed_rs"]) <= 0.5 * len(fleet["sites"])
    # the method and the computation date go with the figure (plan 9.7), as the Savings page shows them
    assert now["saving_rs_method"] == row["mtd_saving_rs_method"] == fleet["summary"]["mtd_saving_rs_method"] == "counterfactual"
    assert now["saving_rs_computed"] == row["mtd_saving_rs_computed"] == fleet["summary"]["mtd_saving_rs_computed"] == sav["computed"]
    assert sav["computed"]
    # a site with no scored day this month has no figure and no date, never a zero
    other = _row(fleet, world.other)
    assert other["mtd_saving_rs"] is None and other["mtd_saving_rs_computed"] is None
    # a viewer and a key get none of it: the figure, its method or its date
    for blob in (json.dumps(world.c["viewer"].get(f"/api/sites/{sid}/now").json()), json.dumps(world.c["viewer"].get("/api/fleet").json())):
        assert "saving_rs" not in blob and "saving_rs_method" not in blob and "saving_rs_computed" not in blob


# ------------------------------------------------------------------------------------------------ pages-A-03, role-viewer-09

def test_a_grid_site_without_its_connection_yet_is_not_off_grid_in_the_fleet_and_its_outage_counts(world):
    a, two = world.c["admin"], world.other                 # Secret Site Two: no connection yet, not a farm, no off_grid flag
    assert _row(a.get("/api/fleet").json(), two)["category"] is None
    with world.use():
        telemetry.ingest(two, [{"ts": _now() // 60 * 60 - 120, "grid_present": 0, "pv_power_w": 900}], "edge")
    assert a.get("/api/fleet").json()["summary"]["in_outage"] == 1      # before: left out as "Off-grid"
    with world.use():
        farm = sites.create({"name": "Chak farm", "site_type": "farm"}, "test")["id"]
        flagged = sites.create({"name": "Island office", "site_type": "office", "off_grid": True}, "test")["id"]
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, farm)["category"] == "Off-grid" and _row(fleet, flagged)["category"] == "Off-grid"   # off-grid stays the site's own fact
    assert _row(fleet, world.site)["category"] == "B-2(b)"
    assert a.put(f"/api/sites/{two}/connection", json={"regime": "off_grid"}).status_code == 200
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, two)["category"] == "Off-grid" and fleet["summary"]["in_outage"] == 0


# ------------------------------------------------------------------------------------------------ pages-A-09

def test_the_overview_reads_today_s_solar_used_on_site_battery_cycles_and_outages_covered(world, monkeypatch):
    a, sid = world.c["admin"], world.site                  # Mill One: a 200 kWh pack, a grid connection
    d = today() - timedelta(days=1)                        # a whole day in the past, whatever the hour the test runs at
    day0 = int(datetime(d.year, d.month, d.day, tzinfo=PKT).timestamp())
    now = day0 + 14 * 3600
    monkeypatch.setattr(routes_sites, "now_ts", lambda: now)
    monkeypatch.setattr(routes_sites, "today", lambda: d)
    rows = []
    for m in range(240):                                   # 10:00 to 14:00
        r = {"ts": day0 + 10 * 3600 + 60 * m, "grid_present": 1, "pv_power_w": 0, "battery_power_w": 0, "grid_export_wh": 0, "unserved_w": 0}
        if m < 120:
            r.update(pv_power_w=60_000, grid_export_wh=250)        # 120 kWh of solar, 30 kWh of it exported: 75 % used on site
        if 120 <= m < 180:
            r["battery_power_w"] = 50_000                          # 50 kWh out of the 200 kWh pack: a quarter of a cycle
        if 120 <= m < 150:
            r["grid_present"] = 0                                  # an outage the battery carries
        if 180 <= m < 200:
            r.update(grid_present=0, unserved_w=5_000)             # one that leaves critical load unserved
        rows.append(r)
    with world.use():
        telemetry.ingest(sid, rows, "edge", now=now)
    k = a.get(f"/api/sites/{sid}/now").json()["today_kpis"]
    assert k == {"pv_self_pct": 75.0, "battery_cycles": 0.25, "outages": 2, "outages_covered": 1}
    assert world.c["viewer"].get(f"/api/sites/{sid}/now").json()["today_kpis"] == k          # energy and counts: a viewer reads them too
    # a site with no data today has none of them, never a zero
    assert a.get(f"/api/sites/{world.other}/now").json()["today_kpis"] == {"pv_self_pct": None, "battery_cycles": None, "outages": None,
                                                                           "outages_covered": None}


def test_the_overview_chart_marks_the_peak_window_of_its_actual_steps(world):
    a, sid = world.c["admin"], world.site
    now = _now()
    with world.use():
        telemetry.ingest(sid, [{"ts": now - 900 * i - 60, "grid_present": 1, "pv_power_w": 1000} for i in range(96)], "edge")
        conn = sites.connections(sid)[0]
    c = a.get(f"/api/sites/{sid}/overview/chart").json()
    ts = c["actual"]["ts"]
    assert len(ts) >= 95 and len(c["peak"]) == len(ts)
    with world.use():
        expected = [bool(x) for x in plans.peak_mask(conn, np.asarray(ts))]
    assert c["peak"] == expected and any(expected) and not all(expected)     # B-2(b) has a peak window in every day
    assert world.c["viewer"].get(f"/api/sites/{sid}/overview/chart").json()["peak"] == expected
    # a site with no grid tariff has no peak window
    assert all(not x for x in a.get(f"/api/sites/{world.other}/overview/chart").json()["peak"])


def test_the_overview_lists_the_genset_start_requests_waiting_for_an_answer(world):
    a, sid = world.c["admin"], world.site                  # Mill One: a manual-start genset
    t0 = _now() // 900 * 900
    with world.use():
        telemetry.ingest(sid, [{"ts": t0 - 60, "grid_present": 0, "genset_running": 0}], "edge")
        alerts.genset_requests(sites.full(sid), _genset_plan(t0, range(0, 8)), t0 + 30)
    waiting = a.get(f"/api/sites/{sid}/now").json()["genset_requests"]
    assert [r["payload"]["start"] for r in waiting] == [t0]
    assert a.post(f"/api/sites/{sid}/genset/requests/{waiting[0]['id']}", json={"outcome": "done"}).status_code == 200
    assert a.get(f"/api/sites/{sid}/now").json()["genset_requests"] == []


# ------------------------------------------------------------------------------------------------ frontend-states-formats-14

def test_the_fleet_s_month_saving_is_missing_without_a_scored_day_and_a_real_zero_stays_zero(world):
    a = world.c["admin"]
    assert a.get("/api/fleet").json()["summary"]["mtd_saving_rs"] is None      # no site has a scored day this month: missing, not Rs 0
    with world.use():
        _put_days(world.site, {today().isoformat(): _day(0.0, 300.0, 300.0, 1500.0)})      # a scored day that saved nothing
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, world.site)["mtd_saving_rs"] == 0 and fleet["summary"]["mtd_saving_rs"] == 0


# ------------------------------------------------------------------------------------------------ role-analyst-14, role-owner-13, flow-savings-pilot-bill-21, engine-money-live-14

def test_the_fleet_s_7_day_bars_carry_each_day_s_claimed_saving_so_unclaimed_replay_noise_is_not_a_loss(world):
    a, sid = world.c["admin"], world.site
    t = today()
    y = t - timedelta(days=1)
    # a recommendation-only day (the Lahore mall's -Rs 0.09): the replay's figure, which nothing claims; and a day that claims its saving
    noise = {**_day(0.0, 300.0, 300.0, 1500.0), "saving_rs": -0.09, "claimed_rs": 0.0, "fidelity": "recommendation_only"}
    with world.use():
        _put_days(sid, {y.isoformat(): noise, t.isoformat(): _day(512.4, 300.0, 300.0, 1500.0)})
    spark = _row(a.get("/api/fleet").json(), sid)["spark"]
    assert [(p["day"], p["claimed_rs"]) for p in spark] == [(y.isoformat(), 0.0), (t.isoformat(), 512.4)]    # before: saving_rs only
    assert spark[0]["saving_rs"] == -0.09                                                  # the replay's own figure stays beside it
    # money both: a viewer's bars carry the days only
    assert [set(p) for p in _row(world.c["viewer"].get("/api/fleet").json(), sid)["spark"]] == [{"day"}, {"day"}]


# ------------------------------------------------------------------------------------------------ pages-A-11, role-viewer-18

def _claimed_day(claimed: float, peak_actual: float, peak_native: float, litres: float | None, fidelity: str = "confirmed") -> dict:
    d = _day(claimed if fidelity == "confirmed" else 0.0, 300.0, 300.0, 1500.0)
    d.update(fidelity=fidelity, litres_saved=litres, saving_rs=claimed)
    d["actual"] = {**d["actual"], "peak_import_kwh": peak_actual}
    d["baseline"] = {**d["baseline"], "peak_import_kwh": peak_native}
    return d


def test_a_viewer_reads_the_month_s_peak_energy_moved_and_diesel_avoided_in_place_of_the_rupees(world):
    a, v, sid = world.c["admin"], world.c["viewer"], world.site          # Mill One: a grid connection and a genset
    t = today()
    first = t.replace(day=1)
    days = {t.isoformat(): _claimed_day(700.0, 400.0, 520.0, 12.5),
            (first - timedelta(days=1)).isoformat(): _claimed_day(900.0, 100.0, 900.0, 80.0)}    # last month's: not this month's
    if first != t:                                                       # a recommendation-only day claims nothing, so its energy is not counted
        days[first.isoformat()] = _claimed_day(500.0, 100.0, 900.0, 30.0, fidelity="recommendation_only")
    with world.use():
        _put_days(sid, days)
    for c in (a, v):
        fleet = c.get("/api/fleet").json()
        row = _row(fleet, sid)
        assert row["mtd_peak_kwh_avoided"] == 120.0 and row["mtd_litres_saved"] == 12.5          # energy, not money: every role reads it
        assert fleet["summary"]["mtd_peak_kwh_avoided"] == 120.0 and fleet["summary"]["mtd_litres_saved"] == 12.5
        other = _row(fleet, world.other)                                  # no claimed day: missing, never a zero
        assert other["mtd_peak_kwh_avoided"] is None and other["mtd_litres_saved"] is None
    assert "mtd_saving_rs" not in _row(v.get("/api/fleet").json(), sid)
    # a site without a genset has no litres to avoid, and an off-grid one no peak import
    with world.use():
        _put_days(world.other, {t.isoformat(): _claimed_day(50.0, 0.0, 0.0, 0.0)})
    other = _row(a.get("/api/fleet").json(), world.other)
    assert other["mtd_litres_saved"] is None and other["mtd_peak_kwh_avoided"] == 0.0
    assert a.put(f"/api/sites/{world.other}/connection", json={"regime": "off_grid"}).status_code == 200
    assert _row(a.get("/api/fleet").json(), world.other)["mtd_peak_kwh_avoided"] is None


# ------------------------------------------------------------------------------------------------ pages-A-17

def test_the_fleet_counts_the_sites_on_a_plan_and_on_native_settings_and_names_those_with_a_genset(world):
    a, sid = world.c["admin"], world.site
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, sid)["has_genset"] is True and _row(fleet, world.other)["has_genset"] is False
    assert _row(fleet, sid)["on_plan"] is False and fleet["summary"]["on_plan"] == 0 and fleet["summary"]["on_native"] == 0
    _fake_plan(world)
    assert _row(a.get("/api/fleet").json(), sid)["on_plan"] is False        # at the monitor tier a plan is a recommendation only
    with world.use():
        sites.set_tier_capable(sid, "closed_loop")
        sites.set_tier_allowed(sid, "closed_loop", "test")
    # closed loop runs through an edge device: with none ever heard from, nothing reaches the inverter (control-edge, role-operator-11)
    assert _row(a.get("/api/fleet").json(), sid)["on_plan"] is False
    with world.use():
        sites.put_state(sid, edge_last_seen=_now())
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, sid)["on_plan"] is True and fleet["summary"]["on_plan"] == 1 and _row(fleet, sid)["on_native"] is False
    # released to its native settings: no longer on a plan, and counted as handed back (the row says so, so the web app's count of the real sites
    # is the summary's own rule, review)
    until = datetime.fromtimestamp(_now() + 7200, PKT).isoformat()
    assert a.post(f"/api/sites/{sid}/overrides", json={"kind": "force_native", "until": until}).status_code == 200
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, sid)["on_plan"] is False and fleet["summary"]["on_plan"] == 0 and fleet["summary"]["on_native"] == 1
    assert _row(fleet, sid)["on_native"] is True and _row(fleet, world.other)["on_native"] is False
    # the same for a viewer: none of it is money
    seen = world.c["viewer"].get("/api/fleet").json()
    assert seen["summary"]["on_native"] == 1 and _row(seen, sid)["has_genset"] is True


def test_a_hold_at_the_monitor_tier_is_not_a_plan_in_force(world):
    """Review of pages-A-17: control_state names a hold before the tier, so a hold at a monitor site read "hold" and the site was counted on a plan,
    although at the monitor tier the plan is a recommendation and nothing reaches the inverter."""
    a, sid = world.c["admin"], world.site                  # Mill One: the monitor tier
    _fake_plan(world)
    assert a.post(f"/api/sites/{sid}/control/hold", json={"hours": 2}).status_code == 200
    fleet = a.get("/api/fleet").json()
    row = _row(fleet, sid)
    assert row["control"]["key"] == "hold" and row["tier"] == "monitor"
    assert row["on_plan"] is False and fleet["summary"]["on_plan"] == 0
    with world.use():                                      # above the monitor tier the same hold is a plan in force
        sites.set_tier_capable(sid, "closed_loop")
        sites.set_tier_allowed(sid, "closed_loop", "test")
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, sid)["on_plan"] is True and fleet["summary"]["on_plan"] == 1


# ------------------------------------------------------------------------------------------------ frontend-states-formats-19 (pages-A-08's duplicate)

def test_the_reading_now_says_whether_its_state_of_charge_is_the_estimate_or_the_bms_s(world):
    """The SoC on Fleet and Overview is the estimate where the source sends one, else the BMS's own reading (_snapshot): the snapshot says which, so
    the pages mark it, as the Battery page does (plan 9.7, "Estimated" on estimated SoC)."""
    a, v, sid = world.c["admin"], world.c["viewer"], world.site
    t = _now() // 60 * 60
    with world.use():
        telemetry.ingest(sid, [{"ts": t - 180, "grid_present": 1, "battery_soc_bms_pct": 61.0}], "edge")
    assert a.get(f"/api/sites/{sid}/now").json()["now"]["soc_source"] == "bms"
    assert _row(a.get("/api/fleet").json(), sid)["now"]["soc_source"] == "bms"
    with world.use():
        telemetry.ingest(sid, [{"ts": t - 60, "grid_present": 1, "battery_soc_bms_pct": 62.0, "battery_soc_est_pct": 60.5}], "edge")
    now = v.get(f"/api/sites/{sid}/now").json()["now"]                     # not money: a viewer reads it too
    assert now["soc_pct"] == 60.5 and now["soc_source"] == "estimate"
    assert _row(v.get("/api/fleet").json(), world.other)["now"] is None      # no reading, no snapshot
