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


def billing_period(conn: dict | None, t0: int) -> tuple[int, int]:
    """The current bill's consumption window as epoch seconds [start, end): from the day after the previous reading to the end of the reading day
    (tariffs.billing_period_for, from the connection's optional reading_day; the calendar month without one)."""
    first, last = tariffs.billing_period_for(conn, _local(t0).date())
    start = int(datetime(first.year, first.month, first.day, tzinfo=PKT).timestamp())
    end = int((datetime(last.year, last.month, last.day, tzinfo=PKT) + timedelta(days=1)).timestamp())
    return start, end


def month_to_date(site_id: str, conn: dict | None, t0: int) -> dict:
    """Billing-month-to-date imports (and exports) per period, the MDI peak so far, the open block's elapsed mean, and the recent import (and export)
    per day per period. The month is the bill's: it starts the day after the previous meter reading (calendar month without a reading_day)."""
    ms, me = billing_period(conn, t0)
    g = telemetry.minute_grid(site_id, min(ms, t0 - 7 * 86400), t0)
    imp = np.nan_to_num(g["imp"])
    exp = np.nan_to_num(g["exp"])
    pk = peak_mask(conn, g["ts"])
    in_month = g["ts"] >= ms
    mtd = {"peak": float(imp[in_month & pk].sum() / 60), "off_peak": float(imp[in_month & ~pk].sum() / 60)}
    mtd_exp = {"peak": float(exp[in_month & pk].sum() / 60), "off_peak": float(exp[in_month & ~pk].sum() / 60)}
    week = g["ts"] >= t0 - 7 * 86400
    # the per-day rate divides by the days the telemetry actually covers (a new site, an upload gap), not by the window's seven
    days_w = max(1.0, float(np.sum(~np.isnan(g["imp"][week]))) / 1440)
    per_day = {"peak": float(imp[week & pk].sum() / 60 / days_w), "off_peak": float(imp[week & ~pk].sum() / 60 / days_w)}
    per_day_exp = {"peak": float(exp[week & pk].sum() / 60 / days_w), "off_peak": float(exp[week & ~pk].sum() / 60 / days_w)}
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
    return {"mtd": mtd, "per_day": per_day, "M0": m0, "G_elapsed": g_el, "n_elapsed": int(n_el_steps), "mtd_export": mtd_exp,
            "per_day_export": per_day_exp, "period_start": ms, "period_end": me}


GAP_MINUTES = 15  # a gap in the genset signal up to this long continues the run on both sides (5-minute sources, ingest lag)


def _carried_genset(site_id: str, t0: int) -> dict:
    g = telemetry.minute_grid(site_id, t0 - 86400, t0)
    on = g["gen_on"]
    known = ~np.isnan(on)
    if not known.any():
        return {"u_prev": 0, "elapsed": 99, "hours_today": 0.0, "unknown": True}
    # walk back over the known samples only: the run started at the first sample of the last unbroken stretch in the same state
    idx = np.where(known)[0]
    st = on[idx] > 0.5
    last = bool(st[-1])
    j = len(idx) - 1
    while j > 0 and st[j - 1] == last and idx[j] - idx[j - 1] <= GAP_MINUTES:
        j -= 1
    elapsed_steps = (len(on) - int(idx[j])) // 15
    # hours run today: each sample stands for the minutes up to the next one, at most GAP_MINUTES (a 5-minute row is five minutes)
    n = np.arange(len(on))
    last_pos = np.maximum.accumulate(np.where(known, n, -1))
    filled = np.where((last_pos >= 0) & (n - last_pos < GAP_MINUTES), on[np.maximum(last_pos, 0)], np.nan)
    today0 = int(datetime(_local(t0).year, _local(t0).month, _local(t0).day, tzinfo=PKT).timestamp())
    hours_today = float(np.nansum(filled[g["ts"] >= today0]) / 60)
    return {"u_prev": int(last), "elapsed": int(elapsed_steps), "hours_today": hours_today, "unknown": False}


RELEASE_REASONS = {"pause_control": "control_paused", "force_native": "released_to_native"}


def _ts(iso: str | None) -> int | None:
    if not iso:
        return None
    d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())


def _override_steps(o: dict, ts: np.ndarray) -> np.ndarray:
    """The 15-minute steps an override covers: those overlapping [from_ts, until_ts)."""
    until = _ts(o["until_ts"])
    start = _ts(o.get("from_ts"))
    sel = np.asarray(ts) < until
    if start is not None:
        sel &= np.asarray(ts) + 900 > start
    return sel


def control_release(site_id: str, at: int | None = None) -> dict | None:
    """The pause (pause_control) or release to native (force_native) in force at `at` (now by default), else None. While one is in force no
    plan is in force: the tick stores its plan as held and the edge is sent a release instead of the slots (Part 7.3)."""
    at = int(at if at is not None else datetime.now(timezone.utc).timestamp())
    ovs = sites.overrides(site_id)
    for kind in ("pause_control", "force_native"):
        for o in ovs:
            if o["kind"] == kind and bool(_override_steps(o, np.array([forecast.align(at)]))[0]):
                return {"reason": RELEASE_REASONS[kind], "kind": kind, "until": o["until_ts"], "author": o["author"], "override_id": o["id"]}
    return None


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
        # the rest of the bill after the horizon: up to the end of the reading day (the calendar month's end without a reading_day)
        rest_days = max(0.0, (mtd["period_end"] - (t0 + steps * 900)) / 86400)
    rest = {k: v * rest_days for k, v in mtd["per_day"].items()} if mtd else None
    exports = {}
    if mtd and regime == "net_metering_legacy":
        # legacy net metering nets each period's exports against its imports on the bill, so the case is decided on the net
        exports = {"mtd_export": mtd["mtd_export"], "rest_net_export": {k: v * rest_days for k, v in mtd["per_day_export"].items()}}
    vec = tariffs.vectors(None if off_grid else conn, t0_dt, steps, mtd["mtd"] if mtd else None, rest, G_max, **exports)
    # the current billing month's steps (the ICP case and the MDI month are the bill's), from the vectors when they carry it
    bms = vec.get("billing_month")
    if bms is not None and len(bms) == steps:
        in_month = np.array([b == bms[0] for b in bms], dtype=bool)
    else:
        in_month = solar.local_month(ts) == _local(t0).month
    p = {"T": steps, "ts": ts, "L": L, "Lhi": np.maximum(Lhi, L), "Lc": np.minimum(Lc, L), "PV": PV, "PVlo": np.minimum(PVlo, PV), "g": g,
         "p_start": p_start, "dur_steps": dur, "c_imp": vec["c_imp"], "c_exp": vec["c_exp"], "peak": vec["peak"], "in_month": in_month,
         "G_max": G_max, "X_max": X_max, "off_grid": off_grid, "load_mode": int(d.get("outage_load_mode", 0)),
         "voll": float(d.get("voll_rs_per_kwh", 500)), "reserve_threshold": float((d.get("reserve_policy") or {}).get("p_threshold", 0.2)),
         "icp": vec["icp"], "regime": regime}
    if vec["mdi"] and mtd:
        month_end = int(vec["billing_month_end_step"]) if "billing_month_end_step" in vec else (int(np.argmin(in_month)) if not in_month.all() else steps)
        p["mdi"] = {"rate": vec["mdi"]["rate_rs_per_kw"], "M0": mtd["M0"], "floor": vec["mdi"]["floor_kw"], "n_mdi": vec["mdi"]["interval_steps"],
                    "G_elapsed": mtd["G_elapsed"], "n_elapsed": mtd["n_elapsed"], "month_end_step": month_end}
    # overrides as bounds (Part 4.8)
    ovs = sites.overrides(site["id"])
    mask = {k: np.zeros(steps, dtype=bool) for k in ("genset_hold", "charge_block", "reserve", "pause_control", "force_native", "force_charge")}
    # a manual reserve (hold) and a force-charge target are per-step floors in percent, each inside its own window only; they are kept apart since
    # they bind differently around an outage (optimise.manual_floor_parts)
    hold_pct, force_pct = np.zeros(steps), np.zeros(steps)
    for o in ovs:
        if o["kind"] not in mask:
            continue
        sel = _override_steps(o, ts)
        mask[o["kind"]] |= sel
        if o["kind"] == "reserve" and o["value"] is not None:
            hold_pct[sel] = np.maximum(hold_pct[sel], float(o["value"]))
        elif o["kind"] == "force_charge":
            target = float(o["value"]) if o["value"] is not None else 100.0
            force_pct[sel] = np.maximum(force_pct[sel], target)
    floor_pct = np.maximum(hold_pct, force_pct)
    # pause and release to native do not shape the solve (the tick holds the plan and the edge gets a release); the rest are operator holds
    p["override_mask"] = mask["genset_hold"] | mask["charge_block"] | mask["reserve"] | mask["force_charge"]
    p["overrides"] = [{"kind": o["kind"], "value": o["value"], "until": o["until_ts"], "author": o["author"]} for o in ovs]
    gen = None
    if g_asset and d.get("genset_start_control", "none") != "none":
        kind = g_asset.get("fuel_kind", "hsd")
        price, _src = tariffs.fuel_price(kind, _local(t0).date())
        carried = _carried_genset(site["id"], t0)
        control = d.get("genset_start_control", "manual")
        if carried["unknown"] and control == "auto_on_grid_loss" and len(g) and g[0] < 0.5:
            carried = {**carried, "u_prev": 1, "elapsed": 99}  # in an outage the panel has already started the set; this fixes nothing
        local_days = np.array([(_local(int(x)).date() - _local(t0).date()).days for x in ts])
        c_om = g_asset["c_om_rs_per_kwh"] + (SELFGEN_DUTY_RS_PER_KWH if g_asset["rated_kw"] > 400 and (d.get("lat", 31) > 28) else 0.0)
        gen = {"S": g_asset["rated_kw"], "f_min": g_asset["f_min"], "a": g_asset["a"], "b": g_asset["b"], "c_fuel": price, "c_start": g_asset["c_start_rs"],
               "c_runhr": g_asset["c_runhr_rs"], "c_om": c_om, "UT": max(1, int(math.ceil(g_asset["min_up_h"] * 4))),
               "DT": max(1, int(math.ceil(g_asset["min_down_h"] * 4))), "u_prev": carried["u_prev"], "elapsed": carried["elapsed"],
               "cap_hours": g_asset["cap_hours_per_day"], "hours_today": carried["hours_today"], "day_index": local_days,
               "fuel_available": float(site.get("state", {}).get("fuel_l", g_asset["tank_l"]) or 1e9), "parallel": g_asset["parallel_allowed"],
               "control": control, "hold": mask["genset_hold"], "unknown": carried["unknown"],
               "soc_on": float((d.get("reserve_policy") or {}).get("min_soc_pct", 20.0)) / 100.0 + 0.05}  # twin.plant_of's start threshold
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
               "soc_max": b_asset["soc_max"], "a_grid": bool(d.get("grid_charge_allowed", True)) and not off_grid, "a_gen": bool(d.get("gen_charge_allowed", True)) and delivery.GEN_CHARGE_SUPPORTED,
               "c_cyc": optimise.wear_cost(b_asset),
               "b_cal": optimise.calendar_slope(temp, b_asset["replacement_cost_per_kwh"]) if d.get("calendar_cost_on", True) else np.zeros(steps),
               "charge_block": mask["charge_block"]}
        p["battery"] = bat
        if floor_pct.any():
            # every manual floor (a caller that drops the overrides drops this key), and its two parts
            p["reserve_manual"] = floor_pct / 100.0 * e_nom
            p["reserve_manual_hold"] = hold_pct / 100.0 * e_nom
            p["reserve_manual_force"] = force_pct / 100.0 * e_nom
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
    r_rows, man = res.get("r") or {}, res.get("manual_floor") or {}
    dump = s.get("W") if s.get("W") is not None else np.zeros(T)

    def floor_pct(t: int) -> float | None:
        """The floor the plan holds at step t: soc_min, raised by the reserve row and the manual floor where those rows exist, never above
        soc_max (the reserve row's 2-sigma widening can push its target past the band)."""
        if not En:
            return None
        f = bat["soc_min"] * En + (float(res["R"][t]) if t in r_rows else 0.0)
        if t in man:
            f = max(f, float(man[t]))
        return round(min(bat["soc_max"] * En, f) / En * 100, 1)

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
                    "reserve_pct": floor_pct(t),
                    "genset_kw": round(float(s["P"][t]), 2), "genset_on": bool(s["u"][t] > 0.5), "litres": round(float(s["F"][t]), 3),
                    "genset_dump_kw": round(float(dump[t]), 2),
                    "grid_charge_permitted": bool(bat.get("a_grid", True)) if bat else False,
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
    soc0 = bat["E0"] / bat["E_nom"]
    sim = twin.simulate(series, pol, plant, soc0)
    # a battery that starts below the band is planned towards it, never refused (Part 4.3): the floor is the lower of the band and the start
    if float(np.min(sim["soc"])) < min(bat["soc_min"], soc0) - 0.02:
        return "replayed_soc_below_floor"
    if float(np.max(np.where(series["grid"] < 0.5, sim["imp"], 0.0))) > 1e-6:
        return "import_in_outage"
    return None


def _saved_outage(site_id: str, t0: int) -> dict | None:
    """The outage issue already saved for this quarter (a "Refresh forecasts" or an earlier re-solve), if any."""
    try:
        prior = telemetry.latest_forecast(site_id, "outage", t0)
    except Exception:  # a missing or unreadable forecast table never stops a tick
        return None
    return prior if prior and int(prior.get("belief_time", -1)) == int(t0) else None


def _keep_observed_outage(site_id: str, t0: int, now: int, bundle: dict, prior: dict | None, save: bool) -> dict:
    """A re-solve never drops an outage that an earlier issue of the same quarter observed and that telemetry has not seen end.

    When the saved issue observed the grid down and this one does not (the edge's rows stopped arriving, or this tick's clock is earlier), the
    saved issue is kept: if its observation began by `now` and no known minute since shows the grid back, the plan uses it too; if it began after
    `now`, this tick plans on what was known at `now` but leaves the better-informed saved issue in place. A grid seen back lets the new issue
    stand."""
    out = bundle.get("outage") or {}
    old = (prior or {}).get("observed")
    if not old or out.get("observed") or out.get("off_grid"):
        return bundle
    since = int(old.get("since", now))
    keep_plan = False
    if since <= now:
        g = telemetry.minute_grid(site_id, since, max(since + 60, int(now)))
        grid = g["grid"]
        if np.any(grid[~np.isnan(grid)] == 1):
            return bundle  # the grid came back: the new issue is right
        keep_plan = True
    if save:
        body = {k: v for k, v in prior.items() if k not in ("belief_time", "stage", "model_version")}
        telemetry.save_forecast(site_id, "outage", t0, int(prior.get("stage", out.get("stage", 0))), str(prior.get("model_version", forecast.VERSION)), body)
    if not keep_plan:
        return bundle
    n = len(out.get("g", []))
    g_old = np.asarray(prior.get("g") or [], dtype=float)
    pd_old = np.asarray(prior.get("p_down") or [], dtype=float)
    if len(g_old) < n or len(pd_old) < n:
        return bundle
    return {**bundle, "outage": {**out, "g": g_old[:n], "p_down": np.maximum(np.asarray(out["p_down"], dtype=float), pd_old[:n]), "observed": old}}


def tick(site_id: str, at: int | None = None, steps: int = 192, save: bool = True, time_limit: float = 10.0) -> dict:
    site = sites.full(site_id)
    if site is None:
        raise KeyError(site_id)
    now = int(at if at is not None else datetime.now(timezone.utc).timestamp())
    t0 = forecast.align(now)
    started = store._now()
    # the observed grid state reads telemetry up to the actual tick time, so a grid loss inside the current quarter is planned for at once
    prior = _saved_outage(site_id, t0)
    bundle = forecast.issue(site, t0, save=save, now=now)
    bundle = _keep_observed_outage(site_id, t0, now, bundle, prior, save)
    # a pause or a release to native is read before the solve, so a failed solve while one is in force still ends the pre-pause plan
    release = control_release(site_id, t0)
    p = build_problem(site, t0, bundle, steps)
    res = optimise.solve_with_policy(p, time_limit=time_limit)
    if res.get("sol") is not None and len(res["sol"]["G"]) != p["T"]:
        p = optimise.shorten(p, len(res["sol"]["G"]))  # the relaxed retry solved a shorter horizon: steps, slots and the guard follow it
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
                          "relaxed": res.get("relaxed"), "message": res.get("message"), "shed_rule_resolve": res.get("shed_rule_resolve")},
               "horizon_steps": p["T"], "control_release": release}
    if res["status"] in ("not_solved", "refused"):
        reason = res.get("message", "not solved")
        if save:
            with store.connect() as con:
                if release:
                    # paused or released: no plan is in force whatever the solve did, so the pre-pause plan stops being active here too
                    con.execute("UPDATE plans SET status = 'superseded' WHERE site_id = ? AND status = 'active'", (site_id,))
                pid = store.new_id()
                con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, reason, solver, summary) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, site_id, store._now() if at is None else _iso(now), _iso(t0), _iso(t0),
                                                             _iso(t0 + p["T"] * 900), "a", "failed", reason, "highs", json.dumps(summary)))
                con.execute("INSERT OR REPLACE INTO runs (site_id, tick, started, finished, status, reason, plan_id) VALUES (?,?,?,?,?,?,?)",
                            (site_id, _iso(t0), started, store._now(), "failed", reason, pid))
            if release:
                sites.put_state(site_id, active_plan=None, last_tick=_iso(t0))
        return {"status": "failed", "reason": reason, "control_release": release}
    steps_out = _steps_out(p, res, bundle)
    fallback_floor = float((site["data"].get("reserve_policy") or {}).get("min_soc_pct", 20.0))
    bms_low = ((site.get("assets") or {}).get("battery") or {}).get("soc_min", 0.1) * 100
    compiled = delivery.compile_mode_a(steps_out, brand, p.get("battery"), fallback_floor, bms_low, res["M"][0] if res.get("M") else None)
    guard = _guard(site, p, compiled, steps_out)
    summary.update({"breakdown": res["breakdown"], "objective_rs": res["objective_rs"], "mdi_cap_kw": round(res["M"][0], 1) if res.get("M") else None,
                    "v_end": round(res.get("v_end", 0.0), 2), "guard": guard})
    # a pause or a release to native means no plan is in force: the plan is kept for reading, held, and the previous one stops being active
    # (so nothing stale is served or keyed in when the pause ends; the next tick publishes a fresh plan)
    reason = release["reason"] if release else guard
    status = "held" if reason else "active"
    plan_id = store.new_id()
    if save:
        with store.connect() as con:
            if status == "active" or release:
                con.execute("UPDATE plans SET status = 'superseded' WHERE site_id = ? AND status = 'active'", (site_id,))
            con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, reason, solver, solve_seconds, "
                        "objective_rs, config_hash, summary, steps, slots) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, site_id, store._now() if at is None else _iso(now), _iso(t0), _iso(t0), _iso(t0 + p["T"] * 900),
                         "b" if site["data"].get("control_mode") == "mode_b" else "a", status, reason or "", "highs", res["solve_seconds"], res["objective_rs"],
                         res["config_hash"], json.dumps(summary), json.dumps(steps_out), json.dumps(compiled)))
            con.execute("INSERT OR REPLACE INTO runs (site_id, tick, started, finished, status, reason, solve_s, objective_rs, plan_id) VALUES (?,?,?,?,?,?,?,?,?)",
                        (site_id, _iso(t0), started, store._now(), status, reason or res["status"], res["solve_seconds"], res["objective_rs"], plan_id))
        active = plan_id if status == "active" else (None if release else site.get("state", {}).get("active_plan"))
        sites.put_state(site_id, active_plan=active, last_tick=_iso(t0))
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


def held_info(site_id: str, now: int | None = None) -> dict | None:
    """Why no plan is in force, for GET /plan's `held` ({reason, until, plan_id}), else None.
    - A pause or a release to native in force at `now`: its reason (control_paused or released_to_native), its end (`until`, the override's ISO
      until_ts) and the plan the tick held under it (`plan_id`; None before the first tick of the pause, or when that tick's solve failed). Also
      `kind`, `author` and `override_id` of the override.
    - Otherwise, with no plan active, the latest plan when its guard held it: the guard's reason, `until` None and its id. After a pause has ended
      and before the next tick, this is the pause's held plan under the pause's reason.
    - Otherwise None: a plan is in force, none was made yet, or the latest solve failed."""
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    rel = control_release(site_id, now)
    with store.connect() as con:
        last = con.execute("SELECT id, status, reason FROM plans WHERE site_id = ? ORDER BY generated_at DESC, rowid DESC LIMIT 1", (site_id,)).fetchone()
        active = con.execute("SELECT 1 FROM plans WHERE site_id = ? AND status = 'active' LIMIT 1", (site_id,)).fetchone()
    held = last is not None and last["status"] == "held"
    if rel:
        return {"reason": rel["reason"], "until": rel["until"], "plan_id": last["id"] if held and last["reason"] == rel["reason"] else None,
                "kind": rel["kind"], "author": rel["author"], "override_id": rel["override_id"]}
    if active is None and held:
        return {"reason": last["reason"] or "held", "until": None, "plan_id": last["id"]}
    return None


def get(site_id: str, plan_id: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM plans WHERE site_id = ? AND id = ?", (site_id, plan_id)).fetchone()
    return _plan_out(r) if r else None


def history(site_id: str, limit: int = 50) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM plans WHERE site_id = ? ORDER BY generated_at DESC LIMIT ?", (site_id, limit)).fetchall()
    return [_plan_out(r, with_steps=False) for r in rows]


def _released_at(site_id: str, ts: int) -> bool:
    """Whether a pause or a release to native was in force at the instant `ts`, from the override history: from its creation (from_ts) to its end,
    or to when it was cleared if that came first. The instant, not its quarter: a plan published after a pause was cleared mid-quarter is in force
    from then on."""
    for o in sites.overrides(site_id, active_only=False):
        if o.get("kind") not in RELEASE_REASONS:
            continue
        start, end, cleared = _ts(o.get("from_ts")), _ts(o.get("until_ts")), _ts(o.get("cleared"))
        if cleared is not None and (end is None or cleared < end):
            end = cleared
        if end is not None and (start is None or start <= ts) and ts < end:
            return True
    return False


def active_at(site_id: str, ts: int) -> dict | None:
    """The plan that was in force at an instant: the latest generated at or before it (belief time not after it). None while a pause or a release
    to native was in force: the tick then stores its plan as held (or, when the solve failed, a failed row noting the release), and no plan is in
    force; a guard hold or a failed tick otherwise leaves the previous plan in force."""
    if _released_at(site_id, ts):
        return None
    with store.connect() as con:
        # the latest rows first (the light columns only): a held or failed row of a pause ends the search with no plan in force
        rows = con.execute("SELECT id, status, reason, summary FROM plans WHERE site_id = ? AND belief_time <= ? "
                           "ORDER BY belief_time DESC, generated_at DESC, rowid DESC LIMIT 200", (site_id, _iso(ts))).fetchall()
        for r in rows:
            if r["status"] in ("active", "superseded"):
                return _plan_out(con.execute("SELECT * FROM plans WHERE id = ?", (r["id"],)).fetchone())
            if r["status"] == "held" and r["reason"] in RELEASE_REASONS.values():
                return None
            if r["status"] == "failed" and (json.loads(r["summary"] or "{}") or {}).get("control_release"):
                return None
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


def _no_money(body: dict) -> dict:
    """The message body with every money field taken out before it is signed or stored (plan Part 10.5: plans sent to a key carry no money)."""
    from . import money  # the API's name filter, imported here so the engine does not load it at import time
    return money.for_key(body)


def _record_send(site_id: str, plan_id: str, msg: dict) -> None:
    with store.connect() as con:
        con.execute("INSERT INTO plan_messages (id, site_id, plan_id, key_id, body, sent_at) VALUES (?,?,?,?,?,?)",
                    (store.new_id(), site_id, plan_id, msg["signature"]["key_id"], json.dumps(msg), store._now()))


def _release_message(site_id: str, rel: dict, record: bool = True) -> dict:
    """A signed release while control is paused or released to native: the same keys as a plan message, no slots and no steps, and a
    `release` block; the edge runs the inverter's native settings until the override ends and a fresh plan is issued (Part 7.3)."""
    site = sites.get(site_id) or {"data": {}}
    floor = float((site["data"].get("reserve_policy") or {}).get("min_soc_pct", 20.0))
    now = store._now()
    body = {"contract": delivery.CONTRACT, "type": "release", "plan_id": None, "site_id": site_id, "issued_at": now, "valid_from": now,
            "valid_until": rel["until"], "timezone": "Asia/Karachi", "mode": "b" if site["data"].get("control_mode") == "mode_b" else "a",
            "fallback": {"on_expiry": "inverter_native", "min_soc_floor_pct": floor, "revert_after_hours": delivery.FALLBACK_HOURS},
            "mode_a": {"days": []}, "mode_b": None,
            "notes": {"reason": {"control_paused": "Control paused", "released_to_native": "Released to native"}[rel["reason"]]},
            "release": {"reason": rel["reason"], "until": rel["until"], "by": rel["author"]}}
    msg = delivery.sign(site_id, _no_money(body))
    if record:
        _record_send(site_id, "release", msg)
    return msg


def signed_message(site_id: str, record: bool = True) -> dict | None:
    """The current plan as the ems-edge/1.0 plan message, signed with the site's key; a signed release instead while control is paused or
    released to native. The body carries no money field (the plan's rupees stay on the plan page). `record` writes the plan_messages row that
    the edge page lists as a send: the edge's own pull (the key-only route) records it; any other caller passes record=False."""
    rel = control_release(site_id)
    if rel:
        return _release_message(site_id, rel, record=record)
    plan = current(site_id)
    if plan is None:
        return None
    body = {"contract": delivery.CONTRACT, "type": "plan", "plan_id": plan["id"], "site_id": site_id, "issued_at": plan["generated_at"],
            "valid_from": plan["valid_from"], "valid_until": plan["valid_until"], "timezone": "Asia/Karachi", "mode": plan["mode"],
            "fallback": (plan.get("slots") or {}).get("fallback"),
            "mode_a": {"days": [{"date": d["date"], "slots": [{k: v for k, v in s.items() if k not in ("intent",)} for s in d["slots"]]}
                                for d in (plan.get("slots") or {}).get("days", [])]},
            "mode_b": delivery.mode_b(plan["steps"]) if plan["mode"] == "b" else None,
            "notes": {"reason": plan["summary"].get("status_label")}}
    msg = delivery.sign(site_id, _no_money(body))
    if record:
        _record_send(site_id, plan["id"], msg)
    return msg
