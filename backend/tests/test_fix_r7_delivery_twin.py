"""Round-7 regressions for the delivery compile and the vendor tables (audit package delivery-twin; plan Part 7.1 steps 4 and 5, DLV-02, R6 3.1 to
3.4, R8 2.15): the table to key in carries each slot's charge target, each brand's table is in the vendor's own layout (its rows exactly the
grammar's fields, the values keyed once beside them, and a sentence for what the brand cannot hold as planned), and the MDI cap is carried only where
something on site holds it. Each test fails on the code before its fix. Test data: synthetic compiled days and plan steps; nothing reads the network."""
from __future__ import annotations

from test_fix_api import world  # noqa: F401 (a fixture: a company of its own with a site and a client per role)

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
