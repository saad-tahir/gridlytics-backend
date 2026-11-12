"""Round 7 (audit), work package deploy-ci: CI, the release and load scripts, and the pins (plan Part 10.1, 10.7 and 10.9; DEP-01, DEP-02, DEP-04).

- parity-architecture-01: neither repository had a CI workflow, so pull requests into v1-build were merged with no automated test, audit or image build.
  `.github/workflows/ci.yml` is the family's shape (solarlytics-backend at c853130): the backend tests on Linux with Python 3.12 and `pytest -n auto` on
  every push and pull request, the web repository checked out beside with the read-only FRONTEND_READ_TOKEN, the tests on Windows, pip-audit, the licence
  gate, and the solver benchmarks and the Docker image on a push of v1-build. Without the token (before the owner adds it, or on a fork's pull request)
  the run warns and the tests that read the web repository skip, each saying why, instead of failing the checkout (follow-up frontend-read-token).

The workflow is read as text (the tests' Python has no YAML parser): each job is the block under its name."""
from __future__ import annotations

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
