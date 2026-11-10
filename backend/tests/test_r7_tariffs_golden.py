"""Hand-worked golden bills and prices for the tariff engine (audit engine-tariffs-07, with tracker-and-docs-07 and tests-and-perf-03).

Plan Part 6.3's acceptance asks for bills worked by hand from the SRO rates, every line in R9 section 7's order and matched to the paisa: "a fixture
produced by the engine is not a golden". Each figure below was worked from the rates R9 reports (SRO 279(I)/2026's cards, the adders billed in the
month, the tax rules of R9 section 7) by the arithmetic written in the test's docstring, never by running the engine; the test then asserts the
engine's bill line by line. The cases:

- TAR-06 and VER-04, the four bills of Part 6.3: a B-2(b) ICP month whose reading period spans the November-to-December window change, an A-2(c)
  net-billing month with an export credit and GST, a legacy net-metering month, and a flat A-3 month with the per-consumer fixed charge (also entered
  as a DISCO bill and matched, VER-04);
- TAR-02, a golden per regime: net billing (with the credit carried to the next bill), legacy net metering, flat, none (exports earn nothing) and
  off-grid (no grid prices);
- TAR-03 and OPT-06, the ICP cases: a peak period whose month ends above its baseline, an off-peak one that ends below, and the uncertain middle,
  with the bill engine's marginal price agreeing with the optimiser's;
- TAR-01, peak membership at every season boundary of both calendars and in a window that crosses midnight;
- TAR-04 and TAR-05: fixed 30-minute MDI blocks, the floor, the PF penalty and the gross-up; the surcharge and tax stack line by line (DSS, the
  Neelum-Jhelum line, provincial duty, Punjab's per-unit duty, GST, income tax, further and extra tax, PTV and MUCT).
- engine-money-live-15: the dated diesel price of each day (R9 section 9.1), none before the first entry, and a genset day's cost priced with it.

R9 section 7's order of a bill: (1) energy by period, (2) fixed charges, (3) FPA, QTA and surcharges on the units, (4) electricity duty, (5) GST,
(6) income tax s.235, (7) further and extra tax, (8) flat fees, (9) adjustments (here the export credit and a credit carried between bills).
Conventions the hand working follows (R9 sections 7 and 13; each marked there as to be confirmed on a real bill): duty on energy plus adders; GST,
further and extra tax on energy, fixed charges, adders and duties; s.235 on the bill amount including those taxes; the export credit netted after
taxes; fixed charges prorated by days only when the reading interval is more than 4 days off 30. Amounts are rounded to the paisa per line; the
total is rounded once, from the unrounded lines."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from test_fix_savings import make_site
from test_fix_tariffs import SRC, _conn, _minutes, company  # noqa: F401  (company is a fixture)

from gridlytics import sites, tariffs, verify

PKT = timezone(timedelta(hours=5))
DSS = "Debt service surcharge (printed as FC or PHL surcharge)"
FCA_AUG, FCA_JUL, QTA_Q2 = "FCA August 2026", "FCA July 2026", "QTA Q2 CY2026"
ED, GST, S235, FURTHER, EXTRA, FLAT = ("Electricity duty", "GST", "Income tax (s.235)", "Further tax", "Extra tax (not on the Active Taxpayer List)",
                                       "Flat fees (PTV, MUCT)")


def _series(d0: date, days: int, imp, exp=None):
    """1-minute series over whole Pakistan days from d0: imp(minute of day, day index) and exp alike, in kW, as numpy expressions."""
    ts = _minutes(d0, days)
    mod = ((ts + 5 * 3600) % 86400) // 60
    day = (ts - ts[0]) // 86400
    i = np.broadcast_to(np.asarray(imp(mod, day), dtype=float), ts.shape).copy()
    e = np.broadcast_to(np.asarray(exp(mod, day), dtype=float), ts.shape).copy() if exp else np.zeros(len(ts))
    return ts, i, e


def _approve(kind: str, key: str, start: str, body: dict) -> None:
    tariffs.approve(tariffs.propose(kind, key, start, body, SRC, "t")["id"], "t")


def _assert_bill(bill: dict, lines: list[tuple[str, float]], total: float) -> None:
    """The bill's lines, in this order, each to the paisa, and its total."""
    got = [(ln["line"], ln["amount_rs"]) for ln in bill["lines"]]
    assert [n for n, _ in got] == [n for n, _ in lines]
    for (name, amount), (_, want) in zip(got, lines):
        assert amount == pytest.approx(want, abs=0.001), name
    assert bill["total_rs"] == pytest.approx(total, abs=0.001)
    assert sum(a for _, a in got) == pytest.approx(total, abs=0.02)  # the rounded lines add up to the total within rounding


def _line(bill: dict, name: str) -> dict:
    return next(ln for ln in bill["lines"] if ln["line"] == name)


# ------------------------------------------------------------------------------------------------ TAR-06: the four bills of Part 6.3

def test_golden_b2b_icp_month_across_the_november_to_december_window_change(company):
    """B-2(b), XWDISCO, Punjab, sanctioned 400 kW, STRN and ATL, ICP-eligible with a December reference of 14,000 kWh peak and 65,000 off-peak.
    Previous reading 19 Nov 2026, reading 19 Dec 2026: the days billed are 20 Nov to 19 Dec, 30 days, billing month December 2026. The load is 100 kW
    all day plus 200 kW from 17:00 to 18:00. Two FCAs are approved for the December bill as test data (none is notified yet): +1.50 under "fca" and
    -0.40 under "fca_late"; the debt-service surcharge is Rs 3.23 (no QTA is billed in December).

    Periods by the calendar month of each minute (R9 13 step 1): 20-30 Nov (11 days) peak 18:00-22:00 at 100 kW = 400 kWh a day; 1-19 Dec (19 days)
    peak 17:00-21:00, 17:00-18:00 at 300 kW and 18:00-21:00 at 100 kW = 600 kWh a day. Peak 11 x 400 + 19 x 600 = 15,800 kWh; all units
    30 x (24 x 100 + 200) = 78,000; off-peak 62,200. MDI: the 17:00-17:30 block, 300 kW; floor 25 % x 400 = 100 kW; billing demand 300 kW.
    ICP: December 2026 is the package's second year, escalation 1.028^2 = 1.056784. Baselines: peak 14,000 x 1.056784 = 14,794.976 kWh, off-peak
    65,000 x 1.056784 = 68,690.96 kWh. Peak ends above it, incremental 15,800 - 14,794.976 = 1,005.024 kWh; off-peak ends below, none.

    (1) Energy peak       14,794.976 kWh x 35.68 =   527,884.74
        ICP incremental    1,005.024 kWh x 22.98 =    23,095.45
        Energy off-peak   62,200     kWh x 22.83 = 1,420,026.00
    (2) Fixed charges     1,250 x 300 kW (30 days: no proration) = 375,000.00
    (3) FCA +1.50 on all 78,000 kWh (a positive FCA applies to ICP units)          =   117,000.00
        FCA -0.40 on 78,000 - 1,005.024 = 76,994.976 kWh (a negative FCA does not)    =   -30,797.99
        DSS 3.23 on 76,994.976 kWh (not on ICP units)                               =   248,693.77
    (4) Electricity duty 1.5 % of energy and adders 2,305,901.97728                 =    34,588.53
    (5) GST 18 % of 2,305,901.97728 + 375,000 + 34,588.52966 = 2,715,490.50694       =   488,788.29
    (6) Income tax s.235, industrial: 1,950 + 5 % x (3,204,278.79819 - 20,000)     =   161,163.94
    Total 3,365,442.74"""
    _approve("adder", "fca", "2026-12-01", {"name": "FCA (test) December 2026", "rate": 1.50, "billing_months": ["2026-12"],
                                             "exempt": ["lifeline", "prepaid", "evcs"]})
    _approve("adder", "fca_late", "2026-12-01", {"name": "FCA (test, late) December 2026", "rate": -0.40, "billing_months": ["2026-12"],
                                                  "exempt": ["lifeline", "prepaid", "evcs"]})
    conn = _conn("B-2(b)", sanctioned=400.0, extra={"icp": {"eligible": True, "peak": {"12": 14000.0}, "off_peak": {"12": 65000.0}}})
    ts, imp, exp = _series(date(2026, 11, 20), 30, lambda m, d: 100.0 + np.where((m >= 17 * 60) & (m < 18 * 60), 200.0, 0.0))
    bill = tariffs.rebuild(ts, imp, exp, conn, date(2026, 11, 19), date(2026, 12, 19), period_start_exclusive=True)
    assert bill["import_gross_kwh"] == pytest.approx({"peak": 15800.0, "off_peak": 62200.0})
    assert bill["icp_incremental_kwh"] == pytest.approx({"peak": 1005.024, "off_peak": 0.0})
    assert bill["mdi_kw"] == 300.0 and bill["days"] == 30 and bill["billing_month"] == "2026-12"
    _assert_bill(bill, [("Energy peak", 527884.74), ("ICP incremental peak", 23095.45), ("Energy off-peak", 1420026.00), ("Fixed charges", 375000.00),
                        ("FCA (test) December 2026", 117000.00), ("FCA (test, late) December 2026", -30797.99), (DSS, 248693.77), (ED, 34588.53),
                        (GST, 488788.29), (S235, 161163.94)], 3365442.74)
    assert _line(bill, "FCA (test) December 2026")["units_kwh"] == 78000.0 and _line(bill, DSS)["units_kwh"] == pytest.approx(76994.976)


def _a2c_net_billing(regime: str = "net_billing"):
    conn = _conn("A-2(c)", regime, 120.0, extra={"agreement_start": "2026-03-01"}, strn=False)
    ts, imp, exp = _series(date(2026, 10, 1), 31, lambda m, d: np.where((m >= 600) & (m < 900), 0.0, 50.0),
                           lambda m, d: np.where((m >= 600) & (m < 900), 80.0, 0.0))
    return tariffs.rebuild(ts, imp, exp, conn, date(2026, 10, 1), date(2026, 10, 31))


def test_golden_a2c_net_billing_month_with_an_export_credit_and_gst(company):
    """A-2(c) commercial TOU, XWDISCO, Punjab, net billing (agreement from 1 Mar 2026), sanctioned 120 kW, no STRN (further tax applies), ATL.
    October 2026, a calendar month without a reading day: 31 days, billing month October (no proration: 31 is within 4 days of 30). Each day the site
    imports 50 kW except from 10:00 to 15:00, when it exports 80 kW. Adders billed in October: FCA August 2026 1.11, QTA Q2 CY2026 0.5194, DSS 3.23.
    The export price is the NAEPP of SRO 1501(I)/2026 para 23, Rs 8.13 (the net-billing version from 9 Feb 2026).

    Peak 18:00-22:00 (September to November): 31 x 4 h x 50 = 6,200 kWh; off-peak 31 x 15 h x 50 = 23,250 kWh; all 29,450. Exports
    31 x 5 h x 80 = 12,400 kWh. MDI 50 kW (every block), floor 25 % x 120 = 30 kW: billing demand 50 kW.

    (1) Energy peak       6,200 kWh x 43.82 = 271,684.00
        Energy off-peak  23,250 kWh x 35.15 = 817,237.50
    (2) Fixed charges     1,250 x 50 kW    =  62,500.00
    (3) FCA 29,450 x 1.11 = 32,689.50; QTA 29,450 x 0.5194 = 15,296.33; DSS 29,450 x 3.23 = 95,123.50
    (4) Electricity duty 1.5 % x 1,232,030.83 = 18,480.46
    (5) GST 18 % x (1,232,030.83 + 62,500 + 18,480.46245 = 1,313,011.29245) = 236,342.03
    (6) Income tax s.235, commercial: 1,950 + 12 % x (1,588,743.66386 - 20,000) = 190,199.24 (the bill amount includes GST and further tax)
    (7) Further tax 3 % x 1,313,011.29245 = 39,390.34
    (8) PTV fee, commercial: 60.00
    (9) Export credit 12,400 kWh x 8.13 = -100,812.00, after taxes
    Charges 1,779,002.90352; total 1,779,002.90352 - 100,812 = 1,678,190.90"""
    bill = _a2c_net_billing()
    assert bill["import_kwh"] == pytest.approx({"peak": 6200.0, "off_peak": 23250.0}) and sum(bill["export_kwh"].values()) == pytest.approx(12400.0)
    _assert_bill(bill, [("Energy peak", 271684.00), ("Energy off-peak", 817237.50), ("Fixed charges", 62500.00), (FCA_AUG, 32689.50), (QTA_Q2, 15296.33),
                        (DSS, 95123.50), (ED, 18480.46), (GST, 236342.03), (S235, 190199.24), (FURTHER, 39390.34), (FLAT, 60.00),
                        ("Export credit", -100812.00)], 1678190.90)
    assert _line(bill, "Export credit")["rate"] == 8.13 and bill["credits_rs"] == 100812.00 and bill["charges_rs"] == 1779002.90


def test_golden_legacy_net_metering_month(company):
    """B-2(b) industrial TOU, XWDISCO, Punjab, legacy net metering (agreement 1 Jan 2020 to 31 Dec 2027, by-period netting), sanctioned 300 kW, STRN
    and ATL; the bill before carried a credit of Rs 12,345.67. October 2026, 31 days. Each day the site imports 100 kW at peak (18:00-22:00), exports
    150 kW from 10:00 to 15:00 and imports 20 kW the other 15 off-peak hours. The surplus is credited at the notified NAPPP, Rs 25.32 (SRO
    1501(I)/2026 para 23, the legacy version from 1 Jan 2026); adders as in October: FCA 1.11, QTA 0.5194, DSS 3.23.

    Netting by period (R9 8.3): peak imports 31 x 4 x 100 = 12,400 kWh, no peak export: 12,400 billed. Off-peak imports 31 x 15 x 20 = 9,300 kWh
    against exports 31 x 5 x 150 = 23,250 kWh: nothing billed, a surplus of 13,950 kWh. MDI 100 kW (the peak blocks); floor 25 % x 300 = 75 kW.

    (1) Energy peak      12,400 kWh x 35.68 = 442,432.00
        Energy off-peak       0 kWh x 22.83 =       0.00
    (2) Fixed charges     1,250 x 100 kW   = 125,000.00
    (3) On the 12,400 billed units: FCA 13,764.00; QTA 6,440.56; DSS 40,052.00
    (4) Electricity duty 1.5 % x 502,688.56 = 7,540.33
    (5) GST 18 % x (502,688.56 + 125,000 + 7,540.3284 = 635,228.8884) = 114,341.20
    (6) Income tax s.235, industrial: 1,950 + 5 % x (749,570.08831 - 20,000) = 38,428.50
    (9) Export credit, the surplus 13,950 kWh x 25.32 = -353,214.00; credit brought forward -12,345.67
    Charges 787,998.59273; total 787,998.59273 - 353,214 - 12,345.67 = 422,438.92"""
    conn = _conn("B-2(b)", "net_metering_legacy", 300.0, extra={"agreement_start": "2020-01-01", "agreement_end": "2027-12-31"})
    ts, imp, exp = _series(date(2026, 10, 1), 31, lambda m, d: np.where((m >= 1080) & (m < 1320), 100.0, np.where((m >= 600) & (m < 900), 0.0, 20.0)),
                           lambda m, d: np.where((m >= 600) & (m < 900), 150.0, 0.0))
    bill = tariffs.rebuild(ts, imp, exp, conn, date(2026, 10, 1), date(2026, 10, 31), credit_brought_forward_rs=12345.67)
    assert bill["import_gross_kwh"] == pytest.approx({"peak": 12400.0, "off_peak": 9300.0})
    assert bill["import_kwh"] == pytest.approx({"peak": 12400.0, "off_peak": 0.0})
    _assert_bill(bill, [("Energy peak", 442432.00), ("Energy off-peak", 0.00), ("Fixed charges", 125000.00), (FCA_AUG, 13764.00), (QTA_Q2, 6440.56),
                        (DSS, 40052.00), (ED, 7540.33), (GST, 114341.20), (S235, 38428.50), ("Export credit", -353214.00),
                        ("Credit brought forward", -12345.67)], 422438.92)
    assert _line(bill, "Export credit")["units_kwh"] == 13950.0 and _line(bill, "Export credit")["rate"] == 25.32
    assert bill["credit_carried_rs"] == 0.0


A3_TOTAL = 1680138.30


def test_golden_flat_a3_month_with_the_per_consumer_fixed_charge(company):
    """A-3 general services, a flat card (no time of use), XWDISCO, Punjab, the regime "flat", STRN and ATL; class commercial. Previous reading
    25 Aug 2026, reading 30 Sep 2026: the days billed are 26 Aug to 30 Sep, 36 days (6 more than 30, so the fixed charge is prorated by days, R9
    section 4), billing month September 2026. A steady 30 kW. Adders billed in September: FCA July 2026 2.0581, QTA Q2 CY2026 0.5194, DSS 3.23.

    Units 36 x 24 x 30 = 25,920 kWh, one bucket.

    (1) Energy            25,920 kWh x 42.48 = 1,101,081.60
    (2) Fixed charges     1,000 per consumer x 36 / 30 = 1,200.00
    (3) FCA 25,920 x 2.0581 = 53,345.95; QTA 25,920 x 0.5194 = 13,462.85; DSS 25,920 x 3.23 = 83,721.60
    (4) Electricity duty 1.5 % x 1,251,612.00 = 18,774.18
    (5) GST 18 % x (1,251,612 + 1,200 + 18,774.18 = 1,271,586.18) = 228,885.51
    (6) Income tax s.235, commercial: 1,950 + 12 % x (1,500,471.6924 - 20,000) = 179,606.60
    (8) PTV fee, commercial: 60.00
    Total 1,680,138.30"""
    conn = _conn("A-3", "flat", 40.0)
    ts, imp, exp = _series(date(2026, 8, 26), 36, lambda m, d: 30.0)
    bill = tariffs.rebuild(ts, imp, exp, conn, date(2026, 8, 25), date(2026, 9, 30), period_start_exclusive=True)
    assert bill["days"] == 36 and bill["billing_month"] == "2026-09"
    _assert_bill(bill, [("Energy", 1101081.60), ("Fixed charges", 1200.00), (FCA_JUL, 53345.95), (QTA_Q2, 13462.85), (DSS, 83721.60), (ED, 18774.18),
                        (GST, 228885.51), (S235, 179606.60), (FLAT, 60.00)], A3_TOTAL)
    fixed = _line(bill, "Fixed charges")
    assert (fixed["days"], fixed["of_days"], fixed["billing_demand_kw"]) == (36, 30, 0.0)


def test_the_hand_worked_a3_bill_entered_as_a_disco_bill_matches(company):
    """VER-04: the A-3 bill above entered as a DISCO bill with its printed reading dates and total, over a site's own 1-minute meter rows, is rebuilt
    to the same total: no difference, inside the 2 % check."""
    from test_fix_r2_savings import _flat_rows
    sid = make_site(name="Golden A3", conn={"tariff_category": "A-3", "sanctioned_load_kw": 40, "regime": "flat"})
    _flat_rows(sid, date(2026, 8, 26), 36, 30.0)
    b = verify.add_bill(sid, {"previous_reading_date": "2026-08-25", "period_end": "2026-09-30", "total_rs": A3_TOTAL, "units_off_peak": 25920}, "t")
    assert b["rebuilt"]["total_rs"] == A3_TOTAL and b["difference_pct"] == 0.0
    assert b["check"]["verdict"] == "within" and b["rebuilt"]["completeness"] == 1.0
    assert sites.connections(sid)[0]["data"]["regime"] == "flat"


# ------------------------------------------------------------------------------------------------ TAR-02: a golden per regime

def test_golden_regime_none_earns_nothing_for_exports(company):
    """The A-2(c) month above under the regime "none" (no export agreement): the same charges, 1,779,002.90 (the sum of lines 1 to 8 worked in the
    net-billing golden), and no export credit for the 12,400 kWh sent out."""
    bill = _a2c_net_billing("none")
    assert not any(ln["line"].startswith("Export credit") for ln in bill["lines"])
    assert bill["credits_rs"] == 0.0 and bill["total_rs"] == pytest.approx(1779002.90, abs=0.001)


def test_golden_net_billing_credit_beyond_the_bill_is_carried_forward(company):
    """A-3 under net billing (agreement in force), October 2026, STRN and ATL: a 2 kW import all month and a 100 kW export from 10:00 to 15:00. The
    bill does not go below zero; the excess credit is carried to the next bill (R9 13 step 9; Prosumer reg. 14(2)).

    Units 31 x 24 x 2 = 1,488 kWh; exports 31 x 5 x 100 = 15,500 kWh.
    (1) Energy 1,488 x 42.48 = 63,210.24  (2) Fixed charges 1,000.00 (31 days: no proration)
    (3) FCA 1,488 x 1.11 = 1,651.68; QTA 1,488 x 0.5194 = 772.87; DSS 1,488 x 3.23 = 4,806.24
    (4) Electricity duty 1.5 % x 70,441.0272 = 1,056.62
    (5) GST 18 % x 72,497.642608 = 13,049.58
    (6) Income tax s.235, commercial: 1,950 + 12 % x (85,547.21828 - 20,000) = 9,815.67
    (8) PTV 60.00. Charges 95,422.88447.
    (9) Export credit 15,500 x 8.13 = -126,015.00; credit carried to the next bill 126,015 - 95,422.88447 = 30,592.12. Total 0.00"""
    conn = _conn("A-3", "net_billing", 40.0, extra={"agreement_start": "2026-03-01"})
    ts, imp, exp = _series(date(2026, 10, 1), 31, lambda m, d: 2.0, lambda m, d: np.where((m >= 600) & (m < 900), 100.0, 0.0))
    bill = tariffs.rebuild(ts, imp, exp, conn, date(2026, 10, 1), date(2026, 10, 31))
    _assert_bill(bill, [("Energy", 63210.24), ("Fixed charges", 1000.00), (FCA_AUG, 1651.68), (QTA_Q2, 772.87), (DSS, 4806.24), (ED, 1056.62),
                        (GST, 13049.58), (S235, 9815.67), (FLAT, 60.00), ("Export credit", -126015.00), ("Credit carried to the next bill", 30592.12)],
                 0.0)
    assert bill["credit_carried_rs"] == 30592.12 and bill["charges_rs"] == 95422.88


def test_golden_off_grid_has_no_grid_prices(company):
    """Off-grid: no grid, so every import and export price is zero, the grid is unavailable at every step and there is no MDI term."""
    v = tariffs.vectors(_conn("B-2(b)", "off_grid"), datetime(2026, 10, 2, tzinfo=PKT).astimezone(timezone.utc), 96)
    assert not v["c_imp"].any() and not v["c_exp"].any() and not v["grid"].any() and v["mdi"] is None and v["icp"] == {}


# ------------------------------------------------------------------------------------------------ TAR-05 and TAR-04: the surcharge and tax stack, MDI

def test_golden_k_electric_commercial_bill_with_sindh_duty_further_tax_ptv_and_muct(company):
    """A-2(c) commercial at K-Electric, Sindh, no STRN, ATL, regime none, sanctioned 200 kW; October 2026, 31 days. KE's peak window April to October
    is 18:30-22:30 (R9 2.2). The load is 60 kW all day plus 40 kW from 18:00 to 19:00 and from 22:00 to 23:00.

    Peak per day: 60 x 4 + 40 x 0.5 (18:30-19:00) + 40 x 0.5 (22:00-22:30) = 280 kWh; all units 60 x 24 + 40 x 2 = 1,520 kWh. Peak 8,680 kWh,
    off-peak 38,440, all 47,120. MDI 100 kW, floor 50 kW.
    (1) Energy peak 8,680 x 43.82 = 380,357.60; off-peak 38,440 x 35.15 = 1,351,166.00
    (2) Fixed charges 1,250 x 100 = 125,000.00
    (3) FCA 47,120 x 1.11 = 52,303.20; QTA 47,120 x 0.5194 = 24,474.13; DSS 47,120 x 3.23 = 152,197.60
    (4) Electricity duty, Sindh commercial 2 % (KE page) x 1,960,498.528 = 39,209.97
    (5) GST 18 % x 2,124,708.49856 = 382,447.53
    (6) Income tax s.235, commercial: 1,950 + 12 % x (2,570,897.28326 - 20,000) = 308,057.67
    (7) Further tax 3 % x 2,124,708.49856 = 63,741.25
    (8) Flat fees: PTV 60 + MUCT 750 (Karachi, commercial) = 810.00
    Total 2,879,764.96"""
    conn = _conn("A-2(c)", sanctioned=200.0, extra={"disco_group": "ke"}, strn=False, province="sindh")
    ts, imp, exp = _series(date(2026, 10, 1), 31, lambda m, d: 60.0 + np.where(((m >= 1080) & (m < 1140)) | ((m >= 1320) & (m < 1380)), 40.0, 0.0))
    bill = tariffs.rebuild(ts, imp, exp, conn, date(2026, 10, 1), date(2026, 10, 31))
    assert bill["import_kwh"] == pytest.approx({"peak": 8680.0, "off_peak": 38440.0})
    _assert_bill(bill, [("Energy peak", 380357.60), ("Energy off-peak", 1351166.00), ("Fixed charges", 125000.00), (FCA_AUG, 52303.20), (QTA_Q2, 24474.13),
                        (DSS, 152197.60), (ED, 39209.97), (GST, 382447.53), (S235, 308057.67), (FURTHER, 63741.25), (FLAT, 810.00)], 2879764.96)


def _b3_heavy(sanctioned: float = 600.0):
    tax = dict(tariffs.tax_rules(date(2026, 10, 1)))
    tax["extra_tax_non_atl"] = [{"upto": 20000, "rate": 0.05}, {"upto": 100000, "rate": 0.10}, {"upto": None, "rate": 0.17}]
    if tariffs.resolve("tax", "national", date(2026, 10, 1))["body"].get("extra_tax_non_atl") is None:
        _approve("tax", "national", "2026-10-01", tax)
        _approve("adder", "nj", "2026-10-01", {"name": "Neelum-Jhelum surcharge", "rate": 0.10, "billing_months": "all", "exempt": ["lifeline"]})
    conn = _conn("B-3", sanctioned=sanctioned, extra={"sanctioned_kva": 800}, strn=False, atl=False)
    # 400 kW all month, and 700 kW from 10:15 to 10:45 on 14 October: a sliding 30 minutes would read 700 kW, the fixed blocks read 550
    ts, imp, exp = _series(date(2026, 10, 1), 31, lambda m, d: 400.0 + np.where((d == 13) & (m >= 615) & (m < 645), 300.0, 0.0))
    return tariffs.rebuild(ts, imp, exp, conn, date(2026, 10, 1), date(2026, 10, 31), power_factor_pct=85.0)


def test_golden_punjab_heavy_industrial_bill_with_every_surcharge_and_tax(company):
    """B-3 industrial, XWDISCO, Punjab, sanctioned 600 kW and 800 kVA (above 500 kVA: Punjab's 4 paisa a unit applies), no STRN, not on the Active
    Taxpayer List, power factor 85 %; October 2026, 31 days. Test data approved from 1 Oct 2026: the Neelum-Jhelum surcharge at Rs 0.10 (R9 7:
    optional, unverified on a 2026 bill) and extra-tax slabs of 5 % to Rs 20,000, 10 % to Rs 100,000 and 17 % above (R9 7: 5 to 17 % by slab,
    bounds unverified). The load is 400 kW all month and 700 kW from 10:15 to 10:45 on 14 October.

    MDI (TAR-04): 30-minute blocks fixed to the clock: 10:00-10:30 (15 x 400 + 15 x 700) / 30 = 550 kW, 10:30-11:00 also 550 kW (a sliding window
    would read 700). Floor 25 % x 600 = 150 kW; billing demand 550 kW. PF penalty (R9 3): 1 + 2 % x (90 - 85) = 1.10.
    Units: 31 x 24 x 400 + 300 x 0.5 = 297,750 kWh; peak 31 x 4 x 400 = 49,600, off-peak 248,150.
    (1) Energy peak 49,600 x 35.68 = 1,769,728.00; off-peak 248,150 x 23.67 = 5,873,710.50
    (2) Fixed charges 1,250 x 550 x 1.10 = 756,250.00
    (3) FCA 297,750 x 1.11 = 330,502.50; QTA x 0.5194 = 154,651.35; DSS x 3.23 = 961,732.50; Neelum-Jhelum x 0.10 = 29,775.00
    (4) Electricity duty 1.5 % x 9,120,099.85 = 136,801.50; Punjab per-unit duty 297,750 x 0.04 = 11,910.00
    (5) GST 18 % x (9,120,099.85 + 756,250 + 136,801.49775 + 11,910 = 10,025,061.34775) = 1,804,511.04
    (6) Income tax s.235, industrial: 1,950 + 5 % x (13,834,584.6599 - 20,000) = 692,679.23
    (7) Further tax 3 % x 10,025,061.34775 = 300,751.84; extra tax at the top slab 17 % x 10,025,061.34775 = 1,704,260.43
    Total 14,527,263.89"""
    bill = _b3_heavy()
    _assert_bill(bill, [("Energy peak", 1769728.00), ("Energy off-peak", 5873710.50), ("Fixed charges", 756250.00), (FCA_AUG, 330502.50),
                        (QTA_Q2, 154651.35), (DSS, 961732.50), ("Neelum-Jhelum surcharge", 29775.00), (ED, 136801.50),
                        ("Punjab electricity duty (per unit, above 500 kVA)", 11910.00), (GST, 1804511.04), (S235, 692679.23), (FURTHER, 300751.84),
                        (EXTRA, 1704260.43)], 14527263.89)
    fixed = _line(bill, "Fixed charges")
    assert (bill["mdi_kw"], fixed["billing_demand_kw"], fixed["pf_factor"]) == (550.0, 550.0, 1.1)


def test_golden_the_floor_sets_billing_demand_above_the_mdi(company):
    """TAR-04: the same month on a sanctioned load of 2,400 kW: the floor 25 % x 2,400 = 600 kW is above the 550 kW MDI, so the fixed charge is
    1,250 x 600 x 1.10 = 825,000.00."""
    fixed = _line(_b3_heavy(2400.0), "Fixed charges")
    assert (fixed["billing_demand_kw"], fixed["mdi_kw"], fixed["amount_rs"]) == (600.0, 550.0, 825000.00)


def test_golden_the_optimisers_mdi_rate_is_grossed_up_by_the_taxes_the_company_bears(company):
    """TAR-04's gross-up: a B-2(b) connection that cannot recover its GST nor adjust its s.235 bears, per kW of billing demand, the fixed charge with
    GST and the s.235 marginal 5 % on it: 1,250 x (1 + 0.18 + 0.05 x 1.18) = 1,548.75 Rs/kW. Floor 25 % x 400 = 100 kW; blocks of 30 minutes are two
    15-minute steps."""
    conn = _conn("B-2(b)", sanctioned=400.0, gst_recoverable=False, s235_adjustable=False)
    mdi = tariffs.vectors(conn, datetime(2026, 10, 2, tzinfo=PKT).astimezone(timezone.utc), 96)["mdi"]
    assert mdi["rate_rs_per_kw"] == pytest.approx(1548.75, abs=1e-9) and mdi["floor_kw"] == 100.0 and mdi["interval_steps"] == 2


# ------------------------------------------------------------------------------------------------ TAR-03 and OPT-06: the three ICP cases

ICP_CONN = {"icp": {"eligible": True, "peak": {"10": 10000.0}, "off_peak": {"10": 60000.0}}}
ICP_ABOVE = 24.45135      # (22.98 + positive FCA 1.11; no QTA or DSS on ICP units) x (1 + 1.5 % duty)
TOU_BELOW_OFF = 28.104741  # (22.83 + FCA 1.11 + QTA 0.5194 + DSS 3.23) x 1.015
TOU_PEAK = 41.147491       # (35.68 + 4.8594) x 1.015


def test_golden_icp_cases_above_below_and_uncertain(company):
    """B-2(b), ICP-eligible, October 2026 (package year 1: escalation 1.028), reference peak 10,000 and off-peak 60,000 kWh: baselines 10,280 and
    61,680 kWh. The horizon is 2 Oct, 96 steps: 16 peak steps (18:00-22:00) and 80 off-peak. With a grid limit of 100 kW the swing a horizon can
    still make is 100 x 16 x 0.25 = 400 kWh at peak and 100 x 80 x 0.25 = 2,000 kWh off-peak (plan 4.3).
    Above: peak 5,000 so far + 6,000 forecast = 11,000; 11,000 - 10,280 = 720 > 400. Every peak kWh is an ICP kWh: (22.98 + 1.11) x 1.015 = 24.45135.
    Below: off-peak 30,000 + 25,000 = 55,000; 61,680 - 55,000 = 6,680 > 2,000. The TOU price: (22.83 + 1.11 + 0.5194 + 3.23) x 1.015 = 28.104741.
    Uncertain: peak 5,000 + 5,500 = 10,500; |10,500 - 10,280| = 220 < 400. The step carries the TOU price (35.68 + 4.8594) x 1.015 = 41.147491 and
    the ICP rate 24.45135 for the month-level binary."""
    conn = _conn("B-2(b)", extra=ICP_CONN)
    t0 = datetime(2026, 10, 2, tzinfo=PKT).astimezone(timezone.utc)
    v = tariffs.vectors(conn, t0, 96, {"peak": 5000.0, "off_peak": 30000.0}, {"peak": 6000.0, "off_peak": 25000.0}, 100.0)
    assert (v["icp"]["peak"]["case"], v["icp"]["off_peak"]["case"]) == ("above", "below")
    assert v["icp"]["peak"]["baseline_kwh"] == pytest.approx(10280.0) and v["icp"]["off_peak"]["baseline_kwh"] == pytest.approx(61680.0)
    assert v["c_imp"][76] == pytest.approx(ICP_ABOVE, abs=1e-9) and v["peak"][76]           # 19:00
    assert v["c_imp"][12] == pytest.approx(TOU_BELOW_OFF, abs=1e-9) and not v["peak"][12]   # 03:00
    u = tariffs.vectors(conn, t0, 96, {"peak": 5000.0, "off_peak": 30000.0}, {"peak": 5500.0, "off_peak": 25000.0}, 100.0)
    assert u["icp"]["peak"]["case"] == "uncertain" and u["icp"]["peak"]["rate"] == pytest.approx(ICP_ABOVE, abs=1e-9)
    assert u["c_imp"][76] == pytest.approx(TOU_PEAK, abs=1e-9)


def test_golden_the_bill_engine_prices_an_icp_month_at_the_optimisers_price(company):
    """OPT-06, the binary case agrees with the bill engine: an October month of 100 kW (with one 150 kW block for the MDI) ends its peak above the
    baseline (12,400 > 10,280 kWh) and its off-peak below (62,025 < 71,960 for a 70,000 kWh reference). One more kWh at 19:05 costs the company
    24.45135 (the ICP price above); one more at 03:05 costs 28.104741 (the TOU price below): GST and s.235 are recovered, so neither is borne."""
    conn = _conn("B-2(b)", extra={"icp": {"eligible": True, "peak": {"10": 10000.0}, "off_peak": {"10": 70000.0}}})
    ts, imp, exp = _series(date(2026, 10, 1), 31, lambda m, d: 100.0 + np.where((d == 19) & (m >= 600) & (m < 630), 50.0, 0.0))

    def bump(minute_of_day: int) -> float:
        base = tariffs.rebuild(ts, imp, exp, conn, date(2026, 10, 1), date(2026, 10, 31))["bearable_rs"]
        imp2 = imp.copy()
        imp2[4 * 1440 + minute_of_day] += 60.0
        return tariffs.rebuild(ts, imp2, exp, conn, date(2026, 10, 1), date(2026, 10, 31))["bearable_rs"] - base

    assert bump(19 * 60 + 5) == pytest.approx(ICP_ABOVE, abs=0.011)
    assert bump(3 * 60 + 5) == pytest.approx(TOU_BELOW_OFF, abs=0.011)


# ------------------------------------------------------------------------------------------------ TAR-01: peak membership at every boundary

#: (calendar key, day, its peak window) on each side of every season boundary, from R9 2.1 (XWDISCOs, the 2023 Terms and Conditions) and 2.2 (KE)
BOUNDARIES = [
    ("xwdisco", date(2026, 2, 28), "17:00", "21:00"), ("xwdisco", date(2026, 3, 1), "18:00", "22:00"),
    ("xwdisco", date(2026, 5, 31), "18:00", "22:00"), ("xwdisco", date(2026, 6, 1), "19:00", "23:00"),
    ("xwdisco", date(2026, 8, 31), "19:00", "23:00"), ("xwdisco", date(2026, 9, 1), "18:00", "22:00"),
    ("xwdisco", date(2026, 11, 30), "18:00", "22:00"), ("xwdisco", date(2026, 12, 1), "17:00", "21:00"),
    ("ke", date(2026, 3, 31), "18:00", "22:00"), ("ke", date(2026, 4, 1), "18:30", "22:30"),
    ("ke", date(2026, 10, 31), "18:30", "22:30"), ("ke", date(2026, 11, 1), "18:00", "22:00"),
]


def _hhmm(s: str) -> int:
    return int(s[:2]) * 60 + int(s[3:])


def _edges(key: str, day: date) -> list[int]:
    """The minutes to test on a boundary day: either side of both its own window's edges and those of the neighbouring season's."""
    out = set()
    for k, d, a, b in BOUNDARIES:
        if k == key and abs((d - day).days) <= 1:
            for m in (_hhmm(a), _hhmm(b)):
                out |= {m - 1, m}
    return sorted(out)


def _one_day_peak_kwh(conn: dict, day: date, minutes: list[int]) -> float:
    """The bill engine's peak kWh for one day with 2^i kWh drawn in the i-th of `minutes` and nothing else: the total names exactly the minutes the
    bill puts at peak."""
    ts, imp, exp = _series(day, 1, lambda m, d: 0.0)
    for i, m in enumerate(minutes):
        imp[m] = 60.0 * 2 ** i
    return tariffs.rebuild(ts, imp, exp, conn, day, day)["import_kwh"]["peak"]


@pytest.mark.parametrize("key,day,start,end", BOUNDARIES, ids=[f"{k}-{d.isoformat()}" for k, d, _, _ in BOUNDARIES])
def test_golden_peak_membership_at_every_season_boundary(company, key, day, start, end):
    """Half-open windows [start, end) by the calendar month of the minute (R9 2.3 and 13 step 1): the minute before the start is off-peak, the start
    and the minute before the end are peak, the end is off-peak; the neighbouring season's edges fall by this day's own window. The calendar, the bill
    engine and the optimiser's 15-minute vector agree."""
    conn = _conn("B-2(b)", extra={"disco_group": key})
    a, b = _hhmm(start), _hhmm(end)
    minutes = _edges(key, day)
    expected = {m: a <= m < b for m in minutes}
    cal = tariffs.calendar_for(conn, day)
    for m, peak in expected.items():
        assert tariffs.is_peak_local(cal, datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=PKT)) == peak, m
    assert _one_day_peak_kwh(conn, day, minutes) == pytest.approx(sum(2 ** i for i, m in enumerate(minutes) if expected[m]))
    v = tariffs.vectors(conn, datetime(day.year, day.month, day.day, tzinfo=PKT).astimezone(timezone.utc), 96)
    assert [i for i in range(96) if v["peak"][i]] == list(range(a // 15, b // 15))


@pytest.mark.parametrize("key", ["xwdisco", "ke"])
def test_golden_a_peak_window_across_midnight_follows_the_month_of_each_minute(company, key):
    """A calendar version (test data, approved from 1 Jan 2026) whose windows cross midnight: January to November 22:00-01:00, December 23:00-03:00.
    On 30 Nov 00:30 and 23:30 are peak (November's window), 01:00 is not; on 1 Dec 01:30 and 02:59 are peak and 03:00 is not (December's window,
    though the evening before was November's), and 22:30 is off-peak (it would be peak in November's window), 23:00 peak."""
    _approve("tou", key, "2026-01-01", {"seasons": [{"months": list(range(1, 12)), "peak": ["22:00", "01:00"]},
                                                    {"months": [12], "peak": ["23:00", "03:00"]}], "dst_adjust": False})
    conn = _conn("B-2(b)", extra={"disco_group": key})
    cases = {date(2026, 11, 30): {30: True, 59: True, 60: False, 1319: False, 1320: True, 1410: True},
             date(2026, 12, 1): {90: True, 179: True, 180: False, 1350: False, 1379: False, 1380: True}}
    for day, expected in cases.items():
        cal = tariffs.calendar_for(conn, day)
        for m, peak in expected.items():
            assert tariffs.is_peak_local(cal, datetime(day.year, day.month, day.day, m // 60, m % 60, tzinfo=PKT)) == peak, (day, m)
        minutes = sorted(expected)
        assert _one_day_peak_kwh(conn, day, minutes) == pytest.approx(sum(2 ** i for i, m in enumerate(minutes) if expected[m])), day


# ------------------------------------------------------------------------------------------------ engine-money-live-15: the dated diesel price

#: R9 section 9.1's daily HSD prices (OGRA notifications, Rs per litre), each in force from its day until the next entry
HSD_BY_DAY = {date(2026, 10, 30): 400.67, date(2026, 10, 31): 402.96, date(2026, 11, 1): 402.96, date(2026, 11, 2): 402.96,
              date(2026, 11, 3): 395.20, date(2026, 11, 4): 391.52, date(2026, 11, 5): 387.81, date(2026, 11, 6): 382.25, date(2026, 11, 9): 382.25}


def test_golden_the_diesel_price_of_each_day_and_none_before_the_first(company):
    """R9 section 9.1: HSD Rs 400.67 from 30 Oct 2026, 402.96 from 31 Oct, 395.20 from 3 Nov, 391.52 from 4 Nov, 387.81 from 5 Nov and 382.25 from
    6 Nov. A day takes the latest price on or before it: 1 and 2 Nov have no entry of their own and keep 31 Oct's 402.96, 9 Nov keeps 6 Nov's 382.25.
    29 Oct is before the first entry: it has no fuel price. The first one (400.67, from 30 Oct) stands in, so that a plan never runs on free fuel,
    and only with a flag that says so (before: the same 400.67, silently)."""
    for day, price in HSD_BY_DAY.items():
        p, src = tariffs.fuel_price("hsd", day)
        assert p == price and not src.get("stands_in"), day
        assert src["kind"] == "ogra", day
    p, src = tariffs.fuel_price("hsd", date(2026, 10, 29))
    assert p == 400.67
    assert src["stands_in"] is True and src["first_from"] == "2026-10-30"
    assert src["note"] == "No fuel price for this day: the first entered price, from 30 Oct 2026, stands in"
    tariffs.add_fuel_price("hsd", "2026-10-01", 380.10, "t")  # a price entered for October: the day now has its own
    p, src = tariffs.fuel_price("hsd", date(2026, 10, 29))
    assert p == 380.10 and not src.get("stands_in")


def test_golden_a_genset_day_is_priced_with_that_days_diesel(company):
    """One run of 2 hours (one start) at 150 kW burning 0.5 L a minute: 60 L and 300 kWh. Start Rs 500, Rs 200 a run hour, O&M Rs 2 a kWh:
    1 x 500 + 2 x 200 + 300 x 2 = Rs 1,500.00 besides the fuel.
    - 3 Nov 2026: 60 x 395.20 = 23,712.00, total Rs 25,212.00;
    - 2 Nov 2026 (no entry that day, 31 Oct's 402.96): 60 x 402.96 = 24,177.60, total Rs 25,677.60;
    - 29 Oct 2026 (no fuel price; 30 Oct's 400.67 stands in, flagged): 60 x 400.67 = 24,040.20, total Rs 25,540.20, an estimate the day does not
      claim on (test_fix_r7_tariffs)."""
    site = {"assets": {"genset": {"fuel_kind": "hsd", "c_start_rs": 500.0, "c_runhr_rs": 200.0, "c_om_rs_per_kwh": 2.0}}}
    on = np.zeros(1440, dtype=bool)
    on[600:720] = True
    sim = {"gen_on": on, "fuel": np.where(on, 0.5, 0.0), "gen": np.where(on, 150.0, 0.0)}
    for day, total in ((date(2026, 11, 3), 25212.00), (date(2026, 11, 2), 25677.60), (date(2026, 10, 29), 25540.20)):
        cost, litres = verify._genset_cost(site, sim, day)
        assert litres == pytest.approx(60.0)
        assert round(cost, 2) == total, day
