export const meta = {
  name: 'gridlytics-implement-wps',
  description: 'Implement the audit findings directly in code: per work package, batches run one after another in the package worktree (verify, test first, fix, commit), then a reviewer runs the full suites, reviews the diff and fixes what is wrong',
  phases: [{ title: 'Implement', detail: 'one implementer per batch, sequential within a work package' }, { title: 'Review', detail: 'one reviewer per work package' }],
}
const P = args
const BIN = `${P.audit}/impl/bin`
const TRAILER = `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_01M2tN1FBYJ9WYvdMJBZxhCV`

const CTX = (wp) => `CONTEXT: Gridlytics = OnyxLeaf's energy-dispatch optimiser for Pakistani C&I sites (PV + battery + genset + grid; TOU tariffs, MDI, load shedding). An end-to-end audit produced findings; the owner asked that every finding be IMPLEMENTED directly in code, with regression tests.
YOUR WORK PACKAGE: "${wp.key}". Its entry in ${wp.plan} (wps[] with key "${wp.key}") gives its title, owns_files, notes (READ THE NOTES: they say what this package must and must not do, and which halves of shared findings belong to other packages) and batches; the plan's duplicates[] list which other finding ids each canonical id stands for.
WHERE YOU WORK (only here): backend worktree /home/user/wt/${wp.key}/gridlytics-backend and web-app worktree /home/user/wt/${wp.key}/gridlytics-frontend, both on branch audit/${wp.key}, made from ${wp.base || 'origin/fix/offgrid-carry-over (round 6, the open PR, on top of v1-build)'}. Other packages are being implemented in parallel in other worktrees on other branches. Avoid editing files other packages own (see their owns_files in the plan) unless the finding needs it (then keep the change minimal and say so in notes). Shared additive files (frontend/src/types/api.ts, lib/api/endpoints.ts, lib/api/keys.ts, docs/API_CONTRACT.md, docs/BACKEND_ASKS.md, lib/format.ts, lib/time.ts, backend schemas.py, store.py migrations) may be edited: append rather than reorder; a new store.py migration takes the next free number and will be renumbered at integration if two packages collide.
READ ONLY WHAT YOU NEED (tokens are limited): do not read HANDOFF.md or docs/DESIGN_LOG.md whole. grep them (and the web app's docs/HANDOFF.md, docs/GATES.md, docs/API_CONTRACT.md) for the modules, functions and words your findings touch, and read those sections; a finding that contradicts a settled, still-sensible DESIGN_LOG rule is refuted, not implemented. The spec is at ${P.plan} (build-plan/EMS_BUILD_PLAN.md, research/*): grep it the same way.
THE FINDINGS: full records (steps, expected, actual, evidence, a verifier's verdict and, for some, a fix_proposal with diff sketches and tests) are in ${P.audit}/impl/all-findings.json (late findings also in ${P.audit}/results/findings-late-*.json); read the ids you are given, AND the ids of their duplicates, with python (json.load, filter by id) — fixing a canonical id must also fix what its duplicates describe.
TOOLS: ${BIN}/tb.sh ${wp.key} [pytest args] runs backend tests of your worktree (no args = full suite under the machine-wide test lock, about 3-4 min under load; with args, e.g. backend/tests/test_fix_r7_${wp.key.replace(/-/g, '_')}.py or -k expr, a targeted run). ${BIN}/tf.sh ${wp.key} [vitest file filters] runs the web app's typecheck plus the named vitest files (no args = full gates: typecheck, all tests, build, under the lock). The backend runs with /home/user/saad-tahir/gridlytics-backend/.scratch/venv/bin/python (conftest puts backend/ on sys.path, so your worktree's code is what the tests import). The live scratch app (API http://127.0.0.1:8151, web http://127.0.0.1:5295) may be down and is not needed; if it is up it runs the MAIN checkout (v1-build, without round 6 and without anyone's fixes), so use it only to see the 'before' behaviour; API helper ${P.audit}/api.py (client('owner'|'operator'|'analyst'|'viewer')), screenshot helper ${P.audit}/shot.cjs. Never print passwords, tokens or secrets.
OWNER'S RULES (non-negotiable): sign-in is by preassigned accounts only (email + password made by the founder console or a company admin), exactly as solarlytics-backend: no Google/social sign-in, no self-signup, no magic links, no invitations. A viewer never sees money (rupees, rates, fees, costs, ratios of money) anywhere: page, API answer, export, report, alert text. Pakistan time (Asia/Karachi, UTC+5) for every date and time shown. Wording rules of the web app's docs (no banned words such as 'optimal', 'guaranteed', 'maximum savings'; British spelling; plain sentences). Do not restyle the UI toward Boxlytics here (that is the later parity-ui package) unless your finding is about it. Never touch any data/ folder, the main checkouts (/home/user/saad-tahir/gridlytics-backend, /home/user/gridlytics-frontend) or the Boxlytics/Solarlytics folders. Never push, never merge, never rebase other branches. Files are LF. No secrets in commits.
HOW TO WORK, PER FINDING:
1. Verify it against YOUR worktree's code (round 6 may already have changed savings code): read the cited file/lines and where the behaviour really lives. Decide: implement | already_fixed (round 6 or another change already does it; cite where) | refuted (by design per DESIGN_LOG/plan and still sensible, or not reproducible: say exactly why) | deferred (only an owner/business decision, hardware or real-site data: say what decision is needed). Prefer implementing.
2. Write the regression test FIRST (backend: backend/tests/test_fix_r7_${wp.key.replace(/-/g, '_')}.py following conftest.py and existing test_fix_r*_*.py conventions; web app: vitest beside the component, using src/test/render.tsx and fixtures), run it and see it fail for the right reason, then implement the fix, then see it pass. A pure documentation finding needs no test.
3. Keep fixes minimal, correct and consistent with the code's style (strict pydantic models via schemas.check for request bodies; one-sentence 422 refusals; role checks via auth.need; money keys hidden by money.py for viewers and API keys). Update docs/API_CONTRACT.md (web app) when an API answer changes shape, appending under a heading "## Round 7 (audit): ${wp.key}".
4. Commit each finding (or a tight group sharing one root cause) separately in the repo(s) it touches, message "R7 ${wp.key}: <what changed, in words> (audit <ids>)", then a blank line, then exactly these two lines:\n${TRAILER}\nUse git -C <worktree> commit -F <file> to keep the trailer intact. Make sure git status is clean at the end.
5. Before you finish the batch, run the targeted tests you added plus the existing test files of the modules you changed (backend) and tf.sh with your touched test files (web app); all must pass.
AN EARLIER ATTEMPT MAY HAVE BEEN INTERRUPTED (usage limits): first run git log (commits whose message names your ids are already done: verify them quickly, do not redo them) and git status in both worktrees. Uncommitted changes belong to the interrupted attempt at the next batch of this package: inspect them (git diff), keep what is right, finish and commit them under the findings they serve (even if those ids are not in your batch, report them in notes), and never discard work blindly.`

const RESULT = { type: 'object', properties: {
  results: { type: 'array', items: { type: 'object', properties: {
    id: { type: 'string' }, status: { type: 'string', enum: ['implemented', 'already_fixed', 'refuted', 'deferred', 'partial'] },
    summary: { type: 'string' }, reason: { type: 'string' }, commits: { type: 'array', items: { type: 'string' } },
    tests: { type: 'array', items: { type: 'string' } }, files: { type: 'array', items: { type: 'string' } } },
    required: ['id', 'status', 'summary', 'commits', 'tests', 'files'] } },
  notes: { type: 'string' },
}, required: ['results', 'notes'] }

const REVIEW = { type: 'object', properties: {
  backend_suite: { type: 'string' }, frontend_gates: { type: 'string' }, fixed_in_review: { type: 'array', items: { type: 'string' } },
  remaining_issues: { type: 'array', items: { type: 'string' } }, design_log_notes: { type: 'string' }, summary: { type: 'string' },
}, required: ['backend_suite', 'frontend_gates', 'fixed_in_review', 'remaining_issues', 'design_log_notes', 'summary'] }

const wps = P.wps  // [{key, n (number of batches), plan (path to the plan file), base?}]
log(`Work packages: ${wps.map(w => `${w.key} (${w.n} batches)`).join(', ')}`)

const out = await pipeline(wps,
  async (wp) => {
    const done = []
    for (let i = 0; i < wp.n; i++) {
      const r = await agent(`${CTX(wp)}\n\nYOUR BATCH: batch number ${(wp.offset || 0) + i + 1} of ${(wp.offset || 0) + wp.n} in the package's batches[] list in ${wp.plan} (index ${(wp.offset || 0) + i}, counting from 0): implement its ids (theme as given there).${(wp.offset || 0) + i > 0 ? ` Earlier batches of this package are already committed on the branch (git log); build on them.` : ''} Implement them as described above and return one result per id.`,
        { label: `impl:${wp.key}:${(wp.offset || 0) + i + 1}`, phase: 'Implement', schema: RESULT })
      done.push({ batch: (wp.offset || 0) + i + 1, result: r })
    }
    return { wp, done }
  },
  async ({ wp, done }) => {
    const touched = done.flatMap(d => (d.result && d.result.results) || []).filter(x => x.status === 'implemented' || x.status === 'partial')
    const review = await agent(`${CTX(wp)}\n\nYou are the REVIEWER of work package "${wp.key}". The implementers' results per finding are below; results of batches done in an earlier run are in ${P.audit}/impl/results-stage1-round1.json (key = package, then batch number), and the commits on the branch are the record of all of them. (1) Run the full backend suite (${BIN}/tb.sh ${wp.key}) if the backend branch changed (git -C <backend wt> log ${wp.baseRef || 'origin/fix/offgrid-carry-over'}..HEAD), and the full web-app gates (${BIN}/tf.sh ${wp.key}) if the web branch changed; fix every failure (a failing existing test means either the fix broke behaviour, or the test encoded the old, wrong behaviour: decide, and say which). (2) Review the whole diff (git diff ${wp.baseRef || 'origin/fix/offgrid-carry-over'}...HEAD in each worktree) as a senior engineer: correctness, edge cases, the owner's rules (viewer never sees money; preassigned accounts only; Pakistan time; wording), DESIGN_LOG consistency, missing or weak regression tests (a test that would pass without the fix), and anything an implementer marked partial. Fix what is wrong, with tests, and commit ("R7 ${wp.key}: review: <what>", same trailer). (3) Re-run the suites after your fixes until green. (4) Write design_log_notes: a short Markdown list of the decisions this package made that belong in DESIGN_LOG round 7. Report results honestly, including anything still failing.\n\nIMPLEMENTERS' RESULTS:\n${JSON.stringify(done.map(d => ({ batch: d.batch, results: d.result ? d.result.results : 'BATCH AGENT FAILED: its findings were not done; do them now', notes: d.result ? d.result.notes : '' })))}`,
      { label: `review:${wp.key}`, phase: 'Review', schema: REVIEW })
    return { key: wp.key, batches: done, review }
  })
return { packages: out.filter(Boolean) }
