"""UX backend ask K23 (plan H11; E6 item 4, E1.3.4; the Forecasts section's step 10): stage progress per kind.

Each issue says how far its kind is from stage 1, `stage_progress` {have, need, unit}, counted by the stage rule itself (forecast.STAGE1_DAYS), so
"2 of 3 days" and the stage never disagree. The helpers here are the forecast asks' (K20 and K21 import them). Test data: synthetic issues and
telemetry; nothing here calls the network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import forecast, solar, store, telemetry, twin

PKT = timezone(timedelta(hours=5))
LAT, LON = 31.5, 74.3
PLANT = {"kwp_dc": 100.0, "kw_ac": 100.0, "tilt": 15.0, "azimuth": 180.0}


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _site(sid: str, pv: bool = True) -> dict:
    return {"id": sid, "data": {"lat": LAT, "lon": LON, "disco": "LESCO"}, "assets": {"pv": PLANT} if pv else {},
            "connections": [{"id": "c1", "data": {"regime": "none"}}]}


def _ts(y, m, d, hh=0, mm=0) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=PKT).timestamp())


def _issue(sid: str, kind: str, bt: int, q50: float = 4.0, q10: float = 2.0, q90: float = 6.0, **more) -> None:
    body = {"q05": [q10 / 2] * 192, "q10": [q10] * 192, "q25": [q50 - 1] * 192, "q50": [q50] * 192, "q75": [q50 + 1] * 192, "q90": [q90] * 192,
            "q95": [q90 + 1] * 192, "p_down": [0.1] * 192, "p_start": [0.01] * 192, **more}
    telemetry.save_forecast(sid, kind, bt, 1, "v1.0", body)


def _write_pv_days(sid: str, first: date, days: int) -> None:
    for i in range(days):
        a = twin.day_start_utc(first + timedelta(days=i))
        ts = np.arange(a, a + 86400, 300, dtype=np.int64)
        kw = solar.pv_power(ts, LAT, LON, PLANT, 1.0, solar.local_month(ts))
        telemetry.write_rows([(sid, int(x), "edge", float(v) * 1000) for x, v in zip(ts, kw)], ("site_id", "ts", "source_id", "pv_power_w"))



# ------------------------------------------------------------------------------------------------ K23: stage progress per kind

def test_each_issue_says_how_far_its_kind_is_from_stage_1_by_the_stage_rule_s_own_count(company):
    """Two whole days of PV: stage 0, "2 of 3 days"; a third: stage 1, 3 of 3. The load and outage issues count their own days the same way
    (load: whole days of unshed readings; outages: whole days of grid readings here and on the feeder, 7 needed)."""
    sid = "k23-pv"
    site = _site(sid)
    _write_pv_days(sid, date(2026, 11, 9), 2)
    t0 = _ts(2026, 11, 11, 10)
    f = forecast.pv(site, t0)
    assert f["stage"] == 0 and f["stage_progress"] == {"have": 2, "need": 3, "unit": "days"}
    _write_pv_days(sid, date(2026, 11, 8), 1)
    f = forecast.pv(site, t0)
    assert f["stage"] == 1 and f["stage_progress"] == {"have": 3, "need": 3, "unit": "days"}
    # load: 2 days of readings, stage 0; outages: 4 days of grid readings of 7
    lid = "k23-load"
    a = _ts(2026, 11, 9)
    ts = np.arange(a, a + 2 * 86400, 60, dtype=np.int64)
    telemetry.write_rows([(lid, int(x), "edge", 50_000.0, 1.0) for x in ts], ("site_id", "ts", "source_id", "load_power_w", "grid_present"))
    lf = forecast.load(_site(lid, pv=False), t0)
    assert lf["stage"] == 0 and lf["stage_progress"] == {"have": 2, "need": 3, "unit": "days"}
    ts = np.arange(a - 2 * 86400, a + 2 * 86400, 60, dtype=np.int64)
    telemetry.write_rows([(lid, int(x), "edge", 50_000.0, 1.0) for x in ts], ("site_id", "ts", "source_id", "load_power_w", "grid_present"))
    of = forecast.outage(_site(lid, pv=False), t0)
    assert of["stage"] == 0 and of["stage_progress"] == {"have": 4, "need": 7, "unit": "days"}


def test_the_count_and_the_stage_read_one_number(company, monkeypatch):
    """The progress is the stage rule's own count against its own threshold: with the PV threshold at 2 days, two days of data are stage 1 and
    "2 of 2"; the page's words can never say "2 of 3 days" beside a stage 1."""
    monkeypatch.setitem(forecast.STAGE1_DAYS, "pv", 2)
    sid = "k23-one"
    _write_pv_days(sid, date(2026, 11, 9), 2)
    f = forecast.pv(_site(sid), _ts(2026, 11, 11, 10))
    assert f["stage"] == 1 and f["stage_progress"] == {"have": 2, "need": 2, "unit": "days"}


def test_the_page_answers_the_stored_issue_s_progress_null_for_an_issue_stored_before_and_for_a_kind_that_does_not_apply(company):
    sid = "k23-page"
    t0 = forecast.align(int(datetime.now(timezone.utc).timestamp()))
    site = _site(sid)
    b = forecast.issue(site, t0)                                         # saves the three kinds with their progress
    for kind in ("pv", "load", "outage"):
        page = forecast.page(site, kind)
        assert page["stage_progress"] == b[kind]["stage_progress"] and page["stage_progress"]["unit"] == "days", kind
        assert "stage_progress" not in page["issue"], kind
    _issue("k23-old", "load", t0)                                        # an issue stored before K23 has none
    assert forecast.page(_site("k23-old"), "load")["stage_progress"] is None
    no_pv = forecast.page(_site("k23-nopv", pv=False), "pv")
    assert no_pv["not_applicable"] == "no_pv" and no_pv["stage_progress"] is None
