"""During a CSV upload to the AUDIT perf site, poll a telemetry-reading route of another site and a route that reads no telemetry."""
import sys, time, threading, json
sys.path.insert(0, "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit")
from api import client
csvp, out = sys.argv[1], sys.argv[2]
c = client("owner"); c.timeout = 600
stop = False; rec = {"tele": [], "plain": []}
def poll(key, path):
    p = client("analyst"); p.timeout = 120
    while not stop:
        t = time.perf_counter(); r = p.get(path); rec[key].append((round(time.time(), 2), round(time.perf_counter() - t, 3), r.status_code)); time.sleep(0.2)
ths = [threading.Thread(target=poll, args=("tele", "/api/sites/6d4db49bf87e/telemetry/quality?days=14")), threading.Thread(target=poll, args=("plain", "/api/alerts/counts"))]
# quality reads telemetry.first_ts (minute table) and the imports list; alerts/counts reads only the workspace file
for th in ths: th.start()
time.sleep(2)
body = open(csvp, "rb").read()
t = time.perf_counter()
r = c.post("/api/sites/22029dfe45a7/telemetry/upload?tz_offset_h=0&file_name=perf60.csv", content=body, headers={"Content-Type": "text/csv"})
dt = time.perf_counter() - t
time.sleep(2); stop = True
for th in ths: th.join()
s = lambda k: sorted(x[1] for x in rec[k])
print(json.dumps({"upload_s": round(dt, 2), "status": r.status_code, "rows": r.json().get("rows"),
  "tele_max": s("tele")[-1], "tele_p50": s("tele")[len(rec["tele"]) // 2], "tele_n": len(rec["tele"]),
  "plain_max": s("plain")[-1], "plain_p50": s("plain")[len(rec["plain"]) // 2], "plain_n": len(rec["plain"])}))
json.dump(rec, open(out, "w"))
