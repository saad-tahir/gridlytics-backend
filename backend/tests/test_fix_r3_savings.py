"""Round-3 regression tests for the savings group (verify.py, twin.py, demo.py, tariffs.py): the import-completeness residuals the round-2 verifiers left
open, and the web app's asks (demo readings never after now, the litres' source, the stored bill row). Each test fails on the code before its fix."""
from __future__ import annotations

import time
from datetime import date, timedelta, timezone

import numpy as np
import pytest
from test_fix_r2_savings import GRID_COLS, NIGHT_CHARGER, D, P, _blank_where, _flat_rows
from test_fix_savings import company, make_site, twin_day  # noqa: F401 (company is a fixture)

from gridlytics import demo, sites, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))
SOC = ("battery_soc_est_pct", "battery_soc_bms_pct")
BAT = ("battery_power_w",)
GEN = ("genset_running", "genset_power_w", "genset_fuel_l")
#: the verifier's shifting table: grid-charge at night, hold through the day, discharge into the evening peak
SHIFTING = [{"start": "00:00", "mode": "charge_grid", "soc_floor_pct": 20.0, "soc_target_pct": 90.0, "grid_charge": True},
            {"start": "06:00", "mode": "hold", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False},
            {"start": "17:00", "mode": "discharge", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False},
            {"start": "22:00", "mode": "self_use", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False}]


def _table_site(name, table):
    """A closed-loop site whose inverter ran `table` on the day before and on the day (the native capture is self-use)."""
    sid = make_site(name=name, tier="closed_loop")
    pol = lambda s, ts: twin.table_policy(table, ts)  # noqa: E731
    _, sp, rows_p = twin_day(sid, P, 0.5, seed=1, policy=pol)
    _, _, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2, policy=pol)
    return sid, rows_p, rows


def _score(sid, rows_p, rows, where, cols):
    telemetry.delete_site(sid)
    telemetry.write_rows(rows_p + _blank_where(rows, where, *cols))
    return verify.compute_day(sid, D, with_bound=False)["native"]


def _claims_metered_or_is_withheld(r, metered):
    if r["import_unknown"]:
        return r["claimed_rs"] == 0.0 and r["claim_withheld"] == "import_unknown"
    return abs(r["claimed_rs"] - metered) <= max(30.0, 0.02 * abs(metered))


# ------------------------------------------------------------------------------------------------ routed-import-completeness residual (a)

def test_the_battery_flow_is_taken_from_the_state_of_charge_only_over_short_spans():
    p = twin.Plant(e_nom=100.0, p_ch=50.0, p_dis=50.0, eta_c=0.9, eta_d=0.8)
    n = 62
    soc = np.full(n, np.nan)
    soc[0], soc[20], soc[61] = 0.50, 0.52, 0.40  # states 20 minutes apart, then 41 minutes apart
    g = {"soc": soc, "soc_bms": np.full(n, np.nan), "bat": np.full(n, np.nan)}
    need = np.ones(n, dtype=bool)
    need[0] = need[61] = False
    g["bat"][61] = 0.0
    out = verify._bat_from_soc(g, p, need)
    assert np.allclose(out[1:21], -2.0 / 0.9 / (20 / 60))  # 2 kWh stored over 20 minutes
    assert np.isnan(out[21:61]).all()  # before: the 12 kWh drop spread evenly over 40 minutes
    assert verify.SOC_SPAN_MAX_MIN == 30
    assert not np.isnan(verify._bat_from_soc(g, p, need, max_span=None)[21:61]).any()  # the old behaviour on request


def test_a_day_whose_grid_battery_and_soc_columns_drop_out_for_hours_claims_the_metered_figure_or_nothing(company):
    # the verifier's night-charger table (metered claim about -1,983): grid, battery power and state of charge all blank for the first 30% of the day
    sid, rows_p, rows = _table_site("Night charger r3", NIGHT_CHARGER)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    assert metered < -1000.0
    r = _score(sid, rows_p, rows, lambda i: i < 432, GRID_COLS + BAT + SOC)
    assert _claims_metered_or_is_withheld(r, metered), r["claimed_rs"]  # before: -318.5, the round trip inside the gap smeared away
    assert r["import_unknown"] is True and r["import_unresolved_share"] == pytest.approx(0.3, abs=0.005)
    # the state of charge read every 15 minutes still rebuilds the gap, and the day claims what the metered day does
    telemetry.delete_site(sid)
    blank = _blank_where(rows, lambda i: i < 432, *(GRID_COLS + BAT))
    telemetry.write_rows(rows_p + _blank_where(blank, lambda i: i < 432 and i % 15 != 14, *SOC))
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["import_unknown"] is False and r["import_unresolved_share"] == 0.0
    assert r["claimed_rs"] == pytest.approx(metered, abs=max(30.0, 0.02 * abs(metered)))


def test_an_evening_gap_over_the_discharge_is_withheld_not_smeared_into_the_peak(company):
    sid, rows_p, rows = _table_site("Shifting r3", SHIFTING)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    r = _score(sid, rows_p, rows, lambda i: 960 <= i < 1380, GRID_COLS + BAT + SOC)  # 16:00-23:00 blank
    assert _claims_metered_or_is_withheld(r, metered), (r["claimed_rs"], metered)  # before: -917.4 against the metered -1,422.5


# ------------------------------------------------------------------------------------------------ routed-import-completeness residual (b)

def test_the_actual_arms_grid_charging_under_masked_minutes_is_priced(company):
    # grid and genset columns blank 00:00-01:10 (4.9% of the day, under the 5% cap) over a 100 kW grid charge: the genset's state is unknown, so the
    # balance cannot be closed and the minutes are masked; the battery's charge that PV does not cover is still import on every arm
    sid, rows_p, rows = _table_site("Masked charge r3", NIGHT_CHARGER)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    r = _score(sid, rows_p, rows, lambda i: i < 70, GRID_COLS + GEN)
    assert r["import_unknown"] is False and 0.0 < r["import_unresolved_share"] <= verify.GRID_UNRESOLVED_MAX
    assert r["claimed_rs"] == pytest.approx(metered, abs=max(30.0, 0.02 * abs(metered)))  # before: +1,295.9 against -1,983.0
    assert r["actual"]["import_kwh"] > 100.0  # the charge's import is on the actual arm


def test_masked_minutes_price_only_the_charge_pv_does_not_cover():
    mask = np.array([True, True, True, False])
    sim = {"imp": np.array([5.0, 5.0, 5.0, 7.0]), "exp": np.array([1.0, 0.0, 0.0, 2.0]), "bat": np.array([-10.0, -3.0, 4.0, -1.0])}
    pv = np.array([2.0, 5.0, 0.0, 0.0])
    out = verify._grid_masked(sim, mask, pv)
    assert out["imp"].tolist() == [8.0, 0.0, 0.0, 7.0] and out["exp"].tolist() == [0.0, 0.0, 0.0, 2.0]
    assert verify._grid_masked(sim, mask)["imp"].tolist() == [0.0, 0.0, 0.0, 7.0]  # the old call shape


# ------------------------------------------------------------------------------------------------ round 3b: masked minutes priced on the actual arm's own flow
# (regression-soc-span-short-gap-overclaims, regression-masked-charge-priced-on-baselines-only)

#: a site whose native capture is the night charger the inverter runs: the actual arm is the native arm, so the true saving is nil
NATIVE_TOU = {"source": "entered", "work_mode": "tou", "backup_soc_pct": 20, "slots": NIGHT_CHARGER}


def _native_tou_site(name):
    sid = make_site(name=name, tier="closed_loop", native=NATIVE_TOU)
    _, sp, rows_p = twin_day(sid, P, 0.5, seed=1)
    _, sim, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2)
    return sid, rows_p, rows, sim


def _claim(sid, rows):
    telemetry.delete_site(sid)
    telemetry.write_rows(rows)
    return verify.compute_day(sid, D, with_bound=False)["native"]


def test_masked_minutes_price_the_charge_and_wear_only_where_the_actual_arms_battery_flow_is_known():
    mask = np.array([True, True, True, False])
    sim = {"imp": np.array([5.0, 5.0, 5.0, 7.0]), "exp": np.array([1.0, 0.0, 0.0, 2.0]), "bat": np.array([-10.0, -3.0, 4.0, -1.0])}
    pv = np.array([2.0, 0.0, 0.0, 0.0])
    known = np.array([True, False, True, True])
    out = verify._grid_masked(sim, mask, pv, known=known)  # a baseline: its own flow, priced only where the actual arm's is known
    assert out["imp"].tolist() == [8.0, 0.0, 0.0, 7.0] and out["bat"].tolist() == [-10.0, 0.0, 4.0, -1.0]
    flow = np.array([-6.0, np.nan, 2.0, 99.0])  # the actual arm: its flow from the state of charge stands in on the masked minutes
    out = verify._grid_masked(sim, mask, pv, flow, known)
    assert out["imp"].tolist() == [4.0, 0.0, 0.0, 7.0] and out["bat"].tolist() == [-6.0, 0.0, 2.0, -1.0]
    assert verify._grid_masked(sim, mask, pv)["bat"] is sim["bat"]  # the old call shape leaves the flow alone


def _minutes(n, **cols):
    """A minute grid (`g`) and its filled replay inputs (`f`) of n minutes, every column blank unless given, the grid present throughout."""
    names = ("imp", "exp", "load", "pv", "bat", "gen", "gen_on", "fuel", "soc", "soc_bms")
    g = {k: np.asarray(cols.get(k, np.full(n, np.nan)), dtype=float) for k in names}
    f = {k: np.nan_to_num(g[k]) for k in names}
    f["grid"] = np.ones(n)
    return g, f


def test_the_actual_arms_flow_over_masked_minutes_comes_from_the_state_of_charge_with_no_span_limit():
    p = twin.Plant(e_nom=100.0, p_ch=50.0, p_dis=50.0, eta_c=0.9, eta_d=0.8)
    n = 60
    soc = np.full(n, np.nan)
    soc[0], soc[50] = 0.5, 0.6  # 50 minutes apart, beyond SOC_SPAN_MAX_MIN; nothing read after minute 50
    bat = np.full(n, np.nan)
    bat[0] = 0.0
    g, f = _minutes(n, soc=soc, bat=bat)
    unresolved = np.ones(n, dtype=bool)
    unresolved[0] = False
    none = np.zeros(n, dtype=bool)
    flow, known = verify._masked_charge(g, f, np.zeros(n), none, unresolved, True, False, p, None)
    assert np.allclose(flow[1:51], -10.0 / 0.9 / (50 / 60))  # 10 kWh stored over 50 minutes
    assert known[:51].all() and not known[51:].any()  # no state after minute 50 brackets the rest
    assert verify._masked_charge(g, f, np.zeros(n), none, unresolved, False, False, p, None)[1].all()  # no battery: nothing to price


def test_a_short_gap_in_the_grid_battery_and_soc_columns_claims_the_metered_figure(company):
    # gaps of 45 and 70 minutes, under the 5% cap, with the state of charge blank too: the states either side are more than SOC_SPAN_MAX_MIN apart, so
    # the minutes are masked, and the actual arm's grid charge under them, its battery power blank and read as zero, went unbilled
    sid, rows_p, rows = _table_site("Night charger r3b", NIGHT_CHARGER)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    assert metered < -1000.0
    for n_min in (45, 70):
        r = _score(sid, rows_p, rows, lambda i, n=n_min: i < n, GRID_COLS + BAT + SOC)
        assert r["import_unknown"] is False and 0.0 < r["import_unresolved_share"] <= verify.GRID_UNRESOLVED_MAX
        assert r["claimed_rs"] == pytest.approx(metered, abs=max(30.0, 0.02 * abs(metered))), n_min  # before: +124.8 and +1,295.9 against -1,983.0
    sid, rows_p, rows = _table_site("Shifting r3b", SHIFTING)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    r = _score(sid, rows_p, rows, lambda i: i < 70, GRID_COLS + BAT + SOC)
    assert r["claimed_rs"] == pytest.approx(metered, abs=max(30.0, 0.02 * abs(metered)))  # before: +1,856.4 against -1,422.5


def test_a_native_night_charger_site_claims_nothing_over_masked_minutes_whose_battery_power_is_blank(company):
    # the inverter runs the native capture, so the true saving is nil; before, each baseline's grid charge was priced over the masked minutes while
    # the actual arm's, its battery power blank and read as zero, was not
    sid, rows_p, rows, sim = _native_tou_site("Native TOU r3b")
    assert np.asarray(sim["bat"])[:70].mean() < -50.0  # grid-charging over 00:00-01:10
    assert abs(_claim(sid, rows_p + rows)["claimed_rs"]) < 1.0
    for label, rr, before in [("grid, genset and battery power blank 00:00-01:10", rows_p + _blank_where(rows, lambda i: i < 70, *(GRID_COLS + GEN + BAT)),
                               3278.9),
                              ("grid, battery power and soc blank 00:00-01:10", rows_p + _blank_where(rows, lambda i: i < 70, *(GRID_COLS + BAT + SOC)),
                               3278.9),
                              ("logger offline 00:00-01:00", rows_p + rows[60:], 2810.5),
                              ("logger offline 00:00-04:00", rows_p + rows[240:], 4452.4)]:
        r = _claim(sid, rr)
        assert r["import_unknown"] is False
        assert abs(r["claimed_rs"]) <= 30.0, (label, r["claimed_rs"], before)
    # a source writing every other minute: the gap minutes are masked; the claim may fall short but never runs ahead (before: +972.4)
    r = _claim(sid, [x for i, x in enumerate(rows_p) if i % 2 == 0] + [x for i, x in enumerate(rows) if i % 2 == 0])
    assert r["claimed_rs"] <= 30.0


def test_the_actual_arms_discharge_over_masked_minutes_is_worn_as_each_baselines_is(company):
    # the morning self-use discharge under a gap: each baseline's discharge there was worn and the actual arm's, its battery power blank, was not
    sid, rows_p, rows, sim = _native_tou_site("Native TOU wear r3b")
    assert np.asarray(sim["bat"])[360:430].mean() > 50.0  # discharging over 06:00-07:10
    for cols in (GRID_COLS + BAT + SOC, GRID_COLS + GEN + BAT):
        r = _claim(sid, rows_p + _blank_where(rows, lambda i: 360 <= i < 430, *cols))
        assert r["import_unknown"] is False and r["import_unresolved_share"] > 0.0
        assert abs(r["claimed_rs"]) <= 30.0, (cols, r["claimed_rs"])  # before: +1,263.9, the baseline's wear over the gap
        assert r["actual"]["wear_rs"] == pytest.approx(r["baseline"]["wear_rs"], abs=30.0)


# ------------------------------------------------------------------------------------------------ round 3c: a long state-of-charge span priced on the balance,
# the masked charge settled by the balance around it, and the actual arm's wear where its battery power is blank (regression-soc-span-short-gap-overclaims,
# regression-short-gap-discharge-underclaims, regression-masked-charge-priced-on-baselines-only, savings-wear-blank-battery-power-overclaims)

def _mixed_site(name):
    """The verifier's mixed site: the native capture grid-charges at night and self-uses from 06:00, while the inverter runs the shifting table."""
    sid = make_site(name=name, tier="closed_loop", native=NATIVE_TOU)
    pol = lambda s, ts: twin.table_policy(SHIFTING, ts)  # noqa: E731
    _, sp, rows_p = twin_day(sid, P, 0.5, seed=1, policy=pol)
    _, _, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2, policy=pol)
    return sid, rows_p, rows


def test_a_battery_flow_spread_over_a_long_span_is_rebuilt_on_the_balance_and_counted_toward_the_cap():
    p = twin.Plant(e_nom=100.0, p_ch=50.0, p_dis=50.0, eta_c=0.9, eta_d=0.8)
    n = 62
    gap = np.zeros(n, dtype=bool)
    gap[1:41] = True  # grid and battery power blank over minutes 1-40; the states at minutes 0 and 40 are 40 minutes apart
    soc = np.full(n, np.nan)
    soc[0], soc[40] = 0.5, 0.46  # 4 kWh given out over 40 minutes
    g, f = _minutes(n, imp=np.where(gap, np.nan, 5.0), exp=np.where(gap, np.nan, 0.0), load=np.full(n, 10.0), bat=np.where(gap, np.nan, 0.0),
                    soc=soc)
    imp, exp, bat, rebuilt, unresolved, spanned = verify._grid_flows(g, f, True, False, p, None)
    assert rebuilt[1:41].all() and spanned[1:41].all() and not unresolved.any()  # before: unresolved, so no arm's load was priced over it
    assert np.allclose(bat[1:41], 4.0 * 0.8 / (40 / 60)) and np.allclose(imp[1:41], 10.0 - 4.8)
    soc[20] = 0.48  # a state read halfway: two spans of 20 minutes, rebuilt and not counted
    g["soc"] = soc
    _, _, _, rebuilt, unresolved, spanned = verify._grid_flows(g, f, True, False, p, None)
    assert rebuilt[1:41].all() and not spanned.any() and not unresolved.any()


def test_the_masked_charge_is_what_the_states_leave_over_once_the_balance_settles_the_metered_minutes():
    p = twin.Plant(e_nom=100.0, p_ch=100.0, p_dis=100.0, eta_c=0.9, eta_d=0.8)
    n = 120
    masked = np.zeros(n, dtype=bool)
    masked[:30] = True  # a whole-row gap: the battery charged at 60 kW (27 kWh stored)
    blank_bat = np.arange(n) < 90  # its power stays blank to minute 90 while it discharges 10 kW into a 10 kW load (12.5 kWh)
    soc = np.full(n, np.nan)
    soc[90] = 0.5 + 0.27 - 0.125
    g, f = _minutes(n, imp=np.where(masked, np.nan, 0.0), exp=np.where(masked, np.nan, 0.0), load=np.where(masked, np.nan, 10.0),
                    bat=np.where(blank_bat, np.nan, 0.0), soc=soc)
    none = np.zeros(n, dtype=bool)
    flow, known = verify._masked_charge(g, f, np.zeros(n), none, masked, True, False, p, 0.5)
    assert known.all()
    assert np.allclose(flow[:30], -60.0)  # before: -10.7, the 14.5 kWh net spread over all 90 blank minutes
    assert np.allclose(flow[30:90], 10.0)  # the metered minutes' flow from their balance
    # the load unread past the masked run as well (60 more minutes no balance settles): the masked minutes' flow is unknown, so no arm's charge is
    # priced there, and the actual arm's wear on the other 60 minutes is unknown
    g["load"] = np.where(blank_bat, np.nan, 10.0)
    flow, known = verify._masked_charge(g, {**f, "load": np.nan_to_num(g["load"])}, np.zeros(n), none, masked, True, False, p, 0.5)
    assert not known[:90].any() and known[90:].all()


def test_an_hour_long_gap_in_the_grid_battery_and_soc_columns_claims_the_metered_figure_on_every_site(company):
    gbs = GRID_COLS + BAT + SOC
    sid, rows_p, rows = _table_site("Night charger r3c", NIGHT_CHARGER)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    r = _score(sid, rows_p, rows, lambda i: 360 <= i < 430, gbs)  # 06:00-07:10, over the morning self-use discharge
    assert r["import_unknown"] is False and 0.04 < r["import_unresolved_share"] <= verify.GRID_UNRESOLVED_MAX
    assert r["claimed_rs"] == pytest.approx(metered, abs=max(30.0, 0.02 * abs(metered))), (r["claimed_rs"], metered)  # before: -5,261.9 against -1,983.0
    sid, rows_p, rows = _table_site("Shifting r3c", SHIFTING)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    r = _score(sid, rows_p, rows, lambda i: 1020 <= i < 1090, gbs)  # 17:00-18:10, over the evening discharge
    assert r["claimed_rs"] == pytest.approx(metered, abs=max(30.0, 0.02 * abs(metered))), (r["claimed_rs"], metered)  # before: -4,918.8 against -1,422.5
    sid, rows_p, rows = _mixed_site("Mixed r3c")
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    assert metered > 300.0
    for a, b, before in [(360, 391, 2012.6), (360, 430, 3839.4), (1020, 1090, -2935.8)]:  # the native arm discharges at 06:00, the actual at 17:00
        r = _score(sid, rows_p, rows, lambda i, a=a, b=b: a <= i < b, gbs)
        assert r["import_unknown"] is False
        assert r["claimed_rs"] == pytest.approx(metered, abs=max(30.0, 0.02 * abs(metered))), (a, b, r["claimed_rs"], metered, before)
    r = _score(sid, rows_p, rows, lambda i: i < 432, gbs)  # 30% of the day: still withheld
    assert r["import_unknown"] is True and r["claimed_rs"] == 0.0


def test_a_battery_gap_that_outlasts_a_logger_gap_claims_nothing_on_a_site_running_its_native_capture(company):
    sid, rows_p, rows, _ = _native_tou_site("Native TOU r3c")
    for label, rr, before in [("offline 00:00-01:10, battery blank to 02:00", _blank_where(rows[70:], lambda i: i < 50, *(BAT + SOC)), 681.7),
                              ("offline 00:00-01:10, battery blank to 06:00", _blank_where(rows[70:], lambda i: i < 290, *(BAT + SOC)), 2413.2),
                              ("offline 00:00-02:00, battery blank to 08:00", _blank_where(rows[120:], lambda i: i < 360, *(BAT + SOC)), 6001.3),
                              ("grid blank 00:00-01:10, battery blank to 06:00",
                               _blank_where(_blank_where(rows, lambda i: i < 360, *(BAT + SOC)), lambda i: i < 70, *GRID_COLS), 2413.2)]:
        r = _claim(sid, rows_p + rr)
        assert r["import_unknown"] or abs(r["claimed_rs"]) <= 30.0, (label, r["claimed_rs"], before)
    sid, rows_p, rows = _mixed_site("Mixed r3c offline")
    metered = _claim(sid, rows_p + rows)["claimed_rs"]
    r = _claim(sid, rows_p + _blank_where(rows[70:], lambda i: i < 50, *(BAT + SOC)))
    assert r["claimed_rs"] == pytest.approx(metered, abs=max(30.0, 0.02 * abs(metered))), (r["claimed_rs"], metered)  # before: +1,242.2 against +560.5


def test_the_actual_arms_wear_is_taken_from_its_state_of_charge_where_its_battery_power_is_blank(company):
    sid, rows_p, rows, _ = _native_tou_site("Native TOU wear r3c")
    r = _claim(sid, rows_p + _blank_where(rows, lambda i: True, *BAT))  # battery power blank all day, the state of charge read every minute
    assert abs(r["claimed_rs"]) <= 30.0, r["claimed_rs"]  # before: +1,548.9, the baseline's whole wear
    assert r["levers"]["wear"] == pytest.approx(0.0, abs=30.0) and r["actual"]["discharge_kwh"] > 50.0
    sid, rows_p, rows = _table_site("Night charger wear r3c", NIGHT_CHARGER)
    metered = _claim(sid, rows_p + rows)
    r = _claim(sid, rows_p + _blank_where(rows, lambda i: True, *BAT))
    assert r["claimed_rs"] == pytest.approx(metered["claimed_rs"], abs=30.0)  # before: -434.1 against -1,983.0
    assert r["levers"]["wear"] == pytest.approx(metered["levers"]["wear"], abs=30.0)
    # the inverter's columns (load, PV, battery power and state of charge) blank 10:00-11:00 under a working grid meter: no balance settles the battery's
    # flow and no state brackets it closely, so the actual arm's wear is unknown and the lever is withheld, as an unknown genset's is
    inv = ("load_power_w", "pv_power_w") + BAT + SOC
    r = _claim(sid, rows_p + _blank_where(rows, lambda i: 600 <= i < 660, *inv))
    assert r["levers"]["wear"] is None and r["import_unknown"] is False
    # ...and the day claims nothing: withholding the wear lever alone would credit the baseline's whole wear when the actual arm cycled more
    assert r["claimed_rs"] == 0.0 and r["claim_withheld"] == "battery_flow_unknown"
    r = _claim(sid, rows_p + _blank_where(rows, lambda i: 600 <= i < 620, *inv))  # 20 minutes: spread from the states either side
    assert r["claim_withheld"] is None
    assert r["levers"]["wear"] == pytest.approx(metered["levers"]["wear"], abs=30.0)


# ------------------------------------------------------------------------------------------------ ask: demo telemetry ahead of the clock

def _demo_site(name):
    return make_site(name=name, tier="advisory_writes", demo_link=demo.DEMO_LINK, control="none", genset=False)


def test_the_live_demo_never_stamps_a_reading_after_now(company):
    sid = _demo_site("Demo clock")
    now = int(time.time()) // 60 * 60
    demo.advance(sid, now - 3600)
    assert telemetry.latest(sid)["ts"] < now
    demo.advance(sid, now + 7200)  # a now later than the clock is the clock
    assert telemetry.latest(sid)["ts"] <= int(time.time())  # before: the twin ran on to now + 2 h
    # a span asked past the clock (build_history's path) stops at the current minute
    telemetry.delete_site(sid)
    demo._simulate_span(sites.full(sid), now - 1800, now + 3600, None)
    assert telemetry.latest(sid)["ts"] <= int(time.time())


def test_twin_readings_left_ahead_of_the_clock_are_dropped_on_the_next_advance(company):
    # minutes written while the clock was ahead (the web app saw last_data 02:20 at 00:30): the next advance drops them, so the last reading is not
    # in the future
    sid = _demo_site("Demo ahead")
    now = int(time.time()) // 60 * 60
    rows = [(sid, t, "twin", 1.0, 1000.0) for t in range(now - 7200, now + 110 * 60, 60)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "load_power_w"))
    telemetry.write_rows([(sid, now + 200 * 60, "csv", 1.0, 1000.0)], ("site_id", "ts", "source_id", "grid_present", "load_power_w"))
    demo.advance(sid)
    ahead = telemetry.raw(sid, now + 60, now + 300 * 60, ("load_power_w",))["ts"]
    assert ahead.tolist() == [now + 200 * 60]  # before: 109 twin minutes after now; another source's reading is not the twin's to drop
    telemetry.delete_site(sid)
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "load_power_w"))
    demo.advance(sid)
    assert now - 7200 <= telemetry.latest(sid)["ts"] <= int(time.time())


# ------------------------------------------------------------------------------------------------ ask: energy.litres_source

def _genset_rows(sid, day, fuel_blank=False, power_blank=False):
    """60 minutes of a genset at 100 kW from 10:00 with 0.5 L a minute metered, then 30 minutes from 11:40 whose fuel reading (and power reading)
    may be blank; the genset reads off at every other minute of the day."""
    t0 = twin.day_start_utc(day)
    rows = []
    for m in range(1440):
        on = 600 <= m < 660 or 700 <= m < 730
        late = 700 <= m < 730
        fuel = None if fuel_blank and late else 0.5 if on else 0.0
        power = None if power_blank and late else 100_000.0 if on else 0.0
        rows.append((sid, t0 + 60 * m, "csv", 1.0, 1.0 if on else 0.0, power, fuel))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "genset_running", "genset_power_w", "genset_fuel_l"))


def test_the_litres_say_whether_they_were_metered_or_taken_from_the_fuel_curve(company):
    sid = make_site(name="Litres")
    _genset_rows(sid, D)
    u = verify.litres_used(sid, D.isoformat(), D.isoformat())
    assert u["litres_source"] == "meter" and u["litres"] == pytest.approx(45.0) and u["litres_curve"] == 0.0
    telemetry.delete_site(sid)
    _genset_rows(sid, D, fuel_blank=True)
    u = verify.litres_used(sid, D.isoformat(), D.isoformat())
    g = sites.full(sid)["assets"]["genset"]
    curve = 30 * (g["a"] * 100.0 + g["b"] * g["rated_kw"]) / 60
    assert u["litres_source"] == "curve" and u["litres_metered"] == pytest.approx(30.0) and u["litres"] == pytest.approx(30.0 + curve, abs=0.1)
    # running with neither power nor litres read: the power from the balance, here the genset's minimum load (nothing else is read)
    telemetry.delete_site(sid)
    _genset_rows(sid, D, fuel_blank=True, power_blank=True)
    u = verify.litres_used(sid, D.isoformat(), D.isoformat())
    curve = 30 * (g["a"] * g["f_min"] * g["rated_kw"] + g["b"] * g["rated_kw"]) / 60
    assert u["litres_source"] == "curve" and u["litres_curve"] == pytest.approx(curve, abs=0.1)
    # unknown: no genset signal at all in the period, or no genset
    assert verify.litres_used(sid, "2026-09-01", "2026-09-02")["litres_source"] is None
    other = make_site(name="No genset", genset=False)
    _genset_rows(other, D)
    assert verify.litres_used(other, D.isoformat(), D.isoformat())["litres_source"] is None


# ------------------------------------------------------------------------------------------------ ask: add_bill answers the stored row

def test_entering_a_bill_answers_the_row_the_bill_list_shows(company):
    sid = make_site(name="Bill row")
    _flat_rows(sid, date(2026, 10, 20), 31, 100.0, step_min=15)
    got = verify.add_bill(sid, {"previous_reading_date": "2026-10-20", "period_end": "2026-11-19", "total_rs": 2_000_000, "reference": "B-1"}, "t")
    listed = verify.bills(sid)[0]
    assert set(listed) <= set(got)  # before: only id, real, rebuilt and difference_pct
    assert {k: got[k] for k in listed} == listed
    assert got["period_start"] == "2026-10-21" and got["period_end"] == "2026-11-19" and got["connection_id"] == sites.connections(sid)[0]["id"]
    assert got["real"]["reference"] == "B-1" and got["difference_pct"] == listed["difference_pct"]
