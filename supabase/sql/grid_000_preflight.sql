-- ===========================================================================================================================================================
-- Gridlytics, preflight for migration grid_001: for Gridlytics' own Supabase project only, never Boxlytics' or Solarlytics'
-- ===========================================================================================================================================================
-- READ ONLY. One SELECT over the catalogue: it changes nothing, makes nothing and calls none of the migration's functions. Run it in the SQL Editor (or psql)
-- before the migration and after it. It answers one row per check: step, item, result ("ok" or "NOT OK") and a detail.
--
--   step "ready"    what the migration needs of the project: PostgreSQL 15 or later, Supabase Auth's accounts with the columns the migration's functions
--                   read (get_my_context reads banned_until and deleted_at, for instance) and auth.uid(), the roles anon, authenticated and service_role,
--                   gen_random_uuid(). Every row must read ok before the migration is applied.
--   step "applied"  what grid_001_accounts_and_companies.sql makes: types, tables and their columns, indexes, row-level security and its policies (reading
--                   only), the browser's roles' rights (authenticated reads the tables and calls the two RPCs; anon gets nothing), the service role's rights,
--                   the functions (return type, SECURITY DEFINER, a pinned search_path) and the triggers.
--                   On a new project, before the migration, every row reads NOT OK. One that reads ok then means the project already holds something of that
--                   name: stop and look before applying. After the migration every row must read ok.
--
-- What it cannot see: the sign-in settings (sign-ups and Google off, the redirect URLs), which live outside the database. supabase/README.md lists them.
--
-- Ported for review only (audit parity-supabase-auth-03) from saad-tahir/solarlytics-backend at c853130, supabase/sql/solar_000_preflight.sql, with the
-- product's names changed; the checks are the same, the migration being the same.
-- ===========================================================================================================================================================

WITH
-- the columns of Supabase Auth's accounts that the migration's functions read: handle_new_user (the metadata), get_my_context and company_list_members
auth_columns (col) AS (
  VALUES ('id'), ('email'), ('raw_user_meta_data'), ('created_at'), ('updated_at'), ('banned_until'), ('deleted_at')
),
tables (tbl) AS (
  VALUES ('profiles'), ('companies'), ('company_memberships')
),
expected_columns (tbl, col, udt) AS (
  VALUES
    ('profiles', 'id', 'uuid'),
    ('profiles', 'first_name', 'text'),
    ('profiles', 'last_name', 'text'),
    ('profiles', 'avatar_url', 'text'),
    ('profiles', 'created_at', 'timestamptz'),
    ('profiles', 'updated_at', 'timestamptz'),
    ('companies', 'id', 'uuid'),
    ('companies', 'name', 'text'),
    ('companies', 'verified', 'bool'),
    ('companies', 'created_at', 'timestamptz'),
    ('company_memberships', 'id', 'uuid'),
    ('company_memberships', 'user_id', 'uuid'),
    ('company_memberships', 'company_id', 'uuid'),
    ('company_memberships', 'role', 'company_role'),
    ('company_memberships', 'status', 'membership_status'),
    ('company_memberships', 'created_at', 'timestamptz'),
    ('company_memberships', 'activated_at', 'timestamptz')
),
expected_indexes (idx) AS (
  VALUES ('companies_name_unique_lower'), ('memberships_one_owner_per_company'), ('idx_memberships_user'), ('idx_memberships_company'),
         ('idx_memberships_active')
),
expected_policies (tbl, policy) AS (
  VALUES
    ('profiles', 'profiles_self_read'),
    ('profiles', 'profiles_same_company_read'),
    ('companies', 'companies_member_read'),
    ('company_memberships', 'memberships_self_read'),
    ('company_memberships', 'memberships_company_peers_read')
),
-- browser: whether the signed-in person (authenticated) may call it; anon may call none
expected_functions (signature, returns, definer, browser) AS (
  VALUES
    ('public.is_active_member(uuid)', 'boolean', true, true),
    ('public.get_my_context()', 'jsonb', true, true),
    ('public.company_list_members(uuid)', 'record', true, true),
    ('public.handle_new_user()', 'trigger', true, false),
    ('public.touch_updated_at()', 'trigger', false, false),
    ('public.memberships_stamp_activated()', 'trigger', false, false)
),
expected_triggers (tbl, trg, fn) AS (
  VALUES
    ('auth.users', 'on_auth_user_created', 'handle_new_user'),
    ('public.profiles', 'trg_profiles_updated_at', 'touch_updated_at'),
    ('public.company_memberships', 'trg_memberships_activated', 'memberships_stamp_activated')
),
roles_present (ok) AS (
  SELECT count(*) = 3 FROM pg_catalog.pg_roles WHERE rolname IN ('anon', 'authenticated', 'service_role')
),
checks (section, step, item, ok, detail) AS (
  -- 1. What the migration needs of the project
  SELECT 1, 'ready', 'PostgreSQL 15 or later', current_setting('server_version_num')::int >= 150000, current_setting('server_version')
  UNION ALL
  SELECT 1, 'ready', 'Supabase Auth''s accounts: table auth.users', to_regclass('auth.users') IS NOT NULL, ''
  UNION ALL
  SELECT 1, 'ready', 'Supabase Auth''s accounts: column auth.users.' || a.col,
    EXISTS (SELECT 1 FROM pg_catalog.pg_attribute x
            WHERE x.attrelid = to_regclass('auth.users') AND x.attname = a.col AND x.attnum > 0 AND NOT x.attisdropped), ''
  FROM auth_columns a
  UNION ALL
  SELECT 1, 'ready', 'Supabase Auth''s auth.uid()', to_regprocedure('auth.uid()') IS NOT NULL, ''
  UNION ALL
  SELECT 1, 'ready', 'role ' || r.rolname, EXISTS (SELECT 1 FROM pg_catalog.pg_roles x WHERE x.rolname = r.rolname), ''
  FROM (VALUES ('anon'), ('authenticated'), ('service_role')) AS r (rolname)
  UNION ALL
  SELECT 1, 'ready', 'gen_random_uuid()', to_regprocedure('gen_random_uuid()') IS NOT NULL, ''

  -- 2. Types
  UNION ALL
  SELECT 2, 'applied', 'type public.company_role: owner, admin, member',
    COALESCE((SELECT array_agg(e.enumlabel::text ORDER BY e.enumsortorder) FROM pg_catalog.pg_enum e
              WHERE e.enumtypid = to_regtype('public.company_role')) = ARRAY['owner', 'admin', 'member'], false), ''
  UNION ALL
  SELECT 2, 'applied', 'type public.membership_status: pending, active, suspended',
    COALESCE((SELECT array_agg(e.enumlabel::text ORDER BY e.enumsortorder) FROM pg_catalog.pg_enum e
              WHERE e.enumtypid = to_regtype('public.membership_status')) = ARRAY['pending', 'active', 'suspended'], false), ''

  -- 3. Tables, columns and indexes
  UNION ALL
  SELECT 3, 'applied', 'table public.' || t.tbl, to_regclass('public.' || t.tbl) IS NOT NULL, ''
  FROM tables t
  UNION ALL
  SELECT 3, 'applied', format('column public.%s.%s %s', c.tbl, c.col, c.udt),
    EXISTS (SELECT 1 FROM information_schema.columns i
            WHERE i.table_schema = 'public' AND i.table_name = c.tbl AND i.column_name = c.col AND i.udt_name = c.udt), ''
  FROM expected_columns c
  UNION ALL
  SELECT 3, 'applied', 'index public.' || x.idx, to_regclass('public.' || x.idx) IS NOT NULL, ''
  FROM expected_indexes x

  -- 4. Row-level security: on, with the reading policies and no other
  UNION ALL
  SELECT 4, 'applied', 'row-level security on public.' || t.tbl,
    COALESCE((SELECT c.relrowsecurity FROM pg_catalog.pg_class c WHERE c.oid = to_regclass('public.' || t.tbl)), false), ''
  FROM tables t
  UNION ALL
  SELECT 4, 'applied', format('policy %s on public.%s, reading only, for authenticated', p.policy, p.tbl),
    EXISTS (SELECT 1 FROM pg_catalog.pg_policies x
            WHERE x.schemaname = 'public' AND x.tablename = p.tbl AND x.policyname = p.policy AND x.cmd = 'SELECT'
              AND x.roles = ARRAY['authenticated']::name[]), ''
  FROM expected_policies p
  UNION ALL
  SELECT 4, 'applied', 'no other policy on the three tables',
    (SELECT count(*) = 3 FROM tables t WHERE to_regclass('public.' || t.tbl) IS NOT NULL)
      AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_policies x
                      WHERE x.schemaname = 'public' AND x.tablename IN (SELECT tbl FROM tables)
                        AND (x.tablename::text, x.policyname::text) NOT IN (SELECT tbl, policy FROM expected_policies)),
    COALESCE((SELECT string_agg(x.tablename || '.' || x.policyname, ', ') FROM pg_catalog.pg_policies x
              WHERE x.schemaname = 'public' AND x.tablename IN (SELECT tbl FROM tables)
                AND (x.tablename::text, x.policyname::text) NOT IN (SELECT tbl, policy FROM expected_policies)), '')

  -- 5. Table rights: authenticated reads, anon nothing, the service role (the founder console) everything
  UNION ALL
  SELECT 5, 'applied', 'public.' || t.tbl || ': authenticated may read and not write',
    CASE WHEN to_regclass('public.' || t.tbl) IS NULL OR NOT (SELECT ok FROM roles_present) THEN false
         ELSE has_table_privilege('authenticated', to_regclass('public.' || t.tbl), 'SELECT')
              AND NOT has_table_privilege('authenticated', to_regclass('public.' || t.tbl), 'INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
    END, ''
  FROM tables t
  UNION ALL
  SELECT 5, 'applied', 'public.' || t.tbl || ': anon may do nothing',
    CASE WHEN to_regclass('public.' || t.tbl) IS NULL OR NOT (SELECT ok FROM roles_present) THEN false
         ELSE NOT has_table_privilege('anon', to_regclass('public.' || t.tbl), 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')
    END, ''
  FROM tables t
  UNION ALL
  SELECT 5, 'applied', 'public.' || t.tbl || ': service_role may read and write',
    CASE WHEN to_regclass('public.' || t.tbl) IS NULL OR NOT (SELECT ok FROM roles_present) THEN false
         ELSE has_table_privilege('service_role', to_regclass('public.' || t.tbl), 'SELECT')
              AND has_table_privilege('service_role', to_regclass('public.' || t.tbl), 'INSERT')
              AND has_table_privilege('service_role', to_regclass('public.' || t.tbl), 'UPDATE')
              AND has_table_privilege('service_role', to_regclass('public.' || t.tbl), 'DELETE')
    END, ''
  FROM tables t

  -- 6. Functions: what they return, whose rights they run with, and a search_path of their own
  UNION ALL
  SELECT 6, 'applied',
    format('function %s returns %s, %s, search_path pinned', f.signature, f.returns, CASE WHEN f.definer THEN 'security definer' ELSE 'security invoker' END),
    COALESCE((SELECT pg_catalog.format_type(p.prorettype, NULL) = f.returns AND p.prosecdef = f.definer
                     AND EXISTS (SELECT 1 FROM unnest(p.proconfig) AS s (config) WHERE s.config LIKE 'search_path=%')
              FROM pg_catalog.pg_proc p WHERE p.oid = to_regprocedure(f.signature)), false),
    COALESCE((SELECT array_to_string(p.proconfig, ' ') FROM pg_catalog.pg_proc p WHERE p.oid = to_regprocedure(f.signature)), '')
  FROM expected_functions f

  -- 7. Function rights: the signed-in person calls the two RPCs and the policies' helper; anon calls nothing
  UNION ALL
  SELECT 7, 'applied',
    f.signature || CASE WHEN f.browser THEN ': authenticated may call it, anon may not' ELSE ': neither authenticated nor anon may call it' END,
    CASE WHEN to_regprocedure(f.signature) IS NULL OR NOT (SELECT ok FROM roles_present) THEN false
         ELSE has_function_privilege('authenticated', to_regprocedure(f.signature), 'EXECUTE') = f.browser
              AND NOT has_function_privilege('anon', to_regprocedure(f.signature), 'EXECUTE')
    END, ''
  FROM expected_functions f

  -- 8. Triggers, each calling its function and switched on
  UNION ALL
  SELECT 8, 'applied', format('trigger %s on %s calls %s', t.trg, t.tbl, t.fn),
    EXISTS (SELECT 1 FROM pg_catalog.pg_trigger g JOIN pg_catalog.pg_proc p ON p.oid = g.tgfoid
            WHERE g.tgrelid = to_regclass(t.tbl) AND g.tgname = t.trg AND NOT g.tgisinternal AND g.tgenabled <> 'D' AND p.proname = t.fn), ''
  FROM expected_triggers t
)
SELECT step, item, CASE WHEN ok THEN 'ok' ELSE 'NOT OK' END AS result, detail
FROM checks
ORDER BY section, item;
