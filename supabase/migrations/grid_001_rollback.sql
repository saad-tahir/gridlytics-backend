-- ===========================================================================================================================================================
-- Gridlytics, migration grid_001 undone: for Gridlytics' own Supabase project only, never Boxlytics' or Solarlytics'
-- ===========================================================================================================================================================
-- DESTRUCTIVE. It drops what grid_001_accounts_and_companies.sql made: the profiles, companies and company_memberships tables with every row in them, their
-- policies, triggers and indexes, the six functions and the two types. The project's accounts (auth.users) are left as they are: only the trigger
-- grid_001 put on them is dropped. Delete accounts, if that is wanted too, in the dashboard (Authentication, Users).
--
-- Shipped for review, like the migration: only the owner runs it, by hand. Take a backup first (Database, Backups), and run the preflight
-- (../sql/grid_000_preflight.sql) after it: every "applied" row should then read NOT OK. Ported for review only (audit parity-supabase-auth-03) from
-- saad-tahir/solarlytics-backend at c853130, supabase/migrations/solar_001_rollback.sql, with the product's names changed.
--
-- One transaction, and no CASCADE: if anything else in the project has come to depend on these objects since, the drop fails and changes nothing, rather than
-- taking that with it. The order is the one that needs no CASCADE: the trigger on the accounts first; profiles before company_memberships, which its policy
-- reads; company_memberships before companies, which its rows point at; the tables before the functions their policies and triggers call and the types
-- their columns have; company_list_members, whose result has the types, before the types.
--
-- Dropping a trigger needs the ownership of its table, and auth.users belongs to Supabase Auth, not to the SQL Editor's role. Should the project refuse the
-- first drop ("must be owner of relation users"), nothing has changed: remove the trigger on_auth_user_created in the dashboard (Database, Triggers, schema
-- auth), then run this file again.
-- ===========================================================================================================================================================

BEGIN;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;

DROP FUNCTION IF EXISTS public.get_my_context();
DROP FUNCTION IF EXISTS public.company_list_members(UUID);

DROP TABLE IF EXISTS public.profiles;
DROP TABLE IF EXISTS public.company_memberships;
DROP TABLE IF EXISTS public.companies;

DROP FUNCTION IF EXISTS public.is_active_member(UUID);
DROP FUNCTION IF EXISTS public.handle_new_user();
DROP FUNCTION IF EXISTS public.touch_updated_at();
DROP FUNCTION IF EXISTS public.memberships_stamp_activated();

DROP TYPE IF EXISTS public.membership_status;
DROP TYPE IF EXISTS public.company_role;

COMMIT;
