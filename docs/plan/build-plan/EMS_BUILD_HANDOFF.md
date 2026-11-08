# Gridlytics build handoff

Written 6 November 2026 for the session that builds Gridlytics (OnyxLeaf's Product 1, the TOU-aware, outage-aware, genset-aware dispatch optimiser) from the plan into a pilot-ready app. Read this file first, every session. It tells you what to build, from which documents, in what order, under which rules, and how to leave the work so the next session can resume. "Gridlytics" is the working name; decision D1 of the plan may rename it (one search-and-replace before Step 0).

## 1. The job

Build the product the plan describes, as two repositories in the shape of Boxlytics and Solarlytics: a Python FastAPI backend that holds every piece of engine IP, and a React web app that asks the API and draws the answers. The owner wants, in their words:

1. Two repositories, one frontend and one backend, just like Boxlytics and boxlytics-backend (those folders are for audit only).
2. The core product pages and tabs first; the admin dashboard and the rest later (the founder console comes with the lifted sign-in module and needs no separate step).
3. The best, most ideal product, built from the right combination of code available online (permissive licences only) plus the Pakistan layer written new.

The plan already specifies all of this. Your job is to build it, test it and document it.

## 2. Documents, in order of authority

| # | File | Role |
|---|---|---|
| 1 | `EMS_BUILD_PLAN.md` (copied into `docs/PLAN.md` at Step 0) | **Master document.** Scope, decisions, architecture, data model, engine, forecasters, tariffs, delivery, verification, pages, skeletons, build order with item IDs and acceptance, licences, risks. Wins every conflict, including over the research notes where they differ (Part 10.4 names the places) |
| 2 | `EMS_BUILD_PROGRESS.md` (copied into `docs/BUILD_PROGRESS.md`) | **Progress checklist**, generated from the plan's Part 12. You keep it current. It is how the next session knows where to resume |
| 3 | `EMS_BUILD_HANDOFF.md` (copied into `docs/BUILD_HANDOFF.md`) | This file: rules, environment, protocol, decisions |
| 4 | `plan-work/research/*.md` | The research notes behind the plan (U1 to U3, R1 to R9). Read the one an item cites before building it; they hold file paths, line numbers, register facts, licence texts and verified numbers |
| 5 | `plan-work/review/*.md` | The adversarial reviews of the plan (engine, build, market, UX-licence-security lenses) and what was changed because of them; read a finding when an item cites one |
| 6 | `Perfect-EMS_TOU_Dispatch_Deep_Review.md`, `Product 1 - TOU-Aware Energy Management and Dispatch.md` | The owner's research reports: the market, the economics, the method, the pilot protocol. Read for context; the plan is the specification |
| 7 | `docs/PRODUCT_BRIEF.md`, `EQUATIONS.md`, `ASSUMPTIONS.md`, `COMPETITORS.md`, `DESIGN_LOG.md`, `TEST_MATRIX.md`, `GATES.md`, `THIRD_PARTY_NOTICES.md`, `API_CONTRACT.md` | Product documentation you create at Step 0 and keep true as you change the code |

## 3. Paths and environment

| What | Where or how |
|---|---|
| Backend repository | `C:\Users\SaadTahir\Videos\gridlytics-backend` (GitHub saad-tahir/gridlytics-backend, created by the owner, O10) |
| Web repository | `C:\Users\SaadTahir\Videos\gridlytics-frontend`, beside it (CI checks it out under that name too) |
| Plan package | `C:\Users\SaadTahir\Videos\solarytics\` (the plan, this file, the tracker, `plan-work/research/`, `plan-work/review/`, `plan-work/bench/`) |
| Reference repositories (read-only) | `C:\Users\SaadTahir\Videos\solarlytics-backend` (the tenancy module to lift, at commit `c853130`); `C:\Users\SaadTahir\Videos\solarlytics-frontend` (the web app's sign-in pages, launcher and browser-check runner to lift; the owner clones it, O11; the backup folder does not hold it; if it is absent, stop and ask); `boxlytics` and `boxlytics-backend` (patterns only) |
| Python | `C:\Users\SaadTahir\Videos\gridlytics-backend\.scratch\venv\Scripts\python.exe`, made once in PLT-02 with `C:\Users\SaadTahir\anaconda3\envs\ops\python.exe -m venv .scratch\venv` and then `.scratch\venv\Scripts\python.exe -m pip install -r requirements-dev.txt`. **Every `python` in this file means that file.** `python` on PATH is the Windows Store stub and the conda `ops` env lacks PyJWT, cryptography, pvlib, APScheduler and pytest-xdist; use `ops` only to create the venv |
| Node | 22.13 on this machine; `.nvmrc` = 22 |
| Start the app | `python scripts\start_local.py --scratch` in the web repository (API 8151, web 5295, a throwaway data folder, the scheduler on); the owner's ports are 8051 and 5195 and the release build's 8061 (5193 is Retailytics' demo port; 8041, 5192 and 8060 are Solarlytics'); the launcher defaults to the backend's venv, refuses the Store stub and any port that already answers, and runs every child windowless; one server per data folder (a lock file). The API does not hot-reload: restart after backend edits |
| Backend tests | `cd backend` then `python -m pytest -q -n auto` |
| Verification | `cd verification` then `python -m pytest -q`; `python compare.py --update` rewrites `REPORT.md` |
| Bench | `cd bench` then `python milp_bench.py`; `bench\REPORT.md` |
| Frontend checks | `cd frontend` then `npm run gates` (typecheck, lint, format, tests, build); `npm run e2e` (signs in with the seed accounts on a scratch backend) |
| Browser check | `node scripts\e2e\check.mjs` in the web repository (own app on scratch ports; refuses the owner's ports and data; signs in with the seed accounts) |
| Workspace data | `data\` (git-ignored): `platform.db`, `platform.secret`, `companies\` (each company's workspace file and its `-telemetry` file), `telemetry\nwp.db`, `backup\`, `.env.founder` |
| Lane work | `C:\Users\SaadTahir\Videos\_build\gridlytics\` (git-ignored, outside the temp scratchpad, which is wiped) |
| Owner's memory notes | `C:\Users\SaadTahir\.claude\projects\C--Users-SaadTahir-Videos\memory\` (`MEMORY.md` is the index; read `ems-dispatch-plan.md`, `git-workflow-rules.md`, `few-agents-at-once.md`, `scratchpad-wipe-trap.md`, `no-console-windows.md`) |

## 4. Standing rules (do not break these)

1. **Clean-room by default.** Write new code. Copy only what the plan's Part 14 allows (MIT, BSD, Apache-2.0 with notices kept), record every copy in `THIRD_PARTY_NOTICES.md` with a header in the file. Patterns only from EPL, LGPL, GPL, AGPL and non-commercial sources; never clone them into the product's folders.
2. **Hands off other repositories.** `boxlytics`, `boxlytics-backend`, `solarlytics-backend`, `solarlytics-frontend`, `solarlytics` (and its backup), `panelytics*`, `retailytics*`, `packlytics` are read-only.
3. **The lifted code is OnyxLeaf's own.** The tenancy module from `solarlytics-backend` at `c853130` and the web app's sign-in pages are lifted as the plan's Part 10.4 and 11.3 say; the founder token format's Boxlytics origin is named in `DESIGN_LOG.md` for the owner to confirm. Do not re-derive them.
4. **Copyleft and benchmark tools stay in `verification/` and `bench/`.** The product never imports python-microgrid, PySAM, BLAST-Lite, PyBaMM, CityLearn, OpenSTEF or Quartz.
5. **Protect the owner's data.** Never delete or reseed `data\` once the owner has used the app on it; before that, and always on `--scratch`, the seed may be re-run. A copy before any schema change; tests on a temp data folder; manual checks on `--scratch`.
6. **Git.** Work on `v1-build` after the baseline commit; never merge, never push, never add a remote, never enable auto-merge; the owner opens pull requests and pushes `v1-build` at each stage exit so CI runs. Commit at the end of each finished unit with the item IDs in the message; a WIP commit's message starts with `WIP` and names the IDs. The web app commits before the backend.
7. **Honesty about numbers.** Every tariff value carries its SRO, date and status; every optimiser coefficient equals the bill engine's marginal price (plan Part 4); every saving is reported against both baselines at 1-minute settlement; `[verify]` on anything recalled; "Preliminary" on reports until the switchback has run and the FCA is billed; a gate measured on twin data proves only that a pipeline runs.
8. **Never claim more than the code does.** No "guaranteed", "optimal", "certified" or "works with all inverters" on any page or document.
9. **Fixes come with tests.** A safety rule gets a test that fails on the old code; a rupee or energy figure gets a golden test worked by hand in the docstring; a fixture produced by the engine is not a golden.
10. **Do not delete working features**; say so in the session log if a change removes behaviour.
11. **Few agents at once.** At most two or three subagents at a time (the owner's limit for this build, below the five in `few-agents-at-once.md`); each saves progress to files; never resume a pooled workflow (rerun one step as one agent).
12. **No console windows.** Background servers run windowless (`CREATE_NO_WINDOW`, `pythonw`).

## 5. Build order

Stages 0 to 8 of the plan's Part 12, in order, each ending with the four suites green locally and the stage's exit check, then a request to the owner to push `v1-build`. Before starting each stage, write a short plan (units, files, data model changes, tests) at the top of that stage's entry in the session log, then build. Items marked `[owner]` wait for the owner; do not fake their inputs beyond the fixtures the plan names. Owner tasks O1, O10 and O11 come before PLT-01; O12 before VER-02.

## 6. Decisions already made

The plan's Part 0 lists D1 to D17 with the default for each. Take the defaults unless the owner has written otherwise in `docs/DESIGN_LOG.md`'s decisions table; record any override there.

## 7. What to take from the research notes

| Note | Use it for |
|---|---|
| U2 | The lift of the sign-in module (section 1.7), the conventions (section 2), the document templates (section 5); section 4's skeleton only where the plan does not differ (the plan wins on APScheduler, the telemetry file, `errors.py` and the setting names) |
| U1 | The frontend skeleton (section 7), the lettered-tab structure (section 8), the patterns to mirror and avoid (sections 5 and 6) |
| U3 | The telemetry table (1.2), primitives and brand mappings (2.2 to 2.4), the safety contract (3), the ingest paths and the twin (4), the contract with examples (5) |
| R1 | The model-builder structure, the constructions to port with notices (sections 9 and 11), the prototype (`R1-target-milp-prototype.py`), but not its horizon-local ICP counter |
| R2 | The define-once and MPC patterns (sections 3 and 4), the functions worth copying (8.2), the single-recovery rows for an out-of-band SoC |
| R3 | The genset, outage, demand-charge and degradation mathematics (sections 2 to 6, 8) and the big-M inventory (9); the expected-value reserve rule of 3.5 |
| R4 | The schema and plan state machine (sections 4 and 6) |
| R5 | The forecasting pipelines, cadence, metrics by horizon class, storage, fallback (sections 4 to 8) |
| R6 | The TouSlotPlan and compiler rules (3.6), the per-brand registers (3.1 to 3.5), the safety invariants and rules (section 4: I1 to I8, C1 to C13, the event matrix, the tier-2 confirmation), the battery service (5), the wear derivation (6), the licences (7) |
| R7 | The platform decisions, the benchmark and the pins (12.1 and 12.2), except the solver pair the plan keeps at the measured versions |
| R8 | The page and tab inventory with components, data, empty states, roles, tiers, polling and the demo story |
| R9 | The tariff data model, regimes, adders, the bill-rebuild algorithm, the verified numbers and the uncertainties |

## 8. Session protocol (how to resume)

**At the start of every session:** read this file, then the session log at the top of `docs/BUILD_PROGRESS.md`, then `git log --oneline -15` on `v1-build` in both repositories; run the backend tests, `npm run gates` and, when the stage has them, the verification and the browser check. If the last log line or a WIP commit names a RESUMING unit, that unit's red tests are finished, never reverted; any other failure is fixed first. Continue at the "Next item" named in the last log line.

**For each unit:** read its rows in the plan's Part 12 and the parts and notes they cite; write the test first where the item is a rule or a figure; implement backend, API and page together; run all checks; update `EQUATIONS.md`, `ASSUMPTIONS.md`, `DESIGN_LOG.md`, `PRODUCT_BRIEF.md`, `API_CONTRACT.md`, `GATES.md` and `THIRD_PARTY_NOTICES.md` where the change touches them; tick the rows (`[x]` only when code, tests and docs are done; `[~]` if partly).

**Reviews:** one reviewer per unit told to refute it (highest effort); a fixer only on a medium or high finding; low findings batched into a polish pass at the end of the stage; one integrated review of the stage's diff before its exit check.

**At the end of every session:** add a line to the session log (date, items finished, anything left half done, the next item) and commit.

**After a usage-limit stop:** `git status`; keep intact uncommitted work and resume the unit; commit partial files as `WIP <IDs>` or remove them and add a RESUMING note to the unit's plan.

**Stop and ask the owner when:** an item needs an owner task (O1 to O12: a pilot site, a bill, a weather plan, the product name, a liability clause, the frontend clone, the real data month, a decision D8 or D10); a change would move a verified plan or saving beyond what the change explains; a change would delete or overwrite saved data; or you are about to touch anything outside the two repositories and the lane folder.

## 9. Done means

| Level | Software-checkable test |
|---|---|
| Stage done | Every item of the stage ticked; the four suites green locally; the stage's exit check passed; counts in the session log and `GATES.md`; the owner asked to push |
| Engine ready (after Stage 5) | The 192-step genset case under 30 s on the laptop (10 s on CI after the owner's push); the marginal-price goldens and the carried-state mechanism tests green; zero hard-row breaches over 1,000 twin days; the golden archetypes reproduce; the reference cases match |
| Pilot-ready (after Stage 8) | The plan's Part 1.4 pilot-ready row; O12's real-month figures in `GATES.md`; the owner's checklist handed over; every page live on the demo sites in every role |

The owner-only items (pilot sites, bills, feeder codes, the data month, subscriptions, the name, the legal wording) remain open until the owner closes them. The app is not sellable until they are, whatever this checklist says.

## 10. Paste-ready prompts

**Master prompt (first session):**

> Read `C:\Users\SaadTahir\Videos\solarytics\EMS_BUILD_HANDOFF.md`, then `EMS_BUILD_PLAN.md` Parts 0, 2, 3, 10, 11, 12 and 13, then the memory notes the handoff names. Check that owner tasks O1, O10 and O11 are done (the product name, the GitHub pair, the `solarlytics-frontend` clone at `C:\Users\SaadTahir\Videos\solarlytics-frontend`); if not, stop and ask. Then do Stage 0 of the plan's Part 12 in order: create the two repositories as the plan says, make the backend's venv and use it for every command, lift the tenancy module from `solarlytics-backend` at `c853130` following the plan's Part 10.4 and `plan-work/research/U2-backends.md` section 1.7, build both skeletons, the demo seed, the launcher and CI, copy the plan, the handoff and the tracker into `docs/`, and write the first design-log section. Run at most two subagents at a time and save progress to files. Commit on `v1-build` after each finished unit; never merge or push. End the session with a line in the tracker's session log naming the next item, and ask the owner to push `v1-build` when Stage 0's exit check passes.

**Resume prompt (every later session):**

> Read `docs\BUILD_HANDOFF.md`, the session log at the top of `docs\BUILD_PROGRESS.md` and `git log --oneline -15` on `v1-build` in both repositories. Run the suites with the backend's venv Python; finish a RESUMING unit's red tests, fix any other failure first. Continue at the "Next item" of the last log line, one unit at a time, with the review and commit rules of the handoff. Two subagents at most at a time.
