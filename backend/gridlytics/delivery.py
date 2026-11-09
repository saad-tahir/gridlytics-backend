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

#: `fields` are the vendor table's columns in order: exactly the keys of each row vendor_table writes (the web contract: columns = grammar.fields).
#: `max_slots` is what compile_mode_a fits a day into: Growatt's 3 plus 3 windows (Part 7.1 step 2) with Load First between them, which takes none
BRANDS = {
    "deye": {"label": "Deye / Sunsynk", "max_slots": 6, "layout": "six programs (Time of Use)", "ring": True, "min_slot_minutes": 30,
             "fields": ["program", "time", "power_w", "soc_pct", "grid_charge", "gen_charge", "sell"], "table_writes_per_day": 3, "mdi_primitive": False},
    "huawei": {"label": "Huawei SUN2000", "max_slots": 14, "layout": "fourteen TOU periods (charge or discharge), with a global cut-off and floors",
               "ring": False, "min_slot_minutes": 15, "fields": ["period", "start", "end", "charge_or_discharge", "days"], "table_writes_per_day": 3,
               "mdi_primitive": True},
    "growatt": {"label": "Growatt", "max_slots": 6, "layout": "three Battery First and three Grid First windows, Load First between them", "ring": False,
                "min_slot_minutes": 30, "fields": ["priority", "window", "start", "end", "enabled"], "table_writes_per_day": 3, "mdi_primitive": False},
    "sungrow": {"label": "Sungrow", "max_slots": 2, "layout": "two forced-charge windows (emulated), self-consumption between them", "ring": False,
                "min_slot_minutes": 30, "fields": ["window", "start", "end", "target_soc"], "table_writes_per_day": 4, "mdi_primitive": False},
    "voltronic": {"label": "Voltronic / Axpert", "max_slots": 3, "layout": "emulated mode changes (output and charger source priority)", "ring": False,
                  "min_slot_minutes": 60, "fields": ["change", "at", "output_priority", "charger_priority"], "table_writes_per_day": 3,
                  "mdi_primitive": False},
    "solis": {"label": "Solis", "max_slots": 6, "layout": "three charge and three discharge slots, self-use between them", "ring": False,
              "min_slot_minutes": 30, "fields": ["kind", "slot", "start", "end", "power_w", "current_a"], "table_writes_per_day": 3, "mdi_primitive": False},
    "other": {"label": "Generic", "max_slots": 6, "layout": "generic six-slot table", "ring": False, "min_slot_minutes": 30,
              "fields": ["slot", "start", "end", "mode", "soc_pct"], "table_writes_per_day": 3, "mdi_primitive": False},
}
#: R6 I6: after this many seconds without the cloud (five missed 60-second heartbeats) the edge flags the site unattended and keeps executing the
#: cached plan to its valid_until; only then does it fall back
LINK_LOSS_AFTER_S = 300
#: R6 I2's lowest absolute floor (per cent)
FLOOR_MIN_PCT = 10.0


def fallback_floor(fallback_floor_pct: float, bms_low_pct: float = 10.0) -> float:
    """The absolute floor of a plan's fallback block and of every slot (R6 I2, Part 7.1 step 5): the larger of the BMS low cap + 2, the reserve the
    owner set and 10 %."""
    return float(max(bms_low_pct + 2, fallback_floor_pct, FLOOR_MIN_PCT))


def export_floor_w(site: dict | None) -> int:
    """The export cap that holds in every slot and in fallback (R6 I3), in W: the licensed export limit at a net-billing or legacy net-metering
    connection (build_problem's X_max), else 0."""
    conns = (site or {}).get("connections") or []
    d = (conns[0].get("data") or {}) if conns else {}
    return int(round(float(d.get("export_cap_kw") or 0.0) * 1000)) if d.get("regime") in ("net_billing", "net_metering_legacy") else 0


def native_ref(site: dict | None) -> str | None:
    """The native-settings capture the fallback restores, "site_id:vN" (`sites.native_settings_ref` names (site_id, version), Part 3.2); None
    before a capture."""
    nat = (site or {}).get("native") or {}
    return f"{site['id']}:v{nat['version']}" if site and site.get("id") and nat.get("version") is not None else None


def fallback_block(site: dict | None, fallback_floor_pct: float, bms_low_pct: float = 10.0) -> dict:
    """The fallback block of a plan or a release (Part 7.1 step 6, U3 3.4, R6 I2 to I4 and I6): on expiry the inverter's native settings (the
    capture `native_settings_ref` names), never below `min_soc_floor_pct` and never exporting above `max_export_w_floor`; after
    `on_link_loss_after_s` without the cloud an edge keeps executing the cached plan to its valid_until. No revert timer: a keyed Deye or Growatt
    table persists and repeats daily (Part 7.1 step 6 makes the table itself safe to repeat), and the 1,800-second revert belongs to Mode B's
    volatile setpoints (`mode_b`'s refresh block). Part 7.1's opening line still says 'revert after 3 hours'; step 6 and DLV-03 are the rule."""
    return {"on_expiry": "inverter_native", "on_link_loss_after_s": LINK_LOSS_AFTER_S,
            "min_soc_floor_pct": fallback_floor(fallback_floor_pct, bms_low_pct), "max_export_w_floor": export_floor_w(site),
            "native_settings_ref": native_ref(site)}


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
    lo = fallback_floor(fallback_floor_pct, bms_low_pct)  # no slot below the fallback block's own floor (step 5)
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

        def moved(a: int, b: int) -> float:
            return sum(abs(steps[i]["discharge_kw"]) + abs(steps[i]["charge_kw"]) for i in range(a, b))

        def widen(k: int) -> bool:
            # a short run that moves energy (a single-quarter MDI or peak discharge) is widened to the brand's minimum with quarters taken from a
            # neighbour that stays long enough and moves less in them, the later one first so the run keeps its planned start: folded whole into
            # a hold beside it, the planned discharge vanished from the table (Faisalabad's Deye day read '07:15-00:00 Hold'). The run's floor still
            # stops it where the plan's discharge does, so it moves no more energy than planned
            r = runs[k]
            need = min_steps - (r["end"] - r["start"])
            if r["energy"] <= 0.0:
                return False
            best = None
            for j in (k + 1, k - 1):
                if not 0 <= j < len(runs) or runs[j]["end"] - runs[j]["start"] - need < min_steps:
                    continue
                a, b = (runs[j]["start"], runs[j]["start"] + need) if j == k + 1 else (runs[j]["end"] - need, runs[j]["end"])
                if moved(a, b) < r["energy"] and (best is None or moved(a, b) < best[1]):
                    best = (j, moved(a, b))
            if best is None:
                return False
            j = best[0]
            if j == k + 1:
                r["end"] += need
                runs[j]["start"] += need
            else:
                r["start"] -= need
                runs[j]["end"] -= need
            for x in (r, runs[j]):
                x.update(floor=min(intents[i]["floor"] for i in range(x["start"], x["end"])),
                         target=max(intents[i]["target"] for i in range(x["start"], x["end"])), energy=moved(x["start"], x["end"]))
            return True

        def coalesce():
            # adjacent runs of one mode become one run (a merge keeps the absorbing run's mode, so it can leave two equal neighbours)
            k = 1
            while k < len(runs):
                if runs[k]["mode"] == runs[k - 1]["mode"]:
                    merge(k, k - 1)
                else:
                    k += 1

        changed = True
        while changed and len(runs) > 1:
            changed = False
            for k, r in enumerate(runs):
                if r["end"] - r["start"] < min_steps:
                    if not widen(k):
                        merge(k, neighbour(k))
                    changed = True
                    break
        coalesce()
        # the least energy is dropped until the day fits, merging equal neighbours after every drop: without it two equal runs stayed apart and took
        # two of the brand's slots, and the next drop took a planned charge or discharge instead (Sungrow's two slots came out as one discharge split
        # in two, the night's grid charge gone; another day as two charge slots, the evening discharge gone)
        while len(runs) > gram["max_slots"]:
            k = min(range(len(runs)), key=lambda j: runs[j]["energy"])
            merge(k, neighbour(k))
            coalesce()
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
                          # the plan's own genset runs inside the slot ([start_ts, end_ts], epoch s): a replay runs the set in these and not
                          # through the whole slot, as `genset` alone made it do (the off-grid farm's one self-use slot a day ran its set all day)
                          "genset_runs": _runs([st for st in span if st["genset_on"]]),
                          "intent": _intent_words(mode, floor, target)})
            if mdi_cap_kw and not held and _translate_mdi(slots[-1], span, battery, float(mdi_cap_kw), lo):
                slots[-1]["intent"] = _intent_words(mode, slots[-1]["soc_floor_pct"], target)
        # the safe tail (R6 I1, Part 7.1 step 6): a keyed Deye or Growatt table persists and, with no new plan, repeats; a ring's last program also
        # runs on round midnight. So a discharging last slot stops at the larger of the reserve the plan holds at the day's end and the fallback
        # floor (a hold or a charge does not discharge: its level stands)
        last = slots[-1]
        tail = _tail_floor(steps[idx[-1]], lo)
        if last["mode"] in DISCHARGING and last["soc_floor_pct"] < tail:
            last["soc_floor_pct"] = tail
            last["intent"] = _intent_words(last["mode"], tail, last["soc_target_pct"])
        days.append({"date": day, "slots": slots})
    out = {"brand": brand, "grammar": gram, "days": days, "fallback": fallback_block(site, fallback_floor_pct, bms_low_pct)}
    # the battery's own limits, for the brands that key a power once for the table, or a power where a slot has none (Deye's self-use program)
    b = battery or {}
    out["limits"] = {"max_charge_w": round(b["P_ch"] * 1000) if b.get("P_ch") else None,
                     "max_discharge_w": round(b["P_dis"] * 1000) if b.get("P_dis") else None, "battery_v": b.get("V_nom")}
    out["mdi"] = {"cap_kw": round(float(mdi_cap_kw), 1) if mdi_cap_kw else None, "held": held,
                  "not_enforceable": None if held or not mdi_cap_kw else _mdi_not_enforceable(steps, out, battery, site, float(mdi_cap_kw))}
    out["safety"] = table_safety(out, steps, battery, site)
    return out


def _tail_floor(end_step: dict, floor: float) -> float:
    """The floor a day table's last slot stops at (R6 I1): the larger of the reserve the plan holds at the day's last step and the fallback floor."""
    return round(float(np.clip(max(floor, float(end_step.get("reserve_pct") or 0.0)), floor, 100.0)), 1)


def _midnight(date_local: str) -> int:
    return int(datetime.fromisoformat(date_local).replace(tzinfo=PKT).timestamp())


def table_safety(compiled: dict, steps: list[dict], battery: dict | None, site: dict | None) -> dict:
    """Whether each day table is safe to repeat with no new plan (Part 7.1 step 6, R6 I1 and I5), as a keyed Deye or Growatt table, or an edge
    holding its cached plan through a lost link, runs it. Per day: no slot's floor is below the fallback floor; a discharging last slot stops at
    the reserve the plan holds at the day's end (`tail_floor_pct`); and a whole day's table (tomorrow's, the fallback day) run again over the next
    day, from the plan's state of charge at the day's end and on the plan's forecast for that day (the same clock time a day or two earlier past
    the horizon), meets every forecast outage that starts in it with at least the reserve the plan keeps for it there, less 2 points (never more
    than the plan's own state of charge at that time). `ok` is False on any failure, with the day's `reason`, and the wrong-plan guard refuses
    the plan (step 8). Today's remainder is checked for its floors and its tail: the plan itself covers it."""
    floor = float((compiled.get("fallback") or {}).get("min_soc_floor_pct") or 0.0)
    by_t = {int(st["t"]): st for st in steps}
    plant = twin.plant_of(site) if site and (battery or {}).get("E_nom") and (site.get("assets") or {}).get("battery") else None
    days = []
    for d in compiled.get("days") or []:
        slots = d["slots"]
        if not slots:
            continue
        last = slots[-1]
        end_step = by_t.get(int(last["end_ts"]) - 900) or {}
        rec = {"date": d["date"], "ok": True, "reason": None, "tail_floor_pct": _tail_floor(end_step, floor)}
        whole = int(slots[0]["start_ts"]) == _midnight(d["date"]) and int(last["end_ts"]) == _midnight(d["date"]) + 86400
        if min(float(s["soc_floor_pct"]) for s in slots) < floor - 0.05:
            rec.update(ok=False, reason="floor_below_fallback")
        elif last["mode"] in DISCHARGING and float(last["soc_floor_pct"]) < rec["tail_floor_pct"] - 0.05:
            rec.update(ok=False, reason="tail_below_reserve")
        elif whole and plant is not None and end_step.get("soc_pct") is not None:
            short = _repeat_short(slots, by_t, plant, float(end_step["soc_pct"]))
            if short:
                rec.update(ok=False, reason="reserve_short_on_repeat", **short)
        days.append(rec)
    return {"ok": all(r["ok"] for r in days), "days": days}


def _repeat_short(slots: list[dict], by_t: dict[int, dict], plant: "twin.Plant", soc_end_pct: float) -> dict | None:
    """The day's table run again over the 24 hours after it ends (table_safety): the first forecast outage it meets short of the plan's reserve,
    as {short_at (epoch s), soc_pct, reserve_pct}, else None (also when the forecast for that day is not known)."""
    w0 = int(slots[-1]["end_ts"])
    src = []
    for q in range(w0, w0 + 86400, 900):
        st = by_t.get(q) or by_t.get(q - 86400) or by_t.get(q - 2 * 86400)
        if st is None:
            return None
        src.append(st)
    ts = np.arange(w0, w0 + 86400, 60)
    rep = twin.repeated_slots(slots, w0, w0 + 86400)  # what a person keyed runs again by clock time

    def col(key: str, alt: str | None = None, default: float = 0.0) -> np.ndarray:
        return np.repeat([float(st[key] if st.get(key) is not None else st.get(alt) if alt and st.get(alt) is not None else default)
                          for st in src], 15)

    series = {"ts": ts, "pv": col("pv_forecast_kw", "pv_kw"), "load": col("load_kw"), "crit": col("critical_kw", "load_kw"),
              "grid": col("grid_present", default=1.0), "temp": np.full(len(ts), 30.0)}
    soc0 = soc_end_pct / 100.0
    sim = twin.simulate(series, twin.slots_policy(rep, ts), plant, soc0)
    down = series["grid"] < 0.5
    for i in np.flatnonzero(down[1:] & ~down[:-1]) + 1:  # where the grid goes down: the next known or likely window
        st = src[i // 15]
        reserve = float(st.get("reserve_pct") or 0.0)
        planned = float(st["soc_start_pct"]) if st.get("soc_start_pct") is not None else 100.0
        soc = float(sim["soc"][i - 1]) * 100.0
        if soc < min(reserve, planned) - 2.0:
            return {"short_at": int(ts[i]), "soc_pct": round(soc, 1), "reserve_pct": round(reserve, 1)}
    return None


def _runs(on: list[dict]) -> list[list[int]]:
    """[start_ts, end_ts] of each stretch of consecutive quarters among `on` (the steps of a slot with the genset on)."""
    out: list[list[int]] = []
    for st in on:
        t = int(st["t"])
        if out and out[-1][1] == t:
            out[-1][1] = t + 900
        else:
            out.append([t, t + 900])
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


def _limit(compiled: dict, key: str) -> int | float | None:
    """One of the battery's limits the compiled table carries (`limits`: max_charge_w, max_discharge_w, battery_v); None on a table from before."""
    return (compiled.get("limits") or {}).get(key)


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


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def _ring(slots: list[dict], n: int) -> list[tuple[int, int]]:
    """(slot index, start minute) for exactly `n` programs where the day has fewer slots: the longest stretch is split in two, on a quarter-hour
    mark where it is at least 30 minutes long, until there are `n` (a split program repeats its slot's settings, so the device does the same)."""
    pieces = [[k, _minutes(s["start"]), _minutes(s["end"]) or 1440] for k, s in enumerate(slots)]
    while len(pieces) < n:
        i = max(range(len(pieces)), key=lambda j: pieces[j][2] - pieces[j][1])
        k, a, b = pieces[i]
        if b - a < 2:
            break
        mid = (a + (b - a) // 2) // 15 * 15 if b - a >= 30 else a + (b - a) // 2
        mid = mid if mid > a else a + 15
        pieces[i:i + 1] = [[k, a, mid], [k, mid, b]]
    return [(k, a) for k, a, _ in pieces]


def _deye(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    """Deye / Sunsynk (R6 3.1): six programs in a ring, each a start time, a power (the most the battery discharges in it), a capacity (the SoC a
    discharge stops at, or a charge from grid or genset goes up to), the grid and genset charge flags and the sell flag (export allowed). All six are
    always keyed: where the plan needs fewer, a slot's settings are repeated, so no program keeps an old value. A self-use slot discharges at the
    battery's power (keyed as 0 it would be a hold); a hold is power 0 at its level; an export slot sells."""
    rows, notes = [], []
    rated = _limit(compiled, "max_discharge_w")
    for i, (k, start) in enumerate(_ring(slots, 6)):
        s = slots[k]
        if s["mode"] in ("discharge", "export"):
            power = s["max_discharge_w"]
        elif s["mode"] == "self_use":
            power = s["max_discharge_w"] or rated
        elif s["mode"].startswith("charge"):
            power = s["max_charge_w"]
        else:
            power = 0
        rows.append({"program": i + 1, "time": f"{start // 60:02d}:{start % 60:02d}", "power_w": power, "soc_pct": _soc_keyed(s),
                     "grid_charge": s["grid_charge"], "gen_charge": s["gen_charge"], "sell": bool(s.get("export_allowed")), "_slots": [k]})
    if len(rows) > len(slots):
        notes.append("Deye runs its six programs in a ring: all six are set, and where the plan needs fewer a program repeats the settings of the "
                     "one before it, so none keeps an old value.")
    if any(r["power_w"] is None for r in rows):
        notes.append("Key the battery's rated discharge power where the power is blank: those programs run self-use.")
    return rows, [], notes


def _windows(slots: list[dict], kind_of) -> list[dict]:
    """The brand's windows: runs of adjacent slots of the same kind (`kind_of(slot)`, None for a slot that takes no window), each with its kind,
    the slots it keys and its first and last slot."""
    out: list[dict] = []
    for k, s in enumerate(slots):
        kind = kind_of(s)
        if kind is None:
            continue
        if out and out[-1]["kind"] == kind and out[-1]["slots"][-1] == k - 1:
            out[-1]["slots"].append(k)
            out[-1]["last"] = s
        else:
            out.append({"kind": kind, "slots": [k], "first": s, "last": s})
    return out


def _joined(spans: list[str]) -> str:
    return spans[0] if len(spans) == 1 else ", ".join(spans[:-1]) + " and " + spans[-1]


EVERY_DAY = "Every day"


def _huawei(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    """Huawei SUN2000 with LUNA2000 in TOU mode (R6 3.2): a period is charge or discharge, every day (a day-table must be safe to repeat), and
    outside every period the battery does not discharge, so a hold (and a solar-only charge, with excess PV set to charge the battery) is a gap,
    not a period; the last period ends at 23:59. One grid-charge cut-off for the day (the highest grid-charge target), one discharge cut-off (the
    lowest floor of a discharging slot), the backup SoC at the fallback floor, and the plan's MDI cap as one capacity-control period every day
    (Huawei's peak-shaving primitive, `mdi_primitive`)."""
    kind = lambda s: "charge" if s["mode"] == "charge_grid" else "discharge" if s["mode"] in DISCHARGING else None
    wins = _windows(slots, kind)
    rows = [{"period": i + 1, "start": w["first"]["start"], "end": _end(w["last"]), "charge_or_discharge": w["kind"], "days": EVERY_DAY,
             "_slots": w["slots"]} for i, w in enumerate(wins)]
    charge = [s for s in slots if s["mode"] == "charge_grid"]
    dis = [s for s in slots if s["mode"] in DISCHARGING]
    cutoff = max((s["soc_target_pct"] for s in charge), default=None)
    floor = min((s["soc_floor_pct"] for s in dis), default=None)
    settings = [_setting("working_mode", "Working mode", "TOU (LUNA2000)"),
                _setting("charge_from_grid", "Charge from grid", bool(charge)),
                _setting("grid_charge_cutoff_soc", "Grid-charge cut-off SoC", _pct(cutoff), "%"),
                _setting("discharge_cutoff_soc", "Discharge cut-off SoC", _pct(floor), "%"),
                _setting("backup_soc", "Backup power SoC", _pct(_min_floor(compiled)), "%"),
                _setting("excess_pv_in_tou", "Excess PV energy in TOU", "Charge the battery")]
    cap = (compiled.get("mdi") or {}).get("cap_kw")
    if cap:
        settings.append(_setting("peak_shaving_power_w", "Capacity control: peak power, 00:00 to 23:59 every day", round(cap * 1000), "W"))
    notes = []
    gaps = [s for s in slots if kind(s) is None]
    if gaps:
        notes.append(f"No period from {_joined([_span(s) for s in gaps])}: outside its periods Huawei does not discharge the battery, so it holds "
                     "(and charges from surplus solar).")
    for s in dis:
        if floor is not None and s["soc_floor_pct"] > floor + 3:
            notes.append(f"Huawei keeps one discharge cut-off for the day ({floor:.0f} %): the discharge from {_span(s)} is planned to stop at "
                         f"{s['soc_floor_pct']:.0f} % but will run on to {floor:.0f} %.")
    for s in charge:
        if cutoff is not None and s["soc_target_pct"] < cutoff - 3:
            notes.append(f"Huawei keeps one grid-charge cut-off for the day ({cutoff:.0f} %): the charge from {_span(s)} is planned to stop at "
                         f"{s['soc_target_pct']:.0f} % but will run on to {cutoff:.0f} %.")
    return rows, settings, notes


def _floor_notes(brand: str, what: str, floor: float, slots: list[dict]) -> list[str]:
    """A sentence per discharging slot whose own floor the brand's one global floor (`floor`) cannot keep (more than 3 points apart, the fidelity
    tolerance)."""
    return [f"{brand} keeps one {what} ({floor:.0f} %): the discharge from {_span(s)} is planned to stop at {s['soc_floor_pct']:.0f} % but will run on "
            f"to {floor:.0f} %." for s in slots if s["mode"] in DISCHARGING and s["soc_floor_pct"] > floor + 3]


def _sungrow(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    """Sungrow SH in self-consumption with forced charging (R6 3.3): two forced-charge windows of start, end and target SoC, every day; outside
    them the battery serves the load down to Min SoC (0 to 50 %). Max SoC (50 to 100 %), Min SoC, the backup reserve and the power limits are global.
    The forced-charge target is the slot's charge target (the floor was keyed before)."""
    charge = [(k, s) for k, s in enumerate(slots) if s["mode"] == "charge_grid"]
    rows = [{"window": i + 1, "start": s["start"], "end": _end(s), "target_soc": round(s["soc_target_pct"]), "_slots": [k]}
            for i, (k, s) in enumerate(charge[:2])]
    dis = [s for s in slots if s["mode"] in DISCHARGING]
    floor = min((s["soc_floor_pct"] for s in dis), default=_min_floor(compiled))
    min_soc = min(floor, 50.0)
    max_soc = min(100.0, max([s["soc_target_pct"] for s in slots] + [50.0]))
    settings = [_setting("ems_mode", "EMS mode", "Self-consumption"),
                _setting("forced_charging", "Forced charging", "Enabled, every day" if charge else "Disabled"),
                _setting("max_soc", "Max SoC", _pct(max_soc), "%"),
                _setting("min_soc", "Min SoC", _pct(min_soc), "%"),
                _setting("reserved_soc_backup", "Reserved SoC for backup", _pct(_min_floor(compiled)), "%"),
                _setting("max_charging_power_w", "Max charging power", max((s["max_charge_w"] or 0 for _, s in charge), default=0)
                         or _limit(compiled, "max_charge_w"), "W"),
                _setting("max_discharging_power_w", "Max discharging power", _limit(compiled, "max_discharge_w")
                         or max((s["max_discharge_w"] or 0 for s in dis), default=0) or None, "W")]
    notes = []
    if len(charge) > 2:
        notes.append(f"Sungrow has two forced-charge windows: the charge from {_joined([_span(s) for _, s in charge[2:]])} cannot be keyed.")
    held = [s for s in slots if s["mode"] in ("hold", "charge_pv_only")]
    if held:
        notes.append(f"Sungrow cannot keep the battery from discharging outside a forced-charge window: from {_joined([_span(s) for s in held])} it "
                     f"serves the load down to the Min SoC ({min_soc:.0f} %).")
    if floor > 50.0:
        notes.append(f"Sungrow's Min SoC goes up to 50 %: the plan's floor of {floor:.0f} % is keyed as 50 %.")
    return rows, settings, notes + _floor_notes("Sungrow", "Min SoC", min_soc, slots)


#: Growatt's priority per slot mode: Battery First charges (from the grid where the global flag allows) and does not discharge, so a hold is one too;
#: Grid First discharges to the grid; discharge and self-use are Load First, the default between windows (None: no window)
_GROWATT = {"charge_grid": "battery_first", "charge_pv_only": "battery_first", "hold": "battery_first", "export": "grid_first"}


def _growatt(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    """Growatt SPH (R6 3.4): up to three Battery First windows (charge to one global stop SoC, from the grid per one global flag, no discharge)
    and up to three Grid First windows (discharge to the grid down to one global floor), each a begin, end and enable; the rest of the day runs
    Load First (the battery serves the load). Adjacent slots of one priority are one window, so a six-slot day needs at most three of each."""
    rows, notes, n = [], [], {"battery_first": 0, "grid_first": 0}
    for w in _windows(slots, lambda s: _GROWATT.get(s["mode"])):
        n[w["kind"]] += 1
        rows.append({"priority": w["kind"], "window": n[w["kind"]], "start": w["first"]["start"], "end": _end(w["last"]), "enabled": True,
                     "_slots": w["slots"]})
    for pri, name in (("battery_first", "Battery First"), ("grid_first", "Grid First")):
        if n[pri] > 3:
            notes.append(f"Growatt has three {name} windows: the plan's {name} windows 4 to {n[pri]} cannot be keyed.")
    charge = [s for s in slots if s["mode"].startswith("charge")]
    grid = any(s["mode"] == "charge_grid" for s in slots)
    stop = max((s["soc_target_pct"] for s in charge), default=max((s["soc_floor_pct"] for s in slots if s["mode"] == "hold"), default=None))
    exp_floor = min((s["soc_floor_pct"] for s in slots if s["mode"] == "export"), default=_min_floor(compiled))
    load_floor = min((s["soc_floor_pct"] for s in slots if s["mode"] in ("discharge", "self_use")), default=_min_floor(compiled))
    settings = [_setting("charge_from_grid", "Battery First: charge from grid", grid),
                _setting("battery_first_stop_soc", "Battery First stop SoC", _pct(stop), "%"),
                _setting("grid_first_min_soc", "Grid First minimum SoC", _pct(exp_floor), "%"),
                _setting("load_first_stop_soc", "Load First: stop discharging at", _pct(load_floor), "%")]
    if grid and any(s["mode"] in ("hold", "charge_pv_only") for s in slots):
        notes.append("A hold or a solar-only charge is a Battery First window: while charging from the grid is on, it charges from the grid too.")
    return rows, settings, notes + _floor_notes("Growatt", "Load First floor", load_floor, [s for s in slots if s["mode"] != "export"])


#: Voltronic / Axpert output source priority per slot mode (SUB: solar, utility, then battery, so the battery holds while the grid is up; SBU:
#: solar, battery, then utility) and charger source priority (SNU: solar and utility; OSO: only solar)
_POP = {"hold": "SUB", "charge_grid": "SUB", "charge_pv_only": "SUB"}


def _voltronic(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    """Voltronic / Axpert: no time table on the inverter. At each boundary where the priorities change, the output and charger source priorities
    are set (by a person, or by an edge device: the emulation the plan names), and the battery goes back to utility at one SoC."""
    rows = []
    for k, s in enumerate(slots):
        pop, pcp = _POP.get(s["mode"], "SBU"), "SNU" if s["mode"] == "charge_grid" else "OSO"
        if rows and (rows[-1]["output_priority"], rows[-1]["charger_priority"]) == (pop, pcp):
            rows[-1]["_slots"].append(k)
            continue
        rows.append({"change": len(rows) + 1, "at": s["start"], "output_priority": pop, "charger_priority": pcp, "_slots": [k]})
    floor = min((s["soc_floor_pct"] for s in slots if s["mode"] in DISCHARGING), default=_min_floor(compiled))
    notes = ["Voltronic has no time table: each change is made at its time, by a person or by an edge device."] if len(rows) > 1 else []
    return rows, [_setting("back_to_utility_soc", "Back to utility at SoC", _pct(floor), "%")], notes + _floor_notes(
        "Voltronic", "back-to-utility SoC", floor, slots)


def _solis(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    """Solis (R8 2.15): three charge and three discharge slots, each with its times and a current; self-use outside them. The current is the
    slot's power over the battery's nominal voltage (`limits.battery_v`), null where the battery's voltage is not known."""
    v = _limit(compiled, "battery_v")
    rows, notes, n = [], [], {"charge": 0, "discharge": 0}
    kind = lambda s: "charge" if s["mode"] == "charge_grid" else "discharge" if s["mode"] in ("discharge", "export") else None
    for w in _windows(slots, kind):
        n[w["kind"]] += 1
        ws = [slots[k] for k in w["slots"]]
        power = max(((s["max_charge_w"] if w["kind"] == "charge" else s["max_discharge_w"]) or 0) for s in ws) or None
        rows.append({"kind": w["kind"], "slot": n[w["kind"]], "start": w["first"]["start"], "end": _end(w["last"]), "power_w": power,
                     "current_a": round(power / v) if v and power else None, "_slots": w["slots"]})
    if rows and not v:
        notes.append("Solis takes a current for each slot: divide its power by the battery's nominal voltage, which this battery's details do not give.")
    held = [s for s in slots if s["mode"] in ("hold", "charge_pv_only")]
    if held:
        notes.append(f"Solis cannot hold the battery outside its slots: from {_joined([_span(s) for s in held])} it runs self-use.")
    for k in ("charge", "discharge"):
        if n[k] > 3:
            notes.append(f"Solis has three {k} slots: the plan's {k} slots 4 to {n[k]} cannot be keyed.")
    floor = min((s["soc_floor_pct"] for s in slots if s["mode"] in DISCHARGING), default=_min_floor(compiled))
    return rows, [_setting("overdischarge_soc", "Overdischarge SoC", _pct(floor), "%")], notes + _floor_notes("Solis", "overdischarge SoC", floor, slots)


def _generic(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    rows = [{"slot": k + 1, "start": s["start"], "end": s["end"], "mode": s["mode"], "soc_pct": _soc_keyed(s), "_slots": [k]}
            for k, s in enumerate(slots)]
    return rows, [], []


VENDOR = {"deye": _deye, "huawei": _huawei, "growatt": _growatt, "sungrow": _sungrow, "voltronic": _voltronic, "solis": _solis, "other": _generic}


def vendor_table(compiled: dict | None, day_index: int = 0) -> dict:
    """One day's table as the installer keys it into the vendor's own screen: `rows` (in the vendor's layout), `slots` (per row, the compiled
    slots it keys, for the intent beside it), `settings` (keyed once for the table: {key, label, value, unit}) and `notes` (what the brand cannot
    hold as planned, one sentence each)."""
    if not compiled or not compiled.get("days"):
        return {"rows": [], "slots": [], "settings": [], "notes": []}
    day = compiled["days"][min(day_index, len(compiled["days"]) - 1)]
    rows, settings, notes = VENDOR.get(compiled.get("brand"), _generic)(day["slots"], compiled)
    return {"rows": [{k: v for k, v in r.items() if k != "_slots"} for r in rows], "slots": [r["_slots"] for r in rows], "settings": settings,
            "notes": notes + _part_day_notes(day["slots"], compiled) + _mdi_notes(compiled)}


def _part_day_notes(slots: list[dict], compiled: dict) -> list[str]:
    """For a table of part of a day (today's remainder from the tick, or the horizon's last day): what runs round midnight once it is keyed, since a
    keyed table stays and repeats (Part 7.1 step 6): a ring's last program runs on to its first program's time; other brands keep nothing outside
    their windows. The table's last slot is made safe to sit in (compile_mode_a's safe tail)."""
    if not slots:
        return []
    out = []
    first, last = slots[0], slots[-1]
    ring = BRANDS.get(compiled.get("brand"), BRANDS["other"])["ring"]
    if first["start"] != "00:00":
        out.append(f"This table starts at {first['start']}: key tomorrow's table before midnight."
                   + (f" Until then the settings of its last slot (from {last['start']}) also run from midnight to {first['start']}." if ring else ""))
    if last["end"] != "00:00":
        out.append(f"This table ends at {last['end']}, where the plan ends: " + ("its last program runs on to midnight." if ring else
                   "after it the inverter runs its own default mode until the next table."))
    return out


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
