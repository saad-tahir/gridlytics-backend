export const meta = {
  name: 'gridlytics-verify-fix-batched',
  description: 'Verify audit findings in small batches (reproduce + by-design/severity in one pass), then write concrete fix proposals (diff sketch + test) for every kept finding, batched by area; findings are read from a file',
  phases: [{ title: 'Verify', detail: 'one verifier per batch of ~6 findings' }, { title: 'Fix', detail: 'one engineer per batch of ~5 kept findings' }],
}
const P = args
const S = P.sites
const RULES = `
CONTEXT: Gridlytics = OnyxLeaf's energy-dispatch optimiser for Pakistani C&I sites. Backend ${P.backend} (branch v1-build; backend/gridlytics; tests backend/tests; python ${P.backend}/.scratch/venv/bin/python). Web app ${P.frontend} (frontend/src; vitest). Spec ${P.plan}/build-plan/EMS_BUILD_PLAN.md; tracker ${P.plan}/build-plan/EMS_BUILD_PROGRESS.md; product brief "${P.plan}/requirements/Product 1 - TOU-Aware Energy Management and Dispatch.md"; deliberate deviations and settled rules in ${P.backend}/docs/DESIGN_LOG.md; handoff ${P.backend}/HANDOFF.md (section 8 = known open work, 9 = known limits); web docs ${P.frontend}/docs/*.md. Reference products (read-only): Boxlytics ${P.boxMain} (older UI ${P.boxUiV1}), Solarlytics ${P.solarBackend} and ${P.solarFrontend}. The scratch app is RUNNING (API http://127.0.0.1:8151, web http://127.0.0.1:5295; demo sites Faisalabad ${S.faisalabad}, Karachi ${S.karachi}, Lahore ${S.lahore}, farm ${S.farm}); screenshot/report helper: cd ${P.audit} && node shot.cjs --role <owner|operator|analyst|viewer|founder> --paths /a,/b --width <1440|375> --out <dir> [--a11y] [--actions "click:<sel>;fill:<sel>=<v>;key:Tab"]; API helper ${P.audit}/api.py (run with the venv python: client('owner').get('/api/...')). Never print secrets.
OWNER'S CONSTRAINTS: no Google/social sign-in or self-signup, ever: accounts are preassigned by the founder/admin as in solarlytics-backend (local mode; Supabase only as an optional owner-applied mode). Gridlytics must look like a sibling of Boxlytics (landing page, fonts, colours, spacing, components). A viewer must never see money.
HARD RULES: never modify repository files (write scratch files only under ${P.audit}/); never touch ${P.backend}/data; never restart/reseed servers; do not run pytest/vitest/npm build; several agents share this 4-CPU machine, so slowness and time-limited plans solved during the audit may be audit load, not the product.
FINDINGS FILE: ${P.file} is a JSON array of findings; each has id, dim, repo, title, label, severity, area, file, line, steps, expected, actual, evidence, design_wrong, confidence (live findings also role, url, width, screenshot). Read ONLY the ids assigned to you (e.g. with the venv python: json.load, filter by id).`

const VERDICTS = { type: 'object', properties: { verdicts: { type: 'array', items: { type: 'object', properties: {
  id: { type: 'string' }, refuted: { type: 'boolean' }, confidence: { type: 'number' }, reason: { type: 'string' },
  severity: { type: 'string', enum: ['high', 'medium', 'low'] }, design_wrong: { type: 'boolean' }, corrected_title: { type: 'string' },
}, required: ['id', 'refuted', 'confidence', 'reason', 'severity', 'design_wrong'] } } }, required: ['verdicts'] }

const FIXES = { type: 'object', properties: { fixes: { type: 'array', items: { type: 'object', properties: {
  id: { type: 'string' },
  fix_summary: { type: 'string' },
  approach: { type: 'string' },
  changes: { type: 'array', items: { type: 'object', properties: { repo: { type: 'string', enum: ['backend', 'frontend', 'docs'] }, path: { type: 'string' }, diff: { type: 'string' } }, required: ['repo', 'path', 'diff'] } },
  tests: { type: 'string' },
  risk: { type: 'string' },
  effort: { type: 'string', enum: ['S', 'M', 'L'] },
  depends_on: { type: 'string' },
  design_note: { type: 'string' },
}, required: ['id', 'fix_summary', 'approach', 'changes', 'tests', 'risk', 'effort'] } } }, required: ['fixes'] }

const chunk = (xs, n) => { const out = []; for (let i = 0; i < xs.length; i += n) out.push(xs.slice(i, i + n)); return out }
const groupBy = (xs, key) => xs.reduce((m, x) => { (m[key(x)] = m[key(x)] || []).push(x); return m }, {})

const items = P.index // [{id, dim, repo, area, sev}]
const VB = P.verifyBatch || 6, FB = P.fixBatch || 5
const vBatches = Object.values(groupBy(items, f => f.dim)).flatMap(g => chunk(g, VB))
log(`Verifying ${items.length} findings in ${vBatches.length} batches (${P.slice || ''})`)

phase('Verify')
const verified = (await parallel(vBatches.map(b => () => agent(`${RULES}

You are an adversarial verifier. Your findings are ids ${b.map(f => f.id).join(', ')} (dimension "${b[0].dim}") in the findings file. For EACH of them do BOTH checks and return one verdict per id:
(1) REPRODUCE: open the cited file and lines (search for where the behaviour really lives if the citation is off); for a live-app finding reproduce it with shot.cjs or the API client. Refute with confidence >= 0.7 ONLY if you did the check and the behaviour is not as claimed. If you could not run the check, set refuted=false, confidence 0.3 and say why.
(2) BY-DESIGN + SEVERITY: check DESIGN_LOG.md, EMS_BUILD_PLAN.md and HANDOFF sections 8-9. Refute if the behaviour is a documented, reasonable decision, or an already-listed open item merely restated with no new evidence that the decision is wrong (if the decision itself is wrong, keep it and set design_wrong=true). Refute pure stylistic preferences with no user impact. Otherwise keep and set the severity warranted for a Pakistani factory owner/operator/analyst/viewer and for OnyxLeaf's credibility: high = wrong money, safety, role leak, data loss, crash, blocked flow, misleading savings; medium = a real flow wrong/missing or misleading figures; low = polish.
In 'reason' state exactly what you checked (file:line, screenshot path or API call) and the design-log/plan reference if any. If the finding is right but the title is off, give corrected_title. Do not skip any id.`, { label: `verify:${b[0].id}..${b[b.length - 1].id}`, phase: 'Verify', schema: VERDICTS })
  .then(r => ({ batch: b, verdicts: (r && r.verdicts) || [] }))))).filter(Boolean)

const byId = {}
for (const { batch, verdicts } of verified) for (const f of batch) {
  const v = verdicts.find(x => x.id === f.id)
  if (!v) { byId[f.id] = { ...f, keep: true, verdicts: [], severity_final: f.sev, design_wrong_votes: 0, unverified: true }; continue }
  const keep = !(v.refuted && v.confidence >= 0.7)
  byId[f.id] = { ...f, corrected_title: v.corrected_title, keep, verdicts: [v], severity_final: v.refuted ? f.sev : (v.severity || f.sev), design_wrong_votes: v.design_wrong ? 1 : 0 }
}
const all = items.map(f => byId[f.id]).filter(Boolean)
const kept = all.filter(f => f.keep)
log(`Kept ${kept.length}, refuted ${all.length - kept.length}`)

phase('Fix')
// one engineer per ~FB kept findings of the same dimension, related areas next to each other
const fBatches = Object.values(groupBy(kept, f => f.dim)).flatMap(g => chunk(g.slice().sort((a, b) => `${a.repo}|${a.area}`.localeCompare(`${b.repo}|${b.area}`)), FB))
log(`Writing fixes in ${fBatches.length} batches`)
const fixed = (await parallel(fBatches.map(b => () => agent(`${RULES}

You are a senior engineer writing FIX PROPOSALS for ${b.length} verified audit findings (ids ${b.map(f => f.id).join(', ')} in the findings file; read them first), to be applied later by a developer. The verifier's notes per id are below: use the corrected severity and title. Read the real code around each finding (and its callers/tests) before writing. For EACH id return: (1) fix_summary (one sentence); (2) approach (why this fix, alternatives considered, how it respects the DESIGN_LOG rules and the plan); (3) changes: per file a unified-diff-style sketch (\`\`\`diff with - and + lines and real surrounding context copied from the file; minimal and correct; new files in full when small); (4) tests: the regression test to add first (pytest in backend/tests/test_fix_r7_*.py following the existing conventions, or vitest beside the component) with code; (5) risk and roll-back; (6) effort S/M/L; (7) depends_on (other finding ids or BACKEND_ASKS items); (8) design_note when design_wrong_votes > 0: what the design should be instead. If a finding is a documentation/spec mismatch, the change may be to the doc. If it is a Boxlytics-parity gap, give the exact tokens/files/markup to change. Where two findings share a root cause, say so and give the shared diff once (reference it from the other id). Do not skip any id.

VERIFIER NOTES:
${JSON.stringify(b.map(f => ({ id: f.id, severity_final: f.severity_final, corrected_title: f.corrected_title, design_wrong_votes: f.design_wrong_votes, notes: (f.verdicts || []).map(v => v.reason) })), null, 1)}`, { label: `fix:${b[0].id}..${b[b.length - 1].id}`, phase: 'Fix', schema: FIXES })
  .then(r => ({ batch: b, fixes: (r && r.fixes) || [] }))))).filter(Boolean)

for (const { batch, fixes } of fixed) for (const f of batch) {
  const fx = fixes.find(x => x.id === f.id)
  if (fx) byId[f.id].fix = fx
}
const results = items.map(f => byId[f.id]).filter(Boolean)
log(`Fixes written for ${results.filter(f => f.fix).length} of ${kept.length} kept findings`)
return { file: P.file, results }
