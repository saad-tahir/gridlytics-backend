"""The scheduler (plan Part 2.4 and 10.6): the 15-minute tick and the nightly jobs, for every company Gridlytics is on for.

Off unless GRIDLYTICS_SCHEDULER=1 (the launcher sets it), so tests and a second worker never tick twice. One APScheduler BackgroundScheduler in the API
process for v1. A company's sites are ticked on a small pool of worker threads (tick_workers; plan Part 10.6: the solves on a ThreadPoolExecutor of 2
to 4), each tick is timed and logged and kept in the `ticks` table, and a tick that runs into the next quarter raises the tick_overrun alert; at about
90 sites per core the solves move to worker processes behind the same `tick_company` function.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import alerts, demo, forecast, plans, platform as platformdb, sites, store, tariffs, telemetry, verify

log = logging.getLogger("gridlytics.scheduler")
PKT = timezone(timedelta(hours=5))
_sched = None
#: the tick starts this many seconds after each quarter (start: the cron's second=20)
TICK_OFFSET_S = 20
#: the nightly table job (plan Part 7.1 step 7): after the 23:00 solve, the next day's Mode A table is issued to the inverter (plans.issue_nightly)
NIGHTLY_TABLES_AT = (23, 5)


def _after_tick(site_id: str) -> None:
    """What follows a site's solve: today's table issued or re-issued when worth a write (plans.issue_tables), then the "it took" card of today's
    keyed table (plans.early_check, plan Part 9.5). A failure of either is logged, never the tick's."""
    for what, fn in (("issuing the table", plans.issue_tables), ("the early check of the keyed table", plans.early_check)):
        try:
            fn(site_id)
        except Exception:
            log.exception("%s failed for site %s", what, site_id)


#: GRIDLYTICS_ERROR_WEBHOOK names an https address (http only on this machine) a record of each failed scheduled tick is posted to as JSON, in the
#: background, so any error tracker with a webhook can sit behind it (the family's observability: Solarlytics' SOLARLYTICS_ERROR_WEBHOOK; round 7,
#: parity-architecture-06). The record carries no error text (it may quote a site's figures), only its type and code.
def _post_record(url: str, record: dict) -> None:
    import urllib.request
    try:
        req = urllib.request.Request(url, data=json.dumps(record).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as exc:  # the tracker being down never matters to the tick
        log.warning("the error record of site %s was not posted: %s", record.get("site_id"), type(exc).__name__)


def _report_error(record: dict) -> None:
    url = os.environ.get("GRIDLYTICS_ERROR_WEBHOOK", "")
    if not url:
        return
    from urllib.parse import urlparse
    try:
        parts = urlparse(url)
        fit = (parts.scheme == "https" and bool(parts.hostname)) or (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"))
    except ValueError:
        fit = False
    if fit:
        threading.Thread(target=_post_record, args=(url, record), daemon=True).start()


def error_code(exc: BaseException) -> str:
    """A tick's error as the code its runs row, its plan_failed alert and GET /plan's `failing` carry: no_tariff_version (a connection's tariff has
    no version in force for the days planned), else tick_error."""
    return "no_tariff_version" if isinstance(exc, tariffs.NoTariffVersion) else "tick_error"


def _run_row(site_id: str, tick: str, started: str, status: str, reason: str = "", plan_id: str | None = None, solve_s: float | None = None,
             objective_rs: float | None = None, error: str = "") -> None:
    """The scheduled tick's durable runs row for a site (plan Part 2.4), written here only (round 7, performance-live-09: a manual re-solve in the
    same quarter overwrote it, and a tick that raised wrote none)."""
    with store.connect() as con:
        con.execute("INSERT OR REPLACE INTO runs (site_id, tick, started, finished, status, reason, solve_s, objective_rs, plan_id, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (site_id, tick, started, store._now(), status, reason or "", solve_s, objective_rs, plan_id, error))


def _now_s() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def tick_workers() -> int:
    """How many sites a company's tick solves at once (round 7, performance-live-11: every site of every company ran one after another in one
    thread, so a tick's length grew with the fleet and an overrun skipped the next quarter): GRIDLYTICS_TICK_WORKERS (1 to 8), else one fewer than
    the machine's cores, 1 to 4 (plan Part 10.6: a ThreadPoolExecutor of 2 to 4), so a core stays free for the API's requests."""
    cores = max(1, (os.cpu_count() or 2) - 1)
    try:
        asked = int(os.environ.get("GRIDLYTICS_TICK_WORKERS", "") or 0)
    except ValueError:
        asked = 0
    return max(1, min(8, asked)) if asked > 0 else max(1, min(4, cores))


def _site_tick(path, s: dict, tick: str) -> tuple[str, float]:
    """One site's part of a scheduled tick, on a worker thread of the company's file: a demo site's twin catches up to now, the solve (plans.tick),
    its runs row, today's table and the early check (_after_tick), then the site's alerts. A tick that raises writes an `error` runs row with its
    code and the error's type and words, is posted to the error webhook, and the alerts are still evaluated, so plan_failed says so (round 7,
    parity-architecture-06). The runs row keeps the site's whole time, alerts included (`seconds`; round 7, performance-live-09). Returns the runs
    status and that time."""
    t = time.monotonic()
    with store.use_company(path):
        started = store._now()
        try:
            if s.get("demo_link"):
                demo.advance(s["id"])
            r = plans.tick(s["id"])
            status = r["status"]
            _run_row(s["id"], tick, started, status, r.get("reason") or r.get("solver_status") or "", r.get("plan_id") or r.get("row_id"),
                     r.get("solve_seconds"), r.get("objective_rs"))
            _after_tick(s["id"])
        except Exception as exc:
            log.exception("tick failed for site %s", s["id"])
            status = "error"
            try:
                _run_row(s["id"], tick, started, status, error_code(exc), error=f"{type(exc).__name__}: {exc}"[:500])
            except Exception:
                log.exception("the failed tick of site %s was not recorded", s["id"])
            _report_error({"event": "tick_failed", "company": getattr(path, "stem", str(path)), "site_id": s["id"], "tick": tick,
                           "reason": error_code(exc), "error": type(exc).__name__, "time": store._now()})
        try:
            alerts.evaluate(s["id"])
        except Exception:
            log.exception("the alerts failed for site %s", s["id"])
        seconds = round(time.monotonic() - t, 3)
        try:
            with store.connect() as con:
                con.execute("UPDATE runs SET seconds = ? WHERE site_id = ? AND tick = ?", (seconds, s["id"], tick))
        except Exception:
            log.exception("the tick time of site %s was not recorded", s["id"])
    return status, seconds


def tick_company(path, tick: str | None = None, workers: int | None = None) -> dict:
    """A company's scheduled tick for the quarter `tick` (ISO UTC; the current one by default): every active site on a pool of `workers` threads
    (tick_workers by default; never more than the sites), each with its own runs row (_site_tick), so one site's failure never stops the others.
    The tick's own row in `ticks` keeps its start, end, length, sites, failures, workers and slowest site, apart from any manual re-solve (round 7,
    performance-live-09), and one log line says the same. Returns {tick, sites (the ticks that did not raise), failed (made no plan), seconds,
    workers}."""
    tick = tick or plans._iso(forecast.align(_now_s()))
    t = time.monotonic()
    with store.use_company(path):
        started = store._now()
        listed = [s for s in sites.listing() if s.get("status") == "active"]
    n = max(1, min(int(workers or tick_workers()), len(listed) or 1))
    if n == 1:
        done = [_site_tick(path, s, tick) for s in listed]
    else:
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="gridlytics-tick") as pool:
            done = list(pool.map(lambda s: _site_tick(path, s, tick), listed))
    seconds = round(time.monotonic() - t, 3)
    out = {"tick": tick, "sites": sum(st != "error" for st, _ in done), "failed": sum(st in plans.FAILING_RUNS for st, _ in done),
           "seconds": seconds, "workers": n}
    slow = max(zip(listed, done), key=lambda x: x[1][1], default=None)
    with store.use_company(path), store.connect() as con:
        con.execute("INSERT OR REPLACE INTO ticks (tick, started, finished, seconds, sites, failed, workers, slowest_site, slowest_s) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (tick, started, store._now(), seconds, len(listed), out["failed"], n,
                                                   slow[0]["id"] if slow else None, slow[1][1] if slow else None))
    log.info("tick %s of %s: %d sites, %d made no plan, %.1f s on %d workers%s", tick, getattr(path, "stem", path), len(listed), out["failed"],
             seconds, n, f", slowest {slow[0]['id']} {slow[1][1]:.1f} s" if slow else "")
    return out


def tick_all(files=None) -> dict:
    """The 15-minute job: every company's tick for the quarter it started in. A job that ends after the next quarter's tick was due ran into it, and
    APScheduler skips that tick (max_instances 1): every active site of every company is then told so by the tick_overrun alert, which the next tick
    that ends in time clears (round 7, performance-live-11). Returns {tick, seconds, overran, companies}."""
    begun = _now_s()
    q = forecast.align(begun)
    tick = plans._iso(q)
    done = []
    for path in (files if files is not None else platformdb.company_files()):
        if path and path.exists():
            done.append(path)
            try:
                tick_company(path, tick)
            except Exception:
                log.exception("scheduler job failed for %s", path)
    ended = _now_s()
    overran = ended > q + 900 + TICK_OFFSET_S
    if overran:
        log.warning("tick %s took %d s and ran into the next quarter's tick, which is skipped", tick, ended - begun)
    for path in done:
        try:
            with store.use_company(path):
                if overran:
                    with store.connect() as con:
                        con.execute("UPDATE ticks SET overran = 1 WHERE tick = ?", (tick,))
                for s in sites.listing():
                    if s.get("status") == "active":
                        alerts.tick_overrun(s["id"], overran, q, ended - begun)
        except Exception:
            log.exception("the tick's overrun check failed for %s", path)
    return {"tick": tick, "seconds": ended - begun, "overran": overran, "companies": len(done)}


def nightly_company(path) -> None:
    """At 00:40 Pakistan time: for each active site, yesterday's Pakistan-time day (a demo site's twin caught up first) aggregated with its peak
    mask, verified, and its nightly alerts; then every site's plan retention (plans.prune)."""
    yesterday = datetime.fromtimestamp(_now_s(), PKT).date() - timedelta(days=1)
    with store.use_company(path):
        for s in sites.listing():
            if s.get("status") != "active":
                continue
            try:
                if s.get("demo_link"):
                    demo.advance(s["id"])
                conn = (sites.connections(s["id"]) or [None])[0]
                from . import twin
                telemetry.aggregate_day(s["id"], yesterday, plans.peak_mask(conn, twin.minutes_of(yesterday)) if conn else None)
                verify.compute_day(s["id"], yesterday)
                alerts.nightly(s["id"], yesterday)
            except Exception:
                log.exception("nightly job failed for site %s", s["id"])
        # plan retention (plan Part 3.3; round 7, performance-live-01): every site's plans past 90 days keep only what the readers read
        for s in sites.listing():
            try:
                plans.prune(s["id"])
            except Exception:
                log.exception("the plan retention failed for site %s", s["id"])


def nightly_tables_company(path) -> None:
    """At 23:05 Pakistan time, after the 23:00 solve: each site's table for the next day (plans.issue_nightly)."""
    with store.use_company(path):
        for s in sites.listing():
            if s.get("status") != "active":
                continue
            try:
                plans.issue_nightly(s["id"])
            except Exception:
                log.exception("the nightly table failed for site %s", s["id"])


def _all(fn) -> None:
    for path in platformdb.company_files():
        if path and path.exists():
            try:
                fn(path)
            except Exception:
                log.exception("scheduler job failed for %s", path)


def start() -> None:
    global _sched
    if os.environ.get("GRIDLYTICS_SCHEDULER") != "1" or _sched is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    _sched = BackgroundScheduler(timezone="Asia/Karachi", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    _sched.add_job(tick_all, "cron", minute="0,15,30,45", second=TICK_OFFSET_S, id="tick")
    _sched.add_job(lambda: _all(nightly_company), "cron", hour=0, minute=40, id="nightly")
    _sched.add_job(lambda: _all(nightly_tables_company), "cron", hour=NIGHTLY_TABLES_AT[0], minute=NIGHTLY_TABLES_AT[1], id="nightly_tables")
    _sched.start()
    log.info("scheduler started")


def stop() -> None:
    global _sched
    if _sched is not None:
        _sched.shutdown(wait=False)
        _sched = None
