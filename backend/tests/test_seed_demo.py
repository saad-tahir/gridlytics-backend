"""The demo companies, people and passwords (`python -m gridlytics.seed_demo`), carried over from Solarlytics' `tests/test_seed_demo.py` (round 7, audit
parity-architecture-10; the owner's decisions of 24 October 2026, section 6).

The seed makes, in local mode only, the companies and people of the decisions (or of the owner's own list, `--from companies.csv users.csv`) with new
random passwords, as the founder console makes them, sets the demo roles as the company's owner would (the users route's own rules) and makes the
founder's login. The passwords and the founder's code secret go only to the git-ignored accounts list, the founder's four settings to the git-ignored
`.env.founder` in the data folder; nothing the command prints holds one. The demo sites' history (demo.load) is test_fix_r7_demo_seed's: these runs pass
`--no-demo-sites`.

Every test has its own data folder; the accounts list and the settings file are written to the test's own folder. The passwords are made at run time and
never printed by these tests either, not even when one fails: PYTEST_DONT_REWRITE turns off pytest's rewriting of this file's asserts, which would show
the values compared (a password among them), and `_short_tracebacks` reports a failure without the arguments and locals of the functions it went
through. A failure names its lines, its exception and the message it gives.
"""
from __future__ import annotations

import csv
import os
import re
import shutil
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gridlytics import api, auth, founder, identity, mfa, seed_demo, store, store_founder, store_members
from gridlytics import platform as platformdb

REPO = Path(__file__).resolve().parents[2]
#: The decisions' list (section 6, as Gridlytics has it): the companies with their people, each person's company role and the Gridlytics role they get
DECIDED = {
    "OnyxLeaf Demo": [("owner@onyxleaf.demo", "Demo Owner", "owner", "admin"), ("operator@onyxleaf.demo", "Demo Operator", "member", "operator"),
                      ("analyst@onyxleaf.demo", "Demo Analyst", "member", "analyst"), ("viewer@onyxleaf.demo", "Demo Viewer", "member", "viewer")],
    "Development": [("dev@localhost", "Local Tester", "owner", "admin")],
}
NO_SITES = "--no-demo-sites"


@pytest.fixture(autouse=True)
def _short_tracebacks(request):
    """A failure of this file reported in pytest's short style, without locals: the report is made at the end of the test's call, while this is set."""
    option = request.config.option
    before = (getattr(option, "tbstyle", "auto"), getattr(option, "showlocals", False))
    option.tbstyle, option.showlocals = "short", False
    yield
    option.tbstyle, option.showlocals = before


@pytest.fixture
def db(tmp_path, monkeypatch):
    """The test's own database, and with it its own registry (platform.db, beside it) and its own companies' files."""
    monkeypatch.delenv("GRIDLYTICS_DATA_DIR", raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "seed.db")
    return tmp_path / "seed.db"


def _run(capsys, *args: str) -> tuple[int, str]:
    """The command, as `python -m gridlytics.seed_demo` runs it, and everything it printed."""
    code = seed_demo.main([*args, NO_SITES])
    out = capsys.readouterr()
    return code, out.out + out.err


def _listed(path: Path) -> dict:
    """What the accounts list says: each person's password by address (None where the list says it is not known), and the founder's login."""
    text = path.read_text(encoding="utf-8")
    people = {}
    for line in text.splitlines():
        m = re.match(r"^\|[^|]*\|[^|]*\| `([^`]+)` \| (`[^`]+`|[^|]+?) \|", line)
        if m:
            people[m.group(1)] = m.group(2)[1:-1] if m.group(2).startswith("`") else None
    founder_login = {key: re.search(rf"^- \*\*{label}:\*\* `([^`]+)`", text, re.M) for key, label in
                     (("username", "Username"), ("password", "Password"), ("secret", "Code secret"), ("link", "Authenticator link"))}
    return {"people": people, "founder": {k: m.group(1) if m else None for k, m in founder_login.items()}, "text": text}


def _settings(path: Path) -> dict:
    """The founder's settings file, read as a shell or the launcher reads it: NAME='value' lines, the quotes taken off."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"(GRIDLYTICS_[A-Z_]+)='([^']*)'", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _secrets_in(listed: dict, settings: dict) -> list[str]:
    return [p for p in listed["people"].values() if p] + [v for v in (listed["founder"]["password"], listed["founder"]["secret"]) if v] + \
        [settings[k] for k in ("GRIDLYTICS_FOUNDER_PASSWORD_HASH", "GRIDLYTICS_FOUNDER_TOTP_SECRET", "GRIDLYTICS_FOUNDER_SESSION_SECRET")]


def _hashes() -> dict:
    with closing(sqlite3.connect(platformdb.db_path())) as con:
        return dict(con.execute("SELECT email, password FROM accounts").fetchall())


def _founder_audit(action: str) -> list[dict]:
    return [e for e in platformdb.list_founder_audit() if e["action"] == action]


def _company(name: str) -> dict:
    return next(c for c in platformdb.list_companies() if c["name"] == name)


def _signs_in(email: str, password: str) -> dict | None:
    """The person's sign-in state in their company after signing in with the password, as the web app does; None when the password is refused."""
    c = TestClient(api.app)
    res = c.post("/api/local/session", json={"email": email, "password": password})
    if res.status_code != 200:
        return None
    account = platformdb.account_by_email(email)
    company = platformdb.memberships(account["id"])[0]["company_id"]
    return c.get("/api/auth/state", headers={"Authorization": f"Bearer {res.json()['access_token']}", "X-Gridlytics-Company": company}).json()


def _member(company: str, email: str) -> dict | None:
    with store.use_company(platformdb.company_file(_company(company))), store.use_org(store.DEFAULT_ORG):
        return store_members.member_by_email(email)


def _audit(company: str) -> list[dict]:
    with store.use_company(platformdb.company_file(_company(company))), store.use_org(store.DEFAULT_ORG):
        return store.list_audit()


# ------------------------------------------------------------------------------------------------ the list

def test_the_demo_list_is_the_decisions_and_the_templates_hold_it(db):
    companies, people = seed_demo.check_list(seed_demo.DEMO_COMPANIES, seed_demo.DEMO_PEOPLE)
    assert [c["name"] for c in companies] == list(DECIDED)
    for name, decided in DECIDED.items():
        assert [(p["email"], p["name"], p["company_role"], p["role"] or "admin") for p in people if p["company"] == name] == decided
    assert seed_demo.FOUNDER_USERNAME == "founder"
    # the templates beside the seed are the owner's list to fill in (Gridlytics' are a placeholder company, not the demo's): a list `--from` takes as
    # it is, in its columns, with one owner and a person of each other role
    t_companies, t_people = seed_demo.read_list(REPO / "demo" / "companies.template.csv", REPO / "demo" / "users.template.csv")
    assert len(t_companies) == 1 and [p["company_role"] for p in t_people].count("owner") == 1
    assert sorted(p["role"] for p in t_people if p["company_role"] != "owner") == ["analyst", "operator", "viewer"]
    with open(REPO / "demo" / "users.template.csv", encoding="utf-8", newline="") as f:
        assert next(csv.reader(f)) == ["email", "name", "company", "company_role", "role"]


# ------------------------------------------------------------------------------------------------ the seed

def test_the_seed_makes_the_companies_the_people_their_roles_and_the_founders_login(db, tmp_path, capsys, monkeypatch):
    out = tmp_path / "DEMO_ACCOUNTS.local.md"
    code, printed = _run(capsys, "--out", str(out))
    assert code == 0, printed
    listed, settings = _listed(out), _settings(db.parent / ".env.founder")

    # the companies, each with its owner named and Gridlytics on, with seats for its people
    for name, decided in DECIDED.items():
        row = _company(name)
        assert row["enabled"] and row["seats"] == max(platformdb.SEATS, len(decided))
        held = {m["email"]: (m["role"], m["status"]) for m in platformdb.company_members(row["id"])}
        assert held == {email: (company_role, "active") for email, _, company_role, _ in decided}
    # each made by the console's own functions, its lines in the founder's audit as the console writes them, by the seed
    made = _founder_audit("made a company")
    assert {(e["target"], e["detail"]["owner"], e["detail"]["new_owner"], e["detail"]["enabled"], e["actor"]) for e in made} == {
        ("OnyxLeaf Demo", "owner@onyxleaf.demo", True, True, "demo seed"), ("Development", "dev@localhost", True, True, "demo seed")}
    assert {(e["target"], e["detail"]["company"], e["detail"]["company_role"]) for e in _founder_audit("made an account")} == {
        (email, "OnyxLeaf Demo", "member") for email in ("operator@onyxleaf.demo", "analyst@onyxleaf.demo", "viewer@onyxleaf.demo")}

    # new random passwords: long, each its own, and each signs in to its company with the role the owner gave
    passwords = listed["people"]
    assert set(passwords) == {e for people in DECIDED.values() for e, *_ in people}
    assert all(p is not None and len(p) >= 16 for p in passwords.values())
    assert len(set(passwords.values())) == len(passwords)
    roles = {email: role for people in DECIDED.values() for email, _, _, role in people}
    for email, password in passwords.items():
        state = _signs_in(email, password)
        assert state is not None and state["user"]["role"] == roles[email], email

    # the roles were set ahead of the first sign-in as the owner would set them: the users route's rules, audited "changed a user" by the seed for the
    # owner (the viewer's too: the list names it, so it is set, not left to the default)
    owner = platformdb.account_by_email("owner@onyxleaf.demo")
    lines = [e for e in _audit("OnyxLeaf Demo") if e["action"] == "changed a user"]
    assert sorted(e["target"] for e in lines) == ["analyst@onyxleaf.demo", "operator@onyxleaf.demo", "viewer@onyxleaf.demo"]
    assert {(e["actor"], e["actor_id"], e["actor_email"]) for e in lines} == {("demo seed, for Demo Owner", owner["id"], "owner@onyxleaf.demo")}
    assert _member("OnyxLeaf Demo", "operator@onyxleaf.demo")["role_source"] == "set"
    assert _member("OnyxLeaf Demo", "owner@onyxleaf.demo")["role_source"] == "owner"        # signed in above: the owner, pinned

    # the founder's login: its four settings quoted in the git-ignored file, from which the console signs in with the listed password and a code
    assert set(settings) == set(founder.SETTINGS)
    text = (db.parent / ".env.founder").read_text(encoding="utf-8")
    assert "GRIDLYTICS_FOUNDER_PASSWORD_HASH='pbkdf2_sha256$" in text
    assert listed["founder"]["username"] == settings["GRIDLYTICS_FOUNDER_USERNAME"] == "founder"
    assert len(listed["founder"]["password"]) >= 16 and listed["founder"]["password"] not in passwords.values()
    assert listed["founder"]["secret"] == settings["GRIDLYTICS_FOUNDER_TOTP_SECRET"]
    assert listed["founder"]["link"] == mfa.uri(settings["GRIDLYTICS_FOUNDER_TOTP_SECRET"], "founder", f"{mfa.ISSUER} founder")
    assert len(settings["GRIDLYTICS_FOUNDER_SESSION_SECRET"]) >= 32
    assert "python -m gridlytics.founder code" in listed["text"]
    # the code secret is in the list's two lines that give it, never in a command to type, which the shell's history would keep
    holding = [line for line in listed["text"].splitlines() if settings["GRIDLYTICS_FOUNDER_TOTP_SECRET"] in line]
    assert [line.split(":**")[0] for line in holding] == ["- **Code secret", "- **Authenticator link"], "the code secret is in a command"
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    assert founder.missing() == [] and founder.malformed() == []
    challenge = founder.login("founder", listed["founder"]["password"], "127.0.0.1")["challenge"]
    key = mfa.secret_bytes(settings["GRIDLYTICS_FOUNDER_TOTP_SECRET"])
    assert founder.code(challenge, mfa.totp(key), "127.0.0.1")["token"]

    # nothing printed holds a password, a code secret or a hash; what it says is where they are
    assert not any(secret in printed for secret in _secrets_in(listed, settings)), "the command printed a secret"
    assert str(out) in printed and ".env.founder" in printed


def test_a_second_run_keeps_the_accounts_and_reset_passwords_makes_new_ones(db, tmp_path, capsys, monkeypatch):
    out = tmp_path / "DEMO_ACCOUNTS.local.md"
    assert _run(capsys, "--out", str(out))[0] == 0
    first, hashes, settings = _listed(out), _hashes(), (db.parent / ".env.founder").read_text(encoding="utf-8")
    audited = len(platformdb.list_founder_audit())
    # an owner chooses another role for the operator after the seed: a second run leaves it as it is
    with store.use_company(platformdb.company_file(_company("OnyxLeaf Demo"))), store.use_org(store.DEFAULT_ORG):
        store_members.update_member(platformdb.account_by_email("operator@onyxleaf.demo")["id"], role="analyst", role_source="set", by="Demo Owner")
    # and the founder gives the viewer a new password in the console: the list cannot know it
    store_founder.set_password(platformdb.account_by_email("viewer@onyxleaf.demo")["id"], "a password the founder chose 1")

    code, printed = _run(capsys, "--out", str(out))
    assert code == 0, printed
    second = _listed(out)
    assert len(platformdb.list_companies()) == 2 and len(_hashes()) == 5
    assert len(platformdb.list_founder_audit()) == audited                                    # nothing made, nothing changed
    viewer = "viewer@onyxleaf.demo"
    assert {k: v for k, v in _hashes().items() if k != viewer} == {k: v for k, v in hashes.items() if k != viewer}
    assert {k: v for k, v in second["people"].items() if k != viewer} == {k: v for k, v in first["people"].items() if k != viewer}
    assert second["people"][viewer] is None and "--reset-passwords" in second["text"]
    assert second["founder"] == first["founder"] and (db.parent / ".env.founder").read_text(encoding="utf-8") == settings
    assert _member("OnyxLeaf Demo", "operator@onyxleaf.demo")["role"] == "analyst"
    assert len([e for e in _audit("OnyxLeaf Demo") if e["action"] == "changed a user"]) == 3
    assert not any(secret in printed for secret in _secrets_in(second, _settings(db.parent / ".env.founder"))), "the command printed a secret"

    # a founder session of the settings before the reset
    old = _settings(db.parent / ".env.founder")
    for name, value in old.items():
        monkeypatch.setenv(name, value)
    session, _ = founder.new_token()
    assert founder.verify(session)

    code, printed = _run(capsys, "--out", str(out), "--reset-passwords")
    assert code == 0, printed
    third, now = _listed(out), _settings(db.parent / ".env.founder")
    assert all(_hashes()[email] != hashes[email] for email in hashes)
    assert all(third["people"][email] not in (first["people"][email], None) for email in first["people"])
    assert {e["target"] for e in _founder_audit("set a password")} == set(first["people"])
    assert _signs_in("operator@onyxleaf.demo", third["people"]["operator@onyxleaf.demo"]) is not None
    assert _signs_in("owner@onyxleaf.demo", first["people"]["owner@onyxleaf.demo"]) is None
    # the founder's password is new; the code secret (already in the authenticator app) is kept; the session key is new, so that the new password ends
    # every founder session, as an account's new password ends its sessions
    assert third["founder"]["password"] != first["founder"]["password"]
    assert now["GRIDLYTICS_FOUNDER_PASSWORD_HASH"] != old["GRIDLYTICS_FOUNDER_PASSWORD_HASH"]
    assert founder.check_password(third["founder"]["password"], now["GRIDLYTICS_FOUNDER_PASSWORD_HASH"])
    assert now["GRIDLYTICS_FOUNDER_TOTP_SECRET"] == old["GRIDLYTICS_FOUNDER_TOTP_SECRET"]
    assert now["GRIDLYTICS_FOUNDER_SESSION_SECRET"] != old["GRIDLYTICS_FOUNDER_SESSION_SECRET"]
    assert len(now["GRIDLYTICS_FOUNDER_SESSION_SECRET"]) >= 32
    for name, value in now.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(founder.Refused):
        founder.verify(session)
    assert not any(secret in printed for secret in _secrets_in(third, now)), "the command printed a secret"


def test_the_seed_refuses_in_supabase_mode_and_writes_nothing(db, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(identity, "MODE", "supabase")
    out = tmp_path / "DEMO_ACCOUNTS.local.md"
    code, printed = _run(capsys, "--out", str(out))
    assert code == 1 and "local mode" in printed and "founder console" in printed
    assert not out.exists() and not (db.parent / ".env.founder").exists()
    assert not platformdb.db_path().exists() or (platformdb.count_accounts() == 0 and platformdb.list_companies() == [])
    # the mode is said before an owner's list is read: there is nothing to check it for
    code, printed = _run(capsys, "--from", str(tmp_path / "no-companies.csv"), str(tmp_path / "no-users.csv"), "--out", str(out))
    assert code == 1 and "local mode" in printed and "no such file" not in printed, printed


# ------------------------------------------------------------------------------------------------ the owner's own list

def _write(path: Path, rows: list[list[str]]) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(rows)
    return path


def test_from_two_csv_files_loads_the_owners_list_with_its_seats_and_roles(db, tmp_path, capsys):
    companies = _write(tmp_path / "companies.csv", [["name", "seats"], ["Acme Mills", "2"], ["Sunny Farms", ""]])
    users = _write(tmp_path / "users.csv", [
        ["email", "name", "company", "company_role", "role"],
        ["ali@acme.test", "Ali Raza", "Acme Mills", "owner", ""],
        ["hina@acme.test", "Hina Shah", "acme mills", "admin", "analyst"],
        ["omar@acme.test", "Omar Farooq", "Acme Mills", "member", "operator"],
        ["zara@acme.test", "Zara Malik", "Acme Mills", "member", ""],
        ["hina@acme.test", "Hina Shah", "Sunny Farms", "owner", "admin"],
    ])
    out = tmp_path / "DEMO_ACCOUNTS.local.md"
    code, printed = _run(capsys, "--from", str(companies), str(users), "--out", str(out))
    assert code == 0, printed
    assert {c["name"]: (c["seats"], c["enabled"]) for c in platformdb.list_companies()} == {"Acme Mills": (2, True), "Sunny Farms": (platformdb.SEATS, True)}
    assert {m["email"]: m["role"] for m in platformdb.company_members(_company("Acme Mills")["id"])} == {
        "ali@acme.test": "owner", "hina@acme.test": "admin", "omar@acme.test": "member", "zara@acme.test": "member"}
    assert {m["email"]: m["role"] for m in platformdb.company_members(_company("Sunny Farms")["id"])} == {"hina@acme.test": "owner"}
    # one account, one password, for a person of two companies
    assert len(_hashes()) == 4 and len(set(_listed(out)["people"].values())) == 4
    # the seats hold as the owner's own change holds them: Hina takes the second seat (the owner keeps one of their own), and Omar's role is kept for him,
    # his access off for want of a seat until the founder gives one; nobody's role is set where the list names none
    hina, omar = _member("Acme Mills", "hina@acme.test"), _member("Acme Mills", "omar@acme.test")
    assert (hina["role"], hina["disabled"]) == ("analyst", False)
    assert (omar["role"], omar["disabled"], omar["disabled_reason"]) == ("operator", True, "no seat")
    assert _member("Acme Mills", "zara@acme.test") is None
    assert "Omar Farooq" in printed and "seat" in printed
    assert _member("Sunny Farms", "hina@acme.test") is None                                   # the owner's role is the owner's: nothing to set
    assert not any(secret in printed for secret in _secrets_in(_listed(out), _settings(db.parent / ".env.founder"))), "the command printed a secret"


def test_roles_are_set_only_by_an_owner_who_can_sign_in_now(db, tmp_path, capsys):
    """A list run again on a registry the founder changed since: with the company's owner disabled, nobody sets roles as that owner, and no administrator
    is pinned for them."""
    companies = _write(tmp_path / "companies.csv", [["name"], ["Acme Mills"]])
    header, ali = ["email", "name", "company", "company_role", "role"], ["ali@acme.test", "Ali Raza", "Acme Mills", "owner", ""]
    out = tmp_path / "DEMO_ACCOUNTS.local.md"
    assert _run(capsys, "--from", str(companies), str(_write(tmp_path / "users.csv", [header, ali])), "--out", str(out))[0] == 0
    store_founder.update_account(platformdb.account_by_email("ali@acme.test")["id"], disabled=True, leave_without_owner=True)
    users = _write(tmp_path / "users.csv", [header, ali, ["hina@acme.test", "Hina Shah", "Acme Mills", "member", "analyst"]])
    code, printed = _run(capsys, "--from", str(companies), str(users), "--out", str(out))
    assert code == 0 and "Ali Raza cannot sign in to it as its owner" in printed, "the note is missing"
    assert platformdb.membership(platformdb.account_by_email("hina@acme.test")["id"], _company("Acme Mills")["id"])["status"] == "active"
    assert _member("Acme Mills", "hina@acme.test") is None and _member("Acme Mills", "ali@acme.test") is None
    assert not [e for e in _audit("Acme Mills") if e["action"] == "changed a user"]


@pytest.mark.parametrize("companies, users, said", [
    ([["name"], ["Acme Mills"]], [["email", "name", "company", "company_role"], ["a@acme.test", "A", "Acme Mills", "member"]],
     "Acme Mills has no owner"),
    ([["name"], ["Acme Mills"]], [["email", "name", "company", "company_role"], ["a@acme.test", "A", "Acme Mills", "owner"],
                                  ["b@acme.test", "B", "Acme Mills", "owner"]], "one owner"),
    ([["name"], ["Acme Mills"]], [["email", "name", "company", "company_role"], ["a@acme.test", "A", "Acme Millz", "owner"]], "line 2"),
    ([["name"], ["Acme Mills"]], [["email", "name", "company", "company_role"], ["a@acme.test", "A", "Acme Mills", "boss"]], "company_role"),
    ([["name"], ["Acme Mills"]], [["email", "name", "company", "company_role", "role"], ["a@acme.test", "A", "Acme Mills", "owner", ""],
                                  ["b@acme.test", "B", "Acme Mills", "member", "estimator"]], "line 3"),
    ([["name"], ["Acme Mills"]], [["email", "name", "company", "company_role", "role"], ["a@acme.test", "A", "Acme Mills", "owner", "viewer"]],
     "administrator"),
    ([["name"], ["Acme Mills"], ["ACME MILLS"]], [["email", "name", "company", "company_role"], ["a@acme.test", "A", "Acme Mills", "owner"]],
     "twice"),
    ([["name", "seats"], ["Acme Mills", "many"]], [["email", "name", "company", "company_role"], ["a@acme.test", "A", "Acme Mills", "owner"]],
     "seats"),
    ([["name"], ["Acme Mills"]], [["email", "name", "company"], ["a@acme.test", "A", "Acme Mills"]], "company_role"),
    ([["name"], ["Acme Mills"]], [["email", "name", "company", "company_role"], ["not an address", "A", "Acme Mills", "owner"]], "email"),
    ([["name"]], [["email", "name", "company", "company_role"]], "no company"),
])
def test_a_list_with_a_mistake_is_refused_before_anything_is_written(db, tmp_path, capsys, companies, users, said):
    out = tmp_path / "DEMO_ACCOUNTS.local.md"
    code, printed = _run(capsys, "--from", str(_write(tmp_path / "companies.csv", companies)), str(_write(tmp_path / "users.csv", users)),
                         "--out", str(out))
    assert code == 1 and said in printed, printed
    assert (not platformdb.db_path().exists() or platformdb.count_accounts() == 0) and not out.exists() and not (db.parent / ".env.founder").exists()


def test_a_list_saved_by_a_spreadsheet_is_read(db, tmp_path):
    """Excel saves a CSV in UTF-8 with a byte order mark, and may leave empty rows, a column with no name and spaces around the cells."""
    (tmp_path / "companies.csv").write_bytes("﻿name,seats,\r\n Acme Mills , 3,\r\n,,\r\n".encode("utf-8"))
    (tmp_path / "users.csv").write_bytes("﻿email,name,company,company_role,role\r\n Ali@Acme.test ,Ali Raza,Acme Mills, owner ,\r\n".encode("utf-8"))
    companies, people = seed_demo.read_list(tmp_path / "companies.csv", tmp_path / "users.csv")
    assert companies == [{"name": "Acme Mills", "seats": 3}]
    assert people == [{"email": "ali@acme.test", "name": "Ali Raza", "company": "Acme Mills", "company_role": "owner", "role": ""}]


# ------------------------------------------------------------------------------------------------ where it writes, and git

def test_the_list_and_the_settings_sit_with_the_data_they_describe(tmp_path, monkeypatch):
    # paths only: nothing is written, and the repository's data folder is never made
    monkeypatch.delenv("GRIDLYTICS_DATA_DIR", raising=False)
    monkeypatch.setattr(store, "DB_PATH", REPO / "data" / "gridlytics.db")
    assert seed_demo.accounts_file() == REPO / "demo" / "DEMO_ACCOUNTS.local.md"
    assert seed_demo.env_file() == REPO / "data" / ".env.founder"
    # a scratch data folder (a test, the browser check): beside it, so that a scratch run never replaces the owner's list
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "e2e.db")
    assert seed_demo.accounts_file() == tmp_path / "DEMO_ACCOUNTS.local.md"
    assert seed_demo.env_file() == tmp_path / ".env.founder"


_GIT = shutil.which("git") is not None and (REPO / ".git").exists()


@pytest.mark.skipif(not _GIT, reason="needs git and the repository")
def test_the_accounts_list_and_the_founders_settings_are_ignored_by_git_and_the_templates_are_not():
    for path in ("demo/DEMO_ACCOUNTS.local.md", "data/.env.founder"):
        assert subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=REPO).returncode == 0, path
    for path in ("demo/companies.template.csv", "demo/users.template.csv", "backend/gridlytics/seed_demo.py"):
        assert subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=REPO).returncode == 1, path


@pytest.mark.skipif(not _GIT, reason="needs git and the repository")
def test_the_passwords_never_go_to_a_file_of_the_repository_that_git_would_take(db, capsys):
    taken = REPO / "demo" / "accounts.md"
    try:
        code, printed = _run(capsys, "--out", str(taken))
        assert code == 1 and "git does not ignore it" in printed
        assert not taken.exists() and (not platformdb.db_path().exists() or platformdb.count_accounts() == 0)
    finally:
        taken.unlink(missing_ok=True)                 # should the guard ever fail, no list of passwords is left where git would take it


@pytest.mark.skipif(not _GIT, reason="needs git and the repository")
def test_the_founders_settings_are_guarded_too(db, tmp_path, capsys, monkeypatch):
    """The settings file is in the data folder, which git ignores; should that ever not hold, nothing is written either."""
    taken = REPO / "demo" / "founder-settings.txt"
    monkeypatch.setattr(seed_demo, "env_file", lambda: taken)
    try:
        code, printed = _run(capsys, "--out", str(tmp_path / "DEMO_ACCOUNTS.local.md"))
        assert code == 1 and "git does not ignore it" in printed
        assert not taken.exists() and (not platformdb.db_path().exists() or platformdb.count_accounts() == 0)
    finally:
        taken.unlink(missing_ok=True)


def test_a_run_that_fails_after_making_passwords_still_lists_them(db, tmp_path, monkeypatch):
    """A password that is in no list is lost: the list is written whatever fails once the first one is made."""
    def broken(*args):
        raise RuntimeError("made up: the roles could not be set")

    monkeypatch.setattr(seed_demo, "_set_roles", broken)
    out = tmp_path / "DEMO_ACCOUNTS.local.md"
    with pytest.raises(RuntimeError):
        seed_demo.seed(out=out)
    listed = _listed(out)
    assert len(listed["people"]) == 5 and all(platformdb.check_sign_in(e, p) is not None for e, p in listed["people"].items() if p), "a password is lost"
    assert all(listed["people"].values()), "a password is lost"
    assert founder.check_password(listed["founder"]["password"], _settings(db.parent / ".env.founder")["GRIDLYTICS_FOUNDER_PASSWORD_HASH"])


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_the_founders_settings_load_into_an_environment_exactly(db, tmp_path, capsys):
    """The settings file read by a POSIX shell (as docker compose's env_file and `set -a; . file` read it): the four values come back exactly, the $
    signs of the PBKDF2 hash kept (the values are quoted). The values never pass through this test's output."""
    out = tmp_path / "DEMO_ACCOUNTS.local.md"
    assert _run(capsys, "--out", str(out))[0] == 0
    env_file = db.parent / ".env.founder"
    script = 'set -a; . "$1"; set +a; for n in ' + " ".join(founder.SETTINGS) + '; do printenv "$n"; done'
    clean = {k: v for k, v in os.environ.items() if k not in founder.SETTINGS}       # the conftest's made-up founder settings would hide a value not loaded
    done = subprocess.run([shutil.which("sh"), "-c", script, "sh", env_file.as_posix()], capture_output=True, text=True, timeout=120, env=clean)
    assert done.returncode == 0, "the shell refused the file"
    assert done.stdout.splitlines() == [_settings(env_file)[n] for n in founder.SETTINGS], "a setting did not load as written"


def test_a_password_is_random_long_and_easy_to_type():
    made = {seed_demo.new_password() for _ in range(50)}
    assert len(made) == 50
    assert all(len(p) >= 16 and re.fullmatch(r"[A-Za-z2-9]+", p) and not set(p) & set("O0Il1") for p in made)
    assert all(auth.password_problem(p) is None for p in made)
