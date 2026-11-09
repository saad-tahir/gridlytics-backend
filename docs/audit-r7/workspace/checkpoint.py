"""Copy every completed agent result from all workflow journals into results/journal-results.json (idempotent)."""
import glob, json, os, time
BASE = '/root/.claude/projects/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/subagents/workflows/'
OUT = '/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit/results/journal-results.json'
def once():
    allres = {}
    for j in glob.glob(BASE + 'wf_*/journal.jsonl'):
        wf = j.split('/')[-2]
        try:
            rows = [json.loads(l) for l in open(j) if l.strip()]
        except Exception:
            continue
        lab = {r['key']: r.get('label') for r in rows if r.get('type') == 'started'}
        for r in rows:
            if r.get('type') == 'result' and r.get('result') is not None:
                key = f"{wf}:{lab.get(r['key'], r['key'])}"
                allres[key] = {'workflow': wf, 'label': lab.get(r['key']), 'result': r['result']}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = OUT + '.tmp'
    json.dump(allres, open(tmp, 'w'))
    os.replace(tmp, OUT)
    return len(allres)
if __name__ == '__main__':
    import sys
    if '--loop' in sys.argv:
        while True:
            n = once(); time.sleep(60)
    else:
        print('saved', once(), 'results')
