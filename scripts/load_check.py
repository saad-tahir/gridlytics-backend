#!/usr/bin/env python3
"""A load and concurrency check of the API (plan DEP-02; round 7, audit parity-architecture-16; Solarlytics' scripts/load_check.py at c853130, written
again for Gridlytics).

    python scripts/load_check.py [--threads 8] [--seconds 60] [--port 8341] [--demo-days 2]

It measures a server of its own, never one already running: it refuses the owner's ports (8051 and 5195, the release's 8061, and the family's other
products' 8041, 5192, 8060 and 5193) and any port that already answers. It seeds a throwaway data folder (a temporary folder, never the owner's
`data/`) with the demo accounts and, unless `--demo-days 0`, the four demo sites with that many days of history (a few minutes: the history is planned),
then starts the API alone on it (uvicorn, one process, as the hosted image runs it, with the rate limits off: the check measures the server, not the
limiter, and without the scheduler, so the load meets only the requests), and signs in as the demo company's owner. The throwaway password signs in
nowhere else, so a server that is not the one started is refused before anything is asked of it.

It then sends, from `--threads` threads at once for `--seconds` seconds, a mix of what the pages ask: the company's pages (meta, the fleet, the site
list, the alerts, the tariffs) and, with the demo sites, one site's Overview, Plan, Savings and Telemetry. It reports, for each request, how many were
sent, how many failed (anything but a 200) and the time to answer (median, 95th percentile and the slowest), and the requests answered a second in all.
It ends with exit status 1 when any request failed, and removes its throwaway folder. Run it on an otherwise idle machine: the numbers are the
machine's as much as the server's.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: The owner's API and web ports and the release's (plan Part 2.7), and the family's other products' (Solarlytics 8041, 5192 and 8060; Retailytics 5193)
OWNER_PORTS = (8051, 5195, 8061, 8041, 5192, 8060, 5193)
OWNER = "owner@onyxleaf.demo"


def _port_in_use(port: int) -> bool:
    """Whether something already accepts connections on the port, on either loopback address."""
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return True
        except OSError:
            pass
    return False


def _send(base: str, method: str, path: str, body=None, token: str | None = None) -> tuple[int, float, str]:
    headers = {"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {}),
               **({"Authorization": f"Bearer {token}"} if token else {})}
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode("utf-8"), headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read().decode("utf-8", "replace")
            return r.status, time.perf_counter() - t0, text
    except urllib.error.HTTPError as e:
        return e.code, time.perf_counter() - t0, e.read()[:300].decode("utf-8", "replace")
    except Exception as e:                           # a refused or dropped connection counts as a failure
        return 0, time.perf_counter() - t0, str(e)[:300]


def _password(accounts: Path, email: str) -> str:
    """The password the seed gave `email`: the cell after the email in the accounts list (never printed)."""
    for line in accounts.read_text(encoding="utf-8").splitlines():
        if f"`{email}`" in line:
            cells = [c.strip().strip("`") for c in line.split("|")]
            return cells[cells.index(email) + 1]
    raise SystemExit(f"{email} is not in the seed's list {accounts}")


def _requests(base: str, token: str, sites: bool) -> dict[str, tuple[str, str]]:
    """What the pages ask: the company's pages, and one site's when there are sites."""
    asks = {"meta": ("GET", "/api/meta"), "fleet": ("GET", "/api/fleet"), "sites": ("GET", "/api/sites"), "alerts": ("GET", "/api/alerts"),
            "tariffs": ("GET", "/api/tariffs")}
    if sites:
        status, _, text = _send(base, "GET", "/api/sites", token=token)
        listed = json.loads(text) if status == 200 else []
        listed = listed.get("sites", []) if isinstance(listed, dict) else listed
        if not listed:
            raise SystemExit("The seeded company has no site to ask about")
        sid = listed[0]["id"]
        asks |= {"site now": ("GET", f"/api/sites/{sid}/now"), "site plan": ("GET", f"/api/sites/{sid}/plan"),
                 "site savings": ("GET", f"/api/sites/{sid}/savings"), "site telemetry": ("GET", f"/api/sites/{sid}/telemetry")}
    return asks


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--port", type=int, default=8341)
    ap.add_argument("--demo-days", type=int, default=2, help="days of history of the four demo sites; 0 seeds the accounts only (the company's pages)")
    args = ap.parse_args(argv)
    if args.port in OWNER_PORTS:
        raise SystemExit(f"Port {args.port} is the owner's; use a spare one")
    if _port_in_use(args.port):
        raise SystemExit(f"Port {args.port} is already in use; the check starts its own server, so give it a spare port")
    data = Path(tempfile.mkdtemp(prefix="gridlytics-load-"))
    if data.resolve() == (ROOT / "data").resolve():
        raise SystemExit("The check must not use the owner's data folder")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GRIDLYTICS_") and k != "FORWARDED_ALLOW_IPS"}
    env.update(GRIDLYTICS_DATA_DIR=str(data), GRIDLYTICS_DB=str(data / "gridlytics.db"), GRIDLYTICS_LOGS="0", GRIDLYTICS_AUTH_MODE="local",
               GRIDLYTICS_RATE_USER="0", GRIDLYTICS_RATE_ADDRESS="0", GRIDLYTICS_RATE_PUBLIC="0", GRIDLYTICS_RATE_SIGN_IN="0")
    accounts = data / "DEMO_ACCOUNTS.local.md"
    print(f"Seeding a throwaway data folder ({data})" + (f" with the demo sites, {args.demo_days} days: a few minutes" if args.demo_days else ""))
    seed = [sys.executable, "-m", "gridlytics.seed_demo", "--out", str(accounts)]
    seed += ["--demo-days", str(args.demo_days)] if args.demo_days > 0 else ["--no-demo-sites"]
    done = subprocess.run(seed, cwd=ROOT / "backend", env=env, capture_output=True, text=True)
    if done.returncode != 0:
        shutil.rmtree(data, ignore_errors=True)
        raise SystemExit(f"The seed stopped (exit code {done.returncode}): {done.stderr[-2000:]}")
    password = _password(accounts, OWNER)
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "gridlytics.api:app", "--host", "127.0.0.1", "--port", str(args.port), "--log-level",
                               "warning", "--no-proxy-headers", "--no-server-header"], cwd=ROOT / "backend", env=env)
    base = f"http://127.0.0.1:{args.port}"
    try:
        for _ in range(240):
            # a server that has stopped (it could not take the port, or failed to start) is not waited for
            if server.poll() is not None:
                raise SystemExit(f"The API stopped before it answered (exit code {server.returncode})")
            if _send(base, "GET", "/api/health")[0] == 200:
                break
            time.sleep(0.5)
        else:
            raise SystemExit("The API did not start")
        status, _, text = _send(base, "POST", "/api/local/session", {"email": OWNER, "password": password})
        if server.poll() is not None or status != 200:
            raise SystemExit(f"The server on port {args.port} is not the one the check started ({status}); give the check a spare port")
        token = json.loads(text)["access_token"]
        asks = _requests(base, token, args.demo_days > 0)
        names = list(asks)
        seen: dict[str, list[float]] = {n: [] for n in names}
        failed: dict[str, list[str]] = {n: [] for n in names}
        lock = threading.Lock()
        stop = time.perf_counter() + args.seconds

        def worker(k: int) -> None:
            i = k
            while time.perf_counter() < stop:
                name = names[i % len(names)]
                method, path = asks[name]
                status, dt, text = _send(base, method, path, token=token)
                with lock:
                    seen[name].append(dt)
                    if status != 200:
                        failed[name].append(f"{status} {text[:200]}")
                i += 1

        t0 = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(k,)) for k in range(args.threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t0
        total = sum(len(v) for v in seen.values())
        print(f"{args.threads} threads for {wall:.0f} s: {total} requests, {total / wall:.1f} a second, {sum(len(v) for v in failed.values())} failed")
        for n in names:
            v = sorted(seen[n])
            if not v:
                continue
            p95 = v[min(len(v) - 1, int(0.95 * len(v)))]
            print(f"  {n:15s} {len(v):5d} sent, {len(failed[n]):3d} failed; median {statistics.median(v) * 1000:7.0f} ms, 95% {p95 * 1000:7.0f} ms, "
                  f"slowest {v[-1] * 1000:7.0f} ms")
            for f in failed[n][:3]:
                print(f"      {f}")
        return 1 if any(failed.values()) else 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        # the throwaway folder goes with the server that used it
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
