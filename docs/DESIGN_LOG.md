# Design log

Newest first. Each entry says what was decided, why, and where the plan (`EMS_BUILD_PLAN.md`) says otherwise.

## 2026-11-07 Round 4: the handoff's open work, checked on public data

### The PV model against public data

`backend/tests/fixtures/public` holds one year (2020) of hourly PVGIS 5.2 and NASA POWER data for the four demo cities, with each source and
licence in its `SOURCE.md`. `public_data.py` fetches the data by hand; tests never call the network.

- **Clear sky:** Haurwitz times a monthly haze factor. Each month's factor is the highest ratio of measured GHI to Haurwitz on the clearest fifth of
  that month's days, across the four sites and both providers. It replaces the single 0.88, which put clear winter days 15-20 % above "clear sky".
- **Glass reflection loss** (Martin and Ruiz, 0.16) is modelled: 2.4-2.6 % against PVGIS's 2.6 %.
- **Fit to PVGIS:** driven by PVGIS's GHI, the model is within 2.2 % of PVGIS's annual output at all four sites.
- **`MONTH_K`** is each month's mean clearness against the new clear sky. It is fitted to PVGIS's 2005-2020 monthly output, with the GHI averaged
  with NASA POWER's climatology, because NASA sees Punjab's winter fog and ERA5 does not. Stage 0's annual error went from -6..-12 % to within
  ±5.2 %.
- **PV quantiles** are capped at `min(cs × max(1, pr), kw_ac)`: a plant fitted above the nominal clear sky keeps its clear days, but is never
  forecast above its inverter's AC rating.
- **Later item, not done:** blending yesterday's clearness 50/50 with the 14-day median beats the median alone by 4-9 points on this data. It was not
  done because the twin's daily clearness is drawn independently each day, so the blend would make the demo's forecasts worse.

### Savings

- **Masked minutes with a read load** price each arm on its own battery and genset flows against that load. This replaces round 3's known limit
  (grid and genset columns blank together could over-claim). Minutes with no load reading keep the charge-only rule.
- **A blank genset** is read from the balance where the grid flow is known. Where the balance cannot be closed and the set could have run (an outage,
  a parallel set with the grid up, or the set running at the edge of the stretch), the day is withheld: `claim_withheld: genset_unknown`.
- **A state-of-charge spread** across an import-price edge is priced at its least value: discharge on the cheapest minutes, charge on the dearest.
- **Off-grid end-of-day energy** is valued at the genset's cost at its after-dark load ratio (`verify.night_load_ratio`), not at 75 % load.
- **The demo history plans 48 hours** (192 steps), as live ticks do. In paired continuous replays the 48-hour plans beat 24-hour ones every time.

### Optimiser and delivery

- **Off-grid soc_threshold sites:** every first plan is checked for dodged starts, whether optimal or time-limited. A plan with fuel left over gets the
  shed-rule re-solve. Otherwise it is repaired: the site's controller is replayed over it, respecting the daily run-hour cap, and the problem is
  solved again with the genset fixed. The label says "Repaired to the site's controller". This retires round 3's known limit and its
  "a plan that burns the whole tank is kept" rule.
- **Charge slots** charge at their mean planned rate only up to their last planned charge step, recorded as `charge_end_ts`.
- **Time limits:** every plan records `time_limited`, `mip_gap` and `time_limit_s`. A relaxed retry after a timed-out first solve is flagged too.
  `plans.time_limited_recent(site_id, hours)` counts them. The limits themselves are unchanged.

## 2026-11-07 The demo's negative savings: plan and delivery out of step

The end-to-end walk on a freshly seeded demo showed the fleet losing money: Rs -13,409 for the month, from the Karachi feed mill and the off-grid
farm. Two read-only investigators traced it to three product defects. In each, the plan asked for something the compiled slot table cannot make a device
do. The ledger was pricing fairly: the per-day ledger landed within about Rs 1,000 of an 11-day continuous replay.

- **Charging from the genset:** the optimiser planned short full-power genset runs that charged the battery. No brand grammar has a
  charge-from-genset slot, and on site the genset input only follows the load. At the farm the plan expected 1,675 kWh of genset charging and the twin
  delivered 66 kWh. The battery sat near its floor after each run, and 30 of 42 starts were rescues (native made 22). The plan now never counts on it:
  `a_gen` is `gen_charge_allowed` and `delivery.GEN_CHARGE_SUPPORTED`, which is False. The site flag stays stored for the day a brand supports it.
  A second row was tried and dropped: "off-grid, charge only from PV beyond the load". It closes a small loophole (PV charging while the genset
  carries the load), worth about Rs 2,400 over 11 days at the farm. But it made the 120 L farm solve take 23 s against 6 s, and a tick has 10 s.
- **The 2-slot compile:** to fit Sungrow's 2 slots, `compile_mode_a` dropped the run that moved the least energy. A hold run moves none, so it always
  folded into its neighbour with more energy: the evening discharge. That discharge then started at 15:00-17:00 instead of the plan's 20:45, emptied the
  battery off-peak, and had nothing left for the 18:30-22:30 peak. A run that does not discharge now folds into a neighbour that does not discharge,
  where there is one. (Part 7.1 asks for boundaries ranked by a twin-priced value; this rule fixes the case at hand, and the full ranking stays a
  later item.)
- **The charge slot's demand cap:** a merged charge slot charges at its mean planned rate across its whole span, including the morning block that sets
  the month's maximum demand. Its `grid_cap_w` was the larger of the largest planned import and the MDI cap, so it never stopped the charge, and the
  monthly MDI rose by 16-19 kW. A charging slot is now capped at the plan's MDI cap alone.

Replays from the investigators: the farm goes from about -17,900 to +7,400 to +8,300 Rs over 11 days, and the Karachi period from -42,200 to about
+54,300 (daily levers plus MDI). Days without an outage stay honestly negative, about -1,100 to -1,250 each. That is the premium for holding the
outage reserve, which the outage days repay.

Karachi rebuilt from scratch for 14 days, one configuration at a time:

| Configuration | Total | MDI lever |
|---|---|---|
| Old compiler | +21.1k | +5.6k |
| Compiler fixes with genset charging planned | +35.8k | -24.1k |
| Both fixes | +39.3k | +3.4k |

**Wall-clock limits:** plan quality depends on CPU load, because the solve limits (5 s in the demo build, 10 s per tick) are wall-clock time. A demo
seeded while the test suite ran in parallel made visibly worse plans. Seed the demo on an otherwise idle machine.

## 2026-11-07 Review fixes, round 3, and the web app's backend asks

### Optimiser

- **Hold against force_charge around an outage:** a Hold (reserve) binds only on grid-up window steps before the window's first outage step. A
  force_charge binds on every grid-up step of its window, including after the grid returns, so one set during an outage charges once the grid is back.
  Off-grid, both bind on every window step. The shed sum for both starts at the window's first step, so load shed in an outage inside the window counts
  against the rows that follow. Without that, rows after the outage, priced at VoLL per step, made the plan shed the outage's critical load to keep the
  battery full.
- **Off-grid soc_threshold sites:** the first solve has no shed rule, for solve time. A time-limited plan that sheds with the set off, the tank not
  empty and fuel left at the horizon's end is solved once more with a shed rule, and the summary says so (`solver.shed_rule_resolve`).
- **Known limit, accepted for v1 (superseded by round 4: every such plan is now repaired to the controller):** a 10 s plan whose time-limited incumbent burns the whole tank (300 to 310 L of the 400 L farm) can still shed with
  the set off while fuel remains. Solved to optimality, those plans shed nothing, but that takes 75 to 90 s. Off-grid soc_threshold control is not the
  default and no demo site uses it. A tick there can take up to three solver limits (about 30 s): a first solve, the relaxed retry and the shed-rule
  re-solve. Repairing this by replaying the threshold controller over the plan is a later item.
- **Legacy net metering at ICP sites:** the ICP uncertain-case rows count net import (G - X), as the bill nets exports per period.

### Telemetry, sites and forecasts

- Forecast stage rules count minutes of data, not 1-minute rows: samples times the source's median interval (at most 15 minutes). This applies to load,
  PV and outage.
- `SiteFields` carries `load_peak_kw`, `off_grid` and `data_source`. A demo site's stored `twin` is kept unchanged on update and cannot be changed
  through the API. The server, not the client, records who confirmed the grid signal and when.
- Imports carry `from_ts`, `to_ts` and `file_name`. The columns are added by `telemetry._imports_columns` outside the schema version, so only columns are
  added and no rows change.
- A new telemetry file is set up under a process lock, and the WAL switch is retried on locked or busy.
- `sites.put_state` reads and writes in one write transaction, as `routes_common.state_change` does, so a tick, an outage event and a tier sign-off never
  write over each other's fields.

### API

- **OPT-15 re-solve:** the pending re-solve is read, merged and cleared in one transaction. The wait fallback runs only for a stored telemetry batch
  that has a reading from a minute after the event's minute. `since` restarts when the deciding event changes.
- **GET /plan** answers `{plan: null, held: {reason, until, plan_id}}` as soon as a pause or release is in force, before the next tick.
- **Drafts** are owned by the member's id. A draft id another member holds answers 409 `draft_taken`.
- **Connections:** a save is idempotent on the client's key, or else on regime, tariff category and meter reference. The last connection of an active,
  grid-connected site cannot be removed (409).
- **Alerts:** Control and Savings alerts (plan 9.3), `plan_failed` included, wait for an active plan and 7 days of telemetry.
- **Reports:** `preliminary` is stored when a report is made (workspace schema version 2, with a backup before the migration). The energy-only report
  has its own permission, held by all four roles.
- The tier sign-off is kept in `site_state.tier_signoff`, with the audit line as the fallback for earlier changes.
- The savings answer names its litres' source (`energy.litres_source`: meter, curve or null) for ranges up to about two months.

### Savings: blank columns

- **Unresolved minutes** are only those whose balance cannot be closed: load unread, genset unknown, or no state of charge on either side. A battery
  flow taken from two states of charge more than 30 minutes apart is rebuilt on the balance for every arm, but those minutes count toward the 5 %
  unresolved cap, so a long gap is withheld rather than smeared.
- **Blank battery power:** the actual arm's flow comes from the balance (metered grid with known load and genset), else from the change in state of
  charge left over. That change may span any number of masked minutes but at most 30 others. Where the flow stays unknown, no arm's masked charge is
  priced and the day claims nothing (`claim_withheld: battery_flow_unknown`). Withholding only the wear lever would credit the baseline's whole wear on
  a day the actual arm cycled more.
- **Known limit (superseded by round 4: masked minutes now price each arm's own flows against the read load):** with the grid and genset columns blank for up to 5 % of a day (the genset unknown), the masked minutes price no arm's load. On a site
  whose baseline discharges in that gap while the actual holds, the day can still over-claim (a reproduced case: +3,839 against a metered +560). It
  needs both columns blank at once, and it predates round 3. A tighter cap or symmetric discharge pricing on masked minutes is a later item.

### Demo

- The twin writes only minutes before the clock's current minute. The next advance deletes twin minutes left at or after the clock.

## 2026-11-07 Review fixes, rounds 1 and 2

An adversarial review of v1 confirmed 69 findings, 13 of them high (`.scratch/review/*.json`). Rounds 1 (`507a4db`) and 2 (`32eafe5`) fixed them in
file-disjoint groups, each change checked by a separate verifier. The rules those rounds settle:

### Optimiser and plan

- **Manual floor (Hold, force_charge):** at a grid-connected site a Hold binds only on grid-up window steps before the window's first outage step. The
  reserve is used in that outage and is not refilled inside the window. At an off-grid site it binds on every window step. Shed load never counts
  towards keeping a floor.
- **grid_charge_allowed = False:** the battery charges only from the forecast PV beyond the load on grid-up steps (plus the genset where
  `gen_charge_allowed`). Plan steps carry `grid_charge_permitted`, so delivery never compiles grid charging there. The compiler has no charge-from-genset
  mode: genset charging with the grid up compiles as `charge_pv_only`, which is the safe direction.
- **soc_threshold genset control:** the controller's starts are treated like AMF panel starts. They are exempt from the run-hour cap and from min up/down,
  and happen in an outage only at or under the threshold. The fuel cap stays physical, and threshold rows lapse where the set cannot run. In grid
  outages a per-step shed rule stops shedding being used to dodge a start. Off-grid sites have no such rows, to keep solve time down (they tripled the
  farm solve time). Fuel burnt before step t is a running-sum variable.
- **Pause and release to native:** while either is in force, no plan is in force, even when the solve fails. A tick stores its plan as `held` (reason
  `control_paused` or `released_to_native`), the previous plan is superseded, and the edge gets a signed `release` message with no slots. `active_at`
  answers None for any instant inside a pause.
- **Observed grid state:** a tick reads it up to its actual time. A same-quarter re-solve never replaces an issue that observed an outage with a blind
  one, unless telemetry shows the grid back.
- **Billing month:** connections carry an optional `reading_day` (1-31; null means the calendar month). The month-to-date counters, M0, rest of month,
  `in_month` and the MDI month end follow the bill's period. Legacy net metering passes its per-period exports. Its export is priced 0.01 Rs/kWh below
  the import it nets, so equal prices cannot give circulating flows (the ledger adds the 0.01 back).
- **Signed messages** carry no money in the signed, stored or served body. A `plan_messages` row is written only by the edge's key-only pull.

### Tariffs and bills

- Adders that list their billing months ignore `effective_to`. A correction with the same start supersedes the old version. Tariff and fuel starts must
  be canonical `YYYY-MM-DD`.
- Bills can be entered with the printed reading dates (`previous_reading_date`) and are stored as the first day billed (`rebuild(...,
  period_start_exclusive=True)`).
- ICP sites: the ledger and the prices page price from the billing month's scaled period totals, not a 1e9 placeholder.

### Telemetry and forecasts

- **Untrusted input (plan Part 2.8):** times must lie between 2015-01-01 and a day after now. `ingest` skips and counts rows outside that range
  (`out_of_range`). The edge route refuses such a batch with 422 so the stream never stalls. A numeric cell above `CELL_MAX` (1e9) or a non-finite value
  is stored as missing and counted. A CSV upload refuses repeated header names, any out-of-range time and a span over 400 days, stores nothing on a
  refusal, and aggregates only the days that received rows. CSV exports quote text cells that start with `=`, `+`, `-` or `@`.
- **Missing columns:** where a minute has neither energy column, import and export come from `grid_power_w`. `g["soc"]` falls back to the BMS reading,
  while `g["soc_est"]` keeps the estimate alone. Battery trust compares the estimate with the BMS only where both were measured.
- **Load censoring (plan 5.2):** outages are read over known samples. A gap of up to 15 minutes, or a grid-up blip of at most 2 samples, does not end
  an outage. The reference is the median of the last 15 or fewer known grid-up samples within 60 minutes. The 40 % drop is tested on the first known
  samples after the loss, and the whole outage is censored.
- A stored non-finite day row is healed on read. Where its minutes are gone, it becomes null, never zero.
- `forecast_scores` has one `baseline_value` column, so the climatology Brier is returned but not stored.

### Savings

- **Advisory confirmation needs evidence:** without battery power, or with no scorable test and a table that cannot be told apart from native, the day
  stays `pending` and claims nothing. `not_applied` (closer to native, with a 2 % of battery margin) is decided before `confirmed`. This is a
  simplified form of Part 7.6.
- **Import completeness:** blank grid minutes in the actual arm are rebuilt from the balance. Where that is impossible, the minute is priced on neither
  arm. A day whose metered import share is under 0.5, or with more than 5 % unresolved, is withheld from the claim, the verified days, the MDI months and
  the switchback regression.
- **Unmetered genset fuel** is unknown (`genset_unknown`, litres null), never zero.
- **Native replay:** `backup` mode fills the battery from PV to soc_max, and `backup_soc_pct` is only the grid-charge level. `feed_in_first` is modelled
  too. CSV and BMS-only sites start their replays from the measured state of charge, not 50 %.
- "Preliminary" waits for the switchback weeks and the FCA, so every demo site shows it today.

### API and security

- `/api/edge/*` and the signed plan message take a site API key only. A person's session gets 403 `key_required`. A key holds no role permission and
  never gets money fields, whatever role it was made with.
- **Edge events are stored once:** the sequence claim and the events are written in one company-file transaction.
- **OPT-15:** an outage event re-solves an active site once telemetry shows the event's minute, or after 120 s. It runs at most once per 30 s, and a
  pending re-solve is dropped after 15 minutes. Resume re-solves at once.
- **Money settings:** changing voll, the battery's replacement cost or warranty, or the genset's start, run-hour and O&M costs needs
  `edit_money_settings`. So does deleting a battery or genset whose money settings an administrator changed.
- **Genset requests:** one run holds one pending request. A start that moves at least a step earlier withdraws the request ("Moved earlier to HH:MM")
  and asks again. While no plan is in force, every open request is withdrawn.
- The CSV upload parses on a worker thread, so the event loop stays free.

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
