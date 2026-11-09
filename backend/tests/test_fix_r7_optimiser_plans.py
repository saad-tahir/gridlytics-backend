"""Regressions for round 7's audit findings on the optimiser and plan building (work package optimiser-plans): no load shed with the grid up, the
plan's own MDI cap per step, grid charging in the peak window and the reason rules."""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from gridlytics import forecast, optimise, plans, sites, store, twin

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
