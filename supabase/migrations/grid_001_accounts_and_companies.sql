-- ===========================================================================================================================================================
-- Gridlytics, migration grid_001: accounts and companies, for Gridlytics' own Supabase project
-- ===========================================================================================================================================================
-- For supabase mode (GRIDLYTICS_AUTH_MODE=supabase), in the project the owner creates for Gridlytics: never Boxlytics' project, and not Solarlytics' either,
-- since a company's id here is its id in Gridlytics' own registry. Shipped for review: nobody applies it but the owner, by hand, as supabase/README.md says.
-- Local mode (the default) needs none of it.
--
-- What it makes:
--   profiles               one row per account of the project's sign-in (auth.users), made by a trigger when the founder console creates the account
--   companies              the companies the founder makes; a company's id is the same here and in the backend's registry (platform.db)
--   company_memberships    an account's company role (owner, admin or member) and status (pending, active or suspended) in one company
--   is_active_member()     the helper the row-level security reads
--   get_my_context()       the person's profile and memberships, which the backend reads with the person's own token (companies._project_context)
--   company_list_members() a company's members with their email addresses, for the company's Users list in supabase mode
--
-- Who writes: only the founder console (store_founder._Project), on the server, with the project's service-role key, which row-level security does not
-- bind. A signed-in person reads, and only their own profile and memberships, the companies they are an active member of, and the profiles and memberships
-- of the people in those companies. Every account is made in the founder console, with an email and a first password: there are no sign-ups, invitations,
-- onboarding or super-admins, so none of Boxlytics' writing RPCs is here. Whether Gridlytics is switched on for a company, its seats, its database, and
-- whether it or an account is archived, are the backend's registry's, not this project's.
--
-- Ported for review only (audit parity-supabase-auth-03) from saad-tahir/solarlytics-backend at c853130 (the owner's own code), the file
-- supabase/migrations/solar_001_accounts_and_companies.sql, with the product's names changed. That file adapts Boxlytics at boxlytics@c429bef (the owner
-- allowed Solarlytics to reuse its sign-in pieces; carrying them into Gridlytics is a second reuse, for the owner to confirm), each piece naming its source:
--   migrations/001_multitenant.sql (types, tables, helper, triggers, policies, get_my_context), 003_company_views.sql (company_list_members),
--   009_fix_invites_rls.sql (no policy reads auth.users), 011_token_economy.sql (get_my_context as it last defined it, without token_balance).
-- Changed throughout: every function runs with an empty search_path and names every table by its schema; the browser's roles get table rights by GRANT as
-- well as by policy, so a policy added later by mistake cannot open a write; anon is given nothing.
-- Not copied: the tokens economy (011), simulation results (001 section 2.5, 004), the material catalogue (012), invitations (001, 009), onboarding and
-- super-admin RPCs (001 section 7, 002, 005 to 008), the founder console's project tables (010), seats (seats are the registry's).
--
-- One transaction: all of it or none. It can be run again: types are made when missing, tables and indexes IF NOT EXISTS, functions and triggers OR
-- REPLACE, policies dropped and made again. Undo: grid_001_rollback.sql. Check before and after: ../sql/grid_000_preflight.sql.
-- ===========================================================================================================================================================

BEGIN;


-- -----------------------------------------------------------------------------------------------------------------------------------------------------------
-- 1. Types (001 section 1: company_role and membership_status; assignment_status and invite_status are not copied, having no use here)
-- -----------------------------------------------------------------------------------------------------------------------------------------------------------

DO $$
BEGIN
  IF to_regtype('public.company_role') IS NULL THEN
    CREATE TYPE public.company_role AS ENUM ('owner', 'admin', 'member');
  END IF;

  IF to_regtype('public.membership_status') IS NULL THEN
    CREATE TYPE public.membership_status AS ENUM ('pending', 'active', 'suspended');
  END IF;
END$$;


-- -----------------------------------------------------------------------------------------------------------------------------------------------------------
-- 2. Tables
-- -----------------------------------------------------------------------------------------------------------------------------------------------------------

-- From 001 section 2.1, without is_super_admin, assignment_status and requested_company_name: no super-admins (the founder works with the service-role key)
-- and no self-onboarding
CREATE TABLE IF NOT EXISTS public.profiles (
  id          UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  first_name  TEXT,
  last_name   TEXT,
  avatar_url  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- From 001 section 2.2. The founder makes every company, so it is verified when made (Boxlytics' companies waited for a super-admin's approval); setting it
-- false shuts the company out at the sign-in, whatever the registry says (companies._counted). Not copied: seat_limit (the registry's seats), created_by
-- and verified_by (the founder is not an account of this project), verified_at. A name has the registry's bounds, 1 to 80 characters (platform._name)
CREATE TABLE IF NOT EXISTS public.companies (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name        TEXT NOT NULL CHECK (char_length(btrim(name)) BETWEEN 1 AND 80),
  verified    BOOLEAN NOT NULL DEFAULT true,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 001: one company per name, whatever its case ("Acme" and "acme")
CREATE UNIQUE INDEX IF NOT EXISTS companies_name_unique_lower
  ON public.companies (lower(name));

-- From 001 section 2.3. A membership the founder adds is active unless it says otherwise, as in the registry (platform.add_membership); Boxlytics' started
-- pending, waiting for an approval Gridlytics does not have. A membership pending or suspended opens nothing; the backend tells the person its state
CREATE TABLE IF NOT EXISTS public.company_memberships (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  company_id    UUID NOT NULL REFERENCES public.companies(id) ON DELETE CASCADE,
  role          public.company_role NOT NULL DEFAULT 'member',
  status        public.membership_status NOT NULL DEFAULT 'active',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  activated_at  TIMESTAMPTZ,
  CONSTRAINT memberships_user_company_unique UNIQUE (user_id, company_id)
);

-- 001: at most one owner per company, whatever the owner's status. The owner is the company's pinned Gridlytics administrator (companies.py); making another
-- member the owner hands the ownership over (store_founder._Project.set_membership)
CREATE UNIQUE INDEX IF NOT EXISTS memberships_one_owner_per_company
  ON public.company_memberships (company_id)
  WHERE role = 'owner';

-- 001 section 3
CREATE INDEX IF NOT EXISTS idx_memberships_user    ON public.company_memberships (user_id);
CREATE INDEX IF NOT EXISTS idx_memberships_company ON public.company_memberships (company_id);
CREATE INDEX IF NOT EXISTS idx_memberships_active  ON public.company_memberships (company_id) WHERE status = 'active';


-- -----------------------------------------------------------------------------------------------------------------------------------------------------------
-- 3. The helper the policies read (001 section 4.2). SECURITY DEFINER, so that a policy can read memberships without the memberships' own policies
-- calling themselves. Not copied: is_super_admin, my_role_in, is_company_admin_or_owner and seats_remaining (001), my_email (009): nothing here calls them
-- -----------------------------------------------------------------------------------------------------------------------------------------------------------

-- From 001 section 4.2: is the person an active member of the company?
CREATE OR REPLACE FUNCTION public.is_active_member(p_company_id UUID)
RETURNS BOOLEAN
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.company_memberships
    WHERE user_id = auth.uid()
      AND company_id = p_company_id
      AND status = 'active'
  );
$$;


-- -----------------------------------------------------------------------------------------------------------------------------------------------------------
-- 4. Triggers
-- -----------------------------------------------------------------------------------------------------------------------------------------------------------

-- From 001 section 5.1: a profile for every account the project's sign-in makes. The founder console creates an account with a name in its metadata
-- (full_name, or first_name and last_name); the Google and camelCase keys Boxlytics also walked are not read, there being no Google sign-in here. A full
-- name fills whichever of the two is missing, split at its first space as 001 splits it and as the registry splits it for local mode's context
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_first  TEXT;
  v_last   TEXT;
  v_full   TEXT;
BEGIN
  v_first := NULLIF(btrim(NEW.raw_user_meta_data->>'first_name'), '');
  v_last  := NULLIF(btrim(NEW.raw_user_meta_data->>'last_name'), '');
  v_full  := COALESCE(NULLIF(btrim(NEW.raw_user_meta_data->>'full_name'), ''),
                      NULLIF(btrim(NEW.raw_user_meta_data->>'name'), ''));

  IF v_first IS NULL AND v_full IS NOT NULL THEN
    v_first := split_part(v_full, ' ', 1);
  END IF;
  IF v_last IS NULL AND v_full IS NOT NULL AND strpos(v_full, ' ') > 0 THEN
    v_last := NULLIF(btrim(substr(v_full, strpos(v_full, ' ') + 1)), '');
  END IF;

  INSERT INTO public.profiles (id, first_name, last_name, avatar_url)
  VALUES (NEW.id, v_first, v_last, NULLIF(btrim(NEW.raw_user_meta_data->>'avatar_url'), ''))
  ON CONFLICT (id) DO NOTHING;

  RETURN NEW;
END;
$$;

-- Replaced in place, never dropped and made again: dropping a trigger needs the table's ownership, and the SQL Editor's role does not own auth.users;
-- CREATE OR REPLACE TRIGGER needs only the TRIGGER right (PostgreSQL 14 and later). The same for the two triggers below
CREATE OR REPLACE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- From 001 section 5.2: profiles.updated_at follows each change
CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_profiles_updated_at
  BEFORE UPDATE ON public.profiles
  FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

-- New, after 001's place_user (section 7.6), which kept activated_at as COALESCE(activated_at, now()): a membership is active from the first time it is made
-- active, whoever writes it, as in the registry (platform.set_membership)
CREATE OR REPLACE FUNCTION public.memberships_stamp_activated()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = ''
AS $$
BEGIN
  IF NEW.status = 'active' AND NEW.activated_at IS NULL THEN
    NEW.activated_at := now();
  END IF;
  RETURN NEW;
END;
$$;

CREATE OR REPLACE TRIGGER trg_memberships_activated
  BEFORE INSERT OR UPDATE OF status ON public.company_memberships
  FOR EACH ROW EXECUTE FUNCTION public.memberships_stamp_activated();


-- -----------------------------------------------------------------------------------------------------------------------------------------------------------
-- 5. Row-level security (001 section 6, the reading policies only). Every write is the founder's, through the service-role key; so no policy lets the
-- browser's roles insert, update or delete. Per 009, no policy reads auth.users, which the authenticated role may not read: a policy that did would make
-- every query on its table fail with "permission denied for table users"
-- -----------------------------------------------------------------------------------------------------------------------------------------------------------

ALTER TABLE public.profiles            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.companies           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.company_memberships ENABLE ROW LEVEL SECURITY;

-- 001 section 6.1: your own profile, and the profiles of the people you share an active company with
DROP POLICY IF EXISTS "profiles_self_read" ON public.profiles;
CREATE POLICY "profiles_self_read" ON public.profiles
  FOR SELECT TO authenticated
  USING (id = (SELECT auth.uid()));

DROP POLICY IF EXISTS "profiles_same_company_read" ON public.profiles;
CREATE POLICY "profiles_same_company_read" ON public.profiles
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1
      FROM public.company_memberships m1
      JOIN public.company_memberships m2 ON m1.company_id = m2.company_id
      WHERE m1.user_id = (SELECT auth.uid())
        AND m1.status = 'active'
        AND m2.user_id = profiles.id
        AND m2.status = 'active'
    )
  );

-- 001 section 6.2: an active member reads their company
DROP POLICY IF EXISTS "companies_member_read" ON public.companies;
CREATE POLICY "companies_member_read" ON public.companies
  FOR SELECT TO authenticated
  USING (public.is_active_member(id));

-- 001 section 6.3: your own memberships, of any status, and those of the companies you are an active member of
DROP POLICY IF EXISTS "memberships_self_read" ON public.company_memberships;
CREATE POLICY "memberships_self_read" ON public.company_memberships
  FOR SELECT TO authenticated
  USING (user_id = (SELECT auth.uid()));

DROP POLICY IF EXISTS "memberships_company_peers_read" ON public.company_memberships;
CREATE POLICY "memberships_company_peers_read" ON public.company_memberships
  FOR SELECT TO authenticated
  USING (public.is_active_member(company_id));


-- -----------------------------------------------------------------------------------------------------------------------------------------------------------
-- 6. Table rights (new). Supabase's default privileges give anon and authenticated every right on a new table in public and leave the policies alone to
-- guard it. Here the browser's roles get only reading, which is all the policies allow; the service role (the founder console, on the server) gets all
-- -----------------------------------------------------------------------------------------------------------------------------------------------------------

REVOKE ALL ON TABLE public.profiles, public.companies, public.company_memberships FROM PUBLIC, anon, authenticated;
GRANT SELECT ON TABLE public.profiles, public.companies, public.company_memberships TO authenticated;
GRANT ALL ON TABLE public.profiles, public.companies, public.company_memberships TO service_role;


-- -----------------------------------------------------------------------------------------------------------------------------------------------------------
-- 7. What the backend reads, with the person's own token
-- -----------------------------------------------------------------------------------------------------------------------------------------------------------

-- From 001 section 7.1, as 011 section 5 last defined it, without token_balance and seat_limit (011, 001). The answer has the shape of local mode's context
-- (platform.context) key for key, so the backend (companies.py) and the web app read one shape in both modes:
--   profile      the profile with the account's email and name; is_super_admin, assignment_status and requested_company_name are constants kept for the shape
--                (so is must_change_password, Gridlytics' own, for local mode's one-time password: Supabase Auth makes none, so it is false)
--   memberships  every membership, of any status, each with its company {id, name, verified}; the registry adds `enabled` and the seats (decisions 2 and 5)
--   pending_invites_for_email  always empty: no invitations
-- An account that is banned (the founder's "disable" in supabase mode) or deleted gets null, as a caller with no token does, and as a disabled account gets
-- None in local mode: its access token, good for up to an hour, then opens nothing once the backend's cached context runs out
CREATE OR REPLACE FUNCTION public.get_my_context()
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
  v_uid    UUID := auth.uid();
  v_result JSONB;
BEGIN
  IF v_uid IS NULL THEN
    RETURN NULL;
  END IF;

  SELECT jsonb_build_object(
    'profile', jsonb_build_object(
      'id',                     u.id,
      'email',                  lower(u.email),
      'name',                   COALESCE(NULLIF(btrim(concat_ws(' ', p.first_name, p.last_name)), ''), lower(u.email)),
      'first_name',             p.first_name,
      'last_name',              p.last_name,
      'avatar_url',             p.avatar_url,
      'is_super_admin',         false,
      'assignment_status',      'none',
      'requested_company_name', NULL,
      'created_at',             COALESCE(p.created_at, u.created_at),
      'updated_at',             COALESCE(p.updated_at, u.updated_at),
      'must_change_password',   false
    ),
    'memberships', (
      SELECT COALESCE(jsonb_agg(
        jsonb_build_object(
          'id',           m.id,
          'role',         m.role,
          'status',       m.status,
          'created_at',   m.created_at,
          'activated_at', m.activated_at,
          'company',      jsonb_build_object(
            'id',       c.id,
            'name',     c.name,
            'verified', c.verified
          )
        ) ORDER BY lower(c.name), m.created_at
      ), '[]'::jsonb)
      FROM public.company_memberships m
      JOIN public.companies c ON c.id = m.company_id
      WHERE m.user_id = u.id
    ),
    'pending_invites_for_email', '[]'::jsonb
  )
  INTO v_result
  FROM auth.users u
  LEFT JOIN public.profiles p ON p.id = u.id
  WHERE u.id = v_uid
    AND u.deleted_at IS NULL
    AND (u.banned_until IS NULL OR u.banned_until <= now());

  RETURN v_result;
END;
$$;

-- From 003: a company's members with their email addresses (which live in auth.users, closed to the browser's roles), owner first. As in Boxlytics, only
-- the company's owner or an admin may call it: the addresses of a company's people are not for every member to read from the browser. A person made a
-- Gridlytics administrator in Gridlytics while they are a company member here sees the people seen in Gridlytics from the backend instead, which asks for
-- manage_users before it calls this. Refused with insufficient_privilege, which PostgREST answers with 403, where Boxlytics' plain exception gave 400
CREATE OR REPLACE FUNCTION public.company_list_members(p_company_id UUID)
RETURNS TABLE (
  membership_id  UUID,
  user_id        UUID,
  email          TEXT,
  first_name     TEXT,
  last_name      TEXT,
  avatar_url     TEXT,
  role           public.company_role,
  status         public.membership_status,
  created_at     TIMESTAMPTZ,
  activated_at   TIMESTAMPTZ
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM public.company_memberships AS me
                 WHERE me.company_id = p_company_id AND me.user_id = auth.uid() AND me.status = 'active' AND me.role IN ('owner', 'admin')) THEN
    RAISE EXCEPTION 'only the company''s owner or an admin may list its members' USING ERRCODE = '42501';
  END IF;

  RETURN QUERY
  SELECT
    m.id                 AS membership_id,
    m.user_id,
    lower(u.email)::TEXT AS email,
    p.first_name,
    p.last_name,
    p.avatar_url,
    m.role,
    m.status,
    m.created_at,
    m.activated_at
  FROM public.company_memberships m
  JOIN auth.users u ON u.id = m.user_id
  LEFT JOIN public.profiles p ON p.id = m.user_id
  WHERE m.company_id = p_company_id
  ORDER BY
    CASE m.role
      WHEN 'owner'  THEN 0
      WHEN 'admin'  THEN 1
      WHEN 'member' THEN 2
    END,
    m.created_at ASC;
END;
$$;


-- -----------------------------------------------------------------------------------------------------------------------------------------------------------
-- 8. Function rights (001 section 8, narrowed). PostgreSQL lets PUBLIC execute a new function, and Supabase's default privileges add anon: both are taken
-- back. The signed-in person calls the two RPCs, and the policies call is_active_member as that person; the trigger functions are called by nobody
-- -----------------------------------------------------------------------------------------------------------------------------------------------------------

REVOKE ALL ON FUNCTION public.is_active_member(UUID) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.get_my_context() FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.company_list_members(UUID) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.handle_new_user() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.touch_updated_at() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.memberships_stamp_activated() FROM PUBLIC, anon, authenticated;

GRANT EXECUTE ON FUNCTION public.is_active_member(UUID) TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.get_my_context() TO authenticated, service_role;
GRANT EXECUTE ON FUNCTION public.company_list_members(UUID) TO authenticated, service_role;

COMMIT;
