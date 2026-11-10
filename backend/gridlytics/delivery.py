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
from pathlib import Path, PureWindowsPath

import numpy as np
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import platform as platformdb
from . import store, twin
from .contract import canonical_json as _jcs

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
    if site is not None and site.get("tier") != "closed_loop":
        # a person keys this table (TWN-02, round 7): each slot carries what the brand runs in its place, so every replay below and after (the
        # MDI report, the safety check, the guard, the compare drawer, the demo and the fidelity check) runs the table as the device does. An edge
        # executes the slots as compiled, and a table compiled without a site keeps v1's generic inverter
        for d in days:
            for slot, ov in zip(d["slots"], as_keyed(brand, d["slots"], out)):
                if ov:
                    slot["as_keyed"] = ov
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


def _inner_charge_end(s: dict) -> bool:
    """Whether a charge slot's charging ends before the slot does (`charge_end`, HANDOFF 8.A.4): it then holds what it charged to its end."""
    ce = s.get("charge_end_ts")
    return bool(str(s["mode"]).startswith("charge") and ce and s.get("charge_end") and int(s["start_ts"]) < int(ce) < int(s["end_ts"])
                and _minutes(s["start"]) < _minutes(s["charge_end"]) < (_minutes(s["end"]) or 1440))


def _charge_tails(slots: list[dict], room: int) -> set[int]:
    """The charge slots whose early charge end a brand with `room` slots or programs to spare keys as a hold of its own (the first ones)."""
    return set([k for k, s in enumerate(slots) if _inner_charge_end(s)][:max(0, room)])


def _ends_keyed(brand: str, slots: list[dict]) -> set[int]:
    """The charge slots whose early charge end the brand's table keys: every one on Huawei (its charge period ends there, and outside its periods
    it holds) and Voltronic (a change of charger priority there); on Deye and the generic table those that take a spare program or row; none on
    Sungrow, Growatt or Solis, whose charge window runs to the slot's end."""
    if brand in ("huawei", "voltronic"):
        return {k for k, s in enumerate(slots) if _inner_charge_end(s)}
    if brand in ("deye", "other"):
        return _charge_tails(slots, BRANDS[brand]["max_slots"] - len(slots))
    return set()


#: the brand as a sentence's subject in the notes
_NAME = {"deye": "Deye", "huawei": "Huawei", "sungrow": "Sungrow", "growatt": "Growatt", "voltronic": "Voltronic", "solis": "Solis",
         "other": "The inverter"}


def _charge_end_notes(brand: str, slots: list[dict]) -> list[str]:
    """A sentence per charge slot whose early charge end the brand's table cannot key: the charge runs on to its target if it is not there by then."""
    keyed = _ends_keyed(brand, slots)
    return [f"The charge from {_span(s)} is planned to end at {s['charge_end']}: {_NAME.get(brand, _NAME['other'])} keeps charging after it until "
            f"the battery reaches {s['soc_target_pct']:.0f} %, if it has not by then." for k, s in enumerate(slots) if _inner_charge_end(s) and k not in keyed]


def _ring(slots: list[dict], n: int) -> list[tuple[int, int, bool]]:
    """(slot index, start minute, hold) for exactly `n` programs where the day has fewer slots: a charge whose charging ends before its slot does
    takes a spare program first, a hold from its charge end (`hold` True); then the longest stretch is split in two, on a quarter-hour mark where it
    is at least 30 minutes long, until there are `n` (a split program repeats its slot's settings, so the device does the same)."""
    tails = _charge_tails(slots, n - len(slots))
    pieces = []
    for k, s in enumerate(slots):
        a, b = _minutes(s["start"]), _minutes(s["end"]) or 1440
        if k in tails:
            ce = _minutes(s["charge_end"])
            pieces += [[k, a, ce, False], [k, ce, b, True]]
        else:
            pieces.append([k, a, b, False])
    while len(pieces) < n:
        i = max(range(len(pieces)), key=lambda j: pieces[j][2] - pieces[j][1])
        k, a, b, h = pieces[i]
        if b - a < 2:
            break
        mid = (a + (b - a) // 2) // 15 * 15 if b - a >= 30 else a + (b - a) // 2
        mid = mid if mid > a else a + 15
        pieces[i:i + 1] = [[k, a, mid, h], [k, mid, b, h]]
    return [(k, a, h) for k, a, _, h in pieces]


def _deye(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    """Deye / Sunsynk (R6 3.1): six programs in a ring, each a start time, a power (the most the battery discharges in it), a capacity (the SoC a
    discharge stops at, or a charge from grid or genset goes up to), the grid and genset charge flags and the sell flag (export allowed). All six are
    always keyed: where the plan needs fewer, a slot's settings are repeated, so no program keeps an old value. A self-use slot discharges at the
    battery's power (keyed as 0 it would be a hold); a hold is power 0 at its level; an export slot sells."""
    rows, notes = [], []
    rated = _limit(compiled, "max_discharge_w")
    for i, (k, start, hold) in enumerate(_ring(slots, 6)):
        s = slots[k]
        if hold:  # a charge's early end: a program of its own that holds what was charged (power 0, no grid charge)
            rows.append({"program": i + 1, "time": f"{start // 60:02d}:{start % 60:02d}", "power_w": 0, "soc_pct": round(s["soc_target_pct"]),
                         "grid_charge": False, "gen_charge": False, "sell": False, "_slots": [k]})
            continue
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
    return rows, [], notes + _charge_end_notes("deye", slots)


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


def _huawei_levels(slots: list[dict]) -> tuple[float | None, float | None]:
    """Huawei's one grid-charge cut-off (the highest grid-charge target) and one discharge cut-off (the lowest floor of a discharging slot)."""
    return (max((s["soc_target_pct"] for s in slots if s["mode"] == "charge_grid"), default=None),
            min((s["soc_floor_pct"] for s in slots if s["mode"] in DISCHARGING), default=None))


def _discharge_floor(slots: list[dict], compiled: dict) -> float:
    """A brand's one floor for every discharge (Sungrow's Min SoC before its 50 % limit, Voltronic's back-to-utility SoC, Solis's overdischarge
    SoC): the lowest floor of a discharging slot, else the fallback floor."""
    return min((s["soc_floor_pct"] for s in slots if s["mode"] in DISCHARGING), default=_min_floor(compiled))


def _growatt_levels(slots: list[dict], compiled: dict) -> tuple[bool, float | None, float, float]:
    """Growatt's global settings: the grid-charge flag, the Battery First stop SoC, the Grid First minimum SoC and the Load First floor."""
    charge = [s for s in slots if s["mode"].startswith("charge")]
    grid = any(s["mode"] == "charge_grid" for s in slots)
    stop = max((s["soc_target_pct"] for s in charge), default=max((s["soc_floor_pct"] for s in slots if s["mode"] == "hold"), default=None))
    exp_floor = min((s["soc_floor_pct"] for s in slots if s["mode"] == "export"), default=_min_floor(compiled))
    load_floor = min((s["soc_floor_pct"] for s in slots if s["mode"] in ("discharge", "self_use")), default=_min_floor(compiled))
    return grid, stop, exp_floor, load_floor


def _huawei(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    """Huawei SUN2000 with LUNA2000 in TOU mode (R6 3.2): a period is charge or discharge, every day (a day-table must be safe to repeat), and
    outside every period the battery does not discharge, so a hold (and a solar-only charge, with excess PV set to charge the battery) is a gap,
    not a period; the last period ends at 23:59. One grid-charge cut-off for the day (the highest grid-charge target), one discharge cut-off (the
    lowest floor of a discharging slot), the backup SoC at the fallback floor, and the plan's MDI cap as one capacity-control period every day
    (Huawei's peak-shaving primitive, `mdi_primitive`)."""
    kind = lambda s: "charge" if s["mode"] == "charge_grid" else "discharge" if s["mode"] in DISCHARGING else None
    wins = _windows(slots, kind)
    # a charge whose charging ends before its slot does: the period ends there, and the rest of the slot is a gap, which holds
    end = lambda s: s["charge_end"] if _inner_charge_end(s) else _end(s)
    rows = [{"period": i + 1, "start": w["first"]["start"], "end": end(w["last"]), "charge_or_discharge": w["kind"], "days": EVERY_DAY,
             "_slots": w["slots"]} for i, w in enumerate(wins)]
    charge = [s for s in slots if s["mode"] == "charge_grid"]
    dis = [s for s in slots if s["mode"] in DISCHARGING]
    cutoff, floor = _huawei_levels(slots)
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
    gaps = sorted([(_minutes(s["start"]), _span(s)) for s in slots if kind(s) is None]
                  + [(_minutes(s["charge_end"]), f"{s['charge_end']} to {_end(s)}") for s in slots if _inner_charge_end(s)])
    if gaps:
        notes.append(f"No period from {_joined([g for _, g in gaps])}: outside its periods Huawei does not discharge the battery, so it holds "
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
    floor = _discharge_floor(slots, compiled)
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
    return rows, settings, notes + _floor_notes("Sungrow", "Min SoC", min_soc, slots) + _charge_end_notes("sungrow", slots)


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
    grid, stop, exp_floor, load_floor = _growatt_levels(slots, compiled)
    settings = [_setting("charge_from_grid", "Battery First: charge from grid", grid),
                _setting("battery_first_stop_soc", "Battery First stop SoC", _pct(stop), "%"),
                _setting("grid_first_min_soc", "Grid First minimum SoC", _pct(exp_floor), "%"),
                _setting("load_first_stop_soc", "Load First: stop discharging at", _pct(load_floor), "%")]
    if grid and any(s["mode"] in ("hold", "charge_pv_only") for s in slots):
        notes.append("A hold or a solar-only charge is a Battery First window: while charging from the grid is on, it charges from the grid too.")
    return rows, settings, notes + _floor_notes("Growatt", "Load First floor", load_floor, [s for s in slots if s["mode"] != "export"]) + \
        _charge_end_notes("growatt", slots)


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
        if _inner_charge_end(s):  # the charge ends early: solar only from then, so the battery holds what it charged
            rows.append({"change": len(rows) + 1, "at": s["charge_end"], "output_priority": "SUB", "charger_priority": "OSO", "_slots": [k]})
    floor = _discharge_floor(slots, compiled)
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
    floor = _discharge_floor(slots, compiled)
    return rows, [_setting("overdischarge_soc", "Overdischarge SoC", _pct(floor), "%")], notes + _floor_notes("Solis", "overdischarge SoC", floor, slots) + \
        _charge_end_notes("solis", slots)


def _generic(slots: list[dict], compiled: dict) -> tuple[list, list, list]:
    """One row per slot; a charge whose charging ends before its slot does takes a hold row from its charge end where the table has rows to spare."""
    tails = _charge_tails(slots, BRANDS["other"]["max_slots"] - len(slots))
    rows = []
    for k, s in enumerate(slots):
        if k in tails:
            rows.append({"slot": len(rows) + 1, "start": s["start"], "end": s["charge_end"], "mode": s["mode"], "soc_pct": _soc_keyed(s), "_slots": [k]})
            rows.append({"slot": len(rows) + 1, "start": s["charge_end"], "end": s["end"], "mode": "hold", "soc_pct": round(s["soc_target_pct"]),
                         "_slots": [k]})
        else:
            rows.append({"slot": len(rows) + 1, "start": s["start"], "end": s["end"], "mode": s["mode"], "soc_pct": _soc_keyed(s), "_slots": [k]})
    return rows, [], _charge_end_notes("other", slots)


def _keyed_windows(slots: list[dict], kind_of, per_kind: int) -> list[str | None]:
    """Per slot, the kind of the brand window that keys it, or None where it has none (outside every window, or in a window past the brand's
    `per_kind` of that kind, which cannot be keyed)."""
    out: list[str | None] = [None] * len(slots)
    seen: dict[str, int] = {}
    for w in _windows(slots, kind_of):
        seen[w["kind"]] = seen.get(w["kind"], 0) + 1
        if seen[w["kind"]] <= per_kind:
            for k in w["slots"]:
                out[k] = w["kind"]
    return out


def as_keyed(brand: str, slots: list[dict], compiled: dict) -> list[dict]:
    """TWN-02 (plan Part 7.4 item 2): how the brand runs each slot of a day table a person keys, from the same register facts as its vendor table
    (R6 3.1 to 3.4; none bench-confirmed yet, DLV-00) and saying in fields what the table's notes say in words. Per slot, only the fields the brand
    runs differently from the plan ({} where it runs the slot as planned): `mode`, `soc_floor_pct`, `soc_target_pct`, `grid_charge` and
    `max_discharge_w` (None: the battery's own power, where the brand keys no per-slot power), `pv_full` (surplus PV charges the battery above the
    slot's target), and `charge_end_ts` None where the table cannot stop a charge before its slot ends (`_ends_keyed`). twin.slots_policy applies them.

    * Deye: a program's capacity stops a discharge and a grid charge, but surplus PV charges past it.
    * Huawei: one grid-charge cut-off and one discharge cut-off for the day; outside its periods it holds, and surplus PV charges the battery.
    * Sungrow: outside its two forced-charge windows it is self-consumption down to its one Min SoC (at most 50 %), at the battery's own power.
    * Growatt: a Battery First window charges (from the grid when the day's flag is on) to one stop SoC and does not discharge; Grid First exports
      to one floor; elsewhere, and in windows past its three of a kind, Load First serves the load to one floor.
    * Voltronic: the battery serves the load (SBU) down to one back-to-utility SoC; it has no export.
    * Solis: three charge and three discharge slots, discharging to one overdischarge SoC; self-use elsewhere.
    * Generic ("other"): as planned."""
    ov: list[dict] = [{} for _ in slots]
    if brand == "deye":
        for o, s in zip(ov, slots):
            if s["mode"] in ("hold", "charge_pv_only", "charge_grid"):
                o["pv_full"] = True
    elif brand == "huawei":
        cutoff, floor = _huawei_levels(slots)
        for o, s in zip(ov, slots):
            if s["mode"] == "charge_grid":
                o["soc_target_pct"] = cutoff
            elif s["mode"] in DISCHARGING:
                o.update(soc_floor_pct=floor, max_discharge_w=None)
            else:
                o.update(mode="hold", pv_full=True)
    elif brand == "sungrow":
        min_soc = min(_discharge_floor(slots, compiled), 50.0)
        windows = [k for k, s in enumerate(slots) if s["mode"] == "charge_grid"][:2]
        for k, (o, s) in enumerate(zip(ov, slots)):
            if k not in windows:
                o.update(mode="self_use", soc_floor_pct=min_soc, max_discharge_w=None)
    elif brand == "growatt":
        grid, stop, exp_floor, load_floor = _growatt_levels(slots, compiled)
        for o, s, kind in zip(ov, slots, _keyed_windows(slots, lambda s: _GROWATT.get(s["mode"]), 3)):
            if kind == "battery_first":
                o.update(mode="charge_grid" if grid else "charge_pv_only", grid_charge=grid, soc_target_pct=stop)
            elif kind == "grid_first":
                o["soc_floor_pct"] = exp_floor
            else:
                o.update(mode="self_use", soc_floor_pct=load_floor, max_discharge_w=None)
    elif brand == "voltronic":
        floor = _discharge_floor(slots, compiled)
        for o, s in zip(ov, slots):
            if s["mode"] in DISCHARGING:
                o.update(mode="self_use", soc_floor_pct=floor, max_discharge_w=None)
    elif brand == "solis":
        floor = _discharge_floor(slots, compiled)
        kind_of = lambda s: "charge" if s["mode"] == "charge_grid" else "discharge" if s["mode"] in ("discharge", "export") else None
        for o, s, kind in zip(ov, slots, _keyed_windows(slots, kind_of, 3)):
            if kind == "discharge":
                o["soc_floor_pct"] = floor
            elif kind is None:
                o.update(mode="self_use", soc_floor_pct=floor, max_discharge_w=None)
    keyed = _ends_keyed(brand, slots)
    for k, (o, s) in enumerate(zip(ov, slots)):
        if _inner_charge_end(s) and k not in keyed:
            o["charge_end_ts"] = None
    return [{key: v for key, v in o.items() if key == "charge_end_ts" or s.get(key) != v} for o, s in zip(ov, slots)]


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
    """The company's keys folder, <data folder>/keys/<its file's stem>: the owner's alone (0700), as each key in it is (0600; plan Part 2.8)."""
    root = platformdb.data_dir() / "keys"
    d = root / store.db_path().stem
    for folder in (root, d):
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt" and folder.stat().st_mode & 0o077:
            try:
                os.chmod(folder, 0o700)          # a folder made open before round 7
            except OSError:
                pass
    return d


def _write_key(path: Path, key: Ed25519PrivateKey) -> None:
    """The private key written owner-only from its first byte (plan Part 2.8: private keys in 0600 files; audit security-roles-11): the file is made
    with its mode (os.open, O_EXCL: never over a file there), never written open and narrowed after, where a refused chmod would leave it readable."""
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class KeyMissing(RuntimeError):
    """The site's signing key on record is not in the keys folder, or the file there is not that key (audit engine-delivery-13). It is never
    replaced on its own: a new key would sign plans that no edge given the old public key accepts. The keys folder is restored from the backup, or
    an administrator replaces the key (`replace_key`, POST /api/sites/{id}/edge/key) and the new public key is provisioned on the edge."""

    def __init__(self, site_id: str, key_id: str, why: str = "is missing from the keys folder"):
        self.site_id, self.key_id = site_id, key_id
        super().__init__(f"The site's signing key {key_id} {why}, so no plan is signed: restore the keys folder from the backup, or an administrator "
                         "replaces the key and the new public key is provisioned on the edge")


#: The site's key in force: its newest key not retired (one at most since round 7; a tie of two made at once before is broken by its id)
_IN_FORCE = "SELECT key_id, public_key, private_path FROM site_keys WHERE site_id = ? AND retired IS NULL ORDER BY created DESC, key_id DESC LIMIT 1"


def _public_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _key_file(kept: str) -> Path:
    """Where a key kept as `kept` is. A key is kept by its file name in the company's keys folder, which is read in today's data folder, so a moved
    or restored data folder still finds it (audit engine-delivery-13). A key kept before round 7 holds the absolute path of its time (a Windows one
    on a Linux host, say): its name is read in today's keys folder, and the old path only while the file is not there."""
    name = PureWindowsPath(kept).name                 # a Windows path or a POSIX one: both separators are taken
    here = _key_dir() / name
    return here if here.exists() or name == kept else Path(kept)


def _load(site_id: str, row) -> Ed25519PrivateKey:
    """The private key of a site_keys row; KeyMissing when its file is not there, does not read, or is not the key whose public half is on record."""
    try:
        key = serialization.load_pem_private_key(_key_file(row["private_path"]).read_bytes(), password=None)
    except FileNotFoundError:
        raise KeyMissing(site_id, row["key_id"]) from None
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
        raise KeyMissing(site_id, row["key_id"], "in the keys folder does not read") from None
    if not isinstance(key, Ed25519PrivateKey) or _public_b64(key) != row["public_key"]:
        raise KeyMissing(site_id, row["key_id"], "in the keys folder is not the key on record")
    return key


def _new_key(con, site_id: str) -> tuple[str, Ed25519PrivateKey]:
    """A new key for the site, written to the keys folder and recorded (by its file name, never a path) in the caller's write transaction."""
    key = Ed25519PrivateKey.generate()
    kid = store.new_id(8)
    name = f"{site_id}-{kid}.pem"
    _write_key(_key_dir() / name, key)
    con.execute("INSERT INTO site_keys (site_id, key_id, public_key, private_path, created) VALUES (?,?,?,?,?)",
                (site_id, kid, _public_b64(key), name, store._now()))
    return kid, key


def _site_key(site_id: str) -> tuple[str, Ed25519PrivateKey]:
    """The site's signing key in force. The first signing makes it, once however many ask at once (the check and the insert are one write
    transaction, so a second caller waits and reads the first one's key); after that it is never made again on its own: a key on record whose file
    is missing, or is not that key, raises KeyMissing (audit engine-delivery-13)."""
    with store.connect() as con:
        r = con.execute(_IN_FORCE, (site_id,)).fetchone()
    if r is None:
        with store.write_transaction() as con:
            r = con.execute(_IN_FORCE, (site_id,)).fetchone()
            if r is None:
                return _new_key(con, site_id)
    return r["key_id"], _load(site_id, r)


def public_key(site_id: str) -> dict:
    """The site's public key in force, {key_id, alg, public_key, missing}: the key on record (the one its edge was given), with `missing` true when
    its private key is not in the keys folder or is not that key, so nothing is signed until it is restored or replaced. The first call makes the
    key, as the first signing does."""
    with store.connect() as con:
        r = con.execute(_IN_FORCE, (site_id,)).fetchone()
    if r is None:
        kid, key = _site_key(site_id)
        return {"key_id": kid, "alg": "Ed25519", "public_key": _public_b64(key), "missing": False}
    try:
        _load(site_id, r)
        missing = False
    except KeyMissing:
        missing = True
    return {"key_id": r["key_id"], "alg": "Ed25519", "public_key": r["public_key"], "missing": missing}


def replace_key(site_id: str, by: str, reason: str) -> tuple[dict, list[str]]:
    """A new signing key for the site, every key in force retired (plan Part 2.8: on a lost key and on a handover): an administrator's action, in
    the audit trail with its reason. A retired key's file is kept, so the plan messages it signed still verify for a dispute. The edge refuses the
    plans signed with the new key until its public key is provisioned there. Returns the new public key and the key ids retired."""
    with store.write_transaction() as con:
        retired = [r["key_id"] for r in con.execute("SELECT key_id FROM site_keys WHERE site_id = ? AND retired IS NULL ORDER BY created, key_id",
                                                    (site_id,))]
        con.execute("UPDATE site_keys SET retired = ? WHERE site_id = ? AND retired IS NULL", (store._now(), site_id))
        kid, key = _new_key(con, site_id)
        store.audit(by, "replaced the site's signing key", site_id, {"key_id": kid, "retired": retired, "reason": reason}, con=con)
    return {"key_id": kid, "alg": "Ed25519", "public_key": _public_b64(key), "missing": False}, retired


def canonical(body: dict) -> bytes:
    """The bytes a plan message's signature covers: the RFC 8785 canonical JSON of the message without `signature` (contract.canonical_json), which
    an edge in any language reproduces from the message as it parses it (audit engine-delivery-22)."""
    return _jcs(body)


def _legacy_canonical(body: dict) -> bytes:
    """What a plan message was signed over before round 7 (Python's json.dumps, which only a Python edge reproduces): read only by `verify`, so a
    message stored before still verifies for a dispute."""
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()


def _plain(v):
    """A value as the JSON the API sends of it: a numpy scalar is its number, a datetime or other object its text (as signed before)."""
    return v.item() if isinstance(v, np.generic) else str(v)


def sign(site_id: str, body: dict) -> dict:
    """The message signed with the site's key in force. The body is first made the plain JSON it is sent as (a numpy number its number, a tuple a
    list), so the message returned, stored and sent is the very value whose canonical bytes were signed."""
    kid, key = _site_key(site_id)
    plain = json.loads(json.dumps(body, default=_plain, allow_nan=False))
    return {**plain, "signature": {"alg": "Ed25519", "key_id": kid, "value": base64.b64encode(key.sign(canonical(plain))).decode()}}


def verify(message: dict, public_b64: str) -> bool:
    """Whether the message's signature is the key's over its canonical bytes (or, for a message signed before round 7, over Python's json.dumps)."""
    body = {k: v for k, v in message.items() if k != "signature"}
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        sig = base64.b64decode(message["signature"]["value"])
    except Exception:
        return False
    for form in (canonical, _legacy_canonical):
        try:
            pub.verify(sig, form(body))
            return True
        except Exception:
            continue
    return False


# ------------------------------------------------------------------------------------------------ ems-edge/1.0 messages (strict; the one validator)

# The message models live in contract.py (plan DAT-02), with the JSON Schema files generated from them; they are named here as before
from .contract import (Ack, AckItem, Capabilities, Device, Event, EventBatch, Heartbeat, PlanMessage, TelemetryBatch,  # noqa: E402,F401
                       INVERTER_ROLES)


def latest_capabilities(site_id: str) -> dict | None:
    """The site's current capabilities message (the newest by its sent_at, whatever its sequence: routes_edge.edge_caps), with `seq`; None when the
    edge has sent none."""
    with store.connect() as con:
        r = con.execute("SELECT seq, body FROM capabilities WHERE site_id = ? ORDER BY sent_ts IS NULL, sent_ts DESC, received_at DESC LIMIT 1",
                        (site_id,)).fetchone()
    if r is None:
        return None
    body = json.loads(r["body"])
    return {**body, "seq": int(r["seq"])}


def targets(caps: dict | None) -> list[dict]:
    """The devices a plan targets (U3 5.4 `targets`): the inverters the capabilities name."""
    return [{"device_id": d["device_id"]} for d in (caps or {}).get("devices") or [] if d.get("role") in INVERTER_ROLES and d.get("device_id")]


class NotWritable(ValueError):
    """A table the device's capabilities cannot take as it stands (more slots than its slot model holds): it is not signed (R6 C1)."""


def fit_to_capabilities(days: list[dict], caps: dict | None) -> tuple[list[dict], dict | None]:
    """The Mode A days as the target inverter can write them (R6 C1, brief: Product 1 compiles only what the device reports writable; audit
    brief-vs-build-12). From the first inverter in the current capabilities that names its primitives: a grid-charge, generator-charge or export flag
    the device cannot write is dropped (the slot runs without it), a power limit is held to battery_power_w's range, and a floor or target to its
    primitive's range. A day with more slots than the device's slot model holds raises NotWritable. A device that names no primitives (an edge that
    reports none) gets the table unchanged, as before. Returns the days and what was fitted ({device_id, capabilities_seq, not_writable, clamped}),
    or None when nothing changed."""
    dev = next((d for d in (caps or {}).get("devices") or [] if d.get("role") in INVERTER_ROLES and d.get("primitives")), None)
    if dev is None:
        return days, None
    prims, model = dev["primitives"], dev.get("slot_model") or {}
    dropped, clamped = set(), set()

    def held(s: dict, key: str, lo, hi, name: str) -> None:
        v = s.get(key)
        if v is None:
            return
        x = min(max(float(v), lo if lo is not None else float(v)), hi if hi is not None else float(v))
        if x != float(v):
            s[key] = round(x) if isinstance(v, int) else x
            clamped.add(name)

    out = []
    for day in days:
        cap = model.get("max_slots")
        if cap and len(day.get("slots") or []) > int(cap):
            raise NotWritable(f"The table of {day.get('date')} has {len(day['slots'])} slots and the device {dev['device_id']} takes {int(cap)}")
        slots = []
        for s in day.get("slots") or []:
            s = dict(s)
            for flag, prim in (("grid_charge", "grid_charge_allowed"), ("gen_charge", "gen_charge_allowed"), ("export_allowed", "export_limit_w")):
                if s.get(flag) and prim not in prims:
                    s[flag] = False
                    dropped.add(prim)
            power = prims.get("battery_power_w")
            if power is not None:
                hi, lo = power.get("max"), power.get("min")
                held(s, "max_discharge_w", None, hi, "battery_power_w")
                held(s, "max_charge_w", None, abs(lo) if lo is not None else None, "battery_power_w")
            for key in ("soc_floor_pct", "soc_target_pct"):
                r = prims.get(key)
                if r is not None:
                    held(s, key, r.get("min"), r.get("max"), key)
            slots.append(s)
        out.append({**day, "slots": slots})
    if not dropped and not clamped:
        return days, None
    return out, {"device_id": dev["device_id"], "capabilities_seq": (caps or {}).get("seq"), "not_writable": sorted(dropped), "clamped": sorted(clamped)}


#: The slot modes that avoid the peak by discharging: those R6 C10 widens when the inverter clock is off
PEAK_MODES = ("discharge", "export")


def widen_peak_slots(days: list[dict], minutes: int = 15, min_slot_minutes: int = 15) -> list[dict]:
    """The Mode A days with every discharge or export slot started `minutes` early and ended `minutes` late (R6 C10: an inverter clock more than 300 s
    off shifts every boundary; being early costs a little stored energy, being late costs the peak tariff; audit engine-delivery-10). The slots beside
    it give up that time (never more than they hold), and one left shorter than the device's shortest slot (`min_slot_minutes`) gives the rest of its
    time to the discharge beside it. A charge slot's charge_end is kept within the slot. The local HH:MM times follow the new boundaries."""
    step = int(minutes) * 60
    hhmm = lambda t: datetime.fromtimestamp(int(t), PKT).strftime("%H:%M")
    out = []
    for day in days:
        slots = sorted((dict(s) for s in day.get("slots") or []), key=lambda s: s["start_ts"])
        peak = [s.get("mode") in PEAK_MODES for s in slots]
        if not any(peak):
            out.append(day)
            continue
        for i, s in enumerate(slots):
            if not peak[i]:
                continue
            if i > 0 and not peak[i - 1]:
                s["start_ts"] = max(slots[i - 1]["start_ts"], s["start_ts"] - step)
                slots[i - 1]["end_ts"] = s["start_ts"]
            if i + 1 < len(slots) and not peak[i + 1]:
                s["end_ts"] = min(slots[i + 1]["end_ts"], s["end_ts"] + step)
                slots[i + 1]["start_ts"] = s["end_ts"]
        kept: list[tuple[dict, bool]] = []
        for i, s in enumerate(slots):
            if not peak[i] and s["end_ts"] - s["start_ts"] < int(min_slot_minutes) * 60:
                if i + 1 < len(slots) and peak[i + 1]:
                    slots[i + 1]["start_ts"] = s["start_ts"]
                elif kept:
                    kept[-1][0]["end_ts"] = s["end_ts"]
                elif i + 1 < len(slots):
                    slots[i + 1]["start_ts"] = s["start_ts"]
                else:
                    kept.append((s, peak[i]))
                continue
            kept.append((s, peak[i]))
        new = []
        for s, _ in kept:
            s["start"], s["end"] = hhmm(s["start_ts"]), hhmm(s["end_ts"])
            if s.get("charge_end_ts") is not None and s["charge_end_ts"] > s["end_ts"]:
                s["charge_end_ts"] = s["end_ts"]
                if "charge_end" in s:
                    s["charge_end"] = hhmm(s["end_ts"])
            new.append(s)
        out.append({**day, "slots": new})
    return out
