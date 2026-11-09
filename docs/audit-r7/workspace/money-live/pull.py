import sys, json
sys.path.insert(0,'/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit')
from api import client
c=client('owner')
SITES={'fsd':'6d4db49bf87e','khi':'e9941b41c70b','lhr':'37b2fd882dcb','ryk':'47895916bed4'}
out={}
def g(p):
    r=c.get(p)
    try: return {'status':r.status_code,'body':r.json()}
    except Exception: return {'status':r.status_code,'text':r.text[:3000]}
out['tariffs']=g('/api/tariffs')
out['fleet']=g('/api/fleet')
for k,s in SITES.items():
    out[k+'_site']=g(f'/api/sites/{s}')
    out[k+'_prices']=g(f'/api/sites/{s}/prices')
    out[k+'_savings']=g(f'/api/sites/{s}/savings')
    out[k+'_bills']=g(f'/api/sites/{s}/bills')
json.dump(out,open('pull1.json','w'),indent=1)
for k,v in out.items(): print(k, v['status'], len(json.dumps(v)))
