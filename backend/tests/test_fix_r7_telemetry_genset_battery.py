"""Round-7 audit tests of the telemetry, genset and battery package (telemetry.py, routes_telemetry.py).

Batch 1, CSV import integrity: an upload that overlaps stored minutes merges per column (flow-telemetry-plan-control-01); a 5- or 15-minute file is
stored as the intervals it covers (-10); implausible values are reported per file, and impossible ones stored as missing (-13); a grid_present column
that cannot be read is refused or reported (-14); duplicate times are counted once and reported (-24); a probable time-zone slip is reported
(frontend-states-formats-06 and flow-telemetry-plan-control-11, the backend's half).

Batch 2, genset litres, runs, fuel cost and cards: litres metered where a meter reads and estimated from the fuel curve where not, never 0 L for an
unmetered set, with the source named as the Savings page names it (pages-C-03 and its duplicates, the Overview's month label included); runs and outages
counted in minutes, a blank sample ending neither (pages-C-04); no run signal told apart from no runs (pages-C-05); each run's fuel priced at its own
day's price (role-owner-07); the KPI row's cost per kWh, hours below the minimum load, month view and daily figures (pages-C-15); the fuel curve's
measured points and refit (pages-C-16).

Batch 3, CSV mapping, refusals, data quality and series: today's completeness is counted over the minutes elapsed so far, not the whole day
(role-viewer-07, and its duplicate role-analyst-09); a file's times may be epoch seconds or milliseconds, a refusal of unreadable times names the
formats read, and a file over the upload cap is told the importer's sentence however it arrives (flow-telemetry-plan-control-25); a vendor export is read through a
column mapping (column to key, unit, sign, date order) saved per site, guessed from the file's head, and a file without the keys is refused with the
columns it has and the ones expected; a semicolon file with decimal commas is read (flow-telemetry-plan-control-12).

Batch 4, the battery page: this Karachi month's cycles, throughput, hot-and-full hours and wear, the cycles over the battery's life, and the state of
health named as entered (pages-C-13); the daily rate divided by the days with readings, which the answer gives (role-owner-38); the state of charge
named by its source, the estimate or the BMS, and the outages for the chart (pages-C-14).

Batch 5, the telemetry file's cost: a read takes a deferred (read) transaction, never the write lock, so readers neither wait for a writer nor make one
wait, and the export formats its CSV outside any transaction (performance-live-03, and its duplicate tests-and-perf-04); a chart series never answers
more than SERIES_MAX_POINTS (2,000) points, an explicit resolution that would is coarsened for every role, and the automatic one is the finest within the
cap (tests-and-perf-05, and its duplicate performance-live-13); a CSV upload is parsed once, in one streamed pass into columns, with memory a small multiple
of the file, and written one Karachi day per transaction (performance-live-04, and its duplicate tests-and-perf-23); forecast issues are kept whole for
3 days, hourly for 35 days (the stage-1 bands' 16-day window and the scoring), then only each Karachi day's day-ahead issue, the one scoring reads
(performance-live-06; the minute rows' 13-month Parquet archive, DAT-07, is planned work).

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own."""
from __future__ import annotations

import io
import json
import types
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import sites, store, telemetry
from gridlytics import platform as platformdb

PKT = timezone(timedelta(hours=5))
DAY = "2026-11-05"
T0 = int(datetime(2026, 11, 5, tzinfo=PKT).timestamp())  # 5 Nov 2026, 00:00 Karachi time
NOW = T0 + 10 * 86400


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


@pytest.fixture()
def world(tmp_path, monkeypatch):
    """A company of its own with an operator signed in (as tests/test_fix_r3_data.py)."""
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    import identities
    co = identities.company("R7 telemetry company", own_file=True)
    db = platformdb.company_file(co)
    db.parent.mkdir(parents=True, exist_ok=True)
    owner = identities.client("owner@r7tel.test", None, co, "owner", raise_server_exceptions=False)
    role = lambda r: identities.client(f"{r}@r7tel.test", r, co, "member", raise_server_exceptions=False)  # noqa: E731
    return types.SimpleNamespace(db=db, owner=owner, role=role, use=lambda: store.use_company(db))


def _local(ts: int) -> str:
    """A time as a logger set to Karachi time writes it: no offset."""
    return datetime.fromtimestamp(ts, PKT).replace(tzinfo=None).isoformat()


def _file(header: tuple, rows: list[tuple]) -> bytes:
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for r in rows:
        buf.write(",".join("" if x is None else str(x) for x in r) + "\n")
    return buf.getvalue().encode()


def _site(name: str = "Uploads") -> str:
    return sites.create({"name": name}, "t")["id"]


def _day(sid: str, day: str = DAY) -> dict:
    rows = telemetry.days(sid, day, day)
    assert rows, f"no day row for {day}"
    return rows[0]


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-01: overlap merges

def test_an_overlapping_upload_keeps_every_column_the_file_does_not_carry(company):
    """The audit's case: a full file for 12:00-12:59, then a file of only ts, grid_present and load_power_w for the same minutes. The second file's load
    replaces the stored load; PV, the battery, the grid power and the BMS state of charge stay, and so do an edge batch's own columns. A blank cell in a
    carried column is missing, so it does not erase a stored reading either. The answer says how many minutes were already stored."""
    sid = _site("Overlap")
    t = T0 + 12 * 3600
    head = ("ts", "grid_present", "grid_power_w", "pv_power_w", "load_power_w", "battery_power_w", "battery_soc_bms_pct")
    full = _file(head, [(_local(t + 60 * i), 1, 114600, 150000, 224600, -40000, 70) for i in range(60)])
    first = telemetry.import_csv(sid, full, 5, now=NOW)
    # an edge batch on the same minutes carries the estimate a CSV never does: it merges too, and keeps the file's PV
    telemetry.ingest(sid, [{"ts": t + 60 * i, "grid_present": 1, "battery_soc_est_pct": 71.0} for i in range(60)], "edge", now=NOW)
    before = telemetry.aggregate_day(sid, date.fromisoformat(DAY))  # an edge batch does not aggregate the day itself
    part = _file(("ts", "grid_present", "load_power_w"), [(_local(t + 60 * i), 1, None if i == 0 else 230000) for i in range(60)])
    res = telemetry.import_csv(sid, part, 5, now=NOW)
    cols = ("pv_power_w", "battery_soc_bms_pct", "battery_power_w", "grid_power_w", "load_power_w", "battery_soc_est_pct", "grid_present")
    d = telemetry.raw(sid, t, t + 3600, cols)
    assert len(d["ts"]) == 60
    assert np.all(d["pv_power_w"] == 150000) and np.all(d["battery_soc_bms_pct"] == 70) and np.all(d["battery_power_w"] == -40000)
    assert np.all(d["grid_power_w"] == 114600) and np.all(d["battery_soc_est_pct"] == 71.0) and np.all(d["grid_present"] == 1)
    assert d["load_power_w"][0] == 224600 and np.all(d["load_power_w"][1:] == 230000)
    after = _day(sid)
    for k in ("pv_kwh", "import_kwh", "import_completeness", "completeness", "min_soc", "max_soc"):
        assert after[k] == before[k], k
    assert after["load_kwh"] > before["load_kwh"]
    # the answer's fields come after the behaviour, so the test fails on the erased columns without the fix, not only on a missing field
    assert first["overlap"] == 0 and first["minutes"] == 60
    assert res["overlap"] == 60 and res["rows"] == 60
    assert any("60 minutes were already stored" in line for line in res["log"]), res["log"]


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-24: duplicates

def test_duplicate_times_are_counted_once_and_reported(company):
    """Each minute 10:00-10:59 twice: 60 minutes are stored (the last row of each), the answer and the imports row say 60 rows, and the duplicates are
    named in the notes."""
    sid = _site("Twice")
    t = T0 + 10 * 3600
    rows = []
    for i in range(60):
        rows += [(_local(t + 60 * i), 1, 1000), (_local(t + 60 * i), 1, 2000)]
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), rows), 5, now=NOW)
    assert (res["rows"], res["duplicates"], res["minutes"]) == (60, 60, 60)
    assert any(line.startswith("60 rows repeat a minute") for line in res["log"]), res["log"]
    assert telemetry.imports(sid)[0]["rows"] == 60
    d = telemetry.raw(sid, t, t + 3600, ("load_power_w",))
    assert len(d["ts"]) == 60 and np.all(d["load_power_w"] == 2000)  # the last row of each minute
    # a clean file reports no duplicates
    clean = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), [(_local(t + 7200 + 60 * i), 1, 5) for i in range(5)]), 5, now=NOW)
    assert clean["duplicates"] == 0 and not any("repeat a minute" in line for line in clean["log"])


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-10: 5- and 15-minute files

@pytest.mark.parametrize("step", [5, 15])
def test_a_coarse_file_is_stored_as_the_intervals_its_rows_cover(company, step):
    """The audit's case: a day at 15-minute (or 5-minute) steps, 200 kW of load all day, the grid off 14:00-15:00 and the meter's import as Wh per row.
    Each row is stored as the minutes it starts, energy spread evenly over them: the day's load is 4,800 kWh (not 1/15th), the peak 200 kW, the outage
    60 minutes, the day complete, and no row is a gap."""
    sid = _site(f"Coarse {step}")
    n = 1440 // step
    rows = []
    for i in range(n):
        ts = T0 + i * step * 60
        up = not (14 * 60 <= i * step < 15 * 60)
        rows.append((_local(ts), 1 if up else 0, 200000 if up else 0, 200000, 200000 * step / 60 if up else 0))
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "grid_power_w", "load_power_w", "grid_import_wh"), rows), 5, now=NOW)
    day = _day(sid)
    assert day["load_kwh"] == pytest.approx(4800.0)
    assert day["import_kwh"] == pytest.approx(200 * 23)
    assert day["peak_kw"] == pytest.approx(200.0)
    assert day["outage_minutes"] == 60
    assert day["completeness"] == 1.0
    out = telemetry.outages(sid, T0, T0 + 86400)
    assert [(o["start"], o["minutes"]) for o in out] == [(T0 + 14 * 3600, 60)]
    assert (res["rows"], res["step_min"], res["minutes"], res["gaps"]) == (n, step, 1440, 0)
    assert any(f"Rows are {step} minutes apart" in line for line in res["log"]), res["log"]


def test_a_missing_row_in_a_coarse_file_is_a_gap_and_its_minutes_stay_missing(company):
    sid = _site("Coarse gap")
    rows = [(_local(T0 + i * 900), 1, 100000) for i in range(96) if i != 40]
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), rows), 5, now=NOW)
    assert _day(sid)["completeness"] == pytest.approx(1425 / 1440, abs=1e-4)
    assert (res["rows"], res["step_min"], res["minutes"], res["gaps"]) == (95, 15, 1425, 1)
    # a 1-minute file keeps its rows as single minutes
    one = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), [(_local(T0 + 86400 + 60 * i), 1, 1) for i in range(30)]), 5, now=NOW)
    assert (one["step_min"], one["minutes"]) == (1, 30)


def test_rows_more_than_an_hour_apart_are_refused(company):
    sid = _site("Sparse")
    body = _file(("ts", "grid_present", "load_power_w"), [(_local(T0 + i * 7200), 1, 100000) for i in range(12)])
    with pytest.raises(ValueError, match="120 minutes apart"):
        telemetry.import_csv(sid, body, 5, now=NOW)
    assert telemetry.first_ts(sid) is None  # nothing stored


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-14: grid_present

def test_grid_present_written_as_on_and_off_is_read(company):
    sid = _site("Words")
    rows = [(_local(T0 + 9 * 3600 + 60 * i), "OFF" if 10 <= i < 25 else "On", 50000) for i in range(60)]
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), rows), 5, now=NOW)
    assert not res["warnings"]
    assert _day(sid)["outage_minutes"] == 15


def test_a_file_whose_grid_present_cannot_be_read_is_refused(company):
    sid = _site("Unreadable")
    rows = [(_local(T0 + 9 * 3600 + 60 * i), "N/A", 50000) for i in range(60)]
    with pytest.raises(ValueError, match="grid_present"):
        telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), rows), 5, now=NOW)
    blank = [(_local(T0 + 9 * 3600 + 60 * i), None, 50000) for i in range(60)]
    with pytest.raises(ValueError, match="grid_present"):
        telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), blank), 5, now=NOW)
    assert telemetry.first_ts(sid) is None
    # the grid voltage stands in where it can be read
    volts = [(_local(T0 + 9 * 3600 + 60 * i), "N/A", 230, 50000) for i in range(60)]
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "grid_voltage_v", "load_power_w"), volts), 5, now=NOW)
    assert res["rows"] == 60


def test_some_unreadable_grid_present_cells_are_a_warning(company):
    sid = _site("Partly")
    rows = [(_local(T0 + 9 * 3600 + 60 * i), "?" if i < 10 else 1, 50000) for i in range(60)]
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), rows), 5, now=NOW)
    assert any(w.startswith("10 grid_present cells could not be read") for w in res["warnings"]), res["warnings"]
    assert res["status"] == "done_with_warnings" and telemetry.imports(sid)[0]["status"] == "done_with_warnings"


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-13: plausibility

def _solar(ts: int, peak: float) -> float:
    h = ((ts + 5 * 3600) % 86400) / 3600
    return round(max(0.0, peak * np.sin(np.pi * (h - 6.5) / 11)) if 6.5 <= h <= 17.5 else 0.0, 3)


def test_kilowatts_in_watt_columns_are_reported(company):
    """The audit's C2 file: a day of load and PV in kW under the *_w names. Both columns are named in the warnings, and the import is marked done with
    warnings, never plain done; the same day in watts has no warning."""
    sid = _site("Units")
    rows = [(_local(T0 + 60 * i), 1, 250, _solar(T0 + 60 * i, 150)) for i in range(1440)]
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w", "pv_power_w"), rows), 5, now=NOW)
    text = " ".join(res["warnings"])
    assert "load_power_w" in text and "pv_power_w" in text and "kW" in text, res["warnings"]
    assert res["status"] == "done_with_warnings" and telemetry.imports(sid)[0]["status"] == "done_with_warnings"
    assert all(w in res["log"] for w in res["warnings"])  # the quality page's imports row shows them too
    good = [(_local(T0 + 86400 + 60 * i), 1, 250000, _solar(T0 + 86400 + 60 * i, 150000)) for i in range(1440)]
    ok = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w", "pv_power_w"), good), 5, now=NOW)
    assert ok["warnings"] == [] and ok["status"] == "done" and telemetry.imports(sid)[0]["status"] == "done"


def test_impossible_values_are_stored_as_missing_and_reported(company):
    """The audit's C10 file: load -50 kW, a state of charge of 150 %, PV -2 kW. Those cells are stored as missing and each kind is reported; a small
    negative PV reading at night (an inverter's standby draw) is kept as read without a warning."""
    sid = _site("Range")
    t = T0 + 2 * 3600  # 02:00, night
    rows = []
    for i in range(60):
        load = -50000 if i < 5 else 80000
        soc = 150 if i == 10 else -3 if i == 11 else 60
        pv = -2000 if i == 20 else -20
        rows.append((_local(t + 60 * i), 1, load, soc, pv))
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w", "battery_soc_bms_pct", "pv_power_w"), rows), 5, now=NOW)
    d = telemetry.raw(sid, t, t + 3600, ("load_power_w", "battery_soc_bms_pct", "pv_power_w"))
    assert np.all(np.isnan(d["load_power_w"][:5])) and np.all(d["load_power_w"][5:] == 80000)
    assert np.isnan(d["battery_soc_bms_pct"][10]) and np.isnan(d["battery_soc_bms_pct"][11]) and d["battery_soc_bms_pct"][12] == 60
    assert np.isnan(d["pv_power_w"][20]) and d["pv_power_w"][21] == -20
    day = _day(sid)
    assert day["max_soc"] == 60.0 and day["min_soc"] == 60.0
    assert day["load_kwh"] == pytest.approx(80 * 55 / 60, abs=0.01)
    w = " | ".join(res["warnings"])
    assert "5 load_power_w cells were below -500 W" in w, w
    assert "1 pv_power_w cell was below -500 W" in w, w
    assert "2 battery_soc_bms_pct cells were outside 0 to 100 %" in w, w


def test_a_state_of_charge_given_as_a_fraction_is_reported(company):
    sid = _site("Fraction")
    rows = [(_local(T0 + 8 * 3600 + 60 * i), 1, 80000, round(0.4 + 0.004 * i, 3)) for i in range(120)]
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w", "battery_soc_bms_pct"), rows), 5, now=NOW)
    assert any("battery_soc_bms_pct never goes above 1" in w for w in res["warnings"]), res["warnings"]


# ------------------------------------------------------------------------------------------------ frontend-states-formats-06 / flow-telemetry-plan-control-11

def test_solar_power_at_night_reports_a_probable_time_zone_slip(company):
    """A logger's local-time file (PV 06:30-17:30) read as UTC puts PV at 21:00 Karachi time; read as Karachi time it does not. The slip is reported, in
    the other direction too (a UTC file read as Karachi time puts PV before dawn)."""
    sid = _site("Zone")
    local_rows = [(_local(T0 + 60 * i), 1, 200000, _solar(T0 + 60 * i, 150000)) for i in range(1440)]
    body = _file(("ts", "grid_present", "load_power_w", "pv_power_w"), local_rows)
    wrong = telemetry.import_csv(sid, body, 0, now=NOW)
    assert any("Karachi time" in w and "zone" in w for w in wrong["warnings"]), wrong["warnings"]
    sid2 = _site("Zone right")
    right = telemetry.import_csv(sid2, body, 5, now=NOW)
    assert right["warnings"] == []
    utc_rows = [(datetime.fromtimestamp(T0 + 60 * i, timezone.utc).replace(tzinfo=None).isoformat(), 1, 200000, _solar(T0 + 60 * i, 150000))
                for i in range(1440)]
    sid3 = _site("Zone other way")
    other = telemetry.import_csv(sid3, _file(("ts", "grid_present", "load_power_w", "pv_power_w"), utc_rows), 5, now=NOW)
    assert any("zone" in w for w in other["warnings"]), other["warnings"]


# ------------------------------------------------------------------------------------------------ the route's answer

def test_the_upload_route_answers_the_new_fields(world):
    c = world.owner
    sid = c.post("/api/sites", json={"name": "Route"}).json()["id"]
    start = (int(datetime.now(timezone.utc).timestamp()) // 86400 - 2) * 86400
    rows = []
    for i in range(32):
        ts = datetime.fromtimestamp(start + 900 * (i // 2), timezone.utc).isoformat()
        rows.append((ts, 1, 250))  # every 15 minutes, twice, in kW
    up = c.post(f"/api/sites/{sid}/telemetry/upload", params={"tz_offset_h": 0, "file_name": "x.csv"},
                content=_file(("ts", "grid_present", "load_power_w"), rows), headers={"content-type": "text/csv"})
    assert up.status_code == 200, up.text
    body = up.json()
    assert (body["rows"], body["duplicates"], body["step_min"], body["minutes"], body["overlap"]) == (16, 16, 15, 240, 0)
    assert body["status"] == "done_with_warnings" and any("load_power_w" in w for w in body["warnings"])
    bad = c.post(f"/api/sites/{sid}/telemetry/upload", params={"tz_offset_h": 0},
                 content=_file(("ts", "grid_present", "load_power_w"), [(r[0], "x", 1) for r in rows]), headers={"content-type": "text/csv"})
    assert bad.status_code == 422 and "grid_present" in bad.json()["detail"]


# ================================================================================================ batch 2: genset litres, runs, fuel cost and cards

GEN = {"rated_kw": 200.0, "f_min": 0.3, "a": 0.25, "b": 0.03}


def _genset_site(name: str = "Genset", **g) -> str:
    sid = sites.create({"name": name}, "t")["id"]
    sites.put_asset(sid, "genset", {**GEN, **g}, "t")
    return sid


def _write(sid: str, t0: int, n: int, step: int = 60, **cols) -> None:
    """n stored rows from t0, `step` seconds apart; each column a value, or a function of the row's index (None: a blank cell)."""
    names = tuple(cols)
    rows = [(sid, t0 + i * step, "edge", *[cols[c](i) if callable(cols[c]) else cols[c] for c in names]) for i in range(n)]
    telemetry.write_rows(rows, ("site_id", "ts", "source_id") + names)


def _runs(sid: str, t0: int = T0, t1: int = T0 + 86400) -> dict:
    return telemetry.genset_runs(sid, t0, t1, sites.assets(sid)["genset"])


# ------------------------------------------------------------------------------------------------ pages-C-04: runs and outages in minutes

def test_a_blank_sample_inside_a_genset_run_does_not_end_it(company):
    """A 1-minute source with one blank row in a 2-hour run: one run of 2 hours (it was two runs, the blank read as off, and two starts)."""
    sid = _genset_site()
    t = T0 + 10 * 3600
    _write(sid, t, 180, grid_present=0, genset_running=lambda i: None if i == 30 else int(i < 120),
           genset_power_w=lambda i: None if i == 30 else (100000 if i < 120 else 0))
    runs = _runs(sid)["runs"]
    assert [(r["start"], r["end"], r["hours"]) for r in runs] == [(t, t + 7200, 2.0)]
    assert runs[0]["kwh"] == pytest.approx(200.0)  # the blank minute's power is the reading before it


@pytest.mark.parametrize("step", [5, 15])
def test_a_coarse_source_counts_its_genset_minutes_energy_and_load(company, step):
    """2 hours at 100 kW on a 200 kW set from a 5- or 15-minute source: 200 kWh and a load ratio of 0.5 (it was 40 or 13 kWh, 0.1 or 0.04: every run
    under the minimum load)."""
    sid = _genset_site(f"Coarse {step}")
    run = lambda i: 600 <= i * step < 720  # noqa: E731
    _write(sid, T0, 1440 // step, step * 60, grid_present=1, genset_running=lambda i: int(run(i)), genset_power_w=lambda i: 100000 if run(i) else 0)
    runs = _runs(sid)["runs"]
    assert [(r["start"], r["end"], r["hours"]) for r in runs] == [(T0 + 36000, T0 + 43200, 2.0)]
    assert runs[0]["kwh"] == pytest.approx(200.0) and runs[0]["load_ratio"] == pytest.approx(0.5) and runs[0]["hours_below_min"] == 0


def test_a_blank_grid_reading_does_not_split_an_outage_but_a_long_gap_does(company):
    sid = _site("Blank outage")
    t = T0 + 14 * 3600
    _write(sid, t - 600, 80, grid_present=lambda i: None if i == 40 else int(not 10 <= i < 70))
    assert [(o["start"], o["minutes"]) for o in telemetry.outages(sid, T0, T0 + 86400)] == [(t, 60)]
    # 20 minutes without any row inside an outage: unknown for longer than RUN_GAP_S, so two outages
    t2 = T0 + 18 * 3600
    _write(sid, t2, 20, grid_present=0)
    _write(sid, t2 + 40 * 60, 20, grid_present=0)
    _write(sid, t2 + 60 * 60, 5, grid_present=1)
    assert [(o["start"], o["minutes"]) for o in telemetry.outages(sid, T0 + 17 * 3600, T0 + 86400)] == [(t2, 20), (t2 + 2400, 20)]


@pytest.mark.parametrize("step", [5, 15])
def test_a_coarse_source_counts_outage_genset_minutes_and_unserved_energy(company, step):
    """A 60-minute outage carried by the genset with 6 kW unserved, from a 5- or 15-minute source: 60 genset minutes and 6 kWh unserved (it was 12 or
    4 minutes and 1.2 or 0.4 kWh)."""
    sid = _genset_site(f"Coarse outage {step}")
    down = lambda i: 14 * 60 <= i * step < 15 * 60  # noqa: E731
    _write(sid, T0, 1440 // step, step * 60, grid_present=lambda i: 0 if down(i) else 1, genset_running=lambda i: int(down(i)),
           genset_power_w=lambda i: 80000 if down(i) else 0, unserved_w=lambda i: 6000 if down(i) else 0)
    out = telemetry.outages(sid, T0, T0 + 86400)
    assert [(o["start"], o["minutes"], o["genset_minutes"]) for o in out] == [(T0 + 14 * 3600, 60, 60)]
    assert out[0]["unserved_kwh"] == pytest.approx(6.0)


# ------------------------------------------------------------------------------------------------ pages-C-03: litres metered or estimated, never 0

def _one_run(sid: str, start: int, minutes: int = 60, fuel: float | None = None, power: float | None = 100000.0, **extra) -> None:
    """A run of `minutes` from `start` between off minutes, during an outage; `fuel` litres a minute (None: no fuel meter), `power` watts (None: none)."""
    on = lambda i: 10 <= i < 10 + minutes  # noqa: E731
    cols = {"grid_present": 0, "genset_running": lambda i: int(on(i))}
    if power is not None:
        cols["genset_power_w"] = lambda i: power if on(i) else 0.0
    if fuel is not None:
        cols["genset_fuel_l"] = lambda i: fuel if on(i) else 0.0
    _write(sid, start - 600, minutes + 20, **cols, **extra)


def test_metered_litres_are_named_metered_as_on_the_savings_page(company):
    from gridlytics import verify
    sid = _genset_site("Metered")
    _one_run(sid, T0 + 10 * 3600, fuel=0.5)
    res = _runs(sid)
    r = res["runs"][0]
    assert (r["litres"], r["litres_metered"], r["litres_curve"], r["litres_source"]) == (30.0, 30.0, 0.0, "meter")
    used = verify.litres_used(sid, DAY, DAY)
    assert (used["litres"], used["litres_source"]) == (r["litres"], r["litres_source"])


def test_an_unmetered_set_s_litres_come_from_the_fuel_curve_not_zero(company):
    """No genset_fuel_l column: 60 minutes at 100 kW on a 200 kW set with a = 0.25 and b = 0.03 burn (0.25 x 100 + 0.03 x 200) = 31 L, from the curve
    (they were 0 L and Rs 0, badged estimated)."""
    from gridlytics import verify
    sid = _genset_site("Unmetered")
    _one_run(sid, T0 + 10 * 3600)
    r = _runs(sid)["runs"][0]
    assert (r["litres"], r["litres_metered"], r["litres_curve"], r["litres_source"]) == (31.0, 0.0, 31.0, "curve")
    used = verify.litres_used(sid, DAY, DAY)
    assert (used["litres"], used["litres_source"]) == (31.0, "curve")


def test_a_run_signal_alone_takes_its_power_from_the_site_s_balance(company):
    """Only genset_running: during an outage the set carries the load less PV and battery (150 kW here), so 43.5 L from the curve, as Savings reads it."""
    from gridlytics import verify
    sid = _genset_site("Flag only")
    _one_run(sid, T0 + 10 * 3600, power=None, load_power_w=150000, pv_power_w=0, battery_power_w=0)
    r = _runs(sid)["runs"][0]
    assert r["kwh"] == pytest.approx(150.0) and r["kwh_estimated"] is True
    assert (r["litres"], r["litres_source"]) == (43.5, "curve")
    assert verify.litres_used(sid, DAY, DAY)["litres"] == pytest.approx(43.5)


# ------------------------------------------------------------------------------------------------ the genset page's answer

def _pk_day(days_ago: int) -> date:
    return datetime.now(PKT).date() - timedelta(days=days_ago)


def _pk_ts(day: date, hour: int) -> int:
    return int(datetime(day.year, day.month, day.day, hour, tzinfo=PKT).timestamp())


def _route_site(c, name: str, **g) -> str:
    sid = c.post("/api/sites", json={"name": name}).json()["id"]
    assert c.put(f"/api/sites/{sid}/assets/genset", json={**GEN, **g}).status_code == 200
    return sid


def test_the_genset_page_names_its_litres_and_prices_each_run_at_its_own_day_s_price(world):
    """pages-C-03 and role-owner-07: a metered run (30 L) on a day whose diesel is Rs 300/L and an unmetered one (31 L from the curve) on a day whose
    diesel is Rs 400/L cost Rs 21,400, not 61 L at the newest price; the litres are named partly estimated; a viewer gets the litres, no rupees."""
    from gridlytics import tariffs
    c = world.owner
    sid = _route_site(c, "Priced")
    d1, d2 = _pk_day(2), _pk_day(1)
    with world.use():
        tariffs.ensure_shipped()
        tariffs.add_fuel_price("hsd", d1.isoformat(), 300.0, "t")
        tariffs.add_fuel_price("hsd", d2.isoformat(), 400.0, "t")
        _one_run(sid, _pk_ts(d1, 10), fuel=0.5)
        _one_run(sid, _pk_ts(d2, 10))
    page = c.get(f"/api/sites/{sid}/genset").json()
    k = page["kpis"]
    # the behaviour first: 31 L from the curve for the unmetered run (it was 0 L), and Rs 21,400 at each day's price (not every litre at Rs 400)
    assert (k["litres"], k["fuel_cost_rs"]) == (61.0, 21400.0)
    assert (k["litres"], k["litres_metered"], k["litres_curve"], k["litres_source"], k["estimated"]) == (61.0, 30.0, 31.0, "curve", True)
    runs = sorted(page["runs"], key=lambda r: r["start"])
    assert [(r["litres"], r["litres_source"], r["price_per_l"], r["fuel_cost_rs"]) for r in runs] == [(30.0, "meter", 300.0, 9000.0),
                                                                                                       (31.0, "curve", 400.0, 12400.0)]
    assert k["fuel_cost_rs"] == 21400.0 and k["kwh"] == pytest.approx(200.0) and k["fuel_rs_per_kwh"] == pytest.approx(107.0)
    assert page["fuel_price"]["range_per_l"] == [300.0, 400.0]
    v = world.role("viewer").get(f"/api/sites/{sid}/genset").json()
    assert v["kpis"]["litres"] == 61.0 and v["kpis"]["litres_source"] == "curve"
    assert not {"fuel_cost_rs", "fuel_rs_per_kwh"} & set(v["kpis"]) and "fuel_price" not in v
    assert all(not {"price_per_l", "fuel_cost_rs"} & set(r) for r in v["runs"])


def test_no_run_signal_is_unknown_and_a_live_signal_with_no_runs_is_zero(world):
    """pages-C-05: a source that sends no genset reading gives hours, starts and litres as unknown (null), not 0 h / 0 starts / 0 L; once the signal is
    read and says off, the zeros are facts."""
    c = world.owner
    sid = _route_site(c, "Silent")
    t = _pk_ts(_pk_day(1), 10)
    with world.use():
        _write(sid, t, 60, grid_present=1, load_power_w=50000)
    page = c.get(f"/api/sites/{sid}/genset").json()
    k = page["kpis"]
    assert (k["hours"], k["starts"], k["litres"], k["estimated"]) == (None, None, None, False)  # unknown, not 0 h, 0 starts and 0 L
    assert page["signal"] == {"present": False, "last_seen": None, "running": None}
    assert (k["hours"], k["starts"], k["litres"], k["kwh"], k["litres_source"], k["estimated"]) == (None, None, None, None, None, False)
    assert k["fuel_cost_rs"] is None and page["runs"] == [] and all(d["hours"] is None and d["litres"] is None for d in page["daily"])
    with world.use():
        _write(sid, t + 3600, 60, grid_present=1, genset_running=0)
    page = c.get(f"/api/sites/{sid}/genset").json()
    assert page["signal"] == {"present": True, "last_seen": t + 3600 + 59 * 60, "running": False}
    k = page["kpis"]
    assert (k["hours"], k["starts"], k["litres"], k["estimated"]) == (0.0, 0, 0.0, False)
    assert next(d for d in page["daily"] if d["day"] == _pk_day(1).isoformat())["hours"] == 0.0


def test_the_month_view_compares_with_the_month_before_and_counts_hours_below_the_minimum_load(world):
    """pages-C-15: GET /genset?month=YYYY-MM answers that month (to now) with the previous month's hours, starts and litres, a row per day, and the hours
    below the minimum load; a month that is not one, or has not started, is refused in one sentence."""
    c = world.owner
    sid = _route_site(c, "Monthly")
    d = _pk_day(1)
    before = (d.replace(day=1) - timedelta(days=1)).replace(day=15)
    with world.use():
        _one_run(sid, _pk_ts(d, 10), fuel=0.5)
        _one_run(sid, _pk_ts(before, 10), minutes=120, power=50000.0, fuel=0.2)  # a quarter of the rating: under f_min 0.3 for 2 hours
    m, pm = d.strftime("%Y-%m"), before.strftime("%Y-%m")
    page = c.get(f"/api/sites/{sid}/genset", params={"month": m}).json()
    assert page["period"]["kind"] == "month" and page["period"]["month"] == m
    assert (page["kpis"]["hours"], page["kpis"]["starts"], page["kpis"]["litres"]) == (1.0, 1, 30.0)
    assert page["previous"] == {"month": pm, "hours": 2.0, "starts": 1, "litres": 24.0, "kwh": 100.0}
    last_day = datetime.fromtimestamp(page["period"]["end"] - 1, PKT).date().isoformat()  # today, or the month's last day on the 1st of a month
    assert page["daily"][0]["day"] == d.replace(day=1).isoformat() and page["daily"][-1]["day"] == last_day
    assert next(x for x in page["daily"] if x["day"] == d.isoformat()) == {"day": d.isoformat(), "hours": 1.0, "litres": 30.0}
    last = c.get(f"/api/sites/{sid}/genset", params={"month": pm}).json()
    assert last["kpis"]["hours_below_min"] == 2.0 and last["runs"][0]["hours_below_min"] == 2.0
    days14 = c.get(f"/api/sites/{sid}/genset").json()
    assert days14["period"]["kind"] == "days" and days14["period"]["days"] == 14 and "previous" not in days14 and len(days14["daily"]) == 15
    for bad_month in ("2026-13", "November", (date.today() + timedelta(days=40)).strftime("%Y-%m")):
        r = c.get(f"/api/sites/{sid}/genset", params={"month": bad_month})
        assert r.status_code == 422 and r.json()["detail"].endswith("."), (bad_month, r.text)


# ------------------------------------------------------------------------------------------------ pages-C-16: measured points and the refit

def test_the_fuel_curve_shows_the_metered_runs_and_refits_from_them_or_from_data_sheet_points(world):
    c = world.owner
    sid = _route_site(c, "Refit")
    a, b, s = 0.22, 0.05, GEN["rated_kw"]
    loads = (0.4, 0.6, 0.8, 1.0)
    with world.use():
        for n, lr in enumerate(loads):
            _one_run(sid, _pk_ts(_pk_day(4 - n), 10), power=lr * s * 1000, fuel=(a * lr * s + b * s) / 60)
    page = c.get(f"/api/sites/{sid}/genset").json()
    pts = sorted((p["load_ratio"], p["litres_per_hour"]) for p in page["curve"]["measured"])
    assert pts == [pytest.approx((lr, a * lr * s + b * s), abs=0.02) for lr in loads]
    fit = c.post(f"/api/sites/{sid}/genset/curve/refit", json={}).json()
    assert fit["from"] == "meter" and fit["points_used"] == 4 and fit["applied"] is False
    assert fit["a"] == pytest.approx(a, abs=0.002) and fit["b"] == pytest.approx(b, abs=0.002)
    assert c.get(f"/api/sites/{sid}").json()["assets"]["genset"]["a"] == GEN["a"]  # a preview changes nothing
    # data-sheet points, applied by an operator
    sheet = [{"load_ratio": lr, "litres_per_hour": round(0.3 * lr * s + 0.02 * s, 2)} for lr in (0.25, 0.5, 0.75, 1.0)]
    for role in ("viewer", "analyst"):
        assert world.role(role).post(f"/api/sites/{sid}/genset/curve/refit", json={"points": sheet, "apply": True}).status_code == 403
    op = world.role("operator").post(f"/api/sites/{sid}/genset/curve/refit", json={"points": sheet, "apply": True})
    assert op.status_code == 200, op.text
    body = op.json()
    assert body["from"] == "data_sheet" and body["applied"] is True and body["a"] == pytest.approx(0.3, abs=1e-3)
    g = c.get(f"/api/sites/{sid}").json()["assets"]["genset"]
    assert (g["a"], g["b"]) == (body["a"], body["b"]) and g["rated_kw"] == s
    # refusals: one point, a point at no load, an unknown field, a site without metered runs
    for bad_body in ({"points": sheet[:1]}, {"points": [{"load_ratio": 0, "litres_per_hour": 5}] + sheet[1:]}, {"pts": sheet}):
        r = c.post(f"/api/sites/{sid}/genset/curve/refit", json=bad_body)
        assert r.status_code == 422, bad_body
    other = _route_site(c, "No meter")
    r = c.post(f"/api/sites/{other}/genset/curve/refit", json={})
    assert r.status_code == 422 and "metered runs" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ pages-A-06 (a duplicate of pages-C-03): the Overview

def test_the_overview_month_names_where_its_litres_came_from(world):
    """The Overview's 'Genset this month' carries litres_source as the Savings page does (it said "(estimated)" on metered litres), and its litres are
    the Savings page's (an unmetered set's are no longer 0)."""
    c = world.owner
    now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    month0 = _pk_ts(datetime.now(PKT).date().replace(day=1), 0)
    start = max(month0 + 600, now - 80 * 60)
    minutes = min(60, (now - start) // 60 - 15)
    if minutes < 5:
        pytest.skip("too close to the start of a month for a run this month")
    metered, unmetered = _route_site(c, "Metered overview"), _route_site(c, "Unmetered overview")
    with world.use():
        _one_run(metered, start, minutes=minutes, fuel=0.5)
        _one_run(unmetered, start, minutes=minutes)
    m = c.get(f"/api/sites/{metered}/now").json()["month"]
    assert (m["litres"], m["litres_source"]) == (round(0.5 * minutes, 1), "meter")
    u = c.get(f"/api/sites/{unmetered}/now").json()["month"]
    assert (u["litres"], u["litres_source"]) == (round(31.0 * minutes / 60, 1), "curve")


# ================================================================================================ batch 3: CSV mapping, refusals, data quality and series

# ------------------------------------------------------------------------------------------------ role-viewer-07: today's completeness so far

def test_today_s_completeness_counts_the_minutes_elapsed_so_far(company):
    """The audit's case: at 12:24 Karachi time every minute since midnight is present, and the quality strip said 51 % (red), the share of all 1,440
    minutes. Today is now counted up to QUALITY_GRACE_S ago and marked in progress, so a healthy feed is 100 %; a feed that stopped at 06:00 falls with
    the day; past days keep the whole day's share."""
    sid = _site("Quality today")
    today = date.fromisoformat(DAY)
    now = T0 + 12 * 3600 + 24 * 60
    yesterday = T0 - 86400
    _write(sid, yesterday, 1440 // 2, grid_present=1)  # half of yesterday
    _write(sid, T0, (now - T0) // 60, grid_present=1)  # every minute so far today
    for d in (today - timedelta(days=1), today):
        telemetry.aggregate_day(sid, d)
    assert _day(sid)["completeness"] < 0.6  # the stored day row: the share of the whole day
    q = {r["day"]: r for r in telemetry.quality_days(sid, today - timedelta(days=3), today, now)}
    assert q[DAY]["completeness"] == 1.0 and q[DAY]["in_progress"] is True
    assert q["2026-11-04"]["completeness"] == 0.5 and not q["2026-11-04"].get("in_progress")
    # a feed that stopped at 06:00: by 12:00 half of today's elapsed minutes are missing
    sid2 = _site("Stopped feed")
    _write(sid2, T0, 6 * 60, grid_present=1)
    telemetry.aggregate_day(sid2, today)
    q2 = {r["day"]: r for r in telemetry.quality_days(sid2, today, today, T0 + 12 * 3600 + telemetry.QUALITY_GRACE_S)}
    assert q2[DAY]["completeness"] == 0.5
    # a feed that has sent nothing yet today, after earlier days: today shows 0 % so far rather than no square at all
    sid3 = _site("Silent today")
    _write(sid3, yesterday, 60, grid_present=1)
    telemetry.aggregate_day(sid3, today - timedelta(days=1))
    q3 = {r["day"]: r for r in telemetry.quality_days(sid3, today - timedelta(days=1), today, now)}
    assert (q3[DAY]["completeness"], q3[DAY]["in_progress"]) == (0.0, True)
    # just after midnight, before the grace has passed, a silent feed is not yet counted against
    q4 = {r["day"]: r for r in telemetry.quality_days(sid3, today, today, T0 + 300)}
    assert DAY not in q4


def test_the_quality_route_counts_today_so_far(world, monkeypatch):
    """GET /telemetry/quality answers today's share over the minutes elapsed so far, with in_progress."""
    from gridlytics import routes_telemetry
    c = world.owner
    sid = c.post("/api/sites", json={"name": "Route quality"}).json()["id"]
    now = T0 + 12 * 3600 + 24 * 60
    monkeypatch.setattr(routes_telemetry, "now_ts", lambda: now)
    monkeypatch.setattr(routes_telemetry, "today", lambda: date.fromisoformat(DAY))
    with world.use():
        _write(sid, T0, (now - T0) // 60, grid_present=1)
        telemetry.aggregate_day(sid, date.fromisoformat(DAY))
    q = c.get(f"/api/sites/{sid}/telemetry/quality", params={"days": 14})
    assert q.status_code == 200, q.text
    last = q.json()["days"][-1]
    assert (last["day"], last["completeness"]) == (DAY, 1.0)  # it was the share of all 1,440 minutes, 52 %
    assert last["in_progress"] is True
    viewer = world.role("viewer").get(f"/api/sites/{sid}/telemetry/quality")
    assert viewer.status_code == 200 and viewer.json()["days"][-1]["completeness"] == 1.0


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-25: format hints, size

def test_epoch_seconds_and_milliseconds_are_read_as_times(company):
    """C6_epoch_ts.csv was refused ("No rows with a readable ts were found") although the edge path reads epoch seconds; both are read now, as UTC
    whatever zone is chosen (an epoch time names its own zone)."""
    t = T0 + 9 * 3600
    for scale, name in ((1, "seconds"), (1000, "milliseconds")):
        sid = _site(f"Epoch {name}")
        res = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), [((t + 60 * i) * scale, 1, 40000) for i in range(30)]), 5,
                                   now=NOW)
        assert (res["rows"], res["from_ts"], res["to_ts"]) == (30, t, t + 29 * 60), name


def test_day_first_dates_and_unreadable_times_are_refused_with_the_formats_read(company):
    """C3_ddmmyyyy_timestamps.csv: refused with the formats the import reads and the way to read day-first dates, not 'no readable ts'."""
    sid = _site("Formats")
    with pytest.raises(ValueError) as e:
        telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), [(f"06/11/2026 09:{i:02d}", 1, 40000) for i in range(30)]), 5,
                             now=NOW)
    msg = str(e.value)
    assert "06/11/2026 09:00" in msg and "2026-11-06T09:00" in msg and "epoch seconds" in msg and "day first" in msg, msg
    with pytest.raises(ValueError) as e:
        telemetry.import_csv(sid, _file(("ts", "grid_present"), [("yesterday", 1), ("today", 1)]), 5, now=NOW)
    msg = str(e.value)
    assert "'yesterday'" in msg and "ISO 8601" in msg and "epoch seconds" in msg, msg
    # the upload's window check (the import's own since performance-live-04) reads epoch times as times: a 1970 epoch row is refused, nothing stored
    with pytest.raises(ValueError, match="before 2015"):
        telemetry.import_csv(sid, _file(("ts", "grid_present"), [(60, 1), (T0, 1)]), 5, now=NOW)
    assert telemetry.latest(sid) is None


def test_a_file_over_the_upload_cap_is_told_the_importer_s_sentence(world):
    """A 26 MB upload answered a bare 413 "The request is too large", and import_csv's own "larger than 30 MB" could never be reached. One cap now, the
    API's body limit, with one sentence, through the route and from import_csv alike."""
    from gridlytics import api
    assert telemetry.UPLOAD_MAX_BYTES == api.MAX_BODY
    with world.use():
        sid = _site("Big")
        with pytest.raises(ValueError, match="larger than 25 MB"):
            telemetry.import_csv(sid, b"ts,grid_present\n" + b"x" * telemetry.UPLOAD_MAX_BYTES, 5, now=NOW)
    c = world.owner
    sid = c.post("/api/sites", json={"name": "Big route"}).json()["id"]
    big = b"ts,grid_present\n" + b"2026-11-05T00:00,1\n" * (telemetry.UPLOAD_MAX_BYTES // 19 + 2)
    r = c.post(f"/api/sites/{sid}/telemetry/upload", content=big, headers={"content-type": "text/csv"})
    assert r.status_code == 413 and r.json()["detail"] == telemetry.TOO_LARGE, r.text
    other = c.put(f"/api/sites/{sid}/outages/schedule", content=b"{" + b" " * api.MAX_BODY + b"}", headers={"content-type": "application/json"})
    assert other.status_code == 413 and other.json()["detail"] == "The request is too large"


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-12: the column mapping

VENDOR_HEAD = ("Timestamp", "Grid Status", "Load (kW)", "PV (kW)", "SOC (%)", "Battery Power (kW)")


def _vendor(t: int, n: int = 60, stamp=None) -> bytes:
    """C1_wrong_headers.csv: a vendor export in kW, grid status as On/Off, battery power with charge positive (the product's discharge is positive)."""
    stamp = stamp or _local
    return _file(VENDOR_HEAD, [(stamp(t + 60 * i), "On" if i % 10 else "Off", 22.5, 15.0, 70, 4.0) for i in range(n)])


def test_a_vendor_export_without_a_mapping_is_refused_with_its_columns_and_the_keys_expected(company):
    sid = _site("Vendor")
    with pytest.raises(ValueError) as e:
        telemetry.import_csv(sid, _vendor(T0 + 9 * 3600), 5, now=NOW)
    msg = str(e.value)
    assert "Timestamp, Grid Status, Load (kW)" in msg and "ts" in msg and "grid_present" in msg and "column mapping" in msg, msg
    with pytest.raises(ValueError) as e:  # a ts column but no grid signal: the columns found are named too
        telemetry.import_csv(sid, _file(("ts", "Grid Status", "load_power_w"), [(_local(T0), "On", 1000)]), 5, now=NOW)
    assert "Grid Status" in str(e.value) and "column mapping" in str(e.value), str(e.value)


def test_the_guess_reads_a_vendor_export_and_its_mapping_imports_the_units_and_signs(company):
    """The mapping step's guess names each column's key and unit from its header (with the first cells); confirmed, with the battery's sign reversed,
    it imports the vendor file in watts with the product's signs, On/Off read as the grid signal."""
    sid = _site("Vendor mapped")
    t = T0 + 9 * 3600
    body = _vendor(t)
    g = telemetry.guess_mapping(body.decode()[:4000])
    cols = {c["name"]: c for c in g["columns"]}
    assert {n: (c["key"], c["unit"]) for n, c in cols.items()} == {
        "Timestamp": ("ts", None), "Grid Status": ("grid_present", None), "Load (kW)": ("load_power_w", "kW"), "PV (kW)": ("pv_power_w", "kW"),
        "SOC (%)": ("battery_soc_bms_pct", "%"), "Battery Power (kW)": ("battery_power_w", "kW")}
    assert cols["Load (kW)"]["sample"][:1] == ["22.5"] and g["missing"] == [] and g["delimiter"] == "," and g["ts_format"] == "auto"
    assert any("inverter_mode_active" in n for n in g["notes"])
    mapping = {"columns": {c["name"]: {"key": c["key"], "unit": c["unit"], "reverse": c["name"].startswith("Battery")} for c in g["columns"]},
               "ts_format": g["ts_format"]}
    res = telemetry.import_csv(sid, body, 5, mapping=mapping, now=NOW)
    assert res["rows"] == 60 and res["status"] == "done", res
    d = telemetry.raw(sid, t, t + 3600, ("load_power_w", "pv_power_w", "battery_power_w", "battery_soc_bms_pct", "grid_present"))
    assert np.all(d["load_power_w"] == 22500) and np.all(d["pv_power_w"] == 15000) and np.all(d["battery_power_w"] == -4000)
    assert np.all(d["battery_soc_bms_pct"] == 70) and d["grid_present"][0] == 0 and d["grid_present"][1] == 1
    assert any("column mapping" in line for line in res["log"]), res["log"]


def test_a_state_of_charge_as_a_fraction_is_read_as_percent_through_the_mapping(company):
    sid = _site("Fraction")
    t = T0 + 9 * 3600
    body = _file(("ts", "grid_present", "soc"), [(_local(t + 60 * i), 1, 0.65) for i in range(60)])
    g = telemetry.guess_mapping(body.decode())
    assert {c["name"]: (c["key"], c["unit"]) for c in g["columns"]}["soc"] == ("battery_soc_bms_pct", "fraction")
    telemetry.import_csv(sid, body, 5, mapping={"columns": {"soc": {"key": "battery_soc_bms_pct", "unit": "fraction"}}}, now=NOW)
    assert np.all(telemetry.raw(sid, t, t + 3600, ("battery_soc_bms_pct",))["battery_soc_bms_pct"] == 65)


@pytest.mark.parametrize("fmt", ["dmy", "mdy"])
def test_day_first_and_month_first_dates_are_read_in_the_order_the_mapping_chooses(company, fmt):
    sid = _site(f"Dates {fmt}")
    t = T0 + 9 * 3600  # 5 Nov 2026, 09:00 Karachi time

    def stamp(ts: int) -> str:
        x = datetime.fromtimestamp(ts, PKT)
        return x.strftime("%d/%m/%Y %H:%M" if fmt == "dmy" else "%m/%d/%Y %I:%M %p")
    body = _file(("Time", "Grid", "Load"), [(stamp(t + 60 * i), 1, 40000) for i in range(30)])
    g = telemetry.guess_mapping(body.decode())
    assert g["ts_format"] == "dmy"  # 05/11 and 11/05 cannot be told apart: day first, as Pakistan writes dates, and said so
    assert g["ts_ambiguous"] is True
    res = telemetry.import_csv(sid, body, 5, mapping={"columns": {"Time": {"key": "ts"}, "Grid": {"key": "grid_present"}, "Load": {"key": "load_power_w"}},
                                                       "ts_format": fmt}, now=NOW)
    assert (res["rows"], res["from_ts"], res["to_ts"]) == (30, t, t + 29 * 60)
    assert {c["name"]: c["key"] for c in g["columns"]} == {"Time": "ts", "Grid": "grid_present", "Load": "load_power_w"}  # 1s: the signal
    if fmt == "dmy":
        later = _file(("Time", "Grid"), [("13/11/2026 09:00", 1)])
        assert (telemetry.guess_mapping(later.decode())["ts_format"], telemetry.guess_mapping(later.decode())["ts_ambiguous"]) == ("dmy", False)


def test_a_semicolon_file_with_decimal_commas_is_read(company):
    """C8_semicolon.csv: a European logger's export, ';' between cells and ',' before the decimals; refused before as having no ts column."""
    sid = _site("Semicolon")
    t = T0 + 9 * 3600
    body = "\n".join(["ts;grid_present;load_power_w;pv_power_w"] + [f"{_local(t + 60 * i)};1;40000,5;1500,25" for i in range(30)]).encode()
    res = telemetry.import_csv(sid, body, 5, now=NOW)
    assert res["rows"] == 30
    d = telemetry.raw(sid, t, t + 1800, ("load_power_w", "pv_power_w"))
    assert np.all(d["load_power_w"] == 40000.5) and np.all(d["pv_power_w"] == 1500.25)
    g = telemetry.guess_mapping(body.decode())
    assert (g["delimiter"], g["decimal"]) == (";", ",")
    # the window check splits the header the same way: a semicolon file dated 1971 is refused
    with pytest.raises(ValueError, match="before 2015"):
        telemetry.import_csv(_site("Semicolon 1971"), b"ts;grid_present\n1971-01-02T00:00:00;1\n", 5, now=NOW)


def test_two_columns_read_as_one_key_are_refused(company):
    sid = _site("Twice")
    body = _file(("load_power_w", "Load (kW)", "ts", "grid_present"), [(1000, 1, _local(T0), 1)])
    with pytest.raises(ValueError, match="load_power_w"):
        telemetry.import_csv(sid, body, 5, mapping={"columns": {"Load (kW)": {"key": "load_power_w", "unit": "kW"}}}, now=NOW)
    with pytest.raises(ValueError, match="unit"):
        telemetry.import_csv(sid, _vendor(T0), 5, mapping={"columns": {"Load (kW)": {"key": "load_power_w", "unit": "%"}}}, now=NOW)


def test_the_mapping_is_saved_per_site_and_an_upload_reads_with_it(world):
    """GET, guess and PUT /telemetry/mapping, and POST /telemetry/upload?mapping=saved: the operator saves the vendor file's mapping once and uploads
    with it; a viewer reads the mapping but cannot save one; a mapping naming an unknown key is one sentence; another site has none."""
    c = world.owner
    op, viewer = world.role("operator"), world.role("viewer")
    sid = c.post("/api/sites", json={"name": "Mapped"}).json()["id"]
    other = c.post("/api/sites", json={"name": "Unmapped"}).json()["id"]
    start = (int(datetime.now(timezone.utc).timestamp()) // 86400 - 2) * 86400
    body = _vendor(start, stamp=lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%d/%m/%Y %H:%M"))
    first = c.get(f"/api/sites/{sid}/telemetry/mapping")
    assert first.status_code == 200 and first.json()["mapping"] is None
    keys = {k["key"]: k for k in first.json()["keys"]}
    assert keys["load_power_w"]["units"] == ["W", "kW", "MW"] and keys["battery_power_w"]["signed"] and keys["ts"]["required"]
    up = op.post(f"/api/sites/{sid}/telemetry/upload", params={"tz_offset_h": 0}, content=body, headers={"content-type": "text/csv"})
    assert up.status_code == 422 and "Timestamp" in up.json()["detail"], up.text
    g = op.post(f"/api/sites/{sid}/telemetry/mapping/guess", json={"head": body.decode()[:2000]})
    assert g.status_code == 200, g.text
    guess = g.json()
    assert guess["ts_format"] == "dmy" and guess["saved"] is False
    mapping = {"columns": {x["name"]: {"key": x["key"], "unit": x["unit"], "reverse": x["name"].startswith("Battery")} for x in guess["columns"]},
               "ts_format": guess["ts_format"]}
    assert viewer.put(f"/api/sites/{sid}/telemetry/mapping", json=mapping).status_code == 403
    assert viewer.post(f"/api/sites/{sid}/telemetry/mapping/guess", json={"head": "ts"}).status_code == 403
    bad = op.put(f"/api/sites/{sid}/telemetry/mapping", json={"columns": {"Load": {"key": "load_kw"}}})
    assert bad.status_code == 422 and "load_kw" in bad.json()["detail"], bad.text
    assert op.put(f"/api/sites/{sid}/telemetry/mapping", json={"columns": {"A": {"key": "ts"}}, "extra": 1}).status_code == 422
    saved = op.put(f"/api/sites/{sid}/telemetry/mapping", json=mapping)
    assert saved.status_code == 200 and saved.json()["mapping"]["columns"]["Load (kW)"] == {"key": "load_power_w", "unit": "kW", "reverse": False}
    assert viewer.get(f"/api/sites/{sid}/telemetry/mapping").json()["mapping"] == saved.json()["mapping"]
    again = op.post(f"/api/sites/{sid}/telemetry/mapping/guess", json={"head": body.decode()[:2000]}).json()
    assert again["saved"] is True and {x["name"]: x["from"] for x in again["columns"]}["Battery Power (kW)"] == "saved"
    assert {x["name"]: x["reverse"] for x in again["columns"]}["Battery Power (kW)"] is True
    up = op.post(f"/api/sites/{sid}/telemetry/upload", params={"tz_offset_h": 0, "mapping": "saved"}, content=body,
                 headers={"content-type": "text/csv"})
    assert up.status_code == 200, up.text
    assert (up.json()["rows"], up.json()["from_ts"]) == (60, start)
    with world.use():
        d = telemetry.raw(sid, start, start + 3600, ("load_power_w", "battery_power_w"))
    assert np.all(d["load_power_w"] == 22500) and np.all(d["battery_power_w"] == -4000)
    none = op.post(f"/api/sites/{other}/telemetry/upload", params={"mapping": "saved"}, content=body, headers={"content-type": "text/csv"})
    assert none.status_code == 422 and "mapping" in none.json()["detail"], none.text
    assert op.post(f"/api/sites/{sid}/telemetry/upload", params={"mapping": "mine"}, content=body,
                   headers={"content-type": "text/csv"}).status_code == 422


# ================================================================================================ batch 4: the battery page

PACK = {"nameplate_kwh": 100.0, "p_ch_kw": 50.0, "p_dis_kw": 50.0}
#: 15 Nov 2026, 12:00 Karachi time: the month runs from 1 Nov
B_NOW = T0 + 10 * 86400 + 12 * 3600
NOV1 = int(datetime(2026, 11, 1, tzinfo=PKT).timestamp())
OCT20 = int(datetime(2026, 10, 20, tzinfo=PKT).timestamp())


def _battery_site(world, monkeypatch, name: str) -> str:
    from gridlytics import routes_telemetry
    monkeypatch.setattr(routes_telemetry, "now_ts", lambda: B_NOW)
    monkeypatch.setattr(routes_telemetry, "today", lambda: datetime.fromtimestamp(B_NOW, PKT).date())
    c = world.owner
    sid = c.post("/api/sites", json={"name": name}).json()["id"]
    assert c.put(f"/api/sites/{sid}/assets/battery", json=PACK).status_code == 200
    return sid


def test_the_battery_page_counts_this_month_and_the_battery_s_life_over_the_days_with_data(world, monkeypatch):
    """pages-C-13 and role-owner-38: the KPI row reads this Karachi month (R8 2.10): 100 kWh discharged over the two days of November that have readings
    is 1.0 cycle, 0.5 a day over 2.0 days with data (not 1.0 / 15 days, nor the rate under a "30 days" title); 90 minutes above 90 % charge and 35 °C
    are 1.5 h this month; with 50 kWh on 20 October the battery's life holds 1.5 cycles since then; the wear is this month's and money (a viewer gets
    the rest); the state of health says it is as entered, not estimated."""
    from gridlytics import optimise
    c = world.owner
    sid = _battery_site(world, monkeypatch, "Monthly battery")
    t = NOV1 + 9 * 86400  # 10 Nov, 00:00
    with world.use():
        _write(sid, OCT20, 60, battery_power_w=50000.0, battery_soc_bms_pct=60.0)
        telemetry.aggregate_day(sid, date(2026, 10, 20))
        _write(sid, t, 2 * 1440, grid_present=1, battery_power_w=lambda i: 50000.0 if i < 120 else 0.0,
               battery_soc_bms_pct=lambda i: 95.0 if 600 <= i < 690 else 50.0, battery_temp_c=lambda i: 40.0 if i < 1440 else 30.0)
        wear = optimise.wear_cost(sites.assets(sid)["battery"])
    page = c.get(f"/api/sites/{sid}/battery").json()
    m = page["month"]
    assert (m["month"], m["start"], m["end"]) == ("2026-11", NOV1, B_NOW)
    assert (m["days_with_data"], m["throughput_kwh"], m["cycles"], m["cycles_per_day"], m["hot_full_hours"]) == (2.0, 100.0, 1.0, 0.5, 1.5)
    assert m["wear_rs"] == round(100.0 * wear)
    assert page["lifetime"] == {"cycles": 1.5, "throughput_kwh": 150.0, "since": "2026-10-20"}
    assert page["battery"]["soh_source"] == "entered"
    v = world.role("viewer").get(f"/api/sites/{sid}/battery").json()
    assert "wear_rs" not in v["month"] and v["month"]["cycles"] == 1.0 and v["lifetime"]["cycles"] == 1.5


def test_a_month_without_battery_readings_or_temperature_says_unknown_not_zero(world, monkeypatch):
    """pages-C-13: before any battery reading this month the month's figures are unknown (null), and a source that sends no temperature gives no
    hot-and-full hours rather than 0 h; under a day of readings there is no daily rate yet (R8 2.10's "Needs 1 day")."""
    c = world.owner
    sid = _battery_site(world, monkeypatch, "Quiet battery")
    page = c.get(f"/api/sites/{sid}/battery").json()
    m = page["month"]
    assert (m["days_with_data"], m["throughput_kwh"], m["cycles"], m["cycles_per_day"], m["hot_full_hours"], m["wear_rs"]) == (0.0, None, None, None,
                                                                                                                               None, None)
    assert page["lifetime"] == {"cycles": None, "throughput_kwh": None, "since": None}
    with world.use():
        _write(sid, B_NOW - 6 * 3600, 6 * 60, battery_power_w=20000.0, battery_soc_bms_pct=96.0)
    m = c.get(f"/api/sites/{sid}/battery").json()["month"]
    assert (m["days_with_data"], m["throughput_kwh"], m["cycles"], m["cycles_per_day"], m["hot_full_hours"]) == (0.25, 120.0, 1.2, None, None)


def test_the_state_of_charge_names_its_source_and_the_chart_gets_the_outages(world, monkeypatch):
    """pages-C-14: a source that sends the BMS reading alone gives a state of charge read from the BMS (soc_source "bms"), not an estimate; once the
    estimate arrives it is "estimate"; the 48-hour series carries grid_present, so the chart can shade the outages, and the plan steps carry theirs."""
    c = world.owner
    sid = _battery_site(world, monkeypatch, "Sourced battery")
    with world.use():
        _write(sid, B_NOW - 3 * 3600, 120, battery_soc_bms_pct=55.0, grid_present=lambda i: 0 if 30 <= i < 60 else 1)
    from gridlytics import routes_telemetry
    step = {"t": B_NOW + 900, "soc_pct": 50.0, "reserve_pct": 20.0, "grid_present": 0.0, "cost_rs": 12.0}
    monkeypatch.setattr(routes_telemetry.plans, "current", lambda site_id: {"steps": [step]})
    page = c.get(f"/api/sites/{sid}/battery").json()
    assert (page["battery"]["soc_pct"], page["battery"]["soc_source"]) == (55.0, "bms")
    present = page["soc"]["series"]["grid_present"]
    assert 0 in present and 1 in present
    assert page["plan"] == [{"t": B_NOW + 900, "soc_pct": 50.0, "reserve_pct": 20.0, "grid_present": 0.0}]
    with world.use():
        _write(sid, B_NOW - 60 * 60, 30, battery_soc_bms_pct=56.0, battery_soc_est_pct=58.0)
    b = c.get(f"/api/sites/{sid}/battery").json()["battery"]
    assert (b["soc_pct"], b["soc_source"]) == (58.0, "estimate")


# ------------------------------------------------------------------------------------------------ performance-live-03: reads never take the write lock

def _traced(monkeypatch) -> list:
    """Every telemetry connection opened from here on, with the statements it runs (seen[i] = (connection, [statements]))."""
    seen = []
    real = telemetry._open

    def traced():
        con = real()
        sql: list[str] = []
        con.set_trace_callback(sql.append)
        seen.append((con, sql))
        return con
    monkeypatch.setattr(telemetry, "_open", traced)
    return seen


def test_telemetry_reads_take_a_read_transaction_and_only_writers_take_the_write_lock(company, monkeypatch):
    """Every reader of the telemetry file (the pages' series, the battery and genset figures, the export, the forecast issues, the outages' grid signal and
    the edge's sequence check) ran BEGIN IMMEDIATE, the write lock, so a company's readers and writers ran one at a time. A read now begins a deferred
    transaction; the writes (ingest, write_rows, the sequence, the forecast issues, a site's removal) still take the write lock."""
    from gridlytics import routes_edge, routes_telemetry
    sid = _genset_site("Reads")
    telemetry.ingest(sid, [{"ts": T0 + 60 * i, "grid_present": 1, "load_power_w": 1000.0, "genset_running": 0} for i in range(30)], "edge", now=NOW)
    telemetry.save_forecast(sid, "pv", T0, 1, "v", {"q50": [0.0]})
    seen = _traced(monkeypatch)
    readers = {
        "latest": lambda: telemetry.latest(sid), "first_ts": lambda: telemetry.first_ts(sid),
        "battery_totals": lambda: telemetry.battery_totals(sid, T0, T0 + 3600), "soc_reading": lambda: telemetry.soc_reading(sid, T0),
        "raw": lambda: telemetry.raw(sid, T0, T0 + 3600), "minute_grid": lambda: telemetry.minute_grid(sid, T0, T0 + 3600),
        "series": lambda: telemetry.series(sid, T0, T0 + 3600, ["load_power_w"]), "heatmap": lambda: telemetry.heatmap(sid, "load_power_w"),
        "quality_days": lambda: telemetry.quality_days(sid, date(2026, 11, 5), date(2026, 11, 5), T0 + 7200),
        "export_csv": lambda: telemetry.export_csv(sid, T0, T0 + 3600), "latest_forecast": lambda: telemetry.latest_forecast(sid, "pv"),
        "outages": lambda: telemetry.outages(sid, T0, T0 + 3600), "genset_runs": lambda: _runs(sid),
        "grid_signal": lambda: routes_telemetry._grid_signal(sid, T0 + 3600), "seq_last": lambda: routes_edge._seq_last(sid, "telemetry"),
    }
    for name, read in readers.items():
        seen.clear()
        read()
        sql = [s for _, run in seen for s in run]
        assert sql, name
        assert not any(s.strip().upper().startswith("BEGIN IMMEDIATE") for s in sql), (name, sql)
        assert any(s.strip().upper() == "BEGIN" for s in sql), (name, sql)
    writers = {
        "ingest": lambda: telemetry.ingest(sid, [{"ts": T0 + 3600, "grid_present": 1}], "edge", now=NOW),
        "write_rows": lambda: _write(sid, T0 + 7200, 1, grid_present=1), "next_seq": lambda: telemetry.next_seq(sid, "telemetry", 1),
        "save_forecast": lambda: telemetry.save_forecast(sid, "pv", T0 + 900, 1, "v", {"q50": [0.0]}),
        "delete_site": lambda: telemetry.delete_site("gone"),
    }
    for name, write in writers.items():
        seen.clear()
        write()
        assert any(s.strip().upper().startswith("BEGIN IMMEDIATE") for _, run in seen for s in run), name


def test_a_reader_does_not_wait_for_a_writer_and_sees_the_last_committed_minute(company):
    """The audit's three threads: a read 0.2 s after a writer took the file waited 1.2 s behind it. A writer now holds the file for up to 2 s while a
    reader reads the minute: the read answers at once, with the value committed before the writer began."""
    import contextvars
    import threading
    import time
    sid = _site("Waiting")
    telemetry.ingest(sid, [{"ts": T0, "grid_present": 1, "load_power_w": 1000.0}], "edge", now=NOW)
    entered, release = threading.Event(), threading.Event()

    def hold():
        with telemetry.connect() as con:
            con.execute("UPDATE minute SET load_power_w = 2 WHERE site_id = ?", (sid,))
            entered.set()
            release.wait(2.0)
    writer = threading.Thread(target=contextvars.copy_context().run, args=(hold,))
    writer.start()
    try:
        assert entered.wait(5.0)
        t = time.monotonic()
        row = telemetry.latest(sid)
        took = time.monotonic() - t
    finally:
        release.set()
        writer.join()
    assert took < 1.0, f"the read waited {took:.2f} s for the writer"
    assert row["load_power_w"] == 1000.0
    assert telemetry.latest(sid)["load_power_w"] == 2


def test_the_export_formats_its_csv_outside_any_transaction(company, monkeypatch):
    """export_csv formatted the whole file inside the lock (0.47 s for 32 days). It now reads the rows, closes its read, and formats them after."""
    import sqlite3
    sid = _site("Export")
    telemetry.ingest(sid, [{"ts": T0 + 60 * i, "grid_present": 1, "load_power_w": 1000.0 + i} for i in range(10)], "edge", now=NOW)
    seen = _traced(monkeypatch)
    open_while_formatting = []
    real_cell = telemetry._csv_cell

    def cell(v):
        for con, _ in seen:
            try:
                open_while_formatting.append(con.in_transaction)
            except sqlite3.ProgrammingError:  # closed: no transaction
                open_while_formatting.append(False)
        return real_cell(v)
    monkeypatch.setattr(telemetry, "_csv_cell", cell)
    text = telemetry.export_csv(sid, T0, T0 + 3600)
    assert text.count("\n") == 11 and "1009.0" in text
    assert open_while_formatting and not any(open_while_formatting)


# ------------------------------------------------------------------------------------------------ tests-and-perf-05: at most about 2,000 points

def test_a_series_answers_at_most_2000_points_at_the_finest_resolution_within_them(company):
    """performance-live-13: 2 days at 1 minute answered 2,880 points and 30 days at 15 minutes 2,881. Each range now gets the finest of 1m, 5m, 15m, 1h
    and 1d that keeps its buckets within SERIES_MAX_POINTS, counting a range that starts part-way through a bucket."""
    sid = _site("Dense")
    _write(sid, T0, 2 * 1440, load_power_w=1000.0)
    _write(sid, T0 + 2 * 86400, 28 * 96, step=900, load_power_w=2000.0)
    cases = {  # (span from T0 + 30 s, so every range starts part-way through a minute) -> the resolution
        86400: "1m", 2 * 86400: "5m", 7 * 86400: "15m", 14 * 86400: "15m", 30 * 86400: "1h", 90 * 86400: "1d", 400 * 86400: "1d"}
    for span, res in cases.items():
        s = telemetry.series(sid, T0 + 30, T0 + 30 + span, ["load_power_w"])
        assert s["resolution"] == res, (span // 86400, s["resolution"])
        assert len(s["ts"]) <= 2000, (span // 86400, len(s["ts"]))
    two = telemetry.series(sid, T0, T0 + 2 * 86400, ["load_power_w"])
    assert (two["resolution"], len(two["ts"]), two["ts"][1] - two["ts"][0]) == ("5m", 576, 300)
    assert set(two["series"]["load_power_w"]) == {1000.0}
    month = telemetry.series(sid, T0, T0 + 30 * 86400, ["load_power_w"])
    assert (month["resolution"], len(month["ts"])) == ("1h", 720)
    # an explicit resolution within the cap is kept
    assert telemetry.series(sid, T0, T0 + 86400, ["load_power_w"], "15m")["resolution"] == "15m"
    assert telemetry.SERIES_MAX_POINTS == 2000


def test_an_explicit_resolution_past_the_cap_is_coarsened_for_every_role(world):
    """tests-and-perf-05: a viewer asked for 400 days at 1 minute (about 576,000 buckets of every series, built in Python). The route answers the finest
    resolution within the cap, says which was asked and the cap, and refuses only a resolution it does not know."""
    c = world.owner
    sid = c.post("/api/sites", json={"name": "Capped"}).json()["id"]
    end = int(datetime.now(timezone.utc).timestamp())
    with world.use():
        _write(sid, end - 3 * 86400, 3 * 1440, load_power_w=1000.0, pv_power_w=500.0)
    viewer = world.role("viewer")
    for res, start, given in (("1m", end - 400 * 86400, "1d"), ("1m", end - 2 * 86400, "5m"), ("15m", end - 30 * 86400, "1h"), ("1m", end - 86400, "1m"),
                              ("auto", end - 3 * 86400, "5m")):
        r = viewer.get(f"/api/sites/{sid}/telemetry", params={"start": start, "end": end, "res": res, "names": "load_power_w,pv_power_w"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["ts"]) <= 2000 and body["resolution"] == given, (res, start, body["resolution"], len(body["ts"]))
        assert (body["resolution"], body["requested_resolution"], body["max_points"]) == (given, res, 2000), (res, start)
    r = viewer.get(f"/api/sites/{sid}/telemetry", params={"res": "10s"})
    assert r.status_code == 422 and "5m" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ performance-live-04: one streamed parse, day-sized writes

_LOGGER = ("ts", "grid_present", "grid_power_w", "grid_import_wh", "grid_export_wh", "grid_voltage_v", "pv_power_w", "load_power_w", "battery_power_w",
           "battery_voltage_v", "battery_current_a", "battery_temp_c", "battery_soc_bms_pct", "genset_running", "genset_power_w", "genset_fuel_l",
           "inverter_mode_active")


def _logger_file(t0: int, n: int) -> bytes:
    """A 1-minute logger's export of n rows from t0 (Karachi time, no offset), every key a CSV may carry, about 100 bytes a row; PV from 06:00 to
    18:00."""
    def pv(t: int) -> float:
        return 15000.25 if 6 <= datetime.fromtimestamp(t, PKT).hour < 18 else 0
    return _file(_LOGGER, [(_local(t), 1, 1234.5, 20.6, 0, 231.2, pv(t), 40000.5, -2000.5, 51.2, -40.1, 31.5, 65.5, 0, 0, 0, "self_use")
                           for t in range(t0, t0 + 60 * n, 60)])


def test_an_upload_is_parsed_once(world, monkeypatch):
    """The route parsed every row for its date window, then import_csv parsed them all again. Each row's time is read once now: the window is the
    import's own check (before anything is stored), and the route reads only the header, for a column named twice."""
    c = world.owner
    sid = c.post("/api/sites", json={"name": "Parsed once"}).json()["id"]
    start = (int(datetime.now(timezone.utc).timestamp()) // 86400 - 2) * 86400
    calls = []
    real = telemetry.csv_time
    monkeypatch.setattr(telemetry, "csv_time", lambda *a, **k: calls.append(1) or real(*a, **k))
    body = _file(("ts", "grid_present", "load_power_w"), [(datetime.fromtimestamp(start + 60 * i, timezone.utc).isoformat(), 1, 40000) for i in range(90)])
    r = c.post(f"/api/sites/{sid}/telemetry/upload", content=body, headers={"content-type": "text/csv"}, params={"tz_offset_h": 0})
    assert r.status_code == 200 and r.json()["rows"] == 90, r.text
    assert len(calls) == 90
    # the window is still judged before anything is stored, and a column named twice is still refused by the route
    old = _file(("ts", "grid_present"), [("1971-01-02T00:00:00", 1), (datetime.fromtimestamp(start, timezone.utc).isoformat(), 1)])
    r = c.post(f"/api/sites/{sid}/telemetry/upload", content=old, headers={"content-type": "text/csv"})
    assert r.status_code == 422 and "before 2015" in r.json()["detail"], r.text
    r = c.post(f"/api/sites/{sid}/telemetry/upload", content=b"ts;grid_present;ts\n2026-11-01T00:00:00;1;2026-11-01T00:00:00\n",
               headers={"content-type": "text/csv"})
    assert r.status_code == 422 and "more than once" in r.json()["detail"], r.text
    r = c.post(f"/api/sites/{sid}/telemetry/upload", content=body, headers={"content-type": "text/csv"}, params={"tz_offset_h": 30})
    assert r.status_code == 422 and "between -23 and 23" in r.json()["detail"], r.text
    # a multipart form's file part is cut out once, exactly, with its name, beside another field
    from gridlytics import routes_telemetry
    part, name = routes_telemetry._file_part(b"--xx\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nmill\r\n--xx\r\nContent-Disposition: "
                                             b"form-data; name=\"file\"; filename=\"a.csv\"\r\n\r\nts,grid_present\r\n1,1\r\n\r\n--xx--\r\n", b"xx")
    assert (part, name) == (b"ts,grid_present\r\n1,1\r\n", "a.csv")
    assert routes_telemetry._file_part(b"--xx\r\nContent-Disposition: form-data; name=\"note\"\r\n\r\nmill\r\n--xx--\r\n", b"xx") == (b"", None)
    r = c.post(f"/api/sites/{sid}/telemetry/upload", files={"file": ("logger.csv", body, "text/csv")}, data={"note": "mill"})
    assert r.status_code == 200 and (r.json()["rows"], r.json()["overlap"], r.json()["file_name"]) == (90, 90, "logger.csv"), r.text


def test_an_upload_s_memory_is_a_small_multiple_of_the_file(company):
    """The audit's 23.5 MB file raised the API's memory by 544 MB (23 times the file): the text decoded twice, a dict per row, then a tuple per row for
    one write. The rows are now read in one streamed pass into a column of numbers per key, so the import's peak allocation stays within six times the
    file (it was 24 times)."""
    import tracemalloc
    sid = _site("Memory")
    body = _logger_file(T0, 20000)
    tracemalloc.start()
    try:
        res = telemetry.import_csv(sid, body, 5, now=T0 + 20 * 86400)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 6 * len(body), f"peak {peak / 1e6:.1f} MB for a {len(body) / 1e6:.1f} MB file ({peak / len(body):.1f} times)"
    assert res["rows"] == 20000 and res["minutes"] == 20000 and res["status"] == "done", res["warnings"]
    d = telemetry.raw(sid, T0, T0 + 3600, ("load_power_w", "grid_import_wh", "inverter_mode_active"))
    assert np.all(d["load_power_w"] == 40000.5) and np.all(d["grid_import_wh"] == 20.6) and d["inverter_mode_active"][0] == "self_use"


def test_an_upload_is_written_one_karachi_day_per_transaction(company, monkeypatch):
    """The whole file went in one executemany under one BEGIN IMMEDIATE, so every reader and edge batch of the company waited for all of it. Each
    Karachi day's minutes are now one write transaction, at most 1,440 rows, the day aggregated after it."""
    sid = _site("Days")
    seen = _traced(monkeypatch)
    res = telemetry.import_csv(sid, _logger_file(T0 + 12 * 3600, 2 * 1440), 5, now=NOW)  # noon on 5 Nov to noon on 7 Nov: three Karachi days
    writes = [sum(1 for x in run if x.startswith("INSERT")) for _, run in seen if any(x.startswith("BEGIN IMMEDIATE") for x in run)]
    writes = [n for n in writes if n]
    assert writes == [720, 1440, 720], writes
    assert res["minutes"] == 2 * 1440
    for day, n in (("2026-11-05", 720), ("2026-11-06", 1440), ("2026-11-07", 720)):
        assert _day(sid, day)["completeness"] == round(n / 1440, 4), day


# ------------------------------------------------------------------------------------------------ performance-live-06: forecast issues' retention

def _issue_rows(sid: str, kind: str = "pv") -> list[int]:
    # a plain read of the file (telemetry._open, as old as the table), so the test fails without the fix on what it keeps, not on a missing helper
    con = telemetry._open()
    try:
        return [r[0] for r in con.execute("SELECT belief_time FROM forecast_issues WHERE site_id = ? AND kind = ? ORDER BY belief_time", (sid, kind))]
    finally:
        con.close()


def test_forecast_issues_are_kept_hourly_for_35_days_then_only_each_day_s_day_ahead_issue(company):
    """Every hourly issue was kept forever, about 240 MB a site-year (four times the minute rows). Now: every issue for 3 days, the hourly ones for 35
    days (the stage-1 bands read 16 days, the night's scoring 3), then only each Karachi day's day-ahead issue, the latest within 6 hours before its
    midnight, which is what the day's scoring reads; a day without one keeps nothing. Another site's and another kind's issues are untouched."""
    sid, other = "retained", "other"
    newest = T0 + 45 * 86400  # a Karachi midnight
    first = T0
    quarters = list(range(first, newest, 900))
    body = json.dumps({"q50": [1.0] * 192})
    gone_midnight = T0 + 2 * 86400  # 7 Nov: its midnight issue never ran, so its day-ahead issue is the one an hour before
    dark_day = T0 + 4 * 86400  # 9 Nov: no issue in the 6 hours before its midnight
    with telemetry.connect() as con:
        con.executemany("INSERT INTO forecast_issues (site_id, kind, belief_time, stage, model_version, body) VALUES (?,?,?,?,?,?)",
                        [(s_, k, bt, 1, "v", body) for s_ in (sid, other) for k in ("pv", "load") for bt in quarters
                         if bt != gone_midnight and not dark_day - 6 * 3600 <= bt <= dark_day])
    telemetry.save_forecast(sid, "pv", newest, 1, "v", {"q50": [1.0] * 192})
    kept = _issue_rows(sid)
    recent = [bt for bt in kept if bt >= newest - 3 * 86400]
    hourly = [bt for bt in kept if newest - 35 * 86400 <= bt < newest - 3 * 86400]
    old = [bt for bt in kept if bt < newest - 35 * 86400]
    assert recent == [bt for bt in quarters if bt >= newest - 3 * 86400] + [newest]
    assert hourly == [bt for bt in quarters if newest - 35 * 86400 <= bt < newest - 3 * 86400 and bt % 3600 == 0]
    midnights = [d for d in range(T0, newest - 35 * 86400, 86400)]
    assert old == sorted([d for d in midnights if d not in (gone_midnight, dark_day)] + [gone_midnight - 3600]), [
        datetime.fromtimestamp(t, PKT).isoformat() for t in old]
    # the day's scoring finds its day-ahead issue still (forecast.score_day: the latest at or before midnight, at most 6 hours before it)
    assert telemetry.latest_forecast(sid, "pv", T0 + 86400)["belief_time"] == T0 + 86400
    assert telemetry.latest_forecast(sid, "pv", gone_midnight)["belief_time"] == gone_midnight - 3600
    assert dark_day - telemetry.latest_forecast(sid, "pv", dark_day)["belief_time"] > 6 * 3600
    assert len(kept) < len(quarters) / 3
    # another kind and another site are pruned by their own saves, not this one
    assert len(_issue_rows(sid, "load")) == len(_issue_rows(other, "pv")) == len(quarters) - 1 - 25
    assert (telemetry.ISSUE_ALL_DAYS, telemetry.ISSUE_HOURLY_DAYS) == (3, 35)
