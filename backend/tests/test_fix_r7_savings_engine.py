"""Round-7 regression tests for the savings engine (verify.py), from the end-to-end audit: which days claim, and what the headline sums.

Each test fails on the code before its fix. Test data: the demo twin's generated days and hand-made day rows only; nothing here calls the network."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from test_fix_api import world  # noqa: F401 (a fixture)
from test_fix_r2_savings import NIGHT_CHARGER, D, P
from test_fix_r6_savings import DAYS, LEAD, _blank_evening, _continuous, _farm, _night_runs, _rows, _run
from test_fix_savings import _put_rows, company, make_site, standing_plan, twin_day  # noqa: F401 (company is a fixture)

from gridlytics import demo, money, sites, store, tariffs, telemetry, twin, verify


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


def _override(sid: str, kind: str, t_from: float, t_to: float, cleared: float | None = None) -> None:
    """An override row as sites.add_override writes it, set in the past (the route refuses an end in the past)."""
    with store.connect() as con:
        con.execute("INSERT INTO overrides (id, site_id, kind, value, from_ts, until_ts, author, reason, created, cleared) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (store.new_id(), sid, kind, None, _iso(t_from), _iso(t_to), "t", "", _iso(t_from), None if cleared is None else _iso(cleared)))


def _plan_row(sid: str, belief: float, valid_until: float, status: str = "superseded", reason: str = "", summary: dict | None = None) -> None:
    """A plans row as plans.tick stores it (the light columns active_at reads)."""
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, reason, summary) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (store.new_id(), sid, _iso(belief), _iso(belief), _iso(belief), _iso(valid_until), "a", status, reason,
                                                     json.dumps(summary or {})))


def _event(sid: str, ts: float, kind: str) -> None:
    with store.connect() as con:
        con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)", (sid, _iso(ts), kind, "edge", "{}"))


def _clear(sid: str, *tables: str) -> None:
    with store.connect() as con:
        for t in tables:
            con.execute(f"DELETE FROM {t} WHERE site_id = ?", (sid,))


# ------------------------------------------------------------------------------------------------ engine-savings-02: control in force

def _closed_loop_day(name: str) -> str:
    """A closed-loop grid site (no demo) whose inverter ran the night charger the day before and on D, every minute read."""
    sid = make_site(name=name, tier="closed_loop")
    _, sp, _ = twin_day(sid, P, 0.9, seed=1, policy=lambda s, ts: twin.table_policy(NIGHT_CHARGER, ts))
    twin_day(sid, D, float(sp["soc"][-1]), seed=2, policy=lambda s, ts: twin.table_policy(NIGHT_CHARGER, ts))
    return sid


def test_a_closed_loop_day_claims_only_while_control_was_in_force(company):
    sid = _closed_loop_day("Closed r7 control")
    t0 = twin.day_start_utc(D)
    full = verify.compute_day(sid, D, with_bound=False)["native"]
    assert full["fidelity"] == "confirmed" and full["claimed_rs"] == full["saving_rs"] and full["claimed_rs"] != 0.0
    assert full["control"]["in_force_share"] == 1.0 and full["fidelity_score"] == 1.0

    # paused all day: the tick held its plans and the edge was sent a signed release, so the inverter ran its own settings. Before: "confirmed" by
    # construction, and the replay's error claimed
    _override(sid, "pause_control", t0 - 3600, t0 + 86400 + 3600)
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["fidelity"] == "not_applied" and r["claimed_rs"] == 0.0 and r["control"]["released_min"] == 1440
    assert r["control"]["in_force_share"] == 0.0
    assert verify.summary(sid, D.isoformat(), D.isoformat())["baselines"]["native"]["verified_days"] == 0

    # released to native from 00:00, cleared at 06:00: a quarter of the day ran native, the rest the plan; a partial day claims, its fidelity beside it
    _clear(sid, "overrides")
    _override(sid, "force_native", t0, t0 + 2 * 86400, cleared=t0 + 6 * 3600)
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["fidelity"] == "partial" and r["claimed_rs"] == r["saving_rs"] and r["control"]["released_min"] == 360
    assert r["fidelity_score"] == 0.75

    # no plan in force: none was ever made (the site never ticked), so nothing on the day was the product's doing
    _clear(sid, "overrides", "plans")
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["fidelity"] == "not_applied" and r["claimed_rs"] == 0.0 and r["control"]["no_plan_min"] == 1440

    # the plan in force expired at 10:00 (the ticks failed, so nothing replaced it): the edge reverts to native on expiry
    _plan_row(sid, t0 - 3600, t0 + 10 * 3600)
    _plan_row(sid, t0 + 3600, t0 + 3600 + 192 * 900, status="failed", reason="not solved")  # a failed tick leaves the plan in force
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["control"]["no_plan_min"] == 14 * 60 and r["fidelity"] == "not_applied" and r["claimed_rs"] == 0.0

    # a pause's held tick at 08:00 ends the plan in force; a fresh plan at 12:00 (the pause cleared) is in force from then on
    _clear(sid, "plans")
    _plan_row(sid, t0 - 3600, t0 + 2 * 86400)
    _plan_row(sid, t0 + 8 * 3600, t0 + 2 * 86400, status="held", reason="control_paused")
    _plan_row(sid, t0 + 12 * 3600, t0 + 2 * 86400, status="active")
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["control"]["no_plan_min"] == 4 * 60 and r["fidelity"] == "partial" and r["claimed_rs"] == r["saving_rs"]

    # the edge fell back to native (its own report) from 04:00 to 18:00: the device ran its own settings for most of the day
    _clear(sid, "plans")
    _plan_row(sid, t0 - 3600, t0 + 2 * 86400)
    _event(sid, t0 + 4 * 3600, "fallback_engaged")
    _event(sid, t0 + 18 * 3600, "fallback_cleared")
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["control"]["edge_fallback_min"] == 14 * 60 and r["fidelity"] == "not_applied" and r["claimed_rs"] == 0.0
    # control disabled on the edge the evening before and never enabled again: no minute of the day was under the plan
    _clear(sid, "events")
    _event(sid, t0 - 3600, "control_disabled")
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["control"]["edge_fallback_min"] == 1440 and r["claimed_rs"] == 0.0
    _event(sid, t0 + 23 * 3600, "control_enabled")
    assert verify.compute_day(sid, D, with_bound=False)["native"]["control"]["edge_fallback_min"] == 23 * 60


def test_a_demo_day_under_a_pause_claims_nothing(company):
    # the demo twin runs the native settings while control is paused (round 2), so the day's saving is not the product's. Before: a demo day was
    # "confirmed" whatever was in force
    sid = _farm("Farm paused r7")
    _run(sid, [LEAD] + DAYS[:1], _night_runs)
    assert verify.compute_day(sid, DAYS[0], with_bound=False)["native"]["fidelity"] == "confirmed"
    t0 = twin.day_start_utc(DAYS[0])
    _override(sid, "pause_control", t0 - 600, t0 + 86400)
    r = verify.compute_day(sid, DAYS[0], with_bound=False)["native"]
    assert r["fidelity"] == "not_applied" and r["claimed_rs"] == 0.0 and r["control"]["released_min"] == 1440
    # the demo has no edge: its telemetry and events say nothing about an edge, so only the plan in force counts
    assert r["control"]["edge_fallback_min"] == 0 and r["control"]["edge_unheard_min"] == 0


# ------------------------------------------------------------------------------------------------ pr-review-03: the chain restarts where the plan takes over

def test_the_first_plan_day_after_days_the_plan_did_not_run_restarts_from_the_measured_state(company):
    # an active site at tier monitor (a recommendation: the inverter ran its own settings), then raised to closed loop. The monitor day's native
    # replay drifts from the battery that really ran native; chained into the first claiming day, that drift was credited or debited at v_end in a
    # claim. A switchback block's first day already restarts for this reason (plan 8.2); an activation is the same change of arm
    sid = _farm("Farm activation r7")
    _run(sid, [LEAD] + DAYS, _night_runs)
    sites.set_tier_allowed(sid, "monitor", "t", "test")
    first = verify.compute_day(sid, DAYS[0], with_bound=False)["native"]
    assert first["fidelity"] == "recommendation_only" and first["claimed_rs"] == 0.0
    sites.set_tier_allowed(sid, "closed_loop", "t", "test")
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS[1:]]
    # before: chained from the monitor day (from "previous_day", its start gap valued at the monitor day's v_end)
    assert rows[0]["chain"]["from"] == "actual" and rows[0]["chain"]["reset"] == "arm_change" and rows[0]["chain"]["start"]["gap_kwh"] == 0.0
    assert rows[0]["fidelity"] == "confirmed" and rows[1]["chain"]["from"] == "previous_day"  # the plan's own days chain on
    # the first plan day claims what it claims with the monitor day not scored at all: nothing the plan did not run rests in it
    with store.connect() as con:
        con.execute("DELETE FROM savings_days WHERE site_id = ? AND day_local = ?", (sid, DAYS[0].isoformat()))
    gap = verify.compute_day(sid, DAYS[1], with_bound=False, forward=False)["native"]
    assert gap["chain"]["reset"] == "first_day" and gap["claimed_rs"] == pytest.approx(rows[0]["claimed_rs"], abs=0.01)
    sites.set_tier_allowed(sid, "monitor", "t", "test")
    verify.compute_day(sid, DAYS[0], with_bound=False, forward=False)
    sites.set_tier_allowed(sid, "closed_loop", "t", "test")
    span = (DAYS[0].isoformat(), DAYS[2].isoformat())
    assert verify.summary(sid, *span)["chain_stale_days"] == []  # a restart from the measured state is never out of step
    # a closed-loop day paused all day (not applied: the inverter ran native) restarts the next plan day too
    t1 = twin.day_start_utc(DAYS[1])
    _override(sid, "pause_control", t1 - 60, t1 + 86400)
    paused, after = (verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS[1:])
    assert paused["fidelity"] == "not_applied" and after["chain"]["reset"] == "arm_change"
    assert verify.summary(sid, *span)["chain_stale_days"] == []
    # two days in a row the plan did not run chain between them (native against native), and only the plan's first day restarts
    _clear(sid, "overrides")
    sites.set_tier_allowed(sid, "monitor", "t", "test")
    a, b = (verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS[:2])
    assert b["fidelity"] == "recommendation_only" and b["chain"]["from"] == "previous_day"


# ------------------------------------------------------------------------------------------------ pr-review-11: no claim rests on a long dropout

def test_a_blank_pv_or_load_is_closed_from_the_balance_and_a_day_the_balance_cannot_close_is_withheld(company):
    sid = _farm("Farm PV blank r7")
    _run(sid, [LEAD] + DAYS, _night_runs)
    full = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    assert all(r["claim_withheld"] is None and r["inputs_unread_min"] == 0 for r in full)
    # PV blank 10:00-13:00, the load, the battery and the genset read. Before: the replay counted the PV as zero, so native's replay saw no sun, ran
    # its set and drained, and the day claimed that (the verifier's case: no other withhold rule applies), and the next day chained from that state.
    # The balance closes the PV exactly (load, battery and genset read), so the replay runs on what the site had
    assert _blank_evening(sid, DAYS[1], 10.0, 13.0, cols=("pv_power_w",)) == 180
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    for r, f in zip(rows, full):
        assert r["claimed_rs"] == pytest.approx(f["claimed_rs"], abs=1.0) and r["inputs_unread_min"] == 0
    assert rows[2]["chain"]["from"] == "previous_day"
    # the load blank too: the balance still gives load less PV exactly, which native's battery and genset answer
    assert _blank_evening(sid, DAYS[1], 10.0, 13.0, cols=("pv_power_w", "load_power_w")) == 180
    r = verify.compute_day(sid, DAYS[1], with_bound=False)["native"]
    assert r["claimed_rs"] == pytest.approx(full[1]["claimed_rs"], abs=1.0) and r["inputs_unread_min"] == 0
    # the battery's power blank as well (its state of charge read): nothing closes the PV or the load, and three hours of them are over 5 % of the
    # day, so the day claims nothing and the next day restarts from the measured state
    assert _blank_evening(sid, DAYS[1], 10.0, 13.0, cols=("pv_power_w", "load_power_w", "battery_power_w")) == 180
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    assert rows[1]["claim_withheld"] == "inputs_unread" and rows[1]["claimed_rs"] == 0.0 and rows[1]["inputs_unread_min"] == 180
    assert rows[1]["fidelity"] == "confirmed" and rows[1]["chain"]["end"]["unread_min"] == 180
    assert rows[2]["chain"]["from"] == "actual" and rows[2]["chain"]["reset"] == "unread_inputs"
    assert rows[0]["claimed_rs"] == pytest.approx(full[0]["claimed_rs"], abs=0.01)
    assert verify.summary(sid, DAYS[0].isoformat(), DAYS[2].isoformat())["baselines"]["native"]["verified_days"] == 2
    # a short gap the balance cannot close (20 minutes) is carried as the replay counts it: the day claims and the next day chains on
    _run(sid, [LEAD] + DAYS, _night_runs)
    assert _blank_evening(sid, DAYS[1], 12.0, 12.0 + 20 / 60, cols=("pv_power_w", "load_power_w", "battery_power_w")) == 20
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    assert rows[1]["claim_withheld"] is None and rows[1]["inputs_unread_min"] == 20 and rows[2]["chain"]["from"] == "previous_day"
    # PV blank at night hides nothing (the sun is down, so the replay's zero is right): no unread minute even with nothing to close it
    _run(sid, [LEAD] + DAYS, _night_runs)
    assert _blank_evening(sid, DAYS[1], 0.0, 4.0, cols=("pv_power_w", "battery_power_w")) == 240
    r = verify.compute_day(sid, DAYS[1], with_bound=False)["native"]
    assert r["inputs_unread_min"] == 0 and r["chain"]["end"]["unread_min"] == 0 and r["claim_withheld"] is None


def test_a_closed_loop_edge_not_heard_for_two_hours_is_partial_and_its_claim_withheld(company):
    sid = _closed_loop_day("Closed r7 unheard")
    t0 = twin.day_start_utc(D)
    with telemetry.connect() as con:
        assert con.execute("DELETE FROM minute WHERE site_id = ? AND ts >= ? AND ts < ?", (sid, t0 + 10 * 3600, t0 + 12 * 3600)).rowcount == 120
    r = verify.compute_day(sid, D, with_bound=False)["native"]
    assert r["control"]["edge_unheard_min"] == 120 and r["fidelity"] == "partial"
    # two hours with neither load nor PV read: the grid's own rule does not count such minutes, so before nothing capped them and the day claimed
    assert r["import_unknown"] is False and r["claim_withheld"] == "inputs_unread" and r["claimed_rs"] == 0.0 and r["inputs_unread_min"] == 120


# ------------------------------------------------------------------------------------------------ engine-savings-06, role-owner-02: what the headline sums

def _day(levers: dict, fidelity: str = "confirmed", withheld: str | None = None, start_carry: float = 0.0, end_carry: float = 0.0) -> dict:
    """A stored day row as compute_day writes it, with hand-made levers and chain (golden figures)."""
    saving = round(sum(v for v in levers.values() if v is not None), 2)
    claimed = saving if fidelity in verify.CLAIMING and not withheld else 0.0
    st = {"soc": 0.5, "gen_on": False, "gen_elapsed": 0, "outage_min": 0, "gen_rescue": False}
    return {"saving_rs": saving, "claimed_rs": claimed, "levers": levers, "fidelity": fidelity, "claim_withheld": withheld,
            "import_unknown": withheld == "import_unknown", "litres_saved": 0.0, "actual": {"cost_rs": 10000.0}, "baseline": {"cost_rs": 10000.0 + saving},
            "peak_kw_actual": 0.0, "peak_kw_baseline": 0.0, "mdi_rate": 0.0, "unserved_kwh_actual": 0.0, "unserved_kwh_baseline": 0.0,
            "inputs_unread_min": 0, "control": {"in_force_share": 1.0},
            "chain": {"from": "previous_day" if start_carry else "actual", "start": {"state": st, "carry_rs": start_carry, "v_rs_per_kwh": 50.0},
                      "end": {"state": st, "carry_rs": end_carry, "v_rs_per_kwh": 50.0, "unread_min": 0}}}


L0 = {"peak_window": 0.0, "offpeak_energy": 0.0, "export": 0.0, "genset": 0.0, "wear": 0.0, "carry_over": 0.0}


def _lv(**kw) -> dict:
    return {**L0, **kw}


GOLDEN = {
    # confirmed: claims 1,100, of which 500 carry-over (its end gap, used by the next day)
    "2026-10-01": _day(_lv(peak_window=1000.0, offpeak_energy=-300.0, wear=-100.0, carry_over=500.0), end_carry=500.0),
    # withheld for its battery's data: the wear lever unknown, so the day's saving is unknown (800 is a part of it)
    "2026-10-02": _day(_lv(peak_window=800.0, offpeak_energy=-200.0, wear=None, carry_over=200.0), withheld="battery_flow_unknown",
                       start_carry=500.0, end_carry=700.0),
    # the plan not in control: read data, claims nothing
    "2026-10-03": _day(_lv(peak_window=50.0, wear=10.0, carry_over=-40.0), fidelity="not_applied", start_carry=700.0, end_carry=660.0),
    # confirmed, restarted (arm change): claims 900, of which 300 carry-over that no later day has used yet
    "2026-10-04": _day(_lv(peak_window=900.0, offpeak_energy=-250.0, wear=-50.0, carry_over=300.0), end_carry=300.0),
}


def test_the_headline_is_the_claim_and_a_withheld_days_partial_levers_add_to_no_total(company):
    sid = make_site(name="Golden r7 headline")
    _put_rows(sid, {d: {"native": r, "tuned": r} for d, r in GOLDEN.items()})
    store.set_setting(f"fee:{sid}", 1000)
    s = verify.summary(sid, "2026-10-01", "2026-10-04", see_fee=True)
    n = s["baselines"]["native"]
    # every scored day whose saving is known: 1,100 + 20 + 900. Before: 2,820, with the withheld day's partial 800 (its carry-over credited without
    # its wear)
    assert n["saving_rs"] == 2020.0
    assert n["levers"] == {"peak_window": 1950.0, "offpeak_energy": -550.0, "export": 0.0, "genset": 0.0, "wear": -140.0, "carry_over": 760.0, "mdi": 0.0}
    # the claim: the claiming days less the carry-over still provisional (the last day's 300). Before: 2,000
    assert n["claimed_rs"] == 1700.0 and n["carry_over_provisional_rs"] == 300.0
    assert n["levers_claimed_rs"] == {"peak_window": 1900.0, "offpeak_energy": -550.0, "export": 0.0, "genset": 0.0, "wear": -150.0, "carry_over": 500.0,
                                      "mdi": 0.0}
    assert sum(n["levers_claimed_rs"].values()) == pytest.approx(n["claimed_rs"], abs=0.01)
    assert (n["claiming_days"], n["withheld_days"], n["unclaimed_days"], n["verified_days"]) == (2, 1, 1, 2)
    # the fee check is built on the same claim: 1,700 over the 3 days whose saving is known, a 31-day month. Before: 20,667
    assert s["fee"]["verified_saving_rs_per_month"] == 17567
    days = {d["day"]: d for d in s["days"]}
    w = days["2026-10-02"]
    assert w["saving_native_rs"] is None and w["saving_tuned_rs"] is None and w["claims"] is False and w["claimed_native_rs"] == 0.0
    assert w["not_claimed_reason"] == "battery_flow_unknown" and w["levers_kept"] == ["peak_window", "offpeak_energy", "export", "genset", "carry_over"]
    assert days["2026-10-03"]["not_claimed_reason"] == "not_applied" and days["2026-10-03"]["saving_native_rs"] == 20.0
    assert days["2026-10-03"]["levers_kept"] is None
    assert days["2026-10-01"]["claims"] is True and days["2026-10-01"]["not_claimed_reason"] is None
    assert days["2026-10-01"]["carry_over_provisional"] is False and days["2026-10-01"]["carry_over_provisional_native_rs"] is None
    assert days["2026-10-04"]["carry_over_provisional"] is True and days["2026-10-04"]["carry_over_provisional_native_rs"] == 300.0
    assert days["2026-10-04"]["claimed_native_rs"] == 900.0 and days["2026-10-04"]["control_share"] == 1.0


def test_a_carry_over_credit_is_claimed_once_a_later_day_has_used_the_stored_energy(company):
    sid = make_site(name="Golden r7 carry")
    _put_rows(sid, {d: {"native": r, "tuned": r} for d, r in GOLDEN.items()})
    # the next day is scored, chained from the last: it debits the 300 the day before credited, and its own levers carry what the energy did
    _put_rows(sid, {"2026-10-05": {b: _day(_lv(peak_window=600.0, carry_over=-200.0), start_carry=300.0, end_carry=100.0) for b in ("native", "tuned")}})
    n = verify.summary(sid, "2026-10-01", "2026-10-04")["baselines"]["native"]
    assert n["claimed_rs"] == 2000.0 and n["carry_over_provisional_rs"] == 0.0
    n = verify.summary(sid, "2026-10-01", "2026-10-05")["baselines"]["native"]
    assert n["claimed_rs"] == 2300.0 and n["carry_over_provisional_rs"] == 100.0  # 1,100 + 900 + 400, less the new last day's 100
    assert n["levers_claimed_rs"]["carry_over"] == 500.0  # 500 + 300 - 200, less the 100 still provisional


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-01, pages-D-03, engine-savings-04,
# ------------------------------------------------------------------------------------------------ engine-savings-05: the MDI lever is the billing month's

def _mdi_day(peak_a: float, peak_b: float, rate: float = 1250.0, floor: float = 125.0, fidelity: str = "confirmed", withheld: str | None = None,
             version: str | None = None) -> dict:
    """A day row that saves Rs 100 in its peak window, with its 30-minute peaks on both arms and its month's MDI terms as compute_day stores them."""
    return {**_day(_lv(peak_window=100.0), fidelity=fidelity, withheld=withheld), "peak_kw_actual": peak_a, "peak_kw_baseline": peak_b,
            "mdi_rate": rate, "mdi_floor_kw": floor, "genset_unknown": withheld == "genset_unknown", "tariff_version": version}


def _put_mdi(sid: str, rows: dict) -> None:
    _put_rows(sid, {d: {"native": r, "tuned": r} for d, r in rows.items()})


def _telemetry_day(sid: str, day: str, peak_kw: float) -> None:
    """A day with telemetry and no savings row (an observation day under the native settings), as aggregate_day stores it."""
    with store.connect() as con:
        con.execute("INSERT OR REPLACE INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                    (sid, day, json.dumps({"peak_kw": peak_kw, "import_kwh": 5000.0, "completeness": 1.0})))


#: 1-10 Oct: 300 kW actual every day, the native replay's 340 kW on 3 Oct and 310 kW on the others. October's lever: (340 - 300) x Rs 1,250 = 50,000
#: over its 31 days, so each day carries 50,000 / 31
OCT_MDI = {f"2026-10-{d:02d}": _mdi_day(300.0, 340.0 if d == 3 else 310.0) for d in range(1, 11)}
SHARE = 50_000.0 / 31


def test_the_mdi_lever_is_the_billing_months_and_a_range_gets_its_days_share(company):
    sid = make_site(name="Golden r7 mdi range")
    _put_mdi(sid, OCT_MDI)
    one = verify.summary(sid, "2026-10-03", "2026-10-03")
    n = one["baselines"]["native"]
    # one day: its share of the month's lever. Before: the whole month's 50,000 on the strength of the day's own 340 kW
    assert n["levers_claimed_rs"]["mdi"] == pytest.approx(SHARE, abs=0.01) and n["claimed_rs"] == pytest.approx(100.0 + SHARE, abs=0.01)
    assert one["days"][0]["mdi_month"] == "2026-10" and one["days"][0]["mdi_share_native_rs"] == pytest.approx(SHARE, abs=0.01)
    a = verify.summary(sid, "2026-10-01", "2026-10-05")["baselines"]["native"]
    b = verify.summary(sid, "2026-10-06", "2026-10-10")["baselines"]["native"]
    w = verify.summary(sid, "2026-10-01", "2026-10-10")["baselines"]["native"]
    # the halves add up to the whole. Before: 1-5 Oct claimed 50,000 and 6-10 Oct 12,500 (its own 310 kW), against 50,000 for 1-10 Oct
    assert b["levers_claimed_rs"]["mdi"] == pytest.approx(5 * SHARE, abs=0.01)
    assert a["claimed_rs"] + b["claimed_rs"] == pytest.approx(w["claimed_rs"], abs=0.02)
    assert w["claimed_rs"] == pytest.approx(1000.0 + 10 * SHARE, abs=0.01) and w["levers"]["mdi"] == pytest.approx(10 * SHARE, abs=0.01)
    m = w["mdi_months"]["2026-10"]
    assert (m["actual_kw"], m["baseline_kw"], m["floor_kw"], m["rate_rs_per_kw"], m["lever_rs"]) == (300.0, 340.0, 125.0, 1250.0, 50_000.0)
    assert (m["days_in_month"], m["scored_days"], m["claiming_days"], m["from"], m["to"]) == (31, 10, 10, "2026-10-01", "2026-10-31")
    # a day of the month with telemetry and no row (the native settings ran it) sets the month's peak on both arms: its measured 360 kW is the
    # bill's MDI whatever the plan did on the other days. Before: the range's own days only, so the plan was credited 50,000
    _telemetry_day(sid, "2026-10-12", 360.0)
    w = verify.summary(sid, "2026-10-01", "2026-10-10")["baselines"]["native"]
    assert (w["mdi_months"]["2026-10"]["actual_kw"], w["mdi_months"]["2026-10"]["baseline_kw"]) == (360.0, 360.0)
    assert w["levers_claimed_rs"]["mdi"] == 0.0 and w["claimed_rs"] == 1000.0


def test_a_month_not_over_books_its_scored_days_share_of_the_mdi_lever_marked_provisional(company, monkeypatch):
    sid = make_site(name="Golden r7 mdi to date")
    _put_mdi(sid, {f"2026-10-{d:02d}": _mdi_day(300.0, 333.0) for d in range(1, 9)})  # to date (33 kW x 1,250) = 41,250, 8 days scored
    monkeypatch.setattr(verify, "_today", lambda: date(2026, 10, 9), raising=False)
    s = verify.summary(sid, "2026-10-01", "2026-10-09")
    n = s["baselines"]["native"]
    # before: the month-to-date 41,250 booked in full after 8 days (95 % of the headline), with nothing to say the month is not over
    assert n["levers_claimed_rs"]["mdi"] == pytest.approx(41_250.0 * 8 / 31, abs=0.01)
    assert n["mdi_provisional_rs"] == pytest.approx(41_250.0 * 8 / 31, abs=0.01)
    assert n["mdi_months"]["2026-10"]["provisional"] is True and all(d["mdi_provisional"] for d in s["days"])
    monkeypatch.setattr(verify, "_today", lambda: date(2026, 11, 1))
    s = verify.summary(sid, "2026-10-01", "2026-10-09")
    assert s["baselines"]["native"]["mdi_provisional_rs"] == 0.0 and s["baselines"]["native"]["mdi_months"]["2026-10"]["provisional"] is False
    assert not any(d["mdi_provisional"] for d in s["days"])


def test_a_withheld_day_claims_no_mdi_share_and_its_replayed_peak_credits_nothing(company):
    sid = make_site(name="Golden r7 mdi withheld")
    # every day withheld for a genset that may have run unseen: a parallel run cut the actual arm's peak (250 kW against the replay's 300)
    _put_mdi(sid, {f"2026-10-{d:02d}": _mdi_day(250.0, 300.0, withheld="genset_unknown") for d in range(1, 4)})
    n = verify.summary(sid, "2026-10-01", "2026-10-03")["baselines"]["native"]
    # before: (300 - 250) x 1,250 = 62,500 claimed, though every day says its claim is withheld
    assert n["claimed_rs"] == 0.0 and n["levers_claimed_rs"]["mdi"] == 0.0 and n["levers"]["mdi"] == 0.0
    assert n["mdi_months"]["2026-10"]["lever_rs"] == 0.0  # its measured 250 kW stands on both arms
    # a claiming day beside a withheld one: the withheld day's measured peak counts on both arms, its replayed 400 kW on neither
    _put_mdi(sid, {"2026-10-04": _mdi_day(300.0, 320.0), "2026-10-05": _mdi_day(260.0, 400.0, withheld="genset_unknown")})
    n = verify.summary(sid, "2026-10-04", "2026-10-05")["baselines"]["native"]
    # before: (400 - 300) x 1,250 = 125,000 claimed on the strength of the withheld day's replay
    assert n["mdi_months"]["2026-10"]["lever_rs"] == 25_000.0  # (320 - 300) x 1,250
    assert n["levers_claimed_rs"]["mdi"] == pytest.approx(25_000.0 / 31, abs=0.01) and n["levers"]["mdi"] == pytest.approx(25_000.0 / 31, abs=0.01)
    assert n["mdi_months"]["2026-10"]["claiming_days"] == 1 and n["claimed_rs"] == pytest.approx(100.0 + 25_000.0 / 31, abs=0.01)


def test_each_months_mdi_lever_is_priced_at_that_months_own_rate(company):
    sid = make_site(name="Golden r7 mdi rate")
    # Tariff F: September out of season (no fixed charge), October in season; 40 kW lower on the replay in both months
    sep = {f"2026-09-{d:02d}": _mdi_day(300.0, 340.0, rate=0.0) for d in range(25, 31)}
    octo = {f"2026-10-{d:02d}": _mdi_day(300.0, 340.0, rate=1250.0, version="v-oct") for d in range(1, 6)}
    _put_mdi(sid, {**sep, **octo})
    n = verify.summary(sid, "2026-09-25", "2026-10-05")["baselines"]["native"]
    # before: the last day's 1,250 priced September too, and both months were claimed in full (100,000)
    assert n["levers_claimed_rs"]["mdi"] == pytest.approx(50_000.0 * 5 / 31, abs=0.01)
    assert n["mdi_months"]["2026-09"]["lever_rs"] == 0.0 and n["mdi_months"]["2026-10"]["lever_rs"] == 50_000.0
    assert n["mdi_months"]["2026-10"]["tariff_version"] == "v-oct" and n["mdi_months"]["2026-09"]["rate_rs_per_kw"] == 0.0
    # the other way round: before, October's rate of 0 zeroed September's lever
    _put_mdi(sid, {**{d: {**r, "mdi_rate": 1250.0} for d, r in sep.items()}, **{d: {**r, "mdi_rate": 0.0} for d, r in octo.items()}})
    n = verify.summary(sid, "2026-09-25", "2026-10-05")["baselines"]["native"]
    assert n["levers_claimed_rs"]["mdi"] == pytest.approx(50_000.0 * 6 / 30, abs=0.01)
    assert n["mdi_months"]["2026-09"]["lever_rs"] == 50_000.0 and n["mdi_months"]["2026-10"]["lever_rs"] == 0.0
    # a new tariff version inside the month: the bill prices the month's MDI at the version in force at its end, the rate its last scored day has
    _put_mdi(sid, {"2026-10-04": _mdi_day(300.0, 340.0, rate=1000.0, version="v-old"), "2026-10-05": _mdi_day(300.0, 340.0, rate=1250.0, version="v-new")})
    m = verify.summary(sid, "2026-10-01", "2026-10-05")["baselines"]["native"]["mdi_months"]["2026-10"]
    assert (m["rate_rs_per_kw"], m["tariff_version"], m["lever_rs"]) == (1250.0, "v-new", 50_000.0)
    # a scored day stores the schedule version its rates came from
    pr = verify._minute_prices(sites.connections(sid)[0], date(2026, 10, 5), sid)
    assert pr["tariff_version"] == tariffs.card_for("B-2(b)", date(2026, 10, 5))[1]["id"] and pr["tariff_version"]


# ------------------------------------------------------------------------------------------------ engine-savings-10: a closed month is re-scored

SRC = {"kind": "manual", "ref": "test", "verified": "primary"}
RESCORED = "re-scored a closed month's savings days"


def _stored(sid: str, day: date) -> tuple[dict, str]:
    with store.connect() as con:
        r = con.execute("SELECT data, computed FROM savings_days WHERE site_id = ? AND day_local = ? AND baseline = 'native'", (sid, day.isoformat())).fetchone()
    return json.loads(r["data"]), r["computed"]


def _rescore_audits(sid: str) -> list[dict]:
    with store.connect() as con:
        return [json.loads(r["detail"]) for r in con.execute("SELECT detail FROM audit WHERE action = ? AND target = ? ORDER BY rowid", (RESCORED, sid))]


def test_a_day_scored_mid_month_is_rescored_once_the_month_closes_and_after_a_later_fca(company, monkeypatch):
    sid = _closed_loop_day("ICP r7 month end")
    conn = sites.connections(sid)[0]
    # the rest of October draws hard on the grid (a second shift started): 1.5 MW, read by the grid meter alone
    t0, t1 = twin.day_start_utc(D) + 2 * 86400, twin.day_start_utc(date(2026, 10, 31))
    telemetry.write_rows([(sid, t, "csv", 1.0, 1500.0 * 1000 / 60) for t in range(t0, t1, 60)], ("site_id", "ts", "source_id", "grid_present", "grid_import_wh"))
    mid = verify.month_totals(sid, conn, D, now=twin.day_start_utc(D) + 86400)["import"]  # what the nightly job of 15 Oct saw: two days scaled up
    end = verify.month_totals(sid, conn, D)["import"]  # the month as it ended
    icp = {p: round((mid[p] * end[p]) ** 0.5, 1) for p in ("peak", "off_peak")}  # the October baseline sits between the two
    assert all(mid[p] < icp[p] < end[p] for p in icp)
    conn = sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 500, "regime": "none",
                                      "icp": {"eligible": True, "peak": {"10": icp["peak"]}, "off_peak": {"10": icp["off_peak"]}}}, "t", conn_id=conn["id"])
    # 14 Oct scored by the nightly job of 15 Oct, the month's totals projected from its first days: below the ICP baseline, so TOU rates
    real = verify.month_totals
    monkeypatch.setattr(verify, "month_totals", lambda s, c, d, now=None: real(s, c, d, twin.day_start_utc(d) + 86400 if now is None else now))
    early = verify.compute_day(sid, D, with_bound=False, forward=False)["native"]
    monkeypatch.setattr(verify, "month_totals", real)
    assert early["icp_case"] == {"peak": "below", "off_peak": "below"} and early["claimed_rs"] != 0.0
    # the nightly job of 1 Nov scores 31 Oct (no data that day) and settles October: 14 Oct is priced on the month as it ended. Before: nothing
    # re-scored it, so its TOU-priced levers stood in the ledger, the report and the fee check
    assert verify.compute_day(sid, date(2026, 10, 31)) is None
    row, computed = _stored(sid, D)
    assert row["icp_case"] == {"peak": "above", "off_peak": "above"} and row["month_totals_projected"] is False
    audits = _rescore_audits(sid)
    assert len(audits) == 1 and audits[0]["billing_period"] == ["2026-10-01", "2026-10-31"]
    assert [(x["day"], x["why"], x["claimed_rs_before"]) for x in audits[0]["days"]] == [(D.isoformat(), "prices", early["claimed_rs"])]
    assert audits[0]["days"][0]["claimed_rs_after"] == row["claimed_rs"] != early["claimed_rs"]
    # the same figures as scoring the day afresh now
    fresh = verify.compute_day(sid, D, with_bound=False, forward=False)["native"]
    assert fresh["levers"] == row["levers"] and fresh["price_basis"] == row["price_basis"]
    # the next night changes nothing and re-scores nothing
    scored = []
    monkeypatch.setattr(verify, "_score_day", lambda s, d, with_bound=True, _f=verify._score_day: scored.append(d) or _f(s, d, with_bound))
    verify.compute_day(sid, date(2026, 11, 1))
    assert scored == [date(2026, 11, 1)] and len(_rescore_audits(sid)) == 1
    # an FCA for October approved after the month closed: the next night re-scores 14 Oct with it (an above-baseline period pays the positive FCA)
    fca = [a["rate"] for a in tariffs.adders_for("2026-10") if a["key"] == "fca"]
    v = tariffs.propose("adder", "fca", "2026-10-31", {"name": "FCA billed in October", "rate": (fca or [0.0])[0] + 3.0, "billing_months": ["2026-10"]},
                        SRC, "t")
    tariffs.approve(v["id"], "t")
    verify.compute_day(sid, date(2026, 11, 2))
    row2 = _stored(sid, D)[0]
    assert D in scored and row2["price_basis"] != row["price_basis"] and len(_rescore_audits(sid)) == 2
    assert _rescore_audits(sid)[1]["days"][0]["why"] == "prices" and row2["claimed_rs"] != row["claimed_rs"]


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-03, engine-savings-12,
# ------------------------------------------------------------------------------------------------ role-analyst-01: the switchback result

def _calendar(sid: str, blocks: list[tuple[str, str, str]]) -> None:
    with store.connect() as con:
        for a, b, arm in blocks:
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,1)", (sid, a, b, arm))


def _sb_day(sid: str, day: str, cost: float, saving: float, i: int, fidelity: str = "confirmed", withheld: str | None = None) -> dict:
    """A priced day of a switchback calendar: its telemetry totals (the regression's covariates, varied by `i`) and its native row."""
    with store.connect() as con:
        con.execute("INSERT OR REPLACE INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                    (sid, day, json.dumps({"pv_kwh": 1000.0 + 37 * (i % 5), "load_kwh": 5000.0 + 53 * (i % 3), "outage_minutes": 30 * (i % 4),
                                           "unserved_kwh": 0.0})))
    r = _day(_lv(peak_window=saving), fidelity=fidelity, withheld=withheld)
    r["actual"] = {"cost_rs": cost, "e_end_kwh": 100.0, "v_end": 30.0}
    r["baseline"] = {"cost_rs": cost + saving, "e_end_kwh": 100.0, "v_end": 30.0}
    return r


def test_the_switchback_gives_no_figure_until_both_arms_have_six_days_of_the_plan_in_control(company):
    # Faisalabad (audit): one plan day, a Sunday at Rs 47k, against seven native weekdays at Rs 120-150k gave "Rs 71,256 a day" (adjusted Rs 22,572)
    # with no Preliminary mark: the gate was 6 days in total with both arms present, and a 5-coefficient regression was fitted on 8 days
    sid = make_site(name="Switchback r7 minimum")
    _calendar(sid, [("2026-10-01", "2026-10-01", "plan"), ("2026-10-02", "2026-10-08", "native")])
    rows = {"2026-10-01": _sb_day(sid, "2026-10-01", 47_391.0, 1_901.0, 0)}
    rows.update({f"2026-10-{d:02d}": _sb_day(sid, f"2026-10-{d:02d}", 120_000.0 + 4_000 * (d % 4), 0.0, d) for d in range(2, 9)})
    _put_rows(sid, {d: {"native": r} for d, r in rows.items()})
    sb = verify.switchback_result(sid)
    assert sb["result"] is None and sb["result_withheld"] == "too_few_days"  # before: a figure from 1 plan day
    assert sb["arm_days"] == {"plan": 1, "native": 7} and sb["min_days_per_arm"] == 6
    # six days on each arm: a figure, with its uncertainty, Preliminary until 8 weeks of switchback, and the counterfactual beside it
    _calendar(sid, [("2026-10-09", "2026-10-13", "plan")])
    for i, d in enumerate(range(9, 14)):
        rows[f"2026-10-{d:02d}"] = _sb_day(sid, f"2026-10-{d:02d}", 119_000.0 + 3_000 * (i % 3), 1_000.0, d)
    _put_rows(sid, {d: {"native": r} for d, r in rows.items()})
    sb = verify.switchback_result(sid)
    r = sb["result"]
    assert sb["arm_days"] == {"plan": 6, "native": 7} and r["plan_days"] == 6 and r["native_days"] == 7
    plan = [rows["2026-10-01"]] + [rows[f"2026-10-{d:02d}"] for d in range(9, 14)]
    native = [rows[f"2026-10-{d:02d}"] for d in range(2, 9)]
    mean = lambda xs: sum(x["actual"]["cost_rs"] for x in xs) / len(xs)  # noqa: E731
    assert r["saving_rs_per_day_unadjusted"] == pytest.approx(mean(native) - mean(plan), abs=0.01)
    assert r["degrees_of_freedom"] == 13 - 5 and r["std_error_rs_per_day_adjusted"] > 0.0
    assert r["lower_80_rs_per_day_adjusted"] < r["saving_rs_per_day_adjusted"]
    assert r["preliminary"] is True and "under_8_weeks" in r["preliminary_reasons"] and sb["switchback_weeks"] < 8
    # plan 8.2 and VER-08: the counterfactual and the switchback agree within 15 % of the switchback saving or Rs 300 a day, whichever is larger
    ag = r["agreement"]
    cf = sum(x["saving_rs"] for x in plan) / 6
    assert ag["counterfactual_rs_per_day"] == pytest.approx(cf, abs=0.01)
    assert ag["switchback_rs_per_day"] == r["saving_rs_per_day_adjusted"]
    assert ag["tolerance_rs_per_day"] == pytest.approx(max(0.15 * abs(r["saving_rs_per_day_adjusted"]), 300.0), abs=0.01)
    assert ag["agrees"] is (abs(r["saving_rs_per_day_adjusted"] - cf) <= ag["tolerance_rs_per_day"]) and ag["agrees"] is False


def test_a_plan_day_the_plan_did_not_control_or_a_withheld_day_is_no_switchback_evidence(company):
    sid = make_site(name="Switchback r7 evidence")
    _calendar(sid, [("2026-09-01", "2026-09-07", "plan"), ("2026-09-08", "2026-09-14", "native")])
    rows = {}
    for i in range(14):
        d = (date(2026, 9, 1) + timedelta(days=i)).isoformat()
        plan = i < 7
        fid = "not_applied" if i == 2 else "confirmed"  # control paused: the inverter ran its own settings on a plan day
        withheld = "genset_unknown" if i in (4, 10) else None  # the genset may have run unseen: the day's actual cost is not known
        rows[d] = _sb_day(sid, d, (9_000.0 if plan else 10_000.0) + 11 * (i % 3), 1_000.0 if plan else 0.0, i, fidelity=fid, withheld=withheld)
    _put_rows(sid, {d: {"native": r} for d, r in rows.items()})
    sb = verify.switchback_result(sid)
    # before: 14 days, the paused day counted as the plan's and the withheld days as known costs
    assert sb["arm_days"] == {"plan": 5, "native": 6} and sb["excluded_days"] == {"plan_not_in_control": 1, "withheld": 2}
    assert sb["result"] is None and sb["result_withheld"] == "too_few_days"
    # a monitor-tier site only recommends: its plan days are recommendation_only, so the switchback gives no result there at all
    mon = make_site(name="Switchback r7 monitor", tier="monitor")
    _calendar(mon, [("2026-09-01", "2026-09-07", "plan"), ("2026-09-08", "2026-09-14", "native")])
    _put_rows(mon, {d: {"native": {**_sb_day(mon, d, r["actual"]["cost_rs"], 0.0, i, fidelity="recommendation_only" if i < 7 else "native_block")}}
                    for i, (d, r) in enumerate(sorted(rows.items()))})
    sb = verify.switchback_result(mon)
    assert sb["result"] is None and sb["result_withheld"] == "monitor_tier" and sb["arm_days"]["plan"] == 0


# ------------------------------------------------------------------------------------------------ engine-savings-11, pages-D-30: every block boundary

def test_a_native_block_that_hands_the_plan_a_fuller_battery_is_credited_at_its_boundary(company):
    # plan 8.2: "at every block boundary the bill engine credits the arm that ends with more stored energy". A backup-mode native holds its battery
    # full; the plan block after it starts on that energy. Before: only plan-to-native boundaries were credited, so the plan's block was flattered
    sid = make_site(name="Switchback r7 native hand-over")
    _calendar(sid, [("2026-09-01", "2026-09-07", "plan"), ("2026-09-08", "2026-09-14", "native"), ("2026-09-15", "2026-09-21", "plan"),
                    ("2026-09-22", "2026-09-28", "native")])
    rows = {}
    for i in range(28):
        d = (date(2026, 9, 1) + timedelta(days=i)).isoformat()
        plan = (i // 7) % 2 == 0
        r = _sb_day(sid, d, 9_000.0 + 13 * (i % 3) if plan else 10_000.0 + 17 * (i % 4), 1_000.0 if plan else 0.0, i,
                    fidelity="confirmed" if plan else "native_block")
        # the plan ends its days at 100 kWh (120 on its first block's last day, 80 on its second's); native's backup mode at 200, its replay at 150
        r["actual"]["e_end_kwh"] = (120.0 if i == 6 else 80.0 if i == 20 else 100.0) if plan else 200.0
        r["baseline"]["e_end_kwh"] = 150.0 if plan else 200.0
        rows[d] = r
    _put_rows(sid, {d: {"native": r} for d, r in rows.items()})
    credits = verify.switchback_result(sid)["carry_over_credits"]
    assert [(c["boundary"], c["from_arm"], c["to_arm"]) for c in credits] == [("2026-09-08", "plan", "native"), ("2026-09-15", "native", "plan"),
                                                                               ("2026-09-22", "plan", "native")]
    # the plan arm's end energy on a native block's last day is not replayed: it is the plan days' mean end energy (100 kWh here), so the native
    # block that handed over 200 kWh takes 100 kWh at v_end off the plan's saving
    back = credits[1]
    assert back["e_end_plan_kwh"] == pytest.approx(100.0) and back["e_end_native_kwh"] == 200.0 and back["e_end_plan_basis"] == "plan_days_mean"
    assert back["credit_rs"] == pytest.approx((100.0 - 200.0) * 30.0)
    assert credits[0]["credit_rs"] == pytest.approx((120.0 - 150.0) * 30.0) and credits[2]["credit_rs"] == pytest.approx((80.0 - 150.0) * 30.0)
    r = verify.switchback_result(sid)["result"]
    assert r["carry_over_credit_rs"] == pytest.approx(-900.0 - 3_000.0 - 2_100.0)


# ------------------------------------------------------------------------------------------------ brief-vs-build-11, engine-savings-08: the hand-over
# ------------------------------------------------------------------------------------------------ is valued at what the native block realised

DAYS5 = [LEAD + timedelta(days=i) for i in range(1, 6)]


def _switch_farm(name: str) -> str:
    """The demo farm on a switchback calendar: the plan's night runs on two days, the native settings on the next two, the plan again."""
    sid = _farm(name)
    native = lambda s, ts: twin.native_policy(s, ts)  # noqa: E731
    _run(sid, [LEAD] + DAYS5, [_night_runs, _night_runs, _night_runs, native, native, _night_runs])
    _calendar(sid, [(DAYS5[0].isoformat(), DAYS5[1].isoformat(), "plan"), (DAYS5[2].isoformat(), DAYS5[3].isoformat(), "native"),
                    (DAYS5[4].isoformat(), DAYS5[4].isoformat(), "plan")])
    return sid


def _claimed(sid: str, d0: date, d1: date) -> dict:
    return verify.summary(sid, d0.isoformat(), d1.isoformat())["baselines"]["native"]


def test_a_plan_blocks_hand_over_is_provisional_until_the_native_block_ends_then_claimed_at_what_it_realised(company):
    # Plan 8.2 credits the plan block's last day with the energy it hands the native block at v_end, and round 6 claimed it once the native block's
    # first day was scored. v_end can differ from what native makes of that energy (round 6: Faisalabad 3,263 credited against 1,771 realised),
    # so the switchback site's claims missed the continuous replay by +1.5k to +2k. The native block's days now replay native on from the plan
    # block's end state beside the block's own replay, and the claim takes the realised value once the block is over
    sid = _switch_farm("Farm hand-over r7")
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS5[:3]]
    credit = rows[1]["chain"]["end"]["carry_rs"]
    assert abs(credit) > 50.0 and rows[2]["fidelity"] == "native_block" and rows[2]["chain"]["reset"] == "switchback"
    ho = rows[2]["handover"]
    assert ho["start"]["state"] == rows[1]["chain"]["end"]["state"] and ho["start"]["carry_rs"] == pytest.approx(credit)
    # the native block is not over: the hand-over is provisional, left out of the claim (before: claimed at v_end once its first day was scored)
    n = _claimed(sid, DAYS5[0], DAYS5[2])
    assert n["carry_over_provisional_rs"] == pytest.approx(credit, abs=0.01)
    assert n["claimed_rs"] == pytest.approx(rows[0]["claimed_rs"] + rows[1]["claimed_rs"] - credit, abs=0.02)
    day1 = next(d for d in verify.summary(sid, DAYS5[0].isoformat(), DAYS5[2].isoformat())["days"] if d["day"] == DAYS5[1].isoformat())
    assert day1["carry_over_handover"]["settled"] is False and day1["carry_over_handover"]["realised_rs"] is None
    # its last day scored: the hand-over is what the block realised, and the plan block's claim is the replay of native straight through both
    # blocks, less the native block's own (unclaimed) score
    rows.append(verify.compute_day(sid, DAYS5[3], with_bound=False)["native"])
    realised = credit + rows[2]["handover"]["rs"] + rows[3]["handover"]["rs"]
    assert rows[3]["handover"]["start"]["state"] == rows[2]["handover"]["end"]["state"]
    pure = _continuous(sid, DAYS5[:4], rows[3]["actual"]["v_end"])
    n = _claimed(sid, DAYS5[0], DAYS5[3])
    assert n["carry_over_provisional_rs"] == 0.0 and n["carry_over_handover_adjustment_rs"] == pytest.approx(realised - credit, abs=0.02)
    assert n["claimed_rs"] == pytest.approx(pure - rows[2]["saving_rs"] - rows[3]["saving_rs"], abs=3.0), (n["claimed_rs"], pure)
    assert sum(n["levers_claimed_rs"].values()) == pytest.approx(n["claimed_rs"], abs=0.02)
    day1 = next(d for d in verify.summary(sid, DAYS5[0].isoformat(), DAYS5[3].isoformat())["days"] if d["day"] == DAYS5[1].isoformat())
    assert day1["carry_over_handover"] == {"days": 2, "v_end_rs": pytest.approx(credit, abs=0.01), "realised_rs": pytest.approx(realised, abs=0.02),
                                           "settled": True}
    # the switchback's credit at that boundary is the realised value too, the v_end figure beside it
    c = next(c for c in verify.switchback_result(sid)["carry_over_credits"] if c["from_arm"] == "plan")
    assert c["realised"] is True and c["provisional"] is False and c["credit_rs"] == pytest.approx(realised, abs=0.02)
    assert c["credit_v_end_rs"] == pytest.approx(credit, abs=1.0)
    # the next plan block restarts from the measured state
    verify.compute_day(sid, DAYS5[4], with_bound=False)
    assert verify._rows_after(sid, "native", DAYS5[3].isoformat())[DAYS5[4].isoformat()]["chain"]["reset"] == "switchback"
    assert verify.summary(sid, DAYS5[0].isoformat(), DAYS5[4].isoformat())["chain_stale_days"] == []


def test_a_native_block_day_without_its_hand_over_replay_is_out_of_step_and_a_recompute_restores_it(company):
    sid = _switch_farm("Farm hand-over stale r7")
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS5[:4]]
    span = (DAYS5[0].isoformat(), DAYS5[3].isoformat())
    settled = _claimed(sid, DAYS5[0], DAYS5[3])["claimed_rs"]
    # rows stored before the hand-over replay: the hand-over cannot be valued, so it stays provisional and the days are listed for a recompute
    with store.connect() as con:
        for d in DAYS5[2:4]:
            for r in con.execute("SELECT baseline, data FROM savings_days WHERE site_id = ? AND day_local = ?", (sid, d.isoformat())).fetchall():
                data = json.loads(r["data"])
                data.pop("handover", None)
                con.execute("UPDATE savings_days SET data = ? WHERE site_id = ? AND day_local = ? AND baseline = ?",
                            (json.dumps(data), sid, d.isoformat(), r["baseline"]))
    s = verify.summary(sid, *span)
    assert s["chain_stale_days"] == [DAYS5[2].isoformat()]
    assert s["baselines"]["native"]["carry_over_provisional_rs"] == pytest.approx(rows[1]["chain"]["end"]["carry_rs"], abs=0.01)
    # a recompute of the native block's first day re-scores the next, whose hand-over starts where the first's now ends
    verify.compute_range(sid, DAYS5[2], DAYS5[2])
    assert verify.summary(sid, *span)["chain_stale_days"] == []
    assert _claimed(sid, DAYS5[0], DAYS5[3])["claimed_rs"] == pytest.approx(settled, abs=0.01)
    # the plan block's last day re-scored on its own (say, its data corrected): the native block's hand-over replays follow it
    verify.compute_day(sid, DAYS5[1], with_bound=False, forward=False)
    assert verify.summary(sid, *span)["chain_stale_days"] == []  # the same end state: nothing moved
    with store.connect() as con:
        r = json.loads(con.execute("SELECT data FROM savings_days WHERE site_id = ? AND day_local = ? AND baseline = 'native'",
                                   (sid, DAYS5[1].isoformat())).fetchone()["data"])
        r["chain"]["end"]["carry_rs"] += 100.0
        con.execute("UPDATE savings_days SET data = ? WHERE site_id = ? AND day_local = ? AND baseline = 'native'", (json.dumps(r), sid, DAYS5[1].isoformat()))
    assert verify.summary(sid, *span)["chain_stale_days"] == [DAYS5[2].isoformat()]


def test_energy_handed_to_a_day_the_plan_did_not_run_is_claimed_at_what_that_day_realised(company):
    # 1 Oct (the plan in control) credits 500 at v_end for the energy it leaves. Control is paused on 2 Oct: the inverter runs its own settings on
    # that energy, 150 cheaper than native from native's own state, and 200 of the gap is still unspent at the day's end. So the energy realised
    # 150 + 200 = 350: the credit plus the paused day's own saving (150 - 300). Before: the 500 was claimed once 2 Oct was scored
    sid = make_site(name="Golden r7 hand-over")
    rows = {"2026-10-01": _day(_lv(peak_window=1000.0, carry_over=500.0), end_carry=500.0),
            "2026-10-02": _day(_lv(peak_window=150.0, carry_over=-300.0), fidelity="not_applied", start_carry=500.0, end_carry=200.0),
            "2026-10-03": _day(_lv(peak_window=700.0, carry_over=50.0), end_carry=50.0)}  # the plan again: restarted from the measured state
    _put_rows(sid, {d: {"native": r, "tuned": r} for d, r in rows.items()})
    s = verify.summary(sid, "2026-10-01", "2026-10-03")
    n = s["baselines"]["native"]
    assert n["claimed_rs"] == pytest.approx(1500.0 - 150.0 + 700.0) and n["carry_over_handover_adjustment_rs"] == -150.0
    assert n["carry_over_provisional_rs"] == 50.0 and n["levers_claimed_rs"]["carry_over"] == pytest.approx(350.0)
    assert sum(n["levers_claimed_rs"].values()) == pytest.approx(n["claimed_rs"])
    assert s["days"][0]["carry_over_handover"] == {"days": 1, "v_end_rs": 500.0, "realised_rs": 350.0, "settled": True}
    # while the paused day's own saving is unknown (withheld for its data), so is what the energy realised: the credit stays provisional
    _put_rows(sid, {"2026-10-02": {b: _day(_lv(peak_window=150.0, wear=None, carry_over=-300.0), fidelity="not_applied", withheld="battery_flow_unknown",
                                           start_carry=500.0, end_carry=200.0) for b in ("native", "tuned")}})
    n = verify.summary(sid, "2026-10-01", "2026-10-03")["baselines"]["native"]
    assert n["carry_over_provisional_rs"] == 550.0 and n["claimed_rs"] == pytest.approx(1000.0 + 700.0)


# ------------------------------------------------------------------------------------------------ engine-savings-08, engine-money-live-02: HANDOFF
# ------------------------------------------------------------------------------------------------ 8.A.1's done criterion, 11 days on three seeds

ELEVEN = [LEAD + timedelta(days=i) for i in range(1, 12)]


@pytest.mark.parametrize("seed", ["a", "b", "c"])
def test_the_off_grid_farms_eleven_day_ledger_tracks_the_continuous_replay_and_its_claim_rests_on_no_carry_over_value(company, seed):
    # The audit read the per-day ledger before round 6: each day's replays restarted from the actual arm's state and the whole end gap was credited
    # at v_end (the genset's all-in cost at its night load, Rs 150-180/kWh), so 11 days missed the continuous replay by -6.2k to +7.9k and the
    # farm's month was claimed on carry-over while it burnt more diesel than native. With chained replays (round 6), the last day's credit
    # provisional (round 7, role-owner-02) and the hand-over to days the plan did not run realised (brief-vs-build-11), the period's ledger is the
    # continuous replay, and the claim is the cost difference alone: no rupee of it rests on how stored energy is valued
    sid = _farm(f"Farm eleven r7 {seed}")
    _run(sid, [LEAD] + ELEVEN, _night_runs)
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in ELEVEN]
    assert all(r["claimed_rs"] == r["saving_rs"] for r in rows)
    cont = _continuous(sid, ELEVEN, rows[-1]["actual"]["v_end"])
    n = verify.summary(sid, ELEVEN[0].isoformat(), ELEVEN[-1].isoformat())["baselines"]["native"]
    assert abs(n["saving_rs"] - cont) < 1_000.0 and n["saving_rs"] == pytest.approx(cont, abs=5.0), (n["saving_rs"], cont)
    end = rows[-1]["chain"]["end"]["carry_rs"]
    assert n["carry_over_provisional_rs"] == pytest.approx(end, abs=0.01)
    assert n["claimed_rs"] == pytest.approx(cont - end, abs=5.0) and n["levers_claimed_rs"]["carry_over"] == pytest.approx(0.0, abs=0.05)


# ------------------------------------------------------------------------------------------------ pr-review-02: a re-score is audited, answered and
# ------------------------------------------------------------------------------------------------ marked where it changes a closed month

RECOMPUTED = "recomputed savings days"
WALKED = "re-scored later savings days"


def _audits(sid: str, action: str) -> list[dict]:
    with store.connect() as con:
        return [json.loads(r["detail"]) | {"actor": r["actor"]} for r in
                con.execute("SELECT actor, detail FROM audit WHERE action = ? AND target = ? ORDER BY rowid", (action, sid))]


def test_a_recompute_answers_audits_and_marks_every_day_it_rescores_in_a_closed_month(company):
    # Round 6's forward walk re-scores the later days whose chain start moved, up to 62 of them and across closed months. Before: the recompute
    # answered {"days": 1}, wrote no audit line, and nothing marked the closed month's days whose claims it rewrote (a report of that month
    # kept its old figures)
    sid = _farm("Farm restated r7")
    _run(sid, [LEAD] + DAYS, _night_runs)
    first = {d.isoformat(): {b: r["claimed_rs"] for b, r in verify.compute_day(sid, d, with_bound=False).items()} for d in DAYS}
    assert _audits(sid, WALKED) == []  # scoring days in order re-scores nothing
    # a dropout found later on the first day (October is over): it now claims nothing, and the day after it restarts from the measured state
    _blank_evening(sid, DAYS[0])
    out = verify.compute_range_detail(sid, DAYS[0], DAYS[0], by="analyst@example.test")
    assert out["days"] == 1 and out["rescored_days"] == [DAYS[1].isoformat(), DAYS[2].isoformat()]
    assert verify.compute_range(sid, DAYS[0], DAYS[0]) == 1  # the old answer's figure
    rows = {d.isoformat(): _rows(sid, d) for d in DAYS}
    now = {d: r["native"] for d, r in rows.items()}
    changed = [d for d in rows if any(abs(rows[d][b]["claimed_rs"] - first[d][b]) > 0.005 for b in rows[d])]
    assert changed and out["restated_days"] == changed
    # one audit line for the recompute: each day it re-scored, why, and its claim before and after
    a = _audits(sid, RECOMPUTED)[0]
    assert a["actor"] == "analyst@example.test" and (a["start"], a["end"]) == (DAYS[0].isoformat(), DAYS[0].isoformat())
    assert [(x["day"], x["why"]) for x in a["days"]] == [(DAYS[0].isoformat(), "recompute"), (DAYS[1].isoformat(), "chain"), (DAYS[2].isoformat(), "chain")]
    for x in a["days"]:
        assert x["claimed_rs_before"] == pytest.approx(first[x["day"]]["native"], abs=0.01) and x["claimed_rs_after"] == now[x["day"]]["claimed_rs"]
        assert x["restated"] is (x["day"] in changed)
    # the rows say so, and the summary lists them for the month's reports
    for d in changed:
        r = now[d]["restated"][-1]
        assert r["claimed_rs_before"] == pytest.approx(first[d]["native"], abs=0.01) and r["claimed_rs_after"] == now[d]["claimed_rs"]
        assert r["by"] == "analyst@example.test" and r["why"] == ("recompute" if d == DAYS[0].isoformat() else "chain")
    s = verify.summary(sid, DAYS[0].isoformat(), DAYS[-1].isoformat())
    assert [x["day"] for x in s["restated_days"]] == changed and all(x["at"] for x in s["restated_days"])
    # the nightly scoring of a day whose later days moved writes its own audit line (here the third day's stored start is off)
    with store.connect() as con:
        for b, data in _rows(sid, DAYS[2]).items():
            data["chain"]["start"]["state"]["soc"] += 0.01
            con.execute("UPDATE savings_days SET data = ? WHERE site_id = ? AND day_local = ? AND baseline = ?",
                        (json.dumps(data), sid, DAYS[2].isoformat(), b))
    verify.compute_day(sid, DAYS[1], with_bound=False)
    walks = _audits(sid, WALKED)
    assert len(walks) == 1 and walks[0]["actor"] == "system" and walks[0]["after_day"] == DAYS[1].isoformat()
    assert [(x["day"], x["why"], x["restated"]) for x in walks[0]["days"]] == [(DAYS[2].isoformat(), "chain", False)]
    assert len(_rows(sid, DAYS[2])["native"].get("restated") or []) == (1 if DAYS[2].isoformat() in changed else 0)  # its marks carried through


def test_a_rescore_in_a_month_not_over_is_no_restatement(company, monkeypatch):
    monkeypatch.setattr(verify, "_today", lambda: date(2026, 10, 20))  # October is still open
    sid = _farm("Farm open month r7")
    _run(sid, [LEAD] + DAYS, _night_runs)
    for d in DAYS:
        verify.compute_day(sid, d, with_bound=False)
    _blank_evening(sid, DAYS[0])
    out = verify.compute_range_detail(sid, DAYS[0], DAYS[0])
    assert out["rescored_days"] and out["restated_days"] == []
    assert not any(_rows(sid, d)["native"].get("restated") for d in DAYS)
    assert verify.summary(sid, DAYS[0].isoformat(), DAYS[-1].isoformat())["restated_days"] == []


def test_the_recompute_route_answers_the_later_days_it_rescored(world, monkeypatch):
    seen = {}

    def detail(site_id, d0, d1, by="system"):
        seen["by"] = by
        return {"days": 2, "rescored_days": ["2026-10-14"], "restated_days": ["2026-10-14"]}

    monkeypatch.setattr(verify, "compute_range_detail", detail)
    r = world.c["analyst"].post(f"/api/sites/{world.site}/savings/recompute", json={"start": "2026-10-12", "end": "2026-10-13"})
    assert r.status_code == 200 and r.json() == {"days": 2, "rescored_days": ["2026-10-14"], "restated_days": ["2026-10-14"]}
    assert seen["by"] and seen["by"] != "system"


# ------------------------------------------------------------------------------------------------ engine-savings-03, brief-vs-build-10: the tuned
# ------------------------------------------------------------------------------------------------ baseline's HOMER cycle charging at genset sites

def _off_grid_plant(**kw) -> twin.Plant:
    """The demo farm's plant: 120 kWh (40 kW each way), a 48 kW set at a 30 % minimum under manual start, off grid."""
    p = twin.Plant(e_nom=120.0, soc_min=0.15, soc_max=0.95, p_ch=40.0, p_dis=40.0, g_max=0.0, gen_kw=48.0, gen_fmin=0.3, gen_control="manual",
                   gen_min_run=60, load_mode=1)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_cycle_charging_runs_the_set_near_its_rating_and_stops_at_the_set_point():
    # HOMER's cycle charging (plan 8.1): a running set charges the battery near its rating and stops at the set point. Before, the twin had no such
    # rule: the operator's rule followed the load (a 20 kW load on a 48 kW set charges nothing) and ran the set from the lights going out all day
    ts = twin.minutes_of(D)
    n = len(ts)
    series = {"ts": ts, "pv": np.zeros(n), "load": np.full(n, 20.0), "crit": np.full(n, 8.0), "grid": np.zeros(n)}
    pol = twin.table_policy([{"start": "00:00", "mode": "self_use", "soc_floor_pct": 25.0, "soc_target_pct": 100.0, "grid_charge": False}], ts)
    plant = _off_grid_plant()
    op = twin.simulate(series, pol, plant, 0.15)
    cc = twin.simulate(series, {**pol, "cycle_soc": np.full(n, 0.8)}, plant, 0.15)
    assert op["gen"][30] == pytest.approx(20.0) and op["gen_on"][10:].all()  # the operator's rule: the load, and the set never stops
    assert cc["gen"][30] == pytest.approx(48.0) and cc["bat"][30] == pytest.approx(-28.0)  # the rating: the load and 28 kW into the battery
    stop = int(np.argmax(~cc["gen_on"][10:])) + 10
    assert cc["soc"][stop - 1] >= 0.8 - 1e-9 and cc["soc"][stop - 2] < 0.8  # it stops at the set point
    # golden: started as the lights go out (minute 10 of the outage), 78 kWh (15 % to 80 % of 120) at 28 kW x 0.95 takes 176 minutes
    assert int(np.argmax(cc["gen_on"])) == 9 and stop - 9 == 176
    assert cc["soc"].max() == pytest.approx(0.8, abs=0.005) and cc["gen_on"].sum() < 0.6 * op["gen_on"].sum()
    # a soc_threshold controller stops at the set point too, instead of its own level
    th = twin.simulate(series, {**pol, "cycle_soc": np.full(n, 0.6)}, _off_grid_plant(gen_control="soc_threshold", gen_soc_on=0.25), 0.2)
    assert th["soc"].max() == pytest.approx(0.6, abs=0.005)
    # off (NaN) or without a battery the rule changes nothing
    assert np.array_equal(twin.simulate(series, {**pol, "cycle_soc": np.full(n, np.nan)}, plant, 0.15)["gen"], op["gen"])


def test_the_tuned_baseline_at_a_genset_site_is_fitted_with_cycle_charging_and_claims_must_beat_it(company):
    # Before: the tuned fit searched static tables only and the tuned arm ran the operator's genset rule, so at the off-grid farm, where a table's
    # modes do not apply in an outage, the tuned arm was the native arm minute for minute and "claims must beat the tuned baseline" protected nothing
    sid = _farm("Farm tuned r7")
    _run(sid, [LEAD] + DAYS[:1], lambda s, ts: twin.native_policy(s, ts))  # the farm ran its own settings
    rows = verify.compute_day(sid, DAYS[0], with_bound=False, forward=False)
    rule = rows["tuned"]["tuned_rule"]
    assert rule["cycle_soc_pct"] in verify.CYCLE_SET_POINTS
    n, t = rows["native"]["baseline"], rows["tuned"]["baseline"]
    assert t["genset_hours"] < n["genset_hours"] - 1.0 and t["cost_rs"] < n["cost_rs"]
    # the actual arm ran native: about nothing saved against native, and a loss against the better rule
    assert rows["tuned"]["saving_rs"] < rows["native"]["saving_rs"] - 1_000.0
    assert verify.summary(sid, DAYS[0].isoformat(), DAYS[0].isoformat())["baselines"]["tuned"]["cycle_charge_soc_pct"] == rule["cycle_soc_pct"]
    # the rule is the cheapest on its window of the best table alone and the table with each set point
    site = {**sites.full(sid), "native": verify.native_for_day(sid, DAYS[0])}
    assert verify.fit_tuned(site, DAYS[0]) == rule
    # a site with no genset fits the table alone
    grid = make_site(name="No genset tuned r7", genset=False)
    twin_day(grid, D, 0.5)
    assert verify.compute_day(grid, D, with_bound=False, forward=False)["tuned"]["tuned_rule"]["cycle_soc_pct"] is None


# ------------------------------------------------------------------------------------------------ engine-money-live-11: the tuned baseline is never
# ------------------------------------------------------------------------------------------------ worse than the native capture on its fit window

#: an installer's hand-tuned table (plan 8.1: "owners already hand-tune their inverters"): the grid charges the battery to 90 % overnight, it holds
#: from 06:00 while PV tops it up, discharges to 40 % in the evening peak and then runs self-use at its 30 % backup floor
HAND_TUNED = {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 30, "slots": [
    {"start": "00:00", "mode": "charge_grid", "soc_floor_pct": 30, "soc_target_pct": 90, "grid_charge": True},
    {"start": "06:00", "mode": "hold", "soc_floor_pct": 30, "soc_target_pct": 100, "grid_charge": False},
    {"start": "18:00", "mode": "discharge", "soc_floor_pct": 40, "soc_target_pct": 100, "grid_charge": False},
    {"start": "22:00", "mode": "self_use", "soc_floor_pct": 30, "soc_target_pct": 100, "grid_charge": False}]}
#: the observation week before November (the fit's window for a November day) and the scored days
WEEK = [date(2026, 10, 25) + timedelta(days=i) for i in range(7)]
NOV = [date(2026, 11, 1), date(2026, 11, 2)]


def _mall(name: str, native: dict, conn: dict | None = None) -> str:
    """The demo mall (100 kWh at 50 kW, 200 kWp, an auto-start set) with a fixed id, on a B-2(b) connection with no export unless `conn` says."""
    spec = next(s for s in demo.DEMO_SITES if s["key"] == "mall")
    s = sites.create({**spec["site"], "name": name}, "t", site_id="t-" + name.lower().replace(" ", "-"), demo_link=demo.DEMO_LINK)
    sid = s["id"]
    with store.connect() as con:
        con.execute("UPDATE sites SET data = ?, status = 'active' WHERE id = ?", (json.dumps({**s["data"], **spec["extra"]}), sid))
    sites.put_connection(sid, conn or {"tariff_category": "B-2(b)", "sanctioned_load_kw": 650, "regime": "none"}, "t")
    for role, fields in spec["assets"].items():
        sites.put_asset(sid, role, fields, "t")
    sites.set_tier_capable(sid, "closed_loop")
    sites.set_tier_allowed(sid, "monitor", "t", "demo")
    sites.capture_native(sid, native, "t")
    standing_plan(sid)
    return sid


def _window_costs(sid: str, day: date, policies: dict) -> dict:
    """What each policy costs over the fit window of `day` (the 7 days before its month starts), replayed from each day's measured start and each
    day's stored-energy change valued at the ledger's v_end, as the ledger values it."""
    site = {**sites.full(sid), "native": verify.native_for_day(sid, day)}
    conn = site["connections"][0]
    plant = twin.plant_of(site)
    out = dict.fromkeys(policies, 0.0)
    for d in WEEK:
        series, act = verify._replay_inputs(site, d)
        soc0 = verify._soc_start(sid, twin.day_start_utc(d), act["g"], act["f"], plant)
        tot = verify.month_totals(sid, conn, d)
        pr = verify._minute_prices(conn, d, sid, tot.get("import"), tot.get("export"))
        v_end = verify._terminal_value(site, d, series, pr)[1]
        for k, pol in policies.items():
            sim = twin.simulate(series, pol(site, series["ts"]), plant, soc0)
            e_end = float(sim["soc"][-1]) * plant.e_nom
            out[k] += verify.price_arm(site, sim, pr, d, e_end, v_end)["cost_rs"] - v_end * (e_end - soc0 * plant.e_nom)
    return out


def test_the_tuned_baseline_is_never_worse_than_the_native_capture_on_its_window(company):
    # Lahore's tuned table cost Rs 294 a day more than its native settings: the fit tried only the eight TOU tables, each forcing a peak discharge,
    # none plain self-use and never the capture itself. Here the installer's own table beats every one of the eight over the observation week
    sid = _mall("Mall hand tuned r7", HAND_TUNED)
    _run(sid, WEEK + NOV, lambda s, ts: twin.native_policy(s, ts))  # the site ran its own settings
    rows = verify.compute_day(sid, NOV[1], with_bound=False, forward=False)
    rule = rows["tuned"]["tuned_rule"]
    window = tariffs.peak_window(tariffs.calendar_for(sites.full(sid)["connections"][0], NOV[1]), 11)
    pols = {"native": twin.native_policy, "rule": lambda s, ts: verify.tuned_policy(rule, ts)}
    pols.update({v: (lambda s, ts, v=v: twin.table_policy(verify.tuned_table(s, v, window), ts)) for v in verify.TUNED_VARIANTS})
    cost = _window_costs(sid, NOV[1], pols)
    grid_best = min(cost[v] for v in verify.TUNED_VARIANTS)
    assert grid_best > cost["native"] + 1_000.0  # the case: the best of the eight costs over Rs 1,000 more on the week than the capture
    # the rule is never worse than the capture on its window (plan VER-12); here it is the capture itself
    assert cost["rule"] <= cost["native"] + 0.01
    assert rule["kind"] == "native_capture" and rule["native"] == verify.native_for_day(sid, NOV[1])["body"] and rule["cycle_soc_pct"] is None
    assert rule["fit"] == {"days": [d.isoformat() for d in sorted(WEEK, reverse=True)], "cost_rs": pytest.approx(cost["rule"], abs=0.01),
                           "native_cost_rs": pytest.approx(cost["native"], abs=0.01)}
    # golden: on the scored day both baselines replay the capture from the same start, so they cost the same to the paisa and save the same
    n, t = rows["native"], rows["tuned"]
    assert t["baseline"]["cost_rs"] == n["baseline"]["cost_rs"] and t["saving_rs"] == n["saving_rs"]
    s = verify.summary(sid, NOV[1].isoformat(), NOV[1].isoformat())["baselines"]["tuned"]
    assert s["rule_kind"] == "native_capture" and s["rule_fit"]["days"] == rule["fit"]["days"]
    # a viewer and an API key read the window's days but not its rupees
    assert money.for_key(s)["rule_fit"] == {"days": rule["fit"]["days"]}


def test_the_native_like_self_use_table_is_fitted_where_the_tou_tables_lose(company, monkeypatch):
    # Lahore's case: on A-2(c), grid charging off-peak at Rs 39.48 to discharge at Rs 48.28 loses after the round trip and the wear, so the tables
    # that force it cost more than none. With the grid held to the four tables that grid-charge and a capture that charges to 100 % every night,
    # the native-like self-use table (self-use at the capture's backup floor) is the rule: before, there was no such candidate
    monkeypatch.setattr(verify, "TUNED_VARIANTS", [(fp, hb, 80) for fp in (20, 40) for hb in (False, True)])
    night = {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 30, "slots": [
        {"start": "00:00", "mode": "charge_grid", "soc_floor_pct": 30, "soc_target_pct": 100, "grid_charge": True},
        {"start": "06:00", "mode": "self_use", "soc_floor_pct": 30, "soc_target_pct": 100, "grid_charge": False}]}
    sid = _mall("Mall night charger r7", night, conn={"tariff_category": "A-2(c)", "sanctioned_load_kw": 650, "regime": "net_metering_legacy",
                                                       "export_cap_kw": 180, "agreement_start": "2019-07-01", "agreement_end": "2027-07-01"})
    _run(sid, WEEK + NOV, lambda s, ts: twin.native_policy(s, ts))
    rule = verify.compute_day(sid, NOV[1], with_bound=False, forward=False)["tuned"]["tuned_rule"]
    assert rule["kind"] == "self_use" and rule["table"] == [{"start": "00:00", "mode": "self_use", "soc_floor_pct": 30.0, "soc_target_pct": 100.0,
                                                             "grid_charge": False}]
    window = tariffs.peak_window(tariffs.calendar_for(sites.full(sid)["connections"][0], NOV[1]), 11)
    pols = {"native": twin.native_policy, "table": lambda s, ts: twin.table_policy(rule["table"], ts)}
    pols.update({v: (lambda s, ts, v=v: twin.table_policy(verify.tuned_table(s, v, window), ts)) for v in verify.TUNED_VARIANTS})
    cost = _window_costs(sid, NOV[1], pols)
    assert cost["table"] < min(cost[v] for v in verify.TUNED_VARIANTS) and cost["table"] < cost["native"] - 1_000.0
    assert rule["fit"]["cost_rs"] <= rule["fit"]["native_cost_rs"] - 1_000.0


# ------------------------------------------------------------------------------------------------ engine-optimiser-14 (role-owner-05, role-operator-12,
# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-06, engine-money-live-07)

def _hindsight(seen: dict, status: str = "optimal", limited: bool = False, e_end: float = 21.5):
    """A stand-in hindsight solve: 100 kW imported all day, an end energy of `e_end` kWh, and the breakdown's genset, wear, unserved and MDI terms."""
    def solve(p, time_limit=10.0, **kw):
        seen["p"], seen["time_limit"] = p, time_limit
        T = int(p["T"])
        return {"status": status, "time_limited": limited, "M": [120.0],
                "breakdown": {"energy_rs": -1.0, "genset_rs": 0.0, "wear_rs": 50.0, "unserved_rs": 500.0, "mdi_rise_rs": 2_500.0},
                "sol": {"G": np.full(T, 100.0), "X": np.zeros(T), "E": np.r_[np.full(T, 107.5), e_end]}}
    return solve


def test_the_bound_prices_every_term_the_solve_minimised_at_the_ledgers_prices_and_refuses_a_time_limited_solve(company, monkeypatch):
    # Before: the bound's cost was energy + genset + wear at the solve's own (projected) prices, while the solve also traded against unserved load at
    # VoLL and the rise of the month's maximum demand; it took a time-limited solve as a bound, and held it against the 1-minute native arm
    from test_fix_savings import WEEKDAY, _series
    sid = make_site(name="Bound terms r7", genset=False)
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    sites.add_override(sid, {"kind": "reserve", "value": 80.0, "until": until}, "t")
    seen: dict = {}
    monkeypatch.setattr(verify.optimise, "solve", _hindsight(seen))
    pr = {"c_imp": np.full(1440, 40.0), "c_exp": np.full(1440, 5.0), "peak": np.zeros(1440, dtype=bool)}
    actual = {"cost_rs": 100_000.0, "unserved_kwh": 2.0, "peak_kw": 130.0}
    out = verify._bound(sites.full(sid), WEEKDAY, _series(outage=None), pr, 0.5, actual, 50.0, 300.0, c_next=40.0, v_end=30.0)
    p = seen["p"]
    # the ledger's prices (the month's ICP case settled in them), no calendar ageing, today's override not applied to a past day
    assert np.allclose(p["c_imp"], 40.0) and np.allclose(p["c_exp"], 5.0) and p["icp"] == {} and not p["battery"]["b_cal"].any()
    assert not p["override_mask"].any() and "reserve_manual" not in p and p["terminal"]["c_next"] == 40.0 and p["terminal"]["H_peak"] >= 24
    # golden: the hindsight plan on every term: 2,400 kWh at Rs 40, wear 50, unserved 500, MDI rise 2,500, less 21.5 kWh at Rs 30
    hind = 96_000.0 + 50.0 + 500.0 + 2_500.0 - 30.0 * 21.5
    # the actual arm on the same terms: its cost, 2 kWh unserved at the site's VoLL (Rs 500), its own rise above M0 or the floor, less its end energy
    mdi = p["mdi"]
    rise = mdi["rate"] * max(0.0, 130.0 - max(mdi["M0"], mdi["floor"]))
    act = 100_000.0 + 500.0 * 2.0 + rise - 30.0 * 50.0
    assert out["status"] == "optimal" and out["solved_headroom_rs"] == pytest.approx(act - hind, abs=0.01)
    assert out["bound_rs"] == pytest.approx(300.0 + max(0.0, act - hind), abs=0.01) and out["mdi_m0_kw"] == pytest.approx(mdi["M0"], abs=0.01)
    # a hindsight plan that does worse than the site did on these terms (the 15-minute model) is no headroom: the bound is the saving, never under 0
    actual_cheap = {**actual, "cost_rs": 90_000.0}
    low = verify._bound(sites.full(sid), WEEKDAY, _series(outage=None), pr, 0.5, actual_cheap, 50.0, 300.0, c_next=40.0, v_end=30.0)
    assert low["solved_headroom_rs"] < 0 and low["headroom_rs"] == 0.0 and low["bound_rs"] == 300.0
    assert verify._bound(sites.full(sid), WEEKDAY, _series(outage=None), pr, 0.5, actual_cheap, 50.0, -200.0, c_next=40.0, v_end=30.0)["bound_rs"] == 0.0
    # a solve stopped at its time limit, or not solved, is no bound
    for status, limited in (("feasible", True), ("not_solved", False)):
        monkeypatch.setattr(verify.optimise, "solve", _hindsight(seen, status=status, limited=limited))
        none = verify._bound(sites.full(sid), WEEKDAY, _series(outage=None), pr, 0.5, actual, 50.0, 300.0, c_next=40.0, v_end=30.0)
        assert none["bound_rs"] is None and none["status"] == ("time_limited" if limited else "not_solved")


def test_a_days_bound_is_never_below_its_saving_nor_zero(company):
    # A monitor-tier grid site whose inverter ran its native settings (the actual arm is native), with outages and a manually started genset: the
    # audit read -Rs 6,328 for such a day at Lahore. The bound is the realised saving plus the hindsight plan's headroom over the actual arm
    sid = make_site(name="Bound real r7", tier="monitor", twin_extra={"windows": [["14:00", "15:30"]], "adherence": 1.0, "trips_per_day": 1.0})
    twin_day(sid, D, 0.5)
    n = verify.compute_day(sid, D, forward=False)["native"]
    pf = n["perfect_foresight"]
    assert pf["status"] == "optimal" and not pf["time_limited"], pf
    assert n["perfect_foresight_rs"] >= max(0.0, n["saving_rs"]) - 0.01
    assert n["perfect_foresight_rs"] == pytest.approx(max(0.0, n["saving_rs"] + pf["headroom_rs"]), abs=0.01)


def _pf_day(saving: float, bound: float | None, fidelity: str = "confirmed", new: bool = True, carry: float = 0.0) -> dict:
    """A day row with a bound: `carry` of its saving is its end gap at v_end (provisional while no later day is scored); `new` a bound by _bound."""
    r = _day(_lv(peak_window=saving - carry, carry_over=carry), fidelity=fidelity, end_carry=carry)
    r["perfect_foresight_rs"] = bound
    if new:
        r["perfect_foresight"] = {"bound_rs": bound, "headroom_rs": None if bound is None else bound - saving,
                                  "status": "optimal" if bound is not None else "time_limited"}
    return r


def test_the_periods_bound_is_held_against_the_claim_of_the_same_days(company):
    # Before: the summary added every day's bound, a native block's and a day that claims nothing's too (Faisalabad's 7 native block days against
    # 1 verified day), and a bound stored by the earlier method: here 27,877 - 760 + 1,400 = 28,517 against a claim of 1,200
    sid = make_site(name="Bound summary r7")
    _put_rows(sid, {"2026-10-05": {b: _pf_day(0.0, 27_877.0, fidelity="native_block") for b in ("native", "tuned")},
                    "2026-10-06": {b: _pf_day(800.0, None) for b in ("native", "tuned")},
                    "2026-10-07": {b: _pf_day(500.0, -760.0, new=False) for b in ("native", "tuned")},
                    # the last day: 300 of its 1,000 is carry-over no later day has used yet, out of the claim and out of the bound alike
                    "2026-10-08": {b: _pf_day(1_000.0, 1_400.0, carry=300.0) for b in ("native", "tuned")}})
    s = verify.summary(sid, "2026-10-05", "2026-10-08")
    n = s["baselines"]["native"]
    assert n["claimed_rs"] == 800.0 + 500.0 + 700.0 and n["carry_over_provisional_rs"] == 300.0
    assert n["perfect_foresight_rs"] == 1_100.0 and n["perfect_foresight_claimed_rs"] == 700.0
    assert n["perfect_foresight_days"] == 1 and n["perfect_foresight_missing_days"] == 2
    assert n["perfect_foresight_rs"] >= n["perfect_foresight_claimed_rs"]
    assert s["baselines"]["tuned"]["perfect_foresight_rs"] is None
    # a period with no claiming day that has a bound shows none
    assert verify.summary(sid, "2026-10-05", "2026-10-07")["baselines"]["native"]["perfect_foresight_rs"] is None
    # a claiming day that lost money: a planner with hindsight could have run the native settings, so the bound is 0, not the claim's -800
    _put_rows(sid, {"2026-10-10": {b: _pf_day(-500.0, 0.0, carry=300.0) for b in ("native", "tuned")}})
    low = verify.summary(sid, "2026-10-10", "2026-10-10")["baselines"]["native"]
    assert low["claimed_rs"] == -800.0 and low["perfect_foresight_claimed_rs"] == -800.0 and low["perfect_foresight_rs"] == 0.0


# ------------------------------------------------------------------------------------------------ pr-review-04 (and pr-review-09's legacy row):
# ------------------------------------------------------------------------------------------------ rows stored before round 6's chained replays

def _as_v1(sid: str, day: date) -> None:
    """A day's rows as v1-build stored them: no chain, the day's carry-over the whole end gap at v_end."""
    with store.connect() as con:
        for r in con.execute("SELECT baseline, data FROM savings_days WHERE site_id = ? AND day_local = ?", (sid, day.isoformat())).fetchall():
            d = json.loads(r["data"])
            d.pop("chain", None)
            d.pop("handover", None)
            con.execute("UPDATE savings_days SET data = ? WHERE site_id = ? AND day_local = ? AND baseline = ?",
                        (json.dumps(d), sid, day.isoformat(), r["baseline"]))


def test_rows_scored_by_the_earlier_method_are_named_until_recomputed_and_the_next_day_restarts_from_the_measured_state(company):
    # Before: a period mixed v1-build's whole-gap carry-over with chained days under one label, and nothing said so (chain_stale_days treats a row
    # with no chain as one that started from the actual arm, so no "Recompute needed" either)
    sid = make_site(name="Legacy rows r7")
    days3 = [P, D, D + timedelta(days=1)]
    soc = 0.5
    for i, d in enumerate(days3):
        _, sim, _ = twin_day(sid, d, soc, seed=i + 1)
        soc = float(sim["soc"][-1])
    for d in days3[:2]:
        verify.compute_day(sid, d, with_bound=False, forward=False)
        _as_v1(sid, d)
    span = (P.isoformat(), D.isoformat())
    s = verify.summary(sid, *span)
    assert s["earlier_method_days"] == [P.isoformat(), D.isoformat()] and s["chain_stale_days"] == []
    assert [x["earlier_method"] for x in s["days"]] == [True, True] and all(x["baseline_start"] is None for x in s["days"])
    # the first day scored after them restarts from the measured state: a row with no chain has no end state to carry on from
    third = verify.compute_day(sid, days3[2], with_bound=False, forward=False)["native"]
    assert third["chain"]["from"] == "actual" and third["chain"]["reset"] == "no_state" and third["chain"]["start"]["gap_kwh"] == 0.0
    s = verify.summary(sid, P.isoformat(), days3[2].isoformat())
    assert s["earlier_method_days"] == [P.isoformat(), D.isoformat()] and [x["earlier_method"] for x in s["days"]] == [True, True, False]
    # recomputed oldest first, the period is chained and named no more
    assert verify.compute_range(sid, P, days3[2]) == 3
    s = verify.summary(sid, P.isoformat(), days3[2].isoformat())
    assert s["earlier_method_days"] == [] and not any(x["earlier_method"] for x in s["days"]) and s["chain_stale_days"] == []
    assert [x["baseline_start"] for x in s["days"]] == ["actual", "previous_day", "previous_day"]


# ------------------------------------------------------------------------------------------------ pr-review-09: the paths round 6 left untested

def test_a_chained_days_bound_solves_from_the_actual_start_and_carries_the_chained_saving(company, monkeypatch):
    # Round 6 subtracted the chained start gap inside the bound (carry_start) with no test: flip its sign and the suite still passed. The bound is
    # now the day's saving, which carries the chain's start gap, plus the hindsight plan's headroom over the actual arm, solved from where the actual
    # arm started
    sid = _farm("Farm bound chained r7")
    _run(sid, [LEAD] + DAYS, _night_runs)
    verify.compute_day(sid, DAYS[0], with_bound=False, forward=False)
    seen: dict = {}
    real = verify.optimise.solve

    def spy(p, **kw):
        seen["p"] = p
        return real(p, **kw)

    monkeypatch.setattr(verify.optimise, "solve", spy)
    n = verify.compute_day(sid, DAYS[1], forward=False)["native"]
    ch = n["chain"]
    # a chained day whose native replay started emptier than the battery the plan's night run left: its start gap is charged in its saving
    assert ch["from"] == "previous_day" and ch["start"]["gap_kwh"] > 1.0 and ch["start"]["carry_rs"] > 1.0
    assert n["levers"]["carry_over"] == pytest.approx(ch["end"]["carry_rs"] - ch["start"]["carry_rs"], abs=0.01)
    # the hindsight solve starts where the actual arm started, not where the chained native replay did
    assert seen["p"]["battery"]["E0"] == pytest.approx(ch["start"]["actual_e_kwh"], abs=1e-6)
    assert abs(seen["p"]["battery"]["E0"] - ch["start"]["baseline_e_kwh"]) > 1.0
    pf = n["perfect_foresight"]
    assert pf["status"] == "optimal" and pf["headroom_rs"] >= 0.0
    assert n["perfect_foresight_rs"] == pytest.approx(max(0.0, n["saving_rs"] + pf["headroom_rs"]), abs=0.01)
    assert n["perfect_foresight_rs"] >= n["saving_rs"] - 0.01


def test_a_blank_grid_presence_on_a_day_that_claims_nothing_restarts_the_next_day(company):
    # _unread_mask's grid branch: at a grid site a blank grid presence is carried forward, so an outage there is unseen and native's end state is
    # not its own. It does not withhold the day (load and PV were read), but the day after one that claims nothing restarts from the measured state
    sid = make_site(name="Grid blank r7", tier="monitor")
    soc = 0.5
    for i, d in enumerate((P, D)):
        _, sim, _ = twin_day(sid, d, soc, seed=i + 1)
        soc = float(sim["soc"][-1])
    first = verify.compute_day(sid, P, with_bound=False, forward=False)["native"]
    assert first["fidelity"] == "recommendation_only" and first["chain"]["end"]["unread_min"] == 0
    assert verify.compute_day(sid, D, with_bound=False, forward=False)["native"]["chain"]["from"] == "previous_day"
    assert _blank_evening(sid, P, 10.0, 12.0, cols=("grid_present",)) == 120
    first = verify.compute_day(sid, P, with_bound=False, forward=False)["native"]
    assert first["chain"]["end"]["unread_min"] == 120 and first["inputs_unread_min"] == 0 and first["claim_withheld"] is None
    nxt = verify.compute_day(sid, D, with_bound=False, forward=False)["native"]
    assert nxt["chain"]["from"] == "actual" and nxt["chain"]["reset"] == "unread_inputs" and nxt["chain"]["start"]["gap_kwh"] == 0.0


def test_a_day_beyond_the_forward_walk_is_listed_stale_until_recomputed(company, monkeypatch):
    # FORWARD_MAX_DAYS: a re-scored day walks at most that many later days; one beyond keeps its old start and the summary names it
    sid = _farm("Farm walk r7")
    _run(sid, [LEAD] + DAYS, _night_runs)
    for d in DAYS:
        verify.compute_day(sid, d, with_bound=False, forward=False)
    span = (DAYS[0].isoformat(), DAYS[2].isoformat())
    assert verify.summary(sid, *span)["chain_stale_days"] == []
    # the first day's evening goes blank: it claims nothing over unread inputs, so the second day restarts and its end moves, and so the third's start
    monkeypatch.setattr(verify, "FORWARD_MAX_DAYS", 1)
    _blank_evening(sid, DAYS[0], 19.0, 23.0)
    verify.compute_day(sid, DAYS[0], with_bound=False)
    assert _rows(sid, DAYS[1])["native"]["chain"]["reset"] == "unread_inputs"  # within the walk: re-scored
    assert verify.summary(sid, *span)["chain_stale_days"] == [DAYS[2].isoformat()]  # beyond it: stale
    monkeypatch.setattr(verify, "FORWARD_MAX_DAYS", 62)
    verify.compute_day(sid, DAYS[1], with_bound=False)
    assert verify.summary(sid, *span)["chain_stale_days"] == []
    assert _rows(sid, DAYS[2])["native"]["chain"]["start"]["state"] == _rows(sid, DAYS[1])["native"]["chain"]["end"]["state"]


# ------------------------------------------------------------------------------------------------ pages-D-04 (security-roles-05,
# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-10): the pilot's request model

CRITERIA = {"min_saving_pct": 8.0, "max_unserved_kwh": 0.0, "reserve_adequacy_pct": 90.0, "fidelity_pct": 80.0, "fee_multiple": 3.0,
            "forecast_pv_nrmse_max": 0.35}


def _pilot_ready(w) -> str:
    """The world's mill at the advisory-writes tier with a native capture and a switchback calendar painted a month ahead, so its pilot can start
    once a baseline window is chosen (`_start` chooses one where none is; audit flow-savings-pilot-bill-11, pages-D-11)."""
    _tier(w, w.site, "advisory_writes")
    with w.use():
        sites.capture_native(w.site, {"source": "entered", "work_mode": "self_use"}, "t")
        verify.plan_switchback(w.site, date.today() + timedelta(days=30), 2, 7, "t", seed=1)
    return w.site


def _tier(w, sid: str, tier: str) -> None:
    """The site's effective control tier: what its source can do and what an administrator allows, both set to `tier`."""
    with w.use():
        sites.set_tier_capable(sid, tier)
        sites.set_tier_allowed(sid, tier, "t")


def _start(w, sid: str, name: str = "Plant manager"):
    """Start the pilot; a pilot that has neither started nor a baseline window gets one first (tomorrow, 14 days), since the start needs it."""
    admin = w.c["admin"]
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    if not p["started"] and not p["pilot"]["baseline_start"]:
        assert admin.put(f"/api/sites/{sid}/pilot", json={"baseline_start": (_pkt_today() + timedelta(days=1)).isoformat()}).status_code == 200
    return admin.post(f"/api/sites/{sid}/pilot/start", json={"name": name})


def test_the_pilot_put_takes_only_the_set_ups_own_fields_typed_and_in_range(world):
    sid = _pilot_ready(world)
    admin = world.c["admin"]
    fresh = admin.get(f"/api/sites/{sid}/pilot").json()["pilot"]
    for body in ({"signoff": {"by": "someone else", "at": "2026-01-01", "name": "X"}}, {"amendments": [{"at": "2026-01-01", "by": "x", "changes": []}]},
                 {"criteria": "abc"}, {"criteria": None}, {"baseline_days": -5}, {"baseline_days": 1000}, {"baseline_days": 14.5},
                 {"baseline_start": 12345}, {"baseline_start": "2026-02-30"}, {"baseline_start": "20261120"},
                 {"criteria": {"min_saving_pct": "lots"}}, {"criteria": {"foo": -1}}, {"criteria": {"min_saving_pct": 101}},
                 {"criteria": {"fidelity_pct": None}}, {"bogus_field": 1}):
        r = admin.put(f"/api/sites/{sid}/pilot", json=body)
        assert r.status_code == 422 and r.json()["code"] == "invalid", (body, r.status_code, r.text)
        assert "\n" not in r.json()["detail"] and "pydantic" not in r.json()["detail"], r.text  # one sentence, no stack
    assert admin.get(f"/api/sites/{sid}/pilot").json()["pilot"] == fresh
    r = admin.put(f"/api/sites/{sid}/pilot", json={"baseline_start": "2026-11-20", "baseline_days": 14})
    assert r.status_code == 200 and (r.json()["pilot"]["baseline_start"], r.json()["pilot"]["baseline_days"]) == ("2026-11-20", 14)
    # one criterion changes alone; the others keep their values
    r = admin.put(f"/api/sites/{sid}/pilot", json={"criteria": {"min_saving_pct": 5}})
    assert r.status_code == 200 and r.json()["pilot"]["criteria"] == {**CRITERIA, "min_saving_pct": 5.0}
    # the sign-off is the start's alone, and stays as the start wrote it
    assert _start(world, sid).status_code == 200
    signed = admin.get(f"/api/sites/{sid}/pilot").json()["pilot"]["signoff"]
    assert signed["name"] == "Plant manager" and signed["by"]
    for body in ({"signoff": {"by": "Customer CEO", "at": "2026-10-01T00:00:00+00:00", "name": "CEO"}}, {"signoff": None}, {"amendments": []}):
        assert admin.put(f"/api/sites/{sid}/pilot", json=body).status_code == 422
    after = admin.get(f"/api/sites/{sid}/pilot").json()["pilot"]
    assert after["signoff"] == signed and after["amendments"] == []


def test_a_pilot_record_stored_with_bad_criteria_reads_back_as_an_object(world):
    # a row the old PUT stored with criteria null (or text) no longer reaches the page as such: it read p.pilot.criteria[key] and crashed
    sid = _pilot_ready(world)
    for bad_criteria in (None, "abc", {"min_saving_pct": "lots", "fidelity_pct": 70}):
        with world.use(), store.connect() as con:
            con.execute("INSERT INTO pilots (site_id, data, updated) VALUES (?,?,?) ON CONFLICT(site_id) DO UPDATE SET data = excluded.data",
                        (sid, json.dumps({"baseline_start": None, "baseline_days": 14, "criteria": bad_criteria, "signoff": None, "amendments": []}),
                         "2026-11-01T00:00:00+00:00"))
        got = world.c["admin"].get(f"/api/sites/{sid}/pilot").json()["pilot"]["criteria"]
        assert got == {**CRITERIA, **({"fidelity_pct": 70.0} if isinstance(bad_criteria, dict) else {})}


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-30, pages-D-17: an amendment
# ------------------------------------------------------------------------------------------------ says what changed from what to what, and why

def _audit_lines(w, action: str) -> list[dict]:
    with w.use():
        return [a for a in store.list_audit() if a["action"] == action]


def test_an_amendment_records_its_reason_and_each_changed_fields_old_and_new_value_with_an_audit_line(world):
    sid = _pilot_ready(world)
    admin = world.c["admin"]
    # before the start a change is no amendment, but it is audited with its values before and after
    r = admin.put(f"/api/sites/{sid}/pilot", json={"baseline_start": "2026-11-20", "baseline_days": 14, "criteria": CRITERIA})
    assert r.status_code == 200 and r.json()["pilot"]["amendments"] == []
    (line,) = _audit_lines(world, "changed the pilot set-up")
    assert line["target"] == sid and line["detail"]["values"] == [{"field": "baseline_start", "before": None, "after": "2026-11-20"}]
    assert _start(world, sid).status_code == 200
    # after it, a change needs a reason, and a save that changes nothing records nothing (every save sends the whole criteria)
    r = admin.put(f"/api/sites/{sid}/pilot", json={"criteria": {"min_saving_pct": 5}})
    assert r.status_code == 422 and r.json()["code"] == "reason_needed"
    r = admin.put(f"/api/sites/{sid}/pilot", json={"criteria": CRITERIA, "baseline_days": 14, "reason": "Saved again"})
    assert r.status_code == 422 and r.json()["code"] == "no_change"
    r = admin.put(f"/api/sites/{sid}/pilot", json={"criteria": {**CRITERIA, "min_saving_pct": 5}, "reason": "  The customer accepted a lower bar  "})
    assert r.status_code == 200
    (am,) = r.json()["pilot"]["amendments"]
    assert am["reason"] == "The customer accepted a lower bar" and am["by"] and am["at"]
    assert am["changes"] == ["criteria.min_saving_pct"] and am["values"] == [{"field": "criteria.min_saving_pct", "before": 8.0, "after": 5.0}]
    (line,) = _audit_lines(world, "amended the pilot")
    assert line["detail"] == {"reason": "The customer accepted a lower bar", "values": am["values"]}
    r = admin.put(f"/api/sites/{sid}/pilot", json={"reason": "x" * 501, "baseline_days": 21})
    assert r.status_code == 422


def test_a_role_without_the_fee_reads_no_fee_multiple_in_the_pilot_nor_in_its_amendments(world):
    sid = _pilot_ready(world)
    admin = world.c["admin"]
    assert _start(world, sid).status_code == 200
    assert admin.put(f"/api/sites/{sid}/pilot", json={"criteria": {"fee_multiple": 2.5}, "reason": "Fee renegotiated"}).status_code == 200
    assert admin.get(f"/api/sites/{sid}/pilot").json()["pilot"]["criteria"]["fee_multiple"] == 2.5
    for role in ("operator", "viewer"):
        p = world.c[role].get(f"/api/sites/{sid}/pilot").json()["pilot"]
        assert "fee_multiple" not in p["criteria"], role
        (am,) = p["amendments"]
        assert am["values"] == [] and am["changes"] == [] and am["reason"] == "Fee renegotiated", role
    assert world.c["analyst"].get(f"/api/sites/{sid}/pilot").json()["pilot"]["amendments"][0]["values"][0]["after"] == 2.5


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-09: one start

def test_a_second_start_is_refused_and_the_start_and_sign_off_stand(world):
    sid = _pilot_ready(world)
    admin = world.c["admin"]
    assert admin.post(f"/api/sites/{sid}/pilot/start", json={}).status_code == 422  # the sign-off names someone
    assert admin.post(f"/api/sites/{sid}/pilot/start", json={"name": "X", "by": "someone"}).status_code == 422
    first = _start(world, sid, "AUDIT Owner").json()
    assert first["started"] and first["pilot"]["signoff"]["name"] == "AUDIT Owner"
    r = _start(world, sid, "Someone Else")
    assert r.status_code == 409 and r.json()["code"] == "already_started"
    now = admin.get(f"/api/sites/{sid}/pilot").json()
    assert now["started"] == first["started"] and now["pilot"]["signoff"] == first["pilot"]["signoff"]
    assert len(_audit_lines(world, "started the pilot")) == 1


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-12: the pilot's native
# ------------------------------------------------------------------------------------------------ capture is pinned at the start

def _pkt_today() -> date:
    return datetime.now(timezone(timedelta(hours=5))).date()


def test_the_start_pins_the_capture_a_later_one_waits_for_an_amendment_and_verify_replays_the_pinned_one(world):
    sid = _pilot_ready(world)
    admin, operator = world.c["admin"], world.c["operator"]
    today = _pkt_today()
    p = _start(world, sid).json()
    assert p["capture"]["version"] == 1 and p["capture_newer"] is None and p["pilot"]["capture_pins"] == [{"version": 1, "from": today.isoformat()}]
    # the operator records a new capture after the start: it is kept, shown as not adopted, and the pilot still replays version 1
    r = operator.post(f"/api/sites/{sid}/native", json={"source": "entered", "work_mode": "backup", "backup_soc_pct": 90})
    assert r.status_code == 200 and r.json()["version"] == 2
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    assert p["capture"]["version"] == 1 and p["capture_newer"]["version"] == 2 and p["pilot"]["amendments"] == []
    with world.use():
        assert verify.native_for_day(sid, today)["version"] == 1
        assert verify.native_for_day(sid, today + timedelta(days=20))["version"] == 1
        conn = sites.connections(sid)[0]
        before = verify._month_fingerprint(sid, conn, today.replace(day=1), today)
    # only an amendment with a reason adopts it, from the day it was captured; an older or unknown version, or the operator, cannot
    assert operator.put(f"/api/sites/{sid}/pilot", json={"capture_version": 2, "reason": "x"}).status_code == 403
    assert admin.put(f"/api/sites/{sid}/pilot", json={"capture_version": 2}).json()["code"] == "reason_needed"
    for v in (1, 3, 0):
        assert admin.put(f"/api/sites/{sid}/pilot", json={"capture_version": v, "reason": "x"}).status_code == 422, v
    r = admin.put(f"/api/sites/{sid}/pilot", json={"capture_version": 2, "reason": "The installer set backup mode at 90 %"})
    assert r.status_code == 200
    p = r.json()
    assert p["capture"]["version"] == 2 and p["capture_newer"] is None
    assert p["pilot"]["capture_pins"] == [{"version": 1, "from": today.isoformat()}, {"version": 2, "from": today.isoformat()}]
    (am,) = p["pilot"]["amendments"]
    assert am["values"] == [{"field": "capture_version", "before": 1, "after": 2}] and am["reason"] == "The installer set backup mode at 90 %"
    assert _audit_lines(world, "amended the pilot")[0]["detail"]["values"] == am["values"]
    with world.use():
        assert verify.native_for_day(sid, today)["version"] == 2
        # the month's fingerprint moves with the pins, so the nightly settle re-scores the days the adopted capture now stands for
        assert verify._month_fingerprint(sid, conn, today.replace(day=1), today) != before


def test_a_capture_is_adopted_only_by_a_started_pilot_and_a_pilot_started_before_pins_replays_the_capture_of_its_start(world):
    sid = _pilot_ready(world)
    admin = world.c["admin"]
    r = admin.put(f"/api/sites/{sid}/pilot", json={"capture_version": 1})
    assert r.status_code == 422 and r.json()["code"] == "not_started"
    # a pilot started before the start pinned its capture: the capture latest at its start stands for every day from it
    with world.use():
        sites.capture_native(sid, {"source": "entered", "work_mode": "backup", "backup_soc_pct": 90}, "t")
        with store.connect() as con:
            con.execute("UPDATE native_settings SET captured_at = ? WHERE site_id = ? AND version = 1", ("2026-10-01T00:00:00+00:00", sid))
            con.execute("UPDATE native_settings SET captured_at = ? WHERE site_id = ? AND version = 2", ("2026-10-20T08:00:00+00:00", sid))
            con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?)",
                        (sid, json.dumps({"baseline_start": None, "baseline_days": 14, "criteria": CRITERIA, "signoff": None, "amendments": []}),
                         "2026-10-10T20:00:00+00:00", "2026-10-10T20:00:00+00:00"))
        assert verify.pilot_capture_pins(sid) == [{"version": 1, "from": "2026-10-11"}]  # 20:00 UTC is 01:00 the next day in Pakistan
        assert verify.native_for_day(sid, date(2026, 10, 25))["version"] == 1
        assert verify.native_for_day(sid, date(2026, 10, 5))["version"] == 1  # before the start: the capture in force that day, as before
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    assert p["capture"]["version"] == 1 and p["capture_newer"]["version"] == 2
    # with no pilot, a day takes the capture in force by its end (plan 8.1, 8.4), unchanged
    with world.use():
        with store.connect() as con:
            con.execute("DELETE FROM pilots WHERE site_id = ?", (sid,))
        assert verify.native_for_day(sid, date(2026, 10, 25))["version"] == 2


# ------------------------------------------------------------------------------------------------ pages-D-19: tab A's completeness check and KPI list,
# ------------------------------------------------------------------------------------------------ tab C's declared holiday closures

def test_the_pilot_answers_the_baseline_windows_completeness_so_far_and_the_kpis_it_measures(world):
    sid, admin = world.site, world.c["admin"]
    assert admin.get(f"/api/sites/{sid}/pilot").json()["baseline_quality"] is None  # no window chosen yet
    today = _pkt_today()
    start = today - timedelta(days=4)
    assert admin.put(f"/api/sites/{sid}/pilot", json={"baseline_start": start.isoformat(), "baseline_days": 14}).status_code == 200
    with world.use(), store.connect() as con:
        for i, c in ((0, 1.0), (1, 0.5), (3, 0.97), (4, 0.2)):  # day 2 has no data; day 4 is today, not over yet
            con.execute("INSERT INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                        (sid, (start + timedelta(days=i)).isoformat(), json.dumps({"completeness": c, "import_kwh": 10.0})))
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    q = p["baseline_quality"]
    assert (q["from"], q["to"], q["through"]) == (start.isoformat(), (start + timedelta(days=13)).isoformat(), (today - timedelta(days=1)).isoformat())
    assert (q["days_so_far"], q["days_with_data"], q["days_complete"], q["day_complete_share"]) == (4, 3, 2, 0.95)
    assert q["completeness"] == round((1.0 + 0.5 + 0.0 + 0.97) / 4, 4)
    assert [d["completeness"] for d in q["days"]] == [1.0, 0.5, None, 0.97]
    # the mill is on the grid with a battery and a genset, and no solar
    assert p["kpis"] == ["import_kwh", "peak_import_kwh", "peak_kw", "outage_hours", "load_kwh", "throughput_kwh", "min_soc", "genset_hours",
                         "litres", "unserved_kwh"]
    assert admin.get(f"/api/sites/{world.other}/pilot").json()["kpis"] == ["load_kwh", "unserved_kwh"]
    # a window that starts tomorrow has no day so far
    assert admin.put(f"/api/sites/{sid}/pilot", json={"baseline_start": (today + timedelta(days=1)).isoformat()}).status_code == 200
    q = admin.get(f"/api/sites/{sid}/pilot").json()["baseline_quality"]
    assert (q["days_so_far"], q["completeness"], q["through"], q["days"]) == (0, None, None, [])


def test_holiday_closures_are_declared_in_the_pilot_typed_and_amended_with_a_reason(world):
    sid = _pilot_ready(world)
    admin = world.c["admin"]
    # after the calendar (a month ahead, four weeks long), since a closure under a block holds the start (audit flow-savings-pilot-bill-11)
    day = lambda n: (_pkt_today() + timedelta(days=n)).isoformat()  # noqa: E731
    eid = {"start": day(130), "end": day(133), "label": "Eid closure"}
    xmas = {"start": day(70), "end": day(72), "label": "Year-end shutdown"}
    for bad in ([{"start": "2026-12-26", "end": "2026-12-24"}], [{"start": "2026-12-01", "end": "2027-03-01"}], [{"start": "x", "end": "2026-12-24"}],
                [{**xmas, "kind": "eid"}], [{**xmas, "label": "y" * 81}], [xmas] * 21, None, "Eid"):
        assert admin.put(f"/api/sites/{sid}/pilot", json={"closures": bad}).status_code == 422, bad
    r = admin.put(f"/api/sites/{sid}/pilot", json={"closures": [eid, {"start": xmas["start"], "end": xmas["end"]}]})
    assert r.status_code == 200 and r.json()["pilot"]["closures"] == [{**xmas, "label": ""}, eid]  # kept in date order
    assert _start(world, sid).status_code == 200
    r = admin.put(f"/api/sites/{sid}/pilot", json={"closures": [xmas, eid], "reason": "The year-end shutdown was named"})
    assert r.status_code == 200
    (am,) = r.json()["pilot"]["amendments"]
    assert am["values"] == [{"field": "closures", "before": [{**xmas, "label": ""}, eid], "after": [xmas, eid]}]


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-11 (pages-D-18): the start's
# ------------------------------------------------------------------------------------------------ gates: tier, capture, baseline window, calendar, rules

def _gates(p: dict) -> dict:
    return {g["id"]: g["ok"] for g in p["start_gates"]}


def _checks(p: dict) -> dict:
    return {c["id"]: c for c in p["calendar_checks"]}


def test_the_pilot_starts_only_past_its_gates_and_says_which_one_holds_it(world):
    sid, admin = world.site, world.c["admin"]
    today = _pkt_today()
    start = lambda: admin.post(f"/api/sites/{sid}/pilot/start", json={"name": "Plant manager"})  # noqa: E731
    with world.use():
        sites.capture_native(sid, {"source": "entered", "work_mode": "self_use"}, "t")
        verify.plan_switchback(sid, today + timedelta(days=30), 2, 7, "t", seed=1)
    # the world's mill is at the monitor tier, and no baseline window is chosen: the gates say so and the start is refused, tier first
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    assert _gates(p) == {"tier": False, "capture": True, "baseline": False, "calendar": True, "rules": False}
    assert p["start_gates"][-1]["failing"] == ["tier-length"]  # no block length runs at the monitor tier
    r = start()
    assert r.status_code == 422 and r.json()["code"] == "monitor_tier", r.text
    _tier(world, sid, "advisory_writes")
    r = start()
    assert r.status_code == 422 and r.json()["code"] == "no_baseline", r.text
    # a baseline window that runs into the calendar: the rule check fails, the gate names it and the start is refused
    assert admin.put(f"/api/sites/{sid}/pilot", json={"baseline_start": (today + timedelta(days=20)).isoformat()}).status_code == 200
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    after = _checks(p)["after-baseline"]
    assert after["ok"] is False and (after["baseline_end"], after["first_day"]) == ((today + timedelta(days=33)).isoformat(),
                                                                                   (today + timedelta(days=30)).isoformat())
    assert _gates(p)["rules"] is False and p["start_gates"][-1]["failing"] == ["after-baseline"]
    r = start()
    assert r.status_code == 422 and r.json()["code"] == "calendar_rules" and "after the baseline window" in r.json()["detail"], r.text
    # a calendar that began yesterday (the engine paints it; the API refuses such a start): the 'future' check fails (before: started on it)
    assert admin.put(f"/api/sites/{sid}/pilot", json={"baseline_start": (today - timedelta(days=40)).isoformat()}).status_code == 200
    with world.use():
        verify.plan_switchback(sid, today - timedelta(days=1), 2, 7, "t", seed=1)
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    fut = _checks(p)["future"]
    assert fut["ok"] is False and (fut["first_day"], fut["today"]) == ((today - timedelta(days=1)).isoformat(), today.isoformat())
    r = start()
    assert r.status_code == 422 and r.json()["code"] == "calendar_rules" and "after today" in r.json()["detail"], r.text
    # a declared holiday closure under a block fails too
    with world.use():
        verify.plan_switchback(sid, today + timedelta(days=2), 2, 7, "t", seed=1)
    eid = {"start": (today + timedelta(days=3)).isoformat(), "end": (today + timedelta(days=4)).isoformat(), "label": "Eid closure"}
    assert admin.put(f"/api/sites/{sid}/pilot", json={"closures": [eid]}).status_code == 200
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    assert _checks(p)["closures"]["ok"] is False and _checks(p)["closures"]["hits"] == [eid]
    assert start().json()["code"] == "calendar_rules"
    # nothing refused started anything or locked the calendar
    with world.use():
        assert not any(b["locked"] for b in verify.switchback_blocks(sid))
    assert admin.get(f"/api/sites/{sid}/pilot").json()["started"] is None and _audit_lines(world, "started the pilot") == []
    # every gate met: it starts
    assert admin.put(f"/api/sites/{sid}/pilot", json={"closures": []}).status_code == 200
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    assert all(_gates(p).values()) and all(c["ok"] for c in p["calendar_checks"])
    assert {"balanced", "paired", "joined", "length", "tier-length", "enough", "after-baseline", "future"} == set(_checks(p))
    r = start()
    assert r.status_code == 200 and r.json()["started"], r.text


# ------------------------------------------------------------------------------------------------ pages-D-11: a monitor-tier site has no switchback

def test_a_monitor_tier_site_paints_no_calendar_and_the_pilot_says_its_tier(world):
    sid, admin, operator = world.site, world.c["admin"], world.c["operator"]
    tomorrow = (_pkt_today() + timedelta(days=1)).isoformat()
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    assert (p["tier"], p["block_days_allowed"], p["calendar"]) == ("monitor", [], None)
    for c in (admin, operator):
        r = c.put(f"/api/sites/{sid}/switchback", json={"start": tomorrow, "fortnights": 2, "block_days": 7})
        assert r.status_code == 422 and r.json()["code"] == "monitor_tier", r.text
    with world.use():
        assert verify.switchback_blocks(sid) == []
    _tier(world, sid, "advisory_writes")
    r = operator.put(f"/api/sites/{sid}/switchback", json={"start": tomorrow, "fortnights": 2, "block_days": 7})
    assert r.status_code == 200 and len(r.json()) == 4
    assert world.c["viewer"].put(f"/api/sites/{sid}/switchback", json={"start": tomorrow}).status_code == 403


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-25: 3-day blocks at the
# ------------------------------------------------------------------------------------------------ closed-loop tier only

def test_three_day_blocks_are_painted_at_the_closed_loop_tier_only(world):
    sid, admin, operator = world.site, world.c["admin"], world.c["operator"]
    tomorrow = _pkt_today() + timedelta(days=1)
    paint = lambda **kw: operator.put(f"/api/sites/{sid}/switchback", json={"start": tomorrow.isoformat(), "fortnights": 2, **kw})  # noqa: E731
    _tier(world, sid, "advisory_writes")
    assert admin.get(f"/api/sites/{sid}/pilot").json()["block_days_allowed"] == [7]
    r = paint(block_days=3)
    assert r.status_code == 422 and "7 days" in r.json()["detail"], r.text
    for bad in ({"block_days": 5}, {"block_days": "7"}, {"fortnights": 0}, {"fortnights": 27}, {"bogus": 1}):
        r = paint(**bad)
        assert r.status_code == 422 and r.json()["code"] == "invalid", (bad, r.text)
    assert paint(block_days=7).status_code == 200
    _tier(world, sid, "closed_loop")
    assert admin.get(f"/api/sites/{sid}/pilot").json()["block_days_allowed"] == [3, 7]
    r = paint(block_days=3)
    assert r.status_code == 200
    day = lambda n: (tomorrow + timedelta(days=n)).isoformat()  # noqa: E731
    assert [(b["block_start"], b["block_end"]) for b in r.json()][:2] == [(day(0), day(2)), (day(3), day(5))]


def test_a_calendars_checks_name_its_pair_and_hold_a_block_length_the_tier_does_not_run(world):
    sid, admin, operator = _pilot_ready(world), world.c["admin"], world.c["operator"]
    tomorrow = (_pkt_today() + timedelta(days=1)).isoformat()
    assert admin.put(f"/api/sites/{sid}/pilot", json={"baseline_start": (_pkt_today() - timedelta(days=30)).isoformat()}).status_code == 200
    assert _checks(admin.get(f"/api/sites/{sid}/pilot").json())["paired"]["unit"] == "fortnight"
    _tier(world, sid, "closed_loop")
    assert operator.put(f"/api/sites/{sid}/switchback", json={"start": tomorrow, "fortnights": 2, "block_days": 3}).status_code == 200
    p = admin.get(f"/api/sites/{sid}/pilot").json()
    assert _checks(p)["paired"]["unit"] == "pair" and _checks(p)["tier-length"]["ok"] is True and all(_gates(p).values())
    # the tier lowered under a calendar of 3-day blocks: its tier-length check fails, so the start is refused
    _tier(world, sid, "advisory_writes")
    tl = _checks(admin.get(f"/api/sites/{sid}/pilot").json())["tier-length"]
    assert (tl["ok"], tl["lengths"], tl["allowed"], tl["tier"]) == (False, [3], [7], "advisory_writes")
    r = _start(world, sid)
    assert r.status_code == 422 and r.json()["code"] == "calendar_rules" and "a block length the control tier runs" in r.json()["detail"], r.text


# ------------------------------------------------------------------------------------------------ security-roles-06: the draw, the start after today,
# ------------------------------------------------------------------------------------------------ and only blocks painted before they began count

def test_the_calendar_is_drawn_from_os_entropy_after_today_stored_audited_and_only_blocks_painted_ahead_count(world, monkeypatch):
    sid, admin, operator = world.site, world.c["admin"], world.c["operator"]
    _tier(world, sid, "advisory_writes")
    today = _pkt_today()
    for start in (today.isoformat(), (today - timedelta(days=30)).isoformat(), "2026-02-30", None, 20261120):
        r = operator.put(f"/api/sites/{sid}/switchback", json={"start": start, "fortnights": 4, "block_days": 7})
        assert r.status_code == 422 and r.json()["code"] == "invalid", (start, r.text)
    with world.use():
        assert verify.switchback_blocks(sid) == []
    # the arms come from the operating system's entropy, never a generator seeded by the clock
    bits = iter([1, 0, 0, 1, 0, 0, 0, 0])
    monkeypatch.setattr(verify.secrets, "randbits", lambda k: next(bits))
    monkeypatch.setattr(verify.np.random, "default_rng", lambda *a, **k: pytest.fail("a seeded generator drew the arms"))
    tomorrow = today + timedelta(days=1)
    r = operator.put(f"/api/sites/{sid}/switchback", json={"start": tomorrow.isoformat(), "fortnights": 4, "block_days": 7})
    assert r.status_code == 200, r.text
    arms = ["plan", "native", "native", "plan", "native", "plan", "plan", "native"]
    assert [b["arm"] for b in r.json()] == arms
    cal = admin.get(f"/api/sites/{sid}/pilot").json()["calendar"]
    assert (cal["draw"], cal["painted_on"], cal["paint_count"], cal["pairs"], cal["block_days"]) == ("os_entropy", today.isoformat(), 1, 4, 7)
    assert cal["by"] and cal["painted_at"]
    (line,) = _audit_lines(world, "painted the switchback calendar")
    assert line["detail"]["arms"] == arms and line["detail"]["draw"] == "os_entropy" and line["detail"]["start"] == tomorrow.isoformat()
    # a repaint before the start is counted and audited with its own arms
    r = operator.put(f"/api/sites/{sid}/switchback", json={"start": tomorrow.isoformat(), "fortnights": 2, "block_days": 7})
    assert [b["arm"] for b in r.json()] == ["native", "plan", "native", "plan"]
    assert admin.get(f"/api/sites/{sid}/pilot").json()["calendar"]["paint_count"] == 2
    assert [a["detail"]["arms"] for a in _audit_lines(world, "painted the switchback calendar")] in ([arms, ["native", "plan", "native", "plan"]],
                                                                                                   [["native", "plan", "native", "plan"], arms])
    monkeypatch.undo()
    with world.use():
        assert all(b["painted_before_start"] for b in verify.switchback_blocks(sid))
        # eight weeks painted after they began (backdated): none counts toward the eight weeks, so nothing loses its Preliminary for them
        verify.plan_switchback(sid, today - timedelta(days=70), 4, 7, "t", seed=3)
        blocks = verify.switchback_blocks(sid)
        assert len(blocks) == 8 and not any(b["painted_before_start"] for b in blocks)
        assert verify._switchback_weeks(blocks, today) == 0 and verify.switchback_result(sid)["switchback_weeks"] == 0
        assert verify.paint_record(sid)["draw"] == "seeded"
        # the same eight weeks painted the day before they began count (before: any block that had ended counted, painted when it may)
        verify.plan_switchback(sid, today - timedelta(days=70), 4, 7, "t", seed=3, painted_on=today - timedelta(days=71))
        assert verify._switchback_weeks(verify.switchback_blocks(sid), today) == 8
        # a block the recorded paint did not draw (written some other way) counts for nothing
        with store.connect() as con:
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,0)",
                        (sid, (today - timedelta(days=14)).isoformat(), (today - timedelta(days=8)).isoformat(), "plan"))
        blocks = verify.switchback_blocks(sid)
        assert [b["painted_before_start"] for b in blocks][-1] is False and verify._switchback_weeks(blocks, today) == 8


def test_a_calendar_locked_before_the_paint_record_counts_the_blocks_that_began_after_the_pilots_start(world):
    """A real pilot started before round 7 has no paint record: the blocks its start locked that began after the start's day were painted before
    they began (the paint precedes the lock), so they keep counting towards the 8 weeks; the rest, and any unlocked block, count for nothing."""
    sid, today = world.site, _pkt_today()
    began = today - timedelta(days=60)
    with world.use(), store.connect() as con:
        for k in range(8):  # the first block began the week before the start, the second on the start's day (the start came after its midnight)
            a = began - timedelta(days=7) + timedelta(days=7 * k)
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,1)",
                        (sid, a.isoformat(), (a + timedelta(days=6)).isoformat(), "plan" if k % 2 else "native"))
        con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?)",
                    (sid, "{}", f"{began.isoformat()}T05:00:00+00:00", f"{began.isoformat()}T05:00:00+00:00"))
    with world.use():
        assert verify.paint_record(sid) is None
        blocks = verify.switchback_blocks(sid)
        assert [b["painted_before_start"] for b in blocks] == [False, False] + [True] * 6
        assert verify._switchback_weeks(blocks, today) == 6
        with store.connect() as con:
            con.execute("UPDATE switchback SET locked = 0 WHERE site_id = ?", (sid,))
        assert not any(b["painted_before_start"] for b in verify.switchback_blocks(sid))


# ------------------------------------------------------------------------------------------------ security-roles-14 (flow-savings-pilot-bill-26): the
# ------------------------------------------------------------------------------------------------ calendar file escapes text, stamps events, names itself

def test_the_calendar_file_escapes_the_site_name_stamps_each_event_folds_long_lines_and_names_itself(world):
    import re
    sid = world.site
    name = "Mill\r\nBEGIN:VALARM\r\nACTION:DISPLAY; call, now\\END:VALARM\x07"
    with world.use():
        verify.plan_switchback(sid, _pkt_today() + timedelta(days=30), 1, 7, "t", seed=1)
        with store.connect() as con:
            con.execute("UPDATE sites SET name = ? WHERE id = ?", (name, sid))
    r = world.c["viewer"].get(f"/api/sites/{sid}/pilot/calendar.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert r.headers["content-disposition"] == f'attachment; filename="{sid}-switchback.ics"'
    raw = r.content.decode("utf-8")
    assert raw.endswith("\r\n") and "\r" not in raw.replace("\r\n", "") and "\n" not in raw.replace("\r\n", "")  # CRLF lines alone
    assert all(len(line.encode()) <= 75 for line in raw.split("\r\n"))
    lines = raw.replace("\r\n ", "").split("\r\n")[:-1]  # unfolded
    assert not any(line.startswith(("BEGIN:VALARM", "ACTION:", "END:VALARM")) for line in lines)
    assert lines.count("BEGIN:VEVENT") == 2 and lines.count("END:VEVENT") == 2
    summaries = [line for line in lines if line.startswith("SUMMARY:")]
    assert summaries[0] in ("SUMMARY:Mill\\nBEGIN:VALARM\\nACTION:DISPLAY\\; call\\, now\\\\END:VALARM: plan block",
                            "SUMMARY:Mill\\nBEGIN:VALARM\\nACTION:DISPLAY\\; call\\, now\\\\END:VALARM: native settings block")
    stamps = [line for line in lines if line.startswith("DTSTAMP:")]
    assert len(stamps) == 2 and all(re.fullmatch(r"DTSTAMP:\d{8}T\d{6}Z", s) for s in stamps)
    # a long name in Urdu folds at whole characters
    long = "ملک " * 30
    with world.use(), store.connect() as con:
        con.execute("UPDATE sites SET name = ? WHERE id = ?", (long.strip(), sid))
    raw = world.c["viewer"].get(f"/api/sites/{sid}/pilot/calendar.ics").content.decode("utf-8")
    assert all(len(line.encode()) <= 75 for line in raw.split("\r\n"))
    assert any(line == f"SUMMARY:{long.strip()}: plan block" for line in raw.replace("\r\n ", "").split("\r\n"))
