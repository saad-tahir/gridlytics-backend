"""The engine's version stamp (lifted from solarlytics-backend at c853130, version.py, Solarlytics' PS-27; its docstring rewritten for Gridlytics in
round 7, audit parity-architecture-12).

The stamp is the release's version and a fingerprint of the engine itself, "<version>+<fingerprint>": the SHA-256 of every Python and data (.json)
file of the package, in the order of their paths, cut to ten hexadecimal digits. It is worked once, when the package is imported, from the package's
files as they are then. The modules the server loads as it starts are what it runs, so an edit of one of them on disk while the server runs changes
neither what it does nor its stamp; a module first imported when it is first used is read then, so an edit of it while the server runs can change its
answers without changing the stamp, until the next start. Any change to the code gives a new stamp at the next start, even where the version number
was not raised. What a company's own data holds (its tariff versions, its fuel prices, its sites' settings) is not code and is not in the stamp: the
stored results record the versions of those they used themselves (a savings day's tariff_version and native_capture_version, say).

It goes on every answer of the API as the header `X-Gridlytics-Engine` (api.py), the health route's included. Each stored result also records the
stamp of the engine that computed it, at the time it was computed, so a figure computed after a change of the code is told apart from one computed
before it, and a saving shown to a customer, and the fee based on it, can be traced to the code that made it:

* a plan, solved or failed: `summary.engine` (plans.tick);
* a savings day, each baseline's row: `engine` in its data (verify's scoring); the Savings answer gives each day's (`days[].engine`, null for a day
  stored before it was recorded);
* a bill rebuilt from the data: `rebuilt.engine` (verify.rebuild_bill, and again when a credit carried from the bill before settles it anew);
* a report: its row's `engine` (schema version 6). Every page of its PDF says in its foot the engine that made it and, for a report of savings (the
  monthly savings report, the pilot report), the engines that computed its days' savings; its CSV gives `report_engine` on every row and, for a
  report of savings, `day_engine`, the engine of the row's day. The engine that made a report is not always the one that computed the stored days
  it prints, so both are given.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import __version__

PACKAGE = Path(__file__).resolve().parent


def fingerprint(root: Path = PACKAGE) -> str:
    """Ten hexadecimal digits of the SHA-256 over every .py and .json file under `root`, each as its path relative to `root` and its bytes, in the order of the
    paths (caches and compiled files are not read)."""
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.suffix in (".py", ".json") and "__pycache__" not in p.parts):
        h.update(f.relative_to(root).as_posix().encode("utf-8") + b"\0")
        h.update(f.read_bytes() + b"\0")
    return h.hexdigest()[:10]


#: The engine's stamp, "<version>+<fingerprint>", worked as the package is imported, from its files as they are then
STAMP = f"{__version__}+{fingerprint()}"


def stamp() -> str:
    """The engine's stamp, as worked when the package was imported."""
    return STAMP
