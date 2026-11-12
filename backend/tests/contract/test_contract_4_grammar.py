"""Contract test 4 (EMS_BUILD_PLAN.md Part 7.4, U3 5.8 item 4; tracker DLV-01, DLV-02): Mode A grammar per brand profile.

For every brand of delivery.BRANDS, on a day that asks for every slot mode: the compiled table fits the brand (at most its slots, each at least its
shortest slot, on quarter-hour marks, covering the day without a gap, no floor under the fallback floor); its vendor table has exactly the
grammar's fields and the brand's own caps (Deye's ring of six programs, Huawei's 14 periods none across midnight, Growatt's three windows of each
priority, Sungrow's two forced-charge windows, Solis's three slots of each kind, Voltronic's changes in time order); and the modes the brand runs
where a person keys the table are only those its grammar can express (Sungrow is self-consumption outside its forced-charge windows, Voltronic has
no export, and so on).

DLV-01's acceptance per brand, on a golden arbitrage day (grid charge at night, hold, discharge in the evening peak): the compiled table replayed
through the twin keeps at least 80 % of the plan's saving against the native replay, never costs more than the native table and never takes the
battery below the fallback floor, both as an edge executes it and as a person keys it. A Solis table keyed by a person does not (see its mark).
Round 7, audit tests-and-perf-15."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from contract_kit import PKT
from gridlytics import delivery, twin

T0 = int(datetime(2026, 11, 16, tzinfo=PKT).timestamp())
BATTERY = {"E_nom": 200.0, "soc_min": 0.1, "soc_max": 0.95, "P_ch": 50.0, "P_dis": 50.0}
FLOOR = 20.0
#: what each brand's grammar can run in a slot a person keys (R6 3.1 to 3.4, R8 2.15; delivery.as_keyed's register facts)
BRAND_MODES = {
    "deye": set(twin.MODES), "other": set(twin.MODES),
    "huawei": {"charge_grid", "hold", "discharge", "export", "self_use"},
    "sungrow": {"charge_grid", "self_use"},
    "growatt": {"charge_grid", "charge_pv_only", "export", "self_use"},
    "voltronic": {"charge_grid", "charge_pv_only", "hold", "self_use"},
    "solis": {"charge_grid", "discharge", "export", "self_use"},
}
#: (from hour, to hour, mode) of the day that asks for every mode
EVERY_MODE = [(0, 3, "charge_grid"), (3, 5, "hold"), (5, 7, "self_use"), (7, 9, "discharge"), (9, 12, "charge_pv_only"), (12, 14, "export"),
              (14, 16, "hold"), (16, 18, "charge_grid"), (18, 22, "discharge"), (22, 24, "self_use")]


def _flows(mode: str) -> dict:
    """A step's flows that step_intents reads as `mode` (load 50 kW)."""
    return {"charge_grid": dict(charge_kw=30.0, pv_kw=0.0, grid_import_kw=80.0, reason="charge_from_grid"),
            "charge_pv_only": dict(charge_kw=40.0, pv_kw=120.0, grid_import_kw=0.0, reason="store_surplus"),
            "discharge": dict(discharge_kw=30.0, grid_import_kw=20.0, reason="discharge_peak_window"),
            "export": dict(discharge_kw=40.0, pv_kw=40.0, grid_export_kw=30.0, reason="export_peak"),
            "hold": dict(grid_import_kw=50.0, reason="hold_for_outage"),
            "self_use": dict(grid_import_kw=50.0, reason="self_consume")}[mode]


def _steps(spans: list[tuple[int, int, str]]) -> list[dict]:
    steps, soc = [], 50.0
    for i in range(96):
        mode = next(m for a, b, m in spans if a * 4 <= i < b * 4)
        s = {"t": T0 + 900 * i, "charge_kw": 0.0, "discharge_kw": 0.0, "grid_export_kw": 0.0, "load_kw": 50.0, "pv_kw": 0.0, "grid_present": 1.0,
             "genset_on": False, "grid_import_kw": 0.0, **_flows(mode)}
        s["soc_start_pct"] = soc
        soc = float(np.clip(soc + (s["charge_kw"] * 0.95 - s["discharge_kw"] / 0.95) * 0.25 / BATTERY["E_nom"] * 100, 20.0, 95.0))
        s["soc_pct"] = round(soc, 2)
        steps.append(s)
    return steps


def _site(tier: str) -> dict:
    return {"id": "s", "tier": tier, "data": {}, "native": None, "connections": [{"data": {"regime": "none"}}], "assets": {}}


@pytest.mark.parametrize("brand", list(delivery.BRANDS))
def test_each_brands_table_fits_its_grammar_and_runs_only_the_modes_it_can_express(brand):
    g = delivery.BRANDS[brand]
    steps = _steps(EVERY_MODE)
    assert {i["mode"] for i in delivery.step_intents(steps, BATTERY)} == set(twin.MODES)            # the day asks for every mode
    keyed = delivery.compile_mode_a(steps, brand, BATTERY, FLOOR, 10.0, None, site=_site("advisory_writes"))
    edge = delivery.compile_mode_a(steps, brand, BATTERY, FLOOR, 10.0, None, site=_site("closed_loop"))
    for comp in (keyed, edge):
        (day,) = comp["days"]
        slots = day["slots"]
        assert 1 <= len(slots) <= g["max_slots"], (brand, len(slots))
        assert slots[0]["start_ts"] == T0 and slots[-1]["end_ts"] == T0 + 86400
        assert all(a["end_ts"] == b["start_ts"] for a, b in zip(slots, slots[1:])), brand                  # no gap, no overlap
        assert all(s["start_ts"] % 900 == 0 and s["end_ts"] - s["start_ts"] >= 60 * g["min_slot_minutes"] for s in slots), brand
        assert all(s["soc_floor_pct"] >= delivery.fallback_floor(FLOOR) for s in slots), brand
        assert comp["fallback"]["min_soc_floor_pct"] == delivery.fallback_floor(FLOOR)
    # an edge runs the slots as compiled; a person keys the brand's own table, which runs only what the brand can express
    assert all("as_keyed" not in s for s in edge["days"][0]["slots"])
    ran = {(s.get("as_keyed") or {}).get("mode", s["mode"]) for s in keyed["days"][0]["slots"]}
    assert ran <= BRAND_MODES[brand], (brand, ran - BRAND_MODES[brand])
    t = delivery.vendor_table(keyed)
    rows = t["rows"]
    assert all(list(r) == g["fields"] for r in rows), (brand, rows)
    # Sungrow keys a window only for a grid charge, and its two slots of this day hold none (the night's charge folds into the day's solar charge)
    assert rows or brand == "sungrow", brand
    count = lambda key, value: sum(1 for r in rows if r[key] == value)  # noqa: E731
    if brand == "deye":
        assert len(rows) == 6 and g["ring"]                                      # all six programs of the ring, every time
    elif brand == "huawei":
        assert len(rows) <= 14 and all(r["end"] == "23:59" or r["start"] < r["end"] for r in rows), rows      # none across midnight
    elif brand == "growatt":
        assert count("priority", "battery_first") <= 3 and count("priority", "grid_first") <= 3, rows
    elif brand == "sungrow":
        assert len(rows) <= 2 and all(r["target_soc"] is not None for r in rows), rows
    elif brand == "solis":
        assert count("kind", "charge") <= 3 and count("kind", "discharge") <= 3, rows
    elif brand == "voltronic":
        assert [r["at"] for r in rows] == sorted(r["at"] for r in rows), rows
    else:
        assert len(rows) <= g["max_slots"]


# ------------------------------------------------------------------------------------------------ DLV-01: the share of the plan's saving kept

def _golden_day() -> tuple[list[dict], np.ndarray]:
    """A 200 kWh battery on a 100 kW load with no PV: grid charge from 00:00 to 04:00 (20 % to 91 %), hold, discharge 18:00 to 22:00 in a Rs 60 peak
    (Rs 30 off-peak), idle after. The price per step."""
    price = np.array([60.0 if 72 <= i < 88 else 30.0 for i in range(96)])
    steps, soc = [], 20.0
    for i in range(96):
        ch, dis = (37.5 if i < 16 else 0.0), (35.0 if 72 <= i < 88 else 0.0)
        start, soc = soc, soc + (ch * 0.25 * 0.95 - dis * 0.25 / 0.95) / BATTERY["E_nom"] * 100
        steps.append({"t": T0 + 900 * i, "charge_kw": ch, "discharge_kw": dis, "grid_export_kw": 0.0, "load_kw": 100.0, "pv_kw": 0.0,
                      "soc_pct": round(soc, 3), "soc_start_pct": round(start, 3), "grid_present": 1.0, "genset_on": False,
                      "reason": "charge_from_grid" if ch else "discharge_peak_window" if dis else "hold_for_outage" if i < 72 else "idle",
                      "grid_import_kw": 100.0 + ch - dis})
    return steps, price


def _priced(sim: dict, price: np.ndarray) -> float:
    """The day's import at the step's price, less the energy left in the battery above where it started valued at the off-peak price (a table that
    ends the day charged has bought energy it has not used yet)."""
    return float((np.repeat(price, 15) * sim["imp"]).sum() / 60) - (sim["soc"][-1] - 0.2) * BATTERY["E_nom"] * 30.0


@pytest.mark.parametrize("brand,executor", [
    pytest.param(b, x, marks=pytest.mark.xfail(strict=True, reason=(
        "A Solis table keyed by a person cannot hold the battery outside its charge and discharge slots (self-use there, delivery.as_keyed), so the "
        "night's grid charge is spent on the morning's off-peak load and the table costs more than the native settings; the compiler does not yet "
        "fold a hold after a grid charge into the charge window for a brand that cannot hold (DLV-01 is [~]: a follow-up for the engine's owner)"))
                 if (b, x) == ("solis", "keyed") else ())
    for b in delivery.BRANDS for x in ("edge", "keyed")])
def test_each_brands_table_keeps_80_percent_of_the_plans_saving_never_costs_more_than_native_and_keeps_the_floor(brand, executor):
    steps, price = _golden_day()
    ts = np.arange(T0, T0 + 86400, 60)
    series = {"pv": np.zeros(len(ts)), "load": np.full(len(ts), 100.0), "crit": np.full(len(ts), 20.0), "grid": np.ones(len(ts)), "ts": ts,
              "temp": np.full(len(ts), 30.0)}
    plant = twin.Plant(e_nom=BATTERY["E_nom"], soc_min=0.1, soc_max=0.95, p_ch=50.0, p_dis=50.0, g_max=1000.0)
    site = _site("advisory_writes" if executor == "keyed" else "closed_loop")
    plan = float(sum(s["grid_import_kw"] * price[i] * 0.25 for i, s in enumerate(steps))) - (steps[-1]["soc_pct"] / 100 - 0.2) * BATTERY["E_nom"] * 30
    native = _priced(twin.simulate(series, twin.native_policy(site, ts), plant, 0.2), price)
    comp = delivery.compile_mode_a(steps, brand, BATTERY, FLOOR, 10.0, None, site=site)
    sim = twin.simulate(series, twin.slots_policy(comp["days"][0]["slots"], ts), plant, 0.2)
    table = _priced(sim, price)
    assert native - plan > 3000                                                  # the plan's saving: about Rs 3,750 on the day
    kept = (native - table) / (native - plan)
    assert table <= native, (brand, executor, table, native)
    assert kept >= 0.8, (brand, executor, round(kept, 3))
    assert sim["soc"].min() >= delivery.fallback_floor(FLOOR) / 100 - 1e-6, (brand, executor, sim["soc"].min())
