"""Contract test 6 (EMS_BUILD_PLAN.md Part 7.4, U3 5.8 item 6; R6 I6): link loss.

Ten minutes without the cloud: the edge keeps running its cached plan minute for minute (flagged unattended from five missed heartbeats, 300 s) to
its valid_until, and only then falls back; the cloud says the edge is offline once its data is five minutes old. When the link is back, the
edge's store-and-forward sends its backlog: every batch is stored whatever order it arrives in, a batch sent twice is stored once, and the site's
minutes come out complete and in time order, with the events of the gap at their own times. Round 7, audit tests-and-perf-15."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from contract_kit import PKT, ReferenceEdge, edge, iso, now, plan_row, table  # noqa: F401  (edge is a fixture)
from gridlytics import delivery, routes_sites, sites, store, telemetry, twin
from test_fix_api import world  # noqa: F401  (world is a fixture)


def test_ten_minutes_silent_the_edge_runs_its_cached_plan_unattended_to_its_end_and_the_cloud_says_it_is_offline(edge):
    today = datetime.now(PKT).date()
    with edge.use():
        plan_row(edge.sid, {d: table(d, [(0, 6, "charge_grid"), (6, 18, "hold"), (18, 22, "discharge"), (22, 24, "self_use")])
                            for d in (today, today + timedelta(days=1))}, valid_s=6 * 3600)
        ref = ReferenceEdge(edge.sid, delivery.public_key(edge.sid))
        site = sites.full(edge.sid)
    msg = edge.pull().json()
    assert ref.take(msg) is None and msg["fallback"]["on_link_loss_after_s"] == 300
    lost = now() // 60 * 60
    until = int(datetime.fromisoformat(msg["valid_until"]).timestamp())
    ts = np.arange(lost - 1800, until + 3600, 60)
    linked, _ = ref.runs(site, ts)
    pol, state = ref.runs(site, ts, link_lost_at=lost)
    names = np.array([twin.FALLBACK_STATES[int(s)] for s in state])
    # the cached plan continues: minute for minute what the edge ran with the link up
    for k in ("mode", "floor", "target", "grid_charge", "max_dis", "max_ch"):
        assert np.array_equal(pol[k], linked[k]), k
    assert set(names[ts < lost + 300]) == {"plan"}                               # under five missed heartbeats: nothing to flag
    assert set(names[(ts >= lost + 300) & (ts < until)]) == {"unattended"}     # ten minutes silent: unattended, still on the plan
    assert names[np.flatnonzero(ts == lost + 600)[0]] == "unattended"
    assert set(names[ts >= until]) == {"native"}                               # the plan's end, not the link's loss, brings the fallback
    # the cloud: the edge's data is ten minutes old, so the control state says the edge is offline (five minutes is the mark)
    with edge.use():
        s = sites.get(edge.sid)
        sites.put_state(edge.sid, edge_last_seen=lost)
        assert routes_sites.control_state(s, {"ts": lost - 60}, lost + 600)["key"] == "edge_offline"
        assert routes_sites.control_state(s, {"ts": lost + 540}, lost + 600)["key"] != "edge_offline"


def _rows(t0: int, minutes: int, grid=lambda t: 1) -> list[list]:
    return [[t0 + 60 * k, grid(t0 + 60 * k), 900 + k] for k in range(minutes)]


def test_store_and_forward_delivers_every_row_in_order_whatever_order_the_backlog_arrives_in(edge):
    start = now() // 3600 * 3600 - 3 * 3600                                    # a three-hour gap ending an hour ago
    out_from, out_to = start + 3600, start + 3600 + 25 * 60                     # an outage in the gap
    grid = lambda t: 0 if out_from <= t < out_to else 1                         # noqa: E731
    batches = [{"contract": "ems-edge/1.0", "seq": 100 + n, "sent_at": iso(now()), "columns": ["ts", "grid_present", "pv_power_w"],
                "rows": _rows(start + n * 1800, 30, grid)} for n in range(6)]
    # sent newest first, as a store-and-forward queue may when the live batch goes before the backlog, and one sent twice
    for b in [batches[5], *batches[:5], batches[2]]:
        r = edge.post("telemetry", b)
        assert r.status_code == 200, r.text
    assert r.json()["duplicate"] is True
    events = {"contract": "ems-edge/1.0", "seq": 9, "sent_at": iso(now()),
              "events": [{"ts": iso(out_from), "type": "outage_start"}, {"ts": iso(out_to), "type": "outage_end"},
                         {"ts": iso(start + 60), "type": "link_lost"}]}
    assert edge.post("events", events).status_code == 200
    with edge.use():
        raw = telemetry.raw(edge.sid, start, start + 3 * 3600, columns=["grid_present", "pv_power_w"])
        ts = np.asarray(raw["ts"])
        assert len(ts) == 180 and np.all(np.diff(ts) == 60) and int(ts[0]) == start               # every minute, once, in time order
        down = np.asarray(raw["grid_present"]) == 0
        assert int(down.sum()) == 25 and int(ts[down][0]) == out_from                              # the outage at its own minutes
        with store.connect() as con:
            kept = [r["type"] for r in con.execute("SELECT type FROM events WHERE site_id = ? ORDER BY ts", (edge.sid,))]
    assert kept == ["link_lost", "outage_start", "outage_end"]
