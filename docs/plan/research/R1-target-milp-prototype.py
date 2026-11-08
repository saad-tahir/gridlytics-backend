"""Prototype of the Product 1 target dispatch MILP on scipy.optimize.milp (HiGHS).

Purpose: measure build and solve time for the FULL target feature set at 15-minute
steps over 48 hours (T = 192), so the fork-vs-rewrite judgement in R1 rests on a number.
This is OnyxLeaf's own code, not derived from the evcc clone; it only copies evcc's
STRUCTURE (energy balance, SoC dynamics, import/export direction binary, soft limits
with slack variables, clean objective recomputed from the schedule).

Features modelled (all per step t, dt = 0.25 h):
  grid import split into TOU-priced and ICP-priced energy (baseline exhaustion binary)
  grid export at an asymmetric net-billing credit, no simultaneous import/export (binary y)
  grid availability a_t: outage windows force import and export to zero
  battery charge/discharge/SoC with efficiencies, charge-vs-discharge lock (binary)
  soft reserve SoC for the next forecast outage (slack + penalty)
  genset unit commitment: on/off binary, start and stop indicators, min load, min up/down,
    start cost, HOMER fuel curve F = F0*u + F1*g (L/h) times diesel price
  monthly MDI charge on any rise of the horizon peak above the peak to date
  piecewise-linear convex battery wear cost on discharge throughput (3 segments)
  PV curtailment, unserved critical load slack (VOLL penalty)
  terminal value of stored energy
Units: kW, kWh, Rs.
"""
import sys
import time

import numpy as np
from scipy.optimize import LinearConstraint, Bounds, milp
from scipy.sparse import coo_array


class Model:
    def __init__(self):
        self.n = 0
        self.lb, self.ub, self.integ, self.cost, self.names = [], [], [], [], []
        self.rows_i, self.rows_j, self.rows_v = [], [], []
        self.row_lb, self.row_ub = [], []
        self.m = 0

    def var(self, name, lb=0.0, ub=np.inf, integer=False, cost=0.0):
        self.names.append(name)
        self.lb.append(lb)
        self.ub.append(ub)
        self.integ.append(1 if integer else 0)
        self.cost.append(cost)
        self.n += 1
        return self.n - 1

    def vars(self, name, T, **kw):
        return [self.var(f"{name}_{t}", **kw) for t in range(T)]

    def row(self, terms, lb=-np.inf, ub=np.inf):
        """terms: list of (var_index, coefficient)."""
        for j, v in terms:
            if v != 0.0:
                self.rows_i.append(self.m)
                self.rows_j.append(j)
                self.rows_v.append(float(v))
        self.row_lb.append(lb)
        self.row_ub.append(ub)
        self.m += 1

    def solve(self, time_limit=60.0, mip_rel_gap=1e-4):
        A = coo_array((self.rows_v, (self.rows_i, self.rows_j)), shape=(self.m, self.n)).tocsr()
        cons = LinearConstraint(A, np.array(self.row_lb), np.array(self.row_ub))
        res = milp(c=np.array(self.cost), constraints=cons,
                   bounds=Bounds(np.array(self.lb), np.array(self.ub)),
                   integrality=np.array(self.integ),
                   options={"time_limit": time_limit, "mip_rel_gap": mip_rel_gap, "disp": False})
        return res


def build(T=192, dt=0.25, seed=0, outage=True, genset=True, icp=True, mdi=True, wear=True, sparse_icp=False):
    rng = np.random.default_rng(seed)
    hours = (np.arange(T) * dt) % 24
    # Pakistani B-2 style inputs. Load 60 to 160 kW with a daytime shift, PV 150 kWp.
    load = 80 + 60 * np.clip(np.sin((hours - 7) / 24 * 2 * np.pi), 0, 1) + rng.normal(0, 6, T)
    load = np.clip(load, 40, 200)
    pv = 150 * np.clip(np.sin((hours - 6) / 12 * np.pi), 0, 1) * np.where((hours > 6) & (hours < 18), 1, 0)
    pv = pv * rng.uniform(0.6, 1.0, T)
    # TOU: peak 18:00 to 22:00 Rs 35.68, off-peak Rs 22.83 (B-2(b) from the Product 1 report)
    p_tou = np.where((hours >= 18) & (hours < 22), 35.68, 22.83)
    p_icp = 22.98           # ICP flat price above the monthly baseline
    p_exp = 8.13            # net-billing export credit
    p_term = 20.0           # terminal value of stored energy, Rs/kWh, below off-peak
    # grid availability: forecast load-shedding window 19:00 to 21:00 on both days
    avail = np.ones(T)
    if outage:
        avail[(hours >= 19) & (hours < 21)] = 0
    # critical load share that must be served
    critical = 0.5 * load
    # reserve: energy needed to carry critical load through the NEXT outage window, from each step
    reserve = np.zeros(T)
    if outage:
        for t in range(T):
            # find next outage block starting at or after t
            idx = np.where(avail[t:] == 0)[0]
            if len(idx):
                s = t + idx[0]
                e = s
                while e < T and avail[e] == 0:
                    e += 1
                reserve[t] = critical[s:e].sum() * dt / 0.95
    # battery 200 kWh, 100 kW, LFP
    E, p_ch, p_dis, eta_c, eta_d = 200.0, 100.0, 100.0, 0.95, 0.95
    soc_min, soc_max, soc0 = 0.1 * E, 0.95 * E, 0.5 * E
    # genset 150 kVA ~ 120 kW, min load 30 %, HOMER curve F0 = 0.08 L/h/kW_rated, F1 = 0.25 L/kWh
    G, gmin, F0, F1, diesel, start_cost = 120.0, 0.3, 0.08 * 120.0, 0.25, 400.67, 500.0
    UT, DT = 4, 4  # minimum up and down: 1 hour each
    # MDI: peak to date this month 140 kW, Rs 1,250 per kW per month
    P0, mdi_rate = 140.0, 1250.0
    # ICP: baseline remaining this month (kWh); small so the binary is exercised
    base_rem = 1500.0
    # wear: convex PWL on discharge throughput over the horizon (Rs/kWh): 7.5 up to 1 cycle, 10.8 next, 20 beyond
    wear_seg = [(7.5, E), (10.8, E), (20.0, np.inf)]
    VOLL, pen_reserve = 500.0, 300.0
    M_grid = 500.0

    m = Model()
    imp_tou = m.vars("imp_tou", T, cost=0)           # kW
    imp_icp = m.vars("imp_icp", T, cost=0) if icp else None
    exp = m.vars("exp", T)
    relax = bool(int(sys.argv[4])) if len(sys.argv) > 4 else False   # relax direction and lock binaries
    y = m.vars("y", T, ub=1, integer=not relax)      # 1 = import allowed, 0 = export allowed
    ch = m.vars("ch", T, ub=p_ch)
    dis = m.vars("dis", T, ub=p_dis)
    soc = m.vars("soc", T, lb=0, ub=E)
    zcd = m.vars("zcd", T, ub=1, integer=not relax)
    s_min_pen = m.vars("s_min_pen", T)
    s_max_pen = m.vars("s_max_pen", T)
    s_res = m.vars("s_res", T) if outage else None
    curt = m.vars("curt", T)
    unserved = m.vars("unserved", T)
    if genset:
        u = m.vars("u", T, ub=1, integer=True)
        v = m.vars("v", T, ub=1)
        w = m.vars("w", T, ub=1)
        g = m.vars("g", T, ub=G)
    if icp:
        x = m.vars("x", T, ub=1, integer=True)       # baseline exhausted by step t
        cum = m.vars("cum", T, ub=base_rem) if sparse_icp else None
    if mdi:
        peak = m.var("peak", lb=P0)
    if wear:
        q = [m.var(f"wear_q{k}", ub=cap) for k, (c, cap) in enumerate(wear_seg)]

    # objective (minimise)
    for t in range(T):
        m.cost[imp_tou[t]] = p_tou[t] * dt
        if icp:
            m.cost[imp_icp[t]] = p_icp * dt
        m.cost[exp[t]] = -p_exp * dt
        m.cost[s_min_pen[t]] = 1000.0
        m.cost[s_max_pen[t]] = 1000.0
        m.cost[unserved[t]] = VOLL * dt
        if outage:
            m.cost[s_res[t]] = pen_reserve
        if genset:
            m.cost[u[t]] = F0 * dt * diesel
            m.cost[g[t]] = F1 * dt * diesel
            m.cost[v[t]] = start_cost
    m.cost[soc[T - 1]] = -p_term
    if mdi:
        m.cost[peak] = mdi_rate
    if wear:
        for k, (c, cap) in enumerate(wear_seg):
            m.cost[q[k]] = c

    for t in range(T):
        # energy balance (kW): pv - curt + imp + dis + g = load - unserved + ch + exp
        terms = [(curt[t], -1), (imp_tou[t], 1), (dis[t], 1), (unserved[t], 1), (ch[t], -1), (exp[t], -1)]
        if icp:
            terms.append((imp_icp[t], 1))
        if genset:
            terms.append((g[t], 1))
        m.row(terms, lb=load[t] - pv[t], ub=load[t] - pv[t])
        # curtailment bounded by PV
        m.ub[curt[t]] = pv[t]
        # unserved bounded by non-critical load? keep critical as the floor: unserved <= load - critical
        m.ub[unserved[t]] = load[t] - critical[t] + critical[t] * 0.0 + 1e-9 if False else load[t]
        # import/export direction and availability
        m.row([(imp_tou[t], 1)] + ([(imp_icp[t], 1)] if icp else []) + [(y[t], -M_grid * avail[t])], ub=0)
        m.row([(exp[t], 1), (y[t], M_grid * avail[t])], ub=M_grid * avail[t])
        # charge / discharge lock
        m.row([(ch[t], 1), (zcd[t], -p_ch)], ub=0)
        m.row([(dis[t], 1), (zcd[t], p_dis)], ub=p_dis)
        # SoC dynamics
        prev = soc0 if t == 0 else None
        terms = [(soc[t], 1), (ch[t], -eta_c * dt), (dis[t], dt / eta_d)]
        if t > 0:
            terms.append((soc[t - 1], -1))
            m.row(terms, lb=0, ub=0)
        else:
            m.row(terms, lb=soc0, ub=soc0)
        # soft SoC band
        m.row([(s_max_pen[t], 1), (soc[t], -1)], lb=-soc_max)
        m.row([(s_min_pen[t], 1), (soc[t], 1)], lb=soc_min)
        # soft reserve for the next forecast outage
        if outage and reserve[t] > 0:
            m.row([(soc[t], 1), (s_res[t], 1)], lb=reserve[t])
        if genset:
            m.row([(g[t], 1), (u[t], -gmin * G)], lb=0)
            m.row([(g[t], 1), (u[t], -G)], ub=0)
            u_prev = [(u[t - 1], -1)] if t > 0 else []
            m.row([(v[t], 1), (u[t], -1)] + [(u[t - 1], 1)] if t > 0 else [(v[t], 1), (u[t], -1)], lb=0)
            m.row([(w[t], 1), (u[t], 1)] + [(u[t - 1], -1)] if t > 0 else [(w[t], 1), (u[t], 1)], lb=0)
            # minimum up: starts in the last UT steps <= u_t ; minimum down: stops in last DT steps <= 1 - u_t
            m.row([(v[s], 1) for s in range(max(0, t - UT + 1), t + 1)] + [(u[t], -1)], ub=0)
            m.row([(w[s], 1) for s in range(max(0, t - DT + 1), t + 1)] + [(u[t], 1)], ub=1)
        if icp:
            # ICP energy only once the baseline is exhausted; TOU cumulative <= remaining baseline
            m.row([(imp_icp[t], 1), (x[t], -M_grid)], ub=0)
            if sparse_icp:
                terms = [(cum[t], 1), (imp_tou[t], -dt)] + ([(cum[t - 1], -1)] if t > 0 else [])
                m.row(terms, lb=0, ub=0)
                m.row([(cum[t], 1), (x[t], -base_rem)], lb=0)
            else:
                m.row([(imp_tou[s], dt) for s in range(t + 1)], ub=base_rem)
                m.row([(imp_tou[s], dt) for s in range(t + 1)] + [(x[t], -base_rem)], lb=0)
            if t > 0:
                m.row([(x[t], 1), (x[t - 1], -1)], lb=0)
        if mdi:
            m.row([(peak, 1), (imp_tou[t], -1)] + ([(imp_icp[t], -1)] if icp else []), lb=0)
    if wear:
        m.row([(dis[t], dt) for t in range(T)] + [(q[k], -1) for k in range(len(wear_seg))], lb=0, ub=0)
    if mdi:
        # MDI cost on the rise above the peak to date only: shift constant
        m.cost[peak] = mdi_rate
    meta = dict(T=T, load=load, pv=pv, avail=avail, reserve=reserve, p_tou=p_tou, P0=P0)
    return m, meta


def main():
    T = int(sys.argv[1]) if len(sys.argv) > 1 else 192
    print(f"scipy milp (HiGHS) prototype, T={T}")
    results = []
    for seed in range(3):
        for cfg_name, cfg in [("full_dense_icp", {}),
                              ("full_sparse_icp", dict(sparse_icp=True)),
                              ("genset_only(no icp)", dict(icp=False)),
                              ("icp_only_sparse(no genset)", dict(genset=False, sparse_icp=True)),
                              ("no_genset_no_icp", dict(genset=False, icp=False))]:
            t0 = time.perf_counter()
            m, meta = build(T=T, seed=seed, **cfg)
            t1 = time.perf_counter()
            res = m.solve(time_limit=float(sys.argv[2]) if len(sys.argv) > 2 else 120.0,
                          mip_rel_gap=float(sys.argv[3]) if len(sys.argv) > 3 else 1e-4)
            t2 = time.perf_counter()
            nbin = sum(m.integ)
            status = res.message.split('(')[0].strip() if res.message else str(res.status)
            print(f"seed={seed} {cfg_name:28s} vars={m.n:5d} bin={nbin:4d} rows={m.m:5d} "
                  f"build={t1 - t0:6.3f}s solve={t2 - t1:7.3f}s status={res.status} "
                  f"obj={res.fun if res.fun is not None else float('nan'):12.1f} gap={getattr(res, 'mip_gap', float('nan'))}")
            results.append((seed, cfg_name, m.n, nbin, m.m, t1 - t0, t2 - t1, res.status, res.fun))
    return results


if __name__ == "__main__":
    main()
