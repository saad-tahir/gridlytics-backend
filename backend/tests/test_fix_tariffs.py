"""Regression tests for the confirmed tariff-engine defects (review group "tariffs"): adder versions resolved by billing month, the seeding race, the
legacy net-metering export price, billing months from the reading day, the tax stack, Tariff F, reading dates, version validation and the FY2025-26
schedule. Each test fails on the code before the fix."""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import store, tariff_data, tariffs

PKT = timezone(timedelta(hours=5))
SRC = {"kind": "manual", "ref": "test", "verified": "primary"}


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _conn(category="B-2(b)", regime="none", sanctioned=400.0, extra=None, **tax):
    data = {"disco_group": "xwdisco", "tariff_category": category, "regime": regime, "sanctioned_load_kw": sanctioned, "export_cap_kw": 100,
            "mdi_interval_min": 30, "icp": {"eligible": False},
            "tax": {"gst_recoverable": True, "s235_adjustable": True, "strn": True, "atl": True, "province": "punjab", **tax},
            "seasonal_multiplier": 1.0, "season_months": []}
    data.update(extra or {})
    return {"id": "c1", "data": data}


def _minutes(d0: date, days: int):
    t0 = int(datetime(d0.year, d0.month, d0.day, tzinfo=PKT).timestamp())
    return np.arange(t0, t0 + days * 86400, 60)


def _t0(ts) -> datetime:
    return datetime.fromtimestamp(int(ts[0]), timezone.utc)


def _bump(conn, ts, imp, exp, p0, p1, minute, which="imp"):
    """The bearable rupees one more kWh of import (or export) in a given minute adds to the bill."""
    base = tariffs.rebuild(ts, imp, exp, conn, p0, p1)["bearable_rs"]
    imp2, exp2 = imp.copy(), exp.copy()
    (imp2 if which == "imp" else exp2)[minute] += 60.0
    return tariffs.rebuild(ts, imp2, exp2, conn, p0, p1)["bearable_rs"] - base


def _flat_day(d0, kw=100.0):
    ts = _minutes(d0, 1)
    imp = np.full(len(ts), kw)
    imp[600:630] = kw * 1.5  # the month's MDI block, so a perturbation elsewhere moves no fixed charge
    return ts, imp, np.zeros(len(ts))


PEAK_NOV, OFF = 19 * 60 + 5, 3 * 60 + 5


# ------------------------------------------------------------------------------------------------ tariffs-adders-ignore-validity

def test_a_new_surcharge_version_replaces_the_old_one_from_its_start_only(company):
    v = tariffs.propose("adder", "dss", "2027-01-01", {"name": "DSS 2027", "rate": 3.82, "billing_months": "all", "exempt": ["icp_units", "lifeline"]},
                        SRC, "t")
    tariffs.approve(v["id"], "t")
    assert [a["rate"] for a in tariffs.adders_for("2026-11") if a["key"] == "dss"] == [3.23]
    assert [a["rate"] for a in tariffs.adders_for("2027-02") if a["key"] == "dss"] == [3.82]


def test_a_corrected_fca_for_the_same_bill_replaces_the_requested_one(company):
    requested = next(v for v in tariffs.versions("adder") if v["key"] == "fca" and v["status"] == "pending")
    tariffs.approve(requested["id"], "t")
    v = tariffs.propose("adder", "fca", "2026-11-01", {"name": "FCA September 2026 (notified)", "rate": 1.90, "billing_months": ["2026-11"],
                                                       "exempt": ["lifeline", "prepaid", "evcs"], "negative_exempt_icp": True}, SRC, "t")
    tariffs.approve(v["id"], "t")
    assert [a["rate"] for a in tariffs.adders_for("2026-11") if a["key"] == "fca"] == [1.90]
    assert [a["rate"] for a in tariffs.adders_for("2026-10") if a["key"] == "fca"] == [1.11]  # October's bill keeps August's FCA
    old = next(x for x in tariffs.versions("adder") if x["id"] == requested["id"])
    assert old["status"] == "superseded"
    vec = tariffs.vectors(_conn(), datetime(2026, 11, 2, 3, 0, tzinfo=PKT).astimezone(timezone.utc), 4)
    assert vec["c_imp"][0] == pytest.approx((22.83 + 0.5194 + 1.90 + 3.23) * 1.015, abs=1e-6)


def test_one_malformed_active_adder_does_not_stop_every_bill(company):
    tariffs.ensure_shipped()
    with store.connect() as con:
        con.execute("INSERT INTO tariff_versions (id, kind, key, effective_from, effective_to, known_from, status, supersedes, body, source, hash, created, by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("bad1", "adder", "nj", "2026-01-01", None, "2026-01-01", "active", None,
                                                          json.dumps({"name": "Neelum-Jhelum", "rate": 0.10}), json.dumps(SRC), "x", store._now(), "t"))
    assert sorted(a["key"] for a in tariffs.adders_for("2026-11")) == ["dss", "qta"]


# ------------------------------------------------------------------------------------------------ tariffs-ensure-shipped-race

def test_racing_first_uses_of_a_company_file_seed_the_library_once(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        with store.connect():
            pass  # a fresh, migrated file
    real = store.new_id

    def slow_id(n: int = 12) -> str:  # widens the window between the check and the inserts
        time.sleep(0.01)
        return real(n)

    monkeypatch.setattr(store, "new_id", slow_id)
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def first_use():
        try:
            with store.use_company(db):
                barrier.wait()
                tariffs.ensure_shipped()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=first_use) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    with store.use_company(db):
        with store.connect() as con:
            assert con.execute("SELECT COUNT(*) FROM tariff_versions").fetchone()[0] == len(tariff_data.SHIPPED)
            assert con.execute("SELECT COUNT(*) FROM fuel_prices").fetchone()[0] == len(tariff_data.FUEL_PRICES)
        assert [a["key"] for a in tariffs.adders_for("2026-11")].count("dss") == 1


# ------------------------------------------------------------------------------------------------ tariffs-legacy-export-price

def test_legacy_export_is_worth_the_import_it_nets_when_the_period_ends_as_an_import(company):
    conn = _conn("A-2(c)", "net_metering_legacy")
    d0 = date(2026, 11, 2)
    ts, imp, exp = _flat_day(d0)
    vec = tariffs.vectors(conn, _t0(ts), 96)
    tie = tariffs.LEGACY_EXPORT_TIE_BREAK  # the optimiser's export price sits a hair below the import (tariffs-legacy-degenerate-lp)
    for minute in (PEAK_NOV, OFF):
        step = minute // 15
        assert vec["c_exp"][step] == pytest.approx(vec["c_imp"][step] - tie)
        assert -_bump(conn, ts, imp, exp, d0, d0, minute, "exp") == pytest.approx(vec["c_exp"][step] + tie, rel=1e-3, abs=0.02)


@pytest.mark.parametrize("tax", [{}, {"strn": False, "gst_recoverable": False}])
def test_legacy_surplus_period_moves_only_the_bare_credit(company, tax):
    conn = _conn("A-2(c)", "net_metering_legacy", **tax)
    d0 = date(2026, 11, 2)
    ts, imp, exp = _flat_day(d0)
    exp[9 * 60:15 * 60] = 400.0  # 2,400 kWh exported off-peak against 2,025 imported: the off-peak period ends in surplus
    cal = tariffs.calendar_for(conn, d0)
    peak = np.array([tariffs.is_peak_local(cal, datetime.fromtimestamp(int(t), PKT)) for t in ts])
    mtd_imp = {"peak": float(imp[peak].sum() / 60), "off_peak": float(imp[~peak].sum() / 60)}
    mtd_exp = {"peak": float(exp[peak].sum() / 60), "off_peak": float(exp[~peak].sum() / 60)}
    vec = tariffs.vectors(conn, _t0(ts), 96, mtd_imp, None, 1e9, mtd_exp, None)
    s = OFF // 15
    tie = tariffs.LEGACY_EXPORT_TIE_BREAK  # the optimiser's export price sits a hair below the import (tariffs-legacy-degenerate-lp)
    s_rate = 25.32  # the notified NAPPP since round 7 (audit engine-tariffs-04); the off-peak 35.15 before
    assert vec["c_imp"][s] == pytest.approx(s_rate) and vec["c_exp"][s] == pytest.approx(s_rate - tie)
    assert -_bump(conn, ts, imp, exp, d0, d0, OFF, "exp") == pytest.approx(s_rate, abs=0.02)
    assert _bump(conn, ts, imp, exp, d0, d0, OFF, "imp") == pytest.approx(s_rate, abs=0.02)
    p = PEAK_NOV // 15  # the peak period still nets to an import
    assert -_bump(conn, ts, imp, exp, d0, d0, PEAK_NOV, "exp") == pytest.approx(vec["c_exp"][p] + tie, rel=1e-3, abs=0.02)


# ------------------------------------------------------------------------------------------------ tariffs-vectors-billing-month

def test_a_step_after_the_reading_day_carries_the_next_bills_adders(company):
    conn = _conn(extra={"reading_day": 15})
    assert tariffs.billing_month_for(conn, date(2026, 8, 15)) == "2026-08"
    assert tariffs.billing_month_for(conn, date(2026, 8, 25)) == "2026-09"
    assert tariffs.billing_month_for(conn, date(2026, 12, 20)) == "2027-01"
    assert tariffs.billing_period_for(conn, date(2026, 8, 25)) == (date(2026, 8, 16), date(2026, 9, 15))
    assert tariffs.billing_period_for(_conn(), date(2026, 8, 25)) == (date(2026, 8, 1), date(2026, 8, 31))
    d = date(2026, 8, 25)
    ts, imp, exp = _flat_day(d)
    vec = tariffs.vectors(conn, _t0(ts), 96)
    assert vec["billing_month"][0] == "2026-09"
    step = OFF // 15
    bill = _bump(conn, ts, imp, exp, date(2026, 8, 16), date(2026, 9, 15), OFF)
    assert vec["c_imp"][step] == pytest.approx(bill, rel=1e-3, abs=0.02)
    assert vec["c_imp"][step] == pytest.approx((22.83 + 0.5194 + 2.0581 + 3.23) * 1.015, abs=1e-6)
    assert tariffs.vectors(_conn(), _t0(ts), 4)["billing_month"][0] == "2026-08"  # no reading day: the calendar month


def test_a_horizon_across_the_reading_day_switches_adders_at_the_reading(company):
    conn = _conn(extra={"reading_day": 15})
    t0 = datetime(2026, 8, 15, 0, 0, tzinfo=PKT).astimezone(timezone.utc)
    vec = tariffs.vectors(conn, t0, 192)
    assert vec["billing_month_end_step"] == 96
    assert vec["c_imp"][12] == pytest.approx((22.83 - 1.9857 + 3.23) * 1.015, abs=1e-6)  # 15 Aug 03:00, August's bill
    assert vec["c_imp"][96 + 12] == pytest.approx((22.83 + 0.5194 + 2.0581 + 3.23) * 1.015, abs=1e-6)  # 16 Aug 03:00, September's bill


def test_the_icp_baseline_is_the_billing_months(company):
    conn = _conn(extra={"reading_day": 15, "icp": {"eligible": True, "peak": {"8": 100.0, "9": 200.0}, "off_peak": {"8": 1000.0, "9": 2000.0}}})
    vec = tariffs.vectors(conn, datetime(2026, 8, 25, 0, 0, tzinfo=PKT).astimezone(timezone.utc), 96)
    assert vec["icp"]["off_peak"]["baseline_kwh"] == pytest.approx(2000.0 * 1.028)


# ------------------------------------------------------------------------------------------------ tariffs-energy-factor-tax-stack

@pytest.mark.parametrize("category,tax", [("C-2", {"s235_adjustable": False}), ("D-2(b)", {"s235_adjustable": False}),
                                          ("A-2(c)", {"s235_adjustable": False}), ("B-2(b)", {"s235_adjustable": False}),
                                          ("B-2(b)", {"strn": False, "gst_recoverable": False}),
                                          ("A-2(c)", {"strn": False, "gst_recoverable": False, "s235_adjustable": False})])
def test_the_import_price_is_the_bills_marginal_rupee_under_every_tax_flag(company, category, tax):
    conn = _conn(category, **tax)
    d0 = date(2026, 11, 2)
    ts, imp, exp = _flat_day(d0)
    vec = tariffs.vectors(conn, _t0(ts), 96)
    for minute in (PEAK_NOV, OFF):
        assert _bump(conn, ts, imp, exp, d0, d0, minute) == pytest.approx(vec["c_imp"][minute // 15], rel=1e-3, abs=0.02)


# ------------------------------------------------------------------------------------------------ tariffs-tariff-f-mdi-rate (and the fixed factor)

TARIFF_F = {"seasonal_multiplier": 1.25, "season_months": [10, 11, 12, 1, 2, 3]}


@pytest.mark.parametrize("category,tax,extra,day", [("B-2(b)", {}, {}, date(2026, 11, 2)),
                                                    ("B-2(b)", {"strn": False, "gst_recoverable": False}, {}, date(2026, 11, 2)),
                                                    ("A-2(c)", {"s235_adjustable": False}, {}, date(2026, 11, 2)),
                                                    ("B-2(b)", {}, TARIFF_F, date(2026, 11, 2)),
                                                    ("B-2(b)", {}, TARIFF_F, date(2026, 6, 2))])
def test_the_mdi_rate_is_what_the_bill_charges_per_kw_of_billing_demand(company, category, tax, extra, day):
    ts = _minutes(day, 1)
    imp, exp = np.full(len(ts), 50.0), np.zeros(len(ts))  # MDI 50 kW, under the 25 percent floor of 400 kW
    conn = _conn(category, extra=extra, **tax)
    more = _conn(category, sanctioned=404.0, extra=extra, **tax)  # one more kW of billing demand
    base = tariffs.rebuild(ts, imp, exp, conn, day, day)
    after = tariffs.rebuild(ts, imp, exp, more, day, day)
    per_kw_month = (after["bearable_rs"] - base["bearable_rs"]) * 30  # a one-day bill is prorated x1/30
    vec = tariffs.vectors(conn, _t0(ts), 96)
    assert vec["mdi"]["rate_rs_per_kw"] == pytest.approx(per_kw_month, abs=0.5)


def test_tariff_f_charges_fixed_charges_only_in_its_season(company):
    nov, jun = date(2026, 11, 2), date(2026, 6, 2)
    conn = _conn(extra=TARIFF_F)
    fixed = lambda out: next(x["amount_rs"] for x in out["lines"] if x["line"] == "Fixed charges")  # noqa: E731
    ts = _minutes(nov, 1)
    assert fixed(tariffs.rebuild(ts, np.full(len(ts), 50.0), np.zeros(len(ts)), conn, nov, nov)) == pytest.approx(1250 * 100 * 1.25 / 30, abs=0.01)
    ts = _minutes(jun, 1)
    assert fixed(tariffs.rebuild(ts, np.full(len(ts), 50.0), np.zeros(len(ts)), conn, jun, jun)) == 0.0
    assert tariffs.vectors(conn, _t0(ts), 4)["mdi"]["rate_rs_per_kw"] == 0.0


# ------------------------------------------------------------------------------------------------ tariffs-reading-date-off-by-one

def test_the_reading_dates_printed_on_a_bill_do_not_bill_the_previous_reading_day(company):
    conn = _conn()
    ts = _minutes(date(2026, 10, 20), 31)  # 20 Oct to 19 Nov
    imp, exp = np.full(len(ts), 100.0), np.zeros(len(ts))
    by_reading = tariffs.rebuild(ts, imp, exp, conn, date(2026, 10, 20), date(2026, 11, 19), period_start_exclusive=True)
    by_first_day = tariffs.rebuild(ts, imp, exp, conn, date(2026, 10, 21), date(2026, 11, 19))
    assert by_reading["days"] == 30 and by_reading["first_day"] == "2026-10-21" and by_reading["last_day"] == "2026-11-19"
    assert sum(by_reading["import_kwh"].values()) == pytest.approx(72000.0)
    assert by_reading["total_rs"] == by_first_day["total_rs"]
    # a 34-day reading interval is 34 days, prorated x34/30
    ts = _minutes(date(2026, 10, 20), 35)
    out = tariffs.rebuild(ts, np.full(len(ts), 100.0), np.zeros(len(ts)), conn, date(2026, 10, 20), date(2026, 11, 23), period_start_exclusive=True)
    assert out["days"] == 34


# ------------------------------------------------------------------------------------------------ tariffs-unvalidated-version-approval

def test_a_malformed_version_is_refused_before_it_can_stop_every_bill(company):
    bad = [("adder", "nj", "2026-11-01", {"name": "Neelum-Jhelum", "rate": 0.10}),
           ("adder", "nj", "2026-11-01", {"name": "Neelum-Jhelum", "rate": "0.10", "billing_months": "all"}),
           ("adder", "fca", "2026-11-01", {"name": "FCA", "rate": 1.0, "billing_months": ["Nov 2026"]}),
           ("adder", "fca", "2026-12-08", {"name": "FCA", "rate": 1.0, "billing_months": ["2026-11"]}),
           ("schedule", "national", "2027-01-01", {"name": "CY2027", "cards": {}}),
           ("schedule", "national", "2027-01-01", {"name": "CY2027", "cards": {"B-2(b)": {"peak": 35.0, "off_peak": 22.0, "tou": True}}}),
           ("schedule", "cy2027", "2027-01-01", {"name": "CY2027", "cards": tariff_data.CARDS_279}),
           ("tou", "xwdisco", "2027-01-01", {"seasons": [{"months": [1], "peak": ["5 PM", "9 PM"]}]}),
           ("icp", "national", "2027-01-01", {"rate": 22.98}),
           ("prosumer", "net_billing", "2027-01-01", {"quarter": "2026-07..2026-09"})]
    for kind, key, start, body in bad:
        with pytest.raises(ValueError):
            tariffs.propose(kind, key, start, body, SRC, "t")
    placeholder = next(v for v in tariffs.versions("schedule") if v["status"] == "awaiting_notification")
    with pytest.raises(ValueError):
        tariffs.approve(placeholder["id"], "t")
    assert tariffs.card_for("B-2(b)", date(2027, 1, 2))[0]["peak"] == 35.68  # plans and bills still run after New Year


def test_every_shipped_version_passes_the_checks_and_has_a_distinct_start():
    starts = [(v["kind"], v["key"], v["effective_from"]) for v in tariff_data.SHIPPED]
    assert len(starts) == len(set(starts))
    for v in tariff_data.SHIPPED:
        if v["status"] != "awaiting_notification":
            tariffs.validate_body(v["kind"], v["key"], v["body"], v["effective_from"])


def test_a_schedule_missing_a_card_in_use_is_refused_and_a_full_one_supersedes_the_placeholder(company):
    tariffs.ensure_shipped()
    with store.connect() as con:
        con.execute("INSERT INTO connections (id, site_id, data, created, updated) VALUES (?,?,?,?,?)",
                    ("k1", "s1", json.dumps(_conn()["data"]), store._now(), store._now()))
    partial = tariffs.propose("schedule", "national", "2027-01-01", {"name": "CY2027", "cards": {"A-2(c)": tariff_data.CARDS_279["A-2(c)"]}}, SRC, "t")
    with pytest.raises(ValueError, match=r"B-2\(b\)"):
        tariffs.approve(partial["id"], "t")
    cards = {**tariff_data.CARDS_279, "B-2(b)": {**tariff_data.CARDS_279["B-2(b)"], "peak": 34.0}}
    full = tariffs.propose("schedule", "national", "2027-01-01", {"name": "CY2027", "cards": cards}, SRC, "t")
    tariffs.approve(full["id"], "t")
    by_id = {v["id"]: v for v in tariffs.versions("schedule")}
    assert next(v for v in by_id.values() if v["body"]["name"].startswith("Uniform schedule CY2027"))["status"] == "superseded"
    assert by_id[full["id"]]["status"] == "active"
    assert tariffs.card_for("B-2(b)", date(2027, 1, 2))[0]["peak"] == 34.0
    assert tariffs.card_for("B-2(b)", date(2026, 12, 31))[0]["peak"] == 35.68


# ------------------------------------------------------------------------------------------------ tariffs-pre-2026-bill-500

def test_a_2025_bill_is_priced_with_the_fy2025_26_schedule(company):
    conn = _conn()
    ts = _minutes(date(2025, 12, 20), 1)
    out = tariffs.rebuild(ts, np.full(len(ts), 100.0), np.zeros(len(ts)), conn, date(2025, 11, 21), date(2025, 12, 20))
    v = next(v for v in tariffs.versions("schedule") if v["effective_from"] == "2025-07-01")
    assert out["schedule_version"] == v["id"] and v["source"]["verified"] == "unverified" and v["body"]["cards"]["B-2(b)"]["unverified"]
    with pytest.raises(ValueError, match="No rate card"):  # before any version: a refusal (422), not a server error
        tariffs.card_for("B-2(b)", date(2025, 6, 30))
    with pytest.raises(LookupError):
        tariffs.card_for("B-2(b)", date(2025, 6, 30))


def test_an_existing_company_file_gets_a_newly_shipped_version_once(company):
    tariffs.ensure_shipped()
    with store.connect() as con:  # a file seeded by an earlier build, before the FY2025-26 version shipped
        con.execute("DELETE FROM tariff_versions WHERE kind = 'schedule' AND effective_from = '2025-07-01'")
    tariffs.ensure_shipped()
    tariffs.ensure_shipped()
    assert tariffs.card_for("B-2(b)", date(2025, 12, 20))[1]["effective_from"] == "2025-07-01"
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM tariff_versions").fetchone()[0] == len(tariff_data.SHIPPED)
