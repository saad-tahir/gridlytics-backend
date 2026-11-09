"""Round-7 audit tests of the telemetry, genset and battery package (telemetry.py, routes_telemetry.py).

Batch 1, CSV import integrity: an upload that overlaps stored minutes merges per column (flow-telemetry-plan-control-01); a 5- or 15-minute file is
stored as the intervals it covers (-10); implausible values are reported per file, and impossible ones stored as missing (-13); a grid_present column
that cannot be read is refused or reported (-14); duplicate times are counted once and reported (-24); a probable time-zone slip is reported
(frontend-states-formats-06 and flow-telemetry-plan-control-11, the backend's half).

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
    return types.SimpleNamespace(db=db, owner=owner, use=lambda: store.use_company(db))


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
