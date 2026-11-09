"""Regressions for the optimiser group's review findings (overrides, the relaxed retry, carried genset state, AMF sites, the reserve floor shipped
to the edge, the charge-source flags, soc_threshold control, the ICP per-day rate, the wrong-plan guard and the reason rules)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import delivery, forecast, optimise, plans, sites, store, telemetry, twin
from gridlytics.schemas import check

PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _problem(T=96, peak=(72, 88), genset=False, outage=None, battery=True):
    ts = 1794000000 // 900 * 900 + 900 * np.arange(T)
    c_imp = np.full(T, 28.0)
    pk = np.zeros(T, dtype=bool)
    pk[peak[0]:peak[1]] = True
    c_imp[pk] = 60.0
    g = np.ones(T)
    if outage:
        g[outage[0]:outage[1]] = 0
    p = {"T": T, "ts": ts, "L": np.full(T, 80.0), "Lhi": np.full(T, 90.0), "Lc": np.full(T, 30.0), "PV": np.zeros(T), "PVlo": np.zeros(T), "g": g,
         "p_start": np.zeros(T), "dur_steps": np.full(T, 8), "c_imp": c_imp, "c_exp": np.zeros(T), "peak": pk, "in_month": np.ones(T, dtype=bool),
         "G_max": 400.0, "X_max": 0.0, "off_grid": False, "load_mode": 0, "voll": 500.0, "c_short": 500.0, "icp": {}}
    if battery:
        p["battery"] = {"E_nom": 200.0, "E0": 100.0, "sigma0": 0.0, "P_ch": 100.0, "P_dis": 100.0, "eta_c": 0.95, "eta_d": 0.95, "soc_min": 0.1,
                        "soc_max": 0.95, "a_grid": True, "a_gen": False, "c_cyc": 5.0, "b_cal": np.zeros(T), "charge_block": np.zeros(T, dtype=bool)}
        p["terminal"] = {"c_next": 28.0, "H_peak": 4.0, "R_next": 0.0, "c_cal_hold": 0.0}
    if genset:
        p["genset"] = {"S": 100.0, "f_min": 0.3, "a": 0.273, "b": 0.033, "c_fuel": 380.0, "c_start": 1500.0, "c_runhr": 400.0, "c_om": 8.0, "UT": 4, "DT": 2,
                       "u_prev": 0, "elapsed": 99, "cap_hours": 24.0, "hours_today": 0.0, "day_index": np.zeros(T, dtype=int), "fuel_available": 1e9,
                       "parallel": False, "control": "manual", "hold": np.zeros(T, dtype=bool)}
        p["c_short"] = optimise._gen_marginal(p["genset"])
    return p


def _genset_invariants(p, r):
    s = r["sol"]
    gen = p["genset"]
    on = s["u"] > 0.5
    assert np.all(s["P"][on] >= gen["f_min"] * gen["S"] - 1e-4), "genset below its minimum load while on"
    assert np.all(s["P"][~on] < 1e-4), "genset output while off"
    assert np.all(s["G"][np.asarray(p["g"]) < 0.5] < 1e-6), "import during an outage"


def _site(genset_control="manual", genset=True, **extra):
    s = sites.create({"name": "Mill", "disco": "FESCO", "critical_load_kw": [50.0] * 24, "genset_start_control": genset_control, **extra}, "t")
    sid = s["id"]
    sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 200, "kw_ac": 180}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    if genset:
        sites.put_asset(sid, "genset", {"rated_kw": 150}, "t")
    return sid


def _until(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _now_t0() -> int:
    return forecast.align(datetime.now(timezone.utc).timestamp())


# ------------------------------------------------------------------------------------------------ data-override-pause-native-noop

def test_a_pause_holds_the_plan_and_the_edge_gets_a_release_not_the_slots(company):
    sid = _site()
    first = plans.tick(sid)
    assert first["status"] == "active"
    sites.add_override(sid, {"kind": "pause_control", "until": _until(24), "reason": "test"}, "t")
    r = plans.tick(sid)
    assert r["status"] == "held"
    with store.connect() as con:
        row = con.execute("SELECT status, reason FROM plans WHERE id = ?", (r["plan_id"],)).fetchone()
    assert (row["status"], row["reason"]) == ("held", "control_paused")
    assert plans.current(sid) is None  # nothing stays in force while control is paused
    msg = plans.signed_message(sid)
    assert msg["type"] == "release" and msg["release"]["reason"] == "control_paused"
    assert msg["mode_a"]["days"] == [] and msg["mode_b"] is None
    assert delivery.verify(msg, delivery.public_key(sid)["public_key"])


def test_release_to_native_holds_the_plan_without_relabelling_its_steps(company):
    sid = _site()
    sites.add_override(sid, {"kind": "force_native", "until": _until(4), "reason": "test"}, "t")
    r = plans.tick(sid)
    assert r["status"] == "held" and r["summary"]["control_release"]["reason"] == "released_to_native"
    assert not any(s["reason"] == "operator_hold" for s in r["steps"])
    assert plans.signed_message(sid)["type"] == "release"


def test_force_charge_is_a_floor_inside_its_window(company):
    sid = _site(genset=False)
    sites.add_override(sid, {"kind": "force_charge", "value": 90, "until": _until(2), "reason": "test"}, "t")
    full = sites.full(sid)
    t0 = _now_t0()
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    fl = np.broadcast_to(np.asarray(p.get("reserve_manual", 0.0), dtype=float), (96,))
    assert fl[0] == pytest.approx(0.9 * 200)
    assert 7 <= int((fl > 0).sum()) <= 9 and not fl[12:].any()
    assert p["override_mask"][0] and not p["override_mask"][12:].any()
    res = optimise.solve(p)
    assert res["status"] == "optimal"
    E = res["sol"]["E"]
    assert E[1] > p["battery"]["E0"] + 1.0  # it charges towards the target at once


# ------------------------------------------------------------------------------------------------ data-reserve-override-whole-horizon

def test_a_one_hour_hold_floors_only_its_own_hour(company):
    sid = _site(genset=False)
    sites.add_override(sid, {"kind": "reserve", "value": 90, "until": _until(1), "reason": "test"}, "t")
    full = sites.full(sid)
    t0 = _now_t0()
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    fl = np.asarray(p["reserve_manual"], dtype=float)
    assert fl.shape == (96,)
    assert fl[0] == pytest.approx(180.0) and 3 <= int((fl > 0).sum()) <= 5 and not fl[8:].any()


def test_a_short_manual_reserve_leaves_the_evening_peak_alone():
    base = _problem()
    base["battery"]["E0"] = 190.0
    free = optimise.solve(base)
    held = _problem()
    held["battery"]["E0"] = 190.0
    floor = np.zeros(96)
    floor[:4] = 180.0
    held["reserve_manual"] = floor
    r = optimise.solve(held)
    assert r["status"] == "optimal"
    pk = held["peak"]
    assert r["sol"]["D"][pk].sum() == pytest.approx(free["sol"]["D"][pk].sum(), abs=1.0)
    assert r["sol"]["E"][1:5].min() >= 180.0 - 1e-6


# ------------------------------------------------------------------------------------------------ optimiser-relaxed-retry-crashes-tick

def test_a_relaxed_retry_publishes_a_24_hour_plan_instead_of_crashing(company, monkeypatch):
    sid = _site()
    real = optimise.solve

    def first_fails(p, time_limit=10.0, gap=1e-3, relaxed=False):
        if not relaxed:
            return {"status": "not_solved", "solver_status": 2, "message": "forced by the test", "relaxed": False, "notes": [], "config_hash": "x"}
        return real(p, time_limit, gap, relaxed)

    monkeypatch.setattr(optimise, "solve", first_fails)
    r = plans.tick(sid)
    assert r["status"] in ("active", "held")
    assert len(r["steps"]) == 96 and "relaxed_retry" in r["summary"]["notes"]
    with store.connect() as con:
        row = con.execute("SELECT belief_time, valid_until FROM plans WHERE id = ?", (r["plan_id"],)).fetchone()
    span = datetime.fromisoformat(row["valid_until"]) - datetime.fromisoformat(row["belief_time"])
    assert span == timedelta(hours=24)


def test_shorten_never_lengthens_a_problem():
    p = _problem(T=48)
    q = optimise.shorten(p, 96)
    assert q["T"] == 48 and len(q["L"]) == 48


# ------------------------------------------------------------------------------------------------ optimiser-amf-forced-genset-infeasible

def test_an_amf_set_forced_on_at_light_load_is_feasible():
    p = _problem(genset=True, outage=(0, 16))
    p["L"][:] = 84.0
    p["Lc"][:] = 40.0
    p["load_mode"] = 1
    p["genset"].update({"S": 640.0, "f_min": 0.25, "UT": 2, "DT": 2, "control": "auto_on_grid_loss", "u_prev": 1, "elapsed": 99})
    p["battery"].update({"E_nom": 100.0, "E0": 95.0, "P_ch": 50.0, "P_dis": 50.0})
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    _genset_invariants(p, r)
    assert np.all(r["sol"]["u"][:16] == 1)
    assert r["sol"]["U"].sum() < 1e-6


# ------------------------------------------------------------------------------------------------ optimiser-manual-reserve-whole-horizon-and-outages

def test_the_manual_reserve_is_used_in_the_outage_it_was_set_for():
    p = _problem(outage=(20, 28))
    p["battery"]["E0"] = 160.0
    p["reserve_manual"] = 160.0  # the scalar form, limited by its mask, as an older caller writes it
    mask = np.zeros(96, dtype=bool)
    mask[:28] = True
    p["reserve_manual_mask"] = mask
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert r["sol"]["U"].sum() < 1e-6  # the reserve carries the critical load
    assert r["sol"]["E"][28] < 160.0 - 10.0
    assert set(r["manual_floor"]) <= set(range(20))  # rows only where the grid is up and inside the window


# ------------------------------------------------------------------------------------------------ optimiser-operator-hold-blocks-grid-charging

def test_grid_charging_under_an_operator_hold_compiles_as_grid_charging():
    p = _problem()
    p["battery"]["E0"] = 40.0
    window = np.zeros(96, dtype=bool)
    window[:24] = True
    p["reserve_manual"] = 160.0  # a night-time hold at 80 percent
    p["reserve_manual_mask"] = window
    p["override_mask"] = window
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    steps = plans._steps_out(p, r, {"outage": {"p_down": np.zeros(96)}})
    charging = [i for i, s in enumerate(steps[:24]) if s["charge_kw"] > 0.1]
    assert charging and all(steps[i]["reason"] == "operator_hold" for i in charging)
    intents = delivery.step_intents(steps, p["battery"])
    assert all(intents[i]["mode"] == "charge_grid" for i in charging)
    mb = delivery.mode_b(steps)["steps"]
    assert all(mb[i]["grid_charge_allowed"] for i in charging)


# ------------------------------------------------------------------------------------------------ optimiser-relaxed-retry-fractional-genset

def test_the_relaxed_retry_keeps_the_genset_binary():
    p = _problem(genset=True, battery=False, outage=(0, 16))
    p["Lc"][:] = 20.0  # under the set's 30 kW minimum
    r = optimise.solve(p, relaxed=True)
    assert r["status"] != "not_solved"
    assert r["binaries"] >= 2 * 96
    _genset_invariants(p, r)


# ------------------------------------------------------------------------------------------------ optimiser-carried-state-infeasible

def test_a_used_up_run_cap_wins_over_the_carried_minimum_up_time():
    p = _problem(genset=True, outage=(0, 16))
    p["genset"].update({"u_prev": 1, "elapsed": 1, "cap_hours": 8.0, "hours_today": 8.0})
    r = optimise.solve(p)
    assert r["status"] == "optimal" and "genset_state_overridden" in r["notes"]
    assert r["sol"]["u"].sum() == 0
    _genset_invariants(p, r)


def test_an_empty_tank_wins_over_the_carried_minimum_up_time():
    p = _problem(genset=True, outage=(0, 16))
    p["genset"].update({"u_prev": 1, "elapsed": 0, "fuel_available": 2.0})
    r = optimise.solve(p)
    assert r["status"] == "optimal" and "genset_state_overridden" in r["notes"]
    _genset_invariants(p, r)


@pytest.mark.parametrize("windows, ut, dt", [([(0, 3)], 4, 2), ([(40, 43)], 4, 2), ([(0, 2), (3, 11)], 2, 2)])
def test_panel_fixings_at_an_amf_site_are_never_infeasible(windows, ut, dt):
    p = _problem(genset=True)
    for a, b in windows:
        p["g"][a:b] = 0
    p["genset"].update({"control": "auto_on_grid_loss", "UT": ut, "DT": dt, "u_prev": 0, "elapsed": 99})
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    _genset_invariants(p, r)
    assert np.all(r["sol"]["u"][p["g"] < 0.5] == 1)


# ------------------------------------------------------------------------------------------------ optimiser-carried-genset-elapsed-gaps

def _gen_rows(sid, t0, minutes, step=1, skip=()):
    rows = [(sid, int(t0 - 60 * k), "test", 1.0) for k in range(minutes, 0, -step) if k not in skip]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "genset_running"))


@pytest.mark.parametrize("step, skip", [(1, (1, 2)), (1, (20,)), (5, ())])
def test_the_carried_run_survives_short_gaps_in_the_genset_signal(company, step, skip):
    sid = _site()
    t0 = twin.day_start_utc(date(2026, 11, 2)) + 20 * 3600  # 20:00 local; the run started at 10:00
    _gen_rows(sid, t0, 600, step, skip)
    c = plans._carried_genset(sid, t0)
    assert c["u_prev"] == 1 and c["elapsed"] == 40
    assert c["hours_today"] == pytest.approx(10.0, abs=0.1)


# ------------------------------------------------------------------------------------------------ optimiser-reserve-pct-false-floor

def test_the_reserve_floor_is_shipped_only_where_the_plan_holds_one():
    p = _problem()
    p["battery"]["sigma0"] = 0.05
    p["battery"]["E0"] = 190.0
    r = optimise.solve(p)
    steps = plans._steps_out(p, r, {"outage": {"p_down": np.zeros(96)}})
    assert {s["reserve_pct"] for s in steps} == {10.0}
    p2 = _problem()
    p2["battery"]["sigma0"] = 0.15
    p2["p_start"][:] = 0.3
    p2["dur_steps"][:] = 96
    p2["Lc"][:] = 80.0
    r2 = optimise.solve(p2)
    steps2 = plans._steps_out(p2, r2, {"outage": {"p_down": np.zeros(96)}})
    assert max(s["reserve_pct"] for s in steps2) <= 95.0
    for s, b in zip(steps2, delivery.mode_b(steps2)["steps"]):
        assert b["soc_floor_pct"] <= s["soc_pct"] + 1e-9


# ------------------------------------------------------------------------------------------------ optimiser-gen-charge-flag-unsettable

def test_the_charge_source_flags_are_site_fields_and_reach_the_battery(company):
    check(sites.SiteFields, {"name": "x", "gen_charge_allowed": True, "grid_charge_allowed": False})
    t0 = _now_t0()
    sid = _site(gen_charge_allowed=False, grid_charge_allowed=False)
    full = sites.full(sid)
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    assert p["battery"]["a_grid"] is False and p["battery"]["a_gen"] is False
    sid2 = _site()
    full2 = sites.full(sid2)
    p2 = plans.build_problem(full2, t0, forecast.issue(full2, t0, save=False), 96)
    # the flag is stored, but no brand can be told to charge from the genset (delivery.GEN_CHARGE_SUPPORTED), so the plan never counts on it
    assert full2["data"].get("gen_charge_allowed", True) is True and p2["battery"]["a_gen"] is False


# ------------------------------------------------------------------------------------------------ optimiser-soc-threshold-unmodelled

def test_a_soc_threshold_set_runs_whenever_the_battery_is_under_its_threshold():
    p = _problem(genset=True, outage=(8, 32))
    p["battery"]["E0"] = 120.0
    p["genset"].update({"control": "soc_threshold", "soc_on": 0.25})
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    _genset_invariants(p, r)
    E, u = r["sol"]["E"], r["sol"]["u"]
    under = [t for t in range(9, 32) if E[t] < 0.25 * 200 - 1e-3]
    assert under and all(u[t] == 1 for t in under)


# ------------------------------------------------------------------------------------------------ optimiser-icp-rest-per-day-divisor

def test_the_icp_per_day_rate_divides_by_the_days_covered(company):
    sid = _site()
    conn = sites.full(sid)["connections"][0]
    t0 = twin.day_start_utc(date(2026, 11, 20))
    rows = [(sid, int(t), "test", 1.0, 100.0 * 1000 / 60) for t in range(t0 - 2 * 86400, t0, 60)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "grid_import_wh"))
    m = plans.month_to_date(sid, conn, t0)
    assert m["per_day"]["peak"] + m["per_day"]["off_peak"] == pytest.approx(2400.0, rel=0.01)


# ------------------------------------------------------------------------------------------------ optimiser-guard-holds-below-floor-start

def test_a_battery_starting_below_the_floor_is_planned_not_held(company):
    sid = _site(genset=False)
    sites.put_state(sid, soc=0.05)
    r = plans.tick(sid)
    assert r["summary"]["guard"] is None and r["status"] == "active"


# ------------------------------------------------------------------------------------------------ optimiser-explain-contradicts-flows

def test_a_running_parallel_set_is_never_named_genset_off():
    p = _problem(genset=True)
    p["genset"].update({"parallel": True, "c_fuel": 5.0, "c_runhr": 0.0, "c_start": 0.0})
    p["p_start"][:] = 0.25
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    P = r["sol"]["P"]
    running = [t for t in range(96) if P[t] > 0.1]
    assert running
    assert not any(r["reasons"][t]["reason"] in ("genset_off", "idle", "terminal_value", "wear_margin_too_small") for t in running)
    assert any(r["reasons"][t]["reason"] == "genset_on" for t in running)


def test_charging_in_an_outage_is_not_named_idle():
    p = _problem(outage=(0, 96))
    p["off_grid"] = True
    p["G_max"] = 0.0
    p["PV"][32:64] = 200.0
    p["PVlo"][32:64] = 150.0
    p["battery"]["E0"] = 40.0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    charging = [t for t in range(96) if r["sol"]["C"][t] > 0.1]
    assert charging and all(r["reasons"][t]["reason"] == "charge_from_pv" for t in charging)
