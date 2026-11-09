"""Round-7 regression tests for the tariff engine (audit work package "tariffs"): the price vector and the bill engine (tariffs.py, tariff_data.py).

Batch 1, price vector correctness:
- flow-onboarding-01 (with engine-money-live-04): export earns nothing before an agreement starts or after a net-billing agreement ends; after a
  legacy net-metering agreement ends the site is billed under net billing (exports at the NAEPP, nothing netted), not at the import rate; in the
  optimiser's vector, the plan, the bill and the ledger; the Tariffs page lists each agreement with its state and the regime billed;
- engine-tariffs-03: every FCA line (any key that names an FCA, not only "fca") prices ICP units as the bill does, with the negative-FCA exemption;
- engine-tariffs-04: a legacy net-metering surplus is credited at the notified NAPPP (SRO 1501(I)/2026 para 23), not the off-peak rate;
- engine-tariffs-11: the debt-service surcharge is 43 paisa for agricultural tube wells (R9 section 7), and an adder's exemptions name categories;
- engine-tariffs-12: a TOU calendar must cover each month exactly once with a non-empty peak window.

Each test fails on the code before its fix."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_fix_savings import _days, _put_rows, _row, make_site
from test_fix_tariffs import OFF, PEAK_NOV, SRC, _bump, _conn, _flat_day, _minutes, _t0, company  # noqa: F401  (company is a fixture)

from gridlytics import sites, store, tariff_data, tariffs, verify

PKT = timezone(timedelta(hours=5))
NOV = date(2026, 11, 2)


def _vec(conn, d0: date = NOV, steps: int = 96, **kw):
    t0 = datetime(d0.year, d0.month, d0.day, tzinfo=PKT).astimezone(timezone.utc)
    return tariffs.vectors(conn, t0, steps, **kw)


def _agreement(regime: str, category: str = "B-2(b)", start=None, end=None):
    return _conn(category, regime, extra={"agreement_start": start, "agreement_end": end})


# ------------------------------------------------------------------------------------------------ engine-tariffs-12: TOU calendars cover each month once

def test_a_tou_calendar_must_cover_each_month_once_with_a_peak_window(company):
    bad = [{"seasons": [{"months": [12, 1, 2], "peak": ["17:00", "21:00"]}]},                                          # March to November missing
           {"seasons": [{"months": list(range(1, 8)), "peak": ["18:00", "22:00"]}, {"months": list(range(7, 13)), "peak": ["19:00", "23:00"]}]},  # July twice
           {"seasons": [{"months": list(range(1, 13)), "peak": ["18:00", "18:00"]}]}]                                   # an empty window
    for body, says in zip(bad, ("March, April, May, June, July, August, September, October and November have none", "July is listed twice",
                                "peak window whose start and end differ")):
        with pytest.raises(ValueError, match=says):
            tariffs.propose("tou", "xwdisco", "2027-01-01", body, SRC, "t")
    ok = tariffs.propose("tou", "xwdisco", "2027-01-01", {"seasons": [{"months": list(range(1, 13)), "peak": ["22:00", "02:00"]}]}, SRC, "t")
    assert ok["status"] == "pending"  # a window across midnight is a window
    # a draft stored before the check is refused at approval
    with store.connect() as con:
        con.execute("UPDATE tariff_versions SET body = ? WHERE id = ?", (json.dumps(bad[0]), ok["id"]))
    with pytest.raises(ValueError, match="have none"):
        tariffs.approve(ok["id"], "t")
    for v in tariff_data.SHIPPED:
        if v["kind"] == "tou":
            tariffs.validate_body("tou", v["key"], v["body"], v["effective_from"])
