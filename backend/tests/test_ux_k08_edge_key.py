"""UX backend ask K8 (plan H11; C19.2 item 4, C12.2; step 9d, Data and devices' edge checklist).

`GET /api/sites/{id}/edge` gains `key: {exists, made_at}`: whether an edge device's key, not revoked, exists for this site, and when the newest was
made (Pakistan time), with no prefix, no id and no name, so item 1 of the checklist ("Make the site key") reads done for an operator, who may not list
the company's keys (`manage_users`)."""
from __future__ import annotations

from datetime import datetime

import identities
from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import apikeys, sites, store
from gridlytics import platform as platformdb

ROLES = ("admin", "operator", "analyst", "viewer")


def _key(w, role="admin"):
    r = w.c[role].get(f"/api/sites/{w.site}/edge")
    assert r.status_code == 200, (role, r.text)
    return r.json()["key"]


def _make(w, site=None, kind="edge", name=None):
    r = w.c["admin"].post("/api/platform/keys", json={"name": name or f"box {kind} {site or w.site}", "scopes": ["telemetry:write", "plans:read"],
                                                      "site_id": site or w.site, "kind": kind})
    assert r.status_code == 200, r.text
    return r.json()


def test_no_key_yet_reads_as_none_for_every_role(world):
    for role in ROLES:
        assert _key(world, role) == {"exists": False, "made_at": None}, role


def test_an_edge_key_of_this_site_reads_as_made_with_its_time_in_karachi_and_nothing_else_of_it(world):
    made = _make(world)
    for role in ROLES:
        key = _key(world, role)
        assert set(key) == {"exists", "made_at"} and key["exists"] is True, (role, key)
        assert key["made_at"].endswith("+05:00"), key
        assert datetime.fromisoformat(key["made_at"]) == datetime.fromisoformat(made["created"]), (key, made["created"])
        # no prefix, no id, no name of the key (the operator may not list keys)
        text = world.c[role].get(f"/api/sites/{world.site}/edge").text
        assert made["prefix"] not in text and made["id"] not in text and made["name"] not in text, role


def test_only_a_live_edge_device_s_key_of_this_site_counts(world):
    """A partner tool's key, and an edge key of the company's other site, are not this site's box key; a revoked one no longer is."""
    _make(world, kind="partner")
    _make(world, site=world.other)
    assert _key(world) == {"exists": False, "made_at": None}
    mine = _make(world)
    assert _key(world)["exists"] is True
    assert world.c["admin"].delete(f"/api/platform/keys/{mine['id']}").status_code == 200
    assert _key(world, "operator") == {"exists": False, "made_at": None}


def test_the_newest_live_key_gives_the_time(world):
    with world.use():
        first, _ = apikeys.add("box one", "operator", ["telemetry:write"], "t", world.site, "edge")
        second, _ = apikeys.add("box two", "operator", ["telemetry:write"], "t", world.site, "edge")
        with store._connect() as con:
            con.execute("UPDATE api_keys SET created = ? WHERE id = ?", ("2026-11-01T09:20:00+00:00", first["id"]))
            con.execute("UPDATE api_keys SET created = ? WHERE id = ?", ("2026-11-11T09:20:00+00:00", second["id"]))
    assert _key(world)["made_at"] == "2026-11-11T14:20:00+05:00"
    world.c["admin"].delete(f"/api/platform/keys/{second['id']}")
    assert _key(world)["made_at"] == "2026-11-01T14:20:00+05:00"


def test_another_company_s_site_is_no_site_here(world):
    """The route answers this company's site (so the 404 below is its own refusal), and another company's site with an edge key is no site here."""
    other = identities.company("Another company", own_file=True)
    with store.use_company(platformdb.company_file(other)):
        theirs = sites.create({"name": "Their Mill", "disco": "LESCO"}, "t")["id"]
        apikeys.add("their box", "operator", ["telemetry:write"], "t", theirs, "edge")
    for role in ROLES:
        assert world.c[role].get(f"/api/sites/{world.site}/edge").status_code == 200, role
        r = world.c[role].get(f"/api/sites/{theirs}/edge")
        assert (r.status_code, r.json()["detail"]) == (404, "No such site"), (role, r.text)
