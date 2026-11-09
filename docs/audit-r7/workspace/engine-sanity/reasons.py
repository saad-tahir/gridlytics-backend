import json, collections
A = json.load(open('allplans.json'))
H=0.25
for k, plans in A.items():
    c = collections.Counter(); kwh = collections.Counter()
    ex = collections.defaultdict(list)
    for p in plans:
        for i, s in enumerate(p['steps']):
            r = s['reason']; c[r] += 1
            if s['discharge_kw'] > 0.5 and not s['peak'] and s['grid_present'] > 0.5:
                kwh['offpeak_dis_'+r] += s['discharge_kw']*H
                if len(ex[r]) < 3: ex[r].append((p['id'], i, s['date'], s['local'], s['discharge_kw'], s['grid_import_kw'], s['soc_pct'], s['c_imp'], p['summary'].get('mdi_cap_kw')))
    print('=====', k, dict(c))
    print({a: round(b,1) for a,b in kwh.items()})
    for r, l in ex.items(): print('  ', r, l)
