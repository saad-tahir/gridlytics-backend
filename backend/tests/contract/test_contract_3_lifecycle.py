"""Contract test 3 (EMS_BUILD_PLAN.md Part 7.4, U3 5.8 item 3; tracker DLV-03, DLV-04): the plan lifecycle.

Receive (the edge pulls and takes the message), ack (received, then applied with what it wrote and read back, which the control page shows per
primitive), apply at the right minute (each slot from its start minute, Pakistan time), read-back, the expiry fallback (the inverter's native
settings from valid_until, never below the block's floor; the cloud serves no plan after it, and an edge refuses a message past it) and supersede
(a newer plan names the one before, and the edge runs the newest). The edge's half runs through ReferenceEdge and twin.fallback_policy. Round 7,
audit tests-and-perf-15."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from contract_kit import PKT, ReferenceEdge, edge, iso, now, plan_row, stamped, table  # noqa: F401  (edge is a fixture)
from gridlytics import contract, delivery, sites, store, twin
from test_fix_api import world  # noqa: F401  (world is a fixture)

SPANS = [(0, 6, "charge_grid"), (6, 18, "hold"), (18, 22, "discharge"), (22, 24, "self_use")]


def _taken(e, valid_s: int = 2 * 86400) -> tuple[ReferenceEdge, dict, dict]:
    """Today's and tomorrow's tables as one plan, pulled and taken by the site's edge; the site as the twin reads it."""
    today = datetime.now(PKT).date()
    with e.use():
        plan_row(e.sid, {d: table(d, SPANS) for d in (today, today + timedelta(days=1))}, valid_s=valid_s)
        key, site = delivery.public_key(e.sid), sites.full(e.sid)
    r = e.pull()
    assert r.status_code == 200, r.text
    ref = ReferenceEdge(e.sid, key)
    assert ref.take(r.json()) is None
    return ref, r.json(), site


def _mode(pol: dict, ts: np.ndarray, at: int) -> str:
    return twin.MODES[int(pol["mode"][int(np.flatnonzero(ts == at)[0])])]


def test_a_plan_is_received_acknowledged_applied_and_read_back(edge):
    ref, msg, _ = _taken(edge)
    pid = msg["plan_id"]
    assert edge.post("ack", {**stamped("ack", edge.sid, 1, plan_id=pid), "status": "received", "per_device": []}).status_code == 200
    # applied: what was asked, written and read back per primitive (U3 5.6's per_device)
    applied = [{"primitive": "soc_floor_pct", "requested": 20, "written": 20, "readback": 20, "readback_ok": True},
               {"primitive": "battery_power_w", "requested": 50000, "written": 12000, "readback": 12000, "readback_ok": True}]
    ack = {**stamped("ack", edge.sid, 2, plan_id=pid), "status": "applied",
           "per_device": [{"device_id": "inv1", "status": "applied", "writes_used_today": {"tou_table": 1}, "applied": applied, "rejected": []}]}
    assert edge.post("ack", ack).status_code == 200
    beat = {**stamped("heartbeat", edge.sid, 1, plan_id=pid), "control_state": "executing", "plan_valid_until": msg["valid_until"]}
    assert edge.post("heartbeat", beat).status_code == 200
    with edge.use(), store.connect() as con:
        assert [r["status"] for r in con.execute("SELECT status FROM acks WHERE site_id = ? ORDER BY rowid", (edge.sid,))] == ["received", "applied"]
    rb = edge.world.c["viewer"].get(f"/api/sites/{edge.sid}/control/readback").json()
    assert rb["plan_id"] == pid and rb["ack"]["outcome"] == "ok", rb
    lines = {ln["primitive"]: ln for ln in rb["primitives"]}
    assert lines["soc_floor_pct"]["read_back"] == 20 and lines["soc_floor_pct"]["match"] is True
    assert lines["battery_power_w"]["written"] == 12000 and lines["battery_power_w"]["read_back"] == 12000
    c = edge.world.c["viewer"].get(f"/api/sites/{edge.sid}/control").json()
    assert c["control"]["key"] == "following", c["control"]


def test_each_slot_is_applied_from_its_start_minute_pakistan_time(edge):
    ref, msg, site = _taken(edge)
    day = datetime.now(PKT).date()
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    ts = np.arange(t0, t0 + 86400, 60)
    pol, state = ref.runs(site, ts)
    assert set(twin.FALLBACK_STATES[int(s)] for s in state) == {"plan"}
    for a, b, mode in SPANS:
        start, end = t0 + a * 3600, t0 + b * 3600
        assert _mode(pol, ts, start) == mode and _mode(pol, ts, end - 60) == mode, (a, mode)
        if a:
            assert _mode(pol, ts, start - 60) != mode, (a, mode)                 # not a minute early
    # the boundaries are Pakistan wall-clock: 18:00 PKT is 13:00 UTC
    assert datetime.fromtimestamp(t0 + 18 * 3600, PKT).hour == 18 and msg["timezone"] == "Asia/Karachi"
    assert [(s["start"], s["end"]) for s in msg["mode_a"]["days"][0]["slots"]] == [("00:00", "06:00"), ("06:00", "18:00"), ("18:00", "22:00"),
                                                                                    ("22:00", "00:00")]


def test_at_valid_until_the_edge_runs_its_fallback_and_the_cloud_serves_no_plan(edge):
    ref, msg, site = _taken(edge, valid_s=3 * 3600)
    until = contract.epoch(msg["valid_until"])
    ts = np.arange(now() // 60 * 60, until + 3 * 3600, 60)
    pol, state = ref.runs(site, ts)
    names = np.array([twin.FALLBACK_STATES[int(s)] for s in state])
    assert set(names[ts < until]) == {"plan"} and set(names[ts >= until]) == {"native"}
    # the native settings, never below the fallback block's floor
    assert np.all(pol["floor"][ts >= until] >= msg["fallback"]["min_soc_floor_pct"] / 100.0 - 1e-9)
    assert msg["fallback"]["on_expiry"] == "inverter_native"
    # an edge refuses the message once it has run out, and the cloud, its plan past valid_until, serves none
    assert ReferenceEdge(edge.sid, ref.key).refusal(msg, at=until) == "EXPIRED"
    with edge.use(), store.connect() as con:
        con.execute("UPDATE plans SET valid_until = ? WHERE id = ?", (iso(now() - 60), msg["plan_id"]))
    r = edge.pull()
    assert r.status_code == 404 and r.json()["code"] == "plan_expired", r.text


def test_a_newer_plan_supersedes_the_one_before_and_the_edge_runs_the_newest(edge):
    ref, first, site = _taken(edge)
    day = datetime.now(PKT).date()
    later = [(0, 6, "charge_grid"), (6, 16, "hold"), (16, 22, "discharge"), (22, 24, "self_use")]
    with edge.use():
        plan_row(edge.sid, {day: table(day, later)})
    second = edge.pull().json()
    assert second["supersedes"] == first["plan_id"] and second["seq"] > first["seq"]
    assert ref.take(second) is None
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    ts = np.arange(t0, t0 + 86400, 60)
    pol, _ = ref.runs(site, ts)
    assert _mode(pol, ts, t0 + 16 * 3600) == "discharge"                        # the newest plan's 16:00, not the first's 18:00
    # the first, delivered late after a link loss, is not run again
    assert ref.take(first) == "replayed" and ref.taken[-1]["plan_id"] == second["plan_id"]
