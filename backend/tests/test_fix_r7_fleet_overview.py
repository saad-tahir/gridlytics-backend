"""Round 7 (audit), the Fleet and site Overview: the month-to-date saving is the Savings page's month figure, MDI lever included, with its method and
computation date (flow-savings-pilot-bill-02, its duplicates role-owner-01, role-operator-01 and role-analyst-02, and pages-A-08), and off-grid in the
fleet is the site's own fact, so a grid site whose connection is not saved yet is not labelled Off-grid and its outage counts (pages-A-03,
role-viewer-09)."""
from __future__ import annotations

import json
from datetime import timedelta

from gridlytics import sites, store, telemetry
from gridlytics.routes_common import today
from test_fix_api import _now, world  # noqa: F401  (world is a fixture)


def _row(fleet: dict, site_id: str) -> dict:
    return next(s for s in fleet["sites"] if s["id"] == site_id)


def _day(claimed: float, peak_a: float, peak_b: float, rate: float) -> dict:
    """A scored native day as verify stores it: a claimed saving, and the day's peaks behind the month's MDI lever."""
    return {"saving_rs": claimed, "claimed_rs": claimed, "levers": {"peak_window": claimed}, "fidelity": "confirmed", "litres_saved": 0.0,
            "actual": {"cost_rs": 1000.0, "e_end_kwh": 0.0, "v_end": 0.0}, "baseline": {"cost_rs": 1000.0 + claimed, "e_end_kwh": 0.0, "v_end": 0.0},
            "peak_kw_actual": peak_a, "peak_kw_baseline": peak_b, "mdi_rate": rate, "unserved_kwh_actual": 0.0, "unserved_kwh_baseline": 0.0}


def _put_days(site_id: str, days: dict[str, dict]) -> None:
    with store.connect() as con:
        for day, data in days.items():
            con.execute("INSERT OR REPLACE INTO savings_days (site_id, day_local, baseline, data, computed) VALUES (?,?,?,?,?)",
                        (site_id, day, "native", json.dumps(data), store._now()))


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-02, pages-A-08

def test_the_month_to_date_saving_on_fleet_and_overview_is_the_savings_page_s_month_figure_with_its_mdi_lever(world):
    a, sid = world.c["admin"], world.site
    t = today()
    first = t.replace(day=1)
    with world.use():
        # two scored days this month (one when today is the 1st): a daily claim, and a month peak 40 kW under the native replay's at Rs 1,500 a kW;
        # and a day of last month, which is not this month's
        _put_days(sid, {first.isoformat(): _day(700.0, 300.0, 340.0, 1500.0), t.isoformat(): _day(512.4, 290.0, 320.0, 1500.0),
                        (first - timedelta(days=1)).isoformat(): _day(9999.0, 100.0, 900.0, 1500.0)})
    sav = a.get(f"/api/sites/{sid}/savings").json()                                     # the Savings page's "This month"
    native = sav["baselines"]["native"]
    assert native["levers"]["mdi"] > 0                                                    # the month's MDI lever is part of the claim
    month = round(native["claimed_rs"], 0)
    now = a.get(f"/api/sites/{sid}/now").json()["month"]
    fleet = a.get("/api/fleet").json()
    row = _row(fleet, sid)
    assert now["saving_rs"] == month and row["mtd_saving_rs"] == month                    # before: the daily claims alone, without the MDI lever
    assert fleet["summary"]["mtd_saving_rs"] == sum(r["mtd_saving_rs"] or 0 for r in fleet["sites"])
    assert abs(fleet["summary"]["mtd_saving_rs"] - a.get("/api/fleet/savings").json()["total_claimed_rs"]) <= 0.5 * len(fleet["sites"])
    # the method and the computation date go with the figure (plan 9.7), as the Savings page shows them
    assert now["saving_rs_method"] == row["mtd_saving_rs_method"] == fleet["summary"]["mtd_saving_rs_method"] == "counterfactual"
    assert now["saving_rs_computed"] == row["mtd_saving_rs_computed"] == fleet["summary"]["mtd_saving_rs_computed"] == sav["computed"]
    assert sav["computed"]
    # a site with no scored day this month has no figure and no date, never a zero
    other = _row(fleet, world.other)
    assert other["mtd_saving_rs"] is None and other["mtd_saving_rs_computed"] is None
    # a viewer and a key get none of it: the figure, its method or its date
    for blob in (json.dumps(world.c["viewer"].get(f"/api/sites/{sid}/now").json()), json.dumps(world.c["viewer"].get("/api/fleet").json())):
        assert "saving_rs" not in blob and "saving_rs_method" not in blob and "saving_rs_computed" not in blob


# ------------------------------------------------------------------------------------------------ pages-A-03, role-viewer-09

def test_a_grid_site_without_its_connection_yet_is_not_off_grid_in_the_fleet_and_its_outage_counts(world):
    a, two = world.c["admin"], world.other                 # Secret Site Two: no connection yet, not a farm, no off_grid flag
    assert _row(a.get("/api/fleet").json(), two)["category"] is None
    with world.use():
        telemetry.ingest(two, [{"ts": _now() // 60 * 60 - 120, "grid_present": 0, "pv_power_w": 900}], "edge")
    assert a.get("/api/fleet").json()["summary"]["in_outage"] == 1      # before: left out as "Off-grid"
    with world.use():
        farm = sites.create({"name": "Chak farm", "site_type": "farm"}, "test")["id"]
        flagged = sites.create({"name": "Island office", "site_type": "office", "off_grid": True}, "test")["id"]
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, farm)["category"] == "Off-grid" and _row(fleet, flagged)["category"] == "Off-grid"   # off-grid stays the site's own fact
    assert _row(fleet, world.site)["category"] == "B-2(b)"
    assert a.put(f"/api/sites/{two}/connection", json={"regime": "off_grid"}).status_code == 200
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, two)["category"] == "Off-grid" and fleet["summary"]["in_outage"] == 0
