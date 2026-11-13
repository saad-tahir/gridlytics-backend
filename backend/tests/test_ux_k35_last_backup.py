"""UX backend ask K35 (plan H11; G9 item 4; step 14, the founder's Overview and company drawer: "No copy since 8 Nov").

Each company of `GET /api/founder/companies` (and each company answer of the console) carries `last_backup`: when its newest copy was made, in Karachi
time, or null with none; the backup folder is listed once for every company. The company's administrator reads the same for their own company in
`GET /api/platform/org` (manage_users only: no other role, no key)."""
from __future__ import annotations

import os
from datetime import datetime

from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_founder import _company, _console, _ok, clock, fdb  # noqa: F401  (clock and fdb are fixtures)

from gridlytics import backups, sites, store
from gridlytics import platform as platformdb


def _rows(c) -> dict:
    return {x["name"]: x for x in _ok(c.get("/api/founder/companies"))["companies"]}


def test_each_company_says_when_its_newest_copy_was_made_in_karachi_time(fdb, clock):
    c = _console()
    mine = _company(c)["company"]
    other = _company(c, "Development", {"email": "dev@localhost", "name": "Local Tester", "password": "dev first password 1", "one_time": False})["company"]
    assert {n: r["last_backup"] for n, r in _rows(c).items()} == {"Boxlytics Internal": None, "Development": None}
    with store.use_company(platformdb.company_file(mine["id"])):                  # a file to copy
        sites.create({"name": "Mill", "disco": "FESCO"}, "test")
    first = _ok(c.post(f"/api/founder/companies/{mine['id']}/backups", json={}))
    second = _ok(c.post(f"/api/founder/companies/{mine['id']}/backups", json={}))
    folder = platformdb.company_file(mine["id"]).parent / "backup"
    os.utime(folder / first["made"], (1_762_820_000, 1_762_820_000))             # 11 Nov 2025, 05:13:20 in Karachi
    os.utime(folder / second["made"], (1_762_833_000, 1_762_833_000))            # 11 Nov 2025, 08:50:00 in Karachi
    rows = _rows(c)
    assert rows["Boxlytics Internal"]["last_backup"] == "2025-11-11T08:50:00+05:00"
    assert rows["Development"]["last_backup"] is None                              # another company's copies are not its own
    listed = _ok(c.get(f"/api/founder/companies/{mine['id']}/backups"))["backups"]
    assert datetime.fromisoformat(listed[0]["modified"]) == datetime.fromisoformat(rows["Boxlytics Internal"]["last_backup"])
    # an answer of the console that is the company carries it too
    renamed = _ok(c.patch(f"/api/founder/companies/{mine['id']}", json={"seats": 6}))["company"]
    assert renamed["last_backup"] == "2025-11-11T08:50:00+05:00"
    assert _ok(c.patch(f"/api/founder/companies/{other['id']}", json={"seats": 6}))["company"]["last_backup"] is None


def test_the_backup_folder_is_listed_once_for_every_company(fdb, clock, monkeypatch):
    c = _console()
    for i in range(3):
        _company(c, f"Company {i}", {"email": f"o{i}@co.test", "name": f"Owner {i}", "password": "a first password 1", "one_time": False})
    for row in platformdb.list_companies():
        with store.use_company(platformdb.company_file(row["id"])):
            sites.create({"name": "Mill", "disco": "FESCO"}, "test")
            store.backup("daily")
    listed = []
    real = backups.Path.iterdir

    def spy(self):
        listed.append(self)
        return real(self)
    monkeypatch.setattr(backups.Path, "iterdir", spy)
    rows = _ok(c.get("/api/founder/companies"))["companies"]
    assert all(r["last_backup"] and r["last_backup"].endswith("+05:00") for r in rows), rows
    assert len([p for p in listed if p.name == "backup"]) == 1, listed


def test_the_administrator_reads_the_company_s_last_backup_and_no_other_role_does(world):
    admin = world.c["admin"].get("/api/platform/org").json()
    assert admin["last_backup"] is None
    with world.use():
        made = store.backup("manual")
    os.utime(made, (1_762_833_000, 1_762_833_000))
    assert world.c["admin"].get("/api/platform/org").json()["last_backup"] == "2025-11-11T08:50:00+05:00"
    for role in ("operator", "analyst", "viewer"):
        got = world.c[role].get("/api/platform/org")
        assert got.status_code == 200 and "last_backup" not in got.json(), (role, got.json())
    keyed = world.anon.get("/api/platform/org", headers=world.key(["sites:read"]))
    assert keyed.status_code != 200 or "last_backup" not in keyed.json(), keyed.text
