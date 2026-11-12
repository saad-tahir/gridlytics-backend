"""The hosted delivery's files (round 7, audit parity-architecture-02, and tracker-and-docs-12 with its late duplicate parity-supabase-auth-06: plan Part 10.1
and 10.3, PLT-02, DEP-06; Solarlytics' stage F8 test of the same name, solarlytics-backend at c853130, backend/tests/test_deploy_docs.py, re-aimed).

Before, a deployer had no Dockerfile, no .dockerignore, no .env.production.example and no docs/DEPLOY.md: 27 of the 31 settings the server reads were
written nowhere, nothing said that a hosted start in local mode needs its signing key (identity.check_startup refuses it), that the service-role key stays
on the server or that the founder console answers 503 until its four settings are set, and the API said `server: uvicorn` on every answer. Here:

- every GRIDLYTICS_ setting the package reads is in .env.production.example (and nothing it does not read), with no value that is a secret and no default
  that opens the server, in the NAME=value form `docker run --env-file` takes;
- docs/DEPLOY.md names every setting of the server and the web app's build settings, and has the owner's hand steps;
- the image is the Solarlytics S0b shape: python 3.12 slim bookworm, the API only, a user of its own, the data folder on /data, a health check, one uvicorn
  process (one scheduler per data folder) believing the proxy's headers and sending no server header; no sign-in mode is baked in;
- the image's own command serves /api/health with no server header, and a hosted start without the signing key stops, naming it (run here, on a temporary
  data folder, never the owner's).

Nothing else is run: the files are read."""
from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "backend" / "gridlytics"

#: Read but not by the package: the port the image's command passes to uvicorn, and uvicorn's own list of the proxies it believes
OTHERS = {"GRIDLYTICS_PORT", "FORWARDED_ALLOW_IPS"}
#: The web app's build settings (gridlytics-frontend's frontend/.env.example), set in the Vercel project
WEB = ("VITE_AUTH_MODE", "VITE_API_BASE_URL", "VITE_SUPABASE_URL", "VITE_SUPABASE_ANON_KEY")
_LINE = re.compile(r"^(#\s*)?([A-Z][A-Z0-9_]*)=(.*)$")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _words(text: str) -> str:
    """The text with its lines joined as they read (a phrase may wrap)."""
    return " ".join(text.split())


def _section(guide: str, heading: str) -> str:
    """The part of the guide from its heading to the next heading of the same level, as it reads."""
    level = heading.split(" ", 1)[0]
    return _words(guide.split(heading, 1)[1].split(f"\n{level} ", 1)[0])


def _example() -> dict[str, tuple[bool, str]]:
    """The example's settings: name -> (set, the value written). A setting left to its default is written commented out, with that default."""
    out = {}
    for line in _read(".env.production.example").splitlines():
        m = _LINE.match(line)
        if m:
            assert m.group(2) not in out, f"{m.group(2)} is written twice"
            out[m.group(2)] = (m.group(1) is None, m.group(3))
    return out


def _read_by_the_server() -> set[str]:
    """Every GRIDLYTICS_ setting the package reads: each name written whole as a string."""
    names = set()
    for path in PACKAGE.rglob("*.py"):
        names |= set(re.findall(r"""["'](GRIDLYTICS_[A-Z0-9_]+)["']""", path.read_text(encoding="utf-8")))
    return names


# ------------------------------------------------------------------------------------------------ .env.production.example

def test_every_setting_the_server_reads_is_in_the_example_and_nothing_else():
    read = _read_by_the_server()
    # the lifted set of plan Part 10.3 and the EMS's own that the server reads today
    assert {"GRIDLYTICS_AUTH_MODE", "GRIDLYTICS_LOCAL_JWT_SECRET", "GRIDLYTICS_DATA_DIR", "GRIDLYTICS_TRUST_PROXY", "GRIDLYTICS_CORS_ORIGINS",
            "GRIDLYTICS_CORS_ORIGIN_REGEX", "GRIDLYTICS_HOSTS", "GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY", "GRIDLYTICS_FOUNDER_USERNAME",
            "GRIDLYTICS_FOUNDER_PASSWORD_HASH", "GRIDLYTICS_FOUNDER_TOTP_SECRET", "GRIDLYTICS_FOUNDER_SESSION_SECRET", "GRIDLYTICS_SCHEDULER",
            "GRIDLYTICS_WEB_DIST", "GRIDLYTICS_RATE_SIGN_IN", "GRIDLYTICS_TICK_WORKERS"} <= read
    example = set(_example())
    missing = (read | OTHERS) - example
    assert not missing, f"not in .env.production.example: {sorted(missing)}"
    # a setting written there that nothing reads would tell a deployer something the server ignores
    stale = example - read - OTHERS
    assert not stale, f"in .env.production.example but read by nothing: {sorted(stale)}"


def test_the_example_holds_no_secret_and_reads_as_an_env_file():
    for line in _read(".env.production.example").splitlines():
        if line.strip() and not line.startswith("#"):
            # docker run --env-file and systemd's EnvironmentFile take NAME=value as written: no spaces around '=', no quotes, no export
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\"' ]*", line), line
    for name, (_set, value) in _example().items():
        if re.search(r"SECRET|PASSWORD|_KEY$|_HASH$", name):
            assert value == "", f"{name} has a value in the example: secrets are made on the server, never written here"
        assert not re.fullmatch(r"eyJ[\w-]+\.[\w-]+\.[\w-]+|sb_(secret|publishable)_\w+|[A-Za-z0-9_-]{32,}", value), name


def test_the_example_opens_nothing_by_default():
    example = _example()
    set_ = {name: value for name, (on, value) in example.items() if on}
    assert set_.get("GRIDLYTICS_DOCS", "0") != "1"
    assert "*" not in set_.get("GRIDLYTICS_CORS_ORIGINS", "") and "*" not in set_.get("FORWARDED_ALLOW_IPS", "")
    assert set_["GRIDLYTICS_AUTH_MODE"] == "local"
    # behind the TLS proxy the lockout believes X-Forwarded-For only with both: the proxy named to uvicorn, and the server told it has one
    assert set_["GRIDLYTICS_TRUST_PROXY"] == "1" and set_["FORWARDED_ALLOW_IPS"]
    # a hosted copy in local mode refuses to start without its signing key (identity.check_startup): the key is written, empty, to be filled
    assert set_["GRIDLYTICS_LOCAL_JWT_SECRET"] == ""
    # the founder console's four are written, empty: it answers 503 until they are set
    for name in ("GRIDLYTICS_FOUNDER_USERNAME", "GRIDLYTICS_FOUNDER_PASSWORD_HASH", "GRIDLYTICS_FOUNDER_TOTP_SECRET", "GRIDLYTICS_FOUNDER_SESSION_SECRET"):
        assert set_[name] == "", name
    # supabase mode is the owner's to switch on: its settings are written commented out
    for name in ("GRIDLYTICS_SUPABASE_URL", "GRIDLYTICS_SUPABASE_ANON_KEY", "GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY", "GRIDLYTICS_SUPABASE_JWT_SECRET"):
        assert not example[name][0], f"{name} is set in the example"
    # the scheduler is not set here: the image sets it, and a second container on the same data folder must not
    assert "GRIDLYTICS_SCHEDULER" not in set_
    text = _words(" ".join(line.lstrip("#") for line in _read(".env.production.example").splitlines()))
    for words in ("on the server only", "answers 503 until all four are set", "never Boxlytics'", "no sign-up", "no Google"):
        assert words in text, words


# ------------------------------------------------------------------------------------------------ docs/DEPLOY.md

def test_the_guide_names_every_setting_of_the_server_and_of_the_web_app():
    guide = _read("docs/DEPLOY.md")
    missing = [name for name in [*_example(), *WEB] if name not in guide]
    assert not missing, f"not named in docs/DEPLOY.md: {missing}"
    assert ".env.production.example" in guide and "frontend/.env.example" in guide


def test_the_guide_has_the_owners_hand_steps():
    guide = _read("docs/DEPLOY.md")
    for heading in ("## The container", "### The server's environment", "### The founder console", "### The first company and its owner", "### TLS",
                    "## Backups", "## Gridlytics' own Supabase project", "## The owner's checklist for a hosted copy"):
        assert heading in guide, heading
    container = _section(guide, "## The container")
    # why a hosted start stops (identity.check_startup), the proxy's address as the container sees it, one process per data folder
    for words in ("GRIDLYTICS_LOCAL_JWT_SECRET", "refuses to start", "FORWARDED_ALLOW_IPS", "GRIDLYTICS_TRUST_PROXY", "one uvicorn process",
                  "GRIDLYTICS_SCHEDULER=1", "deploy/docker-compose.yml", "/api/health", "--no-server-header"):
        assert words in container, words
    founder = _section(guide, "### The founder console")
    for words in ("python -m gridlytics.founder hash", "python -m gridlytics.founder totp-secret", "python -m gridlytics.founder session-secret",
                  "/admin", "503"):
        assert words in founder, words
    tls = _section(guide, "### TLS")
    for words in ("client_max_body_size 26m", "max_size 26MB", "Strict-Transport-Security", "X-Forwarded-For"):
        assert words in tls, words
    supabase = _section(guide, "## Gridlytics' own Supabase project")
    for words in ("supabase/README.md", "never Boxlytics'", "Allow new users to sign up", "Google", "server only", "SERVICE_ROLE_KEY"):
        assert words in supabase, words
    backups = _section(guide, "## Backups")
    for words in ("scripts/restore_backup.py", "keys", "reports", "off the machine"):
        assert words in backups, words


def test_the_guide_leaves_the_mode_to_the_server_and_adds_people_only_by_the_console():
    guide = _words(_read("docs/DEPLOY.md"))
    assert "never baked into the image" in guide
    # preassigned accounts only (the owner's decisions of 24 October 2026, section 1)
    assert "no sign-up" in guide and "no invitation" in guide
    # Boxlytics' project, its functions and its secrets are no part of Gridlytics
    for words in ("ADMIN_SESSION_SECRET", "send-invite", "founder-admin", "db push --", "/api/auth/setup"):
        assert words not in guide, words


# ------------------------------------------------------------------------------------------------ the image

def _dockerfile() -> str:
    return _read("Dockerfile")


def _command() -> list[str]:
    """The uvicorn command of the image's CMD, as a list (the shell's `exec` and the port variable left to the caller)."""
    cmd = re.search(r'^CMD \["sh", "-c", "exec (uvicorn [^"]*(?:\\"[^"]*)*)"\]$', _dockerfile(), flags=re.M)
    assert cmd, "the image's command is not CMD [\"sh\", \"-c\", \"exec uvicorn ...\"]"
    return shlex.split(cmd.group(1).replace('\\"', '"'))


def test_the_image_is_the_families_shape():
    text = _dockerfile()
    assert re.search(r"^FROM python:3\.12\.\d+-slim-bookworm$", text, flags=re.M), "the base is python 3.12 slim bookworm, pinned to a release"
    # the API only: the package, its pins and the restore script; never the tests, the data, a .env file or the web app
    copies = re.findall(r"^COPY (\S+) ", text, flags=re.M)
    assert set(copies) == {"requirements.txt", "backend/gridlytics", "scripts/restore_backup.py"}, copies
    assert "requirements-dev.txt" not in text
    # a user of its own, which owns /data only
    assert re.search(r"^USER gridlytics$", text, flags=re.M) and "--uid 10001" in text
    assert re.search(r'^VOLUME \["/data"\]$', text, flags=re.M)
    for env in ("GRIDLYTICS_DATA_DIR=/data", "GRIDLYTICS_DB=/data/gridlytics.db", "GRIDLYTICS_DAILY_BACKUP=1", "GRIDLYTICS_SCHEDULER=1"):
        assert env in text, env
    assert re.search(r"^HEALTHCHECK .*\n.*/api/health", text, flags=re.M)
    # no sign-in mode baked in (the server's environment sets it), and no secret set or passed in
    assert "GRIDLYTICS_AUTH_MODE" not in text
    settings = " ".join(re.findall(r"^(?:ENV|ARG) (.*(?:\\\n.*)*)", text, flags=re.M))
    assert not re.search(r"SECRET|PASSWORD|_KEY\b|_HASH\b", settings), settings
    cmd = _command()
    assert cmd[:2] == ["uvicorn", "gridlytics.api:app"]
    # one process, so one scheduler per data folder; the proxy's headers believed (from FORWARDED_ALLOW_IPS only); no `server: uvicorn`
    assert "--workers" not in cmd and "--reload" not in cmd
    assert "--proxy-headers" in cmd and "--no-server-header" in cmd


def test_the_build_leaves_out_the_data_the_tests_and_every_env_file():
    ignored = {line.strip() for line in _read(".dockerignore").splitlines() if line.strip() and not line.startswith("#")}
    for name in ("data", ".scratch", "backend/tests", "**/.env*", "demo/*.local.md", ".git", "release", "**/__pycache__", "frontend"):
        assert name in ignored, name


def test_the_compose_file_runs_one_api_on_this_machine_only():
    compose = _read("deploy/docker-compose.yml")
    assert re.search(r'^\s+- "127\.0\.0\.1:8061:8061"$', compose, flags=re.M), "the API is published on this machine's loopback address only"
    assert "env_file:" in compose and ".env.production" in compose
    assert re.search(r"^\s+- gridlytics-data:/data$", compose, flags=re.M)
    # the proxy's connections reach the API from the network's gateway, which FORWARDED_ALLOW_IPS names: a fixed one
    gateway = re.search(r"gateway: (\d+\.\d+\.\d+\.\d+)", compose)
    assert gateway and _example()["FORWARDED_ALLOW_IPS"] == (True, gateway.group(1))
    # the later services are written, commented out
    services = re.findall(r"^  ([a-z]+):$", compose.split("\nservices:", 1)[1].split("\nvolumes:", 1)[0], flags=re.M)
    assert services == ["api"], services
    for later in ("# mosquitto:", "# valkey:", "# worker:"):
        assert later in compose, later


# ------------------------------------------------------------------------------------------------ the image's command, run here

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server_env(tmp: Path, **extra: str) -> dict:
    """The environment of a server the test starts: none of the tests' own GRIDLYTICS_ settings (conftest), a temporary data folder, no log, no
    scheduler, no daily copies."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRIDLYTICS_") and k != "FORWARDED_ALLOW_IPS"}
    env.update(GRIDLYTICS_DATA_DIR=str(tmp), GRIDLYTICS_DB=str(tmp / "gridlytics.db"), GRIDLYTICS_LOGS="0", **extra)
    return env


def _start(tmp: Path, port: int, env: dict) -> subprocess.Popen:
    args = [a.replace("$GRIDLYTICS_PORT", str(port)) for a in _command()[1:]]
    args[args.index("--host") + 1] = "127.0.0.1"                 # 0.0.0.0 in the container; this machine only here
    return subprocess.Popen([sys.executable, "-m", "uvicorn", *args], cwd=ROOT / "backend", env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)


def test_the_images_command_answers_health_with_no_server_header(tmp_path):
    port = _free_port()
    server = _start(tmp_path, port, _server_env(tmp_path, GRIDLYTICS_HOSTS="api.example.test",
                                                 GRIDLYTICS_LOCAL_JWT_SECRET="made-up-local-signing-key-of-the-deploy-test-01"))
    try:
        for _ in range(120):
            assert server.poll() is None, server.stdout.read()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    status, headers = r.status, {k.lower(): v for k, v in r.getheaders()}
                break
            except OSError:
                time.sleep(0.25)
        else:
            pytest.fail("the API did not answer")
        assert status == 200 and "server" not in headers, headers
        assert headers["x-content-type-options"] == "nosniff"
    finally:
        server.terminate()
        server.wait(timeout=15)


def test_a_hosted_start_without_the_signing_key_stops_and_names_it(tmp_path):
    server = _start(tmp_path, _free_port(), _server_env(tmp_path, GRIDLYTICS_HOSTS="api.example.test"))
    try:
        out, _ = server.communicate(timeout=60)
    finally:
        server.kill()
    assert server.returncode != 0
    assert "needs GRIDLYTICS_LOCAL_JWT_SECRET" in out
