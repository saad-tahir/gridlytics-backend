# R3 — REopt.jl formulations to port into the Python EMS optimiser

Research note for the OnyxLeaf TOU/outage/genset-aware EMS (Product 1). Written 6 November 2026.
Scope: the generator (genset) fuel and unit-commitment constraints, outage handling (deterministic and
stochastic), the MPC module and how a rolling horizon is driven, the tariff and demand-charge
formulation, and the battery-degradation model, as implemented in REopt.jl, written out as plain
math that can be ported to a Python MILP (scipy.optimize.milp over HiGHS, PuLP, or CVXPY).
Everything below was read from the source; nothing was executed in Julia (no Julia on this machine).
Items marked UNVERIFIED could not be confirmed from a primary source.

This note does not restate the two planning documents in `C:/Users/SaadTahir/Videos/solarytics/`
(`Perfect-EMS_TOU_Dispatch_Deep_Review.md` and `Product 1 - TOU-Aware Energy Management and
Dispatch.md`). Where it touches them it extends or corrects them (see section 12).

---

## 0. Provenance

| Item | Value |
|---|---|
| Repository | https://github.com/NatLabRockies/REopt.jl (the first URL in the task worked; `git remote -v` confirms `origin https://github.com/NatLabRockies/REopt.jl`) |
| Clone (shallow, read-only, untrusted data) | `C:/Users/SaadTahir/AppData/Local/Temp/claude/C--Users-SaadTahir-Videos/1143ee09-5b9b-42cf-b77e-04f40d670b5a/scratchpad/oss/REopt.jl` |
| Commit at HEAD | `35abb019e29532ef45d4a61c8dcecd2b8cdb74da`, 2026-10-28 13:06:52 -0600, "Increase timeout limit for PV API calls" |
| Package version | `Project.toml` line 3: `version = "1.0.1"`; `julia = "1.8"` compat; depends on JuMP (0.21–1), MathOptInterface, HTTP, ArchGDAL, CoolProp, LinDistFlow (`Project.toml` lines 7–41) |
| Licence files | `LICENSE` = Apache License 2.0 full text (no appendix block filled in); `NOTICE` = "Copyright 2023 Alliance for Sustainable Energy, LLC" plus the Apache boilerplate |
| Source header on every `.jl` file (94 files under `src/`) | `# REopt®, Copyright (c) Alliance for Energy Innovation, LLC. See also https://github.com/NatLabRockies/REopt.jl/blob/master/LICENSE.` (e.g. `src/constraints/generator_constraints.jl` line 1) |
| README line 1 | `# REopt® Julia package`; line 2 says REopt.jl is "developed by the National Laboratory of the Rockies (NLR)" |
| Solvers | README "Quick Start": tested with HiGHS, Cbc, SCIP, Xpress, CPLEX; `src/REopt.jl` line 113: `const INDICATOR_COMPATIBLE_SOLVERS = ["CPLEX","Xpress"]`, so with HiGHS every `if solver_is_compatible_with_indicator_constraints(...)` branch falls to the **big-M** alternative. Those big-M branches are the ones written out below. `src/core/settings.jl` line 12: default `solver_name = "HiGHS"`. |
| Constants | `src/REopt.jl` line 65: `KWH_PER_GAL_DIESEL = 40.7` (HHV, kWh per US gallon); line 66: `KWH_PER_MMBTU = 293.07107`; line 71: `BIG_NUMBER = 1.0e10` |
| Time handling | `hours_per_time_step = 1 / time_steps_per_hour` (`src/mpc/inputs.jl` line 48). 15-minute steps are `time_steps_per_hour = 4`. All energy terms multiply kW by `hours_per_time_step`. |

Files studied (line counts): `src/constraints/generator_constraints.jl` (116), `src/core/generator.jl` (197),
`src/core/electric_utility.jl` (646), `src/constraints/outage_constraints.jl` (438),
`src/constraints/battery_degradation.jl` (227), `src/core/energy_storage/electric_storage.jl` (795),
`src/mpc/model.jl` (309), `src/mpc/inputs.jl` (166), `src/mpc/structs.jl` (401), `src/mpc/scenario.jl` (146),
`src/mpc/constraints.jl` (39), `src/mpc/results.jl` (59), `src/core/electric_tariff.jl` (463),
`src/constraints/electric_utility_constraints.jl` (598), `src/constraints/storage_constraints.jl` (393),
`src/constraints/load_balance.jl` (193), `src/constraints/tech_constraints.jl` (70),
`src/constraints/operating_reserve_constraints.jl` (62), `src/core/reopt.jl` (sections), `src/core/reopt_inputs.jl`
(lines 1218–1247), `src/core/utils.jl` (lines 1–9, 642–665), `src/outagesim/outage_simulator.jl` (header),
`CHANGELOG.md`, `README.md`, `docs/src/mpc/*.md`, `test/runtests.jl` (lines 467–495), `test/scenarios/mpc.json`.

---

## 1. Symbol map (REopt name → port name used below)

All REopt decision variables are JuMP variables keyed by tech `t`, storage `b`, time step `ts`
(1-based) and sometimes tier. The port symbols below are per time step `t = 1..T`, step length
`h` hours (0.25 for 15-minute steps).

| REopt (file, line) | Port symbol | Meaning |
|---|---|---|
| `dvRatedProduction["Generator", ts]` (reopt.jl 656; mpc/model.jl 236) | `P_gen[t]` ≥ 0 | genset electrical output, kW (production factor for a generator is 1, `src/mpc/inputs.jl` 162) |
| `binGenIsOnInTS["Generator", ts]` (reopt.jl 684; mpc/model.jl 271) | `u[t]` ∈ {0,1} | genset on in step |
| `dvFuelUsage["Generator", ts]` (reopt.jl 689; mpc/model.jl 270) | `F[t]` ≥ 0 | fuel **energy** burned in the step, kWh (HHV). Gallons = F / HHV (`src/results/generator.jl` line 60) |
| `dvSize["Generator"]` (fixed to `existing_sizes` in MPC, mpc/model.jl 256) | `S` | genset rating, kW (a constant in the EMS) |
| `p.max_sizes[t]` (= existing size in MPC, mpc/inputs.jl 7, 97) | `S_max` | big-M for the genset (equals S in the EMS) |
| `dvGridPurchase[ts, tier]` (reopt.jl 655; mpc/model.jl 235, one tier) | `G[t]` ≥ 0 | grid import, kW (includes energy routed to the battery) |
| `dvGridToStorage[b, ts]` | `G_b[t]` ≥ 0 | grid-to-battery part of the import |
| `dvProductionToGrid[t, u, ts]`, `dvStorageToGrid[b, u, ts]` (export bin `u` ∈ {NEM, WHL, EXC}) | `X_pv[t]`, `X_b[t]` ≥ 0 | export from PV and from battery |
| `dvProductionToStorage[b, t, ts]` | `C_pv[t]`, `C_gen[t]` ≥ 0 | PV (or genset) power charging the battery |
| `dvDischargeFromStorage[b, ts]` | `D[t]` ≥ 0 | battery discharge, kW (AC side) |
| `dvCurtail[t, ts]` | `K[t]` ≥ 0 | PV curtailment |
| `dvStoredEnergy[b, ts]`, index `0:T` (reopt.jl 661; mpc/model.jl 241) | `E[t]`, `E[0]` given | battery energy, kWh |
| `dvStorageEnergy[b]`, `dvStoragePower[b]` (fixed in MPC, mpc/model.jl 258–261) | `E_cap`, `P_cap` | battery kWh and inverter kW |
| `dvPeakDemandMonth[mth, tier]`, `dvPeakDemandTOU[r, tier]` | `M_mon[m]`, `M_tou[r]` | billed peak kW for the month and for each TOU demand ratchet |
| `dvUnservedLoad[s, tz, ts]` etc. | section 3.3 | stochastic-outage variables |
| `p.production_factor[t, ts]` | `pf[t]` (PV: kW per kW installed; genset: 1) | |
| `p.s.electric_load.loads_kw[ts]`, `critical_loads_kw[ts]` | `L[t]`, `L_crit[t]` | site load and critical load |
| `p.s.electric_tariff.energy_rates[ts, tier]` | `c_e[t]` | import energy price per kWh |
| `p.s.electric_tariff.export_rates[u][ts]` (stored **negative**) | `-c_x[t]` | export credit per kWh |
| `p.fuel_cost_per_kwh["Generator"][ts]` = $/gal ÷ HHV (mpc/inputs.jl 163) | `c_f` | fuel price per kWh of fuel energy |
| `p.pwf_e`, `p.pwf_om`, `p.pwf_fuel`, `p.levelization_factor`, `p.third_party_factor` | all = 1.0 in MPC (mpc/inputs.jl 78–83) | present-worth and levelisation factors used only by the 25-year sizing model |

---

## 2. Generator (genset)

### 2.1 Inputs and defaults

Sizing model `Generator` (`src/core/generator.jl`, constructor lines 99–145):

- `only_runs_during_grid_outage::Bool = true` (line 102)
- `electric_efficiency_full_load = 0.322` (line 111), `electric_efficiency_half_load = electric_efficiency_full_load` (line 112), both HHV basis (CHANGELOG lines 625–627 and 771: these replaced the earlier direct `fuel_slope_gal_per_kwh = 0.076` and `fuel_intercept_gal_per_hr` inputs)
- `fuel_avail_gal = 1.0e9` (line 113), `fuel_higher_heating_value_kwh_per_gal = KWH_PER_GAL_DIESEL = 40.7` (line 114)
- `min_turn_down_fraction = off_grid_flag ? 0.15 : 0.0` (line 115)
- `fuel_cost_per_gallon = 2.25` (line 110); `om_cost_per_kwh = 0.0` (108); `om_cost_per_hr_per_kw_rated = 0.0` (109)
- `sells_energy_back_to_grid = false` (116), `can_net_meter/can_wholesale/can_export_beyond_nem_limit = false` (117–119), `can_curtail = false` (120)

MPC model `MPCGenerator` (`src/mpc/structs.jl` lines 282–336; constructor defaults 299–314):

- `size_kw` required; `max_kw = size_kw` (line 316)
- `fuel_cost_per_gallon = 3.0` (301), `electric_efficiency_full_load = 0.3233` (302), half = full (303)
- `min_turn_down_fraction = 0.0` with the comment `# TODO change this to non-zero value` (306)
- `only_runs_during_grid_outage = true` (307) — **in the MPC the genset is by default fixed to zero whenever the grid is up** (see 2.4). For the EMS set this false so the optimiser can choose diesel peak-shaving when the ICP/peak TOU price exceeds the diesel marginal cost.

### 2.2 Fuel curve: slope and intercept from two efficiencies

`src/core/utils.jl` lines 652–665 (`fuel_slope_and_intercept`; docstring from line 642):

```
fuel_burn_full_load_kwht   = 1.0 / eff_full          # line 657  [kWh fuel per hour per kW rated, at 100 % load]
fuel_burn_half_load_kwht   = 0.5 / eff_half          # line 658  [kWh fuel per hour per kW rated, at 50 % load]
slope_kwht_per_kwhe        = (full - half) / (1.0 - 0.5)              # line 659
intercept_kwht_per_hr      = full - slope_kwht_per_kwhe * 1.0          # line 660  (per kW rated)
slope_unit_per_kwhe        = slope_kwht_per_kwhe / HHV_kwh_per_unit    # line 661  [gal per kWh_e]
intercept_unit_per_hr      = intercept_kwht_per_hr / HHV_kwh_per_unit  # line 662  [gal per hour per kW rated]
```

In plain math, with a = slope (kWh fuel per kWh electric) and b = intercept (kWh fuel per hour per kW
rated), the fuel curve through the two measured points is

    fuel_rate(P) = a * P + b * S        [kWh fuel per hour]   for a genset of rating S kW at output P kW
    a = (1/eff_full - 0.5/eff_half) / 0.5
    b = 1/eff_full - a

With the defaults (eff_full = eff_half = 0.322): a = 1/0.322 = 3.1056, b = 0 — the default curve has
**no idle intercept**, which hides the scaling issue in 2.3. With a realistic split (eff_full = 0.34,
eff_half = 0.30): a = 2.549 kWh/kWh, b = 0.392 kWh/h per kW rated; in US gallons that is
0.0626 gal/kWh and 0.00964 gal/h/kW. See the appendix for a Python re-computation and a
litres-per-hour version for a Pakistani 100 kW set.

### 2.3 Fuel-burn constraint (`add_fuel_burn_constraints`, `generator_constraints.jl` lines 2–17)

Line 8–12, for every genset t and step ts:

    F[t] = (a_gal * HHV) * pf[t] * h * P_gen[t]  +  (b_gal * HHV) * h * u[t]

Since `a_gal * HHV = a` and `b_gal * HHV = b`, this is

    F[t] = a * h * P_gen[t] + b * h * u[t]            [kWh fuel]            (REopt as written)

**Scaling quirk (verified by reading, not by execution).** `b` is derived per kW rated (line 657:
`1.0 / eff` is the fuel for one kW), but line 11 multiplies the intercept by `u[t]` only, not by
`dvSize` or `S`. For a genset larger than 1 kW the idle fuel is therefore under-counted by the factor S
whenever eff_half differs from eff_full. The CHP code does scale the intercept by size: it introduces
`dvMGCHPOnSize = dvMGsize * binMGCHPIsOnInTS` and linearises the product
(`outage_constraints.jl` lines 237–249). The same omission exists in the outage copy for the
generator (`add_MG_Gen_fuel_burn_constraints`, lines 178–183: `fuel_intercept_gal_per_hr *
hours_per_time_step * sum(binMGGenIsOnInTS)`). The outage simulator, by contrast, takes `b` as a whole-unit
gal/h intercept (`src/outagesim/outage_simulator.jl` lines 10–11, 36). Port with the size explicitly:

    F[t] = a * h * P_gen[t] + b * S * h * u[t]        [kWh fuel]            (corrected port)

Fuel cap, line 13–16: `sum_t F[t] <= fuel_avail_gal * HHV` (one tank for the whole horizon). For the
EMS, use the measured tank level: `sum_t F[t] / HHV_per_litre <= litres_in_tank - reserve_litres`.

Fuel cost (sizing path, lines 113–115): `TotalGenFuelCosts = sum_t pwf_fuel * F[t] * c_f[t]` with
`c_f = fuel_cost_per_gallon / HHV` (`src/mpc/inputs.jl` line 163) — units consistent.

**MPC fuel-cost units bug (verified by reading).** `src/mpc/model.jl` lines 169–171 overwrite that
expression with `sum(dvFuelUsage[t,ts] * p.s.generator.fuel_cost_per_gallon)`. `dvFuelUsage` is in kWh
of fuel (line 270 comment "[kWh]", and `results/generator.jl` line 60 divides it by HHV to report
gallons), so the MPC objective prices fuel at $/gal per kWh, i.e. 40.7 times too high. Consequence for
the build plan: do not use `run_mpc` with a generator as the "REopt reference case" for the WP6 1 %
match test; use the annual `run_reopt` with `Generator.min_kw = max_kw = existing_kw` and the
sizing-path fuel cost instead, or patch line 170 locally (divide by HHV) in a scratch copy.

### 2.4 On/off binary, minimum turndown, "can run" rules

`add_binGenIsOnInTS_constraints` (lines 20–37):

    P_gen[t] <= S_max * u[t]                                             (line 22–24)
    f_min * S - P_gen[t] <= S_max * (1 - u[t])                           (lines 26–36; see which steps below)

With S fixed (EMS) this is the standard pair

    f_min * S * u[t] <= P_gen[t] <= S * u[t]

Which steps the turndown applies to (comment at line 25 and the branch at 26–36):

- on-grid analysis (`off_grid_flag = false`): only `ts in p.time_steps_with_grid` — **minimum
  turndown is NOT enforced during a deterministic outage** in the on-grid model;
- off-grid analysis: only `ts in p.time_steps_without_grid` (all steps are grid-less there).
- The stochastic-outage copy does enforce it inside outages (`outage_constraints.jl` lines 289–294, see 3.3).
- CHANGELOG line 1133: "removed redundant generator minimum turn down constraint"; line 575: "Do not enforce `CHP.min_turn_down_fraction` for outages" (the same policy for CHP).

For the EMS, enforce turndown in every step the genset runs; running a diesel set below ~30 % load
causes wet-stacking, and that is a real operating rule, not a modelling convenience.

`add_gen_can_run_constraints` (lines 39–51):

- if `only_runs_during_grid_outage`: `fix(dvRatedProduction[t, ts], 0.0)` for `ts in time_steps_with_grid` (41–43);
- if not `sells_energy_back_to_grid`: `fix(dvProductionToGrid[t, u, ts], 0.0)` for all export bins (46–50).

`add_gen_rated_prod_constraint` (54–58): `P_gen[t] <= S` (redundant with the on/off upper bound when S_max = S).

### 2.5 Hourly O&M when on (`add_generator_hourly_om_charges`, lines 66–90)

Linearisation of `om_hr * S * u[t]` with `dvOMByHourBySizeGen[t,ts] = y[t]`, using M = (existing_kw + max_kw) * om_hr:

    y[t] >= om_hr * S - M * (1 - u[t])          (71–75)
    y[t] <= om_hr * S                            (77–80)
    y[t] <= M * u[t]                             (82–85)
    TotalHourlyGenOMCosts = pwf_om * sum_t y[t] * h    (87–88)

With S fixed in the EMS this collapses to a constant coefficient on the binary: `cost += om_hr * S * h * u[t]`.
`om_cost_per_kwh` is a plain linear term: `sum_t om_kwh * h * P_gen[t]` (109–112).

### 2.6 What REopt does NOT have for the generator (and what to add in-house)

Checked with `grep -rn -i "min_up|min_down|minimum_up|start_cost|startup|ramp" src/constraints src/core/generator.jl src/mpc`:
no minimum up time, no minimum down time, no start/stop variables, no start cost, no ramp limit for
`Generator`. The only ramp constraint in the repo is for CHP (`src/constraints/chp_constraints.jl`
lines 196–209: `|P[t] - P[t-1]| <= ramp_fraction_per_hour * size / time_steps_per_hour`). The Product 1
report line 360 already says these are to be "written in-house". Standard unit-commitment
formulation to add (this is textbook, not from REopt; e.g. Rajan & Takriti 2005 and Carrión & Arroyo
2006 — citation from memory, UNVERIFIED here):

    u[t] - u[t-1] = v[t] - w[t]                       start v[t], stop w[t] ∈ {0,1}
    v[t] + w[t] <= 1
    sum_{k = t-UT+1 .. t} v[k] <= u[t]                 minimum up time UT steps
    sum_{k = t-DT+1 .. t} w[k] <= 1 - u[t]             minimum down time DT steps
    cost += c_start * v[t]  (+ c_stop * w[t])
    initial conditions: u[0] = measured state; if the set has been on for n < UT steps force u[1..UT-n] = 1 (and the mirror for down time)
    optional ramp: P_gen[t] - P_gen[t-1] <= R_up * h + S * v[t]  and the mirror for ramp-down

For a diesel set the per-start wear cost is the right lever; the R2-EMHASS note already describes
the same edge-detection pattern (`plan-work/research/R2-emhass.md` line 256).

### 2.7 Operating reserve during grid-less steps (off-grid model only)

`src/constraints/operating_reserve_constraints.jl` lines 3–62 (only when `off_grid_flag`, `reopt.jl`
lines 370–372). The genset's contribution to spinning reserve is capped by its on-state:
`dvOpResFromTechs[gen, ts] <= binGenIsOnInTS[gen, ts] * max_size` (lines 37–39), battery reserve by
headroom in energy and power (23–29), requirement = fraction of critical load served plus a fraction of
PV output (18–21), provided >= required (58–60). Useful pattern for an island-mode reserve rule
(cloud-cover headroom when the genset is off and the battery is the only firm source).

---

## 3. Outages

REopt has two mutually exclusive outage models (`src/core/electric_utility.jl` docstring lines 9–17 and
check at lines 331–340: "Cannot supply both"). A third component, the post-solve `simulate_outages` /
`backup_reliability` tools in `src/outagesim/`, is outside this note.

### 3.1 Deterministic single outage (Option 1): `outage_start_time_step`, `outage_end_time_step`

Inputs: integers, 1-based, inclusive of the end step (`electric_utility.jl` lines 59–63: a 3-hour outage
from 12 AM to 3 AM on 1 Jan at hourly steps is start = 1, end = 3). Both must be given or both zero
(328–330).

Step sets (`src/core/reopt_inputs.jl` lines 1227–1247, also used by the MPC via `src/mpc/inputs.jl` line 85):

    time_steps_without_grid = [start .. end]
    time_steps_with_grid    = [1 .. start-1] ++ [end+1 .. T]

What changes in grid-less steps (the whole handling is "which constraint set applies to which step"):

1. Fixings (`src/core/reopt.jl` lines 192–210; MPC copy `src/mpc/model.jl` lines 81–96):
   `G[t] = 0`, `G_b[t] = 0`, `X_pv[t] = 0`, `X_b[t] = 0` for t in without-grid.
2. Load balance switches from site load to critical load (`src/constraints/load_balance.jl`):
   - with grid, (8a) lines 22–37:
     `sum_tech pf[t]*P_tech[t] + D[t] + G[t] = sum_tech (C_tech[t] + X_tech[t] + K_tech[t]) + X_b[t] + G_b[t] + L[t]`
     (plus thermal/GHP terms that are zero in the MPC: `src/mpc/scenario.jl` lines 111–115 set the thermal loads to zeros)
   - without grid, (8b) lines 46–53 (on-grid analysis):
     `sum_tech pf[t]*P_tech[t] + D[t] = sum_tech (C_tech[t] + K_tech[t]) + L_crit[t]`
   - off-grid analysis, lines 55–68: the right-hand side is `L_crit[t] * served_fraction[t]` with an annual minimum served fraction.
3. Production split (`load_balance.jl` (4d) lines 76–91 with grid, (4e) 94–99 without):
   `C_tech[t] + K_tech[t] (+ X_tech[t] with grid) <= pf[t] * P_tech[t]`.
4. Battery SOC uses the no-grid recursion ((4g)-2, `storage_constraints.jl` lines 94–99) which drops the `G_b` term; the "charge alone must not overfill" guard (4h)-2 likewise (110–114).
5. Genset: allowed to run in without-grid steps regardless of `only_runs_during_grid_outage` (the fixing at `generator_constraints.jl` 41–43 is only over with-grid steps); minimum turndown is **not** applied in these steps for the on-grid model (2.4).
6. The utility bill, demand peaks and export benefit simply see `G[t] = 0` in those steps.

In the MPC this path works as read (`MPCElectricLoad.critical_loads_kw` exists, `src/mpc/structs.jl`
lines 10–13, default `nothing`; if an outage window is set without `critical_loads_kw` the load balance
would index `nothing` — UNVERIFIED, not executed). This is exactly the pattern for **scheduled
load-shedding windows** in Pakistan: known start/end steps inside the 48-hour horizon, served from
PV + battery + genset, import and export forced to zero.

Generalisation for the EMS (own extension): let `g[t] ∈ {0,1}` be a *data* flag (grid available). Then

    0 <= G[t] <= g[t] * G_max,   0 <= X[t] <= g[t] * X_max,   G_b[t] <= G[t]
    load balance RHS uses L[t] when g[t] = 1 and L_crit[t] (or L[t] minus sheddable blocks) when g[t] = 0

which reproduces REopt's two constraint sets with one formulation and lets several windows per horizon be
expressed without rebuilding the model.

### 3.2 Wrap-around helper

`src/core/utils.jl` lines 2–5: `time_step_wrap_around(ts) = ((ts - 1) mod (8760 * steps_per_hour)) + 1`.
Used by the stochastic model so an outage starting near the end of the year wraps into January
(`outage_constraints.jl` lines 42–43, 62, 158, 180). For a 48-hour horizon the equivalent is clamping an
outage that runs past the horizon end (do not wrap).

### 3.3 Stochastic multiple outages (Option 2): start steps, durations, probabilities

Inputs (`electric_utility.jl` lines 165–174, checks 322–346):

- `outage_start_time_steps::Vector{Int}` — the set `TZ` of candidate start steps; the model
  **minimises the maximum cost over start steps** (comment line 14: "minimax the expected outage cost,
  with max taken over outage start time, expectation taken over outage duration").
- `outage_durations::Vector{Int}` — in time steps (line 16: "duration of 4 equates to 1 hour if
  time_steps_per_hour is 4"); defines the scenario set `S = 1:length(outage_durations)` (line 174).
- `outage_probabilities` — one per duration, default uniform `1/|S|` (line 172); must sum to 1 within 1e-5 (344–346).
- `outage_time_steps = 1:maximum(outage_durations)` (line 173): the relative step index `ts` inside an outage.
- `Site.min_resil_time_steps` (line 154) — must be <= the longest duration (322–324).
- `Financial.value_of_lost_load_per_kwh` (`src/mpc/structs.jl` lines 23–25: default 1.00 per kWh; scalar or series, expanded to a series at `src/mpc/inputs.jl` line 105).
- v1.0.0 added `PV.outage_production_fraction` (CHANGELOG line 37) which scales PV inside outages (`outage_constraints.jl` lines 3–36).

Variables (`src/core/reopt.jl` lines 744–775; the MPC declares a subset, see 3.4), indexed `[s, tz, ts]`:

    U[s,tz,ts]   >= 0   unserved critical load, kW                          (dvUnservedLoad)
    Pmg[t,s,tz,ts]      MG rated production per tech                       (dvMGRatedProduction; >= 0 added at 161–163)
    Cmg[t,s,tz,ts] >= 0 tech-to-battery inside outage                      (dvMGProductionToStorage)
    Kmg[t,s,tz,ts] >= 0 curtailment inside outage                          (dvMGCurtail)
    Dmg[s,tz,ts]  >= 0  battery discharge inside outage                    (dvMGDischargeFromStorage)
    Emg[s,tz,ts], ts = 0..max_dur, >= 0   battery energy inside outage    (dvMGStoredEnergy)
    Cost_max[s]   >= 0  worst-start-time outage cost per duration scenario (dvMaxOutageCost)
    Fmg[t,s,tz]   >= 0  fuel used per outage                                (dvMGFuelUsed)
    Fmax[s], Cfmax[s] >= 0  worst-case fuel and fuel cost per scenario      (dvMGGenMaxFuelUsage, dvMGGenMaxFuelCost)
    umg[s,tz,ts]  ∈ {0,1} genset on inside outage                          (binMGGenIsOnInTS)
    binMGTechUsed[t], binMGStorageUsed ∈ {0,1}; dvMGsize[t] >= 0; upgrade-cost variables  (sizing only)

Constraints (all in `src/constraints/outage_constraints.jl`), with `w(tz, ts) = wrap(tz + ts - 1)`:

(a) Unserved load definition, lines 41–48:

    U[s,tz,ts] = L_crit[w] - sum_tech ( Pmg[t,s,tz,ts] * pf_outage[t][w] - Cmg[t,s,tz,ts] - Kmg[t,s,tz,ts] ) - Dmg[s,tz,ts]
    where pf_outage = (pf + unavailability) * outage_production_fraction (lines 3–36)

(b) Minimum resilience, lines 52–58: `U[s,tz,ts] <= 0` for `ts = 1..min_resil_time_steps` (if that is <= max duration).

(c) Worst-case cost and expectation, lines 60–67:

    Cost_max[s] >= pwf_e * sum_{ts=1..dur[s]} VoLL[w] * U[s,tz,ts]      for every tz      (61–63)
    ExpectedOutageCost = sum_s prob[s] * Cost_max[s]                                     (65–67)

   i.e. max over start times (via the >= for every tz) and expectation over durations. The remainder of
   `add_outage_cost_constraints` (69–128) is sizing-only microgrid upgrade cost.

(d) MG production bounds, lines 152–168: `Cmg + Kmg <= pf_outage * Pmg` (156–159); `Pmg >= 0` (161–163); `Pmg <= dvMGsize[t]` (165–167).

(e) Genset fuel inside each outage, lines 171–212:

    Fmg[t,s,tz] = a_gal * h * sum_{ts=1..dur[s]} (pf + unavail)[w] * Pmg[t,s,tz,ts]  +  b_gal * h * sum_{ts=1..dur[s]} umg[s,tz,ts]      (178–183; gallons; intercept not scaled by size, see 2.3)
    Fmg[t,s,tz] <= fuel_avail_gal                                                       (186–188)  one tank per outage
    Fmax[s] >= sum_t Fmg[t,s,tz]   for every tz;   ExpectedMGGenFuelUsed = sum_s prob[s] * Fmax[s]      (190–196)
    Cfmax[s] >= sum_t Fmg[t,s,tz] * fuel_cost_per_gallon;   ExpectedMGGenFuelCost = sum_s prob[s] * Cfmax[s]  (198–211)

(f) Genset on/off inside outages (big-M branch, lines 289–294; indicator branch 279–287 is CPLEX/Xpress only):

    Pmg[t,s,tz,ts] <= S_max * umg[s,tz,ts]
    Pmg[t,s,tz,ts] >= f_min * dvMGsize[t] - S_max * (1 - umg[s,tz,ts])
    binMGTechUsed[t] >= umg[s,tz,ts]                                                    (296–298)

(g) MG battery, lines 319–388:

    Emg[s,tz,0] <= E[tz]                       (321–323)  outage starts from the grid-optimal SOC at tz (≤, not =)
    Emg[s,tz,ts] = Emg[s,tz,ts-1] + h * ( eta_c * sum_t Cmg[t,s,tz,ts] - Dmg[s,tz,ts] / eta_d )      (326–331)
    E_cap >= Emg[s,tz,ts-1] + h * eta_c * sum_t Cmg[...]     "prevent simultaneous charge/discharge"  (334–338; see note)
    Emg[s,tz,ts] >= soc_min * E_cap   only if soc_min_applies_during_outages              (341–346; default false, electric_storage.jl 206)
    P_cap >= sum_t Cmg;  P_cap >= Dmg;  P_cap >= Dmg + sum_t Cmg;  E_cap >= Emg             (350–369)
    sum_t Cmg <= max_kw * binMGStorageUsed;  Dmg <= max_kw * binMGStorageUsed              (380–386; sizing)

   Note on lines 334–338: the constraint is indexed `[ts in p.time_steps_without_grid]` but refers to
   `s` and `tz`, which are not bound in that scope. It never raises because the stochastic model is only
   built when `time_steps_without_grid` is empty (the two options are exclusive), so the comprehension is
   empty. Treat it as dead code; the intended constraint is the (4h)-style guard from 4.2 applied to Emg.

(h) Renewables cannot be turned down inside an outage and a microgrid needs a dispatchable source or a
battery (lines 408–438) — sizing logic, not needed in the EMS.

Objective contribution (`reopt.jl` line 548; MPC `model.jl` line 222):
`Costs += ExpectedOutageCost + mgTotalTechUpgradeCost + dvMGStorageUpgradeCost + ExpectedMGFuelCost`.

### 3.4 Outages in the MPC module: present in `build_mpc!` but not usable in v1.0.1

`src/mpc/model.jl` lines 180–207 call the stochastic-outage constraint functions when
`outage_durations` is non-empty (with the comment `# TODO: random outages in MPC?` at line 180). But:

- `add_variables!` for the MPC (lines 281–309) comments out `dvMGTechUpgradeCost`, `dvMGStorageUpgradeCost`,
  `dvMGsize`, `binMGStorageUsed`, `binMGTechUsed` (lines 296–298, 305–306), while `add_outage_cost_constraints`
  (outage_constraints.jl 69–127), `add_MG_size_constraints` (131–149) and `add_MG_production_constraints`
  (165–167, `m[:dvMGsize]`) reference them — a KeyError on first use.
- line 194 calls `add_MG_fuel_burn_constraints(m,p)`, a function that does not exist; the real name is
  `add_MG_Gen_fuel_burn_constraints` (outage_constraints.jl line 171) — an UndefVarError.
- `test/runtests.jl` line 467: `# TODO test MPC with outages`; `src/mpc/results.jl` lines 52–57 have the
  outage results call commented out.

Conclusion: the deterministic window works in the MPC; the probabilistic outage model exists only in the
annual sizing model. The EMS must carry its own outage-aware MPC. The pattern to port is 3.3 with the
sizing parts removed (see 3.5).

### 3.5 Port recommendation for a 15-minute, 48-hour MPC with unreliable grid

Two layers, both linear with the existing binaries:

1. **Known windows** (announced load-shedding schedule, maintenance): the data flag `g[t]` of 3.1.
2. **Unannounced outages** (outage probability forecast per step, `p_out[t]`): two options, cheapest first.
   - *Reserve-SOC rule (LP, recommended for v1)*: require
     `E[t] >= soc_min * E_cap + R[t]` with `R[t] = min(E_cap - soc_min*E_cap, sum_{k=t+1..t+d(t)} max(0, L_crit[k] - pf[k]*P_pv) * h / eta_d)`
     computed from the forecast expected duration `d(t)` and scaled by a policy knob (e.g. only when
     `p_out[t] > threshold`). This is the "minimum SoC during outages" idea (`soc_min_applies_during_outages`)
     made time-varying; no scenario variables. The genset covers the rest: add `S * u_avail >= L_crit_peak - ...` only if a cold start is unacceptable.
   - *REopt-style scenario block (MILP, v2)*: a small set of start steps `TZ` inside the horizon (e.g. every
     2 hours), a few durations with probabilities, variables `U, Dmg, Emg, Pmg_gen, umg` as in 3.3 (drop
     `dvMGsize`, `binMGTechUsed`, `binMGStorageUsed`, upgrade costs), `Emg[s,tz,0] <= E[tz]`, the minimax cost
     `Cost_max[s] >= sum VoLL * U` over `tz`, expectation over `s`, fuel per outage limited by the tank.
     Size: |TZ| x |S| x max_dur copies of the battery and genset variables; with |TZ| = 24, |S| = 3,
     max_dur = 16 (4 h) that is ~1,150 scenario steps, fine for HiGHS at 15-minute resolution (UNVERIFIED
     on our hardware; benchmark in WP6). Use the expected-value (sum over tz weighted by p_out[tz]) instead
     of the minimax over tz if the minimax is too conservative — minimax is REopt's planning stance,
     not a dispatch stance.

---

## 4. Battery dispatch constraints the genset and outage blocks rely on

`src/constraints/storage_constraints.jl` (numbers in parentheses are REopt's own constraint labels):

- (4a) initial SOC, lines 40–54: `E[0] = soc_init_fraction * E_cap` (47–49), or `E[0] = E[T]` when `optimize_soc_init_fraction` (41–45). In the EMS `E[0]` is the measured SOC.
- (4n) `E[t] <= E_cap` (57–59); (4j) `E[t] >= soc_min_fraction * E_cap` (62–64); `D[t] <= P_cap` (67–69).
- (4d) `X_b[t] <= D[t]` (77–83) — export from the battery is part of the discharge.
- (4g)-1 with grid, lines 86–92: `E[t] = E[t-1] + h * ( eta_c * sum_tech C_tech[t] + eta_gc * G_b[t] - D[t] / eta_d )`
- (4g)-2 without grid, lines 94–99: same with no `G_b` term.
- (4h) "prevent simultaneous charge and discharge by limiting charging alone to not make the SOC exceed 100 %", lines 103–114:
  `E_cap >= E[t-1] + h * ( eta_c * sum C_tech[t] + eta_gc * G_b[t] )`. This is a heuristic, not a true
  exclusivity constraint; REopt relies on round-trip losses (eta_c * eta_d < 1) to make simultaneous
  charge and discharge sub-optimal. The EMS should keep it and add a binary `z[t]` (`sum C + G_b <= P_cap * z[t]`,
  `D <= P_cap * (1 - z[t])`) only if negative export prices or curtailment incentives appear (not the case in Pakistan today).
- (4i) `P_cap >= D[t] + sum C_tech[t] + G_b[t]` (117–121) — one inverter rating for charge plus discharge.
- `can_grid_charge = false` → `G_b[t] = 0` fixed (124–128).
- (4k) average SOC floor over the horizon (131–137); (4l) `fixed_soc_series_fraction` ± tolerance (140–148) — this is the hook for **replaying an inverter's own TOU-slot schedule** inside the same model (counterfactual).
- (8c) `G[t] >= G_b[t]` (331–334): grid-to-battery is a part of the metered import.

Efficiency defaults (MPC, `src/mpc/structs.jl` 246–247): `eta_c = eta_d = 0.96 * 0.975^0.5 = 0.9480`;
`soc_min = 0.2` (248), `soc_init = 0.5` (249), `can_grid_charge = true` (250).

---

## 5. Battery degradation (`src/constraints/battery_degradation.jl`, inputs in `src/core/energy_storage/electric_storage.jl`)

### 5.1 Inputs (`electric_storage.jl` lines 153–160; docstring 2–70)

    calendar_fade_coefficient  k_cal = 1.16e-3
    cycle_fade_coefficient     k_cyc = [2.46e-5]      (vector: one per DoD segment j)
    cycle_fade_fraction        f_j   = [1.0]          (per-step energy limit per segment, fraction of E_cap)
    time_exponent              alpha = 0.428
    installed_cost_per_kwh_declination_rate r_g = 0.05
    maintenance_strategy       "augmentation" (default) | "replacement"
    maintenance_cost_per_kwh   optional vector, length 365*analysis_years - 1

CHANGELOG line 223: "Changed default inputs for degradation module to match parameters for **NMC-Gr
Kokam 75Ah** cell". These are **not LFP** coefficients; the EMS must source LFP calendar/cycle
coefficients elsewhere (the Product 1 report's WP6 names BLAST-Lite). Docstring lines 66–70 warn the
parameters are "expected to be reasonable only within the range of conditions tested".

Default maintenance price per day (lines 83–92; docstring 24–28):
`maintenance_cost_per_kwh[d] = installed_cost_per_kwh * (1 - r_g)^(d/365) / (1 + r_d)^(d/365)`, with
`r_d` the owner discount rate.

### 5.2 Variables (lines 4–13), days `d = 1..365*years`, segments `j = 1..J`

    Eavg[d] >= 0           daily average stored energy, kWh
    Eplus_sum[d,j] >= 0    energy charged in day d through segment j, kWh
    Eminus_sum[d,j] >= 0   energy discharged in day d through segment j, kWh
    EFC[d] >= 0            daily equivalent full cycles (in kWh units, see note)
    SOH[d]                 state of health, kWh (free sign; "NOTE SOH can be negative", line 111)
    Pc[ts,j], Pd[ts,j] >= 0   per-step charge and discharge power split by segment

### 5.3 Constraints (lines 16–66)

    Eavg[d] = (1 / steps_per_day) * sum_{ts in day d} E[ts]                                   (31–33)
    EFC[d]  = sum_j ( Eplus_sum[d,j] + Eminus_sum[d,j] ) / 2                                  (35–37)  (defined, but not used by the SOH recursion)
    sum_j Pc[ts,j] = sum_tech C_tech[ts] + G_b[ts]                                             (40–43)
    sum_j Pd[ts,j] = D[ts]                                                                     (46–47)
    Eplus_sum[d,j]  = h * sum_{ts in day d} Pc[ts,j];   Eminus_sum[d,j] = h * sum_{ts in day d} Pd[ts,j]      (50–51)
    h * Pc[ts,j] <= f_j * E_cap;   h * Pd[ts,j] <= f_j * E_cap                                (55–65)  segment energy limits (a piecewise DoD proxy)

SOH recursion (lines 103–113):

    SOH[1] = E_cap
    SOH[d] = SOH[d-1] - h * ( k_cal * alpha * Eavg[d-1] * d^(alpha-1) + sum_j k_cyc[j] * Eminus_sum[d-1,j] )     d = 2..D

Docstring vs code: the docstring (electric_storage.jl lines 52–56) writes the calendar term as
`(1/2) k_cal Eavg[d-1] / sqrt(d)`, i.e. alpha = 0.5 hard-coded; the code uses the general derivative
`alpha * d^(alpha-1)` with alpha = 0.428. The factor `h` multiplies the whole daily decrement even though
`Eminus_sum` is already an energy; keep that quirk in mind when re-deriving coefficients from data —
calibrate against the code's form, not the docstring.

### 5.4 Cost of degradation (lines 116–223)

- **augmentation** (210–220): `degr_cost = sum_{d=2..D} maintenance_cost_per_kwh[d-1] * (SOH[d-1] - SOH[d])`,
  residual value 0. Linear: pay for each kWh of lost capacity at that day's present-worth price.
- **replacement** (116–208): monthly binaries `binSOHIndicator[m]` (1 while SOH >= 0.8 E_cap, lines 169–172
  with big-M = `24 * max(load)` or `max_kwh`, 160–166), a change indicator (176–177), a linearised product
  `dvSOHChangeTimesEnergy[m] = E_cap * change[m]` (182–184), a replacement-count cost curve per month
  (186–202) and a residual value (199–201). Objective: `Costs += degr_cost - residual_value` (`reopt.jl` 563).

### 5.5 Port for a 48-hour MPC (own guidance)

A multi-year SOH recursion is a sizing construct. For dispatch, linearise it to a marginal wear price:

    wear_cost = sum_t ( c_cyc * D[t] * h  +  c_cal * E[t] * h )
    c_cyc = (cost_per_kWh_replacement) * k_cyc_LFP           [currency per kWh discharged]
    c_cal = (cost_per_kWh_replacement) * k_cal_LFP * alpha * age_days^(alpha-1) / 24   [currency per kWh-hour of stored energy; penalises holding a high SOC when idle]

Keep the segment idea (f_j) only if a DoD-dependent cycle coefficient is available for the LFP cell;
otherwise one segment. Use `EFC` from 5.3 as a reported metric. The Product 1 report already budgets
"wear" in WP6; this is the formula to put there.

---

## 6. Electric tariff, demand charges and export

### 6.1 Sizing-model `ElectricTariff` (`src/core/electric_tariff.jl`)

Struct lines 9–41. Fields relevant to the EMS: `energy_rates[ts, tier]`, `monthly_demand_rates[mth, tier]`,
`time_steps_monthly` (12 step sets), `tou_demand_rates[r, tier]`, `tou_demand_ratchet_time_steps` (one
step set per ratchet r), `demand_lookback_months`, `demand_lookback_percent`, `demand_lookback_range`,
`fixed_monthly_charge`, `annual_min_charge`, `min_monthly_charge`, `export_rates::Dict{Symbol,Array}`,
`export_bins`, coincident-peak fields. Ways to build it (constructor 95–380): URDB label/response,
`tou_energy_rates_per_kwh` (a full 8760*steps series, lines 149–162), `monthly_energy_rates` (12 values,
164–194), `blended_annual_energy_rate` (196–214). Tiers are collapsed with `remove_tiers` (261–271).

Export bins (docstring 74–85; code 306–348): NEM at `-0.999 * energy_rate` (lines 161, 193, 213, 232 — the
0.999 makes buying-and-selling in the same step weakly sub-optimal), WHL at `-wholesale_rate`, EXC at
`-export_rate_beyond_net_metering_limit`. "Only one of NEM and Wholesale can be exported into due to
the binary constraints. Excess can be exported into in the same time step as NEM." (lines 80–81).

### 6.2 MPC tariff `MPCElectricTariff` (`src/mpc/structs.jl` lines 48–73, constructor 152–220)

Per-step inputs with the horizon's length (docstring 101–150):

    energy_rates[ts]                     required
    monthly_demand_rates                 default [0.0] (157); time_steps_monthly default = one "month" = all steps (158)
    monthly_previous_peak_demands        default [0.0] (159)   ← month-to-date peak carried into the horizon
    tou_demand_rates, tou_demand_time_steps, tou_previous_peak_demands   (161–164; equal lengths)
    wholesale_rate                       scalar or series (178–182)
    net metering flag comes from ElectricUtility.net_metering_limit_kw > 0 (scenario.jl 103; nem rate = -0.999 * energy_rates, line 175)
    fixed_monthly_charge = annual_min_charge = min_monthly_charge = 0 (167–169); one tier everywhere (199, 204, 208)
    no EXC bin in MPC "b/c MPC does not track annual energy exported" (194)

### 6.3 Demand-charge constraints (`src/constraints/electric_utility_constraints.jl`)

Monthly (flat) peak, `add_monthly_peak_constraint` lines 246–292:

    M_mon[m] >= G[t]      for every t in month m                                   (261–264; single tier)
    tiers (267–291): binaries binMonthlyDemandTier[m, tier] with ordering and "fill the lower tier first" big-M constraints

TOU ratchet peak, `add_tou_peak_constraint` lines 295–328:

    M_tou[r] >= G[t]      for every t in ratchet r's step set                      (297–300)

MPC "previous peak" lower bounds (`src/mpc/constraints.jl`):

    M_mon[m] >= monthly_previous_peak_demands[m]                                   (2–7)
    M_tou[r] >= tou_previous_peak_demands[r]                                       (10–15)

Charges (`add_elec_utility_expressions` lines 453–525):

    TotalEnergyChargesUtil = pwf_e * h * sum_t c_e[t] * G[t]                       (463–466)
    DemandTOUCharges       = pwf_e * sum_r tou_rate[r] * M_tou[r]                  (468–475)
    DemandFlatCharges      = pwf_e * sum_m month_rate[m] * M_mon[m]                (477–484)
    TotalFixedCharges      = pwf_e * fixed_monthly_charge * 12                     (488)
    TotalElecBill = energy + demand + export benefit (negative) + fixed + coincident-peak + 0.999 * MinChargeAdder   (511–518)

Because the previous peak is a lower bound on `M_mon`, the objective pays `rate * max(previous_peak,
horizon peak)`: a horizon that stays under the month-to-date peak pays nothing extra, one that exceeds it
pays only the increment. This is exactly the MDI logic the EMS needs; carry the month-to-date MDI into
every 15-minute re-solve.

Lookback (`add_demand_lookback_constraints` lines 406–437; only when `demand_lookback_percent > 0`, `reopt.jl` 394–396):

    range form (410–422):   PL[m] >= G[t]  for t in each of the previous `demand_lookback_range` months;  M_mon[m] >= pct * PL[m]
    months form (424–436):  PL >= M_mon[lm]  for lm in demand_lookback_months;           M_mon[m] >= pct * PL

Pakistan's MDI floor ("billed MDI is at least 50 % of sanctioned load" — tariff detail is in the Product
1 report, not restated here) is a constant lower bound `M_mon[m] >= floor_kW`, a special case of the same
construction and simply a constant in the objective.

Coincident peak (440–450): `dvPeakDemandCP[prd] >= G[t]` over listed steps, priced per kW.

Measurement note for porting: REopt's peak is the per-step kW, which at 15-minute steps is stricter than
a 30-minute integrated MDI. Model MDI either as the 30-minute rolling mean
`M_mon >= (G[t] + G[t+1]) / 2` for every t (two constraints per step, still linear) or keep the per-step
form as a conservative bound; decide per DISCO meter type.

### 6.4 Export, net metering and the simultaneous import/export binary

`add_export_constraints` lines 2–236:

- transmission limit on `imports + exports` per step (5–10); export plus curtailment <= production (13–17).
- NEM annual rule "can't sell more than you purchase" (35–42) is **skipped for MPC** (34: "to avoid
  over-constraining dispatch since this is an annual energy limit"). Under legacy 1:1 net metering the
  EMS must track the cumulative annual net itself (state carried between solves).
- When NEM and WHL both exist and `net_metering_limit_kw != interconnection_limit_kw`, a binary pair
  `binNEM + binWHL = 1` (193) chooses the bin for the whole horizon, with big-M bounds on export (137–142,
  221–226). For Pakistan net billing (fixed Rs/kWh export) use one WHL bin with `wholesale_rate` = the
  net-billing rate and `net_metering_limit_kw = 0`: no binaries.

`add_simultaneous_export_import_constraint` lines 339–365 (only when `allow_simultaneous_export_import = false`
and export bins exist; `reopt.jl` 386–388; `mpc/model.jl` 155–157). Big-M branch (353–364), binary `n[t]` = "no grid purchases":

    G[t] + G_b[t] <= M * (1 - n[t])
    X_pv[t] + X_b[t] <= M * n[t]
    M = max(L) + max(thermal loads, zero in MPC) + sum of storage max_kw                 (354)

`electric_utility.jl` line 7 says when it matters: "Set to false if the export rate is greater than the
cost of energy (otherwise, REopt will export before meeting site load)". With net billing in Pakistan the
export credit is below the import price in every step, so the LP never imports and exports together and
the binary can be left out (keep the option behind a flag for a future tariff where it is not true).

Grid draw / export limits per step (`src/mpc/constraints.jl` 18–40; `MPCLimits`, structs 398–401):
`G[t] <= grid_draw_limit[t]`, `X_pv[t] + X_b[t] <= export_limit[t]`. This is the hook for a sanctioned-load
cap, a DISCO export cap (e.g. export limited to the net-metering licence kW), and for
`g[t]`-style outage flags (set the limits to 0 in outage steps instead of fixing variables).

---

## 7. The MPC module and how a rolling horizon is driven

### 7.1 Structure

- `src/mpc/scenario.jl` lines 2–18: `MPCScenario` = settings, PVs (`MPCPV`: `size_kw`, `production_factor_series`,
  structs 38–45), one `MPCElectricStorage`, `MPCElectricTariff`, `MPCElectricLoad` (`loads_kw`,
  `critical_loads_kw`), `ElectricUtility` (same struct as the sizing model, built without lat/lon so
  `is_MPC = true`, electric_utility.jl 203), `MPCFinancial` (VoLL only), `MPCGenerator`, dummy thermal
  loads, `MPCLimits`. Minimum JSON keys: `ElectricLoad`, `ElectricTariff` (docstring 45–47).
- `src/mpc/inputs.jl` lines 45–125: `time_steps = 1:length(loads_kw)` (47) — **the horizon is whatever
  length the series have**; `hours_per_time_step` from `Settings.time_steps_per_hour` (48); `months =
  1:length(monthly_demand_rates)` (50); `ratchets = 1:length(tou_demand_ratchet_time_steps)` (111); all
  present-worth and levelisation factors 1.0 (78–83); generator production factor = ones, fuel price per
  kWh = $/gal ÷ HHV (160–166).
- `docs/src/mpc/examples.md` line 2: "the MPC model can be built for an arbitrary time length, or
  'horizon' ... The MPC model also requires the user to provide load and resource forecasts as inputs".

### 7.2 `build_mpc!` order (`src/mpc/model.jl` lines 78–228)

1. `add_variables!` (79; body 231–310): sizes are constants (`m[:dvSize] = p.existing_sizes`, 256), storage
   kW/kWh fixed (258–261), `MinChargeAdder = 0` (264), genset binaries only if a generator exists (266–273),
   `binNoGridPurchases` only if simultaneous import/export is disallowed (275–279), outage variables (281–309).
2. Fix import, grid-charge and exports to zero in `time_steps_without_grid` (81–96).
3. Storage constraints or zero-fixings when size is zero (98–124); `(8c)` (122–124).
4. Production split (126), no-turndown techs (128–132), load balance (134), grid draw limits (136–138),
   export constraints and export limits (140–145), monthly and TOU peaks (147–153), simultaneous
   import/export binary (155–157).
5. Generator: `add_gen_constraints` (164), per-kWh O&M (165–168), fuel cost (169–171, the units bug of 2.3).
6. Utility bill expressions (176), previous-peak lower bounds (177–178).
7. Outages (180–207, non-functional, 3.4).
8. Objective expression `Costs = TotalPerUnitProdOMCosts + TotalPerUnitHourOMCosts + TotalFuelCosts + TotalElecBill (+ outage terms)` (210–223).

`run_mpc` (34–68): objective `Min Costs`, or `Min Costs - sum_t E[t] / T` when `Settings.add_soc_incentive`
(37–43) — a tiny push to end the horizon with a full battery (the default of `add_soc_incentive` is
recorded in the appendix). Status mapping 49–57; results via `mpc_results` (62; `src/mpc/results.jl`
12–59: `ElectricStorage`, `ElectricTariff`, `ElectricUtility`, `PV`, `Generator`, `Costs`, plus `status` and
`solver_seconds`).

Test (`test/runtests.jl` 468–481, HiGHS): checks grid-to-load stays under the previous peaks 98/97 kW in
the two TOU ratchets, no PV export, and the `grid_draw_limit` of 90 kW at step 10.

### 7.3 There is no receding-horizon loop in REopt.jl

`run_mpc` solves **one** horizon and returns. Rolling is the caller's job. From the code, the state that
must be carried from one solve to the next and re-supplied as inputs is:

| Carried state | REopt input it feeds | Where it is read |
|---|---|---|
| measured battery SOC | `ElectricStorage.soc_init_fraction` (→ `E[0]`) | storage_constraints.jl 47–49 |
| month-to-date peak kW | `ElectricTariff.monthly_previous_peak_demands` | mpc/constraints.jl 2–7 |
| TOU-window-to-date peaks | `ElectricTariff.tou_previous_peak_demands` | mpc/constraints.jl 10–15 |
| load forecast | `ElectricLoad.loads_kw` (and `critical_loads_kw`) | load_balance.jl |
| PV forecast | `PV.production_factor_series` (kW per kW) | mpc/inputs.jl 151–157 |
| prices | `ElectricTariff.energy_rates`, `wholesale_rate` | electric_utility_constraints.jl 463–466 |
| known outage window | `ElectricUtility.outage_start_time_step/end` | reopt_inputs.jl 1227–1247 |
| grid and export caps | `Limits.grid_draw_limit_kw_by_time_step`, `export_limit_kw_by_time_step` | mpc/constraints.jl 18–40 |
| genset state (on/off, hours run), tank litres | **not carried** (no min up/down; one `fuel_avail_gal`) | — |
| annual NEM net energy | **not carried** (NEM annual rule skipped) | electric_utility_constraints.jl 34 |

EMS loop (own design, consistent with the product shape in the brief): every 15 minutes, read telemetry
(SOC, genset state and hours since last switch, tank, month-to-date MDI, TOU-to-date peaks, cumulative
net-metering balance), refresh forecasts (load, PV, outage probability) for 192 steps, build the MILP from
the sections above with the carried state as constants, solve (HiGHS; warm-start from the previous
solution shifted by one step), apply the first step (Mode A: translate the first day's plan into inverter
TOU slots; Product 2: send the 15-minute setpoints), log the plan for the verification engine.

### 7.4 Multi-node MPC

`src/mpc/model_multinode.jl` (214 lines) and `run_mpc(m, ps::Vector{MPCInputs})` (test 483–494) build one
model for several sites sharing nothing but the solver — not needed; one model per site is the EMS design.

---

## 8. Load balance and objective, assembled for the port

Per step t (h = 0.25 h), with the genset, PV, one battery, grid import G and exports X:

    pf[t] * P_pv + P_gen[t] + D[t] + G[t]  =  C_pv[t] + C_gen[t] + K[t] + X_pv[t] + X_b[t] + G_b[t] + L[t]        (grid up; load_balance.jl 22–37)
    pf[t] * P_pv + P_gen[t] + D[t]         =  C_pv[t] + C_gen[t] + K[t] + L_crit[t]                              (grid down; 46–53)
    C_pv[t] + K[t] + X_pv[t] <= pf[t] * P_pv ;  C_gen[t] + X_gen[t] <= P_gen[t]                                  (76–99)
    G_b[t] <= G[t]  (8c) ;  X_b[t] <= D[t]  (4d)
    battery (4g), (4h), (4i), (4j), (4n) from section 4;  genset (2.3), (2.4), (2.6);  demand peaks (6.3);  optional (6.4)

Objective (MPC form, `src/mpc/model.jl` 210–223 with `electric_utility_constraints.jl` 463–518 and
`generator_constraints.jl` 109–115), all pwf = 1:

    minimise  sum_t h * c_e[t] * G[t]                              energy charge
            + sum_m rate_mon[m] * M_mon[m] + sum_r rate_tou[r] * M_tou[r]    demand charges (peaks bounded below by month-to-date values)
            - sum_t h * c_x[t] * (X_pv[t] + X_b[t])                export credit
            + sum_t c_f * F[t]                                      fuel (F in kWh fuel, c_f per kWh fuel)
            + sum_t h * (om_kwh * P_gen[t] + om_hr * S * u[t])      genset O&M
            + fixed charges (constant)
            [+ wear_cost of 5.5, + start costs of 2.6, + VoLL * unserved of 3.3/3.5, - terminal SOC value]

Terminal condition: REopt's only end-of-horizon device is the optional `add_soc_incentive` term
(7.2). For a 48-hour receding horizon prefer an explicit terminal value `- v_end * E[T]` with `v_end` =
the expected next-day peak price times `eta_d`, or a terminal SOC floor; both are linear.

## 9. Big-M inventory (values REopt uses with HiGHS)

| Constraint | big-M | Source |
|---|---|---|
| genset on/off, turndown | `p.max_sizes[t]` (= S in MPC) | generator_constraints.jl 23, 29, 34; outage_constraints.jl 290, 293 |
| genset hourly O&M | `(existing_kw + max_kw) * om_hr` | generator_constraints.jl 73, 83 |
| no simultaneous import/export | `max(L) + max(thermal loads) + sum(storage max_kw)` | electric_utility_constraints.jl 354 |
| NEM/WHL export choice | `sum(L)` (sum of the load series!) | electric_utility_constraints.jl 138, 141, 222, 225 |
| demand tiers | `2 * max(100 * L + thermal) + storage kW`, capped by tier limits | electric_utility_constraints.jl 541–566 |
| degradation replacement | `24 * max(L)` or `max_kwh` | battery_degradation.jl 160–166 |
| MG upgrade costs | `cost_slope * max_size` | outage_constraints.jl 76–81 |

For the port use the tightest honest values: S for the genset, the sanctioned load (or the inverter
rating) for the import/export binary, P_cap for a charge/discharge binary. Tight big-Ms are what keep a
192-step MILP with ~200–600 binaries inside the 30-second budget of WP6.

## 10. Licence: Apache-2.0 obligations and the REopt trademark

### 10.1 What the files say

- `LICENSE`: Apache License, Version 2.0, January 2004, full text, sections 1–9, "END OF TERMS AND CONDITIONS" (the appendix "How to apply" block is not included; no copyright line in the LICENSE file itself).
- `NOTICE` (14 lines): "Copyright 2023 Alliance for Sustainable Energy, LLC" followed by the standard
  "Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
  compliance with the License. ..." boilerplate.
- Every source file header (94 files): "REopt®, Copyright (c) Alliance for Energy Innovation, LLC. See also
  https://github.com/NatLabRockies/REopt.jl/blob/master/LICENSE." — note the two different LLC names
  (10.3).

### 10.2 Obligations that apply to the EMS (quoted from `LICENSE`)

Section 4, "Redistribution": "You may reproduce and distribute copies of the Work or Derivative Works
thereof in any medium, with or without modifications, and in Source or Object form, provided that You meet
the following conditions:
  (a) You must give any other recipients of the Work or Derivative Works a copy of this License; and
  (b) You must cause any modified files to carry prominent notices stating that You changed the files; and
  (c) You must retain, in the Source form of any Derivative Works that You distribute, all copyright,
      patent, trademark, and attribution notices from the Source form of the Work, excluding those notices
      that do not pertain to any part of the Derivative Works; and
  (d) If the Work includes a "NOTICE" text file as part of its distribution, then any Derivative Works that
      You distribute must include a readable copy of the attribution notices contained within such NOTICE
      file ... in at least one of the following places: within a NOTICE text file distributed as part of the
      Derivative Works; within the Source form or documentation, if provided along with the Derivative Works;
      or, within a display generated by the Derivative Works, if and wherever such third-party notices
      normally appear."
"You may add Your own copyright statement to Your modifications and may provide additional or different
license terms and conditions for use, reproduction, or distribution of Your modifications, or for any such
Derivative Works as a whole, provided Your use, reproduction, and distribution of the Work otherwise
complies with the conditions stated in this License."

Section 3 grants a patent licence that terminates if "You institute patent litigation against any entity
... alleging that the Work or a Contribution incorporated within the Work constitutes direct or contributory
patent infringement".

Section 6, "Trademarks": "This License does not grant permission to use the trade names, trademarks,
service marks, or product names of the Licensor, except as required for reasonable and customary use in
describing the origin of the Work and reproducing the content of the NOTICE file."

Sections 7–8: "AS IS" basis, no warranty, no liability.

Practical reading for the two repositories (not legal advice):

1. The EMS will be a proprietary SaaS; Apache-2.0 permits that (the whole-work licence may differ,
   section 4 last paragraph). "Distribution" in the licence sense happens when the backend ships to a
   customer site (Product 2 edge box) or when source is handed to a contractor; a hosted service alone is
   not distribution under Apache-2.0, but plan as if it were, since the edge product will be shipped.
2. If any REopt code is translated line-for-line into Python (even the 12-line fuel-curve helper), treat
   the resulting file as a Derivative Work: keep a `LICENSE-APACHE-2.0` copy and a `NOTICE` in the backend
   repo that reproduces the REopt NOTICE text ("Copyright 2023 Alliance for Sustainable Energy, LLC ...")
   and the per-file attribution ("Portions derived from REopt.jl, https://github.com/NatLabRockies/REopt.jl,
   Apache-2.0, Copyright (c) Alliance for Energy Innovation, LLC; modified by OnyxLeaf"), and mark modified
   files (4(b)).
3. If only the mathematics is re-implemented from this note (the recommended path — the formulations are
   standard MILP constraints and the code is Julia/JuMP, which does not port mechanically to scipy.milp),
   the obligations above are arguably not triggered, but add the same attribution line in the optimiser
   module docstring and in the product documentation anyway: it costs nothing and removes any argument.
4. Do not vendor the clone into either repo; it carries SAM/SSC and solver binaries under `src/sam/` whose
   licences were not reviewed here (UNVERIFIED), and the task rules treat the clone as untrusted data.

### 10.3 Trademark note (REopt®)

- The mark: README line 1 "REopt® Julia package"; the ® appears in every file header. The NLR product
  page (https://www.nlr.gov/reopt/, fetched 6 Nov 2026) marks "REopt®" and "REopt Lite®" and says the lab
  "is a national laboratory of the U.S. Department of Energy" "operated under Contract No. DE-AC36-08GO28308";
  it has no licence text and no citation format.
- USPTO record (from the aggregator gleanmark.com, fetched 6 Nov 2026; USPTO TSDR itself did not render —
  "TSDR is having availability issues" — so the record is **UNVERIFIED against the primary source**):
  word mark REOPT (standard characters; pseudo-mark "RENEWABLE ENERGY OPTIMIZATION"), serial 88377665,
  registration no. 6022044, filed 9 Apr 2019, registered 31 Mar 2020, status Registered, owner listed as
  "Alliance for Energy Innovation, LLC, 15013 Denver West Parkway, Golden, CO 80401", International Class 42
  ("Technology consultation and research in the field of energy ... energy optimizing web tool, namely
  providing temporary use of non-downloadable software" for optimising energy systems for buildings,
  campuses, communities and microgrids). Related registrations surfaced by the same search: REOPT LITE
  (reg. no. 6022049 or 6022053 — the aggregators disagree) and the combined mark "REOPT RENEWABLE ENERGY
  INTEGRATION AND OPTIMIZATION" (reg. no. 6022037). The web search also mentioned an ownership assignment
  processed 11 Mar 2026 on one record; which record and to whom is UNVERIFIED.
- Lab and operator names: the U.S. Department of Energy renamed the National Renewable Energy Laboratory
  (NREL) the **National Laboratory of the Rockies (NLR)**, announced 1 December 2025, "effective immediately",
  "reflected in all public communications and official correspondence" (energy.gov article fetched
  6 Nov 2026). The REopt.jl CHANGELOG records the fallout (line 134 "Update links that broke with NLR domain
  change and update other references to NREL"; line 67 env vars renamed `NLR_DEVELOPER_API_KEY`). The
  operating contractor appears in two names in the repo: "Alliance for Sustainable Energy, LLC" (NOTICE,
  2023) and "Alliance for Energy Innovation, LLC" (every 2025–2026 file header and the USPTO owner line).
  The rename of the LLC is consistent across sources but the date and legal basis are UNVERIFIED (the
  nrel.gov editorial page did not resolve on 6 Nov 2026: DNS failure).
- What this means for the EMS:
  1. Never use "REopt", "REopt Lite", the ® mark or NLR/NREL logos in the product name, UI, marketing or
     domain names; never imply endorsement ("powered by REopt", "REopt-certified").
  2. Customary-origin statements are allowed by section 6: in the documentation and NOTICE write
     "The dispatch formulation was informed by the open-source REopt.jl model (National Laboratory of the
     Rockies, Apache-2.0)". Keep the registered symbol out of our own text except when quoting their notice.
  3. If REopt.jl is ever run as a benchmark inside the product (not planned), its output must not be labelled
     as a REopt result to customers without checking the trademark owner's guidelines (none found; UNVERIFIED).
  4. The Product 1 report line 367 already says "do not use the 'REopt' trademark"; this section gives the
     registration details behind that line.

## 11. Verified numbers and what could not be verified

Verified by reading the clone at commit 35abb019 (6 Nov 2026):

- REopt.jl v1.0.1 is Apache-2.0 with a NOTICE file; 94 Julia source files; last commit 28 Oct 2026.
- The generator fuel curve is two-point linear (full-load and half-load HHV efficiencies) → slope + intercept
  (`utils.jl` 652–665); default efficiencies 0.322/0.322 → intercept 0; MPC defaults 0.3233/0.3233 and
  `min_turn_down_fraction = 0.0` with a TODO.
- The intercept is per kW rated but is not multiplied by the size in the generator constraints
  (`generator_constraints.jl` 11; `outage_constraints.jl` 181–182); the CHP path does scale it (237–249).
- MPC fuel cost multiplies kWh of fuel by a $/gallon price (`mpc/model.jl` 169–171): a 40.7x units error as read.
- No minimum up/down time, start cost or ramp for `Generator`; CHP has a ramp rate (chp_constraints.jl 196–209).
- Minimum turndown is not enforced in deterministic-outage steps of an on-grid analysis (`generator_constraints.jl` 25–36) but is enforced inside stochastic outages (`outage_constraints.jl` 289–294).
- Stochastic outages: start steps x durations x probabilities, minimax over start, expectation over duration, VoLL-priced unserved load, optional min-SOC-in-outage, one tank per outage, PV outage production fraction.
- MPC with stochastic outages is wired in `build_mpc!` but cannot run (missing variables and a misspelled function name, `mpc/model.jl` 194 vs `outage_constraints.jl` 171).
- Demand charges: per-step peak variables bounded below by every step's import and by the month-to-date and TOU-to-date previous peaks; lookback in two forms; tiers via binaries.
- Simultaneous import/export: one binary per step with M = max load + storage kW; recommended off when export price < import price.
- Degradation: daily SOH recursion with calendar (k_cal, alpha) and cycle (k_cyc per segment) terms; defaults are NMC (Kokam 75Ah), not LFP; docstring and code differ in the calendar exponent.
- HiGHS is not in `INDICATOR_COMPATIBLE_SOLVERS`; the big-M branches are the ones that run.

UNVERIFIED / not done:

- Nothing was executed in Julia (no Julia installed; the clone is untrusted data). The two "bugs" above are
  readings of the code, not reproduced failures.
- USPTO registration details come from a third-party aggregator; TSDR was unavailable.
- The date and basis of the "Alliance for Energy Innovation" rename.
- Licences of the binaries under `src/sam/` (SSC, HiGHS, SCIP, OR-tools DLLs) were not reviewed.
- The unit-commitment literature citations in 2.6 are from memory.
- Solve-time estimates for the scenario-based outage block (3.5) are not benchmarked.

## 12. Corrections and extensions to the two planning documents

- Product 1 report line 321 ("REopt.jl ... Yes: fuel curve, on/off, turndown") is right, with two caveats
  now documented: the intercept-scaling omission and the absent min up/down (the report's line 360 already
  plans those in-house).
- Product 1 report line 346 says `src/mpc/model.jl` "holds an MPC module; port these patterns" — the MPC is
  a single-horizon solve with no rolling loop (7.3) and its outage path is non-functional (3.4); the
  patterns worth porting are the previous-peak lower bounds, the per-step grid/export limits, the fixed
  sizes, and the deterministic without-grid step set.
- Product 1 report WP6 acceptance "matches a REopt reference case within 1 %" (line 568): build the
  reference with the annual `run_reopt` at fixed sizes, not `run_mpc`, because of the MPC fuel-cost units
  issue (2.3). A reference without a generator (PV + battery + TOU + demand charge) is unaffected and is
  the cleaner first check.
- Degradation coefficients in REopt are NMC; the report's BLAST-Lite reference for LFP stands (5.1).
- `U2-backends.md` line 251 (replay against python-microgrid and a REopt reference case) — same caveat as above.

## 13. Recommendations for the build plan

1. Port the math, not the code: implement sections 2.3 (corrected), 2.4, 2.6, 4, 6.3, 6.4 (flagged off), 8 in
   one `optimiser/model.py` using `scipy.optimize.milp` (HiGHS) with explicit sparse matrices, or PuLP if the
   team wants named constraints; keep the REopt line references in docstrings for audit.
2. Genset block v1: on/off + turndown + intercept scaled by S + min up/down + start cost + tank limit + hours-run
   carry-over. Default Pakistani curve from the two-point method with measured full/half-load litres per hour
   (see appendix for the conversion) rather than REopt's US-gallon defaults.
3. Outages v1: data flag `g[t]` for announced windows plus the reserve-SOC rule; v2: the scenario block of 3.5,
   benchmarked in WP6; keep REopt's minimax as an option, default to expectation over start times.
4. MDI/TOU demand: previous-peak lower bounds carried per solve; choose per-step or 30-minute-mean measurement per DISCO meter.
5. Export: one WHL bin at the net-billing rate for new customers; NEM bin with an annual-net state for legacy
   net-metering customers; simultaneous import/export binary behind a flag, default off.
6. Wear: marginal cycle and calendar prices (5.5) with LFP coefficients from BLAST-Lite; keep EFC as a metric.
7. Terminal condition: explicit terminal SOC value, not `add_soc_incentive`.
8. Reference and regression: a REopt annual run at fixed sizes for a PV+battery+TOU+demand case (no genset)
   as the 1 % cross-check; the genset case cross-checked against our own brute-force enumeration on a 1-day
   problem instead.
9. Licence hygiene in `solarlytics-backend`: `THIRD_PARTY_NOTICES.md` with the REopt attribution line, an
   Apache-2.0 copy if any code is translated, no REopt name in product-facing strings.

---

## Appendix A. Verification run (6 Nov 2026, conda `ops` Python 3.12, scipy.optimize.milp over HiGHS)

`Settings.add_soc_incentive` default: `true` (`src/core/settings.jl` lines 8 and 17: "when true, an incentive
is added to the model's objective function to keep the ElectricStorage SOC high"). So `run_mpc` by default
minimises `Costs - mean(E[t])` (`src/mpc/model.jl` 37–43); the EMS should replace this with an explicit
terminal value (section 8).

Script: `C:/Users/SaadTahir/AppData/Local/Temp/claude/C--Users-SaadTahir-Videos/1143ee09-5b9b-42cf-b77e-04f40d670b5a/scratchpad/scripts/r3_check.py`
(re-implements only the two-point fuel-curve arithmetic of `utils.jl` 652–665 and a 40-variable toy MILP of
the genset block from sections 2.3, 2.4, 2.6; it reads nothing from the clone). Output:

```
--- fuel curve (REopt two-point method, utils.jl 652-665), HHV 40.7 kWh/gal ---
eff_full=0.322 eff_half=0.322: a=3.1056 kWhf/kWhe b=0.0000 kWhf/h/kW | a=0.07630 gal/kWhe b=0.000000 gal/h/kW | a=0.2888 L/kWhe b=0.00000 L/h/kW
    100 kW set: idle 0.00 L/h, full 28.88 L/h = 0.2888 L/kWh, half 14.44 L/h = 0.2888 L/kWh
eff_full=0.3233 eff_half=0.3233: a=3.0931 kWhf/kWhe b=0.0000 kWhf/h/kW | a=0.07600 gal/kWhe b=0.000000 gal/h/kW | a=0.2877 L/kWhe b=0.00000 L/h/kW
    100 kW set: idle 0.00 L/h, full 28.77 L/h = 0.2877 L/kWh, half 14.38 L/h = 0.2877 L/kWh
eff_full=0.34 eff_half=0.3: a=2.5490 kWhf/kWhe b=0.3922 kWhf/h/kW | a=0.06263 gal/kWhe b=0.009635 gal/h/kW | a=0.2371 L/kWhe b=0.03647 L/h/kW
    100 kW set: idle 3.65 L/h, full 27.36 L/h = 0.2736 L/kWh, half 15.50 L/h = 0.3100 L/kWh
eff_full=0.33 eff_half=0.28: a=2.4892 kWhf/kWhe b=0.5411 kWhf/h/kW | a=0.06116 gal/kWhe b=0.013295 gal/h/kW | a=0.2315 L/kWhe b=0.05033 L/h/kW
    100 kW set: idle 5.03 L/h, full 28.18 L/h = 0.2818 L/kWh, half 16.61 L/h = 0.3322 L/kWh
HHV diesel per litre = 10.7518 kWh/L
--- toy milp: G_max=200 kW, diesel 285.0 Rs/L, diesel marginal at full load 77.96 Rs/kWh_e vs peak grid 45 Rs/kWh ---
status: 0 Optimization terminated successfully. (HiGHS Status 7: Optimal) | objective Rs 7812.5
t=0 L=   60 G=  60.0 P=   0.0 u=0 v=0 w=0
t=1 L=   80 G=  80.0 P=   0.0 u=0 v=0 w=0
t=2 L=  120 G= 120.0 P=   0.0 u=0 v=0 w=0
t=3 L=  150 G= 150.0 P=   0.0 u=0 v=0 w=0
t=4 L=  160 G= 160.0 P=   0.0 u=0 v=0 w=0
t=5 L=  140 G= 140.0 P=   0.0 u=0 v=0 w=0
t=6 L=   90 G=  90.0 P=   0.0 u=0 v=0 w=0
t=7 L=   50 G=  50.0 P=   0.0 u=0 v=0 w=0
--- toy milp: G_max=120 kW, diesel 285.0 Rs/L, diesel marginal at full load 77.96 Rs/kWh_e vs peak grid 45 Rs/kWh ---
status: 0 Optimization terminated successfully. (HiGHS Status 7: Optimal) | objective Rs 9656.3
t=0 L=   60 G=  60.0 P=   0.0 u=0 v=0 w=0
t=1 L=   80 G=  80.0 P=   0.0 u=0 v=0 w=0
t=2 L=  120 G= 120.0 P=   0.0 u=0 v=0 w=0
t=3 L=  150 G= 120.0 P=  30.0 u=1 v=1 w=0
t=4 L=  160 G= 120.0 P=  40.0 u=1 v=0 w=0
t=5 L=  140 G= 110.0 P=  30.0 u=1 v=0 w=0
t=6 L=   90 G=  90.0 P=   0.0 u=0 v=0 w=1
t=7 L=   50 G=  50.0 P=   0.0 u=0 v=0 w=0
--- toy milp: G_max=120 kW, diesel 120.0 Rs/L, diesel marginal at full load 32.83 Rs/kWh_e vs peak grid 45 Rs/kWh ---
status: 0 Optimization terminated successfully. (HiGHS Status 7: Optimal) | objective Rs 7095.1
t=0 L=   60 G=  60.0 P=   0.0 u=0 v=0 w=0
t=1 L=   80 G=  80.0 P=   0.0 u=-0 v=-0 w=0
t=2 L=  120 G=  20.0 P= 100.0 u=1 v=1 w=0
t=3 L=  150 G=  50.0 P= 100.0 u=1 v=0 w=0
t=4 L=  160 G=  60.0 P= 100.0 u=1 v=0 w=0
t=5 L=  140 G=  40.0 P= 100.0 u=1 v=0 w=0
t=6 L=   90 G=  90.0 P=   0.0 u=0 v=0 w=1
t=7 L=   50 G=  50.0 P=   0.0 u=0 v=0 w=0
```

Reading of the run: with the REopt default (equal full- and half-load efficiencies) the intercept is zero
and the curve is a flat 0.289 L/kWh; with a realistic 0.34/0.30 split a 100 kW set idles at 3.65 L/h and
burns 0.274 L/kWh at full load and 0.310 L/kWh at half load, which is in the range Pakistani C&I sites
report (UNVERIFIED against a specific site). In the toy MILP the genset stays off while its marginal cost
(about Rs 78/kWh at Rs 285/L) exceeds the peak grid price (Rs 45/kWh), runs only to cover the excess over
a 120 kW sanctioned-load cap (and never below 30 kW because of the turndown), and peak-shaves when diesel
is cheap enough — the three behaviours the EMS genset block must show, obtained with scipy's HiGHS wrapper
and no extra packages.

End of note.
