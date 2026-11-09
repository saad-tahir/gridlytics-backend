import json, sys
d=json.load(open('pull.json'))
k=sys.argv[1]
p=d[k]['plan']['body']['plan']
print(p['id'], p['generated_at'], p['status'], p['summary'].get('tier'), 'recommendation', p['summary'].get('recommendation'))
cols=['local','date','peak','c_imp','c_exp','pv_forecast_kw','load_kw','critical_kw','grid_import_kw','grid_export_kw','charge_kw','discharge_kw','soc_pct','reserve_pct','genset_kw','genset_on','unserved_kw','curtail_kw','p_start','p_down','grid_present','reason']
print(' '.join(c[:7] for c in cols))
step = int(sys.argv[2]) if len(sys.argv)>2 else 1
for s in p['steps'][::step]:
    print(' '.join(str(s.get(c))[:7].ljust(7) for c in cols), json.dumps(s.get('detail'))[:120])
