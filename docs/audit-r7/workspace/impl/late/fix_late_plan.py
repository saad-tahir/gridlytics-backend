"""fix_late_plan.py - critic corrections to the late-plan draft (plan-late-draft.json -> plan-late-corrected.json)."""
import json
A = "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit/impl"
p = json.load(open(f"{A}/plan-late-draft.json"))
plan = json.load(open(f"{A}/plan.json"))
pw = {w["key"]: w for w in plan["wps"]}
w = {x["key"]: x for x in p["wps"]}
FR = "flow-reports-alerts-members-keys-"; PA = "parity-architecture-"; SA = "parity-supabase-auth-"; PL = "performance-live-"; TP = "tests-and-perf-"

def set_extra(key, extra):
    w[key]["notes"] = pw[key]["notes"] + " " + extra

# 1. alerts-reports: parity-architecture-11 (reports.py, theirs in stage 1) joins the reports batch
w["alerts-reports"]["batches"] = [
    {"theme": "Late: alert actions and the rule editor: closed-alert state, re-raise after close, rule validation, channels not sending",
     "ids": [FR+"08", FR+"10", FR+"09", FR+"11"]},
    {"theme": "Late: reports: file paths under the data folder, period validation, header and empty periods, the viewer's list",
     "ids": [PA+"11", FR+"13", FR+"16", FR+"29"]},
]
set_extra("alerts-reports", "Late batches: acknowledging a closed alert is refused (409), closing a still-true condition suppresses it until the condition changes or the drawer says it will be raised again, PUT /api/alerts/rules gets a request model (no 500, bounded values, every known category), Email/SMS/WhatsApp are shown as not sending yet and the quiet-hours/escalation fields carry no placeholder that reads as a set value (delivery itself stays deferred), report files are stored relative to the data folder and resolved through platform.data_dir() so a moved or restored folder still serves every stored download (parity-architecture-11; its signing-key half is control-edge's engine-delivery-13 and reports/ in the backup copy is founder-console's), report periods are validated (YYYY-MM, not before the site's data, not in the future), the report header says 'Grid connection not saved' instead of 'Off-grid', prints the regime in words and states an empty period or a site with no plan, and the viewer's Reports page says in words which reports the role lists; the late duplicate of pages-D-24 raises it to high (the daily sheet printed the 48-hour plan's partial last day, undated, as 'Tomorrow's slot table': print dated day tables, today's remainder and tomorrow, and ask for a day, not a month, in the dialog).")

# 2. auth-security: one batch of 7 instead of 4 + 3
w["auth-security"]["batches"] = [
    {"theme": "Late: sessions and sign-in: same-second revocation, removed members, the optional owner-applied Supabase mode (web sign-in, members list), the API's log folder, throttling, how people are added",
     "ids": [SA+"01", FR+"07", SA+"04", PA+"05", SA+"05", SA+"08", FR+"28"]},
]
set_extra("auth-security", "Late batch: tokens issued in the same second as a password reset or a disable are refused (sub-second or inclusive comparison, regression test), a person the founder removed leaves Members and cannot be switched on, promoted or counted against seats, Members says that OnyxLeaf adds and removes people (no invite flow: accounts stay preassigned), GET /api/users carries Solarlytics' supabase branch so an admin can set a role before first sign-in, the web app gets Solarlytics' optional owner-applied Supabase sign-in (password only: no Google, no sign-up, no invites; the VITE_SUPABASE_* settings in env.ts are shell-nav's), sign-in throttling tightens per address and per account with unknown-email tries counted, and api.py writes its log under the data folder in use so a scratch run never writes into gridlytics-backend/data (the web repository's scripts/start_local.py also sets GRIDLYTICS_DB beside GRIDLYTICS_DATA_DIR, as Solarlytics' launcher does).")

# 3. founder-console: the backup halves of parity-architecture-11/-16 and performance-live-07
set_extra("founder-console", "Late batch: Solarlytics' supabase/ SQL (preflight, accounts and companies, rollback) and its owner checklist are ported, renamed and marked for review only, for the optional owner-applied mode that companies._project_context and store_founder._Project already expect (preassigned accounts, no Google, no self-signup), and a suspended or pending membership is told its state and who can change it instead of 'You are not a member of that company'; besides, the daily copy takes the company's reports/ folder beside keys/ and restore_backup.py puts both back with the database and telemetry (the backup halves of parity-architecture-11 and -16), and backups.daily_all copies the default database only when it lies in the data folder in use, so a scratch run never writes a backup into gridlytics-backend/data (performance-live-07's half).")

# 4. tests-ci: the scheduler test moves to storage-perf (which changes the tick's failure path in the same stage)
w["tests-ci"]["batches"] = [
    {"theme": "Late: test infrastructure: sibling contract tests, lifted guard tests, company isolation, load-independent optimiser tests",
     "ids": [PA+"09", PA+"10", TP+"14", TP+"12"]},
    {"theme": "Late: missing suites: Part 7.4 contract suite, outage forecaster outputs, onboarding activation, behaviour not constants",
     "ids": [TP+"15", TP+"24", TP+"19", TP+"20"]},
]
set_extra("tests-ci", "Late batches: cross-repository contract tests through backend/tests/sibling.py and frontend/src/test/sibling.ts (types, permissions, refusal codes, re-captured fixtures, metaFor from fx.meta), the lifted guard tests (hygiene, sign-in guard, seed demo, the 7 skipped lifted tests re-aimed; test_role_walk.py is auth-security's and test_deploy_docs.py deploy-ci's), company isolation on the EMS routes and key folders, the wall-clock optimiser assertions made load-independent (quality checks, a serial marker or a deterministic budget), the Part 7.4 contract suite on control-edge's contract models and schema files (brief-vs-build-12) with per-brand Mode A grammar checks, the outage forecaster's announced-window outputs, the onboarding Review and activate path, and behaviour instead of constants (the fallback parametrisations go; the routes_plans._held shim they keep alive is product code, removed by storage-perf), while the scheduler test (tests-and-perf-13) is storage-perf's because that package changes the tick's failure path in the same stage, and CI itself is deploy-ci's (stage 3).")

# 5. docs: PLT-02 now becomes true through deploy-ci
set_extra("docs", "Late batch: a CLAUDE.md per repository (the .scratch venv, never write to gridlytics-backend/data, --scratch on 8151/5295, branches, origin, never merge or push unasked; .claude/launch.json without the Windows-only path), the family docs set (CHANGELOG, ACCOUNTS, TROUBLESHOOTING, PRODUCT_BRIEF, EQUATIONS, ASSUMPTIONS, TEST_MATRIX, OWNER_CHECKLIST; DEPLOY.md is deploy-ci's) with the stale README figures refreshed (bringing the plan package onto v1-build is the owner's merge), and the Boxlytics constructions (founder token format, founder store and client, the founder lists' search) recorded with their Boxlytics paths and commit c429bef in both THIRD_PARTY_NOTICES files and the DESIGN_LOG, as the owner's decisions allow; PLT-02 (tracker-and-docs-12, with its late duplicate parity-supabase-auth-06) becomes true when deploy-ci adds .env.production.example, DEPLOY.md and test_deploy_docs.py, so tick it only once those have landed and annotate it otherwise.")

# 6. savings-engine: the bound's resolution mismatch from engine-money-live-07
set_extra("savings-engine", "Late batch: the tuned fit gets a native-like self-use candidate and values end-of-day energy as the ledger does, so the tuned baseline is never worse than native (engine-money-live-11, golden test), and the pilot page takes its switchback checks from the server (a result needs 6 days in total with both arms, as verify.switchback_result) instead of re-implementing them in switchback-rules.ts (parity-architecture-23); the late duplicates of role-owner-02, engine-savings-08, flow-savings-pilot-bill-01, pages-D-03 and engine-optimiser-14 add evidence (the carry-over mechanism: both replays start at the actual's SoC and the next day's native replay inherits the energy credited at the peak price; the off-grid farm's month that is all carry-over; the MDI month's actual peak built from every day of the month, not only scored days; the perfect-foresight bound solved at 15 minutes from the day's month-to-date M0 against a native arm priced per minute, so compare both on the same 1-minute basis and levers).")

# 7. storage-perf: no data-folder paths (alerts-reports, control-edge, founder-console own them), the scheduler test, the _held shim
sp = w["storage-perf"]
sp["batches"] = [
    {"theme": "Plan store and scheduler: plan retention, tick failures and a scheduler test, tick timing and capacity",
     "ids": [PL+"01", PA+"06", TP+"13", PL+"09", PL+"11"]},
    {"theme": "Server read paths and long requests: solves as jobs, /now cost and freshness, plans.active_at, tick history reads, capture photo",
     "ids": [PL+"05", PL+"02", TP+"10", TP+"07", TP+"09", TP+"08"]},
    {"theme": "What the browser downloads and polls: shell fleet poll, compression, chart bundles",
     "ids": [PL+"08", PL+"10", PL+"12", TP+"17"]},
]
sp["owns_files"] = [
    "backend/gridlytics/plans.py (stage 2: a new prune function and the column choice of history/current/active_at; no change to build_problem or the guard)",
    "backend/gridlytics/scheduler.py (stage 2: failed-site runs rows, tick timing, the nightly prune call, bounded tick)",
    "backend/gridlytics/jobs.py",
    "backend/gridlytics/routes_plans.py (stage 2: re-solve and resume as jobs, and _held's getattr/TypeError fallback removed, only)",
    "backend/gridlytics/routes_sites.py (stage 2: site_now, fleet_row and the site list's light fields only)",
    "backend/gridlytics/battery.py (stage 2: the estimate cached per tick only)",
    "backend/gridlytics/forecast.py (stage 2: the history window read once per tick only)",
    "backend/gridlytics/telemetry.py (stage 2: column-selecting reads only)",
    "backend/gridlytics/sites.py (stage 2: native_latest columns only)",
    "backend/gridlytics/alerts.py (stage 2: plan_failed and tick-overrun rules only)",
    "backend/gridlytics/store.py (connection reuse and indexes; a migration renumbered at integration)",
    "backend/gridlytics/api.py (stage 2: GZipMiddleware only)",
    "frontend/src/components/layout/ (stage 2: the site switcher's light poll only)",
    "frontend/src/components/charts/spark-bars.tsx (stage 2: SVG sparkline)",
    "frontend/src/components/charts/echarts-setup.ts (stage 2: registered modules)",
    "frontend/vite.config.ts (chunk-size warning)",
    "backend/tests/test_storage_perf.py (new)",
    "backend/tests/test_scheduler.py (new)",
]
sp["notes"] = (
 "New package for late findings: plan-row retention, telemetry read cost, tick failures, observability and capacity, long requests and payload cost cut across files owned by several stage-1 packages (plans.py and scheduler.py split between control-edge and optimiser-plans, routes_plans.py, routes_sites.py, telemetry.py, battery.py, forecast.py, sites.py, alerts.py, api.py, store.py, the shell and two chart files), so they run once on the integrated stage-1 branch instead of in optimiser-plans or fleet-overview. "
 "Stage 2, in parallel with savings-ui-crosscut and tests-ci: change only the functions named in owns_files; routes_savings.py is savings-ui-crosscut's, which moves recompute onto the same jobs.py pattern (tests-and-perf-11 under pr-review-05), and reports.py is savings-ui-crosscut's in this stage. "
 "Plan store: retention per spec Part 3.3 (superseded plans' steps and slots kept 90 days, then only the first plan of each hour, the plan active at each step and applied plans), run from the nightly job, never removing a row verify's replays, the reports or the applied log read (test: savings and report figures equal before and after a prune); readers select the columns they use (history, current, fleet_row); plans.active_at fetches the plans covering a range once per request; indexes on plans(site_id, belief_time), plan_messages(site_id, sent_at) and acks(site_id, received_at). Back up before any schema change. "
 "The data-folder paths are not here: report files are alerts-reports' (parity-architecture-11, stage 1), the signing keys control-edge's (engine-delivery-13) and the keys/ and reports/ backup copies founder-console's. "
 "Scheduler: a failing site tick writes a failed runs row, raises the plan_failed alert and still evaluates alerts, so the page's 'first plan within 15 minutes' gives way to the failure; scheduled ticks record start, end and per-site durations apart from manual re-solves (a re-solve no longer overwrites the tick's row) and log one line per tick; an overrun raises an alert; the tick stays well under the quarter at the documented sites per core (staggering or a small worker pool, DLV-09); the deterministic work limit and idle re-solve of HANDOFF 8.A.3 stay out. "
 "Scheduler test (tests-and-perf-13, moved here from tests-ci because this package changes the tick's failure path in the same stage): two companies with a few sites each, one raising, through tick_company and nightly_company: plans, runs and savings_days rows land in each company's own file, the failing site leaves a failed runs row and does not stop the others, a demo site advances before its tick, 'yesterday' is the PKT day and aggregate_day gets the peak mask. "
 "Requests: re-solve and resume answer 202 and run as jobs with progress (jobs.py); routes_plans._held drops its getattr/TypeError fallback now that plans.held_info always exists (the product half of tests-and-perf-20, whose test half is tests-ci's); /now reads the header's state from indexed reads and a battery estimate stored per tick (no 30-day battery.state per poll) and re-aggregates today's day row when it is older than the last minute, so edge sites' 'today' tiles move (tests-and-perf-10); the tick reads its history window once with only the columns each forecaster needs and shares it; native_latest selects explicit columns with has_photo; one connection per request (no PRAGMA round trips per connection). "
 "Web: the shell's site switcher polls a light list (the site list gains the state dot and last-data age) instead of /fleet; GZipMiddleware(minimum_size=1000); an SVG sparkline instead of Recharts, keeping fleet-overview's cross-row scale and noise floor; ECharts registers only the charts and components used, the chunk-size warning is back on and chunk sizes go in GATES. "
 "Each change gets a test that fails without it; timings are measured and reported in the pull request, never asserted as wall-clock limits. Never run against the owner's data folder. Shared additive (append only): types/api.ts, lib/api/endpoints.ts, lib/api/keys.ts, API_CONTRACT.md, BACKEND_ASKS.md, schemas.py, new test fixture files; leave HANDOFF.md/DESIGN_LOG.md to the docs WP.")

# 8. deploy-ci: after parity-ui (lint/format fixes and package.json on parity-ui's files), parallel with docs
dc = w["deploy-ci"]
dc["depends_on"] = ["tests-ci", "savings-ui-crosscut", "storage-perf", "parity-ui"]
n = dc["notes"]
n = n.replace("so it runs last, once tests-ci has made the suites deterministic and storage-perf has settled the server settings. Stage 3, in parallel with parity-ui and docs.",
              "so it runs last, once tests-ci has made the suites deterministic, storage-perf has settled the server settings and parity-ui has restyled the web files the lint and format gate will touch. Stage 3, after parity-ui and in parallel with docs: append only to BACKEND_ASKS.md and BUILD_NOTES.md (docs owns the rest of them) and touch only the dependency lists of THIRD_PARTY_NOTICES.md.")
n = n.replace("enforced by test_deploy_docs.py;", "enforced by test_deploy_docs.py (this meets PLT-02: tracker-and-docs-12 and its late duplicate parity-supabase-auth-06);")
n = n.replace("(fix what they flag, in parity-ui's files after parity-ui lands; a rule left off is written in BUILD_NOTES)",
              "(fix what they flag, parity-ui's restyled files included; a rule left off is written in BUILD_NOTES)")
n = n.replace("(restore_backup.py is founder-console's and export_contract.py control-edge's;",
              "(restore_backup.py, which also puts back keys/ and reports/, is founder-console's and export_contract.py control-edge's;")
assert n.count("PLT-02") == 1 and "after parity-ui and in parallel with docs" in n and "parity-ui's restyled files included" in n and "also puts back keys/" in n
dc["notes"] = n

# 9. duplicates
for d in p["duplicates"]:
    if d["dups"] == [SA+"06"]:
        d["canonical"] = "tracker-and-docs-12"
        d["note"] = ("Same defect as the original: PLT-02 is ticked while .env.production.example and test_deploy_docs.py do not exist and about 30 GRIDLYTICS_* settings are documented nowhere; the late finding adds DEPLOY.md, the service-role key being server-only and the founder console's 503 until its four secrets are set. "
                     "deploy-ci writes the files under parity-architecture-02, and docs ticks PLT-02 once they land.")
    elif d["canonical"] == "engine-optimiser-14":
        d["note"] = ("Bound below the claim and negative (KHI Nov Rs 615 against Rs 43,594; LHR Nov -Rs 7,314). Besides the missing MDI term and the 5 s limit, the bound is solved at 15 minutes from the day's month-to-date M0 and compared with a native arm priced per minute: "
                     "compare both on the same 1-minute basis and levers (written in savings-engine's late note).")
    elif d["canonical"] == "pages-A-02":
        d["note"] = ("'Forgotten your password? Ask your company's administrator' names someone with no reset path; the late finding adds that another member's name or email can be changed nowhere (PUT /api/users refuses them, the founder's PATCH takes only disabled and name), "
                     "so the hint names who can help under preassigned accounts (OnyxLeaf), as auth-security's sign-in hint item says.")
    elif d["dups"] == [PL+"07"]:
        d["note"] = ("Late-late duplicate of the log half (api.py, auth-security). Its other half, backups.daily_all copying Path(store.DB_PATH), the default database, every day, so a scratch run writes a backup into gridlytics-backend/data, "
                     "is founder-console's (backups.py) and is written in founder-console's late note.")

order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
late = [l.split("\t") for l in open(f"{A}/index-late.tsv").read().splitlines()[1:]]
sev = {r[0]: r[1] for r in late}
nb = sum(len(b["ids"]) for x in p["wps"] for b in x["batches"])
nbat = sum(len(x["batches"]) for x in p["wps"])
ndup = sum(len(d["dups"]) for d in p["duplicates"])
print(nb, nbat, ndup, len(p["duplicates"]), len(p["deferred"]))
p["summary"] = (
 f"116 late findings: {nb} in {nbat} late batches across 12 packages, {ndup} duplicates in {len(p['duplicates'])} groups, {len(p['deferred'])} deferred (checked with late/check_late.py: every index-late.tsv id appears exactly once; every batch severity-ordered; no canonical is itself a duplicate or deferred in plan.json; every existing package's title, owns_files, stage, depends_on and repos equal plan.json and its notes are plan.json's plus one sentence). "
 "Critic corrections to the draft: (1) parity-architecture-11 moved from storage-perf to alerts-reports, which owns reports.py in stage 1; its signing-key half is already control-edge's engine-delivery-13 (keys relative to the data folder, never silently regenerated), so storage-perf no longer claims delivery.py, reports.py or backups.py, and the reports/ backup copy and its restore go to founder-console's late note with performance-live-07's daily_all half and parity-architecture-16's restore half; "
 "(2) tests-and-perf-13 (scheduler test) moved from tests-ci to storage-perf, which rewrites the tick's failure path in the same stage (tests-ci cannot depend on it), and the product half of tests-and-perf-20 (routes_plans._held's fallback shim) goes to storage-perf because tests-ci adds no product code; "
 "(3) parity-supabase-auth-06 now points at the original tracker-and-docs-12 (the same PLT-02 defect: .env.production.example and test_deploy_docs.py missing, settings undocumented), with deploy-ci writing the files and docs ticking PLT-02 once they land; "
 "(4) deploy-ci depends on parity-ui as well: its lint/format fixes and package.json edits land on parity-ui's restyled files, which the draft's own note required ('after parity-ui lands'); it runs in parallel with docs, appending to BACKEND_ASKS/BUILD_NOTES and touching only the dependency lists of THIRD_PARTY_NOTICES; "
 "(5) auth-security's 4+3 batches merged into one batch of 7, and alerts-reports now has 4+4; (6) dup notes widened where the late finding adds a cause (engine-optimiser-14: the bound's 15-minute vs 1-minute basis and month-to-date M0, carried into savings-engine's late sentence; pages-A-02: names and emails no one can change). "
 "Kept from the draft: duplicates of original findings point at the original canonical; late-late duplicates point at a batched late canonical (parity-architecture-05/08/09/13, performance-live-01/03/04/10/11, tests-and-perf-05); pages-D-24 is raised to high by its late duplicate. "
 "Two new packages: storage-perf (stage 2, after every stage-1 package, in parallel with savings-ui-crosscut and tests-ci) because plan retention, tick failures and capacity, read cost, long requests and payload cost cut across files of several stage-1 owners; deploy-ci (stage 3, after tests-ci, savings-ui-crosscut, storage-perf and parity-ui, in parallel with docs) because CI and deployment were left out of tests-ci and nothing owns the repository shape. "
 "Accounts stay preassigned (no Google, no self-signup); Supabase stays an optional owner-applied mode as in solarlytics-backend (auth-security's web sign-in and members list, founder-console's SQL for review only). "
 "Deferred: parity-architecture-17 (owner decision on a family toolchain and shared UI package) and parity-architecture-22 (planned HANDOFF 8.C verification harness and benchmark). "
 "Batches of 2-3 remain only where a package received 2-3 related late findings (savings-engine, tariffs, founder-console, shell-nav, savings-ui-crosscut, docs) and moving them would break one owner per file; the late batches of stage-1 packages run after their planned batches in the same worktree. Written to " + A + "/plan-late-corrected.json by " + A + "/late/fix_late_plan.py (the draft is plan-late-draft.json).")
json.dump(p, open(f"{A}/plan-late-corrected.json", "w"), indent=1, ensure_ascii=False)
