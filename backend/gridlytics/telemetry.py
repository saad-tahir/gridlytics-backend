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


def _open() -> sqlite3.Connection:
    path = store.telemetry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    if con.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("BEGIN IMMEDIATE")
        for ddl in _DDL:
            con.execute(ddl)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.execute("COMMIT")
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

def write_rows(rows: list[tuple], columns: tuple = COLUMNS) -> int:
    if not rows:
        return 0
    sql = f"INSERT OR REPLACE INTO minute ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
    with connect() as con:
        con.executemany(sql, rows)
    return len(rows)


_VOLT = ALL.index("grid_voltage_v")


def _num(v) -> tuple[float | None, bool]:
    """A numeric cell as (value, unreadable): blanks are missing (None); text that is not a number, NaN and infinities are missing too, and
    counted, so one bad cell neither refuses a file nor stores a value the pages cannot render."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return None, False
    try:
        x = float(v)
    except (TypeError, ValueError, OverflowError):
        return None, True
    return (x, False) if math.isfinite(x) else (None, True)


def ingest(site_id: str, records: list[dict], source: str) -> dict:
    """Normalised dict rows (edge batches and CSV) into the minute table; idempotent by (site, minute), the last value wins."""
    rows, skipped, unreadable = [], 0, 0
    for r in records:
        ts = r.get("ts")
        if isinstance(ts, str):
            try:
                t = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
            except ValueError:
                skipped += 1
                continue
            ts = int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            skipped += 1
            continue
        ts = int(ts) // 60 * 60
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
        rows.append(tuple(row))
    write_rows(rows, ALL)
    return {"accepted": len(rows), "skipped": skipped, "unreadable_cells": unreadable}


def import_csv(site_id: str, content: bytes, tz_offset_h: float = 0.0, mapping: dict | None = None) -> dict:
    """A CSV upload: one row per minute; blanks are missing; duplicates keep the last; gaps over 5 minutes are reported."""
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
        if str(rec.get("genset_running") or "").strip() == "":
            rec.pop("genset_running", None)
        records.append(rec)
    if not records:
        raise ValueError("No rows with a readable ts were found")
    if bad_ts:
        log.append(f"{bad_ts} rows without a readable ts were skipped")
    records.sort(key=lambda r: r["ts"])
    gaps = sum(1 for a, b in zip(records, records[1:]) if b["ts"] - a["ts"] > 300)
    res = ingest(site_id, records, "csv")
    res["skipped"] += bad_ts
    if res.get("unreadable_cells"):
        log.append(f"{res['unreadable_cells']} cells were not finite numbers (text such as N/A, NaN or inf) and were stored as missing")
    digest = hashlib.sha256(content).hexdigest()[:16]
    first, last = records[0]["ts"], records[-1]["ts"]
    with store.connect() as con:
        iid = store.new_id()
        con.execute("INSERT INTO imports (id, site_id, source, file_hash, rows, gaps, status, log, created) VALUES (?,?,?,?,?,?,?,?,?)",
                    (iid, site_id, "csv", digest, res["accepted"], gaps, "done", json.dumps(log), store._now()))
    for day in _days_between(first, last):
        aggregate_day(site_id, day)
    return {"id": iid, "rows": res["accepted"], "skipped": res["skipped"], "gaps": gaps, "log": log,
            "from": datetime.fromtimestamp(first, timezone.utc).isoformat(), "to": datetime.fromtimestamp(last, timezone.utc).isoformat()}


def _days_between(t0: int, t1: int) -> list[date]:
    d0 = datetime.fromtimestamp(t0, PKT).date()
    d1 = datetime.fromtimestamp(t1, PKT).date()
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


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


def aggregate_day(site_id: str, day: date, peak_flags: np.ndarray | None = None) -> dict:
    """The day's totals into telemetry_days (plan Part 3.3)."""
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
    with store.connect() as con:
        con.execute("INSERT INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?) ON CONFLICT(site_id, day_local) DO UPDATE SET data = excluded.data",
                    (site_id, day.isoformat(), json.dumps(data)))
    return data


def days(site_id: str, d0: str, d1: str) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT day_local, data FROM telemetry_days WHERE site_id = ? AND day_local >= ? AND day_local <= ? ORDER BY day_local",
                           (site_id, d0, d1)).fetchall()
    return [{"day": r["day_local"], **{k: _finite(v) for k, v in json.loads(r["data"]).items()}} for r in rows]


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
