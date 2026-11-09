import json, sys, collections
A = "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit/impl"
p = json.load(open(sys.argv[1]))
plan = json.load(open(f"{A}/plan.json"))
late = [l.split("\t") for l in open(f"{A}/index-late.tsv").read().splitlines()[1:]]
orig = [l.split("\t") for l in open(f"{A}/index.tsv").read().splitlines()[1:]]
lateids = {r[0] for r in late}; sev = {r[0]: r[1] for r in late}
origids = {r[0] for r in orig}
seen = collections.Counter()
for w in p["wps"]:
    for b in w["batches"]:
        for i in b["ids"]: seen[i] += 1
for d in p["duplicates"]:
    for i in d["dups"]: seen[i] += 1
for d in p["deferred"]: seen[d["id"]] += 1
print("missing", sorted(lateids - set(seen)))
print("extra", sorted(set(seen) - lateids))
print("multi", sorted(i for i, c in seen.items() if c > 1))
# canonicals
origdup = {}
for d in plan["duplicates"]:
    for i in d["dups"]: origdup[i] = d["canonical"]
origdef = {d["id"] for d in plan["deferred"]}
batched_late = {i for w in p["wps"] for b in w["batches"] for i in b["ids"]}
for d in p["duplicates"]:
    c = d["canonical"]
    if c in origids:
        if c in origdup: print("canonical is orig dup", c, "->", origdup[c])
        if c in origdef: print("canonical is orig deferred", c)
    elif c in lateids:
        if c not in batched_late: print("late canonical not batched", c)
    else: print("unknown canonical", c)
# wp copy check
pw = {w["key"]: w for w in plan["wps"]}
for w in p["wps"]:
    o = pw.get(w["key"])
    if not o: print("NEW wp", w["key"], w["stage"], w["depends_on"]); continue
    for k in ("title", "owns_files", "stage", "depends_on", "repos"):
        if w.get(k) != o.get(k): print("MISMATCH", w["key"], k)
    if not w["notes"].startswith(o["notes"]): print("notes not prefix", w["key"])
    else:
        extra = w["notes"][len(o["notes"]):]
        print(w["key"], "extra sentences ~", extra.count(". ") + 1, len(extra))
    orig_batched = {i for b in o["batches"] for i in b["ids"]}
    for b in w["batches"]:
        for i in b["ids"]:
            if i not in lateids: print("non-late id in batch", w["key"], i)
order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
for w in p["wps"]:
    for b in w["batches"]:
        s = [order[sev[i]] for i in b["ids"]]
        if s != sorted(s): print("not sev-ordered", w["key"], b["ids"], [sev[i] for i in b["ids"]])
        if not 4 <= len(b["ids"]) <= 7: print("batch size", len(b["ids"]), w["key"], b["theme"][:60])
print("counts: batched", len(batched_late), "dups", sum(len(d["dups"]) for d in p["duplicates"]), "groups", len(p["duplicates"]), "deferred", len(p["deferred"]), "batches", sum(len(w["batches"]) for w in p["wps"]))
