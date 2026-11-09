"""Round-7 regression tests for the tariff engine (audit work package "tariffs"): the price vector and the bill engine (tariffs.py, tariff_data.py).

Batch 1, price vector correctness:
- flow-onboarding-01 (with engine-money-live-04): export earns nothing before an agreement starts or after a net-billing agreement ends; after a
  legacy net-metering agreement ends the site is billed under net billing (exports at the NAEPP, nothing netted), not at the import rate; in the
  optimiser's vector, the plan, the bill and the ledger; the Tariffs page lists each agreement with its state and the regime billed;
- engine-tariffs-03: every FCA line (any key that names an FCA, not only "fca") prices ICP units as the bill does, with the negative-FCA exemption;
- engine-tariffs-04: a legacy net-metering surplus is credited at the notified NAPPP (SRO 1501(I)/2026 para 23), not the off-peak rate;
- engine-tariffs-11: the debt-service surcharge is 43 paisa for agricultural tube wells (R9 section 7), and an adder's exemptions name categories;
- engine-tariffs-12: a TOU calendar must cover each month exactly once with a non-empty peak window.

Batch 2, bill rebuild and bill comparison:
- engine-tariffs-01: a bill across a new schedule prices each day with the versions in force that day (pro rata by days), as the vectors do;
- engine-tariffs-02: an export credit beyond the bill is carried to the next bill, and the cost borne counts the month's own credit;
- engine-tariffs-08: the power-factor penalty, Punjab's per-unit duty above 500 kVA, extra tax off the Active Taxpayer List (or "not priced"),
  a credit netted before tax and a TOU export schedule, every version's hash and the series hash;
- engine-tariffs-10: ICP at a legacy net-metering site caps the increments on imported units at the month's net import, pro rata;
- pages-D-10 (with flow-savings-pilot-bill-07): the printed units and MDI are compared line by line, and a line off by more than 2 % fails;
- flow-savings-pilot-bill-08 (with engine-money-live-08): gaps are filled from the profile, and the verdict is withheld below 90 % data;
- flow-savings-pilot-bill-23: an overlapping bill is refused, a bill can be corrected or deleted, and the fixed charge shows its proration.

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


# ------------------------------------------------------------------------------------------------ flow-onboarding-01: agreement dates

def test_export_earns_nothing_before_the_agreement_starts_or_after_it_ends(company):
    naepp = tariffs.prosumer("net_billing", NOV)["naepp"]
    assert max(_vec(_agreement("net_billing", start="2026-03-01", end="2033-02-28"))["c_exp"]) == pytest.approx(naepp)
    assert max(_vec(_agreement("net_billing", start="2019-03-01", end="2026-02-28"))["c_exp"]) == 0.0       # ended in February
    assert max(_vec(_agreement("net_billing", start="2027-01-01"))["c_exp"]) == 0.0                          # not started yet
    assert max(_vec(_agreement("net_billing", start="banana"))["c_exp"]) == 0.0                              # unreadable: not credited
    assert max(_vec(_agreement("net_billing"))["c_exp"]) == pytest.approx(naepp)                             # no dates entered: in force
    # an ended legacy agreement moves the site to net billing (engine-money-live-04): nothing netted, imports priced as without a regime, exports at
    # the NAEPP (before: about the import rate, 39.48 to 48.28 Rs/kWh at A-2(c))
    ended, none = _vec(_agreement("net_metering_legacy", "A-2(c)", "2018-01-01", "2025-01-01")), _vec(_conn("A-2(c)"))
    assert np.allclose(ended["c_imp"], none["c_imp"]) and np.allclose(ended["c_exp"], naepp)
    assert not ended["export_tie_break"].any()
    assert max(_vec(_agreement("net_metering_legacy", "A-2(c)", "2027-01-01"))["c_exp"]) == 0.0              # a legacy agreement not started
    assert max(_vec(_agreement("net_metering_legacy", "A-2(c)", end="2026-13-01"))["c_exp"]) == 0.0         # unreadable
    live = _vec(_agreement("net_metering_legacy", "A-2(c)", "2019-07-01"))
    assert np.allclose(live["c_exp"], live["c_imp"] - tariffs.LEGACY_EXPORT_TIE_BREAK)


def test_a_horizon_across_the_agreements_last_day_stops_the_credit_at_midnight(company):
    v = _vec(_agreement("net_billing", end=NOV.isoformat()), steps=192)
    naepp = tariffs.prosumer("net_billing", NOV)["naepp"]
    assert np.allclose(v["c_exp"][:96], naepp) and np.allclose(v["c_exp"][96:], 0.0)
    v = _vec(_agreement("net_billing", start=(NOV + timedelta(days=1)).isoformat()), steps=192)
    assert np.allclose(v["c_exp"][:96], 0.0) and np.allclose(v["c_exp"][96:], naepp)
    # a legacy agreement whose last day is today: netted at the import rate today, the NAEPP from tomorrow
    v = _vec(_agreement("net_metering_legacy", "A-2(c)", "2019-07-01", NOV.isoformat()), steps=192)
    assert np.allclose(v["c_exp"][:96], v["c_imp"][:96] - tariffs.LEGACY_EXPORT_TIE_BREAK) and np.allclose(v["c_exp"][96:], naepp)
    assert v["export_tie_break"][:96].all() and not v["export_tie_break"][96:].any()


def test_the_bill_credits_only_the_exports_inside_the_agreement(company):
    d0, d1 = date(2026, 11, 1), date(2026, 11, 2)
    ts = _minutes(d0, 2)
    imp, exp = np.full(len(ts), 50.0), np.zeros(len(ts))
    for day in range(2):  # 100 kWh exported at noon each day
        exp[day * 1440 + 12 * 60:day * 1440 + 13 * 60] = 100.0
    naepp = tariffs.prosumer("net_billing", d1)["naepp"]
    for extra, credited_kwh in (({"agreement_end": "2026-11-01"}, 100.0), ({"agreement_start": "2026-11-02"}, 100.0),
                                ({"agreement_start": "2026-01-01"}, 200.0), ({"agreement_end": "2026-10-31"}, 0.0)):
        bill = tariffs.rebuild(ts, imp, exp, _conn("B-2(b)", "net_billing", extra=extra), d0, d1)
        assert bill["credits_rs"] == pytest.approx(credited_kwh * naepp, abs=0.01), extra
        assert bill["export_kwh"]["off_peak"] == pytest.approx(200.0)  # the metered exports are still reported
    # an ended legacy agreement bills under net billing: no netting, every export credited at the NAEPP (before: netted at the import rate)
    ts, imp, exp = _flat_day(NOV)
    exp[9 * 60:15 * 60] = 400.0
    ended = tariffs.rebuild(ts, imp, exp, _agreement("net_metering_legacy", "A-2(c)", "2018-01-01", "2025-01-01"), NOV, NOV)
    plain = tariffs.rebuild(ts, imp, exp, _conn("A-2(c)"), NOV, NOV)
    assert ended["import_kwh"] == plain["import_kwh"] and ended["credits_rs"] == pytest.approx(2400.0 * naepp, abs=0.01)
    assert ended["total_rs"] == pytest.approx(plain["total_rs"] - 2400.0 * naepp, abs=0.01)
    assert [(x["units_kwh"], x["rate"]) for x in ended["lines"] if x["line"].startswith("Export credit")] == [(2400.0, naepp)]
    # and the bill's marginal export is the vector's: nothing after a net-billing agreement ends, the NAEPP after a legacy one ends
    assert _bump(_agreement("net_billing", end="2026-02-28"), ts, imp, exp, NOV, NOV, OFF, "exp") == pytest.approx(0.0, abs=0.01)
    ended_l = _agreement("net_metering_legacy", "A-2(c)", "2018-01-01", "2025-01-01")
    assert -_bump(ended_l, ts, imp, exp, NOV, NOV, OFF, "exp") == pytest.approx(naepp, abs=0.01)
    assert _bump(_agreement("net_metering_legacy", "A-2(c)", "2027-01-01"), ts, imp, exp, NOV, NOV, OFF, "exp") == pytest.approx(0.0, abs=0.01)


def test_a_legacy_agreement_that_ends_inside_the_bill_nets_its_own_days_and_credits_the_rest_at_the_naepp(company):
    d0, d1 = date(2026, 11, 1), date(2026, 11, 2)
    ts = _minutes(d0, 2)
    imp, exp = np.zeros(len(ts)), np.zeros(len(ts))
    for day in range(2):  # 100 kWh exported between 12:00 and 13:00 (off-peak) each day, no import
        exp[day * 1440 + 12 * 60:day * 1440 + 13 * 60] = 100.0
    conn = _agreement("net_metering_legacy", "A-2(c)", "2019-07-01", "2026-11-01")
    bill = tariffs.rebuild(ts, imp, exp, conn, d0, d1)
    nappp, naepp = tariffs.prosumer("net_metering_legacy", d1)["nappp"], tariffs.prosumer("net_billing", d1)["naepp"]
    credit = {x["line"]: (x["units_kwh"], x["rate"], x["amount_rs"]) for x in bill["lines"] if x["line"].startswith("Export credit")}
    assert credit == {"Export credit (legacy net metering)": (100.0, nappp, round(-100.0 * nappp, 2)),
                      "Export credit (net billing)": (100.0, naepp, round(-100.0 * naepp, 2))}
    assert bill["credits_rs"] == pytest.approx(100.0 * (nappp + naepp), abs=0.01)
    # an import on the day after the agreement ends is billed in full: it does not net against the legacy day's export
    imp[1440 + 14 * 60:1440 + 15 * 60] = 80.0  # 80 kWh off-peak on 2 November
    bill = tariffs.rebuild(ts, imp, exp, conn, d0, d1)
    assert bill["import_kwh"]["off_peak"] == pytest.approx(80.0)
    credit = {x["line"]: x["units_kwh"] for x in bill["lines"] if x["line"].startswith("Export credit")}
    assert credit == {"Export credit (legacy net metering)": 100.0, "Export credit (net billing)": 100.0}


def test_the_ledger_prices_an_export_after_a_legacy_agreement_ends_at_the_naepp(company):
    month, naepp = {"peak": 1000.0, "off_peak": 5000.0}, tariffs.prosumer("net_billing", NOV)["naepp"]
    ended = verify._minute_prices(_agreement("net_metering_legacy", "A-2(c)", "2018-01-01", "2025-01-01"), NOV, "s", month, {"peak": 0.0, "off_peak": 0.0})
    assert np.allclose(ended["c_exp"], naepp)  # before: the import price it netted (39.48 to 48.28 Rs/kWh)
    early = verify._minute_prices(_agreement("net_metering_legacy", "A-2(c)", "2027-01-01"), NOV, "s", month, {"peak": 0.0, "off_peak": 0.0})
    assert float(np.max(early["c_exp"])) == 0.0  # not started: nothing, and the optimiser's tie-break is not added back
    live = verify._minute_prices(_agreement("net_metering_legacy", "A-2(c)", "2019-07-01"), NOV, "s", month, {"peak": 0.0, "off_peak": 0.0})
    assert np.allclose(live["c_exp"], live["c_imp"])  # in force: an export nets the import it displaces


def test_a_plan_outside_an_agreement_exports_under_the_regime_billed_that_day(company):
    from gridlytics import forecast, plans
    conn = {"tariff_category": "A-2(c)", "sanctioned_load_kw": 500, "regime": "net_billing", "export_cap_kw": 150,
            "agreement_start": "2021-03-01", "agreement_end": "2026-02-28"}
    sid = make_site(name="Ended Agreement", conn=conn, genset=False)
    full = sites.full(sid)
    cid = full["connections"][0]["id"]
    t0 = int(datetime(2026, 11, 2, 6, tzinfo=PKT).timestamp())

    def problem():
        f = sites.full(sid)
        return plans.build_problem(f, t0, forecast.issue(f, t0, save=False), 96)

    p = problem()  # net billing ended: a zero-export site (before: a 150 kW export cap at 8.13 Rs/kWh)
    assert p["X_max"] == 0.0 and p["regime"] == "none" and float(np.max(p["c_exp"])) == 0.0
    sites.put_connection(sid, {**conn, "regime": "net_metering_legacy", "agreement_start": "2018-01-01", "agreement_end": "2025-01-01"}, "t", cid)
    p = problem()  # legacy ended: net billing, no netting (before: legacy netting at the import rate)
    assert p["X_max"] == 150.0 and p["regime"] == "net_billing" and np.allclose(p["c_exp"], tariffs.prosumer("net_billing", NOV)["naepp"])
    sites.put_connection(sid, {**conn, "regime": "net_metering_legacy", "agreement_end": "2033-01-01"}, "t", cid)
    p = problem()
    assert p["X_max"] == 150.0 and p["regime"] == "net_metering_legacy"


def test_agreement_state_reads_the_dates(company):
    assert tariffs.agreement_state(_agreement("net_billing", start="2019-03-01", end="2026-02-28"), NOV) == "ended"
    assert tariffs.agreement_state(_agreement("net_billing", start="2027-01-01"), NOV) == "not_started"
    assert tariffs.agreement_state(_agreement("net_billing", start="2026-11-02", end="2026-11-02"), NOV) == "active"
    assert tariffs.agreement_state(_agreement("net_billing", end="banana"), NOV) == "unreadable"
    assert tariffs.agreement_state(_conn("B-2(b)", "none"), NOV) is None
    legacy = _agreement("net_metering_legacy", "A-2(c)", "2019-07-01", "2026-11-01")
    assert [tariffs.regime_on(legacy, d) for d in (date(2019, 6, 30), date(2026, 11, 1), NOV)] == ["none", "net_metering_legacy", "net_billing"]
    nb = _agreement("net_billing", start="2026-03-01", end="2026-11-01")
    assert [tariffs.regime_on(nb, d) for d in (date(2026, 2, 28), date(2026, 3, 1), NOV)] == ["none", "net_billing", "none"]
    assert tariffs.regime_on(_agreement("net_metering_legacy", end="banana"), NOV) == "none"
    assert tariffs.regime_on(_conn("B-2(b)", "none"), NOV) == "none" and tariffs.regime_on(_conn("B-2(b)", "flat"), NOV) == "flat"


def test_the_price_page_shows_no_export_value_outside_the_agreement(world):
    """The finding's own steps: GET /prices for a connection saved with an ended, a valid and an ended legacy agreement."""
    a = world.c["admin"]
    with world.use():
        cid = sites.connections(world.site)[0]["id"]
    base = {"tariff_category": "A-2(c)", "sanctioned_load_kw": 400, "export_cap_kw": 100}

    def exp_max(**fields):
        with world.use():
            sites.put_connection(world.site, {**base, **fields}, "test", cid)
        r = a.get(f"/api/sites/{world.site}/prices", params={"day": "2026-11-09"})
        assert r.status_code == 200, r.text
        return max(r.json()["c_exp"])

    with world.use():
        naepp = round(tariffs.prosumer("net_billing", date(2026, 11, 9))["naepp"], 2)
    assert exp_max(regime="net_billing", agreement_start="2019-03-01", agreement_end="2026-02-28") == 0.0  # before: 8.13
    assert exp_max(regime="net_billing", agreement_start="2019-03-01", agreement_end="2033-02-28") == naepp
    assert exp_max(regime="net_metering_legacy", agreement_start="2018-01-01", agreement_end="2025-01-01") == naepp  # before: 40.01


def test_the_tariff_library_lists_each_agreement_with_its_state(world):
    a = world.c["admin"]
    with world.use():
        sites.put_connection(world.site, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400, "regime": "net_billing", "agreement_start": "2021-03-01",
                                          "agreement_end": "2026-02-28", "meter_ref": "M-1"}, "test")
        sites.put_connection(world.other, {"tariff_category": "A-2(c)", "sanctioned_load_kw": 300, "regime": "net_metering_legacy",
                                           "agreement_start": "2019-07-01"}, "test")
        three = sites.create({"name": "Third Mill"}, "test")["id"]
        sites.put_connection(three, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 300, "regime": "net_billing", "agreement_start": "2027-03-01",
                                     "agreement_end": "2032-02-28"}, "test")
    r = a.get("/api/tariffs", params={"on": "2026-11-09"})
    assert r.status_code == 200, r.text
    rows = {x["site_name"]: x for x in r.json()["agreements"]}
    assert set(rows) == {"Mill One", "Secret Site Two", "Third Mill"}       # Mill One's first connection has no export regime and is not listed
    assert rows["Mill One"]["state"] == "ended" and rows["Mill One"]["agreement_end"] == "2026-02-28" and rows["Mill One"]["regime"] == "net_billing"
    assert rows["Mill One"]["billed_as"] == "none"
    assert rows["Secret Site Two"]["state"] == "active" and rows["Secret Site Two"]["agreement_end"] is None
    assert rows["Secret Site Two"]["billed_as"] == "net_metering_legacy"
    assert rows["Third Mill"]["state"] == "not_started" and rows["Third Mill"]["site_id"] == three
    assert r.json()["agreements"] == sorted(r.json()["agreements"], key=lambda x: (x["site_name"], x["connection_id"]))
    on_feb = {x["site_name"]: x["state"] for x in a.get("/api/tariffs", params={"on": "2026-02-01"}).json()["agreements"]}
    assert on_feb["Mill One"] == "active"                                    # read on the day asked
    assert world.c["viewer"].get("/api/tariffs").status_code == 403


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


# ------------------------------------------------------------------------------------------------ engine-tariffs-04: the legacy surplus at the NAPPP

@pytest.mark.parametrize("category", ["A-2(c)", "B-2(b)"])
def test_a_legacy_surplus_is_credited_at_the_notified_nappp(company, category):
    conn = _conn(category, "net_metering_legacy")
    ts, imp, exp = _flat_day(NOV)
    exp[9 * 60:15 * 60] = 400.0  # the off-peak period ends in surplus
    cal = tariffs.calendar_for(conn, NOV)
    peak = np.array([tariffs.is_peak_local(cal, datetime.fromtimestamp(int(t), PKT)) for t in ts])
    mtd_imp = {"peak": float(imp[peak].sum() / 60), "off_peak": float(imp[~peak].sum() / 60)}
    mtd_exp = {"peak": float(exp[peak].sum() / 60), "off_peak": float(exp[~peak].sum() / 60)}
    vec = tariffs.vectors(conn, _t0(ts), 96, mtd_imp, None, 1e9, mtd_exp, None)
    assert tariffs.prosumer("net_metering_legacy", NOV)["nappp"] == 25.32
    assert vec["c_imp"][OFF // 15] == pytest.approx(25.32)                        # before: 35.15 (A-2(c)) or 22.83 (B-2(b)), the off-peak rate
    assert -_bump(conn, ts, imp, exp, NOV, NOV, OFF, "exp") == pytest.approx(25.32, abs=0.02)
    credit = next(line for line in tariffs.rebuild(ts, imp, exp, conn, NOV, NOV)["lines"] if line["line"] == "Export credit")
    assert credit["amount_rs"] == pytest.approx(-25.32 * credit["units_kwh"], abs=0.01)


def test_a_legacy_bill_that_shows_the_off_peak_rate_overrides_the_nappp(company):
    _approve("prosumer", "net_metering_legacy", "2026-11-01", {"name": "Legacy surplus at the off-peak rate (confirmed on the site's bill)",
                                                             "netting": "by_period", "surplus_rate": "off_peak"})
    conn = _conn("A-2(c)", "net_metering_legacy")
    ts, imp, exp = _flat_day(NOV)
    exp[9 * 60:15 * 60] = 400.0
    assert -_bump(conn, ts, imp, exp, NOV, NOV, OFF, "exp") == pytest.approx(35.15, abs=0.02)
    with pytest.raises(ValueError, match="nappp"):
        tariffs.propose("prosumer", "net_metering_legacy", "2027-01-01", {"netting": "by_period", "surplus_rate": "nappp"}, SRC, "t")


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


# ================================================================================================ batch 2: bill rebuild and bill comparison

FEB_DSS = 3.23  # the only adder billed in February 2026


def _month_series(d0: date, days: int, kw: float = 100.0, mdi_day: int = 19):
    """A flat import over whole days with one 150 % block (the bill's MDI) at 10:00 on day `mdi_day`, so a bumped kWh elsewhere moves no fixed charge."""
    ts = _minutes(d0, days)
    imp = np.full(len(ts), kw)
    imp[mdi_day * 1440 + 600:mdi_day * 1440 + 630] = kw * 1.5
    return ts, imp, np.zeros(len(ts))


def _minute_of(ts, day: date, hhmm: int) -> int:
    t = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp()) + hhmm * 60
    return int(np.searchsorted(ts, t))


def _lines(bill, prefix):
    return [ln for ln in bill["lines"] if ln["line"].startswith(prefix)]


# ------------------------------------------------------------------------------------------------ engine-tariffs-01: each day priced by its own versions

def test_a_bill_across_a_schedule_change_prices_each_day_with_the_version_in_force_that_day(company):
    conn = _conn("B-2(b)", sanctioned=400.0)
    d0, d1 = date(2026, 2, 1), date(2026, 2, 28)  # SRO 279(I)/2026 from 12 Feb; the January cards before it
    ts, imp, exp = _month_series(d0, 28)
    jan, cy = (27.41 + FEB_DSS) * 1.015, (22.83 + FEB_DSS) * 1.015
    for day, want in ((date(2026, 2, 5), jan), (date(2026, 2, 20), cy)):
        got = _bump(conn, ts, imp, exp, d0, d1, _minute_of(ts, day, 185))  # 03:05, off-peak
        assert got == pytest.approx(want, abs=0.01)                          # before: 26.45 on 5 Feb (SRO 279 for every day)
        vec = tariffs.vectors(conn, datetime(day.year, day.month, day.day, tzinfo=PKT).astimezone(timezone.utc), 96)
        assert vec["c_imp"][12] == pytest.approx(got, abs=0.01)            # the optimiser and the ledger price the same kWh alike
    bill = tariffs.rebuild(ts, imp, exp, conn, d0, d1)
    # fixed charges pro rata by days: 11 days on the January card (floor 50 % of 400 kW) and 17 on SRO 279's (floor 25 %, so the 150 kW MDI)
    fixed = _lines(bill, "Fixed charges")
    assert [(f["days"], f["of_days"], f["billing_demand_kw"]) for f in fixed] == [(11, 28, 200.0), (17, 28, 150.0)]
    assert sum(f["amount_rs"] for f in fixed) == pytest.approx(1250 * 200 * 11 / 28 + 1250 * 150 * 17 / 28, abs=0.02)  # before: 187,500
    off = _lines(bill, "Energy off-peak")
    assert [ln["rate"] for ln in off] == [27.41, 22.83] and "1 Feb to 11 Feb 2026" in off[0]["line"] and "12 Feb to 28 Feb 2026" in off[1]["line"]
    assert sum(ln["units_kwh"] for ln in off) == pytest.approx(bill["import_kwh"]["off_peak"], abs=0.01)
    # one schedule version over the whole interval: one line per period, as before
    one = tariffs.rebuild(*_month_series(date(2026, 11, 1), 30), conn, date(2026, 11, 1), date(2026, 11, 30))
    assert [ln["line"] for ln in one["lines"] if ln["line"].startswith(("Energy", "Fixed"))] == ["Energy peak", "Energy off-peak", "Fixed charges"]


def test_a_horizon_across_a_schedule_change_prices_the_second_day_with_its_own_card(company):
    conn = _conn("B-2(b)")
    vec = tariffs.vectors(conn, datetime(2026, 2, 11, tzinfo=PKT).astimezone(timezone.utc), 192)
    assert vec["c_imp"][12] == pytest.approx((27.41 + FEB_DSS) * 1.015, abs=0.01)
    assert vec["c_imp"][96 + 12] == pytest.approx((22.83 + FEB_DSS) * 1.015, abs=0.01)  # before: 12 Feb priced on 11 Feb's card


# ------------------------------------------------------------------------------------------------ engine-tariffs-02: excess credit carried forward

def _surplus_month():
    """A legacy A-2(c) month whose off-peak period ends in surplus: 20 kW imported all day, 300 kW exported 09:00 to 15:00."""
    conn = _conn("A-2(c)", "net_metering_legacy", extra={"agreement_start": "2019-07-01"})
    d0 = date(2026, 10, 1)
    ts = _minutes(d0, 31)
    imp, exp = np.full(len(ts), 20.0), np.zeros(len(ts))
    mod = ((ts + 5 * 3600) % 86400) // 60
    exp[(mod >= 9 * 60) & (mod < 15 * 60)] = 300.0
    return conn, ts, imp, exp, d0, date(2026, 10, 31)


def test_an_export_credit_beyond_the_bill_is_carried_to_the_next_bill(company):
    conn, ts, imp, exp, d0, d1 = _surplus_month()
    bill = tariffs.rebuild(ts, imp, exp, conn, d0, d1)
    charges, credit = bill["charges_rs"], bill["credits_rs"]
    assert credit > charges > 0 and bill["total_rs"] == 0.0
    assert bill["credit_carried_rs"] == pytest.approx(credit - charges, abs=0.01)                    # before: dropped
    assert bill["bearable_rs"] == pytest.approx(charges - bill["recovered_taxes_rs"] - credit, abs=0.02)  # the month's exports are worth their credit
    assert sum(ln["amount_rs"] for ln in bill["lines"]) == pytest.approx(bill["total_rs"], abs=0.05)  # the lines add up to the total
    assert _lines(bill, "Credit carried to the next bill")[0]["amount_rs"] == pytest.approx(bill["credit_carried_rs"], abs=0.01)
    # the next month imports only: the carried credit pays its bill first
    ts2 = _minutes(date(2026, 11, 1), 30)
    nxt = tariffs.rebuild(ts2, np.full(len(ts2), 20.0), np.zeros(len(ts2)), conn, date(2026, 11, 1), date(2026, 11, 30),
                          credit_brought_forward_rs=bill["credit_carried_rs"])
    assert nxt["total_rs"] == pytest.approx(max(0.0, nxt["charges_rs"] - bill["credit_carried_rs"]), abs=0.01)
    assert _lines(nxt, "Credit brought forward")[0]["amount_rs"] == pytest.approx(-bill["credit_carried_rs"], abs=0.01)
    assert nxt["credit_carried_rs"] == pytest.approx(max(0.0, bill["credit_carried_rs"] - nxt["charges_rs"]), abs=0.01)
    # what the company bears is its own month's: a credit earned before is not counted twice
    alone = tariffs.rebuild(ts2, np.full(len(ts2), 20.0), np.zeros(len(ts2)), conn, date(2026, 11, 1), date(2026, 11, 30))
    assert nxt["bearable_rs"] == pytest.approx(alone["bearable_rs"], abs=0.01)


def _surplus_rows(sid, d0: date, days: int, export_kw: float):
    from test_fix_r2_savings import _flat_rows
    # a 15-minute row carries one minute's energy in this fixture: 15 times the power gives the export_kw mean over its quarter hour
    _flat_rows(sid, d0, days, 10.0, step_min=15, export_kw=lambda m: 15 * export_kw if 9 * 60 <= m < 17 * 60 else 0.0)


def test_entering_bills_in_turn_carries_a_net_billing_surplus_forward(company):
    sid = make_site(name="Surplus Mill", conn={"tariff_category": "B-2(b)", "sanctioned_load_kw": 500, "regime": "net_billing"})
    _surplus_rows(sid, date(2026, 9, 1), 61, 1000.0)
    sep = verify.add_bill(sid, {"period_start": "2026-09-01", "period_end": "2026-09-30", "total_rs": 1.0}, "t")
    carried = sep["rebuilt"]["credit_carried_rs"]
    assert carried > 0 and sep["rebuilt"]["total_rs"] == 0.0
    octo = verify.add_bill(sid, {"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": 1.0}, "t")
    assert octo["rebuilt"]["credit_brought_forward_rs"] == pytest.approx(carried, abs=0.01)
    assert octo["rebuilt"]["credit_carried_rs"] == pytest.approx(carried + octo["rebuilt"]["credits_rs"] - octo["rebuilt"]["charges_rs"], abs=0.02)
    # a bill entered after its successor is rebuilt first, and the successor takes its carried credit then
    later = make_site(name="Surplus Mill Two", conn={"tariff_category": "B-2(b)", "sanctioned_load_kw": 500, "regime": "net_billing"})
    _surplus_rows(later, date(2026, 9, 1), 61, 1000.0)
    verify.add_bill(later, {"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": 1.0}, "t")
    verify.add_bill(later, {"period_start": "2026-09-01", "period_end": "2026-09-30", "total_rs": 1.0}, "t")
    oct2 = next(b for b in verify.bills(later) if b["period_start"] == "2026-10-01")
    assert oct2["rebuilt"]["credit_brought_forward_rs"] == pytest.approx(carried, abs=0.01)


# ------------------------------------------------------------------------------------------------ engine-tariffs-08: the missing bill steps

def test_a_power_factor_below_90_percent_raises_the_fixed_charge(company):
    conn = _conn("B-2(b)", sanctioned=400.0)
    d0 = date(2026, 11, 1)
    ts, imp, exp = _month_series(d0, 30)
    base = _lines(tariffs.rebuild(ts, imp, exp, conn, d0, date(2026, 11, 30)), "Fixed charges")[0]
    low = tariffs.rebuild(ts, imp, exp, conn, d0, date(2026, 11, 30), power_factor_pct=85.0)
    fixed = _lines(low, "Fixed charges")[0]
    assert fixed["amount_rs"] == pytest.approx(base["amount_rs"] * 1.10, abs=0.02)  # 2 % of the fixed charge per point below 90 (R9 3; T&C)
    assert fixed["power_factor_pct"] == 85.0 and fixed["pf_factor"] == pytest.approx(1.10)
    assert _lines(tariffs.rebuild(ts, imp, exp, conn, d0, date(2026, 11, 30), power_factor_pct=93.0), "Fixed charges")[0]["amount_rs"] == base["amount_rs"]
    flat = _conn("A-3", sanctioned=40.0)  # a fixed charge per consumer is not set by maximum demand: no penalty
    a = _lines(tariffs.rebuild(ts, imp, exp, flat, d0, date(2026, 11, 30)), "Fixed charges")[0]["amount_rs"]
    assert _lines(tariffs.rebuild(ts, imp, exp, flat, d0, date(2026, 11, 30), power_factor_pct=80.0), "Fixed charges")[0]["amount_rs"] == a


def test_a_large_punjab_connection_pays_4_paisa_a_unit_of_grid_duty(company):
    d0 = date(2026, 11, 2)
    ts, imp, exp = _flat_day(d0)
    big = _conn("B-3", sanctioned=1000.0)
    bill = tariffs.rebuild(ts, imp, exp, big, d0, d0)
    line = _lines(bill, "Punjab electricity duty")[0]
    units = sum(bill["import_kwh"].values())
    assert line["rate"] == 0.04 and line["units_kwh"] == pytest.approx(units, abs=0.01) and line["amount_rs"] == pytest.approx(0.04 * units, abs=0.01)
    for other in (_conn("B-3", sanctioned=400.0), _conn("B-3", sanctioned=1000.0, province="sindh")):
        assert not _lines(tariffs.rebuild(ts, imp, exp, other, d0, d0), "Punjab electricity duty")
    vec = tariffs.vectors(big, _t0(ts), 96)
    assert vec["c_imp"][12] == pytest.approx(_bump(big, ts, imp, exp, d0, d0, OFF), abs=0.01)  # the price carries the duty too


def test_extra_tax_for_a_connection_off_the_active_taxpayer_list(company):
    d0 = date(2026, 11, 2)
    ts, imp, exp = _flat_day(d0)
    non_atl = _conn("B-2(b)", atl=False)
    bill = tariffs.rebuild(ts, imp, exp, non_atl, d0, d0)
    assert not _lines(bill, "Extra tax") and any("Extra tax" in u for u in bill["unpriced"])  # the slabs are not notified in the shipped version
    assert not tariffs.rebuild(ts, imp, exp, _conn("B-2(b)"), d0, d0)["unpriced"]
    tax = next(v for v in tariff_data.SHIPPED if v["kind"] == "tax")["body"]
    _approve("tax", "national", "2026-11-01", {**tax, "extra_tax_non_atl": [{"upto": 20000, "rate": 0.05}, {"upto": None, "rate": 0.17}]})
    bill = tariffs.rebuild(ts, imp, exp, non_atl, d0, d0)
    gst = _lines(bill, "GST")[0]
    extra = _lines(bill, "Extra tax")[0]
    assert extra["rate"] == 0.17 and extra["amount_rs"] == pytest.approx(gst["amount_rs"] / 0.18 * 0.17, abs=0.02) and not bill["unpriced"]
    assert tariffs.rebuild(ts, imp, exp, _conn("B-2(b)"), d0, d0)["total_rs"] < bill["total_rs"]
    assert tariffs.vectors(non_atl, _t0(ts), 96)["c_imp"][12] == pytest.approx(_bump(non_atl, ts, imp, exp, d0, d0, OFF), abs=0.01)
    with pytest.raises(ValueError, match="extra tax"):
        tariffs.propose("tax", "national", "2026-12-01", {**tax, "extra_tax_non_atl": [{"upto": 20000, "rate": 5}]}, SRC, "t")


def test_a_net_billing_version_can_credit_before_tax_and_price_exports_by_period(company):
    d0 = date(2026, 11, 2)
    ts, imp, exp = _flat_day(d0)
    exp[12 * 60:13 * 60] = 100.0          # 100 kWh off-peak
    exp[19 * 60:19 * 60 + 30] = 100.0     # 50 kWh in the peak window
    conn = _conn("B-2(b)", "net_billing")
    after = tariffs.rebuild(ts, imp, exp, conn, d0, d0)
    nb = tariffs.prosumer("net_billing", d0)
    _approve("prosumer", "net_billing", "2026-11-01", {**nb, "credit_before_tax": True, "tou_export_schedule": {"peak": 12.0, "off_peak": 6.0}})
    before = tariffs.rebuild(ts, imp, exp, conn, d0, d0)
    assert before["credits_rs"] == pytest.approx(50 * 12.0 + 100 * 6.0, abs=0.01)
    gst_after, gst_before = _lines(after, "GST")[0]["amount_rs"], _lines(before, "GST")[0]["amount_rs"]
    assert gst_after - gst_before == pytest.approx(0.18 * before["credits_rs"], abs=0.02)  # the credit came off the GST base
    vec = tariffs.vectors(conn, _t0(ts), 96)
    assert vec["c_exp"][12] == pytest.approx(6.0) and vec["c_exp"][19 * 4 + 1] == pytest.approx(12.0)
    with pytest.raises(ValueError, match="TOU export"):
        tariffs.propose("prosumer", "net_billing", "2026-12-01", {**nb, "tou_export_schedule": {"peak": "high"}}, SRC, "t")


def test_a_rebuilt_bill_carries_every_versions_hash_and_the_series_hash(company):
    d0 = date(2026, 11, 2)
    ts, imp, exp = _flat_day(d0)
    conn = _conn("B-2(b)", "net_billing", extra={"icp": {"eligible": True, "peak": {"11": 10.0}, "off_peak": {"11": 100.0}}})
    bill = tariffs.rebuild(ts, imp, exp, conn, d0, d0)
    kinds = {(v["kind"], v["key"]) for v in bill["versions"]}
    assert {("schedule", "national"), ("tou", "xwdisco"), ("tax", "national"), ("icp", "national"), ("prosumer", "net_billing"),
            ("adder", "dss"), ("adder", "qta")} <= kinds
    assert all(len(v["hash"]) == 16 and v["id"] for v in bill["versions"])
    imp2 = imp.copy()
    imp2[OFF] += 1.0
    assert tariffs.rebuild(ts, imp2, exp, conn, d0, d0)["series_hash"] != bill["series_hash"]
    assert tariffs.rebuild(ts, imp, exp, conn, d0, d0)["series_hash"] == bill["series_hash"]


# ------------------------------------------------------------------------------------------------ pages-D-10 (flow-savings-pilot-bill-07): line by line

def test_the_printed_units_and_mdi_are_compared_line_by_line(company):
    from test_fix_r2_savings import _flat_rows
    sid = make_site(name="Printed Lines")
    _flat_rows(sid, date(2026, 10, 1), 31, 100.0, step_min=15)
    rebuilt = verify.rebuild_bill(sid, sites.connections(sid)[0], date(2026, 10, 1), date(2026, 10, 31))
    pk, off = rebuilt["import_kwh"]["peak"], rebuilt["import_kwh"]["off_peak"]
    b = verify.add_bill(sid, {"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": rebuilt["total_rs"], "units_peak": round(pk * 0.67),
                              "units_off_peak": round(off), "mdi_kw": round(rebuilt["mdi_kw"] * 1.004, 2)}, "t")
    rows = {r["line"]: r for r in b["reconciliation"]}
    assert set(rows) == {"units_peak", "units_off_peak", "mdi_kw"}
    assert rows["units_peak"]["rebuilt"] == pytest.approx(pk, abs=0.01) and not rows["units_peak"]["within"]
    assert rows["units_peak"]["difference_pct"] == pytest.approx((pk - round(pk * 0.67)) / round(pk * 0.67) * 100, abs=0.01)
    assert rows["units_off_peak"]["within"] and rows["mdi_kw"]["within"]
    assert b["difference_pct"] == 0.0 and b["check"]["verdict"] == "outside" and b["check"]["outside"] == ["units_peak"]  # before: 'within', on the total
    assert verify.bills(sid)[0]["check"] == b["check"]
    logged = next(a["detail"] for a in store.list_audit() if a["action"] == "entered a DISCO bill")
    assert {r["line"] for r in logged["residuals"]} == {"units_peak", "units_off_peak", "mdi_kw"}


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-08: gaps are not zero import

def test_a_bill_over_a_gap_fills_it_from_the_profile_and_withholds_the_verdict_when_mostly_missing(company):
    from test_fix_r2_savings import _flat_rows
    sid = make_site(name="Gappy Mill")
    _flat_rows(sid, date(2026, 10, 25), 15, 100.0, step_min=15)  # 25 Oct to 8 Nov
    conn = sites.connections(sid)[0]
    full = verify.rebuild_bill(sid, conn, date(2026, 10, 25), date(2026, 11, 8))
    gap = verify.rebuild_bill(sid, conn, date(2026, 10, 24), date(2026, 11, 8))  # a day before the first data
    assert gap["completeness"] == pytest.approx(15 / 16, abs=1e-3)
    assert sum(gap["import_kwh"].values()) == pytest.approx(sum(full["import_kwh"].values()) * 16 / 15, rel=1e-3)  # before: equal to the 15 days'
    assert gap["filled_kwh"]["import"] == pytest.approx(sum(full["import_kwh"].values()) / 15, rel=1e-3)
    assert full["completeness"] == 1.0  # a 15-minute source's samples cover their 15 minutes
    b = verify.add_bill(sid, {"period_start": "2026-10-24", "period_end": "2026-11-08", "total_rs": gap["total_rs"]}, "t")
    assert b["check"]["verdict"] == "within"
    two = verify.rebuild_bill(sid, conn, date(2026, 10, 23), date(2026, 11, 8))  # two days missing of 17: 88 %, below the 90 % for a verdict
    assert verify._check(two, 0.0, [])["verdict"] == "not_comparable"
    mostly = verify.add_bill(sid, {"period_start": "2026-10-01", "period_end": "2026-10-23", "total_rs": 5_000_000}, "t")
    assert mostly["rebuilt"]["completeness"] == 0.0 and mostly["check"]["verdict"] == "not_comparable"  # before: 'outside the 2 % check'
    other = make_site(name="Partly Mill")
    _flat_rows(other, date(2026, 10, 25), 7, 100.0, step_min=15)
    part = verify.add_bill(other, {"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": 17_500_000}, "t")
    assert part["rebuilt"]["completeness"] == pytest.approx(7 / 31, abs=1e-3) and part["check"]["verdict"] == "not_comparable"


# ------------------------------------------------------------------------------------------------ engine-tariffs-10: ICP at legacy net-metering sites

def test_icp_at_a_legacy_site_caps_gross_import_increments_at_net_imports_pro_rata(company):
    esc = 1.028  # package year 1
    conn = _conn("B-2(b)", "net_metering_legacy", extra={"agreement_start": "2019-07-01",
                                                          "icp": {"eligible": True, "peak": {"11": 200.0 / esc}, "off_peak": {"11": 1000.0 / esc}}})
    d0 = date(2026, 11, 2)
    ts = _minutes(d0, 1)
    mod = ((ts + 5 * 3600) % 86400) // 60
    pk = (mod >= 18 * 60) & (mod < 22 * 60)  # November's peak window, 240 minutes

    def bill(p_imp, p_exp, o_imp, o_exp):
        imp, exp = np.where(pk, p_imp * 60 / 240, o_imp * 60 / 1200), np.where(pk, p_exp * 60 / 240, o_exp * 60 / 1200)
        return tariffs.rebuild(ts, imp, exp, conn, d0, d0)

    # increments over the baseline on imported units (800 + 1,000), capped at the month's net import (3,000 - 2,100 = 900) and split pro rata
    b = bill(1000, 600, 2000, 1500)
    assert b["icp_incremental_kwh"]["peak"] == pytest.approx(900 * 800 / 1800, abs=0.01)     # before: 200 (the period's net import less baseline)
    assert b["icp_incremental_kwh"]["off_peak"] == pytest.approx(900 * 1000 / 1800, abs=0.01)  # before: 0
    # pro rata, but never more ICP units in a period than it bills (800 net off-peak units here)
    b = bill(1000, 300, 2000, 1200)
    assert b["icp_incremental_kwh"] == {"peak": pytest.approx(1500 * 800 / 1800, abs=0.01), "off_peak": pytest.approx(800.0, abs=0.01)}
    assert all(ln["units_kwh"] >= 0 for ln in b["lines"] if ln["line"].startswith("Energy"))
    # a period that ends the month in net export is not eligible (SRO 2409(I)/2025): no increment there
    b = bill(1000, 0, 2000, 2500)
    assert b["icp_incremental_kwh"] == {"peak": pytest.approx(500.0, abs=0.01), "off_peak": 0.0}


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-23: overlaps, correct, delete

def test_an_overlapping_bill_is_refused_and_a_bill_can_be_corrected_or_deleted(company):
    from test_fix_r2_savings import _flat_rows
    sid = make_site(name="Two Bills")
    _flat_rows(sid, date(2026, 10, 20), 20, 100.0, step_min=15)
    first = verify.add_bill(sid, {"period_start": "2026-10-25", "period_end": "2026-11-08", "total_rs": 2_000_000, "reference": "A-1"}, "t")
    with pytest.raises(ValueError, match="overlaps the bill for 25 Oct to 8 Nov 2026"):
        verify.add_bill(sid, {"period_start": "2026-10-23", "period_end": "2026-11-08", "total_rs": 2_100_000}, "t")
    assert len(verify.bills(sid)) == 1
    fixed = verify.correct_bill(sid, first["id"], {"period_start": "2026-10-23", "period_end": "2026-11-08", "total_rs": 2_100_000}, "op")
    assert fixed["id"] == first["id"] and fixed["period_start"] == "2026-10-23" and fixed["real"]["total_rs"] == 2_100_000
    assert fixed["difference_pct"] == pytest.approx((fixed["rebuilt"]["total_rs"] - 2_100_000) / 2_100_000 * 100, abs=0.01)
    assert [b["id"] for b in verify.bills(sid)] == [first["id"]]
    verify.delete_bill(sid, first["id"], "op")
    assert verify.bills(sid) == []
    actions = [a["action"] for a in store.list_audit()]
    assert "corrected a DISCO bill" in actions and "deleted a DISCO bill" in actions
    with pytest.raises(LookupError):
        verify.delete_bill(sid, first["id"], "op")


def test_the_fixed_charge_line_shows_its_proration(company):
    d0 = date(2026, 10, 25)
    ts, imp, exp = _month_series(d0, 15, mdi_day=3)
    bill = tariffs.rebuild(ts, imp, exp, _conn("B-2(b)", sanctioned=400.0), d0, date(2026, 11, 8))
    f = _lines(bill, "Fixed charges")[0]
    assert (f["days"], f["of_days"]) == (15, 30)
    assert f["amount_rs"] == pytest.approx(f["rate"] * f["billing_demand_kw"] * 15 / 30, abs=0.01)  # 150 kW x Rs 1,250 x 15/30


def test_bills_can_be_corrected_and_deleted_through_the_api_by_who_enters_them(world):
    from test_fix_r2_savings import _flat_rows
    with world.use():
        _flat_rows(world.site, date(2026, 10, 1), 31, 100.0, step_min=30)
    op = world.c["operator"]
    r = op.post(f"/api/sites/{world.site}/bills", json={"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": 3_000_000})
    assert r.status_code == 200, r.text
    bid = r.json()["id"]
    again = op.post(f"/api/sites/{world.site}/bills", json={"period_start": "2026-10-15", "period_end": "2026-11-05", "total_rs": 1})
    assert again.status_code == 422 and "overlaps" in again.json()["detail"]
    for role in ("analyst", "viewer"):
        assert world.c[role].put(f"/api/sites/{world.site}/bills/{bid}", json={"period_start": "2026-10-01", "period_end": "2026-10-31",
                                                                               "total_rs": 1}).status_code == 403
        assert world.c[role].delete(f"/api/sites/{world.site}/bills/{bid}").status_code == 403
    r = op.put(f"/api/sites/{world.site}/bills/{bid}", json={"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": 3_100_000,
                                                               "power_factor_pct": 88})
    assert r.status_code == 200 and r.json()["real"]["total_rs"] == 3_100_000 and r.json()["real"]["power_factor_pct"] == 88
    assert op.put(f"/api/sites/{world.site}/bills/nope", json={"period_start": "2026-10-01", "period_end": "2026-10-31", "total_rs": 1}).status_code == 404
    assert op.put(f"/api/sites/{world.other}/bills/{bid}", json={"period_start": "2026-10-01", "period_end": "2026-10-31",
                                                                   "total_rs": 1}).status_code in (404, 422)
    assert op.delete(f"/api/sites/{world.site}/bills/{bid}").status_code == 200
    assert op.get(f"/api/sites/{world.site}/bills").json() == []
    assert op.delete(f"/api/sites/{world.site}/bills/{bid}").status_code == 404
