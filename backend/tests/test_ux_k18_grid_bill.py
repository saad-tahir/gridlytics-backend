"""UX backend ask K18 (plan H11; D9.8, D2.10; step 8, the Overview's money band): `GET /sites/:id/now` adds `month.grid_bill_rs`, the grid bill so
far, and `month.grid_bill_rs_basis`, what it covers.

The figure is verify.bill_so_far: the bill today falls in, rebuilt by the Bills page's own rebuild (verify.rebuild_bill, priced by tariffs.rebuild)
from the billing period's first day to the end of yesterday, in the bill's own billing month (the reading date's), with the credit the bill before
carried and the connection's metering factor where it is calibrated. Both are money names: a viewer and a machine key get neither (the role walk
proves it). The golden is worked by hand from the SRO rates, never by running the engine."""
from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest
from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_fix_r2_savings import _flat_rows
from test_fix_savings import make_site
from test_fix_tariffs import company  # noqa: F401  (company is a fixture)

from gridlytics import routes_sites, sites, store, tariffs, twin, verify

KARACHI = "+05:00"

DSS = "Debt service surcharge (printed as FC or PHL surcharge)"
SO_FAR_TOTAL = 186335.37


def _a3_site(reading_day: int = 25) -> tuple[str, dict]:
    sid = make_site(name="So far A3", conn={"tariff_category": "A-3", "sanctioned_load_kw": 40, "regime": "flat", "reading_day": reading_day},
                    battery=False, genset=False)
    return sid, sites.connections(sid)[0]


def test_golden_the_grid_bill_so_far_is_rebuilt_to_the_end_of_yesterday_in_its_own_billing_month(company):
    """A-3 general services, a flat card, FESCO (Punjab), the regime "flat", STRN and ATL; class commercial; the meter read on the 25th. Today is 30 Aug
    2026: the bill today falls in runs from 26 Aug (the day after the 25 Aug reading) to the 25 Sep reading, billing month September 2026. So far:
    26 to 29 Aug, 4 whole days (today, still running, is not billed). A steady 30 kW. Adders billed in September: FCA July 2026 2.0581, QTA Q2
    CY2026 0.5194, DSS 3.23 (the bill's own month, not August's, the month of the last day read).

    Units 4 x 24 x 30 = 2,880 kWh, one bucket.

    (1) Energy            2,880 kWh x 42.48 = 122,342.40
    (2) Fixed charges     1,000 per consumer x 4 / 30 = 133.33 (4 days, more than 4 off 30: prorated by days)
    (3) FCA 2,880 x 2.0581 = 5,927.33; QTA 2,880 x 0.5194 = 1,495.87; DSS 2,880 x 3.23 = 9,302.40
    (4) Electricity duty 1.5 % x 139,068.00 = 2,086.02
    (5) GST 18 % x (139,068.00 + 133.333 + 2,086.02 = 141,287.353) = 25,431.72
    (6) Income tax s.235, commercial: 1,950 + 12 % x (166,719.077 - 20,000) = 19,556.29
    (8) PTV fee, commercial: 60.00
    Total 186,335.37"""
    sid, conn = _a3_site()
    _flat_rows(sid, date(2026, 8, 26), 5, 30.0)                                 # 26 to 30 Aug: today's readings are there, and not billed
    so = verify.bill_so_far(sid, conn, date(2026, 8, 30))
    assert so == {"total_rs": SO_FAR_TOTAL, "first_day": "2026-08-26", "last_day": "2026-08-29", "reading": "2026-09-25", "billing_month": "2026-09",
                  "days": 4, "completeness": 1.0, "estimated": False, "metering": "uncalibrated"}
    # the Bills page's own rebuild of those days, line by line
    bill = verify.rebuild_bill(sid, conn, date(2026, 8, 26), date(2026, 8, 29), billed_on=date(2026, 9, 25))
    got = [(ln["line"], ln["amount_rs"]) for ln in bill["lines"]]
    want = [("Energy", 122342.40), ("Fixed charges", 133.33), ("FCA July 2026", 5927.33), ("QTA Q2 CY2026", 1495.87), (DSS, 9302.40),
            ("Electricity duty", 2086.02), ("GST", 25431.72), ("Income tax (s.235)", 19556.29), ("Flat fees (PTV, MUCT)", 60.00)]
    assert [n for n, _ in got] == [n for n, _ in want]
    for (name, amount), (_, expected) in zip(got, want):
        assert amount == pytest.approx(expected, abs=0.001), name
    assert bill["total_rs"] == SO_FAR_TOTAL and bill["billing_month"] == "2026-09"
    fixed = next(ln for ln in bill["lines"] if ln["line"] == "Fixed charges")
    assert (fixed["days"], fixed["of_days"]) == (4, 30)
    # without the bill's own month the adders would be August's (the regression the reading date's month closes)
    august = verify.rebuild_bill(sid, conn, date(2026, 8, 26), date(2026, 8, 29))
    assert august["billing_month"] == "2026-08" and august["total_rs"] != SO_FAR_TOTAL
    assert [(a["key"], a["id"]) for a in tariffs.adders_for("2026-08")] != [(a["key"], a["id"]) for a in tariffs.adders_for("2026-09")]


def test_the_grid_bill_so_far_takes_the_credit_carried_and_the_calibrated_metering_factor(company):
    sid, conn = _a3_site()
    _flat_rows(sid, date(2026, 8, 26), 4, 30.0)
    day = date(2026, 8, 30)
    # the bill before (to the 25 Aug reading) carried Rs 5,000 of export credit: the bill so far takes it, as entering this bill would
    with store.connect() as con:
        con.execute("INSERT INTO bills (id, site_id, connection_id, period_start, period_end, real, rebuilt, difference_pct, created) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", ("b-before", sid, conn["id"], "2026-07-26", "2026-08-25", json.dumps({"total_rs": 1.0}),
                                                   json.dumps({"credit_carried_rs": 5000.0}), None, store._now()))
    so = verify.bill_so_far(sid, conn, day)
    assert so["total_rs"] == pytest.approx(SO_FAR_TOTAL - 5000.0, abs=0.005)
    # a metering factor fitted on the connection's bills and applied: the minutes are scaled to the DISCO's register, as the ledger prices them
    sites.put_state(sid, metering={conn["id"]: {**verify._metering_blank("calibrated", 3), "energy": 1.02, "mdi": 1.0, "applied": True,
                                                "gate": "closed"}})
    so = verify.bill_so_far(sid, conn, day)
    scaled = verify.rebuild_bill(sid, conn, date(2026, 8, 26), date(2026, 8, 29), credit_brought_forward_rs=5000.0, energy_scalar=1.02,
                                 billed_on=date(2026, 9, 25))
    assert so["metering"] == "calibrated" and so["total_rs"] == scaled["total_rs"] > SO_FAR_TOTAL - 5000.0
    assert next(ln for ln in scaled["lines"] if ln["line"] == "Energy")["units_kwh"] == pytest.approx(2880 * 1.02, abs=0.001)


def test_the_grid_bill_so_far_is_none_where_nothing_can_be_billed_and_estimated_over_gaps(company):
    sid, conn = _a3_site()
    assert verify.bill_so_far(sid, conn, date(2026, 8, 30)) is None             # no reading in the period: nothing to rebuild from
    _flat_rows(sid, date(2026, 8, 26), 1, 30.0)                                 # the 26th only
    assert verify.bill_so_far(sid, conn, date(2026, 8, 26)) is None             # the period's first day: no whole day billed yet
    so = verify.bill_so_far(sid, conn, date(2026, 8, 30))                      # 1 day read of 4: the rest filled from its profile, an estimate
    assert so["completeness"] == 0.25 and so["estimated"] is True and so["total_rs"] == SO_FAR_TOTAL
    # the readings of today change nothing: only whole days are billed
    _flat_rows(sid, date(2026, 8, 30), 1, 900.0)
    assert verify.bill_so_far(sid, conn, date(2026, 8, 30))["total_rs"] == SO_FAR_TOTAL
    # a connection the readings do not meter, and an off-grid one, have no bill so far
    assert verify.bill_so_far(sid, {**conn, "plant": False}, date(2026, 8, 30)) is None
    assert verify.bill_so_far(sid, {**conn, "data": {**conn["data"], "regime": "off_grid"}}, date(2026, 8, 30)) is None
    assert verify.bill_so_far(sid, None, date(2026, 8, 30)) is None


def test_the_overview_s_money_band_reads_the_grid_bill_so_far_and_a_viewer_never_does(world, monkeypatch):
    sid = world.site                                                           # Mill One: B-2(b) at FESCO, no reading day: the calendar month
    d = date(2026, 10, 5)
    monkeypatch.setattr(routes_sites, "today", lambda: d)
    monkeypatch.setattr(routes_sites, "now_ts", lambda: twin.day_start_utc(d) + 10 * 3600)
    with world.use():
        _flat_rows(sid, date(2026, 10, 1), 5, 100.0)
        so = verify.bill_so_far(sid, sites.connections(sid)[0], d)
    assert so["total_rs"] > 0
    for role in ("admin", "operator", "analyst"):                              # see_costs
        month = world.c[role].get(f"/api/sites/{sid}/now").json()["month"]
        basis = month["grid_bill_rs_basis"]
        assert month["grid_bill_rs"] == so["total_rs"], role
        assert {k: basis[k] for k in so if k != "total_rs"} == {k: v for k, v in so.items() if k != "total_rs"}, role
        assert (basis["first_day"], basis["last_day"], basis["reading"], basis["billing_month"]) == ("2026-10-01", "2026-10-04", "2026-10-31", "2026-10")
        assert basis["computed"].endswith(KARACHI) and set(basis) == set(so) - {"total_rs"} | {"computed"}
    viewer = world.c["viewer"].get(f"/api/sites/{sid}/now")
    assert viewer.status_code == 200 and "grid_bill" not in viewer.text
    # a change the audit trail records (here the connection's sanctioned load, which sets B-2(b)'s billing demand floor: half of it, above the
    # 100 kW read) is read again at once
    assert world.c["admin"].put(f"/api/sites/{sid}/connection", json={"sanctioned_load_kw": 2000}).status_code == 200
    with world.use():
        again = verify.bill_so_far(sid, sites.connections(sid)[0], d)
    assert again["total_rs"] != so["total_rs"]
    assert world.c["admin"].get(f"/api/sites/{sid}/now").json()["month"]["grid_bill_rs"] == again["total_rs"]
    # off-grid, and a site with no connection: no bill
    with world.use():
        conn_id = sites.connections(sid)[0]["id"]
    assert world.c["admin"].put(f"/api/sites/{sid}/connection", json={"id": conn_id, "regime": "off_grid"}).status_code == 200
    month = world.c["admin"].get(f"/api/sites/{sid}/now").json()["month"]
    assert month["grid_bill_rs"] is None and month["grid_bill_rs_basis"] is None
    month = world.c["admin"].get(f"/api/sites/{world.other}/now").json()["month"]
    assert month["grid_bill_rs"] is None and month["grid_bill_rs_basis"] is None


def test_a_tariff_that_cannot_price_the_period_leaves_the_band_with_the_saving_alone(world, monkeypatch):
    sid = world.site
    d = date(2026, 10, 5)
    monkeypatch.setattr(routes_sites, "today", lambda: d)
    with world.use():
        _flat_rows(sid, date(2026, 10, 1), 4, 100.0)

    def unpriced(*a, **k):
        raise LookupError("no schedule version in force")
    monkeypatch.setattr(verify, "rebuild_bill", unpriced)
    r = world.c["admin"].get(f"/api/sites/{sid}/now")
    assert r.status_code == 200 and r.json()["month"]["grid_bill_rs"] is None and r.json()["month"]["grid_bill_rs_basis"] is None


def test_the_bill_engine_s_default_is_unchanged_by_the_billing_month_option(company):
    """`billed_on` left out bills the period's last day's month, as every bill before K18 (the Bills page's rebuild of an entered bill ends on its
    reading date, so the two are the same there)."""
    from test_fix_tariffs import _conn, _minutes
    conn = _conn("A-3", "flat", 40.0)
    ts = _minutes(date(2026, 8, 26), 36)
    imp = np.full(len(ts), 30.0)
    one = tariffs.rebuild(ts, imp, np.zeros(len(ts)), conn, date(2026, 8, 25), date(2026, 9, 30), period_start_exclusive=True)
    two = tariffs.rebuild(ts, imp, np.zeros(len(ts)), conn, date(2026, 8, 25), date(2026, 9, 30), period_start_exclusive=True,
                          billed_on=date(2026, 9, 30))
    assert one == two and one["billing_month"] == "2026-09"


def test_the_grid_bill_so_far_is_rebuilt_once_for_the_overview_s_polls_and_again_after_an_audited_change(world, monkeypatch):
    """The rebuild reads up to a month of minutes and the Overview polls every 30 seconds: the answer is kept for BILL_SO_FAR_REUSE_S while the
    company's audit trail is unchanged (a bill entered, a tariff, a connection or a metering change each writes a line), and a viewer's read does
    not rebuild it at all."""
    sid = world.site
    d = date(2026, 10, 5)
    monkeypatch.setattr(routes_sites, "today", lambda: d)
    with world.use():
        _flat_rows(sid, date(2026, 10, 1), 4, 100.0)
    calls = []
    real = verify.bill_so_far
    monkeypatch.setattr(verify, "bill_so_far", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    # a viewer reads first, on an empty store: the server does not work the bill out for a reader without see_costs (K18 check: the viewer used to
    # read after the administrator, on a warm store whose key is not the role's, so the gate went untested)
    routes_sites._BILL_SO_FAR.items.clear()
    assert world.c["viewer"].get(f"/api/sites/{sid}/now").status_code == 200
    assert calls == []
    first = world.c["admin"].get(f"/api/sites/{sid}/now").json()["month"]
    world.c["operator"].get(f"/api/sites/{sid}/now")
    world.c["viewer"].get(f"/api/sites/{sid}/now")
    assert len(calls) == 1 and first["grid_bill_rs"] is not None
    with world.use():
        store.audit("test", "changed something the bill reads", sid, {})
    assert world.c["admin"].get(f"/api/sites/{sid}/now").json()["month"]["grid_bill_rs"] == first["grid_bill_rs"]
    assert len(calls) == 2


# ------------------------------------------------------------------------------------------------ K18 check

def _bill_row(sid: str, conn_id: str, first: str, end: str, credit: float = 0.0) -> None:
    """A bill of the connection as entering one stores it (its first day and reading date, both billed), carrying `credit` forward."""
    with store.connect() as con:
        con.execute("INSERT INTO bills (id, site_id, connection_id, period_start, period_end, real, rebuilt, difference_pct, created) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (store.new_id(), sid, conn_id, first, end, json.dumps({"total_rs": 1.0}),
                                                   json.dumps({"credit_carried_rs": credit}), None, store._now()))


def test_golden_the_bill_so_far_starts_the_day_after_the_bill_before_was_read_and_takes_its_credit(company):
    """K18 check: the bill so far started on the day after the connection's reading day and took the credit of a bill ending exactly the day before.
    The DISCO read the meter on 27 Aug, two days after the reading day (the 25th), and that bill carried Rs 5,000 forward: the bill so far billed 26 and
    27 Aug a second time and dropped the credit. It now starts the day after the bill's own reading.

    The same A-3 site and rates as the golden above. Today is 30 Aug: 28 and 29 Aug are billed, 2 whole days, in September's bill (the 25 Sep reading).

    Units 2 x 24 x 30 = 1,440 kWh.

    (1) Energy            1,440 x 42.48 = 61,171.20
    (2) Fixed charges     1,000 x 2 / 30 = 66.67
    (3) FCA 1,440 x 2.0581 = 2,963.66; QTA 1,440 x 0.5194 = 747.94; DSS 1,440 x 3.23 = 4,651.20
    (4) Electricity duty 1.5 % x 69,534.00 = 1,043.01
    (5) GST 18 % x (69,534.00 + 66.667 + 1,043.01 = 70,643.677) = 12,715.86
    (6) Income tax s.235, commercial: 1,950 + 12 % x (83,359.538 - 20,000) = 9,553.14
    (8) PTV fee, commercial: 60.00
    Charges 92,972.68, less the Rs 5,000 credit brought forward = 87,972.68"""
    sid, conn = _a3_site()
    _flat_rows(sid, date(2026, 8, 26), 5, 30.0)
    _bill_row(sid, conn["id"], "2026-07-26", "2026-08-27", credit=5000.0)
    so = verify.bill_so_far(sid, conn, date(2026, 8, 30))
    assert so == {"total_rs": 87972.68, "first_day": "2026-08-28", "last_day": "2026-08-29", "reading": "2026-09-25", "billing_month": "2026-09",
                  "days": 2, "completeness": 1.0, "estimated": False, "metering": "uncalibrated"}
    bill = verify.rebuild_bill(sid, conn, date(2026, 8, 28), date(2026, 8, 29), credit_brought_forward_rs=5000.0, billed_on=date(2026, 9, 25))
    want = [("Energy", 61171.20), ("Fixed charges", 66.67), ("FCA July 2026", 2963.66), ("QTA Q2 CY2026", 747.94), (DSS, 4651.20),
            ("Electricity duty", 1043.01), ("GST", 12715.86), ("Income tax (s.235)", 9553.14), ("Flat fees (PTV, MUCT)", 60.00)]
    got = {ln["line"]: ln["amount_rs"] for ln in bill["lines"]}
    for name, expected in want:
        assert got[name] == pytest.approx(expected, abs=0.001), name
    assert bill["total_rs"] == 87972.68
    # the day after that reading: no whole day billed yet
    assert verify.bill_so_far(sid, conn, date(2026, 8, 28)) is None


def test_the_bill_so_far_follows_a_reading_taken_early_and_leaves_an_old_bill_to_the_reading_day(company):
    sid, conn = _a3_site()
    _flat_rows(sid, date(2026, 8, 20), 36, 30.0)                                # 20 Aug to 24 Sep
    # a bill from June ends long before this period: the reading day stands (26 Aug, the 25 Sep reading), and it carries nothing into this bill
    _bill_row(sid, conn["id"], "2026-05-26", "2026-06-25", credit=5000.0)
    so = verify.bill_so_far(sid, conn, date(2026, 8, 30))
    assert (so["first_day"], so["reading"], so["days"], so["total_rs"]) == ("2026-08-26", "2026-09-25", 4, SO_FAR_TOTAL)
    # the August reading taken two days early (23 Aug): the bill so far runs from 24 Aug, 6 days to 29 Aug, in September's bill
    _bill_row(sid, conn["id"], "2026-07-26", "2026-08-23")
    so = verify.bill_so_far(sid, conn, date(2026, 8, 30))
    assert (so["first_day"], so["last_day"], so["reading"], so["billing_month"], so["days"]) == ("2026-08-24", "2026-08-29", "2026-09-25", "2026-09", 6)
    assert so["total_rs"] == 279698.05                                         # 6 days of the golden's bill: 4,320 kWh, worked as above
    # the September reading taken three days early (22 Sep), with Rs 1,000 carried: on 24 Sep the bill so far is 23 Sep alone, in October's bill
    _bill_row(sid, conn["id"], "2026-08-24", "2026-09-22", credit=1000.0)
    so = verify.bill_so_far(sid, conn, date(2026, 9, 24))
    assert (so["first_day"], so["last_day"], so["reading"], so["billing_month"], so["days"]) == ("2026-09-23", "2026-09-23", "2026-10-25", "2026-10", 1)
    alone = verify.rebuild_bill(sid, conn, date(2026, 9, 23), date(2026, 9, 23), credit_brought_forward_rs=1000.0, billed_on=date(2026, 10, 25))
    assert so["total_rs"] == alone["total_rs"]
    # once the October reading day has passed with no bill entered for it, the reading day stands again
    _flat_rows(sid, date(2026, 9, 25), 36, 30.0)
    so = verify.bill_so_far(sid, conn, date(2026, 10, 28))
    assert (so["first_day"], so["reading"]) == ("2026-10-26", "2026-11-25")


def test_the_grid_bill_so_far_read_while_a_bill_is_entered_is_read_again_once_its_metering_factor_is_fitted(world, monkeypatch):
    """K18 check: entering a bill writes its audit line before the credits are chained and the metering factor fitted again. The bill so far was kept
    under the newest audit line, so a /now read in that gap kept the uncalibrated figure for 15 minutes. It is now kept under what it reads: its
    period, the credit carried into it and the metering factor."""
    sid = world.site
    d = date(2026, 10, 5)
    monkeypatch.setattr(routes_sites, "today", lambda: d)
    with world.use():
        _flat_rows(sid, date(2026, 10, 1), 4, 100.0)
    gap = []
    real_refit = verify.refit_metering

    def refit_while_read(site_id, connection_id):
        # the Overview polls between the bill's audit line and the fit
        gap.append(world.c["admin"].get(f"/api/sites/{sid}/now").json()["month"]["grid_bill_rs"])
        real_refit(site_id, connection_id)
        # the fit finds a factor (as three bills or more would): the readings are scaled to the DISCO's register
        sites.put_state(site_id, metering={connection_id: {**verify._metering_blank("calibrated", 3), "energy": 1.05, "mdi": 1.0, "applied": True,
                                                           "gate": "closed"}})
    monkeypatch.setattr(verify, "refit_metering", refit_while_read)
    r = world.c["admin"].post(f"/api/sites/{sid}/bills", json={"period_start": "2026-09-01", "period_end": "2026-09-30", "total_rs": 500000})
    assert r.status_code == 200, r.text
    with world.use():
        calibrated = verify.bill_so_far(sid, sites.connections(sid)[0], d)
    assert gap and gap[0] is not None and calibrated["metering"] == "calibrated" and calibrated["total_rs"] != gap[0]
    month = world.c["admin"].get(f"/api/sites/{sid}/now").json()["month"]
    assert month["grid_bill_rs"] == calibrated["total_rs"] and month["grid_bill_rs_basis"]["metering"] == "calibrated"
