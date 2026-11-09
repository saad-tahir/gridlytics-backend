"""Upload a large CSV to a scratch AUDIT site and measure: total time, the API's RSS, and other requests' latency meanwhile.
upload_test.py <csv> <site_id|new> <out.json>"""
import json
import sys
import threading
import time

sys.path.insert(0, "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit")
from api import client  # noqa: E402

csv_path, site, out = sys.argv[1], sys.argv[2], sys.argv[3]
c = client("owner")
c.timeout = 900
if site == "new":
    r = c.post("/api/sites", json={"name": "AUDIT perf upload", "city": "Lahore", "disco": "LESCO", "site_type": "industrial_2_shift"})
    print("create", r.status_code, r.text[:200])
    site = r.json()["id"]
print("site", site)
PID = int(sys.argv[4]) if len(sys.argv) > 4 else 183


def rss():
    for line in open(f"/proc/{PID}/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1]) // 1024
    return None


stop = False
lat, mem = [], []


def poll():
    p = client("analyst")
    while not stop:
        t = time.perf_counter()
        try:
            code = p.get("/api/alerts/counts").status_code
        except Exception as e:  # noqa: BLE001
            code = type(e).__name__
        lat.append((round(time.time(), 2), round(time.perf_counter() - t, 3), code))
        mem.append((round(time.time(), 2), rss()))
        time.sleep(0.25)


th = threading.Thread(target=poll, daemon=True)
rss0 = rss()
th.start()
time.sleep(2)
body = open(csv_path, "rb").read()
t = time.perf_counter()
r = c.post(f"/api/sites/{site}/telemetry/upload?tz_offset_h=0&file_name=perf.csv", content=body, headers={"Content-Type": "text/csv"})
dt = time.perf_counter() - t
time.sleep(2)
stop = True
th.join()
res = {"site": site, "bytes": len(body), "status": r.status_code, "seconds": round(dt, 2), "answer": (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:300]),
       "rss_before_mb": rss0, "rss_peak_mb": max(m[1] for m in mem if m[1]), "rss_after_mb": rss(),
       "other_requests_max_s": max(x[1] for x in lat), "other_requests_p50_s": sorted(x[1] for x in lat)[len(lat) // 2], "n_polls": len(lat)}
if isinstance(res["answer"], dict):
    res["answer"].pop("log", None)
print(json.dumps(res, indent=1))
json.dump({"res": res, "lat": lat, "mem": mem}, open(out, "w"))
