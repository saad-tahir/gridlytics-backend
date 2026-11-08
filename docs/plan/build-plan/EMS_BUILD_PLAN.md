# EMS dispatch SaaS: end-to-end build plan

Written 6 November 2026 for OnyxLeaf (owner: Saad). This is the master document for building OnyxLeaf's Product 1: a TOU-aware, outage-aware, genset-aware energy management and dispatch optimiser for Pakistani commercial and industrial sites, delivered as a web SaaS in two repositories (a Python backend and a React web app) in the shape of Boxlytics and Solarlytics. The owner reviews this plan; an executing agent (Opus, in Claude Code) then builds it under `EMS_BUILD_HANDOFF.md`, ticking `EMS_BUILD_PROGRESS.md`.

The working product name in this document is **Gridlytics** (package `gridlytics`, environment prefix `GRIDLYTICS_`, repositories `gridlytics-backend` and `gridlytics-frontend`). It is a placeholder in the owner's naming pattern; decision D1 below replaces it with one search-and-replace before Step 0.

## How this document is organised

| Part | What it settles |
|---|---|
| 0 | The decisions the owner must make, each with the default this plan assumes |
| 1 | What the product is: the problem, the first buyer, the value streams, the scope of v1, and what is deliberately not built |
| 2 | The architecture: components, the two repositories, control tiers and delivery modes, the 15-minute loop, what runs where |
| 3 | The data model (workspace, telemetry, beliefs, plans, events, savings) |
| 4 | The optimiser: the MILP formulation, the receding-horizon loop, the solver, the plan explanation |
| 5 | The forecasters: PV, load, outage, with stages, data providers and metrics |
| 6 | The tariff and regime engine and the 1-minute bill engine |
| 7 | Plan delivery: the Mode A slot compiler, the Mode B setpoint schedule, the edge contract, the digital twin, the ingest paths |
| 8 | Savings verification: counterfactual replay, switchback A/B, bill rebuild, reports |
| 9 | The web app: pages, lettered tabs, components, states |
| 10 | The backend skeleton, the lifted sign-in and founder console, settings, jobs, the scheduler |
| 11 | The frontend skeleton, conventions, tests, CI |
| 12 | Build order: stages, items with IDs, acceptance criteria, fixtures, gates |
| 13 | Verification, test matrix, gates and the execution protocol for the building agent |
| 14 | Open-source reuse and licence rules |
| 15 | Risks, kill criteria, owner tasks, the pilot plan and the roadmap after v1 |
| A | Appendices: the research notes this plan draws on, glossary, source list |

Sources. The two research reports the owner supplied are the authority on the market, the economics and the method: `Perfect-EMS_TOU_Dispatch_Deep_Review.md` (called "the guideline" below) and `Product 1 - TOU-Aware Energy Management and Dispatch.md` (called "the report"). Twelve research notes were written for this plan on 6 November 2026 and are cited as U1, U2, U3 and R1 to R9; they live in `plan-work/research/` and are part of the plan package (Appendix A lists them). Where this plan departs from the reports, it says so and why.

---

## Part 0. Decisions for the owner

Each decision has a default. The build is not blocked by an unanswered decision: the executing agent takes the default and records it in `docs/DESIGN_LOG.md`; the owner can override later, at the cost named in the last column.

| # | Decision | Default assumed by this plan | Alternatives | Cost of changing later |
|---|---|---|---|---|
| D1 | Product name and repository names | Working name Gridlytics; repositories `gridlytics-backend` (private) and `gridlytics-frontend`; env prefix `GRIDLYTICS_`; company header `X-Gridlytics-Company`; key prefix `glk_` | Any name in the owner's pattern (Energylytics, Powerlytics, Dispatchlytics); or ship the EMS under the Solarlytics brand as its "operate" module | One search-and-replace before Step 0; expensive after the first release (tokens, headers, keys, documents) |
| D2 | New repositories or the Solarlytics repositories | Two new repositories. The EMS is a different domain (time series, a scheduler, a solver, an edge contract) and Solarlytics' 4,470 backend tests and 6,835-line `api.py` would slow every change | Build inside `solarlytics-backend` and `solarlytics-frontend` as new modules | High: untangling later means a split like the one done in October 2026 |
| D3 | Sign-in and tenancy | Lift Solarlytics' own sign-in module (local mode: the backend is the sign-in service; one SQLite file per company; the founder console with password plus code) from `solarlytics-backend` at commit `c853130`, as U2 section 1.7 describes. Supabase mode's SQL ships for review only | Supabase Auth from day one (U1's suggestion); or Boxlytics' self-service company flow | Medium: the lift is about two days; Supabase mode is already a mode of the lifted module |
| D4 | Who gets in | As Solarlytics: no self sign-up, no Google; OnyxLeaf creates companies and people in the founder console (invitations and password resets there too until email exists); a company is switched on per company; membership roles owner, admin, member from the lift, and four app roles admin, operator, analyst, viewer with the permissions of Part 10.5; an installer is an operator in the site's company | Boxlytics-style self-service onboarding with pending approval | Low: the lifted module already holds the pieces |
| D5 | Workspace and telemetry storage | SQLite: `platform.db`, one workspace file per company (the lifted machinery) and one telemetry file per company (`companies/<id>-telemetry.db`: wide 1-minute rows keyed by site and minute, WAL, 13 months kept, then Parquet per site-month) behind a repository interface, so PostgreSQL 18 with TimescaleDB can replace it at scale without touching callers (Part 3; R7's decision table: 52 MB per site-year, hot reads in milliseconds) | PostgreSQL + TimescaleDB from day one | Medium at 100+ sites: a migration script per file; the interface keeps the code the same |
| D6 | Solver | `scipy.optimize.milp` (HiGHS inside scipy, already installed) through one model-builder module of our own, with `highspy` 1.15 as the second backend when warm starts or time-limited incumbents are wanted (R1, R7: 2 to 6 s per solve on the laptop, optimal at the root). No CVXPY, no PuLP, no Pyomo | CVXPY + HiGHS (R2's preference) | Low: the builder emits sparse matrices; a backend is one class |
| D7 | Background work | One API process per data folder; APScheduler 3.11 (MIT) inside the process for the 15-minute tick, solves on a small thread pool (scipy's solver releases the GIL), a durable run row per site per tick, a per-site lock; Solarlytics' `jobs.py` lifted for interactive work; no Redis, no Celery in v1 (R7) | RQ + Valkey workers as FlexMeasures does with Redis (R4), the 1,000-site path | Medium: the job interface is the same shape; a queue backend is one module |
| D8 | Plan delivery for the pilot | Mode A (a daily inverter TOU-slot table) in control tier 2 (advisory writes: the installer keys the table in through the vendor app; the app shows it in the vendor's own layout and confirms it from telemetry). Closed loop (tier 3) only through Product 2 or an owner-approved interim writer | Build an interim per-brand writer on a Raspberry Pi for two brands (U3 section 4.4, path 3) | Medium: the interim writer is one to two weeks and borrows Product 2's safety rules; it must be retired when Product 2 lands |
| D9 | Battery state ownership | Split by data: the edge (Product 2) owns the 1 Hz shunt estimate; the cloud owns a daily 1-minute re-estimate, the trust score and the wear-cost curve (U3 section 3.6). Both SoC numbers travel in the contract | Cloud only (the report's component 3) or edge only (the Product 2 report's WP8) | Low: the contract carries both from day one |
| D10 | Genset commands | Advisory: the optimiser's start and stop decisions are notifications (in-app plus SMS or WhatsApp with a one-tap Done or Not now reply, since the person on the floor at 19:00 is the only lever in tier 2) scored by whether the genset ran; a command only when a genset controller integration exists (U3 section 3.7). Every site records `genset_start_control` (auto on grid loss by an AMF or ATS panel, SoC threshold by a controller, or manual): at auto-start sites the genset is not a decision, the model fixes it on during outages and the genset stream is reported as "not controllable" | Whitelist a relay or controller write in Product 2 | Low: the plan vocabulary already has `genset_request` |
| D11 | Weather data | Open-Meteo: the free tier for development only (it is non-commercial and excludes the archives); a paid plan from Stage 4 (Standard about USD 29 a month for live runs, Professional about USD 99 for the Historical Forecast and Previous Runs archives, the ensemble and air quality; 2026 prices to confirm); grid-cell deduplication; forecast.solar Public as the second source; Solcast by quote later (R5). Until the archives are bought, stage 2 trains on the live forecasts stored since onboarding and the backfill is skipped with a flag on the model row | Solcast from day one | Low: a provider is one module behind the provider interface |
| D12 | Frontend stack | React 19, TypeScript 5.9, Vite 8, Tailwind 4, react-router 7, TanStack Query 5, Zustand 5, Radix primitives, recharts, vitest 5, Playwright (U1 section 7) | Stay on Vite 7, Tailwind 3 and TypeScript 5.8 to match Boxlytics exactly | Low before Step 0 |
| D13 | PDF reports | Rendered by the backend with ReportLab and matplotlib (both BSD and PSF-style) from the same numbers the API serves, in a job, stored per company (so the PDF and the screen cannot disagree); the web app offers the download. Solarlytics' Word-COM path cannot run in a Linux container | jspdf in the browser as Boxlytics does | Low |
| D14 | Transport for year one | HTTPS batch (same JSON as the MQTT contract) for every source; MQTT over mutual TLS when Product 2 exists | MQTT broker from day one | Low: one validator serves both |
| D15 | Pilot order and site qualification | Grid-tied C&I with PV, 200 kWh to 1 MWh of battery on hybrid inverters without a plant controller, and a genset with a controllable start, chosen by their own outage and genset logs (at least 20 hours a month without grid, or 40 genset hours) rather than by tariff category, because industrial feeders are largely exempt from shedding and hours follow the feeder's loss band (Part 1.2); a remote diesel-hybrid site as pilot 1b; two of three pilot sites with a controllable genset start | Off-grid and remote first (the meeting notes); or the named MWh mills (they sit behind Reon and Huawei plant controllers and are segment 2) | None in code: the optimiser handles all; it changes which sites and fixtures come first |
| D16 | Languages | English only; Urdu and Arabic deferred (the owner's Solarlytics decision of 17 October 2026) | - | - |
| D17 | Markets | Pakistan only in v1; the tariff engine is data-driven so a KSA flat tariff and a diesel-only site need data, not code (Part 6) | - | - |

Owner tasks that no code can do (also listed in Part 15): a read-only clone of `saad-tahir/solarlytics-frontend` at `C:\Users\SaadTahir\Videos\solarlytics-frontend` before Step 0 (the web app's sign-in pages, launcher and browser-check runner are lifted from it; the backup folder does not hold it), the GitHub repository pair and a push of `v1-build` at every stage exit so CI runs, pilot sites and their last 12 bills, the DISCO feeder codes, an Open-Meteo subscription, the diesel price source, a trademark knockout check on the product name, the liability wording for advisory and closed-loop control, the Enerlytics and Green Fuel Energy conversations.

---

## Part 1. What the product is

### 1.1 The problem, in the reports' words, corrected

The guideline frames the product as "TOU-aware dispatch". The report corrects the ranking of value: at a B-2 industrial site a 100 kWh battery earns about Rs 1,031 a day from grid-charged TOU arbitrage, about Rs 2,665 a day storing net-billed PV at the verified Rs 8.13 export credit (Rs 2,346 at the press's Rs 11), and about Rs 11,685 on a day it replaces diesel (October 2026 inputs, base rates). Pure TOU arbitrage nets Rs 4.8 to 10.3 per kWh on base rates (Rs 5.7 to 12.3 after duty and GST, which the engine prices), roughly the Rs 7.5 to 10.8 per kWh of battery wear it causes. So the product is a **cost-aware dispatch optimiser for sites with outages and gensets**, and TOU timing is one input among four:

1. Outage reserve and genset displacement (Rs 110 to 142 per kWh of genset energy at Rs 400.67 per litre on 30 October 2026; OGRA now prices diesel daily, Rs 382.25 on 6 November, so the engine takes a dated price).
2. Net-billing self-consumption (imports Rs 35 to 44, exports credited at Rs 8.13 in NEPRA's April to June 2026 accounting, quarterly revisable; the inverter's own self-use mode already captures most of it; the increment is in charge timing).
3. Maximum-demand (MDI) fixed charges (Rs 1,250 per kW per month where MDI sets the charge).
4. TOU arbitrage (Rs 12.85 spread at B-2; zero on units priced under the ICP flat incremental rate).

The product's own increment is the forecast-dependent part: holding reserve for a forecast outage (worth p times Rs 142 minus Rs 35.68 per kWh at outage probability p, so positive above about p = 0.25 and about Rs 106 per kWh when the outage is certain), skipping overnight grid charging before a sunny day, grid pre-charging only on cloudy days, avoiding low-load genset hours, capping MDI, and skipping cycles whose margin is below wear. The pilot must measure this increment against the inverter's own mode at 1-minute settlement; the published evidence says it is small under plain TOU and grows with demand charges, outages and genset costs.

### 1.2 First buyer and segments

| Segment | Who | Why first | What they need on screen |
|---|---|---|---|
| 1 (first) | Grid-tied industrial and commercial sites reached through installers, with PV, 200 kWh to 1 MWh of LFP on hybrid inverters and no plant controller, and a genset with a controllable start; qualified by their own outage hours (20 or more a month without grid) or genset hours (40 or more), not by tariff category: industrial feeders are largely exempt from scheduled shedding (72 percent of Karachi feeders including all industrial ones, Tribune 28 October 2026) and hours follow the feeder's loss band (zero under 10 percent losses on IESCO), and from June 2027 shedding moves from feeders to transformers; textile, food, beverage, rice; energy managers who buy on ROI | They stack all four value streams only when the outages and the genset are real; industrial storage share of lithium imports rose from 3.2 percent (2025) to 13.9 percent (January to May 2026); they mix brands (Huawei, SMA, Sungrow) | A fleet view, a daily plan with reasons, outage reserve adequacy, the monthly rupee saving against their own inverter mode in the CFO's ex-tax numbers, MDI tracking |
| 2 | The named MWh mills (Liberty, Sapphire, Lucky, Shabbir Tiles: 20 to 30 MWh behind Reon and Huawei plant controllers with reserve and genset switch-off) | Integrator-reached B-3 and B-4 sites that already have a controller; the product sits above it later | The same pages at fleet scale |
| 1b | Remote diesel-hybrid sites (farms, off-grid industry, construction) | The meeting notes' off-grid demand; the capability the GCC buys | Genset run hours, litres and starts, battery cycling, the fuel-curve fit |
| Later | Telecom tower fleets, premium homes, installer white label, Saudi remote sites | Towers buy integrated systems today (Huawei, Reon); homes have free OEM apps; KSA has flat tariffs | Fleet reporting at scale; a flat-tariff and diesel-only tariff pack |

Channel: installers and EPCs (Green Fuel Energy, Enercon, Enerlytics as a data partner) as the reports recommend; the web app therefore has a company (the installer or the site owner) that holds many sites.

### 1.3 Scope of v1 (pilot-ready)

In scope:

- Sites with PV, one battery, one or more grid connections (each with its own bill, MDI, ICP baseline and tax flags) with outages, and an optional genset on diesel or captive gas, on any Pakistani tariff category (TOU or flat), under net billing, legacy net metering or no export agreement at all (the regime `none`: most multi-MW mills exceed the prosumer caps and run on zero export, where avoided curtailment is the value), with MDI and the ICP where they apply; flat-tariff and no-grid sites as data cases of the same engine.
- Forecasts: PV, load (with censoring during outages), outage probability per feeder, critical load.
- The optimiser: a MILP economic MPC on 15-minute steps over 48 hours, re-solved every 15 minutes and on events, with genset unit commitment, outage availability, reserve, MDI, ICP, wear and a terminal value.
- Plan delivery: Mode A daily TOU-slot tables per brand grammar; Mode B 15-minute setpoints as a contract and a twin, executed only when Product 2 exists.
- Savings verification: counterfactual replay of the inverter's own mode at 1-minute resolution, switchback A/B, bill rebuild, diesel litres, peak kW, with the KPIs the report pre-registers.
- Ingest: HTTPS batch, CSV upload with saved column mappings, the digital twin, and read-only OEM-cloud pull connectors as a stretch item.
- The web app's core pages (Part 9) with sign-in, companies and roles; the founder console comes with the lifted module.
- Reports: a monthly PDF per site and per company.

Out of scope for v1 (listed so nobody builds it by accident): device drivers, register maps, the edge box and installer onboarding (Product 2); PV structure design and sizing (Solarlytics); EV charging and HVAC control; wholesale or VPP trading; utility-scale; telecom fleets as a first market; Urdu and Arabic; a mobile app; self-service sign-up; payments and plans; the Saudi tariff pack beyond data entries.

### 1.4 What "done" means for v1

| Level | Software-checkable test |
|---|---|
| Engine ready | The 192-step genset case solves in under 30 s (target under 10 s) with zero hard-constraint breaches over 1,000 replayed days; three site archetypes' golden plans reproduce; a REopt-style reference case without a genset matches within 1 percent; the genset case matches brute-force enumeration of a one-day problem |
| Bill engine ready | Each pilot site's last three bills rebuilt within 2 percent, as golden tests |
| Forecasts ready | On measured data (owner task O12's real month, then the pilot's phase 0): PV daytime skill 25 percent or better against same-hours-yesterday persistence at the 6 to 24 hour horizon class over 4 weeks; load beats equivalent-day persistence at every horizon class; the outage model beats schedule-only on the Brier score. On twin data the forecasters are only checked to run end to end, because the twin is generated by the same model families |
| Pilot-ready app | Every core page live on the demo site from day one; a plan explained on screen; Mode A tables shown per brand; a monthly report reproduced from raw data; the founder console and sign-in walk in every role; backend, frontend, verification and browser checks green on CI |
| Pilot done | 8+ weeks of switchback at 2 sites, zero safety incidents, KPIs reported, the go/no-go written against the kill criteria of Part 15 |

---

## Part 2. Architecture

### 2.1 The seven components, and where each lives

The report's seven components are kept. The table maps each to a module of the backend package (Part 10) and names the research note that specifies it.

| # | Component | Module | Inputs | Outputs | Specified in |
|---|---|---|---|---|---|
| 1 | Site and tariff model | `tariffs/` (catalogue, versions, vectors), `sites.py` | Tariff category, seasonal TOU windows, ICP baseline, billing regime and expiry, MDI basis, export cap, genset curve, critical load | Price and limit vectors per 15-minute step; the bill engine | Part 6, R9 |
| 2 | Data ingest and store | `telemetry.py`, `ingest/` (HTTP batch, CSV, twin, OEM pull) | 1-minute rows from Product 2, the twin, CSV, OEM clouds; events; fuel logs | Clean series with gap and outage flags; events | Part 3, Part 7, U3 |
| 3 | Battery state service | `battery.py` | Pack voltage, current, temperature, BMS SoC, the edge's estimate | SoC with confidence band, usable kWh, SoH, trust score, wear-cost curve | Part 4.6, R6 |
| 4 | Forecaster | `forecast/` (providers, pv, load, outage, scoring) | Weather (Open-Meteo), telemetry history, DISCO schedule | PV and load quantiles, critical load, outage probability per step, scenarios; a forecast bundle with a belief time | Part 5, R5 |
| 5 | Optimiser | `optimise/` (model, mpc, plan, explain) | Outputs of 1, 3 and 4, the site's state | A 48-hour plan: battery, grid, genset, reserve, costs per step, the binding constraints, the reasons | Part 4, R1, R2, R3 |
| 6 | Executor and safety layer | `delivery/` (Mode A compiler, Mode B schedule, contract, signing), `twin/` | The plan; the site's capabilities | The Mode A slot table per brand grammar; the Mode B schedule; signed plan messages; read-back and fidelity | Part 7, U3, R6 |
| 7 | Verification and reporting | `verify.py`, `reports/` | Telemetry, the baseline (the inverter's own mode), the tariff | Daily and monthly rupee saving, diesel litres, peak kW, KPIs, the PDF | Part 8 |

Around them: the lifted tenancy package (sign-in, companies, roles, API keys, founder console, backups), the scheduler (APScheduler on the event loop, the solves on a thread pool), the job pool, the API routes, and the web app.

### 2.2 Two repositories

```
gridlytics-backend/   private. The engine IP: tariffs, forecasters, optimiser, battery model, delivery, twin, verification,
                      the API, the tenancy package, docs, verification/ and bench/ folders (Part 10)
gridlytics-frontend/  the web app only: pages, charts, the sign-in pages lifted from Solarlytics' web app, the launcher,
                      the browser check (Part 11). It asks the API and draws the answers; no engine code runs in the browser
```

The rule from Solarlytics holds: engine IP stays in the backend; the web app never computes a price, a plan or a saving. The backend serves a built web app only where `GRIDLYTICS_WEB_DIST` names one (the release build); in development the two run side by side from the launcher.

### 2.3 Control tiers and delivery modes

Every site has two tier facts kept apart: `tier_capable`, asserted by the data source (the edge's capabilities message, or "monitor" for a source with no write path), and `tier_allowed`, set by the company's admin after the conformance check and the installer's sign-off; the effective tier is the lower of the two, so neither a site key nor a page can raise it alone:

| Tier | Name | What the product does | Who applies the plan |
|---|---|---|---|
| 1 | monitor | Ingests telemetry, forecasts, plans, scores the counterfactual saving; shows "what we would have done" | Nobody; recommendations only |
| 2 | advisory_writes | As tier 1, plus the Mode A slot table shown in the vendor's layout per brand and an "applied" record with read-back from telemetry; fidelity scored | A person, through the vendor app or the inverter screen |
| 3 | closed_loop | Signed plans delivered to the edge (Mode A or Mode B); acks, read-back, fallback events | Product 2 (or an owner-approved interim writer) |

Two delivery modes, chosen per device from its capabilities:

- **Mode A**: a daily table of N inverter TOU slots (Deye 6, Huawei 14, Growatt 3+3 or 9, Sungrow emulated), each with a mode, SoC floor or target, power limits, grid-charge and gen-charge flags and an export cap, written at most 4 times a day. The optimiser's 192-step plan is compressed into the slots (Part 7.1). This is the pilot's mode and remains the mode for EEPROM-only brands even after Product 2 exists.
- **Mode B**: 192 fifteen-minute setpoints with a heartbeat and a revert timer; for brands with volatile setpoint registers (Sungrow external EMS, Huawei forcible charge, Solis dispatch, SunSpec 704). Built as a contract and tested against the twin in v1; executed only through Product 2.

### 2.4 The 15-minute loop

Every 15 minutes (at :00, :15, :30 and :45 plus 20 seconds, after the minute's ingest), per enabled site, the scheduler (APScheduler inside the API process; the solve on a thread pool of two to four, which is safe because scipy's solver releases the GIL) runs this sequence; a durable run row per site per tick and a per-site lock keep it idempotent, and every stage writes its state so the site's status endpoint can show where it is (idle, acquiring, forecasting, optimising, publishing, failed with a reason):

1. **Acquire**: read the last telemetry minute and the site's state (SoC and its sigma, genset on, off or unknown and elapsed steps, the month-to-date MDI peak and the open block's elapsed mean, month-to-date imports per period for the ICP, the active plan, the capabilities snapshot, open overrides). Staleness is per data source: an edge, HTTPS batch or twin source must be younger than 30 minutes and an OEM-cloud source younger than 20, else the site is marked unobserved and the previous plan stays active to its expiry; a CSV source is planned once per upload (at upload time and at the nightly solve) from its last uploaded minute with the SoC projected forward under the native mode by the twin and the plan flagged `state_projected`, so the Mode A table the installer keys in always exists, and the day's fidelity (Part 7.6) is scored from the next upload; a CSV site with no upload for 24 hours gets no new table and an alert.
2. **Forecast**: fetch the latest issued forecast bundle (PV, load, critical load, outage probability, scenarios, clear-sky ceiling) with its belief time; if older than 30 minutes, issue a new one (the forecasters run in this thread for pilots and in the job pool at scale).
3. **Tariff vectors**: ask the tariff engine for the 192-step import price, export price, availability (scheduled load-shedding windows), import and export caps, the MDI incumbent and interval, the ICP exhaustion step, the diesel price and the terminal value.
4. **Optimise**: build or reuse the site's model structure, set the parameters, solve under the time budget, accept the result by status (Part 4.5).
5. **Publish**: store the plan and its steps with a belief time; compile Mode A slots (and Mode B steps where supported); sign and queue the plan message for the site's channel; mark the previous plan superseded; emit the "why" lines.
6. **Score**: write the fidelity and forecast-skill rows for the step that just elapsed (what the plan said, what the site did).

Event re-solves run the same sequence out of cycle: outage start or end, genset start or stop, a plan rejection or read-back mismatch, a capabilities change, a forecast bust (PV error above 30 percent of the clear-sky ceiling over 30 minutes), a manual override, a tariff version change. The current 15-minute period is held fixed unless the event concerns it (an outage always does).

The plan state machine per site: `idle -> reoptimising_current | optimising_future -> optimised`; `failed(reason)` keeps the previous optimised plan active until its `valid_until`, and the next tick retries. No silent fallback plan is ever written by the optimiser; the fallback belongs to the edge (revert to the inverter's own mode).

### 2.5 What runs where

| Concern | Cloud (this product) | Edge (Product 2, or the twin) | A person (tier 2) |
|---|---|---|---|
| Telemetry normalisation | Validates and stores | Produces 1-minute rows from device reads | Uploads CSV |
| Forecasting, tariffs, optimisation | Yes | No | No |
| Plan delivery | Signs and sends; compiles slots | Applies, reads back, acks, enforces budgets and revert timers | Keys the slot table in |
| Safety | Validates plans against capabilities; mirrors the edge's safety order as constraints | Whitelist, clamp, write budgets, heartbeat, fallback to native settings, override detection | Confirms the table |
| Battery state | Daily 1-minute re-estimate, trust score, wear curve | 1 Hz EKF with a shunt where fitted | - |
| Verification | Replay, A/B, bill rebuild, reports | Telemetry completeness | Provides bills |

The twin (Part 7.4) stands in for the edge in every test and in the demo: it speaks the same contract, runs a 1-minute plant model with brand profiles and fault injection, and replays recorded sites as the counterfactual baseline.

### 2.6 Tenancy and data layout

One API process per data folder (the Solarlytics rule). The data folder holds `platform.db` (accounts, companies, memberships, refresh tokens, founder audit), one workspace file per company (`companies/<id>.db`: sites, tariffs, plans, events, savings, settings, audit) and one telemetry file per company (`companies/<id>-telemetry.db`: 1-minute rows keyed by site and minute, 15-minute aggregates, forecasts). A company is an installer, an EPC or a site owner; it holds many sites; people are members of companies with a role. The request's company comes from the `X-Gridlytics-Company` header among the person's companies, as in Solarlytics.

Ingest from machines (the edge, a partner gateway, the twin in fleet mode) authenticates with an API key scoped to one site (`telemetry:write`, `plans:read`, `sites:read`; `reports:read` later), issued and revoked in the company's settings and audited. The company's switch is the lifted `companies.enabled` (the founder route "access on or off"); there is no second flag.

### 2.7 Deployment

| Piece | Development (Windows) | Hosted |
|---|---|---|
| API | The backend's own venv `gridlytics-backend\.scratch\venv\Scripts\python.exe` (made once from the `ops` interpreter; every `python` in this plan and the handoff means that file, never `python` from PATH, which is the Windows Store stub); `python scripts\start_local.py` in the web repository starts API and web app windowless on 8051 and 5195 (`--scratch`: 8151 and 5295; the release build 8061; 5193 is Retailytics' demo port and 8041, 5192 and 8060 are Solarlytics'), with a lock file per data folder; the launcher defaults to that venv when it exists (else `GRIDLYTICS_PYTHON`), refuses the Store stub and any port that already answers, each with a TROUBLESHOOTING row, and sets `GRIDLYTICS_SCHEDULER=1` for the one server it starts on the owner's folder and on `--scratch` alike (a departure from R7 section 5 on purpose: a scratch run is where the 15-minute loop is watched); tests never set it | Docker image on `python:3.12-slim-bookworm`, one uvicorn worker, behind nginx with certbot on a Linux host (the boxlytics-backend pattern: compose binds 127.0.0.1, health check, volumes for data; an SSE location with buffering off and 64 MB uploads; the mosquitto, valkey and worker services commented in for later); `GRIDLYTICS_SCHEDULER=1` in exactly one container per data folder; CI builds the image and runs the solver benchmark with a 20-second assertion when the owner pushes `v1-build` (the laptop has no Docker; the agent never pushes) |
| Web app | Vite dev server | Vercel (SPA rewrite) or served by the API from `GRIDLYTICS_WEB_DIST` |
| Data | `data/` git-ignored | A volume; daily in-app SQLite copies kept 14 days; a nightly off-machine copy; a quarterly restore drill; migrations copy first |
| Secrets | `.env.founder` and `platform.secret` in the data folder | Environment; every name in `.env.production.example`, enforced by a test |
| Weather and schedules | `GRIDLYTICS_OFFLINE=1` uses cached runs and fixtures | Open-Meteo key; the CCMS scraper and K-Electric PDF watcher as jobs |
| Edge channel | The twin in-process | HTTPS batch endpoints; MQTT broker (Mosquitto, mutual TLS) when Product 2 ships |

### 2.8 Security and audit, in one list

- Sign-in as Solarlytics: PBKDF2 passwords, one-hour JWT with rotating refresh tokens, lockouts, a re-read of the account on every request; the founder console behind password plus TOTP code; every founder action audited in `platform.db`.
- Roles on the server for every route (`auth.need(permission)` first); a viewer never sees the fuel price, the fee or the company's cost settings.
- Plans signed with Ed25519 per site: a `site_keys` table (key id, public key, created, retired), private keys in 0600 files under the data folder's secrets, provisioned to the edge at enrolment, rotated by a signed `rotate_key` command and on handover, every stored plan message carrying its `key_id`; the off-machine backup encrypts the secrets (rclone crypt) and the restore drill proves signing works after a restore; the edge rejects unsigned, replayed or foreign-site plans.
- Every mutating route is audited (who, why, before, after): overrides, tariff edits, settings, applied records, captures, calendar locks, resumes, acks, report generation and exports; `test_audit_walk.py` holds every mutating route to an audit row.
- Scraped schedule text, PDFs and uploaded CSV are untrusted input: parsed in the job pool with size and time caps by permissive parsers (pypdf, BSD; selectolax, MIT; never an AGPL PDF library), never executed, never rendered as HTML (`dangerouslySetInnerHTML` is banned by lint); exported CSV cells starting with `=`, `+`, `-` or `@` are quote-prefixed against formula injection.
- Security headers, host checks, body limits, CORS lists and rate limits come with the lifted guard, plus an ingest rate budget per site key, the SPA's headers in `vercel.json`, and no secrets, tokens or request bodies in logs (the evcc-style slow-request dump is redacted to the solver inputs).

---

## Part 3. The data model

Three kinds of database file, all SQLite in v1 (decision D5), hand-written DDL with `user_version` migrations that copy the file first, WAL, `BEGIN IMMEDIATE` under one lock, `Row` factories, no ORM (the Solarlytics shape). Every table that may ever be hosted elsewhere is reached through a repository function, never by SQL in a route, so PostgreSQL with TimescaleDB can replace the telemetry files at scale without touching callers.

### 3.1 `platform.db` (lifted with the tenancy package)

`companies`, `accounts`, `memberships`, `refresh_tokens`, `founder_audit`, `account_events`, `share_index`, `api_key_index`: as Solarlytics, renamed. The product is switched on per company by the lifted `companies.enabled` (the founder route); the company's workspace file is created at first request. Open mode is not lifted: `auth.open_mode()` returns False and the guard answers 401 to every request without a token or key; `GRIDLYTICS_DB` stays as the tests' default file path (the carried `conftest.py` and `identities.py` use it as each test company's `db_file`) but no route ever serves it. Invitations and password resets are done in the founder console until email exists (PLT-11).

### 3.2 The company workspace file `companies/<id>.db`

| Table | Key columns (beyond id, created, updated) | Notes |
|---|---|---|
| `members`, `role_presets`, `api_keys`, `audit` | As the lifted package needs them | Migration 1 |
| `settings` | A versioned JSON block per area (company, display, fees, alert rules, report branding) with bounds checked on save | Costs and fees hidden from viewer and analyst |
| `sites` | name, timezone, lat, lon, address, disco, outage_unit (kind feeder or transformer, code, name: the key the outage model pools on; feeders now, transformer ids from June 2027), critical_load_profile_id, tier_capable (source-asserted), tier_allowed (admin-set), control_mode (advisory, mode_a, mode_b), demo_link (null, or the demo site this row mirrors), status (draft, observing, shadow, switchback, control, paused), native_settings_ref, genset_start_control (auto_on_grid_loss, soc_threshold, manual), outage_load_mode, notes | A site holds connections, assets, data sources and a state |
| `connections` | site_id, meter reference, tariff_category, regime (net_billing, net_metering_legacy, none, flat, off_grid), agreement dates and expiry, sanctioned_load_kw, mdi_basis and interval, icp_baseline by month and period, tax flags (`gst_recoverable`, `s235_adjustable`, STRN and ATL status, province), `seasonal_multiplier` (Tariff F), the site tariff binding and overrides | Bills, MDI, the ICP baseline and taxes are per meter; a mill with two connections is two rows and the rebuild and the gates run per connection |
| `assets` | site_id, role (pv_array, inverter, battery, genset, grid_meter, load_meter), family, model, firmware, params JSON (PV: kWp, tilt, azimuth, planes; inverter: kW AC, slot grammar; battery: nameplate kWh, usable, chemistry, P_ch, P_dis, eta, soc bounds, replacement cost; genset: rated kW, f_min, fuel kind (hsd, gas_captive, lng_captive) with its unit price and units-per-kWh curve a and b, start cost, run-hour cost, min up and down, tank; meter: points) | One row per device; the twin's brand profile id lives here |
| `site_state` | site_id, as_of, soc_est, soc_sigma, soc_bms, genset_on, genset_elapsed_steps, hours_run_today, fuel_available_l, mdi_peak_month, mdi_interval_partial, icp_month_import_kwh, active_plan_id, capabilities_seq, last_telemetry_ts, last_solve_status | Carried between ticks (Part 4.8) |
| `capabilities` | site_id, seq, received_at, body JSON, hash | Every message kept; the active one is the latest |
| `data_sources` | site_id, kind (edge, https_batch, csv, twin, oem_cloud), api_key_id, column_mapping JSON, quality, last_seen | One site may have several over time |
| `tariffs`, `tariff_versions` | catalogue id, disco, category code, valid_from, valid_to, body JSON (seasons, windows, rates, fixed charges with basis, adders, taxes, regime rules), source (SRO, URL, date), status (shipped, confirmed, superseded) | Never edited in place; a change is a new version with its own valid_from |
| `site_tariffs` | site_id, tariff_version_id, valid_from, icp_baseline_kwh, mdi_floor_kw, export_cap_kw, diesel_price_source | The assignment, versioned too |
| `diesel_prices` | valid_from, price_per_l, source | OGRA notifications or manual |
| `outage_schedule_versions` | feeder_code, fetched_at, source (ccms, ke_pdf, manual), slots JSON, hash | Part 5.3 |
| `plans` | site_id, plan_id, generated_at, belief_time, valid_from, valid_until, horizon_steps, mode, status (optimised, superseded, expired, failed), reason, solver, solve_seconds, objective_rs, cost_breakdown JSON, inputs_ref (the forecast bundle id, tariff version, capabilities seq, overrides), model_version, config_hash, unresolved JSON (soft rows that bound) | One row per solve; the active plan is the latest optimised |
| `plan_steps` | plan_id, step_index, step_start, state (self_consume, hold, charge_from_pv, charge_from_grid, charge_from_gen, discharge_to_load, discharge_to_grid, reserve_hold, genset_on), grid_kw, export_kw, charge_kw, discharge_kw, soc_kwh, genset_kw, reserve_kwh, curtail_kw, unserved_kw, cost_rs, reason_code, reason_params JSON | 192 rows per plan; the chart and the explanation read these |
| `plan_slots` | plan_id, device_id, day_local, slot_index, start_local, end_local, mode, soc_floor, soc_target, max_discharge_w, max_charge_w, grid_charge, gen_charge, export_cap_w, brand_payload JSON | The Mode A table as compiled; `brand_payload` is the vendor-layout rendering |
| `plan_messages`, `acks` | plan_id, message JSON, signature, sent_at; ack body JSON, status, received_at | The contract traffic, kept for disputes |
| `applied` | site_id, plan_id, applied_at, by (edge, person), method (edge, vendor_app, inverter_screen), confirmation (readback, telemetry), fidelity_score | Tier 2 and 3 records |
| `events` | site_id, ts, type, source, detail JSON | The contract's event types plus product events (plan published, override set, tariff changed, data gap) |
| `overrides` | site_id, kind (reserve, genset_hold, charge_block, pause_control), value, from_ts, until_ts, author, reason | Audited; enter the model as bounds |
| `switchback` | site_id, day_local, arm (baseline, optimiser), reset_soc, assigned_by, locked | The A/B calendar |
| `savings_days` | site_id, day_local, arm, baseline_kind, cost_baseline_rs, cost_actual_rs, cost_perfect_rs, delta_rs, import_kwh, export_kwh, peak_kw, diesel_l, genset_hours, starts, throughput_kwh, wear_rs, unserved_kwh, reserve_adequacy, fidelity, tariff_version_id, replay_version | One row per day per baseline kind |
| `bills` | connection_id, period_start, period_end, real_bill JSON (entered from the paper bill), rebuilt JSON, difference_pct, metering factors fitted | WP1's gate and the bill-level proof, per connection |
| `fuel_prices` | kind (hsd, gas_captive, lng_captive), valid_from, price per unit, source | Diesel daily from OGRA; gas and LNG from the site's contract |
| `forecast_runs` | site_id, kind, belief_time, model_stage, model_version, provider, nwp_run_time, flags, bundle_ref | The index of issues; rows live in the company telemetry file |
| `forecast_scores` | site_id, kind, day_local, horizon_class, regime, metric, value, baseline_value | Part 5.5 |
| `models` | site_id or fleet, kind, stage, trained_at, train_window, feature_hash, metrics JSON, artefact_path | The registry |
| `jobs` | The lifted jobs table: owner, company, kind, status, progress, request_hash, result_ref, started, finished | What-ifs, backtests, imports, reports |
| `reports` | site_id or company, kind (money or energy_only), period, generated_at, inputs_hash, pdf_path, csv_path, status (preliminary, final) | Part 8.5; a viewer reads energy-only kinds |
| `site_keys` | site_id, key_id, public_key, created, retired, provisioned_to | The plan-signing keys (Part 2.8); private keys in 0600 files |
| `native_settings` photos | A stored image per capture (JPEG or PNG, 5 MB cap, in the data folder, company-scoped, deleted with the capture) | Part 8.1 |
| `alerts`, `alert_rules` | site_id, rule, severity, opened_at, closed_at, detail | Part 9 |
| `imports` | site_id, source, file_hash, rows, gaps, mapping_id, status, log | CSV uploads |
| `runs` | site_id, tick, started, finished, status (queued, running, optimal, feasible, failed), reason, solve_s, gap, objective_rs, plan_id; unique (site_id, tick) | The durable record of every scheduler tick (Part 2.4) |
| `notifications` | site_id, ts, kind (genset_start, genset_stop, apply_table, alert), payload JSON, status (pending, done, dismissed), outcome, scored_at | Genset advice and apply reminders; the outcome feeds fidelity |
| `native_settings` | site_id, version, captured_at, by, body JSON (the inverter's own TOU table and modes in the brand's grammar), source (oem_cloud, edge, entered) | `sites.native_settings_ref` names (site_id, version); the baseline the replay runs and the fallback the edge restores |
| `critical_load_profiles` | site_id, version, hour_local, kw, source (entered, learned) | `sites.critical_load_profile_id` names (site_id, version) |
| `site_drafts` | draft_id, site_id nullable, person, body JSON, updated | The wizard's autosave; one per person per site |
| `report_schedules` | company, scope, period, next_run, recipients | Part 8.5 |
| `sequences`, `schema_log` | Per-company counters; the migration record | - |

Manual fuel entries (litres bought, price, tank reading) are `events` of type `fuel_log` with the figures in `detail`.

### 3.3 The company telemetry file `companies/<id>-telemetry.db`

One file per company beside the workspace file (R7's measured choice: a wide row costs 10 bytes per point, 52 MB per site-year; the hot reads take 2 to 7 ms; a day's replay 2 ms; a copy runs at about 70 MB/s). `store.telemetry_path()` derives its path from `db_path()` (the stem plus `-telemetry`) under the lifted `use_company` ContextVar; `telemetry._connect()` opens it with its own `user_version` chain; `telemetry/` in the data folder holds only `nwp.db`. The lifted `backups.daily_all` and `back_up` are edited in PLT-03 to copy both files of a company (SQLite's online backup, as lifted), and `backups.restore` probes for a `sites` table.

| Table | Shape | Notes |
|---|---|---|
| `minute` | One wide row per site per minute, `WITHOUT ROWID`, primary key (site_id, ts UTC): source_id, grid_present, grid_present_min, grid_power_w, grid_import_wh, grid_export_wh, grid_voltage_v, grid_frequency_hz, pv_power_w, load_power_w, load_reconstructed_w, battery_power_w, battery_voltage_v, battery_current_a, battery_temp_c, battery_soc_bms_pct, battery_soc_est_pct, battery_soc_sigma_pct, battery_trust_score, battery_soh_pct, inverter_mode_active, export_limit_active_w, genset_running, genset_power_w, genset_fuel_l, censored, quality_flags | `INSERT OR REPLACE` by (site, ts); nulls for missing points; 13 months kept at 1 minute, then exported to Parquet per site-month (pyarrow, read by DuckDB in `bench/` and the analytics jobs) |
| `quarter` | 15-minute aggregates per site (means, minimums for grid_present, sums for energy) written nightly | What the charts beyond 48 hours and the forecasters read; never a year of 1-minute rows in a request |
| `forecasts` | (site_id, kind, belief_time, target_time) primary key, horizon_min, q05..q95 (outage: p_down, p_start, p_end, remaining_min), model_stage, model_version, nwp_run_time, nwp_source, flags | The full 48 hours at each hourly refresh and the first 6 hours at every 15-minute issue: about 7,000 rows a site a day; pruned to one belief time per hour after 90 days |
| `nwp_cells` (shared file `telemetry/nwp.db`, one per data folder) | cell_id, model, run_time, valid_time, variables | Deduplicated across companies and sites; two years kept for training |
| `telemetry_days` (in the workspace file) | site_id, day_local, import_kwh, export_kwh, pv_kwh, load_kwh, genset_kwh, genset_hours, litres, peak_kw, min_soc, outage_minutes, completeness | Daily aggregates for the fleet pages and the reports, written nightly |

Retention: 1-minute rows 13 months in SQLite, then Parquet for as long as the company keeps the site; 15-minute aggregates forever; every belief time of forecasts 90 days, then hourly; events, acks and savings rows forever; `plans` rows forever, but the `plan_steps` and `plan_slots` of superseded plans 90 days and then only for the plans whose belief time is the first of each hour, while the plan that was active at each step and every plan with an `applied` row keep their steps forever (96 solves a day would otherwise leave 6.7 million step rows per site-year in the workspace file). Backups: the daily SQLite copy of every file (14 kept, `VACUUM INTO`), a copy before every migration, a nightly off-machine copy (rclone to an object store the owner names) and a quarterly restore drill; the founder console lists the copies. The nightly copy is what breaks first at scale (about 57 GB of hot telemetry at 1,000 sites): that is the trigger to move the telemetry tables to PostgreSQL 18 with TimescaleDB behind the same repository functions.

### 3.4 Conventions

- Timestamps UTC in storage; site-local on screen and in Mode A slot times; the site's timezone on every site row; no site may ever sit in a daylight-saving zone without a plan-format change.
- Signs as OpenEMS: battery charge negative, grid import positive, PV and load positive. Names as SunSpec.
- Belief time on every forecast and plan row (the FlexMeasures and OpenSTEF discipline), so replay and switchback scoring are plain queries over what was known when.
- Money in rupees as integers of paisa in storage, formatted on screen; every money row names its tariff version.
- A company's file holds nothing another company may read; the founder console reads counts only.

---

## Part 4. The optimiser

The optimiser is OnyxLeaf's own Python module. It follows the structure of evcc-io/optimizer (MIT) and the define-once, update-parameters, warm-start discipline of EMHASS (MIT), and ports the genset, outage and demand-charge mathematics of REopt.jl (Apache-2.0) with the corrections R3 found. No code is forked: R1 costed a fork at 15 to 18 days against 10 to 14 for a clean build and found the fork would drag in Python 3.13, PuLP 4 and CBC. A named list of constructions is ported with notices (Part 14).

One rule governs every coefficient below: **the optimiser sees the bill engine's marginal price.** Every rupee term is the derivative of the bill engine's own output for that site and month, and OPT-05 to OPT-07 ship a hand-worked golden per value stream (reserve, PV shift, MDI, TOU, genset) in which the bill engine prices a one-kWh perturbation and the test asserts the optimiser's coefficient equals it. This is where the product's increment is won or lost.

### 4.1 Inputs per tick

| Group | Symbol | Unit | From |
|---|---|---|---|
| Time | T = 192 steps, h = 0.25 h, t0 the start of the current 15-minute period, site timezone | - | scheduler |
| Load | L[t] (q50), Lhi[t] (q75), Lc[t] critical load (the 75th percentile of observed load during outages in which the genset was off and the SoC stayed above the cut-off plus 5 points, or from the `load_power_backup_w` point where it exists) | kW | forecast bundle |
| PV | PV[t] (q50), PVlo[t] (q25); the clear-sky ceiling PVcs[t] is the forecast-bust reference and the upper bound of the PV quantiles, not an optimiser row | kW | forecast bundle |
| Grid | g[t] in {0,1}: 0 only for an observed outage (for the expected remaining duration d_exp, extended by the forecaster's late-restoration overrun) and for scheduled windows whose posterior adherence exceeds 0.9; every other scheduled window enters only through the outage probability; p_start[t] the probability that an outage starts in step t+1 and d(t) its expected duration in steps, from the forecaster's hazard chain | - | forecast bundle, tariff engine (schedule) |
| Prices | c_imp[t], c_exp[t], on the connection's bearable-cost basis: the period rate or the ICP price plus the adders, duty and the taxes the company cannot recover (GST is input tax for a registered B-2 or B-3 company and section 235 is an adjustable advance, so both are excluded where the connection's flags say so; a no-STRN commercial site bears them); the export credit on the same basis | Rs/kWh | tariff engine (Part 6.4) |
| Caps | G_max import (sanctioned load or the ATS rating), X_max export (0 for zero-export, else the agreed cap) | kW | site |
| MDI | rate_mdi grossed up by the duty and tax factor the bill applies to fixed charges, M0 month-to-date peak, M_floor (the basis floor), n_mdi steps per block (2 for 30 minutes), the open block's elapsed mean G_elapsed over n_elapsed steps, the steps of the horizon inside the current billing month | Rs/kW/month, kW | tariff engine, site state |
| ICP | per period p: the month-to-date imports, the baseline_p and the forecast net imports for the rest of the month beyond the horizon (Q_rest_p); the regime of 4.3 | kWh | tariff engine |
| Battery | E_nom = nameplate times SoH; E0 = soc_est times E_nom; sigma0 as a fraction (SoC points divided by 100); P_ch, P_dis (lowered by a `derate` alert); eta_c, eta_d; soc_min, soc_max; a_grid and a_gen (the grid-charge and gen-charge flags); c_cyc (Rs per kWh discharged); the calendar slope b(T) in Rs per kWh stored per step at the forecast enclosure temperature | kWh, kW, Rs | battery service, site |
| Genset | S rated, f_min, a and b (fuel units per kWh and per hour per kW rated: litres of diesel, or cubic metres or MMBtu of captive gas or LNG), c_fuel (the day's delivered price per unit for the fuel kind), c_start, c_runhr, c_om (including the Punjab 4 paisa per kWh self-generation duty above 500 kVA), UT and DT minimum up and down steps, the carried state (u_prev on, off or unknown; the elapsed on or off steps), the daily run-hour cap and hours run today, fuel available, parallel_allowed, `outage_load_mode` m (0: the ATS carries only the critical circuits; 1: the genset carries the whole plant), `genset_start_control` (auto_on_grid_loss: an AMF or ATS panel starts it on every grid loss, so u[t] is fixed to 1 while g[t] = 0 and the genset is not a decision; soc_threshold: a controller starts it below a SoC, modelled as a row on E; manual: u is free and the decision is a notification) | kW, fuel units, Rs | site, tariff engine, site state |
| Reserve | R[t] reserve energy for an outage starting at t+1 (4.3) | kWh | derived |
| Value of lost load | VoLL | Rs/kWh | site setting (default 500; the owner tunes) |
| Terminal | c_next, v_end, R_next, H_peak (4.4) | Rs/kWh, kWh, h | tariff engine |
| Overrides | a manual reserve, a genset hold, a charge block, a control pause, each with an expiry | - | site state |
| Capabilities | which primitives the device accepts, the Mode A slot grammar, write budgets | - | capabilities snapshot |

Quantile policy for v1: the plan is solved on the median PV and load; the reserve uses the pessimistic pair (Lc, PVlo) widened by the SoC uncertainty; the MDI rows are duplicated under the upper load quantile (4.3). Stochastic scenarios (K = 20 outage paths from the forecaster) are stored but not used by the v1 model; a scenario block is a v2 item (OPT-12).

### 4.2 Variables

Continuous, per step t: G[t] import, X[t] export, C[t] charge, D[t] discharge, E[t] stored energy (E[T] included), P[t] genset output, F[t] fuel litres, K[t] PV curtailment, U[t] unserved critical load, r[t] reserve slack, and D_j[t] wear segments when the piecewise wear curve is on. Global: M the billing-month peak (and M2 when the horizon crosses into the next month), E_use the end-of-horizon energy the next peak can absorb, and Z_p the ICP incremental energy per period when the month-level binary is on.

Binary, per step t: u[t] genset on, v[t] genset start. Global: b_p per period for the ICP middle case (at most two). Optional (default off; R1 and R7 measured no change in the objective at net-billing prices and a quarter to a third less solve time without them): y[t] grid direction (import or export, not both), z[t] charge or discharge exclusivity.

### 4.3 Constraints

Balance, per step. The served load is L when the grid is up; when it is down it depends on who carries the plant: `Lserved[t] = g[t] L[t] + (1 - g[t]) (Lc[t] + m (L[t] - Lc[t]) u[t])`, linear in u because L and Lc are data. With m = 0 the ATS sheds all but the critical circuits, which battery and genset share; with m = 1 the genset carries the whole plant when it runs and the battery alone carries Lc when it does not.

```
PV[t] - K[t] + G[t] + D[t] + P[t] = Lserved[t] - U[t] + C[t] + X[t]
```

Grid: `0 <= G[t] <= g[t] G_max`, `0 <= X[t] <= g[t] X_max`; optional `G[t] <= G_max y[t]`, `X[t] <= X_max (1 - y[t])`.

Battery: `E[t+1] = E[t] + h (eta_c C[t] - D[t] / eta_d)`, `E[0] = E0`, `soc_min E_nom <= E[t] <= soc_max E_nom`, `0 <= C[t] <= P_ch`, `0 <= D[t] <= P_dis`. An E0 outside the band enters through EMHASS's single-recovery rows (R2 section 3.5): the first steps may only move towards the band, never a 422. Charge sources: `C[t] <= (PV[t] - K[t]) + a_grid G[t] + a_gen P[t]`; this single linear row forbids charging from a disallowed source. Optional exclusivity `C[t] <= P_ch z[t]`, `D[t] <= P_dis (1 - z[t])`.

Reserve (soft, expected value; R3 section 3.5): for every step with g[t] = 1 only, `E[t] + r[t] >= soc_min E_nom + R[t]`, `r[t] >= 0`, where R[t] is the energy to carry the critical load through an outage that starts at t+1 and lasts d(t) steps, widened for the SoC estimate's uncertainty:

```
R[t] = min( (soc_max - soc_min) E_nom ,  sum_{k = t+1 .. t + d(t)} max(0, Lc[k] - PVlo[k]) h / eta_d ) + 2 sigma0 E_nom
```

No reserve row exists for steps with g[t] = 0: the balance row governs inside an outage. The slack is priced per step at the probability that an outage starts in the next step times the cost of being short: `c_reserve[t] = p_start[t] c_short`, with c_short the genset's marginal rupees per kWh at a genset site and VoLL otherwise; rows with p_start[t] below 0.01 are dropped. So holding reserve beats discharging into the peak only where `p_start c_short` exceeds the peak price, which is the report's own arithmetic (p times 142 against 35.68, positive above about p = 0.25), and a shortfall is never charged twice against diesel or lost load. A manual reserve override enters as a floor `E[t] >= R_manual` with its own slack at VoLL.

Genset unit commitment (R3 sections 2.3 to 2.6, with the intercept scaled by the rated size, which REopt omits), consistent with the carried state so that no routine state is infeasible:

```
f_min S u[t] <= P[t] <= S u[t]
v[t] >= u[t] - u[t-1],  v[t] <= u[t],  v[t] <= 1 - u[t-1],  u[-1] = u_prev
sum_{k = t .. min(t+UT-1, T-1)} u[k] >= (min(t+UT-1, T-1) - t + 1) v[t]        minimum up time, scaled to the clipped window
sum_{k = t .. min(t+DT-1, T-1)} (1 - u[k]) >= (min(t+DT-1, T-1) - t + 1) (u[t-1] - u[t])   minimum down time, likewise
F[t] = h (a P[t] + b S u[t])                                                   HOMER-style fuel curve, litres
for each local day d in the horizon: sum_{t in d} h u[t] <= cap_hours - hours_run_today [d is today]
sum_t F[t] <= fuel_available
u[t] <= 1 - g[t]   only when parallel_allowed is false
```

At `auto_on_grid_loss` sites u[t] = 1 is fixed for every step with g[t] = 0 (the panel decides, not the plan), the battery's value there is the fuel saved by carrying load the genset would otherwise burn for, and the genset stream is reported "not controllable"; the reserve rule still applies because an AMF panel starts the genset only after the grid falls, so the energy to carry the critical load through the start delay and through a failed start is still held. The carried state: when the genset is on with fewer than UT steps elapsed, u is fixed to 1 only for the remaining steps while `g[t] = 0 or parallel_allowed` (a grid return ends the minimum up time: the ATS transfers regardless); the mirror for a fresh stop. A run-hour cap, a fuel cap or a genset-hold override that conflicts with a fixing wins and the fixing is dropped with the reason `genset_state_overridden`. A genset state of unknown fixes nothing and raises a data-quality flag on the plan. OPT-02's mechanism tests cover an outage end inside the minimum up time, a used-up cap, an empty tank, a hold against a fixing, and a start in the last UT-1 steps, each asserting `optimal` with no relaxed retry.

MDI (R2's demand-charge epigraph with the open interval, R3's previous-peak lower bound), on the site's MDI blocks (default fixed 30-minute blocks aligned to the hour, as the bill engine): `M >= M0`, `M >= M_floor`; for the open block `M >= (G_elapsed n_elapsed + sum_{k in rest of block} G[k]) / n_mdi`; for every later block inside the current billing month `M >= (1 / n_mdi) sum_{k in block} G[k]`. The same rows are written again with `G[k] + (Lhi[k] - L[k])` in place of `G[k]`, so the peak holds under the 75th-percentile load with the planned battery flow. The objective pays `rate_mdi M` with rate_mdi grossed up as the bill grosses up fixed charges; since M >= M0 only a rise above the month-to-date peak costs anything. When the horizon crosses a month boundary a second variable M2 with floor M_floor covers the new month's steps.

ICP (R9 sections 5 and 13; R1's and R7's horizon-local counters are not used): the ICP prices each period's monthly total, not a time-ordered exhaustion. Units above `baseline_p` cost the ICP price (Rs 22.98 plus positive FCA plus the energy-scaling duty and tax share, without QTA and the surcharge), units below cost the period rate, and the marginal price of a horizon step depends on where the month will end. The tariff engine computes per period `Q_p = month-to-date imports + Q_rest_p` where Q_rest_p is the forecast net import (load minus PV minus the previous plan's battery flow) for the rest of the month outside the horizon. If `Q_p - baseline_p` exceeds the horizon's largest possible swing in that period (`G_max x steps in p x h`), every horizon step of period p is priced at the ICP price; if `baseline_p - Q_p` exceeds it, at the period rate; otherwise the model carries one binary b_p for that period and month:

```
Z_p >= Q_rest_p + sum_{t in p} h G[t] - baseline_p,   Z_p <= M_p b_p,   Z_p <= Q_rest_p + sum_{t in p} h G[t] - baseline_p + M_p (1 - b_p),   Z_p >= 0
energy term for period p:  rate_p ( sum_{t in p} h G[t] - Z_p ) + c_icp Z_p
```

Two binaries at most, no chain. TAR-03, TAR-07 and OPT-06 accept on a golden where the peak period's month ends above baseline and the off-peak's does not, and on the uncertain middle case.

Curtailment: `0 <= K[t] <= PV[t]`; a small penalty keeps it at zero unless export is capped and the battery is full. At a connection with regime `none` (no export agreement, zero export: most multi-MW mills exceed the prosumer caps) X_max = 0 and every kWh of PV the plan keeps from curtailment is worth the all-in import price, which is the site's main lever and gets its own golden and ledger line.

Unserved critical load: `0 <= U[t] <= Lserved[t]`, priced at VoLL.

Wear, v1: a throughput cost `c_cyc h D[t]` with c_cyc = replacement cost per usable kWh divided by warranted throughput (Rs 7.5 to 10.8 per kWh at Rs 45,000 to 65,000 per kWh and 6,000 cycles under the reports' convention, Rs 9.4 to 13.5 if the warranty's 80 percent depth is honoured; default Rs 10.8; the battery service supplies the site's number from its data sheet), plus the calendar holding cost `sum_t b(T[t]) E[t]` with b in rupees per kWh stored per 15-minute step from BLAST-Lite's LFP calendar term at the forecast enclosure temperature (R6 section 6.4: about 0.109 at 45 degrees and 0.034 at 25 degrees, interpolated; holding a 100 kWh pack full for 8 hours at 45 degrees costs about Rs 400 against Rs 126 at 25 degrees). No h factor on the calendar term: b is already per step. The calendar term is the only wear term that changes behaviour in summer, so it ships in v1 behind a site flag; there is no separate "hot-and-full" penalty. Wear v2 (OPT-11): the piecewise-linear cost by depth of discharge of Xu et al. 2018 with two or three segments (R6 found the LFP curve nearly flat, Rs 10.3 to 13.2 per kWh across the segments), calibrated from BLAST-Lite's LFP model in `bench/wear/` with a degradation scalar fitted to the warranty (R3 section 5.5, R6 section 6).

### 4.4 Objective (minimise, rupees over the horizon)

```
  sum_t h ( c_imp[t] G[t] - c_exp[t] X[t] )   (with the ICP period term of 4.3 where it applies)       energy bought and sold
+ sum_t ( c_fuel F[t] + c_start v[t] + h c_runhr u[t] + h c_om P[t] )                                    genset
+ sum_t h c_cyc D[t] + sum_t b(T[t]) E[t]                                                                battery wear and calendar holding cost
+ rate_mdi M (+ rate_mdi M2)                                                                            demand charge
+ VoLL sum_t h U[t] + sum_t p_start[t] c_short r[t]                                                     lost load and expected reserve shortfall
+ eps_curtail sum_t K[t] + tie-break slopes                                                              keep the plan plain
- v_end E_use                                                                                           value of stored energy the next peak can use
```

Every soft term is a rupee the product can explain: VoLL is the site's stated value of lost load; the reserve slack is priced at the probability of the outage times the diesel (or lost-load) cost it causes; c_start and c_runhr are the genset's maintenance figures. There is no "penalty base" scaled from the largest price (evcc's scheme is rejected for this reason in R1). Big-Ms are the site's connection size, the inverter rating and the genset rating, never 1e6.

Terminal value: `v_end = max(0, eta_d c_next - c_cyc - c_cal_hold)` where c_next is the highest all-in marginal import price of the day after the horizon from the same ICP-aware vector the horizon uses, or the genset's marginal cost when that day's outage probability exceeds the site threshold, and c_cal_hold the calendar cost of holding the energy until that peak. It is applied only to the energy the next peak can absorb: `E_use <= E[T]`, `E_use <= P_dis H_peak + R_next` (H_peak the peak window's hours, R_next the reserve for the first outage beyond the horizon). By construction v_end sits below the all-in off-peak price, so the plan neither empties nor fills the battery at hour 48 (both prototypes set the terminal value below off-peak for this reason; the report's bare "next-day peak price" would make every plan grid-charge at hours 40 to 48). OPT-07's golden asserts no grid charging in the last 6 hours of a B-2 case with no outage forecast.

### 4.5 Solver, time budget and result policy

- Backend: `scipy.optimize.milp` (HiGHS) through a model builder that emits sparse `A`, bounds and integrality with named rows and a `relax()` toggle per binary family (`optimise/model.py`); a second backend on `highspy` with the same builder (`optimise/backends.py`) for warm starts and time-limited incumbents. Settings: `time_limit` 10 s, hard ceiling 30 s (WP6), `mip_rel_gap` 1e-3, presolve on.
- Measured on the owner's laptop (R7's bench, 6 November 2026): 192 steps, 2,690 variables, 577 binaries, 2,683 rows solve to proven optimality in 2 to 6 s, always at the root node (the time is heuristics and cuts, not branching, so the gap setting changes nothing and a 2-second limit already returns the optimum unproven); R1's fuller prototype with a horizon-local ICP chain and 768 binaries in 8 to 10 s; without genset and ICP 0.3 s; relaxing the direction and lock binaries saves a quarter to a third. Budget: one solve per site per 15 minutes on one vCPU; about 90 sites fit one core at 10 s (R7: 180 at 5 s at full utilisation); at 1,000 sites the solves move to workers (Part 10.6).
- Result policy (evcc's semantics): `optimal`, `feasible` (time-limited incumbent that passes the row and integrality re-check), or `not_solved`. On `not_solved`: one relaxed retry (minimum up and down times dropped, exclusivity binaries off, a 24-hour horizon) and a logged reason; if that fails the state machine records `failed(reason)` and the previous plan stays active. Never republish after an exception. Hard rows exist only where the physics allows no choice (balance, caps, the battery band, the genset's own limits) and 4.3 makes them consistent with every carried state; reserve, lost load, MDI rises and curtailment are priced rows. The API refuses with a 422 only inputs that are impossible: `f_min > 1`, `E_nom <= 0`, `P_ch or P_dis <= 0`, `UT or DT > T`.
- Every result is re-checked in plain numpy: balance rows within 1e-6 kW, binaries integral, no import during outages, genset never below f_min when on, the reserve rows' slacks reported; a plan failing the re-check is `not_solved`.
- Structural cache: the model structure (rows, columns, integrality) is built once per site configuration hash and reused with new parameter values; the configuration hash covers every input in 4.1 that changes the shape (horizon, genset present, flags, wear mode, the ICP case per period).

### 4.6 The battery state service

Inputs per minute: pack voltage, current, temperature, BMS SoC, BMS SoH, charge and discharge limits, cell voltage spread where published, and the edge's estimate with its sigma when an edge exists. Outputs for the optimiser and the UI: `soc_est` and `soc_sigma`, usable kWh, a trust score in [0, 1], the wear coefficient c_cyc and the calendar slope, the P_dis derating, and alerts.

Method v1 (R6 section 5): coulomb counting between voltage anchors (the full-charge plateau exit and the low-voltage knee) with the BMS value as a trust-weighted prior. The LFP plateau makes pack voltage useless for SoC between the knees (about 0.5 mV per SoC point with 5 to 25 mV of hysteresis, so a 0.1 V reading on a 120-cell pack is worth 2 to 5 points at best); sigma therefore grows with hours and throughput since the last anchor (about 0.19 points per hour of current-offset error plus 2 percent of throughput on inverter-derived current: 4.6 points after a day, about 15 after three cloudy days; a shunt cuts that to about 2.5). When sigma exceeds 15 points the optimiser is asked for an anchor slot (a full charge). Usable kWh = E_nom times max(0, soc_est minus 2 sigma minus soc_floor) times eta_d (R6 section 5.5). The trust score over 30 days is 0.4 times anchor agreement plus 0.3 times coulomb-count agreement plus 0.2 times self-consistency plus 0.1 times anchor coverage; the edge contract's acceptance mark ("within 5 points at anchors") is the anchor term. The seven alerts of R6 section 5.7, as written there: trust_low (under 0.5), soc_disagreement (BMS and estimate more than 10 points apart), hot_high_soc (hours above 90 percent SoC above 35 degrees), sigma_high (above 15 points), imbalance (cell spread or temperature limit from the BMS), capacity_drop (3 points in a month), derate (the BMS discharge limit under 50 percent of nominal for 30 minutes, which lowers P_dis for the next solve). The optimiser widens the reserve by 2 sigma.

Method v2 (OPT-14, later): a 1-RC-plus-hysteresis equivalent-circuit EKF at 1-minute resolution for sites with a shunt, on filterpy (MIT), after a commissioning open-circuit-voltage test procedure exists for the packs sold in Pakistan. Wear: the throughput price and the calendar slope of 4.3, calibrated monthly per site against measured SoH with BLAST-Lite's LFP model in `bench/wear/` (BSD-3; only its calendar term is trusted for Pakistani heat, because its cycle term was fitted at 0.8 to 1.0 depth and 10 to 45 degrees and has an inverse temperature dependence by its authors' own admission); PySAM, PyBaMM and PyBatterySE stay in `bench/` too, never imported by the API.

### 4.7 Explaining the plan

Every plan carries, per slot, the dominant reason computed from the solution and the duals or the cost breakdown, in a fixed vocabulary the UI renders: `hold_for_outage(p, duration)`, `charge_from_pv(cheap export)`, `charge_from_grid(off-peak before cloudy day)`, `discharge_peak_window`, `mdi_cap(kW)`, `genset_on(min load, diesel Rs/kWh)`, `genset_off(battery cheaper)`, `icp_flat_price`, `terminal_value`, `export_capped`, `wear_margin_too_small`, `forecast_guard`, `operator_hold`. The rules live in `optimise/explain.py` (evcc's `explain.py` is the template; the rules are ours) and are unit-tested on the golden cases so a reason never contradicts the numbers; because every coefficient equals the bill engine's marginal price (the rule at the top of this part), the reasons are consistent with the bill by construction.

### 4.8 Receding-horizon loop and state

State carried between ticks (Part 3's `site_state` row): E0 and sigma0, the genset state (on, off or unknown) and its elapsed steps, hours run today, fuel available, the month-to-date MDI peak and the open block's elapsed mean and count, month-to-date import per period for the ICP, the active plan id, open overrides (a manual reserve, a genset hold, a charge block, a control pause, each with an expiry), the last capabilities sequence. Overrides enter the model as bounds (a manual reserve raises the floor with its own slack; a genset hold fixes u = 0 and wins over any fixing; a charge block fixes C = 0) and are shown on the plan with their author and expiry.

### 4.9 Tests the optimiser must ship with

- Golden JSON cases per site archetype: grid-tied TOU B-2 with PV and battery; outage-heavy feeder with a genset (both outage load modes); off-grid genset site; MDI-bound site; the three ICP cases (a period whose month ends above baseline, one below, the uncertain middle); a flat-tariff A-3 hospital; a legacy net-metering site. Goldens are solved at `mip_rel_gap` 1e-6 and compared within 1e-5 relative on the objective, and within 1 kWh on the schedule where the feature is not a tie-break.
- The marginal-price goldens of the rule at the top of this part: one per value stream, asserting each optimiser coefficient equals the bill engine's one-kWh perturbation price.
- Invariants on every case and on 1,000 replayed twin days: no import during outages; genset never below minimum load when on and never starting more than the cap; no simultaneous import and export when the flag is on; SoC within bounds; MDI never exceeded without a rise in M; and, as the reserve rule's acceptance (OPT-04): of the outages the forecaster gave p_start at or above 0.2 at least 2 hours ahead, 90 percent are entered with E at or above R, unserved critical energy on those days is zero, and on days with no forecast outage the battery still discharges into the peak window.
- Mechanism tests that fake solver results (time limit, infeasible, fractional binaries, a row-breaking schedule) and prove the result policy, plus the carried-state cases of 4.3 (an outage end inside the minimum up time, a used-up cap, an empty tank, a hold against a fixing, a start near the horizon end), each `optimal` without a relaxed retry.
- Cross-checks: the no-genset case against a REopt-style annual run at fixed sizes within 1 percent (`verification/`); the genset case against brute-force enumeration of a one-day problem at hourly steps; python-microgrid (LGPL, unmodified, `verification/` only) as a second opinion on a shared case.
- Performance: the 192-step genset case under 30 s on the laptop as the local gate and under 10 s on the CI runner when the owner pushes, recorded in `GATES.md`.

---

## Part 5. The forecasters

Three forecasters, each a provider behind one interface (`forecast/providers.py`, the Akkudoktor EOS shape: `provider_id`, `enabled`, `update(force)`, a dependency order, per-provider settings chosen by id per site, and an import provider for every quantity so a CSV or a partner feed can stand in). Every forecast row carries `belief_time` (issue time), `target_time`, `horizon_min`, seven quantiles (q05, q10, q25, q50, q75, q90, q95), `model_stage`, `model_version`, `nwp_run_time`, `nwp_source` and flags. Stages advance per site on data-sufficiency gates, never on calendar time. All series are UTC inside, Asia/Karachi on screen (no daylight saving). R5 is the full specification; this part fixes what the build implements.

### 5.1 PV forecaster, per site

| Stage | Gate | Method | Quantiles |
|---|---|---|---|
| 0, day 0 | Site record only | pvlib chain: Open-Meteo hourly NWP per 0.1-degree cell (shortwave, direct, diffuse, DNI, temperature, humidity, wind, cloud cover by layer, precipitation; air quality AOD, dust, PM2.5 four times a day) downscaled to 15 minutes through the clear-sky index (not by interpolating power), Perez-Driesse transposition per plane, cell temperature, PVWatts DC and AC with clipping, a soiling state (default 3 percent after rain, +0.5 percent per dry day capped at 20 percent in October to February, +0.3 percent otherwise, until learned), PVGIS horizon mask; priors from PVGIS (ERA5 where SARAH3 ends) and NASA POWER | A regional error table by horizon and clear-sky-index bin, seeded from published arid and humid nRMSE and an offline study on the ESMAP stations, marked "prior" |
| 1, from day 3 | About 3 daylight days of 1-minute telemetry | Capacity and bias fit on clear-sky periods (robust regression of measured on physical power) refit nightly on 14 days, exposed as the performance ratio with a cleaning alert on an 8 percent drop without rain; conformal residual bands by horizon class and sky class; an intraday ratio blend for 0 to 2 hours (the smart-persistence component) | Conformal from the last 14 to 28 days |
| 2, from week 4 to 6 | 1,500+ daytime 15-minute samples and 10+ non-clear days | xgboost multi-quantile model (`reg:quantileerror`, the quantile list in one booster) on clear-sky-normalised power with the nine Quartz NWP variables, AOD, dust, PM2.5, humidity, calendar and solar geometry, days since rain and cleaning, the stage-1 ratio, lead time and model id, the latest observed index for short horizons; trained on the Historical Forecast and Previous Runs archives so training features carry the same lead time as inference; monotone in the NWP index | Model quantiles, sorted |
| 3, from month 3 | 20+ sites in a region | One fleet model with site features plus a per-site residual correction; new sites start on it | Model quantiles |

Outputs per issue: 192 slots of AC kW at seven quantiles plus the clear-sky ceiling (the forecast-bust reference and the upper bound of the quantiles; not an optimiser row). Every gate in this part is measured on real data (owner task O12's month, the ESMAP stations for the PV chain, PRECON for load persistence, then the pilot's phase 0); on twin data the pipelines are only checked to run end to end and write every score, because the twin's PV, load and outages come from the same model families the forecasters start from.

### 5.2 Load forecaster, per site

| Stage | Gate | Method |
|---|---|---|
| 0, day 0 | Onboarding form | Template weekly 15-minute profile by site type (office, retail, one to three-shift industrial, cold store, tower, hospital, school), operating hours, off days, contracted or MDI kW, HVAC share, Ramadan behaviour; spread plus or minus 25 percent |
| 1, from day 3 | 3 days of data | Equivalent-day persistence: the median of the same slot on the last 3 then 7 same-day-type days (working, half-day, off day, public holiday from the `holidays` library's Pakistan calendar, Ramadan working day, Eid), scaled by the last two hours' ratio with a decaying horizon; empirical spread, conformal after week 2. This is also the baseline the model must beat |
| 2, from week 4 | 3 weeks at under 10 percent gaps and one off-day cycle | One direct multi-horizon xgboost quantile model with horizon as a feature and belief-time-safe lags (the OpenSTEF `available_at` discipline), calendar, weather, planned shutdowns and shift calendar, and the peak-window flag; clipped at 1.3 times MDI |

Site load is reconstructed from inverter telemetry (PV plus discharge plus import plus genset minus charge minus export), never from grid import alone. **Censoring**: a sample is marked censored when the grid is absent and backup is limited (genset off and SoC at the cut-off plus 5 points), when the inverter reports overload or derating, or when load fell more than 40 percent within two minutes of grid loss and stayed down; censored samples leave the training set and the persistence medians. The critical-load series the optimiser's reserve uses is the 75th percentile of observed load during adequately backed outages, per hour of day; more than 30 percent censoring on a feeder falls back to the template for those hours with a data-quality warning.

### 5.3 Outage probability forecaster, per feeder

Inputs: the feeder id captured at onboarding from the bill (the 6-digit PITC code for ex-WAPDA DISCOs; the K-Electric feeder name), the DISCO, the schedule (scraped from the PITC CCMS feeder page hourly and from K-Electric's PDF schedules, or entered by hand; parsed as untrusted text; versioned), every site's 1-minute grid-present flags on the feeder (all sites on a feeder are sensors), calendar, the weather forecast (heat index, thunderstorm flag), recent history and schedule adherence, and an optional national shortfall level entered by an operator.

Model at 15-minute slots: `P(down) = 1 - (1 - p_sched)(1 - p_unsched)`. The scheduled part is the schedule indicator times a Beta-Binomial adherence rate per feeder (prior Beta(8, 2), separate rates for starting and ending on time). The unscheduled part is a logistic regression on hour of day, season, day type, heat index, precipitation, recent history, DISCO and feeder loss category, with DISCO-level coefficients as the prior and a shrunk feeder intercept; replaced by a small gradient-boosted classifier after 60 unscheduled events. A duration model (empirical survival per feeder, lognormal until 30 events) extends P(down) across slots and, during an outage, gives the probability of still being down. Outputs: P(down) per slot, expected remaining duration, 20 sampled outage paths for a later stochastic model. An observed grid loss re-issues the forecast at once and triggers the optimiser's event re-solve. Cold start without a schedule: the DISCO's hourly climatology from the fleet, or a hand-entered prior (for example 10 hours a day on QESCO, PESCO and SEPCO feeders; 2 hours on urban LESCO and IESCO) flagged "prior" on screen.

### 5.4 Jobs, cadence and compute

| Job | Cadence | Scope |
|---|---|---|
| nwp_fetch | Hourly at :05 (deterministic runs), four times a day (ensemble, air quality) | Per NWP grid cell, deduplicated across sites; cached by (cell, model, run time); staleness flag after 3 hours |
| schedule_fetch | Hourly | Per feeder; a diff writes a new schedule version |
| forecast_issue | Inside the tick (Part 2.4 step 2), per site, before its solve: the first 6 hours at every quarter; the full 48 hours when a new NWP run has arrived (the hourly `nwp_fetch` sets a flag) and on events. There is no separate quarter-hour forecast job | All sites |
| stage1_refit | Nightly 01:30 PKT | Per site: capacity ratio, soiling state, conformal tables, persistence baselines |
| site_model_train | Nightly 02:00 PKT (incremental), weekly full | Per site in stage 2; CPU, under a minute per site; artefacts as xgboost JSON with a version |
| fleet_model_train | Weekly | All sites; promoted only if the 14-day backtest improves |
| scoring | Daily 04:00 PKT | Scores yesterday's forecasts at every belief time and horizon |
| backfill | At onboarding | Two years of Historical Forecast features and the PVGIS and NASA priors |

At 1,000 sites: issuing is well under a CPU-minute per tick; nightly training about 11 CPU-hours on two to four workers; no GPU.

### 5.5 Metrics, pre-registered

| Forecast | Metric | Baseline | Gate (WP5) |
|---|---|---|---|
| PV | Daytime nRMSE (elevation above 5 degrees) by AC nameplate; MAE as a percent of capacity excluding night; skill scored per horizon class | Smart persistence per class: the last-hour clear-sky index at 0 to 2 hours (reported only, since the stage-1 blend is that model), the same hours yesterday at 2 to 6 and 6 to 24 hours | Skill 25 percent or better over 4 weeks of measured data at the 6 to 24 hour class (the class the nightly Mode A table uses); 24 to 48 hours reported; nRMSE at or under 12 percent at 24 hours in the dry season by stage 2 |
| Load | nMAE, nRMSE by mean load | Equivalent-day persistence | Beats it at every horizon class |
| Both | Pinball loss, CRPS from the quantile set, q10 to q90 coverage 80 percent plus or minus 5, q05 to q95 coverage 90 percent, PIT histograms by regime | - | Coverage within tolerance by stage 2 |
| Outage | Brier score and skill; reliability; start recall and precision within one slot at P over 0.5; duration MAE | Schedule-only and climatology | Beats schedule-only |
| Economic | Forecast regret in rupees per day: the forecast-driven plan's cost against the perfect-foresight plan's cost from the replay | - | Reported; no gate |

### 5.6 Data providers and terms

| Provider | Use | Terms (6 November 2026) |
|---|---|---|
| Open-Meteo | Primary NWP, ensemble, air quality, Historical Forecast and Previous Runs archives | Free tier is non-commercial (under 10,000 calls a day); Professional plan needed for a paid SaaS and for the archives (about USD 99 a month in the 2023 announcement; the 2026 page prints no price: the owner confirms); data CC BY 4.0 with attribution on screen; global models only over Pakistan (ICON about 11 km, GFS, IFS 0.25 degree); 15-minute data is interpolated outside Europe and North America |
| PVGIS 5.3 | Day-0 priors, horizon, TMY | Free with the acknowledgment "PVGIS (c) European Communities"; SARAH3 likely ends at 65 degrees east, so Lahore and Islamabad fall to ERA5 (to confirm) |
| NASA POWER | Climatology | No restrictions, citation requested; 1-degree, 5 to 7 day latency |
| forecast.solar Public | Second PV source for pilots | Free hourly, 12 calls an hour per IP address; keys may not be shared with clients; not for 1,000 sites |
| Solcast | Benchmark and premium option | Quote only; the hobbyist tier is non-commercial |
| PITC CCMS, K-Electric | Load-shedding schedules | Public under a Pakistan Information Commission order; no API; scraped as untrusted text; ask for an official feed in parallel |
| ESMAP Pakistan stations (2015 to 2017), PRECON Lahore load | Offline studies | CC BY 4.0; CC0 |

Fallback chain when a provider fails, each step flagged on the forecast row and widening the quantiles by 25 percent up to double: the cached run (6 hours), another model on the same provider, the secondary source (forecast.solar for PV; a self-hosted GFS puller at scale), provider-free models kept trained (clear-sky times a persisted index; a lag-only load model; the hazard model with climatology temperature), and, when the site itself is offline for 24 hours, plan expiry and the inverter's own mode. A health probe every 5 minutes, a circuit breaker after three failures, and a dashboard of the share of forecasts issued on each step.

### 5.7 Libraries

Runtime: pvlib 0.16.1 (BSD-3), the installed xgboost 3.2 (Apache-2.0) and scikit-learn 1.8 (BSD-3), `holidays` (MIT), an HTTP client with caching. Optional: skforecast 0.26 (BSD-3), LightGBM 4.7 (MIT). Reference and benchmark only, never runtime: OpenSTEF 4.7 (MPL-2.0, its `available_at` discipline is copied as an idea), Quartz 1.2 (MIT, UK-trained, pins xgboost 2.0.3; runs in its own venv as a benchmark), darts (torch-heavy).

---

## Part 6. The tariff and regime engine and the bill engine

R9 verified the reports' tariff table against NEPRA's decision of 11 February 2026 (notified as SRO 279(I)/2026 on 12 February 2026) page by page, the Prosumer Regulations 2026, the ICP notification, the QTA notification and the Terms and Conditions of Tariff. Every peak and off-peak rate the reports quote is right; the corrections and additions below change how the engine is built. R9 sections 11 to 13 hold the JSON schema sketch, the versioning rules and the bill-rebuild algorithm; this part fixes what the build implements.

### 6.1 What the primary sources settle

| Topic | Verified fact | Consequence for the engine |
|---|---|---|
| The schedule | One national GoP-applicable schedule for the ten ex-WAPDA DISCOs and K-Electric for calendar 2026, in three versions a pilot's twelve bills will span: the FY2025-26 schedule, SRO 41-52(I)/2026 from 1 January 2026 (B-2 36.68/27.41, a 50 percent MDI floor) and SRO 279(I)/2026 from 12 February 2026 (proration of February open); the CY2027 rebasing was heard 13 to 15 October 2026 and a new schedule is expected from 1 January 2027, possibly with a different fixed-charge design | All three versions ship, plus an empty CY2027 version marked "awaiting notification"; the 2 percent gate is measured on bills from March 2026 onwards and re-run on the first 2027 bill; an owner task (O13) enters and approves every new notification within five working days |
| Rates | A-1(b) TOU 46.85/34.53; A-2(c) 43.82/35.15; A-2(b) 39.76; A-3 42.48; B-1(b) 35.74/25.48; B-2(b) 35.68/22.83; B-3 35.68/23.67; B-4 35.68/23.38; B-5 35.68/23.13; C-1(c) 46.31/37.54; D-2(b) 29.54/28.69 (Rs/kWh, peak/off-peak) | Shipped as data with the SRO as source, status primary |
| Fixed charges | Rs 1,250 per kW per month for A-2(b), A-2(c), B-2 to B-5 and C, billed on the higher of 25 percent of sanctioned load or the month's MDI; Rs 675 per kW on the higher of 50 percent or MDI for A-1 TOU; Rs 1,000 per consumer per month for A-3 and B-1(b) (the reports' per-kW reading was wrong); Rs 400 per kW on sanctioned load for D-2; no minimum charge where a fixed charge applies | The MDI term matters only where the basis is MDI; the floor is data per category |
| MDI | The highest 30-minute demand in the month, in kW (15 minutes for arc furnaces); fixed clock blocks assumed, all hours, to confirm on a bill; power factor below 90 percent adds 2 percent to fixed charges per point | The optimiser's `n_mdi` is 2 steps; interval, block mode and scope are site settings until a bill confirms them |
| TOU windows | Fixed by NEPRA's Terms and Conditions, not by monthly notices: December to February 17:00 to 21:00, March to May 18:00 to 22:00, June to August 19:00 to 23:00, September to November 18:00 to 22:00, with a daylight-saving hook; K-Electric April to October 18:30 to 22:30, November to March 18:00 to 22:00 | Two TOU calendars as data (ex-WAPDA, K-Electric); peak membership is a pure function of calendar, month and clock minute; tests at every season boundary and at the midnight-crossing 19:00 to 23:00 window |
| QTA and FCA | Flat rupees per kWh on every unit of the billing month, no peak split; QTA Rs 0.5194 for September to November 2026; FCA monthly, billed two months later; exemptions: lifeline, prepaid, ICP units (QTA, negative FCA, the debt-service surcharge); EV stations and others for FCA | A monthly adders table keyed by billing month with an exemption set and a sign per adder |
| The ICP | Rs 22.98 per kWh on incremental peak and incremental off-peak units above the consumer's own consumption in the same month of December 2023 to November 2024, escalated 2.8 percent a year, for three years from the December 2025 billing month; positive FCA applies on incremental units, QTA and the surcharge do not; new consumers' baselines from MDI or sanctioned load times hours times a load factor; net-metering sites capped at net imports; a review hearing on 5 October 2026 has no published outcome | A first-class price regime: the month's kWh split per period into baseline and incremental by monthly totals before pricing (order inside the month is irrelevant); the optimiser's marginal price per period depends on where the month will end (Part 4.3): the ICP price when the period's month ends above baseline, so the TOU spread collapses; the rate and validity are versioned fields |
| Surcharges and taxes | The debt-service surcharge (printed as FC or PHL surcharge) about Rs 3.23 per kWh; a possible Neelum-Jhelum Rs 0.10 line; provincial electricity duty (Sindh: industrial 1.5 percent, commercial 2 percent; Punjab, KP and Balochistan rates unverified); Punjab's 4 paisa per unit on grid and self-generated units above 500 kVA; GST 18 percent on the bill value; further tax 3 percent without an STRN; extra tax 5 to 17 percent off the active taxpayer list; advance income tax under section 235 by slab; PTV fee; Karachi's MUCT Rs 750; late payment 10 percent | A configurable stack with per-site overrides captured from real bills; the base of each line configurable; the ordering of R9 section 7 as the default |
| Net billing | Imports at the applicable tariff; exports credited at the national average energy purchase price, one flat rate regardless of hour, Rs 8.13 per kWh in NEPRA's accounting for April to June 2026 (the press's Rs 10 to 11 is unsupported), revisable by notification each quarter; excess credit rolls to the next cycle or is paid quarterly; 5-year agreements; capacity capped at sanctioned load; 80 percent transformer rule; no fee and DISCO approval at 25 kW and below | Export price flat with a quarterly version, plus an optional TOU export schedule (a Power Division proposal of Rs 18 to 22 for evening exports exists but is not notified) |
| Legacy net metering | Agreements signed before 9 February 2026 keep the 2015 mechanism until expiry (SRO 547(I)/2026, primary); NEPRA's accounting used Rs 25.32 (primary). That DISCOs net by period and value the surplus at the off-peak rate is secondary and unverified until a legacy bill confirms it | A regime with by-period netting as an option, carry-forward balances, agreement dates and a modification flag; its netting rule marked unverified in the provenance view |
| No export agreement | The prosumer caps (1 MW, the sanctioned load, 80 percent of the transformer) leave most multi-MW mills as non-prosumers on zero export | Regime `none`: X_max = 0, avoided curtailment valued at the all-in import price |
| Tariff F | Seasonal industrial consumers (rice shellers among them) pay 125 percent of the industrial fixed charge for the running season, at least six months | A `seasonal_multiplier` and season months on the connection; a golden |
| Taxes the company recovers | A sales-tax-registered B-2 or B-3 company recovers GST as input tax (the 3 percent further tax exists only for no-STRN consumers) and adjusts the section 235 advance against its income tax; a no-STRN commercial site bears both | Connection flags `gst_recoverable` and `s235_adjustable`; prices, the ledger, the fee and the gates run on the bearable cost, with "taxes you recover" as a separate line, so the figure matches the CFO's ex-GST number; diesel and wear on the same basis |
| Captive fuel | The ICP's intervenors cite the captive gas levy: Punjab mills run gas engines near the grid's peak cost | The fuel kind per genset (diesel, captive gas, LNG) with its own price table and units-per-kWh curve; the fuel line in the fuel's unit |
| Diesel | OGRA now prices daily (since July 2026): Rs 400.67 on 30 October, Rs 382.25 on 6 November 2026 | A daily price table with effective dates; delivered price adds transport and shrinkage from site data |
| Fuel curve | HOMER's convention F = F0 times rated kW plus F1 times output; the documented example F0 0.033 L/h per rated kW, F1 0.273 L/kWh (0.405 L/kWh at 25 percent load, 0.306 at full) | Per genset from its data sheet (25, 50, 75, 100 percent points) fitted to a line; the example as the placeholder |
| Bill periods | 30 days or fewer from the last meter reading; fixed charges prorated when the interval moves by more than 4 days | The rebuild uses the real reading dates, never the calendar month |

### 6.2 The objects (R9 section 11)

Separate, effective-dated, immutable objects joined at bill time, each carrying a `Source` (kind, reference, URL, retrieval date, verified: primary, secondary, unverified) and a `Validity` (effective from and to, and for adders the billing months they apply to): the **schedule** (rate cards per category with fixed charges, their basis and floor, minimum charges, the PF rule), the **TOU calendars** (ex-WAPDA and K-Electric, seasons, windows, the daylight-saving hook), the **monthly adders** (QTA, FCA, surcharges, each with sign and exemption set), the **tax rules** (lines, rates, bases, who pays, by province and category), the **ICP rule** (rate, reference months, escalation, load factors, eligibility rules, validity), the **prosumer regimes** (net billing with the export price schedule; legacy netting rules), the **fuel prices** (daily), and the **site binding** (category, DISCO, regime, agreement dates, sanctioned load, MDI settings, ICP baseline by month and period, the site's overrides with the bill as source).

Versioning rules: never edit in place; a change is a new object with `supersedes`; resolution by date, adders by billing month; three time axes kept apart (consumption minute, billing month, `known_from`), so a replay can run "as known at the time" (what the optimiser could know) and "as finally billed" (what the bill shows); provenance on every number shown in the UI; site overrides versioned too; a content hash on every version, stored with every bill result. Watchers on nepra.org.pk (tariff notifications and the legal page), ke.com.pk and OGRA propose new versions; a person approves each before it goes live.

### 6.3 The bill engine (R9 section 13), at 1-minute resolution

For a site and a billing period, with the 1-minute import and export series (and the replayed baseline series for the counterfactual), the resolved objects and the binding:

1. Tag each minute with its period from the calendar (half-open windows; the calendar month of the minute, not the billing month).
2. Energy by period, kWh, at 1-minute precision, rounded only at the end.
3. MDI from 30-minute fixed blocks aligned to the hour (configurable); billing demand = max(floor share times sanctioned load, MDI) for per-kW categories.
4. The ICP split per period: baseline for the month and period times the escalation; incremental = max(0, imports minus baseline); for net-metering sites cap at net imports.
5. Energy charges: baseline units at the period rate, incremental units at the ICP rate; legacy sites net first.
6. Fixed charges by basis, prorated for reading intervals; the PF penalty.
7. Adders by billing month with exemptions (incremental units exempt from QTA, the surcharge and negative FCA).
8. Electricity duty on the variable subtotal (base configurable; the Punjab per-unit duty where it applies).
9. Export credit: flat NAEPP or the TOU export schedule; legacy surplus at the configured rate with carry-forward; the bill never goes negative.
10. GST on the configured base; income tax by slab; further and extra tax where applicable; flat items.
11. Outputs: a line-item bill plus the three aggregates verification needs (cost of imported energy, cost of demand, credits), with the version hashes and the series hash.
12. The counterfactual runs the same steps on the replayed series; the saving is the difference line by line, so the report shows the MDI saving, the period-shift saving, the ICP-crossing effect, the export change and the fuel line separately.
13. Reconciliation: when the real bill arrives, every line's residual is logged; a persistent residual becomes a site override with the bill as its source, never a silent re-tune.

Metering: site telemetry is a CT or a site meter at 1-minute means, not the DISCO's register, so a metering factor per connection (one energy scalar and one MDI scalar, not one per period, so that it stays identifiable) is fitted on at least two bills and tested on a held-out third; a factor outside 0.97 to 1.03 is a data finding (a load the inverter does not see, a second meter), not a calibration; it is applied to both arms before pricing. The 2 percent gate stays open until a held-out bill passes. A persistent residual on a tariff line, not a metering one, becomes a connection override.

Acceptance: before the owner's bills (O3), four hand-worked bills in the test docstrings, every line in R9 section 7's order from the SRO rates, matched to the paisa: a B-2(b) ICP month whose reading period spans the November-to-December window change, an A-2(c) net-billing month with an export credit and GST, a legacy net-metering month, and a flat A-3 month with the per-consumer fixed charge (a fixture produced by the engine is not a golden); then each pilot site's last three bills rebuilt within 2 percent on the calibrated rebuild; unit tests at every season boundary and the midnight-crossing window for both calendars.

### 6.4 What the optimiser gets from the engine (`tariffs/vectors.py`)

For the 192 steps from the current period, all-in rupees so that the optimiser prices what the bill will show: the import price per step (the period rate plus the adders that scale energy and the duty and tax share that scales with energy, including the section 235 slab rate where the site sits in the slab; and the ICP case per period of Part 4.3: the ICP price for every step of a period whose month will end above baseline, the period rate for one that will end below, the month-level binary for the uncertain middle, with the baseline, the month-to-date imports and the forecast rest-of-month net imports); the export credit per step (`c_exp = NAEPP x (1 + ED)(1 + GST)` where the site's bill nets the credit before taxes, else NAEPP; or the TOU export schedule); grid availability for observed outages and high-adherence windows, and the adherence of the rest; import and export caps; the MDI incumbent, blocks, floor, the open block's elapsed mean and the steps inside the current billing month, with the demand rate grossed up as the bill grosses up fixed charges; the day's delivered diesel price and the Punjab self-generation duty where it applies; and the terminal inputs (the highest all-in marginal price of the day after the horizon from the same vector, the next outage's reserve, the peak window's hours).

### 6.5 Onboarding checklist for the owner and the operator

The site wizard's tariff tab asks for a current bill and fills R9 section 15's ranked list: the electricity-duty line and rate for the province; the surcharge lines as printed; the GST base and the prosumer credit's position relative to taxes; the MDI practice and floor; the printed export rate and settlement; legacy netting and the agreement end date; ICP enrolment and the DISCO's baseline; the printed peak window; the reading dates; which FCA months appear on which bill; the taxes charged; for K-Electric the MUCT and Sindh duty; the delivered diesel price and any self-generation duty; the genset's data-sheet fuel points. Every answer becomes a versioned site override with the bill as its source.

---

## Part 7. Plan delivery, the edge contract, the twin and the ingest paths

Product 2 does not exist yet. Its own plan puts bench writes to the first brands about six months after it starts and field-proven writes about ten (U3 section 6.6). This product therefore runs its first year on CSV, HTTPS batch and OEM-cloud telemetry, delivers Mode A tables that installers apply, and tests its executor against a digital twin that speaks the contract the real edge will speak. Everything below is designed so that the day Product 2 arrives, the only change is the transport.

### 7.1 Mode A: the slot compiler

The optimiser emits a brand-agnostic `TouSlotPlan v1` (R6 section 3.6): contiguous slots covering 24 hours, each with `mode` (discharge, hold, charge_pv_only, charge_grid, charge_gen, export), `soc_floor_pct`, `soc_target_pct`, `max_discharge_w`, `max_charge_w`, `grid_charge`, `gen_charge`, `export_allowed`, `export_cap_w`; a `reserve` block (critical kWh for the next outage, the SoC to hold and until when); the `mdi_cap_kw`; a `fallback` block (revert after 3 hours to self-consumption with a minimum SoC); a `write_budget` (3 table writes and 2 event writes a day); and an Ed25519 signature.

The compiler (`delivery/mode_a.py`) turns the 192-step solution into at most N slots for the device's grammar, read from its capabilities (`slot_model`: max_slots, ring, fields, time resolution, min_slot_minutes, weekday mask, table writes per day):

1. Merge adjacent steps with the same mode, flags and target into candidate slots; merge runs shorter than `min_slot_minutes` (default 30) into the cheaper neighbour.
2. Rank the remaining boundaries by their value against the native replay (the twin replays the table with and without each boundary and the bill engine prices both, so the value is the increment the boundary keeps, not a share of the whole bill) and drop the least valuable until the count fits: Deye 6, Growatt 3 plus 3 (or 9 on TL-XH), Huawei 14, Sungrow 2 emulated windows, Voltronic emulated mode changes.
3. Quantise boundaries to the device's resolution but keep them on 15-minute marks so the verification replay lines up.
4. Translate targets to the brand's semantics: Deye per-slot capacity and charge-source flags; Huawei one global grid-charge cut-off (the maximum of the grid-charge targets) with floors through the discharge and backup SoC registers; Growatt global charge-stop and discharge-floor; Sungrow max and min SoC plus the forced-charge target. The MDI cap: Huawei's capacity-control periods carry `mdi_cap_kw` directly; on Deye, Growatt and Sungrow, which have no peak-shaving primitive, the compiler sets each peak-risk slot's `max_discharge_w` to at least `max(0, Lhi - mdi_cap_kw)` over the slot and its `soc_floor_pct` so that the energy `sum max(0, Lhi - cap) h / eta_d` is available at the slot start; where the brand cannot hold both, the floor wins and the expected MDI rise is reported on the plan as `mdi_not_enforceable`, so the savings page never credits a peak cut the device could not make.
5. Clamp every SoC to [BMS low cap + 2, 100] and every power to the limits read at boot; never a floor below the fallback's minimum SoC.
6. Make the table safe to repeat: on Deye and Growatt the table persists and runs again every day with no new plan, and the ring has no "native" slot, so the only safe tail is a whole table that is safe on its own (R6 invariant I1): for every boundary from the last planned re-solve to `valid_until`, the twin-replayed SoC stays at or above the reserve for the next known or likely window, no slot's floor is below `fallback.min_soc_floor_pct`, and the last slot's floor equals the larger of the reserve and the fallback floor. Two day-tables are compiled (today's remainder and tomorrow); tomorrow's is the fallback day and must pass the same check. The Mode A fallback block carries `on_expiry: inverter_native`, `min_soc_floor_pct`, `max_export_w_floor` and `native_settings_ref` (U3 section 3.4) and no revert timer; the 1,800-second revert belongs to Mode B's volatile setpoints.
7. Enforce the write budget: the nightly table write after the 23:00 solve; an event rewrite only when the expected rupee gain exceeds a threshold (default Rs 500), the budget allows and the previous write is more than 60 minutes old. The cloud plans for 3 table writes a day against the edge's cap of 4; on Deye and Growatt every TOU register is EEPROM-class, so an event rewrite costs a table write, and only Sungrow (and possibly Huawei, persistence unverified) has volatile override primitives. A forum-grade 100,000 flash cycles gives 55 years at 5 writes a day and 2.7 years at the 100-a-day hard cap, which is why the cap is a backstop and 3 to 4 is the operating point.
8. Replay every plan through the twin before signing it (the wrong-plan guard, R6 rule C2): the plan is refused and the previous one stays when the replayed SoC crosses the absolute floor, export exceeds the export floor, the plan's own grid draw exceeds `mdi_cap_kw`, a genset slot falls in a period where the grid is expected present, or the whole-table safety of step 6 fails; and any plan whose day-over-day change is implausible is held for a human check in tier 2.

Per-brand grammars and register facts are data files (`delivery/brands/*.json`), written clean-room from the permissive sources R6 and U3 name (kellerza/sunsynk and wills106 under Apache-2.0, ha-solarman, mkaiser and mpp-solar under MIT) with attribution; nothing from the AGPL Huawei library. The compiler's tests cover each grammar (the Deye ring order, Huawei's no-overnight-span and 14-period cap, Growatt's pair writes, Sungrow and Voltronic emulation) and the budget.

### 7.2 Mode B: the setpoint schedule

192 step objects of the abstract primitives (`battery_mode`, `battery_power_w`, `soc_floor_pct`, `soc_target_pct`, `grid_charge_allowed`, `gen_charge_allowed`, `export_limit_w`, `pv_curtail_pct`, `genset_request`, `release`), a `refresh` block (heartbeat 60 s, revert after 1,800 s, residual band 300 W) and the same fallback block. A missing field means "no change". Mode B is emitted only for devices whose capabilities say `mode_b.supported` (Sungrow external EMS, Huawei forcible charge under third-party control, Solis dispatch, SunSpec 704); a Mode B plan may carry the Mode A table as the fallback for slot-only devices. In v1 Mode B is validated against the twin only.

### 7.3 The edge contract, ems-edge/1.0

U3 section 5 is the contract. The messages are pydantic strict models in `delivery/contract.py`, which is the one validator for MQTT and HTTPS; the JSON Schema files under `contracts/ems-edge/1.0/` are generated from those models by `scripts/export_contract.py` (pydantic emits 2020-12) and a test fails when a file drifts from its model, so no separate schema library is needed. Summary:

| Message | Direction | Content |
|---|---|---|
| telemetry | edge to cloud | Columnar 1-minute batches (up to 1,440 rows when catching up), a declared column subset of the telemetry point table of U3 section 1.2, quality flags, acknowledged by sequence |
| capabilities | edge to cloud, on boot, on template change, daily | Edge software and clock state, control tier (edge-asserted), per device: family, template, firmware, points, primitives with min, max, step, persistence class and daily write limit, `slot_model`, `mode_b`, the native-settings capture reference |
| plan | cloud to edge, signed | plan id, supersedes, issued and valid times, timezone, the capabilities sequence it was built for, mode, targets, fallback, `mode_a.days[]` or `mode_b.steps[]`, notes (solver, objective, outage risk, reason) |
| events | edge to cloud | outage start and end, genset start and stop, plan received, rejected, applied, expired, fallback engaged and cleared, read-back mismatch, write failed, budget exhausted, local override, template or firmware change, control disabled or enabled, reboot, link lost, clock unsynced, battery trust low, physics check failed, data gap, installer session |
| ack | edge to cloud | Per device and per primitive: written value, read-back, status, reason codes (SIG_INVALID, CAPS_STALE, EXPIRED, SCHEMA, OFF_WHITELIST, NOT_SUPPORTED, RANGE_CLAMPED, STEP_ROUNDED, BUDGET_EXHAUSTED, UNLOCK_FAILED, DEVICE_OFFLINE, READBACK_MISMATCH, ATOMIC_GROUP_FAILED, CONTROL_DISABLED, OVERRIDE_ACTIVE, RATE_LIMITED, SLOT_GRAMMAR, TIMEZONE_MISMATCH) |
| command | cloud to edge, signed | release, hold (bounded emergency), request_capabilities, request_debug_window, request_native_capture |
| heartbeat | edge to cloud, every 60 s | uptime, active plan, validity, control state, queue depth, clock offset; the liveness source for the 5-minute link-loss mark |

Rules the cloud lives by (R6 section 4 writes them out in full, cloud side and edge side, with the event matrix): validate every plan against the stored capabilities before sending (a rejected plan is a bug); replay it through the twin before signing; re-issue every 15 minutes; a plan's `valid_until` at most 48 hours after issue; treat READBACK_MISMATCH, OVERRIDE_LOCAL and a template change as "unknown state": re-plan from telemetry and pause control on that site; two read-back mismatches in 24 hours demote the site to advisory until a person resumes it; a local override needs an "adopt as native" or "resume" decision from a person; inverter clock drift (R6 rule C10) raises a warning at 120 seconds, at 300 seconds the compiler starts every peak-avoidance or discharge slot 15 minutes early and ends it 15 minutes late and the installer is asked to set the clock, and at 900 seconds the site is demoted to advisory until the offset is under 120; store every capabilities message and every ack (savings disputes); stagger fleet pushes; spread the site keys and rotate on handover. Additions R6 proposes for the contract's next revision, adopted here: `native_capture_id` in capabilities with a `NATIVE_REF_STALE` reason code, an `inverter_clock_offset_s` telemetry point, a `DEVICE_BUSY` reason code, and the safe-tail formula as a signing check. The safety order the edge enforces (battery protection, the reserve ladder with hysteresis that only caps discharge and fails open with an alarm, genset interlocks, the cloud plan step, default self-consumption) is mirrored in the MILP as hard rows and priced rows.

Contract test suite (`backend/tests/contract/`), shared by the twin now and the real edge later: schema validation of every message; signature round trip, replay and foreign-site rejection; the plan lifecycle (receive, ack, apply at the right minute, read-back, expiry fallback, supersede); Mode A grammar per brand; budgets (the fifth table write is refused and the previous table remains); link loss (10 minutes silent, cached plan continues, store-and-forward delivers in order); overrides and template changes disable control; 30 simulated days of telemetry at 99.5 percent completeness with every minute present or flagged.

### 7.4 The digital twin (`twin/`)

A simulated edge that speaks the contract, used in every test, in the demo company and as the counterfactual engine:

1. Contract endpoint: an in-process adapter (and later an MQTT client) that receives plans, checks signature and schema, acks with read-backs, emits telemetry and events.
2. Brand profiles as data (Deye, Huawei, Sungrow, Growatt, Voltronic, Solis): slot grammar, primitives, persistence class and write budget, refresh and revert timers, write latency, unlock requirement, atomic groups, known quirks.
3. Plant model at 1-minute steps: PV from the clear-sky profile with cloud noise or a measured profile; load from a template or a measured series; a battery with usable capacity, power limits, round-trip efficiency, temperature derating and a BMS SoC error model (bias plus drift, reset at full charge); the inverter's native mode logic (self-use with configurable slots and SoC targets) so the baseline is realistic; a grid with scheduled and random outages from a hazard by hour; a genset with auto-start on outage below a SoC threshold, a fuel curve and a minimum run time.
4. Fault injection mapped to event codes: link loss, plan expiry, reboot, read-back mismatch, budget exhaustion, local override, template change, clock drift, sentinel values, partial minutes.
5. Replay mode: recorded 1-minute site data instead of the synthetic plant, so the twin reproduces what the inverter's own mode did (WP4's acceptance: daily grid import within 3 percent and battery throughput within 5 percent of measured on 90 percent of days).
6. Fleet mode: N twins with staggered clocks to test the scheduler.

The demo company ships with three twin sites (a B-2 textile plant with a genset on a load-shedding feeder, an A-2(c) commercial site under net billing, an off-grid farm with a genset) and 90 days of generated history, so every page is live on day one.

### 7.5 Ingest paths

| Path | Accepts | Cadence | Notes |
|---|---|---|---|
| HTTPS batch (`POST /api/v1/edge/telemetry` and the other message types) | The contract JSON with a site API key | Any | The same validator as MQTT; idempotent by (site, sequence); partners' gateways (NeoTAQ, Enercon, GFE), a thin logger, Enerlytics push |
| CSV upload | One row per minute per site with the documented columns (`ts, grid_present, grid_power_w, grid_import_wh, grid_export_wh, pv_power_w, load_power_w, battery_power_w, battery_voltage_v, battery_current_a, battery_temp_c, battery_soc_bms_pct, genset_running, genset_power_w, genset_fuel_l, inverter_mode_active, quality_flags`), UTC or a declared timezone | Daily or one-off | A mapping screen (column to key, unit, sign) saved per source; blanks are missing, not zero; grid_present derived from voltage when absent (180 V threshold, to confirm); a source without `inverter_mode_active` is accepted for tier 1 only, because the counterfactual needs it, and the mapping screen says so; a missing genset signal is unknown, not off; duplicate timestamps keep the last; gaps over 5 minutes become data-gap events; the file is untrusted input (size limit, no formulas rendered); planned once per upload (Part 2.4) |
| Twin | The contract | 1 minute | Demo and tests |
| OEM-cloud pull (stretch, ING-06) | Solarman (Deye, Solis, Sofar loggers), Huawei FusionSolar, Sungrow iSolarCloud, Growatt | About 5 minutes, unverified per OEM | Read-only, installer-granted keys only, never stored passwords, data quality tagged oem_cloud; brittle by nature |
| MQTT over mutual TLS | The contract | 1 minute | When Product 2 exists; Mosquitto with per-edge certificates and topic ACLs per site |

Every path produces the same normalised rows (SunSpec names, OpenEMS signs: battery charge negative, grid import positive, PV and load positive) with the mandatory grid-present flag; the importer records the source and its quality on every row.

### 7.6 Mode A delivery before Product 2 (tier 2)

In order of preference: (1) the installer or owner keys the table into the vendor app or the inverter screen from the product's per-brand slot view (shown in the vendor's own layout: Deye's six programs, Huawei's fourteen periods, Growatt's windows), and the product confirms it from telemetry alone, since there is no read-back: discharge rises above 10 percent of rated within 30 minutes of a peak-slot start and falls below 5 percent at its end, floors are respected within 3 points, no forbidden grid charging occurs; each day is classified confirmed, partial or not applied, and the day's fidelity is 0.4 times boundary hits plus 0.3 times floor respect plus 0.3 times energy fidelity (R6 section 4.6); the "applied" row and the score feed the savings page, where the reported saving is multiplied by the energy-fidelity term so an unapplied table never claims a saving; (2) OEM-cloud settings APIs, worth a one-week spike per OEM and not to be relied on; (3) an interim per-brand writer on a Raspberry Pi for one or two brands, with Product 2's safety rules hard-coded for that template, daily writes only, read-back and a manual release, retired when Product 2 lands (decision D8; an owner call). The product never builds register maps, transport drivers, an identification engine or an installer onboarding tool: those are Product 2.

---

## Part 8. Savings verification and reporting

The measurement methodology is also the pricing engine: the report's rule is never to price above one third of verified, counterfactual-accounted savings, and the kill criteria of Part 15 are written against the same numbers. Nothing in this part is reusable from open source; it is built from scratch on the twin's replay mode and the bill engine. The design goal is a number a CFO cannot pick apart: immutable baselines, a tuned comparator at every site, a metering factor from real bills, a block switchback with priced carry-over, and fidelity as a gate rather than a multiplier.

### 8.1 The baselines

Two baselines at every site, and every saving is reported against both:

1. **The native capture**: the inverter's own settings as the installer set them, captured at onboarding by read-back or by a photograph of the vendor app, stored immutably (`native_settings`); any later installer change becomes a new capture with its date, never an edit. This is what the replay runs and what the edge restores on fallback.
2. **The tuned baseline**: the best static table of the same brand grammar that the product's own engine fits on the observation month (the 6-slot or 14-period table that minimises the replayed bill), and at genset sites additionally HOMER-style cycle charging with a set-point SoC (the genset runs near rated output and charges to the set point). Claims must beat the tuned baseline, not only the default; owners already hand-tune their inverters.

### 8.2 Three layers of proof, computed every day

1. **Digital counterfactual** (`verify.py`, the twin in replay mode): the actual arm is the measured day priced by the bill engine; the baseline arms are the native capture and the tuned table replayed by the twin on the measured PV, load and outages and priced the same way, at 1-minute resolution with the same tariff version, TOU windows, MDI blocks, adders, taxes, net-billing export credit or legacy netting, the dated diesel price and the same wear charge; the report gives the rupee delta, the diesel litres, the peak kW and the throughput per arm. The replay must first prove itself: on twin days it reproduces the twin's recorded import and throughput exactly (a regression test); on recorded real days (owner task O12 and the pilot's phase 0) daily grid import within 3 percent and battery throughput within 5 percent of measured on 90 percent of days (WP4).
2. **Switchback**: blocks of 3 to 7 days assigned by a coin flip per fortnight (tier 2: 7-day blocks, one table change per switch, which an installer can do; tier 3: 3-day blocks within the write budget); no SoC reset (a reset is itself a control action the native mode never performs); the comparison is adjusted after the fact by a regression on PV kWh, load kWh and outage hours, shown adjusted and unadjusted. Carry-over is priced, not removed: at every block boundary the bill engine credits the arm that ends with more stored energy by `(E_end_A - E_end_B) x v_end` with the terminal value of Part 4.4, and the report shows the credit beside the saving. The switchback measures the levers a day can show (energy by window, export, fuel, reserve); MDI is the month's single highest block and the ICP is by monthly totals, so both arms sit inside every month and those two levers come from the full-month counterfactual only, labelled as such; at MDI or ICP sites the pilot's phase 3 runs month-long blocks so two real bills show the MDI line. At genset sites the operator's genset actions are logged per arm and the genset stream is reported with its fidelity rather than claimed as blinded. The counterfactual runs on every day of both arms, so block length costs no comparison data. At least 8 weeks.
3. **Bill-level truth**: the DISCO bill per connection and settlement period rebuilt by the bill engine from the 1-minute series and compared with the real bill (WP1's 2 percent gate), so the CFO's number and the product's number agree. The metering factor of Part 6.3 (one energy scalar and one MDI scalar per connection, fitted on two bills, tested on a held-out third) is applied to both arms before pricing. Every rupee in the ledger, the fee check and the gates is on the connection's bearable-cost basis (ex recoverable GST and adjustable section 235 where the flags say so), with "taxes you recover" as its own line, so the saving is the CFO's ex-GST number.

The two estimates (counterfactual and switchback) must agree within 15 percent of the switchback saving or Rs 300 a day, whichever is larger; a disagreement is a finding, not a rounding. A perfect-foresight plan (a hindsight re-solve on the realised PV, load and outages, EMHASS's `perform_perfect_forecast_optim` idea) is reported separately as the upper bound, labelled "not achievable, for reference", so forecast error is visible in rupees.

### 8.3 KPIs, as the report pre-registers them

| KPI | Definition |
|---|---|
| Rupee saving | Reconstructed bill plus fuel plus wear, the measured day against each baseline; Rs per day and percent of energy cost; realised-forecast and perfect-foresight columns |
| Diesel | Litres (metered where a sensor exists, else the fitted curve at the dated pump price), run hours, starts per day, average load ratio |
| Peak | Monthly MDI in kW; the fixed-charge difference |
| Peak-window import | kWh imported in the evening peak window |
| Forecast quality | PV daytime nRMSE and skill by horizon class; load nRMSE; outage Brier score |
| Reserve adequacy | Share of forecastable outages entered with SoC at or above the reserve target; unserved critical load kWh |
| Constraint violations | SoC below reserve at outage start; genset below minimum load; export above cap; import during an outage |
| Battery use | Throughput kWh, equivalent full cycles, hours above 90 percent SoC above 35 degrees, wear rupees, throughput per saved rupee |
| Control health | Plan-execution fidelity (the day's class: confirmed, partial, not applied; the score of Part 7.6), writes per register per day, read-back failures, fallback events |
| BMS trust | Gap between BMS SoC and the independent estimate; trust score |
| Data quality | Telemetry completeness, censored share, NWP staleness, schedule availability |

### 8.4 Fair accounting rules

- Rebuild the bill at 1-minute resolution for both arms with the same tariff version, window calendar, MDI blocks, adders and metering factor; price on the bearable-cost basis and show the recoverable taxes separately.
- Charge battery wear to both arms at the same Rs per kWh.
- Price diesel at measured litres where possible, else the fitted fuel curve at the dated pump price.
- Report realised-forecast results and the perfect-foresight bound separately.
- Fidelity gates, it does not multiply: a day classified "not applied" claims nothing; a "partial" day is reported with its fidelity beside it; no multiplier is applied to a saving measured on actuals (the measured arm already shows what a partly applied table earned).
- Never average at 15 or 60 minutes before pricing: the literature shows averaging inflates an optimiser's edge by about 69 percent.
- Keep every tariff version, forecast belief time, plan, ack and capture, so any day can be re-scored later with the inputs of that day; a persistent residual against a real bill becomes a site override with the bill as its source, never a silent re-tune.

### 8.5 Reports

- A monthly report per site and a roll-up per company, rendered by the backend (decision D13) from the same rows the API serves: the headline saving against each baseline with the method stated, the ledger by lever, diesel, peak, outage coverage, forecast skill, control health, the switchback calendar with the carry-over credits and the bill comparison; a one-page summary and an annex of daily rows; "Preliminary" until 8 weeks of switchback have run and the FCA of every month in the period has been billed (the bill truth of a month is final only then); never the words "guaranteed" or "optimal".
- The data behind every figure downloadable as CSV with the tariff version, the capture version and the plan ids, so a dispute can be re-run.
- A pilot write-up template (WP10) generated from the same report with the pre-registered success and kill criteria of Part 15.3 filled in; the first monthly report of each pilot site is reviewed by that site's energy manager in the shadow phase (the report's WP9 acceptance) and their comments recorded.

---

## Part 9. The web app: pages, tabs, states

R8 studied thirteen products and wrote the full inventory with eight fields per page (purpose, components, data, charts, empty and cold-start states, roles, tiers, live updates) in `R8-ux-pages.md` sections 2.1 to 2.21; the executing agent builds from that note. This part fixes the decisions and the shape. The admin and founder consoles are not here (they come with the lift and grow later).

### 9.1 What the study settled

- A plan is a **table of slots first** (Predbat's columns: time, window, prices, PV, load, battery intent, grid, genset, SoC, reserve, cost, cumulative) with a Gantt beside it; operators act on tables and owners glance at charts.
- A **per-slot reason** is what every planner's users ask for and only one product gives; it is a first-class field of every plan step in the API from the first solver version (Part 4.7).
- Uncertainty is shown as **named cases** (median and pessimistic paths), not error bars; the plan says which case it weighs.
- Savings are a **ledger by lever** (baseline, saving by peak avoidance, outage reserve and genset avoided, cheap charging, export value, MDI; the perfect-foresight bound separately and labelled "not achievable, for reference").
- Every site shows its **tier and control state as header chips**, a **last-data stamp**, and the offline rule in words; every install starts in monitor.
- The **viewer role is a server-side field filter**: no fuel price, fee, wear cost, rate as money or rupee KPI ever reaches the browser.
- **Lettered tabs** only for the three sequential editors (site onboarding, tariff editor, pilot set-up); plain tabs or sub-routes everywhere else.
- **Demo mode is the twin**: three seeded sites with 90 days of history regenerated nightly and run through the real optimiser, so no page is empty on day one and the demo is the role-permission test bed.

### 9.2 Shared components

SiteHeader (name, DISCO and category, tier chip, control-state chip such as Following plan, Native mode, Hold until 21:00, Released, Edge offline, the last-data stamp); KpiTile (value, unit, delta, Preliminary flag); SlotTable (the 15-minute table, reused by Plan, Pilot and Devices and control); EnergyFlow (the radial figure with PV, Battery, Grid, Genset and Load nodes, live kW, grey maximum bars, the Grid node hatched in an outage); TimeSeriesChart (series toggles, 1-minute to daily, the "now" line, outage shading, TOU-window shading; ECharts for dense data); ReasonChip; EmptyState (one sentence, one action, the demo offer); RoleGate and TierGate; LetteredTabs.

### 9.3 The pages

| Page | Route | Purpose | Key components | Empty and cold-start | Polling |
|---|---|---|---|---|---|
| UI-01 Fleet | `/fleet` | Which sites need attention, what each does now, the month's saving | Summary strip; site table (tier, control state, last data, a live mini flow, next event, open alerts, MTD saving, 7-day sparkline); filters; map toggle later | No sites: "Add your first site" and "Load demo sites"; one site: land on its overview; new site: "Collecting data: day 1 of 14" | 60 s; last-data amber after 5 min, red after 30 |
| UI-02 Site onboarding | `/sites/new` | Create a site with everything the optimiser needs | Lettered tabs A site and connection (address and pin, DISCO, feeder, data source with a connection test that refuses a source without a grid-present signal), B PV, battery and genset (brand drives the slot grammar; wear cost derived), C tariff and billing (library pick or inline; the day's price vector drawn), D control mode and limits (tier, mode, reserve policy, cycle cap, write budget, fallback; the native-settings capture), Review and activate | The wizard is the cold start; defaults marked Preliminary | None |
| UI-03 Tariffs | `/tariffs` | The company's tariff library with versions | Library table; version timeline; editor with lettered tabs A category and windows (heatmap preview), B ICP and fixed charges, C net-billing settlement and regime expiry, D diesel; Review with the price vector and the diff; "Simulate on site X"; the pending-version approvals from the watchers | Seeded templates marked "Template, verify against your bill"; an expired regime shows red and the export value falls to zero until a new version | On focus |
| UI-04 Site overview | `/sites/:id` | What the site does now, next, and how the month goes | SiteHeader; EnergyFlow; "Now and next" card (the current step in words, the next three reasons); KPI row (today and MTD); the 24-hour actual-versus-plan chart with SoC and the reserve line; alerts strip; a 12-hour plan strip; tariff and weather footer | Day 1 banner; stage 0 wording; no plan: a checklist to the missing wizard tab; offline: the radial greys, the plan band stays | Snapshot 30 s, chart 60 s, plan 5 min |
| UI-05 Telemetry | `/sites/:id/telemetry` | Any series at 1 minute over any range | Range and series pickers; TimeSeriesChart with plan and outage overlays; a weekday-by-hour heatmap; a data-quality strip; CSV export (owner, analyst) | "First data received at 09:14"; CSV-only banner with the last upload range | 60 s when the range includes now |
| UI-06 Devices and data | `/sites/:id/devices` | Where data comes from and what devices can do | Sources with status and test; devices table with capabilities drawer; points table (required or optional, mapped or missing); CSV upload with mapping and a validation report; the native-settings capture card; the edge card | A three-option source chooser; "No edge device: plans are delivered as daily slot tables" | 60 s |
| UI-07 Outages | `/sites/:id/outages` | The feeder's history, schedule, probability and reserve adequacy | KPI row; the outage heatmap; the log with editable cause; the announced-schedule editor (paste from SMS helper); the 48-hour probability chart with the reserve overlaid; the model card (Brier, sample size, feeder pooling) | Stage 0 prior badge; a blocking notice when the site has no grid-present signal; hidden for off-grid sites | 15 min |
| UI-08 Forecasts | `/sites/:id/forecasts` | What the optimiser believes and how good it has been | PV, Load and Outage panels with fan charts and actuals; accuracy cards (7 and 30 days); weather strip; stage card with retrain; the bust log | Clear-sky curve with a wide band and "Stage 0, Preliminary"; "Needs 7 days" | 15 min |
| UI-09 Plan | `/sites/:id/plan` | The product's centre | Plan header (status "Solved within tolerance", objective breakdown, stage, Preliminary ribbon); views Table (SlotTable with the reason column and override glyphs, past 24 hours greyed with actuals), Gantt (lanes, SoC with the reserve line and the pessimistic path, price step line, outage band), Slots (the Mode A compression); the reasons legend; the overrides panel; "Re-solve now" (rate-limited); the compare drawer against the native-mode replay; versions and diffs | Checklist card until the first solve; "Forecast guard: wider reserve while in stage 0"; monitor tier reads "Recommendation"; a failed solve shows the previous plan in amber with the reason | Plan 5 min, now-marker and actuals 60 s |
| UI-10 Battery | `/sites/:id/battery` | State, health and economics | KPI row (SoC with BMS versus estimate and trust, usable kWh, SoH, cycles, throughput, hot-and-full hours, wear rupees); the SoC chart with reserve and plan target; daily cycles against the cap; the pack card; the trust card; the limits card; the economics card (owner, analyst) | "Waiting for the first anchor"; the pack card hidden for OEM-cloud sources | 30 s state, 60 s charts |
| UI-11 Genset | `/sites/:id/genset` | Hours, litres, rupees and whether advice was followed | KPI row; the run log with the plan-requested flag; the fuel-curve card with refit; the fuel log form; the advice panel (pending requests with reason, Done or Not now); the policy card ("the product never starts or stops the genset itself") | Hidden when the site has no genset; "Estimated from fuel curve" badges; a missing signal is unknown, not off | Pending requests 60 s |
| UI-12 Savings | `/sites/:id/savings` | The proof page | Period picker; the ledger by lever with the perfect-foresight bound; method tabs Counterfactual, Switchback, Bill comparison (line by line, the 2 percent check); the KPI grid of Part 8.3; the fee check card (owner only, the one place the fee appears); exports | Blocked until the native-settings capture exists; "First saving figure tomorrow after 06:00"; Preliminary until 28 verified days | After the nightly job |
| UI-13 Pilot set-up | `/sites/:id/pilot` | Pre-register the pilot | Lettered tabs A baseline window, B native-mode capture, C switchback calendar (paint weeks; rule checks; `.ics` export), D success criteria (the gates prefilled, sign-off); Review and start, which locks the tabs; later edits are amendments | Tab B blocks until the capture exists; the genset gate hidden without a genset | None |
| UI-14 Reports | `/reports` | What leaves the product | Report list; generate dialog (monthly savings, pilot report, daily operations sheet, energy-only viewer-safe report); a browser preview from the same components; schedules | "Your first monthly report on the 3rd"; partial-month preview marked Preliminary | 5 s while generating |
| UI-15 Devices and control | `/sites/:id/control` | The control plane: the slot table in the vendor's layout, read-back, fallback, release and hold | SiteHeader with the control-state chip large; the capabilities card with budgets used today; the Mode A table in the vendor's layout per brand with the intent in words beside each value, copy and print, "Mark as applied" with time, person and photo; the read-back panel with ack codes and the day's fidelity; the write log; the fallback card with "Test fallback" (owner, closed loop); Hold, Release to native, Resume, Pause writes | Monitor: "What the plan would set"; no capabilities: the brand's generic grammar as a preview; no edge: read-back inferred from behaviour | Control state 15 s, read-back and log 60 s |
| UI-16 Alerts | `/alerts` | One list of what needs a human | Inbox with severity, category, state, assignment; detail drawer with what the product did automatically; rule editor with channels, quiet hours and escalation; categories Data, Control, Reserve, Battery, Genset, Forecast, Tariff, Savings, Product | "Nothing needs you"; Control and Savings alerts suppressed until a plan and 7 days exist | Counts 30 s, inbox 60 s |
| UI-17 Site settings | `/sites/:id/settings` | The post-onboarding editor of the same data | Plain tabs General, Assets, Tariff, Control tier (the tier change with its confirmation flow and the installer sign-off record; owner only), Notifications, Members | - | On focus |
| UI-18 Company settings and account | `/settings`, `/account` | Members and roles, the demo-sites switch, report schedules, API keys for the company's own tools; profile, password, sessions, notification preferences | The lifted members and keys pages restyled | - | On focus |

Sign-in and the "No sites yet" first-run page come with the lift (Part 11.3); until PLT-11 adds email, an admin issues a one-time password with a forced change on first sign-in and can clear a lockout, the founder console does the same across companies, and the "Forgot password" text says to ask the company's admin.

### 9.4 Roles and tiers on screen

| Role | Sees | Does |
|---|---|---|
| Admin (the company's owner is one) | Everything, including money settings, the fee card, the tier change, release and hold | Creates sites, publishes tariffs, starts pilots, edits limits and economics, manages members |
| Operator | Costs but not money settings or the fee | Acknowledges alerts, marks slots applied, logs fuel and genset events, places holds and overrides within limits, uploads CSVs, paints the switchback calendar |
| Analyst | Everything including money settings and raw exports | Runs counterfactuals and reports, drafts tariffs for approval, retrains forecasts; edits nothing that affects control |
| Viewer | Dashboards and energy figures only; "Hidden for your role" placeholders where a money tile would be | Reads |

Tier changes wording and controls, not pages: monitor reads "Recommended" everywhere and offers no apply control; advisory makes the Slots view primary with "Mark as applied" and an "unconfirmed apply" nudge; closed loop shows ack codes, read-back, budgets, the fallback test, release and resume. The tier is changed only in site settings by the owner, and never above what the data source supports.

### 9.5 Navigation

Sidebar groups: Fleet (Fleet, Alerts with a count badge, Reports); Site (the site switcher as the group header; Overview, Plan, Telemetry, Forecasts, Outages, Battery, Genset when present, Savings, Devices and data, Devices and control, Site settings); Company (Tariffs, Pilot set-up, Settings, Members); footer (account, the DEMO ribbon toggle, help). Routes carry the site id (`/sites/:id/plan`), `?tab=` for wizards and `?view=` for plain tabs so links are shareable; the site switcher keeps the current page; breadcrumbs under the header; the top bar holds search, the alerts bell, the user's role chip and the site's tier chip. On a phone the sidebar collapses to Overview, Plan, Control, Alerts and Genset; the installer's whole path is one phone screen: Plan, then the Slots view in the vendor's own layout, "Share as text", "Mark as applied" with the camera, and an "it took" or "it did not take" card 30 minutes after the first peak slot; a Playwright project at 375 by 812 pixels covers it.

### 9.6 Demo mode

Four seeded sites in a demo company every new company can clone with one click from any empty state, each one a Pakistani energy manager would recognise: a B-2 textile plant in Faisalabad (FESCO, a mostly exempt industrial feeder with random trips and a 2-hour economic-shedding window, 300 kWp, 215 kWh at 100 kW, a 250 kVA genset with a flow meter and a manual start, two-shift load, Eid closures, a switchback calendar 4 of 8 weeks in, an "apply not confirmed" alert, since tier 2 has no read-back); an A-2(c) mall in Lahore (LESCO, a legacy 2019 net-metering agreement expiring in month 7 of the demo year, 200 kWp, 100 kWh, two 400 kVA gensets with an auto-start panel and 180 kW of critical load, an uploaded sample bill rebuilt within 2 percent, a viewer login); a K-Electric B-3 feed mill in Karachi (regime `none` on zero export, a 1 MW gas engine on captive gas, two connections, the MDI line on two bills); an off-grid farm in Rahim Yar Khan (tube-well and cold store, 80 kWp, 120 kWh, a 60 kVA genset with tank-level metering, manual start, the genset page as the main page). There is one computed Demo company; a clone is a set of `demo_link` rows that show its sites inside the customer's company (so no demo login is copied and the four sites are optimised once, not once per customer), made by an admin only, carrying a DEMO ribbon on every page, excluded from real KPIs and reports, regenerated nightly so the window ends yesterday, running on the normal 15-minute schedule so pages change during a sales call, and removable in one click; a narrative seed (PLT-09b) plants the switchback calendar, the resolved alert, the sample bill, the applied records and the genset requests the story names. Every demo number is labelled illustrative and dated; the 3.4-times-fee ratio in R8's sketch is not a claim until the simulator produces it.

### 9.7 First-time flow and design rules

The first-time flow is R8 section 2.19 in seven steps: sign-in and the "No sites yet" page with the demo offer; site and connection with the grid-present check; assets with the slot grammar shown; tariff with the printed bill; control mode (monitor preselected, the native-settings capture, how later tiers unlock); review and activate; the first plan within 15 minutes as a recommendation with its reasons and the Slots view in the vendor's layout.

Design rules (R8 section 2.21, the frontend's lint list): "Rs 1,234,567" with three-digit grouping (the owner confirms against lakh grouping), two decimals on Rs/kWh, "kWh" and "kW" with one decimal under 100, litres as "L", percent with a space, Asia/Karachi 24-hour times, slot labels by start time, day separators in long tables, relative ages with absolute tooltips; window names as on the bill (Peak, Off-peak); one colour per energy source in every chart, chip and node (PV amber, battery green and teal, grid blue, genset red-orange, load slate, unserved magenta, outage hatched, reserve dashed red) checked for colour-blind contrast with icons; "Preliminary" as a visible mark until 28 verified days and stage 1, "Verified" per figure only when its method is complete, "Estimated" on curve-derived litres and estimated SoC; never "guaranteed", "optimal", "optimum" or "maximum savings" (the solver status reads "Solved within tolerance"); every money figure with its method and computation date; every page with a last-data stamp; advice as a request with a reason; role and tier differences shown, not silently removed; ARIA tab pairs that resolve; a 16 px gutter on phones.

### 9.8 Open questions from the study (owner)

Whether analyst is a real role or owner plus an export permission; whether the fee appears in the product at all (the pilot gate is a multiple of it); SMS or WhatsApp for genset requests and who pays; the first demo brand (Deye assumed); the liability of shipped tariff templates (marked "verify against your bill" and dated); lakh versus three-digit grouping; a fleet map in release 1; whether the pilot wizard is visible to all customers.

---

## Part 10. The backend repository

The backend follows Solarlytics' shape exactly, because the owner approved that shape twice and because the sign-in module, its tests and the web app's sign-in pages are written against it (U2 section 4). Replace `gridlytics` and `GRIDLYTICS_` per decision D1.

### 10.1 Layout

```
gridlytics-backend/
  backend/gridlytics/          the package (10.2)
  backend/tests/               pytest: conftest.py, identities.py, golden.py, sibling.py (GRIDLYTICS_FRONTEND_DIR, else ../gridlytics-frontend;
                               a folder that also holds the engine is refused; GRIDLYTICS_SKIP_SIBLING=1 only on a developer's machine),
                               test_troubleshooting.py (pins every launcher and server message to a row through sibling.frontend_file),
                               walk_cases/, fixtures/, contract/
  verification/                replay against python-microgrid (LGPL, unmodified) and the REopt-style reference case; never imported by the product
  bench/                       the MILP benchmark, backtests on recorded sites, the BLAST-Lite wear calibration; reports regenerated with --update
  contracts/ems-edge/1.0/      JSON Schema files of the edge contract
  docs/                        PRODUCT_BRIEF, EQUATIONS, ASSUMPTIONS, COMPETITORS, DESIGN_LOG, TEST_MATRIX, ACCOUNTS, TROUBLESHOOTING, DEPLOY,
                               OWNER_CHECKLIST, HANDOFF_NEXT, BUILD_HANDOFF, BUILD_PROGRESS, THIRD_PARTY_NOTICES
  scripts/                     build_release.py, restore_backup.py, load_check.py, fetch_tariffs.py, fetch_schedules.py, export_contract.py
  demo/                        companies.template.csv, users.template.csv, sites.template.csv; the seed's git-ignored password list
  supabase/                    SQL for review only (copied from Solarlytics and renamed), for supabase mode later
  data/                        git-ignored: platform.db, platform.secret, companies/ (each company's workspace file and its -telemetry file),
                               telemetry/nwp.db, backup/, logs/, models/, .env.founder
  .scratch/venv/               git-ignored: the Python every command in this plan means
  .github/workflows/ci.yml     backend-linux, backend-windows, audit, verification, licences
  Dockerfile  .dockerignore  .env.production.example  requirements.txt  requirements-dev.txt  CHANGELOG.md  CLAUDE.md  README.md
```

### 10.2 The package

| Module | What it holds | Origin |
|---|---|---|
| `api.py` (about 450 lines) | The FastAPI app and guard: hosts, CORS, security headers, body limits, the error and refusal shapes, public and identity paths, the mounting loop over the route modules, health, auth state, users, the optional SPA catch-all, the lifespan hook that starts backups, the scheduler and the job pool | Solarlytics `api.py` lines 1-560, 705-720, 1183-1185, 4897-4942, 6786-6835, cut down (U2 section 4.2); never grows by accretion |
| `schemas.py` | The one-sentence 422, the strict request models; re-exports `Refusal` from `tenancy/errors.py` | Solarlytics `schemas.py` 27-98, 313-330 plus the EMS models |
| `store.py` | ContextVars, DB path, `use_company`, `set_actor`, `_connect`, `write_transaction`, `audit`, the migration chain of Part 3.2 | Solarlytics `store.py` 1-131, 475-526, 956-990, 1402-1420 |
| `telemetry.py` | The company telemetry files: ingest (`INSERT OR REPLACE` by site and minute), read with step, a server-side downsampling function capped at about 2,000 points per series for the charts, gap and outage flags, the nightly 15-minute and daily aggregates, the 13-month Parquet export, retention | New |
| `tenancy/` | The thirteen lifted modules auth (the app roles and permissions of 10.5), identity, platform, companies, store_members, routes_local, founder, routes_founder, store_founder (EMS counts), mfa, apikeys (EMS scopes), backups (both files of a company), seed_demo; plus `routes_platform` trimmed to org and keys, `ratelimit.py` (the `Limiter` of `routes_accept.py`) and a new `errors.py` holding `Refusal` and `fail()` so that no tenancy module imports `api`; `link_company.py` is not lifted; open mode is not lifted | Lifted from solarlytics-backend `c853130` with the edits of U2 section 1.7 |
| `version.py` | The engine stamp and header | Verbatim |
| `jobs.py` | The thread-pool job table scoped per company and owner | Solarlytics `jobs.py`, verbatim |
| `scheduler.py` | The 15-minute loop (Part 2.4) on APScheduler 3.11's `AsyncIOScheduler` started in the lifespan, on only with `GRIDLYTICS_SCHEDULER=1`; the tick at :00, :15, :30, :45 plus 20 s; solves on a `ThreadPoolExecutor` of 2 to 4; a durable `runs` row per site per tick; a per-site lock and argument-hash de-duplication; the other jobs (NWP fetch, schedule fetch, refits, training, scoring, backups, Parquet export) on their cadences; never raising out of a job; staggering sites across the window | New; R7 section 5 |
| `sites.py` | Sites, assets, state, capabilities, data sources, overrides | New |
| `tariffs/` | `catalog.py` (categories, versions, the shipped SRO data with sources), `bill.py` (the 1-minute settlement engine and the monthly rebuild), `vectors.py` (the 192-step price, availability and cap vectors, the ICP exhaustion step, the MDI incumbent, the terminal value), `diesel.py`, `schedules.py` (the CCMS scraper, the K-Electric PDF watcher, manual entry) | New (Part 6) |
| `forecast/` | `providers.py`, `weather.py` (Open-Meteo, forecast.solar, PVGIS, NASA POWER behind `GRIDLYTICS_OFFLINE`), `pv.py`, `load.py`, `outage.py`, `bundle.py`, `scoring.py`, `registry.py` | New (Part 5) |
| `battery.py` | SoC estimate, sigma, trust, SoH, wear coefficient, alerts | New (Part 4.6) |
| `optimise/` | `model.py` (the builder: sparse matrices, bounds, integrality), `backends.py` (scipy.milp, highspy), `mpc.py` (the tick, state, events, result policy, structural cache), `explain.py`, `recheck.py` | New (Part 4) |
| `delivery/` | `mode_a.py` (the slot compiler), `mode_b.py`, `contract.py` (envelope, schema validation, signing with Ed25519), `brands/*.json`, `channels.py` (HTTPS batch, MQTT later) | New (Part 7) |
| `twin/` | `plant.py`, `profiles/*.json`, `edge.py` (the contract endpoint), `faults.py`, `replay.py`, `fleet.py` | New (Part 7.4) |
| `verify.py` | Counterfactual replay, switchback, bill-level comparison, the savings rows, the perfect-foresight bound | New (Part 8) |
| `reports/` | The monthly PDF (ReportLab with matplotlib figures) and CSV from the API's rows, rendered in a job and stored per company with a `reports` row | New; R7 section 9 |
| `routes_sites.py`, `routes_tariffs.py`, `routes_telemetry.py`, `routes_forecasts.py`, `routes_plans.py`, `routes_savings.py`, `routes_reports.py`, `routes_alerts.py`, `routes_edge.py`, `routes_platform.py` | One `APIRouter` per feature; every route `auth.need(permission)` first, a strict model, a 422 in a sentence, a walk case | New; the pattern of Solarlytics `routes_owner.py` |
| `data.py`, `rules.py` | The shipped defaults with their source and date (tariffs, fuel curves, load templates, brand profiles) and the settings bounds | New |

### 10.3 Settings

Module constants from `os.environ` at import for what never changes in a process; functions read at each call for what a test or a scratch run moves. Every name appears in `.env.production.example` first, enforced by the lifted `test_deploy_docs.py` scan; one name per setting, the names below are final and win over the research notes where they differ. The lifted set: `GRIDLYTICS_AUTH_MODE`, `_LOCAL_JWT_SECRET`, the Supabase five, the founder four, `_HOSTS`, `_CORS_ORIGINS`, `_CORS_ORIGIN_REGEX`, `_TRUST_PROXY`, `_PORT`, `_DB` (the tests' default file; no route serves it), `_DATA_DIR`, `_DAILY_BACKUP`, `_LOGS`, `_WEB_DIST`, the three rate limits, `_ERROR_WEBHOOK`, `_DOCS`. The EMS's own: `_SCHEDULER` (1 runs the loop), `_SOLVER_THREADS` (the solve pool, 2 to 4), `_OFFLINE`, `_WEATHER_PROVIDER`, `_WEATHER_KEY`, `_SOLVER` (scipy or highspy), `_SOLVER_TIME_LIMIT` (10 s; hard 30), `_MODELS_DIR`, `_PLAN_SIGNING_KEY_DIR`, `_MQTT_URL` (unused until Product 2). The launcher's `_API_PORT`, `_WEB_PORT`, `_PYTHON`, `_BACKEND_DIR`; the tests' `_FRONTEND_DIR`, `_SKIP_SIBLING`. No `list[str]` through pydantic-settings (the Boxlytics trap).

### 10.4 The lift of the sign-in module (Step 0)

U2 section 1.7 is the recipe down to line numbers; where this plan and U2 section 4 differ (APScheduler in 10.2, the telemetry file in 3.3, `errors.py` below, the setting names in 10.3), the plan wins.

1. Copy the thirteen modules auth, identity, platform, companies, store_members, routes_local, founder, routes_founder, store_founder, mfa, apikeys, backups, seed_demo into `backend/gridlytics/tenancy/`; add `routes_platform` trimmed to org and keys, `routes_accept.Limiter` as `tenancy/ratelimit.py`, and a new `tenancy/errors.py` holding `Refusal` and `fail()` so that no tenancy module imports `api` (the host `schemas.py` re-exports `Refusal`); `version.py` and `jobs.py` are host modules; `link_company.py` is not lifted. Rename every `SOLARLYTICS_`, `solarlytics-local`, `slk_`, `X-Solarlytics-Company`, the logger name and `mfa.ISSUER`.
2. The host `store.py` provides the context functions, `telemetry_path()` and the four tables in migration 1. Open mode is not lifted: `auth.open_mode()` returns False; `GRIDLYTICS_DB` stays only as the tests' default file path.
3. The host `api.py` imports `tenancy.auth`, `identity`, `companies`, `apikeys`, `platform`, `store_members`, `backups` and mounts `tenancy.routes_local` (local mode), `tenancy.routes_founder`, `routes_platform` and the EMS routes through the same loop; `GET /api/users` and `PUT /api/users/{id}` stay in `api.py` because the seed calls `api.users_edit`.
4. Edits named by U2 section 1.7: `auth.ROLES` and `PERMISSIONS` as 10.5; `apikeys` scopes `telemetry:write`, `plans:read`, `sites:read`; `store_founder.read_file` counts sites, plans and days instead of Solarlytics tables; `backups.daily_all` and `back_up` copy both files of a company and `backups.restore` probes for a `sites` table; `seed_demo` gets `sites.template.csv`, the Demo company with the three twin sites and an empty second company for the clone test and the role walk; the legacy-user linking is dropped.
5. Tests carried over (about 200 test functions): `conftest.py`, `identities.py`, `test_identity.py`, `test_platform.py`, `test_companies.py` (less the legacy linking), `test_founder.py` (less `read_file`'s counts), `test_signin_guard.py` (less share links), `test_auth.py` tests 2 and 3 as they are, test 1 dropped (open mode), tests 4 and 5 re-aimed as "a viewer never sees a fuel price or a fee" and "each role changes only the settings its permissions allow", `test_seed_demo.py`, `test_company_files.py`, `test_deploy_docs.py` (its guide test passes once DOC-01 writes `DEPLOY.md`), `test_hygiene.py`'s first five (the fifth pins the security headers); the API-key scope tests re-aimed; `test_role_walk.py` greps every answer to a viewer for the keys `price|cost|fee|wear|_rs|rupee`.
6. Day-one check, with `python` meaning `.scratch\venv\Scripts\python.exe`: `python -m pytest -q -n auto` green on the lift alone; `python -m gridlytics.seed_demo` on a scratch folder; the launcher on scratch ports; sign in at `/login` with a demo account; `/admin` with the founder's code; `GET /api/auth/state` names the company and the role.
7. Record in `docs/DESIGN_LOG.md`'s first section: the reuse of solarlytics-backend at `c853130` by file, and the founder token format's origin in Boxlytics' `founder-auth.ts` at `c429bef` (allowed by the owner's decision of 24 October 2026; the owner confirms the second reuse).

### 10.5 Roles and permissions

Membership roles in `platform.db` are the lifted `owner`, `admin`, `member`; the company's owner is its pinned administrator. App roles in the company file are `admin`, `operator`, `analyst`, `viewer`, given by the company's administrators (everyone starts as viewer). Every route names one permission in `auth.need(...)`, every walk case checks every role, and `RoleGate` in the web app keys on the same names.

| Permission | admin | operator | analyst | viewer |
|---|---|---|---|---|
| `manage_members` | yes | - | - | - |
| `edit_money_settings` (fees, battery prices, wear, the fuel price source) | yes | - | - | - |
| `edit_sites` | yes | yes | - | - |
| `edit_tariffs` | yes | - | drafts | - |
| `approve_tariff` (publish a version) | yes | - | - | - |
| `change_tier` | yes | - | - | - |
| `hold_release` (holds, charge blocks, genset holds, release to native) | yes | yes | - | - |
| `resume_control` (resume after a safety demotion, pause writes, test fallback) | yes | - | - | - |
| `pin_genset` (force charge, pin a genset decision) | yes | - | - | - |
| `apply_plan` (mark a Mode A table applied) | yes | yes | - | - |
| `ack_alerts` | yes | yes | - | - |
| `upload_data` | yes | yes | - | - |
| `export` | yes | - | yes | - |
| `see_costs` (costs, rates as money, savings in rupees) | yes | yes | yes | - |
| `see_fee` | yes | - | yes | - |
| `start_pilot` | yes | - | - | - |

A machine key carries scopes (`telemetry:write`, `plans:read`, `sites:read`), not a role; two key kinds exist (edge and partner), a key is bound to one site and every envelope whose `site_id` differs from the key's site is refused with 403, plans served to a key carry no money fields, and each key has an ingest rate budget separate from the address limiter. An installer is an operator in the site's company (a site lives in one company's file; cross-company access does not exist). The viewer filter is server-side and typed, not a key grep: every money field in every response model is annotated `Money` (prices, costs, fees, wear, savings, objectives, VoLL, demand rates, tariff bodies' rates), the filter strips by type, reason texts and alert details come from viewer-safe templates with the money parameter omitted, and `test_role_walk.py` compares the admin's and the viewer's answer to every route and fails on any shared money value. Reports and exports carry a `kind` (money or energy-only); a viewer gets 404 on money kinds and only energy-category alerts; every export is audited. A named viewer may be granted `see_costs` by an admin. The founder (OnyxLeaf) is not a role but the console: companies, people, the product on or off per company, backups, counts, the audit; it never reads a company's telemetry or plans.

### 10.6 Jobs, the scheduler and scale

- Interactive work (a what-if solve, a backtest, a CSV import, a report, a model training) runs in the lifted job pool: per company and owner scoping, limits, a request-hash cache, abandonment after 180 s, ContextVars copied into the pool thread; the API returns 202 with a job URL and the page polls.
- The 15-minute tick runs on APScheduler in exactly one process per data folder; sites are spread across the window by a stable hash; a site whose previous solve is still running is skipped and logged; the ingest route caches the API-key-to-company lookup for 60 seconds so a batch a minute per site never re-reads `platform.db`.
- At 100 sites one core suffices (2 to 6 s a solve measured, 10 s budgeted, staggered: one core serves about 90 sites at half load). At 1,000 sites, as line changes rather than rewrites: RQ 3.0 workers on Valkey (BSD) take the solves (8 to 12 vCPU) and post results to the single-writer API, forecasts batch by NWP cell, the telemetry tables move to PostgreSQL 18 with TimescaleDB 2.30 (Timescale licence: self-hosting is allowed; the database itself is never offered as a service) behind the same repository functions, and Valkey pub/sub fans out the SSE stream across processes (R7 section 13).

### 10.7 Dependencies

`requirements.txt` with exact dated pins (R7 section 12.1 names the versions read on 6 November 2026, except that the solver pair stays at the measured scipy 1.17.1 and numpy 2.4.3 until the CI benchmark passes on 1.18, and xgboost at the installed 3.2 unless the pilot's training needs a later one): fastapi, starlette, pydantic, uvicorn, numpy, scipy (the MILP), pandas, pyarrow, pvlib, xgboost, scikit-learn, holidays, PyJWT, cryptography (also Ed25519 signing), APScheduler 3.11, sse-starlette (the SSE step), reportlab, matplotlib, httpx, python-multipart, threadpoolctl. Optional: highspy 1.15, skforecast, lightgbm, aiomqtt (when Product 2 exists). Dev: pytest, pytest-xdist, httpx, pip-audit, pip-licenses. No `jsonschema`: the contract's validator is pydantic (Part 7.3). Never: dramatiq and fpdf2 (LGPL), psycopg 3 (LGPL; asyncpg if PostgreSQL comes), python-mip (EPL), Redis 8 (use Valkey), EMQX (BSL; use Mosquitto). Verification and bench only: python-microgrid (LGPL-3.0), NREL-PySAM (BSD-3), BLAST-Lite (BSD-3), CityLearn (MIT), Quartz (MIT, own venv). The backend has its own venv, made once in PLT-02 with `C:\Users\SaadTahir\anaconda3\envs\ops\python.exe -m venv .scratch\venv` and then `.scratch\venv\Scripts\python.exe -m pip install -r requirements-dev.txt`, because the `ops` env lacks PyJWT, cryptography, pvlib, APScheduler and pytest-xdist and `python` on PATH is the Windows Store stub. `.scratch\venv\Scripts\python.exe` is what every `python` in this plan and the handoff means; `CLAUDE.md` says so; the launcher in the web repository defaults to `..\gridlytics-backend\.scratch\venv\Scripts\python.exe` when it exists, else `GRIDLYTICS_PYTHON`, and refuses the Store stub with a TROUBLESHOOTING row.

### 10.8 Documents set

Backend `docs/`, as Solarlytics: `PRODUCT_BRIEF.md` (what the app does, inputs per tab, method, verification table), `EQUATIONS.md` (every formula with a status letter and `[verify]` on recalled values: the MILP rows, the tariff rules, the fuel curve, the wear curve, the forecast metrics), `ASSUMPTIONS.md`, `COMPETITORS.md` (from the reports), `DESIGN_LOG.md` (the guide's shape, one section per batch), `TEST_MATRIX.md`, `GATES.md` (the backend, verification and bench commands with their expected counts and timings), `ACCOUNTS.md` and `TROUBLESHOOTING.md` (the same two files in both repositories, pinned equal by a sibling test; every launcher and server message pinned to a row by `test_troubleshooting.py`), `DEPLOY.md`, `OWNER_CHECKLIST.md`, `HANDOFF_NEXT.md`, `THIRD_PARTY_NOTICES.md`, `CHANGELOG.md`, and `PLAN.md`, `BUILD_HANDOFF.md`, `BUILD_PROGRESS.md` copied from the plan package at Step 0 and kept there. Web `docs/`: `API_CONTRACT.md` (per screen), `GATES.md` (the web and browser-check commands and counts), `CONVENTIONS.md`, `ACCOUNTS.md`, `TROUBLESHOOTING.md`.

### 10.9 CI

`backend-linux` on every push and pull request (Python 3.12, `pytest -n auto`, the web repository checked out beside as `gridlytics-frontend` with a read-only token); `backend-windows` on a pull request into main, by hand and weekly; `audit` (pip-audit); `licences` (pip-licenses against the allow-list of Part 14); `verification` by hand and weekly; `bench` (the 192-step gate with a 20-second assertion) and `image` (the Docker build) by hand and on a push of `v1-build`. A push cancels the run it replaces. The agent never pushes, so CI runs only when the owner pushes `v1-build` at a stage exit (Part 13.2); until then the stage gate is local.

---

## Part 11. The frontend repository

The web app is in Boxlytics' stack (the owner asked for "just like Boxlytics") with Boxlytics' good patterns rewritten clean-room and its known faults left behind (U1 sections 5 and 6), and with the sign-in pages lifted from Solarlytics' web app against the identical routes and refusal codes. Replace `gridlytics` per decision D1.

### 11.1 Stack (pins verified on the npm registry on 6 November 2026, U1 section 7.1)

React 19.3, TypeScript 5.9 (TypeScript 7 revisited at month 2), Vite 8.3 with the React plugin 6, Tailwind 4.3 through the Vite plugin with CSS-first `@theme` tokens, react-router 7.18 imported from `react-router`, TanStack Query 5, Zustand 5, zod 4, react-hook-form 7.91 with resolvers 7 (fallback 5.2), axios 1.21 (or a 40-line fetch wrapper), Radix primitives only (tabs, dialog, tooltip, popover, dropdown, select), lucide-react 1.x, sonner, clsx and tailwind-merge, date-fns 4 with Asia/Karachi handling, recharts 3.10 for the dashboard and plan bars, Apache ECharts 6 (Apache-2.0, canvas, tree-shaken and code-split behind a 40-line hook) for the 1-minute explorer and the 48-hour Gantt, with uPlot named as the fallback and every dense series downsampled by the API to about 2,000 points (R7 section 8). Live updates by React Query polling (30 s on live views, 60 s on dashboards, 2 s on a running job); one SSE stream per tab through sse-starlette as the next step, never WebSockets for the UI. Dev: vitest 5 with coverage, jsdom 31, Testing Library, msw 2, Playwright 1.65, eslint 9 or 10 flat config with typescript-eslint 8.72 and import ordering, prettier 3.9 with the Tailwind plugin. Node 22 (`.nvmrc`, `engines`). Not added: framer-motion, three.js, html2canvas, Headless UI, jspdf (reports come from the backend, decision D13).

### 11.2 Layout

```
gridlytics-frontend/
  frontend/
    src/
      main.tsx, app/ (router.tsx with public, auth, app and ops shells; providers.tsx; query-client.ts; env.ts zod-parsed)
      features/   auth (the lifted sign-in: login, admin console pages, session, company switch, NotSetUp; guards; auth store)
                  sites (fleet list, site onboarding wizard with lettered tabs, site settings)
                  overview (per-site dashboard), plan, forecasts, tariffs, savings, battery, genset, outages, alerts,
                  devices (control, applied tables, write log), reports, org (members, roles, keys, settings), ops (the founder console pages, lifted)
      components/ ui (Radix wrappers, DataTable, EmptyState, StatCard, PageHeader, ErrorBoundary), layout (app shell with Outlet, ops shell,
                  public shell, sidebar, header, site switcher), workflow (WorkflowTabs, WorkflowNavigation, useLetteredTabs), charts (themed wrappers:
                  TimeSeriesChart, PlanGantt, SlotTable, SocChart, EnergyFlow, SavingsBars, ForecastBands)
      lib/        api/client.ts (live token, refresh once then logout, X-Request-Id), api/endpoints.ts (typed per docs/API_CONTRACT.md),
                  errors.ts, format.ts (PKR, kWh, kW, litres, percent, slot labels), time.ts (slot index and timestamps, 48-hour helpers), cn.ts
      stores/     ui.store, selected-site.store (auth store is feature-owned)
      types/      api.ts (DTOs mirrored from the backend's models), domain.ts
      test/       setup.ts, msw handlers, renderWithProviders, fixtures (a 192-step plan, a tariff calendar, a day of telemetry, a forecast bundle)
      styles/     index.css with @theme tokens
    tests/e2e/    Playwright: dev-bypass login, fleet, site onboarding tabs, plan page, savings report
  scripts/start_local.py (the launcher, Solarlytics' pattern: windowless children, one server per data folder, --scratch on 8151/5293, .env.founder)
  scripts/e2e/ (the browser check driven over the DevTools protocol, as Solarlytics' step1.mjs)
  docs/ API_CONTRACT.md (per screen: endpoint, fields, units, fallbacks), GATES.md (exact commands and expected counts), CONVENTIONS.md,
        ACCOUNTS.md, TROUBLESHOOTING.md (the same two files as the backend's)
  .claude/launch.json (one configuration, --scratch, never the owner's ports or data)
  .github/workflows/ci.yml, .nvmrc, vercel.json, .env.example (every VITE_* the code reads, including the dev-bypass trio)
```

### 11.3 Patterns kept from Boxlytics (rewritten) and from Solarlytics (lifted)

- Guard-then-layout routing expressed once with nested routes and `Outlet`; one `useAuthRoutingState` discriminated union feeding thin guards; session bootstrap once in `main.tsx` with minimal persisted state.
- One axios instance with interceptors that read the live session, retry once after a refresh and only then log out; `getErrorMessage` aware of FastAPI's `detail` and the backend's `{detail, code, request_id}` refusals.
- Typed service objects consumed only through React Query with hierarchical keys and invalidate-on-success; no data calls outside `lib/api`.
- The `VITE_DEV_AUTH_BYPASS` mock session, gated on `import.meta.env.DEV`, used only by msw-backed component tests and proved absent from the production bundle by a test. There is no backend bypass: Playwright and the browser check sign in through `POST /api/local/session` with the seed's demo accounts on a scratch folder, reading the git-ignored password list.
- `RouteErrorBoundary` per route; Button variants and `cn()`; HSL tokens; pure utils with golden tests; the two documents (the API contract per screen and the gates file).
- Lifted from Solarlytics' web app (the owner's own code): `/login`, `/admin` (the founder console pages), `session.ts`, `companyswitch.tsx`, `NotSetUp.tsx`, the launcher and its tests, the browser-check runner; restyled to the Tailwind tokens, routes and refusal codes unchanged (`api.SIGN_IN_CODES`). The repository is not on this machine and the backup folder does not hold it: the owner clones `saad-tahir/solarlytics-frontend` to `C:\Users\SaadTahir\Videos\solarlytics-frontend` (read-only) before Step 0 (owner task O11); if the folder is absent, PLT-07 stops and asks.
- Left behind: 6,000-line pages with every tab panel mounted and hidden; the 401 hard redirect without refresh; a static API key in the bundle; Supabase RPCs from the browser; dead entry files and patch scripts; unused dependencies; inline brand hex; console logging of env values.

### 11.4 Lettered tabs

Tabs declared once as a typed const array per page (id, letter, label, locked); a tested `useLetteredTabs` hook (status callback, unlock callback, URL `?tab=` sync so refresh and deep links work, `select()` that refuses locked tabs and scrolls to top only on an accepted change, next and previous skipping disabled tabs); one panel component per tab in its own file, mounted only when active, with real `id` and `aria-labelledby` pairs; one react-hook-form instance per page with a zod schema per tab and a `useTabStatuses` helper; drafts autosaved to the backend (debounced) so tab B is never lost; the last tab a read-only Review with the activate action; results and plans on their own routes, never a locked Results tab. Lettered tabs are for sequential editors (site onboarding, the tariff editor, pilot set-up); dashboards, the plan, telemetry and savings use plain Radix tabs or sub-routes.

### 11.5 Conventions, scripts, tests, CI

- Files kebab-case, components PascalCase, one component per file, no `React.FC`, no default exports except route lazies; `@/` imports only; a page under 300 lines; route-level `lazy()` per feature; every money and energy formatter and every slot-time conversion has a golden test; no scripts that rewrite source.
- Scripts: `dev`, `build` (typecheck then vite build), `preview`, `typecheck` (`tsc --noEmit -p tsconfig.app.json`, never bare `tsc`), `lint --max-warnings 0`, `format`, `format:check`, `test` (vitest with coverage), `e2e`, `gates` (all of them).
- Required tests from day one: the routing-state union table; the interceptor's refresh-then-logout; `useLetteredTabs`; golden formatters and slot-time conversions across the PKT offset; one msw-backed page test per feature; Playwright smoke of login, fleet and the plan page tabs at desktop and at 375 by 812 pixels; the browser check of every page in every role (Solarlytics' pattern, counts recorded in `GATES.md`); a coverage threshold starting at 60 percent of lines; the npm licence gate of Part 14.3 and a lint rule banning `dangerouslySetInnerHTML`.
- CI: one workflow on push and pull request with node from `.nvmrc`: `npm ci`, typecheck, lint, format check, tests with coverage, build, Playwright against `vite preview` signed in with the seed accounts against the backend's test server; Vercel preview deploys per pull request; `VITE_DEV_AUTH_BYPASS` never set in hosted environments. CI runs when the owner pushes; the agent's gate is `npm run gates` and the browser check locally.

---

## Part 12. Build order: stages, items, acceptance

Nine stages in strict order; each ends with the four suites green locally and its exit check (CI runs when the owner pushes `v1-build` at the stage exit, Part 13.2). Items carry an area prefix (PLT platform and skeleton, SIT sites, TAR tariffs and bills, DAT telemetry and ingest, TWN twin, FOR forecasts, OPT optimiser and battery, DLV delivery and contract, VER verification and reports, UI pages, DOC documents, DEP deployment) and are the rows of `EMS_BUILD_PROGRESS.md`. Each item is sized for one to three commits; the large ones were split so that a usage-limit stop inside a stage still leaves finished rows. "Refs" names the part of this plan and the research note that specify the item. Effort is a rough planning figure in agent-days, not a promise. Acceptance on twin data proves that a pipeline runs; every number that measures quality (forecast skill, replay accuracy, the increment) is measured on real data: owner task O12's month, then the pilot.

### Stage 0. Safety net, the lift and the two skeletons (about 6 days)

| ID | Item | Acceptance | Refs |
|---|---|---|---|
| PLT-01 | Create `gridlytics-backend` and `gridlytics-frontend` locally (the owner creates the GitHub pair, O10); baseline commit on `main`; branch `v1-build`; `.gitignore`, `.gitattributes` (LF), `core.autocrlf false`, `CLAUDE.md` with the rules and the Python path | Both repositories exist with one baseline commit | Part 10.1, 11.2, 13.3; U2 2.1 |
| PLT-02 | Backend skeleton: the venv (`ops` python `-m venv .scratch\venv`, then `requirements-dev.txt`), package, `api.py` cut-down guard, `schemas.py`, `store.py` with migration 1 and `telemetry_path()`, `version.py`, settings names, `.env.production.example`, `requirements*.txt` with dated pins | `GET /api/health` answers; `test_deploy_docs.py` tests 1 to 3 green (the guide test passes at DOC-01); the venv is the Python the launcher finds | Part 10.2, 10.3, 10.7; U2 4 |
| PLT-03 | Lift the tenancy package as Part 10.4 (thirteen modules, the trimmed `routes_platform`, `ratelimit.py`, `errors.py`; open mode not lifted; `backups.daily_all` and `back_up` copy both files of a company; `restore` probes for `sites`); the app roles and permissions of 10.5; API-key scopes; the carried tests re-aimed as 10.4 step 5 | The carried tests green (`-n auto`); sign-in, companies, founder console and keys work on a scratch folder; a request without a token gets 401; the daily copy includes the telemetry file | Part 10.4, 10.5; U2 1.7 |
| PLT-04 | Jobs table lifted; the APScheduler skeleton in the lifespan (the 15-minute tick at :00, :15, :30, :45 plus 20 s with durable `runs` rows, the per-site lock, staggering, the status endpoint, no work yet); the launcher sets `GRIDLYTICS_SCHEDULER=1` on the owner's folder and on `--scratch` | A job runs and is polled; the scheduler ticks on a scratch run and never in tests; two ticks for one site never overlap | Part 2.4, 10.6; R7 5 |
| PLT-05 | Demo seed with `sites.template.csv` and the founder's settings into `.env.founder`; two companies: Demo (the three twin sites of Part 9.6, empty of history until PLT-09) and an empty company for the clone test and the role walk; passwords only in the git-ignored list | `python -m gridlytics.seed_demo` on a scratch folder; people in every role; re-running the seed on a scratch folder is allowed and idempotent | Part 7.4, 9.6; U2 4.4 |
| PLT-06 | Frontend skeleton: the stack pins, folders, scripts, env module, providers, router with the four shells, UI kit basics, chart wrappers (empty), test set-up with msw and fixtures, Playwright signing in with the seed accounts, CI workflow | `npm run gates` green on an empty app; a smoke e2e signs in on a scratch backend | Part 11; U1 7 |
| PLT-07 | Lift the Solarlytics web sign-in pages, session, company switch, NotSetUp, the founder console pages, the launcher (defaulting to the backend's venv, refusing the Store stub and answering ports, ports 8051/5195 and 8151/5295) and the browser-check runner from `C:\Users\SaadTahir\Videos\solarlytics-frontend`; restyle to the tokens | Login, company switch and `/admin` walk against the backend on scratch ports; the launcher is windowless. Requires owner task O11; if the folder is absent, stop and ask | Part 11.3; U2 2.5 |
| PLT-08 | `useLetteredTabs`, `WorkflowTabs`, `WorkflowNavigation` with their tests | Hook tests pass (lock, skip disabled, URL sync, scroll only on accepted change) | Part 11.4; U1 8 |
| DOC-01 | The documents set of Part 10.8 created with first sections in both repositories (backend: PRODUCT_BRIEF, EQUATIONS, ASSUMPTIONS, COMPETITORS from the reports, DESIGN_LOG's first section with the clean-room and reuse statement, TEST_MATRIX, GATES, ACCOUNTS, TROUBLESHOOTING, DEPLOY, THIRD_PARTY_NOTICES, and PLAN, BUILD_HANDOFF, BUILD_PROGRESS copied from the plan package; web: API_CONTRACT, GATES, CONVENTIONS, ACCOUNTS, TROUBLESHOOTING); `test_troubleshooting.py` pins every launcher and server message to a row through the sibling | Every file exists; the troubleshooting test and the sibling-equality test pass | Part 10.8, 14 |
| DEP-01 | CI workflows for both repositories (the jobs of Part 10.9 and 11.5; the web repository checked out as `gridlytics-frontend` with the read-only token) | The workflow files pass `actionlint`; each CI step's command passes locally on Windows; the first green CI run is recorded in GATES after the owner's push | Part 10.9, 11.5, 13.2 |

Exit check: the day-one check of Part 10.4 step 6 on a scratch folder; counts in the session log and GATES; the owner asked to push `v1-build`.

### Stage 1. Brand data, sites, assets, the tariff engine and the bill engine (about 10 days)

| ID | Item | Acceptance | Refs |
|---|---|---|---|
| DLV-00 | Brand data files `delivery/brands/*.json` with provenance blocks for Deye, Huawei, Sungrow, Growatt, Voltronic, Solis: `slot_model` (max slots, ring, fields, resolution, min slot minutes, weekday mask, table writes per day), primitives with persistence class and limits, budgets, timers, quirks, `bench_confirmed` per value; the capabilities-snapshot shape that reads them; written clean-room from the permissive sources and, for Huawei, from Huawei's published interface definitions | Each file validates against one schema; a test pins every value to its provenance line; the Huawei file's provenance reads "read for facts, nothing copied" for the AGPL library | Part 7.1, 14; R6 3.1 to 3.5; U3 2.2 to 2.4 |
| SIT-01 | Sites, connections and assets tables, routes and strict models: site (outage unit, `genset_start_control`, `outage_load_mode`), connections (category, regime including `none`, MDI, ICP baseline, tax flags, seasonal multiplier), PV arrays, inverter (its slot grammar from DLV-00), battery, genset (fuel kind and curve, limits, costs, parallel flag), meters; `native_settings` and `critical_load_profiles` tables | CRUD in every role with walk cases; 422 in a sentence on every bad field; a two-connection site rebuilds per connection | Part 3.2; U3 1 |
| SIT-02 | Site state row and its update functions (including the genset's tri-state and the open MDI block); overrides table and routes (reserve, genset hold, charge block, pause) with expiry and audit; `site_drafts` | An override is audited and expires; a draft survives a restart | Part 3.2, 4.8 |
| SIT-03 | Critical-load profile per site (hand-entered on day 0; learned later from backup-load points or genset-off outages) | Stored per hour of day; versioned | Part 4.1, 5.2 |
| TAR-01 | The versioned objects of Part 6.2 (schedule with rate cards, the two TOU calendars with the daylight-saving hook, monthly adders with exemption sets, tax rules, the ICP rule, the prosumer regimes, fuel prices, the site binding with overrides), each with `Source` and `Validity`, never edited in place, resolved by date and by billing month, with `known_from` | Peak membership tests at every season boundary and at the midnight-crossing window for both calendars; a new version supersedes without touching the old | Part 6.1, 6.2; R9 1, 2, 11, 12 |
| TAR-12 | The shipped data: the three 2025-26 schedule versions (FY2025-26, SRO 41-52(I)/2026, SRO 279(I)/2026), the empty CY2027 version marked "awaiting notification", Tariff F's seasonal multiplier, the regime `none`, the fuel kinds, the connection tax flags; every number with its source and status, the legacy netting rule marked unverified | Every shipped number has a source and a status; the provenance view lists them; a Tariff F golden | Part 6.1; R9 1 to 10 |
| TAR-13 | Tariff routes and the provenance view; the pending-version approval flow | Walk cases in every role; a viewer cannot read rates as money | Part 6.2, 9.3 |
| TAR-02 | Regimes: net billing (flat NAEPP export credit versioned quarterly, the optional TOU export schedule, settlement and carry-forward, agreement dates and caps), legacy net metering (by-period netting as an option, surplus at the configured rate, carry-forward, agreement end, modification flag), flat, off-grid | Each regime has a hand-worked golden | Part 6.1; R9 8 |
| TAR-03 | The ICP as a price regime by monthly totals: baseline per month and period, escalation, new-consumer rules with load factors, net-metering cap, eligibility and validity; month-to-date tracking per period; the three per-period cases for the optimiser (above, below, uncertain) with the rest-of-month net-import forecast | Golden: a peak period whose month ends above baseline, an off-peak one that ends below, and the uncertain middle case | Part 4.3, 6.1; R9 5 |
| TAR-04 | MDI: 30-minute fixed blocks aligned to the hour over all hours (interval, block mode and scope as site settings), the basis floor per category, the month-to-date incumbent and the open block's elapsed mean, the PF penalty, the gross-up factor for fixed charges | Golden case against a hand-worked bill; the real bill's fixed charge once O3 lands | Part 4.3, 6.1; R9 3 |
| TAR-05 | The surcharge and tax stack as configurable lines (the debt-service surcharge, the optional Neelum-Jhelum line, provincial duty by province and category, the Punjab per-unit duty above 500 kVA on grid and self-generated units, GST, further and extra tax, income tax slabs, PTV, MUCT, late payment) with bases and exemptions and per-site overrides from bills | Golden bill rebuilt line by line in R9 section 7's order | Part 6.1; R9 7 |
| TAR-06 | The 1-minute bill engine `tariffs/bill.py`: the thirteen steps of Part 6.3 with the real reading dates, the ICP split by totals, adders by billing month, duty, credits, taxes, flat items, the three aggregates, version and series hashes; the metering factor per site; the reconciliation of a real bill with residuals logged | The four hand-worked bills of Part 6.3 (B-2(b) ICP month across the November-to-December window change, A-2(c) net billing with export credit and GST, legacy net metering, A-3 flat) match to the paisa; then each pilot site's last three bills within 2 percent on the calibrated rebuild (O3), recorded as open in GATES until then | Part 6.3, 8.2; R9 13 |
| TAR-07 | Price and limit vectors for 192 steps as Part 6.4: all-in prices with the ICP case per period, the grossed-up export credit, availability for observed outages and high-adherence windows and the adherence of the rest, caps, the MDI incumbent, blocks, floor, elapsed mean and gross-up, the day's delivered diesel price and self-generation duty, the terminal inputs; "as known" versus "as billed" | Vector tests at every window boundary in every season; each ICP case prices as specified; the marginal-price goldens of Part 4 start here | Part 4.1, 6.4 |
| TAR-08 | Fuel prices by kind (diesel daily from OGRA; captive gas and LNG from the site's contract) with effective dates and a delivered-price adder per site; per-genset fuel curves in the fuel's unit fitted from data-sheet points with HOMER's example as the placeholder | A dated price per kind is used by the bill and the optimiser; the fit reproduces the example's 0.405 and 0.306 L/kWh; a gas-engine golden | Part 6.1; R9 9 |
| TAR-09 | Watchers and alerts: nepra.org.pk notifications and legal pages, ke.com.pk, OGRA, with a person approving each proposed version (owner task O13: within five working days of a notification); the January 2027 schedule change flagged; a month's report blocked until that month's adders are approved | A proposed version appears as pending and never applies without approval; a report for a month with unapproved adders is refused with the reason | Part 6.2; R9 12 |
| TAR-10 | The onboarding checklist of Part 6.5 as the tariff tab's bill-reading form, each answer a versioned site override with the bill as source | The checklist fills from the demo bill fixture | Part 6.5; R9 15 |
| UI-00 | Shared components: SiteHeader with tier and control-state chips and the last-data stamp, KpiTile, SlotTable, EnergyFlow, TimeSeriesChart (ECharts), ReasonChip, EmptyState with the demo offer, RoleGate and TierGate, the formatters and the design-rule lint (banned words, Rs grouping, Asia/Karachi) | Component tests; the lint fails on a banned word | Part 9.2, 9.7; R8 2.0, 2.21 |
| UI-01 | Fleet page: summary strip, the site table (tier, control state, last data, live mini flow, next event, alerts, MTD saving, sparkline), filters; empty states; one-site redirect | Renders from a fixture; msw page test; the viewer sees no rupees | Part 9.3; R8 2.1 |
| UI-02 | Site onboarding wizard with lettered tabs A to D and Review as Part 9.3 (the brand shows its slot grammar from DLV-00; tab D's tier choice sets `tier_allowed` never above `tier_capable`); drafts autosaved; the connection test and the native-settings capture land in Stage 3 as UI-02b with DAT-03 and VER-01 | A site is created end to end in the browser check; tab B survives a refresh | Part 9.3, 11.4; R8 2.2 |
| UI-03 | Tariffs page: library, versions, the lettered-tab editor A to D with the heatmap preview and the price-vector review, "Simulate on site X", the pending versions from the watchers | A version is added and shown; viewers cannot open the page | Part 9.3; R8 2.3 |
| DOC-02 | EQUATIONS rows for every tariff rule with status letters; ASSUMPTIONS for every default; the marginal-price rule of Part 4 recorded | Reviewed against R9 | Part 10.8 |

Exit check: the four hand-worked bills match to the paisa; the site wizard and the tariff page walk in every role; the owner asked to push.

### Stage 2. Telemetry, ingest and the twin (about 9 days)

| ID | Item | Acceptance | Refs |
|---|---|---|---|
| DAT-01 | The company telemetry file: the wide minute table keyed by site and minute, `INSERT OR REPLACE`, the day read; the daily copy covers it | A site-year written and read back; a day read under 10 ms; the daily backup copy includes the file | Part 3.3; R7 2, 3 |
| DAT-08 | Reads with step and the server-side downsampling endpoint (about 2,000 points per series) | A 30-day range at 1 minute returns downsampled under 200 ms | Part 3.3, 11.1; R7 8 |
| DAT-09 | Quality flags, gap detection, the nightly 15-minute and daily aggregates, retention rules | Gaps become events; a year's daily aggregate under 1 s | Part 3.3 |
| DAT-02 | The contract as pydantic strict models in `delivery/contract.py`; the schema files generated by `scripts/export_contract.py` and pinned by a test | Every example in U3 section 5 validates; a bad message is refused with the reason code; a drifted schema file fails the test | Part 7.3; U3 5 |
| DAT-03 | HTTPS batch ingest for telemetry, capabilities, events, ack, heartbeat with a site API key (`telemetry:write`, 1 MiB cap, a 60-second cache of key to company, a per-key rate budget); an envelope whose `site_id` differs from the key's site is refused with 403; idempotent by (site, seq); capabilities set `tier_capable` only; capabilities and acks stored | Contract tests 1 and 8; a batch a minute from 50 twins costs no `platform.db` read and trips no address limiter; a cross-site write is refused | Part 7.5, 10.5; R7 4 |
| DAT-04 | CSV upload with a saved column mapping per source, units and signs, blanks, duplicates, gaps, size limits, the tier-1-only rule without `inverter_mode_active`, a report of the import; the per-source staleness rule and the planned-at-upload path of Part 2.4 | A 30-day file imports with every minute present or flagged; a CSV site gets a plan at upload | Part 2.4, 7.5; U3 4.2 |
| DAT-05 | Load reconstruction from inverter telemetry and the censoring rules; grid-present derivation from voltage when absent; the tri-state genset signal | Censored samples flagged on an outage fixture; a missing genset signal is unknown | Part 5.2, 4.8 |
| DAT-06 | Events table and routes: outage start and end detected from the grid-present flag, genset start and stop, data gaps, fuel logs, plus the contract's event types | Events appear on the outage fixture | Part 3.2 |
| TWN-01 | Twin plant model at 1-minute steps: PV, load, battery with BMS error, inverter native mode with slots and SoC targets, grid with scheduled and random outages, genset auto-start with fuel curve and minimum run, both outage load modes | 90 generated days per demo site are physically consistent (balance within 1 W, SoC within bounds) | Part 7.4 |
| TWN-02 | Twin brand profiles (from DLV-00) with slot grammar, primitives, budgets, timers, quirks | Capabilities messages per profile validate | Part 7.1, 7.4; R6, U3 |
| TWN-03 | Twin contract endpoint in process: telemetry batches, capabilities, events, heartbeat; replay mode from a CSV | The three demo sites stream into the API on the launcher's scratch folder | Part 7.4 |
| PLT-09 | Demo mode: the one computed Demo company's four twin sites of Part 9.6 (including the auto-start genset and the zero-export gas-engine mill) with 90 days of history, shown in a customer's company through `demo_link` rows made by an admin, the DEMO ribbon, excluded from real KPIs, regenerated nightly, removable; PLT-09b the narrative seed (switchback calendar, resolved alert, sample bill, applied records, genset requests) added at the Stage 5, 6 and 7 exits | Every page of Stages 1 and 2 shows real-looking data on day one; the linked sites appear on the Fleet page of a new company with no demo login copied; the narrative parts are re-checked at the later exits | Part 7.4, 9.6; R8 2.18 |
| UI-04 | Site overview page: live energy-flow diagram, today's KPIs, grid and genset state, SoC with its band, last telemetry age, data-quality strip | Renders from the twin; updates by polling | Part 9.3 |
| UI-05 | Telemetry page: 1-minute and 15-minute series charts with range, outage and genset bands, data-quality flags, CSV export | Chart of a day renders under 1 s from the API | Part 9.3 |
| UI-06 | Devices and data page: data sources, API keys (admin), CSV upload with mapping, capabilities history, the native-settings capture card, control tier shown as asserted by the source | A CSV is uploaded through the browser check | Part 9.3 |
| UI-07 | Outages page: log, schedule (manual entry), the feeder, reserve adequacy placeholder | Renders events and the manual schedule | Part 9.3 |

Exit check: the three demo sites live; a CSV import and an HTTPS batch both land as identical rows; contract tests 1 and 8 green; the owner asked to push.

### Stage 3. The baseline replay and the savings engine (about 7 days)

| ID | Item | Acceptance | Refs |
|---|---|---|---|
| VER-01 | Native-mode capture: the inverter's own settings per site by read-back or a photograph (JPEG or PNG, 5 MB cap, company-scoped storage), immutable, a later change a new capture (`native_settings`) | Stored and shown; used by the replay and the fallback; an edit creates a version | Part 8.1 |
| UI-02b | The wizard's connection test (the first rows received, the detected points, the refusal without a grid-present signal) and the native-settings capture step, wired to DAT-03 and VER-01 | The browser check completes a site with a live twin source | Part 9.3; R8 2.2 |
| VER-12 | The tuned baseline: the best static table of the site's brand grammar fitted by the engine on the observation month, and HOMER cycle charging at genset sites | The fitted table replays to a lower bill than the native capture on the demo sites; both baselines stored | Part 8.1 |
| VER-02 | Counterfactual replay: the twin in replay mode runs both baselines on the measured day; the bill engine prices the measured arm and the replayed arms at 1 minute; the `savings_days` rows per baseline | On twin days the replay reproduces the twin's recorded import and throughput exactly (a regression test); on O12's real month within 3 percent daily import and 5 percent throughput on 90 percent of days (recorded as open in GATES until O12 lands) | Part 8.2 |
| VER-13 | The metering factor per connection (one energy scalar, one MDI scalar) fitted on two bills and tested on a held-out third, applied to both arms; the bearable-cost basis (the `gst_recoverable` and `s235_adjustable` flags) in every rebuild, ledger, fee check and gate, with "taxes you recover" as its own line | Fitted and held-out on the demo bill fixtures; a factor outside 0.97 to 1.03 raises a data finding; a registered B-2 site's ledger is ex-GST | Part 6.3, 8.2 |
| VER-03 | Savings page v1: daily and monthly rupees against each baseline, diesel, peak, throughput, the method stated, CSV export with tariff, capture and plan ids | Numbers equal the golden rows; the saving column reads zero and says why until Stage 5 | Part 9.3 |
| VER-04 | Bills table and the bill-level comparison: real bill entered, rebuilt, difference, residuals per line | The hand-worked bills match; a pilot bill fixture flow walks | Part 8.2 |
| VER-05 | The PySAM replay cross-check in `verification/` | Report section committed | Part 13.1 |
| DOC-03 | DESIGN_LOG and EQUATIONS for the replay, the baselines and the fair-accounting rules | Reviewed | Part 8.4 |

Exit check: the baseline bill totals of a month of twin history equal a hand calculation in the test docstring; the owner asked to push.

### Stage 4. Forecasters v1 (about 9 days)

| ID | Item | Acceptance | Refs |
|---|---|---|---|
| FOR-01 | Provider interface, settings per site, the import provider, the forecast rows with belief time, the bundle and its 30-minute freshness rule | A CSV-imported forecast serves a bundle | Part 5; R4 A7 |
| FOR-02 | Weather client: Open-Meteo (deterministic, and the ensemble, air quality, Historical Forecast and Previous Runs once the plan is bought, O5), grid-cell deduplication, caching, `GRIDLYTICS_OFFLINE` fixtures, the fallback chain with flags and quantile widening, the health probe and circuit breaker | Offline tests on fixtures; one live smoke test by hand | Part 5.6 |
| FOR-03 | PV stage 0: the pvlib chain with clear-sky-index downscaling, soiling state, horizon mask, priors from PVGIS and NASA POWER; the prior error table from the ESMAP stations (O5's download) | Matches a hand-computed clear-sky day within 1 percent; quantiles sorted; the ESMAP study reproduces a station day | Part 5.1; R5 4.1 |
| FOR-04 | PV stage 1: capacity and bias fit on clear-sky periods, conformal bands, the intraday blend; the performance ratio and cleaning alert; the skill scoring per horizon class | Runs end to end on twin data and writes every score; the 25 percent skill gate at the 6 to 24 hour class is measured on O12's month and recorded in GATES | Part 5.1, 5.5 |
| FOR-05 | PV stage 2: the xgboost multi-quantile residual model with lead-time-faithful training (live stored forecasts until the archives exist), the gates, the registry | Trains on 6 weeks of data under a minute; coverage within tolerance on O12's month | Part 5.1 |
| FOR-06 | Load stage 0 templates (the onboarding form's site types) and stage 1 equivalent-day persistence with day types and the Pakistan holiday calendar | Baseline rows written daily; persistence checked against PRECON | Part 5.2 |
| FOR-07 | Load stage 2: the direct multi-horizon quantile model with belief-time-safe lags and censoring; the critical-load estimate | Runs end to end on twin data; beats persistence at every horizon class on O12's month (recorded in GATES) | Part 5.2 |
| FOR-08 | Outage schedules: the CCMS feeder scraper, the K-Electric PDF watcher, manual entry, versions, untrusted-text handling, the adherence posterior | Offline fixtures parse; a diff makes a version; adherence updates per observed slot | Part 5.3 |
| FOR-09 | Outage model: Beta-Binomial adherence, the logistic hazard with DISCO priors, the duration survival, P(down), p_start and the remaining duration, 20 sampled paths; re-issue on observed grid loss | Runs end to end on twin data; Brier skill against schedule-only on O12's month (recorded in GATES) | Part 5.3 |
| FOR-10 | Scoring job and `forecast_scores`; the metrics of Part 5.5 by horizon class; skill-regression alerts | Scores written for every belief time | Part 5.5 |
| FOR-11 | Job wiring in the scheduler: nwp_fetch, schedule_fetch, the forecast issue inside the tick (step 2 of Part 2.4) and on events, refits, training, scoring, backfill | Each job runs on its cadence in a scratch run; none runs in tests; no site is issued twice in a quarter | Part 5.4 |
| UI-08 | Forecasts page: PV, load and outage probability with quantile bands against actuals, accuracy to date by horizon class, the stage per forecaster, the data-provider status and fallback share | Renders from twin forecasts | Part 9.3 |
| DOC-04 | EQUATIONS and ASSUMPTIONS rows for the forecasters; THIRD_PARTY_NOTICES for pvlib and the EMHASS functions copied | Reviewed | Part 14 |

Exit check: every forecaster runs end to end on the demo sites every quarter and writes its scores; the real-data gates run once on O12's month and their figures are in GATES; the owner asked to push.

### Stage 5. The optimiser, the battery service and the plan (about 13 days)

| ID | Item | Acceptance | Refs |
|---|---|---|---|
| OPT-01 | The model builder: inputs dataclass, variables, the rows of Part 4.3, the objective of 4.4, sparse matrices, bounds, integrality, named rows, the `relax()` toggle per binary family, the configuration hash and structural cache | Builds the 192-step case in under 50 ms; the row count matches the spec | Part 4.2 to 4.4; R1, R3 |
| OPT-02 | The scipy backend and the highspy backend behind one interface; time limit, gap, the incumbent policy, the numpy re-check; the carried-state mechanism tests of Part 4.3 (an outage end inside the minimum up time, a used-up cap, an empty tank, a hold against a fixing, a start near the horizon end) | Each carried-state case `optimal` without a relaxed retry; `feasible` returned on a forced time limit; the genset case under 30 s on the laptop (10 s on CI when pushed) | Part 4.5 |
| OPT-03 | Genset unit commitment with the carried state, the clipped windows, the per-day run-hour cap, the fuel cap, the parallel flag, the outage load mode, the three `genset_start_control` kinds (auto-start fixed on during outages, SoC threshold as a row, manual as a decision), fuel kinds | Golden off-grid and outage-heavy cases in both load modes and for each start-control kind; brute-force cross-check on a one-day problem | Part 4.3; R3 2 |
| OPT-04 | Availability by adherence, the expected-value reserve rule with p_start pricing and the sigma widening; observed outages from events | On 1,000 twin days: of the outages the forecaster gave p_start at or above 0.2 at least 2 hours ahead, 90 percent are entered with E at or above R; unserved critical energy on those days is zero; on days with no forecast outage the battery still discharges into the peak | Part 4.3, 4.9; R3 3.5 |
| OPT-05 | MDI rows on the site's blocks with the open block's elapsed mean, the upper-quantile duplicate rows, the grossed-up rate and the second-month variable; the MDI marginal-price golden | Golden MDI-bound case; the coefficient equals the bill engine's perturbation | Part 4.3; R2 |
| OPT-06 | The three ICP cases per period and the month-level binary; the ICP marginal-price golden | The three goldens of TAR-03 reproduce; the binary case agrees with the bill engine | Part 4.3; R9 5 |
| OPT-07 | Wear v1 (the throughput price and the calendar holding cost behind a site flag), curtailment, unserved load, the terminal value with the absorption cap, tie-breaks; every soft term in rupees; the PV-shift, TOU and genset marginal-price goldens | Golden cases; no grid charging in the last 6 hours of a B-2 case with no outage forecast; the worked 100 kWh day of R6 section 6.8 reproduced; no 1e6 big-M anywhere (a test greps the builder) | Part 4.3, 4.4; R6 6 |
| OPT-08 | The MPC tick in the scheduler: acquire with per-source staleness, forecast, vectors, solve, publish, score; the plan state machine; `runs` rows | The demo sites re-plan every 15 minutes on a scratch run; a CSV site plans at upload | Part 2.4, 4.8 |
| OPT-15 | Event re-solves (outage, genset, rejection, capabilities, forecast bust, override, tariff version) and overrides as bounds | An outage event re-solves within a minute; a genset hold wins over a fixing | Part 2.4, 4.8 |
| OPT-16 | The site status endpoint, per-tick timings, the last-run and plan endpoints with a versioned schema | Status shows each stage; timings logged | Part 2.4; R2 |
| OPT-09 | Plans, plan steps, cost breakdown, unresolved rows, the explanation vocabulary and its rules | Every golden case's reasons match a hand reading | Part 4.7 |
| OPT-10 | Battery service v1: coulomb count between anchors with the BMS as a trust-weighted prior, the sigma growth model, the trust score formula, usable kWh as R6 5.5, SoH pass-through, the wear coefficient and calendar slope, the anchor-slot request above 15 points of sigma, the seven alerts of R6 5.7 including `derate` | The twin's BMS error model is tracked within 5 points at anchors over 30 days; sigma grows as R6 section 5.3 predicts on a three-cloudy-day fixture; a derate lowers P_dis at the next solve | Part 4.6; R6 5 |
| OPT-11 | Wear v2 (piecewise by depth of discharge, LFP coefficients from BLAST-Lite in `bench/wear/`) | Optional; behind a flag; v1 ships without it | Part 4.3; R3 5.5 |
| OPT-12 | Stochastic scenario block (v2) | Optional; timed in `bench/`; not in v1 | Part 4.1; R3 3.5 |
| VER-06 | The REopt-style reference case and python-microgrid second opinion in `verification/`; the bench harness and `bench/REPORT.md` | Within 1 percent on the no-genset case; report committed | Part 13.1 |
| UI-09 | Plan page, the header and the table view: status wording and objective breakdown, the SlotTable with the reason column, override glyphs and past actuals | Renders the demo plan; the viewer sees no rupee columns | Part 9.3; R8 2.9 |
| UI-20 | Plan page, the Gantt view and the reasons legend; the Slots view (the Mode A table in the vendor's layout with "Share as text", "Mark as applied" with the camera and the "it took" card; the same component UI-15 uses) shows "compiled at Stage 7" until DLV-01; the 375-pixel Playwright project | Renders; the phone project passes | Part 9.3, 9.5 |
| UI-21 | Plan page, overrides, re-solve (rate-limited), the compare drawer against the native replay, versions and diffs | An override changes the next plan | Part 9.3 |
| UI-10 | Battery page: SoC with the band and trust, SoH, throughput, cycles, hours hot and full, wear rupees, alerts | Renders | Part 9.3 |
| UI-11 | Genset page: run hours, starts, litres, load ratio, the fuel-curve fit against observed, the optimiser's start and stop decisions as notifications with their outcome (DLV-07 moves to this stage and lands with it) | Renders; a request's outcome shows | Part 9.3 |
| DOC-05 | EQUATIONS rows for every MILP row with the source note; DESIGN_LOG for the optimiser; THIRD_PARTY_NOTICES for the evcc and EMHASS constructions | Reviewed against R1, R2, R3 | Part 14 |

Exit check: the engine-ready row of Part 1.4; the marginal-price goldens green; PLT-09's optimiser parts re-checked; the owner asked to push.

### Stage 6. Backtest, go/no-go, switchback and reports (about 8 days)

| ID | Item | Acceptance | Refs |
|---|---|---|---|
| VER-07 | Offline backtest: run the optimiser on a recorded period in shadow against both replayed baselines; the per-site value report with realised-forecast and perfect-foresight columns; the go/no-go template against the kill criteria | A 30-day twin backtest produces the report; the same on O12's real month, with its figure in GATES | Part 8.2, 15.3 |
| VER-14 | The real-data check: O12's month through the replay, the forecasters and the backtest, all figures recorded in GATES as the only non-circular evidence before the pilot | The three gates (replay 3 and 5 percent, PV skill, load skill, outage Brier) reported with pass or fail | Part 1.4, 5, 8 |
| VER-08 | Switchback: blocks of 3 to 7 days by a coin flip per fortnight (7-day blocks in tier 2), no SoC reset, the arm applied to the plan (a baseline block plans "native"), the priced carry-over at block boundaries, the post-hoc regression adjustment, the lever split (energy, export, fuel and reserve from the switchback; MDI and ICP from the month's counterfactual, labelled), month-long blocks for phase 3 at MDI or ICP sites, locking, the agreement check (15 percent or Rs 300 a day) on the switchback levers | Calendar tests; the carry-over credit, the adjustment and the agreement statistic computed on the demo sites; the MDI line never attributed to a switchback | Part 8.2 |
| VER-09 | KPI rows of Part 8.3 computed daily; reserve adequacy, fidelity as a day class, control health and BMS trust | All KPIs on the demo sites | Part 8.3 |
| VER-10 | Monthly report: backend-rendered PDF (ReportLab) and CSV per site and per company, the "Preliminary" rule including the FCA condition, the pilot write-up template with the pre-registered gates of Part 15.3 | A report downloads through the browser check and its figures equal the page | Part 8.5 |
| UI-12 | Savings page complete: the ledger by lever against both baselines, counterfactual, switchback blocks with carry-over, the bill comparison, the perfect-foresight bound, the KPIs, the fee card (admin only), the report downloads | Walk in every role | Part 9.3 |
| UI-13 | Pilot set-up page with lettered tabs: A baseline window, B native-mode capture, C switchback calendar (blocks), D success criteria (the gates of 15.3 prefilled) | Walk | Part 9.3, 11.4 |
| UI-14 | Reports page: list, generate, download, schedules | Walk | Part 9.3 |

Exit check: a month-long twin pilot produces a go/no-go report with every KPI; O12's real-month figures in GATES; PLT-09's switchback parts re-checked; the owner asked to push.

### Stage 7. Delivery: Mode A, the contract end to end, Mode B against the twin, alerts (about 10 days)

| ID | Item | Acceptance | Refs |
|---|---|---|---|
| DLV-01 | The TouSlotPlan and the Mode A compiler: merge, rank boundaries by their value against the native replay, drop, quantise, translate per brand including the MDI translation, clamp, the whole-table safety check with two day-tables, the write budget with the 60-minute spacing, the pre-signature twin replay with R6's refusal checks | On each golden case, with the 192-step plan, the compiled table and the native capture all replayed through the twin and priced by the bill engine: the table retains at least 80 percent of the plan's saving against the native replay, never breaches a floor or the export cap, never costs more than the native table; the per-brand loss written to `bench/REPORT.md`; grammar tests per brand | Part 7.1; R6 3.6, 4 |
| DLV-02 | The vendor-layout rendering per brand (Deye programs, Huawei periods, Growatt windows, Sungrow and Voltronic emulation) with the intent in words beside each value | Each layout renders for the demo sites | Part 7.1, 9.3; U3 2.4 |
| DLV-03 | Plan signing (Ed25519 per site), the plan and command messages, the envelope, supersede, validity, the Mode A fallback block without a revert timer | Contract tests 2 and 3 | Part 7.3 |
| DLV-04 | The twin's executor: apply at the right minute, read-back, acks with reason codes, budgets, expiry fallback, link loss, overrides, template change; fault injection; the cloud's escalation rules (two mismatches demote, override needs a decision, the clock-drift ladder of R6 C10) | Contract tests 3 to 7; a plan that fails a refusal check is never signed | Part 7.3, 7.4; R6 4 |
| DLV-05 | Tier 2 flow: the applied record (by a person, method, confirmation from telemetry), the boundary and floor tests, the day classification and the fidelity score; the savings page's gate (a "not applied" day claims nothing) | Fidelity computed on the demo sites; an unapplied day claims no saving | Part 7.6, 8.4; R6 4.6 |
| DLV-06 | Mode B schedule and refresh block; emitted only where capabilities allow; validated against the twin | Contract tests on a Sungrow profile | Part 7.2 |
| DLV-07 | Genset and apply notifications (advisory) through the `notifications` table, delivered in-app, by SMTP email (stdlib), by a per-company signed webhook and by Web Push, with SMS or WhatsApp behind the same sender interface (the provider and its cost a company setting), each with a one-tap Done or Not now reply that writes the outcome; the scoring; auto-start sites get no genset requests; built here because UI-11 shows the outcomes | Notification rows on the manual-start demo site; a reply writes the outcome; none on the auto-start site; an email and a webhook delivered on a scratch run | Part 2.3; U3 3.7 |
| DLV-08 | Alerts and rules: trust low, outage reserve missed, forecast skill regression, data gap, plan failed, read-back mismatch, override, tariff pending, CSV site silent 24 hours; acknowledgement | Alerts open and close on fixtures | Part 9.3 |
| DLV-09 | Fleet staggering and the per-site channel queue; heartbeat liveness and the 5-minute link-loss mark | Scheduler test with 50 twins | Part 7.3 |
| UI-15 | Devices and control page complete: capabilities, the Mode A table in the vendor's layout with "mark as applied", write log, read-back, fallback status, control tier and mode, release and hold (tier 3) | Walk in every role; usable on a phone | Part 9.3 |
| UI-16 | Alerts page (inbox, detail drawer, rule editor with channels, quiet hours and escalation, the categories of Part 9.3) and the header badge | Walk | Part 9.3; R8 2.16 |
| UI-17 | Site settings page (General, Assets, Tariff, Control tier with its confirmation flow and installer sign-off, Notifications, Members) reusing the wizard's panels | Walk; the tier never rises above what the source supports | Part 9.3; R8 2.17 |
| UI-18 | Company settings and account pages: members and roles, the demo-sites switch and clone, report schedules, API keys; profile, password, sessions, notifications | Walk | Part 9.3; R8 2.17 |
| DOC-06 | The contract documented from the schemas; DESIGN_LOG for delivery; the brand provenance log | Reviewed against U3 and R6 | Part 7.3, 14 |

Exit check: the eight contract suites green against the twin; a day of Mode A tables shown and "applied" for each demo brand; the Slots view live; PLT-09's alert parts re-checked; the owner asked to push.

### Stage 8. Hardening, release and pilot readiness (about 7 days)

| ID | Item | Acceptance | Refs |
|---|---|---|---|
| DEP-02 | Performance gates: the 192-step solve (30 s locally, 10 s on CI), a day's telemetry read, the fleet page at 100 sites; the local figures in GATES and the CI figures after the owner's push | Numbers recorded in GATES | Part 13.2 |
| DEP-03 | Security review of the whole: roles on every route, cost and fee leaks (the viewer grep), the scraped-text and CSV paths, headers, rate limits, signing keys, the demo clone | Findings fixed with regression tests | Part 2.8 |
| DEP-04 | The release build (`build_release.py`) on port 8061 | A release runs from its own folder on 8061 | Part 2.7; U2 3.4 |
| DEP-06 | The Dockerfile on `python:3.12-slim-bookworm`, compose (api only; mosquitto, valkey and worker commented in), nginx with the SSE location and 64 MB uploads, certbot notes, `DEPLOY.md`, the CI image job | The image job is proven by the owner's push and reads "unverified: no Docker here" in GATES until then | Part 2.7; R7 10, 11 |
| DEP-07 | The three backup layers (daily in-app copies of both files, nightly off-machine copy, the quarterly restore drill) and `restore_backup.py` | A restore from backup tested on a scratch folder | Part 3.3 |
| DAT-07 | The Parquet export per site-month after 13 months and the 1-minute prune | A synthetic 14-month site exports one file per month and the prune leaves 13 months | Part 3.3; R7 3 |
| DEP-05 | Observability: the rotating log, the error webhook, per-tick timings, the status endpoint, the fallback-share figures | Visible in the founder console's counts | Part 10 |
| PLT-10 | Founder console counts and backups for the EMS (sites, plans, days, companies on); nothing more (the owner's "later") | Walk as founder | Part 10.5 |
| DOC-07 | OWNER_CHECKLIST, HANDOFF_NEXT, TESTER_HANDOVER_PROMPT, PRODUCT_BRIEF complete with the verification table, README of both repositories, CHANGELOG 0.1.0 | Reviewed | Part 10.8 |
| VER-11 | The pilot-ready row of Part 1.4 checked and recorded | All green locally; CI green after the owner's push | Part 1.4 |

Exit check: the pilot-ready table; the owner's checklist handed over; the owner asked to push.

### Later stages (not in v1; listed so the tracker can hold them)

| ID | Item |
|---|---|
| ING-06 | OEM-cloud pull connectors (Solarman, FusionSolar, iSolarCloud, Growatt), read-only, installer-granted keys |
| DLV-10 | MQTT over mutual TLS channel, broker set-up, per-edge certificates, topic ACLs (when Product 2 exists) |
| DLV-11 | The interim per-brand writer on a Raspberry Pi (only on the owner's decision D8) |
| OPS-01 to OPS-06 | Founder console extras: fleet statistics, plan health, usage counters, plans and seats, the error log view, impersonation with audit |
| FOR-12 | Fleet models and the self-hosted GFS second source |
| OPT-13 | Process-pool solves and the PostgreSQL telemetry repository for 1,000 sites |
| OPT-14 | Battery service v2: the equivalent-circuit EKF on filterpy for shunt sites, after a commissioning OCV test procedure exists |
| TAR-11 | KSA tariff pack and the Oman hourly tariff |
| PLT-11 | Supabase sign-in mode live, email sender (invitations and resets by email), authenticator app |
| UI-19 | Installer white-label branding; a mobile view; a fleet map |

---

## Part 13. Verification, gates and the execution protocol

### 13.1 Test layers

| Layer | Where | What it proves | Count target at pilot-ready |
|---|---|---|---|
| Unit and golden | `backend/tests/` | Every rupee figure worked by hand in the test's docstring (bills, savings rows, fuel costs, wear); every engine rule (reserve, genset minimum load, no import in outage) with a test that fails on the old code; one-sentence 422s; the settings bounds | 1,200+ (the lift brings about 200) |
| Walk cases | `backend/tests/walk_cases/` | Every route in every role, including the machine key and the founder; a viewer never sees a fuel price or a fee; the demo walk in each role | One case per route per role |
| Contract | `backend/tests/contract/` | The eight suites of Part 7.3 against the twin | All green |
| Replay invariants | `backend/tests/test_invariants.py` | 1,000 twin days across the three demo sites and the fixture archetypes: no hard-row breach, every soft breach priced and reported | 0 breaches |
| Verification | `verification/` (own requirements, never imported) | The no-genset case against a REopt-style annual run at fixed sizes within 1 percent; the genset case against brute-force enumeration of a one-day problem; the replay against PySAM's battery model; python-microgrid as a second opinion; the fuel-curve arithmetic; `verification/REPORT.md` regenerated with `--update` | Report committed |
| Bench | `bench/` | The 192-step genset case under 10 s (CI Linux) and 30 s (Windows); the ICP chain variant; the scenario block (v2) timings; the BLAST-Lite wear calibration; backtests on recorded sites | Numbers in `bench/REPORT.md` |
| Frontend | `frontend/src/**/*.test.tsx`, Playwright | Routing union, interceptor, tabs hook, formatters, one page test per feature, smoke | 300+ unit, 6+ e2e |
| Browser check | `scripts/e2e/` in the web repository | Every page in every role on the demo data, PDF downloads caught, counts in `GATES.md` | 0 failed |
| Deploy docs | `test_deploy_docs.py`, `test_hygiene.py` | Every setting in the example file; no secrets; the troubleshooting rows pinned | Green |

### 13.2 Gates

- Per unit of work: the unit's own tests and the affected test files; `tsc --noEmit` when the web app changed.
- Per stage: the four suites (backend with `-n auto`, frontend typecheck and tests, verification, the browser check) plus the stage's exit check (Part 12 names it); the counts recorded in `BUILD_PROGRESS.md` and `GATES.md`.
- Before the pilot: the pilot-ready table of Part 1.4; the owner's checklist of Part 15.
- CI runs when the owner pushes `v1-build`, asked for at every stage exit (owner task O10); until then the stage gate is local. The workflow files pass `actionlint` and each CI step's command passes locally on Windows before a stage is called done; the Docker image and the 10-second Linux solve figure are recorded in `GATES.md` from the owner's push and read "unverified: no Docker here" until then. A red CI after a push is the first item of the next session.

### 13.3 Execution protocol for the building agent (Opus in Claude Code)

The protocol follows what worked on Retailytics and Panelytics (the owner's memory notes), with the owner's current limits on parallel agents.

1. **Read first, every session**: `docs/BUILD_HANDOFF.md`, the session log at the top of `docs/BUILD_PROGRESS.md`, `git log --oneline -15` on `v1-build`, then run the suites; fix any failure before new work.
2. **Units**: a unit is one or a few tightly coupled items of Part 12 (one commit naming their IDs). For each unit: a short plan (files, data model change, tests) at the top of the stage's entry in the session log; the test first where the item is a rule or a rupee figure; backend, API and page together; the docs rows (EQUATIONS, ASSUMPTIONS, DESIGN_LOG, PRODUCT_BRIEF, API_CONTRACT) where touched; the tracker ticked (`[x]` only when code, tests and docs are done, `[~]` if partly).
3. **Agents**: at most two or three subagents at once (the owner's limit after usage-limit stops), each saving progress to files after every unit of work; a reviewer told to refute the unit's claims (max effort) and a fixer only when the review finds a medium or high; low findings batched into one polish pass after the stage. Never resume a pooled workflow; rerun one step as one agent. Lane worktrees and notes live under the project (`Videos/_build/gridlytics/`, git-ignored), never under the temp scratchpad.
4. **Git**: a baseline commit on `main` at Step 0, then branch `v1-build`; commit at the end of each finished unit; never merge, never push, never add a remote; the owner opens pull requests. The web app commits first, then the backend, so the backend's sibling tests run against the committed pages.
5. **Data**: tests on a temp data folder; never delete or reseed `data/` once the owner has used the app on it (before that, and always on `--scratch`, the seed may be re-run); a copy before any schema change; the launcher's `--scratch` for every manual check.
6. **Stop and ask the owner** when: an item needs an owner task (a pilot site, a bill, a weather key, the product name, a liability clause); a change would move a verified plan or saving beyond what the change explains; a change would delete or overwrite saved data; anything outside the two repositories would be touched.
7. **End of session**: a line in the session log (date, items finished, anything half done, the next item) and a commit.
8. **Resume after a usage-limit stop**: `git status`; if the last log line or a WIP commit names a RESUMING unit, that unit's red tests are finished, never reverted; other failures are fixed first; intact uncommitted work is kept and the unit resumed; partial files are committed as WIP (the message starts with `WIP` and names the item IDs) or removed, with a RESUMING note in the unit's plan.

### 13.4 Definition of done, per item

Code with its tests green; a hand-worked golden where money or energy is computed; the API contract row and the page where a screen is involved; the equation row with its status letter and `[verify]` on anything recalled; the design-log paragraph; the tracker row ticked; the demo site still walks in every role.

---

## Part 14. Open-source reuse and licence rules

The answer to "start from copies of code available online?" is: take formulations, structures and a short list of small functions from permissive projects, write the Pakistan layer and the product new, and never let copyleft or non-commercial code near the repositories. R1 to R6 verified every licence text on 6 November 2026.

### 14.1 What is taken, from where, and the obligation

| Source | Licence | What is taken | Obligation and where it is recorded |
|---|---|---|---|
| evcc-io/optimizer (commit 1bb387d) | MIT, "Copyright (c) 2025 andig" | Structure of the model-builder class; the direction gate with tight big-M; the SoC convention and soft band; the demand-rate block as the MDI term; the slack-plus-penalty-plus-flag pattern; result semantics with the integrality and row re-check; the clean objective recomputed from the schedule; the per-request JSON log line and replayable slow-request dump; the golden-case harness with strict mode and invariants; the shape of an explanation module | Keep the copyright line and permission notice: `THIRD_PARTY_NOTICES.md` names the commit and the line ranges; a one-line header in each derived file; the evcc name never in marketing |
| EMHASS (commit 4efdaa2, v0.18.8) | MIT, "Copyright (c) 2021-2026 David HERNANDEZ TORRES" | Patterns: define-once and parameter update, structural cache, warm start, the never-infeasible policy, the demand-charge epigraph with interval averaging, the unit-commitment block of deferrable loads generalised to the genset, the per-step limit arrays as the outage hook, the versioned plan endpoint. Functions worth copying nearly verbatim (R2 section 8.2): the stress-cost segments, the power-limit array preparation, the incumbent and relaxed-retry checks, the min-on and min-off block, the naive load persistence with DST handling, the lag list, the forecast metrics, the date features, the adaptive-conformal blend tuning, the walk-forward calibration skeleton, the plan store | The same notice rule; the EMHASS name never in the product |
| REopt.jl v1.0.1 (commit 35abb019) | Apache-2.0; NOTICE "Copyright 2023 Alliance for Sustainable Energy, LLC"; headers "REopt(R), Copyright (c) Alliance for Energy Innovation, LLC" | Mathematics only, re-derived in Python with corrections: the two-point fuel curve (the intercept multiplied by the rated size), on-off and turndown big-M, the deterministic outage step set, the stochastic outage block (v2), the demand-charge constraints with the previous-peak lower bound, the no-simultaneous-import-export binary, the degradation recursion's form | Re-implementation from the written formulation arguably triggers no clause; attribute anyway in `THIRD_PARTY_NOTICES.md`; the REopt name, the registered mark and NLR or NREL branding never in the product, UI, marketing or domains; never vendor the clone (it carries unreviewed solver and SAM binaries) |
| FlexMeasures (commit be16d433) and timely-beliefs | Apache-2.0 (NOTICE "Copyright 2018-2026 Seita Energy Flexibility"); timely-beliefs MIT "Copyright (c) 2018 Seita BV" | The belief-timestamped time-series idea (own five-column table), the job-cache and job-map idea, the API shape (202 and a job URL), the commitment and deviation-price objective idea, the unresolved-constraints report | Design only; if the job-cache code is copied, keep the Apache notice and mark modifications |
| Akkudoktor EOS (commit f804d480) | Apache-2.0, "Copyright 2024 Dr. Andreas Schmitz" | The prediction-provider shape, the plan with validity and active-instruction semantics, LCOS and terminal-value settings | Design only, or copy with notice |
| OpenEMS Edge and Backend (commit ab52862a) | EPL-2.0 | Design only: the controller priority chain, the TOU state vocabulary with degradation to plainer states, the plan state machine, the reserve hysteresis ladder, the backend-unreachable behaviour | No code copied; modified EPL files would stay EPL |
| OpenEMS UI | AGPL-3.0 | Nothing; never opened | - |
| Victron dbus-systemcalc-py and dbus_generator | MIT | The load-shedding window rules and the genset start-stop condition stack, re-implemented in the twin and the fallback rules | Notice kept |
| kellerza/sunsynk, wills106 solax-modbus (Growatt), sunspec/models | Apache-2.0 | Register facts for the Deye, Growatt and SunSpec grammars in the brand data files | Attribution and a provenance log per data file; converted files keep the notice |
| davidrapan/ha-solarman, mkaiser Sungrow, jblance/mpp-solar | MIT | Register facts for Deye, Sungrow and Voltronic grammars | Notice kept |
| wlcrs/huawei-solar-lib | AGPL-3.0 | Nothing copied. The Huawei grammar is written from Huawei's published Modbus interface definitions as the primary source, with the AGPL library read only as a cross-check of register facts (recorded as "read for facts, nothing copied" in the provenance log), and every value carries `bench_confirmed: false` until a bench capture exists | Facts are not copyrightable, files are: no file or data from it enters the product |
| pvlib | BSD-3 | Runtime dependency | Dependency notice |
| xgboost, scikit-learn, holidays, scipy, HiGHS (through scipy or highspy), FastAPI and the web stack | Apache-2.0, BSD-3, MIT | Runtime dependencies | The `licences` CI job checks the allow-list |
| OpenSTEF | MPL-2.0 | The `available_at` discipline as an idea; the library may be imported unmodified in `bench/` only | A modified MPL file would have to be published; so none is modified |
| Quartz solar forecast | MIT | Benchmark in its own venv | Notice if ever imported |
| python-microgrid | LGPL-3.0 | Second opinion in `verification/`, unmodified, never imported by the product | Dynamic use in test tooling only |
| NREL PySAM, BLAST-Lite | BSD-3 ("Copyright (c) 2023, Alliance for Energy Innovation, LLC" plus the DOE notice) | Replay check and wear calibration in `verification/` and `bench/wear/`, with their own notices file; BLAST-Lite vendors `rainflow` | Notice |
| rainflow 3.2 | MIT, "Copyright (c) 2018 Piotr Janiszewski" | Cycle counting in the wear calibration and the battery page's equivalent cycles | Notice (fatpack, ISC, is the alternative; ffpack is GPL-3 and excluded) |
| filterpy | MIT, "Copyright (c) 2015 Roger R. Labbe Jr" | The v2 battery EKF | Notice |
| PyBaMM, PyBatterySE | BSD-3 | Bench only, never imported by the API (PyBaMM's only LFP parameter set is flagged unvalidated by its maintainers) | Notice in `bench/wear/` |
| CityLearn | MIT | Benchmark | Notice |
| Predbat | Personal, non-commercial, UK only | Nothing. Its public ideas (plan tables, PV10 and PV90 weighting, a cycle cost) informed the UX study | - |
| OpenLEMS, emoncms, OpenRemote, energy-py-linear (GPL-3.0), MicroGridsPy (EUPL-1.2) | AGPL, GPL, EUPL | Nothing; learn only | - |
| evcc-io/evcc's 89 sponsor-only files | Proprietary | Nothing | - |
| Solarlytics backend and web app (OnyxLeaf's own) | Private | The tenancy package, the sign-in pages, the launcher, the browser check, the jobs table, conventions | Recorded in `DESIGN_LOG.md` by file and commit; the founder token format's Boxlytics origin named for the owner to confirm |
| Boxlytics (another party's repository) | Private | Patterns only, rewritten; no file copied | The hands-off rule stays |

### 14.2 Data terms

Open-Meteo CC BY 4.0 with attribution on screen and a commercial subscription for a paid SaaS; PVGIS free with its acknowledgment; NASA POWER citation; ESMAP CC BY 4.0; PRECON CC0; the Sungrow protocol PDF is marked not for redistribution, so only derived tables ship; DISCO and K-Electric schedules are public under a Pakistan Information Commission order and are parsed as untrusted text.

### 14.3 Rules the building agent follows

1. Clean-room by default: write new code. Copy only what the table above allows, with its notice.
2. Keep `docs/THIRD_PARTY_NOTICES.md` current: one entry per source with licence, commit, what was taken, where it lives.
3. A one-line header in every file that derives from a notice-bearing source.
4. The `licences` gate runs in both repositories (pip-licenses and an npm licence checker) against checked-in allow and deny lists with named exceptions, because the runtime graph itself has edge cases: `certifi` (MPL-2.0, used unmodified, allowed), `pillow` (HPND, allowed), the CUDA wheels xgboost pulls on Linux (proprietary, avoided by pinning `xgboost-cpu`), anything else outside MIT, BSD, Apache-2.0, ISC, PSF or an EPL-or-BSD dual licence taken under BSD fails. The product serves `/about/licences` with every notice the distributed bundles require (ECharts' Apache NOTICE among them) and the data attributions (Open-Meteo CC BY 4.0, PVGIS, NASA POWER). No endorsement is implied by NREL, NLR, BLAST-Lite or PySAM. Named exclusions from R7: dramatiq and fpdf2 (LGPL), psycopg 3 (LGPL; asyncpg instead), python-mip and cbcbox (EPL), Redis 8 (RSAL, SSPL or AGPL; Valkey BSD instead), EMQX 5.9 and later (BSL; Mosquitto run unmodified as a service instead), WeasyPrint (Pango on Windows and antivirus flags; ReportLab instead). TimescaleDB Community is under the Timescale licence: self-hosting for the product's own use is allowed, offering the database itself as a service is not.
5. Never clone an AGPL, GPL or non-commercial repository into the product's folders; study them, if at all, in a scratch folder and record only facts.
6. Brand register data files carry a provenance block: source repository, commit, licence, date, and whether a bench capture confirmed the value.
7. Product names and marketing never use REopt, EMHASS, evcc, FlexMeasures, OpenEMS or Victron.

---

## Part 15. Risks, kill criteria, owner tasks, the pilot and the road after v1

### 15.1 Risks and what the plan does about them

| Risk | Evidence | What the plan does | Kill or reshape rule |
|---|---|---|---|
| The increment over the inverter's own mode is too small to bill for | A hardware test found rule, decision-tree and MPC controllers within 0.6 percent; MPC's edge over rules shrank 69 percent when re-scored at 1 minute; self-use already captures most PV shift | Target outage and genset sites first; the offline backtest gate (WP7) before any field control; 1-minute accounting throughout; the perfect-foresight bound reported so the forecast's share of the gap is visible | Kill the pure-optimisation pitch if verified savings are under 1.5 times the fee on 2 of 3 sites after 8 weeks of switchback; reshape to outage reserve, genset reduction and fleet reporting, which static modes cannot do |
| Tariff and policy churn | The uniform schedule ends December 2026; the ICP is under review; the buyback rate was under PM-ordered review from 22 October 2026 | Tariffs as effective-dated versions never edited in place; a change re-vectors the next tick; the value model re-run quarterly | - |
| Wrong battery SoC | 1 mV of LFP plateau error is about 5 percent SoC; a JK BMS read 85 percent on an empty pack | Two SoC numbers with sigma and trust in the contract; the reserve widened by 2 sigma; an alert when they disagree by 10 points; a shunt where Product 2 fits one | - |
| Flash wear and inverter damage from writes | SMA warns cyclic parameter changes destroy flash; about 100,000 EEPROM cycles | Mode A daily tables with a write budget; volatile registers for Mode B; read-back; the edge enforces the budget | - |
| Unreliable outage schedules | Faults and fuel shocks are unscheduled; DISCO pages have no API | A learned per-feeder hazard on top of the schedule; the reserve margin; scraped schedules treated as untrusted and versioned; a manual entry path | - |
| Forecast error erases the value | NREL found MPC fell to manual-dispatch value with real day-ahead forecasts | Quantile forecasts; robust peak-shaving decisions on the upper load quantile; re-solves every 15 minutes and on events; the regret metric in rupees | - |
| Write access reality | Many installed inverters expose poor or no remote control | Tiers 1 and 2 deliver value without writes; Mode A tables in the vendor's layout | If under half of the target fleet accepts closed-loop control, the advisory tier is the product |
| Product 2 timing | Writes to the first brands about month 6 to 10 after Product 2 starts | The contract, the twin and the HTTPS path make the product independent of Product 2's date | - |
| NeoTAQ adds forecasting; Enerlytics builds or declines; free OEM features | NeoTAQ plans edge AI and fleet forecasting with 50+ sites; Huawei, Victron and Tesla bundle scheduling free | Sell to mixed-brand C&I with gensets; sell proven savings; keep an own data path; the Enerlytics conversation is an owner task | Decide partner or compete within 60 days |
| Warranty and liability | Third-party control may conflict with OEM terms | Installer terms and a certified-control brand list; automatic fallback to native settings; the control tier asserted by the edge; "Preliminary" on reports | - |
| Savings attribution disputes | Counterfactuals are contestable | The pre-registered protocol, every input kept with its belief time, the switchback arm on demand, CSV exports with tariff and plan ids | - |
| Licence contamination | Predbat, GPL and AGPL code within reach; the Huawei library is AGPL | Part 14's rules; the licence CI job; provenance blocks on brand data | - |
| Usage-limit stops during the build | Two long sessions were cut off on 5 to 6 November 2026 | Two or three agents at once; progress saved to files; lane work outside the temp folder; the resume protocol | - |
| Cybersecurity | SunSpec requires IP-based Modbus to be secured; OEM cloud credentials are a known incident class | Outbound-only edge links; signed plans; no grid-protection writes; installer-granted OEM keys only; scraped text never executed | - |

### 15.2 Owner tasks (no code can do these)

| # | Task | Needed by |
|---|---|---|
| O1 | Decide D1 to D17 (or accept the defaults) and the product name's trademark knockout check | Step 0 |
| O2 | Three pilot sites signed by the Stage 1 exit (two inverter brands, at least two with a controllable genset start, qualified by their own outage or genset hours as Part 1.2), through GFE, Enercon or Enerlytics, with a site agreement carrying the control tier, the write scope and O9's liability wording; their data collection starts at the Stage 2 exit so that phase 0 and the shadow phase run during the build | Stage 1 exit (signed), Stage 2 exit (collecting) |
| O3 | Each signed site's last 12 bills per connection, tariff category, sanctioned load, MDI history, billing regime and contract expiry, the outage unit code, the STRN and ATL status | Stage 1 start, as the sites sign |
| O4 | The inverter brands, firmware and the installer's native settings per site (by read-back or a photograph of the vendor app); genset rating and a fuel log or tank reading | Stage 2 |
| O5 | An Open-Meteo paid plan (Standard for live runs; Professional for the archives, ensemble and air quality) before Stage 4, since the free tier is non-commercial and excludes the archives; the manual download of the ESMAP Pakistan station data; a diesel price source (OGRA) | Stage 4 |
| O6 | The decision on an interim per-brand writer (D8) and on genset commands (D10) | Stage 7 |
| O7 | The Enerlytics, GFE and Enercon conversations (data API, sites, channel terms); the NeoTAQ question | Throughout |
| O8 | The fee to test (Part 15.4), fixed and locked in the pilot set-up before phase 2 so the 3-times gate is pre-registered against a known number; the VoLL figure per site; written price feedback from five site owners before phase 2 (the report's WP11) | Before the switchback (phase 2) |
| O9 | A lawyer's wording for advisory and closed-loop control, and for savings claims; needed in O2's site agreement | Before O2 |
| O13 | Tariff upkeep from January 2027 through the pilot: enter and approve every NEPRA notification (the CY2027 schedule and its February revision, the quarterly QTA, the monthly FCA, the NAEPP, the ICP review outcome) within five working days, as TAR-09 proposes them | Ongoing from Stage 1 |
| O10 | A GitHub repository pair and the read-only tokens for CI; a push of `v1-build` at every stage exit so CI runs; a Linux host for the API | Step 0, every stage exit, deployment |
| O11 | A read-only clone of `saad-tahir/solarlytics-frontend` (`main`) at `C:\Users\SaadTahir\Videos\solarlytics-frontend`: the web app's sign-in pages, launcher and browser-check runner are lifted from it and the backup folder does not hold it | Step 0 (PLT-07) |
| O12 | One month of real 1-minute inverter data from a partner site (any brand, PV plus battery; a genset if possible) with that site's bill and the installer's native settings, so the replay, the forecasters and the backtest get one non-circular check before the pilot | Stage 3 (VER-02), Stage 4, Stage 6 |

### 15.3 The pilot plan (the report's protocol, kept)

| Phase | Duration | What happens | Product state |
|---|---|---|---|
| 0 Observe | 4 weeks | The inverter's own mode; 1-minute data collected; the baseline replay validated on real data (daily import within 3 percent, throughput within 5 percent on 90 percent of days) and the forecast gates of Part 5.5 measured; the tuned baseline fitted | Tier 1; CSV or HTTPS ingest |
| 1 Shadow | 2 weeks | The optimiser plans but nothing is applied; its plan compared with what the inverter did; the counterfactual saving reported | Tier 1 |
| 2 Switchback | 8 weeks minimum | Blocks of 3 to 7 days (7 in tier 2: one table change per switch) assigned at random within fortnights, stratified by scheduled-outage days, no SoC reset, carry-over priced at block boundaries (Part 8.2); at least one outage-heavy month | Tier 2 (Mode A tables applied by the installer) or tier 3 where a writer exists |
| 3 Full control | 4 weeks | The optimiser daily; stability and fallback checked | Tier 2 or 3 |

Pre-registered gates (the reports' gates, written into the pilot template before the pilot starts, on the bearable-cost basis of Part 8.2). The statistic is the lower 80 percent confidence bound of the daily verified increment over the tuned baseline, times 30, against the fee locked in O8. Success: at 2 of 3 sites that statistic is at least 3 times the fee, with zero unserved critical load caused by the optimiser and zero writes outside the whitelist; at sites with a controllable genset start the run-hours fall at least 15 percent against the tuned rule; at least 90 percent of forecastable outages are entered with the battery at or above its reserve; equivalent full cycles per day no more than 1.1 times the tuned baseline's; the counterfactual and the switchback agree within 15 percent of the switchback saving or Rs 300 a day on the switchback levers; the MDI and ICP lines shown on two real monthly bills where they apply. Kill: the statistic below 1.5 times the fee at 2 of 3 sites after 8 weeks; or the two most common pilot brands refuse slot or setpoint writes. Between 1.5 and 3 times, extend once by 4 weeks. Reshape toward genset-only sites if grid-tied increments fall below 2 percent of the site's monthly energy charges (energy plus adders, ex taxes) while genset sites clear the bar. Three sites start; two may finish.

### 15.4 Pricing hypotheses to test (from the reports)

C&I: the reports' levers give about Rs 1,000 to 2,000 a day per 100 kWh of battery at a qualified site, so the fee is sized to the battery: Rs 100 to 200 per usable kWh per month (Rs 10,000 to 20,000 per 100 kWh), capped at one third of the verified ex-tax increment, or a gain share of 20 to 30 percent of the same figure in year one; the report's Rs 15,000 to 50,000 per site rested on the battery's gross value, not the optimiser's increment. Remote and telecom: Rs 3,000 to 8,000 per site per month at fleet scale. Installer white label: per-site wholesale at 50 to 60 percent of list. The product's savings page is the pricing evidence; the fee and the share are company settings hidden from viewers; five owners' written price feedback before the switchback (O8).

### 15.5 After v1

| Item | Why later |
|---|---|
| Product 2 integration: MQTT over mutual TLS, Mode B in the field, the edge's 1 Hz SoC estimate, installer sessions | Product 2 does not exist; the contract and the twin are ready for it |
| Stochastic MPC: the scenario block over outage paths (OPT-12), quantile-robust plans | Only once the deterministic model's increment is measured and forecast uncertainty is shown to bind |
| Wear v2: the piecewise cost by depth of discharge with LFP coefficients (OPT-11) | Needs real SoC traces to calibrate |
| Fleet forecasting models, the self-hosted GFS second source | Needs 20+ sites in a region |
| The founder console beyond the lift: fleet statistics, plan health, usage counters, plans and seats | The owner asked for core pages first |
| Telecom tower fleets; installer white-label branding; a mobile view | A second market and channel |
| KSA pack: flat tariffs, diesel-only sites, Oman's hourly tariff; SASO is Product 2's concern | The report's year-two expansion |
| Supabase sign-in mode, an email sender for resets, an authenticator app | The lifted module already has the mode; the project does not exist yet |
| Urdu and Arabic | Deferred by the owner |
| Payments and plans | Not needed for pilots |

---

## Appendix A. The research behind this plan, the glossary and the sources

### A.1 Research notes (6 November 2026), in `plan-work/research/`

| Note | File | What it settles |
|---|---|---|
| U1 | `U1-boxlytics-frontend.md` | The Boxlytics web app's stack, routes, auth, admin and founder features, patterns to mirror, faults to leave, the recommended frontend skeleton with pins verified on npm, the lettered-tab structure |
| U2 | `U2-backends.md` | The Solarlytics backend's sign-in, tenancy and founder-console module (file list, coupling, the lift recipe), the owner's conventions, boxlytics-backend's patterns, the backend skeleton, document templates |
| U3 | `U3-product2-interface.md` | The telemetry point table, the abstract primitives and per-brand mappings, the Mode A grammars, the safety contract, control tiers, battery-state ownership, the ingest paths, the twin, the ems-edge/1.0 contract with examples, Product 2's timing |
| R1 | `R1-evcc-optimizer.md`, `R1-target-milp-prototype.py` | evcc-io/optimizer's formulation, service and tests; fork versus re-implement; the prototype of the target model on scipy's HiGHS with timings |
| R2 | `R2-emhass.md` | EMHASS's CVXPY model, MPC handling, forecasters, UI; what to lift and the functions worth copying under MIT |
| R3 | `R3-reopt-formulations.md` | REopt.jl's genset, outage, degradation, tariff and MPC mathematics written out for a Python port, with three defects found and the Apache and trademark notes |
| R4 | `R4-architecture-refs.md` | FlexMeasures, Akkudoktor EOS and OpenEMS: the data model, jobs, API, plan state machine, reserve ladder and offline behaviour to adopt (A1 to A14) and to reject (R1 to R10) |
| R5 | `R5-forecasting.md` | Libraries, data providers and terms, Pakistani datasets and schedule sources, the staged pipelines, cadence, metrics, storage, fallback |
| R6 | `R6-executor-battery.md` | Victron's rule logic, the per-brand TOU registers, the TouSlotPlan and compiler rules, the fallback and safety rules, the battery-state service and the wear-cost derivation |
| R7 | `R7-platform-stack.md` | Decision tables for storage, ingestion, jobs, solver, real-time updates, charting, PDFs, auth and deployment; the MILP benchmark |
| R8 | `R8-ux-pages.md` | The UX study of the closest products and the page and tab inventory |
| R9 | `R9-pakistan-tariffs.md` | The tariff data model, the regimes, adders and taxes, the bill-rebuild algorithm, verified numbers and the uncertainties to confirm from bills |
| - | `_journal_results.md` | The structured summaries of the first eight tracks |
| - | `bench/milp_bench.py`, `bench/milp_bench_variants.py` | The 192-step benchmark and its variants |

### A.2 Glossary

| Term | Meaning |
|---|---|
| TOU | Time-of-use tariff: peak and off-peak energy rates by window and season |
| ICP | Incremental Consumption Package: a flat rate on industrial and agricultural units above a monthly baseline, from December 2025 for three years |
| MDI | Maximum demand indicator: the billing month's peak demand over the DISCO's interval, which sets fixed charges where it exceeds the basis floor |
| Net billing | The 2026 prosumer regime: imports and exports valued separately, exports at the published rate |
| Legacy net metering | The older regime: unit-for-unit netting with a banked credit, kept for existing contracts |
| QTA, FPA | Quarterly tariff adjustment and monthly fuel price adjustment, applied per unit |
| DISCO | A distribution company (IESCO, LESCO, GEPCO, FESCO, MEPCO, PESCO, HESCO, SEPCO, QESCO, TESCO) or K-Electric |
| Feeder | The 11 kV line a site hangs from; load shedding is scheduled per feeder |
| Mode A, Mode B | A daily inverter TOU-slot table; a 15-minute setpoint schedule with heartbeat and revert |
| Control tier | monitor, advisory writes, closed loop; asserted by the data source |
| Belief time | When a forecast or plan was issued; every row keeps it so replay scores what was known then |
| Counterfactual | The replay of the inverter's own mode on the realised day, priced the same way as the executed plan |
| Switchback | Days randomly assigned to baseline or optimiser, both reset to a common SoC at 06:00 |
| Reserve | The stored energy held for the next forecast outage, derived from critical load and expected duration |
| VoLL | The site's value of lost load, rupees per kWh of unserved critical load |
| Twin | The simulated edge and plant that speak the contract |
| Native settings | The inverter's own configuration captured at onboarding; the baseline and the fallback |

### A.3 Sources

The reports' source lists (the guideline's "Sources" and the report's "Sources") hold the market, tariff, policy, player and literature references with dates. The research notes hold the repository commits, licence texts, registry versions and provider terms read on 6 November 2026, each marked verified or UNVERIFIED. This plan quotes numbers only from those two sets.
