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

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own."""
from __future__ import annotations

import io
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
    assert first["overlap"] == 0 and first["minutes"] == 60
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
    assert (res["rows"], res["step_min"], res["minutes"], res["gaps"]) == (n, step, 1440, 0)
    assert any(f"Rows are {step} minutes apart" in line for line in res["log"]), res["log"]
    day = _day(sid)
    assert day["load_kwh"] == pytest.approx(4800.0)
    assert day["import_kwh"] == pytest.approx(200 * 23)
    assert day["peak_kw"] == pytest.approx(200.0)
    assert day["outage_minutes"] == 60
    assert day["completeness"] == 1.0
    out = telemetry.outages(sid, T0, T0 + 86400)
    assert [(o["start"], o["minutes"]) for o in out] == [(T0 + 14 * 3600, 60)]


def test_a_missing_row_in_a_coarse_file_is_a_gap_and_its_minutes_stay_missing(company):
    sid = _site("Coarse gap")
    rows = [(_local(T0 + i * 900), 1, 100000) for i in range(96) if i != 40]
    res = telemetry.import_csv(sid, _file(("ts", "grid_present", "load_power_w"), rows), 5, now=NOW)
    assert (res["rows"], res["step_min"], res["minutes"], res["gaps"]) == (95, 15, 1425, 1)
    assert _day(sid)["completeness"] == pytest.approx(1425 / 1440, abs=1e-4)
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
    w = " | ".join(res["warnings"])
    assert "5 load_power_w cells were below -500 W" in w, w
    assert "1 pv_power_w cell was below -500 W" in w, w
    assert "2 battery_soc_bms_pct cells were outside 0 to 100 %" in w, w
    d = telemetry.raw(sid, t, t + 3600, ("load_power_w", "battery_soc_bms_pct", "pv_power_w"))
    assert np.all(np.isnan(d["load_power_w"][:5])) and np.all(d["load_power_w"][5:] == 80000)
    assert np.isnan(d["battery_soc_bms_pct"][10]) and np.isnan(d["battery_soc_bms_pct"][11]) and d["battery_soc_bms_pct"][12] == 60
    assert np.isnan(d["pv_power_w"][20]) and d["pv_power_w"][21] == -20
    day = _day(sid)
    assert day["max_soc"] == 60.0 and day["min_soc"] == 60.0
    assert day["load_kwh"] == pytest.approx(80 * 55 / 60, abs=0.01)


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
    assert page["signal"] == {"present": False, "last_seen": None, "running": None}
    k = page["kpis"]
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
    assert page["daily"][0]["day"] == d.replace(day=1).isoformat() and page["daily"][-1]["day"] == datetime.now(PKT).date().isoformat()
    assert next(x for x in page["daily"] if x["day"] == d.isoformat()) == {"day": d.isoformat(), "hours": 1.0, "litres": 30.0}
    last = c.get(f"/api/sites/{sid}/genset", params={"month": pm}).json()
    assert last["kpis"]["hours_below_min"] == 2.0 and last["runs"][0]["hours_below_min"] == 2.0
    days14 = c.get(f"/api/sites/{sid}/genset").json()
    assert days14["period"]["kind"] == "days" and days14["period"]["days"] == 14 and "previous" not in days14 and len(days14["daily"]) == 15
    for bad_month in ("2026-13", "November", (date.today() + timedelta(days=40)).strftime("%Y-%m")):
        r = c.get(f"/api/sites/{sid}/genset", params={"month": bad_month})
        assert r.status_code == 422 and r.json()["detail"].endswith("."), (bad_month, r.text)


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
