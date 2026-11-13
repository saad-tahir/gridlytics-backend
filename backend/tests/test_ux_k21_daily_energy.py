"""UX backend ask K21 (plan H11; E6 item 2, E1.1.6; the Forecasts section's step 10): each issue's energy per Karachi day.

`issue.daily_kwh`, the day's median energy and its 10 to 90 % range, read by the server from the issue's own construction (its steps move together
through a day), so the browser never adds quantiles. Golden figures below. Test data: synthetic issues; nothing here calls the network."""
from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pytest
from test_ux_k23_stage_progress import PKT, _issue, _site, _ts, company  # noqa: F401  (company is a fixture)

from gridlytics import forecast

# ------------------------------------------------------------------------------------------------ K21: each issue's energy per Karachi day

def test_an_issue_s_energy_per_karachi_day_is_its_steps_at_the_median_and_at_the_10_and_90_percent_levels():
    """Golden: an issue at 14:00 on 12 Nov (Karachi) of 4 kW median, 2 kW at 10 % and 6 kW at 90 % on every step. Its 192 steps reach three Karachi
    days: 40 steps of the 12th (14:00 to 24:00), the whole 13th (96 steps), 56 steps of the 14th (00:00 to 14:00). At 0.25 h a step: 40.0, 96.0 and
    56.0 kWh at the median, half that at 10 %, one and a half times it at 90 %."""
    t0 = _ts(2026, 11, 12, 14)
    t = [t0 + 900 * i for i in range(192)]
    q = {"q10": [2.0] * 192, "q50": [4.0] * 192, "q90": [6.0] * 192}
    got = forecast.daily_kwh(t, q)
    assert got == [
        {"day": "2026-11-12", "from": "2026-11-12T14:00:00+05:00", "to": "2026-11-13T00:00:00+05:00", "whole_day": False,
         "median_kwh": 40.0, "low_kwh": 20.0, "high_kwh": 60.0},
        {"day": "2026-11-13", "from": "2026-11-13T00:00:00+05:00", "to": "2026-11-14T00:00:00+05:00", "whole_day": True,
         "median_kwh": 96.0, "low_kwh": 48.0, "high_kwh": 144.0},
        {"day": "2026-11-14", "from": "2026-11-14T00:00:00+05:00", "to": "2026-11-14T14:00:00+05:00", "whole_day": False,
         "median_kwh": 56.0, "low_kwh": 28.0, "high_kwh": 84.0}]


def test_a_day_s_energy_adds_its_own_steps_whatever_their_shape():
    """Golden: a day whose median rises 1 kW a step from 0 (0 + 1 + ... + 95 = 4,560 kW of steps, 1,140.0 kWh); its 10 % level is 0.8 of the median
    and its 90 % level the median plus 2 kW (1,140 + 96 x 2 x 0.25 = 1,188.0 kWh). A step left blank adds nothing."""
    t0 = _ts(2026, 11, 13)
    t = [t0 + 900 * i for i in range(96)]
    med = [float(i) for i in range(96)]
    got = forecast.daily_kwh(t, {"q10": [0.8 * v for v in med], "q50": med, "q90": [v + 2.0 for v in med]})
    assert got == [{"day": "2026-11-13", "from": "2026-11-13T00:00:00+05:00", "to": "2026-11-14T00:00:00+05:00", "whole_day": True,
                    "median_kwh": 1140.0, "low_kwh": 912.0, "high_kwh": 1188.0}]
    q = {"q10": [1.0] * 96, "q50": [2.0] * 96, "q90": [3.0] * 96}
    q["q50"][5] = None
    assert forecast.daily_kwh(t, q)[0]["median_kwh"] == 47.5
    assert forecast.daily_kwh(t, {"q50": med}) == []                     # an issue without its 10 and 90 % levels: none


def test_a_stage_0_pv_day_is_one_clearness_times_the_clear_sky_so_its_range_is_the_clearness_range(company):
    """Why the server reads a day at one level (forecast.daily_kwh): a PV issue is one clearness per day times the clear-sky curve. At stage 0 in
    November its 10 % clearness is 0.45 x 0.89 / 0.86 of clear sky and its median 0.89 of it, and its 90 % level (1.035) is held at the clear sky
    itself; so the whole day's median, low and high are those shares of the day's clear-sky energy, exactly."""
    sid = "k21-pv"
    t0 = _ts(2026, 11, 12, 14)
    f = forecast.pv(_site(sid), t0)
    assert f["stage"] == 0
    t = [int(x) for x in forecast.step_times(t0)]
    got = forecast.daily_kwh(t, {k: [float(x) for x in v] for k, v in f["q"].items()})
    whole = next(d for d in got if d["whole_day"])
    day = [i for i, x in enumerate(t) if datetime.fromtimestamp(x, PKT).date() == date(2026, 11, 13)]
    clear = float(np.sum(f["cs"][day])) * 0.25
    k = forecast.MONTH_K[11]
    assert whole["median_kwh"] == pytest.approx(clear * k, abs=0.1)
    assert whole["low_kwh"] == pytest.approx(clear * 0.45 * k / 0.86, abs=0.1)
    assert whole["high_kwh"] == pytest.approx(clear, abs=0.1)


def test_the_forecast_page_carries_each_pv_and_load_issue_s_daily_energy_and_no_outage_issue_does(company):
    sid = "k21-page"
    now = int(datetime.now(timezone.utc).timestamp())
    bt = forecast.align(now)
    for kind in ("pv", "load", "outage"):
        _issue(sid, kind, bt)
    site = _site(sid)
    for kind in ("pv", "load"):
        page = forecast.page(site, kind)
        assert page["issue"]["daily_kwh"] == forecast.daily_kwh(page["t"], page["issue"]), kind
        assert sum(d["median_kwh"] for d in page["issue"]["daily_kwh"]) == pytest.approx(192 * 4.0 * 0.25), kind
    assert "daily_kwh" not in forecast.page(site, "outage")["issue"]
