"""status.py — per work package: batches done (from every implement run's journal), per-finding status, commits, dirty files, remaining batches.
    python3 -I impl/status.py > impl/STATUS-IMPL.json  (also prints a summary)"""
import json, glob, subprocess, collections, os
A = "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit"
J = "/root/.claude/projects/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/subagents/workflows"
RUNS = ["wf_4eeead6a-8aa", "wf_e7ee2643-f9b", "wf_f9aeec06-a18", "wf_c9fe50b5-0ab", "wf_852eb745-0d9", "wf_471422c7-12c", "wf_1a8428a9-a72"]
plan = json.load(open(f"{A}/impl/plan-all.json"))
done = collections.defaultdict(dict)
reviews = {}
for w in RUNS:
    p = f"{J}/{w}/journal.jsonl"
    if not os.path.exists(p):
        continue
    labels = {}
    for l in open(p):
        r = json.loads(l)
        if r.get("type") == "started":
            labels[r["key"]] = r.get("label") or ""
        if r.get("type") == "result" and r.get("result"):
            lab = labels.get(r["key"], "")
            if lab.startswith("impl:"):
                _, wp, k = lab.split(":"); done[wp][int(k)] = r["result"]
            elif lab.startswith("review:"):
                reviews[lab[7:]] = r["result"]
def git(d, *a):
    return subprocess.run(["git", "-C", d, *a], capture_output=True, text=True).stdout.strip()
out = {}
for wp in plan["wps"]:
    k = wp["key"]
    root = f"/home/user/wt/{k}"
    info = {"key": k, "stage": wp["stage"], "title": wp["title"], "batches_total": len(wp["batches"]), "batches_done": sorted(done[k]),
            "review": reviews.get(k), "findings": {}, "remaining_batches": []}
    for i, b in enumerate(wp["batches"], 1):
        if i not in done[k]:
            info["remaining_batches"].append({"batch": i, "theme": b["theme"], "ids": b["ids"]})
    for i, r in done[k].items():
        for x in r.get("results", []):
            info["findings"][x["id"]] = {"status": x["status"], "summary": x.get("summary", "")[:600], "reason": x.get("reason", ""), "commits": x.get("commits", []), "tests": x.get("tests", [])}
    for repo in ("gridlytics-backend", "gridlytics-frontend"):
        d = f"{root}/{repo}"
        if os.path.isdir(d):
            info[repo] = {"branch": git(d, "rev-parse", "--abbrev-ref", "HEAD"), "head": git(d, "rev-parse", "--short", "HEAD"),
                          "commits": git(d, "log", "--format=%h %s", "origin/fix/offgrid-carry-over..HEAD").splitlines(),
                          "dirty": git(d, "status", "--short").splitlines()}
    out[k] = info
json.dump(out, open(f"{A}/impl/STATUS-IMPL.json", "w"), indent=1)
tot = collections.Counter()
for k, i in out.items():
    st = collections.Counter(v["status"] for v in i["findings"].values()); tot.update(st)
    b = i.get("gridlytics-backend", {}); f = i.get("gridlytics-frontend", {})
    print(f"s{i['stage']} {k:26s} done {i['batches_done']} of {i['batches_total']}  backend {len(b.get('commits', []))}c/{len(b.get('dirty', []))}d  web {len(f.get('commits', []))}c/{len(f.get('dirty', []))}d  {dict(st)}")
print("finding statuses so far:", dict(tot))
