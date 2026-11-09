# Gridlytics audit and fixes: handover

Written 9 November 2026, {{TIME}} UTC, at the point the work was paused. Read this first; it says what exists, where it is, and exactly what to do next.

## 1. What this work is

1. **The brief** (from Saad Tahir, forwarded by Abdullah): a full end-to-end review of Gridlytics (Product 1: OnyxLeaf's energy-dispatch optimiser for Pakistani C&I sites), repositories `saad-tahir/gridlytics-backend` and `saad-tahir/gridlytics-frontend`, branch `v1-build`, side by side. Set up per HANDOFF.md, get both suites green, run the scratch demo, review every page in every role at desktop and 375 px, every feature against EMS_BUILD_PLAN.md Part 9 and the Product 1 brief, the real flows, engine sanity, the open PR `fix/offgrid-carry-over`, then the HANDOFF section 8 work.
2. **The owner's own requirements (Abdullah):**
   - Gridlytics must look and feel like a sibling of Boxlytics ("creator products"): same landing page structure and text style, fonts, colour scheme, spacing, components on every page. Main architecture reference: the Boxlytics zips (boxlytics-main, boxlytics-backend-main). Solarlytics (`saad-tahir/solarlytics-backend`) shows the multi-company fixes. Boxlytics and Solarlytics are read only.
   - **Sign-in: preassigned accounts only, exactly as solarlytics-backend.** No Google or other social sign-in, no self-signup, no magic links, no invitations. Supabase only as Solarlytics' optional owner-applied mode.
   - A viewer must never see money anywhere.
3. **How the deliverable evolved:** issues → one Markdown audit with fix proposals → end-to-end tests to judge shippability → **"implement these changes directly"** (latest instruction). So the job now is: implement every verified finding in code, with regression tests, on branches, then integrate, restyle to Boxlytics, document, and report.
4. **Working rules (from the brief, still in force):** a branch off `v1-build` per piece of work; tests for every fix; both suites green; pull requests into `v1-build`; never push to `main` or `v1-build`; never touch `gridlytics-backend/data` (use a scratch data folder); no secrets in commits; Boxlytics/Solarlytics untouched.

## 2. Where everything is (this handover package)

**Nothing is pushed to GitHub yet.** The session could push to `gridlytics-frontend` but was refused push access to `gridlytics-backend`, so both repositories' branches are saved as git bundles instead (section 3 restores them). After restoring, push the `audit/*` branches yourself (never `main` or `v1-build`).


| File | What it is |
|---|---|
| `HANDOVER.md` | This document |
| `gridlytics-backend-audit.bundle` | Git bundle: every `audit/*` branch of the backend, on top of `origin/fix/offgrid-carry-over` |
| `gridlytics-frontend-audit.bundle` | Git bundle: every `audit/*` branch of the web app, same base |
| `gridlytics-audit-workspace.tar.gz` | The audit workspace without screenshots: all findings, verdicts, fix proposals, the implementation plan, per-package status, scripts and the Claude Code workflow scripts |
| `STATUS-IMPL.json` (inside the tarball, `impl/`) | Machine-readable state of every work package: batches done, per-finding status, commits, remaining batches with their finding ids |

Inside the tarball:

| Path | What it is |
|---|---|
| `impl/all-findings.json` | 557 findings (full records: steps, expected, actual, evidence, verdict, fix proposal where written) |
| `results/findings-late-*.json` | 116 late findings (same shape) |
| `impl/index.tsv`, `impl/index-late.tsv` | One line per finding: id, severity, repo, label, file, area, title |
| `impl/plan-all.json` | **The implementation plan**: 21 work packages (stage 1/2/3), each with owns_files, notes (read them: they split shared findings between packages) and ordered batches of finding ids; `duplicates` (finding → canonical id) and `deferred` |
| `impl/STATUS-IMPL.json` | State per package (see section 4) |
| `impl/bin/mkwt.sh`, `tb.sh`, `tf.sh` | Worktree maker and the serialised backend / web test runners |
| `impl/check_plan.py`, `impl/status.py`, `impl/merge_all.py` | Plan coverage check, status collector, findings merger |
| `results/front-matter.md`, `parity.md`, `missed.md`, `pr-review-notes.md` | Drafted parts of the final audit document (summary/method, Boxlytics parity change plan, aspects outside the findings, the PR review) |
| `render.py` | Assembles `GRIDLYTICS_AUDIT.md` from the parts (section 7.H) |
| `workflows/*.js` | Claude Code workflow scripts used (finders, verify, cluster, implement) |
| `api.py`, `shot.cjs` | API client and Playwright page-report helper for the scratch app (sign in through the real page; never print passwords) |

Screenshots (about 900 MB) are not in the package; findings cite their paths for provenance only.

## 3. Restore the work on a new machine

```bash
mkdir work && cd work
git clone https://github.com/saad-tahir/gridlytics-backend.git
git clone https://github.com/saad-tahir/gridlytics-frontend.git
cd gridlytics-backend && git fetch ../gridlytics-backend-audit.bundle 'refs/heads/audit/*:refs/heads/audit/*' && cd ..
cd gridlytics-frontend && git fetch ../gridlytics-frontend-audit.bundle 'refs/heads/audit/*:refs/heads/audit/*' && cd ..
```

(Adjust the bundle paths.) Every `audit/<package>` branch starts at `origin/fix/offgrid-carry-over` (round 6, the open PR: backend ab83058, web 499cebb), not at `v1-build`, because the savings work builds on round 6's chained replays. If the owner rejects round 6, rebase the branches onto `v1-build` and resolve `verify.py`.

Environment (HANDOFF section 3): Python 3.12 venv in `gridlytics-backend/.scratch/venv` (`pip install -r requirements.txt -r requirements-dev.txt`); Node 22, `cd gridlytics-frontend/frontend && npm ci`. Tests: `cd gridlytics-backend && .scratch/venv/bin/python -m pytest backend/tests -q` and `cd gridlytics-frontend/frontend && npm run gates`. Scratch app: `cd gridlytics-frontend && python scripts/start_local.py --scratch` (API 8151, web 5295; seed per HANDOFF; never the owner's `data/`).

Parallel work as it was done here: one worktree pair per package, side by side so the launcher finds `../gridlytics-backend`:

```bash
impl/bin/mkwt.sh <package> [base]   # /home/user/wt/<package>/{gridlytics-backend,gridlytics-frontend} on audit/<package>
impl/bin/tb.sh <package> [pytest args]   # backend tests of that worktree, under an exclusive machine-wide lock
impl/bin/tf.sh <package> [vitest files]  # web typecheck + tests (no args = full gates), same lock
```

Edit the paths at the top of the scripts for your machine. **Serialise test runs**: the solver tests are time-limited, and on a loaded 4-CPU machine they fail for no real reason.

## 4. State at the pause

Baselines on `origin/fix/offgrid-carry-over`: backend 503 passed / 8 skipped (one more skip than v1-build: the farm benchmark is opt-in); web 259 tests, typecheck and build clean. On `v1-build`: backend 495 passed / 7 skipped, web 257; browser check 405 visits, 0 failures.

Audit: 673 findings (557 + 116 late) → 407 + 66 canonical findings to implement in 21 packages; 192 duplicates folded into canonical ids; 8 deferred (4 refuted in verification, 4 owner decisions or planned HANDOFF-8 features; list in section 8).

Implementation: stage-1 packages were being worked in parallel by agents; each finished batch is committed on its branch with messages `R7 <package>: <what> (audit <ids>)`. **No package has had its review pass yet** (full suites + diff review): each implementer ran only targeted tests of what it changed. Work an agent had not committed when the work was paused is committed as a separate commit named `WIP (interrupted, unreviewed)` — treat it as a draft: check it, finish it or drop it.

**Progress in numbers:** {{COUNTS}}

**Branch health at the pause (checked after the last commit):** every backend branch compiles (`python -m compileall backend`); every web branch passes `npm run typecheck`, including the WIP commits. The full suites were not run per package (that is step 7.B).

**Work-in-progress commits** (agents stopped mid-batch at the pause; review before building on them):

{{WIP}}

{{PACKAGE_TABLE}}

## 5. How to implement a finding (the procedure every batch followed)

1. Read the finding (and its duplicates' records) from `all-findings.json` / `findings-late-*.json`, and the package's `notes` in `plan-all.json`: they say which half of a shared finding belongs to which package.
2. Verify it against the branch's code (round 6 changed savings code): implement | already_fixed (cite where) | refuted (by design per DESIGN_LOG and still sensible, or not reproducible: say why) | deferred (only an owner/business decision, hardware or real-site data).
3. Write the regression test first and watch it fail: backend `backend/tests/test_fix_r7_<package>.py` (conftest conventions); web vitest beside the component (`src/test/render.tsx`, fixtures).
4. Fix minimally in the code's own style: strict pydantic request models via `schemas.check` (one-sentence 422s); `auth.need` for roles; money keys hidden by `money.py` for viewers and API keys; Pakistan time; the web app's wording rules (`docs/GATES.md`); no restyle outside `parity-ui`.
5. If an API answer changes shape, append to the web app's `docs/API_CONTRACT.md` under "## Round 7 (audit): <package>".
6. Commit per finding (or per root cause): `R7 <package>: <what> (audit <ids>)`.
7. Run the new tests plus the existing test files of the modules touched.

## 6. Files several packages touch (merge hotspots)

- Shared additive files: `frontend/src/types/api.ts`, `lib/api/endpoints.ts`, `lib/api/keys.ts`, `lib/format.ts`, `lib/time.ts`, `docs/API_CONTRACT.md`, `docs/BACKEND_ASKS.md`, `backend/gridlytics/schemas.py`. Conflicts there are appends: keep both sides.
- `store.py` migrations: a package that added one took the next free number; renumber at integration if two collide.
- Function splits recorded in the plan: `verify.py` (savings-engine; bill functions are tariffs'), `plans.py` (optimiser-plans; control-edge owns the guard/held/expiry functions and edited `tick` in one line to pass the solve to the guard), `delivery.py` (delivery-twin compile half; control-edge keys and messages), `routes_savings.py` (savings-engine pilot/switchback; tariffs bills), `routes_sites.py` (see plan notes).
- `HANDOFF.md` and `docs/DESIGN_LOG.md` were left alone in stages 1–2 on purpose: the `docs` package writes DESIGN_LOG "Round 7" and the HANDOFF update once, from the packages' results.

## 7. What to do next, in order

### A. Finish stage 1 (15 packages)
For each package in section 4, in its worktree: review any `WIP (interrupted, unreviewed)` commit; then, before starting a remaining batch, `git log --oneline origin/fix/offgrid-carry-over..HEAD | grep <id>` for each of its ids, because agents that were stopped by usage limits earlier sometimes committed part of a later batch (those commits name their ids: verify and keep them); then implement the remaining batches listed for it (ids in `STATUS-IMPL.json` → `remaining_batches`, or in section 4's table), batch by batch, following section 5. High-severity findings come first within each package.

### B. Review each stage-1 package
Full backend suite and full web gates on the branch; fix every failure (decide whether the fix broke behaviour or the old test encoded the old, wrong behaviour, and say which in the commit). Review the whole diff `git diff origin/fix/offgrid-carry-over...audit/<package>`: correctness, edge cases, the owner's rules, DESIGN_LOG consistency, tests that would pass without the fix. Write each package's design decisions as notes for DESIGN_LOG round 7.

### C. Integrate stage 1
Branch `audit/integration` from `origin/fix/offgrid-carry-over` in both repos; merge the 15 packages in this order (fewest shared files first): demo-seed, shell-nav, ui-charts-a11y, founder-console, auth-security, fleet-overview, telemetry-genset-battery, forecasts-outages, alerts-reports, sites-onboarding, tariffs, delivery-twin, control-edge, optimiser-plans, savings-engine. After each merge run the targeted tests of both sides; after all, both full suites, then the browser check (`npm run e2e` in the web app, against `start_local.py --scratch` launched from the integration worktrees with a fresh scratch data folder and a new seed, since stored rows predate the fixes).

### D. Stage 2 (branch each from `audit/integration`)
- `savings-ui-crosscut`: the Savings page halves of the savings findings (claims vs every day, withheld reasons, carry-over provisional, MDI month, fee basis, amendments, switchback panel), pilot go/no-go and success criteria, the cross-module engine batch (multi-connection and metering factor, Punjab duty by connection, Mode B in the twin/guard/demo, genset charging, event re-solves), the app-wide sweeps (row-action names, written dates). The savings-engine branch already exposes the API fields these pages need (see its API_CONTRACT section).
- `storage-perf`: plan-row retention (spec Part 3.3: steps/slots of superseded plans kept 90 days), the /now polling cost (cache battery.state; header reads only what it shows), the scheduler failure path and its test, backups (daily copies of a growing file).
- `tests-ci`: the engine acceptance tests (bill goldens to the paisa per regime, optimiser invariants, savings fairness), the browser check in CI.
Integrate them into `audit/integration`.

### E. Stage 3 (on the integrated branch)
- `parity-ui`: the Boxlytics look, following `results/parity.md` exactly: tokens (primary #1f8a78 / dark #145f53 / light #2ba98f, white page, glass cards, radius 0.5rem), Poppins + JetBrains Mono, the 15/14/16 px base scale, the 264 px glass sidebar with the filled active item and accent bar, the 80 px header with page title and subtitle, rounded-lg buttons, KPI tiles, inputs, dialogs; a landing page at `/` section by section as Boxlytics (with "Sign in" and "Request access", never "Get started free": accounts are preassigned); privacy, terms, cookie and contact pages; the footer; the founder console frame; `index.html` title, icon and fonts. Keep the energy-source colours. Record reused Boxlytics files with path and commit in THIRD_PARTY_NOTICES.md.
- `docs`: DESIGN_LOG "Round 7 (audit)", HANDOFF (sections 1, 3, 8, 9 and the test counts), API_CONTRACT tidy, the tracker (EMS_BUILD_PROGRESS items the fixes close), and merging the plan package (`docs/plan-package` branch) into the repo so the code's citations resolve.
- `deploy-ci`: GitHub Actions for both repos (Python 3.12 pytest; Node 22 `npm run gates`; the browser check against a seeded scratch app), Dockerfile + compose for the API, `.env.example` / `.env.production.example`, README/RUN_LOCALLY/DEPLOY/TROUBLESHOOTING in the Boxlytics/Solarlytics shape.

### F. Prove it is shippable
Both suites and the browser check on the final branch; the viewer money sweep (every page and API answer as viewer and as a site API key: no Rs, PKR, rates, fees); the role matrix at 1440 and 375 px; the flows (onboarding, CSV upload, plan and slot tables per brand, hold/pause/resume, savings native capture, switchback, pilot start, bill entry and rebuild, reports, alerts, members, API keys, founder console); the engine acceptance tests. Record the results in the audit document's ship gate.

### G. Pull requests
Push the branches and open pull requests into `v1-build` (never push `main` or `v1-build`). Either one PR per package (reviewable, but they depend on round 6 and on each other's shared files) or, simpler, one PR from `audit/integration` per repo plus the parity/docs PRs. State in each PR that it is stacked on `fix/offgrid-carry-over` (merge that first).

### H. The audit document and the developer message
Render `GRIDLYTICS_AUDIT.md` with `render.py` from the parts in `results/` plus a findings file built from `STATUS-IMPL.json` (each finding: status implemented / already fixed / refuted / deferred, with its commit), so the document says what was done and what remains. Then the short message to the developer pointing at each file.

## 8. Deferred findings and owner decisions

{{DEFERRED}}

Other owner tasks from the audit: re-seed or remove the stale demo in the owner's real `data/` folder (HANDOFF 8.D); buy the weather-feed licence before FOR-02 is wired; have the monthly-statement and fee wording reviewed once; name who enters and approves each new NEPRA/SRO tariff version; collect two weeks of raw inverter exports from the first customer before a pilot.

## 9. Gotchas

- **Never touch `gridlytics-backend/data`** (the owner's real data). Use a scratch folder (`--scratch`, `.scratch/data`).
- Solver tests are time-limited: run suites one at a time on a quiet machine; a laptop on battery throttles enough to fail them (HANDOFF 9).
- Sign-in stays local, preassigned accounts; `conftest.py` forces local mode in tests.
- The demo twin follows its own switchback calendar; real sites follow the live tick: several savings findings were about that difference.
- Round 6 is not merged yet; everything here is stacked on it.
- If you continue with Claude Code: the workflow scripts in `workflows/` take `args` with absolute paths (edit them); runs die at the account's usage limit and resume with `resumeFromRunId` (completed agents replay from cache). Keep parallelism moderate (about 6 agents at once) and serialise tests.
