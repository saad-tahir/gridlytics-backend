"""Contract test 2 (EMS_BUILD_PLAN.md Part 7.4, U3 5.8 item 2; tracker DLV-03): the signature's round trip, replay rejection and foreign-site
rejection, both ways.

Cloud to edge: the plan message an edge pulls verifies with the key its edge was given at enrolment, over the RFC 8785 bytes an edge in any language
makes; any change to it fails; each pull is a new message (a new msg_id, a sequence above the last), so an edge that refuses what it has seen
refuses a replayed or an older message and never a fresh one; a message for another site fails its signature at this site's edge, and a site's key
cannot pull another site's plan. Edge to cloud: a message sent again is stored once, and a message naming another site, or sent to another site's
path, is refused with nothing stored. Round 7, audit tests-and-perf-15."""
from __future__ import annotations

import copy
from datetime import datetime

import pytest

from contract_kit import PKT, STREAMS, ReferenceEdge, closed_loop, edge, plan_row, stamped, table  # noqa: F401  (edge is a fixture)
from gridlytics import delivery, store
from test_fix_api import world  # noqa: F401  (world is a fixture)


def _plan(e) -> None:
    day = datetime.now(PKT).date()
    with e.use():
        plan_row(e.sid, {day: table(day, [(0, 6, "charge_grid"), (6, 18, "hold"), (18, 22, "discharge"), (22, 24, "self_use")])})


def _key(e, site_id: str | None = None) -> dict:
    with e.use():
        return delivery.public_key(site_id or e.sid)


def test_a_pulled_plan_verifies_with_the_enrolled_key_and_any_change_to_it_does_not(edge):
    _plan(edge)
    msg = edge.pull().json()
    key = _key(edge)
    assert msg["signature"]["key_id"] == key["key_id"]
    assert ReferenceEdge(edge.sid, key).take(msg) is None
    changes = {
        "a slot's floor": lambda m: m["mode_a"]["days"][0]["slots"][2].update(soc_floor_pct=5.0),
        "the validity": lambda m: m.update(valid_until="2099-01-01T00:00:00+00:00"),
        "the sequence": lambda m: m.update(seq=m["seq"] + 1000),
        "the fallback floor": lambda m: m["fallback"].update(min_soc_floor_pct=0.0),
        "a member added": lambda m: m["notes"].update(extra=1),
        "the signature": lambda m: m["signature"].update(value=m["signature"]["value"][::-1]),
        "another key's id": lambda m: m["signature"].update(key_id="k-other"),
    }
    for what, change in changes.items():
        bad = copy.deepcopy(msg)
        change(bad)
        assert ReferenceEdge(edge.sid, key).take(bad) == "SIG_INVALID", what
    unsigned = {k: v for k, v in msg.items() if k != "signature"}
    assert ReferenceEdge(edge.sid, key).take(unsigned) == "SIG_INVALID"


def test_each_pull_is_a_new_message_so_a_replayed_or_older_one_is_refused_and_a_fresh_one_taken(edge):
    _plan(edge)
    a, b, c = (edge.pull().json() for _ in range(3))
    assert a["seq"] < b["seq"] < c["seq"] and len({a["msg_id"], b["msg_id"], c["msg_id"]}) == 3
    assert a["plan_id"] == b["plan_id"] == c["plan_id"] and a["mode_a"] == b["mode_a"]       # the same plan, written only if changed (U3 5.4)
    ref = ReferenceEdge(edge.sid, _key(edge))
    assert ref.take(b) is None
    assert ref.take(b) == "replayed"                                          # the same message again
    assert ref.take(a) == "replayed"                                          # an older one delivered late
    assert ref.take(c) is None and ref.taken[-1]["msg_id"] == c["msg_id"]
    # a newer plan supersedes: its message is above the last and names the plan before it
    _plan(edge)
    d = edge.pull().json()
    assert d["seq"] > c["seq"] and d["plan_id"] != c["plan_id"] and d["supersedes"] == c["plan_id"]
    assert ref.take(d) is None


def test_a_message_for_another_site_is_refused_by_this_sites_edge_and_this_sites_key_cannot_pull_it(edge):
    other = edge.world.other
    day = datetime.now(PKT).date()
    with edge.use():
        closed_loop(other)
        plan_row(other, {day: table(day, [(0, 18, "hold"), (18, 22, "discharge"), (22, 24, "hold")])})
        theirs = delivery.public_key(other)
    assert theirs["key_id"] != _key(edge)["key_id"] and theirs["public_key"] != _key(edge)["public_key"]
    msg = edge.world.anon.get(f"/api/sites/{other}/plans/current/message", headers=edge.world.key(["plans:read"], role="viewer", site=other)).json()
    assert ReferenceEdge(other, theirs).take(msg) is None                     # its own edge takes it
    assert ReferenceEdge(edge.sid, _key(edge)).take(msg) == "SIG_INVALID"     # signed with the other site's key
    # the same message re-addressed to this site still fails: the signature covers site_id
    assert ReferenceEdge(edge.sid, _key(edge)).take({**msg, "site_id": edge.sid}) == "SIG_INVALID"
    # this site's key is bound to its site: the other site's plan is not served to it
    r = edge.world.anon.get(f"/api/sites/{other}/plans/current/message", headers=edge.read)
    assert r.status_code == 403 and r.json()["code"] == "wrong_site", r.text


@pytest.mark.parametrize("stream", [s for s in STREAMS if s != "heartbeat"])
def test_an_edge_message_sent_again_is_stored_once(edge, stream):
    body = stamped(stream, edge.sid, 7)
    first = edge.post(stream, body)
    assert first.status_code == 200 and not first.json().get("duplicate"), first.text
    with edge.use(), store.connect() as con:
        seen = con.execute("SELECT count(*) FROM edge_seen WHERE site_id = ? AND stream = ?", (edge.sid, stream)).fetchone()[0]
    # a retry stamped again (a new sent_at and msg_id) is still the same message
    again = edge.post(stream, {**body, "sent_at": "2026-11-06T10:00:00Z", "msg_id": "retry-1"})
    assert again.status_code == 200 and again.json()["duplicate"] is True and again.json()["ack"] == 7, again.text
    with edge.use(), store.connect() as con:
        assert con.execute("SELECT count(*) FROM edge_seen WHERE site_id = ? AND stream = ?", (edge.sid, stream)).fetchone()[0] == seen == 1


@pytest.mark.parametrize("stream", STREAMS)
def test_an_edge_message_naming_or_sent_to_another_site_is_refused_with_nothing_stored(edge, stream):
    other = edge.world.other
    r = edge.post(stream, stamped(stream, other, 1))
    assert r.status_code == 403 and r.json()["code"] == "wrong_site", r.text
    r = edge.post(stream, stamped(stream, other, 1), site=other)
    assert r.status_code == 403 and r.json()["code"] == "wrong_site", r.text
    with edge.use(), store.connect() as con:
        assert con.execute("SELECT count(*) FROM edge_seen").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM acks").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM capabilities").fetchone()[0] == 0
