"""Round-7 regression tests for the Savings page's package (savings-page, stage 2): the fee check's figure and what it rests on, and the claimed
wording of the reports, from the end-to-end audit.

Each test fails on the code before its fix. Test data: hand-made day rows and a stubbed summary only; nothing here calls the network."""
from __future__ import annotations

import json

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from test_fix_api import world  # noqa: F401 (a fixture)
from test_fix_r7_alerts_reports import _baseline, _day_row, _fake_summary, _past_month, _story, _tday, _with_summary
from test_fix_savings import _days, _put_rows, _row, company, make_site  # noqa: F401 (company is a fixture)

from gridlytics import sites, store, tariffs, telemetry, verify

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


# ------------------------------------------------------------------------------------------------ pages-D-05: the switchback's carry-over

class _Doc:
    """A stand-in for the report's document: the tables and paragraphs a section writes."""

    def __init__(self):
        self.tables, self.paras = [], []

    def table(self, rows, widths=None, right=True):
        self.tables.append(rows)

    def p(self, text, lead=""):
        self.paras.append(text)

    def gap(self, n=3):
        pass


def test_the_reports_switchback_names_each_switch_both_ways_and_the_figures_before_the_carry_over(monkeypatch):
    """pages-D-05: both switchback figures add the carry-over credit of every switch, a native-to-plan switch included (engine-savings-11), but the
    report's table headed every row 'Switch to native on' and its sentence gave the figures without what they were before the credit. Each row now
    says which way the switch went, a plan arm's stored energy that is the plan days' mean says so, and the sentence gives both figures before the
    credit beside the credit."""
    from gridlytics import reports
    credits = [{"boundary": "2026-11-16", "from_arm": "plan", "to_arm": "native", "e_end_plan_kwh": 120.0, "e_end_native_kwh": 60.0,
                "e_end_plan_basis": "plan_block_last_day", "v_end": 30.0, "credit_rs": 1800.0, "credit_v_end_rs": 1800.0, "realised": False,
                "provisional": True},
               {"boundary": "2026-11-23", "from_arm": "native", "to_arm": "plan", "e_end_plan_kwh": 90.0, "e_end_native_kwh": 100.0,
                "e_end_plan_basis": "plan_days_mean", "v_end": 40.0, "credit_rs": -400.0, "credit_v_end_rs": -400.0, "realised": False,
                "provisional": False}]
    blocks = [{"block_start": "2026-11-09", "block_end": "2026-11-15", "arm": "plan"}, {"block_start": "2026-11-16", "block_end": "2026-11-22", "arm": "native"},
              {"block_start": "2026-11-23", "block_end": "2026-11-29", "arm": "plan"}]
    res = {"plan_days": 14, "native_days": 7, "saving_rs_per_day_adjusted": 1200.0, "saving_rs_per_day_unadjusted": 1300.0,
           "carry_over_credit_rs": 1400.0, "carry_over_rs_per_plan_day": 100.0, "saving_rs_per_day_adjusted_before_carry_over": 1100.0,
           "saving_rs_per_day_unadjusted_before_carry_over": 1200.0}
    monkeypatch.setattr(verify, "switchback_result", lambda site_id: {"blocks": blocks, "carry_over_credits": credits, "result": res})
    doc = _Doc()
    reports._switchback(doc, "s", date(2026, 11, 1), date(2026, 11, 30), whole=True)
    head, *rows = doc.tables[1]
    assert head[:2] == ["Switch on", "From and to"]                              # before: "Switch to native on" for both
    assert rows[0][1] == "The plan to the native settings" and rows[1][1] == "The native settings to the plan"
    assert rows[1][2] == "90.0 kWh (mean of the plan days)" and rows[1][-1] == "-Rs 400"
    text = " ".join(doc.paras)
    assert "Rs 1,100 a day adjusted and Rs 1,200 unadjusted before the carry-over credit" in text, text
    assert "Carry-over credit: Rs 1,400 in all, Rs 100 a plan day, included in both." in text, text


# ------------------------------------------------------------------------------------------------ brief-vs-build-13 and pages-D-12: the KPIs and the gates

def _arm(hours=0.0, starts=0, discharge=0.0, unserved=0.0, cost=0.0) -> dict:
    return {"genset_hours": hours, "genset_starts": starts, "discharge_kwh": discharge, "unserved_kwh": unserved, "cost_rs": cost, "e_end_kwh": 0.0,
            "v_end": 0.0}


def _gate_rows(sid: str, d0: date, n: int, actual: dict, native: dict, tuned: dict, saving=1000.0, fidelity="confirmed") -> None:
    rows = {}
    for d in _days(d0, d0 + timedelta(days=n - 1)):
        nat = {**_row(saving, saving, fidelity=fidelity), "actual": {**_arm(**actual)}, "baseline": {**_arm(**native)}}
        tun = {**_row(saving / 2, saving / 2, fidelity=fidelity), "actual": {**_arm(**actual)}, "baseline": {**_arm(**tuned)}}
        rows[d.isoformat()] = {"native": nat, "tuned": tun}
    _put_rows(sid, rows)


def _ack(sid: str, at: datetime, body: dict) -> None:
    with store.connect() as con:
        con.execute("INSERT INTO acks (id, site_id, plan_id, status, body, received_at) VALUES (?,?,?,?,?,?)",
                    (store.new_id(), sid, "p1", "applied", json.dumps(body), at.isoformat(timespec="seconds")))


def test_the_kpis_give_reserve_adequacy_the_constraint_violations_control_health_and_each_arms_genset_and_cycles(company, monkeypatch):
    """brief-vs-build-13: the brief's KPI table has reserve adequacy over a period, the constraint violations (genset under its minimum load, export
    above the cap, import during an outage), control health (writes per register per day, read-back failures, fallback events) and the genset's run
    hours and starts against the baselines; none of them was computed anywhere. gonogo.measures gives them for a period."""
    from gridlytics import gonogo, telemetry, twin
    sid = make_site(name="KPI mill")                                    # battery 215 kWh, genset 200 kW at f_min 0.3, a grid site without export
    day = date(2026, 11, 2)
    t0 = twin.day_start_utc(day)
    cols = ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "grid_export_wh", "genset_running", "genset_power_w")
    rows = []
    for i in range(120):
        t = t0 + 10 * 3600 + i * 60
        down = i < 30                                                    # 30 minutes of outage, 10 of them importing 5 kW (a meter's back-feed)
        imp = 5.0 if down and i < 10 else (0.0 if down else 50.0)
        exp = 3.0 if 60 <= i < 80 else 0.0                               # 20 minutes exporting 3 kW where no export is allowed
        gen_kw = 30.0 if down else 0.0                                   # the genset carries the outage at 30 kW, under its 60 kW minimum
        rows.append((sid, t, "csv", 0.0 if down else 1.0, imp * 1000 / 60, exp * 1000 / 60, 1.0 if down else 0.0, gen_kw * 1000))
    telemetry.write_rows(rows, cols)
    at = datetime.fromtimestamp(t0 + 11 * 3600, timezone.utc)
    _ack(sid, at, {"items": [{"device": "inv", "primitive": "tou_slots", "written": 1, "read_back": 1, "status": "ok"},
                             {"device": "inv", "primitive": "tou_slots", "written": 2, "read_back": 3, "status": "ok"},
                             {"device": "inv", "primitive": "max_charge_a", "written": 50, "read_back": None, "status": "failed", "codes": ["OFF_WHITELIST"]}]})
    _ack(sid, at + timedelta(days=1), {"per_device": [{"device_id": "inv", "status": "applied", "applied": [{"primitive": "tou_slots", "readback_ok": False}],
                                                       "rejected": [{"primitive": "grid_charge", "reason": "OFF_WHITELIST"}]}]})
    _ack(sid, at + timedelta(hours=1), {"status": "rejected", "reason": "OFF_WHITELIST", "items": []})   # a whole plan refused off the whitelist
    with store.connect() as con:
        con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)", (sid, at.isoformat(), "fallback_engaged", "edge", "{}"))
    from gridlytics import battery
    seen = []
    monkeypatch.setattr(battery, "state", lambda site, now=None: seen.append(now) or {"trust": 0.82, "trust_basis": "independent", "c_cyc": 9.0})
    _gate_rows(sid, day, 2, actual={"hours": 2.0, "starts": 1, "discharge": 150.0, "unserved": 1.5}, native={"hours": 3.0, "starts": 2, "unserved": 1.0},
               tuned={"hours": 4.0, "starts": 3, "discharge": 100.0})
    m = gonogo.measures(sid, day, day + timedelta(days=1), now=t0 + 3 * 86400)
    v = m["violations"]
    assert v["minutes_checked"] is True and v["minutes_read"] == 120
    assert v["import_during_outage_min"] == 10 and v["export_above_cap_min"] == 20 and v["export_cap_kw"] == 0.0
    assert v["genset_below_min_load_min"] == 30 and v["genset_min_load_kw"] == 60.0
    assert v["unserved_caused_by_plan_kwh"] == 1.0                     # 1.5 - 1.0 on each of two claiming days
    c = m["control"]
    assert c["writes"] == 3 and c["writes_per_register_per_day"] == {"tou_slots": 1.5} and c["writes_per_register_per_day_max"] == 2
    assert c["readback_failures"] == 2 and c["write_failures"] == 1 and c["off_whitelist"] == 3 and c["fallback_events"] == 1
    assert c["fidelity_pct"] == 100.0 and c["plan_days"] == 2
    assert m["genset"]["hours"] == {"actual": 4.0, "native": 6.0, "tuned": 8.0} and m["genset"]["starts"] == {"actual": 2.0, "native": 4.0, "tuned": 6.0}
    e_nom = twin.plant_of(sites.full(sid)).e_nom
    assert m["battery"]["efc_per_day"]["actual"] == pytest.approx(150.0 / e_nom, abs=1e-3)
    assert m["battery"]["efc_per_day"]["tuned"] == pytest.approx(100.0 / e_nom, abs=1e-3)
    # the brief's BMS trust (the gap between the BMS and the independent estimate) over the 30 days to the period's end, no money with it
    assert m["battery"]["bms_trust"] == {"trust": 0.82, "basis": "independent", "to": "2026-11-03"} and seen == [t0 + 2 * 86400]


def test_the_minute_checks_read_a_coarse_sources_energy_as_power_over_its_own_interval(company):
    """Review of brief-vs-build-13: the minute checks turned a row's import and export energy into power as if every row were one minute (Wh x 60 /
    1000), while a coarse source's rows (an OEM cloud's 5 minutes) hold the energy of their whole interval. A 5-minute row importing 0.3 kW in an
    outage (25 Wh, under the 0.5 kW margin) read as 1.5 kW, and 15 minutes of it counted as imported in an outage."""
    from gridlytics import gonogo, telemetry, twin
    sid = make_site(name="Coarse mill")
    day = date(2026, 11, 2)
    t0 = twin.day_start_utc(day)
    cols = ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "grid_export_wh")
    # an hour of 5-minute rows: the first three in an outage, importing 0.3 kW (a meter's creep), then 2 kW of import over 5 minutes each
    rows = [(sid, t0 + 10 * 3600 + i * 300, "cloud", 0.0 if i < 3 else 1.0, (0.3 if i < 3 else 2.0) * 1000 * 5 / 60, 0.0) for i in range(12)]
    telemetry.write_rows(rows, cols)
    v = gonogo.measures(sid, day, day, now=t0 + 2 * 86400)["violations"]
    assert v["minutes_read"] == 60
    assert v["import_during_outage_min"] == 0                         # before: 15 (25 Wh a row read as 1.5 kW)
    # the same outage importing 1 kW is counted, 15 minutes of it
    telemetry.write_rows([(sid, t0 + 10 * 3600 + i * 300, "cloud", 0.0, 1.0 * 1000 * 5 / 60, 0.0) for i in range(3)], cols)
    assert gonogo.measures(sid, day, day, now=t0 + 2 * 86400)["violations"]["import_during_outage_min"] == 15


def test_the_ledger_counts_each_arms_genset_starts():
    """brief-vs-build-13: the ledger stored the genset's litres and hours per arm but not its starts (plan 8.3's diesel KPI: 'run hours, starts per
    day'). A run going on at midnight is not a start, as the genset's cost counts it."""
    assert verify._gen_starts({"gen_on": np.array([1, 1, 0, 1, 1, 0, 0, 1], dtype=bool)}, on_before=False) == 3
    assert verify._gen_starts({"gen_on": np.array([1, 1, 0, 1, 1, 0, 0, 1], dtype=bool)}, on_before=True) == 2


def test_the_pilot_scorecard_reads_every_criterion_against_the_results_with_the_genset_and_cycling_gates(company, monkeypatch):
    """pages-D-12 (and brief-vs-build-02, flow-savings-pilot-bill-13): the pilot's criteria were stored and listed but never read against a result,
    and plan 15.3's genset run-hours and cycling gates did not exist. The scorecard reads each criterion from the pilot's start: met, not met, not
    measured yet or not applying here."""
    from gridlytics import gonogo
    from gridlytics.routes_savings import PILOT_DEFAULT
    assert PILOT_DEFAULT["criteria"]["genset_hours_cut_pct"] == 15.0 and PILOT_DEFAULT["criteria"]["cycles_ratio_max"] == 1.1
    sid = make_site(name="Gate mill")
    start = date(2026, 10, 1)
    with store.connect() as con:
        con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?)",
                    (sid, json.dumps({**PILOT_DEFAULT, "criteria": {**PILOT_DEFAULT["criteria"], "min_saving_pct": 10.0}}),
                     datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc).isoformat(), store._now()))   # 1 Oct 01:00 in Karachi
    # 20 claiming days: Rs 1,000 claimed on Rs 5,000 of native cost (20 %); the genset ran 3 h against the tuned rule's 4 h (25 % less); the
    # battery cycled 1.2 times the tuned baseline's
    rows = {}
    for d in _days(start, start + timedelta(days=19)):
        nat = {**_row(1000.0, 1000.0, cost=4000.0), "actual": _arm(3.0, 1, 120.0, cost=4000.0), "baseline": _arm(5.0, 2, 90.0, cost=5000.0)}
        tun = {**_row(600.0, 600.0, cost=4000.0), "actual": _arm(3.0, 1, 120.0, cost=4000.0), "baseline": _arm(4.0, 2, 100.0, cost=4600.0)}
        rows[d.isoformat()] = {"native": nat, "tuned": tun}
    _put_rows(sid, rows)
    store.set_setting(f"fee:{sid}", 5000.0)
    card = gonogo.scorecard(sid, start + timedelta(days=19))
    gates = {g["id"]: g for g in card["gates"]}
    assert card["started"] is True and card["from"] == "2026-10-01"
    assert gates["saving"]["status"] == "met" and gates["saving"]["result"] == 20.0 and gates["saving"]["target"] == 10.0
    assert gates["unserved"]["status"] == "met" and gates["unserved"]["result"] == 0.0
    assert gates["fidelity"]["status"] == "met" and gates["fidelity"]["result"] == 100.0
    assert gates["genset_hours"]["status"] == "met" and gates["genset_hours"]["result"] == 25.0
    assert gates["cycles"]["status"] == "not_met" and gates["cycles"]["result"] == 1.2 and gates["cycles"]["target"] == 1.1
    assert gates["reserve"]["status"] == "not_yet"                       # no outage judged yet
    assert gates["agreement"]["status"] == "not_yet"                     # no switchback result
    assert gates["forecast_pv"]["status"] == "not_yet"
    assert gates["whitelist"]["status"] == "met"                         # a closed-loop site with no write refused
    assert gates["fee_multiple"]["status"] in ("met", "not_met") and gates["fee_multiple"]["fee_gate"] is True
    # plan 15.3's statistic: 20 verified days of Rs 600 over the tuned table is Rs 18,000 a month, 3.6 times the fee; but no switchback weeks yet
    assert gates["statistic"]["result"] == 3.6 and gates["statistic"]["status"] == "met"
    assert card["verdict"] == "not_yet" and "under_8_weeks" in card["verdict_reasons"] and card["so_far"] == "success"
    monkeypatch.setattr(verify, "SWITCHBACK_WEEKS_FOR_FINAL", 0)
    card = gonogo.scorecard(sid, start + timedelta(days=19))
    assert card["verdict"] == "review" and card["failing"] == ["cycles"]   # the statistic clears the bar, but the cycling gate is not met
    # the pilot write-up prints the same gates and the verdict (plan 8.5: "with the pre-registered success and kill criteria of Part 15.3 filled in")
    from gridlytics import reports
    built = _story(monkeypatch)
    reports.generate("pilot", sid, "2026-10", "test")
    lines = built[-1].splitlines()
    row = {ln.split(" | ")[0]: ln for ln in lines if " | " in ln}
    assert row["Genset run hours cut at least (against the tuned rule, on the days that claim)"].endswith("| meets it")
    assert "25.0 %" in row["Genset run hours cut at least (against the tuned rule, on the days that claim)"]
    assert row["Battery cycles a day at most (times the tuned baseline's)"].endswith("1.20 times | falls short")
    assert any(ln.startswith("Verdict: review.") for ln in lines), lines
    # a site without a genset or a battery: those gates do not apply (R8 2.13: "the genset gate hidden without a genset")
    bare = make_site(name="Gate bare", battery=False, genset=False)
    with store.connect() as con:
        con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?)", (bare, json.dumps(PILOT_DEFAULT), store._now(), store._now()))
    gates = {g["id"]: g for g in gonogo.scorecard(bare, datetime.now(PKT).date())["gates"]}
    assert gates["genset_hours"]["status"] == gates["cycles"]["status"] == gates["reserve"]["status"] == "not_applicable"


def test_the_scorecard_route_hides_money_gates_from_a_viewer_and_fee_gates_from_an_operator(world):
    """pages-D-12 with the owner's rules: a viewer never reads a money figure or a money ratio, and an operator reads costs but not the fee."""
    sid = world.site
    with world.use():
        from gridlytics.routes_savings import PILOT_DEFAULT
        with store.connect() as con:
            con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?)", (sid, json.dumps(PILOT_DEFAULT), store._now(), store._now()))
        store.set_setting(f"fee:{sid}", 5000.0)
    admin = world.c["admin"].get(f"/api/sites/{sid}/pilot/scorecard").json()
    ids = [g["id"] for g in admin["gates"]]
    assert {"saving", "unserved", "reserve", "fidelity", "fee_multiple", "forecast_pv", "genset_hours", "cycles", "agreement", "whitelist",
            "statistic"} == set(ids)
    assert admin["verdict"] == "not_yet" and "fee_multiple" in admin["criteria"]
    op = world.c["operator"].get(f"/api/sites/{sid}/pilot/scorecard").json()
    assert "fee_multiple" not in [g["id"] for g in op["gates"]] and "statistic" not in [g["id"] for g in op["gates"]]
    assert op["verdict"] is None and op["fee_hidden"] is True and "fee_multiple" not in op["criteria"]
    viewer = world.c["viewer"].get(f"/api/sites/{sid}/pilot/scorecard").json()
    saving = next(g for g in viewer["gates"] if g["id"] == "saving")
    assert saving == {"id": "saving", "money_gate": True, "hidden": True}
    kpis = world.c["viewer"].get(f"/api/sites/{sid}/savings/kpis").json()
    assert {"reserve", "violations", "control", "genset", "battery"} <= set(kpis)


# ------------------------------------------------------------------------------------------------ pages-D-06: the exports carry the claim

def _export_rows(sid: str, version: str) -> list[str]:
    """Five days of November 2025 (a closed month) at a site: three that claim (the last with a carry-over no later day has used yet), one withheld for
    an unknown import (its grid levers unknown) and a native block that claims nothing; each priced at `version`, native capture version 3, and an
    MDI lever (the native settings' peak 350 kW against the plan's 300, at Rs 1,000 a kW)."""
    days = ["2025-11-03", "2025-11-04", "2025-11-05", "2025-11-06", "2025-11-07"]
    rows = {}
    for i, day in enumerate(days):
        nat = {**_row(1000.0 + i, 1000.0 + i, peak_a=300.0, peak_b=350.0, rate=1000.0), "tariff_version": version, "native_capture_version": 3,
               "levers": {"peak_window": 700.0 + i, "offpeak_energy": 200.0, "export": 0.0, "genset": 100.0, "wear": 0.0, "carry_over": 0.0}}
        if day == "2025-11-05":
            nat.update(fidelity="confirmed", claimed_rs=0.0, claim_withheld="import_unknown", import_unknown=True,
                       levers={"peak_window": None, "offpeak_energy": None, "export": None, "genset": 100.0, "wear": 0.0, "carry_over": 0.0})
        if day == "2025-11-06":
            nat.update(fidelity="native_block", claimed_rs=0.0)
        if day == "2025-11-07":
            nat["levers"] = {**nat["levers"], "carry_over": 150.0}      # nothing scored after it: its carry-over is provisional
        rows[day] = {"native": nat, "tuned": {**_row(500.0, 500.0), "tariff_version": version, "native_capture_version": 3}}
    _put_rows(sid, rows)
    return days


def _plan(sid: str, belief: str, status: str = "superseded") -> str:
    pid = store.new_id()
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status) VALUES (?,?,?,?,?,?,?,?)",
                    (pid, sid, belief, belief, belief, "2100-01-01T00:00:00+00:00", "a", status))
    return pid


def _tariff_version(effective_from: str = "2025-07-01") -> str:
    vid = store.new_id()
    with store.connect() as con:
        con.execute("INSERT INTO tariff_versions (id, kind, key, effective_from, known_from, status, body, source, hash, created) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?)", (vid, "schedule", "national", effective_from, effective_from, "active", "{}", "test", vid, store._now()))
    return vid


def test_the_savings_csv_carries_the_claim_the_levers_the_mdi_and_the_tariff_capture_and_plan_ids_and_adds_up_to_the_page(world):
    """pages-D-06 (with engine-savings-07, flow-savings-pilot-bill-16 and engine-money-live-13): the savings CSV gave each day's saving, costs, litres
    and peak only, so its rows summed to Rs 1,956 while the page claimed Rs 43,594 (the MDI lever missing), a withheld or native-block day's saving
    read like any other, and no tariff version, capture version or plan id let a dispute be re-run (plan 8.5). Each day now says whether it claims,
    its part of the page's claim (the column adds up to it), why it claims nothing, the levers a withheld day keeps, each lever, its share of the
    month's MDI lever and whether that month is still open, the native settings' peak, its tariff version and the day that version took effect, its
    native capture version and the plans in force over its Pakistan day."""
    import csv
    import io
    sid = world.site
    with world.use():
        version = _tariff_version()
        days = _export_rows(sid, version)
        before = _plan(sid, "2025-11-02T18:00:00+00:00")       # 23:00 PKT on 2 Nov: in force when 3 Nov begins
        early = _plan(sid, "2025-11-02T19:30:00+00:00")        # 00:30 PKT on 3 Nov (a UTC date of 2 Nov)
        late = _plan(sid, "2025-11-03T20:00:00+00:00")         # 01:00 PKT on 4 Nov (a UTC date of 3 Nov)
        _plan(sid, "2025-11-03T10:00:00+00:00", status="failed")
    page = world.c["analyst"].get(f"/api/sites/{sid}/savings", params={"start": days[0], "end": days[-1]}).json()
    r = world.c["analyst"].get(f"/api/sites/{sid}/savings/export.csv", params={"start": days[0], "end": days[-1]})
    assert r.status_code == 200, r.text
    rows = list(csv.DictReader(io.StringIO(r.text)))
    head = r.text.splitlines()[0].split(",")
    # the columns of before keep their place; the claim, the levers, the MDI and the versions follow
    assert head[:10] == ["day", "saving_native_rs", "saving_tuned_rs", "fidelity", "actual_cost_rs", "native_cost_rs", "litres_actual",
                         "litres_native", "peak_kw", "peak_import_kwh"], head
    for col in ("claims", "claimed_native_rs", "not_claimed_reason", "claim_withheld", "levers_kept", "carry_over_provisional_native_rs",
                "mdi_month", "mdi_share_native_rs", "mdi_provisional", "peak_window_native_rs", "offpeak_energy_native_rs", "export_native_rs",
                "genset_native_rs", "wear_native_rs", "carry_over_native_rs", "peak_kw_native", "tariff_version", "tariff_version_from",
                "native_capture_version", "plan_ids"):
        assert col in head, col
    by = {x["day"]: x for x in rows}
    assert [x["day"] for x in rows] == days
    # the claim column adds up to the page's claim, the MDI shares included and the provisional carry-over left out
    nat = page["baselines"]["native"]
    assert nat["levers"]["mdi"] > 0
    assert sum(float(x["claimed_native_rs"] or 0) for x in rows) == pytest.approx(nat["claimed_rs"], abs=0.05)
    # and the saving column with the MDI shares of the days whose saving is known adds up to the page's saving of every day
    known = [x for x in rows if x["saving_native_rs"] != ""]
    assert sum(float(x["saving_native_rs"]) + float(x["mdi_share_native_rs"]) for x in known) == pytest.approx(nat["saving_rs"], abs=0.05)
    assert by["2025-11-03"]["claims"] == "true" and by["2025-11-03"]["not_claimed_reason"] == ""
    withheld = by["2025-11-05"]
    assert withheld["claims"] == "false" and withheld["claim_withheld"] == "import_unknown" and withheld["not_claimed_reason"] == "import_unknown"
    assert withheld["saving_native_rs"] == "" and withheld["claimed_native_rs"] == "0.0"
    assert withheld["levers_kept"] == "genset;wear;carry_over" and withheld["peak_window_native_rs"] == ""
    block = by["2025-11-06"]
    assert block["claims"] == "false" and block["not_claimed_reason"] == "native_block" and block["claim_withheld"] == ""
    assert by["2025-11-07"]["carry_over_provisional_native_rs"] == "150.0"
    assert by["2025-11-03"]["mdi_month"] == "2025-11" and by["2025-11-03"]["mdi_provisional"] == "false"
    assert by["2025-11-03"]["peak_kw_native"] == "350.0" and by["2025-11-03"]["peak_window_native_rs"] == "700.0"
    assert {x["tariff_version"] for x in rows} == {version} and {x["tariff_version_from"] for x in rows} == {"2025-07-01"}
    assert {x["native_capture_version"] for x in rows} == {"3"}
    # the plans in force over each Pakistan day: the one in force at its midnight and those made during it (never a failed one), by Karachi day
    assert by["2025-11-03"]["plan_ids"].split(";") == [before, early]
    assert by["2025-11-04"]["plan_ids"].split(";") == [early, late]
    assert by["2025-11-05"]["plan_ids"].split(";") == [late]


def test_the_monthly_report_csv_carries_the_claim_columns_and_its_plan_ids_by_karachi_day(world, monkeypatch):
    """pages-D-06 (with flow-reports-alerts-members-keys-04 and the rest of engine-savings-06): the report's CSV had no claim, no tariff or capture
    version, while the PDF said the tariff versions were 'dated in the CSV annex', and its plan_ids were the first four plans whose UTC date was the
    day. Its rows now carry the savings CSV's claim columns, the plans in force over each Pakistan day, and the PDF names the columns it means."""
    import csv
    import io
    from gridlytics import reports
    sid = world.site
    with world.use():
        version = _tariff_version("2025-07-01")
        days = _export_rows(sid, version)
        before = _plan(sid, "2025-11-02T18:00:00+00:00")
        early = _plan(sid, "2025-11-02T19:30:00+00:00")
        mids = [_plan(sid, f"2025-11-03T{h:02d}:00:00+00:00") for h in range(0, 19, 3)]   # seven plans made on 3 Nov
    monkeypatch.setattr(telemetry, "days", lambda site_id, d0, d1: [_tday(d) for d in days])
    built = _story(monkeypatch)
    with world.use():
        rep = reports.generate("monthly_savings", sid, "2025-11", "test")
        text = reports.file_path(rep["id"], "csv").read_text(encoding="utf-8")
        summ = verify.summary(sid, "2025-11-01", "2025-11-30")
    rows = {x["day"]: x for x in csv.DictReader(io.StringIO(text))}
    assert rows["2025-11-03"]["plan_ids"].split(";") == [before, early, *mids]       # before: the first four by UTC date
    assert rows["2025-11-05"]["claim_withheld"] == "import_unknown" and rows["2025-11-05"]["levers_kept"] == "genset;wear;carry_over"
    assert rows["2025-11-06"]["not_claimed_reason"] == "native_block"
    assert rows["2025-11-03"]["tariff_version"] == version and rows["2025-11-03"]["tariff_version_from"] == "2025-07-01"
    assert rows["2025-11-03"]["native_capture_version"] == "3" and rows["2025-11-03"]["mdi_month"] == "2025-11"
    assert sum(float(x["claimed_native_rs"] or 0) for x in rows.values()) == pytest.approx(summ["baselines"]["native"]["claimed_rs"], abs=0.05)
    pdf = built[-1]
    assert "as dated in the CSV annex" not in pdf
    assert "tariff_version" in pdf and "native_capture_version" in pdf and "plan_ids" in pdf, pdf


# ------------------------------------------------------------------------------------------------ pr-review-05 (tests-and-perf-11): recompute as a job

def test_the_recompute_runs_as_a_job_one_at_a_time_per_site_and_says_how_far_it_is(world, monkeypatch):
    """pr-review-05 with tests-and-perf-11: POST /savings/recompute scored up to 62 days, each with a bound solve, and as many later days, inside the
    request, so a month held it open for minutes and a second click started another. It now runs as a job: the request answers 202 with the job
    once RECOMPUTE_WAIT_S has passed, the job says how many days of the range it has scored, a second recompute of the site while one runs is
    refused in a sentence (the same person's same period answers that job), and the finished job answers what it did, the later days included."""
    import threading
    from gridlytics import routes_savings
    go, seen = threading.Event(), {}

    def detail(site_id, d0, d1, by="system", progress=None):
        seen["by"] = by
        progress(1, 2)
        assert go.wait(30)
        progress(2, 2)
        return {"days": 2, "rescored_days": ["2026-10-14"], "restated_days": []}

    monkeypatch.setattr(verify, "compute_range_detail", detail)
    monkeypatch.setattr(routes_savings, "RECOMPUTE_WAIT_S", 0.3)
    url = f"/api/sites/{world.site}/savings/recompute"
    body = {"start": "2026-10-12", "end": "2026-10-13"}
    try:
        r = world.c["analyst"].post(url, json=body)
        assert r.status_code == 202, r.text                              # before: 200 once every day was scored, the request held open
        job = r.json()
        assert job["status"] in ("queued", "running") and job["job_id"] and (job["start"], job["end"]) == ("2026-10-12", "2026-10-13")
        poll = world.c["analyst"].get(f"{url}/{job['job_id']}").json()
        assert poll["status"] == "running" and (poll["done"], poll["total"]) == (1, 2), poll
        # the same person's same period answers the job already running; anyone else's recompute of the site waits for it to end
        again = world.c["analyst"].post(url, json=body)
        assert again.status_code == 202 and again.json()["job_id"] == job["job_id"]
        other = world.c["admin"].post(url, json={"start": "2026-10-01", "end": "2026-10-05"})
        assert other.status_code == 409 and other.json()["detail"] == ("A recompute of this site from 12 Oct 2026 to 13 Oct 2026 is running; try "
                                                                         "again when it ends"), other.text
        # a job is its person's: another's read of it is a 404
        assert world.c["admin"].get(f"{url}/{job['job_id']}").status_code == 404
    finally:
        go.set()
    from gridlytics import jobs
    for _ in range(300):
        done = world.c["analyst"].get(f"{url}/{job['job_id']}").json()
        if done["status"] not in jobs.ACTIVE:
            break
        threading.Event().wait(0.05)
    assert done == {"status": "done", "days": 2, "rescored_days": ["2026-10-14"], "restated_days": [], "job_id": job["job_id"],
                    "start": "2026-10-12", "end": "2026-10-13"}, done
    assert seen["by"] and seen["by"] != "system"
    # once it has ended, the site recomputes again, here within the wait: the answer is the outcome at once
    r = world.c["admin"].post(url, json={"start": "2026-10-01", "end": "2026-10-05"})
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["days"] == 2, r.text


def test_the_recompute_runs_below_the_ticks_priority(world, monkeypatch):
    """tests-and-perf-11: the recompute's replays and solves burnt the CPU the scheduler's wall-clock-limited ticks need (HANDOFF 8.A.3). Its work
    runs in a thread of its own at a higher nice value (Linux threads each have one), and the request's thread keeps its own."""
    import os
    import threading
    from gridlytics import routes_savings
    if not hasattr(os, "setpriority") or not hasattr(threading, "get_native_id"):
        pytest.skip("no per-thread priority on this system")
    base = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
    seen = {}

    def detail(site_id, d0, d1, by="system", progress=None):
        seen["nice"] = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())
        seen["thread"] = threading.current_thread().name
        return {"days": 0, "rescored_days": [], "restated_days": []}

    monkeypatch.setattr(verify, "compute_range_detail", detail)
    monkeypatch.setattr(routes_savings, "RECOMPUTE_WAIT_S", 30.0)
    r = world.c["analyst"].post(f"/api/sites/{world.site}/savings/recompute", json={"start": "2026-10-12", "end": "2026-10-12"})
    assert r.status_code == 200 and r.json()["status"] == "done", r.text
    assert seen["thread"] == "gridlytics-recompute"
    assert seen["nice"] == max(base, routes_savings.RECOMPUTE_NICE)
    assert os.getpriority(os.PRIO_PROCESS, threading.get_native_id()) == base


# ------------------------------------------------------------------------------------------------ pr-review-07: who may act, from the meta

def test_the_meta_says_in_words_which_roles_hold_each_permission(world):
    """pr-review-07: the Savings page wrote "An administrator, operator or analyst can recompute the period" into its own text, a list of roles that
    goes stale the day the permissions change. GET /api/meta now says, for every permission, which roles hold it in the words of the 403 refusals
    (auth.roles_words), so the page reads them from there."""
    from gridlytics import auth
    for role in ("admin", "viewer"):
        meta = world.c[role].get("/api/meta").json()
        assert meta["who_can"]["run_jobs"] == "an administrator, an operator or an analyst", meta.get("who_can")
        assert meta["who_can"]["export"] == "an administrator or an analyst"
        assert set(meta["who_can"]) == set(auth.PERMISSIONS)


# ------------------------------------------------------------------------------------------------ pr-review-02 (rest): a report made before a restatement

def _report_row(sid: str, kind: str, period: str, made: str) -> str:
    rid = store.new_id()
    with store.connect() as con:
        con.execute("INSERT INTO reports (id, site_id, kind, period, generated_at, inputs_hash, pdf_path, csv_path, status, preliminary, generated_by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (rid, sid, kind, period, made, "h", f"reports/x/{rid}.pdf", f"reports/x/{rid}.csv", "done", 0, "test"))
    return rid


def test_a_stored_report_is_marked_out_of_date_once_a_day_of_its_month_is_restated_after_it(world):
    """pr-review-02 (the reports' half): a recompute that changes a claim in a closed month restates it (verify keeps `restated[].at` on the day's rows,
    and the Savings page lists the days), but the reports list served a monthly savings report made before as it was, its figures the old ones. A
    report whose figures are the savings days' (the monthly savings report, the company roll-up, the pilot report) is now listed out of date, with
    the days restated after it was made; one made after the restatement, and an energy report, are not."""
    from gridlytics import reports
    sid = world.site
    with world.use():
        restated = [{"at": "2026-11-10T09:00:00+00:00", "why": "recompute", "by": "analyst", "claimed_rs_before": 900.0, "claimed_rs_after": 700.0}]
        _put_rows(sid, {"2026-10-14": {"native": {**_row(700.0, 700.0), "restated": restated}, "tuned": _row(500.0, 500.0)},
                        "2026-10-15": {"native": _row(800.0, 800.0), "tuned": _row(500.0, 500.0)}})
        before = _report_row(sid, "monthly_savings", "2026-10", "2026-11-05T12:00:00+00:00")
        after = _report_row(sid, "monthly_savings", "2026-10", "2026-11-11T12:00:00+00:00")
        energy = _report_row(sid, "energy_only", "2026-10", "2026-11-05T12:00:00+00:00")
        rollup = _report_row("", "company_rollup", "2026-10", "2026-11-05T12:00:00+00:00")
        other = _report_row(sid, "monthly_savings", "2026-09", "2026-11-05T12:00:00+00:00")
        listed = {r["id"]: r for r in reports.listing()}
        assert reports.get(before)["out_of_date"] is True
    assert (listed[before]["out_of_date"], listed[before]["restated_days"]) == (True, ["2026-10-14"])     # before: listed as it was made
    assert (listed[rollup]["out_of_date"], listed[rollup]["restated_days"]) == (True, ["2026-10-14"])
    for rid in (after, energy, other):
        assert (listed[rid]["out_of_date"], listed[rid]["restated_days"]) == (False, []), listed[rid]
    # the route lists it so to a role that reads the kind
    rows = {r["id"]: r for r in world.c["analyst"].get("/api/reports").json()}
    assert rows[before]["out_of_date"] is True and rows[before]["restated_days"] == ["2026-10-14"]
    # and its download is saved under a name that says so
    with world.use():
        made = reports.generate("monthly_savings", sid, "2026-10", "test")
        with store.connect() as con:
            con.execute("UPDATE reports SET generated_at = ? WHERE id = ?", ("2026-11-05T12:00:00+00:00", made["id"]))
    r = world.c["analyst"].get(f"/api/reports/{made['id']}/pdf")
    assert r.status_code == 200 and "monthly-savings-2026-10-out-of-date.pdf" in r.headers["content-disposition"], r.headers
    assert reports.file_name({**made, "out_of_date": False}, "csv").endswith("monthly-savings-2026-10.csv")


# ------------------------------------------------------------------------------------------------ alerts-reports review: R8 2.14 on the server

def test_a_monthly_savings_report_is_refused_for_a_site_with_fewer_than_seven_days_of_data(world):
    """R8 2.14: "Fewer than 7 days of data: Generate offers only the Daily operations sheet". The web dialog kept to it (pages-D-25), but POST
    /api/reports made a monthly savings report of a site with a day of data. The server now refuses it in one sentence; the daily operations sheet
    is still made, and so is the savings report once the site has a week of data (its first reading, as the fleet's collecting day counts it)."""
    import time
    from gridlytics import reports
    sid, a = world.site, world.c["analyst"]
    this = datetime.now(PKT).strftime("%Y-%m")
    now = int(time.time()) // 60 * 60
    with world.use():
        telemetry.write_rows([(sid, now - 2 * 86400, "csv", 1.0, 1000.0)], ("site_id", "ts", "source_id", "grid_present", "grid_import_wh"))
    r = a.post("/api/reports", json={"kind": "monthly_savings", "site_id": sid, "period": this})
    assert r.status_code == 422, r.text                                          # before: 200, a savings report on two days of data
    assert r.json()["detail"] == (f"Mill One has fewer than {reports.REPORT_DAYS_NEEDED} days of data: its monthly savings report can be generated once "
                                  "it has a week of data, and the daily operations sheet now"), r.text
    assert a.post("/api/reports", json={"kind": "daily_operations", "site_id": sid}).status_code == 200
    with world.use():
        telemetry.write_rows([(sid, now - 8 * 86400, "csv", 1.0, 1000.0)], ("site_id", "ts", "source_id", "grid_present", "grid_import_wh"))
    assert a.post("/api/reports", json={"kind": "monthly_savings", "site_id": sid, "period": this}).status_code == 200


# ------------------------------------------------------------------------------------------------ batch 5: the ledger, the days table and the daily chart

def _put_tdays(sid: str, days: dict[str, dict]) -> None:
    with store.connect() as con:
        for day, data in days.items():
            con.execute("INSERT OR REPLACE INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)", (sid, day, json.dumps(data)))


def test_each_savings_day_carries_its_outage_hours_so_a_losing_day_can_be_explained(world):
    """flow-savings-pilot-bill-17: six of eight Karachi days were negative with no word why. DESIGN_LOG (the compiler fixes) gives the reason for
    most: a day without an outage pays the premium of holding the outage reserve, which the outage days repay. The page has each day's levers; it
    now has each day's outage hours too (energy, not money: a viewer keeps them), so it can say which losing days had no outage. A day with no
    telemetry row reads null, not 0."""
    sid = world.site
    with world.use():
        _put_rows(sid, {"2026-10-03": {"native": _row(-3622.0, -3622.0), "tuned": _row(-100.0, -100.0)},
                        "2026-10-04": {"native": _row(5000.0, 5000.0), "tuned": _row(900.0, 900.0)},
                        "2026-10-05": {"native": _row(10.0, 10.0), "tuned": _row(5.0, 5.0)}})
        _put_tdays(sid, {"2026-10-03": {"outage_minutes": 0}, "2026-10-04": {"outage_minutes": 150}})
    for role in ("analyst", "viewer"):
        page = world.c[role].get(f"/api/sites/{sid}/savings", params={"start": "2026-10-03", "end": "2026-10-05"}).json()
        assert {d["day"]: d.get("outage_hours") for d in page["days"]} == {"2026-10-03": 0.0, "2026-10-04": 2.5, "2026-10-05": None}, role


def test_the_summary_gives_the_claim_of_the_verified_days_only(company):
    """pages-D-22 (R8 2.12): "Days with overrides or data gaps are flagged in the daily bars and excluded from the headline when the owner ticks
    verified days only". The claim counts the partly followed days too, and nothing gave the figure over the verified days (followed in full, no
    data withheld) alone. Each baseline now carries it, by lever, with the verified days' shares of the MDI lever and their carry-over on the
    claim's own terms; a partly followed, withheld or native-block day adds nothing to it."""
    sid = make_site(name="Verified only")
    rows = {"2026-11-02": {"native": _row(1000.0, 1000.0), "tuned": _row(400.0, 400.0)},
            "2026-11-03": {"native": _row(700.0, 700.0, fidelity="partial"), "tuned": _row(300.0, 300.0, fidelity="partial")},
            "2026-11-04": {"native": {**_row(500.0, 0.0), "claim_withheld": "genset_unknown"}, "tuned": {**_row(200.0, 0.0), "claim_withheld": "genset_unknown"}},
            "2026-11-05": {"native": _row(-300.0, 0.0, fidelity="native_block"), "tuned": _row(-900.0, 0.0, fidelity="native_block")},
            "2026-11-06": {"native": _row(2000.0, 2000.0), "tuned": _row(600.0, 600.0)}}
    _put_rows(sid, rows)
    summ = verify.summary(sid, "2026-11-02", "2026-11-06")
    nat, tun = summ["baselines"]["native"], summ["baselines"]["tuned"]
    assert nat["claimed_rs"] == pytest.approx(3700.0) and tun["claimed_rs"] == pytest.approx(1300.0)    # the claim: the partial day in it
    assert nat["verified"]["days"] == 2 and nat["verified_days"] == 2
    assert nat["verified"]["claimed_rs"] == pytest.approx(3000.0)                                       # before: no such figure
    assert tun["verified"]["claimed_rs"] == pytest.approx(1000.0)
    assert nat["verified"]["levers_claimed_rs"]["peak_window"] == pytest.approx(3000.0)
    assert sum(nat["verified"]["levers_claimed_rs"].values()) == pytest.approx(nat["verified"]["claimed_rs"])
    # a viewer reads the count, never the rupees
    from gridlytics import money
    assert money._strip(summ, fee_ok=False, cost_ok=False)["baselines"]["native"]["verified"] == {"days": 2}


def test_the_savings_answer_says_which_native_captures_its_days_were_replayed_on(world):
    """flow-savings-pilot-bill-19: the proof page said only "against the native capture"; the settings it is priced against (work mode, backup
    state of charge, slots, version) were on Devices and data or the pilot's tab B alone, though each scored day stores its capture version. The
    answer now gives each capture the period's days were replayed on, with its settings and the days it priced, and the latest version (not money:
    every role reads it). A period whose days carry no version (stored before) names the capture in force at its end, with no days."""
    sid = world.site
    with world.use():
        sites.capture_native(sid, {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 20}, "installer@fix.test")
        sites.capture_native(sid, {"source": "photo", "work_mode": "tou", "backup_soc_pct": 30, "grid_charge_allowed": True,
                                   "slots": [{"start": "00:00", "mode": "charge_grid", "soc_floor_pct": 30, "soc_target_pct": 90, "grid_charge": True}]},
                             "installer@fix.test")
        sites.capture_native(sid, {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 40}, "installer@fix.test")
        _put_rows(sid, {"2026-10-03": {"native": {**_row(10.0, 10.0), "native_capture_version": 1}, "tuned": _row(5.0, 5.0)},
                        "2026-10-04": {"native": {**_row(10.0, 10.0), "native_capture_version": 2}, "tuned": _row(5.0, 5.0)},
                        "2026-10-05": {"native": {**_row(10.0, 10.0), "native_capture_version": 2}, "tuned": _row(5.0, 5.0)},
                        "2026-09-03": {"native": _row(10.0, 10.0), "tuned": _row(5.0, 5.0)}})
    for role in ("analyst", "viewer"):
        page = world.c[role].get(f"/api/sites/{sid}/savings", params={"start": "2026-10-03", "end": "2026-10-05"}).json()
        caps = page["native_captures"]                              # before: no such field
        assert [(c["version"], c["first_day"], c["last_day"], c["days"]) for c in caps] == [(1, "2026-10-03", "2026-10-03", 1),
                                                                                            (2, "2026-10-04", "2026-10-05", 2)], role
        assert caps[1]["body"]["work_mode"] == "tou" and caps[1]["body"]["backup_soc_pct"] == 30
        assert caps[1]["body"]["slots"][0]["soc_target_pct"] == 90 and caps[1]["source"] == "photo" and caps[1]["by"] == "installer@fix.test"
        assert caps[1]["captured_at"] and page["native_latest_version"] == 3
    old = world.c["analyst"].get(f"/api/sites/{sid}/savings", params={"start": "2026-09-03", "end": "2026-09-03"}).json()
    assert [(c["version"], c["days"]) for c in old["native_captures"]] == [(1, None)]


def test_the_savings_periods_give_the_billing_period_and_the_pilot_window(world, monkeypatch):
    """pages-D-23 (R8 2.12: the period picker offers "day, month, billing period, pilot window"): the page offered this month, last month, the last
    30 days and a custom range, so the MDI rows, keyed by billing month, never lined up with a period. GET /savings/periods dates the billing period
    today falls in (from the day after the last reading to today) and the last whole one, from the connection's reading day as the ledger's months
    are, and the pilot window (from the start's Pakistan day to the calendar's last day, today at the latest); none before the pilot starts and no
    billing period at an off-grid site."""
    from gridlytics import routes_savings
    sid = world.site
    monkeypatch.setattr(routes_savings, "today", lambda: date(2026, 11, 12))
    with world.use():
        cid = sites.connections(sid)[0]["id"]
        sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400, "reading_day": 25}, "test", conn_id=cid)
    a = world.c["viewer"]
    p = a.get(f"/api/sites/{sid}/savings/periods").json()
    assert p["billing"] == {"start": "2026-10-26", "end": "2026-11-12", "reading": "2026-11-25", "month": "2026-11", "reading_day": 25}
    assert p["last_billing"] == {"start": "2026-09-26", "end": "2026-10-25", "month": "2026-10"}
    assert p["pilot"] is None
    with world.use():
        with store.connect() as con:
            con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?)", (sid, "{}", "2026-10-31T20:30:00+00:00", "x"))
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,1)", (sid, "2026-11-02", "2026-11-08", "plan"))
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,1)", (sid, "2026-11-09", "2026-11-15", "native"))
    p = a.get(f"/api/sites/{sid}/savings/periods").json()
    assert p["pilot"] == {"start": "2026-11-01", "end": "2026-11-12", "calendar_end": "2026-11-15"}       # 01:30 on 1 Nov in Pakistan
    # without a reading day the bill is the calendar month
    with world.use():
        sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "test", conn_id=cid)
    p = a.get(f"/api/sites/{sid}/savings/periods").json()
    assert p["billing"]["start"] == "2026-11-01" and p["last_billing"] == {"start": "2026-10-01", "end": "2026-10-31", "month": "2026-10"}
    assert p["billing"]["reading_day"] is None


def test_fleet_and_overview_count_the_months_native_block_days_beside_its_saving(world, monkeypatch):
    """flow-savings-pilot-bill-18, the Fleet and Overview half (left at batch 5): a month whose scored days are a native block of the switchback reads
    "Rs 0" on Fleet and Overview with no word why. The fleet row and the overview's month now count those days (not money: a viewer reads the count)."""
    from gridlytics import routes_sites
    monkeypatch.setattr(routes_sites, "today", lambda: date(2026, 10, 20))
    sid = world.site
    block = {"native": _row(0.4, 0.0, fidelity="native_block"), "tuned": _row(-841.0, 0.0, fidelity="native_block")}
    with world.use():
        _put_rows(sid, {"2026-09-30": block,                                    # last month: not this month's
                        "2026-10-01": block, "2026-10-02": block, "2026-10-03": block,
                        "2026-10-04": {"native": _row(500.0, 500.0), "tuned": _row(100.0, 100.0)}})
    for role in ("admin", "viewer"):
        row = next(s for s in world.c[role].get("/api/fleet").json()["sites"] if s["id"] == sid)
        assert row.get("mtd_native_block_days") == 3, role                      # before: no such field
        month = world.c[role].get(f"/api/sites/{sid}/now").json()["month"]
        assert month.get("native_block_days") == 3, role
    other = next(s for s in world.c["admin"].get("/api/fleet").json()["sites"] if s["id"] == world.other)
    assert other["mtd_native_block_days"] == 0


# ------------------------------------------------------------------------------------------------ integration (savings-page with sweeps): fuel units

def test_the_savings_and_report_csvs_name_the_unit_of_their_litres_columns(world, monkeypatch):
    """Integration follow-up (savings-page with sweeps' frontend-states-formats-01): a captive-gas genset's curve and price are per MMBtu, so its litres
    columns hold MMBtu, while the Savings CSV and the monthly and energy reports' CSV gave them with no unit (the company roll-up's CSV already names
    it). Each now ends its own columns with fuel_unit: L for diesel, MMBtu for gas, blank at a site without a genset."""
    import csv
    import io
    from gridlytics import reports, savings_export
    sid = world.site
    with world.use():
        days = _export_rows(sid, _tariff_version("2025-07-01"))
        assert savings_export.fuel_unit(sid) == "L" and savings_export.fuel_unit(world.other) == ""

    def savings_units() -> set[str]:
        r = world.c["analyst"].get(f"/api/sites/{sid}/savings/export.csv", params={"start": days[0], "end": days[-1]})
        assert r.status_code == 200, r.text
        assert r.text.splitlines()[0].split(",")[-1] == "fuel_unit"                # after the columns of before
        return {x["fuel_unit"] for x in csv.DictReader(io.StringIO(r.text))}
    assert savings_units() == {"L"}
    with world.use():
        sites.put_asset(sid, "genset", {"rated_kw": 1000, "fuel_kind": "gas_captive"}, "test")
    assert savings_units() == {"MMBtu"}
    monkeypatch.setattr(telemetry, "days", lambda site_id, d0, d1: [_tday(d) for d in days])
    _story(monkeypatch)
    for kind, engines in (("monthly_savings", ["report_engine", "day_engine"]), ("energy_only", ["report_engine"])):
        with world.use():
            rep = reports.generate(kind, sid, "2025-11", "test")
            text = reports.file_path(rep["id"], "csv").read_text(encoding="utf-8")
        head = text.splitlines()[0].split(",")
        assert head[-len(engines) - 1:] == ["fuel_unit", *engines], (kind, head)    # the report's own columns end with it; the engines follow
        rows = list(csv.DictReader(io.StringIO(text)))
        assert rows and {x["fuel_unit"] for x in rows} == {"MMBtu"}, kind


def test_the_fuel_logs_audit_line_names_the_unit_of_the_gensets_fuel(world):
    """Integration follow-up (savings-page's audited fuel log with sweeps' frontend-states-formats-01): the audit trail read every fuel log as litres,
    while a captive-gas genset's fuel is in MMBtu. The line now names the unit, so the trail reads "4.5 MMBtu"."""
    sid = world.site
    with world.use():
        sites.put_asset(sid, "genset", {"rated_kw": 1000, "fuel_kind": "gas_captive"}, "test")
    r = world.c["operator"].post(f"/api/sites/{sid}/genset/fuel", json={"litres": 4.5})
    assert r.status_code == 200, r.text
    with world.use():
        line = next(a for a in store.list_audit() if a["action"] == "logged fuel")
    detail = line["detail"] if isinstance(line["detail"], dict) else json.loads(line["detail"])
    assert detail["litres"] == 4.5 and detail["fuel_unit"] == "MMBtu", detail


def test_an_off_grid_sites_savings_days_carry_no_outage_hours(world):
    """Integration (savings-page's per-day outage hours with sweeps' role-operator-10): an off-grid site's grid reads 0 on every minute, so its day rows
    count the whole day an outage. The period's outage time is null at such a site; each day's outage hours are null too, so the page never explains a
    losing day by an outage the site cannot have."""
    sid = world.site
    with world.use():
        _put_rows(sid, {"2026-10-03": {"native": _row(-50.0, -50.0), "tuned": _row(-10.0, -10.0)},
                        "2026-10-04": {"native": _row(80.0, 80.0), "tuned": _row(20.0, 20.0)}})
        _put_tdays(sid, {"2026-10-03": {"outage_minutes": 1440}, "2026-10-04": {"outage_minutes": 1440}})
    page = world.c["analyst"].get(f"/api/sites/{sid}/savings", params={"start": "2026-10-03", "end": "2026-10-04"}).json()
    assert [d["outage_hours"] for d in page["days"]] == [24.0, 24.0]               # a grid site in a day-long outage
    with world.use():
        conn = sites.connections(sid)[0]
        sites.put_connection(sid, {**conn["data"], "regime": "off_grid"}, "test", conn_id=conn["id"])
    page = world.c["analyst"].get(f"/api/sites/{sid}/savings", params={"start": "2026-10-03", "end": "2026-10-04"}).json()
    assert page["energy"]["outage_hours"] is None
    assert [d["outage_hours"] for d in page["days"]] == [None, None]
