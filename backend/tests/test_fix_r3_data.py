"""Round-3 tests of the data group (telemetry.py, forecast.py, sites.py): the forecasts count days of data, not 1-minute rows, for a 5- or 15-minute
source; two first opens of a new telemetry file at once; the named observed-grid lag; the CSV import's uploaded range and file name; and a demo site's
settings save, the off-grid flag and the data source; the imports rows as the quality page reads them (log always a list, every route field kept).

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own."""
from __future__ import annotations

import io
import json
import threading
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import forecast, solar, sites, store, telemetry
from gridlytics import platform as platformdb

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
    """A company of its own with an owner signed in (as tests/test_fix_r2_data.py)."""
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    import identities
    co = identities.company("R3 data company", own_file=True)
    db = platformdb.company_file(co)
    db.parent.mkdir(parents=True, exist_ok=True)
    admin = identities.client("admin@r3data.test", None, co, "owner", raise_server_exceptions=False)
    return types.SimpleNamespace(db=db, admin=admin, use=lambda: store.use_company(db))


def _at(t: np.ndarray, hour: int) -> int:
    return int(np.where(((t + 5 * 3600) % 86400) // 900 == hour * 4)[0][0])


# ------------------------------------------------------------------------------------------------ regression-coarse-source-load-falls-to-template

def _demand(minute: int) -> float:
    return 120.0 if 8 * 60 <= minute < 20 * 60 else 60.0  # 120 kW from 08:00 to 20:00, 60 kW at night


@pytest.mark.parametrize("step", [5, 15])
def test_a_coarse_source_with_eight_hours_shed_daily_stays_at_stage_1(company, step):
    """The verifier's case: 21 days of rows every 5 (or 15) minutes, the grid shed 09:00-17:00 every day with only 30 kW of backed-up load lit. The
    shed samples are censored, which left under 3 days' worth of 1-minute rows; counted as days of data the forecast stays at stage 1, the night and
    evening medians at the true demand, and the shed slots fall to the template, never to the 30 kW served in them."""
    sid = f"coarse-{step}"
    t0 = int(datetime(2026, 11, 23, tzinfo=PKT).timestamp())
    h0 = t0 - 21 * 86400
    rows = []
    for i in range(0, 21 * 1440, step):
        minute = i % 1440
        shed = 9 * 60 <= minute < 17 * 60
        rows.append((sid, h0 + 60 * i, "edge", 0.0 if shed else 1.0, (30.0 if shed else _demand(minute)) * 1000))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "load_power_w"))
    site = {"id": sid, "data": {"load_peak_kw": 120.0, "site_type": "industrial_2_shift"}, "assets": {}, "connections": []}
    f = forecast.load(site, t0)
    assert f["stage"] == 1 and 13 <= f["days"] <= 15, (f["stage"], f["days"])  # 16 unshed hours a day over 21 days
    t = forecast.step_times(t0)
    q50 = f["q"]["q50"]
    for hour in (2, 21):
        assert q50[_at(t, hour)] == pytest.approx(60.0, rel=0.02), (hour, q50[_at(t, hour)])
    tmpl = forecast._mean15(lambda x: forecast.twin.load_template(site, x), t)
    for hour in (10, 15):
        assert q50[_at(t, hour)] == pytest.approx(tmpl[_at(t, hour)], rel=1e-6) and q50[_at(t, hour)] > 90.0


def test_a_five_minute_source_gets_the_recent_ratio_as_a_one_minute_source_does(company):
    """The last two hours' ratio counts minutes of data too: 24 rows every 5 minutes cover the two hours, so a load running 30 percent above its usual
    level lifts the first steps of the forecast."""
    sid = "ratio-5"
    t0 = int(datetime(2026, 11, 23, 12, 0, tzinfo=PKT).timestamp())
    h0 = t0 - 21 * 86400
    rows = [(sid, h0 + 60 * i, "edge", 1.0, (130.0 if h0 + 60 * i >= t0 - 7200 else 100.0) * 1000) for i in range(0, 21 * 1440, 5)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "load_power_w"))
    site = {"id": sid, "data": {"load_peak_kw": 100.0}, "assets": {}, "connections": []}
    f = forecast.load(site, t0)
    assert f["stage"] == 1
    assert f["q"]["q50"][0] == pytest.approx(130.0, rel=0.03)
    assert f["q"]["q50"][100] == pytest.approx(100.0, rel=0.03)  # the lift decays over the horizon


def test_a_five_minute_pv_source_reaches_stage_1(company):
    """Fourteen days of PV every 5 minutes at 80 percent of clear sky: each day has more than four hours of daylight data, so the PV forecast fits."""
    sid = "pv-5"
    pv_asset = {"kwp_dc": 150.0, "kw_ac": 120.0, "tilt": 15.0, "azimuth": 180.0}
    site = {"id": sid, "data": {"lat": 31.5, "lon": 74.3}, "assets": {"pv": pv_asset}, "connections": []}
    t0 = int(datetime(2026, 11, 16, 11, 0, tzinfo=PKT).timestamp())
    ts = np.arange(t0 - 14 * 86400, t0, 300, dtype=np.int64)
    pv = solar.pv_power(ts, 31.5, 74.3, pv_asset, 1.0, solar.local_month(ts)) * 0.8
    telemetry.write_rows([(sid, int(a), "edge", 1.0, float(b) * 1000) for a, b in zip(ts, pv)], ("site_id", "ts", "source_id", "grid_present", "pv_power_w"))
    out = forecast.pv(site, t0)
    assert out["stage"] == 1 and out["days"] >= 13
    assert out["pr"] == pytest.approx(0.8, abs=0.05)


def test_a_five_minute_source_s_outages_are_one_event_each_in_the_hazard(company):
    """28 days of grid readings every 5 minutes, the grid lost 18:00-20:00 every day: one start a day of about two hours, not one start per sample, and
    28 days of history (stage 1)."""
    sid = "outage-5"
    site = {"id": sid, "data": {"disco": "LESCO", "lat": 31.5, "lon": 74.3}, "connections": [{"id": "c1", "data": {"regime": "none"}}], "assets": {}}
    t0 = int(datetime(2026, 11, 23, tzinfo=PKT).timestamp())
    h0 = t0 - 28 * 86400
    rows = [(sid, h0 + 60 * i, "edge", 0.0 if 18 * 60 <= i % 1440 < 20 * 60 else 1.0) for i in range(0, 28 * 1440, 5)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present"))
    o = forecast.outage(site, t0)
    assert o["stage"] == 1
    assert 115 <= o["median_minutes"] <= 125, o["median_minutes"]
    assert 0.8 <= o["lam_day"] <= 1.2, o["lam_day"]
    t = forecast.step_times(t0)
    assert o["p_down"][_at(t, 19)] > o["p_down"][_at(t, 3)]


def test_a_one_minute_source_s_counts_are_unchanged(company):
    assert forecast.sample_minutes(np.ones(100, dtype=bool)) == 1
    known = np.zeros(1440, dtype=bool)
    known[::5] = True
    known[100:400] = False  # a lost stretch does not change the source's interval
    assert forecast.sample_minutes(known) == 5
    known = np.zeros(1440, dtype=bool)
    known[::60] = True
    assert forecast.sample_minutes(known) == 15  # a sample stands for at most one 15-minute step
    assert forecast.sample_minutes(np.zeros(10, dtype=bool)) == 1


# ------------------------------------------------------------------------------------------------ forecast.OBSERVED_LAG_MIN

def test_the_observed_grid_lag_is_a_named_constant_the_outage_reads(company, monkeypatch):
    sid = "lag"
    site = {"id": sid, "data": {"disco": "LESCO"}, "connections": [{"id": "c1", "data": {"regime": "none"}}], "assets": {}}
    t0 = int(datetime(2026, 11, 2, 10, 0, tzinfo=PKT).timestamp())
    telemetry.ingest(sid, [{"ts": t0 + 60 * i, "grid_present": 0 if i >= 3 else 1} for i in range(-10, 6)], "edge")
    now = t0 + 9 * 60  # the last reading (10:05) lags the last whole minute (10:08) by 3 minutes
    assert forecast.OBSERVED_LAG_MIN == 2
    assert forecast.outage(site, t0, now=now)["observed"] is None
    monkeypatch.setattr(forecast, "OBSERVED_LAG_MIN", 3)
    assert forecast.outage(site, t0, now=now)["observed"]["since"] == t0 + 180


# ------------------------------------------------------------------------------------------------ telemetry._open: two first opens at once

def test_first_opens_of_a_new_telemetry_file_at_once_all_succeed(tmp_path, monkeypatch):
    """Eight threads open a brand-new company's telemetry file at the same moment, in six fresh companies: none answers 'database is locked'."""
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    (tmp_path / "companies").mkdir()
    errors: list[str] = []
    for n in range(6):
        db = tmp_path / "companies" / f"c{n}.db"
        gate = threading.Barrier(8)

        def first_open(db=db, gate=gate):
            try:
                with store.use_company(db):
                    gate.wait(timeout=30)
                    assert telemetry.latest("s1") is None
            except Exception as exc:  # noqa: BLE001 - every failure is reported below
                errors.append(repr(exc))

        threads = [threading.Thread(target=first_open) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=60)
        with store.use_company(db):
            with telemetry.connect() as con:
                assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
                assert con.execute("PRAGMA user_version").fetchone()[0] == telemetry.SCHEMA_VERSION
    assert not errors, errors


# ------------------------------------------------------------------------------------------------ imports carry the uploaded range and the file name

def _csv(rows: list[tuple]) -> bytes:
    buf = io.StringIO()
    buf.write("ts,grid_present,load_power_w\n")
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    return buf.getvalue().encode()


def test_an_import_keeps_its_uploaded_range_and_file_name(company):
    sid = sites.create({"name": "Uploads"}, "t")["id"]
    # an import stored before the range was kept: the table as the workspace schema made it, without the new columns
    with store.connect() as con:
        con.execute("INSERT INTO imports (id, site_id, source, file_hash, rows, gaps, status, log, created) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("old", sid, "csv", "h", 5, 0, "done", "[]", "2026-01-01T00:00:00+00:00"))
    start = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
    res = telemetry.import_csv(sid, _csv([(datetime.fromtimestamp(start + 60 * i, timezone.utc).isoformat(), 1, 50000) for i in range(30)]),
                               file_name="C:\\logs\\site-a\\march\x07.csv")
    assert res["from_ts"] == start and res["to_ts"] == start + 29 * 60 and res["file_name"] == "march.csv"
    assert res["from"] == datetime.fromtimestamp(start, timezone.utc).isoformat()  # the old fields stay
    rows = telemetry.imports(sid)
    assert [r["id"] for r in rows] == [res["id"], "old"]
    new, old = rows
    assert (new["from_ts"], new["to_ts"], new["file_name"], new["rows"], new["source"]) == (start, start + 29 * 60, "march.csv", 30, "csv")
    assert isinstance(new["log"], list) and new["created"]
    assert (old["from_ts"], old["to_ts"], old["file_name"]) == (None, None, None)
    # without a file name, and with a name of only blanks, the name is not known
    res2 = telemetry.import_csv(sid, _csv([(datetime.fromtimestamp(start + 86400, timezone.utc).isoformat(), 1, 1000)]))
    res3 = telemetry.import_csv(sid, _csv([(datetime.fromtimestamp(start + 2 * 86400, timezone.utc).isoformat(), 1, 1000)]), file_name="  /  ")
    assert res2["file_name"] is None and res3["file_name"] is None
    assert telemetry.imports(sid, limit=1)[0]["id"] in (res2["id"], res3["id"])
    assert len(telemetry._file_name("x" * 500 + ".csv")) == telemetry.FILE_NAME_MAX


def test_the_imports_rows_carry_every_field_the_quality_page_reads_whatever_the_stored_log(world):
    """telemetry.imports is what the quality route can return in place of its own SELECT: a superset of the route's fields on a workspace file whose
    imports table predates the new columns (as one restored from an older backup), and a log that is always a list, even where the stored text is
    JSON null, an object, a number or not JSON at all."""
    with world.use():
        sid = sites.create({"name": "Logs"}, "t")["id"]
        with store.connect() as con:
            con.execute("INSERT INTO imports (id, site_id, source, file_hash, rows, gaps, status, log, created) VALUES (?,?,?,?,?,?,?,?,?)",
                        ("ok", sid, "csv", "h", 5, 0, "done", '["read 5 rows"]', "2026-01-01T00:00:00+00:00"))
    route = world.admin.get(f"/api/sites/{sid}/telemetry/quality")
    assert route.status_code == 200, route.text
    route_fields = set(route.json()["imports"][0])
    with world.use():
        with store.connect() as con:
            for n, log in enumerate(["null", '{"note": 1}', "7", "not json"]):  # four in the same second: the later first
                con.execute("INSERT INTO imports (id, site_id, source, file_hash, rows, gaps, status, log, created) VALUES (?,?,?,?,?,?,?,?,?)",
                            (f"bad{n}", sid, "csv", f"h{n}", 1, 0, "done", log, "2026-02-01T00:00:00+00:00"))
        rows = telemetry.imports(sid)
    assert [r["id"] for r in rows] == ["bad3", "bad2", "bad1", "bad0", "ok"]
    assert all(route_fields | {"from_ts", "to_ts", "file_name"} <= set(r) for r in rows)
    assert [r["log"] for r in rows] == [[], [], [], [], ["read 5 rows"]]
    assert all((r["from_ts"], r["to_ts"], r["file_name"]) == (None, None, None) for r in rows)


def test_the_quality_page_shows_an_upload_s_range_and_file_name(world):
    """The reproduced scenario: a CSV uploaded through the route with its file name; the quality page's imports row shows the range and the name."""
    c = world.admin
    sid = c.post("/api/sites", json={"name": "Quality"}).json()["id"]
    start = (int(datetime.now(timezone.utc).timestamp()) // 86400 - 2) * 86400
    body = _csv([(datetime.fromtimestamp(start + 60 * i, timezone.utc).isoformat(), 1, 40000) for i in range(30)])
    up = c.post(f"/api/sites/{sid}/telemetry/upload", params={"file_name": "C:\\x\\may.csv"}, content=body,
                headers={"content-type": "text/csv"})
    assert up.status_code == 200, up.text
    assert (up.json()["from_ts"], up.json()["to_ts"], up.json()["file_name"]) == (start, start + 29 * 60, "may.csv")
    q = c.get(f"/api/sites/{sid}/telemetry/quality")
    assert q.status_code == 200, q.text
    row = q.json()["imports"][0]
    assert (row["id"], row["from_ts"], row["to_ts"], row["file_name"]) == (up.json()["id"], start, start + 29 * 60, "may.csv")


# ------------------------------------------------------------------------------------------------ sites: demo save, off_grid, data_source

DEMO_EXTRA = {"load_peak_kw": 330, "twin": {"windows": [["14:00", "16:00"]], "adherence": 0.8, "trips_per_day": 0.8, "trip_minutes": 70,
                                            "unconfirmed_alert": True}}


def _demo_site(by: str = "test") -> str:
    """A site as demo.create_sites leaves it: created through the model, then the demo's extras written into its data directly."""
    s = sites.create({"name": "Faisalabad textile mill (demo)", "disco": "FESCO", "control_mode": "mode_a"}, by, demo_link="demo")
    with store.connect() as con:
        con.execute("UPDATE sites SET data = ?, status = 'active' WHERE id = ?", (json.dumps({**s["data"], **DEMO_EXTRA}), s["id"]))
    return s["id"]


def test_general_and_control_saves_of_a_demo_site_answer_200_and_keep_the_twin(world):
    with world.use():
        sid = _demo_site()
    c = world.admin
    r = c.put(f"/api/sites/{sid}", json={"name": "Textile mill (renamed)", "city": "Faisalabad", "address": "Jhang Road"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["twin"] == DEMO_EXTRA["twin"] and r.json()["data"]["load_peak_kw"] == 330
    r = c.put(f"/api/sites/{sid}", json={"control_mode": "mode_b", "cycle_cap_per_day": 1.2, "reserve_policy": {"p_threshold": 0.3, "min_soc_pct": 30}})
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["control_mode"] == "mode_b" and d["twin"] == DEMO_EXTRA["twin"] and d["name"] == "Textile mill (renamed)"
    # the settings form sent back whole, the twin as it was read
    r = c.put(f"/api/sites/{sid}", json={**c.get(f"/api/sites/{sid}").json()["data"], "notes": "Two shifts"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["twin"] == DEMO_EXTRA["twin"] and r.json()["data"]["notes"] == "Two shifts"
    # the twin is the demo's: a save cannot change it, and a real site cannot be given one
    r = c.put(f"/api/sites/{sid}", json={"twin": {"windows": []}})
    assert r.status_code == 422 and "twin" in r.json()["detail"]
    with world.use():
        assert sites.get(sid)["data"]["twin"] == DEMO_EXTRA["twin"]
    other = c.post("/api/sites", json={"name": "Real mill"}).json()["id"]
    assert c.put(f"/api/sites/{other}", json={"twin": {"windows": []}}).status_code == 422
    # the peak load is a site field of its own now, checked as one
    r = c.put(f"/api/sites/{sid}", json={"load_peak_kw": -5})
    assert r.status_code == 422
    r = c.put(f"/api/sites/{other}", json={"load_peak_kw": 250})
    assert r.status_code == 200 and r.json()["data"]["load_peak_kw"] == 250


def test_the_off_grid_flag_and_the_data_source_are_stored_and_the_confirmation_stamped_by_the_server(world):
    c = world.admin
    r = c.post("/api/sites", json={"name": "Farm", "site_type": "office", "off_grid": True,
                                   "data_source": {"kind": "edge", "grid_signal_confirmed": True, "confirmed_by": "someone else", "at": "2001-01-01"}})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    d = r.json()["data"]
    assert d["off_grid"] is True
    src = d["data_source"]
    assert src["kind"] == "edge" and src["grid_signal_confirmed"] is True
    assert src["confirmed_by"] != "someone else" and src["confirmed_by"] and src["at"] != "2001-01-01"
    stamp = dict(src)
    # a later save keeps the confirmation's stamp, whatever it sends
    r = c.put(f"/api/sites/{sid}", json={"notes": "x", "data_source": {**stamp, "confirmed_by": "forged", "at": "1999-01-01"}})
    assert r.status_code == 200 and r.json()["data"]["data_source"] == stamp
    r = c.put(f"/api/sites/{sid}", json={"notes": "y"})
    assert r.json()["data"]["data_source"] == stamp
    # an unconfirmed source carries no stamp; an unknown kind is refused in words
    r = c.put(f"/api/sites/{sid}", json={"data_source": {"kind": "csv", "grid_signal_confirmed": False}})
    assert r.status_code == 200 and r.json()["data"]["data_source"] == {"kind": "csv", "grid_signal_confirmed": False, "confirmed_by": "", "at": None}
    r = c.put(f"/api/sites/{sid}", json={"data_source": {"kind": "modbus"}})
    assert r.status_code == 422 and "kind" in r.json()["detail"]
    r = c.put(f"/api/sites/{sid}", json={"off_grid": False})
    assert r.status_code == 200 and r.json()["data"]["off_grid"] is False
    # a site saved without them reads the defaults
    plain = c.post("/api/sites", json={"name": "Plain"}).json()["data"]
    assert plain["off_grid"] is False and plain["data_source"] is None and plain["load_peak_kw"] is None


def test_two_state_writers_at_once_keep_both_fields(tmp_path, monkeypatch):
    """sites.put_state reads and writes in one write transaction: a writer held between its read and its write no longer loses the other's field."""
    import threading
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    from gridlytics import sites, store
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with store.use_company(db):
        sid = sites.create({"name": "State race"}, "t")["id"]
    real_dumps = sites.json.dumps
    gate, entered = threading.Event(), threading.Event()

    def slow_dumps(obj, *a, **k):
        if isinstance(obj, dict) and obj.get("first") == 1:
            entered.set()
            gate.wait(5)
        return real_dumps(obj, *a, **k)

    monkeypatch.setattr(sites.json, "dumps", slow_dumps)

    def first():
        with store.use_company(db):
            sites.put_state(sid, first=1)

    th = threading.Thread(target=first)
    th.start()
    assert entered.wait(5)
    done = threading.Event()

    def second():
        with store.use_company(db):
            sites.put_state(sid, second=2)
        done.set()

    th2 = threading.Thread(target=second)
    th2.start()
    # the old read-then-write finished the second write within this pause (and the first then wrote over it); now the second waits for the lock
    import time
    time.sleep(0.6)
    gate.set()
    th.join(10)
    th2.join(10)
    with store.use_company(db):
        st = sites.state(sid) if hasattr(sites, "state") else sites.full(sid)["state"]
    assert st.get("first") == 1 and st.get("second") == 2
