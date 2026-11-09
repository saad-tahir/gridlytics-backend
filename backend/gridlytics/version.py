"""The engine's version stamp (master PS-27, wave 2, 20 October 2026).

The stamp is the release's version and a fingerprint of the engine itself: the SHA-256 of every Python and data file of the package, in the order of their paths, cut
to ten hexadecimal digits. It is worked once, when the package is imported, from the package's files as they are then (review of wave 2: it was worked at the first
request, from the files on disk then). The Python modules the server loads as it starts are what it runs, so an edit of one of them on disk while the server runs
changes neither what it does nor its stamp. Two things are read later: the climate of the shipped cities (climate.json) is read again each time the settings are
built, which most requests do, and a module first imported when it is first used (market) is read then. An edit of either while the server runs can change its
answers without changing its stamp, until the next start. Any change to the code or to its shipped data (a catalogue, the climate of the shipped cities) gives a new
stamp at the next start, even where the version number was not raised.

It goes on every answer of the API as the header `X-Gridlytics-Engine`, the health route's included. The pages keep the latest stamp they were sent and print it on
the foot of every PDF as "Printed with Gridlytics <stamp>": the engine that served the print, which is not always the one that computed a result stored earlier and
printed now. A calculation package's Engine row carries the stored result's own version, the one recorded with it when it was computed. The stamp is never put in an
answer's body: the replayed answers are compared whole, and a stamp there would change with every edit of the code.
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
