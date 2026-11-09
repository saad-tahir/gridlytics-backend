"""Round-7 regression tests for the savings engine (verify.py), from the end-to-end audit: which days claim, and what the headline sums.

Each test fails on the code before its fix. Test data: the demo twin's generated days and hand-made day rows only; nothing here calls the network."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from test_fix_r2_savings import NIGHT_CHARGER, D, P
from test_fix_r6_savings import DAYS, LEAD, _blank_evening, _farm, _night_runs, _run
from test_fix_savings import _put_rows, company, make_site, twin_day  # noqa: F401 (company is a fixture)

from gridlytics import sites, store, telemetry, twin, verify


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
