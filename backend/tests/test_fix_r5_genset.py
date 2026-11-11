"""Round-5 regressions for the optimiser group (HANDOFF 8.A.1, the off-grid demo farm). With the grid down a genset's output follows the load,
never under its minimum, and what that minimum leaves over charges the battery on its own (twin.simulate's outage branch): a plan may run the set
under a load below its minimum and count on that surplus, but never on charging from the set at full power, and never with the grid up. The
off-grid terminal value prices stored energy as the savings ledger does. Before: a night below the farm's 14.4 kW minimum could not be planned
with the set on, so the plan shed load (329 kWh over 11 days) and the twin's operator made the rescue starts (15 of 23). Test data: the
optimiser's own toy problems at the demo farm's sizes and the demo twin's generated days; nothing reads the network."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest
from test_fix_optimiser import _problem
from test_fix_r2_savings import D, P
from test_fix_savings import company, twin_day  # noqa: F401 (company is a fixture)

from gridlytics import demo, forecast, optimise, plans, sites, tariffs, twin, verify

H = 0.25
S, F_MIN = 48.0, 0.3
MIN_KW = S * F_MIN  # 14.4 kW


def _farm_night(T: int = 48, outage: tuple[int, int] | None = None, c_next: float = 0.0) -> dict:
    """The demo farm's sizes: a 48 kW set (14.4 kW minimum, manual start, 400 L in the tank) and a 120 kWh battery at 20 % over a 15 % cut-off,
    10 kW of load and no PV. Off-grid unless `outage` is given: then a grid-connected site whose grid is down over `outage` (steps), carrying
    only its 8 kW of critical load there, and whose battery may not charge from the grid."""
    p = _problem(T=T, genset=True)
    p["L"][:], p["Lhi"][:], p["Lc"][:] = 10.0, 12.0, 8.0
    if outage is None:
        p.update({"off_grid": True, "g": np.zeros(T), "G_max": 0.0, "c_imp": np.zeros(T), "load_mode": 1})
    else:
        p["g"][:] = 1.0
        p["g"][outage[0]:outage[1]] = 0.0
    p["battery"].update({"E_nom": 120.0, "E0": 24.0, "soc_min": 0.15, "P_ch": 40.0, "P_dis": 40.0, "a_grid": False, "a_gen": False,
                         "a_gen_surplus": True})
    p["genset"].update({"S": S, "f_min": F_MIN, "c_fuel": 280.0, "c_start": 500.0, "c_runhr": 100.0, "c_om": 3.0, "fuel_available": 400.0})
    p["c_short"] = optimise._gen_marginal(p["genset"])
    p["terminal"]["c_next"] = c_next
    return p


def _surplus(p: dict) -> np.ndarray:
    """What the set's minimum load leaves over the load PV leaves, per step (twin.simulate: gen = max(f_min S, need), the rest to the battery)."""
    need = np.asarray(p["L"] if p.get("off_grid") or p.get("load_mode") == 1 else p["Lc"], dtype=float) - np.asarray(p["PV"], dtype=float)
    return np.maximum(0.0, MIN_KW - np.maximum(0.0, need))


# ------------------------------------------------------------------------------------------------ a night below the set's minimum load

@pytest.mark.parametrize("outage", [None, (0, 24)], ids=["off_grid", "grid_outage"])
def test_a_night_below_the_sets_minimum_load_runs_the_set_instead_of_shedding(outage):
    p = _farm_night(outage=outage)
    down = np.flatnonzero(np.asarray(p["g"]) < 0.5)
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    s = r["sol"]
    # before: with the set unable to run under 14.4 kW of load and the battery 6 kWh above its cut-off, the plan shed 114 kWh off-grid (42 kWh of
    # the outage's critical load) while 400 L sat in the tank
    assert float(s["U"].sum()) * H < 1e-3, float(s["U"].sum()) * H
    assert s["u"][down].sum() > 0 and float(s["F"].sum()) <= 400.0
    on = down[s["u"][down] > 0.5]
    # the set runs at its minimum load, and the battery takes what the load leaves of it
    assert np.allclose(s["P"][on], MIN_KW, atol=1e-4)
    assert np.allclose(s["C"][on] - s["D"][on], (MIN_KW - np.asarray(p["L"] if outage is None else p["Lc"]))[on], atol=1e-3)


FARM_MARKS = [(soc, h) for soc in (0.2, 0.5) for h in range(0, 24, 3)]
#: the default suite's two of them (round 7, audit pr-review-01): an evening start low and a midnight start half full, each with a whole night and a
#: second day of runs in its 192 steps
FARM_DEFAULT_MARKS = [(0.2, 18), (0.5, 0)]


def _demo_farm() -> tuple[str, int]:
    sid = next(i for i in demo.create_sites("t") if "farm" in sites.full(i)["name"].lower())
    return sid, twin.day_start_utc(datetime(2026, 11, 5).date())


def _farm_plan(sid: str, day0: int, soc: float, h: int) -> tuple[int, dict, dict]:
    """The demo farm's plan from hour `h` of 5 November at state of charge `soc` (400 L in the tank), solved as the tick solves it."""
    sites.put_state(sid, soc=soc, fuel_l=400.0)
    site = sites.full(sid)
    t0 = forecast.align(day0 + h * 3600)
    p = plans.build_problem(site, t0, forecast.issue(site, t0, save=False), 192)
    return t0, p, optimise.solve_with_policy(p, time_limit=10.0)


def _check_farm_plan(t0: int, p: dict, r: dict, soc: float, h: int) -> tuple[list[tuple], int]:
    """What every farm plan must do (round 5): solve to optimality inside the tick's 10 s, shed (almost) nothing, run the set under a night load below
    its minimum. Returns the second day's starts and stops that fall off the clock's hour marks (the second_day_hour rows), with the number of
    starts and stops there, so a caller can see the check was not empty."""
    assert r["status"] == "optimal" and r["time_limited"] is False and r["relaxed"] is False, (soc, h, r["status"], r.get("mip_gap"))
    s = r["sol"]
    assert float(s["U"].sum()) * H < 0.5, (soc, h, float(s["U"].sum()) * H)
    night = np.asarray(p["L"]) - np.asarray(p["PV"]) < MIN_KW
    assert (s["u"][night] > 0.5).any(), (soc, h)                                     # runs under a load below the set's minimum
    changes = [t for t in range(97, 192) if s["u"][t] != s["u"][t - 1]]
    return [(soc, h, t) for t in changes if (t0 // 900 + t) % 4], len(changes)


@pytest.mark.parametrize("soc,h", FARM_DEFAULT_MARKS, ids=["soc20_from_18h", "soc50_from_00h"])
def test_the_demo_farms_192_step_plans_run_the_set_at_night_and_start_the_second_days_runs_on_hour_marks(company, soc, h):
    # the default suite's check of what the opt-in benchmark below asserts besides its timing (round 7, audit pr-review-01: skipped by default, it was
    # the only test of the real farm's 192-step plans and of the second day's hour-mark rows, whose deletion failed nothing): no shed, the set run
    # under a night load below its minimum, inside the tick's 10 s, and on the horizon's second day every start and stop on an hour mark
    sid, day0 = _demo_farm()
    t0, p, r = _farm_plan(sid, day0, soc, h)
    flips, changes = _check_farm_plan(t0, p, r, soc, h)
    assert changes >= 1, (soc, h, changes)                                            # the second day has a start or a stop to check
    assert not flips, flips


@pytest.mark.slow
def test_the_demo_farms_plans_from_every_three_hour_mark_run_the_set_and_solve_about_as_fast_as_without_the_surplus(company):
    # the demo farm (192 steps, a manual start, 400 L in the tank) planned from each 3-hour mark of 5 November at two states of charge. Before:
    # the plans shed the nights' load with the set off (1 to 88 kWh in every plan). The night runs this change allows doubled or tripled the
    # solve time until the surplus rows and the second day's hour marks. These 16 problems, each solved by every version in turn (mean and
    # worst): on an idle machine on mains power HEAD's model 1.2 s and 2.5 s, the surplus without those rows 3.0 s and 8.5 s, with them 1.4 s
    # and 3.7 s; on battery power 2.2 s and 5.2 s, 6.1 s and 17.6 s (four over the tick's 10 s), 2.9 s and 7.6 s. So the time is checked
    # against the model without the surplus (`a_gen_surplus` off, row for row HEAD's) solved here on the same machine, and each solve must
    # finish inside the limit. One easy instance hid the slowdown, so this times a set. An opt-in benchmark (the `slow` marker, conftest.py: about a
    # minute on an idle machine on mains power, many minutes and a timing failure when the machine is busy or throttled on battery); the test above
    # keeps its correctness in the default suite
    sid, day0 = _demo_farm()
    secs, ref, flips = [], [], []
    for soc, h in FARM_MARKS:
        t0, p, r = _farm_plan(sid, day0, soc, h)
        flips += _check_farm_plan(t0, p, r, soc, h)[0]
        secs.append(r["solve_seconds"])
        q = {**p, "battery": {**p["battery"], "a_gen_surplus": False}}
        assert not optimise.genset_surplus_steps(q).any()
        ref.append(optimise.solve_with_policy(q, time_limit=10.0)["solve_seconds"])
    assert not flips, flips
    # in total within 1.8 times the model without the surplus (measured 1.2-1.3 times; 2.6-2.7 times without the surplus rows)
    assert sum(secs) < 1.8 * sum(ref) + 2.0, (round(sum(secs), 2), round(sum(ref), 2), secs, ref)


# ------------------------------------------------------------------------------------------------ no more than the minimum-load surplus

def _day_shape(p: dict) -> dict:
    """Night (10 kW, no PV), a morning the set must carry (40 kW over 10 kW of PV: 30 kW to find) and a sunny noon (20 kW under 40 kW of PV)."""
    p["L"][16:32], p["PV"][16:32] = 40.0, 10.0
    p["L"][32:48], p["PV"][32:48] = 20.0, 40.0
    p["Lhi"][:] = p["L"] * 1.1
    p["Lc"][:] = np.minimum(p["Lc"], p["L"])
    p["PVlo"][:] = p["PV"]
    return p


def test_the_battery_takes_no_more_from_the_set_than_its_minimum_load_surplus():
    # a stored kWh worth Rs 375 at the horizon's end: the plan would fill the battery from the set at full power if it could (a_gen); no slot
    # table can make a device do that, and on site the set's output only follows the load
    p = _day_shape(_farm_night(c_next=400.0))
    r = optimise.solve(p)
    assert r["status"] in ("optimal", "feasible")
    s = r["sol"]
    on = s["u"] > 0.5
    need = np.asarray(p["L"]) - np.asarray(p["PV"])
    assert on[:16].any() and on[16:32].any()                                       # it runs at night (it could not before) and in the morning
    # the set never makes more than the load PV leaves, or its minimum load where that is less
    assert np.all(s["P"][on] <= np.maximum(MIN_KW, need[on]) + 1e-4)
    # so the battery takes from the set no more than the minimum-load surplus: none while the set carries the morning (30 kW of load to find; before
    # this change PV charged the battery there while the set carried the load, which the set's load-following output never allows)...
    pv_used = np.asarray(p["PV"]) - s["K"]
    from_set = s["C"] - s["D"] - np.maximum(0.0, pv_used - np.asarray(p["L"]))
    assert np.all(from_set[on] <= _surplus(p)[on] + 1e-3), from_set[on]
    assert np.all(s["C"][16:32] - s["D"][16:32] <= 1e-4)
    # ... and at night exactly that surplus, 4.4 kW (what the twin's battery takes from a set at its minimum under 10 kW of load)
    assert np.allclose(s["C"][:16][on[:16]], MIN_KW - 10.0, atol=1e-3)
    # with a brand that could be told to charge from the set (a_gen), the cap does not apply: the plan charges at full power, as it is allowed to
    q = _day_shape(_farm_night(c_next=400.0))
    q["battery"]["a_gen"] = True
    assert not optimise.genset_surplus_steps(q).any()
    full = optimise.solve(q)["sol"]
    assert float(np.max(full["P"])) > MIN_KW + 10.0 and float(np.max(full["C"][:16])) > MIN_KW


def test_the_twin_following_the_plans_runs_ends_where_the_plan_said():
    # the plan's night, keyed into the twin minute by minute with its genset schedule (a manual start, as the demo's operator runs it): the battery
    # ends within a few kWh of the plan, and nothing is shed. Before: the plan shed with the set off, and the twin's operator rescued the site
    p = _farm_night(c_next=150.0)
    r = optimise.solve(p)
    s = r["sol"]
    assert float(s["U"].sum()) < 1e-6 and s["u"].sum() > 0
    T, n = p["T"], p["T"] * 15
    ts = np.repeat(p["ts"], 15) + np.tile(np.arange(15) * 60, T)
    series = {"pv": np.repeat(p["PV"], 15), "load": np.repeat(p["L"], 15), "crit": np.repeat(p["Lc"], 15), "grid": np.zeros(n), "ts": ts,
              "temp": np.full(n, 30.0)}
    pol = {"mode": np.zeros(n, dtype=int), "floor": np.full(n, 0.15), "target": np.full(n, 0.95), "max_dis": np.full(n, np.inf),
           "max_ch": np.full(n, np.inf), "grid_charge": np.zeros(n, dtype=bool), "genset": np.repeat(s["u"].astype(int), 15)}
    plant = twin.Plant(e_nom=120.0, soc_min=0.15, soc_max=0.95, p_ch=40.0, p_dis=40.0, g_max=0.0, gen_kw=S, gen_fmin=F_MIN, gen_control="manual",
                       gen_min_run=60)
    sim = twin.simulate(series, pol, plant, 24.0 / 120.0, outage_min0=60)
    assert float(np.sum(sim["unserved"])) / 60 < 0.05
    assert abs(float(sim["soc"][-1]) * 120.0 - float(s["E"][-1])) < 3.0, (float(sim["soc"][-1]) * 120.0, float(s["E"][-1]))
    assert float(np.sum(sim["gen"])) / 60 == pytest.approx(float(np.sum(s["P"])) * H, rel=0.03)


# ------------------------------------------------------------------------------------------------ grid-up steps take no genset charge

def test_grid_up_steps_take_no_charge_from_the_set_and_outage_steps_take_its_surplus():
    # a parallel set cheaper than the grid (Rs 200 a kWh), a battery that may not charge from the grid and a stored kWh worth Rs 300: with the grid
    # up the set's output nobody takes is not stored (twin.simulate's grid branch), so the plan may not charge from it there
    p = _farm_night(outage=(0, 16), c_next=300.0)
    p["L"][16:], p["Lhi"][16:] = 30.0, 33.0                                           # over the set's minimum once the grid is back
    p["c_imp"][:] = 200.0
    p["genset"]["parallel"] = True
    up = np.asarray(p["g"]) > 0.5
    r = optimise.solve(p)
    assert r["status"] in ("optimal", "feasible")
    s = r["sol"]
    assert (s["u"][up] > 0.5).any()                                                   # the set runs in parallel with the grid up...
    assert float(np.max(s["C"][up])) < 1e-4                                           # ...and never charges the battery there
    out = ~up & (s["u"] > 0.5)
    assert out.any() and float(np.max(s["C"][out])) == pytest.approx(MIN_KW - 8.0, abs=1e-3)   # in the outage: the surplus over 8 kW
    assert float(s["U"].sum()) < 1e-6                                                 # before: 8 kW of critical load shed in the outage
    assert (optimise.genset_surplus_steps(p) == ~up).all()


def test_with_grid_charging_allowed_a_parallel_set_cheaper_than_the_grid_never_charges_the_battery_with_the_grid_up():
    # the verifier's gap in the test above: there the battery may not charge from the grid, so the charge_surplus row alone already keeps the
    # set's output out of it with the grid up. Here it may, so only the charge source row does. A stored kWh is worth Rs 137.5 at the horizon's
    # end: more than the set's extra output costs once it runs (Rs 79 a kWh, Rs 84 stored), less than a grid kWh (Rs 200, Rs 211 stored), so
    # the plan would fill the battery from the set with the grid up if the charge rows let it
    p = _farm_night(outage=(0, 16), c_next=150.0)
    p["battery"]["a_grid"] = True
    p["L"][16:], p["Lhi"][16:] = 30.0, 33.0
    p["c_imp"][:] = 200.0
    p["genset"]["parallel"] = True
    up = np.asarray(p["g"]) > 0.5
    r = optimise.solve(p)
    assert r["status"] in ("optimal", "feasible")
    s = r["sol"]
    assert (s["u"][up] > 0.5).sum() >= 16                                              # the set carries the load with the grid up...
    pv_left = np.asarray(p["PV"]) - s["K"]
    assert np.all(s["C"][up] <= s["G"][up] + pv_left[up] + 1e-6)                      # ...and the battery charges only from the grid or PV
    assert float(np.max(s["C"][up])) < 1e-4                                          # (here neither is worth it)
    assert float(np.max(s["P"][up] - np.asarray(p["L"])[up])) < 1e-4                 # so the set never makes more than the load
    out = ~up & (s["u"] > 0.5)
    assert out.any() and float(np.max(s["C"][out])) == pytest.approx(MIN_KW - 8.0, abs=1e-3)   # in the outage: the surplus over 8 kW


# ------------------------------------------------------------------------------------------------ the surplus rows cut no plan off

def _cases() -> dict:
    whole = _farm_night(outage=(4, 30), c_next=150.0)
    whole["load_mode"] = 1                                                            # the set carries the whole site in the outage
    auto = _farm_night(outage=(6, 40), c_next=150.0)
    auto["genset"].update({"control": "auto_on_grid_loss", "u_prev": 1, "elapsed": 1})  # panel starts, and the dumps they allow
    held = _farm_night(c_next=200.0)
    held["genset"]["hold"] = np.zeros(48, dtype=bool)
    held["genset"]["hold"][20:28] = True                                              # an operator's hold on the set
    return {"off_grid": _farm_night(c_next=150.0), "sunny_day": _day_shape(_farm_night(c_next=250.0)), "outage_critical": _farm_night(outage=(0, 24)),
            "outage_whole_site": whole, "auto_start": auto, "operator_hold": held}


@pytest.mark.parametrize("case", list(_cases()))
def test_the_rows_on_the_sets_surplus_and_the_battery_cut_no_plan_off(monkeypatch, case):
    # optimise.solve's grid-down rows (C >= P - n_on u - K - W, D >= n_off (1 - u) - U) follow from the balance once the set is on or off, so
    # the optimum is the same with and without them; they only tighten the relaxation (round 5: the demo farm's 192-step solves halve)
    p = _cases()[case]
    with_rows = optimise.solve(p, gap=1e-7)
    monkeypatch.setattr(optimise, "SURPLUS_ROWS", False)
    without = optimise.solve(_cases()[case], gap=1e-7)
    assert with_rows["status"] == without["status"] == "optimal"
    assert with_rows["rows"] > without["rows"]
    assert with_rows["objective_rs"] == pytest.approx(without["objective_rs"], abs=0.05)


@pytest.mark.parametrize("case", ["soc_threshold", "full_power_charging", "grid_up"])
def test_the_surplus_rows_are_not_written_where_they_do_not_apply(monkeypatch, case):
    # a soc_threshold controller's rows bind the set to the state of charge, and these only slowed those solves (the 400 L threshold farm:
    # 5.4 s without them, 8.5 s to time-limited with them); a brand that charges from the set at full power (a_gen) and a grid that never
    # goes down have no minimum-load surplus steps
    p = _farm_night(c_next=150.0) if case != "grid_up" else _farm_night(outage=(0, 0), c_next=150.0)
    if case == "soc_threshold":
        p["genset"].update({"control": "soc_threshold", "soc_on": 0.25})
    if case == "full_power_charging":
        p["battery"]["a_gen"] = True
    rows = optimise.solve(p)["rows"]
    monkeypatch.setattr(optimise, "SURPLUS_ROWS", False)
    assert optimise.solve(p)["rows"] == rows


# ------------------------------------------------------------------------------------------------ the threshold repair's replay

def test_the_threshold_replay_stores_the_sets_minimum_load_surplus():
    # an off-grid soc_threshold site under 10 kW of load: once the controller starts the set under its threshold, the battery takes the 4.4 kW its
    # minimum load leaves over, as on site. Before: the replay counted no charge from the set, so the battery sat under its threshold and the set
    # ran on for as long as the tank allowed
    p = _farm_night()
    p["genset"].update({"control": "soc_threshold", "soc_on": 0.25})
    T = p["T"]
    sol = {"u": np.zeros(T), "P": np.zeros(T)}
    out = optimise.threshold_replay(p, sol, np.zeros(T), np.ones(T), list(range(1, T)), 0.25 * 120.0, np.ones(T, dtype=bool))
    on = np.flatnonzero(out["u"])
    assert on.size and on[0] == 1                                                     # 24 kWh is under the 30 kWh threshold
    assert out["E"][on[0] + 1] - out["E"][on[0]] == pytest.approx(H * 0.95 * (MIN_KW - 10.0), abs=1e-6)
    assert float(out["E"].max()) > 30.0                                               # charged over its threshold...
    assert out["u"].sum() < T - 1                                                     # ...so the controller stops it (before: on to the end)
    assert float(out["U"].sum()) < 1e-9


# ------------------------------------------------------------------------------------------------ the terminal value, as the ledger values a kWh

def test_the_off_grid_terminal_value_prices_a_stored_kwh_as_the_savings_ledger_does(company):
    sid = next(i for i in demo.create_sites("t") if "farm" in sites.full(i)["name"].lower())
    site = sites.full(sid)
    g, b = site["assets"]["genset"], site["assets"]["battery"]
    # the plan's c_next: the genset's cost per kWh at its after-dark load ratio (verify.night_load_ratio) on the horizon's last day of forecast
    # load and PV, as the ledger prices a kWh left at midnight. Before: its cost at 75 % load, about Rs 27 a kWh less at the farm, whose set
    # carries 8-21 kW after dark
    t0 = forecast.align(datetime(2026, 11, 4, 19, 0, tzinfo=timezone.utc).timestamp())
    p = plans.build_problem(site, t0, forecast.issue(site, t0, save=False), 192)
    assert p["off_grid"] and p["genset"]["control"] == "manual"
    c_next = p["terminal"]["c_next"]
    ratio = verify.night_load_ratio(g, {"pv": p["PV"][-96:], "load": p["L"][-96:]})
    gm = {"S": g["rated_kw"], "a": g["a"], "b": g["b"], "c_fuel": p["genset"]["c_fuel"], "c_om": g["c_om_rs_per_kwh"], "c_runhr": g["c_runhr_rs"]}
    assert 0.3 <= ratio < 0.5 and c_next == pytest.approx(optimise._gen_marginal(gm, ratio), abs=1e-9)
    assert c_next > p["c_short"] + 15.0
    assert p["battery"]["a_gen"] is False and p["battery"]["a_gen_surplus"] is True
    r = optimise.solve(optimise.shorten(p, 8))                                       # the terminal value, on a short solve
    assert r["v_end"] == pytest.approx(max(0.0, b["eta_d"] * c_next - p["battery"]["c_cyc"] - p["terminal"]["c_cal_hold"]), abs=1e-9)
    # one rule for both: the ledger's v_end on a twin day is plans.offgrid_c_next applied to that day's load and PV
    _, sp, _ = twin_day(sid, P, 0.5, seed=1)
    twin_day(sid, D, float(sp["soc"][-1]), seed=2)
    row = verify.compute_day(sid, D, with_bound=False)["native"]
    series, _ = verify._replay_inputs(sites.full(sid), D)
    price = tariffs.fuel_price(g["fuel_kind"], D)[0]
    assert row["actual"]["v_end"] == pytest.approx(b["eta_d"] * plans.offgrid_c_next(g, price, series["load"], series["pv"]) - optimise.wear_cost(b),
                                                   abs=0.01)
    assert c_next == pytest.approx(plans.offgrid_c_next(g, p["genset"]["c_fuel"], p["L"][-96:], p["PV"][-96:]), abs=1e-9)
