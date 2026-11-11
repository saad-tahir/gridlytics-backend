"""Round-7 regression tests for the cross-module engine (audit work package "crossmodule", stage 2, batch 1): the changes that need several
stage-1 packages' code at once.

- tracker-and-docs-01 (with pages-D-09): a site's connections are all priced where they are metered: the connection the plant and the site's
  meter readings are on (`plant`) is the one the plan, the ledger and the bill rebuild price, and a bill names the connection it is for.
- engine-tariffs-09: Punjab's self-generation duty comes from the connection's province (else the site's DISCO's) and the tax version in force,
  in the plan's genset cost and in the ledger's, instead of latitude > 28 and a constant in plans.py.
- engine-savings-09 (VER-13): the metering factor per connection, one energy and one MDI scalar fitted on two bills and tested on a held-out third,
  applied to both arms before pricing; outside 0.97 to 1.03 a data finding raised as an alert; the 2 % gate closed only by a held-out bill.
- followup bound-mdi (engine-optimiser-14): the perfect-foresight bound prices both arms' demand charge from the solve's own lower bound.

Each test fails on the code before its fix."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import forecast, plans, sites, store, tariffs, twin, verify

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
