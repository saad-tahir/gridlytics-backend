"""The company's workspace file and its telemetry file (plan Part 3).

One server keeps a database per company (the lifted sign-in's per-company files): every read and write goes to the file of the company the request works in
(`use_company`, `db_path()`), set by the guard. Unset, it is `DB_PATH` (the tests' default file; no route serves it, plan Part 10.4). Each company also has a
telemetry file beside its workspace file, `<stem>-telemetry.db` (`telemetry_path()`), with its own schema version (telemetry.py).

The context functions, the WAL-once rule, the copy before a migration and BEGIN IMMEDIATE under one lock are lifted from solarlytics-backend at c853130
(store.py lines 33-131, 475-526, 956-990, 1402-1420; DESIGN_LOG first section). The schema is Gridlytics' own: version 1 holds the four tables the lifted
package needs (members, role_presets, api_keys, audit) and the EMS tables of plan Part 3.2; version 2 adds reports.preliminary; version 5 (round 7)
reports.generated_by, alerts.history and alerts.assigned_id.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("GRIDLYTICS_DB", Path(__file__).resolve().parents[2] / "data" / "gridlytics.db"))
DEFAULT_ORG = "default"
_ORG: ContextVar[str] = ContextVar("gridlytics_org", default=DEFAULT_ORG)
_DB: ContextVar[Path | None] = ContextVar("gridlytics_db", default=None)
_ACTOR: ContextVar[dict | None] = ContextVar("gridlytics_actor", default=None)
SCHEMA_VERSION = 5
_lock = threading.Lock()


def _org() -> str:
    return _ORG.get()


def org() -> str:
    return _ORG.get()


def set_org(org_id: str):
    return _ORG.set(org_id)


def reset_org(token) -> None:
    _ORG.reset(token)


@contextmanager
def use_org(org_id: str):
    token = _ORG.set(org_id)
    try:
        yield org_id
    finally:
        _ORG.reset(token)


def db_path() -> Path:
    """The workspace file the current request works in: its company's, or the default file."""
    return _DB.get() or Path(DB_PATH)


def telemetry_path(path: Path | None = None) -> Path:
    """The telemetry file beside a workspace file (plan Part 3.3): the stem plus `-telemetry`."""
    path = Path(path) if path is not None else db_path()
    return path.with_name(f"{path.stem}-telemetry{path.suffix or '.db'}")


def set_company(path: str | Path):
    if path is None or not str(path).strip():
        raise ValueError("A company's database is named by its file")
    return _DB.set(Path(path))


def reset_company(token) -> None:
    _DB.reset(token)


@contextmanager
def use_company(path: str | Path):
    token = set_company(path)
    try:
        yield db_path()
    finally:
        _DB.reset(token)


def set_actor(actor_id: str, email: str = ""):
    return _ACTOR.set({"id": str(actor_id or ""), "email": str(email or "").strip().lower()})


def reset_actor(token) -> None:
    _ACTOR.reset(token)


def actor() -> dict | None:
    return _ACTOR.get()


def __getattr__(name: str):
    if name == "ORG":
        return _ORG.get()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp(after: str | None = None) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    if after and now <= after:
        now = (datetime.fromisoformat(after) + timedelta(microseconds=1)).isoformat(timespec="microseconds")
    return now


def new_id(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


# ------------------------------------------------------------------------------------------------ the schema

_V1 = [
    # the lifted package's four tables (solarlytics-backend store.py DDL of v17 and v20)
    """CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL DEFAULT 'default', name TEXT NOT NULL, prefix TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL, by TEXT NOT NULL DEFAULT '', last_used TEXT, revoked TEXT,
        revoked_by TEXT, site_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'edge')""",
    """CREATE TABLE IF NOT EXISTS members (
        org_id TEXT NOT NULL, user_id TEXT NOT NULL, email TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL,
        role_source TEXT NOT NULL DEFAULT 'default', company_role TEXT NOT NULL DEFAULT '', licence TEXT NOT NULL DEFAULT '', seal TEXT NOT NULL DEFAULT '',
        disabled INTEGER NOT NULL DEFAULT 0, disabled_reason TEXT NOT NULL DEFAULT '', legacy_user_id TEXT, first_seen TEXT NOT NULL, last_seen TEXT,
        updated TEXT NOT NULL, by TEXT NOT NULL DEFAULT '', PRIMARY KEY (org_id, user_id))""",
    "CREATE INDEX IF NOT EXISTS members_email ON members (org_id, email)",
    """CREATE TABLE IF NOT EXISTS role_presets (
        org_id TEXT NOT NULL, email TEXT NOT NULL, role TEXT NOT NULL, created TEXT NOT NULL, by TEXT NOT NULL DEFAULT '', used TEXT,
        PRIMARY KEY (org_id, email))""",
    """CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL DEFAULT 'default', at TEXT NOT NULL, actor TEXT NOT NULL, actor_id TEXT NOT NULL DEFAULT '',
        actor_email TEXT NOT NULL DEFAULT '', action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}')""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated TEXT NOT NULL DEFAULT '')",
    # the EMS tables (plan Part 3.2); most fields live in a JSON `data` column checked by the strict models of schemas.py
    """CREATE TABLE IF NOT EXISTS sites (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft', tier_capable TEXT NOT NULL DEFAULT 'monitor',
        tier_allowed TEXT NOT NULL DEFAULT 'monitor', demo_link TEXT, data TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL, updated TEXT NOT NULL,
        deleted TEXT)""",
    "CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS connections_site ON connections (site_id)",
    "CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, role TEXT NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS assets_site ON assets (site_id)",
    "CREATE TABLE IF NOT EXISTS site_state (site_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS capabilities (site_id TEXT NOT NULL, seq INTEGER NOT NULL, received_at TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (site_id, seq))",
    """CREATE TABLE IF NOT EXISTS data_sources (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, kind TEXT NOT NULL, api_key_id TEXT NOT NULL DEFAULT '',
        data TEXT NOT NULL DEFAULT '{}', last_seen TEXT, created TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS native_settings (site_id TEXT NOT NULL, version INTEGER NOT NULL, captured_at TEXT NOT NULL, by TEXT NOT NULL,
        source TEXT NOT NULL, body TEXT NOT NULL, photo TEXT NOT NULL DEFAULT '', PRIMARY KEY (site_id, version))""",
    """CREATE TABLE IF NOT EXISTS tariff_versions (id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, effective_from TEXT NOT NULL,
        effective_to TEXT, known_from TEXT NOT NULL, status TEXT NOT NULL, supersedes TEXT, body TEXT NOT NULL, source TEXT NOT NULL, hash TEXT NOT NULL,
        created TEXT NOT NULL, by TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS tariff_versions_key ON tariff_versions (kind, key, effective_from)",
    "CREATE TABLE IF NOT EXISTS fuel_prices (kind TEXT NOT NULL, valid_from TEXT NOT NULL, price REAL NOT NULL, source TEXT NOT NULL, PRIMARY KEY (kind, valid_from))",
    """CREATE TABLE IF NOT EXISTS outage_schedules (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, fetched_at TEXT NOT NULL, source TEXT NOT NULL,
        slots TEXT NOT NULL, hash TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, generated_at TEXT NOT NULL, belief_time TEXT NOT NULL,
        valid_from TEXT NOT NULL, valid_until TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
        solver TEXT NOT NULL DEFAULT '', solve_seconds REAL NOT NULL DEFAULT 0, objective_rs REAL NOT NULL DEFAULT 0, config_hash TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '{}', steps TEXT, slots TEXT, applied INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS plans_site ON plans (site_id, generated_at)",
    "CREATE TABLE IF NOT EXISTS plan_messages (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, plan_id TEXT NOT NULL, key_id TEXT NOT NULL, body TEXT NOT NULL, sent_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS acks (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, plan_id TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS applied (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, plan_id TEXT NOT NULL, day_local TEXT NOT NULL, by TEXT NOT NULL,
        method TEXT NOT NULL, photo TEXT NOT NULL DEFAULT '', confirmation TEXT NOT NULL DEFAULT 'pending', fidelity REAL, data TEXT NOT NULL DEFAULT '{}',
        created TEXT NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}')",
    "CREATE INDEX IF NOT EXISTS events_site ON events (site_id, ts)",
    """CREATE TABLE IF NOT EXISTS overrides (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, kind TEXT NOT NULL, value REAL, from_ts TEXT NOT NULL,
        until_ts TEXT NOT NULL, author TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, cleared TEXT)""",
    """CREATE TABLE IF NOT EXISTS switchback (site_id TEXT NOT NULL, block_start TEXT NOT NULL, block_end TEXT NOT NULL, arm TEXT NOT NULL,
        locked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (site_id, block_start))""",
    "CREATE TABLE IF NOT EXISTS pilots (site_id TEXT PRIMARY KEY, data TEXT NOT NULL, started TEXT, updated TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS savings_days (site_id TEXT NOT NULL, day_local TEXT NOT NULL, baseline TEXT NOT NULL, data TEXT NOT NULL,
        computed TEXT NOT NULL, PRIMARY KEY (site_id, day_local, baseline))""",
    """CREATE TABLE IF NOT EXISTS bills (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, connection_id TEXT NOT NULL, period_start TEXT NOT NULL,
        period_end TEXT NOT NULL, real TEXT NOT NULL, rebuilt TEXT NOT NULL DEFAULT '{}', difference_pct REAL, created TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS forecast_scores (site_id TEXT NOT NULL, kind TEXT NOT NULL, day_local TEXT NOT NULL, horizon_class TEXT NOT NULL,
        metric TEXT NOT NULL, value REAL NOT NULL, baseline_value REAL, PRIMARY KEY (site_id, kind, day_local, horizon_class, metric))""",
    """CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, site_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, period TEXT NOT NULL,
        generated_at TEXT NOT NULL, inputs_hash TEXT NOT NULL, pdf_path TEXT NOT NULL DEFAULT '', csv_path TEXT NOT NULL DEFAULT '', status TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, rule TEXT NOT NULL, category TEXT NOT NULL, severity TEXT NOT NULL,
        title TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'open', assigned TEXT NOT NULL DEFAULT '', opened_at TEXT NOT NULL,
        closed_at TEXT, note TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS alerts_state ON alerts (state, opened_at)",
    """CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, source TEXT NOT NULL, file_hash TEXT NOT NULL, rows INTEGER NOT NULL,
        gaps INTEGER NOT NULL, status TEXT NOT NULL, log TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runs (site_id TEXT NOT NULL, tick TEXT NOT NULL, started TEXT NOT NULL, finished TEXT, status TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '', solve_s REAL, objective_rs REAL, plan_id TEXT, PRIMARY KEY (site_id, tick))""",
    """CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, site_id TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', outcome TEXT NOT NULL DEFAULT '', scored_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS site_drafts (draft_id TEXT PRIMARY KEY, site_id TEXT, person TEXT NOT NULL, body TEXT NOT NULL, updated TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS site_keys (site_id TEXT NOT NULL, key_id TEXT NOT NULL, public_key TEXT NOT NULL, private_path TEXT NOT NULL,
        created TEXT NOT NULL, retired TEXT, PRIMARY KEY (site_id, key_id))""",
    "CREATE TABLE IF NOT EXISTS telemetry_days (site_id TEXT NOT NULL, day_local TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (site_id, day_local))",
]


#: Version 2 (round 3): a report keeps whether its figures were preliminary when it was made (NULL for a report made before, and for the daily sheet)
_V2_COLUMNS = (("reports", "preliminary", "INTEGER"),)
#: Version 5 (round 7, audit pages-D-25; 3 and 4 are other round-7 packages', renumbered when they are merged): a report keeps who generated it
#: ('' for a report made before); an alert keeps its history, each action with who, when and the note (audit pages-D-28; '[]' for an alert of before),
#: and the member it is assigned to (audit role-operator-31; '' for nobody, and for an alert assigned by a typed name before)
_V5_COLUMNS = (("reports", "generated_by", "TEXT NOT NULL DEFAULT ''"), ("alerts", "history", "TEXT NOT NULL DEFAULT '[]'"),
               ("alerts", "assigned_id", "TEXT NOT NULL DEFAULT ''"))


def _migrate(con: sqlite3.Connection, version: int) -> None:
    if version < 1:
        for sql in _V1:
            con.execute(sql)
    if version < 2:
        for table, column, kind in _V2_COLUMNS:
            if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    if version < 5:
        for table, column, kind in _V5_COLUMNS:
            if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def backup(tag: str) -> Path | None:
    """A copy of the current workspace file and its telemetry file in `backup/` beside them (SQLite's own backup, consistent while another connection
    writes); None when there is no file yet."""
    path = db_path()
    if not path.exists() or path.stat().st_size == 0:
        return None
    folder = path.parent / "backup"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y-%m-%d-%H%M%S-%f')
    target = folder / f"{path.stem}-{stamp}-{tag}.db"
    for src_path, dst_path in ((path, target), (telemetry_path(path), folder / f"{path.stem}-telemetry-{stamp}-{tag}.db")):
        if src_path.exists() and src_path.stat().st_size > 0:
            with closing(sqlite3.connect(src_path)) as src, closing(sqlite3.connect(dst_path)) as dst:
                src.backup(dst)
    return target


def _connect() -> sqlite3.Connection:
    """A connection to the current workspace file at the current schema; a file is migrated the first time it is opened, a copy first when it holds data."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    if con.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        with _lock:
            con.execute("PRAGMA journal_mode=WAL")
    if con.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return con
    with _lock:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            has_data = path.stat().st_size > 0 and con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] > 0
            if has_data:
                backup(f"schema-v{version}")
            con.isolation_level = None
            con.execute("BEGIN IMMEDIATE")
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if version < SCHEMA_VERSION:
                    _migrate(con, version)
                    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            con.isolation_level = ""
    return con


@contextmanager
def connect():
    """A connection closed at the end of the block; commits on success."""
    con = _connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


@contextmanager
def write_transaction():
    con = _connect()
    try:
        con.isolation_level = None
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def audit(actor: str, action: str, target: str = "", detail: dict | None = None, con: sqlite3.Connection | None = None) -> None:
    """One line in the audit trail: who did what to what, when. The id and email of who acts come from the context (`set_actor`). Given `con` (an open
    transaction on the workspace file), the line is written in that transaction, so it stands or falls with the change it records."""
    who = _ACTOR.get() or {}
    args = (_org(), _now(), actor, who.get("id", ""), who.get("email", ""), action, target, json.dumps(detail or {}, default=str))
    sql = "INSERT INTO audit (org_id, at, actor, actor_id, actor_email, action, target, detail) VALUES (?,?,?,?,?,?,?,?)"
    if con is not None:
        con.execute(sql, args)
        return
    with connect() as c:
        c.execute(sql, args)


def list_audit(limit: int = 300, offset: int = 0) -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT at, actor, actor_id, actor_email, action, target, detail FROM audit WHERE org_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                           (_org(), limit, offset)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


# ------------------------------------------------------------------------------------------------ small helpers the feature modules share

def row(r: sqlite3.Row | None, json_fields: tuple = ("data",)) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for f in json_fields:
        if f in out and isinstance(out[f], str):
            out[f] = json.loads(out[f]) if out[f] else None
    return out


def get_setting(key: str, default=None):
    with connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value) -> None:
    with connect() as con:
        con.execute("INSERT INTO settings (key, value, updated) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
                    (key, json.dumps(value, default=str), _now()))
