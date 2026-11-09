"""Round-6 regression tests for the savings group (verify.py): HANDOFF 8.A.1, the off-grid per-day ledger against a continuous replay.

Each day's native and tuned replays started from the actual arm's state at 00:00 and the day's carry-over valued the whole end-of-day gap in stored
energy, so at the demo farm the per-day ledger summed over 11 days missed a continuous replay by -6.2k to +7.9k Rs. Each baseline's replays are now
chained from one scored day to the next, and a day's carry-over is the change of the valued gap over the day. Each test fails on the code before its
fix. Test data: the demo twin's generated days only; nothing here calls the network."""
from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import pytest
from test_fix_r3_savings import SHIFTING
from test_fix_savings import company, make_site, standing_plan  # noqa: F401 (company is a fixture)

from gridlytics import demo, optimise, sites, store, tariffs, telemetry, twin, verify

LEAD = date(2026, 10, 12)  # an unscored day before the first scored one
DAYS = [LEAD + timedelta(days=i) for i in range(1, 4)]


def _farm(name: str) -> str:
    """The demo farm (off-grid, 120 kWh, a 48 kW set under manual start) with a fixed id: the twin seeds its days by the id."""
    spec = next(s for s in demo.DEMO_SITES if s["key"] == "farm")
    s = sites.create({**spec["site"], "name": name}, "t", site_id="t-" + name.lower().replace(" ", "-"), demo_link=demo.DEMO_LINK)
    sid = s["id"]
    with store.connect() as con:
        con.execute("UPDATE sites SET data = ?, status = 'active' WHERE id = ?", (json.dumps({**s["data"], **spec["extra"]}), sid))
    for role, fields in spec["assets"].items():
        sites.put_asset(sid, role, fields, "t")
    sites.set_tier_capable(sid, "closed_loop")
    sites.set_tier_allowed(sid, spec["tier"], "t", "demo")
    sites.capture_native(sid, spec["native"], "t")
    standing_plan(sid)
    return sid


def _night_runs(site, ts):
    """A plan's genset runs 21:00-01:00 every night (a run across midnight; its minimum-load surplus charges the battery), the operator's rescue
    rule outside them."""
    pol = twin.native_policy(site, ts)
    minute = ((np.asarray(ts) + 5 * 3600) % 86400) // 60
    pol["genset"] = np.where((minute >= 21 * 60) | (minute < 60), 1, 0)
    return pol


def _run(sid: str, days, policy, soc0: float = 0.5) -> None:
    """Twin days written as telemetry, the plant's state carried from one day into the next as the live twin carries it."""
    site = sites.full(sid)
    plant = twin.plant_of(site)
    st = (soc0, False, 0, 0, False)
    for i, day in enumerate(days):
        tr = twin.truth(site, day)
        pol = policy(site, tr["ts"]) if callable(policy) else policy[i](site, tr["ts"])
        sim = twin.simulate(tr, pol, plant, st[0], st[1], gen_elapsed0=st[2], outage_min0=st[3], gen_rescue0=st[4])
        telemetry.write_rows(twin.telemetry_rows(sid, tr, sim, rng_seed=i + 1, inverter_mode=pol["mode"]))
        st = (float(sim["soc"][-1]), bool(sim["gen_on_end"]), int(sim["gen_elapsed_end"]), int(sim["outage_min_end"]), bool(sim["gen_rescue_end"]))


def _genset_rs(site: dict, sim: dict, day: date, on_before: bool) -> float:
    """The genset's cost over a day as a continuous replay counts it: a run going on before the day's first minute is no start."""
    g = (site.get("assets") or {}).get("genset")
    if not g:
        return 0.0
    on = np.asarray(sim["gen_on"], dtype=bool)
    starts = int(np.sum(on[1:] & ~on[:-1])) + int(bool(on[0]) and not on_before)
    return (float(np.sum(sim["fuel"])) * tariffs.fuel_price(g["fuel_kind"], day)[0] + starts * g["c_start_rs"]
            + float(on.sum()) / 60 * g["c_runhr_rs"] + float(np.sum(sim["gen"])) / 60 * g["c_om_rs_per_kwh"])


def _cost(site: dict, sim: dict, pr: dict | None, day: date, on_before: bool) -> float:
    """An arm's cost over a day as a continuous replay counts it: energy at the minute's prices, the genset and the battery's wear."""
    out = _genset_rs(site, sim, day, on_before)
    if pr is not None:
        out += float(np.sum(pr["c_imp"] * sim["imp"]) - np.sum(pr["c_exp"] * sim["exp"])) / 60
    b = (site.get("assets") or {}).get("battery")
    if b:
        out += float(np.sum(np.maximum(sim["bat"], 0.0))) / 60 * optimise.wear_cost(b)
    return out


def _running_before(sid: str, day: date) -> bool:
    g = telemetry.minute_grid(sid, twin.day_start_utc(day) - 60, twin.day_start_utc(day))
    return bool(np.nan_to_num(g["gen_on"][-1]) > 0.5)


def _continuous(sid: str, days, v_end: float, soc0: float | None = None, state: dict | None = None) -> float:
    """The native capture replayed straight through `days` against the measured days: the difference in cost plus the end-of-period gap in stored
    energy at v_end. The replay starts from the actual arm's state at the first day's start, or from `state` (a carried twin state) less the
    start gap valued at `v_start`."""
    site = sites.full(sid)
    site["native"] = verify.native_for_day(sid, days[0])
    plant = twin.plant_of(site)
    conn = (site.get("connections") or [None])[0]
    ins = [verify._replay_inputs(site, d) for d in days]
    cat = {k: np.concatenate([s[k] for s, _ in ins]) for k in ("ts", "pv", "load", "crit", "grid", "temp")}
    a0 = ins[0][1]
    s_act = verify._soc_start(sid, twin.day_start_utc(days[0]), a0["g"], a0["f"], plant)
    gen0 = bool(np.nan_to_num(a0["g"]["gen_on"][0]) > 0.5)
    before = _running_before(sid, days[0])
    if state is None:
        nat = twin.simulate(cat, twin.native_policy(site, cat["ts"]), plant, s_act, gen0)
        nb, start_charge = before, 0.0
    else:
        s = state["state"]
        nat = twin.simulate(cat, twin.native_policy(site, cat["ts"]), plant, s["soc"], s["gen_on"], gen_elapsed0=s["gen_elapsed"],
                            outage_min0=s["outage_min"], gen_rescue0=s["gen_rescue"])
        nb, start_charge = bool(s["gen_on"]), state["v"] * (s_act - s["soc"]) * plant.e_nom
    on_grid = conn is not None and conn["data"].get("regime") != "off_grid"
    total, ab = 0.0, before
    for i, (d, (_, a)) in enumerate(zip(days, ins)):
        part = {k: np.asarray(nat[k])[1440 * i:1440 * (i + 1)] for k in ("imp", "exp", "gen_on", "fuel", "gen", "bat")}
        act = verify._actual_sim(a["f"], a["g"], plant if (site.get("assets") or {}).get("genset") else None)
        pr = verify._minute_prices(conn, d, sid) if on_grid else None
        total += _cost(site, part, pr, d, nb) - _cost(site, act, pr, d, ab)
        nb, ab = bool(part["gen_on"][-1]), bool(act["gen_on"][-1])
    last = ins[-1][1]
    e_a = verify._soc_end(last["g"], last["f"], plant) * plant.e_nom
    return total + (e_a - float(nat["soc"][-1]) * plant.e_nom) * v_end - start_charge


def _rows(sid: str, day: date) -> dict:
    with store.connect() as con:
        return {r["baseline"]: json.loads(r["data"]) for r in
                con.execute("SELECT baseline, data FROM savings_days WHERE site_id = ? AND day_local = ?", (sid, day.isoformat()))}


# ------------------------------------------------------------------------------------------------ HANDOFF 8.A.1: the daily ledger adds up

def test_an_off_grid_farms_daily_ledger_adds_up_to_one_continuous_replay(company):
    # the plan's night runs leave the battery fuller at midnight than the operator's rule does, so native's own state at 00:00 is not the actual
    # arm's. Before: each day's native replay started from the actual arm's state and the day's carry-over valued the whole end gap, so the three
    # days missed the continuous replay
    sid = _farm("Farm chain r6")
    _run(sid, [LEAD] + DAYS, _night_runs)
    rows = [verify.compute_day(sid, d, with_bound=False) for d in DAYS]
    v_end = rows[-1]["native"]["actual"]["v_end"]
    cont = _continuous(sid, DAYS, v_end)
    ledger = sum(r["native"]["saving_rs"] for r in rows)
    assert ledger == pytest.approx(cont, abs=3.0), (ledger, cont)
    # the chain carries a real gap: native's own state at the second and third midnight is not the actual arm's
    for r in rows[1:]:
        ch = r["native"]["chain"]
        assert ch["from"] == "previous_day" and abs(ch["start"]["gap_kwh"]) > 5.0
        assert r["native"]["levers"]["carry_over"] == pytest.approx(ch["end"]["carry_rs"] - ch["start"]["carry_rs"], abs=0.01)
    # each baseline carries its own state: the tuned replay starts where the tuned replay of the day before ended
    for a, b in zip(rows, rows[1:]):
        for base in ("native", "tuned"):
            assert b[base]["chain"]["start"]["state"] == a[base]["chain"]["end"]["state"]
    # the summary of the three days is the continuous replay too, and no day is out of step with the one before
    s = verify.summary(sid, DAYS[0].isoformat(), DAYS[-1].isoformat())
    assert s["baselines"]["native"]["saving_rs"] == pytest.approx(cont, abs=3.0) and s["chain_stale_days"] == []
    assert [d["baseline_start"] for d in s["days"]] == ["actual", "previous_day", "previous_day"]


def test_a_grid_sites_daily_ledger_adds_up_to_one_continuous_replay(company):
    # a grid-connected site running a shifting table (empty by midnight) against a backup capture (held full from PV). Before: native restarted
    # each day from the table's empty battery, refilled from PV, and every day's carry-over charged the whole gap again
    # (a battery native fills from PV by noon: while it is not full, its gain over a day does not depend on where it started)
    sid = make_site(name="Grid chain r6", genset=False, native={"source": "entered", "work_mode": "backup", "backup_soc_pct": 20}, load_peak_kw=120,
                    battery={"nameplate_kwh": 120, "p_ch_kw": 60, "p_dis_kw": 60})
    _run(sid, [LEAD] + DAYS, lambda s, ts: twin.table_policy(SHIFTING, ts))
    rows = [verify.compute_day(sid, d, with_bound=False) for d in DAYS]
    cont = _continuous(sid, DAYS, rows[-1]["native"]["actual"]["v_end"])
    ledger = sum(r["native"]["saving_rs"] for r in rows)
    assert ledger == pytest.approx(cont, abs=3.0), (ledger, cont)  # before: -7,371 against -4,571
    assert abs(rows[1]["native"]["chain"]["start"]["gap_kwh"]) > 50.0


# ------------------------------------------------------------------------------------------------ recomputing one day reads the chain

def test_recomputing_one_day_reads_the_day_befores_end_state_and_re_scores_the_days_after(company):
    sid = _farm("Farm recompute r6")
    _run(sid, [LEAD] + DAYS, _night_runs)
    first = [verify.compute_day(sid, d, with_bound=False) for d in DAYS]
    cont = _continuous(sid, DAYS, first[-1]["native"]["actual"]["v_end"])
    # POST /savings/recompute (and the nightly job) score one day alone: it must start from the stored end state of the day before
    assert verify.compute_range(sid, DAYS[1], DAYS[1]) == 1
    total = sum(_rows(sid, d)["native"]["saving_rs"] for d in DAYS)
    assert total == pytest.approx(cont, abs=3.0), (total, cont)  # before: the recomputed day started from the actual arm's state again
    again = _rows(sid, DAYS[1])["native"]
    assert again["chain"]["start"]["state"] == first[0]["native"]["chain"]["end"]["state"]
    assert again["saving_rs"] == pytest.approx(first[1]["native"]["saving_rs"], abs=0.01)
    # the first day's rows go (it can no longer be scored): the second day restarts from the actual arm's state, and the third, whose chain
    # started from the second's old end, is re-scored from its new end
    with store.connect() as con:
        con.execute("DELETE FROM savings_days WHERE site_id = ? AND day_local = ?", (sid, DAYS[0].isoformat()))
    verify.compute_range(sid, DAYS[1], DAYS[1])
    second, third = _rows(sid, DAYS[1])["native"], _rows(sid, DAYS[2])["native"]
    assert second["chain"]["from"] == "actual" and third["chain"]["start"]["state"] == second["chain"]["end"]["state"]
    cont2 = _continuous(sid, DAYS[1:], third["actual"]["v_end"])
    assert second["saving_rs"] + third["saving_rs"] == pytest.approx(cont2, abs=3.0)
    assert verify.summary(sid, DAYS[1].isoformat(), DAYS[2].isoformat())["chain_stale_days"] == []
    # a day scored without re-scoring the days after it leaves them out of step, and the summary names them: first the day that restarted for
    # want of it, then the day whose start moved
    span = (DAYS[0].isoformat(), DAYS[2].isoformat())
    verify.compute_day(sid, DAYS[0], with_bound=False, forward=False)
    assert verify.summary(sid, *span)["chain_stale_days"] == [DAYS[1].isoformat()]
    verify.compute_day(sid, DAYS[1], with_bound=False, forward=False)
    assert verify.summary(sid, *span)["chain_stale_days"] == [DAYS[2].isoformat()]
    verify.compute_day(sid, DAYS[0], with_bound=False)  # forward: re-scores the days after it while their start has moved
    assert verify.summary(sid, *span)["chain_stale_days"] == []
    total = sum(_rows(sid, d)["native"]["saving_rs"] for d in DAYS)
    assert total == pytest.approx(cont, abs=3.0)


# ------------------------------------------------------------------------------------------------ the first day of a period

def test_the_first_day_of_a_chain_starts_from_the_actual_arm_and_bills_a_run_across_midnight_no_start(company):
    sid = _farm("Farm first r6")
    _run(sid, [LEAD] + DAYS, _night_runs)
    assert _running_before(sid, DAYS[0])  # the plan's run of the night before goes on past midnight
    r = verify.compute_day(sid, DAYS[0], with_bound=False)["native"]
    site = sites.full(sid)
    plant = twin.plant_of(site)
    # the run going on at midnight is no start (before: the actual arm paid a start, Rs 1,500, at 00:00 every day it ran across)
    _, act = verify._replay_inputs(site, DAYS[0])
    a = verify._actual_sim(act["f"], act["g"], plant)
    assert bool(a["gen_on"][0])
    assert r["actual"]["genset_rs"] == pytest.approx(_genset_rs(site, a, DAYS[0], True), abs=0.05)
    # the first scored day: native starts where the actual arm was, and the carry-over is the whole end gap at v_end, as before
    ch = r["chain"]
    assert ch["from"] == "actual" and ch["reset"] == "first_day" and ch["start"]["gap_kwh"] == 0.0
    assert ch["start"]["state"]["soc"] * plant.e_nom == pytest.approx(ch["start"]["actual_e_kwh"]) and ch["start"]["state"]["gen_on"] is True
    assert r["levers"]["carry_over"] == pytest.approx((r["actual"]["e_end_kwh"] - r["baseline"]["e_end_kwh"]) * r["actual"]["v_end"], abs=1.0)
    # after a day that was not scored the chain restarts from the actual arm's state
    third = verify.compute_day(sid, DAYS[2], with_bound=False)["native"]
    assert third["chain"]["from"] == "actual" and third["chain"]["reset"] == "gap"
    # a period may start on a chained day: its total is the continuous replay from native's carried state, less the start gap at the day before's
    # value (the energy native was already short of, credited on the day before)
    verify.compute_day(sid, DAYS[1], with_bound=False)  # re-scores the third day from the second's end (forward)
    second = _rows(sid, DAYS[1])["native"]
    assert _rows(sid, DAYS[2])["native"]["chain"]["from"] == "previous_day"
    st = {"state": second["chain"]["start"]["state"], "v": second["chain"]["start"]["v_rs_per_kwh"]}
    cont = _continuous(sid, DAYS[1:], _rows(sid, DAYS[2])["native"]["actual"]["v_end"], state=st)
    s = verify.summary(sid, DAYS[1].isoformat(), DAYS[2].isoformat())["baselines"]["native"]
    assert s["saving_rs"] == pytest.approx(cont, abs=3.0), (s["saving_rs"], cont)


# ------------------------------------------------------------------------------------------------ switchback blocks keep their rules

def test_a_native_blocks_first_day_restarts_the_chain_and_claims_nothing(company):
    sid = _farm("Farm switch r6")
    _run(sid, [LEAD] + DAYS, [_night_runs, _night_runs, _night_runs, lambda s, ts: twin.native_policy(s, ts)])
    with store.connect() as con:
        for a, b, arm in ((DAYS[0], DAYS[1], "plan"), (DAYS[2], DAYS[2], "native")):
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,1)", (sid, a.isoformat(), b.isoformat(), arm))
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    assert rows[1]["chain"]["from"] == "previous_day" and rows[1]["claimed_rs"] == rows[1]["saving_rs"]
    # the native block replays native from the state the plan block handed it, and claims nothing
    assert rows[2]["chain"]["from"] == "actual" and rows[2]["chain"]["reset"] == "switchback"
    assert rows[2]["fidelity"] == "native_block" and rows[2]["claimed_rs"] == 0.0
    # the hand-over credit (plan 8.2) is the plan block's end gap against native run over the whole block
    credit = verify._carry_over_credits(verify.switchback_blocks(sid), {d.isoformat(): r for d, r in zip(DAYS, rows)})
    assert len(credit) == 1 and credit[0]["e_end_native_kwh"] == pytest.approx(rows[1]["chain"]["end"]["baseline_e_kwh"], abs=0.01)


# ------------------------------------------------------------------------------------------------ a day that claims nothing restarts the chain

def _blank_evening(sid: str, day: date, h0: float = 19.0, h1: float = 23.0, cols=("load_power_w", "genset_running", "genset_power_w", "genset_fuel_l")) -> int:
    """A telemetry dropout: columns blank from h0 to h1 (local) on a day; by default the load and the genset's three, as the verifier's farm s7 case."""
    t0 = twin.day_start_utc(day)
    with telemetry.connect() as con:
        return con.execute(f"UPDATE minute SET {', '.join(c + ' = NULL' for c in cols)} WHERE site_id = ? AND ts >= ? AND ts < ?",
                           (sid, int(t0 + h0 * 3600), int(t0 + h1 * 3600))).rowcount


def test_a_dropout_on_a_withheld_day_cannot_move_another_days_claim(company):
    # Before (chained through every day): the withheld day's native replay ran on a blank load (filled as zero), so it ended fuller than native's
    # own state; the next day, whose data is complete, started native there, and its claim moved with what the blank hid (farm s7: +1,140 Rs on
    # two such days). Now a day after one that claims nothing over unread inputs restarts from the actual arm's measured state
    sid = _farm("Farm withheld r6")
    _run(sid, [LEAD] + DAYS, _night_runs)
    full = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    assert all(r["claim_withheld"] is None and r["claimed_rs"] == r["saving_rs"] and r["chain"]["end"]["unread_min"] == 0 for r in full)
    claims = []
    for h0 in (21.0, 19.0):  # a short dropout, then a longer one over the same evening
        assert _blank_evening(sid, DAYS[1], h0, 23.0) == int((23.0 - h0) * 60)
        rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
        assert rows[1]["claim_withheld"] == "genset_unknown" and rows[1]["claimed_rs"] == 0.0
        assert rows[1]["chain"]["end"]["unread_min"] == int((23.0 - h0) * 60)
        assert rows[2]["chain"]["from"] == "actual" and rows[2]["chain"]["reset"] == "unread_inputs" and rows[2]["chain"]["start"]["gap_kwh"] == 0.0
        assert rows[0]["claimed_rs"] == pytest.approx(full[0]["claimed_rs"], abs=0.01)
        claims.append(rows[2]["claimed_rs"])
    # the withheld day's replay served no load over the blank, so it ended fuller than native's own state; the next day's claim does not move
    assert rows[1]["chain"]["end"]["baseline_e_kwh"] > full[1]["chain"]["end"]["baseline_e_kwh"] + 10.0
    assert claims[0] == pytest.approx(claims[1], abs=0.01), claims
    # it is the claim the next day makes with the withheld day not scored at all (a gap): it rests on nothing the blank hid
    with store.connect() as con:
        con.execute("DELETE FROM savings_days WHERE site_id = ? AND day_local = ?", (sid, DAYS[1].isoformat()))
    gap = verify.compute_day(sid, DAYS[2], with_bound=False, forward=False)["native"]
    assert gap["chain"]["reset"] == "gap" and gap["claimed_rs"] == pytest.approx(claims[1], abs=0.01)
    # the withheld day itself chains from the claiming day before it, and the restart after it is not stale
    verify.compute_day(sid, DAYS[1], with_bound=False)
    assert _rows(sid, DAYS[1])["native"]["chain"]["from"] == "previous_day"
    assert verify.summary(sid, DAYS[0].isoformat(), DAYS[2].isoformat())["chain_stale_days"] == []


def test_a_day_withheld_on_a_read_load_keeps_the_chain_and_the_days_around_it_claim_what_they_claim_on_complete_data(company):
    # the battery's and the genset's columns blank for an evening: the day is withheld (its battery flow is unknown), but its load and PV were read,
    # so native's replay of it is native's own and the next day carries on from it
    sid = _farm("Farm genset blank r6")
    _run(sid, [LEAD] + DAYS, _night_runs)
    full = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    cols = ("battery_power_w", "battery_soc_est_pct", "battery_soc_bms_pct", "genset_running", "genset_power_w", "genset_fuel_l")
    assert _blank_evening(sid, DAYS[1], cols=cols) == 240
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    assert rows[1]["claim_withheld"] == "battery_flow_unknown" and rows[1]["claimed_rs"] == 0.0 and rows[1]["chain"]["end"]["unread_min"] == 0
    assert rows[1]["chain"]["end"]["state"] == full[1]["chain"]["end"]["state"]
    assert rows[2]["chain"]["from"] == "previous_day"
    for r, f in ((rows[0], full[0]), (rows[2], full[2])):
        assert r["claimed_rs"] == pytest.approx(f["claimed_rs"], abs=0.01)


def test_a_day_withheld_on_read_inputs_keeps_the_chain_and_a_later_dropout_on_it_is_stale(company):
    # a day that claims nothing on fully read load and PV keeps the chain when the plan ran on it: here a day withheld for its battery's and genset's
    # data alone (round 7, audit pr-review-03: a day the plan did not run, a monitor day or a table not applied, now restarts the next plan day's
    # chain from the measured state; test_fix_r7_savings_engine)
    sid = _farm("Farm unclaimed r6")
    _run(sid, [LEAD] + DAYS, _night_runs)
    ref = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    cols = ("battery_power_w", "battery_soc_est_pct", "battery_soc_bms_pct", "genset_running", "genset_power_w", "genset_fuel_l")
    assert _blank_evening(sid, DAYS[0], cols=cols) == 240
    first = verify.compute_day(sid, DAYS[0], with_bound=False)["native"]
    assert first["claim_withheld"] == "battery_flow_unknown" and first["claimed_rs"] == 0.0 and first["chain"]["end"]["unread_min"] == 0
    rows = [_rows(sid, d)["native"] for d in DAYS[1:]]
    assert rows[0]["chain"]["from"] == "previous_day" and rows[0]["chain"]["start"]["state"] == first["chain"]["end"]["state"]
    for r, f in zip(rows, ref[1:]):
        assert r["claimed_rs"] == pytest.approx(f["claimed_rs"], abs=0.01)
    span = (DAYS[0].isoformat(), DAYS[2].isoformat())
    assert verify.summary(sid, *span)["chain_stale_days"] == []
    # a dropout found later on the withheld day, re-scored on its own: the second day still chains from it, so the summary names it
    assert _blank_evening(sid, DAYS[0], 20.0, 22.0) == 120
    verify.compute_day(sid, DAYS[0], with_bound=False, forward=False)
    assert verify.summary(sid, *span)["chain_stale_days"] == [DAYS[1].isoformat()]
    verify.compute_range(sid, DAYS[0], DAYS[0])  # forward: the second day restarts from the measured state
    assert _rows(sid, DAYS[1])["native"]["chain"]["reset"] == "unread_inputs"
    assert verify.summary(sid, *span)["chain_stale_days"] == []


# ------------------------------------------------------------------------------------------------ a switchback site is held to the segmented replay

def test_a_switchback_plan_block_claims_its_own_continuous_replay_with_the_hand_over_at_what_the_native_block_realised(company):
    # plan 8.2: each block's baseline is native from the block's measured start, and the plan block's last day credits the energy it hands to the
    # native block at v_end. So a switchback site's stored claims are the segmented replay (the plan block straight through, then its end gap at
    # v_end). Round 7 (audit brief-vs-build-11): once the native block is over, the period's claim takes the hand-over at what the native block
    # realised with it, as native run straight through both blocks counts it (test_fix_r7_savings_engine)
    sid = _farm("Farm segmented r6")
    _run(sid, [LEAD] + DAYS, [_night_runs, _night_runs, _night_runs, lambda s, ts: twin.native_policy(s, ts)])
    with store.connect() as con:
        for a, b, arm in ((DAYS[0], DAYS[1], "plan"), (DAYS[2], DAYS[2], "native")):
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,1)", (sid, a.isoformat(), b.isoformat(), arm))
    rows = [verify.compute_day(sid, d, with_bound=False)["native"] for d in DAYS]
    seg = _continuous(sid, DAYS[:2], rows[1]["actual"]["v_end"])
    assert rows[0]["claimed_rs"] + rows[1]["claimed_rs"] == pytest.approx(seg, abs=3.0), (rows[0]["claimed_rs"] + rows[1]["claimed_rs"], seg)
    credit = verify._carry_over_credits(verify.switchback_blocks(sid), {d.isoformat(): r for d, r in zip(DAYS, rows)})
    assert credit[0]["credit_v_end_rs"] == pytest.approx(rows[1]["chain"]["end"]["carry_rs"], abs=1.0)  # the credit reads the rounded energies
    # the native block replays native from the state it was handed: the inverter ran native, so it scores about nothing and claims nothing
    assert rows[2]["chain"]["reset"] == "switchback" and rows[2]["claimed_rs"] == 0.0
    assert abs(rows[2]["saving_rs"]) < 0.02 * abs(credit[0]["credit_v_end_rs"]) + 5.0, (rows[2]["saving_rs"], credit)
    # round 7: the native block is over, so the hand-over is what it realised: the claim is native straight through both blocks, less the native
    # block's own (unclaimed) score. Before: the segmented replay, the hand-over at v_end
    assert credit[0]["realised"] and credit[0]["credit_rs"] == pytest.approx(rows[1]["chain"]["end"]["carry_rs"] + rows[2]["handover"]["rs"], abs=0.02)
    pure = _continuous(sid, DAYS, rows[2]["actual"]["v_end"])
    s = verify.summary(sid, DAYS[0].isoformat(), DAYS[2].isoformat())
    assert s["baselines"]["native"]["claimed_rs"] == pytest.approx(pure - rows[2]["saving_rs"], abs=3.0) and s["chain_stale_days"] == []
