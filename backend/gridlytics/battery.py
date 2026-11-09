"""The battery state service, method v1 (plan Part 4.6): the estimate and its uncertainty, a trust score, wear prices and the seven alerts.

Coulomb counting between anchors with the BMS as a trust-weighted prior is the edge's job where an edge exists; from cloud telemetry v1 takes the
estimate column when a source sends one and the BMS value otherwise, and grows sigma with the hours and throughput since the last anchor (a full charge,
`full_charge_level`, or the low knee), which is what the optimiser's reserve widening reads.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from . import optimise, telemetry

#: R6 5.3's full-charge anchors (A1, A3) on cloud data: the estimate at 98 % or more
FULL_CHARGE = 0.98
#: the upper knee of an LFP pack (R6 5.2: about 3.40 V per cell under low current, SoC 95 to 100), where the voltage reads the state of charge
UPPER_KNEE = 0.95
#: a reading older than one control period (the tick's 15 minutes; R6 5.9: the optimiser's soc_est and sigma are never older than that) is
#: stale: from then on sigma grows by R6 5.3's gap term, 1 point per 10 minutes, as well as by the hours since the anchor
STALE_MINUTES = 15.0


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
    out = {"e_nom_kwh": round(e_nom, 1), "c_cyc": round(optimise.wear_cost(b), 2), "soh_pct": round(b.get("soh", 1.0) * 100, 1),
           "p_ch_kw": b["p_ch_kw"], "p_dis_kw": b["p_dis_kw"], "soc_min_pct": b["soc_min"] * 100, "soc_max_pct": b["soc_max"] * 100}
    if not known.any():
        return {**out, "soc_pct": None, "sigma_pct": 25.0, "trust": None, "trust_basis": None, "usable_kwh": None, "anchor": None, "alerts": [],
                "waiting_for_anchor": True, "reading_age_min": None, "stale": True, "anchor_due": False, "anchor_soc_pct": None}
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
        alerts.append(("hot_high_soc", f"{hot:.1f} hours above 90 percent charge above 35 degrees in the last day"))
    disch = np.nansum(np.maximum(g["bat"], 0)) / 60.0
    days_seen = max(1.0, known.sum() / 1440)
    cycles = disch / max(e_nom, 1e-6)
    return {**out, "soc_pct": round(soc * 100, 1), "soc_bms_pct": None if np.isnan(bms_now) else round(float(bms_now) * 100, 1),
            "sigma_pct": round(sigma, 1), "trust": trust, "trust_basis": basis, "usable_kwh": round(usable, 1), "anchor": anchor_ts, "alerts": alerts,
            "throughput_30d_kwh": round(float(disch), 1), "cycles_30d": round(cycles, 2), "cycles_per_day": round(cycles / days_seen, 2),
            "hot_full_hours_24h": round(float(hot), 1), "wear_30d_rs": round(float(disch * optimise.wear_cost(b)), 0), "last_ts": int(g["ts"][last]),
            "waiting_for_anchor": anchor_ts is None, "reading_age_min": round(age_min, 1), "stale": bool(age_min > STALE_MINUTES),
            "anchor_due": anchor_due, "anchor_soc_pct": round(target * 100, 1) if anchor_due else None}
