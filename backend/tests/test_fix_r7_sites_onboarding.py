"""Round 7 (audit), sites and onboarding: one regression test per finding of the sites-onboarding work package.

flow-onboarding-05: a resumed wizard draft sent its older values back with `PUT /api/sites/:id` and silently reverted what the owner had changed in Site
settings since. A save may now carry `if_unchanged`, the stored values its form was based on (null: none was stored); when they are no longer what is
stored, the save is refused with 409 `site_changed` instead of writing over the newer values. Without it a save is written as before.

flow-onboarding-07: an empty tariff category was stored (200), after which the active site never planned and raised no alert. ConnectionFields now refuses an
empty or unknown category in the web app's sentence, and activation asks for one when a connection stored before the check has none.

flow-onboarding-18: the agreement's dates were any text, in any order, and the genset's money figures and tank had no upper bound. They are now days written
YYYY-MM-DD, the end not before the start, and Rs 1,000,000 a start or a running hour, Rs 10,000 a kWh of upkeep and a 1,000,000 L tank at most.

flow-onboarding-14 (and flow-savings-pilot-bill-15): a native capture stored slot starts such as '99:99' and two slots starting at the same time, and the
savings replay then used the impossible slot as the overnight default. A slot now starts at a time from 00:00 to 23:59, and no two slots start together.

role-operator-16 (and role-owner-35, role-analyst-27): a native capture held up to fourteen slots whatever the inverter, so a six-program Deye could be given
a table it cannot hold. A capture now holds at most the slots of the site's inverter brand (delivery.BRANDS, GET /api/meta brand_grammars).

flow-onboarding-08: the API activated a site with no data source, or with a grid-presence signal nobody confirmed, and it was then planned although its
outages could not be seen. Activation now asks for the data source with its grid-presence signal confirmed (tab A), as the wizard does.

flow-onboarding-03: tier_capable was set only by an edge's capabilities message, so a CSV or cloud site stayed at monitor for good although the wizard
promises slot tables to key in. Saving a CSV or cloud source makes the site capable of advisory writes (a CSV once a file carries the inverter's mode);
a change back to an edge device takes what the edge last asserted.
"""
from __future__ import annotations

import pytest

from gridlytics import sites
from gridlytics.schemas import Refusal
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
        with pytest.raises(Refusal, match="Choose the tariff category"):
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


# ------------------------------------------------------------------------------------------------ flow-onboarding-14 (and flow-savings-pilot-bill-15)

def _native(c, sid, starts, **body):
    return c.post(f"/api/sites/{sid}/native", json={"work_mode": "tou", "slots": [{"start": t, "mode": "self_use"} for t in starts], **body})


def test_a_native_capture_holds_real_and_distinct_slot_times(world):
    a, sid = world.c["admin"], world.site
    # '99:99' was stored, and the replay read it as minute 6039: the slot that governs every minute before the first real one
    for bad in ("99:99", "25:99", "24:00", "12:60", "7:00", "07.00"):
        r = _native(a, sid, ["06:00", bad])
        assert r.status_code == 422 and r.json()["detail"] == "Each slot starts at a time from 00:00 to 23:59", (bad, r.text)
    r = _native(a, sid, ["17:00", "06:00", "17:00"])
    assert r.status_code == 422 and r.json()["detail"] == "Two slots cannot start at the same time", r.text
    r = _native(a, sid, [f"{h:02d}:00" for h in range(15)])
    assert r.status_code == 422 and r.json()["detail"] == "An inverter's table holds at most 14 slots", r.text
    with world.use():
        assert sites.native_versions(sid) == []
    r = _native(a, sid, ["00:00", "17:00", "23:59"])
    assert r.status_code == 200 and [s["start"] for s in r.json()["body"]["slots"]] == ["00:00", "17:00", "23:59"], r.text
    with world.use():
        assert [v["version"] for v in sites.native_versions(sid)] == [1]


# ------------------------------------------------------------------------------------------------ role-operator-16 (and role-owner-35, role-analyst-27)

def test_a_native_capture_holds_no_more_slots_than_the_sites_inverter_table(world):
    op, sid = world.c["operator"], world.site
    hours = [f"{h:02d}:00" for h in range(15)]
    # no inverter recorded yet: the most any brand's table holds (Huawei's fourteen)
    assert _native(op, sid, hours[:14]).status_code == 200
    # a Deye's table holds six programs (delivery.BRANDS, GET /api/meta brand_grammars): a seventh was accepted
    with world.use():
        sites.put_asset(sid, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
    r = _native(op, sid, hours[:7])
    assert r.status_code == 422 and r.json()["code"] == "invalid", r.text
    assert r.json()["detail"] == "A Deye / Sunsynk inverter's table holds at most 6 slots"
    assert _native(op, sid, hours[:6]).status_code == 200
    with world.use():
        sites.put_asset(sid, "inverter", {"brand": "voltronic", "kw_ac": 100}, "test")
    assert _native(op, sid, hours[:4]).json()["detail"] == "A Voltronic / Axpert inverter's table holds at most 3 slots"
    with world.use():
        sites.put_asset(sid, "inverter", {"brand": "other", "kw_ac": 100}, "test")
    assert _native(op, sid, hours[:7]).json()["detail"] == "An inverter's table holds at most 6 slots"
    with world.use():
        assert [v["version"] for v in sites.native_versions(sid)] == [2, 1]        # newest first: only the two that fit were kept


# ------------------------------------------------------------------------------------------------ flow-onboarding-08

def test_activation_asks_for_a_data_source_whose_grid_signal_was_confirmed(world):
    a, sid = world.c["admin"], world.site
    with world.use():
        sites.put_asset(sid, "inverter", {"kw_ac": 100}, "test")
    # no data source at all: the site was activated (200) and planned, although outages could not be seen
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.status_code == 422 and r.json()["code"] == "incomplete", r.text
    assert r.json()["detail"] == "Add the data source, with its grid-presence signal confirmed (tab A) before activating"
    # a source whose grid-presence signal was never confirmed: refused the same way
    assert a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "oem_cloud", "grid_signal_confirmed": False}}).status_code == 200
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.status_code == 422 and "the data source, with its grid-presence signal confirmed (tab A)" in r.json()["detail"], r.text
    assert _site(a, sid)["status"] != "active"
    # every missing part is named, in tab order
    with world.use():
        sites.remove_asset(sid, "inverter", "test")
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.json()["detail"] == "Add the data source, with its grid-presence signal confirmed (tab A) and the inverter (tab B) before activating"
    with world.use():
        sites.put_asset(sid, "inverter", {"kw_ac": 100}, "test")
    assert a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "oem_cloud", "grid_signal_confirmed": True}}).status_code == 200
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.status_code == 200 and r.json()["status"] == "active", r.text


# ------------------------------------------------------------------------------------------------ flow-onboarding-03

def test_a_csv_or_cloud_source_lets_the_site_reach_advisory_writes(world):
    a, sid = world.c["admin"], world.site
    with world.use():
        sites.capture_native(sid, {"source": "entered"}, "test")
    assert _site(a, sid)["tier_capable"] == "monitor"
    # the inverter maker's cloud: plans go as slot tables keyed in by a person (plan 7.6), so advisory writes; it cannot read writes back
    r = a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "oem_cloud", "grid_signal_confirmed": True}})
    assert r.status_code == 200 and r.json()["tier_capable"] == "advisory_writes", r.text
    r = a.put(f"/api/sites/{sid}/tier", json={"tier": "advisory_writes", "signoff": "Installer A"})
    assert r.status_code == 200 and r.json()["tier"] == "advisory_writes", r.text
    r = a.put(f"/api/sites/{sid}/tier", json={"tier": "closed_loop", "signoff": "Installer A"})
    assert r.status_code == 422 and r.json()["code"] == "above_capable", r.text
    assert r.json()["detail"] == "This site's data source supports advisory writes at most"
    # a CSV source: advisory writes too, once an imported file carries the inverter's mode (plan 7.5: without it, monitor only)
    assert a.put(f"/api/sites/{sid}/tier", json={"tier": "monitor"}).status_code == 200
    r = a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "csv", "grid_signal_confirmed": True}})
    assert r.json()["tier_capable"] == "advisory_writes"
    r = a.put(f"/api/sites/{sid}/tier", json={"tier": "advisory_writes", "signoff": "Installer A"})
    assert r.status_code == 422 and r.json()["code"] == "no_inverter_mode", r.text
    assert r.json()["detail"].startswith("No imported file of this site carries inverter_mode_active")
    with world.use():
        from gridlytics import telemetry
        telemetry.write_rows([(sid, 1_762_400_000, "csv", 1.0, "self_use")], ("site_id", "ts", "source_id", "grid_present", "inverter_mode_active"))
    r = a.put(f"/api/sites/{sid}/tier", json={"tier": "advisory_writes", "signoff": "Installer A"})
    assert r.status_code == 200 and r.json()["tier"] == "advisory_writes", r.text
    # back to an edge device: what its own capabilities message says (none yet: monitor), so the effective tier falls back
    r = a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "edge", "grid_signal_confirmed": True}})
    assert (r.json()["tier_capable"], r.json()["tier"]) == ("monitor", "monitor"), r.text
    # a site created with a cloud source starts able to reach advisory writes; one created without a source stays at monitor
    r = a.post("/api/sites", json={"name": "Cloud site", "data_source": {"kind": "oem_cloud", "grid_signal_confirmed": True}})
    assert r.status_code == 200 and (r.json()["tier_capable"], r.json()["tier"]) == ("advisory_writes", "monitor"), r.text
    assert a.post("/api/sites", json={"name": "No source"}).json()["tier_capable"] == "monitor"
