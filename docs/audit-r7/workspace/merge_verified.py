"""merge_verified.py — turn a verify-fix-batched workflow result into the shape render.py expects.

    python3 -I merge_verified.py <workflow-result.json or journal.jsonl> --out results/verified-<slice>.json

The workflow returns {file, results:[{id, dim, repo, area, sev, keep, verdicts, severity_final, design_wrong_votes, corrected_title, fix}]}.
This joins each row with the full finding from `file` (by id) and writes {"results": [finding + verification fields]}.
"""
import json
import sys


def load_result(path):
    if path.endswith(".jsonl"):
        res = None
        for line in open(path, encoding="utf8"):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("type") == "result" and isinstance(row.get("result"), dict) and "results" in row["result"]:
                res = row["result"]
            if row.get("type") in ("workflow_result", "return", "done") and isinstance(row.get("result"), dict) and "results" in row["result"]:
                res = row["result"]
        if res is None:
            raise SystemExit("no workflow result with 'results' in " + path)
        return res
    d = json.load(open(path, encoding="utf8"))
    return d.get("result", d)


def main():
    src = sys.argv[1]
    out = sys.argv[sys.argv.index("--out") + 1]
    res = load_result(src)
    findings = {f["id"]: f for f in json.load(open(res["file"], encoding="utf8"))}
    merged = []
    for r in res["results"]:
        f = dict(findings.get(r["id"], {}))
        if not f:
            continue
        f.update({k: v for k, v in r.items() if k not in ("sev",)})
        if r.get("corrected_title"):
            f["title"] = r["corrected_title"]
        merged.append(f)
    json.dump({"results": merged}, open(out, "w", encoding="utf8"), indent=1)
    kept = sum(1 for m in merged if m.get("keep"))
    fixed = sum(1 for m in merged if m.get("fix"))
    print(f"wrote {out}: {len(merged)} rows, {kept} kept, {fixed} with fix, {len(merged) - kept} refuted")


if __name__ == "__main__":
    main()
