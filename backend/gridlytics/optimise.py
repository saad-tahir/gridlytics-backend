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
EPS_ORDER = 0.002      # Rs/kWh across the horizon: of two steps alike, discharge the earlier and charge the later (round 7: ties split by turns
                       # read as 0 / full / 0 / full runs, "Peak discharge" and "Wear > saving" alternating at one price)
PKT_OFFSET_S = 5 * 3600  # Pakistan time (UTC+5, no daylight saving): a step's local day for the curtailment's price (`curtail_cost`)
#: Behind an auto-start (AMF) panel the battery bridges only the panel's start delay (mains-fail confirmation, crank, warm-up and transfer: a minute at
#: most) and, when the set fails to start, the critical load until someone starts it by hand (two steps, the cover the reserve kept before). One
#: start in twenty fails (a set exercised weekly fails 1 to 5 % of its starts; the pessimistic end). Round 7, engine-plans-live-03
AMF_START_DELAY_S = 60
AMF_FAIL_TO_START = 0.05
AMF_FAILED_START_STEPS = 2
SURPLUS_ROWS = True    # solve's grid-down rows on the set's surplus and the battery; a switch only for the tests that check they cut no plan off
REASONS = ("hold_for_outage", "charge_from_pv", "charge_from_grid", "discharge_peak_window", "mdi_cap", "genset_on", "genset_off", "icp_flat_price",
           "terminal_value", "export_capped", "wear_margin_too_small", "forecast_guard", "operator_hold", "carry_outage", "self_consumption", "idle",
           "unserved", "charge_for_reserve", "save_for_later", "carry_load", "cycle_cap")


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


def solve(p: dict, time_limit: float = 10.0, gap: float = 1e-3, relaxed: bool = False, shed_rule: bool = False,
          fix_u: np.ndarray | None = None) -> dict:
    """One solve. `fix_u` (0/1 per step) fixes the genset's on/off schedule, as the threshold repair does (see the end of solve); every other row
    and variable is as without it."""
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
    bat, gen = p.get("battery"), p.get("genset")
    k_cost = curtail_cost(p)
    K = m.var("K", T, 0, PV, k_cost)
    lmode = int(p.get("load_mode", 0))
    W, forced, rm_floor = None, np.zeros(T, dtype=bool), {}
    e_gap_idx, e_gap_price = None, 0.0
    anchor_a, anchor_ends, anchor_short = None, [], None
    cycle_left: dict[int, float] = {}                # the cycle cap's kWh per local day where its row is written (brief-vs-build-04)
    dump_ok = dump_forced = forced
    thr_t: list[int] = []
    thr_w: dict[int, int] = {}
    thr_z: dict[int, int] = {}
    gen_surplus = genset_surplus_steps(p)
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
        day_index = np.asarray(gen.get("day_index", np.zeros(T, dtype=int)))[:T]
        if gen["u_prev"] == 1 and gen["elapsed"] < UT:
            # the carried minimum up time: u = 1 for the remaining steps while the grid is down (or parallel is allowed); a grid return ends it
            fix = []
            for t in range(min(T, UT - gen["elapsed"])):
                if not (g[t] < 0.5 or gen["parallel"]):
                    break
                if hold[t] or u_ub[t] == 0:
                    notes.append("genset_state_overridden")
                    break
                fix.append(t)
            # a run-hour cap or a fuel cap that cannot cover the fixing wins and the fixing is dropped (Part 4.3)
            if fix and not auto:
                fx = np.asarray(fix)
                per_day = {int(d): int(np.sum(day_index[fx] == d)) for d in np.unique(day_index[fx])}
                cap_short = any(n_ * H > max(0.0, gen["cap_hours"] - (gen["hours_today"] if d == 0 else 0.0)) + 1e-9 for d, n_ in per_day.items())
                fuel_short = gen["fuel_available"] < len(fix) * H * (gen["a"] * gen["f_min"] * S + gen["b"] * S) - 1e-9
                if cap_short or fuel_short:
                    notes.append("genset_state_overridden")
                    fix = []
            u_lb[fix] = 1
        if gen["u_prev"] == 0 and gen["elapsed"] < DT:
            for t in range(min(T, DT - gen["elapsed"])):
                if u_lb[t] < 1:
                    u_ub[t] = 0
        # a soc_threshold controller starts the set whenever the state of charge is under its threshold in an outage (Part 4.1: a row on E), and
        # only then. A start it forces is not the plan's to refuse: like a panel start at an auto_on_grid_loss site, the run-hour cap and the
        # minimum up and down rows do not bind it (w[t] = 1 marks a step run because the threshold forces it: the set is on and E[t] is under the
        # threshold), and the row is not written where the set cannot run at all (a hold, a carried down time, a tank that cannot cover one step
        # at minimum load)
        E_on = thr_big = min_burn = 0.0
        if gen["control"] == "soc_threshold" and bat:
            En_ = bat["E_nom"]
            E_on = min(bat["soc_max"] * En_, float(gen.get("soc_on", 0.25)) * En_)
            thr_big = E_on - min(bat["soc_min"] * En_, bat["E0"])
            min_burn = H * (gen["a"] * gen["f_min"] * S + gen["b"] * S)
            if thr_big > 0 and gen["fuel_available"] >= min_burn - 1e-9:
                thr_t = [t for t in range(1, T) if g[t] < 0.5 and u_ub[t] > 0]
        if thr_t:
            wv = m.var("w_thr", len(thr_t), 0, 1, 0.0, integer=True)
            thr_w = {t: int(wv[k]) for k, t in enumerate(thr_t)}
        # where the panel or the carried state forces the set on, it runs at least at its minimum load; what the load and the battery cannot
        # take is dumped (the twin curtails it the same way), so a forced state is never infeasible (Part 4.3); a threshold start likewise
        forced = u_lb >= 1
        dump_ok = forced.copy()
        dump_ok[thr_t] = True
        dump_forced = dump_ok.copy()
        # and with the grid down a run of the plan's own dumps what the load and the battery cannot take, explicitly (round 7,
        # engine-plans-live-15): the battery never charges and discharges at once (the discharge_net rows below), which a plan used to burn the
        # set's minimum-load surplus in the battery's round trip (the farm: 14.4 kW in, 7.8 to 10.9 kW out, at 83 to 95 %), and a minimum up time
        # can outlast the room a nearly full battery has (on site the set then runs under its minimum, as in the twin). Only while it runs
        dump_run = (g < 0.5) & (u_ub > 0) & ~dump_ok
        dump_ok = dump_ok | dump_run
        # with the grid down the set's output follows the load, never under its minimum (twin.simulate's outage branch): its output is capped at
        # max(f_min S, served load - PV), so the battery takes the set's output only up to its minimum-load surplus, never at full power (HANDOFF
        # 8.A.1, round 5). On every grid-down step whatever the charging flag (round 7, engine-optimiser-06: with gen_charge_allowed off the cap
        # was off too, and the plan ran the set at the whole load while PV charged the battery, genset charging in net terms the site's inverter
        # never does); only a brand that can be told to charge from the set at full power (a_gen) runs it uncapped. Whether that surplus may
        # reach the battery is the flag's (`genset_surplus_steps`, and the charge_pv_surplus rows below)
        follow = (g < 0.5) & (not (bat and bat.get("a_gen")))
        P_on = np.where(follow, genset_follow_cap(p), S)
        Pg = m.var("P", T, 0, P_on, H * gen["c_om"])
        F = m.var("F", T, 0, np.inf, gen["c_fuel"])
        if fix_u is not None:
            # the threshold repair's schedule, fixed only here: the threshold steps, the forced dumps and the panel's free starts above are read
            # from the site's own bounds, so every row is the unfixed problem's
            fx_u = np.clip(np.round(np.asarray(fix_u, dtype=float)[:T]), u_lb, u_ub)
            u = m.var("u", T, fx_u, fx_u, H * gen["c_runhr"], integer=True)
        else:
            u = m.var("u", T, u_lb, u_ub, H * gen["c_runhr"], integer=True)
        v = m.var("v", T, 0, 1, gen["c_start"], integer=True)
        # a dump costs more than a curtailment at the same step (curtail PV first, then dump: twin.simulate's order; `curtail_cost`)
        W = m.var("W", T, 0, np.where(dump_ok, gen["f_min"] * S, 0.0), k_cost + H * EPS_CURTAIL) if dump_ok.any() else None
        for t in np.flatnonzero(dump_run):
            m.row([W[t], u[t]], [1, -gen["f_min"] * S], ub=0, name=f"dump_on[{t}]")
        # at a panel-started site the panel decides u: minimum up and down rows bind only the starts and stops the plan is free to make
        free_start = ~(auto & (u_lb >= 1))
        free_stop = ~(auto & (u_ub <= 0))
        if fix_u is None:
            # on the horizon's second day (re-planned long before it runs) a run with the grid down, where the set may run under a load below
            # its minimum and store its surplus (`genset_surplus_steps`), starts and stops on the clock's hour marks, for solve time: its quarters
            # are near-ties the solver otherwise has to tell apart. Grid-up steps keep their quarters, and so does the first day (held to hours
            # there, a short outage shed a kWh rather than run another hour). Never on a soc_threshold controller's steps (thr_w), which start
            # the set at any step. Timings with the surplus rows below: see there
            quarter = np.asarray(p["ts"], dtype=np.int64)[:T] // 900 % 4
            for t in range(96, T):
                if (gen_surplus[t] and gen_surplus[t - 1] and quarter[t] and u_lb[t] == u_lb[t - 1] and u_ub[t] == u_ub[t - 1]
                        and t not in thr_w and t - 1 not in thr_w):
                    m.row([u[t], u[t - 1]], [1, -1], 0, 0, name=f"second_day_hour[{t}]")
        for t in range(T):
            m.row([Pg[t], u[t]], [1, -float(P_on[t])], ub=0, name=f"gen_max[{t}]")  # the cap times u: a tighter relaxation
            m.row([Pg[t], u[t]], [-1, gen["f_min"] * S], ub=0, name=f"gen_min[{t}]")
            m.row([F[t], Pg[t], u[t]], [1, -H * gen["a"], -H * gen["b"] * S], 0, 0, f"fuel[{t}]")
            if t == 0:
                m.row([u[0], v[0]], [1, -1], ub=gen["u_prev"], name="start[0]")
                m.row([v[0]], [1], ub=1 - gen["u_prev"], name="start_off[0]")
            else:
                m.row([u[t], u[t - 1], v[t]], [1, -1, -1], ub=0, name=f"start[{t}]")
                m.row([v[t], u[t - 1]], [1, 1], ub=1, name=f"start_off[{t}]")
            m.row([v[t], u[t]], [1, -1], ub=0, name=f"start_on[{t}]")
            if t in thr_w:
                # in an outage the controller, not the plan, starts the set: only when the state of charge is under the threshold
                m.row([thr_w[t], u[t]], [1, -1], ub=0, name=f"thr_on[{t}]")
                m.row([v[t], thr_w[t]], [1, -1], ub=0, name=f"thr_start[{t}]")
            if UT > 1 and free_start[t]:
                t2 = min(t + UT - 1, T - 1)
                n_ = t2 - t + 1
                cols, vals = list(u[t:t2 + 1]) + [v[t]], [1] * n_ + [-n_]
                if t in thr_w:  # a threshold start runs for as long as the controller keeps it on, not the plan's minimum
                    cols.append(thr_w[t])
                    vals.append(n_)
                m.row(cols, vals, lb=0, name=f"min_up[{t}]")
            if DT > 1 and free_stop[t]:
                t2 = min(t + DT - 1, T - 1)
                n_ = t2 - t + 1
                cols = list(u[t:t2 + 1])
                vals = [-1.0] * n_
                vals[0] += n_
                # the threshold restarts a set it needs, however recently it stopped
                restart = [thr_w[k] for k in range(t + 1, t2 + 1) if k in thr_w]
                if t == 0:
                    m.row(cols + restart, vals + [n_] * len(restart), lb=-n_ + n_ * gen["u_prev"], name="min_down[0]")
                else:
                    m.row(cols + [u[t - 1]] + restart, vals + [-n_] + [n_] * len(restart), lb=-n_, name=f"min_down[{t}]")
        if not auto:
            for d in np.unique(gen["day_index"]):
                steps = np.where(gen["day_index"] == d)[0]
                cap = gen["cap_hours"] - (gen["hours_today"] if d == 0 else 0.0)
                if cap < len(steps) * H:
                    exempt = [thr_w[int(s)] for s in steps if int(s) in thr_w]  # threshold runs do not count against the cap
                    m.row(list(u[steps]) + exempt, [H] * len(steps) + [-H] * len(exempt), ub=max(0.0, cap), name=f"run_cap[{d}]")
            if gen["fuel_available"] < 1e8:
                m.row(F, [1] * T, ub=max(0.0, gen["fuel_available"]), name="fuel_cap")
        # the fuel cap stays physical: z[t] = 1 (the threshold row lapses) only once the fuel burnt before t leaves less than one step at minimum load
        thr_z: dict[int, int] = {}
        if thr_t and gen["fuel_available"] < 1e8:
            zv = m.var("z_thr", len(thr_t), 0, 1, 0.0, integer=True)
            need = max(0.0, gen["fuel_available"] - min_burn)
            # the fuel burnt before each step as a running sum: one short row per step, not a row of t entries per threshold step (the
            # dense form slowed a two-day off-grid solve by about half)
            Fb = m.var("F_before", T, 0, np.inf, 0.0)
            m.row([Fb[0]], [1], 0, 0, "fuel_before[0]")
            for t in range(1, T):
                m.row([Fb[t], Fb[t - 1], F[t - 1]], [1, -1, -1], 0, 0, f"fuel_before[{t}]")
            for k, t in enumerate(thr_t):
                thr_z[t] = int(zv[k])
                m.row([Fb[t], thr_z[t]], [1, -need], lb=0, name=f"fuel_out[{t}]")
    # ---------------------------------------------------------------- served load and unserved
    if off_grid:
        served_g0 = L
    else:
        served_g0 = Lc
    base_served = np.where(g > 0.5, L, served_g0)
    u_dep = (g < 0.5) & (lmode == 1) & bool(gen) & (not off_grid)
    U_ub = np.where(u_dep, L, base_served)
    # With the grid up a site cannot shed load on command (no Mode A or Mode B primitive does it; the meter simply imports), so a grid-up step
    # may leave unserved only the load beyond what the connection and PV can carry: never load shed to hold the MDI or dodge a price, which a VoLL
    # under the cost of the alternative made cheap (round 7, engine-optimiser-01: Rs 250 per kW-block against Rs 1,250 per kW of MDI)
    # (PV reaches the AC side only through the inverter: never more than its kW AC, round 7)
    inv_ac = float(p["inverter_kw_ac"]) if p.get("inverter_kw_ac") else None
    pv_ac = np.minimum(PV, inv_ac) if inv_ac is not None else PV
    U_ub = np.where(g > 0.5, np.maximum(0.0, L - float(p["G_max"]) - pv_ac), U_ub)
    # in the threshold repair (the genset fixed, off-grid) a shortfall the tank leaves costs the same VoLL whenever it is shed, so a tie-break
    # (a ten-thousandth of VoLL, falling over the horizon) sheds it as late as it can be: after the battery has carried what it can, as the
    # battery-inverter does on site, not earlier with the battery above its cut-off
    late = (1.0 + 1e-4 * (T - np.arange(T)) / max(T, 1)) if (fix_u is not None and off_grid) else 1.0
    U = m.var("U", T, 0, U_ub, H * voll * late)
    # ---------------------------------------------------------------- battery
    if bat:
        En, E0 = bat["E_nom"], bat["E0"]
        Emin, Emax = bat["soc_min"] * En, bat["soc_max"] * En
        c_cyc = bat["c_cyc"]
        ch_ub = np.full(T, bat["P_ch"])
        ch_ub[np.asarray(bat.get("charge_block", np.zeros(T, dtype=bool)), dtype=bool)] = 0
        # the order tie-break only where the grid is up: grid-down and off-grid steps keep their costs (the battery-inverter follows the load
        # there, and the off-grid threshold solves are timed to the second)
        ramp = np.where(g > 0.5, np.arange(T) / max(T, 1), 0.0)
        C = m.var("C", T, 0, ch_ub, H * (EPS_CHARGE + EPS_ORDER * np.where(g > 0.5, 1.0 - ramp, 0.0)))
        d_ub = np.full(T, float(bat["P_dis"]))
        D = m.var("D", T, 0, d_ub, H * (c_cyc + EPS_ORDER * ramp))
        e_lb = np.full(T + 1, min(Emin, E0))
        e_ub = np.full(T + 1, max(Emax, E0))
        e_lb[0] = e_ub[0] = E0
        # the floor the delivered table and the fallback hold with the grid up (round 7, engine-delivery-02): every Mode A slot's floor is at
        # least max(BMS low + 2, fallback floor) and the edge enforces the fallback floor whatever the slots say, so with the grid up the plan
        # never counts on the energy under it. In an outage the battery-inverter carries the load to its own cut-off (soc_min). A grid-up run
        # that starts above the floor (the horizon's first, from E0) holds it as a bound; one that may start under it (after an outage, or E0
        # under the floor) recovers once, as EMHASS's single-recovery rows do (R2 3.5): until the battery is back at the floor it does not
        # discharge, as the device holds a battery under its slot floor, and from then on the floor binds. `floor_runs` lists those runs
        Elo = Emin
        if bat.get("soc_floor_grid") is not None and not off_grid:
            Elo = min(Emax, max(Emin, float(bat["soc_floor_grid"]) * En))
        floor_runs: list[tuple[int, int]] = []
        if Elo > Emin + 1e-9:
            t = 0
            while t < T:
                if g[t] < 0.5:
                    t += 1
                    continue
                a_ = t
                while t < T and g[t] > 0.5:
                    t += 1
                if a_ == 0 and E0 >= Elo - 1e-9:
                    e_lb[a_ + 1:t + 1] = np.maximum(e_lb[a_ + 1:t + 1], Elo)
                else:
                    floor_runs.append((a_, t))
        cal = np.concatenate([[0.0], np.asarray(bat.get("b_cal", np.zeros(T)), dtype=float)])
        E = m.var("E", T + 1, e_lb, e_ub, cal)
        for t in range(T):
            m.row([E[t + 1], E[t], C[t], D[t]], [1, -1, -H * bat["eta_c"], H / bat["eta_d"]], 0, 0, f"soc[{t}]")
            # charge sources: PV, and the grid or the genset only where allowed (the genset's minimum-load surplus with the grid down: its
            # output is capped there, see the genset block)
            cols, vals = [C[t], K[t]], [1, 1]
            if bat["a_grid"]:
                cols.append(G[t])
                vals.append(-1)
            if gen and (bat["a_gen"] or gen_surplus[t]):
                cols.append(Pg[t])
                vals.append(-1)
            m.row(cols, vals, ub=PV[t], name=f"charge_src[{t}]")
            if not bat["a_grid"] and g[t] > 0.5:
                # no grid charging: with the grid up the load takes PV first, so the battery charges only from the PV beyond the load (and the
                # genset where allowed); otherwise PV would charge while the grid served the load, which is grid charging in net terms
                cols, vals = [C[t], K[t]], [1, 1]
                if gen and bat["a_gen"]:
                    cols.append(Pg[t])
                    vals.append(-1)
                m.row(cols, vals, ub=max(0.0, float(PV[t] - L[t])), name=f"charge_surplus[{t}]")
            if gen and g[t] < 0.5 and not (bat["a_gen"] or gen_surplus[t]):
                # no genset charging, with the grid down: the load takes PV first (the set follows what it leaves), so the battery charges only
                # from the PV beyond the load, never from the set's minimum-load surplus (round 7, engine-optimiser-06). The load served is the
                # critical load with the set off at a grid-connected site, the whole load where the set carries it (load_mode 1) or off-grid
                served_on = float(L[t] if (off_grid or lmode == 1) else Lc[t])
                cap_off, cap_on = max(0.0, float(PV[t] - base_served[t])), max(0.0, float(PV[t]) - served_on)
                m.row([C[t], u[t]], [1, cap_off - cap_on], ub=cap_off, name=f"charge_pv_surplus[{t}]")
            if g[t] < 0.5:
                # with the grid down the battery discharges only into the load PV leaves (with the set off), or what PV and the set's minimum load
                # leave of it (with the set on: the set follows the load first), so it never charges and discharges at once, which an inverter
                # cannot do and the twin does not (round 7, engine-plans-live-15: a plan burnt the set's surplus in the battery's round trip)
                cap_off = max(0.0, float(base_served[t] - PV[t]))
                if gen:
                    served_on = float(L[t] if (off_grid or lmode == 1) else Lc[t])
                    cap_on = max(0.0, served_on - float(PV[t]) - gen["f_min"] * gen["S"])
                    m.row([D[t], u[t]], [1, cap_off - cap_on], ub=cap_off, name=f"discharge_net[{t}]")
                else:
                    m.row([D[t]], [1], ub=cap_off, name=f"discharge_net[{t}]")
        # the single recovery to the grid-up floor (see e_lb above): y = 1 once the battery is back at the floor, monotone over the run, one per
        # clock hour (a crossing within the hour waits for its end, a quarter of the binaries); while y = 0 there is no discharge
        rec_last = None                                  # the recovery binary of the horizon's last step, where it ends in such a run
        if floor_runs:
            hour = np.asarray(p["ts"], dtype=np.int64)[:T] // 3600
            for a_, b_ in floor_runs:
                keys = list(dict.fromkeys(int(h) for h in hour[a_:b_]))
                yv = m.var(f"floor_rec[{a_}]", len(keys), 0, 1, 0.0, integer=True)
                y_of = {k: int(yv[j]) for j, k in enumerate(keys)}
                for j in range(1, len(keys)):
                    m.row([yv[j], yv[j - 1]], [1, -1], lb=0, name=f"floor_rec_order[{a_},{j}]")
                for t in range(a_, b_):
                    y = y_of[int(hour[t])]
                    m.row([D[t], y], [1, -float(bat["P_dis"])], ub=0, name=f"floor_rec_dis[{t}]")
                    lb_ = float(e_lb[t + 1])
                    m.row([E[t + 1], y], [1, -(Elo - lb_)], lb=lb_, name=f"floor_rec_e[{t}]")
                if b_ == T:
                    rec_last = y_of[int(hour[T - 1])]
        # a start under the cut-off itself (E0 < soc_min, after native operation or an outage) recovers once too, as EMHASS's single-recovery rows
        # do (R2 3.5; round 7, engine-optimiser-04): z = 1 once the battery is back at soc_min, monotone over the whole horizon (grid up or down:
        # the BMS keeps a battery under its cut-off from discharging), one per clock hour; while z = 0 there is no discharge, so the state of
        # charge only moves towards the band, and from then on soc_min binds. Before, min(soc_min, E0) bounded every step, so a plan starting
        # at 5 % could discharge to 5 % again in the evening peak, and Mode B was told it may
        if E0 < Emin - 1e-9:
            hour = np.asarray(p["ts"], dtype=np.int64)[:T] // 3600
            keys = list(dict.fromkeys(int(h) for h in hour))
            zv = m.var("cut_rec", len(keys), 0, 1, 0.0, integer=True)
            z_of = {k: int(zv[j]) for j, k in enumerate(keys)}
            for j in range(1, len(keys)):
                m.row([zv[j], zv[j - 1]], [1, -1], lb=0, name=f"cut_rec_order[{j}]")
            for t in range(T):
                z = z_of[int(hour[t])]
                m.row([D[t], z], [1, -float(bat["P_dis"])], ub=0, name=f"cut_rec_dis[{t}]")
                m.row([E[t + 1], z], [1, -(Emin - E0)], lb=E0, name=f"cut_rec_e[{t}]")
        # the anchor the battery service asks for (round 7, engine-optimiser-15; R6 5.7, Part 4.6): with sigma over 15 points, or no anchor in
        # the 30 days it read, one charge to the pack's top of charge, held at the end of one clock hour the solver picks (a = 1 there), so a
        # reading there re-anchors the estimate. The kWh short of it at that hour are priced a rupee above the dearest kWh the horizon could
        # store, as a start under the cut-off is, so the cheapest charge meets it (a sunny day's PV first) and nothing makes it infeasible
        if bat.get("anchor_soc") and T:
            A = min(Emax, float(bat["anchor_soc"]) * En)
            hour = np.asarray(p["ts"], dtype=np.int64)[:T] // 3600
            anchor_ends = [t for t in range(T) if t == T - 1 or hour[t + 1] != hour[t]]
            if bat.get("anchor_by"):
                # by the time an earlier plan fixed for it (plans._ask_anchor), or the first hour's end where none is that soon
                ts_end = np.asarray(p["ts"], dtype=np.int64)[:T] + 900
                anchor_ends = [t for t in anchor_ends if ts_end[t] <= int(bat["anchor_by"])] or anchor_ends[:1]
            anchor_a = m.var("anchor", len(anchor_ends), 0, 1, 0.0, integer=True)
            m.row([int(a) for a in anchor_a], [1.0] * len(anchor_ends), lb=1, ub=1, name="anchor_one")
            dear = float(np.max(np.asarray(p["c_imp"])[:T])) if not off_grid else float(p.get("c_short", voll))
            anchor_short = m.var("anchor_short", 1, 0, A, dear / bat["eta_c"] + 1.0)[0]
            for j, t in enumerate(anchor_ends):
                lb_ = float(e_lb[t + 1])
                m.row([E[t + 1], int(anchor_a[j]), anchor_short], [1, -(A - lb_), 1], lb=lb_, name=f"anchor[{t}]")
        # the site's daily cycle cap (R8 tab D; the deep review's "cap daily cycles"; round 7, brief-vs-build-04): each local day's discharge
        # with the grid up stays under cycle_cap_kwh (cycles a day times E_nom, as the battery page counts a cycle), the first day's less what
        # the battery discharged since midnight. The discharge in an outage is the battery-inverter's, which carries the load whatever the plan
        # says, so the cap never makes the plan shed load to save a cycle; at a site without a grid every step is so and no row is written.
        # A hard row: no grid-up step needs the battery (the grid carries the load), so it never makes a plan infeasible
        if bat.get("cycle_cap_kwh") is not None and not off_grid:
            days = np.asarray(bat.get("day_index", np.zeros(T, dtype=int)))[:T]
            for dd in np.unique(days):
                ks = [int(k) for k in np.flatnonzero(days == dd) if g[k] > 0.5]
                left = max(0.0, float(bat["cycle_cap_kwh"]) - (float(bat.get("discharged_today_kwh") or 0.0) if dd == 0 else 0.0))
                if not ks or left >= H * float(np.sum(d_ub[ks])) - 1e-9:
                    continue  # it cannot bind
                m.row([D[k] for k in ks], [H] * len(ks), ub=left, name=f"cycle_cap[{int(dd)}]")
                cycle_left[int(dd)] = left
        # reserve (soft, expected value): rows only where the grid is up and an outage may start next, and none where the next step is itself a
        # modelled outage (g = 0): its balance rows already price that outage's shortfall (genset fuel or lost load), which is never charged
        # twice (Part 4.3; round 7, engine-optimiser-09: the step before a scheduled window paid about 0.9 c_short on top)
        sig = bat.get("sigma0", 0.0)
        band = (bat["soc_max"] - bat["soc_min"]) * En
        R = np.zeros(T)
        R_gap = np.zeros(T)                              # behind an AMF panel: the start delay's reserve (R is then the failed start's)
        R_steps = np.zeros(T, dtype=int)  # the steps of outage each reserve covers (two at most behind an AMF panel), for explain's detail
        r_idx, r_gap_idx, r_price, r_gap_price = {}, {}, {}, {}
        c_short = p["c_short"]
        amf = bool(gen and gen["control"] == "auto_on_grid_loss")
        # what the battery can deliver in a step of an outage: the critical load less PV, never above its discharge power, nor above what the
        # inverter's kW AC leaves after PV (round 7, engine-plans-live-06: Lahore held 115 kWh for a 180 kW load on a 50 kW battery, of which
        # it could deliver 25 kWh in the 30 minutes the reserve covered)
        out_kw = np.minimum(np.maximum(0.0, Lc - PVlo), float(bat["P_dis"]))
        if inv_ac is not None:
            out_kw = np.minimum(out_kw, np.maximum(0.0, inv_ac - np.minimum(PVlo, Lc)))
        deficit = out_kw * H / bat["eta_d"]
        for t in range(T):
            d_ = min(int(dur[t]), AMF_FAILED_START_STEPS) if amf else int(dur[t])
            R_steps[t] = d_
            R[t] = min(band, float(deficit[t + 1:t + 1 + d_].sum())) + 2 * sig * En
            if amf and t + 1 < T:
                R_gap[t] = min(band, float(out_kw[t + 1]) * AMF_START_DELAY_S / 3600 / bat["eta_d"]) + 2 * sig * En
        for t in range(T):
            if not (g[t] > 0.5 and p_start[t] >= 0.01 and (t + 1 >= T or g[t + 1] > 0.5)):
                continue
            if amf:
                # behind an AMF panel a shortfall is lost load, in the start delay of every start and over a failed start's 30 minutes:
                # the delay's reserve at p (1 - p_fail) VoLL and the failed start's at p p_fail VoLL, so each kWh short is priced once at
                # its expected cost (round 7, engine-plans-live-03: before, 30 minutes of the whole critical load at the set's diesel cost)
                if R_gap[t] > 0:
                    r_gap_price[t] = float(p_start[t]) * (1.0 - AMF_FAIL_TO_START) * voll
                    rg = m.var(f"r_gap{t}", 1, 0, np.inf, r_gap_price[t])[0]
                    r_gap_idx[t] = rg
                    m.row([E[t + 1], rg], [1, 1], lb=Emin + R_gap[t], name=f"reserve_gap[{t}]")
                price = float(p_start[t]) * AMF_FAIL_TO_START * voll
            else:
                price = float(p_start[t]) * c_short
            if R[t] > 0:
                r = m.var(f"r{t}", 1, 0, np.inf, price)[0]
                r_idx[t] = r
                r_price[t] = price
                m.row([E[t + 1], r], [1, 1], lb=Emin + R[t], name=f"reserve[{t}]")
        # A battery-inverter sheds load only once the battery is at its cut-off, so where a row on E stands for an operator's floor during an
        # outage, load shed in that outage counts as if the battery had carried it (`shed_kwh`): shedding never keeps a floor, which it would
        # otherwise do whenever VoLL times the shortfall is cheaper than the alternative (the soc_threshold rows below use a per-step rule instead)
        def shed_kwh(k0: int, k1: int) -> tuple[list, list]:
            ks = [k for k in range(k0, k1) if g[k] < 0.5]
            return [U[k] for k in ks], [-H / bat["eta_d"]] * len(ks)

        # a manual reserve (Hold) or a force-charge target is a floor with its own slack at VoLL (Part 4.3), only on the steps inside its window.
        # At a grid-connected site a Hold binds while the grid is up and only before the window's first outage step: the reserve is held for that
        # outage and is there to be used in it, and refilling it afterwards is never worth lost load or a genset run. A force-charge target is the
        # operator asking for a charge, not a reserve for an outage, so it binds on every grid-up step of its window, also once the grid returns
        # from an outage the window started in. At an off-grid site there is no outage to hold for, so both bind on every step of their window
        hold_lb, force_lb = manual_floor_parts(p, T)
        hold_on = manual_floor_steps(hold_lb, g, off_grid)
        force_on = (force_lb > 0) & ((g > 0.5) | bool(off_grid))
        rm_on = hold_on | force_on
        rm_lb = np.maximum(np.where(hold_on, hold_lb, 0.0), np.where(force_on, force_lb, 0.0))
        run0 = None
        win_on = (hold_lb > 0) | (force_lb > 0)
        for t in range(T):
            if not rm_on[t]:
                run0 = None
                continue
            if run0 is None:
                run0 = t
                while run0 > 0 and win_on[run0 - 1]:
                    run0 -= 1
            rm = m.var(f"rm{t}", 1, 0, np.inf, voll)[0]
            rm_floor[t] = min(Emax, float(rm_lb[t]))
            sc, sv = shed_kwh(run0, t + 1)
            m.row([E[t + 1], rm] + sc, [1, 1] + sv, lb=rm_floor[t], name=f"manual_reserve[{t}]")
        # the soc_threshold rows (see the genset block): under the threshold the set runs, unless the tank is empty (z); the per-step slack at
        # VoLL is only a guard against infeasibility, since every start the controller would make is allowed. The rows read E alone, so their
        # big-M stays the threshold's depth below the start (a tight relaxation however long the outage), and a shed the plan cannot avoid (the
        # set too small, a hold, an overload) is never carried into later steps: it never keeps the set running once the battery is back above
        # its threshold.
        # In a grid outage shedding cannot dodge them either (`thr_shed`): the battery-inverter sheds only at its cut-off, so on a step k where
        # the set could run and the set is off, load is shed only for what the battery's power cannot carry (`cut`), when the tank is empty
        # (z[k]), or when the battery runs out inside step k. The last is y[k]: it needs the controller's start at k + 1 (w) and shrinks to
        # nothing as E[k + 1] rises from the cut-off to the threshold, so a shed never lands the battery on the threshold to spare a start.
        # An off-grid site has no such rows in its first solve: coupled to the set's binaries on every step of the horizon they slow the solve of
        # a tank that runs dry past the tick's limit, and there a shed only moves the shortfall the tank leaves to another hour. A first plan,
        # optimal or stopped at its limit, that sheds while the tank has fuel left over is solved once more with a lighter rule (`shed_rule`, the
        # `thr_offgrid_shed` rows), and one that burns its tank is repaired (see the end of solve)
        if thr_t:
            th = m.var("thr", len(thr_t), 0, np.inf, voll)
            E_hi = max(Emax, E0)
            for k, t in enumerate(thr_t):
                cols, vals = [E[t], u[t], th[k]], [1, thr_big, 1]
                if t in thr_z:
                    cols.append(thr_z[t])
                    vals.append(thr_big)
                m.row(cols, vals, lb=E_on, name=f"soc_threshold[{t}]")
                m.row([E[t], thr_w[t]], [1, E_hi - E_on], ub=E_hi, name=f"thr_forced[{t}]")
            thr_set = set(thr_t)
            shed_steps = [k for k in range(T - 1) if k + 1 in thr_set and (k in thr_set or (k == 0 and g[0] < 0.5 and u_ub[0] > 0))]
            shed_k = [] if off_grid else shed_steps
            if off_grid and shed_rule:
                # with the set off at k and at k + 1 and fuel in the tank, load is shed only for what the battery's power cannot carry: a battery
                # that reaches its cut-off is under the threshold, so the controller starts the set (u[k + 1]) unless the tank is empty (z[k]). It
                # adds no variables; on the farm's full tank (seven start hours, 10 s) every plan shed nothing, at objectives within 3 % of the
                # first solve's. On a tank that runs dry it slows the solve past the limit, hence only in this re-solve
                for k in shed_steps:
                    cut = max(0.0, float(base_served[k] - PV[k]) - bat["P_dis"])
                    if U_ub[k] <= cut:
                        continue
                    cols, vals = [U[k], u[k], u[k + 1]], [1, -(U_ub[k] - cut), -(U_ub[k] - cut)]
                    if k in thr_z:
                        cols.append(thr_z[k])
                        vals.append(-(U_ub[k] - cut))
                    m.row(cols, vals, ub=cut, name=f"thr_offgrid_shed[{k}]")
            if shed_k:
                yv = m.var("thr_out", len(shed_k), 0, 1, 0.0)
                # a guard only: every shed the rule allows is reachable without it, and at ten times VoLL it is never the cheap way round it
                sv = m.var("thr_shed", len(shed_k), 0, np.inf, H * 10 * voll)
                for i, k in enumerate(shed_k):
                    cut = max(0.0, float(base_served[k] - PV[k]) - bat["P_dis"])
                    cols, vals = [U[k], u[k], yv[i], sv[i]], [1, -U_ub[k], -U_ub[k], -1]
                    if k in thr_z:
                        cols.append(thr_z[k])
                        vals.append(-U_ub[k])
                    m.row(cols, vals, ub=cut, name=f"thr_shed[{k}]")
                    w1 = thr_w[k + 1]
                    m.row([yv[i], w1], [1, -1], ub=0, name=f"thr_out_w[{k}]")
                    m.row([yv[i], E[k + 1], w1], [thr_big, 1, E_hi - E_on], ub=E_hi, name=f"thr_out_e[{k}]")
        # terminal value on the energy the next peak can use
        term = p.get("terminal") or {}
        v_end = max(0.0, bat["eta_d"] * term.get("c_next", 0.0) - c_cyc - term.get("c_cal_hold", 0.0))
        E_use = m.var("E_use", 1, 0, bat["P_dis"] * term.get("H_peak", 4.0) + term.get("R_next", 0.0), -v_end)[0]
        # the terminal value values stored energy, it imposes no floor (round 7, engine-optimiser-03): E_use >= 0 made E[T] >= soc_min a hard
        # row, so a battery that started under its cut-off with nothing to recharge it (grid charging off and no PV surplus, or a charge block
        # to the horizon's end) made the tick infeasible, and the relaxed retry too. Such a start ends under the cut-off only by `E_gap`, priced
        # a rupee above the dearest kWh the horizon could store (EMHASS's soft terminal condition, R2 3.5), so it recovers whenever anything
        # can recharge it, and with nothing to it holds what it has, with no credit
        tail = []
        if E0 < Emin - 1e-9:
            dear = float(np.max(np.asarray(p["c_imp"])[:T])) if (T and not off_grid) else float(p.get("c_short", voll))
            e_gap_price = dear / bat["eta_c"] + 1.0
            e_gap_idx = m.var("E_gap", 1, 0, Emin - E0, e_gap_price)[0]
            tail = [e_gap_idx]
        # the next peak uses only the energy above the floor the table holds with the grid up (round 7, engine-delivery-02); a horizon that
        # ends in an outage keeps the cut-off, and one that ends in a run still under the floor counts above the floor once it is recovered
        if T and g[T - 1] > 0.5 and Elo > Emin + 1e-9:
            if rec_last is None:
                m.row([E_use, E[T]] + tail, [1, -1] + [-1] * len(tail), ub=-Elo, name="terminal")
            else:
                m.row([E_use, E[T], rec_last] + tail, [1, -1, Elo - Emin] + [-1] * len(tail), ub=-Emin, name="terminal")
                m.row([E_use, rec_last], [1, -float(m.ub[E_use])], ub=0, name="terminal_recovered")
        else:
            m.row([E_use, E[T]] + tail, [1, -1] + [-1] * len(tail), ub=-Emin, name="terminal")
    # ---------------------------------------------------------------- the set's surplus and the battery, with the grid down
    if gen and bat and SURPLUS_ROWS and gen_surplus.any() and not thr_t:
        # Two rows the balance implies on a grid-down step (no import, no export) once the set's state is 0 or 1, written out because the
        # relaxation does not see them: with the set on, the battery takes what of its output the load less PV leaves (C >= P - n_on u - K - W),
        # and with it off, the battery carries the load less PV (D >= n_off (1 - u) - U). Without them the relaxation runs the set at the
        # fraction of its minimum load that just meets a night's load, storing nothing and losing no round trip, and the solver spent seconds
        # closing that gap. They cut no plan off (tests/test_fix_r5_genset.py checks the optimum with and without them). Not where a
        # soc_threshold controller starts the set in the horizon: its rows bind the set to the state of charge, and these only slowed those
        # solves (the 400 L threshold farm: optimal in 5.4 s without them, 8.5 s to time-limited with them; 60 L: 0.3 s against 1.1 s).
        # Timings (round 5; the demo farm's manual-start 192-step problems; an idle machine on mains power; 30 s limit; each problem solved by
        # every version in turn), mean and worst, without these rows and the second day's hour marks / with them / HEAD (no surplus: its
        # plans shed): 32 states from 3-hour marks of four days 4.5 s, 7.8 s / 2.1 s, 4.7 s / 1.9 s, 5.7 s; 26 midnight states of two daily
        # rebuilds 3.8 s, 7.5 s / 1.8 s, 4.7 s / 1.9 s, 5.1 s; 48 states of a fresh farm 2.9 s, 8.5 s / 1.7 s, 4.6 s / 1.1 s, 3.2 s
        served_on = L if (off_grid or lmode == 1) else Lc
        for t in np.flatnonzero(gen_surplus):
            n_on, n_off = float(served_on[t] - PV[t]), float(base_served[t] - PV[t])
            cols, vals = [C[t], K[t], Pg[t], u[t]], [1, 1, -1, n_on]
            if W is not None and dump_ok[t]:
                cols.append(W[t])
                vals.append(1)
            m.row(cols, vals, lb=0, name=f"surplus_charge[{t}]")
            if n_off > 1e-9:
                m.row([D[t], U[t], u[t]], [1, 1, n_off], lb=n_off, name=f"off_discharge[{t}]")
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
            if W is not None and dump_ok[t]:
                cols.append(W[t])
                vals.append(-1)
            if u_dep[t]:
                cols.append(u[t])
                vals.append(-(L[t] - Lc[t]))
                m.row([U[t], u[t]], [1, -(L[t] - Lc[t])], ub=Lc[t], name=f"unserved_cap[{t}]")
        m.row(cols, vals, rhs, rhs, f"balance[{t}]")
    # ---------------------------------------------------------------- the inverter's AC rating
    if inv_ac is not None:
        # PV reaches the AC side and the battery discharges through the same inverter, and grid or genset charging comes back through it: the
        # net flow (PV - K) + D - C stays within its kW AC both ways (round 7, flow-onboarding-09's engine half: a 50 kW inverter passed PV 163 kW
        # and a 76 kW charge)
        for t in range(T):
            cols, vals = [K[t]], [-1.0]
            if bat:
                cols += [D[t], C[t]]
                vals += [1.0, -1.0]
            m.row(cols, vals, lb=-inv_ac - float(PV[t]), ub=inv_ac - float(PV[t]), name=f"inverter_ac[{t}]")
    # ---------------------------------------------------------------- MDI
    mdi = p.get("mdi")
    M_idx, M_lb = [], []
    if mdi and not off_grid and g.sum() > 0:
        n_blk = mdi["n_mdi"]
        # never under the month's expected peak (mdi_floors; round 7, engine-optimiser-02): at a month's start M0 is near nothing, and M2 was
        # bounded by the floor alone, so every kW above the floor was priced as a whole month's demand charge
        lb_M, lb_M2 = mdi_floors(mdi)
        Mv = m.var("M", 1, lb_M, np.inf, mdi["rate"])[0]
        M_idx.append(Mv)
        M_lb.append(lb_M)
        M2 = None
        blocks = (np.asarray(p["ts"]) // (n_blk * 900)).astype(np.int64)
        month_end = mdi.get("month_end_step", T)
        if month_end < T:
            M2 = m.var("M2", 1, lb_M2, np.inf, mdi["rate"])[0]
            M_idx.append(M2)
            M_lb.append(lb_M2)
        extra = np.where(g > 0.5, np.maximum(0.0, Lhi - L), 0.0)
        for bid in np.unique(blocks):
            steps = np.where(blocks == bid)[0]
            target = Mv if steps[0] < month_end else M2
            if target is None:
                continue
            elapsed = mdi["G_elapsed"] * mdi["n_elapsed"] if steps[0] == 0 and mdi.get("n_elapsed") else 0.0
            m.row(list(G[steps]) + [target], [1.0] * len(steps) + [-n_blk], ub=-elapsed, name=f"mdi[{bid}]")
            m.row(list(G[steps]) + [target], [1.0] * len(steps) + [-n_blk], ub=-elapsed - float(extra[steps].sum()), name=f"mdi_hi[{bid}]")
            # and step by step: every Mode A slot carries grid_cap_w = M and the device shaves each instant above it, so a step that imports above
            # the cap while its block's mean stays under it (0 / full / 0 / full charging, or one step of each block discharging for both) is a
            # plan the device throttles. Under the upper load quantile as the block rows are (round 7, engine-plans-live-17)
            for t in steps:
                if g[t] > 0.5:
                    m.row([G[t], target], [1.0, -1.0], ub=-float(extra[t]), name=f"mdi_step[{t}]")
    # ---------------------------------------------------------------- ICP, the uncertain middle case
    icp = p.get("icp") or {}
    # legacy net metering nets each period's exports against its imports before the ICP case is decided (tariffs.vectors decides it on the net), so
    # the rows read net import per step: on gross import, an import above the baseline earned the ICP rate while the export it fed was credited at
    # the full import price, and the plan circulated energy through the meter both ways at once
    icp_net = p.get("regime") == "net_metering_legacy"
    for per, info in icp.items():
        if info.get("case") != "uncertain":
            continue
        steps = np.where((np.asarray(p["peak"]) == (per == "peak")) & np.asarray(p["in_month"]) & (g > 0.5))[0]
        if not len(steps):
            continue
        Q, base = info["q_kwh"], info["baseline_kwh"]
        Mp = (p["G_max"] + (p["X_max"] if icp_net else 0.0)) * len(steps) * H + abs(Q - base) + 1.0
        coef = info["rate"] - float(c_imp[steps[0]])
        Z = m.var(f"Z_{per}", 1, 0, np.inf, coef)[0]
        b = m.var(f"b_{per}", 1, 0, 1, 0.0, integer=True)[0]
        q_cols, q_vals = list(G[steps]), [-H] * len(steps)
        if icp_net:
            q_cols += list(X[steps])
            q_vals += [H] * len(steps)
        m.row([Z] + q_cols, [1] + q_vals, lb=Q - base, name=f"icp_lo[{per}]")
        m.row([Z, b], [1, -Mp], ub=0, name=f"icp_b[{per}]")
        m.row([Z] + q_cols + [b], [1] + q_vals + [Mp], ub=Q - base + Mp, name=f"icp_hi[{per}]")
    # ---------------------------------------------------------------- solve
    t_start = time.perf_counter()
    res, A = m.solve(time_limit, gap)
    secs = time.perf_counter() - t_start
    status = "optimal" if res.status == 0 else "feasible" if (res.status == 1 and res.x is not None) else "not_solved"
    out = {"status": status, "solver_status": int(res.status), "message": str(res.message), "solve_seconds": round(secs, 3), "relaxed": relaxed,
           "variables": len(m.lb), "rows": len(m.rlb), "binaries": int(sum(m.integ)), "notes": notes, "config_hash": config_hash(p),
           # the limits are wall-clock time, so a plan's quality depends on the machine's load: say when the solve stopped at its limit, and the
           # relative gap to the solver's bound it reached (Part 4.5; HANDOFF 8.A.5)
           "time_limited": bool(res.status == 1), "time_limit_s": float(time_limit), "mip_gap": _gap_of(res),
           "dual_bound_rs": _finite(getattr(res, "mip_dual_bound", None))}
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
    gv = 0.0
    if gen and T:
        # the genset never below its minimum load when on, and silent when off (Part 4.5)
        on = x[u] > 0.5
        gv = max(float(np.max(np.where(on, gen["f_min"] * gen["S"] - x[Pg], 0.0))), float(np.max(np.where(~on, x[Pg], 0.0))))
        gv = gv if gv > 1e-4 + 1e-7 * gen["S"] else 0.0
    if viol > 1e-4 or frac > 1e-5 or imp_out > 1e-6 or gv > 0.0:
        out["status"] = "not_solved"
        out["message"] = f"Re-check failed: balance {viol:.2e}, integrality {frac:.2e}, import in outage {imp_out:.2e}, genset load {gv:.2e}"
        return out

    def get(name, default=None):
        idx = m.blocks.get(name)
        return x[idx] if idx is not None else (np.zeros(T) if default is None else default)

    sol = {"G": get("G"), "X": get("X"), "K": get("K"), "U": get("U"), "C": get("C"), "D": get("D"), "P": get("P"), "F": get("F"),
           "u": np.round(get("u")), "v": np.round(get("v")), "E": get("E", np.zeros(T + 1)), "W": get("W")}
    out["objective_rs"] = round(float(res.fun), 2)
    out["sol"] = sol
    out["T"] = T
    out["R"] = R if bat else np.zeros(T)
    out["R_steps"] = R_steps if bat else np.zeros(T, dtype=int)
    out["r"] = {t: float(x[i]) for t, i in r_idx.items()} if bat else {}
    # behind an AMF panel: the start delay's reserve and slack (R and r are then the failed start's), and each row's price per kWh short
    out["R_gap"] = R_gap if bat else np.zeros(T)
    out["r_gap"] = {t: float(x[i]) for t, i in r_gap_idx.items()} if bat else {}
    out["r_price"] = dict(r_price) if bat else {}
    out["r_gap_price"] = dict(r_gap_price) if bat else {}
    out["manual_floor"] = dict(rm_floor)
    out["M"] = [float(x[i]) for i in M_idx]
    out["M_lb"] = list(M_lb)  # each peak's lower bound: the month so far, the floor and the expected month peak (mdi_floors)
    out["v_end"] = v_end if bat else 0.0
    # the energy the terminal value credits (the next peak's use, above the grid-up floor and under its cap), and that floor in kWh
    out["E_use"] = float(x[E_use]) if bat else 0.0
    # a start under the cut-off that ends under it: the kWh short of soc_min at the horizon's end and their price (engine-optimiser-03)
    out["E_gap"] = float(x[e_gap_idx]) if e_gap_idx is not None else 0.0
    out["E_gap_price"] = e_gap_price
    # the hour end the anchor charge was planned for (the step, and its end as epoch seconds) and the kWh it stays short of it (engine-optimiser-15)
    out["anchor"] = None
    if anchor_a is not None:
        j = int(np.argmax(x[anchor_a]))
        out["anchor"] = {"step": int(anchor_ends[j]), "t": int(np.asarray(p["ts"])[anchor_ends[j]]) + 900, "short_kwh": round(float(x[anchor_short]), 3)}
    out["E_floor_grid"] = Elo if bat else 0.0
    # the cycle cap's kWh per local day where its row binds the plan (each day's grid-up discharge reaches it), for explain
    out["cycle_cap"] = {}
    if bat and cycle_left:
        days = np.asarray(bat.get("day_index", np.zeros(T, dtype=int)))[:T]
        for dd, left in cycle_left.items():
            used = H * float(np.sum(x[D][(days == dd) & (g > 0.5)]))
            if used >= left - 1e-3 * max(1.0, float(bat["E_nom"])):
                out["cycle_cap"][dd] = left
    out["breakdown"] = _breakdown(p, sol, out)
    out["reasons"] = explain(p, sol, out)
    if off_grid and thr_t and not shed_rule and fix_u is None:
        # An off-grid threshold site's first solve has no shed rule (see the threshold rows), so its plan, optimal or stopped at its limit, can
        # shed with the set off now and next, the battery above its cut-off and fuel in the tank (at a full farm tank: 51.6 kWh over a night at
        # 25 %): a plan the site would never run, since its battery-inverter sheds only at its cut-off and the controller then starts the set.
        # The check reads every first plan, not only one that stopped at its limit (round 4, HANDOFF 8.A.3): the first problem's own optimum can
        # dodge (the 400 L farm at 300 L, optimal in 14.7 s, shed 4.6 kWh at steps 99 and 100 with 149 L left; at 250 L optimal in 9.55 s), so
        # what the tick publishes no longer depends on whether the solve finished inside its limit. A step is a dodge where the rule's row would
        # be broken: the tank can still cover a step there (the fuel burnt before it leaves at least one step at minimum load, so z cannot be 1).
        # Where such a plan leaves fuel in the tank at the horizon's end, the shed was not rationing a short tank, and the problem is solved once
        # more with the rule (the tick then takes up to twice its limit).
        tank_need = max(0.0, float(gen["fuel_available"]) - min_burn)
        burnt = np.concatenate([[0.0], np.cumsum(x[F])])
        dodges = [k for k in shed_steps
                  if x[U[k]] > max(0.0, float(base_served[k] - PV[k]) - bat["P_dis"]) + 1e-3 and x[u[k]] < 0.5 and x[u[k + 1]] < 0.5
                  and not (k in thr_z and burnt[k] >= tank_need - 1e-6)]
        left = float(gen["fuel_available"]) - float(np.sum(x[F]))
        resolved, resolve_limited = False, False
        if dodges and left >= min_burn - 1e-6:
            resolved = True
            again = solve(p, time_limit=time_limit, gap=gap, relaxed=relaxed, shed_rule=True)
            if again["status"] != "not_solved":
                again["solve_seconds"] = round(secs + again["solve_seconds"], 3)
                again["shed_rule_resolve"] = {"shed_steps": len(dodges), "shed_kwh": round(float(np.sum(x[U[dodges]])) * H, 2),
                                              "first_objective_rs": out["objective_rs"], "first_solve_seconds": out["solve_seconds"],
                                              "first_status": status, "first_time_limited": out["time_limited"]}
                return again
            secs += again["solve_seconds"]
            resolve_limited = bool(again.get("time_limited"))
        # The threshold repair (HANDOFF 8.A.3): a plan that still dodges, because it burns the whole tank (the 400 L farm at 300 to 310 L: 10 s
        # plans shed 4.6 kWh with the set off and fuel in the tank, while the optimum, 75 to 90 s away, sheds nothing) or because the re-solve
        # found no plan, is never published as it stands. The controller is replayed over the plan (`threshold_replay`: the set starts under its
        # threshold whenever the tank can cover a step, and the plan's own runs stand where the controller allows them), and the problem is solved
        # with the genset fixed to that schedule and the shed rule on. With the binaries fixed it is close to a linear programme (0.05 to 0.11 s
        # on the farm against 10 s for the first solve), so the repair does not depend on the limit. A repair that finds no plan leaves the first
        # one, with `threshold_repair.status` not_solved. A tick stays within three limits: the repair is skipped after a failed re-solve in the
        # relaxed retry, which already spent two
        if dodges and not (resolved and relaxed):
            rep_u = threshold_replay(p, sol, u_lb, u_ub, thr_t, E_on, dump_forced, relaxed=relaxed)
            rep = solve(p, time_limit=time_limit, gap=gap, relaxed=relaxed, shed_rule=True, fix_u=rep_u["u"])
            info = {"shed_steps": len(dodges), "shed_kwh": round(float(np.sum(x[U[dodges]])) * H, 2), "first_objective_rs": out["objective_rs"],
                    "first_solve_seconds": out["solve_seconds"], "first_mip_gap": out["mip_gap"], "repair_seconds": rep["solve_seconds"],
                    "shed_rule_resolve_failed": resolved, "first_status": status, "first_time_limited": out["time_limited"]}
            if rep["status"] != "not_solved":
                rep["solve_seconds"] = round(secs + rep["solve_seconds"], 3)
                # the schedule is the controller's, not the solver's: the plan is "feasible" (never proven optimal), it counts as time-limited
                # where a solve on its way stopped at its limit (the first, the re-solve or the repair itself), and its gap is read against the
                # first solve's bound (a valid bound: the repaired problem only adds rows to it)
                limited = bool(out["time_limited"] or resolve_limited or rep.get("time_limited"))
                rep["time_limited"] = limited
                rep["mip_gap"] = _gap(rep["objective_rs"], out["dual_bound_rs"])
                rep["dual_bound_rs"] = out["dual_bound_rs"]
                info.update({"status": rep["status"], "starts": int(np.sum(rep["sol"]["v"])), "litres": round(float(np.sum(rep["sol"]["F"])), 2),
                             "unserved_kwh": round(float(np.sum(rep["sol"]["U"])) * H, 2)})
                rep["status"] = "feasible"  # not to the full problem's tolerance: the genset follows the replayed controller
                rep["message"] = ("Threshold repair of a time-limited plan: " if limited else "Threshold repair of a plan that shed with the set off: ") \
                    + str(out.get("message", ""))
                rep["threshold_repair"] = info
                rep["notes"] = list(rep.get("notes", [])) + ["threshold_repair"]
                return rep
            info.update({"status": "not_solved", "message": rep.get("message", "")})
            out["threshold_repair"] = info
            out["solve_seconds"] = round(secs + rep["solve_seconds"], 3)
    return out


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _gap(objective: float | None, bound: float | None) -> float | None:
    """The relative gap of an objective to a lower bound, as HiGHS reports it: (objective - bound) / |objective|."""
    if objective is None or bound is None:
        return None
    return round(max(0.0, (objective - bound) / max(abs(objective), 1e-9)), 6)


def _gap_of(res) -> float | None:
    if getattr(res, "x", None) is None:
        return None
    g = _finite(getattr(res, "mip_gap", None))
    if g is None:
        g = _gap(_finite(getattr(res, "fun", None)), _finite(getattr(res, "mip_dual_bound", None)))
    return round(max(0.0, g), 6) if g is not None else None


def threshold_replay(p: dict, sol: dict, u_lb: np.ndarray, u_ub: np.ndarray, thr_steps, e_on: float, dump_ok: np.ndarray | None = None,
                     relaxed: bool = False) -> dict:
    """The soc_threshold controller replayed over an off-grid plan (`sol`), on the MILP's own 15-minute physics (HANDOFF 8.A.3).

    The plan's genset schedule and output stand where the controller allows them, and the controller overrides them where the site would:
    - under its threshold, with the tank able to cover a step at minimum load, the set runs (a start the plan dodged by shedding);
    - a start runs at least its minimum up time (as the twin's operator does), and then as long as the plan keeps it on or the battery is
      under its threshold;
    - above its threshold the plan cannot start the set (only the controller starts it), and with the tank unable to cover a step it stops;
    - where a day's run-hour cap binds, the hours the threshold does not force count against it (as in the run_cap rows), and once it is used
      a run above the threshold stops, unless it is the plan's own start inside its minimum up time.
    A run the plan did not make follows the load (at least its minimum load). What the load leaves of PV and the set's output charges the
    battery (from the PV beyond the load only, unless the genset may charge it: at full power, or its minimum-load surplus with the grid down),
    the rest is curtailed, then dumped; with the set off the battery carries the load, and load is shed only for what its power or its energy
    above the cut-off cannot carry. The last of the tank is burnt at the output it can still cover. A hold and a carried minimum up or down time
    are kept (`u_lb`, `u_ub`). Returns the schedule `u` and the replayed `E`, `U`, `F` and `P`: a feasible point of the problem with the shed rule and
    that schedule."""
    T = int(p["T"])
    bat, gen = p["battery"], p["genset"]
    L, PV = np.asarray(p["L"], dtype=float), np.asarray(p["PV"], dtype=float)
    u_plan, P_plan = np.asarray(sol["u"], dtype=float), np.asarray(sol["P"], dtype=float)
    En = float(bat["E_nom"])
    Emin, Emax = bat["soc_min"] * En, bat["soc_max"] * En
    eta_c, eta_d = float(bat["eta_c"]), float(bat["eta_d"])
    ch_ub = np.full(T, float(bat["P_ch"]))
    ch_ub[np.asarray(bat.get("charge_block", np.zeros(T, dtype=bool)), dtype=bool)[:T]] = 0.0
    S, f_min, a, b = float(gen["S"]), float(gen["f_min"]), float(gen["a"]), float(gen["b"])
    UT = 1 if relaxed else int(gen["UT"])
    min_burn = H * (a * f_min * S + b * S)
    fuel = float(gen["fuel_available"])
    thr = {int(t) for t in thr_steps}
    dump = np.zeros(T, dtype=bool) if dump_ok is None else np.asarray(dump_ok, dtype=bool)
    # the set's output may charge the battery at full power (a_gen), or only its minimum-load surplus with the grid down (a_gen_surplus): the
    # replay's outputs never exceed max(f_min S, load - PV) where they are not the plan's own, so what the battery takes is that surplus
    gen_ch = np.full(T, bool(bat.get("a_gen"))) | genset_surplus_steps(p)
    E = float(bat["E0"])
    u, Es, Us, Fs, Ps = np.zeros(T), np.zeros(T + 1), np.zeros(T), np.zeros(T), np.zeros(T)
    Es[0] = E
    # the day's run-hour cap, as the run_cap rows read it: hours the threshold does not force count against it, and where the cap binds a run
    # the threshold no longer forces stops once the cap is used, unless it is the plan's own start, which its minimum up time still binds
    day = np.asarray(gen.get("day_index", np.zeros(T, dtype=int)))[:T]
    cap_left: dict[int, float] = {}
    if gen.get("control") != "auto_on_grid_loss":
        for d in np.unique(day):
            c = float(gen.get("cap_hours", np.inf)) - (float(gen.get("hours_today", 0.0)) if d == 0 else 0.0)
            if c < int(np.sum(day == d)) * H:
                cap_left[int(d)] = max(0.0, c)
    on_prev, keep, own_start = gen["u_prev"] == 1, 0, False
    for t in range(T):
        plan_on = u_plan[t] > 0.5
        forced_thr = False
        if u_lb[t] >= 1:
            on = True
        elif u_ub[t] <= 0 or fuel < min_burn - 1e-9:
            on = False
        elif t not in thr:
            on = plan_on  # step 0: no threshold row, the plan decides
        elif E < e_on - 1e-6:
            on = forced_thr = True
        else:
            on = on_prev and (keep > 0 or plan_on)
            d = int(day[t])
            if on and d in cap_left and cap_left[d] < H - 1e-9 and not (own_start and keep > 0):
                on = False
        if on and not on_prev:
            keep = UT
            own_start = not forced_thr
        if on and not forced_thr and int(day[t]) in cap_left:
            cap_left[int(day[t])] -= H
        net = float(L[t] - PV[t])
        P = burn = 0.0
        if on:
            if not dump[t]:
                # step 0 without a forced run: the plan's own output, which its flows carry from the same start
                P = float(P_plan[t]) if plan_on else max(f_min * S, net)
            else:
                # never more than the load PV leaves plus what the battery can take (the PV the set frees for it, or the set's own output
                # where the genset may charge it), and at least the minimum load: no fuel is burnt only to be dumped above that minimum
                room_c = max(0.0, Emax - E) / (H * eta_c)
                useful = net + min(ch_ub[t], room_c, np.inf if gen_ch[t] else max(0.0, -net))
                P = min(S, max(f_min * S, min(float(P_plan[t]) if plan_on else net, useful)))
            burn = H * (a * P + b * S)
            if burn > fuel:
                P = max(f_min * S, (fuel / H - b * S) / a)
                burn = min(fuel, H * (a * P + b * S))
        rest = net - P
        C = D = 0.0
        if rest > 0:
            D = min(float(bat["P_dis"]), max(0.0, E - Emin) * eta_d / H, rest)
            Us[t] = rest - D
        else:
            # without genset charging the battery takes only the PV beyond the load (round 7, engine-optimiser-06), never the set's surplus
            src = -rest if gen_ch[t] else min(-rest, max(0.0, -net))
            C = min(ch_ub[t], max(0.0, Emax - E) / (H * eta_c), src)
        E += H * (eta_c * C - D / eta_d)
        fuel -= burn
        u[t], Es[t + 1], Fs[t], Ps[t] = 1.0 if on else 0.0, E, burn, P
        keep = max(0, keep - 1) if on else 0
        on_prev = on
    return {"u": u, "E": Es, "U": Us, "F": Fs, "P": Ps}


def _breakdown(p: dict, s: dict, out: dict) -> dict:
    c_imp, c_exp = np.asarray(p["c_imp"]), np.asarray(p["c_exp"])
    gen, bat = p.get("genset"), p.get("battery")
    energy = float(np.sum(H * (c_imp * s["G"] - c_exp * s["X"])))
    genset = 0.0
    if gen:
        genset = float(np.sum(gen["c_fuel"] * s["F"] + gen["c_start"] * s["v"] + H * gen["c_runhr"] * s["u"] + H * gen["c_om"] * s["P"]))
    wear = float(np.sum(H * bat["c_cyc"] * s["D"])) if bat else 0.0
    # the demand charge (round 7, engine-plans-live-08, engine-optimiser-12): the objective pays rate x M, and rate x M2 where the horizon
    # crosses into the next billing month. `mdi_month_rs` is the charge at each peak's lower bound (the month so far, the floor and the expected
    # month peak: mdi_floors), which no plan changes; `mdi_rise_rs` what the plan adds above it, M's and M2's. Before, the rise was measured from
    # max(M0, floor) alone, so at a month's start (M0 near nothing) the whole month's charge read as a rise, M2 was in no line, and the rest of the
    # charge was left unitemised
    mdi_rise = mdi_month = 0.0
    if p.get("mdi") and out.get("M"):
        rate = float(p["mdi"]["rate"])
        lbs = list(out.get("M_lb") or mdi_floors(p["mdi"]))[:len(out["M"])]
        mdi_month = rate * float(sum(lbs))
        mdi_rise = rate * float(sum(max(0.0, M - lb) for M, lb in zip(out["M"], lbs)))
    # the calendar ageing the objective prices on the energy held after each step (b_cal, Rs per kWh per step)
    calendar = 0.0
    if bat:
        b_cal = np.asarray(bat.get("b_cal", np.zeros(len(s["G"]))), dtype=float)[:len(s["G"])]
        calendar = float(np.dot(b_cal, np.asarray(s["E"], dtype=float)[1:len(b_cal) + 1]))
    unserved = float(np.sum(H * p["voll"] * s["U"]))
    # each reserve row's slack at its own price (p c_short; behind an AMF panel the start delay's and the failed start's, round 7)
    prices = out.get("r_price") or {}
    reserve = float(sum(prices.get(t, np.asarray(p["p_start"])[t] * p["c_short"]) * r for t, r in out.get("r", {}).items()))
    reserve += float(sum((out.get("r_gap_price") or {}).get(t, 0.0) * r for t, r in (out.get("r_gap") or {}).items()))
    # the credit the objective takes: v_end times E_use, the energy above the floor the next peak can use and never above its cap (round 7,
    # engine-delivery-02; before, every kWh above the cut-off was credited, past the cap)
    terminal = -float(out.get("v_end", 0.0) * out.get("E_use", 0.0)) if bat else 0.0
    # and a start under the cut-off that ends under it pays for the kWh short of it (round 7, engine-optimiser-03)
    terminal += float(out.get("E_gap_price", 0.0) * out.get("E_gap", 0.0)) if bat else 0.0
    return {"energy_rs": round(energy, 2), "genset_rs": round(genset, 2), "wear_rs": round(wear, 2), "mdi_rise_rs": round(mdi_rise, 2),
            "mdi_month_rs": round(mdi_month, 2), "calendar_rs": round(calendar, 2), "unserved_rs": round(unserved, 2), "expected_shortfall_rs": round(reserve, 2), "terminal_credit_rs": round(terminal, 2),
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
    Efl = max(Emin, float(out.get("E_floor_grid", Emin)))  # the grid-up floor: a battery at it has nothing to hold (round 7, engine-delivery-02)
    R = out.get("R", np.zeros(T))
    R_steps = np.asarray(out.get("R_steps", np.asarray(p["dur_steps"])[:T]), dtype=int)
    icp = p.get("icp") or {}
    c_imp, p_start, ts = np.asarray(p["c_imp"], dtype=float), np.asarray(p["p_start"], dtype=float), np.asarray(p["ts"], dtype=np.int64)
    Cs, Ds, Es = np.asarray(s["C"], dtype=float), np.asarray(s["D"], dtype=float), np.asarray(s["E"], dtype=float)
    # the steps that carry a reserve row (p_start >= 0.01 with the grid up); behind an AMF panel also the start delay's rows (R_gap)
    rows = set(out.get("r") or {}) | set(out.get("r_gap") or {})
    R_gap = np.asarray(out.get("R_gap", np.zeros(T)), dtype=float)
    R_top = np.maximum(np.asarray(R, dtype=float), R_gap)
    amf = bool(gen and gen.get("control") == "auto_on_grid_loss")
    margin = 2 * float(bat.get("sigma0", 0.0)) * En if bat else 0.0   # every reserve row's widening for the SoC estimate (Part 4.3)
    tol = 0.02 * En
    Ms = out.get("M") or []
    month_end = int((p.get("mdi") or {}).get("month_end_step", T))

    def cap_at(t: int) -> float | None:
        """The MDI cap step t is held under: the billing month's peak M, or M2 in the next month (round 7, engine-optimiser-02)."""
        if not Ms:
            return None
        return float(Ms[1] if (t >= month_end and len(Ms) > 1) else Ms[0])

    def binds(k: int) -> bool:
        """The reserve row of step k holds the battery: its energy after the step is at or under the reserve it asks for."""
        return k in rows and float(Es[k + 1]) <= Emin + float(R_top[k]) + tol

    def reserve_window(t: int, stop_at_charge: bool) -> int | None:
        """The last step whose reserve row binds the energy the battery holds after step t: from t on, while the grid is up and until the
        battery next discharges (or, for a step that holds, next charges, which refills what a discharge now would take). None where no row in
        that stretch binds: the reserve is not what keeps the energy."""
        last = None
        for k in range(t, T):
            if g[k] < 0.5 or (k > t and Ds[k] > 0.1) or (stop_at_charge and k > t and Cs[k] > 0.1):
                break
            if binds(k):
                last = k
        return last

    def reserve_detail(t: int, last: int) -> dict:
        """The figures a reserve reason can be checked against: the chance that an outage starts while the energy is held for it (every step of
        the window, not one step's), how long that is, how long an outage the reserve covers (two steps behind an AMF panel), and the reserve
        the rows ask for, never above the battery's usable band."""
        k0 = next(k for k in range(t, last + 1) if binds(k))
        chance = 1.0 - float(np.prod(1.0 - np.clip(p_start[t:last + 1], 0.0, 1.0)))
        band = (bat["soc_max"] - bat["soc_min"]) * En
        if amf:
            # behind an AMF panel (round 7, engine-plans-live-03): the energy up to the start delay's reserve is held for the delay of every
            # start; above it, for a failed start (one start in AMF_FAIL_TO_START)
            if k0 in (out.get("r_gap") or {}) and float(Es[t + 1]) <= Emin + float(R_gap[k0]) + tol:
                d = {"p": round(chance, 2), "within_h": round((last + 1 - t) * H, 2), "cover": "start_delay", "seconds": AMF_START_DELAY_S,
                     "hours": round(AMF_START_DELAY_S / 3600, 3), "kwh": round(min(float(R_gap[k0]), band), 1)}
            else:
                d = {"p": round(chance, 2), "within_h": round((last + 1 - t) * H, 2), "cover": "failed_start", "p_fail": AMF_FAIL_TO_START,
                     "hours": round(int(R_steps[k0]) * H, 2), "kwh": round(min(float(R[k0]), band), 1)}
        else:
            d = {"p": round(chance, 2), "within_h": round((last + 1 - t) * H, 2), "hours": round(int(R_steps[k0]) * H, 2),
                 "kwh": round(min(float(R[k0]), band), 1)}
        # the part of it that is the state of charge's uncertainty (2 sigma E_nom, Part 4.3), never above the reserve named: behind an AMF panel
        # it is nearly all of the start delay's reserve, which read as hundreds of kW for a minute (round 7, engine-plans-live-03)
        d["margin_kwh"] = round(min(margin, d["kwh"]), 1)
        return d

    def next_discharge(t: int) -> int | None:
        return next((k for k in range(t + 1, T) if Ds[k] > 0.1), None)

    uses: dict[int, int] = {}                       # a step saved for later and the step its energy is used at (its reason is filled in after)
    capped = out.get("cycle_cap") or {}             # the local days whose grid-up discharge reached the cycle cap (brief-vs-build-04)
    cap_days = np.asarray((bat or {}).get("day_index", np.zeros(T, dtype=int)))[:T] if capped else np.zeros(T, dtype=int)

    def held(t: int, E1: float) -> tuple[str, dict] | None:
        """Why a grid-up step with energy above the floor neither charges nor discharges, from the numbers (round 7, flow-telemetry-plan-control-08):
        - the reserve: a reserve row binds from this step on, before the battery next charges or discharges, so a kWh discharged now is a kWh
          short for the outage those rows hold it for (also on a step whose own p_start has no row);
        - later use: the battery discharges later before it next charges, so the energy is kept for that step;
        - wear: discharging now and putting the kWh back at the next charge (its grid price, or the export its PV forgoes, over the round trip)
          gains a positive gap smaller than the wear cost;
        - the terminal value: nothing refills or uses the energy in the horizon, and it is worth more kept for the day after.
        None where none holds (nothing to gain)."""
        last = reserve_window(t, stop_at_charge=True)
        if last is not None:
            det = reserve_detail(t, last)
            det["kwh"] = round(min(float(det["kwh"]), E1 - Emin), 1)  # the reserve the battery holds, never above what it holds
            det["margin_kwh"] = round(min(float(det["margin_kwh"]), det["kwh"]), 1)
            return "hold_for_outage", det
        nc = next((k for k in range(t + 1, T) if Cs[k] > 0.1), None)
        use = next_discharge(t)
        if use is not None and (nc is None or use < nc):
            uses[t] = use
            det = {"use_t": int(ts[use])}
            if g[use] > 0.5:
                det["use_rs_per_kwh"] = round(float(c_imp[use]), 2)
            return "save_for_later", det
        rt = bat["eta_c"] * bat["eta_d"]
        if nc is None:
            refill = float(out.get("v_end", 0.0)) / bat["eta_d"]
        else:
            Gn, Kn, Pn = float(s["G"][nc]), float(s["K"][nc]), float(s["P"][nc]) if gen else 0.0
            from_grid = Cs[nc] - max(0.0, PV[nc] - Kn + Pn - L[nc]) > 0.1 and Gn > 0.1
            refill = float(c_imp[nc] if from_grid else np.asarray(p["c_exp"], dtype=float)[nc]) / rt
        gap = float(c_imp[t]) - refill
        c_cyc = float(bat["c_cyc"])
        if nc is None and gap < c_cyc + 1e-6:
            return "terminal_value", {}
        if nc is not None and 0.01 < gap < c_cyc + 1e-6:
            return "wear_margin_too_small", {"gap_rs_per_kwh": round(gap, 2), "wear_rs_per_kwh": round(c_cyc, 2)}
        if g[t] > 0.5 and int(cap_days[t]) in capped and gap > c_cyc:
            # discharging now would gain more than its wear, and the day's discharge has reached the cycle cap (brief-vs-build-04)
            cap_kwh = float(bat["cycle_cap_kwh"])
            return "cycle_cap", {"cycles": round(cap_kwh / En, 2), "kwh": round(cap_kwh, 1)}
        return None

    reasons = []
    for t in range(T):
        G, X, K, U, C, D, P = (float(s[k][t]) for k in ("G", "X", "K", "U", "C", "D", "P"))
        E1 = float(s["E"][t + 1]) if bat else 0.0
        d = {}
        if hold[t]:
            r = "operator_hold"
        elif U > 0.1:
            # load the plan cannot serve: in an outage, at a site without a grid, or (the grid up) beyond the connection and PV
            r = "unserved"
            d = {"kw": round(U, 1), "grid": "up" if g[t] >= 0.5 else ("none" if p.get("off_grid") else "down")}
        elif g[t] < 0.5:
            if P > 0.1:
                r = "genset_on"
                d = {"kw": round(P, 1), "rs_per_kwh": round(_gen_marginal(gen), 1) if gen else None}
            elif D > 0.1:
                # a site without a grid has no outage: its battery simply carries the load (round 7, engine-plans-live-14)
                r = "carry_load" if p.get("off_grid") else "carry_outage"
                d = {"kw": round(D, 1)}
            elif C > 0.1:
                r = "charge_from_pv"
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
            elif p.get("mdi") and G > 0.8 * (cap_at(t) or 1e9):
                r = "mdi_cap"
                d = {"kw": round(cap_at(t), 1)}
            else:
                r = "self_consumption"
        elif C > 0.1:
            # what PV and the genset leave over after the load, as delivery.grid_charging reads it; a site without grid charging never names it
            surplus = max(0.0, PV[t] - K + (P if gen else 0.0) - L[t])
            if C - surplus > 0.1 and G > 0.1 and (not bat or bat.get("a_grid", True)):
                # grid charging is "cheaper" only against a dearer step the energy is used at; where it is not (the peak window, or a later use
                # at the same price) and the reserve rows bind the energy, the step is named for the reserve it refills (round 7,
                # flow-telemetry-plan-control-02): never "while energy is cheaper" at the day's highest price
                use = next_discharge(t)
                use_price = float(c_imp[use]) if use is not None and g[use] > 0.5 else None
                cheaper = use_price is not None and use_price > float(c_imp[t]) + 1e-6
                last = reserve_window(t, stop_at_charge=False) if bat else None
                d = {"rs_per_kwh": round(float(c_imp[t]), 2)}
                if last is not None and not cheaper:
                    r = "charge_for_reserve"
                    d.update(reserve_detail(t, last))
                else:
                    r = "charge_from_grid"
                    if use is not None:
                        d["use_t"] = int(ts[use])
                        if use_price is not None:
                            d["use_rs_per_kwh"] = round(use_price, 2)
            elif gen and P > 0.1:
                r = "genset_on"
            else:
                r = "charge_from_pv"
        elif gen and P > 0.1:
            # a set running with the grid up (parallel) is named for what it does, never "genset_off" (Part 4.7)
            r = "genset_on"
            d = {"kw": round(P, 1), "rs_per_kwh": round(_gen_marginal(gen), 1)}
        elif bat and E1 > (Efl if g[t] > 0.5 else Emin) + tol and (why := held(t, E1)) is not None:
            r, d = why
        elif gen and g[t] > 0.5 and float(p["p_start"][t]) >= float(p.get("reserve_threshold", 0.2)) > 0.0:
            # an outage at least as likely as the site's reserve threshold (round 7, engine-plans-live-12), with its chance
            r = "genset_off"
            d = {"p": round(float(p["p_start"][t]), 2)}
        else:
            r = "idle"
        reasons.append({"reason": r, "detail": d})
    for t, k in uses.items():
        reasons[t]["detail"]["use_reason"] = reasons[k]["reason"]
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
    second["T"] = short["T"]  # the horizon actually solved: the caller rebuilds its steps on shorten(p, T)
    # a first solve that stopped at its limit without a plan is what put the relaxed 24-hour plan in its place, so the plan says so (HANDOFF
    # 8.A.5): it counts as time-limited, and its solve time is both solves'
    second["first_time_limited"] = bool(first.get("time_limited"))
    second["first_solve_seconds"] = first.get("solve_seconds")
    second["solve_seconds"] = round(float(first.get("solve_seconds") or 0.0) + float(second.get("solve_seconds") or 0.0), 3)
    if second["first_time_limited"]:
        second["time_limited"] = True
    if second["status"] != "not_solved":
        second["notes"] = second.get("notes", []) + ["relaxed_retry"]
    return second


def manual_floor(p: dict, T: int) -> np.ndarray:
    """The manual reserve per step in kWh (0 where none): `reserve_manual` is a vector (one value per step, as build_problem writes it) or a
    scalar for every step, optionally limited by `reserve_manual_mask`."""
    raw = p.get("reserve_manual")
    if raw is None:
        return np.zeros(T)
    a = np.asarray(raw, dtype=float)
    out = np.full(T, float(a)) if a.ndim == 0 else np.pad(a[:T], (0, max(0, T - len(a))))
    mask = p.get("reserve_manual_mask")
    if mask is not None:
        mk = np.asarray(mask, dtype=bool)[:T]
        out = np.where(np.pad(mk, (0, max(0, T - len(mk)))), out, 0.0)
    return np.nan_to_num(out)


def manual_floor_parts(p: dict, T: int) -> tuple[np.ndarray, np.ndarray]:
    """The manual floor split into its Hold part and its force-charge part, in kWh per step. `reserve_manual_hold` and `reserve_manual_force`
    (build_problem writes both) are read only under `reserve_manual`, so a caller that drops that key drops every manual floor; a problem without
    the parts treats the whole floor as a Hold."""
    total = manual_floor(p, T)
    if p.get("reserve_manual_hold") is None and p.get("reserve_manual_force") is None:
        return total, np.zeros(T)

    def part(key: str) -> np.ndarray:
        raw = p.get(key)
        if raw is None:
            return np.zeros(T)
        a = np.asarray(raw, dtype=float)
        a = np.full(T, float(a)) if a.ndim == 0 else np.pad(a[:T], (0, max(0, T - len(a))))
        return np.minimum(np.nan_to_num(a), total)

    return part("reserve_manual_hold"), part("reserve_manual_force")


def manual_floor_steps(floor: np.ndarray, g: np.ndarray, off_grid: bool = False) -> np.ndarray:
    """The steps on which a manual floor binds (Part 4.3, 4.8): inside its window (a run of steps with a floor) while the grid is up and before the
    window's first outage step, since the reserve is held for that outage and is used in it; at an off-grid site every step of the window."""
    on = np.asarray(floor, dtype=float) > 0
    if off_grid:
        return on
    g = np.asarray(g, dtype=float)
    out = np.zeros(len(on), dtype=bool)
    lapsed = False
    for t in range(len(on)):
        if not on[t]:
            lapsed = False
            continue
        lapsed = lapsed or g[t] < 0.5
        out[t] = not lapsed
    return out


def genset_surplus_steps(p: dict) -> np.ndarray:
    """The steps on which the battery takes the genset's minimum-load surplus (`battery.a_gen_surplus`): every step with the grid down (an
    outage, and every step off-grid), where the set's output follows the load and what its minimum load leaves over charges the battery on its
    own (twin.simulate's outage branch), with no slot asking for it. Never with the grid up, and not where the plan may already charge from the
    genset at full power (`a_gen`, a brand that supports it)."""
    T = int(p["T"])
    bat, gen = p.get("battery"), p.get("genset")
    if not (bat and gen and bat.get("a_gen_surplus") and not bat.get("a_gen")):
        return np.zeros(T, dtype=bool)
    return np.asarray(p["g"], dtype=float)[:T] < 0.5


def curtail_cost(p: dict) -> np.ndarray:
    """The objective's coefficient on curtailed PV (K, kW) per step: a tie-break that keeps PV unless it cannot be used, priced so that PV is
    never curtailed while the battery has room to store it (round 7, engine-plans-live-16). Storing a kWh at a step costs the calendar ageing of
    holding it (b_cal on E), so where PV would fill the battery later the same day, curtailing now and charging later saved those slopes (and
    the order tie-break preferred the later charge): Faisalabad curtailed 54 to 67 kW from 12:00 with the battery at 78 % and charged inside
    the afternoon's load-shedding window, on the median forecast's word that the afternoon would fill it. A kW curtailed therefore pays the
    calendar cost of holding its charge to the end of its local day, and a tie-break falling over the horizon at twice the order's slope, so
    charging now always costs less than curtailing now to charge later that day, as the inverter does on site (PV beyond the load charges a
    battery with room). The genset's dump costs more still (`solve`: curtail PV first, then dump, twin.simulate's order)."""
    T = int(p["T"])
    cost = np.full(T, H * EPS_CURTAIL)
    bat = p.get("battery")
    if not bat or not T:
        return cost
    b_cal = np.asarray(bat.get("b_cal", np.zeros(T)), dtype=float)[:T]
    b_cal = np.pad(b_cal, (0, T - len(b_cal)))
    day = (np.asarray(p["ts"], dtype=np.int64)[:T] + PKT_OFFSET_S) // 86400
    hold, acc = np.zeros(T), 0.0
    for t in range(T - 1, -1, -1):
        if t == T - 1 or day[t + 1] != day[t]:
            acc = 0.0
        acc += max(0.0, float(b_cal[t]))
        hold[t] = acc
    return cost + H * (2 * EPS_ORDER * (1.0 - np.arange(T) / T) + float(bat["eta_c"]) * hold)


def genset_follow_cap(p: dict) -> np.ndarray:
    """The genset's output per step where it follows the load with the grid down: the load it serves less PV, never under its minimum load nor
    over its rating (twin.simulate: gen = min(S, max(f_min S, served - PV))). The load served is the forecast load off-grid or where the set
    carries the whole site in an outage (load_mode 1), else the critical load."""
    T = int(p["T"])
    gen = p["genset"]
    S, f_min = float(gen["S"]), float(gen["f_min"])
    L, Lc, PV = (np.asarray(p[k], dtype=float)[:T] for k in ("L", "Lc", "PV"))
    served = L if (p.get("off_grid", False) or int(p.get("load_mode", 0)) == 1) else Lc
    return np.minimum(S, np.maximum(f_min * S, served - PV))


def shorten(p: dict, T: int) -> dict:
    q = dict(p)
    T = min(int(T), int(p["T"]))
    q["T"] = T
    for k in ("L", "Lhi", "Lc", "PV", "PVlo", "g", "c_imp", "c_exp", "p_start", "dur_steps", "peak", "in_month", "ts", "override_mask",
              "reserve_manual", "reserve_manual_mask", "reserve_manual_hold", "reserve_manual_force"):
        if k in q and q[k] is not None and np.ndim(q[k]) > 0:
            q[k] = np.asarray(q[k])[:T]
    if q.get("battery"):
        b = dict(q["battery"])
        for k in ("b_cal", "charge_block", "day_index"):
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


def mdi_floors(mdi: dict) -> tuple[float, float]:
    """The lower bounds of the billing month's peak M and of the next month's M2 (kW): the floor, the month's expected peak (`M_exp`,
    plans.month_to_date: the rest of the month almost surely reaches it, so a kW under it costs nothing; round 7, engine-optimiser-02) and, for
    M, the month's peak so far."""
    exp = float(mdi.get("M_exp") or 0.0)
    return max(float(mdi["M0"]), float(mdi["floor"]), exp), max(float(mdi["floor"]), exp)


def wear_cost(battery: dict) -> float:
    """Rs per kWh discharged: replacement cost per usable kWh over warranted throughput (Part 4.3, at the warranted depth)."""
    return battery["replacement_cost_per_kwh"] / (battery["warranted_cycles"] * battery["warranted_depth"])


def calendar_slope(temp_c: np.ndarray, replacement_cost_per_kwh: float) -> np.ndarray:
    """BLAST-Lite-style LFP calendar term as Rs per kWh stored per 15-minute step (R6 section 6.4: about 0.034 at 25 C and 0.109 at 45 C at
    Rs 52,000 per kWh), Arrhenius-like between and beyond, scaled by the pack's cost."""
    t = np.asarray(temp_c, dtype=float)
    k = math.log(0.109 / 0.034) / 20.0
    return 0.034 * np.exp(k * (t - 25.0)) * replacement_cost_per_kwh / 52000.0
