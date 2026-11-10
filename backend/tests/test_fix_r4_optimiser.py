"""Round-4 regressions for the optimiser group (HANDOFF 8.A.3 to 8.A.5): the threshold repair of a time-limited off-grid plan that sheds with
the set off and fuel in the tank, the merged charge slot that charged on into the peak before its discharge, and time-limited plans made
visible (`solver.time_limited`, `solver.mip_gap`, `plans.time_limited_recent`). Test data: the optimiser's own toy problems, the demo farm's
sizes and a synthetic Sungrow day; nothing reads the network."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest

from gridlytics import delivery, optimise, plans, sites, store, twin
from test_fix_optimiser import _problem, _site
from test_fix_r3_optimiser import _as_if_time_limited, _dodges, _offgrid_threshold, _steps_for


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


# ------------------------------------------------------------------------------------------------ 8.A.3 the threshold repair

def _offgrid_toy(fuel: float) -> dict:
    """Twelve hours off-grid: 40 kW of load, no PV, the battery at 50 % over a 30 % threshold, a 100 kW set that may not charge the battery
    (as no brand can be told to), on a soc_threshold controller. The set follows the 40 kW load (round 7, engine-optimiser-06: it could run
    above it before), so it burns at most 14.2 L an hour: the poor incumbent's tank (155 L, was 160) is one it can burn in the 11 hours left."""
    p = _problem(T=48, genset=True)
    p.update({"off_grid": True, "g": np.zeros(48), "G_max": 0.0, "X_max": 0.0, "c_imp": np.zeros(48), "c_exp": np.zeros(48), "voll": 500.0})
    p["L"][:], p["Lhi"][:], p["Lc"][:] = 40.0, 45.0, 20.0
    p["battery"].update({"a_grid": False, "a_gen": False, "E0": 100.0})
    p["terminal"]["c_next"] = 0.0
    p["genset"].update({"control": "soc_threshold", "soc_on": 0.3, "fuel_available": fuel})
    return p


def _poor_incumbent(monkeypatch, off: range, shed_kw: float, report: int = 1, burn_all: bool = True) -> list[int]:
    """The first solve of a call returns a poor plan, as the two-day farm solve does at 300 to 310 L: the set off over `off` while load is shed
    with the battery above its cut-off, and (`burn_all`) the whole tank burnt. It reports HiGHS status `report`: 1 as a solve that stopped at
    its limit, 0 as one that finished inside it (the first problem's own optimum can dodge). Later solves are left alone."""
    real, seen = optimise.Model.solve, []

    def limited(self, time_limit, gap):
        first = not seen
        if first:
            lb, ub, rlb = list(self.lb), list(self.ub), list(self.rlb)
            for k in off:
                self.ub[self.blocks["u"][k]] = 0.0
                self.lb[self.blocks["U"][k]] = min(shed_kw, self.ub[self.blocks["U"][k]])
            if burn_all:
                i = self.rname.index("fuel_cap")
                self.rlb[i] = self.rub[i]
        res, A = real(self, time_limit, gap)
        seen.append(int(res.status))
        if first:
            self.lb, self.ub, self.rlb = lb, ub, rlb
            if res.x is not None:
                res.status = report
        return res, A

    monkeypatch.setattr(optimise.Model, "solve", limited)
    return seen


def test_a_time_limited_plan_that_dodges_a_start_and_burns_its_whole_tank_is_repaired_by_the_threshold_controller(monkeypatch):
    clean = optimise.solve(_offgrid_toy(155.0))
    assert clean["status"] in ("optimal", "feasible") and float(clean["sol"]["U"].sum()) * 0.25 < 1e-3  # solved properly, nothing is shed
    seen = _poor_incumbent(monkeypatch, range(10, 14), 20.0)
    p = _offgrid_toy(155.0)
    r = optimise.solve(p)
    rep = r.get("threshold_repair")
    # before: the incumbent was published as it stood (20 kW shed for an hour with the set off, the battery above its cut-off and fuel in the
    # tank); the re-solve with the shed rule only ran for a plan that left fuel over, and this one burnt the tank
    assert rep and rep["shed_steps"] >= 3 and rep["shed_kwh"] >= 15.0, r.get("notes")
    assert len(seen) == 2 and "threshold_repair" in r["notes"] and "shed_rule_resolve" not in r
    assert _dodges(p, r) == []
    assert float(r["sol"]["U"].sum()) * 0.25 < 1e-3                        # the repaired plan sheds nothing, as the optimum does
    E, u = r["sol"]["E"], r["sol"]["u"]
    under = [t for t in range(1, 48) if E[t] < 60.0 - 1e-6]
    assert under and all(u[t] > 0.5 for t in under)                       # under its threshold the set always runs, as on site
    assert float(r["sol"]["F"].sum()) <= 155.0 + 1e-6
    assert r["status"] == "feasible" and r["time_limited"] is True        # the schedule is the controller's, not proven by the solver
    assert r["mip_gap"] is not None and 0.0 <= r["mip_gap"] < 1.0
    assert rep["repair_seconds"] < 2.0                                     # the genset fixed: close to a linear programme


def _spy(monkeypatch) -> list[int]:
    """The HiGHS status of every solve of a call, left as it is."""
    real, seen = optimise.Model.solve, []

    def spy(self, time_limit, gap):
        res, A = real(self, time_limit, gap)
        seen.append(int(res.status))
        return res, A

    monkeypatch.setattr(optimise.Model, "solve", spy)
    return seen


def test_an_optimal_first_plan_that_dodges_with_fuel_left_is_solved_again_with_the_shed_rule(monkeypatch):
    # verifier, round 4 (8.A.3 GAP 1): the check ran only on a plan that stopped at its limit, so the first problem's own optimum, which dodges
    # here (VoLL under the set's cost), was published as it stood: 40 kW load held on the threshold by shedding, the set off, the tank unlimited
    p = _offgrid_threshold(1e9, 50.0)
    seen = _spy(monkeypatch)
    r = optimise.solve(p)
    assert seen[0] == 0                                                    # the first solve finished inside its limit
    res = r.get("shed_rule_resolve")
    assert res and res["first_status"] == "optimal" and res["first_time_limited"] is False and res["shed_steps"] > 0
    assert len(seen) == 2 and _dodges(p, r) == [] and r["sol"]["u"].sum() > 0
    assert r["time_limited"] is (seen[1] == 1)                             # the published plan is the re-solve's, flagged by its own stop


def test_an_optimal_first_plan_that_dodges_and_burns_its_whole_tank_is_repaired_without_counting_as_time_limited(monkeypatch):
    p = _offgrid_threshold(6.0, 500.0)
    seen = _spy(monkeypatch)
    r = optimise.solve(p)
    rep = r.get("threshold_repair")
    assert seen[0] == 0 and rep and rep["status"] == "optimal" and rep["first_status"] == "optimal" and rep["first_time_limited"] is False
    assert "shed_rule_resolve" not in r and len(seen) == 2                 # no fuel left over: no re-solve, the repair only
    assert _dodges(p, r) == [] and float(r["sol"]["F"].sum()) == pytest.approx(6.0, abs=0.01)
    assert r["status"] == "feasible" and r["time_limited"] is False        # never proven optimal, but no solve stopped at its limit
    assert r["message"].startswith("Threshold repair of a plan that shed with the set off")
    assert plans._status_label(r) == "Repaired to the site's controller"


def test_a_poor_first_plan_reported_optimal_is_repaired_as_one_that_stopped_at_its_limit(monkeypatch):
    seen = _poor_incumbent(monkeypatch, range(10, 14), 20.0, report=0)
    p = _offgrid_toy(155.0)
    r = optimise.solve(p)
    rep = r.get("threshold_repair")
    assert rep and rep["first_status"] == "optimal" and rep["shed_steps"] >= 3 and len(seen) == 2
    assert _dodges(p, r) == [] and float(r["sol"]["U"].sum()) * 0.25 < 1e-3
    assert r["status"] == "feasible" and r["time_limited"] is False


def test_the_replay_stops_a_run_the_threshold_does_not_force_once_the_days_run_hour_cap_is_used():
    # verifier, round 4 (8.A.3 MINOR): with a daily cap of 2 to 6 h the replay kept the plan's runs above the threshold past the cap, so the
    # fixed-genset solve paid the threshold rows' VoLL slack (the battery under the threshold with the set off)
    p = _offgrid_toy(1000.0)
    T = p["T"]
    p["genset"].update({"cap_hours": 1.0, "hours_today": 0.0, "day_index": np.zeros(T, dtype=int)})
    sol = {"u": np.r_[np.ones(31), np.zeros(T - 31)], "P": np.r_[np.full(31, 40.0), np.zeros(T - 31)]}  # a run of 31 steps from step 0
    thr = list(range(1, T))
    out = optimise.threshold_replay(p, sol, np.zeros(T), np.ones(T), thr, 60.0, np.ones(T, dtype=bool))
    u, E = out["u"], out["E"]
    counted = [t for t in range(T) if u[t] > 0.5 and not (t >= 1 and E[t] < 60.0 - 1e-6)]   # hours the threshold does not force
    assert len(counted) * 0.25 <= 1.0 + 1e-9, counted                                       # 31 steps (7.75 h) before
    assert list(np.flatnonzero(u[:4])) == [0, 1, 2, 3]                                       # the plan's own start keeps its minimum up time
    under = [t for t in range(1, T) if E[t] < 60.0 - 1e-6]
    assert under and all(u[t] > 0.5 for t in under)                                          # threshold runs are not capped
    fixed = optimise.solve(p, shed_rule=True, fix_u=u)
    assert fixed["status"] == "optimal" and _dodges(p, fixed) == []
    # the fixed schedule is a point of the capped problem: its threshold rows need no slack (E under the threshold only with the set on)
    assert all(fixed["sol"]["u"][t] > 0.5 for t in range(1, T) if fixed["sol"]["E"][t] < 60.0 - 1e-3)


def test_the_replay_starts_the_set_under_its_threshold_and_stops_it_when_the_tank_cannot_cover_a_step():
    p = _offgrid_toy(20.0)
    T = p["T"]
    sol = {"u": np.zeros(T), "P": np.zeros(T)}                             # a plan that never runs the set
    out = optimise.threshold_replay(p, sol, np.zeros(T), np.ones(T), list(range(1, T)), 60.0, np.ones(T, dtype=bool))
    on = np.flatnonzero(out["u"])
    assert on[0] == 4                                                     # 100 kWh at 40 kW: under 60 kWh after four quarters
    burn = 0.25 * (0.273 * 30.0 + 0.033 * 100.0)                          # a step at minimum load
    assert float(out["F"].sum()) <= 20.0 + 1e-9 and 20.0 - float(out["F"].sum()) < burn
    assert list(on) == list(range(4, 4 + len(on)))                         # one run, from the threshold until the tank is out
    assert float(out["U"][: on[-1] + 1].sum()) < 1e-9                      # nothing shed while the set or the battery can carry it


def _farm(fuel: float) -> str:
    """The demo farm's sizes (tests/test_fix_r3_optimiser.py): 80 kWp, 120 kWh, a 48 kW set with a 400 L tank on a soc_threshold controller."""
    s = sites.create({"name": "Farm", "critical_load_kw": [8.0] * 24, "genset_start_control": "soc_threshold", "outage_load_mode": 1,
                      "site_type": "farm"}, "t")
    sid = s["id"]
    sites.put_asset(sid, "pv", {"kwp_dc": 80, "kw_ac": 70}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 120, "p_ch_kw": 40, "p_dis_kw": 40, "soc_min": 0.15}, "t")
    sites.put_asset(sid, "genset", {"rated_kw": 48, "f_min": 0.3, "min_up_h": 1.0, "tank_l": 400}, "t")
    sites.put_state(sid, soc=0.5, fuel_l=fuel)
    return sid


def test_the_400_l_farm_at_300_l_publishes_no_plan_that_sheds_with_the_set_off_and_fuel_in_the_tank(company):
    # HANDOFF 8.A.3: at t0 = 2026-11-04 22:00 UTC the 10 s solve's incumbent burnt the whole 300 L and shed 4.6 kWh with the set off (steps 99
    # and 100), so no re-solve ran; solved to optimality (75 to 90 s) it sheds nothing
    sid = _farm(300.0)
    at = int(datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc).timestamp())
    r = plans.tick(sid, at=at, time_limit=10.0)
    assert r["status"] in ("active", "held")
    steps = r["steps"]
    burn = 0.25 * (0.273 * 0.3 * 48 + 0.033 * 48)
    litres = np.concatenate([[0.0], np.cumsum([s["litres"] for s in steps])])
    dodged = [t for t in range(len(steps) - 1)
              if steps[t]["unserved_kw"] > max(0.0, steps[t]["load_kw"] - steps[t]["pv_forecast_kw"] - 40.0) + 0.01  # more than the battery's power
              and not steps[t]["genset_on"] and not steps[t + 1]["genset_on"]                                       # the set off, and not started
              and steps[t]["soc_pct"] > 15.0 + 0.1                                                                  # the battery above its cut-off
              and litres[t] < 300.0 - burn]                                                                         # fuel in the tank
    assert dodged == [], (dodged, r["summary"]["solver"])
    sv = r["summary"]["solver"]
    assert r["solve_seconds"] < 30.0                                       # under three solver limits
    assert isinstance(sv["time_limited"], bool) and sv["time_limit_s"] == 10.0 and sv["mip_gap"] is not None
    assert sv["time_limited"] == (r["summary"]["status_label"] == "Solved within the time limit")
    recent = plans.time_limited_recent(sid, 1, now=at)
    assert recent["plans"] == 1 and recent["time_limited"] == int(sv["time_limited"])


def _farm_dodges(steps: list[dict], fuel: float) -> list[int]:
    burn = 0.25 * (0.273 * 0.3 * 48 + 0.033 * 48)
    litres = np.concatenate([[0.0], np.cumsum([s["litres"] for s in steps])])
    return [t for t in range(len(steps) - 1)
            if steps[t]["unserved_kw"] > max(0.0, steps[t]["load_kw"] - steps[t]["pv_forecast_kw"] - 40.0) + 0.01
            and not steps[t]["genset_on"] and not steps[t + 1]["genset_on"] and steps[t]["soc_pct"] > 15.0 + 0.1 and litres[t] < fuel - burn]


def test_the_400_l_farm_at_300_l_publishes_no_dodge_when_its_first_solve_finishes_inside_its_limit(company, monkeypatch):
    # verifier, round 4 (8.A.3 GAP 1): with a 60 s limit the first solve was optimal in 14.7 s and the tick published its dodge at steps 99
    # and 100 (4.6 kWh shed with the set off, the battery at 30 to 31 kWh over an 18 kWh cut-off, 149 L left), with no re-solve and no repair,
    # because the check ran only on a plan that stopped at its limit. Here the first solve returns that plan and reports it optimal, whatever
    # the machine's speed: the set off at steps 99 and 100 while 9 kW is shed, fuel left over
    sid = _farm(300.0)
    at = int(datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc).timestamp())
    seen = _poor_incumbent(monkeypatch, range(99, 101), 9.0, report=0, burn_all=False)
    r = plans.tick(sid, at=at, time_limit=10.0)
    assert r["status"] in ("active", "held")
    sv = r["summary"]["solver"]
    fixed = sv.get("shed_rule_resolve") or sv.get("threshold_repair")
    assert fixed and fixed["first_status"] == "optimal" and fixed["first_time_limited"] is False and fixed["shed_steps"] >= 1, sv
    assert _farm_dodges(r["steps"], 300.0) == [], sv
    assert len(seen) >= 2 and r["solve_seconds"] < 30.0                    # under three solver limits


# ------------------------------------------------------------------------------------------------ 8.A.4 the merged charge slot's rate

def _sungrow_day() -> list[dict]:
    """A 1 MWh battery: grid charge at 100 kW from 00:00 to 06:00 (30 % to 87 %), hold to 20:45, discharge at 200 kW to midnight, in a peak of
    18:30 to 22:30."""
    day = [("charge_from_grid", 100.0, 0.0)] * 24 + [("hold_for_outage", 0.0, 0.0)] * 59 + [("discharge_peak_window", 0.0, 200.0)] * 13
    steps = _steps_for(day)
    soc = 30.0
    for i, st in enumerate(steps):
        st["soc_start_pct"] = soc
        if i < 24:
            soc += 100.0 * 0.25 * 0.95 / 1000.0 * 100.0
        elif i >= 83:
            soc -= 200.0 * 0.25 / 0.95 / 1000.0 * 100.0
        st["soc_pct"] = round(soc, 3)
        st["peak"] = 74 <= i < 90
    return steps


def test_a_merged_charge_slot_charges_at_its_planned_rate_and_ends_the_charge_before_the_peak():
    steps = _sungrow_day()
    out = delivery.compile_mode_a(steps, "sungrow", {"E_nom": 1000.0, "soc_min": 0.1, "soc_max": 0.95}, 20.0, 10.0, 900.0)
    slots = out["days"][0]["slots"]
    assert len(slots) == 2 and slots[1]["mode"] == "discharge" and slots[1]["start_ts"] == steps[83]["t"]
    charge = slots[0]
    assert charge["mode"] == "charge_grid" and charge["end_ts"] == steps[83]["t"]         # the hold folded into the charge slot (round 3)
    assert charge["max_charge_w"] == 100_000                                               # 28,916 W before: 2,400 kW of quarters over 83
    assert charge["charge_end_ts"] == steps[24]["t"] and charge["charge_end"] == "06:00"
    assert charge["grid_cap_w"] == 900_000                                                 # its demand cap as it was
    assert slots[1]["charge_end_ts"] is None and slots[1]["charge_end"] is None
    # keyed into the twin: the battery is charged by the end of the night's steps and takes nothing in the peak hours before the discharge
    ts = np.arange(steps[0]["t"], steps[-1]["t"] + 900, 60)
    n = len(ts)
    series = {"pv": np.zeros(n), "load": np.full(n, 400.0), "crit": np.full(n, 100.0), "grid": np.ones(n), "ts": ts, "temp": np.full(n, 30.0)}
    plant = twin.Plant(e_nom=1000.0, soc_min=0.1, soc_max=0.95, p_ch=200.0, p_dis=300.0, g_max=2000.0)
    sim = twin.simulate(series, twin.slots_policy(slots, ts), plant, 0.30)
    charge_kwh = np.maximum(0.0, -sim["bat"]) / 60.0
    in_peak = (ts >= steps[74]["t"]) & (ts < steps[83]["t"])
    assert float(charge_kwh[in_peak].sum()) < 1.0                         # about 65 kWh of peak-priced charging before
    assert float(charge_kwh[ts >= steps[24]["t"]].sum()) < 5.0
    assert sim["soc"][int(np.flatnonzero(ts == steps[24]["t"])[0])] >= 0.86


def test_a_charge_slot_without_folded_steps_keeps_its_mean_rate():
    day = [("idle", 0.0, 0.0)] * 8 + [("charge_from_grid", 80.0, 0.0)] * 4 + [("charge_from_grid", 40.0, 0.0)] * 4 + [("idle", 0.0, 0.0)] * 80
    steps = _steps_for(day)
    out = delivery.compile_mode_a(steps, "deye", {"E_nom": 1000.0, "soc_min": 0.1, "soc_max": 0.95}, 20.0, 10.0, None)
    charge = [s for s in out["days"][0]["slots"] if s["mode"] == "charge_grid"]
    assert len(charge) == 1 and charge[0]["max_charge_w"] == 60_000 and charge[0]["charge_end_ts"] == steps[16]["t"] == charge[0]["end_ts"]


# ------------------------------------------------------------------------------------------------ 8.A.5 time-limited plans made visible

def test_a_solve_says_whether_it_stopped_at_its_limit_and_the_gap_it_reached(monkeypatch):
    r = optimise.solve(_problem(T=48, genset=True, outage=(8, 16)))
    assert r["status"] == "optimal" and r["time_limited"] is False and r["time_limit_s"] == 10.0
    assert r["mip_gap"] is not None and 0.0 <= r["mip_gap"] <= 1e-3 and r["dual_bound_rs"] is not None
    _as_if_time_limited(monkeypatch)
    r2 = optimise.solve(_problem(T=48, genset=True, outage=(8, 16)), time_limit=7.0)
    assert r2["status"] == "feasible" and r2["time_limited"] is True and r2["time_limit_s"] == 7.0 and r2["mip_gap"] is not None


def test_the_plan_summary_and_time_limited_recent_show_a_plan_that_stopped_at_its_limit(company, monkeypatch):
    sid = _site()
    first = plans.tick(sid, steps=96)
    sv = first["summary"]["solver"]
    assert isinstance(sv["time_limited"], bool) and sv["mip_gap"] is not None and sv["time_limit_s"] == 10.0
    _as_if_time_limited(monkeypatch)
    second = plans.tick(sid, steps=96)
    assert second["summary"]["solver"]["time_limited"] is True
    assert plans.get(sid, second["plan_id"])["summary"]["solver"]["time_limited"] is True     # stored with the plan
    recent = plans.time_limited_recent(sid, 1)
    assert recent["plans"] == 2 and recent["time_limited"] == 1 + int(sv["time_limited"])
    assert recent["items"][0]["id"] == second["plan_id"] and recent["items"][0]["time_limit_s"] == 10.0
    assert 0.0 < recent["share"] <= 1.0
    now = int(datetime.now(timezone.utc).timestamp())
    assert plans.time_limited_recent(sid, 1, now=now - 7200)["plans"] == 0  # nothing generated in that hour


def test_a_plan_stored_before_the_flag_counts_as_time_limited_from_its_status():
    assert plans._plan_time_limited({"solver": {"status": "feasible"}}) is True
    assert plans._plan_time_limited({"solver": {"status": "optimal"}}) is False
    assert plans._plan_time_limited({"solver": {"status": "optimal", "time_limited": True}}) is True


def _first_times_out_without_a_plan(monkeypatch) -> list[int]:
    """The first solve of a call stops at its limit before it finds a plan (HiGHS status 1, no x), as a two-day solve on a loaded machine can;
    later solves are left alone."""
    real, seen = optimise.Model.solve, []

    def limited(self, time_limit, gap):
        res, A = real(self, time_limit, gap)
        seen.append(int(res.status))
        if len(seen) == 1:
            res.status, res.x = 1, None
        return res, A

    monkeypatch.setattr(optimise.Model, "solve", limited)
    return seen


def test_a_relaxed_retry_after_a_first_solve_that_timed_out_without_a_plan_counts_as_time_limited(monkeypatch):
    # verifier, round 4 (8.A.5): the relaxed 24-hour retry was published with its own flag (time_limited false, status optimal, the first
    # solve's time not added), so the plans the wall-clock limit affects most were the ones time_limited_recent never counted
    calls = []
    real_solve = optimise.solve

    def recording(*a, **k):
        out = real_solve(*a, **k)
        calls.append(dict(out))  # as each solve returned it, before solve_with_policy adds to it
        return out

    seen = _first_times_out_without_a_plan(monkeypatch)
    monkeypatch.setattr(optimise, "solve", recording)
    r = optimise.solve_with_policy(_problem(T=96, genset=True, outage=(8, 16)))
    assert len(seen) == 2 and calls[0]["status"] == "not_solved" and calls[0]["time_limited"] is True
    assert r["relaxed"] is True and r["status"] in ("optimal", "feasible") and "relaxed_retry" in r["notes"]
    assert r["first_time_limited"] is True and r["time_limited"] is True and r["retry_of"] == calls[0]["message"]
    assert r["first_solve_seconds"] == calls[0]["solve_seconds"]
    assert r["solve_seconds"] == pytest.approx(calls[0]["solve_seconds"] + calls[1]["solve_seconds"], abs=2e-3)


def test_a_relaxed_retry_is_stored_and_counted_as_time_limited(company, monkeypatch):
    sid = _site()
    _first_times_out_without_a_plan(monkeypatch)
    r = plans.tick(sid, steps=96)
    sv = r["summary"]["solver"]
    assert r["status"] in ("active", "held") and sv["relaxed"] is True
    assert sv["time_limited"] is True and sv["first_time_limited"] is True and sv["retry_of"] and sv["first_solve_seconds"] is not None
    assert r["summary"]["status_label"] == "Solved within the time limit"                    # "Solved within tolerance" before
    assert r["solve_seconds"] >= sv["first_solve_seconds"]
    stored = plans.get(sid, r["plan_id"])["summary"]["solver"]
    assert stored["first_time_limited"] is True and stored["time_limited"] is True
    recent = plans.time_limited_recent(sid, 1)
    assert recent["plans"] == 1 and recent["time_limited"] == 1 and recent["items"][0]["relaxed_retry"] is True   # 0 before


def test_a_stored_relaxed_retry_counts_as_time_limited_whatever_its_own_flag_and_the_labels_follow_the_flags():
    assert plans._plan_time_limited({"solver": {"status": "optimal", "time_limited": False, "first_time_limited": True}}) is True
    assert plans._plan_time_limited({"solver": {"status": "optimal", "time_limited": False, "first_time_limited": False}}) is False
    assert plans._status_label({"status": "optimal", "time_limited": False}) == "Solved within tolerance"
    assert plans._status_label({"status": "optimal", "time_limited": True, "first_time_limited": True}) == "Solved within the time limit"
    assert plans._status_label({"status": "feasible", "time_limited": True}) == "Solved within the time limit"
    assert plans._status_label({"status": "feasible"}) == "Solved within the time limit"                     # a result from before the flag
    assert plans._status_label({"status": "feasible", "time_limited": False, "threshold_repair": {"status": "optimal"}}) \
        == "Repaired to the site's controller"
    assert plans._status_label({"status": "not_solved"}) == plans._status_label({"status": "refused"}) == "Not solved"
