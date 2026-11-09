export const meta = {
  name: 'gridlytics-verify-fix',
  description: 'Verify each audit finding with two independent lenses, then write a concrete fix proposal (diff sketch + test) for every kept finding',
  phases: [{ title: 'Verify', detail: 'reproduce + by-design/severity per finding' }, { title: 'Fix', detail: 'diff sketch and test per kept finding' }],
}
const P = args
const S = P.sites
const RULES = `
CONTEXT: Gridlytics = OnyxLeaf's energy-dispatch optimiser for Pakistani C&I sites. Backend ${P.backend} (branch v1-build; backend/gridlytics; tests backend/tests; python ${P.backend}/.scratch/venv/bin/python). Web app ${P.frontend} (frontend/src; vitest). Spec ${P.plan}/build-plan/EMS_BUILD_PLAN.md; tracker ${P.plan}/build-plan/EMS_BUILD_PROGRESS.md; product brief "${P.plan}/requirements/Product 1 - TOU-Aware Energy Management and Dispatch.md"; deliberate deviations and settled rules in ${P.backend}/docs/DESIGN_LOG.md; handoff ${P.backend}/HANDOFF.md; web docs ${P.frontend}/docs/*.md. Reference products: Boxlytics ${P.boxMain} (and older ${P.boxUiV1}), Solarlytics ${P.solarBackend} and ${P.solarFrontend}. The scratch app is RUNNING (API http://127.0.0.1:8151, web http://127.0.0.1:5295; demo sites Faisalabad ${S.faisalabad}, Karachi ${S.karachi}, Lahore ${S.lahore}, farm ${S.farm}); screenshot/report helper: cd ${P.audit} && node shot.cjs --role <owner|operator|analyst|viewer|founder> --paths /a,/b --width <1440|375> --out <dir> [--a11y] [--actions "click:<sel>;fill:<sel>=<v>;key:Tab"]; API helper ${P.audit}/api.py (client('owner').get('/api/...')). Never print secrets.
HARD RULES: never modify repository files (write scratch files only under ${P.audit}/); never touch ${P.backend}/data; never restart/reseed servers; do not run pytest/vitest/npm build; several agents share this 4-CPU machine, so slowness and time-limited plans after 2026-11-08 14:45 UTC may be audit load.`

const VERDICT = { type: 'object', properties: { refuted: { type: 'boolean' }, confidence: { type: 'number' }, reason: { type: 'string' }, severity: { type: 'string', enum: ['high', 'medium', 'low'] }, design_wrong: { type: 'boolean' }, corrected_title: { type: 'string' } }, required: ['refuted', 'confidence', 'reason', 'severity', 'design_wrong'] }
const LENSES = [
  'REPRODUCE lens: independently confirm the finding. Open the cited file and lines (and search for where the behaviour really lives if the citation is off); for a live-app finding reproduce it with shot.cjs or the API client. Refute with confidence >= 0.7 ONLY if you did the check and the behaviour is not as claimed. If you could not run the reproduction, set refuted=false, confidence 0.3 and say why. If the finding is right but the title/severity is off, give corrected_title.',
  'BY-DESIGN + SEVERITY lens: check DESIGN_LOG.md, EMS_BUILD_PLAN.md and HANDOFF sections 8-9. Refute if the behaviour is a documented, reasonable decision, or an already-listed open item merely restated with no new evidence that the decision is wrong (if the decision itself is wrong, keep it and set design_wrong=true). Refute pure stylistic preferences with no user impact. Otherwise keep, and set the severity warranted for a Pakistani factory owner/operator/analyst/viewer and for OnyxLeaf\'s credibility: high = wrong money, safety, role leak, data loss, crash, blocked flow, misleading savings; medium = a real flow wrong/missing or misleading figures; low = polish.',
]

const FIX = { type: 'object', properties: {
  fix_summary: { type: 'string' },
  approach: { type: 'string' },
  changes: { type: 'array', items: { type: 'object', properties: { repo: { type: 'string', enum: ['backend', 'frontend', 'docs'] }, path: { type: 'string' }, diff: { type: 'string' } }, required: ['repo', 'path', 'diff'] } },
  tests: { type: 'string' },
  risk: { type: 'string' },
  effort: { type: 'string', enum: ['S', 'M', 'L'] },
  depends_on: { type: 'string' },
  design_note: { type: 'string' },
}, required: ['fix_summary', 'approach', 'changes', 'tests', 'risk', 'effort'] }

phase('Verify')
const items = P.findings
log(`Verifying ${items.length} findings (${P.slice || ''})`)
const out = await pipeline(items,
  f => parallel(LENSES.map((lens, i) => () => agent(`${RULES}\n\nYou are an adversarial verifier. ${lens}\n\nFINDING:\n${JSON.stringify(f, null, 2)}\n\nReport exactly what you checked (file:line, screenshot path or API call) in reason.`, { label: `verify:${f.id}:${i}`, phase: 'Verify', schema: VERDICT })))
    .then(vs => {
      const v = vs.filter(Boolean)
      const strong = v.filter(x => x.refuted && x.confidence >= 0.7).length
      const keep = v.length > 0 && strong === 0 && v.filter(x => x.refuted).length < v.length
      const rank = { high: 3, medium: 2, low: 1 }
      const sev = v.filter(x => !x.refuted).map(x => x.severity)
      const sevFinal = sev.length ? (Object.keys(rank).find(k => rank[k] === Math.round(sev.reduce((a, s) => a + rank[s], 0) / sev.length)) || f.severity) : f.severity
      const title = (v.find(x => x.corrected_title) || {}).corrected_title || f.title
      return { ...f, title, keep, verdicts: v, severity_final: sevFinal, design_wrong_votes: v.filter(x => x.design_wrong).length }
    }),
  async (f) => {
    if (!f || !f.keep) return f
    const fix = await agent(`${RULES}\n\nYou are a senior engineer writing the FIX PROPOSAL for one verified audit finding, to be applied later by a developer. Read the real code around the finding (and its callers/tests) before writing. Produce: (1) fix_summary (one sentence); (2) approach (why this fix, alternatives considered, how it respects the DESIGN_LOG rules and the plan); (3) changes: for each file a unified-diff-style sketch (\`\`\`diff with - and + lines, real surrounding context copied from the file; keep it minimal and correct; include new files in full when small); (4) tests: the regression test to add first (pytest in backend/tests/test_fix_r7_*.py following the existing conventions, or vitest beside the component) with code; (5) risk and roll-back; (6) effort S/M/L; (7) depends_on (other findings or backend asks); (8) design_note if the finding is design-wrong: what the design should be instead. If the finding is a documentation/spec mismatch, the change may be to the doc. If it is a Boxlytics-parity gap, give the exact tokens/files/markup to change.\n\nFINDING (verified):\n${JSON.stringify({ id: f.id, repo: f.repo, title: f.title, label: f.label, severity: f.severity_final, area: f.area, url: f.url, file: f.file, line: f.line, steps: f.steps, expected: f.expected, actual: f.actual, evidence: f.evidence, design_wrong_votes: f.design_wrong_votes, verifier_notes: f.verdicts.map(v => v.reason) }, null, 2)}`, { label: `fix:${f.id}`, phase: 'Fix', schema: FIX })
    return { ...f, fix }
  }
)
const done = out.filter(Boolean)
log(`Kept ${done.filter(x => x.keep).length}, refuted ${done.filter(x => !x.keep).length}`)
return { results: done }
