"""api.py — a tiny client for the scratch Gridlytics API (port 8151) for audit scripts.

    from api import client
    c = client("owner")            # owner | operator | analyst | viewer
    print(c.get("/api/fleet").json())

Reads only the password cell for the role from DEMO_ACCOUNTS.local.md; never prints it.
Run with the backend venv: /home/user/saad-tahir/gridlytics-backend/.scratch/venv/bin/python -I -c 'import sys; sys.path.insert(0, "<this dir>"); ...'
"""
import json
import re
import sys

import httpx

ACCOUNTS = "/home/user/saad-tahir/gridlytics-backend/.scratch/data/DEMO_ACCOUNTS.local.md"
BASE = "http://127.0.0.1:8151"


def password_for(email: str) -> str:
    for line in open(ACCOUNTS, encoding="utf8"):
        if f"`{email}`" in line:
            cells = [c.strip() for c in line.split("|")]
            i = cells.index(f"`{email}`")
            return cells[i + 1].strip("`")
    raise KeyError(email)


def client(role: str = "owner", base: str = BASE) -> httpx.Client:
    email = f"{role}@onyxleaf.demo"
    c = httpx.Client(base_url=base, timeout=60)
    r = c.post("/api/local/session", json={"email": email, "password": password_for(email)})
    r.raise_for_status()
    tok = r.json()["access_token"]
    c.headers["Authorization"] = f"Bearer {tok}"
    return c


if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) > 1 else "owner"
    path = sys.argv[2] if len(sys.argv) > 2 else "/api/fleet"
    c = client(role)
    r = c.get(path)
    print(r.status_code)
    try:
        print(json.dumps(r.json(), indent=1)[:6000])
    except Exception:
        print(r.text[:2000])
