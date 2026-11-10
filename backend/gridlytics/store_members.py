"""The people who use a company's workspace (table `members`, schema version 20) and the roles set ahead of a first sign-in (`role_presets`): the sign-in
and split, decisions-v2 sections 3 and 5, design B.5.

A member is kept by the sign-in's own id (the token's `sub`, a uuid, in local mode and in supabase mode alike), in the company's own database, with the
Gridlytics role and where it came from: `owner` (the company's owner, an administrator who cannot be demoted), `legacy` (a user of the workspace of
before, linked by address), `default` (derived from the company role, so it follows it) or `set` (chosen: by an administrator, or by a preset, a role set
for the address before the first sign-in and used once). The address, the name and the company role follow the sign-in and the membership. Who becomes
what is decided in companies.py; this module only keeps the rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import auth
from .store import _connect, _now, _org, write_transaction

ROLE_SOURCES = ("owner", "legacy", "default", "set")
#: The tables whose `owner` is a user's id, which a user of before hands on to the member they become (design F.4). Calculation sign-offs and a revision's
#: `discount_by` keep the old id: they are records of who did what, compared through the member's `legacy_user_id`
OWNED: tuple = ()
#: A member's last sight is written at most this often, so a page's requests do not each write
SEEN_EVERY = timedelta(minutes=5)
#: What a change may set (the key, the first sight and the time of the change are the store's)
_FIELDS = ("email", "name", "role", "role_source", "company_role", "licence", "seal", "disabled", "disabled_reason", "legacy_user_id", "by")


def _member(r) -> dict | None:
    """A member's row as the store answers it: the seal's image is not in it, only whether there is one (`seal()` gives the image), as for a user."""
    if r is None:
        return None
    row = dict(r)
    return {**{k: v for k, v in row.items() if k != "seal"}, "disabled": bool(row["disabled"]), "has_seal": bool(row["seal"])}


def _role(role) -> str:
    if role not in auth.ROLES:
        raise ValueError(f"The role must be one of {', '.join(auth.ROLES)}")
    return role


def _values(fields: dict) -> dict:
    """The columns of a change, checked: a role of the six, a source of ROLE_SOURCES, the address in lower case, text for text."""
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise ValueError(f"A member has no field {', '.join(sorted(unknown))} to change")
    out = {}
    for key, value in fields.items():
        if key == "role":
            value = _role(value)
        elif key == "role_source" and value not in ROLE_SOURCES:
            raise ValueError(f"The role's source must be one of {', '.join(ROLE_SOURCES)}")
        elif key == "disabled":
            value = int(bool(value))
        elif key == "legacy_user_id":
            value = value or None
        elif key == "email":
            value = (value or "").strip().lower()
        else:
            value = value if isinstance(value, str) else ""
        out[key] = value
    return out


def add_member(user_id: str, *, email: str, name: str, role: str, role_source: str = "default", company_role: str = "", licence: str = "",
               seal: str = "", disabled: bool = False, disabled_reason: str = "", legacy_user_id: str | None = None, by: str = "") -> dict:
    """The member of a first sight, or the row already there: two first requests of one person may race, and the first row made is the one both read."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("A member is kept by the sign-in's id")
    values = _values({"email": email, "name": name, "role": role, "role_source": role_source, "company_role": company_role, "licence": licence, "seal": seal,
                      "disabled": disabled, "disabled_reason": disabled_reason, "legacy_user_id": legacy_user_id, "by": by})
    now = _now()
    columns = ("org_id", "user_id", *values, "first_seen", "updated")
    with _connect() as con:
        con.execute(f"INSERT OR IGNORE INTO members ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    (_org(), user_id, *values.values(), now, now))
    return member(user_id)


def member(user_id: str) -> dict | None:
    with _connect() as con:
        return _member(con.execute("SELECT * FROM members WHERE org_id = ? AND user_id = ?", (_org(), user_id)).fetchone())


def member_by_email(email: str) -> dict | None:
    with _connect() as con:
        return _member(con.execute("SELECT * FROM members WHERE org_id = ? AND email = ? ORDER BY first_seen LIMIT 1",
                                   (_org(), (email or "").strip().lower())).fetchone())


def members() -> list[dict]:
    """Every member of the organisation, in the order they were first seen."""
    with _connect() as con:
        return [_member(r) for r in con.execute("SELECT * FROM members WHERE org_id = ? ORDER BY first_seen, rowid", (_org(),)).fetchall()]


def update_member(user_id: str, **fields) -> dict | None:
    """Change a member's fields (of `_FIELDS`); the member as it is now, or None when there is no such member."""
    values = _values(fields)
    if values:
        with _connect() as con:
            con.execute(f"UPDATE members SET {', '.join(f'{k} = ?' for k in values)}, updated = ? WHERE org_id = ? AND user_id = ?",
                        (*values.values(), _now(), _org(), user_id))
    return member(user_id)


def seen(user_id: str) -> bool:
    """Write the member's last sight, unless it was written less than SEEN_EVERY ago; whether it was written."""
    now = _now()
    before = (datetime.fromisoformat(now) - SEEN_EVERY).isoformat(timespec="seconds")
    with _connect() as con:
        return con.execute("UPDATE members SET last_seen = ? WHERE org_id = ? AND user_id = ? AND (last_seen IS NULL OR last_seen <= ?)",
                           (now, _org(), user_id, before)).rowcount == 1


def count_members(role: str | None = None, include_disabled: bool = False) -> int:
    """The members who are not switched off (the seats they take, design B.5), or of one role (the last administrator); with `include_disabled`, all."""
    sql, args = "SELECT COUNT(*) FROM members WHERE org_id = ?", [_org()]
    if not include_disabled:
        sql += " AND disabled = 0"
    if role is not None:
        sql += " AND role = ?"
        args.append(role)
    with _connect() as con:
        return con.execute(sql, args).fetchone()[0]


def seal(user_id: str) -> str:
    """A member's seal (an image as a data URL); '' when none or no such member. The seal is never in a member's answer, only whether there is one."""
    with _connect() as con:
        r = con.execute("SELECT seal FROM members WHERE org_id = ? AND user_id = ?", (_org(), user_id)).fetchone()
    return r["seal"] if r else ""


def as_user(m: dict) -> dict:
    """A member as the signed-in user `auth.current` holds: today's shape of a user (so `auth.can`, `auth.actor` and every `need` work unchanged), with the
    company role and the id of the user of before it was linked to."""
    return {"id": m["user_id"], "email": m["email"], "name": m["name"], "role": m["role"], "created": m["first_seen"], "disabled": bool(m["disabled"]),
            "licence": m["licence"], "has_seal": m["has_seal"], "mfa": False, "company_role": m["company_role"], "legacy_id": m["legacy_user_id"]}


# ------------------------------------------------------------------ the users of before (a workspace linked to its company, design F.4)

def legacy_user(email: str) -> dict | None:
    """Gridlytics has no workspace of before to link (plan Part 10.4): nobody is a user of before."""
    return None


def adopt_legacy(legacy_id: str, user_id: str) -> dict:
    """Nothing to adopt in Gridlytics (no workspace of before)."""
    return {}

def _preset(r) -> dict | None:
    return dict(r) if r is not None else None


def set_preset(email: str, role: str, by: str = "") -> dict:
    """The role the person of `email` gets at their first sign-in (a preset set again replaces the role and is good once more)."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("Give the person's e-mail address")
    with _connect() as con:
        con.execute("INSERT INTO role_presets (org_id, email, role, created, by) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(org_id, email) DO UPDATE SET role = excluded.role, created = excluded.created, by = excluded.by, used = NULL",
                    (_org(), email, _role(role), _now(), by))
    return _preset_row(email)


def _preset_row(email: str) -> dict | None:
    with _connect() as con:
        return _preset(con.execute("SELECT * FROM role_presets WHERE org_id = ? AND email = ?", (_org(), email)).fetchone())


def preset(email: str) -> dict | None:
    """The preset of an address that is not used yet, or None."""
    p = _preset_row((email or "").strip().lower())
    return p if p and p["used"] is None else None


def use_preset(email: str) -> dict | None:
    """Take the preset of an address once: it is marked used and returned; None when there is none left to use."""
    email = (email or "").strip().lower()
    with _connect() as con:
        taken = con.execute("UPDATE role_presets SET used = ? WHERE org_id = ? AND email = ? AND used IS NULL", (_now(), _org(), email)).rowcount
    return _preset_row(email) if taken else None


def list_presets() -> list[dict]:
    """The presets not used yet, by address."""
    with _connect() as con:
        return [_preset(r) for r in con.execute("SELECT * FROM role_presets WHERE org_id = ? AND used IS NULL ORDER BY email", (_org(),)).fetchall()]


def delete_preset(email: str) -> bool:
    with _connect() as con:
        return con.execute("DELETE FROM role_presets WHERE org_id = ? AND email = ?", (_org(), (email or "").strip().lower())).rowcount == 1
