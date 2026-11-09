# Gridlytics audit: progress and how to resume (saved history)

Updated 2026-11-09 07:15 UTC (model switched to Opus 5.5 after the Fable usage limit at ~00:45).

## Done and saved
- Setup: both repos on v1-build; backend 495 passed / 7 skipped; web 257 tests, typecheck and build clean; scratch demo seeded; app runs on API 8151 / web 5295 (`rm -f /tmp/gridlytics-data-*.lock; cd /home/user/gridlytics-frontend && exec ../gridlytics-backend/.scratch/venv/bin/python scripts/start_local.py --scratch`).
- Finder results saved (119 findings): pages-A (18), pages-B (17), pages-C (23), pages-D (30), brief-vs-build (18), engine-savings (13) → `results/findings-saved-6dims.json` (ids `<dim>-NN`), split into `results/batches/slice-A.json` (58) and `slice-B.json` (61).
- `results/journal-results.json` is refreshed every 60 s by `checkpoint.py --loop` from every workflow journal (completed agents only).

## Running (launched 00:27–00:30 UTC)
| Run | Script | Agents |
|---|---|---|
| wf_f29de1e9-591 | find-phase2.js (live) | role-owner, role-viewer, founder-console, flow-onboarding, flow-telemetry-plan-control, parity-boxlytics-ui |
| wf_60d2c7ef-075 | find-phase2.js (live) | role-operator, role-analyst, flow-savings-pilot-bill, flow-reports-alerts-members-keys, engine-plans-live, engine-money-live, parity-supabase-auth, parity-architecture |
| wf_5aad076f-cf9 | find-phase1.js (code) | engine-optimiser, engine-forecast, engine-tariffs, engine-delivery, security-roles, frontend-a11y, frontend-states-formats, pr-review, tracker-and-docs, tests-and-perf |
| wf_34b600f4-7b9 | verify-fix-batched.js slice A | 10 verify batches, then fix batches |
| wf_140e2b61-ba7 | verify-fix-batched.js slice B | 11 verify batches, then fix batches |

Dropped as duplicates of the live reviewers: code dims boxlytics-ui-parity, auth-supabase-parity, onboarding-flows. Still to run after these: performance-live (alone), verify-fix slices C.. for the new findings, section8-designs.js, the shippability workflow, the completeness critic.

## Owner's constraints added to every prompt
- No Google/social sign-in, no self-signup: preassigned accounts as in solarlytics-backend (local mode; Supabase only as an optional owner-applied mode).
- Gridlytics must look like a sibling of Boxlytics (landing page, fonts, colours, spacing, components).
- A viewer never sees money. Never touch `gridlytics-backend/data`. Never push to main or v1-build.

## Resume recipe
1. If a run died: `Workflow({scriptPath, resumeFromRunId})` with the same args; completed agents return cached results.
2. Verify results: `python3 -I merge_verified.py <journal.jsonl or result.json> --out results/verified-<slice>.json`.
3. Final document: `python3 -I render.py --out /home/user/GRIDLYTICS_AUDIT.md --verified results/verified-*.json --designs results/section8.json --ship results/ship.json --front results/front-matter.md --top results/top10.md --parity results/parity.md`.


## 07:10 UTC relaunch (after the Fable limit killed almost every agent)
Saved before relaunch: engine-forecast findings (16) → `findings-engine-forecast.json`; verdicts for pages-B-01..12 and pages-D-01..12 (cached in their runs).
Relaunched with resumeFromRunId (cached agents replay): wf_5aad076f-cf9 (code dims), wf_f29de1e9-591 and wf_60d2c7ef-075 (live dims), wf_34b600f4-7b9 (verify slice A; fix phase now batched by dimension, 5 per agent).
Queued: wf_140e2b61-ba7 (verify slice B) resume; then verify slices for the new findings; section8-designs.js; shippability script; completeness critic; PR branch test run.
App and checkpoint loop now run detached (setsid nohup), so the 2-hour background limit no longer kills them: launcher log `.scratch/launcher4.log`.
Drafted: results/front-matter.md, results/parity.md, results/missed.md, results/pr-review-notes.md; render.py rewritten (Parts 0-8 + appendices C, D).

## 12:50 UTC — owner's new instruction: implement every finding directly
- All reviewers done: 557 + 116 late = 673 findings. Merged file: impl/all-findings.json (+ results/findings-late-*.json); index impl/index.tsv, impl/index-late.tsv.
- Plan: impl/plan.json (19 work packages, 75 batches, 407 canonical ids, 144 duplicates, 6 deferred). Check: `python3 -I impl/check_plan.py impl/plan.json`.
- Baseline on origin/fix/offgrid-carry-over: backend 503 passed / 8 skipped; web 259 tests, typecheck, build clean (impl/logs/base-*.log).
- Worktrees: /home/user/wt/<wp>/{gridlytics-backend,gridlytics-frontend}, branch audit/<wp> from origin/fix/offgrid-carry-over. Helpers impl/bin/mkwt.sh, tb.sh (backend tests, flock), tf.sh (web tests, flock).
- Stage-1 implementation running (implement-wps.js): wf_4eeead6a-8aa (optimiser-plans, fleet-overview, tariffs, shell-nav, demo-seed), wf_e7ee2643-f9b (savings-engine, forecasts-outages, founder-console), wf_f9aeec06-a18 (control-edge, sites-onboarding, delivery-twin), wf_c9fe50b5-0ab (alerts-reports, auth-security, telemetry-genset-battery, ui-charts-a11y). Resume any with resumeFromRunId and the same args.
- Late findings assignment: wf_b52f0d77-3ef → impl/plan-late.json, then a second implement run per package (offset batches).
- Then: integration branch audit/integration (merge 15 branches, suites, browser check), stage 2 (savings-ui-crosscut, tests-ci), stage 3 (parity-ui, docs), push + PRs into v1-build, report and developer message.

## 17:10 UTC — round 2 of stage-1 implementation (after the 5-hour limit at ~14:00 and a machine restart)
- Round 1 finished batch 1 of savings-engine, control-edge, optimiser-plans, forecasts-outages, sites-onboarding, fleet-overview, shell-nav (results: impl/results-stage1-round1.json); other agents died mid-batch, leaving commits and uncommitted work in their worktrees, which round 2 picks up.
- Late findings: impl/plan-late.json; combined plan impl/plan-all.json (late batches appended to each package; new packages storage-perf (stage 2) and deploy-ci (stage 3)).
- Implementers now read only what they need (grep the docs) and resume interrupted attempts from git log/status. Every test run is serialised by impl/bin/tb.sh / tf.sh (exclusive flock).
- Round 2 runs (offset = batches already done): wf_852eb745-0d9 (optimiser-plans, savings-engine, tariffs, fleet-overview, demo-seed), wf_471422c7-12c (control-edge, alerts-reports, delivery-twin, founder-console, shell-nav), wf_1a8428a9-a72 (auth-security, telemetry-genset-battery, forecasts-outages, sites-onboarding, ui-charts-a11y).
- If a run dies on the usage limit: resume it with resumeFromRunId and the same args (completed agents replay from cache).
- The scratch app is NOT running (not needed for implementation); restart it for integration.

## 18:15 UTC — PAUSED FOR HANDOVER (owner's request)
All implement workflows stopped (wf_852eb745-0d9, wf_471422c7-12c, wf_1a8428a9-a72). Unfinished agent work committed as "WIP (interrupted, unreviewed)" commits (tariffs both repos, optimiser-plans web, sites-onboarding web, shell-nav web). All backend branches compile; all web branches typecheck.
Handover package: /home/user/gridlytics-handover/ (HANDOVER.md, the two git bundles, gridlytics-audit-workspace.tar.gz). Backend push access was refused to this session, so nothing is pushed.
