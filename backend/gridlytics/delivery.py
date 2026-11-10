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
from . import store

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
                   mdi_cap_kw: float | None = None) -> dict:
    """Slots per local day: merge equal intents, fold runs shorter than the brand's minimum into a neighbour, then drop the least valuable
    boundaries (the smallest energy moved) until the day fits the brand's slot count."""
    gram = BRANDS.get(brand, BRANDS["other"])
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
                          "grid_cap_w": (round((mdi_cap_kw if mode.startswith("charge") else max(grid_cap, mdi_cap_kw)) * 1000) if mdi_cap_kw
                                         else (round(grid_cap * 1000) if mode == "charge_grid" else None)),
                          "gen_charge": False, "export_allowed": mode == "export",
                          "genset": any(st["genset_on"] for st in steps[r["start"]:r["end"]]) or None,
                          "intent": _intent_words(mode, floor, target)})
        days.append({"date": day, "slots": slots})
    return {"brand": brand, "grammar": gram, "days": days, "fallback": {"on_expiry": "inverter_native", "min_soc_floor_pct": fallback_floor_pct,
                                                                        "revert_after_hours": FALLBACK_HOURS}}


def _intent_words(mode: str, floor: float, target: float) -> str:
    return {"discharge": f"Use the battery for the load, not below {floor:.0f} %",
            "export": f"Discharge to the grid, not below {floor:.0f} %",
            "hold": f"Hold the battery at {floor:.0f} % (no discharge)",
            "charge_grid": f"Charge from the grid to {target:.0f} %",
            "charge_pv_only": f"Charge from solar only, up to {target:.0f} %",
            "self_use": f"Self-use, not below {floor:.0f} %"}.get(mode, mode)


def vendor_view(compiled: dict, day_index: int = 0) -> list[dict]:
    """The table as the installer keys it into the vendor's own screen (Deye's six programs, Huawei's periods, Growatt's windows)."""
    if not compiled.get("days"):
        return []
    day = compiled["days"][min(day_index, len(compiled["days"]) - 1)]
    brand = compiled["brand"]
    rows = []
    for k, s in enumerate(day["slots"]):
        if brand == "deye":
            rows.append({"program": k + 1, "time": s["start"], "power_w": s["max_discharge_w"] or s["max_charge_w"] or 0, "soc_pct": round(
                s["soc_target_pct"] if s["mode"].startswith("charge") else s["soc_floor_pct"]), "grid_charge": s["grid_charge"], "gen_charge": s["gen_charge"]})
        elif brand == "huawei":
            rows.append({"period": k + 1, "start": s["start"], "end": s["end"], "charge_or_discharge": "charge" if s["mode"].startswith("charge") else
                         "discharge" if s["mode"] in ("discharge", "export") else "hold"})
        else:
            rows.append({"slot": k + 1, "start": s["start"], "end": s["end"], "mode": s["mode"], "soc_pct": round(s["soc_floor_pct"])})
    return rows


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
