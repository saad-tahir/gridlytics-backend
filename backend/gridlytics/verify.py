"""Savings verification (plan Part 8): the counterfactual replay against two baselines, the ledger by lever, switchback, the bill check and the fee check.

The actual arm is the measured day; the baseline arms are the native capture and the tuned table replayed by the twin on the measured PV, load and
outages, from the same starting state of charge. Every arm is priced at 1-minute resolution with the same tariff version, on the connection's
bearable-cost basis, with the dated fuel price and the same wear charge; the arm that ends the day with more stored energy is credited at the terminal
value (carry-over priced, not removed). The perfect-foresight bound is a hindsight re-solve, labelled not achievable.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import forecast, optimise, plans, sites, store, tariffs, telemetry, twin

PKT = timezone(timedelta(hours=5))
VERIFIED_DAYS_FOR_FINAL = 28


def _minute_prices(conn: dict | None, day: date, site_id: str) -> dict:
    t0 = twin.day_start_utc(day)
    if conn is None or conn["data"].get("regime") == "off_grid":
        z = np.zeros(1440)
        return {"c_imp": z, "c_exp": z, "peak": np.zeros(1440, dtype=bool), "mdi_rate": 0.0}
    mtd = plans.month_to_date(site_id, conn, t0)
    vec = tariffs.vectors(conn, datetime.fromtimestamp(t0, timezone.utc), 96, mtd["mtd"], None, 1e9)
    return {"c_imp": np.repeat(vec["c_imp"], 15), "c_exp": np.repeat(vec["c_exp"], 15), "peak": np.repeat(vec["peak"], 15),
            "mdi_rate": (vec["mdi"] or {}).get("rate_rs_per_kw", 0.0)}


def _genset_cost(site: dict, sim: dict, day: date) -> tuple[float, float]:
    g = (site.get("assets") or {}).get("genset")
    if not g:
        return 0.0, 0.0
    price, _ = tariffs.fuel_price(g.get("fuel_kind", "hsd"), day)
    on = np.asarray(sim["gen_on"], dtype=bool)
    starts = int(np.sum(on[1:] & ~on[:-1]) + (1 if len(on) and on[0] else 0))
    litres = float(np.sum(sim["fuel"]))
    cost = litres * price + starts * g["c_start_rs"] + on.sum() / 60 * g["c_runhr_rs"] + float(np.sum(sim["gen"]) / 60) * g["c_om_rs_per_kwh"]
    return cost, litres


def price_arm(site: dict, sim: dict, pr: dict, day: date, e_end_kwh: float, v_end: float) -> dict:
    b = (site.get("assets") or {}).get("battery")
    imp, exp = sim["imp"], sim["exp"]
    peak = pr["peak"]
    energy_peak = float(np.sum(pr["c_imp"][peak] * imp[peak]) / 60)
    energy_off = float(np.sum(pr["c_imp"][~peak] * imp[~peak]) / 60)
    export_credit = float(np.sum(pr["c_exp"] * exp) / 60)
    gen_rs, litres = _genset_cost(site, sim, day)
    discharge = float(np.sum(np.maximum(sim["bat"], 0)) / 60)
    wear = discharge * optimise.wear_cost(b) if b else 0.0
    blocks = imp.reshape(48, 30).mean(axis=1)
    return {"peak_energy_rs": round(energy_peak, 2), "offpeak_energy_rs": round(energy_off, 2), "export_credit_rs": round(export_credit, 2),
            "genset_rs": round(gen_rs, 2), "wear_rs": round(wear, 2), "litres": round(litres, 2),
            "cost_rs": round(energy_peak + energy_off - export_credit + gen_rs + wear, 2), "import_kwh": round(float(imp.sum() / 60), 2),
            "peak_import_kwh": round(float(imp[peak].sum() / 60), 2), "export_kwh": round(float(exp.sum() / 60), 2), "peak_kw": round(float(blocks.max()), 2),
            "discharge_kwh": round(discharge, 2), "unserved_kwh": round(float(np.sum(sim["unserved"]) / 60), 2),
            "genset_hours": round(float(np.sum(sim["gen_on"]) / 60), 2), "e_end_kwh": round(e_end_kwh, 2), "v_end": round(v_end, 2)}


def _replay_inputs(site: dict, day: date) -> tuple[dict, dict] | tuple[None, None]:
    t0 = twin.day_start_utc(day)
    g = telemetry.minute_grid(site["id"], t0, t0 + 86400)
    if g["completeness"] < 0.5:
        return None, None
    f = telemetry.filled(g)
    hours = ((g["ts"] + 5 * 3600) % 86400) // 3600
    series = {"ts": g["ts"], "pv": f["pv"] + f["curtail"], "load": f["load"] + f["unserved"], "grid": f["grid"],
              "crit": np.minimum(twin.critical_share(site)[hours], f["load"] + f["unserved"]), "temp": np.nan_to_num(g["temp"], nan=30.0)}
    return series, {"g": g, "f": f}


def _actual_sim(f: dict, g: dict) -> dict:
    return {"imp": f["imp"], "exp": f["exp"], "bat": f["bat"], "gen": f["gen"], "fuel": f["fuel"], "gen_on": np.nan_to_num(g["gen_on"]) > 0.5,
            "unserved": f["unserved"], "soc": np.nan_to_num(g["soc"])}


TUNED_VARIANTS = [(fp, hb, gc) for fp in (20, 40) for hb in (False, True) for gc in (0, 80)]


def tuned_table(site: dict, variant: tuple, peak_window: tuple[int, int]) -> list[dict]:
    fp, hold_before, gc = variant
    backup = float((site.get("native") or {}).get("body", {}).get("backup_soc_pct", 20.0))
    a, b = peak_window
    hhmm = lambda m: f"{(m // 60) % 24:02d}:{m % 60:02d}"
    pre = "hold" if hold_before else "self_use"
    rows = [{"start": "00:00", "mode": "charge_grid" if gc else pre, "soc_floor_pct": backup, "soc_target_pct": float(gc or 100), "grid_charge": bool(gc)},
            {"start": "06:00", "mode": pre, "soc_floor_pct": backup, "soc_target_pct": 100.0, "grid_charge": False}]
    if a >= 0:
        rows += [{"start": hhmm(a), "mode": "discharge", "soc_floor_pct": float(fp), "soc_target_pct": 100.0, "grid_charge": False},
                 {"start": hhmm(b), "mode": "self_use", "soc_floor_pct": backup, "soc_target_pct": 100.0, "grid_charge": False}]
    return sorted(rows, key=lambda r: r["start"])


def fit_tuned(site: dict, day: date) -> list[dict]:
    """The best static table on the observation window (the 7 days before the month starts, else the first 7 measured days), cached per month."""
    key = f"tuned:{site['id']}:{day.strftime('%Y-%m')}"
    held = store.get_setting(key)
    if held:
        return held
    conn = (site.get("connections") or [None])[0]
    cal = tariffs.calendar_for(conn, day) if conn else {"seasons": []}
    window = tariffs.peak_window(cal, day.month) if conn else (-1, -1)
    plant = twin.plant_of(site)
    month0 = day.replace(day=1)
    cands = [month0 - timedelta(days=i) for i in range(1, 8)]
    best, best_cost = None, None
    inputs = [(d, *_replay_inputs(site, d)) for d in cands]
    inputs = [x for x in inputs if x[1] is not None]
    if not inputs:
        inputs = [(d, *_replay_inputs(site, d)) for d in (day - timedelta(days=i) for i in range(0, 7))]
        inputs = [x for x in inputs if x[1] is not None]
    if not inputs or not (site.get("assets") or {}).get("battery"):
        return tuned_table(site, (20, False, 0), window)
    for v in TUNED_VARIANTS:
        table = tuned_table(site, v, window)
        total = 0.0
        for d, series, act in inputs:
            pr = _minute_prices(conn, d, site["id"])
            soc0 = float(np.nan_to_num(act["g"]["soc"][0], nan=0.5))
            sim = twin.simulate(series, twin.table_policy(table, series["ts"]), plant, soc0)
            total += price_arm(site, sim, pr, d, sim["soc"][-1] * plant.e_nom, 0.0)["cost_rs"]
        if best_cost is None or total < best_cost:
            best, best_cost = table, total
    store.set_setting(key, best)
    return best


def compute_day(site_id: str, day: date, with_bound: bool = True) -> dict | None:
    site = sites.full(site_id)
    if site is None:
        return None
    series, act = _replay_inputs(site, day)
    if series is None:
        return None
    conn = (site.get("connections") or [None])[0]
    pr = _minute_prices(conn, day, site_id)
    plant = twin.plant_of(site)
    b = (site.get("assets") or {}).get("battery")
    soc0 = float(np.nan_to_num(act["g"]["soc"][0], nan=0.5)) if b else 0.0
    gen0 = bool(np.nan_to_num(act["g"]["gen_on"][0]) > 0.5)
    actual = _actual_sim(act["f"], act["g"])
    e_end_actual = float(np.nan_to_num(act["g"]["soc"][-1], nan=soc0)) * plant.e_nom
    c_next = float(np.max(pr["c_imp"])) if conn and conn["data"].get("regime") != "off_grid" else 0.0
    gen_asset = (site.get("assets") or {}).get("genset")
    if gen_asset and (c_next == 0.0):
        price, _ = tariffs.fuel_price(gen_asset.get("fuel_kind", "hsd"), day)
        c_next = optimise._gen_marginal({"S": gen_asset["rated_kw"], "a": gen_asset["a"], "b": gen_asset["b"], "c_fuel": price,
                                         "c_om": gen_asset["c_om_rs_per_kwh"], "c_runhr": gen_asset["c_runhr_rs"]})
    v_end = max(0.0, (b["eta_d"] * c_next - optimise.wear_cost(b)) if b else 0.0)
    arms = {"actual": price_arm(site, actual, pr, day, e_end_actual, v_end)}
    sims = {"native": twin.simulate(series, twin.native_policy(site, series["ts"]), plant, soc0, gen0),
            "tuned": twin.simulate(series, twin.table_policy(fit_tuned(site, day), series["ts"]), plant, soc0, gen0)}
    for k, sim in sims.items():
        arms[k] = price_arm(site, sim, pr, day, float(sim["soc"][-1]) * plant.e_nom, v_end)
    # fidelity: in tier advisory the day counts only with an applied row; the twin and closed loop follow the plan by construction
    tier = site.get("tier", "monitor")
    with store.connect() as con:
        applied = con.execute("SELECT confirmation FROM applied WHERE site_id = ? AND day_local = ?", (site_id, day.isoformat())).fetchone()
    switch = None
    with store.connect() as con:
        r = con.execute("SELECT arm FROM switchback WHERE site_id = ? AND block_start <= ? AND block_end >= ?", (site_id, day.isoformat(),
                                                                                                                   day.isoformat())).fetchone()
        switch = r["arm"] if r else None
    if switch == "native":
        fidelity = "native_block"
    elif site.get("demo_link") or tier == "closed_loop":
        fidelity = "confirmed"
    elif tier == "advisory_writes":
        fidelity = "confirmed" if applied else "not_applied"
    else:
        fidelity = "recommendation_only"
    rows = {}
    for base in ("native", "tuned"):
        a, n = arms["actual"], arms[base]
        carry = (a["e_end_kwh"] - n["e_end_kwh"]) * v_end
        levers = {"peak_window": round(n["peak_energy_rs"] - a["peak_energy_rs"], 2), "offpeak_energy": round(n["offpeak_energy_rs"] - a["offpeak_energy_rs"], 2),
                  "export": round(a["export_credit_rs"] - n["export_credit_rs"], 2), "genset": round(n["genset_rs"] - a["genset_rs"], 2),
                  "wear": round(n["wear_rs"] - a["wear_rs"], 2), "carry_over": round(carry, 2)}
        saving = round(sum(levers.values()), 2)
        claim = saving if fidelity in ("confirmed",) else 0.0
        rows[base] = {"saving_rs": saving, "claimed_rs": claim, "levers": levers, "actual": a, "baseline": n, "fidelity": fidelity,
                      "litres_saved": round(n["litres"] - a["litres"], 2), "peak_kw_actual": a["peak_kw"], "peak_kw_baseline": n["peak_kw"],
                      "mdi_rate": pr["mdi_rate"], "unserved_kwh_actual": a["unserved_kwh"], "unserved_kwh_baseline": n["unserved_kwh"]}
    if with_bound and b:
        rows["native"]["perfect_foresight_rs"] = _bound(site, day, series, pr, soc0, arms["native"]["cost_rs"])
    now = store._now()
    with store.connect() as con:
        for base, data in rows.items():
            con.execute("INSERT INTO savings_days (site_id, day_local, baseline, data, computed) VALUES (?,?,?,?,?) ON CONFLICT(site_id, day_local, baseline) "
                        "DO UPDATE SET data = excluded.data, computed = excluded.computed", (site_id, day.isoformat(), base, json.dumps(data), now))
    return rows


def _bound(site: dict, day: date, series: dict, pr: dict, soc0: float, native_cost: float) -> float | None:
    """A hindsight re-solve of the day on the realised PV, load and outages at 15 minutes: the upper bound, not achievable."""
    try:
        t0 = twin.day_start_utc(day)
        q = lambda a: np.asarray(a).reshape(96, 15).mean(axis=1)
        bundle = {"pv": {"q": {"q50": q(series["pv"]), "q25": q(series["pv"])}, "stage": 1},
                  "load": {"q": {"q50": q(series["load"]), "q75": q(series["load"])}, "crit": q(series["crit"]), "stage": 1},
                  "outage": {"g": (q(series["grid"]) > 0.5).astype(float), "p_start": np.zeros(96), "dur_steps": np.full(96, 4), "p_down": 1 - q(series["grid"])}}
        site = dict(site)
        site["state"] = {**site.get("state", {}), "soc": soc0}
        p = plans.build_problem(site, t0, bundle, 96)
        if p.get("battery"):
            p["battery"]["E0"] = soc0 * p["battery"]["E_nom"]
            p["battery"]["sigma0"] = 0.0
        p["terminal"] = {"c_next": 0.0, "H_peak": 0.0, "R_next": 0.0}
        res = optimise.solve(p, time_limit=5.0)
        if res["status"] == "not_solved":
            return None
        bd = res["breakdown"]
        cost = bd["energy_rs"] + bd["genset_rs"] + bd["wear_rs"]
        return round(native_cost - cost, 2)
    except Exception:
        return None


def compute_range(site_id: str, d0: date, d1: date) -> int:
    n = 0
    d = d0
    while d <= d1:
        if compute_day(site_id, d):
            n += 1
        d += timedelta(days=1)
    return n


def summary(site_id: str, d0: str, d1: str, see_fee: bool = False) -> dict:
    site = sites.get(site_id)
    with store.connect() as con:
        rows = con.execute("SELECT day_local, baseline, data, computed FROM savings_days WHERE site_id = ? AND day_local >= ? AND day_local <= ? ORDER BY day_local",
                           (site_id, d0, d1)).fetchall()
    days: dict[str, dict] = {}
    computed = None
    for r in rows:
        days.setdefault(r["day_local"], {})[r["baseline"]] = json.loads(r["data"])
        computed = max(computed or "", r["computed"])
    out = {"from": d0, "to": d1, "computed": computed, "days": [], "baselines": {}}
    native_capture = sites.native_latest(site_id) is not None
    for base in ("native", "tuned"):
        levers: dict[str, float] = {}
        total = claimed = litres = 0.0
        verified = 0
        bound = 0.0
        months: dict[str, list[float]] = {}
        unserved_a = unserved_b = 0.0
        rate = 0.0
        for day, by in days.items():
            d = by.get(base)
            if not d:
                continue
            for k, v in d["levers"].items():
                levers[k] = levers.get(k, 0.0) + v
            total += d["saving_rs"]
            claimed += d["claimed_rs"]
            litres += d["litres_saved"]
            verified += int(d["fidelity"] == "confirmed")
            bound += d.get("perfect_foresight_rs") or 0.0
            m = months.setdefault(day[:7], [0.0, 0.0])
            m[0] = max(m[0], d["peak_kw_actual"])
            m[1] = max(m[1], d["peak_kw_baseline"])
            rate = d.get("mdi_rate") or rate
            unserved_a += d["unserved_kwh_actual"]
            unserved_b += d["unserved_kwh_baseline"]
        mdi = sum((b - a) * rate for a, b in months.values())
        levers["mdi"] = round(mdi, 2)
        out["baselines"][base] = {"saving_rs": round(total + mdi, 2), "claimed_rs": round(claimed + (mdi if verified else 0.0), 2),
                                  "levers": {k: round(v, 2) for k, v in levers.items()}, "litres_saved": round(litres, 1), "verified_days": verified,
                                  "perfect_foresight_rs": round(bound, 2) if base == "native" else None,
                                  "mdi_months": {k: {"actual_kw": v[0], "baseline_kw": v[1]} for k, v in months.items()},
                                  "unserved_kwh_actual": round(unserved_a, 1), "unserved_kwh_baseline": round(unserved_b, 1)}
    for day, by in sorted(days.items()):
        n = by.get("native", {})
        out["days"].append({"day": day, "saving_native_rs": n.get("saving_rs"), "saving_tuned_rs": (by.get("tuned") or {}).get("saving_rs"),
                            "fidelity": n.get("fidelity"), "actual_cost_rs": (n.get("actual") or {}).get("cost_rs"),
                            "native_cost_rs": (n.get("baseline") or {}).get("cost_rs"), "litres_actual": (n.get("actual") or {}).get("litres"),
                            "litres_native": (n.get("baseline") or {}).get("litres"), "peak_kw": (n.get("actual") or {}).get("peak_kw"),
                            "peak_import_kwh": (n.get("actual") or {}).get("peak_import_kwh"), "levers": n.get("levers")})
    verified = out["baselines"].get("native", {}).get("verified_days", 0)
    out["preliminary"] = verified < VERIFIED_DAYS_FOR_FINAL or not native_capture
    out["blocked"] = None if native_capture else "Capture the inverter's native settings first: the baseline is what the inverter did on its own"
    out["method"] = ("Counterfactual replay at 1-minute resolution against the native capture and a tuned table, priced on the bearable-cost basis "
                     "(recoverable GST and adjustable s.235 excluded), with the dated fuel price and the same wear charge on both arms")
    if see_fee and site:
        fee = float(store.get_setting(f"fee:{site_id}", 0) or 0)
        months_n = max(1, len({d[:7] for d in days}))
        saving_pm = out["baselines"].get("native", {}).get("claimed_rs", 0.0) / months_n
        out["fee"] = {"fee_rs_per_month": fee, "verified_saving_rs_per_month": round(saving_pm, 0),
                      "within_one_third": fee <= saving_pm / 3 if fee else None}
    return out


# ------------------------------------------------------------------------------------------------ switchback (Part 8.2)

def plan_switchback(site_id: str, start: date, fortnights: int, block_days: int, by: str, seed: int | None = None) -> list[dict]:
    rng = np.random.default_rng(seed if seed is not None else int(datetime.now().timestamp()))
    rows = []
    with store.connect() as con:
        con.execute("DELETE FROM switchback WHERE site_id = ? AND locked = 0", (site_id,))
        d = start
        for _ in range(fortnights):
            first = "plan" if rng.random() < 0.5 else "native"
            for arm in (first, "native" if first == "plan" else "plan"):
                end = d + timedelta(days=block_days - 1)
                con.execute("INSERT OR REPLACE INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,0)",
                            (site_id, d.isoformat(), end.isoformat(), arm))
                rows.append({"block_start": d.isoformat(), "block_end": end.isoformat(), "arm": arm})
                d = end + timedelta(days=1)
    store.audit(by, "painted the switchback calendar", site_id, {"start": start.isoformat(), "fortnights": fortnights, "block_days": block_days})
    return rows


def switchback_blocks(site_id: str) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM switchback WHERE site_id = ? ORDER BY block_start", (site_id,)).fetchall()
    return [dict(r) for r in rows]


def arm_on(site_id: str, day: date) -> str | None:
    with store.connect() as con:
        r = con.execute("SELECT arm FROM switchback WHERE site_id = ? AND block_start <= ? AND block_end >= ?", (site_id, day.isoformat(), day.isoformat())).fetchone()
    return r["arm"] if r else None


def switchback_result(site_id: str) -> dict:
    """Mean daily cost on plan blocks against native blocks, unadjusted and adjusted by a regression on PV kWh, load kWh and outage hours."""
    blocks = switchback_blocks(site_id)
    if not blocks:
        return {"blocks": [], "result": None}
    d0, d1 = blocks[0]["block_start"], blocks[-1]["block_end"]
    tdays = {d["day"]: d for d in telemetry.days(site_id, d0, d1)}
    with store.connect() as con:
        rows = con.execute("SELECT day_local, data FROM savings_days WHERE site_id = ? AND baseline = 'native' AND day_local >= ? AND day_local <= ?",
                           (site_id, d0, d1)).fetchall()
    X, y, arms = [], [], []
    for r in rows:
        day = r["day_local"]
        arm = next((b["arm"] for b in blocks if b["block_start"] <= day <= b["block_end"]), None)
        t = tdays.get(day)
        if arm is None or t is None:
            continue
        cost = json.loads(r["data"])["actual"]["cost_rs"]
        X.append([1.0, 1.0 if arm == "plan" else 0.0, t["pv_kwh"], t["load_kwh"], t["outage_minutes"] / 60])
        y.append(cost)
        arms.append(arm)
    if len(set(arms)) < 2 or len(y) < 6:
        return {"blocks": blocks, "result": None, "days": len(y)}
    X, y = np.array(X), np.array(y)
    a = np.array(arms)
    unadj = float(np.mean(y[a == "native"]) - np.mean(y[a == "plan"]))
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    adj = float(-coef[1])
    return {"blocks": blocks, "days": len(y), "result": {"saving_rs_per_day_unadjusted": round(unadj, 2), "saving_rs_per_day_adjusted": round(adj, 2),
                                                          "plan_days": int(np.sum(a == "plan")), "native_days": int(np.sum(a == "native"))}}


# ------------------------------------------------------------------------------------------------ bill truth (Part 8.2 layer 3)

def add_bill(site_id: str, fields: dict, by: str) -> dict:
    conns = sites.connections(site_id)
    if not conns:
        raise ValueError("Add the site's grid connection first")
    try:
        p0, p1 = date.fromisoformat(fields["period_start"]), date.fromisoformat(fields["period_end"])
        total = float(fields["total_rs"])
    except (KeyError, ValueError, TypeError):
        raise ValueError("A bill needs period_start, period_end (YYYY-MM-DD) and total_rs") from None
    if p1 < p0 or (p1 - p0).days > 40:
        raise ValueError("A billing period runs forwards and is at most 40 days")
    real = {k: fields.get(k) for k in ("total_rs", "units_peak", "units_off_peak", "mdi_kw", "reference") if fields.get(k) not in (None, "")}
    bid = store.new_id()
    rebuilt = rebuild_bill(site_id, conns[0], p0, p1)
    diff = round((rebuilt["total_rs"] - total) / total * 100, 2) if total else None
    with store.connect() as con:
        con.execute("INSERT INTO bills (id, site_id, connection_id, period_start, period_end, real, rebuilt, difference_pct, created) VALUES (?,?,?,?,?,?,?,?,?)",
                    (bid, site_id, conns[0]["id"], p0.isoformat(), p1.isoformat(), json.dumps(real), json.dumps(rebuilt), diff, store._now()))
    store.audit(by, "entered a DISCO bill", site_id, {"period": f"{p0}..{p1}", "difference_pct": diff})
    return {"id": bid, "real": real, "rebuilt": rebuilt, "difference_pct": diff}


def rebuild_bill(site_id: str, conn: dict, p0: date, p1: date) -> dict:
    t0 = twin.day_start_utc(p0)
    t1 = twin.day_start_utc(p1) + 86400
    g = telemetry.minute_grid(site_id, t0, t1)
    f = telemetry.filled(g)
    out = tariffs.rebuild(g["ts"], f["imp"], f["exp"], conn, p0, p1)
    out["completeness"] = round(g["completeness"], 4)
    return out


def bills(site_id: str) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM bills WHERE site_id = ? ORDER BY period_start DESC", (site_id,)).fetchall()
    return [{**dict(r), "real": json.loads(r["real"]), "rebuilt": json.loads(r["rebuilt"])} for r in rows]
