import sys, json
sys.path.insert(0,'/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit')
from api import client
c=client('owner')
SITES={'fsd':'6d4db49bf87e','khi':'e9941b41c70b','lhr':'37b2fd882dcb'}
days=['2026-08-15','2026-09-15','2026-10-20','2026-10-31','2026-11-01','2026-11-30','2026-12-01','2026-12-24','2027-01-05','2026-02-11','2026-02-12','2026-03-15','2026-05-15']
for k,s in SITES.items():
    for d in days:
        r=c.get(f'/api/sites/{s}/prices',params={'day':d})
        if r.status_code!=200: print(k,d,r.status_code,r.text[:200]); continue
        b=r.json()
        ci=b['c_imp']; ce=b['c_exp']; pk=b['peak']
        idx=[i for i,p in enumerate(pk) if p]
        win=(f"{idx[0]//4:02d}:{idx[0]%4*15:02d}-{(idx[-1]+1)//4:02d}:{(idx[-1]+1)%4*15:02d}") if idx else None
        print(k,d,'offpk',sorted(set(ci[i] for i in range(len(ci)) if not pk[i])),'pk',sorted(set(ci[i] for i in idx)),'exp',sorted(set(ce)),'win',win,'n',len(ci),'ef',b.get('energy_factor'),'mdi',b.get('mdi'))
