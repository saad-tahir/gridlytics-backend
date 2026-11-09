"""Round 7 (audit), package control-edge: what reaches the device and the control state.

Batch 1, what reaches the device:
- a site below closed loop gets no plan at its edge (audit flow-telemetry-plan-control-03, engine-plans-live-11).

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import delivery, forecast, plans, sites, store
from test_fix_api import _fake_plan, world  # noqa: F401  (world is a fixture)
from test_fix_optimiser import _now_t0, _site, _until

PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _closed_loop(sid: str) -> None:
    sites.set_tier_capable(sid, "closed_loop")
    sites.set_tier_allowed(sid, "closed_loop", "t", "test")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _messages(sid: str) -> int:
    with store.connect() as con:
        return con.execute("SELECT count(*) FROM plan_messages WHERE site_id = ?", (sid,)).fetchone()[0]


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-03, engine-plans-live-11

@pytest.mark.parametrize("tier", ["monitor", "advisory_writes"])
def test_a_site_below_closed_loop_sends_its_edge_no_plan(company, tier):
    sid = _site(genset=False)
    sites.set_tier_capable(sid, "closed_loop")
    sites.set_tier_allowed(sid, tier, "t", "test")
    assert sites.get(sid)["tier"] == tier
    assert plans.tick(sid)["status"] == "active"
    with pytest.raises(plans.NotClosedLoop) as no:
        plans.signed_message(sid)
    assert no.value.tier == tier
    assert _messages(sid) == 0                                    # nothing was sent, so nothing is counted as a table write
    # a pause at such a site sends nothing either: the edge holds no plan to release
    sites.add_override(sid, {"kind": "pause_control", "until": _until(2), "reason": "test"}, "t")
    with pytest.raises(plans.NotClosedLoop):
        plans.signed_message(sid)
    assert _messages(sid) == 0


def test_a_closed_loop_site_sends_its_edge_the_signed_plan(company):
    sid = _site(genset=False)
    _closed_loop(sid)
    assert plans.tick(sid)["status"] == "active"
    msg = plans.signed_message(sid)
    assert msg["type"] == "plan" and msg["mode_a"]["days"]
    assert delivery.verify(msg, delivery.public_key(sid)["public_key"])
    assert _messages(sid) == 1


def test_the_edge_route_refuses_a_plan_below_closed_loop_and_serves_it_at_closed_loop(world):
    sid = world.site
    _fake_plan(world)
    kh = world.key(["plans:read"], role="viewer")
    r = world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh)
    assert r.status_code == 409 and r.json()["code"] == "not_closed_loop", r.text
    assert "monitor" in r.json()["detail"] and "no plan is sent" in r.json()["detail"]
    with world.use():
        assert _messages(sid) == 0
        _closed_loop(sid)
    r = world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh)
    assert r.status_code == 200 and r.json()["type"] == "plan", r.text
