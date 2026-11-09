"""Round-2 regression tests for the savings group (verify.py, twin.py, demo.py, tariffs.py): the items the round-1 verifiers left open, the
regressions they found, and the work routed here. Each test fails on the code before its fix."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from test_fix_savings import _blank, _insert_plan, _mark, _plant, _put_rows, _row, _series, company, make_site, twin_day  # noqa: F401 (company is a fixture)

from gridlytics import demo, optimise, plans, sites, store, tariffs, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))
D = date(2026, 10, 14)  # a Wednesday
P = D - timedelta(days=1)
SRC = {"kind": "manual", "ref": "test", "verified": "primary"}
OFF, PEAK_NOV = 3 * 60 + 5, 19 * 60 + 5


def _slot(t0, a, b, mode, floor=20.0, target=100.0, gc=False):
    return {"start_ts": t0 + int(a * 3600), "end_ts": t0 + int(b * 3600), "mode": mode, "soc_floor_pct": floor, "soc_target_pct": target,
            "grid_charge": gc, "max_discharge_w": None, "max_charge_w": None, "genset": None}


def _confirmation(pid):
    with store.connect() as con:
        r = con.execute("SELECT confirmation, fidelity, data FROM applied WHERE plan_id = ?", (pid,)).fetchone()
    return r["confirmation"], r["fidelity"], json.loads(r["data"]).get("confirmation_detail") or {}


def _blank_where(rows, where, *cols):
    """Rows with the given columns blank on the row indices where `where(i)` holds."""
    idx = {telemetry.COLUMNS.index(c) for c in cols}
    return [tuple(None if (k in idx and where(i)) else v for k, v in enumerate(r)) for i, r in enumerate(rows)]


def _flat_rows(sid, d0: date, days: int, kw, step_min: int = 1, export_kw=None):
    """Grid-only rows: import kw (a number, or a function of the local day) every step_min minutes, and optional export (kW by local minute)."""
    t0 = twin.day_start_utc(d0)
    rows = []
    for t in range(t0, t0 + days * 86400, 60 * step_min):
        local = datetime.fromtimestamp(t, PKT)
        k = kw(local.date()) if callable(kw) else kw
        x = export_kw(local.hour * 60 + local.minute) if export_kw else 0.0
        rows.append((sid, t, "csv", 1.0, k * 1000 / 60, x * 1000 / 60))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "grid_export_wh"))


# ------------------------------------------------------------------------------------------------ savings-fidelity-gate-bypassed

def test_a_keyed_table_is_confirmed_only_on_evidence(company):
    """R6 4.6 at an advisory site: a table the inverter never ran is not applied; no battery power or no scorable test leaves the day pending; the
    charge slot's target-reached test confirms a followed table and catches a wrong target."""
    sid = make_site(name="Adv fid", tier="advisory_writes", control="none", genset=False)
    _, sp, rows_p = twin_day(sid, P, 0.9, seed=1)
    tr, _, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2)  # the inverter keeps its native self-use all day
    site = sites.full(sid)
    t0 = twin.day_start_utc(D)
    slots = [_slot(t0, 0, 6, "charge_grid", target=90.0, gc=True), _slot(t0, 6, 24, "self_use")]
    followed = twin.simulate(tr, twin.slots_policy(slots, tr["ts"]), twin.plant_of(site), float(sp["soc"][-1]))
    pid = _insert_plan(sid, D, slots, followed)
    _mark(sid, pid, D)
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["fidelity"] == "not_applied" and r["claimed_rs"] == 0.0  # before: confirmed on 0 of 0 boundaries
    cls, _, detail = _confirmation(pid)
    assert cls == "not_applied" and detail["targets_scored"] == 1 and detail["targets_hit"] == 0
    # a source with no battery power: nothing tells a followed table from an ignored one
    telemetry.delete_site(sid)
    telemetry.write_rows(_blank(rows_p + rows, "battery_power_w"))
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["fidelity"] == "pending" and r["claimed_rs"] == 0.0  # before: confirmed, and the replay error claimed
    assert _confirmation(pid)[2]["reason"] == "no_battery_power"
    # the table followed: its charge slot reaches the target, which confirms the day
    telemetry.delete_site(sid)
    twin_day(sid, P, 0.9, seed=1)
    twin_day(sid, D, float(sp["soc"][-1]), seed=2, policy=lambda s, ts: twin.slots_policy(slots, ts))
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["fidelity"] == "confirmed" and r["claimed_rs"] == r["saving_rs"]
    assert _confirmation(pid)[2]["targets_hit"] == 1
    # the installer keyed 60 percent for the 90 percent target: the target test misses and the day is not confirmed
    wrong = [_slot(t0, 0, 6, "charge_grid", target=60.0, gc=True), _slot(t0, 6, 24, "self_use")]
    telemetry.delete_site(sid)
    twin_day(sid, P, 0.9, seed=1)
    twin_day(sid, D, float(sp["soc"][-1]), seed=2, policy=lambda s, ts: twin.slots_policy(wrong, ts))
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    cls, score, detail = _confirmation(pid)
    assert r["fidelity"] != "confirmed" and cls == r["fidelity"]  # before: confirmed (no boundary scorable, so a hit rate of 1)
    assert detail["targets_scored"] == 1 and detail["targets_hit"] == 0 and score < 0.8


def test_a_table_that_runs_the_battery_as_the_native_settings_do_stays_pending(company):
    sid = make_site(name="Adv same", tier="advisory_writes", control="none", genset=False)
    _, sp, _ = twin_day(sid, P, 0.9, seed=1)
    tr, _, _ = twin_day(sid, D, float(sp["soc"][-1]), seed=2)
    t0 = twin.day_start_utc(D)
    for slots in ([_slot(t0, 0, 24, "self_use")],
                  # the discharge slot opens on an empty battery: its boundaries show no change on this day, in the table's own replay either
                  [_slot(t0, 0, 18, "self_use"), _slot(t0, 18, 22, "discharge"), _slot(t0, 22, 24, "self_use")]):
        followed = twin.simulate(tr, twin.slots_policy(slots, tr["ts"]), twin.plant_of(sites.full(sid)), float(sp["soc"][-1]))
        pid = _insert_plan(sid, D, slots, followed)
        _mark(sid, pid, D)
        r = verify.compute_day(sid, D, with_bound=False)["native"]
        assert r["fidelity"] == "pending" and r["claimed_rs"] == 0.0  # before: confirmed (0 of 0) or partial on a boundary nothing could show
        assert _confirmation(pid)[2]["reason"] == "no_evidence"


# ------------------------------------------------------------------------------------------------ new-backup-replay-caps-pv-charging

def test_backup_mode_lets_surplus_pv_fill_the_battery_and_grid_charges_only_to_the_backup_level():
    plant = _plant(gen_kw=0.0, x_max=0.0)
    sunny = _series(outage=None, pv=150.0, load=50.0)
    for allowed in (False, True):
        backup = {"native": {"body": {"work_mode": "backup", "backup_soc_pct": 30.0, "grid_charge_allowed": allowed, "slots": []}}}
        sim = twin.simulate(sunny, twin.native_policy(backup, sunny["ts"]), plant, 0.3)
        assert sim["soc"].max() == pytest.approx(0.95, abs=1e-6)  # before: held at the 30 percent backup level all day
        assert sim["curtail"][:60].sum() == 0.0 and sim["bat"][0] == pytest.approx(-100.0)
        assert sim["imp"].sum() == 0.0 and sim["bat"].max() <= 0.0  # held (no discharge) with the grid present
    dark = _series(outage=(1200, 1260), pv=0.0, load=50.0)
    backup = {"native": {"body": {"work_mode": "backup", "backup_soc_pct": 60.0, "grid_charge_allowed": True, "slots": []}}}
    sim = twin.simulate(dark, twin.native_policy(backup, dark["ts"]), plant, 0.3)
    assert sim["soc"][1199] == pytest.approx(0.6, abs=0.005)  # the grid charges to the backup level, not to the PV ceiling
    assert sim["bat"][1200:1260].min() > 0


def test_a_backup_site_enters_an_evening_outage_with_the_pv_it_stored():
    ts = twin.minutes_of(D)
    hours = ((ts + 5 * 3600) % 86400) / 3600
    pv = np.clip(np.sin((hours - 6) / 12 * np.pi), 0, None) * 200.0
    grid = np.ones(len(ts))
    grid[18 * 60:21 * 60] = 0.0
    series = {"ts": ts, "pv": pv, "load": np.full(len(ts), 40.0), "crit": np.full(len(ts), 40.0), "grid": grid}
    plant = _plant(gen_control="manual")
    backup = {"native": {"body": {"work_mode": "backup", "backup_soc_pct": 30.0, "slots": []}}}
    sim = twin.simulate(series, twin.native_policy(backup, ts), plant, 0.3)
    assert sim["soc"][18 * 60 - 1] > 0.9  # before: 0.30 at the outage's start
    assert sim["fuel"].sum() < 1.0  # the stored PV carries the outage; before, the genset was booked for most of it


# ------------------------------------------------------------------------------------------------ new-soc-start-stale-evening-reading

TABLE = [{"start": "00:00", "mode": "charge_grid", "soc_floor_pct": 20, "soc_target_pct": 90, "grid_charge": True},
         {"start": "06:00", "mode": "self_use", "soc_floor_pct": 20, "soc_target_pct": 100, "grid_charge": False},
         {"start": "22:00", "mode": "charge_grid", "soc_floor_pct": 20, "soc_target_pct": 90, "grid_charge": True}]


def test_the_days_start_carries_a_stale_evening_reading_forward_with_the_flows(company):
    sid = make_site(name="Midnight", tier="closed_loop", native={"source": "entered", "work_mode": "self_use", "backup_soc_pct": 20, "slots": TABLE})
    pol = lambda s, ts: twin.table_policy(TABLE, ts)  # noqa: E731
    _, sp, rows_p = twin_day(sid, P, 0.5, seed=1, policy=pol)
    _, sim, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2, policy=pol)
    assert -np.sum(sp["bat"][1390:]) / 60 > 20  # still charging after the last evening reading
    truth = float(sp["soc"][-1])
    telemetry.delete_site(sid)
    # the evening's readings stop at 23:10 while the flows go on, and the day's first reading comes at 01:00
    telemetry.write_rows(_blank_where(rows_p, lambda i: i >= 1390, "battery_soc_est_pct", "battery_soc_bms_pct")
                         + _blank_where(rows, lambda i: i < 60, "battery_soc_est_pct", "battery_soc_bms_pct"))
    site = sites.full(sid)
    _, act = verify._replay_inputs(site, D)
    soc0 = verify._soc_start(sid, twin.day_start_utc(D), act["g"], act["f"], twin.plant_of(site))
    assert soc0 == pytest.approx(truth, abs=0.01)  # before: the 23:10 reading, about 20 points low
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert abs(r["saving_rs"]) < 5.0  # the native replay of the same table from the right start: no saving to find


# ------------------------------------------------------------------------------------------------ verify: a day whose import is unknown

def test_a_day_with_no_grid_columns_claims_nothing(company):
    sid = make_site(name="No grid cols", tier="closed_loop")
    _, sp, rows_p = twin_day(sid, P, 0.9, seed=1)
    _, _, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]
    assert metered["import_unknown"] is False and metered["claimed_rs"] == metered["saving_rs"]
    telemetry.delete_site(sid)
    telemetry.write_rows(_blank(rows_p + rows, "grid_import_wh", "grid_export_wh", "grid_power_w"))
    r = verify.compute_day(sid, D, with_bound=False)
    n = r["native"]
    assert n["import_unknown"] is True and n["claimed_rs"] == 0.0 and r["tuned"]["claimed_rs"] == 0.0  # before: the native arm's whole energy bill
    assert n["levers"]["peak_window"] is None and n["levers"]["offpeak_energy"] is None and n["levers"]["export"] is None
    assert n["saving_rs"] == pytest.approx(sum(v for v in n["levers"].values() if v is not None), abs=0.02)
    assert abs(n["saving_rs"]) < 0.2 * n["baseline"]["cost_rs"]
    s = verify.summary(sid, D.isoformat(), D.isoformat())["baselines"]["native"]
    assert s["claimed_rs"] == 0.0 and s["verified_days"] == 0 and s["import_unknown_days"] == 1
    assert s["levers"]["mdi"] == 0.0  # an unmetered day's zero peak is no MDI saving


def test_the_switchback_result_leaves_out_a_day_whose_import_is_unknown(company):
    sid = make_site(name="Switch unmetered")
    d0 = date(2026, 9, 1)
    with store.connect() as con:
        for a, arm in ((0, "plan"), (7, "native"), (14, "plan"), (21, "native")):
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,1)",
                        (sid, (d0 + timedelta(days=a)).isoformat(), (d0 + timedelta(days=a + 6)).isoformat(), arm))
        for i in range(28):
            con.execute("INSERT INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                        (sid, (d0 + timedelta(days=i)).isoformat(),
                         json.dumps({"pv_kwh": 1000.0 + 10 * (i % 5), "load_kwh": 5000.0 + 7 * (i % 3), "outage_minutes": 30 * (i % 4), "unserved_kwh": 0.0})))
    rows = {}
    for i in range(28):
        plan_block = (i // 7) % 2 == 0
        unmetered = plan_block and i % 7 in (2, 3, 4)  # blank grid columns: the day's actual grid cost reads zero
        rows[(d0 + timedelta(days=i)).isoformat()] = {"native": {**_row(cost=0.0 if unmetered else (9000.0 if plan_block else 10000.0)),
                                                                 "import_unknown": unmetered}}
    _put_rows(sid, rows)
    r = verify.switchback_result(sid)
    assert r["days"] == 22 and r["result"]["plan_days"] == 8  # before: 28 days, the six unmetered ones booked as free
    assert r["result"]["saving_rs_per_day_unadjusted"] == pytest.approx(1000.0, abs=0.01)


# ------------------------------------------------------------------------------------------------ round 3: routed-import-completeness above 0.5

GRID_COLS = ("grid_import_wh", "grid_export_wh", "grid_power_w")
NIGHT_CHARGER = [{"start": "00:00", "mode": "charge_grid", "soc_floor_pct": 20.0, "soc_target_pct": 90.0, "grid_charge": True},
                 {"start": "06:00", "mode": "self_use", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False},
                 {"start": "17:00", "mode": "discharge", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False},
                 {"start": "22:00", "mode": "self_use", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False}]


def _rescore(sid, rows_p, rows, frac, cols):
    """The day re-scored with the given columns blank over its first `frac` of minutes."""
    n_blank = int(1440 * frac)
    telemetry.delete_site(sid)
    telemetry.write_rows(rows_p + _blank_where(rows, lambda i: i < n_blank, *cols))
    return verify.compute_day(sid, D, with_bound=False)["native"]


def test_a_day_whose_grid_meter_drops_out_for_part_of_it_claims_what_the_metered_day_does(company):
    # the verifier's case: a closed-loop site running the native table (the true saving is nil) whose grid meter's columns drop out while the
    # load and PV columns continue
    sid = make_site(name="Partly metered", tier="closed_loop")
    _, sp, rows_p = twin_day(sid, P, 0.9, seed=1)
    _, _, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]
    assert abs(metered["claimed_rs"]) < 1.0
    for frac in (0.3, 0.45):
        r = _rescore(sid, rows_p, rows, frac, GRID_COLS)
        assert r["import_completeness"] == pytest.approx(1 - frac, abs=0.01) and r["import_unknown"] is False
        assert abs(r["claimed_rs"]) < 1.0  # before: Rs 23,552 at 30% and Rs 41,665 at 45%, the baseline's bill over the blank minutes
        assert r["import_rebuilt_share"] == pytest.approx(frac, abs=0.01) and r["import_unresolved_share"] == 0.0
        assert r["actual"]["import_kwh"] == pytest.approx(metered["actual"]["import_kwh"], abs=0.5)
        assert r["actual"]["cost_rs"] == pytest.approx(metered["actual"]["cost_rs"], abs=20.0)
    r = _rescore(sid, rows_p, rows, 0.55, GRID_COLS)
    assert r["import_unknown"] is True and r["claimed_rs"] == 0.0 and r["claim_withheld"] == "import_unknown"  # the round-2 rule stands


def test_a_blank_grid_meter_still_prices_the_grid_charging_done_under_it(company):
    # an optimised table that charges from the grid at night: with the grid columns blank over the charging, reading them as zero priced the charge
    # at nothing; the balance rebuilds it from the battery's power, else from its state of charge, else from the readings either side of the gap
    sid = make_site(name="Night charger", tier="closed_loop")
    pol = lambda s, ts: twin.table_policy(NIGHT_CHARGER, ts)  # noqa: E731
    _, sp, rows_p = twin_day(sid, P, 0.5, seed=1, policy=pol)
    _, _, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2, policy=pol)
    metered = verify.compute_day(sid, D, with_bound=False)["native"]["claimed_rs"]
    soc = ("battery_soc_est_pct", "battery_soc_bms_pct")
    for frac, cols in ((0.3, GRID_COLS), (0.1, GRID_COLS + ("battery_power_w",)), (0.3, GRID_COLS + ("battery_power_w",))):
        r = _rescore(sid, rows_p, rows, frac, cols)
        assert r["import_unknown"] is False and r["import_unresolved_share"] == 0.0, cols
        assert r["claimed_rs"] == pytest.approx(metered, abs=max(30.0, 0.02 * abs(metered))), (frac, cols)
    # round 3: with the state of charge blank too, the states either side are 144 minutes apart, too far to spread the flow over (verify.SOC_SPAN_MAX_MIN):
    # the minutes stay unresolved and the day is withheld rather than claimed on an even smear
    r = _rescore(sid, rows_p, rows, 0.1, GRID_COLS + ("battery_power_w",) + soc)
    assert r["import_unknown"] is True and r["claimed_rs"] == 0.0 and r["import_unresolved_share"] == pytest.approx(0.1, abs=0.005)


def test_minutes_whose_grid_flow_cannot_be_rebuilt_price_neither_arm_and_too_many_withhold_the_day(company):
    # grid and genset columns blank (the genset's state unknown, so the balance cannot be closed): before, the actual arm read zero import there while
    # the baseline imported the load, a claim of the baseline's bill over those minutes on a site whose true saving is nil
    sid = make_site(name="Unresolved", tier="closed_loop")
    _, sp, rows_p = twin_day(sid, P, 0.9, seed=1)
    _, _, rows = twin_day(sid, D, float(sp["soc"][-1]), seed=2)
    cols = GRID_COLS + ("genset_running", "genset_power_w", "genset_fuel_l")
    r = _rescore(sid, rows_p, rows, 0.03, cols)
    assert abs(r["claimed_rs"]) < 1.0  # both arms priced without the masked minutes
    assert r["import_unresolved_share"] == pytest.approx(0.03, abs=0.005) and r["import_unknown"] is False
    r = _rescore(sid, rows_p, rows, 0.1, cols)
    assert r["import_unknown"] is True and r["claimed_rs"] == 0.0 and r["levers"]["peak_window"] is None
    assert r["import_unresolved_share"] > verify.GRID_UNRESOLVED_MAX


def test_the_battery_flow_over_a_gap_comes_from_the_state_of_charge_around_it():
    p = twin.Plant(e_nom=100.0, p_ch=50.0, p_dis=50.0, eta_c=0.9, eta_d=0.8)
    # minute 0 read at 50%; minutes 1-8 have no battery power, with a reading of 52% at minute 4; minute 9 reads 50% after charging at 6 kW
    soc = np.array([0.5, np.nan, np.nan, np.nan, 0.52, np.nan, np.nan, np.nan, np.nan, 0.50])
    g = {"soc": soc, "soc_bms": np.full(10, np.nan), "bat": np.array([np.nan] * 9 + [-6.0])}
    need = np.array([False] + [True] * 8 + [False])
    out = verify._bat_from_soc(g, p, need)
    h = 4 / 60
    assert np.allclose(out[1:5], -2.0 / 0.9 / h)  # 2 kWh stored over four minutes, drawn from the bus at the charge efficiency
    e_end = 50.0 - 6.0 * 0.9 / 60  # the state before minute 9: its reading with that minute's charge backed out
    assert np.allclose(out[5:9], (52.0 - e_end) * 0.8 / h)
    assert np.isnan(out[0]) and np.isnan(out[9])
    fast = verify._bat_from_soc(g, twin.Plant(e_nom=100.0, p_ch=10.0, p_dis=10.0, eta_c=0.9, eta_d=0.8), need)
    assert np.allclose(fast[1:9], [-10.0] * 4 + [10.0] * 4)  # within the battery's power
    # a gap at the day's start takes the state at 00:00; a gap whose next reading follows an unread flow is left unresolved, not guessed
    g2 = {"soc": np.array([np.nan, np.nan, 0.51]), "soc_bms": np.full(3, np.nan), "bat": np.full(3, np.nan)}
    assert np.allclose(verify._bat_from_soc(g2, p, np.array([True, True, True]), soc0=0.5)[:3], -1.0 / 0.9 / (3 / 60))
    assert np.isnan(verify._bat_from_soc(g2, p, np.array([True, True, False]), soc0=0.5)).all()


# ------------------------------------------------------------------------------------------------ round 3: regression-fee-rate-counts-withheld-days

def test_the_fee_check_rates_the_month_over_the_days_that_could_claim(company):
    sid = make_site(name="Fee unknown")
    rows = {}
    for i in range(30):
        unknown = i >= 10
        r = _row(saving=1000.0, claimed=0.0 if unknown else 1000.0)
        r["import_unknown"] = unknown
        rows[(date(2026, 9, 1) + timedelta(days=i)).isoformat()] = {"native": r, "tuned": dict(r)}
    _put_rows(sid, rows)
    store.set_setting(f"fee:{sid}", 5000.0)
    s = verify.summary(sid, "2026-09-01", "2026-09-30", see_fee=True)
    assert s["baselines"]["native"]["claimed_rs"] == 10000.0 and s["baselines"]["native"]["verified_days"] == 10
    assert s["fee"]["verified_saving_rs_per_month"] == 30000.0 and s["fee"]["within_one_third"] is True  # before: 10,000 and False
    # a month of unknown days alone has no rate: it is left out, not averaged in as nil
    rows = {(date(2026, 10, 1) + timedelta(days=i)).isoformat(): {"native": {**_row(saving=1000.0), "import_unknown": True},
                                                                   "tuned": {**_row(saving=1000.0), "import_unknown": True}} for i in range(5)}
    _put_rows(sid, rows)
    s = verify.summary(sid, "2026-09-01", "2026-10-31", see_fee=True)
    assert s["fee"]["verified_saving_rs_per_month"] == 30000.0


# ------------------------------------------------------------------------------------------------ tariffs-legacy-export-price (verify side)

LEGACY = {"tariff_category": "A-2(c)", "sanctioned_load_kw": 650, "regime": "net_metering_legacy", "export_cap_kw": 400, "agreement_start": "2019-07-01"}


def test_the_ledger_prices_a_legacy_surplus_period_at_the_surplus_rate(company):
    sid = make_site(name="Legacy ledger", conn=LEGACY)
    conn = sites.connections(sid)[0]
    _flat_rows(sid, date(2026, 11, 1), 3, 100.0, export_kw=lambda m: 400.0 if 9 * 60 <= m < 15 * 60 else 0.0)  # off-peak exports beat imports
    day = date(2026, 11, 2)
    pr = verify._minute_prices(conn, day, sid)
    assert pr["c_imp"][OFF] == pytest.approx(35.15)  # before: priced as a net import (about 39.5), the exports never passed
    assert pr["c_exp"][OFF] == pytest.approx(35.15)  # the bill's own price: the optimiser's tie-break is not booked in the ledger
    tot = verify.month_totals(sid, conn, day)
    assert tot["export"]["off_peak"] > tot["import"]["off_peak"] and tot["export"]["peak"] == 0.0
    assert verify.month_imports(sid, conn, day) == tot["import"]
    full = tariffs.vectors(conn, datetime.fromtimestamp(twin.day_start_utc(day), timezone.utc), 96, tot["import"], None, 0.0)["c_imp"]
    assert pr["c_imp"][PEAK_NOV] == pytest.approx(full[PEAK_NOV // 15])  # the peak period still nets to an import
    assert pr["c_exp"][PEAK_NOV] == pytest.approx(pr["c_imp"][PEAK_NOV])  # and an export there is worth the import it nets


# ------------------------------------------------------------------------------------------------ tariffs-vectors-billing-month (verify side)

def test_month_imports_settle_over_the_reading_interval(company):
    sid = make_site(name="Reading day")
    _flat_rows(sid, date(2026, 8, 10), 42, lambda d: 100.0 if d.month == 8 else 200.0, step_min=15)
    conn = sites.connections(sid)[0]
    after = twin.day_start_utc(date(2026, 10, 1))
    calendar_month = sum(verify.month_imports(sid, conn, date(2026, 9, 5), now=after).values())
    assert calendar_month == pytest.approx(30 * 24 * 200.0, rel=1e-6)
    conn = {**conn, "data": {**conn["data"], "reading_day": 15}}
    by_reading = sum(verify.month_imports(sid, conn, date(2026, 9, 5), now=after).values())
    assert by_reading == pytest.approx(16 * 24 * 100.0 + 15 * 24 * 200.0, rel=1e-6)  # 16 Aug to 15 Sep; before: the calendar month
    assert sum(verify.month_imports(sid, conn, date(2026, 9, 20), now=after).values()) == pytest.approx(30 * 24 * 200.0, rel=1e-6)


# ------------------------------------------------------------------------------------------------ tariffs-reading-date-off-by-one (verify side)

def test_a_bill_entered_with_its_printed_reading_dates_bills_the_reading_interval(company):
    sid = make_site(name="Bill dates")
    _flat_rows(sid, date(2026, 10, 20), 31, 100.0)  # 20 Oct to 19 Nov
    by_reading = verify.add_bill(sid, {"previous_reading_date": "2026-10-20", "period_end": "2026-11-19", "total_rs": 2_000_000}, "t")
    assert by_reading["rebuilt"]["days"] == 30 and by_reading["rebuilt"]["first_day"] == "2026-10-21"  # before: refused (no period_start)
    assert sum(by_reading["rebuilt"]["import_kwh"].values()) == pytest.approx(72_000.0)
    assert by_reading["real"]["previous_reading_date"] == "2026-10-20"
    flagged = verify.add_bill(sid, {"period_start": "2026-10-20", "period_start_is_reading": True, "period_end": "2026-11-19", "total_rs": 2_000_000}, "t")
    first_day = verify.add_bill(sid, {"period_start": "2026-10-21", "period_end": "2026-11-19", "total_rs": 2_000_000}, "t")  # the old call shape
    assert flagged["rebuilt"]["total_rs"] == first_day["rebuilt"]["total_rs"] == by_reading["rebuilt"]["total_rs"]
    assert {b["period_start"] for b in verify.bills(sid)} == {"2026-10-21"}
    assert verify.rebuild_bill(sid, sites.connections(sid)[0], date(2026, 10, 20), date(2026, 11, 19))["days"] == 31  # positional calls unchanged
    with pytest.raises(ValueError):
        verify.add_bill(sid, {"previous_reading_date": "2026-11-19", "period_end": "2026-11-19", "total_rs": 1}, "t")


# ------------------------------------------------------------------------------------------------ tariffs-legacy-degenerate-lp

def test_a_legacy_site_never_plans_import_and_export_in_the_same_step(company):
    sid = make_site(name="Legacy mall", control="auto_on_grid_loss", tier="monitor", load_mode=1, crit=180.0, load_peak_kw=420,
                    conn={**LEGACY, "export_cap_kw": 180}, battery={"nameplate_kwh": 100, "p_ch_kw": 50, "p_dis_kw": 50},
                    genset={"rated_kw": 640, "f_min": 0.25, "fuel_kind": "hsd", "min_up_h": 0.5})
    site = sites.full(sid)
    t0 = twin.day_start_utc(date(2026, 11, 3))
    T = 192
    ts = np.arange(t0, t0 + T * 900, 900)
    hours = ((ts + 5 * 3600) % 86400) / 3600
    pv = np.clip(np.sin((hours - 6) / 12 * np.pi), 0, None) * 180.0
    load = twin.load_template(site, ts)
    bundle = {"pv": {"q": {"q50": pv, "q25": pv * 0.9}, "stage": 1},
              "load": {"q": {"q50": load, "q75": load * 1.05}, "crit": np.minimum(load, 180.0), "stage": 1},
              "outage": {"g": np.ones(T), "p_start": np.zeros(T), "dur_steps": np.full(T, 4), "p_down": np.zeros(T)}}
    site["state"] = {**site.get("state", {}), "soc": 0.5}
    p = plans.build_problem(site, t0, bundle, T)
    res = optimise.solve(p, time_limit=10.0)
    G, X = np.asarray(res["sol"]["G"]), np.asarray(res["sol"]["X"])
    assert int(np.sum((G > 0.1) & (X > 0.1))) == 0  # before: 33 steps of circulating import and export on this problem
    assert np.allclose(np.asarray(p["c_imp"]) - np.asarray(p["c_exp"]), tariffs.LEGACY_EXPORT_TIE_BREAK)  # before: equal prices
    # a surplus period keeps the tie-break too
    conn = sites.connections(sid)[0]
    vec = tariffs.vectors(conn, datetime(2026, 11, 2, 0, 0, tzinfo=PKT).astimezone(timezone.utc), 96, {"peak": 400.0, "off_peak": 2000.0}, None, 1e9,
                          {"peak": 0.0, "off_peak": 2400.0}, None)
    s = OFF // 15
    assert vec["c_imp"][s] == pytest.approx(35.15) and vec["c_exp"][s] == pytest.approx(35.15 - tariffs.LEGACY_EXPORT_TIE_BREAK)


# ------------------------------------------------------------------------------------------------ tariffs-adder-early-start-drops-previous

def test_an_adder_approved_ahead_of_its_months_keeps_the_previous_one_on_its_listed_bills(company):
    v = tariffs.propose("adder", "qta", "2026-10-20", {"name": "QTA Q3 CY2026", "rate": 0.80, "billing_months": ["2026-12", "2027-01", "2027-02"],
                                                       "exempt": ["icp_units"]}, SRC, "t")
    tariffs.approve(v["id"], "t")
    rates = lambda bm: [a["rate"] for a in tariffs.adders_for(bm) if a["key"] == "qta"]  # noqa: E731
    assert rates("2026-11") == [0.5194]  # before: QTA Q2 closed on 19 Oct and dropped from November's bill
    assert rates("2026-10") == [0.5194] and rates("2026-12") == [0.80] and rates("2027-02") == [0.80]
    assert sorted(a["key"] for a in tariffs.adders_for("2026-11")) == ["dss", "qta"]
    # an "all" adder still ends where the next version starts
    w = tariffs.propose("adder", "dss", "2027-01-01", {"name": "DSS 2027", "rate": 3.82, "billing_months": "all"}, SRC, "t")
    tariffs.approve(w["id"], "t")
    assert [a["rate"] for a in tariffs.adders_for("2026-12") if a["key"] == "dss"] == [3.23]
    assert [a["rate"] for a in tariffs.adders_for("2027-01") if a["key"] == "dss"] == [3.82]


# ------------------------------------------------------------------------------------------------ tariffs-noncanonical-start-date

def test_a_start_date_must_be_written_yyyy_mm_dd(company):
    before = tariffs.card_for("B-2(b)", date(2026, 11, 5))[1]["id"]
    for start in ("20270101", "2026-W45-1", "2027-02-30", "2027-1-1"):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):  # before: '20270101' and '2026-W45-1' were stored as given
            tariffs.propose("adder", "dss", start, {"name": "DSS", "rate": 3.5, "billing_months": "all"}, SRC, "t")
    ok = tariffs.propose("adder", "dss", "2027-01-01", {"name": "DSS", "rate": 3.5, "billing_months": "all"}, SRC, "t")
    assert ok["effective_from"] == "2027-01-01"
    # a draft stored before the check (raw start '20261101') is refused at approval and the schedule in force stays
    sched = next(v for v in tariffs.versions("schedule") if v["status"] == "active" and v["effective_from"] == "2026-01-01")
    with store.connect() as con:
        con.execute("INSERT INTO tariff_versions (id, kind, key, effective_from, effective_to, known_from, status, supersedes, body, source, hash, created, by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("raw1", "schedule", "national", "20261101", None, "2026-11-01", "pending", None,
                                                          json.dumps(sched["body"]), json.dumps(SRC), "x", store._now(), "t"))
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        tariffs.approve("raw1", "t")
    assert tariffs.card_for("B-2(b)", date(2026, 11, 5))[1]["id"] == before  # before: CY2026 closed at 31 Oct, the January 2026 card took over
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        tariffs.add_fuel_price("hsd", "20261101", 280.0, "t")


# ------------------------------------------------------------------------------------------------ demo: a pause runs the native settings

def test_the_live_demo_runs_the_native_settings_while_control_is_paused(company):
    sid = make_site(name="Demo pause", tier="advisory_writes", demo_link=demo.DEMO_LINK, control="none", genset=False)
    now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    day = datetime.fromtimestamp(now, PKT).date()
    a, b = now - 86400, now + 86400
    slots = [{"start_ts": a, "end_ts": b, "mode": "discharge", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False,
              "max_discharge_w": None, "max_charge_w": None, "genset": None}]
    _insert_plan(sid, day, slots, {"bat": np.zeros(1440)})
    plan = plans.current(sid)
    assert plan is not None
    site = sites.full(sid)
    ts = np.arange(now - 7200, now, 60, dtype=np.int64)
    _, modes = demo._policy_for(site, day, ts, plan)
    assert (modes == twin.MODE_CODE["discharge"]).all()
    until = datetime.fromtimestamp(now + 86400, timezone.utc).isoformat(timespec="seconds")
    o = sites.add_override(sid, {"kind": "pause_control", "until": until}, "t")
    with store.connect() as con:  # paused an hour ago
        con.execute("UPDATE overrides SET from_ts = ? WHERE id = ?", (datetime.fromtimestamp(now - 3600, timezone.utc).isoformat(timespec="seconds"), o["id"]))
    native = twin.native_policy(site, ts)["mode"]
    _, modes = demo._policy_for(site, day, ts, plan)  # before: the pre-pause plan's discharge throughout
    paused = ts >= (now - 3600) // 900 * 900
    assert (modes[paused] == native[paused]).all() and (modes[~paused] == twin.MODE_CODE["discharge"]).all()
    assert demo.advance(sid, now) == 60  # the pre-pause plan is still the current one (the paused tick has not run)
    written = telemetry.raw(sid, now - 3600, now, ("inverter_mode_active",))["inverter_mode_active"]
    assert set(written) == {"self_use"}
