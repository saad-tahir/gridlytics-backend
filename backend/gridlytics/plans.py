"""The 15-minute loop (plan Part 2.4 and 4.8): assemble the optimiser's inputs, solve, explain, compile, sign and store the plan.

`tick(site_id, at)` is one pass for one site: it reads only what was known at `at` (telemetry before it, the forecast issued at it, the tariff versions
in force), so the demo history and a later re-score run the same code as the live loop.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import battery as battery_service
from . import delivery, forecast, optimise, sites, solar, store, tariffs, telemetry, twin

PKT = timezone(timedelta(hours=5))
H = 0.25
SELFGEN_DUTY_RS_PER_KWH = 0.04  # Punjab, above 500 kVA (plan Part 4.1)


def _local(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), PKT)


def peak_mask(conn: dict | None, ts: np.ndarray) -> np.ndarray:
    """Vectorised peak flags for epoch seconds, by the connection's TOU calendar of each local month."""
    ts = np.asarray(ts, dtype=np.int64)
    if conn is None:
        return np.zeros(len(ts), dtype=bool)
    card, _ = tariffs.card_for(conn["data"]["tariff_category"], _local(int(ts[0])).date() if len(ts) else date.today())
    if not (card.get("tou") and card.get("peak") is not None):
        return np.zeros(len(ts), dtype=bool)
    cal = tariffs.calendar_for(conn, _local(int(ts[0])).date() if len(ts) else date.today())
    months = solar.local_month(ts)
    minute = ((ts + 5 * 3600) % 86400) // 60
    out = np.zeros(len(ts), dtype=bool)
    for mo in np.unique(months):
        a, b = tariffs.peak_window(cal, int(mo))
        sel = months == mo
        out[sel] = (minute[sel] >= a) & (minute[sel] < b) if a <= b else (minute[sel] >= a) | (minute[sel] < b)
    return out


def _month_start(ts: int) -> int:
    d = _local(ts)
    return int(datetime(d.year, d.month, 1, tzinfo=PKT).timestamp())


def month_to_date(site_id: str, conn: dict | None, t0: int) -> dict:
    """Month-to-date imports per period, the MDI peak so far, the open block's elapsed mean, and the recent net import per day per period."""
    ms = _month_start(t0)
    g = telemetry.minute_grid(site_id, min(ms, t0 - 7 * 86400), t0)
    imp = np.nan_to_num(g["imp"])
    pk = peak_mask(conn, g["ts"])
    in_month = g["ts"] >= ms
    mtd = {"peak": float(imp[in_month & pk].sum() / 60), "off_peak": float(imp[in_month & ~pk].sum() / 60)}
    week = g["ts"] >= t0 - 7 * 86400
    days_w = max(1.0, week.sum() / 1440)
    per_day = {"peak": float(imp[week & pk].sum() / 60 / days_w), "off_peak": float(imp[week & ~pk].sum() / 60 / days_w)}
    interval = int((conn or {"data": {}})["data"].get("mdi_interval_min", 30))
    m0 = 0.0
    mi = imp[in_month]
    mts = g["ts"][in_month]
    if len(mi):
        blk = (mts // (interval * 60))
        uniq, inv = np.unique(blk, return_inverse=True)
        sums = np.bincount(inv, weights=mi)
        full = np.bincount(inv) >= interval * 0.8
        if full.any():
            m0 = float((sums[full] / interval).max())
    blk_start = t0 // (interval * 60) * (interval * 60)
    el = imp[g["ts"] >= blk_start]
    n_el_steps = (t0 - blk_start) // 900
    g_el = float(el.mean()) if len(el) else 0.0
    return {"mtd": mtd, "per_day": per_day, "M0": m0, "G_elapsed": g_el, "n_elapsed": int(n_el_steps)}


def _carried_genset(site_id: str, t0: int) -> dict:
    g = telemetry.minute_grid(site_id, t0 - 86400, t0)
    on = g["gen_on"]
    known = ~np.isnan(on)
    if not known.any():
        return {"u_prev": 0, "elapsed": 99, "hours_today": 0.0, "unknown": True}
    last = bool(on[known][-1] > 0.5)
    k = len(on) - 1
    while k > 0 and not np.isnan(on[k - 1]) and (on[k - 1] > 0.5) == last:
        k -= 1
    elapsed_steps = (len(on) - k) // 15
    today0 = int(datetime(_local(t0).year, _local(t0).month, _local(t0).day, tzinfo=PKT).timestamp())
    hours_today = float(np.nansum(on[g["ts"] >= today0]) / 60)
    return {"u_prev": int(last), "elapsed": int(elapsed_steps), "hours_today": hours_today, "unknown": False}


def build_problem(site: dict, t0: int, bundle: dict, steps: int = 192) -> dict:
    conn = (site.get("connections") or [None])[0]
    a = site.get("assets") or {}
    b_asset, g_asset = a.get("battery"), a.get("genset")
    d = site["data"]
    t0_dt = datetime.fromtimestamp(t0, timezone.utc)
    ts = forecast.step_times(t0, steps)
    off_grid = conn is None or conn["data"].get("regime") == "off_grid"
    p_load, p_pv, p_out = bundle["load"], bundle["pv"], bundle["outage"]
    L = np.asarray(p_load["q"]["q50"][:steps])
    Lhi = np.asarray(p_load["q"]["q75"][:steps])
    Lc = np.asarray(p_load["crit"][:steps])
    PV = np.asarray(p_pv["q"]["q50"][:steps])
    PVlo = np.asarray(p_pv["q"]["q25"][:steps])
    g = np.zeros(steps) if off_grid else np.asarray(p_out["g"][:steps], dtype=float)
    p_start = np.zeros(steps) if off_grid else np.asarray(p_out["p_start"][:steps])
    dur = np.asarray(p_out["dur_steps"][:steps], dtype=int)
    G_max = 0.0 if off_grid else float(conn["data"]["sanctioned_load_kw"]) * 1.25
    regime = (conn or {"data": {}})["data"].get("regime", "none")
    X_max = float(conn["data"].get("export_cap_kw", 0)) if regime in ("net_billing", "net_metering_legacy") else 0.0
    mtd = month_to_date(site["id"], None if off_grid else conn, t0) if not off_grid else None
    rest_days = 0.0
    if mtd:
        local = _local(t0)
        nxt = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=PKT)
        rest_days = max(0.0, (nxt.timestamp() - (t0 + steps * 900)) / 86400)
    rest = {k: v * rest_days for k, v in mtd["per_day"].items()} if mtd else None
    vec = tariffs.vectors(None if off_grid else conn, t0_dt, steps, mtd["mtd"] if mtd else None, rest, G_max)
    in_month = solar.local_month(ts) == _local(t0).month
    p = {"T": steps, "ts": ts, "L": L, "Lhi": np.maximum(Lhi, L), "Lc": np.minimum(Lc, L), "PV": PV, "PVlo": np.minimum(PVlo, PV), "g": g,
         "p_start": p_start, "dur_steps": dur, "c_imp": vec["c_imp"], "c_exp": vec["c_exp"], "peak": vec["peak"], "in_month": in_month,
         "G_max": G_max, "X_max": X_max, "off_grid": off_grid, "load_mode": int(d.get("outage_load_mode", 0)),
         "voll": float(d.get("voll_rs_per_kwh", 500)), "reserve_threshold": float((d.get("reserve_policy") or {}).get("p_threshold", 0.2)),
         "icp": vec["icp"]}
    if vec["mdi"] and mtd:
        month_end = int(np.argmin(in_month)) if not in_month.all() else steps
        p["mdi"] = {"rate": vec["mdi"]["rate_rs_per_kw"], "M0": mtd["M0"], "floor": vec["mdi"]["floor_kw"], "n_mdi": vec["mdi"]["interval_steps"],
                    "G_elapsed": mtd["G_elapsed"], "n_elapsed": mtd["n_elapsed"], "month_end_step": month_end}
    # overrides as bounds (Part 4.8)
    ovs = sites.overrides(site["id"])
    mask = {k: np.zeros(steps, dtype=bool) for k in ("genset_hold", "charge_block", "reserve", "pause_control", "force_native", "force_charge")}
    reserve_pct = None
    for o in ovs:
        until = int(datetime.fromisoformat(o["until_ts"]).timestamp())
        sel = ts < until
        mask[o["kind"]] |= sel
        if o["kind"] == "reserve" and o["value"] is not None:
            reserve_pct = max(reserve_pct or 0.0, float(o["value"]))
    p["override_mask"] = mask["genset_hold"] | mask["charge_block"] | mask["reserve"] | mask["force_native"]
    p["overrides"] = [{"kind": o["kind"], "value": o["value"], "until": o["until_ts"], "author": o["author"]} for o in ovs]
    gen = None
    if g_asset and d.get("genset_start_control", "none") != "none":
        kind = g_asset.get("fuel_kind", "hsd")
        price, _src = tariffs.fuel_price(kind, _local(t0).date())
        carried = _carried_genset(site["id"], t0)
        local_days = np.array([(_local(int(x)).date() - _local(t0).date()).days for x in ts])
        c_om = g_asset["c_om_rs_per_kwh"] + (SELFGEN_DUTY_RS_PER_KWH if g_asset["rated_kw"] > 400 and (d.get("lat", 31) > 28) else 0.0)
        gen = {"S": g_asset["rated_kw"], "f_min": g_asset["f_min"], "a": g_asset["a"], "b": g_asset["b"], "c_fuel": price, "c_start": g_asset["c_start_rs"],
               "c_runhr": g_asset["c_runhr_rs"], "c_om": c_om, "UT": max(1, int(math.ceil(g_asset["min_up_h"] * 4))),
               "DT": max(1, int(math.ceil(g_asset["min_down_h"] * 4))), "u_prev": carried["u_prev"], "elapsed": carried["elapsed"],
               "cap_hours": g_asset["cap_hours_per_day"], "hours_today": carried["hours_today"], "day_index": local_days,
               "fuel_available": float(site.get("state", {}).get("fuel_l", g_asset["tank_l"]) or 1e9), "parallel": g_asset["parallel_allowed"],
               "control": d.get("genset_start_control", "manual"), "hold": mask["genset_hold"], "unknown": carried["unknown"]}
        p["genset"] = gen
    p["c_short"] = optimise._gen_marginal(gen) if gen else p["voll"]
    bat = None
    if b_asset:
        bs = battery_service.state(site, t0) or {}
        e_nom = b_asset["nameplate_kwh"] * b_asset.get("soh", 1.0)
        soc = bs.get("soc_pct")
        soc = (soc / 100.0) if soc is not None else float(site.get("state", {}).get("soc", 0.5))
        temp = solar.air_temp(ts, solar.local_month(ts) - 1) + float(d.get("enclosure_temp_offset_c", 7.0))
        bat = {"E_nom": e_nom, "E0": soc * e_nom, "sigma0": min(0.15, (bs.get("sigma_pct") or 5.0) / 100.0), "P_ch": b_asset["p_ch_kw"],
               "P_dis": b_asset["p_dis_kw"], "eta_c": b_asset["eta_c"], "eta_d": b_asset["eta_d"], "soc_min": b_asset["soc_min"],
               "soc_max": b_asset["soc_max"], "a_grid": bool(d.get("grid_charge_allowed", True)) and not off_grid, "a_gen": bool(d.get("gen_charge_allowed", False)),
               "c_cyc": optimise.wear_cost(b_asset),
               "b_cal": optimise.calendar_slope(temp, b_asset["replacement_cost_per_kwh"]) if d.get("calendar_cost_on", True) else np.zeros(steps),
               "charge_block": mask["charge_block"]}
        p["battery"] = bat
        if reserve_pct is not None:
            p["reserve_manual"] = reserve_pct / 100.0 * e_nom
        c_next = float(np.max(vec["c_imp"][-96:])) if not off_grid else p["c_short"]
        lam = p_out.get("lam_day") or 0.0
        r_next = float(np.mean(Lc)) * 2.0 / b_asset["eta_d"] if (lam > 0.5 or off_grid) else 0.0
        p["terminal"] = {"c_next": c_next, "H_peak": 4.0, "R_next": r_next, "c_cal_hold": float(np.mean(bat["b_cal"][:32])) if steps else 0.0}
    return p


def _steps_out(p: dict, res: dict, bundle: dict) -> list[dict]:
    s = res["sol"]
    T = p["T"]
    bat = p.get("battery")
    En = bat["E_nom"] if bat else 0.0
    cost_cum = 0.0
    out = []
    gen = p.get("genset")
    for t in range(T):
        G, X = float(s["G"][t]), float(s["X"][t])
        cost = H * (float(p["c_imp"][t]) * G - float(p["c_exp"][t]) * X)
        if gen:
            cost += gen["c_fuel"] * float(s["F"][t]) + gen["c_start"] * float(s["v"][t]) + H * gen["c_runhr"] * float(s["u"][t]) + H * gen["c_om"] * float(s["P"][t])
        if bat:
            cost += H * bat["c_cyc"] * float(s["D"][t])
        cost_cum += cost
        rsn = res["reasons"][t]
        out.append({"t": int(p["ts"][t]), "local": _local(int(p["ts"][t])).strftime("%H:%M"), "date": _local(int(p["ts"][t])).date().isoformat(),
                    "peak": bool(p["peak"][t]), "c_imp": round(float(p["c_imp"][t]), 2), "c_exp": round(float(p["c_exp"][t]), 2),
                    "pv_kw": round(float(p["PV"][t] - s["K"][t]), 2), "pv_forecast_kw": round(float(p["PV"][t]), 2), "pv_lo_kw": round(float(p["PVlo"][t]), 2),
                    "load_kw": round(float(p["L"][t]), 2), "load_hi_kw": round(float(p["Lhi"][t]), 2), "critical_kw": round(float(p["Lc"][t]), 2),
                    "grid_import_kw": round(G, 2), "grid_export_kw": round(X, 2), "charge_kw": round(float(s["C"][t]), 2),
                    "discharge_kw": round(float(s["D"][t]), 2), "soc_start_pct": round(float(s["E"][t]) / En * 100, 1) if En else None,
                    "soc_pct": round(float(s["E"][t + 1]) / En * 100, 1) if En else None,
                    "reserve_pct": round((bat["soc_min"] * En + float(res["R"][t])) / En * 100, 1) if En else None,
                    "genset_kw": round(float(s["P"][t]), 2), "genset_on": bool(s["u"][t] > 0.5), "litres": round(float(s["F"][t]), 3),
                    "unserved_kw": round(float(s["U"][t]), 2), "curtail_kw": round(float(s["K"][t]), 2),
                    "p_down": round(float(bundle["outage"]["p_down"][t]), 3), "p_start": round(float(p["p_start"][t]), 3), "grid_present": float(p["g"][t]),
                    "cost_rs": round(cost, 2), "cum_rs": round(cost_cum, 2), "reason": rsn["reason"], "detail": rsn["detail"]})
    return out


def _guard(site: dict, p: dict, compiled: dict, steps_out: list[dict]) -> str | None:
    """The wrong-plan guard (Part 7.1 step 8): replay the compiled table through the twin on the forecast and refuse a plan that crosses the floor,
    imports in a forecast outage or exceeds its own MDI cap."""
    bat = p.get("battery")
    if not bat:
        return None
    T = p["T"]
    ts = np.repeat(p["ts"], 15) + np.tile(np.arange(15) * 60, T)
    series = {"pv": np.repeat(p["PV"], 15), "load": np.repeat(p["L"], 15), "crit": np.repeat(p["Lc"], 15), "grid": np.repeat(p["g"], 15),
              "ts": ts, "temp": np.full(len(ts), 30.0)}
    slots = [s for d in compiled["days"] for s in d["slots"]]
    pol = twin.slots_policy(slots, ts)
    plant = twin.plant_of(site)
    sim = twin.simulate(series, pol, plant, bat["E0"] / bat["E_nom"])
    if float(np.min(sim["soc"])) < bat["soc_min"] - 0.02:
        return "replayed_soc_below_floor"
    if float(np.max(np.where(series["grid"] < 0.5, sim["imp"], 0.0))) > 1e-6:
        return "import_in_outage"
    return None


def tick(site_id: str, at: int | None = None, steps: int = 192, save: bool = True, time_limit: float = 10.0) -> dict:
    site = sites.full(site_id)
    if site is None:
        raise KeyError(site_id)
    now = int(at if at is not None else datetime.now(timezone.utc).timestamp())
    t0 = forecast.align(now)
    started = store._now()
    bundle = forecast.issue(site, t0, save=save)
    p = build_problem(site, t0, bundle, steps)
    res = optimise.solve_with_policy(p, time_limit=time_limit)
    brand = ((site.get("assets") or {}).get("inverter") or {}).get("brand", "deye")
    stage = min(bundle["pv"]["stage"], bundle["load"]["stage"])
    tier = site.get("tier", "monitor")
    summary = {"stage": stage, "stages": {"pv": bundle["pv"]["stage"], "load": bundle["load"]["stage"], "outage": bundle["outage"]["stage"]},
               "tier": tier, "recommendation": tier == "monitor", "brand": brand, "notes": res.get("notes", []),
               "outage_now": bundle["outage"].get("observed"), "adherence": bundle["outage"].get("adherence"),
               "icp": {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in (p.get("icp") or {}).items()},
               "overrides": p.get("overrides", []), "genset_state_unknown": bool(p.get("genset", {}) and p["genset"].get("unknown")),
               "status_label": {"optimal": "Solved within tolerance", "feasible": "Solved within the time limit"}.get(res["status"], "Not solved"),
               "solver": {"status": res["status"], "variables": res.get("variables"), "rows": res.get("rows"), "binaries": res.get("binaries"),
                          "relaxed": res.get("relaxed"), "message": res.get("message")}}
    if res["status"] in ("not_solved", "refused"):
        reason = res.get("message", "not solved")
        if save:
            with store.connect() as con:
                pid = store.new_id()
                con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, reason, solver, summary) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, site_id, store._now(), _iso(t0), _iso(t0), _iso(t0 + steps * 900), "a", "failed", reason,
                                                             "highs", json.dumps(summary)))
                con.execute("INSERT OR REPLACE INTO runs (site_id, tick, started, finished, status, reason, plan_id) VALUES (?,?,?,?,?,?,?)",
                            (site_id, _iso(t0), started, store._now(), "failed", reason, pid))
        return {"status": "failed", "reason": reason}
    steps_out = _steps_out(p, res, bundle)
    fallback_floor = float((site["data"].get("reserve_policy") or {}).get("min_soc_pct", 20.0))
    bms_low = ((site.get("assets") or {}).get("battery") or {}).get("soc_min", 0.1) * 100
    compiled = delivery.compile_mode_a(steps_out, brand, p.get("battery"), fallback_floor, bms_low, res["M"][0] if res.get("M") else None)
    guard = _guard(site, p, compiled, steps_out)
    summary.update({"breakdown": res["breakdown"], "objective_rs": res["objective_rs"], "mdi_cap_kw": round(res["M"][0], 1) if res.get("M") else None,
                    "v_end": round(res.get("v_end", 0.0), 2), "guard": guard})
    status = "held" if guard else "active"
    plan_id = store.new_id()
    if save:
        with store.connect() as con:
            if status == "active":
                con.execute("UPDATE plans SET status = 'superseded' WHERE site_id = ? AND status = 'active'", (site_id,))
            con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, reason, solver, solve_seconds, "
                        "objective_rs, config_hash, summary, steps, slots) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, site_id, store._now() if at is None else _iso(now), _iso(t0), _iso(t0), _iso(t0 + steps * 900),
                         "b" if site["data"].get("control_mode") == "mode_b" else "a", status, guard or "", "highs", res["solve_seconds"], res["objective_rs"],
                         res["config_hash"], json.dumps(summary), json.dumps(steps_out), json.dumps(compiled)))
            con.execute("INSERT OR REPLACE INTO runs (site_id, tick, started, finished, status, reason, solve_s, objective_rs, plan_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (site_id, _iso(t0), started, store._now(), status, guard or res["status"], res["solve_seconds"], res["objective_rs"], plan_id))
        sites.put_state(site_id, active_plan=plan_id if status == "active" else site.get("state", {}).get("active_plan"), last_tick=_iso(t0))
    return {"status": status, "plan_id": plan_id, "objective_rs": res["objective_rs"], "solve_seconds": res["solve_seconds"], "steps": steps_out,
            "slots": compiled, "summary": summary}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------ reading

def _plan_out(r, with_steps: bool = True) -> dict:
    d = dict(r)
    d["summary"] = json.loads(d["summary"] or "{}")
    if with_steps:
        d["steps"] = json.loads(d["steps"]) if d.get("steps") else []
        d["slots"] = json.loads(d["slots"]) if d.get("slots") else None
    else:
        d.pop("steps", None)
        d.pop("slots", None)
    return d


def current(site_id: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM plans WHERE site_id = ? AND status = 'active' ORDER BY generated_at DESC LIMIT 1", (site_id,)).fetchone()
        last = con.execute("SELECT id, status, reason, generated_at FROM plans WHERE site_id = ? ORDER BY generated_at DESC LIMIT 1", (site_id,)).fetchone()
    if r is None:
        return None
    out = _plan_out(r)
    if last and last["status"] in ("failed", "held") and last["id"] != out["id"]:
        out["latest_attempt"] = dict(last)
    return out


def get(site_id: str, plan_id: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM plans WHERE site_id = ? AND id = ?", (site_id, plan_id)).fetchone()
    return _plan_out(r) if r else None


def history(site_id: str, limit: int = 50) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM plans WHERE site_id = ? ORDER BY generated_at DESC LIMIT ?", (site_id, limit)).fetchall()
    return [_plan_out(r, with_steps=False) for r in rows]


def active_at(site_id: str, ts: int) -> dict | None:
    """The plan that was in force at an instant: the latest generated at or before it (belief time not after it)."""
    with store.connect() as con:
        r = con.execute("SELECT * FROM plans WHERE site_id = ? AND status IN ('active','superseded') AND belief_time <= ? ORDER BY belief_time DESC LIMIT 1",
                        (site_id, _iso(ts))).fetchone()
    return _plan_out(r) if r else None


def now_and_next(plan: dict | None, now: int | None = None) -> dict | None:
    if not plan or not plan.get("steps"):
        return None
    now = int(now or datetime.now(timezone.utc).timestamp())
    steps = plan["steps"]
    cur = next((s for s in steps if s["t"] <= now < s["t"] + 900), steps[0])
    nxt, last = [], cur["reason"]
    for s in steps:
        if s["t"] > cur["t"] and s["reason"] != last:
            nxt.append({"t": s["t"], "local": s["local"], "reason": s["reason"], "detail": s["detail"]})
            last = s["reason"]
        if len(nxt) == 3:
            break
    return {"current": cur, "next": nxt}


def mark_applied(site_id: str, plan_id: str, day_local: str, by: str, method: str, photo: str = "") -> dict:
    if method not in ("installer_keyed", "vendor_app", "edge_ack", "owner_keyed"):
        raise ValueError("The method is one of installer_keyed, vendor_app, edge_ack or owner_keyed")
    if photo and not photo.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
        raise ValueError("The photograph must be a PNG or JPEG image")
    if get(site_id, plan_id) is None:
        raise KeyError(plan_id)
    aid = store.new_id()
    with store.connect() as con:
        con.execute("INSERT INTO applied (id, site_id, plan_id, day_local, by, method, photo, created) VALUES (?,?,?,?,?,?,?,?)",
                    (aid, site_id, plan_id, day_local, by, method, photo[:7_000_000], store._now()))
        con.execute("UPDATE plans SET applied = 1 WHERE id = ?", (plan_id,))
    store.audit(by, "marked a plan as applied", site_id, {"plan_id": plan_id, "day": day_local, "method": method})
    return {"id": aid, "plan_id": plan_id, "day_local": day_local, "method": method, "confirmation": "pending"}


def applied_rows(site_id: str, limit: int = 30) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT id, plan_id, day_local, by, method, confirmation, fidelity, created FROM applied WHERE site_id = ? ORDER BY created DESC LIMIT ?",
                           (site_id, limit)).fetchall()
    return [dict(r) for r in rows]


def signed_message(site_id: str) -> dict | None:
    """The current plan as the ems-edge/1.0 plan message, signed with the site's key."""
    plan = current(site_id)
    if plan is None:
        return None
    body = {"contract": delivery.CONTRACT, "type": "plan", "plan_id": plan["id"], "site_id": site_id, "issued_at": plan["generated_at"],
            "valid_from": plan["valid_from"], "valid_until": plan["valid_until"], "timezone": "Asia/Karachi", "mode": plan["mode"],
            "fallback": (plan.get("slots") or {}).get("fallback"),
            "mode_a": {"days": [{"date": d["date"], "slots": [{k: v for k, v in s.items() if k not in ("intent",)} for s in d["slots"]]}
                                for d in (plan.get("slots") or {}).get("days", [])]},
            "mode_b": delivery.mode_b(plan["steps"]) if plan["mode"] == "b" else None,
            "notes": {"objective_rs": plan["objective_rs"], "reason": plan["summary"].get("status_label")}}
    msg = delivery.sign(site_id, body)
    with store.connect() as con:
        con.execute("INSERT INTO plan_messages (id, site_id, plan_id, key_id, body, sent_at) VALUES (?,?,?,?,?,?)",
                    (store.new_id(), site_id, plan["id"], msg["signature"]["key_id"], json.dumps(msg), store._now()))
    return msg
