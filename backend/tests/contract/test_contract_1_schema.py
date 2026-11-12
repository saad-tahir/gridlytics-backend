"""Contract test 1 (EMS_BUILD_PLAN.md Part 7.4, U3 5.8 item 1): schema validation of every message.

Every message of ems-edge/1.0 has its schema file; the example the contract publishes for each edge message is taken by the live route; each
member a schema file lists as required is asked for, and a member it does not list is refused, with 422 SCHEMA naming it, the same way every time
the message is sent, with nothing stored; and the plan message the cloud signs is its schema's (every required member, no other). The schema files
are the pydantic models (test_the_published_schema_files_are_the_contract_models pins them), so the model is the validator here (no jsonschema
package is shipped). Round 7, audit tests-and-perf-15."""
from __future__ import annotations

from datetime import datetime

import pytest

from contract_kit import PKT, STREAMS, edge, iso, now, plan_row, schema, stamped, table  # noqa: F401  (edge is a fixture)
from gridlytics import contract, sites, store, telemetry
from test_fix_api import world  # noqa: F401  (world is a fixture)

#: where each stream's accepted message is kept (the heartbeat in the site's carried state, the others in a table of their own)
KEPT = {"events": "SELECT count(*) FROM events WHERE site_id = ?", "ack": "SELECT count(*) FROM acks WHERE site_id = ?", "capabilities": "SELECT count(*) FROM capabilities WHERE site_id = ?"}


def _kept(e, stream: str) -> int:
    with e.use():
        if stream == "heartbeat":
            return int("edge_heartbeat" in sites.state(e.sid))
        if stream == "telemetry":
            return int(telemetry.latest(e.sid) is not None)
        with store.connect() as con:
            return con.execute(KEPT[stream], (e.sid,)).fetchone()[0]


def test_every_message_has_its_schema_file_and_example():
    assert set(contract.MODELS) == {*STREAMS, "plan"}
    for name in contract.MODELS:
        s = schema(name)
        assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema" and s["$id"] == f"ems-edge/1.0/{name}.schema.json"
        assert s.get("additionalProperties") is False, name                      # a member the contract does not name is refused
    for name in STREAMS:
        contract.MODELS[name].model_validate(stamped(name, "s1", 1))


@pytest.mark.parametrize("stream", STREAMS)
def test_the_published_example_of_each_edge_message_is_taken_by_its_route(edge, stream):
    assert _kept(edge, stream) == 0
    r = edge.post(stream, stamped(stream, edge.sid, 1))
    assert r.status_code == 200 and r.json()["ack"] == 1, r.text
    assert _kept(edge, stream) >= 1


@pytest.mark.parametrize("stream", STREAMS)
def test_each_required_member_is_asked_for_and_an_unknown_one_refused_the_same_way_each_time_with_nothing_stored(edge, stream):
    required = schema(stream)["required"]
    assert {"seq", "sent_at"} <= set(required), required                         # the envelope's own
    cases = [({k: v for k, v in stamped(stream, edge.sid, 1).items() if k != name}, name) for name in required]
    cases.append(({**stamped(stream, edge.sid, 1), "made_up_member": 1}, "made_up_member"))
    for body, name in cases:
        answers = [edge.post(stream, body) for _ in range(2)]
        for r in answers:
            assert r.status_code == 422 and r.json()["code"] == "SCHEMA", (stream, name, r.text)
            assert r.json()["detail"].startswith(f"{name}:"), (stream, name, r.text)
        same = [{k: v for k, v in r.json().items() if k != "request_id"} for r in answers]
        assert same[0] == same[1]                                                  # never acknowledged later as a duplicate
    assert _kept(edge, stream) == 0
    with edge.use(), store.connect() as con:
        assert con.execute("SELECT count(*) FROM edge_seen WHERE site_id = ?", (edge.sid,)).fetchone()[0] == 0


@pytest.mark.parametrize("stream", STREAMS)
def test_a_message_of_another_contract_version_or_message_type_is_refused(edge, stream):
    r = edge.post(stream, {**stamped(stream, edge.sid, 1), "contract": "ems-edge/2.0"})
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA" and r.json()["detail"].startswith("contract:"), r.text
    other = "events" if stream != "events" else "ack"
    r = edge.post(stream, {**stamped(stream, edge.sid, 1), "msg_type": other})
    assert r.status_code == 422 and r.json()["detail"].startswith("msg_type:"), r.text
    assert _kept(edge, stream) == 0


def test_the_plan_message_the_cloud_signs_is_its_schemas(edge):
    s = schema("plan")
    day = datetime.now(PKT).date()
    with edge.use():
        plan_row(edge.sid, {day: table(day, [(0, 6, "charge_grid"), (6, 18, "hold"), (18, 22, "discharge"), (22, 24, "self_use")])})
    r = edge.pull()
    assert r.status_code == 200, r.text
    msg = r.json()
    contract.PlanMessage.model_validate(msg)
    assert set(s["required"]) <= set(msg), set(s["required"]) - set(msg)
    assert set(msg) <= set(s["properties"]), set(msg) - set(s["properties"])
    assert (msg["contract"], msg["msg_type"], msg["type"], msg["site_id"], msg["timezone"]) == ("ems-edge/1.0", "plan", "plan", edge.sid, "Asia/Karachi")
    # U3 5.4: valid at most 48 hours from its issue
    assert contract.epoch(msg["valid_until"]) - contract.epoch(msg["issued_at"]) <= 48 * 3600
    # a release is the same message with no slots
    with edge.use():
        sites.add_override(edge.sid, {"kind": "force_native", "until": iso(now() + 7200), "reason": "contract suite"}, "t")
    rel = edge.pull().json()
    contract.PlanMessage.model_validate(rel)
    assert rel["type"] == "release" and rel["mode_a"]["days"] == [] and set(s["required"]) <= set(rel)
