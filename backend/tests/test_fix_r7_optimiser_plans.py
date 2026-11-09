"""Regressions for round 7's audit findings on the optimiser and plan building (work package optimiser-plans): no load shed with the grid up, the
plan's own MDI cap per step, grid charging in the peak window and the reason rules."""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from gridlytics import forecast, optimise, plans, sites, store, telemetry, twin

H = 0.25


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _problem(T=96, peak=(72, 88), outage=None, battery=True):
    """A grid-tied problem in the shape build_problem writes (as test_fix_optimiser._problem): 80 kW of load, Rs 28 off-peak and Rs 60 in the peak."""
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
    return p


def _mdi(p, M0=100.0, rate=1250.0):
    p["mdi"] = {"rate": rate, "M0": M0, "floor": 0.0, "n_mdi": 2, "G_elapsed": 0.0, "n_elapsed": 0, "month_end_step": p["T"]}
    return p


def _site(**extra):
    s = sites.create({"name": "Mill", "disco": "FESCO", "critical_load_kw": [50.0] * 24, "genset_start_control": "none", **extra}, "t")
    sid = s["id"]
    sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 200, "kw_ac": 180}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    return sid


# ------------------------------------------------------------------------------------------------ engine-optimiser-01, engine-plans-live-01

def test_the_plan_never_sheds_load_with_the_grid_up_to_hold_its_mdi():
    """The audit's reproduction: a 300 kW hour against a 100 kW month-to-date peak and a nearly empty battery. Shedding a kW for one 30-minute
    block at VoLL (Rs 250) was cheaper than raising M (Rs 1,250/kW), so the plan shed real load with the grid up and named the steps
    self_consumption or mdi_cap. A grid-connected site cannot shed on command: the plan imports and prices the MDI rise."""
    p = _mdi(_problem())
    p["L"] = p["L"].copy()
    p["L"][40:44] = 300.0
    p["Lhi"] = np.maximum(p["Lhi"], p["L"])
    p["battery"]["E0"] = 25.0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    up = np.asarray(p["g"]) > 0.5
    assert float(np.max(r["sol"]["U"][up])) < 1e-6
    assert r["breakdown"]["unserved_rs"] == pytest.approx(0.0, abs=1e-6)
    # the rise is priced: M carries the 300 kW hour less what the battery's 100 kW can shave off it
    assert r["M"][0] >= 300.0 - 100.0 - 1e-6
    assert r["breakdown"]["mdi_rise_rs"] == pytest.approx(1250.0 * (r["M"][0] - 100.0), abs=0.5)
    assert all(x["reason"] != "unserved" for x in r["reasons"])


def test_a_voll_of_zero_never_sheds_with_the_grid_up():
    """VoLL accepts 0: with VoLL under the import price the plan shed every grid-up step instead of importing."""
    p = _problem(battery=False)
    p["voll"] = 0.0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert float(np.max(r["sol"]["U"])) < 1e-6
    assert np.allclose(r["sol"]["G"], p["L"])


def test_only_load_beyond_the_connection_and_pv_is_unserved_with_the_grid_up_and_it_is_named():
    """Load above what the connection (G_max) and PV can carry, with the battery empty, is the one shed a grid-up step can have; the step says so."""
    p = _problem()
    p["L"] = p["L"].copy()
    p["L"][0] = 450.0
    p["Lhi"] = np.maximum(p["Lhi"], p["L"])
    p["battery"]["E0"] = 20.0                         # at soc_min, at the first step: nothing to discharge
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert r["sol"]["U"][0] == pytest.approx(50.0, abs=1e-4)
    assert float(np.max(r["sol"]["U"][1:])) < 1e-6
    assert r["reasons"][0]["reason"] == "unserved"
    assert r["reasons"][0]["detail"]["grid"] == "up" and r["reasons"][0]["detail"]["kw"] == pytest.approx(50.0)


def test_the_problem_values_lost_load_at_least_at_the_dearest_import_price(company):
    """A site saved with a VoLL under the import price: the problem raises it to the horizon's dearest import price, so a manual floor's slack and
    an outage's shortfall are never cheaper than the grid."""
    sid = _site(voll_rs_per_kwh=0.0)
    full = sites.full(sid)
    t0 = twin.day_start_utc(date(2026, 11, 3))
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    assert p["voll"] == pytest.approx(float(np.max(p["c_imp"])))
    assert p["voll"] > 0
    sites_default = _site()
    q = plans.build_problem(sites.full(sites_default), t0, forecast.issue(sites.full(sites_default), t0, save=False), 96)
    assert q["voll"] == pytest.approx(500.0)          # the default, already above every price, is kept


def test_a_ticked_plan_never_sheds_with_the_grid_up_nor_imports_above_its_own_cap(company):
    """The tick end to end, as the wrong-plan guard reads a plan (Part 7.1 step 8: no import above the plan's own MDI cap; the guard is the
    control-edge package's): a mill whose load jumps from 100 to 300 kW for an hour each afternoon, with the month's peak so far 100 kW. Before,
    the plan shed four of those steps with the grid up (Rs 95,880 of "unserved" load) and 25 steps imported above its own cap."""
    sid = _site(load_peak_kw=300)
    t0 = twin.day_start_utc(date(2026, 11, 1))
    rows = []
    for t in range(twin.day_start_utc(date(2026, 10, 18)), t0 + 13 * 3600, 60):
        load = 300.0 if 15 <= ((t + 5 * 3600) % 86400) / 3600 < 16 else 100.0
        rows.append((sid, t, "test", 1.0, load * 1000 / 60, 0.0, 0.0, load * 1000))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "grid_export_wh", "pv_power_w", "load_power_w"))
    out = plans.tick(sid, at=t0 + 13 * 3600, save=False)
    assert out["status"] == "active"
    up = [x for x in out["steps"] if x["grid_present"] > 0.5]
    assert max(x["load_kw"] for x in up) >= 250.0                       # the forecast carries the afternoon hour
    assert all(x["unserved_kw"] < 0.01 for x in up)
    assert out["summary"]["breakdown"]["unserved_rs"] == pytest.approx(0.0, abs=0.01)
    cap = out["summary"]["mdi_cap_kw"]
    assert all(x["grid_import_kw"] <= cap + 0.05 for x in up)


# ------------------------------------------------------------------------------------------------ engine-plans-live-17

def _sawtooth(x, on=0.1):
    """Steps that do nothing between two steps that act (0 / full / 0 / full): the alternation the 30-minute rows left free."""
    act = np.asarray(x) > on
    return [t for t in range(1, len(act) - 1) if act[t - 1] and not act[t] and act[t + 1]]


def test_each_steps_import_stays_under_the_plans_own_mdi_cap_and_grid_charging_is_smooth():
    """Mode A slots carry grid_cap_w = M and the device shaves every instant above it, so a step importing above the plan's cap (the 30-minute
    rows held only the block's mean) throttles the charging the plan counted on: Karachi's charge ran 65.6 / 0 / 188.6 / 0 / 118.1 kW, 962 kW
    against a 902 kW cap."""
    p = _mdi(_problem())
    p["battery"]["E0"] = 20.0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    G, M = r["sol"]["G"], r["M"][0]
    extra = np.maximum(0.0, np.asarray(p["Lhi"]) - np.asarray(p["L"]))
    assert float(np.max(G - M)) <= 1e-6                 # the plan's own import, step by step
    assert float(np.max(G + extra - M)) <= 1e-6         # and under the upper load quantile, as the 30-minute rows already were
    assert not _sawtooth(r["sol"]["C"][:72])


def test_a_peak_discharge_held_to_the_cap_under_the_upper_load_does_not_alternate():
    """The AUDIT site: 18:00 Peak discharge, 18:15 Wear > saving, 18:30 Peak discharge, all at Rs 40.02. The 30-minute row under the upper load
    quantile let one step of each block discharge for both."""
    p = _mdi(_problem(), M0=140.0)
    p["L"], p["Lhi"] = p["L"].copy(), p["Lhi"].copy()
    p["L"][72:88], p["Lhi"][72:88] = 150.0, 175.0
    p["c_imp"] = np.full(p["T"], 40.0)                 # one price all day: only the cap decides when the battery works
    p["battery"]["E0"] = 190.0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    G, M = r["sol"]["G"], r["M"][0]
    extra = np.maximum(0.0, np.asarray(p["Lhi"]) - np.asarray(p["L"]))
    assert float(np.max(G + extra - M)) <= 1e-6
    assert not _sawtooth(r["sol"]["D"])


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-02, engine-plans-live-04, -13

def _peak_start(p_peak: float, T=96, E0=20.0):
    """A plan made at the start of the peak window (Rs 60 for four hours, then Rs 28) with an empty battery: the only way to refill the outage
    reserve before the off-peak hours is the grid at the peak price."""
    p = _problem(T=T, peak=(0, 16))
    p["battery"]["E0"] = E0
    p["p_start"] = np.where(np.asarray(p["peak"]), p_peak, 0.0)
    p["c_short"] = 130.0                                  # a genset site's marginal cost of a kWh short
    return p


def test_grid_charging_in_the_peak_for_the_reserve_is_named_for_the_reserve_and_never_cheaper():
    """Faisalabad charged 92 kWh at Rs 40.02 in the peak with the reason "Charging from the grid while energy is cheaper". The reserve rule's
    expected shortfall pays for it, so the plan may do it, but the step names the reserve, its chance of an outage and the price paid."""
    p = _peak_start(0.05)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    s, pk = r["sol"], np.asarray(p["peak"])
    grid_charge = [t for t in range(p["T"]) if pk[t] and s["C"][t] > 0.1 and s["G"][t] > 0.1]
    assert grid_charge, "the reserve at p = 0.05 a step is worth buying at the peak price: the case needs a peak grid charge"
    for t in grid_charge:
        x = r["reasons"][t]
        assert x["reason"] == "charge_for_reserve", (t, x)
        assert x["detail"]["rs_per_kwh"] == pytest.approx(60.0)
        # the chance of an outage starting while the charge is held for it: every step of the window, not one step's 5 %
        n = round(x["detail"]["within_h"] / H)
        assert x["detail"]["p"] == pytest.approx(1 - (1 - 0.05) ** n, abs=0.006)
        assert x["detail"]["p"] > 0.05
        assert 0 < x["detail"]["kwh"] <= (0.95 - 0.1) * 200.0
    assert not any(x["reason"] == "charge_from_grid" for t, x in enumerate(r["reasons"]) if pk[t])


def test_no_grid_charging_in_the_peak_without_a_reserve_row():
    """Below p = 0.01 a step has no reserve row, and nothing else makes peak energy worth buying: the refill waits for the off-peak price."""
    p = _peak_start(0.005)
    r = optimise.solve(p)
    pk = np.asarray(p["peak"])
    assert float(np.max(r["sol"]["C"][pk])) < 0.1


def test_off_peak_grid_charging_names_the_price_and_the_time_the_energy_is_used_at():
    p = _problem()
    p["battery"]["E0"] = 20.0
    r = optimise.solve(p)
    s = r["sol"]
    charges = [t for t in range(72) if s["C"][t] > 0.1 and s["G"][t] > 0.1]
    assert charges
    first_use = next(t for t in range(p["T"]) if s["D"][t] > 0.1)
    assert first_use == 72
    for t in charges:
        x = r["reasons"][t]
        assert x["reason"] == "charge_from_grid"
        assert x["detail"]["rs_per_kwh"] == pytest.approx(28.0)
        assert x["detail"]["use_t"] == int(p["ts"][72]) and x["detail"]["use_rs_per_kwh"] == pytest.approx(60.0)


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-08, engine-plans-live-14

def _held_for_the_reserve(p_steps):
    """Faisalabad and Lahore: a full battery at the start of the peak, a reserve (sigma at its cap and a heavy critical load) above the usable band,
    and reserve rows only where p_start reaches 0.01."""
    p = _problem(peak=(0, 16))
    p["battery"]["E0"] = 190.0
    p["battery"]["sigma0"] = 0.15
    p["Lc"] = np.full(p["T"], 60.0)
    p["p_start"] = np.zeros(p["T"])
    p["p_start"][:16] = p_steps
    p["c_short"] = 130.0
    return p


def test_a_battery_held_through_the_peak_for_the_reserve_says_so_on_every_step_without_flipping():
    """Lahore flipped between "Wear > saving" and "Hold for outage" four times in one peak with the state of charge fixed at 95 %, and Faisalabad
    named 18:00 to 18:30 "Wear > saving" while the battery was held for the reserve rows from 18:45: a step whose own p_start is under 0.01 has no
    row, but discharging there empties the battery the later rows hold."""
    p = _held_for_the_reserve([0.005, 0.005, 0.005] + [0.05, 0.005] * 6 + [0.05])
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    s = r["sol"]
    held = [t for t in range(16) if s["D"][t] <= 0.1 and s["C"][t] <= 0.1]
    assert held[:3] == [0, 1, 2]
    first_dis = next((t for t in range(16) if s["D"][t] > 0.1), 16)
    for t in range(first_dis):
        x = r["reasons"][t]
        assert x["reason"] == "hold_for_outage", (t, x)
        En, Emin = 200.0, 20.0
        assert x["detail"]["kwh"] <= float(s["E"][t + 1]) - Emin + 1e-6      # never more than the battery holds (184.8 against 182.75)
        assert x["detail"]["kwh"] <= (0.95 - 0.1) * En + 1e-6
        n = round(x["detail"]["within_h"] / H)
        assert x["detail"]["p"] == pytest.approx(1 - float(np.prod(1 - p["p_start"][t:t + n])), abs=0.006)
    assert not any(x["reason"] == "wear_margin_too_small" for x in r["reasons"][:first_dis])


def test_a_peak_step_whose_energy_is_spent_later_says_when_not_wear():
    """Karachi said "Wear > saving" at 13.4 % right after discharging 250 kW at the same price: the 17 kWh left was spent on MDI-cap discharges the
    next morning. Here the battery cannot recharge (no grid charging, no PV) and keeps its energy for a dearer peak."""
    p = _problem()
    p["c_imp"] = p["c_imp"].copy()
    p["c_imp"][8:24] = 50.0
    p["peak"] = p["peak"].copy()
    p["peak"][8:24] = True
    p["battery"]["a_grid"] = False
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    s = r["sol"]
    use = next(t for t in range(p["T"]) if s["D"][t] > 0.1)
    assert float(np.max(s["D"][8:24])) < 0.1 and 72 <= use < 88
    for t in range(8, 24):
        x = r["reasons"][t]
        assert x["reason"] == "save_for_later", (t, x)
        assert x["detail"]["use_t"] == int(p["ts"][use]) and x["detail"]["use_rs_per_kwh"] == pytest.approx(60.0)
        assert x["detail"]["use_reason"] == "discharge_peak_window"


def _solution(T, E, C=None, D=None, G=None):
    z = np.zeros(T)
    return {"G": z.copy() if G is None else np.asarray(G, float), "X": z.copy(), "K": z.copy(), "U": z.copy(), "P": z.copy(), "F": z.copy(),
            "C": z.copy() if C is None else np.asarray(C, float), "D": z.copy() if D is None else np.asarray(D, float), "u": z.copy(), "v": z.copy(),
            "E": np.asarray(E, float), "W": z.copy()}


def test_wear_is_named_only_where_a_positive_price_gap_is_smaller_than_the_wear_cost():
    """Rs 48 now against a refill at Rs 40 a few hours later: Rs 48 - 40 / (0.95 x 0.95) = Rs 3.68 a kWh, under the Rs 5 wear. At the refill's
    own price there is no gap at all: nothing to gain, not "the gap is smaller than the wear cost"."""
    T = 8
    p = _problem(T=T, peak=(0, 4))
    p["c_imp"] = np.where(np.asarray(p["peak"]), 48.0, 40.0)
    p["L"] = np.full(T, 80.0)
    E = [150.0] * 6 + [159.5] * 3                    # held, then a grid charge at step 5
    s = _solution(T, E, C=[0, 0, 0, 0, 0, 40.0, 0, 0], G=[80, 80, 80, 80, 80, 120, 80, 80])
    out = {"R": np.zeros(T), "R_steps": np.full(T, 8), "r": {}, "M": [], "v_end": optimise.solve(p)["v_end"]}
    reasons = optimise.explain(p, s, out)
    for t in range(4):
        assert reasons[t]["reason"] == "wear_margin_too_small", (t, reasons[t])
        assert reasons[t]["detail"]["gap_rs_per_kwh"] == pytest.approx(48.0 - 40.0 / (0.95 * 0.95), abs=0.01)
        assert reasons[t]["detail"]["wear_rs_per_kwh"] == pytest.approx(5.0)
    assert reasons[4]["reason"] == "idle"                # Rs 40 now, Rs 40 at the refill
    assert reasons[5]["reason"] == "charge_from_grid"


def test_an_off_grid_battery_carries_the_load_not_an_outage():
    """Rahim Yar Khan, off-grid: "The grid is down: the battery carries the load" on 45 slots of a site with no grid at all."""
    p = _problem()
    p["off_grid"] = True
    p["g"] = np.zeros(p["T"])
    p["G_max"] = 0.0
    p["PV"] = np.where((np.arange(p["T"]) % 96 >= 28) & (np.arange(p["T"]) % 96 < 64), 250.0, 0.0)
    p["PVlo"] = p["PV"].copy()
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    named = {x["reason"] for x in r["reasons"]}
    assert "carry_outage" not in named
    carried = [t for t in range(p["T"]) if r["sol"]["D"][t] > 0.1 and r["sol"]["U"][t] <= 0.1]  # a step that also sheds says "unserved"
    assert carried and all(r["reasons"][t]["reason"] == "carry_load" for t in carried)
    assert r["reasons"][carried[0]]["detail"]["kw"] == pytest.approx(float(r["sol"]["D"][carried[0]]), abs=0.05)


def _amf(p):
    """An auto-start (AMF) genset: the reserve covers the panel's start delay, and a failed start's two steps."""
    p["genset"] = {"S": 100.0, "f_min": 0.3, "a": 0.273, "b": 0.033, "c_fuel": 380.0, "c_start": 1500.0, "c_runhr": 400.0, "c_om": 8.0, "UT": 4,
                   "DT": 2, "u_prev": 0, "elapsed": 99, "cap_hours": 24.0, "hours_today": 0.0, "day_index": np.zeros(p["T"], dtype=int),
                   "fuel_available": 1e9, "parallel": False, "control": "auto_on_grid_loss", "hold": np.zeros(p["T"], dtype=bool)}
    p["battery"]["sigma0"] = 0.0
    return p


def test_the_reserve_behind_an_amf_panel_says_it_covers_two_steps():
    """Lahore's hold said "hours 1.0" although behind its AMF panel the reserve covers the start delay and a failed start: two steps."""
    p = _amf(_held_for_the_reserve([0.05] * 16))
    p["battery"]["E0"] = 60.0                         # near the 51.6 kWh the two steps' reserve asks for
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    reserve = [x for x in r["reasons"] if x["reason"] in ("hold_for_outage", "charge_for_reserve")]
    assert reserve
    assert all(x["detail"]["hours"] == pytest.approx(0.5) for x in reserve)


def test_a_discharge_at_one_price_is_one_run_not_peak_discharge_and_wear_by_turns():
    """The AUDIT site: 18:00 Peak discharge, 18:15 Wear > saving, 18:30 Peak discharge, all at Rs 40.02. Steps at one price are ties the solver
    split at random; a tie-break of a fraction of a paisa discharges the earlier and charges the later of two equal steps."""
    p = _amf(_held_for_the_reserve([0.05] * 16))      # a full battery, 128 kWh above a 51.6 kWh reserve: the peak's 16 steps tie
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    D = r["sol"]["D"][:16]
    assert float(np.max(D)) > 0.1
    # one run down to the reserve, a hold, and the reserve's release where its last rows are worth less than the price: before, a third run
    # (80 x 5, 6, 0, 0, 0, 80, 0, 0, 0, 0, 80, 80) kept energy between two discharges at the same price for no reason the numbers give
    act = D > 0.1
    assert int(np.sum(act[1:] & ~act[:-1])) + int(act[0]) <= 2
    named = [x["reason"] for t, x in enumerate(r["reasons"][:16]) if not act[t]]
    assert set(named) <= {"hold_for_outage"}


def test_a_step_saved_for_later_compiles_as_a_hold_like_the_no_flow_reasons_it_replaces():
    """A grid-up step that keeps its energy for a later discharge was "Wear > saving" or "idle", both compiled as a hold; the new reason must not
    fall through to self-use, where the device would discharge the energy the plan keeps."""
    from gridlytics import delivery
    step = {"grid_present": 1.0, "discharge_kw": 0.0, "charge_kw": 0.0, "grid_export_kw": 0.0, "load_kw": 80.0, "pv_kw": 0.0, "soc_pct": 60.0,
            "grid_import_kw": 80.0, "reason": "save_for_later"}
    battery = {"a_grid": True}
    assert delivery.step_intents([step], battery)[0]["mode"] == "hold"
    assert delivery.step_intents([{**step, "reason": "wear_margin_too_small"}], battery)[0]["mode"] == "hold"


# ------------------------------------------------------------------------------------------------ engine-delivery-02, flow-telemetry-plan-control-06

def _floored(p, floor=0.2):
    """The floor the delivered table holds with the grid up (build_problem writes it from the BMS low cap and the reserve policy)."""
    p["battery"]["soc_floor_grid"] = floor
    return p


def test_the_plan_holds_the_delivered_floor_on_every_grid_up_step():
    """The feed mill discharged at 250 kW into the peak down to 13.4 % and 10 %, against slots that all stop at 20 %: the plan counted on
    energy the device keeps back. With the grid up the plan holds the table's floor, and the peak it could not shave there is imported."""
    p = _floored(_problem())
    p["battery"]["E0"] = 120.0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    E = r["sol"]["E"]
    assert float(np.min(E[1:])) >= 0.2 * 200.0 - 1e-6
    assert float(np.sum(r["sol"]["D"])) > 1.0                         # it still discharges in the peak, down to 20 %, not 10 %
    assert float(np.min(E[73:89])) == pytest.approx(40.0, abs=0.5)
    # the steps it ships carry the same floor, and so does Mode B
    from gridlytics import delivery
    steps = plans._steps_out(p, r, {"outage": {"p_down": np.zeros(p["T"])}})
    assert min(s["reserve_pct"] for s in steps) >= 20.0
    assert min(b["soc_floor_pct"] for b in delivery.mode_b(steps)["steps"]) >= 20.0 - 1e-9


def test_an_outage_may_use_the_band_down_to_the_bms_cut_off():
    """In an outage the battery-inverter carries the load to its own cut-off (twin.simulate's outage branch), so the delivered floor binds only
    with the grid up."""
    p = _floored(_problem(outage=(40, 56)))
    p["battery"]["E0"] = 120.0
    p["Lc"] = np.full(p["T"], 60.0)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    E = r["sol"]["E"]
    assert float(np.min(E[41:57])) < 0.2 * 200.0 - 1.0              # the outage takes the battery under 20 %
    assert float(np.min(E[41:57])) >= 0.1 * 200.0 - 1e-6


def test_below_the_floor_after_an_outage_the_battery_does_not_discharge_until_it_is_back_at_the_floor():
    """After an outage the battery is below the table's floor: the device then holds (no discharge) until it charges back to the floor, and
    the plan may not count on discharging from there, nor is the state infeasible where it cannot recharge (no grid charging, no sun)."""
    p = _floored(_problem(outage=(30, 31), peak=(48, 64)))
    p["battery"]["E0"] = 45.0
    p["battery"]["a_grid"] = False
    p["Lc"] = np.full(p["T"], 30.0)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    E, D = r["sol"]["E"], r["sol"]["D"]
    assert 20.0 + 1.0 < float(E[31]) < 40.0 - 1.0                     # it came out of the outage between the cut-off and the 20 % floor
    up_after = range(31, p["T"])
    assert all(D[t] < 1e-6 for t in up_after)                         # and with nothing to recharge it, it never discharges again (the peak
    #                                                                   at Rs 60 took it to the 10 % cut-off before)
    steps = plans._steps_out(p, r, {"outage": {"p_down": np.zeros(p["T"])}})
    from gridlytics import delivery
    for s, b in zip(steps, delivery.mode_b(steps)["steps"]):
        assert b["soc_floor_pct"] <= s["soc_pct"] + 1e-9               # Mode B holds what is there, never asks for energy it lacks


def test_a_battery_recharged_to_the_floor_after_an_outage_may_discharge_above_it():
    """With grid charging allowed the battery is recharged off-peak after the outage and discharged in the peak, never under the floor."""
    p = _floored(_problem(outage=(20, 36), peak=(72, 88)))
    p["battery"]["E0"] = 60.0
    p["Lc"] = np.full(p["T"], 60.0)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    E, D = r["sol"]["E"], r["sol"]["D"]
    assert float(E[36]) < 40.0 - 1.0
    assert float(np.sum(D[72:88])) > 1.0
    for t in range(36, p["T"]):
        if D[t] > 1e-6:
            assert float(E[t + 1]) >= 40.0 - 1e-6


def test_a_start_below_the_floor_is_planned_towards_it_and_never_infeasible():
    p = _floored(_problem(peak=(8, 24)))
    p["battery"]["E0"] = 30.0                                          # 15 %: under the 20 % floor, over the 10 % cut-off
    p["battery"]["a_grid"] = False
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert float(np.max(r["sol"]["D"])) < 1e-6                        # nothing recharges it, so it holds what it has
    p["battery"]["a_grid"] = True
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    E, D = r["sol"]["E"], r["sol"]["D"]
    for t in range(p["T"]):
        if D[t] > 1e-6:
            assert float(E[t + 1]) >= 40.0 - 1e-6


def test_the_terminal_credit_counts_only_energy_above_the_floor_and_under_its_cap():
    """Golden: the terminal credit is v_end times E_use, the energy the next peak can use: above the delivered floor (not the cut-off) and never
    above P_dis H_peak + R_next. Before, the breakdown credited every kWh above the cut-off, past the cap."""
    p = _floored(_problem(peak=(0, 0)))
    p["c_imp"] = np.full(p["T"], 28.0)
    p["battery"]["E0"] = 150.0
    p["battery"]["c_cyc"] = 0.0
    p["terminal"] = {"c_next": 60.0, "H_peak": 0.5, "R_next": 0.0, "c_cal_hold": 0.0}
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    v_end = 0.95 * 60.0
    assert r["v_end"] == pytest.approx(v_end)
    cap = 100.0 * 0.5                                                  # P_dis x H_peak
    E_T = float(r["sol"]["E"][-1])
    assert E_T - 40.0 >= cap - 1e-6                                   # it keeps at least the cap above the floor
    assert r["breakdown"]["terminal_credit_rs"] == pytest.approx(-v_end * cap, abs=0.05)
    # and with a battery that ends under the cap, only the energy above the floor counts
    p["terminal"]["H_peak"] = 4.0
    p["battery"]["E0"] = 60.0
    p["battery"]["a_grid"] = False
    r = optimise.solve(p)
    E_T = float(r["sol"]["E"][-1])
    assert r["breakdown"]["terminal_credit_rs"] == pytest.approx(-v_end * max(0.0, E_T - 40.0), abs=0.05)


def test_build_problem_plans_on_the_floor_the_compiled_table_holds(company):
    """The floor the plan holds is the one delivery.compile_mode_a clamps every slot to, max(BMS low + 2, reserve_policy.min_soc_pct), and the
    fallback block's floor never exceeds it."""
    from gridlytics import delivery
    sid = _site()
    full = sites.full(sid)
    t0 = twin.day_start_utc(date(2026, 11, 3))
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    assert p["battery"]["soc_floor_grid"] == pytest.approx(0.20)
    assert plans.delivered_floor_pct(full) == pytest.approx(20.0)
    sites.update(sid, {"reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 5.0}}, "t")
    assert plans.delivered_floor_pct(sites.full(sid)) == pytest.approx(12.0)   # the BMS's 10 % plus 2
    step = {"t": int(t0), "discharge_kw": 50.0, "charge_kw": 0.0, "grid_import_kw": 0.0, "grid_export_kw": 0.0, "load_kw": 80.0, "pv_kw": 0.0,
            "soc_start_pct": 50.0, "soc_pct": 11.0, "reserve_pct": 10.0, "genset_on": False, "reason": "discharge_peak_window", "grid_present": 1.0}
    for floor, bms in ((20.0, 10.0), (5.0, 10.0), (30.0, 25.0)):
        site = {**full, "data": {**full["data"], "reserve_policy": {"p_threshold": 0.2, "min_soc_pct": floor}},
                "assets": {**full["assets"], "battery": {**full["assets"]["battery"], "soc_min": bms / 100.0}}}
        c = delivery.compile_mode_a([step], "deye", {"a_grid": True}, floor, bms)
        assert c["days"][0]["slots"][0]["soc_floor_pct"] == pytest.approx(plans.delivered_floor_pct(site))


def test_a_ticked_plan_never_plans_grid_up_energy_under_the_delivered_floor(company):
    """The feed mill's 40 of 40 plans planned grid-up SoC under 20 %. A ticked plan with the default policy (20 %) and a 10 % BMS cut-off."""
    sid = _site(load_peak_kw=150)
    t0 = twin.day_start_utc(date(2026, 11, 1))
    rows = []
    for t in range(twin.day_start_utc(date(2026, 10, 18)), t0 + 13 * 3600, 60):
        rows.append((sid, t, "test", 1.0, 150.0 * 1000 / 60, 0.0, 0.0, 150.0 * 1000))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "grid_export_wh", "pv_power_w", "load_power_w"))
    out = plans.tick(sid, at=t0 + 13 * 3600, save=False)
    assert out["status"] == "active"
    up = [x for x in out["steps"] if x["grid_present"] > 0.5]
    assert sum(x["discharge_kw"] for x in up) > 1.0
    assert min(x["soc_pct"] for x in up) >= 20.0 - 0.05
    assert min(s["soc_floor_pct"] for d in out["slots"]["days"] for s in d["slots"]) >= 20.0


# ------------------------------------------------------------------------------------------------ flow-onboarding-09 (engine half)

def test_the_dispatch_stays_within_the_inverters_ac_rating():
    """PV to AC plus battery discharge, less charge, stays within the inverter's kW AC both ways: before, a 50 kW inverter passed PV 163 kW and
    a 76 kW charge, and the plan counted on it."""
    q = _problem(peak=(40, 56))
    q["PV"] = np.zeros(q["T"])
    q["PV"][20:40] = 163.0
    q["PVlo"] = np.minimum(q["PVlo"], q["PV"])
    q["inverter_kw_ac"] = 50.0
    q["battery"]["E0"] = 20.0
    rq = optimise.solve(q)
    assert rq["status"] == "optimal"
    s = rq["sol"]
    ac = (q["PV"] - s["K"]) + s["D"] - s["C"]
    assert float(np.max(ac)) <= 50.0 + 1e-6
    assert float(np.min(ac)) >= -50.0 - 1e-6
    assert float(np.max(s["D"][40:56])) <= 50.0 + 1e-6
    assert float(np.max(s["U"])) < 1e-6                                # the grid carries what the inverter cannot


def test_build_problem_reads_the_inverters_ac_rating(company):
    sid = _site()
    full = sites.full(sid)
    t0 = twin.day_start_utc(date(2026, 11, 3))
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    assert p.get("inverter_kw_ac") is None                             # no inverter on record: no limit
    sites.put_asset(sid, "inverter", {"brand": "deye", "kw_ac": 50}, "t")
    full = sites.full(sid)
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    assert p["inverter_kw_ac"] == pytest.approx(50.0)


# ------------------------------------------------------------------------------------------------ engine-optimiser-09

def test_no_reserve_row_on_the_step_before_a_modelled_outage():
    """Part 4.3: the balance rows price a modelled outage (g = 0), so the step before it carries no reserve row as well: before, a scheduled
    window with adherence above 0.9 was paid twice, the reserve's expected shortfall (about 0.9 x c_short) plus the window's own lost load."""
    p = _problem(outage=(40, 48))
    p["Lc"] = np.full(p["T"], 60.0)
    p["p_start"] = np.zeros(p["T"])
    p["p_start"][39] = 0.95                                            # forecast.outage: p_start = adherence on the step before the window
    p["p_start"][20] = 0.05                                            # and an unscheduled chance well before it
    p["battery"]["E0"] = 30.0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert 39 not in r["r"]
    assert 20 in r["r"]
    # the expected shortfall the breakdown reports is only the rows the plan has: step 20's
    assert r["breakdown"]["expected_shortfall_rs"] == pytest.approx(0.05 * p["c_short"] * r["r"][20], abs=0.01)
    # and a step whose next step has the grid up keeps its row
    q = _problem()
    q["p_start"] = np.zeros(q["T"])
    q["p_start"][-2] = 0.2
    q["Lc"] = np.full(q["T"], 60.0)
    rq = optimise.solve(q)
    assert q["T"] - 2 in rq["r"]


# ------------------------------------------------------------------------------------------------ engine-plans-live-06

def _reserve_case(T=96):
    """One grid-up day with a 20 % chance of an outage starting at step 40, lasting four steps, a 180 kW critical load and no sun."""
    p = _problem(T=T)
    p["p_start"] = np.zeros(T)
    p["p_start"][40] = 0.2
    p["dur_steps"] = np.full(T, 4)
    p["Lc"] = np.full(T, 180.0)
    p["L"] = np.full(T, 200.0)
    p["Lhi"] = np.full(T, 210.0)
    return p


def test_the_reserve_counts_only_what_the_battery_can_discharge():
    """Lahore held 115 kWh for a 180 kW critical load on a 50 kW battery: in an hour's outage it can deliver at most 50 kW, so the reserve is
    the sum of min(Lc - PVlo, P_dis) h / eta_d, not the whole critical load's."""
    p = _reserve_case()
    p["battery"]["P_dis"] = 50.0
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert float(r["R"][40]) == pytest.approx(4 * 50.0 * H / 0.95, abs=1e-6)        # 52.6 kWh, not min(170, 189.5)


def test_the_reserve_counts_only_what_the_inverter_can_pass():
    """PV and the battery share the inverter's kW AC: in an outage the battery passes what PV leaves of it."""
    p = _reserve_case()
    p["inverter_kw_ac"] = 60.0
    p["PVlo"] = np.full(p["T"], 20.0)
    p["PV"] = np.full(p["T"], 30.0)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert float(r["R"][40]) == pytest.approx(4 * (60.0 - 20.0) * H / 0.95, abs=1e-6)


# ------------------------------------------------------------------------------------------------ engine-plans-live-03

def _mall(p_start=0.01, T=96):
    """Lahore: a 100 kWh / 50 kW battery, a 180 kW critical load and two sets behind an auto-start (AMF) panel; the A-2(c) peak at Rs 48 for
    the first four hours and a 1 % chance of an outage starting every step."""
    p = _problem(T=T, peak=(0, 16))
    p["c_imp"] = np.where(np.asarray(p["peak"]), 48.28, 28.0)
    p["L"] = np.full(T, 200.0)
    p["Lhi"] = np.full(T, 210.0)
    p["Lc"] = np.full(T, 180.0)
    p["p_start"] = np.full(T, p_start)
    p["dur_steps"] = np.full(T, 4)
    p["battery"].update({"E_nom": 100.0, "E0": 95.0, "P_ch": 50.0, "P_dis": 50.0, "sigma0": 0.0})
    p["genset"] = {"S": 640.0, "f_min": 0.25, "a": 0.273, "b": 0.033, "c_fuel": 280.0, "c_start": 1500.0, "c_runhr": 400.0, "c_om": 8.0, "UT": 2,
                   "DT": 2, "u_prev": 0, "elapsed": 99, "cap_hours": 24.0, "hours_today": 0.0, "day_index": np.zeros(T, dtype=int),
                   "fuel_available": 1e9, "parallel": False, "control": "auto_on_grid_loss", "hold": np.zeros(T, dtype=bool)}
    p["c_short"] = optimise._gen_marginal(p["genset"])
    return p


def test_behind_an_amf_panel_the_battery_is_free_to_shave_the_peak():
    """The mall's plan bought 65 kWh from the grid at Rs 39.48 and held 95 % for 48 hours with no discharge: its reserve was 30 minutes of
    the whole critical load priced at diesel cost. Behind an AMF panel the battery bridges only the start delay, and the rest of a failed start
    (one start in twenty), so the reserve is a few kWh and the battery shaves the peak."""
    p = _mall()
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert r["breakdown"]["discharge_kwh"] > 20.0
    s, pk = r["sol"], np.asarray(p["peak"])
    assert float(np.sum(s["D"][pk])) * H > 20.0
    assert not any(s["C"][t] > 0.1 and s["G"][t] > 0.1 for t in range(p["T"]) if pk[t])     # no grid charging in the peak for the reserve
    # the start delay's reserve: a minute of what the battery can deliver (50 kW), well under the old 115 kWh
    assert float(r["R_gap"][20]) == pytest.approx(50.0 * optimise.AMF_START_DELAY_S / 3600 / 0.95, abs=1e-6)
    assert float(r["R"][20]) == pytest.approx(2 * 50.0 * H / 0.95, abs=1e-6)             # a failed start: 30 minutes, as before


def test_the_amf_reserve_prices_the_start_delay_and_a_failed_start_once_each():
    """Golden: with the battery at its cut-off and nothing to charge it, the expected shortfall is the start delay's reserve at
    p (1 - p_fail) VoLL plus the failed start's at p p_fail VoLL, one of each on the step with the chance: never the diesel cost of a reserve
    the panel's start covers."""
    p = _mall(p_start=0.0)
    p["p_start"][40] = 0.2
    p["battery"].update({"E0": 10.0, "a_grid": False})
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    gap = 50.0 * 60 / 3600 / 0.95
    fail = 2 * 50.0 * H / 0.95
    assert r["r_gap"][40] == pytest.approx(gap, abs=1e-6)
    assert r["r"][40] == pytest.approx(fail, abs=1e-6)
    pf = optimise.AMF_FAIL_TO_START
    want = 0.2 * (1 - pf) * 500.0 * gap + 0.2 * pf * 500.0 * fail
    assert r["breakdown"]["expected_shortfall_rs"] == pytest.approx(round(want, 2), abs=0.01)
    assert want == pytest.approx(214.91, abs=0.01)                    # 83.33 for the delay, 131.58 for a failed start


def test_the_amf_reserve_reason_says_what_it_covers():
    p = _mall(p_start=0.05)
    p["c_imp"] = np.full(p["T"], 28.0)                                 # nothing to shave: the battery sits on its reserve
    p["battery"].update({"E0": 12.0, "c_cyc": 40.0})
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    held = [x for x in r["reasons"] if x["reason"] in ("hold_for_outage", "charge_for_reserve")]
    assert held
    for x in held:
        d = x["detail"]
        assert d["cover"] in ("start_delay", "failed_start")
        if d["cover"] == "start_delay":
            assert d["seconds"] == optimise.AMF_START_DELAY_S
        else:
            assert d["p_fail"] == pytest.approx(optimise.AMF_FAIL_TO_START)
            assert d["hours"] == pytest.approx(0.5)


def test_the_reserve_floor_shipped_is_the_reserve_the_plan_holds():
    """The mall's steps said reserve 95 % while the plan held its battery for a reserve the AMF panel covers. A step's reserve_pct is the
    reserve the plan holds (its row's target less the slack it accepts at that row's price), so the table and Mode B never ask for a floor
    above the planned state of charge, and an outage that starts at the planned state of charge is not "below the planned reserve"."""
    p = _mall()
    r = optimise.solve(p)
    steps = plans._steps_out(p, r, {"outage": {"p_down": np.zeros(p["T"])}})
    for s in steps:
        assert s["reserve_pct"] <= s["soc_pct"] + 0.1
    assert min(s["reserve_pct"] for s in steps) < 20.0                # no 95 % floor through the peak


# ------------------------------------------------------------------------------------------------ engine-plans-live-12

def test_the_reserve_policys_threshold_decides_the_reserve_beyond_the_horizon(company):
    """The site's "reserve when an outage is this likely" did nothing: R_next switched on a hard-coded 0.5 outages a day. It now follows the
    site's threshold against the chance that an outage starts on the horizon's last day (the forecast for the day after): a Lahore feeder's
    0.35 trips a day is a 30 % chance, over the default 20 %, and under a 50 % threshold."""
    sid = _site(disco="LESCO")
    full = sites.full(sid)
    t0 = twin.day_start_utc(date(2026, 11, 3))
    bundle = forecast.issue(full, t0, save=False)
    p = plans.build_problem(full, t0, bundle, 192)
    chance = 1.0 - float(np.prod(1.0 - np.asarray(bundle["outage"]["p_start"][96:192])))
    assert 0.2 < chance < 0.5
    assert p["terminal"]["p_next"] == pytest.approx(chance, abs=1e-6)
    assert p["terminal"]["R_next"] > 0
    # the reserve beyond the horizon is what the battery can deliver: 2 hours of the critical load, never above its discharge power
    assert p["terminal"]["R_next"] == pytest.approx(float(np.mean(np.minimum(p["Lc"][-96:], 100.0))) * 2.0 / 0.95, abs=1e-6)
    sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 20}, "t")
    q = plans.build_problem(sites.full(sid), t0, bundle, 192)
    assert q["terminal"]["R_next"] == pytest.approx(20.0 * 2.0 / 0.95, abs=1e-6)
    sites.update(sid, {"reserve_policy": {"p_threshold": 0.5, "min_soc_pct": 20.0}}, "t")
    full = sites.full(sid)
    q = plans.build_problem(full, t0, bundle, 192)
    assert q["terminal"]["R_next"] == 0.0


def test_no_step_enters_a_likely_outage_under_the_reserve_floor():
    """Faisalabad entered its 81 %-likely scheduled load shedding at 13.0-13.7 % against the site's "20 % floor when an outage is 20 % likely".
    The delivered floor (min_soc_pct, which the fallback holds on every grid-up step) keeps it at 20 % or more, peak or not."""
    p = _floored(_problem(peak=(24, 40)))
    p["c_imp"] = np.where(np.asarray(p["peak"]), 40.02, 28.0)
    p["battery"]["E0"] = 150.0
    p["p_start"] = np.zeros(p["T"])
    p["p_start"][39] = 0.81                                            # a window adherence 0.8 keeps g = 1: the chance sits on the step before
    p["Lc"] = np.zeros(p["T"])                                         # no reserve row: the floor alone holds the battery
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert float(np.sum(r["sol"]["D"][24:40])) > 1.0
    assert float(r["sol"]["E"][40]) >= 40.0 - 1e-6
    p["battery"].pop("soc_floor_grid")
    assert float(optimise.solve(p)["sol"]["E"][40]) < 40.0 - 1.0     # without the floor the peak took it to the cut-off


def test_genset_off_is_named_at_the_sites_threshold_with_its_chance():
    """"Genset off ... although an outage is likely soon" was named above a fixed 20 %: it follows the site's threshold and gives the chance."""
    p = _problem()
    p["genset"] = {"S": 100.0, "f_min": 0.3, "a": 0.273, "b": 0.033, "c_fuel": 380.0, "c_start": 1500.0, "c_runhr": 400.0, "c_om": 8.0, "UT": 4,
                   "DT": 2, "u_prev": 0, "elapsed": 99, "cap_hours": 24.0, "hours_today": 0.0, "day_index": np.zeros(p["T"], dtype=int),
                   "fuel_available": 1e9, "parallel": False, "control": "manual", "hold": np.zeros(p["T"], dtype=bool)}
    del p["battery"]
    p["p_start"] = np.full(p["T"], 0.12)
    p["reserve_threshold"] = 0.1
    r = optimise.solve(p)
    assert r["reasons"][10]["reason"] == "genset_off"
    assert r["reasons"][10]["detail"]["p"] == pytest.approx(0.12)
    p["reserve_threshold"] = 0.2
    r = optimise.solve(p)
    assert r["reasons"][10]["reason"] == "idle"
