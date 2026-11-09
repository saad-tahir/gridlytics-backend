"""Round-3 regressions for the optimiser group: a force-charge set in an outage, the time-limited off-grid threshold plan that sheds instead of
starting the set, the ICP rows at legacy net-metering sites, and plans.held_info for GET /plan while control is paused or released."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import forecast, optimise, plans, sites, store, telemetry, twin
from test_fix_optimiser import _now_t0, _problem, _site, _until  # noqa: F401  (the round-1 fixtures and helpers)
from test_fix_r2_optimiser import _connection, _floor


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


# ------------------------------------------------------------------------------------------------ regression-force-charge-set-in-outage-noop

def _with_outage(bundle: dict, steps: int) -> dict:
    out = dict(bundle["outage"])
    g = np.array(out["g"], dtype=float)
    g[:steps] = 0.0
    out["g"] = g
    return {**bundle, "outage": out}


def test_a_force_charge_set_in_an_outage_charges_to_its_target_once_the_grid_returns(company):
    sid = _site(genset=False)
    sites.put_state(sid, soc=0.3)
    sites.add_override(sid, {"kind": "force_charge", "value": 90, "until": _until(4), "reason": "test"}, "t")
    full = sites.full(sid)
    t0 = _now_t0()
    p = plans.build_problem(full, t0, _with_outage(forecast.issue(full, t0, save=False), 8), 96)
    window = np.flatnonzero(np.asarray(p["reserve_manual_force"]) > 0)
    assert window[0] == 0 and 15 <= len(window) <= 17 and not np.asarray(p["reserve_manual_hold"]).any()
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    # rows on every grid-up step of the window: none in the outage, all of them once the grid is back
    assert sorted(r["manual_floor"]) == [int(t) for t in window if t >= 8]
    E, En = r["sol"]["E"], p["battery"]["E_nom"]
    assert E[window[-1] + 1] >= 0.9 * En - 1e-3                               # at its target by the window's end
    # (no assertion on when it charges: the test runs at the wall-clock hour, and in daylight PV fills the battery in the outage itself; the rows
    #  on every grid-up step and the target by the window's end are the rule)


def test_a_hold_set_in_an_outage_still_lapses_and_a_force_charge_beside_it_does_not():
    base = _problem(outage=(0, 8))
    base["battery"]["E0"] = 60.0
    floor = _floor(96, slice(0, 16), 180.0)
    hold = dict(base, reserve_manual=floor, reserve_manual_hold=floor, reserve_manual_force=np.zeros(96))
    assert optimise.solve(hold)["manual_floor"] == {}                        # the verifier's rule for a Hold stands
    force = dict(base, reserve_manual=floor, reserve_manual_hold=np.zeros(96), reserve_manual_force=floor)
    r = optimise.solve(force)
    assert sorted(r["manual_floor"]) == list(range(8, 16)) and r["sol"]["E"][16] >= 180.0 - 1e-3
    # a caller that drops `reserve_manual` (savings' counterfactual) drops both parts with it
    gone = dict(base, reserve_manual_hold=floor, reserve_manual_force=floor)
    assert optimise.solve(gone)["manual_floor"] == {}


@pytest.mark.parametrize("outage, window", [((4, 12), slice(0, 24)), ((8, 16), slice(0, 24)), ((0, 8), slice(0, 16))])
def test_a_force_charge_window_around_an_outage_sheds_no_more_in_the_outage_than_no_override(outage, window):
    """regression-force-charge-window-sheds-critical-load-in-outage: rows after the outage priced at VoLL per step made the plan shed the outage's
    critical load to keep the battery full. Shed in the window's outages now counts against the rows that follow (the shed sum starts at the window)."""
    base = _problem(outage=outage)
    base["battery"]["E0"] = 60.0
    o = list(range(*outage))
    none = optimise.solve(dict(base))
    shed_none = float(none["sol"]["U"][o].sum()) * 0.25
    floor = _floor(96, window, 180.0)
    r = optimise.solve(dict(base, reserve_manual=floor, reserve_manual_hold=np.zeros(96), reserve_manual_force=floor))
    assert r["status"] == "optimal"
    assert float(r["sol"]["U"][o].sum()) * 0.25 <= shed_none + 22.5          # at most what the battery at 30 % cannot carry (r1 shed 22 kWh at 0-8)
    if outage[0] > 0:
        assert float(r["sol"]["U"][o].sum()) * 0.25 <= shed_none + 1e-3      # an outage inside the window: no extra shed at all
    assert r["sol"]["E"][window.stop] >= 180.0 - 1e-3                        # and the target is still reached by the window's end


# ------------------------------------------------------------------------------------------------ regression-threshold-offgrid-solve-time

def test_a_time_limited_off_grid_threshold_plan_with_a_full_tank_never_sheds_instead_of_starting_the_set(company):
    # the farm with a full 400 L tank at 21:00 in Pakistan on 4 November: the 10 s solve stopped at its limit with a plan that shed 51.6 kWh over
    # the night with the set off, the battery on its 25 % threshold and over 360 L in the tank
    s = sites.create({"name": "Farm", "critical_load_kw": [8.0] * 24, "genset_start_control": "soc_threshold", "outage_load_mode": 1, "site_type": "farm"}, "t")
    sid = s["id"]
    sites.put_asset(sid, "pv", {"kwp_dc": 80, "kw_ac": 70}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 120, "p_ch_kw": 40, "p_dis_kw": 40, "soc_min": 0.15}, "t")
    sites.put_asset(sid, "genset", {"rated_kw": 48, "f_min": 0.3, "min_up_h": 1.0, "tank_l": 400}, "t")
    sites.put_state(sid, soc=0.5, fuel_l=400.0)
    full = sites.full(sid)
    t0 = forecast.align(datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc).timestamp())
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 192)
    assert p["off_grid"] and p["genset"]["control"] == "soc_threshold" and p["genset"]["fuel_available"] == 400.0
    r = optimise.solve(p, time_limit=10.0)
    assert r["status"] in ("optimal", "feasible")
    sol, gen, bat = r["sol"], p["genset"], p["battery"]
    cut_off = bat["soc_min"] * bat["E_nom"]
    burn = 0.25 * (gen["a"] * gen["f_min"] * gen["S"] + gen["b"] * gen["S"])
    dodged = [t for t in range(191)
              if sol["U"][t] > max(0.0, p["L"][t] - p["PV"][t] - bat["P_dis"]) + 1e-3  # more than the battery's power cannot carry
              and sol["u"][t] < 0.5 and sol["u"][t + 1] < 0.5                        # the set off, and not started next
              and sol["E"][t + 1] > cut_off + 1e-3                                     # the battery above its cut-off
              and float(sol["F"][:t].sum()) < gen["fuel_available"] - burn]            # fuel in the tank
    assert dodged == [], (dodged, r.get("shed_rule_resolve"))
    assert float(sol["U"].sum()) * 0.25 < 1.5                                          # 51.6 kWh before
    assert r["solve_seconds"] < 25.0                                                   # at most two solves at the limit


def _offgrid_threshold(fuel: float, voll: float) -> dict:
    p = _problem(T=48, genset=True)
    p.update({"off_grid": True, "g": np.zeros(48), "G_max": 0.0, "X_max": 0.0, "c_imp": np.zeros(48), "c_exp": np.zeros(48), "voll": voll})
    p["L"][:], p["Lhi"][:], p["Lc"][:] = 40.0, 45.0, 20.0
    p["battery"].update({"a_grid": False, "a_gen": True, "E0": 100.0})
    p["terminal"]["c_next"] = 0.0
    p["genset"].update({"control": "soc_threshold", "soc_on": 0.3, "fuel_available": fuel})
    return p


def _as_if_time_limited(monkeypatch):
    """The first solve of a call reports that it stopped at its limit (its incumbent is the optimum), as a two-day farm solve does."""
    real, seen = optimise.Model.solve, []

    def limited(self, time_limit, gap):
        res, A = real(self, time_limit, gap)
        seen.append(int(res.status))
        if len(seen) == 1 and res.status == 0:
            res.status = 1
        return res, A

    monkeypatch.setattr(optimise.Model, "solve", limited)
    return seen


def _dodges(p: dict, r: dict) -> list[int]:
    sol, bat = r["sol"], p["battery"]
    return [t for t in range(p["T"] - 1) if sol["U"][t] > max(0.0, p["L"][t] - p["PV"][t] - bat["P_dis"]) + 1e-3 and sol["u"][t] < 0.5
            and sol["u"][t + 1] < 0.5 and sol["E"][t + 1] > bat["soc_min"] * bat["E_nom"] + 1e-3]


def test_a_limited_off_grid_plan_that_sheds_with_fuel_left_over_is_solved_again_with_the_shed_rule(monkeypatch):
    # with VoLL under the set's cost the cheapest plan sheds to keep the battery on its threshold, which the site cannot do: the inverter sheds
    # only at its cut-off, and under the threshold the controller starts the set
    first = optimise.solve(_offgrid_threshold(1e9, 50.0))
    # since round 4 (HANDOFF 8.A.3) an optimal first plan is checked as well: the first problem's optimum dodges here, so it is solved again
    assert first["status"] in ("optimal", "feasible") and first["shed_rule_resolve"]["shed_steps"] > 0
    assert _dodges(_offgrid_threshold(1e9, 50.0), first) == []
    seen = _as_if_time_limited(monkeypatch)
    p = _offgrid_threshold(1e9, 50.0)
    r = optimise.solve(p)
    assert len(seen) == 2 and r["shed_rule_resolve"]["shed_steps"] > 0
    # under a loaded test run (-n 4) the re-solve can stop at its own limit with a good incumbent: what matters is that it no longer dodges
    assert r["status"] in ("optimal", "feasible") and _dodges(p, r) == [] and r["sol"]["u"].sum() > 0


def test_a_limited_off_grid_plan_that_burns_its_whole_tank_is_repaired(monkeypatch):
    # a plan that burns the whole tank is not solved again with the shed rule (round 2's rule for part-full tanks). Since round 4 (HANDOFF 8.A.3)
    # its dodges are repaired instead: the threshold controller is replayed over it and one fast solve with the genset fixed follows, so the
    # shortfall the tank leaves is shed where the site would shed it, after the tank, never with the set off and fuel in the tank
    p = _offgrid_threshold(6.0, 500.0)
    seen = _as_if_time_limited(monkeypatch)
    r = optimise.solve(p)
    assert r["status"] == "feasible" and len(seen) == 2 and "shed_rule_resolve" not in r and r["threshold_repair"]["status"] == "optimal"
    assert float(r["sol"]["F"].sum()) == pytest.approx(6.0, abs=0.01) and r["sol"]["U"].sum() > 0 and _dodges(p, r) == []


# ------------------------------------------------------------------------------------------------ tariffs-legacy-degenerate-lp

def test_an_icp_eligible_legacy_site_never_plans_import_and_export_at_once(company):
    sid = _site(genset=False)
    full = _connection(sid, regime="net_metering_legacy", export_cap_kw=200,
                       icp={"eligible": True, "peak": {"11": 2000.0}, "off_peak": {"11": 8000.0}})
    t0 = twin.day_start_utc(date(2026, 11, 20))
    rows = [(sid, int(t), "test", 1.0, 1000.0, 0.0, 60000.0) for t in range(t0 - 2 * 86400, t0, 60)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "grid_export_wh", "load_power_w"))
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 192)
    assert p["regime"] == "net_metering_legacy" and {v["case"] for v in p["icp"].values()} == {"uncertain"}
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    G, X = r["sol"]["G"], r["sol"]["X"]
    assert int(np.sum((G > 0.1) & (X > 0.1))) == 0                           # 20 of 192 steps on gross import
    # elsewhere the rows still read gross import: a net-billing export is not netted on the bill
    q = dict(p, regime="net_billing")
    assert optimise.solve(q)["status"] == "optimal"


# ------------------------------------------------------------------------------------------------ ask: GET /plan while held

def test_held_info_names_the_pause_its_end_and_the_plan_held_under_it(company):
    sid = _site()
    assert plans.tick(sid)["status"] == "active"
    assert plans.held_info(sid) is None                                       # a plan is in force
    until = _until(6)
    sites.add_override(sid, {"kind": "pause_control", "until": until, "reason": "test"}, "t")
    early = plans.held_info(sid)
    assert early["reason"] == "control_paused" and early["plan_id"] is None   # paused, no tick under the pause yet
    r = plans.tick(sid)
    assert r["status"] == "held"
    h = plans.held_info(sid, int(datetime.now(timezone.utc).timestamp()))
    assert h["reason"] == "control_paused" and h["plan_id"] == r["plan_id"] and h["kind"] == "pause_control"
    assert plans._ts(h["until"]) == plans._ts(until)
    later = int((datetime.now(timezone.utc) + timedelta(hours=7)).timestamp())
    assert plans.held_info(sid, later) == {"reason": "control_paused", "until": None, "plan_id": r["plan_id"]}  # ended, no tick since


def test_held_info_names_a_release_to_native_and_a_guard_hold(company, monkeypatch):
    sid = _site()
    sites.add_override(sid, {"kind": "force_native", "until": _until(2), "reason": "test"}, "t")
    assert plans.held_info(sid)["reason"] == "released_to_native"
    other = _site()
    monkeypatch.setattr(plans, "_guard", lambda *a, **k: "guard_test")
    r = plans.tick(other)
    assert r["status"] == "held"
    assert plans.held_info(other) == {"reason": "guard_test", "until": None, "plan_id": r["plan_id"]}


# ------------------------------------------------------------------------------------------------ demo-savings diagnosis (lead, after round 3)

def _steps_for(modes_kw: list[tuple[str, float, float]], t0: int = 1794000000 // 86400 * 86400 - 5 * 3600) -> list[dict]:
    """Plan steps for one local day from (reason, charge_kw, discharge_kw) per quarter, 96 of them."""
    out = []
    for i, (reason, ch, dis) in enumerate(modes_kw):
        out.append({"t": t0 + 900 * i, "charge_kw": ch, "discharge_kw": dis, "grid_import_kw": 900.0 if ch > 0 else 400.0, "grid_export_kw": 0.0,
                    "soc_start_pct": 50.0, "soc_pct": 50.0, "reserve_pct": 20.0, "genset_on": False, "reason": reason, "grid_present": 1,
                    "pv_kw": 0.0, "load_kw": 400.0, "grid_charge_permitted": True})
    return out


def test_a_hold_before_the_evening_discharge_folds_into_the_charge_not_the_discharge():
    """Sungrow allows 2 slots a day. A plan of night charging, an afternoon hold and a peak discharge used to compile as charge then a discharge slot
    starting at the hold (16:00): the device emptied the battery off-peak. The hold now folds into the charge slot and the discharge keeps its start."""
    from gridlytics import delivery
    day = [("charge_from_grid", 100.0, 0.0)] * 24 + [("hold_for_outage", 0.0, 0.0)] * 59 + [("discharge_peak_window", 0.0, 250.0)] * 13
    steps = _steps_for(day)
    for i, st in enumerate(steps):  # the battery state the intents read
        st["soc_pct"] = st["soc_start_pct"] = min(90.0, 20.0 + i * 3) if i < 24 else (90.0 if i < 83 else 90.0 - (i - 82) * 5)
    out = delivery.compile_mode_a(steps, "sungrow", {"E_nom": 1000.0, "soc_min": 0.1, "soc_max": 0.95}, 20.0, 10.0, 900.0)
    slots = out["days"][0]["slots"]
    assert len(slots) <= 2
    dis = [s for s in slots if s["mode"] in delivery.DISCHARGING]
    assert dis and dis[0]["start_ts"] == steps[83]["t"], slots           # the discharge starts where the plan's does (20:45 local), not at the hold
    charge = [s for s in slots if s["mode"].startswith("charge")]
    assert charge and charge[0]["grid_cap_w"] == 900_000                  # a charging slot is capped at the plan's MDI cap, not its largest import


def test_plans_never_count_on_charging_from_the_genset_which_no_brand_can_be_told_to_do(company):
    from gridlytics import delivery
    assert delivery.GEN_CHARGE_SUPPORTED is False
    sid = _site()
    full = sites.full(sid)
    assert full["data"].get("gen_charge_allowed", True) is True
    t0 = _now_t0()
    p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
    assert p["battery"]["a_gen"] is False                                    # the flag is kept, but the plan does not count on it
    # (the off-grid "charge only from PV beyond the load" row was tried and dropped: it made the 120 L farm solve 23 s against 6 s)
