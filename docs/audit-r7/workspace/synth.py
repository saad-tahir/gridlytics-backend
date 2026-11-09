"""synth.py — merge the audit workflows' results into one findings JSON, a markdown report of issue-ready entries, and a CSV.

    python3 -I synth.py phase1.json phase2.json [extra.json ...] --out DIR

Each input is the workflow's return value: {kept: [...], dropped: [...], notes: {...}, gaps: [...]}.
"""
import csv
import json
import os
import re
import sys

SEV = {"high": 0, "medium": 1, "low": 2}


def load(path):
    with open(path, encoding="utf8") as f:
        return json.load(f)


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else "."
    os.makedirs(out, exist_ok=True)
    kept, dropped, notes, gaps = [], [], {}, []
    for p in args:
        d = load(p)
        tag = os.path.basename(p).replace(".json", "")
        for k in d.get("kept", []):
            k["source"] = tag
            kept.append(k)
        for x in d.get("dropped", []):
            x["source"] = tag
            dropped.append(x)
        notes[tag] = d.get("notes", {})
        gaps += [f"[{tag}] {g}" for g in d.get("gaps", [])]
    # cross-run dedup by title similarity
    seen, merged = [], []
    for f in kept:
        words = set(w for w in re.sub(r"[^a-z0-9 ]", " ", f["title"].lower()).split() if len(w) > 3)
        dup = next((g for g in seen if len(words & g["w"]) >= max(2, len(words) // 2) and (g["f"].get("file") == f.get("file") or g["f"].get("url") == f.get("url") or g["f"].get("area") == f.get("area"))), None)
        if dup:
            dup["f"].setdefault("also_found_by", []).append(f["source"] + ":" + f.get("dim", ""))
            continue
        seen.append({"w": words, "f": f})
        merged.append(f)
    for i, f in enumerate(sorted(merged, key=lambda x: (SEV.get(x.get("severity_final", x["severity"]), 3), x["repo"], x["label"]))):
        f["id"] = f"G-{i+1:03d}"
    merged.sort(key=lambda x: x["id"])
    json.dump({"findings": merged, "dropped": dropped, "notes": notes, "gaps": gaps}, open(os.path.join(out, "findings.json"), "w"), indent=1)
    with open(os.path.join(out, "findings.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "repo", "severity", "label", "area", "design_wrong_votes", "title", "url", "file", "source", "dim"])
        for x in merged:
            w.writerow([x["id"], x["repo"], x.get("severity_final", x["severity"]), x["label"], x.get("area"), x.get("design_wrong_votes", 0), x["title"], x.get("url", ""), x.get("file", ""), x["source"], x.get("dim", "")])
    lines = ["# Gridlytics audit: issue-ready findings", "", f"{len(merged)} verified findings ({sum(1 for x in merged if x.get('severity_final', x['severity'])=='high')} high, {sum(1 for x in merged if x.get('severity_final', x['severity'])=='medium')} medium, {sum(1 for x in merged if x.get('severity_final', x['severity'])=='low')} low); {len(dropped)} refuted in verification.", ""]
    for repo in ("backend", "frontend", "both"):
        xs = [x for x in merged if x["repo"] == repo]
        if not xs:
            continue
        lines.append(f"## Repository: gridlytics-{repo if repo != 'both' else 'backend + gridlytics-frontend'}")
        lines.append("")
        for x in xs:
            sev = x.get("severity_final", x["severity"])
            lines.append(f"### {x['id']} · [{x['label']}] [{sev}] {x['title']}")
            lines.append("")
            meta = []
            if x.get("area"): meta.append(f"Area: {x['area']}")
            if x.get("role"): meta.append(f"Role: {x['role']}")
            if x.get("url"): meta.append(f"URL: {x['url']}")
            if x.get("width"): meta.append(f"Width: {x['width']}")
            if x.get("file"): meta.append(f"File: `{x['file']}`" + (f":{x['line']}" if x.get("line") else ""))
            if x.get("design_wrong_votes"): meta.append(f"Design-wrong votes: {x['design_wrong_votes']}")
            meta.append(f"Found by: {x['source']}:{x.get('dim','')}" + (", " + ", ".join(x["also_found_by"]) if x.get("also_found_by") else ""))
            lines.append("; ".join(meta))
            lines.append("")
            lines.append(f"**Steps to reproduce**\n\n{x['steps']}\n")
            lines.append(f"**Expected**\n\n{x['expected']}\n")
            lines.append(f"**Actual**\n\n{x['actual']}\n")
            lines.append(f"**Evidence**\n\n{x['evidence']}\n")
            if x.get("screenshot"): lines.append(f"Screenshot: `{x['screenshot']}`\n")
            vs = x.get("verdicts", [])
            if vs:
                lines.append("**Verification**\n")
                for v in vs:
                    lines.append(f"- {'refuted' if v.get('refuted') else 'confirmed'} (sev {v.get('severity')}): {v.get('reason','')[:400]}")
                lines.append("")
    lines.append("## Refuted in verification (not reported)")
    lines.append("")
    for x in dropped:
        lines.append(f"- [{x['source']}:{x.get('dim','')}] {x['title']} — " + " | ".join(r[:160] for r in x.get("reasons", [])))
    lines.append("")
    lines.append("## Coverage gaps named by the critics")
    lines.append("")
    lines += [f"- {g}" for g in gaps]
    open(os.path.join(out, "FINDINGS.md"), "w", encoding="utf8").write("\n".join(lines))
    print(f"{len(merged)} findings → {out}/FINDINGS.md, findings.json, findings.csv; {len(dropped)} refuted; {len(gaps)} gaps")


if __name__ == "__main__":
    main()
