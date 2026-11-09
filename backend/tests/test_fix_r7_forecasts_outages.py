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
