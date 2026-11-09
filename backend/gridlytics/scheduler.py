"""The scheduler (plan Part 2.4 and 10.6): the 15-minute tick and the nightly jobs, for every company Gridlytics is on for.

Off unless GRIDLYTICS_SCHEDULER=1 (the launcher sets it), so tests and a second worker never tick twice. One APScheduler BackgroundScheduler in the API
process for v1; at about 90 sites per core the solves move to workers behind the same `tick_company` function.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from . import alerts, demo, plans, platform as platformdb, sites, store, telemetry, verify

log = logging.getLogger("gridlytics.scheduler")
PKT = timezone(timedelta(hours=5))
_sched = None
#: the nightly table job (plan Part 7.1 step 7): after the 23:00 solve, the next day's Mode A table is issued to the inverter (plans.issue_nightly)
NIGHTLY_TABLES_AT = (23, 5)


def _after_tick(site_id: str) -> None:
    """What follows a site's solve: today's table issued or re-issued when worth a write (plans.issue_tables). A failure is logged, never the tick's."""
    try:
        plans.issue_tables(site_id)
    except Exception:
        log.exception("issuing the table failed for site %s", site_id)


def tick_company(path) -> dict:
    out = {"sites": 0, "failed": 0}
    with store.use_company(path):
        for s in sites.listing():
            if s.get("status") not in ("active",):
                continue
            try:
                if s.get("demo_link"):
                    demo.advance(s["id"])
                r = plans.tick(s["id"])
                out["sites"] += 1
                out["failed"] += int(r["status"] == "failed")
                _after_tick(s["id"])
                alerts.evaluate(s["id"])
            except Exception:
                log.exception("tick failed for site %s", s["id"])
                out["failed"] += 1
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
