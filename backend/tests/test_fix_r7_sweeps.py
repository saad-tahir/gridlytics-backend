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


# ------------------------------------------------------------------------------------------------ role-operator-10, flow-reports-alerts-members-keys-30

def test_an_off_grid_site_s_energy_report_prints_no_grid_import_export_peak_or_outage_rows(world, monkeypatch):
    from gridlytics import reports
    sid, month = world.site, _past_month()
    days = [f"{month}-05", f"{month}-06"]
    # the farm's day rows: its grid reads 0 on every minute, so the telemetry counts the whole day an outage
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(d) for d in days]),
                  [_tday(d, import_kwh=0.0, export_kwh=0.0, outage_minutes=1440, peak_kw=0.0) for d in days])
    built = _story(monkeypatch)
    with world.use():
        _data_from_last_year(sid)
        conn = sites.connections(sid)[0]
        sites.put_connection(sid, {**conn["data"], "regime": "off_grid"}, "test", conn_id=conn["id"])
        rep = reports.generate("energy_only", sid, month, "test")
        csv_text = reports.file_path(rep["id"], "csv").read_text(encoding="utf-8")
    text = built[-1]
    for row in ("Grid import", "Grid export", "Outage time", "Peak 30-minute import", "Import kWh", "Outage h"):
        assert row not in text, (row, text)
    assert "48.0" not in text and "24.0" not in text, text                 # before: "Outage time 48.0 h" and "Outage h 24.0" every day
    assert "Solar" in text and "Load" in text and "no grid connection" in text.lower(), text
    # the CSV keeps its columns, the grid's left blank rather than an import of 0 and a day-long outage
    rows = csv_text.splitlines()
    head = rows[0].split(",")
    first = dict(zip(head, rows[1].split(",")))
    assert first["outage_minutes"] == "" and first["import_kwh"] == "" and first["peak_kw"] == "", first


def test_an_off_grid_site_s_savings_energy_counts_no_outage_hours_and_no_grid_peak(world, monkeypatch):
    a, v, sid = world.c["admin"], world.c["viewer"], world.site
    month = _past_month()
    days = [f"{month}-05", f"{month}-06"]
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(d) for d in days]),
                  [_tday(d, import_kwh=0.0, export_kwh=0.0, outage_minutes=1440, peak_kw=0.0) for d in days])
    params = {"start": days[0], "end": days[1]}
    on_grid = a.get(f"/api/sites/{sid}/savings", params=params).json()["energy"]
    assert on_grid["outage_hours"] == 48.0 and on_grid["peak_kw"] == 0.0            # a grid site's day rows: its outages are real
    with world.use():
        conn = sites.connections(sid)[0]
        sites.put_connection(sid, {**conn["data"], "regime": "off_grid"}, "test", conn_id=conn["id"])
    for c in (a, v):
        e = c.get(f"/api/sites/{sid}/savings", params=params).json()["energy"]
        # before: "Outage time 48 h" (every minute of a grid that reads 0) and a grid peak of 0.0 kW
        assert e["outage_hours"] is None and e["peak_kw"] is None, e
        assert e["pv_kwh"] == 1600.0 and e["load_kwh"] == 4200.0, e                   # the site's own energy stays


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


# ------------------------------------------------------------------------------------------------ role-owner-37 (jargon) and the hot_high_soc follow-up

def test_the_unconfirmed_slot_table_alert_says_why_in_plain_words(world, monkeypatch):
    """role-owner-37: the title read "tier 2 has no read-back"."""
    import re as _re
    from gridlytics import alerts
    from test_fix_r7_alerts_reports import _iso, _minutes, _ready
    sid = world.site
    now = int(datetime(2026, 11, 9, 20, 0, tzinfo=PKT).timestamp())
    _ready(monkeypatch)
    plan = {"id": "p1", "steps": [], "summary": {}, "valid_until": _iso(now + 86400).replace("Z", "+00:00")}
    monkeypatch.setattr(alerts.plans, "current", lambda site_id: plan)
    monkeypatch.setattr(alerts, "_peak_unconfirmed", lambda site, now, today: True)
    with world.use():
        sites.set_tier_capable(sid, "advisory_writes")
        sites.set_tier_allowed(sid, "advisory_writes", "test")
        telemetry.ingest(sid, _minutes(now - 90 * 60, now), "edge", now=now)
        alerts.evaluate(sid, now)
        title = {a["rule"]: a for a in alerts.listing("active", sid)}["apply_unconfirmed"]["title"]
    assert title.startswith("The slot table for Monday 9 November 2026 is not marked as applied: at the advisory tier nothing reads the inverter's "
                            "settings back"), title
    assert not _re.search(r"\btier \d\b|read-back", title), title
    assert title.endswith("until the table is marked applied on the Control page"), title


def test_the_hot_full_battery_alert_names_its_24_hours_not_the_last_day(world):
    """A follow-up of role-operator-31: the hot_high_soc title said "in the last day", still read after the alert cleared."""
    from gridlytics import battery
    sid = world.site
    end = int(datetime(2026, 11, 9, 20, 0, tzinfo=PKT).timestamp())
    with world.use():
        sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "test")
        # three hours at 95 % and 40 degrees, the minute before 20:00 the last
        rows = [(sid, end - 180 * 60 + 60 * i, "edge", 95.0, 40.0, 1) for i in range(180)]
        telemetry.write_rows(rows, ("site_id", "ts", "source_id", "battery_soc_bms_pct", "battery_temp_c", "grid_present"))
        st = battery.state(sites.full(sid), end)
    text = dict(st["alerts"])["hot_high_soc"]
    assert text == "3.0 hours above 90 percent charge above 35 degrees in the 24 hours to 9 Nov 20:00", text
    assert "last day" not in text


# ------------------------------------------------------------------------------------------------ the rest of role-analyst-32: the inbox follows a person's choices

def test_the_inbox_and_its_badge_count_only_the_kinds_of_alert_a_person_follows(world):
    from gridlytics import alerts
    a, op, sid = world.c["admin"], world.c["operator"], world.site
    with world.use():
        alerts._open(sid, "data_stale", "Data", "warning", "No data for 40 minutes", {})
        alerts._open(sid, "sigma_high", "Battery", "warning", "The battery estimate may be off", {})
        alerts._open(sid, "genset_unanswered", "Genset", "critical", "A genset request was not answered", {})
    every = op.get("/api/alerts/counts").json()
    assert every["total"] == 3 and every["following"] is None, every                  # until a person chooses, every kind
    assert op.put("/api/platform/notifications", json={"follow": ["Data", "Genset"]}).status_code == 200
    mine = op.get("/api/alerts/counts").json()
    assert mine["total"] == 2 and mine["following"] == ["Data", "Genset"], mine       # the badge counts the kinds followed
    assert mine["by_severity"] == {"warning": 1, "critical": 1} and mine["by_site"] == {sid: 2}, mine
    assert "Battery" in mine["categories"]                                          # the kinds the role receives, for the filter
    inbox = op.get("/api/alerts").json()
    assert sorted(x["category"] for x in inbox) == ["Data", "Genset"], inbox
    assert sorted(x["category"] for x in op.get("/api/alerts", params={"every": "true"}).json()) == ["Battery", "Data", "Genset"]
    # another person's choices are their own: the administrator still counts every kind
    assert a.get("/api/alerts/counts").json()["total"] == 3
    # the fleet and a site's own page are not the inbox: they show every alert the role receives
    with world.use():
        assert alerts.counts()["total"] == 3
    assert op.put("/api/platform/notifications", json={"follow": []}).status_code == 200
    assert op.get("/api/alerts/counts").json()["total"] == 0 and op.get("/api/alerts").json() == []


# ------------------------------------------------------------------------------------------------ sites-onboarding review: an off-grid source has no grid signal

def test_an_off_grid_site_activates_without_a_grid_presence_signal_and_a_grid_site_still_needs_one(world):
    a = world.c["admin"]
    with world.use():
        farm = sites.create({"name": "Farm", "site_type": "farm", "data_source": {"kind": "oem_cloud", "grid_signal_confirmed": False}}, "test")["id"]
        sites.put_asset(farm, "inverter", {"kw_ac": 50}, "test")
        mill = sites.create({"name": "Mill", "site_type": "industrial_1_shift", "data_source": {"kind": "oem_cloud", "grid_signal_confirmed": False}},
                            "test")["id"]
        sites.put_asset(mill, "inverter", {"kw_ac": 50}, "test")
    r = a.post(f"/api/sites/{farm}/activate")
    assert r.status_code == 200 and r.json()["status"] == "active", r.text      # before: "Add the data source, with its grid-presence signal confirmed"
    # a grid site's source still needs it, and an off-grid site still needs a source
    assert a.put(f"/api/sites/{mill}/connection", json={"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}).status_code == 200
    r = a.post(f"/api/sites/{mill}/activate")
    assert r.status_code == 422 and "the data source, with its grid-presence signal confirmed (tab A)" in r.json()["detail"], r.text
    with world.use():
        bare = sites.create({"name": "Bare farm", "site_type": "farm"}, "test")["id"]
        sites.put_asset(bare, "inverter", {"kw_ac": 50}, "test")
    r = a.post(f"/api/sites/{bare}/activate")
    assert r.status_code == 422 and r.json()["detail"] == "Add the data source (tab A) before activating", r.text
    # a site whose connection is of the off_grid regime is off-grid too
    with world.use():
        sites.update(mill, {"data_source": {"kind": "csv", "grid_signal_confirmed": False}}, "test")
        conn = sites.connections(mill)[0]
        sites.put_connection(mill, {**conn["data"], "regime": "off_grid"}, "test", conn_id=conn["id"])
    assert a.post(f"/api/sites/{mill}/activate").status_code == 200
