"""Round 7 (audit), sites and onboarding: one regression test per finding of the sites-onboarding work package.

flow-onboarding-05: a resumed wizard draft sent its older values back with `PUT /api/sites/:id` and silently reverted what the owner had changed in Site
settings since. A save may now carry `if_unchanged`, the stored values its form was based on (null: none was stored); when they are no longer what is
stored, the save is refused with 409 `site_changed` instead of writing over the newer values. Without it a save is written as before.
"""
from __future__ import annotations

from gridlytics import sites
from test_fix_api import world  # noqa: F401  (world is a fixture)


def _site(c, sid):
    r = c.get(f"/api/sites/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------------------------------------ flow-onboarding-05

def test_a_site_save_based_on_older_values_is_refused_rather_than_written_over_newer_ones(world):
    a, op, sid = world.c["admin"], world.c["operator"], world.site
    base = _site(op, sid)["data"]
    seen = {k: base.get(k) for k in ("name", "city", "load_peak_kw", "data_source")}
    # the owner renames the site in Site settings meanwhile (no check: a save without if_unchanged is written as before)
    r = a.put(f"/api/sites/{sid}", json={"name": "Mill One (renamed by the owner)", "load_peak_kw": 900})
    assert r.status_code == 200, r.text
    # the operator's older draft sends its values back, based on what it read before: refused, and the owner's values stay
    r = op.put(f"/api/sites/{sid}", json={"name": "Mill One", "city": "Faisalabad", "load_peak_kw": None, "if_unchanged": seen})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "site_changed"
    assert body["changed"] == ["load_peak_kw", "name"]
    assert body["detail"].startswith("The site's") and "changed after this form was loaded" in body["detail"]
    now = _site(a, sid)["data"]
    assert (now["name"], now["load_peak_kw"], now["city"]) == ("Mill One (renamed by the owner)", 900, "")
    # based on the values as they are now, the same save is written
    fresh = {k: now.get(k) for k in seen}
    r = op.put(f"/api/sites/{sid}", json={"city": "Faisalabad", "if_unchanged": fresh})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["city"] == "Faisalabad" and r.json()["data"]["name"] == "Mill One (renamed by the owner)"
    # a stored number read back as a whole number (20.0 sent as 20 by the web app) is the same value
    r = op.put(f"/api/sites/{sid}", json={"cycle_cap_per_day": 2, "if_unchanged": {"cycle_cap_per_day": 1.5, "critical_load_kw": [20] * 24}})
    assert r.status_code == 200, r.text
    # if_unchanged is an object of stored values; anything else is refused in a sentence
    for wrong in ("yes", [1], None):
        r = op.put(f"/api/sites/{sid}", json={"city": "Lahore", "if_unchanged": wrong})
        assert r.status_code == 422 and r.json()["code"] == "invalid", (wrong, r.text)
    assert _site(a, sid)["data"]["city"] == "Faisalabad"
    # a refused save writes nothing to the audit trail
    with world.use():
        from gridlytics import store
        changes = [x for x in store.list_audit() if x["action"] == "changed a site"]
    assert len(changes) == 3


def test_a_connection_save_based_on_older_values_is_refused(world):
    a, op, sid = world.c["admin"], world.c["operator"], world.site
    conn = _site(op, sid)["connections"][0]
    seen = dict(conn["data"])
    r = a.put(f"/api/sites/{sid}/connection", json={**seen, "id": conn["id"], "sanctioned_load_kw": 650})
    assert r.status_code == 200, r.text
    stale = {**seen, "meter_ref": "M-OLD"}
    r = op.put(f"/api/sites/{sid}/connection", json={**stale, "id": conn["id"], "if_unchanged": seen})
    assert r.status_code == 409 and r.json()["code"] == "site_changed", r.text
    assert r.json()["changed"] == ["sanctioned_load_kw"]
    assert r.json()["detail"].startswith("The grid connection's sanctioned load")
    assert _site(a, sid)["connections"][0]["data"]["sanctioned_load_kw"] == 650
    # the form last saw no connection (null), but this one is stored now: refused as well
    r = op.put(f"/api/sites/{sid}/connection", json={**stale, "id": conn["id"], "if_unchanged": None})
    assert r.status_code == 409 and r.json()["detail"].startswith("The grid connection was added after this form was loaded"), r.text
    # based on the stored values, written
    held = _site(a, sid)["connections"][0]["data"]
    r = op.put(f"/api/sites/{sid}/connection", json={**stale, "id": conn["id"], "sanctioned_load_kw": 650, "if_unchanged": held})
    assert r.status_code == 200, r.text
    assert r.json()["connections"][0]["data"]["meter_ref"] == "M-OLD"
    # a new connection has nothing stored to compare with: the check goes with the id of the connection it was read from
    r = op.put(f"/api/sites/{sid}/connection", json={"tariff_category": "A-2(c)", "meter_ref": "M-NEW", "if_unchanged": held})
    assert r.status_code == 422 and "id" in r.json()["detail"], r.text
    assert len(_site(a, sid)["connections"]) == 1


def test_an_asset_save_based_on_older_values_is_refused(world):
    a, op, sid = world.c["admin"], world.c["operator"], world.site
    battery = {k: v for k, v in _site(op, sid)["assets"]["battery"].items() if k != "id"}
    r = a.put(f"/api/sites/{sid}/assets/battery", json={**battery, "nameplate_kwh": 250})
    assert r.status_code == 200, r.text
    r = op.put(f"/api/sites/{sid}/assets/battery", json={**battery, "soc_min": 0.15, "if_unchanged": battery})
    assert r.status_code == 409 and r.json()["code"] == "site_changed", r.text
    assert r.json()["changed"] == ["nameplate_kwh"] and r.json()["detail"].startswith("The battery's nameplate energy")
    assert _site(a, sid)["assets"]["battery"]["nameplate_kwh"] == 250
    # the form last saw no PV: saved; it last saw no battery, but one is stored: refused
    r = op.put(f"/api/sites/{sid}/assets/pv", json={"kwp_dc": 100, "kw_ac": 80, "if_unchanged": None})
    assert r.status_code == 200, r.text
    r = op.put(f"/api/sites/{sid}/assets/battery", json={**battery, "if_unchanged": None})
    assert r.status_code == 409 and r.json()["detail"].startswith("The battery was added after this form was loaded"), r.text
    # removed meanwhile: refused rather than added again from the older form
    with world.use():
        sites.remove_asset(sid, "pv", "test")
    r = op.put(f"/api/sites/{sid}/assets/pv", json={"kwp_dc": 120, "kw_ac": 80, "if_unchanged": {"kwp_dc": 100, "kw_ac": 80}})
    assert r.status_code == 409 and r.json()["detail"].startswith("The PV array was removed after this form was loaded"), r.text
    assert "pv" not in _site(a, sid)["assets"]
    # based on what is stored, written
    held = {k: v for k, v in _site(a, sid)["assets"]["battery"].items() if k != "id"}
    r = op.put(f"/api/sites/{sid}/assets/battery", json={**held, "soc_min": 0.15, "if_unchanged": held})
    assert r.status_code == 200 and r.json()["assets"]["battery"]["soc_min"] == 0.15, r.text
