"""Savings verification (plan Part 8): the counterfactual replay against two baselines, the ledger by lever, switchback, the bill check and the fee check.

The actual arm is the measured day; the baseline arms are the native capture and the tuned table replayed by the twin on the measured PV, load and
outages. Each baseline's replays are chained: a day starts from that baseline's own end state of the day before (its state of charge and its genset's
run, as the twin carries them), and from the actual arm's state only on the first scored day, after a day that was not scored, after a day that
claims nothing and whose replay ran over unread load or PV, and on the first day of a switchback block that changes arm (_chain_starts). Every arm is priced at 1-minute resolution with the same tariff version, on the connection's
bearable-cost basis, with the dated fuel price and the same wear charge; the gap in stored energy between the actual arm and the baseline is valued at
the terminal value, and a day's carry-over is the change of that valued gap over the day, so the days of a chain add up to one continuous replay with
its end-of-period energy term (carry-over priced, not removed). The perfect-foresight bound is a hindsight re-solve, labelled not achievable.
"""
from __future__ import annotations

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


def _billing_bounds(conn: dict | None, day: date) -> tuple[int, int]:
    """The UTC bounds [start, end) of the bill a day falls in: the reading interval when the connection has a `reading_day`, else the calendar month."""
    p0, p1 = tariffs.billing_period_for(conn, day)
    return twin.day_start_utc(p0), twin.day_start_utc(p1) + 86400


def month_totals(site_id: str, conn: dict, day: date, now: int | None = None) -> dict:
    """The billing month's grid imports and exports per period (kWh): {"import": {"peak", "off_peak"}, "export": {...}}. The billing month is the
    reading interval the day is billed in (tariffs.billing_period_for: the calendar month without a reading day). Once it is over these are the
    realised totals; before then (and over telemetry gaps) its metered minutes scaled up to the whole billing month, period by period."""
    t_ms, t_nx = _billing_bounds(conn, day)
    now = int(datetime.now(timezone.utc).timestamp()) if now is None else int(now)
    end = min(t_nx, max(now // 60 * 60, twin.day_start_utc(day) + 86400))
    g = telemetry.minute_grid(site_id, t_ms, end)
    pk = plans.peak_mask(conn, g["ts"])
    pk_all = plans.peak_mask(conn, np.arange(t_ms, t_nx, 60, dtype=np.int64))
    out: dict = {}
    for flow, col in (("import", "imp"), ("export", "exp")):
        a = g[col]
        known = ~np.isnan(a)
        out[flow] = {}
        for p, sel, sel_all in (("peak", pk, pk_all), ("off_peak", ~pk, ~pk_all)):
            n = int(np.sum(known & sel))
            kwh = float(np.sum(a[known & sel]) / 60)
            out[flow][p] = kwh * float(np.sum(sel_all)) / n if n else 0.0
    return out


def month_imports(site_id: str, conn: dict, day: date, now: int | None = None) -> dict:
    """The billing month's grid imports per period (kWh), which settle the month's ICP case for a day scored in hindsight (see month_totals)."""
    return month_totals(site_id, conn, day, now)["import"]


def _legacy(conn: dict | None) -> bool:
    return bool(conn) and conn["data"].get("regime") == "net_metering_legacy"


def _minute_prices(conn: dict | None, day: date, site_id: str, month_q: dict | None = None, month_x: dict | None = None) -> dict:
    t0 = twin.day_start_utc(day)
    if conn is None or conn["data"].get("regime") == "off_grid":
        z = np.zeros(1440)
        return {"c_imp": z, "c_exp": z, "peak": np.zeros(1440, dtype=bool), "mdi_rate": 0.0, "mdi_floor": 0.0, "mdi_interval": 30, "icp": {}}
    if month_q is None or (month_x is None and _legacy(conn)):
        tot = month_totals(site_id, conn, day)
        month_q = tot["import"] if month_q is None else month_q
        month_x = tot["export"] if month_x is None else month_x
    # no swing: the month's own totals settle each period's ICP case, so an above-baseline period prices every minute at the ICP rate (plus the positive
    # FCA), as the bill engine charges the increment; the optimiser's binary for the uncertain middle does not exist in a replay. Legacy net metering
    # nets each period's exports first, so its exports decide both the ICP case and whether a period ends the month in surplus
    vec = tariffs.vectors(conn, datetime.fromtimestamp(t0, timezone.utc), 96, month_q, None, 0.0, month_x if _legacy(conn) else None, None)
    c_exp = np.asarray(vec["c_exp"], dtype=float)
    if _legacy(conn):
        # the optimiser's tie-break (an export priced a hair below the import it nets) is not on the bill: the ledger prices exports as the bill does
        c_exp = c_exp + tariffs.LEGACY_EXPORT_TIE_BREAK
    mdi = vec["mdi"] or {}
    iv = int(conn["data"].get("mdi_interval_min", 30) or 30)
    if iv <= 0 or 1440 % iv:
        iv = 30
    return {"c_imp": np.repeat(vec["c_imp"], 15), "c_exp": np.repeat(c_exp, 15), "peak": np.repeat(vec["peak"], 15),
            "mdi_rate": mdi.get("rate_rs_per_kw", 0.0), "mdi_floor": float(mdi.get("floor_kw", 0.0) or 0.0), "mdi_interval": iv,
            "icp": {p: info["case"] for p, info in (vec.get("icp") or {}).items()}}


def _genset_cost(site: dict, sim: dict, day: date, on_before: bool = False) -> tuple[float, float]:
    """The genset's fuel, start, run-hour and O&M cost over the day, and its litres. A run already going before the day's first minute
    (`on_before`) is not a start: a run across midnight is one start, as a continuous replay counts it."""
    g = (site.get("assets") or {}).get("genset")
    if not g:
        return 0.0, 0.0
    price, _ = tariffs.fuel_price(g.get("fuel_kind", "hsd"), day)
    on = np.asarray(sim["gen_on"], dtype=bool)
    starts = int(np.sum(on[1:] & ~on[:-1]) + (1 if len(on) and on[0] and not on_before else 0))
    litres = float(np.sum(sim["fuel"]))
    cost = litres * price + starts * g["c_start_rs"] + on.sum() / 60 * g["c_runhr_rs"] + float(np.sum(sim["gen"]) / 60) * g["c_om_rs_per_kwh"]
    return cost, litres


def price_arm(site: dict, sim: dict, pr: dict, day: date, e_end_kwh: float, v_end: float, gen_before: bool = False) -> dict:
    """An arm's day priced by lever. `gen_before`: the arm's genset was running before the day's first minute (_genset_cost)."""
    b = (site.get("assets") or {}).get("battery")
    imp, exp = sim["imp"], sim["exp"]
    peak = pr["peak"]
    energy_peak = float(np.sum(pr["c_imp"][peak] * imp[peak]) / 60)
    energy_off = float(np.sum(pr["c_imp"][~peak] * imp[~peak]) / 60)
    export_credit = float(np.sum(pr["c_exp"] * exp) / 60)
    gen_rs, litres = _genset_cost(site, sim, day, gen_before)
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


def _drawn_kwh(bat: np.ndarray, plant: twin.Plant) -> float:
    """The stored energy (kWh) measured battery flows take out (discharge positive, charge negative; blanks count as no flow)."""
    bat = np.nan_to_num(np.asarray(bat, dtype=float))
    return float(np.sum(np.where(bat > 0, bat / plant.eta_d, bat * plant.eta_c)) / 60)


def _soc_start(site_id: str, t0: int, g: dict, f: dict, plant: twin.Plant) -> float | None:
    """The state of charge at the day's start, from the reading nearest midnight: the last reading of the hour before it carried forward with the
    measured flows after it (the previous day's 23:59 reading is the state at 00:00), or the day's first reading with the day's flows up to it backed
    out (a reading is the state after its minute's flow), whichever is fewer minutes from midnight. None when the day has no reading at all."""
    soc = _soc(g)
    idx = np.where(~np.isnan(soc))[0]
    if not idx.size:
        return None
    gp = telemetry.minute_grid(site_id, t0 - 3600, t0)
    prev = _soc(gp)
    pidx = np.where(~np.isnan(prev))[0]
    k_first = int(idx[0])
    if pidx.size and (len(prev) - 1 - int(pidx[-1])) <= k_first + 1:  # minutes of flow to carry: after the last reading, against up to the first
        k = int(pidx[-1])
        if plant.e_nom <= 0:
            return float(prev[k])
        e = float(prev[k]) * plant.e_nom - _drawn_kwh(gp["bat"][k + 1:], plant)
        return float(np.clip(e / plant.e_nom, 0.0, 1.0))
    if plant.e_nom <= 0:
        return float(soc[k_first])
    e = float(soc[k_first]) * plant.e_nom + _drawn_kwh(f["bat"][:k_first + 1], plant)
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
    e = float(soc[k]) * plant.e_nom - _drawn_kwh(f["bat"][k + 1:], plant)
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


def litres_used(site_id: str, d0: str, d1: str) -> dict:
    """The genset's litres over the local days d0..d1 (YYYY-MM-DD, both included) as the actual arm reads them (plan 8.3: metered litres where a
    sensor exists, else the fitted fuel curve), and where they came from: `litres_source` is "meter" when the period has fuel readings and every
    running minute has one, "curve" when any running minute's litres come from the curve (the figure is then estimated, wholly or in part), and None
    when there is nothing to name (no genset, no genset signal at all in the period, or neither a fuel reading nor a run)."""
    out = {"litres": 0.0, "litres_source": None, "litres_metered": 0.0, "litres_curve": 0.0}
    site = sites.full(site_id)
    try:
        t0, t1 = twin.day_start_utc(date.fromisoformat(d0[:10])), twin.day_start_utc(date.fromisoformat(d1[:10])) + 86400
    except (TypeError, ValueError):
        return out
    if site is None or t1 <= t0:
        return out
    plant = twin.plant_of(site)
    has_gen = bool((site.get("assets") or {}).get("genset")) and plant.gen_kw > 0
    metered = curve = 0.0
    signal = read = from_curve_any = False
    for a in range(t0, t1, 31 * 86400):  # a month of minutes at a time
        b = min(t1, a + 31 * 86400)
        d = telemetry.raw(site_id, a, b, ("genset_running", "genset_power_w", "genset_fuel_l"))  # the genset's columns alone: cheap
        flag, p_meas, fuel_meas = d["genset_running"], d["genset_power_w"] / 1000.0, d["genset_fuel_l"]
        metered += float(np.nansum(fuel_meas))
        read = read or bool((~np.isnan(fuel_meas)).any())
        if not has_gen or bool(np.all(np.isnan(flag)) and np.all(np.isnan(p_meas)) and np.all(np.isnan(fuel_meas))):
            continue
        signal = True
        # running as the actual arm reads it: the flag, else the power, else the litres
        on = np.where(~np.isnan(flag), flag > 0.5, np.where(~np.isnan(p_meas), p_meas > 0.0, np.nan_to_num(fuel_meas) > 0.0))
        from_curve = on & np.isnan(fuel_meas)
        if not from_curve.any():
            continue
        from_curve_any = True
        powered = from_curve & ~np.isnan(p_meas)
        curve += float(np.sum((plant.gen_a * p_meas[powered] + plant.gen_b * plant.gen_kw) / 60.0))
        if (from_curve & np.isnan(p_meas)).any():  # running with no power read: its power from the site's balance, as the actual arm takes it
            g = telemetry.minute_grid(site_id, a, b)
            sim = _actual_sim(telemetry.filled(g), g, plant)
            blind = np.asarray(sim["gen_on"], dtype=bool) & np.isnan(g["fuel"]) & np.isnan(g["gen"])
            curve += float(np.sum(np.asarray(sim["fuel"], dtype=float)[blind]))
    out.update(litres=round(metered + curve, 1), litres_metered=round(metered, 1), litres_curve=round(curve, 1))
    if has_gen and signal:
        out["litres_source"] = "curve" if from_curve_any else "meter" if read else None
    return out


#: the most of a day whose grid flow is unresolved (not metered, not an outage, not rebuilt from the balance) while the load or PV is read, before the
#: day claims nothing: where their load is read they price each arm on its own battery and genset flows against it (a blank genset read as off only
#: where it could not have run, else the day is withheld: _gen_from_balance), and
#: where it is unread neither arm's load (only each arm's battery charge that PV does not cover), so battery energy moved across them goes unpriced
GRID_UNRESOLVED_MAX = 0.05
#: the most minutes of unread battery flow between two known states of charge that the change between them is spread over: over a longer span a
#: round trip inside it (a charge then a discharge) is lost and the even spread moves energy between tariff periods, so it stays unresolved
SOC_SPAN_MAX_MIN = 30


def _bat_from_soc(g: dict, plant: twin.Plant | None, need: np.ndarray, soc0: float | None = None,
                  max_span: int | None = SOC_SPAN_MAX_MIN, free: np.ndarray | None = None, price: np.ndarray | None = None) -> np.ndarray:
    """The battery flow (kW, discharge positive) over runs of `need` minutes from the state of charge: a reading is the state after its minute's flow
    (`soc0` the state at 00:00), the state either side of a run comes from the nearest reading with the read flows between carried over, and the change
    between two states is spread evenly over the minutes between them, within the battery's power, when they are at most `max_span` minutes apart
    (None: any span; minutes in `free` are not counted). NaN on the minutes no state brackets, or none brackets closely enough.

    Given the import price per minute (`price`), a change spread over minutes whose price differs is not spread evenly: the states say how much the
    battery gave out or took in between them, not when, and an even spread across a price edge (a discharge that stopped at 17:55 spread over
    17:20-18:30) moves discharge into the peak and credits the arm for peak import it never displaced. A discharge is placed on the cheapest of the
    minutes, a charge on the dearest, each minute up to the battery's power: the least the measured change can be worth, so the claim never rests on a
    timing the data do not show."""
    soc = _soc(g)
    bat = np.asarray(g["bat"], dtype=float)
    n = len(soc)
    out = np.full(n, np.nan)
    if plant is None or plant.e_nom <= 0:
        return out
    known = ~np.isnan(soc)
    start = np.nan if soc0 is None else float(soc0) * plant.e_nom

    def before(i: int) -> float:  # the stored energy at the start of minute i
        r = i - 1
        while r >= 0 and not known[r]:
            if np.isnan(bat[r]):
                return np.nan
            r -= 1
        return (start if r < 0 else float(soc[r]) * plant.e_nom) - _drawn_kwh(bat[r + 1:i], plant)

    def after(j: int) -> float:  # the stored energy at the end of minute j - 1
        r = j
        while r < n and not known[r]:
            if np.isnan(bat[r]):
                return np.nan
            r += 1
        if r >= n or np.isnan(bat[r]):
            return np.nan
        return float(soc[r]) * plant.e_nom + _drawn_kwh(bat[j:r + 1], plant)

    def spread(a: int, b: int, de: float) -> None:
        if max_span is not None and (b - a if free is None else int(np.count_nonzero(~free[a:b]))) > max_span:
            return
        h = (b - a) / 60.0
        p = -de / plant.eta_c / h if de > 0 else -de * plant.eta_d / h
        cap = plant.p_dis if p > 0 else plant.p_ch
        c = None if price is None else np.round(np.asarray(price[a:b], dtype=float), 4)
        if c is None or p == 0.0 or cap <= 0 or float(c.max() - c.min()) < 1e-6:
            out[a:b] = min(max(p, -plant.p_ch), plant.p_dis)
            return
        left, seg = abs(p) * (b - a), np.zeros(b - a)  # kW-minutes to place
        levels = np.unique(c)
        for level in (levels if p > 0 else levels[::-1]):  # a discharge on the cheapest minutes first, a charge on the dearest
            at = c == level
            take = min(left, cap * int(at.sum()))
            seg[at] = take / int(at.sum())
            left -= take
            if left <= 1e-9:
                break
        out[a:b] = np.sign(p) * seg

    i = 0
    while i < n:
        if not need[i]:
            i += 1
            continue
        j = i
        while j < n and need[j]:
            j += 1
        k0, e0 = i - 1, before(i)
        for k in range(i, j):
            if known[k]:
                ek = float(soc[k]) * plant.e_nom
                if not np.isnan(e0):
                    spread(k0 + 1, k + 1, ek - e0)
                k0, e0 = k, ek
        if k0 < j - 1 and not np.isnan(e0):
            e1 = after(j)
            if not np.isnan(e1):
                spread(k0 + 1, j, e1 - e0)
        i = j
    return out


def _gen_known(g: dict, has_gen: bool) -> np.ndarray:
    """The minutes whose genset flow is known: its power read, or a running flag (else its litres) that says it is off, as the actual arm reads its
    state; every minute on a site with no genset."""
    gen_p = np.asarray(g["gen"], dtype=float)
    if not has_gen:
        return np.ones(len(gen_p), dtype=bool)
    flag, fuel = np.asarray(g["gen_on"], dtype=float), np.asarray(g["fuel"], dtype=float)
    off = np.where(~np.isnan(flag), flag <= 0.5, ~np.isnan(fuel) & (np.nan_to_num(fuel) <= 0.0))
    return ~np.isnan(gen_p) | off


def _bat_balance(g: dict, f: dict, has_gen: bool) -> np.ndarray:
    """The battery flow (kW, discharge positive) from the balance the twin keeps (battery = load - PV - genset - import + export) on the minutes whose
    grid flow is known (metered, or an outage: none) and whose load and genset flow are read; NaN elsewhere."""
    metered = ~np.isnan(np.asarray(g["imp"], dtype=float)) | ~np.isnan(np.asarray(g["exp"], dtype=float))
    ok = (metered | (np.asarray(f["grid"], dtype=float) < 0.5)) & ~np.isnan(np.asarray(g["load"], dtype=float)) & _gen_known(g, has_gen)
    bal = f["load"] - f["pv"] - np.nan_to_num(np.asarray(g["gen"], dtype=float)) - np.where(metered, f["imp"] - f["exp"], 0.0)
    return np.where(ok, bal, np.nan)


def _grid_flows(g: dict, f: dict, has_bat: bool, has_gen: bool, plant: twin.Plant | None = None,
                soc0: float | None = None, c_imp: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The actual arm's grid import and export and battery flow (kW) per minute, the minutes rebuilt from the balance, the minutes left unresolved,
    and the rebuilt minutes whose battery flow is spread over a long span (`spanned`).

    A metered minute (either energy column, or the grid power the minute grid derives them from) keeps its reading and an outage minute neither imports
    nor exports. Any other minute whose load is read is rebuilt from the replay's own inputs as the twin balances the baseline (import - export = load -
    PV - battery - genset), where the genset's flow is known (or the site has none) and the battery's is read, or taken from the state of charge around
    a stretch with no battery power (given the plant; the flows on the stretch's metered minutes from their balance, _bat_balance): read as zero, a
    blank import priced the actual arm at no grid cost while the baseline imported the minute's whole load. Where the states either side are more than
    SOC_SPAN_MAX_MIN minutes apart the minute is still rebuilt with the even spread, so every arm's load is priced over it, but it is `spanned`: a round
    trip inside the span is lost, so the caller counts it toward GRID_UNRESOLVED_MAX. What is left is unresolved (the load unread, the genset unknown,
    or no state brackets the battery's flow), and the caller prices neither arm's grid flow over it beyond its battery charge (_grid_masked).

    Given the import price per minute (`c_imp`), a state-of-charge spread across a price edge puts its discharge on the cheapest minutes and its charge
    on the dearest (_bat_from_soc), on short and long spans alike."""
    imp_m, exp_m = np.asarray(g["imp"], dtype=float), np.asarray(g["exp"], dtype=float)
    metered = ~np.isnan(imp_m) | ~np.isnan(exp_m)
    outage = np.asarray(f["grid"], dtype=float) < 0.5
    n = len(imp_m)
    gen_p = np.asarray(g["gen"], dtype=float)
    # an unread load is not a zero load: rebuilt as one, a battery serving it would read as an export
    open_ = ~metered & ~outage & _gen_known(g, has_gen) & ~np.isnan(np.asarray(g["load"], dtype=float))
    bat = np.asarray(f["bat"], dtype=float)
    spanned = np.zeros(n, dtype=bool)
    if has_bat:
        read = ~np.isnan(np.asarray(g["bat"], dtype=float))
        need = open_ & ~read
        if need.any():
            gc = {**g, "bat": np.where(read, np.asarray(g["bat"], dtype=float), _bat_balance(g, f, has_gen))}
            near = _bat_from_soc(gc, plant, need, soc0, price=c_imp)
            far = _bat_from_soc(gc, plant, need, soc0, max_span=None, price=c_imp) if np.isnan(near[need]).any() else near
            spanned = need & np.isnan(near) & ~np.isnan(far)
            bat = np.where(read, bat, np.where(np.isnan(near), far, near))
    rebuilt = open_ & ~np.isnan(bat)
    net = f["load"] - f["pv"] - np.nan_to_num(bat) - np.nan_to_num(gen_p)
    imp = np.where(rebuilt, np.maximum(net, 0.0), f["imp"])
    exp = np.where(rebuilt, np.maximum(-net, 0.0), f["exp"])
    bat = np.where(rebuilt, np.nan_to_num(bat), f["bat"])
    return imp, exp, bat, rebuilt, ~metered & ~outage & ~rebuilt, spanned


def _grid_masked(sim: dict, mask: np.ndarray, pv: np.ndarray | None = None, bat: np.ndarray | None = None,
                 known: np.ndarray | None = None, load: np.ndarray | None = None) -> dict:
    """An arm with its grid flows taken out over the masked minutes (whose actual grid flow is unknown): neither arm's load is priced on them. Given
    the PV available (`pv`, kW), each arm's battery charge that the PV does not cover is still priced as import there: with it taken out too, an arm
    that charges from the grid under a blank meter and spends the energy later is credited for energy it was never charged for.

    `bat` (kW, discharge positive) stands in for the arm's own battery flow on the masked minutes, for that charge and for its wear (the actual arm's,
    taken from its state of charge where its power is blank: read as zero, its charge went unbilled and its discharge unworn while each baseline's was
    priced), and `known` limits both to the minutes where the actual arm's battery flow is known: elsewhere no arm's charge or wear is priced, as no
    arm's load is, since pricing a baseline's alone bills the baseline for what the actual arm may equally have done.

    `load` (kW, NaN where unread) prices the masked minutes whose load is read, where the actual arm's battery flow is known, on each arm's own flows:
    import and export are what the read load leaves over after the arm's PV (`sim["pv"]` where the arm has it, else `pv`), its battery flow and its
    genset output (`sim["gen"]`; on a masked minute whose genset columns are blank the actual arm's set is off, as compute_day reads it only where it
    could not have run and withholds the day elsewhere: _gen_from_balance). The load is
    common to every arm, so what differs is each arm's own battery and genset flow: its discharge as well as its charge. With the charge alone priced,
    a baseline that discharged in the gap while the actual arm held went uncredited for the import its discharge displaced, and then reached the next
    peak emptier, so the day over-claimed (and under-claimed in the mirror case). The minutes whose load is unread keep the charge-only rule."""
    if not mask.any():
        return sim
    flow = np.nan_to_num(np.asarray(sim["bat"] if bat is None else bat, dtype=float))
    ok = np.ones(len(mask), dtype=bool) if known is None else np.asarray(known, dtype=bool)
    imp = 0.0 if pv is None else np.where(ok, np.maximum(-flow - np.asarray(pv, dtype=float), 0.0), 0.0)
    exp = 0.0
    if load is not None:
        load = np.asarray(load, dtype=float)
        read = mask & ok & ~np.isnan(load)
        if read.any():
            pv_arm = np.asarray(sim["pv"] if "pv" in sim else (pv if pv is not None else np.zeros(len(mask))), dtype=float)
            net = np.nan_to_num(load) - pv_arm - flow - np.nan_to_num(np.asarray(sim.get("gen", 0.0), dtype=float))
            imp = np.where(read, np.maximum(net, 0.0), imp)
            exp = np.where(read, np.maximum(-net, 0.0), 0.0)
    out = {**sim, "imp": np.where(mask, imp, sim["imp"]), "exp": np.where(mask, exp, sim["exp"])}
    if bat is not None or known is not None:
        out["bat"] = np.where(mask, np.where(ok, flow, 0.0), sim["bat"])
    return out


def _masked_charge(g: dict, f: dict, bat: np.ndarray, rebuilt: np.ndarray, unresolved: np.ndarray, has_bat: bool, has_gen: bool,
                   plant: twin.Plant | None, soc0: float | None, c_imp: np.ndarray | None = None) -> tuple[np.ndarray | None, np.ndarray]:
    """The actual arm's battery flow (kW, discharge positive) on every minute, and the minutes where it is known (None and every minute when its
    power is read throughout, or the site has no battery): read; else from the balance where the grid flow, load and genset are known (_bat_balance);
    else the flow a rebuilt minute (`rebuilt`, `bat` from _grid_flows) was rebuilt with; else spread from the state of charge with those flows carried
    across, so that what the states either side leave over lands on the minutes nothing else settles. The spread counts only where the states are at
    most SOC_SPAN_MAX_MIN minutes apart not counting the unresolved minutes: there only the energy the battery took in or gave out is priced
    (_grid_masked), which the states settle once the other minutes' flows are known, while over a long span of other minutes the even spread moves
    that energy off the unresolved run. Elsewhere the flow is unknown: on an unresolved minute no arm's charge or wear is priced, and elsewhere the
    actual arm's wear is unknown (compute_day withholds the lever). Given the import price per minute (`c_imp`), a spread across a price edge puts its
    discharge on the cheapest minutes and its charge on the dearest (_bat_from_soc): the masked minutes price this flow against the load minute by
    minute (_grid_masked), so an even spread over a discharge that stopped mid-gap would credit peak import the battery never displaced."""
    n = len(unresolved)
    ones = np.ones(n, dtype=bool)
    if not has_bat:
        return None, ones
    raw = np.asarray(g["bat"], dtype=float)
    read = ~np.isnan(raw)
    if read.all():
        return None, ones
    flow = np.where(read, raw, np.where(rebuilt, np.asarray(bat, dtype=float), _bat_balance(g, f, has_gen)))
    need = np.isnan(flow)
    if need.any():
        flow = np.where(need, _bat_from_soc({**g, "bat": flow}, plant, need, soc0, free=np.asarray(unresolved, dtype=bool), price=c_imp), flow)
    return flow, ~np.isnan(flow)


#: the share of the genset's minimum load the balance must leave over (load - PV - battery - import + export) before a minute whose genset columns are
#: all blank reads as running: a running set carries at least its minimum load, and with it off the balance closes on meter error alone
GEN_BALANCE_ON = 0.5


def _gen_from_balance(g: dict, f: dict, bat: np.ndarray, plant: twin.Plant,
                      read_on: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, bool]:
    """The genset on the minutes whose running flag, power and litres are all blank, from the site's energy balance (HANDOFF 8.A.2).

    Read as off, a blank set that ran (an outage it carried, a parallel run the plan asked for) books none of its fuel on the actual arm while each
    baseline's runs are priced, and the day over-claims by the set's whole bill. Where the grid flow is known (metered, or an outage: none) and the load,
    PV and battery flow (`bat`, NaN where unknown) are known, what the balance leaves over is the set's output: over GEN_BALANCE_ON of its minimum load
    it ran, its output clipped to its range and its litres from the fitted curve, as _actual_sim takes a running flag with blank power.

    Elsewhere the set's state is unknown, and it is read as off only where it could not have run: the grid present, a set that may not run in parallel,
    and not running (`read_on`, or from the balance) either side of the blank stretch. Returns the inferred minutes, their running state, output and
    litres, and whether the set may have run unseen (the caller withholds the day's claim: genset_unknown)."""
    n = len(read_on)
    flag, p_meas, fuel_meas = (np.asarray(g[k], dtype=float) for k in ("gen_on", "gen", "fuel"))
    blank = np.isnan(flag) & np.isnan(p_meas) & np.isnan(fuel_meas)
    zeros = np.zeros(n)
    if plant.gen_kw <= 0 or not blank.any():
        return np.zeros(n, dtype=bool), np.zeros(n, dtype=bool), zeros, zeros, False
    metered = ~np.isnan(np.asarray(g["imp"], dtype=float)) | ~np.isnan(np.asarray(g["exp"], dtype=float))
    outage = np.asarray(f["grid"], dtype=float) < 0.5
    load, pv, bat = np.asarray(g["load"], dtype=float), np.asarray(g["pv"], dtype=float), np.asarray(bat, dtype=float)
    closed = blank & (metered | outage) & ~np.isnan(load) & ~np.isnan(pv) & ~np.isnan(bat)
    balance = np.nan_to_num(load - pv - bat) - np.where(metered, np.asarray(f["imp"], dtype=float) - np.asarray(f["exp"], dtype=float), 0.0)
    s_min = plant.gen_fmin * plant.gen_kw
    on = closed & (balance > GEN_BALANCE_ON * s_min)
    gen = np.where(on, np.clip(balance, s_min, plant.gen_kw), 0.0)
    fuel = np.where(on, (plant.gen_a * gen + plant.gen_b * plant.gen_kw) / 60.0, 0.0)
    open_ = blank & ~closed
    running = np.asarray(read_on, dtype=bool) & ~blank | on
    unseen = bool((open_ & outage).any()) or bool(plant.parallel and (open_ & ~outage).any())
    i = 0
    while not unseen and i < n:
        if not open_[i]:
            i += 1
            continue
        j = i
        while j < n and open_[j]:
            j += 1
        unseen = bool((i > 0 and running[i - 1]) or (j < n and running[j]))
        i = j
    return closed, on, gen, fuel, unseen


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
    month_t: dict = {}
    prepared = []
    for d, series, act in inputs:
        soc0 = _soc_start(site["id"], twin.day_start_utc(d), act["g"], act["f"], plant)
        if soc0 is None:  # a day with no state of charge cannot be replayed fairly
            continue
        mk = tariffs.billing_period_for(conn, d) if on_grid else None
        if on_grid and mk not in month_t:
            month_t[mk] = month_totals(site["id"], conn, d)
        tot = month_t.get(mk) or {}
        prepared.append((d, series, _minute_prices(conn, d, site["id"], tot.get("import"), tot.get("export")), soc0))
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


def night_load_ratio(gen_asset: dict, series: dict) -> float:
    """The genset's load ratio carrying the day's dark minutes (PV under 1 % of the day's peak): its output there is the load, but never under its
    minimum load nor over its rating, and this is the mean of that output over the rating.

    The ledger values a kWh left in an off-grid battery at midnight at the genset's cost per kWh at this ratio: off-grid, the energy left at midnight
    serves the night, where the genset otherwise carries a small load near its minimum and its no-load burn and run-hour cost weigh on every kWh. At
    the demo farm (8-21 kW after dark on a 48 kW set with a 14.4 kW minimum, a ratio of 0.33-0.40) that is Rs 172-184 per kWh against Rs 140-147
    at 75 % load. Against 11-day continuous replays of five rebuilds, the per-day ledger valued at 75 % ran Rs 2,600 to 12,200 ahead of the continuous
    figure in four (an over-claim); valued at this ratio it runs Rs 2,100 behind to Rs 5,700 ahead."""
    S = float(gen_asset.get("rated_kw") or 0.0)
    if S <= 0:
        return 0.75
    pv, load = np.asarray(series["pv"], dtype=float), np.asarray(series["load"], dtype=float)
    dark = pv <= 0.01 * max(float(pv.max()) if len(pv) else 0.0, 1e-9)
    if not dark.any():
        return 0.75
    out = np.clip(load[dark], float(gen_asset.get("f_min", 0.3)) * S, S)
    return float(np.mean(out) / S)


# ------------------------------------------------------------------------------------------------ chained baseline replays (HANDOFF 8.A.1)

#: how many later days a scored day walks to re-score those whose chain no longer starts where the day before ends (_recompute_forward); a day
#: beyond them keeps its old start and the summary lists it in `chain_stale_days` until it is recomputed
FORWARD_MAX_DAYS = 62


def _end_state(sim: dict) -> dict:
    """What the twin carries from a replay's last minute into the next day's replay (twin.simulate's state): the state of charge (0-1, full
    precision) and the genset's run (on, minutes run, minutes into an outage, a rescue run). The twin keeps no tank, so there is none to carry."""
    soc = np.asarray(sim["soc"], dtype=float)
    return {"soc": float(soc[-1]) if len(soc) else 0.0, "gen_on": bool(sim.get("gen_on_end", False)),
            "gen_elapsed": int(sim.get("gen_elapsed_end", 0)), "outage_min": int(sim.get("outage_min_end", 0)),
            "gen_rescue": bool(sim.get("gen_rescue_end", False))}


def _gen_running_before(site_id: str, t0: int) -> bool:
    """Whether the genset was running in the minute before t0, as the actual arm reads its state (the flag, else its power, else its litres);
    False where that minute has no genset reading."""
    d = telemetry.raw(site_id, t0 - 60, t0, ("genset_running", "genset_power_w", "genset_fuel_l"))
    if not len(d["ts"]):
        return False
    flag, p, fuel = (float(d[c][-1]) for c in ("genset_running", "genset_power_w", "genset_fuel_l"))
    if not np.isnan(flag):
        return flag > 0.5
    if not np.isnan(p):
        return p > 0.0
    return bool(np.nan_to_num(fuel) > 0.0)


def _claims(row: dict | None) -> bool:
    """Whether a stored row claims its saving: a claiming fidelity (CLAIMING) and no claim withheld."""
    return bool(row) and row.get("fidelity") in CLAIMING and not row.get("claim_withheld")


def _unread_minutes(g: dict, on_grid: bool) -> int:
    """The minutes of a day whose replay inputs were not read: the load or the PV blank (the replay counts them as zero), or, on a grid
    connection, the grid's presence blank (carried forward, so an outage there is unseen). Native's end state after them is not its own."""
    out = np.isnan(np.asarray(g["load"], dtype=float)) | np.isnan(np.asarray(g["pv"], dtype=float))
    if on_grid:
        out = out | np.isnan(np.asarray(g["grid"], dtype=float))
    return int(np.sum(out))


def _expected_start(prev: dict | None, switch: bool, earlier: bool) -> dict:
    """Where a baseline's replay starts on a day, given that baseline's stored row of the day before (`prev`, None where there is none), whether
    the day opens a switchback block of the other arm (`switch`), and whether any day before it is stored (`earlier`). See _chain_starts."""
    if switch:
        return {"from": "actual", "reset": "switchback"}
    if not prev:
        return {"from": "actual", "reset": "gap" if earlier else "first_day"}
    end = (prev.get("chain") or {}).get("end") or {}
    if not end.get("state"):
        return {"from": "actual", "reset": "no_state"}
    if not _claims(prev) and int(end.get("unread_min") or 0) > 0:
        return {"from": "actual", "reset": "unread_inputs"}
    return {"from": "previous_day", "reset": None, "state": end["state"], "v_rs_per_kwh": float(end.get("v_rs_per_kwh") or 0.0)}


def _chain_starts(site_id: str, day: date) -> dict[str, dict]:
    """Where each baseline's replays start on a day: {"from": "previous_day", "state", "v_rs_per_kwh"} carries that baseline's own end state and
    terminal value from the day before's stored row; {"from": "actual", "reset": why} starts from the actual arm's state. The chain restarts:
    - on the first scored day ("first_day"), after a day with no row ("gap": not scored, or unscoreable), and after a row stored before chaining
      ("no_state");
    - after a day that claims nothing (withheld, or a fidelity outside CLAIMING) whose replay ran over unread minutes ("unread_inputs",
      _unread_minutes): the replay counted a blank load or PV as zero, so its end state is not native's own, and the day's own share of that error
      is not claimed while the next day's would be. Chained on, a 19:00-23:00 dropout on a withheld farm day raised the next two days' claims by
      Rs 1,140 (round 6 verifier, seed 7). The next day's claim then rests on nothing the dropout hid: it is the claim it makes with that day not
      scored. A day that claims nothing on fully read inputs (a monitor site, a table not applied, a day withheld for its grid meter, battery or
      genset alone) keeps the chain: native's replay of it is valid, so the days around it claim what they claim on complete data;
    - on the first day of a switchback block whose arm differs from the day before's ("switchback", plan 8.2): each block's baseline is native from
      the block's measured start, so a plan block's last day carries the hand-over credit at v_end, and a native block, where the inverter ran
      native, scores about zero. A switchback site is held to that segmented replay (DESIGN_LOG, round 6)."""
    prev = day - timedelta(days=1)
    switch = (arm_on(site_id, day) == "native") != (arm_on(site_id, prev) == "native")
    with store.connect() as con:
        rows = {r["baseline"]: json.loads(r["data"]) for r in
                con.execute("SELECT baseline, data FROM savings_days WHERE site_id = ? AND day_local = ?", (site_id, prev.isoformat()))}
        earlier = bool(rows) or con.execute("SELECT 1 FROM savings_days WHERE site_id = ? AND day_local < ? LIMIT 1",
                                            (site_id, prev.isoformat())).fetchone() is not None
    return {base: _expected_start(rows.get(base), switch, earlier) for base in ("native", "tuned")}


def _same_state(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    return (abs(float(a.get("soc", 0.0)) - float(b.get("soc", 0.0))) <= 1e-9 and bool(a.get("gen_on")) == bool(b.get("gen_on"))
            and all(int(a.get(k, 0)) == int(b.get(k, 0)) for k in ("gen_elapsed", "outage_min")) and bool(a.get("gen_rescue")) == bool(b.get("gen_rescue")))


def _start_matches(chain: dict | None, expect: dict) -> bool:
    """Whether a stored row's chain started where the rows before it now say it starts (a row that started from the actual arm's state matches any
    expectation that also starts there: that state is read from telemetry, not from the day before's row, so the reason for the restart does not
    change the figures). A row stored before chaining has no chain: it started from the actual arm's state, the only rule then."""
    chain = chain or {"from": "actual"}
    if chain.get("from") != expect["from"]:
        return False
    if expect["from"] == "actual":
        return True
    s = chain.get("start") or {}
    return _same_state(s.get("state"), expect["state"]) and abs(float(s.get("v_rs_per_kwh") or 0.0) - expect["v_rs_per_kwh"]) <= 1e-9


def _recompute_forward(site_id: str, day: date) -> list[str]:
    """Walk the stored days after `day` in order, up to the first day with no row (the chain restarts there) or FORWARD_MAX_DAYS, and re-score each
    whose chain no longer starts where the day before's row now ends (_start_matches; a day that still matches is only read). A day keeps its
    perfect-foresight bound only where it had one. Returns the days re-scored."""
    done: list[str] = []
    d = day
    for _ in range(FORWARD_MAX_DAYS):
        d = d + timedelta(days=1)
        with store.connect() as con:
            rows = {r["baseline"]: json.loads(r["data"]) for r in
                    con.execute("SELECT baseline, data FROM savings_days WHERE site_id = ? AND day_local = ?", (site_id, d.isoformat()))}
        if not rows:
            break
        expect = _chain_starts(site_id, d)
        if all(_start_matches(rows[b].get("chain"), expect[b]) for b in rows if b in expect):
            continue
        if compute_day(site_id, d, with_bound="perfect_foresight_rs" in (rows.get("native") or {}), forward=False) is None:
            break
        done.append(d.isoformat())
    return done


def compute_day(site_id: str, day: date, with_bound: bool = True, forward: bool = True) -> dict | None:
    """Score a day against the native and the tuned baseline and store its rows (one per baseline).

    Each baseline's replay is chained (_chain_starts): it starts from that baseline's own end state of the day before, so the days of a chain add up
    to one continuous replay; a day after one that claims nothing and ran over unread inputs restarts from the actual arm's state, so a claim never
    rests on what a dropout hid. Its carry-over lever is the change of the valued gap in stored energy over the day: v_end x (actual - baseline end
    energy) less the day before's v_end x (actual - baseline start energy), which is zero where the chain restarts from the actual arm's state. A
    run of the genset across midnight is one start on each arm. The row stores the chain's start and end (`chain`), and with `forward` the stored
    later days whose chain started from this day's old end are re-scored (_recompute_forward)."""
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
        # the actual arm starts and ends where the battery really was at 00:00 and 24:00 (a replay whose chain restarts today starts there too);
        # without a reading the day is unscoreable
        soc0 = _soc_start(site_id, twin.day_start_utc(day), act["g"], act["f"], plant)
        soc_end = _soc_end(act["g"], act["f"], plant)
        if soc0 is None or soc_end is None:
            return None
        e_end_actual = soc_end * plant.e_nom
    pr = _minute_prices(conn, day, site_id)
    gen0 = bool(np.nan_to_num(act["g"]["gen_on"][0]) > 0.5)
    gen_asset = (site.get("assets") or {}).get("genset")
    gen_unknown = bool(gen_asset) and _genset_unknown(act["g"])
    # a blank grid meter read as zero import would let the actual arm claim the baseline's energy bill over the blank minutes: they are rebuilt from the
    # balance where the load is read and the battery's flow (read, or from its state of charge) and the genset's are known; the rest whose load is read
    # price each arm on its own battery and genset flows against that load, and those whose load is unread price neither arm's load (each arm's battery
    # charge that PV does not cover is still import there; _grid_masked). A grid connection whose import is unmetered for most of the
    # day (a source with no grid columns), or unresolved for more than GRID_UNRESOLVED_MAX of it (counting the minutes rebuilt with a battery flow
    # spread over more than SOC_SPAN_MAX_MIN), withholds the grid levers and claims nothing
    on_grid = conn is not None and conn["data"].get("regime") != "off_grid"
    g_imp, g_exp, g_bat, rebuilt, unresolved, spanned = _grid_flows(act["g"], act["f"], bool(b), bool(gen_asset), plant, soc0 if b else None,
                                                                    c_imp=pr["c_imp"])
    if not on_grid:
        rebuilt, unresolved, spanned = np.zeros_like(rebuilt), np.zeros_like(unresolved), np.zeros_like(spanned)
    seen = ~np.isnan(np.asarray(act["g"]["load"], dtype=float)) | ~np.isnan(np.asarray(act["g"]["pv"], dtype=float))
    unresolved_share = float(np.mean((unresolved | spanned) & seen)) if len(seen) else 0.0
    imp_unknown = on_grid and (float(act["g"].get("imp_completeness", 1.0)) < 0.5 or unresolved_share > GRID_UNRESOLVED_MAX)
    f_act = {**act["f"], "imp": g_imp, "exp": g_exp, "bat": g_bat} if on_grid else act["f"]
    actual = _actual_sim(f_act, act["g"], plant if gen_asset else None)
    c_next = float(np.max(pr["c_imp"])) if conn and conn["data"].get("regime") != "off_grid" else 0.0
    if gen_asset and (c_next == 0.0):
        price, _ = tariffs.fuel_price(gen_asset.get("fuel_kind", "hsd"), day)
        c_next = optimise._gen_marginal({"S": gen_asset["rated_kw"], "a": gen_asset["a"], "b": gen_asset["b"], "c_fuel": price,
                                         "c_om": gen_asset["c_om_rs_per_kwh"], "c_runhr": gen_asset["c_runhr_rs"]},
                                        night_load_ratio(gen_asset, series) if not on_grid else 0.75)
    v_end = max(0.0, (b["eta_d"] * c_next - optimise.wear_cost(b)) if b else 0.0)
    # the actual arm's battery flow where its power is blank (_masked_charge): read as zero, its discharge went unworn while each baseline's was worn,
    # and on the masked minutes its charge went unbilled while each baseline's was billed. On the masked minutes each arm's charge beyond PV, and its
    # wear, is priced only where that flow is known; elsewhere it stands in for the actual arm's discharge and wear, and where it is unknown the wear
    # lever is withheld, as the genset lever is
    a_bat, charge_known = _masked_charge(act["g"], act["f"], g_bat, rebuilt, unresolved, bool(b), bool(gen_asset), plant, soc0 if b else None,
                                         c_imp=pr["c_imp"])
    wear_unknown = bool((~unresolved & ~charge_known).any())
    if a_bat is not None:
        actual = {**actual, "bat": np.where(~unresolved & charge_known, a_bat, actual["bat"])}
    # a genset whose columns are all blank on part of a day it is read on: its runs from the balance where that closes, and a day it may have run
    # unseen on (an outage, or a parallel set with the grid up, whose balance cannot be closed) claims nothing (_gen_from_balance)
    gen_unseen = False
    if gen_asset and not gen_unknown and plant.gen_kw > 0:
        raw_bat = np.asarray(act["g"]["bat"], dtype=float)
        bat_flow = np.zeros(len(raw_bat)) if not b else (raw_bat if a_bat is None else np.where(charge_known, a_bat, np.nan))
        inferred, on_i, gen_i, fuel_i, gen_unseen = _gen_from_balance(act["g"], f_act, bat_flow, plant, actual["gen_on"])
        if inferred.any():
            actual = {**actual, "gen_on": np.where(inferred, on_i, actual["gen_on"]), "gen": np.where(inferred, gen_i, actual["gen"]),
                      "fuel": np.where(inferred, fuel_i, actual["fuel"])}
    # on the masked minutes whose load is read, every arm is priced on its own battery and genset flows against that load (_grid_masked)
    load_read = np.asarray(act["g"]["load"], dtype=float)
    masked_actual = _grid_masked({**actual, "pv": np.asarray(act["f"]["pv"], dtype=float)}, unresolved, series["pv"], a_bat, charge_known, load_read)
    masked_actual.pop("pv", None)
    # a run going on at midnight is not a start: the actual arm's state before 00:00 from the minute before it
    gen_before = _gen_running_before(site_id, twin.day_start_utc(day)) if gen_asset else False
    arms = {"actual": price_arm(site, masked_actual, pr, day, e_end_actual, v_end, gen_before=gen_before)}
    # each baseline's replay starts from its own end state of the day before (its chain), or from the actual arm's state where the chain restarts
    starts = _chain_starts(site_id, day)
    policies = {"native": twin.native_policy(site, series["ts"]), "tuned": twin.table_policy(fit_tuned(site, day), series["ts"])}
    e_start_actual = soc0 * plant.e_nom
    # minutes the replays ran over unread inputs: a day after one that claims nothing restarts when there were any (_chain_starts)
    unread = _unread_minutes(act["g"], on_grid)
    sims, chains, befores = {}, {}, {}
    for k, pol in policies.items():
        st = starts[k]
        if st["from"] == "previous_day":
            s = st["state"]
            sim = twin.simulate(series, pol, plant, float(s["soc"]), bool(s["gen_on"]), gen_elapsed0=int(s["gen_elapsed"]),
                                outage_min0=int(s["outage_min"]), gen_rescue0=bool(s["gen_rescue"]))
            on_before, soc_start, v_start = bool(s["gen_on"]), float(s["soc"]), st["v_rs_per_kwh"]
            start_state = dict(s)
        else:
            sim = twin.simulate(series, pol, plant, soc0, gen0)
            on_before, soc_start, v_start = gen_before, soc0, 0.0
            start_state = {"soc": float(soc0), "gen_on": gen0, "gen_elapsed": 0, "outage_min": 0, "gen_rescue": False}
        sims[k], befores[k] = sim, on_before
        e_end_base = float(sim["soc"][-1]) * plant.e_nom
        arms[k] = price_arm(site, _grid_masked(sim, unresolved, series["pv"], known=charge_known, load=load_read), pr, day, e_end_base, v_end,
                            gen_before=on_before)
        gap_start = (e_start_actual - soc_start * plant.e_nom) if st["from"] == "previous_day" else 0.0
        gap_end = e_end_actual - e_end_base
        # the gap in stored energy is valued at the day's v_end at its end, and was valued at the day before's at its start: the days telescope
        chains[k] = {"from": st["from"], "reset": st.get("reset"),
                     "start": {"state": start_state, "baseline_e_kwh": soc_start * plant.e_nom, "actual_e_kwh": e_start_actual, "gap_kwh": gap_start,
                               "v_rs_per_kwh": v_start, "carry_rs": v_start * gap_start},
                     "end": {"state": _end_state(sim), "baseline_e_kwh": e_end_base, "actual_e_kwh": e_end_actual, "gap_kwh": gap_end,
                             "v_rs_per_kwh": v_end, "carry_rs": v_end * gap_end, "unread_min": unread}}
    # fidelity gates the claim (plan 8.4): a monitor site only recommends, even in the demo; closed loop (the demo's twin included) follows the plan by
    # construction; in tier advisory a day counts only once telemetry confirms the keyed table (plan 7.6), never on the mark alone, at a demo site too
    # (audit engine-delivery-18: every demo day was confirmed, marked or not)
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
    elif tier == "closed_loop":
        fidelity = "confirmed"
    elif tier == "advisory_writes":
        if applied is None:
            fidelity = "not_applied"
        else:
            # which table the inverter ran is judged against native from the day's own start, not the chained counterfactual's
            native_today = sims["native"] if starts["native"]["from"] == "actual" else twin.simulate(series, policies["native"], plant, soc0, gen0)
            conf = confirm_applied(site, day, dict(applied), act, plant, native_today, series=series, soc0=soc0)
            fidelity = conf["class"] if conf else "pending"
            fidelity_score = conf["fidelity"] if conf else None
    else:
        fidelity = "recommendation_only"
    rows = {}
    for base in ("native", "tuned"):
        a, n = arms["actual"], arms[base]
        # the change of the valued stored-energy gap over the day (its start is zero where the chain restarts): summed over a chain, the end gap at
        # the last day's v_end, as one continuous replay counts it
        carry = chains[base]["end"]["carry_rs"] - chains[base]["start"]["carry_rs"]
        # a genset with no signal at all is unknown, not off: its lever and litres are withheld rather than booked as the baseline's whole fuel bill;
        # likewise the grid levers of a day whose import is unknown
        grid_lever = lambda x: None if imp_unknown else round(x, 2)  # noqa: E731
        g_unknown = gen_unknown or gen_unseen
        levers = {"peak_window": grid_lever(n["peak_energy_rs"] - a["peak_energy_rs"]), "offpeak_energy": grid_lever(n["offpeak_energy_rs"] - a["offpeak_energy_rs"]),
                  "export": grid_lever(a["export_credit_rs"] - n["export_credit_rs"]),
                  "genset": None if g_unknown else round(n["genset_rs"] - a["genset_rs"], 2),
                  "wear": None if wear_unknown else round(n["wear_rs"] - a["wear_rs"], 2), "carry_over": round(carry, 2)}
        saving = round(sum(v for v in levers.values() if v is not None), 2)
        # a day whose import or whose actual battery flow is unknown claims nothing: withholding only its wear lever would credit the baseline's whole
        # wear when the actual cycled more (the "never over-claim" rule of plan 8.4); likewise a day whose genset may have run unseen, whose energy
        # the grid levers or the carry-over would credit without its fuel
        withheld = ("import_unknown" if imp_unknown else "battery_flow_unknown" if wear_unknown else "genset_unknown" if gen_unseen else None)
        claim = saving if fidelity in CLAIMING and not withheld else 0.0
        rows[base] = {"saving_rs": saving, "claimed_rs": claim, "levers": levers, "actual": a, "baseline": n, "fidelity": fidelity,
                      "fidelity_score": fidelity_score, "litres_saved": None if g_unknown else round(n["litres"] - a["litres"], 2),
                      "genset_unknown": g_unknown, "import_unknown": imp_unknown, "claim_withheld": withheld,
                      "import_completeness": round(float(act["g"].get("imp_completeness", 1.0)), 4),
                      "import_rebuilt_share": round(float(np.mean(rebuilt)), 4) if len(rebuilt) else 0.0,
                      "import_unresolved_share": round(unresolved_share, 4),
                      "peak_kw_actual": a["peak_kw"], "peak_kw_baseline": n["peak_kw"], "mdi_rate": pr["mdi_rate"],
                      "mdi_floor_kw": pr["mdi_floor"], "mdi_interval_min": pr["mdi_interval"], "icp_case": pr["icp"],
                      "native_capture_version": (site.get("native") or {}).get("version"),
                      "unserved_kwh_actual": a["unserved_kwh"], "unserved_kwh_baseline": n["unserved_kwh"], "chain": chains[base]}
    if with_bound and b:
        # the hindsight solve prices every minute: it is held against the native arm priced the same way
        native_cost = (price_arm(site, sims["native"], pr, day, 0.0, 0.0, gen_before=befores["native"])["cost_rs"] if unresolved.any()
                       else arms["native"]["cost_rs"])
        rows["native"]["perfect_foresight_rs"] = _bound(site, day, series, pr, soc0, native_cost,
                                                        e_end_native=float(sims["native"]["soc"][-1]) * plant.e_nom, c_next=c_next, v_end=v_end,
                                                        carry_start=chains["native"]["start"]["carry_rs"])
    now = store._now()
    with store.connect() as con:
        for base, data in rows.items():
            con.execute("INSERT INTO savings_days (site_id, day_local, baseline, data, computed) VALUES (?,?,?,?,?) ON CONFLICT(site_id, day_local, baseline) "
                        "DO UPDATE SET data = excluded.data, computed = excluded.computed", (site_id, day.isoformat(), base, json.dumps(data), now))
    if forward:
        _recompute_forward(site_id, day)
    return rows


def _bound(site: dict, day: date, series: dict, pr: dict, soc0: float, native_cost: float, e_end_native: float | None = None,
           c_next: float = 0.0, v_end: float = 0.0, carry_start: float = 0.0) -> float | None:
    """A hindsight re-solve of the day on the realised PV, load and outages at 15 minutes: the upper bound, not achievable.

    The solve values the energy it leaves in the battery at the day's terminal value and the bound credits the difference from the native arm's end
    energy at the same v_end, as the daily saving does (otherwise the bound earns free energy by draining the battery). The solve starts where the
    actual arm started, so a chained native replay's start gap is charged as the daily saving charges it (`carry_start`, the day before's v_end x
    (actual - native start energy)). The day is solved with its own inputs: overrides, a manual reserve and the tank level active today do not apply
    to a past day."""
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
        return round(native_cost - cost - float(carry_start or 0.0), 2)
    except Exception:
        return None


def _record_confirmation(applied: dict, cls: str, score: float | None, detail: dict) -> None:
    """Write a confirmation class, its score and its detail to the applied row."""
    with store.connect() as con:
        try:
            data = json.loads(applied.get("data") or "{}")
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["confirmation_detail"] = {**detail, "checked": store._now()}
        con.execute("UPDATE applied SET confirmation = ?, fidelity = ?, data = ? WHERE id = ?", (cls, score, json.dumps(data), applied["id"]))


def confirm_applied(site: dict, day: date, applied: dict, act: dict, plant: twin.Plant, native_sim: dict, series: dict | None = None,
                    soc0: float | None = None) -> dict | None:
    """Confirm a table the installer keyed (tier 2) from telemetry alone and score the day's fidelity (plan 7.6, R6 section 4.6); the class and score
    are written to the applied row. None (the mark stays "pending" and claims nothing) when there is no evidence either way: no readable plan, no slot
    on the day, battery power unknown for most of the slots' minutes, or no scorable test while the plan's table and the native one would have run the
    battery alike.

    * Boundaries (grid present): discharge above 10 percent of rated within 30 minutes of a discharge slot's start and below 5 percent within 30 minutes
      of its end; charging while importing above load within 30 minutes of a grid-charge slot's start. Other boundaries are not scored, nor is one the
      table's own replay does not show on the day (no observable change).
    * Targets: in a charge slot that ends on the day and starts below its target, the state of charge reaches the target less 5 points before the slot
      ends, scored where energy was available to do so: the plan's table replayed by the twin on the day's measured PV, load and outages (`series`
      from `soc0`) reaches it (without a replay: the grid was present for the slot and it was long enough at the rated charge power).
    * Floors: the state of charge stays at or above the slot's floor less 3 points while the grid is present.
    * Forbidden: grid charging in a slot without the flag, or discharge in a hold slot, for 5 minutes or more.
    * Energy fidelity: 1 - clip(sum |E_planned - E_measured| / max(sum |E_planned|, 0.1 x usable kWh), 0, 1) over the slots.
    * Not applied (decided first): the measured battery energy per slot is closer to the native replay than to the table's replay, by more than 2
      percent of the battery. Confirmed: at least 80 percent of the scorable boundaries and targets hit (or, with none scorable, the day measurably
      follows the table rather than the native settings), nothing forbidden, no floor breach. Partial otherwise.
    * Fidelity = 0.4 x hit rate (the energy fidelity when nothing is scorable) + 0.3 x (1 - floor breach share) + 0.3 x energy fidelity."""
    plan = plans.get(site["id"], applied["plan_id"]) if applied.get("plan_id") else None
    if plant.e_nom <= 0:
        return None
    if plan is None:
        _record_confirmation(applied, "pending", None, {"reason": "plan_unreadable"})
        return None
    t0 = twin.day_start_utc(day)
    t1 = t0 + 86400
    every = sorted((s for d in ((plan.get("slots") or {}).get("days") or []) for s in (d.get("slots") or [])), key=lambda s: int(s["start_ts"]))
    slots = [s for s in every if int(s["end_ts"]) > t0 and int(s["start_ts"]) < t1]
    if not slots:
        _record_confirmation(applied, "pending", None, {"reason": "no_slots_on_the_day"})
        return None
    g, f = act["g"], act["f"]
    ts = g["ts"]
    bat_raw = np.asarray(g["bat"], dtype=float)
    known = ~np.isnan(bat_raw)
    masks = [(ts >= int(s["start_ts"])) & (ts < int(s["end_ts"])) for s in slots]
    in_slots = np.logical_or.reduce(masks)
    coverage = float(np.mean(known[in_slots])) if in_slots.any() else 0.0
    if coverage < 0.5:  # blanks read as no flow: without the battery's power nothing tells a followed table from an ignored one
        _record_confirmation(applied, "pending", None, {"reason": "no_battery_power", "battery_power_coverage": round(coverage, 3)})
        return None
    soc = _soc(g) * 100.0
    on_grid = np.asarray(f["grid"]) > 0.5
    p_dis, p_ch = max(plant.p_dis, 1e-6), max(plant.p_ch, 1e-6)
    plan_sim = None
    if series is not None and soc0 is not None:
        gen0 = bool(np.nan_to_num(g["gen_on"][0]) > 0.5)
        plan_sim = twin.simulate(series, twin.slots_policy(slots, series["ts"]), plant, float(soc0), gen0)
    def boundary_ok(kind: str, w: np.ndarray, bat: np.ndarray, imp: np.ndarray, load: np.ndarray) -> bool:
        if kind == "discharge_start":
            return bool(np.any(bat[w] > 0.10 * p_dis))
        if kind == "discharge_end":
            return bool(np.any(bat[w] < 0.05 * p_dis))
        return bool(np.any((-bat[w] > 0.10 * p_ch) & (imp[w] > load[w])))

    hits = scorable = 0
    for prev, cur in zip(every, every[1:]):
        bnd = int(cur["start_ts"])
        if not t0 <= bnd < t1:
            continue
        if cur["mode"] == "discharge" and prev["mode"] != "discharge":
            kind = "discharge_start"
        elif prev["mode"] == "discharge" and cur["mode"] != "discharge":
            kind = "discharge_end"
        elif cur["mode"] == "charge_grid" and prev["mode"] != "charge_grid":
            kind = "charge_start"
        else:
            continue
        win = (ts >= bnd) & (ts < bnd + 1800) & on_grid
        w = win & ~np.isnan(bat_raw)
        if not w.any():
            continue
        # a boundary the table itself would not show on this day (a discharge slot that opens on an empty battery, or ends after the battery ran out)
        # has no observable change
        if plan_sim is not None:
            sb = np.asarray(plan_sim["bat"])
            before = (ts >= bnd - 1800) & (ts < bnd)
            if not boundary_ok(kind, win, sb, np.asarray(plan_sim["imp"]), np.asarray(series["load"])) or \
                    (kind == "discharge_end" and not boundary_ok("discharge_start", before, sb, sb, sb)):
                continue
        scorable += 1
        hits += int(boundary_ok(kind, w, bat_raw, f["imp"], f["load"]))
    t_hits = t_scorable = 0
    for s, m0 in zip(slots, masks):
        charge = s["mode"] in ("charge_grid", "charge_pv_only") or bool(s.get("grid_charge"))
        if not charge or int(s["end_ts"]) > t1:
            continue
        sv = soc[m0]
        sv = sv[~np.isnan(sv)]
        target = min(float(s.get("soc_target_pct", 100.0)), plant.soc_max * 100.0) - 5.0
        if not sv.size or float(sv[0]) >= target:
            continue
        if plan_sim is not None:
            available = float(np.max(np.asarray(plan_sim["soc"])[m0])) * 100.0 >= target
        else:
            grid_ch = s["mode"] == "charge_grid" or bool(s.get("grid_charge"))
            hours = float(np.sum(m0)) / 60.0
            need_h = (target - float(sv[0])) / 100.0 * plant.e_nom / max(plant.eta_c, 1e-6) / p_ch
            available = grid_ch and float(np.mean(on_grid[m0])) >= 0.9 and need_h <= hours
        if not available:
            continue
        t_scorable += 1
        t_hits += int(float(sv.max()) >= target)
    steps = plan.get("steps") or []
    st_t = np.array([int(s["t"]) for s in steps], dtype=np.int64)
    st_e = np.array([(float(s.get("discharge_kw") or 0.0) - float(s.get("charge_kw") or 0.0)) * 0.25 for s in steps], dtype=float)
    breaches = n_slots = 0
    forbidden = False
    dev_plan = dev_ref = dev_native = planned_abs = 0.0
    for s, m in zip(slots, masks):
        if not m.any():
            continue
        n_slots += 1
        mg = m & on_grid
        sv = soc[mg]
        sv = sv[~np.isnan(sv)]
        if sv.size and float(sv.min()) < float(s.get("soc_floor_pct", 0.0)) - 3.0:
            breaches += 1
        grid_charging = mg & (f["imp"] > f["load"] + 0.05 * p_ch) & (f["bat"] < -0.05 * p_ch)
        if not s.get("grid_charge") and s["mode"] != "charge_grid" and int(np.sum(grid_charging)) >= 5:
            forbidden = True
        if s["mode"] == "hold" and int(np.sum(mg & (f["bat"] > 0.05 * p_dis))) >= 5:
            forbidden = True
        sel = (st_t >= max(int(s["start_ts"]), t0)) & (st_t < min(int(s["end_ts"]), t1)) if len(st_t) else np.zeros(0, dtype=bool)
        e_plan = float(st_e[sel].sum()) if len(st_t) else 0.0
        e_meas = float(np.sum(f["bat"][m]) / 60)
        dev_plan += abs(e_plan - e_meas)
        planned_abs += abs(e_plan)
        if plan_sim is not None:
            # the table's replay and the native one against the measurement, over the minutes whose battery power is known (a blank is no evidence)
            mk = m & known
            e_meas_k = float(np.sum(bat_raw[mk]) / 60)
            dev_ref += abs(float(np.sum(np.asarray(plan_sim["bat"])[mk]) / 60) - e_meas_k)
            dev_native += abs(float(np.sum(np.asarray(native_sim["bat"])[mk]) / 60) - e_meas_k)
        else:
            dev_ref += abs(e_plan - e_meas)
            dev_native += abs(float(np.sum(np.asarray(native_sim["bat"])[m]) / 60) - e_meas)
    energy_fid = 1.0 - float(np.clip(dev_plan / max(planned_abs, 0.1 * plant.e_nom), 0.0, 1.0))
    tests = scorable + t_scorable
    hit_rate = (hits + t_hits) / tests if tests else None
    score = round(0.4 * (hit_rate if hit_rate is not None else energy_fid) + 0.3 * (1.0 - breaches / max(n_slots, 1)) + 0.3 * energy_fid, 3)
    distinguishable = abs(dev_native - dev_ref) > 0.02 * plant.e_nom
    detail = {"boundaries_hit": hits, "boundaries_scored": scorable, "targets_hit": t_hits, "targets_scored": t_scorable, "floor_breaches": breaches,
              "slots": n_slots, "forbidden": forbidden, "energy_fidelity": round(energy_fid, 3), "deviation_from_plan_kwh": round(dev_ref, 2),
              "deviation_from_native_kwh": round(dev_native, 2), "battery_power_coverage": round(coverage, 3), "replayed": plan_sim is not None}
    if distinguishable and dev_native < dev_ref:
        cls = "not_applied"
    elif not tests and not distinguishable:
        # nothing scorable, and the table would have run the battery as the native settings do: no evidence it was applied
        _record_confirmation(applied, "pending", None, {**detail, "reason": "no_evidence"})
        return None
    elif (hit_rate is None or hit_rate >= 0.8) and not forbidden and breaches == 0:
        cls = "confirmed"
    else:
        cls = "partial"
    _record_confirmation(applied, cls, score, detail)
    return {"class": cls, "fidelity": score, **detail}


def compute_range(site_id: str, d0: date, d1: date) -> int:
    """Score the days d0..d1 in order, each baseline's chain carried from one to the next, then re-score the stored days after d1 whose chain the
    range moved (_recompute_forward). Returns the days d0..d1 scored."""
    n = 0
    d = d0
    while d <= d1:
        if compute_day(site_id, d, forward=False):
            n += 1
        d += timedelta(days=1)
    if d1 >= d0:
        _recompute_forward(site_id, d1)
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


def _arm_in(blocks: list[dict], day: str) -> str | None:
    """arm_on over blocks already read (switchback_blocks): the locked block first, then the latest start."""
    hits = [b for b in blocks if b["block_start"] <= day <= b["block_end"]]
    return max(hits, key=lambda b: (int(b.get("locked") or 0), b["block_start"]))["arm"] if hits else None


def _chain_stale(site_id: str, days: dict[str, dict]) -> list[str]:
    """The days whose stored chain no longer starts where the rows before them now say it starts (_chain_starts' rules on the stored rows): a day
    before them was re-scored without re-scoring them, its row is gone or has come, or its claim or its unread minutes changed (a day after one
    that claims nothing over unread inputs restarts from the actual arm's state). Their figures do not add up with the days before them until they
    are recomputed. A restart at a switchback block is never stale."""
    if not days:
        return []
    first = min(days)
    try:
        before = (date.fromisoformat(first[:10]) - timedelta(days=1)).isoformat()
    except ValueError:
        return []
    with store.connect() as con:
        prev_rows = {r["baseline"]: json.loads(r["data"]) for r in
                     con.execute("SELECT baseline, data FROM savings_days WHERE site_id = ? AND day_local = ?", (site_id, before))}
    blocks = switchback_blocks(site_id)
    stale = []
    for day in sorted(days):
        try:
            prev_day = (date.fromisoformat(day[:10]) - timedelta(days=1)).isoformat()
        except ValueError:
            continue
        prev = days.get(prev_day) or (prev_rows if prev_day == before else {})
        switch = (_arm_in(blocks, day[:10]) == "native") != (_arm_in(blocks, prev_day) == "native")
        # `earlier` only names a restart's reason, and every restart from the actual arm's state gives the same figures
        if any(not _start_matches(row.get("chain"), _expected_start(prev.get(base), switch, earlier=True)) for base, row in days[day].items()):
            stale.append(day)
    return stale


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
    out = {"from": d0, "to": d1, "computed": computed, "days": [], "baselines": {}, "chain_stale_days": _chain_stale(site_id, days)}
    native_capture = sites.native_latest(site_id) is not None
    conns = sites.connections(site_id) if site else []
    on_grid = bool(conns) and conns[0]["data"].get("regime") != "off_grid"
    conn0 = conns[0] if on_grid else None

    def bill_month(day: str) -> str:
        """The bill a day falls in ("YYYY-MM"): the reading interval when the connection has a reading day (its MDI is that interval's highest
        block), else the calendar month."""
        try:
            return tariffs.billing_month_for(conn0, date.fromisoformat(day[:10]))
        except ValueError:
            return day[:7]

    #: per baseline and month: (claimed rupees of the days, days scored, the month's claimed MDI lever), for the fee check
    per_month: dict[str, dict[str, tuple[float, int, float]]] = {}
    for base in ("native", "tuned"):
        levers: dict[str, float] = {}
        total = claimed = litres = 0.0
        verified = 0
        gen_unknown_days = imp_unknown_days = 0
        bound = 0.0
        # per month: actual peak, baseline peak, billing-demand floor, claiming days, claimed rupees, days scored (a day whose import is unknown is not)
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
            imp_unknown = bool(d.get("import_unknown"))
            imp_unknown_days += int(imp_unknown)
            verified += int(d["fidelity"] == "confirmed" and not imp_unknown and not d.get("claim_withheld"))
            bound += d.get("perfect_foresight_rs") or 0.0
            m = months.setdefault(bill_month(day), [0.0, 0.0, 0.0, 0, 0.0, 0])
            if not imp_unknown:  # an unmetered day's actual peak reads zero: it says nothing about the month's MDI on either arm
                m[0] = max(m[0], d["peak_kw_actual"])
                m[1] = max(m[1], d["peak_kw_baseline"])
            m[2] = max(m[2], float(d.get("mdi_floor_kw") or 0.0))
            m[3] += int(d["fidelity"] in CLAIMING and not imp_unknown)
            m[4] += d["claimed_rs"]
            m[5] += int(not imp_unknown and not d.get("claim_withheld"))  # an unknown saving is not a zero saving: the fee's rate is over the days that could claim
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
                                  "genset_unknown_days": gen_unknown_days, "import_unknown_days": imp_unknown_days}
    for day, by in sorted(days.items()):
        n = by.get("native", {})
        out["days"].append({"day": day, "saving_native_rs": n.get("saving_rs"), "saving_tuned_rs": (by.get("tuned") or {}).get("saving_rs"),
                            "fidelity": n.get("fidelity"), "actual_cost_rs": (n.get("actual") or {}).get("cost_rs"),
                            "native_cost_rs": (n.get("baseline") or {}).get("cost_rs"), "litres_actual": (n.get("actual") or {}).get("litres"),
                            "litres_native": (n.get("baseline") or {}).get("litres"), "peak_kw": (n.get("actual") or {}).get("peak_kw"),
                            "peak_import_kwh": (n.get("actual") or {}).get("peak_import_kwh"), "levers": n.get("levers"),
                            "import_unknown": bool(n.get("import_unknown")), "claim_withheld": n.get("claim_withheld"),
                            "baseline_start": (n.get("chain") or {}).get("from")})
    verified = out["baselines"].get("native", {}).get("verified_days", 0)
    # plan 8.5: "Preliminary" until 8 weeks of switchback have run and the FCA of every month in the period has been billed
    today_l = datetime.now(PKT).date()
    sb_days = sum((date.fromisoformat(b["block_end"]) - date.fromisoformat(b["block_start"])).days + 1 for b in switchback_blocks(site_id)
                  if date.fromisoformat(b["block_end"]) < today_l)
    fca_billed = (not on_grid) or all(any(a["key"] == "fca" for a in tariffs.adders_for(mk))
                                      for mk in _months_between(bill_month(d0) + "-01", bill_month(d1) + "-01"))
    out["preliminary"] = (verified < VERIFIED_DAYS_FOR_FINAL or not native_capture or sb_days < 7 * SWITCHBACK_WEEKS_FOR_FINAL or not fca_billed)
    out["final_conditions"] = {"verified_days": verified, "verified_days_needed": VERIFIED_DAYS_FOR_FINAL, "native_capture": native_capture,
                               "switchback_weeks": round(sb_days / 7, 1), "switchback_weeks_needed": SWITCHBACK_WEEKS_FOR_FINAL, "fca_billed": fca_billed}
    out["blocked"] = None if native_capture else "Capture the inverter's native settings first: the baseline is what the inverter did on its own"
    out["method"] = ("Counterfactual replay at 1-minute resolution against the native capture and a tuned table, priced on the bearable-cost basis "
                     "(recoverable GST and adjustable s.235 excluded), with the dated fuel price and the same wear charge on both arms")
    if see_fee and site:
        fee = float(store.get_setting(f"fee:{site_id}", 0) or 0)
        # each month's claimed rate scaled to the whole billing month (a part month is not a month), plus its MDI lever; the smaller of the two
        # baselines (plan 8.1: claims must beat the tuned baseline); the months averaged
        monthly = []
        for mk in sorted({bill_month(d) for d in days}):
            b0, b1 = tariffs.billing_period_for(conn0, date(int(mk[:4]), int(mk[5:7]), 1))  # day 1 is billed in its own month's bill
            dim = (b1 - b0).days + 1
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
        if arm is None or t is None or by_day[day].get("import_unknown"):  # an unmetered day's grid cost reads zero: it is no evidence for either arm
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

def _bill_day(value, what: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"A bill's {what} is a date written YYYY-MM-DD")
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise ValueError(f"A bill's {what} is a date written YYYY-MM-DD") from None


def add_bill(site_id: str, fields: dict, by: str) -> dict:
    """Enter a DISCO bill and rebuild it from telemetry. The period is either the first day billed and the reading date (period_start, period_end,
    both billed), or the two reading dates as printed on the bill: previous_reading_date (not billed: it closed the bill before) and period_end, or
    period_start with period_start_is_reading true. Both forms are stored as the first and last day billed."""
    conns = sites.connections(site_id)
    if not conns:
        raise ValueError("Add the site's grid connection first")
    prev_reading = fields.get("previous_reading_date")
    exclusive = prev_reading not in (None, "") or fields.get("period_start_is_reading") is True
    try:
        start_field = prev_reading if prev_reading not in (None, "") else fields["period_start"]
        p0, p1 = _bill_day(start_field, "start"), _bill_day(fields["period_end"], "reading date")
        total = float(fields["total_rs"])
    except (KeyError, ValueError, TypeError):
        raise ValueError("A bill needs period_start (or previous_reading_date), period_end (YYYY-MM-DD) and total_rs") from None
    first = p0 + timedelta(days=1) if exclusive else p0
    if p1 < first or (p1 - first).days > 40:
        raise ValueError("A billing period runs forwards and is at most 40 days")
    real = {k: fields.get(k) for k in ("total_rs", "units_peak", "units_off_peak", "mdi_kw", "reference") if fields.get(k) not in (None, "")}
    if exclusive:
        real["previous_reading_date"] = p0.isoformat()
    bid = store.new_id()
    rebuilt = rebuild_bill(site_id, conns[0], p0, p1, period_start_exclusive=exclusive)
    diff = round((rebuilt["total_rs"] - total) / total * 100, 2) if total else None
    with store.connect() as con:
        con.execute("INSERT INTO bills (id, site_id, connection_id, period_start, period_end, real, rebuilt, difference_pct, created) VALUES (?,?,?,?,?,?,?,?,?)",
                    (bid, site_id, conns[0]["id"], first.isoformat(), p1.isoformat(), json.dumps(real), json.dumps(rebuilt), diff, store._now()))
    store.audit(by, "entered a DISCO bill", site_id, {"period": f"{first}..{p1}", "difference_pct": diff})
    with store.connect() as con:
        row = con.execute("SELECT * FROM bills WHERE id = ?", (bid,)).fetchone()
    # the stored row as bills() lists it, with the fields this call answered before
    return {**(_bill_row(row) if row else {}), "id": bid, "real": real, "rebuilt": rebuilt, "difference_pct": diff}


def rebuild_bill(site_id: str, conn: dict, p0: date, p1: date, period_start_exclusive: bool = False) -> dict:
    """The connection's bill over [p0, p1] (both billed) rebuilt from telemetry; with period_start_exclusive, p0 is the previous reading date and
    the bill starts the day after it (tariffs.rebuild's convention)."""
    first = p0 + timedelta(days=1) if period_start_exclusive else p0
    t0 = twin.day_start_utc(first)
    t1 = twin.day_start_utc(p1) + 86400
    g = telemetry.minute_grid(site_id, t0, t1)
    f = telemetry.filled(g)
    out = tariffs.rebuild(g["ts"], f["imp"], f["exp"], conn, p0, p1, period_start_exclusive=period_start_exclusive)
    out["completeness"] = round(g["completeness"], 4)
    return out


def _bill_row(r) -> dict:
    return {**dict(r), "real": json.loads(r["real"]), "rebuilt": json.loads(r["rebuilt"])}


def bills(site_id: str) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM bills WHERE site_id = ? ORDER BY period_start DESC", (site_id,)).fetchall()
    return [_bill_row(r) for r in rows]
