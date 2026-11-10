"""Daily database backups, their retention, and a tested restore (master PS-12, minimal PS-13).

A copy of the workspace database is taken once a day while the app runs, with SQLite's own backup (consistent while
the app writes), into `backup/` beside the database. The last `KEEP` daily copies are kept; copies taken before a
schema migration are never removed. A restore first copies the current database aside, so it can be undone.

With a database per company (decisions-v2 section 5) everything here works on the current company's file (`store.db_path()`):
the companies' files share `companies/backup/`, and each copy is named by its company's id, so listing, pruning and restoring
touch only that company's copies. The daily check goes through the default file, every company's file and the registry; a company
Gridlytics is switched off for is not copied daily (nothing of it changes while it is off), only when the founder asks.

A copy is one set (audit founder-console-01 and -12): the workspace file `<id>-<stamp>-<kind>.db`, the telemetry file taken with it
`<id>-telemetry-<stamp>-<kind>.db` (store.backup), the folder of the company's signing keys `<id>-keys-<stamp>-<kind>/` (the backup
half of engine-delivery-13: a restore onto another machine needs the keys the edges were given) and the folder of the reports it made
`<id>-reports-<stamp>-<kind>/` (the backup half of parity-architecture-11 and -16: a stored report is downloaded from its file). The four
are listed, pruned and restored together; `scripts/restore_backup.py` restores one from the command line with the server stopped.
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


def reports_folder(path: Path | None = None) -> Path:
    """The reports a company made, as reports.py keeps them: the data folder's `reports/<the file's stem>`."""
    return platformdb.data_dir() / "reports" / (path or store.db_path()).stem


def companions(name: str, path: Path | None = None) -> tuple[str, str, str]:
    """The telemetry copy, the keys folder and the reports folder taken with the workspace copy `name` (store.backup names the four by one stamp and
    kind)."""
    stem = (path or store.db_path()).stem
    rest = name[len(stem) + 1:-len(".db")]
    return f"{stem}-telemetry-{rest}.db", f"{stem}-keys-{rest}", f"{stem}-reports-{rest}"


def _copy_files(source: Path, target: Path, private: bool = False) -> int:
    """The files of the folder `source` copied into the folder `target` (made only when there is one), readable by the owner only when `private`; how
    many."""
    files = sorted(f for f in source.iterdir() if f.is_file()) if source.is_dir() else []
    if files:
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copyfile(f, target / f.name)
            if private:
                _private(target / f.name)
    return len(files)


def copy_keys(path: Path, target: Path) -> int:
    """The signing keys of the database `path` copied into the folder `target`, readable by the owner only; how many (0, and no folder, when it has none)."""
    return _copy_files(keys_folder(path), target, private=True)


def copy_folders(path: Path, folder: Path, rest: str) -> None:
    """What a copy of the database `path` takes beside its two files, into the backup folder `folder` under the copy's `<stamp>-<kind>` (`rest`): the
    signing keys and the reports (store.backup)."""
    copy_keys(path, folder / f"{path.stem}-keys-{rest}")
    _copy_files(reports_folder(path), folder / f"{path.stem}-reports-{rest}")


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


def _files(folder: Path) -> list[Path]:
    """The files in a copy's keys or reports folder; none when it has none, or was pruned while being listed."""
    try:
        return [f for f in folder.iterdir() if f.is_file()]
    except (FileNotFoundError, NotADirectoryError):
        return []


def list_backups() -> list[dict]:
    """The current database's copies, newest first: the workspace copy's name (the one to restore), its kind and when it was made (UTC, with its offset: the
    server's local time with none was read as UTC by the browser, frontend-states-formats-17), the telemetry copy taken with it (None when the company had no
    telemetry then), the number of signing keys and of report files (`reports`: a report is a PDF and a CSV), and the sizes: `bytes` is the whole copy's,
    the workspace's, the telemetry's and the reports' beside it."""
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
        telemetry_name, keys_name, reports_name = companions(f.name)
        telemetry_bytes = _size(folder() / telemetry_name)
        reports = [n for r in _files(folder() / reports_name) if (n := _size(r)) is not None]
        out.append({"name": f.name, "kind": kind, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    "bytes": st.st_size + (telemetry_bytes or 0) + sum(reports), "workspace_bytes": st.st_size,
                    "telemetry": telemetry_name if telemetry_bytes is not None else None, "telemetry_bytes": telemetry_bytes or 0,
                    "keys": len(_files(folder() / keys_name)), "reports": len(reports), "reports_bytes": sum(reports)})
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
        telemetry_name, keys_name, reports_name = companions(old.name, path)
        old.unlink()
        # the telemetry copy, the keys and the reports go with it (the telemetry copies were never removed: founder-console-12)
        (folder(path) / telemetry_name).unlink(missing_ok=True)
        shutil.rmtree(folder(path) / keys_name, ignore_errors=True)
        shutil.rmtree(folder(path) / reports_name, ignore_errors=True)


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
    when it had none, so that no later minute is left beside older plans), its signing keys and its reports (those the folder lacks: a key or a report file
    never changes once made, and a newer one is kept). The files as they are now are copied aside first, as one set too ("restore point"), so a restore can
    be undone. Only a copy of this database is put back: the companies' copies share one folder. Stop the server first (scripts/restore_backup.py runs it
    from the command line). The answer names what was put back: {workspace, telemetry (None when empty), keys, reports, restore_point}."""
    source = (folder() / name).resolve()
    if (source.parent != folder().resolve() or not _ours().match(source.name) or not source.is_file() or source.suffix != ".db"
            or source.stat().st_size == 0 or not _is_database(source, "sites")):     # refuses a file that is not a workspace database
        raise FileNotFoundError(name)
    telemetry_name, keys_name, reports_name = companions(source.name)
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
    keys = _put_back(folder() / keys_name, keys_folder(), private=True)
    reports = _put_back(folder() / reports_name, reports_folder())
    return {"workspace": source.name, "telemetry": telemetry_name if telemetry_copy.exists() else None, "keys": keys, "reports": reports,
            "restore_point": aside.name if aside else None}


def _put_back(copied: Path, mine: Path, private: bool = False) -> int:
    """The files of a copy's keys or reports folder `copied` that the company's folder `mine` lacks, put back; how many the copy holds."""
    files = sorted(_files(copied))
    if files:
        mine.mkdir(parents=True, exist_ok=True)
    for f in files:
        if not (mine / f.name).exists():
            shutil.copyfile(f, mine / f.name)
            if private:
                _private(mine / f.name)
    return len(files)


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
