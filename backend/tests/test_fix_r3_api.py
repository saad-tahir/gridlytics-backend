"""Round 3 of the API group (7 November 2026): the two open items of round 2 (routed-opt15-outage-resolve's race and stall; the flaky concurrent-events
test is steadied in test_fix_r2_api.py) and the web app's backend asks (frontend docs/BACKEND_ASKS.md) that the API group's files answer.

Each test works in a data folder of its own (the `world` fixture of test_fix_api.py), with the edge routes' clock set by the test where timing decides."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import BackgroundTasks

from gridlytics import alerts, apikeys, plans, reports, routes_edge, sites, store, store_founder, store_members, tariffs, telemetry
from gridlytics import platform as platformdb
from test_fix_api import _batch, _now, world  # noqa: F401  (world is a fixture)
from test_fix_r2_api import _clock, _events, _iso


def _posts(w, sid, kh):
    post_t = lambda seq, rows: w.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_batch(seq, rows=rows, columns=("ts", "grid_present")))
    post_e = lambda seq, events: w.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_events(seq, events))
    return post_t, post_e


def _active_with_ticks(w, monkeypatch, clock_box: dict) -> list:
    """The site made active, and plans.tick replaced by one that records the edge clock it ran at."""
    with w.use():
        sites.set_status(w.site, "active")
    ticks = []
    monkeypatch.setattr(plans, "tick", lambda site_id, *a, **k: ticks.append(clock_box["clock"]["now"]) or {"status": "active"})
    return ticks


# ------------------------------------------------------------------------------------------------ routed-opt15-outage-resolve: the race

def test_a_batch_runs_the_pending_re_solve_as_stored_after_it_not_one_an_event_replaced_meanwhile(world, monkeypatch):
    """Round 2's batch ran the re-solve from the state read at the request's start and then cleared whatever was pending: an outage_end stored by an
    events request while the batch was being stored was wiped, the start's re-solve ran instead, and the plan kept the grid down until the next quarter."""
    sid = world.site
    kh = world.key(["telemetry:write"])
    box = {}
    ticks = _active_with_ticks(world, monkeypatch, box)
    minute = _now() // 60 * 60 - 1800
    box["clock"] = clock = _clock(monkeypatch, minute + 5)
    post_t, post_e = _posts(world, sid, kh)
    assert post_t(1, [[minute - 120, 1], [minute - 60, 1]]).status_code == 200
    assert post_e(1, [{"ts": _iso(minute + 5), "type": "outage_start"}]).json()["resolve"] == "waiting_for_telemetry"
    # the batch that shows the outage is held open once its rows are stored; the grid comes back meanwhile and its event is stored
    real_ingest = telemetry.ingest
    started, go, hold = threading.Event(), threading.Event(), {"on": True}

    def held_ingest(*a, **k):
        res = real_ingest(*a, **k)
        if hold["on"]:
            hold["on"] = False
            started.set()
            go.wait(30)
        return res

    monkeypatch.setattr(telemetry, "ingest", held_ingest)
    clock["now"] = minute + 65
    answers = {}
    t = threading.Thread(target=lambda: answers.__setitem__("batch", post_t(2, [[minute, 0]])))
    t.start()
    try:
        assert started.wait(30)
        r = post_e(2, [{"ts": _iso(minute + 62), "type": "outage_end"}])
        assert r.status_code == 200 and r.json()["resolve"] == "waiting_for_telemetry", r.text
    finally:
        go.set()
        t.join(60)
    b = answers["batch"]
    assert b.status_code == 200 and b.json()["resolve"] == "waiting_for_telemetry" and ticks == [], b.text
    with world.use():
        assert sites.state(sid)["resolve_pending"] == {"minute": minute + 60, "event": "outage_end", "since": minute + 65}
    # the next minute's batch shows the grid back: the end's re-solve runs
    clock["now"] = minute + 125
    r = post_t(3, [[minute + 60, 1]])
    assert r.json()["resolve"] == "scheduled" and ticks == [minute + 125], r.text
    with world.use():
        assert sites.state(sid).get("resolve_pending") is None


def test_a_re_solve_clears_only_the_pending_one_it_decided_on(world, monkeypatch):
    """Read, compare and clear are one write transaction: a re-solve decided on a pending event that an events request has since replaced clears
    nothing and runs nothing; the newer one is decided on in its turn (here: telemetry does not show it yet)."""
    sid = world.site
    box = {}
    _active_with_ticks(world, monkeypatch, box)
    minute = _now() // 60 * 60 - 1800
    box["clock"] = _clock(monkeypatch, minute + 65)
    old = {"minute": minute, "event": "outage_start", "since": minute + 10}       # telemetry shows it: the minute ended with the grid down
    new = {"minute": minute + 60, "event": "outage_end", "since": minute + 64}    # stored meanwhile; its minute has not ended
    with world.use():
        telemetry.ingest(sid, [{"ts": minute - 60, "grid_present": 1}, {"ts": minute, "grid_present": 0}], "edge")
        sites.put_state(sid, resolve_pending=new)
        bg = BackgroundTasks()
        assert routes_edge._resolve_pending(sid, {"resolve_pending": old}, bg, True, wait_over=True) == "waiting_for_telemetry"
        assert bg.tasks == [] and sites.state(sid)["resolve_pending"] == new
        assert sid not in routes_edge._last_event_solve                          # nothing ran, so the gap is not taken
        # the pending one that is stored runs, and is cleared
        bg = BackgroundTasks()
        sites.put_state(sid, resolve_pending=old)
        assert routes_edge._resolve_pending(sid, sites.state(sid), bg, True) == "scheduled" and len(bg.tasks) == 1
        assert sites.state(sid)["resolve_pending"] is None


# ------------------------------------------------------------------------------------------------ routed-opt15-outage-resolve: the stall

def test_an_event_sent_before_the_backlog_after_a_stall_waits_for_telemetry_that_shows_it(world, monkeypatch):
    """Round 2 kept the held re-solve's `since` when a newer event replaced it: after telemetry stalled for 275 s, an outage_start sent before the backlog
    was past RESOLVE_WAIT_S at once, ticked on telemetry that did not show it, and cleared the pending re-solve; the backlog then answered None."""
    sid = world.site
    kh = world.key(["telemetry:write"])
    box = {}
    ticks = _active_with_ticks(world, monkeypatch, box)
    m0 = _now() // 60 * 60 - 3600
    box["clock"] = clock = _clock(monkeypatch, m0 + 5)
    post_t, post_e = _posts(world, sid, kh)
    assert post_t(1, [[m0 - 120, 1], [m0 - 60, 1]]).status_code == 200
    assert post_e(1, [{"ts": _iso(m0 + 3), "type": "outage_end"}]).json()["resolve"] == "waiting_for_telemetry"
    # telemetry stalls; 275 s on the grid is lost and the event arrives before the backlog
    clock["now"] = m0 + 280
    m1 = m0 + 240
    r = post_e(2, [{"ts": _iso(m0 + 275), "type": "outage_start"}])
    assert r.status_code == 200 and r.json()["resolve"] == "waiting_for_telemetry" and ticks == [], r.text
    with world.use():
        assert sites.state(sid)["resolve_pending"] == {"minute": m1, "event": "outage_start", "since": m0 + 280}
    # the backlog arrives within the event's minute: a tick now reads only the minutes before it, so it still waits
    clock["now"] = m0 + 290
    r = post_t(2, [[m, 1] for m in range(m0, m1, 60)] + [[m1, 0]])
    assert r.json()["resolve"] == "waiting_for_telemetry" and ticks == [], r.text
    # the next minute's batch: the event's minute has ended and reads the grid down
    clock["now"] = m0 + 305
    r = post_t(3, [[m1 + 60, 0]])
    assert r.json()["resolve"] == "scheduled" and ticks == [m0 + 305], r.text


def test_an_events_batch_never_runs_a_re_solve_on_the_wait_alone(world, monkeypatch):
    """The RESOLVE_WAIT_S fallback is taken only by a telemetry batch, once it is stored: an event re-sent after the wait (the same deciding event, so its
    `since` is kept) does not tick on telemetry older than the event."""
    sid = world.site
    kh = world.key(["telemetry:write"])
    box = {}
    ticks = _active_with_ticks(world, monkeypatch, box)
    minute = _now() // 60 * 60 - 1800
    box["clock"] = _clock(monkeypatch, minute + 200)
    post_t, post_e = _posts(world, sid, kh)
    with world.use():
        telemetry.ingest(sid, [{"ts": minute - 60, "grid_present": 1}], "edge")
        sites.put_state(sid, resolve_pending={"minute": minute, "event": "outage_start", "since": minute + 10})
    r = post_e(1, [{"ts": _iso(minute + 5), "type": "outage_start"}])
    assert r.status_code == 200 and r.json()["resolve"] == "waiting_for_telemetry" and ticks == [], r.text
    with world.use():
        assert sites.state(sid)["resolve_pending"] == {"minute": minute, "event": "outage_start", "since": minute + 10}
    r = post_t(1, [[minute + 120, 1]])
    assert r.json()["resolve"] == "scheduled" and ticks == [minute + 200], r.text


def test_an_events_batch_whose_re_solve_cannot_be_held_is_answered_failed_with_its_events_stored(world, monkeypatch):
    """The pending re-solve is written inside the try: a failure there answers 200 with resolve 'failed' (the events are stored and acknowledged), not a
    500 whose retry is acknowledged as a duplicate."""
    sid = world.site
    kh = world.key(["telemetry:write"])
    box = {}
    _active_with_ticks(world, monkeypatch, box)
    box["clock"] = _clock(monkeypatch, _now())

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(routes_edge, "_merge_pending", broken)
    _, post_e = _posts(world, sid, kh)
    events = [{"ts": _iso(_now()), "type": "outage_start"}]
    r = post_e(1, events)
    assert r.status_code == 200 and r.json()["stored"] == 1 and r.json()["resolve"] == "failed", r.text
    assert post_e(1, events).json()["duplicate"] is True                # the same message sent again


# ------------------------------------------------------------------------------------------------ plan held

def _plan_row(w, sid, pid, status, reason, generated):
    # valid for the 48 hours a tick's plan is (a plan past its valid_until is not in force: round 7, engine-delivery-24)
    until = (datetime.fromisoformat(generated) + timedelta(hours=48)).isoformat(timespec="seconds")
    with w.use(), store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                    (pid, sid, generated, generated, generated, until, "a", status, reason))


def test_the_plan_answer_says_why_no_plan_is_in_force(world, monkeypatch):
    sid = world.site
    c = world.c["viewer"]
    monkeypatch.delattr(plans, "held_info", raising=False)                     # the fallback of this file first
    assert c.get(f"/api/sites/{sid}/plan").json() == {"plan": None, "held": None}
    until = (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(timespec="seconds")
    with world.use():
        o = sites.add_override(sid, {"kind": "pause_control", "until": until, "reason": "test"}, "test")
    _plan_row(world, sid, "p-held", "held", "control_paused", "2026-11-07T10:00:00+00:00")
    r = c.get(f"/api/sites/{sid}/plan")
    assert r.status_code == 200 and r.json() == {"plan": None, "held": {"reason": "control_paused", "until": o["until_ts"], "plan_id": "p-held"}}, r.text
    # the pause cleared, the latest plan held by its guard
    with world.use():
        sites.clear_override(sid, o["id"], "test")
    _plan_row(world, sid, "p-guard", "held", "The plan would discharge below the reserve", "2026-11-07T10:15:00+00:00")
    assert c.get(f"/api/sites/{sid}/plan").json()["held"] == {"reason": "The plan would discharge below the reserve", "until": None, "plan_id": "p-guard"}
    # the optimiser's helper decides where it exists
    monkeypatch.setattr(plans, "held_info", lambda site_id, now=None: {"reason": "control_paused", "until": "u", "plan_id": "p", "more": 1}, raising=False)
    assert c.get(f"/api/sites/{sid}/plan").json() == {"plan": None, "held": {"reason": "control_paused", "until": "u", "plan_id": "p"}}


@pytest.mark.parametrize("helper", ["fallback", "plans.held_info"])
def test_a_pause_before_its_first_tick_is_held_with_no_plan_id(world, monkeypatch, helper):
    sid = world.site
    if helper == "fallback":
        monkeypatch.delattr(plans, "held_info", raising=False)
    elif not hasattr(plans, "held_info"):
        pytest.skip("the optimiser group's plans.held_info is not there yet")
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    with world.use():
        o = sites.add_override(sid, {"kind": "force_native", "until": until, "reason": "test"}, "test")
    r = world.c["admin"].get(f"/api/sites/{sid}/plan")
    assert r.status_code == 200 and r.json() == {"plan": None, "held": {"reason": "released_to_native", "until": o["until_ts"], "plan_id": None}}, r.text
    _plan_row(world, sid, "p-paused", "held", "released_to_native", "2026-11-07T10:00:00+00:00")
    assert world.c["admin"].get(f"/api/sites/{sid}/plan").json()["held"]["plan_id"] == "p-paused"


# ------------------------------------------------------------------------------------------------ sites: activation, connections, prices, drafts, tier

def test_an_off_grid_site_activates_on_its_connection_or_its_flag_and_reads_off_grid_in_the_fleet(world):
    a = world.c["admin"]
    two = world.other
    source = {"data_source": {"kind": "csv", "grid_signal_confirmed": True}}     # a confirmed source is asked for too (audit flow-onboarding-08)
    with world.use():
        sites.update(two, source, "test")
        sites.put_asset(two, "inverter", {"kw_ac": 50}, "test")
        three = sites.create({"name": "Three", "site_type": "office", **source}, "test")["id"]
        sites.put_asset(three, "inverter", {"kw_ac": 50}, "test")
    r = a.post(f"/api/sites/{two}/activate")
    assert r.status_code == 422 and "tab C" in r.json()["detail"], r.text
    assert a.put(f"/api/sites/{two}/connection", json={"regime": "off_grid"}).status_code == 200
    assert a.post(f"/api/sites/{two}/activate").status_code == 200
    row = next(s for s in a.get("/api/fleet").json()["sites"] if s["id"] == two)
    assert row["category"] == "Off-grid"
    with world.use(), store.connect() as con:                                    # the site flag (SiteFields carries it once the data group adds it)
        con.execute("UPDATE sites SET data = json_set(data, '$.off_grid', json('true')) WHERE id = ?", (three,))
    assert a.post(f"/api/sites/{three}/activate").status_code == 200


def test_the_prices_page_reads_the_connection_it_is_asked_for(world):
    sid = world.site
    a = world.c["admin"]
    with world.use():
        first = sites.connections(sid)[0]["id"]
        second = sites.put_connection(sid, {"tariff_category": "A-2(a)", "meter_ref": "M-2"}, "test")["id"]
    r = a.get(f"/api/sites/{sid}/prices")
    assert r.status_code == 200 and r.json()["connection_id"] == first, r.text
    r = a.get(f"/api/sites/{sid}/prices", params={"connection": second})
    assert r.status_code == 200 and r.json()["connection_id"] == second and len(r.json()["c_imp"]) == 96, r.text
    assert a.get(f"/api/sites/{sid}/prices", params={"connection": "nope"}).status_code == 404


def test_a_retried_first_save_of_a_connection_makes_no_duplicate(world):
    two = world.other
    a = world.c["admin"]
    body = {"tariff_category": "B-2(b)", "regime": "net_billing", "sanctioned_load_kw": 200}
    count = lambda: len(a.get(f"/api/sites/{two}").json()["connections"])
    for _ in range(2):
        assert a.put(f"/api/sites/{two}/connection", json=dict(body)).status_code == 200
    assert count() == 1
    # the retry carries an edit: the same regime, tariff and meter reference is the same connection
    assert a.put(f"/api/sites/{two}/connection", json={**body, "sanctioned_load_kw": 250}).status_code == 200
    conns = a.get(f"/api/sites/{two}").json()["connections"]
    assert len(conns) == 1 and conns[0]["data"]["sanctioned_load_kw"] == 250
    # another meter is another connection
    assert a.put(f"/api/sites/{two}/connection", json={**body, "meter_ref": "M-2"}).status_code == 200 and count() == 2
    # a client's idempotency key decides, in the header or in the body
    h = {"Idempotency-Key": "first-save-3"}
    assert a.put(f"/api/sites/{two}/connection", headers=h, json={"tariff_category": "A-2(a)"}).status_code == 200 and count() == 3
    r = a.put(f"/api/sites/{two}/connection", headers=h, json={"tariff_category": "A-2(a)", "sanctioned_load_kw": 300})
    assert r.status_code == 200 and count() == 3
    assert [c["data"]["sanctioned_load_kw"] for c in r.json()["connections"] if c["data"]["tariff_category"] == "A-2(a)"] == [300]
    for _ in range(2):
        assert a.put(f"/api/sites/{two}/connection", json={"idempotency_key": "k4", "tariff_category": "A-2(a)"}).status_code == 200
    assert count() == 4
    assert a.put(f"/api/sites/{two}/connection", json={"idempotency_key": "x" * 101}).status_code == 422


def test_a_connection_is_removed_by_whoever_edits_sites_and_an_active_site_keeps_one(world):
    sid = world.site
    with world.use():
        first = sites.connections(sid)[0]["id"]
        second = sites.put_connection(sid, {"tariff_category": "A-2(a)", "meter_ref": "M-2"}, "test")["id"]
    assert world.c["viewer"].delete(f"/api/sites/{sid}/connection/{second}").status_code == 403
    assert world.c["operator"].delete(f"/api/sites/{sid}/connection/nope").status_code == 404
    r = world.c["operator"].delete(f"/api/sites/{sid}/connection/{second}")
    assert r.status_code == 200 and [c["id"] for c in r.json()["connections"]] == [first], r.text
    with world.use():
        sites.set_status(sid, "active")
        assert any(x["action"] == "removed a connection" and x["detail"] == {"connection_id": second} for x in store.list_audit())
    r = world.c["admin"].delete(f"/api/sites/{sid}/connection/{first}")
    assert r.status_code == 409 and len(world.c["admin"].get(f"/api/sites/{sid}").json()["connections"]) == 1, r.text


def test_drafts_are_listed_for_their_person_and_carry_the_site_they_made(world):
    sid = world.site
    a, op = world.c["admin"], world.c["operator"]
    assert a.put("/api/drafts/dA", json={"general": {"name": "New mill"}}).status_code == 200
    r = a.put("/api/drafts/dB", params={"site_id": sid}, json={"general": {"name": "Mill One"}})
    assert r.status_code == 200 and r.json()["site_id"] == sid, r.text
    assert a.put("/api/drafts/dC", json={"site_id": world.other, "general": {}}).json()["site_id"] == world.other
    assert a.put("/api/drafts/dD", params={"site_id": "nope"}, json={}).status_code == 422
    # a later save without one keeps the site
    assert a.put("/api/drafts/dB", json={"general": {"name": "Mill One", "city": "Lahore"}}).json()["site_id"] == sid
    mine = a.get("/api/drafts")
    assert mine.status_code == 200, mine.text
    assert {d["draft_id"]: d["site_id"] for d in mine.json()} == {"dA": None, "dB": sid, "dC": world.other}
    assert next(d for d in mine.json() if d["draft_id"] == "dB")["body"]["general"]["city"] == "Lahore"
    assert op.get("/api/drafts").json() == []


def test_the_tier_sign_off_is_kept_on_the_site(world):
    sid, two = world.site, world.other
    a = world.c["admin"]
    r = a.put(f"/api/sites/{sid}/tier", json={"tier": "monitor", "signoff": "Installer A. Khan, 6 Nov"})
    assert r.status_code == 200 and r.json()["tier_signoff"]["signoff"] == "Installer A. Khan, 6 Nov", r.text
    got = world.c["viewer"].get(f"/api/sites/{sid}").json()["tier_signoff"]
    assert got["tier"] == "monitor" and got["signoff"] == "Installer A. Khan, 6 Nov" and got["by"] and got["at"]
    assert a.get(f"/api/sites/{two}").json()["tier_signoff"] is None
    # a change made before round 3 is read from the audit trail
    with world.use():
        store.audit("Old Admin", "changed a site's allowed control tier", two, {"tier": "monitor", "signoff": "old sheet"})
    assert a.get(f"/api/sites/{two}").json()["tier_signoff"] == {"tier": "monitor", "signoff": "old sheet", "by": "Old Admin",
                                                                  "at": a.get(f"/api/sites/{two}").json()["tier_signoff"]["at"]}


# ------------------------------------------------------------------------------------------------ reports

def test_any_member_generates_the_energy_report_and_every_row_says_whether_it_is_preliminary(world):
    sid = world.site
    v, a = world.c["viewer"], world.c["admin"]
    assert v.get("/api/meta").json()["permissions"]["energy_report"] is True
    month = datetime.now(timezone(timedelta(hours=5))).strftime("%Y-%m")
    r = v.post("/api/reports", json={"kind": "energy_only", "site_id": sid, "period": month})
    assert r.status_code == 200 and r.json()["preliminary"] is True, r.text
    assert v.post("/api/reports", json={"kind": "monthly_savings", "site_id": sid, "period": month}).status_code == 403
    assert v.post("/api/reports", json={"kind": "daily_operations", "site_id": sid, "period": month}).status_code == 403
    # the daily operations sheet is for a day since round 7 (audit pages-D-24): a month is refused
    assert a.post("/api/reports", json={"kind": "daily_operations", "site_id": sid, "period": month}).status_code == 422
    day = datetime.now(timezone(timedelta(hours=5))).date().isoformat()
    assert a.post("/api/reports", json={"kind": "daily_operations", "site_id": sid, "period": day}).json()["preliminary"] is None
    rows = a.get("/api/reports").json()
    assert sorted((r["kind"], r["preliminary"]) for r in rows) == [("daily_operations", None), ("energy_only", True)]
    assert [r["preliminary"] for r in v.get("/api/reports").json()] == [True]


def test_a_workspace_file_of_schema_1_gains_the_reports_preliminary_column(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as con, con:
        for sql in store._V1:
            con.execute(sql)
        con.execute("INSERT INTO reports (id, site_id, kind, period, generated_at, inputs_hash, status) VALUES "
                    "('r1','s','energy_only','2026-10','2026-10-20T10:00:00+00:00','h','done'), "
                    "('r2','s','monthly_savings','2026-09','2026-10-20T10:00:00+00:00','h','done')")
        con.execute("PRAGMA user_version = 1")
    with store.use_company(path):
        rows = {r["id"]: r for r in reports.listing()}
        with store.connect() as con:
            assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION >= 2
    assert rows["r1"]["preliminary"] is True and rows["r2"]["preliminary"] is None    # October not over when made; September unknown
    assert list((tmp_path / "backup").glob("old-*-schema-v1.db"))                     # the copy before the migration


# ------------------------------------------------------------------------------------------------ tariffs

def test_the_tariff_index_counts_only_versions_that_can_be_approved(world):
    a = world.c["admin"]
    versions = a.get("/api/tariffs/versions").json()
    assert any(v["status"] == "awaiting_notification" for v in versions)        # the shipped library holds a placeholder
    assert a.get("/api/tariffs").json()["pending"] == sum(v["status"] == "pending" for v in versions)


def test_a_typed_fuel_price_records_its_source(world):
    a = world.c["admin"]
    body = {"kind": "hsd", "valid_from": "2026-11-16", "price": 265.5, "source": {"ref": "OGRA notification 16 Nov", "url": "https://ogra.org.pk/x"}}
    r = a.post("/api/tariffs/fuel", json=body)
    assert r.status_code == 200, r.text
    row = next(p for p in r.json()["prices"] if p["valid_from"] == "2026-11-16")
    assert row["source"]["ref"] == "OGRA notification 16 Nov" and row["source"]["url"] == "https://ogra.org.pk/x" and row["source"]["by"]
    r = a.post("/api/tariffs/fuel", json={"kind": "hsd", "valid_from": "2026-11-17", "price": 266})
    row = next(p for p in r.json()["prices"] if p["valid_from"] == "2026-11-17")
    assert row["source"]["kind"] == "manual" and row["source"].get("url") is None                # no source given: as before
    for bad in ({"url": "javascript:alert(1)"}, {"ref": "x" * 201}, {"other": 1}, "OGRA"):
        assert a.post("/api/tariffs/fuel", json={**body, "valid_from": "2026-11-18", "source": bad}).status_code == 422, bad
    assert not any(p["valid_from"] == "2026-11-18" for p in a.get("/api/tariffs/fuel").json()["prices"])


# ------------------------------------------------------------------------------------------------ platform: keys, seats, founder console

def test_a_key_needs_no_role(world):
    a = world.c["admin"]
    r = a.post("/api/platform/keys", json={"name": "edge one", "scopes": ["telemetry:write"], "site_id": world.site})
    assert r.status_code == 200 and r.json()["role"] == apikeys.DEFAULT_ROLE == "operator", r.text
    assert a.post("/api/platform/keys", json={"name": "e2", "role": "viewer", "scopes": ["telemetry:write"], "site_id": world.site}).json()["role"] == "viewer"
    assert a.post("/api/platform/keys", json={"name": "e3", "role": "boss", "scopes": ["telemetry:write"], "site_id": world.site}).status_code == 422


def test_the_org_answer_carries_its_seats(world):
    for c in world.c.values():
        assert c.get("/api/meta").status_code == 200                           # every member's row is made
    body = world.c["viewer"].get("/api/platform/org").json()
    assert body["seats"] == platformdb.company(world.company["id"])["seats"] and body["seats_used"] == 4, body


def test_the_founder_console_answers_the_gridlytics_role_and_not_the_old_name(world):
    for c in world.c.values():
        c.get("/api/meta")
    members = store_founder.company_members(world.company["id"])
    assert {m["gridlytics_role"] for m in members} == {"admin", "operator", "analyst", "viewer"}
    assert not any("solarlytics_role" in m for m in members)             # the lifted name is gone (round 7, audit founder-console-18)


# ------------------------------------------------------------------------------------------------ telemetry, outages, genset

def test_the_outages_page_carries_the_grid_signal(world):
    sid = world.site
    a = world.c["admin"]
    assert a.get(f"/api/sites/{sid}/outages").json()["signal"] == {"grid_present": None, "last_seen": None}
    t = _now() // 60 * 60
    with world.use():
        telemetry.ingest(sid, [{"ts": t - 180, "grid_present": 1}, {"ts": t - 120, "grid_present": 0}, {"ts": t - 60, "grid_present": None, "pv_power_w": 100}],
                         "edge")
    assert a.get(f"/api/sites/{sid}/outages").json()["signal"] == {"grid_present": False, "last_seen": t - 120}


def test_the_fuel_log_is_listed(world):
    sid = world.site
    op = world.c["operator"]
    assert op.post(f"/api/sites/{sid}/genset/fuel", json={"litres": 50, "day": "2026-11-05", "cost_rs": 14000, "note": "tanker"}).status_code == 200
    assert op.post(f"/api/sites/{sid}/genset/fuel", json={"litres": 20}).status_code == 200
    for bad in (-1, "lots", True, 1e12):
        assert op.post(f"/api/sites/{sid}/genset/fuel", json={"litres": 5, "cost_rs": bad}).status_code == 422, bad
    logs = world.c["admin"].get(f"/api/sites/{sid}/genset/fuel").json()["logs"]
    assert [(x["litres"], x["cost_rs"], x["day"]) for x in logs][1] == (50, 14000, "2026-11-05") and logs[0]["litres"] == 20
    assert logs[1]["note"] == "tanker" and logs[0]["by"] and logs[0]["ts"]
    assert all("cost_rs" not in x for x in world.c["viewer"].get(f"/api/sites/{sid}/genset/fuel").json()["logs"])


def test_an_upload_passes_its_file_name_where_the_import_takes_one(world, monkeypatch):
    sid = world.site
    a = world.c["admin"]
    seen = []

    def with_name(site_id, content, tz_offset_h=0.0, mapping=None, now=None, file_name=None):
        seen.append(file_name)
        return {"rows": 1, "gaps": 0}

    def without_name(site_id, content, tz_offset_h=0.0, mapping=None, now=None):
        seen.append("called")
        return {"rows": 1, "gaps": 0}

    csv = b"ts,grid_present\n2026-11-01T00:00:00Z,1\n"
    monkeypatch.setattr(telemetry, "import_csv", with_name)
    assert a.post(f"/api/sites/{sid}/telemetry/upload", files={"file": ("march.csv", csv, "text/csv")}).status_code == 200
    r = a.post(f"/api/sites/{sid}/telemetry/upload", params={"file_name": "C:\\logs\\april.csv"}, content=csv, headers={"Content-Type": "text/csv"})
    assert r.status_code == 200 and seen == ["march.csv", "april.csv"], (r.text, seen)
    monkeypatch.setattr(telemetry, "import_csv", without_name)
    assert a.post(f"/api/sites/{sid}/telemetry/upload", files={"file": ("may.csv", csv, "text/csv")}).status_code == 200 and seen[-1] == "called"


# ------------------------------------------------------------------------------------------------ alerts

def test_control_alerts_wait_for_a_plan_and_seven_days_of_data(world, monkeypatch):
    sid = world.site
    now = _now()
    plan = {"id": "p1", "steps": [], "latest_attempt": {"id": "f1", "status": "failed", "reason": "not solved"}}
    monkeypatch.setattr(alerts, "_peak_unconfirmed", lambda site, now, today: True)
    monkeypatch.setattr(plans, "current", lambda site_id: plan)
    rules = lambda: {a["rule"] for a in alerts.listing("active", sid)}
    with world.use():
        sites.set_tier_capable(sid, "advisory_writes")
        sites.set_tier_allowed(sid, "advisory_writes", "test")
        telemetry.ingest(sid, [{"ts": now // 60 * 60 - 2 * 86400, "grid_present": 1}], "edge")
        alerts.evaluate(sid, now)
        assert not {"apply_unconfirmed", "plan_failed"} & rules()             # a plan, but two days of data
        telemetry.ingest(sid, [{"ts": now // 60 * 60 - 8 * 86400, "grid_present": 1}], "edge")
        alerts.evaluate(sid, now)
        assert {"apply_unconfirmed", "plan_failed"} <= rules()
        assert alerts.settled(sid, None, now) is False                          # no plan in force: not settled
        alerts.evaluate(sid, now - 2 * 86400)                                   # back when the data was 6 days old: closed again
        assert not {"apply_unconfirmed", "plan_failed"} & rules()


_ = (json, pytest, tariffs)


# ================================================================================================ round 3, second pass (the verifier's open items)

# ------------------------------------------------------------------------------------------------ routed-opt15-outage-resolve: a backlog older than its event

def test_a_backlog_chunk_older_than_the_event_does_not_run_its_re_solve_on_the_wait(world, monkeypatch):
    """The verifier's probe: telemetry stalls at m0-180 and outage_start arrives at m0+5. The backlog comes oldest first 130 s later: its first chunk
    (m0-120 and m0-60, grid present) is past RESOLVE_WAIT_S from the event but ends before it, so it must not tick on telemetry that cannot show the
    outage nor clear the pending re-solve; the chunk that reaches the event's minute and after shows it and runs it."""
    sid = world.site
    kh = world.key(["telemetry:write"])
    box = {}
    ticks = _active_with_ticks(world, monkeypatch, box)
    m0 = _now() // 60 * 60 - 3600
    box["clock"] = clock = _clock(monkeypatch, m0 - 175)
    post_t, post_e = _posts(world, sid, kh)
    assert post_t(1, [[m0 - 240, 1], [m0 - 180, 1]]).status_code == 200
    clock["now"] = m0 + 5
    assert post_e(1, [{"ts": _iso(m0 + 5), "type": "outage_start"}]).json()["resolve"] == "waiting_for_telemetry"
    clock["now"] = m0 + 135
    assert clock["now"] - (m0 + 5) > routes_edge.RESOLVE_WAIT_S
    r = post_t(2, [[m0 - 120, 1], [m0 - 60, 1]])
    assert r.status_code == 200 and r.json()["resolve"] == "waiting_for_telemetry" and ticks == [], r.text
    with world.use():
        assert sites.state(sid)["resolve_pending"] == {"minute": m0, "event": "outage_start", "since": m0 + 5}
    r = post_t(3, [[m0, 0], [m0 + 60, 0], [m0 + 120, 0]])
    assert r.status_code == 200 and r.json()["resolve"] == "scheduled" and ticks == [m0 + 135], r.text
    with world.use():
        assert sites.state(sid).get("resolve_pending") is None


def test_the_wait_runs_a_re_solve_only_once_telemetry_reaches_past_the_events_minute(world, monkeypatch):
    """A reading in the event's own minute may predate the event (the grid back 40 s into a minute whose reading still says down): past RESOLVE_WAIT_S it
    still waits. A reading from the next minute on that disagrees with the event is telemetry newer than it: the wait then runs the re-solve."""
    sid = world.site
    kh = world.key(["telemetry:write"])
    box = {}
    ticks = _active_with_ticks(world, monkeypatch, box)
    m = _now() // 60 * 60 - 1800
    box["clock"] = clock = _clock(monkeypatch, m + 45)
    post_t, post_e = _posts(world, sid, kh)
    assert post_t(1, [[m - 120, 0], [m - 60, 0]]).status_code == 200
    assert post_e(1, [{"ts": _iso(m + 40), "type": "outage_end"}]).json()["resolve"] == "waiting_for_telemetry"
    clock["now"] = m + 45 + routes_edge.RESOLVE_WAIT_S + 5
    r = post_t(2, [[m, 0]])
    assert r.json()["resolve"] == "waiting_for_telemetry" and ticks == [], r.text
    r = post_t(3, [[m + 60, 0]])
    assert r.json()["resolve"] == "scheduled" and ticks == [clock["now"]], r.text


# ------------------------------------------------------------------------------------------------ ask-plan-held: a pause with a plan still stored as active

@pytest.mark.parametrize("helper", ["fallback", "plans.held_info"])
def test_a_pause_or_release_answers_no_plan_while_the_plan_of_before_is_still_active(world, monkeypatch, helper):
    """The verifier's probe: with an active plan, a pause (and a release) left GET /plan serving that plan as in force, with no held, until the next tick
    stored a held plan; plans.active_at(now) was already None."""
    sid = world.site
    admin, viewer = world.c["admin"], world.c["viewer"]
    if helper == "fallback":
        monkeypatch.delattr(plans, "held_info", raising=False)
    elif not hasattr(plans, "held_info"):
        pytest.skip("the optimiser group's plans.held_info is not there yet")
    monkeypatch.setattr(plans, "tick", lambda site_id, *a, **k: {"status": "active", "plan_id": "p-active"})
    _plan_row(world, sid, "p-active", "active", "", (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds"))
    assert viewer.get(f"/api/sites/{sid}/plan").json()["plan"]["id"] == "p-active"
    for action, kind, reason in (("pause", "pause_control", "control_paused"), ("release", "force_native", "released_to_native")):
        assert admin.post(f"/api/sites/{sid}/control/{action}", json={"hours": 2}).status_code == 200
        with world.use():
            until = next(o["until_ts"] for o in sites.overrides(sid) if o["kind"] == kind)
            assert plans.active_at(sid, _now()) is None
        r = viewer.get(f"/api/sites/{sid}/plan")
        assert r.status_code == 200 and r.json() == {"plan": None, "held": {"reason": reason, "until": until, "plan_id": None}}, r.text
        assert admin.post(f"/api/sites/{sid}/control/resume", json={}).status_code == 200
        assert viewer.get(f"/api/sites/{sid}/plan").json()["plan"]["id"] == "p-active"


# ------------------------------------------------------------------------------------------------ ask-drafts: owned by the member, not the display name

def _ali(w, n: int):
    import identities
    # administrators: saving a draft is the administrator's (audit role-operator-07); the name, not the role, is what these tests are about
    return identities.client(f"ali.{n}@fix.test", "admin", w.company, "member", name="Ali Khan", raise_server_exceptions=False)


def test_two_members_with_one_name_keep_their_own_drafts(world):
    """The verifier's probe: two members of one company both named 'Ali Khan'. The second one's GET /api/drafts listed the first one's draft and body,
    and his PUT of the same draft id wrote over it."""
    ali1, ali2 = _ali(world, 1), _ali(world, 2)
    assert ali1.put("/api/drafts/d-ali", json={"general": {"name": "Ali's mill"}}).status_code == 200
    assert ali2.get("/api/drafts").json() == []
    assert ali2.get("/api/drafts/d-ali").json() == {}
    r = ali2.put("/api/drafts/d-ali", json={"general": {"name": "Overwritten"}})
    assert r.status_code == 409, r.text
    assert [(d["draft_id"], d["body"]["general"]["name"]) for d in ali1.get("/api/drafts").json()] == [("d-ali", "Ali's mill")]
    assert ali2.put("/api/drafts/d-ali-2", json={"general": {"name": "Ali two's"}}).status_code == 200
    assert [d["draft_id"] for d in ali2.get("/api/drafts").json()] == ["d-ali-2"]
    assert [d["draft_id"] for d in ali1.get("/api/drafts").json()] == ["d-ali"]


def test_drafts_stored_under_a_name_go_to_the_one_member_who_bears_it(world):
    """Rows stored under the display name before drafts were kept by id: the only member of that name takes them over; a name two members share
    lists them to neither, and they are not written over."""
    ali1, ali2 = _ali(world, 1), _ali(world, 2)
    a = world.c["admin"]
    for c in (ali1, ali2, a):
        assert c.get("/api/meta").status_code == 200                              # every member's row is made
    with world.use():
        admin_name = next(m["name"] for m in store_members.members() if m["email"] == "admin@fix.test")
        with store.connect() as con:
            for draft_id, person in (("old-admin", admin_name), ("old-ali", "Ali Khan")):
                con.execute("INSERT INTO site_drafts (draft_id, site_id, person, body, updated) VALUES (?,?,?,?,?)",
                            (draft_id, None, person, json.dumps({"general": {"name": draft_id}}), "2026-11-01T10:00:00+00:00"))
    assert [d["draft_id"] for d in a.get("/api/drafts").json()] == ["old-admin"]
    assert a.get("/api/drafts/old-admin").json()["body"] == {"general": {"name": "old-admin"}}
    assert a.put("/api/drafts/old-admin", json={"general": {"name": "resumed"}}).status_code == 200
    assert ali1.get("/api/drafts").json() == [] and ali2.get("/api/drafts").json() == []
    assert ali1.put("/api/drafts/old-ali", json={}).status_code == 409
    with world.use(), store.connect() as con:
        rows = {r["draft_id"]: (r["person"], json.loads(r["body"])) for r in con.execute("SELECT draft_id, person, body FROM site_drafts")}
    assert rows["old-ali"] == ("Ali Khan", {"general": {"name": "old-ali"}})
    assert rows["old-admin"][0] != admin_name and rows["old-admin"][1] == {"general": {"name": "resumed"}}


def test_an_autosave_naming_a_deleted_site_in_its_body_still_saves(world):
    """The wizard sends the site it made in every autosave's body: one deleted meanwhile is not recorded and the save stands (round 3's first pass
    answered 422). The query parameter still names a site or is refused."""
    a = world.c["admin"]
    two = world.other
    assert a.put("/api/drafts/d-del", json={"site_id": two, "general": {"name": "Two"}}).json()["site_id"] == two
    with world.use():
        sites.delete(two, "test")
    r = a.put("/api/drafts/d-del", json={"site_id": two, "general": {"name": "Two", "city": "Multan"}})
    assert r.status_code == 200 and r.json()["body"]["general"]["city"] == "Multan", r.text
    r = a.put("/api/drafts/d-new", json={"site_id": two, "general": {}})
    assert r.status_code == 200 and r.json()["site_id"] is None, r.text
    assert a.put("/api/drafts/d-new", params={"site_id": two}, json={}).status_code == 422


# ------------------------------------------------------------------------------------------------ regression-tier-signoff-put-state-race

def test_a_tier_save_keeps_an_outage_events_pending_re_solve_stored_meanwhile(world, monkeypatch):
    """The verifier's probe: the tier save's state write was held between its read and its write while an outage_start stored its pending re-solve;
    once the tier save finished, resolve_pending was gone (sites.put_state reads and writes with no lock)."""
    sid = world.site
    kh = world.key(["telemetry:write"])
    box = {}
    _active_with_ticks(world, monkeypatch, box)
    m = _now() // 60 * 60 - 1800
    box["clock"] = _clock(monkeypatch, m + 5)
    _, post_e = _posts(world, sid, kh)
    real_dumps = json.dumps
    started, go, hold = threading.Event(), threading.Event(), {"on": True}

    def held_dumps(obj, *a, **k):
        # the tier save's state, read and about to be written: held here while the event has its chance
        if hold["on"] and isinstance(obj, dict) and "tier_signoff" in obj:
            hold["on"] = False
            started.set()
            go.wait(30)
        return real_dumps(obj, *a, **k)

    monkeypatch.setattr(json, "dumps", held_dumps)
    answers = {}
    admin = world.c["admin"]
    t1 = threading.Thread(target=lambda: answers.__setitem__("tier", admin.put(f"/api/sites/{sid}/tier", json={"tier": "monitor", "signoff": "A. Khan"})))
    t2 = threading.Thread(target=lambda: answers.__setitem__("event", post_e(1, [{"ts": _iso(m + 5), "type": "outage_start"}])))
    t1.start()
    try:
        assert started.wait(30)
        t2.start()
        t2.join(2)                     # the tier save's write transaction is open: the event waits for it (with no lock it was stored meanwhile)
    finally:
        go.set()
        t1.join(60)
        t2.join(60)
    assert answers["tier"].status_code == 200, answers["tier"].text
    assert answers["event"].status_code == 200 and answers["event"].json()["resolve"] == "waiting_for_telemetry", answers["event"].text
    with world.use():
        st = sites.state(sid)
    assert st["tier_signoff"]["signoff"] == "A. Khan"
    assert st["resolve_pending"] == {"minute": m, "event": "outage_start", "since": m + 5}


# ------------------------------------------------------------------------------------------------ savings litres source (lead, after round 3)

def test_the_savings_answer_names_where_the_genset_litres_came_from(world, monkeypatch):
    """energy.litres_source is served (verify.litres_used) so the page can mark curve litres "Estimated"; a failure there never fails the page."""
    sid = world.site
    from gridlytics import verify
    monkeypatch.setattr(verify, "litres_used", lambda *a, **k: {"litres": 12.34, "litres_source": "curve", "litres_metered": 0.0, "litres_curve": 12.34})
    for role in ("admin", "viewer"):
        e = world.c[role].get(f"/api/sites/{sid}/savings").json()["energy"]
        assert e["litres_source"] == "curve" and e["litres"] == 12.3, e
    monkeypatch.setattr(verify, "litres_used", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = world.c["admin"].get(f"/api/sites/{sid}/savings")
    assert r.status_code == 200 and r.json()["energy"]["litres_source"] is None
    # a range over about two months keeps the metered sum and names no source
    assert world.c["admin"].get(f"/api/sites/{sid}/savings?start=2026-01-01&end=2026-06-30").json()["energy"]["litres_source"] is None
