"""make_late_plan.py - build the LATE plan (late findings only) in plan.json's schema and check coverage."""
import json, collections
A = "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit/impl"
plan = json.load(open(f"{A}/plan.json"))
wp = {w["key"]: w for w in plan["wps"]}
late = [l.split("\t") for l in open(f"{A}/index-late.tsv").read().splitlines()[1:]]
sev = {r[0]: r[1] for r in late}
FR = "flow-reports-alerts-members-keys-"
PA = "parity-architecture-"; SA = "parity-supabase-auth-"; PL = "performance-live-"; TP = "tests-and-perf-"; EM = "engine-money-live-"

EXTRA = {
"savings-engine": "Late batch: the tuned fit gets a native-like self-use candidate and values end-of-day energy as the ledger does, so the tuned baseline is never worse than native (engine-money-live-11, golden test), and the pilot page takes its switchback checks from the server (a result needs 6 days in total with both arms, as verify.switchback_result) instead of re-implementing them in switchback-rules.ts (parity-architecture-23); the late duplicates of role-owner-02, engine-savings-08, flow-savings-pilot-bill-01, pages-D-03 and engine-optimiser-14 add evidence only (the carry-over mechanism: both replays start at the actual's SoC and the next day's native replay inherits the energy credited at the peak price; the off-grid farm's month that is all carry-over; the MDI month's actual peak built from every day of the month, not only scored days).",
"tariffs": "Late batch: a genset day before the first entered fuel price is flagged 'no fuel price for this day' instead of borrowing a later price (engine-money-live-15, golden test), and the shipped-library seeding check runs once per company file and process instead of on every lookup (tests-and-perf-25); the late duplicate of flow-onboarding-01 extends the agreement dates to the bill rebuild (tariffs.py 548-553, 607-611) and prices exports after a legacy agreement's end by the regime that follows it, not at the import rate.",
"telemetry-genset-battery": "Late batch: telemetry reads use deferred (read) transactions and only writers take BEGIN IMMEDIATE (export_csv formats outside any lock), a CSV upload is parsed once in a streamed or chunked pass with bounded memory and written in day-sized transactions, the series route caps or downgrades an explicit resolution to about 2,000 points for every role, and forecast_issues keep only what skill scoring needs after a set age (the 13-month minute-row archive to Parquet, DAT-07, stays planned work).",
"alerts-reports": "Late batches: acknowledging a closed alert is refused (409), closing a still-true condition suppresses it until the condition changes or the drawer says it will be raised again, PUT /api/alerts/rules gets a request model (no 500, bounded values, every known category), Email/SMS/WhatsApp are shown as not sending yet and the quiet-hours/escalation fields carry no placeholder that reads as a set value (delivery itself stays deferred), report periods are validated (YYYY-MM, not before the site's data, not in the future), the report header says 'Grid connection not saved' instead of 'Off-grid', prints the regime in words and states an empty period or a site with no plan, and the viewer's Reports page says in words which reports the role lists; the late duplicate of pages-D-24 raises it to high (the daily sheet printed the 48-hour plan's partial last day, undated, as 'Tomorrow's slot table': print dated day tables, today's remainder and tomorrow, and ask for a day, not a month, in the dialog).",
"auth-security": "Late batches: tokens issued in the same second as a password reset or a disable are refused (sub-second or inclusive comparison, regression test), a person the founder removed leaves Members and cannot be switched on, promoted or counted against seats, Members says that OnyxLeaf adds and removes people (no invite flow: accounts stay preassigned), GET /api/users carries Solarlytics' supabase branch so an admin can set a role before first sign-in, the web app gets Solarlytics' optional owner-applied Supabase sign-in (password only: no Google, no sign-up, no invites; the VITE_SUPABASE_* settings in env.ts are shell-nav's), sign-in throttling tightens per address and per account with unknown-email tries counted, and api.py writes its log under the data folder in use so a scratch run never writes into gridlytics-backend/data (the web repository's scripts/start_local.py sets the scratch paths).",
"founder-console": "Late batch: Solarlytics' supabase/ SQL (preflight, accounts and companies, rollback) and its owner checklist are ported, renamed and marked for review only, for the optional owner-applied mode that companies._project_context and store_founder._Project already expect (preassigned accounts, no Google, no self-signup), and a suspended or pending membership is told its state and who can change it instead of 'You are not a member of that company'.",
"shell-nav": "Late batch: env.ts keeps each valid build setting when another one is invalid, refuses or warns on an unknown VITE_AUTH_MODE and accepts the modes the backend has (local, and supabase with its VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY for auth-security's optional mode), and parses the variables with plain code so full zod leaves the eager bundle.",
"savings-ui-crosscut": "Late batch: every stored plan, savings day, bill rebuild and report records version.stamp() and the monthly PDF and CSV print it (version.py's lifted Solarlytics docstring rewritten), deleting a site revokes its API keys and its reports keep the site's name marked 'site removed', Solarlytics leftovers in wording go ('quotes:read' in the key error and docstring, 'a energy only report' in the audit), recompute runs as a background job one site at a time (tests-and-perf-11, a duplicate of pr-review-05), the report CSV lists plan ids by Karachi day (from the duplicate of pages-D-06) and the off-grid energy report drops its grid rows (the duplicate of role-operator-10).",
"tests-ci": "Late batches: cross-repository contract tests through backend/tests/sibling.py and frontend/src/test/sibling.ts (types, permissions, refusal codes, re-captured fixtures, metaFor from fx.meta), the lifted guard tests (hygiene, sign-in guard, seed demo, the 7 skipped lifted tests re-aimed; test_role_walk.py is auth-security's and test_deploy_docs.py deploy-ci's), company isolation on the EMS routes and key folders, the scheduler's tick and nightly jobs, the wall-clock optimiser assertions made load-independent (quality checks, a serial marker or a deterministic budget), the Part 7.4 contract suite, the outage forecaster's announced-window outputs, the onboarding Review and activate path, and behaviour instead of constants; CI itself is deploy-ci's (stage 3).",
"docs": "Late batch: a CLAUDE.md per repository (the .scratch venv, never write to gridlytics-backend/data, --scratch on 8151/5295, branches, origin, never merge or push unasked; .claude/launch.json without the Windows-only path), the family docs set (CHANGELOG, ACCOUNTS, TROUBLESHOOTING, PRODUCT_BRIEF, EQUATIONS, ASSUMPTIONS, TEST_MATRIX, OWNER_CHECKLIST; DEPLOY.md is deploy-ci's) with the stale README figures refreshed (bringing the plan package onto v1-build is the owner's merge), and the Boxlytics constructions (founder token format, founder store and client, the founder lists' search) recorded with their Boxlytics paths and commit c429bef in both THIRD_PARTY_NOTICES files and the DESIGN_LOG, as the owner's decisions allow.",
}

BATCHES = {
"savings-engine": [("Late: tuned baseline never worse than native; switchback checks from the server", [EM+"11", PA+"23"])],
"tariffs": [("Late: fuel price before the first entry; seeding check on every lookup", [EM+"15", TP+"25"])],
"telemetry-genset-battery": [("Late: telemetry file cost: read transactions, streamed CSV upload, series cap, forecast-issue retention", [PL+"03", PL+"04", TP+"05", PL+"06"])],
"alerts-reports": [("Late: alert actions and the rule editor: closed-alert state, re-raise after close, rule validation, channels not sending", [FR+"08", FR+"10", FR+"09", FR+"11"]),
                   ("Late: reports: period validation, header and empty periods, the viewer's list", [FR+"13", FR+"16", FR+"29"])],
"auth-security": [("Late: sessions and members: same-second revocation, removed people, how people are added, Supabase-mode members list", [SA+"01", FR+"07", SA+"05", FR+"28"]),
                  ("Late: optional Supabase sign-in in the web app, the API's log folder, sign-in throttling", [SA+"04", PA+"05", SA+"08"])],
"founder-console": [("Late: owner-applied Supabase mode: project SQL for review, membership-state refusals", [SA+"03", SA+"09"])],
"shell-nav": [("Late: build settings in env.ts", [PA+"04", TP+"16"])],
"savings-ui-crosscut": [("Late: engine version on stored results, deleted sites, lifted wording", [PA+"12", FR+"14", FR+"27"])],
"tests-ci": [("Late: test infrastructure: sibling contract tests, lifted guard tests, isolation, scheduler, load-independent optimiser tests", [PA+"09", PA+"10", TP+"14", TP+"13", TP+"12"]),
             ("Late: missing suites: Part 7.4 contract suite, outage forecaster outputs, onboarding activation, behaviour not constants", [TP+"15", TP+"24", TP+"19", TP+"20"])],
"docs": [("Late: CLAUDE.md, the family docs set and Boxlytics provenance", [PA+"08", PA+"14", PA+"15"])],
"storage-perf": [("Plan store and scheduler: retention, data-folder paths, tick failures, timing and capacity", [PL+"01", PA+"06", PA+"11", PL+"09", PL+"11"]),
                 ("Server read paths and long requests: solves as jobs, /now cost and freshness, plans.active_at, tick history reads, capture photo", [PL+"05", PL+"02", TP+"10", TP+"07", TP+"09", TP+"08"]),
                 ("What the browser downloads and polls: shell fleet poll, compression, chart bundles", [PL+"08", PL+"10", PL+"12", TP+"17"])],
"deploy-ci": [("CI and deployment: workflows, Docker image and deploy settings, Vercel, release scripts, pins and audit", [PA+"01", PA+"02", PA+"03", PA+"16", PA+"20"]),
              ("Repository hygiene: secret ignore rules, lifted-code drift, lint and format gate, unused dependencies, caches", [PA+"13", PA+"07", PA+"18", PA+"19", PA+"21"])],
}

STAGE1 = [w["key"] for w in plan["wps"] if w["stage"] == 1]
NEW = {
"storage-perf": dict(
 key="storage-perf",
 title="Storage growth, read paths, scheduler capacity and payload cost (plan retention, telemetry and plan reads, tick observability, jobs, compression, bundles)",
 repos=["backend", "frontend"],
 owns_files=[
  "backend/gridlytics/plans.py (stage 2: a new prune function and the column choice of history/current/active_at; no change to build_problem or the guard)",
  "backend/gridlytics/scheduler.py (stage 2: failed-site runs rows, tick timing, the nightly prune call, bounded tick)",
  "backend/gridlytics/jobs.py",
  "backend/gridlytics/routes_plans.py (stage 2: re-solve and resume as jobs only)",
  "backend/gridlytics/routes_sites.py (stage 2: site_now and fleet_row read cost only)",
  "backend/gridlytics/battery.py (stage 2: the estimate cached per tick only)",
  "backend/gridlytics/forecast.py (stage 2: the history window read once per tick only)",
  "backend/gridlytics/telemetry.py (stage 2: column-selecting reads only)",
  "backend/gridlytics/sites.py (stage 2: native_latest columns only)",
  "backend/gridlytics/reports.py (stage 2: file paths relative to the data folder only)",
  "backend/gridlytics/delivery.py (stage 2: the key path resolved through the data folder only)",
  "backend/gridlytics/backups.py (stage 2: reports/ in the copy)",
  "backend/gridlytics/alerts.py (stage 2: plan_failed and tick-overrun rules only)",
  "backend/gridlytics/store.py (connection reuse and indexes; a migration renumbered at integration)",
  "backend/gridlytics/api.py (stage 2: GZipMiddleware only)",
  "frontend/src/components/layout/ (stage 2: the site switcher's light poll only)",
  "frontend/src/components/charts/spark-bars.tsx (stage 2: SVG sparkline)",
  "frontend/src/components/charts/echarts-setup.ts (stage 2: registered modules)",
  "frontend/vite.config.ts (chunk-size warning)",
  "backend/tests/test_storage_perf.py (new)"],
 stage=2,
 depends_on=STAGE1,
 notes=("New package for late findings: plan-row retention, telemetry read cost, tick observability and capacity, long requests and payload cost cut across files owned by several stage-1 packages (plans.py, scheduler.py, routes_sites.py, telemetry.py, battery.py, forecast.py, api.py, the shell), so they run once on the integrated stage-1 branch instead of in optimiser-plans or fleet-overview. "
  "Stage 2, in parallel with savings-ui-crosscut and tests-ci: change only the functions named in owns_files; routes_savings.py is savings-ui-crosscut's, which moves recompute onto the same jobs.py pattern (tests-and-perf-11 under pr-review-05). "
  "Plan store: retention per spec Part 3.3 (superseded plans' steps and slots kept 90 days, then only the first plan of each hour, the plan active at each step and applied plans), run from the nightly job, never removing a row verify's replays, the reports or the applied log read (test: savings and report figures equal before and after a prune); readers select the columns they use (history, current, fleet_row); plans.active_at fetches the plans covering a range once per request; indexes on plans(site_id, belief_time), plan_messages(site_id, sent_at) and acks(site_id, received_at). Back up before any schema change. "
  "Data folder: report files and site keys stored relative to the data folder and resolved through platform.data_dir(), so a moved folder still serves downloads and never silently makes a new key; reports/ goes into the backup copy beside keys/ (the key-path and key-backup halves are control-edge's engine-delivery-13 and founder-console's; build on them). "
  "Scheduler: a failing site tick writes a failed runs row, raises the plan_failed alert and still evaluates alerts, so the page's 'first plan within 15 minutes' gives way to the failure; scheduled ticks record start, end and per-site durations apart from manual re-solves (a re-solve no longer overwrites the tick's row) and log one line per tick; an overrun raises an alert; the tick stays well under the quarter at the documented sites per core (staggering or a small worker pool, DLV-09); the deterministic work limit and idle re-solve of HANDOFF 8.A.3 stay out. "
  "Requests: re-solve and resume answer 202 and run as jobs with progress (jobs.py); /now reads the header's state from indexed reads and a battery estimate stored per tick (no 30-day battery.state per poll) and re-aggregates today's day row when it is older than the last minute, so edge sites' 'today' tiles move (tests-and-perf-10); the tick reads its history window once with only the columns each forecaster needs and shares it; native_latest selects explicit columns with has_photo; one connection per request (no PRAGMA round trips per connection). "
  "Web: the shell's site switcher polls a light list carrying the state dot and last-data age instead of /fleet; GZipMiddleware(minimum_size=1000); an SVG sparkline instead of Recharts, keeping fleet-overview's cross-row scale and noise floor; ECharts registers only the charts and components used, the chunk-size warning is back on and chunk sizes go in GATES. "
  "Each change gets a test that fails without it; timings are measured and reported in the pull request, never asserted as wall-clock limits. Never run against the owner's data folder. Shared additive (append only): types/api.ts, lib/api/endpoints.ts, lib/api/keys.ts, API_CONTRACT.md, BACKEND_ASKS.md, schemas.py, new test fixture files; leave HANDOFF.md/DESIGN_LOG.md to the docs WP.")),
"deploy-ci": dict(
 key="deploy-ci",
 title="Deployment, CI and repository shape: workflows, Docker and deploy settings, Vercel, secret ignore rules, lint gate, dependencies, lifted-code drift",
 repos=["backend", "frontend"],
 owns_files=[
  ".github/workflows/ci.yml (new, both repositories)",
  "Dockerfile, .dockerignore, .env.production.example (new, backend repository)",
  "docs/DEPLOY.md (new, backend repository)",
  "backend/tests/test_deploy_docs.py (new)",
  "backend/tests/test_lift_drift.py and backend/lift_manifest.json (new)",
  "scripts/build_release.py, scripts/load_check.py (new, backend repository)",
  "frontend/vercel.json (new)",
  "frontend/scripts/deploy_check.mjs (new)",
  "frontend/eslint.config.js, frontend/.prettierrc (new)",
  "frontend/package.json and frontend/package-lock.json (scripts, devDependencies, removed dependencies)",
  "requirements.txt, requirements-dev.txt (backend repository)",
  ".gitignore (both repositories)",
  "backend/gridlytics/founder.py (stage 3: founder_file()'s backend-root .env.founder fallback only)",
  "THIRD_PARTY_NOTICES.md (both repositories: the dependency lists only; the Boxlytics section is docs')"],
 stage=3,
 depends_on=["tests-ci", "savings-ui-crosscut", "storage-perf"],
 notes=("New package for late findings: the original plan left CI and deployment (DEP-01..07) out of tests-ci as planned work, and no package owns the repository shape (workflows, Docker, Vercel, ignore rules, lint, pins), so it runs last, once tests-ci has made the suites deterministic and storage-perf has settled the server settings. "
  "Stage 3, in parallel with parity-ui and docs. CI (.github/workflows/ci.yml in both repositories, the family's shape): the backend on every push and pull request with Python 3.12 and pytest -n auto, the web repository checked out beside it with a read-only FRONTEND_READ_TOKEN (the owner adds the secret: write it in BACKEND_ASKS as an owner task), pip-audit; the frontend with tsc, vitest, build, lint and format:check and npm audit; the browser check as its own job. "
  "Deployment (the Solarlytics S0b shape): Dockerfile on python:3.12-slim-bookworm, API only, non-root, a /data volume (platform.db, companies/, keys/, reports/, backup/, logs/), HEALTHCHECK on /api/health, one uvicorn worker so one scheduler runs; .dockerignore; .env.production.example naming every GRIDLYTICS_* setting the code reads (the service-role key server-only, the founder console's 503 until its four secrets are set, Supabase only as the optional owner-applied mode with preassigned accounts, no Google, no self-signup), enforced by test_deploy_docs.py; docs/DEPLOY.md with compose and reverse-proxy notes (TLS, TRUST_PROXY, hosts and CORS); scripts/build_release.py for the GRIDLYTICS_WEB_DIST release and scripts/load_check.py (restore_backup.py is founder-console's and export_contract.py control-edge's; no fetch_tariffs/fetch_schedules scraping of NEPRA/OGRA). "
  "Web hosting: frontend/vercel.json (vite, npm ci, a deploy check before and after the build that refuses an unset or unknown VITE_AUTH_MODE or API base, an SPA rewrite of every non-asset path, a CSP whose connect-src names the API host plus the security headers, asset caching). "
  "Hygiene: both .gitignore files ignore .env, .env.*, !.env*.example, data/platform.secret, demo/*.local.md, __pycache__/, *.pyc and .pytest_cache/, and the launcher's backend-root .env.founder fallback goes; eslint 9 flat config (typescript-eslint, react-hooks exhaustive-deps, import order), prettier 3 with the Tailwind plugin and a coverage threshold from 60 % of lines, all in npm run gates and CI (fix what they flag, in parity-ui's files after parity-ui lands; a rule left off is written in BUILD_NOTES); pandas, date-fns and @radix-ui/react-popover removed and the notices' dependency lists matched; the shared sign-in stack pinned as in solarlytics-backend once both suites pass on it. "
  "Lifted code: a manifest of every module lifted from Solarlytics (path, commit, rename map) and a drift test against a read-only sibling checkout when one is present (skipped otherwise); shared onyxleaf-platform/@onyxleaf/auth packages are an owner decision, written in BACKEND_ASKS. "
  "Never change Solarlytics or Boxlytics; never push or merge: the owner opens the pull requests. Tests never use the owner's data folder.")),
}

DUPS = [
 ("role-owner-02", [EM+"01"], "Faisalabad's whole saving is carry-over: both replays start at the actual's SoC, the day's end energy is credited at the peak price while the next day's native replay inherits it (verify.py 641-675, 733); Part 8.2 prices carry-over only at switchback block boundaries."),
 ("engine-savings-08", [EM+"02"], "The off-grid farm claims Rs 12,581 this month, all carry-over, while burning more diesel than native."),
 ("flow-savings-pilot-bill-01", [EM+"03"], "The MDI lever follows the chosen range; the month's actual peak must also come from every day of the month, not only scored days (FSD 26 Oct)."),
 ("flow-onboarding-01", [EM+"04"], "Legacy net-metering end date ignored in the vectors and in the bill rebuild (tariffs.py 548-553, 607-611): after expiry exports are priced by the regime that follows (net billing Rs 8.13), not at the import rate."),
 ("flow-savings-pilot-bill-02", [EM+"05"], "Same Karachi Rs 1,956 (fleet, overview) vs Rs 43,594 (savings, report, fleet-savings API)."),
 ("pages-D-03", [EM+"06"], "Month-to-date books the whole month's MDI reduction while its daily holding cost accrues."),
 ("engine-optimiser-14", [EM+"07"], "Bound below the claim and negative (KHI Nov Rs 615, LHR Nov -Rs 7,314)."),
 ("flow-savings-pilot-bill-08", [EM+"08"], "Bill comparison counts missing meter data as zero while the page says the gaps were filled."),
 ("frontend-states-formats-01", [EM+"09"], "Karachi's captive-gas MMBtu shown as diesel litres in savings-kpis.tsx."),
 ("engine-tariffs-05", [EM+"10"], "Tax flags and the ICP baseline cannot be set in the web app."),
 ("pages-D-01", [EM+"12"], "Fee check labels the tuned figure 'against the native settings' and extrapolates 1-4 days to a month."),
 ("pages-D-06", [EM+"13", FR+"04"], "Savings CSV omits MDI, levers, carry-over, baseline peak and version ids; the report CSV has no tariff or capture version and its plan_ids are the first four plans by UTC date (reports.py:152): list plan ids by Karachi day."),
 ("role-analyst-14", [EM+"14"], "Lahore's -Rs 0.09 drawn as a full-height red bar."),
 ("pages-D-24", [FR+"01", FR+"19"], "Raises pages-D-24 to high: at 17:11 the sheet printed the 48-hour plan's partial last day (hold 20 % all day) undated as 'Tomorrow's slot table'; print dated day tables (today's remainder and tomorrow, spec line 582), and the dialog asks for a day, not a month, with no Preliminary hint."),
 ("frontend-states-formats-05", [FR+"02"], "Empty Reports page promises an automatic report on the 3rd; schedules (UI-14) stay deferred."),
 ("pages-D-07", [FR+"03", FR+"05"], "No company roll-up or pilot report (pilot kind answers 422), no browser preview (pages-D-25's half), and the PDF lacks forecast skill, control health, switchback and carry-over, bill comparison and outage coverage, and does not state a partial-data period."),
 ("founder-console-06", [FR+"06"], "Both no_seat and member_disabled render as 'not available for your role'."),
 ("security-roles-07", [FR+"12"], "sites:read reads plans and telemetry; plans:read cannot read /plan."),
 ("role-analyst-25", [FR+"15"], "Report file names omit the site."),
 ("engine-savings-13", [FR+"17"], "The PDF's Preliminary sentence must name the condition that applies, including the 8-week switchback one; litres marked Estimated and no '-0 L' (with pages-D-08)."),
 ("role-operator-20", [FR+"18"], "Report route checks only see_costs, not export."),
 ("role-operator-09", [FR+"20"], "At 375 px the Reports, API keys and Members tables hide PDF/CSV, Revoke and Switch off."),
 ("role-analyst-20", [FR+"21"], "'The API keys is not available for your role'."),
 ("pages-A-13", [FR+"22"], "Settings and Members both highlighted on every Company settings tab."),
 ("frontend-a11y-07", [FR+"23"], "Member role select saves on change, including promotion to Administrator."),
 ("frontend-a11y-16", [FR+"24"], "One-time key dialog closes on Escape; the 'Role on record' half was refuted in verification as pages-A-16, so change that column only if the page text contradicts it."),
 ("pages-D-16", [FR+"25"], "'ends on 2020-01-01' for long-ended agreements; the drawer's raw rule and connection ids are role-viewer-17's half."),
 ("role-operator-31", [FR+"26"], "Free-text assignee; the API also stores any string: validate against members."),
 ("role-operator-10", [FR+"30"], "The off-grid farm's energy report prints grid import/export, peak import 0.0 kW and 'Outage time 168 h'."),
 ("pages-A-02", [SA+"02"], "'Forgotten your password? Ask your company's administrator' names someone with no reset path."),
 ("parity-architecture-02", [SA+"06"], "Late-late duplicate: no .env.production.example, DEPLOY.md or test_deploy_docs.py."),
 ("parity-architecture-13", [SA+"07"], "Late-late duplicate: .env files not ignored in the backend."),
 ("parity-architecture-08", [SA+"10"], "Late-late duplicate: Boxlytics reuse not recorded with path and commit."),
 ("parity-architecture-05", [PL+"07"], "Late-late duplicate; also backups.daily_all copies Path(store.DB_PATH), the default database, every day."),
 ("tests-and-perf-05", [PL+"13"], "Late-late duplicate: series exceed the ~2,000-point cap (2,880 at 2 days / 1 min, 2,881 at 30 days / 15 min)."),
 ("security-roles-01", [TP+"01"], "Events route returns fuel-log cost_rs to viewers and keys."),
 ("tracker-and-docs-03", [TP+"02"], "The money-filter test is not the every-route role walk; test_role_walk.py does not exist."),
 ("engine-tariffs-07", [TP+"03"], "No hand-worked golden bill."),
 ("performance-live-03", [TP+"04"], "Late-late duplicate: BEGIN IMMEDIATE on every telemetry read."),
 ("performance-live-01", [TP+"06"], "Late-late duplicate; adds plans.history's SELECT * and the plan_messages/acks indexes."),
 ("pr-review-05", [TP+"11"], "Recompute runs up to 62 days of replays and bound solves inside one request: run it as a background job (jobs.py), one per site, at low priority beside the ticks."),
 ("parity-architecture-09", [TP+"18"], "Late-late duplicate: fixtures predate rounds 3-5 and the role map is hard-coded; re-capture them and check them through sibling.ts."),
 ("performance-live-11", [TP+"21"], "Late-late duplicate: sequential tick; an overrun silently skips the next tick."),
 ("performance-live-10", [TP+"22"], "Late-late duplicate: no gzip on large JSON answers."),
 ("performance-live-04", [TP+"23"], "Late-late duplicate: upload read whole and parsed twice, one locking write; the unreachable 30 MB sentence is flow-telemetry-plan-control-25's."),
]
DEFERRED = [
 (PA+"17", "Owner decision: one frontend toolchain and a shared @onyxleaf/ui package across Boxlytics, Solarlytics and Gridlytics would change the other products (Boxlytics belongs to another party and is never changed); Gridlytics' own look is parity-ui's (stage 3) and react-hook-form's absence is recorded in BUILD_NOTES by the docs WP."),
 (PA+"22", "Restates planned HANDOFF 8.C work with its plan 10.1 design: the independent verification harness (VER-05, VER-06, VER-07: python-microgrid replay and the REopt-style case with their own requirements and weekly job) and the MILP benchmark (DEP-02); the default-suite 192-step check is tests-ci's (pr-review-01)."),
]

out_wps = []
order = [w["key"] for w in plan["wps"]] + ["storage-perf", "deploy-ci"]
for k in order:
    if k not in BATCHES: continue
    if k in NEW:
        w = dict(NEW[k])
    else:
        o = wp[k]
        w = dict(key=o["key"], title=o["title"], repos=o["repos"], owns_files=o["owns_files"], stage=o["stage"], depends_on=o["depends_on"], notes=o["notes"].rstrip() + " " + EXTRA[k])
    w["batches"] = [dict(theme=t, ids=ids) for t, ids in BATCHES[k]]
    out_wps.append(dict(key=w["key"], title=w["title"], repos=w["repos"], owns_files=w["owns_files"], stage=w["stage"], depends_on=w["depends_on"], batches=w["batches"], notes=w["notes"]))

# checks
seen = collections.Counter()
rank = {"high": 0, "medium": 1, "low": 2}
for w in out_wps:
    for b in w["batches"]:
        r = [rank[sev[i]] for i in b["ids"]]
        assert r == sorted(r), (w["key"], b["theme"], b["ids"])
        for i in b["ids"]: seen[i] += 1
orig_loc = {}
for w in plan["wps"]:
    for b in w["batches"]:
        for i in b["ids"]: orig_loc[i] = w["key"]
orig_dup = {i: d["canonical"] for d in plan["duplicates"] for i in d["dups"]}
batch_ids = {i for w in out_wps for b in w["batches"] for i in b["ids"]}
for c, ds, n in DUPS:
    assert c not in orig_dup, ("canonical is itself a dup", c)
    assert c in orig_loc or c in batch_ids, ("canonical not in a batch", c)
    for i in ds: seen[i] += 1
for i, r in DEFERRED: seen[i] += 1
ids = list(sev)
missing = [i for i in ids if seen[i] == 0]; twice = [i for i, n in seen.items() if n > 1]; unknown = [i for i in seen if i not in sev]
print(f"{len(ids)} late ids; missing {missing}; twice {twice}; unknown {unknown}")
for w in out_wps:
    print(f"  s{w['stage']} {w['key']:26s} " + " | ".join(f"{len(b['ids'])}" for b in w["batches"]))
nb = sum(len(b["ids"]) for w in out_wps for b in w["batches"]); nd = sum(len(d[1]) for d in DUPS)
print("batched", nb, "dups", nd, "groups", len(DUPS), "deferred", len(DEFERRED))
summary = (f"116 late findings: {nb} in {sum(len(w['batches']) for w in out_wps)} late batches across {len(out_wps)} packages, {nd} duplicates in {len(DUPS)} groups, {len(DEFERRED)} deferred. "
 "Duplicates of original findings point at the original canonical (none of them is itself a duplicate in plan.json); late findings that repeat another late finding point at the late canonical in a batch (parity-architecture-02/05/08/09/13, performance-live-01/03/04/10/11, tests-and-perf-05). "
 "The money findings of engine-money-live are almost all repeats of round-7 originals (carry-over, MDI range, fee card, CSV, bound, gas units, tax flags, sparkline, agreement dates); their notes carry the extra evidence. New in existing packages: savings-engine (tuned baseline worse than native; switchback rule re-implemented in the browser), tariffs (fuel price before the first entry; per-lookup seeding check), telemetry-genset-battery (read transactions, CSV memory, series cap, forecast-issue size), alerts-reports (closed-alert acknowledge, re-raise after close, rule validation, channels shown live, report period/header/viewer wording), auth-security (same-second token revocation, removed members, Members wording, Supabase-mode members list and the optional web sign-in under the owner's rule, throttling, the scratch log written into the owner's data folder), founder-console (Supabase SQL ported for review only; membership-state wording), shell-nav (env.ts), savings-ui-crosscut (engine version on stored results, deleted sites, lifted wording), tests-ci (sibling contract tests, lifted guard tests, isolation, scheduler, flaky timing tests, the 7.4 contract suite, forecaster and onboarding tests) and docs (CLAUDE.md, the family docs set, Boxlytics provenance). "
 "Two new packages: storage-perf (stage 2, after every stage-1 package, parallel with savings-ui-crosscut and tests-ci) because plan retention, telemetry read cost, tick observability and capacity, long requests and payload cost cut across files of several stage-1 owners; deploy-ci (stage 3, after tests-ci, savings-ui-crosscut and storage-perf, parallel with parity-ui and docs) because CI and deployment were left out of tests-ci and nothing owns the repository shape. "
 "Accounts stay preassigned (no Google, no self-signup); Supabase stays an optional owner-applied mode as in solarlytics-backend. Deferred: parity-architecture-17 (owner decision on a family toolchain and shared UI package) and parity-architecture-22 (planned HANDOFF 8.C verification and benchmark). "
 "Some late batches have fewer than 4 ids because the package received only 2-3 related late findings and moving them would break one-owner-per-file; the late batches of stage-1 packages run after their planned batches in the same worktree.")
res = dict(wps=out_wps, duplicates=[dict(canonical=c, dups=d, note=n) for c, d, n in DUPS], deferred=[dict(id=i, reason=r) for i, r in DEFERRED], summary=summary)
json.dump(res, open(f"{A}/plan-late.json", "w"), indent=1)
print("written", f"{A}/plan-late.json")
