"""The battery state service, method v1 (plan Part 4.6): the estimate and its uncertainty, a trust score, wear prices and the seven alerts.

Coulomb counting between anchors with the BMS as a trust-weighted prior is the edge's job where an edge exists; from cloud telemetry v1 takes the
estimate column when a source sends one and the BMS value otherwise, and grows sigma with the hours and throughput since the last anchor (a full charge,
`full_charge_level`, or the low knee), which is what the optimiser's reserve widening reads.
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from datetime import datetime, timezone

import numpy as np

from . import optimise, store, telemetry

#: R6 5.3's full-charge anchors (A1, A3) on cloud data: the estimate at 98 % or more
FULL_CHARGE = 0.98
#: the upper knee of an LFP pack (R6 5.2: about 3.40 V per cell under low current, SoC 95 to 100), where the voltage reads the state of charge
UPPER_KNEE = 0.95
#: a reading older than one control period (the tick's 15 minutes; R6 5.9: the optimiser's soc_est and sigma are never older than that) is
#: stale: from then on sigma grows by R6 5.3's gap term, 1 point per 10 minutes, as well as by the hours since the anchor
STALE_MINUTES = 15.0
PKT_OFFSET_S = 5 * 3600
#: a generic LFP pack's power limits against its temperature, as points (degrees C, share of the rated power), linear between and flat beyond
#: (round 7, brief-vs-build-05; the deep review's "respect temperature derating from BMS telemetry"). LFP datasheets stop charging under 0
#: degrees and over 55 and discharging under -20 and over 60, and derate near each limit; the figures are generic, UNVERIFIED for any pack. The
#: BMS's own limits would replace the curve where telemetry carries them (R6 5.1's battery_charge_limit_a and battery_discharge_limit_a, which
#: the telemetry contract does not hold yet)
CHARGE_DERATE = ((0.0, 0.0), (10.0, 1.0), (45.0, 1.0), (55.0, 0.0))
DISCHARGE_DERATE = ((-20.0, 0.0), (0.0, 1.0), (50.0, 1.0), (60.0, 0.0))
#: R6 5.7's derate: the discharge limit under half its rating for over 30 minutes
DERATE_ALERT_SHARE = 0.5
DERATE_ALERT_MINUTES = 30.0
#: the plan holds a derating for its first two hours: a pack cools over hours, and every 15-minute tick reads its temperature again
DERATE_HOLD_STEPS = 8
#: R6 5.7's capacity_drop: the BMS's state of health falls more than 3 points in a month (the 30 days the service reads)
CAPACITY_DROP_POINTS = 3.0


def derate_factors(temp_c: float) -> tuple[float, float]:
    """The shares of its rated charge and discharge power a pack at `temp_c` may use (CHARGE_DERATE, DISCHARGE_DERATE)."""
    t = float(temp_c)
    return tuple(float(np.interp(t, [a for a, _ in pts], [b for _, b in pts])) for pts in (CHARGE_DERATE, DISCHARGE_DERATE))


def _derating(b: dict, g: dict, now: int) -> tuple[dict, list]:
    """The pack's power limits now from its temperature (the last reading, where it is fresh: not older than one control period) and the
    `derate` alert where the discharge limit has been under half its rating for over 30 minutes."""
    temp = g["temp"]
    seen = np.flatnonzero(~np.isnan(temp))
    p_ch, p_dis = float(b["p_ch_kw"]), float(b["p_dis_kw"])
    if not len(seen) or (now - int(g["ts"][seen[-1]])) / 60.0 > STALE_MINUTES:
        return {"temp_c": None, "p_ch_limit_kw": p_ch, "p_dis_limit_kw": p_dis, "derated": False}, []
    t_now = float(temp[seen[-1]])
    f_ch, f_dis = derate_factors(t_now)
    info = {"temp_c": round(t_now, 1), "p_ch_limit_kw": round(p_ch * f_ch, 1), "p_dis_limit_kw": round(p_dis * f_dis, 1),
            "derated": bool(f_ch < 1.0 or f_dis < 1.0)}
    # how long the discharge limit has been under half its rating: back over the readings while it is, a gap longer than a control period
    # ends the run
    first, prev = None, None
    for i in seen[::-1]:
        if derate_factors(float(temp[i]))[1] >= DERATE_ALERT_SHARE or (prev is not None and (int(g["ts"][prev]) - int(g["ts"][i])) / 60.0
                                                                        > STALE_MINUTES):
            break
        first, prev = i, i
    alerts = []
    if first is not None and (now - int(g["ts"][first])) / 60.0 > DERATE_ALERT_MINUTES:
        alerts.append(("derate", f"The battery is at {t_now:.0f} degrees: the plan limits its discharge to {info['p_dis_limit_kw']:.0f} kW of its "
                                 f"{p_dis:.0f} kW and its charge to {info['p_ch_limit_kw']:.0f} kW of its {p_ch:.0f} kW until it cools"))
    return info, alerts


def _capacity_drop(site_id: str, now: int) -> tuple[float | None, list]:
    """The BMS's state of health now (the median of its last day of readings, battery_soh_pct) and the capacity_drop alert where it is more than
    CAPACITY_DROP_POINTS under the median of the first day of the 30 days read. None and no alert without readings."""
    d = telemetry.raw(site_id, now - 30 * 86400, now, ("battery_soh_pct",))
    v = d["battery_soh_pct"]
    ok = ~np.isnan(v)
    if not ok.any():
        return None, []
    ts, v = d["ts"][ok], v[ok]
    first = float(np.median(v[ts < ts[0] + 86400]))
    last = float(np.median(v[ts > ts[-1] - 86400]))
    days = int(round((int(ts[-1]) - int(ts[0])) / 86400))
    alerts = []
    if first - last > CAPACITY_DROP_POINTS:
        alerts.append(("capacity_drop", f"The BMS's state of health fell from {first:.0f} % to {last:.0f} % in {days} days"))
    return round(last, 1), alerts


def _local_midnight(ts: int) -> int:
    """The start (epoch seconds) of the Pakistan-time day that holds `ts` (UTC+5, no daylight saving)."""
    return (int(ts) + PKT_OFFSET_S) // 86400 * 86400 - PKT_OFFSET_S


def full_charge_level(b: dict) -> float:
    """The estimate at which a charge anchors sigma: 98 %, or a point under the pack's own top of charge (soc_max) where that lies on the upper
    knee. A pack that stops at its soc_max ends its charge there and its BMS re-reads the state of charge on the knee; every demo pack and most
    real ones stop at 95 %, so a 98 % rule alone never anchored them, sigma climbed 0.19 points an hour to its cap, and the reserve's 2-sigma
    widening (up to 30 % of the pack) kept the battery full through every peak (round 7, engine-plans-live-02). A top of charge on the plateau
    (under 95 %) is no anchor: the state of charge cannot be read there."""
    top = float(b.get("soc_max", 0.95))
    return min(FULL_CHARGE, top - 0.01) if top >= UPPER_KNEE - 1e-9 else FULL_CHARGE


def anchor_target(b: dict) -> float | None:
    """The charge the plan asks for to re-anchor the estimate (R6 5.7: a charge-to-full slot when sigma is high): the pack's top of charge where
    a charge there anchors (`full_charge_level`), else None, since the plan never charges past the pack's own soc_max."""
    top = float(b.get("soc_max", 0.95))
    level = full_charge_level(b)
    return min(top, level + 0.01) if level <= top + 1e-9 else None


def state(site: dict, now: int | None = None) -> dict | None:
    b = (site.get("assets") or {}).get("battery")
    if not b:
        return None
    now = int(now or datetime.now(timezone.utc).timestamp())
    g = telemetry.minute_grid(site["id"], now - 30 * 86400, now)
    e_nom = b["nameplate_kwh"] * b.get("soh", 1.0)
    est = np.where(np.isnan(g["soc"]), g["soc_bms"], g["soc"])
    known = ~np.isnan(est)
    # the kWh discharged since local midnight (Pakistan time), which the day's cycle cap counts first (round 7, brief-vs-build-04)
    today = g["ts"] >= _local_midnight(now)
    done = float(np.nansum(np.maximum(g["bat"][today], 0.0)) / 60.0)
    # the pack's temperature derating and the BMS's state of health (round 7, brief-vs-build-05): the derate and capacity_drop alerts of R6 5.7
    derate, pack_alerts = _derating(b, g, now)
    soh_bms, drop = _capacity_drop(site["id"], now)
    pack_alerts += drop
    out = {"e_nom_kwh": round(e_nom, 1), "c_cyc": round(optimise.wear_cost(b), 2), "soh_pct": round(b.get("soh", 1.0) * 100, 1),
           "p_ch_kw": b["p_ch_kw"], "p_dis_kw": b["p_dis_kw"], "soc_min_pct": b["soc_min"] * 100, "soc_max_pct": b["soc_max"] * 100,
           "discharged_today_kwh": round(done, 2), **derate, "soh_bms_pct": soh_bms}
    if not known.any():
        return {**out, "soc_pct": None, "sigma_pct": 25.0, "trust": None, "trust_basis": None, "usable_kwh": None, "anchor": None,
                "alerts": pack_alerts, "waiting_for_anchor": True, "reading_age_min": None, "stale": True, "anchor_due": False, "anchor_soc_pct": None}
    last = int(np.where(known)[0][-1])
    soc = float(est[last])
    # sigma at now, not at the last reading (round 7, engine-optimiser-07): it froze where the data stopped, so a plan started from a reading
    # hours old with the margin it had then. The hours since the anchor run to now, and a reading older than one control period adds the gap term
    age_min = max(0.0, (now - int(g["ts"][last])) / 60.0)
    gap_pts = max(0.0, age_min - STALE_MINUTES) / 10.0
    anchors = np.where(known & ((est >= full_charge_level(b) - 1e-9) | (est <= b["soc_min"] + 0.02)))[0]
    if len(anchors):
        a = int(anchors[-1])
        hours = (now - int(g["ts"][a])) / 3600.0
        thr = float(np.nansum(np.abs(g["bat"][a:last + 1])) / 60.0)
        sigma = min(25.0, 1.0 + 0.19 * hours + 2.0 * thr / max(e_nom, 1e-6) + gap_pts)
        anchor_ts = int(g["ts"][a])
    else:
        sigma, anchor_ts = min(25.0, 15.0 + gap_pts), None
    # the estimate against the BMS only where both were measured: g["soc"] falls back to the BMS where the estimate is missing (a CSV history, an edge
    # that sends the estimate part of the time), and comparing the BMS with itself there would dilute the gap and hide a low trust
    soc_est = g.get("soc_est", g["soc"])
    both = known & ~np.isnan(g["soc_bms"]) & ~np.isnan(soc_est)
    anchor_cov = min(1.0, len(np.unique(g["ts"][anchors] // 86400)) / 30.0) if len(anchors) else 0.0
    if both.any():
        gap = float(np.mean(np.abs(g["soc_bms"][both] - soc_est[both])) * 100)
        trust = round(0.4 * max(0.0, 1 - gap / 10.0) + 0.3 * max(0.0, 1 - sigma / 25.0) + 0.2 * (1.0 if gap < 5 else 0.5) + 0.1 * anchor_cov, 3)
        basis = "independent"
    else:
        # nothing independent to check the BMS against (a BMS-only or CSV source: the estimate is the BMS itself): trust is not assessed (round 7,
        # brief-vs-build-03). Scored with a gap of 0 it read 0.6 to 1.0 whatever the BMS's quality, and trust_low could never fire
        trust, basis = None, "bms_only"
    usable = e_nom * max(0.0, soc - 2 * sigma / 100.0 - b["soc_min"]) * b["eta_d"]
    alerts = []
    if trust is not None and trust < 0.5:
        alerts.append(("trust_low", f"Battery estimate trust is {trust:.2f}, under 0.5"))
    bms_now = g["soc_bms"][last]
    if not np.isnan(bms_now) and abs(bms_now - soc) > 0.10:
        alerts.append(("soc_disagreement", f"BMS reads {bms_now * 100:.0f} percent against an estimate of {soc * 100:.0f} percent"))
    # an anchor is due with sigma over 15 points (R6 5.7) or none in the 30 days read, where the plan can reach one (round 7, engine-optimiser-15:
    # the alert promised "a full charge to re-anchor" that nothing planned), and only on a reading that is not stale: until readings arrive
    # again a charge would not be seen
    target = anchor_target(b)
    anchor_due = bool(target is not None and (sigma > 15 or anchor_ts is None) and age_min <= STALE_MINUTES)
    if sigma > 15:
        if target is not None:
            text = (f"The battery estimate may be off by up to {sigma:.0f} percentage points; the plan charges the battery to {target * 100:.0f} % "
                    "once to correct it")
        else:
            text = (f"The battery estimate may be off by up to {sigma:.0f} percentage points; a charge to 98 % would correct it, and the battery is "
                    f"set to stop charging at {float(b['soc_max']) * 100:.0f} %")
        alerts.append(("sigma_high", text))
    day = slice(max(0, last - 1440), last + 1)
    hot = np.nansum((est[day] > 0.9) & (g["temp"][day] > 35)) / 60.0
    if hot > 2:
        # the title names its 24 hours, as the other alerts name their day (round 7 sweeps, a follow-up of role-operator-31: "in the last day" was
        # still read after the alert cleared); Karachi time
        to = datetime.fromtimestamp(int(g["ts"][last]) + 60 + PKT_OFFSET_S, timezone.utc)
        alerts.append(("hot_high_soc", f"{hot:.1f} hours above 90 percent charge above 35 degrees in the 24 hours to {to.day} {to:%b %H:%M}"))
    alerts += pack_alerts
    disch = np.nansum(np.maximum(g["bat"], 0)) / 60.0
    days_seen = max(1.0, known.sum() / 1440)
    cycles = disch / max(e_nom, 1e-6)
    return {**out, "soc_pct": round(soc * 100, 1), "soc_bms_pct": None if np.isnan(bms_now) else round(float(bms_now) * 100, 1),
            "sigma_pct": round(sigma, 1), "trust": trust, "trust_basis": basis, "usable_kwh": round(usable, 1), "anchor": anchor_ts, "alerts": alerts,
            "throughput_30d_kwh": round(float(disch), 1), "cycles_30d": round(cycles, 2), "cycles_per_day": round(cycles / days_seen, 2),
            "hot_full_hours_24h": round(float(hot), 1), "wear_30d_rs": round(float(disch * optimise.wear_cost(b)), 0), "last_ts": int(g["ts"][last]),
            "waiting_for_anchor": anchor_ts is None, "reading_age_min": round(age_min, 1), "stale": bool(age_min > STALE_MINUTES),
            "anchor_due": anchor_due, "anchor_soc_pct": round(target * 100, 1) if anchor_due else None}


#: how long a page's battery estimate is reused (estimate): the overview's 30-second poll and the battery page's read share one per minute
ESTIMATE_REUSE_S = 60
_ESTIMATES_KEPT = 512
_estimates: "OrderedDict[tuple, tuple[int, dict | None]]" = OrderedDict()
_estimates_lock = threading.Lock()


def estimate(site: dict, now: int | None = None, last_ts: int | None = None) -> dict | None:
    """`state` for a page: reused for ESTIMATE_REUSE_S while the site's newest reading (`last_ts`, telemetry.latest's ts) and its battery are the
    same, so a site's open tabs share one 30-day read a minute (round 7, audit performance-live-02: every 30-second /now poll of every open tab
    re-read 30 days of minutes, about 0.1 s of the API's one worker; the battery page read it twice a refresh, /now and /battery). A new reading,
    a changed battery or another company's file computes it again; the solve reads `state` itself."""
    b = (site.get("assets") or {}).get("battery")
    if not b:
        return None
    now = int(now or datetime.now(timezone.utc).timestamp())
    key = (str(store.telemetry_path()), site["id"], json.dumps(b, sort_keys=True, default=str), last_ts)
    with _estimates_lock:
        hit = _estimates.get(key)
        if hit is not None and 0 <= now - hit[0] < ESTIMATE_REUSE_S:
            _estimates.move_to_end(key)
            return dict(hit[1]) if hit[1] is not None else None
    st = state(site, now)
    with _estimates_lock:
        _estimates[key] = (now, st)
        _estimates.move_to_end(key)
        while len(_estimates) > _ESTIMATES_KEPT:
            _estimates.popitem(last=False)
    return dict(st) if st is not None else None
