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
    per day per period. The month is the bill's: it starts the day after the previous meter reading (calendar month without a reading_day).

    A missing minute is unknown, not zero import (round 7, engine-optimiser-13): the month's imports and exports per period are the known
    minutes' energy scaled to all the period's minutes so far (as verify.month_totals scales a scored month's), an MDI block counts only with
    80 % of its minutes known, at the mean of those, and the open block's elapsed mean is that of its known minutes."""
    ms, me = billing_period(conn, t0)
    g = telemetry.minute_grid(site_id, min(ms, t0 - 7 * 86400), t0)
    k_imp, k_exp = ~np.isnan(g["imp"]), ~np.isnan(g["exp"])
    imp = np.nan_to_num(g["imp"])
    exp = np.nan_to_num(g["exp"])
    pk = peak_mask(conn, g["ts"])
    in_month = g["ts"] >= ms

    def to_date(a: np.ndarray, known: np.ndarray) -> dict:
        out = {}
        for per, sel in (("peak", in_month & pk), ("off_peak", in_month & ~pk)):
            n = int(np.sum(known & sel))
            out[per] = float(a[known & sel].sum() / 60) * float(np.sum(sel)) / n if n else 0.0
        return out

    mtd, mtd_exp = to_date(imp, k_imp), to_date(exp, k_exp)
    week = g["ts"] >= t0 - 7 * 86400
    # the per-day rate divides by the days the telemetry actually covers (a new site, an upload gap), not by the window's seven
    days_w = max(1.0, float(np.sum(~np.isnan(g["imp"][week]))) / 1440)
    per_day = {"peak": float(imp[week & pk].sum() / 60 / days_w), "off_peak": float(imp[week & ~pk].sum() / 60 / days_w)}
    per_day_exp = {"peak": float(exp[week & pk].sum() / 60 / days_w), "off_peak": float(exp[week & ~pk].sum() / 60 / days_w)}
    interval = int((conn or {"data": {}})["data"].get("mdi_interval_min", 30))

    def block_means(sel: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The MDI blocks over the selected minutes with 80 % of their minutes known (their start, epoch seconds) and their mean import."""
        if not sel.any():
            return np.zeros(0, dtype=np.int64), np.zeros(0)
        uniq, inv = np.unique(g["ts"][sel] // (interval * 60), return_inverse=True)
        n_known = np.bincount(inv, weights=k_imp[sel].astype(float), minlength=len(uniq))
        sums = np.bincount(inv, weights=imp[sel], minlength=len(uniq))
        full = n_known >= interval * 0.8
        return uniq[full] * interval * 60, sums[full] / np.maximum(n_known[full], 1.0)

    _, means = block_means(in_month)
    m0 = float(means.max()) if len(means) else 0.0
    # the month's expected peak (round 7, engine-optimiser-02, engine-plans-live-07): the second-highest local day's peak of the seven days
    # before t0, so one odd day does not set it. At a month's start M0 is near nothing, and every kW above the floor was priced as a whole
    # month's demand charge: the plan drained the battery off-peak on the 1st to hold a peak the month's weekdays then passed anyway (Karachi:
    # 797 kW on Sunday 1 Nov, 902 kW by the 4th). The rest of a month almost surely reaches the peak of the second-busiest of its last seven
    # days, so a kW under it costs nothing; it bounds M (and M2, the next month's peak) from below. Once the seven days lie inside the month,
    # the month so far covers it
    starts, wk_means = block_means(week)
    m_exp = 0.0
    if len(starts):
        days = (starts + 5 * 3600) // 86400
        day_peaks = np.sort(np.array([float(wk_means[days == d].max()) for d in np.unique(days)]))[::-1]
        m_exp = float(day_peaks[1]) if len(day_peaks) >= 2 else 0.0
    blk_start = t0 // (interval * 60) * (interval * 60)
    el_known = k_imp & (g["ts"] >= blk_start)
    n_el_steps = (t0 - blk_start) // 900
    g_el = float(imp[el_known].mean()) if el_known.any() else 0.0
    return {"mtd": mtd, "per_day": per_day, "M0": m0, "M_exp": m_exp, "G_elapsed": g_el, "n_elapsed": int(n_el_steps), "mtd_export": mtd_exp,
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


#: the fuel kinds that come by pipe: no tank to run dry, so the plan does not ration them (an explicit kind, never a tank of 0 litres)
PIPED_FUELS = ("gas_captive", "lng_captive")


def fuel_level(site: dict, t0: int) -> dict | None:
    """The fuel the genset's tank holds at t0, for the plan to ration (Part 4.1 "fuel available", carried between ticks as Part 4.8 asks; round
    7, engine-optimiser-05). Nothing wrote the carried level before, so every tick planned on a full tank, and `or 1e9` made a tank of 0 litres,
    or a carried level of 0, unlimited fuel. `basis`:
    - `carried`: the level the last tick planned on (`state.fuel_l` at `state.fuel_ts`), plus the fills logged since (the fuel log, by the time
      each was logged), less the litres burnt since (`_fuel_burnt`), never under empty nor over the tank;
    - `assumed_full`: no level carried yet: a full tank, as before (the first tick of a site, or one whose level was never written);
    - `unknown`: a diesel tank of no size (tank_l 0): there is nothing to ration against, and the plan says so;
    - `piped`: captive gas or RLNG by pipe (`PIPED_FUELS`): not rationed.
    `litres` and `tank_l` are null for the last two. None without a genset."""
    g = (site.get("assets") or {}).get("genset")
    if not g:
        return None
    if g.get("fuel_kind", "hsd") in PIPED_FUELS:
        return {"basis": "piped", "litres": None, "tank_l": None}
    tank = float(g.get("tank_l") or 0.0)
    if tank <= 0:
        return {"basis": "unknown", "litres": None, "tank_l": None}
    st = site.get("state") or {}
    if st.get("fuel_l") is None:
        return {"basis": "assumed_full", "litres": tank, "tank_l": tank}
    level = float(st["fuel_l"])
    ts0 = st.get("fuel_ts")
    if ts0 is not None and int(ts0) < int(t0):
        level += _fuel_filled(site["id"], int(ts0), int(t0)) - _fuel_burnt(site["id"], g, int(ts0), int(t0))
    return {"basis": "carried", "litres": min(tank, max(0.0, level)), "tank_l": tank}


def _fuel_filled(site_id: str, a: int, b: int) -> float:
    """The litres logged as put in the tank (POST .../genset/fuel) with their log time in (a, b]: each fill counts once, in the first tick after
    it was logged, whatever day it names."""
    with store.connect() as con:
        rows = con.execute("SELECT ts, detail FROM events WHERE site_id = ? AND type = 'fuel_log' AND ts >= ?",
                           (site_id, _iso(a - 86400))).fetchall()
    total = 0.0
    for r in rows:
        at = _ts(r["ts"])
        if at is None or not (a < at <= b):
            continue
        try:
            total += max(0.0, float(json.loads(r["detail"] or "{}").get("litres") or 0.0))
        except (TypeError, ValueError):
            continue
    return total


def _fuel_burnt(site_id: str, g: dict, a: int, b: int) -> float:
    """The litres the genset burnt in [a, b): metered where the minute carries litres; for a run with no litres read, the fuel curve at its
    metered power (three quarters of its rating where none is read either); and for the minutes with no genset reading at all, the litres the
    plan in force at `a` planned for them, so a site with no genset signal still burns its tank down as it was asked to run. Each reading
    stands for the minutes up to the next one, at most the source's own interval (the median between its readings: a 5-minute source's row
    is five minutes) and never more than GAP_MINUTES."""
    d = telemetry.raw(site_id, a, b, ("genset_running", "genset_power_w", "genset_fuel_l"))
    ts = np.asarray(d["ts"], dtype=np.int64)
    flag, p_kw, fuel = d["genset_running"], d["genset_power_w"] / 1000.0, d["genset_fuel_l"]
    S, ga, gb = float(g["rated_kw"]), float(g["a"]), float(g["b"])
    burnt = float(np.nansum(fuel))
    known = ~(np.isnan(flag) & np.isnan(p_kw) & np.isnan(fuel))
    span = np.zeros(len(ts))
    if len(ts):
        every = min(float(np.median(np.diff(ts[known]))), GAP_MINUTES * 60.0) if known.sum() > 1 else 60.0
        span = np.minimum(np.diff(np.append(ts, b)), max(60.0, every)) / 60.0
        on = np.where(~np.isnan(flag), flag > 0.5, np.where(~np.isnan(p_kw), p_kw > 0.0, np.nan_to_num(fuel) > 0.0)) & known
        curve = on & np.isnan(fuel)
        kw = np.where(np.isnan(p_kw), 0.75 * S, p_kw)
        burnt += float(np.sum((ga * kw[curve] + gb * S) / 60.0 * span[curve]))
    # the minutes no reading covers, per 15-minute step, at the planned litres of that step
    covered: dict[int, float] = {}
    for t, m in zip(ts[known] // 900 * 900, span[known]):
        covered[int(t)] = covered.get(int(t), 0.0) + float(m)
    steps = range(a // 900 * 900, b, 900)
    unread = {s: max(0.0, (min(s + 900, b) - max(s, a)) / 60.0 - covered.get(s, 0.0)) for s in steps}
    if any(v > 1e-9 for v in unread.values()):
        plan = active_at(site_id, a)
        planned = {int(x["t"]): float(x.get("litres") or 0.0) for x in (plan or {}).get("steps") or []}
        burnt += sum(planned.get(s, 0.0) * m / 15.0 for s, m in unread.items())
    return burnt


RELEASE_REASONS = {"pause_control": "control_paused", "force_native": "released_to_native"}
#: the held reason of a day in a native block of the switchback calendar (plan 8.2, VER-08: a baseline block plans "native"); it holds the plan as a
#: pause does, but comes from the calendar, not from an override, so no person resumes it: the block's end does
SWITCHBACK_REASON = "switchback_native"
#: every held reason under which no plan is in force because the inverter runs its own settings
RELEASED_REASONS = (*RELEASE_REASONS.values(), SWITCHBACK_REASON)


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
    """The pause (pause_control) or release to native (force_native) in force at `at` (now by default), else, on a day in a native block of the
    switchback calendar, that block (`switchback_release`), else None. While one is in force no plan is in force: the tick stores its plan as held
    and the edge is sent a release instead of the slots (Part 7.3). A person's pause or release comes first: it names who placed it and can be
    resumed, which a calendar block cannot."""
    at = int(at if at is not None else datetime.now(timezone.utc).timestamp())
    ovs = sites.overrides(site_id)
    for kind in ("pause_control", "force_native"):
        for o in ovs:
            if o["kind"] == kind and bool(_override_steps(o, np.array([forecast.align(at)]))[0]):
                return {"reason": RELEASE_REASONS[kind], "kind": kind, "until": o["until_ts"], "author": o["author"], "override_id": o["id"]}
    return switchback_release(site_id, forecast.align(at))


def offgrid_c_next(g_asset: dict, fuel_price: float, load: np.ndarray, pv: np.ndarray) -> float:
    """The off-grid terminal value's price of a kWh left in the battery at the horizon's end: the ledger's rule (verify.compute_day's v_end), the
    genset's all-in cost per kWh at its after-dark load ratio (verify.night_load_ratio), here on the forecast load and PV of the horizon's last
    day. Not at 75 % load: after dark the set carries a small load near its minimum, where its no-load burn and run-hour cost weigh on every kWh,
    so the plan and the savings ledger value stored energy alike."""
    from . import verify  # verify imports plans at module load

    ratio = verify.night_load_ratio(g_asset, {"pv": np.asarray(pv, dtype=float), "load": np.asarray(load, dtype=float)})
    return optimise._gen_marginal({"S": g_asset["rated_kw"], "a": g_asset["a"], "b": g_asset["b"], "c_fuel": float(fuel_price),
                                   "c_om": g_asset["c_om_rs_per_kwh"], "c_runhr": g_asset["c_runhr_rs"]}, ratio)


def fallback_floor_pct(site: dict) -> float:
    """The fallback block's floor (`min_soc_floor_pct`): the site's reserve policy, which the edge enforces whatever the slots say (R6 I2)."""
    return float((site["data"].get("reserve_policy") or {}).get("min_soc_pct", 20.0))


def bms_low_pct(site: dict) -> float:
    """The battery's own cut-off (its soc_min) in percent."""
    return float(((site.get("assets") or {}).get("battery") or {}).get("soc_min", 0.1)) * 100


def delivered_floor_pct(site: dict) -> float:
    """The state of charge the delivered table holds with the grid up: delivery.compile_mode_a clamps every slot's floor to
    max(BMS low + 2, fallback floor), and the fallback floor is enforced by the edge whatever the slots say. The plan holds the same floor on its
    grid-up steps (round 7, engine-delivery-02): before, it planned down to the BMS soc_min (10 %) against slots that all stop at 20 %, so its
    peak discharges and its savings counted on energy the device keeps back. In an outage the battery-inverter carries the load to its own
    cut-off, so there the plan keeps the BMS soc_min."""
    return max(bms_low_pct(site) + 2.0, fallback_floor_pct(site))


def terminal_cal_hold(b_cal: np.ndarray, c_imp: np.ndarray, load: np.ndarray, pv: np.ndarray, off_grid: bool) -> float:
    """The terminal value's c_cal_hold (Part 4.4): the calendar cost of holding a kWh from the horizon's end until the next peak uses it, the
    sum of the per-step slopes over the steps between (round 7, engine-optimiser-10: it was the mean slope of 32 steps, one 15-minute step's
    cost, Rs 0.03 to 0.1 against Rs 1 to 6 for the hours the energy waits). The next peak is the day after's dearest step, c_next's own (the
    horizon's last day stands for the day after, as it does for c_next), and off-grid the first step whose load PV does not cover; the slopes
    are those of the same hours of the horizon's last day."""
    n = min(96, len(b_cal))
    if not n:
        return 0.0
    if off_grid:
        short = np.flatnonzero(np.asarray(load, dtype=float)[-n:] > np.asarray(pv, dtype=float)[-n:] + 1e-9)
        hold = int(short[0]) if len(short) else n
    else:
        hold = int(np.argmax(np.asarray(c_imp, dtype=float)[-n:]))
    return float(np.sum(np.asarray(b_cal, dtype=float)[-n:][:hold]))


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
    if regime in tariffs.EXPORT_REGIMES:
        # the regime billed today (tariffs.regime_on): none outside the agreement (a zero-export site that nets nothing), net billing after
        # a legacy agreement ends
        regime = tariffs.regime_on(conn, _local(t0).date())
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
         # lost load is never valued under the dearest import price of the horizon (round 7, engine-optimiser-01): a VoLL the site saved under it
         # (the setting accepts 0) made a manual floor's slack and an outage's shortfall cheaper than the grid
         "voll": max(float(d.get("voll_rs_per_kwh", 500)), float(np.max(vec["c_imp"])) if len(vec["c_imp"]) else 0.0), "reserve_threshold": float((d.get("reserve_policy") or {}).get("p_threshold", 0.2)),
         "icp": vec["icp"], "regime": regime, "tariff_notices": vec.get("tariff_notices", []),
         # PV and the battery reach the AC side through the inverter: its kW AC bounds both, and the reserve (round 7, engine-plans-live-06)
         "inverter_kw_ac": (float(a["inverter"]["kw_ac"]) if (a.get("inverter") or {}).get("kw_ac") else None)}
    if vec["mdi"] and mtd:
        month_end = int(vec["billing_month_end_step"]) if "billing_month_end_step" in vec else (int(np.argmin(in_month)) if not in_month.all() else steps)
        p["mdi"] = {"rate": vec["mdi"]["rate_rs_per_kw"], "M0": mtd["M0"], "floor": vec["mdi"]["floor_kw"], "n_mdi": vec["mdi"]["interval_steps"],
                    "G_elapsed": mtd["G_elapsed"], "n_elapsed": mtd["n_elapsed"], "month_end_step": month_end,
                    # the month's expected peak (month_to_date): M and M2 are never bounded under it (round 7, engine-optimiser-02)
                    "M_exp": mtd.get("M_exp", 0.0)}
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
    # each kind's own window, for the plan table's Flags column (round 7, pages-B-07; R8 2.9): every step names the overrides it lies inside
    p["override_kinds"] = {k: m for k, m in mask.items() if m.any()}
    p["overrides"] = [{"kind": o["kind"], "value": o["value"], "until": o["until_ts"], "author": o["author"]} for o in ovs]
    gen = None
    # each step's local (Pakistan) day from t0's: the genset's run-hour cap and the battery's cycle cap are per day
    local_days = np.array([(_local(int(x)).date() - _local(t0).date()).days for x in ts])
    if g_asset and d.get("genset_start_control", "none") != "none":
        # the fuel the tank holds (fuel_level; round 7, engine-optimiser-05): piped gas and a tank of no size are not rationed
        fuel = fuel_level(site, t0)
        p["fuel"] = fuel
        kind = g_asset.get("fuel_kind", "hsd")
        price, _src = tariffs.fuel_price(kind, _local(t0).date())
        carried = _carried_genset(site["id"], t0)
        control = d.get("genset_start_control", "manual")
        if carried["unknown"] and control == "auto_on_grid_loss" and len(g) and g[0] < 0.5:
            carried = {**carried, "u_prev": 1, "elapsed": 99}  # in an outage the panel has already started the set; this fixes nothing
        c_om = g_asset["c_om_rs_per_kwh"] + (SELFGEN_DUTY_RS_PER_KWH if g_asset["rated_kw"] > 400 and (d.get("lat", 31) > 28) else 0.0)
        gen = {"S": g_asset["rated_kw"], "f_min": g_asset["f_min"], "a": g_asset["a"], "b": g_asset["b"], "c_fuel": price, "c_start": g_asset["c_start_rs"],
               "c_runhr": g_asset["c_runhr_rs"], "c_om": c_om, "UT": max(1, int(math.ceil(g_asset["min_up_h"] * 4))),
               "DT": max(1, int(math.ceil(g_asset["min_down_h"] * 4))), "u_prev": carried["u_prev"], "elapsed": carried["elapsed"],
               "cap_hours": g_asset["cap_hours_per_day"], "hours_today": carried["hours_today"], "day_index": local_days,
               "fuel_available": float(fuel["litres"]) if fuel and fuel["litres"] is not None else 1e9, "parallel": g_asset["parallel_allowed"],
               "control": control, "hold": mask["genset_hold"], "unknown": carried["unknown"],
               "soc_on": sites.genset_start_soc(d)}  # twin.plant_of's start threshold
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
               # with the grid down the set's output follows the load and its minimum-load surplus charges the battery on its own, with no slot
               # asking for it (twin.simulate's outage branch), so the plan counts on that much and no more (optimise.genset_surplus_steps)
               "a_gen_surplus": bool(d.get("gen_charge_allowed", True)),
               "c_cyc": optimise.wear_cost(b_asset),
               "b_cal": optimise.calendar_slope(temp, b_asset["replacement_cost_per_kwh"]) if d.get("calendar_cost_on", True) else np.zeros(steps),
               "charge_block": mask["charge_block"],
               # the floor the delivered table and the fallback hold with the grid up (round 7, engine-delivery-02); never above soc_max
               "soc_floor_grid": min(float(b_asset["soc_max"]), delivered_floor_pct(site) / 100.0),
               # the site's daily cycle cap (round 7, brief-vs-build-04): kWh discharged per local day, at most cycle_cap_per_day times E_nom (a
               # cycle as the battery page counts it), less what the battery discharged since midnight on t0's day
               "cycle_cap_kwh": float(d.get("cycle_cap_per_day", 1.5)) * e_nom,
               "discharged_today_kwh": float(bs.get("discharged_today_kwh") or 0.0), "day_index": local_days}
        p["battery"] = bat
        # what the plan starts from, for the plan's summary (round 7, engine-optimiser-07): the state service's estimate, its uncertainty and the
        # age of the reading it read; a reading older than one control period, or none (the plan then starts from an assumed state), is stale
        age = bs.get("reading_age_min")
        p["battery_state"] = {"soc_pct": round(soc * 100, 1), "sigma_pct": bs.get("sigma_pct"), "age_min": age,
                              "stale": bool(bs.get("stale", True)) if bs.get("soc_pct") is not None else True,
                              "anchor_soc_pct": bs.get("anchor_soc_pct") if bs.get("anchor_due") else None, "derate": None}
        # the pack's temperature derating (round 7, brief-vs-build-05; R6 5.7: "a derate lowers P_dis at the next solve"): the limits its fresh
        # temperature reading allows hold for the plan's first two hours, the rated powers after (each tick reads the temperature again)
        lim_ch = float(bs.get("p_ch_limit_kw", b_asset["p_ch_kw"]))
        lim_dis = float(bs.get("p_dis_limit_kw", b_asset["p_dis_kw"]))
        if lim_ch < float(b_asset["p_ch_kw"]) - 1e-9 or lim_dis < float(b_asset["p_dis_kw"]) - 1e-9:
            hold = min(steps, battery_service.DERATE_HOLD_STEPS)
            bat["P_ch_t"], bat["P_dis_t"] = np.full(steps, float(b_asset["p_ch_kw"])), np.full(steps, float(b_asset["p_dis_kw"]))
            bat["P_ch_t"][:hold], bat["P_dis_t"][:hold] = lim_ch, lim_dis
            p["battery_state"]["derate"] = {"temp_c": bs.get("temp_c"), "p_ch_kw": lim_ch, "p_dis_kw": lim_dis, "until": int(t0 + hold * 900)}
        if floor_pct.any():
            # every manual floor (a caller that drops the overrides drops this key), and its two parts
            p["reserve_manual"] = floor_pct / 100.0 * e_nom
            p["reserve_manual_hold"] = hold_pct / 100.0 * e_nom
            p["reserve_manual_force"] = force_pct / 100.0 * e_nom
        c_next = float(np.max(vec["c_imp"][-96:])) if not off_grid else p["c_short"]
        if off_grid and gen:
            c_next = offgrid_c_next(g_asset, gen["c_fuel"], L[-96:], PV[-96:])
        # the reserve for the first outage beyond the horizon, held where the site's reserve policy asks for it: the chance that an outage starts
        # on the horizon's last day (the forecast for the day after) at or above its threshold (round 7, engine-plans-live-12: a hard-coded
        # 0.5 outages a day decided it, and the site's setting did nothing). Two hours of the critical load, never above what the battery and the
        # inverter can deliver (engine-plans-live-06)
        p_next = 1.0 - float(np.prod(1.0 - np.clip(p_start[-96:], 0.0, 1.0))) if len(p_start) else 0.0
        # Part 4.4: at a grid-connected site c_next is the dearest import price of the day after, or the genset's marginal cost where that day's
        # outage chance reaches the site's threshold (round 7, engine-optimiser-11: always the import price, so at an outage-heavy feeder a kWh
        # left at the horizon's end, which a likely outage would take from the set at about Rs 130, was valued at a Rs 50 peak, and the plan
        # ran the battery down towards hour 48). The dearer of the two: the kWh serves whichever use comes. Not behind an auto-start panel: there
        # the set starts within a minute of the grid's loss and follows the load (twin.simulate), so a stored kWh spares it nothing beyond the
        # start delay the reserve already covers (engine-plans-live-03), and Lahore's plan held its battery for an outage it never used
        c_next_basis = "genset" if off_grid and gen else ("lost_load" if off_grid else "import")
        if (not off_grid and gen and gen["control"] != "auto_on_grid_loss" and p_next >= p["reserve_threshold"]
                and p["c_short"] > c_next):
            c_next, c_next_basis = float(p["c_short"]), "genset"
        kw_next = np.minimum(Lc[-96:], float(b_asset["p_dis_kw"]))
        if p.get("inverter_kw_ac"):
            kw_next = np.minimum(kw_next, float(p["inverter_kw_ac"]))
        r_next = float(np.mean(kw_next)) * 2.0 / b_asset["eta_d"] if (off_grid or p_next >= p["reserve_threshold"]) and len(kw_next) else 0.0
        p["terminal"] = {"c_next": c_next, "c_next_basis": c_next_basis, "H_peak": 4.0, "R_next": r_next,
                         "c_cal_hold": terminal_cal_hold(bat["b_cal"], vec["c_imp"], L, PV, off_grid), "p_next": p_next}
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
    r_gap = res.get("r_gap") or {}
    dump = s.get("W") if s.get("W") is not None else np.zeros(T)
    # the override windows by kind (build_problem); a shortened (relaxed 24-hour) problem keeps the full masks, and only t < T is read
    kinds = p.get("override_kinds") or {}

    E_up = float(res.get("E_floor_grid") or 0.0)

    def floor_pct(t: int) -> float | None:
        """The floor the plan holds at step t: soc_min, raised by the reserve rows and the manual floor where those rows exist and, with the grid
        up, by the floor the delivered table holds (round 7, engine-delivery-02), never above soc_max (the reserve row's 2-sigma widening can
        push its target past the band). A reserve row is soft: the plan holds its target less the slack it accepts at the row's price (round 7,
        engine-plans-live-03: behind an AMF panel a failed start's 30 minutes, priced at one start in twenty, shipped as a 36 % floor the plan
        never meant to hold)."""
        if not En:
            return None
        held = 0.0
        if t in r_rows:
            held = max(held, float(res["R"][t]) - float(r_rows[t]))
        if t in r_gap:
            held = max(held, float(res["R_gap"][t]) - float(r_gap[t]))
        f = bat["soc_min"] * En + held
        if float(p["g"][t]) > 0.5:
            f = max(f, E_up)
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
                    "cost_rs": round(cost, 2), "cum_rs": round(cost_cum, 2), "reason": rsn["reason"], "detail": rsn["detail"],
                    "flags": [k for k, m in kinds.items() if bool(m[t])]})
    return out


#: the wrong-plan guard's tolerances: state of charge (a fraction of the battery: the twin's minutes against the plan's quarters; at the Lahore demo
#: a 3 kW peak shave past a 20 % floor dipped 2.1 points, which is not a wrong plan), energy over the horizon (kWh, and a share of the critical
#: energy of the forecast outages) and an MDI block's mean (kW)
GUARD_SOC_TOL = 0.05
GUARD_KWH_TOL, GUARD_OUTAGE_SHARE = 2.0, 0.05
GUARD_MDI_TOL_KW = 1.0


def _guard(site: dict, p: dict, compiled: dict, steps_out: list[dict], res: dict | None = None) -> str | None:
    """The wrong-plan guard (Part 7.1 step 8, R6 rule C2): check the plan and its compiled table, replayed through the twin on the plan's own
    forecast, and refuse the plan (the previous one stays in force) when either would do what the plan may not. The first rule broken is the
    reason:
    - `unsafe_table`: the compiled table is not safe to repeat with no new plan (delivery.table_safety, step 6: a floor under the fallback's,
      a discharging tail under the day's reserve, or a whole day's table that meets a forecast outage short of the reserve when run again);
    - `slot_floor_below_fallback`: a slot's floor is under the fallback's minimum state of charge (the whole-table safety of step 6);
    - `replayed_soc_below_floor`: with the grid up, the table discharges the battery under that fallback floor (an outage may use the reserve
      down to the battery's own floor; a grid-up hour may not);
    - `unserved_in_outage`: in the forecast outages the table leaves more critical load unserved than the plan did (the reserve was spent before);
    - `mdi_cap_exceeded`: the plan's own grid draw over an MDI block, with any load it sheds while the grid is up added back (the site's meter
      reads that load), exceeds the plan's MDI cap (`res["M"]`; the first block carries its minutes already elapsed, as the plan's row does);
    - `sheds_load_with_grid_up`: the plan sheds load with the grid up that the site would in fact serve from the grid (the replay serves it);
    - `genset_slot_with_grid`: a genset slot lies wholly where the grid is expected present, at a site whose genset may not run beside the grid;
    - `export_not_allowed`: an export slot at a connection that may export nothing.
    None when every rule holds, or the site has no battery (no table to replay)."""
    bat = p.get("battery")
    if not bat:
        return None
    if (compiled.get("safety") or {}).get("ok") is False:  # Part 7.1 steps 6 and 8: a table that is not safe to repeat is not signed
        return "unsafe_table"
    T = p["T"]
    slots = [s for d in compiled["days"] for s in d["slots"]]
    fallback = float(((compiled.get("fallback") or {}).get("min_soc_floor_pct") or 0.0)) / 100.0
    if any(float(s["soc_floor_pct"]) / 100.0 < fallback - 1e-6 for s in slots):
        return "slot_floor_below_fallback"
    ts = np.repeat(p["ts"], 15) + np.tile(np.arange(15) * 60, T)
    series = {"pv": np.repeat(p["PV"], 15), "load": np.repeat(p["L"], 15), "crit": np.repeat(p["Lc"], 15), "grid": np.repeat(p["g"], 15),
              "ts": ts, "temp": np.full(len(ts), 30.0)}
    pol = twin.slots_policy(slots, ts)
    plant = twin.plant_of(site)
    soc0 = bat["E0"] / bat["E_nom"]
    sim = twin.simulate(series, pol, plant, soc0)
    up = series["grid"] > 0.5
    # the fallback floor with the grid up: a battery already under it (a start below the band, or the end of an outage) may stay there, but the
    # table must not discharge it further
    if np.any(up & (sim["bat"] > 1e-3) & (sim["soc"] < fallback - GUARD_SOC_TOL)):
        return "replayed_soc_below_floor"
    planned = {k: np.array([float(s.get(k) or 0.0) for s in steps_out[:T]]) for k in ("unserved_kw", "grid_present", "grid_import_kw")}
    down_q = planned["grid_present"] < 0.5
    unserved_out = float(sim["unserved"][~up].sum() / 60)
    planned_out = float(planned["unserved_kw"][down_q].sum() * H)
    crit_out = float(series["crit"][~up].sum() / 60)
    if unserved_out > planned_out + max(GUARD_KWH_TOL, GUARD_OUTAGE_SHARE * crit_out):
        return "unserved_in_outage"
    shed_q = np.where(down_q, 0.0, planned["unserved_kw"])
    caps = (res or {}).get("M") or []
    mdi = p.get("mdi")
    if mdi and caps:
        n = max(1, int(mdi["n_mdi"]))
        draw = planned["grid_import_kw"] + shed_q
        blocks = (np.asarray(p["ts"]) // (n * 900)).astype(np.int64)
        month_end = int(mdi.get("month_end_step", T))
        for bid in np.unique(blocks):
            st = np.flatnonzero(blocks == bid)
            cap = caps[0] if st[0] < month_end else (caps[1] if len(caps) > 1 else None)
            if cap is None:
                continue
            elapsed = float(mdi.get("G_elapsed") or 0.0) * float(mdi.get("n_elapsed") or 0) if st[0] == 0 else 0.0
            if (float(draw[st].sum()) + elapsed) / n > cap + GUARD_MDI_TOL_KW:
                return "mdi_cap_exceeded"
    if float(shed_q.sum() * H) > float(sim["unserved"][up].sum() / 60) + GUARD_KWH_TOL:
        return "sheds_load_with_grid_up"
    if not plant.parallel and not p.get("off_grid"):
        g_q = np.asarray(p["g"], dtype=float)
        for s in slots:
            if s.get("genset"):
                sel = (np.asarray(p["ts"]) >= s["start_ts"]) & (np.asarray(p["ts"]) < s["end_ts"])
                if sel.any() and bool(np.all(g_q[sel] > 0.5)):
                    return "genset_slot_with_grid"
    if float(p.get("X_max") or 0.0) <= 0.0 and any(s.get("mode") == "export" for s in slots):
        return "export_not_allowed"
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


def _ask_anchor(site: dict, p: dict, t0: int) -> None:
    """The charge to the pack's top the battery service asks for to re-anchor its estimate (round 7, engine-optimiser-15; R6 5.7), put to a live
    tick's problem (a hindsight solve of a past day, verify._bound, builds the problem without it). The first active plan that schedules it fixes
    when (`anchor_by` in the site's state): re-planned every 15 minutes, a charge that costs the same tonight as tomorrow night would otherwise
    move to the later night every time and never happen."""
    bat, bs = p.get("battery"), p.get("battery_state") or {}
    if not bat or not bs.get("anchor_soc_pct"):
        return
    bat["anchor_soc"] = float(bs["anchor_soc_pct"]) / 100.0
    by = (site.get("state") or {}).get("anchor_by")
    if by and int(by) > t0:
        bat["anchor_by"] = int(by)


#: a solve that stopped at its wall-clock limit with a gap to the solver's bound above KEEP_GAP, or an unknown gap (a relaxed retry after a first
#: solve that found no plan in time), does not replace a plan in force made at most KEEP_MAX_AGE_S before, solved within KEEP_GAP, on the same
#: observed outage and overrides: it is kept for reading, held with TIME_LIMIT_HELD, and that plan stays in force (round 7, engine-optimiser-08).
#: The limits are wall-clock time (HANDOFF 8.A.3), so a loaded machine otherwise put a 30 % plan, and the table and genset requests that follow
#: it, in place of an optimal one made 15 minutes before
KEEP_GAP = 0.05
KEEP_MAX_AGE_S = 2 * 3600
TIME_LIMIT_HELD = "time_limited_gap"


def _kept_by_time_limit(site_id: str, res: dict, t0: int, summary: dict) -> dict | None:
    """`{plan_id, mip_gap, gap_limit}` of the plan in force that a time-limited solve far from its bound leaves in force (see KEEP_GAP), else
    None: the solve finished or came within KEEP_GAP, nothing is in force, the plan in force is older or itself no closer, or the outage
    observed or the overrides changed since it was made (it did not plan for them)."""
    limited = bool(res.get("time_limited", res.get("status") == "feasible")) or bool(res.get("first_time_limited"))
    gap = None if res.get("first_time_limited") else res.get("mip_gap")
    if not limited or (gap is not None and float(gap) <= KEEP_GAP):
        return None
    prev = current(site_id, t0)  # the plan in force at the tick's own time (a past tick, as the demo's history makes, reads its own day's)
    if prev is None:
        return None
    made, until = _ts(prev.get("belief_time")), _ts(prev.get("valid_until"))
    if made is None or not 0 <= t0 - made <= KEEP_MAX_AGE_S or until is None or until <= t0:
        return None
    ps = prev.get("summary") or {}
    psv = ps.get("solver") or {}
    if _plan_time_limited(ps) and (psv.get("first_time_limited") or psv.get("mip_gap") is None or float(psv["mip_gap"]) > KEEP_GAP):
        return None

    def observed(o):
        return bool(o), (o or {}).get("since")

    def same(a, b) -> bool:
        return json.dumps(a or [], sort_keys=True, default=str) == json.dumps(b or [], sort_keys=True, default=str)

    if observed(ps.get("outage_now")) != observed(summary.get("outage_now")) or not same(ps.get("overrides"), summary.get("overrides")):
        return None
    return {"plan_id": prev["id"], "mip_gap": gap, "gap_limit": KEEP_GAP}


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
    _ask_anchor(site, p, t0)
    fuel = p.get("fuel")
    if save and fuel and fuel["basis"] in ("carried", "assumed_full") and int((site.get("state") or {}).get("fuel_ts") or 0) <= t0:
        # the level this tick plans on is the next tick's start (Part 4.8; round 7, engine-optimiser-05), written whatever the solve does: the
        # fills and burn up to t0 are in it. Never moved back by a tick for an earlier time
        sites.put_state(site_id, fuel_l=round(float(fuel["litres"]), 3), fuel_ts=int(t0))
    res = optimise.solve_with_policy(p, time_limit=time_limit)
    if res.get("sol") is not None and len(res["sol"]["G"]) != p["T"]:
        p = optimise.shorten(p, len(res["sol"]["G"]))  # the relaxed retry solved a shorter horizon: steps, slots and the guard follow it
    brand = ((site.get("assets") or {}).get("inverter") or {}).get("brand", "deye")
    # a kind that does not apply to the site (no PV, an off-grid site's outages) has no stage and is left out (forecast.not_applicable)
    stages = {k: bundle[k]["stage"] for k in ("pv", "load", "outage") if bundle[k].get("stage") is not None}
    stage = min(v for k, v in stages.items() if k in ("pv", "load"))
    tier = site.get("tier", "monitor")
    summary = {"stage": stage, "stages": stages,
               "tier": tier, "recommendation": tier == "monitor", "brand": brand, "notes": res.get("notes", []),
               "outage_now": bundle["outage"].get("observed"), "adherence": bundle["outage"].get("adherence"),
               "icp": {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in (p.get("icp") or {}).items()},
               "overrides": p.get("overrides", []), "genset_state_unknown": bool(p.get("genset", {}) and p["genset"].get("unknown")),
               # the fuel the plan rationed (fuel_level): carried, a full tank assumed, unknown (a tank of no size) or piped
               "fuel": ({"basis": fuel["basis"], "litres": round(float(fuel["litres"]), 1) if fuel["litres"] is not None else None,
                         "tank_l": fuel["tank_l"]} if fuel else None),
               # where an anchor charge is asked for, the end of the hour it is planned for and the kWh it stays short of the target
               "battery_state": ({**p["battery_state"], "anchor_t": (res.get("anchor") or {}).get("t"),
                                  "anchor_short_kwh": (res.get("anchor") or {}).get("short_kwh")}
                                 if p.get("battery_state") and p["battery_state"].get("anchor_soc_pct") else p.get("battery_state")),
               "status_label": _status_label(res),
               "solver": {"status": res["status"], "variables": res.get("variables"), "rows": res.get("rows"), "binaries": res.get("binaries"),
                          "relaxed": res.get("relaxed"), "message": res.get("message"), "shed_rule_resolve": res.get("shed_rule_resolve"),
                          # the limits are wall-clock time (HANDOFF 8.A.5): whether the published solve stopped at its limit, the gap it reached
                          # and the limit itself, so a time-limited plan is visible (time_limited_recent) and can be solved again when idle
                          "time_limited": bool(res.get("time_limited", res["status"] == "feasible")), "mip_gap": res.get("mip_gap"),
                          "time_limit_s": float(time_limit), "solve_seconds": res.get("solve_seconds"),
                          "threshold_repair": res.get("threshold_repair"),
                          # a relaxed 24-hour retry: why the first solve failed, and whether it stopped at its limit (it then counts as
                          # time-limited too); null where no retry ran
                          "retry_of": res.get("retry_of"), "first_time_limited": res.get("first_time_limited"),
                          "first_solve_seconds": res.get("first_solve_seconds")},
               "horizon_steps": p["T"], "control_release": release,
               # a tariff version awaiting its notification the horizon reaches: the plan is priced with the one still in force (engine-tariffs-06)
               "tariff_notices": p.get("tariff_notices", [])}
    # how many of the plans made in the 24 hours to this one, this one included, stopped at their time limit (round 7, engine-plans-live-19:
    # time_limited_recent counted them and nothing read it), for the plan page; a failed solve counts as a plan, never as time-limited
    recent = time_limited_recent(site_id, 24.0, now)
    solved = res["status"] not in ("not_solved", "refused")
    summary["time_limits_24h"] = {"hours": 24.0, "plans": recent["plans"] + 1,
                                  "time_limited": recent["time_limited"] + int(solved and _plan_time_limited(summary))}
    if res["status"] in ("not_solved", "refused"):
        # a code the web app words, never the solver's own text (round 7, frontend-states-formats-07); the message stays in summary.solver
        reason = failure_code(res)
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
        return {"status": "failed", "reason": reason, "control_release": release, "in_force": None if release else _in_force_id(site_id, now)}
    steps_out = _steps_out(p, res, bundle)
    # the slots' floors are max(bms_low + 2, fallback floor): delivered_floor_pct, the floor the plan held on its grid-up steps. A horizon that
    # crosses the billing month's end compiles the new month's days with its own cap, M2 (round 7, engine-optimiser-02: every slot carried M,
    # so the new month's shaved to the old month's peak). The month ends at a local midnight and slots are per local day, so the two compiles
    # give the days a single one would, each with its month's cap
    Ms = res.get("M") or []
    split = int((p.get("mdi") or {}).get("month_end_step", p["T"])) if len(Ms) > 1 else p["T"]
    compiled = delivery.compile_mode_a(steps_out[:split], brand, p.get("battery"), fallback_floor_pct(site), bms_low_pct(site),
                                       Ms[0] if Ms else None, site=site)
    if split < len(steps_out):
        nxt = delivery.compile_mode_a(steps_out[split:], brand, p.get("battery"), fallback_floor_pct(site), bms_low_pct(site), Ms[1], site=site)
        compiled = {**compiled, "days": compiled["days"] + nxt["days"]}
    guard = _guard(site, p, compiled, steps_out, res)
    summary.update({"breakdown": res["breakdown"], "objective_rs": res["objective_rs"], "mdi_cap_kw": round(Ms[0], 1) if Ms else None,
                    # the next billing month's cap where the horizon crosses into it (its days' slots carry it), else null
                    "mdi_cap_next_kw": round(Ms[1], 1) if len(Ms) > 1 else None,
                    "v_end": round(res.get("v_end", 0.0), 2), "guard": guard})
    # a time-limited solve far from its bound leaves a recent, closer plan in force (_kept_by_time_limit; round 7, engine-optimiser-08)
    kept = None if (release or guard) else _kept_by_time_limit(site_id, res, t0, summary)
    if kept:
        summary["time_limit_kept"] = kept
    # a pause or a release to native means no plan is in force: the plan is kept for reading, held, and the previous one stops being active
    # (so nothing stale is served or keyed in when the pause ends; the next tick publishes a fresh plan)
    reason = release["reason"] if release else (guard or (TIME_LIMIT_HELD if kept else None))
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
        # the anchor charge's deadline (see _ask_anchor): set by the first active plan that schedules one, dropped once none is asked for
        anchor = {}
        by = (site.get("state") or {}).get("anchor_by")
        if (p.get("battery") or {}).get("anchor_soc") and res.get("anchor"):
            if status == "active" and not (by and int(by) > t0):
                anchor["anchor_by"] = int(res["anchor"]["t"])
        elif by is not None:
            anchor["anchor_by"] = None
        sites.put_state(site_id, active_plan=active, last_tick=_iso(t0), **anchor)
    # why a plan was held, and the plan in force after this tick (null: none, as under a pause, a release or before the first plan passes the
    # guard), so a re-solve says what stays in force (round 7, frontend-states-formats-07)
    in_force = plan_id if status == "active" else (None if release else _in_force_id(site_id, now))
    return {"status": status, "plan_id": plan_id, "reason": reason, "in_force": in_force, "objective_rs": res["objective_rs"],
            "solve_seconds": res["solve_seconds"], "steps": steps_out, "slots": compiled, "summary": summary}


#: a failed solve's reason by the solver's status (scipy.optimize.milp: 1 a time or iteration limit with no plan, 2 infeasible, 3 unbounded)
FAILURE_CODES = {1: "solve_time_limit", 2: "solve_infeasible", 3: "solve_unbounded"}


def failure_code(res: dict) -> str:
    """Why a solve produced no plan, as a code (round 7, frontend-states-formats-07: the plan's reason was scipy's message, "Time limit reached.
    (HiGHS Status 13: ...)", and the plan page and the re-solve toast showed it raw): problem_refused when the problem failed validation,
    solve_recheck_failed when the solution failed the numpy re-check, else by the solver's status, solve_failed for any other."""
    if res.get("status") == "refused":
        return "problem_refused"
    if str(res.get("message") or "").startswith("Re-check failed"):
        return "solve_recheck_failed"
    return FAILURE_CODES.get(res.get("solver_status"), "solve_failed")


def _in_force_id(site_id: str, now: int) -> str | None:
    """The id of the plan in force at `now` once a tick has written its row, else None: the latest active plan while `now` is before its
    valid_until, as current() reads it (a plan past it is not in force, and the edge runs its fallback)."""
    with store.connect() as con:
        r = con.execute("SELECT id, valid_until FROM plans WHERE site_id = ? AND status = 'active' ORDER BY generated_at DESC LIMIT 1",
                        (site_id,)).fetchone()
    return r["id"] if r and not _past_valid_until(r["valid_until"], now) else None


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


def current(site_id: str, now: int | None = None) -> dict | None:
    """The plan in force: the latest active plan, while `now` (the clock by default) is before its valid_until. A plan past its valid_until is
    not in force (Part 2.4: a failed tick keeps the previous plan only until then), so it is neither served to the edge nor keyed in; held_info
    names it (plan_expired)."""
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    with store.connect() as con:
        r = con.execute("SELECT * FROM plans WHERE site_id = ? AND status = 'active' ORDER BY generated_at DESC LIMIT 1", (site_id,)).fetchone()
        last = con.execute("SELECT id, status, reason, generated_at FROM plans WHERE site_id = ? ORDER BY generated_at DESC LIMIT 1", (site_id,)).fetchone()
    if r is None or _past_valid_until(r["valid_until"], now):
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
    - Otherwise, when the active plan has passed its valid_until (the ticks stopped or every solve since failed): `plan_expired`, `until` None,
      the expired plan's id and `since`, its valid_until. The edge then runs its fallback (the inverter's native settings).
    - Otherwise None: a plan is in force, none was made yet, or the latest solve failed.
    A day in a native block of the switchback calendar is a release (control_release): reason switchback_native, `until` the block's end, kind
    `switchback`, no override."""
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    rel = control_release(site_id, now)
    with store.connect() as con:
        last = con.execute("SELECT id, status, reason FROM plans WHERE site_id = ? ORDER BY generated_at DESC, rowid DESC LIMIT 1", (site_id,)).fetchone()
        active = con.execute("SELECT id, valid_until FROM plans WHERE site_id = ? AND status = 'active' ORDER BY generated_at DESC LIMIT 1",
                             (site_id,)).fetchone()
    held = last is not None and last["status"] == "held"
    if rel:
        return {"reason": rel["reason"], "until": rel["until"], "plan_id": last["id"] if held and last["reason"] == rel["reason"] else None,
                "kind": rel["kind"], "author": rel["author"], "override_id": rel["override_id"]}
    if active is None and held:
        return {"reason": last["reason"] or "held", "until": None, "plan_id": last["id"]}
    if active is not None and _past_valid_until(active["valid_until"], now):
        return {"reason": EXPIRED_REASON, "until": None, "plan_id": active["id"], "since": active["valid_until"]}
    return None


def get(site_id: str, plan_id: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM plans WHERE site_id = ? AND id = ?", (site_id, plan_id)).fetchone()
    return _plan_out(r) if r else None


def _plan_time_limited(summary: dict) -> bool:
    """Whether a stored plan came from a solve that stopped at its time limit: `solver.time_limited` where the tick stored it, else (a plan from
    before it was stored) its solver status, since a solve is "feasible" only when it stopped at its limit with a plan."""
    sv = (summary or {}).get("solver") or {}
    if sv.get("first_time_limited"):
        return True  # a relaxed retry put in place of a first solve that stopped at its limit without a plan
    if "time_limited" in sv:
        return bool(sv["time_limited"])
    return sv.get("status") == "feasible"


def _status_label(res: dict) -> str:
    """The plan's solver status in words. A plan from a solve that stopped at its limit, or a relaxed retry after one, is "within the time
    limit"; a threshold repair of a plan that finished inside its limit is named as such (the genset follows the site's controller replayed over
    the plan, so it is feasible, never proven to the tolerance)."""
    status = res.get("status")
    if status not in ("optimal", "feasible"):
        return "Not solved"
    if res.get("time_limited", status == "feasible") or res.get("first_time_limited"):
        return "Solved within the time limit"
    if status == "feasible" and (res.get("threshold_repair") or {}).get("status") not in (None, "not_solved"):
        return "Repaired to the site's controller"
    return "Solved within tolerance" if status == "optimal" else "Solved within the time limit"


def time_limited_recent(site_id: str, hours: float = 24.0, now: int | None = None) -> dict:
    """The site's plans generated in the `hours` before `now` (epoch seconds; default the clock) and those whose solve stopped at its wall-clock
    limit (HANDOFF 8.A.5), for a check that marks plan quality or solves again when the machine is idle. Returns `{site_id, hours, since, plans,
    time_limited, share, items}`: `items` lists the time-limited plans, newest first, with `id`, `generated_at`, `belief_time`, `status`,
    `solve_seconds`, `mip_gap`, `time_limit_s` and `relaxed_retry` (a relaxed 24-hour retry after a first solve that stopped at its limit without
    a plan, which counts as time-limited). A failed solve (no plan) is counted in `plans` only."""
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    since = _iso(now - int(round(float(hours) * 3600)))
    with store.connect() as con:
        rows = con.execute("SELECT id, generated_at, belief_time, status, solve_seconds, summary FROM plans WHERE site_id = ? AND generated_at >= ? "
                           "AND generated_at <= ? ORDER BY generated_at DESC, rowid DESC", (site_id, since, _iso(now))).fetchall()
    items = []
    for r in rows:
        summary = json.loads(r["summary"] or "{}")
        if r["status"] == "failed" or not _plan_time_limited(summary):
            continue
        sv = summary.get("solver") or {}
        items.append({"id": r["id"], "generated_at": r["generated_at"], "belief_time": r["belief_time"], "status": r["status"],
                      "solve_seconds": r["solve_seconds"], "mip_gap": sv.get("mip_gap"), "time_limit_s": sv.get("time_limit_s"),
                      "relaxed_retry": bool(sv.get("first_time_limited"))})
    n = len(rows)
    return {"site_id": site_id, "hours": float(hours), "since": since, "plans": n, "time_limited": len(items),
            "share": round(len(items) / n, 3) if n else 0.0, "items": items}


#: GET /plan/past: the hours a request may ask for (the plan table shows the last 24, R8 2.9)
PAST_HOURS = (1, 48)
#: the 15-minute means a past step carries as measured: power in W (sent as kW), the state of charge (the estimate, else the BMS's) and the grid
PAST_SERIES = ("pv_power_w", "load_power_w", "grid_power_w", "battery_power_w", "genset_power_w", "battery_soc_est_pct", "battery_soc_bms_pct",
               "grid_present")


def past_steps(site_id: str, now: int, hours: int = 24) -> dict:
    """The plan table's past rows (round 7, pages-B-07; UI-09 and R8 2.9: "the past 24 hours ... greyed, with actuals beside plan"): every
    15-minute step of the `hours` before the current one, as the plan in force then planned it (active_at: none under a pause or a release to
    native, or before the first plan) and as measured (telemetry.series' 15-minute means; null where no reading arrived, never zeros). Battery
    positive is discharging and grid positive importing, as in the plan's steps. Money is the caller's to filter (routes_common.out)."""
    t1 = forecast.align(int(now))
    t0 = t1 - int(hours) * 3600
    act = telemetry.series(site_id, t0, t1, list(PAST_SERIES), "15m")
    at = {int(t): i for i, t in enumerate(act["ts"])}
    ser = act["series"]

    def kw(name: str, i: int) -> float | None:
        v = ser[name][i]
        return None if v is None else round(v / 1000, 2)

    steps_of: dict[str, dict[int, dict]] = {}
    rows = []
    for t in range(t0, t1, 900):
        plan = active_at(site_id, t)
        step = None
        if plan is not None:
            if plan["id"] not in steps_of:
                steps_of[plan["id"]] = {int(x["t"]): x for x in plan.get("steps") or []}
            step = steps_of[plan["id"]].get(t)
        i = at.get(t)
        actual = None
        if i is not None:
            soc = ser["battery_soc_est_pct"][i]
            actual = {"pv_kw": kw("pv_power_w", i), "load_kw": kw("load_power_w", i), "grid_kw": kw("grid_power_w", i),
                      "battery_kw": kw("battery_power_w", i), "genset_kw": kw("genset_power_w", i),
                      "soc_pct": soc if soc is not None else ser["battery_soc_bms_pct"][i], "grid_present": ser["grid_present"][i]}
        local = _local(t)
        rows.append({"t": t, "local": local.strftime("%H:%M"), "date": local.date().isoformat(), "plan_id": plan["id"] if step is not None else None,
                     "planned": step, "actual": actual})
    return {"from": t0, "to": t1, "hours": int(hours), "steps": rows}


def history(site_id: str, limit: int = 50) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM plans WHERE site_id = ? ORDER BY generated_at DESC LIMIT ?", (site_id, limit)).fetchall()
    return [_plan_out(r, with_steps=False) for r in rows]


def _released_at(site_id: str, ts: int) -> bool:
    """Whether a pause or a release to native was in force at the instant `ts`, from the override history: from its creation (from_ts) to its end,
    or to when it was cleared if that came first. The instant, not its quarter: a plan published after a pause was cleared mid-quarter is in force
    from then on. A day in a native block of the switchback calendar is released all day (switchback_release)."""
    if switchback_release(site_id, ts) is not None:
        return True
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
    force; a guard hold or a failed tick otherwise leaves the previous plan in force, up to its valid_until (after it, None)."""
    if _released_at(site_id, ts):
        return None
    with store.connect() as con:
        # the latest rows first (the light columns only): a held or failed row of a pause ends the search with no plan in force
        rows = con.execute("SELECT id, status, reason, summary, valid_until FROM plans WHERE site_id = ? AND belief_time <= ? "
                           "ORDER BY belief_time DESC, generated_at DESC, rowid DESC LIMIT 200", (site_id, _iso(ts))).fetchall()
        for r in rows:
            if r["status"] in ("active", "superseded"):
                if _past_valid_until(r["valid_until"], ts):
                    return None
                return _plan_out(con.execute("SELECT * FROM plans WHERE id = ?", (r["id"],)).fetchone())
            if r["status"] == "held" and r["reason"] in RELEASED_REASONS:
                return None
            if r["status"] == "failed" and (json.loads(r["summary"] or "{}") or {}).get("control_release"):
                return None
        r = con.execute("SELECT * FROM plans WHERE site_id = ? AND status IN ('active','superseded') AND belief_time <= ? ORDER BY belief_time DESC LIMIT 1",
                        (site_id, _iso(ts))).fetchone()
    return _plan_out(r) if r and not _past_valid_until(r["valid_until"], ts) else None


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
    if len(photo) > 7_000_000:
        raise ValueError("The photograph is larger than 5 MB")     # refused, not cut short (a cut image does not open)
    try:
        day_local = date.fromisoformat(str(day_local)).isoformat()
    except ValueError:
        raise ValueError("The day is a date written YYYY-MM-DD") from None
    plan = get(site_id, plan_id)
    if plan is None:
        raise KeyError(plan_id)
    # a day the plan has no table for is never confirmed: control_state and the alerts read today's row, verify scores the day's slots
    days = [d.get("date") for d in ((plan.get("slots") or {}).get("days") or [])]
    if day_local not in days:
        raise ValueError(f"This plan has no table for {day_local}" + (f"; its days are {', '.join(days)}" if days else ""))
    aid = store.new_id()
    with store.connect() as con:
        con.execute("INSERT INTO applied (id, site_id, plan_id, day_local, by, method, photo, created) VALUES (?,?,?,?,?,?,?,?)",
                    (aid, site_id, plan_id, day_local, by, method, photo, store._now()))
        con.execute("UPDATE plans SET applied = 1 WHERE id = ?", (plan_id,))
    store.audit(by, "marked a plan as applied", site_id, {"plan_id": plan_id, "day": day_local, "method": method})
    return {"id": aid, "plan_id": plan_id, "day_local": day_local, "method": method, "confirmation": "pending"}


def applied_rows(site_id: str, limit: int = 30) -> list[dict]:
    """The applied log, newest first. Each row says which table was keyed (`table`, round 7: audit pages-B-02): the issue of its plan for its day
    ({issued_at, reason, superseded: a later issue for the day names another plan}), None when that plan's table was never issued for the day; and
    its early verdict (`early_check`, the "it took" card: early_check, audit pages-B-08), None before one."""
    with store.connect() as con:
        rows = con.execute("SELECT id, plan_id, day_local, by, method, confirmation, fidelity, data, created FROM applied WHERE site_id = ? "
                           "ORDER BY created DESC LIMIT ?", (site_id, limit)).fetchall()
        days = sorted({r["day_local"] for r in rows})
        issues = con.execute("SELECT plan_id, day_local, issued_at, reason FROM table_issues WHERE site_id = ? AND day_local IN "
                             f"({','.join('?' * len(days))}) ORDER BY issued_at, rowid", (site_id, *days)).fetchall() if days else []
    out = []
    for r in rows:
        d = dict(r)
        try:
            extra = json.loads(d.pop("data") or "{}")
        except (TypeError, ValueError):
            extra = {}
        d["early_check"] = extra.get("early_check") if isinstance(extra, dict) else None
        day = [i for i in issues if i["day_local"] == d["day_local"]]
        mine = [i for i in day if i["plan_id"] == d["plan_id"]]
        d["table"] = ({"issued_at": mine[0]["issued_at"], "reason": mine[0]["reason"], "superseded": day[-1]["plan_id"] != d["plan_id"]}
                      if mine else None)
        out.append(d)
    return out


def _no_money(body: dict) -> dict:
    """The message body with every money field taken out before it is signed or stored (plan Part 10.5: plans sent to a key carry no money)."""
    from . import money  # the API's name filter, imported here so the engine does not load it at import time
    return money.for_key(body)


def _record_send(site_id: str, plan_id: str, msg: dict) -> None:
    with store.connect() as con:
        con.execute("INSERT INTO plan_messages (id, site_id, plan_id, key_id, body, sent_at) VALUES (?,?,?,?,?,?)",
                    (store.new_id(), site_id, plan_id, msg["signature"]["key_id"], json.dumps(msg), store._now()))


def _release_message(site_id: str, rel: dict, record: bool = True) -> dict:
    """A signed release while control is paused or released to native, or on a native-block day of the switchback calendar: the same keys as a
    plan message, no slots and no steps, and a `release` block; the edge runs the inverter's native settings until the override or the block
    ends and a fresh plan is issued (Part 7.3)."""
    site = sites.full(site_id) or {"data": {}}
    floor = float((site["data"].get("reserve_policy") or {}).get("min_soc_pct", 20.0))
    bms_low = ((site.get("assets") or {}).get("battery") or {}).get("soc_min", 0.1) * 100
    now = store._now()
    # at most 48 h after issue, as every message (U3 3.2; the override's own end is in `release.until`): the edge then runs on_expiry, native too
    until = min(datetime.fromisoformat(rel["until"].replace("Z", "+00:00")), datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(hours=48))
    body = {"contract": delivery.CONTRACT, "type": "release", "plan_id": None, "site_id": site_id, "issued_at": now, "valid_from": now,
            "valid_until": until.isoformat(timespec="seconds"), "timezone": "Asia/Karachi",
            "mode": "b" if site["data"].get("control_mode") == "mode_b" else "a",
            "fallback": delivery.fallback_block(site, floor, bms_low),
            "mode_a": {"days": []}, "mode_b": None,
            "notes": {"reason": {"control_paused": "Control paused", "released_to_native": "Released to native",
                                 SWITCHBACK_REASON: "Switchback native block"}[rel["reason"]]},
            "release": {"reason": rel["reason"], "until": rel["until"], "by": rel["author"]}, **_edge_envelope(site_id, None)}
    msg = delivery.sign(site_id, _checked(_no_money(body)))
    if record:
        _record_send(site_id, "release", msg)
    return msg


def signed_message(site_id: str, record: bool = True) -> dict | None:
    """The current plan as the ems-edge/1.0 plan message, signed with the site's key; a signed release instead while control is paused or
    released to native, or on a native-block day of the switchback calendar. The body carries no money field (the plan's rupees stay on the plan
    page). `record` writes the plan_messages row that the edge page lists as a send: the edge's own pull (the key-only route) records it; any other
    caller passes record=False. None when no plan is in force (none yet, or the last one is past its valid_until).
    Only a closed-loop site's edge applies plans (plan Part 2.3): below it (the effective tier, monitor or advisory_writes) nothing is signed or
    sent and NotClosedLoop is raised, so a plans:read key at a monitor site gets no actionable table and no send is counted."""
    tier = (sites.get(site_id) or {}).get("tier", "monitor")
    if tier != "closed_loop":
        raise NotClosedLoop(tier)
    rel = control_release(site_id)
    if rel:
        return _release_message(site_id, rel, record=record)
    clock = clock_info(site_id)
    if clock and clock["level"] == "demoted":
        raise ClockDemoted(clock["offset_s"])
    plan = current(site_id)
    if plan is None:
        return None
    caps = delivery.latest_capabilities(site_id)
    full = sites.full(site_id)
    compiled = plan.get("slots") or {}
    if plan["mode"] == "a" and issues_tables(full):
        # the table issued for each day, not each quarter's draft: the nightly table, re-issued only when worth a write (Part 7.1 step 7)
        compiled, _ = issued_view(site_id, compiled)
    days = [{"date": d["date"], "slots": [{k: v for k, v in s.items() if k not in ("intent",)} for s in d["slots"]]}
            for d in compiled.get("days", [])]
    try:
        # only what the target inverter can write (R6 C1): the plan page keeps the optimiser's table, the message carries what was fitted
        days, fit = delivery.fit_to_capabilities(days, caps)
    except delivery.NotWritable as exc:
        raise CapsMismatch(str(exc)) from None
    body = {"contract": delivery.CONTRACT, "type": "plan", "plan_id": plan["id"], "site_id": site_id, "issued_at": plan["generated_at"],
            "valid_from": plan["valid_from"], "valid_until": plan["valid_until"], "timezone": "Asia/Karachi", "mode": plan["mode"],
            "fallback": (plan.get("slots") or {}).get("fallback"), "mode_a": {"days": days, "apply_policy": write_policy(full)[0]},
            "write_budget": write_policy(full)[1],
            "mode_b": delivery.mode_b(plan["steps"]) if plan["mode"] == "b" else None,
            "notes": {"reason": plan["summary"].get("status_label")}, **_edge_envelope(site_id, plan["id"], caps)}
    if fit:
        body["capabilities_fit"] = fit
    if clock and clock["level"] == "widen":
        # the inverter clock is more than 300 s off: every discharge slot starts 15 minutes early and ends 15 minutes late (R6 C10)
        grammar = (plan.get("slots") or {}).get("grammar") or {}
        body["mode_a"] = {**body["mode_a"], "days": delivery.widen_peak_slots(days, CLOCK_WIDEN_MIN, int(grammar.get("min_slot_minutes") or 15))}
        body["clock"] = {"offset_s": clock["offset_s"], "widened_min": CLOCK_WIDEN_MIN}
    msg = delivery.sign(site_id, _checked(_no_money(body)))
    if record:
        _record_send(site_id, plan["id"], msg)
    return msg


# ------------------------------------------------------------------------------------------------ round 7 (audit): what reaches the device

#: the held reason when the plan in force has passed its valid_until and no newer plan replaced it (Part 2.4)
EXPIRED_REASON = "plan_expired"


class NotClosedLoop(Exception):
    """A signed plan message asked for at a site whose effective control tier is below closed loop: nothing is sent to its edge (plan Part 2.3,
    monitor: recommendations only; advisory_writes: a person keys the table in)."""

    def __init__(self, tier: str):
        self.tier = tier
        words = {"monitor": "monitor", "advisory_writes": "advisory writes"}.get(tier, str(tier).replace("_", " "))
        how = ("the plan is a recommendation" if tier == "monitor" else "a person keys the table in")
        super().__init__(f"The site's control tier is {words}, so no plan is sent to its edge: {how}")


class CapsMismatch(Exception):
    """The plan in force does not fit the device the edge's current capabilities describe (more slots than its slot model holds): it is not signed
    (R6 C1: a table the edge would refuse is a cloud bug), and the edge keeps what it runs."""


def _past_valid_until(valid_until: str | None, ts: int) -> bool:
    """Whether the instant `ts` is at or after a plan's valid_until (a plan with none never expires)."""
    end = _ts(valid_until)
    return end is not None and int(ts) >= end


def switchback_release(site_id: str, at: int | None = None) -> dict | None:
    """The native block of the switchback calendar that holds the Pakistan-time day of `at` (now by default), as a release, else None: on such a
    day the inverter runs its own settings (plan 8.2, VER-08: a baseline block plans "native"), so the tick holds its plan, the edge is sent a
    release and an advisory site is not asked to key a table. `until` is the block's end (midnight after its last day, Pakistan time); `author` the
    calendar. The arm is verify.arm_on's (a locked block first), so the live site and the savings ledger read the same calendar."""
    from . import verify  # verify imports plans at module load

    at = int(at if at is not None else datetime.now(timezone.utc).timestamp())
    day = _local(at).date()
    if verify.arm_on(site_id, day) != "native":
        return None
    with store.connect() as con:
        r = con.execute("SELECT block_start, block_end FROM switchback WHERE site_id = ? AND block_start <= ? AND block_end >= ? "
                        "ORDER BY locked DESC, block_start DESC LIMIT 1", (site_id, day.isoformat(), day.isoformat())).fetchone()
    end_day = date.fromisoformat(r["block_end"]) + timedelta(days=1)
    until = datetime(end_day.year, end_day.month, end_day.day, tzinfo=PKT).astimezone(timezone.utc).isoformat(timespec="seconds")
    return {"reason": SWITCHBACK_REASON, "kind": "switchback", "until": until, "author": "switchback calendar", "override_id": None,
            "block_start": r["block_start"], "block_end": r["block_end"]}


def _edge_envelope(site_id: str, plan_id: str | None, caps: dict | None = None) -> dict:
    """U3 5.1 and 5.4's envelope of a plan message (audit engine-delivery-11): msg_type, a fresh msg_id, the edge it is for (the edge_id of its
    current capabilities or last heartbeat), the message's sequence (one more than the messages sent to the site so far), sent_at, the plan this one
    supersedes (the last other plan sent to the edge, so it can drop older queued plans after link loss), the capabilities sequence the plan was
    checked against (CAPS_STALE) and the devices it targets."""
    import uuid

    caps = caps if caps is not None else delivery.latest_capabilities(site_id)
    beat = sites.state(site_id).get("edge_heartbeat") or {}
    with store.connect() as con:
        sent = con.execute("SELECT count(*) FROM plan_messages WHERE site_id = ?", (site_id,)).fetchone()[0]
        prev = con.execute("SELECT plan_id FROM plan_messages WHERE site_id = ? AND plan_id NOT IN ('release', ?) ORDER BY sent_at DESC, rowid DESC "
                           "LIMIT 1", (site_id, plan_id or "")).fetchone()
    return {"msg_type": "plan", "msg_id": str(uuid.uuid4()), "edge_id": (caps or {}).get("edge_id") or beat.get("edge_id"), "seq": int(sent) + 1,
            "sent_at": store._now(), "supersedes": prev["plan_id"] if prev and plan_id else None,
            "capabilities_seq": caps["seq"] if caps else None, "targets": delivery.targets(caps)}


def _checked(body: dict) -> dict:
    """The plan message body checked against the contract's model before it is signed (R6 C1: a message the edge would refuse for its shape is a
    cloud bug, never signed)."""
    delivery.PlanMessage.model_validate(body)
    return body


# ------------------------------------------------------------------------------------------------ round 7 (audit): what the edge reports

#: A heartbeat speaks for the edge for this long (the edge sends one a minute; five minutes is the edge-offline mark)
HEARTBEAT_FRESH_S = 300
#: The heartbeat states in which the edge does not run the cloud's plan, as a control state
_EDGE_STATES = {"disabled": ("edge_disabled", "Edge disabled control"),
                "fallback": ("edge_fallback", "Edge on fallback: the inverter runs its own settings"),
                "native": ("edge_fallback", "Edge on fallback: the inverter runs its own settings"),
                "released": ("edge_released", "Edge released the inverter to its own settings"),
                "hold": ("edge_hold", "Edge holding the battery")}
#: The events with which the edge says it does not run the plan, and the events that end each state (R6 E18, 4.4)
_OFF_EVENTS = {"control_disabled": ("disabled", True), "control_enabled": ("disabled", False),
               "fallback_engaged": ("fallback", True), "fallback_cleared": ("fallback", False), "plan_rejected": ("rejected", None)}


def edge_view(site_id: str, now: int | None = None) -> dict | None:
    """What the edge itself reports when it is not running the plan in force (audit engine-delivery-12), as a control state {key, label}, else None:
    control it disabled (a control_disabled event not yet followed by control_enabled, or a fresh heartbeat saying so), a fallback (fallback_engaged
    not yet cleared, or a fresh heartbeat in fallback or native), a release or hold of its own, or the plan in force refused (a rejected or expired
    ack) or applied in part (a partial ack, or a primitive clamped, failed or refused). A heartbeat older than HEARTBEAT_FRESH_S says nothing."""
    from . import contract

    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    st = sites.state(site_id)
    reports = st.get("edge_reports") or {}
    off = reports.get("disabled") or {}
    if off.get("on"):
        return {"key": "edge_disabled", "label": "Edge disabled control" + (f": {off['cause']}" if off.get("cause") else "")}
    beat, at = st.get("edge_heartbeat") or {}, st.get("edge_heartbeat_at")
    state = beat.get("control_state") if at is not None and now - int(at) <= HEARTBEAT_FRESH_S else None
    if (reports.get("fallback") or {}).get("on") and state not in contract.FOLLOWING_STATES:
        state = "fallback"
    if state in _EDGE_STATES:
        key, label = _EDGE_STATES[state]
        return {"key": key, "label": label}
    plan = current(site_id, now)
    if plan is None:
        return None
    with store.connect() as con:
        r = con.execute("SELECT body FROM acks WHERE site_id = ? AND plan_id = ? ORDER BY received_at DESC, rowid DESC LIMIT 1",
                        (site_id, plan["id"])).fetchone()
    if r is None:
        return None
    outcome, codes = contract.ack_outcome(json.loads(r["body"]))
    why = f" ({', '.join(codes)})" if codes else ""
    if outcome == "rejected":
        return {"key": "edge_refused", "label": f"Edge refused the plan{why}"}
    if outcome == "expired":
        return {"key": "edge_fallback", "label": "Edge reports the plan ran out: the inverter runs its own settings"}
    if outcome == "partial":
        return {"key": "edge_partial", "label": f"Edge applied part of the plan{why}"}
    return None


def edge_fidelity(site_id: str, day: date) -> str:
    """A closed-loop day's fidelity from what the edge reported (audit engine-delivery-12; R6 C13: acks and events feed the fidelity), not 'confirmed'
    by construction: 'not_applied' when at any time of the Pakistan-time day control was disabled or the edge was on fallback (a report before the day
    counts until the event that ends it), or the edge rejected a plan or reported one expired; 'partial' when it applied a plan in part; else
    'confirmed'."""
    from . import contract

    t0 = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    t1 = t0 + 86400
    with store.connect() as con:
        evs = con.execute(f"SELECT ts, type FROM events WHERE site_id = ? AND type IN ({','.join('?' * len(_OFF_EVENTS))})",
                          (site_id, *_OFF_EVENTS)).fetchall()
        acks = con.execute("SELECT body, received_at FROM acks WHERE site_id = ? AND received_at >= ? AND received_at < ?",
                           (site_id, _iso_utc(t0 - 86400), _iso_utc(t1 + 86400))).fetchall()
    on = {"disabled": False, "fallback": False}
    for at, kind in sorted((contract.epoch(e["ts"]), e["type"]) for e in evs if contract.epoch(e["ts"]) is not None):
        if at >= t1:
            break
        if at >= t0 and any(on.values()):
            return "not_applied"                      # in that state when the day began
        key, state = _OFF_EVENTS[kind]
        if key == "rejected":
            if at >= t0:
                return "not_applied"
            continue
        if at >= t0 and state:
            return "not_applied"
        on[key] = state
    if any(on.values()):
        return "not_applied"                          # in that state since before the day began, or from within it
    partial = False
    for a in acks:
        at = contract.epoch(a["received_at"])
        if at is None or not t0 <= at < t1:
            continue
        outcome, _ = contract.ack_outcome(json.loads(a["body"]))
        if outcome in ("rejected", "expired"):
            return "not_applied"
        partial |= outcome == "partial"
    return "partial" if partial else "confirmed"


def _iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------ round 7 (audit): the clock-offset ladder

#: R6 C10 (plan Part 7.3): over CLOCK_WARN_S the cloud raises clock_unsynced; over CLOCK_WIDEN_S the plan message widens every discharge slot by
#: CLOCK_WIDEN_MIN each side and the control state asks for the inverter clock to be set; over CLOCK_DEMOTE_S control is demoted (no table is sent)
#: until the offset is back under CLOCK_WARN_S
CLOCK_WARN_S, CLOCK_WIDEN_S, CLOCK_DEMOTE_S, CLOCK_WIDEN_MIN = 120, 300, 900, 15


def clock_level(offset_s: float, demoted: bool = False) -> str:
    """The ladder's step for the worst clock offset (seconds, either sign): 'ok', 'unsynced', 'widen' or 'demoted'; a demoted site stays demoted
    until the offset is under CLOCK_WARN_S."""
    off = abs(float(offset_s))
    if off >= CLOCK_DEMOTE_S or (demoted and off >= CLOCK_WARN_S):
        return "demoted"
    return "widen" if off >= CLOCK_WIDEN_S else "unsynced" if off >= CLOCK_WARN_S else "ok"


def clock_info(site_id: str) -> dict | None:
    """The site's clock offset when the ladder acts on it ({offset_s, level, source}: the worst of the edge's heartbeat and the inverter's own clock
    reported in telemetry, routes_edge), else None."""
    c = sites.state(site_id).get("edge_clock") or {}
    if c.get("level", "ok") == "ok":
        return None
    return {"offset_s": float(c["offset_s"]), "level": c["level"], "source": c.get("source")}


def clock_words(offset_s: float) -> str:
    off = abs(float(offset_s))
    return f"{off / 60:.0f} min" if off >= 90 else f"{off:.0f} s"


class ClockDemoted(Exception):
    """The site's clock is off by CLOCK_DEMOTE_S or more (or has not come back under CLOCK_WARN_S since): control is demoted and no table is signed."""

    def __init__(self, offset_s: float):
        self.offset_s = offset_s
        super().__init__(f"The inverter clock is {clock_words(offset_s)} off, so control is demoted and no plan is sent until the clock is within "
                         f"{CLOCK_WARN_S} seconds")



# ------------------------------------------------------------------------------------------------ round 7 (audit): Karachi days of stored stamps

def karachi_day_bounds(day: date) -> tuple[str, str]:
    """The stored UTC stamps (store._now()'s form) at which a Karachi day starts and the next one starts: a stamp s is on the day when lo <= s < hi.
    A stored stamp's first ten characters are its UTC date, which is the Karachi date only from 05:00, so a local day is never matched on them
    (audit pages-B-06)."""
    lo = datetime(day.year, day.month, day.day, tzinfo=PKT).astimezone(timezone.utc)
    return lo.isoformat(timespec="seconds"), (lo + timedelta(days=1)).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------ round 7 (audit): the table issued to the inverter

#: Plan Part 7.1 step 7 and R6 4.5 (audit pages-B-02): the Mode A table that reaches the inverter (keyed in by a person at the advisory tier, written by
#: the edge at closed loop) is issued once a day, by the nightly job after the 23:00 solve (scheduler, 23:05) for the next day; a day that began with
#: none (a new site, a pause, a missed nightly job) takes the table of its first plan in force. A later plan re-issues today's table only when it is
#: worth REISSUE_GAIN_RS more over the plan's horizon, the day's table writes are under the brand's budget and the last issue is REISSUE_SPACING_S
#: old; a hold or a resume issues it at once (the budget card's "unless a hold or release needs it").
REISSUE_GAIN_RS = 500.0
REISSUE_SPACING_S = 3600
#: U3 5.4's apply_policy (the edge's own cap of table writes a day, written only on a change, never across midnight) and R6 3.6's write budget, of
#: which the cloud uses the brand's table_writes_per_day (at most the edge's cap)
EDGE_TABLE_WRITE_CAP = 4
EVENT_WRITES_PER_DAY = 2
APPLY_WINDOW_LOCAL = "00:05-23:50"
#: why a table was issued: the nightly job, a day's first, a later plan worth its write (event), a hold or a resume
ISSUE_REASONS = ("nightly", "first", "event", "hold", "resume")


def issues_tables(site: dict | None) -> bool:
    """Whether a site's plan reaches its inverter as an issued Mode A table: a person keys it at the advisory tier, the edge writes it at closed loop
    (unless the site runs Mode B setpoints). Nothing is written at the monitor tier."""
    if not site:
        return False
    tier = site.get("tier", "monitor")
    return tier == "advisory_writes" or (tier == "closed_loop" and (site.get("data") or {}).get("control_mode") != "mode_b")


def table_budget(site: dict | None) -> int:
    """The table writes a day the cloud uses at a site: its inverter brand's (delivery.BRANDS), at most the edge's cap."""
    brand = (((site or {}).get("assets") or {}).get("inverter") or {}).get("brand", "other")
    return min(int(delivery.BRANDS.get(brand, delivery.BRANDS["other"])["table_writes_per_day"]), EDGE_TABLE_WRITE_CAP)


def issued_tables(site_id: str, day_local: str) -> list[dict]:
    """The tables issued for a Karachi day, oldest first: {id, plan_id, day_local, issued_at, reason, gain_rs}."""
    with store.connect() as con:
        rows = con.execute("SELECT id, plan_id, day_local, issued_at, reason, gain_rs FROM table_issues WHERE site_id = ? AND day_local = ? "
                           "ORDER BY issued_at, rowid", (site_id, day_local)).fetchall()
    return [dict(r) for r in rows]


def issued_for(site_id: str, day_local: str) -> dict | None:
    """The table to write for a Karachi day: its latest issue, else None (none issued yet: the plan in force's table is a draft)."""
    rows = issued_tables(site_id, day_local)
    return rows[-1] if rows else None


def _write_issue(site_id: str, day_local: str, plan_id: str, reason: str, at: int, gain_rs: float | None = None) -> dict:
    row = {"id": store.new_id(), "plan_id": plan_id, "day_local": day_local, "issued_at": _iso(at), "reason": reason, "gain_rs": gain_rs}
    with store.connect() as con:
        con.execute("INSERT INTO table_issues (id, site_id, day_local, plan_id, issued_at, reason, gain_rs) VALUES (?,?,?,?,?,?,?)",
                    (row["id"], site_id, day_local, plan_id, row["issued_at"], reason, gain_rs))
    return row


def issue_first(site_id: str, plan: dict, day_local: str, at: int) -> dict | None:
    """A day's first table, issued at `at` from `plan`, when nothing is issued for the day yet and the plan has a table for it (as issue_tables
    issues a day's first): for a day the scheduler never reached, such as the history the demo builds (its installer keys what was issued). The
    issue, else None."""
    if issued_for(site_id, day_local) is not None or _day_slots(plan, day_local) is None:
        return None
    return _write_issue(site_id, day_local, plan["id"], "first", at)


def _day_slots(plan: dict | None, day_local: str) -> list[dict] | None:
    """A plan's compiled slots for one Karachi day, else None."""
    return next((d["slots"] for d in ((plan or {}).get("slots") or {}).get("days") or [] if d.get("date") == day_local), None)


def _table_key(slots: list[dict] | None) -> str:
    """A day's table as the inverter holds it: its slots without their epoch stamps (the same ring on two days is the same table) or the intent words."""
    return json.dumps([{k: v for k, v in s.items() if k not in ("start_ts", "end_ts", "intent")} for s in slots or []], sort_keys=True, default=str)


def reissue_gain(site: dict, plan: dict, issued_slots: list[dict], day_local: str) -> float:
    """What the plan's table for `day_local` saves over the issued one, in rupees: both replayed through the twin on the plan's own forecast over its
    horizon from its starting state of charge, with the plan's other days behind each (so the energy either leaves in the battery is priced on the
    days after); priced as the compare drawer prices energy (import less export credit), plus the critical load left unserved at the site's value
    of lost load, so a table that no longer holds the reserve for a forecast outage is worth re-issuing."""
    steps = plan.get("steps") or []
    if not steps:
        return 0.0
    ts = np.repeat([int(s["t"]) for s in steps], 15) + np.tile(np.arange(15) * 60, len(steps))

    def rep(k: str, default: float = 0.0) -> np.ndarray:
        return np.repeat([float(default if s.get(k) is None else s[k]) for s in steps], 15)

    series = {"ts": ts, "pv": rep("pv_forecast_kw"), "load": rep("load_kw"), "crit": rep("critical_kw"), "grid": rep("grid_present", 1.0),
              "temp": np.full(len(ts), 30.0)}
    c_imp, c_exp = rep("c_imp"), rep("c_exp")
    voll = float((site.get("data") or {}).get("voll_rs_per_kwh", 500.0))
    plant = twin.plant_of(site)
    soc0 = float(steps[0].get("soc_start_pct") or 50.0) / 100.0
    later = [s for d in (plan.get("slots") or {}).get("days") or [] if d.get("date") != day_local for s in d.get("slots") or []]

    def cost(day_slots: list[dict]) -> float:
        sim = twin.simulate(series, twin.slots_policy(list(day_slots) + later, ts), plant, soc0)
        return float((c_imp * sim["imp"] - c_exp * sim["exp"] + voll * sim["unserved"]).sum() / 60)

    return cost(issued_slots) - cost(_day_slots(plan, day_local) or [])


def writes_used(site_id: str, day: date, site: dict | None = None) -> int:
    """The table writes of a Karachi day (audit pages-B-02, pages-B-06), never the edge's pulls:
    - advisory tier: the distinct tables issued for the day or marked applied for it (each is a table a person keys in);
    - closed loop: the most of the tables issued for the day, the day's sends that changed the table the inverter runs (the day's table at the send;
      a repeat pull of the same table is not a write, a release is one) and the count the edge itself reports in the day's acks (writes_used_today);
    - monitor tier: none."""
    site = site if site is not None else sites.full(site_id)
    tier = (site or {}).get("tier", "monitor")
    if tier == "monitor":
        return 0
    d = day.isoformat()
    with store.connect() as con:
        issued = {r[0] for r in con.execute("SELECT plan_id FROM table_issues WHERE site_id = ? AND day_local = ?", (site_id, d)).fetchall()}
        if tier == "advisory_writes":
            marked = {r[0] for r in con.execute("SELECT plan_id FROM applied WHERE site_id = ? AND day_local = ?", (site_id, d)).fetchall()}
            return len(issued | marked)
        lo, hi = karachi_day_bounds(day)
        prev = con.execute("SELECT body, sent_at FROM plan_messages WHERE site_id = ? AND sent_at < ? ORDER BY sent_at DESC, rowid DESC LIMIT 1",
                           (site_id, lo)).fetchone()
        sent = con.execute("SELECT body, sent_at FROM plan_messages WHERE site_id = ? AND sent_at >= ? AND sent_at < ? ORDER BY sent_at, rowid",
                           (site_id, lo, hi)).fetchall()
        acks = con.execute("SELECT body FROM acks WHERE site_id = ? AND received_at >= ? AND received_at < ?", (site_id, lo, hi)).fetchall()

    def written(r) -> str:
        try:
            body = json.loads(r["body"])
        except (TypeError, ValueError):
            return ""
        if body.get("type") == "release":
            return "release"
        on = _local(_ts(r["sent_at"])).date().isoformat()
        return _table_key(next((x.get("slots") for x in (body.get("mode_a") or {}).get("days") or [] if x.get("date") == on), None))

    changes, last = 0, written(prev) if prev else None
    for r in sent:
        k = written(r)
        if k != last:
            changes, last = changes + 1, k
    edge = 0
    for a in acks:
        try:
            body = json.loads(a["body"])
        except (TypeError, ValueError):
            continue
        for dev in body.get("per_device") or []:
            edge = max(edge, int((dev.get("writes_used_today") or {}).get("tou_table") or 0))
    return max(len(issued), changes, edge)


def issue_nightly(site_id: str, now: int | None = None) -> dict | None:
    """The nightly job (scheduler, 23:05 Pakistan time, after the 23:00 solve): the next day's table from the plan in force, when it has one, none is
    issued for that day yet and no pause or release is in force at its start. Returns the issue, else None."""
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    site = sites.full(site_id)
    if not issues_tables(site):
        return None
    nxt = _local(now).date() + timedelta(days=1)
    plan = current(site_id, now)
    if plan is None or _day_slots(plan, nxt.isoformat()) is None or issued_for(site_id, nxt.isoformat()) is not None:
        return None
    if control_release(site_id, int(datetime(nxt.year, nxt.month, nxt.day, tzinfo=PKT).timestamp())) is not None:
        return None
    return _write_issue(site_id, nxt.isoformat(), plan["id"], "nightly", now)


def issue_tables(site_id: str, now: int | None = None, force: str | None = None) -> dict | None:
    """After a solve (each scheduler tick, a manual re-solve, a control action): today's table.
    - None issued for today: the plan in force's table is issued ('first').
    - The plan in force has another table: `force` ('hold' or 'resume', a control action that needs the table now) issues it at once; otherwise it
      is re-issued ('event') only when reissue_gain reaches REISSUE_GAIN_RS, writes_used is under table_budget and the last issue is
      REISSUE_SPACING_S old. The decision is kept in the site's state (`table_reissue`) for GET /control.
    Returns {plan_id, day, reason (when issued), gain_rs, issued, blocked: below_threshold, budget_used, too_soon or None, at}; None when nothing was
    in question: no table is issued below the advisory tier or at a Mode B site, nor while no plan is in force (a pause, a release, a native-block
    day, a plan past its valid_until), and the plan in force's table is the issued one."""
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    site = sites.full(site_id)
    if not issues_tables(site) or control_release(site_id, now) is not None:
        return None
    plan = current(site_id, now)
    day = _local(now).date()
    today = day.isoformat()
    slots = _day_slots(plan, today)
    if plan is None or slots is None:
        return None
    cur = issued_for(site_id, today)
    old = _day_slots(get(site_id, cur["plan_id"]), today) if cur else None
    out = {"plan_id": plan["id"], "day": today, "gain_rs": None, "issued": True, "blocked": None, "at": _iso(now)}
    if cur is None or old is None:
        # nothing issued for today yet (or the issued plan is no longer readable): the day's first table
        _write_issue(site_id, today, plan["id"], "first", now)
        return {**out, "reason": "first"}
    if cur["plan_id"] == plan["id"] or _table_key(old) == _table_key(slots):
        return None
    if force in ("hold", "resume"):
        _write_issue(site_id, today, plan["id"], force, now)
        return {**out, "reason": force}
    gain = round(reissue_gain(site, plan, old, today), 0)
    since = now - (_ts(cur["issued_at"]) or 0)
    blocked = ("below_threshold" if gain < REISSUE_GAIN_RS else "budget_used" if writes_used(site_id, day, site) >= table_budget(site)
               else "too_soon" if since < REISSUE_SPACING_S else None)
    out = {**out, "gain_rs": gain, "issued": blocked is None, "blocked": blocked}
    if blocked is None:
        _write_issue(site_id, today, plan["id"], "event", now, gain)
        out["reason"] = "event"
    sites.put_state(site_id, table_reissue=out)
    return out


def issued_view(site_id: str, compiled: dict) -> tuple[dict, list[dict]]:
    """The compiled table as it reaches the inverter: each day from the plan issued for it (the plan in force's draft where none is issued yet), and
    per day its issue: {day, plan_id (None for a draft), issued_at, reason, gain_rs}."""
    days, info = [], []
    for d in compiled.get("days") or []:
        iss = issued_for(site_id, d["date"])
        slots = _day_slots(get(site_id, iss["plan_id"]), d["date"]) if iss else None
        days.append({**d, "slots": slots} if slots is not None else d)
        info.append({"day": d["date"], "plan_id": iss["plan_id"], "issued_at": iss["issued_at"], "reason": iss["reason"], "gain_rs": iss["gain_rs"]}
                    if slots is not None else {"day": d["date"], "plan_id": None})
    return {**compiled, "days": days}, info


def reissue_held(site_id: str, plan_id: str | None, day: date) -> dict | None:
    """The plan in force's re-issue decision for today when it was held back (GET /control's `reissue`), else None."""
    st = sites.state(site_id).get("table_reissue") or {}
    if not plan_id or st.get("plan_id") != plan_id or st.get("day") != day.isoformat() or st.get("issued"):
        return None
    return {k: st.get(k) for k in ("plan_id", "day", "gain_rs", "issued", "blocked", "at")}


def write_policy(site: dict | None) -> tuple[dict, dict]:
    """The plan message's U3 5.4 mode_a.apply_policy and R6 3.6 write_budget (audit engine-delivery-09)."""
    return ({"write_if_changed_only": True, "max_table_writes_per_day": EDGE_TABLE_WRITE_CAP, "apply_window_local": APPLY_WINDOW_LOCAL},
            {"table_writes_per_day": table_budget(site), "event_writes_per_day": EVENT_WRITES_PER_DAY})


# ------------------------------------------------------------------------------------------------ round 7 (audit): the "it took" card

#: Plan Part 9.5 and 7.6 (audit pages-B-08): the early verdict on a keyed table comes this long after its first discharge slot in the peak starts
EARLY_CHECK_S = 1800
#: the verdicts that are kept and not computed again
EARLY_FINAL = ("took", "did_not_take", "no_evidence", "no_peak_slot")
_DISCHARGING = ("discharge", "export")


def _keep_early(applied_id: str, check: dict) -> dict:
    """The early verdict written to the applied row's data (`early_check`), what else the row holds kept (the nightly confirmation's detail)."""
    with store.write_transaction() as con:
        r = con.execute("SELECT data FROM applied WHERE id = ?", (applied_id,)).fetchone()
        try:
            data = json.loads((r["data"] if r else None) or "{}")
        except (TypeError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        con.execute("UPDATE applied SET data = ? WHERE id = ?", (json.dumps({**data, "early_check": check}), applied_id))
    return check


def early_check(site_id: str, now: int | None = None, site: dict | None = None) -> dict | None:
    """The "it took" or "it did not take" card (plan Part 9.5): today's keyed table at an advisory site, read from telemetry 30 minutes after its first
    discharge (or export) slot that starts in the peak window (any discharge start at a site with no peak window), from a quarter before the mark on
    (a table cannot be followed before it is keyed). The test is the nightly confirmation's boundary test (verify.confirm_applied: discharge above 10
    percent of rated within 30 minutes of the start, grid present). The table and the native capture in force are replayed through the twin over the
    hour round the start from the measured state of charge: where the table would not discharge there either there is no evidence (the battery
    near its floor), and where the native settings would discharge too, the measured energy over the hour must be nearer one replay than the other
    by 2 percent of the battery (the nightly not-applied margin).

    Kept on the latest mark of today as `early_check` ({verdict, reason, boundary_ts, due_ts, slot_start, plan_id, max_discharge_kw, checked}).
    Verdicts: waiting (before the due time, or no battery reading for the window yet: a CSV source uploads later), took, did_not_take, no_evidence,
    no_peak_slot; a final one is not computed again. It never sets the day's class, its fidelity or its claim: the nightly confirm_applied does.
    None when the site is not at the advisory tier or today has no mark."""
    from . import verify  # verify imports plans at module load

    site = site or sites.full(site_id)
    if site is None or site.get("tier") != "advisory_writes":
        return None
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    day = _local(now).date()
    with store.connect() as con:
        row = con.execute("SELECT id, plan_id, data, created FROM applied WHERE site_id = ? AND day_local = ? ORDER BY created DESC, rowid DESC LIMIT 1",
                          (site_id, day.isoformat())).fetchone()
    if row is None:
        return None
    try:
        prior = (json.loads(row["data"] or "{}") or {}).get("early_check") or {}
    except (TypeError, ValueError, AttributeError):
        prior = {}
    if prior.get("verdict") in EARLY_FINAL and prior.get("plan_id") == row["plan_id"]:
        return prior
    plan = get(site_id, row["plan_id"])
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    every = sorted((s for d in ((plan or {}).get("slots") or {}).get("days") or [] for s in d.get("slots") or []), key=lambda s: int(s["start_ts"]))
    marked = _ts(row["created"]) or now
    conn = (site.get("connections") or [None])[0]
    quarters = np.arange(t0, t0 + 86400, 900, dtype=np.int64)
    pk = np.asarray(peak_mask(conn, quarters), dtype=bool) if conn else np.zeros(len(quarters), dtype=bool)
    cur = next((c for prev, c in zip(every, every[1:]) if c["mode"] in _DISCHARGING and prev["mode"] not in _DISCHARGING
                and t0 <= int(c["start_ts"]) < t0 + 86400 and int(c["start_ts"]) >= marked - 900
                and (not pk.any() or bool(pk[(int(c["start_ts"]) - t0) // 900]))), None)
    if cur is None:
        return _keep_early(row["id"], {"verdict": "no_peak_slot", "reason": None, "plan_id": row["plan_id"], "checked": store._now()})
    bnd = int(cur["start_ts"])
    out = {"boundary_ts": bnd, "due_ts": bnd + EARLY_CHECK_S, "slot_start": cur.get("start"), "plan_id": row["plan_id"]}
    if now < bnd + EARLY_CHECK_S:
        return _keep_early(row["id"], {**out, "verdict": "waiting", "reason": None})
    g = telemetry.minute_grid(site_id, bnd - EARLY_CHECK_S, bnd + EARLY_CHECK_S)
    f = telemetry.filled(g)
    ts = g["ts"]
    bat = np.asarray(g["bat"], dtype=float)
    seen = ~np.isnan(bat) & (np.asarray(f["grid"]) > 0.5)
    after = seen & (ts >= bnd)
    soc = verify._soc(g)
    with_soc = np.flatnonzero(~np.isnan(soc))
    plant = twin.plant_of(site)
    if int(after.sum()) < 10 or not with_soc.size or plant.e_nom <= 0:
        # no battery reading for the window yet: the next tick tries again; the nightly check decides the day
        return _keep_early(row["id"], {**out, "verdict": "waiting", "reason": "no_battery_power"})
    p_dis = max(plant.p_dis, 1e-6)
    measured = bool(np.any(bat[after] > 0.10 * p_dis))
    k = int(with_soc[0])
    hours = ((ts + 5 * 3600) % 86400) // 3600
    load = f["load"] + f["unserved"]
    series = {"ts": ts[k:], "pv": (f["pv"] + f["curtail"])[k:], "load": load[k:], "grid": f["grid"][k:],
              "crit": np.minimum(twin.critical_share(site)[hours], load)[k:], "temp": np.nan_to_num(g["temp"], nan=30.0)[k:]}
    native_site = {**site, "native": verify.native_for_day(site_id, day) or site.get("native")}
    sims = {"table": twin.simulate(series, twin.slots_policy(every, series["ts"]), plant, float(soc[k])),
            "native": twin.simulate(series, twin.native_policy(native_site, series["ts"]), plant, float(soc[k]))}
    win = (series["ts"] >= bnd) & (np.asarray(series["grid"]) > 0.5)
    shows = {name: bool(np.any(np.asarray(sim["bat"])[win] > 0.10 * p_dis)) for name, sim in sims.items()}
    if not shows["table"]:
        verdict, reason = "no_evidence", "no_observable_change"          # the table itself would not discharge there (the battery near its floor)
    elif not shows["native"]:
        verdict, reason = ("took" if measured else "did_not_take"), None
    else:
        # the native settings would discharge there too: the measured energy over the hour says which the battery followed
        w = seen[k:]
        e_meas = float(np.sum(bat[k:][w]) / 60)
        d_tab = abs(float(np.sum(np.asarray(sims["table"]["bat"])[w]) / 60) - e_meas)
        d_nat = abs(float(np.sum(np.asarray(sims["native"]["bat"])[w]) / 60) - e_meas)
        margin = 0.02 * plant.e_nom
        verdict = "took" if d_tab + margin < d_nat else "did_not_take" if d_nat + margin < d_tab else "no_evidence"
        reason = "native_alike"
    return _keep_early(row["id"], {**out, "verdict": verdict, "reason": reason, "max_discharge_kw": round(max(float(np.max(bat[after])), 0.0), 1),
                                   "checked": store._now()})


# ------------------------------------------------------------------------------------------------ round 7 (audit): the closed-loop read-back and write log

def _same(a, b) -> bool:
    """Whether a value written and a value read back (or asked and written) are the same: numbers to within a millionth, anything else as is."""
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) < 1e-6
    return a == b


def ack_lines(body: dict) -> list[dict]:
    """An ack's lines per primitive (the read-back panel and the write log, plan Part 9.3 UI-15; audit role-operator-11): {device, primitive,
    requested, written, read_back, match, status, codes}. This contract's `items` say what was written and read back, with ok, clamped or failed and
    their codes; U3's `per_device` says what was asked, written and read back (`applied`: failed when the read-back differs, clamped when the value
    written is not the one asked) and what was refused (`rejected`, with its reason code). `match` is None where nothing was read back."""
    out = []
    for it in body.get("items") or []:
        w, rb = it.get("written"), it.get("read_back")
        out.append({"device": it.get("device"), "primitive": it.get("primitive"), "requested": None, "written": w, "read_back": rb,
                    "match": None if w is None or rb is None else _same(w, rb), "status": it.get("status"), "codes": list(it.get("codes") or [])})
    for d in body.get("per_device") or []:
        for a in d.get("applied") or []:
            req, w, rb = a.get("requested"), a.get("written"), a.get("readback")
            match = a.get("readback_ok") if a.get("readback_ok") is not None else (None if w is None or rb is None else _same(w, rb))
            status = "failed" if match is False else "clamped" if req is not None and w is not None and not _same(req, w) else "ok"
            out.append({"device": d.get("device_id"), "primitive": a.get("primitive"), "requested": req, "written": w, "read_back": rb, "match": match,
                        "status": status, "codes": []})
        for r in d.get("rejected") or []:
            out.append({"device": d.get("device_id"), "primitive": r.get("primitive"), "requested": None, "written": None, "read_back": None,
                        "match": None, "status": "rejected", "codes": [r["reason"]] if r.get("reason") else []})
    return out


def readback(site_id: str, now: int | None = None) -> dict:
    """The read-back panel (UI-15, closed loop; audit role-operator-11): the last plan message sent to the edge (a release aside), the edge's latest
    ack of that plan ({received_at, status, outcome, codes, detail}: contract.ack_outcome) with its lines per primitive (`primitives`: ack_lines),
    and today's fidelity from what the edge reported (edge_fidelity). plan_id, sent_at and ack are None before the first send."""
    from . import contract

    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    with store.connect() as con:
        sent = con.execute("SELECT plan_id, sent_at FROM plan_messages WHERE site_id = ? AND plan_id != 'release' ORDER BY sent_at DESC, rowid DESC "
                           "LIMIT 1", (site_id,)).fetchone()
        ack = con.execute("SELECT body, status, received_at FROM acks WHERE site_id = ? AND plan_id = ? ORDER BY received_at DESC, rowid DESC LIMIT 1",
                          (site_id, sent["plan_id"])).fetchone() if sent else None
    # its lines per primitive are `primitives`: a key named `lines` is a bill's, which the money filter removes from every reader without costs
    out = {"plan_id": sent["plan_id"] if sent else None, "sent_at": sent["sent_at"] if sent else None, "ack": None, "primitives": [],
           "fidelity_today": edge_fidelity(site_id, _local(now).date())}
    if ack is not None:
        body = json.loads(ack["body"])
        outcome, codes = contract.ack_outcome(body)
        out["ack"] = {"received_at": ack["received_at"], "status": ack["status"], "outcome": outcome, "codes": codes, "detail": body.get("detail")}
        out["primitives"] = ack_lines(body)
    return out


#: The edge's events the write log lists: what it says of its writes and of its control (U3 5.5; U3's names for them too)
WRITE_LOG_EVENTS = ("readback_mismatch", "write_failed", "budget_exhausted", "local_override", "override_local", "fallback_engaged", "fallback_cleared",
                    "control_disabled", "control_enabled", "template_change", "template_change_detected", "firmware_change", "firmware_change_detected")


def write_log(site_id: str, day: date, primitive: str | None = None) -> list[dict]:
    """The write log of a Karachi day (UI-15; U3 3.3's audit log and the evidence in a savings dispute; audit role-operator-11), newest first. Each
    row is {at, source (person, cloud or edge), who, kind, why, primitive} and the fields of its kind:
    - override_placed {override_kind, value, until} and override_cleared {override_kind}: a person's hold, release, pause, genset hold, charging
      block or forced charge (or the edge's own pause), with its reason as `why`;
    - marked_applied {plan_id, for_day, method}: a person keyed a table in;
    - table_issued {plan_id, for_day, reason}: the table issued for a day (no rupee gain: the log carries no money);
    - sent {plan_id, release_reason, before}: a send that changed what the inverter runs (a release is one; a repeat pull of the same table is not);
    - ack {plan_id, status, outcome, codes} and write {plan_id, device, requested, written, read_back, match, status, codes}: what the edge
      acknowledged, and per primitive what it wrote and read back;
    - event {event, detail}: what the edge reported of its writes and its control (WRITE_LOG_EVENTS).
    `primitive` keeps only the rows of that primitive."""
    from . import contract

    lo, hi = karachi_day_bounds(day)
    t0, t1 = _ts(lo), _ts(hi)
    with store.connect() as con:
        ovs = con.execute("SELECT * FROM overrides WHERE site_id = ? AND ((created >= ? AND created < ?) OR (cleared >= ? AND cleared < ?))",
                          (site_id, lo, hi, lo, hi)).fetchall()
        clears = con.execute("SELECT at, actor, detail FROM audit WHERE target = ? AND action = 'cleared an override' AND at >= ? AND at < ?",
                             (site_id, lo, hi)).fetchall()
        marks = con.execute("SELECT plan_id, day_local, by, method, created FROM applied WHERE site_id = ? AND created >= ? AND created < ?",
                            (site_id, lo, hi)).fetchall()
        issues = con.execute("SELECT plan_id, day_local, issued_at, reason FROM table_issues WHERE site_id = ? AND issued_at >= ? AND issued_at < ?",
                             (site_id, lo, hi)).fetchall()
        prev = con.execute("SELECT plan_id, body, sent_at FROM plan_messages WHERE site_id = ? AND sent_at < ? ORDER BY sent_at DESC, rowid DESC LIMIT 1",
                           (site_id, lo)).fetchone()
        sent = con.execute("SELECT plan_id, body, sent_at FROM plan_messages WHERE site_id = ? AND sent_at >= ? AND sent_at < ? ORDER BY sent_at, rowid",
                           (site_id, lo, hi)).fetchall()
        acks = con.execute("SELECT plan_id, status, body, received_at FROM acks WHERE site_id = ? AND received_at >= ? AND received_at < ?",
                           (site_id, lo, hi)).fetchall()
        evs = con.execute(f"SELECT ts, type, source, detail FROM events WHERE site_id = ? AND type IN ({','.join('?' * len(WRITE_LOG_EVENTS))})",
                          (site_id, *WRITE_LOG_EVENTS)).fetchall()
    edge_who = (sites.state(site_id).get("edge_heartbeat") or {}).get("edge_id") or "Edge device"
    rows: list[dict] = []

    def add(at: str, source: str, who: str, kind: str, why: str | None = None, primitive_: str | None = None, **fields) -> None:
        rows.append({"at": at, "source": source, "who": who, "kind": kind, "why": why or None, "primitive": primitive_, **fields})

    cleared_by = {}
    for c in clears:
        oid = (json.loads(c["detail"] or "{}") or {}).get("override_id")
        if oid:
            cleared_by[oid] = c["actor"]
    for o in ovs:
        src = "edge" if o["author"] == "edge" else "person"
        if lo <= o["created"] < hi:
            add(o["created"], src, o["author"], "override_placed", o["reason"], override_kind=o["kind"], value=o["value"], until=o["until_ts"])
        if o["cleared"] and lo <= o["cleared"] < hi:
            add(o["cleared"], "person", cleared_by.get(o["id"], "–"), "override_cleared", override_kind=o["kind"])
    for m in marks:
        add(m["created"], "person", m["by"], "marked_applied", plan_id=m["plan_id"], for_day=m["day_local"], method=m["method"])
    for i in issues:
        add(i["issued_at"], "cloud", "Gridlytics", "table_issued", plan_id=i["plan_id"], for_day=i["day_local"], reason=i["reason"])

    def written(r) -> tuple[str, dict]:
        try:
            body = json.loads(r["body"])
        except (TypeError, ValueError):
            return "", {}
        if body.get("type") == "release":
            return "release:" + str((body.get("release") or {}).get("reason")), body
        on = _local(_ts(r["sent_at"])).date().isoformat()
        return _table_key(next((x.get("slots") for x in (body.get("mode_a") or {}).get("days") or [] if x.get("date") == on), None)), body

    last, before = (written(prev)[0], prev["plan_id"]) if prev else (None, None)
    for r in sent:
        k, body = written(r)
        if k == last:
            continue                                   # a repeat pull of the table the inverter already runs is no write
        rel = (body.get("release") or {}) if body.get("type") == "release" else None
        add(r["sent_at"], "cloud", "Gridlytics", "sent", plan_id=None if rel is not None else r["plan_id"],
            release_reason=(rel or {}).get("reason") if rel is not None else None, before=before)
        last, before = k, r["plan_id"]
    for a in acks:
        body = json.loads(a["body"])
        outcome, codes = contract.ack_outcome(body)
        add(a["received_at"], "edge", edge_who, "ack", body.get("detail"), plan_id=a["plan_id"], status=a["status"], outcome=outcome, codes=codes)
        for ln in ack_lines(body):
            add(a["received_at"], "edge", edge_who, "write", None, ln["primitive"], plan_id=a["plan_id"],
                **{k: ln[k] for k in ("device", "requested", "written", "read_back", "match", "status", "codes")})
    for e in evs:
        at = contract.epoch(e["ts"])
        if at is None or not t0 <= at < t1:
            continue
        detail = json.loads(e["detail"] or "{}") or {}
        add(_iso_utc(at), "edge", edge_who if e["source"] in ("edge", "") else str(e["source"]).removeprefix("edge:"), "event",
            str(detail.get("reason") or detail.get("cause") or "") or None, detail.get("primitive"), event=contract.event_meaning(e["type"]),
            detail={k: detail[k] for k in ("requested", "readback", "action") if k in detail})
    if primitive:
        rows = [r for r in rows if r["primitive"] == primitive]
    return sorted(rows, key=lambda r: (_ts(r["at"]) or 0, r["kind"] == "write"), reverse=True)


# ------------------------------------------------------------------------------------------------ round 7 (audit): the fallback test (UI-15)

#: Plan Part 9.3 UI-15: "Test fallback" (an administrator's, closed loop) releases the inverter and verifies the restore (audit
#: flow-telemetry-plan-control-19). The release lasts FALLBACK_TEST_MIN; the edge must report the inverter's own settings during it (or within
#: FALLBACK_GRACE_S after: it beats once a minute) and the plan again within FALLBACK_RESTORE_S of its end (the next tick publishes a fresh plan within
#: 15 minutes and the edge pulls it).
FALLBACK_TEST_MIN, FALLBACK_GRACE_S, FALLBACK_RESTORE_S = 15, 300, 1800
FALLBACK_TEST_REASON = "Fallback test"
#: The heartbeat states in which the inverter runs its own settings
NATIVE_STATES = ("native", "released", "fallback")


def start_fallback_test(site_id: str, by: str, now: int | None = None) -> dict:
    """Starts a fallback test: a release to native (force_native, reason FALLBACK_TEST_REASON) for FALLBACK_TEST_MIN, and the test kept in the site's
    state (`fallback_test`: {id, override_id, by, started_at, until, released_at, restored_at}). The caller checks the tier, the edge and that no
    release is in force, and re-solves at once so the edge is sent the release."""
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    until = _iso_utc(now + FALLBACK_TEST_MIN * 60)
    o = sites.add_override(site_id, {"kind": "force_native", "until": until, "reason": FALLBACK_TEST_REASON}, by)
    test = {"id": store.new_id(), "override_id": o["id"], "by": by, "started_at": _iso_utc(now), "until": until, "released_at": None,
            "restored_at": None}
    sites.put_state(site_id, fallback_test=test)
    store.audit(by, "started a fallback test", site_id, {"until": until})
    return test


def fallback_test_step(test: dict, control_state: str | None, at: int) -> dict | None:
    """What a heartbeat's control state at `at` adds to a fallback test: `released_at` the first time the edge reports the inverter's own settings
    from the test's start to FALLBACK_GRACE_S after its end, then `restored_at` the first time it reports the plan again from the end to
    FALLBACK_RESTORE_S after it; else None."""
    from . import contract

    if not test or test.get("restored_at"):
        return None
    start, until = _ts(test["started_at"]), _ts(test["until"])
    if not test.get("released_at"):
        return {"released_at": _iso_utc(at)} if control_state in NATIVE_STATES and start <= at < until + FALLBACK_GRACE_S else None
    if control_state in contract.FOLLOWING_STATES and until <= at < until + FALLBACK_RESTORE_S:
        return {"restored_at": _iso_utc(at)}
    return None


def fallback_test_status(test: dict, now: int) -> dict:
    """The test with its `status`: running (no release reported yet), released (reported, the release still in force), restoring (the release over,
    the plan not yet reported), passed, or failed with its `cause`: not_released (nothing reported by FALLBACK_GRACE_S after the end) or not_restored
    (no plan reported by FALLBACK_RESTORE_S after it)."""
    until = _ts(test["until"])
    if test.get("restored_at"):
        status, cause = "passed", None
    elif not test.get("released_at"):
        status, cause = ("failed", "not_released") if now >= until + FALLBACK_GRACE_S else ("running", None)
    elif now >= until + FALLBACK_RESTORE_S:
        status, cause = "failed", "not_restored"
    else:
        status, cause = ("released" if now < until else "restoring"), None
    return {**test, "status": status, "cause": cause}


def fallback_test(site_id: str, now: int | None = None) -> dict | None:
    """The site's last fallback test with its status (fallback_test_status), else None."""
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    test = sites.state(site_id).get("fallback_test")
    return fallback_test_status(test, now) if test else None


def fallback_test_observe(site_id: str, control_state: str | None, at: int | None = None) -> dict | None:
    """A heartbeat's control state applied to the site's fallback test (routes_edge's heartbeat), read and written in one write transaction on the
    site's state; a restore says so in the audit trail. The test's change, else None."""
    at = int(at if at is not None else datetime.now(timezone.utc).timestamp())
    with store.write_transaction() as con:
        r = con.execute("SELECT data FROM site_state WHERE site_id = ?", (site_id,)).fetchone()
        cur = json.loads(r["data"]) if r else {}
        step = fallback_test_step(cur.get("fallback_test") or {}, control_state, at)
        if step:
            cur["fallback_test"] = {**cur["fallback_test"], **step}
            con.execute("UPDATE site_state SET data = ?, updated = ? WHERE site_id = ?", (json.dumps(cur, default=str), store._now(), site_id))
    if step and step.get("restored_at"):
        store.audit("edge", "passed a fallback test", site_id, {"released_at": cur["fallback_test"].get("released_at"), "restored_at": step["restored_at"]})
    return step
