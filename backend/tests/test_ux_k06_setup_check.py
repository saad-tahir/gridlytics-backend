"""UX backend ask K6 (plan H11; C19.2 item 2, A8.2 to A8.4, C5.4 to C5.6, C7.4, C9.2; steps 9b and 9c, Set-up B to D and Review).

`POST /api/sites/{id}/validate` checks a step's section as typed, over the site as stored, by the models a save is checked by, and saves nothing:
`problems` (each on its field and its step), `setup_warnings` of the site as it would be, and `derived`, the figures the server works out (the battery's
nominal and usable kWh, the genset's usable kW and its fuel an hour at half and full load, the reserve floor's kWh and minutes of the critical load,
the wear cost per kWh for see_money_settings only), so the page shows the server's figures rather than its own (the server is the authority).
The goldens are worked by hand below."""
from __future__ import annotations

import json

import identities
from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import money, sites, store

AH_BATTERY = {"entry_unit": "ah", "capacity_ah": 1000, "nominal_v": 51.2, "p_ch_kw": 50, "p_dis_kw": 50}
CRITICAL = [0.0] * 8 + [60.0] * 10 + [0.0] * 6           # 60 kW from 08:00 to 18:00


def _check(w, body, role="admin", site=None):
    return w.c[role].post(f"/api/sites/{site or w.site}/validate", json=body)


def test_the_battery_s_nominal_and_usable_kwh_and_its_wear_cost_are_the_server_s(world):
    r = _check(world, {"assets": {"battery": AH_BATTERY}})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["ok"] is True and got["problems"] == [], got
    b = got["derived"]["battery"]
    # golden: 1,000 Ah x 51.2 V = 51.2 kWh nominal; usable at the window 51.2 x 1.00 x (0.95 - 0.10) = 43.52 kWh
    assert (b["nominal_kwh"], b["usable_kwh"]) == (51.2, 43.52)
    assert (b["entry_unit"], b["capacity_ah"], b["nominal_v"], b["soc_min_pct"], b["soc_max_pct"], b["soh_pct"]) == ("ah", 1000, 51.2, 10.0, 95.0, 100.0)
    # golden: Rs 52,000 a kWh over 6,000 warranted cycles at 80 % depth = 52,000 / 4,800 = Rs 10.83 per kWh discharged (the defaults)
    assert b["wear_cost_rs_per_kwh"] == 10.83
    # a pack at 90 % health, 20 % to 90 %, Rs 40,000 over 4,000 cycles at 90 %: 100 x 0.9 x 0.7 = 63 kWh; 40,000 / 3,600 = Rs 11.11
    b = _check(world, {"assets": {"battery": {"nameplate_kwh": 100, "soh": 0.9, "soc_min": 0.2, "soc_max": 0.9, "p_ch_kw": 50, "p_dis_kw": 50,
                                              "replacement_cost_per_kwh": 40000, "warranted_cycles": 4000, "warranted_depth": 0.9}}}).json()
    assert (b["derived"]["battery"]["usable_kwh"], b["derived"]["battery"]["wear_cost_rs_per_kwh"]) == (63.0, 11.11), b


def test_the_genset_s_usable_kw_and_its_fuel_an_hour_at_half_and_full_load(world):
    g = _check(world, {"assets": {"genset": {"rating_entry": "kva", "rated_kva": 500, "power_factor": 0.8}}}).json()["derived"]["genset"]
    # golden: 500 kVA x 0.80 = 400 kW; diesel's typical curve a 0.273 L/kWh, b 0.033 L/h per rated kW:
    # half load 0.033 x 400 + 0.273 x 200 = 13.2 + 54.6 = 67.8 L/h; full load 13.2 + 109.2 = 122.4 L/h (plan C5.5: "about 68", "about 122")
    assert (g["usable_kw"], g["rating_entry"], g["rated_kva"], g["power_factor"], g["fuel_unit"]) == (400.0, "kva", 500, 0.8, "L")
    assert g["fuel_per_hour"] == {"half_load": 67.8, "full_load": 122.4, "unit": "L/h", "estimated": True}
    # a captive gas engine of 400 kW on the gas curve, a 0.0085 MMBtu/kWh, b 0.0010 MMBtu/h per rated kW: 0.4 + 1.7 = 2.1; 0.4 + 3.4 = 3.8 MMBtu/h
    g = _check(world, {"assets": {"genset": {"rated_kw": 400, "fuel_kind": "gas_captive"}}}).json()["derived"]["genset"]
    assert (g["fuel_unit"], g["fuel_per_hour"]) == ("MMBtu", {"half_load": 2.1, "full_load": 3.8, "unit": "MMBtu/h", "estimated": True})


def test_the_reserve_floor_s_energy_and_minutes_of_the_critical_load(world):
    body = {"site": {"critical_load_kw": CRITICAL, "reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 20}},
            "assets": {"battery": AH_BATTERY, "inverter": {"brand": "deye", "kw_ac": 50}}}
    res = _check(world, body).json()["derived"]["reserve"]
    # golden: the floor 20 % to the battery's cut-off 10 % of 51.2 kWh, after 95 % discharge efficiency: 0.10 x 51.2 x 0.95 = 4.864 kWh;
    # at the critical load's 60 kW, 4.864 / 60 h = 4.9 minutes; the battery gives 50 kW (its discharge power, the inverter's 50 kW alike)
    assert res == {"floor_pct": 20.0, "floor_set_pct": 20.0, "battery_low_pct": 10.0, "held_kwh": 4.86, "critical_kw": 60.0, "minutes": 4.9,
                   "battery_kw": 50.0}
    # a floor typed under the battery's cut-off plus 2 points is held there (the delivered floor: plans.delivered_floor_pct): 12 % of 51.2 x 0.95
    res = _check(world, {**body, "site": {"critical_load_kw": CRITICAL, "reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 5}}}).json()
    assert (res["derived"]["reserve"]["floor_pct"], res["derived"]["reserve"]["floor_set_pct"], res["derived"]["reserve"]["held_kwh"]) == (12.0, 5.0, 0.97)
    # no critical load: no minutes; an inverter smaller than the discharge power gives its own kW
    res = _check(world, {"site": {"critical_load_kw": [0.0] * 24}, "assets": {"battery": AH_BATTERY, "inverter": {"brand": "deye", "kw_ac": 30}}}).json()
    assert (res["derived"]["reserve"]["minutes"], res["derived"]["reserve"]["battery_kw"]) == (None, 30.0)
    # the stored site: Mill One's 200 kWh battery (10 % to 95 %, floor 20 %) holds 0.10 x 200 x 0.95 = 19 kWh; its 20 kW critical load, 57 minutes
    res = _check(world, {}).json()["derived"]
    assert (res["reserve"]["held_kwh"], res["reserve"]["minutes"], res["battery"]["usable_kwh"], res["genset"]["usable_kw"]) == (19.0, 57.0, 170.0, 150.0)


def test_every_problem_is_on_its_field_and_its_step_in_the_form_s_words(world):
    body = {"site": {"name": "", "reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 120}},
            "assets": {"battery": {"nameplate_kwh": 100, "p_ch_kw": 0, "p_dis_kw": 50, "soh": 0.2}, "genset": {"rated_kw": 400, "f_min": 1.5}},
            "connection": {"tariff_category": "B-2(b)", "sanctioned_load_kw": -5, "tax": {"strn": False, "gst_recoverable": True}}}
    got = _check(world, body).json()
    assert got["ok"] is False
    assert [(p["step"], p["field"]) for p in got["problems"]] == [
        ("a", "site.name"), ("b", "assets.battery.soh"), ("b", "assets.battery.p_ch_kw"), ("b", "assets.genset.f_min"),
        ("c", "connection.sanctioned_load_kw"), ("c", "connection.tax.gst_recoverable"), ("d", "site.reserve_policy.min_soc_pct")]
    words = {p["field"]: p["message"] for p in got["problems"]}
    assert words == {"site.name": "The name is needed",
                     "assets.battery.soh": "The state of health is a percent above 30, up to 100",
                     "assets.battery.p_ch_kw": "The charge power (kW) must be more than 0",
                     "assets.genset.f_min": "The minimum loading is a percent from 0 to 100",
                     "connection.sanctioned_load_kw": "The sanctioned load (kW) must be more than 0",
                     "connection.tax.gst_recoverable": "A connection without a sales-tax registration (STRN) cannot recover the bill's GST",
                     "site.reserve_policy.min_soc_pct": "The reserve floor (% state of charge) must be at most 100"}
    assert next(p for p in got["problems"] if p["field"] == "assets.battery.soh")["path"] == ["assets", "battery", "soh"]
    # a figure that cannot be worked out is null, never 0, with why
    assert got["derived"] == {"battery": None, "genset": None, "reserve": None}
    assert got["not_derived"] == {"battery": "The state of health is a percent above 30, up to 100",
                                  "reserve": "The state of health is a percent above 30, up to 100",
                                  "genset": "The minimum loading is a percent from 0 to 100"}
    # a model's own check is on the field it concerns (the save refuses with the same sentence)
    got = _check(world, {"assets": {"battery": {"nameplate_kwh": 100, "p_ch_kw": 50, "p_dis_kw": 50, "soc_min": 0.5, "soc_max": 0.4},
                                    "genset": {"rating_entry": "kva", "rated_kva": 500, "power_factor": 0.8, "rated_kw": 500}}}).json()
    assert [(p["field"], p["message"]) for p in got["problems"]] == [
        ("assets.battery.soc_min", "The battery's lowest state of charge must be below its highest"),
        ("assets.genset.rated_kva", "A genset's rating in kW is its kVA times its power factor: 500 kVA at 0.8 is 400 kW")]
    r = world.c["admin"].put(f"/api/sites/{world.site}/assets/battery", json={"nameplate_kwh": 100, "p_ch_kw": 50, "p_dis_kw": 50, "soc_min": 0.5,
                                                                            "soc_max": 0.4})
    assert r.json()["detail"] == "The battery's lowest state of charge must be below its highest"
    # the DISCO group the site's DISCO contradicts (Mill One is FESCO's), as the connection's save refuses it
    got = _check(world, {"connection": {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400, "disco_group": "ke"}}).json()
    assert got["problems"] == [{"step": "c", "field": "connection.disco_group", "path": ["connection", "disco_group"],
                                "message": "This site's DISCO is FESCO: its connection uses the other DISCOs' time-of-use windows, not K-Electric's"}]


def test_the_warnings_are_the_draft_s_and_nothing_is_saved(world):
    with world.use():
        before = sites.full(world.site)
        audit = len(store.list_audit(limit=1000))
    body = {"site": {"critical_load_kw": [300.0] * 24, "load_peak_kw": 200},
            "assets": {"inverter": {"brand": "deye", "kw_ac": 50}, "battery": {**AH_BATTERY, "p_dis_kw": 400}, "genset": None},
            "connection": {"id": before["connections"][0]["id"], "tariff_category": "B-2(b)", "sanctioned_load_kw": 400, "export_cap_kw": 600,
                           "if_unchanged": {"tariff_category": "B-2(b)"}}}
    got = _check(world, body).json()
    assert got["ok"] is True, got["problems"]
    w = got["setup_warnings"]
    assert "The battery's discharge power (400 kW) is above the inverter's AC size (50 kW): the plan keeps it within 50 kW" in w
    assert ("The export cap (600 kW) is above the sanctioned load (400 kW): a net-metering or net-billing agreement allows no more than the sanctioned "
            "load") in w
    assert "The critical load (300 kW at most) is above the typical peak load (200 kW): check both" in w
    # the genset unticked: the site as it would be has none
    assert got["derived"]["genset"] is None and "genset" not in got["not_derived"]
    with world.use():
        after = sites.full(world.site)
        assert len(store.list_audit(limit=1000)) == audit
    for k in ("data", "assets", "connections"):
        assert json.dumps(after[k], sort_keys=True) == json.dumps(before[k], sort_keys=True), k
    # a connection id the site does not have is no connection of it
    r = _check(world, {"connection": {"id": "not-a-connection", "tariff_category": "B-2(b)"}})
    assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
    # the request's own shape is strict: one sentence
    r = _check(world, {"assets": {"boiler": {}}})
    assert (r.status_code, r.json()["detail"]) == (422, "Unknown field 'assets.boiler'"), r.text
    r = _check(world, {"steps": "b"})
    assert (r.status_code, r.json()["detail"]) == (422, "Unknown field 'steps'"), r.text


def test_an_operator_reads_the_figures_without_the_wear_cost_and_a_viewer_and_a_key_are_refused(world):
    body = {"assets": {"battery": AH_BATTERY}}
    admin = _check(world, body).json()
    for role in ("operator", "analyst"):
        r = _check(world, body, role)
        assert r.status_code == 200, (role, r.text)
        b = r.json()["derived"]["battery"]
        if role == "operator":
            # plan 9.4: an operator sees costs but not the money settings (the wear is made from them alone)
            assert "wear_cost_rs_per_kwh" not in b and b == {k: v for k, v in admin["derived"]["battery"].items() if k != "wear_cost_rs_per_kwh"}
        else:
            assert b == admin["derived"]["battery"]
    assert money.is_money("wear_cost_rs_per_kwh") and "wear_cost_rs_per_kwh" in money.MONEY_SETTING_KEYS
    assert "wear_cost_rs_per_kwh" not in json.dumps(money.for_key(admin))
    r = _check(world, body, "viewer")
    assert (r.status_code, r.json()["detail"]) == (403, "Checking a site's set-up is for an administrator, an operator or an analyst"), r.text
    r = world.anon.post(f"/api/sites/{world.site}/validate", json=body, headers=world.key(["sites:read", "plans:read"]))
    assert r.status_code == 403, r.text
    # an operator's form sends the money settings it is not shown at their defaults: the check reads the stored ones, as the save keeps them
    with world.use():
        sites.put_asset(world.site, "battery", {**AH_BATTERY, "replacement_cost_per_kwh": 30000}, "test")
    sent = {**AH_BATTERY, "replacement_cost_per_kwh": 52000.0, "warranted_cycles": 6000.0, "warranted_depth": 0.8}
    assert _check(world, {"assets": {"battery": sent}}, "operator").status_code == 200
    # golden: Rs 30,000 / (6,000 x 0.8) = Rs 6.25, the stored price, for the administrator who sends the form's values as read
    assert _check(world, {"assets": {"battery": {**AH_BATTERY, "replacement_cost_per_kwh": 30000}}}).json()["derived"]["battery"][
        "wear_cost_rs_per_kwh"] == 6.25


def test_another_company_s_site_is_no_site_here(world):
    """The route answers this company's site (so a 404 below is its own refusal, not a path that is not served), and another company's site, with a
    connection and a battery, is no site here, for every role that reads the check (UX K6 check)."""
    other = identities.company("Another company", own_file=True)
    from gridlytics import platform as platformdb
    with store.use_company(platformdb.company_file(other)):
        theirs = sites.create({"name": "Their Mill", "disco": "LESCO"}, "t")["id"]
        held = sites.put_connection(theirs, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "t")
        sites.put_asset(theirs, "battery", {"nameplate_kwh": 100, "p_ch_kw": 50, "p_dis_kw": 50}, "t")
    for role in ("admin", "operator", "analyst"):
        assert _check(world, {}, role).status_code == 200, role
        for body in ({}, {"assets": {"battery": AH_BATTERY}}, {"connection": {"id": held["id"], "tariff_category": "B-2(b)"}}):
            r = _check(world, body, role, site=theirs)
            assert (r.status_code, r.json()["detail"]) == (404, "No such site"), (role, r.text)
