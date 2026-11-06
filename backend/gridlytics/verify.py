"""Savings verification (plan Part 8): the counterfactual replay against two baselines, the ledger by lever, switchback, the bill check and the fee check.

The actual arm is the measured day; the baseline arms are the native capture and the tuned table replayed by the twin on the measured PV, load and
outages, from the same starting state of charge. Every arm is priced at 1-minute resolution with the same tariff version, on the connection's
bearable-cost basis, with the dated fuel price and the same wear charge; the arm that ends the day with more stored energy is credited at the terminal
value (carry-over priced, not removed). The perfect-foresight bound is a hindsight re-solve, labelled not achievable.
"""
from __future__ import annotations

import calendar
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import forecast, optimise, plans, sites, store, tariffs, telemetry, twin
from .schemas import Refusal

PKT = timezone(timedelta(hours=5))
VERIFIED_DAYS_FOR_FINAL = 28
SWITCHBACK_WEEKS_FOR_FINAL = 8
#: the fidelity classes that claim their saving (plan 8.4: a partial day is reported with its fidelity beside it; a day not applied claims nothing)
CLAIMING = ("confirmed", "partial")


class CalendarLocked(Refusal, ValueError):
    """A repaint of a switchback calendar the pilot has locked: 409 through the API's refusal handler, and a ValueError for other callers."""

    def __init__(self, detail: str = "The switchback calendar is locked: the pilot has started, so it cannot be repainted"):
        super().__init__(409, detail, "calendar_locked")


def _month_bounds(day: date) -> tuple[int, int]:
    t_ms = int(datetime(day.year, day.month, 1, tzinfo=PKT).timestamp())
    t_nx = int(datetime(day.year + (day.month == 12), day.month % 12 + 1, 1, tzinfo=PKT).timestamp())
    return t_ms, t_nx


def month_imports(site_id: str, conn: dict, day: date, now: int | None = None) -> dict:
    """The billing month's grid imports per period (kWh), which settle the month's ICP case for a day scored in hindsight: the realised totals once the
    month is over; before then (and over telemetry gaps) the month's metered minutes scaled up to the whole month, period by period."""
    t_ms, t_nx = _month_bounds(day)
    now = int(datetime.now(timezone.utc).timestamp()) if now is None else int(now)
    end = min(t_nx, max(now // 60 * 60, twin.day_start_utc(day) + 86400))
    g = telemetry.minute_grid(site_id, t_ms, end)
    imp = g["imp"]
    known = ~np.isnan(imp)
    pk = plans.peak_mask(conn, g["ts"])
    pk_all = plans.peak_mask(conn, np.arange(t_ms, t_nx, 60, dtype=np.int64))
    out = {}
    for p, sel, sel_all in (("peak", pk, pk_all), ("off_peak", ~pk, ~pk_all)):
        n = int(np.sum(known & sel))
        kwh = float(np.sum(imp[known & sel]) / 60)
        out[p] = kwh * float(np.sum(sel_all)) / n if n else 0.0
    return out


def _minute_prices(conn: dict | None, day: date, site_id: str, month_q: dict | None = None) -> dict:
    t0 = twin.day_start_utc(day)
    if conn is None or conn["data"].get("regime") == "off_grid":
        z = np.zeros(1440)
        return {"c_imp": z, "c_exp": z, "peak": np.zeros(1440, dtype=bool), "mdi_rate": 0.0, "mdi_floor": 0.0, "mdi_interval": 30, "icp": {}}
    q = month_q if month_q is not None else month_imports(site_id, conn, day)
    # no swing: the month's own totals settle each period's ICP case, so an above-baseline period prices every minute at the ICP rate (plus the positive
    # FCA), as the bill engine charges the increment; the optimiser's binary for the uncertain middle does not exist in a replay
    vec = tariffs.vectors(conn, datetime.fromtimestamp(t0, timezone.utc), 96, q, None, 0.0)
    mdi = vec["mdi"] or {}
    iv = int(conn["data"].get("mdi_interval_min", 30) or 30)
    if iv <= 0 or 1440 % iv:
        iv = 30
    return {"c_imp": np.repeat(vec["c_imp"], 15), "c_exp": np.repeat(vec["c_exp"], 15), "peak": np.repeat(vec["peak"], 15),
            "mdi_rate": mdi.get("rate_rs_per_kw", 0.0), "mdi_floor": float(mdi.get("floor_kw", 0.0) or 0.0), "mdi_interval": iv,
            "icp": {p: info["case"] for p, info in (vec.get("icp") or {}).items()}}


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
    iv = int(pr.get("mdi_interval", 30) or 30)
    if iv <= 0 or len(imp) % iv:
        iv = 30
    blocks = imp.reshape(len(imp) // iv, iv).mean(axis=1)  # the connection's MDI blocks, aligned to the hour as the bill engine aligns them
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


def _soc(g: dict) -> np.ndarray:
    """State of charge (0-1) per minute: the independent estimate where present, else the BMS reading (as the battery service reads it); NaN when neither."""
    est = np.asarray(g["soc"], dtype=float)
    bms = g.get("soc_bms")
    return est if bms is None else np.where(np.isnan(est), np.asarray(bms, dtype=float), est)


def _soc_start(site_id: str, t0: int, g: dict, f: dict, plant: twin.Plant) -> float | None:
    """The state of charge at the day's start: the last reading before it (the previous day's 23:59 reading is the state at 00:00), else the day's first
    reading with the day's flows up to it backed out (a reading is the state after its minute's flow). None when the day has no reading at all."""
    prev = _soc(telemetry.minute_grid(site_id, t0 - 3600, t0))
    prev = prev[~np.isnan(prev)]
    soc = _soc(g)
    idx = np.where(~np.isnan(soc))[0]
    if not idx.size:
        return None
    if prev.size:
        return float(prev[-1])
    if plant.e_nom <= 0:
        return float(soc[idx[0]])
    k = int(idx[0])
    bat = np.asarray(f["bat"][:k + 1], dtype=float)
    e = float(soc[k]) * plant.e_nom + float(np.sum(np.where(bat > 0, bat / plant.eta_d, bat * plant.eta_c)) / 60)
    return float(np.clip(e / plant.e_nom, 0.0, 1.0))


def _soc_end(g: dict, f: dict, plant: twin.Plant) -> float | None:
    """The state of charge at the day's end: the day's last reading with the measured flows after it carried forward. None without a reading."""
    soc = _soc(g)
    idx = np.where(~np.isnan(soc))[0]
    if not idx.size:
        return None
    k = int(idx[-1])
    if plant.e_nom <= 0:
        return float(soc[k])
    bat = np.asarray(f["bat"][k + 1:], dtype=float)
    e = float(soc[k]) * plant.e_nom - float(np.sum(np.where(bat > 0, bat / plant.eta_d, bat * plant.eta_c)) / 60)
    return float(np.clip(e / plant.e_nom, 0.0, 1.0))


def _genset_unknown(g: dict) -> bool:
    """No genset signal at all for the day (running flag, power and litres all missing): the genset is unknown, not off (plan 7.5)."""
    return bool(np.all(np.isnan(g["gen_on"])) and np.all(np.isnan(g["gen"])) and np.all(np.isnan(g["fuel"])))


def _actual_sim(f: dict, g: dict, plant: twin.Plant | None = None) -> dict:
    """The measured day as an arm. Where the genset's meters are blank, the running flag falls back to its power (then its litres), its power to the
    site's energy balance within the genset's range, and its litres to the fitted curve the replays use (plan 8.3: metered litres where a sensor exists)."""
    flag, p_meas, fuel_meas = np.asarray(g["gen_on"], dtype=float), np.asarray(g["gen"], dtype=float), np.asarray(g["fuel"], dtype=float)
    on = np.where(~np.isnan(flag), flag > 0.5, np.where(~np.isnan(p_meas), p_meas > 0.0, np.nan_to_num(fuel_meas) > 0.0))
    gen, fuel = f["gen"], f["fuel"]
    if plant is not None and plant.gen_kw > 0:
        balance = f["load"] - f["pv"] - f["bat"] - f["imp"] + f["exp"]
        p_est = np.clip(balance, plant.gen_fmin * plant.gen_kw, plant.gen_kw)
        gen = np.where(np.isnan(p_meas), np.where(on, p_est, 0.0), p_meas)
        curve = (plant.gen_a * gen + plant.gen_b * plant.gen_kw) / 60.0
        fuel = np.where(np.isnan(fuel_meas), np.where(on, curve, 0.0), fuel_meas)
    return {"imp": f["imp"], "exp": f["exp"], "bat": f["bat"], "gen": gen, "fuel": fuel, "gen_on": on.astype(bool),
            "unserved": f["unserved"], "soc": np.nan_to_num(_soc(g))}


def native_for_day(site_id: str, day: date) -> dict | None:
    """The native capture in force on a day: the latest captured by the day's end, else the first one (the onboarding capture stands for the days before
    it was recorded). Plan 8.1 and 8.4: a later installer change is a new dated capture, and a day is re-scored with that day's inputs."""
    end = datetime.fromtimestamp(twin.day_start_utc(day) + 86400, timezone.utc).isoformat(timespec="seconds")
    with store.connect() as con:
        r = con.execute("SELECT * FROM native_settings WHERE site_id = ? AND captured_at <= ? ORDER BY version DESC LIMIT 1", (site_id, end)).fetchone()
        if r is None:
            r = con.execute("SELECT * FROM native_settings WHERE site_id = ? ORDER BY version ASC LIMIT 1", (site_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["body"] = json.loads(d["body"])
    d["has_photo"] = bool(d.pop("photo", None))
    return d


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


def _tuned_key(site: dict, day: date) -> str:
    """The tuned table's cache key: per month and per native capture (its backup floor shapes the table)."""
    return f"tuned:{site['id']}:{day.strftime('%Y-%m')}:v{(site.get('native') or {}).get('version', 0)}"


def fit_tuned(site: dict, day: date) -> list[dict]:
    """The best static table on the observation window (the 7 days before the month starts, else the first 7 measured days), cached per month and
    capture."""
    key = _tuned_key(site, day)
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
    on_grid = conn is not None and conn["data"].get("regime") != "off_grid"
    month_q: dict = {}
    prepared = []
    for d, series, act in inputs:
        soc0 = _soc_start(site["id"], twin.day_start_utc(d), act["g"], act["f"], plant)
        if soc0 is None:  # a day with no state of charge cannot be replayed fairly
            continue
        mk = (d.year, d.month)
        if on_grid and mk not in month_q:
            month_q[mk] = month_imports(site["id"], conn, d)
        prepared.append((d, series, _minute_prices(conn, d, site["id"], month_q.get(mk)), soc0))
    if not prepared:
        return tuned_table(site, (20, False, 0), window)
    for v in TUNED_VARIANTS:
        table = tuned_table(site, v, window)
        total = 0.0
        for d, series, pr, soc0 in prepared:
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
    site["native"] = native_for_day(site_id, day) or site.get("native")
    series, act = _replay_inputs(site, day)
    if series is None:
        return None
    conn = (site.get("connections") or [None])[0]
    plant = twin.plant_of(site)
    b = (site.get("assets") or {}).get("battery")
    soc0, e_end_actual = 0.0, 0.0
    if b:
        # both replays start where the battery really was at 00:00, and the actual arm ends where it really was; without a reading the day is unscoreable
        soc0 = _soc_start(site_id, twin.day_start_utc(day), act["g"], act["f"], plant)
        soc_end = _soc_end(act["g"], act["f"], plant)
        if soc0 is None or soc_end is None:
            return None
        e_end_actual = soc_end * plant.e_nom
    pr = _minute_prices(conn, day, site_id)
    gen0 = bool(np.nan_to_num(act["g"]["gen_on"][0]) > 0.5)
    gen_asset = (site.get("assets") or {}).get("genset")
    gen_unknown = bool(gen_asset) and _genset_unknown(act["g"])
    actual = _actual_sim(act["f"], act["g"], plant if gen_asset else None)
    c_next = float(np.max(pr["c_imp"])) if conn and conn["data"].get("regime") != "off_grid" else 0.0
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
    # fidelity gates the claim (plan 8.4): a monitor site only recommends, even in the demo; the twin and closed loop follow the plan by construction;
    # in tier advisory a day counts only once telemetry confirms the keyed table (plan 7.6), never on the mark alone
    tier = site.get("tier", "monitor")
    with store.connect() as con:
        applied = con.execute("SELECT * FROM applied WHERE site_id = ? AND day_local = ? ORDER BY created DESC, rowid DESC LIMIT 1",
                              (site_id, day.isoformat())).fetchone()
        r = con.execute("SELECT arm FROM switchback WHERE site_id = ? AND block_start <= ? AND block_end >= ? ORDER BY locked DESC, block_start DESC",
                        (site_id, day.isoformat(), day.isoformat())).fetchone()
    switch = r["arm"] if r else None
    fidelity_score = None
    if switch == "native":
        fidelity = "native_block"
    elif tier == "monitor":
        fidelity = "recommendation_only"
    elif site.get("demo_link") or tier == "closed_loop":
        fidelity = "confirmed"
    elif tier == "advisory_writes":
        if applied is None:
            fidelity = "not_applied"
        else:
            conf = confirm_applied(site, day, dict(applied), act, plant, sims["native"])
            fidelity = conf["class"] if conf else "pending"
            fidelity_score = conf["fidelity"] if conf else None
    else:
        fidelity = "recommendation_only"
    rows = {}
    for base in ("native", "tuned"):
        a, n = arms["actual"], arms[base]
        carry = (a["e_end_kwh"] - n["e_end_kwh"]) * v_end
        # a genset with no signal at all is unknown, not off: its lever and litres are withheld rather than booked as the baseline's whole fuel bill
        levers = {"peak_window": round(n["peak_energy_rs"] - a["peak_energy_rs"], 2), "offpeak_energy": round(n["offpeak_energy_rs"] - a["offpeak_energy_rs"], 2),
                  "export": round(a["export_credit_rs"] - n["export_credit_rs"], 2),
                  "genset": None if gen_unknown else round(n["genset_rs"] - a["genset_rs"], 2),
                  "wear": round(n["wear_rs"] - a["wear_rs"], 2), "carry_over": round(carry, 2)}
        saving = round(sum(v for v in levers.values() if v is not None), 2)
        claim = saving if fidelity in CLAIMING else 0.0
        rows[base] = {"saving_rs": saving, "claimed_rs": claim, "levers": levers, "actual": a, "baseline": n, "fidelity": fidelity,
                      "fidelity_score": fidelity_score, "litres_saved": None if gen_unknown else round(n["litres"] - a["litres"], 2),
                      "genset_unknown": gen_unknown, "peak_kw_actual": a["peak_kw"], "peak_kw_baseline": n["peak_kw"], "mdi_rate": pr["mdi_rate"],
                      "mdi_floor_kw": pr["mdi_floor"], "mdi_interval_min": pr["mdi_interval"], "icp_case": pr["icp"],
                      "native_capture_version": (site.get("native") or {}).get("version"),
                      "unserved_kwh_actual": a["unserved_kwh"], "unserved_kwh_baseline": n["unserved_kwh"]}
    if with_bound and b:
        rows["native"]["perfect_foresight_rs"] = _bound(site, day, series, pr, soc0, arms["native"]["cost_rs"],
                                                        e_end_native=float(sims["native"]["soc"][-1]) * plant.e_nom, c_next=c_next, v_end=v_end)
    now = store._now()
    with store.connect() as con:
        for base, data in rows.items():
            con.execute("INSERT INTO savings_days (site_id, day_local, baseline, data, computed) VALUES (?,?,?,?,?) ON CONFLICT(site_id, day_local, baseline) "
                        "DO UPDATE SET data = excluded.data, computed = excluded.computed", (site_id, day.isoformat(), base, json.dumps(data), now))
    return rows


def _bound(site: dict, day: date, series: dict, pr: dict, soc0: float, native_cost: float, e_end_native: float | None = None,
           c_next: float = 0.0, v_end: float = 0.0) -> float | None:
    """A hindsight re-solve of the day on the realised PV, load and outages at 15 minutes: the upper bound, not achievable.

    The solve values the energy it leaves in the battery at the day's terminal value and the bound credits the difference from the native arm's end
    energy at the same v_end, as the daily saving does (otherwise the bound earns free energy by draining the battery). The day is solved with its own
    inputs: overrides, a manual reserve and the tank level active today do not apply to a past day."""
    try:
        t0 = twin.day_start_utc(day)
        q = lambda a: np.asarray(a).reshape(96, 15).mean(axis=1)
        bundle = {"pv": {"q": {"q50": q(series["pv"]), "q25": q(series["pv"])}, "stage": 1},
                  "load": {"q": {"q50": q(series["load"]), "q75": q(series["load"])}, "crit": q(series["crit"]), "stage": 1},
                  "outage": {"g": (q(series["grid"]) > 0.5).astype(float), "p_start": np.zeros(96), "dur_steps": np.full(96, 4), "p_down": 1 - q(series["grid"])}}
        site = dict(site)
        site["state"] = {**site.get("state", {}), "soc": soc0}
        p = plans.build_problem(site, t0, bundle, 96)
        T = int(p["T"])
        p["override_mask"] = np.zeros(T, dtype=bool)
        p["overrides"] = []
        p.pop("reserve_manual", None)
        if p.get("genset"):
            p["genset"]["hold"] = np.zeros(T, dtype=bool)
            p["genset"]["fuel_available"] = 1e9
        if p.get("battery"):
            p["battery"]["E0"] = soc0 * p["battery"]["E_nom"]
            p["battery"]["sigma0"] = 0.0
            p["battery"]["charge_block"] = np.zeros(T, dtype=bool)
        valued = e_end_native is not None and v_end > 0
        # H_peak of a day: the terminal value may cover the whole battery, as the ledger's carry-over does
        p["terminal"] = {"c_next": float(c_next) if valued else 0.0, "H_peak": 24.0 if valued else 0.0, "R_next": 0.0}
        res = optimise.solve(p, time_limit=5.0)
        if res["status"] == "not_solved":
            return None
        bd = res["breakdown"]
        cost = bd["energy_rs"] + bd["genset_rs"] + bd["wear_rs"]
        if p.get("battery") and e_end_native is not None:
            cost -= (float(res["sol"]["E"][-1]) - float(e_end_native)) * v_end
        return round(native_cost - cost, 2)
    except Exception:
        return None


def confirm_applied(site: dict, day: date, applied: dict, act: dict, plant: twin.Plant, native_sim: dict) -> dict | None:
    """Confirm a table the installer keyed (tier 2) from telemetry alone and score the day's fidelity (plan 7.6, R6 section 4.6); the class and score
    are written to the applied row. None when the plan has no slots on the day (nothing to confirm: the mark stays pending and claims nothing).

    * Boundaries: discharge above 10 percent of rated within 30 minutes of a discharge slot's start and below 5 percent within 30 minutes of its end;
      charging while importing above load within 30 minutes of a grid-charge slot's start. Other boundaries are not scored.
    * Floors: the state of charge stays at or above the slot's floor less 3 points while the grid is present.
    * Forbidden: grid charging in a slot without the flag, or discharge in a hold slot, for 5 minutes or more.
    * Energy fidelity: 1 - clip(sum |E_planned - E_measured| / max(sum |E_planned|, 0.1 x usable kWh), 0, 1) over the slots.
    * Confirmed: at least 80 percent of scorable boundaries hit, nothing forbidden, no floor breach. Not applied: the measured battery energy per slot is
      closer to the native replay than to the plan. Partial otherwise. Fidelity = 0.4 x hit rate + 0.3 x (1 - floor breach share) + 0.3 x energy fidelity."""
    plan = plans.get(site["id"], applied["plan_id"]) if applied.get("plan_id") else None
    if plan is None or plant.e_nom <= 0:
        return None
    t0 = twin.day_start_utc(day)
    t1 = t0 + 86400
    slots = sorted((s for d in ((plan.get("slots") or {}).get("days") or []) for s in (d.get("slots") or [])), key=lambda s: int(s["start_ts"]))
    slots = [s for s in slots if int(s["end_ts"]) > t0 and int(s["start_ts"]) < t1]
    if not slots:
        return None
    g, f = act["g"], act["f"]
    ts = g["ts"]
    bat_raw = np.asarray(g["bat"], dtype=float)
    soc = _soc(g) * 100.0
    on_grid = np.asarray(f["grid"]) > 0.5
    p_dis, p_ch = max(plant.p_dis, 1e-6), max(plant.p_ch, 1e-6)
    hits = scorable = 0
    for prev, cur in zip(slots, slots[1:]):
        bnd = int(cur["start_ts"])
        if not t0 <= bnd < t1:
            continue
        w = (ts >= bnd) & (ts < bnd + 1800) & ~np.isnan(bat_raw)
        if not w.any():
            continue
        if cur["mode"] == "discharge" and prev["mode"] != "discharge":
            ok = bool(np.any(bat_raw[w] > 0.10 * p_dis))
        elif prev["mode"] == "discharge" and cur["mode"] != "discharge":
            ok = bool(np.any(bat_raw[w] < 0.05 * p_dis))
        elif cur["mode"] == "charge_grid" and prev["mode"] != "charge_grid":
            ok = bool(np.any((-bat_raw[w] > 0.10 * p_ch) & (f["imp"][w] > f["load"][w])))
        else:
            continue
        scorable += 1
        hits += int(ok)
    steps = plan.get("steps") or []
    st_t = np.array([int(s["t"]) for s in steps], dtype=np.int64)
    st_e = np.array([(float(s.get("discharge_kw") or 0.0) - float(s.get("charge_kw") or 0.0)) * 0.25 for s in steps], dtype=float)
    breaches = n_slots = 0
    forbidden = False
    dev_plan = dev_native = planned_abs = 0.0
    for s in slots:
        m = (ts >= int(s["start_ts"])) & (ts < int(s["end_ts"]))
        if not m.any():
            continue
        n_slots += 1
        mg = m & on_grid
        sv = soc[mg]
        sv = sv[~np.isnan(sv)]
        if sv.size and float(sv.min()) < float(s.get("soc_floor_pct", 0.0)) - 3.0:
            breaches += 1
        grid_charging = mg & (f["imp"] > f["load"] + 0.05 * p_ch) & (f["bat"] < -0.05 * p_ch)
        if not s.get("grid_charge") and int(np.sum(grid_charging)) >= 5:
            forbidden = True
        if s["mode"] == "hold" and int(np.sum(mg & (f["bat"] > 0.05 * p_dis))) >= 5:
            forbidden = True
        sel = (st_t >= max(int(s["start_ts"]), t0)) & (st_t < min(int(s["end_ts"]), t1)) if len(st_t) else np.zeros(0, dtype=bool)
        e_plan = float(st_e[sel].sum()) if len(st_t) else 0.0
        e_meas = float(np.sum(f["bat"][m]) / 60)
        e_nat = float(np.sum(np.asarray(native_sim["bat"])[m]) / 60)
        dev_plan += abs(e_plan - e_meas)
        dev_native += abs(e_nat - e_meas)
        planned_abs += abs(e_plan)
    energy_fid = 1.0 - float(np.clip(dev_plan / max(planned_abs, 0.1 * plant.e_nom), 0.0, 1.0))
    hit_rate = hits / scorable if scorable else 1.0
    score = round(0.4 * hit_rate + 0.3 * (1.0 - breaches / max(n_slots, 1)) + 0.3 * energy_fid, 3)
    if hit_rate >= 0.8 and not forbidden and breaches == 0:
        cls = "confirmed"
    elif dev_native < dev_plan:
        cls = "not_applied"
    else:
        cls = "partial"
    detail = {"boundaries_hit": hits, "boundaries_scored": scorable, "floor_breaches": breaches, "slots": n_slots, "forbidden": forbidden,
              "energy_fidelity": round(energy_fid, 3), "checked": store._now()}
    with store.connect() as con:
        try:
            data = json.loads(applied.get("data") or "{}")
        except (TypeError, ValueError):
            data = {}
        data["confirmation_detail"] = detail
        con.execute("UPDATE applied SET confirmation = ?, fidelity = ?, data = ? WHERE id = ?", (cls, score, json.dumps(data), applied["id"]))
    return {"class": cls, "fidelity": score, **detail}


def compute_range(site_id: str, d0: date, d1: date) -> int:
    n = 0
    d = d0
    while d <= d1:
        if compute_day(site_id, d):
            n += 1
        d += timedelta(days=1)
    return n


def _months_between(d0: str, d1: str) -> list[str]:
    """The calendar months ("YYYY-MM") a period touches."""
    try:
        a, b = date.fromisoformat(d0[:10]), date.fromisoformat(d1[:10])
    except ValueError:
        return []
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month) and len(out) < 240:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


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
    #: per baseline and month: (claimed rupees of the days, days scored, the month's claimed MDI lever), for the fee check
    per_month: dict[str, dict[str, tuple[float, int, float]]] = {}
    for base in ("native", "tuned"):
        levers: dict[str, float] = {}
        total = claimed = litres = 0.0
        verified = 0
        gen_unknown_days = 0
        bound = 0.0
        # per month: actual peak, baseline peak, billing-demand floor, claiming days, claimed rupees, days scored
        months: dict[str, list] = {}
        unserved_a = unserved_b = 0.0
        rate = 0.0
        for day, by in days.items():
            d = by.get(base)
            if not d:
                continue
            for k, v in d["levers"].items():
                if v is not None:  # a lever withheld (an unmetered genset) adds nothing
                    levers[k] = levers.get(k, 0.0) + v
            total += d["saving_rs"]
            claimed += d["claimed_rs"]
            litres += d.get("litres_saved") or 0.0
            gen_unknown_days += int(bool(d.get("genset_unknown")))
            verified += int(d["fidelity"] == "confirmed")
            bound += d.get("perfect_foresight_rs") or 0.0
            m = months.setdefault(day[:7], [0.0, 0.0, 0.0, 0, 0.0, 0])
            m[0] = max(m[0], d["peak_kw_actual"])
            m[1] = max(m[1], d["peak_kw_baseline"])
            m[2] = max(m[2], float(d.get("mdi_floor_kw") or 0.0))
            m[3] += int(d["fidelity"] in CLAIMING)
            m[4] += d["claimed_rs"]
            m[5] += 1
            rate = d.get("mdi_rate") or rate
            unserved_a += d["unserved_kwh_actual"]
            unserved_b += d["unserved_kwh_baseline"]
        # the bill charges max(floor, MDI) a month: below the floor a lower peak saves nothing
        mdi_by_month = {k: (max(v[2], v[1]) - max(v[2], v[0])) * rate for k, v in months.items()}
        mdi = sum(mdi_by_month.values())
        mdi_claimed = sum(x for k, x in mdi_by_month.items() if months[k][3])
        per_month[base] = {k: (v[4], v[5], mdi_by_month[k] if v[3] else 0.0) for k, v in months.items()}
        levers["mdi"] = round(mdi, 2)
        out["baselines"][base] = {"saving_rs": round(total + mdi, 2), "claimed_rs": round(claimed + mdi_claimed, 2),
                                  "levers": {k: round(v, 2) for k, v in levers.items()}, "litres_saved": round(litres, 1), "verified_days": verified,
                                  "perfect_foresight_rs": round(bound, 2) if base == "native" else None,
                                  "mdi_months": {k: {"actual_kw": v[0], "baseline_kw": v[1], "floor_kw": v[2]} for k, v in months.items()},
                                  "unserved_kwh_actual": round(unserved_a, 1), "unserved_kwh_baseline": round(unserved_b, 1),
                                  "genset_unknown_days": gen_unknown_days}
    for day, by in sorted(days.items()):
        n = by.get("native", {})
        out["days"].append({"day": day, "saving_native_rs": n.get("saving_rs"), "saving_tuned_rs": (by.get("tuned") or {}).get("saving_rs"),
                            "fidelity": n.get("fidelity"), "actual_cost_rs": (n.get("actual") or {}).get("cost_rs"),
                            "native_cost_rs": (n.get("baseline") or {}).get("cost_rs"), "litres_actual": (n.get("actual") or {}).get("litres"),
                            "litres_native": (n.get("baseline") or {}).get("litres"), "peak_kw": (n.get("actual") or {}).get("peak_kw"),
                            "peak_import_kwh": (n.get("actual") or {}).get("peak_import_kwh"), "levers": n.get("levers")})
    verified = out["baselines"].get("native", {}).get("verified_days", 0)
    # plan 8.5: "Preliminary" until 8 weeks of switchback have run and the FCA of every month in the period has been billed
    today_l = datetime.now(PKT).date()
    sb_days = sum((date.fromisoformat(b["block_end"]) - date.fromisoformat(b["block_start"])).days + 1 for b in switchback_blocks(site_id)
                  if date.fromisoformat(b["block_end"]) < today_l)
    conns = sites.connections(site_id) if site else []
    on_grid = bool(conns) and conns[0]["data"].get("regime") != "off_grid"
    fca_billed = (not on_grid) or all(any(a["key"] == "fca" for a in tariffs.adders_for(mk)) for mk in _months_between(d0, d1))
    out["preliminary"] = (verified < VERIFIED_DAYS_FOR_FINAL or not native_capture or sb_days < 7 * SWITCHBACK_WEEKS_FOR_FINAL or not fca_billed)
    out["final_conditions"] = {"verified_days": verified, "verified_days_needed": VERIFIED_DAYS_FOR_FINAL, "native_capture": native_capture,
                               "switchback_weeks": round(sb_days / 7, 1), "switchback_weeks_needed": SWITCHBACK_WEEKS_FOR_FINAL, "fca_billed": fca_billed}
    out["blocked"] = None if native_capture else "Capture the inverter's native settings first: the baseline is what the inverter did on its own"
    out["method"] = ("Counterfactual replay at 1-minute resolution against the native capture and a tuned table, priced on the bearable-cost basis "
                     "(recoverable GST and adjustable s.235 excluded), with the dated fuel price and the same wear charge on both arms")
    if see_fee and site:
        fee = float(store.get_setting(f"fee:{site_id}", 0) or 0)
        # each month's claimed rate scaled to the whole calendar month (a part month is not a month), plus its MDI lever; the smaller of the two
        # baselines (plan 8.1: claims must beat the tuned baseline); the months averaged
        monthly = []
        for mk in sorted({d[:7] for d in days}):
            dim = calendar.monthrange(int(mk[:4]), int(mk[5:7]))[1]
            cands = [c / n * dim + mdi_c for (c, n, mdi_c) in (per_month.get(base, {}).get(mk, (0.0, 0, 0.0)) for base in ("native", "tuned")) if n]
            if cands:
                monthly.append(min(cands))
        saving_pm = sum(monthly) / len(monthly) if monthly else 0.0
        out["fee"] = {"fee_rs_per_month": fee, "verified_saving_rs_per_month": round(saving_pm, 0),
                      "within_one_third": fee <= saving_pm / 3 if fee else None,
                      "basis": "claimed saving per scored day x days in the month, plus the month's MDI lever; the smaller of the native and tuned baselines"}
    return out


# ------------------------------------------------------------------------------------------------ switchback (Part 8.2)

def plan_switchback(site_id: str, start: date, fortnights: int, block_days: int, by: str, seed: int | None = None) -> list[dict]:
    """Paint the calendar: a coin flip per fortnight. Refused once the pilot has locked it (a repaint would re-roll or overlap the signed-off blocks)."""
    rng = np.random.default_rng(seed if seed is not None else int(datetime.now().timestamp()))
    rows = []
    with store.connect() as con:
        if con.execute("SELECT 1 FROM switchback WHERE site_id = ? AND locked = 1 LIMIT 1", (site_id,)).fetchone():
            raise CalendarLocked()
        con.execute("DELETE FROM switchback WHERE site_id = ? AND locked = 0", (site_id,))
        d = start
        for _ in range(fortnights):
            first = "plan" if rng.random() < 0.5 else "native"
            for arm in (first, "native" if first == "plan" else "plan"):
                end = d + timedelta(days=block_days - 1)
                con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,0)",
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
        r = con.execute("SELECT arm FROM switchback WHERE site_id = ? AND block_start <= ? AND block_end >= ? ORDER BY locked DESC, block_start DESC",
                        (site_id, day.isoformat(), day.isoformat())).fetchone()
    return r["arm"] if r else None


def _carry_over_credits(blocks: list[dict], by_day: dict[str, dict]) -> list[dict]:
    """Plan 8.2: at each plan-to-native boundary the plan block hands its stored energy to the native block; the plan arm is credited with
    (E_end_plan - E_end_native) x v_end, where E_end_native is the native replay's end energy on the plan block's last day. A native block's last day has
    no plan replay to compare with, so native-to-plan boundaries carry no credit."""
    out = []
    for a, b in zip(blocks, blocks[1:]):
        if a["arm"] != "plan" or b["arm"] != "native":
            continue
        if date.fromisoformat(a["block_end"]) + timedelta(days=1) != date.fromisoformat(b["block_start"]):
            continue
        r = by_day.get(a["block_end"])
        if not r:
            continue
        e_a, e_b = float(r["actual"].get("e_end_kwh") or 0.0), float(r["baseline"].get("e_end_kwh") or 0.0)
        v = float(r["actual"].get("v_end") or 0.0)
        out.append({"boundary": b["block_start"], "from_arm": "plan", "to_arm": "native", "e_end_plan_kwh": round(e_a, 2), "e_end_native_kwh": round(e_b, 2),
                    "v_end": round(v, 2), "credit_rs": round((e_a - e_b) * v, 2)})
    return out


def _curtailed_by_day(site_id: str, d0: str, d1: str) -> dict[str, float]:
    """Curtailed PV per local day (kWh), so the regression's PV covariate is the PV available, which neither arm changes."""
    try:
        t0 = twin.day_start_utc(date.fromisoformat(d0))
        t1 = twin.day_start_utc(date.fromisoformat(d1)) + 86400
        s = telemetry.series(site_id, t0, t1, ["curtail_w"], "1d")
    except Exception:
        return {}
    return {datetime.fromtimestamp(int(b), PKT).date().isoformat(): float(v or 0.0) * 24 / 1000 for b, v in zip(s["ts"], s["series"]["curtail_w"])}


def switchback_result(site_id: str) -> dict:
    """Mean daily cost on plan blocks against native blocks, unadjusted and adjusted by a regression on PV kWh (available), load kWh (served plus
    unserved) and outage hours, with the carry-over credit of every plan-to-native boundary added to the plan's saving and shown beside it."""
    blocks = switchback_blocks(site_id)
    if not blocks:
        return {"blocks": [], "result": None}
    d0, d1 = blocks[0]["block_start"], max(b["block_end"] for b in blocks)
    tdays = {d["day"]: d for d in telemetry.days(site_id, d0, d1)}
    curtailed = _curtailed_by_day(site_id, d0, d1)
    with store.connect() as con:
        rows = con.execute("SELECT day_local, data FROM savings_days WHERE site_id = ? AND baseline = 'native' AND day_local >= ? AND day_local <= ?",
                           (site_id, d0, d1)).fetchall()
    by_day = {r["day_local"]: json.loads(r["data"]) for r in rows}
    credits = _carry_over_credits(blocks, by_day)
    X, y, arms = [], [], []
    for day in sorted(by_day):
        arm = next((b["arm"] for b in sorted(blocks, key=lambda b: (-int(b.get("locked") or 0), b["block_start"]))
                    if b["block_start"] <= day <= b["block_end"]), None)
        t = tdays.get(day)
        if arm is None or t is None:
            continue
        pv = float(t.get("pv_kwh") or 0.0) + curtailed.get(day, 0.0)
        load = float(t.get("load_kwh") or 0.0) + float(t.get("unserved_kwh") or 0.0)
        X.append([1.0, 1.0 if arm == "plan" else 0.0, pv, load, (t.get("outage_minutes") or 0) / 60])
        y.append(by_day[day]["actual"]["cost_rs"])
        arms.append(arm)
    if len(set(arms)) < 2 or len(y) < 6:
        return {"blocks": blocks, "result": None, "days": len(y), "carry_over_credits": credits}
    X, y = np.array(X), np.array(y)
    a = np.array(arms)
    unadj = float(np.mean(y[a == "native"]) - np.mean(y[a == "plan"]))
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    adj = float(-coef[1])
    plan_days = int(np.sum(a == "plan"))
    credit = float(sum(c["credit_rs"] for c in credits))
    per_day = credit / plan_days if plan_days else 0.0
    return {"blocks": blocks, "days": len(y), "carry_over_credits": credits,
            "result": {"saving_rs_per_day_unadjusted": round(unadj + per_day, 2), "saving_rs_per_day_adjusted": round(adj + per_day, 2),
                       "plan_days": plan_days, "native_days": int(np.sum(a == "native")), "carry_over_credit_rs": round(credit, 2),
                       "carry_over_rs_per_plan_day": round(per_day, 2), "saving_rs_per_day_unadjusted_before_carry_over": round(unadj, 2),
                       "saving_rs_per_day_adjusted_before_carry_over": round(adj, 2)}}


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
