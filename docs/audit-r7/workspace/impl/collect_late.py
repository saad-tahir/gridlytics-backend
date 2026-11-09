"""collect_late.py — pull findings of dimensions not yet in impl/all-findings.json from every finder journal into results/findings-late-<dim>.json."""
import json, glob, os
J = "/root/.claude/projects/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/subagents/workflows"
A = "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit"
have = set(f["dim"] for f in json.load(open(f"{A}/impl/all-findings.json")))
for jf in glob.glob(f"{J}/*/journal.jsonl"):
    labels = {}
    for l in open(jf):
        r = json.loads(l)
        if r.get("type") == "started":
            labels[r["key"]] = r.get("label") or ""
        if r.get("type") == "result" and isinstance(r.get("result"), dict) and "findings" in r["result"]:
            lab = labels.get(r["key"], "")
            if not lab.startswith("find:"):
                continue
            dim = lab[5:]
            if dim in have or os.path.exists(f"{A}/results/findings-late-{dim}.json"):
                continue
            fs = r["result"]["findings"]
            for i, f in enumerate(fs, 1):
                f["dim"] = dim; f["id"] = f"{dim}-{i:02d}"
            json.dump(fs, open(f"{A}/results/findings-late-{dim}.json", "w"), indent=1)
            json.dump({"dim": dim, "notes": r["result"].get("notes")}, open(f"{A}/results/notes-{dim}.json", "w"), indent=1)
            print("late:", dim, len(fs))
