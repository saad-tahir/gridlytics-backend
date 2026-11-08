# R2-emhass: EMHASS study notes for the OnyxLeaf EMS optimiser

Written 6 November 2026 by a research subagent of the EMS build-plan workflow. Self-contained: a reader who has not seen the brief should be able to use it alone. Everything below is from a shallow clone of EMHASS read on this date, with file paths and line numbers from that clone; nothing in the clone was executed.

## 1. Provenance of what was studied

- Repository: https://github.com/davidusb-geek/emhass (author David HERNANDEZ TORRES, "An Energy Management System for Home Assistant").
- Clone: `git clone --depth 1` on 6 Nov 2026 into `C:/Users/SaadTahir/AppData/Local/Temp/claude/C--Users-SaadTahir-Videos/1143ee09-5b9b-42cf-b77e-04f40d670b5a/scratchpad/oss/emhass` (a Temp folder; it may be wiped, so every line number quoted here is also tied to the commit below).
- HEAD commit: `4efdaa25bb22a8d4161a8022be614df88bdb8ddf`, dated 2026-11-05 13:36:56 +0100, "Merge pull request #1307 from MMicieli/fix/victoriametrics-basic-auth-1301".
- Version: `src/emhass/__init__.py` line 1: `__version__ = "0.18.8"`; CHANGELOG.md line 3: "0.18.8 - 2026-11-01".
- Package size (wc -l, src/emhass): optimization.py 9,295 lines; command_line.py 4,913; utils.py 7,176; forecast.py 3,527; machine_learning_forecaster.py 770; machine_learning_regressor.py 291; forecast_calibration.py 381; pv_bias_calibration.py 399; battery_identification.py 674; thermal_dp.py 420; web_server.py 1,437; retrieve_hass.py 2,617; plan_store.py 69; last_run.py 196; total 32,502 lines. The supporting report (Product 1 doc, line 344) quoted 8,990 lines for optimization.py from a 30 Oct clone; it is 9,295 at this commit, so the file is growing by roughly 300 lines a week.
- Python requirement and dependencies (pyproject.toml lines 6 and 21-46): `requires-python = ">=3.12"`; `cvxpy>=1.6.0, <1.10.0`; `highspy>=1.10.0`; `skforecast>=0.19.1,<0.23.0`; `pvlib>=0.11.0`; `scipy>=1.15.0`; `pandas>=2.2.0`; `numpy>=2.0.0, <2.6.0`; `plotly>=6.0.0`; web stack `quart`, `gunicorn`, `uvicorn==0.30.6`, `waitress`; `influxdb>=5.3.1`. No PuLP and no Pyomo in the dependency list.

### Modelling library: CVXPY now, PuLP before

- Current code: `import cvxpy as cp` at optimization.py line 10; every decision variable is a `cp.Variable`, every forecast or price input a `cp.Parameter`, and the problem is `cp.Problem(objective_expr, constraints)` at line 8505.
- History: CHANGELOG.md line 515 ("This release marks a significant milestone ... moving from **PuLP** to **CVXPY**"), lines 519-522 (vectorised constraint generation, "4-5x faster, clocking in at approximately 0.1s per iteration", HiGHS adopted as default, "no more system-level dependencies" because highspy ships as a wheel, Gurobi/CPLEX pluggable through `LP_SOLVER` env var), line 532 (`lp_solver` and `lp_solver_path` config keys removed). The surrounding version heading was not captured; the migration shipped in release 0.16.0, dated 2026-01-21 (CHANGELOG.md line 511), with hot fixes in 0.16.1 of 2026-01-23 (line 505) for an optimisation window size mismatch and a missing optim_status key when falling back to the LP problem.
- Residue: `__init__` still reads `optim_conf["lp_solver"]` and defaults it to "Highs" (lines 258-261), but the solve path ignores it and uses the environment variable (lines 8536-8560).

## 2. Licence and the notice obligation

LICENSE file, verbatim in full:

> MIT License
>
> Copyright (c) 2021-2026 David HERNANDEZ TORRES
>
> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

pyproject.toml line 8: `license = { text = "MIT" }`; classifier line 15: "License :: OSI Approved :: MIT License".

What this means for a closed-source SaaS:

1. Copying code, modifying it, and selling a service built on it are all permitted; there is no copyleft and no network-use clause.
2. The single condition is the notice: any file or "substantial portion" copied from EMHASS must carry the text "Copyright (c) 2021-2026 David HERNANDEZ TORRES" plus the permission paragraph. The practical rule for the backend repo: (a) keep a `THIRD_PARTY_NOTICES.md` at the repo root listing EMHASS, the MIT text above, the commit hash, and which files or functions were derived; (b) put a one-line header comment at the top of each derived Python file ("Portions derived from EMHASS, Copyright (c) 2021-2026 David HERNANDEZ TORRES, MIT License; see THIRD_PARTY_NOTICES.md").
3. Patterns, formulations and ideas are not copyrightable, so re-implementing the SoC recursion or the PWL stress cost from scratch in OnyxLeaf's own words carries no obligation. Only literal or near-literal copies do. Section 9 below says which is which.
4. The MIT licence does not grant the EMHASS name or logo; do not call the product or the module "EMHASS".
5. No dependency in the chain changes this: CVXPY is Apache-2.0, highspy/HiGHS is MIT, skforecast is BSD-3, pvlib is BSD-3 (licences as stated in the two guideline documents; the licence files of those projects were not re-read for this note, so mark the exact versions UNVERIFIED here).

## 3. The formulation, listed from the code with line numbers

All line numbers are in `src/emhass/optimization.py` at commit 4efdaa2 unless another file is named. Units: powers in W, energy in Wh, prices in currency per kWh, `time_step` in hours (line 177: `self.time_step = self.freq.seconds / 3600`). n = `num_timesteps` = `int(time_delta / freq)` (line 283) or the value passed for a DST-adjusted horizon.

### 3.1 Problem build pattern ("define once, update parameters")

- `__init__` (lines 161-434) creates every forecast and price input as a `cp.Parameter` (lines 285-313), per-battery scalar parameters (lines 335-349), the SoC recovery, intermediate target, availability and capacity-charge parameters (lines 436-526, 729-893), then the decision variables and bound constraints via `_initialize_decision_variables` (line 427). `self.prob = None` (line 430) until the first solve.
- `_perform_optimization_locked` (lines 7396-8869) sets `Parameter.value` for all inputs (lines 7722-7760 for pv, load, cost, export ceiling, terminal-SoC penalty; 7832-7853 for SoC init/final/limits/recovery gaps), builds `cp.Problem` only when `self.prob is None` (constraints and objective assembled at lines 8440-8508), and solves with `warm_start=True` (line 8602).
- If the input length differs from n the whole thing is rebuilt (lines 7437-7520, "Dynamic Resizing").
- Comments throughout insist on DPP-safe forms (no Parameter times Parameter products, see lines 7676-7681 and 764-770) so that CVXPY can re-use the canonicalised problem between solves.
- `command_line.py` lines 214-245 define `OptimizationCacheKey` (a frozen dataclass of the structural fields: number of loads, battery flags, hybrid flag, curtailment flag, time step, horizon, cost function, plant and optim config hashes) and lines 247-580 an `OptimizationCache` singleton that keeps one `Optimization` instance per process and rebuilds it only when the key changes.
- A re-entrant lock serialises solves per instance (`self._solve_lock`, line 183; `perform_optimization` line 7368).

### 3.2 Decision variables (`_initialize_decision_variables`, lines 2603-2862)

| Variable | Line | Type and bound |
| --- | --- | --- |
| `p_grid_neg[t]` export (<= 0) | 2625-2627 | continuous, `nonpos`, `>= -maximum_power_to_grid[t]` |
| `p_grid_pos[t]` import | 2630-2632 | continuous, `nonneg`, `<= maximum_power_from_grid[t]` |
| `p_deferrable[k][t]` | 2653-2657 | continuous nonneg, `<= nominal power` |
| `p_def_bin1[k]`, `p_def_start[k]`, `p_def_bin2[k]` | 2660-2662 | boolean per load per step (on state, rising edge, on state) |
| `p_def_stop[k]` | 2639-2641, 6652 | boolean falling edge, only when a minimum off time is set |
| `D[t]` grid direction | 2672 | boolean, 1 = import, 0 = export (one shared grid connection) |
| `E[k][t]` battery direction | 2681 | boolean per battery, 1 = discharge, 0 = charge |
| `p_sto_pos[k][t]` discharge | 2690-2691 | nonneg, `<= param_battery_discharge_power_max[k]` |
| `p_sto_neg[k][t]` charge (<= 0) | 2694-2695 | nonpos, `>= -param_battery_charge_power_max[k]` |
| `soc_low_recovered[k]`, `soc_high_recovered[k]` | 2699-2703 | boolean, out-of-band start recovery |
| `soc_deficit_cost[k]`, `soc_surplus_cost[k]` | 2705-2709 | nonneg epigraph variables |
| `soc_final_under[k]`, `soc_final_over[k]` | 2718-2721 | nonneg scalars, terminal-SoC slacks |
| `battery_first_import_gate[t]`, `battery_first_penalty[t]` | 2733-2738 | boolean gate and nonneg slack, optional |
| `SC[t]` | 2762 | self-consumption (only for costfun "self-consumption", maxmin variant, disabled per docs) |
| `p_hybrid_inverter[t]` | 2766 | free, AC-side inverter power (hybrid only) |
| `p_pv_curtailment[t]` | 2769 | nonneg |
| `peak_import` | 2783 | nonneg scalar epigraph of the priced demand peak (only when `capacity_cost_per_kw` > 0) |
| `peak_import_k[k]` | 2829 | one per demand-charge component (list form of `capacity_cost_per_kw`) |
| `p_dc_ac[t]`, `p_ac_dc[t]`, `is_dc_sourcing[t]` | 3348-3350 | hybrid inverter DC-to-AC, AC-to-DC and direction binary |
| `inv_curve_*_u{k}`, `inv_curve_*_b{k}` | 3264-3265 | PWL fill fractions (nonneg, <= 1) and ordering binaries for inverter curves |
| `battery_stress_cost_{k}[t]` | 1696-1698 | nonneg epigraph of the PWL stress cost |
| `inv_stress_cost[t]` | 2036-2038 | same for the inverter |
| `soc_above_{k}[i,t]` | 3574-3578 | boolean indicators for SoC-dependent charge derating |

`p_def_sum` is the sum of the electric deferrable loads (lines 2845-2852), a plain expression.

### 3.3 Parameters (inputs that change per solve)

`pv_forecast`, `load_forecast`, `load_cost`, `prod_price` (lines 285-287, 313; all length n), `load_cost_pos` = max(load_cost, 0) (294-296), `export_ceiling` = max(0, PV - load) (304-306), `soc_final_penalty` and `soc_final_reward` (311-312), `cost_per_load_{k}` (324), `soc_init_{k}`, `soc_final_{k}` (335-338), `battery_charge_power_max_{k}`, `battery_discharge_power_max_{k}` (345-349), `soc_low_gap_{k}`, `soc_high_gap_{k}`, `soc_low_required_{k}`, `soc_high_required_{k}` (443-453), `soc_target_floor_{k}` length n (480), `batt_charge_ceiling_{k}`, `batt_discharge_ceiling_{k}` length n and `batt_discharge_energy_max_{k}` (508-516), `current_period_peak` (750), `capacity_window_mask` length n (780), `capacity_interval_matrix` (k_max by n) and `capacity_realised_contribution` (797-802), per-load window masks and energy/timestep parameters (915-1042), thermal parameters (1097-1166).

### 3.4 Objective (`_build_objective_function`, lines 2864-3152; `cp.Maximize(cp.sum(objective_terms))` at 3152)

`scale = 0.001 * time_step` (line 2892) converts W over a step to kWh.

1. Energy cost, by `costfun`:
   - "profit" (2898-2915): `-scale * sum(load_cost * p_grid_pos + prod_price * p_grid_neg)`; since `p_grid_neg <= 0` the second term is export income. With `set_total_pv_sell` the import cost is charged on `p_load + p_def_sum` instead.
   - "cost" (2917-2923): import cost only, export unpaid.
   - "self-consumption" (2925-2933): import priced at `SELF_CONSUMPTION_BIGM = 1e3` times the tariff (constant at line 97).
2. Battery cycle cost (2939-2963): `-scale * sum(weight_dis * p_sto_pos - weight_chg * p_sto_neg)` per battery; the weights (`weight_battery_discharge`, `weight_battery_charge`, config defaults 0.0 at config_defaults.json lines 24-25) are currency per kWh and may be time series.
3. Multi-battery tie-break (2973-2977): `-1e-9 * sum_k k * sum(p_sto_pos[k] - p_sto_neg[k])`, constant `BATTERY_TIEBREAK_EPS` at line 64.
4. Deferrable start-up penalty (2980-2996): `-scale * penalty * nominal_power * sum(p_def_start * load_cost_pos)`.
5. Deferrable "max cost" reward (3001-3010).
6. Per-load tariff override (3027-3048), used for gas boilers.
7. Inverter stress `-sum(inv_stress_cost)` (3053-3054) and battery stress `-sum(battery_stress_cost_k)` (3059-3063).
8. SoC deficit and surplus dwell costs (3066-3081).
9. Terminal-SoC deviation (3092-3097): `-soc_final_penalty * sum(soc_final_under) + soc_final_reward * sum(soc_final_over)`; `soc_final_penalty` is set per solve at lines 7760-7765 to `0.001 * 100 * max(max(load_cost), 1e-3)` (constant `SOC_FINAL_DEVIATION_PENALTY_FACTOR = 100.0` at line 94), so a missed target costs 100 times the dearest import slot per kWh; the over-shoot reward is either `battery_soc_final_reward_factor` times the last-step tariff or the same penalty negated (7770-7780).
10. Battery-first import penalty (3109-3115): `-scale * 100 * sum(load_cost_pos * battery_first_penalty)` (constant line 78).
11. Demand (capacity) charge (3124-3142): `-capacity_cost_per_kw * peak_import / 1000`, not scaled by time step because it prices a peak kW, summed per component in the multi-component form.
12. Curtailment timing tie-break (3145-3150): `-1e-7 * sum(weights_decreasing_in_t * p_pv_curtailment)`.

### 3.5 Constraints

Power balance (`_add_main_power_balance_constraints`, 3154-3223):

- Non-hybrid with curtailment (3194-3204): `p_pv - p_pv_curtailment - p_def_sum - p_load + p_grid_neg + p_grid_pos + sum_k p_sto_pos[k] + sum_k p_sto_neg[k] == 0`; without curtailment the same without the curtailment term (3206-3216).
- Hybrid (3189-3191): `p_hybrid_inverter - p_def_sum - p_load + p_grid_neg + p_grid_pos == 0`; PV and battery then live on the DC bus (below).
- Grid direction exclusivity with time-varying limits (3220, 3223): `p_grid_pos <= max_from_grid[t] * D[t]` and `-p_grid_neg <= max_to_grid[t] * (1 - D[t])`. The limits come from `_prepare_power_limit_array` (1987-2021): a scalar is broadcast, a list of length n is used as is, a wrong-length list falls back to its first value with a warning. This per-step limit array is the one existing hook through which a grid outage could be expressed in EMHASS (set both limits to 0 in the outage steps); nothing in the code does so today.

Hybrid inverter (`_add_hybrid_inverter_constraints`, 3279-3417):

- DC bus (3359-3366): `(p_pv - p_pv_curtailment + sum p_sto_pos + sum p_sto_neg) - (p_dc_ac - p_ac_dc) == 0`.
- AC side (3376-3391): `ac_out = p_dc_ac * eff_dc_ac`, `ac_in = p_ac_dc / eff_ac_dc` (scalar efficiencies, defaults 1.0) or exact PWL curves; `p_hybrid_inverter == ac_out - ac_in`.
- Direction exclusivity (3397-3404): `p_ac_dc <= (1 - is_dc_sourcing) * p_ac_dc_max`, `p_dc_ac <= is_dc_sourcing * p_dc_ac_max`, with `p_dc_ac_max = inverter_ac_output_max / eff_dc_ac` (3334) and `p_ac_dc_max = inverter_ac_input_max * eff_ac_dc` (3337). The nominal AC power can come from a bundled CEC inverter database (`data/cec_inverters.pbz2`, lines 3296-3325).
- PWL curve (`_add_inverter_pwl_transfer`, 3239-3277): incremental method, `dc = sum_k slope_k * width_k * u_k`, `ac = sum_k width_k * u_k`, `u_k in [0,1]`, ordering `u[k+1] <= b[k] <= u[k]` with K-1 binaries, first segment gated by the direction binary.

Battery (`_add_battery_constraints`, 3419-3785), per battery k unless stated:

- No charge from grid (3446-3447, shared): `sum_k p_sto_neg[k] + p_pv >= 0`.
- No discharge to grid (3458-3473, shared): hybrid `E[k] <= D` ("can only discharge while importing"); AC-coupled `p_grid_neg + export_ceiling >= 0` i.e. export limited to the PV surplus, plus `p_grid_neg + p_pv - p_pv_curtailment >= 0`.
- Ramp limits when `set_battery_dynamic` (3501-3522): step differences of `p_sto_pos` and `p_sto_neg` bounded by `time_step * battery_dynamic_max/min * power_max` (defaults 0.9 and -0.9).
- Charge/discharge exclusivity is BINARY (3526, 3530): `p_sto_pos[k] <= eff_dis * max_dis * E[k]` and `p_sto_neg[k] >= -(1/eff_chg) * max_chg * (1 - E[k])`. So the base problem is a MILP even with no deferrable loads: n binaries for D plus n per battery for E. EMHASS does not rely on the usual argument that asymmetric efficiencies make simultaneous charge and discharge sub-optimal; it forces exclusivity.
- Availability ceilings and energy budget (3535-3541): `p_sto_pos <= eff_dis * batt_discharge_ceiling[t]`, `p_sto_neg >= -(1/eff_chg) * batt_charge_ceiling[t]`, `sum(p_sto_pos) * time_step / eff_dis <= batt_discharge_energy_max`.
- SoC dynamics (3548-3556), the heart of it: `power_flow = p_sto_pos / eff_dis + p_sto_neg * eff_chg`; `energy_change = power_flow * time_step`; `cumulative_energy = cp.cumsum(energy_change)`; `current_stored_energy = soc_init * cap - cumulative_energy` (Wh). Convention: `p_sto_pos` is AC-side discharge power, so battery-side energy out is divided by `eff_dis`; `p_sto_neg` is AC-side charge power (negative), battery-side energy in is multiplied by `eff_chg`. Defaults 0.95 each, capacity 5,000 Wh, SoC band 0.3-0.9, target 0.6 (config_defaults.json lines 144-152).
- SoC-dependent charge derating (3565-3592): big-M indicators `above[i,t]` pinned to the SoC at the start of the step, each threshold crossing steps the charge ceiling down.
- Band limits with a single recovery transition (3597-3634): `stored >= min_energy - soc_low_gap * (1 - soc_low_recovered)` and the mirror for the top; the recovery binaries are monotone non-decreasing and must end at 1 when a gap exists; while unrecovered the SoC may only move towards the band. The gap parameters are set at 7842-7853 from `max(0, (soc_min - soc_init) * cap)` and `max(0, (soc_init - soc_max) * cap)`. This is how EMHASS avoids infeasibility when the measured SoC arrives outside the configured band at an MPC tick.
- Terminal SoC, SOFT (3643-3649): `sum(energy_change) == (soc_init - soc_final) * cap + soc_final_under - soc_final_over`, slacks priced as in 3.4 item 9.
- Intermediate SoC target (3661): `current_stored_energy >= soc_target_floor[t]` where the floor vector is zero except `soc_target * cap` at the requested step (set at 7566-7640, with reachability warnings). This is the only "reserve by time X" primitive in EMHASS.
- Stress cost (3664-3676) via `_build_stress_segments` (2049-2067) and `_add_stress_constraints` (2069-2082): a quadratic `y = max_cost_step * (x / max_power)^2` with `max_cost_step = (max_power / 1000) * stress_unit_cost * time_step` is sampled at `segments + 1` points (default 10 segments, config line 154) and the chords become `stress >= slope * P + intercept` and `stress >= -slope * P + intercept` for `P = p_sto_pos - p_sto_neg` (total magnitude). Because the quadratic is convex the envelope needs no binaries; the cost is therefore a convex PWL penalty on battery power magnitude, in currency per step, with `battery_stress_cost` in currency per kWh at full load (config line 153, default 0).
- SoC deficit / surplus dwell penalties (3680-3702): `soc_deficit_cost >= (threshold_energy - stored) * rate * time_step` with the rate in currency per kWh (config lines 155-158, defaults 0.40 / 0.90 thresholds, 0 cost).
- Battery-first gate (3732-3781, optional, aggregate over the fleet): `aggregate_stored - aggregate_min - 0.01 * aggregate_cap <= aggregate_cap * (1 - import_gate)` and `battery_first_penalty >= p_grid_pos - max_from_grid * import_gate`.

Curtailment bound (8469-8470): `p_pv_curtailment <= pv_forecast`.

Demand charge epigraph (2783-2830): `peak_import >= capacity_window_mask[t] * p_grid_pos[t]` for every t (interval form: `peak_import >= capacity_interval_matrix @ p_grid_pos + realised_contribution`, where the matrix averages `capacity_charge_interval_timesteps` native steps per tariff interval and the first row carries the already-elapsed part of the open interval, built in NumPy at `_build_capacity_interval_arrays` 2302-2414), and `peak_import >= current_period_peak` (floor at the incumbent peak of the billing period, 2808).

Deferrable loads (`_add_deferrable_load_constraints`, 6237-6915), kept here only for the pieces the EMS will re-use for a genset:

- Rising edge (6562-6565): `start[0] >= bin2[0] - current_state`, `start[t] >= bin2[t] - bin2[t-1]`; and `start[t] + bin2[t-1] <= 1` (6575-6576), plus `start <= bin2` when the start is priced (6571-6572).
- Maximum number of starts (6579-6586): `sum(start) <= max_starts`.
- Minimum on time (6594-6616): for each t, `sum(bin2[t : t+N_on]) >= (window_end - t) * start[t]`, clamped at the horizon end.
- Minimum off time (6618-6684): a `stop` binary pinned to the falling edge by three inequalities `stop[t] >= bin2[t-1] - bin2[t]`, `stop[t] <= bin2[t-1]`, `stop[t] <= 1 - bin2[t]`, then `sum(1 - bin2[t : t+N_off]) >= (window_end - t) * stop[t]`.
- Semi-continuous operation (6747-6754): `p == on_level[t] * bin1` and `bin1 == bin2` (EMHASS runs a semi-continuous load at exactly its nominal power, not between a minimum and a maximum).
- Big-M chosen per load as the nominal power (6281-6294), with a comment that a tight M "significantly speeds up the solver".

### 3.6 Solver settings (`_perform_optimization_locked`, lines 8518-8605)

- Solver choice: `LP_SOLVER` environment variable, default "HIGHS" (8536-8537); "GUROBI" or "CPLEX" are used only if installed (8539-8560); the `lp_solver` config key is dead.
- HiGHS options (8563-8593): `time_limit = float(lp_solver_timeout)` (code fallback 180 s at 8532, shipped default 45 s at config_defaults.json line 11), `threads = num_threads` only when > 0 (default 0 = let HiGHS decide; `__init__` lines 249-255 map 0 to `os.cpu_count()` for its own logging), `run_crossover = "on"`, `mip_rel_gap` always passed (shipped default 0.01 at config line 12, code fallback 0.0; benchmarks quoted in the comment at 8576-8577: 5 % gap about 1.75x faster, 10 % 1.86x, 20 % 2.89x).
- Call: `self.prob.solve(solver=cp.HIGHS, warm_start=True, **solver_opts)` (8602). On exception the status is forced to None so a stale previous plan is not republished (8603-8610).
- Status handling (8626-8800): `_has_feasible_incumbent` (7207-7254) checks HiGHS `extra_stats.primal_solution_status == 2` (otherwise it evaluates constraint violations and integrality by hand); `_needs_relaxed_retry` (7256-7270) sends `infeasible`, `unbounded`, None, or a `user_limit` without a feasible incumbent to a one-shot relaxed rebuild that drops the semi-continuous and single-block load binaries (8650-8800) and returns "Optimal (Relaxed)"; a time-limited solve with an incumbent is accepted as "Optimal (Incumbent)" (8803-8820).
- Timing: `stage_times` records build, solve and extract durations (8596-8599, 8822-8825, 8866-8867).
- Result extraction (`_build_results_dataframe`, 6976-7204): columns `P_PV`, `P_Load`, `P_PV_curtailment`, `P_grid_pos`, `P_grid_neg`, `P_grid`, `P_deferrable{k}`, `P_batt` (= p_sto_pos + p_sto_neg, positive = discharge), `SOC_opt` (fraction 0-1, re-integrated in NumPy at 7049-7058 and required to "stay in lockstep" with the CVXPY recursion), `P_hybrid_inverter`, `unit_load_cost`, `unit_prod_price`, `maximum_power_from_grid`, `maximum_power_to_grid`, `cost_profit`, `cost_fun_<costfun>`, `optim_status`, thermal columns. Values within 1e-9 of zero are snapped to zero (6993-7002). The schema is versioned ("1.0") and documented in `docs/plan_output_schema.md`.

### 3.7 Size of the problem the EMS would build (for reference)

With one battery, no deferrable loads, no hybrid-inverter curves and a demand charge, EMHASS at 192 steps has about 7 continuous vectors of length 192 (grid import, grid export, battery discharge, battery charge, curtailment, stress epigraph, and the SoC expression which is not a variable) and 2 boolean vectors of length 192 (D, E), that is 384 binaries, plus a handful of scalars. The EMS adds a genset on/off, start and stop vector (576 more binaries) and a genset power vector. At this size HiGHS solves in well under a second on a laptop in the authors' experience (CHANGELOG line 519 quotes about 0.1 s per iteration for their standard cases); the EMS case was not benchmarked here, so treat timing as UNVERIFIED until the Opus build measures it.

## 4. Receding-horizon (naive MPC) handling

### 4.1 What the code does

- Entry point `perform_naive_mpc_optim` (9023-9240). Signature: `df_input_data` (prices and weather), `p_pv`, `p_load` (forecast Series), `prediction_horizon` (steps), `soc_init`, `soc_final`, `soc_target`, `soc_target_timestep`, `current_period_peak`, `capacity_charge_window`, `capacity_charge_consideration`, `capacity_charge_current_interval_history`, deferrable hours and windows, `battery_availability`, `battery_discharge_energy_max`, `def_availability`.
- Guard: `prediction_horizon < 5` is rejected (9186-9190). A horizon different from the initialised `num_timesteps` only logs a warning (9193-9198); the resize path in `_perform_optimization_locked` then rebuilds.
- The inputs are sliced to the horizon: `df_input_data.iloc[:prediction_horizon]`, `p_pv.iloc[:prediction_horizon]`, `p_load.iloc[:prediction_horizon]` (9201-9207), then `perform_optimization` is called (9210-9233).
- Initial and final SoC: `soc_init` and `soc_final` arrive as runtime parameters (`command_line.py` 3102-3103) in fraction units; if one is missing it is copied from the other, and if both are missing from `battery_target_state_of_charge` (7523-7534). The terminal condition is soft (3.4 item 9), so an unreachable `soc_final` degrades gracefully. The unit test at tests/test_optimization.py 647-703 checks that the last `SOC_opt` equals `soc_final` within 1e-3 for a 10-step horizon with soc_init 0.4 and soc_final 0.6, then 0.8 to 0.5.
- Out-of-band `soc_init` (measured SoC below `soc_min` or above `soc_max`) is handled by the single-recovery-transition constraints (3.5), with gap parameters set at 7842-7853; a battery that cannot recover within its availability window has the band switched off for that run (7854-7880).
- Horizon in the HA deployment: `command_line.naive_mpc_optim` (3067-3210) clips `prediction_horizon` to the available forecast rows (3098-3101), runs the solve under the instance lock after `_apply_cached_opt_runtime_updates` (3137-3140), writes a CSV, optionally publishes entities, and records a `last_run` snapshot plus the plan in `plan_store` (3208-3210, 72-114). `plan_store.record` writes `plan_latest.json` (plan_store.py 35-53) served by `GET /api/v1/plan` (web_server.py 842) next to `GET /api/v1/last-run` (806) and `/healthz` (1008).
- Documented intent (docs/advanced_math_model.md 519-546): "an informal/naive representation of a MPC controller"; set the prediction horizon "a fixed value for a receding horizon or a variable value for a shrinking horizon"; the example runs the MPC every 6 h with a horizon shrinking to the end of the day "to keep the one-day energy optimization notion"; "the values for soc_init and soc_final for each MPC optimization can be taken from the initial day-ahead optimization performed at the beginning of each day". docs/passing_data.md line 174: without `prediction_horizon`, naive-mpc-optim plans only 10 steps.
- There is no state carried between ticks other than what the caller passes (SoC, incumbent peak, interval history, elapsed on-time of loads). EMHASS itself never reads the previous plan to warm the next one except through CVXPY's `warm_start=True` on the same problem object.
- `perform_perfect_forecast_optim` (8871-8944) is a hindsight re-solve over past days with realised PV and load, day by day; it is the only "counterfactual" tool in the repo and gives a perfect-information cost floor per day.

### 4.2 MPC loop design for the OnyxLeaf EMS (what to take, what to change)

Take:

1. The define-once, parameter-update, warm-start structure with a structural cache key per site. At 15-minute steps over 48 h (192 steps) the LP/MILP is small; the saving from warm starts matters less than the build cost, which CVXPY's canonicalisation dominates, so caching the built problem per site is worth it in a service that re-solves every site every 15 minutes.
2. Explicit inputs per tick: measured SoC as `soc_init`, the billing period's incumbent MDI as `current_period_peak`, the elapsed minutes of the open MDI interval as the "current interval history", the genset's current state and elapsed on/off steps (EMHASS does this for deferrable loads through `param_def_current_state`, `current_on_timesteps`, `current_off_timesteps` at 952-1003 and 1254-1388).
3. Soft terminal condition on SoC with a penalty scaled to the horizon's dearest price rather than a hard equality, and the band-recovery handling for a measured SoC outside the configured band.
4. Accept a time-limited HiGHS incumbent when it is primal feasible; keep one relaxed fallback; never republish a stale plan after an exception.
5. Record build, solve and extract times per tick and expose last-run and plan endpoints with a versioned schema.

Change:

1. EMHASS's "shrinking horizon to midnight" idea comes from a daily deferrable-load energy budget; the EMS has no such daily budget, so use a fixed 48 h receding horizon (192 steps) at every tick, with `soc_final` free (set the terminal penalty to zero and instead value end-of-horizon stored energy at a marginal price, which EMHASS only approximates through `battery_soc_final_reward_factor`).
2. Re-solve on events (outage detected, genset started, forecast bust) as the guideline requires; EMHASS has no event trigger, only HA automations calling the REST action.
3. Because the shipped product delivers a daily TOU slot table to the inverter (Mode A), the MPC output must be post-processed into a small number of slots; EMHASS has no such step (see section 9).
4. Multi-site: one optimiser object per site in a worker pool, keyed by site structure, not a process singleton.

## 5. How prices arrive

- `Forecast.get_load_cost_forecast` (forecast.py 3214-3306) fills the column named by `var_load_cost` with, by `load_cost_forecast_method`: `hp_hc_periods` (default, config line 75): `load_offpeak_hours_cost` everywhere, overwritten with `load_peak_hours_cost` inside each `load_peak_hour_periods` window using `DatetimeIndex.indexer_between_time` on the tariff clock (`tariff_schedule_time_zone`, 3246-3263); `csv`: a file in the data path; `list`: `passed_data["load_cost_forecast"]`, rejected if shorter than the forecast dates unless a `prediction_horizon` is given (3277-3301).
- `Forecast.get_prod_price_forecast` (3308-3397): `constant` (default, config line 107) = `photovoltaic_production_sell_price`; `csv`; `list` from `passed_data["prod_price_forecast"]`.
- The optimiser then only sees two arrays, `unit_load_cost` and `unit_prod_price`, in currency per kWh, one value per step (`perform_naive_mpc_optim` 9206-9207, `perform_optimization` 7722-7725). Everything tariff-specific (seasons, holidays, demand windows, incumbent peaks) is the caller's job; the code says so at 764-766 ("The mask is computed by the caller (Home Assistant owns the business-day / holiday / season calendar) - EMHASS stays tariff-agnostic").
- Demand charge inputs are likewise runtime lists: `capacity_cost_per_kw` (currency per kW, scalar or list), `capacity_charge_interval_timesteps` (native steps per tariff measurement interval), `capacity_charge_window` (0/1 per step), `capacity_charge_consideration`, `current_period_peak` (W), `capacity_charge_current_interval_history` (W per elapsed step). docs/passing_data.md 327-410 explain each.
- Consequence for the EMS tariff engine: it should produce exactly these arrays per site and tick (import price per step, export price per step, MDI window mask per step, MDI interval length, incumbent MDI) plus the pieces EMHASS has no slot for (ICP block price, diesel price, outage probability, net-metering period netting).

## 6. Forecaster design (forecast.py, machine_learning_forecaster.py, calibration modules)

### 6.1 PV side (`forecast.py`, class `Forecast`, lines 175-3527)

- Weather methods (`weather_forecast_method`, config default `open-meteo` at config_defaults.json line 62; docs/forecasts.md 17, 33-170): `open-meteo` (lat/lon NWP, cached in the data folder, converted to PV power with pvlib), `solcast` (site-specific PV forecast with P10/P50/P90, 10 calls a day on the free tier, 30-minute data; rate-limit guard `_solcast_rate_limit_ok` at 650), `solar.forecast` (keyless public API, hourly), `csv`, `list` (the caller passes `pv_power_forecast`, optionally with `pv_power_forecast_p10`).
- PV physics: `_calculate_pvlib_power` (1887-1943) runs a pvlib ModelChain per PV plant configuration (module, inverter, tilt, azimuth, modules per string, strings per inverter) using the CEC module and inverter databases shipped under `src/emhass/data`; `get_power_from_weather` (1944-1998) wraps it; `cloud_cover_to_irradiance` (1611-1648) derives irradiance from cloud cover when the API gives none.
- Conservative blend: `_parse_pv_quantile_bias` (701) reads `weather_forecast_pv_quantile_bias` so the planned PV is `bias * P10 + (1 - bias) * P50` (docs/forecasts.md 73).
- Learned correction: `adjust_pv_forecast_data_prep` (2039-2192), `adjust_pv_forecast_predict` (2193-2260) and `machine_learning_regressor.py` (291 lines) fit a regression of realised PV on forecast PV plus solar angles (`compute_solar_angles` 1999-2014) and cyclic hour features (`add_cyclic_hour_features` 2015-2038), then clip by solar elevation (docs/forecasts.md 239). Curtailed steps are excluded from training (pv_bias_calibration.py module docstring, "issue #1026").
- Self-tuning bias: `pv_bias_calibration.compute_pv_bias_calibration` (253 onward) implements adaptive conformal inference (Gibbs and Candes 2021) on the P10/P50 blend: `bias_{t+1} = clip(bias_t + gamma * (shortfall_t - alpha), 0, 1)` with `DEFAULT_TARGET_SHORTFALL_RATE = 0.10` and `DEFAULT_GAMMA = 0.05` (lines 115-119). It is a pure-NumPy reporting module with no side effects, 399 lines, and it documents the curtailment-censoring problem carefully.

### 6.2 Load side

Three methods (`load_forecast_method`, config default `typical` at line 65; docs/forecasts.md 21-26, 266-280):

- `typical`: a one-year reference profile grouped by month and day-of-week, scaled by `maximum_power_from_grid`, from `data/data_train_load_clustering.pkl` (docs/forecasts.md 269). Household-specific and useless for a C&I site.
- `naive`: persistence, `get_naive_load_forecast` (2547-2620): each forecast step takes the observation at the same local wall-clock time n calendar days earlier, with DST-safe fallbacks and nearest-match tolerance of half a step (2761). The honest baseline every learned model must beat.
- `mlforecaster`: `MLForecaster` in machine_learning_forecaster.py (770 lines). Design:
  - skforecast `ForecasterRecursive` (import line 13; built at 328) around a scikit-learn regressor chosen by name from twelve options (`_get_sklearn_model`, 218-258: LinearRegression, Ridge, Lasso, ElasticNet, KNeighborsRegressor, DecisionTreeRegressor, SVR, RandomForest, ExtraTrees, GradientBoosting, AdaBoost, MLPRegressor). Default `KNeighborsRegressor`, `num_lags` 48 (docs/mlforecaster.md 45-55). Neither LightGBM nor XGBoost is used.
  - Features: autoregressive lags (`num_lags`, or the tuned set from `get_lags_list_from_frequency`, 130-153, which maps 6, 12, 24, 36, 48, 60, 72 hours to steps) plus calendar exogenous features from `utils.add_date_features` (utils.py 6871-6920: year, month, day_of_week, day_of_year, day, hour) and optional weather covariates passed through as exog (`weather_features`, `_merge_weather_exog` 185-216, `generate_exog` 156-183). No holiday feature, no "day type" feature beyond day-of-week, no load-shedding indicator.
  - `fit` (260-409): hold out the last `split_date_delta` (default 48 h) as test, fit on the rest with `store_in_sample_residuals=True`, log an R2 on the test window, optional backtest with `TimeSeriesFold(steps=num_lags, refit=False)` and `backtesting_forecaster` (339-357), then MAE, RMSE, R2 and MAPE through `utils.compute_forecast_metrics` (4033-4100).
  - `predict` (411-486): rebuilds the exog for the horizon from the last observed window, predicts `steps` ahead (the optimisation horizon when called from the optimiser, else the lag count).
  - `tune` (627 onward) runs skforecast's `bayesian_search_forecaster` (optuna) over a per-model search space (`_get_search_space`, 488-625) that always includes the lag set; `n_trials` default 10.
  - The last-window plumbing in forecast.py (`_prepare_ml_last_window`, 2870-2935) is careful about time zones, bucket phase and completed-only observations; this is where most real-world bugs lived (CHANGELOG 0.18.8 "Fix MLForecaster DST horizons and load-history timestamp alignment").
  - Documented accuracy is modest: the worked example in docs/mlforecaster.md 66-68 reports an R2 of 0.27 on the 2-day test for KNN on 240 days of household data.
- Calibration report: `forecast_calibration.py` (381 lines) splits the history into train/test/val, runs a day-ahead walk-forward for `naive`, `typical` and `mlforecaster` (`_walk_forward`, 121-209), scores each window, and reports a skill score versus naive (`_skill_vs_naive`, 321-346). Constants: `CALIBRATION_MIN_DAYS = 60`, default 90 days of history, 14-day test and validation windows (lines 36-42). This is the right shape for the EMS's per-site forecaster selection.

### 6.3 Forecaster design for the EMS (what to take)

1. Keep the three-tier structure: physics prior (pvlib clear-sky and plane-of-array), a learned residual or correction model, and an explicit conservative quantile blend; add the ACI self-tuning of the blend as a nightly job per site. All three exist in EMHASS as patterns.
2. Keep persistence (naive) as the baseline and the walk-forward calibration report as the acceptance test for any learned model; publish skill-versus-naive per site in the admin pages.
3. Replace the model zoo with one gradient-boosted model (xgboost 3.2 is in the ops environment; LightGBM is not) trained per site on lags, calendar features, a Pakistani holiday calendar, a day-type flag, the grid-availability history, and weather exog, with quantile outputs for the reserve logic. skforecast is not installed in the ops environment; a thin recursive or direct multi-step wrapper around xgboost is enough and avoids the skforecast version churn EMHASS pins (`skforecast>=0.19.1,<0.23.0`).
4. Do not take `typical`; C&I load is shift-pattern driven and needs site data.
5. Take the time-alignment discipline of `_prepare_ml_last_window` as a test list (tz-aware index, unique and monotonic, only completed buckets, lattice phase check).

## 7. How the web UI draws the plan

- The UI is server-rendered, not a single-page app. `utils.get_injection_dict` (utils.py 3894-4020) takes the result DataFrame and builds four Plotly Express line charts with `line_shape="hv"` (step lines, right for per-interval setpoints), `template="presentation"`, `render_mode="svg"`, colours sampled from the "jet" scale: figure 0 all `P_*` columns ("Systems powers schedule after optimization results", W), figure 1 `SOC_opt` or `SOC_opt_k` ("Battery state of charge schedule", %), an optional temperature figure, figure 2 all `cost_*` and `unit_*` columns ("Systems costs"). Each figure becomes an HTML fragment via `fig.to_html(full_html=False, default_width="75%")`. Two HTML tables follow: the whole rounded DataFrame (`to_html(classes="mystyle")`, 4003) and a cost summary with the sum of each `cost_*` column plus `optim_status` (4004-4011).
- The dict is pickled to the data folder (`_save_injection_dict`, web_server.py 799) and the index route (web_server.py 260-289) renders `templates/index.html`, which loops `{% for plot in injection_dict %}` and injects each fragment as safe HTML (index.html 63-68). `static/script.js` (640 lines) only drives the action buttons (`fetch("action/" + action)`, line 207), the basic/advanced toggle and a sticky table header; `static/style.css` is 1,471 lines.
- Machine-readable plan: `GET /api/v1/plan` returns `{"emhass_schema_version", "generated_at", "plan": [records]}` from `plan_store` (plan_store.py 27-33 serialises the DataFrame with `to_json(orient="records", date_format="iso")`, index renamed `timestamp`); JSON Schemas live in `docs/api/v1/`.
- For the EMS frontend (React, TypeScript, Vite) the reusable ideas are: step-shaped lines for all power series; a separate SoC panel with the min/max band; a cost panel with the per-step tariff drawn as a staircase under the plan; a status badge from `optim_status`; a table view of the same records; and a versioned JSON contract between backend and frontend (the EMS should publish an OpenAPI schema for the plan, as EMHASS does at `static/openapi.json` and `tests/test_openapi.py`). Nothing in the EMHASS UI code is worth copying as code; Plotly-in-Jinja does not fit a React app.

## 8. What to lift as patterns, and what is worth copying under MIT

### 8.1 Patterns to lift (re-implement in OnyxLeaf's own code, no notice needed)

1. Variable conventions: split import/export and discharge/charge into non-negative pairs with direction binaries; keep the SoC as a cumulative-sum expression of the flows rather than a per-step state variable (fewer rows, same model); keep a plain-NumPy re-integration of SoC in the result builder as a cross-check.
2. Define-once CVXPY problem with `cp.Parameter` inputs, DPP-safe expressions, structural cache key, warm start, per-instance lock.
3. "Never infeasible" policy: soft terminal SoC with price-scaled slacks, out-of-band SoC recovery, soft battery-first gate, accept time-limited incumbents, one relaxed retry.
4. Demand charge as an epigraph over a window mask with an incumbent floor and an interval-averaging matrix; the "consideration" mask for recurring windows inside a rolling horizon. This maps straight onto a Pakistani MDI charge measured as a 30-minute average kW (`capacity_charge_interval_timesteps = 2` at 15-minute steps). Whether every DISCO's MDI is a 30-minute average of kW (not kVA) is UNVERIFIED here and belongs to the tariff research task.
5. Unit-commitment logic for the genset: on-state, start and stop binaries pinned by the three-inequality edge detection, minimum on and off times by windowed sums, a start count cap, and a priced start; generalise EMHASS's `p == nominal * on` to `p_min * on <= p_gen <= p_max * on` with a fuel cost `a * on + b * p_gen` (intercept plus slope, as REopt does).
6. Time-varying grid limits as per-step arrays: the simplest way to encode a scheduled or forecast outage window is `max_from_grid[t] = max_to_grid[t] = 0`.
7. Intermediate SoC floor vector as the "reserve by time X" primitive; the EMS turns the outage-probability forecast into a per-step floor.
8. Convex PWL cost from a sampled curve (`_build_stress_segments`), reused for the LFP wear cost derived from BLAST-Lite.
9. Forecast-side: physics prior plus learned correction plus quantile blend plus ACI self-tuning; persistence baseline; walk-forward calibration report with skill versus naive.
10. Operations: `last_run` and `plan` snapshots with a versioned schema, `/healthz`, stage timers.

### 8.2 Functions worth copying nearly verbatim under MIT (carry the notice)

Each is small, self-contained, and would take longer to re-derive than to copy. Put them in a clearly marked module (for example `ems/optimiser/_emhass_derived.py`) with the MIT header from section 2.

| Function | Location | Lines | Why |
| --- | --- | --- | --- |
| `_build_stress_segments`, `_add_stress_constraints` | optimization.py 2049-2082 | 34 | Convex PWL epigraph builder for any sampled convex cost. |
| `_prepare_power_limit_array` | optimization.py 1987-2021 | 35 | Scalar-or-vector limit coercion with sane fallbacks. |
| `_has_feasible_incumbent`, `_needs_relaxed_retry` | optimization.py 7207-7270 | 64 | HiGHS incumbent detection through `extra_stats.primal_solution_status`, with a manual fallback that checks integrality. |
| `_add_inverter_pwl_transfer` | optimization.py 3239-3277 | 39 | Incremental-method PWL with ordering binaries, if inverter efficiency curves are ever modelled. |
| Min on / min off block | optimization.py 6594-6684 | 90 | Edge binaries and windowed sums; adapt variable names for the genset. |
| `get_naive_load_forecast` | forecast.py 2547-2620 | 74 | DST-safe persistence baseline. |
| `get_lags_list_from_frequency` | machine_learning_forecaster.py 130-153 | 24 | Lag set scaled to the step. |
| `compute_forecast_metrics` | utils.py 4033-4100 | about 68 | MAE, RMSE, R2, MAPE with degenerate-case guards. |
| `add_date_features` | utils.py 6871-6920 | about 50 | Calendar features. |
| `pv_bias_calibration.py` whole module | 399 lines | 399 | ACI self-tuning of the P10/P50 blend; pure NumPy, well documented. |
| `forecast_calibration.py` walk-forward skeleton | lines 90-209, 321-346 | about 150 | Day-ahead walk-forward scoring and skill versus naive; replace the method registry. |
| `plan_store.py` | whole file | 69 | Cache plus write-through JSON snapshot of the last plan. |

What NOT to copy: `optimization.py` as a whole (9,295 lines, most of it thermal loads, shared tanks, deferrable sequences, multi-battery availability, DP COP refinement, relaxed-retry bookkeeping that the EMS does not need), `command_line.py` and `utils.py` (Home Assistant configuration system, `associations.csv`, runtime-parameter coercion), `retrieve_hass.py`, `web_server.py` and the templates. Forking would import 25,000 lines of someone else's bug surface for the roughly 600 lines the EMS actually needs.

### 8.3 Modelling library choice (an input to the build plan, not EMHASS's decision)

- EMHASS uses CVXPY 1.6-1.9 with highspy 1.10+. CVXPY's Parameter and DPP machinery is what makes "define once" cheap; PuLP has no Parameter concept (every tick rebuilds the model) and scipy's `milp` takes raw matrices.
- The ops environment on this machine has scipy 1.17 with `scipy.optimize.milp` (wraps HiGHS) but neither cvxpy nor highspy nor pulp (verified on 6 Nov 2026 with importlib.util.find_spec in that interpreter: cvxpy, highspy, pulp, pyomo, skforecast, pvlib and lightgbm absent; xgboost and scipy present). The backend repo will need `cvxpy` and `highspy` added to its requirements, or the optimiser can be written straight against `scipy.optimize.milp` with hand-built sparse matrices. For 192 steps and about 10 vectors the matrix build is a few hundred lines and avoids CVXPY's canonicalisation time (EMHASS's comments at 7676-7681 show how much care DPP needs), but loses readability and the warm start. Recommendation for the build plan: CVXPY plus HiGHS, exactly as EMHASS, with `mip_rel_gap` 0.01 and a 20-30 s time limit per site tick, and a benchmark gate on 192-step problems before pilot.

## 9. What EMHASS cannot do that the EMS needs

Verified by grep over `src/emhass/*.py` on the clone: no occurrence of genset, diesel, generator (as a device), outage (other than a retrieve_hass comment about data outages), blackout, islanded, load shedding, Deye, Sunsynk, Solarman, Modbus, tenant or site_id.

1. Genset. No generator variable, no fuel cost, no run-hour or start counter. The deferrable-load unit-commitment block (section 3.5) supplies the on/off, start/stop and minimum on/off patterns; the EMS must add `p_gen` with `p_min * on <= p_gen <= p_max * on`, the fuel cost `fuel_price * (a * on + b * p_gen) * time_step`, a start cost, an optional maintenance cost per run hour, and the genset in the power balance. A genset that cannot run in parallel with the grid (most Pakistani C&I ATS setups, UNVERIFIED per site) needs `on[t] <= 1 - grid_available[t]`, which is trivial once `grid_available` is a per-step parameter.
2. Outages. No grid-availability vector, no outage probability, no expected unserved energy, no islanded balance. EMHASS's closest primitives are the per-step grid limit arrays (set to zero in an outage) and the intermediate SoC floor (reserve by time X). The EMS needs: `grid_available[t]` (0/1 for scheduled or confirmed outages; the balance then has `p_grid_pos[t] = p_grid_neg[t] = 0`), a probabilistic reserve floor `stored[t] >= reserve[t]` derived from the outage-probability forecast and the critical load, and a priced shortfall slack so a deep outage never makes the problem infeasible. Nothing to copy.
3. MDI details. Present: peak epigraph, window mask, interval average, incumbent floor, consideration mask, multi-component. Missing: monthly billing-period accounting across ticks (caller-owned in EMHASS; the EMS tariff engine must track the incumbent and reset it on the billing date), kVA versus kW and power-factor penalties, any "sanctioned load" exceedance tiers, and the choice of the fixed charge per tariff category. These belong to the tariff engine, with the epigraph as EMHASS has it.
4. ICP (the flat incremental price above a baseline consumption). EMHASS prices every imported kWh at `load_cost[t]`; there is no block or tiered tariff on cumulative consumption. A cheaper marginal price above a monthly baseline makes the cost concave in consumption, so it needs a binary (or an a-priori decision whether the site will exceed the baseline this month) plus a cumulative-import variable across the horizon with the month-to-date import as a parameter. This is new modelling.
5. Net billing versus legacy net metering. Asymmetric per-step import and export prices (net billing) are exactly EMHASS's "profit" cost function. Legacy net metering (kWh netting over a billing period at the import rate, excess credited at a lower rate) needs period-level netting variables and the month-to-date net position as a parameter; EMHASS has nothing for it.
6. Diesel price and fuel curves: absent (see 1).
7. Battery degradation: only the convex PWL stress on power magnitude and per-kWh throughput weights; no depth-of-discharge, calendar or state-of-health term and no cycle counting across ticks. The EMS plan calls for a BLAST-Lite-derived PWL wear cost; EMHASS's `_build_stress_segments` is the builder, the curve must come from elsewhere.
8. Plan delivery as inverter TOU slots (Mode A). EMHASS emits per-step setpoints and HA entities; it has no routine that compresses a 96-step day into the 6 or so charge/discharge windows that a Deye, Growatt or similar inverter accepts, nor any inverter register map. The EMS needs a slot-fitting step (choose K slots and per-slot SoC targets that minimise the cost loss versus the full plan) and a per-brand executor; kellerza/sunsynk register maps are the reference per the Product 1 report.
9. Savings verification. None: no counterfactual replay against the inverter's own mode, no switchback A/B, no bill rebuild. `perform_perfect_forecast_optim` provides a per-day hindsight optimum, which is a useful upper bound in the savings report, but the 1-minute baseline replay is new work.
10. Multi-tenancy and service shape. One process, one configuration, one cached `Optimization` singleton (`OptimizationCache._instance`, command_line.py 261), pickled parameters and plan files in a data directory, Home Assistant as the data source and the publication target, Quart web server. The EMS needs per-site state in a database, a job scheduler for the 15-minute ticks and nightly training, and FastAPI endpoints; none of this comes from EMHASS.
11. Forecast features for Pakistan: no holiday calendar, no load-shedding history as a feature, no dust or haze attenuation model; the PV correction regression and the ACI bias are the reusable parts.
12. Step and horizon defaults: 30 minutes and 1 day (config lines 4 and 74), both configurable; 15 minutes and 48 h are supported (`prediction_horizon` 192 with a Solcast or Open-Meteo window extended accordingly, docs/forecasts.md 56-69).

## 10. UNVERIFIED items and open questions

- Solve times for the EMS-sized problem (192 steps, one battery, one genset); EMHASS's "about 0.1 s per iteration" (CHANGELOG 519) is for its own household cases.
- Whether Pakistani DISCO MDI is a 30-minute average of kW; EMHASS's interval machinery supports any integer multiple of the step, so this only sets a parameter.
- Whether the target sites' gensets can run in parallel with the grid (closed transition) or only through an ATS; it changes one constraint.
- Licence versions of CVXPY (Apache-2.0), highspy/HiGHS (MIT), skforecast (BSD-3) and pvlib (BSD-3) were taken from the two guideline documents, not re-read here.
- EMHASS's `tests/test_optimization.py` (17,579 lines, 347 test functions) and `tests/test_multi_battery_optimization.py` (896 lines) were not run; the test harness pattern (build a config dict, call `create_optimization`, assert on the result columns) is worth imitating.

## 11. Reading list for the Opus build (files and line ranges in the clone)

1. `src/emhass/optimization.py` 161-434 (constructor and parameters), 2603-2862 (variables), 2864-3152 (objective), 3154-3223 (balance), 3419-3785 (battery), 2049-2082 (PWL cost), 2783-2830 and 2302-2414 (demand charge), 6562-6684 (unit-commitment patterns), 7207-7306 and 8518-8820 (solver and status policy), 6976-7204 (results), 9023-9240 (MPC entry).
2. `src/emhass/command_line.py` 214-300 (cache key), 3067-3210 (MPC wrapper), 72-114 (run and plan snapshots).
3. `src/emhass/forecast.py` 2547-2620 (persistence), 2870-2935 (last-window checks), 3214-3397 (prices).
4. `src/emhass/machine_learning_forecaster.py` 130-153, 218-258, 260-486 (lags, models, fit and predict).
5. `src/emhass/forecast_calibration.py` and `src/emhass/pv_bias_calibration.py` whole.
6. `src/emhass/plan_store.py`, `src/emhass/last_run.py`, `src/emhass/utils.py` 3894-4020 (UI injection), `docs/plan_output_schema.md`, `docs/passing_data.md` 174-410, `docs/advanced_math_model.md` 519-580.
7. `LICENSE` (section 2 above).
