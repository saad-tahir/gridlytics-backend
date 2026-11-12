"""The code lifted from Solarlytics, held to its upstream (round 7, audit parity-architecture-07).

Sign-in, tenancy and the founder console are a copy of saad-tahir/solarlytics-backend at the commit THIRD_PARTY_NOTICES records, renamed (plan D3 chose
"lift"). Nothing noticed when either side changed, so a security fix in one product would not reach the other. `backend/lift_manifest.json` lists every
lifted file: its upstream path, the rename map, whether it is still identical after the rename or adapted (and why), the upstream file's hash after the
rename and this file's hash as last reviewed against it.

- Always (CI too): a lifted file that changes fails here until the change is recorded in the manifest, so a divergence is a decision someone wrote down,
  and the question "does Solarlytics need this too?" is asked when it is made. A file recorded identical must still hash to its renamed upstream.
- With a solarlytics-backend checkout beside this repository (or GRIDLYTICS_SOLARLYTICS_DIR), read only: the upstream at the recorded commit must hash as
  recorded, and the checkout's own file must still be that one, so an upstream fix since the lift is noticed here. Skipped without one.

Recording a reviewed change: put the new hash the failure names into the file's `sha256` (and, for an upstream change ported or judged not needed, the
new upstream hash into `upstream_sha256` and its commit into `reviewed_upstream`), with the reason under `why`. Shared packages instead of the copy
(onyxleaf-platform, @onyxleaf/auth) are an owner decision (the web repository's docs/BACKEND_ASKS.md).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "backend" / "lift_manifest.json"

#: The modules docs/DESIGN_LOG.md ("The lift (Part 10.4)") names as lifted, and ratelimit.py (Solarlytics' Limiter)
LIFTED_MODULES = {"identity", "platform", "companies", "routes_local", "founder", "routes_founder", "store_founder", "store_members", "seed_demo", "backups",
                  "mfa", "auth", "apikeys", "version", "jobs", "ratelimit"}


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _text(raw: bytes) -> str:
    return raw.decode("utf-8").replace("\r\n", "\n")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def renamed(text: str, entry: dict, manifest: dict) -> str:
    """The upstream text with Solarlytics' names turned into Gridlytics' (the manifest's map, then the file's own extra pairs)."""
    for old, new in [*manifest["rename"], *entry.get("rename", [])]:
        text = text.replace(old, new)
    return text


def test_the_manifest_lists_every_lifted_file_with_its_upstream_and_a_reason_for_each_difference():
    manifest = _manifest()
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    # the commit the notices record is the one the manifest holds the files to
    assert f"`{manifest['upstream']['repository']}` at `{manifest['upstream']['commit']}`" in notices
    paths = [e["path"] for e in manifest["files"]]
    assert len(paths) == len(set(paths)), "a file is listed twice"
    modules = {Path(p).stem for p in paths if p.startswith("backend/gridlytics/")}
    assert LIFTED_MODULES <= modules, f"lifted but not in the manifest: {sorted(LIFTED_MODULES - modules)}"
    for e in manifest["files"]:
        assert (ROOT / e["path"]).is_file(), f"{e['path']} is listed but not there"
        assert e["status"] in ("identical", "adapted", "extract"), e["path"]
        assert e["upstream"] and re.fullmatch(r"[0-9a-f]{64}", e["sha256"]) and re.fullmatch(r"[0-9a-f]{64}", e["upstream_sha256"]), e["path"]
        if e["status"] == "identical":
            assert e["sha256"] == e["upstream_sha256"], f"{e['path']} is recorded identical but its hashes differ"
        else:
            assert len(e.get("why", "").strip()) > 20, f"{e['path']} differs from its upstream with no reason recorded"


def test_each_lifted_file_is_as_last_reviewed_against_its_upstream():
    manifest = _manifest()
    changed = []
    for e in manifest["files"]:
        now = _sha(_text((ROOT / e["path"]).read_bytes()))
        if now != e["sha256"]:
            changed.append(f"{e['path']} (sha256 now {now})")
    assert not changed, (
        "Lifted from solarlytics-backend and changed since it was last reviewed against it: " + "; ".join(changed) + ". If the change is a fix, check "
        "whether Solarlytics needs it too (a security fix must reach both products); then record it in backend/lift_manifest.json: the new sha256, and "
        "under why what now differs (a file recorded identical becomes adapted).")


def _sibling() -> Path | None:
    named = os.environ.get("GRIDLYTICS_SOLARLYTICS_DIR", "").strip()
    other = Path(named) if named else ROOT.parent / "solarlytics-backend"
    return other if (other / "backend" / "solarlytics").is_dir() else None


def _at_commit(repo: Path, commit: str, path: str) -> str | None:
    if shutil.which("git") is None or not (repo / ".git").exists():
        return None
    got = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"], capture_output=True)
    return _text(got.stdout) if got.returncode == 0 else None


def test_the_upstream_has_not_moved_since_the_lift_when_a_solarlytics_checkout_is_beside():
    other = _sibling()
    if other is None:
        pytest.skip(f"no solarlytics-backend checkout at {ROOT.parent / 'solarlytics-backend'} or GRIDLYTICS_SOLARLYTICS_DIR: the files are held to the "
                    "hashes recorded in the manifest only")
    manifest = _manifest()
    commit = manifest["upstream"]["commit"]
    moved, wrong = [], []
    for e in manifest["files"]:
        reviewed = e.get("reviewed_upstream", commit)
        then = _at_commit(other, reviewed, e["upstream"])
        if then is not None and _sha(renamed(then, e, manifest)) != e["upstream_sha256"]:
            wrong.append(f"{e['path']} (upstream {e['upstream']} at {reviewed})")
        now_path = other / e["upstream"]
        now = _sha(renamed(_text(now_path.read_bytes()), e, manifest)) if now_path.is_file() else None
        if now != e["upstream_sha256"]:
            moved.append(f"{e['upstream']} -> {e['path']}" + ("" if now else " (gone upstream)"))
    assert not wrong, "the manifest's upstream hashes do not match the recorded commit: " + "; ".join(wrong)
    assert not moved, (
        f"solarlytics-backend changed lifted files since {commit}: " + "; ".join(moved) + f". Read `git -C {other} log {commit}..HEAD -- <file>`, port "
        "what Gridlytics needs (a security fix always), and record the upstream hash and commit you reviewed in backend/lift_manifest.json.")


def test_a_file_recorded_identical_is_its_upstream_line_for_line_when_a_solarlytics_checkout_is_beside():
    other = _sibling()
    if other is None:
        pytest.skip("no solarlytics-backend checkout beside this repository")
    manifest = _manifest()
    for e in manifest["files"]:
        if e["status"] != "identical":
            continue
        upstream = _at_commit(other, e.get("reviewed_upstream", manifest["upstream"]["commit"]), e["upstream"])
        if upstream is None:
            pytest.skip(f"{other} is not a git checkout")
        assert renamed(upstream, e, manifest) == _text((ROOT / e["path"]).read_bytes()), e["path"]
