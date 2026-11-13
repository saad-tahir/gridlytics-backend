"""UX K6 check (plan H11; C7.3, C7.4, C19.2 item 2): the checkers' findings on Set-up's check (`POST /api/sites/{id}/validate`), each a test that fails
without its fix.

- The reserve's floor is the floor the plan holds: never above the battery's highest state of charge (plans.grid_floor, the plan's own soc_floor_grid),
  and a floor typed above it is said in the site's warnings.
- The reserve reads the site as it would be, the same site the warnings read: a section with a problem counts as stored (a typed inverter that fails
  leaves the stored one), and only a problem in the reserve's own fields (the critical load, the reserve policy) holds back its figures.
- A negative hour of the critical load is refused, by the save and by the check (plan C7.3: "0 or more; 24 values").
- The check agrees with the save for the reader's role: a change of a control limit or a money setting that the reader's save would refuse is a
  problem on its field, in the save's own words.
"""
from __future__ import annotations

from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import plans, sites

AH_BATTERY = {"entry_unit": "ah", "capacity_ah": 1000, "nominal_v": 51.2, "p_ch_kw": 50, "p_dis_kw": 50}
CRITICAL = [0.0] * 8 + [60.0] * 10 + [0.0] * 6           # 60 kW from 08:00 to 18:00


def _check(w, body, role="admin", site=None):
    r = w.c[role].post(f"/api/sites/{site or w.site}/validate", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_golden_a_floor_above_the_highest_state_of_charge_is_held_at_it_as_the_plan_holds_it(world):
    """1,000 Ah at 51.2 V (10 % to 95 %), the reserve floor typed at 100 %, the critical load 60 kW. The plan's grid-up floor is min(soc_max, the
    delivered floor) = 95 % (plans.build_problem's soc_floor_grid), so the energy held is (0.95 - 0.10) x 51.2 x 0.95 = 41.344 kWh, 41.3 minutes of 60
    kW; the uncapped figure was 0.90 x 51.2 x 0.95 = 43.78 kWh, 43.8 minutes, energy the battery never holds."""
    body = {"site": {"critical_load_kw": CRITICAL, "reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 100}}, "assets": {"battery": AH_BATTERY}}
    got = _check(world, body)
    res = got["derived"]["reserve"]
    assert (res["floor_pct"], res["floor_set_pct"], res["held_kwh"], res["minutes"]) == (95.0, 100.0, 41.34, 41.3), res
    assert ("The reserve floor (100 %) is above the battery's highest state of charge (95 %): the plan holds the reserve at 95 %"
            in got["setup_warnings"]), got["setup_warnings"]
    # one rule: the plan's own floor is the check's
    site = {"data": {"reserve_policy": {"min_soc_pct": 100}}, "assets": {"battery": {"soc_min": 0.1, "soc_max": 0.95}}}
    assert plans.grid_floor(site) == 0.95
    site["data"]["reserve_policy"]["min_soc_pct"] = 30
    assert plans.grid_floor(site) == 0.30
    # a floor at or under the highest state of charge says nothing
    body["site"]["reserve_policy"]["min_soc_pct"] = 95
    assert not any("highest state of charge" in w for w in _check(world, body)["setup_warnings"])


def test_a_typed_inverter_that_fails_leaves_the_stored_one_for_the_reserve_as_for_the_warnings(world):
    with world.use():
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 30}, "test")
    stored = _check(world, {})
    assert stored["derived"]["reserve"]["battery_kw"] == 30.0
    got = _check(world, {"assets": {"inverter": {"brand": "deye", "kw_ac": -1}}})
    assert [p["field"] for p in got["problems"]] == ["assets.inverter.kw_ac"]
    # the stored inverter's 30 kW, as the warnings in the same answer read it (Mill One's battery gives 100 kW)
    assert "The battery's discharge power (100 kW) is above the inverter's AC size (30 kW): the plan keeps it within 30 kW" in got["setup_warnings"]
    assert got["derived"]["reserve"] == stored["derived"]["reserve"], got["derived"]["reserve"]


def test_only_a_problem_in_the_reserve_s_own_fields_holds_back_its_figures(world):
    # step A's name left empty: a problem on it, but the reserve reads the critical load as typed (Mill One: 200 kWh, 10 % to 95 %, floor 20 %:
    # 0.10 x 200 x 0.95 = 19 kWh, 19 minutes of 60 kW)
    got = _check(world, {"site": {"name": "", "critical_load_kw": [60.0] * 24}})
    assert [p["field"] for p in got["problems"]] == ["site.name"]
    assert "reserve" not in got["not_derived"], got["not_derived"]
    res = got["derived"]["reserve"]
    assert (res["critical_kw"], res["held_kwh"], res["minutes"]) == (60.0, 19.0, 19.0), res
    # the warnings read the same site: a critical load above the battery's discharge power (100 kW) typed beside the empty name
    got = _check(world, {"site": {"name": "", "critical_load_kw": [150.0] * 24}})
    assert got["derived"]["reserve"]["critical_kw"] == 150.0
    assert ("The critical load (150 kW at most) is more than the battery's discharge power (100 kW): it cannot carry it alone in an outage"
            in got["setup_warnings"]), got["setup_warnings"]
    # a problem in the reserve's own fields holds it back, with that field's sentence
    got = _check(world, {"site": {"name": "", "reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 120}}})
    assert got["derived"]["reserve"] is None
    assert got["not_derived"]["reserve"] == "The reserve floor (% state of charge) must be at most 100"


def test_a_negative_hour_of_the_critical_load_is_refused_by_the_save_and_the_check(world):
    hours = [0.0] * 5 + [-3.0] + [0.0] * 18
    r = world.c["admin"].put(f"/api/sites/{world.site}", json={"critical_load_kw": hours})
    assert (r.status_code, r.json()["detail"]) == (422, "The critical load (kW) must be at least 0"), r.text
    with world.use():
        assert sites.full(world.site)["data"]["critical_load_kw"] == [20.0] * 24
    got = _check(world, {"site": {"critical_load_kw": hours}})
    assert got["ok"] is False
    assert got["problems"] == [{"step": "d", "field": "site.critical_load_kw.5", "path": ["site", "critical_load_kw", 5],
                                "message": "The critical load (kW) must be at least 0"}]
    assert got["not_derived"]["reserve"] == "The critical load (kW) must be at least 0"
    # 0 and more is taken
    assert _check(world, {"site": {"critical_load_kw": [0.0] * 23 + [5.0]}})["ok"] is True


LIMITS = "Changing the site's control limits is for an administrator"
SITE_MONEY = "Changing the site's value of lost load or wear costing is for an administrator"
ASSET_MONEY = "Changing the equipment's money settings (prices and wear) is for an administrator"


def test_an_operator_s_check_of_a_change_its_save_refuses_is_not_ok(world):
    op, sid = world.c["operator"], world.site
    # a control limit (the reserve floor): the save refuses it; the check says so on the field, and works out no reserve from it
    r = op.put(f"/api/sites/{sid}", json={"reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 55}})
    assert (r.status_code, r.json()["detail"]) == (403, LIMITS), r.text
    got = _check(world, {"site": {"reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 55}}}, "operator")
    assert got["ok"] is False
    assert got["problems"] == [{"step": "d", "field": "site.reserve_policy", "path": ["site", "reserve_policy"], "message": LIMITS}]
    assert got["derived"]["reserve"] is None and got["not_derived"]["reserve"] == LIMITS
    # the site's value of lost load
    r = op.put(f"/api/sites/{sid}", json={"voll_rs_per_kwh": 4321})
    assert (r.status_code, r.json()["detail"]) == (403, SITE_MONEY), r.text
    got = _check(world, {"site": {"voll_rs_per_kwh": 4321}}, "operator")
    assert [(p["step"], p["field"], p["message"]) for p in got["problems"]] == [("d", "site.voll_rs_per_kwh", SITE_MONEY)]
    # the battery's replacement cost
    battery = {**AH_BATTERY, "replacement_cost_per_kwh": 12345}
    r = op.put(f"/api/sites/{sid}/assets/battery", json=battery)
    assert (r.status_code, r.json()["detail"]) == (403, ASSET_MONEY), r.text
    got = _check(world, {"assets": {"battery": battery}}, "operator")
    assert [(p["step"], p["field"], p["message"]) for p in got["problems"]] == [("b", "assets.battery.replacement_cost_per_kwh", ASSET_MONEY)]
    # an administrator's check of the same is ok, and so is an operator's that sends the limits and money settings as stored
    assert _check(world, {"site": {"reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 55}, "voll_rs_per_kwh": 4321},
                          "assets": {"battery": battery}})["ok"] is True
    got = _check(world, {"site": {"reserve_policy": {"p_threshold": 0.2, "min_soc_pct": 20.0}, "voll_rs_per_kwh": 500,
                                  "critical_load_kw": [20] * 24},
                         "assets": {"battery": {**AH_BATTERY, "replacement_cost_per_kwh": 52000}}}, "operator")
    assert got["ok"] is True, got["problems"]
    # removing a battery whose money settings an administrator set
    with world.use():
        sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100, "replacement_cost_per_kwh": 30000}, "test")
    r = op.delete(f"/api/sites/{sid}/assets/battery")
    words = "Removing equipment whose money settings (prices and wear) an administrator set is for an administrator"
    assert (r.status_code, r.json()["detail"]) == (403, words), r.text
    got = _check(world, {"assets": {"battery": None}}, "operator")
    assert [(p["step"], p["field"], p["message"]) for p in got["problems"]] == [("b", "assets.battery", words)]
    assert _check(world, {"assets": {"battery": None}})["ok"] is True
