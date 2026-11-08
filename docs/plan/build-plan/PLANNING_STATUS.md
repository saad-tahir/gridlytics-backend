# Planning run status (resume note)

Kept outside the session scratchpad on purpose (the scratchpad is wiped between sessions).

## What this folder is
Working notes for the end-to-end build plan of the EMS dispatch SaaS (OnyxLeaf "Product 1"). The deliverables go to
`C:\Users\SaadTahir\Videos\solarytics\`: `EMS_BUILD_PLAN.md` (master), `EMS_BUILD_HANDOFF.md` (rules, environment,
protocol and the master prompt for the executing agent), `EMS_BUILD_PROGRESS.md` (tracker). The research notes under
`research/` are part of the plan package and are cited by the plan.

## Research tracks
| Track | File | State (6 Nov 2026) |
|---|---|---|
| U1 Boxlytics frontend audit | research/U1-boxlytics-frontend.md | done (551 lines) |
| U2 Backends audit (solarlytics-backend, boxlytics-backend) | research/U2-backends.md | done (603 lines) |
| U3 Product 2 interface | research/U3-product2-interface.md | done (607 lines) |
| R1 evcc-io/optimizer | research/R1-evcc-optimizer.md, R1-target-milp-prototype.py | done (512 lines) |
| R2 EMHASS | research/R2-emhass.md | done (322 lines) |
| R3 REopt formulations | research/R3-reopt-formulations.md | done (958 lines) |
| R4 Architecture refs (FlexMeasures, EOS, OpenEMS) | research/R4-architecture-refs.md | done (298 lines) |
| R5 Forecasting | research/R5-forecasting.md | done (198 lines) |
| R6 Executor and battery | research/R6-executor-battery.md | done (693 lines; folded into draft 04.3, 04.6, 07.1, 07.3, 07.6, 08.4, 12, 14) |
| R7 Platform stack | research/R7-platform-stack.md | done (97 KB; verdicts folded into drafts 01, 02, 03, 10, 11, 12, 14: company telemetry file, APScheduler, ECharts for dense charts, ReportLab PDFs, Mosquitto/Valkey later) |
| R8 UX pages | research/R8-ux-pages.md | done (584 lines; draft Part 9 written from it; UI-00, UI-17, UI-18 added to Part 12) |
| R9 Pakistan tariffs | research/R9-pakistan-tariffs.md | done (605 lines; primary-source verification; corrections folded into draft Part 6, 4.3, 12) |

`research/_journal_results.md` holds the structured summaries of the eight finished tracks (from the workflow journal).

## Lessons from the first session
- The first run (12 agents, 5 at a time) was cut off by the usage limit after 8 finished; the user then asked for fewer agents at a time. Run at most 2 at a time from now on, as single Agent calls (never resume a pooled workflow: the cache prefix breaks and agents re-run live).
- Every agent appends to its notes file as it goes, so a cut-off leaves most of the work.

## Draft state (second session)
Draft parts written in `draft/`: 01 front and decisions, 02 architecture, 03 data model, 04 optimiser, 05 forecasting,
07 delivery and edge contract, 08 verification, 10 backend, 11 frontend, 13 verification and protocol, 14 licences,
15 risks and pilot. Also written: 06 tariffs, 12 build order, 16 appendix, HANDOFF.md, make_progress.py. All sixteen parts written and assembled on 6 Nov (second session) into ../EMS_BUILD_PLAN.md (1,285 lines, 121 items), ../EMS_BUILD_HANDOFF.md and ../EMS_BUILD_PROGRESS.md (assemble.py runs make_progress.py). Review: review/engine-lens.md (26 findings) and review/build-lens.md (25 findings) are done and every finding is applied in draft/ (6 Nov, third session: Parts 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 15, HANDOFF, make_progress). The plan was re-assembled (1,344 lines, 136 items). The market-and-tariff and UX-licence-security reviewers were cut off by the usage limit before writing much and were resumed by message on the re-assembled file; their findings go to review/market-lens.md and review/ux-licence-security-lens.md. After them: apply, re-assemble, final memory update, hand over. then assemble `EMS_BUILD_PLAN.md`, write `EMS_BUILD_HANDOFF.md` and `EMS_BUILD_PROGRESS.md`, review.

## Next steps
1. When R7 and R9 return: run R6 (continue from its file) and R8, two at a time.
2. Write the plan: decisions, architecture, data model, engine spec, edge contract, pages and tabs, stages with item IDs, acceptance, fixtures, verification, licence rules, owner tasks, risks.
3. Adversarial review of the plan (2 reviewers at a time), fix, then hand to the user for review.

## State at hand-over (6 Nov 2026, end of the third session)
- Delivered: ../EMS_BUILD_PLAN.md, ../EMS_BUILD_HANDOFF.md, ../EMS_BUILD_PROGRESS.md (all re-assembled after the engine and build reviews were applied).
- Done reviews: review/engine-lens.md (26 findings) and review/build-lens.md (25 findings), all applied.
- Not done: the market-and-tariff lens (review/market-lens.md holds only its opening) and the UX-licence-security lens (no file). Both reviewers were stopped on the user's request to save tokens. To rerun later: one Agent each with the briefs of the third session (refute Parts 0, 1, 6, 8, 15 against R9 and the reports; refute Parts 9, 11, 14, 2.8, 10.5 against R8, U1, R7, R6 section 7, U3 section 5), two at a time at most, then apply to draft/ and run assemble.py.

## Fourth session (6 Nov 2026, after the hand-over request)
- ../START_HERE.md written: the hand-off for a person or agent with no context (what the product is, the file manifest, the two reference repositories, reading order, how to work, the review state). plan-work/context/ holds copies of the owner's memory notes the plan relies on.
- Reviews now run one at a time, each writing its findings file incrementally: the market-and-tariff reviewer was resumed first (review/market-lens.md); the UX-licence-security reviewer (review/ux-licence-security-lens.md, 17 KB partial) runs after it. After each: apply to draft/, run assemble.py, update this file.
- Market-and-tariff review done (review/market-lens.md, 21 findings) and applied to draft/ (Parts 1, 3, 4, 6, 8, 9, 12, 15; owner tasks O2, O3, O8, O9 re-timed; O13 added); the plan re-assembled. The UX-licence-security reviewer was resumed alone; after it: apply, re-assemble, update START_HERE section 6.
- UX-licence-security review done (review/ux-licence-security-lens.md, 19 findings) and applied (Parts 2.8, 3.2, 9.3, 9.5, 9.6, 10.5, 11.5, 12, 14); final re-assembly done. All four reviews are complete and applied. Planning is finished; the next step is the owner's review of Part 0 and the owner tasks O1, O10, O11.

## Build status (7 Nov 2026)

The plan has been built end to end:
- Backend: `Videos\gridlytics-backend`, branch `v1-build`, head `fc49e3a`, 423 tests.
- Web app: `Videos\gridlytics-frontend`, branch `v1-build`, head `3282a14`, 222 tests.
- Nothing has been pushed; there is no remote.

Where to look:
- Rounds of review fixes and every deviation: `gridlytics-backend\docs\DESIGN_LOG.md`.
- Item ticks: `EMS_BUILD_PROGRESS.md`.

Run locally with `python scripts\start_local.py`, from the frontend repository, using the backend venv's python:
- Owner ports 8051/5195, on the owner's `data` folder.
- `--scratch` gives ports 8151/5295 on `.scratch\data`.
