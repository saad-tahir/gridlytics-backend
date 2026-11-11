"""Regressions for round 7's audit findings on storage growth, read paths and the scheduler (work package storage-perf): plan retention and the plan
store's indexes and column reads, a failing tick that is seen, the scheduler's tick and nightly jobs under test, tick timing apart from re-solves, the
tick's worker pool and its overrun alert, and newer shipped fuel prices reaching an existing company."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

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
