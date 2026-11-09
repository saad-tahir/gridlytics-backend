"""Regression tests of the API group's round-2 items (6 November 2026): the CSV upload refuses a repeated column, an operator cannot reset money settings
by deleting and re-adding equipment, one genset run holds one request however its start moves, edge event batches are stored once (concurrent sends and
retries), a telemetry batch with an impossible ts is refused deterministically, an outage event re-solves the plan, resume re-solves, reports tolerate
missing day values, the overview falls back to the BMS state of charge, the prices page shows the ICP prices the ledger uses, and pending genset requests
are withdrawn while no plan is in force.

Each test works in a data folder of its own (the `world` fixture of test_fix_api.py: GRIDLYTICS_DATA_DIR and a company file in tmp_path)."""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import alerts, plans, reports, routes_edge, sites, store, telemetry, twin, verify
from test_fix_api import PKT, _batch, _genset_plan, _now, world  # noqa: F401  (world is a fixture)


def _events(seq: int, events: list[dict]) -> dict:
    return {"contract": "ems-edge/1.0", "seq": seq, "sent_at": "2026-11-06T10:00:00Z", "events": events}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------------ security-csv-upload-blocks-event-loop

def test_a_csv_header_that_names_a_column_twice_is_refused(world):
    sid = world.site
    op = world.c["operator"]
    headers = {"Content-Type": "text/csv"}
    # the window check reads the first ts column and the import (by name) the last: 2026 in the first, 1971 and 1974 in the second
    sneaky = "ts,grid_present,ts\n2026-11-01T00:00:00Z,1,1971-01-02T00:00:00Z\n2026-11-01T00:01:00Z,1,1974-01-02T00:00:00Z\n"
    r = op.post(f"/api/sites/{sid}/telemetry/upload", content=sneaky, headers=headers)
    assert r.status_code == 422 and "more than once" in r.json()["detail"] and "ts" in r.json()["detail"], r.text
    r = op.post(f"/api/sites/{sid}/telemetry/upload", content="ts,grid_present,grid_present\n2026-11-01T00:00:00Z,1,0\n", headers=headers)
    assert r.status_code == 422 and "grid_present" in r.json()["detail"]
    with world.use():
        assert telemetry.latest(sid) is None and telemetry.first_ts(sid) is None
        with store.connect() as con:
            assert con.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0
    # blank header cells (trailing commas from a spreadsheet) name no column, so they are not refused
    ok = "ts,grid_present,,\n2026-11-01T00:00:00Z,1,,\n2026-11-01T00:01:00Z,1,,\n"
    r = op.post(f"/api/sites/{sid}/telemetry/upload", content=ok, headers=headers)
    assert r.status_code == 200 and r.json()["rows"] == 2, r.text


# ------------------------------------------------------------------------------------------------ security-operator-edits-money-settings

def test_an_operator_cannot_reset_money_settings_by_deleting_the_equipment(world):
    sid = world.site
    admin, operator = world.c["admin"], world.c["operator"]
    pack = {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}
    # equipment at its default money settings: an operator may remove it
    assert operator.delete(f"/api/sites/{sid}/assets/genset").status_code == 200
    assert admin.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 150, "c_start_rs": 9000}).status_code == 200
    assert admin.put(f"/api/sites/{sid}/assets/battery", json={**pack, "replacement_cost_per_kwh": 90000, "warranted_cycles": 3000}).status_code == 200
    # an administrator's money settings: deleting (and re-adding with defaults) is refused
    assert operator.delete(f"/api/sites/{sid}/assets/battery").status_code == 403
    assert operator.delete(f"/api/sites/{sid}/assets/genset").status_code == 403
    r = operator.put(f"/api/sites/{sid}/assets/battery", json=pack)          # re-saving without the money fields keeps them
    assert r.status_code == 200 and r.json()["assets"]["battery"]["replacement_cost_per_kwh"] == 90000
    with world.use():
        a = sites.assets(sid)
        assert a["battery"]["replacement_cost_per_kwh"] == 90000 and a["battery"]["warranted_cycles"] == 3000
        assert a["genset"]["c_start_rs"] == 9000
    # other equipment, and an administrator, are unaffected
    assert operator.put(f"/api/sites/{sid}/assets/pv", json={"kwp_dc": 100, "kw_ac": 80}).status_code == 200
    assert operator.delete(f"/api/sites/{sid}/assets/pv").status_code == 200
    assert admin.delete(f"/api/sites/{sid}/assets/genset").status_code == 200
    with world.use():
        assert "genset" not in sites.assets(sid)


# ------------------------------------------------------------------------------------------------ data-genset-request-every-tick

def _requests(w, sid):
    with w.use():
        pend = [r for r in alerts.requests_for(sid) if r["status"] == "pending"]
        open_ = [a for a in alerts.listing("active", sid) if a["rule"].startswith("genset_request_")]
    return pend, open_


def test_a_run_whose_start_moves_earlier_holds_one_request(world):
    sid = world.site
    t0 = _now() // 900 * 900
    now = _now()
    with world.use():
        site = sites.full(sid)
        telemetry.ingest(sid, [{"ts": now - 60, "grid_present": 1, "genset_running": 0}], "edge")
        for first in (4, 3, 2):                                # the run's start moves t0+3600, t0+2700, t0+1800 over three re-solves
            alerts.genset_requests(site, _genset_plan(t0, range(first, 7)), now)
    pend, open_ = _requests(world, sid)
    assert len(pend) == 1 and len(open_) == 1, (pend, open_)
    assert pend[0]["payload"]["start"] == t0 + 1800               # each step earlier withdrew the request and asked at the new start (round 3)
    with world.use():                                          # a later, separate run (not overlapping the first) gets a request of its own
        alerts.genset_requests(site, _genset_plan(t0, list(range(2, 7)) + [8, 9]), now)
    pend, open_ = _requests(world, sid)
    assert sorted(p["payload"]["start"] for p in pend) == [t0 + 1800, t0 + 7200]
    assert len(open_) == 2


def test_a_run_whose_start_moves_earlier_is_asked_for_at_its_new_start(world):
    """regression-genset-request-keeps-later-start: a run at steps [6, 8) re-planned as [1, 8) kept only the request for the old, later start, so a
    manual-start operator would start the set 75 minutes late."""
    sid = world.site
    t0 = _now() // 900 * 900
    now = _now()
    with world.use():
        site = sites.full(sid)
        telemetry.ingest(sid, [{"ts": now - 60, "grid_present": 1, "genset_running": 0}], "edge")
        alerts.genset_requests(site, _genset_plan(t0, range(6, 8)), now)
        assert [r["payload"]["start"] for r in alerts.requests_for(sid)] == [t0 + 5400]
        alerts.genset_requests(site, _genset_plan(t0, range(1, 8)), now)
    pend, open_ = _requests(world, sid)
    assert [p["payload"]["start"] for p in pend] == [t0 + 900], pend
    assert len(open_) == 1 and open_[0]["rule"] == f"genset_request_{t0 + 900}"
    new_local = datetime.fromtimestamp(t0 + 900, PKT).strftime("%H:%M")
    assert f"at {new_local}" in open_[0]["title"]
    with world.use():
        assert {r["payload"]["start"]: r["status"] for r in alerts.requests_for(sid)} == {t0 + 5400: "withdrawn", t0 + 900: "pending"}
        with store.connect() as con:
            old = con.execute("SELECT state, note FROM alerts WHERE rule = ?", (f"genset_request_{t0 + 5400}",)).fetchone()
        assert old["state"] == "resolved" and old["note"] == f"Moved earlier to {new_local}"
        # the start moving later, or earlier by less than a step, keeps the one request (the operator starts early, never late)
        alerts.genset_requests(site, _genset_plan(t0, range(2, 8)), now)
        alerts.genset_requests(site, _genset_plan(t0, range(1, 8)), now)
    pend, open_ = _requests(world, sid)
    assert [p["payload"]["start"] for p in pend] == [t0 + 900] and len(open_) == 1
    with world.use():                                          # an answered request is not asked again, wherever the start moves
        alerts.answer_request(sid, pend[0]["id"], "not_now", "operator@fix.test")
        alerts.genset_requests(site, _genset_plan(t0, range(0, 8)), now)
        assert [r["status"] for r in alerts.requests_for(sid)].count("pending") == 0


def test_pending_genset_requests_are_withdrawn_while_no_plan_is_in_force(world):
    sid = world.site
    t0 = _now() // 900 * 900
    now = _now()
    with world.use():
        site = sites.full(sid)
        telemetry.ingest(sid, [{"ts": now - 60, "grid_present": 1, "genset_running": 0}], "edge")
        alerts.genset_requests(site, _genset_plan(t0, range(4, 8)), now)
        assert len(_requests(world, sid)[0]) == 1
        # control is paused: the pre-pause plan (still current if the paused tick failed) asks for nothing
        until = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat()
        sites.add_override(sid, {"kind": "pause_control", "until": until}, "test")
        alerts.genset_requests(site, _genset_plan(t0, range(4, 8)), now)
    pend, open_ = _requests(world, sid)
    assert pend == [] and open_ == []
    with world.use():
        assert [r["status"] for r in alerts.requests_for(sid)] == ["withdrawn"]
        with store.connect() as con:
            assert con.execute("SELECT note FROM alerts WHERE rule LIKE 'genset_request_%'").fetchone()["note"] == "Control is paused"
        for o in sites.overrides(sid):
            sites.clear_override(sid, o["id"], "test")
        # a new request, then no plan at all (none in force): withdrawn too
        alerts.genset_requests(site, _genset_plan(t0, range(5, 8)), now)
        assert len(_requests(world, sid)[0]) == 1
        alerts.genset_requests(site, None, now)
    assert _requests(world, sid) == ([], [])


# ------------------------------------------------------------------------------------------------ regression-edge-events-duplicated

def _event_rows(w, sid):
    with w.use(), store.connect() as con:
        n = con.execute("SELECT COUNT(*) FROM events WHERE site_id = ? AND source = 'edge'", (sid,)).fetchone()[0]
        p = con.execute("SELECT COUNT(*) FROM overrides WHERE site_id = ? AND kind = 'pause_control'", (sid,)).fetchone()[0]
    return n, p


def test_two_identical_event_batches_sent_at_once_are_stored_once(world, monkeypatch):
    sid = world.site
    kh = world.key(["telemetry:write"])
    real = store.audit

    def slow(actor, action, *a, **k):                         # widens the window between the events' write and the pause's
        if action == "set an override":
            time.sleep(1.0)
        return real(actor, action, *a, **k)

    monkeypatch.setattr(store, "audit", slow)
    body = _events(9, [{"ts": "2026-11-06T10:00:00Z", "type": "readback_mismatch", "detail": {"device": "inv"}}])
    with world.use():
        telemetry.latest(sid)                                  # the company's telemetry file is made before the threads (two first opens at once)
    answers = []

    def send():
        answers.append(world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=body))

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert sorted(r.status_code for r in answers) == [200, 200], [r.text for r in answers]
    assert sorted(bool(r.json().get("duplicate")) for r in answers) == [False, True], [r.json() for r in answers]
    assert _event_rows(world, sid) == (1, 1)


def test_an_event_batch_that_fails_midway_stores_nothing_and_is_stored_once_on_retry(world, monkeypatch):
    sid = world.site
    kh = world.key(["telemetry:write"])
    real = store.audit
    calls = {"n": 0}

    def failing_once(actor, action, *a, **k):
        if action == "set an override":
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
        return real(actor, action, *a, **k)

    monkeypatch.setattr(store, "audit", failing_once)
    body = _events(4, [{"ts": "2026-11-06T10:00:00Z", "type": "plan_applied"}, {"ts": "2026-11-06T10:01:00Z", "type": "local_override"}])
    assert world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=body).status_code == 500
    assert _event_rows(world, sid) == (0, 0)                   # nothing of the failed batch was kept
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=body)
    assert r.status_code == 200 and r.json()["stored"] == 2 and not r.json().get("duplicate"), r.text
    assert world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=body).json()["duplicate"] is True
    assert _event_rows(world, sid) == (2, 1)
    with world.use():
        assert "set an override" in [a["action"] for a in store.list_audit()]


# ------------------------------------------------------------------------------------------------ regression-edge-poison-batch

def test_a_telemetry_batch_with_an_impossible_ts_is_refused_every_time_before_anything_is_written(world):
    sid = world.site
    kh = {**world.key(["telemetry:write"]), "Content-Type": "application/json"}
    good_ts = _now() // 60 * 60 - 120
    for ts in (1e20, 1794000000000, 1_000_000_000, "9999-01-01T00:00:00Z", "1971-01-02T00:00:00Z", -5):
        body = _batch(3, rows=[[good_ts, 1, 900], [ts, 1, 900]])
        for _ in range(2):                                     # the same answer on every retry, never a 500
            r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=body)
            assert r.status_code == 422 and "row 2" in r.json()["detail"], (ts, r.status_code, r.text)
    # a non-finite ts (JSON's Infinity and NaN, which the body parser reads)
    for word in ("Infinity", "NaN", "-Infinity", "1" + "0" * 400):
        raw = '{"contract": "ems-edge/1.0", "seq": 3, "sent_at": "2026-11-06T10:00:00Z", "columns": ["ts", "grid_present"], "rows": [[%s, 1]]}' % word
        r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, content=raw)
        assert r.status_code == 422, (word, r.status_code, r.text)
    # a repeated column is refused too (the rows are read by name, so the last ts would win)
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(3, rows=[[good_ts, 1, good_ts]], columns=("ts", "grid_present", "ts")))
    assert r.status_code == 422 and "more than once" in r.json()["detail"]
    with world.use():
        assert telemetry.latest(sid) is None
    # the stream moves on: the same sequence, corrected, is stored (a ts in ISO form too)
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(3, rows=[[good_ts, 1, 900], [_iso(good_ts + 60), 1, 950]]))
    assert r.status_code == 200 and r.json()["accepted"] == 2 and not r.json().get("duplicate"), r.text


# ------------------------------------------------------------------------------------------------ OPT-15: an outage event re-solves (data-outage-unseen-until-next-quarter)

def _clock(monkeypatch, start: int) -> dict:
    """The clock the edge routes read, set by the test: an event re-solve's timing is decided by whole minutes."""
    clock = {"now": start}
    monkeypatch.setattr(routes_edge, "now_ts", lambda: clock["now"])
    monkeypatch.setattr(routes_edge, "_last_event_solve", {})
    return clock


def test_an_outage_event_re_solves_once_telemetry_shows_it(world, monkeypatch):
    sid = world.site
    kh = world.key(["telemetry:write"])
    with world.use():
        sites.set_status(sid, "active")
    ticks = []

    def fake_tick(site_id, *a, **k):
        ticks.append((site_id, store.db_path()))
        return {"status": "active", "plan_id": "p-event"}

    monkeypatch.setattr(plans, "tick", fake_tick)
    minute = _now() // 60 * 60 - 1800
    clock = _clock(monkeypatch, minute + 5)
    post_t = lambda seq, rows: world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(seq, rows=rows, columns=("ts", "grid_present")))
    post_e = lambda seq, events: world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_events(seq, events))
    assert post_t(1, [[minute - 120, 1], [minute - 60, 1]]).status_code == 200
    # the grid is lost this minute: the event arrives before the batch that shows it, so the re-solve waits for that batch
    r = post_e(1, [{"ts": _iso(minute + 5), "type": "outage_start"}])
    assert r.status_code == 200 and r.json()["resolve"] == "waiting_for_telemetry" and ticks == [], r.text
    # the batch carries this minute's row, but a tick reads only the whole minutes before its time: still waiting (round 2 ticked here, saw no outage
    # and cleared the pending re-solve)
    clock["now"] = minute + 20
    r = post_t(2, [[minute, 0]])
    assert r.status_code == 200 and r.json()["resolve"] == "waiting_for_telemetry" and ticks == [], r.text
    clock["now"] = minute + 65                                  # the minute has ended: the next batch runs the re-solve
    r = post_t(3, [[minute + 60, 0]])
    assert r.status_code == 200 and r.json()["resolve"] == "scheduled", r.text
    assert ticks == [(sid, world.db)]                          # the tick ran in the company's own file
    clock["now"] = minute + 125
    assert post_t(4, [[minute + 120, 0]]).json().get("resolve") is None
    # the grid returns 20 seconds into minute+180: the re-solve waits for that minute to end, and then the site re-solved less than 30 seconds ago
    clock["now"] = minute + 205
    assert post_e(2, [{"ts": _iso(minute + 200), "type": "outage_end"}]).json()["resolve"] == "waiting_for_telemetry"
    clock["now"] = minute + 245
    r = post_t(5, [[minute + 180, 1]])
    assert r.json()["resolve"] == "rate_limited" and len(ticks) == 1, r.text
    routes_edge._last_event_solve.clear()                      # 30 seconds on: the next batch runs it
    clock["now"] = minute + 275
    assert post_t(6, [[minute + 240, 1]]).json()["resolve"] == "scheduled" and len(ticks) == 2
    # the grid is lost 50 seconds into a minute whose row still reads it present: that row does not show the outage, the next minute's row does
    m = minute + 600
    routes_edge._last_event_solve.clear()
    clock["now"] = m + 52
    assert post_e(3, [{"ts": _iso(m + 50), "type": "outage_start"}]).json()["resolve"] == "waiting_for_telemetry"
    clock["now"] = m + 62
    r = post_t(7, [[m - 60, 1], [m, 1]])
    assert r.json()["resolve"] == "waiting_for_telemetry" and len(ticks) == 2, r.text
    clock["now"] = m + 122
    assert post_t(8, [[m + 60, 0]]).json()["resolve"] == "scheduled" and len(ticks) == 3
    # telemetry that never shows what the event says: the re-solve runs anyway once RESOLVE_WAIT_S have passed since the event
    m = minute + 900
    routes_edge._last_event_solve.clear()
    clock["now"] = m + 5
    assert post_e(4, [{"ts": _iso(m), "type": "outage_end"}]).json()["resolve"] == "waiting_for_telemetry"
    clock["now"] = m + 65
    assert post_t(9, [[m, 0]]).json()["resolve"] == "waiting_for_telemetry"
    clock["now"] = m + 5 + routes_edge.RESOLVE_WAIT_S
    assert post_t(10, [[m + 60, 0]]).json()["resolve"] == "scheduled" and len(ticks) == 4
    # an outage that started and ended within one batch (sent in any order): the newest event decides, so the grid present shows it
    m = minute + 1200
    routes_edge._last_event_solve.clear()
    clock["now"] = m + 125
    assert post_t(11, [[m, 0], [m + 60, 1]]).json().get("resolve") is None
    r = post_e(5, [{"ts": _iso(m + 60), "type": "outage_end"}, {"ts": _iso(m), "type": "outage_start"}])
    assert r.json()["resolve"] == "scheduled" and len(ticks) == 5, r.text
    # a failing tick never fails the ack: the events are stored
    def broken(site_id, *a, **k):
        raise RuntimeError("solver crashed")

    monkeypatch.setattr(plans, "tick", broken)
    routes_edge._last_event_solve.clear()
    r = post_e(6, [{"ts": _iso(m + 60), "type": "outage_end"}])
    assert r.status_code == 200 and r.json()["stored"] == 1 and r.json()["resolve"] == "scheduled", r.text
    # a site that is not active (the scheduler does not tick it) is not re-solved
    monkeypatch.setattr(plans, "tick", fake_tick)
    with world.use():
        sites.set_status(sid, "observing")
    routes_edge._last_event_solve.clear()
    r = post_e(7, [{"ts": _iso(m + 60), "type": "outage_end"}])
    assert r.json()["resolve"] is None and len(ticks) == 5


def test_an_outage_event_re_solve_with_the_real_tick_plans_for_the_outage_and_for_its_end(world, monkeypatch):
    """routed-opt15-outage-resolve, the verifier's probes with the real plans.tick (6 hours of history; the clock the edge routes and the tick read is
    set by the test): a batch that carries the current minute's row, the grid's return, and a grid lost 50 seconds into a minute whose row still reads
    it present. Round 2 ticked as soon as any row at the event's minute arrived, while the tick reads only the whole minutes before its time: the
    re-solve saw no outage (or still saw it after it ended), cleared the pending re-solve, and the plan kept grid import until the next quarter."""
    sid = world.site
    kh = world.key(["telemetry:write"])
    t0 = int(datetime(2026, 11, 2, 21, 0, tzinfo=PKT).timestamp())    # an evening quarter (no sun), in the past
    clock = _clock(monkeypatch, t0 + 280)
    real_tick = plans.tick
    results = []

    def tick(site_id, *a, **k):                                # the real tick, at the test's clock
        results.append(real_tick(site_id, at=clock["now"]))
        return results[-1]

    monkeypatch.setattr(plans, "tick", tick)
    cols = ("ts", "grid_present", "load_power_w", "pv_power_w")
    with world.use():
        sites.set_status(sid, "active")
        telemetry.ingest(sid, [dict(zip(cols, (m, 1, 60000, 0))) for m in range(t0 - 6 * 3600, t0 + 300, 60)], "edge")
    post_t = lambda seq, rows: world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh,
                                               json=_batch(seq, rows=[[t, g, 60000, 0] for t, g in rows], columns=cols))
    post_e = lambda seq, events: world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_events(seq, events))
    # 1. the grid is lost 5 seconds into minute m1, and the batch carries the current minute's row
    m1 = t0 + 300
    clock["now"] = m1 + 6
    assert post_e(1, [{"ts": _iso(m1 + 5), "type": "outage_start"}]).json()["resolve"] == "waiting_for_telemetry"
    clock["now"] = m1 + 20
    r = post_t(1, [(m1, 0)])
    assert r.status_code == 200 and r.json()["resolve"] == "waiting_for_telemetry" and results == [], r.text
    clock["now"] = m1 + 70
    assert post_t(2, [(m1 + 60, 0)]).json()["resolve"] == "scheduled"
    assert len(results) == 1 and results[0]["status"] in ("active", "held"), results
    seen = results[0]["summary"]["outage_now"]
    assert seen is not None and seen["since"] == m1, results[0]["summary"]
    assert results[0]["steps"][0]["grid_present"] == 0.0 and results[0]["steps"][0]["grid_import_kw"] == 0.0
    # 2. the grid returns 20 seconds into minute m2; the batch carries that minute's row (current), then the next one's
    m2 = m1 + 180
    routes_edge._last_event_solve.clear()
    clock["now"] = m2 + 5
    assert post_t(3, [(m1 + 120, 0)]).json().get("resolve") is None
    clock["now"] = m2 + 25
    assert post_e(2, [{"ts": _iso(m2 + 20), "type": "outage_end"}]).json()["resolve"] == "waiting_for_telemetry"
    assert post_t(4, [(m2, 1)]).json()["resolve"] == "waiting_for_telemetry" and len(results) == 1
    clock["now"] = m2 + 70
    assert post_t(5, [(m2 + 60, 1)]).json()["resolve"] == "scheduled"
    assert len(results) == 2 and results[1]["summary"]["outage_now"] is None, results[-1]["summary"]
    # 3. the grid is lost 50 seconds into minute m3 (next quarter), whose row still reads it present: the next minute's row shows the outage
    m3 = t0 + 900 + 120
    routes_edge._last_event_solve.clear()
    clock["now"] = m3 + 2
    assert post_t(6, [(m, 1) for m in range(m2 + 120, m3, 60)]).json().get("resolve") is None
    clock["now"] = m3 + 52
    assert post_e(3, [{"ts": _iso(m3 + 50), "type": "outage_start"}]).json()["resolve"] == "waiting_for_telemetry"
    clock["now"] = m3 + 62
    assert post_t(7, [(m3, 1)]).json()["resolve"] == "waiting_for_telemetry" and len(results) == 2
    clock["now"] = m3 + 122
    assert post_t(8, [(m3 + 60, 0)]).json()["resolve"] == "scheduled"
    assert len(results) == 3 and (results[2]["summary"]["outage_now"] or {}).get("since") == m3 + 60, results[-1]["summary"]
    with world.use():
        assert sites.state(sid).get("resolve_pending") is None


def test_an_outage_event_after_a_stale_leftover_starts_a_fresh_re_solve(world, monkeypatch):
    """routed-opt15-outage-resolve: a re-solve left pending for more than RESOLVE_STALE_S (the link dropped during an outage, so no batch ran it) was
    merged into the next outage event with its old 'since', and that event's re-solve was then dropped as stale."""
    sid = world.site
    kh = world.key(["telemetry:write"])
    ticks = []
    monkeypatch.setattr(plans, "tick", lambda site_id, *a, **k: ticks.append(site_id) or {"status": "active"})
    minute = _now() // 60 * 60 - 3600
    clock = _clock(monkeypatch, minute + 65)
    with world.use():
        sites.set_status(sid, "active")
        telemetry.ingest(sid, [{"ts": minute - 60, "grid_present": 1}, {"ts": minute, "grid_present": 0}], "edge")
        sites.put_state(sid, resolve_pending={"minute": minute - 1200, "event": "outage_end", "since": clock["now"] - routes_edge.RESOLVE_STALE_S - 60})
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_events(1, [{"ts": _iso(minute + 5), "type": "outage_start"}]))
    assert r.status_code == 200 and r.json()["resolve"] == "scheduled" and ticks == [sid], r.text
    with world.use():
        assert sites.state(sid).get("resolve_pending") is None
        # a held re-solve that is not stale is merged; since round 3 its 'since' restarts when a newer event changes what telemetry must show (the
        # RESOLVE_WAIT_S fallback counts from the event that decides), and is kept when the deciding event is the held one
        clock["now"] = minute + 600
        sites.put_state(sid, resolve_pending={"minute": minute + 480, "event": "outage_start", "since": clock["now"] - 30})
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_events(2, [{"ts": _iso(minute + 545), "type": "outage_end"}]))
    assert r.json()["resolve"] == "waiting_for_telemetry" and ticks == [sid], r.text
    with world.use():
        assert sites.state(sid)["resolve_pending"] == {"minute": minute + 540, "event": "outage_end", "since": clock["now"]}
    clock["now"] += 10
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_events(3, [{"ts": _iso(minute + 500), "type": "outage_start"}]))
    assert r.json()["resolve"] == "waiting_for_telemetry" and ticks == [sid], r.text
    with world.use():
        assert sites.state(sid)["resolve_pending"] == {"minute": minute + 540, "event": "outage_end", "since": clock["now"] - 10}


def test_a_pending_event_re_solve_is_dropped_when_the_site_is_no_longer_active_or_it_is_stale(world, monkeypatch):
    sid = world.site
    kh = world.key(["telemetry:write"])
    ticks = []
    monkeypatch.setattr(plans, "tick", lambda site_id, *a, **k: ticks.append(site_id) or {"status": "active"})
    monkeypatch.setattr(routes_edge, "_last_event_solve", {})
    minute = _now() // 60 * 60
    post_t = lambda seq, rows: world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(seq, rows=rows, columns=("ts", "grid_present")))
    with world.use():
        sites.set_status(sid, "active")
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_events(1, [{"ts": _iso(minute), "type": "outage_start"}]))
    assert r.json()["resolve"] == "waiting_for_telemetry"
    with world.use():
        sites.set_status(sid, "observing")                     # taken out of control before the batch arrives: the scheduler does not tick it
    assert post_t(1, [[minute, 0]]).json().get("resolve") is None and ticks == []
    with world.use():
        assert sites.state(sid).get("resolve_pending") is None
        sites.set_status(sid, "active")
        # an event re-solve left pending for more than a quarter (the edge's telemetry stopped): a scheduled tick has re-solved since
        sites.put_state(sid, resolve_pending={"minute": minute + 60, "event": "outage_end", "since": _now() - routes_edge.RESOLVE_STALE_S - 60})
    assert post_t(2, [[minute + 60, 1]]).json().get("resolve") is None and ticks == []
    with world.use():
        assert sites.state(sid).get("resolve_pending") is None


# ------------------------------------------------------------------------------------------------ resume re-solves

def test_resume_re_solves_at_once_and_a_failed_tick_does_not_undo_it(world, monkeypatch):
    sid = world.site
    admin = world.c["admin"]
    ticks = []

    def fake_tick(site_id, *a, **k):
        ticks.append(site_id)
        return {"status": "active", "plan_id": "p-resume"}

    monkeypatch.setattr(plans, "tick", fake_tick)
    assert admin.post(f"/api/sites/{sid}/control/pause", json={"hours": 2}).json()["control"]["key"] == "paused"
    r = admin.post(f"/api/sites/{sid}/control/resume", json={})
    assert r.status_code == 200 and r.json()["control"]["key"] != "paused", r.text
    # the pause re-solved at once too (round 7, audit flow-telemetry-plan-control-15: every control action takes effect at once)
    assert ticks == [sid, sid] and r.json()["resolve"] == {"status": "active", "plan_id": "p-resume", "reason": None}
    # nothing to resume: no solve
    assert "resolve" not in admin.post(f"/api/sites/{sid}/control/resume", json={}).json() and ticks == [sid, sid]

    def broken(site_id, *a, **k):
        raise RuntimeError("solver crashed")

    monkeypatch.setattr(plans, "tick", broken)
    assert admin.post(f"/api/sites/{sid}/control/release", json={"hours": 2}).json()["control"]["key"] == "native"
    r = admin.post(f"/api/sites/{sid}/control/resume", json={})
    assert r.status_code == 200 and r.json()["resolve"]["status"] == "failed" and r.json()["control"]["key"] != "native", r.text
    with world.use():
        assert sites.overrides(sid) == []


# ------------------------------------------------------------------------------------------------ new-report-fails-on-old-infinity-day (reports side)

def test_a_report_prints_a_dash_for_a_missing_day_value(world, monkeypatch):
    sid = world.site
    period = datetime.now(PKT).strftime("%Y-%m")
    day = f"{period}-01"
    row = {"day": day, "import_kwh": None, "export_kwh": None, "pv_kwh": 12.0, "load_kwh": None, "genset_kwh": None, "genset_hours": None, "litres": None,
           "outage_minutes": None, "unserved_kwh": None, "peak_kw": None}
    monkeypatch.setattr(telemetry, "days", lambda site_id, d0, d1: [dict(row), {"day": day[:-2] + "02", "pv_kwh": 3.0}])
    with world.use():
        for kind in ("energy_only", "monthly_savings"):
            rep = reports.generate(kind, sid, period, "test")
            assert reports.file_path(rep["id"], "pdf").read_bytes()[:4] == b"%PDF"
            text = reports.file_path(rep["id"], "csv").read_text(encoding="utf-8")
            assert text.splitlines()[1].startswith(f"{day},,,12.0,"), text
    assert reports.num(None) == "-" and reports.num(float("inf")) == "-" and reports.num(90, ".1f", scale=60) == "1.5"


# ------------------------------------------------------------------------------------------------ overview SoC from the BMS

def test_the_overview_falls_back_to_the_bms_state_of_charge(world):
    sid = world.site
    now = _now()
    with world.use():
        telemetry.ingest(sid, [{"ts": now - 120, "grid_present": 1, "battery_soc_bms_pct": 63.0},
                               {"ts": now - 60, "grid_present": 1, "battery_soc_bms_pct": 64.0, "battery_soc_est_pct": 61.0}], "csv")
    r = world.c["viewer"].get(f"/api/sites/{sid}/now")
    assert r.status_code == 200 and r.json()["now"]["soc_pct"] == 61.0, r.text     # the estimate wins where there is one
    with world.use():
        telemetry.ingest(sid, [{"ts": now, "grid_present": 1, "battery_soc_bms_pct": 66.0}], "csv")
    assert world.c["viewer"].get(f"/api/sites/{sid}/now").json()["now"]["soc_pct"] == 66.0
    with world.use():
        sid2 = sites.create({"name": "BMS only"}, "test")["id"]
        telemetry.ingest(sid2, [{"ts": now - 3 * 3600 + 60 * i, "grid_present": 1, "battery_soc_bms_pct": 70.0} for i in range(10)], "csv")
    chart = world.c["viewer"].get(f"/api/sites/{sid2}/overview/chart").json()
    soc = [x for x in chart["actual"]["series"]["battery_soc_est_pct"] if x is not None]
    assert soc and all(x == 70.0 for x in soc), chart["actual"]
    assert "battery_soc_bms_pct" in chart["actual"]["series"]


# ------------------------------------------------------------------------------------------------ tariffs-verify-icp-priced-at-tou (prices page side)

def test_the_prices_page_shows_the_icp_prices_the_ledger_uses(world):
    with world.use():
        sid = sites.create({"name": "ICP mill"}, "test")["id"]
        sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 500,
                                   "icp": {"eligible": True, "peak": {str(m): 100.0 for m in range(1, 13)},
                                           "off_peak": {str(m): 1000.0 for m in range(1, 13)}}}, "test")
        conn = sites.connections(sid)[0]
        d = datetime.now(PKT).date()
        t_start = twin.day_start_utc(d.replace(day=1))
        cols = ("site_id", "ts", "source_id", "grid_present", "grid_import_wh")
        telemetry.write_rows([(sid, t, "csv", 1.0, 100.0 * 1000 / 60) for t in range(t_start, _now() // 60 * 60, 60)], cols)
        ledger = verify._minute_prices(conn, d, sid)
    r = world.c["admin"].get(f"/api/sites/{sid}/prices", params={"day": d.isoformat()})
    assert r.status_code == 200, r.text
    page = r.json()
    assert {p: v["case"] for p, v in page["icp"].items()} == ledger["icp"], (page["icp"], ledger["icp"])
    assert all(v["case"] == "above" for v in page["icp"].values()), page["icp"]
    assert np.allclose(page["c_imp"], np.round(ledger["c_imp"][::15], 2), atol=0.011), (page["c_imp"][:4], ledger["c_imp"][:60:15])
    assert page["month_import_kwh"]["peak"] > 100 and page["month_import_kwh"]["off_peak"] > 1000
    assert page["month_export_kwh"] is None                    # exports are netted only at a legacy net-metered site
    # an off-grid site still answers (no imports to read)
    with world.use():
        off = sites.create({"name": "Farm"}, "test")["id"]
    assert world.c["admin"].get(f"/api/sites/{off}/prices").json()["month_import_kwh"] is None


def test_the_prices_page_nets_a_legacy_sites_exports_as_the_ledger_does(world):
    with world.use():
        sid = sites.create({"name": "Legacy roof"}, "test")["id"]
        sites.put_connection(sid, {"tariff_category": "A-1(b)", "sanctioned_load_kw": 20, "regime": "net_metering_legacy"}, "test")
        conn = sites.connections(sid)[0]
        d = datetime.now(PKT).date()
        t_start = twin.day_start_utc(d.replace(day=1))
        cols = ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "grid_export_wh")
        telemetry.write_rows([(sid, t, "csv", 1.0, 2.0 * 1000 / 60, 30.0 * 1000 / 60) for t in range(t_start, _now() // 60 * 60, 60)], cols)
        ledger = verify._minute_prices(conn, d, sid)
    page = world.c["admin"].get(f"/api/sites/{sid}/prices", params={"day": d.isoformat()}).json()
    assert page["month_export_kwh"]["peak"] > 0 and page["month_export_kwh"]["off_peak"] > 0, page
    assert np.allclose(page["c_exp"], np.round(ledger["c_exp"][::15], 2), atol=0.011), (page["c_exp"][:4], ledger["c_exp"][:60:15])
    assert np.allclose(page["c_imp"], np.round(ledger["c_imp"][::15], 2), atol=0.011)


# ------------------------------------------------------------------------------------------------ regression-prices-odd-day-500

def test_the_prices_page_refuses_a_day_it_has_no_prices_for(world, monkeypatch):
    """A day before 1970 or past about the year 3000 answered 500 on Windows (verify.month_totals reads plans.peak_mask, whose local time comes from
    datetime.fromtimestamp, which raises OSError there): such a day is refused with 422, as is one before 2015 or more than a year ahead."""
    sid = world.site
    admin = world.c["admin"]
    today = datetime.now(PKT).date()
    for day in ("1969-12-31", "0001-01-01", "2014-12-31", "9999-12-31", "3001-01-01", (today + timedelta(days=400)).isoformat()):
        r = admin.get(f"/api/sites/{sid}/prices", params={"day": day})
        assert r.status_code == 422 and "2015-01-01" in r.json()["detail"], (day, r.status_code, r.text)
    for day in (today.isoformat(), (today + timedelta(days=300)).isoformat()):
        r = admin.get(f"/api/sites/{sid}/prices", params={"day": day})
        assert r.status_code == 200 and len(r.json()["c_imp"]) == 96 and r.json()["day"] == day, (day, r.status_code, r.text)
    assert admin.get(f"/api/sites/{sid}/prices", params={"day": "2015-01-01"}).status_code in (200, 422)
    # a day the clock cannot place locally (any other path to OSError or OverflowError) is a 422 too, never a 500

    def no_local_time(*a, **k):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(verify, "month_totals", no_local_time)
    r = admin.get(f"/api/sites/{sid}/prices", params={"day": today.isoformat()})
    assert r.status_code == 422 and r.json()["detail"] == "That day has no prices", r.text


_ = (json, date, pytest)
