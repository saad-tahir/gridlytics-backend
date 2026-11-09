"""The demo companies, people and passwords (the owner's decisions of 24 October 2026, section 6; stage D1 of the sign-in and split).

    python -m gridlytics.seed_demo [--reset-passwords] [--from companies.csv users.csv] [--out <file>] [--no-demo-sites] [--demo-days N]

Local mode only: in supabase mode the accounts are the Gridlytics project's, and the founder console makes them there. The seed does what the founder
console and the companies' owners would do, with their own functions, so that the founder's audit and each company's audit read as theirs:

* each company is made with its owner (R-405), Gridlytics on, and seats for its people (platform.SEATS, or more for a longer list), and every person gets
  an account with a new random password and a membership with their company role (store_founder.py and platform.py, each change in the founder's audit,
  by "demo seed");
* the Gridlytics roles the list gives are set as the company's owner sets them, through the users route itself (api.users_edit: the owner pinned, the seats,
  the row made ahead of a person's first sign-in), with the owner's own first sight made first, as their sign-in would, and audited "changed a user" by
  "demo seed, for <the owner>";
* the founder console's login: the username `founder`, a new password, its hash made by founder.py, a Base32 code secret and a session key. The API reads
  the four settings from its environment only (decisions section 4), so they are written to `.env.founder` in the data folder, which git ignores
  (`.env.*`), quoted, for the launcher or the shell to load.

The list of what was made, with every password, the founder's code secret, its otpauth:// link and the command that prints the current code, goes only to
`demo/DEMO_ACCOUNTS.local.md` (git-ignored), or beside the data when the data folder is not the repository's own `data/` (a test, the browser check), so
that a scratch run never replaces the owner's list. Nothing printed holds a password or a secret.

Then, for the decisions' list, the four demo sites of plan Part 9.6 go into the OnyxLeaf Demo company with their history (`demo.load`, 21 days unless
`--demo-days` says otherwise; `--no-demo-sites` leaves them out). They are not read from a file: their specs are `demo.DEMO_SITES`.

Running it again keeps what is there: a company, an account, its password (carried over from the list written before, where it still signs in), a
membership, a role chosen since, and the demo sites. `--reset-passwords` gives every account of the list, and the founder, a new password, which
ends their sessions; the founder's code secret is kept, as the authenticator app holds it already, and the founder's session key is new.

`--from companies.csv users.csv` loads the owner's real list instead (the templates to copy are `demo/companies.template.csv` and
`demo/users.template.csv` in this repository):
companies with `name` and `seats` (empty: enough for its people), people with `email`, `name`, `company`, `company_role` (owner, admin or member; each
company exactly one owner) and `role` (a Gridlytics role, empty for the default: the owner is the administrator, everyone else a viewer until the owner
chooses). The whole list is checked before anything is written.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import secrets
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from . import auth, founder, identity, mfa, store, store_founder, store_members
from . import platform as platformdb

ROOT = Path(__file__).resolve().parents[2]
#: Who the founder's audit says made the demo's companies and accounts
ACTOR = "demo seed"
FOUNDER_USERNAME = "founder"
#: The founder's four settings, in the data folder, ignored by git's `.env.*`
ENV_NAME = ".env.founder"
ACCOUNTS_NAME = "DEMO_ACCOUNTS.local.md"
#: A password's characters: letters and digits, without those that read alike (0 O, 1 l I); 20 of them are about 115 bits
PASSWORD_LENGTH = 20
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
#: The decisions' list (section 6): the companies of Boxlytics' repositories, and the people of its README's demo table and its dev user
DEMO_COMPANIES = ({"name": "OnyxLeaf Demo", "seats": None}, {"name": "Development", "seats": None})
DEMO_PEOPLE = (
    {"email": "owner@onyxleaf.demo", "name": "Demo Owner", "company": "OnyxLeaf Demo", "company_role": "owner", "role": ""},
    {"email": "operator@onyxleaf.demo", "name": "Demo Operator", "company": "OnyxLeaf Demo", "company_role": "member", "role": "operator"},
    {"email": "analyst@onyxleaf.demo", "name": "Demo Analyst", "company": "OnyxLeaf Demo", "company_role": "member", "role": "analyst"},
    {"email": "viewer@onyxleaf.demo", "name": "Demo Viewer", "company": "OnyxLeaf Demo", "company_role": "member", "role": "viewer"},
    {"email": "dev@localhost", "name": "Local Tester", "company": "Development", "company_role": "owner", "role": ""},
)
COMPANY_COLUMNS = ("name", "seats")
USER_COLUMNS = ("email", "name", "company", "company_role", "role")
_NEEDED = {"companies": ("name",), "users": ("email", "name", "company", "company_role")}
_SETTING = re.compile(r"""(GRIDLYTICS_FOUNDER_[A-Z_]+)=(?:'([^']*)'|"([^"]*)"|(\S*))""")
_UNKNOWN = "not known: changed since this list was made (run the seed again with --reset-passwords for a new one)"


class Refused(Exception):
    """Why nothing was made. Raised before anything is written."""


# ------------------------------------------------------------------------------------------------ where it writes

def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def env_file() -> Path:
    """The founder's four settings: `.env.founder` in the data folder (platform.data_dir)."""
    return platformdb.data_dir() / ENV_NAME


def accounts_file() -> Path:
    """The list of what was made: the decisions' `demo/DEMO_ACCOUNTS.local.md` for the repository's own data folder, else beside the data it describes."""
    data = platformdb.data_dir()
    return ROOT / "demo" / ACCOUNTS_NAME if _same(data, ROOT / "data") else data / ACCOUNTS_NAME


def _ignored(path: Path) -> bool:
    """Whether a file in the repository is one git ignores; True outside it, or where git cannot tell."""
    try:
        inside = os.path.relpath(os.path.abspath(path), os.path.abspath(ROOT))
    except ValueError:                                  # another drive
        return True
    if inside == ".." or inside.startswith(".." + os.sep):
        return True
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        return True
    done = subprocess.run([git, "check-ignore", "-q", "--no-index", Path(inside).as_posix()], cwd=ROOT, capture_output=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return done.returncode != 1


def _write_private(path: Path, text: str) -> None:
    """A file only its owner reads where the system has such modes (POSIX); the passwords and the secrets are in it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if os.name == "posix":
        os.fchmod(fd, 0o600)                            # a file made before with wider modes is narrowed before the secrets go in
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def new_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(PASSWORD_LENGTH))


# ------------------------------------------------------------------------------------------------ the list

def _cell(value) -> str:
    return value.strip() if isinstance(value, str) else "" if value is None else str(value).strip()


def check_list(companies, people, *, files: tuple[str, str] | None = None, lines: tuple[list[int], list[int]] | None = None) -> tuple[list, list]:
    """The list as the seed makes it: names trimmed, addresses in lower case, each person's company as the company list writes it, a seat count or None
    (enough for its people), the owner's role empty (the owner is the administrator, pinned). Refused, naming the line, for anything the founder console
    would refuse, a company without exactly one owner, and a person named twice in a company or by two names."""
    companies, people = list(companies), list(people)

    def at(kind: int, i: int) -> str:
        if lines is not None:
            return f"{files[kind]}, line {lines[kind][i]}"
        return f"{('company', 'person')[kind]} {i + 1} of the list"

    if not companies:
        raise Refused(f"{files[0]}: no company in the list" if files else "The list has no company")
    made, by_name = [], {}
    for i, c in enumerate(companies):
        name = _cell(c.get("name"))
        if not 1 <= len(name) <= 80:
            raise Refused(f"{at(0, i)}: a company's name has 1 to 80 characters")
        if name.lower() in by_name:
            raise Refused(f"{at(0, i)}: {name} is in the list twice")
        seats = _cell(c.get("seats"))
        if seats and not (re.fullmatch(r"[0-9]{1,6}", seats) and int(seats) <= 100_000):     # "²" is a digit to str.isdigit, not to int
            raise Refused(f"{at(0, i)}: seats is a whole number from 0 (or empty for enough for its people), not {seats}")
        by_name[name.lower()] = name
        made.append({"name": name, "seats": int(seats) if seats else None})
    out, names, owners, seen = [], {}, {}, set()
    for i, p in enumerate(people):
        email, name, company = _cell(p.get("email")), _cell(p.get("name")), _cell(p.get("company"))
        company_role, role = _cell(p.get("company_role")).lower(), _cell(p.get("role")).lower()
        try:
            email = platformdb._email(email)
        except ValueError:
            raise Refused(f"{at(1, i)}: {email or 'an empty cell'} is not an email address") from None
        if not 1 <= len(name) <= 80:
            raise Refused(f"{at(1, i)}: a person's name has 1 to 80 characters")
        if company.lower() not in by_name:
            raise Refused(f"{at(1, i)}: no company {company or '(empty)'} in the companies' list")
        company = by_name[company.lower()]
        if company_role not in platformdb.COMPANY_ROLES:
            raise Refused(f"{at(1, i)}: company_role is {', '.join(platformdb.COMPANY_ROLES[:-1])} or {platformdb.COMPANY_ROLES[-1]}, "
                          f"not {company_role or '(empty)'}")
        if role and role not in auth.ROLES:
            raise Refused(f"{at(1, i)}: role {role} is not a Gridlytics role: one of {', '.join(auth.ROLES)}, or empty for the default")
        if company_role == "owner":
            if role not in ("", "admin"):
                raise Refused(f"{at(1, i)}: a company's owner is its administrator in Gridlytics, and stays one: leave role empty or admin")
            role = ""
            owners.setdefault(company, []).append(at(1, i))
        if names.setdefault(email, name) != name:
            raise Refused(f"{at(1, i)}: {email} has two names in the list ({names[email]} and {name})")
        if (email, company) in seen:
            raise Refused(f"{at(1, i)}: {email} is in {company} twice")
        seen.add((email, company))
        out.append({"email": email, "name": name, "company": company, "company_role": company_role, "role": role})
    for c in made:
        held = owners.get(c["name"], [])
        if not held:
            raise Refused(f"{c['name']} has no owner: give one of its people the company_role owner (a company is made with its owner)")
        if len(held) > 1:
            raise Refused(f"{c['name']} is made with one owner, and the list names {len(held)} ({'; '.join(held)})")
    return made, out


def _rows(path: Path, kind: str) -> tuple[list[dict], list[int]]:
    """A CSV file's rows by column, and the line each is on. A spreadsheet's byte order mark, spaces around a header and empty rows are taken."""
    columns = COMPANY_COLUMNS if kind == "companies" else USER_COLUMNS
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            header = [h.strip().lower() for h in next(reader, [])]
            if not any(header):
                raise Refused(f"{path.name}: the first line names the columns: {', '.join(columns)}")
            unknown = [h for h in header if h and h not in columns]      # a column with no name (a trailing comma) is left out
            if unknown:
                raise Refused(f"{path.name}: unknown column {', '.join(unknown)}; the columns are {', '.join(columns[:-1])} and {columns[-1]}")
            twice = sorted({h for h in header if h and header.count(h) > 1})
            if twice:
                raise Refused(f"{path.name}: the column {' and '.join(twice)} is there twice")
            missing = [c for c in _NEEDED[kind] if c not in header]
            if missing:
                raise Refused(f"{path.name}: the column {' and '.join(missing)} is missing")
            rows, lines = [], []
            for cells in reader:
                if not any(c.strip() for c in cells):
                    continue
                if len(cells) > len(header):
                    raise Refused(f"{path.name}, line {reader.line_num}: more cells than columns")
                rows.append(dict(zip(header, cells)))
                lines.append(reader.line_num)
    except FileNotFoundError:
        raise Refused(f"{path}: no such file") from None
    except (UnicodeDecodeError, csv.Error) as exc:
        raise Refused(f"{path.name}: not a CSV file in UTF-8 ({exc})") from None
    return rows, lines


def read_list(companies_csv, users_csv) -> tuple[list, list]:
    """The owner's list from two CSV files (`--from`), checked as a whole (check_list)."""
    companies_csv, users_csv = Path(companies_csv), Path(users_csv)
    companies, company_lines = _rows(companies_csv, "companies")
    people, people_lines = _rows(users_csv, "users")
    return check_list(companies, people, files=(companies_csv.name, users_csv.name), lines=(company_lines, people_lines))


# ------------------------------------------------------------------------------------------------ what an earlier run wrote

def _earlier(path: Path) -> dict:
    """The passwords the list written before holds, by address, and the founder's: a run that keeps an account lists its password again where it still
    signs in."""
    out = {"people": {}, "founder": None}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for m in re.finditer(r"^\|.*?\| `([^`|]+)` \| `([^`|]+)` \|", text, re.M):
        out["people"].setdefault(m.group(1), m.group(2))
    if (m := re.search(r"^- \*\*Password:\*\* `([^`]+)`", text, re.M)):
        out["founder"] = m.group(1)
    return out


def _read_settings(path: Path) -> dict:
    out = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return out
    for line in lines:
        if (m := _SETTING.fullmatch(line.strip())):
            out[m.group(1)] = next(v for v in m.groups()[1:] if v is not None)
    return out


# ------------------------------------------------------------------------------------------------ the founder's login

def _founder_login(reset: bool, earlier: str | None) -> dict:
    """The founder console's four settings in `.env.founder`: kept when they are whole and no reset is asked; else a new password and its hash, the code
    secret there kept where it can work (the authenticator app holds it), and the session key kept where it can work, but new at a reset: a new password
    ends every founder session, as the founder's new password for an account ends that account's (platform.set_password)."""
    path = env_file()
    held = _read_settings(path)
    username = held.get("GRIDLYTICS_FOUNDER_USERNAME", "")
    username = username if re.fullmatch(r"[^\s'\"]{1,80}", username) else FOUNDER_USERNAME
    secret = held.get("GRIDLYTICS_FOUNDER_TOTP_SECRET", "")
    try:
        mfa.secret_bytes(secret)
    except ValueError:
        secret = ""
    session = held.get("GRIDLYTICS_FOUNDER_SESSION_SECRET", "")
    session = session if len(session) >= 32 and "'" not in session else ""
    stored = held.get("GRIDLYTICS_FOUNDER_PASSWORD_HASH", "")
    whole = bool(secret and session and founder._HASH_FORMS.fullmatch(stored)) and username == held.get("GRIDLYTICS_FOUNDER_USERNAME")
    out = {"file": path, "username": username, "secret": secret, "made": False, "reset": False}
    if whole and not reset:
        return {**out, "password": earlier if earlier and founder.check_password(earlier, stored) else None}
    password = new_password()
    values = {"GRIDLYTICS_FOUNDER_USERNAME": username, "GRIDLYTICS_FOUNDER_PASSWORD_HASH": founder.hash_password(password),
              "GRIDLYTICS_FOUNDER_TOTP_SECRET": secret or mfa.new_secret(),
              "GRIDLYTICS_FOUNDER_SESSION_SECRET": session if session and not reset else secrets.token_urlsafe(48)}
    # Quoted: the PBKDF2 hash holds $ signs, which a shell or docker compose would read as variables
    _write_private(path, f"# The founder console's login, made by python -m gridlytics.seed_demo on {datetime.now():%Y-%m-%d %H:%M} for a demonstration "
                         "in local mode.\n# The API reads these four settings from its environment only: load this file into the environment of the process "
                         "that starts it.\n# Git ignores this file (.env.*): never commit it. The password itself is in the demo's accounts list.\n"
                         + "".join(f"{k}='{v}'\n" for k, v in values.items()))
    return {**out, "secret": values["GRIDLYTICS_FOUNDER_TOTP_SECRET"], "password": password, "made": not held, "reset": bool(held)}


# ------------------------------------------------------------------------------------------------ the roles, as the owner sets them

def _set_roles(company: dict, people: list[dict], notes: list[str]) -> list[dict]:
    """The Gridlytics roles the list gives the company's people, set through the users route (api.users_edit) as the company's owner sets them: the owner's
    own member row first, as their first sign-in makes it, then each person's, made ahead of their first sign-in. A role already chosen in Gridlytics is
    kept. Each change is in the company's audit, "changed a user", by the seed for the owner."""
    # The app, for its users route and its context: imported only here, as nothing else of the seed needs it
    from . import api, companies
    wanted = [p for p in people if p["company"] == company["name"] and p["role"]]
    if not wanted:
        return []
    owner = next(p for p in people if p["company"] == company["name"] and p["company_role"] == "owner")
    owner_account = platformdb.account_by_email(owner["email"])
    # Only an owner who could sign in now sets roles: a run again keeps the founder's changes since (a company switched off, an account disabled, another
    # owner), and the owner's first sight below would otherwise pin an administrator the registry no longer has
    as_owner = platformdb.membership(owner_account["id"], company["id"])
    if not company["enabled"] or owner_account["disabled"] or as_owner is None or (as_owner["role"], as_owner["status"]) != ("owner", "active"):
        notes.append(f"The roles in {company['name']} were not set: {owner['name']} cannot sign in to it as its owner now")
        return []
    path = platformdb.company_file(company)
    done = []
    with store.use_company(path), store.use_org(store.DEFAULT_ORG):
        with companies._seat_lock:
            mine = store_members.member(owner_account["id"]) or companies._first_sight(owner_account["id"], owner_account["email"], owner_account["name"],
                                                                                         "owner", company)
        acting = {**store_members.as_user(mine), "name": f"{ACTOR}, for {owner_account['name']}"}
    for p in wanted:
        account = platformdb.account_by_email(p["email"])
        held = platformdb.membership(account["id"], company["id"])
        if held is None or held["status"] != "active" or account["disabled"]:
            notes.append(f"{p['name']}'s role in {company['name']} was not set: their {'account is disabled' if account['disabled'] else 'membership is not active'}")
            continue
        markers = api._enter(acting, store.DEFAULT_ORG, {"id": company["id"], "name": company["name"]}, path)
        try:
            found = store_members.member(account["id"])
            if found is not None and (found["role_source"] != "default" or found["role"] == p["role"]):
                if found["role"] != p["role"]:
                    notes.append(f"{p['name']}'s role in {company['name']} is {auth.ROLES[found['role']]}, chosen in Gridlytics: kept")
                continue
            try:
                made = api.users_edit(account["id"], None, {"role": p["role"]})
            except HTTPException as refused:
                notes.append(f"{p['name']}'s role in {company['name']} was not set: {refused.detail}")
                continue
        finally:
            api._leave(markers)
        done.append({"name": p["name"], "company": company["name"], "role": p["role"]})
        if made["disabled"] and made["disabled_reason"] == companies.NO_SEAT:
            notes.append(f"{p['name']} is {auth.ROLES[p['role']]} in {company['name']}, but every seat of {company['name']} is taken: their access is off "
                         "until the founder gives the company another seat")
    return done


# ------------------------------------------------------------------------------------------------ the seed

def _need_local() -> None:
    if identity.MODE != "local":
        raise Refused("The demo seed is for local mode, where this server keeps the accounts; with GRIDLYTICS_AUTH_MODE=supabase they are the "
                      "Gridlytics Supabase project's: make them in the founder console (/admin)")


def seed(companies=None, people=None, *, reset_passwords: bool = False, out: str | Path | None = None) -> dict:
    """Make the list (the decisions' demo list unless one is given): the companies, the accounts and memberships, the roles, the founder's login, and the
    accounts list. What it did, without a password."""
    _need_local()
    companies, people = check_list(DEMO_COMPANIES, DEMO_PEOPLE) if companies is None else check_list(companies, people)
    out = Path(out) if out is not None else accounts_file()
    for path in (out, env_file()):
        if not _ignored(path):
            raise Refused(f"{path} would be in the repository, and git does not ignore it: the passwords and the founder's settings go only to files git "
                          f"ignores, such as demo/{ACCOUNTS_NAME} and data/{ENV_NAME}")
    earlier = _earlier(out)
    made: dict[str, str] = {}                        # the passwords made in this run, by address
    done = {"companies": [], "accounts": [], "kept": [], "reset": [], "memberships": [], "roles": [], "notes": [], "out": out}
    founder_login = None
    try:
        rows = {c["name"].lower(): c for c in platformdb.list_companies()}
        for c in companies:
            mine = [p for p in people if p["company"] == c["name"]]
            if c["name"].lower() in rows:
                row = rows[c["name"].lower()]
                done["companies"].append({**row, "made": False})
                if not row["enabled"]:
                    done["notes"].append(f"Gridlytics is off for {row['name']} (the founder switched it off): switch it on in the founder console")
                continue
            owner = next(p for p in mine if p["company_role"] == "owner")
            account = platformdb.account_by_email(owner["email"])
            spec = {"user_id": account["id"]} if account else {"email": owner["email"], "name": owner["name"],
                                                              "password": made.setdefault(owner["email"], new_password())}
            seats = c["seats"] if c["seats"] is not None else max(platformdb.SEATS, len(mine))
            row, account = store_founder.create_company(c["name"], seats, True, spec)
            platformdb.founder_audit("made a company", row["name"], {"seats": seats, "enabled": True, "owner": account["email"],
                                                                    "new_owner": "user_id" not in spec}, actor=ACTOR)
            if "user_id" not in spec:
                done["accounts"].append(account["email"])
            rows[row["name"].lower()] = row
            done["companies"].append({**row, "made": True})
        for p in people:
            company = rows[p["company"].lower()]
            account = platformdb.account_by_email(p["email"])
            if account is None:
                account = store_founder.create_account(p["email"], p["name"], made.setdefault(p["email"], new_password()), company["id"], p["company_role"])
                platformdb.founder_audit("made an account", account["email"], {"company": company["name"], "company_role": p["company_role"]}, actor=ACTOR)
                done["accounts"].append(account["email"])
                continue
            if p["email"] not in made and p["email"] not in done["kept"]:
                done["kept"].append(p["email"])
                if reset_passwords:
                    store_founder.set_password(account["id"], made.setdefault(p["email"], new_password()))
                    platformdb.founder_audit("set a password", account["email"], actor=ACTOR)
                    done["reset"].append(account["email"])
                if account["disabled"]:
                    done["notes"].append(f"The account of {p['email']} is disabled (the founder's choice): kept so")
            if platformdb.membership(account["id"], company["id"]) is None:
                store_founder.add_membership(account["id"], company["id"], p["company_role"])
                platformdb.founder_audit("added a membership", account["email"], {"company": company["name"], "company_role": p["company_role"]},
                                         actor=ACTOR)
                store_founder.follow_membership(company["id"], account["id"], "active")
                done["memberships"].append((account["email"], company["name"]))
        founder_login = _founder_login(reset_passwords, earlier["founder"])
        for c in companies:
            done["roles"] += _set_roles(rows[c["name"].lower()], people, done["notes"])
    finally:
        if made or founder_login is not None:
            # Written whatever failed after the first password was made: a password that is in no list is lost
            passwords = {}
            for p in people:
                if p["email"] in made:
                    passwords[p["email"]] = made[p["email"]]
                elif p["email"] not in passwords:
                    known = earlier["people"].get(p["email"])
                    passwords[p["email"]] = known if known and platformdb.check_sign_in(p["email"], known) else None
            _write_private(out, _accounts_text(companies, people, passwords, founder_login))
    done["founder"] = {k: v for k, v in founder_login.items() if k in ("file", "username", "made", "reset")}
    done["unknown"] = [e for e, pw in passwords.items() if pw is None]
    return done


def _md(text: str) -> str:
    return text.replace("|", "\\|")


def _accounts_text(companies: list, people: list, passwords: dict, login: dict | None) -> str:
    """The accounts list (demo/DEMO_ACCOUNTS.local.md): every person's password, and the founder console's login."""
    def role(p: dict) -> str:
        if p["company_role"] == "owner":
            return "Owner (administrator)"
        return auth.ROLES[p["role"]] if p["role"] else "Viewer, until the owner chooses"

    lines = [
        "# Demo accounts",
        "",
        f"Made by `python -m gridlytics.seed_demo` on {datetime.now():%Y-%m-%d %H:%M} for the data folder `{platformdb.data_dir()}` (local mode).",
        "Git ignores this file: never commit it, and never paste a password from it into a chat, a ticket or an email.",
        "",
        "## Gridlytics",
        "",
        "Start the app (`python scripts/start_local.py` in the web repository, `gridlytics-frontend`, beside this one, on this data folder) and sign in with an",
        "address and its password. The company's owner chooses the others' roles under Settings, Users and audit.",
        "",
        "| Company | Name | Email | Password | Company role | Gridlytics role |",
        "|---|---|---|---|---|---|",
    ]
    for p in people:
        password = f"`{passwords[p['email']]}`" if passwords.get(p["email"]) else _UNKNOWN
        lines.append(f"| {_md(p['company'])} | {_md(p['name'])} | `{p['email']}` | {password} | {p['company_role']} | {role(p)} |")
    if login is not None:
        secret, env = login["secret"], login["file"]
        lines += [
            "",
            "## The founder console",
            "",
            "The console is the web app's `/admin` page (with the launcher, `http://127.0.0.1:5195/admin`, or port 5295 with `--scratch`). It signs in with the username, the password and the",
            "six-digit code of an authenticator app.",
            "",
            f"- **Username:** `{login['username']}`",
            f"- **Password:** `{login['password']}`" if login["password"] else f"- **Password:** {_UNKNOWN}",
            f"- **Code secret:** `{secret}` (Base32: type it into the authenticator app as a time-based key)",
            f"- **Authenticator link:** `{mfa.uri(secret, login['username'], f'{mfa.ISSUER} founder')}` (the same key, for an app that opens such a link)",
            "",
            f"The API reads the console's four settings from its environment only. They are in `{env}` (git-ignored, the values quoted). The web",
            "repository's launcher (`python scripts/start_local.py`) loads that file itself when it runs on this data folder (a setting already in its",
            "environment wins). To start the API another way, load the file into the environment of the process that starts it; in PowerShell:",
            "",
            # The path in PowerShell's single quotes, where a quote is written twice; the file is read, so no secret is typed where the shell's history keeps it
            f"    foreach ($l in Get-Content '{str(env).replace(chr(39), chr(39) * 2)}') {{ if ($l -match \"^(\\w+)='(.*)'$\") "
            "{ Set-Item \"env:$($Matches[1])\" $Matches[2] } }",
            "",
            "Until the four are set, the console says it is not set up on this server.",
            "",
            "**The current code without an app:** with the settings loaded as above, in `backend/`: `python -m gridlytics.founder code`. It reads the code",
            "secret from the environment; never type the secret itself on a command line, which the shell's history keeps.",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gridlytics.seed_demo", description=__doc__.split("\n\n")[0].replace("\n", " "))
    ap.add_argument("--reset-passwords", action="store_true", help="give every account of the list, and the founder, a new password")
    ap.add_argument("--from", dest="lists", nargs=2, metavar=("COMPANIES_CSV", "USERS_CSV"),
                    help="the owner's list instead of the demo's (templates: demo/companies.template.csv, demo/users.template.csv)")
    ap.add_argument("--out", help=f"where the accounts list goes (default demo/{ACCOUNTS_NAME}, or beside a data folder elsewhere)")
    ap.add_argument("--no-demo-sites", action="store_true", help="do not load the four twin sites into the OnyxLeaf Demo company")
    ap.add_argument("--demo-days", type=int, default=None, help="days of generated history per demo site (default 21)")
    args = ap.parse_args(argv)
    try:
        _need_local()                                    # before the owner's list is read: in supabase mode there is nothing to check it for
        companies, people = read_list(*args.lists) if args.lists else (None, None)
        done = seed(companies, people, reset_passwords=args.reset_passwords, out=args.out)
    except Refused as exc:
        print(f"Nothing was made. {exc}", file=sys.stderr)
        return 1
    print(f"Local mode; the data folder is {platformdb.data_dir()}")
    for c in done["companies"]:
        print(f"{c['name']}: {'made, with its owner' if c['made'] else 'kept as it was'}; Gridlytics {'on' if c['enabled'] else 'off'}, {c['seats']} seat(s)")
    print(f"Accounts: {len(done['accounts'])} made, {len(done['kept'])} kept"
          + (f", {len(done['reset'])} given a new password" if args.reset_passwords else "")
          + (f"; {len(done['memberships'])} added to a company" if done["memberships"] else ""))
    if done["roles"]:
        print("Roles set as the owner would set them: " + ", ".join(f"{r['name']} {auth.ROLES[r['role']]} ({r['company']})" for r in done["roles"]))
    login = done["founder"]
    print(f"The founder console's login ({login['username']}): {'made' if login['made'] else 'a new password (its sessions end)' if login['reset'] else 'kept'}")
    for note in done["notes"]:
        print(f"Note: {note}")
    if done["unknown"]:
        print(f"{len(done['unknown'])} kept password(s) are not in the list (changed since it was made): --reset-passwords makes new ones")
    print(f"The passwords and the founder's login are in {done['out']}, and the founder's four settings in {login['file']}: never commit either (in the")
    print("repository git ignores both). Load the settings into the environment of the process that starts the API, as the list says.")
    print("This data folder has accounts now, so the app asks everyone to sign in.")
    if not args.no_demo_sites and not args.lists:
        load_demo_sites(args.demo_days)
    return 0


def load_demo_sites(days: int | None = None) -> None:
    """The four twin sites in the OnyxLeaf Demo company, with their history (plan Part 9.6); kept when they are already there."""
    from . import demo, store
    row = next((c for c in platformdb.list_companies() if c["name"] == DEMO_COMPANIES[0]["name"]), None)
    if row is None:
        return
    with store.use_company(platformdb.company_file(row)):
        if demo.demo_sites():
            print("The demo sites are already in OnyxLeaf Demo: kept")
            return
        print("Building the demo sites' history through the forecasters, the optimiser and the savings engine (a few minutes)...")
        st = demo.load(ACTOR, days, background=False)
        print(f"Demo sites: {st.get('state')}: {st.get('message')}")


if __name__ == "__main__":
    sys.exit(main())
