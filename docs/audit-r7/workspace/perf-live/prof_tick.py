"""Profile one scheduler tick on a COPY of the company database (never the live data). Times each phase per site.
Run: GRIDLYTICS_DATA_DIR=<copy> GRIDLYTICS_DB=<copy>/x.db GRIDLYTICS_LOGS=0 python -I prof_tick.py <copy company db> [at_epoch]"""
import functools
import json
import sys
import time

sys.path.insert(0, "/home/user/saad-tahir/gridlytics-backend/backend")
from gridlytics import alerts, demo, forecast, optimise, plans, sites, store, delivery, scheduler, battery  # noqa: E402

path = sys.argv[1]
T = {}


def wrap(mod, name, key):
    f = getattr(mod, name)

    @functools.wraps(f)
    def g(*a, **k):
        t = time.perf_counter()
        try:
            return f(*a, **k)
        finally:
            T.setdefault(key, []).append(time.perf_counter() - t)
    setattr(mod, name, g)


wrap(demo, "advance", "demo.advance")
wrap(plans, "tick", "plans.tick")
wrap(alerts, "evaluate", "alerts.evaluate")
wrap(forecast, "issue", "forecast.issue")
wrap(plans, "build_problem", "build_problem")
wrap(optimise, "solve_with_policy", "solve_with_policy")
wrap(delivery, "compile_mode_a", "compile_mode_a")
wrap(plans, "active_at", "plans.active_at")
wrap(plans, "current", "plans.current")
wrap(battery, "state", "battery.state")

per_site = []
with store.use_company(path):
    for s in sites.listing():
        if s.get("status") != "active":
            continue
        T.clear()
        t0 = time.perf_counter()
        err = None
        try:
            if s.get("demo_link"):
                demo.advance(s["id"])
            r = plans.tick(s["id"])
            alerts.evaluate(s["id"])
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:200]
        wall = time.perf_counter() - t0
        row = {"site": s["id"], "name": s["name"], "wall": round(wall, 2), "err": err,
               **{k: [round(sum(v), 3), len(v)] for k, v in T.items()}}
        per_site.append(row)
        print(json.dumps(row), flush=True)
print("TOTAL", round(sum(r["wall"] for r in per_site), 2))
