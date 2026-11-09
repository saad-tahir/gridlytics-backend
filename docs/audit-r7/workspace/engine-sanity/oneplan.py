import json, sys
A = json.load(open('allplans.json'))
k, pid = sys.argv[1], sys.argv[2]
a = int(sys.argv[3]) if len(sys.argv)>3 else 0
b = int(sys.argv[4]) if len(sys.argv)>4 else 192
p = next(x for x in A[k] if x['id']==pid)
print(p['id'], p['generated_at'], p['status'], json.dumps(p['summary'].get('outage_now')), 'breakdown', json.dumps(p['summary']['breakdown']))
cols=['local','peak','c_imp','pv_forecast_kw','load_kw','critical_kw','grid_import_kw','grid_export_kw','charge_kw','discharge_kw','soc_start_pct','soc_pct','reserve_pct','genset_kw','genset_on','litres','genset_dump_kw','unserved_kw','curtail_kw','p_start','p_down','grid_present','reason']
print('i   '+' '.join(c[:7].ljust(7) for c in cols))
for i,s in enumerate(p['steps'][a:b], start=a):
    print(str(i).ljust(4)+' '.join(str(s.get(c))[:7].ljust(7) for c in cols), json.dumps(s.get('detail'))[:80])
