"""Plan delivery (plan Part 7): the Mode A slot compiler per brand grammar, Mode B steps, Ed25519 signing and the ems-edge/1.0 message models.

Brand grammars are data, written clean-room from the permissive sources the plan names (kellerza/sunsynk and wills106 under Apache-2.0, ha-solarman,
mkaiser and mpp-solar under MIT; nothing from the AGPL Huawei library). The compiler merges the 15-minute solution into the device's slot count,
keeps boundaries on 15-minute marks, clamps state of charge to the device's band and never below the fallback floor, and replays the table through the
twin before signing (the wrong-plan guard).
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import numpy as np
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

from . import platform as platformdb
from . import store, twin

PKT = timezone(timedelta(hours=5))
CONTRACT = "ems-edge/1.0"

BRANDS = {
    "deye": {"label": "Deye / Sunsynk", "max_slots": 6, "layout": "six programs (Time of Use)", "ring": True, "min_slot_minutes": 30,
             "fields": ["time", "power_w", "soc_pct", "grid_charge", "gen_charge"], "table_writes_per_day": 3, "mdi_primitive": False},
    "huawei": {"label": "Huawei SUN2000", "max_slots": 14, "layout": "fourteen TOU periods", "ring": False, "min_slot_minutes": 15,
               "fields": ["start", "end", "charge_or_discharge"], "table_writes_per_day": 3, "mdi_primitive": True},
    "growatt": {"label": "Growatt", "max_slots": 3, "layout": "three charge and three discharge windows", "ring": False, "min_slot_minutes": 30,
                "fields": ["start", "end", "enabled"], "table_writes_per_day": 3, "mdi_primitive": False},
    "sungrow": {"label": "Sungrow", "max_slots": 2, "layout": "two forced-charge windows (emulated)", "ring": False, "min_slot_minutes": 30,
                "fields": ["start", "end", "target_soc"], "table_writes_per_day": 4, "mdi_primitive": False},
    "voltronic": {"label": "Voltronic / Axpert", "max_slots": 3, "layout": "emulated mode changes", "ring": False, "min_slot_minutes": 60,
                  "fields": ["mode"], "table_writes_per_day": 3, "mdi_primitive": False},
    "solis": {"label": "Solis", "max_slots": 6, "layout": "three charge and three discharge slots", "ring": False, "min_slot_minutes": 30,
              "fields": ["start", "end", "current_a"], "table_writes_per_day": 3, "mdi_primitive": False},
    "other": {"label": "Generic", "max_slots": 6, "layout": "generic six-slot table", "ring": False, "min_slot_minutes": 30,
              "fields": ["start", "mode", "soc_pct"], "table_writes_per_day": 3, "mdi_primitive": False},
}
FALLBACK_HOURS = 3


def cap_held(brand: str, tier: str | None) -> bool:
    """Whether something on site holds a slot's grid import cap (`grid_cap_w`, the plan's MDI cap): the brand's own peak-shaving primitive (Huawei's
    capacity control) or an edge at the closed-loop tier. A table a person keys into a Deye, Growatt, Sungrow, Solis or Voltronic holds none
    (Part 7.1 step 4, 7.6)."""
    return bool(BRANDS.get(brand, BRANDS["other"])["mdi_primitive"]) or tier == "closed_loop"


# ------------------------------------------------------------------------------------------------ the compiler

def grid_charging(s: dict) -> bool:
    """Whether a step charges the battery from the grid, read from the solved flows (not from the reason, which an operator hold can name):
    the grid is up and importing, and the charge exceeds what PV and the genset leave over after the load. A step of a site whose battery may not
    charge from the grid (`grid_charge_permitted` False on the step, the site's grid_charge_allowed) is never grid charging: the plan charges it
    from the surplus only."""
    if s.get("grid_charge_permitted") is False:
        return False
    if s.get("reason") == "charge_from_grid":
        return True
    if s.get("grid_present", 1.0) < 0.5 or s.get("grid_import_kw", 0.0) <= 0.1:
        return False
    local = max(0.0, s.get("pv_kw", 0.0) + s.get("genset_kw", 0.0) - s.get("load_kw", 0.0))
    return s.get("charge_kw", 0.0) > local + 0.1


def step_intents(steps: list[dict], battery: dict | None) -> list[dict]:
    """Per 15-minute step: the Mode A intent (mode, floor and target) from the solved flows."""
    out = []
    for s in steps:
        soc_end = s.get("soc_pct") or 0.0
        if not battery:
            out.append({"mode": "self_use", "floor": 20.0, "target": 100.0, "grid_charge": False, "gen_charge": False})
            continue
        if s["grid_present"] < 0.5:
            mode = "self_use"
        elif s["discharge_kw"] > 0.1 and s["grid_export_kw"] > 0.1 and s["discharge_kw"] > s["load_kw"] - s["pv_kw"] + 0.1:
            mode = "export"
        elif s["discharge_kw"] > 0.1:
            mode = "discharge"
        elif s["charge_kw"] > 0.1 and grid_charging(s) and battery.get("a_grid", True):
            mode = "charge_grid"
        elif s["charge_kw"] > 0.1:
            mode = "charge_pv_only"
        elif s["reason"] in ("hold_for_outage", "wear_margin_too_small", "terminal_value", "idle", "operator_hold", "genset_off"):
            mode = "hold"
        else:
            mode = "self_use"
        out.append({"mode": mode, "floor": soc_end, "target": soc_end, "grid_charge": mode == "charge_grid", "gen_charge": False})
    return out


#: No brand grammar has a charge-from-genset slot yet ("gen_charge" is always False), so the plan must not count on one: on site the genset input
#: only follows the load. A plan that charged from the genset left the battery near its floor after every run and forced rescue starts
#: (the off-grid demo farm: 42 starts against native's 22). Set True only with a compiled mode and a twin that executes it.
GEN_CHARGE_SUPPORTED = False

#: The slot modes in which the battery may discharge (the compiler never folds a hold or charge run into one of these when it can avoid it)
DISCHARGING = ("discharge", "export", "self_use")


def compile_mode_a(steps: list[dict], brand: str, battery: dict | None, fallback_floor_pct: float, bms_low_pct: float = 10.0,
                   mdi_cap_kw: float | None = None, *, site: dict | None = None) -> dict:
    """Slots per local day: merge equal intents, fold runs shorter than the brand's minimum into a neighbour, then drop the least valuable
    boundaries (the smallest energy moved) until the day fits the brand's slot count.

    `site` (plans.tick passes it) says what the table runs on. A slot carries `grid_cap_w` only where something on site holds it (`cap_held`: the
    brand's peak-shaving primitive, or an edge at the closed-loop tier), so that no twin replay (the guard, the compare drawer, the demo, the fidelity
    check) shaves a peak the device cannot. Where nothing holds the plan's MDI cap, it is translated into what a person can key (`_translate_mdi`),
    and the demand the keyed table reaches is replayed on the plan's forecast and reported as `mdi.not_enforceable` (Part 7.1 step 4). Without a
    site the cap is held, as v1 assumed everywhere."""
    gram = BRANDS.get(brand, BRANDS["other"])
    held = True if site is None else cap_held(brand, site.get("tier"))
    intents = step_intents(steps, battery)
    by_day: dict[str, list[int]] = {}
    for i, s in enumerate(steps):
        by_day.setdefault(datetime.fromtimestamp(s["t"], PKT).date().isoformat(), []).append(i)
    days = []
    lo = max(bms_low_pct + 2, fallback_floor_pct)
    for day, idx in by_day.items():
        runs = []
        for i in idx:
            it = intents[i]
            if runs and runs[-1]["mode"] == it["mode"]:
                r = runs[-1]
                r["end"] = i + 1
                r["floor"] = min(r["floor"], it["floor"])
                r["target"] = max(r["target"], it["target"])
                r["energy"] += abs(steps[i]["discharge_kw"]) + abs(steps[i]["charge_kw"])
            else:
                runs.append({"mode": it["mode"], "start": i, "end": i + 1, "floor": it["floor"], "target": it["target"],
                             "energy": abs(steps[i]["discharge_kw"]) + abs(steps[i]["charge_kw"])})
        min_steps = max(1, gram["min_slot_minutes"] // 15)

        def merge(k: int, into: int):
            a, b = runs[min(k, into)], runs[max(k, into)]
            keep = runs[into]
            merged = {"mode": keep["mode"], "start": a["start"], "end": b["end"], "floor": min(a["floor"], b["floor"]),
                      "target": max(a["target"], b["target"]), "energy": a["energy"] + b["energy"]}
            runs[min(k, into)] = merged
            del runs[max(k, into)]

        def neighbour(k: int) -> int:
            # a run that does not discharge folds into a neighbour that does not discharge where there is one: a hold or idle run moves no energy,
            # so the energy rule alone always folded it into the next discharge run, which then started hours early and emptied the battery
            # off-peak before the peak it was planned for (the 2-slot Sungrow grammar did this on 9 of 11 demo days)
            cands = [j for j in (k - 1, k + 1) if 0 <= j < len(runs)]
            if runs[k]["mode"] not in DISCHARGING:
                cands = [j for j in cands if runs[j]["mode"] not in DISCHARGING] or cands
            if len(cands) == 1:
                return cands[0]
            return k - 1 if runs[k - 1]["energy"] >= runs[k + 1]["energy"] else k + 1

        changed = True
        while changed and len(runs) > 1:
            changed = False
            for k, r in enumerate(runs):
                if r["end"] - r["start"] < min_steps:
                    merge(k, neighbour(k))
                    changed = True
                    break
        k = 1
        while k < len(runs):
            if runs[k]["mode"] == runs[k - 1]["mode"]:
                merge(k, k - 1)
            else:
                k += 1
        while len(runs) > gram["max_slots"]:
            k = min(range(len(runs)), key=lambda j: runs[j]["energy"])
            merge(k, neighbour(k))
        slots = []
        for r in runs:
            s0, s1 = steps[r["start"]], steps[r["end"] - 1]
            mode = r["mode"]
            floor = float(np.clip(r["floor"], lo, 100.0))
            target = float(np.clip(r["target"], lo, 100.0))
            if mode == "hold":
                floor = float(np.clip(s0.get("soc_start_pct", r["floor"]), lo, 100.0))
            span = steps[r["start"]:r["end"]]
            max_dis = max(st["discharge_kw"] for st in span)
            # charge at the slot's mean planned rate (energy kept, no faster than planned), and never import above the plan's MDI cap. A charge
            # slot that absorbed a later hold or idle run (Sungrow's 2 slots) takes that mean only up to its last planned charge step and ends
            # the charge there (HANDOFF 8.A.4): spread over the whole span, the charge ran on to the span's end, into the peak hours the plan
            # kept free before its discharge
            charge_end = None
            if mode.startswith("charge"):
                last = max((j for j, st in enumerate(span) if st["charge_kw"] > 0.1), default=len(span) - 1)
                charge_end = int(span[last]["t"] + 900)
                max_ch = sum(st["charge_kw"] for st in span[:last + 1]) / (last + 1)
            else:
                max_ch = sum(st["charge_kw"] for st in span) / len(span)
            grid_cap = max((st["grid_import_kw"] for st in span), default=0.0)
            slots.append({"start": datetime.fromtimestamp(s0["t"], PKT).strftime("%H:%M"), "end": datetime.fromtimestamp(s1["t"] + 900, PKT).strftime("%H:%M"),
                          "start_ts": int(s0["t"]), "end_ts": int(s1["t"] + 900), "mode": mode, "soc_floor_pct": round(floor, 1),
                          "soc_target_pct": round(target, 1), "max_discharge_w": round(max_dis * 1000) if mode in ("discharge", "export") else None,
                          "max_charge_w": round(max_ch * 1000) if mode.startswith("charge") else None, "grid_charge": mode == "charge_grid",
                          # where a charge slot's charging ends (its last planned charge step); the slot then holds what it charged, with no
                          # discharge, to its end. Null on other slots
                          "charge_end_ts": charge_end,
                          "charge_end": datetime.fromtimestamp(charge_end, PKT).strftime("%H:%M") if charge_end else None,
                          # a charging slot never imports above the plan's MDI cap: its mean rate runs through the morning peak that sets the month's
                          # maximum demand, which the plan's own night-only charging never touched; other slots shave to the higher of the two
                          # (only where something on site holds a cap: a keyed slot-only table has no such setting)
                          "grid_cap_w": None if not held else (
                              round((mdi_cap_kw if mode.startswith("charge") else max(grid_cap, mdi_cap_kw)) * 1000) if mdi_cap_kw
                              else (round(grid_cap * 1000) if mode == "charge_grid" else None)),
                          "gen_charge": False, "export_allowed": mode == "export",
                          "genset": any(st["genset_on"] for st in steps[r["start"]:r["end"]]) or None,
                          "intent": _intent_words(mode, floor, target)})
            if mdi_cap_kw and not held and _translate_mdi(slots[-1], span, battery, float(mdi_cap_kw), lo):
                slots[-1]["intent"] = _intent_words(mode, slots[-1]["soc_floor_pct"], target)
        days.append({"date": day, "slots": slots})
    out = {"brand": brand, "grammar": gram, "days": days, "fallback": {"on_expiry": "inverter_native", "min_soc_floor_pct": fallback_floor_pct,
                                                                       "revert_after_hours": FALLBACK_HOURS}}
    out["mdi"] = {"cap_kw": round(float(mdi_cap_kw), 1) if mdi_cap_kw else None, "held": held,
                  "not_enforceable": None if held or not mdi_cap_kw else _mdi_not_enforceable(steps, out, battery, site, float(mdi_cap_kw))}
    return out


def _translate_mdi(slot: dict, span: list[dict], battery: dict | None, cap_kw: float, lo: float) -> bool:
    """Part 7.1 step 4 for a table a person keys into a device with no peak-shaving primitive: a discharging slot is given the room to keep the grid
    under the plan's MDI cap at the high load quantile. Its need per grid-up step is max(0, Lhi - PVlo - cap); a discharge slot's `max_discharge_w`
    is raised to at least the slot's largest need (Deye keys it as the slot's power), and the floor is lowered so that the energy sum(need) h / eta_d
    lies above it at the slot's start, never below the clamp or the outage reserve the plan holds in the slot (the floor wins; what is left over is
    `mdi.not_enforceable`). An export slot keeps its power (it discharges at it whatever the load, so more would only export more), and a hold or
    charge slot cannot discharge on such a device at all. Deviation from the plan's max(0, Lhi - cap): the low PV quantile is subtracted, as the
    optimiser's MDI rows count net import, so a sunny day's need is not keyed as a discharge that empties the battery before the evening. True when
    the slot changed."""
    if not battery or not battery.get("E_nom") or slot["mode"] not in DISCHARGING:
        return False
    need = [max(0.0, float(st.get("load_hi_kw", st["load_kw"])) - float(st.get("pv_lo_kw", st.get("pv_kw", 0.0))) - cap_kw)
            if st.get("grid_present", 1.0) >= 0.5 else 0.0 for st in span]
    peak = max(need, default=0.0)
    if peak <= 0.0:
        return False
    if slot["mode"] == "discharge":
        slot["max_discharge_w"] = max(slot["max_discharge_w"] or 0, round(peak * 1000))
    start = span[0].get("soc_start_pct")
    if start is not None:
        e_need_pct = sum(need) * 0.25 / float(battery.get("eta_d", 0.95)) / float(battery["E_nom"]) * 100.0
        reserve = max((float(st.get("reserve_pct") or 0.0) for st in span), default=0.0)
        # lowered, never raised: down to the energy the need takes from the slot's start, but no lower than the clamp or the reserve
        slot["soc_floor_pct"] = round(min(slot["soc_floor_pct"], max(lo, reserve, float(start) - e_need_pct)), 1)
    slot["mdi_shave_kw"] = round(peak, 1)
    return True


def _mdi_not_enforceable(steps: list[dict], compiled: dict, battery: dict | None, site: dict | None, cap_kw: float) -> dict | None:
    """Where nothing on site holds the plan's MDI cap: the demand the table reaches as keyed. The table is replayed through the twin on the plan's
    forecast (no slot carries a cap, so the twin shaves nothing), the plan's own high-load margin is added (max(0, Lhi - L) on grid-up minutes, as
    the optimiser's MDI rows add it), and the import is averaged over each demand block of the connection's MDI interval. The highest block, with
    the cap and the rise, when it is above the cap by more than half a kW; else None."""
    if not battery or not site or not steps:
        return None
    ts = np.repeat([int(st["t"]) for st in steps], 15) + np.tile(np.arange(15) * 60, len(steps))
    rep = lambda key, alt=None: np.repeat([float(st.get(key, st.get(alt, 0.0)) if alt else st.get(key, 0.0)) for st in steps], 15)
    series = {"ts": ts, "pv": rep("pv_forecast_kw", "pv_kw"), "load": rep("load_kw"), "crit": rep("critical_kw", "load_kw"),
              "grid": rep("grid_present"), "temp": np.full(len(ts), 30.0)}
    slots = [s for d in compiled["days"] for s in d["slots"]]
    e_nom = float(battery.get("E_nom") or 0.0)
    soc0 = float(battery["E0"]) / e_nom if battery.get("E0") is not None and e_nom else float(steps[0].get("soc_start_pct") or 50.0) / 100.0
    sim = twin.simulate(series, twin.slots_policy(slots, ts), twin.plant_of(site), soc0)
    margin = np.maximum(0.0, rep("load_hi_kw", "load_kw") - series["load"])
    flow = np.where(series["grid"] > 0.5, sim["imp"] + margin, 0.0)
    conns = site.get("connections") or []
    minutes = int(((conns[0].get("data") or {}) if conns else {}).get("mdi_interval_min") or 30)
    blocks = ts // (minutes * 60)
    peak = max(float(flow[blocks == b].mean()) for b in np.unique(blocks))
    if peak - cap_kw <= 0.5:
        return None
    return {"cap_kw": round(cap_kw, 1), "keyed_peak_kw": round(peak, 1), "rise_kw": round(peak - cap_kw, 1)}


def _intent_words(mode: str, floor: float, target: float) -> str:
    return {"discharge": f"Use the battery for the load, not below {floor:.0f} %",
            "export": f"Discharge to the grid, not below {floor:.0f} %",
            "hold": f"Hold the battery at {floor:.0f} % (no discharge)",
            "charge_grid": f"Charge from the grid to {target:.0f} %",
            "charge_pv_only": f"Charge from solar only, up to {target:.0f} %",
            "self_use": f"Self-use, not below {floor:.0f} %"}.get(mode, mode)


# ------------------------------------------------------------------------------------------------ the vendor's layout (Part 7.1 step 4, DLV-02)
#
# Each brand's translation answers (rows, settings, notes) for one day: the rows in the vendor's own layout, each with the compiled slots it keys
# (`_slots`, moved out of the row by vendor_table, so the page can put the plan's intent beside it); the values keyed once for the table (the global
# settings a brand keeps instead of per-slot values); and a sentence for each thing in the plan the brand cannot hold as planned. Register facts are
# from R6 3.1 to 3.4 and R8 2.15 (for Huawei read for facts only, nothing copied); none is bench-confirmed yet (DLV-00).

def _end(s: dict) -> str:
    """A slot's end as the vendor screens take it: a slot that ends at midnight ends at 23:59 (Huawei refuses 00:00 as an end, R6 3.2)."""
    return "23:59" if s["end"] == "00:00" else s["end"]


def _span(s: dict) -> str:
    return f"{s['start']} to {_end(s)}"


def _pct(x: float | None) -> int | None:
    return None if x is None else round(x)


def _setting(key: str, label: str, value, unit: str = "") -> dict:
    return {"key": key, "label": label, "value": value, "unit": unit}


def _min_floor(compiled: dict) -> float:
    return float((compiled.get("fallback") or {}).get("min_soc_floor_pct") or 20.0)


def _kw(x: float) -> str:
    return f"{x:,.0f} kW" if abs(x) >= 100 else f"{x:.1f} kW"


def _mdi_notes(compiled: dict) -> list[str]:
    """The sentence for a plan's MDI cap that nothing on site holds (`mdi.not_enforceable`): what the table reaches as keyed."""
    m = (compiled.get("mdi") or {}).get("not_enforceable")
    if not m:
        return []
    return [f"This inverter has no demand (MDI) limit the table can set, so the plan's {_kw(m['cap_kw'])} cap cannot be held: as keyed, the demand "
            f"can reach about {_kw(m['keyed_peak_kw'])}, {_kw(m['rise_kw'])} above it."]


def _soc_keyed(s: dict) -> int:
    """The state of charge a per-slot SoC field keys: a charging slot's target (it charges up to it), any other slot's floor (it stops there)."""
    return round(s["soc_target_pct"] if s["mode"].startswith("charge") else s["soc_floor_pct"])


def _deye(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    rows = [{"program": k + 1, "time": s["start"], "power_w": s["max_discharge_w"] or s["max_charge_w"] or 0, "soc_pct": _soc_keyed(s),
             "grid_charge": s["grid_charge"], "gen_charge": s["gen_charge"], "_slots": [k]} for k, s in enumerate(slots)]
    return rows, [], []


def _generic(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    rows = [{"slot": k + 1, "start": s["start"], "end": s["end"], "mode": s["mode"], "soc_pct": _soc_keyed(s), "_slots": [k]}
            for k, s in enumerate(slots)]
    return rows, [], []


VENDOR = {"deye": _deye, "other": _generic}


def vendor_table(compiled: dict | None, day_index: int = 0) -> dict:
    """One day's table as the installer keys it into the vendor's own screen: `rows` (in the vendor's layout), `slots` (per row, the compiled
    slots it keys, for the intent beside it), `settings` (keyed once for the table: {key, label, value, unit}) and `notes` (what the brand cannot
    hold as planned, one sentence each)."""
    if not compiled or not compiled.get("days"):
        return {"rows": [], "slots": [], "settings": [], "notes": []}
    day = compiled["days"][min(day_index, len(compiled["days"]) - 1)]
    rows, settings, notes = VENDOR.get(compiled.get("brand"), _generic)(day["slots"], compiled)
    return {"rows": [{k: v for k, v in r.items() if k != "_slots"} for r in rows], "slots": [r["_slots"] for r in rows], "settings": settings,
            "notes": notes + _mdi_notes(compiled)}


def vendor_view(compiled: dict, day_index: int = 0) -> list[dict]:
    """The rows of vendor_table: the table as the installer keys it into the vendor's own screen."""
    return vendor_table(compiled, day_index)["rows"]


def _mode_b_floor(s: dict) -> float | None:
    """The floor the step may discharge to: the plan's reserve where it holds one, never above the state of charge it plans for the step's end
    (a floor above the plan's own path would refuse the planned discharge)."""
    floor, soc = s.get("reserve_pct"), s.get("soc_pct")
    if floor is None or soc is None:
        return floor
    return round(min(float(floor), float(soc)), 1)


def mode_b(steps: list[dict]) -> dict:
    return {"refresh": {"heartbeat_s": 60, "revert_after_s": 1800, "residual_band_w": 300},
            "steps": [{"t": s["t"], "battery_power_w": round((s["discharge_kw"] - s["charge_kw"]) * 1000), "soc_floor_pct": _mode_b_floor(s),
                       "grid_charge_allowed": grid_charging(s), "genset_request": bool(s["genset_on"])} for s in steps]}


# ------------------------------------------------------------------------------------------------ signing (Ed25519, one key per site)

def _key_dir() -> Path:
    d = platformdb.data_dir() / "keys" / store.db_path().stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def _site_key(site_id: str) -> tuple[str, Ed25519PrivateKey]:
    with store.connect() as con:
        r = con.execute("SELECT key_id, private_path FROM site_keys WHERE site_id = ? AND retired IS NULL ORDER BY created DESC LIMIT 1",
                        (site_id,)).fetchone()
    if r and Path(r["private_path"]).exists():
        return r["key_id"], serialization.load_pem_private_key(Path(r["private_path"]).read_bytes(), password=None)
    key = Ed25519PrivateKey.generate()
    kid = store.new_id(8)
    path = _key_dir() / f"{site_id}-{kid}.pem"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    with store.connect() as con:
        con.execute("INSERT INTO site_keys (site_id, key_id, public_key, private_path, created) VALUES (?,?,?,?,?)",
                    (site_id, kid, base64.b64encode(pub).decode(), str(path), store._now()))
    return kid, key


def public_key(site_id: str) -> dict:
    kid, key = _site_key(site_id)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"key_id": kid, "alg": "Ed25519", "public_key": base64.b64encode(pub).decode()}


def canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()


def sign(site_id: str, body: dict) -> dict:
    kid, key = _site_key(site_id)
    return {**body, "signature": {"alg": "Ed25519", "key_id": kid, "value": base64.b64encode(key.sign(canonical(body))).decode()}}


def verify(message: dict, public_b64: str) -> bool:
    body = {k: v for k, v in message.items() if k != "signature"}
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(base64.b64decode(message["signature"]["value"]), canonical(body))
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------------------------ ems-edge/1.0 messages (strict; the one validator)

class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: Literal["ems-edge/1.0"] = CONTRACT
    seq: int = Field(ge=0)
    sent_at: str


class TelemetryBatch(_Msg):
    columns: list[str] = Field(min_length=2, max_length=40)
    rows: list[list[float | int | str | None]] = Field(max_length=1440)
    quality: dict = Field(default_factory=dict)


class Heartbeat(_Msg):
    uptime_s: int = Field(ge=0)
    active_plan: str | None = None
    valid_until: str | None = None
    control_state: Literal["following_plan", "native", "hold", "released", "disabled"] = "native"
    queue_depth: int = Field(0, ge=0)
    clock_offset_s: float = 0.0


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ts: str
    type: Literal["outage_start", "outage_end", "genset_start", "genset_stop", "plan_received", "plan_rejected", "plan_applied", "plan_expired",
                  "fallback_engaged", "fallback_cleared", "readback_mismatch", "write_failed", "budget_exhausted", "local_override",
                  "template_change", "firmware_change", "control_disabled", "control_enabled", "reboot", "link_lost", "clock_unsynced",
                  "battery_trust_low", "physics_check_failed", "data_gap", "installer_session"]
    detail: dict = Field(default_factory=dict)


class EventBatch(_Msg):
    events: list[Event] = Field(max_length=500)


class AckItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: str = Field(max_length=60)
    primitive: str = Field(max_length=60)
    written: float | str | None = None
    read_back: float | str | None = None
    status: Literal["ok", "clamped", "failed"]
    codes: list[str] = Field(default_factory=list, max_length=10)


class Ack(_Msg):
    plan_id: str
    status: Literal["received", "applied", "rejected", "expired"]
    items: list[AckItem] = Field(default_factory=list, max_length=200)


class Capabilities(_Msg):
    edge_version: str = Field(max_length=40)
    clock_synced: bool = True
    tier: Literal["monitor", "advisory_writes", "closed_loop"] = "monitor"
    devices: list[dict] = Field(default_factory=list, max_length=20)
