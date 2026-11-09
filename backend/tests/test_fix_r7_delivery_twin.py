"""Round-7 regressions for the delivery compile and the vendor tables (audit package delivery-twin; plan Part 7.1 steps 4 and 5, DLV-02, R6 3.1 to
3.4, R8 2.15): the table to key in carries each slot's charge target, each brand's table is in the vendor's own layout (its rows exactly the
grammar's fields, the values keyed once beside them, and a sentence for what the brand cannot hold as planned), and the MDI cap is carried only where
something on site holds it. Each test fails on the code before its fix. Test data: synthetic compiled days and plan steps; nothing reads the network."""
from __future__ import annotations

from test_fix_api import world  # noqa: F401 (a fixture: a company of its own with a site and a client per role)
from test_fix_optimiser import company  # noqa: F401 (a fixture: a company file of its own)

from gridlytics import delivery


def _slot(start: str, end: str, mode: str, floor: float, target: float, dis: int | None = None, ch: int | None = None, k: int = 0) -> dict:
    """A compiled slot as compile_mode_a writes it (the times of a day at epoch 0 + k quarters: only the clock strings matter to a vendor table)."""
    return {"start": start, "end": end, "start_ts": k, "end_ts": k + 1, "mode": mode, "soc_floor_pct": floor, "soc_target_pct": target,
            "max_discharge_w": dis, "max_charge_w": ch, "grid_charge": mode == "charge_grid", "grid_cap_w": None, "gen_charge": False,
            "export_allowed": mode == "export", "genset": None, "charge_end_ts": None, "charge_end": None, "intent": mode}


#: A busy day: a night grid charge (floor 30, target 80), a hold, a solar charge, a self-use afternoon, the evening discharge and a late grid charge
BUSY = [_slot("00:00", "05:00", "charge_grid", 30, 80, ch=50000), _slot("05:00", "09:00", "hold", 80, 80),
        _slot("09:00", "16:00", "charge_pv_only", 60, 95, ch=40000), _slot("16:00", "18:30", "self_use", 55, 95),
        _slot("18:30", "22:30", "discharge", 25, 95, dis=150000), _slot("22:30", "00:00", "charge_grid", 25, 60, ch=30000)]


def _compiled(brand: str, slots: list[dict] | None = None, **extra) -> dict:
    return {"brand": brand, "grammar": delivery.BRANDS[brand], "days": [{"date": "2026-11-10", "slots": BUSY if slots is None else slots}],
            "fallback": {"on_expiry": "inverter_native", "min_soc_floor_pct": 20.0, "revert_after_hours": 3}, **extra}


def _table(brand: str, slots: list[dict] | None = None, **extra) -> dict:
    return delivery.vendor_table(_compiled(brand, slots, **extra), 0)


def _keyed_soc(row: dict) -> int | None:
    """The state of charge a row keys, under whichever name its brand gives it."""
    return row.get("target_soc", row.get("soc_pct"))


# ------------------------------------------------------------------------------------------------ the charge target (role-operator-02)

def test_a_grid_charge_row_keys_the_slots_target_not_its_floor_at_every_brand_that_keys_a_soc_per_row():
    """A Sungrow forced-charge row read 'SoC % 62' while its intent said 'to 82 %': every brand but Deye and Huawei keyed the slot's floor."""
    for brand in ("sungrow", "other", "deye"):
        t = _table(brand)
        charge_rows = [r for r, ks in zip(t["rows"], t["slots"]) if BUSY[ks[0]]["mode"] == "charge_grid"]
        assert charge_rows, brand
        assert [_keyed_soc(r) for r in charge_rows][:2] == [80, 60], (brand, charge_rows)   # the targets; 30 and 25 (the floors) before
    other = _table("other")["rows"]
    assert [r["soc_pct"] for r in other] == [80, 80, 95, 55, 25, 60]                       # a charge slot keys its target, any other its floor


def test_the_table_says_which_slot_each_row_keys_so_its_intent_can_stand_beside_it():
    t = _table("other")
    assert t["slots"] == [[0], [1], [2], [3], [4], [5]] and t["settings"] == [] and isinstance(t["notes"], list)
    assert delivery.vendor_view(_compiled("other")) == t["rows"]                            # the rows the control page always had
    assert delivery.vendor_table(None) == {"rows": [], "slots": [], "settings": [], "notes": []}


def test_the_control_page_gets_each_rows_slots_the_values_keyed_once_and_the_notes(world, monkeypatch):
    """GET /control answered only `vendor_view`: the page put slot i's intent beside row i, which is wrong as soon as a brand's rows are not one per
    slot (a Huawei hold is a gap, a Deye ring repeats a program), and had nowhere to show a brand's global settings or what it cannot hold."""
    from gridlytics import plans
    comp = _compiled("other")
    monkeypatch.setattr(plans, "current", lambda site_id, *a, **k: {"id": "p1", "slots": comp, "steps": [], "summary": {}})
    body = world.c["operator"].get(f"/api/sites/{world.site}/control").json()
    t = delivery.vendor_table(comp, 0)
    assert body["vendor_view"] == [t["rows"]] and body["vendor_slots"] == [t["slots"]]
    assert body["vendor_settings"] == [t["settings"]] and body["vendor_notes"] == [t["notes"]]


# ------------------------------------------------------------------------------------------------ the MDI cap where nothing holds it (pages-B-03)

T0 = 1794000000 // 86400 * 86400 - 5 * 3600       # a Karachi midnight


def _peak_day(e_nom: float = 2000.0) -> list[dict]:
    """A 400 kW load whose high quantile is 500 kW in the 18:00-22:00 peak (420 kW elsewhere), no PV: hold at 90 % to 18:00, discharge at 150 kW to
    22:00, then hold; the outage reserve 20 %."""
    out, soc = [], 90.0
    for i in range(96):
        peak = 72 <= i < 88
        dis = 150.0 if peak else 0.0
        st = {"t": T0 + 900 * i, "charge_kw": 0.0, "discharge_kw": dis, "grid_import_kw": 400.0 - dis, "grid_export_kw": 0.0, "load_kw": 400.0,
              "load_hi_kw": 500.0 if peak else 420.0, "pv_kw": 0.0, "pv_forecast_kw": 0.0, "pv_lo_kw": 0.0, "critical_kw": 100.0, "soc_start_pct": soc,
              "reserve_pct": 20.0, "genset_on": False, "reason": "discharge_peak_window" if peak else "hold_for_outage", "grid_present": 1.0,
              "grid_charge_permitted": True}
        soc -= dis * 0.25 / 0.95 / e_nom * 100
        st["soc_pct"] = round(soc, 2)
        out.append(st)
    return out


BAT = {"E_nom": 2000.0, "E0": 1800.0, "P_ch": 300.0, "P_dis": 300.0, "eta_c": 0.95, "eta_d": 0.95, "soc_min": 0.1, "soc_max": 0.95}


def _mdi_site(tier: str = "advisory_writes") -> dict:
    return {"tier": tier, "assets": {"battery": {"nameplate_kwh": 2000, "soc_min": 0.1, "soc_max": 0.95, "p_ch_kw": 300, "p_dis_kw": 300, "eta_c": 0.95,
                                                 "eta_d": 0.95}},
            "connections": [{"data": {"regime": "net_billing", "sanctioned_load_kw": 800, "export_cap_kw": 0, "mdi_interval_min": 30}}], "data": {}}


def test_a_slot_only_brand_keyed_by_a_person_gets_the_mdi_cap_as_discharge_room_and_floor_not_as_a_cap_it_cannot_hold():
    """Every slot carried grid_cap_w and the twin peak-shaved to it, though a Deye, Growatt, Sungrow, Solis or Voltronic table keyed by a person has
    no such setting: the guard, the compare drawer, the demo twin and the fidelity replay all credited a peak cut the device cannot make."""
    deye = delivery.compile_mode_a(_peak_day(), "deye", BAT, 20.0, 10.0, 300.0, site=_mdi_site())
    assert deye["mdi"]["cap_kw"] == 300.0 and deye["mdi"]["held"] is False
    slots = deye["days"][0]["slots"]
    assert all(s["grid_cap_w"] is None for s in slots)                                  # nothing on site holds it, so no replay counts on it
    dis = next(s for s in slots if s["mode"] == "discharge")
    assert dis["max_discharge_w"] == 200_000 and dis["mdi_shave_kw"] == 200.0          # 500 - 300, above the plan's 150 kW (Deye keys it as its power)
    assert dis["soc_floor_pct"] == 47.9 and dis["intent"].endswith("not below 48 %")  # 42.1 points of shaving from 90 %; the plan's own floor was 58.4
    hold = next(s for s in slots if s["mode"] == "hold")
    assert "mdi_shave_kw" not in hold and hold["max_discharge_w"] is None              # a hold cannot discharge on a slot-only device
    shallow = delivery.compile_mode_a(_peak_day(1000.0), "deye", {**BAT, "E_nom": 1000.0, "E0": 900.0}, 20.0, 10.0, 300.0, site=_mdi_site())
    assert next(s for s in shallow["days"][0]["slots"] if s["mode"] == "discharge")["soc_floor_pct"] == 20.0   # 84 points needed: the reserve wins
    held_back = _peak_day()
    for st in held_back:
        st["reserve_pct"] = 70.0                                                         # an outage reserve above the plan's own floor
    kept = delivery.compile_mode_a(held_back, "deye", BAT, 20.0, 10.0, 300.0, site=_mdi_site())
    assert next(s for s in kept["days"][0]["slots"] if s["mode"] == "discharge")["soc_floor_pct"] == 58.4    # never raised above the plan's floor


def test_the_cap_stays_on_the_slots_where_the_brand_or_an_edge_holds_it_and_where_no_site_is_given():
    steps = _peak_day()
    for comp in (delivery.compile_mode_a(steps, "huawei", BAT, 20.0, 10.0, 300.0, site=_mdi_site()),                 # capacity control holds it
                 delivery.compile_mode_a(steps, "deye", BAT, 20.0, 10.0, 300.0, site=_mdi_site("closed_loop")),       # an edge holds it
                 delivery.compile_mode_a(steps, "deye", BAT, 20.0, 10.0, 300.0)):                                    # no site: as before
        assert comp["mdi"]["held"] is True and comp["mdi"]["not_enforceable"] is None
        assert all(s["grid_cap_w"] for d in comp["days"] for s in d["slots"]) and all("mdi_shave_kw" not in s for d in comp["days"] for s in d["slots"])
    assert delivery.cap_held("huawei", "monitor") and delivery.cap_held("sungrow", "closed_loop")
    assert not delivery.cap_held("deye", "advisory_writes") and not delivery.cap_held("growatt", "monitor")


def test_a_replay_of_a_table_nothing_holds_the_cap_for_does_not_peak_shave():
    import numpy as np

    from gridlytics import twin
    steps = _peak_day()
    slots = [s for d in delivery.compile_mode_a(steps, "deye", BAT, 20.0, 10.0, 300.0, site=_mdi_site())["days"] for s in d["slots"]]
    ts = np.arange(steps[0]["t"], steps[-1]["t"] + 900, 60)
    assert np.isinf(twin.slots_policy(slots, ts)["grid_cap"]).all()


def test_the_compiled_table_reports_the_demand_a_keyed_table_reaches_above_a_cap_nothing_holds():
    """Part 7.1 step 4: 'the expected MDI rise is reported on the plan as mdi_not_enforceable, so the savings page never credits a peak cut the device
    could not make'. Nothing reported it."""
    deye = delivery.compile_mode_a(_peak_day(), "deye", BAT, 20.0, 10.0, 300.0, site=_mdi_site())
    # the holds around the peak cannot discharge: 400 kW of load and the plan's 20 kW high-load margin, against a 300 kW cap (the peak is shaved)
    assert deye["mdi"]["not_enforceable"] == {"cap_kw": 300.0, "keyed_peak_kw": 420.0, "rise_kw": 120.0}
    notes = delivery.vendor_table(deye, 0)["notes"]
    assert any("420 kW" in n and "300 kW" in n and "demand" in n for n in notes), notes
    assert not any("Rs" in n for n in notes)


def test_a_tick_compiles_for_its_site_so_the_cap_is_held_only_where_something_holds_it(company):
    from test_fix_optimiser import _site

    from gridlytics import plans
    sid = _site()
    r = plans.tick(sid, steps=96)
    assert r["status"] == "active"
    mdi = r["slots"]["mdi"]
    assert mdi["held"] is False                                                         # a Deye at the monitor tier: a person would key it
    assert mdi["cap_kw"] == r["summary"]["mdi_cap_kw"]
    assert all(s["grid_cap_w"] is None for d in r["slots"]["days"] for s in d["slots"])


# ------------------------------------------------------------------------------------------------ Huawei's periods (pages-B-04, engine-delivery-05)

def test_huaweis_periods_are_charge_or_discharge_every_day_with_a_hold_as_a_gap_and_the_cut_offs_set_once():
    """The Huawei rows carried 'hold' (no Huawei period type), a last period ending 00:00 (Huawei refuses it), no days, and none of the global
    cut-off, floor and capacity-control values Part 7.1 step 4 keys instead of per-slot targets."""
    t = _table("huawei", mdi={"cap_kw": 300.0, "held": True, "not_enforceable": None})
    assert all(list(r) == delivery.BRANDS["huawei"]["fields"] for r in t["rows"])
    assert [(r["start"], r["end"], r["charge_or_discharge"]) for r in t["rows"]] == [("00:00", "05:00", "charge"), ("16:00", "22:30", "discharge"),
                                                                                      ("22:30", "23:59", "charge")]
    assert t["slots"] == [[0], [3, 4], [5]] and {r["days"] for r in t["rows"]} == {"Every day"} and [r["period"] for r in t["rows"]] == [1, 2, 3]
    s = {x["key"]: x["value"] for x in t["settings"]}
    assert (s["grid_charge_cutoff_soc"], s["discharge_cutoff_soc"], s["backup_soc"], s["peak_shaving_power_w"], s["charge_from_grid"]) == (80, 25, 20,
                                                                                                                                         300000, True)
    assert any(n.startswith("No period from 05:00 to 09:00 and 09:00 to 16:00") for n in t["notes"]), t["notes"]
    assert any("one discharge cut-off for the day (25 %): the discharge from 16:00 to 18:30" in n for n in t["notes"]), t["notes"]
    assert any("one grid-charge cut-off for the day (80 %): the charge from 22:30 to 23:59" in n for n in t["notes"]), t["notes"]


def test_a_whole_day_huawei_period_ends_at_23_59_and_a_day_of_holds_has_no_period():
    t = _table("huawei", [_slot("00:00", "00:00", "self_use", 30, 90)])
    assert [(r["start"], r["end"], r["charge_or_discharge"]) for r in t["rows"]] == [("00:00", "23:59", "discharge")]
    assert "peak_shaving_power_w" not in {x["key"] for x in t["settings"]}                    # no MDI cap on the plan
    held = _table("huawei", [_slot("00:00", "00:00", "hold", 60, 60)])
    assert held["rows"] == [] and {x["key"]: x["value"] for x in held["settings"]}["charge_from_grid"] is False


# ------------------------------------------------------------------------------------------------ the other brands' layouts (pages-B-05, engine-delivery-06,
# tracker-and-docs-06, flow-onboarding-23)

def test_every_brands_rows_are_exactly_its_grammars_fields():
    """Growatt, Sungrow, Voltronic, Solis and 'other' all got {slot, start, end, mode, soc_pct}, while the grammar (and the web contract: columns =
    grammar.fields) said ['start', 'end', 'enabled'], ['start', 'end', 'target_soc'], ['mode'], ['start', 'end', 'current_a'], ['start', 'mode',
    'soc_pct']."""
    for brand, g in delivery.BRANDS.items():
        t = _table(brand, mdi={"cap_kw": 300.0, "held": brand == "huawei", "not_enforceable": None})
        assert t["rows"] and len(t["rows"]) == len(t["slots"]), brand
        assert all(list(r) == g["fields"] for r in t["rows"]), (brand, t["rows"][0], g["fields"])


def test_sungrow_keys_its_forced_charge_windows_with_their_targets_and_says_what_self_consumption_cannot_hold():
    t = _table("sungrow", limits={"max_charge_w": 300000, "max_discharge_w": 300000, "battery_v": None})
    assert t["rows"] == [{"window": 1, "start": "00:00", "end": "05:00", "target_soc": 80}, {"window": 2, "start": "22:30", "end": "23:59", "target_soc": 60}]
    assert t["slots"] == [[0], [5]]
    s = {x["key"]: x["value"] for x in t["settings"]}
    assert (s["ems_mode"], s["forced_charging"], s["min_soc"], s["max_soc"], s["reserved_soc_backup"]) == ("Self-consumption", "Enabled, every day", 25,
                                                                                                         95, 20)
    assert (s["max_charging_power_w"], s["max_discharging_power_w"]) == (50000, 300000)
    assert any("cannot keep the battery from discharging" in n and "05:00 to 09:00" in n for n in t["notes"]), t["notes"]
    assert any("one Min SoC (25 %)" in n and "16:00 to 18:30" in n for n in t["notes"]), t["notes"]
    three = _table("sungrow", [_slot("00:00", "04:00", "charge_grid", 30, 70, ch=1), _slot("04:00", "08:00", "discharge", 40, 70, dis=1),
                               _slot("08:00", "12:00", "charge_grid", 40, 80, ch=1), _slot("12:00", "16:00", "discharge", 40, 80, dis=1),
                               _slot("16:00", "00:00", "charge_grid", 40, 90, ch=1)])
    assert len(three["rows"]) == 2 and any("two forced-charge windows" in n and "16:00 to 23:59" in n for n in three["notes"])


def test_growatt_has_three_battery_first_and_three_grid_first_windows_with_load_first_between_them():
    """BRANDS said 'three charge and three discharge windows' and 'up to 3 slots' at once (the onboarding card and the Slots view printed both):
    the plan's 3 plus 3 (Part 7.1 step 2), with self-use as Load First between windows, which takes no window."""
    g = delivery.BRANDS["growatt"]
    assert g["max_slots"] == 6 and g["layout"].startswith("three Battery First and three Grid First windows")
    t = _table("growatt")
    assert [(r["priority"], r["window"], r["start"], r["end"], r["enabled"]) for r in t["rows"]] == [("battery_first", 1, "00:00", "16:00", True),
                                                                                                      ("battery_first", 2, "22:30", "23:59", True)]
    assert t["slots"] == [[0, 1, 2], [5]]
    s = {x["key"]: x["value"] for x in t["settings"]}
    assert (s["charge_from_grid"], s["battery_first_stop_soc"], s["grid_first_min_soc"], s["load_first_stop_soc"]) == (True, 95, 20, 25)
    assert any("charges from the grid too" in n for n in t["notes"]), t["notes"]
    exp = _table("growatt", [_slot("00:00", "18:00", "self_use", 30, 90), _slot("18:00", "21:00", "export", 40, 90, dis=1),
                             _slot("21:00", "00:00", "self_use", 40, 90)])
    assert [(r["priority"], r["window"]) for r in exp["rows"]] == [("grid_first", 1)]
    assert {x["key"]: x["value"] for x in exp["settings"]}["grid_first_min_soc"] == 40


def test_the_compiler_fits_a_growatt_day_into_three_windows_of_each_priority():
    """With six slots and Load First taking no window, a compiled Growatt day never needs a fourth window of one priority."""
    import itertools
    for modes in itertools.product(("charge_grid", "hold", "discharge", "export"), repeat=6):
        slots = [_slot(f"{2 * k:02d}:00", f"{2 * k + 2:02d}:00", m, 40, 60) for k, m in enumerate(modes)]
        if any(a == b for a, b in zip(modes, modes[1:])):
            continue                                                                 # the compiler merges equal neighbours
        t = _table("growatt", slots)
        for pri in ("battery_first", "grid_first"):
            assert sum(r["priority"] == pri for r in t["rows"]) <= 3, modes
        assert not any("cannot be keyed" in n for n in t["notes"]), modes


def test_voltronic_keys_its_source_priorities_at_each_change_and_solis_its_charge_and_discharge_slots():
    v = _table("voltronic")
    assert [(r["change"], r["at"], r["output_priority"], r["charger_priority"]) for r in v["rows"]] == [
        (1, "00:00", "SUB", "SNU"), (2, "05:00", "SUB", "OSO"), (3, "16:00", "SBU", "OSO"), (4, "22:30", "SUB", "SNU")]
    assert {x["key"]: x["value"] for x in v["settings"]}["back_to_utility_soc"] == 25
    assert any("no time table" in n for n in v["notes"])
    s = _table("solis")
    assert [(r["kind"], r["slot"], r["start"], r["end"], r["power_w"], r["current_a"]) for r in s["rows"]] == [
        ("charge", 1, "00:00", "05:00", 50000, None), ("discharge", 1, "18:30", "22:30", 150000, None), ("charge", 2, "22:30", "23:59", 30000, None)]
    assert any("nominal voltage" in n for n in s["notes"]) and any("05:00 to 09:00" in n and "self-use" in n for n in s["notes"])
    with_v = _table("solis", limits={"max_charge_w": None, "max_discharge_w": None, "battery_v": 500.0})
    assert [r["current_a"] for r in with_v["rows"]] == [100, 300, 60] and not any("nominal voltage" in n for n in with_v["notes"])


def test_the_compiled_table_carries_the_batterys_limits_for_the_brands_that_key_them_once():
    from test_fix_r3_optimiser import _steps_for
    comp = delivery.compile_mode_a(_steps_for([("idle", 0.0, 0.0)] * 96), "sungrow", {"E_nom": 1000.0, "P_ch": 250.0, "P_dis": 300.0}, 20.0)
    assert comp["limits"] == {"max_charge_w": 250000, "max_discharge_w": 300000, "battery_v": None}
    assert delivery.compile_mode_a(_steps_for([("idle", 0.0, 0.0)] * 96), "sungrow", None, 20.0)["limits"] == {
        "max_charge_w": None, "max_discharge_w": None, "battery_v": None}


# ------------------------------------------------------------------------------------------------ Deye's ring of six programs (engine-delivery-07,
# flow-telemetry-plan-control-18)

LIMITS = {"max_charge_w": 300000, "max_discharge_w": 300000, "battery_v": None}


def test_deye_keys_all_six_programs_of_its_ring_repeating_a_slots_settings_where_the_plan_needs_fewer():
    """A day of one slot gave one program ('1 of 6'): whatever programs 2 to 6 already held kept running in the ring."""
    t = _table("deye", [_slot("00:00", "00:00", "hold", 60, 60)], limits=LIMITS)
    times = [r["time"] for r in t["rows"]]
    assert [r["program"] for r in t["rows"]] == [1, 2, 3, 4, 5, 6] and times == sorted(set(times)) and times[0] == "00:00"
    assert {(r["power_w"], r["soc_pct"], r["grid_charge"], r["sell"]) for r in t["rows"]} == {(0, 60, False, False)} and t["slots"] == [[0]] * 6
    assert any("six programs" in n and "ring" in n for n in t["notes"]), t["notes"]
    three = _table("deye", [_slot("12:15", "18:00", "charge_grid", 30, 80, ch=50000), _slot("18:00", "22:00", "discharge", 30, 80, dis=100000),
                            _slot("22:00", "00:00", "hold", 30, 30)], limits=LIMITS)
    # the longest stretch is split in two on a quarter-hour mark until there are six
    assert [(r["time"], r["power_w"], r["soc_pct"]) for r in three["rows"]] == [("12:15", 50000, 80), ("15:00", 50000, 80), ("16:30", 50000, 80),
                                                                                ("18:00", 100000, 30), ("20:00", 100000, 30), ("22:00", 0, 30)]
    assert three["slots"] == [[0], [0], [0], [1], [1], [2]]
    six = _table("deye", limits=LIMITS)
    assert len(six["rows"]) == 6 and six["slots"] == [[k] for k in range(6)] and not any("ring" in n for n in six["notes"])


def test_deye_keys_self_use_at_the_batterys_power_a_hold_at_none_and_an_export_with_its_sell_flag():
    """Self-use was keyed as power 0, which on Deye is no discharge at all (a hold), while the twin replays it at the battery's power; an export slot
    had no sell flag, so a keyed Deye never exported."""
    slots = BUSY[:4] + [_slot("18:30", "22:30", "export", 25, 95, dis=150000), BUSY[5]]
    rows = _table("deye", slots, limits=LIMITS)["rows"]
    assert [(r["power_w"], r["soc_pct"], r["sell"]) for r in rows] == [(50000, 80, False), (0, 80, False), (40000, 95, False), (300000, 55, False),
                                                                       (150000, 25, True), (30000, 60, False)]
    old = _table("deye", BUSY)                                                               # a table compiled before its limits were carried
    assert old["rows"][3]["power_w"] is None and any("rated discharge power" in n for n in old["notes"])


# ------------------------------------------------------------------------------------------------ batch 2: the slot-count reduction (engine-delivery-04)

def _day_of(runs: list[tuple[str, int, float, float]], soc0: float = 50.0, e_nom: float = 1000.0) -> list[dict]:
    """Plan steps for one local day from (reason, quarters, charge kW, discharge kW) runs at a 400 kW load with no PV, the state of charge following
    the flows (a 1 MWh battery)."""
    from test_fix_r3_optimiser import _steps_for
    day = [(reason, ch, dis) for reason, n, ch, dis in runs for _ in range(n)]
    assert len(day) == 96
    steps, soc = _steps_for(day), soc0
    for st in steps:
        st["soc_start_pct"] = round(soc, 2)
        soc += (st["charge_kw"] * 0.95 - st["discharge_kw"] / 0.95) * 0.25 / e_nom * 100
        st["soc_pct"] = round(soc, 2)
    return steps


def _shape(comp: dict, day: int = 0) -> list[tuple[str, str, str]]:
    return [(s["start"], s["end"], s["mode"]) for s in comp["days"][day]["slots"]]


SMALL = {"E_nom": 1000.0, "soc_min": 0.1, "soc_max": 0.95}


def test_the_slot_count_reduction_merges_equal_neighbours_again_so_no_planned_charge_is_dropped():
    """The audit's probe day: grid charge 00-02 at 10 kW, discharge 02-06 at 5 kW, grid charge 06-07 at 5 kW, discharge 07-24 at 10 kW. Sungrow's two
    slots came out as one discharge split in two, [00:00-06:00 discharge, 06:00-00:00 discharge]: the planned grid charge was gone. Voltronic's three
    wasted one on two adjacent discharge slots."""
    steps = _day_of([("charge_from_grid", 8, 10.0, 0.0), ("discharge_peak_window", 16, 0.0, 5.0), ("charge_from_grid", 4, 5.0, 0.0),
                     ("discharge_peak_window", 68, 0.0, 10.0)], soc0=60.0)
    assert _shape(delivery.compile_mode_a(steps, "sungrow", SMALL, 20.0)) == [("00:00", "02:00", "charge_grid"), ("02:00", "00:00", "discharge")]
    assert _shape(delivery.compile_mode_a(steps, "voltronic", SMALL, 20.0)) == [("00:00", "02:00", "charge_grid"), ("02:00", "00:00", "discharge")]
    for brand, g in delivery.BRANDS.items():
        modes = [s["mode"] for s in delivery.compile_mode_a(steps, brand, SMALL, 20.0)["days"][0]["slots"]]
        assert len(modes) <= g["max_slots"] and all(a != b for a, b in zip(modes, modes[1:])), (brand, modes)   # never two equal neighbours


def test_a_two_slot_day_keeps_the_evening_discharge_after_its_charges_merge():
    """Sungrow compiled [00:00-10:00 charge_grid, 10:00-00:00 charge_grid] for a plan that discharges 100 kW at 19:00-20:00: the two charge runs were
    never merged, so the discharge was the run to drop."""
    steps = _day_of([("charge_from_grid", 24, 100.0, 0.0), ("hold_for_outage", 16, 0.0, 0.0), ("charge_from_grid", 16, 50.0, 0.0),
                     ("hold_for_outage", 20, 0.0, 0.0), ("discharge_peak_window", 4, 0.0, 100.0), ("hold_for_outage", 16, 0.0, 0.0)], soc0=20.0)
    comp = delivery.compile_mode_a(steps, "sungrow", SMALL, 20.0)
    assert _shape(comp) == [("00:00", "19:00", "charge_grid"), ("19:00", "00:00", "discharge")]
    assert comp["days"][0]["slots"][1]["max_discharge_w"] == 100_000


def test_a_short_planned_discharge_is_widened_to_the_brands_minimum_not_folded_into_a_hold():
    """Faisalabad's Deye table read '07:15-00:00 Hold' while Overview announced the 17:00 MDI discharge and the 18:00 peak discharge: each was a
    single quarter, shorter than Deye's 30-minute slot, and folded into the hold beside it. A short run that moves energy now takes a quarter from
    a neighbour that moves none and keeps its planned start."""
    steps = _day_of([("hold_for_outage", 68, 0.0, 0.0), ("discharge_mdi", 1, 0.0, 40.0), ("hold_for_outage", 3, 0.0, 0.0),
                     ("discharge_peak_window", 1, 0.0, 60.0), ("hold_for_outage", 23, 0.0, 0.0)], soc0=90.0)
    comp = delivery.compile_mode_a(steps, "deye", SMALL, 20.0)
    assert _shape(comp) == [("00:00", "17:00", "hold"), ("17:00", "17:30", "discharge"), ("17:30", "18:00", "hold"),
                            ("18:00", "18:30", "discharge"), ("18:30", "00:00", "hold")]
    dis = [s for s in comp["days"][0]["slots"] if s["mode"] == "discharge"]
    assert [s["max_discharge_w"] for s in dis] == [40_000, 60_000]
    assert [s["soc_floor_pct"] for s in dis] == [round(steps[68]["soc_pct"], 1), round(steps[72]["soc_pct"], 1)]   # where the plan's discharge stops
    # a run that moves no energy still folds into a neighbour, and a short run with no neighbour long enough to give a quarter still folds
    steps = _day_of([("hold_for_outage", 95, 0.0, 0.0), ("discharge_peak_window", 1, 0.0, 60.0)], soc0=90.0)
    assert _shape(delivery.compile_mode_a(steps, "deye", SMALL, 20.0)) == [("00:00", "23:30", "hold"), ("23:30", "00:00", "discharge")]
    steps = _day_of([("idle", 1, 0.0, 0.0), ("discharge_peak_window", 95, 0.0, 5.0)], soc0=90.0)
    assert _shape(delivery.compile_mode_a(steps, "deye", SMALL, 20.0)) == [("00:00", "00:00", "discharge")]


# ------------------------------------------------------------------------------------------------ the plan's genset runs (flow-telemetry-plan-control-04)

def _outage_day(gen_from: int = 72, gen_to: int = 80) -> list[dict]:
    """A grid site's day with a forecast outage 18:00-22:00 (quarters 72-88) in which the plan runs the genset 18:00-20:00 only, a 40 kW load (20 kW
    critical), no PV, the battery holding at 80 % outside the outage."""
    from test_fix_r3_optimiser import _steps_for
    steps = _steps_for([("hold_for_outage", 0.0, 0.0)] * 96)
    for i, st in enumerate(steps):
        down = 72 <= i < 88
        st.update(grid_present=0.0 if down else 1.0, grid_import_kw=0.0 if down else 40.0, load_kw=40.0, critical_kw=20.0, pv_forecast_kw=0.0,
                  soc_start_pct=80.0, soc_pct=80.0, genset_on=gen_from <= i < gen_to, c_imp=40.0, c_exp=0.0, peak=False)
    return steps


def test_a_slot_carries_the_plans_genset_runs_and_the_replay_runs_the_set_only_in_them():
    """The compiled slot's genset flag was any(genset_on) over its steps, so a replay of the slots (the guard, the compare drawer, the demo and the
    fidelity check) ran the set through the whole slot: the off-grid farm's Voltronic day was one self-use slot, and its genset ran all day."""
    import numpy as np

    from gridlytics import twin
    steps = _outage_day()
    comp = delivery.compile_mode_a(steps, "voltronic", SMALL, 20.0)
    slots = [s for d in comp["days"] for s in d["slots"]]
    run = [s for s in slots if s["genset"]]
    assert len(run) == 1 and run[0]["start_ts"] < steps[72]["t"] + 1 and run[0]["end_ts"] > steps[80]["t"]   # the slot is wider than the run
    assert run[0]["genset_runs"] == [[steps[72]["t"], steps[80]["t"]]]
    assert all(s["genset_runs"] == [] for s in slots if not s["genset"])
    ts = np.arange(steps[0]["t"], steps[-1]["t"] + 900, 60)
    gen = twin.slots_policy(slots, ts)["genset"]
    in_run = (ts >= steps[72]["t"]) & (ts < steps[80]["t"])
    assert (gen[in_run] == 1).all() and (gen[~in_run] == 0).all()        # 1 for the whole slot before; 0 is "no planned run" (a rescue still runs)
    older = [{k: v for k, v in s.items() if k != "genset_runs"} for s in slots]
    assert (twin.slots_policy(older, ts)["genset"][(ts >= run[0]["start_ts"]) & (ts < run[0]["end_ts"])] == 1).all()   # a stored plan as it was


def test_compare_with_native_burns_the_plans_genset_hours_not_the_whole_slots(world, monkeypatch):
    """The farm's drawer said diesel native 235 L against plan 283 L while the plan's own figure was 218 L: the plan arm ran the set wherever a slot
    had any run. Here the plan runs the 150 kW set for two hours of a four-hour outage (about 33 L at its 45 kW minimum load, less once the battery
    is full)."""
    from gridlytics import plans
    steps = _outage_day()
    comp = delivery.compile_mode_a(steps, "voltronic", SMALL, 20.0)
    monkeypatch.setattr(plans, "current", lambda site_id, *a, **k: {"id": "p1", "slots": comp, "steps": steps, "summary": {}})
    arms = world.c["admin"].get(f"/api/sites/{world.site}/plan/compare").json()["compare"]
    assert 28.0 <= arms["plan"]["litres"] <= 36.0, arms["plan"]           # 50 L before: the set ran through the whole outage

