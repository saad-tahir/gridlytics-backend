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

from gridlytics import sites, store, tariffs, verify

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
