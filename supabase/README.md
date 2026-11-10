# Gridlytics' own Supabase project

This folder holds the SQL for the Supabase project that Gridlytics uses in **supabase mode** (`GRIDLYTICS_AUTH_MODE=supabase`), an optional mode that only
the owner applies.

- It is Gridlytics' own project, which the owner creates. It is **never** Boxlytics' project: Boxlytics is read only and is not touched in any way. It is not
  Solarlytics' project either: a company here has the id it has in Gridlytics' own registry, so two products cannot share one project's companies.
- The SQL is shipped for review, never applied: it has not been run against any project. Only the owner applies it, by hand, with the steps below. No agent,
  script or CI job runs it.
- Local mode (`GRIDLYTICS_AUTH_MODE=local`, the default) needs none of this. There the backend is the sign-in service, and accounts, companies and
  memberships live in `platform.db` in the data folder.

The files are ported from Solarlytics (`saad-tahir/solarlytics-backend` at `c853130`, its `supabase/` folder: the owner's own code) with the product's names
changed (audit parity-supabase-auth-03). The SQL itself is Solarlytics' unchanged, since the lifted sign-in code (`companies.py`, `store_founder.py`) reads and
writes the same tables and functions.

## The files

| File | What it is |
|---|---|
| `sql/grid_000_preflight.sql` | Read only: one `SELECT` that checks the project before and after the migration and changes nothing |
| `migrations/grid_001_accounts_and_companies.sql` | The migration: profiles, companies, company memberships, row-level security, `get_my_context()` and `company_list_members()` |
| `migrations/grid_001_rollback.sql` | The undo. Destructive: it drops the three tables with every row in them |

The migration adapts Boxlytics' `migrations/001_multitenant.sql`, `003_company_views.sql`, `009_fix_invites_rls.sql` and `011_token_economy.sql` (at
`boxlytics@c429bef`), as Solarlytics' does. Each piece names its source in the file. Nothing of Boxlytics' tokens economy, simulation results, material
catalogue, invitations, onboarding or super-admins is copied. The owner allowed Solarlytics to reuse Boxlytics' sign-in pieces; carrying them into Gridlytics
is a second reuse, for the owner to confirm.

## What the project holds, and who writes it

- **`profiles`**: one row per account. A trigger makes it when an account is created.
- **`companies`**: the companies the founder makes. A company has the same id here as in the backend's registry (`platform.db`).
- **`company_memberships`**: an account's company role (`owner`, `admin` or `member`) and status (`pending`, `active` or `suspended`) in one company. A
  company has at most one owner.
- **Who writes.** Only the founder console (`/admin`), on the server, with the project's service-role key (`store_founder._Project`). Every account is made
  there, with an email and a first password. Nobody signs up, signs in with Google or another provider, signs in by a link, is invited or onboards.
- **Who reads.** A signed-in person reads only their own profile and memberships, the companies they are an active member of, and the profiles and
  memberships of the people in those companies. `anon` gets nothing.
- **What stays in the registry.** Whether Gridlytics is switched on for a company, its seats, its database, and whether a company or an account is archived
  are the backend's registry's, not this project's.

What the backend reads, always with the person's own access token:

- **`rpc/get_my_context`** (`companies._project_context`). It returns `{profile, memberships: [{id, role, status, created_at, activated_at, company: {id,
  name, verified}}], pending_invites_for_email: []}`. That is the shape of local mode's `GET /api/local/context`, key for key; the registry adds each
  company's `enabled` and seats.
  - Every membership comes back, of any status: a membership pending or suspended opens nothing, and the backend tells the person its state.
  - The profile's `must_change_password` is always false: Supabase Auth makes no one-time password, which only local mode's sign-in has.
  - An account that is banned (the founder's "disable", and an archive, which disables) or deleted gets `null`, as a disabled account does in local mode.
- **`rpc/company_list_members`** (`p_company_id`). It returns the company's members with their email addresses, owner first, for the company's Users list.
  - Only the company's owner or an admin may call it, as in Boxlytics. The backend also checks the Gridlytics permission `manage_users` before it calls.
  - Anyone else is refused with 403.

## The owner's steps

### 1. Create the project

1. In the Supabase dashboard, **Create the project** in your own organization, never in Boxlytics'. Name it, for example, `gridlytics`, and choose the
   region nearest the users.
2. Keep the database password in your password manager. It is never written in this repository or in any environment file.

### 2. Check, apply, check again

1. In the **SQL Editor**, run `sql/grid_000_preflight.sql`.
   - Every row of step `ready` must read `ok`.
   - On a new project every row of step `applied` reads `NOT OK`. If one reads `ok`, the project already holds something of that name: stop and look.
2. Take a backup if the project holds anything (Database, Backups).
3. Run `migrations/grid_001_accounts_and_companies.sql` in the SQL Editor: paste the whole file into a new query and run it.
   - It is one transaction, so it applies completely or not at all. It can be run again.
   - Or use `psql` with the connection string from the dashboard's **Connect** panel:
     `psql "<connection string>" -v ON_ERROR_STOP=1 -f supabase/migrations/grid_001_accounts_and_companies.sql`.
   - Do not use `supabase db push`. These files are applied by hand after review, and their names do not follow the CLI's `<timestamp>_<name>.sql`, so the
     CLI would skip them anyway.
4. Run the preflight again. Every row must now read `ok`.

### 3. Sign-in settings (Authentication)

Only people the founder adds get in, with an email and a password. The preflight cannot see these settings, so check each one by hand.

- **Sign In / Providers:**
  - turn off **Allow new users to sign up**;
  - keep **Email** on, with passwords;
  - turn off **Google** and every other provider;
  - keep **anonymous sign-ins** off.
  - "Confirm email" does not matter: the founder console creates accounts already confirmed.
- **Magic links and one-time codes** cannot be switched off while email sign-in is on. The backend refuses every token whose `amr` lacks `password` or whose
  provider is not `email` (`backend/gridlytics/identity.py`), and the web app has no such button.
- **Passwords:** set the minimum length to **10**, the app's own rule (`auth.password_problem`). Supabase Auth refuses passwords over 72 characters, so in
  this mode a password has 10 to 72 characters, where local mode allows up to 200.
- **JWT keys:**
  - Prefer the asymmetric signing keys (ECC P-256). The backend then checks tokens against the project's public keys (`/auth/v1/.well-known/jwks.json`) and
    needs no secret.
  - If the project still signs with the legacy JWT secret, the backend needs it as `GRIDLYTICS_SUPABASE_JWT_SECRET`.
  - Keep the access token's expiry at **3600** seconds, the same hour as local mode.
- **Data API:** keep the `public` schema exposed. `rpc/get_my_context` and `rpc/company_list_members` are called through it.

### 4. URL configuration (Authentication, URL Configuration)

- **Site URL:** the web app's address, for example `https://<web app host>`.
- **Redirect URLs:** only the password-reset page, which is the only page Supabase sends anyone back to:
  - `https://<web app host>/reset-password`;
  - the preview pattern of the web app's host, for example `https://*-<team>.vercel.app/reset-password`, if previews are used;
  - `http://127.0.0.1:5295/reset-password` for the web app on this machine (its launcher's default port, `GRIDLYTICS_WEB_PORT`).
- There is no `/auth/callback` (there is no Google sign-in) and no `/invite/**` (there are no invitations).

### 5. Email

- Password-reset mails need an email sender. Set one up under Authentication, Emails, SMTP Settings.
  - The default sender only reaches the project's own team, a few mails an hour.
  - This sender is still missing.
- The "Reset password" template's link leads to `/reset-password`.

### 6. The keys

The keys are in the dashboard under Project Settings: **Data API** gives the URL, and **API Keys** gives the anon (publishable) key and the service-role
(secret) key. Put them in the hosting platform's environment, never in this repository.

The backend, on the server:

| Variable | What |
|---|---|
| `GRIDLYTICS_AUTH_MODE` | `supabase` |
| `GRIDLYTICS_SUPABASE_URL` | `https://<project-ref>.supabase.co` |
| `GRIDLYTICS_SUPABASE_ANON_KEY` | the anon (publishable) key; it is public |
| `GRIDLYTICS_SUPABASE_JWT_SECRET` | only if the project signs with the legacy JWT secret (step 3) |
| `GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY` | the service-role key: server only, never in the web app or the browser. The founder console uses it to create accounts, companies and memberships |
| `GRIDLYTICS_JWT_AUDIENCE` | optional; default `authenticated` |
| `GRIDLYTICS_JWKS_TTL` | optional; seconds the project's public keys are kept, default 600 |

The web app, at build time:

| Variable | What |
|---|---|
| `VITE_AUTH_MODE` | `supabase` |
| `VITE_SUPABASE_URL` | the same URL as the backend's |
| `VITE_SUPABASE_ANON_KEY` | the anon (publishable) key |
| `VITE_API_BASE_URL` | the backend's address |

The web app's sign-in in this mode is the web repository's: check that its `src/app/env.ts` takes `VITE_AUTH_MODE=supabase` before building with it. The
web app never gets the service-role key, the JWT secret or the database password: each one would pass every policy.

The rest of the server's environment (its hosts, the web app's origin, the founder console's secrets) is not this project's: it belongs with the
deployment's settings (`docs/DEPLOY.md`, once it is written: audit parity-supabase-auth-06).

### 7. Companies and people

- Add them only through the founder console. In supabase mode it does three things:
  - it creates each account through the project's Admin API, with the email, a first password, the email marked confirmed, and the name as `full_name`;
  - it writes the company here with the same id as in the registry;
  - it adds the membership with its company role.
- Adding a company in the dashboard alone is not enough: the backend finds a company in its registry by the id.
- The founder's "disable" (and an archive, which disables) bans the account. From then on `get_my_context` returns `null`, and the backend refuses the
  account once its kept context runs out (about a minute, `GRIDLYTICS_MEMBERSHIP_TTL`).

### 8. Undo

`migrations/grid_001_rollback.sql` drops everything the migration made, with every row in the three tables. Take a backup first.

- The accounts (`auth.users`) stay; delete them in the dashboard if that is wanted too.
- Afterwards, the preflight's `applied` rows read `NOT OK` again.

### 9. Later changes

A later change to the project comes as a new pair in `migrations/`, `grid_NNN_<what>.sql` and `grid_NNN_rollback.sql`, with the preflight brought up to
date. It is reviewed and applied by hand in the same way, never by `supabase db push`.

## Still missing

- The project itself, which does not exist yet.
- An email sender for password-reset mails.
