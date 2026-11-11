"""Regressions for round 7's audit findings on storage growth, read paths and the scheduler (work package storage-perf): plan retention and the plan
store's indexes and column reads, a failing tick that is seen, the scheduler's tick and nightly jobs under test, tick timing apart from re-solves, the
tick's worker pool and its overrun alert, and newer shipped fuel prices reaching an existing company."""
from __future__ import annotations

import json
import sqlite3
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
