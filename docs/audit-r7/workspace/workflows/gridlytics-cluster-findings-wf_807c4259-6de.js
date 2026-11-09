export const meta = {
  name: 'gridlytics-cluster-findings',
  description: 'Group the audit findings into implementation work packages (disjoint files, ordered batches, duplicates merged), then have a critic check coverage and conflicts',
  phases: [{ title: 'Cluster', detail: 'one planner' }, { title: 'Critique', detail: 'one critic revises the plan' }],
}
const A = args.audit
const PLAN = { type: 'object', properties: {
  wps: { type: 'array', items: { type: 'object', properties: {
    key: { type: 'string' }, title: { type: 'string' }, repos: { type: 'array', items: { type: 'string', enum: ['backend', 'frontend'] } },
    owns_files: { type: 'array', items: { type: 'string' } }, stage: { type: 'integer' }, depends_on: { type: 'array', items: { type: 'string' } },
    batches: { type: 'array', items: { type: 'object', properties: { ids: { type: 'array', items: { type: 'string' } }, theme: { type: 'string' } }, required: ['ids', 'theme'] } },
    notes: { type: 'string' } }, required: ['key', 'title', 'repos', 'owns_files', 'stage', 'depends_on', 'batches', 'notes'] } },
  duplicates: { type: 'array', items: { type: 'object', properties: { canonical: { type: 'string' }, dups: { type: 'array', items: { type: 'string' } }, note: { type: 'string' } }, required: ['canonical', 'dups'] } },
  deferred: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, reason: { type: 'string' } }, required: ['id', 'reason'] } },
  summary: { type: 'string' },
}, required: ['wps', 'duplicates', 'deferred', 'summary'] }

const BRIEF = `CONTEXT: An audit of Gridlytics (OnyxLeaf's energy-dispatch optimiser for Pakistani C&I sites) produced the findings in ${A}/impl/index.tsv (one per line: id, severity, repo, label, file, area, title, state) with full detail in ${A}/impl/all-findings.json (steps, expected, actual, evidence, verdict, fix_proposal). Repos: backend ${args.backend} (Python FastAPI, backend/gridlytics/*.py, tests backend/tests) and web app ${args.frontend} (React, frontend/src/features/<area>, components, lib, types/api.ts; docs/*.md). Their HANDOFF.md / docs/DESIGN_LOG.md explain the modules. The owner has asked for EVERY finding to be implemented directly in code (with regression tests), on branches, by implementer agents working IN PARALLEL, one per work package (WP), each in its own git worktree; a WP's batches run one after another in that worktree; WPs are merged into one integration branch afterwards, so file overlap between WPs creates merge conflicts and must be minimised.
RULES FOR THE PLAN:
- Work packages by code area with MOSTLY DISJOINT file ownership (list owns_files: the backend modules and frontend feature folders the WP will edit). Shared, additive files may be touched by several WPs: frontend/src/types/api.ts, docs/API_CONTRACT.md, docs/BACKEND_ASKS.md, lib/format.ts wording helpers, backend schemas.py (say so in notes).
- Expected WPs (adjust to the data): savings engine (verify.py) ; savings/pilot/bills routes+UI ; delivery + control (delivery.py, routes_plans control, devices/control pages) ; optimiser + plans (optimise.py, plans.py, plan page) ; forecasts + outages ; tariffs ; telemetry/CSV + battery + genset ; sites + onboarding + site settings ; alerts + reports ; auth/security/roles/founder console/members/API keys (preassigned accounts only: no Google, no self-signup) ; web shell/navigation/a11y/states/formats (components/layout, components/ui, lib) ; demo seed/twin ; tests-and-perf/CI. Put Boxlytics UI/UX parity findings (fonts, tokens, landing page, look of the shell) in a WP "parity-ui" with stage 3 (it runs after integration, on top of everything), docs/tracker/handoff findings in a WP "docs" with stage 3, and everything else in stage 1 (stage 2 only for a WP that must build on another stage-1 WP's changes: say which in depends_on).
- Batches: 4 to 7 related findings each, highest severity first within a WP, each batch a coherent change (same files/feature). Big WPs may have many batches.
- Duplicates: the same defect reported by several reviewers (e.g. role-owner, role-operator, pages-D, engine-savings all reporting the Fleet/Overview vs Savings MDI mismatch). Pick one canonical id (the best-evidenced), list the others as dups; put ONLY the canonical id in a batch.
- Refuted findings (state=refuted) are not assigned: list them in deferred with reason "refuted in verification".
- Defer ONLY: (a) owner/business decisions that code cannot settle (state the decision needed), (b) items that require hardware or real-site data, (c) whole features that HANDOFF section 8 lists as planned work with a design (tariff editor UI-03, report schedules UI-14, alert delivery, 2-slot compile ranking, solve-time limits, CI/deployment) IF the finding merely restates that missing feature — but a defect inside an existing feature is never deferred. Everything else is assigned.
- Every id in index.tsv must appear exactly once across: batches, duplicates.dups, deferred.
Return the plan.`

phase('Cluster')
const plan = await agent(`${BRIEF}\n\nYou are the planner. Read the whole index (it is ~128 KB; read it in parts), open all-findings.json where a title is ambiguous, look at the repos' file layout (ls backend/gridlytics, frontend/src/features, components, lib) to set owns_files correctly, then produce the plan.`, { label: 'cluster:plan', phase: 'Cluster', schema: PLAN })

phase('Critique')
const revised = await agent(`${BRIEF}\n\nYou are the critic. Below is the planner's plan. Check: (1) coverage: every id of index.tsv exactly once (write a short python check against ${A}/impl/index.tsv using the plan, which you may save to ${A}/impl/plan-draft.json); (2) duplicates are true duplicates (same root cause) and the canonical one is the best; (3) owns_files overlaps between stage-1 WPs that would conflict (move findings so one WP owns each file; shared additive files excepted); (4) batches are coherent and severity-ordered; (5) deferrals follow the rules (a defect inside an existing feature is never deferred). Return the corrected full plan (same schema).\n\nPLAN:\n${JSON.stringify(plan)}`, { label: 'cluster:critic', phase: 'Critique', schema: PLAN })
return { plan: revised || plan, draft: plan }