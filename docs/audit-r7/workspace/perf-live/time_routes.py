"""Time every GET route the web app uses, 3 calls each, per demo site. Writes routes.json.
Never prints tokens or passwords."""
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit")
from api import client  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "routes.json"
ROLE = sys.argv[2] if len(sys.argv) > 2 else "owner"
N = 3
SITES = {
    "6d4db49bf87e": "Faisalabad",
    "e9941b41c70b": "Karachi",
    "37b2fd882dcb": "Lahore",
    "47895916bed4": "RYK farm",
}
c = client(ROLE)
c.timeout = 300
PKT = timezone(timedelta(hours=5))
now = int(time.time())
today = datetime.now(PKT).date()
first = today.replace(day=1)
last_month_end = first - timedelta(days=1)
last_month_start = last_month_end.replace(day=1)

results = []


def timeit(label, path, site=None, n=N):
    times, sizes, codes = [], [], []
    for _ in range(n):
        t = time.perf_counter()
        try:
            r = c.get(path)
            dt = time.perf_counter() - t
            times.append(dt)
            sizes.append(len(r.content))
            codes.append(r.status_code)
        except Exception as e:  # timeout
            times.append(time.perf_counter() - t)
            sizes.append(0)
            codes.append(f"ERR {type(e).__name__}")
    s = sorted(times)
    row = {
        "label": label, "path": path, "site": site,
        "p50": round(statistics.median(s), 3), "p95": round(s[-1], 3), "first": round(times[0], 3),
        "bytes": max(sizes), "codes": codes,
    }
    results.append(row)
    print(f"{row['p50']:7.3f} {row['p95']:7.3f} {row['bytes']:>9} {codes[0]} {site or '-':>12} {label}", flush=True)
    return row


company = [
    ("meta", "/api/meta"), ("fleet", "/api/fleet"), ("alerts/counts", "/api/alerts/counts"),
    ("alerts active", "/api/alerts"), ("alerts all", "/api/alerts?state="), ("alerts/rules", "/api/alerts/rules"),
    ("demo", "/api/demo"), ("reports", "/api/reports"), ("tariffs", "/api/tariffs"),
    ("tariffs/fuel", "/api/tariffs/fuel"), ("drafts", "/api/drafts"), ("sites", "/api/sites"),
    ("users", "/api/users"), ("platform/org", "/api/platform/org"), ("platform/keys", "/api/platform/keys"),
    ("audit", "/api/audit?offset=0"), ("local/context", "/api/local/context"),
]
for label, p in company:
    timeit(label, p)

for sid, name in SITES.items():
    b = f"/api/sites/{sid}"
    timeit("site", b, name)
    timeit("now", b + "/now", name)
    timeit("overview/chart 24h", b + "/overview/chart?hours=24", name)
    timeit("plan", b + "/plan", name)
    r = c.get(b + "/plans")
    plans = r.json() if r.status_code == 200 else []
    timeit("plans", b + "/plans", name)
    if isinstance(plans, list) and plans:
        pid = plans[min(5, len(plans) - 1)]["id"]
        timeit("plans/:id", b + f"/plans/{pid}", name)
    timeit("plan/compare", b + "/plan/compare", name)
    timeit("overrides", b + "/overrides", name)
    timeit("control", b + "/control", name)
    timeit("edge", b + "/edge", name)
    timeit("telemetry/quality 14", b + "/telemetry/quality?days=14", name)
    timeit("telemetry/quality 30", b + "/telemetry/quality?days=30", name)
    timeit("native", b + "/native", name)
    timeit("savings this_month", b + f"/savings?start={first}&end={today}", name)
    timeit("savings last_month", b + f"/savings?start={last_month_start}&end={last_month_end}", name)
    timeit("savings last_30", b + f"/savings?start={today - timedelta(days=29)}&end={today}", name)
    timeit("switchback", b + "/switchback", name)
    timeit("bills", b + "/bills", name)
    timeit("savings/export.csv", b + f"/savings/export.csv?start={first}&end={today}", name)
    timeit("pilot", b + "/pilot", name)
    timeit("pilot/calendar.ics", b + "/pilot/calendar.ics", name)
    for lab, secs in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400), ("90d", 90 * 86400)):
        timeit(f"telemetry {lab} default names", b + f"/telemetry?start={now - secs}&end={now}", name)
    allnames = ("pv_power_w,load_power_w,grid_power_w,battery_power_w,genset_power_w,battery_soc_est_pct,"
                "battery_soc_bms_pct,grid_present,battery_temp_c,genset_fuel_l,grid_voltage_v")
    timeit("telemetry 7d all names", b + f"/telemetry?start={now - 7 * 86400}&end={now}&names={allnames}", name)
    timeit("telemetry 24h res=raw", b + f"/telemetry?start={now - 86400}&end={now}&names={allnames}&res=raw", name)
    timeit("telemetry grid_present 7d 1h", b + f"/telemetry?names=grid_present&res=1h&start={now - 7 * 86400}&end={now}", name)
    for d in (7, 28, 90):
        timeit(f"heatmap load {d}", b + f"/telemetry/heatmap?name=load_power_w&days={d}", name)
    timeit("telemetry/export.csv 14d", b + f"/telemetry/export.csv?start={now - 14 * 86400}&end={now}", name)
    timeit("forecasts/pv", b + "/forecasts/pv", name)
    timeit("forecasts/load", b + "/forecasts/load", name)
    timeit("forecasts/outage", b + "/forecasts/outage", name)
    timeit("forecasts/scores 30", b + "/forecasts/scores?days=30", name)
    for d in (7, 30, 90):
        timeit(f"outages {d}", b + f"/outages?days={d}", name)
    timeit("battery", b + "/battery", name)
    timeit("genset 14", b + "/genset?days=14", name)
    timeit("genset/fuel", b + "/genset/fuel?limit=200", name)
    timeit("prices", b + "/prices", name)

json.dump(results, open(OUT, "w"), indent=1)
print("written", OUT)
