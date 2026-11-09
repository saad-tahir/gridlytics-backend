import json, collections
from datetime import datetime, timezone, timedelta
A = json.load(open('allplans.json'))
D = json.load(open('pull.json'))
PKT = timezone(timedelta(hours=5))
LOAD_CUT = datetime(2026,11,8,14,45,tzinfo=timezone.utc).timestamp()
def ts(iso): return datetime.fromisoformat(iso).timestamp()
for k, plans in A.items():
    site = D[k]['site']['body']
    bat = site['assets'].get('battery'); gen = site['assets'].get('genset')
    En = bat['nameplate_kwh']
    print('==========', k, site['name'], 'plans', len(plans))
    cnt = collections.Counter()
    for p in plans:
        sm = p['summary']; so = sm['solver']
        cnt['status:'+p['status']] += 1
        cnt['solver:'+so['status']] += 1
        if so.get('time_limited'): cnt['time_limited'] += 1
        if so.get('relaxed'): cnt['relaxed'] += 1
        if so.get('threshold_repair'): cnt['threshold_repair'] += 1
        if so.get('shed_rule_resolve'): cnt['shed_rule_resolve'] += 1
        if so.get('retry_of'): cnt['retry_of'] += 1
        if (so.get('mip_gap') or 0) > 1e-3: cnt['gap>1e-3'] += 1
        if sm.get('guard'): cnt['guard:'+str(sm['guard'])[:40]] += 1
        for n in sm.get('notes') or []: cnt['note:'+n] += 1
        gen_at = ts(p['generated_at']); bt = ts(p['belief_time'])
        if bt % 900: cnt['belief_not_quarter'] += 1
        st = p['steps']
        if any(s['t'] % 900 for s in st): cnt['step_not_quarter'] += 1
        if len(st) != 192: cnt[f'nsteps_{len(st)}'] += 1
    print(dict(cnt))
    # time-limited by load window
    tl_before = sum(1 for p in plans if p['summary']['solver'].get('time_limited') and ts(p['generated_at']) < LOAD_CUT)
    tl_after = sum(1 for p in plans if p['summary']['solver'].get('time_limited') and ts(p['generated_at']) >= LOAD_CUT)
    nb = sum(1 for p in plans if ts(p['generated_at']) < LOAD_CUT)
    print('time_limited before load cut', tl_before, '/', nb, ' after', tl_after, '/', len(plans)-nb)
    secs = [p['summary']['solver'].get('solve_seconds') or 0 for p in plans]
    print('solve secs max', max(secs), 'mean', sum(secs)/len(secs))
