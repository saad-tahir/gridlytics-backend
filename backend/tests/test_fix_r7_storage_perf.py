"""Regressions for round 7's audit findings on storage growth, read paths and the scheduler (work package storage-perf): plan retention and the plan
store's indexes and column reads, a failing tick that is seen, the scheduler's tick and nightly jobs under test, tick timing apart from re-solves, the
tick's worker pool and its overrun alert, and newer shipped fuel prices reaching an existing company."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import plans, reports, reserve, sites, store, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _site(name: str = "Mill", battery: bool = True) -> str:
    sid = sites.create({"name": name, "disco": "FESCO", "critical_load_kw": [50.0] * 24, "genset_start_control": "none"}, "t")["id"]
    sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 200, "kw_ac": 180}, "t")
    if battery:
        sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    return sid


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


# ------------------------------------------------------------------------------------------------ performance-live-01, tests-and-perf-06

def test_plan_reads_by_belief_time_and_message_and_ack_reads_by_time_use_an_index(company):
    """active_at's query used the (site, generated_at) index and sorted the site's whole history by belief time (USE TEMP B-TREE), about 50 ms a call at
    10,000 plans; plan_messages and acks had no index at all."""
    with store.connect() as con:
        q = con.execute("EXPLAIN QUERY PLAN SELECT id, status, reason, summary, valid_until FROM plans WHERE site_id = ? AND belief_time <= ? "
                        "ORDER BY belief_time DESC, generated_at DESC, rowid DESC LIMIT 200", ("s", "2026")).fetchall()
        detail = " ".join(r["detail"] for r in q)
        assert "plans_belief" in detail and "TEMP B-TREE FOR ORDER BY" not in detail.replace("RIGHT PART OF ORDER BY", ""), detail
        cols = {name: [r["name"] for r in con.execute(f"PRAGMA index_info({name})")] for name in ("plan_messages_site", "acks_site", "plans_belief")}
    assert cols == {"plan_messages_site": ["site_id", "sent_at"], "acks_site": ["site_id", "received_at"], "plans_belief": ["site_id", "belief_time"]}


def _clone(template: dict, sid: str, q: int, status: str = "superseded", reason: str = "", made_after: int = 20, steps: bool = True) -> str:
    """A plan row as a tick at quarter `q` writes it, from a real plan's row (its steps moved to start at q, its table kept). Its second step models an
    outage (grid 0), which reserve.target_at reads as the outage's own step from the plan in force the quarter before."""
    pid = store.new_id()
    shift = q - int(template["steps"][0]["t"])
    moved = [{**s, "t": int(s["t"]) + shift, **({"grid_present": 0.0} if i == 1 else {})} for i, s in enumerate(template["steps"])]
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, reason, solver, solve_seconds, "
                    "objective_rs, config_hash, summary, steps, slots) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, sid, plans._iso(q + made_after), plans._iso(q), plans._iso(q), plans._iso(q + 192 * 900), "a", status, reason, "highs", 0.1,
                     1000.0, "x", json.dumps(template["summary"]), json.dumps(moved) if steps else None,
                     json.dumps(template["slots"]) if steps else None))
    return pid


def _blob_bytes(sid: str) -> int:
    with store.connect() as con:
        return con.execute("SELECT COALESCE(SUM(LENGTH(steps)), 0) + COALESCE(SUM(LENGTH(slots)), 0) FROM plans WHERE site_id = ?", (sid,)).fetchone()[0]


def _step_at(plan: dict | None, ts: int):
    return next((s for s in (plan or {}).get("steps", []) if s["t"] <= ts < s["t"] + 900), None)


def test_a_prune_trims_old_plans_and_every_reader_answers_as_before(company):
    """Spec Part 3.3: plan rows are kept forever, but past 90 days a plan keeps its steps and table only when it is the first of its hour, applied, or
    in force at a step (that step). Every 15-minute plan kept 192 steps and its table for good: about 12.5 MB a site a day. After a prune, every
    reader of old plans (active_at at any instant, the past plan table, the daily sheet's table, the applied and issued plans, the reports' reserve
    check and the savings' control health) answers exactly as before, and the old plans' blobs shrink to a fraction."""
    sid = _site()
    template = plans.get(sid, plans.tick(sid)["plan_id"])
    with store.connect() as con:
        con.execute("DELETE FROM plans WHERE site_id = ?", (sid,))
    d0 = datetime.now(PKT).date() - timedelta(days=120)
    t0 = twin.day_start_utc(d0)
    quarters = [t0 + 900 * k for k in range(2 * 96)]
    ids = {}
    for k, q in enumerate(quarters):
        if k == 30:
            _clone(template, sid, q, status="failed", reason="solve_infeasible", steps=False)          # a failed solve: the plan before stays
            continue
        if k == 21:
            ids["held"] = _clone(template, sid, q, status="held", reason="guard_mdi")                  # a guard hold: the plan before stays
            continue
        ids[k] = _clone(template, sid, q)
        if k == 10:
            ids["resolve"] = _clone(template, sid, q, made_after=400)                                 # a re-solve in the same quarter wins it
    recent = _clone(template, sid, forecast_align(_now() - 10 * 86400), status="active")                   # inside the 90 days: untouched
    with store.connect() as con:
        con.execute("INSERT INTO applied (id, site_id, plan_id, day_local, by, method, created) VALUES (?,?,?,?,?,?,?)",
                    (store.new_id(), sid, ids[45], d0.isoformat(), "t", "vendor_app", store._now()))
        con.execute("INSERT INTO table_issues (id, site_id, day_local, plan_id, issued_at, reason) VALUES (?,?,?,?,?,?)",
                    (store.new_id(), sid, d0.isoformat(), ids[50], store._now(), "first"))
    # an outage the reserve check reads, entered at 40 % against a planned reserve of 20 %
    rows = [{"ts": t, "grid_present": 0 if quarters[60] <= t < quarters[62] else 1, "battery_soc_bms_pct": 40.0, "load_power_w": 50_000}
            for t in range(quarters[56], quarters[66], 60)]
    telemetry.ingest(sid, rows, "edge")
    site = sites.full(sid)

    def readers() -> dict:
        out = {}
        for q in quarters + [quarters[-1] + 900 * k for k in range(1, 8)]:
            for ts in (q, q + 450):
                p = plans.active_at(sid, ts)
                out[f"at:{ts}"] = (p or {}).get("id"), _step_at(p, ts)
            out[f"target:{q}"] = reserve.target_at(sid, q)
        for d in (d0, d0 + timedelta(days=1), d0 + timedelta(days=2)):
            p = plans.active_at(sid, twin.day_start_utc(d))
            out[f"day:{d}"] = (p or {}).get("id"), (p or {}).get("slots")
        out["applied"] = plans.get(sid, ids[45])
        out["issued"] = plans.get(sid, ids[50])
        out["past"] = plans.past_steps(sid, quarters[-1] + 900, 48)
        out["cover"] = reports.outage_cover(site, d0, d0 + timedelta(days=1))
        g = {"ts": twin.minutes_of(d0)}
        out["control"] = verify._control(sid, g, False)
        out["recent"] = plans.get(sid, recent)
        return out

    before, bytes_before = readers(), _blob_bytes(sid)
    assert before["cover"]["with_reserve"] == 1 and before["cover"]["entered_ok"] == 1     # the step the check reads is there before
    assert before[f"at:{quarters[10]}"][0] == ids["resolve"]
    assert before[f"at:{quarters[21]}"][0] == ids[20] and before[f"at:{quarters[30]}"][0] == ids[29]
    # an outage starting as a new plan takes over: the plan of the quarter before modelled it in its next step (the step past its own span)
    assert before[f"target:{quarters[12]}"] == {"reserve_pct": 20.0, "forecastable": True}
    with store.connect() as con:
        rows_before = con.execute("SELECT COUNT(*) FROM plans WHERE site_id = ?", (sid,)).fetchone()[0]

    done = plans.prune(sid)
    after = readers()
    for k in before:
        assert after[k] == before[k], k
    assert done["trimmed"] > 100 and done["kept_whole"] >= 48 + 2      # the first plan of each hour, the applied and the issued plans
    assert _blob_bytes(sid) < 0.35 * bytes_before                          # a quarter of the plans (the first of each hour) stay whole
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM plans WHERE site_id = ?", (sid,)).fetchone()[0] == rows_before   # rows are kept forever
        first_of_hour = con.execute("SELECT steps IS NOT NULL AND slots IS NOT NULL FROM plans WHERE id = ?", (ids[4],)).fetchone()[0]
        trimmed = con.execute("SELECT steps, slots FROM plans WHERE id = ?", (ids[5],)).fetchone()
        held = con.execute("SELECT steps, slots FROM plans WHERE id = ?", (ids["held"],)).fetchone()
        superseded_in_quarter = con.execute("SELECT steps FROM plans WHERE id = ?", (ids[10],)).fetchone()
    assert first_of_hour
    assert trimmed["slots"] is None and [s["t"] for s in json.loads(trimmed["steps"])] == [quarters[5], quarters[6]]   # its own and the next
    assert held["steps"] is None and held["slots"] is None                 # never in force: nothing reads its steps
    assert superseded_in_quarter["steps"] is None                          # the re-solve of its quarter was in force instead
    # the next prune finds nothing left to trim
    assert plans.prune(sid)["trimmed"] == 0


def test_a_plan_in_force_at_a_day_start_through_failed_ticks_keeps_its_table(company):
    """The daily sheet of a past day and the demo's keyed table read the table of the plan in force at the day's start. A plan made just after midnight
    whose next ticks all failed stays in force (up to its valid_until, 48 hours) across the next two day starts, with no other plan made on those
    days: it keeps its table, though it is not the first plan of its hour."""
    sid = _site()
    template = plans.get(sid, plans.tick(sid)["plan_id"])
    with store.connect() as con:
        con.execute("DELETE FROM plans WHERE site_id = ?", (sid,))
    d0 = datetime.now(PKT).date() - timedelta(days=150)
    t0 = twin.day_start_utc(d0)
    _clone(template, sid, t0 + 900)                                                # 00:15 on d0: the first of its hour
    early = _clone(template, sid, t0 + 1800)                                       # 00:30 on d0
    for k in range(1, 8):
        _clone(template, sid, t0 + 1800 + k * 6 * 3600, status="failed", reason="solve_infeasible", steps=False)   # every solve fails
    _clone(template, sid, t0 + 2 * 86400 + 3600, status="active")                 # 01:00 on d0 + 2, still the active plan
    for d in (d0 + timedelta(days=1), d0 + timedelta(days=2)):
        assert plans.active_at(sid, twin.day_start_utc(d))["id"] == early
    sheet = {d: (plans.active_at(sid, twin.day_start_utc(d)) or {}).get("slots") for d in (d0 + timedelta(days=1), d0 + timedelta(days=2))}
    assert all(sheet.values())
    plans.prune(sid)
    assert {d: (plans.active_at(sid, twin.day_start_utc(d)) or {}).get("slots") for d in sheet} == sheet


def test_the_retention_starts_where_the_last_one_stopped(company, monkeypatch):
    """A nightly prune reads the ticks from where the last one stopped (its latest trimmed plan), not the site's whole history every night."""
    sid = _site()
    template = plans.get(sid, plans.tick(sid)["plan_id"])
    with store.connect() as con:
        con.execute("DELETE FROM plans WHERE site_id = ?", (sid,))
    t0 = twin.day_start_utc(datetime.now(PKT).date() - timedelta(days=100))
    for k in range(4 * 96):
        _clone(template, sid, t0 + 900 * k)
    now = t0 + 92 * 86400
    assert plans.prune(sid, now=now)["trimmed"] > 100
    seen = _Statements(monkeypatch)
    again = plans.prune(sid, now=now + 86400)
    assert again["trimmed"] > 50
    light = [s for s in seen.sql if "has_slots" in s]
    assert light and all(plans._iso(t0) not in s for s in light), light   # bound to where the last prune stopped, never the first plan


def forecast_align(ts: int) -> int:
    return int(ts) // 900 * 900


class _Statements:
    """The SQL statements run on the workspace file while it is installed (store._connect wrapped with a trace callback)."""

    def __init__(self, monkeypatch):
        self.sql: list[str] = []
        real = store._connect

        def traced() -> sqlite3.Connection:
            con = real()
            con.set_trace_callback(self.sql.append)
            return con
        monkeypatch.setattr(store, "_connect", traced)

    def plan_reads(self) -> list[str]:
        return [s for s in self.sql if "FROM plans" in s and s.lstrip().upper().startswith("SELECT")]


def test_the_plan_history_and_the_fleet_row_never_read_the_steps_or_the_table_they_drop(company, monkeypatch):
    """plans.history ran SELECT * and dropped 30 plans' steps and tables (about 4 MB) for a list; the fleet row read a whole plan with its table on every
    60-second poll of every site."""
    from gridlytics import routes_sites
    sid = _site()
    plans.tick(sid)
    seen = _Statements(monkeypatch)
    listed = plans.history(sid, 5)
    assert listed and "steps" not in listed[0] and "slots" not in listed[0] and listed[0]["summary"]
    assert seen.plan_reads() and not any("SELECT *" in s or "steps" in s or "slots" in s for s in seen.plan_reads()), seen.plan_reads()
    seen.sql.clear()
    row = routes_sites.fleet_row(sites.get(sid), _now(), {})
    assert row["next_event"] is not None or row["next_event"] is None   # the row is made
    reads = seen.plan_reads()
    assert reads and not any("SELECT *" in s or "slots" in s for s in reads), reads


def test_the_nightly_job_prunes_every_sites_plans(company, monkeypatch):
    """The retention runs from the nightly job, for every site of the company, and one site's failure does not stop the next."""
    from gridlytics import scheduler
    a, b = _site("A"), _site("B")
    done = []

    def prune(site_id, now=None, keep_days=90):
        done.append(site_id)
        if site_id == a:
            raise RuntimeError("boom")
        return {}
    monkeypatch.setattr(plans, "prune", prune)
    for name in ("aggregate_day",):
        monkeypatch.setattr(telemetry, name, lambda *a, **k: {})
    monkeypatch.setattr(verify, "compute_day", lambda *a, **k: None)
    scheduler.nightly_company(company)
    assert sorted(done) == sorted([a, b])


# ------------------------------------------------------------------------------------------------ parity-architecture-06

from test_fix_api import world  # noqa: E402,F401  (world is a fixture: a company file with a planned mill and the four roles' clients)


def _wait(cond, seconds: float = 3.0) -> bool:
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_a_tick_that_raises_is_recorded_alerted_and_named_on_the_plan_answer(world, monkeypatch):
    """A tick that raised (here a tariff with no version in force) was only logged: no runs row, alerts.evaluate skipped, GET /alerts answered [] and
    GET /plan {plan: null, held: null}, so the page promised the first plan within 15 minutes hours after the first failure. Now the tick writes an
    error runs row, the site's alerts are still evaluated and plan_failed is raised at once (a set-up fault, not a new site's noise), GET /plan names
    the failing ticks, and the record goes to GRIDLYTICS_ERROR_WEBHOOK."""
    from gridlytics import alerts, scheduler, tariffs
    with world.use():
        sites.set_status(world.site, "active")

    def broken(site_id, *a, **k):
        raise tariffs.NoTariffVersion("No version of the schedule is in force")
    monkeypatch.setattr(plans, "tick", broken)
    evaluated = []
    real = alerts.evaluate
    monkeypatch.setattr(alerts, "evaluate", lambda sid, *a, **k: (evaluated.append(sid), real(sid, *a, **k))[1])
    posted = []
    monkeypatch.setattr(scheduler, "_post_record", lambda url, record: posted.append((url, record)))
    monkeypatch.setenv("GRIDLYTICS_ERROR_WEBHOOK", "https://errors.example/hook")
    for _ in range(2):
        assert scheduler.tick_company(world.db)["failed"] == 1
    assert evaluated == [world.site, world.site]
    with world.use(), store.connect() as con:
        run = dict(con.execute("SELECT * FROM runs WHERE site_id = ?", (world.site,)).fetchone())
    assert run["status"] == "error" and run["reason"] == "no_tariff_version" and run["error"].startswith("NoTariffVersion") and run["finished"]
    got = world.c["admin"].get(f"/api/alerts?site_id={world.site}").json()
    rows = got if isinstance(got, list) else got.get("alerts", got.get("items", []))
    failed = [a for a in rows if a["rule"] == "plan_failed"]
    assert len(failed) == 1 and "tariff" in failed[0]["title"] and failed[0]["detail"]["reason"] == "no_tariff_version", rows
    answer = world.c["admin"].get(f"/api/sites/{world.site}/plan").json()
    assert answer["plan"] is None and answer["held"] is None
    assert answer["failing"] == {"status": "error", "reason": "no_tariff_version", "since": run["tick"], "at": run["tick"], "ticks": 1}
    assert _wait(lambda: len(posted) == 2)
    url, record = posted[0]
    assert url == "https://errors.example/hook"
    assert record["event"] == "tick_failed" and record["site_id"] == world.site and record["error"] == "NoTariffVersion"
    assert record["reason"] == "no_tariff_version" and record["company"] == world.db.stem and "No version" not in json.dumps(record)


def test_the_plan_answer_names_failing_solves_too_and_nothing_once_a_tick_plans(world, monkeypatch):
    """Ticks whose solve made no plan, with no plan in force, also left the page promising a first plan: GET /plan names them (the solve's code) until
    a tick makes a plan."""
    from gridlytics import scheduler
    with world.use():
        sites.set_status(world.site, "active")
    real = plans.tick
    monkeypatch.setattr(plans, "tick", lambda site_id, *a, **k: {"status": "failed", "reason": "solve_infeasible", "in_force": None})
    scheduler.tick_company(world.db)
    answer = world.c["admin"].get(f"/api/sites/{world.site}/plan").json()
    assert answer["plan"] is None and answer["failing"]["status"] == "failed" and answer["failing"]["reason"] == "solve_infeasible"
    monkeypatch.setattr(plans, "tick", real)
    scheduler.tick_company(world.db)
    answer = world.c["admin"].get(f"/api/sites/{world.site}/plan").json()
    assert answer["plan"] is not None and "failing" not in answer


# ------------------------------------------------------------------------------------------------ performance-live-09, performance-live-11

def _fake_tick(log_: list | None = None, barrier=None):
    """A stand-in for plans.tick that makes no plan row: it records the company file and the thread it ran on, and, given a barrier, waits there
    until as many sites are inside it at once (a sequential tick never gets there)."""
    def tick(site_id, *a, **k):
        if log_ is not None:
            log_.append((site_id, str(store.db_path()), threading.current_thread().name))
        if barrier is not None:
            barrier.wait()
        return {"status": "active", "plan_id": None, "reason": None, "solve_seconds": 0.01, "objective_rs": 1.0, "solver_status": "optimal"}
    return tick


def test_a_re_solve_never_overwrites_the_scheduled_ticks_row_and_each_tick_is_timed_and_logged(company, monkeypatch, caplog):
    """runs had one row per (site, quarter) that the solve route also wrote, so a re-solve in the quarter erased the tick's timing (a 07:45 tick read
    841 s long); no tick or site duration was kept or logged. Now the scheduler alone writes runs, with the site's own seconds, and each tick has its
    row in `ticks` and one log line."""
    import logging
    from gridlytics import scheduler
    a, b = _site("A"), _site("B")
    for sid in (a, b):
        sites.set_status(sid, "active")
    with caplog.at_level(logging.INFO, logger="gridlytics.scheduler"):
        out = scheduler.tick_company(company, workers=2)
    assert out["sites"] == 2 and out["failed"] == 0 and out["workers"] == 2
    with store.connect() as con:
        runs = {r["site_id"]: dict(r) for r in con.execute("SELECT * FROM runs")}
        ticks = [dict(r) for r in con.execute("SELECT * FROM ticks")]
    assert set(runs) == {a, b} and all(r["seconds"] > 0 and r["status"] in ("active", "held") and r["plan_id"] for r in runs.values())
    assert len(ticks) == 1 and ticks[0]["tick"] == out["tick"] and ticks[0]["sites"] == 2 and ticks[0]["failed"] == 0 and ticks[0]["workers"] == 2
    assert ticks[0]["seconds"] >= max(r["seconds"] for r in runs.values()) * 0.5 and ticks[0]["slowest_site"] in (a, b) and not ticks[0]["overran"]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"tick {out['tick']} of ")]
    assert len(lines) == 1 and "2 sites, 0 made no plan" in lines[0] and "on 2 workers" in lines[0], lines
    # a manual re-solve in the same quarter makes its plan, and leaves the tick's row as the tick wrote it
    again = plans.tick(a)
    with store.connect() as con:
        after = dict(con.execute("SELECT * FROM runs WHERE site_id = ?", (a,)).fetchone())
        n = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert again["plan_id"] != runs[a]["plan_id"] and after == runs[a] and n == 2


def test_the_tick_solves_sites_side_by_side_on_the_company_file(company, monkeypatch):
    """The tick ran every site one after another in one thread, so its length grew with the fleet. Now a company's sites run on a pool of workers,
    each on the company's own file: two sites are inside the solve at once (a barrier a sequential tick never passes)."""
    from gridlytics import scheduler
    ids = [_site(f"S{k}", battery=False) for k in range(4)]
    for sid in ids:
        sites.set_status(sid, "active")
    seen = []
    monkeypatch.setattr(plans, "tick", _fake_tick(seen, threading.Barrier(2, timeout=10)))
    out = scheduler.tick_company(company, workers=2)
    assert out["sites"] == 4 and out["failed"] == 0, out
    assert sorted(s for s, _, _ in seen) == sorted(ids)
    assert {f for _, f, _ in seen} == {str(company)} and len({t for _, _, t in seen}) == 2


def test_the_worker_count_is_the_cores_less_one_up_to_four_or_the_setting(monkeypatch):
    from gridlytics import scheduler
    monkeypatch.delenv("GRIDLYTICS_TICK_WORKERS", raising=False)
    for cores, want in ((1, 1), (2, 1), (4, 3), (16, 4), (None, 1)):
        monkeypatch.setattr(scheduler.os, "cpu_count", lambda c=cores: c)
        assert scheduler.tick_workers() == want, cores
    monkeypatch.setattr(scheduler.os, "cpu_count", lambda: 16)
    for asked, want in (("2", 2), ("12", 8), ("0", 4), ("many", 4)):
        monkeypatch.setenv("GRIDLYTICS_TICK_WORKERS", asked)
        assert scheduler.tick_workers() == want, asked


def test_a_tick_that_runs_into_the_next_quarter_raises_an_alert_the_next_tick_in_time_clears(company, monkeypatch):
    """With max_instances 1 and coalesce, a tick still running at the next quarter made APScheduler skip that tick with only a log line: sites were
    planned every 30 minutes and nothing said so. Now the overrun is kept on the tick's row and raises tick_overrun on every active site."""
    from gridlytics import alerts, scheduler
    sid = _site()
    sites.set_status(sid, "active")
    monkeypatch.setattr(plans, "tick", _fake_tick())
    q = forecast_align(_now()) - 3600
    clock = iter([q + 20, q + 20 + 960, q + 920, q + 920 + 40])        # the first tick ends 16 minutes on, the second in 40 seconds
    monkeypatch.setattr(scheduler, "_now_s", lambda: next(clock))
    first = scheduler.tick_all([company])
    assert first["overran"] and first["companies"] == 1
    with store.connect() as con:
        assert con.execute("SELECT overran FROM ticks WHERE tick = ?", (plans._iso(q),)).fetchone()[0] == 1
    open_ = [a for a in alerts.listing("active", sid) if a["rule"] == "tick_overrun"]
    assert len(open_) == 1 and "took 16 minutes" in open_[0]["title"] and "was skipped" in open_[0]["title"] and open_[0]["category"] == "Control"
    second = scheduler.tick_all([company])
    assert not second["overran"]
    assert not [a for a in alerts.listing("active", sid) if a["rule"] == "tick_overrun"]


# ------------------------------------------------------------------------------------------------ follow-up (tariffs review): newer shipped fuel prices

def test_a_newer_builds_fuel_prices_reach_an_existing_company_and_never_displace_its_own(company, monkeypatch):
    """The shipped fuel prices were seeded only into a file with no fuel price, so a later build's OGRA prices never reached an existing company. Now
    each shipped day the file lacks is added when the build starts, unless the price in force on that day is one the company entered: an entered
    price is never changed, nor displaced from the days it prices."""
    from gridlytics import tariff_data, tariffs
    assert tariffs.fuel_price("hsd", "2026-11-06")[0] == 382.25                       # a file seeded by this build
    tariffs.add_fuel_price("hsd", "2026-11-05", 389.0, "owner")                         # the company's own price on a shipped day
    tariffs.add_fuel_price("hsd", "2026-11-08", 386.0, "owner")                         # and after the last shipped one
    tariffs.add_fuel_price("gas_captive", "2026-08-01", 3900.0, "owner")                # its gas contract price
    with store.connect() as con:
        before = {(r["kind"], r["valid_from"]): (r["price"], r["source"]) for r in con.execute("SELECT * FROM fuel_prices")}
    newer = [*tariff_data.FUEL_PRICES, ("hsd", "2026-10-29", 401.10), ("hsd", "2026-11-07", 380.10), ("hsd", "2026-11-10", 377.00),
             ("hsd", "2026-11-05", 999.0), ("gas_captive", "2026-09-01", 4300.0)]
    monkeypatch.setattr(tariff_data, "FUEL_PRICES", newer)
    tariffs._SEEDED.clear()                                                               # a newer build starts
    assert tariffs.fuel_price("hsd", "2026-11-07")[0] == 380.10                         # added: the day before was a shipped price
    assert tariffs.fuel_price("hsd", "2026-10-29")[0] == 401.10                         # added: before the first price
    assert tariffs.fuel_price("hsd", "2026-11-12")[0] == 386.0                          # not added: the company's 8 November price stands
    assert tariffs.fuel_price("gas_captive", "2026-09-15")[0] == 3900.0                 # not added: its contract price stands
    with store.connect() as con:
        after = {(r["kind"], r["valid_from"]): (r["price"], r["source"]) for r in con.execute("SELECT * FROM fuel_prices")}
    assert {k: v for k, v in after.items() if k in before} == before                    # no row the file held is changed
    assert set(after) - set(before) == {("hsd", "2026-10-29"), ("hsd", "2026-11-07")}
    tariffs.ensure_shipped()
    with store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM fuel_prices").fetchone()[0] == len(after)   # a second check adds nothing


def test_the_founder_console_counts_each_companys_sites_whose_ticks_make_no_plan(world, monkeypatch):
    """parity-architecture-06's last part: the founder console counted no failing site, so a company whose every tick raised looked as well as any.
    Now each company's row has `sites_failing`: its active sites whose latest scheduled tick made no plan."""
    from gridlytics import scheduler, store_founder, tariffs
    with world.use():
        sites.set_status(world.site, "active")
    assert store_founder.company_view(world.company["id"])["sites_failing"] == 0

    def broken(site_id, *a, **k):
        raise tariffs.NoTariffVersion("No version of the schedule is in force")
    monkeypatch.setattr(plans, "tick", broken)
    scheduler.tick_company(world.db)
    view = store_founder.company_view(world.company["id"])
    assert view["sites_failing"] == 1 and view["sites"] == 2


# ------------------------------------------------------------------------------------------------ tests-and-perf-08

def _photo_reads(sql: list[str], table: str) -> list[str]:
    """The statements that read a photograph column of `table` (SELECT * or the column itself in the select list), not only its byte length."""
    import re
    reads = [s for s in sql if f"FROM {table}" in s and s.lstrip().upper().startswith("SELECT")]
    return [s for s in reads if "SELECT *" in s or re.search(r"\bphoto\b", re.sub(r"(octet_)?length\(photo\)", "", s.split(" FROM ")[0]))]


def test_the_native_capture_and_the_marked_table_are_read_without_their_photograph(company, monkeypatch):
    """native_latest ran SELECT * and so read the capture's photograph (up to 7 MB) on every sites.full(): every tick and nearly every site page, then
    threw it away; the replay's capture of the day and its applied mark did the same per replayed day. Now the readers name their columns and has_photo
    comes from the photograph's length."""
    sid = _site()
    photo = "data:image/jpeg;base64," + "A" * 1_000_000
    sites.capture_native(sid, {"source": "photo", "work_mode": "self_use", "backup_soc_pct": 20, "photo": photo}, "t")
    day = datetime.now(PKT).date() - timedelta(days=1)
    t0 = twin.day_start_utc(day)
    telemetry.ingest(sid, [{"ts": t, "grid_present": 1, "battery_soc_bms_pct": 50.0, "load_power_w": 50_000, "grid_power_w": 40_000,
                            "pv_power_w": 10_000} for t in range(t0, t0 + 86400, 60)], "edge")
    pid = plans.tick(sid)["plan_id"]
    with store.connect() as con:
        con.execute("INSERT INTO applied (id, site_id, plan_id, day_local, by, method, photo, created) VALUES (?,?,?,?,?,?,?,?)",
                    (store.new_id(), sid, pid, day.isoformat(), "t", "photo", photo, store._now()))
    seen = _Statements(monkeypatch)
    latest, full, of_day = sites.native_latest(sid), sites.full(sid), verify.native_for_day(sid, day)
    for got in (latest, full["native"], of_day):
        assert got["has_photo"] is True and "photo" not in got and got["body"]["backup_soc_pct"] == 20 and got["version"] == 1
        assert set(got) == {"site_id", "version", "captured_at", "by", "source", "body", "has_photo"}
    verify.compute_day(sid, day)
    assert any("FROM native_settings" in s for s in seen.sql) and any("FROM applied" in s for s in seen.sql)
    assert _photo_reads(seen.sql, "native_settings") == [] and _photo_reads(seen.sql, "applied") == []
    sites.capture_native(sid, {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 25}, "t")
    assert sites.native_latest(sid)["has_photo"] is False and sites.native_latest(sid)["version"] == 2


# ------------------------------------------------------------------------------------------------ tests-and-perf-07

def _reference_active_at(sid: str, ts: int) -> dict | None:
    """active_at as it was before round 7's storage-perf (one instant a call): the reference InForce must answer as."""
    def released() -> bool:
        if plans.switchback_release(sid, ts) is not None:
            return True
        for o in sites.overrides(sid, active_only=False):
            if o.get("kind") not in plans.RELEASE_REASONS:
                continue
            start, end, cleared = plans._ts(o.get("from_ts")), plans._ts(o.get("until_ts")), plans._ts(o.get("cleared"))
            if cleared is not None and (end is None or cleared < end):
                end = cleared
            if end is not None and (start is None or start <= ts) and ts < end:
                return True
        return False
    if released():
        return None
    with store.connect() as con:
        rows = con.execute("SELECT id, status, reason, summary, valid_until FROM plans WHERE site_id = ? AND belief_time <= ? "
                           "ORDER BY belief_time DESC, generated_at DESC, rowid DESC LIMIT 200", (sid, plans._iso(ts))).fetchall()
        for r in rows:
            if r["status"] in ("active", "superseded"):
                if plans._past_valid_until(r["valid_until"], ts):
                    return None
                return plans._plan_out(con.execute("SELECT * FROM plans WHERE id = ?", (r["id"],)).fetchone())
            if r["status"] == "held" and r["reason"] in plans.RELEASED_REASONS:
                return None
            if r["status"] == "failed" and (json.loads(r["summary"] or "{}") or {}).get("control_release"):
                return None
        r = con.execute("SELECT * FROM plans WHERE site_id = ? AND status IN ('active','superseded') AND belief_time <= ? ORDER BY belief_time DESC LIMIT 1",
                        (sid, plans._iso(ts))).fetchone()
    return plans._plan_out(r) if r and not plans._past_valid_until(r["valid_until"], ts) else None


def _history(sid: str) -> tuple[int, int, dict]:
    """Two days of ticks with every kind of row that decides (or does not decide) the plan in force: plans made, a re-solve in a quarter, guard
    holds, failed solves, a held tick of a pause, a failed tick under a release, a plan that ran out (valid_until) before the next, a pause in the
    override history and a switchback native day."""
    template = plans.get(sid, plans.tick(sid)["plan_id"])
    with store.connect() as con:
        con.execute("DELETE FROM plans WHERE site_id = ?", (sid,))
    d0 = datetime.now(PKT).date() - timedelta(days=5)
    t0 = twin.day_start_utc(d0)
    ids = {}
    for k in range(2 * 96):
        q = t0 + 900 * k
        if k in (5, 6, 7):
            _clone(template, sid, q, status="held", reason="guard_mdi")
        elif k == 30:
            _clone(template, sid, q, status="failed", reason="solve_infeasible", steps=False)
        elif k == 40:
            _clone(template, sid, q, status="held", reason="control_paused")
        elif k == 50:
            pid = _clone(template, sid, q, status="failed", reason="solve_infeasible", steps=False)
            with store.connect() as con:
                con.execute("UPDATE plans SET summary = ? WHERE id = ?", (json.dumps({"control_release": {"reason": "released_to_native"}}), pid))
        elif 60 <= k < 70:
            continue                                                                    # no tick for 2.5 hours
        else:
            ids[k] = _clone(template, sid, q)
            if k == 12:
                ids["resolve"] = _clone(template, sid, q, made_after=400)
            if k == 59:
                with store.connect() as con:                                            # runs out an hour into the gap
                    con.execute("UPDATE plans SET valid_until = ? WHERE id = ?", (plans._iso(q + 3600), ids[k]))
    with store.connect() as con:
        con.execute("INSERT INTO overrides (id, site_id, kind, value, from_ts, until_ts, author, reason, created, cleared) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (store.new_id(), sid, "pause_control", None, plans._iso(t0 + 80 * 900 + 300), plans._iso(t0 + 90 * 900), "t", "",
                     plans._iso(t0), plans._iso(t0 + 85 * 900 + 120)))
        con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,?)",
                    (sid, (d0 + timedelta(days=1)).isoformat(), (d0 + timedelta(days=1)).isoformat(), "native", 1))
    return t0, t0 + 2 * 86400, ids


def test_the_plan_in_force_over_a_range_is_read_once_and_answers_as_active_at_did(company, monkeypatch):
    """active_at ran once per genset run, peak quarter, outage or past step, each call reading the override history and up to 200 plan rows and
    parsing a 130 KB plan: O(instants x plans). InForce reads the range's rows and the overrides once and parses each plan in force once, and answers
    exactly as active_at did at every instant, whatever decides it."""
    sid = _site()
    t0, t1, ids = _history(sid)
    from gridlytics import verify as verify_
    assert verify_.arm_on(sid, datetime.fromtimestamp(t0, PKT).date() + timedelta(days=1)) == "native"
    instants = list(range(t0 - 1800, t1 + 1800, 450)) + [t0 + 80 * 900 + 299, t0 + 85 * 900 + 119, t0 + 85 * 900 + 120]
    instants += [t0 + 12 * 900 + 10]
    want = {ts: (_reference_active_at(sid, ts) or {}).get("id") for ts in instants}
    assert want[t0 + 12 * 900 + 10] == ids["resolve"] and want[t0 + 6 * 900] == ids[4]          # a re-solve wins its quarter; holds keep the plan
    assert want[t0 + 40 * 900] is None and want[t0 + 50 * 900] is None and want[t0 + 30 * 900] == ids[29]
    assert want[t0 + 62 * 900] == ids[59] and want[t0 + 63 * 900] is None                      # in force to its valid_until, then none
    assert want[t0 + 82 * 900] is None and want[t0 + 86 * 900] == ids[86]                      # the pause, cleared early
    assert want[t1 - 900] is None                                                                # the switchback native day
    seen = _Statements(monkeypatch)
    in_force = plans.InForce(sid, t0 - 1800, t1 + 1800)
    got = {ts: (in_force.at(ts) or {}).get("id") for ts in instants}
    assert got == want
    overrides = [s for s in seen.sql if "FROM overrides" in s]
    whole = [s for s in seen.plan_reads() if "SELECT *" in s]
    assert len(overrides) == 1 and len(whole) == len({v for v in want.values() if v})          # each plan in force parsed once
    assert len([s for s in seen.plan_reads() if "belief_time <=" in s]) == 1
    for ts in instants[::7]:
        assert (plans.active_at(sid, ts) or {}).get("id") == want[ts]                            # active_at is the one-instant InForce
        step = in_force.step(ts)
        ref = _step_at(_reference_active_at(sid, ts), ts)
        assert step == ref


def test_the_past_rows_the_genset_runs_the_outage_reserve_and_the_peak_check_read_the_plans_once(company, monkeypatch):
    """The callers in loops (the plan table's 96 past steps, a genset run each, an outage each, a peak quarter each) read the override history and the
    plans of their range once."""
    from gridlytics import alerts, reserve, routes_telemetry
    sid = _site()
    sites.put_asset(sid, "genset", {"rated_kw": 150}, "t")
    q = forecast_align(_now())
    plans.tick(sid, at=q - 6 * 3600)
    plans.tick(sid, at=q - 3 * 3600)
    rows = []
    for t in range(q - 5 * 3600, q - 60, 60):
        k = (t - (q - 5 * 3600)) // 60
        run = (k // 20) % 3 == 0                                                                   # a 20-minute run each hour
        out = 60 <= k % 120 < 75                                                                   # a 15-minute outage every two hours
        rows.append({"ts": t, "grid_present": 0 if out else 1, "genset_running": 1 if run else 0, "genset_power_w": 90_000 if run else 0,
                     "battery_soc_bms_pct": 40.0, "load_power_w": 80_000, "grid_power_w": 0 if out else 60_000})
    telemetry.ingest(sid, rows, "edge")
    site = sites.full(sid)
    real_connect = store._connect

    def reads(fn) -> tuple[int, int]:
        seen = _Statements(monkeypatch)
        try:
            fn()
        finally:
            monkeypatch.setattr(store, "_connect", real_connect)
        return len([s for s in seen.sql if "FROM overrides" in s]), len([s for s in seen.plan_reads() if "belief_time <=" in s])

    assert reads(lambda: plans.past_steps(sid, q, 24)) == (1, 1)
    log = telemetry.outages(sid, q - 5 * 3600, q)
    assert len(log) >= 2
    assert reads(lambda: reserve.annotate(site, [dict(o) for o in log])) == (1, 1)
    day = datetime.fromtimestamp(q, PKT).date()
    assert reads(lambda: reports.outage_cover(site, day - timedelta(days=1), day)) == (1, 1)
    assert len(routes_telemetry.genset_page(sid, days=1)["runs"]) >= 3
    assert reads(lambda: routes_telemetry.genset_page(sid, days=1)) == (1, 1)
    yesterday = day - timedelta(days=1)
    assert reads(lambda: alerts._peak_unconfirmed(site, twin.day_start_utc(day), yesterday.isoformat())) == (1, 1)


# ------------------------------------------------------------------------------------------------ tests-and-perf-09

class _MinuteReads:
    """The range reads of the minute table while installed (telemetry._open wrapped with a trace callback): each as (days, a measure of its
    columns)."""

    def __init__(self, monkeypatch, sid: str):
        import re
        self.reads: list[tuple[float, int]] = []
        real = telemetry._open
        pattern = re.compile(rf"SELECT (.*?) FROM minute WHERE site_id = '{sid}' AND ts >= (\d+) AND ts < (\d+) ORDER BY ts")

        def traced(statement: str) -> None:
            m = pattern.search(statement)
            if m and m.group(1).startswith("ts"):                  # a read of rows (a read of their text columns alone goes with one)
                self.reads.append(((int(m.group(3)) - int(m.group(2))) / 86400, m.group(1).count(",")))

        def opened():
            con = real()
            con.set_trace_callback(traced)
            return con
        monkeypatch.setattr(telemetry, "_open", opened)

    def days(self) -> float:
        return sum(d for d, _ in self.reads)


def _history_site(days: int = 40) -> tuple[str, int]:
    sid = _site()
    sites.put_asset(sid, "genset", {"rated_kw": 150}, "t")
    sites.set_status(sid, "active")
    now = _now() // 60 * 60
    for k in range(days):
        t0 = now - (days - k) * 86400
        telemetry.ingest(sid, [{"ts": x, "grid_present": 1 if (x // 3600) % 7 else 0, "grid_power_w": 40_000.0, "pv_power_w": 10_000.0,
                                "load_power_w": 50_000.0, "battery_power_w": 0.0, "battery_soc_bms_pct": 50.0 + (x // 3600) % 30,
                                "battery_soh_pct": 99.0, "inverter_mode_active": "self_use"} for x in range(t0, t0 + 86400, 60)], "edge", now=now)
    return sid, now


def test_a_tick_reads_its_sites_minute_rows_once_and_reads_the_same_numbers(company, monkeypatch):
    """Each tick read about four months of 1-minute rows a site, 17 columns, in seven overlapping reads (the forecasters' 14, 21 and 28 days, the
    month to date, the tick's and the alerts' 30-day battery estimates), converted cell by cell in Python. Now the scheduler's tick reads each column of
    the site's window once (the forecasters, the battery and the alerts slice it), and a slice is what the file holds."""
    from gridlytics import battery, scheduler
    sid, now = _history_site()
    seen = _MinuteReads(monkeypatch, sid)
    assert scheduler._site_tick(company, sites.get(sid), "2026-11-11T00:00:00+00:00")[0] == "active"
    long_reads = [r for r in seen.reads if r[0] > 1.5]
    assert len(long_reads) <= 2 and seen.days() < 80, seen.reads             # 10 reads, 190 days before
    # inside the window every read is the file's own answer: the minute grid, a raw read of text and numbers, the battery estimate
    site = sites.full(sid)
    plain = {"grid": telemetry.minute_grid(sid, now - 21 * 86400 + 30, now - 7), "raw": telemetry.raw(sid, now - 86400, now, ("inverter_mode_active",
             "battery_soh_pct", "grid_present")), "battery": battery.state(site, now)}
    with scheduler.history_window(sid, now) as w:
        inside = {"grid": telemetry.minute_grid(sid, now - 21 * 86400 + 30, now - 7), "raw": telemetry.raw(sid, now - 86400, now, (
                  "inverter_mode_active", "battery_soh_pct", "grid_present")), "battery": battery.state(site, now)}
        assert w.reads <= 2
    for part in ("grid", "raw"):
        assert plain[part].keys() == inside[part].keys()
        for k, v in plain[part].items():
            if isinstance(v, np.ndarray):
                assert np.array_equal(v, inside[part][k], equal_nan=True), (part, k)
            else:
                assert v == inside[part][k], (part, k)
    assert plain["battery"] == inside["battery"]
    assert inside["raw"]["inverter_mode_active"][-1] == "self_use" and len(inside["raw"]["ts"]) == 1440
    # another site's read, or a range the window does not hold, goes to the file
    other = _site("Other")
    telemetry.ingest(other, [{"ts": now - 600, "grid_present": 1}], "edge", now=now)
    with scheduler.history_window(sid, now) as w:
        telemetry.raw(sid, now - 3600, now)
        n, held = len(seen.reads), w.reads
        assert len(telemetry.raw(other, now - 3600, now)["ts"]) == 1 and w.reads == held          # not the window's site: the file's answer
        assert len(telemetry.raw(sid, now - 40 * 86400, now)["ts"]) == 40 * 1440 and len(seen.reads) == n + 1   # more than the window holds


# ------------------------------------------------------------------------------------------------ tests-and-perf-10, performance-live-02

def _minutes(sid: str, a: int, b: int, grid_w: float) -> None:
    telemetry.ingest(sid, [{"ts": t, "grid_present": 1, "grid_power_w": grid_w, "grid_import_wh": grid_w / 60, "pv_power_w": 20_000.0,
                            "load_power_w": 60_000.0, "battery_power_w": 0.0, "battery_soc_bms_pct": 60.0, "genset_running": 0}
                           for t in range(a, b, 60)], "edge")


def _pinned_day(world, monkeypatch, hour: int) -> tuple[date, int]:
    """The routes' clock set to `hour` o'clock of the day before yesterday, Pakistan time (so a test's minutes are all in the past)."""
    from gridlytics import routes_sites, routes_telemetry
    day = datetime.now(PKT).date() - timedelta(days=2)
    t0 = twin.day_start_utc(day)
    for module in (routes_sites, routes_telemetry):
        monkeypatch.setattr(module, "today", lambda: day)
        monkeypatch.setattr(module, "now_ts", lambda: t0 + hour * 3600)
    return day, t0


def test_the_overview_s_today_follows_a_live_sites_newest_minutes_with_its_peak_import(world, monkeypatch):
    """GET /now wrote today's day row once, without the peak flags, and nothing refreshed it until the next night: an edge site's 'Grid import today',
    'Solar today', 'Peak demand today' and 'Outage today', the month's sums and the savings page's energy kept the first view's figures. Now the row is
    aggregated again whenever a newer minute has arrived, with the peak import, and read as stored otherwise."""
    day, t0 = _pinned_day(world, monkeypatch, 22)
    a = world.c["admin"]
    with world.use():
        _minutes(world.site, t0 + 8 * 3600, t0 + 9 * 3600, 30_000.0)
    first = a.get(f"/api/sites/{world.site}/now").json()
    assert first["today"]["import_kwh"] == pytest.approx(30.0, abs=0.1) and first["today"]["last_ts"] == t0 + 9 * 3600 - 60
    assert "peak_import_kwh" in first["today"]
    with world.use():
        _minutes(world.site, t0 + 9 * 3600, t0 + 21 * 3600, 60_000.0)          # through the evening peak
    later = a.get(f"/api/sites/{world.site}/now").json()
    assert later["today"]["import_kwh"] == pytest.approx(30.0 + 12 * 60.0, abs=0.5) and later["today"]["last_ts"] == t0 + 21 * 3600 - 60
    assert later["today"]["peak_import_kwh"] > 0 and later["month"]["import_kwh"] == pytest.approx(later["today"]["import_kwh"], abs=0.2)
    with world.use():
        stored = telemetry.days(world.site, day.isoformat(), day.isoformat())[0]
    assert stored["import_kwh"] == later["today"]["import_kwh"] and stored["peak_import_kwh"] == later["today"]["peak_import_kwh"]
    # nothing newer: the stored row is read, not aggregated again
    seen = _MinuteReads(monkeypatch, world.site)
    assert a.get(f"/api/sites/{world.site}/now").json()["today"] == later["today"]
    assert not [r for r in seen.reads if abs(r[0] - 1.0) < 1e-9]                  # no day's aggregation


def test_the_header_reads_no_history_and_the_overview_and_battery_page_share_one_estimate_a_minute(world, monkeypatch):
    """Every site page polled /now every 30 s only for its header (tier, control, last data), and /now re-read 30 days of minutes (battery.state) on
    each call, as did the battery page beside it: about 7 /now a second saturated the API. The header now has its own few indexed reads; the overview
    and the battery page share one battery estimate a minute while no newer reading arrives, and the month's litres likewise."""
    from gridlytics import battery
    day, t0 = _pinned_day(world, monkeypatch, 12)
    with world.use():
        _minutes(world.site, t0 + 6 * 3600, t0 + 12 * 3600, 30_000.0)
    battery._estimates.clear()
    a, v = world.c["admin"], world.c["viewer"]
    seen = _MinuteReads(monkeypatch, world.site)
    h = v.get(f"/api/sites/{world.site}/header")
    assert h.status_code == 200, h.text
    body = h.json()
    assert set(body) == {"site", "control", "last_data"} and body["last_data"] == t0 + 12 * 3600 - 60 and body["control"]["key"]
    assert body["site"] == {"id": world.site, "name": "Mill One", "tier": "monitor", "status": body["site"]["status"], "demo_link": None}
    assert seen.reads == []                                                          # no range read of the minutes at all
    month = lambda: [r for r in seen.reads if abs(r[0] - 30.0) < 1e-6]            # the estimate's 30 days
    assert a.get(f"/api/sites/{world.site}/now").json()["battery"]["soc_pct"] == 60.0
    n = len(month())
    assert n >= 1
    a.get(f"/api/sites/{world.site}/now")
    a.get(f"/api/sites/{world.site}/battery")
    assert len(month()) == n                                                         # the second poll and the battery page reuse it
    with world.use():
        _minutes(world.site, t0 + 12 * 3600, t0 + 12 * 3600 + 60, 30_000.0)        # a newer reading: estimated again
    a.get(f"/api/sites/{world.site}/now")
    assert len(month()) == 2 * n


def test_the_overviews_month_litres_are_reused_for_a_minute_and_read_again_after_it(world, monkeypatch):
    """GET /now replayed the month's genset minutes (verify.litres_used) on every 30-second poll. They are reused for a minute while the newest reading
    and the genset are the same, and read again after it, so minutes uploaded for earlier days (no newer reading) still reach the overview."""
    from gridlytics import routes_sites
    day, t0 = _pinned_day(world, monkeypatch, 12)
    clock = {"now": t0 + 12 * 3600}
    monkeypatch.setattr(routes_sites, "now_ts", lambda: clock["now"])
    with world.use():
        sites.put_asset(world.site, "genset", {"rated_kw": 150}, "t")
        _minutes(world.site, t0 + 6 * 3600, t0 + 12 * 3600, 30_000.0)
    calls = []
    real = verify.litres_used
    monkeypatch.setattr(verify, "litres_used", lambda *x, **k: (calls.append(x), real(*x, **k))[1])
    a = world.c["admin"]
    assert a.get(f"/api/sites/{world.site}/now").status_code == 200 and len(calls) == 1
    clock["now"] += 30
    a.get(f"/api/sites/{world.site}/now")
    assert len(calls) == 1                                                           # the next poll reuses it
    with world.use():                                                                # an earlier hour uploaded: no newer reading
        telemetry.ingest(world.site, [{"ts": t, "grid_present": 1, "genset_running": 1, "genset_power_w": 90_000.0}
                                      for t in range(t0 + 3600, t0 + 2 * 3600, 60)], "csv")
    clock["now"] += 31
    body = a.get(f"/api/sites/{world.site}/now").json()
    assert len(calls) == 2 and body["month"]["litres"] > 0, body["month"]


# ------------------------------------------------------------------------------------------------ performance-live-05

def test_a_re_solve_and_a_resume_run_as_jobs_and_answer_202_when_the_solve_outlasts_the_wait(world, monkeypatch):
    """Re-solve, resume (and every control action's re-solve) ran the solver inside the HTTP request: 3.6 s measured, up to 30 s, past a proxy's read
    timeout while the work went on. Now the solve is a job on the job pool (jobs.py): the request waits SOLVE_WAIT_S for it, answers its outcome if
    it ended, else 202 with the job, whose outcome its starter reads at GET /sites/:id/jobs/:job_id (another person reads a 404)."""
    from gridlytics import routes_plans
    release, threads = threading.Event(), []
    real = plans.tick

    def slow(site_id, *a, **k):
        threads.append(threading.current_thread().name)
        assert release.wait(10)
        return real(site_id, *a, **k)
    monkeypatch.setattr(plans, "tick", slow)
    monkeypatch.setattr(routes_plans, "SOLVE_WAIT_S", 0.2)
    a, sid = world.c["admin"], world.site
    r = a.post(f"/api/sites/{sid}/plan/solve")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] in ("queued", "running") and body["job_id"] and body["plan_id"] is None
    job = f"/api/sites/{sid}/jobs/{body['job_id']}"
    assert a.get(job).json()["status"] in ("queued", "running")
    assert world.c["operator"].get(job).status_code == 404                   # another person's job is not theirs to read
    release.set()
    assert _wait(lambda: a.get(job).json()["status"] not in ("queued", "running"), 30)
    done = a.get(job).json()
    assert done["status"] == "active" and done["plan_id"] and done["solve_seconds"] is not None and done["job_id"] == body["job_id"]
    assert threads and all(t.startswith("gridlytics-job") for t in threads)  # solved on the job pool, not the request's thread
    with world.use():
        assert plans.current(sid)["id"] == done["plan_id"]
        assert any(x["action"] == "re-solved the plan" for x in store.list_audit())
    # a resume clears its override at once, and its re-solve is a job too
    release.clear()
    with world.use():
        sites.add_override(sid, {"kind": "pause_control", "until": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}, "t")
    r = a.post(f"/api/sites/{sid}/control/resume", json={})
    assert r.status_code == 202, r.text
    res = r.json()
    assert res["control"]["key"] != "paused" and res["resolve"]["status"] in ("queued", "running") and res["resolve"]["job_id"]
    release.set()
    job = f"/api/sites/{sid}/jobs/{res['resolve']['job_id']}"
    assert _wait(lambda: a.get(job).json()["status"] not in ("queued", "running"), 30)
    assert a.get(job).json()["status"] == "active"
    # a solve that ends within the wait answers at once, as before
    monkeypatch.setattr(routes_plans, "SOLVE_WAIT_S", 30.0)
    routes_plans._last_solve.clear()
    r = a.post(f"/api/sites/{sid}/plan/solve")
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["plan_id"] and r.json()["job_id"]


# ------------------------------------------------------------------------------------------------ follow-up: reports as jobs (security-roles-08's rest)

def test_a_report_renders_as_a_job_and_the_same_report_asked_again_is_the_one_already_made(world, monkeypatch):
    """Reports rendered in the request, and the same report asked again rendered and wrote its files again (plan Part 10.6: reports run in the job
    pool, 202 and polling, with a request-hash cache). Now POST /reports answers the report when it is made within REPORT_WAIT_S, else 202 with the
    job, read at GET /reports/jobs/:job_id by any member who may read that kind; the same report asked again while nothing it reads has changed is the
    one already made, and a change (here a recompute's audit row) makes a new one."""
    from gridlytics import routes_reports
    release, calls = threading.Event(), []
    real = reports.generate

    def slow(*a, **k):
        calls.append(a)
        assert release.wait(10)
        return real(*a, **k)
    monkeypatch.setattr(reports, "generate", slow)
    monkeypatch.setattr(routes_reports, "REPORT_WAIT_S", 0.2)
    an, v = world.c["analyst"], world.c["viewer"]
    body = {"kind": "monthly_savings", "site_id": world.site, "period": datetime.now(PKT).strftime("%Y-%m")}
    r = an.post("/api/reports", json=body)
    assert r.status_code == 202 and r.json()["status"] in ("queued", "running") and r.json()["job_id"], r.text
    job = f"/api/reports/jobs/{r.json()['job_id']}"
    assert an.get(job).status_code == 202
    release.set()
    assert _wait(lambda: an.get(job).status_code == 200, 30)
    made = an.get(job).json()
    assert made["kind"] == "monthly_savings" and made["period"] == body["period"] and made["id"]
    assert v.get(job).status_code == 404                                          # a kind the viewer may not read: as good as absent
    again = an.post("/api/reports", json=body)
    assert again.status_code == 200 and again.json()["id"] == made["id"] and len(calls) == 1
    with world.use():
        store.audit("t", "recomputed the savings", world.site, {})
    fresh = an.post("/api/reports", json=body)
    assert _wait(lambda: len(calls) == 2, 10)
    assert fresh.status_code in (200, 202) and (fresh.json().get("id") or fresh.json()["job_id"]) != made["id"]
    # a report refused in words is a 422 with its sentence, as it was in the request
    monkeypatch.setattr(routes_reports, "REPORT_WAIT_S", 10.0)

    def refused(*a, **k):
        raise ValueError("The month has no readings to report on")
    monkeypatch.setattr(reports, "generate", refused)
    with world.use():
        store.audit("t", "uploaded a bill", world.site, {})
    r = an.post("/api/reports", json={**body, "kind": "energy_only"})
    assert r.status_code == 422 and r.json()["detail"] == "The month has no readings to report on", r.text
    # any other failure is the server's, in a sentence that names no exception

    def broken(*a, **k):
        raise RuntimeError("disk full at /srv/reports")
    monkeypatch.setattr(reports, "generate", broken)
    r = an.post("/api/reports", json={**body, "kind": "energy_only"})
    assert r.status_code == 500 and r.json()["code"] == "report_failed" and "disk" not in r.text, r.text


# ------------------------------------------------------------------------------------------------ follow-up: GET /plan/compare (security-roles-08's rest)

def test_the_compare_replays_a_plan_against_its_capture_once(world, monkeypatch):
    """GET /plan/compare ran two 2,880-minute twin simulations a call, with no cache, and any member could loop it. The same plan against the same
    capture (and plant, and price) is now replayed once; a new plan or a new capture is replayed afresh; a viewer still reads it without rupees."""
    from gridlytics import routes_plans
    a, v, sid = world.c["admin"], world.c["viewer"], world.site
    with world.use():
        sites.capture_native(sid, {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 20}, "t")
        now = _now() // 60 * 60
        telemetry.ingest(sid, [{"ts": t, "grid_present": 1, "load_power_w": 60_000.0, "battery_soc_bms_pct": 50.0}
                               for t in range(now - 2 * 86400, now, 600)], "edge", now=now)
        plans.tick(sid)
    replays = []
    real = twin.simulate
    monkeypatch.setattr(twin, "simulate", lambda *x, **k: (replays.append(1), real(*x, **k))[1])

    def compare(client) -> tuple[dict, int]:
        n = len(replays)
        body = client.get(f"/api/sites/{sid}/plan/compare").json()
        return body, len(replays) - n

    first, n = compare(a)
    assert first["compare"] and n == 2
    assert compare(a) == (first, 0)
    seen, n = compare(v)
    assert n == 0 and "energy_rs" not in json.dumps(seen) and seen["compare"]["plan"]["import_kwh"] == first["compare"]["plan"]["import_kwh"]
    with world.use():
        sites.capture_native(sid, {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 30}, "t")
    body, n = compare(a)
    assert body["native"]["version"] == 2 and n == 2
    with world.use():
        plans.tick(sid)
    assert compare(a)[1] == 2
    # the grid connection is the plant's too (its sanctioned load and export cap): a change of it is replayed afresh
    with world.use():
        conn = sites.connections(sid)[0]
        sites.put_connection(sid, {**conn["data"], "sanctioned_load_kw": float(conn["data"].get("sanctioned_load_kw") or 100) + 50}, "t", conn["id"])
    assert compare(a)[1] == 2
    # two members asking at once while it is not yet made: one pair of replays, both answered from it
    with world.use():
        plans.tick(sid)
    gate, slow = threading.Event(), twin.simulate
    monkeypatch.setattr(twin, "simulate", lambda *x, **k: (gate.wait(5), slow(*x, **k))[1])
    n = len(replays)
    answers: list = []
    both = [threading.Thread(target=lambda c=c: answers.append(c.get(f"/api/sites/{sid}/plan/compare").json())) for c in (a, v)]
    for t in both:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in both:
        t.join(30)
    assert len(answers) == 2 and all(x["compare"] for x in answers) and len(replays) - n == 2


# ------------------------------------------------------------------------------------------------ follow-up: plans.write_log's day of events

def test_the_write_log_reads_the_days_events_through_the_index_and_finds_every_one(company, monkeypatch):
    """plans.write_log read every write-log event of the site and parsed each time in Python to keep one day's: the whole history on every read of
    the log. Now the (site_id, ts) index reads the dates a day either side, and the day's events are the same ones, whatever offset their time has."""
    sid = _site()
    day = datetime.now(PKT).date() - timedelta(days=3)
    t0 = twin.day_start_utc(day)
    kind = plans.WRITE_LOG_EVENTS[0]
    iso = lambda t, tz=timezone.utc: datetime.fromtimestamp(t, tz).isoformat(timespec="seconds")   # noqa: E731
    rows = [(sid, iso(t0 - 86400 * k + 3600), kind, "edge", "{}") for k in range(1, 400)]           # the site's history: other days
    inside = [iso(t0 + 60), iso(t0 + 3600, PKT), iso(t0 + 86400 - 60).replace("+00:00", "Z"), iso(t0 + 7200, timezone(timedelta(hours=-11)))]
    edges = [iso(t0 - 60), iso(t0 + 86400), iso(t0 - 30, timezone(timedelta(hours=14)))]             # just outside the Karachi day
    rows += [(sid, t, kind, "edge", "{}") for t in inside + edges]
    with store.connect() as con:
        con.executemany("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)", rows)
    seen = _Statements(monkeypatch)
    log = [r for r in plans.write_log(sid, day) if r["kind"] == "event"]
    assert sorted(r["at"] for r in log) == sorted(plans._iso_utc(plans._ts(t)) for t in inside)
    reads = [s for s in seen.sql if "FROM events" in s]
    assert reads and any("ts >= " in s for s in reads), reads


# ------------------------------------------------------------------------------------------------ follow-up: fleet_row's month saving and gensets

def test_the_fleet_reads_a_sites_month_saving_once_in_a_while_and_every_change_reads_it_again(world, monkeypatch):
    """The fleet's 60-second poll ran verify.summary (the month's savings ledger) and read the assets for every site, every poll, as the overview
    did every 30 seconds. The month's saving is now reused for a few minutes while no savings day was computed again and nothing was changed;
    a change (an audited action) reads it again, and the fleet reads the gensets once."""
    summaries = []
    real = verify.summary
    monkeypatch.setattr(verify, "summary", lambda *x, **k: (summaries.append(x[0]), real(*x, **k))[1])
    a = world.c["admin"]
    assert a.get("/api/fleet").status_code == 200
    n = len(summaries)
    assert n == 2                                                                  # the company's two sites
    a.get("/api/fleet")
    a.get(f"/api/sites/{world.site}/now")
    assert len(summaries) == n
    with world.use():
        store.audit("t", "uploaded a bill", world.site, {})
    a.get("/api/fleet")
    assert len(summaries) == 2 * n
    seen = _Statements(monkeypatch)
    rows = a.get("/api/fleet").json()["sites"]
    assert {r["id"]: r["has_genset"] for r in rows} == {world.site: True, world.other: False}
    assert len([s for s in seen.sql if "FROM assets" in s]) == 1, [s for s in seen.sql if "FROM assets" in s]


# ------------------------------------------------------------------------------------------------ performance-live-10 (tests-and-perf-22)

def _raw(client, url: str, encoding: str) -> tuple[dict, bytes]:
    """The answer's headers and its bytes as sent (before the client unpacks them)."""
    with client.stream("GET", url, headers={"Accept-Encoding": encoding}) as r:
        assert r.status_code == 200, url
        return dict(r.headers), b"".join(r.iter_raw())


def test_an_answer_over_a_kilobyte_is_gzipped_for_a_client_that_takes_it_and_reads_the_same(world):
    """No answer was compressed: the plan page fetched about 170 KB of JSON that gzip makes about 14 KB, and the overview's chart came again every
    minute. An answer of over 1,000 bytes to a client that says it takes gzip is sent gzipped (Vary: Accept-Encoding), and unpacks to the very bytes
    sent to a client that does not; a viewer's is the money-filtered answer, compressed after the filter; a short answer is sent as it is."""
    import gzip
    for role, url in (("admin", f"/api/sites/{world.site}"), ("viewer", f"/api/sites/{world.site}"), ("admin", "/api/meta")):
        c = world.c[role]
        packed_headers, packed = _raw(c, url, "gzip, deflate, br")
        plain_headers, plain = _raw(c, url, "identity")
        assert len(plain) > 1000, (url, len(plain))
        assert packed_headers.get("content-encoding") == "gzip" and "accept-encoding" in packed_headers.get("vary", "").lower(), packed_headers
        assert "content-encoding" not in plain_headers and int(plain_headers["content-length"]) == len(plain)
        assert gzip.decompress(packed) == plain and len(packed) < len(plain) / 2, (url, len(packed), len(plain))
        assert int(packed_headers["content-length"]) == len(packed)            # handed to gzip whole, so it keeps its length
        assert packed_headers["x-request-id"] and packed_headers["content-type"].startswith("application/json")
    viewer = json.loads(_raw(world.c["viewer"], f"/api/sites/{world.site}", "identity")[1])
    admin = json.loads(_raw(world.c["admin"], f"/api/sites/{world.site}", "identity")[1])
    assert viewer != admin                                                  # the viewer's is the filtered one, gzipped or not
    headers, body = _raw(world.c["admin"], "/api/health", "gzip")
    assert len(body) < 1000 and "content-encoding" not in headers           # a short answer is not worth packing
    with world.use():
        _minutes(world.site, _now() // 60 * 60 - 3 * 3600, _now() // 60 * 60, 30_000.0)
    headers, body = _raw(world.c["admin"], f"/api/sites/{world.site}/telemetry/export.csv", "gzip")
    assert headers["content-type"].startswith("text/csv") and headers.get("content-encoding") == "gzip"     # the CSV export too
    assert gzip.decompress(body).count(b"\n") > 170


# ------------------------------------------------------------------------------------------------ performance-live-08

def test_the_site_list_carries_each_sites_last_data_from_one_read_of_the_telemetry_file(world, monkeypatch):
    """The app shell polled the whole fleet answer every minute on every page (about 4 ms a site: the plan, the month's saving, the control state of each)
    only for the site switcher's names, state dots and last-data ages. The site list now carries each site's last_data, the newest minute's time or
    null, from one connection to the telemetry file whatever the number of sites, and works out no fleet row."""
    from gridlytics import routes_sites
    end = _now() // 60 * 60
    with world.use():
        more = [sites.create({"name": f"Extra {i}"}, "t")["id"] for i in range(3)]
        _minutes(world.site, end - 600, end, 30_000.0)
        _minutes(more[0], end - 7200, end - 3600, 10_000.0)
    opened: list[int] = []
    real = telemetry._open
    monkeypatch.setattr(telemetry, "_open", lambda: (opened.append(1), real())[1])
    real_row = routes_sites.fleet_row
    monkeypatch.setattr(routes_sites, "fleet_row", lambda *a, **k: pytest.fail("the site list works out no fleet row"))
    for role in ("viewer", "admin"):
        rows = world.c[role].get("/api/sites").json()
        assert {r["id"]: r["last_data"] for r in rows} == {world.site: end - 60, world.other: None, more[0]: end - 3660, more[1]: None, more[2]: None}
    assert len(opened) == 2                                                      # one connection a request, for five sites
    monkeypatch.setattr(routes_sites, "fleet_row", real_row)
    fleet = world.c["viewer"].get("/api/fleet").json()["sites"]                 # the fleet answer says the same
    assert {r["id"]: r["last_data"] for r in fleet} == {r["id"]: r["last_data"] for r in rows}


def test_a_connection_to_a_workspace_file_this_process_made_ready_runs_no_pragma(company, monkeypatch):
    """Every connection to the workspace file ran PRAGMA journal_mode and PRAGMA user_version first (store._connect): a fleet answer opened about 8
    connections a site, and the 148 of 18 sites took 29 of its 63 ms in the profile. A file this process has seen in WAL mode at the current schema is
    opened without them; a file not seen yet is still set up, and a restore (which may put an older file in its place) forgets what was seen."""
    from gridlytics import backups
    with store.connect() as con:
        con.execute("SELECT COUNT(*) FROM sites").fetchone()
    statements: list[str] = []
    real = sqlite3.connect

    def traced(*a, **k):
        con = real(*a, **k)
        con.set_trace_callback(statements.append)
        return con
    monkeypatch.setattr(sqlite3, "connect", traced)
    for _ in range(3):
        with store.connect() as con:
            con.execute("SELECT COUNT(*) FROM sites").fetchone()
    assert len([s for s in statements if "FROM sites" in s]) == 3
    assert not [s for s in statements if "PRAGMA" in s.upper()], statements
    other = company.parent / "d.db"                                              # a new file is made ready as before
    with store.use_company(other):
        with store.connect() as con:
            assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
            assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    sites.create({"name": "Kept"}, "t")
    copy = store.backup("manual")
    statements.clear()
    backups.restore(copy.name)
    with store.connect() as con:
        con.execute("SELECT COUNT(*) FROM sites").fetchone()
    assert any("PRAGMA USER_VERSION" in s.upper() for s in statements)          # checked again after the restore


# ------------------------------------------------------------------------------------------------ tests-and-perf-20 (the product half)

def test_the_plan_answer_reads_why_no_plan_is_in_force_from_held_info_alone(world, monkeypatch):
    """routes_plans._held wrapped plans.held_info in a broad except, asked it again without the time on a TypeError and otherwise derived the answer
    itself, a fallback from before held_info existed: a fault in held_info was logged and hidden behind an answer of _held's own (round 7, the
    product half of audit tests-and-perf-20, whose test half removed the tests that forced the fallback). GET /plan now answers held_info's reading,
    asked once with the request's time, and a fault in it is the server's error."""
    from gridlytics import routes_plans
    calls = []

    def held(site_id, now=None):
        calls.append((site_id, now))
        return {"reason": "control_paused", "until": "u", "plan_id": "p", "kind": "pause_control"}
    monkeypatch.setattr(plans, "held_info", held)
    monkeypatch.setattr(routes_plans, "now_ts", lambda: 1_800_000_000)
    v = world.c["viewer"]
    assert v.get(f"/api/sites/{world.site}/plan").json() == {"plan": None, "held": {"reason": "control_paused", "until": "u", "plan_id": "p"}}
    assert calls == [(world.site, 1_800_000_000)]

    def broken(site_id, now=None):
        raise RuntimeError("a fault in held_info")
    monkeypatch.setattr(plans, "held_info", broken)
    assert v.get(f"/api/sites/{world.site}/plan").status_code == 500


# ------------------------------------------------------------------------------------------------ performance-live-08 (one connection a request)

def test_the_fleet_answer_reads_the_company_file_through_one_connection_whatever_its_sites(world, monkeypatch):
    """GET /fleet opened about 8 connections to the company's file a site (each loading the schema again: about 0.3 ms), so its cost grew with the fleet
    beyond the reads themselves. The fleet's reads now share one connection for the request (store.shared): adding sites adds no connection, every
    connect() block still commits at its end, and a block that raises rolls back what it wrote; outside the request each block has its own as before."""
    from gridlytics import routes_sites
    opened = []
    real = store._connect
    monkeypatch.setattr(store, "_connect", lambda: (opened.append(threading.current_thread().name), real())[1])
    a = world.c["admin"]

    def fleet() -> tuple[int, dict]:
        routes_sites._mtd.clear()                                                  # the same work on every call: no reused saving
        opened.clear()
        r = a.get("/api/fleet")
        assert r.status_code == 200, r.text
        return len(opened), r.json()
    few, before = fleet()
    with world.use():
        for k in range(4):
            _site(f"Extra {k}")
    many, after = fleet()
    assert len(after["sites"]) == len(before["sites"]) + 4
    assert many <= few <= 3, (few, many)                                         # 30 and 80 before (a once-a-process check may add one)
    assert {r["id"]: r for r in after["sites"] if r["id"] in {s["id"] for s in before["sites"]}} == {r["id"]: r for r in before["sites"]}
    # inside a shared block: one connection, each block committed at its end, a block that raised rolled back, and a block of another thread its own
    with world.use():
        opened.clear()
        with store.shared():
            with store.connect() as con:
                con.execute("INSERT INTO audit (org_id, at, actor, action) VALUES ('default', 'x', 't', 'kept')")
            with pytest.raises(RuntimeError):
                with store.connect() as con:
                    con.execute("INSERT INTO audit (org_id, at, actor, action) VALUES ('default', 'x', 't', 'rolled back')")
                    raise RuntimeError
            done = []
            t = threading.Thread(target=lambda: done.append(store.list_audit(5)))
            t.start()
            t.join()
            with store.connect() as con:
                actions = [r[0] for r in con.execute("SELECT action FROM audit WHERE actor = 't' AND at = 'x'")]
        assert actions == ["kept"] and done and len(opened) == 2                  # the block's one, and the other thread's own
        opened.clear()
        with store.connect() as con, store.connect() as again:
            assert con is not again                                                # outside a shared block, a connection a block
