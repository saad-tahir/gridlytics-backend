"""Poll a few API routes every second across the 15-minute ticks and record latency; read the runs table read-only.
Usage: tick_watch.py <out.jsonl> <until_epoch>"""
import json
import sqlite3
import sys
import time

sys.path.insert(0, "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit")
from api import client  # noqa: E402

out = open(sys.argv[1], "a")
until = float(sys.argv[2])
DB = "/home/user/saad-tahir/gridlytics-backend/.scratch/data/companies/6321e32a-d640-44b9-8cf3-bf06164b2e0c.db"
c = client("owner")
c.timeout = 120
paths = ["/api/sites/6d4db49bf87e/now", "/api/fleet", "/api/alerts/counts", "/api/sites/e9941b41c70b/plan"]
i = 0
while time.time() < until:
    p = paths[i % len(paths)]
    i += 1
    t = time.perf_counter()
    try:
        r = c.get(p)
        code = r.status_code
    except Exception as e:
        code = type(e).__name__
    dt = time.perf_counter() - t
    rec = {"t": round(time.time(), 2), "path": p, "s": round(dt, 3), "code": code}
    if i % 20 == 0:
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
            rec["runs"] = con.execute("select tick, count(*), min(started), max(finished) from runs group by tick order by tick desc limit 1").fetchone()
            con.close()
        except Exception as e:
            rec["runs"] = str(e)
    out.write(json.dumps(rec) + "\n")
    out.flush()
    time.sleep(max(0.0, 1.0 - dt))
