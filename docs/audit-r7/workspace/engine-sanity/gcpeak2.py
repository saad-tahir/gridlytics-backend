import json, sys
A = json.load(open('allplans.json'))
H=0.25
native = {'fsd': {'2026-11-0%d'%i for i in range(1,8)}}
for k in ('fsd','lhr','khi'):
    rows=[]
    for p in A[k]:
        st=p['steps']
        kwh=0; first=None; kwh12=0
        for i,s in enumerate(st):
            surplus = max(0, s['pv_forecast_kw'] - s['curtail_kw'] + s['genset_kw'] - s['load_kw'])
            gc = s['charge_kw'] - surplus
            if s['peak'] and gc > 0.5 and s['grid_import_kw'] > 0.5:
                kwh += gc*H
                if i < 12: kwh12 += gc*H
                if first is None: first=(i, s['date'], s['local'])
        if kwh>0: rows.append((p['generated_at'][:16], p['id'], round(kwh,1), round(kwh12,1), first, 'NATIVE-day' if p['steps'][0]['date'] in native.get(k,()) else ''))
    print(k, len(rows))
    for r in rows: print('  ', r)
