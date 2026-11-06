"""The three forecasters, stages 0 and 1 (plan Part 5): PV per site, load per site, outage probability per feeder.

Every issue covers 192 steps of 15 minutes from its belief time, carries seven quantiles (q05 to q95) and its stage, and reads only telemetry before the
belief time, so a replayed day sees exactly what was known then. Stage 2 models (xgboost) and NWP feeds are later items; until then the PV chain runs on
the clear-sky model with a climatological clearness distribution (stage 0) and a per-site capacity and clearness fit (stage 1), and every screen marks
it Preliminary.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import warnings

import numpy as np

warnings.filterwarnings("ignore", category=RuntimeWarning, message="(Mean of empty slice|All-NaN slice)")

from . import solar, store, telemetry, twin

PKT = timezone(timedelta(hours=5))
STEPS = 192
STEP_S = 900
QS = ("q05", "q10", "q25", "q50", "q75", "q90", "q95")
QV = np.array([0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95])
VERSION = "v1.0"
#: stage-0 prior on the daily clearness index by quantile (scaled by the month's climatology)
K_PRIOR = np.array([0.30, 0.45, 0.70, 0.86, 0.95, 1.00, 1.02])
MONTH_K = {1: .80, 2: .85, 3: .9, 4: .94, 5: .94, 6: .88, 7: .74, 8: .76, 9: .88, 10: .94, 11: .9, 12: .8}
DISCO_TRIPS = {"QESCO": 1.6, "PESCO": 1.4, "SEPCO": 1.5, "HESCO": 1.2, "TESCO": 1.5, "MEPCO": 0.8, "GEPCO": 0.6, "FESCO": 0.7, "HAZECO": 0.8,
               "LESCO": 0.35, "IESCO": 0.3, "K-Electric": 0.5}


def align(ts: float) -> int:
    return int(ts) // STEP_S * STEP_S


def step_times(t0: int, steps: int = STEPS) -> np.ndarray:
    return t0 + STEP_S * np.arange(steps, dtype=np.int64)


def _mean15(f, t: np.ndarray) -> np.ndarray:
    """Mean of a minute-resolution function over each 15-minute step (sampled at 7.5-minute offsets on a 5-minute lattice)."""
    return np.mean([f(t + o) for o in (150, 450, 750)], axis=0)


# ------------------------------------------------------------------------------------------------ PV

def pv(site: dict, t0: int, steps: int = STEPS) -> dict:
    pv_asset = (site.get("assets") or {}).get("pv")
    t = step_times(t0, steps)
    if not pv_asset:
        z = np.zeros(steps)
        return {"stage": 0, "cs": z, "q": {q: z for q in QS}, "pr": None, "note": "No PV on this site"}
    lat, lon = site["data"].get("lat", 31.5), site["data"].get("lon", 74.3)
    months = solar.local_month(t)
    cs = _mean15(lambda x: solar.pv_power(x, lat, lon, pv_asset, 1.0, months), t)
    # history: the last 14 days of measured PV against the clear-sky model, by day
    h0 = t0 - 14 * 86400
    g = telemetry.minute_grid(site["id"], h0, t0)
    days_ok, k_days, pr = 0, [], None
    if g["present"].sum() > 3 * 600:
        csm = solar.pv_power(g["ts"], lat, lon, pv_asset, 1.0, solar.local_month(g["ts"]))
        pvm = g["pv"]
        for d in range(14):
            sl = slice(d * 1440, (d + 1) * 1440)
            ok = ~np.isnan(pvm[sl]) & (csm[sl] > 0.2 * pv_asset["kw_ac"])
            if ok.sum() > 240:
                days_ok += 1
                k_days.append(float(np.sum(pvm[sl][ok]) / max(1e-6, np.sum(csm[sl][ok]))))
    stage = 1 if days_ok >= 3 else 0
    month_k = MONTH_K[int(months[0])]
    if stage == 1:
        k = np.array(k_days)
        pr = float(np.clip(np.quantile(k, 0.9), 0.5, 1.15))  # the performance ratio on the clearest days
        kn = np.clip(k / pr, 0.05, 1.1)
        kq = np.quantile(kn, QV) if len(kn) >= 7 else np.clip(K_PRIOR * month_k / 0.86 * np.median(kn) / max(1e-6, np.median(kn)), 0, 1.1)
        kq = np.maximum.accumulate(kq)
        scale = pr
    else:
        kq = np.clip(K_PRIOR * month_k / 0.86, 0.05, 1.05)
        scale = 1.0
    q = {name: np.minimum(cs, cs * scale * kq[i]) for i, name in enumerate(QS)}
    # smart persistence for the first 2 hours: blend the q50 towards the last hour's observed clearness
    if stage == 1 and g["present"][-60:].sum() > 30:
        last = g["pv"][-60:]
        csl = solar.pv_power(g["ts"][-60:], lat, lon, pv_asset, 1.0, solar.local_month(g["ts"][-60:]))
        ok = ~np.isnan(last) & (csl > 0.1 * pv_asset["kw_ac"])
        if ok.sum() > 20:
            k_now = float(np.clip(np.sum(last[ok]) / np.sum(csl[ok]) / scale, 0.05, 1.1))
            w = np.clip(1 - np.arange(steps) / 8.0, 0, 1)
            for name in QS:
                q[name] = np.minimum(cs, q[name] * (1 - w) + cs * scale * k_now * w * (q[name] / np.maximum(q["q50"], 1e-6)))
    return {"stage": stage, "cs": cs, "q": q, "pr": pr, "days": days_ok}


# ------------------------------------------------------------------------------------------------ load

def _day_type(ts: np.ndarray) -> np.ndarray:
    dow = ((np.asarray(ts) + 5 * 3600) // 86400 + 3) % 7
    return (dow == 6).astype(int)  # 1 = Sunday (off day)


def load(site: dict, t0: int, steps: int = STEPS) -> dict:
    t = step_times(t0, steps)
    tmpl = _mean15(lambda x: twin.load_template(site, x), t)
    h0 = t0 - 21 * 86400
    g = telemetry.minute_grid(site["id"], h0, t0)
    lo = g["load"]
    have_days = int(np.sum(~np.isnan(lo)) // 1440)
    hours = ((t + 5 * 3600) % 86400) // 3600
    crit = twin.critical_share(site)[hours]
    if have_days < 3:
        q = {name: tmpl * (1 + 0.25 * (v - 0.5) / 0.45) for name, v in zip(QS, QV)}
        q["q50"] = tmpl
        return {"stage": 0, "q": q, "crit": np.minimum(crit, q["q75"]), "days": have_days}
    # equivalent-day persistence: per target step, the same slot on the last same-day-type days (up to 7)
    lo15 = np.nanmean(lo[: len(lo) // 15 * 15].reshape(-1, 15), axis=1)
    hist_t = h0 + STEP_S * np.arange(len(lo15))
    hist_type = _day_type(hist_t)
    slot_of = ((hist_t + 5 * 3600) % 86400) // STEP_S
    qarr = np.zeros((len(QS), steps))
    for i, tt in enumerate(t):
        s = ((tt + 5 * 3600) % 86400) // STEP_S
        dt_ = _day_type(np.array([tt]))[0]
        m = (slot_of == s) & (hist_type == dt_) & ~np.isnan(lo15)
        vals = lo15[m][-7:]
        if len(vals) < 2:
            vals = np.array([tmpl[i]])
        med = float(np.median(vals))
        spread = max(float(np.std(vals)), 0.06 * med)
        qarr[:, i] = med + spread * np.array([-1.645, -1.2816, -0.6745, 0, 0.6745, 1.2816, 1.645])
    qarr = np.maximum(qarr, 0)
    # scale by the last two hours' ratio with a decaying weight
    recent = lo[-120:]
    if np.sum(~np.isnan(recent)) > 60:
        rt = t0 - 7200 + 60 * np.arange(120)
        ref = []
        for x in rt:
            s = ((x + 5 * 3600) % 86400) // STEP_S
            m = (slot_of == s) & (hist_type == _day_type(np.array([x]))[0]) & ~np.isnan(lo15)
            ref.append(np.median(lo15[m][-7:]) if m.any() else np.nan)
        ref = np.array(ref)
        ok = ~np.isnan(recent) & ~np.isnan(ref) & (ref > 0)
        if ok.sum() > 30:
            ratio = float(np.clip(np.sum(recent[ok]) / np.sum(ref[ok]), 0.6, 1.5))
            w = np.exp(-np.arange(steps) / 8.0)
            qarr = qarr * (1 + (ratio - 1) * w)
    q = {name: qarr[j] for j, name in enumerate(QS)}
    return {"stage": 1, "q": q, "crit": np.minimum(crit, q["q75"]), "days": have_days}


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


def outage(site: dict, t0: int, steps: int = STEPS) -> dict:
    t = step_times(t0, steps)
    conns = site.get("connections") or []
    if not conns or conns[0]["data"].get("regime") == "off_grid":
        one = np.ones(steps)
        return {"stage": 0, "p_down": one, "p_start": np.zeros(steps), "dur_steps": np.full(steps, 4), "g": np.zeros(steps), "observed": None,
                "adherence": None, "off_grid": True}
    sched = schedule(site["id"])
    windows = sched["windows"] if sched else []
    h0 = t0 - 28 * 86400
    g = telemetry.minute_grid(site["id"], h0, t0)
    grid = g["grid"]
    known = ~np.isnan(grid)
    days_known = known.sum() / 1440
    # events in history
    down = (grid == 0) & known
    starts = np.where(down[1:] & ~down[:-1])[0] + 1
    ends = np.where(~down[1:] & down[:-1])[0] + 1
    in_win = _window_mask(windows, g["ts"]) if windows else np.zeros(len(grid), dtype=bool)
    unsched_starts = [s for s in starts if not in_win[s]]
    durs = []
    for s in unsched_starts:
        e = ends[ends > s]
        if len(e):
            durs.append(int(e[0] - s))
    med_dur = float(np.median(durs)) if len(durs) >= 3 else 75.0
    # adherence: the share of past scheduled windows (in known data) that saw the grid down for at least half of the window
    a_hit, a_n = 8.0, 10.0  # Beta(8, 2)
    if windows and days_known >= 1:
        wm = in_win & known
        day_idx = (np.arange(len(grid)) // 1440)
        for d in np.unique(day_idx[wm]):
            sel = wm & (day_idx == d)
            a_n += 1
            a_hit += float(np.mean(down[sel]) >= 0.5)
    adherence = a_hit / a_n
    # unscheduled start hazard by local hour (starts per hour), shrunk to the DISCO prior with 7 days' weight
    disco = site["data"].get("disco") or "LESCO"
    prior_day = DISCO_TRIPS.get(disco, 0.6)
    prior_h = np.ones(24)
    prior_h[17:23] = 2.5
    prior_h = prior_h / prior_h.sum() * prior_day
    counts = np.zeros(24)
    hrs = ((g["ts"][unsched_starts] + 5 * 3600) % 86400) // 3600 if unsched_starts else []
    for h in hrs:
        counts[int(h)] += 1
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
    p_down = 1 - (1 - p_down_s) * (1 - p_down_u)
    p_start = 1 - (1 - sched_start) * (1 - np.concatenate([p_start_u[1:], [p_start_u[-1]]]))
    dur = np.where(sched_start > p_start_u, dur_s, dur_steps_u).astype(int)
    gvec = np.where(sched_mask & (adherence > 0.9), 0.0, 1.0)
    # an observed outage now: down for its expected remaining duration (to the window's end when inside one)
    observed = None
    if len(grid) and known[-1] and grid[-1] == 0:
        k = len(grid) - 1
        while k > 0 and grid[k - 1] == 0:
            k -= 1
        elapsed = len(grid) - k
        if sched_mask[0]:
            j = 0
            while j < steps and sched_mask[j]:
                j += 1
            remaining = max(1, j)
        else:
            remaining = max(1, int(np.ceil(max(15.0, med_dur * 1.2 - elapsed) / 15)))
        gvec[:remaining] = 0.0
        p_down[:remaining] = 1.0
        observed = {"since": int(g["ts"][k]), "elapsed_min": int(elapsed), "expected_remaining_min": int(remaining * 15)}
    return {"stage": 1 if days_known >= 7 else 0, "p_down": np.clip(p_down, 0, 1), "p_start": np.clip(p_start, 0, 1), "dur_steps": dur, "g": gvec,
            "observed": observed, "adherence": round(adherence, 3), "median_minutes": med_dur, "windows": windows, "off_grid": False,
            "lam_day": round(float(lam_h.sum()), 3)}


# ------------------------------------------------------------------------------------------------ the bundle

def _r(a, n=3):
    return [round(float(x), n) for x in a]


def issue(site: dict, t0: int, save: bool = True) -> dict:
    """One forecast bundle for the optimiser; saved per kind for the forecast page and scoring."""
    t0 = align(t0)
    p, l, o = pv(site, t0), load(site, t0), outage(site, t0)
    if save:
        telemetry.save_forecast(site["id"], "pv", t0, p["stage"], VERSION, {"cs": _r(p["cs"]), **{k: _r(v) for k, v in p["q"].items()}, "pr": p.get("pr")})
        telemetry.save_forecast(site["id"], "load", t0, l["stage"], VERSION, {**{k: _r(v) for k, v in l["q"].items()}, "crit": _r(l["crit"])})
        telemetry.save_forecast(site["id"], "outage", t0, o["stage"], VERSION,
                                {"p_down": _r(o["p_down"]), "p_start": _r(o["p_start"]), "dur_steps": [int(x) for x in o["dur_steps"]], "g": _r(o["g"], 0),
                                 "observed": o.get("observed"), "adherence": o.get("adherence"), "off_grid": o.get("off_grid")})
    return {"t0": t0, "pv": p, "load": l, "outage": o}


def page(site: dict, kind: str) -> dict:
    """The latest issue of a kind with the actuals so far (15-minute means) for the forecast page."""
    f = telemetry.latest_forecast(site["id"], kind)
    if f is None:
        return {"kind": kind, "issue": None}
    t0 = f["belief_time"]
    t = step_times(t0)
    now = int(datetime.now(timezone.utc).timestamp())
    actual = None
    if kind in ("pv", "load"):
        g = telemetry.minute_grid(site["id"], t0, min(now, t0 + STEPS * STEP_S))
        col = g["pv"] if kind == "pv" else g["load"]
        n = len(col) // 15
        actual = [None if np.isnan(x) else round(float(x), 2) for x in np.nanmean(col[: n * 15].reshape(-1, 15), axis=1)] if n else []
    elif kind == "outage":
        g = telemetry.minute_grid(site["id"], t0, min(now, t0 + STEPS * STEP_S))
        n = len(g["grid"]) // 15
        actual = [None if np.isnan(x) else round(1 - float(x), 2) for x in np.nanmin(g["grid"][: n * 15].reshape(-1, 15), axis=1)] if n else []
    return {"kind": kind, "belief_time": t0, "stage": f["stage"], "model_version": f["model_version"], "t": [int(x) for x in t],
            "issue": {k: v for k, v in f.items() if k not in ("belief_time", "stage", "model_version")}, "actual": actual,
            "preliminary": f["stage"] == 0}


def score_day(site: dict, day) -> list[dict]:
    """Yesterday's scores at the day-ahead issue (the first issue at or before local midnight): PV and load nRMSE, load skill against
    same-day persistence, outage Brier."""
    t0 = twin.day_start_utc(day)
    g = telemetry.minute_grid(site["id"], t0, t0 + 86400)
    if g["completeness"] < 0.5:
        return []
    out = []
    for kind in ("pv", "load", "outage"):
        f = telemetry.latest_forecast(site["id"], kind, t0)
        if f is None or t0 - f["belief_time"] > 6 * 3600:
            continue
        off = (t0 - f["belief_time"]) // STEP_S
        sl = slice(off, off + 96)
        if kind in ("pv", "load"):
            col = g["pv"] if kind == "pv" else g["load"]
            act = np.nanmean(col.reshape(96, 15), axis=1)
            pred = np.array(f["q50"][sl])
            ok = ~np.isnan(act)
            if kind == "pv":
                ok &= np.array(f["cs"][sl]) > 0.05 * max(1e-6, max(f["cs"][sl]))
            if ok.sum() < 8:
                continue
            denom = max(1e-6, float(np.mean(act[ok])))
            nrmse = float(np.sqrt(np.mean((pred[ok] - act[ok]) ** 2)) / denom)
            row = {"kind": kind, "metric": "nrmse", "value": round(nrmse, 4), "horizon_class": "day_ahead"}
            if kind == "load":
                prev = telemetry.minute_grid(site["id"], t0 - 7 * 86400, t0 - 6 * 86400)["load"]
                pers = np.nanmean(prev.reshape(96, 15), axis=1)
                okp = ok & ~np.isnan(pers)
                if okp.sum() >= 8:
                    row["baseline_value"] = round(float(np.sqrt(np.mean((pers[okp] - act[okp]) ** 2)) / denom), 4)
            out.append(row)
        else:
            act = (np.nanmin(g["grid"].reshape(96, 15), axis=1) == 0).astype(float)
            pred = np.array(f["p_down"][sl])
            out.append({"kind": kind, "metric": "brier", "value": round(float(np.mean((pred - act) ** 2)), 4), "horizon_class": "day_ahead",
                        "baseline_value": round(float(np.mean((np.mean(act) - act) ** 2)), 4)})
    with store.connect() as con:
        for r in out:
            con.execute("INSERT OR REPLACE INTO forecast_scores (site_id, kind, day_local, horizon_class, metric, value, baseline_value) VALUES (?,?,?,?,?,?,?)",
                        (site["id"], r["kind"], day.isoformat(), r["horizon_class"], r["metric"], r["value"], r.get("baseline_value")))
    return out


def scores(site_id: str, days: int = 30) -> dict:
    since = (datetime.now(PKT).date() - timedelta(days=days)).isoformat()
    with store.connect() as con:
        rows = con.execute("SELECT * FROM forecast_scores WHERE site_id = ? AND day_local >= ? ORDER BY day_local", (site_id, since)).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["kind"], []).append({"day": r["day_local"], "metric": r["metric"], "value": r["value"], "baseline": r["baseline_value"]})
    summary = {}
    for k, v in out.items():
        vals7 = [x["value"] for x in v[-7:]]
        vals30 = [x["value"] for x in v]
        summary[k] = {"metric": v[0]["metric"], "last7": round(float(np.mean(vals7)), 4) if vals7 else None,
                      "last30": round(float(np.mean(vals30)), 4) if vals30 else None, "n": len(v)}
    return {"days": out, "summary": summary}
