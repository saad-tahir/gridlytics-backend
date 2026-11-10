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


# ------------------------------------------------------------------------------------------------ pages-C-01

def test_the_forecast_page_carries_the_past_24_hours_of_actuals_and_the_issue_held_then(company):
    """The tick re-issues every 15 minutes, so the latest issue's own actuals are 0 or 1 step. The page also answers the 24 hours before the belief
    time: their 15-minute actuals (available PV: output plus curtailment) and the issue held a day before, so the chart shows how the forecast tracked."""
    sid = "page-past"
    site = {"id": sid, "data": {"lat": LAT, "lon": LON}, "assets": {"pv": PLANT}, "connections": [{"id": "c1", "data": {"regime": "none"}}]}
    now = int(datetime.now(timezone.utc).timestamp())
    b = forecast.align(now)
    h0 = b - 86400
    ts = np.arange(h0 - 3600, now // 60 * 60, 60, dtype=np.int64)
    down = (ts >= h0 + 6 * 3600) & (ts < h0 + 7 * 3600)  # one hour without grid, 18 hours before the belief time
    rows = [(sid, int(t), "edge", 2000.0, 0.0 if d else 1.0, 3000.0, 1000.0) for t, d in zip(ts, down)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "load_power_w", "grid_present", "pv_power_w", "curtail_w"))
    for kind in ("pv", "load", "outage"):
        telemetry.save_forecast(sid, kind, h0, 1, "v1.0", {"q50": [1.5] * 192, "q05": [1.0] * 192, "q95": [2.0] * 192, "p_down": [0.25] * 192,
                                                          "p_start": [0.05] * 192})
        telemetry.save_forecast(sid, kind, b, 1, "v1.0", {"q50": [2.5] * 192, "p_down": [0.1] * 192, "p_start": [0.01] * 192})
    load = forecast.page(site, "load")
    past = load["past"]
    assert past["t"][0] == h0 and len(past["t"]) == 96 and past["t"][-1] == b - forecast.STEP_S
    assert past["belief_time"] == h0
    assert past["q50"] == [1.5] * 96 and past["q05"] == [1.0] * 96
    assert len(past["actual"]) == 96 and all(v == pytest.approx(2.0) for v in past["actual"])
    pv = forecast.page(site, "pv")["past"]
    assert all(v == pytest.approx(4.0) for v in pv["actual"])  # 3 kW produced plus 1 kW curtailed
    out = forecast.page(site, "outage")["past"]
    assert out["p_down"] == [0.25] * 96 and out["p_start"] == [0.05] * 96
    assert out["actual"][24:28] == [1.0] * 4 and out["actual"][:24] == [0.0] * 24
    # the latest issue's own actuals are unchanged: from its belief time on
    assert len(load["actual"]) == (now // 60 * 60 - b) // forecast.STEP_S


def test_the_past_of_a_site_without_an_older_issue_still_has_its_actuals(company):
    """A site activated less than a day ago has no issue a day old: the page still answers the past actuals, with no past issue."""
    sid = "page-young"
    now = int(datetime.now(timezone.utc).timestamp())
    b = forecast.align(now)
    telemetry.write_rows([(sid, int(t), "edge", 1500.0) for t in range(b - 7200, b, 60)], ("site_id", "ts", "source_id", "load_power_w"))
    telemetry.save_forecast(sid, "load", b, 0, "v1.0", {"q50": [2.5] * 192})
    past = forecast.page({"id": sid}, "load")["past"]
    assert past["belief_time"] is None and "q50" not in past
    assert past["actual"][-8:] == [1.5] * 8 and past["actual"][0] is None


# ------------------------------------------------------------------------------------------------ outages: helpers

def _grid_site(sid: str, disco: str = "LESCO", unit: dict | None = None) -> dict:
    data = {"disco": disco, "lat": LAT, "lon": LON}
    if unit is not None:
        data["outage_unit"] = unit
    return {"id": sid, "data": data, "connections": [{"id": "c1", "data": {"regime": "none"}}], "assets": {}}


def _write_grid(sid: str, ts, grid) -> None:
    """1-minute grid-present rows; a NaN in `grid` is a minute without a row (a lost reading)."""
    rows = [(sid, int(t), "edge", float(g)) for t, g in zip(ts, grid) if not np.isnan(g)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present"))


def _local(ts) -> tuple[np.ndarray, np.ndarray]:
    """(local day number, minute of the local day) of epoch seconds."""
    loc = np.asarray(ts, dtype=np.int64) + 5 * 3600
    return loc // 86400, (loc % 86400) // 60


# ------------------------------------------------------------------------------------------------ engine-forecast-02

def test_schedule_adherence_counts_each_window_on_its_own_local_day_whatever_the_tick_time(company):
    """A daily 14:00-16:00 window kept (grid down throughout) on one local day in four. Each whole window occurrence in the 28 days before the tick is
    one trial on the Beta(8, 2) prior, so ticks at 00:00, 09:00, 15:00 (inside today's window) and 16:30 give the posterior of the windows they can
    see in full. v1 split the history into 24-hour blocks from the tick: at 15:00 each block held the end of one day's window and the start of the
    next day's, and a hit on either half counted, so the posterior rose from 0.39 to 0.55."""
    sid = "adh-days"
    site = _grid_site(sid)
    forecast.set_schedule(sid, [["14:00", "16:00", "daily"]], "manual", "t")
    day0 = datetime(2026, 11, 9, tzinfo=PKT)
    ts = np.arange(int((day0 - timedelta(days=31)).timestamp()), int((day0 + timedelta(hours=17)).timestamp()), 60, dtype=np.int64)
    dayn, minute = _local(ts)
    kept = dayn % 4 == 0
    grid = np.where(kept & (minute >= 14 * 60) & (minute < 16 * 60), 0.0, 1.0)
    _write_grid(sid, ts, grid)
    seen, want = {}, {}
    for hour in (0, 9, 15, 16.5):
        t0 = int((day0 + timedelta(hours=hour)).timestamp())
        h0 = t0 - 28 * 86400
        days = range((h0 + 5 * 3600) // 86400 - 1, (t0 + 5 * 3600) // 86400 + 1)
        whole = [d for d in days if d * 86400 - 5 * 3600 + 14 * 3600 >= h0 and d * 86400 - 5 * 3600 + 16 * 3600 <= t0]
        hits = sum(1 for d in whole if d % 4 == 0)
        o = forecast.outage(site, t0)
        seen[hour] = (o["adherence"], o.get("adherence_windows"))
        want[hour] = (pytest.approx((8 + hits) / (10 + len(whole)), abs=1e-3), len(whole))
    assert seen == want
    assert all(a < 0.45 for a, _ in seen.values()), seen


def test_a_window_with_too_few_readings_is_not_a_trial(company):
    """Windows whose minutes are mostly unread (the logger was off) say nothing about the feeder: they are not counted. 28 days of readings with the
    window's own minutes lost on 20 days leave 8 trials, all kept."""
    sid = "adh-gaps"
    site = _grid_site(sid)
    forecast.set_schedule(sid, [["14:00", "16:00", "daily"]], "manual", "t")
    t0 = int(datetime(2026, 11, 9, tzinfo=PKT).timestamp())
    ts = np.arange(t0 - 28 * 86400, t0, 60, dtype=np.int64)
    dayn, minute = _local(ts)
    inside = (minute >= 14 * 60) & (minute < 16 * 60)
    lost = inside & (dayn % 28 < 20) & (minute >= 14 * 60 + 30)  # 90 of the window's 120 minutes lost on 20 days
    grid = np.where(inside, 0.0, 1.0)
    grid[lost] = np.nan
    _write_grid(sid, ts, grid)
    o = forecast.outage(site, t0)
    assert o["adherence"] == pytest.approx(16 / 18, abs=1e-3)
    assert o["adherence_windows"] == 8


# ------------------------------------------------------------------------------------------------ pages-C-02 (role-viewer-03, role-operator-13)

def test_without_an_announced_schedule_the_outage_issue_has_no_adherence_and_a_schedule_not_yet_seen_is_the_prior(company):
    """With no announced windows there is nothing to keep: the issue's adherence is None (the pages say "No announced schedule"), not the Beta(8, 2)
    prior's 0.8 shown as a measured share. With windows entered but none seen whole yet, the adherence is the prior and `adherence_windows` is 0, so
    the pages can call it a prior; the stored issue (what the pages read) carries both."""
    sid = "adh-none"
    site = _grid_site(sid)
    t0 = int(datetime(2026, 11, 9, 9, 0, tzinfo=PKT).timestamp())
    _write_grid(sid, np.arange(t0 - 3600, t0, 60), np.ones(60))
    o = forecast.outage(site, t0)
    assert o["adherence"] is None and o["adherence_windows"] is None
    forecast.issue(site, t0)
    issue = forecast.page(site, "outage")["issue"]
    assert issue["adherence"] is None and issue["adherence_windows"] is None
    forecast.set_schedule(sid, [["14:00", "16:00", "daily"]], "manual", "t")
    o = forecast.outage(site, t0)
    assert o["adherence"] == pytest.approx(0.8) and o["adherence_windows"] == 0
    forecast.issue(site, t0)
    issue = forecast.page(site, "outage")["issue"]
    assert issue["adherence"] == pytest.approx(0.8) and issue["adherence_windows"] == 0


# ------------------------------------------------------------------------------------------------ engine-forecast-08

def test_a_gap_in_the_readings_does_not_split_an_outage_in_the_hazard_or_the_observed_outage(company):
    """28 days of 1-minute grid readings with a 2-hour outage (10:00-12:00) every day whose logger lost 3 minutes of rows halfway (10:58-11:00). Each
    outage is one start of 120 minutes, as the censoring's outage runs already bridge such gaps: lam_day = (28 + 0.35 x 7) / 35 = 0.87 on LESCO's
    prior. v1 read the unknown minutes as grid up, so it counted two starts a day of about an hour each (lam_day 1.67, median 58.5 minutes). Today the
    grid has been down since 10:00 with a gap at 10:30-10:32: at 11:00 the observed outage started at 10:00, 60 minutes ago, not at 10:33."""
    sid = "gap-runs"
    site = _grid_site(sid)
    t0 = int(datetime(2026, 11, 9, tzinfo=PKT).timestamp())
    ts = np.arange(t0 - 28 * 86400, t0 + 11 * 3600, 60, dtype=np.int64)
    dayn, minute = _local(ts)
    grid = np.where((minute >= 10 * 60) & (minute < 12 * 60), 0.0, 1.0)
    grid[(ts < t0) & (minute >= 10 * 60 + 58) & (minute < 11 * 60 + 1)] = np.nan
    grid[(ts >= t0) & (minute >= 10 * 60 + 30) & (minute < 10 * 60 + 33)] = np.nan
    _write_grid(sid, ts, grid)
    o = forecast.outage(site, t0)
    assert (o["median_minutes"], o["lam_day"]) == (120.0, pytest.approx((28 + 0.35 * 7) / 35, abs=0.01))
    now = t0 + 11 * 3600
    obs = forecast.outage(site, forecast.align(now), now=now + 30)["observed"]
    assert obs is not None and obs["since"] == t0 + 10 * 3600 and obs["elapsed_min"] == 60


def test_an_outage_running_at_the_end_of_the_history_counts_as_a_start_but_not_as_a_length(company):
    """The hazard counts the outage the tick is inside of as a start, but its length is not known yet: it does not enter the median (28 days of
    90-minute outages at 22:30, the last one still running at midnight minus 30 minutes)."""
    sid = "gap-open"
    site = _grid_site(sid)
    t0 = int(datetime(2026, 11, 9, 23, 0, tzinfo=PKT).timestamp())
    ts = np.arange(t0 - 28 * 86400, t0, 60, dtype=np.int64)
    _, minute = _local(ts)
    grid = np.where(minute >= 22 * 60 + 30, 0.0, np.where(minute < 30, 0.0, 1.0))  # 22:30 to 00:30
    _write_grid(sid, ts, grid)
    o = forecast.outage(site, t0)
    assert o["median_minutes"] == 120.0
    assert o["lam_day"] == pytest.approx((28 + 0.35 * 7) / 35, abs=0.01)


# ------------------------------------------------------------------------------------------------ brief-vs-build-09, pages-C-08 (role-analyst-19)

def _feeder_site(name: str, code: str, disco: str = "FESCO", unit_name: str = "Jhang Road Industrial") -> dict:
    from gridlytics import sites
    sid = sites.create({"name": name, "disco": disco, "outage_unit": {"kind": "feeder", "code": code, "name": unit_name}}, "test")["id"]
    sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "test")
    return sites.full(sid)


def test_sites_on_the_same_feeder_pool_their_outage_history(company):
    """A site on feeder 411205 with 28 days of readings (the grid lost 18:00-19:30 every day, and inside the announced 14:00-16:00 window every day)
    and a new site on the same feeder with none: the new site's hazard, median length and adherence are learnt from its neighbour's outages
    (lam_day = (28 + 0.7 x 7) / 35 on FESCO's prior, 90 minutes, 36 / 38 kept), and its issue says so (`pooled`) with its own history in days
    (`days_known`). A site on another feeder, or with a feeder name but no code, stays on the DISCO's prior. v1 read only each site's own history."""
    old = _feeder_site("Old mill", "411205")
    new = _feeder_site("New mill", " 411205 ")
    other = _feeder_site("Other mill", "999999", unit_name="Elsewhere")
    nocode = _feeder_site("No code mill", "", unit_name="SITE-11")
    t0 = int(datetime(2026, 11, 9, tzinfo=PKT).timestamp())
    ts = np.arange(t0 - 28 * 86400, t0, 60, dtype=np.int64)
    _, minute = _local(ts)
    down = ((minute >= 18 * 60) & (minute < 19 * 60 + 30)) | ((minute >= 14 * 60) & (minute < 16 * 60))
    _write_grid(old["id"], ts, np.where(down, 0.0, 1.0))
    forecast.set_schedule(new["id"], [["14:00", "16:00", "daily"]], "manual", "t")
    o = forecast.outage(new, t0)
    assert o["pooled"] == {"sites": 1, "days": 28.0} and o["days_known"] == 0.0
    assert o["lam_day"] == pytest.approx((28 + 0.7 * 7) / 35, abs=0.01)
    assert o["median_minutes"] == 90.0 and o["stage"] == 1
    assert (o["adherence"], o["adherence_windows"]) == (pytest.approx(36 / 38, abs=1e-3), 28)
    for s in (other, nocode):
        o = forecast.outage(s, t0)
        assert o["pooled"] == {"sites": 0, "days": 0.0} and o["stage"] == 0
        assert o["lam_day"] == pytest.approx(0.7, abs=0.01)
    # the stored issue carries the sample size and the pooling, for the model card
    forecast.issue(new, t0)
    issue = forecast.page(new, "outage")["issue"]
    assert issue["pooled"] == {"sites": 1, "days": 28.0} and issue["days_known"] == 0.0


def test_the_feeder_pool_leaves_out_demo_sites_and_off_grid_sites(company):
    """Review of brief-vs-build-09: a demo site's grid history is the twin's, made up, and an off-grid site's grid always reads 0, so neither is a sensor
    of a real feeder, even with the same DISCO and feeder code (the demo seeds FESCO feeder 411205). A real site pools only real sites on the grid, and a
    demo site only demo sites. Before the review every site of the company with the code was pooled: the real site below read the demo's and the
    off-grid site's 56 days, the off-grid site's always-down grid as windows kept."""
    from gridlytics import sites
    unit = {"kind": "feeder", "code": "411205", "name": "Jhang Road Industrial"}
    real = _feeder_site("Real mill", "411205")
    demo_ids = []
    for name in ("Demo mill", "Demo weaving"):
        sid = sites.create({"name": name, "disco": "FESCO", "outage_unit": unit}, "test", demo_link="demo")["id"]
        sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "test")
        demo_ids.append(sid)
    island = sites.create({"name": "Island mill", "disco": "FESCO", "off_grid": True, "outage_unit": unit}, "test")["id"]
    t0 = int(datetime(2026, 11, 9, tzinfo=PKT).timestamp())
    ts = np.arange(t0 - 28 * 86400, t0, 60, dtype=np.int64)
    _, minute = _local(ts)
    _write_grid(demo_ids[0], ts, np.where((minute >= 18 * 60) & (minute < 19 * 60 + 30), 0.0, 1.0))
    _write_grid(island, ts, np.zeros(len(ts)))
    forecast.set_schedule(real["id"], [["14:00", "16:00", "daily"]], "manual", "t")
    o = forecast.outage(real, t0)
    assert o["pooled"] == {"sites": 0, "days": 0.0} and o["stage"] == 0
    assert o["adherence_windows"] == 0 and o["lam_day"] == pytest.approx(0.7, abs=0.01)
    # a demo site pools the other demo site on its feeder, not the real or the off-grid one
    o = forecast.outage(sites.full(demo_ids[1]), t0)
    assert o["pooled"] == {"sites": 1, "days": 28.0}


# ------------------------------------------------------------------------------------------------ engine-forecast-12

def test_a_rural_feeder_without_a_schedule_starts_from_about_ten_hours_a_day_without_grid_flagged_as_a_prior(company):
    """Plan Part 5.3's cold start without a schedule: about 10 hours a day without grid on QESCO, PESCO and SEPCO feeders (the twin's rural DISCOs, HESCO
    and TESCO too), flagged as a prior. v1 had only the DISCO's trip prior, about 2 hours a day on QESCO. The prior has the hazard prior's 7 days'
    weight, so 28 days of the site's own readings leave a fifth of it; an entered schedule replaces it; an urban LESCO feeder keeps its trip prior."""
    t0 = int(datetime(2026, 11, 9, tzinfo=PKT).timestamp())
    rural = _grid_site("rural", disco="QESCO")
    o = forecast.outage(rural, t0)
    assert 9.0 <= float(np.sum(o["p_down"][:96])) / 4 <= 10.5
    assert o["shed_prior"] == {"down_hours_day": 10.0, "weight": 1.0}
    assert float(np.sum(o["p_start"][:96])) > 3  # starts of the assumed shedding blocks, not only the trips
    urban = forecast.outage(_grid_site("urban", disco="LESCO"), t0)
    assert urban["shed_prior"] is None and float(np.sum(urban["p_down"][:96])) / 4 < 1
    seen = _grid_site("rural-seen", disco="QESCO")
    _write_grid("rural-seen", np.arange(t0 - 28 * 86400, t0, 60), np.ones(28 * 1440))
    o = forecast.outage(seen, t0)
    assert o["shed_prior"] == {"down_hours_day": 10.0, "weight": 0.2}
    assert float(np.sum(o["p_down"][:96])) / 4 < 2.5
    forecast.set_schedule("rural", [["14:00", "16:00", "daily"]], "manual", "t")
    assert forecast.outage(rural, t0)["shed_prior"] is None
    # the stored issue carries the flag for the pages
    forecast.issue(seen, t0)
    assert forecast.page(seen, "outage")["issue"]["shed_prior"] == {"down_hours_day": 10.0, "weight": 0.2}


# ------------------------------------------------------------------------------------------------ pages-C-21

@pytest.fixture()
def api_world(tmp_path, monkeypatch):
    """A company file of its own with one grid-connected site, and a client per role (the administrator is the company's owner)."""
    import types

    import identities
    from gridlytics import platform as platformdb, sites
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    co = identities.company("R7 forecasts company", own_file=True)
    db = platformdb.company_file(co)
    db.parent.mkdir(parents=True, exist_ok=True)
    with store.use_company(db):
        sid = sites.create({"name": "Refresh mill", "disco": "FESCO"}, "test")["id"]
        sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "test")
    clients = {role: identities.client(f"{role}@r7fc.test", None if role == "admin" else role, co, "owner" if role == "admin" else "member",
                                       raise_server_exceptions=False) for role in ("admin", "operator", "analyst", "viewer")}
    return types.SimpleNamespace(db=db, site=sid, c=clients, use=lambda: store.use_company(db))


def test_refreshing_forecasts_is_for_an_administrator_or_an_analyst(api_world):
    """Plan Part 9.4 and R8 2.8: retraining or refreshing the forecasts is the analyst's and the owner's. v1 let an operator do it (run_jobs)."""
    w = api_world
    url = f"/api/sites/{w.site}/forecasts/refresh"
    for role, code in (("admin", 200), ("analyst", 200), ("operator", 403), ("viewer", 403)):
        r = w.c[role].post(url)
        assert r.status_code == code, (role, r.text)
    assert w.c["operator"].post(url).json()["detail"] == "Refreshing forecasts is for an administrator or an analyst"
    assert w.c["analyst"].get("/api/meta").json()["permissions"]["refresh_forecasts"] is True
    assert w.c["operator"].get("/api/meta").json()["permissions"]["refresh_forecasts"] is False


def test_a_refresh_keeps_the_quarter_s_observed_outage_when_the_edge_s_rows_lag(api_world, monkeypatch):
    """The quarter's saved outage issue observed the grid down since 10 minutes ago; the edge's newest rows are now 5 minutes behind (beyond
    OBSERVED_LAG_MIN), so a fresh issue is blind. A refresh goes through the tick's protection (plans._keep_observed_outage): the observed issue stays.
    v1 stored the blind issue over it, and the next same-quarter re-solve read that as its prior (DESIGN_LOG rounds 1-2)."""
    from gridlytics import plans, routes_forecasts
    w = api_world
    now = forecast.align(int(datetime.now(timezone.utc).timestamp())) + 450
    monkeypatch.setattr(routes_forecasts, "now_ts", lambda: now)
    t0 = forecast.align(now)
    since = now // 60 * 60 - 600
    with w.use():
        ts = np.arange(since - 3600, now // 60 * 60 - 300, 60)
        telemetry.write_rows([(w.site, int(t), "edge", 0.0 if t >= since else 1.0) for t in ts], ("site_id", "ts", "source_id", "grid_present"))
        observed = {"since": int(since), "elapsed_min": 10, "expected_remaining_min": 60}
        telemetry.save_forecast(w.site, "outage", t0, 1, forecast.VERSION, {"p_down": [1.0] * 192, "g": [0.0] * 192, "observed": observed})
        assert forecast.outage(plans.sites.full(w.site), t0, now=now)["observed"] is None  # the fresh issue alone is blind
    r = w.c["analyst"].post(f"/api/sites/{w.site}/forecasts/refresh")
    assert r.status_code == 200, r.text
    with w.use():
        kept = telemetry.latest_forecast(w.site, "outage")
    assert kept["belief_time"] == t0 and kept["observed"] == observed


# ------------------------------------------------------------------------------------------------ load: helpers

#: a 200 kWh battery whose cut-off is 10 %: the backing is adequate while the state of charge is above 15 %
BATTERY = {"e_nom_kwh": 200.0, "p_ch_kw": 100.0, "p_dis_kw": 100.0, "soc_min": 0.10, "soc_max": 0.95, "eta_c": 0.95, "eta_d": 0.95}


def _load_site(sid: str, crit=None, battery: bool = True, genset: bool = False, **data) -> dict:
    d = {"disco": "LESCO", "lat": LAT, "lon": LON, "site_type": "industrial_2_shift", "load_peak_kw": 100.0, **data}
    if crit is not None:
        d["critical_load_kw"] = [float(crit)] * 24
    assets = {"battery": BATTERY} if battery else {}
    if genset:
        assets["genset"] = {"kva": 100.0, "kw": 80.0}
    return {"id": sid, "data": d, "connections": [{"id": "c1", "data": {"regime": "none"}}], "assets": assets}


def _write_load(sid: str, ts, load_kw, grid=None, soc=None, gen_on=None) -> None:
    """1-minute load rows with, where given, the grid-present flag, the estimated state of charge (0-1) and the genset's running flag."""
    cols = ["site_id", "ts", "source_id", "load_power_w"]
    extra = []
    for name, col in ((grid, "grid_present"), (soc, "battery_soc_est_pct"), (gen_on, "genset_running")):
        if name is not None:
            cols.append(col)
            extra.append(np.broadcast_to(np.asarray(name, dtype=float), (len(ts),)))
    scale = {"battery_soc_est_pct": 100.0}
    rows = []
    for i, (t, v) in enumerate(zip(ts, load_kw)):
        row = [sid, int(t), "edge", float(v) * 1000]
        for c, a in zip(cols[4:], extra):
            row.append(float(a[i]) * scale.get(c, 1.0))
        rows.append(tuple(row))
    telemetry.write_rows(rows, tuple(cols))


def _local_hour(ts) -> np.ndarray:
    return ((np.asarray(ts, dtype=np.int64) + 5 * 3600) % 86400) // 3600


# ------------------------------------------------------------------------------------------------ engine-forecast-05

def _evening_outages(sid: str, t0: int, days: int = 6, crit_kw: float = 30.0, soc: float = 0.6, gen_on=None) -> None:
    """`days` days of 100 kW load before t0 with the grid down from 18:00 to 20:00 each evening, when only the backed-up circuits run (crit_kw)."""
    ts = np.arange(t0 - days * 86400, t0, 60, dtype=np.int64)
    h = _local_hour(ts)
    down = (h >= 18) & (h < 20)
    _write_load(sid, ts, np.where(down, crit_kw, 100.0), grid=np.where(down, 0.0, 1.0), soc=np.full(len(ts), soc),
                gen_on=None if gen_on is None else np.full(len(ts), float(gen_on)))


def test_the_critical_load_is_learnt_from_outages_the_battery_carried(company):
    """Plan Part 4.1 and 5.2: Lc is the 75th percentile of the load seen in adequately backed outages (the genset off, the state of charge above the
    cut-off plus 5 points), per hour of day, with the entered profile as the fallback. Six evenings with the grid down from 18:00 to 20:00 and 30 kW
    carried by the battery: the forecast's critical load is 30 kW in those hours and the entered 0 elsewhere. v1 read only the onboarding form's
    profile, whose default of 0 kW left the reserve rule with nothing to hold."""
    site = _load_site("crit-learnt")
    t0 = _midnight(date(2026, 11, 5))
    _evening_outages("crit-learnt", t0)
    f = forecast.load(site, t0)
    h = _local_hour(forecast.step_times(t0))
    assert np.allclose(f["crit"][(h >= 18) & (h < 20)], 30.0, atol=0.5), f["crit"][(h >= 18) & (h < 20)]
    assert np.all(f["crit"][(h < 18) | (h >= 20)] == 0.0)
    assert f["crit_learned_hours"] == 2 and f["crit_zero"] is False
    # the issue carries both, for the forecast page
    forecast.issue(site, t0)
    body = telemetry.latest_forecast("crit-learnt", "load")
    assert body["crit_learned_hours"] == 2 and body["crit_zero"] is False


@pytest.mark.parametrize("case", ["soc_at_cut_off", "genset_running", "soc_unknown", "no_battery"])
def test_an_outage_without_adequate_backing_teaches_nothing_and_the_entered_profile_stays(company, case):
    """An outage carried at the battery's cut-off (plus 5 points), with the genset running, with no state of charge, or at a site without a battery
    says nothing about the critical load: the entered 50 kW stays in every hour."""
    sid = f"crit-{case}"
    site = _load_site(sid, crit=50, battery=case != "no_battery", genset=case == "genset_running")
    t0 = _midnight(date(2026, 11, 5))
    if case == "soc_unknown":
        ts = np.arange(t0 - 6 * 86400, t0, 60, dtype=np.int64)
        down = (_local_hour(ts) >= 18) & (_local_hour(ts) < 20)
        _write_load(sid, ts, np.where(down, 30.0, 100.0), grid=np.where(down, 0.0, 1.0))
    else:
        _evening_outages(sid, t0, soc=0.12 if case == "soc_at_cut_off" else 0.6, gen_on=1 if case == "genset_running" else None)
    f = forecast.load(site, t0)
    assert np.allclose(f["crit"], 50.0), np.unique(f["crit"])
    assert f["crit_learned_hours"] == 0 and f["crit_zero"] is False


def test_a_critical_load_of_zero_everywhere_is_flagged(company):
    """Nothing entered (the form's default of 0 kW) and nothing learnt: the issue flags `crit_zero`, so the pages can say the plan holds no reserve."""
    site = _load_site("crit-zero", battery=False)
    t0 = _midnight(date(2026, 11, 5))
    _evening_outages("crit-zero", t0)
    f = forecast.load(site, t0)
    assert np.all(f["crit"] == 0.0) and f["crit_zero"] is True and f["crit_learned_hours"] == 0
    # stage 0 (no history) flags it too
    g = forecast.load(_load_site("crit-zero-new", battery=False), t0)
    assert g["stage"] == 0 and g["crit_zero"] is True


def test_an_off_grid_site_does_not_learn_its_critical_load_from_the_always_down_grid(company):
    """An off-grid site's grid reads 0 at every minute: those are not outages, so the entered profile stays (the plan carries the whole load there)."""
    site = _load_site("crit-offgrid", crit=8)
    site["connections"] = [{"id": "c1", "data": {"regime": "off_grid"}}]
    t0 = _midnight(date(2026, 11, 5))
    ts = np.arange(t0 - 6 * 86400, t0, 60, dtype=np.int64)
    _write_load("crit-offgrid", ts, np.full(len(ts), 40.0), grid=np.zeros(len(ts)), soc=np.full(len(ts), 0.6))
    f = forecast.load(site, t0)
    assert np.allclose(f["crit"], 8.0) and f["crit_learned_hours"] == 0


# ------------------------------------------------------------------------------------------------ engine-forecast-06

def test_load_is_scored_on_unshed_minutes_with_the_baseline_over_the_same_slots(company):
    """Plan Part 5.2 and 5.5: censored samples leave the training set and the baselines, and the model and its baseline are scored over the same slots.
    The scored day sheds to its backed-up circuits from 14:00 to 16:00 (100 kW to 30 kW: censored); the week before shed from 10:00 to 11:00 and lost
    its rows from 02:00 to 06:00. The forecast is the full demand, 100 kW, except 150 kW from 02:00 to 06:00, where the baseline has no value. Over the
    unshed slots both have, the forecast and the baseline are exact. v1 scored the shed slots against the full-demand forecast and the forecast over
    96 slots against a baseline over 80, so both read about 30 % off."""
    sid = "load-score"
    site = _load_site(sid, battery=False)
    day = date(2026, 11, 4)
    t0 = _midnight(day)
    for d0, shed, gap in ((t0, (14, 16), None), (t0 - 7 * 86400, (10, 11), (2, 6))):
        ts = np.arange(d0 - 8 * 3600, d0 + 86400, 60, dtype=np.int64)
        h = _local_hour(ts)
        down = (h >= shed[0]) & (h < shed[1]) & (ts >= d0)
        keep = np.ones(len(ts), dtype=bool) if gap is None else ~((h >= gap[0]) & (h < gap[1]) & (ts >= d0))
        _write_load(sid, ts[keep], np.where(down, 30.0, 100.0)[keep], grid=np.where(down, 0.0, 1.0)[keep])
    h_steps = _local_hour(forecast.step_times(t0))
    q50 = np.where((h_steps >= 2) & (h_steps < 6), 150.0, 100.0)
    telemetry.save_forecast(sid, "load", t0, 1, forecast.VERSION, {"q50": [float(x) for x in q50] + [100.0] * 96})
    rows = forecast.score_day(site, day)
    head = next(r for r in rows if r["kind"] == "load" and r["horizon_class"] == "day_ahead")
    assert head["value"] == pytest.approx(0.0, abs=1e-6), head
    assert head["baseline_value"] == pytest.approx(0.0, abs=1e-6), head
    for r in rows:
        if r["kind"] == "load":
            assert r["value"] == pytest.approx(0.0, abs=1e-6) and r["baseline_value"] == pytest.approx(0.0, abs=1e-6), r


# ------------------------------------------------------------------------------------------------ engine-forecast-11 (brief-vs-build-17)

def test_the_pakistan_calendar_knows_the_public_holidays_eid_and_ramadan():
    """A dated table of Pakistan's federal holidays (pk_calendar): fixed dates, the lunar dates announced in 2024 and 2025, and every other year's from
    the arithmetic Islamic calendar, marked estimated."""
    from gridlytics import pk_calendar
    assert pk_calendar.day(date(2026, 11, 9)) == ("holiday", "Iqbal Day", False)
    assert pk_calendar.day(date(2021, 11, 9)) is None  # restored from 2022
    assert pk_calendar.day(date(2024, 5, 28)) == ("holiday", "Youm-e-Takbeer", False)
    assert pk_calendar.day(date(2023, 5, 28)) is None
    assert pk_calendar.day(date(2024, 4, 13)) == ("eid", "Eid ul-Fitr", False)  # four days announced in 2024
    assert pk_calendar.day(date(2025, 6, 6)) == ("eid", "Eid ul-Adha", False)
    assert pk_calendar.day(date(2025, 3, 10)) == ("ramadan", "Ramadan", False)
    assert pk_calendar.day(date(2025, 3, 23)) == ("holiday", "Pakistan Day", False)  # a holiday inside Ramadan is a holiday
    assert pk_calendar.day(date(2025, 3, 1)) is None  # Ramadan began on 2 March in Pakistan
    assert pk_calendar.day(date(2027, 3, 10)) == ("eid", "Eid ul-Fitr", True)
    assert pk_calendar.day(date(2027, 2, 20)) == ("ramadan", "Ramadan", True)
    assert pk_calendar.day(date(2026, 11, 10)) is None


def test_load_day_types_follow_the_calendar_and_the_site_s_off_and_half_days():
    """Day types (plan Part 5.2): working, off day (the site's own, Sunday by default), public holiday, Ramadan working day, Eid, half day. v1 knew
    only Sunday."""
    def at(d: date, hour: int = 12) -> int:
        return int(datetime(d.year, d.month, d.day, hour, tzinfo=PKT).timestamp())
    site = {"id": "s", "data": {}}
    days = [date(2026, 11, 8), date(2026, 11, 9), date(2026, 11, 10), date(2027, 2, 22), date(2027, 3, 10), date(2026, 11, 13)]
    types = forecast._day_type(np.array([at(d) for d in days]), site)
    assert list(types) == [forecast.DAY_OFF, forecast.DAY_HOLIDAY, forecast.DAY_WORKING, forecast.DAY_RAMADAN, forecast.DAY_EID, forecast.DAY_WORKING]
    friday = {"id": "s", "data": {"off_days": ["fri"], "half_days": ["sat"]}}
    types = forecast._day_type(np.array([at(date(2026, 11, 13)), at(date(2026, 11, 14)), at(date(2026, 11, 15))]), friday)
    assert list(types) == [forecast.DAY_OFF, forecast.DAY_HALF, forecast.DAY_WORKING]


def _weeks(sid: str, t0: int, days: int, work_kw: float = 100.0, off_kw: float = 20.0, shut: tuple = ()) -> None:
    """`days` days of flat load before t0: work_kw on working days, off_kw on Sundays and on the local dates in `shut`."""
    ts = np.arange(t0 - days * 86400, t0, 60, dtype=np.int64)
    loc = (ts + 5 * 3600) // 86400
    sunday = (loc + 3) % 7 == 6
    closed = sunday | np.isin(loc, [(_midnight(d) + 5 * 3600) // 86400 for d in shut])
    _write_load(sid, ts, np.where(closed, off_kw, work_kw), grid=np.ones(len(ts)))


def test_a_public_holiday_is_forecast_as_an_off_day(company):
    """Iqbal Day (Monday 9 November 2026): no holiday in the 21 days behind it, so it takes the off days' load (20 kW), and the Tuesday after is a
    working day again (100 kW). v1 forecast the holiday as a working day, so the plan charged for a peak that does not come."""
    sid = "holiday"
    site = _load_site(sid, battery=False, site_type="tower")
    t0 = _midnight(date(2026, 11, 9))
    _weeks(sid, t0, 21)
    f = forecast.load(site, t0)
    q50 = f["q"]["q50"]
    assert np.allclose(q50[:96], 20.0, atol=0.5), q50[:96:8]
    assert np.allclose(q50[96 + 8:], 100.0, atol=0.5), q50[96::8]  # past the first two hours, where the ratio's weight has faded
    assert [c["day"] for c in f["calendar"]] == ["2026-11-09"] and f["calendar"][0]["type"] == "public_holiday"
    assert f["calendar"][0]["name"] == "Iqbal Day" and f["calendar"][0]["estimated"] is False


def test_a_holiday_in_the_history_stays_out_of_the_working_days(company):
    """The plant shut on Iqbal Day (Monday 9 November): on Wednesday 11 November the working days behind the forecast leave it out, so the spread
    stays the working days' own. v1 counted the shut Monday as a working day, which pulled the band down by about 36 kW."""
    sid = "holiday-history"
    site = _load_site(sid, battery=False, site_type="tower")
    t0 = _midnight(date(2026, 11, 11))
    _weeks(sid, t0, 21, shut=(date(2026, 11, 9),))
    f = forecast.load(site, t0)
    q = f["q"]
    assert np.allclose(q["q50"][8:96], 100.0, atol=0.5)
    assert np.allclose(q["q05"][8:96], 100.0 * (1 - 1.645 * 0.06), atol=0.5), q["q05"][8:96:8]


def test_the_site_s_off_days_and_half_days_are_settings():
    """SiteFields carries the site's weekly off days (Sunday by default) and half days, refused in a sentence when not a weekday."""
    from gridlytics import schemas, sites
    s = schemas.check(sites.SiteFields, {"name": "Mill"})
    assert s.off_days == ["sun"] and s.half_days == []
    s = schemas.check(sites.SiteFields, {"name": "Mill", "off_days": ["fri"], "half_days": ["sat"]})
    assert s.off_days == ["fri"] and s.half_days == ["sat"]
    with pytest.raises(schemas.Refusal) as e:
        schemas.check(sites.SiteFields, {"name": "Mill", "off_days": ["friday"]})
    assert "off days" in e.value.detail and "must be one of" in e.value.detail


# ------------------------------------------------------------------------------------------------ engine-forecast-09

def test_the_last_two_hours_ratio_compares_with_earlier_days_only(company):
    """Monday 16 November 2026, 12:00: three days of 100 kW behind it (Friday afternoon, Saturday, Sunday off) and the last two hours at 130 kW. The
    ratio compares those two hours with the same slots on the earlier working day (Saturday: 100 kW), so it is 1.3 and the first step is 130 kW. v1's
    reference took the median with today's own values of those slots (median(100, 130) = 115), halving the lift."""
    sid = "ratio"
    site = _load_site(sid, battery=False, site_type="tower")
    t0 = int(datetime(2026, 11, 16, 12, tzinfo=PKT).timestamp())
    ts = np.arange(t0 - 3 * 86400, t0, 60, dtype=np.int64)
    _write_load(sid, ts, np.where(ts >= t0 - 7200, 130.0, 100.0), grid=np.ones(len(ts)))
    f = forecast.load(site, t0)
    assert f["stage"] == 1
    assert f["q"]["q50"][0] == pytest.approx(130.0, abs=0.5), f["q"]["q50"][:4]


# ------------------------------------------------------------------------------------------------ pages-C-06

def _data_days(sid: str, n: int, completeness: float, start: date = date(2026, 9, 1)) -> None:
    import json as _json
    with store.connect() as con:
        for i in range(n):
            con.execute("INSERT OR REPLACE INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                        (sid, (start + timedelta(days=i)).isoformat(), _json.dumps({"completeness": completeness, "load_kwh": 100.0})))


def test_a_forecast_stays_preliminary_until_28_verified_days_and_stage_1(company):
    """Plan Part 9.7 and R8 2.21: "Preliminary" marks every forecast until the site has 28 verified days and the forecast is stage 1. A verified day is
    a local day with at least half its minutes of data (the share a day needs to be scored). v1 dropped the mark at stage 1, three days in."""
    sid = "prelim"
    site = {"id": sid, "data": {}, "connections": [{"id": "c1", "data": {"regime": "none"}}], "assets": {}}
    telemetry.save_forecast(sid, "load", forecast.align(int(datetime.now(timezone.utc).timestamp())), 1, forecast.VERSION, {"q50": [1.0] * 192})
    p = forecast.page(site, "load")
    assert p["stage"] == 1 and p["preliminary"] is True and p["verified_days"] == 0 and p["verified_days_needed"] == 28
    _data_days(sid, 27, 0.9)
    _data_days(sid, 5, 0.3, start=date(2026, 10, 1))  # thin days are not verified
    p = forecast.page(site, "load")
    assert p["preliminary"] is True and p["verified_days"] == 27
    _data_days(sid, 1, 0.5, start=date(2026, 10, 10))
    p = forecast.page(site, "load")
    assert p["preliminary"] is False and p["verified_days"] == 28
    # a stage-0 forecast stays Preliminary however many days the site has
    telemetry.save_forecast(sid, "outage", forecast.align(int(datetime.now(timezone.utc).timestamp())), 0, forecast.VERSION, {"p_down": [0.1] * 192})
    assert forecast.page(site, "outage")["preliminary"] is True
    assert forecast.preliminary(sid, 1) == {"preliminary": False, "verified_days": 28, "verified_days_needed": 28}


# ------------------------------------------------------------------------------------------------ pages-C-12

def test_a_site_without_pv_has_no_pv_forecast_and_an_off_grid_site_no_outage_forecast(company):
    """A site without PV gets no PV issue (the optimiser still reads zeros) and its page says so; an off-grid site's outage page says it does not
    apply, though the issue the plan reads (always down) is still saved. v1 answered a stage-0 zero fan, "Preliminary" forever, and a stage-0 outage
    card about the feeder's prior."""
    t0 = forecast.align(int(datetime.now(timezone.utc).timestamp()))
    site = _load_site("no-pv")
    b = forecast.issue(site, t0)
    assert b["pv"]["stage"] is None and b["pv"]["not_applicable"] == "no_pv" and not np.any(b["pv"]["q"]["q50"])
    assert telemetry.latest_forecast("no-pv", "pv") is None and telemetry.latest_forecast("no-pv", "load") is not None
    p = forecast.page(site, "pv")
    assert p["issue"] is None and p["not_applicable"] == "no_pv" and p["preliminary"] is False
    assert forecast.page(site, "load")["not_applicable"] is None
    farm = _load_site("farm", crit=8)
    farm["connections"] = [{"id": "c1", "data": {"regime": "off_grid"}}]
    farm["assets"]["pv"] = PLANT
    b = forecast.issue(farm, t0)
    assert b["outage"]["stage"] is None and b["outage"]["off_grid"] is True
    assert telemetry.latest_forecast("farm", "outage")["off_grid"] is True  # the plan's protection still reads it
    p = forecast.page(farm, "outage")
    assert p["issue"] is None and p["not_applicable"] == "off_grid" and p["preliminary"] is False
    assert forecast.page(farm, "pv")["not_applicable"] is None


def test_an_off_grid_site_s_always_down_grid_is_not_scored_and_its_old_rows_are_left_out(company):
    """An off-grid site's outage forecast and its grid are both always down, so the Brier score and its baseline are 0 every day: meaningless. The day
    is not scored, and a summary for the site leaves out rows stored before."""
    farm = _load_site("farm-scores", crit=8)
    farm["connections"] = [{"id": "c1", "data": {"regime": "off_grid"}}]
    day = datetime.now(PKT).date() - timedelta(days=1)
    t0 = _midnight(day)
    ts = np.arange(t0, t0 + 86400, 60, dtype=np.int64)
    _write_load("farm-scores", ts, np.full(len(ts), 40.0), grid=np.zeros(len(ts)))
    telemetry.save_forecast("farm-scores", "outage", t0, 0, forecast.VERSION, {"p_down": [1.0] * 192, "off_grid": True})
    assert not [r for r in forecast.score_day(farm, day) if r["kind"] == "outage"]
    with store.connect() as con:
        con.execute("INSERT INTO forecast_scores (site_id, kind, day_local, horizon_class, metric, value, baseline_value) VALUES (?,?,?,?,?,?,?)",
                    ("farm-scores", "outage", day.isoformat(), "day_ahead", "brier", 0.0, 0.0))
    assert "outage" in forecast.scores("farm-scores")["summary"]
    s = forecast.scores("farm-scores", site=farm)
    assert "outage" not in s["summary"] and "outage" not in s["days"]


def test_the_plan_s_forecast_stage_leaves_out_a_kind_that_does_not_apply(api_world):
    """The refresh answers no PV stage for a site without PV, and the plan's forecast stage is the load's alone: a PV stage of 0 for ever would keep
    every plan at stage 0."""
    from gridlytics import plans
    w = api_world
    r = w.c["analyst"].post(f"/api/sites/{w.site}/forecasts/refresh")
    assert r.status_code == 200 and r.json()["stages"]["pv"] is None and r.json()["stages"]["load"] == 0
    with w.use():
        out = plans.tick(w.site, save=False)
    assert out["summary"]["stage"] == 0 and "pv" not in out["summary"]["stages"] and out["summary"]["stages"]["load"] == 0


# ------------------------------------------------------------------------------------------------ outages page: helpers

#: the outages page's "now" in the route tests: Monday 9 November 2026, 18:00 in Karachi
PAGE_NOW = int(datetime(2026, 11, 9, 18, 0, tzinfo=PKT).timestamp())


def _pkt(month: int, day: int, hh: int, mm: int) -> int:
    return int(datetime(2026, month, day, hh, mm, tzinfo=PKT).timestamp())


def _page(w, monkeypatch, days: int, role: str = "analyst") -> dict:
    """GET /outages?days= at PAGE_NOW as `role`."""
    from gridlytics import routes_telemetry
    monkeypatch.setattr(routes_telemetry, "now_ts", lambda: PAGE_NOW)
    r = w.c[role].get(f"/api/sites/{w.site}/outages?days={days}")
    assert r.status_code == 200, r.text
    return r.json()


def _grid_with_downs(sid: str, t0: int, t1: int, downs, soc=None) -> None:
    """1-minute grid-present rows from t0 to t1, down in each [start, end) of `downs`; with `soc` (0-1, a number or a function of ts), the estimated state
    of charge."""
    ts = np.arange(t0, t1, 60, dtype=np.int64)
    g = np.ones(len(ts))
    for a, b in downs:
        g[(ts >= a) & (ts < b)] = 0.0
    if soc is None:
        _write_grid(sid, ts, g)
    else:
        s = soc(ts) if callable(soc) else np.full(len(ts), float(soc))
        telemetry.write_rows([(sid, int(t), "edge", float(x), float(y) * 100) for t, x, y in zip(ts, g, s)],
                             ("site_id", "ts", "source_id", "grid_present", "battery_soc_est_pct"))


# ------------------------------------------------------------------------------------------------ role-analyst-15

def test_an_outage_starting_a_few_minutes_before_an_announced_window_is_that_window_s(api_world, monkeypatch):
    """The feeder's schedule is 14:00 to 16:00, Monday to Saturday. A DISCO's feeder is switched by hand and seldom on the minute: an outage that starts up
    to 15 minutes before a window opens and is still down when it does is the window's shedding, as is one starting inside it. One starting 30 minutes
    early, one over before the window opens, one on a Sunday and one at 10:00 are not. v1 tested only the start plus 5 minutes, so 13:51 and 13:53 read
    'Announced: No' with no cause, while 13:59 read 'In a window', 'Scheduled shedding'."""
    w = api_world
    outs = {
        (10, 31, 13, 51): ((10, 31, 16, 6), True),     # Saturday, 9 minutes early (v1: not announced)
        (11, 2, 13, 30): ((11, 2, 14, 30), False),     # Monday, 30 minutes early: a trip that ran into the window
        (11, 3, 10, 0): ((11, 3, 10, 40), False),      # Tuesday morning, no window
        (11, 4, 13, 59): ((11, 4, 16, 15), True),      # Wednesday, a minute early
        (11, 5, 14, 4): ((11, 5, 16, 0), True),        # Thursday, inside the window
        (11, 6, 13, 53): ((11, 6, 16, 8), True),       # Friday, 7 minutes early (v1: not announced)
        (11, 7, 13, 40): ((11, 7, 13, 50), False),     # Saturday, over before the window opens
        (11, 8, 14, 0): ((11, 8, 16, 0), False),       # Sunday: the windows are Monday to Saturday
    }
    with w.use():
        forecast.set_schedule(w.site, [["14:00", "16:00", "weekdays"]], "entered", "test")
        _grid_with_downs(w.site, PAGE_NOW - 10 * 86400, PAGE_NOW, [(_pkt(*a), _pkt(*b)) for a, (b, _) in outs.items()])
    log = {o["start"]: o for o in _page(w, monkeypatch, 10)["log"]}
    assert len(log) == len(outs)
    for a, (_b, announced) in outs.items():
        o = log[_pkt(*a)]
        assert o["scheduled"] is announced, a
        assert o["cause"] == ("scheduled" if announced else ""), a


# ------------------------------------------------------------------------------------------------ pages-C-07

def _plan_before(sid: str, step_start: int, reserve_pct: float, p_start: float, modelled: bool = False, n: int = 192) -> None:
    """An active plan issued a step before `step_start` (the outage's step): step 0 is the step before it, holding `reserve_pct` at its end with
    `p_start` the chance an outage starts in the next step; the outage's own step (step 1) drops to the 10 % floor, and is a modelled outage (grid 0)
    with `modelled`."""
    import json as _json
    from gridlytics import plans
    b = step_start - 900
    steps = [{"t": b + 900 * i, "reserve_pct": 10.0, "soc_pct": 70.0, "p_start": 0.0, "p_down": 0.0, "grid_present": 1.0} for i in range(n)]
    steps[0].update(reserve_pct=reserve_pct, p_start=p_start)
    if modelled:
        steps[1].update(grid_present=0.0, p_down=1.0)
    with store.connect() as con:
        con.execute("UPDATE plans SET status = 'superseded' WHERE site_id = ? AND status = 'active'", (sid,))
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, steps, slots) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (store.new_id(), sid, plans._iso(b), plans._iso(b), plans._iso(b), plans._iso(b + n * 900), "a", "active", _json.dumps(steps), "[]"))


def test_the_outages_page_gives_the_reserve_at_each_outage_s_start_and_the_reserve_adequacy(api_world, monkeypatch):
    """Plan Part 8.3 and R8 2.7: reserve adequacy is the share of forecastable outages entered with the state of charge at or above the reserve target,
    and the log gives each outage's reserve target at its start. The target is the floor the plan in force held at the end of the step before the
    outage's step (optimise.py holds the reserve on E[t + 1] for an outage starting in step t + 1); an outage is forecastable when that plan gave a start
    in its step a chance of at least 1 % (the optimiser's threshold for a reserve) or modelled the step as an outage. Two points under the target still
    count as met (the Reserve alert's tolerance). v1 answered none of it, and counted 'with_genset' outages rather than hours on the genset."""
    from gridlytics import plans, reserve, sites
    w = api_world
    #   start              end                 SoC   plan before it: (reserve %, p_start, modelled) or None
    outs = [((11, 4, 14, 4), (11, 4, 16, 4), 0.65, (60.0, 0.40, False)),   # met
            ((11, 5, 14, 0), (11, 5, 15, 0), 0.50, (60.0, 0.40, False)),   # short: 50 + 2 < 60 (the outage's own step's 10 % would read met)
            ((11, 6, 10, 7), (11, 6, 10, 37), 0.30, (10.0, 0.002, False)),  # not forecastable: the plan gave it 0.2 %
            ((11, 7, 14, 1), (11, 7, 15, 1), 0.55, (10.0, 0.0, True)),     # forecastable: the plan modelled the step as an outage; met
            ((11, 8, 15, 3), (11, 8, 15, 33), 0.59, (60.0, 0.20, False)),   # met within the 2 points
            ((10, 31, 12, 0), (10, 31, 12, 20), 0.40, None)]                # no plan in force yet
    with w.use():
        sites.put_asset(w.site, "battery", {"nameplate_kwh": 200.0, "p_ch_kw": 100.0, "p_dis_kw": 100.0}, "test")
        for a, _b, _soc, p in sorted(outs, key=lambda x: _pkt(*x[0])):
            if p is not None:
                _plan_before(w.site, _pkt(*a) // 900 * 900, *p)
        ts = np.arange(PAGE_NOW - 10 * 86400, PAGE_NOW, 60, dtype=np.int64)
        g, soc, gen = np.ones(len(ts)), np.full(len(ts), 0.7), np.zeros(len(ts))
        for a, b, s, _p in outs:
            inside = (ts >= _pkt(*a)) & (ts < _pkt(*b))
            g[inside], soc[inside] = 0.0, s
        gen[(ts >= _pkt(11, 4, 14, 4)) & (ts < _pkt(11, 4, 14, 34))] = 1.0  # the genset ran the first 30 minutes of the first outage
        telemetry.write_rows([(w.site, int(t), "edge", float(x), float(y) * 100, float(z)) for t, x, y, z in zip(ts, g, soc, gen)],
                             ("site_id", "ts", "source_id", "grid_present", "battery_soc_est_pct", "genset_running"))
        assert plans.active_at(w.site, _pkt(10, 31, 12, 0)) is None
    page = _page(w, monkeypatch, 10)
    log = {o["start"]: o for o in page["log"]}
    want = {_pkt(11, 4, 14, 4): (60.0, True, True), _pkt(11, 5, 14, 0): (60.0, True, False), _pkt(11, 6, 10, 7): (10.0, False, None),
            _pkt(11, 7, 14, 1): (10.0, True, True), _pkt(11, 8, 15, 3): (60.0, True, True), _pkt(10, 31, 12, 0): (None, False, None)}
    for start, (target, forecastable, met) in want.items():
        o = log[start]
        assert (o["reserve_pct"], o["forecastable"], o["reserve_met"]) == (target, forecastable, met), o
    assert log[_pkt(11, 4, 14, 4)]["battery_minutes"] == 90 and log[_pkt(11, 5, 14, 0)]["battery_minutes"] == 60
    k = page["kpis"]
    assert (k["reserve_outages"], k["reserve_met"], k["reserve_adequacy_pct"]) == (4, 3, 75.0)
    assert k["genset_hours"] == 0.5 and k["battery_hours"] == round((320 - 30) / 60, 1)
    # the same figure for the pilot's gate (Part 15.3), from a function a report or the pilot can call
    with w.use():
        a = reserve.adequacy(sites.full(w.site), PAGE_NOW - 10 * 86400, PAGE_NOW)
    assert (a["outages"], a["met"], a["pct"]) == (4, 3, 75.0) and [s["start"] for s in a["short"]] == [_pkt(11, 5, 14, 0)]
    # a viewer reads it too (no money in it)
    assert _page(w, monkeypatch, 10, role="viewer")["kpis"]["reserve_adequacy_pct"] == 75.0
    # without a battery there is no reserve to enter an outage with
    with w.use():
        sites.remove_asset(w.site, "battery", "test")
    page = _page(w, monkeypatch, 10)
    assert page["kpis"]["reserve_adequacy_pct"] is None and page["kpis"]["reserve_outages"] == 0 and page["kpis"]["battery_hours"] is None
    assert all(o["reserve_pct"] is None and o["reserve_met"] is None and o["battery_minutes"] is None for o in page["log"])


# ------------------------------------------------------------------------------------------------ pages-C-10

def test_the_outages_page_answers_a_calendar_heatmap_by_date_and_hour(api_world, monkeypatch):
    """R8 2.7: 'an outage heatmap (date on y, hour on x, cells coloured by minutes without grid; the single most useful chart for a load-shedding
    feeder)'. v1 folded every day onto 7 weekday rows (heat[weekday][hour]), so a day's outage, a bad week or a change of schedule could not be read and a
    90-day cell summed about 13 days. The answer now also gives the last `days` local dates to today, oldest first, with each local hour's outage
    minutes; an hour with no grid reading (a logger that was off) and an hour still to come are unknown (null), not an hour with the grid up."""
    w = api_world
    downs = [((11, 5, 14, 30), (11, 5, 16, 10)),   # Thursday afternoon: 30, 60 and 10 minutes
             ((11, 6, 23, 30), (11, 7, 0, 45))]    # across midnight: 30 minutes on the 6th, 45 on the 7th
    gap = (_pkt(11, 8, 2, 0), _pkt(11, 8, 5, 0))   # no rows at all on the 8th from 02:00 to 05:00
    with w.use():
        ts = np.arange(PAGE_NOW - 10 * 86400, PAGE_NOW, 60, dtype=np.int64)
        g = np.ones(len(ts))
        for a, b in downs:
            g[(ts >= _pkt(*a)) & (ts < _pkt(*b))] = 0.0
        g[(ts >= gap[0]) & (ts < gap[1])] = np.nan
        _write_grid(w.site, ts, g)
    heat = _page(w, monkeypatch, 7)["heatmap"]
    # the weekday profile stays (it is what the schedule is read from)
    assert heat["days"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] and len(heat["minutes"]) == 7
    assert heat["dates"] == [f"2026-11-0{d}" for d in range(3, 10)]
    cal = dict(zip(heat["dates"], heat["by_date"]))
    assert all(len(row) == 24 for row in heat["by_date"])
    assert cal["2026-11-05"][13:17] == [0, 30, 60, 10]
    assert cal["2026-11-06"][23] == 30 and cal["2026-11-07"][0] == 45 and cal["2026-11-07"][1] == 0
    assert cal["2026-11-08"][1] == 0 and cal["2026-11-08"][2:5] == [None, None, None] and cal["2026-11-08"][5] == 0
    assert cal["2026-11-09"][17] == 0 and cal["2026-11-09"][18:] == [None] * 6  # the page's now is 18:00
    assert sum(v or 0 for row in heat["by_date"] for v in row) == 175
    # 90 days: one row per date, not the weekday sums
    assert len(_page(w, monkeypatch, 90)["heatmap"]["by_date"]) == 90


# ------------------------------------------------------------------------------------------------ pages-C-19

def test_the_outage_forecast_page_gives_the_announced_windows_as_blocks(company):
    """R8 2.7: the 48-hour probability chart shows 'the announced slots as solid blocks'. v1 answered only the probabilities, so the schedule could be read
    only from a plateau of P(down). The outage page now carries each announced window's occurrence over the chart's span (the 24 hours before the issue
    and its 48 hours), clipped to the span, in epoch seconds; a window across midnight is one block, and a weekday window skips Sunday."""
    sid = "announced"
    site = _grid_site(sid)
    b = _pkt(11, 7, 12, 0)  # Saturday noon: the span runs from Friday noon to Monday noon
    telemetry.save_forecast(sid, "outage", b, 1, forecast.VERSION, {"p_down": [0.1] * 192, "p_start": [0.01] * 192})
    assert forecast.page(site, "outage")["announced"] == []  # no announced schedule
    forecast.set_schedule(sid, [["14:00", "16:00", "weekdays"], ["23:30", "00:30", "daily"]], "entered", "test")
    blocks = forecast.page(site, "outage")["announced"]
    want = [((11, 6, 14, 0), (11, 6, 16, 0)), ((11, 6, 23, 30), (11, 7, 0, 30)), ((11, 7, 14, 0), (11, 7, 16, 0)),
            ((11, 7, 23, 30), (11, 8, 0, 30)), ((11, 8, 23, 30), (11, 9, 0, 30))]  # Sunday the 8th has no 14:00 window
    assert blocks == [{"from": _pkt(*a), "to": _pkt(*z)} for a, z in want]
    # the PV and load pages carry no windows
    assert "announced" not in forecast.page(site, "load")


# ------------------------------------------------------------------------------------------------ engine-forecast-16

def _pv_issues(sid: str, day0: int, q50_of, days: int = 16, pr: float = 1.0, every_h: int = 3) -> None:
    """Stage-1 PV issues every `every_h` local hours over the `days` days before day0, each with the clear sky and the median q50_of(cs, t)."""
    for b in range(day0 - days * 86400, day0, every_h * 3600):
        t = forecast.step_times(b)
        cs = forecast._mean15(_clear, t)
        q50 = q50_of(cs, t)
        body = {"cs": [round(float(x), 3) for x in cs], "pr": pr, **{q: [round(float(x), 3) for x in q50] for q in forecast.QS}}
        telemetry.save_forecast(sid, "pv", b, 1, forecast.VERSION, body)


def test_pv_bands_are_conformal_from_the_stored_issues_errors_by_horizon_class(company):
    """Plan Part 5.1 (stage 1, from day 3): 'conformal residual bands by horizon class and sky class ... conformal from the last 14 to 28 days'. Every
    day is the same: whole local hours alternate between clear sky and half of it, so each day's clearness is the same and v1's fan, the quantiles of
    14 daily ratios applied to every step, had no width at all, while every 15-minute step missed its median by a quarter of the clear sky. The stored
    stage-1 issues of the last 14 days (at the model's median, 0.75 of clear sky) now set the bands: their errors span -0.25 to +0.25 of clear sky,
    so the 5 % and 95 % bands sit at 0.5 and 1.0 of clear sky, at every horizon class. Without stored issues the empirical fan stays."""
    sid = "pv-conformal"
    site = _pv_site(sid)
    day0 = _midnight(date(2026, 11, 9))
    ts = np.arange(day0 - 16 * 86400, day0, 60, dtype=np.int64)
    _write_pv(sid, ts, _clear(ts) * np.where(_local_hour(ts) % 2 == 0, 1.0, 0.5))
    before = forecast.pv(site, day0)
    noon = 12 * 4
    cs = float(before["cs"][noon])
    assert before["stage"] == 1 and before["bands"]["method"] == "empirical"
    assert float(before["q"]["q95"][noon] - before["q"]["q05"][noon]) < 0.05 * cs  # v1's fan: no width
    _pv_issues(sid, day0, lambda cs_, t: 0.75 * cs_)
    f = forecast.pv(site, day0)
    assert f["bands"]["method"] == "conformal", f["bands"]
    assert set(f["bands"]["classes"]) == {"0-2h", "2-6h", "6-24h", "24-48h"}, f["bands"]
    for i in (noon, noon + 96, 10 * 4 + 96):  # noon today (6-24h), noon and 10:00 tomorrow (24-48h)
        c = float(f["cs"][i])
        q = {k: float(f["q"][k][i]) / c for k in forecast.QS}
        assert q["q05"] == pytest.approx(q["q50"] - 0.25, abs=0.03) and q["q25"] == pytest.approx(q["q50"] - 0.25, abs=0.03), (i, q)
        assert q["q95"] == pytest.approx(min(q["q50"] + 0.25, 1.0), abs=0.03) and q["q75"] == pytest.approx(min(q["q50"] + 0.25, 1.0), abs=0.03), (i, q)
    # the bands are saved with the issue, so the page can say where they come from
    forecast.issue(site, day0)
    assert forecast.page(site, "pv")["issue"]["bands"]["method"] == "conformal"


def test_the_conformal_table_bins_the_errors_by_the_sky_at_the_issue_time(company):
    """The sky class is the clearness of the last daylight hour before the issue (plan R5 4.1 item 7: clear, intermediate, overcast). Clear and overcast
    days alternate (all clear sky, or 0.3 of it) and the stored issues forecast 0.65 of clear sky: an issue made under a clear sky missed low over its
    next hours, one made under an overcast sky missed high, and the 0-2 hour class keeps the two apart; the class over every sky holds both."""
    sid = "pv-sky"
    site = _pv_site(sid)
    day0 = _midnight(date(2026, 11, 9))
    ts = np.arange(day0 - 16 * 86400, day0, 60, dtype=np.int64)
    _write_pv(sid, ts, _clear(ts) * np.where(((ts - day0) // 86400) % 2 == 0, 1.0, 0.3))
    _pv_issues(sid, day0, lambda cs_, t: 0.65 * cs_)
    tab = forecast._conformal(site, "pv", day0)
    clear, over, every = tab["by"][("0-2h", "clear")], tab["by"][("0-2h", "overcast")], tab["by"][("0-2h", None)]
    assert np.median(clear) == pytest.approx(0.35, abs=0.03) and np.median(over) == pytest.approx(-0.35, abs=0.03)
    assert min(every) == pytest.approx(-0.35, abs=0.03) and max(every) == pytest.approx(0.35, abs=0.03)
    assert tab["days"] >= 14
    # the issue at midnight follows an overcast day: its first hours read the overcast errors, and the issue says which classes did
    f = forecast.pv(site, day0)
    assert f["bands"]["sky"] == "overcast" and "0-2h" in f["bands"]["sky_classes"], f["bands"]
    assert set(f["bands"]["sky_classes"]) <= set(f["bands"]["classes"])


def test_load_bands_turn_conformal_after_a_week_of_stage_1_issues(company):
    """Plan Part 5.2: load quantiles from 'the empirical spread across the k days plus conformal residuals after week 2'. Days alternate between 80 and
    120 kW, so the spread across the same-type days is about 20 kW and v1's 5 to 95 % band was the median plus or minus 1.645 times it (about 65 kW
    wide). The stored stage-1 issues at 100 kW missed by 20 % either way, so the conformal band is the median plus or minus 20 % of it; with fewer than
    7 days of issues the empirical band stays."""
    sid = "load-conformal"
    site = _load_site(sid, battery=False)
    day0 = _midnight(date(2026, 11, 4))
    ts = np.arange(day0 - 21 * 86400, day0, 60, dtype=np.int64)
    _write_load(sid, ts, np.where(((ts - day0) // 86400) % 2 == 0, 120.0, 80.0), grid=np.ones(len(ts)))

    def issues(days: int) -> None:
        for b in range(day0 - days * 86400, day0, 3 * 3600):
            telemetry.save_forecast(sid, "load", b, 1, forecast.VERSION, {q: [100.0] * 192 for q in forecast.QS})

    issues(5)
    few = forecast.load(site, day0)
    assert few["bands"]["method"] == "empirical", few["bands"]
    issues(16)
    f = forecast.load(site, day0)
    assert f["bands"]["method"] == "conformal", f["bands"]
    i = 12 * 4  # noon: the last two hours' ratio has faded
    m = float(f["q"]["q50"][i])
    assert float(f["q"]["q05"][i]) == pytest.approx(0.8 * m, rel=0.02) and float(f["q"]["q95"][i]) == pytest.approx(1.2 * m, rel=0.02)
    assert float(few["q"]["q95"][i] - few["q"]["q05"][i]) > 60.0  # v1's empirical band: 1.645 times the spread either side


def test_the_day_s_band_coverage_and_pinball_loss_are_scored(company):
    """Plan Part 5.5 (both PV and load): 'pinball loss, ... q10 to q90 coverage 80 percent plus or minus 5, q05 to q95 coverage 90 percent'. v1 scored
    the median only, so the bands the optimiser reads (PV q25, load q75) were never checked. The day runs at 0.8 of clear sky against a midnight issue
    whose 10 to 90 % band tops out at 0.6 and whose 5 to 95 % band reaches 1.2: none of the daytime slots falls in the first, all in the second. The
    nRMSE row stays the PV headline alerts.nightly reads, and the accuracy summary gives the coverage beside its target."""
    sid = "pv-cover"
    site = _pv_site(sid)
    day = datetime.now(PKT).date() - timedelta(days=1)  # within the summary's last 7 days
    t0 = _midnight(day)
    ts = np.arange(t0 - 2 * 86400, t0 + 86400, 60, dtype=np.int64)
    _write_pv(sid, ts, _clear(ts) * 0.8)
    cs = forecast._mean15(_clear, forecast.step_times(t0))
    ks = dict(zip(forecast.QS, (0.3, 0.4, 0.5, 0.55, 0.58, 0.6, 1.2)))
    telemetry.save_forecast(sid, "pv", t0, 1, forecast.VERSION, {"cs": [round(float(x), 3) for x in cs],
                                                                **{q: [round(float(x) * k, 3) for x in cs] for q, k in ks.items()}})
    rows = [r for r in forecast.score_day(site, day) if r["kind"] == "pv"]
    assert rows[0]["metric"] == "nrmse" and rows[0]["horizon_class"] == "day_ahead"
    by = {r["metric"]: r for r in rows if r["horizon_class"] == "day_ahead"}
    assert by["coverage_80"]["value"] == 0.0 and by["coverage_90"]["value"] == 1.0, by
    # the pinball loss over the seven quantiles, by the AC rating, over the daytime slots scored
    act = forecast._q15(_clear(np.arange(t0, t0 + 86400, 60, dtype=np.int64)) * 0.8)
    zen, _ = solar.sun_position(forecast.step_times(t0, 96) + 450, LAT, LON)
    ok = zen < 85.0
    want = np.mean([np.mean(np.maximum(v * (act[ok] - k * cs[:96][ok]), (v - 1) * (act[ok] - k * cs[:96][ok]))) for v, k in zip(forecast.QV, ks.values())])
    assert by["pinball"]["value"] == pytest.approx(want / 100.0, rel=0.02), (by["pinball"], want / 100.0)
    s = forecast.scores(sid, 30)
    assert {r["metric"] for r in s["days"].get("pv", [])} <= {"nrmse"}  # the day table keeps the headline metric
    cal = s["summary"]["pv"]["calibration"]
    assert cal["coverage_80"]["last30"] == 0.0 and cal["coverage_80"]["target"] == 0.8 and cal["coverage_80"]["tolerance"] == 0.05
    assert cal["coverage_90"]["last30"] == 1.0 and cal["coverage_90"]["target"] == 0.9 and cal["pinball"]["n"] == 1


# ------------------------------------------------------------------------------------------------ pages-C-09

def test_the_forecast_page_says_there_is_no_weather_feed(company):
    """Plan 9.3 UI-08 asks for 'the data-provider status'; there is no weather feed yet (FOR-02 waits on the owner's Open-Meteo licence), so every
    forecast page says so (`weather_feed` false) and the page can tell an operator the PV forecast is the clear sky times a clearness distribution."""
    sid = "no-weather"
    site = _pv_site(sid)
    for kind in ("pv", "load", "outage"):  # before the first issue
        assert forecast.page(site, kind)["weather_feed"] is False, kind
    forecast.issue(site, _midnight(date(2026, 11, 9)))
    for kind in ("pv", "load", "outage"):
        assert forecast.page(site, kind)["weather_feed"] is False, kind
    # a kind that does not apply says so too (the page still shows its notice)
    assert forecast.page({**site, "assets": {}}, "pv")["weather_feed"] is False
