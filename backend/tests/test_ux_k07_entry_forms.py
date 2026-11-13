"""UX backend ask K7 (plan H11; C19.2 item 3, A8.2 to A8.4, C5.4, C5.5, C15; step 9b, Set-up B).

The entry form is saved with the asset, so set-up reopens the way it was filled in and Review repeats it: the battery's `entry_unit` ("kwh" or "ah"),
with `capacity_ah` and `nominal_v` in the Ah form, and the genset's `rating_entry` ("kva" or "kw") beside the kVA and power factor it already stores.
The stored canonical figure (nameplate_kwh, rated_kw) is the one the engine reads, and switching the form back and forth never moves it (A8.2: no
drift). A battery or genset saved before reads as the form it was saved in."""
from __future__ import annotations

import json

from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import sites, store

BATTERY = {"p_ch_kw": 50, "p_dis_kw": 50}


def _battery(w, body, role="admin"):
    return w.c[role].put(f"/api/sites/{w.site}/assets/battery", json={**BATTERY, **body})


def _held(w, role):
    with w.use():
        return sites.assets(w.site)[role]


def test_a_battery_given_in_ah_at_a_voltage_is_stored_as_its_kwh_with_the_form_it_was_given_in(world):
    r = _battery(world, {"entry_unit": "ah", "capacity_ah": 1000, "nominal_v": 51.2})
    assert r.status_code == 200, r.text
    b = r.json()["assets"]["battery"]
    # golden: 1,000 Ah x 51.2 V = 51,200 Wh = 51.2 kWh nominal, the figure the engine reads
    assert (b["entry_unit"], b["capacity_ah"], b["nominal_v"], b["nameplate_kwh"]) == ("ah", 1000, 51.2, 51.2)
    assert world.c["admin"].get(f"/api/sites/{world.site}").json()["assets"]["battery"]["entry_unit"] == "ah"
    with world.use():
        assert sites.full(world.site)["assets"]["battery"]["nameplate_kwh"] == 51.2
    # the Ah form with its kWh given as well: kept where it agrees
    r = _battery(world, {"entry_unit": "ah", "capacity_ah": 280, "nominal_v": 51.2, "nameplate_kwh": 14.336})
    assert r.status_code == 200 and r.json()["assets"]["battery"]["nameplate_kwh"] == 14.336, r.text


def test_switching_the_battery_s_form_back_and_forth_never_moves_its_kwh(world):
    assert _battery(world, {"entry_unit": "ah", "capacity_ah": 1000, "nominal_v": 51.2}).status_code == 200
    # the kWh form, sent with the Ah form's figures still in the page's state: the kWh form keeps neither, and the kWh stays as stored
    r = _battery(world, {"entry_unit": "kwh", "nameplate_kwh": 51.2, "capacity_ah": 1000, "nominal_v": 51.2})
    b = r.json()["assets"]["battery"]
    assert (b["entry_unit"], b["capacity_ah"], b["nominal_v"], b["nameplate_kwh"]) == ("kwh", None, None, 51.2), r.text
    for _ in range(3):
        b = _battery(world, {"entry_unit": "ah", "capacity_ah": 1000, "nominal_v": 51.2}).json()["assets"]["battery"]
        assert b["nameplate_kwh"] == 51.2
        b = _battery(world, {"entry_unit": "kwh", "nameplate_kwh": b["nameplate_kwh"]}).json()["assets"]["battery"]
        assert b["nameplate_kwh"] == 51.2 and b["entry_unit"] == "kwh"


def test_the_ah_form_is_refused_without_its_voltage_or_with_a_kwh_it_does_not_give(world):
    r = _battery(world, {"entry_unit": "ah", "capacity_ah": 1000})
    assert (r.status_code, r.json()["detail"]) == (422, "A capacity in Ah needs the battery's nominal voltage (V)"), r.text
    r = _battery(world, {"entry_unit": "ah", "nominal_v": 51.2})
    assert (r.status_code, r.json()["detail"]) == (422, "Give the battery's capacity in Ah"), r.text
    r = _battery(world, {"entry_unit": "ah", "capacity_ah": 1000, "nominal_v": 51.2, "nameplate_kwh": 60})
    assert (r.status_code, r.json()["detail"]) == (422, "A battery's energy is its capacity in Ah times its nominal voltage: 1,000 Ah at 51.2 V is "
                                                        "51.2 kWh"), r.text
    r = _battery(world, {"entry_unit": "ah", "capacity_ah": 1000, "nominal_v": 6})
    assert (r.status_code, r.json()["detail"]) == (422, "The nominal voltage (V) must be at least 12"), r.text
    r = _battery(world, {"entry_unit": "lead", "nameplate_kwh": 50})
    assert r.status_code == 422 and r.json()["detail"].startswith("The capacity's entry form must be one of"), r.text
    # nothing of a refused save is stored: the battery is the one the fixture saved
    assert _held(world, "battery")["nameplate_kwh"] == 200


def test_a_save_that_leaves_the_form_out_is_read_as_the_form_it_was_given_in(world):
    # a save of before (the web app's kWh form): kWh
    b = _battery(world, {"nameplate_kwh": 120}).json()["assets"]["battery"]
    assert (b["entry_unit"], b["capacity_ah"], b["nominal_v"]) == ("kwh", None, None)
    # a capacity in Ah given: the Ah form
    b = _battery(world, {"capacity_ah": 500, "nominal_v": 48}).json()["assets"]["battery"]
    assert (b["entry_unit"], b["nameplate_kwh"]) == ("ah", 24.0)


def test_a_genset_given_in_kva_keeps_its_form_and_its_kw_never_moves_when_the_form_is_switched(world):
    url = f"/api/sites/{world.site}/assets/genset"
    c = world.c["admin"]
    g = c.put(url, json={"rating_entry": "kva", "rated_kva": 500, "power_factor": 0.8}).json()["assets"]["genset"]
    # golden: 500 kVA x 0.80 = 400 kW usable for dispatch
    assert (g["rating_entry"], g["rated_kva"], g["power_factor"], g["rated_kw"]) == ("kva", 500, 0.8, 400.0)
    # the kW form, sent with the kVA still in the page's state: no kVA is kept, the kW stays
    g = c.put(url, json={"rating_entry": "kw", "rated_kw": 400, "rated_kva": 500, "power_factor": 0.8}).json()["assets"]["genset"]
    assert (g["rating_entry"], g["rated_kva"], g["rated_kw"]) == ("kw", None, 400)
    for _ in range(3):
        g = c.put(url, json={"rating_entry": "kva", "rated_kva": 500, "power_factor": 0.8}).json()["assets"]["genset"]
        g = c.put(url, json={"rating_entry": "kw", "rated_kw": g["rated_kw"]}).json()["assets"]["genset"]
        assert g["rated_kw"] == 400
    r = c.put(url, json={"rating_entry": "kva", "power_factor": 0.8})
    assert (r.status_code, r.json()["detail"]) == (422, "Give the genset's prime rating in kVA, or choose a rating in kW"), r.text
    r = c.put(url, json={"rating_entry": "kva", "rated_kva": 500, "power_factor": 0.8, "rated_kw": 500})
    assert (r.status_code, r.json()["detail"]) == (422, "A genset's rating in kW is its kVA times its power factor: 500 kVA at 0.8 is 400 kW"), r.text
    # a save of before: kVA where a kVA is given, else kW
    assert c.put(url, json={"rated_kva": 250, "power_factor": 0.8}).json()["assets"]["genset"]["rating_entry"] == "kva"
    assert c.put(url, json={"rated_kw": 150}).json()["assets"]["genset"]["rating_entry"] == "kw"


def test_equipment_saved_before_reads_as_the_form_it_was_saved_in_and_is_saved_again_without_a_false_conflict(world):
    """A battery or genset stored before K7 holds no form: it reads as the form it was saved in, and a save whose `if_unchanged` is that reading (the
    page sends back what it read) is no conflict; the audit records no change of form."""
    with world.use(), store.connect() as con:
        for role, data in (("battery", {"nameplate_kwh": 200.0, "soh": 1.0, "p_ch_kw": 100.0, "p_dis_kw": 100.0, "eta_c": 0.95, "eta_d": 0.95,
                                        "soc_min": 0.1, "soc_max": 0.95, "replacement_cost_per_kwh": 52000.0, "warranted_cycles": 6000.0,
                                        "warranted_depth": 0.8}),
                           ("genset", {"rated_kw": 400.0, "rated_kva": 500.0, "power_factor": 0.8, "fuel_kind": "hsd", "a": 0.273, "b": 0.033})):
            con.execute("UPDATE assets SET data = ? WHERE site_id = ? AND role = ?", (json.dumps(data), world.site, role))
    read = world.c["operator"].get(f"/api/sites/{world.site}").json()["assets"]
    assert (read["battery"]["entry_unit"], read["battery"]["capacity_ah"], read["battery"]["nominal_v"]) == ("kwh", None, None)
    assert read["genset"]["rating_entry"] == "kva"
    held = {k: v for k, v in read["battery"].items() if k != "id"}
    # the operator's save (edit_sites: the entry form is no money setting), with what it read as if_unchanged
    r = world.c["operator"].put(f"/api/sites/{world.site}/assets/battery",
                                json={**held, "entry_unit": "ah", "capacity_ah": 3906.25, "nominal_v": 51.2, "nameplate_kwh": 200.0,
                                      "if_unchanged": held})
    assert r.status_code == 200, r.text
    assert r.json()["assets"]["battery"]["entry_unit"] == "ah"
    with world.use():
        last = next(a for a in store.list_audit(limit=50) if a["action"] == "saved the battery")
    assert set((last.get("detail") or {}).get("changes") or {}) == {"entry_unit", "capacity_ah", "nominal_v"}, last


def test_only_a_role_that_edits_the_site_saves_an_entry_form(world):
    for role, want in (("admin", 200), ("operator", 200), ("analyst", 403), ("viewer", 403)):
        assert _battery(world, {"entry_unit": "ah", "capacity_ah": 1000, "nominal_v": 51.2}, role).status_code == want, role
