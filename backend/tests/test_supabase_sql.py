"""The SQL for Gridlytics' own Supabase project, `supabase/` (audit parity-supabase-auth-03: ported from saad-tahir/solarlytics-backend at c853130, its
supabase/ folder and backend/tests/test_supabase_sql.py, with the product's names changed), for the optional supabase mode that only the owner applies. Read
as text: nothing here connects to any database, and no Supabase project is ever called. The files are checked for what they make and what they leave out,
the rights they give the browser's roles, the shape `get_my_context` answers in (the shape companies.py reads, which local mode's `platform.context` gives
too), the undo, the read-only preflight and the owner's steps; and, Gridlytics' own, that every table, column, function and constraint the lifted code
(companies._fetch_context, store_founder._Project, api._project_members) calls or names is one the migration makes.

Where pglast (PostgreSQL's own parser, libpg_query) can be imported, every statement and every function body is also parsed. The app's environment does not
carry it, so that test is skipped there."""
import inspect
import json
import re
from pathlib import Path

import pytest

from gridlytics import api, auth, companies, store, store_founder
from gridlytics import platform as platformdb
from test_founder import _company, _console, _ok, clock, fdb, project  # noqa: F401  (fixtures: the console, a stand-in project)

REPO = Path(__file__).resolve().parents[2]
SUPABASE = REPO / "supabase"
MIGRATION = SUPABASE / "migrations" / "grid_001_accounts_and_companies.sql"
ROLLBACK = SUPABASE / "migrations" / "grid_001_rollback.sql"
PREFLIGHT = SUPABASE / "sql" / "grid_000_preflight.sql"
README = SUPABASE / "README.md"
SQL_FILES = (MIGRATION, ROLLBACK, PREFLIGHT)

#: Boxlytics' own objects, which Gridlytics has no use for: the tokens economy (011), simulation results (001, 004), the material catalogue (012), and the
#: platform settings and founder console of Boxlytics' project (010; Gridlytics' founder console is its own backend's)
BOXLYTICS_ONLY = ("token", "billing", "simulation", "material", "platform_settings", "founder", "admin_")
#: What a person or a company would do for themselves in Boxlytics: sign up, onboard, invite, approve, place, change roles and seats. None of it is in
#: Gridlytics, where the founder adds every company and person; nor are super-admins, whose work is the founder's service-role key here
SELF_SERVICE = ("company_invites", "invite", "create_company_signup", "request_join_existing", "restart_onboarding", "approve_company", "set_seat_limit",
                "place_user", "reject_user", "change_member_role", "remove_member", "transfer_ownership", "suspend_company", "reactivate_company",
                "seats_remaining", "seat_limit", "my_email", "my_role_in", "is_company_admin_or_owner", "companies_force_pending_on_insert")
#: The SQL types of the columns, as information_schema's udt_name gives them (the preflight compares with these)
UDT = {"uuid": "uuid", "text": "text", "boolean": "bool", "timestamptz": "timestamptz", "public.company_role": "company_role",
       "public.membership_status": "membership_status"}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(sql: str, strings: bool = True) -> str:
    """The SQL without its comments, and with `strings` False also without what its quoted strings hold, so that a check reads the statements and not the words
    that explain them. Dollar-quoted bodies stay: they are code, with comments and strings of their own."""
    out, i, n = [], 0, len(sql)
    while i < n:
        if sql.startswith("--", i):
            i = n if (j := sql.find("\n", i)) < 0 else j
            continue
        if sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
            continue
        if sql[i] == "'":
            j = i + 1
            while j < n:
                if sql.startswith("''", j):
                    j += 2
                elif sql[j] == "'":
                    break
                else:
                    j += 1
            out.append(sql[i:j + 1] if strings else "''")
            i = j + 1
            continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def _created(sql: str) -> dict[str, set[str]]:
    code = _code(sql)
    return {"type": set(re.findall(r"CREATE TYPE public\.(\w+)", code)),
            "table": set(re.findall(r"CREATE TABLE IF NOT EXISTS public\.(\w+)", code)),
            "function": set(re.findall(r"CREATE OR REPLACE FUNCTION public\.(\w+)", code)),
            "policy": set(re.findall(r'CREATE POLICY "(\w+)"', code)),
            "trigger": set(re.findall(r"CREATE OR REPLACE TRIGGER (\w+)", code)),
            "index": set(re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", code))}


def _function(code: str, name: str) -> tuple[str, str, str]:
    """The arguments, the head (from RETURNS to AS) and the body of the function `name`."""
    m = re.search(rf"CREATE OR REPLACE FUNCTION public\.{name}\(([^)]*)\)(.*?)AS \$\$(.*?)\$\$;", code, re.S)
    assert m, name
    return m.group(1), m.group(2), m.group(3)


def _arguments(text: str, start: int) -> list[str]:
    """The arguments of the call whose opening parenthesis is at `start`, split at its own commas."""
    depth, args, begin, i = 0, [], start + 1, start
    while True:
        c = text[i]
        if c == "'":
            i = text.index("'", i + 1)
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
            if depth == 0:
                args.append(text[begin:i].strip())
                return args
        elif c == "," and depth == 1:
            args.append(text[begin:i].strip())
            begin = i + 1
        i += 1


def _object(expr: str) -> dict[str, str]:
    """The keys of the first jsonb_build_object in `expr`, each with the text of its value."""
    m = re.search(r"jsonb_build_object\(", expr)
    assert m, expr
    args = _arguments(expr, m.end() - 1)
    assert len(args) % 2 == 0, args
    keys = [k.strip("'") for k in args[::2]]
    assert len(set(keys)) == len(keys), keys
    return dict(zip(keys, args[1::2]))


def _columns(code: str) -> dict[str, dict[str, str]]:
    """Each table's columns with their types, from its CREATE TABLE."""
    tables = {}
    for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS public\.(\w+) \((.*?)\n\);", code, re.S):
        tables[name] = {m.group(1): m.group(2).lower() for m in re.finditer(r"^\s+(\w+)\s+(UUID|TEXT|BOOLEAN|TIMESTAMPTZ|public\.\w+)\b", body, re.M)}
    return tables


@pytest.fixture
def local_context(tmp_path, monkeypatch):
    """What local mode gives for `get_my_context` (platform.context): an owner of one company, in a registry of the test's own."""
    monkeypatch.delenv("GRIDLYTICS_DATA_DIR", raising=False)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "gridlytics.db")
    monkeypatch.setattr(auth, "ITERATIONS", 1000)
    company = platformdb.create_company("Acme Power", enabled=True)
    account = platformdb.create_account("saad@example.pk", "Saad Khan", "a long enough password")
    platformdb.add_membership(account["id"], company["id"], "owner")
    return platformdb.context(account["id"])


def test_the_folder_holds_the_migration_its_undo_the_preflight_and_the_owners_steps():
    for path in (*SQL_FILES, README):
        assert path.is_file(), path
    # applied by hand, never by `supabase db push`, whose migrations are named <timestamp>_<name>.sql: these names keep the CLI from taking them
    for path in (SUPABASE / "migrations").iterdir():
        assert not re.match(r"\d{14}_", path.name), path


def test_the_migration_makes_the_accounts_and_companies_and_nothing_else():
    made = _created(_text(MIGRATION))
    assert made["type"] == {"company_role", "membership_status"}
    assert made["table"] == {"profiles", "companies", "company_memberships"}
    assert made["function"] == {"is_active_member", "get_my_context", "company_list_members", "handle_new_user", "touch_updated_at",
                                "memberships_stamp_activated"}
    assert made["trigger"] == {"on_auth_user_created", "trg_profiles_updated_at", "trg_memberships_activated"}
    code = _code(_text(MIGRATION))
    # the company roles and statuses of Boxlytics (001), which platform.db keeps too (P1)
    assert "ENUM ('owner', 'admin', 'member')" in code and "ENUM ('pending', 'active', 'suspended')" in code
    assert tuple(platformdb.COMPANY_ROLES) == ("owner", "admin", "member") and tuple(platformdb.STATUSES) == ("pending", "active", "suspended")


def test_no_boxlytics_only_object_and_no_self_service_is_copied():
    for path in SQL_FILES:
        code = _code(_text(path)).lower()
        # three keys of the context's answer stay for its shape, as constants (no invitations, no onboarding, no super-admins): nothing else of theirs is here
        shape = re.sub(r"'pending_invites_for_email',\s*'\[\]'::jsonb|'assignment_status',\s*'none'|'is_super_admin',\s*false", "", code)
        for word in BOXLYTICS_ONLY + SELF_SERVICE + ("assignment_status", "is_super_admin"):
            assert word not in shape, (path.name, word)


def test_get_my_context_answers_in_the_shape_the_backend_reads(local_context):
    args, head, body = _function(_code(_text(MIGRATION)), "get_my_context")
    assert args == "" and re.search(r"RETURNS JSONB", head)
    top = _object(body)
    assert set(top) == set(local_context) == {"profile", "memberships", "pending_invites_for_email"}
    # no invitations in Gridlytics: always empty, as in local mode
    assert top["pending_invites_for_email"] == "'[]'::jsonb" and local_context["pending_invites_for_email"] == []
    profile = _object(top["profile"])
    assert set(profile) == set(local_context["profile"])
    assert profile["is_super_admin"] == "false" and profile["assignment_status"] == "'none'" and profile["requested_company_name"] == "NULL"
    # local mode's one-time password (auth-security): Supabase Auth makes none, so the project's answer says false, and the console refuses one there
    assert profile["must_change_password"] == "false" and local_context["profile"]["must_change_password"] is False
    membership = _object(top["memberships"])
    assert set(membership) == set(local_context["memberships"][0])
    company = _object(membership["company"])
    # what companies.py reads; whether Gridlytics is on for the company and its seats are platform.db's, which local mode adds
    assert set(company) == {"id", "name", "verified"}
    assert set(company) <= set(local_context["memberships"][0]["company"])
    assert local_context["memberships"][0]["company"]["verified"] is True


def test_get_my_context_reads_only_the_callers_own_rows_and_nothing_for_a_banned_or_deleted_account():
    _, head, body = _function(_code(_text(MIGRATION)), "get_my_context")
    assert re.search(r"v_uid\s+UUID := auth\.uid\(\);", body)
    assert re.search(r"IF v_uid IS NULL THEN\s+RETURN NULL;", body)
    assert re.search(r"WHERE u\.id = v_uid", body) and re.search(r"WHERE m\.user_id = u\.id", body)
    # the founder's "disable" is a ban in supabase mode: like a disabled account in local mode (platform.context gives None), the context is then null
    assert re.search(r"u\.deleted_at IS NULL", body) and re.search(r"u\.banned_until IS NULL OR u\.banned_until <= now\(\)", body)
    assert "STABLE" in head and "SECURITY DEFINER" in head


def test_company_list_members_keeps_the_columns_of_boxlytics_003_and_answers_only_the_companys_owner_or_admin():
    args, head, body = _function(_code(_text(MIGRATION)), "company_list_members")
    assert args == "p_company_id UUID"
    columns = re.search(r"RETURNS TABLE \((.*?)\)\s+LANGUAGE", head, re.S).group(1)
    assert [c.split()[0] for c in columns.split(",")] == ["membership_id", "user_id", "email", "first_name", "last_name", "avatar_url", "role", "status",
                                                           "created_at", "activated_at"]
    # Gridlytics' roles live in the company's database, so this project cannot tell a Gridlytics administrator: the backend asks for manage_users first.
    # Refused with PostgreSQL's insufficient_privilege, which PostgREST answers with 403
    # only the company's owner or an admin, as Boxlytics' 003: the members' addresses are not every member's to read
    assert re.search(r"IF NOT EXISTS \(SELECT 1 FROM public\.company_memberships AS me\s+WHERE me\.company_id = p_company_id AND me\.user_id = auth\.uid\(\) "
                     r"AND me\.status = 'active' AND me\.role IN \('owner', 'admin'\)\) THEN\s+RAISE EXCEPTION '(?:[^']|'')+' USING ERRCODE = '42501';", body)
    assert re.search(r"WHERE m\.company_id = p_company_id", body)


def test_every_table_has_row_level_security_and_every_policy_only_reads():
    code = _code(_text(MIGRATION))
    made = _created(_text(MIGRATION))
    for table in made["table"]:
        assert re.search(rf"ALTER TABLE public\.{table}\s+ENABLE ROW LEVEL SECURITY;", code), table
    # writes are the founder's, through the service-role key, which row-level security does not bind (store_founder._Project)
    policies = re.findall(r'CREATE POLICY "(\w+)" ON public\.(\w+)\s+FOR (\w+)', code)
    assert len(policies) == len(made["policy"]) == len(re.findall(r"CREATE POLICY", code))
    assert {command for _, _, command in policies} == {"SELECT"}
    assert {(name, table) for name, table, _ in policies} == {
        ("profiles_self_read", "profiles"), ("profiles_same_company_read", "profiles"), ("companies_member_read", "companies"),
        ("memberships_self_read", "company_memberships"), ("memberships_company_peers_read", "company_memberships")}
    # 009's lesson: no policy reads auth.users (the browser's role may not), only auth.uid()
    for statement in re.findall(r"CREATE POLICY .*?;", code, re.S):
        assert "auth.users" not in statement


def test_the_browser_roles_get_only_reading_and_the_three_functions_they_call():
    code = _code(_text(MIGRATION))
    made = _created(_text(MIGRATION))
    grants = re.findall(r"GRANT (.+?) ON (TABLE|FUNCTION) (.+?) TO ([\w, ]+);", code, re.S)
    assert grants
    for rights, kind, objects, roles in grants:
        roles = {r.strip() for r in roles.split(",")}
        assert not roles & {"anon", "PUBLIC", "public"}, objects
        if "authenticated" in roles:
            assert rights == ("SELECT" if kind == "TABLE" else "EXECUTE"), objects
    revoked = re.findall(r"REVOKE ALL ON (TABLE|FUNCTION) (.+?) FROM (.+?);", code, re.S)
    for table in made["table"]:
        assert any(kind == "TABLE" and f"public.{table}" in objects and {"PUBLIC", "anon", "authenticated"} <= {r.strip() for r in roles.split(",")}
                   for kind, objects, roles in revoked), table
    for function in made["function"]:
        assert any(kind == "FUNCTION" and f"public.{function}(" in objects and {"PUBLIC", "anon"} <= {r.strip() for r in roles.split(",")}
                   for kind, objects, roles in revoked), function
    called = {re.search(r"public\.(\w+)\(", objects).group(1) for _, kind, objects, roles in grants
              if kind == "FUNCTION" and "authenticated" in roles}
    assert called == {"is_active_member", "get_my_context", "company_list_members"}
    # the founder console reads and writes the tables with the service-role key (store_founder._Project)
    assert any(kind == "TABLE" and rights == "ALL" and "service_role" in roles and all(f"public.{t}" in objects for t in made["table"])
               for rights, kind, objects, roles in grants)


def test_every_function_pins_an_empty_search_path_and_names_every_table_by_its_schema():
    code = _code(_text(MIGRATION))
    definers = set()
    for name in _created(_text(MIGRATION))["function"]:
        _, head, body = _function(code, name)
        assert "SET search_path = ''" in head, name
        if "SECURITY DEFINER" in head:
            definers.add(name)
        for table in re.findall(r"\b(?:FROM|JOIN|INSERT INTO|UPDATE)\s+([\w.]+)", body):
            assert table.startswith(("public.", "auth.")), (name, table)
    assert definers == {"is_active_member", "get_my_context", "company_list_members", "handle_new_user"}


def test_the_migration_is_one_transaction_and_can_be_run_again():
    code = _code(_text(MIGRATION))
    statements = [s.strip() for s in re.split(r";\s*\n", code) if s.strip()]
    assert statements[0] == "BEGIN" and statements[-1].rstrip(";") == "COMMIT"
    assert not re.search(r"CREATE TABLE (?!IF NOT EXISTS)", code) and not re.search(r"CREATE (UNIQUE )?INDEX (?!IF NOT EXISTS)", code)
    assert not re.search(r"CREATE FUNCTION", code)
    for name, table in re.findall(r'CREATE POLICY "(\w+)" ON ([\w.]+)', code):
        assert f'DROP POLICY IF EXISTS "{name}" ON {table};' in code, name
    # a trigger is replaced in place, never dropped: dropping one needs the table's ownership, and Supabase's role `postgres` does not own auth.users
    # (CREATE OR REPLACE TRIGGER needs only the TRIGGER right, PostgreSQL 14 and later)
    assert not re.search(r"CREATE TRIGGER", code) and "DROP TRIGGER" not in code
    assert len(re.findall(r"CREATE OR REPLACE TRIGGER (\w+)\s+\w+ [\w ]+? ON ([\w.]+)", code)) == 3
    for kind in ("company_role", "membership_status"):
        assert f"IF to_regtype('public.{kind}') IS NULL THEN" in code


def test_a_company_name_has_the_registrys_bounds():
    code = _code(_text(MIGRATION))
    most = int(re.search(r"CHECK \(char_length\(btrim\(name\)\) BETWEEN 1 AND (\d+)\)", code).group(1))
    assert platformdb._name("x" * most) and platformdb._name(" x ") == "x"
    for bad in ("", "  ", "x" * (most + 1)):
        with pytest.raises(ValueError):
            platformdb._name(bad)


def test_each_piece_names_its_boxlytics_source():
    sql = _text(MIGRATION)
    assert "boxlytics@c429bef" in sql
    for source in ("001_multitenant.sql", "003_company_views.sql", "009_fix_invites_rls.sql", "011_token_economy.sql"):
        assert source in sql, source
    lines = sql.splitlines()
    for at, line in enumerate(lines):
        if re.match(r"CREATE (TABLE|OR REPLACE FUNCTION) ", line):
            above = []
            for previous in reversed(lines[:at]):
                if not previous.startswith("--"):
                    break
                above.append(previous)
            assert re.search(r"\b(001|003|009|011)\b|\bNew\b", " ".join(above)), line


def test_the_rollback_undoes_everything_the_migration_makes_in_an_order_that_needs_no_cascade():
    made = _created(_text(MIGRATION))
    migration = _code(_text(MIGRATION))
    code = _code(_text(ROLLBACK))
    statements = [s.strip() for s in code.split(";") if s.strip()]
    assert statements[0] == "BEGIN" and statements[-1] == "COMMIT"
    assert all(s.startswith("DROP ") for s in statements[1:-1]), statements
    assert "CASCADE" not in code
    for table in made["table"]:
        assert f"DROP TABLE IF EXISTS public.{table}" in statements, table
    for name in made["function"]:
        args, _, _ = _function(migration, name)
        types = ", ".join(a.split()[-1] for a in args.split(",") if a.strip())
        assert f"DROP FUNCTION IF EXISTS public.{name}({types})" in statements, name
    for kind in made["type"]:
        assert f"DROP TYPE IF EXISTS public.{kind}" in statements, kind
    # a trigger on a table of ours goes with the table; the one on Supabase's accounts is dropped by name, and nothing else of the auth schema is touched
    for name, table in re.findall(r"CREATE OR REPLACE TRIGGER (\w+)\s+\w+ [\w ]+? ON ([\w.]+)", migration):
        assert table == "auth.users" or table.removeprefix("public.") in made["table"], name
    assert [s for s in statements if "auth." in s] == ["DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users"]
    at = {s: n for n, s in enumerate(statements)}

    def order(first, then):
        assert at[first] < at[then], (first, then)
    # profiles' policy reads company_memberships, whose rows point at companies; the policies call is_active_member; the columns and company_list_members'
    # result use the types; the trigger on auth.users calls handle_new_user
    order("DROP TABLE IF EXISTS public.profiles", "DROP TABLE IF EXISTS public.company_memberships")
    order("DROP TABLE IF EXISTS public.company_memberships", "DROP TABLE IF EXISTS public.companies")
    for table in made["table"]:
        order(f"DROP TABLE IF EXISTS public.{table}", "DROP FUNCTION IF EXISTS public.is_active_member(UUID)")
        for kind in made["type"]:
            order(f"DROP TABLE IF EXISTS public.{table}", f"DROP TYPE IF EXISTS public.{kind}")
    for kind in made["type"]:
        order("DROP FUNCTION IF EXISTS public.company_list_members(UUID)", f"DROP TYPE IF EXISTS public.{kind}")
    order("DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users", "DROP FUNCTION IF EXISTS public.handle_new_user()")
    order("DROP TABLE IF EXISTS public.profiles", "DROP FUNCTION IF EXISTS public.touch_updated_at()")
    order("DROP TABLE IF EXISTS public.company_memberships", "DROP FUNCTION IF EXISTS public.memberships_stamp_activated()")


def test_the_preflight_is_one_select_that_changes_nothing():
    code = _code(_text(PREFLIGHT), strings=False)
    statements = [s for s in code.split(";") if s.strip()]
    assert len(statements) == 1 and re.match(r"\s*(WITH|SELECT)\b", statements[0])
    for word in ("INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "ALTER", "DROP", "GRANT", "REVOKE", "TRUNCATE", "COPY", "DO", "CALL", "SET", "RESET",
                 "VACUUM", "LOCK", "COMMENT", "NOTIFY", "BEGIN", "COMMIT", "set_config", "nextval", "setval", "pg_terminate_backend", "pg_cancel_backend",
                 "pg_advisory_lock", "lo_import", "dblink", "pg_read_file", "get_my_context", "company_list_members"):
        assert not re.search(rf"\b{word}\b", code, re.I), word


def test_the_preflight_checks_every_object_column_policy_and_right_the_migration_makes():
    text = _code(_text(PREFLIGHT))
    made = _created(_text(MIGRATION))
    for kind, names in made.items():
        for name in names:
            assert name in text, (kind, name)
    for table, columns in _columns(_code(_text(MIGRATION))).items():
        assert columns, table
        for column, sql_type in columns.items():
            assert f"('{table}', '{column}', '{UDT[sql_type]}')" in text, (table, column)
    for role in ("anon", "authenticated", "service_role"):
        assert f"'{role}'" in text
    # the steps the README tells the owner to read: ready before the migration, applied after it
    assert "'ready'" in text and "'applied'" in text


def test_the_preflight_checks_every_column_of_supabases_accounts_that_the_functions_read():
    """auth.users is Supabase Auth's, not the migration's: a project whose accounts lack a column get_my_context reads (banned_until, deleted_at) would take
    the migration and then fail at the first sign-in, so the preflight's "ready" step looks for each one."""
    code = _code(_text(MIGRATION))
    read = set()
    for name in ("get_my_context", "company_list_members"):
        _, _, body = _function(code, name)
        assert re.search(r"\bauth\.users u\b", body), name
        read |= set(re.findall(r"\bu\.(\w+)", body))
    _, _, body = _function(code, "handle_new_user")
    read |= {"id"} | set(re.findall(r"\bNEW\.(\w+)", body))
    assert {"id", "email", "raw_user_meta_data", "banned_until", "deleted_at"} <= read
    listed = re.search(r"auth_columns \(col\) AS \(\s*VALUES (.*?)\n\)", _code(_text(PREFLIGHT)), re.S)
    assert listed and set(re.findall(r"\('(\w+)'\)", listed.group(1))) == read
    assert re.search(r"'ready', 'Supabase Auth''s accounts: column auth\.users\.' \|\| a\.col", _code(_text(PREFLIGHT)))


def test_the_triggers_make_a_profile_per_account_and_stamp_a_membership_when_it_first_becomes_active():
    code = _code(_text(MIGRATION))
    triggers = {name: (when, table, function) for name, when, table, function in
                re.findall(r"CREATE OR REPLACE TRIGGER (\w+)\s+(\w+ [\w ]+?) ON ([\w.]+)\s+FOR EACH ROW EXECUTE FUNCTION public\.(\w+)\(\);", code)}
    assert triggers == {"on_auth_user_created": ("AFTER INSERT", "auth.users", "handle_new_user"),
                        "trg_profiles_updated_at": ("BEFORE UPDATE", "public.profiles", "touch_updated_at"),
                        "trg_memberships_activated": ("BEFORE INSERT OR UPDATE OF status", "public.company_memberships", "memberships_stamp_activated")}
    # the founder console gives the name as full_name (store_founder._Project._new_user); it fills first and last name, whichever is missing, as 001 does
    _, _, body = _function(code, "handle_new_user")
    for key in ("first_name", "last_name", "full_name", "name", "avatar_url"):
        assert f"NEW.raw_user_meta_data->>'{key}'" in body, key
    assert re.search(r"IF v_first IS NULL AND v_full IS NOT NULL THEN\s+v_first := split_part\(v_full, ' ', 1\);\s+END IF;", body)
    assert re.search(r"IF v_last IS NULL AND v_full IS NOT NULL AND strpos\(v_full, ' '\) > 0 THEN", body)
    assert re.search(r"INSERT INTO public\.profiles \(id, first_name, last_name, avatar_url\)\s+VALUES \(NEW\.id,.*?ON CONFLICT \(id\) DO NOTHING;", body, re.S)
    # activated_at once, the first time the membership is active, as platform.set_membership keeps it (COALESCE(activated, ...))
    _, _, body = _function(code, "memberships_stamp_activated")
    assert re.search(r"IF NEW\.status = 'active' AND NEW\.activated_at IS NULL THEN\s+NEW\.activated_at := now\(\);\s+END IF;\s+RETURN NEW;", body)
    registry = _text(REPO / "backend" / "gridlytics" / "platform.py")
    assert "activated = COALESCE(activated, CASE WHEN ? = 'active' THEN ? END)" in registry


def test_the_readme_gives_the_owners_steps_and_names_the_keys():
    text = _text(README)
    for step in ("Create the project", "grid_000_preflight.sql", "grid_001_accounts_and_companies.sql", "grid_001_rollback.sql", "Allow new users to sign up",
                 "Google", "Site URL", "Redirect URLs", "/reset-password", "service-role key"):
        assert step in text, step
    identity = _text(REPO / "backend" / "gridlytics" / "identity.py")
    for name in sorted(set(re.findall(r"GRIDLYTICS_(?:SUPABASE|JWT|JWKS)_\w+", identity))) + [
            "GRIDLYTICS_AUTH_MODE", "GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY", "VITE_AUTH_MODE", "VITE_SUPABASE_URL", "VITE_SUPABASE_ANON_KEY"]:
        assert name in text, name
    # the service-role key passes every policy: the server's only, never the web app's
    line = next(line for line in text.splitlines() if "GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY" in line)
    assert "server" in line and "never" in line
    assert "Boxlytics" in text and "never applied" in text.lower()


def test_no_key_secret_or_project_address_is_written():
    files = [p for p in SUPABASE.rglob("*") if p.is_file()]
    assert len(files) >= 4, files
    for path in files:
        text = _text(path)
        assert not re.search(r"eyJ[A-Za-z0-9_-]{8,}", text), path            # a JWT, as the legacy anon and service-role keys are
        assert not re.search(r"sb_(?:publishable|secret)_[A-Za-z0-9_-]{6,}", text), path
        assert not re.search(r"\b[a-z0-9]{20}\.supabase\.(?:co|in)\b", text), path   # a real project's address: its ref is 20 characters
        assert not re.search(r"postgres(?:ql)?://[^\s<>]*:[^\s<>@]+@", text), path   # a connection string with a password in it


def _plpgsql_queries(node):
    """Every SQL text inside a parsed PL/pgSQL function, with the mode it is parsed in."""
    if isinstance(node, dict):
        if "PLpgSQL_expr" in node:
            expr = node["PLpgSQL_expr"]
            yield expr["query"], expr.get("parseMode", 0)
        for value in node.values():
            yield from _plpgsql_queries(value)
    elif isinstance(node, list):
        for value in node:
            yield from _plpgsql_queries(value)


def test_every_statement_and_every_function_body_parses():
    pglast = pytest.importorskip("pglast")
    kinds = {}
    for path in SQL_FILES:
        sql = _text(path)
        raw = sql.encode("utf-8")
        statements = pglast.parse_sql(sql)
        kinds[path] = [type(s.stmt).__name__ for s in statements]
        for s in statements:
            # a length of 0 means "to the end of the text" (libpg_query)
            end = s.stmt_location + s.stmt_len if s.stmt_len else len(raw)
            stmt, text = s.stmt, raw[s.stmt_location:end].decode("utf-8")
            if type(stmt).__name__ == "CreateFunctionStmt":
                options = {o.defname: o.arg for o in stmt.options}
                language, body = options["language"].sval, options["as"][0].sval
            elif type(stmt).__name__ == "DoStmt":
                language, body = "plpgsql", next(o.arg.sval for o in stmt.args if o.defname == "as")
                text = f"CREATE FUNCTION pg_temp.grid_do() RETURNS void LANGUAGE plpgsql AS $grid$\n{body}\n$grid$"
            else:
                continue
            if language == "sql":
                assert pglast.parse_sql(body), text
                continue
            assert language == "plpgsql", language
            for query, mode in _plpgsql_queries(pglast.parse_plpgsql(text)):
                # 0: a statement; 2: an expression; 3 to 5: an assignment, "target := expression"
                pglast.parse_sql(query if mode == 0 else "SELECT " + query.split(":=", 1)[-1])
    assert kinds[PREFLIGHT] == ["SelectStmt"]
    assert set(kinds[ROLLBACK]) == {"TransactionStmt", "DropStmt"}
    assert kinds[MIGRATION][0] == kinds[MIGRATION][-1] == "TransactionStmt"


# ================================================================== Gridlytics' own: the lifted code and the SQL agree (audit parity-supabase-auth-03)

def _made_columns() -> dict[str, set[str]]:
    return {table: set(columns) for table, columns in _columns(_code(_text(MIGRATION))).items()}


def test_the_files_are_renamed_for_gridlytics_and_name_their_source():
    names = sorted(p.relative_to(SUPABASE).as_posix() for p in SUPABASE.rglob("*") if p.is_file())
    assert names == ["README.md", "migrations/grid_001_accounts_and_companies.sql", "migrations/grid_001_rollback.sql", "sql/grid_000_preflight.sql"]
    for path in (*SQL_FILES, README):
        text = _text(path)
        assert "Gridlytics" in text and "solar_00" not in _code(text) and "SOLARLYTICS_" not in text, path
        # Solarlytics is named only as the source of the port, with its commit, and as a project that is not this one
        assert "saad-tahir/solarlytics-backend" in text and "c853130" in text, path
        assert "for review" in text.lower(), path


def test_every_table_column_and_rpc_the_lifted_code_calls_is_one_the_migration_makes(project):
    """store_founder._Project reaches the project's tables with the service-role key, and companies._fetch_context its rpc/get_my_context with the person's
    token: each path, column, filter and constraint they name is made by grid_001 (the stand-in project of test_founder records the calls)."""
    c = _console()
    made = _company(c)
    cid, owner = made["company"]["id"], made["owner"]
    maaz = _ok(c.post("/api/founder/users", json={"email": "maaz@meow.test", "name": "Maaz Ali", "password": "maaz first password", "company_id": cid}),
               201)["user"]
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"status": "suspended"}))
    _ok(c.patch(f"/api/founder/users/{maaz['id']}/memberships/{cid}", json={"company_role": "owner", "status": "active"}))
    _ok(c.patch(f"/api/founder/users/{owner['id']}", json={"name": "Saad K. Khan"}))
    _ok(c.patch(f"/api/founder/companies/{cid}", json={"name": "Northwind Solar"}))
    _ok(c.get("/api/founder/users"))
    _ok(c.delete(f"/api/founder/users/{owner['id']}/memberships/{cid}"))
    columns, tables = _made_columns(), _created(_text(MIGRATION))["table"]
    seen = set()
    for method, path, body in project.calls:
        base, _, query = path.partition("?")
        if not base.startswith("/rest/v1/"):
            assert base.startswith("/auth/v1/admin/users"), path                   # the Admin API: Supabase Auth's own
            continue
        table = base.removeprefix("/rest/v1/")
        assert table in tables, path
        seen.add(table)
        args = dict(p.split("=", 1) for p in query.split("&") if p)
        named = set(args.get("select", "").split(",")) - {""}
        named |= {k for k, v in args.items() if v.startswith("eq.")}
        named |= {args["order"].split(".")[0]} if "order" in args else set()
        named |= set(body or {})
        assert named <= columns[table], (path, named - columns[table])
    assert seen == {"companies", "company_memberships", "profiles"}
    # the constraints whose refusals the console says in its own words
    code = _code(_text(MIGRATION))
    lifted = _text(Path(store_founder.__file__))
    for constraint in re.findall(r'"(companies_name_unique_lower|memberships_\w+)" in text', lifted):
        assert re.search(rf"\b{constraint}\b", code), constraint
    assert "CHECK (char_length(btrim(name)) BETWEEN 1 AND 80)" in code        # 23514, said as "a name of 1 to 80 characters"
    # the person's context: the rpc companies.py calls, which the browser's role may call
    assert re.findall(r"/rest/v1/rpc/(\w+)", _text(Path(companies.__file__))) == ["get_my_context"]
    assert re.search(r"GRANT EXECUTE ON FUNCTION public\.get_my_context\(\) TO authenticated", code)
    # the company's Users list in supabase mode (auth-security's api._project_members, audit parity-supabase-auth-05): the rpc it calls with the person's
    # token, by its argument's name, and the columns it reads are the migration's, and the browser's role may call it
    source = _text(Path(api.__file__))
    assert re.findall(r"/rest/v1/rpc/(\w+)", source) == ["company_list_members"] and '{"p_company_id": company_id}' in source
    args, head, _ = _function(code, "company_list_members")
    returned = {c.split()[0] for c in re.search(r"RETURNS TABLE \((.*?)\)\s+LANGUAGE", head, re.S).group(1).split(",")}
    read = {name for pair in re.findall(r'r\.get\("(\w+)"\)|r\["(\w+)"\]', inspect.getsource(api._project_members)) for name in pair if name}
    assert args == "p_company_id UUID" and read == {"user_id", "email", "first_name", "last_name", "role", "status"} and read <= returned
    assert re.search(r"GRANT EXECUTE ON FUNCTION public\.company_list_members\(UUID\) TO authenticated", code)


def test_the_test_fixture_of_get_my_context_has_the_migrations_shape():
    """test_companies answers its stand-in rpc/get_my_context with fixtures/get_my_context.json: the keys are the migration's."""
    fixture = json.loads((Path(__file__).parent / "fixtures" / "get_my_context.json").read_text(encoding="utf-8"))
    _, _, body = _function(_code(_text(MIGRATION)), "get_my_context")
    top = _object(body)
    assert set(fixture) == set(top)
    assert set(fixture["profile"]) == set(_object(top["profile"]))
    membership = _object(top["memberships"])
    for m in fixture["memberships"]:
        assert set(m) == set(membership) and set(m["company"]) == set(_object(membership["company"]))
    # every status the migration's type has is in the fixture, so the backend's refusals for each are tested from it (audit parity-supabase-auth-09)
    assert {m["status"] for m in fixture["memberships"]} == set(platformdb.STATUSES)


def test_the_owners_steps_say_the_apps_password_rule_and_no_other_way_in():
    text = _text(README)
    least = int(re.search(r"set the minimum length to \*\*(\d+)\*\*", text).group(1))
    assert auth.password_problem("p" * least) is None and auth.password_problem("p" * (least - 1)) is not None
    assert "72 characters" in text and str(store_founder.PROJECT_PASSWORD_BYTES) == "72"
    # preassigned accounts only: no sign-up, no Google or other provider, no link sign-in, no invitation
    for words in ("turn off **Allow new users to sign up**", "turn off **Google** and every other provider", "keep **anonymous sign-ins** off",
                  "no `/auth/callback`", "no `/invite/**`", "never by `supabase db push`"):
        assert words in text, words
    assert "GRIDLYTICS_MEMBERSHIP_TTL" in text and companies.MEMBERSHIP_TTL == 60
