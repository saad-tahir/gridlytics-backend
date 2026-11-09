import json, collections
from datetime import datetime, timezone, timedelta
A = json.load(open('allplans.json'))
D = json.load(open('pull.json'))
H=0.25
for k, plans in A.items():
    site = D[k]['site']['body']
    bat = site['assets'].get('battery'); gen = site['assets'].get('genset')
    En = bat['nameplate_kwh']
    print('==========', k)
    tot = collections.Counter()
    pk_dis_h=[]; pk_steps_tot=0
    for p in plans:
        st = p['steps']
        pk = [s for s in st if s['peak']]
        pk_steps_tot += len(pk)
        pd = sum(s['discharge_kw'] for s in pk)*H
        # grid charge at peak: charge > pv surplus and import > 0
        for i, s in enumerate(st):
            surplus = max(0, s['pv_forecast_kw'] - s['curtail_kw'] + s['genset_kw'] - s['load_kw'])
            gc = s['charge_kw'] - surplus
            if s['peak'] and gc > 0.5 and s['grid_import_kw'] > 0.5:
                tot['grid_charge_at_peak_steps'] += 1; tot['grid_charge_at_peak_kwh'] += gc*H
            if s['peak'] and s['grid_export_kw'] > 0.5:
                tot['export_at_peak_steps'] += 1
            if s['grid_export_kw'] > 0.5 and s['discharge_kw'] > 0.5:
                tot['export_while_discharging'] += 1
            if s['grid_import_kw'] > 0.5 and s['grid_export_kw'] > 0.5:
                tot['import_and_export'] += 1
            if s['charge_kw'] > 0.5 and s['discharge_kw'] > 0.5:
                tot['charge_and_discharge'] += 1
            if gc > 0.5 and s['grid_import_kw'] > 0.5 and not s['peak']:
                tot['grid_charge_offpeak_kwh'] += gc*H
            if s['discharge_kw'] > 0.5 and not s['peak'] and s['grid_present'] > 0.5:
                tot['offpeak_discharge_kwh'] += s['discharge_kw']*H
            if s['peak'] and s['discharge_kw'] > 0.5: tot['peak_discharge_kwh'] += s['discharge_kw']*H
            if s['grid_present'] < 0.5 and s['grid_import_kw'] > 1e-3: tot['import_in_outage'] += 1
            if s['unserved_kw'] > 0.1: tot['unserved_steps'] += 1; tot['unserved_kwh'] += s['unserved_kw']*H
            if s['curtail_kw'] > 0.5: tot['curtail_steps'] += 1; tot['curtail_kwh'] += s['curtail_kw']*H
        tot['plans'] += 1
    print({a: (round(b,1) if isinstance(b,float) else b) for a,b in tot.items()}, 'peak steps', pk_steps_tot)
