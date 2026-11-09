"""Regression tests for the data group's review findings: telemetry ingest and readers (telemetry.py) and the forecasters (forecast.py)."""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import forecast, sites, solar, store, telemetry, twin

PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _site(name="S"):
    return sites.create({"name": name}, "t")["id"]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def _grid_site(sid: str, **assets) -> dict:
    return {"id": sid, "data": {"disco": "LESCO", "lat": 31.5, "lon": 74.3}, "connections": [{"id": "c1", "data": {"regime": "none"}}],
            "assets": assets}


# ------------------------------------------------------------------------------------------------ security-inf-telemetry-crashes-pages

def test_non_finite_values_are_stored_as_missing_and_every_reader_renders(company):
    sid = _site()
    t = int(datetime(2026, 11, 2, 10, 0, tzinfo=PKT).timestamp())
    res = telemetry.import_csv(sid, _csv(["ts", "grid_present", "pv_power_w", "load_power_w"],
                                         [[_iso(t), 1, "inf", 10000], [_iso(t + 60), 1, "-Infinity", "nan"]]))
    assert res["rows"] == 2
    telemetry.ingest(sid, [{"ts": t + 120, "grid_present": 1, "pv_power_w": float("inf"), "load_power_w": float("nan")},
                           {"ts": float("inf"), "grid_present": 1}], "edge")  # an infinite timestamp is skipped, not a 500
    last = telemetry.latest(sid)
    assert last["ts"] == t + 120 and last["pv_power_w"] is None and last["load_power_w"] is None
    json.dumps(last, allow_nan=False)
    g = telemetry.minute_grid(sid, t, t + 180)
    assert np.isnan(g["pv"]).all()
    # a value stored before ingest refused them reads as missing everywhere
    telemetry.write_rows([(sid, t + 180, "edge", 1.0, float("inf"))], ("site_id", "ts", "source_id", "grid_present", "pv_power_w"))
    json.dumps(telemetry.latest(sid), allow_nan=False)
    json.dumps(telemetry.series(sid, t, t + 240, ["pv_power_w"], "1m"), allow_nan=False)
    day = telemetry.aggregate_day(sid, date(2026, 11, 2))
    json.dumps(day, allow_nan=False)
    json.dumps(telemetry.days(sid, "2026-11-02", "2026-11-02"), allow_nan=False)
    assert "Infinity" not in telemetry.export_csv(sid, t, t + 240)


# ------------------------------------------------------------------------------------------------ the CSV export (two findings)

def test_the_export_quote_prefixes_text_cells_a_spreadsheet_would_run(company):
    sid = _site()
    t = int(datetime(2026, 11, 2, 10, 0, tzinfo=PKT).timestamp())
    evil = ['=HYPERLINK("http://x.test";"y")', "@SUM(1+9)", "+1+2", "-3+4", "\tx"]
    telemetry.ingest(sid, [{"ts": t + 60 * i, "grid_present": 1, "inverter_mode_active": v, "battery_power_w": -1500} for i, v in enumerate(evil)]
                     + [{"ts": t + 600, "grid_present": 1, "inverter_mode_active": "self_use"}], "csv")
    rows = list(csv.reader(io.StringIO(telemetry.export_csv(sid, t, t + 3600))))
    col = rows[0].index("inverter_mode_active")
    modes = [r[col] for r in rows[1:]]
    assert modes[:5] == ["'" + v for v in evil]
    assert modes[5] == "self_use"
    assert rows[1][rows[0].index("battery_power_w")] == "-1500.0"  # numbers are not text cells


# ------------------------------------------------------------------------------------------------ data-page-crash-current-minute

def test_minute_grid_and_the_forecast_page_take_a_bound_inside_a_minute(company):
    sid = _site()
    t0 = int(datetime(2026, 11, 2, 10, 0, tzinfo=PKT).timestamp())
    telemetry.ingest(sid, [{"ts": t0 + 60 * i, "grid_present": 1, "load_power_w": 1000} for i in range(4)], "edge")
    g = telemetry.minute_grid(sid, t0, t0 + 3 * 60 + 30)  # the row at t0 + 180 lies inside the partial last minute
    assert len(g["ts"]) == 3 and len(g["present"]) == 3 and g["present"].all()
    # the page passes "now": the latest issue at the quarter and a row already in the current minute
    now = int(datetime.now(timezone.utc).timestamp())
    bt = forecast.align(now)
    telemetry.ingest(sid, [{"ts": m, "grid_present": 0, "load_power_w": 2000, "pv_power_w": 0} for m in range(bt, now // 60 * 60 + 60, 60)], "edge")
    for kind in ("load", "outage", "pv"):
        telemetry.save_forecast(sid, kind, bt, 1, "v1.0", {"q50": [1.0] * 192, "p_down": [0.1] * 192})
        out = forecast.page({"id": sid}, kind)
        assert out["belief_time"] == bt and isinstance(out["actual"], list)


# ------------------------------------------------------------------------------------------------ data-energy-soc-columns-missing

def test_a_power_only_source_with_a_bms_reading_gets_import_peak_and_soc(company):
    sid = _site()
    t0 = int(datetime(2026, 11, 2, tzinfo=PKT).timestamp())
    rows = []
    for i in range(1440):
        down = 600 <= i < 660  # one hour of outage from 10:00
        rows.append([_iso(t0 + 60 * i), 0 if down else 1, 0 if down else 50000, 70 - i * 0.01])
    res = telemetry.import_csv(sid, _csv(["ts", "grid_present", "grid_power_w", "battery_soc_bms_pct"], rows))
    assert any("grid_power_w" in line for line in res["log"])
    day = telemetry.days(sid, "2026-11-02", "2026-11-02")[0]
    assert day["import_kwh"] == pytest.approx(50 * 23, rel=1e-3)
    assert day["export_kwh"] == 0 and day["peak_kw"] == pytest.approx(50.0)
    assert day["import_completeness"] == 1.0
    assert day["min_soc"] is not None and day["max_soc"] == pytest.approx(70.0)
    g = telemetry.minute_grid(sid, t0, t0 + 86400)
    assert not np.isnan(g["soc"]).any() and np.isnan(g["soc_est"]).all()
    outs = telemetry.outages(sid, t0, t0 + 86400)
    assert len(outs) == 1 and outs[0]["soc_start_pct"] == pytest.approx(64.0, abs=0.1)
    # export from a negative grid power; an energy column present wins over the power
    t1 = t0 + 86400
    telemetry.ingest(sid, [{"ts": t1, "grid_present": 1, "grid_power_w": -6000},
                           {"ts": t1 + 60, "grid_present": 1, "grid_power_w": 6000, "grid_import_wh": 50, "grid_export_wh": 0}], "edge")
    g = telemetry.minute_grid(sid, t1, t1 + 120)
    assert g["imp"].tolist() == pytest.approx([0.0, 3.0]) and g["exp"].tolist() == pytest.approx([6.0, 0.0])


def test_a_source_without_any_grid_energy_reports_its_import_as_incomplete(company):
    sid = _site()
    t0 = int(datetime(2026, 11, 2, tzinfo=PKT).timestamp())
    res = telemetry.import_csv(sid, _csv(["ts", "grid_present", "pv_power_w"], [[_iso(t0 + 60 * i), 1, 1000] for i in range(1440)]))
    assert any("missing" in line for line in res["log"])
    day = telemetry.days(sid, "2026-11-02", "2026-11-02")[0]
    assert day["completeness"] == 1.0 and day["import_completeness"] == 0.0


# ------------------------------------------------------------------------------------------------ data-load-censored-medians

def test_the_load_forecast_leaves_out_outage_minutes_where_only_backup_was_served(company):
    sid = "load-site"
    site = {"id": sid, "data": {"load_peak_kw": 100.0}, "assets": {}, "connections": []}
    t0 = int(datetime(2026, 11, 23, tzinfo=PKT).timestamp())  # a Monday
    h0 = t0 - 21 * 86400
    rows = []
    for i in range(21 * 1440):
        ts = h0 + 60 * i
        day, minute = i // 1440, i % 1440
        shed = day % 3 != 0 and 14 * 60 <= minute < 16 * 60  # two days in three: shedding 14:00-16:00 with only 30 kW of backed-up load lit
        rows.append((sid, ts, "edge", 0.0 if shed else 1.0, 30000.0 if shed else 100000.0))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "load_power_w"))
    q50 = forecast.load(site, t0)["q"]["q50"]
    t = forecast.step_times(t0)
    for hour in (13, 14, 15, 16):
        i = int(np.where(((t + 5 * 3600) % 86400) // 900 == hour * 4)[0][0])
        assert q50[i] == pytest.approx(100.0, rel=0.02), hour
    # the drop rule itself: a fully backed-up outage (no drop) stays in, a shed one is censored
    g = {"load": np.array([100, 100, 100, 100, 95, 98, 100, 100, 100, 30, 30, 30, 100], float),
         "grid": np.array([1, 1, 1, 1, 0, 0, 1, 1, 1, 0, 0, 0, 1], float)}
    cen = forecast.censored_minutes({"assets": {}}, g)
    assert cen.tolist() == [False] * 9 + [True] * 3 + [False]


# ------------------------------------------------------------------------------------------------ data-voltage-nan-false-outage

def test_an_unreadable_voltage_is_unknown_not_an_outage_and_does_not_refuse_the_file(company):
    sid = _site()
    t0 = int(datetime(2026, 11, 2, 10, 0, tzinfo=PKT).timestamp())
    volts = ["230", "NaN", "nan", "N/A", " ", "231"]
    res = telemetry.import_csv(sid, _csv(["ts", "grid_voltage_v", "pv_power_w"], [[_iso(t0 + 60 * i), v, 1000] for i, v in enumerate(volts)]))
    assert res["rows"] == 6
    assert any("not finite numbers" in line for line in res["log"])
    d = telemetry.raw(sid, t0, t0 + 600, ("grid_present", "grid_voltage_v"))
    gp = d["grid_present"]
    assert gp[0] == 1 and gp[5] == 1 and np.isnan(gp[1:5]).all()
    assert telemetry.outages(sid, t0, t0 + 600) == []
    assert telemetry.ingest(sid, [{"ts": t0 + 600, "grid_voltage_v": "N/A"}, {"ts": t0 + 660, "grid_voltage_v": 120}], "edge")["accepted"] == 2
    assert telemetry.latest(sid)["grid_present"] == 0.0


# ------------------------------------------------------------------------------------------------ data-heatmap-weekday-shift

def test_the_heatmap_rows_carry_their_own_weekday(company):
    sid = _site()
    today = datetime.now(PKT).date()
    monday = today - timedelta(days=today.weekday() or 7)  # the latest Monday before today
    t = int(datetime(monday.year, monday.month, monday.day, 10, 0, tzinfo=PKT).timestamp())
    telemetry.ingest(sid, [{"ts": t + 60 * i, "grid_present": 1, "load_power_w": 5000} for i in range(30)], "edge")
    hm = telemetry.heatmap(sid, "load_power_w", 28)
    assert hm["days"][0] == "Mon"
    assert hm["values"][0][10] == 5000
    assert all(v is None for row in hm["values"][1:] for v in row)


# ------------------------------------------------------------------------------------------------ data-outage-unseen-until-next-quarter

def test_a_grid_loss_inside_the_quarter_is_seen_by_an_issue_at_the_actual_time(company):
    sid = "outage-site"
    site = _grid_site(sid)
    t0 = int(datetime(2026, 11, 2, 10, 0, tzinfo=PKT).timestamp())
    telemetry.ingest(sid, [{"ts": t0 + 60 * i, "grid_present": 0 if i >= 3 else 1} for i in range(-10, 12)], "edge")
    now = t0 + 750  # 10:12:30, grid down since 10:03
    b = forecast.issue(site, now, save=False)
    assert b["t0"] == t0
    obs = b["outage"]["observed"]
    assert obs is not None and obs["since"] == t0 + 180 and obs["elapsed_min"] == 9
    assert b["outage"]["g"][0] == 0 and b["outage"]["p_down"][0] == 1.0
    # an explicit issue time does the same for a caller that passes the quarter
    assert forecast.issue(site, t0, save=False, now=now)["outage"]["observed"]["since"] == t0 + 180
    # history alone (an issue at the quarter itself) still reads only what was known then
    assert forecast.outage(site, t0)["observed"] is None


# ------------------------------------------------------------------------------------------------ data-pv-quantile-crossing

def test_the_pv_smart_persistence_blend_keeps_the_quantiles_sorted(company):
    sid = "pv-site"
    pv_asset = {"kwp_dc": 150.0, "kw_ac": 120.0, "tilt": 15.0, "azimuth": 180.0}
    site = {"id": sid, "data": {"lat": 31.5, "lon": 74.3}, "assets": {"pv": pv_asset}, "connections": []}
    t0 = int(datetime(2026, 11, 16, 11, 0, tzinfo=PKT).timestamp())
    h0 = t0 - 14 * 86400
    ts = np.arange(h0, t0, 60, dtype=np.int64)
    cs = solar.pv_power(ts, 31.5, 74.3, pv_asset, 1.0, solar.local_month(ts))
    day = (ts - h0) // 86400
    k = np.where(day % 2 == 0, 0.35, 0.95)
    k = np.where(ts >= t0 - 3600, 1.0, k)  # a clear last hour
    pv = cs * k
    telemetry.write_rows([(sid, int(a), "edge", 1.0, float(b) * 1000) for a, b in zip(ts, pv)],
                         ("site_id", "ts", "source_id", "grid_present", "pv_power_w"))
    out = forecast.pv(site, t0)
    assert out["stage"] == 1
    qa = np.vstack([out["q"][n] for n in forecast.QS])
    assert np.all(np.diff(qa, axis=0) >= -1e-9)
    assert np.all(qa <= out["cs"] + 1e-9)


# ------------------------------------------------------------------------------------------------ data-csv-short-row-500

def test_a_truncated_csv_row_is_skipped_and_counted(company):
    sid = _site()
    t0 = int(datetime(2026, 11, 2, 10, 0, tzinfo=PKT).timestamp())
    content = f"grid_present,pv_power_w,ts\n1,100,{_iso(t0)}\n1,120,not-a-time\n1,130\n".encode()
    res = telemetry.import_csv(sid, content)
    assert res["rows"] == 1 and res["skipped"] == 2
    assert any("2 rows without a readable ts" in line for line in res["log"])


# ------------------------------------------------------------------------------------------------ data-outage-score-hindsight-baseline

def test_the_outage_score_skips_dark_slots_and_uses_the_schedule_only_baseline(company):
    sid = "score-site"
    site = _grid_site(sid)
    day = date(2026, 11, 2)
    t0 = twin.day_start_utc(day)
    # the first 12 hours seen with the grid down throughout; the logger dark for the rest of the day
    telemetry.write_rows([(sid, t0 + 60 * i, "edge", 0.0) for i in range(720)], ("site_id", "ts", "source_id", "grid_present"))
    telemetry.save_forecast(sid, "outage", t0, 1, "v1.0", {"p_down": [0.9] * 192})
    rows = forecast.score_day(site, day)
    o = next(r for r in rows if r["kind"] == "outage")
    assert o["value"] == pytest.approx(0.01)  # 48 known slots, all down, forecast 0.9
    assert o["slots_scored"] == 48
    assert o["baseline_value"] == pytest.approx(1.0)  # schedule-only, with no schedule: never down
    assert "climatology_value" not in o  # no history before the day
    with store.connect() as con:
        r = con.execute("SELECT value, baseline_value FROM forecast_scores WHERE site_id = ? AND kind = 'outage'", (sid,)).fetchone()
    assert r["value"] == pytest.approx(0.01) and r["baseline_value"] == pytest.approx(1.0)


def test_the_outage_climatology_baseline_reads_only_the_days_before(company):
    sid = "clim-site"
    site = _grid_site(sid)
    day = date(2026, 11, 2)
    t0 = twin.day_start_utc(day)
    # 28 days before: the grid down 18:00-19:00 local every day; up otherwise
    h0 = t0 - 28 * 86400
    hist = [(sid, h0 + 60 * i, "edge", 0.0 if 18 * 60 <= i % 1440 < 19 * 60 else 1.0) for i in range(28 * 1440)]
    today = [(sid, t0 + 60 * i, "edge", 1.0) for i in range(1440)]
    telemetry.write_rows(hist + today, ("site_id", "ts", "source_id", "grid_present"))
    telemetry.save_forecast(sid, "outage", t0, 1, "v1.0", {"p_down": [0.0] * 192})
    o = next(r for r in forecast.score_day(site, day) if r["kind"] == "outage")
    assert o["value"] == 0.0 and o["baseline_value"] == 0.0
    assert o["climatology_value"] == pytest.approx(4 / 96, abs=1e-4)  # the 18:00 hour's four slots predicted down, the day stayed up


def test_the_schedule_only_baseline_follows_the_announced_windows_and_their_adherence(company):
    sid = "sched-site"
    site = _grid_site(sid)
    day = date(2026, 11, 2)
    t0 = twin.day_start_utc(day)
    forecast.set_schedule(sid, [["14:00", "16:00", "daily"]], "manual", "t")
    h0 = t0 - 28 * 86400
    rows = [(sid, h0 + 60 * i, "edge", 0.0 if 14 * 60 <= i % 1440 < 16 * 60 else 1.0) for i in range(29 * 1440)]  # the day itself included
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present"))
    telemetry.save_forecast(sid, "outage", t0, 1, "v1.0", {"p_down": [0.0] * 192})
    o = next(r for r in forecast.score_day(site, day) if r["kind"] == "outage")
    adherence = (8 + 28) / (10 + 28)
    assert o["value"] == pytest.approx(8 / 96, abs=1e-4)
    assert o["baseline_value"] == pytest.approx(8 * (1 - adherence) ** 2 / 96, abs=1e-4)


def test_the_pv_nrmse_is_normalised_by_the_ac_nameplate(company):
    sid = "pv-score"
    pv_asset = {"kwp_dc": 100.0, "kw_ac": 80.0, "tilt": 15.0, "azimuth": 180.0}
    site = {"id": sid, "data": {"lat": 31.5, "lon": 74.3}, "assets": {"pv": pv_asset}, "connections": []}
    day = date(2026, 11, 2)
    t0 = twin.day_start_utc(day)
    telemetry.write_rows([(sid, t0 + 60 * i, "edge", 1.0, 5000.0) for i in range(1440)], ("site_id", "ts", "source_id", "grid_present", "pv_power_w"))
    telemetry.save_forecast(sid, "pv", t0, 1, "v1.0", {"cs": [50.0] * 192, "q50": [10.0] * 192})
    r = next(x for x in forecast.score_day(site, day) if x["kind"] == "pv")
    assert r["value"] == pytest.approx(5 / 80, rel=1e-3)  # an overcast day: 5 kW RMSE on an 80 kW plant, not 1.0
