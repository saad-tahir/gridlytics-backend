"""Daily database backups, their retention, and a tested restore (master PS-12, minimal PS-13).

A copy of the workspace database is taken once a day while the app runs, with SQLite's own backup (consistent while
the app writes), into `backup/` beside the database. The last `KEEP` daily copies are kept; copies taken before a
schema migration are never removed. A restore first copies the current database aside, so it can be undone.

With a database per company (decisions-v2 section 5) everything here works on the current company's file (`store.db_path()`):
the companies' files share `companies/backup/`, and each copy is named by its company's id, so listing, pruning and restoring
touch only that company's copies. The daily check goes through the default file, every company's file and the registry; a company
Gridlytics is switched off for is not copied daily (nothing of it changes while it is off), only when the founder asks.

A copy is one set (audit founder-console-01 and -12): the workspace file `<id>-<stamp>-<kind>.db`, the telemetry file taken with it
`<id>-telemetry-<stamp>-<kind>.db` (store.backup), and the folder of the company's signing keys `<id>-keys-<stamp>-<kind>/` (the backup
half of engine-delivery-13: a restore onto another machine needs the keys the edges were given). The three are listed, pruned and
restored together; `scripts/restore_backup.py` restores one from the command line with the server stopped.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from contextlib import closing
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import platform as platformdb
from . import store

KEEP = 14
#: Copies made with "Back up now" that are kept: the oldest beyond these are removed when one more is made (review R-100)
KEEP_MANUAL = 10
_started = False
log = logging.getLogger("gridlytics")


def folder(path: Path | None = None) -> Path:
    return (path or store.db_path()).parent / "backup"


def _ours(kind: str = r"[a-z0-9-]+", path: Path | None = None):
    """This database's copies: its own name, a date and time, and the kind (another database in the folder is not ours)."""
    return re.compile(rf"^{re.escape((path or store.db_path()).stem)}-\d{{4}}-\d{{2}}-\d{{2}}-\d{{6}}(-\d{{6}})?-({kind})\.db$")


def keys_folder(path: Path | None = None) -> Path:
    """The signing keys of a company's sites, as delivery keeps them: the data folder's `keys/<the file's stem>`."""
    return platformdb.data_dir() / "keys" / (path or store.db_path()).stem


def companions(name: str, path: Path | None = None) -> tuple[str, str]:
    """The telemetry copy and the keys folder taken with the workspace copy `name` (store.backup names the three by one stamp and kind)."""
    stem = (path or store.db_path()).stem
    rest = name[len(stem) + 1:-len(".db")]
    return f"{stem}-telemetry-{rest}.db", f"{stem}-keys-{rest}"


def copy_keys(path: Path, target: Path) -> int:
    """The signing keys of the database `path` copied into the folder `target`, readable by the owner only; how many (0, and no folder, when it has none)."""
    source = keys_folder(path)
    keys = sorted(f for f in source.iterdir() if f.is_file()) if source.is_dir() else []
    if keys:
        target.mkdir(parents=True, exist_ok=True)
        for key in keys:
            shutil.copyfile(key, target / key.name)
            _private(target / key.name)
    return len(keys)


def _private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


def _count(keys: Path) -> int:
    """The key files in a copy's keys folder; 0 when it has none, or was pruned while being listed."""
    try:
        return sum(1 for k in keys.iterdir() if k.is_file())
    except (FileNotFoundError, NotADirectoryError):
        return 0


def list_backups() -> list[dict]:
    """The current database's copies, newest first: the workspace copy's name (the one to restore), its kind and when it was made (UTC, with its offset: the
    server's local time with none was read as UTC by the browser, frontend-states-formats-17), the telemetry copy taken with it (None when the company had no
    telemetry then) and the number of signing keys, and the sizes: `bytes` is the whole copy's, the workspace's and the telemetry's beside it."""
    if not folder().exists():
        return []
    out = []
    for f in sorted(folder().iterdir(), reverse=True):
        m = _ours().match(f.name)
        if not m:
            continue
        try:
            st = f.stat()
        except FileNotFoundError:          # pruned while being listed
            continue
        tag = m.group(2)
        kind = ("daily" if tag == "daily" else "before a schema change" if tag.startswith("schema-v") else "manual" if tag == "manual"
                else "before a reset" if tag == "before-reset" else "before archiving" if tag == "before-archive" else "restore point")
        telemetry_name, keys_name = companions(f.name)
        telemetry_bytes = _size(folder() / telemetry_name)
        out.append({"name": f.name, "kind": kind, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    "bytes": st.st_size + (telemetry_bytes or 0), "workspace_bytes": st.st_size,
                    "telemetry": telemetry_name if telemetry_bytes is not None else None, "telemetry_bytes": telemetry_bytes or 0,
                    "keys": _count(folder() / keys_name)})
    return out


def _daily(path: Path, copy, keep: int) -> Path | None:
    """Today's daily copy of the database `path`, made by `copy(tag)`, if there is none yet; then its oldest daily copies beyond `keep` are removed."""
    today = datetime.now().strftime("%Y-%m-%d")
    if not path.exists() or any(f.name.startswith(f"{path.stem}-{today}") and f.stem.endswith("-daily")
                                for f in (folder(path).glob("*.db") if folder(path).exists() else [])):
        return None
    made = copy("daily")
    prune("daily", keep, path)
    return made


def daily(keep: int = KEEP) -> Path | None:
    """Today's daily copy of the current database, if there is none yet; then the oldest daily copies beyond `keep` are removed."""
    return _daily(store.db_path(), store.backup, keep)


def _company_files() -> list[Path]:
    """The files of the companies Gridlytics is on for, from the registry; none when there is no registry yet (it is not made for this)."""
    return platformdb.company_files() if platformdb.db_path().exists() else []


def daily_all(files=None, keep: int = KEEP) -> list[Path]:
    """One check of the daily backup: today's copy of the default database, of every company's file `files()` gives (the registry's companies when
    None) and of the registry itself, each made once a day. The copies made now; a copy that fails is logged and tried again at the next check, and
    never stops the others."""
    made, seen = [], set()
    for path in [Path(store.DB_PATH), *(files or _company_files)()]:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:                     # a test's company names the default file (addendum item 3)
            continue
        seen.add(key)
        try:
            with store.use_company(path):
                copy = daily(keep)
        except Exception:
            log.exception("the daily copy of %s could not be made", path)
            continue
        if copy is not None:
            made.append(copy)
    try:
        # losing the registry would lose every account and membership
        copy = _daily(platformdb.db_path(), platformdb.backup, keep)
    except Exception:
        log.exception("the daily copy of the registry could not be made")
    else:
        if copy is not None:
            made.append(copy)
    return made


def prune(kind: str, keep: int, path: Path | None = None) -> None:
    """The oldest copies of a kind ("daily", "manual") of the current database (or of `path`) beyond `keep` are removed; their names sort by the time
    they were made."""
    copies = sorted(f for f in folder(path).iterdir() if _ours(kind, path).match(f.name)) if folder(path).exists() else []
    for old in copies[:-keep] if len(copies) > keep else []:
        telemetry_name, keys_name = companions(old.name, path)
        old.unlink()
        # the telemetry copy and the keys go with it (the telemetry copies were never removed: founder-console-12)
        (folder(path) / telemetry_name).unlink(missing_ok=True)
        shutil.rmtree(folder(path) / keys_name, ignore_errors=True)


def before_reset() -> Path | None:
    """A copy of the database before the settings are restored to the shipped defaults (tag 'before-reset'; like a manual copy it is never pruned). None when
    switched off (the tests set GRIDLYTICS_RESET_BACKUP=0) or when there is no database file yet."""
    if os.environ.get("GRIDLYTICS_RESET_BACKUP", "1") == "0":
        return None
    return store.backup("before-reset")


def _is_database(path: Path, table: str) -> bool:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as probe:
            return bool(probe.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0])
    except sqlite3.Error:
        return False


def restore(name: str) -> dict:
    """Put a copy back as the current company's files, as one set (founder-console-01): the workspace file, the telemetry file taken with it (an empty one
    when it had none, so that no later minute is left beside older plans) and its signing keys (those the keys folder lacks: a key file never changes once
    made, and a newer one is kept). The files as they are now are copied aside first, as one set too ("restore point"), so a restore can be undone. Only a
    copy of this database is put back: the companies' copies share one folder. Stop the server first (scripts/restore_backup.py runs it from the command
    line). The answer names what was put back: {workspace, telemetry (None when empty), keys, restore_point}."""
    source = (folder() / name).resolve()
    if (source.parent != folder().resolve() or not _ours().match(source.name) or not source.is_file() or source.suffix != ".db"
            or source.stat().st_size == 0 or not _is_database(source, "sites")):     # refuses a file that is not a workspace database
        raise FileNotFoundError(name)
    telemetry_name, keys_name = companions(source.name)
    telemetry_copy = folder() / telemetry_name
    if telemetry_copy.exists() and not _is_database(telemetry_copy, "minute"):
        raise FileNotFoundError(telemetry_name)
    aside = store.backup("restore-point")
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(store.db_path())) as dst:
        src.backup(dst)
    live = store.telemetry_path()
    if telemetry_copy.exists() or live.exists():
        with closing(sqlite3.connect(telemetry_copy if telemetry_copy.exists() else ":memory:")) as src, closing(sqlite3.connect(live)) as dst:
            src.backup(dst)
        from . import telemetry                         # an emptied file is given its tables again (telemetry imports this module's own imports)
        with telemetry.connect():
            pass
    keys_copy, keys = folder() / keys_name, 0
    if keys_copy.is_dir():
        mine = keys_folder()
        mine.mkdir(parents=True, exist_ok=True)
        for key in sorted(k for k in keys_copy.iterdir() if k.is_file()):
            if not (mine / key.name).exists():
                shutil.copyfile(key, mine / key.name)
                _private(mine / key.name)
            keys += 1
    return {"workspace": source.name, "telemetry": telemetry_name if telemetry_copy.exists() else None, "keys": keys,
            "restore_point": aside.name if aside else None}


def start_daily(interval_s: float = 3600.0, files=None) -> None:
    """Take the daily copies now and then check every hour, in the background (`daily_all`: the default database, the companies' files `files()`
    gives, the registry's when None, and the registry). Only when GRIDLYTICS_DAILY_BACKUP is set (the launchers set it; the tests do not)."""
    global _started
    if _started or os.environ.get("GRIDLYTICS_DAILY_BACKUP") != "1":
        return
    _started = True

    def loop():
        stop = threading.Event()
        while not stop.is_set():
            try:
                daily_all(files)
            except Exception:  # a failed copy must never stop the app; the next check tries again
                pass
            # A database the app has not made yet is looked for again in a minute, then every hour
            stop.wait(interval_s if Path(store.DB_PATH).exists() or platformdb.db_path().exists() else 60.0)

    threading.Thread(target=loop, name="daily-backup", daemon=True).start()
