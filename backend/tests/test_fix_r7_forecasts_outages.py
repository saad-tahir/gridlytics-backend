"""Round-7 (audit) regression tests for the forecasts-and-outages package: forecast.py's PV fit, its scoring and accuracy summary, and the forecast page.

Each test fails on the code before its fix. Test data: synthetic telemetry built from the model's own clear sky (solar.pv_power) and fixed load and grid
rows; nothing here calls the network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import forecast, solar, store, telemetry, twin

PKT = timezone(timedelta(hours=5))
LAT, LON = 31.5, 74.3
#: 100 kWp on a 100 kW inverter at 15 degrees facing south: nothing clips in November
PLANT = {"kwp_dc": 100.0, "kw_ac": 100.0, "tilt": 15.0, "azimuth": 180.0}


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _pv_site(sid: str) -> dict:
    return {"id": sid, "data": {"lat": LAT, "lon": LON}, "assets": {"pv": PLANT}, "connections": []}


def _clear(ts) -> np.ndarray:
    ts = np.asarray(ts, dtype=np.int64)
    return solar.pv_power(ts, LAT, LON, PLANT, 1.0, solar.local_month(ts))


def _write_pv(sid: str, ts, pv_kw, curtail_kw=None) -> None:
    if curtail_kw is None:
        rows = [(sid, int(a), "edge", float(b) * 1000) for a, b in zip(ts, pv_kw)]
        telemetry.write_rows(rows, ("site_id", "ts", "source_id", "pv_power_w"))
    else:
        rows = [(sid, int(a), "edge", float(b) * 1000, float(c) * 1000) for a, b, c in zip(ts, pv_kw, curtail_kw)]
        telemetry.write_rows(rows, ("site_id", "ts", "source_id", "pv_power_w", "curtail_w"))


def _midnight(day: date) -> int:
    return twin.day_start_utc(day)


# ------------------------------------------------------------------------------------------------ engine-forecast-01

def test_the_pv_fit_reads_whole_local_days_so_every_tick_of_a_day_fits_the_same_ratio_and_fan(company):
    """15 local days of 5-minute PV, clear sky on even local days and 0.45 of it on odd days. The 14 whole days behind every tick of 9 November are the
    same alternating history, so midnight, 09:00, 12:00 and 15:00 all fit a ratio of 1.0 and the fan 0.45 / 0.45 / 0.725 / 1.0 of clear sky at
    tomorrow's noon. v1 sliced 24-hour blocks from the tick's own time of day, mixing one day's afternoon with the next day's morning: at 12:00 it fitted
    0.742 with the whole fan within 2.5 % of the median."""
    sid = "pv-days"
    site = _pv_site(sid)
    day0 = datetime(2026, 11, 9, tzinfo=PKT)
    ts = np.arange(int((day0 - timedelta(days=15)).timestamp()), int((day0 + timedelta(hours=15)).timestamp()), 300, dtype=np.int64)
    scale = np.where(((ts + 5 * 3600) // 86400) % 2 == 0, 1.0, 0.45)
    _write_pv(sid, ts, _clear(ts) * scale)
    noon = int((day0 + timedelta(days=1, hours=12)).timestamp())
    seen = {}
    for hour in (0, 9, 12, 15):
        t0 = int((day0 + timedelta(hours=hour)).timestamp())
        f = forecast.pv(site, t0)
        i = (noon - t0) // forecast.STEP_S
        cs = float(f["cs"][i])
        seen[hour] = (f["stage"], f["days"], round(f["pr"], 3), *(round(float(f["q"][q][i]) / cs, 3) for q in ("q05", "q25", "q50", "q95")))
    for hour, (stage, days, pr, q05, q25, q50, q95) in seen.items():
        assert stage == 1 and days == 14, seen
        assert pr == pytest.approx(1.0, abs=0.01), seen
        assert (q05, q25, q50, q95) == pytest.approx((0.45, 0.45, 0.725, 1.0), abs=0.02), seen


# ------------------------------------------------------------------------------------------------ engine-forecast-04

def test_the_pv_fit_and_score_read_available_power_not_curtailed_output(company):
    """A zero-export plant on 15 clear days curtails 40 % of its output from 11:00 to 14:00 once the battery is full. Its fit reads the PV it could
    deliver (output plus curtail_w), so it fits a ratio of 1.0, and the day's score of a clear-sky forecast is near zero. v1 read the curtailed output:
    a ratio near 0.8 on a healthy plant, and the curtailed hours scored as forecast error."""
    sid = "pv-curtail"
    site = _pv_site(sid)
    day = date(2026, 11, 9)
    t0 = _midnight(day)
    ts = np.arange(t0 - 14 * 86400, t0 + 86400, 60, dtype=np.int64)
    avail = _clear(ts)
    hour = ((ts + 5 * 3600) % 86400) // 3600
    cut = np.where((hour >= 11) & (hour < 14), 0.4 * avail, 0.0)
    _write_pv(sid, ts, avail - cut, cut)
    f = forecast.pv(site, t0)
    assert f["stage"] == 1 and f["pr"] == pytest.approx(1.0, abs=0.01), f["pr"]
    q50 = forecast._mean15(_clear, forecast.step_times(t0))
    telemetry.save_forecast(sid, "pv", t0, 1, "v1.0", {"cs": [round(float(x), 3) for x in q50], "q50": [round(float(x), 3) for x in q50]})
    r = next(x for x in forecast.score_day(site, day) if x["kind"] == "pv")
    assert r["value"] < 0.01, r


