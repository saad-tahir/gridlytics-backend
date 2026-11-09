import sqlite3, json, sys
DB='/home/user/saad-tahir/gridlytics-backend/.scratch/data/companies/6321e32a-d640-44b9-8cf3-bf06164b2e0c.db'
con=sqlite3.connect(f'file:{DB}?mode=ro', uri=True); con.row_factory=sqlite3.Row
SITES={'fsd':'6d4db49bf87e','khi':'e9941b41c70b','lhr':'37b2fd882dcb','ryk':'47895916bed4'}
for k,s in SITES.items():
    print('=====',k)
    for r in con.execute("SELECT day_local, data, computed FROM savings_days WHERE site_id=? AND baseline='native' ORDER BY day_local",(s,)):
        d=json.loads(r['data']); a=d['actual']; n=d['baseline']
        print(r['day_local'], r['computed'][:16], 'pkA',a['peak_kw'],'pkN',n['peak_kw'],'eA',a['e_end_kwh'],'eN',n['e_end_kwh'],'v',a['v_end'],'dis',a['discharge_kwh'],n['discharge_kwh'],'imp',a['import_kwh'],n['import_kwh'],'pkimp',a['peak_import_kwh'],n['peak_import_kwh'],'L',a['litres'],n['litres'],'gh',a['genset_hours'],n['genset_hours'],'PF',d.get('perfect_foresight_rs'),'carry',d['levers']['carry_over'])
