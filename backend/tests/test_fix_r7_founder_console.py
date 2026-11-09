"""Round 7 (the end-to-end audit), the founder console: a short session secret is refused. One test per audit finding; the finding's id is in each
test's docstring."""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gridlytics import founder
from test_founder import (PASSWORD, SESSION_SECRET, _client, clock, fdb)  # noqa: F401  (fixtures)

REPO = Path(__file__).resolve().parents[2]


def _headers(token: str) -> dict:
    return {"x-founder-session": token}


# ================================================================== security-roles-12: the session secret's length

def test_a_short_founder_session_secret_is_refused_by_the_console(fdb, monkeypatch):
    """security-roles-12: a 3-character GRIDLYTICS_FOUNDER_SESSION_SECRET was taken, and one token let it be guessed offline."""
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", "abc")
    assert "GRIDLYTICS_FOUNDER_SESSION_SECRET" in founder.malformed()
    for method, path in (("POST", "/api/founder/login"), ("GET", "/api/founder/session")):
        r = _client().request(method, path, json={"username": "founder", "password": PASSWORD} if method == "POST" else None,
                              headers=_headers("x.y"))
        assert r.status_code == 503 and r.json()["code"] == "unavailable", r.text
        assert "GRIDLYTICS_FOUNDER_SESSION_SECRET" in r.json()["detail"] and "32 characters" in r.json()["detail"] and "abc" not in r.text
    with pytest.raises(founder.Refused):
        founder.new_token()
    with pytest.raises(RuntimeError, match="GRIDLYTICS_FOUNDER_SESSION_SECRET is shorter than 32 characters"):
        founder.check_startup()
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", " " * 40)          # not set: the console is off (503), the API starts
    founder.check_startup()
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", "x" * 32)
    founder.check_startup()
    assert founder.malformed() == []


def test_the_founders_command_line_makes_a_session_secret_long_enough(fdb, monkeypatch):
    """security-roles-12: the refusal names `python -m gridlytics.founder session-secret`, which makes a secret the console takes."""
    out, err = io.StringIO(), io.StringIO()
    assert founder.main(["session-secret"], stdout=out, stderr=err) == 0 and err.getvalue() == ""
    secret = out.getvalue().strip()
    assert len(secret) >= founder.SESSION_SECRET_CHARS and secret != SESSION_SECRET
    monkeypatch.setenv("GRIDLYTICS_FOUNDER_SESSION_SECRET", secret)
    founder.check_startup()
    assert founder.malformed() == []


def _start_api(tmp_path: Path, secret: str | None) -> subprocess.CompletedProcess:
    env = {**os.environ, "GRIDLYTICS_DB": str(tmp_path / "start.db"), "GRIDLYTICS_LOGS": "0", "PYTHONPATH": str(REPO / "backend")}
    env.pop("GRIDLYTICS_FOUNDER_SESSION_SECRET", None)
    if secret is not None:
        env["GRIDLYTICS_FOUNDER_SESSION_SECRET"] = secret
    return subprocess.run([sys.executable, "-c", "import gridlytics.api"], cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180)


def test_the_api_does_not_start_with_a_short_founder_session_secret(tmp_path):
    """security-roles-12: the API started with a short session secret; it is refused at start-up, as a short GRIDLYTICS_LOCAL_JWT_SECRET is."""
    refused = _start_api(tmp_path, "abc")
    assert refused.returncode != 0 and "GRIDLYTICS_FOUNDER_SESSION_SECRET is shorter than 32 characters" in refused.stderr
    assert "abc" not in refused.stderr.replace("GRIDLYTICS", "")
    started = _start_api(tmp_path, None)                                       # no console: the API starts, the console answers 503
    assert started.returncode == 0, started.stderr[-2000:]
