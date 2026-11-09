"""Round-7 regression tests for the savings engine (verify.py), from the end-to-end audit: which days claim, and what the headline sums.

Each test fails on the code before its fix. Test data: the demo twin's generated days and hand-made day rows only; nothing here calls the network."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from test_fix_r2_savings import NIGHT_CHARGER, D, P
from test_fix_r6_savings import DAYS, LEAD, _blank_evening, _continuous, _farm, _night_runs, _run
from test_fix_savings import _put_rows, company, make_site, twin_day  # noqa: F401 (company is a fixture)

from gridlytics import sites, store, tariffs, telemetry, twin, verify


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
