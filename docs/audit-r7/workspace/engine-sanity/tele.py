import sys, json
sys.path.insert(0,'/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit')
from api import client
c = client('owner')
SITES = {'fsd':'6d4db49bf87e','khi':'e9941b41c70b','lhr':'37b2fd882dcb','ryk':'47895916bed4'}
names = "grid_present,grid_power_w,grid_import_wh,pv_power_w,load_power_w,battery_power_w,battery_soc_est_pct,genset_running,genset_power_w,genset_fuel_l,unserved_w,curtail_w"
out = {}
for k, sid in SITES.items():
    r = c.get(f'/api/sites/{sid}/telemetry', params={'start': '2026-10-27T19:00:00Z', 'end': '2026-11-09T08:00:00Z', 'names': names, 'res': '15m'})
    out[k] = r.json()
    print(k, r.status_code, len(out[k].get('ts', [])))
json.dump(out, open('tele.json', 'w'))
