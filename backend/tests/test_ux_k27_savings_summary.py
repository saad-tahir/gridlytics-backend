"""UX backend ask K27 (plan H11; F11 item 1, F1.1.3 to F1.1.9; step 11a, Savings › Counterfactual): the savings summary's additions, each figure
by one function that the Savings page's answer (verify.summary) and the monthly savings statement (reports.py) both call.

- per day: the native arm's peak-window import (`peak_import_kwh_native`) and the plan in force at the day's start (`plan_id`); the capture version
  each day was replayed on was answered before (`native_capture_version`, and the page's `native_captures`);
- per baseline: `peak_import_kwh_avoided` (energy, every reader) over `peak_import_days`, `fuel_saved` in `fuel_unit` (litres of diesel, MMBtu of gas)
  over `fuel_days` (both on the claim's days, K13's rule: the review of batches k7 and k8),
  `taxes_recovered_rs` on the claim (money: a viewer's answer has none), and for the tuned baseline its table's rows (`rule_table`);
- the tariff notices the period reaches (`tariff_notices`), and Preliminary by the screen's rule and by the report's (`preliminary_rules`; the owner's
  question Q60, plan F0.1 decision 15).

Every money figure here has a golden test. Test data: hand-made day rows on a site of the savings tests (test_fix_savings.make_site: B-2(b), FESCO,
a diesel genset, a battery, a plan made in 2015 in force on every day)."""
from __future__ import annotations

import json
from datetime import date

import pytest
from test_fix_api import world  # noqa: F401  (a fixture)
from test_fix_r7_alerts_reports import _baseline, _day_row, _fake_summary, _past_month, _story, _tday, _with_summary
from test_fix_savings import _put_rows, _row, company, make_site  # noqa: F401  (company is a fixture)

from gridlytics import money, savings_export, sites, store, tariffs, verify

#: B-2(b) under the shipped tax rules of November 2026, GST recovered and s.235 adjusted (the connection's defaults): GST 18 % plus the s.235
#: advance of 5 % on the bill amount with its GST (0.05 x 1.18), over a bearable stack of 1 (no further or extra tax for a registered, listed company)
SHARE = 0.18 + 0.05 * 1.18
TUNED_TABLE = [{"start": "00:00", "mode": "self_use", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False},
               {"start": "17:00", "mode": "discharge", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False},
               {"start": "21:00", "mode": "self_use", "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "grid_charge": False}]


def _day(saving: float, levers: dict, fidelity: str = "confirmed", peak_native: float = 0.0, peak_actual: float = 0.0, litres: float | None = 0.0,
         **over) -> dict:
    r = _row(saving, saving if fidelity in verify.CLAIMING else 0.0, fidelity)
    r["levers"] = levers
    r["litres_saved"] = litres
    r["actual"]["peak_import_kwh"] = peak_actual
    r["baseline"]["peak_import_kwh"] = peak_native
    return {**r, **over}


def _rows() -> dict:
    """Four November days against the native settings and the tuned table:
    1 Nov confirmed: peak window Rs 1,000, off-peak -Rs 200 (claims Rs 800); 400 kWh in the peak on the native settings, 80 actual; 10 L saved.
    2 Nov confirmed: peak window Rs 500 (claims Rs 500); 300 and 100 kWh; 5 L.
    3 Nov not applied: peak window Rs 300 (claims nothing); 200 and 150 kWh; 2 L.
    4 Nov withheld, the import unmetered: its peak-window figures are no reading; nothing claimed."""
    native = {"2026-11-01": _day(800.0, {"peak_window": 1000.0, "offpeak_energy": -200.0}, peak_native=400.0, peak_actual=80.0, litres=10.0),
              "2026-11-02": _day(500.0, {"peak_window": 500.0, "offpeak_energy": 0.0}, peak_native=300.0, peak_actual=100.0, litres=5.0),
              "2026-11-03": _day(300.0, {"peak_window": 300.0}, fidelity="not_applied", peak_native=200.0, peak_actual=150.0, litres=2.0),
              "2026-11-04": _day(0.0, {"peak_window": None, "offpeak_energy": None}, peak_native=999.0, peak_actual=0.0, litres=0.0,
                                 import_unknown=True, claim_withheld="import_unknown")}
    tuned = {d: {**r, "levers": {k: (v / 2 if v is not None else None) for k, v in r["levers"].items()},
                 "baseline": {**r["baseline"], "peak_import_kwh": r["baseline"]["peak_import_kwh"] / 2}} for d, r in native.items()}
    tuned["2026-11-02"]["tuned_rule"] = {"kind": "tou_variant", "table": TUNED_TABLE, "cycle_soc_pct": None, "fit": {"days": [], "cost_rs": 1.0,
                                                                                                                       "native_cost_rs": 2.0}}
    return {d: {"native": native[d], "tuned": tuned[d]} for d in native}


def test_the_peak_window_import_avoided_counts_the_days_that_claim(company):
    """Golden: (400 - 80) + (300 - 100) = 520 kWh against the native settings over the 2 claiming days; the not-applied day's replay (200 - 150) is
    no saving of the plan's, and the withheld day's unmetered import is no reading (K13's rule, verify.claimed_energy: the energy a day claims beside
    its rupees). Against the tuned table the baseline's peak import is half: (200 - 80) + (150 - 100) = 170 kWh. Each day carries its native arm's
    peak-window import, a day that claims nothing too."""
    sid = make_site(name="K27 peak")
    _put_rows(sid, _rows())
    s = verify.summary(sid, "2026-11-01", "2026-11-04")
    nat, tun = s["baselines"]["native"], s["baselines"]["tuned"]
    assert (nat["peak_import_kwh_avoided"], nat["peak_import_days"]) == (520.0, 2)
    assert (tun["peak_import_kwh_avoided"], tun["peak_import_days"]) == (170.0, 2)
    assert nat["peak_import_days"] <= nat["claiming_days"] == 2
    assert [d["peak_import_kwh_native"] for d in s["days"]] == [400.0, 300.0, 200.0, 999.0]
    assert verify.peak_import_avoided([{"actual": {}, "baseline": {}}]) == (0.0, 0)          # a row stored without the figures adds nothing
    # a day withheld for another reason than its import (its import known) claims nothing either: its replay ran over unread load or PV
    unread = _day(0.0, {"peak_window": 50.0}, peak_native=500.0, peak_actual=100.0, claim_withheld="inputs_unread")
    assert verify.peak_import_avoided([unread]) == (0.0, 0)


def test_the_savings_page_and_fleet_say_one_peak_window_figure_for_the_month(world, monkeypatch):
    """K27 check: the Fleet row's month figure (`mtd_peak_kwh_avoided`, K13) and the Savings summary's tile for the same month are one figure, by one
    rule (verify.claimed_energy): 520 kWh over the claiming days, never the not-applied day's replay nor the withheld day."""
    from gridlytics import routes_sites
    monkeypatch.setattr(routes_sites, "today", lambda: date(2026, 11, 4))
    w = world
    with w.use():
        _put_rows(w.site, _rows())
    row = next(r for r in w.c["admin"].get("/api/fleet").json()["sites"] if r["id"] == w.site)
    page = w.c["admin"].get(f"/api/sites/{w.site}/savings", params={"start": "2026-11-01", "end": "2026-11-04"}).json()
    assert row["mtd_peak_kwh_avoided"] == page["baselines"]["native"]["peak_import_kwh_avoided"] == 520.0
    # the diesel avoided too (review of batches k7 and k8): Fleet's month figure and the Savings tile are 15 L over the 2 claiming days, never the
    # not-applied day's 2 L of replay nor the withheld day's
    assert row["mtd_litres_saved"] == page["baselines"]["native"]["fuel_saved"] == 15.0
    assert page["baselines"]["native"]["fuel_days"] == 2
    viewer = w.c["viewer"].get(f"/api/sites/{w.site}/savings", params={"start": "2026-11-01", "end": "2026-11-04"}).json()
    assert viewer["baselines"]["native"]["peak_import_kwh_avoided"] == 520.0
    assert viewer["baselines"]["native"]["fuel_saved"] == 15.0


def test_a_period_with_no_day_to_count_answers_no_figure_never_a_zero(company):
    """K27 check (K12's rule, "null without its data, never a zero"): a grid site with a genset before its first scored day answers no peak-window
    figure, no fuel saved and no taxes recovered; a period whose days claim nothing answers none of the three either: the fuel the replays saved
    on days the plan did not run is the replay's, as their peak-window import is (K13's rule; review of batches k7 and k8: the fuel saved counted
    every scored day, 17 L here, while the peak-window figure beside it counted none)."""
    sid = make_site(name="K27 empty")
    nat = verify.summary(sid, "2026-11-01", "2026-11-04")["baselines"]["native"]
    assert (nat["peak_import_kwh_avoided"], nat["peak_import_days"], nat["fuel_saved"], nat["fuel_days"], nat["taxes_recovered_rs"]) == \
        (None, 0, None, 0, None)
    assert nat["fuel_unit"] == "L"
    rows = _rows()
    rows = {d: {b: {**r, "fidelity": "not_applied", "claimed_rs": 0.0} for b, r in by.items()} for d, by in rows.items() if d != "2026-11-04"}
    _put_rows(sid, rows)
    nat = verify.summary(sid, "2026-11-01", "2026-11-04")["baselines"]["native"]
    assert nat["claiming_days"] == 0 and nat["litres_saved"] == 17.0                  # the ledger's every scored day, as before
    assert (nat["peak_import_kwh_avoided"], nat["peak_import_days"], nat["taxes_recovered_rs"]) == (None, 0, None)
    assert (nat["fuel_saved"], nat["fuel_days"]) == (None, 0)
    assert verify.fuel_saved([], True) == (None, 0)


def test_the_taxes_recovered_are_negative_where_the_claim_s_grid_levers_are_golden(company):
    """Golden: a claiming day whose plan bought more grid energy than the native settings would have (off-peak charging): peak window Rs 200,
    off-peak -Rs 700, grid levers -Rs 500 x 0.239 = -Rs 119.50. The bills carried that much more of the tax the company recovers: a negative figure,
    as the levers are, never turned positive."""
    sid = make_site(name="K27 negative taxes")
    neg = {"2026-11-01": {"native": _day(-500.0, {"peak_window": 200.0, "offpeak_energy": -700.0}, peak_native=300.0, peak_actual=100.0)}}
    _put_rows(sid, neg)
    s = verify.summary(sid, "2026-11-01", "2026-11-01")
    assert s["baselines"]["native"]["taxes_recovered_rs"] == pytest.approx(-500.0 * SHARE, abs=0.005) == -119.50


def test_the_taxes_recovered_on_the_claim_are_the_recovered_share_of_its_grid_levers_golden(company):
    """Golden: the claiming days' grid levers are Rs 800 (1 Nov: 1,000 - 200) and Rs 500 (2 Nov); the not-applied and withheld days claim nothing.
    0.239 of a bearable rupee is recovered: Rs 1,300 x 0.239 = Rs 310.70 against the native settings, Rs 155.35 against the tuned table (half the
    levers). A company that recovers neither tax recovers Rs 0; an off-grid site has no bill to recover from (null)."""
    assert tariffs.recoverable_share(sites.connections(make_site(name="K27 share"))[0], date(2026, 11, 1)) == pytest.approx(SHARE)
    sid = make_site(name="K27 taxes")
    _put_rows(sid, _rows())
    s = verify.summary(sid, "2026-11-01", "2026-11-04")
    assert s["baselines"]["native"]["taxes_recovered_rs"] == pytest.approx(1300.0 * SHARE, abs=0.005) == 310.70
    assert s["baselines"]["tuned"]["taxes_recovered_rs"] == pytest.approx(650.0 * SHARE, abs=0.005) == 155.35
    # the MDI share of a claiming day is a taxed charge too: a Rs 100 share adds Rs 23.90
    assert verify.taxes_recovered(sites.connections(sid)[0], [("2026-11-01", {"peak_window": 0.0}, 100.0)]) == 23.90
    bears = make_site(name="K27 bears", conn={"tariff_category": "B-2(b)", "sanctioned_load_kw": 500, "regime": "none",
                                              "tax": {"strn": False, "s235_adjustable": False}})
    _put_rows(bears, _rows())
    assert verify.summary(bears, "2026-11-01", "2026-11-04")["baselines"]["native"]["taxes_recovered_rs"] == 0.0
    off = make_site(name="K27 off grid", conn={"regime": "off_grid"})
    _put_rows(off, _rows())
    assert verify.summary(off, "2026-11-01", "2026-11-04")["baselines"]["native"]["taxes_recovered_rs"] is None
    assert verify.summary(off, "2026-11-01", "2026-11-04")["baselines"]["native"]["peak_import_kwh_avoided"] is None


def test_the_fuel_saved_counts_the_days_that_claim_in_the_genset_s_own_unit(company):
    """Golden: 15 L of diesel saved over the 2 claiming days (10 + 5), the figure Fleet's month says (K13's rule, verify.claimed_energy): the
    not-applied day's 2 L are its replay's, and the withheld day claims nothing (review of batches k7 and k8: the tile counted every scored day,
    17 L, beside a peak-window figure of the claiming days alone). `litres_saved`, the ledger's figure of every scored day, is unchanged. A
    captive-gas genset's figure is the same number of MMBtu; a site without a genset has none; claiming days whose fuel was never known give no
    figure ("not metered" while `claiming_days` is above 0), never 0."""
    sid = make_site(name="K27 diesel")
    _put_rows(sid, _rows())
    nat = verify.summary(sid, "2026-11-01", "2026-11-04")["baselines"]["native"]
    assert (nat["fuel_saved"], nat["fuel_days"], nat["fuel_unit"]) == (15.0, 2, "L") and nat["litres_saved"] == 17.0
    tun = verify.summary(sid, "2026-11-01", "2026-11-04")["baselines"]["tuned"]
    assert (tun["fuel_saved"], tun["fuel_days"]) == (15.0, 2)
    gas = make_site(name="K27 gas", genset={"rated_kw": 200, "f_min": 0.3, "fuel_kind": "gas_captive", "min_up_h": 1.0})
    _put_rows(gas, _rows())
    nat = verify.summary(gas, "2026-11-01", "2026-11-04")["baselines"]["native"]
    assert (nat["fuel_saved"], nat["fuel_unit"]) == (15.0, "MMBtu")
    none = make_site(name="K27 no genset", genset=False)
    _put_rows(none, _rows())
    nat = verify.summary(none, "2026-11-01", "2026-11-04")["baselines"]["native"]
    assert (nat["fuel_saved"], nat["fuel_days"], nat["fuel_unit"]) == (None, 0, None)
    # a claiming day whose fuel is unknown (litres None) adds nothing; every claiming day unknown is no figure
    unknown = _day(800.0, {"peak_window": 800.0}, litres=None, genset_unknown=True)
    assert verify.fuel_saved([unknown, unknown], True) == (None, 0)
    assert verify.fuel_saved([unknown, _day(1.0, {"peak_window": 1.0}, litres=3.25)], True) == (3.2, 1)
    assert verify.fuel_saved([_day(1.0, {"peak_window": 1.0}, litres=3.25)], False) == (None, 0)


def test_each_day_names_the_plan_in_force_at_its_start(company):
    """The 2015 plan is in force when every day begins; one made at midnight of 2 Nov is in force from that day's start, one made at 10:00 on 2 Nov
    from 3 Nov's; a held run is no plan in force."""
    sid = make_site(name="K27 plans")
    _put_rows(sid, _rows())
    first = savings_export.plan_at_start(sid, "2026-11-01", "2026-11-01")["2026-11-01"]
    assert first is not None

    def plan(pid: str, belief: str, status: str = "superseded") -> None:
        with store.connect() as con:
            con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status) VALUES (?,?,?,?,?,?,?,?)",
                        (pid, sid, belief, belief, belief, "2100-01-01T00:00:00+00:00", "a", status))

    plan("p-midnight", "2026-11-01T19:00:00+00:00")                     # 00:00 on 2 Nov in Karachi
    plan("p-morning", "2026-11-02T05:00:00+00:00")                      # 10:00 on 2 Nov
    plan("p-held", "2026-11-03T05:00:00+00:00", status="held")
    s = verify.summary(sid, "2026-11-01", "2026-11-04")
    assert [d["plan_id"] for d in s["days"]] == [first, "p-midnight", "p-morning", "p-morning"]


def test_the_tuned_table_s_rows_are_answered_and_none_where_the_native_capture_is_the_rule(company):
    sid = make_site(name="K27 tuned")
    _put_rows(sid, _rows())
    tun = verify.summary(sid, "2026-11-01", "2026-11-04")["baselines"]["tuned"]
    assert tun["rule_kind"] == "tou_variant" and tun["rule_table"] == TUNED_TABLE
    rows = _rows()
    rows["2026-11-02"]["tuned"]["tuned_rule"] = {"kind": "native_capture", "table": None, "native": {"work_mode": "self_use"}, "cycle_soc_pct": None}
    _put_rows(sid, rows)
    tun = verify.summary(sid, "2026-11-01", "2026-11-04")["baselines"]["tuned"]
    assert tun["rule_kind"] == "native_capture" and tun["rule_table"] is None
    assert not any(money.is_money(k) for r in TUNED_TABLE for k in r)                          # settings, not money


def test_the_tariff_notices_the_period_reaches_are_answered_in_words_without_rates(company):
    """The shipped CY2027 schedule waits for its notification from 1 Jan 2027: a period that reaches it carries the notice, one of November does
    not, an off-grid site never."""
    sid = make_site(name="K27 notices")
    assert verify.summary(sid, "2026-11-01", "2026-11-04")["tariff_notices"] == []
    got = verify.summary(sid, "2026-12-30", "2027-01-02")["tariff_notices"]
    assert got and all(n["message"] and n["effective_from"] <= "2027-01-02" for n in got), got
    assert not any(money.is_money(k) for n in got for k in n) and "Rs" not in json.dumps(got)
    off = make_site(name="K27 notices off grid", conn={"regime": "off_grid"})
    assert verify.summary(off, "2026-12-30", "2027-01-02")["tariff_notices"] == []


def test_preliminary_is_answered_by_the_screen_s_rule_and_by_the_report_s(company, monkeypatch):
    """Q60 (plan F0.1 decision 15): the screen's rule is 28 verified days and the native capture; the report's also waits for 8 weeks of switchback
    and the FCA billed. `preliminary` stays the report's. With the verified days met, the screen's figure is final while the report's is not."""
    sid = make_site(name="K27 rules")
    _put_rows(sid, _rows())
    s = verify.summary(sid, "2026-11-01", "2026-11-04")
    assert s["preliminary_rules"] == {"screen": {"preliminary": True, "conditions": ["verified_days", "native_capture"]},
                                      "report": {"preliminary": True, "conditions": ["verified_days", "native_capture", "switchback_weeks",
                                                                                     "fca_billed"]}}
    monkeypatch.setattr(verify, "VERIFIED_DAYS_FOR_FINAL", 2)
    s = verify.summary(sid, "2026-11-01", "2026-11-04")
    assert s["final_conditions"]["verified_days"] == 2
    assert s["preliminary_rules"]["screen"]["preliminary"] is False and s["preliminary_rules"]["report"]["preliminary"] is True
    assert s["preliminary"] is True                                     # the one rule of before, unchanged
    monkeypatch.setattr(verify, "SWITCHBACK_WEEKS_FOR_FINAL", 0)
    monkeypatch.setattr(tariffs, "fca_billed", lambda month: True)
    s = verify.summary(sid, "2026-11-01", "2026-11-04")
    assert s["preliminary"] is False and s["preliminary_rules"]["report"]["preliminary"] is False


def test_a_viewer_reads_every_energy_addition_and_no_taxes(world):
    """The role rule of F11 item 1: a viewer's answer holds the new energy fields and no money key; the administrator's holds the taxes."""
    w = world
    with w.use():
        _put_rows(w.site, _rows())
    q = {"start": "2026-11-01", "end": "2026-11-04"}
    admin = w.c["admin"].get(f"/api/sites/{w.site}/savings", params=q).json()
    viewer = w.c["viewer"].get(f"/api/sites/{w.site}/savings", params=q).json()
    assert admin["baselines"]["native"]["taxes_recovered_rs"] == pytest.approx(310.70, abs=0.005)
    for base in ("native", "tuned"):
        assert "taxes_recovered_rs" not in viewer["baselines"][base]
        for k in ("peak_import_kwh_avoided", "peak_import_days", "fuel_saved", "fuel_days", "fuel_unit"):
            assert viewer["baselines"][base][k] == admin["baselines"][base][k], (base, k)
    assert viewer["baselines"]["tuned"]["rule_table"] == TUNED_TABLE
    assert [d["plan_id"] for d in viewer["days"]] == [d["plan_id"] for d in admin["days"]]
    assert [d["peak_import_kwh_native"] for d in viewer["days"]] == [400.0, 300.0, 200.0, 999.0]
    assert viewer["preliminary_rules"] == admin["preliminary_rules"] and viewer["tariff_notices"] == admin["tariff_notices"] == []


def test_the_monthly_statement_says_the_page_s_figures_by_the_same_functions(world, monkeypatch):
    """The statement's summary carries the peak-window import avoided and its ledger the taxes recovered, from the summary's own figures, and a
    tariff notice the month reaches is printed in its words."""
    from gridlytics import reports
    sid, month = world.site, _past_month()
    d1 = f"{month}-03"
    native = _baseline(peak_import_kwh_avoided=570.0, peak_import_days=3, taxes_recovered_rs=310.7, litres_saved=17.0, fuel_saved=15.0)
    tuned = _baseline(saving_rs=800.0, claimed_rs=780.0, perfect_foresight_rs=None, peak_import_kwh_avoided=120.0, peak_import_days=3,
                      taxes_recovered_rs=155.35, litres_saved=9.0, fuel_saved=7.5)
    notice = {"kind": "schedule", "key": "national", "version_id": "v", "effective_from": f"{month}-01", "name": "x", "in_use": None,
              "message": "The uniform schedule due from 1 Jan 2027 is awaiting its notification: prices from that day still use the one in force."}
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(d1)], native=native, tuned=tuned, tariff_notices=[notice]), [_tday(d1)])
    built = _story(monkeypatch)
    with world.use():
        reports.generate("monthly_savings", sid, month, "test")
    text = built[-1]
    assert "Peak-window import avoided | 570 kWh | 120 kWh" in text, text
    assert "Taxes you recover (not part of the saving) | Rs 311 | Rs 155" in text, text
    # the fuel saved is the page's tile, the claim's days (review of batches k7 and k8), not the ledger's every scored day
    assert "Diesel saved (Estimated) | 15.0 L | 7.5 L" in text and "17.0 L" not in text, text
    assert notice["message"] in text


def test_the_monthly_statement_says_no_fuel_figure_without_a_claiming_day_and_not_metered_with_one(world, monkeypatch):
    """The statement's fuel row and its key figure read the summary's `fuel_saved` as the page does: "-" with no claiming day (nothing claimed, so
    no fuel saved is said, never the replay's litres), "not metered" where days claim and the fuel was known on none of them."""
    from gridlytics import reports
    sid, month = world.site, _past_month()
    d1 = f"{month}-03"
    for native, want in ((_baseline(claiming_days=0, litres_saved=17.0, fuel_saved=None, fuel_days=0), "Diesel saved (Estimated) | - |"),
                         (_baseline(claiming_days=2, litres_saved=0.0, fuel_saved=None, fuel_days=0), "Diesel saved (Estimated) | not metered |")):
        _with_summary(monkeypatch, _fake_summary(month, [_day_row(d1)], native=native, tuned=native), [_tday(d1)])
        built = _story(monkeypatch)
        with world.use():
            reports.generate("monthly_savings", sid, month, "test")
        text = built[-1]
        assert want in text and "17.0 L" not in text, text
