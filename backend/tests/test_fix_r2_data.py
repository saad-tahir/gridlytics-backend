"""Round-2 regression tests of the data group (telemetry.py, forecast.py, battery.py, sites.py): the load censoring over known samples, the battery
trust gap over measured estimates only, old non-finite day rows healed on read, out-of-range edge times skipped, the CSV import's own date window and
span cap, and the connection's reading day.

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own."""
from __future__ import annotations

import csv
import io
import json
import math
import secrets
import types
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gridlytics import api, battery, forecast, reports, sites, store, tariffs, telemetry
from gridlytics import platform as platformdb
from gridlytics.schemas import Refusal

PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A company with one site, an administrator and an operator signed in, and a way to make an edge key (as tests/test_fix_api.py)."""
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    import identities
    co = identities.company("R2 data company", own_file=True)
    db = platformdb.company_file(co)
    db.parent.mkdir(parents=True, exist_ok=True)
    with store.use_company(db):
        one = sites.create({"name": "Mill One", "disco": "FESCO"}, "test")["id"]
        sites.put_connection(one, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "test")
    clients = {"admin": identities.client("admin@r2data.test", None, co, "owner", raise_server_exceptions=False),
               "operator": identities.client("operator@r2data.test", "operator", co, "member", raise_server_exceptions=False)}
    w = types.SimpleNamespace(db=db, site=one, c=clients)

    def use():
        return store.use_company(db)

    def key(scopes):
        r = clients["admin"].post("/api/platform/keys", json={"name": f"k{secrets.token_hex(3)}", "role": "admin", "scopes": scopes, "site_id": one,
                                                               "kind": "edge"})
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["token"]}

    w.use, w.key = use, key
    w.anon = TestClient(api.app, raise_server_exceptions=False)
    return w


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode()


def _imports(site_id: str) -> int:
    with store.connect() as con:
        return con.execute("SELECT COUNT(*) FROM imports WHERE site_id = ?", (site_id,)).fetchone()[0]


def _day_rows(site_id: str) -> dict:
    with store.connect() as con:
        return {r["day_local"]: r["data"] for r in con.execute("SELECT day_local, data FROM telemetry_days WHERE site_id = ?", (site_id,)).fetchall()}


# ------------------------------------------------------------------------------------------------ data-load-censored-medians

@pytest.mark.parametrize("step,variant", [(1, "lost_minute"), (1, "flicker"), (2, None), (5, None)])
def test_the_load_forecast_censors_shed_outages_across_lost_rows_flickers_and_coarse_sources(company, step, variant):
    """The reviewer's 21-day case (100 kW demand; two days in three, shedding 14:00-16:00 with only 30 kW of backed-up load lit), with one lost minute
    or a one-minute grid flicker at 15:00 inside each outage, or rows every 2 or 5 minutes: the medians stay at the demand."""
    sid = f"load-{step}-{variant}"
    site = {"id": sid, "data": {"load_peak_kw": 100.0}, "assets": {}, "connections": []}
    t0 = int(datetime(2026, 11, 23, tzinfo=PKT).timestamp())  # a Monday
    h0 = t0 - 21 * 86400
    rows = []
    for i in range(0, 21 * 1440, step):
        day, minute = i // 1440, i % 1440
        shed = day % 3 != 0 and 14 * 60 <= minute < 16 * 60
        grid = 0.0 if shed else 1.0
        if shed and minute == 15 * 60:
            if variant == "lost_minute":
                continue                              # a dropped batch
            if variant == "flicker":
                grid = 1.0                            # the grid back for one minute; the loads have not restarted
        rows.append((sid, h0 + 60 * i, "edge", grid, 30000.0 if shed else 100000.0))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "load_power_w"))
    q50 = forecast.load(site, t0)["q"]["q50"]
    t = forecast.step_times(t0)
    for hour in (13, 14, 15, 16):
        for quarter in (0, 2):
            i = int(np.where(((t + 5 * 3600) % 86400) // 900 == hour * 4 + quarter)[0][0])
            assert q50[i] == pytest.approx(100.0, rel=0.02), (hour, quarter * 15, q50[i])


def _shed(n_up=30, n_down=60, n_after=10, down_kw=30.0):
    load = np.array([100.0] * n_up + [down_kw] * n_down + [100.0] * n_after)
    grid = np.array([1.0] * n_up + [0.0] * n_down + [1.0] * n_after)
    return load, grid


def test_the_drop_rule_reads_known_samples_not_consecutive_minutes(company):
    site = {"assets": {}}
    # a lost minute half an hour into a shed outage: the whole outage is censored, not only the part before the gap
    load, grid = _shed()
    load[60], grid[60] = np.nan, np.nan
    cen = forecast.censored_minutes(site, {"load": load, "grid": grid})
    assert cen[30:90].all() and not cen[:30].any() and not cen[90:].any()
    # a one- and a two-minute grid flicker inside it: bridged, and the flicker's own minutes leave the medians with the run
    for width in (1, 2):
        load, grid = _shed()
        grid[60:60 + width] = 1.0
        load[60:60 + width] = 100.0
        cen = forecast.censored_minutes(site, {"load": load, "grid": grid})
        assert cen[30:90].all() and not cen[:30].any() and not cen[90:].any(), width
    # rows every 5 minutes: every grid-down sample is censored, not only the first
    load, grid = _shed()
    sparse = np.arange(len(load)) % 5 != 0
    load[sparse], grid[sparse] = np.nan, np.nan
    cen = forecast.censored_minutes(site, {"load": load, "grid": grid})
    down_samples = np.where(grid == 0)[0]
    assert cen[down_samples].all() and not cen[np.where(grid == 1)[0]].any()
    # a fully backed-up outage with a flicker keeps its samples (no drop): the outage load is the real demand
    load, grid = _shed(down_kw=97.0)
    grid[60] = 1.0
    assert not forecast.censored_minutes(site, {"load": load, "grid": grid}).any()
    # three grid-up minutes end the outage: a later shed run is judged on its own (and the earlier backed-up one stays in)
    load = np.array([100, 100, 100, 100, 95, 98, 100, 100, 100, 30, 30, 30, 100], float)
    grid = np.array([1, 1, 1, 1, 0, 0, 1, 1, 1, 0, 0, 0, 1], float)
    assert forecast.censored_minutes(site, {"load": load, "grid": grid}).tolist() == [False] * 9 + [True] * 3 + [False]
    # no grid-up reading within the hour before the loss: the drop cannot be judged and nothing is censored by it
    load, grid = _shed(n_up=100)
    load[:20], grid[:20] = 100.0, 1.0
    load[20:100], grid[20:100] = np.nan, np.nan
    assert not forecast.censored_minutes(site, {"load": load, "grid": grid}).any()


# ------------------------------------------------------------------------------------------------ new-battery-gap-diluted-by-soc-fallback

def test_the_battery_trust_gap_compares_the_estimate_with_the_bms_only_where_both_were_measured(company):
    sid = sites.create({"name": "Bat"}, "t")["id"]
    sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    site = sites.full(sid)
    now = int(datetime(2026, 11, 6, 12, 0, tzinfo=PKT).timestamp())
    start = now - 30 * 86400
    rows = []
    for i in range(30 * 1440):
        est = None if i < 20 * 1440 else 50.0  # 20 days of a CSV source (BMS only), then an edge with its estimate 10 points under the BMS
        rows.append((sid, start + 60 * i, "edge", 1.0, est, 60.0, 0.0))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "battery_soc_est_pct", "battery_soc_bms_pct", "battery_power_w"))
    st = battery.state(site, now)
    assert st["trust"] == pytest.approx(0.22, abs=1e-3)  # the 10-point gap over the 10 days with an estimate, not diluted to 3.3 by the BMS-only days
    assert "trust_low" in [a[0] for a in st["alerts"]]
    assert st["soc_pct"] == pytest.approx(50.0)
    # a BMS-only history alone has nothing to compare: no gap, and the state of charge is the BMS reading
    sid2 = sites.create({"name": "Bms only"}, "t")["id"]
    sites.put_asset(sid2, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    telemetry.write_rows([(sid2, start + 60 * i, "csv", 1.0, None, 60.0, 0.0) for i in range(2 * 1440)],
                         ("site_id", "ts", "source_id", "grid_present", "battery_soc_est_pct", "battery_soc_bms_pct", "battery_power_w"))
    st2 = battery.state(sites.full(sid2), now)
    assert st2["soc_pct"] == pytest.approx(60.0) and "trust_low" not in [a[0] for a in st2["alerts"]]


# ------------------------------------------------------------------------------------------------ new-report-fails-on-old-infinity-day

def test_an_old_infinity_day_row_is_healed_from_its_minutes_and_the_report_generates(company):
    sid = sites.create({"name": "Old rows"}, "t")["id"]
    t = int(datetime(2026, 10, 5, 10, 0, tzinfo=PKT).timestamp())
    # minutes written before ingest refused non-finite values: 1 kWh a minute for an hour, one minute Infinity
    rows = [(sid, t + 60 * i, "edge", 1.0, float("inf") if i == 30 else 1000.0) for i in range(60)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "grid_import_wh"))
    with store.connect() as con:  # the day row the nightly job wrote then (peak_import_kwh came with the peak flags)
        con.execute("INSERT INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                    (sid, "2026-10-05", json.dumps({"import_kwh": float("inf"), "peak_kw": float("inf"), "pv_kwh": 0.0, "peak_import_kwh": 7.5,
                                                    "min_soc": float("nan")})))
    got = telemetry.days(sid, "2026-10-01", "2026-10-31")
    assert len(got) == 1
    d = got[0]
    json.dumps(d, allow_nan=False)
    assert d["import_kwh"] == pytest.approx(59.0) and d["peak_kw"] == pytest.approx(60.0)
    assert d["peak_import_kwh"] == 7.5                     # a field the re-aggregation does not compute is kept
    assert all(not (isinstance(v, float) and not math.isfinite(v)) for v in d.values())
    stored = _day_rows(sid)["2026-10-05"]
    assert "Infinity" not in stored and "NaN" not in stored  # healed once, in the file
    assert json.loads(stored)["import_kwh"] == pytest.approx(59.0)
    for kind in ("energy_only", "monthly_savings"):
        rep = reports.generate(kind, sid, "2026-10", "t")
        assert rep["id"], kind
    # a day row whose minutes are gone reads its non-finite numbers as missing, and is stored that way
    sid2 = sites.create({"name": "No minutes"}, "t")["id"]
    with store.connect() as con:
        con.execute("INSERT INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                    (sid2, "2026-10-05", json.dumps({"import_kwh": float("inf"), "pv_kwh": 1.0})))
    d2 = telemetry.days(sid2, "2026-10-01", "2026-10-31")[0]
    assert d2["import_kwh"] is None and d2["pv_kwh"] == 1.0
    assert "Infinity" not in _day_rows(sid2)["2026-10-05"]


# ------------------------------------------------------------------------------------------------ regression-days-reheal-every-read

def _huge_minutes(sid: str, day: date) -> None:
    """The verifier's day, written as minutes stored before ingest capped magnitudes: 120 minutes of grid_import_wh 1e308 (finite, but a sum of
    them overflows) and 500 minutes of 1 kW of sun."""
    t = int(datetime(day.year, day.month, day.day, 10, tzinfo=PKT).timestamp())
    rows = [(sid, t + 60 * i, "edge", 1.0, 1e308, None) for i in range(120)]
    rows += [(sid, t - 9 * 3600 + 60 * i, "edge", 1.0, None, 1000.0) for i in range(500)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "pv_power_w"))


def test_a_day_whose_minutes_overflow_is_stored_finite_and_never_re_aggregated_on_read(company, monkeypatch):
    sid = "huge"
    d0 = date(2026, 10, 1)
    for k in range(3):
        _huge_minutes(sid, d0 + timedelta(days=k))
    # the nightly job's aggregation: the overflowing totals are stored as missing, never as Infinity
    for k in range(2):
        got = telemetry.aggregate_day(sid, d0 + timedelta(days=k))
        assert got["import_kwh"] is None and got["peak_kw"] is None and got["pv_kwh"] == pytest.approx(500 / 60, abs=0.01)
        json.dumps(got, allow_nan=False)
    stored = _day_rows(sid)
    assert all("Infinity" not in s and "NaN" not in s for s in stored.values())
    # a third day row the old aggregation wrote with Infinity: healed on the first read, then never again
    with store.connect() as con:
        con.execute("INSERT INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)",
                    ("huge", "2026-10-03", json.dumps({"import_kwh": float("inf"), "peak_kw": float("inf"), "pv_kwh": 8.33, "peak_import_kwh": 1.5})))
    calls = []
    real = telemetry.aggregate_day
    monkeypatch.setattr(telemetry, "aggregate_day", lambda *a, **kw: calls.append(a[1]) or real(*a, **kw))
    first = telemetry.days(sid, "2026-10-01", "2026-10-31")
    assert calls == [date(2026, 10, 3)]                                  # only the old Infinity row is re-aggregated
    assert "Infinity" not in _day_rows(sid)["2026-10-03"]                # and written back finite, even though its sums still overflow
    assert first[2]["import_kwh"] is None and first[2]["peak_import_kwh"] == 1.5
    for _ in range(3):
        rows = telemetry.days(sid, "2026-10-01", "2026-10-31")
        json.dumps(rows, allow_nan=False)
        assert [r["import_kwh"] for r in rows] == [None, None, None]
    assert calls == [date(2026, 10, 3)]                                  # no read re-aggregated or rewrote a row again
    assert _day_rows(sid) == {**stored, "2026-10-03": _day_rows(sid)["2026-10-03"]}


def test_ingest_refuses_absurd_magnitudes_so_a_day_cannot_overflow(company):
    sid = "cap"
    now = int(datetime(2026, 11, 6, tzinfo=PKT).timestamp())
    day = date(2026, 10, 1)
    t = int(datetime(day.year, day.month, day.day, 10, tzinfo=PKT).timestamp())
    recs = [{"ts": t + 60 * i, "grid_present": 1, "grid_import_wh": 1e308} for i in range(120)]       # the verifier's crafted edge batch
    recs += [{"ts": t + 7200 + 60 * i, "grid_present": 1, "grid_import_wh": -2e9, "load_power_w": "5e12"} for i in range(5)]
    recs += [{"ts": t + 9000, "grid_present": 1, "grid_import_wh": 900_000.0, "grid_voltage_v": 132_000, "grid_power_w": -5e7}]  # a 50 MW 132 kV site
    res = telemetry.ingest(sid, recs, "edge", now=now)
    assert res["accepted"] == 126 and res["unreadable_cells"] == 130
    last = telemetry.latest(sid)
    assert last["grid_import_wh"] == 900_000.0 and last["grid_power_w"] == -5e7
    data = telemetry.aggregate_day(sid, day)
    assert data["import_kwh"] == pytest.approx(900.0) and math.isfinite(data["peak_kw"])
    # a CSV upload says why the cells were dropped
    csv_res = telemetry.import_csv("cap2", _csv(["ts", "grid_present", "grid_import_wh"], [[_iso(t + 60 * i), 1, "1e308"] for i in range(3)]), now=now)
    assert any("not finite numbers" in line and "beyond any meter's range" in line for line in csv_res["log"])
    assert all(r["import_kwh"] is None or math.isfinite(r["import_kwh"]) for r in telemetry.days("cap2", "2026-10-01", "2026-10-31"))


# ------------------------------------------------------------------------------------------------ regression-edge-poison-batch

def test_ingest_skips_and_counts_rows_whose_time_is_out_of_range_instead_of_raising(company):
    sid = "poison"
    now = _now()
    good = now // 60 * 60 - 120
    bad = [1e20, 10 ** 30, 1e300, now * 1000, 31_536_000, now + 3 * 86400, "9999-12-31T00:00:00Z", "1971-01-02T00:00:00Z",
           "0001-01-01T00:00:00+05:00"]
    res = telemetry.ingest(sid, [{"ts": ts, "grid_present": 1} for ts in bad] + [{"ts": good, "grid_present": 1, "pv_power_w": 900}], "edge")
    assert res["accepted"] == 1 and res["skipped"] == len(bad) and res["out_of_range"] == len(bad)
    last = telemetry.latest(sid)
    assert last["ts"] == good and last["pv_power_w"] == 900
    # a row up to a day ahead (a logger clock a little fast) is kept; an explicit `now` sets the window
    assert telemetry.ingest(sid, [{"ts": now + 3600, "grid_present": 1}], "edge")["accepted"] == 1
    assert telemetry.ingest(sid, [{"ts": now + 3600, "grid_present": 1}], "edge", now=now - 2 * 86400)["out_of_range"] == 1


def test_an_edge_batch_with_an_overflowing_time_is_answered_not_failed_on_every_retry(world):
    sid = world.site
    kh = world.key(["telemetry:write"])
    good = _now() // 60 * 60 - 120
    batch = {"contract": "ems-edge/1.0", "seq": 3, "sent_at": "2026-11-06T10:00:00Z", "columns": ["ts", "grid_present", "pv_power_w"],
             "rows": [[1e20, 1, 5], [good, 1, 900]]}
    # a deterministic answer, never a 500: the route may refuse the batch (422) or store its good rows with the bad one skipped and counted (200)
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=batch)
    assert r.status_code in (200, 422), r.text
    if r.status_code == 200:
        assert r.json()["accepted"] == 1 and r.json()["skipped"] == 1 and r.json()["out_of_range"] == 1
        assert world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=batch).json()["duplicate"] is True
    else:
        assert world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=batch).status_code == 422  # the same answer on a retry
    # either way the stream moves on with the next sequence
    nxt = {**batch, "seq": 4, "rows": [[good + 60, 1, 950]]}
    assert world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=nxt).json().get("accepted") == 1
    with world.use():
        assert telemetry.latest(sid)["ts"] == good + 60


# ------------------------------------------------------------------------------------------------ security-csv-upload-blocks-event-loop

def test_the_csv_import_refuses_wild_dates_from_the_rows_it_parsed_and_stores_nothing(company):
    sid = sites.create({"name": "Upload"}, "t")["id"]
    now = int(datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc).timestamp())
    # a repeated ts header: the reader keeps the last cell, which is what the window must judge
    dup = b"ts,grid_present,ts\n2026-11-01T00:00:00Z,1,1971-01-02T00:00:00Z\n2026-11-01T00:01:00Z,1,1974-01-02T00:00:00Z\n"
    with pytest.raises(ValueError, match="before 2015"):
        telemetry.import_csv(sid, dup, now=now)
    with pytest.raises(ValueError, match="in the future"):
        telemetry.import_csv(sid, b"ts,grid_present\n2026-11-01T00:00:00Z,1\n9999-01-01T00:00:00Z,1\n", now=now)
    with pytest.raises(ValueError, match="spans 731 days"):
        telemetry.import_csv(sid, b"ts,grid_present\n2016-01-01T00:00:00Z,1\n2018-01-01T00:00:00Z,1\n", now=now)
    # a column mapped onto ts is judged the same way
    with pytest.raises(ValueError, match="before 2015"):
        telemetry.import_csv(sid, b"time,grid_present\n1980-01-01T00:00:00Z,1\n", mapping={"time": "ts"}, now=now)
    assert telemetry.latest(sid) is None and _imports(sid) == 0 and _day_rows(sid) == {}


def test_the_csv_import_aggregates_only_the_days_that_received_rows(company, monkeypatch):
    sid = sites.create({"name": "Sparse"}, "t")["id"]
    now = int(datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc).timestamp())
    seen = []
    real = telemetry.aggregate_day

    def spy(site_id, day, *a, **k):
        seen.append(day)
        return real(site_id, day, *a, **k)

    monkeypatch.setattr(telemetry, "aggregate_day", spy)
    rows = [[_iso(int(datetime(2026, 1, 10, 9, 0, tzinfo=PKT).timestamp()) + 60 * i), 1, 1000] for i in range(3)]
    rows += [[_iso(int(datetime(2026, 11, 1, 23, 58, tzinfo=PKT).timestamp()) + 60 * i), 1, 1000] for i in range(4)]  # crosses local midnight
    res = telemetry.import_csv(sid, _csv(["ts", "grid_present", "pv_power_w"], rows), now=now)
    assert res["rows"] == 7
    assert seen == [date(2026, 1, 10), date(2026, 11, 1), date(2026, 11, 2)]  # not the 297 days between
    assert sorted(_day_rows(sid)) == ["2026-01-10", "2026-11-01", "2026-11-02"]


def test_the_upload_route_refuses_a_repeated_ts_header_that_hides_a_wild_date(world):
    sid = world.site
    body = "ts,grid_present,ts\n2026-11-01T00:00:00Z,1,1971-01-02T00:00:00Z\n2026-11-01T00:01:00Z,1,1974-01-02T00:00:00Z\n"
    r = world.c["operator"].post(f"/api/sites/{sid}/telemetry/upload", content=body, headers={"Content-Type": "text/csv"})
    assert r.status_code == 422, r.text  # the route's own header check or the import's window, whichever reads it first
    with world.use():
        assert telemetry.latest(sid) is None and _imports(sid) == 0 and _day_rows(sid) == {}


# ------------------------------------------------------------------------------------------------ tariffs-vectors-billing-month

def test_the_connection_stores_its_reading_day_and_the_billing_period_follows_it(company):
    sid = sites.create({"name": "Meter"}, "t")["id"]
    c = sites.put_connection(sid, {"tariff_category": "B-2(b)", "reading_day": 15}, "t")
    assert c["data"]["reading_day"] == 15
    assert tariffs.billing_month_for(c, date(2026, 8, 25)) == "2026-09"
    assert tariffs.billing_period_for(c, date(2026, 8, 25)) == (date(2026, 8, 16), date(2026, 9, 15))
    for bad in (0, 32, 15.5, "mid-month"):
        with pytest.raises(Refusal) as e:
            sites.put_connection(sid, {"tariff_category": "B-2(b)", "reading_day": bad}, "t", c["id"])
        assert e.value.status_code == 422 and "reading day" in e.value.detail, bad
    # unset, the bill is the calendar month
    c2 = sites.put_connection(sid, {"tariff_category": "B-2(b)"}, "t", c["id"])
    assert c2["data"]["reading_day"] is None
    assert tariffs.billing_period_for(c2, date(2026, 8, 25)) == (date(2026, 8, 1), date(2026, 8, 31))
