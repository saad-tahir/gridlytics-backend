# START HERE: hand-off for whoever builds Gridlytics (OnyxLeaf Product 1)

> **Update, 7 November 2026: the build is done.** Both repositories exist and run end to end:
> - `C:\Users\SaadTahir\Videos\gridlytics-backend`, branch `v1-build`
> - `C:\Users\SaadTahir\Videos\gridlytics-frontend`, branch `v1-build`
>
> A developer continuing the work should read **`gridlytics-backend\HANDOFF.md`** first. It covers setup, running, tests, rules, the open work in
> priority order and the traps met, and assumes no other context. This file below is the planning-stage handoff, kept for the record. Where it says
> "nothing has been built yet", that is out of date.

Written 6 November 2026. Read this file first. It is for a person or an AI agent who has no context: it says what the product is, which files make up the package, in what order to read them, what to set up, how to work, and how to resume when a session is cut off.

## 1. What this is

OnyxLeaf (Saad, Pakistan) is building a SaaS product: a TOU-aware, outage-aware, genset-aware energy management and dispatch optimiser for Pakistani commercial and industrial sites that run solar PV, an LFP battery, an unreliable grid with load shedding and a diesel genset. Every 15 minutes the cloud forecasts PV, load and outage probability, prices the next 48 hours with the real Pakistani tariff (TOU windows, net billing, the ICP flat incremental price, MDI fixed charges, diesel), solves a mixed-integer plan, delivers it first as a daily inverter TOU-slot table (applied by an installer, later by an edge device), and proves the saving against the inverter's own mode at 1-minute resolution. Working name: **Gridlytics** (decision D1 may rename it). Two repositories will be built, a Python FastAPI backend and a React web app, in the shape of the owner's existing products (Boxlytics, Solarlytics).

The plan is finished and reviewed. Nothing has been built yet. The first build step is Stage 0 of the plan's Part 12.

## 2. The files to share, and what each one is

Share the whole folder `C:\Users\SaadTahir\Videos\solarytics\` (about 1.5 MB of text; zip it). It contains everything below. Nothing outside it is needed except the two reference repositories in section 3.

| File or folder | What it is | Who needs it |
|---|---|---|
| `START_HERE.md` | This file | Everyone, first |
| `EMS_BUILD_PLAN.md` | **The master plan** (16 parts, about 1,350 lines): decisions, product, architecture, data model, optimiser, forecasters, tariffs, delivery, verification, pages, backend and frontend skeletons, build order with 136 items and acceptance, verification protocol, licences, risks, pilot. Wins every conflict | Everyone; the builder reads it in full |
| `EMS_BUILD_HANDOFF.md` | The rules, environment, protocol, decisions and paste-ready prompts for the building agent | The builder, every session |
| `EMS_BUILD_PROGRESS.md` | The tracker: one row per item, a session log at the top. The builder ticks it; it is how the next session knows where to resume | The builder, every session |
| `Perfect-EMS_TOU_Dispatch_Deep_Review.md` | The owner's guideline research report on Product 1 (market, players, open source, architecture, pilot) | Context; the plan is the specification |
| `Product 1 - TOU-Aware Energy Management and Dispatch.md` | The owner's supporting report (tariff tables, value streams, 22-repo comparison, formulation sketch, work packages) | Context |
| `Product 2 - Vendor-Agnostic Inverter and Battery Controller.md`, `Perfect-Vendor_Agnostic_Inverter_Controller_Deep_Review.md` | The owner's reports on Product 2, the separate edge controller the plan treats as a supplied interface | Context for the edge contract |
| `plan-work/research/` (12 notes) | The research behind the plan, cited by part and item: U1 Boxlytics frontend audit, U2 the two backends and the sign-in module to lift, U3 the Product 2 contract, R1 evcc-optimizer, R2 EMHASS, R3 REopt formulations, R4 FlexMeasures/EOS/OpenEMS patterns, R5 forecasting, R6 executor and battery, R7 platform stack and the MILP benchmark, R8 UX pages, R9 Pakistan tariffs (verified against NEPRA primary sources) | The builder reads the note an item cites |
| `plan-work/review/` | The adversarial reviews of the plan: `engine-lens.md` and `build-lens.md` (done, all findings applied); `market-lens.md` and `ux-licence-security-lens.md` (see section 6 for their state) | The builder, when an item cites a finding |
| `plan-work/bench/` | The 192-step MILP benchmark scripts (scipy HiGHS); run them to reproduce the 2 to 6 s figure | The builder at Stage 5 |
| `plan-work/draft/`, `assemble.py`, `make_progress.py` | The plan's source parts and the scripts that assemble the three deliverables. Edit the drafts, never the assembled files, then run `assemble.py` | Whoever changes the plan |
| `plan-work/context/` | Copies of the owner's working notes that the plan relies on: git rules (never merge, never push; the owner opens pull requests), the agent limit, the temp-folder wipe, no console windows, the Solarlytics set-up and decisions, the Boxlytics hands-off rule | An outside developer or agent who cannot read the owner's memory folder |
| `plan-work/STATUS.md` | The resume note of the planning run: what is done, what is not, how to rerun the unfinished reviews | Whoever continues the planning or the reviews |

## 3. The two reference repositories (read-only; the owner provides them)

| Repository | Why | Where |
|---|---|---|
| `solarlytics-backend` at commit `c853130` (GitHub saad-tahir/solarlytics-backend, private) | The sign-in, tenancy and founder-console module is lifted from it into the new backend (plan Part 10.4, note U2 section 1.7). It is OnyxLeaf's own code | `C:\Users\SaadTahir\Videos\solarlytics-backend` on the owner's machine; an outside developer needs read access to the GitHub repository |
| `solarlytics-frontend` (GitHub saad-tahir/solarlytics-frontend, private) | The web app's sign-in pages, launcher and browser-check runner are lifted from it (plan Part 11.3). It is not on the owner's machine: owner task O11 is to clone it to `C:\Users\SaadTahir\Videos\solarlytics-frontend` | GitHub |

`boxlytics` and `boxlytics-backend` are audit-only references (patterns, no code); they belong to another party and are never changed. The plan's research notes already describe what matters in them, so an outside developer does not need them.

## 4. Reading order

1. This file.
2. `EMS_BUILD_HANDOFF.md` (20 minutes): the rules and the environment.
3. `EMS_BUILD_PLAN.md` Part 0 (the decisions and their defaults) and Part 1 (what the product is), then Parts 2, 3, 12 and 13. Parts 4 to 11 and 14 to 15 are read when their stage starts.
4. `EMS_BUILD_PROGRESS.md`: the session log and the Stage 0 rows.
5. `plan-work/context/git-workflow-rules.md` and `few-agents-at-once.md`.
6. The research note an item cites, when the item starts.

## 5. How to work

- **The owner decides first:** Part 0's decisions D1 to D17 (defaults are assumed if the owner says nothing) and owner tasks O1 (the name), O10 (the GitHub repository pair) and O11 (the `solarlytics-frontend` clone) before Step 0; O12 (one real month of 1-minute site data with its bill) before Stage 3; O3 (bills) and O5 (an Open-Meteo paid plan) before Stages 1 and 4 respectively. Part 15.2 lists all twelve.
- **An AI agent (Opus in Claude Code):** paste the master prompt from `EMS_BUILD_HANDOFF.md` section 10 in the first session, the resume prompt in every later one. The handoff limits it to two or three subagents at a time and tells it to save progress to files and commit after every finished unit.
- **A human developer:** the same files, the same order; Part 12 is the work breakdown (one item is one to three commits with its acceptance criterion); Part 13 is the test and gate protocol; the handoff's rules 1 to 12 are the conventions. Set up as handoff section 3 says: a venv from the owner's `ops` interpreter, Node 22, the ports 8051/5195 (scratch 8151/5295), windowless servers, one server per data folder.
- **Git:** work on branch `v1-build` after a baseline commit on `main`; never merge, never push; the owner opens pull requests and pushes `v1-build` at each stage exit so CI runs.
- **Stages:** 0 safety net and the lift; 1 sites, tariffs and the bill engine; 2 telemetry, ingest and the twin; 3 the baseline replay and savings; 4 forecasters; 5 the optimiser and the plan page; 6 backtest, switchback and reports; 7 Mode A delivery, the contract, alerts; 8 hardening and release. Each ends with the four suites green and its exit check.
- **If a session is cut off:** `git status`; finish a half-built unit's red tests, never revert them; commit partial work as `WIP <item IDs>`; add a line to the tracker's session log; the next session resumes at the "Next item" of the last log line (handoff section 8).

## 6. State of the plan's own review (6 November 2026)

Four adversarial reviews were run on the plan and every finding is applied: the engine lens (26 findings: the ICP pricing, the reserve rule, the terminal value, the genset carried state, the switchback protocol, real-data gates), the build lens (25: the missing frontend clone, the Python path, CI as the owner's gate, the role model, missing tables, the brand data ordering, split items), the market-and-tariff lens (21: the ex-GST savings basis, MDI and ICP proven on monthly bills not switchbacks, genset auto-start panels, site selection by real outage hours, owner-task timing, connections per meter, captive gas, pricing per kWh of battery) and the UX-licence-security lens (19: a typed money filter for viewers, the control tier as two facts, reports and exports under the role filter, the installer's phone path, the licence gate and notices page, notification channels, key management). The findings files are in `plan-work/review/`; the plan cites them where an item changed. The planning work is finished; the next step is the owner's review of Part 0 and owner tasks O1, O10 and O11.

## 7. One-paragraph summary for a reader in a hurry

Build two repositories, `gridlytics-backend` (FastAPI, SQLite per company, scipy's HiGHS solver, pvlib and xgboost forecasters, a tariff engine verified against NEPRA's 2026 schedule, a digital twin, a savings verifier) and `gridlytics-frontend` (React 19, Vite 8, Tailwind 4, lettered-tab wizards, the plan page as a slot table with reasons), starting from a lift of Solarlytics' sign-in module and following `EMS_BUILD_PLAN.md` Part 12 stage by stage under the rules of `EMS_BUILD_HANDOFF.md`, ticking `EMS_BUILD_PROGRESS.md`, never merging or pushing, and measuring every quality number on real data rather than on the twin.
