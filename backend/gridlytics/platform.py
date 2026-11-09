"""The platform's registry, `platform.db`: companies, the accounts of local sign-in, memberships, refresh tokens, the founder's audit and the record of sign-ins
(the owner's decisions of 24 October 2026, sections 1, 2 and 5; stage P1 of the sign-in and split).

One server keeps a database per company (`companies/<id>.db`, a full workspace each) and this one registry beside them. The data folder is GRIDLYTICS_DATA_DIR
when it is set, and otherwise the folder of `store.DB_PATH`, read at every call: a test or a scratch run that moves the workspace's file moves the registry
with it, so two workspace files never share accounts.

The registry is a file of its own, never opened through `store._connect()`, which would give it the workspace's schema and the workspace's backups. It
follows the same rules: a schema version in SQLite's user_version, a copy in `backup/` before a migration, WAL set once, one migration at a time.

In local mode (GRIDLYTICS_AUTH_MODE, the default) the backend is the sign-in service. An account is an email, a name and a password kept as auth.py's
PBKDF2 hash; a membership gives it a company role (owner, admin or member) and a status (pending, active or suspended) in one company; the founder switches
Gridlytics on or off per company. A session is a one-hour access token (identity.issue_local) and a refresh token, kept only as its hash, replaced at every
use, ending 12 hours unused and 7 days after the sign-in (auth.SESSION_HOURS, auth.SESSION_DAYS).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import auth, store

SCHEMA_VERSION = 1
#: An access token's life (Supabase's default, decisions-v2 section 1)
ACCESS_MINUTES = 60
#: A refresh token not used for this long has ended; a session ends this many days after its sign-in, however used (the cookie sessions' rules)
REFRESH_HOURS = auth.SESSION_HOURS
SESSION_DAYS = auth.SESSION_DAYS
#: A refresh token used again within these seconds of its first use is two tabs refreshing at once: refused, and the session goes on. Later it was copied,
#: and the whole session ends (OAuth 2.0 Security Best Current Practice, RFC 9700 section 4.14.2 [recalled]; Supabase's reuse interval is also 10 s)
REUSE_SECONDS = 10
COMPANY_ROLES = ("owner", "admin", "member")
STATUSES = ("pending", "active", "suspended")
#: Seats a company is made with until the founder sets its own
SEATS = 5
#: The tables that name the company of a share link's or an API key's hash, so that a request carrying one opens that company's file and no other
INDEX_TABLES = ("share_index", "api_key_index")
#: The record's words for a refused and a good sign-in: the lockout counts the first, from the last of the second
REFUSED = "sign-in refused"
SIGNED_IN = "signed in"
#: The shape of an email address, as the workspace's users have it, but with a domain that may be a single name: the decisions' demo user is dev@localhost
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s.][^@\s]{0,189}")
_lock = threading.Lock()
_clock = time.time


# ------------------------------------------------------------------------------------------------ where the files are (addendum item 1)

def data_dir() -> Path:
    """The data folder: GRIDLYTICS_DATA_DIR, else the folder of the workspace's default database (`store.DB_PATH`) as it is at this call."""
    configured = os.environ.get("GRIDLYTICS_DATA_DIR", "").strip()
    return Path(configured) if configured else Path(store.DB_PATH).parent


def db_path() -> Path:
    return data_dir() / "platform.db"


def companies_dir() -> Path:
    """Where a company's database is made unless the registry names another file for it."""
    return data_dir() / "companies"


def secret_file() -> Path:
    """Where local mode keeps the access tokens' signing key when GRIDLYTICS_LOCAL_JWT_SECRET is not set (git-ignored)."""
    return data_dir() / "platform.secret"


def _now() -> str:
    return datetime.fromtimestamp(_clock(), timezone.utc).isoformat(timespec="seconds")


def backup(tag: str) -> Path | None:
    """A copy of the registry in `backup/` beside it, named by date and `tag`; None when there is no registry yet. The daily backup calls it too: losing
    the registry would lose every account and membership."""
    path = db_path()
    if not path.exists() or path.stat().st_size == 0:
        return None
    folder = path.parent / "backup"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{path.stem}-{datetime.now().strftime('%Y-%m-%d-%H%M%S-%f')}-{tag}.db"
    # SQLite's own backup, which is consistent even while another connection writes
    with closing(sqlite3.connect(path)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    return target


def _migrate(con: sqlite3.Connection, version: int) -> None:
    if version < 1:
        con.execute("""CREATE TABLE IF NOT EXISTS companies (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, seats INTEGER NOT NULL DEFAULT 0 CHECK (seats >= 0),
            disabled_reason TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, db_file TEXT NOT NULL)""")
        con.execute("""CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, password TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0,
            created TEXT NOT NULL, updated TEXT NOT NULL, sessions_from INTEGER NOT NULL DEFAULT 0)""")
        con.execute("""CREATE TABLE IF NOT EXISTS memberships (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES accounts(id), company_id TEXT NOT NULL REFERENCES companies(id),
            role TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL, activated TEXT, UNIQUE (user_id, company_id))""")
        con.execute("CREATE INDEX IF NOT EXISTS memberships_company ON memberships(company_id)")
        con.execute("""CREATE TABLE IF NOT EXISTS refresh_tokens (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, family TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
            session_ends REAL NOT NULL, used REAL, revoked INTEGER NOT NULL DEFAULT 0)""")
        con.execute("CREATE INDEX IF NOT EXISTS refresh_tokens_user ON refresh_tokens(user_id)")
        con.execute("CREATE INDEX IF NOT EXISTS refresh_tokens_family ON refresh_tokens(family)")
        con.execute("""CREATE TABLE IF NOT EXISTS founder_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '{}')""")
        # Sign-ins, refused and good, and the account's own changes: the lockout's count (addendum item 13), never a company's audit
        con.execute("""CREATE TABLE IF NOT EXISTS account_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, email TEXT NOT NULL, address TEXT NOT NULL DEFAULT '', event TEXT NOT NULL,
            account_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}')""")
        con.execute("CREATE INDEX IF NOT EXISTS account_events_email ON account_events(email, event, at)")
        for table in INDEX_TABLES:
            con.execute(f"CREATE TABLE IF NOT EXISTS {table} (token_hash TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id))")
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _connect() -> sqlite3.Connection:
    """A connection to the registry at its current schema, made on first use, with a copy before any migration of a file that holds something."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    if con.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        with _lock:
            con.execute("PRAGMA journal_mode=WAL")
    if con.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return con
    with _lock:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version < SCHEMA_VERSION:
            if con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] > 0:
                backup(f"schema-v{version}")
            con.isolation_level = None
            con.execute("BEGIN IMMEDIATE")              # a second process waits here
            try:
                version = con.execute("PRAGMA user_version").fetchone()[0]
                if version < SCHEMA_VERSION:
                    _migrate(con, version)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            con.isolation_level = ""
    return con


@contextmanager
def _write():
    """A connection whose block is one write transaction, taken at once (BEGIN IMMEDIATE): a read and the write it decides happen together, and an error
    inside the block leaves nothing written."""
    con = _connect()
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    finally:
        con.close()


def _read():
    return closing(_connect())


# ------------------------------------------------------------------------------------------------ companies (decisions-v2 sections 2 and 5)

def _company(r) -> dict | None:
    if r is None:
        return None
    return {"id": r["id"], "name": r["name"], "enabled": bool(r["enabled"]), "seats": r["seats"], "disabled_reason": r["disabled_reason"],
            "created": r["created"], "db_file": r["db_file"]}


def _name(name, what: str = "name") -> str:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError(f"Give a {what} of 1 to 80 characters")
    return name.strip()


def _seats(seats) -> int:
    if isinstance(seats, bool) or not isinstance(seats, int) or not 0 <= seats <= 100_000:
        raise ValueError("Seats must be a whole number from 0")
    return seats


def create_company(name: str, *, enabled: bool = False, seats: int = SEATS, db_file: str | Path | None = None, company_id: str | None = None) -> dict:
    """A company, with Gridlytics off until the founder switches it on. Its database is `companies/<id>.db` in the data folder, worked out at each use, unless
    `db_file` names another file by an absolute path (a test's or the browser check's workspace, addendum item 3)."""
    name = _name(name)
    if not isinstance(enabled, bool):
        raise ValueError("Enabled must be yes or no")
    if db_file is not None and not Path(db_file).is_absolute():
        raise ValueError("A company's database file is named by an absolute path")
    company_id = str(uuid.UUID(company_id)) if company_id else str(uuid.uuid4())
    with _write() as con:
        if con.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone():
            raise ValueError("A company with that id exists")
        con.execute("INSERT INTO companies (id, name, enabled, seats, disabled_reason, created, db_file) VALUES (?,?,?,?,?,?,?)",
                    (company_id, name, int(enabled), _seats(seats), "", _now(), str(db_file) if db_file is not None else ""))
    return company(company_id)


def company(company_id: str) -> dict | None:
    with _read() as con:
        return _company(con.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone())


def list_companies() -> list[dict]:
    with _read() as con:
        return [_company(r) for r in con.execute("SELECT * FROM companies ORDER BY name COLLATE NOCASE, created")]


def update_company(company_id: str, *, name: str | None = None, enabled: bool | None = None, seats: int | None = None,
                   disabled_reason: str | None = None) -> dict | None:
    """Rename a company, switch Gridlytics on or off for it, set its seats or say why it is off; None when there is no such company."""
    fields = {}
    if name is not None:
        fields["name"] = _name(name)
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError("Enabled must be yes or no")
        fields["enabled"] = int(enabled)
    if seats is not None:
        fields["seats"] = _seats(seats)
    if disabled_reason is not None:
        if not isinstance(disabled_reason, str) or len(disabled_reason) > 200:
            raise ValueError("The reason must be text of at most 200 characters")
        fields["disabled_reason"] = disabled_reason.strip()
    if fields:
        with _write() as con:
            con.execute(f"UPDATE companies SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), company_id))
    return company(company_id)


def company_file(which: str | dict) -> Path | None:
    """The database file of a company (its id, or its row): the file the registry names, or `companies/<id>.db` in the data folder now."""
    row = which if isinstance(which, dict) else company(which)
    if row is None:
        return None
    return Path(row["db_file"]) if row["db_file"] else companies_dir() / f"{row['id']}.db"


def company_files() -> list[Path]:
    """The database files of the companies Gridlytics is on for: what the background loops (the daily backup) go through."""
    return [company_file(c) for c in list_companies() if c["enabled"]]


# ------------------------------------------------------------------------------------------------ accounts (local mode)

def _account(r) -> dict | None:
    if r is None:
        return None
    return {"id": r["id"], "email": r["email"], "name": r["name"], "disabled": bool(r["disabled"]), "created": r["created"], "updated": r["updated"],
            "sessions_from": r["sessions_from"]}


def _email(email) -> str:
    if not isinstance(email, str) or not _EMAIL.fullmatch(email.strip()):
        raise ValueError("Give an email address")
    return email.strip().lower()


def password_problem(password) -> str | None:
    """auth.py's rule of every password, and not only spaces (review R-237: ten spaces passed)."""
    problem = auth.password_problem(password)
    if problem is None and not password.strip():
        return "A password cannot be only spaces"
    return problem


def count_accounts() -> int:
    """How many accounts there are; 0 without making the registry when there is none yet (open mode lasts until the first account, addendum item 6)."""
    if not db_path().exists():
        return 0
    with _read() as con:
        return con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


def create_account(email: str, name: str, password: str, *, account_id: str | None = None) -> dict:
    """An account of local sign-in, its password hashed by auth.py. Raises ValueError for a bad field or an email that has an account."""
    email, name = _email(email), _name(name)
    if (problem := password_problem(password)):
        raise ValueError(problem)
    account_id = str(uuid.UUID(account_id)) if account_id else str(uuid.uuid4())
    now = _now()
    hashed = auth.hash_password(password)
    with _write() as con:
        if con.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
            raise ValueError("That email address already has an account")
        if con.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone():
            raise ValueError("An account with that id exists")
        con.execute("INSERT INTO accounts (id, email, name, password, disabled, created, updated) VALUES (?,?,?,?,0,?,?)",
                    (account_id, email, name, hashed, now, now))
    return account(account_id)


def account(account_id: str) -> dict | None:
    with _read() as con:
        return _account(con.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone())


def account_by_email(email: str) -> dict | None:
    if not isinstance(email, str):
        return None
    with _read() as con:
        return _account(con.execute("SELECT * FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone())


def check_sign_in(email: str, password: str) -> dict | None:
    """The account an email and a password sign in to: None when there is none, it is disabled, or the password is wrong."""
    if not isinstance(email, str) or not isinstance(password, str):
        return None
    with _read() as con:
        r = con.execute("SELECT * FROM accounts WHERE email = ?", (email.strip().lower(),)).fetchone()
    # The hash is checked first, so that a disabled account takes as long to refuse as a wrong password
    if r is None or not auth.check_password(password, r["password"]) or r["disabled"]:
        return None
    return _account(r)


def signed_in(account_id: str, iat: int | None = None) -> dict | None:
    """The account an access token names, while it may act: it exists, it is not disabled, and the token (`iat`, its issue time in unix seconds) is not
    older than the account's last password change or disabling. Local revocation is immediate (addendum item 17): stage B3 asks this on every request."""
    found = account(account_id) if isinstance(account_id, str) else None
    if found is None or found["disabled"]:
        return None
    if iat is not None and (not isinstance(iat, (int, float)) or iat < found["sessions_from"]):
        return None
    return found


def set_password(account_id: str, password: str) -> dict | None:
    """A new password for an account (the founder's reset, or the person's own change): every session of the account ends, its refresh tokens and the
    access tokens made before now."""
    if (problem := password_problem(password)):
        raise ValueError(problem)
    with _write() as con:
        con.execute("UPDATE accounts SET password = ?, updated = ?, sessions_from = ? WHERE id = ?",
                    (auth.hash_password(password), _now(), int(_clock()), account_id))
        con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (account_id,))
    return account(account_id)


def set_account_disabled(account_id: str, disabled: bool) -> dict | None:
    """Disable an account, which ends its sessions at once, or enable it again (its ended sessions stay ended)."""
    if not isinstance(disabled, bool):
        raise ValueError("Disabled must be yes or no")
    with _write() as con:
        if disabled:
            con.execute("UPDATE accounts SET disabled = 1, updated = ?, sessions_from = ? WHERE id = ?", (_now(), int(_clock()), account_id))
            con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (account_id,))
        else:
            con.execute("UPDATE accounts SET disabled = 0, updated = ? WHERE id = ?", (_now(), account_id))
    return account(account_id)


def rename_account(account_id: str, name: str) -> dict | None:
    name = _name(name)
    with _write() as con:
        con.execute("UPDATE accounts SET name = ?, updated = ? WHERE id = ?", (name, _now(), account_id))
    return account(account_id)


# ------------------------------------------------------------------------------------------------ memberships

def _membership(r) -> dict:
    return {"id": r["id"], "user_id": r["user_id"], "company_id": r["company_id"], "role": r["role"], "status": r["status"], "created_at": r["created"],
            "activated_at": r["activated"],
            "company": {"id": r["company_id"], "name": r["company_name"], "verified": True, "enabled": bool(r["company_enabled"]), "seat_limit": r["company_seats"]}}


_MEMBERSHIPS = ("SELECT m.*, c.name AS company_name, c.enabled AS company_enabled, c.seats AS company_seats "
                "FROM memberships m JOIN companies c ON c.id = m.company_id")


def _role_status(role, status) -> None:
    if role is not None and role not in COMPANY_ROLES:
        raise ValueError(f"A company role is one of {', '.join(COMPANY_ROLES)}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"A membership's status is one of {', '.join(STATUSES)}")


def add_membership(user_id: str, company_id: str, role: str = "member", status: str = "active") -> dict:
    """An account joins a company with a company role. Raises ValueError for an unknown account or company, or a second membership of one company."""
    if role is None or status is None:
        raise ValueError("A membership has a company role and a status")
    _role_status(role, status)
    now = _now()
    with _write() as con:
        if con.execute("SELECT 1 FROM accounts WHERE id = ?", (user_id,)).fetchone() is None:
            raise ValueError("No such account")
        if con.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
            raise ValueError("No such company")
        if con.execute("SELECT 1 FROM memberships WHERE user_id = ? AND company_id = ?", (user_id, company_id)).fetchone():
            raise ValueError("That account is already a member of that company")
        con.execute("INSERT INTO memberships (id, user_id, company_id, role, status, created, activated) VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), user_id, company_id, role, status, now, now if status == "active" else None))
    return membership(user_id, company_id)


def set_membership(user_id: str, company_id: str, *, role: str | None = None, status: str | None = None) -> dict | None:
    """Change a membership's company role or status; None when there is no such membership. It is active from the first time it is made active."""
    _role_status(role, status)
    with _write() as con:
        if role is not None:
            con.execute("UPDATE memberships SET role = ? WHERE user_id = ? AND company_id = ?", (role, user_id, company_id))
        if status is not None:
            con.execute("UPDATE memberships SET status = ?, activated = COALESCE(activated, CASE WHEN ? = 'active' THEN ? END) "
                        "WHERE user_id = ? AND company_id = ?", (status, status, _now(), user_id, company_id))
    return membership(user_id, company_id)


def remove_membership(user_id: str, company_id: str) -> bool:
    with _write() as con:
        return con.execute("DELETE FROM memberships WHERE user_id = ? AND company_id = ?", (user_id, company_id)).rowcount > 0


def membership(user_id: str, company_id: str) -> dict | None:
    """One membership, with its company as the context gives it: whether Gridlytics is on for it is the company's `enabled`."""
    with _read() as con:
        r = con.execute(f"{_MEMBERSHIPS} WHERE m.user_id = ? AND m.company_id = ?", (user_id, company_id)).fetchone()
    return _membership(r) if r else None


def memberships(user_id: str) -> list[dict]:
    with _read() as con:
        return [_membership(r) for r in con.execute(f"{_MEMBERSHIPS} WHERE m.user_id = ? ORDER BY c.name COLLATE NOCASE, m.created", (user_id,))]


def company_members(company_id: str) -> list[dict]:
    """A company's memberships, each with its account's email and name."""
    with _read() as con:
        rows = con.execute("SELECT m.*, c.name AS company_name, c.enabled AS company_enabled, c.seats AS company_seats, a.email, a.name, a.disabled "
                           "FROM memberships m JOIN companies c ON c.id = m.company_id JOIN accounts a ON a.id = m.user_id "
                           "WHERE m.company_id = ? ORDER BY a.name COLLATE NOCASE", (company_id,)).fetchall()
    return [{**_membership(r), "email": r["email"], "name": r["name"], "disabled": bool(r["disabled"])} for r in rows]


def context(user_id: str) -> dict | None:
    """What Supabase's `get_my_context` gives a signed-in person, from the registry: the profile, every membership with its company, and no pending
    invitations (people are added by the founder, never invited). Every company the founder made is `verified`; `enabled` says whether Gridlytics is on
    for it. None for an account that does not exist or is disabled."""
    found = signed_in(user_id)
    if found is None:
        return None
    first, *last = found["name"].split(None, 1)
    profile = {"id": found["id"], "email": found["email"], "name": found["name"], "first_name": first, "last_name": last[0] if last else None, "avatar_url": None,
               "is_super_admin": False, "assignment_status": "none", "requested_company_name": None, "created_at": found["created"],
               "updated_at": found["updated"]}
    return {"profile": profile, "memberships": [{k: v for k, v in m.items() if k not in ("user_id", "company_id")} for m in memberships(user_id)],
            "pending_invites_for_email": []}


# ------------------------------------------------------------------------------------------------ refresh tokens

class Renewed(Exception):
    """A refresh token used again within REUSE_SECONDS of its first use: another window of the same session renewed it a moment ago. Nothing ends; the
    window that sent it takes the session the other one stored."""


def _add_refresh(con: sqlite3.Connection, user_id: str, family: str, session_ends: float) -> str:
    token = secrets.token_urlsafe(32)
    now = _clock()
    con.execute("INSERT INTO refresh_tokens (token_hash, user_id, family, created, expires, session_ends) VALUES (?,?,?,?,?,?)",
                (auth.token_hash(token), user_id, family, now, min(now + REFRESH_HOURS * 3600, session_ends), session_ends))
    return token


def new_refresh(user_id: str) -> str:
    """The refresh token of a new session of an account (a sign-in), kept as its hash. The tokens of sessions past their last day go: nothing can renew
    them, so the table holds a week of sessions, not every one ever made."""
    with _write() as con:
        con.execute("DELETE FROM refresh_tokens WHERE session_ends < ?", (_clock(),))
        return _add_refresh(con, user_id, uuid.uuid4().hex, _clock() + SESSION_DAYS * 86400)


def rotate_refresh(token: str) -> tuple[dict, str] | None:
    """Use a refresh token: the account and the token that replaces it, or None when it is unknown, used, revoked or ended, or its account may no longer
    sign in. Of two uses of one token, one wins; the other, within REUSE_SECONDS, raises Renewed and the session goes on. A token used again later ends
    its whole session, which is recorded."""
    if not isinstance(token, str) or not token:
        return None
    now = _clock()
    raced = False
    with _write() as con:
        r = con.execute("SELECT * FROM refresh_tokens WHERE token_hash = ?", (auth.token_hash(token),)).fetchone()
        if r is None or r["revoked"]:
            return None
        if r["used"] is not None:
            raced = now - r["used"] <= REUSE_SECONDS
            if not raced:
                con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE family = ?", (r["family"],))
                owner = con.execute("SELECT email FROM accounts WHERE id = ?", (r["user_id"],)).fetchone()
                con.execute("INSERT INTO account_events (at, email, event, account_id) VALUES (?,?,?,?)",
                            (now, owner["email"] if owner else "", "refresh token used again", r["user_id"]))
        elif now < r["expires"] and now < r["session_ends"]:
            a = con.execute("SELECT * FROM accounts WHERE id = ?", (r["user_id"],)).fetchone()
            if a is not None and not a["disabled"]:
                con.execute("UPDATE refresh_tokens SET used = ? WHERE token_hash = ?", (now, r["token_hash"]))
                return _account(a), _add_refresh(con, a["id"], r["family"], r["session_ends"])
    if raced:
        raise Renewed()
    return None


def end_session(token: str) -> bool:
    """Sign out: the session the refresh token belongs to ends, every token of it."""
    if not isinstance(token, str) or not token:
        return False
    with _write() as con:
        r = con.execute("SELECT family FROM refresh_tokens WHERE token_hash = ?", (auth.token_hash(token),)).fetchone()
        return bool(r) and con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE family = ?", (r["family"],)).rowcount > 0


def end_sessions(user_id: str) -> int:
    """Every session of an account ends (its refresh tokens; the access tokens end with `set_password` or `set_account_disabled`)."""
    with _write() as con:
        return con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ? AND revoked = 0", (user_id,)).rowcount


# ------------------------------------------------------------------------------------------------ the lockout (addendum item 13; review R-235)

def client_address(request) -> str:
    """The address a request counts under for the lockout. A proxy's X-Forwarded-For or Forwarded header is believed only where the server says it runs
    behind a proxy it trusts (GRIDLYTICS_TRUST_PROXY=1, with uvicorn's --forwarded-allow-ips naming it): otherwise uvicorn's default takes the header from
    any client on this machine, and a new address per try bought five more (R-235). A request that carries such a header untrusted counts as "forwarded"."""
    host = request.client.host if request.client else ""
    if os.environ.get("GRIDLYTICS_TRUST_PROXY") != "1" and (request.headers.get("x-forwarded-for") or request.headers.get("forwarded")):
        return "forwarded"
    return host


def record(event: str, email: str, address: str = "", account_id: str = "", detail: dict | None = None) -> None:
    """One line of the record of sign-ins: a refusal the lockout counts, a sign-in that clears the count, or an account's own change."""
    with _write() as con:
        con.execute("INSERT INTO account_events (at, email, address, event, account_id, detail) VALUES (?,?,?,?,?,?)",
                    (_clock(), (email or "").strip().lower(), address or "", event, account_id or "", json.dumps(detail or {})))


def events(email: str, limit: int = 200) -> list[dict]:
    """An account's record, oldest first."""
    with _read() as con:
        rows = con.execute("SELECT * FROM (SELECT * FROM account_events WHERE email = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                           ((email or "").strip().lower(), limit)).fetchall()
    return [{"at": r["at"], "email": r["email"], "address": r["address"], "event": r["event"], "account_id": r["account_id"],
             "detail": json.loads(r["detail"])} for r in rows]


def lock_wait(email: str, address: str | None, refused: str = REFUSED, cleared: str = SIGNED_IN, tries: int | None = None) -> int:
    """Seconds before an account may be tried again from an address (0 when it may now). `tries` refusals within LOCK_MINUTES stop it for LOCK_MINUTES from
    the last of them; a good sign-in from the address clears the count (R-235: the count outlived a success, and the wait ran from each new try). With
    `address` None, the refusals from every address together (the founder's code step, R-271)."""
    if not email or not db_path().exists():
        return 0
    tries = tries or auth.MAX_TRIES
    window = auth.LOCK_MINUTES * 60
    where, args = ("email = ?", [email.strip().lower()]) if address is None else ("email = ? AND address = ?", [email.strip().lower(), address])
    with _read() as con:
        last_good = con.execute(f"SELECT MAX(at) FROM account_events WHERE {where} AND event = ?", (*args, cleared)).fetchone()[0] or 0
        times = [r[0] for r in con.execute(f"SELECT at FROM account_events WHERE {where} AND event = ? AND at > ? ORDER BY at DESC LIMIT ?",
                                           (*args, refused, last_good, tries))]
    left = times[0] + window - _clock() if len(times) == tries and times[0] - times[-1] < window else 0
    return max(1, round(left)) if left > 0 else 0


# ------------------------------------------------------------------------------------------------ the founder's audit (decisions-v2 section 4)

def founder_audit(action: str, target: str = "", detail: dict | None = None, actor: str = "founder") -> None:
    with _write() as con:
        con.execute("INSERT INTO founder_audit (at, actor, action, target, detail) VALUES (?,?,?,?,?)", (_now(), actor, action, target, json.dumps(detail or {})))


def list_founder_audit(limit: int = 300, offset: int = 0) -> list[dict]:
    with _read() as con:
        rows = con.execute("SELECT at, actor, action, target, detail FROM founder_audit ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


# ------------------------------------------------------------------------------------------------ which company a share link or an API key is of (addendum item 7)

def _index(table: str) -> str:
    if table not in INDEX_TABLES:
        raise ValueError(f"No tokens are indexed in {table}")
    return table


def index_add(table: str, token_hash: str, company_id: str) -> None:
    """Say which company holds the share link or API key of this hash, where it is made (and when a workspace is linked to its company). A hash names one
    company: naming another raises ValueError."""
    table = _index(table)
    with _write() as con:
        if con.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
            raise ValueError("No such company")
        held = con.execute(f"SELECT company_id FROM {table} WHERE token_hash = ?", (token_hash,)).fetchone()
        if held is not None and held["company_id"] != company_id:
            raise ValueError("That token is another company's")
        con.execute(f"INSERT OR IGNORE INTO {table} (token_hash, company_id) VALUES (?,?)", (token_hash, company_id))


def index_find(table: str, token_hash: str) -> str | None:
    """The company of a share link's or an API key's hash; None when none is known. The guard opens that company's file, and no other is ever searched."""
    table = _index(table)
    if not db_path().exists():
        return None
    with _read() as con:
        r = con.execute(f"SELECT company_id FROM {table} WHERE token_hash = ?", (token_hash,)).fetchone()
    return r["company_id"] if r else None


def index_remove(table: str, token_hash: str) -> bool:
    """Forget a hash, where its link or key is revoked."""
    table = _index(table)
    with _write() as con:
        return con.execute(f"DELETE FROM {table} WHERE token_hash = ?", (token_hash,)).rowcount > 0
