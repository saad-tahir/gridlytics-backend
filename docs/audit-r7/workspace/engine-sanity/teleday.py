import json, sys
from datetime import datetime, timezone, timedelta
PKT = timezone(timedelta(hours=5))
T = json.load(open('tele.json'))
k, day = sys.argv[1], sys.argv[2]
h0 = int(sys.argv[3]) if len(sys.argv)>3 else 0
h1 = int(sys.argv[4]) if len(sys.argv)>4 else 24
d = T[k]; s = d['series']
print('local  grid  imp_kW  pv_kW  load_kW bat_kW  soc  gen_on gen_kW fuel unserved curtail')
for i, t in enumerate(d['ts']):
    lt = datetime.fromtimestamp(t, PKT)
    if lt.date().isoformat() != day or not (h0 <= lt.hour < h1): continue
    def v(n, sc=1.0):
        x = s[n][i]; return '-' if x is None else round(x*sc, 1)
    print(lt.strftime('%H:%M'), v('grid_present'), v('grid_power_w', 1e-3), v('pv_power_w',1e-3), v('load_power_w',1e-3), v('battery_power_w',1e-3), v('battery_soc_est_pct'), v('genset_running'), v('genset_power_w',1e-3), v('genset_fuel_l'), v('unserved_w',1e-3), v('curtail_w',1e-3))
