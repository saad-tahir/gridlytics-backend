import json, collections
from datetime import datetime, timezone, timedelta
PKT = timezone(timedelta(hours=5))
T = json.load(open('tele.json'))
win = {'fsd': (18*60, 22*60), 'khi': (18*60, 22*60), 'lhr': (18*60, 22*60)}
for k in ('fsd','khi','lhr'):
    s = T[k]['series']; ts = T[k]['ts']
    agg = collections.defaultdict(lambda: collections.Counter())
    for i, t in enumerate(ts):
        lt = datetime.fromtimestamp(t, PKT); m = lt.hour*60+lt.minute
        d = lt.date().isoformat()
        b = (s['battery_power_w'][i] or 0)/1000; g = s['grid_present'][i]
        pk = win[k][0] <= m < win[k][1]
        if g is None: continue
        if pk and b > 0 and g > 0.5: agg[d]['peak_dis'] += b*0.25
        if pk and b < 0 and g > 0.5:
            pv = (s['pv_power_w'][i] or 0)/1000; load = (s['load_power_w'][i] or 0)/1000
            gc = max(0, -b - max(0, pv-load))
            agg[d]['peak_gridcharge'] += gc*0.25
        if not pk and b > 0 and g > 0.5: agg[d]['offpeak_dis'] += b*0.25
        if b < 0 and g > 0.5 and not pk:
            pv = (s['pv_power_w'][i] or 0)/1000; load = (s['load_power_w'][i] or 0)/1000
            agg[d]['offpeak_gridcharge'] += max(0, -b - max(0, pv-load))*0.25
        if g < 0.5: agg[d]['outage_min'] += 15
        if (s['unserved_w'][i] or 0) > 0: agg[d]['unserved_kwh'] += s['unserved_w'][i]/1000*0.25
    print('=====', k)
    for d in sorted(agg): print(d, {a: round(v,1) for a, v in agg[d].items()})
