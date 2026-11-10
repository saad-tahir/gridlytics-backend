"""The digital twin (plan Part 7.4): a plant at 1-minute steps, the inverter's own logic, and the replay engine verification uses.

* `truth(site, day)` gives a site-day's PV, load, critical load and grid availability, deterministic in (site, day) so a demo site's history and its live
  minutes always agree, and so a re-score reproduces the same day.
* `simulate(...)` runs a per-minute policy (the inverter's native table, a compiled plan's Mode A slots, or a tuned table) over those series and returns
  what the meters would have recorded. The same function is the counterfactual engine: verification replays the native capture and the tuned table on the
  measured PV, load and outages and prices the result with the bill engine.
* Policies are per-minute arrays of the Mode A vocabulary: mode, floor and target state of charge, power caps, the grid-charge flag and the genset command.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import solar

PKT = timezone(timedelta(hours=5))
MODES = ("self_use", "discharge", "hold", "charge_grid", "charge_pv_only", "export")
MODE_CODE = {m: i for i, m in enumerate(MODES)}

#: hour-of-day load shapes (share of the site's peak) by site type, working day and off day
_SHAPES = {
    "office": ([.25] * 7 + [.45, .8, .95, 1, 1, .95, .95, 1, 1, .95, .8, .5, .35, .3, .28, .26, .25], [.25] * 24),
    "retail": ([.2] * 9 + [.4, .7, .85, .9, .95, 1, 1, 1, 1, 1, 1, .95, .8, .4, .25], [.2] * 9 + [.4, .75, .9, .95, 1, 1, 1, 1, 1, 1, 1, 1, .85, .45, .25]),
    "industrial_1_shift": ([.15] * 7 + [.5, .95, 1, 1, 1, .8, .95, 1, 1, .9, .4, .2, .15, .15, .15, .15, .15], [.15] * 24),
    "industrial_2_shift": ([.3] * 6 + [.6, .9, 1, 1, 1, .95, .85, .95, 1, 1, 1, .95, .9, .9, .85, .7, .45, .3], [.3] * 24),
    "industrial_3_shift": ([.85, .82, .8, .8, .8, .82, .9, .95, 1, 1, 1, .95, .9, .95, 1, 1, .98, .95, .92, .92, .9, .9, .88, .86],
                           [.6] * 24),
    "cold_store": ([.7, .68, .66, .65, .65, .66, .7, .75, .8, .85, .9, .95, 1, 1, 1, .98, .95, .9, .85, .8, .78, .76, .74, .72], None),
    "tower": ([.9] * 24, None),
    "hospital": ([.6, .58, .56, .55, .55, .58, .65, .8, .9, .95, 1, 1, 1, 1, 1, .95, .9, .88, .85, .8, .75, .7, .66, .62], None),
    "school": ([.1] * 7 + [.6, 1, 1, 1, 1, .9, .5, .2] + [.1] * 9, [.1] * 24),
    "farm": ([.2, .2, .2, .2, .2, .3, .6, .9, 1, 1, 1, .9, .9, 1, 1, .9, .8, .6, .5, .4, .3, .25, .2, .2], None),
}


def _seed(*parts) -> int:
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def day_start_utc(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())


def minutes_of(day: date) -> np.ndarray:
    t0 = day_start_utc(day)
    return np.arange(t0, t0 + 86400, 60, dtype=np.int64)


def peak_load_kw(site: dict) -> float:
    """The site's typical peak load: the onboarding figure where given, else 60 percent of the sanctioned load."""
    d = site.get("data", {})
    if d.get("load_peak_kw"):
        return float(d["load_peak_kw"])
    conns = site.get("connections") or []
    if conns:
        return 0.6 * float(conns[0]["data"].get("sanctioned_load_kw", 100))
    pv = (site.get("assets") or {}).get("pv")
    return 0.8 * float(pv["kw_ac"]) if pv else 50.0


def load_template(site: dict, ts_utc: np.ndarray) -> np.ndarray:
    """Stage-0 load (kW): the site type's weekly shape times its peak; Sunday is the off day."""
    st = site.get("data", {}).get("site_type", "industrial_2_shift")
    work, off = _SHAPES.get(st, _SHAPES["industrial_2_shift"])
    off = off or work
    loc = np.asarray(ts_utc) + 5 * 3600
    hour = (loc % 86400) / 3600.0
    dow = ((loc // 86400) + 3) % 7  # 1970-01-01 was a Thursday; 6 = Sunday
    h0 = np.floor(hour).astype(int) % 24
    frac = hour - np.floor(hour)
    w = np.array(work)
    o = np.array(off)
    shape_w = w[h0] * (1 - frac) + w[(h0 + 1) % 24] * frac
    shape_o = o[h0] * (1 - frac) + o[(h0 + 1) % 24] * frac
    shape = np.where(dow == 6, shape_o, shape_w)
    return shape * peak_load_kw(site)


def critical_share(site: dict) -> np.ndarray:
    crit = site.get("data", {}).get("critical_load_kw") or [0.0] * 24
    return np.array(crit, dtype=float)


def _feeder_profile(site: dict) -> dict:
    """Outage behaviour of the site's feeder for the twin: scheduled windows and the hourly trip hazard. Demo sites carry theirs in data.twin."""
    tw = site.get("data", {}).get("twin") or {}
    disco = site.get("data", {}).get("disco") or "LESCO"
    rural = disco in ("QESCO", "PESCO", "SEPCO", "HESCO", "TESCO")
    return {"windows": tw.get("windows", [["14:00", "16:00"]] if rural else []),
            "adherence": float(tw.get("adherence", 0.8)),
            "trips_per_day": float(tw.get("trips_per_day", 1.2 if rural else 0.35)),
            "trip_minutes": float(tw.get("trip_minutes", 75.0)),
            "cloudiness": float(tw.get("cloudiness", 0.0))}


def truth(site: dict, day: date) -> dict:
    """The site-day's real-world series at 1 minute: PV available (kW), load (kW), critical load (kW), grid present (0/1), air temperature."""
    ts = minutes_of(day)
    rng = np.random.default_rng(_seed(site["id"], day.isoformat()))
    month = day.month
    prof = _feeder_profile(site)
    # sky: a daily clearness drawn by season (monsoon and winter fog are cloudier), then minute noise on partly cloudy days
    base = {1: .78, 2: .82, 3: .86, 4: .9, 5: .9, 6: .84, 7: .7, 8: .72, 9: .84, 10: .9, 11: .86, 12: .76}[month]
    k_day = float(np.clip(rng.beta(8, 2) * (base / 0.8) - prof["cloudiness"], 0.15, 1.0))
    noise = np.zeros(len(ts))
    if k_day < 0.92:
        e = rng.normal(0, 1, len(ts))
        for i in range(1, len(ts)):
            noise[i] = 0.97 * noise[i - 1] + 0.243 * e[i]
        noise *= (1 - k_day) * 0.5
    clear = np.clip(k_day + noise, 0.05, 1.05)
    pv_asset = (site.get("assets") or {}).get("pv")
    lat, lon = site["data"].get("lat", 31.5), site["data"].get("lon", 74.3)
    pv = solar.pv_power(ts, lat, lon, pv_asset, clear, np.full(len(ts), month)) if pv_asset else np.zeros(len(ts))
    # load: template, a day-level factor and minute noise; Eid and public holidays are not modelled in v1's twin
    load = load_template(site, ts) * float(np.clip(rng.normal(1.0, 0.06), 0.8, 1.2))
    load = np.maximum(0.0, load * (1 + rng.normal(0, 0.03, len(ts))))
    hours = ((ts + 5 * 3600) % 86400) // 3600
    crit = critical_share(site)[hours]
    crit = np.minimum(crit, load)
    # grid: off-grid sites have none; else scheduled windows kept with the adherence rate and random trips by hour
    conns = site.get("connections") or []
    off_grid = not conns or conns[0]["data"].get("regime") == "off_grid"
    grid = np.zeros(len(ts)) if off_grid else np.ones(len(ts))
    if not off_grid:
        dow = ((ts[0] + 5 * 3600) // 86400 + 3) % 7
        if dow != 6:
            for w in prof["windows"]:
                if rng.random() < prof["adherence"]:
                    a = int(w[0][:2]) * 60 + int(w[0][3:])
                    b = int(w[1][:2]) * 60 + int(w[1][3:])
                    a += int(rng.integers(-10, 11))
                    b += int(rng.integers(-5, 25))
                    grid[max(0, a):min(1440, b)] = 0
        # summer evenings trip more often
        hazard = np.ones(24)
        hazard[17:23] = 2.5
        if month in (5, 6, 7, 8):
            hazard *= 1.6
        hazard = hazard / hazard.sum() * prof["trips_per_day"]
        for h in range(24):
            if rng.random() < hazard[h]:
                start = h * 60 + int(rng.integers(0, 60))
                dur = int(np.clip(rng.lognormal(np.log(prof["trip_minutes"]), 0.6), 5, 360))
                grid[start:min(1440, start + dur)] = 0
    temp = solar.air_temp(ts, np.full(len(ts), month - 1))
    return {"ts": ts, "pv": pv, "load": load, "crit": crit, "grid": grid, "temp": temp}


# ------------------------------------------------------------------------------------------------ policies

def native_policy(site: dict, ts: np.ndarray) -> dict:
    """The inverter's own table from the native capture (self-use with a backup floor when nothing was captured).

    * Slots apply whenever the capture has them (a Deye-style TOU program is captured under the default work mode too), each with its floor, target,
      grid-charge flag and charge power (`max_charge_kw`, the battery's own where none was captured); the twin charges from the grid towards the
      slot's target where the flag is set.
    * `backup` holds the battery while the grid is present (no discharge), charges it from the grid to the backup state of charge when grid charging is
      allowed, and discharges only in outages. Surplus PV still charges it to its maximum, as a backup-mode inverter does: the backup level is the
      grid-charge level (`grid_target`), not a ceiling.
    * `feed_in_first` exports surplus PV first (up to the export cap) and charges the battery from what is left.

    `grid_target` is the level a grid-charge flag charges to (the slot's target, except in `backup`); `target` caps every other charging."""
    cap = (site.get("native") or {}).get("body") or {"work_mode": "self_use", "backup_soc_pct": 20.0, "slots": []}
    n = len(ts)
    work = cap.get("work_mode") or "self_use"
    backup = float(cap.get("backup_soc_pct", 20.0)) / 100.0
    mode = np.full(n, MODE_CODE["self_use"])
    floor = np.full(n, backup)
    target = np.full(n, 1.0)
    max_ch = np.full(n, np.inf)
    grid_target = None
    gch = np.zeros(n, dtype=bool)
    export_first = np.full(n, work == "feed_in_first")
    slots = sorted(cap.get("slots") or [], key=lambda s: s["start"])
    if work == "backup":
        mode[:] = MODE_CODE["hold"]
        grid_target = np.full(n, backup)
        gch[:] = bool(cap.get("grid_charge_allowed", False))
    elif slots:
        minute = ((np.asarray(ts) + 5 * 3600) % 86400) // 60
        starts = [int(s["start"][:2]) * 60 + int(s["start"][3:]) for s in slots]
        for i in range(n):
            k = max((j for j, s0 in enumerate(starts) if s0 <= minute[i]), default=len(slots) - 1)
            s = slots[k]
            mode[i] = MODE_CODE.get(s["mode"], 0)
            floor[i] = s["soc_floor_pct"] / 100.0
            target[i] = s["soc_target_pct"] / 100.0
            gch[i] = bool(s.get("grid_charge", False))
            if s.get("max_charge_kw") is not None:  # the slot's captured charge power (round 7: the replay charged at the battery's own)
                max_ch[i] = float(s["max_charge_kw"])
    return {"mode": mode, "floor": floor, "target": target, "max_dis": np.full(n, np.inf), "max_ch": max_ch, "grid_charge": gch,
            "genset": np.full(n, -1), "export_first": export_first, "grid_target": target.copy() if grid_target is None else grid_target}


def slots_policy(slots: list[dict], ts: np.ndarray) -> dict:
    """A per-minute policy from compiled slots (each with start_ts and end_ts in epoch seconds); minutes outside every slot run self-use. The genset
    command follows each slot's `genset_runs` (the plan's own runs), so every replay of a plan's slots (the wrong-plan guard, the compare drawer, the
    demo, the fidelity check) runs the set when the plan does.

    Round 7: a slot's `as_keyed` (delivery.as_keyed, TWN-02) replaces the plan's fields with what the brand runs in their place where a person keys
    the table: a global floor or cut-off, the brand's own mode where it has no window for the slot, `pv_full` (surplus PV charges the battery above
    the slot's target) and `charge_end_ts` None where the table cannot stop a charge early. A charge slot whose charging ends before the slot does
    (`charge_end_ts`, kept where the table or an edge holds it) holds what it charged from then to its end: no grid charge, no discharge."""
    n = len(ts)
    pol = {"mode": np.full(n, MODE_CODE["self_use"]), "floor": np.full(n, 0.2), "target": np.full(n, 1.0), "max_dis": np.full(n, np.inf),
           "max_ch": np.full(n, np.inf), "grid_charge": np.zeros(n, dtype=bool), "genset": np.full(n, -1), "grid_cap": np.full(n, np.inf),
           "pv_full": np.zeros(n, dtype=bool)}
    for s0 in slots:
        m = (ts >= s0["start_ts"]) & (ts < s0["end_ts"])
        if not m.any():
            continue
        s = {**s0, **(s0.get("as_keyed") or {})}
        pol["mode"][m] = MODE_CODE.get(s["mode"], 0)
        pol["floor"][m] = s["soc_floor_pct"] / 100.0
        pol["target"][m] = s["soc_target_pct"] / 100.0
        if s.get("max_discharge_w") is not None:
            pol["max_dis"][m] = s["max_discharge_w"] / 1000.0
        if s.get("max_charge_w") is not None:
            pol["max_ch"][m] = s["max_charge_w"] / 1000.0
        pol["grid_charge"][m] = bool(s.get("grid_charge"))
        pol["pv_full"][m] = bool(s.get("pv_full"))
        if s.get("grid_cap_w"):
            pol["grid_cap"][m] = s["grid_cap_w"] / 1000.0
        ce = s.get("charge_end_ts")
        if ce and str(s["mode"]).startswith("charge") and int(s0["start_ts"]) < int(ce) < int(s0["end_ts"]):
            tail = m & (ts >= int(ce))
            pol["mode"][tail] = MODE_CODE["hold"]
            pol["grid_charge"][tail] = False
            pol["floor"][tail] = np.maximum(pol["floor"][tail], pol["target"][tail])  # what it charged is held, also against a grid cap
        if s.get("genset_runs") is not None:
            # the plan's own runs (round 7): 1 in a run, 0 elsewhere in the slot ("no planned run", under which the operator still rescues a
            # site about to go dark), as the demo has always set it per step; a slot stored before carries only `genset`, for the whole slot
            pol["genset"][m] = 0
            for a, b in s["genset_runs"]:
                pol["genset"][m & (ts >= a) & (ts < b)] = 1
        elif s.get("genset") is not None:
            pol["genset"][m] = 1 if s["genset"] else 0
    return pol


def repeated_slots(slots: list[dict], t_from: int, t_to: int) -> list[dict]:
    """One day's compiled slots repeated by clock time over [t_from, t_to), as a keyed table runs with no new plan (Part 7.1 step 6): the same
    slots every day, and the clock minutes a part-day table leaves uncovered run its last slot, as a ring's last program wraps round. A keyed
    table holds no genset runs, so the operator's own rule runs the set."""
    keep = {"genset": None, "genset_runs": None}
    shift = lambda s, k: {"charge_end_ts": int(s["charge_end_ts"]) + k * 86400} if s.get("charge_end_ts") else {}
    out = [{**slots[-1], **keep, "start_ts": int(t_from), "end_ts": int(t_to), "charge_end_ts": None}]
    first = int(slots[0]["start_ts"])
    for k in range((int(t_from) - first) // 86400 - 1, (int(t_to) - first) // 86400 + 2):
        out += [{**s, **keep, "start_ts": int(s["start_ts"]) + k * 86400, "end_ts": int(s["end_ts"]) + k * 86400, **shift(s, k)} for s in slots]
    return out


#: what a site runs, minute by minute, when a plan's messages stop (fallback_policy): the plan; the cached plan while an edge has lost its link
#: (the site is flagged unattended); the inverter's native settings (a plan expired at an edge, or Mode B's setpoints reverted); a keyed table
#: repeating its day with no new plan
FALLBACK_STATES = ("plan", "unattended", "native", "repeat")


def _merged(a: dict, b: dict, use_b: np.ndarray) -> dict:
    """Policy `b` where `use_b`, else `a`, over the keys of both (a key one side lacks takes its neutral value)."""
    n = len(use_b)
    neutral = {"grid_cap": np.full(n, np.inf), "export_first": np.zeros(n, dtype=bool), "export_cap": np.full(n, np.inf),
               "pv_full": np.zeros(n, dtype=bool)}
    pick = lambda pol, k: pol.get(k, pol["target"] if k == "grid_target" else neutral.get(k))
    return {k: np.where(use_b, pick(b, k), pick(a, k)) for k in set(a) | set(b) if pick(a, k) is not None and pick(b, k) is not None}


def _whole_day(days: list[dict]) -> dict:
    """The last day table that covers a whole local day (a keyed table's day), else the last one."""
    for d in reversed(days):
        t0 = day_start_utc(date.fromisoformat(d["date"]))
        if d["slots"] and int(d["slots"][0]["start_ts"]) == t0 and int(d["slots"][-1]["end_ts"]) == t0 + 86400:
            return d
    return days[-1]


def fallback_policy(site: dict, compiled: dict, ts: np.ndarray, *, valid_until: int, executor: str = "edge", mode: str = "a",
                    link_lost_at: int | None = None, revert_after_s: int = 1800, keyed_day: int | None = None) -> tuple[dict, np.ndarray]:
    """The per-minute policy a site runs when a plan's messages stop, and its state (an index into FALLBACK_STATES), so the twin can replay an
    expiry or a lost link (plan Part 7.1 step 6 and 7.2, U3 3.3 and 3.4, R6 I2, I3, I5 and I6; tracker DLV-04):

    * An edge with a Mode A plan (`executor` "edge"): `fallback.on_link_loss_after_s` after the link drops it flags the site unattended and keeps
      executing the cached plan to `valid_until`; from then on it runs `fallback.on_expiry`, the inverter's native settings, with the block's
      `min_soc_floor_pct` as the floor everywhere and `max_export_w_floor` as the export cap (`export_cap`, kW).
    * Mode B (`mode` "b"): the volatile setpoints revert `revert_after_s` (mode_b's refresh) after the link drops, to the same native fallback,
      or at `valid_until` if that comes first. The minutes before run the compiled slots (the twin has no Mode B executor yet).
    * A table a person keyed (`executor` "keyed", the advisory tier): no edge, so nothing expires; from `valid_until` the keyed day's table
      (`keyed_day`, by default the last whole day) repeats by clock time, the case delivery.table_safety checks."""
    fb = compiled.get("fallback") or {}
    days = [d for d in compiled.get("days") or [] if d.get("slots")]
    n = len(ts)
    pol = slots_policy([s for d in days for s in d["slots"]], ts)
    state = np.zeros(n, dtype=int)
    if executor == "keyed":
        rep = ts >= int(valid_until)
        if rep.any() and days:
            day = days[keyed_day] if keyed_day is not None else _whole_day(days)
            pol = _merged(pol, slots_policy(repeated_slots(day["slots"], int(valid_until), int(ts[-1]) + 60), ts), rep)
            state[rep] = FALLBACK_STATES.index("repeat")
        return pol, state
    end = int(valid_until)
    if link_lost_at is not None:
        if mode == "b":
            end = min(end, int(link_lost_at) + int(revert_after_s))
        state[(ts >= int(link_lost_at) + int(fb.get("on_link_loss_after_s", 300))) & (ts < end)] = FALLBACK_STATES.index("unattended")
    native = ts >= end
    if native.any():
        nat = native_policy(site, ts)
        nat["floor"] = np.maximum(nat["floor"], float(fb.get("min_soc_floor_pct") or 0.0) / 100.0)
        cap = fb.get("max_export_w_floor")
        nat["export_cap"] = np.full(n, np.inf if cap is None else float(cap) / 1000.0)
        pol = _merged(pol, nat, native)
        state[native] = FALLBACK_STATES.index("native")
    return pol, state


def state_changes(ts: np.ndarray, state: np.ndarray) -> list[dict]:
    """The minutes a fallback_policy state begins, as {t, state}: the fallback events a replay counts."""
    return [{"t": int(ts[i]), "state": FALLBACK_STATES[int(state[i])]} for i in range(len(state)) if i == 0 or state[i] != state[i - 1]]


def table_policy(table: list[dict], ts: np.ndarray) -> dict:
    """A static daily table (start "HH:MM", mode, floor, target, grid_charge) repeated every day: the tuned baseline's shape."""
    cap = {"work_mode": "tou", "backup_soc_pct": 20.0, "slots": table}
    return native_policy({"native": {"body": cap}}, ts)


# ------------------------------------------------------------------------------------------------ the plant

@dataclass
class Plant:
    e_nom: float = 0.0            # usable nameplate x SoH, kWh
    soc_min: float = 0.1
    soc_max: float = 0.95
    p_ch: float = 0.0
    p_dis: float = 0.0
    eta_c: float = 0.95
    eta_d: float = 0.95
    g_max: float = 1e9
    x_max: float = 0.0
    gen_kw: float = 0.0
    gen_fmin: float = 0.3
    gen_a: float = 0.273
    gen_b: float = 0.033
    gen_control: str = "none"     # auto_on_grid_loss | soc_threshold | manual | none
    gen_min_run: int = 30         # minutes
    gen_soc_on: float = 0.25
    load_mode: int = 0
    parallel: bool = False


def plant_of(site: dict) -> Plant:
    a = site.get("assets") or {}
    b, g = a.get("battery"), a.get("genset")
    conns = site.get("connections") or []
    c = conns[0]["data"] if conns else {"regime": "off_grid", "sanctioned_load_kw": 0, "export_cap_kw": 0}
    d = site.get("data", {})
    p = Plant()
    if b:
        p.e_nom = b["nameplate_kwh"] * b.get("soh", 1.0)
        p.soc_min, p.soc_max = b["soc_min"], b["soc_max"]
        p.p_ch, p.p_dis, p.eta_c, p.eta_d = b["p_ch_kw"], b["p_dis_kw"], b["eta_c"], b["eta_d"]
    p.g_max = float(c.get("sanctioned_load_kw") or 0) * 1.25 if c.get("regime") != "off_grid" else 0.0
    p.x_max = float(c.get("export_cap_kw") or 0) if c.get("regime") in ("net_billing", "net_metering_legacy") else 0.0
    if g:
        p.gen_kw, p.gen_fmin, p.gen_a, p.gen_b = g["rated_kw"], g["f_min"], g["a"], g["b"]
        p.gen_control = d.get("genset_start_control", "manual")
        p.gen_min_run = int(round(g.get("min_up_h", 1.0) * 60))
        p.parallel = bool(g.get("parallel_allowed"))
    p.gen_soc_on = (d.get("reserve_policy") or {}).get("min_soc_pct", 20.0) / 100.0 + 0.05
    p.load_mode = int(d.get("outage_load_mode", 0))
    return p


def simulate(series: dict, policy: dict, plant: Plant, soc0: float, gen_on0: bool = False, gen_elapsed0: int = 0, outage_min0: int = 0,
             gen_rescue0: bool = False) -> dict:
    """Run the plant minute by minute. Signs as OpenEMS: battery discharge positive, charge negative; grid import positive.

    The genset command per minute (manual start control): 1 a planned run, 0 no planned run (a planned run's tail stops after its minimum run; the operator
    still starts the genset when the lights would go out, and such a rescue run stops once the battery is charged or the grid returns), -1 the operator's own
    rule (the native baseline). `gen_elapsed0`, `outage_min0` and `gen_rescue0` carry the state of a run cut into several calls (the live demo's ticks);
    the result's `gen_elapsed_end`, `outage_min_end` and `gen_rescue_end` are what the next call takes."""
    pv_av, load, crit, grid = series["pv"], series["load"], series["crit"], series["grid"]
    n = len(pv_av)
    dt = 1.0 / 60.0
    out = {k: np.zeros(n) for k in ("imp", "exp", "bat", "soc", "gen", "fuel", "unserved", "curtail", "served", "pv")}
    out["gen_on"] = np.zeros(n, dtype=bool)
    e = soc0 * plant.e_nom
    e_min, e_max = plant.soc_min * plant.e_nom, plant.soc_max * plant.e_nom
    gen_on, elapsed = gen_on0, gen_elapsed0
    rescue = bool(gen_on0 and gen_rescue0)
    outage_min = int(outage_min0)
    mode, floor, target = policy["mode"], policy["floor"], policy["target"]
    grid_target = policy.get("grid_target")
    if grid_target is None:
        grid_target = target
    max_dis, max_ch, gch, gcmd = policy["max_dis"], policy["max_ch"], policy["grid_charge"], policy["genset"]
    gcap = policy.get("grid_cap", np.full(n, np.inf))
    xcap = policy.get("export_cap", np.full(n, np.inf))  # a fallback block's export floor (R6 I3), where a policy carries one
    export_first = policy.get("export_first")
    if export_first is None:
        export_first = np.zeros(n, dtype=bool)
    pv_full = policy.get("pv_full")  # surplus PV charges the battery above the slot's target (a brand profile, twin.slots_policy)
    if pv_full is None:
        pv_full = np.zeros(n, dtype=bool)
    has_bat = plant.e_nom > 0
    for i in range(n):
        g = grid[i] > 0.5
        pv = pv_av[i]
        dis_room = max(0.0, (e - max(e_min, floor[i] * plant.e_nom))) * plant.eta_d / dt if has_bat else 0.0
        dis_room_abs = max(0.0, e - e_min) * plant.eta_d / dt if has_bat else 0.0
        ch_room = max(0.0, (min(e_max, target[i] * plant.e_nom) - e)) / plant.eta_c / dt if has_bat else 0.0
        ch_room_abs = max(0.0, e_max - e) / plant.eta_c / dt if has_bat else 0.0
        pdis = min(plant.p_dis, max_dis[i]) if has_bat else 0.0
        pch = min(plant.p_ch, max_ch[i]) if has_bat else 0.0
        bat = gen = unserved = curtail = 0.0
        if g:
            outage_min = 0
            served = load[i]
            net = served - pv
            m = mode[i]
            # the genset with the grid present: a unit that may not run in parallel stops on grid return; a parallel unit runs out its minimum run and then
            # stops unless a plan asks for a parallel run (which also starts it)
            if gen_on:
                if not plant.parallel or not (gcmd[i] == 1 or elapsed < plant.gen_min_run):
                    gen_on, elapsed, rescue = False, 0, False
                elif gcmd[i] == 1:
                    rescue = False
            elif plant.parallel and plant.gen_kw > 0 and plant.gen_control != "none" and gcmd[i] == 1:
                gen_on, elapsed, rescue = True, 0, False
            # a grid-charge flag in a self-use or hold slot charges from the grid towards its grid target (the slot's target; the backup level in backup
            # mode), and the battery then holds at that level; surplus PV still charges it up to the target
            grid_ch = has_bat and bool(gch[i]) and m in (0, 2)
            dis_room_m = dis_room
            ch_room_g = ch_room
            if grid_ch:
                dis_room_m = max(0.0, (e - max(e_min, max(floor[i], grid_target[i]) * plant.e_nom))) * plant.eta_d / dt
                ch_room_g = max(0.0, (min(e_max, grid_target[i] * plant.e_nom) - e)) / plant.eta_c / dt
            if m == 3 or (grid_ch and ch_room_g > 1e-9):  # charge_grid, never above the slot's grid import cap (the peak-shaving setting)
                bat = -max(0.0, min(pch, ch_room if m == 3 else ch_room_g, gcap[i] - net if net > 0 else gcap[i] + (-net)))
                if m != 3 and net < 0:  # surplus PV beyond the grid-charge level still charges, up to the target
                    bat = -max(-bat, min(-net, pch, ch_room_abs if pv_full[i] else ch_room))
                elif pv_full[i] and net < 0:
                    bat = -max(-bat, min(-net, pch, ch_room_abs))
            elif m == 5:
                # export: discharge at the slot's power, but no more than the load and the export cap take (round 7: an inverter does not discharge
                # into curtailment); surplus PV beyond the cap charges the battery instead
                room = net + min(plant.x_max, xcap[i])
                bat = min(pdis, dis_room, room) if room > 0 else -min(-room, pch, ch_room_abs)
            elif net > 0:
                if m in (0, 1):
                    bat = min(net, pdis, dis_room_m)
            elif export_first[i]:  # feed-in first: surplus PV to the grid up to the cap, the rest into the battery
                rest = max(0.0, -net - plant.x_max)
                bat = -min(rest, pch, ch_room_abs if m in (0, 1) else ch_room)
            else:
                bat = -min(-net, pch, ch_room_abs if m in (0, 1) or pv_full[i] else ch_room)
            flow = served - pv - bat
            if flow > gcap[i] and has_bat:
                # peak shaving to the slot's grid cap (the plan's MDI cap where something on site holds it), never below the slot's floor (round 7:
                # the BMS floor before, so a load above the forecast drained the level a hold keeps for an outage): the floor wins
                extra = max(0.0, min(flow - gcap[i], plant.p_dis - max(bat, 0.0), dis_room_m - max(bat, 0.0)))
                bat += extra
                flow -= extra
            if gen_on:
                gen = max(plant.gen_fmin * plant.gen_kw, min(plant.gen_kw, flow))
                flow -= gen
            if flow > 0:
                imp = min(flow, plant.g_max)
                unserved = flow - imp
                out["imp"][i] = imp
            else:
                exp = min(-flow, plant.x_max, xcap[i])
                curtail = -flow - exp
                out["exp"][i] = exp
                if curtail > pv and bat > 0:  # battery output nobody takes is never discharged, and never booked as PV curtailment
                    back = min(curtail - pv, bat)
                    bat -= back
                    curtail -= back
                if curtail > pv and gen > 0:  # genset output nobody takes is not PV curtailment: the genset runs below its minimum load instead
                    over = min(curtail - pv, gen)
                    gen -= over
                    curtail -= over
        else:
            outage_min += 1
            served_full = load[i]
            # who starts the genset
            want = gen_on
            if plant.gen_kw > 0:
                if plant.gen_control == "auto_on_grid_loss":
                    want = outage_min >= 1
                elif plant.gen_control == "soc_threshold":
                    soc_now = e / max(plant.e_nom, 1e-9)
                    want = (gen_on and soc_now < min(0.9, plant.gen_soc_on + 0.5)) or soc_now < plant.gen_soc_on or not has_bat
                elif plant.gen_control == "manual":
                    # the lights would go out: the battery is at its floor (or there is none) ten minutes into the outage
                    at_floor = (not has_bat) or e <= e_min + 0.02 * plant.e_nom
                    lights_out = at_floor and outage_min >= 10
                    full = has_bat and e >= 0.8 * plant.e_nom
                    if gcmd[i] == 1:
                        want = outage_min >= 5
                        if gen_on:
                            rescue = False
                    elif gcmd[i] == 0:  # no planned run: a planned run's tail stops after its minimum run; a rescue runs until the battery is charged
                        if gen_on and rescue:
                            want = not full
                        elif gen_on:
                            # past its minimum run with the battery at its floor, the operator keeps it running as a rescue rather than let the lights go out
                            want = elapsed < plant.gen_min_run or at_floor
                            rescue = elapsed >= plant.gen_min_run and at_floor
                        else:
                            want = lights_out
                    else:  # the native baseline: someone starts it when the lights would go out and stops it once the battery is charged
                        want = (gen_on and not full) or lights_out
                if gen_on and not want and elapsed < plant.gen_min_run:
                    want = True
            if want and not gen_on:
                gen_on, elapsed = True, 0
                rescue = plant.gen_control == "manual" and gcmd[i] != 1
            elif not want and gen_on:
                gen_on, elapsed, rescue = False, 0, False
            off_grid_site = plant.g_max <= 0
            served = served_full if off_grid_site or (plant.load_mode == 1 and gen_on) else crit[i]
            if gen_on:
                need = served - pv
                gen = min(plant.gen_kw, max(plant.gen_fmin * plant.gen_kw, need))
                surplus = gen + pv - served
                if surplus > 0:
                    bat = -min(surplus, plant.p_ch if has_bat else 0.0, ch_room_abs)
                    # what the battery cannot take curtails PV first (the genset keeps its minimum load); only then does the genset itself run below
                    # its minimum load, which is a breach the KPIs count, never PV curtailment
                    rest = surplus + bat
                    pv_cut = min(rest, pv)
                    gen -= rest - pv_cut
                    curtail = pv_cut
                else:
                    bat = min(-surplus, plant.p_dis, dis_room_abs)
                    unserved = -surplus - bat
            else:
                net = served - pv
                if net > 0:
                    bat = min(net, plant.p_dis, dis_room_abs)
                    unserved = net - bat
                else:
                    bat = -min(-net, plant.p_ch, ch_room_abs)
                    curtail = -net + bat
            if curtail > 0 and gen > plant.gen_fmin * plant.gen_kw:
                cut = min(curtail, gen - plant.gen_fmin * plant.gen_kw)
                gen -= cut
                curtail -= cut
        out["served"][i] = max(0.0, served - max(0.0, unserved))
        if gen_on:
            elapsed += 1
            out["fuel"][i] = (plant.gen_a * gen + plant.gen_b * plant.gen_kw) * dt
        out["gen_on"][i] = gen_on
        if has_bat:
            if bat > 0:
                e -= bat * dt / plant.eta_d
            else:
                e += -bat * dt * plant.eta_c
            e = min(max(e, 0.0), plant.e_nom)
        out["bat"][i], out["gen"][i], out["unserved"][i], out["curtail"][i] = bat, gen, max(0.0, unserved), max(0.0, curtail)
        out["pv"][i] = max(0.0, pv - max(0.0, curtail))
        out["soc"][i] = e / plant.e_nom if has_bat else 0.0
    out["gen_on_end"], out["gen_elapsed_end"] = gen_on, elapsed
    out["outage_min_end"], out["gen_rescue_end"] = outage_min, bool(gen_on and rescue)
    return out


def telemetry_rows(site_id: str, series: dict, sim: dict, rng_seed: int = 0, inverter_mode: np.ndarray | None = None) -> list[tuple]:
    """Rows for the telemetry `minute` table from a simulated day, with a BMS state-of-charge error (bias and drift, reset at full charge)."""
    rng = np.random.default_rng(rng_seed)
    bias = rng.normal(0, 3.0)
    rows = []
    soc_pct = sim["soc"] * 100.0
    bms = np.clip(soc_pct + bias + np.cumsum(rng.normal(0, 0.02, len(soc_pct))), 0, 100)
    bms = np.where(soc_pct >= 98.5, soc_pct, bms)
    temp = series["temp"] + 7.0 + np.abs(sim["bat"]) * 0.0
    for i, ts in enumerate(series["ts"]):
        imp, exp = sim["imp"][i], sim["exp"][i]
        rows.append((site_id, int(ts), "twin", int(series["grid"][i] > 0.5), round((imp - exp) * 1000, 1), round(imp * 1000 / 60, 3),
                     round(exp * 1000 / 60, 3), round(sim["pv"][i] * 1000, 1), round(sim["served"][i] * 1000, 1), round(sim["bat"][i] * 1000, 1),
                     round(float(soc_pct[i]), 2), round(float(bms[i]), 2), round(float(temp[i]), 1), int(bool(sim["gen_on"][i])),
                     round(sim["gen"][i] * 1000, 1), round(sim["fuel"][i], 5), MODES[int(inverter_mode[i])] if inverter_mode is not None else "self_use",
                     round(sim["unserved"][i] * 1000, 1), round(sim["curtail"][i] * 1000, 1)))
    return rows
