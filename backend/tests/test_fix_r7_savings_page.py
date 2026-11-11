"""Round-7 regression tests for the Savings page's package (savings-page, stage 2): the fee check's figure and what it rests on, and the claimed
wording of the reports, from the end-to-end audit.

Each test fails on the code before its fix. Test data: hand-made day rows and a stubbed summary only; nothing here calls the network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from test_fix_api import world  # noqa: F401 (a fixture)
from test_fix_r7_alerts_reports import _baseline, _day_row, _fake_summary, _past_month, _story, _tday, _with_summary
from test_fix_savings import _days, _put_rows, _row, company, make_site  # noqa: F401 (company is a fixture)

from gridlytics import store, tariffs, verify

PKT = timezone(timedelta(hours=5))


def _rows(d0: date, d1: date, native: float, tuned: float) -> dict:
    return {d.isoformat(): {"native": _row(native, native), "tuned": _row(tuned, tuned)} for d in _days(d0, d1)}


# ------------------------------------------------------------------------------------------------ pages-D-01: the fee check's basis

def test_the_fee_check_names_the_baseline_the_claim_and_the_days_its_monthly_figure_rests_on(company):
    """pages-D-01 (and its duplicates flow-savings-pilot-bill-04, role-owner-03, engine-money-live-12): the fee check scaled a few days' claim to the
    whole month, took the smaller of the native and tuned baselines and answered only the figure, so the card called it 'Verified saving a month ...
    against the native settings'. The answer now says which baseline each month's figure is from, what was claimed over how many scored days of how
    many, that a part month is scaled, and that the figure is preliminary."""
    sid = make_site(name="Fee basis")
    store.set_setting(f"fee:{sid}", 5000.0)
    _put_rows(sid, _rows(date(2026, 11, 1), date(2026, 11, 6), native=1000.0, tuned=800.0))
    fee = verify.summary(sid, "2026-11-01", "2026-11-06", see_fee=True)["fee"]
    assert fee["verified_saving_rs_per_month"] == 24000.0           # unchanged: 800 a day over the 30 days of November
    assert fee["baseline"] == "tuned"                                # before: no baseline; the card said "the native settings"
    assert fee["claimed_rs"] == 4800.0 and fee["scored_days"] == 6 and fee["claiming_days"] == 6
    assert fee["scaled"] is True and fee["preliminary"] is True
    assert fee["months"] == [{"month": "2026-11", "baseline": "tuned", "claimed_rs": 4800.0, "scored_days": 6, "claiming_days": 6,
                              "days_in_month": 30, "rs_per_month": 24000.0}]
    assert "smaller of the native and tuned" in fee["basis"]
    # the native figure is the smaller in October: the months name their own baselines, and the whole answer says both
    _put_rows(sid, _rows(date(2026, 10, 25), date(2026, 10, 31), native=500.0, tuned=900.0))
    fee = verify.summary(sid, "2026-10-25", "2026-11-06", see_fee=True)["fee"]
    assert [(m["month"], m["baseline"], m["scored_days"], m["days_in_month"]) for m in fee["months"]] == [("2026-10", "native", 7, 31),
                                                                                                        ("2026-11", "tuned", 6, 30)]
    assert fee["baseline"] == "both" and fee["claimed_rs"] == 7 * 500.0 + 4800.0 and fee["scored_days"] == 13
    assert fee["verified_saving_rs_per_month"] == pytest.approx((500.0 * 31 + 800.0 * 30) / 2, abs=1)
    # no scored day: nothing to rest on, so no baseline and still preliminary
    empty = make_site(name="Fee empty")
    store.set_setting(f"fee:{empty}", 5000.0)
    fee = verify.summary(empty, "2026-11-01", "2026-11-06", see_fee=True)["fee"]
    assert fee["months"] == [] and fee["baseline"] is None and fee["scored_days"] == 0 and fee["preliminary"] is True
    assert fee["within_one_third"] is None                            # before: False, "Fee above one third" with nothing scored


def test_the_fee_check_is_preliminary_on_a_part_month_even_when_the_method_is_complete(company, monkeypatch):
    """pages-D-01: the verdict could read 'within one third of the verified saving' on three days of data. A whole month scored with the method complete
    is the verified figure; a part month scaled to a whole one stays preliminary, whatever the page's other conditions."""
    monkeypatch.setattr(verify, "VERIFIED_DAYS_FOR_FINAL", 1)
    monkeypatch.setattr(verify, "SWITCHBACK_WEEKS_FOR_FINAL", 0)
    monkeypatch.setattr(tariffs, "fca_billed", lambda month: True)
    sid = make_site(name="Fee final")
    store.set_setting(f"fee:{sid}", 5000.0)
    _put_rows(sid, _rows(date(2026, 10, 1), date(2026, 10, 31), native=1000.0, tuned=800.0))
    whole = verify.summary(sid, "2026-10-01", "2026-10-31", see_fee=True)
    assert whole["preliminary"] is False
    assert whole["fee"]["scaled"] is False and whole["fee"]["preliminary"] is False and whole["fee"]["claimed_rs"] == 31 * 800.0
    part = verify.summary(sid, "2026-10-01", "2026-10-03", see_fee=True)
    assert part["preliminary"] is False                               # the page's own conditions are met ...
    assert part["fee"]["scaled"] is True and part["fee"]["preliminary"] is True   # ... but three days scaled to 31 are a projection


def test_the_fee_notice_calls_the_saving_verified_only_when_the_fee_check_is_not_preliminary(world, monkeypatch):
    """pages-D-01: the Product notice read 'The verified saving of November 2026 so far is under three times the fee' on a month scored in part and
    scaled to the whole month. It now says the figure is scaled and preliminary, and calls it verified only once the fee check is not preliminary."""
    from gridlytics import alerts
    from test_fix_r7_alerts_reports import _ready
    sid = world.site
    day = datetime.now(PKT).date() - timedelta(days=1)
    _ready(monkeypatch)
    month = f"{day:%B %Y}"
    with world.use():
        store.set_setting(f"fee:{sid}", 50000.0)
        for prelim, want in ((True, f"The saving of {month} so far, scaled to the whole month, is under three times the fee (preliminary)"),
                             (False, f"The verified saving of {month} is under three times the fee")):
            monkeypatch.setattr(verify, "summary", lambda site_id, d0, d1, see_fee=False, p=prelim: {"fee": {"within_one_third": False,
                                                                                                             "preliminary": p}})
            alerts.nightly(sid, day)
            (got,) = [a for a in alerts.listing("active", sid) if a["rule"] == "fee_above_third"]
            assert got["title"] == want, got["title"]


# ------------------------------------------------------------------------------------------------ pages-D-21: the claimed wording

def test_the_reports_say_which_days_claim_and_never_that_the_claim_counts_the_verified_days_only(world, monkeypatch):
    """pages-D-21: a partly followed day claims its saving but is not a verified day, yet the monthly PDF headed the claim 'Claimed (verified days
    only)' and said 'the claimed figures count the verified days only', and the company roll-up the same. The reports now count the days that claim
    beside the verified days and say what each is."""
    from gridlytics import reports
    sid, month = world.site, _past_month()
    d1, d2 = f"{month}-03", f"{month}-04"
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(d1), _day_row(d2, fidelity="partial")], native=_baseline(claiming_days=2, verified_days=1),
                                             tuned=_baseline(claiming_days=2, verified_days=1, claimed_rs=780.0)), [_tday(d1), _tday(d2)])
    built = _story(monkeypatch)
    with world.use():
        reports.generate("monthly_savings", sid, month, "test")
        reports.generate("company_rollup", None, month, "test")
    monthly, rollup = built[-2], built[-1]
    for text in (monthly, rollup):
        assert "verified days only" not in text, text
    rows = {ln.split(" | ")[0]: ln for ln in monthly.splitlines() if " | " in ln}
    assert "Claimed (plan followed in full or in part)" in rows, rows
    assert rows["Days that claim (plan followed in full or in part)"].split(" | ")[1:] == ["2", "2"], rows
    assert rows["Verified days (plan followed in full)"].split(" | ")[1:] == ["1", "1"], rows
    assert "every scored day" in monthly and "followed in full or in part" in monthly, monthly
    assert "a verified day is one whose plan was followed in full" in rollup, rollup
