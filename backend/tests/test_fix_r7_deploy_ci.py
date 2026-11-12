"""Round 7 (audit), work package deploy-ci: CI, the release and load scripts, and the pins (plan Part 10.1, 10.7 and 10.9; DEP-01, DEP-02, DEP-04).

- parity-architecture-01: neither repository had a CI workflow, so pull requests into v1-build were merged with no automated test, audit or image build.
  `.github/workflows/ci.yml` is the family's shape (solarlytics-backend at c853130): the backend tests on Linux with Python 3.12 and `pytest -n auto` on
  every push and pull request, the web repository checked out beside with the read-only FRONTEND_READ_TOKEN, the tests on Windows, pip-audit, the licence
  gate, and the solver benchmarks and the Docker image on a push of v1-build. Without the token (before the owner adds it, or on a fork's pull request)
  the run warns and the tests that read the web repository skip, each saying why, instead of failing the checkout (follow-up frontend-read-token).
- parity-architecture-16: the founder console named scripts/restore_backup.py (founder-console's, now there), and GRIDLYTICS_WEB_DIST and the ignored
  release/ folder pointed at a release build no script made. scripts/build_release.py makes the one-port release (DEP-04: the API and a local-mode web
  build on 8061, with its own data folder, a daily copy and the restore script set to it) and scripts/load_check.py measures a server of its own on
  throwaway data (DEP-02), never the owner's data or ports. No fetch_tariffs or fetch_schedules: NEPRA's and OGRA's pages are not scraped.

The workflow is read as text (the tests' Python has no YAML parser): each job is the block under its name."""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import sibling

ROOT = Path(__file__).resolve().parents[2]


def _workflow() -> str:
    return (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _jobs() -> dict[str, str]:
    """Each job's block, by its name (the lines under `  <name>:` up to the next job)."""
    body = _workflow().split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9-]*):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def _steps_run(job: str) -> list[str]:
    return [m.strip() for m in re.findall(r"^\s+(?:- )?run: (.+)$", job, flags=re.M)]


# ------------------------------------------------------------------------------------------------ parity-architecture-01: CI

def test_ci_runs_on_every_push_and_pull_request_and_a_push_cancels_the_run_it_replaces():
    wf = _workflow()
    on = wf.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    for trigger in ("push:", "pull_request:", "workflow_dispatch:", "schedule:"):
        assert re.search(rf"^  {trigger}", on, flags=re.M), trigger
    # the push and pull request runs only: a weekly or by-hand run is never cancelled
    assert "cancel-in-progress: ${{ github.event_name == 'push' || github.event_name == 'pull_request' }}" in wf
    assert re.search(r"^permissions:\n  contents: read$", wf, flags=re.M), "the jobs read the repository only"
    assert set(_jobs()) == {"backend-linux", "backend-windows", "audit", "licences", "bench", "image"}


def test_the_backend_tests_run_on_linux_with_python_312_in_parallel_beside_the_web_repository():
    job = _jobs()["backend-linux"]
    assert "runs-on: ubuntu-latest" in job and 'python-version: "3.12"' in job
    assert "python -m pip install -r requirements-dev.txt" in _steps_run(job)
    assert "python -m pytest tests -q -p no:cacheprovider -n auto -rs" in _steps_run(job)
    # the web repository beside this one, as gridlytics-frontend, read with the owner's read-only token; the tests find it there (sibling.py)
    assert "repository: ${{ github.repository_owner }}/gridlytics-frontend\n" in job and "path: gridlytics-frontend\n" in job
    assert "token: ${{ secrets.FRONTEND_READ_TOKEN }}" in job
    assert "GRIDLYTICS_FRONTEND_DIR: ${{ github.workspace }}/gridlytics-frontend\n" in _workflow()


@pytest.mark.parametrize("name", ["backend-linux", "backend-windows"])
def test_without_the_token_the_run_warns_and_skips_the_web_repositorys_tests_instead_of_failing(name):
    job = _jobs()[name]
    # only whether the secret is set reaches the job's environment; the token itself goes to the checkout step alone
    assert "HAS_FRONTEND_TOKEN: ${{ secrets.FRONTEND_READ_TOKEN != '' }}" in job
    assert job.count("${{ secrets.FRONTEND_READ_TOKEN }}") == 1
    checkout = job.split("- name: The web repository beside this one", 1)[1].split("\n      - ", 1)[0]
    assert "if: env.HAS_FRONTEND_TOKEN == 'true'" in checkout
    degrade = job.split("- name: Without the token,", 1)[1].split("\n      - ", 1)[0]
    assert "if: env.HAS_FRONTEND_TOKEN != 'true'" in degrade
    assert "::warning title=FRONTEND_READ_TOKEN is not set::" in degrade and "BACKEND_ASKS" in degrade
    assert 'echo "GRIDLYTICS_SKIP_SIBLING=1" >> "$GITHUB_ENV"' in degrade


def test_without_the_token_each_skipped_test_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(sibling, "REPO", tmp_path / "gridlytics-backend")
    monkeypatch.delenv("GRIDLYTICS_FRONTEND_DIR", raising=False)
    monkeypatch.setenv("GRIDLYTICS_SKIP_SIBLING", "1")
    with pytest.raises(pytest.skip.Exception) as skipped:
        sibling.frontend_file("frontend/src/types/api.ts")
    assert "FRONTEND_READ_TOKEN" in str(skipped.value) and "BACKEND_ASKS" in str(skipped.value)
    # without the switch a missing web repository still fails, as before
    monkeypatch.delenv("GRIDLYTICS_SKIP_SIBLING")
    with pytest.raises(FileNotFoundError, match="clone the web repository"):
        sibling.frontend_file("frontend/src/types/api.ts")


def test_windows_runs_on_a_pull_request_into_main_by_hand_and_weekly():
    job = _jobs()["backend-windows"]
    assert "runs-on: windows-latest" in job and 'python-version: "3.12"' in job
    assert ("if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule' || (github.event_name == 'pull_request' && "
            "github.base_ref == 'main')") in job
    assert "python -m pytest tests -q -p no:cacheprovider -n auto -rs" in _steps_run(job)


def test_the_audit_and_licence_gates_read_the_apps_own_pins():
    audit = _jobs()["audit"]
    assert "pip-audit -r requirements.txt" in _steps_run(audit)
    licences = _jobs()["licences"]
    assert "python -m pip install -r requirements.txt pip-licenses" in _steps_run(licences)
    allowed = re.search(r'--allow-only="([^"]+)"', licences).group(1).split(";")
    assert {"MIT", "BSD", "Apache", "ISC", "PSF", "HPND", "MPL-2.0"} <= set(allowed)
    # plan Part 14.3: no copyleft licence passes the gate
    assert not [name for name in allowed if re.search(r"GPL|EPL|SSPL|BSL|Commons", name)], allowed


def test_the_benchmarks_and_the_image_run_on_a_push_of_v1_build_by_hand_and_weekly():
    jobs = _jobs()
    when = ("if: github.event_name == 'workflow_dispatch' || github.event_name == 'schedule' || (github.event_name == 'push' && "
            "github.ref == 'refs/heads/v1-build')")
    bench, image = jobs["bench"], jobs["image"]
    assert when in bench and when in image
    # the opt-in wall-clock tests, alone (no -n): the only place their claims are checked
    assert "python -m pytest tests -q -p no:cacheprovider -m slow -rs" in _steps_run(bench)
    assert "docker build -t gridlytics:ci ." in _steps_run(image)
    assert "{{.State.Health.Status}}" in image and "-v gridlytics-ci-data:/data" in image and "^server:" in image


def test_the_workflow_holds_no_secret_never_the_owners_data_and_pins_each_action():
    wf = _workflow()
    assert not re.search(r"ghp_|github_pat_|gho_|eyJ[\w-]{10,}|sb_secret_", wf)
    assert set(re.findall(r"secrets\.([A-Z_]+)", wf)) == {"FRONTEND_READ_TOKEN"}
    assert "data/gridlytics.db" not in wf and "GRIDLYTICS_DATA_DIR" not in wf and "GRIDLYTICS_DB" not in wf
    # the tests never run the scheduler (conftest); only the image sets it, inside its own container
    assert "GRIDLYTICS_SCHEDULER" not in wf
    uses = re.findall(r"uses: (\S+)", wf)
    assert uses and all(re.fullmatch(r"actions/(checkout|setup-python)@v\d+", u) for u in uses), uses


# ------------------------------------------------------------------------------------------------ parity-architecture-16: the release and load scripts

def _script(name: str):
    """A script of scripts/ as a module (they are run as files, not imported by the package)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"script_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_every_script_the_console_and_the_docs_name_exists():
    # the founder console's Backups page and routes_founder name restore_backup.py; api.py's GRIDLYTICS_WEB_DIST and the ignored release/ folder name a
    # release build; DEP-02 names a load check
    for name in ("restore_backup", "build_release", "load_check", "export_contract"):
        assert (ROOT / "scripts" / f"{name}.py").is_file(), name
    assert "scripts/restore_backup.py" in (ROOT / "backend" / "gridlytics" / "routes_founder.py").read_text(encoding="utf-8")


def test_the_release_build_refuses_a_one_folder_copy_and_a_folder_without_the_web_app(tmp_path):
    release = _script("build_release")
    with pytest.raises(SystemExit, match="No web app at"):
        release.web_folder(str(tmp_path / "nowhere"))
    both = tmp_path / "both"
    both.joinpath("frontend").mkdir(parents=True)
    both.joinpath("frontend", "package.json").write_text("{}", encoding="utf-8")
    (both / "backend" / "gridlytics").mkdir(parents=True)
    (both / "backend" / "gridlytics" / "api.py").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="holds the engine too"):
        release.web_folder(str(both))
    web = tmp_path / "web-repository"
    web.joinpath("frontend").mkdir(parents=True)
    web.joinpath("frontend", "package.json").write_text("{}", encoding="utf-8")
    assert release.web_folder(str(web)) == web.joinpath("frontend").resolve()
    # a built folder handed in must be a build
    with pytest.raises(SystemExit, match="no index.html"):
        release.main(["--dist", str(tmp_path / "nowhere"), "--out", str(tmp_path / "out")])


def _dist(folder: Path) -> Path:
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text("<!doctype html><title>Gridlytics</title><div id=root></div>", encoding="utf-8")
    (folder / "assets" / "index-test.js").write_text("console.log('page')", encoding="utf-8")
    return folder


def test_the_release_folder_runs_the_api_and_the_page_on_one_port_with_its_own_data(tmp_path):
    release = _script("build_release")
    out = tmp_path / "out"
    release.main(["--dist", str(_dist(tmp_path / "dist")), "--out", str(out)])
    from gridlytics import __version__
    folder = out / f"gridlytics-{__version__}"
    assert (folder / "app" / "backend" / "gridlytics" / "api.py").is_file()
    assert not list((folder / "app").rglob("__pycache__")) and not list((folder / "app").rglob("*.pyc"))
    assert folder.joinpath("app", "frontend", "dist", "index.html").is_file()
    assert (folder / "requirements.txt").read_text(encoding="utf-8") == (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert (folder / "data").is_dir() and not any((folder / "data").iterdir()), "the release starts with an empty data folder of its own"
    for name in ("run.py", "restore_backup.py", "Start Gridlytics.cmd", "README.txt"):
        assert (folder / name).is_file(), name
    run = (folder / "run.py").read_text(encoding="utf-8")
    for words in ('"GRIDLYTICS_PORT", "8061"', 'HERE / "data"', '"GRIDLYTICS_SCHEDULER", "1"', '"GRIDLYTICS_DAILY_BACKUP", "1"', "GRIDLYTICS_WEB_DIST",
                  'host="127.0.0.1"', "server_header=False"):
        assert words in run, words
    # the restore script is pointed at the release's own app and data folder
    restore = (folder / "restore_backup.py").read_text(encoding="utf-8")
    assert 'HERE / "app" / "backend"' in restore and 'os.environ.setdefault("GRIDLYTICS_DATA_DIR"' in restore
    assert "parents[1]" not in restore
    import zipfile
    with zipfile.ZipFile(out / f"gridlytics-{__version__}.zip") as z:
        names = set(z.namelist())
    assert f"gridlytics-{__version__}/run.py" in names and f"gridlytics-{__version__}/app/frontend/dist/index.html" in names
    # the release is started as the .cmd starts it, on a spare port and without the scheduler or a browser: the page and the API on one port
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRIDLYTICS_")}
    env.update(GRIDLYTICS_PORT=str(port), GRIDLYTICS_NO_BROWSER="1", GRIDLYTICS_SCHEDULER="0", GRIDLYTICS_DAILY_BACKUP="0", GRIDLYTICS_LOGS="0")
    # the founder console's settings come from the release's own data/.env.founder (one of the four here, so the console names the other three)
    (folder / "data" / ".env.founder").write_text("GRIDLYTICS_FOUNDER_USERNAME='founder'\n", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "run.py"], cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(120):
            assert server.poll() is None, server.stdout.read()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    assert r.status == 200 and "server" not in {k.lower() for k, _ in r.getheaders()}
                break
            except OSError:
                time.sleep(0.25)
        else:
            pytest.fail("the release did not answer")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/sites/abc/plan", timeout=5) as r:
            assert "<title>Gridlytics</title>" in r.read().decode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/founder/login", data=b'{"username": "x", "password": "y"}', method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(req, timeout=5)
        said = refused.value.read().decode("utf-8")
        assert refused.value.code == 503 and "GRIDLYTICS_FOUNDER_PASSWORD_HASH" in said and "GRIDLYTICS_FOUNDER_USERNAME" not in said, said
    finally:
        server.terminate()
        server.wait(timeout=15)


def test_the_load_check_refuses_the_owners_ports_and_a_busy_one(capsys):
    load = _script("load_check")
    for port in (8051, 5195, 8061):
        with pytest.raises(SystemExit, match="is the owner's"):
            load.main(["--port", str(port)])
    import socket
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        with pytest.raises(SystemExit, match="already in use"):
            load.main(["--port", str(busy.getsockname()[1])])


def test_the_load_check_measures_a_server_of_its_own_on_throwaway_data(capsys):
    load = _script("load_check")
    code = load.main(["--port", str(_free_port()), "--threads", "2", "--seconds", "1", "--demo-days", "0"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert re.search(r"2 threads for \d+ s: \d+ requests, [\d.]+ a second, 0 failed", out), out
    for name in ("fleet", "sites", "alerts", "tariffs", "meta"):
        assert re.search(rf"^\s+{name}\s+\d+ sent", out, flags=re.M), name
    # its throwaway data folder went with its server
    folder = re.search(r"Seeding a throwaway data folder \((.+?)\)", out).group(1)
    assert Path(folder).name.startswith("gridlytics-load-") and not Path(folder).exists()


# ------------------------------------------------------------------------------------------------ parity-architecture-20: the family's pins

#: The shared sign-in stack as solarlytics-backend pins it (c853130: requirements.txt and requirements-dev.txt), where the identity, founder and mfa modules
#: lifted here were last tested; raised in both products together
FAMILY_PINS = {"fastapi": "0.150.0", "starlette": "1.7.0", "pydantic": "2.13.5", "uvicorn": "0.54.0", "PyJWT": "2.15.1", "cryptography": "50.0.2"}
FAMILY_DEV_PINS = {"pytest": "9.1.1", "pytest-xdist": "3.8.0", "httpx2": "2.13.1"}


def _pins(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-r "):
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9][0-9A-Za-z.]*)", line)
        assert m, f"{path.name}: {line!r} is not an exact pin"
        out[m.group(1)] = m.group(2)
    return out


def test_every_dependency_is_pinned_exactly_and_the_sign_in_stack_is_the_familys():
    runtime, dev = _pins(ROOT / "requirements.txt"), _pins(ROOT / "requirements-dev.txt")
    assert {k: runtime.get(k) for k in FAMILY_PINS} == FAMILY_PINS
    assert {k: dev.get(k) for k in FAMILY_DEV_PINS} == FAMILY_DEV_PINS
    assert "-r requirements.txt" in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    # the solver pair stays at the measured versions until the CI benchmark passes on newer ones (plan Part 10.7), with that reason written beside it
    assert runtime["scipy"] == "1.17.1" and runtime["numpy"] == "2.4.3"
    head = (ROOT / "requirements.txt").read_text(encoding="utf-8").split("\nfastapi==", 1)[0]
    assert "scipy 1.17.1 / numpy 2.4.3" in head and "benchmark" in head and "pip-audit" in head


def test_the_sign_in_stack_matches_a_solarlytics_checkout_beside_when_there_is_one():
    # a read-only checkout of solarlytics-backend beside this repository (or GRIDLYTICS_SOLARLYTICS_DIR); none in CI, where the constants above hold
    named = os.environ.get("GRIDLYTICS_SOLARLYTICS_DIR", "").strip()
    other = Path(named) if named else ROOT.parent / "solarlytics-backend"
    if not (other / "requirements.txt").is_file():
        pytest.skip(f"no solarlytics-backend checkout at {other}: the family's pins are compared with FAMILY_PINS only")
    theirs = {**_pins(other / "requirements.txt"), **_pins(other / "requirements-dev.txt")}
    ours = {**_pins(ROOT / "requirements.txt"), **_pins(ROOT / "requirements-dev.txt")}
    assert {k: ours[k] for k in [*FAMILY_PINS, *FAMILY_DEV_PINS]} == {k: theirs[k] for k in [*FAMILY_PINS, *FAMILY_DEV_PINS]}


# ------------------------------------------------------------------------------------------------ parity-architecture-13: the secret files git never takes

_GIT = __import__("shutil").which("git") is not None and (ROOT / ".git").exists()


def _ignored(path: str) -> bool:
    import subprocess
    return subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT).returncode == 0


@pytest.mark.skipif(not _GIT, reason="needs git and the repository")
def test_every_secret_file_a_deployer_or_the_seed_may_make_is_ignored_and_the_examples_are_not():
    # the family's rules (solarlytics-backend/.gitignore): a filled-in server environment (DEPLOY.md copies .env.production.example to .env.production),
    # the founder console's settings wherever they are written, the token-signing secret and the signing keys of a data folder kept anywhere in the
    # repository (not only data/), the demo accounts' passwords, and Python's caches
    for path in (".env", ".env.production", ".env.founder", ".env.local", "backend/.env", "data/.env.founder", "platform.secret",
                 "data/platform.secret", "scratch-data/platform.secret", "scratch-data/keys/site-1-k1.pem", "demo/DEMO_ACCOUNTS.local.md",
                 "demo/other.local.md", "backend/gridlytics/__pycache__/api.cpython-312.pyc", "backend/x.pyc", ".pytest_cache/v/cache/lastfailed"):
        assert _ignored(path), f"{path} is not ignored: git add -A would commit it"
    for path in (".env.production.example", ".env.example", "demo/companies.template.csv", "backend/gridlytics/identity.py", "docs/DEPLOY.md"):
        assert not _ignored(path), f"{path} is ignored, but it belongs in the repository"
