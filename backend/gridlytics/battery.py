"""The battery state service, method v1 (plan Part 4.6): the estimate and its uncertainty, a trust score, wear prices and the seven alerts.

Coulomb counting between anchors with the BMS as a trust-weighted prior is the edge's job where an edge exists; from cloud telemetry v1 takes the
estimate column when a source sends one and the BMS value otherwise, and grows sigma with the hours and throughput since the last anchor (a full charge
above 98 percent or the low knee), which is what the optimiser's reserve widening reads.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from . import optimise, telemetry


def state(site: dict, now: int | None = None) -> dict | None:
    b = (site.get("assets") or {}).get("battery")
    if not b:
        return None
    now = int(now or datetime.now(timezone.utc).timestamp())
    g = telemetry.minute_grid(site["id"], now - 30 * 86400, now)
    e_nom = b["nameplate_kwh"] * b.get("soh", 1.0)
    est = np.where(np.isnan(g["soc"]), g["soc_bms"], g["soc"])
    known = ~np.isnan(est)
    out = {"e_nom_kwh": round(e_nom, 1), "c_cyc": round(optimise.wear_cost(b), 2), "soh_pct": round(b.get("soh", 1.0) * 100, 1),
           "p_ch_kw": b["p_ch_kw"], "p_dis_kw": b["p_dis_kw"], "soc_min_pct": b["soc_min"] * 100, "soc_max_pct": b["soc_max"] * 100}
    if not known.any():
        return {**out, "soc_pct": None, "sigma_pct": 25.0, "trust": None, "usable_kwh": None, "anchor": None, "alerts": [], "waiting_for_anchor": True}
    last = int(np.where(known)[0][-1])
    soc = float(est[last])
    anchors = np.where(known & ((est >= 0.98) | (est <= b["soc_min"] + 0.02)))[0]
    if len(anchors):
        a = int(anchors[-1])
        hours = (last - a) / 60.0
        thr = float(np.nansum(np.abs(g["bat"][a:last + 1])) / 60.0)
        sigma = min(25.0, 1.0 + 0.19 * hours + 2.0 * thr / max(e_nom, 1e-6))
        anchor_ts = int(g["ts"][a])
    else:
        sigma, anchor_ts = 15.0, None
    both = known & ~np.isnan(g["soc_bms"]) & ~np.isnan(g["soc"])
    gap = float(np.mean(np.abs(g["soc_bms"][both] - g["soc"][both])) * 100) if both.any() else 0.0
    anchor_cov = min(1.0, len(np.unique(g["ts"][anchors] // 86400)) / 30.0) if len(anchors) else 0.0
    trust = round(0.4 * max(0.0, 1 - gap / 10.0) + 0.3 * max(0.0, 1 - sigma / 25.0) + 0.2 * (1.0 if gap < 5 else 0.5) + 0.1 * anchor_cov, 3)
    usable = e_nom * max(0.0, soc - 2 * sigma / 100.0 - b["soc_min"]) * b["eta_d"]
    alerts = []
    if trust < 0.5:
        alerts.append(("trust_low", f"Battery estimate trust is {trust:.2f}, under 0.5"))
    bms_now = g["soc_bms"][last]
    if not np.isnan(bms_now) and abs(bms_now - soc) > 0.10:
        alerts.append(("soc_disagreement", f"BMS reads {bms_now * 100:.0f} percent against an estimate of {soc * 100:.0f} percent"))
    if sigma > 15:
        alerts.append(("sigma_high", f"Estimate uncertainty is {sigma:.0f} points; the plan will ask for a full charge to re-anchor"))
    day = slice(max(0, last - 1440), last + 1)
    hot = np.nansum((est[day] > 0.9) & (g["temp"][day] > 35)) / 60.0
    if hot > 2:
        alerts.append(("hot_high_soc", f"{hot:.1f} hours above 90 percent charge above 35 degrees in the last day"))
    disch = np.nansum(np.maximum(g["bat"], 0)) / 60.0
    days_seen = max(1.0, known.sum() / 1440)
    cycles = disch / max(e_nom, 1e-6)
    return {**out, "soc_pct": round(soc * 100, 1), "soc_bms_pct": None if np.isnan(bms_now) else round(float(bms_now) * 100, 1),
            "sigma_pct": round(sigma, 1), "trust": trust, "usable_kwh": round(usable, 1), "anchor": anchor_ts, "alerts": alerts,
            "throughput_30d_kwh": round(float(disch), 1), "cycles_30d": round(cycles, 2), "cycles_per_day": round(cycles / days_seen, 2),
            "hot_full_hours_24h": round(float(hot), 1), "wear_30d_rs": round(float(disch * optimise.wear_cost(b)), 0), "last_ts": int(g["ts"][last]),
            "waiting_for_anchor": anchor_ts is None}
