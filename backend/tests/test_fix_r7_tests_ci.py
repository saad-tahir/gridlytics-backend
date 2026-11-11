"""The engine's acceptance tests the tracker ticks (round 7, audit tracker-and-docs-08, work package tests-ci): TWN-01, VER-02, DAT-01, DAT-08, OPT-03,
OPT-04 and OPT-07 were [x] with none of their acceptance in a test. Each test below is one item's acceptance, written against the engine as stage 1 left
it; nothing reads the network or the owner's data folder (each test's company is a file of its own in a temporary folder).

Timed acceptances (DAT-01's 10 ms day read, DAT-08's 200 ms range) take the best of several runs, as `timeit` does: the best run is the work itself,
the others add what a busy machine adds (the suite runs four workers on four CPUs)."""
from __future__ import annotations

import inspect
import itertools
import re
import sqlite3
import time
import types
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import backups, demo, optimise, sites, store, telemetry, twin, verify
from gridlytics import platform as platformdb

H = 0.25
PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _best(fn, runs: int = 7) -> tuple[float, object]:
    """The best wall-clock time of `runs` calls of fn (seconds), and the last call's answer."""
    best, out = float("inf"), None
    for _ in range(runs):
        t = time.perf_counter()
        out = fn()
        best = min(best, time.perf_counter() - t)
    return best, out


# ------------------------------------------------------------------------------------------------ TWN-01: 90 days per demo site, physically consistent

def _random_slots(rng: np.random.Generator, t0: int) -> list[dict]:
    """A day of compiled slots drawn at random over the Mode A vocabulary (every mode, floors and targets, grid charging, power and grid caps, surplus
    PV to full, planned genset runs and none): the twin must stay physical whatever a plan asks of it."""
    cuts = sorted(rng.choice(np.arange(1, 24), size=5, replace=False).tolist())
    out = []
    for a, b in zip([0] + cuts, cuts + [24]):
        floor = int(rng.integers(10, 60))
        s = {"start_ts": t0 + a * 3600, "end_ts": t0 + b * 3600, "mode": str(rng.choice(twin.MODES)), "soc_floor_pct": floor,
             "soc_target_pct": int(rng.integers(max(floor, 50), 101)), "grid_charge": bool(rng.random() < 0.4), "pv_full": bool(rng.random() < 0.3)}
        if rng.random() < 0.3:
            s["max_charge_w"] = float(rng.integers(5, 200)) * 1000
        if rng.random() < 0.3:
            s["max_discharge_w"] = float(rng.integers(5, 200)) * 1000
        if rng.random() < 0.2:
            s["grid_cap_w"] = float(rng.integers(50, 400)) * 1000
        draw = rng.random()
        if draw < 0.3:
            start = t0 + a * 3600 + int(rng.integers(0, 60)) * 60
            s["genset_runs"] = [[start, start + int(rng.integers(15, 240)) * 60]]
        elif draw < 0.5:
            s["genset_runs"] = []
        out.append(s)
    return out


def _physics(site: dict, plant: twin.Plant, tr: dict, sim: dict, soc0: float, rows: list[tuple] | None) -> list[str]:
    """What a twin day must keep, minute by minute: the energy balance within 1 W, the state of charge within its bounds and following the battery's
    flows, every flow within its rating, no import while the grid is down, no output from a set that is off, nothing served or produced beyond what was
    there. `rows`: the day's telemetry, whose own balance is checked too (rounded to 0.1 W as written)."""
    bad = []
    bal_w = (sim["pv"] + sim["bat"] + sim["gen"] + sim["imp"] - sim["exp"] - sim["served"]) * 1000.0
    if np.abs(bal_w).max() > 1.0:
        bad.append(f"balance {np.abs(bal_w).max():.3f} W at minute {int(np.argmax(np.abs(bal_w)))}")
    if rows is not None:
        c = telemetry.COLUMNS
        a = np.array([[r[c.index(k)] for k in ("pv_power_w", "battery_power_w", "genset_power_w", "grid_power_w", "load_power_w")] for r in rows])
        rec = a[:, 0] + a[:, 1] + a[:, 2] + a[:, 3] - a[:, 4]
        if np.abs(rec).max() > 1.0:
            bad.append(f"recorded balance {np.abs(rec).max():.3f} W")
    tol = 1e-9
    if plant.e_nom > 0:
        soc = sim["soc"]
        if soc.min() < plant.soc_min - tol or soc.max() > plant.soc_max + tol:
            bad.append(f"state of charge {soc.min():.4f}-{soc.max():.4f} outside {plant.soc_min}-{plant.soc_max}")
        e = soc * plant.e_nom
        before = np.concatenate([[soc0 * plant.e_nom], e[:-1]])
        moved = np.where(sim["bat"] > 0, -sim["bat"] / 60 / plant.eta_d, -sim["bat"] / 60 * plant.eta_c)
        if np.abs(e - before - moved).max() > 1e-6:
            bad.append("the state of charge does not follow the battery's flows")
        if sim["bat"].max() > plant.p_dis + tol or sim["bat"].min() < -plant.p_ch - tol:
            bad.append("battery power beyond its rating")
    elif np.abs(sim["bat"]).max() > 0:
        bad.append("battery flow without a battery")
    down = tr["grid"] < 0.5
    if sim["imp"][down].max(initial=0) > tol or sim["exp"][down].max(initial=0) > tol:
        bad.append("grid flow while the grid is down")
    if sim["imp"].max() > plant.g_max + tol or sim["exp"].max() > plant.x_max + tol:
        bad.append("grid flow beyond the connection")
    if sim["gen"].max() > plant.gen_kw + tol or sim["gen"].min() < -tol or sim["gen"][~sim["gen_on"]].max(initial=0) > tol:
        bad.append("genset output outside 0 to its rating, or while off")
    if min(sim["unserved"].min(), sim["curtail"].min()) < -tol:
        bad.append("negative unserved load or curtailment")
    if (sim["pv"] > tr["pv"] + tol).any() or (sim["served"] > tr["load"] + tol).any():
        bad.append("more PV used or load served than there was")
    return bad


def test_twn01_ninety_generated_days_per_demo_site_are_physically_consistent(company):
    # TWN-01's acceptance: 90 generated days per demo site, physically consistent (balance within 1 W, state of charge within its bounds). Each demo
    # site (the grid mill with a manual set, the mall behind an AMF panel carrying the whole site, the feed mill, the off-grid farm) runs 90 days in a
    # row, its state carried from day to day as the demo carries it, on its native table every other day and on a plan's slots drawn at random the
    # others; and the mill once more with a soc_threshold controller and a set allowed to run in parallel with the grid, over 30 days
    rng = np.random.default_rng(2026)
    runs = [(sid, None, 90) for sid in demo.create_sites("t")]
    mill = next(sid for sid, _, _ in runs if "mill" in sites.full(sid)["name"].lower())
    runs.append((mill, {"gen_control": "soc_threshold", "gen_soc_on": 0.4, "parallel": True}, 30))
    bad = []
    for sid, change, days in runs:
        site = sites.full(sid)
        plant = twin.plant_of(site)
        for k, v in (change or {}).items():
            setattr(plant, k, v)
        soc, on, elapsed, out_min, rescue = 0.5, False, 0, 0, False
        for k in range(days):
            day = date(2026, 3, 1) + timedelta(days=k)
            tr = twin.truth(site, day)
            pol = twin.native_policy(site, tr["ts"]) if k % 2 == 0 else twin.slots_policy(_random_slots(rng, int(tr["ts"][0])), tr["ts"])
            sim = twin.simulate(tr, pol, plant, soc, on, elapsed, out_min, rescue)
            rows = twin.telemetry_rows(sid, tr, sim, rng_seed=k, inverter_mode=pol["mode"]) if k % 6 == 0 else None
            bad += [(site["name"], day.isoformat(), why) for why in _physics(site, plant, tr, sim, soc, rows)]
            soc, on, elapsed = float(sim["soc"][-1]), bool(sim["gen_on_end"]), int(sim["gen_elapsed_end"])
            out_min, rescue = int(sim["outage_min_end"]), bool(sim["gen_rescue_end"])
    assert not bad, bad[:10]
    assert len(runs) == 5 and sum(d for _, _, d in runs) == 390


# ------------------------------------------------------------------------------------------------ VER-02: the replay reproduces the twin's day

def test_ver02_the_replay_reproduces_the_twins_recorded_import_and_throughput(company):
    # VER-02's acceptance (the twin half; O12's real month is recorded as open in GATES): on twin days the native baseline's replay reproduces what
    # the twin recorded. Each demo site runs its native table for a week of twin days written as telemetry, as a site that never followed a plan does;
    # the counterfactual replay of that same table on the measured PV, load and outages must give back the recorded import, export, battery
    # throughput, genset hours and litres and unserved load, to the rounding of what was written (the twin's telemetry is written at 0.1 W and its
    # state of charge at 0.01 %, the ledger's figures at 0.01): within 0.05 of a unit, on days of up to 18,000 kWh
    checked = 0
    for sid in demo.create_sites("t"):
        site = sites.full(sid)
        plant = twin.plant_of(site)
        soc, on, elapsed, out_min, rescue = 0.5, False, 0, 0, False
        for k in range(7):
            day = date(2026, 6, 10) + timedelta(days=k)
            tr = twin.truth(site, day)
            pol = twin.native_policy(site, tr["ts"])
            sim = twin.simulate(tr, pol, plant, soc, on, elapsed, out_min, rescue)
            telemetry.write_rows(twin.telemetry_rows(sid, tr, sim, rng_seed=k, inverter_mode=pol["mode"]))
            soc, on, elapsed = float(sim["soc"][-1]), bool(sim["gen_on_end"]), int(sim["gen_elapsed_end"])
            out_min, rescue = int(sim["outage_min_end"]), bool(sim["gen_rescue_end"])
            row = verify.compute_day(sid, day, with_bound=False, forward=False)["native"]
            replay, actual = row["baseline"], row["actual"]
            recorded = {"import_kwh": sim["imp"].sum() / 60, "export_kwh": sim["exp"].sum() / 60, "discharge_kwh": np.maximum(sim["bat"], 0).sum() / 60,
                        "genset_hours": sim["gen_on"].sum() / 60, "litres": sim["fuel"].sum(), "unserved_kwh": sim["unserved"].sum() / 60}
            for key, value in recorded.items():
                assert replay[key] == pytest.approx(value, abs=0.05), (site["name"], day, key, replay[key], value)
                assert replay[key] == pytest.approx(actual[key], abs=0.05), (site["name"], day, key, replay[key], actual[key])
            assert row["import_unknown"] is False and row["genset_unknown"] is False
            checked += 1
    assert checked == 28


# ------------------------------------------------------------------------------------------------ DAT-01 and DAT-08: the telemetry file's reads

YEAR0 = int(datetime(2026, 1, 1, tzinfo=PKT).timestamp())


def _site_year(sid: str) -> tuple[np.ndarray, np.ndarray]:
    """A site-year of minute rows (525,600), written as the edge writes them; returns their minutes and PV (W)."""
    n = 365 * 1440
    ts = YEAR0 + 60 * np.arange(n, dtype=np.int64)
    minute = (ts + 5 * 3600) % 86400 / 60.0
    pv = np.round(np.maximum(0.0, np.sin((minute - 360) / 720 * np.pi)) * 150_000 * (1 + 0.1 * np.sin(np.arange(n) / 977)), 1)
    load = np.round(80_000 + 20_000 * np.sin(np.arange(n) / 541), 1)
    bat = np.round(np.where(pv > load, -(pv - load) * 0.5, 10_000.0), 1)
    grid = np.round(load - pv - bat, 1)
    soc = np.round(50 + 30 * np.sin(np.arange(n) / 1440 * 2 * np.pi), 2)
    rows = [(sid, int(t), "edge", 1, float(g), max(float(g), 0.0) / 60, max(-float(g), 0.0) / 60, float(p), float(lo), float(b), float(s), float(s), 31.0,
             0, 0.0, 0.0, "self_use", 0.0, 0.0) for t, g, p, lo, b, s in zip(ts, grid, pv, load, bat, soc)]
    assert telemetry.write_rows(rows) == n
    return ts, pv


def test_dat01_a_site_year_is_written_and_read_back_a_day_reads_in_under_10_ms_and_the_daily_copy_holds_it(company):
    # DAT-01's acceptance: a site-year written and read back; a day read under 10 ms; the daily backup copy includes the file
    sid = sites.create({"name": "A year of minutes"}, "t")["id"]
    ts, pv = _site_year(sid)
    back = telemetry.raw(sid, YEAR0, YEAR0 + 366 * 86400, ("pv_power_w", "battery_soc_bms_pct"))
    assert len(back["ts"]) == len(ts) and (back["ts"] == ts).all()
    assert np.array_equal(back["pv_power_w"], pv)
    # a day read as the bill engine and the replays read it (the whole minute grid of one day, every column)
    d0 = YEAR0 + 200 * 86400
    took, day = _best(lambda: telemetry.minute_grid(sid, d0, d0 + 86400))
    assert day["completeness"] == 1.0 and len(day["ts"]) == 1440
    assert np.allclose(day["pv"], pv[200 * 1440:201 * 1440] / 1000.0)
    assert took < 0.010, f"a day read took {took * 1000:.1f} ms"
    # the daily copy takes the telemetry file with the workspace file (store.backup), all of the year in it
    made = backups.daily()
    assert made is not None
    copy = backups.list_backups()[0]
    assert copy["kind"] == "daily" and copy["telemetry"] and copy["telemetry_bytes"] > 0
    with closing(sqlite3.connect(backups.folder() / copy["telemetry"])) as con:
        assert con.execute("SELECT count(*) FROM minute WHERE site_id = ?", (sid,)).fetchone()[0] == len(ts)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A company of its own with its owner signed in (as test_fix_r7_telemetry_genset_battery.py)."""
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    import identities
    co = identities.company("R7 acceptance company", own_file=True)
    db = platformdb.company_file(co)
    db.parent.mkdir(parents=True, exist_ok=True)
    owner = identities.client("owner@r7accept.test", None, co, "owner", raise_server_exceptions=False)
    return types.SimpleNamespace(db=db, owner=owner, use=lambda: store.use_company(db))


def test_dat08_a_30_day_range_asked_at_1_minute_comes_back_downsampled_in_under_200_ms(world):
    # DAT-08's acceptance: a 30-day range at 1 minute returns downsampled (about 2,000 points per series at most) in under 200 ms, through the route
    # the telemetry page asks
    with world.use():
        sid = sites.create({"name": "Thirty days"}, "t")["id"]
        _site_year(sid)
    start = datetime.fromtimestamp(YEAR0 + 100 * 86400, PKT).isoformat()
    end = datetime.fromtimestamp(YEAR0 + 130 * 86400, PKT).isoformat()
    names = "pv_power_w,load_power_w,grid_power_w,battery_power_w,battery_soc_est_pct"
    ask = lambda: world.owner.get(f"/api/sites/{sid}/telemetry", params={"start": start, "end": end, "names": names, "res": "1m"})  # noqa: E731
    took, r = _best(ask, runs=5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested_resolution"] == "1m" and body["resolution"] != "1m"                # 43,200 minutes asked, downsampled
    assert 0 < len(body["ts"]) <= telemetry.SERIES_MAX_POINTS and body["max_points"] == telemetry.SERIES_MAX_POINTS
    assert all(len(v) == len(body["ts"]) for v in body["series"].values()) and len(body["series"]) == 5
    assert took < 0.200, f"the 30-day range took {took * 1000:.0f} ms"


# ------------------------------------------------------------------------------------------------ OPT-03: a brute-force cross-check on a one-day problem

OUTAGES = [(36, 41), (70, 74)]  # 09:00-10:15 and 17:30-18:30 of the day: 9 steps the set may run in


def _one_day_genset_problem(load_mode: int) -> dict:
    """A one-day (96-step) grid site with a non-parallel 100 kW set (a manual start, 1-hour minimum up and down times as 2 steps each here, Rs 1,500 a
    start), a 200 kWh battery at 60 kWh that may not charge from the grid, 80 kW of load of which 40 kW is critical, and two outages."""
    from test_fix_optimiser import _problem
    p = _problem(T=96, genset=True)
    for a, b in OUTAGES:
        p["g"][a:b] = 0
    p["L"][:], p["Lhi"][:], p["Lc"][:] = 80.0, 88.0, 40.0
    p["load_mode"] = load_mode
    p["battery"].update({"E0": 60.0, "a_grid": False})
    p["genset"].update({"UT": 2, "DT": 2})
    return p


@pytest.mark.parametrize("load_mode", [0, 1], ids=["critical_load", "whole_site"])
def test_opt03_the_genset_commitment_matches_a_brute_force_search_on_a_one_day_problem(load_mode):
    # OPT-03's acceptance: a brute-force cross-check on a one-day problem, in both outage load modes. A set that may not run with the grid up can run
    # only in the outages' 9 steps, so its every schedule is one of 2^9: each is fixed (fix_u, as the threshold repair fixes one) and the rest of
    # the plan solved; the infeasible ones (a run under its minimum up time, a restart inside its minimum down time) are refused by the model's own
    # rows. The mixed-integer solve must find the cheapest of them, to the rupee
    steps = [t for a, b in OUTAGES for t in range(a, b)]
    milp = optimise.solve(_one_day_genset_problem(load_mode), gap=1e-9)
    assert milp["status"] == "optimal"
    u = np.round(milp["sol"]["u"])
    assert u[[t for t in range(96) if t not in steps]].sum() == 0                    # never with the grid up: the search space is complete
    found = []
    for bits in itertools.product((0, 1), repeat=len(steps)):
        fixed = np.zeros(96)
        fixed[steps] = bits
        r = optimise.solve(_one_day_genset_problem(load_mode), gap=1e-9, fix_u=fixed)
        if r["status"] == "optimal":
            found.append((r["objective_rs"], bits))
    found.sort()
    assert 10 < len(found) < 2 ** len(steps)                                           # the rows refused some schedules, and many remain
    best, best_bits = found[0]
    assert milp["objective_rs"] == pytest.approx(best, abs=0.01), (milp["objective_rs"], found[:3])
    assert tuple(int(x) for x in u[steps]) in {bits for cost, bits in found if cost <= best + 0.01}
    assert found[1][0] > best + 1.0 or found[1][1] != best_bits                       # the cheapest is a real choice among the schedules


# ------------------------------------------------------------------------------------------------ OPT-04: the reserve on 1,000 twin days

TYPES = ("office", "retail", "industrial_1_shift", "industrial_2_shift", "industrial_3_shift", "cold_store", "hospital", "school")
STEPS_PER_DAY = 96


def _reserve_day(i: int) -> tuple[dict, dict, int | None]:
    """Twin day i of the study: a battery site drawn at random (type, size, critical share, PV, battery and its power, tariff spread, state of
    charge, its SoC estimate's sigma, whether it may charge from the grid), its day's PV, load and critical load from the twin (twin.truth, 15-minute
    means; the PV's low band the step's lowest minute and the critical load the step's highest, so the forecast's bands hold the day), planned at
    midnight. Even days carry an announced outage as the forecaster gives one (p_start at its adherence, 0.2 to 0.9, on the step before the window,
    the window's length as its duration, two to 22 hours ahead); every day carries the unscheduled-trip hazard (up to 0.03 a step) or none.
    Returns the problem, the twin's minute series and the outage's first step (None on a day with no forecast outage)."""
    rng = np.random.default_rng([2026, 4, i])
    day = date(2026, 1, 1) + timedelta(days=int(rng.integers(0, 365)))
    peak_kw = float(rng.uniform(40, 300))
    e_nom = peak_kw * float(rng.uniform(1.0, 3.0))
    p_kw = e_nom * float(rng.uniform(0.3, 0.6))
    crit_share = float(rng.uniform(0.2, 0.6))
    site = {"id": f"reserve-study-{i}", "data": {"site_type": str(rng.choice(TYPES)), "load_peak_kw": peak_kw, "lat": float(rng.uniform(24.8, 33.7)),
                                                  "lon": float(rng.uniform(67.0, 74.4)), "critical_load_kw": [min(crit_share * peak_kw, 0.75 * p_kw)] * 24},
            "assets": {"pv": {"kwp_dc": peak_kw * float(rng.uniform(0.0, 1.4)) + 1.0, "kw_ac": peak_kw * 1.2}},
            "connections": [{"data": {"regime": "none", "sanctioned_load_kw": peak_kw * 2}}]}
    tr = twin.truth(site, day)
    q = lambda a: np.asarray(a, dtype=float).reshape(STEPS_PER_DAY, 15)  # noqa: E731
    L, PV = q(tr["load"]).mean(axis=1), q(tr["pv"]).mean(axis=1)
    T = STEPS_PER_DAY
    c_imp = np.full(T, float(rng.uniform(22, 35)))
    peak = np.zeros(T, dtype=bool)
    peak[68:84] = True
    c_imp[peak] = float(rng.uniform(45, 70))
    p_start = rng.uniform(0, 0.03, T) * (rng.random() < 0.5)
    dur = np.full(T, int(rng.integers(3, 7)))
    s = None
    if i % 2 == 0:
        s = int(rng.integers(9, 90))
        dur[s - 1] = min(int(rng.integers(4, 13)), T - s)
        p_start[s - 1] = float(rng.uniform(0.2, 0.9))
    p = {"T": T, "ts": tr["ts"][::15], "L": L, "Lhi": L * 1.1, "Lc": q(tr["crit"]).max(axis=1), "PV": PV, "PVlo": q(tr["pv"]).min(axis=1),
         "g": np.ones(T), "p_start": p_start, "dur_steps": dur, "c_imp": c_imp, "c_exp": np.zeros(T), "peak": peak, "in_month": np.ones(T, dtype=bool),
         "G_max": peak_kw * 3, "X_max": 0.0, "off_grid": False, "load_mode": 0, "voll": 500.0, "c_short": 500.0, "icp": {},
         "battery": {"E_nom": e_nom, "E0": e_nom * float(rng.uniform(0.1, 0.95)), "sigma0": float(rng.choice([0.0, 0.02, 0.04])), "P_ch": p_kw,
                     "P_dis": p_kw, "eta_c": 0.95, "eta_d": 0.95, "soc_min": 0.1, "soc_max": 0.95, "a_grid": bool(rng.random() < 0.85), "a_gen": False,
                     "c_cyc": 10.83, "b_cal": np.zeros(T), "charge_block": np.zeros(T, dtype=bool)},
         "terminal": {"c_next": float(c_imp.min()), "H_peak": 4.0, "R_next": 0.0, "c_cal_hold": 0.0}}
    return p, tr, s


def _outage_replay(p: dict, tr: dict, s: int, e_in: float) -> float:
    """The forecast outage as the twin runs it at 1 minute from the energy the plan entered it with: the day's own PV and critical load, no grid, the
    battery on self-use down to its cut-off. Returns the critical energy it could not serve (kWh)."""
    b = p["battery"]
    m = slice(s * 15, (s + int(p["dur_steps"][s - 1])) * 15)
    n = m.stop - m.start
    series = {"pv": tr["pv"][m], "load": tr["load"][m], "crit": tr["crit"][m], "grid": np.zeros(n), "ts": tr["ts"][m], "temp": np.full(n, 30.0)}
    pol = {"mode": np.zeros(n, dtype=int), "floor": np.full(n, b["soc_min"]), "target": np.full(n, b["soc_max"]), "max_dis": np.full(n, np.inf),
           "max_ch": np.full(n, np.inf), "grid_charge": np.zeros(n, dtype=bool), "genset": np.full(n, -1)}
    plant = twin.Plant(e_nom=b["E_nom"], soc_min=b["soc_min"], soc_max=b["soc_max"], p_ch=b["P_ch"], p_dis=b["P_dis"], g_max=1.0)
    return float(twin.simulate(series, pol, plant, e_in / b["E_nom"])["unserved"].sum()) / 60


def test_opt04_on_1000_twin_days_forecast_outages_are_entered_at_the_reserve_and_no_critical_load_is_lost():
    # OPT-04's acceptance (Part 4.3, 4.9): on 1,000 twin days, of the outages the forecaster gave p_start of at least 0.2 at least 2 hours ahead, 90 %
    # are entered with E at or above R; unserved critical energy on those days is zero; on days with no forecast outage the battery still discharges
    # into the peak. R is the reserve row's own (the critical load less the PV's low band over the outage, at most what the battery can deliver, plus
    # the SoC estimate's 2 sigma), so a battery full at its ceiling has entered as high as it can; a battery that may not charge from the grid may have
    # no way to reach R (it counts against the 90 %). The study's sites carry no genset, whose start would cover the outage at its own price: the
    # battery is the only cover, the case where the reserve rule is a safety rule
    entered = held = carried = 0
    lost, idle_peaks, offered = [], [], 0
    for i in range(1000):
        p, tr, s = _reserve_day(i)
        r = optimise.solve(p)
        assert r["status"] == "optimal", (i, r["status"])
        b, sol = p["battery"], r["sol"]
        e_min, e_max = b["soc_min"] * b["E_nom"], b["soc_max"] * b["E_nom"]
        if s is None:
            # no forecast outage: what the battery holds over the trip hazard's reserve at the peak's start goes into the peak, as far as the
            # peak's load less PV takes it (no export here)
            room = float(sol["E"][68]) - e_min - float(np.max(r["R"][67:84]))
            takes = float(np.sum(np.minimum(np.maximum(0.0, p["L"][68:84] - p["PV"][68:84]), b["P_dis"]))) * H
            if min(room, takes) > 1.0:
                offered += 1
                if float(sol["D"][68:84].sum()) * H < 0.5 * min(room, takes):
                    idle_peaks.append((i, round(room, 1), round(takes, 1)))
            continue
        entered += 1
        e_in = float(sol["E"][s])
        if e_in < min(e_min + float(r["R"][s - 1]), e_max) - 1e-3:
            continue
        held += 1
        # on a day entered at the reserve, the twin's outage loses no critical load the battery could have carried: none when the outage's critical
        # energy fits in the battery, and never more than what exceeds it
        m = slice(s * 15, (s + int(p["dur_steps"][s - 1])) * 15)
        need = float(np.sum(np.maximum(0.0, np.minimum(tr["crit"][m], b["P_dis"]) - tr["pv"][m]))) / 60 / b["eta_d"]
        beyond = max(0.0, need - (e_max - e_min)) * b["eta_d"]
        unserved = _outage_replay(p, tr, s, e_in)
        carried += need <= e_max - e_min
        if unserved > beyond + 0.1:
            lost.append((i, round(unserved, 2), round(beyond, 2)))
    assert entered == 500 and held >= 0.9 * entered, (held, entered)
    assert carried >= 0.8 * held and not lost, lost[:10]
    assert offered >= 400 and not idle_peaks, idle_peaks[:10]


# ------------------------------------------------------------------------------------------------ OPT-07: R6 6.8's worked day, and no big-M

R6_PACK = {"replacement_cost_per_kwh": 52_000.0, "warranted_cycles": 6000, "warranted_depth": 0.8}


def _trace(points: list[tuple[float, float]]) -> np.ndarray:
    """A state-of-charge trace (kWh of a 100 kWh pack) at the 97 quarter-hour boundaries of a day from 06:00, linear between reversal points
    (hours after 06:00, percent)."""
    hours, pct = zip(*points)
    return np.interp(np.arange(97) * H, hours, pct) * 1.0


def test_opt07_the_worked_100_kwh_day_of_r6_section_6_8_is_reproduced():
    # OPT-07's acceptance: the worked 100 kWh day of R6 section 6.8 reproduced. Its pack: 100 kWh of LFP at Rs 52,000 per kWh installed, a warranty of
    # 6,000 cycles at 80 % depth; its day 40 % (06:00), 95 % (13:00, PV), 30 % (21:00, the peak and a 2-hour outage), 60 % (02:00, an off-peak
    # charge), 45 % (05:00), 95 % the next day; an enclosure at 45 C in the afternoon
    c_cyc = optimise.wear_cost(R6_PACK)
    assert c_cyc == pytest.approx(52_000 / 4_800) and round(c_cyc, 1) == 10.8                 # 6.1: Rs 10.8 per kWh (52,000 / 4,800)
    E = _trace([(0, 40), (7, 95), (15, 30), (20, 60), (23, 45), (24, 95)])
    # 6.8 item 1: the day's discharges (95 to 30 and 60 to 45: 65 and 15 kWh, 80 kWh) at the flat throughput price, Rs 867, as the objective prices
    # them (the wear line of the plan's breakdown: c_cyc per kWh discharged)
    D = np.maximum(0.0, E[:-1] - E[1:]) / H
    assert float(D.sum()) * H == pytest.approx(80.0)
    temp = np.where((np.arange(96) >= 28) & (np.arange(96) < 60), 45.0, 30.0)         # 13:00 to 21:00 at 45 C
    zeros = np.zeros(96)
    p = {"c_imp": zeros, "c_exp": zeros, "voll": 500.0, "p_start": zeros, "c_short": 500.0, "peak": zeros.astype(bool),
         "battery": {"c_cyc": c_cyc, "b_cal": optimise.calendar_slope(temp, R6_PACK["replacement_cost_per_kwh"])}}
    s = {"G": zeros, "X": zeros, "D": D, "U": zeros, "F": zeros, "P": zeros, "E": E}
    out = optimise._breakdown(p, s, {})
    assert out["wear_rs"] == pytest.approx(867, abs=0.5) and out["discharge_kwh"] == pytest.approx(80.0)
    # 6.8 item 3 and the summary: the calendar slope b(45 C) is Rs 0.109 per kWh of pack per step per unit SoC (0.034 at 25 C); the optimiser prices
    # the energy held after each step with it (6.4: only the slope matters to it). Holding the pack at 95 % rather than 50 % for the 8 hours at 45 C
    # (13:00 to 21:00) costs b x 45 kWh x 32 steps, Rs 157 on the slope fitted between 20 and 95 %; 6.8 reads the same difference off its table's
    # rates (0.126 - 0.084) as Rs 134, the slope being linear where the curve flattens above 60 %
    assert optimise.calendar_slope(np.array([25.0, 45.0]), 52_000.0) == pytest.approx([0.034, 0.109])
    held = {lvl: optimise._breakdown(p, {**s, "D": zeros, "E": np.full(97, lvl)}, {})["calendar_rs"] for lvl in (50.0, 95.0)}
    hot = 0.109 * 32
    assert held[95.0] - held[50.0] == pytest.approx(45 * hot + 45 * float(np.sum(p["battery"]["b_cal"][temp < 45])), abs=0.5)
    assert 45 * hot == pytest.approx(157, abs=0.5) and 134 < 45 * hot < 1.2 * 134
    # 6.8 item 4: "the cycling decision is close on tariff alone". On a 100 kWh pack (95 % each way) a peak at Rs 48 against an off-peak at Rs 30
    # pays the wear (48 - 30 / 0.9025 = Rs 14.8 a kWh over Rs 10.8): the plan cycles; at Rs 36 off-peak it would not (Rs 8.1), and does not
    from test_fix_optimiser import _problem
    for off, cycles in ((30.0, True), (36.0, False)):
        q = _problem()
        q["c_imp"] = np.where(q["peak"], 48.0, off)
        q["battery"].update({"E_nom": 100.0, "E0": 10.0, "P_ch": 50.0, "P_dis": 50.0, "c_cyc": c_cyc})
        q["terminal"]["c_next"] = 0.0
        r = optimise.solve(q)
        assert r["status"] == "optimal"
        assert (float(r["sol"]["D"].sum()) * H > 50.0) is cycles, (off, float(r["sol"]["D"].sum()) * H)


def test_opt07_no_big_m_constant_anywhere_in_the_builder_and_no_coefficient_beyond_the_problems_own_sizes(monkeypatch):
    # OPT-07's acceptance: no 1e6 big-M anywhere (a test greps the builder). The grep: no constant of a million or more in optimise.py's model code
    # (the fuel cap's "no cap" sentinel of 1e8 is compared with an input, never written into a row, and is named here). And what a grep cannot see,
    # a big-M spelt another way: every coefficient, row bound and variable bound of the built model is within the problem's own sizes (the grid's,
    # the battery's, the set's and the tank's), on problems that write every kind of row
    builder = (optimise.Model, optimise.solve, optimise.genset_follow_cap, optimise.genset_surplus_steps, optimise.curtail_cost, optimise.power_caps,
               optimise.mdi_floors, optimise.manual_floor, optimise.manual_floor_parts, optimise.manual_floor_steps, optimise.threshold_replay)
    big = re.compile(r"(?<![\w.])(?:[1-9](?:\.\d*)?[eE]\+?(?:[6-9]|[1-9]\d)|[1-9]\d{0,2}(?:_?000){2,}(?:\.0*)?|10\s*\*\*\s*(?:[6-9]|\d\d))(?![\w.])")
    hits = [(f.__name__, line.strip()) for f in builder for line in inspect.getsource(f).splitlines() if big.search(line.split("#")[0])]
    assert hits == [("solve", 'if gen["fuel_available"] < 1e8:'), ("solve", 'if thr_t and gen["fuel_available"] < 1e8:')], hits
    seen = []
    solve = optimise.Model.solve

    def spy(self, time_limit, gap):
        res, A = solve(self, time_limit, gap)
        finite = lambda v: np.abs(np.asarray(v, dtype=float)[np.isfinite(v)]).max(initial=0.0)  # noqa: E731
        seen.append(max(float(np.abs(A.data).max(initial=0.0)), finite(self.rlb), finite(self.rub), finite(self.ub), finite(self.lb)))
        return res, A

    monkeypatch.setattr(optimise.Model, "solve", spy)
    from test_fix_optimiser import _problem
    from test_fix_r5_genset import _cases, _farm_night
    threshold = _farm_night(c_next=150.0)
    threshold["genset"].update({"control": "soc_threshold", "soc_on": 0.25})
    mdi = _problem()
    mdi["mdi"] = {"rate": 1250.0, "M0": 60.0, "floor": 50.0, "n_mdi": 2, "G_elapsed": 0.0, "n_elapsed": 0, "month_end_step": 48}  # into the next month
    reserve = _problem()
    reserve["p_start"][30:60] = 0.3
    cases = {"battery": _problem(), "outage": _problem(genset=True, outage=(40, 56)), "threshold": threshold, "mdi": mdi, "reserve": reserve,
             **_cases()}
    for name, p in cases.items():
        seen.clear()
        r = optimise.solve(p)
        assert r["status"] in ("optimal", "feasible") and seen, name
        sizes = [p["G_max"], p.get("X_max", 0.0), float(np.max(p["L"])) * 1.5]
        if p.get("battery"):
            sizes.append(p["battery"]["E_nom"])
        if p.get("genset"):
            sizes += [p["genset"]["S"], *([p["genset"]["fuel_available"]] if p["genset"]["fuel_available"] < 1e8 else [])]
        assert max(seen) <= max(sizes) + 1e-6, (name, max(seen), max(sizes))
