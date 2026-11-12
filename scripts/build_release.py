#!/usr/bin/env python3
"""Build the one-port release of Gridlytics (plan Part 2.7, DEP-04; round 7, audit parity-architecture-16; Solarlytics' scripts/build_release.py at
c853130, written again for Gridlytics): one folder and a zip that run the API and the web app on one port, with a data folder of their own, and no
development server.

    python scripts/build_release.py [--web <folder>] [--dist <folder>] [--out <folder>]

Makes `release/gridlytics-<version>/` (git-ignored) and `release/gridlytics-<version>.zip` with:

- `app/backend/gridlytics/` (the engine and the API) and `app/frontend/dist/` (the built web app, which `run.py` names to the API in GRIDLYTICS_WEB_DIST,
  so the page and /api share http://127.0.0.1:8061);
- `requirements.txt`, this repository's pins as they are, the versions the tests pass with;
- `run.py` and `Start Gridlytics.cmd`, which start it on 127.0.0.1:8061 (GRIDLYTICS_PORT changes it) in local mode, with its own data folder `data/`
  (the registry, the companies' files, the keys, the reports, the daily copies of the last 14 days and the log), the 15-minute scheduler in its one
  process, and the founder console's four settings read from `data/.env.founder` (written by hand, or by the demo seed);
- `restore_backup.py`, the restore of a company's copy (scripts/restore_backup.py) set to the release's own app and data folder.

The web app is the web repository's (`gridlytics-frontend`): `--web <folder>` names it (the repository or its `frontend/`), else `gridlytics-frontend`
beside this repository is built, in local mode, calling /api on its own host (VITE_AUTH_MODE=local, no VITE_API_BASE_URL), whatever the machine's
environment says. A folder that also holds the engine (a one-folder copy) is refused. `--dist <folder>` takes a web app already built so (its
`frontend/dist`) instead of building one. The build never reads or writes a data folder: the release starts with an empty one.

It needs Python 3.12 (the pinned numpy and scipy need it) and, to build the web app, Node 22 with npm. It makes no git tag: tagging and publishing are the
owner's; it prints the tag to make.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: The release's port (plan Part 2.7: 8051 and 5195 are the owner's, 8151 and 5295 a scratch run's, 8061 the release's)
PORT = 8061

RUN = '''"""Start Gridlytics: the API and the web app on one port, with this folder's own data folder, its daily copies and the 15-minute scheduler."""
import os
import re
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
PORT = int(os.environ.get("GRIDLYTICS_PORT", "8061"))
os.environ.setdefault("GRIDLYTICS_DATA_DIR", str(DATA))
os.environ.setdefault("GRIDLYTICS_DB", str(DATA / "gridlytics.db"))
os.environ.setdefault("GRIDLYTICS_AUTH_MODE", "local")
os.environ.setdefault("GRIDLYTICS_DAILY_BACKUP", "1")
os.environ.setdefault("GRIDLYTICS_SCHEDULER", "1")
os.environ.setdefault("GRIDLYTICS_WEB_DIST", str(HERE / "app" / "frontend" / "dist"))
# The founder console's four settings in data/.env.founder (written by hand or by the demo seed): NAME='value', quotes taken off, nothing expanded (its hash
# holds $ signs); a setting already in the environment wins
_SETTING = re.compile(r"""(GRIDLYTICS_FOUNDER_[A-Z_]+)=(?:'([^']*)'|"([^"]*)"|([^\\s'"]*))""")
_founder = DATA / ".env.founder"
if _founder.is_file():
    for _line in _founder.read_text(encoding="utf-8").splitlines():
        _m = _SETTING.fullmatch(_line.strip())
        if _m and not os.environ.get(_m.group(1), "").strip():
            os.environ[_m.group(1)] = next(v for v in _m.groups()[1:] if v is not None)
sys.path.insert(0, str(HERE / "app" / "backend"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Gridlytics on {url} (data folder {os.environ['GRIDLYTICS_DATA_DIR']}). Leave this window open; close it to stop.")
    if os.environ.get("GRIDLYTICS_NO_BROWSER") != "1":
        threading.Thread(target=lambda: (time.sleep(3), webbrowser.open(url)), daemon=True).start()
    # one process, so one scheduler for this data folder; on this machine only, with no proxy to believe and no server header
    uvicorn.run("gridlytics.api:app", host="127.0.0.1", port=PORT, log_level="warning", proxy_headers=False, server_header=False)
'''

CMD = r'''@echo off
rem Start Gridlytics. Needs Python 3.12; set GRIDLYTICS_PYTHON to its path if needed.
cd /d "%~dp0"
if not "%GRIDLYTICS_PYTHON%"=="" (set PY="%GRIDLYTICS_PYTHON%") else (where py >nul 2>nul && (set PY=py -3.12) || (set PY=python))
rem The packages are installed once; a marker says the install finished, so a failed one is tried again
if not exist ".venv\installed.ok" (
  echo First start: making a private Python environment and installing the tested packages...
  if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :fail
  echo ok> ".venv\installed.ok"
)
".venv\Scripts\python.exe" run.py || (echo Gridlytics stopped with an error; the message is above. & pause)
goto :eof
:fail
echo Could not set up Python. Install Python 3.12, check the internet connection, or set GRIDLYTICS_PYTHON to its path.
pause
'''

#: The line of scripts/restore_backup.py that finds the package in the repository, and what it becomes in the release (its own app and data folder)
RESTORE_FIND = 'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))'
RESTORE_RELEASE = ('HERE = Path(__file__).resolve().parent\n'
                   'os.environ.setdefault("GRIDLYTICS_DATA_DIR", str(HERE / "data"))\n'
                   'os.environ.setdefault("GRIDLYTICS_DB", str(HERE / "data" / "gridlytics.db"))\n'
                   'sys.path.insert(0, str(HERE / "app" / "backend"))')


def web_folder(named: str | None) -> Path:
    """The web app to build (the folder of its package.json): `--web` names the web repository or its frontend/, else gridlytics-frontend beside this
    repository. A folder that also holds the engine (a one-folder copy) is refused."""
    root = Path(named).resolve() if named else (ROOT.parent / "gridlytics-frontend").resolve()
    web = root / "frontend" if (root / "frontend" / "package.json").is_file() else root
    if not (web / "package.json").is_file():
        raise SystemExit(f"No web app at {root}: clone the web repository (gridlytics-frontend) beside this folder, or name it with --web <folder>")
    if (web.parent / "backend" / "gridlytics" / "api.py").is_file():
        raise SystemExit(f"{web.parent} holds the engine too (a one-folder copy): name the web repository with --web <folder>")
    return web


def build_web(web: Path) -> Path:
    """Builds the web app and answers its dist folder: in local mode, calling /api on the API's own host and naming no Supabase project, whatever the
    machine's environment or the web folder's .env files say (Vite takes the process's variables first). npm's output is printed when the build fails."""
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise SystemExit("npm is needed to build the web app (Node 22), or give an already built one with --dist <folder>")
    env = {**os.environ, "VITE_AUTH_MODE": "local", "VITE_API_BASE_URL": "", "VITE_SUPABASE_URL": "", "VITE_SUPABASE_ANON_KEY": ""}
    quiet = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    done = subprocess.run([npm, "run", "build"], cwd=web, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", **quiet)
    if done.returncode != 0:
        print(done.stdout[-6000:], done.stderr[-6000:], sep="\n")
        raise SystemExit(f"The web app's build in {web} failed (exit code {done.returncode}); its output is above")
    return web / "dist"


def assemble(dist: Path, out_root: Path, version: str) -> tuple[Path, Path]:
    """The release folder and its zip, from a built web app; answers both paths."""
    if not (dist / "index.html").is_file():
        raise SystemExit(f"The web app at {dist} has no index.html: build it first, or leave out --dist to build it here")
    restore = (ROOT / "scripts" / "restore_backup.py").read_text(encoding="utf-8")
    if RESTORE_FIND not in restore:
        raise SystemExit("scripts/restore_backup.py no longer finds the package as this build expects: update RESTORE_FIND in build_release.py")
    out = out_root / f"gridlytics-{version}"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(ROOT / "backend" / "gridlytics", out / "app" / "backend" / "gridlytics", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(dist, out / "app" / "frontend" / "dist")
    (out / "data").mkdir(parents=True)
    shutil.copyfile(ROOT / "requirements.txt", out / "requirements.txt")
    (out / "run.py").write_text(RUN, encoding="utf-8", newline="\n")
    (out / "restore_backup.py").write_text(restore.replace(RESTORE_FIND, RESTORE_RELEASE), encoding="utf-8", newline="\n")
    (out / "Start Gridlytics.cmd").write_text(CMD.replace("\n", "\r\n"), encoding="utf-8", newline="")
    (out / "README.txt").write_text(
        f"Gridlytics {version}, the one-port release.\r\n\r\n"
        "On Windows, double-click 'Start Gridlytics.cmd'. The first start makes a private Python environment (it needs Python 3.12 and, once, the internet). "
        f"On Linux: python3.12 -m venv .venv, .venv/bin/python -m pip install -r requirements.txt, then .venv/bin/python run.py.\r\n"
        f"The app opens at http://127.0.0.1:{PORT}/ and keeps everything in data\\ (a copy each day in data\\backup and data\\companies\\backup, the last 14). "
        "People sign in with the accounts the founder console makes, at /admin. Before the first start, write the console's four settings into "
        "data\\.env.founder, one NAME='value' line each (the values in single quotes): GRIDLYTICS_FOUNDER_USERNAME, GRIDLYTICS_FOUNDER_PASSWORD_HASH, "
        "GRIDLYTICS_FOUNDER_TOTP_SECRET and GRIDLYTICS_FOUNDER_SESSION_SECRET. In the folder app\\backend, ..\\..\\.venv\\Scripts\\python.exe -m "
        "gridlytics.founder hash, then totp-secret, then session-secret make them (the backend repository's docs/DEPLOY.md, The founder console).\r\n"
        "To restore a company's copy, close the app and run: .venv\\Scripts\\python.exe restore_backup.py (it lists the companies), then with "
        "--company <id or name>, then with the copy's name.\r\n", encoding="utf-8", newline="")
    archive = out_root / f"{out.name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            z.write(f, f.relative_to(out_root))
    return out, archive


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the one-port release: the API and the built web app on 127.0.0.1:8061, with a data folder of its own.")
    parser.add_argument("--web", metavar="FOLDER", help="the web repository (gridlytics-frontend) or its frontend/; default: gridlytics-frontend beside")
    parser.add_argument("--dist", metavar="FOLDER", help="a web app already built in local mode (its frontend/dist), instead of building one")
    parser.add_argument("--out", metavar="FOLDER", help="where the release folder and its zip go; default: release/ in this repository (git-ignored)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "backend"))
    from gridlytics import __version__
    dist = Path(args.dist).resolve() if args.dist else build_web(web_folder(args.web))
    out, archive = assemble(dist, Path(args.out).resolve() if args.out else ROOT / "release", __version__)
    print(f"Built {out} and {archive}")
    print(f"To tag this release (the owner's step): git tag -a v{__version__} -m \"Gridlytics {__version__}\"")


if __name__ == "__main__":
    main()
