"""The company's telemetry file (plan Part 3.3): 1-minute rows per site, forecast issues, and the readers the engine and the pages use.

Signs as OpenEMS (battery discharge positive, charge negative, grid import positive); names as SunSpec where one exists. Timestamps are epoch seconds UTC
of each minute's start. Blanks are missing, never zero; a missing genset signal is unknown, not off.
"""
from __future__ import annotations

import contextvars
import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import time
from array import array
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import warnings

import numpy as np

warnings.filterwarnings("ignore", category=RuntimeWarning, message="(Mean of empty slice|All-NaN slice)")

from . import store

PKT = timezone(timedelta(hours=5))
SCHEMA_VERSION = 1

COLUMNS = ("site_id", "ts", "source_id", "grid_present", "grid_power_w", "grid_import_wh", "grid_export_wh", "pv_power_w", "load_power_w",
           "battery_power_w", "battery_soc_est_pct", "battery_soc_bms_pct", "battery_temp_c", "genset_running", "genset_power_w", "genset_fuel_l",
           "inverter_mode_active", "unserved_w", "curtail_w")
EXTRA = ("grid_voltage_v", "grid_frequency_hz", "battery_voltage_v", "battery_current_a", "battery_soc_sigma_pct", "battery_soh_pct",
         "export_limit_active_w", "censored", "quality_flags")
ALL = COLUMNS + EXTRA
NUMERIC = tuple(c for c in ALL if c not in ("site_id", "ts", "source_id", "inverter_mode_active", "quality_flags"))
#: the columns a CSV may carry (plan Part 7.5); anything else is ignored with a note
CSV_COLUMNS = ("ts", "grid_present", "grid_power_w", "grid_import_wh", "grid_export_wh", "grid_voltage_v", "pv_power_w", "load_power_w",
               "battery_power_w", "battery_voltage_v", "battery_current_a", "battery_temp_c", "battery_soc_bms_pct", "genset_running", "genset_power_w",
               "genset_fuel_l", "inverter_mode_active", "quality_flags")
#: a column mapping may also read a column as the time of day (round 7 sweeps, telemetry review): many loggers write the date and the time in two columns,
#: so the column read as ts holds the date, this one the time, and the import joins them ("06/11/2026" and "09:00" read as "06/11/2026 09:00")
TS_TIME = "ts_time"
MAPPING_KEYS = CSV_COLUMNS + (TS_TIME,)

_DDL = [
    "CREATE TABLE IF NOT EXISTS minute (" + ", ".join(
        ["site_id TEXT NOT NULL", "ts INTEGER NOT NULL", "source_id TEXT NOT NULL DEFAULT ''"]
        + [f"{c} {'TEXT' if c in ('inverter_mode_active', 'quality_flags') else 'REAL'}" for c in ALL[3:]]
        + ["PRIMARY KEY (site_id, ts)"]) + ") WITHOUT ROWID",
    """CREATE TABLE IF NOT EXISTS forecast_issues (site_id TEXT NOT NULL, kind TEXT NOT NULL, belief_time INTEGER NOT NULL, stage INTEGER NOT NULL,
        model_version TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (site_id, kind, belief_time))""",
    "CREATE TABLE IF NOT EXISTS seq (site_id TEXT NOT NULL, stream TEXT NOT NULL, last INTEGER NOT NULL, PRIMARY KEY (site_id, stream))",
]


#: a new file's first set-up (WAL and the tables) runs under this process lock, as store._connect does: two first opens at once would otherwise race on
#: 'PRAGMA journal_mode = WAL', which SQLite answers 'database is locked' without waiting on the busy timeout
_open_lock = threading.Lock()
#: the WAL switch is retried for up to this long when another process holds the new file (SQLITE_BUSY is not waited on for a journal-mode change)
_WAL_RETRY_S = 10.0


def _set_wal(con: sqlite3.Connection) -> None:
    deadline = time.monotonic() + _WAL_RETRY_S
    while True:
        try:
            con.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def _open() -> sqlite3.Connection:
    path = store.telemetry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 30000")
        if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            with _open_lock:
                if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:  # another thread may have set the file up meanwhile
                    _set_wal(con)
                    con.execute("BEGIN IMMEDIATE")
                    try:
                        for ddl in _DDL:
                            con.execute(ddl)
                        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                        con.execute("COMMIT")
                    except BaseException:
                        if con.in_transaction:
                            con.execute("ROLLBACK")
                        raise
    except BaseException:
        con.close()
        raise
    return con


@contextmanager
def connect():
    """A write to the telemetry file: BEGIN IMMEDIATE, the file's one write lock, taken at the start so a read-then-write (the sequence check) never
    fails half-way. Reads use reading()."""
    con = _open()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


@contextmanager
def reading():
    """A read of the telemetry file: a deferred transaction, so the statements inside it see one snapshot of the file and, in WAL mode, never take the
    write lock, never wait for a writer and never make one wait. Every read took BEGIN IMMEDIATE before (audit R7 performance-live-03), so a company's
    page reads, ticks, CSV imports and edge batches ran one at a time behind one lock. Nothing inside it may write: a write belongs in connect()."""
    con = _open()
    try:
        con.execute("BEGIN")
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


# ------------------------------------------------------------------------------------------------ writing

def _insert_sql(columns: tuple, merge: bool) -> str:
    """The minute rows' write. Without `merge` a row replaces the stored minute whole (the twin's own rows); with it, a stored minute keeps every column
    the new row leaves missing (None), so an upload or a batch that carries fewer columns never erases the others (audit R7, overlap)."""
    base = f"INSERT INTO minute ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
    if not merge:
        return base.replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)
    sets = ", ".join(f"{c} = excluded.{c}" if c == "source_id" else f"{c} = COALESCE(excluded.{c}, minute.{c})"
                     for c in columns if c not in ("site_id", "ts"))
    return f"{base} ON CONFLICT(site_id, ts) DO UPDATE SET {sets}"


def write_rows(rows: list[tuple], columns: tuple = COLUMNS, merge: bool = False) -> int:
    if not rows:
        return 0
    with connect() as con:
        con.executemany(_insert_sql(columns, merge), rows)
    return len(rows)


_VOLT = ALL.index("grid_voltage_v")
#: the largest magnitude a minute cell may hold: a billion (a gigawatt, a gigawatt-hour in one minute, a billion volts or litres) is far past any
#: site this product serves, and a finite but absurd reading (1e308 from a crafted batch) would otherwise overflow a day's sums to Infinity
CELL_MAX = 1e9


def _num(v) -> tuple[float | None, bool]:
    """A numeric cell as (value, unreadable): blanks are missing (None); text that is not a number, NaN, infinities and magnitudes over CELL_MAX
    are missing too, and counted, so one bad cell neither refuses a file nor stores a value the pages cannot render."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None, False
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None, True
    return (x, False) if math.isfinite(x) and abs(x) <= CELL_MAX else (None, True)


#: a stored row's time lies between EARLIEST_TS and a day after now: a row dated 1970 or 9999 is a logger with an unset clock (or a crafted batch), and a
#: number past SQLite's 64-bit integer would raise on the write (plan Part 2.8: telemetry is untrusted input, read with size and time caps)
EARLIEST_TS = int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp())
AHEAD_S = 86400
#: one CSV upload spans at most this many days: the import aggregates every day it fills
UPLOAD_MAX_DAYS = 400
#: the words loggers write for an on/off signal (grid_present, genset_running), read as 1 and 0 in a CSV
FLAG_WORDS = {"on": "1", "off": "0", "true": "1", "false": "0", "yes": "1", "no": "0"}
#: a CSV's rows are at most this many minutes apart (an hourly logger); rows further apart are refused, as no minute between them can be filled
STEP_MAX_MIN = 60
#: one CSV upload carries at most this many bytes: the API's body limit (api.MAX_BODY), which answers a larger upload with TOO_LARGE too, so the page, the
#: API and the importer give one limit in one sentence (flow-telemetry-plan-control-25)
UPLOAD_MAX_BYTES = 25_000_000
TOO_LARGE = "The file is larger than 25 MB, the most one upload can carry; split it by month"
#: the ways a CSV's ts column is read: "auto" reads ISO 8601 (a time without an offset is in the chosen zone) and epoch seconds or milliseconds (UTC, as
#: on the edge path); "dmy" and "mdy" read day-first and month-first dates such as 06/11/2026 09:00, which the column mapping chooses between, as no
#: reader can tell 06/11 from 11/06
TS_FORMATS = ("auto", "dmy", "mdy")
_SLASHED = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*([AaPp][Mm])?)?$")
#: a year-first day with a time of day as a logger writes it beside it: an hour of one digit or a 12-hour clock ("2026-11-06 9:00 AM"), which ISO 8601
#: does not read (round 7 sweeps: a date and a time joined from two columns)
_ISO_DAY_TIME = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*([AaPp][Mm])?$")
#: a cell that holds a day only, and one that holds a time of day only: the two halves of a time the mapping step joins
_DAY_ONLY = re.compile(r"^(\d{1,2}[/.-]\d{1,2}[/.-]\d{4}|\d{4}-\d{1,2}-\d{1,2})$")
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\s*([AaPp][Mm])?$")
_NUMBER = re.compile(r"^\d+(\.\d+)?$")
#: an epoch number at or above this is in milliseconds (1e11 seconds is the year 5138)
_EPOCH_MS = 1e11
_FORMATS_READ = "times must be ISO 8601, e.g. 2026-11-06T09:00 or 2026-11-06 09:00, or epoch seconds"


def csv_time(cell, tz_offset_h: float = 0.0, fmt: str = "auto") -> int | None:
    """A CSV ts cell as epoch seconds UTC, or None when it is not a time in `fmt` (see TS_FORMATS); a time past the calendar's edge raises OverflowError,
    as the callers count it apart. An epoch number reads as UTC whatever the zone chosen; an ISO time with its own offset reads as written."""
    text = str(cell if cell is not None else "").strip()
    if not text:
        return None
    if _NUMBER.match(text):
        x = float(text)
        return int(x / 1000) if x >= _EPOCH_MS else int(x)
    tz = timezone(timedelta(hours=tz_offset_h))
    m = _SLASHED.match(text) if fmt in ("dmy", "mdy") else None
    iso = None if m else _ISO_DAY_TIME.match(text)
    if m or iso:
        if m:
            a, b, year, hh, mm, ss, half = m.groups()
            day, month = (int(a), int(b)) if fmt == "dmy" else (int(b), int(a))
        else:
            year, mo, d, hh, mm, ss, half = iso.groups()
            day, month = int(d), int(mo)
        hour = int(hh or 0)
        if half:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if half.lower() == "pm" else 0)
        try:
            return int(datetime(int(year), month, day, hour, int(mm or 0), int(ss or 0), tzinfo=tz).timestamp())
        except ValueError:
            return None
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((t if t.tzinfo else t.replace(tzinfo=tz)).timestamp())


def _no_times(first: str | None, fmt: str) -> str:
    """The refusal of a file in which no row's time can be read, naming the first cell and the formats read (flow-telemetry-plan-control-25)."""
    if not first:
        return f"No row has a time in its ts column: {_FORMATS_READ}"
    if fmt == "auto" and _SLASHED.match(first):
        return (f"No row's time could be read: the file writes times as {first} (a day and a month in an order the import cannot tell); write them as "
                f"ISO 8601, e.g. 2026-11-06T09:00, or as epoch seconds, or choose day first or month first in the column mapping")
    if fmt in ("dmy", "mdy"):
        order = "day first, e.g. 06/11/2026 09:00" if fmt == "dmy" else "month first, e.g. 11/06/2026 09:00"
        return f"No row's time could be read (the first ts is {first!r}): the mapping reads dates {order}; {_FORMATS_READ}"
    return f"No row's time could be read (the first ts is {first!r}): {_FORMATS_READ}"


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _epoch(v) -> int | None:
    """A row's ts as whole epoch seconds, or None when it is not a time: ISO text (UTC when it names no zone) or a finite number."""
    if isinstance(v, str):
        try:
            t = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            return int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())
        except (ValueError, OverflowError):
            return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return int(v)


#: the columns that hold a row's energy over its interval, not a reading at its start: a coarse CSV row's value is spread evenly over the minutes it covers
INTERVAL_ENERGY = ("grid_import_wh", "grid_export_wh", "genset_fuel_l")


def ingest(site_id: str, records: list[dict], source: str, now: int | None = None) -> dict:
    """Normalised dict rows (edge batches) into the minute table; idempotent by (site, minute), the last value wins.

    A stored minute is merged, never replaced whole: a column the row leaves missing (absent, blank or unreadable) keeps its stored value, so a batch
    that carries fewer columns never erases the others, nor a CSV's (audit R7). A CSV is read and written by import_csv, in columns and by day.

    A row whose ts is not a time, or lies before 2015 or more than a day after `now`, is skipped and counted (`skipped`, and `out_of_range` for the
    dated ones), never raised: one bad row must not fail the batch on every retry."""
    rows, skipped, unreadable, out_of_range = [], 0, 0, 0
    latest_ok = (_now_ts() if now is None else int(now)) + AHEAD_S
    for r in records:
        ts = _epoch(r.get("ts"))
        if ts is None:
            skipped += 1
            continue
        if not EARLIEST_TS <= ts <= latest_ok:
            skipped += 1
            out_of_range += 1
            continue
        row = [site_id, ts // 60 * 60, source]
        for c in ALL[3:]:
            v = r.get(c)
            if c in ("inverter_mode_active", "quality_flags"):
                row.append(None if v in (None, "") else str(v)[:40])
            else:
                x, bad = _num(v)
                unreadable += bad
                row.append(x)
        if row[3] is None and row[_VOLT] is not None:
            row[3] = 1.0 if row[_VOLT] >= 180 else 0.0  # grid present from voltage (plan Part 7.5; 180 V to confirm); an unreadable voltage stays unknown
        rows.append(tuple(row))
    res = {"accepted": len(rows), "skipped": skipped, "unreadable_cells": unreadable, "out_of_range": out_of_range}
    if rows:
        with connect() as con:
            con.executemany(_insert_sql(ALL, merge=True), rows)
    return res


def _check_window(stamps, now: int | None = None) -> None:
    """Refuses (ValueError, in words) parsed upload times (any array of epoch seconds) with a row before 2015, a row more than a day ahead, or a span
    over UPLOAD_MAX_DAYS."""
    a = np.asarray(stamps, dtype=np.int64)
    latest_ok = (_now_ts() if now is None else int(now)) + AHEAD_S
    early = int(np.sum(a < EARLIEST_TS))
    late = int(np.sum(a > latest_ok))
    if early or late:
        raise ValueError(f"{early + late} row{'s are' if early + late != 1 else ' is'} dated "
                         + ("before 2015" if early and not late else "in the future" if late and not early else "before 2015 or in the future")
                         + ": check the logger's clock (and the timezone) and upload again")
    span = int(a.max() - a.min()) // 86400 if len(a) else 0
    if span > UPLOAD_MAX_DAYS:
        raise ValueError(f"The file spans {span} days; upload at most {UPLOAD_MAX_DAYS} days per file")


#: the columns an imports row gains beyond the workspace schema's: the uploaded range (epoch seconds UTC of the file's first and last row) and the
#: uploaded file's name, each null when not known (an import from before they were kept, or an upload without a file name)
IMPORT_EXTRA = (("from_ts", "INTEGER"), ("to_ts", "INTEGER"), ("file_name", "TEXT"))
FILE_NAME_MAX = 200


def _imports_columns(con: sqlite3.Connection) -> None:
    """Adds IMPORT_EXTRA to the workspace file's imports table where it lacks them: an additive change that keeps every row, checked on each use (a
    cheap read of the table's columns) so a file restored from an older backup is brought up too."""
    have = {r[1] for r in con.execute("PRAGMA table_info(imports)").fetchall()}
    for name, kind in IMPORT_EXTRA:
        if have and name not in have:
            try:
                con.execute(f"ALTER TABLE imports ADD COLUMN {name} {kind}")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc):  # another connection added it first
                    raise


def _file_name(name) -> str | None:
    """An uploaded file's name as shown back: its last path part, without control characters, at most FILE_NAME_MAX characters; None when blank."""
    if name is None:
        return None
    text = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if ch.isprintable()).strip()[:FILE_NAME_MAX]
    return text or None


def imports(site_id: str, limit: int = 20) -> list[dict]:
    """The site's latest CSV imports, newest first, as the telemetry quality page shows them: id, source, rows, gaps, status, log (a list), created, and
    the uploaded range `from_ts` and `to_ts` (epoch seconds UTC) and `file_name`, each null when not known."""
    with store.connect() as con:
        _imports_columns(con)
        rows = con.execute("SELECT id, source, rows, gaps, status, log, created, from_ts, to_ts, file_name FROM imports WHERE site_id = ? "
                           "ORDER BY created DESC, rowid DESC LIMIT ?", (site_id, max(1, int(limit)))).fetchall()  # two in one second: the later first
    out = []
    for r in rows:
        d = dict(r)
        try:
            log = json.loads(d["log"] or "[]")
        except (TypeError, ValueError):
            log = []
        d["log"] = log if isinstance(log, list) else []  # the page lists it: a stored null or object is shown as no lines
        out.append(d)
    return out


#: plausibility of a CSV's readings (audit R7): a negative PV or load reading down to this is kept as read (an inverter's standby draw at night, a
#: meter's noise); below it the cell is stored as missing and counted
NEGATIVE_TOLERANCE_W = 500.0
#: a load column, or a PV column between 10:00 and 14:00 Karachi time, whose largest reading over at least an hour stays under this many watts probably
#: holds kW: the product serves commercial and industrial sites, whose load and midday PV run to tens of kW at least
KW_SUSPECT_W = 1000.0
_MIDDAY_H = (10, 14)
_WHY_NOT_NEGATIVE = {"pv_power_w": "a solar array does not draw power beyond an inverter's standby",
                     "load_power_w": "a load does not feed power back"}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _local_hour(ts: np.ndarray) -> np.ndarray:
    return (ts + 5 * 3600) % 86400 // 3600


def _check_values(ts: np.ndarray, cols: dict, spans: np.ndarray) -> list[str]:
    """The file's implausible readings, as warnings: PV or load below -NEGATIVE_TOLERANCE_W and a BMS state of charge outside 0 to 100 % are stored as
    missing (blanked here, in the file's columns of numbers, NaN for missing, before they are written); a load or midday PV column that never reaches
    KW_SUSPECT_W, and a state of charge that never goes above 1, are kept as read and named, as a unit slip (kW for W, a fraction for percent) the
    person must check. `spans` holds the minutes each row stands for."""
    out, top = [], {}
    hour = _local_hour(ts)
    for c, why in _WHY_NOT_NEGATIVE.items():
        x = cols.get(c)
        if x is None:
            continue
        read = ~np.isnan(x)
        below = read & (x < -NEGATIVE_TOLERANCE_W)
        x[below] = np.nan
        n = int(below.sum())
        if n:
            out.append(f"{_plural(n, f'{c} cell was', f'{c} cells were')} below -{NEGATIVE_TOLERANCE_W:,.0f} W ({why}) and "
                       f"{'was' if n == 1 else 'were'} stored as missing")
        judged = read & ~below & ((_MIDDAY_H[0] <= hour) & (hour < _MIDDAY_H[1]) if c == "pv_power_w" else True)
        top[c] = (float(np.abs(x[judged]).max()) if judged.any() else 0.0, int(spans[judged].sum()))
    x = cols.get("battery_soc_bms_pct")
    if x is not None:
        read = ~np.isnan(x)
        wrong = read & ~((x >= 0) & (x <= 100))
        x[wrong] = np.nan
        n = int(wrong.sum())
        if n:
            out.append(f"{_plural(n, 'battery_soc_bms_pct cell was', 'battery_soc_bms_pct cells were')} outside 0 to 100 % and "
                       f"{'was' if n == 1 else 'were'} stored as missing")
        inside = read & ~wrong
        if inside.any() and 0 < float(x[inside].max()) <= 1 and int(spans[inside].sum()) >= 60:
            out.append("battery_soc_bms_pct never goes above 1 in this file: it may hold the state of charge as a fraction (0 to 1) where percent "
                       "(0 to 100) is expected; the values were stored as read")
    for c, (peak, minutes) in top.items():
        if minutes >= 60 and 0 < peak < KW_SUSPECT_W:
            where = " between 10:00 and 14:00 Karachi time" if c == "pv_power_w" else ""
            out.append(f"{c} never reaches {KW_SUSPECT_W:,.0f} W{where} in this file (at most {peak:,.1f}): the column may hold kW where watts are "
                       f"expected; the values were stored as read")
    return out


#: 20:00 to 05:00 Karachi time is dark all year in Pakistan (sunset by about 19:20, sunrise from about 05:00)
_NIGHT_H = (20, 5)


def _zone_warning(ts: np.ndarray, cols: dict, spans: np.ndarray) -> str | None:
    """A probable time-zone slip (frontend-states-formats-06): PV above 5 % of the file's largest reading (and above KW_SUSPECT_W) for half an hour or
    more between 20:00 and 05:00 Karachi time. A local-time file read as UTC puts the afternoon's PV after 20:00; a UTC file read as Karachi time puts the
    morning's before 05:00."""
    pv = cols.get("pv_power_w")
    if pv is None:
        return None
    read = ~np.isnan(pv)
    top = float(pv[read].max()) if read.any() else 0.0
    if top < KW_SUSPECT_W:
        return None
    bar = max(KW_SUSPECT_W, 0.05 * top)
    hour = _local_hour(ts)
    night = int(spans[read & (pv > bar) & ~((_NIGHT_H[1] <= hour) & (hour < _NIGHT_H[0]))].sum())
    if night < 30:
        return None
    return (f"pv_power_w is above {bar / 1000:,.1f} kW for {night:,} minutes between 20:00 and 05:00 Karachi time, when there is no sun: the times in "
            f"the file may be in another zone than the one chosen (UTC or Karachi time)")


# ------------------------------------------------------------------------------------------------ the CSV column mapping

#: the units a mapped column may be written in, per key, with the factor to the stored unit (the first is the stored unit); a key not listed has no unit
#: (a time, an on/off signal, a mode). The column mapping of plan Part 7.5 and UI-06 (flow-telemetry-plan-control-12): column to key, unit and sign.
_POWER_UNITS = {"W": 1.0, "kW": 1000.0, "MW": 1e6}
_ENERGY_UNITS = {"Wh": 1.0, "kWh": 1000.0, "MWh": 1e6}
UNITS: dict[str, dict[str, float]] = {
    **{k: _POWER_UNITS for k in ("grid_power_w", "pv_power_w", "load_power_w", "battery_power_w", "genset_power_w")},
    "grid_import_wh": _ENERGY_UNITS, "grid_export_wh": _ENERGY_UNITS, "grid_voltage_v": {"V": 1.0, "kV": 1000.0}, "battery_voltage_v": {"V": 1.0},
    "battery_current_a": {"A": 1.0}, "battery_temp_c": {"°C": 1.0}, "battery_soc_bms_pct": {"%": 1.0, "fraction": 100.0}, "genset_fuel_l": {"L": 1.0},
}
#: the keys whose sign a mapping may reverse: the product's battery discharge and grid import are positive (OpenEMS signs)
SIGNED = ("grid_power_w", "battery_power_w", "battery_current_a")
#: what a file must carry: a time, and the grid signal (grid_present, or grid_voltage_v to derive it)
REQUIRED = ("ts", "grid_present")
TIER1_NOTE = "No inverter_mode_active column: this source supports monitor (tier 1) only, because the counterfactual needs the inverter's mode"

#: a header's words for each key, as common loggers and inverter clouds write them (the unit, in brackets or as a kW/kWh suffix, is read apart); the
#: first pattern that matches the whole of a header's words wins, so the more particular come first
_ALIASES = (
    ("ts", r"ts|time ?stamp|date ?time|date and time|date time|time|date|local time|time utc|collection time|updated time|record time"),
    ("grid_present", r"grid present|grid (status|state|on|available|availability|connected)|(utility|mains|wapda)( status| present| available)?"),
    ("grid_voltage_v", r"(grid|utility|mains|wapda|ac input) (voltage|volt|v)( l1| r| phase a)?"),
    ("grid_import_wh", r"grid import( energy)?|(energy )?(imported|purchased|bought)( energy)?|import energy|buy energy|energy from grid|grid buy"),
    ("grid_export_wh", r"grid export( energy)?|(energy )?(exported|sold|fed in)( energy)?|export energy|sell energy|feed ?in energy|energy to grid"
                       r"|grid sell"),
    ("grid_power_w", r"grid( active)? power|grid|meter( active)? power|(utility|mains|wapda) power|total grid power"),
    ("pv_power_w", r"pv|solar|(pv|solar|dc)( input| output| total| active)? power|total (pv|solar|dc) power|(pv|solar) production|production( power)?"),
    ("load_power_w", r"load|(total |house |home |ac )?load( active)? power|(total )?consumption( power)?|total load|load consumption|house load"
                     r"|home load"),
    ("battery_soc_bms_pct", r"soc|(battery|bat|bms) soc|(battery )?state of charge|battery (level|capacity)"),
    ("battery_voltage_v", r"(battery|bat) (voltage|volt|v)"),
    ("battery_current_a", r"(battery|bat) (current|i)"),
    ("battery_temp_c", r"(battery|bat|bms) (temperature|temp)"),
    ("battery_power_w", r"battery|bat|(battery|bat)( active| charge discharge| charging discharging)? power"),
    ("genset_running", r"(genset|generator|gen|dg) (running|status|state|on)"),
    ("genset_fuel_l", r"((genset|generator|gen|dg) )?(fuel|diesel)( used| consumption| consumed| litres| liters)?"),
    ("genset_power_w", r"genset|generator|(genset|generator|gen|dg|diesel generator)( active)? power"),
    ("inverter_mode_active", r"inverter mode( active)?|(work|working|operating) mode"),
    ("quality_flags", r"quality( flags)?"),
)
_ALIAS_RE = tuple((k, re.compile(p)) for k, p in _ALIASES)
_SIGNAL_OF = {"grid_power_w": "grid_present", "genset_power_w": "genset_running"}
_UNIT_WORDS = {"w": "W", "kw": "kW", "mw": "MW", "wh": "Wh", "kwh": "kWh", "mwh": "MWh", "%": "%", "pct": "%", "percent": "%", "v": "V", "kv": "kV",
               "a": "A", "°c": "°C", "c": "°C", "degc": "°C", "deg c": "°C", "l": "L", "litre": "L", "litres": "L", "liter": "L", "liters": "L",
               "ltr": "L"}
#: the units a header may end in without brackets (load_kw, Energy kWh); one letter alone (grid v) is a word, not a unit
_UNIT_SUFFIX = ("kw", "mw", "kwh", "mwh", "kv")
_BRACKETED = re.compile(r"[\(\[]\s*([^\)\]]*?)\s*[\)\]]")
_DECIMAL_COMMA = re.compile(r"^-?[\d.]*\d,\d+$")


def _dialect(text: str) -> tuple[str, str]:
    """A file's cell separator (",", ";" or a tab: whichever its header line has most of, "," on a tie) and decimal mark ("," when cells are separated
    by ";" or a tab and numbers are written 12,5, as European loggers export them)."""
    lines = text.splitlines()
    head = next((line for line in lines if line.strip()), "")
    delim = max((",", ";", "\t"), key=lambda d: (head.count(d), d == ","))
    if delim != ",":
        for row in csv.reader(lines[1:51], delimiter=delim):
            if any(_DECIMAL_COMMA.match(c.strip()) for c in row):
                return delim, ","
    return delim, "."


def mapping_of(mapping: dict | None) -> dict:
    """A column mapping as {"columns": {file column: {"key", "unit", "reverse"}}, "ts_format"}; also takes the short form {file column: key}. A key of
    None ignores the column; a column the mapping does not name is read by its own name. Raises ValueError, in one sentence, for a key that is not one
    of CSV_COLUMNS, a unit the key is not written in (UNITS), a reversed key without a sign (SIGNED) or a time format not in TS_FORMATS."""
    if not mapping:
        return {"columns": {}, "ts_format": "auto"}
    full = isinstance(mapping.get("columns"), dict)
    cols, fmt = (mapping["columns"], mapping.get("ts_format") or "auto") if full else (mapping, "auto")
    out = {}
    for name, c in cols.items():
        c = c if isinstance(c, dict) else {"key": c}
        key, unit, reverse = c.get("key"), c.get("unit"), bool(c.get("reverse"))
        if key is not None and key not in MAPPING_KEYS:
            raise ValueError(f"Column {name} is mapped to {key}, which is not a key a CSV may carry ({', '.join(MAPPING_KEYS)})")
        if unit is not None and unit not in UNITS.get(key, {}):
            units = UNITS.get(key)
            raise ValueError(f"Column {name} cannot be read in {unit}: "
                             + (f"the units of {key} are {', '.join(units)}" if units else f"{key} has no unit"))
        if reverse and key not in SIGNED:
            raise ValueError(f"Column {name}'s sign cannot be reversed: only {', '.join(SIGNED)} have a sign")
        out[str(name)] = {"key": key, "unit": unit, "reverse": reverse}
    if fmt not in TS_FORMATS:
        raise ValueError(f"The time format is one of {', '.join(TS_FORMATS)}")
    return {"columns": out, "ts_format": fmt}


def _column_names(fields: list[str], m: dict) -> dict:
    """Each of a file's columns as the key it is read as (None: ignored), by the mapping, else by its own name; two columns read as one key are refused."""
    names = {f: (m["columns"][f]["key"] if f in m["columns"] else f) for f in fields}
    twice = Counter(n for n in names.values() if n in MAPPING_KEYS)
    for key, n in twice.items():
        if n > 1:
            cols = [f for f, k in names.items() if k == key]
            raise ValueError(f"Columns {' and '.join(cols[:3])} are both read as {key}: map one of them to another key, or to none")
    return names


def _found(fields: list[str]) -> str:
    shown = [f for f in fields if f.strip()][:12]
    more = len([f for f in fields if f.strip()]) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _header_words(name: str) -> tuple[str, str | None]:
    """A header as (its words in lower case without its unit, the unit it names, if any)."""
    unit = None
    m = _BRACKETED.search(name)
    if m:
        unit = _UNIT_WORDS.get(m.group(1).strip().lower())
        name = _BRACKETED.sub(" ", name)
    if "%" in name:
        unit, name = unit or "%", name.replace("%", " ")
    words = re.sub(r"[^0-9a-z°]+", " ", name.lower()).split()
    if len(words) > 1 and words[-1] in _UNIT_SUFFIX:
        unit, words = unit or _UNIT_WORDS[words[-1]], words[:-1]
    return " ".join(words), unit


def _ts_format_of(cells: list[str]) -> tuple[str, bool]:
    """The date order a file's times are written in, and whether it is a guess: day first when a first number is over 12, month first when a second
    one is; when none is, day first (as Pakistan writes dates), flagged as ambiguous."""
    slashed = [m for c in cells if (m := _SLASHED.match(c.strip()))]
    if not slashed:
        return "auto", False
    if any(int(m.group(1)) > 12 for m in slashed):
        return "dmy", False
    if any(int(m.group(2)) > 12 for m in slashed):
        return "mdy", False
    return "dmy", True


def guess_mapping(head: str, saved: dict | None = None) -> dict:
    """The mapping step's first answer for a file's head (its first lines): the cell separator and decimal mark, each column with the key, unit and sign
    it is read as and where that came from ("saved": the site's saved mapping names the column; "name": it is a documented key; "guess": its header
    reads as one, see _ALIASES; None: not read), its first cells, the date order of its times (with `ts_ambiguous` when only a guess), the keys still
    missing (ts, grid_present) and notes (no inverter_mode_active: tier 1 only). One column per key: a later column a key is guessed for again is left
    unread. Nothing is stored."""
    text = head.lstrip("\ufeff")
    delim, decimal = _dialect(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    header = rows[0] if rows else []
    body = [r for r in rows[1:52] if any(c.strip() for c in r)]
    if len(body) > 1 and len(body[-1]) < len(header):
        body = body[:-1]  # the head was cut in the middle of its last line
    saved_cols = mapping_of(saved)["columns"] if saved else {}
    cols, used = [], set()
    for i, name in enumerate(header):
        cells = [r[i].strip() for r in body if i < len(r)]
        if name in saved_cols:
            c, src = dict(saved_cols[name]), "saved"
        elif name.strip() in CSV_COLUMNS:
            c, src = {"key": name.strip(), "unit": None, "reverse": False}, "name"
        else:
            words, unit = _header_words(name)
            key = next((k for k, rx in _ALIAS_RE if words and rx.fullmatch(words)), None)
            if unit not in UNITS.get(key, {}):
                unit = None
            if key == "battery_soc_bms_pct" and unit is None:
                vals = [x for c in cells if (x := _num(c.replace(",", ".") if decimal == "," else c)[0]) is not None]
                unit = "fraction" if vals and 0 < max(vals) <= 1 else None
            # a "Grid" or "Generator" column whose cells are on and off (or 1 and 0) is the signal, not the power
            if key in _SIGNAL_OF and cells and all(c.strip().lower() in FLAG_WORDS or c.strip() in ("0", "1") for c in cells if c.strip()) \
                    and any(c.strip() for c in cells):
                key, unit = _SIGNAL_OF[key], None
            c, src = {"key": key, "unit": unit, "reverse": False}, ("guess" if key else None)
        if c["key"] in used:
            c, src = {"key": None, "unit": None, "reverse": False}, None
        if c["key"]:
            used.add(c["key"])
        cols.append({"name": name, **c, "from": src, "sample": cells[:5]})
    _join_day_and_time(cols, body)
    used = {c["key"] for c in cols if c["key"]}
    ts_cells = next(([r[i] for r in body if i < len(r)] for i, c in enumerate(cols) if c["key"] == "ts"), [])
    fmt, ambiguous = _ts_format_of(ts_cells)
    saved_fmt = mapping_of(saved)["ts_format"] if saved else "auto"
    if ambiguous and saved_fmt != "auto" and any(c["from"] == "saved" for c in cols):
        fmt = saved_fmt  # the date order the site's saved mapping chose, where the file's own dates cannot tell
    missing = [k for k in ("ts",) if k not in used] + ([] if used & {"grid_present", "grid_voltage_v"} else ["grid_present"])
    return {"delimiter": delim, "decimal": decimal, "ts_format": fmt, "ts_ambiguous": ambiguous, "columns": cols, "missing": missing,
            "notes": [] if "inverter_mode_active" in used else [TIER1_NOTE], "saved": any(c["from"] == "saved" for c in cols)}


def _join_day_and_time(cols: list[dict], body: list[list[str]]) -> None:
    """A file that writes the day and the time of day in two columns ("Date" and "Time"): the column guessed as ts holds days only, so a column left unread
    whose cells are all times of day is read as ts_time, and the import joins them; when the ts column holds the times, the unread column of days becomes
    ts and the times ts_time (round 7 sweeps, telemetry review: the second column was left unread and every row read as midnight). A saved column is
    left as the site's mapping reads it."""
    def cells(i: int) -> list[str]:
        return [r[i].strip() for r in body if i < len(r) and r[i].strip()]

    def all_match(i: int, rx: re.Pattern) -> bool:
        got = cells(i)
        return bool(got) and all(rx.match(x) for x in got)
    at = next((i for i, c in enumerate(cols) if c["key"] == "ts"), None)
    if at is None or any(c["key"] == TS_TIME for c in cols):
        return
    unread = [i for i, c in enumerate(cols) if c["key"] is None and c["from"] != "saved"]
    if all_match(at, _DAY_ONLY):
        j = next((i for i in unread if all_match(i, _TIME_ONLY)), None)
        if j is not None:
            cols[j].update(key=TS_TIME, unit=None, reverse=False, **{"from": "guess"})
    elif all_match(at, _TIME_ONLY) and cols[at]["from"] != "saved":
        j = next((i for i in unread if all_match(i, _DAY_ONLY)), None)
        if j is not None:
            cols[j].update(key="ts", unit=None, reverse=False, **{"from": "guess"})
            cols[at].update(key=TS_TIME, unit=None, reverse=False)


def ts_column(fields: list[str], mapping: dict | None) -> str | None:
    """The file column read as ts, by the mapping or by its name."""
    cols = mapping_of(mapping)["columns"]
    return next((f for f in fields if (cols[f]["key"] if f in cols else f) == "ts"), None)


def saved_mapping(site_id: str) -> dict | None:
    """The site's saved CSV column mapping (its CSV source's data_sources row): {mapping, updated, by}, or None."""
    with store.connect() as con:
        r = con.execute("SELECT data FROM data_sources WHERE site_id = ? AND kind = 'csv' ORDER BY created LIMIT 1", (site_id,)).fetchone()
    try:
        data = json.loads(r["data"]) if r else {}
        if not isinstance(data, dict) or not isinstance(data.get("mapping"), dict):
            return None
        return {"mapping": mapping_of(data["mapping"]), "updated": data.get("updated"), "by": data.get("by")}
    except ValueError:  # not JSON, or a mapping this version no longer reads: as if none were saved
        return None


def save_mapping(site_id: str, mapping: dict, by: str) -> dict:
    """Saves the site's CSV column mapping (checked by mapping_of), replacing the one saved before: {mapping, updated, by}."""
    data = {"mapping": mapping_of(mapping), "updated": store._now(), "by": by}
    with store.connect() as con:
        r = con.execute("SELECT id FROM data_sources WHERE site_id = ? AND kind = 'csv' ORDER BY created LIMIT 1", (site_id,)).fetchone()
        if r:
            con.execute("UPDATE data_sources SET data = ? WHERE id = ?", (json.dumps(data), r["id"]))
        else:
            con.execute("INSERT INTO data_sources (id, site_id, kind, data, created) VALUES (?,?,?,?,?)",
                        (store.new_id(), site_id, "csv", json.dumps(data), store._now()))
    return data


def _mapping_words(m: dict) -> str | None:
    """The import note of a mapping's effect: the columns it renames, their units and signs, and the date order."""
    parts = []
    for f, c in m["columns"].items():
        if c["key"] is None or (c["key"] == f and not c["unit"] and not c["reverse"]):
            continue
        parts.append(f"{f} as {c['key']}" + (f" in {c['unit']}" if c["unit"] and c["unit"] != next(iter(UNITS.get(c["key"], {"": 0}))) else "")
                     + (" with its sign reversed" if c["reverse"] else ""))
    order = {"dmy": "; dates read day first", "mdy": "; dates read month first"}.get(m["ts_format"], "")
    if not parts and not order:
        return None
    shown = ", ".join(parts[:12]) + (f" and {len(parts) - 12} more" if len(parts) > 12 else "")
    return f"Read with the column mapping: {shown or 'the columns by their names'}{order}"


#: the CSV keys that are text, kept as written (at most 40 characters); every other key is a number
_CSV_TEXT = ("inverter_mode_active", "quality_flags")
#: a parsed time beyond this magnitude is clamped (it is refused as before 2015 or in the future either way), so it fits a 64-bit column of times
_TS_CLAMP = 2 ** 62
#: how much of a file's start is decoded to find its separator and decimal mark (the header and the first rows)
_HEAD_BYTES = 65536


class _Columns:
    """A CSV's rows as columns (performance-live-04): the time of each readable row (epoch seconds UTC, array('q')), per number key the values
    (array('d'), NaN for missing) and a byte per cell set where the cell could not be read, per text key the cells. One streamed pass fills them, so an
    upload costs a few machine words a cell instead of a dict and a tuple per row."""

    def __init__(self, keys: list[str]):
        self.ts = array("q")
        self.num = {k: array("d") for k in keys if k not in _CSV_TEXT}
        self.bad = {k: bytearray() for k in self.num}
        self.text = {k: [] for k in keys if k in _CSV_TEXT}


def _read_columns(content: bytes, delim: str, decimal: str, at: dict, scale: dict, tz_offset_h: float, ts_format: str) -> tuple[_Columns, int, str | None]:
    """The file's rows, read once, as _Columns: `at` gives each key's column (the last of two columns with one name, as a reader by name keeps), `scale`
    each mapped key's factor (its unit and sign). The text is decoded as it is read, never whole. Cells are read as import_csv documents (decimal commas,
    on/off words, blanks missing); a row without a readable time is skipped and counted. Returns (columns, rows skipped, the first unreadable time)."""
    cols = _Columns([k for k in at if k not in ("ts", TS_TIME)])
    text = io.TextIOWrapper(io.BytesIO(content), encoding="utf-8-sig", errors="replace", newline="")
    reader = csv.reader(text, delimiter=delim)
    next(reader, None)  # the header, read by the caller
    t_at = at["ts"]
    # the time of day in a column of its own (TS_TIME): joined to the day the ts column holds, "06/11/2026" and "09:00" read as "06/11/2026 09:00"
    clock_at = at.get(TS_TIME)
    nums = [(k, i, cols.num[k].append, cols.bad[k].append, scale.get(k), k in ("grid_present", "genset_running")) for k, i in at.items() if k in cols.num]
    texts = [(k, i, cols.text[k].append) for k, i in at.items() if k in cols.text]
    words: dict[str, str] = {}  # one string per distinct text cell (an inverter mode repeats on every row)
    nan = math.nan
    bad_ts, first_bad = 0, None
    for row in reader:
        if not row:
            continue  # a blank line
        n = len(row)
        cell = row[t_at] if t_at < n else None
        if clock_at is not None:
            clock = row[clock_at].strip() if clock_at < n else ""
            cell = f"{cell.strip()} {clock}" if cell and cell.strip() and clock else None
        try:
            t = csv_time(cell, tz_offset_h, ts_format)
        except (ValueError, TypeError, OverflowError):
            t = None
        if t is None:
            bad_ts += 1
            first_bad = first_bad or (cell or "").strip() or None
            continue
        cols.ts.append(max(-_TS_CLAMP, min(_TS_CLAMP, t)))
        for k, i, put, put_bad, x, flag in nums:
            v = row[i] if i < n else None
            if v is not None:
                if decimal == "," and _DECIMAL_COMMA.match(v.strip()):
                    v = v.strip().replace(".", "").replace(",", ".")
                if flag:
                    v = FLAG_WORDS.get(v.strip().lower(), v)
            f, bad = _num(v)
            if f is not None and x is not None:
                f *= x
                if abs(f) > CELL_MAX:  # a mapped unit that takes a reading past any meter's range: unreadable, as _num reads it
                    f, bad = None, True
            put(nan if f is None else f)
            put_bad(bad)
        for k, i, put in texts:
            v = row[i] if i < n else None
            put(None if v in (None, "") else words.setdefault(v[:40], v[:40]))
    return cols, bad_ts, first_bad


def _write_days(site_id: str, ts: np.ndarray, cols: dict, texts: dict, spans: np.ndarray, latest_ok: int) -> dict:
    """The kept rows into the minute table, merged per column (_insert_sql with merge), one Karachi day per write transaction, each day aggregated after
    its write (performance-live-04: the whole file was one executemany under one BEGIN IMMEDIATE, and every reader and edge batch of the company
    waited for it). Each row is stored on the minutes it covers (`spans`), none past `latest_ok`. A failure part-way leaves the days before it stored;
    uploading the file again stores the same minutes (the write is idempotent by site and minute). Returns {accepted, overlap}."""
    start = ts // 60 * 60
    row = np.repeat(np.arange(len(ts)), spans)
    k = np.arange(len(row)) - np.repeat(np.cumsum(spans) - spans, spans)
    minute = start[row] + 60 * k
    keep = (k == 0) | (minute <= latest_ok)
    row, minute = row[keep], minute[keep]
    names = tuple(cols) + tuple(texts)
    sql = _insert_sql(("site_id", "ts", "source_id") + names, merge=True)
    day = (minute + 5 * 3600) // 86400
    cuts = np.flatnonzero(np.diff(day)) + 1
    accepted = overlap = 0
    for a, b in zip(np.r_[0, cuts], np.r_[cuts, len(minute)]):
        r = row[a:b]
        stamps = minute[a:b].tolist()
        cells = [np.where(np.isnan(cols[c][r]), None, cols[c][r]).tolist() for c in cols] + [[texts[c][i] for i in r.tolist()] for c in texts]
        with connect() as con:
            have = {t for (t,) in con.execute("SELECT ts FROM minute WHERE site_id = ? AND ts >= ? AND ts <= ?", (site_id, stamps[0], stamps[-1]))}
            overlap += sum(1 for t in stamps if t in have)
            con.executemany(sql, ((site_id, t, "csv", *v) for t, *v in zip(stamps, *cells)))
        accepted += len(stamps)
        aggregate_day(site_id, date(1970, 1, 1) + timedelta(days=int(day[a])))
    return {"accepted": accepted, "overlap": overlap}


def refuse_named_twice(content: bytes) -> None:
    """Refuses (ValueError, in words) a file whose header names a column twice: the import reads a repeated name's last column, which the person
    uploading would not expect. Only the header is read (audit R7 performance-live-04: the route parsed every row for the date window and the import
    parsed them all again). The upload's route reads a file by it before import_csv, and the upload's preview (preview_csv) before its rows, so the
    preview refuses such a file in the same words (UX K15 check: it answered the rows the upload then refused)."""
    head = content[:_HEAD_BYTES].decode("utf-8-sig", errors="replace")
    row = next(csv.reader(io.StringIO(head), delimiter=_dialect(head)[0]), None) or []
    twice = sorted({h for h in row if h.strip() and row.count(h) > 1})
    if twice:
        raise ValueError(f"The header names {', '.join(twice[:5])} more than once: give each column once and upload again")


class _Layout:
    """How a CSV is read (`_layout`): its mapping, date order, separator and decimal mark, header, each column's key, the notes of the header, each
    mapped key's factor (its unit and sign) and each key's column."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _layout(content: bytes, mapping: dict | None, ts_format: str) -> _Layout:
    """A file's header read as the upload reads it, and its refusals (ValueError, in one sentence): a mapping that cannot be read, no header, no time
    column, a time of day without its date, no grid signal. The upload (import_csv) and its preview (preview_csv; UX backend ask K15) read a file by
    this one function, so the preview refuses what the upload refuses, in the same words."""
    m = mapping_of(mapping)
    if ts_format == "auto":
        ts_format = m["ts_format"]
    if ts_format not in TS_FORMATS:
        raise ValueError(f"The time format is one of {', '.join(TS_FORMATS)}")
    delim, decimal = _dialect(content[:_HEAD_BYTES].decode("utf-8-sig", errors="replace"))
    fields = next(csv.reader(io.TextIOWrapper(io.BytesIO(content), encoding="utf-8-sig", errors="replace", newline=""), delimiter=delim), None)
    if not fields:
        raise ValueError("The file needs a header row with a ts column (UTC, or set the timezone)")
    names = _column_names(fields, m)
    if TS_TIME in names.values() and "ts" not in names.values():
        clock = next(f for f, k in names.items() if k == TS_TIME)
        raise ValueError(f"Column {clock} holds the time of day without its date: choose the column that holds the date as the time (ts) in the column "
                         f"mapping, and the import joins the two")
    if "ts" not in names.values():
        raise ValueError(f"The file has no ts column (its columns are {_found(fields)}): choose the column that holds the time in the column "
                         f"mapping, or name the columns as the keys a file may carry: {', '.join(CSV_COLUMNS)}")
    log = []
    if delim != ",":
        log.append(f"Cells are separated by {'semicolons' if delim == ';' else 'tabs'}" + (" and decimals by commas (12,5)" if decimal == "," else ""))
    mapped = _mapping_words(m)
    if mapped:
        log.append(mapped)
    ignored = [f for f, n in names.items() if n not in MAPPING_KEYS]
    if ignored:
        log.append(f"Ignored columns: {', '.join(ignored[:10])}")
    if "inverter_mode_active" not in names.values():
        log.append(TIER1_NOTE)
    if "grid_present" not in names.values() and "grid_voltage_v" not in names.values():
        raise ValueError(f"The file needs a grid_present column (or grid_voltage_v to derive it): the product cannot see outages without it. Its "
                         f"columns are {_found(fields)}: choose the one that holds the grid's state in the column mapping")
    # a mapped column's unit and sign: its readings scaled to the stored unit and the product's sign (discharge and import positive)
    scale = {c["key"]: UNITS.get(c["key"], {}).get(c["unit"] or "", 1.0) * (-1.0 if c["reverse"] else 1.0)
             for f, c in m["columns"].items() if f in names and c["key"] and (c["unit"] or c["reverse"])}
    scale = {k: x for k, x in scale.items() if x != 1.0}
    if "grid_import_wh" not in names.values() and "grid_export_wh" not in names.values():
        log.append("No grid_import_wh or grid_export_wh column: import and export are read from grid_power_w (positive is import)"
                   if "grid_power_w" in names.values() else
                   "No grid_import_wh, grid_export_wh or grid_power_w column: grid import and export are missing for this file, not zero")
    at = {}
    for i, f in enumerate(fields):
        if names.get(f) in MAPPING_KEYS:
            at[names[f]] = i  # a name given twice: its last column, as a reader by name keeps
    return _Layout(m=m, ts_format=ts_format, delim=delim, decimal=decimal, fields=fields, names=names, log=log, scale=scale, at=at)


def _in_order(ts: np.ndarray, parsed: _Columns) -> tuple[np.ndarray, dict, dict, dict, int]:
    """A file's parsed rows in time order, each minute once: duplicate times keep the last row (plan Part 7.5) and are counted once (a stable sort keeps,
    of two rows with one time, the later in the file later). Returns (times, number columns, unreadable cells, text columns, rows dropped). A file in
    time order without repeats (the usual one) keeps the parsed columns as they are, not copied; otherwise each parsed column is let go once reordered."""
    order = np.argsort(ts, kind="stable")
    minute = ts[order] // 60
    order = order[np.r_[minute[1:] != minute[:-1], True]]
    duplicates = len(ts) - len(order)
    del minute
    if duplicates == 0 and bool(np.all(order[1:] > order[:-1])):
        cols = {k: np.frombuffer(v, dtype=float) for k, v in parsed.num.items()}
        bad = {k: np.frombuffer(v, dtype=bool) for k, v in parsed.bad.items()}
        texts = parsed.text
    else:
        ts = ts[order]
        cols = {k: np.frombuffer(parsed.num.pop(k), dtype=float)[order] for k in list(parsed.num)}
        bad = {k: np.frombuffer(parsed.bad.pop(k), dtype=bool)[order] for k in list(parsed.bad)}
        texts = {k: [v[i] for i in order.tolist()] for k, v in parsed.text.items()}
    return ts, cols, bad, texts, duplicates


def _repeats_note(duplicates: int) -> str:
    return (f"{duplicates:,} row{'s repeat' if duplicates != 1 else ' repeats'} a minute that a later row of the file also gives: the last row of each "
            f"minute was kept")


def _grid_read(cols: dict, bad: dict, n: int) -> tuple[list[str], np.ndarray]:
    """grid_present is what the product sees outages by: a file in which no row's grid signal (grid_present, or grid_voltage_v) can be read is refused
    (ValueError), and unreadable grid cells are a warning. Returns (the warnings, where the voltage was read)."""
    none = np.zeros(n, dtype=bool)
    grid_read = ~np.isnan(cols["grid_present"]) if "grid_present" in cols else none
    volt_read = ~np.isnan(cols["grid_voltage_v"]) if "grid_voltage_v" in cols else none
    if not (grid_read | volt_read).any():
        raise ValueError("No row's grid_present (or grid_voltage_v) can be read: write grid_present as 1 for grid on and 0 for grid off (on and off "
                         "are read too) and upload again; the product cannot see outages without it")
    warnings = []
    unreadable_grid = int(np.sum(~grid_read & ~volt_read & bad["grid_present"])) if "grid_present" in bad else 0
    if unreadable_grid:
        warnings.append(f"{unreadable_grid:,} grid_present cell{'s' if unreadable_grid != 1 else ''} could not be read (1 is grid on, 0 is grid off): "
                        f"the grid is stored as unknown in those minutes, so an outage in them is not seen")
    return warnings, volt_read


def _step_of(ts: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
    """The logger's interval: the most common step between rows, in whole minutes (the shorter on a tie), refused (ValueError) over STEP_MAX_MIN; with
    the steps between rows and the minutes each row stands for. A 5- or 15-minute row is stored as the minutes it starts (up to the next row), so its
    energy, the day's peak and its outage minutes are counted in full (audit R7)."""
    diffs = np.diff(ts // 60)
    if len(diffs):
        values, counts = np.unique(diffs, return_counts=True)
        step = int(values[np.argmax(counts)])  # np.unique sorts the steps, and argmax takes the first of equal counts: the shorter on a tie
    else:
        step = 1
    if step > STEP_MAX_MIN:
        raise ValueError(f"Rows are {step} minutes apart: the import reads rows at most {STEP_MAX_MIN} minutes apart; upload the logger's "
                         f"1-, 5- or 15-minute rows")
    return step, diffs, np.r_[np.minimum(step, diffs), step].astype(np.int64)


def _step_note(step: int) -> str:
    return (f"Rows are {step} minutes apart: each row was stored as the {step} minutes it starts, its readings held over them and its energy "
            f"({', '.join(INTERVAL_ENERGY)}) spread evenly")


def _grid_from_voltage(cols: dict, volt_read: np.ndarray, n: int) -> None:
    """grid_present from the voltage where the file gives none (plan Part 7.5; 180 V to confirm); an unreadable voltage stays unknown."""
    if "grid_voltage_v" in cols:
        grid = cols.get("grid_present", np.full(n, np.nan))
        derive = np.isnan(grid) & volt_read
        grid[derive] = (cols["grid_voltage_v"][derive] >= 180).astype(float)
        cols["grid_present"] = grid


def import_csv(site_id: str, content: bytes, tz_offset_h: float = 0.0, mapping: dict | None = None, now: int | None = None,
               file_name: str | None = None, ts_format: str = "auto") -> dict:
    """A CSV upload: one row per minute; blanks are missing; duplicate times keep the last row and are counted once (`rows` is the distinct rows kept,
    `duplicates` the rows dropped, both in the notes); rows 2 to 60 minutes apart are stored as the minutes each covers (`step_min`, `minutes`; see
    _write_days), rows further apart are refused; gaps over 5 minutes (or over the step) are reported. A file in which no row's grid_present (or
    grid_voltage_v) can be read is refused; on and off, true and false, yes and no are read as 1 and 0. Implausible readings are checked
    (_check_values: impossible cells stored as missing, probable unit slips named). What may make the import wrong is listed in
    `warnings` (also at the end of `log`), and the import's `status` is then "done_with_warnings", not "done".

    The file is read once, in one streamed pass, into a column of numbers per key (_read_columns), so the import's memory is a small multiple of the
    file's, and the minutes are written one Karachi day per transaction, each day aggregated after its write (_write_days; audit R7 performance-live-04:
    the file was decoded and parsed twice, a dict and a tuple per row, and written in one transaction that every reader and writer waited for).

    The parsed rows must lie between 2015 and a day after `now` and span at most UPLOAD_MAX_DAYS, or the whole file is refused (ValueError) and
    nothing is stored; the check reads the rows exactly as the import does (a repeated column keeps its last cell). Only the local days that received
    rows are aggregated. Times are read by csv_time in `ts_format` (ISO 8601 or epoch seconds by default); a file in which none can be read is refused
    with the formats read, a file over UPLOAD_MAX_BYTES with TOO_LARGE, and a time-zone offset outside -23 to 23 hours in one sentence. The imports row
    keeps the uploaded range (`from_ts`, `to_ts`) and the file's name when given (`file_name`, see _file_name).

    `mapping` (see mapping_of; flow-telemetry-plan-control-12) reads a vendor's columns as the product's keys, in their units and signs, and its
    `ts_format` (day-first or month-first dates) when `ts_format` is left "auto". Cells separated by ";" or tabs, with decimal commas, are read
    (_dialect). A file without a ts column, or without a grid signal, is refused with the columns it has."""
    if len(content) > UPLOAD_MAX_BYTES:
        raise ValueError(TOO_LARGE)
    try:
        timezone(timedelta(hours=tz_offset_h))
    except (ValueError, OverflowError, TypeError):
        raise ValueError("The timezone offset is hours from UTC, between -23 and 23") from None
    lay = _layout(content, mapping, ts_format)
    ts_format, delim, decimal, at, scale, log = lay.ts_format, lay.delim, lay.decimal, lay.at, lay.scale, lay.log
    parsed, bad_ts, first_bad = _read_columns(content, delim, decimal, at, scale, tz_offset_h, ts_format)
    if not parsed.ts:
        raise ValueError(_no_times(first_bad, ts_format))
    now = _now_ts() if now is None else int(now)
    ts = np.frombuffer(parsed.ts, dtype=np.int64)
    _check_window(ts, now)  # before anything is written
    if bad_ts:
        log.append(f"{bad_ts} rows without a readable ts were skipped")
    ts, cols, bad, texts, duplicates = _in_order(ts, parsed)
    del parsed
    if duplicates:
        log.append(_repeats_note(duplicates))
    n = len(ts)
    warnings, volt_read = _grid_read(cols, bad, n)
    step, diffs, spans = _step_of(ts)
    if step > 1:
        log.append(_step_note(step))
    gaps = int(np.sum(diffs > max(5, step)))
    warnings += _check_values(ts, cols, spans)
    zone = _zone_warning(ts, cols, spans)
    if zone:
        warnings.append(zone)
    unreadable = int(sum(int(b.sum()) for b in bad.values()))
    del bad
    _grid_from_voltage(cols, volt_read, n)
    if step > 1:
        for c in INTERVAL_ENERGY:
            if c in cols:
                cols[c] /= step
    res = _write_days(site_id, ts, cols, texts, spans, now + AHEAD_S)
    if res["overlap"]:
        log.append(f"{res['overlap']:,} minutes were already stored: the file's values replaced theirs, and the columns the file does not carry (or "
                   f"leaves blank) were kept")
    if unreadable:
        log.append(f"{unreadable} cells were not finite numbers (text such as N/A, NaN or inf) or were beyond any meter's range "
                   f"(over a billion), and were stored as missing")
    log += warnings  # the imports row keeps them with the notes; the answer also lists them apart
    status = "done_with_warnings" if warnings else "done"
    digest = hashlib.sha256(content).hexdigest()[:16]
    first, last = int(ts[0]), int(ts[-1])
    name = _file_name(file_name)
    with store.connect() as con:
        _imports_columns(con)
        iid = store.new_id()
        con.execute("INSERT INTO imports (id, site_id, source, file_hash, rows, gaps, status, log, created, from_ts, to_ts, file_name) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, site_id, "csv", digest, n, gaps, status, json.dumps(log), store._now(), first, last, name))
    return {"id": iid, "rows": n, "skipped": bad_ts, "gaps": gaps, "log": log, "duplicates": duplicates, "minutes": res["accepted"],
            "step_min": step, "overlap": res["overlap"], "warnings": warnings, "status": status,
            "from": datetime.fromtimestamp(first, timezone.utc).isoformat(), "to": datetime.fromtimestamp(last, timezone.utc).isoformat(),
            "from_ts": first, "to_ts": last, "file_name": name}


# ------------------------------------------------------------------------------------------------ the upload's preview (UX backend ask K15)

#: The rows the preview answers: "HOW THE FIRST ROWS WILL BE READ" (plan C11.4)
PREVIEW_ROWS = 5
#: The keys a reading reads as true or false (on or off)
_STATES = ("grid_present", "genset_running")


def _whole_lines(head: str) -> str:
    """A file's head as the page sends it (its first 64 KB, perhaps cut in the middle of a line): the text after its last line break is read only
    when it has as many cells as the header, as the mapping guess reads a head (guess_mapping)."""
    text = head.lstrip("\ufeff")
    cut = max(text.rfind("\n"), text.rfind("\r"))
    if cut < 0 or cut == len(text) - 1:
        return text
    delim, _ = _dialect(text)
    header = next(csv.reader(io.StringIO(text), delimiter=delim), [])
    last = next(csv.reader([text[cut + 1:]], delimiter=delim), [])
    return text if len(last) >= len(header) else text[:cut + 1]


def _value(key: str, v):
    """A stored reading as the preview answers it: a state as true or false, a number to six decimals (not a float's noise; never -0), missing as
    None."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if key in _STATES:
        return bool(v >= 0.5)
    return round(float(v), 6) + 0.0 if isinstance(v, (int, float, np.floating)) else v     # + 0.0: a reversed 0 reads 0, not -0


def preview_csv(head: str, tz_offset_h: float = 0.0, mapping: dict | None = None, now: int | None = None) -> dict:
    """How the upload will read a file (UX backend ask K15; plan D9.5, C11.4 stage 2): its head read with `mapping` (mapping_of; None reads the columns
    by their names) and the time zone of its times (`tz_offset_h`), by the upload's own steps: refuse_named_twice, _layout, _read_columns, the window,
    _in_order, the grid signal, the step, _check_values and the voltage's grid state. Nothing is written. The first PREVIEW_ROWS rows are answered as
    they will be stored, in time order: `at` the minute the row is stored on, in Karachi time (ISO 8601, +05:00), and `values` each key read in its
    stored unit and the product's sign (battery
    discharge and grid import positive), the grid's and the genset's state as true or false, an unreadable or impossible reading None; an energy column
    is the row's own figure, which the upload spreads over the minutes the row stands for (`step_min`). With the keys read (in the file's order), the
    rows read and skipped (no readable time; the first such cell), the date order used, the header's notes and the readings' warnings, as the upload
    words them. What the upload refuses it refuses (ValueError) in the same words."""
    try:
        timezone(timedelta(hours=tz_offset_h))
    except (ValueError, OverflowError, TypeError):
        raise ValueError("The timezone offset is hours from UTC, between -23 and 23") from None
    content = _whole_lines(head).encode("utf-8")
    m = mapping_of(mapping) if mapping else None         # in the upload's order: its route reads the mapping, then the header's names, then the file
    refuse_named_twice(content)
    lay = _layout(content, m, "auto")
    log = list(lay.log)
    parsed, bad_ts, first_bad = _read_columns(content, lay.delim, lay.decimal, lay.at, lay.scale, tz_offset_h, lay.ts_format)
    if not parsed.ts:
        raise ValueError(_no_times(first_bad, lay.ts_format))
    ts = np.frombuffer(parsed.ts, dtype=np.int64)
    _check_window(ts, _now_ts() if now is None else int(now))
    read = len(ts)
    if bad_ts:
        log.append(f"{bad_ts} rows without a readable ts were skipped")
    ts, cols, bad, texts, duplicates = _in_order(ts, parsed)
    cols = {k: np.array(v) for k, v in cols.items()}    # a copy: the checks below blank impossible readings in place
    if duplicates:
        log.append(_repeats_note(duplicates))
    warnings, volt_read = _grid_read(cols, bad, len(ts))
    step, _, spans = _step_of(ts)
    if step > 1:
        log.append(_step_note(step))
    warnings += _check_values(ts, cols, spans)
    zone = _zone_warning(ts, cols, spans)
    if zone:
        warnings.append(zone)
    _grid_from_voltage(cols, volt_read, len(ts))
    keys = [k for k in lay.at if k not in ("ts", TS_TIME)]
    if "grid_present" in cols and "grid_present" not in keys:
        keys.insert(keys.index("grid_voltage_v") + 1 if "grid_voltage_v" in keys else 0, "grid_present")
    rows = []
    for i in range(min(PREVIEW_ROWS, len(ts))):
        values = {k: _value(k, cols[k][i] if k in cols else texts[k][i]) for k in keys}
        # the minute the upload stores the row on (_write_days), not the file's second (UX K15 check)
        rows.append({"at": datetime.fromtimestamp(int(ts[i]) // 60 * 60, PKT).isoformat(timespec="seconds"), "values": values})
    return {"rows": rows, "keys": keys, "read": read, "skipped": bad_ts, "first_unreadable": first_bad, "step_min": step, "ts_format": lay.ts_format,
            "tz_offset_h": tz_offset_h, "notes": log, "warnings": warnings}


# ------------------------------------------------------------------------------------------------ reading

def _finite(v):
    """A stored value for a JSON page: a non-finite float (written before ingest refused them) reads as missing."""
    return None if isinstance(v, float) and not math.isfinite(v) else v


def latest(site_id: str) -> dict | None:
    with reading() as con:
        r = con.execute("SELECT * FROM minute WHERE site_id = ? ORDER BY ts DESC LIMIT 1", (site_id,)).fetchone()
    return {k: _finite(v) for k, v in dict(r).items()} if r else None


def last_ts(site_ids) -> dict[str, int | None]:
    """Each site's newest minute (its ts, None with none) in one connection: the site list's last-data stamps (round 7, audit performance-live-08). One
    MAX(ts) a site, which the (site_id, ts) key answers from its last entry, where a GROUP BY would read every stored minute."""
    ids = list(dict.fromkeys(site_ids))
    if not ids:
        return {}
    with reading() as con:
        return {sid: con.execute("SELECT MAX(ts) FROM minute WHERE site_id = ?", (sid,)).fetchone()[0] for sid in ids}


def first_ts(site_id: str) -> int | None:
    with reading() as con:
        r = con.execute("SELECT MIN(ts) FROM minute WHERE site_id = ?", (site_id,)).fetchone()
    return r[0] if r else None


def battery_totals(site_id: str, t0: int, t1: int) -> dict:
    """The battery's figures over [t0, t1) from the stored minutes in one aggregate query (the battery page's month, round 7 pages-C-13): the energy
    discharged (kWh, as aggregate_day's discharge_kwh), the minutes with a battery power reading, a state-of-charge reading (the estimate, else the
    BMS) and a temperature reading, the minutes above 90 % charge and 35 °C (battery.state's hot-and-full rule), and the first minute that discharged.
    Each stored row is one minute (a coarser logger's rows are stored on the minutes they cover); a cell past CELL_MAX is no reading."""
    soc = "COALESCE(battery_soc_est_pct, battery_soc_bms_pct)"
    sql = ("SELECT SUM(CASE WHEN battery_power_w > 0 AND battery_power_w <= :m THEN battery_power_w END), "
           "COUNT(CASE WHEN ABS(battery_power_w) <= :m THEN 1 END), "
           f"COUNT(CASE WHEN ABS({soc}) <= :m THEN 1 END), "
           "COUNT(CASE WHEN ABS(battery_temp_c) <= :m THEN 1 END), "
           f"COUNT(CASE WHEN {soc} > 90 AND {soc} <= :m AND battery_temp_c > 35 AND battery_temp_c <= :m THEN 1 END), "
           "MIN(CASE WHEN battery_power_w > 0 AND battery_power_w <= :m THEN ts END) "
           "FROM minute WHERE site_id = :s AND ts >= :t0 AND ts < :t1")
    with reading() as con:
        r = con.execute(sql, {"m": CELL_MAX, "s": site_id, "t0": int(t0), "t1": int(t1)}).fetchone()
    return {"discharge_kwh": float(r[0] or 0.0) / 60000.0, "power_minutes": int(r[1]), "soc_minutes": int(r[2]), "temp_minutes": int(r[3]),
            "hot_full_minutes": int(r[4]), "first_discharge_ts": r[5]}


def soc_reading(site_id: str, ts: int) -> dict | None:
    """The stored state-of-charge readings of the minute starting at ts (a minute grid's cell: minute_grid's cells start where the caller's t0 put
    them, so the row is the one inside [ts, ts + 60)): which of the estimate and the BMS that minute holds."""
    with reading() as con:
        r = con.execute("SELECT battery_soc_est_pct, battery_soc_bms_pct FROM minute WHERE site_id = ? AND ts >= ? AND ts < ? ORDER BY ts LIMIT 1",
                        (site_id, int(ts), int(ts) + 60)).fetchone()
    return {"est": _finite(r[0]), "bms": _finite(r[1])} if r else None


def soc_source(site_id: str, state: dict | None) -> str | None:
    """Where battery.state's soc_pct came from (round 7 pages-C-14): "estimate" where its minute holds the estimate, "bms" where only the BMS read
    (a CSV history, a BMS-only source); the state service takes the estimate first and the BMS where it is missing, at the last minute with either,
    which need not be the newest row. The Battery page and the Overview's Battery tile mark the figure by it."""
    if not state or state.get("soc_pct") is None or not state.get("last_ts"):
        return None
    r = soc_reading(site_id, state["last_ts"])
    if not r:
        return None
    return "estimate" if r["est"] is not None else "bms" if r["bms"] is not None else None


#: the minute columns that hold text, not numbers
TEXT_COLUMNS = ("inverter_mode_active", "quality_flags")


def _read(site_id: str, t0: int, t1: int, columns) -> dict:
    """The minute rows of [t0, t1) of the named columns, as arrays: numbers as float (a blank or non-finite value NaN), text as lists. The numbers come
    from the database as one float block (a blank read as infinity, then NaN): about twice as fast as a list comprehension per column, which cost
    a tick about 0.05 s per 30 days per column set (round 7, audit tests-and-perf-09)."""
    nums = [c for c in columns if c not in TEXT_COLUMNS]
    text = [c for c in columns if c in TEXT_COLUMNS]
    where = "FROM minute WHERE site_id = ? AND ts >= ? AND ts < ? ORDER BY ts"
    args = (site_id, int(t0), int(t1))
    with reading() as con:
        cur = con.cursor()
        cur.row_factory = None
        cur.execute(f"SELECT {', '.join(['ts'] + [f'IFNULL({c}, 9e999)' for c in nums])} {where}", args)
        block = np.fromiter(cur, dtype=np.dtype((np.float64, 1 + len(nums))))
        rows = con.execute(f"SELECT {', '.join(text)} {where}", args).fetchall() if text else []
    out = {"ts": block[:, 0].astype(np.int64)}
    for i, c in enumerate(nums):
        a = np.ascontiguousarray(block[:, i + 1])
        a[~np.isfinite(a)] = np.nan  # a blank, or a non-finite value stored before ingest refused them, is missing
        out[c] = a
    for i, c in enumerate(text):
        out[c] = [r[i] for r in rows]
    return out


def raw(site_id: str, t0: int, t1: int, columns: tuple = COLUMNS[3:]) -> dict:
    """The minute rows of [t0, t1): `ts` and the named columns. Inside a `window` of the site that covers the range they come from the window's one
    read, not the file."""
    w = _window.get()
    if w is not None and w.covers(site_id, t0, t1):
        return w.raw(int(t0), int(t1), tuple(columns))
    return _read(site_id, t0, t1, columns)


class _Window:
    """A site's minute rows over [t0, t1), read from the file once per column (round 7, audit tests-and-perf-09: each tick read about four months
    of rows a site, 17 columns, in seven overlapping reads: the forecasters' 14, 21 and 28 days, the month to date and two 30-day battery
    estimates, the tick's and the alerts'). Every raw and minute_grid read of the site inside the range is a slice of it."""

    def __init__(self, site_id: str, t0: int, t1: int):
        self.site_id, self.t0, self.t1, self.path = site_id, int(t0), int(t1), store.telemetry_path()
        self.ts: np.ndarray | None = None
        self.cols: dict = {}
        self.reads = 0

    def covers(self, site_id: str, t0: int, t1: int) -> bool:
        return site_id == self.site_id and self.t0 <= int(t0) and int(t1) <= self.t1 and store.telemetry_path() == self.path

    def raw(self, t0: int, t1: int, columns: tuple) -> dict:
        missing = [c for c in columns if c not in self.cols]
        if missing or self.ts is None:
            # the columns asked for first, and the minute grid's with them: the forecasters, the battery and the bill all read those
            want = list(dict.fromkeys([*missing, *(c for c in GRID_COLUMNS if c not in self.cols)])) if self.ts is None else missing
            d = _read(self.site_id, self.t0, self.t1, want)
            self.reads += 1
            if self.ts is None:
                self.ts = d["ts"]
            elif len(d["ts"]) != len(self.ts) or not np.array_equal(d["ts"], self.ts):
                # rows arrived since the first read: the window starts again from this read, so its columns share one set of minutes
                self.ts, self.cols = d["ts"], {}
                return self.raw(t0, t1, columns)
            self.cols.update({c: d[c] for c in want})
        i, j = np.searchsorted(self.ts, [t0, t1], side="left")
        out = {"ts": self.ts[i:j].copy()}
        for c in columns:
            v = self.cols[c]
            out[c] = v[i:j].copy() if isinstance(v, np.ndarray) else v[i:j]
        return out


_window: "contextvars.ContextVar[_Window | None]" = contextvars.ContextVar("telemetry_window", default=None)


@contextmanager
def window(site_id: str, t0: int, t1: int):
    """Read the site's minute rows of [t0, t1) once (per column) for everything inside: the tick and the alerts after it (scheduler, the re-solve).
    A window already open for the site over the range is kept; another site's reads, or a range it does not cover, go to the file. What arrives
    while it is open is read only once a column not read yet is asked for (then the window reads again from the file)."""
    w = _window.get()
    if w is not None and w.covers(site_id, t0, t1):
        yield w
        return
    token = _window.set(_Window(site_id, t0, t1))
    try:
        yield _window.get()
    finally:
        _window.reset(token)


#: the columns a minute grid reads
GRID_COLUMNS = COLUMNS[3:] + ("censored",)


def minute_grid(site_id: str, t0: int, t1: int) -> dict:
    """Arrays on a complete 1-minute grid [t0, t1) in kW, with missing minutes as NaN; what the bill engine and the replay read.

    t1 is taken down to the last whole minute after t0, so a bound part-way through a minute (a page's "now") never places a row past the arrays.
    Grid import and export fall back to grid_power_w where a minute has neither energy column (a power-only logger), and the state of charge falls
    back to the BMS reading where the estimate is missing (a CSV source never carries the estimate); `soc_est` keeps the estimate alone."""
    t0 = int(t0)
    t1 = t0 + max(0, (int(t1) - t0) // 60) * 60
    d = raw(site_id, t0, t1, GRID_COLUMNS)
    n = (t1 - t0) // 60
    ts = np.arange(t0, t1, 60, dtype=np.int64)
    idx = ((d["ts"] - t0) // 60).astype(int)
    keep = (idx >= 0) & (idx < n)
    idx = idx[keep]
    out = {"ts": ts, "present": np.zeros(n, dtype=bool)}
    out["present"][idx] = True
    conv = {"imp": ("grid_import_wh", 60 / 1000), "exp": ("grid_export_wh", 60 / 1000), "pv": ("pv_power_w", 1 / 1000), "load": ("load_power_w", 1 / 1000),
            "bat": ("battery_power_w", 1 / 1000), "soc": ("battery_soc_est_pct", 1 / 100), "soc_bms": ("battery_soc_bms_pct", 1 / 100),
            "grid": ("grid_present", 1.0), "gen": ("genset_power_w", 1 / 1000), "gen_on": ("genset_running", 1.0), "fuel": ("genset_fuel_l", 1.0),
            "unserved": ("unserved_w", 1 / 1000), "temp": ("battery_temp_c", 1.0), "curtail": ("curtail_w", 1 / 1000),
            "gp": ("grid_power_w", 1 / 1000), "censored": ("censored", 1.0)}
    for k, (col, f) in conv.items():
        a = np.full(n, np.nan)
        a[idx] = d[col][keep] * f
        out[k] = a
    # a minute with neither energy column but a grid power reading: import and export from the power (positive is import)
    derive = np.isnan(out["imp"]) & np.isnan(out["exp"]) & ~np.isnan(out["gp"])
    out["imp"] = np.where(derive, np.maximum(out["gp"], 0.0), out["imp"])
    out["exp"] = np.where(derive, np.maximum(-out["gp"], 0.0), out["exp"])
    out["soc_est"] = out["soc"]
    out["soc"] = np.where(np.isnan(out["soc_est"]), out["soc_bms"], out["soc_est"])
    modes = [None] * n
    for j, m in zip(idx, [m for m, k in zip(d["inverter_mode_active"], keep) if k]):
        modes[j] = m
    out["mode"] = modes
    out["completeness"] = float(out["present"].mean()) if n else 0.0
    out["imp_completeness"] = float(np.mean(~np.isnan(out["imp"]))) if n else 0.0
    return out


def filled(g: dict) -> dict:
    """The replay's inputs from a minute grid: gaps carried forward (grid assumed present when unknown), energy blanks as zero."""
    out = dict(g)
    for k in ("pv", "load", "imp", "exp", "bat", "gen", "fuel", "unserved", "curtail"):
        out[k] = np.nan_to_num(g[k], nan=0.0)
    grid = g["grid"].copy()
    last = 1.0
    for i in range(len(grid)):
        if np.isnan(grid[i]):
            grid[i] = last
        else:
            last = grid[i]
    out["grid"] = grid
    return out


SERIES_KIND = {"grid_import_wh": "sum", "grid_export_wh": "sum", "genset_fuel_l": "sum", "grid_present": "min"}


#: a chart series answers at most this many buckets (plan Part 9 and the telemetry.py row of Part 11: about 2,000 points per series; R7 section 8)
SERIES_MAX_POINTS = 2000
#: the resolutions a series is read at, finest first, with their bucket in seconds (R8's steps; 1d buckets are Karachi days)
RESOLUTIONS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}


def _buckets(t0: int, t1: int, step: int, off: int) -> int:
    """The buckets of `step` seconds (shifted back by `off`) that [t0, t1) touches: a range starting part-way through a bucket touches one more."""
    return 0 if t1 <= t0 else (int(t1) - 1 + off) // step - (int(t0) + off) // step + 1


def series(site_id: str, t0: int, t1: int, names: list[str], res: str = "auto", max_points: int = SERIES_MAX_POINTS) -> dict:
    """Chart series at a resolution of RESOLUTIONS. "auto" is the finest whose buckets over [t0, t1) stay within max_points; an explicit resolution
    that would answer more is coarsened to that one, whoever asks (audit R7 tests-and-perf-05: 400 days at 1 minute built some 576,000 buckets of
    every series; performance-live-13: 2 days at 1 minute gave 2,880), and 1d is never coarsened. The answer gives the resolution used, the one asked
    (`requested_resolution`) and the cap (`max_points`)."""
    if res != "auto" and res not in RESOLUTIONS:
        raise ValueError(f"The resolution is auto or one of {', '.join(RESOLUTIONS)}")
    asked = res
    finest = 0 if res == "auto" else list(RESOLUTIONS).index(res)
    res = next((r for r in list(RESOLUTIONS)[finest:] if _buckets(t0, t1, RESOLUTIONS[r], 5 * 3600 if r == "1d" else 0) <= max_points), "1d")
    step = RESOLUTIONS[res]
    names = [n for n in names if n in NUMERIC] or ["pv_power_w", "load_power_w", "grid_power_w", "battery_power_w", "battery_soc_est_pct"]
    off = 5 * 3600 if res == "1d" else 0
    parts = []
    for n in names:
        kind = SERIES_KIND.get(n, "avg")
        fn = "SUM" if kind == "sum" else "MIN" if kind == "min" else "AVG"
        parts.append(f"{fn}({n})")
    sql = (f"SELECT ((ts + {off}) / {step}) * {step} - {off} AS b, {', '.join(parts)} FROM minute WHERE site_id = ? AND ts >= ? AND ts < ? "
           f"GROUP BY b ORDER BY b")
    with reading() as con:
        rows = con.execute(sql, (site_id, int(t0), int(t1))).fetchall()
    return {"resolution": res, "requested_resolution": asked, "max_points": int(max_points), "ts": [r[0] for r in rows],
            "series": {n: [None if r[i + 1] is None or not math.isfinite(float(r[i + 1])) else round(float(r[i + 1]), 3) for r in rows]
                       for i, n in enumerate(names)}}


def heatmap(site_id: str, name: str, days: int = 28) -> dict:
    """Weekday by hour mean of a series over the last days (local time)."""
    t1 = int(datetime.now(timezone.utc).timestamp())
    t0 = t1 - days * 86400
    if name not in NUMERIC:
        name = "load_power_w"
    sql = (f"SELECT ((ts + 18000) / 86400 + 3) % 7 AS dow, ((ts + 18000) % 86400) / 3600 AS h, AVG({name}) FROM minute WHERE site_id = ? AND ts >= ? "
           f"GROUP BY dow, h")
    grid = [[None] * 24 for _ in range(7)]
    with reading() as con:
        for dow, h, v in con.execute(sql, (site_id, t0)).fetchall():
            grid[int(dow)][int(h)] = None if v is None or not math.isfinite(float(v)) else round(v, 2)
    # rows Monday..Sunday: 1970-01-01 was a Thursday, so (days since the epoch + 3) % 7 already gives 0 = Monday and 6 = Sunday, as in
    # twin.load_template and forecast._day_type
    return {"series": name, "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "values": grid}


# ------------------------------------------------------------------------------------------------ genset runs and outages, in minutes

#: A genset run or an outage goes on across rows that carry no reading of its signal (a blank cell, a lost row, the minutes between a 5- or 15-minute
#: source's rows) while two of its readings are at most RUN_GAP_S apart (two of the source's own intervals, when longer): a missing signal is unknown, not
#: off (plan 7.5, U3 3.7), as the forecaster's outage runs bridge them (forecast.OUTAGE_GAP_MINUTES). A reading that says otherwise (grid up, set off)
#: ends it. Each row stands for the seconds to the next row, at most the source's own interval, so a coarse source's minutes, energy and litres are
#: counted in full (audit R7 pages-C-04: they were counted per row, a blank row ended a run or an outage).
RUN_GAP_S = 15 * 60


def sample_seconds(ts) -> int:
    """The seconds one row of a source stands for: the median gap between its rows (60 for a 1-minute source, 300 for a 5-minute one), from 60 to
    RUN_GAP_S (as forecast.sample_minutes)."""
    ts = np.asarray(ts, dtype=np.int64)
    if len(ts) < 2:
        return 60
    return int(min(RUN_GAP_S, max(60, round(float(np.median(np.diff(ts))) / 60) * 60)))


def _stretches(ts: np.ndarray, known: np.ndarray, on: np.ndarray, step: int, t1: int) -> list[tuple[int, int, bool]]:
    """(first row, last row, ongoing) of each stretch of `on` readings: rows without a reading between two `on` readings at most RUN_GAP_S (or two
    steps) apart continue it, a known not-`on` reading ends it. `ongoing`: nothing has ended it and its last reading is recent (within the gap of t1)."""
    idx = np.flatnonzero(known & on)
    if not len(idx):
        return []
    offs = np.cumsum(known & ~on)  # known not-on readings up to and including each row
    gap = max(RUN_GAP_S, 2 * step)
    brk = (np.diff(ts[idx]) > gap) | (offs[idx[1:]] != offs[idx[:-1]])
    starts = idx[np.concatenate([[True], brk])]
    ends = idx[np.concatenate([brk, [True]])]
    out = []
    for s, e in zip(starts.tolist(), ends.tolist()):
        ended = bool(offs[-1] != offs[e]) or int(t1) - int(ts[e]) > gap
        out.append((s, e, not ended))
    return out


def _spans(ts: np.ndarray, s: int, e: int, step: int) -> tuple[np.ndarray, np.ndarray]:
    """Seconds each row s..e of a stretch stands for (to the next row; the last row its own interval, at most the next row) and the part of it the
    row's own reading covers (at most the source's interval: the rest is a gap the stretch bridges)."""
    dur = np.empty(e - s + 1, dtype=float)
    dur[:-1] = np.diff(ts[s:e + 1])
    dur[-1] = min(step, int(ts[e + 1] - ts[e])) if e + 1 < len(ts) else step
    return dur, np.minimum(dur, step)


def _held(a: np.ndarray, both: bool = False) -> np.ndarray:
    """A stretch's readings with each blank carried from the reading before it (and, with `both`, leading blanks from the first reading)."""
    a = np.asarray(a, dtype=float).copy()
    if not len(a):
        return a
    pos = np.arange(len(a))
    last = np.maximum.accumulate(np.where(~np.isnan(a), pos, -1))
    fill = np.isnan(a) & (last >= 0)
    a[fill] = a[last[fill]]
    if both and np.isnan(a[0]) and (~np.isnan(a)).any():
        a[:int(np.flatnonzero(~np.isnan(a))[0])] = a[~np.isnan(a)][0]
    return a


def _genset_state(d: dict) -> tuple[np.ndarray, np.ndarray]:
    """(known, running) per row, as the savings' actual arm reads the set (verify._actual_sim): the running flag, else its power, else its litres. A row
    with none of the three is unknown, not off."""
    flag, p, fuel = d["genset_running"], d["genset_power_w"], d["genset_fuel_l"]
    known = ~np.isnan(flag) | ~np.isnan(p) | ~np.isnan(fuel)
    on = np.where(~np.isnan(flag), flag > 0.5, np.where(~np.isnan(p), p > 0.0, np.nan_to_num(fuel) > 0.0))
    return known, on & known


def _local_day(ts) -> np.ndarray:
    return (np.asarray(ts, dtype=np.int64) + 5 * 3600) // 86400


def _grid_stretches(d: dict, step: int, t1: int) -> list[tuple[int, int, bool]]:
    grid = d["grid_present"]
    known = ~np.isnan(grid)
    return _stretches(d["ts"], known, known & (grid < 0.5), step, t1)


#: the columns a genset run reads: the set's own and the grid's (outages); and, only for a run with no output read, the site's balance
_RUN_COLUMNS = ("genset_running", "genset_power_w", "genset_fuel_l", "grid_present")
_BALANCE_COLUMNS = ("load_power_w", "pv_power_w", "battery_power_w", "grid_power_w", "grid_import_wh", "grid_export_wh")


def _at(d: dict, ts: np.ndarray) -> dict:
    """raw()'s numeric columns at the times `ts` (NaN where `d` has no row at that time): a second read lined up with the first by minute."""
    pos = np.minimum(np.searchsorted(d["ts"], ts), max(0, len(d["ts"]) - 1))
    hit = (pos < len(d["ts"])) & (d["ts"][pos] == ts) if len(d["ts"]) else np.zeros(len(ts), dtype=bool)
    out = {"ts": np.asarray(ts)}
    for c, v in d.items():
        if c != "ts":
            a = np.full(len(ts), np.nan)
            a[hit] = v[pos[hit]]
            out[c] = a
    return out


def genset_runs(site_id: str, t0: int, t1: int, genset: dict) -> dict:
    """The genset's runs in [t0, t1), oldest first, counted in minutes (see RUN_GAP_S), with their litres as the Savings page reads them
    (verify.litres_used, plan 8.3: metered where a fuel meter reads, else the fuel curve a x output + b x rating, the output read, else held from the
    run's last reading, else the site's balance within the set's range). Answers:

    - `signal` {present, last_seen, running}: whether any row of the period read the set (its running flag, power or litres), the last such row and what
      it said; without one the set's state is unknown, not off (plan 7.5);
    - `runs[]` {start, end, hours, kwh, kwh_estimated (some output from the balance), litres, litres_metered, litres_curve, litres_source ("meter", or
      "curve" when any of its litres come from the curve), load_ratio, hours_below_min (hours whose read output was under f_min of the rating),
      in_outage (it overlapped an outage), ongoing?};
    - `daily[]` {day, hours, litres}: per Karachi day of the period, null on a day without a genset reading;
    - `fuel_read`: whether any row read the fuel meter (a period of metered zeros is "meter", not unknown)."""
    s_kw = float(genset.get("rated_kw") or 0.0)
    a, b, f_min = float(genset.get("a") or 0.0), float(genset.get("b") or 0.0), float(genset.get("f_min") or 0.0)
    d = raw(site_id, t0, t1, _RUN_COLUMNS)
    ts = d["ts"]
    known, on = _genset_state(d)
    step = sample_seconds(ts)
    outs = [(int(ts[s]), int(ts[e]) + int(_spans(ts, s, e, step)[0][-1])) for s, e, _ in _grid_stretches(d, step, t1)]
    p_kw = d["genset_power_w"] / 1000.0
    fuel = d["genset_fuel_l"]
    first_day, last_day = int(_local_day([t0])[0]), int(_local_day([max(t0, t1 - 1)])[0])
    n_days = last_day - first_day + 1
    day_hours, day_litres = np.zeros(n_days), np.zeros(n_days)
    runs = []
    for s, e, ongoing in _stretches(ts, known, on, step, t1):
        dur, own = _spans(ts, s, e, step)
        power = _held(p_kw[s:e + 1], both=True)
        balanced = np.isnan(power)
        if balanced.any():
            # no output read in the run: the load the grid, PV and battery leave over, within the set's range (as verify._actual_sim); the run's own
            # minutes, matched by time, as a minute written between the two reads would otherwise misalign the arrays (review r7: a 500)
            bd = _at(raw(site_id, int(ts[s]), int(ts[e]) + 1, _BALANCE_COLUMNS), ts[s:e + 1])
            grid_kw = np.where(~np.isnan(bd["grid_power_w"]), bd["grid_power_w"] / 1000.0,
                               (np.nan_to_num(bd["grid_import_wh"]) - np.nan_to_num(bd["grid_export_wh"])) * 3.6 / own)
            bal = (np.nan_to_num(bd["load_power_w"]) - np.nan_to_num(bd["pv_power_w"]) - np.nan_to_num(bd["battery_power_w"])) / 1000.0
            est = np.clip(bal - np.nan_to_num(grid_kw), f_min * s_kw, s_kw) if s_kw > 0 else np.zeros(len(bal))
            power = np.where(balanced, est, power)
        metered_row = ~np.isnan(fuel[s:e + 1])
        metered = float(np.nansum(fuel[s:e + 1]))
        curve_s = dur - np.where(metered_row, own, 0.0)
        curve_rows = (a * power + b * s_kw) * curve_s / 3600.0
        curve = float(curve_rows.sum())
        litres_rows = np.where(metered_row, np.nan_to_num(fuel[s:e + 1]), 0.0) + curve_rows
        start = int(ts[s])
        end = int(ts[e]) + int(dur[-1])
        kwh = float(np.sum(power * dur) / 3600.0)
        hours = (end - start) / 3600.0
        below = float(np.sum(dur[~balanced & (power < f_min * s_kw - 1e-9)]) / 3600.0) if s_kw > 0 else 0.0
        run = {"start": start, "end": end, "hours": round(hours, 2), "kwh": round(kwh, 1), "kwh_estimated": bool(balanced.any()),
               "litres": round(metered + curve, 1), "litres_metered": round(metered, 1), "litres_curve": round(curve, 1),
               "litres_source": "curve" if curve > 1e-9 else "meter", "load_ratio": round(kwh / max(1e-6, hours * s_kw), 2) if s_kw > 0 else None,
               "hours_below_min": round(below, 2), "in_outage": any(os < end and start < oe for os, oe in outs)}
        if ongoing:
            run["ongoing"] = True
        runs.append(run)
        days = _local_day(ts[s:e + 1]) - first_day
        ok = (days >= 0) & (days < n_days)
        np.add.at(day_hours, days[ok], dur[ok] / 3600.0)
        np.add.at(day_litres, days[ok], litres_rows[ok])
    seen = set((_local_day(ts[known]) - first_day).tolist())
    daily = [{"day": date.fromordinal(date(1970, 1, 1).toordinal() + first_day + i).isoformat(),
              "hours": round(float(day_hours[i]), 2) if i in seen else None, "litres": round(float(day_litres[i]), 1) if i in seen else None}
             for i in range(n_days)]
    last = int(np.flatnonzero(known)[-1]) if known.any() else None
    signal = {"present": last is not None, "last_seen": int(ts[last]) if last is not None else None,
              "running": bool(on[last]) if last is not None else None}
    return {"signal": signal, "runs": runs, "daily": daily, "fuel_read": bool((~np.isnan(fuel)).any())}


def outages(site_id: str, t0: int, t1: int) -> list[dict]:
    """Outage intervals from the grid-present flag, counted in minutes (see RUN_GAP_S): a gap in the data is not an outage, and a blank reading does not
    end one. `genset_minutes` are the minutes the genset's runs (as genset_runs reads them) overlap the outage; `unserved_kwh` holds each reading over the
    minutes its row stands for."""
    d = raw(site_id, t0, t1, ("grid_present", "battery_soc_est_pct", "battery_soc_bms_pct", "genset_running", "genset_power_w", "genset_fuel_l",
                              "unserved_w"))
    ts = d["ts"]
    if not len(ts):
        return []
    step = sample_seconds(ts)
    soc = np.where(np.isnan(d["battery_soc_est_pct"]), d["battery_soc_bms_pct"], d["battery_soc_est_pct"])  # the BMS where there is no estimate
    known, on = _genset_state(d)
    gen = [(int(ts[s]), int(ts[e]) + int(_spans(ts, s, e, step)[0][-1])) for s, e, _ in _stretches(ts, known, on, step, t1)]
    out = []
    for s, e, ongoing in _grid_stretches(d, step, t1):
        dur, _own = _spans(ts, s, e, step)
        start, end = int(ts[s]), int(ts[e]) + int(dur[-1])
        unserved = np.nan_to_num(_held(d["unserved_w"][s:e + 1]))
        o = {"start": start, "end": end, "minutes": (end - start) // 60,
             "soc_start_pct": None if np.isnan(soc[s]) else round(float(soc[s]), 1),
             "genset_minutes": int(sum(max(0, min(end, ge) - max(start, gs)) for gs, ge in gen) // 60),
             "unserved_kwh": round(float(np.sum(unserved * dur)) / 3.6e6, 2)}
        if ongoing:
            o["ongoing"] = True
        out.append(o)
    return out


def aggregate_day(site_id: str, day: date, peak_flags: np.ndarray | None = None, keep: dict | None = None) -> dict:
    """The day's totals into telemetry_days (plan Part 3.3). `keep` carries a stored row's fields this call does not compute (peak_import_kwh without
    the peak flags), so re-aggregating a day does not drop them; non-finite kept values are dropped. A total that is not finite (minutes stored
    before ingest capped magnitudes can overflow a sum) is stored and returned as missing (None), so no day row is ever written non-finite and
    days() never finds one to heal again."""
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    g = minute_grid(site_id, t0, t0 + 86400)
    if not g["present"].any():
        return {}
    f = filled(g)
    imp = f["imp"]
    blocks = imp.reshape(48, 30).mean(axis=1)
    data = {"import_kwh": round(float(imp.sum() / 60), 2), "export_kwh": round(float(f["exp"].sum() / 60), 2),
            "pv_kwh": round(float(f["pv"].sum() / 60), 2), "load_kwh": round(float(f["load"].sum() / 60), 2),
            "genset_kwh": round(float(f["gen"].sum() / 60), 2), "genset_hours": round(float(np.nansum(g["gen_on"]) / 60), 2),
            "litres": round(float(f["fuel"].sum()), 2), "peak_kw": round(float(blocks.max()), 2),
            "min_soc": None if np.all(np.isnan(g["soc"])) else round(float(np.nanmin(g["soc"]) * 100), 1),
            "max_soc": None if np.all(np.isnan(g["soc"])) else round(float(np.nanmax(g["soc"]) * 100), 1),
            "throughput_kwh": round(float(np.nansum(np.abs(np.minimum(g["bat"], 0))) / 60), 2),
            "discharge_kwh": round(float(np.nansum(np.maximum(g["bat"], 0)) / 60), 2),
            "outage_minutes": int(np.sum(g["grid"] == 0)), "unserved_kwh": round(float(f["unserved"].sum() / 60), 2),
            "completeness": round(g["completeness"], 4), "import_completeness": round(g["imp_completeness"], 4),
            # the newest minute in the row, so a reader of today's row knows when a newer one has arrived (round 7, audit tests-and-perf-10)
            "last_ts": int(g["ts"][np.flatnonzero(g["present"])[-1]])}
    if peak_flags is not None:
        data["peak_import_kwh"] = round(float(imp[peak_flags].sum() / 60), 2)
    if keep:
        data = {**{k: v for k, v in keep.items() if k != "day" and k not in data and _finite(v) is not None}, **data}
    data = {k: _finite(v) for k, v in data.items()}
    with store.connect() as con:
        con.execute("INSERT INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?) ON CONFLICT(site_id, day_local) DO UPDATE SET data = excluded.data",
                    (site_id, day.isoformat(), json.dumps(data)))
    return data


def _heal_day(site_id: str, day_local: str, stored: dict) -> dict:
    """A stored day row holding a non-finite number (written before ingest refused them): re-aggregated from the minutes, which now read such values as
    missing, and written back, keeping the fields the aggregation does not compute; a total that still overflows is written as missing (null), as
    aggregate_day stores every non-finite total. Without minutes to re-read, the non-finite values are stored as missing (null). Either way the row is
    healed once, not on every read."""
    try:
        day = date.fromisoformat(day_local)
    except ValueError:
        day = None
    fresh = aggregate_day(site_id, day, keep=stored) if day is not None else {}
    if fresh:
        return fresh
    clean = {k: _finite(v) for k, v in stored.items()}
    with store.connect() as con:
        con.execute("UPDATE telemetry_days SET data = ? WHERE site_id = ? AND day_local = ?", (json.dumps(clean), site_id, day_local))
    return clean


def _non_finite(data: dict) -> bool:
    return any(isinstance(v, float) and not math.isfinite(v) for v in data.values())


def days(site_id: str, d0: str, d1: str) -> list[dict]:
    """The stored day rows from d0 to d1 (local days, inclusive). A row holding Infinity or NaN is healed on the way out (see _heal_day), so every number
    returned is finite."""
    with store.connect() as con:
        rows = con.execute("SELECT day_local, data FROM telemetry_days WHERE site_id = ? AND day_local >= ? AND day_local <= ? ORDER BY day_local",
                           (site_id, d0, d1)).fetchall()
    out = []
    for r in rows:
        data = json.loads(r["data"])
        if _non_finite(data):
            data = _heal_day(site_id, r["day_local"], data)
        out.append({"day": r["day_local"], **{k: _finite(v) for k, v in data.items()}})
    return out


#: today's share of minutes is counted from Karachi midnight up to this long ago: a source that sends its minutes in batches is not counted missing for the
#: minutes still on their way
QUALITY_GRACE_S = 900


def quality_days(site_id: str, d0: date, d1: date, now: int) -> list[dict]:
    """Each stored day's completeness from d0 to d1 (local days, inclusive), for the data-quality strips (role-viewer-07). A past day's share is of its
    1,440 minutes; today's is of the minutes elapsed so far (from midnight to QUALITY_GRACE_S ago, at least one), marked `in_progress`, so a healthy
    feed is 100 % at noon instead of 50 %, and a feed that stopped falls as the day goes on. A site with earlier data that has sent nothing yet today
    shows today at 0 % once the grace has passed."""
    out = [{"day": r["day"], "completeness": r.get("completeness")} for r in days(site_id, d0.isoformat(), d1.isoformat())]
    now = int(now)
    day = datetime.fromtimestamp(now, PKT).date()
    if not d0 <= day <= d1:
        return out
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    upto = (now - QUALITY_GRACE_S) // 60 * 60
    row = next((r for r in out if r["day"] == day.isoformat()), None)
    if row is None:
        first = first_ts(site_id)
        if first is None or first >= t0 or upto <= t0:
            return out
        row = {"day": day.isoformat()}
        out.append(row)
    upto = max(upto, t0 + 60)
    with reading() as con:
        n = con.execute("SELECT COUNT(*) FROM minute WHERE site_id = ? AND ts >= ? AND ts < ?", (site_id, t0, upto)).fetchone()[0]
    row["completeness"] = round(min(1.0, n / ((upto - t0) // 60)), 4)
    row["in_progress"] = True
    return out


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(v):
    """An exported cell: blank for missing, and a text cell that a spreadsheet would read as a formula quote-prefixed (plan Part 2.8)."""
    if v is None:
        return ""
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


def export_csv(site_id: str, t0: int, t1: int) -> str:
    """The stored minutes of [t0, t1) as CSV: first the minute in Karachi time, ISO 8601 with its offset ("2026-11-11T14:16:00+05:00"), then the same
    minute in UTC for machines (`ts_utc`, as before), then the stored series (UX backend ask K25, plan E6 item 7: the owner's rule of Pakistan time for
    every time meant for people, and [req] 1's ISO 8601 with offset; it wrote UTC only). The rows are fetched in one read and formatted after it has
    closed, so the formatting (about half a second for 32 days) holds no transaction open (performance-live-03)."""
    with reading() as con:
        rows = con.execute(f"SELECT ts, {', '.join(COLUMNS[3:])} FROM minute WHERE site_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                           (site_id, int(t0), int(t1))).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts_karachi", "ts_utc"] + list(COLUMNS[3:]))
    for r in rows:
        w.writerow([datetime.fromtimestamp(r[0], PKT).isoformat(), datetime.fromtimestamp(r[0], timezone.utc).isoformat().replace("+00:00", "Z")]
                   + [_csv_cell(_finite(v)) for v in r[1:]])
    return buf.getvalue()


def delete_site(site_id: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM minute WHERE site_id = ?", (site_id,))
        con.execute("DELETE FROM forecast_issues WHERE site_id = ?", (site_id,))


# ------------------------------------------------------------------------------------------------ forecast issues

#: forecast issues are kept whole for ISSUE_ALL_DAYS, the hourly ones for ISSUE_HOURLY_DAYS (the stage-1 bands read the last 16 days' issues, the night's
#: scoring the last 3), then only each Karachi day's day-ahead issue: the latest at or before its midnight and at most DAY_AHEAD_S before it, which the
#: day's scoring reads (forecast.score_day). An issue is about 27 KB a site for its three kinds; kept hourly for ever they came to about 240 MB a
#: site-year, four times the minute rows (audit R7 performance-live-06); kept so, about 30 MB a site plus 10 MB a site-year.
ISSUE_ALL_DAYS = 3
ISSUE_HOURLY_DAYS = 35
DAY_AHEAD_S = 6 * 3600
#: the seconds from an issue's belief time to the Karachi midnight at or after it
_TO_MIDNIGHT = "((86400 - (belief_time + 18000) % 86400) % 86400)"


def save_forecast(site_id: str, kind: str, belief_time: int, stage: int, version: str, body: dict) -> None:
    """Saves an issue and prunes the site's issues of that kind by age from this one's belief time (ISSUE_ALL_DAYS, ISSUE_HOURLY_DAYS; see there)."""
    bt = int(belief_time)
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO forecast_issues (site_id, kind, belief_time, stage, model_version, body) VALUES (?,?,?,?,?,?)",
                    (site_id, kind, bt, stage, version, json.dumps(body)))
        con.execute("DELETE FROM forecast_issues WHERE site_id = ? AND kind = ? AND belief_time < ? AND belief_time % 3600 != 0",
                    (site_id, kind, bt - ISSUE_ALL_DAYS * 86400))
        later = (f"SELECT 1 FROM forecast_issues AS later WHERE later.site_id = forecast_issues.site_id AND later.kind = forecast_issues.kind "
                 f"AND later.belief_time > forecast_issues.belief_time AND later.belief_time <= forecast_issues.belief_time + "
                 f"{_TO_MIDNIGHT.replace('belief_time', 'forecast_issues.belief_time')}")
        con.execute(f"DELETE FROM forecast_issues WHERE site_id = ? AND kind = ? AND belief_time < ? "
                    f"AND NOT ({_TO_MIDNIGHT} <= ? AND NOT EXISTS ({later}))",
                    (site_id, kind, bt - ISSUE_HOURLY_DAYS * 86400, DAY_AHEAD_S))


def latest_forecast(site_id: str, kind: str, at_or_before: int | None = None) -> dict | None:
    with reading() as con:
        if at_or_before is None:
            r = con.execute("SELECT * FROM forecast_issues WHERE site_id = ? AND kind = ? ORDER BY belief_time DESC LIMIT 1", (site_id, kind)).fetchone()
        else:
            r = con.execute("SELECT * FROM forecast_issues WHERE site_id = ? AND kind = ? AND belief_time <= ? ORDER BY belief_time DESC LIMIT 1",
                            (site_id, kind, int(at_or_before))).fetchone()
    if r is None:
        return None
    return {"belief_time": r["belief_time"], "stage": r["stage"], "model_version": r["model_version"], **json.loads(r["body"])}


def next_seq(site_id: str, stream: str, seq: int) -> bool:
    """Idempotency by (site, stream, sequence): True if the sequence is new."""
    with connect() as con:
        r = con.execute("SELECT last FROM seq WHERE site_id = ? AND stream = ?", (site_id, stream)).fetchone()
        if r and seq <= r["last"]:
            return False
        con.execute("INSERT INTO seq (site_id, stream, last) VALUES (?,?,?) ON CONFLICT(site_id, stream) DO UPDATE SET last = excluded.last",
                    (site_id, stream, seq))
    return True
