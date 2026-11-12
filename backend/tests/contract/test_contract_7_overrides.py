"""Contract test 7 (EMS_BUILD_PLAN.md Part 7.4, U3 5.8 item 7; R6 4): overrides and template changes disable control.

A local override, a template or firmware change and a read-back mismatch leave the device in a state the cloud does not know, under either the
contract's name or U3's: the event is stored as sent, control is paused (a pause override, shown on the control chip), the edge's next pull is a
signed release with no slots, so nothing more is written, until a person resumes; the edge's own report that it disabled control is shown too.
Round 7, audit tests-and-perf-15."""
from __future__ import annotations

from datetime import datetime

import pytest

from contract_kit import PKT, ReferenceEdge, edge, iso, now, plan_row, table  # noqa: F401  (edge is a fixture)
from gridlytics import contract, delivery, routes_edge, sites, store
from test_fix_api import world  # noqa: F401  (world is a fixture)

#: each event that pauses control, under this contract's name and U3's
PAUSING = ["local_override", "override_local", "template_change", "template_change_detected", "firmware_change", "firmware_change_detected",
           "readback_mismatch"]


@pytest.mark.parametrize("kind", PAUSING)
def test_an_override_or_template_change_pauses_control_and_the_edge_is_sent_a_release_until_a_person_resumes(edge, kind):
    assert contract.event_meaning(kind) in routes_edge.PAUSE_EVENTS
    day = datetime.now(PKT).date()
    with edge.use():
        plan_row(edge.sid, {day: table(day, [(0, 18, "hold"), (18, 22, "discharge"), (22, 24, "hold")])})
        ref = ReferenceEdge(edge.sid, delivery.public_key(edge.sid))
    assert ref.take(edge.pull().json()) is None and ref.taken[-1]["type"] == "plan"
    batch = {"contract": "ems-edge/1.0", "seq": 1, "sent_at": iso(now()),
             "events": [{"ts": iso(now() - 30), "type": kind, "source": "inv1", "detail": {"by": "installer panel"}}]}
    r = edge.post("events", batch)
    assert r.status_code == 200 and r.json()["stored"] == 1 and "unknown_types" not in r.json(), r.text
    with edge.use():
        (o,) = sites.overrides(edge.sid)
        assert o["kind"] == "pause_control" and o["reason"] == routes_edge.PAUSE_REASON and o["author"] == "edge"
        with store.connect() as con:
            assert [x["type"] for x in con.execute("SELECT type FROM events WHERE site_id = ?", (edge.sid,))] == [kind]   # stored as sent
    # nothing more is written: the edge's next message is a signed release with no slots, which it takes
    rel = edge.pull().json()
    assert rel["type"] == "release" and rel["mode_a"]["days"] == [] and rel["release"]["reason"] == "control_paused", rel.get("release")
    assert ref.take(rel) is None
    c = edge.world.c["viewer"].get(f"/api/sites/{edge.sid}/control").json()["control"]
    assert c["key"] == "paused", c
    # the same event sent again pauses nothing more; a person resuming brings the plan back to the edge
    assert edge.post("events", {**batch, "sent_at": iso(now() + 5)}).json()["duplicate"] is True
    with edge.use():
        assert len(sites.overrides(edge.sid)) == 1
    assert edge.world.c["admin"].post(f"/api/sites/{edge.sid}/control/resume", json={}).status_code == 200
    with edge.use():
        assert sites.overrides(edge.sid) == []


def test_control_the_edge_disabled_is_shown_until_it_says_it_is_enabled_and_other_events_pause_nothing(edge):
    day = datetime.now(PKT).date()
    with edge.use():
        plan_row(edge.sid, {day: table(day, [(0, 18, "hold"), (18, 22, "discharge"), (22, 24, "hold")])})
    edge.pull()
    ev = lambda seq, kind, at, **d: edge.post("events", {"contract": "ems-edge/1.0", "seq": seq, "sent_at": iso(now()),  # noqa: E731
                                                      "events": [{"ts": iso(at), "type": kind, "detail": d}]})
    assert ev(1, "control_disabled", now() - 60, cause="installer session").status_code == 200
    c = edge.world.c["viewer"].get(f"/api/sites/{edge.sid}/control").json()["control"]
    assert c["key"] == "edge_disabled" and "installer session" in c["label"], c
    assert ev(2, "control_enabled", now() - 10).status_code == 200
    assert edge.world.c["viewer"].get(f"/api/sites/{edge.sid}/control").json()["control"]["key"] != "edge_disabled"
    # events that leave the device known pause nothing
    for n, kind in enumerate(("outage_start", "outage_end", "genset_start", "plan_applied", "reboot", "edge_reboot", "data_gap"), start=3):
        assert ev(n, kind, now() - 5).status_code == 200
    with edge.use():
        assert sites.overrides(edge.sid) == []
    assert edge.pull().json()["type"] == "plan"
