"""The two audit trails, filtered (UX backend ask K34; plan G9 item 3, G2.4, G4.5): the company's trail (`store.audit`, Company settings › Audit trail)
and the founder's (`platform.founder_audit`, the console's Audit).

Each action a trail writes belongs to one group, the "Kind of change" select's: one map per trail from the action's words to its group (`COMPANY_GROUPS`,
`FOUNDER_GROUPS`), read by the filter (as an SQLite function, so a page of lines and its count are one query each) and by the answer (`group` on each
line), so the select's groups and the server agree. `test_ux_k34_audit_filters` holds every `store.audit(...)` and `founder_audit(...)` call of the
backend to a group. An action no group names (a line of an older build) has no group: it is listed unfiltered, and under no group.

Filters: who (a person by their id; "demo" for the demo set-up; the actor's own word for the product's lines, such as "system" or "edge"), the kind of
change (a group's key), the site (company trail: the line's target, or the site its detail names) or the company (founder trail), From and To (Karachi
days, both included) and a search of who, what, of and the detail (a site's name finds its lines). Times are answered in Karachi time (ISO 8601,
+05:00). A page is PAGE lines, newest first, with `total`, the count of every line the filters match."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone

from . import store

PKT = timezone(timedelta(hours=5))
#: Lines a page holds (the trail's "Show older" pages, as before)
PAGE = 300
#: The lines a CSV export holds at most (the filtered trail, newest first)
EXPORT_MAX = 100_000
#: The longest text a filter takes
TEXT_MAX = 200

#: The company trail's groups, in the select's order: key, words
COMPANY_GROUP_WORDS = {"people": "People and keys", "sites": "Sites and set-up", "plans": "Plans and control", "tariffs": "Tariffs and money",
                       "savings": "Savings and pilot", "reports": "Reports and exports", "demo": "Demo"}
#: Each action the company trail writes, by its words, and its group
COMPANY_GROUPS = {
    **dict.fromkeys(("changed a user", "issued a one-time password", "cleared a sign-in lockout", "made an API key", "revoked an API key",
                     "linked a user of before", "changed their notification choices"), "people"),
    **dict.fromkeys(("added a site", "changed a site", "removed a site", "activated a site", "saved a connection", "removed a connection",
                     "saved the CSV column mapping", "uploaded telemetry", "recorded an outage's cause", "logged fuel",
                     "set the announced load-shedding schedule", "captured the inverter's native settings", "replaced the site's signing key",
                     "changed the alert rules", "changed a site's alert rules"), "sites"),
    **dict.fromkeys(("re-solved the plan", "marked a plan as applied", "set an override", "cleared an override", "started a fallback test",
                     "passed a fallback test", "demoted control: the clock is off", "restored control: the clock is set",
                     "changed the control tier the site's source supports", "changed a site's allowed control tier", "refreshed the forecasts",
                     "acknowledged an alert", "closed an alert", "assigned an alert"), "plans"),
    **dict.fromkeys(("recorded a fuel price's source", "discarded a tariff draft", "corrected a shipped tariff version", "added the shipped fuel prices",
                     "proposed a tariff version", "approved a tariff version", "withdrew a tariff version", "set a fuel price", "entered a DISCO bill",
                     "corrected a DISCO bill", "deleted a DISCO bill", "set the fee"), "tariffs"),
    **dict.fromkeys(("recomputed savings days", "re-scored later savings days", "re-scored a closed month's savings days", "painted the switchback calendar",
                     "amended the pilot", "changed the pilot set-up", "started the pilot"), "savings"),
    **dict.fromkeys(("exported telemetry", "exported savings", "exported the audit trail"), "reports"),
    **dict.fromkeys(("loaded the demo sites", "removed the demo sites"), "demo"),
}
#: Actions written with a part that varies, by their fixed start: "saved the battery", "answered a genset request: started", "generated a pilot report",
#: "downloaded a report (pdf)"; an exact action above is read first
COMPANY_PREFIXES = (("saved the ", "sites"), ("removed the ", "sites"), ("answered a genset request", "plans"), ("generated ", "reports"),
                    ("downloaded a report", "reports"))

#: The founder trail's groups, in the select's order
FOUNDER_GROUP_WORDS = {"signins": "Sign-ins", "refused": "Refused sign-ins", "companies": "Companies", "accounts": "Accounts and memberships",
                       "backups": "Backups", "requests": "Access requests"}
FOUNDER_GROUPS = {
    **dict.fromkeys(("signed in", "signed out", "ended every founder session"), "signins"),
    **dict.fromkeys(("sign-in refused", "code refused", "code step stopped"), "refused"),
    **dict.fromkeys(("made a company", "changed a company", "switched Gridlytics on", "switched Gridlytics off", "archived a company",
                     "brought a company back from the archive"), "companies"),
    **dict.fromkeys(("made an account", "set a password", "changed an account", "archived an account", "brought an account back from the archive",
                     "added a membership", "changed a membership", "removed a membership"), "accounts"),
    "made a backup": "backups",
    **dict.fromkeys(("changed an access request", "removed access requests past their keeping time"), "requests"),
}
#: The company trail's lines written while the demo sites were loaded (the frontend's trailWho: "demo", "demo seed", "demo, for <name>")
_DEMO = re.compile(r"^demo(?: seed)?(?:, for .+)?$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def company_group(action: str | None) -> str | None:
    """The group of a company trail's action, or None for one no group names."""
    if not isinstance(action, str):
        return None
    hit = COMPANY_GROUPS.get(action)
    return hit or next((g for p, g in COMPANY_PREFIXES if action.startswith(p)), None)


def founder_group(action: str | None) -> str | None:
    """The group of a founder trail's action, or None for one no group names."""
    return FOUNDER_GROUPS.get(action) if isinstance(action, str) else None


def who_of(actor: str | None, actor_id: str | None) -> str:
    """Who wrote a company trail's line, as the Who filter names them: "demo" for the demo set-up (whoever loaded it), the person's id, else the actor's
    own word ("system", "edge", "shipped"; "gridlytics" for none)."""
    actor = actor or ""
    if _DEMO.match(actor):
        return "demo"
    return actor_id or actor or "gridlytics"


def karachi(at: str | None) -> str | None:
    """A trail's stamp (UTC, ISO) in Karachi time, ISO 8601 with +05:00."""
    if not at:
        return None
    t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(PKT).isoformat(timespec="seconds")


class Refused(ValueError):
    """A filter that cannot be read, in one sentence."""


def _text(value, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > TEXT_MAX:
        raise Refused(f"{label} is text of at most {TEXT_MAX} characters")
    return value.strip() or None


def _day(value, label: str) -> date | None:
    if value is None or value == "":
        return None
    try:
        if not isinstance(value, str) or not _DAY.match(value.strip()):
            raise ValueError
        return date.fromisoformat(value.strip())
    except ValueError:
        raise Refused(f"{label} is a day written as 2026-11-01") from None


def _bounds(day_from, day_to) -> tuple[str | None, str | None]:
    """From and To (Karachi days, both included) as the stamps the trail keeps (UTC, ISO): the first moment of From and the first after To."""
    d0, d1 = _day(day_from, "From"), _day(day_to, "To")
    if d0 and d1 and d1 < d0:
        raise Refused("From must be on or before To")

    def utc(d: date) -> str:
        return datetime(d.year, d.month, d.day, tzinfo=PKT).astimezone(timezone.utc).isoformat(timespec="seconds")
    return (utc(d0) if d0 else None), (utc(d1 + timedelta(days=1)) if d1 else None)


def offset_of(value) -> int:
    if value is None or value == "":
        return 0
    try:
        n = int(str(value).strip())
    except ValueError:
        raise Refused("The offset is a whole number from 0") from None
    if n < 0:
        raise Refused("The offset is a whole number from 0")
    return n


def _group_key(value, words: dict) -> str | None:
    key = _text(value, "The kind of change")
    if key is not None and key not in words:
        raise Refused(f"The kind of change is one of {', '.join(words)}")
    return key


# ------------------------------------------------------------------------------------------------ the company's trail

def _company_where(who=None, group=None, site=None, day_from=None, day_to=None, q=None) -> tuple[str, list]:
    """The SQL of the company trail's filters (each checked; Refused in a sentence), on a connection with audit_group and audit_who."""
    who, site, q = _text(who, "Who"), _text(site, "The site"), _text(q, "The search")
    group = _group_key(group, COMPANY_GROUP_WORDS)
    t0, t1 = _bounds(day_from, day_to)
    where, args = ["org_id = ?"], [store.org()]
    if who:
        where.append("audit_who(actor, actor_id) = ?")
        args.append(who)
    if group:
        where.append("audit_group(action) = ?")
        args.append(group)
    if site:
        where.append("(target = ? OR json_extract(detail, '$.site_id') = ?)")
        args += [site, site]
    if t0:
        where.append("at >= ?")
        args.append(t0)
    if t1:
        where.append("at < ?")
        args.append(t1)
    if q:
        needle = q.lower()
        from . import sites
        named = [sid for sid, s in sites.names().items() if needle in (s.get("name") or "").lower()]
        cells = " || ' ' || ".join(f"lower(coalesce({c}, ''))" for c in ("actor", "actor_email", "action", "target", "detail"))
        hit = [f"instr({cells}, ?) > 0"]
        args.append(needle)
        if named:
            marks = ",".join("?" * len(named))
            hit.append(f"target IN ({marks}) OR json_extract(detail, '$.site_id') IN ({marks})")
            args += named + named
        where.append("(" + " OR ".join(hit) + ")")
    return " AND ".join(where), args


def _company_con(con) -> None:
    con.create_function("audit_group", 1, company_group, deterministic=True)
    con.create_function("audit_who", 2, who_of, deterministic=True)


def _company_line(r, names: dict) -> dict:
    detail = json.loads(r["detail"]) if r["detail"] else {}
    named = detail.get("site_id") if isinstance(detail, dict) else None
    site_id = r["target"] if r["target"] in names else (named if isinstance(named, str) and named in names else None)
    site = names.get(site_id) or {}
    return {"at": karachi(r["at"]), "actor": r["actor"], "actor_id": r["actor_id"], "actor_email": r["actor_email"], "action": r["action"],
            "target": r["target"], "detail": detail, "group": company_group(r["action"]), "who": who_of(r["actor"], r["actor_id"]),
            "site_id": site_id, "site_name": site.get("name"), "site_removed": bool(site.get("removed")) if site_id else None}


def company_people() -> list[dict]:
    """Who is in the company's trail, for the Who select: each `who` (as who_of names them) with the name and email of their newest line, people by name
    first, then the demo set-up and the product's own lines."""
    with store.connect() as con:
        _company_con(con)
        rows = con.execute("SELECT audit_who(actor, actor_id) AS who, actor, actor_email, MAX(id) AS last FROM audit WHERE org_id = ? "
                           "GROUP BY audit_who(actor, actor_id)", (store.org(),)).fetchall()
        newest = {r["who"]: con.execute("SELECT actor, actor_email, actor_id FROM audit WHERE id = ?", (r["last"],)).fetchone() for r in rows}
    out = [{"who": w, "name": "demo" if w == "demo" else (n["actor"] or ""), "email": "" if w == "demo" else (n["actor_email"] or ""),
            "person": w != "demo" and bool(n["actor_id"]) and w == n["actor_id"]} for w, n in newest.items()]
    return sorted(out, key=lambda p: (not p["person"], p["who"] != "demo", (p["name"] or p["who"]).lower()))


def company_search(who=None, group=None, site=None, day_from=None, day_to=None, q=None, offset=0, limit: int = PAGE) -> dict:
    """A page of the company's trail, newest first, matching the filters, with `total` (every line they match), the groups (key and words, the select's
    order) and who is in the trail (`people`)."""
    where, args = _company_where(who, group, site, day_from, day_to, q)
    offset = offset_of(offset)
    from . import sites
    names = sites.names()
    with store.connect() as con:
        _company_con(con)
        total = con.execute(f"SELECT COUNT(*) FROM audit WHERE {where}", args).fetchone()[0]
        rows = con.execute(f"SELECT at, actor, actor_id, actor_email, action, target, detail FROM audit WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                           [*args, limit, offset]).fetchall()
    return {"entries": [_company_line(r, names) for r in rows], "total": total, "offset": offset, "limit": limit,
            "groups": [{"key": k, "words": w} for k, w in COMPANY_GROUP_WORDS.items()], "people": company_people()}


#: The company trail's CSV columns
CSV_COLUMNS = ("at_karachi", "who", "email", "action", "group", "target", "site", "detail")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(v) -> str:
    """A CSV cell: blank for none, and a text a spreadsheet would read as a formula quote-prefixed (as the telemetry export writes it)."""
    if v is None:
        return ""
    v = str(v)
    return "'" + v if v.startswith(_FORMULA_START) else v


def company_csv(who=None, group=None, site=None, day_from=None, day_to=None, q=None) -> tuple[str, int]:
    """The filtered company trail as CSV, newest first (at most EXPORT_MAX lines): the time in Karachi time (ISO 8601 with its offset), who and their
    email, the action, its group's words, what it was of (the target), the site's name, and the detail as JSON. With the count of lines written."""
    where, args = _company_where(who, group, site, day_from, day_to, q)
    from . import sites
    names = sites.names()
    with store.connect() as con:
        _company_con(con)
        rows = con.execute(f"SELECT at, actor, actor_id, actor_email, action, target, detail FROM audit WHERE {where} ORDER BY id DESC LIMIT ?",
                           [*args, EXPORT_MAX]).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        line = _company_line(r, names)
        w.writerow([_cell(x) for x in (line["at"], line["actor"], line["actor_email"], line["action"],
                                       COMPANY_GROUP_WORDS.get(line["group"] or "", ""), line["target"], line["site_name"],
                                       json.dumps(line["detail"], ensure_ascii=False, sort_keys=True))])
    return buf.getvalue(), len(rows)


# ------------------------------------------------------------------------------------------------ the founder's trail

def founder_search(group=None, company=None, day_from=None, day_to=None, q=None, offset=0, limit: int = PAGE) -> dict:
    """A page of the founder's trail, newest first, matching the filters (the kind of change, the company, From and To, the search of who, what, of
    and the detail), with `total` and the groups. A line is of a company when it was written with one (platform.founder_audit's `company_id`)."""
    from . import platform as platformdb
    company, q = _text(company, "The company"), _text(q, "The search")
    group = _group_key(group, FOUNDER_GROUP_WORDS)
    t0, t1 = _bounds(day_from, day_to)
    offset = offset_of(offset)
    where, args = ["1 = 1"], []
    if group:
        where.append("audit_group(action) = ?")
        args.append(group)
    if company:
        where.append("company_id = ?")
        args.append(company)
    if t0:
        where.append("at >= ?")
        args.append(t0)
    if t1:
        where.append("at < ?")
        args.append(t1)
    if q:
        cells = " || ' ' || ".join(f"lower(coalesce({c}, ''))" for c in ("actor", "action", "target", "detail"))
        where.append(f"instr({cells}, ?) > 0")
        args.append(q.lower())
    sql = " AND ".join(where)
    names = {c["id"]: c["name"] for c in platformdb.list_companies()}
    with platformdb.reading() as con:
        con.create_function("audit_group", 1, founder_group, deterministic=True)
        total = con.execute(f"SELECT COUNT(*) FROM founder_audit WHERE {sql}", args).fetchone()[0]
        rows = con.execute(f"SELECT at, actor, action, target, detail, company_id FROM founder_audit WHERE {sql} ORDER BY id DESC LIMIT ? OFFSET ?",
                           [*args, limit, offset]).fetchall()
    entries = [{"at": karachi(r["at"]), "actor": r["actor"], "action": r["action"], "target": r["target"], "detail": json.loads(r["detail"] or "{}"),
                "group": founder_group(r["action"]), "company_id": r["company_id"] or None, "company_name": names.get(r["company_id"])}
               for r in rows]
    return {"entries": entries, "total": total, "offset": offset, "limit": limit,
            "groups": [{"key": k, "words": w} for k, w in FOUNDER_GROUP_WORDS.items()]}
