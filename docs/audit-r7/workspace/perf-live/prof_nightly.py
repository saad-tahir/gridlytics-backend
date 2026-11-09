"""On a COPY of the company database: time the nightly job per site, a 62-day savings recompute, report generation, and the heavier reads.
GRIDLYTICS_DATA_DIR=<copy> GRIDLYTICS_DB=<copy>/default.db GRIDLYTICS_LOGS=0 python -I prof_nightly.py <copy company db>"""
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/home/user/saad-tahir/gridlytics-backend/backend")
from gridlytics import alerts, plans, reports, scheduler, sites, store, telemetry, twin, verify  # noqa: E402

path = sys.argv[1]
PKT = timezone(timedelta(hours=5))
DEMO = {"6d4db49bf87e": "Faisalabad", "e9941b41c70b": "Karachi", "37b2fd882dcb": "Lahore", "47895916bed4": "RYK farm"}


def T(label, f):
    t = time.perf_counter()
    try:
        r = f()
        err = None
    except Exception as e:  # noqa: BLE001
        r, err = None, f"{type(e).__name__}: {e}"[:160]
    print(f"{time.perf_counter() - t:8.3f} s  {label}" + (f"  ERR {err}" if err else ""), flush=True)
    return r


with store.use_company(path):
    y = datetime.now(PKT).date() - timedelta(days=1)
    for sid, name in DEMO.items():
        conn = (sites.connections(sid) or [None])[0]
        T(f"{name} nightly aggregate_day", lambda: telemetry.aggregate_day(sid, y, plans.peak_mask(conn, twin.minutes_of(y)) if conn else None))
        T(f"{name} nightly verify.compute_day", lambda: verify.compute_day(sid, y))
        T(f"{name} nightly alerts.nightly", lambda: alerts.nightly(sid, y))
    T("nightly_company (all active sites)", lambda: scheduler.nightly_company(path))
    for sid, name in DEMO.items():
        T(f"{name} savings recompute 14 days (compute_range)", lambda: verify.compute_range(sid, y - timedelta(days=13), y))
    for kind in ("monthly_savings", "energy_only", "daily_operations"):
        T(f"report {kind} Faisalabad 2026-11", lambda: reports.generate(kind, "6d4db49bf87e", "2026-11", "perf"))
        T(f"report {kind} Faisalabad 2026-10", lambda: reports.generate(kind, "6d4db49bf87e", "2026-10", "perf"))
