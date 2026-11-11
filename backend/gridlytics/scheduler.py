"""The scheduler (plan Part 2.4 and 10.6): the 15-minute tick and the nightly jobs, for every company Gridlytics is on for.

Off unless GRIDLYTICS_SCHEDULER=1 (the launcher sets it), so tests and a second worker never tick twice. One APScheduler BackgroundScheduler in the API
process for v1; at about 90 sites per core the solves move to workers behind the same `tick_company` function.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from . import alerts, demo, forecast, plans, platform as platformdb, sites, store, tariffs, telemetry, verify

log = logging.getLogger("gridlytics.scheduler")
PKT = timezone(timedelta(hours=5))
_sched = None
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


def _site_tick(path, s: dict, tick: str) -> str:
    """One site's part of a scheduled tick: a demo site's twin catches up to now, the solve (plans.tick), its runs row, today's table and the early
    check (_after_tick), then the site's alerts. A tick that raises writes an `error` runs row with its code and the error's type and words, is
    posted to the error webhook, and the alerts are still evaluated, so plan_failed says so (round 7, parity-architecture-06). Returns the runs
    status."""
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
    return status


def tick_company(path) -> dict:
    tick = plans._iso(forecast.align(int(datetime.now(timezone.utc).timestamp())))
    out = {"sites": 0, "failed": 0}
    with store.use_company(path):
        for s in sites.listing():
            if s.get("status") not in ("active",):
                continue
            status = _site_tick(path, s, tick)
            out["sites"] += int(status != "error")
            out["failed"] += int(status in plans.FAILING_RUNS)
    return out


def nightly_company(path) -> None:
    yesterday = datetime.now(PKT).date() - timedelta(days=1)
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
    _sched.add_job(lambda: _all(tick_company), "cron", minute="0,15,30,45", second=20, id="tick")
    _sched.add_job(lambda: _all(nightly_company), "cron", hour=0, minute=40, id="nightly")
    _sched.add_job(lambda: _all(nightly_tables_company), "cron", hour=NIGHTLY_TABLES_AT[0], minute=NIGHTLY_TABLES_AT[1], id="nightly_tables")
    _sched.start()
    log.info("scheduler started")


def stop() -> None:
    global _sched
    if _sched is not None:
        _sched.shutdown(wait=False)
        _sched = None
