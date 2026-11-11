"""Round-7 regression tests for the cross-module engine (audit work package "crossmodule", stage 2, batch 1): the changes that need several
stage-1 packages' code at once.

- tracker-and-docs-01 (with pages-D-09): a site's connections are all priced where they are metered: the connection the plant and the site's
  meter readings are on (`plant`) is the one the plan, the ledger and the bill rebuild price, and a bill names the connection it is for.

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
