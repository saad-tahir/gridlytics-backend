"""make_handover.py <out.md> — HANDOVER.md from handover_static.md + STATUS-IMPL.json + plan-all.json."""
import json, sys, datetime
A = "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit"
st = json.load(open(f"{A}/impl/STATUS-IMPL.json"))
plan = json.load(open(f"{A}/impl/plan-all.json"))
idx = {}
for line in open(f"{A}/impl/index.tsv").read().splitlines()[1:] + open(f"{A}/impl/index-late.tsv").read().splitlines()[1:]:
    c = line.split("\t"); idx[c[0]] = {"sev": c[1], "title": c[6]}
L = []
L.append("| Stage | Package | Batches done | Backend commits | Web commits | Findings settled | Remaining batches |")
L.append("|---|---|---|---|---|---|---|")
for k, i in sorted(st.items(), key=lambda kv: (kv[1]["stage"], kv[0])):
    b = i.get("gridlytics-backend") or {}; f = i.get("gridlytics-frontend") or {}
    fs = i["findings"]; settled = ", ".join(f"{s} {sum(1 for v in fs.values() if v['status'] == s)}" for s in ("implemented", "partial", "already_fixed", "refuted", "deferred") if any(v["status"] == s for v in fs.values())) or "none"
    L.append(f"| {i['stage']} | `{k}` | {', '.join(map(str, i['batches_done'])) or 'none'} of {i['batches_total']} | {len(b.get('commits', []))} | {len(f.get('commits', []))} | {settled} | {', '.join(str(r['batch']) for r in i['remaining_batches']) or 'none'} |")
L.append("")
L.append("### 4.1 Per package: what is done and what remains\n")
for k, i in sorted(st.items(), key=lambda kv: (kv[1]["stage"], kv[0])):
    w = next(x for x in plan["wps"] if x["key"] == k)
    L.append(f"#### `{k}` (stage {i['stage']}): {i['title']}\n")
    L.append(f"Owns: {'; '.join(w['owns_files'])}.\n")
    if w.get("depends_on"):
        L.append(f"Depends on: {', '.join(w['depends_on'])}.\n")
    for repo in ("gridlytics-backend", "gridlytics-frontend"):
        r = i.get(repo) or {}
        if r.get("commits"):
            L.append(f"*{repo}* `{r['branch']}` at `{r['head']}`: " + "; ".join(c[:140] for c in r["commits"][:40]) + "\n")
    done = [(fid, v) for fid, v in i["findings"].items()]
    if done:
        L.append("Settled findings:\n")
        for fid, v in done:
            L.append(f"- `{fid}` **{v['status']}**: {v['summary'][:400]}" + (f" *Reason:* {v['reason'][:300]}" if v.get("reason") else ""))
        L.append("")
    if i["remaining_batches"]:
        L.append("Remaining batches (ids in order; severity in brackets):\n")
        for r in i["remaining_batches"]:
            L.append(f"- Batch {r['batch']} — {r['theme']}: " + ", ".join(f"`{x}` [{idx.get(x, {}).get('sev', '?')}] {idx.get(x, {}).get('title', '')[:110]}" for x in r["ids"]))
        L.append("")
    L.append(f"Planner's notes: {w['notes'][:2500]}\n")
table = "\n".join(L)
D = [f"- `{d['id']}`: {d['reason']}" for d in plan["deferred"]]
s = open(f"{A}/impl/handover_static.md").read()
import subprocess
wip = []
tot = {"implemented": 0, "partial": 0, "already_fixed": 0, "refuted": 0, "deferred": 0}
nb = nf = 0
for k, i in st.items():
    for v in i["findings"].values():
        tot[v["status"]] = tot.get(v["status"], 0) + 1
    for repo in ("gridlytics-backend", "gridlytics-frontend"):
        r = i.get(repo) or {}
        for c in r.get("commits", []):
            if " WIP (interrupted" in " " + c.split(" ", 1)[1]:
                wip.append(f"- `{k}` {repo} `{c.split(' ', 1)[0]}`: {c.split(' ', 1)[1][:150]}")
        nb += len(r.get("commits", [])) if repo == "gridlytics-backend" else 0
        nf += len(r.get("commits", [])) if repo == "gridlytics-frontend" else 0
done_b = sum(len(i["batches_done"]) for i in st.values()); tot_b = sum(i["batches_total"] for i in st.values())
s1 = [i for i in st.values() if i["stage"] == 1]
counts = (f"{tot['implemented'] + tot['partial']} findings implemented so far ({tot['implemented']} fully, {tot['partial']} partly; already fixed {tot['already_fixed']}, refuted {tot['refuted']}, deferred {tot['deferred']} by implementers), "
          f"in {sum(len(i['batches_done']) for i in s1)} of {sum(i['batches_total'] for i in s1)} stage-1 batches ({done_b} of {tot_b} batches in all stages); {nb} backend and {nf} web commits on the `audit/*` branches. "
          "Duplicates of a settled canonical finding are settled with it.")
s = s.replace("{{COUNTS}}", counts).replace("{{WIP}}", "\n".join(wip) or "none")
s = s.replace("{{TIME}}", datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M")).replace("{{PACKAGE_TABLE}}", table).replace("{{DEFERRED}}", "\n".join(D))
open(sys.argv[1], "w").write(s)
print("wrote", sys.argv[1], len(s) // 1024, "KB")
