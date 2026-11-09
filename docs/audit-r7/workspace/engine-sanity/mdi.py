import json, sys, collections
A = json.load(open('allplans.json'))
k = sys.argv[1]; pid = sys.argv[2] if len(sys.argv)>2 else None
for p in A[k]:
    if pid and p['id'] != pid: continue
    st = p['steps']; cap = p['summary'].get('mdi_cap_kw')
    blocks = collections.defaultdict(list)
    for s in st: blocks[s['t']//1800].append(s)
    worst = []
    for b, ss in blocks.items():
        g = sum(s['grid_import_kw'] for s in ss)/2
        gh = sum(s['grid_import_kw'] + max(0, s['load_hi_kw']-s['load_kw'])*(s['grid_present']>0.5) for s in ss)/2
        worst.append((round(g,1), round(gh,1), ss[0]['date'], ss[0]['local'], len(ss)))
    worst.sort(key=lambda x: -x[1])
    if pid: print('cap', cap, 'top blocks (mean G, mean G+Lhi-L)', worst[:8])
    else:
        mx = max(w[0] for w in worst); mxh = max(w[1] for w in worst)
        if mx > cap + 0.5 or mxh > cap + 0.5: print(p['id'], p['generated_at'], 'cap', cap, 'max G block', mx, 'max G+hi block', mxh, 'mdi_rise_rs', p['summary']['breakdown']['mdi_rise_rs'])
