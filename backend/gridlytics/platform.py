"""The platform's registry, `platform.db`: companies, the accounts of local sign-in, memberships, refresh tokens, the founder's audit and the record of sign-ins
(the owner's decisions of 24 October 2026, sections 1, 2 and 5; stage P1 of the sign-in and split), and the access requests sent to the founder from the
contact page (schema 4; access_requests.py; UX backend asks K2 and K3); schema 5 keeps the company a line of the founder's audit is of (UX K34).

One server keeps a database per company (`companies/<id>.db`, a full workspace each) and this one registry beside them. The data folder is GRIDLYTICS_DATA_DIR
when it is set, and otherwise the folder of `store.DB_PATH`, read at every call: a test or a scratch run that moves the workspace's file moves the registry
with it, so two workspace files never share accounts.

The registry is a file of its own, never opened through `store._connect()`, which would give it the workspace's schema and the workspace's backups. It
follows the same rules: a schema version in SQLite's user_version, a copy in `backup/` before a migration, WAL set once, one migration at a time.

In local mode (GRIDLYTICS_AUTH_MODE, the default) the backend is the sign-in service. An account is an email, a name and a password kept as auth.py's
PBKDF2 hash; a membership gives it a company role (owner, admin or member) and a status (pending, active or suspended) in one company; the founder switches
Gridlytics on or off per company. A session is a one-hour access token (identity.issue_local) and a refresh token, kept only as its hash, replaced at every
use, ending 12 hours unused and 7 days after the sign-in (auth.SESSION_HOURS, auth.SESSION_DAYS).

Until PLT-11 brings email (plan Part 9.3), a person who forgot their password is helped by a company's administrator or the founder: they issue a one-time
password (`set_password(..., one_time=True)`, the account's `must_change`), with which only the sign-in's own paths answer until the person chooses their
own, and they clear a sign-in lockout (`clear_lock`).
"""

from __future__ import annotations

import json
import logging
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

from . import auth, identity, store

SCHEMA_VERSION = 5
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
#: The record's word for a lockout an administrator or the founder cleared (plan Part 9.3; audit pages-A-02): the refusals before it stop counting, from
#: every address
LOCK_CLEARED = "lockout cleared"
#: What a person signed in with a one-time password is told by every path but the sign-in's own (api.IDENTITY_PATHS), until they choose their own
ONE_TIME = "You signed in with a one-time password: choose your own password first, under My account"
#: The shape of an email address, as the workspace's users have it, but with a domain that may be a single name: the decisions' demo user is dev@localhost
_EMAIL = re.compile(r"[^@\s]{1,64}@[^@\s.][^@\s]{0,189}")
_lock = threading.Lock()
_clock = time.time
#: Whether the log has said that a proxy's header arrived untrusted (said once a process: client_address)
_proxy_said = False


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
    if version < 2:
        # a one-time password an administrator or the founder issued (plan Part 9.3; audit pages-A-02): the person chooses their own at the next sign-in.
        # _connect() has copied the file to backup/ before this runs
        con.execute("ALTER TABLE accounts ADD COLUMN must_change INTEGER NOT NULL DEFAULT 0")
    if version < 3:
        # where and when a session started, carried from token to token (UI-18 My account's sessions; audit role-analyst-32): the address and the browser
        # of the sign-in, never shown to anyone but the person
        for column, kind in (("started", "REAL"), ("address", "TEXT NOT NULL DEFAULT ''"), ("agent", "TEXT NOT NULL DEFAULT ''")):
            con.execute(f"ALTER TABLE refresh_tokens ADD COLUMN {column} {kind}")
    if version < 4:
        # the access requests a visitor sends from the contact page to the founder (UX backend asks K2 and K3; plan B4, B20.4, B21.2): the platform's,
        # never a company's file, and never an account. Each is kept KEEP_DAYS (access_requests.py) from when it came. _connect() has copied the file
        con.execute("""CREATE TABLE IF NOT EXISTS access_requests (
            id TEXT PRIMARY KEY, received_at TEXT NOT NULL, topic TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL,
            company TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '', sites TEXT, equipment TEXT NOT NULL DEFAULT '[]',
            inverter TEXT, disco TEXT, message TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new', 'contacted', 'done', 'declined')), note TEXT NOT NULL DEFAULT '', updated_at TEXT,
            sent_by TEXT)""")
        con.execute("CREATE INDEX IF NOT EXISTS access_requests_received ON access_requests(received_at)")
        con.execute("CREATE INDEX IF NOT EXISTS access_requests_state ON access_requests(state, received_at)")
    if version < 5:
        # the company a line of the founder's audit is of, for the console's Company filter (UX backend ask K34; plan G4.5, G9 item 3): given where a
        # line is written with one, and filled here for the lines before it from the company's name in them (_backfill_audit_companies).
        # _connect() has copied the file to backup/ before this runs
        if "company_id" not in {r[1] for r in con.execute("PRAGMA table_info(founder_audit)")}:
            con.execute("ALTER TABLE founder_audit ADD COLUMN company_id TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS founder_audit_company ON founder_audit(company_id, id)")
        _backfill_audit_companies(con)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


#: The founder's audit lines whose target is a company's name (the rest name it in their detail's `company`, if at all)
_COMPANY_TARGETS = ("made a company", "changed a company", "switched Gridlytics on", "switched Gridlytics off", "archived a company",
                    "brought a company back from the archive", "made a backup")


def _backfill_audit_companies(con: sqlite3.Connection) -> None:
    """The company of each founder's audit line written before lines kept it (schema 5): the company named by the line (its target for a company's own
    lines and a backup's, else its detail's `company`), by every name a company has held: its name now and the names it had before a rename (a
    "changed a company" line's name from and to, read newest first so a chain of renames is followed). Only a name one company alone has held is
    filled: a name two companies share now, one a company gave up and another took later (UX K34 check: the first company's lines were given to
    the later one), or one no company has, leaves the line of no company, listed unfiltered only."""
    held: dict[str, set] = {}                      # each name, and every company that held it (None: a company not known)
    now: dict[str, str | None] = {}                # the company that holds each name, back to the rename being read (None: two, or not known)
    for r in con.execute("SELECT id, name FROM companies").fetchall():
        held.setdefault(r["name"], set()).add(r["id"])
        now[r["name"]] = None if r["name"] in now else r["id"]
    lines = con.execute("SELECT id, action, target, detail FROM founder_audit ORDER BY id DESC").fetchall()
    for r in lines:                                # newest first: a rename's new name is known before its old one
        if r["action"] != "changed a company":
            continue
        try:
            change = (json.loads(r["detail"] or "{}") or {}).get("name") or {}
        except (ValueError, AttributeError):
            continue
        before, after = (change.get("from"), change.get("to")) if isinstance(change, dict) else (None, None)
        if not (isinstance(before, str) and isinstance(after, str)) or before == after:
            continue
        who = now.get(after)                       # the company that took `after` in this rename
        if who is not None:
            del now[after]                         # before the rename it held `before`, and nobody `after`
        held.setdefault(before, set()).add(who)
        now[before] = None if before in now else who
    names = {name: next(iter(ids)) for name, ids in held.items() if len(ids) == 1 and None not in ids}
    for r in lines:
        try:
            detail = json.loads(r["detail"] or "{}")
        except ValueError:
            detail = {}
        name = r["target"] if r["action"] in _COMPANY_TARGETS else (detail.get("company") if isinstance(detail, dict) else None)
        if isinstance(name, str) and name in names:
            con.execute("UPDATE founder_audit SET company_id = ? WHERE id = ?", (names[name], r["id"]))


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
            "sessions_from": r["sessions_from"], "must_change": bool(r["must_change"])}


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


def signed_in(account_id: str, iat: int | None = None, *, session: str | None = None, one_time_ok: bool = False) -> dict | None:
    """The account an access token names, while it may act: it exists, it is not disabled, and the token is not older than the account's last password
    change or disabling (`sessions_from`, to a fraction of a second). Local revocation is immediate (addendum item 17): stage B3 asks this on every request.
    A token of a known session (`session`, its `session_id`) is as old as the session, which started when its sign-in was made (to a fraction of a second); one
    naming no session is told by `iat`, its issue time in whole seconds, and is refused when it falls in the second of the change, as it may be older (a
    token of the same second passed before: audit parity-supabase-auth-01).
    An account signed in with a one-time password (`must_change`, plan Part 9.3) is refused 403 `password_change_required` (identity.Refused) unless
    `one_time_ok`: only the sign-in's own paths (the context, the password change) take it."""
    found = account(account_id) if isinstance(account_id, str) else None
    if found is None or found["disabled"]:
        return None
    if iat is not None:
        if not isinstance(iat, (int, float)) or isinstance(iat, bool):
            return None
        started = _session_started(found["id"], session)
        if (started if started is not None else iat) < found["sessions_from"]:
            return None
    if found["must_change"] and not one_time_ok:
        raise identity.Refused(403, "password_change_required", ONE_TIME)
    return found


def set_password(account_id: str, password: str, *, one_time: bool = False) -> dict | None:
    """A new password for an account (the founder's reset, an administrator's one-time password, or the person's own change): every session of the account
    ends, its refresh tokens and the access tokens made before now. `one_time`: a password someone else chose for the person (plan Part 9.3), which they
    must replace at their next sign-in; any other set, the person's own change above all, clears the mark."""
    if (problem := password_problem(password)):
        raise ValueError(problem)
    if not isinstance(one_time, bool):
        raise ValueError("One-time must be yes or no")
    with _write() as con:
        # to a fraction of a second: a token of the same second is told from one made after (audit parity-supabase-auth-01)
        con.execute("UPDATE accounts SET password = ?, updated = ?, sessions_from = ?, must_change = ? WHERE id = ?",
                    (auth.hash_password(password), _now(), _clock(), int(one_time), account_id))
        con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (account_id,))
    return account(account_id)


def mark_one_time(account_id: str) -> None:
    """A first password someone else chose (the founder console's new account, or a company's new owner; plan Part 9.3, audit founder-console-08): the
    person chooses their own at their first sign-in, as after a one-time password (`must_change`). The demo's accounts, made by the store, keep theirs."""
    with _write() as con:
        con.execute("UPDATE accounts SET must_change = 1, updated = ? WHERE id = ?", (_now(), account_id))


def set_account_disabled(account_id: str, disabled: bool) -> dict | None:
    """Disable an account, which ends its sessions at once, or enable it again (its ended sessions stay ended)."""
    if not isinstance(disabled, bool):
        raise ValueError("Disabled must be yes or no")
    with _write() as con:
        if disabled:
            con.execute("UPDATE accounts SET disabled = 1, updated = ?, sessions_from = ? WHERE id = ?", (_now(), _clock(), account_id))
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
    for it. None for an account that does not exist or is disabled. `must_change_password` (Gridlytics' own, plan Part 9.3): signed in with a one-time
    password, so nothing but the password change opens until the person chooses their own."""
    found = signed_in(user_id, one_time_ok=True)
    if found is None:
        return None
    first, *last = found["name"].split(None, 1)
    profile = {"id": found["id"], "email": found["email"], "name": found["name"], "first_name": first, "last_name": last[0] if last else None, "avatar_url": None,
               "is_super_admin": False, "assignment_status": "none", "requested_company_name": None, "created_at": found["created"],
               "updated_at": found["updated"], "must_change_password": found["must_change"]}
    return {"profile": profile, "memberships": [{k: v for k, v in m.items() if k not in ("user_id", "company_id")} for m in memberships(user_id)],
            "pending_invites_for_email": []}


# ------------------------------------------------------------------------------------------------ refresh tokens

class Renewed(Exception):
    """A refresh token used again within REUSE_SECONDS of its first use: another window of the same session renewed it a moment ago. Nothing ends; the
    window that sent it takes the session the other one stored."""


#: The most of a browser's User-Agent kept for a session (My account shows it to the person)
AGENT_CHARS = 200


def _add_refresh(con: sqlite3.Connection, user_id: str, family: str, session_ends: float, started: float | None = None, address: str = "",
                 agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = _clock()
    con.execute("INSERT INTO refresh_tokens (token_hash, user_id, family, created, expires, session_ends, started, address, agent) VALUES (?,?,?,?,?,?,?,?,?)",
                (auth.token_hash(token), user_id, family, now, min(now + REFRESH_HOURS * 3600, session_ends), session_ends,
                 started if started is not None else now, (address or "")[:64], " ".join(str(agent or "").split())[:AGENT_CHARS]))
    return token


def new_refresh(user_id: str, address: str = "", agent: str = "", *, checked: dict | None = None) -> str | None:
    """The refresh token of a new session of an account (a sign-in), kept as its hash, with the address and the browser (`agent`, its User-Agent) it was
    made from. The tokens of sessions past their last day by more than an access token's life go: nothing can renew them, so the table holds a week of
    sessions, not every one ever made.
    `checked` is the account as the sign-in's password check read it: None, and no session, when the account was disabled or its password changed since,
    so a reset landing between the check and this leaves no session behind it (audit parity-supabase-auth-01)."""
    with _write() as con:
        if checked is not None:
            now = con.execute("SELECT disabled, sessions_from FROM accounts WHERE id = ?", (user_id,)).fetchone()
            if now is None or now["disabled"] or now["sessions_from"] != checked["sessions_from"]:
                return None
        # kept an access token's life past the session's end: a session signed out in its last hour keeps its access tokens refused until they run out
        # (session_ended reads these rows; round 7 review of audit security-roles-09)
        con.execute("DELETE FROM refresh_tokens WHERE session_ends < ?", (_clock() - ACCESS_MINUTES * 60,))
        return _add_refresh(con, user_id, uuid.uuid4().hex, _clock() + SESSION_DAYS * 86400, address=address, agent=agent)


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
                return _account(a), _add_refresh(con, a["id"], r["family"], r["session_ends"], r["started"] if r["started"] is not None else r["created"],
                                                 r["address"], r["agent"])
    if raced:
        raise Renewed()
    return None


def end_session(token: str) -> bool:
    """Sign out: the session the refresh token belongs to ends, every token of it, and with them the session's access tokens (`session_ended`)."""
    if not isinstance(token, str) or not token:
        return False
    with _write() as con:
        r = con.execute("SELECT family FROM refresh_tokens WHERE token_hash = ?", (auth.token_hash(token),)).fetchone()
        return bool(r) and con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE family = ?", (r["family"],)).rowcount > 0


# A session's id is its refresh tokens' family, as a uuid: the access tokens of the session carry it as their `session_id` (identity.issue_local), so a
# session that ends (a sign-out, its refresh token used again, a password change, the account disabled) refuses its access tokens at once, not an hour
# later (audit security-roles-09). A token whose id names no family (one made before this, or by a test) has no session to end.

def _family(session: str | None) -> str | None:
    try:
        return uuid.UUID(session).hex if isinstance(session, str) and len(session) <= 64 else None
    except ValueError:
        return None


def session_id(refresh_token: str) -> str | None:
    """The id of the session a refresh token belongs to: its family, as a uuid; None for a token that is not known."""
    if not isinstance(refresh_token, str) or not refresh_token:
        return None
    with _read() as con:
        r = con.execute("SELECT family FROM refresh_tokens WHERE token_hash = ?", (auth.token_hash(refresh_token),)).fetchone()
    return str(uuid.UUID(r["family"])) if r and _family(r["family"]) else None


def _session_started(user_id: str, session: str | None) -> float | None:
    """When the account's session `session` started (its sign-in, in unix seconds with their fraction); None when it names no session of the account."""
    family = _family(session)
    if family is None:
        return None
    with _read() as con:
        r = con.execute("SELECT MIN(COALESCE(started, created)) AS started FROM refresh_tokens WHERE family = ? AND user_id = ?", (family, user_id)).fetchone()
    return r["started"] if r is not None else None


def session_ended(session: str | None) -> bool:
    """Whether the session an access token names has ended: any of its refresh tokens revoked. False for an id that names no session, and while there is
    no registry (nothing has been signed in to)."""
    family = _family(session)
    if family is None or not db_path().exists():
        return False
    with _read() as con:
        return con.execute("SELECT 1 FROM refresh_tokens WHERE family = ? AND revoked = 1 LIMIT 1", (family,)).fetchone() is not None


def open_sessions(user_id: str) -> list[dict]:
    """An account's open sessions, the last renewed first (UI-18 My account; audit role-analyst-32): each session's id, when it started, when it was last
    renewed (`last_active`), when it ends unless renewed again (`ends`), all in unix seconds, and the address and browser it was signed in from. A session
    is open while its newest refresh token is unused, not revoked and not past its time."""
    now = _clock()
    with _read() as con:
        rows = con.execute("SELECT * FROM refresh_tokens WHERE user_id = ? AND revoked = 0 AND used IS NULL AND expires > ? AND session_ends > ? "
                           "ORDER BY created DESC", (user_id, now, now)).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["family"] in seen or _family(r["family"]) is None:
            continue
        seen.add(r["family"])
        out.append({"id": str(uuid.UUID(r["family"])), "started": r["started"] if r["started"] is not None else r["created"], "last_active": r["created"],
                    "ends": r["expires"], "address": r["address"], "device": r["agent"]})
    return out


def end_other_sessions(user_id: str, keep: str | None) -> int:
    """Every session of an account but the one `keep` names (the device asking) ends, and their access tokens with them: how many open ones ended."""
    family = _family(keep) or ""
    ended = sum(1 for s in open_sessions(user_id) if _family(s["id"]) != family)
    with _write() as con:
        con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ? AND revoked = 0 AND family != ?", (user_id, family))
    return ended


def end_session_id(user_id: str, session: str | None) -> bool:
    """End one of an account's sessions by its id (a sign-out with the access token alone): every token of it."""
    family = _family(session)
    if family is None or not isinstance(user_id, str):
        return False
    with _write() as con:
        return con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE family = ? AND user_id = ? AND revoked = 0", (family, user_id)).rowcount > 0


def end_sessions(user_id: str) -> int:
    """Every session of an account ends (its refresh tokens; the access tokens end with `set_password` or `set_account_disabled`)."""
    with _write() as con:
        return con.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ? AND revoked = 0", (user_id,)).rowcount


# ------------------------------------------------------------------------------------------------ the lockout (addendum item 13; review R-235)

def client_address(request) -> str:
    """The address a request counts under for the lockout. A proxy's X-Forwarded-For or Forwarded header is believed only where the server says it runs
    behind a proxy it trusts (GRIDLYTICS_TRUST_PROXY=1, with uvicorn's --forwarded-allow-ips naming it): otherwise uvicorn's default takes the header from
    any client on this machine, and a new address per try bought five more (R-235). A request that carries such a header untrusted counts as "forwarded"."""
    global _proxy_said
    host = request.client.host if request.client else ""
    if os.environ.get("GRIDLYTICS_TRUST_PROXY") != "1" and (request.headers.get("x-forwarded-for") or request.headers.get("forwarded")):
        if not _proxy_said:
            # behind a proxy that was not declared, every client shares the "forwarded" count, and five wrong passwords lock an account for all of them
            # (audit security-roles-10): said once, loudly, with what to set
            _proxy_said = True
            logging.getLogger("gridlytics").warning(
                "a sign-in carried X-Forwarded-For or Forwarded, but GRIDLYTICS_TRUST_PROXY is not 1: every client behind that proxy counts as one address "
                "for the sign-in lockout. Behind a proxy you trust, set GRIDLYTICS_TRUST_PROXY=1 and run uvicorn with --forwarded-allow-ips set to the "
                "proxy's address")
        return "forwarded"
    return host


def record(event: str, email: str, address: str = "", account_id: str = "", detail: dict | None = None) -> None:
    """One line of the record of sign-ins: a refusal the lockout counts, a sign-in that clears the count, or an account's own change. A refusal for an email
    with no account (audit parity-supabase-auth-08) is kept only while the lockout counts it (LOCK_MINUTES): the record never fills with made-up emails."""
    now = _clock()
    with _write() as con:
        if event == REFUSED and not account_id:
            con.execute("DELETE FROM account_events WHERE event = ? AND account_id = '' AND email LIKE '%@%' AND at < ?",
                        (REFUSED, now - auth.LOCK_MINUTES * 60))
        con.execute("INSERT INTO account_events (at, email, address, event, account_id, detail) VALUES (?,?,?,?,?,?)",
                    (now, (email or "").strip().lower(), address or "", event, account_id or "", json.dumps(detail or {})))


def events(email: str, limit: int = 200) -> list[dict]:
    """An account's record, oldest first."""
    with _read() as con:
        rows = con.execute("SELECT * FROM (SELECT * FROM account_events WHERE email = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                           ((email or "").strip().lower(), limit)).fetchall()
    return [{"at": r["at"], "email": r["email"], "address": r["address"], "event": r["event"], "account_id": r["account_id"],
             "detail": json.loads(r["detail"])} for r in rows]


def lock_wait(email: str, address: str | None, refused: str = REFUSED, cleared: str = SIGNED_IN, tries: int | None = None) -> int:
    """Seconds before an account may be tried again from an address (0 when it may now). `tries` refusals within LOCK_MINUTES stop it for LOCK_MINUTES from
    the last of them; a good sign-in from the address clears the count (R-235: the count outlived a success, and the wait ran from each new try), and so
    does a lockout an administrator or the founder cleared, from every address (`clear_lock`, plan Part 9.3). With `address` None, the refusals from every
    address together (the founder's code step, R-271)."""
    if not email or not db_path().exists():
        return 0
    tries = tries or auth.MAX_TRIES
    window = auth.LOCK_MINUTES * 60
    where, args = ("email = ?", [email.strip().lower()]) if address is None else ("email = ? AND address = ?", [email.strip().lower(), address])
    with _read() as con:
        last_good = con.execute(f"SELECT MAX(at) FROM account_events WHERE {where} AND event = ?", (*args, cleared)).fetchone()[0] or 0
        last_good = max(last_good, con.execute("SELECT MAX(at) FROM account_events WHERE email = ? AND event = ?",
                                               (email.strip().lower(), LOCK_CLEARED)).fetchone()[0] or 0)
        times = [r[0] for r in con.execute(f"SELECT at FROM account_events WHERE {where} AND event = ? AND at > ? ORDER BY at DESC LIMIT ?",
                                           (*args, refused, last_good, tries))]
    left = times[0] + window - _clock() if len(times) == tries and times[0] - times[-1] < window else 0
    return max(1, round(left)) if left > 0 else 0


def sign_in_wait(email: str, address: str) -> tuple[int, str]:
    """Seconds before a password may be tried for an account from an address, and why: "address" (MAX_TRIES refused from this address) or "account"
    (ACCOUNT_TRIES refused from every address together, audit security-roles-10); (0, "") when it may now. The account-wide count does not stop an
    address the account signed in from in the last SESSION_DAYS."""
    wait = lock_wait(email, address)
    if wait:
        return wait, "address"
    wait = lock_wait(email, None, tries=auth.ACCOUNT_TRIES)
    if wait and address and address != "forwarded":
        with _read() as con:
            known = con.execute("SELECT 1 FROM account_events WHERE email = ? AND address = ? AND event = ? AND at > ? LIMIT 1",
                                (email.strip().lower(), address, SIGNED_IN, _clock() - SESSION_DAYS * 86400)).fetchone()
        if known:
            return 0, ""
    return (wait, "account") if wait else (0, "")


def clear_lock(email: str, account_id: str, by: str) -> None:
    """An administrator or the founder clears an account's sign-in lockout (plan Part 9.3; audit pages-A-02): the refusals recorded before now stop
    counting, from every address."""
    record(LOCK_CLEARED, email, "", account_id, {"by": by})


# ------------------------------------------------------------------------------------------------ the founder's audit (decisions-v2 section 4)

def founder_audit(action: str, target: str = "", detail: dict | None = None, actor: str = "founder", company_id: str = "") -> None:
    """One line of the founder's audit: what was done (its words are grouped in audit_trail.FOUNDER_GROUPS), to what, the detail, by whom, and the
    company it is of when it is of one (the console's Company filter; UX K34)."""
    with _write() as con:
        con.execute("INSERT INTO founder_audit (at, actor, action, target, detail, company_id) VALUES (?,?,?,?,?,?)",
                    (_now(), actor, action, target, json.dumps(detail or {}), company_id or ""))


def reading():
    """A connection to the registry for reading, closed at the end of the block (the founder's audit's filtered pages: audit_trail.founder_search)."""
    return _read()


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
