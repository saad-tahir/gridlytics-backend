"""Daily database backups, their retention, and a tested restore (master PS-12, minimal PS-13).

A copy of the workspace database is taken once a day while the app runs, with SQLite's own backup (consistent while
the app writes), into `backup/` beside the database. The last `KEEP` daily copies are kept; copies taken before a
schema migration are never removed. A restore first copies the current database aside, so it can be undone.

With a database per company (decisions-v2 section 5) everything here works on the current company's file (`store.db_path()`):
the companies' files share `companies/backup/`, and each copy is named by its company's id, so listing, pruning and restoring
touch only that company's copies. The daily check goes through the default file, every company's file and the registry.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import closing
import threading
from datetime import datetime
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


def list_backups() -> list[dict]:
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
                else "before a reset" if tag == "before-reset" else "restore point")
        out.append({"name": f.name, "bytes": st.st_size, "kind": kind, "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
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
        old.unlink()


def before_reset() -> Path | None:
    """A copy of the database before the settings are restored to the shipped defaults (tag 'before-reset'; like a manual copy it is never pruned). None when
    switched off (the tests set GRIDLYTICS_RESET_BACKUP=0) or when there is no database file yet."""
    if os.environ.get("GRIDLYTICS_RESET_BACKUP", "1") == "0":
        return None
    return store.backup("before-reset")


def restore(name: str) -> Path:
    """Put a backup back as the workspace database. The current one is copied aside first ("restore point"). Only a copy of this database is put back:
    the companies' copies share one folder."""
    source = (folder() / name).resolve()
    if (source.parent != folder().resolve() or not _ours().match(source.name) or not source.is_file() or source.suffix != ".db"
            or source.stat().st_size == 0):
        raise FileNotFoundError(name)
    with closing(sqlite3.connect(source)) as probe:                      # refuses a file that is not a workspace database
        if not probe.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'sites'").fetchone()[0]:
            raise FileNotFoundError(name)
    aside = store.backup("restore-point")
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(store.db_path())) as dst:
        src.backup(dst)
    return aside


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
