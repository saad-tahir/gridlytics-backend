# R1: evcc-io/optimizer as a seed for the OnyxLeaf EMS dispatch engine

Research note for the OnyxLeaf TOU-, outage- and genset-aware EMS (Product 1). Written 6 November 2026 from a shallow clone of https://github.com/evcc-io/optimizer taken the same day. Every line number below refers to that clone (commit `1bb387d9b8a6c10cece85a3413360d02891ab679`, merged 2026-11-04 14:16:52 +0100, "Merge pull request #305 from evcc-io/explain-flat-grid-hint"). Anything not checked against a primary source is marked UNVERIFIED.

The question this note answers: should the dispatch engine for Product 1 be a fork of evcc-io/optimizer, or a re-implementation that borrows its structure? Product 1's engine must add, on top of what evcc already models: genset unit commitment (on/off binary, minimum load, minimum up and down times, start cost, HOMER-style fuel curve), grid availability per step (outage windows force import and export to zero), a reserve-SoC constraint for the next forecast outage, a monthly MDI peak-demand charge on any rise above the peak to date, ICP baseline pricing, net-billing export credit with no simultaneous import and export, a piecewise-linear battery wear cost, unserved critical load slack, and a terminal value of stored energy.

Short answer (details in sections 10 to 12): re-implement. Keep evcc's architecture (dataclass inputs, one model-builder class with `_setup_variables` / `_setup_target_function` / `_add_*_constraints`, soft limits with a tiered penalty base, a clean objective recomputed from the schedule, JSON golden cases, a validity check on every solver result) but write the model fresh on HiGHS through `highspy` (or `scipy.optimize.milp` for a dependency-free start), because fewer than a third of evcc's 1,456 model lines survive the target feature set unchanged, the EV-specific half (charge goals, charge demand, minimum charge power gates, continuity stage, priorities) is dead weight for a C&I site, and the CBC-specific tolerance engineering that makes up most of the file would have to be redone for HiGHS anyway.

---

## 1. Repository facts

| Item | Value | Source |
| --- | --- | --- |
| URL | https://github.com/evcc-io/optimizer | clone |
| Description | "EV and battery charging optimizer" | GitHub API, fetched 6 Nov 2026 |
| Licence | MIT, copyright "(c) 2025 andig" | `LICENSE` lines 1 to 3 |
| Created | 2025-07-21T19:20:48Z | GitHub API |
| Last push | 2026-11-04T13:16:55Z | GitHub API |
| Stars / forks / open issues | 99 / 24 / 30 | GitHub API, 6 Nov 2026 (the Product 1 report counted 90 stars on 30 Oct 2026) |
| Language | Python (model and API), Go (CLI client only) | `pyproject.toml`, `go.mod` |
| Package version | 0.2.0 | `pyproject.toml` line 4 |
| Python | `requires-python = ">=3.13"`; `.python-version` says 3.14; Docker image `python:3.14-slim` | `pyproject.toml` line 7, `.python-version`, `Dockerfile` lines 2 and 27 |
| Authors | andig (andi@evcc.io), ekke | `pyproject.toml` line 6 |
| Inspiration | "Inspired by https://github.com/Akkudoktor-EOS/EOS/pull/462" | `README.md` line 11 |
| Model size | `src/optimizer/optimizer.py` is 1,456 lines; `app.py` 473; `explain.py` 300; `feedback.py` 98; `settings.py` 55; `gunicorn_conf.py` 51 | `wc -l` on the clone |
| Tests | 19 `tests/test_*.py` files holding 114 `def test_` functions (count by grep), plus `tests/testbench.py` (manual tool) and 20 golden JSON cases in `test_cases/` | clone |
| Production | Azure Container Apps, 1 vCPU / 2 GiB per replica, 1 to 50 replicas, gunicorn 4 workers, `OPTIMIZER_TIME_LIMIT=10`, `OPTIMIZER_NUM_THREADS=1` | `infra/main.bicep` lines 121 to 211, `Dockerfile` lines 52 to 56 |

### Dependencies (locked)

From `uv.lock` (resolved) and `pyproject.toml` (declared minimums):

| Package | Declared | Locked | Role |
| --- | --- | --- | --- |
| pulp | >=4.0.1 | 4.0.1 | model builder; `problem.add_variable`, `LpSolveStats` are pulp 4 API |
| highspy | >=1.15.1 | 1.15.1 | HiGHS in process for `OPTIMIZER_SOLVER=highs` and `hybrid`; also imported for the interrupt callback types |
| numpy | >=2.5.4 | 2.5.4 | penalty base, result checks |
| flask | >=3.1.3 | 3.1.3 | HTTP service |
| flask-restx | >=1.3.0 | 1.3.2 | request validation and Swagger models |
| gunicorn | >=22.0.0 | 23.0.0 | production server |
| pydantic / pydantic-settings | >=2.14.1 / >=2.15.0 | 2.14.1 / 2.15.0 | `OPTIMIZER_*` env settings |
| pyjwt | >=2.15.1 | 2.15.1 | optional bearer-token auth |
| pandas, matplotlib, tabulate | unpinned | 2.3.3 / 3.11.2 / - | only `tests/testbench.py` |
| azure-data-tables, azure-identity | >=12.7.0 / >=1.26.0 | - | the `/optimize/explain/feedback` store only |
| werkzeug | >=3.1.9 | 3.1.9 | Flask dependency |
| dev: pytest 9.1.1, ruff 0.17.0, autopep8 2.3.2, locust 2.46.7 | | | `pyproject.toml` line 34 |

CBC is not a Python dependency: pulp 4 "no longer bundles a solver and resolves `cbc` from PATH" (`Dockerfile` line 37). The image downloads the CBC 2.10.10 static release build for amd64 (`Dockerfile` lines 23 to 25, sha256 pinned) and the Debian `coinor-cbc` package on arm64; CI installs the same 2.10.10 build (`.github/workflows/ci.yml` lines 25 to 29). The cbcbox wheel from `pulp[cbc]` is deliberately avoided: "it ships a CBC devel build that solves the captured requests 1.5 to 3 times slower than the 2.10 releases" (`Dockerfile` lines 37 to 39).

Relevance to the OnyxLeaf `ops` conda environment (Python 3.12, scipy 1.17 with `scipy.optimize.milp`, no pulp, no highspy): evcc's code cannot be imported there as is (requires Python 3.13+, pulp 4, highspy). Nothing from the clone was executed for this note.

## 2. File-level map of the repository

Paths relative to the clone root. Line counts from `wc -l` on 6 Nov 2026.

| Path | Lines | What it is | Relevance to Product 1 |
| --- | --- | --- | --- |
| `LICENSE` | 21 | MIT, "Copyright (c) 2025 andig" | governs everything below; see section 11 |
| `README.md` | 141 | service description, example, stage pipeline (mermaid), solver notes, API summary, dev commands | the clearest statement of the design; quoted in sections 5 to 7 |
| `AGENTS.md` | 92 | rules for AI coding agents: architecture, "domain knowledge" bullets on the objective, penalties, pulp 4 API, CBC quirks, test conventions | a model of how to document an engine for an agent-driven build; worth imitating |
| `pyproject.toml`, `uv.lock`, `.python-version` | 47 / - / 1 | packaging (hatchling), deps, ruff and autopep8 config (line length 160) | dependency list in section 1 |
| `Dockerfile` | 58 | two-stage `python:3.14-slim`, uv sync, CBC 2.10.10 static build on amd64, non-root user, gunicorn 4 workers, `OPTIMIZER_TIME_LIMIT=10` | pattern for the engine container |
| `Makefile` | 129 | build, lint, test, run, loadtest, docker, fly, Azure deploy, `slow` log collection | - |
| `fly.toml`, `infra/main.bicep` | 13 / ~220 | fly.io and Azure Container Apps deployment (1 vCPU, 2 GiB, 1 to 50 replicas, KEDA CPU scaling) | sizing reference |
| `openapi.yaml` | 653 | source of truth for the API contract; formulation summary in the `info.description` | request/response schema, section 8 |
| `src/optimizer/__init__.py` | 0 | empty | - |
| `src/optimizer/optimizer.py` | 1,456 | the model: dataclasses (lines 220 to 252), `Optimizer` class: penalty base (314 to 355), variables (370 to 485), objective (487 to 608), energy balance and grid constraints (610 to 726), battery constraints (728 to 825), solver selection (827 to 861), HiGHS interrupt (863 to 888), stage gap and timing (890 to 918), pin/unpin integers (920 to 939), preference stage (941 to 1052), continuity stage (1054 to 1185), clocks and gap (1187 to 1207), probe-then-split (1209 to 1280), validity checks (1282 to 1301), `solve()` and result extraction (1303 to 1419), clean objective (1421 to 1456) | the subject of sections 3 to 7 |
| `src/optimizer/app.py` | 473 | Flask + flask-restx service: JWT check (21 to 44), blocked clients (47 to 59), timed body read (62 to 71), slow-request dump (85 to 122), restx models (161 to 232), `POST /optimize/charge-schedule` (235 to 379), `/optimize/explain` (411 to 440), `/optimize/explain/feedback` (443 to 462), `/optimize/health` (465 to 469) | section 8 |
| `src/optimizer/settings.py` | 55 | pydantic-settings `OptimizerSettings`, env prefix `OPTIMIZER_`: `num_threads`, `solver` (cbc/highs/hybrid), `cbc_path`, `gap_abs` 0.02, `gap_share` 0.0025, `probe_seconds`, `preference_budget` 0, `time_limit`, `log_subject`, `blocked_clients`, `dump_slow_requests`, Azure OpenAI and feedback-table settings | pattern to copy |
| `src/optimizer/explain.py` | 300 | renders a plan as text and asks an Azure OpenAI chat model to explain it; `SYSTEM_PROMPT` describes the model rules in plain words | the plain-language rule list (lines 10 to 60) is a good template for an OnyxLeaf "why this plan" feature |
| `src/optimizer/feedback.py` | 98 | stores explanation votes in an Azure table | - |
| `src/optimizer/gunicorn_conf.py` | 51 | reaps orphaned `cbc` child processes after a SIGKILLed worker (production incident 2026-07-25) | a reason to run the solver in process (HiGHS) rather than as a subprocess (CBC) |
| `tests/test_app.py` | 321 | golden-case runner over `test_cases/*.json` plus API-level tests | section 9 |
| `tests/test_*.py` (18 more) | 82 to 385 each | unit tests per mechanism: bad requests, clean objective, continuity (two files), explain, feedback, fractional solutions, gap share, gunicorn conf, HiGHS, hybrid, infeasible solutions, objective scaling, objective split, peak levelling, penalty base, stage timings, unknown strategy | section 9 |
| `tests/testbench.py` | 244 | CLI: create/update/run a golden case, print tables, plot with matplotlib | pattern for a golden-case tool |
| `tests/conftest.py` | 49 | fixtures for explain and feedback tests | - |
| `test_cases/009-*.json` to `028-*.json` | 20 files, 109 to 3,755 lines | golden cases: `request`, `expected_response`, optional `strict`, some `test_items`; horizons 4 to 245 steps, mixed `dt` (169 s to 3,600 s), 1 to 2 batteries | section 9 |
| `tools/bench_peak_leveling.py` | 197 | benchmark script for the peak-levelling strategy | - |
| `cmd/client.go`, `client/client.gen.go`, `tools/*.go`, `go.mod` | - | Go CLI that posts a request and draws ASCII charts; client generated from `openapi.yaml` (oapi-codegen config in `tools/cfg.yaml`) | not needed |
| `locustfile.py` | ~15 | load test posting random golden requests | pattern |
| `docs/comparison_objective_terms.md` | 47 | tier table of every objective term with its multiple of `penalty_base` and scaled coefficient range | section 5 |
| `docs/img/*.png` | 6 | README charts | - |
| `probe-pulp.mps`, `.mst`, `.sol` | - | a CBC scratch triple committed at the root (UNVERIFIED whether intentional) | ignore |
| `.github/workflows/ci.yml`, `deploy.yml`, `publish.yml` | 31 / - / - | CI: autopep8 diff, ruff, install CBC 2.10.10, pytest | - |

No GitHub releases exist (GitHub API, 6 Nov 2026); the service is deployed from `main`. 12 contributors: andig 114 commits, naltatis 29, dependabot 27, ekkea 18, lfloeer 6, six others 1 to 2 each (GitHub contributors API, 6 Nov 2026).

## 3. The model, as written (variables, constraints, objective)

Conventions in the code: energy in Wh per step, power limits in W, prices per Wh, `dt[t]` in seconds, so a W limit becomes `limit * dt[t] / 3600` Wh (`AGENTS.md` line 37). The problem is a **maximisation** (`pulp.LpProblem("EV_Charging_Optimization", pulp.LpMaximize)`, line 363); costs and penalties are negative terms. Index `i` is a battery (home storage or EV), `t` a step, `T = len(gt)` (line 276).

### 3.1 Inputs (lines 220 to 252)

- `GridConfig`: `p_max_imp` (W, optional), `p_max_exp` (W, optional), `prc_p_exc_imp` (currency per W, optional demand rate). A demand rate is active only when both `p_max_imp` and `prc_p_exc_imp` are set (line 310).
- `BatteryConfig`: `charge_from_grid`, `discharge_to_grid` (bool), `s_capacity`, `s_min`, `s_max`, `s_initial` (Wh), `c_min`, `c_max`, `d_max` (W), `p_a` (currency per Wh, terminal value), `p_demand` (Wh per step, optional "must charge"), `s_goal` (Wh per step, optional "reach by"), `c_priority` (int), `c_active` (bool, charging at horizon start).
- `TimeSeriesData`: `dt` (s), `gt` (load Wh), `ft` (PV Wh), `p_N` (import price per Wh), `p_E` (export price per Wh).
- Constructor defaults: `eta_c = eta_d = 0.95`, big-M `M = 1e6` (line 261).

### 3.2 Decision variables (lines 370 to 485)

| Name in code | Index | Type and bounds | Meaning | Lines |
| --- | --- | --- | --- | --- |
| `c[i][t]` | battery, step | continuous, `[0, c_max*dt/3600]` | charge energy, Wh | 376 to 381 |
| `d[i][t]` | battery, step | continuous, `[0, d_max*dt/3600]` | discharge energy, Wh | 384 to 389 |
| `s[i][t]` | battery, step | continuous, `[0, s_capacity]` | SoC **after** step t, Wh (the hard bound is the capacity, not `s_min`/`s_max`) | 392 to 397 |
| `s_goal_pen[i][t]` | only where `s_goal[t] > 0` | continuous >= 0 | shortfall below the goal | 402 to 407 |
| `p_demand_pen[i][t]` | only if `p_demand` given | continuous >= 0 | shortfall below the required charge | 410, 418 |
| `z_p_demand[i][t]` | only if `p_demand` given | binary | 1 when `s_max` is within one `c_min` step, relaxes the `c_min` gate | 412, 419 |
| `z_s_max_reached[i][t]` | only where `p_demand[t] > 0` | binary | 1 when `s_max` is fully reached, switches the demand off | 414, 423 |
| `s_max_pen[i][t]`, `s_min_pen[i][t]` | battery, step | continuous >= 0 | SoC above `s_max` / below `s_min` | 426 to 429 |
| `n[t]`, `e[t]` | step | continuous >= 0 | grid import / export energy, Wh | 432 to 433 |
| `e_imp_lim_exc[t]` (pulp name `p_imp_pen_t`) | only if `p_max_imp` set | continuous >= 0 | import above the limit | 438 |
| `z_imp_lim[t]` | only if `p_max_imp` set | binary | gate: overshoot allowed only when regular import sits at the limit | 441 |
| `e_exp_lim_exc[t]`, `z_exp_lim[t]` | only if `p_max_exp` set | continuous >= 0, binary | same for export | 445, 447 |
| `p_max_imp_exc` | scalar | continuous >= 0 | horizon maximum import power above the threshold, W (demand rate) | 452 |
| `p_imp_peak`, `p_exp_peak` | scalar per levelled side | continuous >= 0 | horizon maximum grid power per side, W (peak strategies) | 456 to 457 |
| `y[t]` | step | binary | flow direction: 1 = export allowed, 0 = import allowed | 463 to 465 |
| `z_c[i][t]` | only if `c_min > 0` | binary | charging on/off (minimum charge power gate) | 468 to 476 |
| `z_cd[i][t]` | battery with `c_max>0 or d_max>0` | binary | 1 = discharging allowed, 0 = charging allowed | 480 to 485 |
| `cost_slack` | scalar, added in stage 2 only | continuous `[0, slack]` | lets the cost bound tolerate solver rounding | 976 |
| `charge_start_{i}_{t}` | continuity candidate copy only | continuous `[0,1]` | counts a charge start | 1157 |

Per step and per battery, without `p_demand`/`s_goal`/limits: 3 continuous battery variables, 2 penalty variables, 1 `z_cd` binary, plus `z_c` if `c_min > 0`; per step 2 grid variables and 1 `y` binary. For a 192-step, 2-battery request that is about 2,880 variables of which 576 binaries (768 with `c_min` on one battery, 960 on both) and about 2,500 rows; the stored 245-step case 028 is "4450 variables, 1263 binaries" (`tests/test_peak_leveling.py` line 12).

### 3.3 Constraints

Written as the code adds them. `dth = dt[t]/3600`.

Energy balance, every t (lines 621 to 650):

    sum_i (d[i][t] - c[i][t]) + ft[t] + n[t] + e_imp_lim_exc[t] == e[t] + e_exp_lim_exc[t] + gt[t]

(the `*_lim_exc` terms exist only when the matching limit is set). There is no curtailment variable: surplus PV must go to a battery, to the grid, or into the export-overshoot penalty variable.

Flow direction, every t (lines 658 to 667):

    e[t] <= m_exp[t] * y[t]
    n[t] <= m_imp[t] * (1 - y[t])

with `m_exp[t] = ft[t] + dth * sum(d_max of batteries allowed to discharge to grid)` and `m_imp[t] = gt[t] + dth * sum(c_max of batteries allowed to charge from grid)`; the global `M = 1e6` is used instead whenever the opposite side's limit is set, because the unbounded overshoot variable then enters the balance (comment lines 652 to 657). This is the "no simultaneous import and export" gate; the code's own rationale is to stop import-to-export arbitrage when `p_E > p_N` and to implement the grid-charge and grid-discharge permissions (lines 459 to 462).

Grid import limit, every t, if `p_max_imp` set (lines 670 to 688; identical branches for the demand-rate and plain-limit cases):

    n[t] <= p_max_imp * dth
    p_max_imp*dth - n[t] <= p_max_imp*dth * z_imp_lim[t]      (z_imp_lim = 0 forces n to sit at the limit)
    e_imp_lim_exc[t] <= M * (1 - z_imp_lim[t])                (overshoot only when z_imp_lim = 0)

Grid export limit, every t, if `p_max_exp` set (lines 691 to 698): the same three rows on `e`, `z_exp_lim`, `e_exp_lim_exc`.

Peak tracking, every t, per levelled side (lines 708 to 719):

    (n[t] + e_imp_lim_exc[t]) * 3600/dt[t] <= p_imp_peak       and/or the export analogue

Demand rate, every t, if active (lines 723 to 726):

    e_imp_lim_exc[t] <= p_max_imp_exc * dth

SoC soft band, every i, t (lines 735 to 738):

    s_max_pen[i][t] >= s[i][t] - s_max
    s_min_pen[i][t] >= s_min - s[i][t]

SoC dynamics (lines 741 to 754):

    s[i][0] == s_initial + eta_c * c[i][0] - (1/eta_d) * d[i][0]
    s[i][t] == s[i][t-1] + eta_c * c[i][t] - (1/eta_d) * d[i][t]      t >= 1

Charge goal, t >= 1 where `s_goal[t] > 0` (lines 757 to 761):

    s[i][t] + s_goal_pen[i][t] >= s_goal[t]

Minimum charge demand, every t with `p_demand` given (lines 764 to 795); with `p_dem = min(c_max*dth, p_demand[t])`:

    (s_max - s[i][t]) <= (1 - z_p_demand[i][t]) * s_max + c_min*dth        [z_p_demand may be 1 only near full]
    (s_max - s[i][t]) <= (1 - z_s_max_reached[i][t]) * s_max              [z_s_max_reached may be 1 only when full]
    c[i][t] + p_demand_pen[i][t] >= (1 - z_s_max_reached[i][t]) * p_dem   [soft "must charge"]
    z_p_demand[i][t] <= 0                                                 [steps without a demand]
    c[i][t] + c_min*dth * z_p_demand[i][t] >= c_min*dth * z_c[i][t]       [c_min gate, relaxed near full]
    c[i][t] <= c_max*dth * z_c[i][t]

Minimum charge power without a demand, if `c_min > 0` (lines 798 to 804):

    c[i][t] >= c_min*dth * z_c[i][t]
    c[i][t] <= c_max*dth * z_c[i][t]

Grid permissions (lines 807 to 816):

    not charge_from_grid:   c[i][t] <= c_max*dth * y[t]          (charge only in export steps, i.e. from surplus)
    not discharge_to_grid:  d[i][t] <= d_max*dth * (1 - y[t])    (discharge only in import steps)

Charge/discharge lock, every i, t (lines 819 to 825):

    d[i][t] <= d_max*dth * z_cd[i][t]
    c[i][t] <= c_max*dth * (1 - z_cd[i][t])

Stage-2 cost bound (line 977), added only on the split path:

    cost_objective + cost_slack >= cost_found - preference_budget      (row named COST_BOUND)

Continuity candidate (lines 1148 to 1160), added to a **copy** of the problem:

    cost_objective >= cost_found - 1e-5
    preference_objective * scale >= preferred - 1e-5
    p_side_peak <= peak_found + 1e-5
    charge_start[i][t] >= z_c[i][t] - z_c[i][t-1]       (z_c[i][-1] := c_active)
    objective: minimise sum charge_start

### 3.4 Objective (lines 487 to 608), maximised

Real money (`cost_objective`, lines 498 to 566):

    - sum_t (n[t] + e_imp_lim_exc[t]) * p_N[t]                       import cost (overshoot pays for energy too)
    + sum_t e[t] * p_E[t]                                             export revenue
    + sum_i s[i][T-1] * p_a_i                                         terminal value of stored energy
    - prc_p_exc_imp * p_max_imp_exc                                   demand rate on the horizon peak above the threshold
    - prc_soc_exc_pen * sum_{i,t} (s_max_pen + s_min_pen)             SoC band, penalty_base * 1000 per Wh per step
    - prc_e_goal_pen * sum s_goal_pen                                 unmet goal, penalty_base * 100
    - prc_p_goal_pen * sum_t p_demand_pen * (1 + (T-t)/T)             unmet demand, penalty_base * 100, early steps weigh up to 2x
    - prc_e_grid_imp_pen * sum e_imp_lim_exc                          import limit, penalty_base * 100 (only without a demand rate)
    - prc_e_grid_exp_pen * sum (1 - t*1e-5) * e_exp_lim_exc           export limit, penalty_base * 100, decaying so overshoot lands late

Tie-breakers (`preference_objective`, lines 568 to 600), cost-neutral by construction:

    charge_before_export:     - sum_t e[t] * min_import_price * 2e-5 * (T - t)
    attenuate_*_peaks:        - p_side_peak * penalty_base * 1e-3
    discharge_before_import:  - sum_t n[t] * min_import_price * 5e-6 * (T - t)
    priorities:               + sum_{i,t} (c[i][t] + d[i][t]) * min_import_price * 5e-5 * (T - t) * c_priority_i

The model handed to the solver is `(cost + preference) * objective_scale` (line 608), with `objective_scale = 1e6 / max|coefficient|` (lines 60 to 81) because "prices given per Wh" put the raw coefficients near CBC's 1e-7 tolerances (lines 38 to 43). The reported `objective_value` is `get_clean_objective_value()` (lines 1421 to 1456): import cost, export revenue, `(s[T-1] - s_initial) * p_a` and the demand-rate charge only, no penalties and no preferences.

### 3.5 Penalty base (lines 314 to 352)

    penalty_base = max(max p_N, max p_E, max_i p_a, prc_p_exc_imp * 3600 / min(dt) [if demand rate active], 1e-4)

The demand-rate conversion over the **shortest** step is deliberate: 1 Wh kept out of the binding step is worth `3600/dt` W of the rate (comment lines 328 to 332; `tests/test_penalty_base.py` lines 27 to 31 and 65 to 77 pin it). The tier table in `docs/comparison_objective_terms.md` lines 5 to 19 lists every multiple: SoC band x1000, goals, demand and limits x100, real prices x1, peak levelling x1e-3, the other tie-breakers x2e-5 to 5e-5 per remaining step.

## 4. How soft constraints keep the model from returning "infeasible"

The README's claim (line 20): "Never returns 'infeasible' for a goal it cannot reach. Goals, minimum charge demand and grid limits are soft constraints backed by penalties, so an over-constrained request still yields the best achievable schedule plus a flag telling you which limit was violated."

What is hard and what is soft in the model:

| Rule | Hard or soft | Mechanism | Lines |
| --- | --- | --- | --- |
| Energy balance | hard | equality row; `n[t]` and `e[t]` have no upper bound, so any load or surplus can always be absorbed by the grid (or by the overshoot variable when a limit is set) | 646 to 650 |
| SoC dynamics | hard | equality rows; feasible with `c = d = 0` whenever `0 <= s_initial <= s_capacity` | 741 to 754 |
| SoC inside `[0, s_capacity]` | hard | variable bounds | 395 |
| SoC inside `[s_min, s_max]` | soft | `s_min_pen`, `s_max_pen`, `penalty_base * 1000` per Wh per step; an `s_initial` outside the band is driven back "at maximum" rate | 426 to 429, 532, 735 to 738 |
| Charge goal `s_goal` | soft | `s_goal_pen`, `penalty_base * 100` | 402 to 407, 542, 760 |
| Charge demand `p_demand` | soft | `p_demand_pen`, `penalty_base * 100 * (1 + (T-t)/T)` | 418, 546 to 548, 783 |
| Grid import limit | soft | `e_imp_lim_exc` (unbounded) gated by `z_imp_lim`, `penalty_base * 100` per Wh, or the demand rate if one is given; flagged as `grid_import_limit_exceeded` | 438 to 441, 556, 670 to 688, 1361 |
| Grid export limit | soft | `e_exp_lim_exc` gated by `z_exp_lim`, `penalty_base * 100 * (1 - t*1e-5)`; flagged as `grid_export_limit_hit` | 445 to 447, 562, 691 to 698, 1367 |
| Minimum charge power `c_min` | hard | `z_c` gate; always satisfiable with `z_c = 0` | 789 to 804 |
| No import and export in one step, grid permissions, charge/discharge lock | hard | binaries; always satisfiable | 658 to 667, 807 to 825 |

Because every bounded quantity has an unbounded slack with a penalty, the only way the model is infeasible is contradictory input, and the API rejects the two known cases with a 400 before solving: `c_min > c_max` and `s_initial > s_capacity` (`app.py` lines 311 to 319, comment: "contradictory limits came back as a bare Infeasible with nothing naming the battery"). The `Infeasible` status label still exists (lines 167 to 171), and the slow-request dump records such responses with the token subject because "in production it is a handful of installations sending the same request every 15 minutes" (`app.py` lines 93 to 96).

Two consequences for Product 1:

1. The tiering is numerically fragile. Tier 1 penalties sit at `penalty_base * 1000`, the objective is rescaled so the largest coefficient is 1e6, and the big-M rows carry 1e6, so the model spans "up to 1e8" within one model (line 74). The file documents the fallout at length: the pinned tie-break LP is declared infeasible by CBC "on ~6% of production splits" over a bound its own incumbent satisfies (lines 1012 to 1014), a slack ladder widens the bound tenfold per retry up to 1e-2 currency (lines 104 to 111), CBC "writes a column vector that breaks the balance rows by whole charging steps" when stopped inside root heuristics (lines 1284 to 1289), so every kept result is re-validated with `problem.valid(1e-2)` and an integrality check (lines 1282 to 1301, 1342 to 1344). A re-implementation should keep the idea (slack + penalty, flag in the response) but choose penalties in currency terms that the product can explain (value of lost load, cost of a missed reserve) rather than multiples of the largest price, and should keep big-M values tight per step the way lines 658 to 663 already do for the direction gate.
2. There is no load-shedding slack. Load is always served because grid import is unbounded. When the target engine forces import to zero during an outage, the balance can only close through battery, genset, curtailment or an explicit unserved-load variable; that variable is new work (section 9).

## 5. Time step and horizon

- The step is an input: `dt[t]` in seconds per step, validated as `> 0` (`app.py` line 193) and equal in length to every other series (lines 295 to 306). "Time steps do not have to be equally long: `dt` is given per step, so a schedule can be fine grained for the next hour and coarse for tomorrow" (README line 118).
- What the evcc client sends: "Slots are 15 minutes, the first one only covers the rest of the current quarter hour, slots after 48 hours are an hour long" (`explain.py` lines 19 to 20). The stored golden cases run from 4 steps (case 022) to 245 steps (case 028: 244 steps of 900 s after a first step of 169 s). Case 020 has 154 steps, cases 009 to 019 have 29 to 43 steps with 3,600 s steps and a short first step.
- The client version `evcc/0.304.1` is refused by default because it "sends 400 to 480 step horizons the solver cannot close: 65 percent of its requests came back Not Solved after burning the probe and cost stage caps" (`settings.py` lines 36 to 39). That is the practical ceiling of this model on CBC with a 10 s budget on one vCPU: about 250 steps with two batteries.
- Re-solve cadence: the explain prompt says evcc "recomputes the plan every few minutes with fresh forecasts and only acts on the next slot" (`explain.py` lines 66 to 67); the dump comment mentions installations "sending the same request every 15 minutes" (`app.py` line 95). The exact evcc cadence was not verified from the evcc repo (UNVERIFIED).
- SoC is reported **after** each step (`s[i][0]` already includes step 0's charging, lines 744 to 747), which is why the clean objective values stored energy against `s_initial` and not `s[0]` (lines 1443 to 1448, `AGENTS.md` line 42). The initial SoC is not part of the output series.

## 6. Solver selection, stages and measured run time

Everything here is from `README.md` lines 54 to 90 and the constants at `optimizer.py` lines 88 to 217 unless stated.

Solver: `OPTIMIZER_SOLVER` = `cbc` (default; the CBC binary found on PATH or `OPTIMIZER_CBC_PATH`, 2.10.10 in the image), `highs` (HiGHS in process through `highspy`, `pulp.HiGHS`), or `hybrid` (CBC for the joint probe, HiGHS for cost, tie-break and continuity). On HiGHS the relative gap is forced to 0 so only the absolute gap stops it (line 848), and a wall-clock interrupt callback (`_interrupt`, `_hard_limit`, lines 863 to 888; `highspy.cb.HighsCallbackType.kCallbackMipInterrupt` and friends) cuts a run `CUTOFF_MARGIN = 0.5 s` past its limit while keeping the incumbent. CBC runs as a subprocess PuLP waits on without a timeout (line 877); its `-sec` "is only tested between branch and bound nodes" (`AGENTS.md` line 47), which is why the gunicorn hook reaps orphaned `cbc` processes.

Stages of one request, all under one wall clock `OPTIMIZER_TIME_LIMIT` (10 s in production):

| Stage | What | Clock | Lines |
| --- | --- | --- | --- |
| build | build the pulp model, scale the objective to `OBJECTIVE_TARGET = 1e6` | measured, typically tens of ms (not stated numerically; UNVERIFIED) | 1313 to 1315, 43 |
| probe | one solve of cost + preferences with the absolute gap; proven (`Optimal` or `GapLimit`) means path `joint` and nothing else runs on money | `PROBE_SHARE = 0.2` of the limit; "p95 on a joint solve lands near 1.5 s" (line 132) | 1219 to 1246 |
| cost | money only, absolute gap `OPTIMIZER_GAP_ABS = 0.02` currency, raised to `gap_share = 0.25 %` of the goal energy at the mean import price (`_gap_abs`, lines 1192 to 1207: "9 ct and no proof in 110 minutes on the request of #186, where 2.5 ct proves the same schedule in 1.5 s") | `COST_TIME_SHARE = 0.3`, with `PREFERENCE_TIME_SHARE = 0.25` reserved; "every split ran 5.4 s of it and 17 percent of them ended past the limit. The split is 3 percent of the traffic and 38 percent of the CPU" (lines 189 to 191) | 1248 to 1280 |
| tie-break LP | pin the binaries, move only continuous variables under the cost bound; slack ladder 1e-5 to 1e-2 | `LP_PREFERENCE_TIME_LIMIT = 1 s`; "worst measured 0.165 s over the stored cases" (line 197) | 1004 to 1024 |
| tie-break MILP | whole model under the cost bound, no warm start (CBC 2.10.3 "returned a strictly worse schedule and reported it as proven optimal", lines 1037 to 1042) | `MILP_PREFERENCE_TIME_SHARE = 0.1` | 1026 to 1045 |
| continuity | copy of the model, minimise charge starts under cost, preference and peak bounds | `CONTINUITY_TIME_SHARE = 0.1` on joint, `0.25` on split; skipped when the incumbent's own solve took longer than the gate; "ran 5 to 13 times over a 1 s limit on CBC in production (#194)" (line 160) | 1054 to 1185 |

Measured production figures quoted in the source:

- "Measured over 170 captured requests, 150 of them slow ones: HiGHS alone is the fastest in sum but proves 8 of the 54 mid-sized requests, which CBC solves in 1 to 5 s, 2.5 to 5 times slower, overruns the probe and pays a 6 to 8 s split for each, which lifted production p99 from 6.4 to 8.5 s. `hybrid` matches CBC on those (p90 2.05 against 2.01 s) and keeps HiGHS's quality on the slow ones (11 CBC Not Solved become 1)" (README lines 88 to 90; also `_stage_solver` docstring lines 828 to 836).
- "the joint path is 97 % of the traffic" (line 203); mean solve 0.57 s (line 204); p95 0.99 s before the preference reserve, 1.41 s after (line 151).
- "On HiGHS the stage is the one that hits its cap: 44 of 53 benchmark splits ran the full 2.5 s" for the tie-break MILP (lines 181 to 183).
- Long case 028 (245 steps, 4,450 variables, 1,263 binaries): "a long horizon HiGHS needs about 3 s for" (`tests/test_highs.py` line 61); CBC's time on it is not stated.
- Cost of the leveling strategy: the dropped ramp term "cost around a fifth of the solve time of a leveling case" (lines 349 to 350).

Reading for Product 1: a two-battery EV-centred model at 200 to 250 steps already needs this much machinery to finish under 10 s on one core with CBC. HiGHS proves the hard cases better but is slower on the easy ones; the authors kept CBC as default for latency, which is a home-EMS concern (one request per site every few minutes, 50 replicas). For a C&I SaaS re-solving every 15 minutes per site, a 10 to 30 s HiGHS solve per site is acceptable, and the probe/split/tie-break/continuity ladder (about 600 of the 1,456 lines) is not needed if the preferences are folded into the objective in currency terms.

## 7. Service layer, input and output schemas

`src/optimizer/app.py` is a Flask app with flask-restx models (also mirrored in `openapi.yaml`, which the Go client is generated from).

Endpoints: `POST /optimize/charge-schedule` (lines 235 to 379), `POST /optimize/explain` (411 to 440, needs Azure OpenAI settings), `POST /optimize/explain/feedback` (443 to 462, needs an Azure table), `GET /optimize/health` (465 to 469). Swagger UI is served at `/`. Optional bearer JWT (HS256, `JWT_TOKEN_SECRET`, lines 21 to 44); the subject is logged only if `OPTIMIZER_LOG_SUBJECT` is set.

Request body (`OptimizationInput`, lines 161 to 208):

    {
      "strategy":   {"charging_strategy": "none|charge_before_export|attenuate_demand_peaks|attenuate_feedin_peaks|attenuate_grid_peaks",
                     "discharging_strategy": "none|discharge_before_import"},            optional
      "grid":       {"p_max_imp": W, "p_max_exp": W, "prc_p_exc_imp": currency/W},         optional, each field optional
      "batteries":  [{"charge_from_grid": bool, "discharge_to_grid": bool, "s_capacity": Wh (default s_max),
                      "s_min": Wh*, "s_max": Wh*, "s_initial": Wh*, "p_demand": [Wh], "s_goal": [Wh],
                      "c_min": W*, "c_max": W*, "c_active": bool, "d_max": W*, "p_a": currency/Wh*, "c_priority": int}],
      "time_series": {"dt": [s]*, "gt": [Wh]*, "ft": [Wh]*, "p_N": [currency/Wh]*, "p_E": [currency/Wh]*},
      "eta_c": 0.95, "eta_d": 0.95                                                          optional
    }

(* = required). Validation: restx schema (`validate=True`), then all series equal length (400 with a `lengths` map, lines 297 to 306), non-empty, `c_min <= c_max`, `s_initial <= s_capacity` (311 to 319). Any other parsing error is a 400 "Invalid data format"; any solver exception is a 500 (378 to 379). The `openapi.yaml` example passes `"M": 1000000` (line 85) but `app.py` hardcodes `M=1e6` (line 335); whether restx rejects the unknown key was not checked (UNVERIFIED).

Response (`OptimizationResult`, lines 222 to 232; built at `optimizer.py` 1372 to 1419):

    {
      "status": "Optimal|Feasible|Not Solved|Infeasible|Unbounded|Undefined",
      "objective_value": currency (clean: import cost, export revenue, (s_T - s_initial)*p_a, demand rate) or null,
      "limit_violations": {"grid_import_limit_exceeded": bool, "grid_export_limit_hit": bool},
      "batteries": [{"charging_power": [Wh], "discharging_power": [Wh], "state_of_charge": [Wh]}],
      "grid_import": [Wh], "grid_export": [Wh], "flow_direction": [0|1],
      "grid_import_overshoot": [Wh], "grid_export_overshoot": [Wh]
    }

`Optimal` means proven within the gap, `Feasible` means a schedule the clock stopped on, everything else carries empty series. Despite the field names, `charging_power` and `discharging_power` are energies per step in Wh (lines 212 to 213).

Observability baked into the service, worth copying: one JSON log line per request with `read`, `validate`, `elapsed`, per-stage seconds, `path`, `preferences`, `continuity`, `cost_stage_value`, `cost_stage_gap`, `status`, `steps`, `respond`, `total` (lines 346 to 371); a JSON-lines dump of slow, unproven, unsolved or infeasible requests in the same `{"request": ...}` shape as the golden cases so they can be replayed by the test harness (lines 85 to 122).

## 8. Test style

- 19 test modules, 114 test functions (grep count), run with `uv run pytest` on ubuntu with CBC 2.10.10 installed (`ci.yml`). No coverage tool configured.
- **Golden cases**: `tests/test_app.py` lines 15 to 76 parametrise over `test_cases/*.json`, post each `request` through the Flask test client, and assert `status` and `objective_value` (`numpy.isclose`, `rtol=1e-5`, `atol=1e-4`, with `OPTIMIZER_GAP_ABS` pinned to 0.01 because "stored objectives are proven optima"). Cases marked `"strict": true` also compare `grid_import`, `grid_export` and every battery's charge and discharge series with `atol=1` Wh. An independent invariant then checks that with `c_min` set every step charges nothing or at least `c_min`, except when the battery is nearly full (lines 60 to 76). `tests/testbench.py` creates, updates and plots cases. `AGENTS.md` lines 64 to 68: "Prefer a data driven case over a dedicated test module", "Cover both the success and the limit violation paths", do not mark strict a case with several equally optimal schedules.
- **Solver-independent suites**: `test_highs.py` and `test_hybrid.py` re-run every golden case on HiGHS and hybrid with the gap switched off and compare to the stored optimum at `abs=1e-4, rel=1e-5` (`test_highs.py` lines 21 to 34).
- **Mechanism tests by monkeypatching the solve**: `pulp.LpProblem.solve` or `optimizer.problem.solve` is replaced to return a `TimeLimit`, `Infeasible` or `has_solution=False` stats object, or to scribble a fractional or row-breaking vector into the variables, and the tests assert that the stage refuses it, falls back, or reports `Not Solved` (`test_infeasible_solution.py`, `test_fractional_solution.py`, `test_app.py` lines 94 to 116 and 157 to 200). Rationale in `AGENTS.md` line 47 and `test_app.py` lines 99 to 102: CBC's `-sec` cannot be used to provoke a truncation in a test.
- **Unit tests of the arithmetic**: `test_penalty_base.py` (six tests on the penalty base and the SoC floor against a demand rate), `test_clean_objective.py`, `test_gap_share.py`, `test_objective_scaling.py` (scaled vs unscaled objective within 1e-6 over three cases and three strategies), `test_objective_split.py` (the cost/preference split stays exhaustive, the MILP tie-break cap, the slack ladder), `test_peak_leveling.py` (peak variables exist only for levelled sides, no ramp term, what the dropped ramp costs), `test_continuity.py` and `test_continuity_api.py` (starts counted, clock gates, running sessions not interrupted), `test_stage_timings.py` (which stage keys each path produces).
- **API tests**: `test_bad_request.py` (malformed JSON, length mismatches, logged fields), `test_unknown_strategy_is_rejected.py`, auth and subject logging, slow-request dumps.
- Style: each test states in a comment the production incident or issue number it pins (#19, #75, #114, #132, #146, #159, #170, #186, #193, #194), uses small hand-built `Optimizer(...)` instances for arithmetic and the golden cases for behaviour, and never asserts on timing values beyond `>= 0`.

## 9. Element by element: lift as is, adapt, or write new

Target engine elements from the Product 1 brief against what `optimizer.py` holds. "Lift" means the construction can be copied with a notice; "adapt" means the construction exists but needs re-parametrising or a changed sign convention; "new" means nothing in the repo does it.

| Target element | Verdict | What evcc has | What has to change | Effort (dev-days, fork path) |
| --- | --- | --- | --- | --- |
| Energy balance with PV, load, battery, grid | lift | lines 621 to 650 | add genset output, curtailment and unserved load terms to the row | 0.25 |
| Battery SoC dynamics with charge and discharge efficiency | lift | lines 741 to 754 | none; one battery per site is the common case, the per-battery loop stays | 0 |
| Charge/discharge lock | lift | lines 819 to 825 (`z_cd`) | optional: can be relaxed when prices never reward simultaneous charge and discharge (prototype, section 12) | 0 |
| Soft SoC band (`s_min`/`s_max`) with flags | lift | lines 426 to 429, 532, 735 to 738 | add a `soc_band_violated` flag to the response (today only the grid limits are flagged) | 0.25 |
| Net-billing export credit: asymmetric `p_N[t]` / `p_E[t]` | lift | per-step price vectors, lines 505 to 517 | none; the tariff engine supplies the vectors | 0 |
| No simultaneous import and export | lift | `y[t]` gate, lines 658 to 667, with tight per-step big-M | keep the tight-M idea; when a limit is set the code falls back to `M = 1e6`, which the target should avoid by bounding the overshoot by the site's connection size | 0.25 |
| Terminal value of stored energy | lift | `s[i][T-1] * p_a`, line 521; clean objective 1443 to 1448 | `p_a` must come from the tariff engine (value of the next expensive hours or the next outage), not a constant | 0.25 |
| Monthly MDI charge on any rise above the peak to date | adapt | the demand rate: `prc_p_exc_imp * p_max_imp_exc`, where `p_max_imp_exc` is the horizon maximum of import power **above `p_max_imp`** (lines 449 to 452, 526, 670 to 679, 723 to 726) | set `p_max_imp = max(peak_to_date, sanctioned-load floor)` in W and `prc_p_exc_imp = MDI rate / 1000` per W; the term is then `rate * max(0, horizon_peak - peak_to_date)`, which is the target. Two side effects to handle: the `z_imp_lim` gate forces regular import to sit exactly at the threshold before overshoot is allowed (fine), and `penalty_base` jumps to `rate * 3600 / min(dt)` (line 332): at Rs 1,250/kW and 900 s steps that is Rs 5 per Wh against prices of Rs 0.02 to 0.04 per Wh, so every tier-1 penalty is inflated by two orders of magnitude | 0.5 |
| ICP baseline pricing (flat price above a monthly baseline) | new | nothing; one price vector per step | either (a) the tariff engine fixes the step at which the baseline is exhausted from month-to-date metering and the load forecast and hands the engine one price vector, or (b) two import variables per step plus a monotone "baseline exhausted" binary chain and a running counter (prototype, section 12: this chain is the single most expensive addition, 5 to 7 s alone at 192 steps) | 0.5 (a) or 2 (b) |
| Grid availability per step (outage windows) | new, trivial | nothing; `n[t]`, `e[t]` are unbounded | set `n[t].upBound = e[t].upBound = 0` (and the overshoot variables) where `avail[t] = 0`; requires the unserved-load slack below or the balance may become infeasible during an outage with an empty battery | 0.5 |
| Unserved critical load slack | new | nothing; load is always met from the grid | new variable per step in the balance, bounded by the non-critical share if load shedding of non-critical circuits is allowed, penalised at a value of lost load in Rs/kWh; flag in the response | 0.5 |
| Reserve SoC for the next forecast outage | new, simple | the `s_goal` mechanism (soft lower bound on SoC at a step, lines 757 to 761) is the same shape | reuse the `s_goal` row with the reserve vector as `s_goal[t]`, but the penalty must be a real number (the diesel or lost-load cost of an empty battery at outage start), not `penalty_base * 100` | 0.5 |
| Genset unit commitment (on/off, min load, min up/down, start cost, HOMER fuel curve) | new | nothing; the `z_c`/`c_min` minimum-charge gate (lines 798 to 804) is the same construction as min load, and the continuity candidate (lines 1131 to 1161) counts starts the same way a start variable would | new variable block `u`, `v`, `w`, `g` per step, fuel `F0*u + F1*g`, min up/down windows; must enter the energy balance and the penalty base. About 60 lines of model code plus tests | 2 to 3 |
| Piecewise-linear battery wear cost | new | nothing | convex PWL on throughput (segment variables, no binaries) or on depth of discharge; EMHASS's `battery_stress_cost` is the published pattern (not re-checked here; see `R2-emhass.md` in this folder) | 1 |
| Horizon and step | lift | per-step `dt` | none | 0 |
| Soft constraints, never infeasible | adapt | tiered `penalty_base` multiples (lines 314 to 352) | the target wants penalties in Rs the product can explain; keep the slack-and-flag pattern, drop the multiples-of-largest-price scheme | 1 |
| Objective scaling, probe/split/tie-break/continuity ladder | drop | lines 38 to 217, 827 to 1301 (about 600 lines) | preferences that matter for a C&I site (fewer genset starts, flatter grid profile) are real money and belong in the single objective | 0 to drop; carrying cost if kept |
| EV features: `s_goal`, `p_demand`, `c_min` gates, `c_priority`, `c_active`, strategies | drop | lines 399 to 423, 535 to 548, 568 to 600, 756 to 804 | not needed; keep `s_goal` only as the reserve row | 0 |
| Solver on HiGHS with a wall-clock interrupt | lift | `_solver`, `_interrupt`, `_hard_limit`, lines 842 to 888 | only if pulp is kept; on `highspy` directly the same callback is three lines | 0.25 |
| Validity re-check of every solver result (`problem.valid`, integrality) | adapt | lines 1282 to 1301, 1342 to 1344 | the idea transfers; the implementation is pulp-specific | 0.5 |
| Clean objective recomputed from the schedule | lift | lines 1421 to 1456 | extend with fuel, wear, MDI, unserved load; this is the number the savings verification compares | 0.5 |
| Flask service, restx validation, JSON logging, slow-request dump | adapt | `app.py` | OnyxLeaf's backend is FastAPI with pydantic; the validation and log-line design transfer, the code does not | 1 |
| Golden JSON cases, strict mode, invariants, testbench | lift (pattern) | `tests/test_app.py`, `testbench.py` | re-record every case for the new model | 1 |

Sum on the fork path: about 10 to 14 developer-days of model and test work, not counting the Python 3.13 / pulp 4 / CBC toolchain the fork drags in and the time to understand the 600 lines of stage logic well enough not to break it when adding terms (every new objective term must land in `cost_objective` or `preference_objective` and survive `objective_scale`, see `AGENTS.md` lines 40 and 48).

## 10. Fork-and-extend versus re-implement

### Option (a): fork evcc-io/optimizer and extend

What it buys: a working service on day one (Flask, validation, logging, Docker, 20 golden cases, 114 tests), a model whose soft-limit design has survived production since July 2025, and a maintained upstream to pull fixes from.

What it costs:

1. **Toolchain mismatch.** `requires-python >= 3.13`, pulp 4.0.1 (released 14 Oct 2026, PyPI), highspy 1.15.1 (2 Jul 2026, PyPI), and a CBC 2.10.10 binary on PATH. OnyxLeaf's backend runs FastAPI on Python 3.12 (`ops` env). The fork would run as its own container, the way evcc runs it, or the backend would move to 3.13+. pulp's own `highs` extra pins `highspy<1.14` (PyPI `requires_dist`), while evcc depends on `highspy>=1.15.1` directly; the combination works in evcc's CI, but it is a pin to watch (UNVERIFIED whether pulp 4.0.1 documents support for highspy 1.15).
2. **Most of the file is not the model.** Of 1,456 lines, roughly 460 build the model (lines 370 to 825) and the remaining 1,000 are penalty calibration constants, the four-stage solve ladder, CBC workarounds (solution-file corruption, infeasible LP over a satisfied bound, no warm start, orphaned processes) and result extraction. Less than a third of the model lines map onto the target without change (section 9), and the stage ladder exists to decide EV tie-breaks under a 10 s home-EMS latency budget that Product 1 does not have.
3. **Every addition must fit the penalty-tier scheme.** The MDI rate alone inflates `penalty_base` by two orders of magnitude (section 9); a genset with diesel at Rs 400.67/L adds another large coefficient; the target's value of lost load is larger still. The scheme that makes evcc "never infeasible" is the thing most likely to misbehave when the target's terms are added, and its failure modes (section 4, point 1) are all documented as production incidents on the simpler model.
4. **Upstream drift has no value.** Upstream work is about EV sessions, continuity, explanations in German and CBC-vs-HiGHS latency on Azure; none of it is on Product 1's path, so the "pull fixes" argument is weak, while every upstream change to the stage ladder has to be merged into a diverged file.
5. **Effort**: 10 to 14 developer-days for the model and tests (section 9), plus 2 to 3 days of toolchain and container work, plus the learning cost of the stage ladder. Call it 15 to 18 days to a tested engine, with the EV dead weight and CBC workarounds still in the tree.

### Option (b): re-implement with the same structure

What it buys: a model written in the target's own units (kW, kWh, Rs) with penalties the product can explain, no pulp, no CBC, one solver (HiGHS) in process, Python 3.12, and tests that start from the target's cases.

What it costs: writing about 1,000 lines (model builder 350 to 450, settings, validation and flags 150, service adapter 150, golden-case harness and tests 300) and re-deriving the handful of constructions evcc already got right (tight per-step big-M on the direction gate, SoC after-step convention, clean objective from the schedule, status semantics, validity re-check). Section 12's prototype, written for this note in about 260 lines, already encodes every target constraint and solves a 192-step, 768-binary instance to proven optimality in 8 to 10 s on one core with scipy's bundled HiGHS; it is not production code, but it bounds the unknown.

Effort: 10 to 14 developer-days to a tested engine with golden cases, including a day to port evcc's test conventions. Comparable to the fork's model work, without the fork's toolchain and carrying cost.

### Modelling layer for option (b)

| Layer | Licence | Fit | Notes |
| --- | --- | --- | --- |
| `highspy` directly | MIT | best | what evcc's `highs` path uses underneath pulp; in process, row-by-row model building, `setSolution` warm start, `kCallbackMipInterrupt` wall clock (lines 850 to 853 show the callback names), `mip_abs_gap` / `mip_rel_gap` / `time_limit` options; needs a thin helper for named variables and rows (the prototype's 40-line `Model` class is enough to start). Not in the `ops` env today |
| `scipy.optimize.milp` | BSD-3 (scipy) | good start | HiGHS bundled in scipy 1.17, already in the `ops` env, matrix form only, no callbacks, returns the incumbent on `time_limit` (status 1 in section 12), no warm start. Enough for the first pilot engine; swap to `highspy` when warm starts or interrupts are wanted |
| PuLP 4 | MIT | acceptable | what evcc uses; expression objects are convenient but slow to build at thousands of rows (evcc measures `build` separately for that reason, UNVERIFIED how long), CBC subprocess by default, and the `highs` extra pins `highspy<1.14` |
| CVXPY | Apache-2.0 | acceptable | what EMHASS uses; the most readable formulation and `Parameter` objects for re-solves, but a heavy dependency set (clarabel, osqp, scs, ecos) for a MILP that only ever goes to HiGHS, and canonicalisation adds seconds at thousands of variables (UNVERIFIED for this model size) |
| Pyomo | BSD-3 | not recommended | what FlexMeasures uses; general and slow to build; nothing in the target needs it |

### Recommendation

Re-implement (option b) on `highspy`, starting on `scipy.optimize.milp` because it is in the environment today, and port these things from evcc with a notice: the dataclass-shaped inputs and per-step `dt`; the direction gate with tight per-step big-M (lines 652 to 667); the SoC convention and soft band (lines 735 to 754); the demand-rate block as the MDI term (lines 449 to 452, 526, 670 to 679, 723 to 726); the "slack plus penalty plus flag in the response" pattern; `Optimal` / `Feasible` / `Not Solved` semantics with an integrality and row check on every solver result (lines 1282 to 1301, 1342 to 1344); the clean objective recomputed from the schedule (lines 1421 to 1456); the per-request JSON log line and the replayable slow-request dump (`app.py` 85 to 122, 346 to 371); the golden-case harness with `strict` mode and independent invariants (`tests/test_app.py` 15 to 76); and `AGENTS.md` as the model for the engine's own agent-facing documentation. Leave behind: the four-stage ladder, objective scaling, the EV goal and demand machinery, strategies and priorities, CBC.

Two modelling decisions the prototype settles: keep ICP ordering out of the MILP where the tariff engine can fix the exhaustion step (section 12), and keep the `y` direction binary only when a site's prices or permissions make simultaneous import and export attractive (with net-billing credit far below the import price and efficiency losses, the LP relaxation already never does both).

## 11. Licence text and notice obligation

`LICENSE` in the clone, verbatim (21 lines):

    MIT License

    Copyright (c) 2025 andig

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

`openapi.yaml` lines 30 to 32 and the GitHub API both state MIT; `pyproject.toml` declares no `license` field. A case-insensitive grep for "licen" and "copyright" over the clone (excluding `.git` and `uv.lock`) hits only `LICENSE` and `openapi.yaml`; no source file carries its own header, and no file carries a different notice.

Obligations for OnyxLeaf:

- Closed-source commercial use, modification and sale are permitted ("without restriction", "sublicense, and/or sell").
- Any copy or "substantial portion" of the code, including a port of specific constructions with the same structure and comments, must carry the copyright line "Copyright (c) 2025 andig" and the permission notice. Practical form: a `THIRD_PARTY_NOTICES.md` (or `NOTICE`) in the backend repo with the full text above and a line naming which files or functions derive from evcc-io/optimizer at commit `1bb387d`, plus a one-line header comment in each such file. Re-deriving the formulation from the README's mathematics alone does not copy code, but the table in section 9 names concrete line ranges that would be copied, so the notice should be kept regardless.
- No attribution is required in the product UI, no source disclosure, no patent grant is given (MIT has none), and the "evcc" name is not licensed for marketing (trademark use is outside the MIT grant; the Product 1 report's rule on "REopt" applies here as well).
- The MIT text is per-file-agnostic: the 89 "sponsor-only" files mentioned in the Product 1 report belong to the `evcc-io/evcc` repository (`LICENSES/exclusions.md` there), not to this one; nothing in `evcc-io/optimizer` is excluded.

## 12. Experiment: the target model on scipy's HiGHS at 192 steps

To put a number on option (b), a prototype of the **target** engine was written for this note (OnyxLeaf code, not derived from the clone; saved next to this file as `R1-target-milp-prototype.py`, about 260 lines) and run with `C:/Users/SaadTahir/anaconda3/envs/ops/python.exe -I` (Python 3.12, scipy 1.17 `scipy.optimize.milp`, HiGHS bundled) on the owner's Windows machine, single process, 6 Nov 2026. The clone's code was not executed.

What the prototype models, per 15-minute step over 48 hours (T = 192), units kW, kWh, Rs:

- grid import split into TOU-priced and ICP-priced energy; export at a net-billing credit; a direction binary `y` and availability `a_t` that forces import and export to zero in outage windows (19:00 to 21:00 both days)
- battery 200 kWh / 100 kW, efficiencies 0.95, soft SoC band with slack, charge/discharge lock binary, soft reserve-SoC rows sized to carry the critical load (50 % of load) through the next forecast outage
- genset 120 kW, on/off binary `u`, start `v` and stop `w` indicators, minimum load 30 %, minimum up and down 1 h, start cost Rs 500, HOMER fuel curve `F = 0.08 L/h per kW rated * u + 0.25 L/kWh * g`, diesel Rs 400.67/L
- MDI: scalar `peak >= peak_to_date (140 kW)` and `>= import_t`, cost Rs 1,250/kW; ICP: Rs 22.98/kWh after a 1,500 kWh remaining baseline, with a monotone "baseline exhausted" binary chain; convex 3-segment PWL wear cost on discharge throughput (Rs 7.5, 10.8, 20 per kWh); PV curtailment; unserved-load slack at Rs 500/kWh; terminal value Rs 20/kWh
- B-2(b) TOU prices Rs 35.68 peak (18:00 to 22:00) and Rs 22.83 off-peak, export credit Rs 8.13 (figures from the Product 1 report), synthetic load 40 to 200 kW and 150 kWp PV with random noise, three seeds

Solve wall time (seconds, `mip_rel_gap = 1e-4`, proven optimal unless noted; build time 6 to 20 ms in every run):

| Configuration | Variables / binaries / rows | seed 0 | seed 1 | seed 2 |
| --- | --- | --- | --- | --- |
| full, ICP with dense cumulative rows | 3,460 / 768 / 3,828 | 14.5 | 13.6 | 12.2 |
| full, ICP with a running counter (sparse) | 3,652 / 768 / 3,828 | 10.4 | 8.6 | 7.9 |
| genset only, no ICP | 3,076 / 576 / 3,061 | 3.6 | 4.9 | 3.3 |
| ICP only (sparse), no genset | 2,884 / 576 / 2,676 | 5.5 | 6.8 | 6.8 |
| neither genset nor ICP (direction and lock binaries only) | 2,308 / 384 / 1,909 | 0.30 | 0.39 | 0.33 |
| full sparse ICP, direction and lock binaries relaxed to continuous | 3,652 / 384 / 3,828 | 8.8 | 6.8 | 5.6 |
| genset only, direction and lock relaxed | 3,076 / 192 / 3,061 | 1.3 | 0.8 | 0.9 |
| no binaries at all (pure LP) | 2,308 / 0 / 1,909 | 0.03 | 0.04 | 0.03 |
| 24-hour horizon (T = 96), full sparse ICP | 1,828 / 384 / 1,908 | 4.3 | 5.5 | 5.4 |
| 24-hour horizon, genset only | 1,540 / 288 / 1,525 | 0.34 | 0.33 | 0.26 |

With a production-like `time_limit = 10 s` and `mip_rel_gap = 1e-3`: the dense-ICP model hit the limit on all three seeds with a 1.1 to 1.5 % gap left (status 1, incumbent returned); the sparse-ICP model finished two of three in 7.7 and 8.2 s and hit the limit once at 0.97 %; genset-only finished in 1.5 to 5.1 s; ICP-only in 1.8 to 6.9 s.

Readings:

1. **The genset unit commitment is cheap.** 192 on/off binaries with min up/down windows add 1 to 5 s over the LP; relaxing the direction and lock binaries brings genset-only to about 1 s.
2. **The ICP ordering chain is the expensive term**, 5 to 7 s on its own and the reason the full model needs 8 to 10 s; it costs that even when the baseline is not exhausted inside the horizon (the 24-hour runs show identical objectives with and without ICP and still 4 to 5 s). The tariff engine should fix the exhaustion step outside the MILP whenever month-to-date metering makes it predictable, and hand the engine a single price vector; keep the binary chain for the few days a month where the switch falls inside the horizon.
3. **evcc's `y` direction gate and `z_cd` lock are redundant at these prices.** Relaxing both left every objective unchanged to the last digit on every seed (252,258.8; 253,590.0; 253,932.0), because an export credit of Rs 8.13 against imports of Rs 22.83+ and a 0.9025 round-trip efficiency never reward doing both in one step. Keep them as an option for sites where permissions or prices make them bind (evcc needs them for `charge_from_grid = false`), default them off.
4. **Budget**: one 192-step solve on one core in 5 to 10 s means a single 1-vCPU worker re-solving every 15 minutes serves roughly 90 to 180 sites; the Product 1 cadence is not a constraint. A 24-hour horizon halves the time again if ever needed.
5. The prototype is not the engine: it has no tests, synthetic data, a constant MDI threshold, no genset-charges-battery flag, no PV-curtailment pricing and no multi-battery loop. It shows that the full target feature set fits in a few hundred lines on a modelling layer that is already installed, and that the run time is dominated by one term that can be moved out of the model.

## 13. Open questions and unverified items

UNVERIFIED (not checked against a primary source in this note):

- The evcc client's exact re-solve cadence and horizon composition (15-minute slots for 48 hours then hourly) are taken from comments in `explain.py` and `app.py`, not from the evcc repository.
- Whether `openapi.yaml`'s example key `"M"` is rejected or ignored by flask-restx validation.
- PuLP 4.0.0's release year (the GitHub releases page shows "25 Sep" without a year; 4.0.1 is dated 2026-10-14 on PyPI, so 4.0.0 is 2026).
- Whether pulp 4.0.1 officially supports highspy 1.15.x (its `highs` extra pins `<1.14`; evcc's tests pass with 1.15.1).
- `build` stage duration in production (the code times it, no figure is quoted).
- CVXPY canonicalisation time at this model size; EMHASS's wear-cost construction (belongs to the EMHASS research note).
- Whether `probe-pulp.mps/.mst/.sol` at the repo root are intentional.

Open questions for the build plan:

1. Will the engine run inside the FastAPI backend process (Python 3.12, `scipy.optimize.milp` now, `highspy` when added) or as its own container the way evcc does it? The evcc layout (one HTTP service, one request per solve, JSON in and out, stateless) is the right shape for either.
2. Does the tariff engine commit to computing the ICP exhaustion step, so the MILP can stay a single-price-vector model on most days?
3. Which penalties are real money (value of lost load, cost of an empty battery at outage start, genset start) and which are tie-breakers; the product's "why this plan" explanation depends on the answer, and evcc's explain prompt (`explain.py` lines 10 to 60) is the template for writing those rules down in plain words.

## 14. Sources

- Clone of https://github.com/evcc-io/optimizer at commit `1bb387d9b8a6c10cece85a3413360d02891ab679` (2026-11-04), files cited by path and line above.
- GitHub REST API, 6 Nov 2026: repository metadata, contributors, releases, latest commit.
- PyPI JSON API, 6 Nov 2026: `pulp` 4.0.1 (uploaded 2026-10-14, `requires_python >= 3.12`, `license_expression` MIT, extras list), `highspy` 1.15.1 (uploaded 2026-07-02, `requires_python >= 3.9`, `license_expression` MIT, "A thin set of pybind11 wrappers to HiGHS"), latest pulp 4.2.0.
- https://github.com/coin-or/pulp/releases (4.0.0 "25 Sep", 4.0.1 "14 Oct"; items "LpSolveStats (#942)", "Return undefined status for infeasible or unbound (#910)").
- `C:/Users/SaadTahir/Videos/solarytics/Product 1 - TOU-Aware Energy Management and Dispatch.md` lines 311 to 373 (open-source comparison, evcc-io/optimizer row and review, recommended composition, licence rules) and 424 to 436 (formulation sketch); `Perfect-EMS_TOU_Dispatch_Deep_Review.md` lines 100 to 151 and 173 (optimiser choice: "LP first, MILP only where needed").
- Prototype run logs, 6 Nov 2026, reproduced by `R1-target-milp-prototype.py <T> <time_limit> <mip_rel_gap> <relax 0|1>`.
