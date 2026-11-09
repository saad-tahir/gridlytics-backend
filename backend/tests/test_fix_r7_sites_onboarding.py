"""Round 7 (audit), sites and onboarding: one regression test per finding of the sites-onboarding work package.

flow-onboarding-05: a resumed wizard draft sent its older values back with `PUT /api/sites/:id` and silently reverted what the owner had changed in Site
settings since. A save may now carry `if_unchanged`, the stored values its form was based on (null: none was stored); when they are no longer what is
stored, the save is refused with 409 `site_changed` instead of writing over the newer values. Without it a save is written as before.

flow-onboarding-07: an empty tariff category was stored (200), after which the active site never planned and raised no alert. ConnectionFields now refuses an
empty or unknown category in the web app's sentence, and activation asks for one when a connection stored before the check has none.

flow-onboarding-18: the agreement's dates were any text, in any order, and the genset's money figures and tank had no upper bound. They are now days written
YYYY-MM-DD, the end not before the start, and Rs 1,000,000 a start or a running hour, Rs 10,000 a kWh of upkeep and a 1,000,000 L tank at most.
"""
from __future__ import annotations

import pytest

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


# ------------------------------------------------------------------------------------------------ flow-onboarding-07

def test_an_empty_or_unknown_tariff_category_is_refused_and_activation_asks_for_one(world):
    a, sid = world.c["admin"], world.site
    conn = _site(a, sid)["connections"][0]
    # an empty category (or one of spaces) was stored with 200, and the site then never planned: refused in the web app's own sentence
    for empty in ("", "   "):
        r = a.put(f"/api/sites/{sid}/connection", json={"id": conn["id"], "tariff_category": empty})
        assert r.status_code == 422 and r.json()["code"] == "invalid", r.text
        assert r.json()["detail"] == "Choose the tariff category printed on the bill"
    r = a.put(f"/api/sites/{sid}/connection", json={"tariff_category": "", "meter_ref": "M-2"})
    assert r.status_code == 422 and r.json()["detail"] == "Choose the tariff category printed on the bill", r.text
    r = a.put(f"/api/sites/{sid}/connection", json={"id": conn["id"], "tariff_category": "Z-9"})
    assert r.status_code == 422 and r.json()["detail"] == "Unknown tariff category Z-9", r.text
    with world.use():
        with pytest.raises(ValueError, match="Choose the tariff category"):
            sites.put_connection(sid, {"tariff_category": ""}, "test")
    held = _site(a, sid)["connections"]
    assert [c["data"]["tariff_category"] for c in held] == ["B-2(b)"]
    # a connection stored with no category before this check: the site is not activated, and is told which tab to finish
    with world.use():
        from gridlytics import store
        with store.connect() as con:
            con.execute("UPDATE connections SET data = json_set(data, '$.tariff_category', '') WHERE id = ?", (conn["id"],))
        sites.put_asset(sid, "inverter", {"kw_ac": 100}, "test")
    a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "csv", "grid_signal_confirmed": True}})
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.status_code == 422 and r.json()["code"] == "incomplete", r.text
    assert "the tariff category of each grid connection (tab C)" in r.json()["detail"]
    assert _site(a, sid)["status"] != "active"


# ------------------------------------------------------------------------------------------------ flow-onboarding-18

def test_agreement_dates_are_days_in_order_and_the_gensets_money_and_tank_are_bounded(world):
    a, sid = world.c["admin"], world.site
    conn = _site(a, sid)["connections"][0]

    def put_conn(**dates):
        return a.put(f"/api/sites/{sid}/connection", json={"id": conn["id"], "tariff_category": "B-2(b)", **dates})

    for dates, sentence in (({"agreement_start": "banana", "agreement_end": "2020-01-01"}, "The agreement start is a date (YYYY-MM-DD)"),
                            ({"agreement_start": "2026-02-30"}, "The agreement start is a date (YYYY-MM-DD)"),
                            ({"agreement_end": "20330101"}, "The agreement end is a date (YYYY-MM-DD)"),
                            ({"agreement_start": "2030-01-01", "agreement_end": "2020-01-01"}, "The agreement must end after it starts")):
        r = put_conn(**dates)
        assert r.status_code == 422 and r.json() == {**r.json(), "detail": sentence, "code": "invalid"}, (dates, r.text)
    assert _site(a, sid)["connections"][0]["data"]["agreement_start"] is None
    r = put_conn(agreement_start="2026-01-01", agreement_end="2033-12-31")
    assert r.status_code == 200, r.text
    assert {k: r.json()["connections"][0]["data"][k] for k in ("agreement_start", "agreement_end")} == {"agreement_start": "2026-01-01",
                                                                                                        "agreement_end": "2033-12-31"}
    # a blank day (the web app sends null; an empty text is blank too) is no agreement date
    r = put_conn(agreement_start="", agreement_end=None)
    assert r.status_code == 200 and r.json()["connections"][0]["data"]["agreement_start"] is None, r.text
    # the genset's money figures and tank had no upper bound: Rs 1e12 a start and a 1e12 L tank were stored
    for field, sentence in (("c_start_rs", "The cost per start is at most Rs 1,000,000"), ("c_runhr_rs", "The cost per running hour is at most Rs 1,000,000"),
                            ("c_om_rs_per_kwh", "The upkeep per kWh is at most Rs 10,000"), ("tank_l", "The tank holds at most 1,000,000 L")):
        r = a.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 150, field: 1e12})
        assert r.status_code == 422 and r.json()["detail"] == sentence, (field, r.text)
    r = a.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 150, "c_start_rs": 1_000_000, "c_runhr_rs": 1_000_000, "c_om_rs_per_kwh": 10_000,
                                                         "tank_l": 1_000_000})
    assert r.status_code == 200 and r.json()["assets"]["genset"]["tank_l"] == 1_000_000, r.text
