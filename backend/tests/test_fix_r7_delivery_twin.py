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
