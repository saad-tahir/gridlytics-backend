"""Round-7 regression tests for the cross-module engine (audit work package "crossmodule", stage 2, batch 1): the changes that need several
stage-1 packages' code at once.

- tracker-and-docs-01 (with pages-D-09): a site's connections are all priced where they are metered: the connection the plant and the site's
  meter readings are on (`plant`) is the one the plan, the ledger and the bill rebuild price, and a bill names the connection it is for.
- engine-tariffs-09: Punjab's self-generation duty comes from the connection's province (else the site's DISCO's) and the tax version in force,
  in the plan's genset cost and in the ledger's, instead of latitude > 28 and a constant in plans.py.
- engine-savings-09 (VER-13): the metering factor per connection, one energy and one MDI scalar fitted on two bills and tested on a held-out third,
  applied to both arms before pricing; outside 0.97 to 1.03 a data finding raised as an alert; the 2 % gate closed only by a held-out bill.
- followup bound-mdi (engine-optimiser-14): the perfect-foresight bound prices both arms' demand charge from the solve's own lower bound.
- engine-delivery-17 (with brief-vs-build-08): the twin executes Mode B setpoints, and the guard, the compare drawer and the demo replay them at a
  Mode B site; the schedule carries U3's primitives.
- engine-plans-live-18: a Deye or Voltronic site's plan charges from the running genset, the table keys it and the twin executes it.
- brief-vs-build-15: a genset start or stop, an intra-day forecast bust and an approved tariff version re-solve the plan.
- followup icp-gross (engine-tariffs-10, the optimiser half): a legacy site's ICP case and rows count gross imports, capped at the net.
- followup mdi-override (engine-tariffs-08 h): a site override's MDI block mode and scope reach the plan, the guard and the ledger.
- followup native-bill (pages-D-10, the rest): the bill rebuilt for the native arm beside the actual arm's.
- followup achieved (engine-delivery-12, the optimiser half): what the edge could apply re-enters the next solve.
- followup compare-end-value (engine-plans-live-10): the compare drawer values the energy each arm leaves at the horizon's end.

Each test fails on the code before its fix."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import forecast, plans, sites, store, tariffs, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))
NOV = date(2026, 11, 3)


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _genset_site(name="Mill", lat=31.4, disco="FESCO", rated_kw=500.0, conn=None, site_type="industrial_2_shift", connection=True):
    s = sites.create({"name": name, "lat": lat, "lon": 70.0, "disco": disco, "site_type": site_type, "critical_load_kw": [50.0] * 24,
                      "genset_start_control": "manual"}, "t")
    sid = s["id"]
    if connection:
        sites.put_connection(sid, conn or {"tariff_category": "B-2(b)", "sanctioned_load_kw": 800}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 200, "kw_ac": 180}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    sites.put_asset(sid, "genset", {"rated_kw": rated_kw, "f_min": 0.3, "fuel_kind": "hsd", "min_up_h": 1.0, "c_om_rs_per_kwh": 10.0}, "t")
    return sid


def _problem_of(sid, day=NOV, steps=96):
    full = sites.full(sid)
    t0 = twin.day_start_utc(day)
    return plans.build_problem(full, t0, forecast.issue(full, t0, save=False), steps)


# ------------------------------------------------------------------------------------------------ engine-tariffs-09

def test_the_selfgen_duty_follows_the_connections_province_not_the_latitude(company):
    """A Quetta site (Balochistan, latitude 30.2) with a 500 kW set paid Punjab's 4 paisa; a Punjab site south of latitude 28 did not."""
    quetta = _genset_site("Quetta Mill", lat=30.2, disco="QESCO")
    assert sites.full(quetta)["connections"][0]["data"]["tax"]["province"] == "balochistan"
    assert _problem_of(quetta)["genset"]["c_om"] == pytest.approx(10.0)
    sadiqabad = _genset_site("Sadiqabad Mill", lat=27.9, disco="MEPCO")
    assert _problem_of(sadiqabad)["genset"]["c_om"] == pytest.approx(10.04)
    # a set of 500 kVA or less pays nothing, nor does a farm on an agricultural connection
    small = _genset_site("Small Mill", lat=31.4, rated_kw=400.0)
    assert _problem_of(small)["genset"]["c_om"] == pytest.approx(10.0)
    farm = _genset_site("Tube Well", lat=31.4, conn={"tariff_category": "D-2(b)", "sanctioned_load_kw": 800})
    assert _problem_of(farm)["genset"]["c_om"] == pytest.approx(10.0)
    # the set's size is its kVA: entered, else its kW over its own power factor (420 kW is 525 kVA at 0.8, above the line, and 467 kVA at 0.9)
    full = sites.full(sadiqabad)
    g = full["assets"]["genset"]
    sized = lambda **k: tariffs.selfgen_duty({**full, "assets": {"genset": {**g, "rated_kva": None, **k}}}, NOV)  # noqa: E731
    assert sized(rated_kw=420.0, power_factor=0.8) == pytest.approx(0.04)
    assert sized(rated_kw=420.0, power_factor=0.9) == 0.0
    assert sized(rated_kw=494.0, rated_kva=520.0, power_factor=0.95) == pytest.approx(0.04)


def test_the_selfgen_duty_is_the_tax_version_in_force(company):
    """The rate is the versioned tax body's punjab_selfgen_per_kwh, not a constant in plans.py: a new tax version moves the plan's genset cost."""
    sid = _genset_site()
    tax = tariffs.tax_rules(NOV)
    assert tariffs.selfgen_duty(sites.full(sid), NOV) == pytest.approx(float(tax["punjab_selfgen_per_kwh"]))
    body = {**tax, "punjab_selfgen_per_kwh": 0.10}
    with store.connect() as con:
        con.execute("UPDATE tariff_versions SET body = ? WHERE kind = 'tax' AND key = 'national' AND status = 'active'", (json.dumps(body),))
    assert tariffs.selfgen_duty(sites.full(sid), NOV) == pytest.approx(0.10)
    assert _problem_of(sid)["genset"]["c_om"] == pytest.approx(10.10)


def test_the_ledger_prices_the_selfgen_duty_as_the_plan_does(company):
    """verify._genset_cost left the duty out, so the plan and the ledger priced a genset kWh differently (Part 4 rule, R9 9.2)."""
    punjab = sites.full(_genset_site("Punjab Mill"))
    sindh = sites.full(_genset_site("Sindh Mill", lat=25.4, disco="HESCO"))
    n = 1440
    on = np.zeros(n, dtype=bool)
    on[600:720] = True
    sim = {"gen_on": on, "fuel": np.where(on, 0.5, 0.0), "gen": np.where(on, 300.0, 0.0)}
    cost_p, litres_p = verify._genset_cost(punjab, sim, NOV)
    cost_s, litres_s = verify._genset_cost(sindh, sim, NOV)
    assert litres_p == litres_s
    kwh = 300.0 * 120 / 60
    assert cost_p - cost_s == pytest.approx(kwh * 0.04, abs=1e-6)


# ------------------------------------------------------------------------------------------------ followup:bound-mdi (engine-optimiser-14)

def test_the_bound_measures_both_arms_mdi_from_the_peak_the_solve_could_not_lower(company, monkeypatch):
    """build_problem now bounds the month's peak M under the month's expected peak (M_exp, mdi_floors) and itemises the charge at that bound
    (mdi_month_rs) apart from the rise above it (mdi_rise_rs). The bound took the hindsight plan's rise above that bound but the actual arm's rise
    above max(M0, floor) alone, so an actual peak under the expected month peak counted as a rise the hindsight plan was spared: headroom made of
    nothing. Both arms now pay the month's charge at the solve's own lower bound plus their rise above it; a time-limited solve is still no bound."""
    from test_fix_savings import WEEKDAY, _series, make_site
    sid = make_site(name="Bound M_exp r7", genset=False)
    seen: dict = {}

    def solve(p, time_limit=10.0, **kw):
        seen["p"] = p
        T = int(p["T"])
        return {"status": "optimal", "time_limited": False, "M": [150.0], "M_lb": [150.0],
                "breakdown": {"energy_rs": 0.0, "genset_rs": 0.0, "wear_rs": 0.0, "unserved_rs": 0.0, "mdi_rise_rs": 0.0,
                              "mdi_month_rs": 150.0 * float(p["mdi"]["rate"])},
                "sol": {"G": np.full(T, 100.0), "X": np.zeros(T), "E": np.r_[np.full(T, 100.0), 100.0]}}

    monkeypatch.setattr(verify.optimise, "solve", solve)
    pr = {"c_imp": np.full(1440, 40.0), "c_exp": np.zeros(1440), "peak": np.zeros(1440, dtype=bool)}
    # the actual arm: the same energy as the hindsight plan, a peak of 130 kW, under the 150 kW the month will reach anyway
    actual = {"cost_rs": 96_000.0, "unserved_kwh": 0.0, "peak_kw": 130.0}
    out = verify._bound(sites.full(sid), WEEKDAY, _series(outage=None), pr, 0.5, actual, 100.0, 0.0, c_next=0.0, v_end=0.0)
    assert "mdi" in seen["p"] and seen["p"]["mdi"]["rate"] > 0
    assert out["status"] == "optimal"
    assert out["solved_headroom_rs"] == pytest.approx(0.0, abs=0.01)
    assert out["bound_rs"] == pytest.approx(0.0, abs=0.01)
    # a peak above the bound is the actual arm's own rise, at the rate
    above = verify._bound(sites.full(sid), WEEKDAY, _series(outage=None), pr, 0.5, {**actual, "peak_kw": 160.0}, 100.0, 0.0)
    assert above["solved_headroom_rs"] == pytest.approx(10.0 * float(seen["p"]["mdi"]["rate"]), abs=0.01)
    # a solve stopped at its time limit is no upper bound
    monkeypatch.setattr(verify.optimise, "solve", lambda p, time_limit=10.0, **kw: {**solve(p), "status": "feasible", "time_limited": True})
    limited = verify._bound(sites.full(sid), WEEKDAY, _series(outage=None), pr, 0.5, actual, 100.0, 0.0)
    assert limited["status"] == "time_limited" and limited["bound_rs"] is None and limited["time_limited"] is True


# ------------------------------------------------------------------------------------------------ tracker-and-docs-01 (pages-D-09)

def _two_connection_site():
    """A mill whose first connection (B-2(b), 500 kW) feeds its offices and whose second (B-3, 1,500 kW) carries the plant and the site's meter."""
    sid = _genset_site("Two Meter Mill", conn={"meter_ref": "OFFICE-1", "tariff_category": "B-2(b)", "sanctioned_load_kw": 500})
    first = sites.connections(sid)[0]["id"]
    second = sites.put_connection(sid, {"meter_ref": "MILL-2", "tariff_category": "B-3", "sanctioned_load_kw": 1500, "plant": True}, "t")["id"]
    return sid, first, second


def test_the_connection_the_plant_is_on_is_the_one_priced(company):
    """Every engine path read connections[0], the first saved: a second connection was stored and shown with its own prices, and the plan priced the
    first in its place. The connection marked as the plant's (its meter is the site's readings) comes first, and the plan prices it."""
    sid, first, second = _two_connection_site()
    conns = sites.connections(sid)
    assert [c["id"] for c in conns] == [second, first]
    assert [c["plant"] for c in conns] == [True, False]
    p = _problem_of(sid)
    assert p["G_max"] == pytest.approx(1500 * 1.25)            # the B-3 connection's sanctioned load, not the first's 500 kW
    assert sites.plant_connection(sites.full(sid))["id"] == second
    # a save of the plant's connection that leaves the flag out keeps it; marking the first moves it there and unmarks the second
    sites.put_connection(sid, {"meter_ref": "MILL-2", "tariff_category": "B-3", "sanctioned_load_kw": 1600}, "t", conn_id=second)
    assert sites.connections(sid)[0]["id"] == second
    sites.put_connection(sid, {"meter_ref": "OFFICE-1", "tariff_category": "B-2(b)", "sanctioned_load_kw": 500, "plant": True}, "t", conn_id=first)
    conns = sites.connections(sid)
    assert [c["id"] for c in conns] == [first, second] and conns[1]["data"]["plant"] is False
    assert _problem_of(sid)["G_max"] == pytest.approx(500 * 1.25)


def test_a_bill_is_entered_for_the_connection_it_belongs_to(company):
    """add_bill took connections[0] for every bill: the second connection's bill was stored under the first and rebuilt against the first's tariff and
    the site's meter. A bill names its connection; the plant's is rebuilt from the readings, another connection's is kept but not rebuilt (the
    readings are not its meter), and the list says which connection each bill is for."""
    sid, first, second = _two_connection_site()
    base = {"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": 1_000_000}
    office = verify.add_bill(sid, {**base, "connection_id": first}, "t")
    assert office["connection_id"] == first and office["check"]["verdict"] == "not_metered"
    assert office["rebuilt"]["metered"] is False and office["difference_pct"] is None
    assert office["connection"] == {"id": first, "number": 2, "meter_ref": "OFFICE-1", "tariff_category": "B-2(b)", "plant": False}
    mill = verify.add_bill(sid, base, "t")                       # no connection named: the plant's
    assert mill["connection_id"] == second and mill["connection"]["plant"] is True
    assert mill["rebuilt"].get("metered", True) is not False and mill["check"]["verdict"] != "not_metered"
    by_id = {c["id"]: c for c in sites.connections(sid)}
    own = verify.rebuild_bill(sid, by_id[second], date(2026, 10, 1), date(2026, 10, 31))
    other = verify.rebuild_bill(sid, by_id[first], date(2026, 10, 1), date(2026, 10, 31))
    assert mill["rebuilt"]["total_rs"] == pytest.approx(own["total_rs"]) and own["total_rs"] != pytest.approx(other["total_rs"])
    # the same period on another connection is not an overlap; on the same one it is
    with pytest.raises(ValueError, match="overlaps"):
        verify.add_bill(sid, {**base, "connection_id": first}, "t")
    with pytest.raises(ValueError, match="no such grid connection"):
        verify.add_bill(sid, {**base, "period_start": "2026-09-01", "period_end": "2026-09-30", "connection_id": "nope"}, "t")
    # a bill entered against the wrong connection is moved by a correction, and rebuilt for its own
    moved = verify.correct_bill(sid, office["id"], {"period_start": "2026-09-01", "period_end": "2026-09-30", "total_rs": 900_000,
                                                   "connection_id": second}, "t")
    assert moved["connection_id"] == second and moved["check"]["verdict"] != "not_metered"
    assert {b["connection"]["id"] for b in verify.bills(sid)} == {second}


def test_the_site_answer_and_the_prices_name_the_plants_connection(world):
    """The API: the site's connections carry `plant`, a bill takes `connection_id`, and an unknown one is a one-sentence 422."""
    c, sid = world.c["admin"], world.site
    r = c.put(f"/api/sites/{sid}/connection", json={"meter_ref": "SECOND-9", "tariff_category": "B-3", "sanctioned_load_kw": 900, "plant": True})
    assert r.status_code == 200, r.text
    conns = r.json()["connections"]
    assert conns[0]["data"]["meter_ref"] == "SECOND-9" and conns[0]["plant"] is True and conns[1]["plant"] is False
    bad = c.post(f"/api/sites/{sid}/bills", json={"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": 5, "connection_id": "nope"})
    assert bad.status_code == 422 and "no such grid connection" in bad.json()["detail"]
    ok = c.post(f"/api/sites/{sid}/bills", json={"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": 5,
                                                 "connection_id": conns[1]["id"]})
    assert ok.status_code == 200 and ok.json()["check"]["verdict"] == "not_metered"


# ------------------------------------------------------------------------------------------------ engine-savings-09 (the metering factor, VER-13)

MONTHS = [(date(2026, 8, 1), date(2026, 8, 31)), (date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 10, 1), date(2026, 10, 31))]


def _metered_site(name):
    from test_fix_r2_savings import _flat_rows
    from test_fix_savings import make_site
    sid = make_site(name=name)
    _flat_rows(sid, MONTHS[0][0], 92, 100.0, step_min=15)
    return sid


def _printed(sid, p0, p1, k_e, k_m, total_off=1.0):
    """The bill the DISCO prints when its register reads k_e times the site's meter on energy and k_m on MDI."""
    conn = sites.connections(sid)[0]
    raw = verify.rebuild_bill(sid, conn, p0, p1)
    reg = verify.rebuild_bill(sid, conn, p0, p1, energy_scalar=k_e, mdi_scalar=k_m)
    return {"period_start": p0.isoformat(), "period_end": p1.isoformat(), "total_rs": round(reg["total_rs"] * total_off, 2),
            "units_peak": round(raw["import_kwh"]["peak"] * k_e, 1), "units_off_peak": round(raw["import_kwh"]["off_peak"] * k_e, 1),
            "mdi_kw": round(raw["mdi_kw"] * k_m, 2)}


def test_the_metering_factor_is_fitted_on_two_bills_tested_on_a_held_out_third_and_applied_to_both_arms(company):
    """VER-13 (plan 6.3, 8.2): the site's meter reads the DISCO's register 2 % low on energy and 1 % low on MDI. Before, nothing fitted a factor: both
    arms and the rebuild priced the raw meter, so every rupee lever was 2 % short and the bill check could never close. One energy and one MDI scalar
    per connection are fitted on the bills before the latest and tested on the latest, held out; until three bills exist the rebuild runs
    uncalibrated and the 2 % check stays open; once fitted, both arms are priced on the register's scale."""
    sid = _metered_site("Metered Mill")
    for p0, p1 in MONTHS[:2]:
        b = verify.add_bill(sid, _printed(sid, p0, p1, 1.02, 1.01), "t")
    m = b["metering"]
    assert m["status"] == "uncalibrated" and m["applied"] is False and m["bills"] == 2 and m["gate"] == "open"
    assert verify._minute_prices(sites.connections(sid)[0], date(2026, 10, 14), sid)["k_energy"] == 1.0
    oct_ = verify.add_bill(sid, _printed(sid, *MONTHS[2], 1.02, 1.01), "t")
    m = oct_["metering"]
    assert m["status"] == "calibrated" and m["applied"] is True
    assert m["energy"] == pytest.approx(1.02, abs=0.0005) and m["mdi"] == pytest.approx(1.01, abs=0.0005)
    assert len(m["fit_bills"]) == 2 and m["held_out"]["bill_id"] == oct_["id"]
    assert abs(m["held_out"]["difference_pct"]) <= 2.0 and m["held_out"]["within"] is True and m["gate"] == "closed"
    assert all(b["metering"] == m for b in verify.bills(sid))
    # applied to both arms before pricing: a day's import, export and MDI blocks are on the register's scale
    conn = sites.connections(sid)[0]
    pr = verify._minute_prices(conn, date(2026, 10, 14), sid)
    assert pr["k_energy"] == pytest.approx(m["energy"]) and pr["k_mdi"] == pytest.approx(m["mdi"])
    sim = {"imp": np.full(1440, 100.0), "exp": np.zeros(1440), "bat": np.zeros(1440), "gen": np.zeros(1440), "gen_on": np.zeros(1440, dtype=bool),
           "fuel": np.zeros(1440), "unserved": np.zeros(1440)}
    site = sites.full(sid)
    scaled = verify.price_arm(site, sim, pr, date(2026, 10, 14), 0.0, 0.0)
    plain = verify.price_arm(site, sim, {**pr, "k_energy": 1.0, "k_mdi": 1.0}, date(2026, 10, 14), 0.0, 0.0)
    assert scaled["cost_rs"] == pytest.approx(plain["cost_rs"] * m["energy"], rel=1e-4)
    assert scaled["peak_kw"] == pytest.approx(plain["peak_kw"] * m["mdi"], rel=1e-4)
    # a day priced before the factor was fitted is priced on another basis, so its month is re-scored when it settles
    assert verify._price_basis(pr, None) != verify._price_basis({**pr, "k_energy": 1.0, "k_mdi": 1.0}, None)


def test_a_held_out_bill_off_by_more_than_2_percent_keeps_the_gate_open(company):
    sid = _metered_site("Gate Mill")
    for p0, p1 in MONTHS[:2]:
        verify.add_bill(sid, _printed(sid, p0, p1, 1.02, 1.01), "t")
    oct_ = verify.add_bill(sid, _printed(sid, *MONTHS[2], 1.02, 1.01, total_off=1.05), "t")
    m = oct_["metering"]
    assert m["status"] == "calibrated" and m["held_out"]["within"] is False and m["gate"] == "open"
    # deleting the held-out bill leaves two: uncalibrated again
    verify.delete_bill(sid, oct_["id"], "t")
    assert verify.bills(sid)[0]["metering"]["status"] == "uncalibrated"
    assert verify._minute_prices(sites.connections(sid)[0], date(2026, 10, 14), sid)["k_energy"] == 1.0


def test_a_factor_outside_097_to_103_is_a_data_finding_not_a_calibration(company):
    """A meter 6 % below the register is a load the inverter does not see or a second meter: raised as a data finding, never applied."""
    from gridlytics import alerts
    sid = _metered_site("Short Meter Mill")
    for p0, p1 in MONTHS:
        b = verify.add_bill(sid, _printed(sid, p0, p1, 1.06, 1.0), "t")
    m = b["metering"]
    assert m["status"] == "out_of_band" and m["applied"] is False and m["energy"] == pytest.approx(1.06, abs=0.0005) and m["gate"] == "open"
    assert verify._minute_prices(sites.connections(sid)[0], date(2026, 10, 14), sid)["k_energy"] == 1.0
    alerts.evaluate(sid)
    found = [a for a in alerts.listing(site_id=sid) if a["rule"] == "metering_factor"]
    assert found and found[0]["category"] == "Data" and "6.0 %" in found[0]["title"]


# ------------------------------------------------------------------------------------------------ engine-delivery-17 (with brief-vs-build-08): Mode B

def _flat_series(n_steps: int, load=100.0, pv=0.0, t0=None):
    t0 = twin.day_start_utc(NOV) if t0 is None else t0
    ts = t0 + np.arange(n_steps * 15) * 60
    n = len(ts)
    return {"ts": ts, "pv": np.full(n, pv), "load": np.full(n, load), "crit": np.full(n, 20.0), "grid": np.ones(n), "temp": np.full(n, 30.0)}


def test_the_twin_executes_mode_b_setpoints_step_by_step():
    """The twin had no Mode B executor: every replay ran the Mode A table. Each 15-minute step's battery power is followed within the battery's
    power, the step's floor and target; a charge beyond the PV surplus draws on the grid only where the step allows it, and a discharge no load or
    export cap takes is not made (the edge's 1-minute rule layer)."""
    t0 = twin.day_start_utc(NOV)
    steps = [{"t": t0, "battery_power_w": 50_000, "soc_floor_pct": 20.0, "grid_charge_allowed": False},
             {"t": t0 + 900, "battery_power_w": -30_000, "soc_floor_pct": 20.0, "soc_target_pct": 95.0, "grid_charge_allowed": True},
             {"t": t0 + 1800, "battery_power_w": -30_000, "soc_floor_pct": 20.0, "grid_charge_allowed": False},
             {"t": t0 + 2700, "battery_power_w": 150_000, "soc_floor_pct": 20.0, "grid_charge_allowed": False, "export_limit_w": 0},
             {"t": t0 + 3600, "battery_power_w": 0, "soc_floor_pct": 20.0, "genset_request": "prefer_start"}]
    series = _flat_series(5)
    plant = twin.Plant(e_nom=200.0, p_ch=100.0, p_dis=200.0, g_max=500.0)
    pol = twin.setpoints_policy({"step_s": 900, "steps": steps}, series["ts"])
    assert set(pol["mode"]) == {twin.MODE_CODE["setpoint"]} and pol["genset"][-1] == 1 and pol["genset"][0] == 0
    sim = twin.simulate(series, pol, plant, 0.6)
    q = lambda k: sim["bat"][15 * k:15 * k + 15]  # noqa: E731
    assert np.allclose(q(0), 50.0)                       # discharge at the setpoint
    assert np.allclose(q(1), -30.0)                      # a grid charge the step allows
    assert np.allclose(q(2), 0.0)                        # no surplus, no grid charge allowed: no charge
    assert np.allclose(q(3), 100.0)                      # 150 kW asked, the load takes 100 and nothing may be exported
    assert np.allclose(q(4), 0.0)
    assert np.allclose(sim["imp"][:15], 50.0) and np.allclose(sim["imp"][15:30], 130.0) and sim["exp"].max() == 0.0
    # a floor stops the discharge
    low = twin.simulate(series, pol, plant, 0.21)
    assert low["soc"][14] >= 0.2 - 1e-6 and low["bat"][:15].sum() / 60 < 50.0 * 0.25


def test_the_mode_b_schedule_carries_u3s_primitives():
    """mode_b() left out battery_mode, soc_target_pct, export_limit_w and gen_charge_allowed, and sent genset_request as a bool, not U3's enum."""
    from gridlytics import delivery
    from test_fix_r3_optimiser import _steps_for
    steps = _steps_for([("charge_from_grid", 40.0, 0.0), ("discharge_peak_window", 0.0, 30.0), ("hold_for_outage", 0.0, 0.0)])
    for st, soc in zip(steps, (60.0, 50.0, 50.0)):
        st["soc_pct"], st["reserve_pct"] = soc, 20.0
    steps[1]["genset_on"] = True
    site = {"connections": [{"data": {"regime": "net_billing", "export_cap_kw": 80}}]}
    mb = delivery.mode_b(steps, site)
    assert mb["refresh"] == {"heartbeat_s": 60, "revert_after_s": 1800, "residual_band_w": 300} and mb["step_s"] == 900
    a, b, c = mb["steps"]
    assert a["battery_mode"] == "charge" and a["soc_target_pct"] == 60.0 and a["battery_power_w"] == -40_000 and a["grid_charge_allowed"]
    assert b["battery_mode"] == "discharge" and b["soc_target_pct"] is None and b["genset_request"] == "prefer_start"
    assert c["battery_mode"] == "hold" and c["genset_request"] == "prefer_stop" and a["genset_request"] == "none"
    assert {x["export_limit_w"] for x in mb["steps"]} == {80_000} and not any(x["gen_charge_allowed"] for x in mb["steps"])


def _mode_b_site(name="Mode B Mill"):
    from test_fix_savings import make_site
    sid = make_site(name=name, genset=False)
    with store.connect() as con:
        r = con.execute("SELECT data FROM sites WHERE id = ?", (sid,)).fetchone()
        con.execute("UPDATE sites SET data = ? WHERE id = ?", (json.dumps({**json.loads(r["data"]), "control_mode": "mode_b"}), sid))
    sites.put_asset(sid, "inverter", {"brand": "sungrow", "model": "SH10RT", "kw_ac": 100}, "t")
    return sid


def test_a_mode_b_sites_guard_and_demo_replay_the_setpoints_not_the_table(company, monkeypatch):
    """The guard, the compare drawer and the demo replayed the Sungrow 2-slot Mode A table at a Mode B site, whose edge never runs it."""
    from gridlytics import delivery, demo
    sid = _mode_b_site()
    site = sites.full(sid)
    assert site["tier"] == "closed_loop" and delivery.executes_mode_b(site)
    used = []
    real_sp, real_slots = twin.setpoints_policy, twin.slots_policy
    monkeypatch.setattr(twin, "setpoints_policy", lambda *a, **k: used.append("setpoints") or real_sp(*a, **k))
    monkeypatch.setattr(twin, "slots_policy", lambda *a, **k: used.append("slots") or real_slots(*a, **k))
    p = _problem_of(sid)
    res = __import__("gridlytics.optimise", fromlist=["solve"]).solve(p, time_limit=20.0)
    steps = plans._steps_out(p, res, forecast.issue(site, p["ts"][0], save=False))
    compiled = delivery.compile_mode_a(steps, "sungrow", p["battery"], 20.0, site=site)
    used.clear()                                       # the compiler's own table check replays the table: it is the fallback for slot-only devices
    plans._guard(site, p, compiled, steps, res)
    assert used == ["setpoints"]
    used.clear()
    ts = twin.minutes_of(NOV)
    pol, modes = demo._policy_for(site, NOV, ts, {"steps": steps, "slots": compiled})
    assert "setpoints" in used and "slots" not in used and twin.MODE_CODE["setpoint"] in set(modes)
    # a Mode B site at the advisory tier runs the table a person keys
    assert not delivery.executes_mode_b({**site, "tier": "advisory_writes"})


def test_compare_with_native_replays_a_mode_b_plan_as_its_setpoints(world, monkeypatch):
    """At a Mode B site the drawer's plan arm ran the compiled table; it now follows the plan's own path on the plan's forecast."""
    from test_fix_r7_delivery_twin import _compare_ready, _reserve_plan
    steps, comp = _reserve_plan()
    # the plan discharges 40 kW for the first hour, then holds: a table of one hold slot would not
    for i, st in enumerate(steps):
        st.update(discharge_kw=40.0 if i < 4 else 0.0, charge_kw=0.0, grid_import_kw=40.0 if i < 4 else 80.0, grid_export_kw=0.0, pv_kw=0.0,
                  reason="discharge_peak_window" if i < 4 else "hold_for_outage", genset_on=False, reserve_pct=10.0)
    monkeypatch.setattr(plans, "current", lambda site_id, *x, **k: {"id": "p1", "mode": "b", "slots": comp, "steps": steps, "summary": {}})
    _compare_ready(world)
    with world.use():
        sites.set_tier_capable(world.site, "closed_loop")
        sites.set_tier_allowed(world.site, "closed_loop", "t")
        sites.update(world.site, {"control_mode": "mode_b"}, "t")
    plan = world.c["admin"].get(f"/api/sites/{world.site}/plan/compare").json()["compare"]["plan"]
    assert plan["soc"][4] == pytest.approx(60.0 - 40.0 / 0.95 * 1.0 / 200 * 100, abs=1.0)   # an hour at 40 kW from 60 % of 200 kWh
    assert plan["import_kwh"] == pytest.approx(40.0 + 80.0, abs=1.0)


# ------------------------------------------------------------------------------------------------ brief-vs-build-15: event re-solves

def test_a_genset_start_or_stop_re_solves_once_telemetry_shows_it(world, monkeypatch):
    """OPT-15 lists the genset among the events that re-plan; only outages did. A genset start waits, as an outage does, until the telemetry a tick
    reads shows the set running, then re-solves; a stop the same way."""
    from gridlytics import routes_edge
    from test_fix_api import _batch, _now
    from test_fix_r2_api import _clock, _events, _iso
    sid = world.site
    kh = world.key(["telemetry:write"])
    with world.use():
        sites.set_status(sid, "active")
    ticks = []
    monkeypatch.setattr(plans, "tick", lambda site_id, *a, **k: ticks.append(site_id) or {"status": "active"})
    minute = _now() // 60 * 60 - 1800
    clock = _clock(monkeypatch, minute + 5)
    cols = ("ts", "grid_present", "genset_running")
    post_t = lambda seq, rows: world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(seq, rows=rows, columns=cols))  # noqa: E731
    post_e = lambda seq, ev: world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_events(seq, ev))  # noqa: E731
    assert post_t(1, [[minute - 60, 1, 0]]).status_code == 200
    r = post_e(1, [{"ts": _iso(minute + 5), "type": "genset_start"}])
    assert r.status_code == 200 and r.json()["resolve"] == "waiting_for_telemetry" and ticks == [], r.text   # before: None, never re-solved
    clock["now"] = minute + 65
    r = post_t(2, [[minute, 1, 1]])
    assert r.json()["resolve"] == "scheduled" and ticks == [sid], r.text
    routes_edge._last_event_solve.clear()
    clock["now"] = minute + 305
    assert post_e(2, [{"ts": _iso(minute + 300), "type": "genset_stop"}]).json()["resolve"] == "waiting_for_telemetry"
    clock["now"] = minute + 365
    assert post_t(3, [[minute + 240, 1, 1]]).json()["resolve"] == "waiting_for_telemetry"          # still running in the rows a tick reads
    r = post_t(4, [[minute + 300, 1, 0]])
    assert r.json()["resolve"] == "scheduled" and ticks == [sid, sid], r.text


def test_a_forecast_bust_re_solves_once_per_forecast_issue(world, monkeypatch):
    """A busted PV forecast waited up to 15 minutes for the next tick. A telemetry batch whose last 30 minutes of PV miss the forecast the plan was
    solved on by more than the site's bust threshold (of the clear-sky ceiling) re-solves the plan, once per forecast issue."""
    from gridlytics import alerts, routes_edge
    from test_fix_api import _batch, _now
    from test_fix_r2_api import _clock
    sid = world.site
    kh = world.key(["telemetry:write"])
    with world.use():
        sites.set_status(sid, "active")
        sites.put_asset(sid, "pv", {"kwp_dc": 300, "kw_ac": 250}, "t")
    ticks = []
    monkeypatch.setattr(plans, "tick", lambda site_id, *a, **k: ticks.append(site_id) or {"status": "active"})
    q = (_now() // 900 - 4) * 900
    clock = _clock(monkeypatch, q + 1800 + 5)
    with world.use():
        telemetry.save_forecast(sid, "pv", q, 1, "t", {"q50": [200.0] * 192, "cs": [240.0] * 192})
    cols = ("ts", "grid_present", "pv_power_w")
    rows = lambda kw: [[q + 60 * i, 1, kw * 1000] for i in range(30)]  # noqa: E731
    with world.use():
        assert alerts.intraday_bust(sid, q + 1800 + 5) is None                                         # nothing read yet
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(1, rows=rows(190.0), columns=cols))
    assert r.status_code == 200 and r.json().get("resolve") is None and ticks == [], r.text          # 10 kW off a 240 kW ceiling: no bust
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(2, rows=rows(60.0), columns=cols))
    assert r.json()["resolve"] == "scheduled" and r.json()["resolve_reason"] == "forecast_bust" and ticks == [sid], r.text
    with world.use():
        assert alerts.intraday_bust(sid, q + 1800 + 5)["err_pct"] == pytest.approx(140 / 240 * 100, abs=0.1)
    routes_edge._last_event_solve.clear()
    clock["now"] += 60
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(3, rows=[[q + 1800, 1, 60_000]], columns=cols))
    assert r.json().get("resolve") is None and ticks == [sid]                                        # the same issue re-solves once


def test_approving_a_tariff_version_in_the_horizon_re_solves_the_plans(world, monkeypatch):
    """A newly approved tariff version applied at the next scheduled tick; one starting within the plans' two days now re-solves every active grid
    site's plan once the answer is sent. One starting later re-solves nothing."""
    from datetime import date as _date
    from test_fix_tariffs import SRC
    with world.use():
        sites.set_status(world.site, "active")
        tax = tariffs.tax_rules(_date.today())
    ticks = []
    monkeypatch.setattr(plans, "tick", lambda site_id, *a, **k: ticks.append(site_id) or {"status": "active"})
    a = world.c["admin"]
    today = datetime.now(PKT).date()
    with world.use():
        soon = tariffs.propose("tax", "national", (today + timedelta(days=1)).isoformat(), {**tax, "punjab_selfgen_per_kwh": 0.05}, SRC, "t")
        later = tariffs.propose("tax", "national", (today + timedelta(days=40)).isoformat(), {**tax, "punjab_selfgen_per_kwh": 0.06}, SRC, "t")
    r = a.post(f"/api/tariffs/versions/{soon['id']}/approve")
    assert r.status_code == 200 and r.json()["re_solving"] == 1 and ticks == [world.site], r.text
    r = a.post(f"/api/tariffs/versions/{later['id']}/approve")
    assert r.status_code == 200 and r.json()["re_solving"] == 0 and ticks == [world.site], r.text


# ------------------------------------------------------------------------------------------------ followup:icp-gross (engine-tariffs-10, the optimiser half)

def _legacy_icp_conn():
    from test_fix_tariffs import _conn
    return _conn("B-2(b)", "net_metering_legacy", extra={"agreement_start": "2024-01-01", "agreement_end": None,
                                                         "icp": {"eligible": True, "peak": {"11": 500.0}, "off_peak": {"11": 1000.0}}})


def test_a_legacy_sites_icp_case_counts_gross_imports_capped_at_the_net(company):
    """Golden (SRO 2409(I)/2025, R9 section 5; the bill since stage 1): the peak period imported 1,000 kWh and exported 600 against a 514 kWh
    baseline (500 escalated 2.8 % for package year 1); off-peak 2,000 and 1,000 against 1,028. The bill's increments are the gross imports over the
    baseline (486 and 972, each period ending as a net import), capped at the month's imports less exports (1,400). The vectors decided the case on
    the net (400 kWh, under the baseline: 'below'), so the plan and the ledger priced the peak at the normal rate while the bill took the ICP rate."""
    from datetime import datetime as _dt
    conn = _legacy_icp_conn()
    t0 = _dt(2026, 11, 20, tzinfo=PKT).astimezone(timezone.utc)
    vec = tariffs.vectors(conn, t0, 96, {"peak": 1000.0, "off_peak": 2000.0}, None, 1e-6, {"peak": 600.0, "off_peak": 1000.0}, None)
    pk, off = vec["icp"]["peak"], vec["icp"]["off_peak"]
    assert pk["case"] == "above" and pk["q_kwh"] == pytest.approx(1000.0) and pk["gross"] is True
    assert off["case"] == "above" and off["q_kwh"] == pytest.approx(2000.0)
    assert pk["baseline_kwh"] == pytest.approx(514.0) and off["baseline_kwh"] == pytest.approx(1028.0)
    assert pk["cap_kwh"] == pytest.approx(1400.0) and pk["other_inc_kwh"] == pytest.approx(972.0) and off["other_inc_kwh"] == pytest.approx(486.0)
    icp_steps = np.asarray(vec["peak"], dtype=bool)
    assert np.allclose(np.asarray(vec["c_imp"])[icp_steps], pk["rate"])                    # the peak steps at the ICP rate
    # a period that ends the month in surplus has no increment, whatever its gross imports
    surplus = tariffs.vectors(conn, t0, 96, {"peak": 1000.0, "off_peak": 2000.0}, None, 1e-6, {"peak": 1200.0, "off_peak": 0.0}, None)
    assert surplus["icp"]["peak"]["case"] == "below" and surplus["icp"]["off_peak"]["other_inc_kwh"] == 0.0
    # a net-billing site's case is unchanged: its imports are never netted
    nb = dict(conn, data={**conn["data"], "regime": "net_billing"})
    assert "gross" not in tariffs.vectors(nb, t0, 96, {"peak": 1000.0, "off_peak": 2000.0}, None, 1e-6)["icp"]["peak"]


def test_the_uncertain_icp_rows_cap_a_legacy_increment_at_the_months_net_import():
    """The optimiser's uncertain-case rows read gross imports at a legacy site, with the cap: importing and exporting at once moves the month's net
    import by nothing, so circulating energy through the meter never earns the ICP rate, and an increment beyond the net import is not counted."""
    from gridlytics import optimise
    from test_fix_optimiser import _problem
    p = _problem(T=96, battery=True)
    p["X_max"], p["regime"] = 200.0, "net_metering_legacy"
    p["c_exp"] = p["c_imp"] - 0.01
    # the peak's gross imports so far sit 10 kWh under the baseline, and the month has exported 2,500 kWh more than it imported: no increment fits
    p["icp"] = {"peak": {"case": "uncertain", "baseline_kwh": 1000.0, "q_kwh": 990.0, "rate": 30.0, "gross": True, "cap_kwh": -2500.0,
                         "other_inc_kwh": 0.0}}
    r = optimise.solve(p, time_limit=20.0)
    assert r["status"] == "optimal"
    G, X = r["sol"]["G"], r["sol"]["X"]
    assert int(np.sum((G > 0.1) & (X > 0.1))) == 0
    assert r["breakdown"].get("energy_rs") is not None
    # with room under the cap the peak's imports above the baseline earn the ICP rate as before
    q = dict(p, icp={"peak": {**p["icp"]["peak"], "cap_kwh": 50_000.0}})
    r2 = optimise.solve(q, time_limit=20.0)
    assert r2["status"] == "optimal" and r2["objective_rs"] < r["objective_rs"]


# ------------------------------------------------------------------------------------------------ followup:mdi-override (engine-tariffs-08 h)

def test_a_site_overrides_mdi_block_and_scope_reach_the_plan_the_guard_and_the_ledger(company):
    """A site override's MDI block mode (fixed or sliding) and peak-only scope (R9 12.6 and 13) reached the bill only: the plan's MDI rows and the
    ledger's arms read fixed blocks over all hours, so a peak-only meter's off-peak charging was priced as a month's demand charge."""
    from gridlytics import optimise
    from test_fix_r7_tariffs import _override
    from test_fix_savings import make_site
    sid = make_site(name="Peak MDI Mill", genset=False)
    conn = sites.connections(sid)[0]
    assert _problem_of(sid)["mdi"]["block"] == "fixed" and _problem_of(sid)["mdi"]["scope"] == "all_hours"
    _override(conn, {"name": "From the October 2026 bill", "mdi_block": "sliding", "mdi_scope": "peak"}, start="2026-10-01")
    p = _problem_of(sid)
    assert p["mdi"]["block"] == "sliding" and p["mdi"]["scope"] == "peak"
    wins = optimise.mdi_windows(p)
    peak = np.asarray(p["peak"], dtype=bool)
    assert wins and all(peak[w].all() and len(w) == p["mdi"]["n_mdi"] for w in wins)
    assert len(wins) == int(peak.sum()) - (p["mdi"]["n_mdi"] - 1) * len(np.flatnonzero(np.diff(np.r_[0, peak.astype(int)]) == 1))  # one per step
    # the ledger: an off-peak burst sets no MDI on a peak-only meter; a sliding window catches a peak half-hour that straddles two fixed blocks
    pr = verify._minute_prices(conn, NOV, sid)
    assert pr["mdi_block"] == "sliding" and pr["mdi_scope"] == "peak"
    pk = np.flatnonzero(pr["peak"])
    imp = np.full(1440, 100.0)
    imp[180:210] = 400.0                                   # 03:00-03:30, off-peak
    imp[pk[0] + 15:pk[0] + 45] = 250.0                     # half an hour inside the peak, across a block boundary
    sim = {"imp": imp, "exp": np.zeros(1440), "bat": np.zeros(1440), "gen": np.zeros(1440), "gen_on": np.zeros(1440, dtype=bool),
           "fuel": np.zeros(1440), "unserved": np.zeros(1440)}
    site = sites.full(sid)
    assert verify.price_arm(site, sim, pr, NOV, 0.0, 0.0)["peak_kw"] == pytest.approx(250.0)
    fixed_all = verify.price_arm(site, sim, {**pr, "mdi_block": "fixed", "mdi_scope": "all_hours"}, NOV, 0.0, 0.0)["peak_kw"]
    assert fixed_all == pytest.approx(400.0)                                                     # before
    assert verify._price_basis(pr, None) != verify._price_basis({**pr, "mdi_block": "fixed", "mdi_scope": "all_hours"}, None)


# ------------------------------------------------------------------------------------------------ followup:native-bill (pages-D-10, the rest)

def test_the_bill_is_rebuilt_for_the_native_arm_beside_the_actual(company):
    """R8 2.12: 'the DISCO bill vs the rebuilt bill for both arms'. Only the actual arm's bill was rebuilt. The native arm's bill over the same period
    is now rebuilt by the same engine from the ledger's native replay of each scored day, with how many days were replayed and the difference."""
    from test_fix_savings import make_site, twin_day
    sid = make_site(name="Native Bill Mill", genset=False)
    days = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]
    hold = lambda site, ts: twin.table_policy([{"start": "00:00", "mode": "hold", "soc_floor_pct": 20, "soc_target_pct": 60}], ts)  # noqa: E731
    soc = 0.6
    for k, d in enumerate(days):
        _, sim, _ = twin_day(sid, d, soc, seed=k + 1, policy=hold)       # the site held its battery: native self-use would have spent it
        soc = float(sim["soc"][-1])
    for d in days:
        assert verify.compute_day(sid, d, with_bound=False) is not None
    b = verify.add_bill(sid, {"period_start": "2026-10-12", "period_end": "2026-10-14", "total_rs": 100_000}, "t")
    nat = b["rebuilt"]["native_arm"]
    assert nat["days"] == 3 and nat["days_replayed"] == 3
    replay = [verify._native_minutes(sid, d) for d in days]
    assert sum(float(x[1].sum()) / 60 for x in replay) == pytest.approx(sum(nat["import_kwh"].values()), rel=1e-3)
    actual = b["rebuilt"]
    assert sum(nat["import_kwh"].values()) < sum(actual["import_kwh"].values())                 # native discharged into the load
    assert nat["difference_rs"] == pytest.approx(nat["total_rs"] - actual["total_rs"], abs=0.01) and nat["difference_rs"] < 0
    assert {ln["line"] for ln in nat["lines"]} >= {"Energy peak", "Energy off-peak"}
    # a period the ledger has not scored has no native bill
    none = verify.add_bill(sid, {"period_start": "2026-09-01", "period_end": "2026-09-30", "total_rs": 100_000}, "t")
    assert none["rebuilt"]["native_arm"] is None


# ------------------------------------------------------------------------------------------------ followup:achieved (engine-delivery-12, the optimiser half)

def test_what_the_edge_could_apply_re_enters_the_next_solve(company):
    """R6 C5: a partial or rejected ack re-enters the optimiser. The acks were stored and shown, and the next solve planned the same grid charge the
    device refused and the same power it clamped. It now plans within what the edge last reported it applied."""
    from test_fix_savings import make_site
    sid = make_site(name="Clamped Mill", genset=False)                  # closed loop
    p0 = _problem_of(sid)
    assert p0["battery"]["a_grid"] is True and p0["battery"]["P_ch"] == pytest.approx(100.0) and "achieved" not in p0
    body = {"contract": "ems-edge/1.0", "plan_id": "p-sent", "status": "partial", "per_device": [
        {"device_id": "inv1", "status": "partial",
         "applied": [{"primitive": "battery_power_w", "requested": -100000, "written": -60000, "readback": -60000},
                     {"primitive": "battery_power_w", "requested": 90000, "written": 70000, "readback": 70000},
                     {"primitive": "soc_floor_pct", "requested": 20, "written": 30, "readback": 30}],
         "rejected": [{"primitive": "grid_charge_allowed", "reason": "NOT_SUPPORTED"}]}]}
    at = datetime.fromtimestamp(twin.day_start_utc(NOV) - 600, timezone.utc).isoformat(timespec="seconds")
    with store.connect() as con:
        con.execute("INSERT INTO acks (id, site_id, plan_id, status, body, received_at) VALUES (?,?,?,?,?,?)",
                    ("a1", sid, "p-sent", "partial", json.dumps(body), at))
    p = _problem_of(sid)
    bat = p["battery"]
    assert bat["a_grid"] is False and bat["P_ch"] == pytest.approx(60.0) and bat["P_dis"] == pytest.approx(70.0)
    assert bat["soc_floor_grid"] == pytest.approx(0.30)
    assert p["achieved"] == {"plan_id": "p-sent", "received_at": at, "grid_charge_allowed": "refused", "charge_kw": 60.0, "discharge_kw": 70.0,
                             "soc_floor_pct": 30.0}
    # an advisory site (no edge acks what it runs) and an ack older than a plan's horizon change nothing
    sites.set_tier_allowed(sid, "advisory_writes", "t")
    assert "achieved" not in _problem_of(sid)
    sites.set_tier_allowed(sid, "closed_loop", "t")
    assert "achieved" not in _problem_of(sid, day=NOV + timedelta(days=3))


# ------------------------------------------------------------------------------------------------ followup:compare-end-value (engine-plans-live-10)

def test_compare_with_native_values_the_energy_each_arm_holds_at_the_end(world, monkeypatch):
    """The drawer priced the energy each arm bought but not what it left in the battery, so a plan that keeps energy for later (a reserve, the next
    day's peak) looked dearer than native by all of it (the Lahore demo). Each arm now carries what it holds at the horizon's end and its value at
    the plan's own terminal price (eta_d x c_next less the wear of discharging it), as the ledger values end energy."""
    from gridlytics import optimise
    from test_fix_r7_delivery_twin import _compare_ready, _reserve_plan
    steps, comp = _reserve_plan()
    plan = {"id": "p1", "slots": comp, "steps": steps, "summary": {}}
    monkeypatch.setattr(plans, "current", lambda site_id, *x, **k: plan)
    _compare_ready(world)
    r = world.c["admin"].get(f"/api/sites/{world.site}/plan/compare").json()
    nat, arm = r["compare"]["native"], r["compare"]["plan"]
    with world.use():
        b = sites.full(world.site)["assets"]["battery"]
    v_end = 0.95 * 30.0 - optimise.wear_cost(b)                                  # no end price stored: the horizon's dearest import price, Rs 30
    assert r["end_value_rs_per_kwh"] == pytest.approx(v_end, abs=0.01)
    assert arm["end_kwh"] == pytest.approx(120.0, abs=1.0) and arm["end_kwh"] > nat["end_kwh"]
    assert arm["end_value_rs"] == pytest.approx(arm["end_kwh"] * v_end, abs=1.0)
    # the plan bought 160 kWh at Rs 30 and kept 120 in the battery; native spent 80 of them on the load: the gap the drawer showed (all of it the
    # plan's import) narrows by what the plan keeps at the end, and with the outage risk the plan is the cheaper
    gap = arm["energy_rs"] - nat["energy_rs"]
    net_gap = (arm["energy_rs"] - arm["end_value_rs"]) - (nat["energy_rs"] - nat["end_value_rs"])
    assert gap > 0 and net_gap == pytest.approx(gap - (arm["end_kwh"] - nat["end_kwh"]) * v_end, abs=2.0) and net_gap < 0.5 * gap
    assert (arm["energy_rs"] + arm["reserve_short_rs"] - arm["end_value_rs"]) < (nat["energy_rs"] + nat["reserve_short_rs"] - nat["end_value_rs"])
    # the plan's own end price, where it stored one
    plan["summary"] = {"end_price": {"c_next_rs_per_kwh": 50.0, "basis": "import"}}
    r2 = world.c["admin"].get(f"/api/sites/{world.site}/plan/compare").json()
    assert r2["end_value_rs_per_kwh"] == pytest.approx(0.95 * 50.0 - optimise.wear_cost(b), abs=0.01)
    viewer = world.c["viewer"].get(f"/api/sites/{world.site}/plan/compare").json()
    assert viewer["compare"]["plan"]["end_kwh"] == arm["end_kwh"] and "end_value" not in str(viewer)


# ------------------------------------------------------------------------------------------------ engine-plans-live-18: charging from the genset

def _farm(brand="voltronic"):
    s = sites.create({"name": f"Farm {brand}", "lat": 28.42, "lon": 70.3, "site_type": "farm", "genset_start_control": "manual",
                      "outage_load_mode": 1, "critical_load_kw": [8.0] * 24, "off_grid": True}, "t")
    sid = s["id"]
    with store.connect() as con:
        r = con.execute("SELECT data FROM sites WHERE id = ?", (sid,)).fetchone()
        con.execute("UPDATE sites SET data = ? WHERE id = ?", (json.dumps({**json.loads(r["data"]), "load_peak_kw": 42}), sid))
    sites.put_asset(sid, "pv", {"kwp_dc": 80, "kw_ac": 70}, "t")
    sites.put_asset(sid, "inverter", {"brand": brand, "model": "test", "kw_ac": 66}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 120, "p_ch_kw": 40, "p_dis_kw": 40, "soc_min": 0.15}, "t")
    sites.put_asset(sid, "genset", {"rated_kw": 48, "f_min": 0.3, "fuel_kind": "hsd", "min_up_h": 1.0, "tank_l": 400}, "t")
    sites.set_tier_capable(sid, "advisory_writes")
    sites.set_tier_allowed(sid, "advisory_writes", "t")
    return sid


def _farm_day(sid, brand):
    """A day's plan at the farm from 40 %, its table compiled for `brand`, and the twin's replay of that table on the plan's own forecast."""
    from gridlytics import delivery, optimise
    site = sites.full(sid)
    site["state"] = {**site.get("state", {}), "soc": 0.4}
    t0 = twin.day_start_utc(NOV)
    bundle = forecast.issue(site, t0, save=False)
    p = plans.build_problem(site, t0, bundle, 96)
    res = optimise.solve(p, time_limit=30.0)
    steps = plans._steps_out(p, res, bundle)
    comp = delivery.compile_mode_a(steps, brand, p["battery"], 20.0, site=site)
    ts = np.repeat(p["ts"], 15) + np.tile(np.arange(15) * 60, p["T"])
    series = {"pv": np.repeat(p["PV"], 15), "load": np.repeat(p["L"], 15), "crit": np.repeat(p["Lc"], 15), "grid": np.repeat(p["g"], 15), "ts": ts,
              "temp": np.full(len(ts), 30.0)}
    sim = twin.simulate(series, twin.slots_policy([x for d in comp["days"] for x in d["slots"]], ts), twin.plant_of(site), 0.4)
    return p, steps, comp, sim


def test_a_brand_that_can_charge_from_the_genset_is_planned_compiled_and_replayed_doing_it(company):
    """The off-grid farm's set could only follow the load (GEN_CHARGE_SUPPORTED False for every brand): it ran 11 to 20 hours a day near 38 % load.
    A Voltronic/Axpert's charger source priority (SNU) and a Deye program's Gen charge flag let the set charge the battery: the plan counts on it
    for those brands only (a_gen), the compiled slot carries it, the vendor table keys it, and the twin runs the set to charge while it runs, so the
    replayed table does what the plan planned, with fewer run hours than a set that only follows the load."""
    from gridlytics import delivery
    assert delivery.gen_charge_supported("voltronic") and delivery.gen_charge_supported("deye")
    assert not delivery.gen_charge_supported("sungrow") and not delivery.gen_charge_supported("other") and delivery.GEN_CHARGE_SUPPORTED is False
    volt = _farm("voltronic")
    p, steps, comp, sim = _farm_day(volt, "voltronic")
    assert p["battery"]["a_gen"] is True
    assert any(delivery.gen_charging(st) for st in steps)
    gen_slots = [s for d in comp["days"] for s in d["slots"] if s["gen_charge"]]
    assert gen_slots
    rows = delivery.vendor_table(comp)["rows"]
    assert any(r["charger_priority"] == "SNU" for r in rows)                      # the AC input (the set) charges
    plan_hours = sum(st["genset_on"] for st in steps) / 4
    assert abs(float(sim["gen_on"].sum()) / 60 - plan_hours) <= 0.75                 # the replay runs the set as planned
    other = _farm("other")
    p2, steps2, comp2, sim2 = _farm_day(other, "other")
    assert p2["battery"]["a_gen"] is False and not any(s["gen_charge"] for d in comp2["days"] for s in d["slots"])
    assert float(sim["gen_on"].sum()) < float(sim2["gen_on"].sum())                  # fewer run hours than following the load
    assert float(sim["unserved"].sum()) <= float(sim2["unserved"].sum()) + 1e-6


def test_the_twin_charges_from_a_running_set_only_in_a_gen_charge_slot():
    t0 = twin.day_start_utc(NOV)
    n = 120
    ts = t0 + np.arange(n) * 60
    series = {"ts": ts, "pv": np.zeros(n), "load": np.full(n, 10.0), "crit": np.full(n, 10.0), "grid": np.zeros(n), "temp": np.full(n, 30.0)}
    plant = twin.Plant(e_nom=120.0, p_ch=40.0, p_dis=40.0, soc_min=0.15, g_max=0.0, gen_kw=48.0, gen_fmin=0.3, gen_control="manual")
    slot = {"start": "00:00", "end": "02:00", "start_ts": int(t0), "end_ts": int(t0) + 7200, "mode": "self_use", "soc_floor_pct": 20.0,
            "soc_target_pct": 90.0, "max_discharge_w": None, "max_charge_w": None, "grid_charge": False, "grid_cap_w": None, "genset": True,
            "genset_runs": [[int(t0), int(t0) + 7200]]}
    follow = twin.simulate(series, twin.slots_policy([{**slot, "gen_charge": False}], ts), plant, 0.3)
    charge = twin.simulate(series, twin.slots_policy([{**slot, "gen_charge": True}], ts), plant, 0.3)
    assert follow["gen"][30:].max() == pytest.approx(48.0 * 0.3, abs=0.01)          # the set follows a 10 kW load at its minimum
    assert charge["gen"][30:].max() == pytest.approx(48.0, abs=0.01)                # at its rating: 10 kW of load, 38 kW into the battery
    assert charge["soc"][-1] > follow["soc"][-1] + 0.2
