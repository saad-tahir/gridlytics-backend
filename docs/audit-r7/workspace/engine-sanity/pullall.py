import sys, json
sys.path.insert(0,'/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit')
from api import client
c = client('owner')
d=json.load(open('pull.json'))
SITES = {'fsd':'6d4db49bf87e','khi':'e9941b41c70b','lhr':'37b2fd882dcb','ryk':'47895916bed4'}
allp = {}
for k, sid in SITES.items():
    allp[k] = []
    for h in d[k]['plans']['body']:
        r = c.get(f'/api/sites/{sid}/plans/{h["id"]}')
        allp[k].append(r.json()['plan'])
json.dump(allp, open('allplans.json','w'))
print({k: len(v) for k, v in allp.items()})
