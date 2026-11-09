"""The three forecasters, stages 0 and 1 (plan Part 5): PV per site, load per site, outage probability per feeder.

Every issue covers 192 steps of 15 minutes from its belief time, carries seven quantiles (q05 to q95) and its stage, and reads only telemetry before the
belief time, so a replayed day sees exactly what was known then. Stage 2 models (xgboost) and NWP feeds are later items; until then the PV chain runs on
the clear-sky model with a climatological clearness distribution (stage 0) and a per-site capacity and clearness fit (stage 1), and every screen marks
it Preliminary.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import warnings

import numpy as np

warnings.filterwarnings("ignore", category=RuntimeWarning, message="(Mean of empty slice|All-NaN slice)")

from . import pk_calendar, solar, store, telemetry, twin

PKT = timezone(timedelta(hours=5))
STEPS = 192
STEP_S = 900
QS = ("q05", "q10", "q25", "q50", "q75", "q90", "q95")
QV = np.array([0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95])
VERSION = "v1.0"
#: stage-0 prior on the daily clearness index by quantile (scaled by the month's climatology)
K_PRIOR = np.array([0.30, 0.45, 0.70, 0.86, 0.95, 1.00, 1.02])
#: the month's clearness against solar.pv_power's clear sky (the stage-0 median): the four demo locations' mean monthly PV output over 2005-2020
#: (PVGIS), its GHI averaged with NASA POWER's climatology (2001-2025), over the clear-sky output of the same 100 kWp system (tests/test_public_data.py).
#: v1's table, set by hand against the old single haze factor, put stage 0's annual output 6-12 % and its winter months 20-28 % below PVGIS.
MONTH_K = {1: .85, 2: .87, 3: .88, 4: .90, 5: .90, 6: .88, 7: .81, 8: .80, 9: .87, 10: .88, 11: .89, 12: .88}
DISCO_TRIPS = {"QESCO": 1.6, "PESCO": 1.4, "SEPCO": 1.5, "HESCO": 1.2, "TESCO": 1.5, "MEPCO": 0.8, "GEPCO": 0.6, "FESCO": 0.7, "HAZECO": 0.8,
               "LESCO": 0.35, "IESCO": 0.3, "K-Electric": 0.5}
#: the cold start of a feeder without an announced schedule (plan Part 5.3: "a hand-entered prior (for example 10 hours a day on QESCO, PESCO and SEPCO
#: feeders ...) flagged prior on screen"): the hours a day without grid, scheduled shedding included, on the rural DISCOs (the twin's rural set). The
#: share the trip prior (DISCO_TRIPS) does not cover is spread evenly over the day as blocks of SHED_BLOCK_STEPS (the twin's 2-hour window), with the
#: hazard prior's 7 days' weight, until a schedule is entered; the issue flags it (`shed_prior`). The urban feeders keep the trip prior alone.
COLD_START_DOWN_HOURS = {"QESCO": 10.0, "PESCO": 10.0, "SEPCO": 10.0, "HESCO": 10.0, "TESCO": 10.0}
SHED_BLOCK_STEPS = 8
#: the observed grid state (outage): the last minute with a grid reading may lag the last whole minute by at most this many minutes and still be taken as
#: the state now (the newest batch may still be in flight); routes_edge's event re-solve waits on the same rule
OBSERVED_LAG_MIN = 2


def align(ts: float) -> int:
    return int(ts) // STEP_S * STEP_S


def sample_minutes(known) -> int:
    """The minutes one sample of a source stands for: the median gap between its known samples on a minute grid (1 for a 1-minute source, 5 for a
    5-minute one), at most one 15-minute step. The stage rules multiply sample counts by it, so a coarse source reaches them on days of data, not on
    rows (plan Part 5.2)."""
    idx = np.flatnonzero(np.asarray(known, dtype=bool))
    if len(idx) < 2:
        return 1
    return int(min(STEP_S // 60, max(1, round(float(np.median(np.diff(idx)))))))


def _local_midnight(ts: int) -> int:
    """The start (UTC epoch seconds) of the Pakistan-time day that holds ts."""
    return int((int(ts) + 5 * 3600) // 86400 * 86400 - 5 * 3600)


def step_times(t0: int, steps: int = STEPS) -> np.ndarray:
    return t0 + STEP_S * np.arange(steps, dtype=np.int64)


def _mean15(f, t: np.ndarray) -> np.ndarray:
    """Mean of a minute-resolution function over each 15-minute step (sampled at 7.5-minute offsets on a 5-minute lattice)."""
    return np.mean([f(t + o) for o in (150, 450, 750)], axis=0)


# ------------------------------------------------------------------------------------------------ PV

def available_pv(g: dict) -> np.ndarray:
    """The PV a plant could deliver at each minute of a minute grid (kW): its output plus what it curtailed, unknown where the output is. A zero-export
    site curtails once its battery is full, and its clearest days are the most curtailed; the forecast, its fit and its score describe the plant, and
    curtailment is the optimiser's own decision (K[t]), so they read available power, not output."""
    pvm = np.asarray(g["pv"], dtype=float)
    cur = g.get("curtail")
    return pvm if cur is None else pvm + np.nan_to_num(np.asarray(cur, dtype=float))


def pv(site: dict, t0: int, steps: int = STEPS) -> dict:
    pv_asset = (site.get("assets") or {}).get("pv")
    t = step_times(t0, steps)
    if not pv_asset:
        z = np.zeros(steps)
        return {"stage": 0, "cs": z, "q": {q: z for q in QS}, "pr": None, "note": "No PV on this site"}
    lat, lon = site["data"].get("lat", 31.5), site["data"].get("lon", 74.3)
    months = solar.local_month(t)
    cs = _mean15(lambda x: solar.pv_power(x, lat, lon, pv_asset, 1.0, months), t)
    # history: the 14 whole local days before t0's own day, measured PV against the clear-sky model, by day. The slices run from local midnight to
    # local midnight, so every tick of a day fits the same days; 24-hour blocks from the tick's time of day would mix one day's afternoon with the
    # next day's morning and pull the ratio and the fan towards the mean through the day. The grid runs on to t0 for the last hour's blend below.
    h0 = _local_midnight(t0) - 14 * 86400
    g = telemetry.minute_grid(site["id"], h0, t0)
    days_ok, k_days, pr = 0, [], None
    fit = slice(0, 14 * 1440)
    iv = sample_minutes(g["present"])  # sample counts in minutes of data, so a 5- or 15-minute source qualifies as a 1-minute one does
    if g["present"][fit].sum() * iv > 3 * 600:
        csm = solar.pv_power(g["ts"][fit], lat, lon, pv_asset, 1.0, solar.local_month(g["ts"][fit]))
        pvm = available_pv(g)[fit]
        for d in range(14):
            sl = slice(d * 1440, (d + 1) * 1440)
            ok = ~np.isnan(pvm[sl]) & (csm[sl] > 0.2 * pv_asset["kw_ac"])
            if ok.sum() * iv > 240:
                days_ok += 1
                k_days.append(float(np.sum(pvm[sl][ok]) / max(1e-6, np.sum(csm[sl][ok]))))
    stage = 1 if days_ok >= 3 else 0
    month_k = MONTH_K[int(months[0])]
    if stage == 1:
        k = np.array(k_days)
        pr = float(np.clip(np.quantile(k, 0.9), 0.5, 1.15))  # the performance ratio on the clearest days
        kn = np.clip(k / pr, 0.05, 1.1)
        if len(kn) >= 7:
            kq = np.quantile(kn, QV)
        else:
            # 3 to 6 fitted days: the prior's shape, its median moved from the month's climatology towards the site's own median clearness with
            # weight days / 7, so the step to the empirical quantiles at 7 days is small and the first week reads the site's own days
            med = month_k + (float(np.median(kn)) - month_k) * len(kn) / 7.0
            kq = np.clip(K_PRIOR * med / K_PRIOR[3], 0.05, 1.1)
        kq = np.maximum.accumulate(kq)
        scale = pr
    else:
        kq = np.clip(K_PRIOR * month_k / 0.86, 0.05, 1.05)
        scale = 1.0
    # no quantile above the plant's clear-sky output: the model's, or the site's own where its fitted ratio is above 1 (a plant the nominal model
    # under-rates would otherwise have its clear days cut to the nominal clear sky), and never above the inverter's AC rating (cs is clipped there,
    # but a ratio above 1 times a clipped cs is not, so a clipping plant with pr > 1 would otherwise be forecast PV its inverter cannot deliver)
    cap = np.minimum(cs * max(1.0, scale), float(pv_asset["kw_ac"]))
    q = {name: np.minimum(cap, cs * scale * kq[i]) for i, name in enumerate(QS)}
    # smart persistence for the first 2 hours: blend the q50 towards the last hour's observed clearness
    if stage == 1 and g["present"][-60:].sum() * iv > 30:
        last = available_pv(g)[-60:]
        csl = solar.pv_power(g["ts"][-60:], lat, lon, pv_asset, 1.0, solar.local_month(g["ts"][-60:]))
        ok = ~np.isnan(last) & (csl > 0.1 * pv_asset["kw_ac"])
        if ok.sum() * iv > 20:
            k_now = float(np.clip(np.sum(last[ok]) / np.sum(csl[ok]) / scale, 0.05, 1.1))
            w = np.clip(1 - np.arange(steps) / 8.0, 0, 1)
            q50_0 = q["q50"].copy()  # every quantile keeps its ratio to the unblended median, so the fan moves as one
            for name in QS:
                q[name] = np.minimum(cap, q[name] * (1 - w) + cs * scale * k_now * w * (q[name] / np.maximum(q50_0, 1e-6)))
    # quantiles sorted per step (plan Part 5.1)
    qa = np.maximum.accumulate(np.vstack([q[name] for name in QS]), axis=0)
    q = {name: qa[i] for i, name in enumerate(QS)}
    return {"stage": stage, "cs": cs, "q": q, "pr": pr, "days": days_ok}


# ------------------------------------------------------------------------------------------------ load

#: the load's day types (plan Part 5.2): equivalent-day persistence reads the same slot on the last days of the same type
DAY_WORKING, DAY_OFF, DAY_HOLIDAY, DAY_RAMADAN, DAY_EID, DAY_HALF = range(6)
DAY_NAMES = {DAY_WORKING: "working", DAY_OFF: "off_day", DAY_HOLIDAY: "public_holiday", DAY_RAMADAN: "ramadan_working_day", DAY_EID: "eid",
             DAY_HALF: "half_day"}
#: where a day type has too few days in the history (a holiday, Eid, the first days of Ramadan), the persistence reads these types next, in order,
#: then the template
DAY_FALLBACK = {DAY_EID: (DAY_HOLIDAY, DAY_OFF), DAY_HOLIDAY: (DAY_OFF,), DAY_RAMADAN: (DAY_WORKING,), DAY_HALF: (DAY_WORKING,)}
#: the day types the stage-0 template draws with its off-day shape (twin.load_template's Sunday)
OFF_TYPES = (DAY_OFF, DAY_HOLIDAY, DAY_EID)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekdays(site: dict | None, key: str, default: tuple) -> set[int]:
    v = ((site or {}).get("data") or {}).get(key)
    return {WEEKDAYS.index(d) for d in (default if v is None else v) if d in WEEKDAYS}


def _day_type(ts: np.ndarray, site: dict | None = None) -> np.ndarray:
    """The day type of each time's local day: Eid and public holidays from Pakistan's calendar (pk_calendar), then the site's weekly off days
    (`off_days`, Sunday when unset) and half days (`half_days`), then a working day of Ramadan, else a working day."""
    off, half = _weekdays(site, "off_days", ("sun",)), _weekdays(site, "half_days", ())
    days = (np.asarray(ts, dtype=np.int64) + 5 * 3600) // 86400
    out = np.full(len(days), DAY_WORKING)
    for dn in np.unique(days):
        d = date(1970, 1, 1) + timedelta(days=int(dn))
        kind = (pk_calendar.day(d) or (None,))[0]
        out[days == dn] = (DAY_EID if kind == "eid" else DAY_HOLIDAY if kind == "holiday" else DAY_OFF if d.weekday() in off
                           else DAY_HALF if d.weekday() in half else DAY_RAMADAN if kind == "ramadan" else DAY_WORKING)
    return out


def _calendar(t: np.ndarray, types: np.ndarray) -> list[dict]:
    """The local days of a horizon that are not plain working days: {day, type, name (the holiday's, or None), estimated (a lunar date not yet
    announced)}."""
    out = []
    days = (np.asarray(t, dtype=np.int64) + 5 * 3600) // 86400
    for dn in np.unique(days):
        ty = int(types[np.argmax(days == dn)])
        if ty == DAY_WORKING:
            continue
        d = date(1970, 1, 1) + timedelta(days=int(dn))
        sp = pk_calendar.day(d) if ty in (DAY_HOLIDAY, DAY_EID, DAY_RAMADAN) else None
        out.append({"day": d.isoformat(), "type": DAY_NAMES[ty], "name": sp[1] if sp else None, "estimated": bool(sp[2]) if sp else False})
    return out


def _template(site: dict, t: np.ndarray, types: np.ndarray) -> np.ndarray:
    """The stage-0 template per step by day type: twin.load_template's weekly shape has Sunday as its off day, so an off-type day (the site's off day,
    a public holiday, Eid) reads it on a Sunday at the same time of day, and every other day on a Monday."""
    dow = ((np.asarray(t, dtype=np.int64) + 5 * 3600) // 86400 + 3) % 7  # 0 = Monday
    shift = ((np.where(np.isin(types, OFF_TYPES), 6, 0) - dow) % 7) * 86400
    return _mean15(lambda x: twin.load_template(site, x), np.asarray(t, dtype=np.int64) + shift)


#: the drop rule's outage runs are formed over the known samples of the minute grid (plan Part 5.2): minutes without a grid reading (a lost row, a 5- or
#: 15-minute source) between two grid-down samples continue the outage up to OUTAGE_GAP_MINUTES apart, and a grid-up blip of at most FLICKER_MINUTES
#: samples between them does too (the flicker's own minutes are censored with the run: doubtful samples leave the medians)
OUTAGE_GAP_MINUTES = 15
FLICKER_MINUTES = 2
#: the reference load before a grid loss: the median of the last REF_SAMPLES known grid-up samples within REF_MINUTES before the run (at least 2)
REF_MINUTES = 60
REF_SAMPLES = 15


def _outage_runs(grid: np.ndarray) -> list[tuple[int, int]]:
    """(first, last) minute index of each outage over the known grid samples, bridging unknown minutes and short grid-up flickers (see above)."""
    known = ~np.isnan(grid)
    down_idx = np.where(known & (grid == 0))[0]
    if not len(down_idx):
        return []
    ups = np.cumsum(known & (grid == 1))  # grid-up samples up to and including each minute
    brk = (np.diff(down_idx) > OUTAGE_GAP_MINUTES) | ((ups[down_idx[1:]] - ups[down_idx[:-1]]) > FLICKER_MINUTES)
    starts = down_idx[np.concatenate([[True], brk])]
    ends = down_idx[np.concatenate([brk, [True]])]
    return list(zip(starts.tolist(), ends.tolist()))


def censored_minutes(site: dict, g: dict) -> np.ndarray:
    """Minutes of a minute grid whose measured load is not the site's demand (plan Part 5.2): the source's own censored flag and, while the grid is
    down, unserved load, a battery at its cut-off plus 5 points with the genset not running (an unknown genset counts as not running here, so a doubtful
    sample leaves the medians), or a load that fell more than 40 percent within two minutes of the grid loss and stayed down (only the backed-up
    circuits were served).

    The drop rule reads the known samples, not consecutive minutes, so a lost row, a one- or two-minute grid flicker or a 5-minute source does not
    split an outage: the reference is the median of the last known grid-up samples before the loss (up to 15 within an hour), the drop is tested on the
    first known samples after it (within two minutes, or within the source's own sample interval when that is longer), and "stayed down" is the median
    of the outage's grid-down samples. The whole run, from its first to its last grid-down sample, is censored."""
    lo, grid = np.asarray(g["load"], dtype=float), np.asarray(g["grid"], dtype=float)
    n = len(lo)
    down = grid == 0
    cen = np.asarray(g.get("censored", np.full(n, np.nan))) == 1
    cen = cen | (down & (np.nan_to_num(g.get("unserved", np.zeros(n))) > 0))
    bat = (site.get("assets") or {}).get("battery")
    if bat and "soc" in g:
        cen |= down & (np.asarray(g.get("gen_on", np.full(n, np.nan))) != 1) & (g["soc"] <= float(bat.get("soc_min", 0.10)) + 0.05)
    runs = _outage_runs(grid)
    if not runs:
        return cen
    known_idx = np.where(~np.isnan(grid))[0]
    up_idx = np.where((grid == 1) & ~np.isnan(lo))[0]  # grid-up samples with a load reading: the reference
    down_load_idx = np.where(down & ~np.isnan(lo))[0]  # grid-down samples with a load reading: the drop and the body
    for s, e in runs:
        hi = int(np.searchsorted(up_idx, s))
        lo_ = max(int(np.searchsorted(up_idx, s - REF_MINUTES)), hi - REF_SAMPLES)
        pre = lo[up_idx[lo_:hi]]
        if len(pre) < 2:
            continue  # no reference before the loss: the drop cannot be judged
        ref = float(np.median(pre))
        if ref <= 0:
            continue
        p = int(np.searchsorted(known_idx, s)) - 1
        interval = s - int(known_idx[p]) if p >= 0 else 1  # the source's sample interval at the loss
        body = down_load_idx[(down_load_idx >= s) & (down_load_idx <= e)]
        early = body[body <= s + max(2, interval)][:3]
        if not len(early):
            continue
        if float(np.min(lo[early])) < 0.6 * ref and float(np.median(lo[body])) < 0.6 * ref:
            cen[s:e + 1] = True
    return cen


#: the critical load learnt from outages (plan Part 4.1 and 5.2): the CRIT_QUANTILE of the load seen while the battery alone carried the site through an
#: outage (the genset off, the state of charge above the cut-off plus CRIT_SOC_MARGIN), per local hour; an hour is learnt from at least
#: CRIT_MIN_MINUTES of such load on at least CRIT_MIN_DAYS days, and every other hour keeps the entered profile (`critical_load_kw`)
CRIT_QUANTILE = 0.75
CRIT_SOC_MARGIN = 0.05
CRIT_MIN_MINUTES = 30
CRIT_MIN_DAYS = 2


def critical_profile(site: dict, g: dict) -> tuple[np.ndarray, int]:
    """(the critical load by local hour (kW, 24 values), the hours of it learnt from outages): the entered profile where an hour has too few adequately
    backed outage minutes. A minute is adequately backed when the grid was down, the load was read, the battery's state of charge was known and above
    its cut-off plus CRIT_SOC_MARGIN, the genset was not running (unknown counts as running at a site with a genset: a doubtful minute teaches
    nothing), nothing went unserved and the source did not flag the minute censored. The load then is what the battery carried: the critical
    circuits. A site without a battery has no such outage, and an off-grid site has no outages at all (its grid always reads 0), so both keep the
    entered profile. Minutes are counted in the source's own interval, as the stage rules are."""
    prof = np.asarray(twin.critical_share(site), dtype=float).copy()
    assets = site.get("assets") or {}
    bat = assets.get("battery")
    conns = site.get("connections") or []
    if not bat or not conns or conns[0]["data"].get("regime") == "off_grid" or not len(g.get("ts", [])):
        return prof, 0
    n = len(g["ts"])
    lo = np.asarray(g["load"], dtype=float)
    nan = np.full(n, np.nan)
    soc = np.asarray(g.get("soc", nan), dtype=float)
    gen_on = np.asarray(g.get("gen_on", nan), dtype=float)
    gen_off = (gen_on == 0) | (np.isnan(gen_on) & (not assets.get("genset")))
    ok = ((np.asarray(g["grid"], dtype=float) == 0) & ~np.isnan(lo) & gen_off & (soc > float(bat.get("soc_min", 0.10)) + CRIT_SOC_MARGIN)
          & ~(np.nan_to_num(np.asarray(g.get("unserved", nan), dtype=float)) > 0) & ~(np.asarray(g.get("censored", nan), dtype=float) == 1))
    if not ok.any():
        return prof, 0
    iv = sample_minutes(~np.isnan(lo))
    loc = np.asarray(g["ts"], dtype=np.int64) + 5 * 3600
    hour, day = (loc % 86400) // 3600, loc // 86400
    learnt = 0
    for h in range(24):
        m = ok & (hour == h)
        if m.sum() * iv >= CRIT_MIN_MINUTES and len(np.unique(day[m])) >= CRIT_MIN_DAYS:
            prof[h] = float(np.quantile(lo[m], CRIT_QUANTILE))
            learnt += 1
    return prof, learnt


def load(site: dict, t0: int, steps: int = STEPS) -> dict:
    """The load quantiles per step and the critical load (`crit`, learnt from outages where it can be: critical_profile), with `crit_learned_hours`,
    the hours of the day learnt, and `crit_zero`, set when the critical load is zero in every hour (nothing entered or learnt: the reserve rule then
    holds nothing for an outage, and the pages say so)."""
    t = step_times(t0, steps)
    step_type = _day_type(t, site)
    tmpl = _template(site, t, step_type)
    cal = _calendar(t, step_type)
    h0 = t0 - 21 * 86400
    g = telemetry.minute_grid(site["id"], h0, t0)
    lo = np.where(censored_minutes(site, g), np.nan, g["load"])  # censored samples leave the persistence medians and the ratio (plan Part 5.2)
    # days of uncensored data, not of 1-minute rows: each sample stands for the source's own interval, so a 5- or 15-minute source with daily shedding
    # reaches stage 1 as a 1-minute one does; a slot with no uncensored history still falls to the template below
    iv = sample_minutes(~np.isnan(g["load"]))
    have_days = int(np.sum(~np.isnan(lo)) * iv // 1440)
    hours = ((t + 5 * 3600) % 86400) // 3600
    prof, learnt = critical_profile(site, g)
    crit = prof[hours]
    crit_info = {"crit_learned_hours": int(learnt), "crit_zero": bool(np.all(prof <= 0)), "calendar": cal}
    if have_days < 3:
        q = {name: tmpl * (1 + 0.25 * (v - 0.5) / 0.45) for name, v in zip(QS, QV)}
        q["q50"] = tmpl
        return {"stage": 0, "q": q, "crit": np.minimum(crit, q["q75"]), "days": have_days, **crit_info}
    # equivalent-day persistence: per target step, the same slot on the last same-day-type days (up to 7); a type with fewer than 2 such days reads
    # the next type of its fallback (DAY_FALLBACK: Eid and a holiday from the off days, a Ramadan or half day from the working days), then the template
    lo15 = np.nanmean(lo[: len(lo) // 15 * 15].reshape(-1, 15), axis=1)
    hist_t = h0 + STEP_S * np.arange(len(lo15))
    hist_type = _day_type(hist_t, site)
    slot_of = ((hist_t + 5 * 3600) % 86400) // STEP_S
    known15 = ~np.isnan(lo15)

    def same_slot(s: int, dt_: int, need: int, before: int | None = None) -> np.ndarray:
        """The last 7 values of slot s on days of type dt_ (or of its fallback types, the first with at least `need`), from history steps starting
        before `before` when given; empty when none has."""
        m0 = known15 if before is None else known15 & (hist_t < before)
        for k in (dt_, *DAY_FALLBACK.get(dt_, ())):
            v = lo15[(slot_of == s) & (hist_type == k) & m0][-7:]
            if len(v) >= need:
                return v
        return lo15[:0]

    qarr = np.zeros((len(QS), steps))
    for i, tt in enumerate(t):
        vals = same_slot(((tt + 5 * 3600) % 86400) // STEP_S, int(step_type[i]), 2)
        if len(vals) < 2:
            vals = np.array([tmpl[i]])
        med = float(np.median(vals))
        spread = max(float(np.std(vals)), 0.06 * med)
        qarr[:, i] = med + spread * np.array([-1.645, -1.2816, -0.6745, 0, 0.6745, 1.2816, 1.645])
    qarr = np.maximum(qarr, 0)
    # scale by the last two hours' ratio with a decaying weight: those two hours against the same slots on earlier days only (the history before them),
    # so today's own values are not in their reference
    recent = lo[-120:]
    if np.sum(~np.isnan(recent)) * iv > 60:
        rt = t0 - 7200 + 60 * np.arange(120)
        rt_type = _day_type(rt, site)
        ref = []
        for x, k in zip(rt, rt_type):
            v = same_slot(((x + 5 * 3600) % 86400) // STEP_S, int(k), 1, before=t0 - 7200)
            ref.append(np.median(v) if len(v) else np.nan)
        ref = np.array(ref)
        ok = ~np.isnan(recent) & ~np.isnan(ref) & (ref > 0)
        if ok.sum() * iv > 30:
            ratio = float(np.clip(np.sum(recent[ok]) / np.sum(ref[ok]), 0.6, 1.5))
            w = np.exp(-np.arange(steps) / 8.0)
            qarr = qarr * (1 + (ratio - 1) * w)
    q = {name: qarr[j] for j, name in enumerate(QS)}
    return {"stage": 1, "q": q, "crit": np.minimum(crit, q["q75"]), "days": have_days, **crit_info}


# ------------------------------------------------------------------------------------------------ outages

def schedule(site_id: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM outage_schedules WHERE site_id = ? ORDER BY fetched_at DESC LIMIT 1", (site_id,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "fetched_at": r["fetched_at"], "source": r["source"], "windows": json.loads(r["slots"])}


def set_schedule(site_id: str, windows: list, source: str, by: str) -> dict:
    """A new version of the feeder's announced schedule: windows as [["HH:MM", "HH:MM", "weekdays|daily"], ...] (untrusted text parsed here)."""
    clean = []
    for w in windows[:12]:
        a, b = str(w[0]), str(w[1])
        for x in (a, b):
            if len(x) != 5 or x[2] != ":" or not (0 <= int(x[:2]) <= 23) or not (0 <= int(x[3:]) <= 59):
                raise ValueError("Each window needs a start and an end as HH:MM")
        clean.append([a, b, (w[2] if len(w) > 2 and w[2] in ("daily", "weekdays") else "weekdays")])
    import hashlib
    h = hashlib.sha256(json.dumps(clean).encode()).hexdigest()[:16]
    with store.connect() as con:
        con.execute("INSERT INTO outage_schedules (id, site_id, fetched_at, source, slots, hash) VALUES (?,?,?,?,?,?)",
                    (store.new_id(), site_id, store._now(), source, json.dumps(clean), h))
    store.audit(by, "set the announced load-shedding schedule", site_id, {"windows": clean})
    return schedule(site_id)


def _window_mask(windows: list, ts: np.ndarray) -> np.ndarray:
    loc = (np.asarray(ts) + 5 * 3600)
    minute = (loc % 86400) // 60
    dow = (loc // 86400 + 3) % 7
    m = np.zeros(len(ts), dtype=bool)
    for a, b, *rest in windows:
        days = rest[0] if rest else "weekdays"
        s, e = int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:])
        inside = (minute >= s) & (minute < e) if s <= e else (minute >= s) | (minute < e)
        if days == "weekdays":
            inside &= dow != 6
        m |= inside
    return m


#: the schedule adherence's prior, Beta(8, 2) (plan Part 5.3): 8 kept windows in 10
ADHERENCE_PRIOR = (8.0, 10.0)
#: a window occurrence is a trial only when its known grid samples cover at least this share of its minutes (a logger that was off says nothing)
ADHERENCE_COVERAGE = 0.5


def _adherence_trials(windows: list, grid: np.ndarray, ts: np.ndarray) -> tuple[float, int]:
    """(kept, trials) over the announced windows' occurrences in a minute grid: each occurrence (one window on one local day, a window across midnight
    with the day it starts on) seen whole in the grid is one Bernoulli trial (plan Part 5.3, FOR-08 "adherence updates per observed slot"), kept when
    the grid was down for at least half of its known samples. An occurrence cut by either end of the grid (the history's start, or a window still
    running at the tick) and one whose known samples cover less than ADHERENCE_COVERAGE of it are not trials, so the posterior does not depend on the
    time of day of the tick."""
    if not windows or not len(grid):
        return 0.0, 0
    ts = np.asarray(ts, dtype=np.int64)
    m = _window_mask(windows, np.concatenate([[ts[0] - 60], ts, [ts[-1] + 60]]))  # a minute either side: a run touching an end is cut
    edge = np.diff(m.astype(np.int8))
    starts, ends = np.where(edge == 1)[0], np.where(edge == -1)[0]  # grid indices: first minute in, first minute out
    known = ~np.isnan(grid)
    iv = sample_minutes(known)
    kept, n = 0.0, 0
    for s in starts:
        e = ends[ends > s]
        if not len(e):
            break  # still running at the end of the grid
        e = int(e[0])
        k = known[s:e]
        if k.sum() * iv < ADHERENCE_COVERAGE * (e - s):
            continue
        n += 1
        kept += float(np.mean(grid[s:e][k] == 0) >= 0.5)
    return kept, n


def _adherence(windows: list, grid: np.ndarray, ts: np.ndarray) -> float:
    """The posterior mean share of announced windows the feeder keeps: the Beta(8, 2) prior updated by each whole window occurrence in the grid."""
    kept, n = _adherence_trials(windows, grid, ts)
    return (ADHERENCE_PRIOR[0] + kept) / (ADHERENCE_PRIOR[1] + n)


#: the outage model pools the history of the company's other sites on the same feeder or transformer (plan Part 3 `sites.outage_unit`: "the key the
#: outage model pools on"), at most this many of them; a unit without a code is not pooled (a name alone may be a placeholder)
POOL_MAX_SITES = 20


def _pool_key(site: dict) -> tuple | None:
    data = site.get("data") or {}
    unit = data.get("outage_unit") or {}
    code = str(unit.get("code") or "").strip().casefold()
    return (str(data.get("disco") or ""), str(unit.get("kind") or "feeder"), code) if code else None


def _feeder_peers(site: dict) -> list[str]:
    """The ids of the other sites of the company on the site's feeder or transformer (same DISCO, kind and code), at most POOL_MAX_SITES."""
    key = _pool_key(site)
    if key is None:
        return []
    from . import sites as sites_mod
    return sorted(r["id"] for r in sites_mod.listing() if r["id"] != site["id"] and _pool_key(r) == key)[:POOL_MAX_SITES]


def _outage_events(grid: np.ndarray, ts: np.ndarray, windows: list) -> dict:
    """The unscheduled outages of a minute grid for the hazard: `counts`, starts by local hour, and `durs`, their lengths in minutes. An outage is a run
    of _outage_runs (the censoring's runs: unknown minutes up to OUTAGE_GAP_MINUTES and grid-up flickers of up to FLICKER_MINUTES samples inside an
    outage continue it), so a lost row is not a restoration and a 5- or 15-minute source's outage is one event; its length runs to its last grid-down
    sample plus the source's interval. A run starting at the grid's first known sample began before it (not a start); one still running at the end
    (its last sample within OUTAGE_GAP_MINUTES of it) is a start whose length is not known yet. A run starting inside an announced window is
    scheduled and left out."""
    known = ~np.isnan(grid)
    counts = np.zeros(24)
    durs: list[int] = []
    kidx = np.flatnonzero(known)
    if not len(kidx):
        return {"counts": counts, "durs": durs}
    iv = sample_minutes(known)
    in_win = _window_mask(windows, ts) if windows else np.zeros(len(grid), dtype=bool)
    for s, e in _outage_runs(grid):
        if s == kidx[0] or in_win[s]:
            continue
        counts[int(((int(ts[s]) + 5 * 3600) % 86400) // 3600)] += 1
        if not (e == kidx[-1] and len(grid) - 1 - e <= OUTAGE_GAP_MINUTES):
            durs.append(int(e - s + iv))
    return {"counts": counts, "durs": durs}


def outage(site: dict, t0: int, steps: int = STEPS, now: int | None = None) -> dict:
    """P(down) per step from t0. The hazard and adherence read history before t0 only; the observed grid state reads the telemetry up to `now`
    (the actual issue time, when later than t0), so a grid loss inside the current quarter is seen by a re-solve or refresh at once."""
    t = step_times(t0, steps)
    conns = site.get("connections") or []
    if not conns or conns[0]["data"].get("regime") == "off_grid":
        one = np.ones(steps)
        return {"stage": 0, "p_down": one, "p_start": np.zeros(steps), "dur_steps": np.full(steps, 4), "g": np.zeros(steps), "observed": None,
                "adherence": None, "adherence_windows": None, "off_grid": True, "shed_prior": None}
    sched = schedule(site["id"])
    windows = sched["windows"] if sched else []
    h0 = t0 - 28 * 86400
    g = telemetry.minute_grid(site["id"], h0, t0)
    grid = g["grid"]
    known = ~np.isnan(grid)
    iv = sample_minutes(known)
    days_own = known.sum() * iv / 1440  # days of grid readings, not of 1-minute rows (the hazard divides by it)
    ev = _outage_events(grid, g["ts"], windows)
    counts, durs = ev["counts"].copy(), list(ev["durs"])
    kept, trials = _adherence_trials(windows, grid, g["ts"])
    # the other sites on the feeder are sensors of the same outages (plan Part 5.3): their history over the same 28 days adds to the counts, the
    # lengths, the adherence trials and the days the hazard divides by, so the rate is per site-day and a new site starts from its feeder
    pool_sites, pool_days = 0, 0.0
    for pid in _feeder_peers(site):
        pg = telemetry.minute_grid(pid, h0, t0)["grid"]
        pk = ~np.isnan(pg)
        if not pk.any():
            continue
        pev = _outage_events(pg, g["ts"], windows)
        counts += pev["counts"]
        durs += pev["durs"]
        pk_kept, pk_trials = _adherence_trials(windows, pg, g["ts"])
        kept, trials = kept + pk_kept, trials + pk_trials
        pool_sites += 1
        pool_days += pk.sum() * sample_minutes(pk) / 1440
    days_known = days_own + pool_days
    med_dur = float(np.median(durs)) if len(durs) >= 3 else 75.0
    adherence = (ADHERENCE_PRIOR[0] + kept) / (ADHERENCE_PRIOR[1] + trials)
    # unscheduled start hazard by local hour (starts per hour), shrunk to the DISCO prior with 7 days' weight
    disco = site["data"].get("disco") or "LESCO"
    prior_day = DISCO_TRIPS.get(disco, 0.6)
    prior_h = np.ones(24)
    prior_h[17:23] = 2.5
    prior_h = prior_h / prior_h.sum() * prior_day
    lam_h = (counts + prior_h * 7) / (max(days_known, 0) + 7)
    hours = ((t + 5 * 3600) % 86400) // 3600
    lam = lam_h[hours]  # starts per hour at each step
    dur_steps_u = max(1, int(round(med_dur / 15)))
    p_start_u = 1 - np.exp(-lam * 0.25)
    # probability of being down from an unscheduled start in the preceding dur steps
    cum = np.convolve(lam * 0.25, np.ones(dur_steps_u))[:steps]
    p_down_u = 1 - np.exp(-cum)
    sched_mask = _window_mask(windows, t + 450) if windows else np.zeros(steps, dtype=bool)
    p_down_s = sched_mask * adherence
    sched_start = np.zeros(steps)
    dur_s = np.full(steps, dur_steps_u)
    if windows:
        nxt = np.concatenate([sched_mask[1:], [False]])
        starting = nxt & ~sched_mask  # an announced window starts in step t+1
        sched_start = starting * adherence
        for i in np.where(starting)[0]:
            j = i + 1
            while j < steps and sched_mask[j]:
                j += 1
            dur_s[i] = j - i - 1 if j - i - 1 > 0 else dur_steps_u
    # the cold start of a rural feeder without an announced schedule: the shedding its DISCO's trip prior leaves out, evenly over the day in blocks
    p_down_c, p_start_c, shed_prior = np.zeros(steps), np.zeros(steps), None
    down_h = COLD_START_DOWN_HOURS.get(disco)
    if down_h and not windows:
        trips = prior_day * 75.0 / 1440  # the trip prior's share of the day down (its starts times the default 75-minute length)
        w = 7 / (max(days_known, 0) + 7)
        p_down_c = np.full(steps, max(0.0, 1 - (1 - down_h / 24) / (1 - trips)) * w)
        p_start_c = p_down_c / SHED_BLOCK_STEPS
        shed_prior = {"down_hours_day": float(down_h), "weight": round(w, 3)}
    p_down = 1 - (1 - p_down_s) * (1 - p_down_u) * (1 - p_down_c)
    p_start = 1 - (1 - sched_start) * (1 - np.concatenate([p_start_u[1:], [p_start_u[-1]]])) * (1 - p_start_c)
    dur = np.where(sched_start > p_start_u, dur_s, np.where(p_start_c > p_start_u, SHED_BLOCK_STEPS, dur_steps_u)).astype(int)
    gvec = np.where(sched_mask & (adherence > 0.9), 0.0, 1.0)
    # an observed outage now: down for its expected remaining duration (to the window's end when inside one). The grid state is read up to the
    # actual issue time; the last known minute may lag the end by OBSERVED_LAG_MIN minutes (a batch still in flight)
    obs_grid, obs_ts = grid, g["ts"]
    if now is not None and int(now) // 60 * 60 > t0:
        g_now = telemetry.minute_grid(site["id"], t0, int(now))
        obs_grid, obs_ts = np.concatenate([grid, g_now["grid"]]), np.concatenate([g["ts"], g_now["ts"]])
    obs_known = np.where(~np.isnan(obs_grid))[0]
    observed = None
    if len(obs_known) and len(obs_grid) - 1 - obs_known[-1] <= OBSERVED_LAG_MIN and obs_grid[obs_known[-1]] == 0:
        k = int(_outage_runs(obs_grid)[-1][0])  # the outage's first grid-down sample, across lost rows and flickers as in the history
        elapsed = len(obs_grid) - k
        cur = (len(obs_grid) - len(grid)) // 15  # whole steps of the horizon already passed at the issue time
        if sched_mask[min(cur, steps - 1)]:
            j = cur
            while j < steps and sched_mask[j]:
                j += 1
            remaining = max(1, j - cur)
        else:
            remaining = max(1, int(np.ceil(max(15.0, med_dur * 1.2 - elapsed) / 15)))
        gvec[:cur + remaining] = 0.0
        p_down[:cur + remaining] = 1.0
        observed = {"since": int(obs_ts[k]), "elapsed_min": int(elapsed), "expected_remaining_min": int(remaining * 15)}
    return {"stage": 1 if days_known >= 7 else 0, "p_down": np.clip(p_down, 0, 1), "p_start": np.clip(p_start, 0, 1), "dur_steps": dur, "g": gvec,
            "observed": observed,
            # no announced windows: no adherence to show (the prior has nothing to apply to); windows entered but none seen whole: the prior, 0 trials
            "adherence": round(adherence, 3) if windows else None, "adherence_windows": trials if windows else None, "median_minutes": med_dur,
            "windows": windows, "off_grid": False,
            "lam_day": round(float(lam_h.sum()), 3), "days_known": round(float(days_own), 1),
            "pooled": {"sites": pool_sites, "days": round(float(pool_days), 1)}, "shed_prior": shed_prior}


# ------------------------------------------------------------------------------------------------ the bundle

def _r(a, n=3):
    return [round(float(x), n) for x in a]


def issue(site: dict, t0: int, save: bool = True, now: int | None = None) -> dict:
    """One forecast bundle for the optimiser; saved per kind for the forecast page and scoring. The belief time is t0 taken down to the quarter;
    the observed grid state reads telemetry up to `now`, or up to t0 itself when t0 is the actual (unaligned) issue time."""
    now = int(t0) if now is None else int(now)
    t0 = align(t0)
    p, l, o = pv(site, t0), load(site, t0), outage(site, t0, now=now)
    if save:
        telemetry.save_forecast(site["id"], "pv", t0, p["stage"], VERSION, {"cs": _r(p["cs"]), **{k: _r(v) for k, v in p["q"].items()}, "pr": p.get("pr")})
        telemetry.save_forecast(site["id"], "load", t0, l["stage"], VERSION, {**{k: _r(v) for k, v in l["q"].items()}, "crit": _r(l["crit"]),
                                                                           "crit_learned_hours": l.get("crit_learned_hours"), "crit_zero": l.get("crit_zero"),
                                                                           "calendar": l.get("calendar")})
        telemetry.save_forecast(site["id"], "outage", t0, o["stage"], VERSION,
                                {"p_down": _r(o["p_down"]), "p_start": _r(o["p_start"]), "dur_steps": [int(x) for x in o["dur_steps"]], "g": _r(o["g"], 0),
                                 "observed": o.get("observed"), "adherence": o.get("adherence"), "adherence_windows": o.get("adherence_windows"),
                                 "off_grid": o.get("off_grid"), "days_known": o.get("days_known"), "pooled": o.get("pooled"),
                                 "shed_prior": o.get("shed_prior")})
    return {"t0": t0, "pv": p, "load": l, "outage": o}


#: the arrays of an issue the forecast page draws over the 24 hours before the latest issue, per kind
PAST_KEYS = {"pv": (*QS, "cs"), "load": (*QS, "crit"), "outage": ("p_down", "p_start")}
PAST_STEPS = 96


def _actual15(site_id: str, kind: str, a: int, b: int) -> list:
    """The page's 15-minute actuals over [a, b): available PV, load, or 1 where the grid was down in the step (None where a step has no reading)."""
    g = telemetry.minute_grid(site_id, a, b)
    n = len(g["ts"]) // 15
    if not n:
        return []
    if kind == "outage":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            v = 1 - np.nanmin(g["grid"][: n * 15].reshape(-1, 15), axis=1)
    else:
        v = _q15(available_pv(g) if kind == "pv" else g["load"])
    return [None if np.isnan(x) else round(float(x), 2) for x in v]


def _past(site_id: str, kind: str, t0: int) -> dict:
    """The 24 hours before a belief time: their 15-minute actuals and, over them, the issue held a day before (the latest at or before the window's
    start, from the step that falls on it; None past its horizon). `belief_time` is None where no such issue exists (a site younger than a day)."""
    h0 = t0 - PAST_STEPS * STEP_S
    out = {"t": [int(x) for x in step_times(h0, PAST_STEPS)], "actual": _actual15(site_id, kind, h0, t0), "belief_time": None}
    old = telemetry.latest_forecast(site_id, kind, h0)
    if old is None:
        return out
    off = (h0 - int(old["belief_time"])) // STEP_S
    arrays = {k: old[k] for k in PAST_KEYS.get(kind, ()) if isinstance(old.get(k), list)}
    if off < STEPS and arrays:
        out["belief_time"] = int(old["belief_time"])
        for k, v in arrays.items():
            seg = list(v[off:off + PAST_STEPS])
            out[k] = seg + [None] * (PAST_STEPS - len(seg))
    return out


def page(site: dict, kind: str) -> dict:
    """The latest issue of a kind with the actuals so far (15-minute means) for the forecast page, and `past`: the 24 hours before its belief time with
    their actuals and the issue held a day before over them, so the chart shows how the forecast tracked. The tick re-issues every 15 minutes, so the
    latest issue's own actuals are at most one step."""
    f = telemetry.latest_forecast(site["id"], kind)
    if f is None:
        return {"kind": kind, "issue": None}
    t0 = f["belief_time"]
    t = step_times(t0)
    now = int(datetime.now(timezone.utc).timestamp())
    t1 = max(t0, min(now // 60 * 60, t0 + STEPS * STEP_S))  # whole minutes only
    actual = _actual15(site["id"], kind, t0, t1) if kind in PAST_KEYS else None
    return {"kind": kind, "belief_time": t0, "stage": f["stage"], "model_version": f["model_version"], "t": [int(x) for x in t],
            "issue": {k: v for k, v in f.items() if k not in ("belief_time", "stage", "model_version")}, "actual": actual,
            "preliminary": f["stage"] == 0, "past": _past(site["id"], kind, t0) if kind in PAST_KEYS else None}


def _outage_baselines(site: dict, t0: int) -> dict:
    """The outage Brier baselines of plan Part 5.5 for the 96 slots of the day from t0, from data before the day only: schedule-only (the announced
    windows times the adherence; never down without a schedule) and a 28-day climatology of the share of slots down per local hour."""
    t = step_times(t0, 96)
    h = telemetry.minute_grid(site["id"], t0 - 28 * 86400, t0)
    conns = site.get("connections") or []
    sched = schedule(site["id"])
    windows = sched["windows"] if sched else []
    if not conns or conns[0]["data"].get("regime") == "off_grid":
        p_sched = np.ones(96)
    elif windows:
        p_sched = _window_mask(windows, t + 450) * _adherence(windows, h["grid"], h["ts"])
    else:
        p_sched = np.zeros(96)
    hg = h["grid"].reshape(-1, 15)
    hk = ~np.all(np.isnan(hg), axis=1)
    hd = hk & np.any(hg == 0, axis=1)  # a known slot is down when any minute in it was
    hh = ((h["ts"][::15] + 5 * 3600) % 86400) // 3600
    p_clim = None
    if hk.any():
        overall = float(hd.sum() / hk.sum())
        by_h = np.array([float(hd[hh == x].sum() / hk[hh == x].sum()) if hk[hh == x].any() else overall for x in range(24)])
        p_clim = by_h[((t + 5 * 3600) % 86400) // 3600]
    return {"schedule": p_sched, "climatology": p_clim}


#: the horizon classes of plan Part 5.5 (hours ahead of the belief time, from inclusive, to exclusive). The day-ahead row (the issue at local midnight
#: over the whole day) stays the headline that the page's tiles and the forecast-bust alert read; each class is scored over every stored issue whose
#: steps fall on the day at that horizon
HORIZON_CLASSES = (("0-2h", 0, 2), ("2-6h", 2, 6), ("6-24h", 6, 24), ("24-48h", 24, 48))
#: the baseline each kind's skill is measured against (plan Part 5.5): PV smart persistence, load equivalent-day persistence, outage schedule-only
BASELINES = {"pv": "smart_persistence", "load": "equivalent_day", "outage": "schedule_only"}
#: the fewest scored 15-minute slots (or issue-step pairs) a score row needs
MIN_SLOTS = 8


def _q15(a) -> np.ndarray:
    """15-minute means of a minute series from its first minute (NaN where a step has no known minute)."""
    a = np.asarray(a, dtype=float)
    n = len(a) // 15
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # a step with no known minute is NaN, as meant
        return np.nanmean(a[: n * 15].reshape(-1, 15), axis=1)


#: the hours read before a scored day so the censoring sees an outage that began before local midnight with its reference load (REF_MINUTES before the
#: loss): the twin's longest outage is 6 hours
SCORE_LEAD_S = 8 * 3600


def _unshed15(site: dict, d0: int) -> np.ndarray:
    """The 96 15-minute means of a day's load from d0 over its unshed minutes (plan Part 5.2: censored samples leave the scores and the baselines, as
    they leave the training set): NaN where a step has no uncensored minute. The censoring reads SCORE_LEAD_S before the day."""
    g = telemetry.minute_grid(site["id"], d0 - SCORE_LEAD_S, d0 + 86400)
    lo = np.where(censored_minutes(site, g), np.nan, g["load"])
    return _q15(lo[SCORE_LEAD_S // 60:])


def _nrmse(pred: np.ndarray, act: np.ndarray, denom: float) -> float:
    return float(np.sqrt(np.mean((pred - act) ** 2)) / denom)


def _issues(site_id: str, kind: str, b0: int, b1: int, key: str) -> list[tuple[int, np.ndarray]]:
    """The stored issues of a kind with a belief time in (b0, b1), oldest first, as (belief time, the issue's array `key`). Quarter issues older than
    3 days are pruned (telemetry.save_forecast) and hourly ones kept, so the nightly job sees every issue of the day before."""
    with telemetry.connect() as con:
        rows = con.execute("SELECT belief_time, body FROM forecast_issues WHERE site_id = ? AND kind = ? AND belief_time > ? AND belief_time < ? "
                           "ORDER BY belief_time", (site_id, kind, int(b0), int(b1))).fetchall()
    out = []
    for r in rows:
        v = json.loads(r["body"]).get(key)
        if isinstance(v, list):
            out.append((int(r["belief_time"]), np.array([np.nan if x is None else float(x) for x in v], dtype=float)))
    return out


def _power_scores(site: dict, kind: str, t0: int, g: dict) -> list[dict]:
    """The nRMSE rows of a PV or load day (plan Part 5.5): the day-ahead headline and one row per horizon class, each with its baseline.

    PV: daytime slots (solar elevation above 5 degrees) of available PV, by AC nameplate. The baseline is smart persistence, the clear sky at the step
    times a clearness index known at the issue time: the last hour's before the belief time for 0-2 hours (reported only: the stage-1 blend is that
    model), the same step yesterday for the headline, 2-6 and 6-24 hours, and the same step two days before for 24-48 hours (yesterday's is not yet
    known that far ahead). The model and its baseline are scored over the same slots, so the skill compares like with like.
    Load: over the unshed minutes of the day (censored minutes are the shed demand's lower bound, not the demand the forecast describes), by their mean
    load, against equivalent-day persistence (the same slot a week before, its own censored minutes left out), the model over the baseline's slots."""
    sid = site["id"]
    if kind == "pv":
        pv_asset = site["assets"]["pv"]
        lat, lon = site["data"].get("lat", 31.5), site["data"].get("lon", 74.3)
        kw_ac = max(1e-6, float(pv_asset["kw_ac"]))
        h = telemetry.minute_grid(sid, t0 - 2 * 86400, t0 + 86400)  # the two days before, for persistence, and the day
        av = _q15(available_pv(h))
        cs = _q15(solar.pv_power(h["ts"], lat, lon, pv_asset, 1.0, solar.local_month(h["ts"])))
        act = av[192:]
        zen, _ = solar.sun_position(step_times(t0, 96) + STEP_S // 2, lat, lon)
        ok = ~np.isnan(act) & (zen < 85.0)
        denom = kw_ac

        def same_step(days_before: int) -> np.ndarray:
            ap, cp = av[192 - 96 * days_before:288 - 96 * days_before], cs[192 - 96 * days_before:288 - 96 * days_before]
            return np.where(cp > 0.02 * kw_ac, cs[192:] * ap / np.maximum(cp, 1e-9), ap)  # near sunrise and sunset: plain persistence

        sp1, sp2 = same_step(1), same_step(2)

        def baseline(cls: str | None, b: int) -> np.ndarray | None:
            if cls == "0-2h":
                i = (b - (t0 - 2 * 86400)) // STEP_S
                a, c = av[max(0, i - 4):i], cs[max(0, i - 4):i]
                m = ~np.isnan(a) & (c > 0.1 * kw_ac)
                if m.sum() < 2:
                    return None  # no daylight in the hour before the issue: no clearness to persist
                return np.minimum(cs[192:] * float(np.sum(a[m]) / np.sum(c[m])), kw_ac)
            return sp2 if cls == "24-48h" else sp1
    else:
        act = _unshed15(site, t0)
        ok = ~np.isnan(act)
        if not ok.any():
            return []
        denom = max(1e-6, float(np.mean(act[ok])))
        pers = _unshed15(site, t0 - 7 * 86400)

        def baseline(cls: str | None, b: int) -> np.ndarray | None:
            return pers
    if ok.sum() < MIN_SLOTS:
        return []
    rows = []
    f = telemetry.latest_forecast(sid, kind, t0)
    if f is not None and t0 - f["belief_time"] <= 6 * 3600:
        off = (t0 - f["belief_time"]) // STEP_S
        pred = np.array(f["q50"][off:off + 96], dtype=float)
        row = {"kind": kind, "metric": "nrmse", "value": round(_nrmse(pred[ok], act[ok], denom), 4), "horizon_class": "day_ahead"}
        base = baseline(None, f["belief_time"])
        okp = ok & ~np.isnan(base)
        if okp.sum() >= MIN_SLOTS:
            # the model and its baseline over the same slots, so the skill compares like with like
            row["value"] = round(_nrmse(pred[okp], act[okp], denom), 4)
            row["baseline_value"] = round(_nrmse(base[okp], act[okp], denom), 4)
        rows.append(row)
    issues = _issues(sid, kind, t0 - 48 * 3600, t0 + 86400, "q50")
    for cls, a, b in HORIZON_CLASSES:
        P, A, B = [], [], []
        for bt, q in issues:
            k = np.arange(len(q))
            tgt = bt + STEP_S * k
            sel = (tgt >= t0) & (tgt < t0 + 86400) & (k >= 4 * a) & (k < 4 * b)
            if not sel.any():
                continue
            base = baseline(cls, bt)
            if base is None:
                continue
            ks = k[sel]
            js = (tgt[sel] - t0) // STEP_S
            m = ok[js] & ~np.isnan(base[js]) & ~np.isnan(q[ks])
            P.append(q[ks][m])
            A.append(act[js][m])
            B.append(base[js][m])
        n = sum(len(x) for x in P)
        if n < MIN_SLOTS:
            continue
        P, A, B = np.concatenate(P), np.concatenate(A), np.concatenate(B)
        rows.append({"kind": kind, "metric": "nrmse", "value": round(_nrmse(P, A, denom), 4), "horizon_class": cls,
                     "baseline_value": round(_nrmse(B, A, denom), 4), "pairs": int(n)})
    return rows


def score_day(site: dict, day) -> list[dict]:
    """Yesterday's scores (plan Part 5.5): for PV and load the day-ahead issue's (the first issue at or before local midnight) nRMSE and one row per
    horizon class, each against its baseline (_power_scores); the outage Brier over the slots with a known grid state against the schedule-only
    baseline (the pre-registered gate; the climatology Brier is returned beside it). A kind's day-ahead row comes first: alerts.nightly reads the
    first PV row as the day's headline."""
    t0 = twin.day_start_utc(day)
    g = telemetry.minute_grid(site["id"], t0, t0 + 86400)
    if g["completeness"] < 0.5:
        return []
    out = []
    pv_asset = (site.get("assets") or {}).get("pv")
    for kind in ("pv", "load", "outage"):
        if kind == "pv" and not pv_asset:
            continue
        if kind in ("pv", "load"):
            out += _power_scores(site, kind, t0, g)
            continue
        f = telemetry.latest_forecast(site["id"], kind, t0)
        if f is None or t0 - f["belief_time"] > 6 * 3600:
            continue
        off = (t0 - f["belief_time"]) // STEP_S
        sl = slice(off, off + 96)
        G = g["grid"].reshape(96, 15)
        known = ~np.all(np.isnan(G), axis=1)  # a slot without data is not scored (a dark logger is often the outage itself)
        if known.sum() < MIN_SLOTS:
            continue
        act = (np.nanmin(G[known], axis=1) == 0).astype(float)
        pred = np.array(f["p_down"][sl])[known]
        base = _outage_baselines(site, t0)
        row = {"kind": kind, "metric": "brier", "value": round(float(np.mean((pred - act) ** 2)), 4), "horizon_class": "day_ahead",
               "baseline_value": round(float(np.mean((base["schedule"][known] - act) ** 2)), 4), "baseline": "schedule_only",
               "slots_scored": int(known.sum())}
        if base["climatology"] is not None:
            row["climatology_value"] = round(float(np.mean((base["climatology"][known] - act) ** 2)), 4)
        out.append(row)
    with store.connect() as con:
        for r in out:
            con.execute("INSERT OR REPLACE INTO forecast_scores (site_id, kind, day_local, horizon_class, metric, value, baseline_value) VALUES (?,?,?,?,?,?,?)",
                        (site["id"], r["kind"], day.isoformat(), r["horizon_class"], r["metric"], r["value"], r.get("baseline_value")))
    return out


def _skill(value: float | None, baseline: float | None) -> float | None:
    """1 - score / baseline score (lower is better for both): positive when the forecast beats its baseline; None without a baseline above zero."""
    if value is None or baseline is None or baseline <= 0:
        return None
    return round(1.0 - float(value) / float(baseline), 3)


def _window(rows: list[dict]) -> dict:
    """A window's mean score, its days, and the skill over the days with a baseline: 1 - the mean score over the mean baseline of those days (the
    Brier skill score for outages; for nRMSE the same ratio of the days' means)."""
    paired = [r for r in rows if r["baseline"] is not None]
    mean_b = float(np.mean([r["baseline"] for r in paired])) if paired else None
    return {"value": round(float(np.mean([r["value"] for r in rows])), 4) if rows else None, "n": len(rows),
            "baseline": round(mean_b, 4) if mean_b is not None else None,
            "skill": _skill(float(np.mean([r["value"] for r in paired])), mean_b) if paired else None}


def scores(site_id: str, days: int = 30) -> dict:
    """The scored days of the last `days` calendar days (Pakistan time) and their summary. `days` holds each kind's day-ahead rows (one per day and
    metric) with their skill; `summary` gives per kind the mean over the last 7 and the last `days` calendar days (`last7`, `last30`; `n7` and `n` days
    scored in each), the baseline's means and the skill against it, and the same per horizon class (`horizons`, plan Part 5.5's classes in order)."""
    today = datetime.now(PKT).date()
    since = (today - timedelta(days=days)).isoformat()
    since7 = (today - timedelta(days=7)).isoformat()
    with store.connect() as con:
        rows = con.execute("SELECT * FROM forecast_scores WHERE site_id = ? AND day_local >= ? ORDER BY day_local", (site_id, since)).fetchall()
    head: dict = {}
    by_class: dict = {}
    for r in rows:
        x = {"day": r["day_local"], "metric": r["metric"], "value": r["value"], "baseline": r["baseline_value"], "horizon_class": r["horizon_class"],
             "skill": _skill(r["value"], r["baseline_value"])}
        if r["horizon_class"] == "day_ahead":
            head.setdefault(r["kind"], []).append(x)
        else:
            by_class.setdefault(r["kind"], {}).setdefault(r["horizon_class"], []).append(x)

    def window(v: list[dict]) -> dict:
        w30, w7 = _window(v), _window([x for x in v if x["day"] >= since7])
        return {"last7": w7["value"], "last30": w30["value"], "n": w30["n"], "n7": w7["n"], "baseline_last7": w7["baseline"],
                "baseline_last30": w30["baseline"], "skill_last7": w7["skill"], "skill_last30": w30["skill"]}

    summary = {}
    for k in [*head, *(x for x in by_class if x not in head)]:
        v = head.get(k, [])
        classes = by_class.get(k, {})
        horizons = [{"horizon_class": c, "from_h": a, "to_h": b, **window(classes[c])} for c, a, b in HORIZON_CLASSES if classes.get(c)]
        metric = v[0]["metric"] if v else next(iter(classes.values()))[0]["metric"]
        summary[k] = {"metric": metric, **window(v), "baseline_kind": BASELINES.get(k), "horizons": horizons}
    return {"days": head, "summary": summary, "from": since, "from7": since7}
