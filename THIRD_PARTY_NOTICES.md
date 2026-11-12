# Third-party notices

## Code lifted from the owner's own repositories

- `saad-tahir/solarlytics-backend` at `c853130`: the sign-in, tenancy and founder-console modules listed in `docs/DESIGN_LOG.md`, and their tests. Same
  owner; no licence obligation.
- The same repository at the same commit: `supabase/` (`sql/solar_000_preflight.sql`, `migrations/solar_001_accounts_and_companies.sql`,
  `migrations/solar_001_rollback.sql`, `README.md`) as `supabase/` here, renamed `grid_*`, and `backend/tests/test_supabase_sql.py` (audit
  parity-supabase-auth-03). The SQL adapts Boxlytics at `boxlytics@c429bef`, naming each piece's source in the file.

## Constructions from Boxlytics (another party), no files copied

Boxlytics is another party's product: nothing in it is licensed for reuse, and no file of it is copied here. The owner's decisions of 24 October 2026
allowed Solarlytics to follow Boxlytics at `boxlytics@c429bef` for the pieces below, and they came into Gridlytics with the lift above; carrying them into
Gridlytics is a second reuse, which the owner confirms (build plan Part 10.4, step 7; audit parity-architecture-08). Each was rewritten in Gridlytics' own
code. The SHA-256 is of the Boxlytics file in the reference copy the owner supplied (`boxlytics-main`, read on 12 Nov 2026). That copy has no git
history, so the hash names the copy read, which may differ from `c429bef`. The web app's constructions from Boxlytics, and the design values and patterns
its redesign takes from Boxlytics, are listed in the web repository's `THIRD_PARTY_NOTICES.md`.

| Gridlytics file | Boxlytics file (at `c429bef`) | SHA-256 of the reference copy's file | What was followed | Changed for Gridlytics |
|---|---|---|---|---|
| `backend/gridlytics/founder.py` | `supabase/functions/_shared/founder-auth.ts` | `dec64d2e4453ca2966a5b4320ec4e814b6bb67b089bb282eae8b5e67c39b5076` | The founder's 8-hour session token in the same format (base64url of the JSON payload, a dot, base64url of its HMAC-SHA256), checked as `verifySessionToken` checks it | A session ends on the server (signing out, signing out everywhere, a new password); wrong passwords and codes are locked out; a code is never taken twice; the password is sent once |
| `backend/gridlytics/founder.py` | the same file | the same | A founder password hash in Boxlytics' form, a plain SHA-256 in hex, is still taken, with a warning in the log (a compatibility path) | Gridlytics' own form is PBKDF2 (`python -m gridlytics.founder hash`, `docs/DEPLOY.md`). The plain SHA-256 path is to be removed before the first deployment for a customer; the owner confirms the date |
| `supabase/migrations/grid_001_accounts_and_companies.sql`, `supabase/migrations/grid_001_rollback.sql`, `supabase/sql/grid_000_preflight.sql` | `migrations/001_multitenant.sql` | `7703774f74941596a626b818de872decb2ef2834cf1df6bdcaa72e1626c32bb2` | Types, tables, the helper, triggers, policies and `get_my_context`, as Solarlytics' `solar_001` adapts them | Every function runs with an empty search path and names each table by its schema; table rights by grant as well as by policy; nothing for `anon`. Not carried: invitations, onboarding, super-admins, simulation results |
| the same | `migrations/003_company_views.sql` | `d66190d20a00c3732c3e5475032e3db2931cb8886f15cab8d8520f1af15662f9` | `company_list_members` | Refused with `insufficient_privilege` (403) where Boxlytics' plain exception gave 400 |
| the same | `migrations/009_fix_invites_rls.sql` | `f37dfa84837ac6b48aa1f59f43d6fe2bb24060b914d74d4a2e8c29929ac4e441` | No policy reads `auth.users` | No invitations at all |
| the same | `migrations/011_token_economy.sql` | `4499732d7f28d5d185806a68b7411cdb89776b7088a6b249622a8bec466c0568` | `get_my_context` as that migration last defines it | Without the token balance: the tokens economy is not carried |
| `backend/gridlytics/seed_demo.py` | the demo companies and the development user of Boxlytics' repositories, as the owner's decisions (section 6) list them | not in the reference copy read on 12 Nov 2026 | Two demo companies and a local test user (`dev@localhost`, "Local Tester") | Gridlytics' own demo companies and people; every password made by the seed |

## Constructions followed clean-room (no code copied)

| Source | Licence | What was followed |
|---|---|---|
| evcc-io/optimizer | MIT | The structure of the optimiser module, the result semantics (optimal, feasible, not solved), the idea of an explanation per step |
| EMHASS | MIT | Define once, update parameters; the single-recovery idea for a state of charge outside the band; the perfect-foresight bound |
| REopt.jl (NREL) | BSD-3 / Apache-2.0 | Genset unit commitment with minimum up and down times, the fuel curve intercept, the demand-charge epigraph (with R3's corrections) |
| HOMER's fuel-curve convention | published method | `fuel = a * P + b * S * u` |
| BLAST-Lite (NREL) | BSD-3 | The LFP calendar term's temperature slope used for the calendar holding cost |
| NOAA solar calculator | public domain | Sun position equations |
| Haurwitz (1945) | published method | Clear-sky irradiance |
| kellerza/sunsynk, wills106, ha-solarman, mkaiser, mpp-solar | Apache-2.0 / MIT | Facts about brand slot grammars (slot counts, fields), written as data |

Not used anywhere: Predbat (GPL), the AGPL Huawei library, python-microgrid in the product (LGPL; verification only, later).

## Runtime dependencies

FastAPI, Starlette, Pydantic, Uvicorn (MIT/BSD); NumPy, SciPy with HiGHS (BSD/MIT); PyJWT (MIT); cryptography (Apache-2.0/BSD); APScheduler (MIT);
ReportLab (BSD); httpx (BSD). These are the pins of `requirements.txt`, each imported or run by the app (`test_fix_r7_deploy_ci`).
