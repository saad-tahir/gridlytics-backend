"""Regression tests for the savings group's confirmed review findings (verify.py, twin.py, demo.py): each test fails on the code before its fix."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import demo, schemas, sites, store, tariffs, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))
WEEKDAY = date(2026, 10, 14)  # a Wednesday


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def make_site(name="Mill", control="manual", load_mode=0, tier="closed_loop", twin_extra=None, demo_link=None, conn=None, battery=None, genset=None,
              native=None, crit=70.0, load_peak_kw=330):
    s = sites.create({"name": name, "lat": 31.42, "lon": 73.08, "disco": "FESCO", "site_type": "industrial_2_shift", "genset_start_control": control,
                      "outage_load_mode": load_mode, "critical_load_kw": [crit] * 24}, "t", demo_link=demo_link,
                     site_id="t-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))  # a fixed id: the twin seeds its days by it
    sid = s["id"]
    data = {**s["data"], "load_peak_kw": load_peak_kw, "twin": twin_extra if twin_extra is not None else {"windows": [], "trips_per_day": 0.0}}
    with store.connect() as con:
        con.execute("UPDATE sites SET data = ?, status = 'active' WHERE id = ?", (json.dumps(data), sid))
    sites.put_connection(sid, conn or {"tariff_category": "B-2(b)", "sanctioned_load_kw": 500, "regime": "none"}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 300, "kw_ac": 250}, "t")
    if battery is not False:
        sites.put_asset(sid, "battery", battery or {"nameplate_kwh": 215, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    if genset is not False:
        sites.put_asset(sid, "genset", genset or {"rated_kw": 200, "f_min": 0.3, "fuel_kind": "hsd", "min_up_h": 1.0}, "t")
    sites.set_tier_capable(sid, "closed_loop")
    sites.set_tier_allowed(sid, tier, "t")
    sites.capture_native(sid, native or {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 20}, "t")
    standing_plan(sid)
    return sid


def standing_plan(sid):
    """A plan in force on every test day (made in 2015, valid to 2100), as the 15-minute ticks keep one in force at a live site: a closed-loop or demo
    day claims only while a plan was in force (round 7, audit engine-savings-02). Stored as superseded, so `plans.current` still reads only the plans
    a test makes."""
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status) VALUES (?,?,?,?,?,?,?,?)",
                    (store.new_id(), sid, "2015-01-01T00:00:00+00:00", "2015-01-01T00:00:00+00:00", "2015-01-01T00:00:00+00:00",
                     "2100-01-01T00:00:00+00:00", "a", "superseded"))


def twin_day(sid, day, soc0, seed=1, policy=None):
    """A twin day written as telemetry (the native settings unless a policy is given)."""
    site = sites.full(sid)
    tr = twin.truth(site, day)
    pol = policy(site, tr["ts"]) if policy else twin.native_policy(site, tr["ts"])
    sim = twin.simulate(tr, pol, twin.plant_of(site), soc0)
    rows = twin.telemetry_rows(sid, tr, sim, rng_seed=seed, inverter_mode=pol["mode"])
    telemetry.write_rows(rows)
    return tr, sim, rows


def _blank(rows, *cols):
    idx = {telemetry.COLUMNS.index(c) for c in cols}
    return [tuple(None if k in idx else v for k, v in enumerate(r)) for r in rows]


def _bms_only(rows):
    """What a CSV can carry: no estimate column, and a perfect BMS reading in its place."""
    i_est, i_bms = telemetry.COLUMNS.index("battery_soc_est_pct"), telemetry.COLUMNS.index("battery_soc_bms_pct")
    return [tuple(None if k == i_est else (r[i_est] if k == i_bms else v) for k, v in enumerate(r)) for r in rows]


def _series(day=WEEKDAY, pv=0.0, load=100.0, crit=70.0, outage=(600, 780)):
    ts = twin.minutes_of(day)
    n = len(ts)
    grid = np.ones(n)
    if outage:
        grid[outage[0]:outage[1]] = 0.0
    return {"ts": ts, "pv": np.full(n, float(pv)), "load": np.full(n, float(load)), "crit": np.full(n, float(crit)), "grid": grid}


def _plant(**kw):
    p = twin.Plant(e_nom=215.0, soc_min=0.1, soc_max=0.95, p_ch=100.0, p_dis=100.0, g_max=625.0, gen_kw=200.0, gen_fmin=0.3, gen_control="manual",
                   gen_min_run=60)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _row(saving=0.0, claimed=0.0, fidelity="confirmed", peak_a=0.0, peak_b=0.0, rate=0.0, floor=None, e_end_a=0.0, e_end_b=0.0, v_end=0.0, cost=0.0):
    r = {"saving_rs": saving, "claimed_rs": claimed, "levers": {"peak_window": saving}, "fidelity": fidelity, "litres_saved": 0.0,
         "actual": {"cost_rs": cost, "e_end_kwh": e_end_a, "v_end": v_end}, "baseline": {"cost_rs": cost + saving, "e_end_kwh": e_end_b, "v_end": v_end},
         "peak_kw_actual": peak_a, "peak_kw_baseline": peak_b, "mdi_rate": rate, "unserved_kwh_actual": 0.0, "unserved_kwh_baseline": 0.0}
    if floor is not None:
        r["mdi_floor_kw"] = floor
    return r


def _put_rows(sid, rows: dict):
    with store.connect() as con:
        for day, by in rows.items():
            for base, data in by.items():
                con.execute("INSERT OR REPLACE INTO savings_days (site_id, day_local, baseline, data, computed) VALUES (?,?,?,?,?)",
                            (sid, day, base, json.dumps(data), store._now()))


def _days(d0: date, d1: date):
    d = d0
    while d <= d1:
        yield d
        d += timedelta(days=1)


# ------------------------------------------------------------------------------------------------ tariffs-verify-icp-priced-at-tou

def test_the_ledger_prices_an_above_baseline_icp_month_at_the_icp_rate(company):
    sid = make_site(name="ICP", conn={"tariff_category": "B-2(b)", "sanctioned_load_kw": 500, "regime": "none",
                                      "icp": {"eligible": True, "peak": {"11": 100.0}, "off_peak": {"11": 1000.0}}})
    conn = sites.connections(sid)[0]
    t0 = twin.day_start_utc(date(2026, 11, 1))
    cols = ("site_id", "ts", "source_id", "grid_present", "grid_import_wh")
    telemetry.write_rows([(sid, t, "csv", 1.0, 100.0 * 1000 / 60) for t in range(t0, t0 + 3 * 86400, 60)], cols)  # 100 kW flat for three days
    day = date(2026, 11, 2)
    pr = verify._minute_prices(conn, day, sid)
    start = datetime.fromtimestamp(twin.day_start_utc(day), timezone.utc)
    above = tariffs.vectors(conn, start, 96, {"peak": 1e7, "off_peak": 1e7}, None, 0.0)["c_imp"]
    tou = tariffs.vectors(conn, start, 96, {"peak": 0.0, "off_peak": 0.0}, None, 1e9)["c_imp"]
    assert np.allclose(pr["c_imp"][::15], above)
    assert pr["icp"] == {"peak": "above", "off_peak": "above"}
    assert not np.allclose(above, tou)  # the TOU price the ledger used before
    # the bill engine's own marginal: one more kWh in a peak and an off-peak minute costs the ledger's price
    ts = np.arange(t0, t0 + 3 * 86400, 60)
    imp = np.full(len(ts), 100.0)
    imp[1440 + 600:1440 + 630] = 150.0  # the MDI block, so the bump moves no fixed charge
    base = tariffs.rebuild(ts, imp, np.zeros(len(ts)), conn, date(2026, 11, 1), date(2026, 11, 3))
    for minute in (19 * 60 + 5, 3 * 60 + 5):
        bumped = imp.copy()
        bumped[1440 + minute] += 60.0
        after = tariffs.rebuild(ts, bumped, np.zeros(len(ts)), conn, date(2026, 11, 1), date(2026, 11, 3))
        assert after["bearable_rs"] - base["bearable_rs"] == pytest.approx(pr["c_imp"][minute], rel=1e-3, abs=0.02)


# ------------------------------------------------------------------------------------------------ savings-csv-soc-missing

def test_a_bms_only_day_replays_from_the_real_state_of_charge(company):
    sid = make_site(name="BMS 3", load_peak_kw=150)  # PV charges the battery at noon and self-use empties it in the evening
    prev, day = WEEKDAY - timedelta(days=1), WEEKDAY
    _, sim_prev, rows_prev = twin_day(sid, prev, 0.9, seed=1)
    _, sim, rows = twin_day(sid, day, float(sim_prev["soc"][-1]), seed=2)
    assert sim["soc"].max() - sim["soc"].min() > 0.2 and abs(sim["soc"][-1] - 0.5) > 0.1
    e_end = float(sim["soc"][-1]) * 215.0
    full = verify.compute_day(sid, day, with_bound=False)["native"]
    assert abs(full["saving_rs"]) < 2.0  # a native twin day replays exactly, minute 0 counted once
    assert full["actual"]["e_end_kwh"] == pytest.approx(e_end, abs=0.1)
    # a CSV source: the BMS alone, and its first and last ten minutes of the day blank
    edge = [i for i, r in enumerate(rows) if i < 10 or i >= len(rows) - 10]
    i_bms = telemetry.COLUMNS.index("battery_soc_bms_pct")
    bms_rows = [tuple(None if (k == i_bms and i in edge) else v for k, v in enumerate(r)) for i, r in enumerate(_bms_only(rows))]
    telemetry.delete_site(sid)
    telemetry.write_rows(_bms_only(rows_prev) + bms_rows)
    bms = verify.compute_day(sid, day, with_bound=False)["native"]
    assert abs(bms["saving_rs"]) < 2.0  # before: replays from 50 percent and books the end at 50 percent
    assert bms["actual"]["e_end_kwh"] == pytest.approx(e_end, abs=0.2)
    assert bms["baseline"]["import_kwh"] == pytest.approx(full["baseline"]["import_kwh"], abs=0.5)
    # no reading the evening before either: the day's first reading with minute 0's flow backed out
    telemetry.delete_site(sid)
    telemetry.write_rows(_bms_only(rows))
    first = verify.compute_day(sid, day, with_bound=False)["native"]
    assert abs(first["saving_rs"]) < 2.0
    # no state of charge at all: the day cannot be scored, rather than scored from 50 percent
    telemetry.delete_site(sid)
    telemetry.write_rows(_blank(rows_prev + rows, "battery_soc_est_pct", "battery_soc_bms_pct"))
    assert verify.compute_day(sid, day, with_bound=False) is None


# ------------------------------------------------------------------------------------------------ savings-unmetered-fuel-zero

def test_an_unmetered_genset_is_priced_from_the_curve_or_withheld(company):
    sid = make_site(name="Fuel", control="auto_on_grid_loss", twin_extra={"windows": [["14:00", "16:00"]], "adherence": 1.0, "trips_per_day": 0.0})
    prev, day = WEEKDAY - timedelta(days=1), WEEKDAY
    _, sim_prev, rows_prev = twin_day(sid, prev, 0.9, seed=1)
    _, sim, rows = twin_day(sid, day, float(sim_prev["soc"][-1]), seed=2)
    assert sim["fuel"].sum() > 10  # the genset ran in the outage
    metered = verify.compute_day(sid, day, with_bound=False)["native"]
    genset_rs = metered["baseline"]["genset_rs"]
    assert genset_rs > 1000
    assert abs(metered["levers"]["genset"]) < 0.02 * genset_rs
    for blanked in (("genset_fuel_l",), ("genset_fuel_l", "genset_power_w")):
        telemetry.delete_site(sid)
        telemetry.write_rows(rows_prev + _blank(rows, *blanked))
        r = verify.compute_day(sid, day, with_bound=False)["native"]
        assert abs(r["levers"]["genset"]) < 0.05 * genset_rs, blanked  # before: the native arm's whole genset bill
        assert r["actual"]["litres"] == pytest.approx(metered["actual"]["litres"], rel=0.05)
        assert r["genset_unknown"] is False
    telemetry.delete_site(sid)
    telemetry.write_rows(rows_prev + _blank(rows, "genset_fuel_l", "genset_power_w", "genset_running"))
    r = verify.compute_day(sid, day, with_bound=False)
    n = r["native"]
    assert n["genset_unknown"] is True and n["levers"]["genset"] is None and n["litres_saved"] is None
    assert n["saving_rs"] == pytest.approx(sum(v for v in n["levers"].values() if v is not None), abs=0.02)
    assert n["saving_rs"] < metered["saving_rs"] + 0.5 * genset_rs
    s = verify.summary(sid, day.isoformat(), day.isoformat())  # a withheld lever adds nothing and does not break the roll-up
    assert s["baselines"]["native"]["genset_unknown_days"] == 1


# ------------------------------------------------------------------------------------------------ savings-demo-manual-genset-blackout

def test_no_planned_run_still_lets_the_operator_rescue_a_site_about_to_go_dark():
    series = _series(outage=(600, 780))
    plant = _plant()
    pol = twin.slots_policy([], series["ts"])
    pol["genset"][:] = 0  # the demo plan arm outside a planned run
    sim = twin.simulate(series, pol, plant, 0.12)
    native = twin.simulate(series, twin.slots_policy([], series["ts"]), plant, 0.12)  # -1: the operator's own rule
    assert sim["gen_on"][600:780].sum() > 150  # before: never started, 170 minutes dark
    assert sim["unserved"].sum() / 60 <= native["unserved"].sum() / 60 + 0.5
    assert not sim["gen_on"][780:].any()  # stops when the grid returns
    # a planned 30-minute run whose minimum run ends with the battery still at its floor runs on as one rescue, not a stop and a restart
    pol["genset"][605:635] = 1
    sim = twin.simulate(series, pol, plant, 0.12)
    on = sim["gen_on"].astype(int)
    assert int(np.sum(np.diff(on) == 1)) == 1 and on[605:780].all() and sim["unserved"][640:780].sum() == 0.0


# ------------------------------------------------------------------------------------------------ savings-parallel-genset-never-stops

def test_a_parallel_genset_stops_after_the_grid_returns_and_runs_when_a_plan_asks():
    series = _series(outage=(600, 660))
    for parallel in (False, True):
        sim = twin.simulate(series, twin.slots_policy([], series["ts"]), _plant(gen_control="auto_on_grid_loss", parallel=parallel), 0.5)
        assert int(sim["gen_on"].sum()) == 60, parallel  # before: 840 minutes with parallel_allowed
    pol = twin.slots_policy([], series["ts"])
    pol["genset"][1080:1140] = 1  # a planned parallel run at the evening peak, grid present
    sim = twin.simulate(series, pol, _plant(gen_control="manual", parallel=True), 0.5)
    assert sim["gen_on"][1080:1140].all() and not sim["gen_on"][1140:].any()


# ------------------------------------------------------------------------------------------------ savings-mdi-lever-ignores-floor

def test_the_mdi_lever_respects_the_billing_floor_and_the_mdi_interval(company):
    sid = make_site(name="MDI")
    _put_rows(sid, {"2026-10-10": {b: _row(peak_a=80.0, peak_b=120.0, rate=1250.0, floor=125.0) for b in ("native", "tuned")}})
    s = verify.summary(sid, "2026-10-01", "2026-10-31")
    assert s["baselines"]["native"]["levers"]["mdi"] == 0.0  # before: Rs 50,000 though the bill is the same at both peaks
    assert s["baselines"]["native"]["claimed_rs"] == 0.0
    _put_rows(sid, {"2026-10-10": {b: _row(peak_a=80.0, peak_b=120.0, rate=1250.0, floor=100.0) for b in ("native", "tuned")}})
    n = verify.summary(sid, "2026-10-01", "2026-10-31")["baselines"]["native"]
    # the month's lever is 25,000; its one scored day carries 1/31 of it (round 7, audit flow-savings-pilot-bill-01: a day's share, not the month's)
    assert n["mdi_months"]["2026-10"]["lever_rs"] == pytest.approx(25_000.0) and n["levers"]["mdi"] == pytest.approx(25_000.0 / 31, abs=0.01)
    # 15-minute MDI blocks, as the bill engine reads them
    conn = {"id": "c1", "data": {"disco_group": "xwdisco", "tariff_category": "B-2(b)", "regime": "none", "sanctioned_load_kw": 400, "export_cap_kw": 0,
                                 "mdi_interval_min": 15, "icp": {"eligible": False}, "tax": {"province": "punjab"}, "seasonal_multiplier": 1.0,
                                 "season_months": []}}
    d = date(2026, 11, 2)
    ts = twin.minutes_of(d)
    imp = np.full(1440, 100.0)
    imp[600:615] = 200.0
    z = np.zeros(1440)
    sim = {"imp": imp, "exp": z, "bat": z, "gen": z, "fuel": z, "gen_on": np.zeros(1440, dtype=bool), "unserved": z}
    pr = verify._minute_prices(conn, d, sid)
    assert pr["mdi_interval"] == 15
    assert verify.price_arm({"assets": {}}, sim, pr, d, 0.0, 0.0)["peak_kw"] == pytest.approx(tariffs.rebuild(ts, imp, z, conn, d, d)["mdi_kw"]) == 200.0


# ------------------------------------------------------------------------------------------------ savings-fidelity-gate-bypassed

def _slots_plan(day):
    t0 = twin.day_start_utc(day)
    mk = lambda a, b, mode, floor: {"start_ts": t0 + a * 3600, "end_ts": t0 + b * 3600, "mode": mode, "soc_floor_pct": floor, "soc_target_pct": 100.0,
                                    "grid_charge": False, "max_discharge_w": None, "max_charge_w": None, "genset": None}
    return [mk(0, 18, "hold", 20.0), mk(18, 22, "discharge", 20.0), mk(22, 24, "hold", 20.0)]


def _insert_plan(sid, day, slots, sim):
    q = np.asarray(sim["bat"]).reshape(96, 15).mean(axis=1)
    t0 = twin.day_start_utc(day)
    steps = [{"t": t0 + 900 * i, "discharge_kw": float(max(q[i], 0.0)), "charge_kw": float(max(-q[i], 0.0)), "genset_on": False} for i in range(96)]
    pid = store.new_id()
    now = store._now()
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, steps, slots) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, sid, now, now, now, now, "mode_a", "active", json.dumps(steps), json.dumps({"days": [{"day": day.isoformat(), "slots": slots}]})))
    return pid


def _mark(sid, pid, day):
    with store.connect() as con:
        con.execute("INSERT INTO applied (id, site_id, plan_id, day_local, by, method, created) VALUES (?,?,?,?,?,?,?)",
                    (store.new_id(), sid, pid, day.isoformat(), "t", "owner_keyed", store._now()))


def test_fidelity_gates_monitor_demo_sites_and_unconfirmed_marks(company):
    prev, day = WEEKDAY - timedelta(days=1), WEEKDAY
    # a monitor-tier demo site only recommends: nothing claimed, no verified day
    mall = make_site(name="Mall (demo)", tier="monitor", demo_link="demo")
    _, sp, _ = twin_day(mall, prev, 0.9, seed=1)
    twin_day(mall, day, float(sp["soc"][-1]), seed=2)
    r = verify.compute_day(mall, day, with_bound=False)["native"]
    assert r["fidelity"] == "recommendation_only" and r["claimed_rs"] == 0.0
    assert verify.summary(mall, day.isoformat(), day.isoformat())["baselines"]["native"]["verified_days"] == 0
    # an advisory site (its inverter holds the battery for outages): a mark alone (its plan unreadable) stays pending and claims nothing
    adv = make_site(name="Advisory", tier="advisory_writes", native={"source": "entered", "work_mode": "backup", "backup_soc_pct": 90})
    _, sp, _ = twin_day(adv, prev, 0.9, seed=1)
    twin_day(adv, day, float(sp["soc"][-1]), seed=2)
    assert verify.compute_day(adv, day, with_bound=False)["native"]["fidelity"] == "not_applied"
    _mark(adv, "no-such-plan", day)
    r = verify.compute_day(adv, day, with_bound=False)["native"]
    assert r["fidelity"] == "pending" and r["claimed_rs"] == 0.0
    # the table keyed but the inverter kept its own settings: telemetry says not applied
    slots = _slots_plan(day)
    site = sites.full(adv)
    tr = twin.truth(site, day)
    followed = twin.simulate(tr, twin.slots_policy(slots, tr["ts"]), twin.plant_of(site), float(sp["soc"][-1]))
    pid = _insert_plan(adv, day, slots, followed)
    _mark(adv, pid, day)
    r = verify.compute_day(adv, day, with_bound=False)["native"]
    assert r["fidelity"] == "not_applied" and r["claimed_rs"] == 0.0
    with store.connect() as con:
        assert con.execute("SELECT confirmation FROM applied WHERE plan_id = ?", (pid,)).fetchone()[0] == "not_applied"
    # the table followed: confirmed from telemetry, and the day claims
    telemetry.delete_site(adv)
    twin_day(adv, prev, 0.9, seed=1)
    twin_day(adv, day, float(sp["soc"][-1]), seed=2, policy=lambda s, ts: twin.slots_policy(slots, ts))
    r = verify.compute_day(adv, day, with_bound=False)["native"]
    assert r["fidelity"] == "confirmed" and r["claimed_rs"] == r["saving_rs"] and r["fidelity_score"] > 0.8


# ------------------------------------------------------------------------------------------------ savings-rescore-uses-latest-capture

def test_a_past_day_is_rescored_with_the_capture_in_force_on_that_day(company):
    sid = make_site(name="Capture", native={"source": "entered", "work_mode": "self_use", "backup_soc_pct": 20})
    day = WEEKDAY
    twin_day(sid, day, 0.9, seed=2)  # the battery starts full, so the backup level shapes the native replay
    before = verify.compute_day(sid, day, with_bound=False)["native"]
    sites.capture_native(sid, {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 60}, "t")  # an installer change recorded today
    after = verify.compute_day(sid, day, with_bound=False)["native"]
    assert after["saving_rs"] == pytest.approx(before["saving_rs"], abs=0.01)  # before the fix: re-scored on the 60 percent capture
    assert abs(before["saving_rs"]) < 2.0
    assert after["native_capture_version"] == 1
    with store.connect() as con:
        con.execute("UPDATE native_settings SET captured_at = ? WHERE site_id = ? AND version = 1", ("2026-10-01T00:00:00+00:00", sid))
        con.execute("UPDATE native_settings SET captured_at = ? WHERE site_id = ? AND version = 2", ("2026-10-20T00:00:00+00:00", sid))
    assert verify.native_for_day(sid, date(2026, 10, 14))["version"] == 1
    assert verify.native_for_day(sid, date(2026, 10, 25))["version"] == 2
    assert verify.native_for_day(sid, date(2026, 9, 1))["version"] == 1
    site = sites.full(sid)
    assert verify._tuned_key({**site, "native": verify.native_for_day(sid, day)}, day) != verify._tuned_key(site, day)


# ------------------------------------------------------------------------------------------------ savings-native-replay-ignores-grid-charge-and-modes

def test_the_native_replay_honours_grid_charge_backup_and_feed_in_first():
    series = _series(outage=None, pv=0.0, load=50.0)
    plant = _plant(gen_kw=0.0)
    tou = {"native": {"body": {"work_mode": "self_use", "backup_soc_pct": 20.0, "slots": [
        {"start": "00:00", "mode": "self_use", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": True},
        {"start": "06:00", "mode": "self_use", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False}]}}}
    sim = twin.simulate(series, twin.native_policy(tou, series["ts"]), plant, 0.2)
    assert sim["soc"][359] > 0.9 and sim["imp"][:360].sum() / 60 > 50 * 6 + 100  # grid-charged overnight (before: discharged to the floor)
    backup = {"native": {"body": {"work_mode": "backup", "backup_soc_pct": 90.0, "grid_charge_allowed": True, "slots": []}}}
    series_o = _series(outage=(1200, 1260), pv=0.0, load=50.0)
    sim = twin.simulate(series_o, twin.native_policy(backup, series_o["ts"]), plant, 0.3)
    assert sim["soc"][1199] == pytest.approx(0.9, abs=0.01)  # charged to the backup level and held there (before: held at 30 percent)
    assert sim["bat"][1200:1260].min() > 0  # and discharged in the outage
    surplus = _series(outage=None, pv=100.0, load=20.0)
    feed = {"native": {"body": {"work_mode": "feed_in_first", "backup_soc_pct": 20.0, "slots": []}}}
    p = _plant(gen_kw=0.0, x_max=50.0)
    sim = twin.simulate(surplus, twin.native_policy(feed, surplus["ts"]), p, 0.2)
    assert sim["exp"][0] == pytest.approx(50.0) and sim["bat"][0] == pytest.approx(-30.0)
    sim = twin.simulate(surplus, twin.native_policy({"native": {"body": {"work_mode": "self_use", "backup_soc_pct": 20.0}}}, surplus["ts"]), p, 0.2)
    assert sim["exp"][0] == pytest.approx(0.0) and sim["bat"][0] == pytest.approx(-80.0)


# ------------------------------------------------------------------------------------------------ savings-switchback-lock-overwritten

def test_a_locked_switchback_calendar_cannot_be_repainted(company):
    sid = make_site(name="Switch")
    rows = verify.plan_switchback(sid, date(2026, 11, 9), 2, 7, "t", seed=1)
    with store.connect() as con:
        con.execute("UPDATE switchback SET locked = 1 WHERE site_id = ?", (sid,))
    for start in (date(2026, 11, 9), date(2026, 11, 12)):
        with pytest.raises(verify.CalendarLocked) as exc:
            verify.plan_switchback(sid, start, 2, 7, "t", seed=2)
        assert isinstance(exc.value, schemas.Refusal) and exc.value.status_code == 409 and isinstance(exc.value, ValueError)
    blocks = verify.switchback_blocks(sid)
    assert [(b["block_start"], b["arm"], b["locked"]) for b in blocks] == [(r["block_start"], r["arm"], 1) for r in rows]


# ------------------------------------------------------------------------------------------------ savings-fee-check-per-month

def test_the_fee_check_scales_part_months_and_takes_the_smaller_baseline(company):
    sid = make_site(name="Fee")
    store.set_setting(f"fee:{sid}", 5000.0)
    _put_rows(sid, {d.isoformat(): {"native": _row(1000.0, 1000.0), "tuned": _row(800.0, 800.0)} for d in _days(date(2026, 10, 20), date(2026, 11, 6))})
    fee = verify.summary(sid, "2026-11-01", "2026-11-06", see_fee=True)["fee"]
    assert fee["verified_saving_rs_per_month"] == pytest.approx(800.0 * 30)  # before: 6,000 (six days called a month)
    assert fee["within_one_third"] is True
    fee = verify.summary(sid, "2026-10-20", "2026-11-06", see_fee=True)["fee"]
    assert fee["verified_saving_rs_per_month"] == pytest.approx((800.0 * 31 + 800.0 * 30) / 2, abs=1)
    assert fee["within_one_third"] is True


# ------------------------------------------------------------------------------------------------ savings-genset-min-run-reset-per-tick

def test_the_live_demo_keeps_a_minimum_run_across_its_15_minute_ticks(company):
    sid = make_site(name="Ticks", control="manual", tier="advisory_writes", battery={"nameplate_kwh": 2000, "p_ch_kw": 100, "p_dis_kw": 100},
                    twin_extra={"windows": [["10:00", "12:30"]], "adherence": 1.0, "trips_per_day": 0.0})
    site = sites.full(sid)
    day = WEEKDAY
    t0 = twin.day_start_utc(day)
    grid = twin.truth(site, day)["grid"]
    start = int(np.argmax(grid < 0.5))
    assert 580 <= start <= 620
    q0 = t0 + ((start + 10) // 15 + 1) * 900
    plan = {"slots": {"days": []}, "steps": [{"t": q0, "genset_on": True}, {"t": q0 + 900, "genset_on": True}]}  # a planned 30-minute run
    a, b = t0 + 9 * 3600, t0 + 15 * 3600

    def run(chunks: bool, forget: bool = False):
        telemetry.delete_site(sid)
        with store.connect() as con:
            con.execute("DELETE FROM settings WHERE key = ?", (f"demo_twin:{sid}",))
        if not chunks:
            demo._simulate_span(sites.full(sid), a, b, plan)
        else:
            for t in range(a, b, 900):
                if forget:  # the carried state lost: rebuilt from the trailing telemetry
                    with store.connect() as con:
                        con.execute("DELETE FROM settings WHERE key = ?", (f"demo_twin:{sid}",))
                demo._simulate_span(sites.full(sid), t, t + 900, plan)
        return telemetry.raw(sid, a, b, ("genset_running",))["genset_running"]

    single = run(False)
    assert int(single.sum()) == 60  # a 30-minute planned run held to the 60-minute minimum
    assert np.array_equal(run(True), single)  # before: 15-minute calls kept it running until the grid returned
    assert np.array_equal(run(True, forget=True), single)


# ------------------------------------------------------------------------------------------------ savings-genset-surplus-booked-as-pv

def test_genset_output_nobody_takes_is_not_booked_as_pv_curtailment():
    series = _series(outage=(0, 120), pv=0.0, load=105.0, crit=105.0)
    plant = _plant(e_nom=100.0, p_ch=50.0, p_dis=50.0, gen_kw=640.0, gen_fmin=0.25, gen_control="auto_on_grid_loss", gen_min_run=30, load_mode=1)
    sim = twin.simulate(series, twin.slots_policy([], series["ts"]), plant, 0.95)
    assert sim["gen"][60] == pytest.approx(105.0) and sim["curtail"][60] == 0.0 and sim["pv"][60] == 0.0  # before: 160 kW, 55 kW "curtailed PV"
    assert sim["served"][60] == pytest.approx(sim["gen"][60] + sim["pv"][60] + sim["bat"][60])


# ------------------------------------------------------------------------------------------------ savings-perfect-foresight-drains-battery

def test_the_perfect_foresight_bound_prices_its_end_energy_and_ignores_todays_overrides(company, monkeypatch):
    sid = make_site(name="Bound", genset=False)
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    sites.add_override(sid, {"kind": "reserve", "value": 80.0, "until": until}, "t")
    sites.add_override(sid, {"kind": "charge_block", "until": until}, "t")
    seen = {}

    def fake_solve(p, time_limit=10.0, **kw):
        seen["p"] = p
        T = int(p["T"])
        return {"status": "optimal", "time_limited": False, "M": [],
                "breakdown": {"energy_rs": 1000.0, "genset_rs": 0.0, "wear_rs": 50.0, "unserved_rs": 0.0, "mdi_rise_rs": 0.0},
                "sol": {"G": np.full(T, 1000.0 / (0.25 * 40.0 * T)), "X": np.zeros(T), "E": np.array([107.5, 21.5])}}

    monkeypatch.setattr(verify.optimise, "solve", fake_solve)
    s = _series(outage=None)
    # round 7 (audit engine-optimiser-14): the bound is the day's saving plus the hindsight plan's headroom over the actual arm, each priced on the
    # solve's terms at the ledger's prices; here the actual arm cost 5,000 and ended where native did (107.5 kWh), the saving 0
    pr = {"c_imp": np.full(1440, 40.0), "c_exp": np.zeros(1440), "peak": np.zeros(1440, dtype=bool)}
    out = verify._bound(sites.full(sid), WEEKDAY, s, pr, 0.5, {"cost_rs": 5000.0, "unserved_kwh": 0.0, "peak_kw": 0.0}, 107.5, 0.0, c_next=40.0,
                        v_end=30.0)
    assert out["bound_rs"] == pytest.approx(5000.0 - (1050.0 - (21.5 - 107.5) * 30.0))  # before: 3,950, the drained 86 kWh for free
    p = seen["p"]
    assert p["terminal"]["c_next"] == 40.0 and p["terminal"]["H_peak"] >= 24
    assert not p["override_mask"].any() and "reserve_manual" not in p and not p["battery"]["charge_block"].any()


# ------------------------------------------------------------------------------------------------ savings-switchback-no-carry-over

def test_the_switchback_credits_the_plan_block_for_the_energy_it_hands_over(company):
    sid = make_site(name="Carry")
    d0 = date(2026, 9, 1)
    with store.connect() as con:
        for a, arm in ((0, "plan"), (7, "native"), (14, "plan"), (21, "native")):
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,1)",
                        (sid, (d0 + timedelta(days=a)).isoformat(), (d0 + timedelta(days=a + 6)).isoformat(), arm))
        for i in range(28):
            d = (d0 + timedelta(days=i)).isoformat()
            con.execute("INSERT INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                        (sid, d, json.dumps({"pv_kwh": 1000.0 + 10 * (i % 5), "load_kwh": 5000.0 + 7 * (i % 3), "outage_minutes": 30 * (i % 4),
                                             "unserved_kwh": 0.0})))
    rows = {}
    for i in range(28):
        d = d0 + timedelta(days=i)
        plan_block = (i // 7) % 2 == 0
        last_of_plan = plan_block and i % 7 == 6
        rows[d.isoformat()] = {"native": _row(cost=9000.0 if plan_block else 10000.0, e_end_a=200.0 if last_of_plan else 100.0, e_end_b=50.0 if last_of_plan else 100.0,
                                              v_end=30.0)}
    _put_rows(sid, rows)
    res = verify.switchback_result(sid)
    credits = [c for c in res["carry_over_credits"] if c["from_arm"] == "plan"]
    assert [c["boundary"] for c in credits] == ["2026-09-08", "2026-09-22"] and all(c["credit_rs"] == pytest.approx(4500.0) for c in credits)
    # round 7 (audit engine-savings-11): the native block's hand-over to the plan block on 15 Sep is credited too, against the plan days' mean end
    # energy ((12 x 100 + 2 x 200) / 14 kWh): the native block ended 14 kWh below it, so the plan block started short
    back = (1600.0 / 14 - 100.0) * 30.0
    r = res["result"]
    assert r["carry_over_credit_rs"] == pytest.approx(9000.0 + back, abs=0.01)
    assert r["saving_rs_per_day_unadjusted"] == pytest.approx(1000.0 + (9000.0 + back) / 14, abs=0.01)
    assert r["saving_rs_per_day_unadjusted_before_carry_over"] == pytest.approx(1000.0, abs=0.01)


# ------------------------------------------------------------------------------------------------ savings-preliminary-flag-rule

def test_preliminary_holds_until_eight_switchback_weeks_and_the_fca_is_billed(company):
    sid = make_site(name="Final")
    _put_rows(sid, {d.isoformat(): {"native": _row(100.0, 100.0), "tuned": _row(100.0, 100.0)} for d in _days(date(2026, 10, 1), date(2026, 10, 30))})
    s = verify.summary(sid, "2026-10-01", "2026-10-31")
    assert s["baselines"]["native"]["verified_days"] >= 28
    assert s["preliminary"] is True and s["final_conditions"]["switchback_weeks"] == 0  # before: final at 28 verified days
    with store.connect() as con:
        for k in range(8):
            a = date(2026, 8, 1) + timedelta(days=7 * k)
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,1)",
                        (sid, a.isoformat(), (a + timedelta(days=6)).isoformat(), "plan" if k % 2 else "native"))
    s = verify.summary(sid, "2026-10-01", "2026-10-31")
    assert s["final_conditions"]["fca_billed"] is True and s["preliminary"] is False
    _put_rows(sid, {d.isoformat(): {"native": _row(100.0, 100.0), "tuned": _row(100.0, 100.0)} for d in _days(date(2026, 11, 1), date(2026, 11, 28))})
    s = verify.summary(sid, "2026-11-01", "2026-11-30")  # November's FCA is still pending: not yet billed
    assert s["final_conditions"]["fca_billed"] is False and s["preliminary"] is True
