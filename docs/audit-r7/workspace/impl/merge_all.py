"""merge_all.py — one file with every finding, its verdict and fix proposal where they exist.
    python3 -I impl/merge_all.py   → impl/all-findings.json, impl/index.tsv
"""
import json, glob
J = "/root/.claude/projects/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/subagents/workflows"
A = "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit"
items = json.load(open(f"{A}/results/findings-saved-6dims.json")) + json.load(open(f"{A}/results/findings-new.json"))
for extra in sorted(glob.glob(f"{A}/results/findings-late-*.json")):
    items += json.load(open(extra))
by = {}
for f in items:
    if f["id"] in by:
        continue
    by[f["id"]] = f
verd, fixes = {}, {}
for w in ("wf_34b600f4-7b9", "wf_140e2b61-ba7"):
    for l in open(f"{J}/{w}/journal.jsonl"):
        r = json.loads(l)
        if r.get("type") != "result" or not isinstance(r.get("result"), dict):
            continue
        for v in r["result"].get("verdicts", []):
            verd[v["id"]] = v
        for fx in r["result"].get("fixes", []):
            fixes[fx["id"]] = fx
for i, f in by.items():
    if i in verd:
        f["verdict"] = verd[i]
        if verd[i].get("corrected_title"):
            f["title"] = verd[i]["corrected_title"]
        f["severity_final"] = verd[i]["severity"] if not verd[i]["refuted"] else f["severity"]
        f["refuted"] = bool(verd[i]["refuted"] and verd[i]["confidence"] >= 0.7)
    if i in fixes:
        f["fix_proposal"] = fixes[i]
out = list(by.values())
json.dump(out, open(f"{A}/impl/all-findings.json", "w"), indent=1)
with open(f"{A}/impl/index.tsv", "w") as fh:
    fh.write("id\tsev\trepo\tlabel\tfile\tarea\ttitle\tstate\n")
    for f in out:
        st = "refuted" if f.get("refuted") else ("verified+fix" if f.get("fix_proposal") else ("verified" if f.get("verdict") else "unverified"))
        fh.write("\t".join([f["id"], f.get("severity_final") or f["severity"], f["repo"], f["label"], (f.get("file") or "").replace("/home/user/saad-tahir/gridlytics-backend/", "").replace("/home/user/gridlytics-frontend/", "")[:90],
                            (f.get("area") or "")[:60], f["title"][:170].replace("\t", " "), st]) + "\n")
print(len(out), "findings;", sum(1 for f in out if f.get("verdict")), "verified;", sum(1 for f in out if f.get("refuted")), "refuted;", sum(1 for f in out if f.get("fix_proposal")), "with fix proposals")
