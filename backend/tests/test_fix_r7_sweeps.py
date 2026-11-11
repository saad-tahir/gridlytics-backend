"""Round 7 (audit), the cross-page sweeps of stage 2 (savings-ui-crosscut batches 6-8, the "sweeps" stream).

Fuel units (frontend-states-formats-01 and its duplicates): a captive-gas or RLNG genset's fuel is counted in MMBtu (its curve and its price are per
MMBtu), so the fleet names each site's fuel and never adds MMBtu to litres of diesel, and the reports name the fuel in its own unit. An off-grid site
(role-operator-10 and its duplicate flow-reports-alerts-members-keys-30) has no grid to import from, no demand and no outage: its reports print none of
those rows. Unmetered genset fuel over a long Savings period (frontend-states-formats-02) is the curve's figure, or said not counted, never a zero
summed from blank readings."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from gridlytics import sites, store, telemetry
from gridlytics.routes_common import PKT, today
from test_fix_api import _now, world  # noqa: F401  (world is a fixture)
from test_fix_r7_alerts_reports import _baseline, _day_row, _fake_summary, _past_month, _story, _tday, _with_summary
from test_fix_r7_fleet_overview import _claimed_day, _put_days, _row


def _data_from_last_year(site_id: str) -> None:
    """The site's data starts a year back, so a past month's report may be generated (a period before a site's data is refused)."""
    with store.connect() as con:
        con.execute("INSERT OR REPLACE INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                    (site_id, (datetime.now(PKT).date() - timedelta(days=400)).isoformat(), json.dumps({"pv_kwh": 0.0})))


# ------------------------------------------------------------------------------------------------ frontend-states-formats-01 (fleet and reports)

def test_the_fleet_names_each_site_s_fuel_and_never_adds_mmbtu_of_gas_to_litres_of_diesel(world):
    a, v, sid, two = world.c["admin"], world.c["viewer"], world.site, world.other
    t = today()
    with world.use():
        sites.put_asset(two, "genset", {"rated_kw": 1000, "fuel_kind": "gas_captive"}, "test")       # a captive-gas engine: its fuel is in MMBtu
        _put_days(sid, {t.isoformat(): _claimed_day(700.0, 400.0, 520.0, 12.5)})
        _put_days(two, {t.isoformat(): _claimed_day(300.0, 100.0, 100.0, 1.9)})
    for c in (a, v):                                                       # a fuel kind is not money: every role reads it
        fleet = c.get("/api/fleet").json()
        assert _row(fleet, sid)["fuel_kind"] == "hsd" and _row(fleet, two)["fuel_kind"] == "gas_captive"
        assert _row(fleet, two)["mtd_litres_saved"] == 1.9                  # the site's own fuel saved, in its own unit
        # the summary's litres are diesel's alone (before: 14.4, the gas engine's 1.9 MMBtu added to 12.5 litres), and the gas has its own total
        assert fleet["summary"]["mtd_litres_saved"] == 12.5 and fleet["summary"]["mtd_mmbtu_saved"] == 1.9
    with world.use():
        sites.remove_asset(two, "genset", "test")
    fleet = a.get("/api/fleet").json()
    assert _row(fleet, two)["fuel_kind"] is None and fleet["summary"]["mtd_mmbtu_saved"] is None    # no genset: no fuel, never a zero


def test_the_monthly_and_fleet_reports_count_a_gas_genset_s_fuel_in_mmbtu(world, monkeypatch):
    from gridlytics import reports
    sid, month = world.site, _past_month()
    day = f"{month}-05"
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(day)], native=_baseline(litres_saved=1.9), tuned=_baseline(litres_saved=1.2)), [_tday(day)])
    built = _story(monkeypatch)
    with world.use():
        _data_from_last_year(sid)
        sites.put_asset(sid, "genset", {"rated_kw": 1000, "fuel_kind": "gas_captive"}, "test")
        reports.generate("monthly_savings", sid, month, "test")
        reports.generate("company_rollup", "", month, "test")
    monthly, rollup = built[-2], built[-1]
    assert "Gas saved (Estimated) | 1.9 MMBtu | 1.2 MMBtu" in monthly, monthly
    assert "Diesel" not in monthly and " L " not in monthly and not any(ln.endswith(" L") for ln in monthly.splitlines()), monthly
    assert "Fuel saved (Estimated)" in rollup and "1.9 MMBtu" in rollup and "Diesel" not in rollup, rollup


# ------------------------------------------------------------------------------------------------ frontend-states-formats-02

def test_unmetered_genset_fuel_over_a_savings_period_of_more_than_62_days_is_the_curve_s_or_not_counted_never_zero(world):
    a, sid = world.c["admin"], world.site
    t0 = (_now() - 2 * 86400) // 60 * 60
    with world.use():
        # an hour of the genset running at 100 kW with no fuel meter: its litres come from the fuel curve
        telemetry.ingest(sid, [{"ts": t0 + 60 * i, "grid_present": 0, "genset_running": 1, "genset_power_w": 100_000, "load_power_w": 100_000}
                               for i in range(60)], "edge")
    start = (today() - timedelta(days=90)).isoformat()
    e = a.get(f"/api/sites/{sid}/savings", params={"start": start}).json()["energy"]
    assert e["litres_source"] == "curve" and e["litres"] > 1.0, e          # before: the day rows' blank litres summed as 0.0, with no source
    short = a.get(f"/api/sites/{sid}/savings", params={"start": (today() - timedelta(days=10)).isoformat()}).json()["energy"]
    assert abs(short["litres"] - e["litres"]) < 0.05, (short, e)            # the same figure the page reads over a short period
    # a running flag with neither the power nor the fuel read would need a minute-by-minute replay, which a long period does not run: not counted
    with world.use():
        telemetry.ingest(sid, [{"ts": t0 + 3600 + 60 * i, "grid_present": 0, "genset_running": 1, "load_power_w": 80_000} for i in range(30)], "edge")
    blind = a.get(f"/api/sites/{sid}/savings", params={"start": start}).json()["energy"]
    assert blind["litres"] is None and blind["litres_source"] == "not_counted", blind
    assert a.get(f"/api/sites/{sid}/savings", params={"start": (today() - timedelta(days=10)).isoformat()}).json()["energy"]["litres_source"] == "curve"
