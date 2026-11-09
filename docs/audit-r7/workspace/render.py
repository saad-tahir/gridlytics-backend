"""render.py — assemble the final GRIDLYTICS_AUDIT.md from the workflow outputs.

    python3 -I render.py --out /home/user/GRIDLYTICS_AUDIT.md \
        --verified results/verified-*.json --designs results/section8.json --ship results/ship.json \
        --front results/front-matter.md --workplan results/workplan.md --parity results/parity.md --pr results/pr-review.md \
        --missed results/missed.md

Inputs:
  --verified  one or more merged verify-fix results: {"results": [finding + keep + verdicts + severity_final + fix]}
  --designs   section8 designs result: {"docs": [{key, markdown, summary, review:{issues, additions}}]}
  --ship      shippability result: same shape
  --front     hand-written front matter (Parts 0-2); {{COUNTS}} and {{WORKPLAN}} are filled here
  --workplan  hand-written ordered work plan (Part 2 body)
  --parity    Part 5, the Boxlytics parity change plan
  --pr        Part 8, the pull-request review
  --missed    Appendix C, aspects the critic found missing, and the audit's own limits
"""
import glob
import json
import re
import sys

SEV = {"high": 0, "medium": 1, "low": 2}
LABEL_ORDER = {"bug": 0, "spec-mismatch": 1, "feature-gap": 2, "ux": 3}
PARITY_DIMS = {"parity-boxlytics-ui", "parity-supabase-auth", "parity-architecture", "boxlytics-ui-parity", "auth-supabase-parity"}


def opt(name, default=None, many=False):
    if f"--{name}" not in sys.argv:
        return [] if many else default
    i = sys.argv.index(f"--{name}") + 1
    vals = []
    while i < len(sys.argv) and not sys.argv[i].startswith("--"):
        vals += sorted(glob.glob(sys.argv[i])) or [sys.argv[i]]
        i += 1
    return vals if many else (vals[0] if vals else default)


def load(p):
    with open(p, encoding="utf8") as f:
        return json.load(f)


def read(p):
    return open(p, encoding="utf8").read() if p else ""


def words(t):
    return set(w for w in re.sub(r"[^a-z0-9 ]", " ", (t or "").lower()).split() if len(w) > 3)


def dedupe(items):
    seen, out = [], []
    for f in items:
        w = words(f["title"])
        same = next((g for g in seen if len(w & g["w"]) >= max(3, int(len(w) * 0.6)) and (g["f"].get("file") == f.get("file") or (g["f"].get("url") and g["f"].get("url") == f.get("url")) or g["f"].get("area") == f.get("area"))), None)
        if same:
            same["f"].setdefault("also", []).append(f"{f.get('dim', '')} ({f.get('id', '')})")
            # keep the stronger fix / higher severity
            if SEV.get(sev(f), 3) < SEV.get(sev(same["f"]), 3):
                same["f"]["severity_final"] = sev(f)
            if not same["f"].get("fix") and f.get("fix"):
                same["f"]["fix"] = f["fix"]
            continue
        seen.append({"w": w, "f": f})
        out.append(f)
    return out


def sev(f):
    return f.get("severity_final") or f.get("severity") or "low"


def md_block(text, lang):
    t = (text or "").strip()
    if not t:
        return ""
    if t.startswith("```"):
        return t
    return f"```{lang}\n{t}\n```"


def render_docs(d, part, title, intro):
    L = [f"\n## Part {part}. {title}\n", intro, ""]
    for i, doc in enumerate(d.get("docs", []), 1):
        L.append(f"\n### {part}.{i} {doc['key']}\n")
        if doc.get("summary"):
            L.append(f"*Summary.* {doc['summary']}\n")
        body = doc.get("markdown", "")
        # demote the design's own headings under this section
        body = re.sub(r"(?m)^(#{1,3}) ", lambda m: "#" * min(6, len(m.group(1)) + 3) + " ", body)
        L.append(body)
        rv = doc.get("review") or {}
        if rv:
            L.append(f"\n#### {part}.{i}.R Reviewer's corrections and additions\n")
            for x in rv.get("issues", []):
                L.append(f"- {x}")
            L.append("")
            add = rv.get("additions", "")
            add = re.sub(r"(?m)^(#{1,3}) ", lambda m: "#" * min(6, len(m.group(1)) + 4) + " ", add)
            L.append(add)
    return L


def main():
    out = opt("out", "/home/user/GRIDLYTICS_AUDIT.md")
    kept, refuted = [], []
    for p in opt("verified", many=True):
        for r in load(p).get("results", []):
            (kept if r.get("keep") else refuted).append(r)
    kept = dedupe(kept)
    kept.sort(key=lambda f: (SEV.get(sev(f), 3), 0 if f.get("repo") == "backend" else 1 if f.get("repo") == "both" else 2, LABEL_ORDER.get(f.get("label"), 9), f["title"]))
    for i, f in enumerate(kept):
        f["gid"] = f"G-{i+1:03d}"
    counts = {s: sum(1 for f in kept if sev(f) == s) for s in ("high", "medium", "low")}
    n_fix = sum(1 for f in kept if f.get("fix"))
    L = []
    front = read(opt("front"))
    front = front.replace("{{COUNTS}}", f"**{len(kept)} verified findings: {counts['high']} high, {counts['medium']} medium, {counts['low']} low**, {n_fix} of them with a proposed code change and test. {len(refuted)} candidate findings were refuted by verification and are listed in Appendix D.")
    front = front.replace("{{WORKPLAN}}", read(opt("workplan")) or "_(work plan to follow)_")
    L.append(front)
    # findings index
    L.append("\n## Part 3. Findings index\n")
    L.append("IDs are this document's; the reviewer's own id (dimension-number) follows in brackets so the verification record can be traced. Effort: S under half a day, M one to three days, L a week or more.\n")
    L.append("| ID | Sev | Label | Repo | Area | Title | Effort |\n|---|---|---|---|---|---|---|")
    for f in kept:
        L.append(f"| {f['gid']} | {sev(f)} | {f.get('label','')} | {f.get('repo','')} | {f.get('area','')} | {f['title']} ({f.get('id','')}) | {(f.get('fix') or {}).get('effort','')} |")
    L.append("")
    # findings detail
    L.append("\n## Part 4. Findings in detail, with the proposed fix\n")
    L.append("Each finding was raised by one reviewer and re-checked by a verifier who reproduced it against the code or the live app and judged it against DESIGN_LOG, the plan and HANDOFF sections 8–9. The fix proposals were written by a separate agent after reading the real code; they are sketches to apply by hand, each with the regression test to write first.\n")
    cur = None
    for f in kept:
        if sev(f) != cur:
            cur = sev(f)
            L.append(f"\n### Severity: {cur}\n")
        L.append(f"#### {f['gid']} · [{f.get('label','')}] {f['title']}\n")
        meta = [f"**Repo:** {f.get('repo','')}", f"**Area:** {f.get('area','')}"]
        if f.get("role"): meta.append(f"**Role:** {f['role']}")
        if f.get("url"): meta.append(f"**URL:** `{f['url']}`")
        if f.get("width"): meta.append(f"**Width:** {f['width']}")
        if f.get("file"): meta.append(f"**File:** `{f['file']}`" + (f":{f['line']}" if f.get("line") else ""))
        if f.get("design_wrong_votes"): meta.append("**Designed wrong:** yes (the design, not only the code, should change)")
        meta.append(f"**Found by:** {f.get('dim','')} ({f.get('id','')})" + (", also " + ", ".join(f.get("also", [])) if f.get("also") else ""))
        L.append(" · ".join(meta) + "\n")
        L.append(f"**Steps to reproduce.** {f.get('steps','')}\n")
        L.append(f"**Expected.** {f.get('expected','')}\n")
        L.append(f"**Actual.** {f.get('actual','')}\n")
        L.append(f"**Evidence.** {f.get('evidence','')}\n")
        if f.get("screenshot"): L.append(f"**Screenshot.** `{f['screenshot']}`\n")
        vs = f.get("verdicts") or []
        if vs:
            L.append("**Verification.**\n")
            for v in vs:
                L.append(f"- {'refuted' if v.get('refuted') else 'confirmed'} (severity {v.get('severity')}, confidence {v.get('confidence')}): {v.get('reason','')}")
            L.append("")
        elif f.get("unverified"):
            L.append("**Verification.** Not re-checked (the verifier for this batch did not return): treat as the reviewer's claim and check the steps before fixing.\n")
        fx = f.get("fix")
        if fx:
            L.append(f"**Proposed fix ({fx.get('effort','?')}).** {fx.get('fix_summary','')}\n")
            L.append(f"*Approach.* {fx.get('approach','')}\n")
            for c in fx.get("changes", []):
                L.append(f"*{c.get('repo','')}: `{c.get('path','')}`*\n")
                L.append(md_block(c.get("diff", ""), "diff") + "\n")
            if (fx.get("tests") or "").strip():
                L.append("*Tests to write first.*\n")
                L.append(md_block(fx["tests"], "python") + "\n")
            if fx.get("risk"): L.append(f"*Risk and roll-back.* {fx['risk']}\n")
            if fx.get("depends_on"): L.append(f"*Depends on.* {fx['depends_on']}\n")
            if fx.get("design_note"): L.append(f"*Design note.* {fx['design_note']}\n")
        else:
            L.append("**Proposed fix.** Not written (the fix agent for this batch did not return); the steps and evidence above locate the change.\n")
        L.append("---\n")
    # Part 5 parity
    if opt("parity"):
        L.append(read(opt("parity")))
    # Part 6 designs
    if opt("designs"):
        L += render_docs(load(opt("designs")), 6, "Implementation designs for the open work (HANDOFF section 8)", "One design per open item, each checked by a second reviewer whose corrections follow it. They are written to be implemented directly: goal and acceptance, the current code and its limits, the design, diff sketches, the tests to write first, migration notes, effort and risks.")
    # Part 7 ship gate
    if opt("ship"):
        L += render_docs(load(opt("ship")), 7, "Ship gate: is it shippable after the fixes, and how to prove it", "The release gates, the end-to-end acceptance suite (prototyped against the running scratch app) and the engine acceptance tests. Run them after the fixes in Part 4; the product is shippable to a first paying customer when every gate passes.")
    # Part 8 PR review
    if opt("pr"):
        L.append(read(opt("pr")))
    # Appendix C missed aspects
    if opt("missed"):
        L.append(read(opt("missed")))
    # Appendix D refuted
    L.append("\n## Appendix D. Candidate findings refuted in verification (not to fix)\n")
    L.append("Kept for transparency: a reviewer raised each of these and the verifier showed it is by design, already covered, or not reproducible. The verifier's reason is quoted.\n")
    for r in refuted:
        reasons = " | ".join((v.get("reason") or "")[:300] for v in (r.get("verdicts") or []) if v.get("refuted"))
        L.append(f"- **{r['title']}** ({r.get('dim','')} {r.get('id','')}) — {reasons}")
    text = "\n".join(L)
    open(out, "w", encoding="utf8").write(text)
    print(f"wrote {out}: {len(text)//1024} KB, {len(kept)} findings ({counts}), {n_fix} with fixes, {len(refuted)} refuted")
    json.dump({"kept": kept, "refuted": refuted}, open(out.replace(".md", ".findings.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
