"""The scheduler's 15-minute tick and nightly job, run as the scheduler runs them (round 7, audit tests-and-perf-13: nothing executed tick_company or
nightly_company, so the per-file company, the per-site isolation and the order of the nightly steps had no test). Two company files with a few sites
each, one of whose ticks raises: every row lands in its own company's file, the failing site leaves an error runs row and stops no other, a demo
site's twin advances before its tick, and the nightly job scores yesterday as a Pakistan-time day with the site's peak mask."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from test_fix_savings import make_site, twin_day

from gridlytics import demo, plans, scheduler, sites, store, tariffs, telemetry, twin

PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def two(tmp_path, monkeypatch):
    """Two company files, north and south: north has a mill, a farm with no battery and a demo site; south a mill and a site whose tick raises."""
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    (tmp_path / "companies").mkdir()
    files, ids = {}, {}
    plan = {"north": [("North mill", {}), ("North farm", {"battery": False}), ("North demo", {"demo_link": "demo-north"})],
            "south": [("South mill", {}), ("South broken", {})]}
    for name, made in plan.items():
        files[name] = tmp_path / "companies" / f"{name}.db"
        with store.use_company(files[name]):
            ids[name] = [make_site(name=site, tier="advisory_writes", genset=False, **extra) for site, extra in made]
    return files, ids


def _rows(path, sql: str, *args) -> list[tuple]:
    with closing(sqlite3.connect(path)) as con:
        return con.execute(sql, args).fetchall()


def test_the_tick_and_the_nightly_job_run_every_company_on_its_own_file(two, monkeypatch):
    files, ids = two
    broken = ids["south"][1]
    demo_site = ids["north"][2]
    order: list[tuple[str, str]] = []
    real_tick = plans.tick

    def tick(site_id, *a, **k):
        order.append(("tick", site_id))
        if site_id == broken:
            raise tariffs.NoTariffVersion("No version of the schedule is in force")
        return real_tick(site_id, *a, **k)
    monkeypatch.setattr(plans, "tick", tick)
    monkeypatch.setattr(demo, "advance", lambda site_id, *a, **k: order.append(("advance", site_id)))

    yesterday = datetime.now(PKT).date() - timedelta(days=1)
    for name in ("north", "south"):
        with store.use_company(files[name]):
            twin_day(ids[name][0], yesterday, 0.6)          # a day of telemetry for each company's mill to score

    done = scheduler.tick_all([files["north"], files["south"]])
    assert done["companies"] == 2 and not done["overran"]
    tick_iso = done["tick"]
    for name in ("north", "south"):
        own = set(ids[name])
        other = set(ids["south" if name == "north" else "north"])
        made = {r[0] for r in _rows(files[name], "SELECT site_id FROM plans WHERE belief_time >= ?", tick_iso)}
        runs = {r[0]: r[1] for r in _rows(files[name], "SELECT site_id, status FROM runs WHERE tick = ?", tick_iso)}
        assert set(runs) == own and not made & other and not set(runs) & other
        assert _rows(files[name], "SELECT sites, failed FROM ticks WHERE tick = ?", tick_iso) == [(len(own), int(name == "south"))]
        for sid in own - {broken}:
            assert runs[sid] in ("active", "held") and sid in made, (name, sid, runs[sid])
    # the failing site: an error runs row, no plan, and the other site of its company still planned
    assert _rows(files["south"], "SELECT status, reason FROM runs WHERE site_id = ?", broken) == [("error", "no_tariff_version")]
    assert broken not in {r[0] for r in _rows(files["south"], "SELECT site_id FROM plans WHERE belief_time >= ?", tick_iso)}
    # a demo site's twin catches up to now before its tick, and no other site's is advanced
    assert order.index(("advance", demo_site)) < order.index(("tick", demo_site))
    assert [s for kind, s in order if kind == "advance"] == [demo_site]

    # the nightly job at 00:40 Pakistan time, which is 19:40 UTC the day before: yesterday is the Pakistan-time day
    at = twin.day_start_utc(yesterday + timedelta(days=1)) + 40 * 60
    assert datetime.fromtimestamp(at, timezone.utc).date() == yesterday
    monkeypatch.setattr(scheduler, "_now_s", lambda: at)
    aggregated: list[tuple] = []
    real_aggregate = telemetry.aggregate_day

    def aggregate(site_id, day, mask=None, *a, **k):
        aggregated.append((str(store.db_path()), site_id, day, mask))
        return real_aggregate(site_id, day, mask, *a, **k)
    monkeypatch.setattr(telemetry, "aggregate_day", aggregate)
    order.clear()
    for name in ("north", "south"):
        scheduler.nightly_company(files[name])
    assert sorted(s for _, s, _, _ in aggregated) == sorted(ids["north"] + ids["south"])
    for path, sid, day, mask in aggregated:
        name = "north" if sid in ids["north"] else "south"
        assert path == str(files[name]) and day == yesterday
        with store.use_company(files[name]):
            conn = sites.connections(sid)[0]
        expected = plans.peak_mask(conn, twin.minutes_of(yesterday))
        assert mask is not None and np.asarray(mask).any() and np.array_equal(np.asarray(mask), np.asarray(expected)), sid
    assert [s for kind, s in order if kind == "advance"] == [demo_site]        # the demo's twin catches up before its day is scored
    for name in ("north", "south"):
        scored = {r[0] for r in _rows(files[name], "SELECT site_id FROM savings_days WHERE day_local = ?", yesterday.isoformat())}
        assert ids[name][0] in scored and scored <= set(ids[name]), (name, scored)
