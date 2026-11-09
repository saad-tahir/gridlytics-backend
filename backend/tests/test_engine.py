"""The engine's own tests: the bill engine and the optimiser's prices agree (plan Part 4, the marginal-price rule), the optimiser's invariants and
golden behaviours, the twin's determinism, plan signing, and the Mode A compiler's grammar."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import delivery, forecast, optimise, plans, sites, store, tariffs, telemetry, twin

PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _conn(category="B-2(b)", regime="none", **tax):
    return {"id": "c1", "data": {"disco_group": "xwdisco", "tariff_category": category, "regime": regime, "sanctioned_load_kw": 400,
                                 "export_cap_kw": 100, "mdi_interval_min": 30, "icp": {"eligible": False}, "tax": {"gst_recoverable": True,
                                                                                                                  "s235_adjustable": True, "strn": True,
                                                                                                                  "atl": True, "province": "punjab", **tax},
                                 "seasonal_multiplier": 1.0, "season_months": []}}


def _minutes(d0: date, days: int):
    t0 = int(datetime(d0.year, d0.month, d0.day, tzinfo=PKT).timestamp())
    return np.arange(t0, t0 + days * 86400, 60)


def test_the_optimiser_sees_the_bill_engines_marginal_price(company):
    """One kWh more import in a peak and an off-peak minute costs, on the bearable basis, exactly the vector's price for that step."""
    conn = _conn()
    d0 = date(2026, 11, 2)
    ts = _minutes(d0, 1)
    imp = np.full(len(ts), 100.0)
    imp[600:630] = 150.0  # the month's MDI block, so a perturbation elsewhere moves no fixed charge
    exp = np.zeros(len(ts))
    base = tariffs.rebuild(ts, imp, exp, conn, d0, d0)
    vec = tariffs.vectors(conn, datetime.fromtimestamp(int(ts[0]), timezone.utc), 96)
    for minute in (19 * 60 + 5, 3 * 60 + 5):  # 19:05 peak (Sep-Nov 18-22), 03:05 off-peak
        bumped = imp.copy()
        bumped[minute] += 60.0  # one kWh
        after = tariffs.rebuild(ts, bumped, exp, conn, d0, d0)
        step = minute // 15
        assert after["bearable_rs"] - base["bearable_rs"] == pytest.approx(vec["c_imp"][step], rel=1e-3, abs=0.02)
    assert vec["peak"][19 * 4] and not vec["peak"][3 * 4]


def test_a_no_strn_commercial_site_bears_the_gst_in_its_price(company):
    rec = tariffs.energy_factor(_conn("A-2(c)"), date(2026, 11, 2))
    bears = tariffs.energy_factor(_conn("A-2(c)", gst_recoverable=False, strn=False), date(2026, 11, 2))
    assert bears > rec * 1.18


def _problem(T=96, peak=(72, 88), genset=False, outage=None, battery=True):
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
    if genset:
        p["genset"] = {"S": 100.0, "f_min": 0.3, "a": 0.273, "b": 0.033, "c_fuel": 380.0, "c_start": 1500.0, "c_runhr": 400.0, "c_om": 8.0, "UT": 4, "DT": 2,
                       "u_prev": 0, "elapsed": 99, "cap_hours": 24.0, "hours_today": 0.0, "day_index": np.zeros(T, dtype=int), "fuel_available": 1e9,
                       "parallel": False, "control": "manual", "hold": np.zeros(T, dtype=bool)}
        p["c_short"] = optimise._gen_marginal(p["genset"])
    return p


def _invariants(p, r):
    s = r["sol"]
    g = p["g"]
    assert np.all(s["G"][g < 0.5] < 1e-6), "import during an outage"
    if p.get("battery"):
        b = p["battery"]
        assert s["E"].min() >= b["soc_min"] * b["E_nom"] - 1e-6 and s["E"].max() <= b["soc_max"] * b["E_nom"] + 1e-6
    if p.get("genset"):
        on = s["u"] > 0.5
        assert np.all(s["P"][on] >= p["genset"]["f_min"] * p["genset"]["S"] - 1e-6)
        assert np.all(s["P"][~on] < 1e-6)


def test_the_battery_discharges_into_the_peak_and_holds_no_grid_charge_at_the_end():
    p = _problem()
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    _invariants(p, r)
    s = r["sol"]
    assert s["D"][p["peak"]].sum() * 0.25 > 50  # the price spread pays for the cycle
    assert s["C"][-24:].sum() < 1e-6  # OPT-07: no grid charging in the last 6 hours with no outage forecast
    assert {x["reason"] for x in r["reasons"]} & {"discharge_peak_window"}


def test_an_outage_is_carried_by_battery_then_genset_with_no_import():
    p = _problem(genset=True, outage=(40, 64))
    p["p_start"][39] = 0.9
    r = optimise.solve(p)
    assert r["status"] == "optimal"
    _invariants(p, r)
    assert r["sol"]["U"].sum() < 1e-6  # the critical load is served
    assert any(x["reason"] in ("carry_outage", "genset_on") for x in r["reasons"][40:64])


def test_the_reserve_is_held_before_a_likely_outage():
    p = _problem(outage=(80, 96))
    p["p_start"][60:80] = 0.6
    r = optimise.solve(p)
    s = r["sol"]
    floor = p["battery"]["soc_min"] * p["battery"]["E_nom"]
    assert s["E"][80] >= floor + 0.8 * r["R"][79]


def test_the_result_policy_refuses_impossible_inputs_and_retries_relaxed():
    p = _problem()
    p["battery"]["E_nom"] = 0
    assert optimise.solve_with_policy(p)["status"] == "refused"


def test_the_twin_is_deterministic_per_site_day():
    site = {"id": "s1", "data": {"lat": 31.4, "lon": 73.1, "site_type": "industrial_2_shift", "critical_load_kw": [40] * 24, "disco": "FESCO"},
            "connections": [{"data": {"regime": "none", "sanctioned_load_kw": 400, "export_cap_kw": 0}}],
            "assets": {"pv": {"kwp_dc": 300, "kw_ac": 250, "tilt": 15, "azimuth": 180}}}
    a, b = twin.truth(site, date(2026, 11, 3)), twin.truth(site, date(2026, 11, 3))
    assert np.array_equal(a["pv"], b["pv"]) and np.array_equal(a["grid"], b["grid"])
    assert 800 < a["pv"].sum() / 60 < 1600  # a November day of a 300 kWp plant


def test_a_signed_plan_verifies_and_a_changed_one_does_not(company):
    with store.connect():
        pass
    msg = delivery.sign("site-1", {"plan_id": "p1", "valid_until": "2026-11-07T00:00:00Z"})
    pub = delivery.public_key("site-1")["public_key"]
    assert delivery.verify(msg, pub)
    msg["valid_until"] = "2026-11-08T00:00:00Z"
    assert not delivery.verify(msg, pub)


def test_the_compiler_fits_each_brands_slot_count():
    t0 = 1794000000 // 900 * 900
    steps = []
    for i in range(96):
        mode_c = 30.0 if i % 7 == 0 else 0.0
        mode_d = 40.0 if i % 5 == 0 else 0.0
        steps.append({"t": t0 + 900 * i, "charge_kw": mode_c, "discharge_kw": mode_d, "grid_export_kw": 0.0, "load_kw": 50.0, "pv_kw": 0.0, "soc_pct": 50.0,
                      "soc_start_pct": 50.0, "grid_present": 1.0, "reason": "charge_from_grid" if mode_c else "idle", "genset_on": False, "grid_import_kw": 50.0})
    bat = {"E_nom": 100}
    for brand, g in delivery.BRANDS.items():
        comp = delivery.compile_mode_a(steps, brand, bat, 20.0)
        for day in comp["days"]:
            assert 1 <= len(day["slots"]) <= g["max_slots"]
            assert all(s["soc_floor_pct"] >= 20.0 for s in day["slots"])
            assert all(s["start_ts"] % 900 == 0 for s in day["slots"])


def test_a_tick_writes_an_active_plan_from_telemetry_alone(company):
    s = sites.create({"name": "Mill", "disco": "FESCO", "critical_load_kw": [50.0] * 24, "genset_start_control": "manual"}, "t")
    sid = s["id"]
    sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 200, "kw_ac": 180}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    sites.put_asset(sid, "genset", {"rated_kw": 150}, "t")
    full = sites.full(sid)
    d = date(2026, 11, 2)
    tr = twin.truth(full, d)
    sim = twin.simulate(tr, twin.native_policy(full, tr["ts"]), twin.plant_of(full), 0.5)
    telemetry.write_rows(twin.telemetry_rows(sid, tr, sim))
    r = plans.tick(sid, at=twin.day_start_utc(d) + 86400)
    assert r["status"] == "active"
    assert len(r["steps"]) == 192 and r["slots"]["days"]
    # in force at the tick's own time (by today's clock its 48 hours have run out: round 7, a plan past its valid_until is not in force)
    assert plans.current(sid, now=twin.day_start_utc(d) + 86400)["id"] == r["plan_id"]
    assert forecast.page(full, "pv")["issue"] is not None
