"""check_plan.py <plan.json> — coverage and overlap check of a work-package plan against impl/index.tsv."""
import json, sys, collections
A = "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit"
plan = json.load(open(sys.argv[1]))
plan = plan.get("plan", plan)
ids = [l.split("\t")[0] for l in open(f"{A}/impl/index.tsv").read().splitlines()[1:]]
seen = collections.Counter()
for w in plan["wps"]:
    for b in w["batches"]:
        for i in b["ids"]:
            seen[i] += 1
for d in plan["duplicates"]:
    for i in d["dups"]:
        seen[i] += 1
for d in plan["deferred"]:
    seen[d["id"]] += 1
missing = [i for i in ids if seen[i] == 0]
twice = [i for i, n in seen.items() if n > 1]
unknown = [i for i in seen if i not in set(ids)]
print(f"{len(ids)} ids; missing {len(missing)}; twice {len(twice)}; unknown {len(unknown)}")
if missing: print("missing:", missing[:40])
if twice: print("twice:", twice[:40])
if unknown: print("unknown:", unknown[:40])
own = collections.defaultdict(list)
for w in plan["wps"]:
    for f in w["owns_files"]:
        own[f].append(f"{w['key']}(s{w['stage']})")
for f, ws in own.items():
    if len(ws) > 1: print("shared:", f, ws)
for w in sorted(plan["wps"], key=lambda w: (w["stage"], w["key"])):
    n = sum(len(b["ids"]) for b in w["batches"])
    print(f"  s{w['stage']} {w['key']:28s} {len(w['batches']):3d} batches {n:4d} findings  deps={w['depends_on']}")
print("deferred:", len(plan["deferred"]), "dup groups:", len(plan["duplicates"]), "dups:", sum(len(d["dups"]) for d in plan["duplicates"]))
