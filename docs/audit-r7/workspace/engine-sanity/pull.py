import sys, json
sys.path.insert(0,'/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit')
from api import client
c = client('owner')
SITES = {'fsd':'6d4db49bf87e','khi':'e9941b41c70b','lhr':'37b2fd882dcb','ryk':'47895916bed4'}
out = {}
for k, sid in SITES.items():
    d = {}
    for name, path in [('site', f'/api/sites/{sid}'), ('plan', f'/api/sites/{sid}/plan'), ('plans', f'/api/sites/{sid}/plans?limit=200'),
                       ('now', f'/api/sites/{sid}/now'), ('outages', f'/api/sites/{sid}/outages'), ('prices', f'/api/sites/{sid}/prices'),
                       ('fc_pv', f'/api/sites/{sid}/forecasts/pv'), ('fc_load', f'/api/sites/{sid}/forecasts/load'), ('fc_outage', f'/api/sites/{sid}/forecasts/outage'),
                       ('control', f'/api/sites/{sid}/control'), ('battery', f'/api/sites/{sid}/battery'), ('genset', f'/api/sites/{sid}/genset'),
                       ('overrides', f'/api/sites/{sid}/overrides'), ('compare', f'/api/sites/{sid}/plan/compare')]:
        r = c.get(path)
        try:
            d[name] = {'status': r.status_code, 'body': r.json()}
        except Exception:
            d[name] = {'status': r.status_code, 'body': r.text[:500]}
    out[k] = d
json.dump(out, open('pull.json','w'))
for k, d in out.items():
    print(k, {n: v['status'] for n, v in d.items()})
