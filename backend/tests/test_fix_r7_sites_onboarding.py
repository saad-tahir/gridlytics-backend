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

role-operator-07: an operator could add, draft and activate sites and change the control limits, which plan 9.4 gives the administrator. They now need
manage_sites (an administrator's); the operator keeps the site's other fields, the native capture and overrides.

flow-onboarding-02: a K-Electric site's connection took the other DISCOs' time-of-use group and Punjab's duty by default, and K-Electric's group was accepted
on a FESCO site. The group and province now follow the site's DISCO where a save leaves them out, a group that contradicts the DISCO is refused, and a
site whose DISCO changed after its connection was saved is not activated until the group matches.

engine-tariffs-05 (and flow-onboarding-13, engine-money-live-10): {strn: false} alone kept gst_recoverable true, so a no-STRN site's price left its GST
out; the ICP baseline took any key and figure; Tariff F's season took any numbers. A connection without an STRN now bears its GST (saying it recovers it
is refused), the baseline is by calendar month in kWh from 0, the season is six distinct months at least on an industrial connection, and GET /api/meta
names the categories the ICP applies to.

flow-onboarding-10: a captive gas or RLNG genset kept the diesel fuel curve (0.273 litres a kWh read as MMBtu), so its fuel cost came out about 35 times
too high and the plan never ran it. A gas genset now takes the gas curve in MMBtu and no tank where they are left out, and a curve in the other fuel's
unit, or a tank on piped gas, is refused.

flow-onboarding-16: the genset's rating was asked in kW only, though sets are sold and labelled in kVA. A genset may now be given its nameplate kVA and
power factor (0.8 to start with): its kW is the kVA times the power factor, and a kW that disagrees with them is refused.

flow-onboarding-11: a site's genset start method had no threshold, so a set started by a controller at a battery level was planned at the reserve floor
plus 5 points whatever the controller did. A site now records the state of charge its controller starts the genset at, and the plan and the twin use it.

flow-onboarding-09 and flow-onboarding-17: equipment that disagreed with the inverter (a 5 kW inverter with a 5,000 kW battery, PV 900 kW AC from 10 kWp
DC, an export cap above the sanctioned load) and a blank feeder code were saved with no word. Every site answer now carries `setup_warnings`: sentences
that say so, without refusing the save (a site may really have PV on inverters of its own).

flow-onboarding-22: plan 9.3's tab A asks for a connection test that refuses a source without a grid-present signal, and tab D shows the write budget and
the fallback; the wizard had a self-declared tick only, and Mode B could be chosen for a CSV site without a word. `POST /api/sites/:id/source/test` now
says what the data received carries (the minutes, the points, whether grid presence is among them), activation refuses a grid-connected site whose data
carries no grid presence, Mode B with a CSV or cloud source is a setup warning, and GET /api/meta gives the fallback every plan carries.
"""
from __future__ import annotations

import pytest

from gridlytics import delivery, sites, telemetry
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
    # a stored number read back as a whole number (20.0 sent as 20 by the web app) is the same value (the cycle cap is the administrator's, role-operator-07)
    r = a.put(f"/api/sites/{sid}", json={"cycle_cap_per_day": 2, "if_unchanged": {"cycle_cap_per_day": 1.5, "critical_load_kw": [20] * 24}})
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


# ------------------------------------------------------------------------------------------------ role-operator-07

def test_creating_activating_and_the_control_limits_are_for_an_administrator(world):
    a, op, sid = world.c["admin"], world.c["operator"], world.site
    meta = op.get("/api/meta").json()["permissions"]
    assert meta["manage_sites"] is False and meta["edit_sites"] is True
    assert a.get("/api/meta").json()["permissions"]["manage_sites"] is True
    r = op.post("/api/sites", json={"name": "AUDIT operator onboarding"})
    assert r.status_code == 403 and r.json()["detail"] == "Adding a site is for an administrator", r.text
    r = op.put("/api/drafts/d-op", json={"general": {"name": "Half a site"}})
    assert r.status_code == 403 and r.json()["detail"] == "Saving a draft is for an administrator", r.text
    with world.use():
        sites.put_asset(sid, "inverter", {"kw_ac": 100}, "test")
    a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "csv", "grid_signal_confirmed": True}})
    r = op.post(f"/api/sites/{sid}/activate")
    assert r.status_code == 403 and r.json()["detail"] == "Activating a site is for an administrator", r.text
    # the control limits: the reserve floor the plan never crosses, the cycle cap, the control mode, the critical load and the charge sources
    for change in ({"reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 5}}, {"cycle_cap_per_day": 3}, {"control_mode": "mode_b"},
                   {"critical_load_kw": [0.0] * 24}, {"grid_charge_allowed": False}):
        r = op.put(f"/api/sites/{sid}", json=change)
        assert r.status_code == 403, (change, r.text)
        assert r.json()["detail"] == "Changing the site's control limits is for an administrator"
    held = _site(a, sid)["data"]
    assert (held["reserve_policy"]["min_soc_pct"], held["cycle_cap_per_day"], held["control_mode"]) == (20, 1.5, "advisory")
    # the form sent back whole with the limits unchanged is not a change of them; the operator keeps the site's other fields, native capture and overrides
    r = op.put(f"/api/sites/{sid}", json={**held, "notes": "Gate 2 is locked after 18:00"})
    assert r.status_code == 200 and r.json()["data"]["notes"] == "Gate 2 is locked after 18:00", r.text
    assert _native(op, sid, ["00:00", "17:00"]).status_code == 200
    from datetime import datetime, timedelta, timezone
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    r = op.post(f"/api/sites/{sid}/overrides", json={"kind": "reserve", "value": 40, "until": until})
    assert r.status_code == 200, r.text
    # the administrator does all of it
    assert a.put(f"/api/sites/{sid}", json={"cycle_cap_per_day": 3}).status_code == 200
    assert a.post(f"/api/sites/{sid}/activate").status_code == 200
    assert a.post("/api/sites", json={"name": "Admin's site"}).status_code == 200
    assert a.put("/api/drafts/d-admin", json={"general": {"name": "Half a site"}}).status_code == 200


def test_a_limit_sent_as_stored_is_no_change_and_one_a_partial_policy_resets_is(world):
    """Review of role-operator-07: the limits are compared as the save would store them, not as sent. A key a site saved before it existed lacks, sent at
    its default, or a number sent as 20 for a stored 20.0, changes nothing; a reserve policy sent in part does change the part it leaves out (sites.update
    fills it with its default), so that is a change of a limit."""
    a, op, sid = world.c["admin"], world.c["operator"], world.site
    with world.use():
        from gridlytics import store
        with store.connect() as con:      # a site saved before the charge sources were fields
            con.execute("UPDATE sites SET data = json_remove(data, '$.gen_charge_allowed', '$.grid_charge_allowed') WHERE id = ?", (sid,))
    r = op.put(f"/api/sites/{sid}", json={"gen_charge_allowed": True, "grid_charge_allowed": True, "cycle_cap_per_day": 1.5,
                                          "reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 20}, "notes": "Kept as stored"})
    assert r.status_code == 200 and r.json()["data"]["notes"] == "Kept as stored", r.text
    # the administrator sets the outage threshold; the operator's reserve policy without it would put it back to 0.2
    assert a.put(f"/api/sites/{sid}", json={"reserve_policy": {"p_threshold": 0.5, "min_soc_pct": 20}}).status_code == 200
    r = op.put(f"/api/sites/{sid}", json={"reserve_policy": {"min_soc_pct": 20}})
    assert r.status_code == 403 and r.json()["detail"] == "Changing the site's control limits is for an administrator", r.text
    assert _site(a, sid)["data"]["reserve_policy"] == {"p_threshold": 0.5, "min_soc_pct": 20.0}
    # a limit the model refuses is refused for the role first; for the administrator it is the 422 of the field
    assert op.put(f"/api/sites/{sid}", json={"cycle_cap_per_day": 99}).status_code == 403
    r = a.put(f"/api/sites/{sid}", json={"cycle_cap_per_day": 99})
    assert r.status_code == 422 and r.json()["code"] == "invalid", r.text


def test_a_save_that_keeps_the_kind_of_source_never_lowers_what_the_site_can_reach(world):
    """Review of flow-onboarding-03: the source's tier is set when its kind is chosen or changed, and a later save that keeps the kind (a note, the name)
    only raises it to what the kind supports: an edge's capabilities message that raised a CSV site stays until the kind is changed."""
    a, op, sid = world.c["admin"], world.c["operator"], world.site
    r = a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "csv", "grid_signal_confirmed": True}})
    assert r.json()["tier_capable"] == "advisory_writes", r.text
    with world.use():
        sites.set_tier_capable(sid, "closed_loop")       # what routes_edge.edge_caps does when the site's edge reports closed loop
    r = op.put(f"/api/sites/{sid}", json={"notes": "Edge box fitted in the plant room"})
    assert r.status_code == 200 and r.json()["tier_capable"] == "closed_loop", r.text
    # choosing another kind sets what that kind supports
    r = a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "oem_cloud", "grid_signal_confirmed": True}})
    assert r.json()["tier_capable"] == "advisory_writes", r.text
    # a site whose CSV source was saved at monitor before this rule is raised by its next save
    with world.use():
        sites.set_tier_capable(sid, "monitor")
    assert op.put(f"/api/sites/{sid}", json={"notes": "Logger export weekly"}).json()["tier_capable"] == "advisory_writes"


# ------------------------------------------------------------------------------------------------ flow-onboarding-02

def test_a_k_electric_sites_connection_takes_its_own_time_of_use_windows_and_province(world):
    a, sid = world.c["admin"], world.site
    ke = a.post("/api/sites", json={"name": "Karachi mill", "city": "Karachi", "disco": "K-Electric"}).json()["id"]
    # a connection saved without a DISCO group or a province took the other DISCOs' windows and Punjab's duty: they now follow the site's DISCO
    r = a.put(f"/api/sites/{ke}/connection", json={"tariff_category": "B-2(b)", "sanctioned_load_kw": 300})
    assert r.status_code == 200, r.text
    conn = r.json()["connections"][0]
    assert (conn["data"]["disco_group"], conn["data"]["tax"]["province"]) == ("ke", "sindh")
    # so 15 December's peak is K-Electric's 18:00 to 22:00, not the other DISCOs' 17:00 to 21:00
    peak = a.get(f"/api/sites/{ke}/prices", params={"day": "2026-12-15"}).json()["peak"]
    assert [i for i, p in enumerate(peak) if p] == list(range(72, 88))
    # the other DISCOs' group on a K-Electric site is refused in a sentence, and K-Electric's on a FESCO site
    r = a.put(f"/api/sites/{ke}/connection", json={"id": conn["id"], "tariff_category": "B-2(b)", "sanctioned_load_kw": 300, "disco_group": "xwdisco"})
    assert r.status_code == 422 and r.json()["code"] == "invalid", r.text
    assert r.json()["detail"] == "This site's DISCO is K-Electric: its connection uses K-Electric's time-of-use windows"
    held = _site(a, sid)["connections"][0]
    r = a.put(f"/api/sites/{sid}/connection", json={"id": held["id"], "tariff_category": "B-2(b)", "disco_group": "ke", "tax": {"province": "sindh"}})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "This site's DISCO is FESCO: its connection uses the other DISCOs' time-of-use windows, not K-Electric's"
    assert _site(a, sid)["connections"][0]["data"]["disco_group"] == "xwdisco"
    # a province that is given is kept (K-Electric's Hub customers pay Balochistan's duty); tax flags sent without one take the DISCO's
    r = a.put(f"/api/sites/{ke}/connection", json={"id": conn["id"], "tariff_category": "B-2(b)", "sanctioned_load_kw": 300, "tax": {"province": "balochistan"}})
    assert r.json()["connections"][0]["data"]["tax"]["province"] == "balochistan", r.text
    r = a.put(f"/api/sites/{ke}/connection", json={"id": conn["id"], "tariff_category": "B-2(b)", "sanctioned_load_kw": 300, "tax": {"atl": False}})
    assert r.json()["connections"][0]["data"]["tax"] == {**conn["data"]["tax"], "atl": False, "province": "sindh"}, r.text
    # a site with no DISCO keeps the defaults, and may use either group
    other = world.other
    r = a.put(f"/api/sites/{other}/connection", json={"tariff_category": "A-2(c)", "sanctioned_load_kw": 50, "disco_group": "ke", "tax": {"province": "sindh"}})
    assert r.status_code == 200 and r.json()["connections"][0]["data"]["disco_group"] == "ke", r.text
    # a site whose DISCO was changed after its connection was saved is not activated with the other DISCO's windows
    with world.use():
        sites.put_asset(sid, "inverter", {"kw_ac": 100}, "test")
    assert a.put(f"/api/sites/{sid}", json={"disco": "K-Electric", "data_source": {"kind": "csv", "grid_signal_confirmed": True}}).status_code == 200
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.status_code == 422 and r.json()["code"] == "incomplete", r.text
    assert r.json()["detail"] == "Add the DISCO group of each grid connection, to match the site's DISCO (tab C) before activating"
    r = a.put(f"/api/sites/{sid}/connection", json={"id": held["id"], "tariff_category": "B-2(b)", "sanctioned_load_kw": 400})
    assert r.json()["connections"][0]["data"]["disco_group"] == "ke", r.text
    assert a.post(f"/api/sites/{sid}/activate").status_code == 200


# ------------------------------------------------------------------------------------------------ engine-tariffs-05 (and flow-onboarding-13, engine-money-live-10)

def test_tax_flags_the_icp_baseline_and_tariff_f_are_checked_and_no_strn_bears_the_gst(world):
    a, sid = world.c["admin"], world.site
    cid = _site(a, sid)["connections"][0]["id"]
    put = lambda **body: a.put(f"/api/sites/{sid}/connection", json={"id": cid, "sanctioned_load_kw": 400, **body})  # noqa: E731
    # no STRN said alone: a company without a sales-tax registration cannot claim the bill's GST back, so it bears it (it kept gst_recoverable true)
    r = put(tariff_category="A-2(c)", tax={"strn": False})
    assert r.status_code == 200, r.text
    tax = r.json()["connections"][0]["data"]["tax"]
    assert (tax["strn"], tax["gst_recoverable"]) == (False, False)
    with world.use():
        from datetime import date
        from gridlytics import tariffs
        conn = sites.connections(sid)[0]
        bears = tariffs.energy_factor(conn, date(2026, 11, 2))
        registered = tariffs.energy_factor({**conn, "data": {**conn["data"], "tax": {**tax, "strn": True, "gst_recoverable": True}}}, date(2026, 11, 2))
    assert bears == pytest.approx(registered * (1 + 0.03 + 0.18)), (bears, registered)
    # saying a company with no STRN recovers GST is refused in a sentence
    r = put(tariff_category="A-2(c)", tax={"strn": False, "gst_recoverable": True})
    assert r.status_code == 422 and r.json()["detail"] == "A connection without a sales-tax registration (STRN) cannot recover the bill's GST", r.text
    # the ICP baseline is by calendar month (1 to 12), in kWh from 0 up
    for icp, sentence in (({"eligible": True, "peak": {"13": 100.0}}, "The ICP baseline is by calendar month, 1 to 12"),
                          ({"eligible": True, "off_peak": {"1": -5.0}}, "An ICP baseline is from 0 to 100,000,000 kWh"),
                          ({"eligible": True, "peak": {"1": 1e12}}, "An ICP baseline is from 0 to 100,000,000 kWh")):
        r = put(tariff_category="B-2(b)", icp=icp)
        assert r.status_code == 422 and r.json()["detail"] == sentence, (icp, r.text)
    months = {str(m): 1000.0 * m for m in range(1, 13)}
    r = put(tariff_category="B-2(b)", icp={"eligible": True, "peak": months, "off_peak": {"01": 5.0, **{str(m): 9000.0 for m in range(2, 13)}}})
    assert r.status_code == 200, r.text
    icp = r.json()["connections"][0]["data"]["icp"]
    assert icp["eligible"] is True and icp["peak"] == months and icp["off_peak"]["1"] == 5.0 and "01" not in icp["off_peak"]
    # Tariff F: a season of distinct months 1 to 12, six at least (R9 1.2), on an industrial connection
    r = put(tariff_category="B-2(b)", seasonal_multiplier=1.25, season_months=[4, 5, 6, 7, 8, 9])
    assert r.status_code == 200 and r.json()["connections"][0]["data"]["season_months"] == [4, 5, 6, 7, 8, 9], r.text
    for months, category, sentence in (([4, 5], "B-2(b)", "Tariff F's season runs at least six months"),
                                       ([0, 4, 5, 6, 7, 8], "B-2(b)", "A season month is 1 to 12"),
                                       ([4, 4, 5, 6, 7, 8], "B-2(b)", "Each season month is listed once"),
                                       ([4, 5, 6, 7, 8, 9], "A-2(c)", "Tariff F (a season) is for industrial connections")):
        r = put(tariff_category=category, seasonal_multiplier=1.25, season_months=months)
        assert r.status_code == 422 and r.json()["detail"] == sentence, (months, category, r.text)
    # GET /meta names the categories the ICP applies to, for the web app's tab C
    meta = a.get("/api/meta").json()
    assert "B-2(b)" in meta["icp_categories"] and "A-2(c)" not in meta["icp_categories"]


# ------------------------------------------------------------------------------------------------ flow-onboarding-10

def test_a_gas_genset_takes_the_gas_curve_in_mmbtu_and_no_tank(world):
    a, sid = world.c["admin"], world.site
    put = lambda **body: a.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 800, **body})  # noqa: E731
    # the fuel's typical curve where the save leaves it out: diesel in litres with a tank, captive gas and RLNG in MMBtu (their price's unit), piped
    for kind, curve in (("hsd", (0.273, 0.033, 2000.0)), ("gas_captive", (0.0085, 0.0010, 0.0)), ("lng_captive", (0.0085, 0.0010, 0.0))):
        r = put(fuel_kind=kind)
        assert r.status_code == 200, r.text
        g = r.json()["assets"]["genset"]
        assert (g["a"], g["b"], g["tank_l"]) == curve, (kind, g)
    # a full-load kWh of an 800 kW RLNG engine costs about Rs 46 at the placeholder Rs 4,800 an MMBtu, not about Rs 1,469 on the diesel curve
    with world.use():
        from datetime import date
        from gridlytics import tariffs
        price, _ = tariffs.fuel_price("lng_captive", date(2026, 11, 2))
        g = sites.assets(sid)["genset"]
    assert price == 4800.0
    assert (g["a"] * 800 + g["b"] * 800) * price / 800 == pytest.approx(45.6)
    # the other fuel's curve, or a tank on piped gas, is refused in a sentence
    for body, sentence in (({"fuel_kind": "gas_captive", "a": 0.273, "b": 0.001},
                            "A gas genset's fuel curve is in MMBtu: its slope (a) is at most 0.05 MMBtu per kWh"),
                           ({"fuel_kind": "gas_captive", "b": 0.033}, "A gas genset's fuel at no load (b) is at most 0.01 MMBtu an hour per rated kW"),
                           ({"fuel_kind": "lng_captive", "tank_l": 2000}, "A genset on piped gas has no tank: its tank is 0 L"),
                           ({"fuel_kind": "hsd", "a": 0.0085, "b": 0.001},
                            "A diesel genset's fuel curve is in litres: its slope (a) is at least 0.1 L per kWh")):
        r = put(**body)
        assert r.status_code == 422 and r.json()["detail"] == sentence, (body, r.text)
    assert sites.GensetFields.model_validate({"rated_kw": 1000, "fuel_kind": "gas_captive", "a": 0.0085, "b": 0.0010, "tank_l": 0}).a == 0.0085


# ------------------------------------------------------------------------------------------------ flow-onboarding-16

def test_a_gensets_rating_may_be_given_in_kva_at_its_power_factor(world):
    a, sid = world.c["admin"], world.site
    put = lambda **body: a.put(f"/api/sites/{sid}/assets/genset", json=body)  # noqa: E731
    # a 250 kVA nameplate at the usual 0.8 is a 200 kW set: typing 250 as kW overstated it by a quarter
    r = put(rated_kva=250)
    assert r.status_code == 200, r.text
    g = r.json()["assets"]["genset"]
    assert (g["rated_kva"], g["power_factor"], g["rated_kw"]) == (250.0, 0.8, 200.0)
    r = put(rated_kva=400, power_factor=0.85, rated_kw=340)
    assert r.status_code == 200 and r.json()["assets"]["genset"]["rated_kw"] == 340.0, r.text
    r = put(rated_kva=333, rated_kw=266.40000000000003)
    assert r.status_code == 200, r.text
    # a kW that disagrees with the kVA and the power factor is refused, as is a power factor out of its range
    r = put(rated_kva=250, rated_kw=250)
    assert r.status_code == 422 and r.json()["detail"] == "A genset's rating in kW is its kVA times its power factor: 250 kVA at 0.8 is 200 kW", r.text
    r = put(rated_kva=250, power_factor=0.3)
    assert r.status_code == 422 and "power factor" in r.json()["detail"].lower(), r.text
    # a rating in kW alone is kept as before (no kVA recorded)
    r = put(rated_kw=150)
    assert r.status_code == 200 and r.json()["assets"]["genset"]["rated_kva"] is None and r.json()["assets"]["genset"]["rated_kw"] == 150.0, r.text


# ------------------------------------------------------------------------------------------------ flow-onboarding-11

def test_a_soc_threshold_genset_starts_at_the_battery_level_the_site_records(world):
    op, sid = world.c["operator"], world.site
    # how the genset is started is the site's hardware, not a control limit: an operator records it with the threshold its controller starts at
    r = op.put(f"/api/sites/{sid}", json={"genset_start_control": "soc_threshold", "genset_start_soc_pct": 35})
    assert r.status_code == 200 and r.json()["data"]["genset_start_soc_pct"] == 35.0, r.text
    with world.use():
        from datetime import datetime, timezone
        from gridlytics import forecast, plans, twin
        full = sites.full(sid)
        assert sites.genset_start_soc(full["data"]) == pytest.approx(0.35)
        assert twin.plant_of(full).gen_soc_on == pytest.approx(0.35)
        t0 = forecast.align(datetime.now(timezone.utc).timestamp())
        p = plans.build_problem(full, t0, forecast.issue(full, t0, save=False), 96)
        assert p["genset"]["control"] == "soc_threshold" and p["genset"]["soc_on"] == pytest.approx(0.35)
    # none recorded: the reserve floor plus 5 points, as before (the web app marks it Preliminary)
    r = op.put(f"/api/sites/{sid}", json={"genset_start_soc_pct": None})
    assert r.status_code == 200, r.text
    with world.use():
        full = sites.full(sid)
        assert sites.genset_start_soc(full["data"]) == pytest.approx(0.25) and twin.plant_of(full).gen_soc_on == pytest.approx(0.25)
    for bad in (0, 101):
        r = op.put(f"/api/sites/{sid}", json={"genset_start_soc_pct": bad})
        assert r.status_code == 422, (bad, r.text)


# ------------------------------------------------------------------------------------------------ flow-onboarding-09 and flow-onboarding-17

def test_the_site_answer_says_where_the_equipment_disagrees_and_when_no_feeder_is_given(world):
    a, sid = world.c["admin"], world.site
    cid = _site(a, sid)["connections"][0]["id"]
    assert a.put(f"/api/sites/{sid}/assets/inverter", json={"brand": "deye", "kw_ac": 50}).status_code == 200
    assert a.put(f"/api/sites/{sid}/assets/pv", json={"kwp_dc": 300, "kw_ac": 250}).status_code == 200
    assert a.put(f"/api/sites/{sid}/assets/battery", json={"nameplate_kwh": 215, "p_ch_kw": 400, "p_dis_kw": 400}).status_code == 200
    r = a.put(f"/api/sites/{sid}/connection", json={"id": cid, "tariff_category": "B-2(b)", "sanctioned_load_kw": 100, "export_cap_kw": 5000,
                                                    "regime": "net_billing"})
    assert r.status_code == 200, r.text
    r = a.put(f"/api/sites/{sid}", json={"critical_load_kw": [500.0] * 24, "load_peak_kw": 300})
    assert r.status_code == 200, r.text
    # saves are not refused: each answer says what disagrees (a site may have PV on inverters of its own, or a battery rated above its inverter)
    for answer in (r.json(), _site(a, sid), a.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 150}).json(),
                   a.put(f"/api/sites/{sid}/connection", json={"id": cid, "tariff_category": "B-2(b)", "sanctioned_load_kw": 100, "export_cap_kw": 5000,
                                                               "regime": "net_billing"}).json()):
        assert answer["setup_warnings"] == [
            "The PV's AC size (250 kW) is above the inverter's (50 kW): the plan passes no more than 50 kW of PV and battery power through it",
            "The battery's charge power (400 kW) is above the inverter's AC size (50 kW): the plan keeps it within 50 kW",
            "The battery's discharge power (400 kW) is above the inverter's AC size (50 kW): the plan keeps it within 50 kW",
            "The export cap (5,000 kW) is above the inverter's AC size (50 kW): the site cannot export more than the inverter passes",
            "The export cap (5,000 kW) is above the sanctioned load (100 kW): a net-metering or net-billing agreement allows no more than the sanctioned load",
            "The critical load (500 kW at most) is more than the battery can give through the inverter (50 kW): it cannot carry it alone in an outage",
            "The critical load (500 kW at most) is above the typical peak load (300 kW): check both",
            "No feeder or transformer code is given: the outage forecast learns from this site's own outages only, not from the company's other sites on "
            "its feeder",
        ], answer["setup_warnings"]
    # PV far above its DC size is a typing slip worth a word too
    r = a.put(f"/api/sites/{sid}/assets/pv", json={"kwp_dc": 10, "kw_ac": 40})
    assert "The PV's AC size (40 kW) is more than one and a half times its DC size (10 kWp): check both" in r.json()["setup_warnings"], r.text
    # equipment that agrees, and a feeder code, leave nothing to say
    assert a.put(f"/api/sites/{sid}/assets/inverter", json={"brand": "deye", "kw_ac": 600}).status_code == 200
    assert a.put(f"/api/sites/{sid}/assets/pv", json={"kwp_dc": 300, "kw_ac": 250}).status_code == 200
    a.put(f"/api/sites/{sid}/connection", json={"id": cid, "tariff_category": "B-2(b)", "sanctioned_load_kw": 400, "export_cap_kw": 100,
                                                "regime": "net_billing"})
    r = a.put(f"/api/sites/{sid}", json={"critical_load_kw": [200.0] * 24, "outage_unit": {"kind": "feeder", "code": "411205", "name": "Jhang Road"}})
    assert r.status_code == 200 and r.json()["setup_warnings"] == [], r.text
    # an off-grid site has no feeder to pool
    other = world.other
    assert a.put(f"/api/sites/{other}", json={"off_grid": True}).json()["setup_warnings"] == []


# ------------------------------------------------------------------------------------------------ flow-onboarding-22

def test_a_source_test_says_what_the_data_carries_and_a_source_without_grid_presence_is_not_activated(world):
    a, op, v, sid = world.c["admin"], world.c["operator"], world.c["viewer"], world.site
    assert v.post(f"/api/sites/{sid}/source/test").status_code == 403
    # nothing received yet: said so, and nothing is refused on that account (a site is set up before its data flows)
    r = op.post(f"/api/sites/{sid}/source/test")
    assert r.status_code == 200, r.text
    t = r.json()
    assert (t["kind"], t["grid_signal"], t["rows"], t["first_at"], t["last_at"], t["points"]) == (None, "no_data", 0, None, None, [])
    assert t["sentence"] == "No data has arrived from this site yet: test the source again once its first readings are in"
    assert a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "csv", "grid_signal_confirmed": True}}).status_code == 200
    with world.use():
        sites.put_asset(sid, "inverter", {"kw_ac": 100}, "test")
        now = telemetry._now_ts() // 60 * 60
        telemetry.ingest(sid, [{"ts": now - 60 * i, "pv_power_w": 900, "load_power_w": 1500} for i in range(1, 31)], "csv")
    # data without grid presence: the test says so, and activation is refused although the tick says it is there
    t = op.post(f"/api/sites/{sid}/source/test").json()
    assert (t["kind"], t["grid_signal"], t["rows"]) == ("csv", "missing", 30), t
    assert t["points"] == ["pv_power_w", "load_power_w"]
    assert t["first_at"] < t["last_at"] and t["last_at"].endswith("+00:00")
    assert t["sentence"] == ("The data received carries no grid presence (grid_present or the grid voltage): outages cannot be seen and the reserve "
                             "cannot be planned. Add the point to the source, or choose a source that reports it")
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.status_code == 422 and r.json()["code"] == "incomplete", r.text
    assert r.json()["detail"] == ("Add the grid-presence signal to the data the source sends (the data received has neither grid_present nor the grid "
                                  "voltage; tab A) before activating")
    # the grid voltage counts (ingest reads grid presence from it): found, and the site is activated
    with world.use():
        telemetry.ingest(sid, [{"ts": now, "pv_power_w": 900, "grid_voltage_v": 231}], "csv")
    t = op.post(f"/api/sites/{sid}/source/test").json()
    assert (t["grid_signal"], t["rows"]) == ("found", 31), t
    assert t["points"] == ["grid_present", "grid_voltage_v", "pv_power_w", "load_power_w"]
    # in words, not the points' keys (the web app prints the sentence; the keys are in `points`)
    assert t["sentence"] == "Grid presence is in the data received (the grid-present signal and the grid voltage), so outages can be seen"
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.status_code == 200 and r.json()["status"] == "active", r.text


def test_an_off_grid_site_needs_no_grid_presence_in_its_data(world):
    a, other = world.c["admin"], world.other
    assert a.put(f"/api/sites/{other}", json={"off_grid": True, "data_source": {"kind": "csv", "grid_signal_confirmed": True}}).status_code == 200
    with world.use():
        sites.put_asset(other, "inverter", {"kw_ac": 20}, "test")
        telemetry.ingest(other, [{"ts": telemetry._now_ts() // 60 * 60 - 60, "pv_power_w": 900}], "csv")
    assert a.post(f"/api/sites/{other}/source/test").json()["grid_signal"] == "missing"
    r = a.post(f"/api/sites/{other}/activate")
    assert r.status_code == 200, r.text


def test_mode_b_without_an_edge_device_is_a_setup_warning_and_meta_gives_the_fallback(world):
    a, sid = world.c["admin"], world.site
    warning = ("Mode B (set-points written by the edge device) needs an edge device: with a CSV or cloud source the plan reaches the inverter as "
               "slot tables a person keys in, so choose Mode A or advisory")
    r = a.put(f"/api/sites/{sid}", json={"control_mode": "mode_b", "data_source": {"kind": "csv", "grid_signal_confirmed": True}})
    assert r.status_code == 200 and warning in r.json()["setup_warnings"], r.text
    assert warning in a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "oem_cloud", "grid_signal_confirmed": True}}).json()["setup_warnings"]
    assert warning not in a.put(f"/api/sites/{sid}", json={"data_source": {"kind": "edge", "grid_signal_confirmed": True}}).json()["setup_warnings"]
    assert warning not in a.put(f"/api/sites/{sid}", json={"control_mode": "mode_a", "data_source": {"kind": "csv", "grid_signal_confirmed": True}}).json()[
        "setup_warnings"]
    # what every plan falls back to (delivery's fallback block), for tab D to show
    assert a.get("/api/meta").json()["fallback"] == {"on_expiry": "inverter_native", "revert_after_hours": delivery.FALLBACK_HOURS}


# ------------------------------------------------------------------------------------------------ flow-savings-pilot-bill-12, the capture form's half

def test_a_capture_a_person_sends_cannot_claim_the_edge_read_it_back(world):
    # POST /native is a person's (edit_sites; no API key holds a role permission): a capture typed in or taken from a photograph said it was "read back
    # by the edge device", the most trusted source, and the pilot's baseline took it as such
    a, op, sid = world.c["admin"], world.c["operator"], world.site
    for who in (a, op):
        r = _native(who, sid, ["00:00", "17:00"], source="read_back")
        assert r.status_code == 422 and r.json()["code"] == "invalid", r.text
        assert r.json()["detail"] == ("Only the edge device reads the inverter's settings back: a capture sent here was typed in or taken from a photograph "
                                      "of the screen, so say which")
    with world.use():
        assert sites.native_versions(sid) == []
    for source in ("entered", "photo"):
        r = _native(op, sid, ["00:00", "17:00"], source=source)
        assert r.status_code == 200 and r.json()["body"]["source"] == source, r.text
    # the edge's own read-back (the demo's captures are made so) is recorded as such
    with world.use():
        assert sites.capture_native(sid, {"source": "read_back", "work_mode": "self_use"}, "edge")["body"]["source"] == "read_back"


# ------------------------------------------------------------------------------------------------ flow-onboarding-02, an active site's DISCO changed later

def test_a_site_whose_disco_changed_after_its_connection_was_saved_is_told_in_every_answer(world):
    # activation asks for the group to match, but a site already active whose DISCO is changed in Site settings General was priced on the other DISCO's
    # peak windows with no word anywhere
    a, sid = world.c["admin"], world.site
    held = _site(a, sid)["connections"][0]
    assert held["data"]["disco_group"] == "xwdisco"
    warning = ("The grid connection is priced on the other DISCOs' time-of-use windows, but this site's DISCO is K-Electric: save the connection again "
               "with K-Electric's (tab C, or Site settings Tariff)")
    r = a.put(f"/api/sites/{sid}", json={"disco": "K-Electric"})
    assert r.status_code == 200 and warning in r.json()["setup_warnings"], r.text
    assert warning in _site(a, sid)["setup_warnings"]
    r = a.put(f"/api/sites/{sid}/connection", json={"id": held["id"], "tariff_category": "B-2(b)", "sanctioned_load_kw": 400})
    assert r.status_code == 200 and r.json()["connections"][0]["data"]["disco_group"] == "ke", r.text
    assert not [w for w in r.json()["setup_warnings"] if "time-of-use windows" in w], r.text
    # and the other way round
    r = a.put(f"/api/sites/{sid}", json={"disco": "FESCO"})
    assert ("The grid connection is priced on K-Electric's time-of-use windows, but this site's DISCO is FESCO: save the connection again with the other "
            "DISCOs' (tab C, or Site settings Tariff)") in r.json()["setup_warnings"], r.text
