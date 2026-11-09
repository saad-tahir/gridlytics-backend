"""The company's telemetry file (plan Part 3.3): 1-minute rows per site, forecast issues, and the readers the engine and the pages use.

Signs as OpenEMS (battery discharge positive, charge negative, grid import positive); names as SunSpec where one exists. Timestamps are epoch seconds UTC
of each minute's start. Blanks are missing, never zero; a missing genset signal is unknown, not off.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import sqlite3
import threading
import time
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


#: the columns that hold a row's energy over its interval, not a reading at its start: a coarse row's value is spread evenly over the minutes it covers
INTERVAL_ENERGY = ("grid_import_wh", "grid_export_wh", "genset_fuel_l")
_ENERGY_AT = tuple(ALL.index(c) for c in INTERVAL_ENERGY)


def ingest(site_id: str, records: list[dict], source: str, now: int | None = None, count_overlap: bool = False,
           spans: list[int] | None = None, step_min: int = 1) -> dict:
    """Normalised dict rows (edge batches and CSV) into the minute table; idempotent by (site, minute), the last value wins.

    A stored minute is merged, never replaced whole: a column the row leaves missing (absent, blank or unreadable) keeps its stored value, so a CSV that
    carries only the load never erases the PV, the battery or an edge batch's own columns for the minutes it overlaps (audit R7). With `count_overlap`
    the answer also gives `overlap`, the minutes that were already stored.

    `spans` (a CSV of 5- or 15-minute rows, audit R7) gives each record the minutes it covers from its own: the row is stored on each of them, its
    readings held and its interval energy (INTERVAL_ENERGY) divided by `step_min`, the file's interval, so a day's energy, its peak and its outage minutes
    come out as from 1-minute rows. `accepted` counts the minutes written.

    A row whose ts is not a time, or lies before 2015 or more than a day after `now`, is skipped and counted (`skipped`, and `out_of_range` for the
    dated ones), never raised: one bad row must not fail the batch on every retry."""
    rows, skipped, unreadable, out_of_range = [], 0, 0, 0
    latest_ok = (_now_ts() if now is None else int(now)) + AHEAD_S
    for n, r in enumerate(records):
        ts = _epoch(r.get("ts"))
        if ts is None:
            skipped += 1
            continue
        if not EARLIEST_TS <= ts <= latest_ok:
            skipped += 1
            out_of_range += 1
            continue
        ts = ts // 60 * 60
        row = [site_id, ts, source]
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
        span = max(1, int(spans[n])) if spans is not None else 1
        if step_min > 1:
            for j in _ENERGY_AT:
                if row[j] is not None:
                    row[j] = row[j] / step_min
        for k in range(span):
            if k and ts + 60 * k > latest_ok:
                break
            row[1] = ts + 60 * k
            rows.append(tuple(row))
    res = {"accepted": len(rows), "skipped": skipped, "unreadable_cells": unreadable, "out_of_range": out_of_range}
    if count_overlap:
        res["overlap"] = 0
    if not rows:
        return res
    with connect() as con:
        if count_overlap:
            stamps = {r[1] for r in rows}
            res["overlap"] = sum(1 for (t,) in con.execute("SELECT ts FROM minute WHERE site_id = ? AND ts >= ? AND ts <= ?",
                                                           (site_id, min(stamps), max(stamps))) if t in stamps)
        con.executemany(_insert_sql(ALL, merge=True), rows)
    return res


def _check_window(stamps: list[int], now: int | None = None) -> None:
    """Refuses (ValueError, in words) parsed upload times with a row before 2015, a row more than a day ahead, or a span over UPLOAD_MAX_DAYS."""
    latest_ok = (_now_ts() if now is None else int(now)) + AHEAD_S
    early = sum(1 for t in stamps if t < EARLIEST_TS)
    late = sum(1 for t in stamps if t > latest_ok)
    if early or late:
        raise ValueError(f"{early + late} row{'s are' if early + late != 1 else ' is'} dated "
                         + ("before 2015" if early and not late else "in the future" if late and not early else "before 2015 or in the future")
                         + ": check the logger's clock (and the timezone) and upload again")
    span = (max(stamps) - min(stamps)) // 86400 if stamps else 0
    if span > UPLOAD_MAX_DAYS:
        raise ValueError(f"The file spans {span} days; upload at most {UPLOAD_MAX_DAYS} days per file")


def _local_days(stamps) -> list[date]:
    """The local (PKT) days that hold at least one of the times, in order."""
    ks = sorted({(int(t) + 5 * 3600) // 86400 for t in stamps})
    epoch = date(1970, 1, 1)
    return [epoch + timedelta(days=k) for k in ks]


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


def import_csv(site_id: str, content: bytes, tz_offset_h: float = 0.0, mapping: dict | None = None, now: int | None = None,
               file_name: str | None = None) -> dict:
    """A CSV upload: one row per minute; blanks are missing; duplicate times keep the last row and are counted once (`rows` is the distinct rows kept,
    `duplicates` the rows dropped, both in the notes); rows 2 to 60 minutes apart are stored as the minutes each covers (`step_min`, `minutes`; see
    ingest), rows further apart are refused; gaps over 5 minutes (or over the step) are reported. A file in which no row's grid_present (or
    grid_voltage_v) can be read is refused; on and off, true and false, yes and no are read as 1 and 0. What may make the import wrong is listed in
    `warnings` (also at the end of `log`), and the import's `status` is then "done_with_warnings", not "done".

    The parsed rows must lie between 2015 and a day after `now` and span at most UPLOAD_MAX_DAYS, or the whole file is refused (ValueError) and
    nothing is stored; the check reads the rows exactly as the import does (a repeated column keeps its last cell). Only the local days that received
    rows are aggregated. The imports row keeps the uploaded range (`from_ts`, `to_ts`) and the file's name when given (`file_name`, see _file_name)."""
    if len(content) > 30_000_000:
        raise ValueError("The file is larger than 30 MB; split it by month")
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "ts" not in [(mapping or {}).get(f, f) for f in reader.fieldnames]:
        raise ValueError("The file needs a header row with a ts column (UTC, or set the timezone)")
    log, records = [], []
    names = {f: (mapping or {}).get(f, f) for f in reader.fieldnames}
    ignored = [f for f, n in names.items() if n not in CSV_COLUMNS]
    if ignored:
        log.append(f"Ignored columns: {', '.join(ignored[:10])}")
    if "inverter_mode_active" not in names.values():
        log.append("No inverter_mode_active column: this source supports monitor (tier 1) only, because the counterfactual needs the inverter's mode")
    if "grid_present" not in names.values() and "grid_voltage_v" not in names.values():
        raise ValueError("The file needs a grid_present column (or grid_voltage_v to derive it): the product cannot see outages without it")
    if "grid_import_wh" not in names.values() and "grid_export_wh" not in names.values():
        log.append("No grid_import_wh or grid_export_wh column: import and export are read from grid_power_w (positive is import)"
                   if "grid_power_w" in names.values() else
                   "No grid_import_wh, grid_export_wh or grid_power_w column: grid import and export are missing for this file, not zero")
    bad_ts = 0
    for line in reader:
        rec = {names[k]: v for k, v in line.items() if k in names and names[k] in CSV_COLUMNS}
        ts = rec.get("ts") or ""  # a short row leaves its missing trailing cells as None
        try:
            t = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone(timedelta(hours=tz_offset_h)))
            rec["ts"] = int(t.timestamp())
        except (ValueError, TypeError, OverflowError):
            bad_ts += 1
            continue
        for flag in ("grid_present", "genset_running"):
            word = str(rec.get(flag) or "").strip().lower()
            if word in FLAG_WORDS:
                rec[flag] = FLAG_WORDS[word]
        if str(rec.get("genset_running") or "").strip() == "":
            rec.pop("genset_running", None)
        records.append(rec)
    if not records:
        raise ValueError("No rows with a readable ts were found")
    now = _now_ts() if now is None else int(now)
    _check_window([r["ts"] for r in records], now)  # before anything is written
    if bad_ts:
        log.append(f"{bad_ts} rows without a readable ts were skipped")
    records.sort(key=lambda r: r["ts"])  # a stable sort: of two rows with one time, the later in the file stays later
    by_minute: dict[int, dict] = {}
    for r in records:
        by_minute[r["ts"] // 60] = r  # duplicate times keep the last row (plan Part 7.5), and are counted once
    duplicates = len(records) - len(by_minute)
    records = list(by_minute.values())
    if duplicates:
        log.append(f"{duplicates:,} row{'s repeat' if duplicates != 1 else ' repeats'} a minute that a later row of the file also gives: the last row "
                   f"of each minute was kept")
    # grid_present is what the product sees outages by: a file in which no row's grid signal can be read is refused, and unreadable cells are a warning
    warnings: list[str] = []
    known = unreadable_grid = 0
    for r in records:
        grid, bad = _num(r.get("grid_present"))
        if grid is not None or _num(r.get("grid_voltage_v"))[0] is not None:
            known += 1
        elif bad:
            unreadable_grid += 1
    if not known:
        raise ValueError("No row's grid_present (or grid_voltage_v) can be read: write grid_present as 1 for grid on and 0 for grid off (on and off "
                         "are read too) and upload again; the product cannot see outages without it")
    if unreadable_grid:
        warnings.append(f"{unreadable_grid:,} grid_present cell{'s' if unreadable_grid != 1 else ''} could not be read (1 is grid on, 0 is grid off): "
                        f"the grid is stored as unknown in those minutes, so an outage in them is not seen")
    # the logger's interval: the most common step between rows, in whole minutes (the shorter on a tie). A 5- or 15-minute row is stored as the minutes it
    # starts (up to the next row), so its energy, the day's peak and its outage minutes are counted in full (audit R7)
    starts = [r["ts"] // 60 for r in records]
    steps = Counter(b - a for a, b in zip(starts, starts[1:]))
    step = min(steps.items(), key=lambda kv: (-kv[1], kv[0]))[0] if steps else 1
    if step > STEP_MAX_MIN:
        raise ValueError(f"Rows are {step} minutes apart: the import reads rows at most {STEP_MAX_MIN} minutes apart; upload the logger's "
                         f"1-, 5- or 15-minute rows")
    spans = [min(step, b - a) for a, b in zip(starts, starts[1:])] + [step]
    if step > 1:
        log.append(f"Rows are {step} minutes apart: each row was stored as the {step} minutes it starts, its readings held over them and its energy "
                   f"({', '.join(INTERVAL_ENERGY)}) spread evenly")
    gaps = sum(1 for a, b in zip(starts, starts[1:]) if b - a > max(5, step))
    res = ingest(site_id, records, "csv", now=now, count_overlap=True, spans=spans, step_min=step)
    res["skipped"] += bad_ts
    if res["overlap"]:
        log.append(f"{res['overlap']:,} minutes were already stored: the file's values replaced theirs, and the columns the file does not carry (or "
                   f"leaves blank) were kept")
    if res.get("unreadable_cells"):
        log.append(f"{res['unreadable_cells']} cells were not finite numbers (text such as N/A, NaN or inf) or were beyond any meter's range "
                   f"(over a billion), and were stored as missing")
    log += warnings  # the imports row keeps them with the notes; the answer also lists them apart
    status = "done_with_warnings" if warnings else "done"
    digest = hashlib.sha256(content).hexdigest()[:16]
    first, last = records[0]["ts"], records[-1]["ts"]
    name = _file_name(file_name)
    with store.connect() as con:
        _imports_columns(con)
        iid = store.new_id()
        con.execute("INSERT INTO imports (id, site_id, source, file_hash, rows, gaps, status, log, created, from_ts, to_ts, file_name) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, site_id, "csv", digest, len(records), gaps, status, json.dumps(log), store._now(), first, last, name))
    filled_to = (r["ts"] // 60 * 60 + 60 * (n - 1) for r, n in zip(records, spans))  # a coarse row's last minute may fall on the next day
    for day in _local_days([r["ts"] for r in records] + list(filled_to)):  # only the days that received rows, not every day between the first and the last
        aggregate_day(site_id, day)
    return {"id": iid, "rows": len(records), "skipped": res["skipped"], "gaps": gaps, "log": log, "duplicates": duplicates, "minutes": res["accepted"],
            "step_min": step, "overlap": res["overlap"], "warnings": warnings, "status": status,
            "from": datetime.fromtimestamp(first, timezone.utc).isoformat(), "to": datetime.fromtimestamp(last, timezone.utc).isoformat(),
            "from_ts": first, "to_ts": last, "file_name": name}


# ------------------------------------------------------------------------------------------------ reading

def _finite(v):
    """A stored value for a JSON page: a non-finite float (written before ingest refused them) reads as missing."""
    return None if isinstance(v, float) and not math.isfinite(v) else v


def latest(site_id: str) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM minute WHERE site_id = ? ORDER BY ts DESC LIMIT 1", (site_id,)).fetchone()
    return {k: _finite(v) for k, v in dict(r).items()} if r else None


def first_ts(site_id: str) -> int | None:
    with connect() as con:
        r = con.execute("SELECT MIN(ts) FROM minute WHERE site_id = ?", (site_id,)).fetchone()
    return r[0] if r else None


def raw(site_id: str, t0: int, t1: int, columns: tuple = COLUMNS[3:]) -> dict:
    with connect() as con:
        rows = con.execute(f"SELECT ts, {', '.join(columns)} FROM minute WHERE site_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                           (site_id, int(t0), int(t1))).fetchall()
    out = {"ts": np.array([r[0] for r in rows], dtype=np.int64)}
    for i, c in enumerate(columns):
        if c in ("inverter_mode_active", "quality_flags"):
            out[c] = [r[i + 1] for r in rows]
        else:
            a = np.array([np.nan if r[i + 1] is None else r[i + 1] for r in rows], dtype=float)
            a[~np.isfinite(a)] = np.nan  # a non-finite value stored before ingest refused them is missing
            out[c] = a
    return out


def minute_grid(site_id: str, t0: int, t1: int) -> dict:
    """Arrays on a complete 1-minute grid [t0, t1) in kW, with missing minutes as NaN; what the bill engine and the replay read.

    t1 is taken down to the last whole minute after t0, so a bound part-way through a minute (a page's "now") never places a row past the arrays.
    Grid import and export fall back to grid_power_w where a minute has neither energy column (a power-only logger), and the state of charge falls
    back to the BMS reading where the estimate is missing (a CSV source never carries the estimate); `soc_est` keeps the estimate alone."""
    t0 = int(t0)
    t1 = t0 + max(0, (int(t1) - t0) // 60) * 60
    d = raw(site_id, t0, t1, COLUMNS[3:] + ("censored",))
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


def series(site_id: str, t0: int, t1: int, names: list[str], res: str = "auto") -> dict:
    """Chart series at a resolution: 1m, 15m, 1h or 1d (auto picks so a request never returns more than about 3,000 points)."""
    span = t1 - t0
    if res == "auto":
        res = "1m" if span <= 2 * 86400 else "15m" if span <= 31 * 86400 else "1h" if span <= 120 * 86400 else "1d"
    step = {"1m": 60, "15m": 900, "1h": 3600, "1d": 86400}[res]
    names = [n for n in names if n in NUMERIC] or ["pv_power_w", "load_power_w", "grid_power_w", "battery_power_w", "battery_soc_est_pct"]
    off = 5 * 3600 if res == "1d" else 0
    parts = []
    for n in names:
        kind = SERIES_KIND.get(n, "avg")
        fn = "SUM" if kind == "sum" else "MIN" if kind == "min" else "AVG"
        parts.append(f"{fn}({n})")
    sql = (f"SELECT ((ts + {off}) / {step}) * {step} - {off} AS b, {', '.join(parts)} FROM minute WHERE site_id = ? AND ts >= ? AND ts < ? "
           f"GROUP BY b ORDER BY b")
    with connect() as con:
        rows = con.execute(sql, (site_id, int(t0), int(t1))).fetchall()
    return {"resolution": res, "ts": [r[0] for r in rows],
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
    with connect() as con:
        for dow, h, v in con.execute(sql, (site_id, t0)).fetchall():
            grid[int(dow)][int(h)] = None if v is None or not math.isfinite(float(v)) else round(v, 2)
    # rows Monday..Sunday: 1970-01-01 was a Thursday, so (days since the epoch + 3) % 7 already gives 0 = Monday and 6 = Sunday, as in
    # twin.load_template and forecast._day_type
    return {"series": name, "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], "values": grid}


def outages(site_id: str, t0: int, t1: int) -> list[dict]:
    """Outage intervals from the grid-present flag (a gap in data is not an outage)."""
    d = raw(site_id, t0, t1, ("grid_present", "battery_soc_est_pct", "battery_soc_bms_pct", "genset_running", "unserved_w"))
    soc = np.where(np.isnan(d["battery_soc_est_pct"]), d["battery_soc_bms_pct"], d["battery_soc_est_pct"])  # the BMS where there is no estimate
    out, cur = [], None
    for i, t in enumerate(d["ts"]):
        down = d["grid_present"][i] == 0
        if down and cur is None:
            cur = {"start": int(t), "soc_start_pct": None if np.isnan(soc[i]) else round(float(soc[i]), 1),
                   "genset_minutes": 0, "unserved_kwh": 0.0}
        if down and cur is not None:
            cur["end"] = int(t) + 60
            cur["genset_minutes"] += int(d["genset_running"][i] == 1)
            cur["unserved_kwh"] += float(np.nan_to_num(d["unserved_w"][i])) / 60000
        if not down and cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        cur["ongoing"] = True
        out.append(cur)
    for o in out:
        o["minutes"] = (o["end"] - o["start"]) // 60
        o["unserved_kwh"] = round(o["unserved_kwh"], 2)
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
            "completeness": round(g["completeness"], 4), "import_completeness": round(g["imp_completeness"], 4)}
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


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(v):
    """An exported cell: blank for missing, and a text cell that a spreadsheet would read as a formula quote-prefixed (plan Part 2.8)."""
    if v is None:
        return ""
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


def export_csv(site_id: str, t0: int, t1: int) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts_utc"] + list(COLUMNS[3:]))
    with connect() as con:
        for r in con.execute(f"SELECT ts, {', '.join(COLUMNS[3:])} FROM minute WHERE site_id = ? AND ts >= ? AND ts < ? ORDER BY ts",
                             (site_id, int(t0), int(t1))):
            w.writerow([datetime.fromtimestamp(r[0], timezone.utc).isoformat().replace("+00:00", "Z")] + [_csv_cell(_finite(v)) for v in r[1:]])
    return buf.getvalue()


def delete_site(site_id: str) -> None:
    with connect() as con:
        con.execute("DELETE FROM minute WHERE site_id = ?", (site_id,))
        con.execute("DELETE FROM forecast_issues WHERE site_id = ?", (site_id,))


# ------------------------------------------------------------------------------------------------ forecast issues

def save_forecast(site_id: str, kind: str, belief_time: int, stage: int, version: str, body: dict) -> None:
    with connect() as con:
        con.execute("INSERT OR REPLACE INTO forecast_issues (site_id, kind, belief_time, stage, model_version, body) VALUES (?,?,?,?,?,?)",
                    (site_id, kind, int(belief_time), stage, version, json.dumps(body)))
        con.execute("DELETE FROM forecast_issues WHERE site_id = ? AND kind = ? AND belief_time < ? AND belief_time % 3600 != 0",
                    (site_id, kind, int(belief_time) - 3 * 86400))


def latest_forecast(site_id: str, kind: str, at_or_before: int | None = None) -> dict | None:
    with connect() as con:
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
