import sys, json
sys.path.insert(0,'/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit')
from api import client
c=client('owner')
SITES={'fsd':'6d4db49bf87e','khi':'e9941b41c70b','lhr':'37b2fd882dcb','ryk':'47895916bed4'}
out={}
for k,s in SITES.items():
    r=c.get(f'/api/sites/{s}/savings',params={'start':'2026-10-01','end':'2026-11-09'})
    out[k]=r.json()
    b=r.json()
    print('=====',k, 'from',b['from'],'to',b['to'])
    for d in b['days']:
        L=d.get('levers') or {}
        print(d['day'], f"{d.get('saving_native_rs')!s:>10} {d.get('saving_tuned_rs')!s:>10} {d['fidelity']:<12} act={d.get('actual_cost_rs')} nat={d.get('native_cost_rs')} L=", {k2:round(v) for k2,v in L.items()}, 'wh',d.get('claim_withheld'), 'litres',d.get('litres_actual'),d.get('litres_native'),'pk',d.get('peak_kw'))
    n=b['baselines']['native']
    print('NATIVE', json.dumps({k2:v for k2,v in n.items()}))
json.dump(out,open('savings_oct_nov.json','w'),indent=1)
