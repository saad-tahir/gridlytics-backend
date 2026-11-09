import sqlite3, sys, datetime as dt
DB='/home/user/saad-tahir/gridlytics-backend/.scratch/data/companies/6321e32a-d640-44b9-8cf3-bf06164b2e0c-telemetry.db'
con=sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
PKT=dt.timezone(dt.timedelta(hours=5))
def day(site, d, peak=(18*60,22*60), off=27.83, pk=40.02, wear=52000/(6000*0.8)):
    t0=int(dt.datetime.fromisoformat(d).replace(tzinfo=PKT).timestamp()); t1=t0+86400
    rows=con.execute("select ts, grid_power_w, grid_import_wh, grid_export_wh, battery_power_w, load_power_w, pv_power_w, genset_power_w, genset_fuel_l, grid_present, battery_soc_est_pct, battery_soc_bms_pct from minute where site_id=? and ts>=? and ts<? order by ts",(site,t0,t1)).fetchall()
    n=len(rows); e_pk=e_off=0; dis=0; mx=0; blocks={}
    nimp_null=0; via_power=0
    for r in rows:
        ts,gp,gi,ge,bp,lp,pv,gen,fuel,gpres,soc,socb=r
        m=(ts-t0)//60
        if gi is not None: kwh=gi/1000
        elif gp is not None: kwh=max(gp,0)/1000/60; via_power+=1
        else: kwh=0; nimp_null+=1
        if peak[0]<=m<peak[1]: e_pk+=kwh
        else: e_off+=kwh
        if bp is not None and bp>0: dis+=bp/1000/60
        b=m//30; blocks[b]=blocks.get(b,0)+kwh
    first=rows[0]; last=rows[-1]
    mdi=max(blocks.values())*2
    cost=e_pk*pk+e_off*off
    print(site,d,'rows',n,'imp_pk',round(e_pk,2),'imp_off',round(e_off,2),'tot',round(e_pk+e_off,2),'energy_rs',round(cost,2),'dis',round(dis,2),'wear',round(dis*wear,2),'total',round(cost+dis*wear,2),'mdi30',round(mdi,2),'nullimp',nimp_null,'via_power',via_power,'soc0',first[10],first[11],'socend',last[10],last[11])
day('e9941b41c70b','2026-11-05')
day('e9941b41c70b','2026-11-04')
day('e9941b41c70b','2026-11-07')
day('6d4db49bf87e','2026-11-08',off=26.98,pk=40.02)
day('6d4db49bf87e','2026-10-29',off=28.10,pk=41.15)
day('37b2fd882dcb','2026-11-05',off=39.48,pk=48.28)
