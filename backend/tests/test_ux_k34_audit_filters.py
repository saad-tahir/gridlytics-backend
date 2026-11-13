"""UX backend ask K34 (plan H11; G9 item 3, G2.4, G4.5; step 14, Company settings › Audit trail and the founder's Audit; owner's question Q79).

The company's trail is filtered by `GET /api/audit/search` (who, the kind of change, the site, From and To in Karachi days, a search) and exported by
`GET /api/audit/export.csv` with the same filters; the founder's by `GET /api/founder/audit` (the kind of change, the company, From and To, a search).
Each answers `{entries, total, ...}` with its groups, newest first, 300 lines a page, every time in Karachi time. The groups are one server-side map
from an action's words to its group (`audit_trail`), and every `store.audit(...)` and `founder_audit(...)` call of the backend writes an action of a
group. Today's `GET /api/audit` (a list) is left as today's page reads it. A line of the founder's audit keeps its company (platform.db schema 5),
filled for the lines before from the company's name in them."""
from __future__ import annotations

import ast
import csv
import io
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import identities
import pytest
from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_founder import _company, _console, _ok, clock, fdb  # noqa: F401  (clock and fdb are fixtures)

from gridlytics import access_requests, audit_trail, reports, sites, store, verify
from gridlytics import platform as platformdb

ROOT = Path(__file__).resolve().parents[1]
SEARCH = "/api/audit/search"
#: Offsets a page refuses: past what SQLite holds, more than nine digits, and digits or forms int() reads that are not ASCII digits
BAD_OFFSETS = ("99999999999999999999", "1000000000", "\u0663", "\u0661\u0662", "1_0", "+3", " 5 ", "\u00b2")


# ================================================================== the groups: every action the backend writes is in one

def _sources() -> list[Path]:
    return sorted((ROOT / "gridlytics").glob("*.py")) + sorted((ROOT.parent / "scripts").glob("*.py"))


def _patterns(node, module: str) -> list[str]:
    """The action an audit call writes, as regular expressions: a text as itself, an f-string with each part it fills as `.+`, both sides of a choice,
    a module's constant by its value, and the report's words (reports.made_words) for every kind."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [re.escape(node.value)]
    if isinstance(node, ast.JoinedStr):
        return ["".join(re.escape(v.value) if isinstance(v, ast.Constant) else ".+" for v in node.values)]
    if isinstance(node, ast.IfExp):
        return _patterns(node.body, module) + _patterns(node.orelse, module)
    if isinstance(node, ast.Name):
        import importlib
        return [re.escape(getattr(importlib.import_module(f"gridlytics.{module}"), node.id))]
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        import importlib
        return [re.escape(getattr(importlib.import_module(f"gridlytics.{node.value.id}"), node.attr))]
    if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "made_words":
        return [re.escape(reports.made_words(k)) for k in [*reports.MADE_WORDS, "unknown"]]
    raise AssertionError(f"{module}: teach the test this action's form: {ast.dump(node)[:200]}")


def _calls() -> dict[str, list[tuple[str, int, str]]]:
    """Every audit call of the backend and its scripts: {"company": [(module, line, pattern)], "founder": [...]}."""
    out = {"company": [], "founder": []}
    for path in _sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            owner = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            if name == "audit" and owner == "store" and len(node.args) >= 2:
                out["company"] += [(path.stem, node.lineno, p) for p in _patterns(node.args[1], path.stem)]
            elif name == "founder_audit" and node.args:
                out["founder"] += [(path.stem, node.lineno, p) for p in _patterns(node.args[0], path.stem)]
    return out


def _covered(pattern: str, exact: dict, group_of) -> bool:
    """An action is in a group: its words are a key of the map, or (a part filled in) a key matches it or the group of its words with the part filled
    is known (a prefix of the map)."""
    if ".+" not in pattern:
        return group_of(re.sub(r"\\(.)", r"\1", pattern)) is not None
    rx = re.compile(pattern + r"\Z")
    return any(rx.match(k) for k in exact) or group_of(pattern.replace(".+", "x").replace("\\", "")) is not None


def test_every_action_the_backend_writes_belongs_to_a_group():
    calls = _calls()
    assert len(calls["company"]) >= 55 and len(calls["founder"]) >= 20, {k: len(v) for k, v in calls.items()}     # the scan found the calls
    missing = [c for c in calls["company"] if not _covered(c[2], audit_trail.COMPANY_GROUPS, audit_trail.company_group)]
    assert missing == [], f"company trail actions in no group (audit_trail.COMPANY_GROUPS): {missing}"
    missing = [c for c in calls["founder"] if not _covered(c[2], audit_trail.FOUNDER_GROUPS, audit_trail.founder_group)]
    assert missing == [], f"founder trail actions in no group (audit_trail.FOUNDER_GROUPS): {missing}"
    # the actions written with a part that varies, by the values they take
    for action in ("acknowledged an alert", "closed an alert", "assigned an alert", "saved the battery", "removed the genset",
                   "answered a genset request: started", "downloaded a report (pdf)", verify.RECOMPUTED, verify.WALKED, *map(reports.made_words, reports.MADE_WORDS)):
        assert audit_trail.company_group(action), action
    assert audit_trail.company_group("removed the demo sites") == "demo" and audit_trail.company_group("removed the battery") == "sites"
    assert audit_trail.founder_group(access_requests.CHANGED) == "requests"
    for action in ("switched Gridlytics on", "switched Gridlytics off"):
        assert audit_trail.founder_group(action) == "companies"
    # every group is the select's, in its order and words (plan G2.4, G4.5)
    assert list(audit_trail.COMPANY_GROUP_WORDS.values()) == ["People and keys", "Sites and set-up", "Plans and control", "Tariffs and money",
                                                               "Savings and pilot", "Reports and exports", "Demo"]
    assert list(audit_trail.FOUNDER_GROUP_WORDS.values()) == ["Sign-ins", "Refused sign-ins", "Companies", "Accounts and memberships", "Backups",
                                                               "Access requests"]
    assert set(audit_trail.COMPANY_GROUPS.values()) == set(audit_trail.COMPANY_GROUP_WORDS)
    assert set(audit_trail.FOUNDER_GROUPS.values()) == set(audit_trail.FOUNDER_GROUP_WORDS)


# ================================================================== the company's trail

def _line(w, action, target="", detail=None, actor="Ayesha Khan", actor_id="u-ayesha", email="ayesha@fix.test", at=None):
    with w.use():
        token = store.set_actor(actor_id, email)
        try:
            store.audit(actor, action, target, detail or {})
        finally:
            store.reset_actor(token)
        if at:
            with store.connect() as con:
                con.execute("UPDATE audit SET at = ? WHERE id = (SELECT MAX(id) FROM audit)", (at,))


def _search(w, role="admin", **params):
    r = w.c[role].get(SEARCH, params={k.rstrip("_"): v for k, v in params.items()})
    assert r.status_code == 200, (params, r.text)
    return r.json()


def test_the_filtered_trail_answers_a_page_with_its_total_groups_and_people_in_karachi_time(world):
    sid = world.site
    _line(world, "changed a user", "sana@fix.test", {"role": "analyst"}, at="2026-11-10T18:59:59+00:00")     # 11 Nov, 23:59:59 in Karachi
    _line(world, "marked a plan as applied", sid, {"plan_id": "p1"}, actor="Bilal Ahmed", actor_id="u-bilal", email="bilal@fix.test",
          at="2026-11-10T19:00:00+00:00")                                                                     # 12 Nov, 00:00 in Karachi
    _line(world, "loaded the demo sites", "", {"state": "ready"}, actor="demo, for Ayesha Khan")
    every = _search(world)
    with world.use():
        assert every["total"] == len(store.list_audit(limit=10_000)) == len(every["entries"])
    assert set(every) == {"entries", "total", "offset", "limit", "groups", "people"} and (every["offset"], every["limit"]) == (0, 300)
    assert [g["key"] for g in every["groups"]] == list(audit_trail.COMPANY_GROUP_WORDS) and every["groups"][0]["words"] == "People and keys"
    newest = every["entries"][0]
    assert newest["action"] == "loaded the demo sites" and newest["group"] == "demo" and newest["who"] == "demo"
    assert all(e["at"].endswith("+05:00") for e in every["entries"])
    plan = next(e for e in every["entries"] if e["action"] == "marked a plan as applied")
    assert plan["at"] == "2026-11-11T00:00:00+05:00" and plan["site_id"] == sid and plan["site_name"] == "Mill One" and plan["group"] == "plans"
    assert plan["who"] == "u-bilal" and plan["actor_email"] == "bilal@fix.test"
    people = {p["who"]: p for p in every["people"]}
    assert people["u-ayesha"] == {"who": "u-ayesha", "name": "Ayesha Khan", "email": "ayesha@fix.test", "person": True}
    assert people["demo"]["person"] is False and every["people"][0]["person"] is True
    # each filter, and its total
    assert {e["action"] for e in _search(world, group="people")["entries"]} == {"changed a user"}
    assert [e["action"] for e in _search(world, who="u-bilal")["entries"]] == ["marked a plan as applied"]
    assert [e["action"] for e in _search(world, who="demo")["entries"]] == ["loaded the demo sites"]
    on_site = _search(world, site=sid)
    assert on_site["total"] == len(on_site["entries"]) >= 4 and all(e["site_id"] == sid for e in on_site["entries"])
    assert "added a site" in {e["action"] for e in on_site["entries"]}          # its target is the name, its detail the site's id
    day = _search(world, from_="2026-11-11", to="2026-11-11")
    assert [e["action"] for e in day["entries"]] == ["marked a plan as applied"] and day["total"] == 1
    assert [e["action"] for e in _search(world, from_="2026-11-10", to="2026-11-10")["entries"]] == ["changed a user"]
    assert _search(world, to="2026-11-10")["total"] == 1
    # the search reads who, what, of and the detail, and a site's name finds its lines
    assert [e["action"] for e in _search(world, q="BILAL@")["entries"]] == ["marked a plan as applied"]
    assert {e["action"] for e in _search(world, q="analyst")["entries"]} == {"changed a user"}
    assert all(e["site_id"] == sid for e in _search(world, q="mill one")["entries"]) and _search(world, q="mill one")["total"] >= 4
    assert _search(world, q="no line says this")["total"] == 0
    # filters together
    assert _search(world, group="plans", site=sid, from_="2026-11-11", who="u-bilal")["total"] == 1


def test_a_page_is_300_lines_with_the_total_of_every_line_matched(world):
    with world.use():
        for i in range(310):
            store.audit("Ayesha Khan", "set a fuel price", "hsd", {"price": 250 + i})
    first = _search(world, group="tariffs")
    assert first["total"] == 310 and len(first["entries"]) == 300 and first["entries"][0]["detail"]["price"] == 559
    second = _search(world, group="tariffs", offset="300")
    assert second["total"] == 310 and len(second["entries"]) == 10 and second["entries"][-1]["detail"]["price"] == 250
    assert _search(world, group="tariffs", offset="999999999")["entries"] == [] and _search(world, offset="")["offset"] == 0


def test_a_filter_that_cannot_be_read_is_refused_in_a_sentence(world):
    for params, said in (({"group": "money"}, "The kind of change is one of people, sites, plans, tariffs, savings, reports, demo"),
                         ({"from": "2026-13-01"}, "From is a day written as 2026-11-01"), ({"to": "11/11/2026"}, "To is a day written as 2026-11-01"),
                         ({"from": "2026-11-12", "to": "2026-11-11"}, "From must be on or before To"),
                         ({"q": "x" * 201}, "The search is text of at most 200 characters"),
                         ({"offset": "-1"}, "The offset is a whole number from 0"), ({"offset": "two"}, "The offset is a whole number from 0"),
                         # ASCII digits only, at most nine (the founder's lists' rule, UX K2 check): too large for SQLite was a server error, and
                         # "٣", "1_0", "+3" and " 5 " were read as numbers (UX K34 check)
                         *(({"offset": odd}, "The offset is a whole number from 0") for odd in BAD_OFFSETS)):
        r = world.c["admin"].get(SEARCH, params=params)
        assert r.status_code == 422 and r.json()["detail"] == said, (params, r.text)


def test_only_the_administrator_reads_or_exports_the_trail_and_never_another_company_s(world):
    for role in ("operator", "analyst", "viewer"):
        for url in (SEARCH, "/api/audit/export.csv"):
            r = world.c[role].get(url)
            assert r.status_code == 403 and "Rs" not in r.text, (role, url, r.text)
    assert world.anon.get(SEARCH, headers=world.key(["sites:read"])).status_code == 403
    other = identities.company("Another company", own_file=True)
    with store.use_company(platformdb.company_file(other)):
        theirs = sites.create({"name": "Their Secret Mill", "disco": "LESCO"}, "Their Owner")["id"]
        store.audit("Their Owner", "changed a user", "their@other.test", {"role": "viewer"})
    for params in ({}, {"q": "secret mill"}, {"q": "their"}, {"site": theirs}):
        got = _search(world, **params)
        assert not any("Their" in json.dumps(e) or theirs in json.dumps(e) for e in got["entries"]), params
    assert _search(world, site=theirs)["total"] == 0


def test_the_export_is_the_filtered_trail_in_karachi_time_and_is_audited(world):
    sid = world.site
    _line(world, "marked a plan as applied", sid, {"plan_id": "p1"}, at="2026-11-10T19:00:00+00:00")
    _line(world, "changed a site", "=HYPERLINK(\"x\")", {"site_id": sid, "fields": ["name"]}, at="2026-11-11T09:00:00+00:00")
    r = world.c["admin"].get("/api/audit/export.csv", params={"site": sid, "from": "2026-11-11", "to": "2026-11-11"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv"), r.text
    assert 'filename="audit-trail-' in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == list(audit_trail.CSV_COLUMNS)
    assert [x[3] for x in rows[1:]] == ["changed a site", "marked a plan as applied"], rows
    changed, applied = rows[1], rows[2]
    assert applied[0] == "2026-11-11T00:00:00+05:00" and applied[4] == "Plans and control" and applied[6] == "Mill One"
    assert changed[5] == "'=HYPERLINK(\"x\")" and json.loads(changed[7]) == {"fields": ["name"], "site_id": sid}
    with world.use():
        line = store.list_audit(limit=1)[0]
    assert line["action"] == "exported the audit trail" and line["detail"] == {"lines": 2, "site": sid, "from": "2026-11-11", "to": "2026-11-11"}, line
    assert world.c["admin"].get("/api/audit/export.csv", params={"group": "nope"}).status_code == 422


def test_today_s_trail_page_reads_the_list_as_before(world):
    r = world.c["admin"].get("/api/audit")
    assert r.status_code == 200 and isinstance(r.json(), list) and {"at", "actor", "action", "target", "detail"} <= set(r.json()[0])
    assert world.c["operator"].get("/api/audit").status_code == 403


# ================================================================== the founder's trail

def test_the_founder_s_trail_is_filtered_by_kind_company_days_and_search_each_line_with_its_company(fdb, clock):
    c = _console()
    first = _company(c)["company"]
    second = _company(c, "Development", {"email": "dev@localhost", "name": "Local Tester", "password": "dev first password 1", "one_time": False})["company"]
    _ok(c.post(f"/api/founder/companies/{first['id']}/access", json={"enabled": False, "reason": "unpaid"}))
    with store.use_company(platformdb.company_file(second["id"])):              # a file to copy
        sites.create({"name": "Mill", "disco": "FESCO"}, "test")
    _ok(c.post(f"/api/founder/companies/{second['id']}/backups", json={}))
    sunia = _ok(c.post("/api/founder/users", json={"email": "sunia@meow.test", "name": "Sunia Ahmed", "password": "sunia first password",
                                                   "company_id": second["id"]}), 201)["user"]
    _ok(c.post(f"/api/founder/users/{sunia['id']}/password", json={"password": "another password 1"}))
    platformdb.founder_audit("sign-in refused", "", {"address": "10.0.0.9"})
    every = _ok(c.get("/api/founder/audit"))
    assert set(every) == {"entries", "total", "offset", "limit", "groups"} and every["total"] == len(every["entries"]) >= 7
    assert [g["key"] for g in every["groups"]] == list(audit_trail.FOUNDER_GROUP_WORDS)
    assert all(e["at"].endswith("+05:00") and e["group"] for e in every["entries"]), every["entries"]
    mine = _ok(c.get("/api/founder/audit", params={"company": first["id"]}))
    assert [e["action"] for e in mine["entries"]] == ["switched Gridlytics off", "made a company"] and mine["total"] == 2
    assert {e["company_name"] for e in mine["entries"]} == {"Boxlytics Internal"}
    dev = [e["action"] for e in _ok(c.get("/api/founder/audit", params={"company": second["id"]}))["entries"]]
    assert dev == ["made an account", "made a backup", "made a company"], dev     # a password set for an account is no company's
    assert [e["action"] for e in _ok(c.get("/api/founder/audit", params={"group": "refused"}))["entries"]] == ["sign-in refused"]
    assert {e["action"] for e in _ok(c.get("/api/founder/audit", params={"group": "accounts"}))["entries"]} == {"made an account", "set a password"}
    assert [e["action"] for e in _ok(c.get("/api/founder/audit", params={"q": "UNPAID"}))["entries"]] == ["switched Gridlytics off"]
    today = every["entries"][0]["at"][:10]
    assert _ok(c.get("/api/founder/audit", params={"from": today, "to": today}))["total"] == every["total"]
    assert _ok(c.get("/api/founder/audit", params={"to": "2020-01-01"}))["total"] == 0
    for params, status, said in (({"group": "people"}, 422, "The kind of change is one of signins, refused, companies, accounts, backups, requests"),
                                 ({"company": "no-such-company"}, 404, "No such company"), ({"from": "yesterday"}, 422, "From is a day written as 2026-11-01"),
                                 ({"offset": "-1"}, 422, "The offset is a whole number from 0"),
                                 *(({"offset": odd}, 422, "The offset is a whole number from 0") for odd in BAD_OFFSETS)):
        r = c.get("/api/founder/audit", params=params)
        assert r.status_code == status and r.json()["detail"] == said, (params, r.text)
    # the founder's session only
    assert _console().get("/api/founder/audit").status_code == 200
    from test_founder import _client
    assert _client().get("/api/founder/audit").status_code == 401


def test_schema_5_keeps_each_founder_line_s_company_and_fills_the_lines_before_from_their_names(fdb, clock):
    """platform.db of schema 4 (lines with no company): copied to backup/ first, then each line's company found by its name, a renamed company's old name
    too; a name two companies share, or none has, leaves the line of no company."""
    c = _console()
    mill = _company(c, "Old Mill Name")["company"]
    _ok(c.patch(f"/api/founder/companies/{mill['id']}", json={"name": "Faisal Mills"}))
    twin_a = _company(c, "Twin", {"email": "a@twin.test", "name": "A Twin", "password": "a first password 1", "one_time": False})["company"]
    _company(c, "Twin Two", {"email": "b@twin.test", "name": "B Twin", "password": "b first password 1", "one_time": False})
    # a name given up in a rename and taken by a later company (UX K34 check: the first company's lines were given to the later one)
    first_dev = _company(c, "Development", {"email": "a@dev.test", "name": "A Dev", "password": "a first password 1", "one_time": False})["company"]
    _ok(c.patch(f"/api/founder/companies/{first_dev['id']}", json={"name": "Renamed Mills"}))
    later_dev = _company(c, "Development", {"email": "b@dev.test", "name": "B Dev", "password": "b first password 1", "one_time": False})["company"]
    path = platformdb.db_path()
    with closing(sqlite3.connect(path)) as con:                                   # back to schema 4, as a server of before holds it
        con.execute("UPDATE companies SET name = 'Twin' WHERE name = 'Twin Two'")
        con.execute("DROP INDEX founder_audit_company")
        con.execute("ALTER TABLE founder_audit DROP COLUMN company_id")
        con.execute("INSERT INTO founder_audit (at, actor, action, target, detail) VALUES ('2026-11-01T00:00:00+00:00', 'founder', 'made a backup', "
                    "'Faisal Mills', '{}')")
        con.execute("INSERT INTO founder_audit (at, actor, action, target, detail) VALUES ('2026-11-01T00:00:00+00:00', 'founder', 'added a membership', "
                    "'x@y.test', '{\"company\": \"Old Mill Name\", \"company_role\": \"member\"}')")
        con.execute("INSERT INTO founder_audit (at, actor, action, target, detail) VALUES ('2026-11-01T00:00:00+00:00', 'founder', 'made a backup', "
                    "'Gone Ltd', '{}')")
        con.execute("PRAGMA user_version = 4")
        con.commit()
    for f in (path.parent / "backup").glob("platform-*.db"):
        f.unlink()
    assert platformdb.company(mill["id"])["name"] == "Faisal Mills"               # the registry opened: migrated
    with closing(sqlite3.connect(path)) as con:
        con.row_factory = sqlite3.Row
        assert con.execute("PRAGMA user_version").fetchone()[0] == platformdb.SCHEMA_VERSION == 5
        lines = [dict(r) for r in con.execute("SELECT action, target, detail, company_id FROM founder_audit ORDER BY id")]
    assert next(x["company_id"] for x in lines if x["action"] == "made a company" and x["target"] == "Old Mill Name") == mill["id"]
    assert next(x["company_id"] for x in lines if x["action"] == "changed a company") == mill["id"]
    assert next(x["company_id"] for x in lines if x["action"] == "made a backup" and x["target"] == "Faisal Mills") == mill["id"]
    assert next(x["company_id"] for x in lines if x["action"] == "added a membership" and x["target"] == "x@y.test") == mill["id"]
    assert next(x["company_id"] for x in lines if x["target"] == "Gone Ltd") == ""
    assert {x["company_id"] for x in lines if x["action"] == "made a company" and x["target"] in ("Twin", "Twin Two")} == {""}   # two named Twin now
    # "Development" was held by two companies in turn: its lines cannot be told apart by name, so neither company is given them; the renamed one
    # keeps its rename by its new name
    assert [x["company_id"] for x in lines if x["action"] == "made a company" and x["target"] == "Development"] == ["", ""], lines
    assert next(x["company_id"] for x in lines if x["action"] == "changed a company" and x["target"] == "Renamed Mills") == first_dev["id"]
    assert later_dev["id"] not in {x["company_id"] for x in lines}
    copies = list((path.parent / "backup").glob("platform-*-schema-v4.db"))
    assert len(copies) == 1
    with closing(sqlite3.connect(copies[0])) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "company_id" not in {r[1] for r in con.execute("PRAGMA table_info(founder_audit)")}
    # the filter reads the lines filled so: the company's own two (made, renamed), its backup and the membership
    assert _ok(c.get("/api/founder/audit", params={"company": mill["id"], "group": "companies"}))["total"] == 2
    assert _ok(c.get("/api/founder/audit", params={"company": mill["id"]}))["total"] == 4
    assert _ok(c.get("/api/founder/audit", params={"company": twin_a["id"]}))["total"] == 0


@pytest.mark.parametrize("action", ["made a company", "made an account"])
def test_the_console_s_lines_carry_their_company(fdb, clock, action):
    c = _console()
    made = _company(c)["company"]
    _ok(c.post("/api/founder/users", json={"email": "sunia@meow.test", "name": "Sunia Ahmed", "password": "sunia first password", "company_id": made["id"]}),
        201)
    line = next(e for e in _ok(c.get("/api/founder/audit"))["entries"] if e["action"] == action)
    assert line["company_id"] == made["id"] and line["company_name"] == "Boxlytics Internal", line
