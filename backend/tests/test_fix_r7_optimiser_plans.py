"""Regressions for round 7's audit findings on the optimiser and plan building (work package optimiser-plans): no load shed with the grid up, the
plan's own MDI cap per step, grid charging in the peak window and the reason rules."""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from gridlytics import forecast, optimise, plans, sites, solar, store, telemetry, twin

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


def test_a_reserve_reason_says_how_much_of_it_is_the_state_of_charge_margin():
    """With the state of charge uncertain (sigma), every reserve row is widened by 2 sigma E_nom (Part 4.3). Behind an AMF panel that margin is
    nearly all of the start delay's reserve: "Keeping 10.9 kWh in reserve to carry the load for up to 60 seconds" read as 650 kW for a minute.
    The detail gives the margin apart (`margin_kwh`), never above the reserve it names."""
    p = _mall(p_start=0.05)
    p["c_imp"] = np.full(p["T"], 28.0)
    p["battery"].update({"E0": 21.0, "c_cyc": 40.0, "sigma0": 0.05})
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    held = [x["detail"] for x in r["reasons"] if x["reason"] in ("hold_for_outage", "charge_for_reserve")]
    assert held
    gap = 50.0 * optimise.AMF_START_DELAY_S / 3600 / 0.95
    for d in held:
        assert d["margin_kwh"] == pytest.approx(min(2 * 0.05 * 100.0, d["kwh"]), abs=0.05)
        assert d["margin_kwh"] <= d["kwh"] + 1e-9
        if d["cover"] == "start_delay":
            assert d["kwh"] - d["margin_kwh"] == pytest.approx(gap, abs=0.1)     # the delay itself: under a kWh
    # without the uncertainty there is no margin
    p["battery"].update({"E0": 12.0, "sigma0": 0.0})
    r = optimise.solve(p)
    held = [x["detail"] for x in r["reasons"] if x["reason"] in ("hold_for_outage", "charge_for_reserve")]
    assert held and all(d["margin_kwh"] == 0.0 for d in held)
    # nor on a grid-tied reserve's detail without an AMF panel
    q = _reserve_case()
    q["battery"].update({"E0": 140.0, "sigma0": 0.05, "c_cyc": 40.0})        # at the reserve: 20 + 4 x 100 x 0.25 / 0.95 + 20 = 145.3 kWh
    q["c_imp"] = np.full(q["T"], 28.0)
    rq = optimise.solve(q)
    hq = [x["detail"] for x in rq["reasons"] if x["reason"] in ("hold_for_outage", "charge_for_reserve")]
    assert hq and all("cover" not in d for d in hq)
    assert all(d["margin_kwh"] == pytest.approx(min(2 * 0.05 * 200.0, d["kwh"]), abs=0.05) for d in hq)


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


# ------------------------------------------------------------------------------------------------ engine-optimiser-03, engine-optimiser-04

def _under_cut_off(p, E0=10.0):
    """A battery that starts under its own cut-off: 5 % against soc_min 10 % (after native operation or an outage)."""
    p["battery"]["E0"] = E0
    return p


def test_a_start_under_the_cut_off_that_nothing_can_recharge_is_planned_never_infeasible():
    """The terminal row forced E[T] >= soc_min, so a battery under its cut-off with no charge source (grid charging off and no sun, or a charge
    block to the horizon's end) made every tick infeasible, and the relaxed retry too: it holds what it has instead, with no terminal credit."""
    p = _under_cut_off(_problem())
    p["battery"]["a_grid"] = False
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert float(np.max(r["sol"]["D"])) < 1e-6
    assert float(np.min(r["sol"]["E"])) >= 10.0 - 1e-6
    assert r["E_use"] == pytest.approx(0.0, abs=1e-6)
    # golden: the end state under the cut-off is the terminal term, 10 kWh short at a rupee over the dearest stored kWh (Rs 60 / 0.95)
    assert r["breakdown"]["terminal_credit_rs"] == pytest.approx(10.0 * (60.0 / 0.95 + 1.0), abs=0.05)
    blocked = _under_cut_off(_problem())
    blocked["battery"]["charge_block"] = np.ones(blocked["T"], dtype=bool)
    r = optimise.solve_with_policy(blocked)
    assert r["status"] == "optimal" and r["relaxed"] is False
    assert float(np.max(r["sol"]["D"])) < 1e-6


def test_a_start_under_the_cut_off_recovers_to_it_once_and_the_band_binds_from_then_on():
    """min(soc_min, E0) was every step's lower bound, so a plan starting at 5 % charged off-peak and discharged into the evening peak down to
    5 % again (Mode B was then told it may). As EMHASS's single recovery (R2 3.5): until the battery is back at soc_min it does not discharge,
    and from then on soc_min binds."""
    p = _under_cut_off(_problem())
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    E, D = r["sol"]["E"], r["sol"]["D"]
    assert float(np.sum(D[72:88])) > 1.0                               # it still discharges in the peak
    back = next(t for t in range(p["T"] + 1) if E[t] >= 20.0 - 1e-6)
    assert all(D[t] < 1e-6 for t in range(back))                      # no discharge before it is back at the cut-off
    assert float(np.min(E[back:])) >= 20.0 - 1e-6                     # and never under it again
    assert float(E[-1]) >= 20.0 - 1e-6
    from gridlytics import delivery
    steps = plans._steps_out(p, r, {"outage": {"p_down": np.zeros(p["T"])}})
    floors = [b["soc_floor_pct"] for b in delivery.mode_b(steps)["steps"]]
    assert min(floors[back:]) >= 10.0 - 1e-9                          # Mode B is never told to go under the cut-off once it is back


def test_a_start_under_the_cut_off_recovers_even_where_cycling_does_not_pay():
    """At flat prices nothing pays for a cycle, yet a battery left under its cut-off is recharged to it (the end state under the cut-off is priced
    above the dearest kWh the horizon could store), and without a peak it then holds there."""
    p = _under_cut_off(_problem(peak=(0, 0)))
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    assert float(r["sol"]["E"][-1]) >= 20.0 - 1e-6
    assert float(np.max(r["sol"]["D"])) < 1e-6


# ------------------------------------------------------------------------------------------------ engine-plans-live-02, flow-telemetry-plan-control-09

SOC_COLS = ("site_id", "ts", "source_id", "grid_present", "battery_soc_est_pct", "battery_soc_bms_pct", "battery_power_w")


def _battery_site(soc_max=0.95, **extra):
    sid = sites.create({"name": "Pack", "disco": "FESCO", "critical_load_kw": [50.0] * 24, "genset_start_control": "none", **extra}, "t")["id"]
    sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100, "soc_max": soc_max}, "t")
    return sid


def _daily_cycles(sid, start, days, top, bottom=30.0, est=True, bms_off=0.0, until=None, down=None):
    """Minute rows of a pack that charges from `bottom` to `top` percent over 08:00-12:00 local, holds there to 14:00, and discharges back to
    `bottom` by midnight, every day (the estimate column, or the BMS alone with est=False); the grid is down over `down`, a pair of minutes after
    local midnight, every day."""
    rows = []
    for i in range(days * 1440):
        m = i % 1440                                                   # minutes after local midnight (start is a local midnight)
        if m < 480:
            soc, kw = bottom, 0.0
        elif m < 720:
            soc, kw = bottom + (top - bottom) * (m - 480) / 240, -(top - bottom) * 2.0 / 4
        elif m < 840:
            soc, kw = top, 0.0
        else:
            soc, kw = top - (top - bottom) * (m - 840) / 600, (top - bottom) * 2.0 / 10
        if until is None or start + 60 * i < until:
            grid = 0.0 if down and down[0] <= m < down[1] else 1.0
            rows.append((sid, start + 60 * i, "edge", grid, soc if est else None, soc + bms_off, kw * 1000))
    telemetry.write_rows(rows, SOC_COLS)


def test_a_charge_to_the_packs_own_top_of_charge_re_anchors_the_estimate(company):
    """Anchors needed 98 %, but every pack stopped at its soc_max of 95 %, so a full charge never anchored: sigma climbed 0.19 points an hour
    to its cap, and the reserve's 2-sigma widening (up to 30 % of the pack) kept the mill's battery full through every peak. A charge to the
    pack's own top (on the upper knee, 95 % or more) is now the full-charge anchor."""
    from gridlytics import battery
    sid = _battery_site()
    start = twin.day_start_utc(date(2026, 11, 3))
    _daily_cycles(sid, start, 3, top=95.0)
    now = start + 2 * 86400 + 18 * 3600                               # 18:00 on the third day, four hours after the charge ended
    st = battery.state(sites.full(sid), now)
    assert st["waiting_for_anchor"] is False
    assert abs(st["anchor"] - (start + 2 * 86400 + 14 * 3600)) <= 15 * 60   # the charge's end at the top, that afternoon
    assert st["sigma_pct"] < 4.0                                       # 1 + 0.19 x 4 h + 2 x throughput / E_nom
    assert "sigma_high" not in [a[0] for a in st["alerts"]]
    p = plans.build_problem(sites.full(sid), now // 900 * 900, forecast.issue(sites.full(sid), now // 900 * 900, save=False), 96)
    assert p["battery"]["sigma0"] < 0.04                               # so the plan's reserve widens by a few points, not 30 %


def test_a_top_of_charge_under_the_upper_knee_is_no_anchor(company):
    """On the LFP plateau the state of charge cannot be read (R6 5.2): a pack set to stop at 90 % has no full-charge anchor there."""
    from gridlytics import battery
    sid = _battery_site(soc_max=0.90)
    start = twin.day_start_utc(date(2026, 11, 3))
    _daily_cycles(sid, start, 3, top=90.0)
    st = battery.state(sites.full(sid), start + 2 * 86400 + 18 * 3600)
    assert st["waiting_for_anchor"] is True and st["anchor"] is None


# ------------------------------------------------------------------------------------------------ engine-optimiser-07

def test_a_stale_reading_widens_sigma_by_the_time_since_it_and_flags_the_plan(company):
    """battery.state took the last reading up to 30 days old and froze sigma at it (hours from the anchor to the last reading, not to now), so
    a plan started from a 6-hour-old state of charge with the reserve margin it had when the data stopped. Sigma now grows to now, with R6
    5.3's gap term (1 point per 10 minutes past one control period), and the plan says it started from a stale reading."""
    from gridlytics import battery
    sid = _battery_site()
    start = twin.day_start_utc(date(2026, 11, 3))
    fresh = start + 2 * 86400 + 18 * 3600
    _daily_cycles(sid, start, 3, top=95.0, until=fresh)                 # the readings stop at 18:00 on the third day
    st = battery.state(sites.full(sid), fresh)
    assert st["reading_age_min"] == pytest.approx(1.0) and st["stale"] is False
    sigma_fresh = st["sigma_pct"]
    assert sigma_fresh < 4.0
    late = fresh + 6 * 3600                                            # six hours later, no reading since
    st = battery.state(sites.full(sid), late)
    assert st["reading_age_min"] == pytest.approx(361.0) and st["stale"] is True
    assert st["sigma_pct"] >= 15.0                                     # the state could have moved by the pack's power for six hours
    assert st["soc_pct"] == pytest.approx(battery.state(sites.full(sid), fresh)["soc_pct"])
    out = plans.tick(sid, at=late, save=False)
    bs = out["summary"]["battery_state"]
    assert bs["stale"] is True and bs["age_min"] == pytest.approx(361.0)
    assert bs["sigma_pct"] == pytest.approx(st["sigma_pct"])
    # a fresh reading leaves sigma as it was, and the plan unflagged
    bs = plans.tick(sid, at=fresh, save=False)["summary"]["battery_state"]
    assert bs["stale"] is False and bs["sigma_pct"] == pytest.approx(sigma_fresh)


def test_a_plan_with_no_battery_reading_says_it_starts_from_an_assumed_state(company):
    sid = _battery_site()
    bs = plans.tick(sid, at=twin.day_start_utc(date(2026, 11, 3)) + 12 * 3600, save=False)["summary"]["battery_state"]
    assert bs["soc_pct"] == pytest.approx(50.0) and bs["age_min"] is None and bs["stale"] is True


# ------------------------------------------------------------------------------------------------ engine-optimiser-15

def _drifting(sid, soc_max_pct=95.0, bottom=30.0):
    """An anchor four days ago (a full charge to the pack's top, or the low knee), then four days of shallow cycles (30 to 80 %) that never
    reach a knee."""
    start = twin.day_start_utc(date(2026, 11, 1))
    _daily_cycles(sid, start, 1, top=soc_max_pct, bottom=bottom)
    _daily_cycles(sid, start + 86400, 4, top=80.0, until=start + 4 * 86400 + 18 * 3600)
    return start + 4 * 86400 + 18 * 3600


def test_sigma_over_15_points_asks_the_plan_for_one_charge_to_the_top_and_the_alert_says_so(company):
    """The sigma_high alert said "the plan will ask for a full charge to re-anchor", and nothing asked for one. Now the battery service asks
    for it (R6 5.7) and the plan charges the battery to its top once in the horizon; the alert says that in plain words."""
    from gridlytics import battery
    sid = _battery_site()
    now = _drifting(sid)
    st = battery.state(sites.full(sid), now)
    assert st["sigma_pct"] > 15.0 and st["anchor_due"] is True and st["anchor_soc_pct"] == pytest.approx(95.0)
    text = dict(st["alerts"])["sigma_high"]
    assert "re-anchor" not in text and "will ask" not in text
    assert "charges the battery to 95 % once" in text
    # the problem says it is asked for; only a live tick puts it to the solve (a past day's hindsight bound, verify._bound, never)
    t0 = now // 900 * 900
    p = plans.build_problem(sites.full(sid), t0, forecast.issue(sites.full(sid), t0, save=False), 96)
    assert p["battery_state"]["anchor_soc_pct"] == pytest.approx(95.0) and "anchor_soc" not in p["battery"]
    out = plans.tick(sid, at=now, save=False)
    assert out["status"] == "active"
    assert out["summary"]["battery_state"]["anchor_soc_pct"] == pytest.approx(95.0)
    assert max(s["soc_pct"] for s in out["steps"]) >= 95.0 - 0.05
    t = out["summary"]["battery_state"]["anchor_t"]
    assert any(s["t"] + 900 == t and s["soc_pct"] >= 95.0 - 0.05 for s in out["steps"])


def test_a_pack_set_to_stop_under_the_knee_is_not_asked_to_charge_past_its_top(company):
    from gridlytics import battery
    sid = _battery_site(soc_max=0.90)
    now = _drifting(sid, 90.0, bottom=10.0)                           # anchored at the low knee four days ago
    st = battery.state(sites.full(sid), now)
    assert st["sigma_pct"] > 15.0 and st["anchor_due"] is False and st["anchor_soc_pct"] is None
    text = dict(st["alerts"])["sigma_high"]
    assert "charges the battery" not in text and "stop charging at 90 %" in text
    p = plans.build_problem(sites.full(sid), now // 900 * 900, forecast.issue(sites.full(sid), now // 900 * 900, save=False), 96)
    assert "anchor_soc" not in p["battery"]


def test_the_anchor_is_met_at_the_end_of_one_hour_by_the_cheapest_charge_and_never_makes_a_tick_infeasible():
    p = _problem(peak=(0, 0))                                          # flat prices: nothing else would fill the battery
    r = optimise.solve(p)
    assert float(np.max(r["sol"]["E"])) < 150.0
    p["battery"]["anchor_soc"] = 0.95
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    E = r["sol"]["E"]
    t = r["anchor"]["step"]
    assert float(E[t + 1]) >= 190.0 - 1e-6 and r["anchor"]["short_kwh"] == pytest.approx(0.0, abs=1e-6)
    hour = np.asarray(p["ts"]) // 3600
    assert t == p["T"] - 1 or hour[t + 1] != hour[t]                  # at the end of a clock hour
    # on a sunny day without grid charging it is met from the PV surplus
    p["battery"]["a_grid"] = False
    p["PV"] = np.where((np.arange(96) >= 40) & (np.arange(96) < 56), 140.0, 0.0)
    p["PVlo"] = p["PV"] * 0.6
    r = optimise.solve(p)
    assert r["status"] == "optimal" and r["anchor"]["short_kwh"] == pytest.approx(0.0, abs=1e-6)
    assert 40 <= r["anchor"]["step"] < 60
    # with nothing to charge it, the plan is still made and says how far short it stays
    p["PV"] = np.zeros(96)
    p["PVlo"] = np.zeros(96)
    r = optimise.solve(p)
    assert r["status"] == "optimal" and r["anchor"]["short_kwh"] == pytest.approx(90.0, abs=0.01)


def test_the_first_plan_that_schedules_the_anchor_fixes_when_so_re_planning_never_defers_it(company):
    """Re-planned every 15 minutes, a charge that costs the same tonight as tomorrow night moved to the later night at every tick (the demo mall
    was asked for one for days and never charged full). The first active plan's anchor time is kept as a deadline the next plans meet."""
    sid = _battery_site()
    now = _drifting(sid)
    first = plans.tick(sid, at=now)
    by = sites.full(sid)["state"]["anchor_by"]
    assert by == first["summary"]["battery_state"]["anchor_t"]
    later = now + 3 * 3600
    _daily_cycles(sid, twin.day_start_utc(date(2026, 11, 2)), 4, top=80.0, until=later)  # readings carry on, still on the plateau
    second = plans.tick(sid, at=later)
    assert second["summary"]["battery_state"]["anchor_t"] <= by
    assert sites.full(sid)["state"]["anchor_by"] == by
    # once the battery is seen at its top the request ends and the deadline goes
    from gridlytics import battery
    t_full = later + 3600
    telemetry.write_rows([(sid, later + 60 * i, "edge", 1.0, 95.0, 95.0, 0.0) for i in range(60)], SOC_COLS)
    assert battery.state(sites.full(sid), t_full)["anchor_due"] is False
    plans.tick(sid, at=t_full)
    assert sites.full(sid)["state"].get("anchor_by") is None


# ------------------------------------------------------------------------------------------------ brief-vs-build-03

def test_trust_in_a_bms_with_nothing_independent_to_compare_it_with_is_not_assessed(company):
    """A BMS-only (or CSV) source compared the BMS with itself: the gap was 0, so trust read 0.6 to 1.0 whatever the BMS's quality and trust_low
    could never fire. With no independent estimate trust is not assessed (null, basis "bms_only"); with one it is scored as before."""
    from gridlytics import battery
    start = twin.day_start_utc(date(2026, 11, 3))
    now = start + 2 * 86400 + 18 * 3600
    bms_only = _battery_site()
    _daily_cycles(bms_only, start, 3, top=95.0, est=False, until=now)
    st = battery.state(sites.full(bms_only), now)
    assert st["soc_pct"] is not None
    assert st["trust"] is None and st["trust_basis"] == "bms_only"
    assert "trust_low" not in [a[0] for a in st["alerts"]]
    both = _battery_site()
    _daily_cycles(both, start, 3, top=95.0, bms_off=12.0, until=now)  # an estimate 12 points under the BMS
    st = battery.state(sites.full(both), now)
    assert st["trust_basis"] == "independent" and st["trust"] < 0.5
    assert "trust_low" in [a[0] for a in st["alerts"]]


def test_an_anchor_deadline_sooner_than_any_hour_end_keeps_the_first_hour_end():
    p = _problem(peak=(0, 0))
    p["battery"]["anchor_soc"] = 0.95
    p["battery"]["anchor_by"] = int(p["ts"][0]) + 900                 # sooner than the horizon's first clock-hour end
    r = optimise.solve(p)
    hour = np.asarray(p["ts"]) // 3600
    first = next(t for t in range(p["T"]) if hour[t + 1] != hour[t])
    assert r["status"] == "optimal" and r["anchor"]["step"] == first


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-09 (the plans against native)

def _compare(sid, monkeypatch):
    """The plan page's Compare with native (routes_plans.plan_compare: both replayed through the twin on the plan's own forecast), rupees kept."""
    from gridlytics import routes_plans
    monkeypatch.setattr(routes_plans, "out", lambda o: o)
    return routes_plans.plan_compare(sid)["compare"]


@pytest.mark.parametrize("kind", ["textile", "mall"])
def test_a_pack_that_charges_to_its_top_each_day_is_used_in_the_peak_and_the_plan_costs_no_more_than_native(company, monkeypatch, kind):
    """Faisalabad (215 kWh, manual genset, 70 kW critical) and Lahore (100 kWh at 50 kW behind an auto-start panel, 180 kW critical): sigma at
    its cap (no anchor at 95 %) widened the reserve past the usable band, so both plans held 95 % for 48 hours, discharged 0 to 2 kWh, and
    cost Rs 2,700 to 4,100 more than native. A pack that reaches its 95 % top every day now anchors there, and the plan discharges in the
    peak and costs no more than native."""
    if kind == "textile":
        sid = _battery_site(critical_load_kw=[70.0] * 24, genset_start_control="manual")
        sites.put_asset(sid, "battery", {"nameplate_kwh": 215, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    else:
        sid = _battery_site(critical_load_kw=[180.0] * 24, genset_start_control="auto_on_grid_loss", outage_load_mode=1)
        sites.put_asset(sid, "battery", {"nameplate_kwh": 100, "p_ch_kw": 50, "p_dis_kw": 50}, "t")
        sites.put_asset(sid, "genset", {"rated_kw": 640, "f_min": 0.25, "fuel_kind": "hsd", "min_up_h": 0.5, "tank_l": 4000}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 200, "kw_ac": 180}, "t")
    sites.capture_native(sid, {"source": "read_back", "work_mode": "self_use", "backup_soc_pct": 30}, "t")
    if kind == "textile":
        forecast.set_schedule(sid, [["14:00", "16:00", "weekdays"]], "entered", "t")
    start = twin.day_start_utc(date(2026, 11, 3))
    now = start + 2 * 86400 + 16 * 3600 + 900                         # 16:15 on the third day
    _daily_cycles(sid, start, 3, top=95.0, until=now, down=(840, 960) if kind == "textile" else None)
    out = plans.tick(sid, at=now)
    assert out["status"] == "active"
    assert out["summary"]["battery_state"]["sigma_pct"] < 4.0          # anchored this afternoon at the pack's own top
    assert sum(s["discharge_kw"] for s in out["steps"] if s["peak"]) * H > 40.0
    arms = _compare(sid, monkeypatch)
    # before: sigma at 15 points, and the mill's plan cost about Rs 900 more than native over the 48 hours (it bought energy to hold above the band)
    assert arms["plan"]["energy_rs"] <= arms["native"]["energy_rs"]
    assert arms["plan"]["unserved_kwh"] <= arms["native"]["unserved_kwh"]


# ------------------------------------------------------------------------------------------------ engine-optimiser-13

IMP_COLS = ("site_id", "ts", "source_id", "grid_present", "grid_import_wh")


def _import_minutes(sid, t_from, t_to, kw, skip=()):
    """One row a minute of `kw` grid import (a float, or a function of the minute's epoch seconds) over [t_from, t_to), none in `skip`'s spans."""
    rows = []
    for t in range(int(t_from), int(t_to), 60):
        if any(a <= t < b for a, b in skip):
            continue
        k = kw(t) if callable(kw) else kw
        rows.append((sid, t, "test", 1.0, k * 1000 / 60))
    telemetry.write_rows(rows, IMP_COLS)


def test_month_to_date_reads_missing_minutes_as_unknown_not_zero(company):
    """month_to_date filled missing minutes with zero import: a block with 5 of its 30 minutes missing read 5/6 of its mean, a block with 15
    missing still counted as complete (the 80 % rule counted grid minutes, not known ones), and a day without data took a day off the month's
    imports. A block now counts only with 80 % of its minutes known, at the mean of those; the month's imports per period are scaled from the
    known minutes to all of them (as verify.month_totals does), and the open block's elapsed mean is that of its known minutes."""
    sid = _site()
    conn = sites.full(sid)["connections"][0]
    ms = twin.day_start_utc(date(2026, 11, 1))
    t0 = twin.day_start_utc(date(2026, 11, 20)) + 10 * 3600 + 15 * 60     # 10:15 on the 20th: the open block has 15 minutes
    a = twin.day_start_utc(date(2026, 11, 10)) + 10 * 3600               # 10:00-10:30 on the 10th: 300 kW, its last 5 minutes missing
    b = twin.day_start_utc(date(2026, 11, 12)) + 10 * 3600               # 10:00-10:30 on the 12th: 900 kW for 15 minutes, then nothing
    day15 = twin.day_start_utc(date(2026, 11, 15))                       # the 15th: no data at all

    def kw(t):
        return 300.0 if a <= t < a + 1800 else 900.0 if b <= t < b + 1800 else 100.0

    _import_minutes(sid, ms, t0, kw, skip=[(a + 1500, a + 1800), (b + 900, b + 1800), (day15, day15 + 86400), (t0 - 300, t0)])
    m = plans.month_to_date(sid, conn, t0)
    assert m["M0"] == pytest.approx(300.0)                              # before: 450 (block b at 900 x 15/30); block a read 250
    # the month's imports: every minute so far at 100 kW, and blocks a and b's known minutes above it (the known minutes' energy per period,
    # scaled to all of them)
    total = m["mtd"]["peak"] + m["mtd"]["off_peak"]
    extra = (25 * 200.0 + 15 * 800.0) / 60
    assert total == pytest.approx((t0 - ms) / 3600 * 100.0 + extra, rel=0.002)  # before: a whole day (2,400 kWh) and the gaps short
    # the open block (10:00-10:15, its last 5 minutes missing): the mean of the 10 minutes known, not 2/3 of it
    assert m["n_elapsed"] == 1 and m["G_elapsed"] == pytest.approx(100.0)


# ------------------------------------------------------------------------------------------------ engine-optimiser-02, engine-plans-live-07

def _month_end(p, end=48, M0=100.0, floor=50.0, M_exp=None):
    """The MDI month ends at step `end`: M covers the steps before it, M2 the new month's after it (floor 50 kW, Rs 1,250 per kW)."""
    p["mdi"] = {"rate": 1250.0, "M0": M0, "floor": floor, "n_mdi": 2, "G_elapsed": 0.0, "n_elapsed": 0, "month_end_step": end}
    if M_exp is not None:
        p["mdi"]["M_exp"] = M_exp
    return p


def test_a_new_months_steps_are_free_up_to_the_expected_month_peak_and_the_battery_is_kept_for_the_peak():
    """The mall's plan made on 30 Oct: M2 (the new month's peak) was bounded only by the 25 % floor, so every kW above it on 1 Nov was priced as
    a whole month's demand charge, and the plan drained the battery off-peak from 95 % to 10 % to hold 1 Nov's blocks at the floor, leaving
    nothing for the evening peak. The month will reach its expected peak anyway (the second-highest day's peak of the last seven), so a kW
    under it costs nothing: M and M2 are bounded below by it, and the battery discharges in the peak window."""
    p = _month_end(_problem(), M_exp=120.0)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    D = np.asarray(r["sol"]["D"])
    pk = np.asarray(p["peak"], dtype=bool)
    new_off_peak = (np.arange(96) >= 48) & ~pk
    assert float(np.sum(D[new_off_peak])) * H < 1e-3                  # before: about 70 kWh discharged off-peak on the new month's first morning
    assert float(np.sum(D[pk])) * H > 50.0
    assert r["M"] == [pytest.approx(120.0), pytest.approx(120.0)]
    # where the expected peak is under the month so far, the month so far bounds M, and the expected peak M2
    q = optimise.solve(_month_end(_problem(), M0=150.0, M_exp=120.0))
    assert q["M"] == [pytest.approx(150.0), pytest.approx(120.0)]


def test_month_to_date_gives_the_expected_month_peak_from_the_last_seven_days(company):
    """At a month's start M0 is near nothing (Karachi's plans of 1 Nov held 797 kW on a Sunday, then the weekdays reached 902 kW, above
    October's 861). The expected month peak is the second-highest day's peak of the seven days before (one odd day does not set it); it bounds
    the plan's M and M2 from below, and once the seven days lie inside the month the month so far covers it."""
    sid = _site()
    conn = sites.full(sid)["connections"][0]
    t0 = twin.day_start_utc(date(2026, 11, 3)) + 10 * 3600 + 15 * 60     # 10:15 on 3 Nov
    peaks = {date(2026, 10, 28): 500.0, date(2026, 10, 30): 480.0, date(2026, 11, 2): 300.0}
    blocks = {twin.day_start_utc(d) + 10 * 3600: kw for d, kw in peaks.items()}

    def kw(t):
        b = t // 1800 * 1800
        return blocks.get(b, 100.0)

    _import_minutes(sid, t0 - 8 * 86400, t0, kw)
    m = plans.month_to_date(sid, conn, t0)
    assert m["M0"] == pytest.approx(300.0)
    assert m["M_exp"] == pytest.approx(480.0)
    full = sites.full(sid)
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    assert p["mdi"]["M_exp"] == pytest.approx(480.0)
    assert optimise.mdi_floors(p["mdi"]) == (pytest.approx(480.0), pytest.approx(480.0))
    # a week later the seven days are inside the month: the month so far (500 kW on the 5th) covers them
    later = t0 + 7 * 86400
    blocks[twin.day_start_utc(date(2026, 11, 5)) + 10 * 3600] = 500.0
    _import_minutes(sid, t0, later, kw)
    m = plans.month_to_date(sid, conn, later)
    assert m["M_exp"] <= m["M0"] and m["M0"] == pytest.approx(500.0)


def test_slots_in_the_next_billing_month_carry_its_own_cap(company, monkeypatch):
    """plans.tick compiled every slot with M, the month ending's cap, so the new month's slots shaved to the old month's peak. A horizon that
    crosses the month's end compiles the new month's days with M2's cap, and the summary gives it."""
    from gridlytics import delivery
    sid = _site()
    t0 = twin.day_start_utc(date(2026, 10, 31)) + 10 * 3600             # 10:00 on 31 Oct: 14 hours of October, then November
    hot = twin.day_start_utc(date(2026, 10, 30)) + 10 * 3600            # October's peak, 400 kW, on the 30th; 150 kW otherwise
    _import_minutes(sid, t0 - 8 * 86400, t0, lambda t: 400.0 if hot <= t < hot + 1800 else 150.0)
    seen = []
    real = delivery.compile_mode_a

    def spy(steps, brand, battery, fallback, bms_low=10.0, mdi_cap_kw=None):
        seen.append(({plans._local(s["t"]).date() for s in steps}, mdi_cap_kw))
        return real(steps, brand, battery, fallback, bms_low, mdi_cap_kw)

    monkeypatch.setattr(delivery, "compile_mode_a", spy)
    out = plans.tick(sid, at=t0, save=False)
    assert out["status"] in ("active", "held")
    M, M2 = out["summary"]["mdi_cap_kw"], out["summary"]["mdi_cap_next_kw"]
    assert M >= 400.0 - 0.1 and M2 is not None and M2 < M - 100.0
    assert len(seen) == 2
    (before, cap1), (after, cap2) = seen
    assert before == {date(2026, 10, 31)} and cap1 == pytest.approx(M, abs=0.1)
    assert min(after) == date(2026, 11, 1) and cap2 == pytest.approx(M2, abs=0.1)
    # and the compiled days are those of both calls, in order
    assert [d["date"] for d in out["slots"]["days"]] == ["2026-10-31", "2026-11-01", "2026-11-02"]
    for d in out["slots"]["days"][1:]:
        for s in d["slots"]:
            if s["mode"].startswith("charge"):
                assert s["grid_cap_w"] == round(cap2 * 1000)


# ------------------------------------------------------------------------------------------------ engine-plans-live-08, engine-optimiser-12

LINES = ("energy_rs", "genset_rs", "wear_rs", "mdi_rise_rs", "mdi_month_rs", "calendar_rs", "unserved_rs", "expected_shortfall_rs",
         "terminal_credit_rs")


def test_at_a_months_start_the_month_charge_is_its_own_line_and_no_rise():
    """Golden. Karachi's first plans of November showed 'MDI rise' Rs 527,841: M0 is near nothing at a month's start, and the rise was measured
    from max(M0, floor), so the whole month's demand charge above the 25 % floor read as a rise the plan caused. The rise is now measured from
    the peak's lower bound (the expected month peak here), and the charge at that bound is its own line, `mdi_month_rs`."""
    p = _problem()
    p["mdi"] = {"rate": 1250.0, "M0": 0.0, "floor": 50.0, "n_mdi": 2, "G_elapsed": 0.0, "n_elapsed": 0, "month_end_step": 96, "M_exp": 120.0}
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    bd = r["breakdown"]
    assert bd["mdi_rise_rs"] == pytest.approx(0.0, abs=0.01)           # before: 1,250 x (120 - 50) = Rs 87,500
    assert bd["mdi_month_rs"] == pytest.approx(1250.0 * 120.0, abs=0.01)  # Rs 150,000: 120 kW at Rs 1,250 per kW


def test_the_next_months_rise_and_charge_are_itemised():
    """Golden. M2, the next month's peak, was in the objective and in no line of the breakdown: the cost terms did not explain the objective
    across a month's end. A 300 kW half hour on the new month's first morning raises M2 above its 50 kW floor; the rise and the charge at the
    floors are itemised."""
    p = _month_end(_problem(), end=48, M0=100.0, floor=50.0)
    p["L"] = p["L"].copy()
    p["L"][56:58] = 300.0
    p["Lhi"] = np.maximum(p["Lhi"], p["L"])
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    M, M2 = r["M"]
    assert M2 > 150.0
    bd = r["breakdown"]
    assert bd["mdi_rise_rs"] == pytest.approx(1250.0 * ((M - 100.0) + (M2 - 50.0)), abs=0.05)
    assert bd["mdi_month_rs"] == pytest.approx(1250.0 * (100.0 + 50.0), abs=0.01)


def test_the_cost_lines_add_up_to_the_objective():
    """The plan page showed 'Not itemised (mainly the month's demand charge)' Rs 577,290 of a Rs 1,007,127 objective. With the month's charge,
    the next month's and the calendar ageing itemised, the lines add up to the objective but for the solver's tie-breaks (a fraction of a paisa
    per kWh)."""
    p = _month_end(_problem(), end=48, M0=100.0, floor=50.0, M_exp=95.0)
    p["L"] = p["L"].copy()
    p["L"][56:58] = 300.0
    p["Lhi"] = np.maximum(p["Lhi"], p["L"])
    p["battery"]["b_cal"] = np.full(96, 0.04)
    p["terminal"]["c_cal_hold"] = 0.4
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    bd = r["breakdown"]
    E = np.asarray(r["sol"]["E"])
    assert bd["calendar_rs"] == pytest.approx(0.04 * float(np.sum(E[1:])), abs=0.01)
    assert sum(bd[k] for k in LINES) == pytest.approx(r["objective_rs"], abs=2.0)


# ------------------------------------------------------------------------------------------------ engine-optimiser-10

def test_the_terminal_value_takes_the_calendar_cost_of_holding_the_energy_to_the_next_peak(company):
    """Golden. v_end = eta_d c_next - c_cyc - c_cal_hold, c_cal_hold the calendar cost of holding a kWh until the next peak (Part 4.4). It was
    the mean of b_cal over 32 steps, one 15-minute step's slope (Rs 0.04), while energy left at a 07:00 horizon end waits 11 hours for the
    18:00 peak: the sum of the slopes over those 44 steps (the same hours of the horizon's last day stand for the day after, as for c_next)."""
    sid = _battery_site()
    full = sites.full(sid)
    t0 = twin.day_start_utc(date(2026, 11, 10)) + 7 * 3600            # 07:00: the 48 hours end at 07:00 on the 12th
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 192)
    assert int(np.argmax(np.asarray(p["peak"], dtype=bool)[96:])) == 44  # the peak starts at 18:00, 44 steps after 07:00
    ts = np.asarray(p["ts"])[96:140] + 86400                           # 07:00 to 17:45 on the day after the horizon
    temp = solar.air_temp(ts, solar.local_month(ts) - 1) + 7.0          # the enclosure's 7 C above the air (enclosure_temp_offset_c)
    want = float(np.sum(optimise.calendar_slope(temp, 52000.0)))
    assert p["terminal"]["c_cal_hold"] == pytest.approx(want, rel=1e-6)
    assert 1.0 < p["terminal"]["c_cal_hold"] < 3.0                      # before: Rs 0.04
    # and the plan's v_end deducts it
    r = optimise.solve(p, time_limit=30.0)
    assert r["v_end"] == pytest.approx(max(0.0, p["battery"]["eta_d"] * p["terminal"]["c_next"] - p["battery"]["c_cyc"] - want), abs=1e-6)


def test_the_holding_time_runs_to_the_dearest_step_or_off_grid_to_the_first_step_pv_does_not_cover():
    b_cal = np.full(192, 0.05)
    c_imp = np.full(192, 28.0)
    c_imp[96 + 40:96 + 56] = 60.0                                       # the last day's peak from its 40th step
    assert plans.terminal_cal_hold(b_cal, c_imp, np.zeros(192), np.zeros(192), off_grid=False) == pytest.approx(40 * 0.05)
    L, PV = np.full(192, 30.0), np.zeros(192)
    PV[96:96 + 30] = 50.0                                               # PV covers the load for the last day's first 30 steps
    assert plans.terminal_cal_hold(b_cal, np.zeros(192), L, PV, off_grid=True) == pytest.approx(30 * 0.05)
    # a calendar cost switched off holds nothing
    assert plans.terminal_cal_hold(np.zeros(192), c_imp, L, PV, off_grid=False) == 0.0


def _farm_day(c_next=400.0):
    """The demo farm's sizes and day (test_fix_r5_genset): night 10 kW, a morning of 40 kW over 10 kW of PV, a noon of 20 kW under 40 kW."""
    from test_fix_r5_genset import _day_shape, _farm_night

    return _day_shape(_farm_night(c_next=c_next))


# ------------------------------------------------------------------------------------------------ engine-plans-live-16

def _sunny_zero_export():
    """A zero-export day: 80 kW of PV over the 80 kW load from 11:00 to 16:00, the battery at 50 % at 00:00, and a calendar cost on the energy
    it holds (Rs 0.05 per kWh per step)."""
    p = _problem()
    T = p["T"]
    p["PV"] = np.zeros(T)
    p["PV"][44:64] = 160.0
    p["PVlo"] = p["PV"].copy()
    p["battery"]["b_cal"] = np.full(T, 0.05)
    return p


@pytest.mark.parametrize("calendar", [True, False], ids=["calendar_cost", "no_calendar_cost"])
def test_pv_is_never_curtailed_while_the_battery_has_room(calendar):
    """Faisalabad, 8 Nov 12:00: 54 to 67 kW curtailed from 12:00 to 13:45 with the battery flat at 78 % and charging 0, the charge left to 14:00
    inside the load-shedding window. Charging later saved Rs 20 of calendar ageing (and the order tie-break preferred a later charge) against the
    risk that the afternoon's PV never fills the pack. On site the PV surplus charges the battery whenever it has room."""
    p = _sunny_zero_export()
    if not calendar:
        p["battery"]["b_cal"] = np.zeros(p["T"])
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    s = r["sol"]
    E, Emax = np.asarray(s["E"]), 0.95 * 200.0
    curtailed = np.flatnonzero(s["K"] > 1e-3)
    assert curtailed.size                                                       # the battery fills, and PV beyond it is curtailed
    room = [t for t in curtailed if E[t + 1] < Emax - 1e-3 and s["C"][t] < p["battery"]["P_ch"] - 1e-3]
    assert not room, room                                                       # before: 80 kW curtailed from 11:00 with the battery at 10 %
    assert float(s["C"][44]) > 1.0                                              # the charge starts with the PV surplus


def test_curtailing_with_the_battery_full_still_comes_before_dumping_the_sets_output():
    """With a full battery the set's minimum-load surplus curtails PV first, then is dumped (twin.simulate's order): the price that keeps PV
    while the battery has room keeps a dump dearer than a curtailment."""
    p = _farm_day(c_next=0.0)
    p["battery"]["E0"] = 0.95 * 120.0
    p["battery"]["b_cal"] = np.full(p["T"], 0.08)
    p["genset"]["u_prev"], p["genset"]["elapsed"], p["genset"]["UT"] = 1, 0, 48    # carried on: every step forced, so a dump is allowed
    p["genset"]["control"] = "manual"
    r = optimise.solve(p)
    assert r["status"] in ("optimal", "feasible")
    s = r["sol"]
    W = np.asarray(s["W"])
    noon = np.arange(32, 48)
    assert float(np.sum(s["K"][noon])) > 0.0
    # a step that dumps the set's output has no PV left to curtail
    assert np.all((W[noon] < 1e-4) | (np.asarray(p["PV"])[noon] - s["K"][noon] < 1e-4))


# ------------------------------------------------------------------------------------------------ engine-optimiser-06

def test_without_genset_charging_the_set_still_follows_the_load_and_pv_charges_only_what_the_load_leaves():
    """The farm with gen_charge_allowed off: the follow cap max(f_min S, served - PV) was off with it, so on a morning of 40 kW over 10 kW of PV
    the plan ran the set at the whole 40 kW and PV charged the battery 10 kW: genset charging in net terms, which the site's inverter (PV serves
    the load first) never does. The cap now holds on every grid-down step, and the battery takes only the PV beyond the load."""
    p = _farm_day()
    p["battery"]["a_gen_surplus"] = False
    assert not optimise.genset_surplus_steps(p).any()
    r = optimise.solve(p)
    assert r["status"] in ("optimal", "feasible")
    s = r["sol"]
    on = s["u"] > 0.5
    L, PV = np.asarray(p["L"]), np.asarray(p["PV"])
    assert on[16:32].any()                                                      # the set carries the morning
    assert np.all(s["P"][on] <= np.maximum(14.4, (L - PV)[on]) + 1e-4)          # before: 40 kW against 30 kW to find
    assert np.all(s["C"] <= np.maximum(0.0, PV - L) + 1e-4)                     # before: 10 kW from PV while the set carried the load
    assert float(np.max(s["C"][16:32])) < 1e-4


def test_with_genset_charging_the_battery_takes_the_sets_minimum_load_surplus_and_no_more():
    """The flag decides only whether the minimum-load surplus reaches the battery (round 5): with it on, the night's surplus over 10 kW is stored,
    and the morning's set still follows the load."""
    p = _farm_day()
    r = optimise.solve(p)
    s = r["sol"]
    on = s["u"] > 0.5
    L, PV = np.asarray(p["L"]), np.asarray(p["PV"])
    assert np.all(s["P"][on] <= np.maximum(14.4, (L - PV)[on]) + 1e-4)
    assert np.allclose(s["C"][:16][on[:16]], 14.4 - 10.0, atol=1e-3)
