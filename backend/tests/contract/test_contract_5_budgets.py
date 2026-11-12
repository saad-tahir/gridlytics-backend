"""Contract test 5 (EMS_BUILD_PLAN.md Part 7.4, U3 5.8 item 5; R6 3.6): budgets.

An inverter's table lives in EEPROM with a few writes a day: the cloud issues at most the brand's table writes a day (at most the edge's own cap of
four, U3 5.4's apply_policy), so the write past them (Sungrow's fifth, the others' fourth) is refused and the previous table remains: issued for
the day, sent to the edge and shown on the control page. The writes the edge reports in its acks count too, and an edge's BUDGET_EXHAUSTED refusal
is shown in the control state. Round 7, audit tests-and-perf-15."""
from __future__ import annotations

from datetime import datetime

import pytest

from contract_kit import PKT, edge, plan_row, stamped, table  # noqa: F401  (edge is a fixture)
from gridlytics import delivery, plans, sites
from test_fix_api import world  # noqa: F401  (world is a fixture)


def _plan(e, discharge_from: int) -> str:
    day = datetime.now(PKT).date()
    with e.use():
        return plan_row(e.sid, {day: table(day, [(0, discharge_from, "hold"), (discharge_from, 22, "discharge"), (22, 24, "hold")])})


def _discharge_start(msg: dict) -> str:
    return next(s["start"] for s in msg["mode_a"]["days"][0]["slots"] if s["mode"] == "discharge")


@pytest.mark.parametrize("brand", ["deye", "sungrow"])
def test_the_write_past_the_brands_budget_is_refused_and_the_previous_table_remains(edge, monkeypatch, brand):
    monkeypatch.setattr(plans, "reissue_gain", lambda *a: 900.0)               # every newer table worth its write
    monkeypatch.setattr(plans, "REISSUE_SPACING_S", 0)
    day = datetime.now(PKT).date()
    with edge.use():
        sites.put_asset(edge.sid, "inverter", {"brand": brand, "kw_ac": 100}, "t")
        budget = plans.table_budget(sites.full(edge.sid))
    assert budget == delivery.BRANDS[brand]["table_writes_per_day"] == (4 if brand == "sungrow" else 3) <= plans.EDGE_TABLE_WRITE_CAP
    issued = []
    for k in range(budget):
        pid = _plan(edge, 18 - k)
        with edge.use():
            d = plans.issue_tables(edge.sid)
        assert d["issued"] is True and d["reason"] == ("first" if k == 0 else "event"), (k, d)
        issued.append(pid)
    with edge.use():
        assert plans.writes_used(edge.sid, day) == budget
        current = _plan(edge, 12)
        refused = plans.issue_tables(edge.sid)
        assert refused["issued"] is False and refused["blocked"] == "budget_used", refused   # the write past the budget
        assert plans.issued_for(edge.sid, day.isoformat())["plan_id"] == issued[-1]
    # the previous table remains: the edge is sent it (under the plan in force), and the control page says the re-issue was held back
    msg = edge.pull().json()
    assert msg["plan_id"] == current and _discharge_start(msg) == f"{18 - budget + 1:02d}:00"
    assert msg["write_budget"]["table_writes_per_day"] == budget and msg["mode_a"]["apply_policy"]["max_table_writes_per_day"] == 4
    c = edge.world.c["admin"].get(f"/api/sites/{edge.sid}/control").json()
    assert c["budget"] == {"table_writes_per_day": budget, "used_today": budget} and c["reissue"]["blocked"] == "budget_used", c["reissue"]


def test_the_writes_the_edge_reports_count_and_its_budget_refusal_is_shown(edge, monkeypatch):
    monkeypatch.setattr(plans, "reissue_gain", lambda *a: 900.0)
    monkeypatch.setattr(plans, "REISSUE_SPACING_S", 0)
    day = datetime.now(PKT).date()
    first = _plan(edge, 18)
    with edge.use():
        assert plans.issue_tables(edge.sid)["reason"] == "first"
    msg = edge.pull().json()
    # the edge has written its table three times today already (an installer's session, a reboot): it says so in its ack
    used = {**stamped("ack", edge.sid, 1, plan_id=first), "status": "applied",
            "per_device": [{"device_id": "inv1", "status": "applied", "writes_used_today": {"tou_table": 3}, "applied": [], "rejected": []}]}
    assert edge.post("ack", used).status_code == 200
    _plan(edge, 16)
    with edge.use():
        assert plans.writes_used(edge.sid, day) == 3
        assert plans.issue_tables(edge.sid)["blocked"] == "budget_used"
    assert _discharge_start(edge.pull().json()) == _discharge_start(msg) == "18:00"
    # a table the edge refuses for its budget: the control state names the reason
    refusal = {**stamped("ack", edge.sid, 2, plan_id=edge.pull().json()["plan_id"]), "status": "rejected", "reason": "BUDGET_EXHAUSTED",
               "per_device": []}
    assert edge.post("ack", refusal).status_code == 200
    c = edge.world.c["viewer"].get(f"/api/sites/{edge.sid}/control").json()["control"]
    assert c["key"] == "edge_refused" and "BUDGET_EXHAUSTED" in c["label"], c
