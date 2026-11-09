"""What the founder console reads and changes (the owner's decisions of 24 October 2026, sections 4 and 5; design E; stage B6 of the sign-in and split): the
only module that reads across companies.

* **The registry** (platform.db): the companies, always; in local mode also the accounts and memberships. platform.py keeps one row at a time; what the
  console needs beyond it (every account, every membership, a company made with its owner in one transaction, the record of the founder's codes) is read and
  written here through platform.py's own connection (`_read`, `_write`), so its rules (WAL, the schema version, one migration at a time) hold.
* **Supabase mode**: the accounts and memberships are Gridlytics' own project's, reached with the service-role key (GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY,
  on the server only): its Admin API for accounts, PostgREST for its `companies` and `company_memberships` (supabase/migrations). A company is made in the
  project with the registry's id, and the project's refusals (a second company of one name, a second owner, a name outside 1 to 80 characters, a password
  over 72 characters) are said in the console's words.
* **Each company's file**, read under `store.use_company(path)`: the member rows (seats, Gridlytics roles, last sign-ins), the work done, the plan. A
  company with no file yet reads as empty, and no file is made by reading it; an old file is migrated (with its backup first) as its members' first request
  would; a file that cannot be read is said so, and left out of the sums.

The founder's change of a membership reaches the company's file too (B3's review): a seat is a member row not switched off, so a membership suspended or
removed switches the person's row off (its seat free at once), and one made active again gives the row back to the seat rule (`companies.NO_SEAT`), which
lets the person in at their next request if a seat is free. A row a Gridlytics administrator switched off is left as it is.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import auth, backups, identity, store, store_members
from . import platform as platformdb
from .founder import CODE_REFUSED, PASSWORD_TAKEN, SIGNED_IN, WHO

log = logging.getLogger("gridlytics")

#: The work of a company the overview adds up (decisions-v2 section 4; design E)
ACTIVITY = ("seen_7", "seen_30", "sites", "plans_month", "plans_30", "days_30", "reports_30")
#: Why a member row is switched off by the founder's change of the membership; a row off for one of these follows the membership back
MEMBERSHIP_OFF = {"suspended": "membership suspended", "pending": "membership pending", "removed": "membership removed"}
#: The project's own limit of a password (Supabase Auth refuses one over 72 bytes, bcrypt's); local mode allows 200 characters
PROJECT_PASSWORD_BYTES = 72
#: A ban that does not end, as the Admin API takes one; "none" lifts it
BAN = "876000h"
PAGE = 1000
FETCH_TIMEOUT = 10
#: An answer of the project is far smaller than this
MAX_ANSWER = 16 * 1024 * 1024
UNREAD = "the file could not be read"
OWNER_STAYS = "A company keeps its owner: make another member the owner first"
HAND_OVER = "The sign-in project keeps one owner per company, so the ownership is handed only to an active member: make the membership active first"


class NotFound(LookupError):
    """No such company, account or membership: 404."""


class Conflict(Exception):
    """A clash with what exists (an email with an account, a second membership, a company's name, the last owner): 409."""


class Unreachable(Exception):
    """The sign-in project could not be asked, or did not answer as it does: 503."""


class NotSetUp(Unreachable):
    """Supabase mode without the project's address or its service-role key: 503."""


class ProjectRefused(Exception):
    """The project refused a call: its status and its answer (the Admin API's {code, error_code, msg}, PostgREST's {code, message, details})."""

    def __init__(self, status: int, body):
        super().__init__(f"the project answered {status}")
        self.status, self.body = status, body if isinstance(body, dict) else {}


# ================================================================================================================ the founder's codes (founder.py)

def _last_step(con) -> int:
    held = con.execute("SELECT MAX(json_extract(detail, '$.step')) FROM account_events WHERE email = ? AND event = ?", (WHO, SIGNED_IN)).fetchone()[0]
    return int(held) if held is not None else -1


def last_code_step() -> int:
    """The time step of the last code the founder signed in with (-1 before the first): a code of that step or an earlier one is never taken again."""
    with platformdb._read() as con:
        return _last_step(con)


def _event(con, event: str, address: str, detail: dict) -> None:
    con.execute("INSERT INTO account_events (at, email, address, event, account_id, detail) VALUES (?,?,?,?,?,?)",
                (platformdb._clock(), WHO, address or "", event, "", json.dumps(detail)))


def take_code(digest: str, address: str, check, *, tries: int, seconds: int) -> dict | None:
    """One code for the challenge of this hash, in one write transaction: None when the challenge is unknown, older than `seconds`, used, or out of its
    `tries`; else `check(last step)` (the code's step, or None for a wrong code) is recorded as a sign-in or a refused code, and the answer is
    {step, tries_left}. Two codes sent at once are taken one after the other, so the same code never signs in twice."""
    now = platformdb._clock()
    with platformdb._write() as con:
        issued = con.execute("SELECT id FROM account_events WHERE email = ? AND event = ? AND at > ? AND json_extract(detail, '$.challenge') = ? "
                             "ORDER BY id DESC LIMIT 1", (WHO, PASSWORD_TAKEN, now - seconds, digest)).fetchone()
        if issued is None:
            return None

        def count(event: str) -> int:
            return con.execute("SELECT COUNT(*) FROM account_events WHERE email = ? AND event = ? AND id > ? AND json_extract(detail, '$.challenge') = ?",
                               (WHO, event, issued["id"], digest)).fetchone()[0]

        if count(SIGNED_IN) or count(CODE_REFUSED) >= tries:
            return None
        step = check(_last_step(con))
        if step is None:
            _event(con, CODE_REFUSED, address, {"challenge": digest})
        else:
            _event(con, SIGNED_IN, address, {"challenge": digest, "step": step})
        return {"step": step, "tries_left": max(0, tries - count(CODE_REFUSED))}


def code_refusals(seconds: int) -> int:
    """Wrong codes from every address since the last sign-in, within `seconds`: what the founder's code lock counts."""
    now = platformdb._clock()
    with platformdb._read() as con:
        good = con.execute("SELECT MAX(at) FROM account_events WHERE email = ? AND event = ?", (WHO, SIGNED_IN)).fetchone()[0] or 0
        return con.execute("SELECT COUNT(*) FROM account_events WHERE email = ? AND event = ? AND at > ? AND at > ?",
                           (WHO, CODE_REFUSED, good, now - seconds)).fetchone()[0]


# ================================================================================================================ the founder's sessions (founder.py)

#: The record's words for the founder's sessions (audit founder-console-09): the second from which sessions hold, with the mark of the password hash it
#: was set under ({from, password, why}), and a session signed out ({session: the hash of its token, until})
SESSIONS_FROM = "founder sessions from"
SIGNED_OUT = "founder signed out"


def _held(con) -> dict | None:
    row = con.execute("SELECT detail FROM account_events WHERE email = ? AND event = ? ORDER BY id DESC LIMIT 1", (WHO, SESSIONS_FROM)).fetchone()
    return json.loads(row[0]) if row else None


def sessions_from(mark: str, now: int) -> dict:
    """The second from which the founder's sessions hold, and why the earlier ones ended ({from, why}: why "password" or "asked", "" before any ended).
    The mark of the password hash is kept with it: the first time another one is seen (the founder's password, or the session secret, changed), every
    session made before `now` ends, once, and the founder's audit says so. The first mark ever seen ends nothing."""
    with platformdb._read() as con:
        held = _held(con)
    if held is not None and held.get("password") == mark:
        return held
    with platformdb._write() as con:
        held = _held(con)                               # another request may have seen the change meanwhile
        if held is not None and held.get("password") == mark:
            return held
        now_held = {"from": int(now) if held is not None else 0, "password": mark, "why": "password" if held is not None else ""}
        _event(con, SESSIONS_FROM, "", now_held)
    if held is not None:
        platformdb.founder_audit("ended every founder session", "", {"why": "the founder's password or session secret changed"})
    return now_held


def end_sessions(mark: str, start: int) -> None:
    """Every founder session made before the second `start` ends (signing out everywhere)."""
    with platformdb._write() as con:
        _event(con, SESSIONS_FROM, "", {"from": int(start), "password": mark, "why": "asked"})


def sign_out(digest: str, until: int, address: str) -> None:
    """The session whose token has this hash ends; `until` is when it would have ended by itself."""
    with platformdb._write() as con:
        _event(con, SIGNED_OUT, address, {"session": digest, "until": int(until)})


def signed_out(digest: str) -> bool:
    with platformdb._read() as con:
        return con.execute("SELECT 1 FROM account_events WHERE email = ? AND event = ? AND json_extract(detail, '$.session') = ? LIMIT 1",
                           (WHO, SIGNED_OUT, digest)).fetchone() is not None


# ================================================================================================================ the companies' files

def _plan(cfg_plan=None, invoices=None) -> dict:
    """Commercial plans and seats billing are not in Gridlytics v1 (plan Part 15.5): every company reads as on no plan."""
    return {"state": "none", "name": None, "sentence": "No commercial plan in v1"}


def read_file(row: dict) -> dict:
    """What the founder reads of a company's file: its member rows by the sign-in's id, its sites, the plans issued and the work of the last days. Counts
    only: the founder never reads a company's telemetry or plans (plan Part 10.5). A company with no file yet reads as empty (and none is made)."""
    path = platformdb.company_file(row)
    if path is None or not path.exists():
        return {"members": {}, "sites": 0, "plans_issued": 0, "activity": dict.fromkeys(ACTIVITY, 0), "plan": _plan()}
    now = datetime.now(timezone.utc).replace(microsecond=0)
    month = now.replace(day=1, hour=0, minute=0, second=0).isoformat()
    since7, since30 = (now - timedelta(days=7)).isoformat(), (now - timedelta(days=30)).isoformat()
    org = store.DEFAULT_ORG
    with store.use_company(path), store.use_org(org):
        members = {m["user_id"]: m for m in store_members.members()}
        with closing(store._connect()) as con:
            def one(sql: str, *args) -> int:
                return con.execute(sql, args).fetchone()[0]
            figures = {"sites": one("SELECT COUNT(*) FROM sites WHERE deleted IS NULL"), "plans_issued": one("SELECT COUNT(*) FROM plans")}
            activity = {"plans_month": one("SELECT COUNT(*) FROM plans WHERE generated_at >= ?", month),
                        "plans_30": one("SELECT COUNT(*) FROM plans WHERE generated_at >= ?", since30),
                        "days_30": one("SELECT COUNT(*) FROM telemetry_days WHERE day_local >= ?", since30[:10]),
                        "reports_30": one("SELECT COUNT(*) FROM reports WHERE generated_at >= ?", since30)}
    seen = [m["last_seen"] for m in members.values() if m["last_seen"]]
    activity.update(sites=figures["sites"], seen_7=sum(s >= since7 for s in seen), seen_30=sum(s >= since30 for s in seen))
    return {"members": members, **figures, "activity": activity, "plan": _plan()}


def _read_or_none(row: dict) -> dict | None:
    try:
        return read_file(row)
    except (sqlite3.Error, OSError, ValueError) as exc:
        log.warning("the founder console could not read the file of company %s: %s", row["id"], exc)
        return None


def _seated(read: dict) -> list[dict]:
    return [m for m in read["members"].values() if not m["disabled"]]


# ================================================================================================================ accounts and memberships: the registry

def _account_row(r) -> dict:
    return {"id": r["id"], "email": r["email"], "name": r["name"], "disabled": bool(r["disabled"]), "created": r["created"]}


def _same_name(con, name: str, but: str | None = None) -> None:
    """One company per name, whatever its case, as the project keeps them (companies_name_unique_lower): the console refuses the clash in both modes."""
    if con.execute("SELECT 1 FROM companies WHERE lower(name) = lower(?) AND id != ?", (name, but or "")).fetchone():
        raise Conflict("Another company has that name: choose another")


def _insert_membership(con, user_id: str, company_id: str, role: str, status: str) -> None:
    now = platformdb._now()
    con.execute("INSERT INTO memberships (id, user_id, company_id, role, status, created, activated) VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), user_id, company_id, role, status, now, now if status == "active" else None))


class _Registry:
    """Local mode: the accounts and memberships of platform.db."""

    #: Two owners may share a company for the moment of a hand-over
    ONE_OWNER = False

    def accounts(self) -> list[dict]:
        with platformdb._read() as con:
            return [_account_row(r) for r in con.execute("SELECT * FROM accounts ORDER BY name COLLATE NOCASE, email")]

    def account(self, user_id: str) -> dict | None:
        found = platformdb.account(user_id)
        return {k: found[k] for k in ("id", "email", "name", "disabled", "created")} if found else None

    def memberships(self, company_id: str | None = None, user_id: str | None = None) -> list[dict]:
        where, args = [], []
        for column, value in (("company_id", company_id), ("user_id", user_id)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        with platformdb._read() as con:
            rows = con.execute(f"SELECT * FROM memberships {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY created, rowid", args).fetchall()
        return [{"user_id": r["user_id"], "company_id": r["company_id"], "role": r["role"], "status": r["status"]} for r in rows]

    def create_account(self, email: str, name: str, password: str, company_id: str | None, role: str | None) -> dict:
        email, name = platformdb._email(email), platformdb._name(name)
        hashed = auth.hash_password(password)           # the slow part, before the write lock is taken
        account_id, now = str(uuid.uuid4()), platformdb._now()
        with platformdb._write() as con:
            if con.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
                raise Conflict("That email address already has an account")
            if company_id is not None and con.execute("SELECT 1 FROM companies WHERE id = ?", (company_id,)).fetchone() is None:
                raise NotFound("No such company")
            con.execute("INSERT INTO accounts (id, email, name, password, disabled, created, updated) VALUES (?,?,?,?,0,?,?)",
                        (account_id, email, name, hashed, now, now))
            if company_id is not None:
                _insert_membership(con, account_id, company_id, role, "active")
        return self.account(account_id)

    def create_company(self, name: str, seats: int, enabled: bool, owner: dict) -> tuple[dict, dict]:
        name, seats = platformdb._name(name), platformdb._seats(seats)
        hashed = auth.hash_password(owner["password"]) if "user_id" not in owner else None
        company_id, now = str(uuid.uuid4()), platformdb._now()
        with platformdb._write() as con:
            _same_name(con, name)
            if con.execute("SELECT 1 FROM companies WHERE db_file = ?", (str(platformdb.companies_dir() / f"{company_id}.db"),)).fetchone():
                raise Conflict("Another company names that database file")       # two companies never share one workspace (B3's review)
            if "user_id" in owner:
                if con.execute("SELECT 1 FROM accounts WHERE id = ?", (owner["user_id"],)).fetchone() is None:
                    raise NotFound("No such account")
                owner_id = owner["user_id"]
            else:
                email = platformdb._email(owner["email"])
                if con.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
                    raise Conflict("That email has an account: choose it as the owner")
                owner_id = str(uuid.uuid4())
                con.execute("INSERT INTO accounts (id, email, name, password, disabled, created, updated) VALUES (?,?,?,?,0,?,?)",
                            (owner_id, email, platformdb._name(owner["name"]), hashed, now, now))
            con.execute("INSERT INTO companies (id, name, enabled, seats, disabled_reason, created, db_file) VALUES (?,?,?,?,?,?,?)",
                        (company_id, name, int(enabled), seats, "", now, ""))
            _insert_membership(con, owner_id, company_id, "owner", "active")
        return platformdb.company(company_id), self.account(owner_id)

    def rename_company(self, company_id: str, name: str) -> None:
        with platformdb._read() as con:
            _same_name(con, platformdb._name(name), company_id)
        platformdb.update_company(company_id, name=name)

    def set_password(self, user_id: str, password: str) -> None:
        platformdb.set_password(user_id, password)

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        platformdb.set_account_disabled(user_id, disabled)

    def rename(self, user_id: str, name: str) -> None:
        platformdb.rename_account(user_id, name)

    def add_membership(self, user_id: str, company_id: str, role: str) -> None:
        platformdb.add_membership(user_id, company_id, role, "active")

    def set_membership(self, user_id: str, company_id: str, role: str | None, status: str | None) -> None:
        platformdb.set_membership(user_id, company_id, role=role, status=status)

    def remove_membership(self, user_id: str, company_id: str) -> None:
        platformdb.remove_membership(user_id, company_id)


# ================================================================================================================ accounts and memberships: the project

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _call_project(method: str, path: str, body=None):
    """One call to Gridlytics' own Supabase project with the service-role key: its answer as JSON (None when empty). ProjectRefused for an answer of 400
    or more, Unreachable for anything that is not an answer; not redirected (the project's own address or nothing)."""
    url, key = identity.SUPABASE_URL, os.environ.get("GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        raise NotSetUp("The founder console reaches the sign-in project with GRIDLYTICS_SUPABASE_URL and GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY: set both "
                       "on the server")
    headers = {"apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json", "Prefer": "return=representation"}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
    request = urllib.request.Request(f"{url}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=FETCH_TIMEOUT) as res:
            raw = res.read(MAX_ANSWER + 1)
    except urllib.error.HTTPError as exc:
        try:
            answer = json.loads(exc.read(64 * 1024).decode("utf-8") or "{}")
        except ValueError:
            answer = {}
        finally:
            exc.close()
        raise ProjectRefused(exc.code, answer) from None
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise Unreachable(str(exc) or type(exc).__name__) from None
    if len(raw) > MAX_ANSWER:
        raise Unreachable("the answer is far larger than the console reads")
    try:
        return json.loads(raw.decode("utf-8")) if raw.strip() else None
    except ValueError:
        raise Unreachable("the answer is not JSON") from None


#: The call the project directory makes (a test puts a stand-in project here)
_project = _call_project


def _said(refused: ProjectRefused) -> Exception:
    """The project's refusal in the console's words: what the founder can act on, never the project's own text."""
    body = refused.body
    text = " ".join(str(body.get(k) or "") for k in ("code", "error_code", "msg", "message", "details", "error_description")).lower()
    if "companies_name_unique_lower" in text:
        return Conflict("Another company has that name in the sign-in project: choose another")
    if "memberships_one_owner_per_company" in text:
        return Conflict("The company has its owner in the sign-in project, which keeps one owner per company: make this member the owner from the "
                        "membership, which hands the ownership over")
    if "memberships_user_company_unique" in text:
        return Conflict("That account is already a member of that company")
    if "23514" in text:
        return ValueError("Give the company a name of 1 to 80 characters")
    if "email_exists" in text or "already been registered" in text or "already registered" in text:
        return Conflict("That email address already has an account")
    if "72" in text and "password" in text:
        return ValueError(f"A password may have at most {PROJECT_PASSWORD_BYTES} characters in the sign-in project")
    if refused.status == 404 or "user_not_found" in text:
        return NotFound("No such account")
    if refused.status == 422 and "password" in text:
        return ValueError("The sign-in project refused that password: choose a longer one")
    return Unreachable(f"the project answered {refused.status}")


def _ask(method: str, path: str, body=None):
    try:
        return _project(method, path, body)
    except ProjectRefused as refused:
        raise _said(refused) from None


def _undo(method: str, path: str, body=None) -> None:
    """Take back a step of a change that failed later; a failure here is logged, and the founder sees the first one."""
    try:
        _project(method, path, body)
    except Exception as exc:  # noqa: BLE001  (the change has already failed; this is the tidying after it)
        log.warning("the founder console could not take back %s %s in the sign-in project: %s", method, path.split("?")[0], exc)


def _project_name(user: dict) -> str:
    meta = user.get("user_metadata") if isinstance(user.get("user_metadata"), dict) else {}
    name = meta.get("full_name") or meta.get("name")
    return " ".join(name.split())[:80] if isinstance(name, str) and name.strip() else (user.get("email") or "")


def _banned(user: dict) -> bool:
    until = user.get("banned_until")
    if not isinstance(until, str) or not until:
        return False
    try:
        return datetime.fromisoformat(until.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return True


def _project_account(user: dict) -> dict:
    return {"id": user["id"], "email": (user.get("email") or "").lower(), "name": _project_name(user), "disabled": _banned(user),
            "created": user.get("created_at") or ""}


def _eq(**columns) -> str:
    return "&".join(f"{k}=eq.{quote(v, safe='')}" for k, v in columns.items())


def _bytes_ok(password: str) -> None:
    if len(password.encode("utf-8")) > PROJECT_PASSWORD_BYTES:
        raise ValueError(f"A password may have at most {PROJECT_PASSWORD_BYTES} characters in the sign-in project (fewer with letters outside plain English)")


class _Project:
    """Supabase mode: the accounts of the project's Admin API and the memberships of its `company_memberships`, with the service-role key."""

    #: The project keeps one owner per company (memberships_one_owner_per_company): making a member the owner hands the ownership over
    ONE_OWNER = True

    def accounts(self) -> list[dict]:
        out = []
        for page in range(1, 101):
            users = (_ask("GET", f"/auth/v1/admin/users?page={page}&per_page={PAGE}") or {}).get("users") or []
            out += [_project_account(u) for u in users if isinstance(u, dict) and u.get("id")]
            if len(users) < PAGE:
                break
        return sorted(out, key=lambda a: (a["name"].lower(), a["email"]))

    def account(self, user_id: str) -> dict | None:
        try:
            user = _ask("GET", f"/auth/v1/admin/users/{quote(user_id, safe='')}")
        except NotFound:
            return None
        return _project_account(user) if isinstance(user, dict) and user.get("id") else None

    def memberships(self, company_id: str | None = None, user_id: str | None = None) -> list[dict]:
        where = {k: v for k, v in (("company_id", company_id), ("user_id", user_id)) if v is not None}
        out, offset = [], 0
        while True:
            rows = _ask("GET", f"/rest/v1/company_memberships?select=user_id,company_id,role,status,created_at"
                               f"{'&' + _eq(**where) if where else ''}&order=created_at&limit={PAGE}&offset={offset}") or []
            out += [{"user_id": r["user_id"], "company_id": r["company_id"], "role": r["role"], "status": r["status"]} for r in rows]
            if len(rows) < PAGE:
                return out
            offset += PAGE

    def _new_user(self, email: str, name: str, password: str) -> dict:
        _bytes_ok(password)
        user = _ask("POST", "/auth/v1/admin/users", {"email": email, "password": password, "email_confirm": True, "user_metadata": {"full_name": name}})
        if not isinstance(user, dict) or not user.get("id"):
            raise Unreachable("the project made no account")
        return _project_account(user)

    def create_account(self, email: str, name: str, password: str, company_id: str | None, role: str | None) -> dict:
        email, name = platformdb._email(email), platformdb._name(name)
        if company_id is not None and platformdb.company(company_id) is None:
            raise NotFound("No such company")
        made = self._new_user(email, name, password)
        if company_id is not None:
            try:
                self.add_membership(made["id"], company_id, role)
            except Exception:
                _undo("DELETE", f"/auth/v1/admin/users/{made['id']}")
                raise
        return made

    def create_company(self, name: str, seats: int, enabled: bool, owner: dict) -> tuple[dict, dict]:
        name, seats = platformdb._name(name), platformdb._seats(seats)
        with platformdb._read() as con:
            _same_name(con, name)
        company_id = str(uuid.uuid4())
        if "user_id" in owner:
            account = self.account(owner["user_id"])
            if account is None:
                raise NotFound("No such account")
            made = None
        else:
            try:
                account = made = self._new_user(platformdb._email(owner["email"]), platformdb._name(owner["name"]), owner["password"])
            except Conflict:
                raise Conflict("That email has an account: choose it as the owner") from None
        try:
            _ask("POST", "/rest/v1/companies", {"id": company_id, "name": name})
            try:
                _ask("POST", "/rest/v1/company_memberships", {"user_id": account["id"], "company_id": company_id, "role": "owner", "status": "active"})
                company = platformdb.create_company(name, enabled=enabled, seats=seats, company_id=company_id)
            except Exception:
                _undo("DELETE", f"/rest/v1/companies?{_eq(id=company_id)}")       # its memberships go with it (ON DELETE CASCADE)
                raise
        except Exception:
            if made is not None:
                _undo("DELETE", f"/auth/v1/admin/users/{made['id']}")
            raise
        return company, account

    def rename_company(self, company_id: str, name: str) -> None:
        name = platformdb._name(name)
        with platformdb._read() as con:
            _same_name(con, name, company_id)
        _ask("PATCH", f"/rest/v1/companies?{_eq(id=company_id)}", {"name": name})
        platformdb.update_company(company_id, name=name)

    def _user(self, user_id: str, change: dict) -> None:
        _ask("PUT", f"/auth/v1/admin/users/{quote(user_id, safe='')}", change)

    def set_password(self, user_id: str, password: str) -> None:
        _bytes_ok(password)
        self._user(user_id, {"password": password})

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        self._user(user_id, {"ban_duration": BAN if disabled else "none"})

    def rename(self, user_id: str, name: str) -> None:
        name = platformdb._name(name)
        self._user(user_id, {"user_metadata": {"full_name": name}})
        first, *last = name.split(None, 1)
        # The profile's names were made from full_name when the account was (handle_new_user); they follow it here, as get_my_context reads them
        _ask("PATCH", f"/rest/v1/profiles?{_eq(id=user_id)}", {"first_name": first, "last_name": last[0] if last else None})

    def add_membership(self, user_id: str, company_id: str, role: str) -> None:
        _ask("POST", "/rest/v1/company_memberships", {"user_id": user_id, "company_id": company_id, "role": role, "status": "active"})

    def set_membership(self, user_id: str, company_id: str, role: str | None, status: str | None) -> None:
        change = {k: v for k, v in (("role", role), ("status", status)) if v is not None}
        held = [m for m in self.memberships(company_id=company_id) if m["role"] == "owner" and m["user_id"] != user_id]
        # The project keeps one owner per company: making a member the owner hands the ownership over, the owner before becoming an admin
        for m in held if role == "owner" else []:
            _ask("PATCH", f"/rest/v1/company_memberships?{_eq(user_id=m['user_id'], company_id=company_id)}", {"role": "admin"})
        try:
            _ask("PATCH", f"/rest/v1/company_memberships?{_eq(user_id=user_id, company_id=company_id)}", change)
        except Exception:
            for m in held if role == "owner" else []:
                _undo("PATCH", f"/rest/v1/company_memberships?{_eq(user_id=m['user_id'], company_id=company_id)}", {"role": "owner"})
            raise

    def remove_membership(self, user_id: str, company_id: str) -> None:
        _ask("DELETE", f"/rest/v1/company_memberships?{_eq(user_id=user_id, company_id=company_id)}")


_REGISTRY, _PROJECT = _Registry(), _Project()


def directory():
    """Where the accounts and memberships are: the registry in local mode, the project in supabase mode."""
    return _PROJECT if identity.MODE == "supabase" else _REGISTRY


# ================================================================================================================ what the console shows

def _membership_view(m: dict, names: dict) -> dict:
    return {"company_id": m["company_id"], "company_name": names.get(m["company_id"], ""), "company_role": m["role"], "status": m["status"]}


def _company_view(row: dict, mine: list[dict], accounts: dict, read: dict | None) -> dict:
    owner = next((accounts[m["user_id"]] for m in mine if m["role"] == "owner" and m["status"] == "active" and m["user_id"] in accounts), None)
    seated = _seated(read) if read is not None else None
    seen = [m["last_seen"] for m in read["members"].values() if m["last_seen"]] if read is not None else []
    return {"id": row["id"], "name": row["name"], "enabled": row["enabled"], "seats": row["seats"], "disabled_reason": row["disabled_reason"],
            "created": row["created"], "owner": {k: owner[k] for k in ("id", "email", "name")} if owner else None, "members": len(mine),
            "seats_taken": len(seated) if seated is not None else None,
            "admins": sum(m["role"] == "admin" for m in seated) if seated is not None else None,
            "last_seen": max(seen) if seen else None,
            "sites": read["sites"] if read is not None else None, "plans_issued": read["plans_issued"] if read is not None else None,
            "plan": read["plan"] if read is not None else None}


def company_view(company_id: str) -> dict:
    row = platformdb.company(company_id)
    if row is None:
        raise NotFound("No such company")
    d = directory()
    mine = d.memberships(company_id=company_id)
    accounts = {a["id"]: a for a in (d.accounts() if mine else [])}
    return _company_view(row, mine, accounts, _read_or_none(row))


def companies() -> list[dict]:
    d = directory()
    every, accounts = d.memberships(), {a["id"]: a for a in d.accounts()}
    return [_company_view(row, [m for m in every if m["company_id"] == row["id"]], accounts, _read_or_none(row)) for row in platformdb.list_companies()]


def overview() -> dict:
    rows = platformdb.list_companies()
    d = directory()
    accounts, every = d.accounts(), d.memberships()
    on = [r for r in rows if r["enabled"]]
    out = {"companies": {"total": len(rows), "on": len(on), "off": len(rows) - len(on)},
           "accounts": {"total": len(accounts), "disabled": sum(a["disabled"] for a in accounts)},
           "memberships": {s: sum(m["status"] == s for m in every) for s in platformdb.STATUSES},
           "seats": {"given": sum(r["seats"] for r in on), "taken": 0}, "roles": {}, "activity": dict.fromkeys(ACTIVITY, 0),
           "plans": {s: 0 for s in ("none", "trial", "active", "due")}, "unread": []}
    for row in on:
        read = _read_or_none(row)
        if read is None:
            out["unread"].append({"company_id": row["id"], "name": row["name"], "reason": UNREAD})
            continue
        seated = _seated(read)
        out["seats"]["taken"] += len(seated)
        for m in seated:
            out["roles"][m["role"]] = out["roles"].get(m["role"], 0) + 1
        for key in ACTIVITY:
            out["activity"][key] += read["activity"][key]
        out["plans"][read["plan"]["state"]] = out["plans"].get(read["plan"]["state"], 0) + 1
    return out


def company_members(company_id: str) -> list[dict]:
    row = platformdb.company(company_id)
    if row is None:
        raise NotFound("No such company")
    d = directory()
    accounts = {a["id"]: a for a in d.accounts()}
    read = _read_or_none(row)
    out = []
    for m in d.memberships(company_id=company_id):
        who = accounts.get(m["user_id"], {"email": "", "name": "", "disabled": False})
        held = (read or {"members": {}})["members"].get(m["user_id"])
        role = held["role"] if held else None
        # gridlytics_role is the field's name; solarlytics_role (the lifted name) is kept one release for the console page that still reads it
        out.append({"user_id": m["user_id"], "email": who["email"], "name": who["name"], "company_role": m["role"], "status": m["status"],
                    "disabled": who["disabled"], "gridlytics_role": role, "solarlytics_role": role, "seated": (not held["disabled"]) if held else None,
                    "last_seen": held["last_seen"] if held else None})
    return sorted(out, key=lambda x: (x["name"].lower(), x["email"]))


def account_view(account: dict, mine: list[dict], names: dict, reads: dict | None = None) -> dict:
    """An account as the console shows it, never with a password or its hash; with `reads` (each company's file, read once), each membership carries the
    person's Gridlytics role and last sign-in there."""
    out = []
    for m in mine:
        view = _membership_view(m, names)
        if reads is not None:
            held = (reads.get(m["company_id"]) or {"members": {}})["members"].get(account["id"])
            role = held["role"] if held else None
            view.update(gridlytics_role=role, solarlytics_role=role, last_seen=held["last_seen"] if held else None)
        out.append(view)
    return {**{k: account[k] for k in ("id", "email", "name", "disabled", "created")},
            "memberships": sorted(out, key=lambda v: v["company_name"].lower())}


def users() -> list[dict]:
    d = directory()
    rows = platformdb.list_companies()
    names = {r["id"]: r["name"] for r in rows}
    every = d.memberships()
    reads = {r["id"]: _read_or_none(r) for r in rows if any(m["company_id"] == r["id"] for m in every)}
    return [account_view(a, [m for m in every if m["user_id"] == a["id"]], names, reads) for a in d.accounts()]


def user(user_id: str) -> dict:
    d = directory()
    found = d.account(user_id)
    if found is None:
        raise NotFound("No such account")
    return account_view(found, d.memberships(user_id=user_id), {r["id"]: r["name"] for r in platformdb.list_companies()})


def membership_view(user_id: str, company_id: str) -> dict:
    held = directory().memberships(company_id=company_id, user_id=user_id)
    if not held:
        raise NotFound("No such membership")
    row = platformdb.company(company_id) or {"name": ""}
    return _membership_view(held[0], {company_id: row["name"]})


# ================================================================================================================ the founder's changes

def _known(user_id: str, company_id: str | None = None) -> dict:
    """The account (404 when there is none), after checking the company exists when one is named."""
    account = directory().account(user_id)
    if account is None:
        raise NotFound("No such account")
    if company_id is not None and platformdb.company(company_id) is None:
        raise NotFound("No such company")
    return account


def _owner_stays(user_id: str, company_id: str, role: str | None = None, status: str | None = None, removing: bool = False) -> dict:
    """The membership as it is, refused (409) when the change would leave the company with no active owner. Two owners are allowed (a hand-over) where
    the directory keeps them; where it keeps one (the project), making a member the owner makes the owners before admins, so the new owner must be active."""
    d = directory()
    every = d.memberships(company_id=company_id)
    held = next((m for m in every if m["user_id"] == user_id), None)
    if held is None:
        raise NotFound("No such membership")
    after = None if removing else {**held, **{k: v for k, v in (("role", role), ("status", status)) if v is not None}}
    was_owner = held["role"] == "owner" and held["status"] == "active"
    still = after is not None and after["role"] == "owner" and after["status"] == "active"
    others = [m for m in every if m["user_id"] != user_id and m["role"] == "owner" and m["status"] == "active"]
    if role == "owner" and d.ONE_OWNER and others and not still:
        raise Conflict(HAND_OVER)
    if was_owner and not still and not others:
        raise Conflict(OWNER_STAYS)
    return held


def follow_membership(company_id: str, user_id: str, status: str) -> None:
    """The person's member row in the company's file after the founder's change of the membership (`status`, or "removed"): switched off while it is not
    active, given back to the seat rule when it is again. Nothing when the file or the row is not made yet (the first request makes the row)."""
    path = platformdb.company_file(company_id)
    if path is None or not path.exists():
        return
    from .companies import NO_SEAT
    with store.use_company(path), store.use_org(store.DEFAULT_ORG):
        held = store_members.member(user_id)
        if held is None:
            return
        if status != "active":
            if not held["disabled"] or held["disabled_reason"] in (*MEMBERSHIP_OFF.values(), NO_SEAT):
                store_members.update_member(user_id, disabled=True, disabled_reason=MEMBERSHIP_OFF[status], by="founder")
        elif held["disabled"] and held["disabled_reason"] in MEMBERSHIP_OFF.values():
            store_members.update_member(user_id, disabled=True, disabled_reason=NO_SEAT, by="founder")


def create_company(name: str, seats: int, enabled: bool, owner: dict) -> tuple[dict, dict]:
    return directory().create_company(name, seats, enabled, owner)


def update_company(company_id: str, name: str | None = None, seats: int | None = None) -> tuple[dict, dict]:
    """Rename a company and set its seats; the row before and after."""
    before = platformdb.company(company_id)
    if before is None:
        raise NotFound("No such company")
    if name is not None and platformdb._name(name) != before["name"]:
        directory().rename_company(company_id, name)
    if seats is not None:
        platformdb.update_company(company_id, seats=seats)
    return before, platformdb.company(company_id)


def create_account(email: str, name: str, password: str, company_id: str | None = None, role: str | None = None) -> dict:
    if company_id is not None and platformdb.company(company_id) is None:
        raise NotFound("No such company")
    return directory().create_account(email, name, password, company_id, role)


# Each change below returns as soon as it is made, with what the founder's audit says of it: the route writes the audit line before it reads anything back
# (a view of the project that cannot be reached must not leave a change unaudited), and then has the member row follow (follow_membership)

def set_password(user_id: str, password: str) -> dict:
    """A new password; the account."""
    account = _known(user_id)
    directory().set_password(user_id, password)
    return account


def update_account(user_id: str, disabled: bool | None = None, name: str | None = None) -> tuple[dict, dict]:
    """Rename an account, disable or enable it; the account before and after."""
    d = directory()
    before = d.account(user_id)
    if before is None:
        raise NotFound("No such account")
    after = dict(before)
    if name is not None and platformdb._name(name) != before["name"]:
        d.rename(user_id, name)
        after["name"] = platformdb._name(name)
    if disabled is not None and disabled != before["disabled"]:
        d.set_disabled(user_id, disabled)
        after["disabled"] = disabled
    return before, after


def add_membership(user_id: str, company_id: str, role: str) -> dict:
    """The account joins the company, active; the account."""
    account = _known(user_id, company_id)
    if directory().memberships(company_id=company_id, user_id=user_id):
        raise Conflict("That account is already a member of that company")
    directory().add_membership(user_id, company_id, role)
    return account


def change_membership(user_id: str, company_id: str, role: str | None = None, status: str | None = None) -> tuple[dict, dict, dict]:
    """A membership's company role or status; the account, and the membership before and after."""
    account = _known(user_id, company_id)
    before = _owner_stays(user_id, company_id, role, status)
    directory().set_membership(user_id, company_id, role, status)
    return account, before, {**before, **{k: v for k, v in (("role", role), ("status", status)) if v is not None}}


def remove_membership(user_id: str, company_id: str) -> tuple[dict, dict]:
    """The account leaves the company; the account, and the membership it had."""
    account = _known(user_id, company_id)
    before = _owner_stays(user_id, company_id, removing=True)
    directory().remove_membership(user_id, company_id)
    return account, before


# ================================================================================================================ backups, per company

def _backups_here() -> list[dict]:
    """The current company's copies, newest first, each `modified` in UTC with its offset (list_backups gives the server's local time with none, which a
    browser in another zone would misread, R-408)."""
    out = []
    for b in backups.list_backups():
        try:
            at = (backups.folder() / b["name"]).stat().st_mtime
        except FileNotFoundError:               # pruned while being listed
            continue
        out.append({"name": b["name"], "kind": b["kind"], "modified": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds"),
                    "bytes": b["bytes"]})
    return out


def _file(company_id: str):
    path = platformdb.company_file(company_id)
    if path is None:                                 # never the default file: store.use_company refuses None
        raise NotFound("No such company")
    return path


def company_backups(company_id: str) -> list[dict]:
    with store.use_company(_file(company_id)):
        return _backups_here()


def back_up(company_id: str) -> tuple[str | None, list[dict]]:
    """A copy of the company's file now, kept beside its daily ones; its newest KEEP_MANUAL manual copies are kept. None when it has no file yet."""
    with store.use_company(_file(company_id)):
        made = store.backup("manual")
        backups.prune("manual", backups.KEEP_MANUAL)
        return (made.name if made else None), _backups_here()
