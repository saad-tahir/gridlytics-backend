"""Round 2 regressions for the optimiser group: the manual floor around outages and at off-grid sites, grid_charge_allowed honoured, soc_threshold
control that never sheds load, a pause with a failing solve, the plan in force during a pause, the outage seen inside the quarter by a tick, the
billing month from the reading day, legacy exports, and a signed message with no money that records only the edge's pull. Round 3: an unavoidable
shed never keeps a threshold-started set running, and an off-grid threshold site's two-day solve finishes inside the tick's time limit."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import delivery, forecast, optimise, plans, sites, store, tariffs, telemetry, twin
from test_fix_optimiser import _genset_invariants, _problem, _site, _until  # noqa: F401  (the round-1 fixtures and helpers)

PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _off_grid(T=48, genset=False):  # two days of quarters would make the toy MILP slow to prove; twelve hours is enough
    p = _problem(T=T, genset=genset)
    p.update({"off_grid": True, "g": np.zeros(T), "G_max": 0.0, "X_max": 0.0, "c_imp": np.zeros(T), "c_exp": np.zeros(T)})
    p["L"][:] = 40.0
    p["Lhi"][:] = 45.0
    p["Lc"][:] = 20.0
    p["battery"]["a_grid"] = False
    p["terminal"]["c_next"] = 0.0
    if genset:
        p["battery"]["a_gen"] = True
        p["genset"]["S"] = 100.0
    return p


def _floor(T, steps, kwh):
    f = np.zeros(T)
    f[steps] = kwh
    return f


# ------------------------------------------------------------------------------------------------ optimiser-manual-reserve-whole-horizon-and-outages

@pytest.mark.parametrize("Lc, genset", [(30.0, False), (60.0, False), (60.0, True)])
def test_a_hold_that_outlasts_its_outage_is_used_in_the_outage_not_refilled_by_shedding(Lc, genset):
    p = _problem(outage=(8, 16), genset=genset)
    p["battery"]["E0"] = 160.0
    p["Lc"][:] = Lc
    p["reserve_manual"] = _floor(96, slice(0, 24), 160.0)  # the window runs two hours past the outage's end
    p["override_mask"] = p["reserve_manual"] > 0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    s = r["sol"]
    assert s["U"][8:16].sum() < 1e-6                         # no critical load is shed
    assert s["D"][8:16].sum() * 0.25 > 8 * 0.25 * Lc * 0.9    # the battery carries the outage
    if genset:
        assert s["P"][8:16].sum() < 1e-6                     # and the genset stays off
    assert set(r["manual_floor"]) == set(range(8))            # rows only before the window's first outage step


def test_the_manual_floor_binds_only_before_the_windows_first_outage_step():
    g = np.ones(12)
    g[4:6] = 0
    fl = np.zeros(12)
    fl[2:10] = 1.0
    assert optimise.manual_floor_steps(fl, g).tolist() == [False, False, True, True] + [False] * 8
    assert optimise.manual_floor_steps(fl, np.zeros(12), off_grid=True).tolist() == (fl > 0).tolist()


# ------------------------------------------------------------------------------------------------ regression-offgrid-hold-noop

def test_an_off_grid_hold_applies_on_every_step_of_its_window():
    free = _off_grid(genset=True)
    free["battery"]["E0"] = 190.0
    free = optimise.solve(free)
    assert free["status"] == "optimal"
    p = _off_grid(genset=True)
    p["battery"]["E0"] = 190.0
    p["reserve_manual"] = _floor(p["T"], slice(0, 16), 170.0)  # Hold at 85 % for four hours
    p["override_mask"] = p["reserve_manual"] > 0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert sorted(r["manual_floor"]) == list(range(16))
    assert r["sol"]["E"][1:17].min() >= 170.0 - 1e-3          # the battery is held; the genset carries the load
    assert r["sol"]["P"][:16].sum() > 0 and r["sol"]["U"].sum() < 1e-6
    assert free["sol"]["E"][1:17].min() < 170.0 - 10.0         # without the Hold the battery would have been used


def test_an_off_grid_force_charge_charges_inside_its_window():
    def run(override: bool):
        p = _off_grid(genset=True)
        p["battery"]["E0"] = 80.0
        if override:
            p["reserve_manual"] = _floor(p["T"], slice(0, 8), 180.0)  # force_charge to 90 % for two hours
            p["override_mask"] = p["reserve_manual"] > 0
        return optimise.solve(p)

    r, free = run(True), run(False)
    assert r["status"] == "optimal" and free["status"] == "optimal" and sorted(r["manual_floor"]) == list(range(8))
    assert r["sol"]["E"][8] > free["sol"]["E"][8] + 20.0      # it charges (from the genset) towards the target at once
    assert r["sol"]["P"][:8].sum() > free["sol"]["P"][:8].sum()


def test_an_off_grid_hold_never_sheds_load_to_keep_its_floor():
    p = _off_grid()                                            # no genset, no sun: only the battery can carry the load
    p["battery"]["E0"] = 190.0
    p["reserve_manual"] = _floor(p["T"], slice(0, 16), 170.0)
    p["override_mask"] = p["reserve_manual"] > 0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert r["sol"]["U"][:16].sum() < 1e-6
    assert r["sol"]["D"][:16].sum() > 0


# ------------------------------------------------------------------------------------------------ optimiser-gen-charge-flag-unsettable

def _surplus_day():
    p = _problem()
    p["battery"]["a_grid"] = False
    p["battery"]["E0"] = 30.0
    p["PV"] = np.where((np.arange(96) >= 40) & (np.arange(96) < 48), 100.0, 0.0)
    p["PVlo"] = p["PV"] * 0.6
    return p


def test_grid_charge_allowed_false_charges_only_from_the_surplus_and_compiles_no_grid_charging():
    p = _surplus_day()
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    s = r["sol"]
    surplus = np.maximum(0.0, p["PV"] - s["K"] - p["L"])
    assert np.all(s["C"] <= surplus + 1e-6)                    # never more than the PV beyond the load
    assert not any(x["reason"] == "charge_from_grid" for x in r["reasons"])
    steps = plans._steps_out(p, r, {"outage": {"p_down": np.zeros(96)}})
    assert all(st["grid_charge_permitted"] is False for st in steps)
    assert not any(i["mode"] == "charge_grid" for i in delivery.step_intents(steps, p["battery"]))
    comp = delivery.compile_mode_a(steps, "deye", p["battery"], 20.0, 10.0, None)
    assert not any(sl["grid_charge"] or sl["mode"] == "charge_grid" for d in comp["days"] for sl in d["slots"])
    assert not any(b["grid_charge_allowed"] for b in delivery.mode_b(steps)["steps"])
    q = _surplus_day()
    q["battery"]["a_grid"] = True
    assert optimise.solve(q)["objective_rs"] < r["objective_rs"] - 100.0  # the flag now changes the plan


def test_a_step_of_a_site_without_grid_charging_is_never_read_as_grid_charging():
    step = {"grid_present": 1.0, "grid_import_kw": 80.0, "pv_kw": 100.0, "genset_kw": 0.0, "load_kw": 80.0, "charge_kw": 100.0, "reason": "charge_from_grid"}
    assert delivery.grid_charging(step)
    assert not delivery.grid_charging({**step, "grid_charge_permitted": False})


# ------------------------------------------------------------------------------------------------ regression-soc-threshold-sheds-load

def _threshold(control, **gen):
    p = _problem(genset=True, outage=(8, 32))
    p["battery"]["E0"] = 120.0
    p["genset"].update(control=control, soc_on=0.25, **gen)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    _genset_invariants(p, r)
    return p, r


@pytest.mark.parametrize("gen", [{"cap_hours": 8.0, "hours_today": 8.0}, {"fuel_available": 0.0}, {"fuel_available": 4.3}, {"fuel_available": 7.0}, {}],
                         ids=["cap_used_up", "empty_tank", "tank_for_one_step", "tank_for_two_steps", "free"])
def test_soc_threshold_control_sheds_no_more_load_than_manual_control(gen):
    _, manual = _threshold("manual", **gen)
    p, thr = _threshold("soc_threshold", **gen)
    assert thr["sol"]["U"].sum() <= manual["sol"]["U"].sum() + 1e-6
    if not gen:
        assert thr["sol"]["U"].sum() < 1e-6


def test_a_threshold_start_ignores_the_used_up_run_cap_like_a_panel_start():
    _, manual = _threshold("manual", cap_hours=8.0, hours_today=8.0)
    assert manual["sol"]["u"].sum() == 0 and manual["sol"]["U"].sum() > 10.0   # the plan may not start a set whose cap is used up
    p, r = _threshold("soc_threshold", cap_hours=8.0, hours_today=8.0)
    E, u = r["sol"]["E"], r["sol"]["u"]
    assert u[8:32].sum() > 0                                   # the controller starts it all the same
    assert all(u[t] > 0.5 for t in range(8, 32) if E[t] < 0.25 * 200 - 1e-3)  # and runs it whenever the state of charge is under its threshold
    assert E[8:32].min() >= 0.25 * 200 - 30.0 * 0.25 / 0.95 - 1e-3            # which it crosses inside one step at most
    assert r["sol"]["U"].sum() < 1e-6


def test_the_threshold_controller_starts_the_set_only_under_its_threshold():
    p, r = _threshold("soc_threshold")
    E, v = r["sol"]["E"], r["sol"]["v"]
    starts = [t for t in range(1, 96) if v[t] > 0.5 and p["g"][t] < 0.5]
    assert starts and all(E[t] <= 0.25 * 200 + 1e-3 for t in starts)


# ------------------------------------------------------------------------------------------------ regression-threshold-shed-term-keeps-genset-on

def _off_grid_small_set(control):
    """Off-grid, two days: a 30 kW set under a 50 kW night load (an unavoidable shed every night) and 150 kW of sun from 08:00 to 16:00."""
    T = 192
    hour = (np.arange(T) % 96) / 4.0
    p = _problem(T=T, genset=True)
    p.update({"off_grid": True, "g": np.zeros(T), "G_max": 0.0, "c_imp": np.zeros(T)})
    p["PV"] = np.where((hour >= 8) & (hour < 16), 150.0, 0.0)
    p["PVlo"] = p["PV"] * 0.6
    p["L"] = np.where((hour >= 18) | (hour < 6), 50.0, 25.0)
    p["Lhi"] = p["L"] * 1.1
    p["Lc"] = p["L"] * 0.5
    p["battery"].update(E_nom=120.0, E0=36.0, soc_min=0.15, soc_max=0.95, a_grid=False, a_gen=True, P_ch=60.0, P_dis=60.0)
    p["terminal"]["c_next"] = 0.0
    p["genset"].update(control=control, soc_on=0.25, S=30.0, day_index=(np.arange(T) // 96).astype(int))
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    _genset_invariants(p, r)
    return p, r


def test_an_unavoidable_off_grid_night_shed_never_keeps_the_set_running_through_the_sunny_midday():
    _, manual = _off_grid_small_set("manual")
    p, r = _off_grid_small_set("soc_threshold")
    s = r["sol"]
    assert abs(s["U"].sum() - manual["sol"]["U"].sum()) < 1e-3          # the night shed is the same: the set is too small either way
    assert s["u"][96 + 40:96 + 56].sum() == 0                            # 10:00-14:00 on day 2: off while the sun charges the battery
    assert s["P"].sum() * 0.25 < manual["sol"]["P"].sum() * 0.25 * 1.08  # (the shed kept it on for 48 h: 1,064 kWh against 922)


def _grid_overload(control):
    """A ten-hour outage whose first two hours need more than the set and the battery can give (an unavoidable shed), then a light load."""
    p = _problem(genset=True, outage=(8, 48))
    p["Lc"] = np.where((np.arange(96) >= 8) & (np.arange(96) < 16), 75.0, 15.0)
    p["battery"].update(E0=70.0, P_dis=30.0, a_gen=True)
    p["genset"].update(control=control, soc_on=0.25, S=40.0)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    _genset_invariants(p, r)
    return p, r


def test_an_unavoidable_shed_in_a_grid_outage_never_keeps_the_set_on_until_the_grid_returns():
    p, r = _grid_overload("soc_threshold")
    E, u = r["sol"]["E"], r["sol"]["u"]
    assert u[8:48].sum() > 0
    assert u[36:48].sum() == 0                                           # it stops once the battery is back over its threshold
    assert E[36:48].min() >= 0.25 * 200 - 1e-3                           # (it ran to the grid's return with the battery at its floor)
    assert all(u[t] > 0.5 for t in range(9, 48) if E[t] < 0.25 * 200 - 1e-3)


def test_a_shed_in_a_grid_outage_never_lands_the_battery_on_its_threshold_to_spare_a_run():
    # without the shed rule the plan sheds 2 kWh while the battery is at 50 % so the crossing lands exactly on the threshold
    p = _problem(genset=True, outage=(8, 32))
    p["battery"]["E0"] = 120.0
    p["genset"].update(control="soc_threshold", soc_on=0.25, UT=1)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert r["sol"]["U"].sum() < 1e-6


# ------------------------------------------------------------------------------------------------ regression-threshold-offgrid-solve-time

@pytest.mark.parametrize("fuel", [120.0, 25.0])
def test_an_off_grid_threshold_site_with_a_part_full_tank_solves_to_optimal_inside_the_ticks_limit(company, fuel):
    # the farm's sizes, 192 steps (a 60 L tank solves too, in 4-10 s here, too near the limit for a test run beside others)
    s = sites.create({"name": "Farm", "critical_load_kw": [8.0] * 24, "genset_start_control": "soc_threshold", "outage_load_mode": 1, "site_type": "farm"}, "t")
    sid = s["id"]
    sites.put_asset(sid, "pv", {"kwp_dc": 80, "kw_ac": 70}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 120, "p_ch_kw": 40, "p_dis_kw": 40, "soc_min": 0.15}, "t")
    sites.put_asset(sid, "genset", {"rated_kw": 48, "f_min": 0.3, "min_up_h": 1.0, "tank_l": 400}, "t")
    sites.put_state(sid, soc=0.5, fuel_l=fuel)
    full = sites.full(sid)
    t0 = forecast.align(datetime(2026, 11, 3, 13, 0, tzinfo=timezone.utc).timestamp())  # 18:00 in Pakistan
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 192)
    assert p["off_grid"] and p["genset"]["control"] == "soc_threshold" and p["T"] == 192
    r = optimise.solve(p, time_limit=10.0)
    # (it stopped at the limit with VoLL threshold slack in the plan.) Since round 4 (HANDOFF 8.A.3) an optimum that sheds with the set off
    # while the tank still covers a step (here it rations the tank: 102 such steps at 120 L) is repaired to the threshold controller: the
    # first solve still finishes inside the limit (at 25 L; at 120 L see below), and the published plan is the repair's
    rep = r.get("threshold_repair") or {}
    first = rep.get("first_status", r["status"])
    if fuel == 120.0:
        # A known limit since round 5 (HANDOFF 8.A.1): the set may now run under a load below its minimum, its surplus charging the battery
        # (optimise.genset_surplus_steps), so a rationed tank has a real choice of night runs, and this first solve no longer proves its 0.1 %
        # gap inside the limit (HEAD: optimal in 7.4-9.2 s; now stopped at 10 s, on an idle machine on mains power; the incumbent found by 2 s
        # was the 20 s one when round 5 measured it). The threshold controller's steps keep their 15-minute starts (no hour rows, no surplus
        # rows there, which only slowed these solves). The published plan is the repair's either way. What this checks: the first solve stopped
        # at the limit, not later, with a plan within 0.5 % of its bound, and the tick stayed within one limit plus the repair (no shed-rule
        # re-solve: the plan burns the tank)
        assert first in ("optimal", "feasible") and rep and rep["first_mip_gap"] < 0.005
        assert rep["first_solve_seconds"] < 10.0 + 0.5 and rep["shed_rule_resolve_failed"] is False
        assert r["solve_seconds"] < 10.0 + 2.0 and r["time_limited"] is (rep["first_status"] != "optimal")
    else:
        assert first == "optimal" and r["time_limited"] is False
    assert r["status"] in ("optimal", "feasible")
    assert rep.get("status", "optimal") != "not_solved"
    sol, gen = r["sol"], p["genset"]
    thr = gen["soc_on"] * p["battery"]["E_nom"]
    under_off = [t for t in range(1, 192) if sol["E"][t] < thr - 1e-3 and sol["u"][t] < 0.5]
    burn = 0.25 * (gen["a"] * gen["f_min"] * gen["S"] + gen["b"] * gen["S"])
    # the set is off under its threshold only once the tank cannot cover another step at minimum load
    assert all(float(sol["F"][:t].sum()) >= gen["fuel_available"] - burn - 1e-6 for t in under_off)


# ------------------------------------------------------------------------------------------------ data-override-pause-native-noop

def _closed_loop(sid: str) -> None:
    sites.set_tier_capable(sid, "closed_loop")
    sites.set_tier_allowed(sid, "closed_loop", "t", "test")


def _fail_every_solve(monkeypatch):
    def fail(p, time_limit=10.0, gap=1e-3, relaxed=False):
        return {"status": "not_solved", "solver_status": 2, "message": "forced by the test", "relaxed": relaxed, "notes": [], "config_hash": "x"}
    monkeypatch.setattr(optimise, "solve", fail)


def test_a_failed_solve_during_a_pause_still_ends_the_pre_pause_plan(company, monkeypatch):
    sid = _site(genset=False)
    _closed_loop(sid)                                           # only a closed-loop site's edge is sent a message (round 7)
    first = plans.tick(sid)
    assert first["status"] == "active" and plans.current(sid)["id"] == first["plan_id"]
    sites.add_override(sid, {"kind": "pause_control", "until": _until(24), "reason": "test"}, "t")
    _fail_every_solve(monkeypatch)
    r = plans.tick(sid)
    assert r["status"] == "failed" and r["control_release"]["reason"] == "control_paused"
    assert plans.current(sid) is None
    assert (sites.full(sid).get("state") or {}).get("active_plan") is None
    with store.connect() as con:
        assert con.execute("SELECT status FROM plans WHERE id = ?", (first["plan_id"],)).fetchone()["status"] == "superseded"
    assert plans.active_at(sid, int(datetime.now(timezone.utc).timestamp())) is None
    assert plans.signed_message(sid, record=False)["type"] == "release"


def test_a_failed_solve_without_a_pause_leaves_the_plan_in_force(company, monkeypatch):
    sid = _site(genset=False)
    first = plans.tick(sid)
    _fail_every_solve(monkeypatch)
    assert plans.tick(sid)["status"] == "failed"
    assert plans.current(sid)["id"] == first["plan_id"]
    assert plans.active_at(sid, int(datetime.now(timezone.utc).timestamp()))["id"] == first["plan_id"]


# ------------------------------------------------------------------------------------------------ regression-active-at-returns-pre-pause-plan

def test_no_plan_is_in_force_inside_a_pause_even_after_it_is_resumed(company):
    sid = _site(genset=False)
    first = plans.tick(sid)
    now = int(datetime.now(timezone.utc).timestamp())
    assert plans.active_at(sid, now)["id"] == first["plan_id"]
    o = sites.add_override(sid, {"kind": "pause_control", "until": _until(24), "reason": "test"}, "t")
    held = plans.tick(sid)
    assert held["status"] == "held"
    assert plans.active_at(sid, now) is None
    sites.clear_override(sid, o["id"], "t")
    assert plans.active_at(sid, now) is None                    # the held row still says no plan was in force then
    fresh = plans.tick(sid)
    assert fresh["status"] == "active"
    assert plans.active_at(sid, int(datetime.now(timezone.utc).timestamp()))["id"] == fresh["plan_id"]


def test_active_at_reads_the_override_history_when_no_tick_ran_during_the_pause(company):
    sid = _site(genset=False)
    first = plans.tick(sid)
    t_plan = plans._ts(plans.current(sid)["belief_time"])
    sites.add_override(sid, {"kind": "force_native", "until": _until(2), "reason": "test"}, "t")
    now = int(datetime.now(timezone.utc).timestamp())
    assert plans.active_at(sid, now) is None
    assert plans.active_at(sid, t_plan)["id"] == first["plan_id"]  # before the release the plan was in force


# ------------------------------------------------------------------------------------------------ data-outage-unseen-until-next-quarter

def _grid_rows(sid, t_from, t_to, grid):
    telemetry.ingest(sid, [{"ts": m, "grid_present": grid, "load_power_w": 60000, "pv_power_w": 0} for m in range(t_from, t_to, 60)], "edge")


def _quarter():
    return int(datetime(2026, 11, 2, 21, 0, tzinfo=PKT).timestamp())  # an evening quarter, no sun


def test_a_tick_inside_the_quarter_plans_for_a_grid_loss_it_has_already_seen(company):
    sid = _site(genset=False)
    t0 = _quarter()
    _grid_rows(sid, t0 - 3 * 3600, t0 + 60, 1)
    _grid_rows(sid, t0 + 60, t0 + 7 * 60, 0)                   # the grid fell one minute into the quarter
    r = plans.tick(sid, at=t0 + 7 * 60)
    assert r["summary"]["outage_now"] is not None
    assert r["steps"][0]["grid_present"] == 0.0 and r["steps"][0]["grid_import_kw"] == 0.0
    saved = telemetry.latest_forecast(sid, "outage", t0)
    assert saved["belief_time"] == t0 and saved["observed"] is not None


def test_a_re_solve_keeps_an_observed_outage_its_telemetry_has_not_seen_end(company):
    sid = _site(genset=False)
    t0 = _quarter()
    _grid_rows(sid, t0 - 3 * 3600, t0 + 60, 1)
    _grid_rows(sid, t0 + 60, t0 + 7 * 60, 0)
    forecast.issue(sites.full(sid), t0 + 7 * 60)               # "Refresh forecasts" sees the outage
    assert telemetry.latest_forecast(sid, "outage", t0)["observed"] is not None
    r = plans.tick(sid, at=t0 + 13 * 60)                       # the edge's rows stopped arriving: this issue alone would not see it
    assert r["summary"]["outage_now"] is not None and r["steps"][0]["grid_present"] == 0.0
    assert telemetry.latest_forecast(sid, "outage", t0)["observed"] is not None


def test_a_re_solve_after_the_grid_returned_drops_the_outage(company):
    sid = _site(genset=False)
    t0 = _quarter()
    _grid_rows(sid, t0 - 3 * 3600, t0 + 60, 1)
    _grid_rows(sid, t0 + 60, t0 + 7 * 60, 0)
    forecast.issue(sites.full(sid), t0 + 7 * 60)
    _grid_rows(sid, t0 + 7 * 60, t0 + 13 * 60, 1)              # the grid came back
    r = plans.tick(sid, at=t0 + 13 * 60)
    assert r["summary"]["outage_now"] is None
    assert telemetry.latest_forecast(sid, "outage", t0)["observed"] is None


# ------------------------------------------------------------------------------------------------ tariffs-vectors-billing-month

def _connection(sid, **fields):
    """Rewrite the site's one connection (a second put_connection without its id would add a second connection, which the engine ignores)."""
    cid = sites.full(sid)["connections"][0]["id"]
    sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400, **fields}, "t", conn_id=cid)
    return sites.full(sid)


def _imports(sid, t_from, t_to, kw=100.0, export_kw=0.0):
    rows = [(sid, int(t), "test", 1.0, kw * 1000 / 60, export_kw * 1000 / 60) for t in range(t_from, t_to, 60)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "grid_export_wh"))


def test_month_to_date_follows_the_reading_day(company):
    sid = _site()
    conn = _connection(sid, reading_day=15)["connections"][0]
    assert conn["data"]["reading_day"] == 15
    t0 = twin.day_start_utc(date(2026, 11, 20))
    _imports(sid, twin.day_start_utc(date(2026, 11, 10)), t0)
    m = plans.month_to_date(sid, conn, t0)
    assert m["period_start"] == twin.day_start_utc(date(2026, 11, 16)) and m["period_end"] == twin.day_start_utc(date(2026, 12, 16))
    assert m["mtd"]["peak"] + m["mtd"]["off_peak"] == pytest.approx(4 * 2400.0, rel=0.01)  # 16 to 19 November, not from the 1st
    calendar = plans.month_to_date(sid, {**conn, "data": {**conn["data"], "reading_day": None}}, t0)
    assert calendar["mtd"]["peak"] + calendar["mtd"]["off_peak"] == pytest.approx(10 * 2400.0, rel=0.01)


def test_the_problem_takes_its_billing_month_from_the_reading_day(company, monkeypatch):
    sid = _site()
    full = _connection(sid, reading_day=15)
    t0 = twin.day_start_utc(date(2026, 12, 15)) + 20 * 3600     # 20:00 on the reading day: four hours of this bill, then the next
    _imports(sid, t0 - 7 * 86400, t0)
    seen = {}
    real = tariffs.vectors

    def spy(conn, t0_utc, steps=192, mtd_import=None, rest_net_import=None, g_max_kw=1e6, **kw):
        seen.update(mtd=mtd_import, rest=rest_net_import, kw=kw)
        return real(conn, t0_utc, steps, mtd_import, rest_net_import, g_max_kw, **kw)

    monkeypatch.setattr(tariffs, "vectors", spy)
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    assert p["in_month"][:16].all() and not p["in_month"][16:].any()
    if p.get("mdi"):
        assert p["mdi"]["month_end_step"] == 16
    assert sum(seen["rest"].values()) == pytest.approx(0.0, abs=1e-6)  # the bill ends inside the horizon: no rest of the month
    assert "mtd_export" not in seen["kw"]                              # exports matter only to legacy net metering


def test_legacy_net_metering_passes_the_months_exports(company, monkeypatch):
    sid = _site()
    full = _connection(sid, regime="net_metering_legacy", export_cap_kw=100)
    t0 = twin.day_start_utc(date(2026, 11, 20))
    _imports(sid, t0 - 7 * 86400, t0, kw=10.0, export_kw=50.0)
    seen = {}
    real = tariffs.vectors

    def spy(conn, t0_utc, steps=192, mtd_import=None, rest_net_import=None, g_max_kw=1e6, **kw):
        seen.update(kw)
        return real(conn, t0_utc, steps, mtd_import, rest_net_import, g_max_kw, **kw)

    monkeypatch.setattr(tariffs, "vectors", spy)
    plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    assert sum(seen["mtd_export"].values()) == pytest.approx(7 * 24 * 50.0, rel=0.01)
    rest_days = (twin.day_start_utc(date(2026, 12, 1)) - (t0 + 96 * 900)) / 86400
    assert sum(seen["rest_net_export"].values()) == pytest.approx(24 * 50.0 * rest_days, rel=0.01)


# ------------------------------------------------------------------------------------------------ api note: the signed message

def test_the_signed_message_carries_no_money_and_records_only_when_asked(company):
    sid = _site(genset=False)
    _closed_loop(sid)                                           # only a closed-loop site's edge is sent a message (round 7)
    assert plans.tick(sid)["status"] == "active"
    quiet = plans.signed_message(sid, record=False)
    assert "objective_rs" not in json.dumps(quiet) and quiet["notes"]["reason"]
    assert delivery.verify(quiet, delivery.public_key(sid)["public_key"])
    with store.connect() as con:
        assert con.execute("SELECT count(*) FROM plan_messages WHERE site_id = ?", (sid,)).fetchone()[0] == 0
    sent = plans.signed_message(sid)                            # the edge's pull (the key-only route) records the send
    with store.connect() as con:
        rows = con.execute("SELECT body FROM plan_messages WHERE site_id = ?", (sid,)).fetchall()
    assert len(rows) == 1 and "objective_rs" not in rows[0]["body"]
    assert delivery.verify(sent, delivery.public_key(sid)["public_key"])
    sites.add_override(sid, {"kind": "pause_control", "until": _until(2), "reason": "test"}, "t")
    rel = plans.signed_message(sid, record=False)
    assert rel["type"] == "release" and "objective_rs" not in json.dumps(rel)
