"""Round 7 (audit), package auth-security: one regression test per finding, named by the audit's id.

Each test works in a data folder of its own (the `world` fixture of test_fix_api.py: a company file with two sites and a member of every role)."""
from __future__ import annotations

import json

from test_fix_api import world  # noqa: F401  (world is a fixture)


def _keys(obj) -> set[str]:
    """Every key at any depth of an answer."""
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


# ------------------------------------------------------------------------------------------------ security-roles-01 (tracker-and-docs-02)

def test_the_events_route_gives_no_fuel_cost_to_a_viewer_or_a_key(world):
    sid = world.site
    assert world.c["operator"].post(f"/api/sites/{sid}/genset/fuel", json={"litres": 50, "day": "2026-11-05", "cost_rs": 14000, "note": "tanker"}).status_code == 200
    # the administrator still sees what the fuel cost
    mine = world.c["admin"].get(f"/api/sites/{sid}/events").json()
    assert [e["detail"]["cost_rs"] for e in mine if e["type"] == "fuel_log"] == [14000]
    # a viewer and a site key holding sites:read see the litres, never the rupees
    seen = world.c["viewer"].get(f"/api/sites/{sid}/events")
    keyed = world.anon.get(f"/api/sites/{sid}/events", headers=world.key(["sites:read"]))
    for r in (seen, keyed):
        assert r.status_code == 200, r.text
        logs = [e for e in r.json() if e["type"] == "fuel_log"]
        assert [e["detail"]["litres"] for e in logs] == [50]
        assert "cost_rs" not in _keys(r.json()) and "14000" not in json.dumps(r.json())
