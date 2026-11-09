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


# ------------------------------------------------------------------------------------------------ engine-tariffs-03: every FCA key

def _icp_conn():
    return _conn(extra={"icp": {"eligible": True, "peak": {"11": 10.0}, "off_peak": {"11": 100.0}}})


def _approve(kind, key, start, body):
    v = tariffs.propose(kind, key, start, body, SRC, "t")
    return tariffs.approve(v["id"], "t")


def _icp_prices(conn, ts, imp, exp):
    cal = tariffs.calendar_for(conn, NOV)
    peak = np.array([tariffs.is_peak_local(cal, datetime.fromtimestamp(int(t), PKT)) for t in ts])
    mtd = {"peak": float(imp[peak].sum() / 60), "off_peak": float(imp[~peak].sum() / 60)}
    vec = tariffs.vectors(conn, _t0(ts), 96, mtd, None, 0.0)  # no swing: the month's totals settle each period's case
    assert vec["icp"]["off_peak"]["case"] == "above" and vec["icp"]["peak"]["case"] == "above"
    return vec


def test_a_second_fca_billed_under_its_own_key_prices_icp_units_as_the_bill_does(company):
    conn = _icp_conn()
    ts, imp, exp = _flat_day(NOV)
    fca = {"rate": 2.5624, "billing_months": ["2026-11"], "exempt": ["lifeline", "prepaid", "evcs"], "negative_exempt_icp": True}
    _approve("adder", "fca_sep", "2026-11-01", {"name": "FCA September 2026 (bundled)", **fca})
    vec = _icp_prices(conn, ts, imp, exp)
    for minute in (OFF, PEAK_NOV):
        bill = _bump(conn, ts, imp, exp, NOV, NOV, minute)
        assert bill == pytest.approx((22.98 + 2.5624) * 1.015, abs=0.02)      # the ICP rate plus the positive FCA, with the duty
        assert vec["c_imp"][minute // 15] == pytest.approx(bill, abs=0.02)     # before: 23.32 against the bill's 25.93
    # a negative FCA under its own key is not charged on ICP units (R9 section 5), and the vector agrees
    _approve("adder", "fca_aug_neg", "2026-11-01", {"name": "FCA August 2026 (negative, late)", **fca, "rate": -1.5})
    vec = _icp_prices(conn, ts, imp, exp)
    bill = _bump(conn, ts, imp, exp, NOV, NOV, OFF)
    assert bill == pytest.approx((22.98 + 2.5624) * 1.015, abs=0.02)          # before: the -1.5 was charged on the ICP unit (24.41)
    assert vec["c_imp"][OFF // 15] == pytest.approx(bill, abs=0.02)
    bill = tariffs.rebuild(ts, imp, exp, conn, NOV, NOV)
    neg = next(line for line in bill["lines"] if line["line"].startswith("FCA August"))
    assert neg["units_kwh"] == pytest.approx(sum(bill["import_kwh"].values()) - sum(bill["icp_incremental_kwh"].values()), abs=0.01)


def test_an_fca_under_another_key_counts_as_the_months_fca(company):
    assert tariffs.is_fca({"key": "fca"}) and tariffs.is_fca({"key": "fca_sep"}) and tariffs.is_fca({"key": "FPA-2026-09"})
    assert not tariffs.is_fca({"key": "qta"}) and not tariffs.is_fca({"key": "dss"})
    assert not tariffs.fca_billed("2026-11")                                   # the shipped September FCA is still pending
    _approve("adder", "fca_sep", "2026-11-01", {"name": "FCA September 2026", "rate": 2.5624, "billing_months": ["2026-11"]})
    assert tariffs.fca_billed("2026-11") and tariffs.fca_billed("2026-10")


def test_the_tariff_library_marks_every_fca_line(world):
    with world.use():
        _approve("adder", "fca_sep", "2026-11-01", {"name": "FCA September 2026", "rate": 2.5624, "billing_months": ["2026-11"]})
    adders = {a["key"]: a for a in world.c["admin"].get("/api/tariffs", params={"on": "2026-11-09"}).json()["adders"]}
    assert adders["fca_sep"]["fca"] is True and adders["qta"]["fca"] is False and adders["dss"]["fca"] is False


def test_the_savings_report_counts_a_bundled_fca_as_billed(company):
    sid = make_site(name="Bundled FCA")
    _put_rows(sid, {d.isoformat(): {"native": _row(100.0, 100.0), "tuned": _row(100.0, 100.0)} for d in _days(date(2026, 11, 1), date(2026, 11, 28))})
    assert verify.summary(sid, "2026-11-01", "2026-11-30")["final_conditions"]["fca_billed"] is False
    _approve("adder", "fca_sep", "2026-11-01", {"name": "FCA September 2026", "rate": 2.5624, "billing_months": ["2026-11"]})
    assert verify.summary(sid, "2026-11-01", "2026-11-30")["final_conditions"]["fca_billed"] is True  # before: only key "fca" counted


# ------------------------------------------------------------------------------------------------ engine-tariffs-11: DSS by category

def _line(bill, prefix):
    return next((x for x in bill["lines"] if x["line"].startswith(prefix)), None)


def test_agricultural_tube_wells_pay_43_paisa_of_debt_service_surcharge(company):
    ts, imp, exp = _flat_day(NOV)
    d2 = tariffs.rebuild(ts, imp, exp, _conn("D-2(b)"), NOV, NOV)
    b2 = tariffs.rebuild(ts, imp, exp, _conn("B-2(b)"), NOV, NOV)
    assert _line(d2, "Debt service")["rate"] == 0.43 and _line(b2, "Debt service")["rate"] == 3.23
    units = sum(d2["import_kwh"].values())
    assert _line(d2, "Debt service")["amount_rs"] == pytest.approx(0.43 * units, abs=0.01)
    vec = _vec(_conn("D-2(b)"))
    assert vec["c_imp"][OFF // 15] == pytest.approx((28.69 + 0.43 + 0.5194) * 1.015)     # before: 3.23 on every D-2 unit
    assert _bump(_conn("D-2(b)"), ts, imp, exp, NOV, NOV, OFF) == pytest.approx(vec["c_imp"][OFF // 15], abs=0.02)


def test_an_adder_exemption_names_categories_or_classes(company):
    _approve("adder", "nj", "2026-11-01", {"name": "Neelum-Jhelum surcharge", "rate": 0.10, "billing_months": "all", "exempt": ["agricultural", "A-2(c)"]})
    ts, imp, exp = _flat_day(NOV)
    for category, charged in (("B-2(b)", True), ("D-2(b)", False), ("A-2(c)", False), ("A-2(b)", True)):
        bill = tariffs.rebuild(ts, imp, exp, _conn(category), NOV, NOV)
        assert (_line(bill, "Neelum") is not None) == charged, category
        assert (_bump(_conn(category), ts, imp, exp, NOV, NOV, OFF) - _vec(_conn(category))["c_imp"][OFF // 15]) == pytest.approx(0.0, abs=0.02)
    with pytest.raises(ValueError, match="category_rates"):
        tariffs.propose("adder", "dss", "2027-01-01", {"rate": 3.23, "billing_months": "all", "category_rates": {"agricultural": "0.43"}}, SRC, "t")


def test_the_tariff_library_shows_an_adders_category_rates_and_exemptions(world):
    adders = {a["key"]: a for a in world.c["admin"].get("/api/tariffs", params={"on": "2026-11-09"}).json()["adders"]}
    assert adders["dss"]["category_rates"] == {"agricultural": 0.43} and adders["dss"]["exempt"] == ["icp_units", "lifeline"]
    assert adders["qta"]["category_rates"] == {} and "prepaid" in adders["qta"]["exempt"]


def test_a_company_file_seeded_before_the_dss_correction_gets_it_once(company):
    tariffs.ensure_shipped()
    shipped = next(v for v in tariff_data.SHIPPED if v["kind"] == "adder" and v["key"] == "dss")
    old = {k: v for k, v in shipped["body"].items() if k != "category_rates"}  # the body an earlier build shipped
    with store.connect() as con:
        con.execute("UPDATE tariff_versions SET body = ?, hash = ? WHERE kind = 'adder' AND key = 'dss'", (json.dumps(old), tariffs._hash(old)))
    tariffs.ensure_shipped()
    tariffs.ensure_shipped()
    rows = [v for v in tariffs.versions("adder") if v["key"] == "dss"]
    assert sorted(v["status"] for v in rows) == ["active", "superseded"]
    assert next(a for a in tariffs.adders_for("2026-11") if a["key"] == "dss")["category_rates"] == {"agricultural": 0.43}
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM tariff_versions").fetchone()[0] == len(tariff_data.SHIPPED) + 1


def test_a_shipped_correction_leaves_the_companys_own_correction_in_force(company):
    tariffs.ensure_shipped()
    _approve("adder", "dss", "2023-04-01", {"name": "DSS as printed on our bills", "rate": 3.30, "billing_months": "all", "exempt": ["icp_units"]})
    shipped = next(v for v in tariff_data.SHIPPED if v["kind"] == "adder" and v["key"] == "dss")
    old = {k: v for k, v in shipped["body"].items() if k != "category_rates"}
    with store.connect() as con:
        con.execute("UPDATE tariff_versions SET body = ?, hash = ? WHERE kind = 'adder' AND key = 'dss' AND by = 'shipped'", (json.dumps(old), tariffs._hash(old)))
    tariffs.ensure_shipped()
    assert [a["rate"] for a in tariffs.adders_for("2026-11") if a["key"] == "dss"] == [3.30]


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
