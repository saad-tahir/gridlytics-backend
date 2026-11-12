"""Where the web repository is, for the backend tests that read its files (round 7, audit parity-architecture-09: the family's split mechanism, Solarlytics'
stage S0a `backend/tests/sibling.py`, carried over).

The web app is in its own repository (`gridlytics-frontend` on GitHub) checked out beside this one. A test that holds the server's copy of something to the
page's (the permission names, the sign-in refusal codes, the headers the page sends, the money fields, the test fixtures) reads the page's file through
`frontend_file(rel)`, with `rel` from the web repository's root (`frontend/src/types/api.ts`, `scripts/start_local.py`, `README.md`). The root is
`GRIDLYTICS_FRONTEND_DIR` when it is set, else `gridlytics-frontend` beside this repository; never a folder that also holds the engine (a one-folder copy
of both). A missing web repository fails the test and says what to do (`GRIDLYTICS_SKIP_SIBLING=1` skips it instead, for a quick run on a developer's
machine only, never in CI); a file missing from the web repository always fails, so a scan of a folder that is not there never passes with nothing scanned."""
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MISSING = "clone the web repository (gridlytics-frontend) beside this folder or set GRIDLYTICS_FRONTEND_DIR"


def _missing(what: str):
    if os.environ.get("GRIDLYTICS_SKIP_SIBLING") == "1":
        pytest.skip(what)
    raise FileNotFoundError(f"{what}: {MISSING}")


def web_root() -> Path:
    """The web repository's root: `GRIDLYTICS_FRONTEND_DIR`, else `gridlytics-frontend` beside this repository."""
    named = os.environ.get("GRIDLYTICS_FRONTEND_DIR", "").strip()
    root = Path(named).resolve() if named else REPO.parent / "gridlytics-frontend"
    if not (root / "frontend" / "package.json").is_file():
        _missing(f"No web repository at {root}")
    if (root / "backend" / "gridlytics" / "api.py").is_file():
        raise FileNotFoundError(f"{root} is not the web repository (it holds the engine too): {MISSING}")
    return root


def frontend_file(rel: str) -> Path:
    """A file or folder of the web repository, by its path from that repository's root; one that is not there fails (a missing folder must not pass a scan)."""
    root = web_root()
    path = root.joinpath(*rel.split("/"))
    if not path.exists():
        raise FileNotFoundError(f"The web repository at {root} has no {rel}")
    return path
