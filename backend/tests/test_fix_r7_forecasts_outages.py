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


# ------------------------------------------------------------------------------------------------ engine-forecast-03

def test_with_three_to_six_fitted_days_the_clearness_quantiles_move_with_the_site_s_days(company):
    """With 3 to 6 fitted days the quantiles are the climatological prior scaled towards the site's own median clearness (weight: days / 7). v1 scaled
    the prior by median(kn) / median(kn), which is 1, so a site's first week ran on the prior whatever its days looked like."""
    t0 = _midnight(date(2026, 11, 9))
    prior_median = float(forecast.K_PRIOR[3] * forecast.MONTH_K[11] / 0.86)
    shares = {}
    for sid, scales in (("pv-overcast", (1.0, 0.45, 0.45, 0.45)), ("pv-clear", (1.0, 1.0, 1.0, 1.0))):
        ts = np.arange(t0 - 4 * 86400, t0, 300, dtype=np.int64)
        day = (ts - (t0 - 4 * 86400)) // 86400
        _write_pv(sid, ts, _clear(ts) * np.asarray(scales)[day])
        f = forecast.pv(_pv_site(sid), t0)
        assert f["stage"] == 1 and f["days"] == 4
        i = 36 * 4  # tomorrow's noon, past the smart-persistence blend
        shares[sid] = float(f["q"]["q50"][i] / (f["cs"][i] * f["pr"]))
    assert shares["pv-overcast"] < prior_median - 0.1, (shares, prior_median)
    assert shares["pv-clear"] > prior_median + 0.03, (shares, prior_median)


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


# ------------------------------------------------------------------------------------------------ pages-C-22, engine-forecast-07

def test_pv_scores_carry_a_smart_persistence_baseline_and_every_horizon_class(company):
    """Plan Part 5.5: PV skill against smart persistence per horizon class. The scored day runs at 0.8 of clear sky, the day before at 0.5 and the one
    before that at 0.6; hourly issues forecast 0.8 of clear sky. The day-ahead row (the midnight issue) carries the smart-persistence nRMSE of the same
    slots as its baseline, and the 0-2, 2-6, 6-24 and 24-48 hour classes each get a row whose baseline is the persistence known at the issue time (the
    last hour's clearness, the same hours yesterday, the same hours two days before). v1 wrote one day-ahead row with no PV baseline at all."""
    sid = "pv-skill"
    site = _pv_site(sid)
    day = date(2026, 11, 9)
    t0 = _midnight(day)
    ts = np.arange(t0 - 3 * 86400, t0 + 86400, 60, dtype=np.int64)
    scale = np.array([0.6, 0.6, 0.5, 0.8])[(ts - (t0 - 3 * 86400)) // 86400]
    _write_pv(sid, ts, _clear(ts) * scale)
    for b in range(t0 - 2 * 86400 + 3600, t0 + 86400, 3600):
        q50 = 0.8 * forecast._mean15(_clear, forecast.step_times(b))
        telemetry.save_forecast(sid, "pv", b, 1, "v1.0", {"q50": [round(float(x), 3) for x in q50]})
    rows = [r for r in forecast.score_day(site, day) if r["kind"] == "pv"]
    assert rows[0]["horizon_class"] == "day_ahead"  # alerts.nightly reads the first PV row as the day's headline
    by = {r["horizon_class"]: r for r in rows}
    assert set(by) == {"day_ahead", "0-2h", "2-6h", "6-24h", "24-48h"}, sorted(by)
    head = by["day_ahead"]
    assert head["value"] < 0.01 and head["baseline_value"] > 0.05, head
    for cls in ("2-6h", "6-24h", "24-48h"):
        assert by[cls]["value"] < 0.01 and by[cls]["baseline_value"] > 0.03, by[cls]
    # yesterday was 0.3 of clear sky away from the day, the day before 0.2: each class's baseline is the persistence its issues could know
    assert by["24-48h"]["baseline_value"] == pytest.approx(by["6-24h"]["baseline_value"] * 2 / 3, rel=0.05)
    assert by["0-2h"]["baseline_value"] is not None
    with store.connect() as con:
        stored = {r["horizon_class"]: r["baseline_value"] for r in con.execute("SELECT * FROM forecast_scores WHERE site_id = ? AND kind = 'pv'", (sid,))}
    assert set(stored) == set(by) and stored["day_ahead"] == pytest.approx(head["baseline_value"])


def test_the_accuracy_summary_gives_skill_overall_and_by_horizon_class(company):
    """GET /forecasts/scores: the day rows stay the day-ahead headline (one per day and metric, as the page's table reads them), each with its skill;
    the summary gives the skill against the kind's baseline and one entry per horizon class."""
    sid = "sum-skill"
    today = datetime.now(PKT).date()
    with store.connect() as con:
        for back, v, b in ((3, 0.10, 0.20), (2, 0.12, 0.20)):
            d = (today - timedelta(days=back)).isoformat()
            con.execute("INSERT INTO forecast_scores (site_id, kind, day_local, horizon_class, metric, value, baseline_value) VALUES (?,?,?,?,?,?,?)",
                        (sid, "pv", d, "day_ahead", "nrmse", v, b))
            con.execute("INSERT INTO forecast_scores (site_id, kind, day_local, horizon_class, metric, value, baseline_value) VALUES (?,?,?,?,?,?,?)",
                        (sid, "pv", d, "6-24h", "nrmse", v / 2, b))
    s = forecast.scores(sid, 30)
    assert [r["horizon_class"] for r in s["days"]["pv"]] == ["day_ahead", "day_ahead"]
    assert s["days"]["pv"][0]["skill"] == pytest.approx(0.5)
    pv = s["summary"]["pv"]
    assert pv["baseline_kind"] == "smart_persistence"
    assert pv["last7"] == pytest.approx(0.11) and pv["baseline_last7"] == pytest.approx(0.2)
    assert pv["skill_last7"] == pytest.approx(0.45, abs=1e-3) and pv["skill_last30"] == pytest.approx(0.45, abs=1e-3)
    cls = {h["horizon_class"]: h for h in pv["horizons"]}
    assert set(cls) == {"6-24h"} and cls["6-24h"]["skill_last30"] == pytest.approx(0.725, abs=1e-3) and cls["6-24h"]["n"] == 2


# ------------------------------------------------------------------------------------------------ engine-forecast-14, role-analyst-18

def test_the_seven_day_summary_covers_the_last_seven_calendar_days(company):
    """'last 7 days' is the days scored within the last seven calendar days (Pakistan time), with their count; v1 took the last seven scored rows,
    whatever span they covered, and the page printed the 30-day count under it."""
    sid = "sum-days"
    today = datetime.now(PKT).date()
    rows = [(20 - i, 0.5) for i in range(9)] + [(3, 0.1), (1, 0.1)]  # nine old days, then a gap, then two recent days
    with store.connect() as con:
        for back, v in rows:
            con.execute("INSERT INTO forecast_scores (site_id, kind, day_local, horizon_class, metric, value, baseline_value) VALUES (?,?,?,?,?,?,?)",
                        (sid, "pv", (today - timedelta(days=back)).isoformat(), "day_ahead", "nrmse", v, None))
    pv = forecast.scores(sid, 30)["summary"]["pv"]
    assert pv["last7"] == pytest.approx(0.1), pv
    assert pv["n7"] == 2 and pv["n"] == 11
    assert pv["last30"] == pytest.approx((9 * 0.5 + 2 * 0.1) / 11, abs=1e-4)
    assert pv["skill_last7"] is None  # no baseline in the window: no skill


