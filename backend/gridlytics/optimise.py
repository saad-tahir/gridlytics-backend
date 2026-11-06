"""The optimiser (plan Part 4): one mixed-integer linear programme per site per tick on scipy.optimize.milp (HiGHS).

Structure after evcc-io/optimizer (MIT) and EMHASS (MIT); the genset, outage and demand-charge mathematics after REopt.jl (Apache-2.0) with R3's
corrections. Clean-room: no code is copied; the constructions are named in THIRD_PARTY_NOTICES. Every rupee coefficient is the bill engine's marginal
price for the site and month (tariffs.vectors); every soft term is a rupee the product can explain (VoLL, the expected shortfall, genset maintenance).

`solve(problem)` returns the schedule, the cost breakdown, the status (optimal, feasible or not_solved) and a reason per step in the fixed vocabulary of
Part 4.7. `solve_with_policy` adds the result policy of Part 4.5: one relaxed retry, then failure with a reason.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

H = 0.25
EPS_CURTAIL = 0.05     # Rs/kWh: keep PV unless export is capped and the battery is full
EPS_CHARGE = 0.001     # Rs/kWh: a tie-break against charging and discharging at once
REASONS = ("hold_for_outage", "charge_from_pv", "charge_from_grid", "discharge_peak_window", "mdi_cap", "genset_on", "genset_off", "icp_flat_price",
           "terminal_value", "export_capped", "wear_margin_too_small", "forecast_guard", "operator_hold", "carry_outage", "self_consumption", "idle",
           "unserved")


class Model:
    """A small sparse model builder: named variable blocks, rows as triplets, integrality per column."""

    def __init__(self):
        self.lb, self.ub, self.cost, self.integ = [], [], [], []
        self.blocks: dict[str, np.ndarray] = {}
        self.ri, self.ci, self.vv, self.rlb, self.rub, self.rname = [], [], [], [], [], []

    def var(self, name: str, n: int, lb=0.0, ub=np.inf, cost=0.0, integer: bool = False) -> np.ndarray:
        start = len(self.lb)
        idx = np.arange(start, start + n)
        self.lb.extend(np.broadcast_to(np.asarray(lb, dtype=float), (n,)).tolist())
        self.ub.extend(np.broadcast_to(np.asarray(ub, dtype=float), (n,)).tolist())
        self.cost.extend(np.broadcast_to(np.asarray(cost, dtype=float), (n,)).tolist())
        self.integ.extend([1 if integer else 0] * n)
        self.blocks[name] = idx
        return idx

    def row(self, cols, vals, lb=-np.inf, ub=np.inf, name: str = "") -> None:
        r = len(self.rlb)
        for c, v in zip(cols, vals):
            if v != 0:
                self.ri.append(r)
                self.ci.append(int(c))
                self.vv.append(float(v))
        self.rlb.append(float(lb))
        self.rub.append(float(ub))
        self.rname.append(name)

    def solve(self, time_limit: float, gap: float):
        n = len(self.lb)
        A = coo_matrix((self.vv, (self.ri, self.ci)), shape=(len(self.rlb), n)).tocsr()
        lb, ub = np.array(self.lb), np.array(self.ub)
        ub = np.maximum(ub, lb)
        cons = [LinearConstraint(A, np.array(self.rlb), np.array(self.rub))] if self.rlb else []
        return milp(np.array(self.cost), integrality=np.array(self.integ), bounds=Bounds(lb, ub), constraints=cons,
                    options={"time_limit": time_limit, "mip_rel_gap": gap, "presolve": True, "disp": False}), A


def config_hash(p: dict) -> str:
    shape = {"T": p["T"], "battery": bool(p.get("battery")), "genset": bool(p.get("genset")), "mdi": bool(p.get("mdi")),
             "icp": sorted((k, v["case"]) for k, v in (p.get("icp") or {}).items()), "off_grid": p.get("off_grid", False)}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:12]


def validate(p: dict) -> str | None:
    """The only refusals (Part 4.5): inputs that are impossible."""
    b, g = p.get("battery"), p.get("genset")
    if b and (b["E_nom"] <= 0 or b["P_ch"] <= 0 or b["P_dis"] <= 0):
        return "The battery needs a positive capacity and positive power limits"
    if g and (g["f_min"] > 1 or g["UT"] > p["T"] or g["DT"] > p["T"]):
        return "The genset's minimum load must be at most 1 and its minimum up and down times shorter than the horizon"
    return None


def solve(p: dict, time_limit: float = 10.0, gap: float = 1e-3, relaxed: bool = False) -> dict:
    T = p["T"]
    L, Lhi, Lc, PV, PVlo = (np.asarray(p[k], dtype=float) for k in ("L", "Lhi", "Lc", "PV", "PVlo"))
    g = np.asarray(p["g"], dtype=float)
    off_grid = p.get("off_grid", False)
    c_imp, c_exp = np.asarray(p["c_imp"], dtype=float), np.asarray(p["c_exp"], dtype=float)
    p_start = np.asarray(p["p_start"], dtype=float)
    dur = np.asarray(p["dur_steps"], dtype=int)
    voll = float(p["voll"])
    notes: list[str] = []
    m = Model()
    G = m.var("G", T, 0, g * p["G_max"], H * c_imp)
    X = m.var("X", T, 0, g * p["X_max"], -H * c_exp)
    K = m.var("K", T, 0, PV, H * EPS_CURTAIL)
    bat, gen = p.get("battery"), p.get("genset")
    lmode = int(p.get("load_mode", 0))
    # ---------------------------------------------------------------- genset
    if gen:
        S = gen["S"]
        u_lb, u_ub = np.zeros(T), np.ones(T)
        if not gen["parallel"]:
            u_ub[g > 0.5] = 0
        hold = np.asarray(gen.get("hold", np.zeros(T, dtype=bool)), dtype=bool)
        auto = gen["control"] == "auto_on_grid_loss"
        if auto:
            u_lb[(g < 0.5) & ~hold] = 1
        u_ub[hold] = 0
        if gen["control"] == "none":
            u_ub[:] = 0
        UT, DT = (1, 1) if relaxed else (gen["UT"], gen["DT"])
        if gen["u_prev"] == 1 and gen["elapsed"] < UT:
            for t in range(UT - gen["elapsed"]):
                if t < T and (g[t] < 0.5 or gen["parallel"]):
                    if hold[t] or u_ub[t] == 0:
                        notes.append("genset_state_overridden")
                        break
                    u_lb[t] = 1
        if gen["u_prev"] == 0 and gen["elapsed"] < DT:
            for t in range(min(T, DT - gen["elapsed"])):
                if u_lb[t] < 1:
                    u_ub[t] = 0
        Pg = m.var("P", T, 0, S, H * gen["c_om"])
        F = m.var("F", T, 0, np.inf, gen["c_fuel"])
        u = m.var("u", T, u_lb, u_ub, H * gen["c_runhr"], integer=not relaxed)
        v = m.var("v", T, 0, 1, gen["c_start"], integer=not relaxed)
        for t in range(T):
            m.row([Pg[t], u[t]], [1, -S], ub=0, name=f"gen_max[{t}]")
            m.row([Pg[t], u[t]], [-1, gen["f_min"] * S], ub=0, name=f"gen_min[{t}]")
            m.row([F[t], Pg[t], u[t]], [1, -H * gen["a"], -H * gen["b"] * S], 0, 0, f"fuel[{t}]")
            if t == 0:
                m.row([u[0], v[0]], [1, -1], ub=gen["u_prev"], name="start[0]")
                m.row([v[0]], [1], ub=1 - gen["u_prev"], name="start_off[0]")
            else:
                m.row([u[t], u[t - 1], v[t]], [1, -1, -1], ub=0, name=f"start[{t}]")
                m.row([v[t], u[t - 1]], [1, 1], ub=1, name=f"start_off[{t}]")
            m.row([v[t], u[t]], [1, -1], ub=0, name=f"start_on[{t}]")
            if UT > 1:
                t2 = min(t + UT - 1, T - 1)
                n_ = t2 - t + 1
                m.row(list(u[t:t2 + 1]) + [v[t]], [1] * n_ + [-n_], lb=0, name=f"min_up[{t}]")
            if DT > 1:
                t2 = min(t + DT - 1, T - 1)
                n_ = t2 - t + 1
                cols = list(u[t:t2 + 1])
                vals = [-1.0] * n_
                vals[0] += n_
                if t == 0:
                    m.row(cols, vals, lb=-n_ + n_ * gen["u_prev"], name="min_down[0]")
                else:
                    m.row(cols + [u[t - 1]], vals + [-n_], lb=-n_, name=f"min_down[{t}]")
        if not auto:
            for d in np.unique(gen["day_index"]):
                steps = np.where(gen["day_index"] == d)[0]
                cap = gen["cap_hours"] - (gen["hours_today"] if d == 0 else 0.0)
                if cap < len(steps) * H:
                    m.row(u[steps], [H] * len(steps), ub=max(0.0, cap), name=f"run_cap[{d}]")
            if gen["fuel_available"] < 1e8:
                m.row(F, [1] * T, ub=max(0.0, gen["fuel_available"]), name="fuel_cap")
    # ---------------------------------------------------------------- served load and unserved
    if off_grid:
        served_g0 = L
    else:
        served_g0 = Lc
    base_served = np.where(g > 0.5, L, served_g0)
    u_dep = (g < 0.5) & (lmode == 1) & bool(gen) & (not off_grid)
    U = m.var("U", T, 0, np.where(u_dep, L, base_served), H * voll)
    # ---------------------------------------------------------------- battery
    if bat:
        En, E0 = bat["E_nom"], bat["E0"]
        Emin, Emax = bat["soc_min"] * En, bat["soc_max"] * En
        c_cyc = bat["c_cyc"]
        ch_ub = np.full(T, bat["P_ch"])
        ch_ub[np.asarray(bat.get("charge_block", np.zeros(T, dtype=bool)), dtype=bool)] = 0
        C = m.var("C", T, 0, ch_ub, H * EPS_CHARGE)
        D = m.var("D", T, 0, bat["P_dis"], H * c_cyc)
        e_lb = np.full(T + 1, min(Emin, E0))
        e_ub = np.full(T + 1, max(Emax, E0))
        e_lb[0] = e_ub[0] = E0
        cal = np.concatenate([[0.0], np.asarray(bat.get("b_cal", np.zeros(T)), dtype=float)])
        E = m.var("E", T + 1, e_lb, e_ub, cal)
        for t in range(T):
            m.row([E[t + 1], E[t], C[t], D[t]], [1, -1, -H * bat["eta_c"], H / bat["eta_d"]], 0, 0, f"soc[{t}]")
            # charge sources: PV, and the grid or the genset only where allowed
            cols, vals = [C[t], K[t]], [1, 1]
            if bat["a_grid"]:
                cols.append(G[t])
                vals.append(-1)
            if gen and bat["a_gen"]:
                cols.append(Pg[t])
                vals.append(-1)
            m.row(cols, vals, ub=PV[t], name=f"charge_src[{t}]")
        # reserve (soft, expected value): rows only where the grid is up and an outage may start next
        sig = bat.get("sigma0", 0.0)
        R = np.zeros(T)
        r_idx = {}
        c_short = p["c_short"]
        deficit = np.maximum(0.0, Lc - PVlo) * H / bat["eta_d"]
        for t in range(T):
            d_ = int(dur[t]) if not (gen and gen["control"] == "auto_on_grid_loss") else min(int(dur[t]), 2)
            R[t] = min((bat["soc_max"] - bat["soc_min"]) * En, float(deficit[t + 1:t + 1 + d_].sum())) + 2 * sig * En
        for t in range(T):
            if g[t] > 0.5 and p_start[t] >= 0.01 and R[t] > 0:
                r = m.var(f"r{t}", 1, 0, np.inf, p_start[t] * c_short)[0]
                r_idx[t] = r
                m.row([E[t + 1], r], [1, 1], lb=Emin + R[t], name=f"reserve[{t}]")
        if p.get("reserve_manual"):
            rm = m.var("rm", T, 0, np.inf, voll)
            for t in range(T):
                m.row([E[t + 1], rm[t]], [1, 1], lb=min(Emax, p["reserve_manual"]), name=f"manual_reserve[{t}]")
        # terminal value on the energy the next peak can use
        term = p.get("terminal") or {}
        v_end = max(0.0, bat["eta_d"] * term.get("c_next", 0.0) - c_cyc - term.get("c_cal_hold", 0.0))
        E_use = m.var("E_use", 1, 0, bat["P_dis"] * term.get("H_peak", 4.0) + term.get("R_next", 0.0), -v_end)[0]
        m.row([E_use, E[T]], [1, -1], ub=-Emin, name="terminal")
    # ---------------------------------------------------------------- balance
    for t in range(T):
        cols, vals = [K[t], G[t], X[t], U[t]], [-1, 1, -1, 1]
        rhs = base_served[t] - PV[t]
        if bat:
            cols += [D[t], C[t]]
            vals += [1, -1]
        if gen:
            cols.append(Pg[t])
            vals.append(1)
            if u_dep[t]:
                cols.append(u[t])
                vals.append(-(L[t] - Lc[t]))
                m.row([U[t], u[t]], [1, -(L[t] - Lc[t])], ub=Lc[t], name=f"unserved_cap[{t}]")
        m.row(cols, vals, rhs, rhs, f"balance[{t}]")
    # ---------------------------------------------------------------- MDI
    mdi = p.get("mdi")
    M_idx = []
    if mdi and not off_grid and g.sum() > 0:
        n_blk = mdi["n_mdi"]
        Mv = m.var("M", 1, max(mdi["M0"], mdi["floor"]), np.inf, mdi["rate"])[0]
        M_idx.append(Mv)
        M2 = None
        blocks = (np.asarray(p["ts"]) // (n_blk * 900)).astype(np.int64)
        month_end = mdi.get("month_end_step", T)
        if month_end < T:
            M2 = m.var("M2", 1, mdi["floor"], np.inf, mdi["rate"])[0]
            M_idx.append(M2)
        extra = np.where(g > 0.5, np.maximum(0.0, Lhi - L), 0.0)
        for bid in np.unique(blocks):
            steps = np.where(blocks == bid)[0]
            target = Mv if steps[0] < month_end else M2
            if target is None:
                continue
            elapsed = mdi["G_elapsed"] * mdi["n_elapsed"] if steps[0] == 0 and mdi.get("n_elapsed") else 0.0
            m.row(list(G[steps]) + [target], [1.0] * len(steps) + [-n_blk], ub=-elapsed, name=f"mdi[{bid}]")
            m.row(list(G[steps]) + [target], [1.0] * len(steps) + [-n_blk], ub=-elapsed - float(extra[steps].sum()), name=f"mdi_hi[{bid}]")
    # ---------------------------------------------------------------- ICP, the uncertain middle case
    icp = p.get("icp") or {}
    for per, info in icp.items():
        if info.get("case") != "uncertain":
            continue
        steps = np.where((np.asarray(p["peak"]) == (per == "peak")) & np.asarray(p["in_month"]) & (g > 0.5))[0]
        if not len(steps):
            continue
        Q, base = info["q_kwh"], info["baseline_kwh"]
        Mp = p["G_max"] * len(steps) * H + abs(Q - base) + 1.0
        coef = info["rate"] - float(c_imp[steps[0]])
        Z = m.var(f"Z_{per}", 1, 0, np.inf, coef)[0]
        b = m.var(f"b_{per}", 1, 0, 1, 0.0, integer=not relaxed)[0]
        m.row([Z] + list(G[steps]), [1] + [-H] * len(steps), lb=Q - base, name=f"icp_lo[{per}]")
        m.row([Z, b], [1, -Mp], ub=0, name=f"icp_b[{per}]")
        m.row([Z] + list(G[steps]) + [b], [1] + [-H] * len(steps) + [Mp], ub=Q - base + Mp, name=f"icp_hi[{per}]")
    # ---------------------------------------------------------------- solve
    t_start = time.perf_counter()
    res, A = m.solve(time_limit, gap)
    secs = time.perf_counter() - t_start
    status = "optimal" if res.status == 0 else "feasible" if (res.status == 1 and res.x is not None) else "not_solved"
    out = {"status": status, "solver_status": int(res.status), "message": str(res.message), "solve_seconds": round(secs, 3), "relaxed": relaxed,
           "variables": len(m.lb), "rows": len(m.rlb), "binaries": int(sum(m.integ)), "notes": notes, "config_hash": config_hash(p)}
    if status == "not_solved":
        return out
    x = res.x
    # re-check in plain numpy (Part 4.5)
    resid = A @ x
    bal = [i for i, n in enumerate(m.rname) if n.startswith("balance[")]
    viol = float(np.max(np.abs(resid[bal] - np.array(m.rlb)[bal]))) if bal else 0.0
    ints = np.array(m.integ, dtype=bool)
    frac = float(np.max(np.abs(x[ints] - np.round(x[ints])))) if ints.any() else 0.0
    imp_out = float(np.max(np.where(g < 0.5, x[G], 0.0))) if T else 0.0
    if viol > 1e-4 or frac > 1e-5 or imp_out > 1e-6:
        out["status"] = "not_solved"
        out["message"] = f"Re-check failed: balance {viol:.2e}, integrality {frac:.2e}, import in outage {imp_out:.2e}"
        return out

    def get(name, default=None):
        idx = m.blocks.get(name)
        return x[idx] if idx is not None else (np.zeros(T) if default is None else default)

    sol = {"G": get("G"), "X": get("X"), "K": get("K"), "U": get("U"), "C": get("C"), "D": get("D"), "P": get("P"), "F": get("F"),
           "u": np.round(get("u")), "v": np.round(get("v")), "E": get("E", np.zeros(T + 1))}
    out["objective_rs"] = round(float(res.fun), 2)
    out["sol"] = sol
    out["R"] = R if bat else np.zeros(T)
    out["r"] = {t: float(x[i]) for t, i in r_idx.items()} if bat else {}
    out["M"] = [float(x[i]) for i in M_idx]
    out["v_end"] = v_end if bat else 0.0
    out["breakdown"] = _breakdown(p, sol, out)
    out["reasons"] = explain(p, sol, out)
    return out


def _breakdown(p: dict, s: dict, out: dict) -> dict:
    c_imp, c_exp = np.asarray(p["c_imp"]), np.asarray(p["c_exp"])
    gen, bat = p.get("genset"), p.get("battery")
    energy = float(np.sum(H * (c_imp * s["G"] - c_exp * s["X"])))
    genset = 0.0
    if gen:
        genset = float(np.sum(gen["c_fuel"] * s["F"] + gen["c_start"] * s["v"] + H * gen["c_runhr"] * s["u"] + H * gen["c_om"] * s["P"]))
    wear = float(np.sum(H * bat["c_cyc"] * s["D"])) if bat else 0.0
    mdi = 0.0
    if p.get("mdi") and out.get("M"):
        mdi = float(p["mdi"]["rate"] * (out["M"][0] - max(p["mdi"]["M0"], p["mdi"]["floor"])))
    unserved = float(np.sum(H * p["voll"] * s["U"]))
    reserve = float(sum(np.asarray(p["p_start"])[t] * p["c_short"] * r for t, r in out.get("r", {}).items()))
    terminal = -float(out.get("v_end", 0.0) * max(0.0, s["E"][-1] - (bat["soc_min"] * bat["E_nom"] if bat else 0.0))) if bat else 0.0
    return {"energy_rs": round(energy, 2), "genset_rs": round(genset, 2), "wear_rs": round(wear, 2), "mdi_rise_rs": round(mdi, 2),
            "unserved_rs": round(unserved, 2), "expected_shortfall_rs": round(reserve, 2), "terminal_credit_rs": round(terminal, 2),
            "litres": round(float(np.sum(s["F"])), 2), "import_kwh": round(float(np.sum(s["G"]) * H), 2), "export_kwh": round(float(np.sum(s["X"]) * H), 2),
            "discharge_kwh": round(float(np.sum(s["D"]) * H), 2), "genset_kwh": round(float(np.sum(s["P"]) * H), 2),
            "peak_import_kwh": round(float(np.sum(s["G"][np.asarray(p["peak"], dtype=bool)]) * H), 2)}


def explain(p: dict, s: dict, out: dict) -> list[dict]:
    """The dominant reason per step, from the solution (Part 4.7). Rules are ordered; the first that holds names the step."""
    T = p["T"]
    bat, gen = p.get("battery"), p.get("genset")
    g, peak = np.asarray(p["g"]), np.asarray(p["peak"], dtype=bool)
    PV, L = np.asarray(p["PV"]), np.asarray(p["L"])
    hold = np.asarray(p.get("override_mask", np.zeros(T, dtype=bool)), dtype=bool)
    En = bat["E_nom"] if bat else 1.0
    Emin = bat["soc_min"] * En if bat else 0.0
    R = out.get("R", np.zeros(T))
    icp = p.get("icp") or {}
    thr = p.get("reserve_threshold", 0.2)
    reasons = []
    for t in range(T):
        G, X, K, U, C, D, P = (float(s[k][t]) for k in ("G", "X", "K", "U", "C", "D", "P"))
        E1 = float(s["E"][t + 1]) if bat else 0.0
        d = {}
        if hold[t]:
            r = "operator_hold"
        elif g[t] < 0.5:
            if U > 0.1:
                r = "unserved"
                d = {"kw": round(U, 1)}
            elif P > 0.1:
                r = "genset_on"
                d = {"kw": round(P, 1), "rs_per_kwh": round(_gen_marginal(gen), 1) if gen else None}
            elif D > 0.1:
                r = "carry_outage"
                d = {"kw": round(D, 1)}
            else:
                r = "idle"
        elif K > 0.1 and X >= p["X_max"] - 1e-3:
            r = "export_capped"
            d = {"kw": round(K, 1)}
        elif D > 0.1:
            per = "peak" if peak[t] else "off_peak"
            if peak[t]:
                r = "discharge_peak_window"
                d = {"rs_per_kwh": round(float(p["c_imp"][t]), 2)}
            elif icp.get(per, {}).get("case") == "above":
                r = "icp_flat_price"
            elif p.get("mdi") and G > 0.8 * (out["M"][0] if out.get("M") else 1e9):
                r = "mdi_cap"
                d = {"kw": round(out["M"][0], 1)}
            else:
                r = "self_consumption"
        elif C > 0.1:
            surplus = max(0.0, PV[t] - K - L[t])
            if C - surplus > 0.1 and G > 0.1:
                r = "charge_from_grid"
                d = {"rs_per_kwh": round(float(p["c_imp"][t]), 2)}
            elif gen and P > 0.1:
                r = "genset_on"
            else:
                r = "charge_from_pv"
        elif bat and R[t] > 0 and float(p["p_start"][t]) >= 0.01 and E1 <= Emin + R[t] + 0.02 * En and (peak[t] or float(p["p_start"][t]) >= thr):
            r = "hold_for_outage"
            d = {"p": round(float(p["p_start"][t]), 2), "hours": round(int(p["dur_steps"][t]) * H, 2), "kwh": round(float(R[t]), 1)}
        elif bat and peak[t] and E1 > Emin + 0.02 * En:
            r = "wear_margin_too_small"
        elif bat and t >= T - 24 and E1 > Emin + 0.05 * En:
            r = "terminal_value"
        elif gen and g[t] > 0.5 and float(p["p_start"][t]) > 0.2:
            r = "genset_off"
        else:
            r = "idle"
        reasons.append({"reason": r, "detail": d})
    return reasons


def _gen_marginal(gen: dict, load_ratio: float = 0.75) -> float:
    """The genset's all-in rupees per kWh at a load ratio: fuel, the no-load burn spread over output, run-hour and O&M costs."""
    if not gen:
        return 0.0
    S = gen["S"]
    out_kw = max(1e-6, load_ratio * S)
    return gen["c_fuel"] * (gen["a"] + gen["b"] * S / out_kw) + gen["c_om"] + gen["c_runhr"] / out_kw


def solve_with_policy(p: dict, time_limit: float = 10.0) -> dict:
    """Part 4.5's result policy: solve; on not_solved one relaxed retry on a 24-hour horizon; else failure with the reason."""
    problem = validate(p)
    if problem:
        return {"status": "refused", "message": problem}
    first = solve(p, time_limit=time_limit)
    if first["status"] != "not_solved":
        return first
    short = shorten(p, 96)
    second = solve(short, time_limit=time_limit, relaxed=True)
    second["retry_of"] = first.get("message", "")
    if second["status"] != "not_solved":
        second["notes"] = second.get("notes", []) + ["relaxed_retry"]
    return second


def shorten(p: dict, T: int) -> dict:
    q = dict(p)
    q["T"] = T
    for k in ("L", "Lhi", "Lc", "PV", "PVlo", "g", "c_imp", "c_exp", "p_start", "dur_steps", "peak", "in_month", "ts", "override_mask"):
        if k in q and q[k] is not None:
            q[k] = np.asarray(q[k])[:T]
    if q.get("battery"):
        b = dict(q["battery"])
        for k in ("b_cal", "charge_block"):
            if k in b:
                b[k] = np.asarray(b[k])[:T]
        q["battery"] = b
    if q.get("genset"):
        gg = dict(q["genset"])
        for k in ("hold", "day_index"):
            if k in gg:
                gg[k] = np.asarray(gg[k])[:T]
        gg["UT"], gg["DT"] = min(gg["UT"], T), min(gg["DT"], T)
        q["genset"] = gg
    if q.get("mdi"):
        md = dict(q["mdi"])
        md["month_end_step"] = min(md.get("month_end_step", T), T)
        q["mdi"] = md
    return q


def wear_cost(battery: dict) -> float:
    """Rs per kWh discharged: replacement cost per usable kWh over warranted throughput (Part 4.3, at the warranted depth)."""
    return battery["replacement_cost_per_kwh"] / (battery["warranted_cycles"] * battery["warranted_depth"])


def calendar_slope(temp_c: np.ndarray, replacement_cost_per_kwh: float) -> np.ndarray:
    """BLAST-Lite-style LFP calendar term as Rs per kWh stored per 15-minute step (R6 section 6.4: about 0.034 at 25 C and 0.109 at 45 C at
    Rs 52,000 per kWh), Arrhenius-like between and beyond, scaled by the pack's cost."""
    t = np.asarray(temp_c, dtype=float)
    k = math.log(0.109 / 0.034) / 20.0
    return 0.034 * np.exp(k * (t - 25.0)) * replacement_cost_per_kwh / 52000.0
