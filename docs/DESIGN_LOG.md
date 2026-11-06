# Design log

Newest first. Each entry says what was decided, why, and where the plan (`EMS_BUILD_PLAN.md`) says otherwise.

## 2026-11-06 v1 build: what was built and where it differs from the plan

### The lift (Part 10.4)

- Sign-in, tenancy and the founder console are lifted from `saad-tahir/solarlytics-backend` at commit `c853130` (the owner's own code): `identity`,
  `platform`, `companies`, `routes_local`, `founder`, `routes_founder`, `store_founder`, `store_members`, `seed_demo`, `backups`, `mfa`, `auth`, `apikeys`,
  `version`, `jobs`, with `solarlytics` renamed to `gridlytics` and the roles changed to admin, operator, analyst and viewer. `ratelimit.py` is the
  `Limiter` class from Solarlytics' `routes_accept.py`; `schemas.py` keeps its refusal shape. The founder password accepts Boxlytics' plain sha256 with a
  warning, as the lifted code did.
- **Deviation:** the plan put the lifted modules in a `tenancy/` subpackage. They stay flat in `gridlytics/` so their `from . import store` imports work
  unchanged; a later move is mechanical.
- `apikeys` is site-bound (`site_id`, `kind` edge or partner); the guard refuses a key on another site's path with 403 `wrong_site`
  (`/api/sites/{id}/...` and `/api/edge/{id}/...`).
- **Lifted tests:** 141 of Solarlytics' sign-in, platform, company and founder tests pass unchanged after the renames; 7 that assert Solarlytics' own data
  (projects, role presets, its program-routes table) are skipped by name in `tests/conftest.py`, and `test_signin_guard.py` (it imports Solarlytics'
  sharing module) was not carried. Re-aiming them is a follow-up.

### Engine

- **Forecasters (Part 5):** stages 0 and 1 only. The PV chain is a small numpy model (NOAA sun position, Haurwitz clear sky scaled for haze, isotropic
  transposition, PVWatts-style DC/AC) instead of pvlib, so the API has no heavy dependency; no NWP feed yet (Open-Meteo needs the owner's paid plan, O5).
  Stage 1 fits a performance ratio and a clearness distribution from the last 14 days, with smart persistence for the first two hours. Load stage 1 is
  equivalent-day persistence with the last-two-hours ratio. The outage model is Beta(8, 2) schedule adherence plus an hourly unscheduled hazard shrunk to
  a DISCO prior with seven days' weight, and an empirical median duration; the logistic and boosted models are later items.
- **Forecast storage:** one JSON body per issue in the telemetry file's `forecast_issues` table, not a row per target time; scoring reads the issue.
- **Telemetry:** the `minute` table as specified; the 15-minute `quarter` table is not materialised (reads aggregate on the fly by resolution; a year of a
  site still answers in milliseconds); `telemetry_days` is written by the import, the twin and the nightly job.
- **Optimiser (Part 4):** the formulation as written, on scipy's `milp` only (no highspy warm start, no structural cache: a 192-step genset case solves
  in about 0.05 s on the twin sites). At sites whose genset starts on an AMF panel the reserve covers at most two steps (the start delay and a failed
  start), priced at the genset's marginal cost. The terminal value applies to the energy above the floor (`E_use <= E[T] - E_min`).
- **Mode A compiler (Part 7.1):** boundaries are dropped by the energy they move, not by a twin-priced value; every slot carries `grid_cap_w` (the plan's
  MDI cap) so the device peak-shaves to it, and a grid-charging slot charges at its mean planned rate. Brand grammars are a dict in `delivery.py`, not
  JSON files yet. The wrong-plan guard replays the table through the twin and refuses a plan whose state of charge crosses the floor or that imports in
  a forecast outage.
- **Battery service:** reads the estimate column (or the BMS) from telemetry and grows sigma from the last anchor; coulomb counting belongs to the edge.
- **Savings (Part 8):** the actual arm against the native capture and a tuned table (a grid search over eight static TOU tables on the observation week,
  cached per month), priced per minute on the bearable basis with the dated fuel price and the same wear charge; carry-over credited at the terminal value;
  the perfect-foresight bound is a 15-minute hindsight solve of the day. MDI is a monthly lever from the arms' monthly peaks. Days in the native blocks
  of a switchback calendar are labelled `native_block`; a day claims its saving only when its fidelity is `confirmed`.
- **Reports:** ReportLab PDF and CSV for the monthly savings report, the energy-only (viewer-safe) report and the daily operations sheet.
- **Demo (Part 9.6):** four sites as the plan describes; the history defaults to 21 days (`GRIDLYTICS_DEMO_DAYS`; the plan says 90) so a seed takes a
  couple of minutes. The history runs the native settings for three observation days, then each day's plan; the monitor-tier mall keeps its native
  settings (a recommendation changes nothing on site), so it shows no claimed saving, honestly.
- **CSV upload:** the API takes the file as a raw `text/csv` body (python-multipart is not a dependency); a multipart body is parsed minimally.
- **Scheduler:** APScheduler in the API process behind `GRIDLYTICS_SCHEDULER=1`; tick at :00/:15/:30/:45 plus 20 s, nightly at 00:40 PKT.

### Money on the wire

- The viewer filter strips every key in `money.MONEY_KEYS` and every key ending `_rs`, `_rs_per_kwh`, `_rs_per_kw` or `_pkr`; the API walk test proves a
  viewer gets none of the plan's prices, costs, savings, levers or fuel prices.
