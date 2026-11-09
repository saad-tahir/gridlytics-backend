#!/usr/bin/env python3
"""Put a company's backup back (audit founder-console-01): its workspace database, the telemetry file copied with it and its sites' signing keys, together,
so that plans and telemetry stay in step. Stop the server first: this refuses while the launcher (`scripts/start_local.py` in the web app's repository)
holds the data folder, and holds it itself while the files are put back.

    python scripts/restore_backup.py                                   # the companies, and how many copies each has
    python scripts/restore_backup.py --company <id or name>            # that company's copies, newest first
    python scripts/restore_backup.py --company <id or name> <copy>     # put that copy back

The files as they are now are copied aside first, as one set too ("restore point"), so a restore can be undone by putting that copy back the same way. The
data folder is the server's: GRIDLYTICS_DATA_DIR, else the folder of GRIDLYTICS_DB (the backend's data/ by default). The founder console's Backups page lists
the same copies under the same names. Times are Pakistan time. A refusal is said in a sentence, with exit status 2.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from gridlytics import backups, store  # noqa: E402
from gridlytics import platform as platformdb  # noqa: E402

#: Pakistan time (UTC+5, no daylight saving)
PKT = timezone(timedelta(hours=5))


class Stop(Exception):
    """A refusal: its sentence goes to the standard error and the exit status is 2."""


def hold(folder: Path):
    """The launcher's lock on the data folder (start_local.hold: the same file in the temporary folder, named after the folder's path), taken while the
    files are put back, so that no server starts on them meanwhile; refused while a server holds it."""
    key = hashlib.sha256(os.path.normcase(str(folder.resolve())).encode("utf-8")).hexdigest()[:20]
    handle = open(Path(tempfile.gettempdir()) / f"gridlytics-data-{key}.lock", "a+b")
    try:
        try:
            import msvcrt
        except ImportError:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise Stop(f"A Gridlytics server is running on the data folder {folder}: stop it first, then run this again.") from None
    return handle


def _companies() -> list[dict]:
    if not platformdb.db_path().exists():
        raise Stop(f"There is no registry of companies in {platformdb.data_dir()}: set GRIDLYTICS_DATA_DIR to the server's data folder.")
    return platformdb.list_companies()


def _company(which: str) -> dict:
    """A company by its id, or by its name (any case) when no other company has that name."""
    rows, which = _companies(), which.strip()
    for row in rows:
        if row["id"] == which:
            return row
    named = [row for row in rows if row["name"].strip().casefold() == which.casefold()]
    if len(named) == 1:
        return named[0]
    if named:
        raise Stop(f"{len(named)} companies are named {which}: give the id of the one meant (run this with no arguments to list them).")
    raise Stop(f"No company has the id or the name {which!r}: run this with no arguments to list the companies.")


def _mb(n: int) -> str:
    return f"{n / 1048576:,.1f} MB"


def _when(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(PKT).strftime("%Y-%m-%d %H:%M")


def _copies(company: dict) -> list[dict]:
    with store.use_company(platformdb.company_file(company)):
        return backups.list_backups()


def list_companies(out) -> None:
    rows = _companies()
    print(f"Companies in {platformdb.data_dir()}; list one's copies with --company <id or name>:", file=out)
    if not rows:
        print("  none yet", file=out)
    for row in rows:
        n = len(_copies(row))
        print(f"  {row['id']}  {row['name']}  (Gridlytics {'on' if row['enabled'] else 'off'}, {n} cop{'y' if n == 1 else 'ies'})", file=out)


def list_copies(company: dict, out) -> None:
    copies = _copies(company)
    print(f"Copies of {company['name']} ({company['id']}), newest first, in Pakistan time; put one back with --company {company['id']} <copy>:", file=out)
    if not copies:
        print("  none yet", file=out)
    for b in copies:
        keys = f"{b['keys']} signing key{'' if b['keys'] == 1 else 's'}"
        telemetry = f"telemetry {_mb(b['telemetry_bytes'])}" if b["telemetry"] else "no telemetry"
        print(f"  {b['name']}\n      {b['kind']}, {_when(b['modified'])}, {_mb(b['bytes'])} (workspace {_mb(b['workspace_bytes'])}, {telemetry}, {keys})",
              file=out)


def restore(company: dict, name: str, out) -> None:
    path = platformdb.company_file(company)
    with hold(platformdb.data_dir()):
        try:
            with store.use_company(path):
                done = backups.restore(name.strip())
        except FileNotFoundError:
            raise Stop(f"{company['name']} has no copy named {name}: list its copies with --company {company['id']}.") from None
        except (OSError, sqlite3.Error) as e:
            raise Stop(f"The copy {name} could not be put back ({e}); the files as they were are kept as a restore point when one was made.") from None
    telemetry = (f"its telemetry file {done['telemetry']}" if done["telemetry"]
                 else "an empty telemetry file (the copy was made before the company had any telemetry)")
    keys = f"{done['keys']} signing key{'' if done['keys'] == 1 else 's'}" if done["keys"] else "no signing keys (the copy holds none)"
    print(f"Put back {done['workspace']} as {company['name']}'s workspace ({path}), with {telemetry} and {keys}.", file=out)
    if done["restore_point"]:
        print(f"The files as they were are kept as the restore point {done['restore_point']}; put that back the same way to undo this.", file=out)
    else:
        print("There was nothing to keep as a restore point: the company's workspace file was empty.", file=out)


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python scripts/restore_backup.py",
                                     description="Put a company's backup back: its workspace, its telemetry and its signing keys together. Stop the server first.")
    parser.add_argument("--company", metavar="ID_OR_NAME", help="the company, by its id or its name")
    parser.add_argument("copy", nargs="?", help="the copy to put back, by the name its list gives")
    args = parser.parse_args(argv)
    try:
        if args.company is None:
            if args.copy:
                raise Stop("Say whose copy it is: --company <id or name>.")
            list_companies(out)
        elif args.copy is None:
            list_copies(_company(args.company), out)
        else:
            restore(_company(args.company), args.copy, out)
    except Stop as stop:
        print(stop, file=err)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
