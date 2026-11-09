import json, sys
A = json.load(open('allplans.json'))
H=0.25
k=sys.argv[1]
n=0
for p in A[k]:
    st=p['steps']
    rows=[]
    for i,s in enumerate(st):
        surplus = max(0, s['pv_forecast_kw'] - s['curtail_kw'] + s['genset_kw'] - s['load_kw'])
        gc = s['charge_kw'] - surplus
        if s['peak'] and gc > 0.5 and s['grid_import_kw'] > 0.5:
            rows.append((i, s['date'], s['local'], round(gc,1), s['grid_import_kw'], s['soc_start_pct'], s['soc_pct'], s['reserve_pct'], s['p_start'], s['p_down'], s['grid_present'], s['reason'], s['detail'], s['c_imp']))
    if rows:
        n+=1
        print(p['id'], p['generated_at'], 'outage_now', p['summary'].get('outage_now'))
        for r in rows: print('   ', r)
print('plans with grid charging at peak', n)
