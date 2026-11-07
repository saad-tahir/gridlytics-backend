"""Round-4 regression tests for the savings group (verify.py, twin.py, demo.py): HANDOFF 8.A.2 (masked minutes whose grid and genset columns are both
blank price each arm on its own battery and genset flows against the read load) and HANDOFF 8.A.1 (the off-grid demo farm). Each test fails on the code
before its fix. Test data: the demo twin's generated days only; nothing here calls the network."""
from __future__ import annotations

import numpy as np
import pytest
from test_fix_r2_savings import GRID_COLS, NIGHT_CHARGER, D, P
from test_fix_r3_savings import BAT, GEN, SHIFTING, SOC, _score
from test_fix_savings import company, make_site, twin_day  # noqa: F401 (company is a fixture)

from gridlytics import sites, telemetry, twin, verify


def _tou_site(name, native_slots, table):
    """A closed-loop site whose native capture is the time-of-use table `native_slots` while the inverter ran `table` on the day before and on the day."""
    sid = make_site(name=name, tier="closed_loop", native={"source": "entered", "work_mode": "tou", "backup_soc_pct": 20, "slots": native_slots})
    pol = lambda s, ts: twin.table_policy(table, ts)  # noqa: E731
    _, sp, rows_p = twin_day(sid, P, 0.5, seed=1, policy=pol)
    _, sim, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2, policy=pol)
    return sid, rows_p, rows, sim


def _near(claim, metered):
    return abs(claim - metered) <= max(30.0, 0.02 * abs(metered))


# ------------------------------------------------------------------------------------------------ HANDOFF 8.A.2: grid and genset blank together

def test_masked_minutes_whose_load_is_read_price_each_arms_own_flows():
    mask = np.array([True, True, True, True, False])
    sim = {"imp": np.array([5.0, 5.0, 5.0, 5.0, 7.0]), "exp": np.array([1.0, 0.0, 0.0, 0.0, 2.0]), "bat": np.array([-10.0, 30.0, 4.0, 0.0, -1.0]),
           "gen": np.array([0.0, 0.0, 0.0, 20.0, 0.0]), "pv": np.array([2.0, 0.0, 50.0, 0.0, 0.0])}
    pv = np.array([2.0, 0.0, 50.0, 0.0, 0.0])
    load = np.array([40.0, 40.0, 30.0, 40.0, 40.0])
    out = verify._grid_masked(sim, mask, pv, load=load)
    # the charge on top of the load, the discharge that displaced import, the PV and discharge beyond the load exported, the genset's share
    assert out["imp"].tolist() == [48.0, 10.0, 0.0, 20.0, 7.0] and out["exp"].tolist() == [0.0, 0.0, 24.0, 0.0, 2.0]
    # an unread load keeps the charge-only rule (before this change every masked minute was priced so: [8, 0, 0, 0] and no export)
    out = verify._grid_masked(sim, mask, pv, load=np.array([np.nan, np.nan, 30.0, np.nan, 40.0]))
    assert out["imp"].tolist() == [8.0, 0.0, 0.0, 0.0, 7.0] and out["exp"].tolist() == [0.0, 0.0, 24.0, 0.0, 2.0]
    # nor where the actual arm's battery flow is unknown (no arm's flow is priced there)
    out = verify._grid_masked(sim, mask, pv, known=np.array([True, False, True, True, True]), load=load)
    assert out["imp"][1] == 0.0 and out["bat"][1] == 0.0
    assert verify._grid_masked(sim, mask, pv)["imp"].tolist() == [8.0, 0.0, 0.0, 0.0, 7.0]  # the old call shape


def test_a_grid_and_genset_gap_over_the_baselines_discharge_claims_the_metered_figure(company):
    # the verifier's mixed site: the native capture self-uses from 06:00 (it discharges) while the inverter holds; grid and genset blank 06:00-07:10
    # (4.9 % of the day, under the cap), so the genset is unknown and the balance cannot be closed
    sid, rows_p, rows, sim = _tou_site("Mixed r4", NIGHT_CHARGER, SHIFTING)
    assert abs(float(np.mean(sim["bat"][360:430]))) < 1.0  # the actual arm holds over the gap
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    assert metered > 300.0
    for a, b, before in [(360, 430, 3839.4), (360, 391, 2012.6)]:
        for cols in (GRID_COLS + GEN, GRID_COLS + GEN + BAT, GRID_COLS + GEN + BAT + SOC):
            r = _score(sid, rows_p, rows, lambda i, a=a, b=b: a <= i < b, cols)
            assert r["import_unknown"] is False and 0.0 < r["import_unresolved_share"] <= verify.GRID_UNRESOLVED_MAX
            assert _near(r["claimed_rs"], metered), (a, b, cols, r["claimed_rs"], metered, before)  # before: +3,839.4 and +2,012.6 against +560.5
    # the evening gap over the actual arm's own discharge (before: -2,935.8, an under-claim)
    r = _score(sid, rows_p, rows, lambda i: 1020 <= i < 1090, GRID_COLS + GEN)
    assert _near(r["claimed_rs"], metered), (r["claimed_rs"], metered)


def test_the_mirror_case_the_actual_discharges_in_the_gap_while_the_baseline_holds(company):
    # the native capture holds from 06:00 while the inverter self-uses (it discharges): the same gap now under-claimed by the actual arm's own
    # discharge (before: -3,839.4 against -560.5); and the evening gap over the baseline's discharge over-claimed (before: +2,935.8 against -560.5)
    sid, rows_p, rows, sim = _tou_site("Mirror r4", SHIFTING, NIGHT_CHARGER)
    assert float(np.mean(sim["bat"][360:430])) > 50.0
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    assert metered < -300.0
    for a, b, before in [(360, 430, -3839.4), (1020, 1090, 2935.8)]:
        r = _score(sid, rows_p, rows, lambda i, a=a, b=b: a <= i < b, GRID_COLS + GEN)
        assert r["import_unknown"] is False
        assert _near(r["claimed_rs"], metered), (a, b, r["claimed_rs"], metered, before)


# ------------------------------------------------------------------------------------------------ HANDOFF 8.A.1: the off-grid demo farm's carry-over

def test_the_gensets_night_load_ratio_is_its_output_over_the_dark_minutes():
    g = {"rated_kw": 48.0, "f_min": 0.3}
    series = {"pv": np.array([0.0, 0.0, 10.0, 20.0, 0.0]), "load": np.array([5.0, 30.0, 40.0, 40.0, 60.0])}
    # dark minutes 0, 1 and 4: the set's output is the load, held at its 14.4 kW minimum and its 48 kW rating
    assert verify.night_load_ratio(g, series) == pytest.approx((14.4 + 30.0 + 48.0) / 3 / 48.0)
    assert verify.night_load_ratio({"rated_kw": 0.0}, series) == 0.75


def test_an_off_grid_days_carry_over_is_valued_at_the_gensets_cost_at_its_night_load(company):
    # the demo farm: a kWh left at midnight serves the night, where the genset otherwise carries 8-21 kW near its 14.4 kW minimum; valued at its cost
    # at 75 % load (before: about Rs 128 a kWh), the per-day ledger ran Rs 2,600-12,200 ahead of 11-day continuous replays in four of five rebuilds
    from gridlytics import demo, optimise, sites, tariffs

    sid = next(i for i in demo.create_sites("t") if "farm" in sites.full(i)["name"].lower())
    _, sp, _ = twin_day(sid, P, 0.5, seed=1)
    twin_day(sid, D, float(sp["soc"][-1]), seed=2)
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    site = sites.full(sid)
    g, b = site["assets"]["genset"], site["assets"]["battery"]
    series, _ = verify._replay_inputs(site, D)
    ratio = verify.night_load_ratio(g, series)
    assert 0.3 <= ratio < 0.5
    gm = {"S": g["rated_kw"], "a": g["a"], "b": g["b"], "c_fuel": tariffs.fuel_price(g["fuel_kind"], D)[0], "c_om": g["c_om_rs_per_kwh"],
          "c_runhr": g["c_runhr_rs"]}
    at_night = b["eta_d"] * optimise._gen_marginal(gm, ratio) - optimise.wear_cost(b)
    at_75 = b["eta_d"] * optimise._gen_marginal(gm) - optimise.wear_cost(b)
    assert r["actual"]["v_end"] == pytest.approx(at_night, abs=0.01) and at_night > at_75 + 15.0
    assert r["levers"]["carry_over"] == pytest.approx((r["actual"]["e_end_kwh"] - r["baseline"]["e_end_kwh"]) * r["actual"]["v_end"], abs=1.0)


# ------------------------------------------------------------------------------------------------ round 4b: a blank genset that ran (savings-1)

def _site_day(name, native_slots, table, outage=None, gen_run=None, genset=None):
    """A closed-loop site whose native capture is the table `native_slots` while the inverter ran `table`; on the scored day the grid is down over
    `outage` and the plan asks the genset to run over `gen_run` (minute ranges)."""
    sid = make_site(name=name, tier="closed_loop", native={"source": "entered", "work_mode": "tou", "backup_soc_pct": 20, "slots": native_slots},
                    genset=genset)
    site = sites.full(sid)
    plant = twin.plant_of(site)
    out, soc0 = [], 0.5
    for day, seed in ((P, 1), (D, 2)):
        tr = twin.truth(site, day)
        pol = twin.table_policy(table, tr["ts"])
        if day == D and outage:
            tr["grid"][outage[0]:outage[1]] = 0.0
        if day == D and gen_run:
            pol["genset"][gen_run[0]:gen_run[1]] = 1
        sim = twin.simulate(tr, pol, plant, soc0)
        rows = twin.telemetry_rows(sid, tr, sim, rng_seed=seed, inverter_mode=pol["mode"])
        telemetry.write_rows(rows)
        soc0 = float(sim["soc"][-1])
        out.append(rows)
    return sid, out[0], out[1], sim


def test_a_blank_genset_that_carried_an_outage_is_read_from_the_balance(company):
    # the verifier's outage case: the native capture shifts while the inverter runs the night charger; the grid is down 18:00-19:00 and the set
    # carries the evening once the battery is at its floor. With its columns blank it read as off, so its fuel went unpriced on the actual arm alone
    sid, rows_p, rows, sim = _site_day("Outage r4b", SHIFTING, NIGHT_CHARGER, outage=(1080, 1140))
    assert int(sim["gen_on"][1080:1140].sum()) >= 30 and bool(sim["gen_on"][1139])
    m = verify.compute_day(sid, D, with_bound=False)["native"]
    assert m["claimed_rs"] < -1000.0 and m["genset_unknown"] is False
    for a, b, cols in [(1070, 1140, GRID_COLS + GEN), (1070, 1140, GRID_COLS + GEN + BAT), (1080, 1140, GEN)]:
        r = _score(sid, rows_p, rows, lambda i, a=a, b=b: a <= i < b, cols)
        # before: +4,357 against the metered -5,589 on all three (the 54-minute run of the verifier's draw: +2,535 against -7,600)
        assert r["claim_withheld"] is None and _near(r["claimed_rs"], m["claimed_rs"]), (a, b, cols, r["claimed_rs"], m["claimed_rs"])
        assert r["actual"]["litres"] == pytest.approx(m["actual"]["litres"], rel=0.02) and r["actual"]["genset_hours"] == m["actual"]["genset_hours"]
    # the set was running when the grid came back at 19:00: a grid-up gap from there may hide its run going on, so the day claims nothing
    r = _score(sid, rows_p, rows, lambda i: 1140 <= i < 1210, GRID_COLS + GEN)
    assert r["claimed_rs"] == 0.0 and r["claim_withheld"] == "genset_unknown" and r["genset_unknown"] is True and r["levers"]["genset"] is None


def test_a_parallel_run_under_blank_columns_is_read_from_the_balance_or_withheld(company):
    # a set that may run in parallel, which the plan runs 06:00-07:10 with the grid up (metered claim about -25,323: the set's fuel)
    gen = {"rated_kw": 200, "f_min": 0.3, "fuel_kind": "hsd", "min_up_h": 1.0, "parallel_allowed": True}
    sid, rows_p, rows, sim = _site_day("Parallel r4b", NIGHT_CHARGER, SHIFTING, gen_run=(360, 430), genset=gen)
    assert bool(sim["gen_on"][360:430].all()) and not sim["gen_on"][:360].any()
    m = verify.compute_day(sid, D, with_bound=False)["native"]
    assert m["claimed_rs"] < -10000.0
    # the grid metered and the genset blank: the balance closes and finds the run (before: +7,118)
    r = _score(sid, rows_p, rows, lambda i: 360 <= i < 430, GEN)
    assert r["claim_withheld"] is None and _near(r["claimed_rs"], m["claimed_rs"]), (r["claimed_rs"], m["claimed_rs"])
    # grid and genset blank: the balance cannot close and a parallel set may have run, so the day claims nothing (before: +560.5)
    for cols in (GRID_COLS + GEN, GRID_COLS + GEN + BAT, GRID_COLS + GEN + BAT + SOC):
        r = _score(sid, rows_p, rows, lambda i: 360 <= i < 430, cols)
        assert r["claimed_rs"] == 0.0 and r["claim_withheld"] in ("genset_unknown", "battery_flow_unknown"), (cols, r["claimed_rs"], r["claim_withheld"])
    assert _score(sid, rows_p, rows, lambda i: 360 <= i < 430, GRID_COLS + GEN)["claim_withheld"] == "genset_unknown"


def test_the_genset_balance_reads_a_run_only_where_it_closes():
    p = twin.Plant(gen_kw=100.0, gen_fmin=0.3, gen_a=0.25, gen_b=0.03)
    nan = np.nan
    g = {"gen_on": np.array([0.0, nan, nan, nan, nan, nan, 1.0]), "gen": np.array([0.0, nan, nan, nan, nan, nan, 60.0]),
         "fuel": np.array([0.0, nan, nan, nan, nan, nan, 0.3]), "imp": np.array([10.0, nan, nan, nan, 5.0, nan, nan]),
         "exp": np.full(7, nan), "load": np.array([50.0, 80.0, 90.0, 40.0, 45.0, 60.0, 70.0]), "pv": np.array([20.0, 0.0, 0.0, 0.0, 40.0, 0.0, 0.0])}
    f = {"grid": np.array([1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0]), "imp": np.nan_to_num(g["imp"]), "exp": np.zeros(7)}
    bat = np.array([20.0, 40.0, -30.0, 40.0, 0.0, 0.0, 10.0])
    closed, on, gen, fuel, unseen = verify._gen_from_balance(g, f, bat, p, np.array([False] * 6 + [True]))
    # outage minutes 1-3: 40 kW over the battery (on), 120 kW clipped to the 100 kW rating (on), nothing left over (off); minute 4 metered: 0 kW
    assert closed.tolist() == [False, True, True, True, True, False, False] and on.tolist() == [False, True, True, False, False, False, False]
    assert gen.tolist() == [0.0, 40.0, 100.0, 0.0, 0.0, 0.0, 0.0] and fuel[1] == pytest.approx((0.25 * 40 + 0.03 * 100) / 60)
    # minute 5 (grid up, unmetered) is open, and the set reads running on minute 6 just after it: it may have run unseen
    assert unseen is True
    g2 = {**g, "gen_on": np.array([0.0, nan, nan, nan, nan, nan, 0.0]), "gen": np.array([0.0, nan, nan, nan, nan, nan, 0.0]),
          "fuel": np.array([0.0, nan, nan, nan, nan, nan, 0.0])}
    assert verify._gen_from_balance(g2, f, bat, p, np.zeros(7, dtype=bool))[4] is False  # off either side, the grid up: off
    assert verify._gen_from_balance(g2, f, bat, twin.Plant(gen_kw=100.0, parallel=True), np.zeros(7, dtype=bool))[4] is True  # unless parallel
    assert verify._gen_from_balance(g2, f, np.array([20.0, nan, -30.0, 40.0, 0.0, 0.0, 10.0]), p, np.zeros(7, dtype=bool))[4] is True  # outage open


# ------------------------------------------------------------------------------------------------ round 4b: a spread across a price edge (savings-r4)

#: grid-charge at night, hold through the day, discharge 16:50-17:55 and hold through the evening peak (18:00-22:00)
STOPS = [{"start": "00:00", "mode": "charge_grid", "soc_floor_pct": 20.0, "soc_target_pct": 90.0, "grid_charge": True},
         {"start": "06:00", "mode": "hold", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False},
         {"start": "16:50", "mode": "discharge", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False},
         {"start": "17:55", "mode": "hold", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False},
         {"start": "22:00", "mode": "self_use", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False}]


def test_a_state_of_charge_spread_across_a_price_edge_puts_discharge_on_the_cheapest_minutes():
    p = twin.Plant(e_nom=100.0, p_ch=50.0, p_dis=50.0, eta_c=0.9, eta_d=0.8)
    n = 62
    soc = np.full(n, np.nan)
    soc[0], soc[61] = 0.60, 0.35  # 25 kWh out over 60 minutes: 20 kWh delivered, 20 kW evenly
    g = {"soc": soc, "soc_bms": np.full(n, np.nan), "bat": np.full(n, np.nan)}
    g["bat"][61] = 0.0
    need = np.ones(n, dtype=bool)
    need[0] = need[61] = False
    price = np.where(np.arange(n) < 41, 28.1, 41.15)  # the peak from minute 41
    even = verify._bat_from_soc(g, p, need, max_span=None)
    assert np.allclose(even[1:61], 20.0)  # the old spread: a third of it in the peak
    out = verify._bat_from_soc(g, p, need, max_span=None, price=price)
    assert np.allclose(out[1:41], 30.0) and np.allclose(out[41:61], 0.0)  # all on the 40 off-peak minutes, within 50 kW
    assert np.sum(out[1:61]) == pytest.approx(np.sum(even[1:61]))
    soc[61] = 0.85  # a 25 kWh charge: on the dearest minutes first, at most 50 kW, the rest on the cheaper ones
    out = verify._bat_from_soc(g, p, need, max_span=None, price=price)
    assert np.allclose(out[41:61], -50.0) and np.allclose(out[1:41], -(25 / 0.9 * 60 - 50 * 20) / 40)
    assert np.allclose(verify._bat_from_soc(g, p, need, max_span=None, price=np.full(n, 28.1)), verify._bat_from_soc(g, p, need, max_span=None),
                       equal_nan=True)  # one price: the even spread as before


def test_a_gap_over_18_00_on_a_table_that_stops_discharging_mid_gap_claims_the_metered_figure(company):
    # the verifier's site: the inverter discharges 16:50-17:55 and holds through the peak; the metered claim is nil. With battery power and state
    # of charge blank too, the actual arm's flow over the gap is a state-of-charge spread, which moved about 25 kWh across 18:00 into the peak
    sid, rows_p, rows, sim = _tou_site("Stops r4b", NIGHT_CHARGER, STOPS)
    assert float(np.mean(sim["bat"][1010:1075])) > 50.0 and abs(float(np.mean(sim["bat"][1080:1200]))) < 1.0
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    for a, b, before in [(1040, 1110, 326.1), (1030, 1100, 279.5), (1020, 1090, 170.8), (1050, 1120, 310.6)]:
        # the masked path (grid and genset blank too) and the spanned path of the round-3 rebuild (the genset read)
        for cols in (GRID_COLS + GEN + BAT + SOC, GRID_COLS + BAT + SOC):
            r = _score(sid, rows_p, rows, lambda i, a=a, b=b: a <= i < b, cols)
            assert r["import_unknown"] is False and _near(r["claimed_rs"], metered), (a, b, cols, r["claimed_rs"], metered, before)
    # the writer's own mixed site over its battery's run-out at about 18:25 (before: +692.9 against +560.5)
    sid, rows_p, rows, _ = _tou_site("Mixed r4b", NIGHT_CHARGER, SHIFTING)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    r = _score(sid, rows_p, rows, lambda i: 1050 <= i < 1120, GRID_COLS + GEN + BAT + SOC)
    assert _near(r["claimed_rs"], metered), (r["claimed_rs"], metered)


# ------------------------------------------------------------------------------------------------ round 4b: the demo history plans 48 hours (savings-2)

def test_the_demo_history_plans_48_hours_as_the_live_ticks_do(company, monkeypatch):
    from gridlytics import alerts, demo, forecast, plans

    seen = []
    monkeypatch.setattr(plans, "tick", lambda sid, at=None, steps=192, **k: seen.append(steps) or {})
    monkeypatch.setattr(demo, "_simulate_span", lambda *a, **k: 0)
    monkeypatch.setattr(verify, "compute_day", lambda *a, **k: None)
    monkeypatch.setattr(forecast, "score_day", lambda *a, **k: None)
    monkeypatch.setattr(alerts, "evaluate", lambda *a, **k: None)
    sid = next(i for i in demo.create_sites("t") if "farm" in sites.full(i)["name"].lower())
    demo.build_history(sid, 5)
    assert len(seen) == 4 and set(seen) == {192}  # before: 96-step (24-hour) plans for the history days and this morning's plan
