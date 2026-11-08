# U2. Backends audit: solarlytics-backend (sign-in, tenancy, founder console), the owner's conventions, boxlytics-backend patterns, and the EMS backend skeleton

Research note for the OnyxLeaf EMS plan (Product 1: TOU-aware, outage-aware, genset-aware dispatch optimiser for Pakistani C&I sites). Written 6 November 2026. Author: planning subagent (task U2-backends). Both repositories were read only; nothing was run from them, edited, committed, branched or pushed.

Purpose. The EMS will be two new repositories, a Python FastAPI backend and a React + TypeScript + Vite frontend, in the style of the owner's Solarlytics and Boxlytics. This note audits (1) `C:/Users/SaadTahir/Videos/solarlytics-backend`, the sign-in, per-company tenancy and founder-console module, to judge whether it can be lifted into the EMS backend as a package; (2) the conventions the owner approved there (rules, documents, tests, launcher, environment names, CI); (3) `C:/Users/SaadTahir/Videos/boxlytics-backend`, its settings, API key, async jobs, deploy files and the Streamlit dashboard; (4) a recommended backend skeleton for the EMS; (5) templates for BUILD_HANDOFF, BUILD_PROGRESS and DESIGN_LOG in the owner's style.

Sources read in full on 6 November 2026 (line numbers below refer to these files as they are today; the backend repository is at commit `c853130` on `main`, remote `https://github.com/saad-tahir/solarlytics-backend.git`; boxlytics-backend is at `68eea39`, remote `https://github.com/OmerJauhar/boxlytics-backend.git`):

- solarlytics-backend: `CLAUDE.md`, `README.md`, `CHANGELOG.md` (headings), `Dockerfile`, `.dockerignore`, `.env.production.example`, `.gitignore`, `.gitattributes`, `requirements.txt`, `requirements-dev.txt`, `.github/workflows/ci.yml`, `scripts/restore_backup.py`, `scripts/build_release.py` (head), `scripts/load_check.py` (head), `supabase/README.md`, `docs/ACCOUNTS.md`, `docs/DEPLOY.md` (head and headings), `docs/TROUBLESHOOTING.md`, `docs/BUILD_HANDOFF.md`, `docs/DESIGN_LOG_GUIDE.md`, `docs/PRODUCT_BRIEF.md`, `docs/COMPETITORS.md`, the top, headings and tail of `docs/BUILD_PROGRESS.md`, `docs/DESIGN_LOG.md`, `docs/HANDOFF_NEXT.md`, `docs/TEST_MATRIX.md`, `docs/OWNER_CHECKLIST.md`, `docs/EQUATIONS.md`, `docs/ASSUMPTIONS.md`, `docs/GLOSSARY.md`, `docs/split/S0a.md`, `S0b.md`, `DOC.md`, `D1.md` (heads), `docs/program/ARCHITECTURE.md` (head); the modules `identity.py`, `platform.py`, `companies.py`, `routes_local.py`, `routes_platform.py`, `founder.py`, `routes_founder.py`, `store_founder.py` (in part), `store_members.py`, `seed_demo.py` (in part), `backups.py`, `mfa.py`, `auth.py`, `apikeys.py`, `link_company.py`, `version.py`, `jobs.py`, `schemas.py` (head), `store.py` (lines 1-200, 403-530 and the function index), `api.py` (lines 1-560, 705-720, 1183-1185, 4897-4942, 6786-6835 and the symbol index); the tests `conftest.py`, `identities.py`, `golden.py`, `sibling.py`, `test_sibling.py`, `test_deploy_docs.py`, and the heads of `test_identity.py`, `test_founder.py`, `test_signin_guard.py`, `test_auth.py`, `test_platform.py`, `test_companies.py`, `test_old_signin_removed.py`, `test_hygiene.py`, `test_concurrency_w2.py`.
- boxlytics-backend: `README.md`, `RUN_LOCALLY.md`, `SYNC_WORKFLOW.md`, `backend/main.py`, `backend/core/config.py`, `backend/core/security.py`, `backend/config.py` (the stale root copy), `backend/services/job_manager.py`, `backend/api/routes/{jobs,simulate,results,parameters}.py`, `backend/db/database.py`, `backend/models/{job,analysis}.py`, `backend/deploy/boxlytics-backend.service`, `backend/deploy/docker-compose.prod.yml`, `backend/deploy/nginx-boxlytics-backend.conf`, `backend/Dockerfile`, `backend/DEPLOYMENT.md`, `backend/README.md`, `backend/.env.production.example`, `.env.example`, `Dockerfile`, `docker-compose.yml`, `entrypoint.sh`, `requirements.txt`, `backend/requirements.txt`, `run_server.py`, `docs/TENANCY_BOUNDARY.md`, `docs/DATA_PIPELINE.md` (head), `dashboard.py` (head and outline), `.gitignore`.
- The owner's memory notes (`C:/Users/SaadTahir/.claude/projects/C--Users-SaadTahir-Videos/memory/`): `solarlytics-app.md`, `solarlytics-scope-decisions.md`, `solarlytics-review-oct-2026.md`, `no-console-windows.md`, `git-workflow-rules.md`, `few-agents-at-once.md`, `lighter-models-for-subagents.md`.
- The plan documents in `C:/Users/SaadTahir/Videos/solarytics/`: `Perfect-EMS_TOU_Dispatch_Deep_Review.md` lines 98-204 (open-source codebases, build vs reuse, product scope) and `Product 1 - TOU-Aware Energy Management and Dispatch.md` lines 309-375 (open-source comparison, composition, licence rules), 438-466 (architecture) and 557-578 (work packages).
- The owner's Python, checked by running it (`C:/Users/SaadTahir/anaconda3/envs/ops/python.exe -I`): Python 3.12.11, fastapi 0.141.1, pydantic 2.13.4, starlette 1.6.0, uvicorn 0.52.3, numpy 2.4.3, scipy 1.17.1 (`scipy.optimize.milp` imports), pandas 3.0.2, scikit-learn 1.8.0, xgboost 3.2.0, pydantic-settings 2.15.0, httpx 0.28.1, pytest 9.1.1. MISSING there: PyJWT, cryptography, pytest-xdist, highspy, pulp, pyomo, cvxpy, pvlib, apscheduler, sqlalchemy, alembic, streamlit. Nothing was installed.

Conventions of this note. A path is given from the repository root. "Line N" is the line in the file today. UNVERIFIED marks what I could not confirm from a file on this machine. The web repository `solarlytics-frontend` is NOT on this machine (the folder `C:/Users/SaadTahir/Videos/solarlytics` is the old one-folder v0.1-era workspace, last commit `c85fb5c`, and its `scripts/start_local.py` is the October-16 version, not the split launcher); everything said about the web app's launcher comes from the backend's record `docs/split/S0b.md`, from `docs/TROUBLESHOOTING.md`, and from the launcher's last version in the backend's git history (`git show d95060c^:scripts/start_local.py`), and is marked so.

---

## 1. solarlytics-backend: the sign-in, tenancy and founder-console module

### 1.1 What the backend is, in one paragraph

`backend/solarlytics/` is one flat Python package of 114 modules (`ls backend/solarlytics/*.py | wc -l`), plus `codepacks/`, `regionpacks/` and `switches/`. `api.py` alone is 6,835 lines (446 KB) and holds the FastAPI app, the guard middleware, most routes and the page catch-all; `store.py` is 2,196 lines (the SQLite store at schema version 20, lines 41 and 135-460). The sign-in and split of 24-25 October 2026 (stages P1, B1-B8, F1-F8, D1, IP, S0a, S0b, DOC, Z, each with a record in `docs/split/`) added the modules below, kept the engine and the API in this repository, and moved the web app to `saad-tahir/solarlytics-frontend`. `backend/tests/` holds 167 files; the last full run recorded in `docs/BUILD_PROGRESS.md` (session log row 2026-10-24 to 25) is "backend 4,470 passed, 4 skipped, 1 xfailed".

### 1.2 The exact file list with line counts (`wc -l`, 6 November 2026)

| File (`backend/solarlytics/`) | Lines | What it is | Imports from the package |
|---|---|---|---|
| `identity.py` | 345 | Who a bearer token names: local HS256 tokens the backend mints (`issue_local`, lines 185-202) or a Supabase project's ES256/RS256/HS256 tokens through its JWKS (`_jwks_key`, 258-275); `verify` (300-345) returns a frozen `Identity(user_id, email, name, issuer, session_id, aal, claims)` (102-111) or raises `Refused(status, code, detail)` (89-95); `check_startup(hosts)` (116-136) refuses a configuration that opens the server; the local signing key is `SOLARLYTICS_LOCAL_JWT_SECRET` or a `platform.secret` file made once with `O_EXCL` (164-182) | `jwt` (PyJWT). Lazy: `from . import platform as platformdb` at line 144 (only for `secret_file()`), to avoid the cycle platform -> auth -> identity |
| `platform.py` | 629 | The registry `platform.db`: companies, accounts, memberships, refresh tokens, the founder's audit, the record of sign-ins, and the two token-hash indexes (`share_index`, `api_key_index`). Its own `_connect` (128-155) with WAL, `user_version`, a copy to `backup/` before a migration (`backup`, 83-95), `BEGIN IMMEDIATE` under one lock; `_write()` (158-173) is one write transaction. `SCHEMA_VERSION = 1` (line 33). Companies 201-260, accounts 294-376, memberships 398-466, refresh tokens 484-533 (rotation with a 10-second reuse window, `Renewed`, 471-518), the lockout 538-578, the founder's audit 583-591, the indexes 602-629 | `from . import auth, store` (line 31). `store` is used ONLY for `store.DB_PATH` in `data_dir()` (line 62) |
| `companies.py` | 386 | From a token to the company, its database file and the person's member row: `resolve(token, wanted)` (356-386) -> `Who(user, company, db_path, identity)` (83-89). Local context from the registry (221-226), Supabase context from `rpc/get_my_context` with a 60-second keep and a 300-second grace (127-216), `_choose` (244-263), the first sight and the seat rule (`_first_sight` 276-308, `_member` 328-351) | `from . import auth, identity, store, store_members` and `platform` (42-44). Uses `store.use_company`, `store.use_org`, `store.DEFAULT_ORG`, `store.set_actor/reset_actor`, `store.audit` (305, legacy linking only) |
| `routes_local.py` | 153 | The local sign-in service: `POST /api/local/session`, `/refresh`, `/logout`, `GET /api/local/context`, `POST /api/local/password` (docstring lines 3-7; routes 76-153). Constant-time dummy hash for an unknown email (94-97), the lockout (48-55) | `from . import api as _api` (for `_api._fail`), `auth`, `schemas`, `platform`; lazy `identity` at line 40 |
| `routes_platform.py` | 382 | `/api/platform/*`: only `GET /api/platform/org` (266-273) and the API keys (278-309) are tenancy; the rest is Solarlytics commerce (a test message 98-119, the plan and its invoices 140-221, anonymising a customer 226-261, webhooks 321-382) | `apikeys, auth, netguard, notify, plans, regionpacks, store, store_members, webhooks, platform` (34-35); lazy `api`, `quotes` |
| `founder.py` | 317 | The founder console's own two-step sign-in (username + password, then a TOTP code against a challenge) and its 8-hour session token in the exact format of Boxlytics' `founder-auth.ts` at `boxlytics@c429bef` (docstring 14-17; `new_token` 157-163, `verify` 176-202); the four settings `SOLARLYTICS_FOUNDER_USERNAME`, `_PASSWORD_HASH`, `_TOTP_SECRET`, `_SESSION_SECRET` (54); `login` 224-240, `code` 243-270; the CLI `python -m solarlytics.founder hash | totp-secret | code` (275-313) | `from . import auth, mfa` and `platform` (48-49); lazy `store_founder` (246) |
| `routes_founder.py` | 412 | `/api/founder/*` (the contract is listed in the docstring, lines 4-22): login, login/code, session, overview, companies (list, create with its owner, patch, access on/off, members, backups list and make), users (list, create, password, patch, memberships add/change/remove), audit. Every route but the two sign-in steps has `dependencies=[Depends(_session)]` (63-69) reading the header `x-founder-session` | `from . import api as _api`, `founder, schemas, store_founder`, `platform` (39-41) |
| `store_founder.py` | 803 | What the console reads and changes: `_Registry` (local mode, 198-282) and `_Project` (Supabase REST with the service-role key, 398-522), chosen by `directory()` (527-529); `read_file(row)` (141-165) opens a company's file and counts Solarlytics things (projects, issued quote revisions, invoices, acceptances, calcs); `overview`, `companies`, `company_view`, `users`, memberships with the owner-stays rule (660-770); `company_backups`, `back_up` (793-803) | `auth, backups, identity, plans, quotes, store, store_members, platform` and `founder` constants (34-36) |
| `store_members.py` | 212 | The `members` and `role_presets` tables of a company's own file (schema v20, `store.py` 445-453): a member by the sign-in's `sub`, their app role and its source (`owner`, `legacy`, `default`, `set`), `count_members` (the seats), `as_user` (135-139, the dict `auth.current` holds), the users-of-before linking (`legacy_user`, `adopt_legacy` 144-164, which rewrites `OWNED = ("crm_projects", "followups", "project_drafts")`, line 21) | `from . import auth`; `from .store import _connect, _now, _org, write_transaction` (15-16) |
| `seed_demo.py` | 530 | `python -m solarlytics.seed_demo [--reset-passwords] [--from companies.csv users.csv] [--out file]`: the demo companies and people with random 20-character passwords written only to a git-ignored list and the founder's four settings to `<data folder>/.env.founder` (docstring 1-29; `seed` 360-434) | `auth, founder, identity, mfa, store, store_founder, store_members, platform` (47-48); lazy `api, companies` (306) because `_set_roles` (301-349) calls `api._enter/_leave` and `api.users_edit` |
| `backups.py` | 161 | Daily copies of every database (`daily_all` 82-108: the default file, each enabled company's file from `platform.company_files()`, and the registry), `KEEP = 14` daily and `KEEP_MANUAL = 10` (26-27), `restore` (127-140), `start_daily` (143-161) only with `SOLARLYTICS_DAILY_BACKUP=1`, an hourly thread | `from . import platform as platformdb`, `store` (23-24) |
| `mfa.py` | 99 | RFC 4226 HOTP and RFC 6238 TOTP written from the RFC text with `hmac`/`hashlib`/`base64`; `ISSUER = "Solarlytics"` (28); `verify` never accepts a code twice (77-93); accepts Arabic-Indic and full-width digits (35-37) | none from the package |
| `auth.py` | 171 | Roles and permissions of the app (`ROLES` 19, `PERMISSIONS` 23-46, Solarlytics-specific), the session constants (`SESSION_HOURS = 12`, `SESSION_DAYS = 7`, 49-50), the lockout constants (`MAX_TRIES = 5`, `LOCK_MINUTES = 15`, `MFA_ACCOUNT_TRIES = 20`, `CHALLENGE_MINUTES = 5`, 57-64), the rate limits read from `SOLARLYTICS_RATE_USER/ADDRESS/PUBLIC` (80-82), the two ContextVars `current` and `company` (106-109), PBKDF2-SHA256 with 200,000 iterations (`ITERATIONS` 54, `hash_password` 112-115, `check_password` 118-124), `open_mode` (139-144), `can/need/actor` (147-171) | lazy `identity, platform` (143); lazy `from .routes_accept import Limiter` (90) for the sliding-window rate limiter |
| `apikeys.py` | 165 | Scoped API keys: prefix `slk_` (48), SHA-256 kept, `SCOPES` (34-43) and `RESOURCES` (45-46) name Solarlytics paths (quotes, customers, projects); `add/listing/revoke/by_token/acting_user` (115-165) on the `api_keys` table of a company's file (schema v17, `store.py` 420-423); the registry's `api_key_index` says which company holds a key (`add` 121, `revoke` 144) | `from . import auth, store` and `platform` (31-32) |
| `link_company.py` | 257 | CLI that copies the owner's old one-file workspace into `companies/<id>.db`, registers its share links and keys, makes the company (115-200). Reads Solarlytics tables (`runs`, `users`, `share_links`, `api_keys`, `orgs`) | `identity, store, platform` (34-35) |
| `store.py` (the parts the module needs) | 2,196 total | `DB_PATH` from `SOLARLYTICS_DB` (33), `DEFAULT_ORG` (35), the ContextVars `_ORG`, `_DB`, `_ACTOR` (36-40), `SCHEMA_VERSION = 20` (41), `set_company/reset_company/use_company` (80-99; `set_company(None)` is refused, 83-85), `set_actor/actor` (102-113), `backup(tag)` (475-487), `_connect` (490-526: WAL once, a copy before a migration, `BEGIN IMMEDIATE`), `_migrate` (135-460), `audit` (1402), `write_transaction` (956) | none of the sign-in modules; `store` is the host |
| `api.py` (the parts the module needs) | 6,835 total | `ALLOWED_HOSTS` and `identity.check_startup` (104-107); CORS lists (148-154; `CORS_ALLOW_HEADERS` names `X-Solarlytics-Company` and `x-founder-session`); `ERROR_CODES`, `SIGN_IN_CODES`, `SECURITY_HEADERS`, `MAX_BODY` (161-173); `_error_body/_refusal` (179-186); `PUBLIC_PATHS`, `LOCAL_POSTS`, `IDENTITY_PATHS`, `FOUNDER_PREFIX` (261-276); the guard (279-489): request id, host and origin checks, JSON-only writes, body limit, rate limits, bearer token or API key, the company's file through `_enter/_leave` (328-343), open mode (467-472), the 500 record; `_VersionedPath` (/api/v1 rewrite, 492-504), `_BodyLimit` (507-542); middleware order (545-551); `_fail` (705); `GET /api/health` (1183-1185); `GET /api/auth/state` (4897-4905); `PROGRAM_ROUTES` mounted route by route with `app.add_api_route` (6788-6802); the SPA catch-all only with `SOLARLYTICS_WEB_DIST` (6808-6835) | the host |
| `version.py` | 42 | `STAMP = "<version>+<10 hex of SHA-256 over every .py and .json of the package>"`, worked at import (28-42); sent as `X-Solarlytics-Engine` on every /api answer (`api.py` 298) | `from . import __version__` |
| `schemas.py` (one class) | 460+ | `Refusal(HTTPException)` with `code` and `extra` (27-33): every refusal is `{detail, code, request_id}` | `rules, workflow` (Solarlytics) |

Total of the fifteen sign-in modules named in the task (identity, platform, companies, routes_local, routes_platform, founder, routes_founder, store_founder, store_members, seed_demo, backups, mfa, auth, apikeys, link_company, version): 5,064 lines; with `store.py` and `api.py` the task listed, 14,095 (`wc -l` total).

### 1.3 The import graph and its two deliberate cycles

```
mfa.py  -> (nothing)
auth.py -> [lazy] identity, platform; [lazy] routes_accept.Limiter
identity.py -> jwt; [lazy] platform (secret_file only)
platform.py -> auth, store (DB_PATH only)
store_members.py -> auth, store (_connect, _now, _org, write_transaction)
companies.py -> auth, identity, store, store_members, platform
apikeys.py -> auth, store, platform
backups.py -> platform, store
founder.py -> auth, mfa, platform; [lazy] store_founder
store_founder.py -> auth, backups, identity, plans, quotes, store, store_members, platform, founder
routes_local.py -> api (lazy use of _api._fail), auth, schemas, platform; [lazy] identity
routes_founder.py -> api, founder, schemas, store_founder, platform
routes_platform.py -> apikeys, auth, netguard, notify, plans, regionpacks, store, store_members, webhooks, platform
seed_demo.py -> auth, founder, identity, mfa, store, store_founder, store_members, platform; [lazy] api, companies
link_company.py -> identity, store, platform
api.py -> apikeys, auth, backups, companies, identity, store, store_members, platform, version, ... and at the end routes_founder, routes_local, routes_platform
```

The two cycles are broken on purpose: `platform -> auth -> [lazy] platform` (`auth.py` line 143) and `platform -> auth -> [lazy] identity -> [lazy] platform` (`identity.py` line 144). The route modules import `api` at module level and use it inside handlers because `api.py` imports them "while it is still being read" (`routes_platform.py` lines 2-3, `api.py` 6788-6789). These three patterns carry over unchanged.

Who else in the package imports the sign-in modules (grep of `import ... identity|platformdb|companies|store_members|store_founder|founder|mfa|apikeys` and `from . import platform`): `api.py`, `imports.py` (the workspace import/export), `schemas.py`, `store.py`, besides the modules themselves. So the coupling from the rest of Solarlytics into the sign-in is small: `api.py` (the guard and `/api/auth/state`, `/api/users`), `imports.py` and `schemas.py` (UNVERIFIED what exactly; the grep matched only, I did not read those two for it).

### 1.4 How it works, end to end (what the EMS would inherit)

**Files on disk (the data folder).** `platform.data_dir()` (platform.py 59-62) is `SOLARLYTICS_DATA_DIR`, else the folder of `store.DB_PATH` (`SOLARLYTICS_DB`, default `<repo>/data/solarlytics.db`). In it: `platform.db` (the registry, schema v1), `platform.secret` (local mode's signing key when the env var is not set), `companies/<uuid>.db` (one full workspace per company, made at the first request through `store._connect` at the current schema), `backup/` (copies of `solarlytics.db` and `platform.db`), `companies/backup/` (each company's copies, named `<company id>-<date>-<time>-<tag>.db`), `logs/solarlytics.log` (a 2 MB rotating file, five kept, `api.py` 36-47), `.env.founder` (written by the seed, read by the web launcher), `solarlytics.db` (the default workspace, opened only in open mode). `docs/ACCOUNTS.md` lines 155-168 and `docs/DEPLOY.md` lines 60-72 say the same.

**The request path (api.py `_guarded`, 346-489).** (1) Host header checked against `ALLOWED_HOSTS` (this machine's names plus `SOLARLYTICS_HOSTS`), 400 `unknown_host` otherwise; (2) Origin checked, 403 `cross_site`; (3) a POST/PUT/PATCH/DELETE under `/api/` must be `application/json` (415) and under 25 MB (413); (4) per-address rate limit; (5) public paths: `/api/health`, `/api/auth/state`, the three local sign-in POSTs (in local mode only), share links, acceptance routes, `/api/founder/*`; (6) `Authorization: Bearer` is either an API key (`slk_` prefix: company from `platformdb.index_find("api_key_index")`, key row from that company's file, scope check) or an access token (`companies.resolve` off the event loop with `run_in_threadpool`; `X-Solarlytics-Company` names the company when the person has several); (7) per-user or per-public-address rate limit; (8) `_enter` sets `auth.current`, `auth.company`, `store.set_company(db_file)`, `store.set_org("default")` and `store.set_actor`; (9) open mode: with no token, no account in `platform.db` and the client on `auth.LOCAL_CLIENTS`, the request works on the default file, otherwise 401 "Sign in first"; (10) `call_next`, with any exception logged as one JSON record and answered 500 `{detail, code, request_id}`; (11) `_leave` resets the ContextVars; the answer gets `X-Request-ID`, the security headers, `X-API-Version: 1`, `X-Solarlytics-Engine` and, for a person, `X-Solarlytics-Role`.

**Sign-in (local mode).** `POST /api/local/session {email, password}` -> `platform.check_sign_in` (PBKDF2 checked first so a disabled account takes as long) -> `identity.issue_local` (an HS256 JWT in the claim shape of a Supabase access token: `iss solarlytics-local`, `aud authenticated`, `sub` uuid, `amr [{method: password}]`, `app_metadata.provider email`, one hour) plus a refresh token (`secrets.token_urlsafe(32)`, kept as SHA-256, family id, 12 hours unused, 7 days from sign-in, rotated at every use; a reuse within 10 seconds is two tabs, 409 `session_renewed`; a reuse later ends the whole family). Five wrong passwords for one account from one address within 15 minutes lock that address for 15 minutes (`platform.lock_wait`); the address is `request.client.host` unless `SOLARLYTICS_TRUST_PROXY=1` (538-545). Every request reads the account again (`platform.signed_in(sub, iat)`: a password change or a disable sets `sessions_from` and refuses older tokens at once).

**Tenancy.** A company is a row of `platform.db` (`id, name, enabled, seats, disabled_reason, created, db_file`); its database is `db_file` when set (tests) else `companies/<id>.db`. A membership is `(user_id, company_id, role in owner|admin|member, status in pending|active|suspended)`. Only an active membership of an enabled company lets a person in; the owner is a pinned administrator; everyone else starts as a viewer unless a `role_presets` row names a role; seats are counted against members not switched off (`companies._seated`, 272-273, under `_seat_lock`). Refusal codes and statuses: `api.SIGN_IN_CODES` (164-165): `token_invalid 401, token_expired 401, session_ended 401, invalid_credentials 401, not_a_member 403, no_company 403, company_disabled 403, member_disabled 403, no_seat 403, company_required 409, session_renewed 409, identity_unavailable 503`. The web app's `nextStep` acts on these codes (`api.py` line 163 says so; the web repository is not on this machine: UNVERIFIED).

**The founder console.** Not an account: four environment settings, 503 until all four are set and well formed (`founder.need_set_up`, 111-119). `POST /api/founder/login {username, password}` -> a 43-character challenge kept as its hash in `account_events` under `WHO = "founder"` (so it can never collide with an email); `POST /api/founder/login/code {challenge, code}` -> `store_founder.take_code` (one code per 30-second step, never twice, 5 tries per challenge, 5 minutes) -> `{token, expires_at}`; the token is `base64url({"sub":"founder","iat":..,"exp":..})` + `.` + `base64url(HMAC-SHA256)` (`founder.py` 145-163), checked in constant time, nothing kept server-side. Locks: 5 wrong passwords per address, 5 wrong codes per address, and 20 wrong codes from every address together in 15 minutes (`_locked`, 212-221). Every action and every sign-in attempt goes to `founder_audit` in `platform.db`. The console's reads of a company's file (`store_founder.read_file`) are Solarlytics counts (projects, issued quotes, invoices, acceptances, calcs: lines 156-161).

**Backups and migrations.** Three independent copy mechanisms, all SQLite's own `Connection.backup` (consistent while another connection writes): (a) `store._connect` copies a company's file as `<stem>-<date>-<time>-schema-v<N>.db` before any migration (`store.py` 508-510) and `platform._connect` does the same for the registry (142-143); (b) `backups.start_daily` takes one copy a day of every file while the app runs, keeping 14 (`KEEP`), on an hourly check, only with `SOLARLYTICS_DAILY_BACKUP=1` (the launcher and the image set it); (c) the founder's "Back up now" keeps 10 manual copies per company (`KEEP_MANUAL`). Restore is a hand step with the app stopped (`scripts/restore_backup.py`, which calls `backups.restore`; it refuses a copy that has no `runs` table, `backups.py` 773-775: a Solarlytics probe). Migrations are a `user_version` integer and a chain of `if version < N:` blocks that must be idempotent (`store._migrate` 135-460, comment at 444: "Every statement can run again"); the registry's chain is `platform._migrate` (98-125). The container runs one uvicorn worker and "no two containers share the volume" (`Dockerfile` 8-9), because the first sight and the seats are ordered within one process (`companies._seat_lock`).

**Supabase mode (later).** `identity.verify` checks ES256/RS256 against the project's JWKS (`/auth/v1/.well-known/jwks.json`, kept 600 s, a 3,600 s grace while unreachable) or HS256 with the legacy secret; `companies._project_context` calls `rpc/get_my_context` with the person's own token; `store_founder._Project` calls the Admin API with the service-role key. The SQL in `supabase/` (`sql/solar_000_preflight.sql`, `migrations/solar_001_accounts_and_companies.sql`, `solar_001_rollback.sql`) ships for the owner's review and has never been applied (`supabase/README.md` lines 5-7). The project does not exist (README "Still missing").

### 1.5 Every environment name the module reads

From `.env.production.example` (every setting the server reads is pinned there by `backend/tests/test_deploy_docs.py::test_every_setting_the_server_reads_is_in_the_example`, lines 52-57, which greps the package for literal `SOLARLYTICS_...` strings):

| Group | Names |
|---|---|
| Sign-in | `SOLARLYTICS_AUTH_MODE` (local, default; supabase), `SOLARLYTICS_LOCAL_JWT_SECRET` (32+ chars; needed once `SOLARLYTICS_HOSTS` names a host other than this machine), `SOLARLYTICS_SUPABASE_URL`, `_SUPABASE_ANON_KEY`, `_SUPABASE_SERVICE_ROLE_KEY`, `_SUPABASE_JWT_SECRET`, `_JWT_AUDIENCE` (authenticated), `_JWKS_TTL` (600), `_MEMBERSHIP_TTL` (60) |
| Founder console | `SOLARLYTICS_FOUNDER_USERNAME`, `_FOUNDER_PASSWORD_HASH` (PBKDF2 or Boxlytics' plain sha256 hex, taken with a warning), `_FOUNDER_TOTP_SECRET` (Base32), `_FOUNDER_SESSION_SECRET` |
| Hosts and proxy | `SOLARLYTICS_HOSTS`, `_CORS_ORIGINS` (comma list or JSON array, never `*`), `_CORS_ORIGIN_REGEX`, `FORWARDED_ALLOW_IPS` (uvicorn's), `SOLARLYTICS_TRUST_PROXY`, `_PORT` (8060 in the image) |
| Files | `SOLARLYTICS_DB`, `_DATA_DIR`, `_DAILY_BACKUP`, `_RESET_BACKUP`, `_LOGS`, `_WEB_DIST` |
| Limits and switches | `SOLARLYTICS_RATE_USER` (6000/min), `_RATE_ADDRESS` (20000), `_RATE_PUBLIC` (1200), `_ERROR_WEBHOOK`, `_OFFLINE`, `_WEBHOOK_LOCAL`, `_DOCS` |
| Tests and tools (not in the example) | `SOLARLYTICS_FRONTEND_DIR`, `_SKIP_SIBLING`, `_REQUIRE_SCANNER`; the launcher's `SOLARLYTICS_API_PORT` (8041), `_WEB_PORT` (5192), `_PYTHON`, `_BACKEND_DIR` |
| The web app (build time) | `VITE_AUTH_MODE`, `VITE_API_BASE_URL`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` |

The convention: the module reads `os.environ` at import time into module constants (`identity.py` 47-58, `auth.py` 80-82) or at each call for what a test may move (`platform.data_dir`, every founder setting: "Each is read when it is needed, never kept", `founder.py` line 11). There is no settings object. Tests set the environment in `conftest.py` (lines 8-28) before the package is imported, and `monkeypatch.setattr(identity, "MODE", ...)` for a mode.

### 1.6 The tests that hold the module

| Test file (`backend/tests/`) | Lines | What it holds |
|---|---|---|
| `conftest.py` | 102 | `SOLARLYTICS_DB` to a temp file, `SOLARLYTICS_HOSTS=testserver`, `SOLARLYTICS_AUTH_MODE=local` with a made-up 43-character signing key, the founder's username and session secret made up, CORS origins for the hygiene tests; autouse fixtures that fail any test leaving users, members or accounts in the shared test database (58-72), clear `companies`/`identity` caches (75-82), reset the rate limiter (85-92), and stub the resolver (95-102) |
| `identities.py` | helper | `company()`, `account()`, `token()`, `client()`: a signed-in TestClient of a person with a role in a company, the company registered in the test's own registry with `db_file = store.DB_PATH` (or `own_file=True` for a hosted-style `companies/<id>.db`) |
| `test_identity.py` | 572 | Both modes offline: an EC P-256 JWKS made in the test, `identity._fetch_json` replaced; `alg none`, a wrong key, an expired token, a Google sign-in refused, the kept secret file |
| `test_platform.py` | 607 | The registry: accounts, memberships, refresh rotation and reuse, the lockout, the founder's audit, concurrency with a second process (`subprocess`), the indexes |
| `test_companies.py` | 723 | `resolve` in both modes against `fixtures/get_my_context.json`; the seat rule; a disabled account refused on the next request; `ITERATIONS` lowered to 1000 for speed (line 47) |
| `test_founder.py` | 1,021 | The two-step login with a moved clock, the token compared with one Boxlytics' own `createSessionToken` made under Node 22 (`BOXLYTICS_TOKEN`, lines 36-41), every route, the audit, both modes with an in-memory stand-in project |
| `test_signin_guard.py` | 370 | The guard end to end: two companies with their own files never see each other's rows; API keys and share links found through the index; expired and forged tokens |
| `test_auth.py` | 179 | Open mode until the first account; the lockout; the sales role never sees a cost; the `db` fixture every sign-in test reuses (lines 32-36: `monkeypatch.setattr(store, "DB_PATH", tmp_path / "auth.db")`) |
| `test_seed_demo.py` | 479 | The seed's files, passwords never printed, `--reset-passwords`, `--from`, run again keeps everything |
| `test_link_company.py` | 639 | The link command on copies, refusals, WAL handling |
| `test_old_signin_removed.py` | 190 | The old cookie sign-in's 19 routes answer 404; nobody reads the cookie |
| `test_security_b1.py` | 186 | Stage B1's security review items |
| `test_company_files.py` | 290 | Each company's file, its backups, the daily loop over companies |
| `test_supabase_sql.py` | 484 | The SQL files parsed and checked (never applied) |
| `test_deploy_docs.py` | 121 | `.env.production.example` names every setting, holds no secret, opens nothing; `docs/DEPLOY.md` names every setting and the hand steps |

About 5,900 lines of tests for about 5,100 lines of module. The tests have the same prose docstring style as the modules ("Stage B6 of the sign-in and split: ...") and name the decision document each rule comes from.

### 1.7 Judgement: can the module be lifted into the EMS backend as a package?

**Yes, as a package with a small host contract, if six things are edited.** The sign-in, the registry, the per-company files, the founder console and the backups are already a module in all but the folder: they share one `store` context (`DB_PATH`, `set_company`, `use_org`, `_connect`, `backup`, `audit`) and one `api` helper set (`_fail`, `Refusal`, `_enter/_leave`), and nothing else. What is Solarlytics-specific is small and named below. The web app's sign-in pages, the company switch and the founder console pages are in the frontend repository (`docs/ACCOUNTS.md` lines 4-7: `/login`, `/admin`, `session.ts`, `founderAdmin.store.ts`, `companyswitch.tsx` per `docs/split/DOC.md` line 85) and can be lifted with the same routes; I could not read them (UNVERIFIED).

**Lift verbatim (rename `SOLARLYTICS_` and the product words only):**

| File | Edits |
|---|---|
| `mfa.py` | `ISSUER = "Solarlytics"` (line 28) -> the EMS product name. Nothing else |
| `identity.py` | Env names; `LOCAL_ISSUER = "solarlytics-local"` (49); the logger name `"solarlytics"` (37); the lazy import path of `platform` (144). Keep both modes |
| `platform.py` | Nothing but the one `store.DB_PATH` read (62) if the host's `store` keeps that name; `SCHEMA_VERSION = 1` stays |
| `founder.py` | Env names (54); the CLI's `prog` string (281); the `mfa.ISSUER` label (304). Keep the Boxlytics-format token: the owner allowed reusing Boxlytics' sign-in parts "as is" for the critical parts and asked that each reuse be recorded in DESIGN_LOG (memory `solarlytics-scope-decisions.md`, "Reuse"; `founder.py` 14-17 cites `supabase/functions/_shared/founder-auth.ts` at `boxlytics@c429bef`). Record it again in the EMS DESIGN_LOG |
| `routes_local.py` | The import of `api` for `_fail` (27) and of `schemas` for `Refusal` (28): point them at the EMS's `api` and `schemas`, or give the package its own `errors.py` with `Refusal` and `fail()` (recommended, section 4) |
| `routes_founder.py` | Same two imports (39-40); otherwise verbatim |
| `backups.py` | The probe `SELECT ... name = 'runs'` in `restore` (774) -> the EMS's own first table (for example `sites`); env names; the logger name |
| `store_members.py` | `OWNED = ("crm_projects", "followups", "project_drafts")` (21) -> the EMS tables whose `owner` is a user id, or an empty tuple; `legacy_user`/`adopt_legacy` (144-164) and the `legacy_user_id` column can be dropped entirely with the "users of before" branch of `companies._first_sight` (283-306), since the EMS has no old workspace to link |
| `companies.py` | Drop the legacy branch (see above); the header name `X-Solarlytics-Company` is read in `api.py` (420), not here; `MEMBER_ROLE = "viewer"` (70) stays or becomes the EMS's lowest role |

**Edit before lifting (the Solarlytics-specific parts):**

1. `auth.py` `ROLES` (19) and `PERMISSIONS` (23-46) are the solar quotation roles (engineer, estimator, sales, approver). The EMS needs its own table (a proposal: `admin`, `operator` (writes plans and overrides), `analyst` (reads telemetry, reports), `viewer`; permissions such as `edit_site`, `edit_tariff`, `approve_plan`, `override`, `manage_users`, `edit_workspace`, `see_reports`) and the `can/need/roles_words` machinery stays. `auth.limiter()` (86-92) imports `Limiter` from `routes_accept.py` (a Solarlytics route module): copy the `Limiter` class into a `ratelimit.py` of the package (`routes_accept.py` lines 80-110: `__init__(clock)`, `hit(key, limit, window)`, `reset()`, about 30 lines, a sliding window in memory).
2. `apikeys.py` `SCOPES` (34-43), `RESOURCES` (45-46), `PREFIX = "slk_"` (48) and the `api_keys` DDL (`store.py` 420-423) -> EMS resources (for example `sites:read|write`, `telemetry:write`, `plans:read`, `reports:read`), a new prefix, the same table.
3. `store_founder.read_file` (141-165) counts Solarlytics tables; replace the five SQL lines with EMS counts (sites, days of telemetry, plans issued, last solve) and `ACTIVITY` keys; `_plan` (135-138) reads Solarlytics' plan state (`plans.status`), drop or replace. `_Project` (Supabase) stays as is.
4. `routes_platform.py`: take only `org_mine` (266-273) and the three key routes (278-309) into a small `routes_platform.py`; leave the test message, plans, invoices, anonymise and webhooks behind (they import `netguard, notify, plans, regionpacks, webhooks`).
5. `seed_demo.py` `_set_roles` (301-349) calls `api.users_edit` and `api._enter/_leave`: the EMS host must provide the same users route (`PUT /api/users/{id}` with a `role` field, `api.py` 4920-4926) or the seed sets roles through `store_members.update_member` directly; `DEMO_COMPANIES/DEMO_PEOPLE` (61-67) become EMS demo sites' companies (the owner's decision kept the Boxlytics company and user names with new passwords for Solarlytics; the EMS may do the same).
6. `link_company.py`: do not lift (no old workspace); keep as a pattern for a future "import a workspace" command.

**What the host app must provide (the contract the package is written against):**

| Needed by | Host symbol | Where Solarlytics has it |
|---|---|---|
| platform.py | `store.DB_PATH` (a `Path`, re-read at each call) | `store.py` 33 |
| companies.py, backups.py, store_founder.py, seed_demo.py | `store.use_company(path)`, `set_company/reset_company`, `db_path()`, `use_org`, `set_org/reset_org`, `DEFAULT_ORG`, `set_actor/reset_actor`, `audit(actor, action, target, detail)`, `backup(tag)`, `_connect()`, `_now()`, `write_transaction()`, `SCHEMA_VERSION` | `store.py` 33-131, 475-526, 956, 1402 |
| store_members.py, apikeys.py | tables `members`, `role_presets` (v20 DDL at 445-453), `api_keys` (v17 DDL at 420-423), `audit` with `actor_id, actor_email` (454-457) in the per-company schema | `store._migrate` |
| routes_local.py, routes_founder.py, routes_platform.py | `api._fail(message, code)` -> raises `schemas.Refusal(422, ...)`; `schemas.Refusal(status, detail, code, headers, extra)` | `api.py` 705, `schemas.py` 27-33 |
| the guard | everything in `api.py` 97-107 (hosts, `check_startup`), 148-186 (CORS lists, codes, headers, `_refusal`), 255-343 (public paths, `guard`, `_enter/_leave`), 346-489 (`_guarded`), 492-551 (the two ASGI wrappers and the middleware order), 554-600 (the exception handlers, which I read only in part), 1183-1185 (health), 4897-4905 (auth state), 4908-4942 (users list and edit), 6788-6802 (mounting `PROGRAM_ROUTES` route by route, needed because FastAPI 0.150 keeps an included router as one entry of `app.routes`, comment at 6796-6797) | `api.py` |
| the launcher and the seed | the data folder convention (`SOLARLYTICS_DB`, `_DATA_DIR`), `.env.founder` | `platform.data_dir`, `seed_demo.env_file` |

A workable cut of `api.py` for the EMS is about 450 lines (the parts listed), not 6,835: Solarlytics' `api.py` grew by accretion ("The completion program's routes ... one module each, mounted here", 6786-6787, is the later, better pattern). Section 4 proposes the layout.

**What the package would weigh in the EMS:** about 4,300 lines of module (the fifteen files less `link_company.py` and most of `routes_platform.py`) plus about 450 lines of `api.py` and 130 lines of `store.py` context code, with about 5,000 lines of tests to carry over and re-aim (`test_identity`, `test_platform`, `test_companies`, `test_founder`, `test_signin_guard`, `test_auth`'s first five, `test_seed_demo`, `test_company_files`, `test_old_signin_removed` is not needed, `test_deploy_docs`).

**Risks and what to check first.**

- The guard puts `companies.resolve` in a thread pool per request and reads `platform.db` on every request (local mode: "Nothing is kept", `companies.py` 10). At Solarlytics' scale that was measured fine (`scripts/load_check.py`; `docs/HANDOFF_NEXT.md` "Load and concurrency, measured on 20 October", not read in detail). An EMS ingesting 1-minute telemetry from many sites through the same guard would make one `platform.db` read per telemetry POST. Batch telemetry (one POST per minute per site carries 60 rows or more) or give telemetry its own API-key path that caches the key's company for a minute. UNVERIFIED cost; measure with `load_check.py`'s pattern.
- Per-company SQLite files suit the EMS's tenancy (a site belongs to one company) and the owner's taste, but telemetry volume is new: one site at 1-minute resolution with 8 channels is about 4.2 million rows a year. SQLite handles that in one file per company if the telemetry table is keyed `(site_id, ts)` and WAL stays on; the backup copy of a multi-GB file once a day through `Connection.backup` is slow and doubles disk. Decide early: telemetry in a separate `companies/<id>-telemetry.db` (same `use_company` machinery, a second ContextVar) or in Parquet/DuckDB files beside the SQLite workspace, with only daily aggregates in the workspace file. Not a blocker for the lift; a blocker for `backups.daily_all` as written.
- `uvicorn` runs one worker and the stage B7 review says one server process per data folder. The optimiser solves (sub-second LP, up to 30 s MILP with a genset per WP6's acceptance) must go through a job pool like `jobs.py` (section 4), never inline in a request.
- `identity.py` needs PyJWT and cryptography, which the owner's `ops` env lacks (`CLAUDE.md` line 8-9; verified above). The EMS backend needs its own venv like `solarlytics-backend/.scratch/venv` (memory `solarlytics-app.md`, "Current setup (29 Oct 2026)").
- The Boxlytics-derived founder token format is a deliberate port with the owner's permission; the rest of the module is OnyxLeaf's own code written in October 2026, so there is no licence question in reusing it in another OnyxLeaf product. Record the reuse (file, commit `c853130`) in the EMS DESIGN_LOG as `CLAUDE.md` asks for Boxlytics reuse.

---

## 2. The conventions the owner approved (Solarlytics)

### 2.1 The rules in `CLAUDE.md` (26 lines, the short version the agent reads first)

Quoted from `CLAUDE.md` lines 8-19, each a rule the EMS repositories should carry with the product name changed:

1. "Python is `C:/Users/SaadTahir/anaconda3/envs/ops/python.exe`, not `python` on PATH. The sign-in needs PyJWT and cryptography from `requirements.txt`: install them into it, or point `SOLARLYTICS_PYTHON` at a Python that has them." (In practice the backend used its own venv, `.scratch/venv`; memory `solarlytics-app.md`.)
2. "Boxlytics belongs to another party: never change it. Reuse its code only where the owner's decisions allow it, and record each reused file with its path and commit."
3. "Engine IP stays here: the web app asks the API and only draws the answers."
4. "The backend tests read the web app's files only through `backend/tests/sibling.py` (`frontend_file`)."
5. "OpenSees only under `verification/`." (For the EMS: the heavy or copyleft tools, python-microgrid (LGPL), CityLearn, PySAM, stay under `verification/` or `bench/`, never imported by the product: the same rule.)
6. "Never delete or reseed `data/solarlytics.db`; back it up before schema changes. Never touch `C:/Users/SaadTahir/Videos/solarlytics` (the owner's real workspace)."
7. "Work on a branch. Never merge, and push only when the owner asks in the conversation: the owner opens the pull requests. The remote `origin` is GitHub's saad-tahir/solarlytics-backend here ...; never add another."
8. "Do not edit `docs/MARKETABILITY_AND_PRODUCT_READINESS_AUDIT_2026-10-14.md`." (a third party's document kept verbatim)
9. "Every safety fix gets a regression test; every money calculation gets a golden test."
10. "Update `docs/BUILD_PROGRESS.md` at the end of every session."

And its first paragraph names the reading order: `docs/BUILD_HANDOFF.md` (rules, environment, build order, session protocol), then the session log at the top of `docs/BUILD_PROGRESS.md`, then the governing document (`docs/MARKET_READINESS_AUDIT.md`). The memory rule `git-workflow-rules.md` repeats rule 7 for every repository under `Videos`.

### 2.2 The rules in `docs/BUILD_HANDOFF.md` (162 lines, 16 October 2026) and the session protocol

Section 4, "Standing rules (do not break these)", lines 45-56: (1) clean-room (new code; patterns only from Boxlytics and open source; sources recorded in DESIGN_LOG), (2) hands off other repositories, (3) shared files with a sibling product may diverge, each divergence noted, (4) the verification-only dependency stays in `verification/`, (5) protect the owner's data (never delete, reseed or overwrite the live database; a copy to `data/backup/` before any schema change; tests on a temp database), (6) git as above, (7) engineering honesty (`[verify]` marks, cite the clause, never reproduce a standard's tables), (8) never claim more than the code does, (9) fixes come with tests (a regression test that fails on the old code; a golden test with hand-checked numbers for money), (10) do not delete working features.

Section 5, "Build order", lines 58-83: numbered steps, then phases; "Before starting each phase, write a short plan (items, files, data model changes, tests) at the top of that phase's entry in the session log, then build."

Section 6, "Decisions already made", lines 85-102: a table `# | Question | Decision for this build` that answers the master document's open questions so the build is not blocked, and names which questions are the owner's.

Section 8, "Session protocol (how to resume)", lines 134-151, in substance:

- At the start of every session: read this file, then the session log at the top of `BUILD_PROGRESS.md`, then `git log --oneline -15` on the build branch; run the backend tests, the verification tests and `npx tsc --noEmit`, fix anything failing first; continue at the "Next item" named in the last log line.
- For each item: read its row in the master and the gap rows it traces to; write the test first where the item is engineering or money; implement backend, API and frontend together, SI units in the engine; run all checks, and for UI or PDF changes drive headless Chrome; update `EQUATIONS.md`, `DESIGN_LOG.md`, `PRODUCT_BRIEF.md` or Help text; tick the item in `BUILD_PROGRESS.md` (`[x]` only when code, tests and docs are done; `[~]` if partly).
- At the end of every session: add a line to the session log (date, items finished, anything half done, the next item), and commit on the build branch.
- "Stop and ask the owner when: a step needs an owner task (lawyer, engineer, real prices, standards); a fix would change the result of an existing verified case beyond what the fix explains; a change would delete or overwrite saved projects; or you are about to touch anything outside" the repository.

Section 9, "Done means", lines 153-162: a table `Level | Software-checkable test` per phase, ending "The owner-only items ... remain open until the owner closes them. The app is not sellable until they are, whatever this checklist says."

The later programs added two protocol documents in the same voice: `docs/program/ARCHITECTURE.md` (21 October) opens with "Decisions, not essays" and a table of invariants `I1..I8` each with "How it is proved" (I1 defaults unchanged, I2 422 never 500, I3 no cost to a role that may not see it, I4 old data loads, I5 a migration backs up first, I6 every money calculation has a golden test, I7 every new check has a hand case and its switch off equals HEAD, I8 no new runtime dependency); and every stage of the split has a record `docs/split/<stage>.md` with the sections Status (a checklist), What the stage does (a commit table), Tests (counts), Review (a numbered table of findings, fixes and the tests that pin them). The EMS plan should keep the invariants idea (for the optimiser: a plan on a replayed day is reproducible to the rupee; a new setting at its default gives the same plan).

### 2.3 The documents set and the shape of each

| File | Size today | Shape | Who it is for |
|---|---|---|---|
| `docs/PRODUCT_BRIEF.md` | 127 lines | `## 1. What the app does today` (a table `# / Module / Question it answers / Main outputs`), `## 2. Inputs` (a table per tab), `## 3. Method` (one paragraph per method, then `### Verification`, a table Check / Reference / Result, and the test counts), `## 4. Who buys it and why` (buyer table, "Why now", "Price to test"), `## 5. Before first customer use` (numbered), `## 6. Next modules`, `## 7. What version 0.1 does not have` | the owner, a buyer, a new engineer |
| `docs/EQUATIONS.md` | 197 KB, 307 items | "Every equation in the engine, where it comes from, and how it has been checked"; a status column A (checked against an independent solver), B (against a published source), C (a standard formula checked by hand, coefficients from memory, `[verify]`), D (a simplification of ours); sections by domain, each a table `# / Equation / Code (module.function) / Source / Checked by / Status`; `## 7. What checks what`, `## 8. Still to do, in order of value` | engineers; the EMS equivalent is the MILP formulation, the tariff rules and the battery model, each row citing the SRO or the data sheet |
| `docs/ASSUMPTIONS.md` | 134 KB, 338 rows | Assumption, Basis, Confidence (official, secondary, recalled, draft, sample, design choice), Change it in, Record; the invariant "a new setting at its default gives the result the app gave before" | the owner and a tax adviser or engineer |
| `docs/COMPETITORS.md` | 81 lines | `## 1. Tools found` (tables per category: Tool, What it does, Who uses it, Price, Limits for our buyers), `## 2. What each buyer uses today`, `## 3. Feature by feature` (a matrix), `## 4. Selling points, strongest first`, `## 5. Where the others are ahead`, `## 6. What this means for the next month`; a dated web search, prices as the vendors state them | the owner |
| `docs/DESIGN_LOG.md` | 737 KB, 63 sections | Dated sections, newest at the end; the shape is in `docs/DESIGN_LOG_GUIDE.md` section 1 (section 5 of this note has the template); the first section is the clean-room statement, method sources, data shipped (a table Data, Source, Terms), decisions (Decision, Reason), third-party software (Package, Use, Licence) | the record |
| `docs/TEST_MATRIX.md` | 49 KB | `## 1. The four suites` (Suite, Command, Time, Last result, Must end with), `## 2. What each kind of change must carry` (Change, Test it must come with), `## 3. The goldens and the replay set`, open cases, the program's tests by package | whoever runs the checks |
| `docs/OWNER_CHECKLIST.md` | 61 KB | Part A the owner once, Part B each customer workspace, Part C before each firm quote; checkbox lines with where it is done in bold and why after a dash | the owner |
| `docs/HANDOFF_NEXT.md` | 61 KB | "State on (date): READ THIS FIRST" at the top, older states kept as history below; where the branch stands, CI, checks, schema version; what was added; how it was done; open, in order; documents to read; how to run the four suites; next | the next team |
| `docs/BUILD_PROGRESS.md` | 244 KB | Section 5 of this note | the resume point |
| `docs/BUILD_HANDOFF.md` | 19 KB | Section 2.2 and section 5 | the first thing read |
| `docs/ACCOUNTS.md`, `docs/TROUBLESHOOTING.md`, `docs/DEPLOY.md` | 13, 19, 36 KB | The same two guides are kept in both repositories (ACCOUNTS lines 4-5; TROUBLESHOOTING lines 4-6); TROUBLESHOOTING is a Mermaid flowchart from what a person sees to rows R1..R15 of a table (Symptom in the exact words the app prints, Where to look, What to do), and tests pin that every message the launcher or the server prints leads to a row (`test_split_doc.py`); DEPLOY lists three deliveries (release folder, container, Vercel plus container), the server's environment table, the founder console, the first company, TLS (Caddy or nginx), the database, backups, staging, updates, monitoring, Vercel, the Supabase project, DNS, and the owner's checklist | operators and the owner |
| `docs/GLOSSARY.md` | 2.4 KB | "One name for one thing": Word, What it names, Where it is said; the states of each object | writers of pages and documents |
| `CHANGELOG.md` | 9 KB | Newest first; `## Unreleased` then `## 0.2.0 (20 October 2026)`; a release needs a section before `scripts/build_release.py` will build it (lines 5-7) | users |
| `docs/split/(stage).md`, `docs/program/packages/(P).md`, `docs/session_notes_(date)/` | many | The raw records | the record |

Writing style, consistent through every document and docstring: plain sentences, the reason after the rule, review items as `R-123`, master items as `PS-08` or `CE-04`, `[verify]` and `[recalled]` on anything written from memory, dates written "24 October 2026", "the owner" (never "the user") and "a person" (never "the user" for an end user), British spelling ("licence", "organisation"), no exclamation marks, no emoji. A docstring begins with what the module is and the decision it comes from ("the owner's decisions of 24 October 2026, section 4; design E; stage B6").

### 2.4 Test style

- pytest, function tests, one file per batch or stage, named after it (`test_identity.py` for stage B1, `test_founder.py` for B6, `test_program_p21.py` for package P21, `test_fix_s43_roles.py` for a fix session), a docstring first (`docs/DESIGN_LOG_GUIDE.md` section 5). Long descriptive names: `test_the_workspace_is_open_until_the_first_account_then_needs_a_sign_in`.
- xdist: `python -m pytest -q -n auto` in CI (`ci.yml` lines 55 and 83), `-n 6` on the owner's machine (README, "Test it"); `pytest-xdist==3.8.0` pinned in `requirements-dev.txt`. Every test that signs anyone in uses its own database (the `db` fixture of `test_auth.py`, lines 32-36: `monkeypatch.setattr(store, "DB_PATH", tmp_path / "auth.db")`) so workers never share people; a guard fixture fails a test that leaves users in the shared file (`conftest.py` 58-72).
- Golden tests: `backend/tests/golden.py` `diff(old, new)` compares numbers to 1e-8 relative (an absolute floor of 1e-8), reads `-0.00` and `+0.00` as one zero, allows new keys, demands every old key and every list length (lines 1-51); goldens live in `backend/tests/fixtures/` (`golden_packs.json`, `golden_packs_sharp.json.gz`, `api_replay_full.json.gz`, `api_replay_baseline.json` of hashes, and `schema14_workspace.db`, `schema19_workspace.db`: old-schema files that must still open and migrate). `TEST_MATRIX.md` section 2: a safety fix comes with a regression test that fails on the old code ("Prove it: save the patch, check out the old file, run the test, re-apply"); a money calculation with a golden test worked by hand in the docstring; a new route with a 422 test; anything a cost-less role can call with the role walk; a moved default only with the owner's approval and a surgical re-record.
- Sibling tests: `backend/tests/sibling.py` `web_root()` and `frontend_file(rel)` find the web repository beside (`SOLARLYTICS_FRONTEND_DIR`, else a sibling folder; the file on disk today, lines 23-31, names `solarlytics-web`; the memory note says commit `ddeadca` on branch `fix/web-repo-name` looks for `solarlytics-frontend` first, which is not on `main` here: UNVERIFIED), refuse a folder that also holds the engine, and fail (or skip with `SOLARLYTICS_SKIP_SIBLING=1`, never in CI) when missing; `test_sibling.py` pins that no backend test reads a web path directly (lines 63-66). The web repository has the mirror `frontend/tests/sibling.ts` (`docs/split/S0a.md` line 15).
- Pinning tests on the infrastructure: `test_deploy_docs.py` (the env example and the deploy guide), `test_fix_main.py` (the CI jobs and pins), `test_split_doc.py` (every launcher and server message leads to a troubleshooting row), `test_hygiene.py` (hosts, origins, JSON-only writes, CORS headers), `test_concurrency_w2.py` (8 threads, numbers unique, no 500), `test_version_w2.py` (the engine stamp). The owner's convention: a document that no test pins drifts.
- Walk cases: `tests/walk_cases/(pkg).py` give each new route a good body, a wrong-shape body, a non-object body and an unknown id (ARCHITECTURE I2); `walk_base.py` drives them; `test_role_walk.py` walks every route as every role and greps the answers for cost keys.
- Replay: `replay_set.py` replays recorded requests over `/api` and `/api/v1` against recorded answers; the engine stamp is a header so bodies stay byte-equal.
- Browser check (the web repository): `node scripts/e2e/step1.mjs`, one flow per batch, on a scratch database and ports 8141/5292, refusing the owner's ports and data; 1,428 checks at stage Z.
- Verification (`verification/`): side by side with an independent implementation (OpenSees, PVGIS, SAM), a report `REPORT.md` regenerated with `--update`, weekly in CI. For the EMS the analogue is a replay against python-microgrid (LGPL, unmodified, tests only) and a REopt reference case (Product 1 report lines 361-363 and the WP6 row at 568).

### 2.5 The launcher and ports convention

From `docs/split/S0b.md` lines 33-62 (the launcher is the web repository's `scripts/start_local.py`, not on this machine; the record is the backend's):

- `python scripts/start_local.py [--scratch [folder]]` in the web repository starts the API (`uvicorn solarlytics.api:app --host 127.0.0.1 --port (API_PORT) --no-proxy-headers` in `(backend)/backend` with `SOLARLYTICS_PYTHON`, else its own Python) and the web app (`npm run dev`), waits on `/api/health` and on the page, and prints `http://127.0.0.1:(WEB_PORT)`.
- Ports: the owner's are 8041 (API) and 5192 (web); `--scratch` defaults to 8141 and 5292 and refuses the owner's ports and data folders; the release folder runs on 8060; agents used 8241/5392 (a preview) and 8341-8372 (stage runs). Boxlytics uses 8000/5173, Retailytics 8001/5173 (`RUN_LOCALLY.md` 122-124). The EMS must pick a pair nobody uses (a proposal: 8051 for the API and 5193 for the web app; scratch 8151 and 5293).
- The backend beside: `SOLARLYTICS_BACKEND_DIR`, else `../solarlytics-backend`; a folder holding both `frontend/package.json` and the engine is refused.
- No console windows: every child gets `creationflags=CREATE_NO_WINDOW` on Windows through one helper, stdin from DEVNULL, stdout and stderr piped and copied line by line marked `[npm]`, `[api]`, `[web]` (S0b 38-41). The memory rule `no-console-windows.md`: long-lived servers under `pythonw.exe` with a runner that points stdout and stderr at a log file; Node's `spawn` with `detached: true` ignores `windowsHide`; check for stray `WindowsTerminal.exe` or `OpenConsole.exe` processes after starting.
- One server per data folder: a lock file in the temp folder named by a hash of the data folder's path (`msvcrt.locking` or `fcntl.flock`); a second launcher is refused with "Another Solarlytics launcher is running on the data folder" (TROUBLESHOOTING R11).
- `.env.founder` in the data folder is read by the launcher (`NAME='value'` lines, only `SOLARLYTICS_FOUNDER_*` names, a variable already set wins).
- `--scratch` sets `SOLARLYTICS_DB=(folder)/solarlytics.db` and `SOLARLYTICS_DATA_DIR=(folder)` (default `.scratch/local`, git-ignored).
- `SOLARLYTICS_DAILY_BACKUP=1` set in the children's environment; `VITE_AUTH_MODE` defaults to the server's mode.
- `.claude/launch.json` (the web repository) has one configuration, `solarlytics-scratch`, which runs the launcher with `--scratch` on 5292 with the pinned venv's Python and never names the owner's folder or ports (S0b 60-62; the memory note says `fix/frontend-folder` points it at the backend's venv on 8141/5292).
- The launcher's own tests: `scripts/tests/test_start_local.py` in the web repository (15 tests), run by that repository's CI (S0b 27).

### 2.6 Environment variable naming

One upper-case prefix per product, `SOLARLYTICS_`, on every server setting; uvicorn's own `FORWARDED_ALLOW_IPS` is the one exception; the web app's build settings are `VITE_` (`VITE_AUTH_MODE`, `VITE_API_BASE_URL`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`). Boxlytics uses `BOXLYTICS_` through pydantic-settings' `env_prefix` (section 3). Secrets are never written in the repository: `.env.production.example` writes `NAME=` with an empty value for every secret and a commented `# NAME=default` for every default (`test_deploy_docs.py` 60-80 holds it: no quotes, no spaces around `=`, no `*` in the CORS or proxy lists, `AUTH_MODE` in `{local, supabase}`). `.gitignore` ignores `.env` and `.env.*` but keeps `.env*.example`. The `$` signs of a PBKDF2 hash are the documented trap (ACCOUNTS 65-66; DEPLOY 42-46).

### 2.7 CI jobs (`.github/workflows/ci.yml`, 118 lines)

- `backend-linux`: on every push and pull request; `ubuntu-latest`, Python 3.13, `pip install -r requirements-dev.txt`, `python -m pytest -q -p no:cacheprovider -n auto` in `backend/`, with the web repository checked out beside as `solarlytics-web` using the read-only secret `FRONTEND_READ_TOKEN`, `SOLARLYTICS_FRONTEND_DIR` set, `SOLARLYTICS_LOGS=0`, `PYTHONUTF8=1`, `SOLARLYTICS_REQUIRE_SCANNER=1` (lines 32-57).
- `backend-windows`: only on a pull request into `main`, by hand (`workflow_dispatch`) and weekly (`cron: "17 2 * * 1"`); `windows-latest`, the same steps (59-85). The owner's platform is Windows, so it runs there before `main` moves.
- `audit`: `pip-audit -r requirements.txt` on every push and pull request, installed in its own job, never in the requirements files (87-100).
- `verification`: by hand and weekly, `verification/requirements.txt`, `SOLARLYTICS_DB` under `runner.temp` (102-118).
- `concurrency`: a new push cancels the run it replaces; weekly runs are never cancelled (20-24).
- Requirements are exact pins at the versions the full checks passed with, dated in the file's comments (`requirements.txt` lines 1-4); transitive dependencies are left to pip. Today's pins: `fastapi==0.150.0 starlette==1.7.0 pydantic==2.13.5 uvicorn==0.54.0 numpy==2.5.3 threadpoolctl==3.7.0 PyJWT==2.15.1 cryptography==50.0.2`; dev adds `pytest==9.1.1 pytest-xdist==3.8.0 httpx2==2.13.1 mpmath==1.4.1 zxing-cpp==3.1.1 pillow==13.0.0`. The `ops` env has the older fastapi 0.141.1, starlette 1.6.0, uvicorn 0.52.3, httpx 0.28.1: the Solarlytics pins were not installed there.
- Releases: `scripts/build_release.py` makes `release/solarlytics-(version)/` and a zip for one Windows machine (the API serving the built web app on 8060), refuses a version with no `CHANGELOG.md` section, prints the git tag to make (tagging is the owner's). The `Dockerfile` (`python:3.12.7-slim-bookworm`, one uvicorn worker, user 10001, a `/data` volume, HEALTHCHECK on `/api/health`, `--proxy-headers`) builds the API only; the web app is on Vercel.

---

## 3. boxlytics-backend patterns

The repository is two things in one (`README.md` lines 17-46): the deployed prediction API in `backend/` (FastAPI, one trained surrogate model) and the OpenSeesPy data pipeline at the root (`run_model.py`, `generate_dataset.py`, `dashboard.py`, `orinal_code/`). It is read-only for OnyxLeaf (memory `boxlytics-repos-hands-off.md`); the owner allowed reading it for patterns. Everything below is a pattern description, not code to copy.

### 3.1 Settings: pydantic-settings with a prefix

`backend/core/config.py` (85 lines): `class Settings(BaseSettings)` with `model_config = SettingsConfigDict(env_file=(backend)/.env, env_prefix="BOXLYTICS_", extra="ignore")` (lines 24-28); fields `api_title`, `api_version`, `api_prefix = "/api/v1"`, `api_key = "boxlytics-dev-key-2024"` (a default dev key, line 36), the model paths resolved relative to the file (`MODELS_DIR = Path(__file__).resolve().parents[1] / "deploy" / "models"`, line 15, with a comment at 43-49 explaining why absolute container paths broke), `cors_origins: list[str]` with a `field_validator(mode="before")` that accepts a JSON array or a comma list (70-79), `database_url = "sqlite:///./boxlytics_jobs.db"`, `results_dir`; `@lru_cache get_settings()` (82-85). Known trap (`RUN_LOCALLY.md` 117-120): pydantic-settings JSON-decodes a `list[str]` field from the environment before the validator runs, so a comma-separated `BOXLYTICS_CORS_ORIGINS` fails with `SettingsError`; only a JSON array works from the environment. (Solarlytics avoided pydantic-settings and parses the list itself, `api.py` 113-133, with a test, `test_hygiene.py` 47-54.) A stale copy `backend/config.py` (54 lines) with one developer's absolute paths is still in the tree.

### 3.2 API key

`backend/core/security.py` (31 lines): `APIKeyHeader(name="X-API-Key", auto_error=False)`; `verify_api_key` is a FastAPI dependency: 401 "Missing API Key. Include 'X-API-Key' header." when absent, 403 "Invalid API Key" when it differs from `settings.api_key` (a plain `!=`, not a constant-time compare, line 25); every route declares `api_key: str = Depends(verify_api_key)` (`routes/jobs.py` 16, `simulate.py` 21 and 45, `results.py` 14 and 29, `parameters.py` 15). One shared key for every tenant; the frontend bakes it into the Vite bundle (`RUN_LOCALLY.md` 56-64 warns that anyone who loads the page can read it). `docs/TENANCY_BOUNDARY.md` (56 lines) is honest about it: "Jobs have no owner. Anyone holding the shared API key can `GET /jobs/{id}` for any job" (28-29); tenancy lives in Supabase's row-level security, the frontend writes results there tagged with the company; the smallest fix would carry `company_id` through and return 404 on a mismatch (42-52); "verifying the Supabase JWT here instead of a shared key ... is the better answer if this service is ever exposed beyond the frontend" (54-56). Solarlytics' per-request token plus per-company scoped keys (`apikeys.py`) is that better answer, built.

### 3.3 Async jobs and polling

`backend/services/job_manager.py` (140 lines): a module-level `ThreadPoolExecutor(max_workers=4)` made on first use (16-24) and shut down in the app's lifespan (`main.py` 41-42); `submit_box_analysis` makes a `uuid4` id, writes a `pending` row through `db/database.py`, submits `_run_box_analysis` (35-65); the worker sets `running` with progress 10 and 50, calls the model, writes the result dict (`summary`, `execution_time`, `model_version`) or the error (68-135). `backend/db/database.py` (168 lines): one SQLite file beside the package (`DATABASE_PATH = Path(__file__).parent.parent / "boxlytics_jobs.db"`, 13), `jobs(id, job_type, status, progress, parameters, result, error_msg, created_at, started_at, completed_at)` (19-32), `check_same_thread=False` connections opened per call, no WAL, no migrations. Routes: `POST /api/v1/simulate/box` -> `{job_id, status: pending, message}`; `GET /api/v1/jobs/{job_id}` -> `JobStatusResponse` with `result` and a `results: [result]` list "because the frontend looks for a `results` array" (`routes/jobs.py` 41-44); `POST /api/v1/simulate/predict` is the synchronous path (`simulate.py` 43-70); `GET /api/v1/parameters` gives bounds for dropdowns; `/results/{job_id}/files` is a stub (`results.py`). The lifespan pre-loads the model so a missing artefact fails at start-up "while somebody is still watching the console, instead of silently downgrading every answer once the service is live" (`main.py` 27-34).

Compared with Solarlytics' `jobs.py` (266 lines, section 1.2): the Solarlytics one scopes a job to (company file, org, owner) so another company gets 404, limits waiting jobs per workspace (4) and per user (2), caches a finished search by the hash of its request and basis, stops a job nobody reads for 180 s, carries the request's ContextVars into the pool thread with `contextvars.copy_context().run` (line 212), and keeps jobs in memory (a restart drops them; the page starts again). For the EMS, lift Solarlytics' `jobs.py` for the interactive solves (a what-if, a backtest) and keep Boxlytics' persistence idea (a `jobs` table) only where a run must survive a restart; the 15-minute scheduler's solves are idempotent re-runs, not persisted jobs.

### 3.4 Deploy: nginx, systemd, compose

- `backend/deploy/docker-compose.prod.yml` (22 lines): one service, `build: context: ..`, `restart: unless-stopped`, `env_file: .env`, `ports: 127.0.0.1:8000:8000` (loopback only, TLS in front), volumes `./models:/app/models:ro`, `./data:/app/backend/data`, `./results:/app/backend/simulation_results`, a `healthcheck` that opens `/api/v1/health` every 30 s with 5 retries and a 20 s start period.
- `backend/deploy/nginx-boxlytics-backend.conf` (17 lines): `listen 80; server_name api.your-domain.com; client_max_body_size 20m; location / { proxy_pass http://127.0.0.1:8000; proxy_http_version 1.1; Host, X-Real-IP, X-Forwarded-For, X-Forwarded-Proto; proxy_read_timeout 120s; proxy_connect_timeout 30s; }`; TLS by certbot (`DEPLOYMENT.md` 57-61). The deployed instance is `https://api.onyxleaf.co` (README 12-13).
- `backend/deploy/boxlytics-backend.service` (16 lines): `Type=simple`, `User=ubuntu`, `WorkingDirectory=/opt/boxalytics`, `EnvironmentFile=/opt/boxalytics/backend/deploy/.env`, `ExecStart=/opt/boxalytics/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000`, `Restart=always`, `RestartSec=5`.
- `backend/Dockerfile` (24 lines): `python:3.11-slim`, CPU-only torch from the PyTorch index first, `COPY . /app/backend`, `uvicorn backend.main:app --host 0.0.0.0 --port 8000`. The root `Dockerfile` and `docker-compose.yml` run the data pipeline and the Streamlit dashboard on 8501 with a 7.5 GB memory limit (`entrypoint.sh` starts `generate_dataset.py --indefinite` in the background and `streamlit run dashboard.py` in the foreground).
- `backend/DEPLOYMENT.md` (91 lines): a numbered migration guide (prerequisites, files to set, launch with compose, nginx, TLS, a frontend checklist, operations commands, the systemd option).
- Solarlytics' `docs/DEPLOY.md` is the later, fuller version of the same shape (Caddy or nginx, `FORWARDED_ALLOW_IPS`, one process per data folder, a volume, staging, updates, monitoring, the owner's checklist). For the EMS take Solarlytics' DEPLOY and Dockerfile; add Boxlytics' compose file and nginx snippet, with `proxy_read_timeout` long enough for a 30-second MILP only if a solve is ever synchronous (it should not be).

### 3.5 The Streamlit dashboard

`dashboard.py` (799 lines) is a monitor for the dataset generation, not an admin console: `st.set_page_config(page_title="Structural Simulation Dataset Monitor", layout="wide")` (53-58); a cached Supabase client from `SUPABASE_URL` and `SUPABASE_KEY` (the service-role key, used read-only, 68-84) or a local results folder (`--local`); the sidebar chooses the source, a refresh interval and the number of records (652-669); five tabs (713) of metrics, input and output distributions, correlation plots, a time series and a failure analysis, plotly with a matplotlib fallback. Streamlit is in the root `requirements.txt` (`streamlit>=1.20.0`), not in `backend/requirements.txt`. Nothing of it touches the API. The owner's request mentions "a similar admin dashboard also in solarlytics-backend": what Solarlytics has instead is the founder console (`/api/founder/*` with the web app's `/admin` pages), which is the admin dashboard the EMS should inherit; a Streamlit page could be added later as an internal fleet monitor reading the per-company files read-only, but it would be a fourth process with its own port and is not in the first build.

### 3.6 Other Boxlytics habits worth keeping

- `RUN_LOCALLY.md`: two repositories, two terminals, a table of ports, the exact `curl` that proves the right model answered, a "when something looks wrong" list in the error's own words. Solarlytics' TROUBLESHOOTING is the grown-up version.
- `SYNC_WORKFLOW.md`: how a UI change travels between the three UI folders and what must not travel; the gates before pushing (`npx tsc --noEmit -p tsconfig.app.json`, not bare `tsc`, because the solution-style `tsconfig.json` compiles nothing; `vitest run`, `vite build`, `eslint .`). The EMS frontend should run `tsc -p tsconfig.app.json` from day one if it adopts the same solution-style config (UNVERIFIED for the Solarlytics frontend, whose checks run in its own CI).
- Trained weights are committed (a 67 MB pickle) and the README says to move to Git LFS if they change often (138-141). The EMS will have per-site forecast models: keep them out of git (in the data folder, per company), never committed.

---

## 4. A recommended backend skeleton for the EMS

The product has no name yet (the owner's products are `boxlytics`, `solarlytics`, `panelytics`, `retailytics`, `packlytics`; the EMS might follow the pattern). Below, `(pkg)` is the Python package name and `(PREFIX)_` the environment prefix; both are one search-and-replace once the owner names it. Everything else follows Solarlytics' shape exactly, because the owner approved that shape twice (memory `solarlytics-app.md`, "How to apply") and because the sign-in module, its tests and the web app's sign-in pages are written against it.

### 4.1 The two repositories

```
(pkg)-backend/                      private; the optimiser, forecasters, tariff engine, verification and the API (engine IP stays here)
  backend/(pkg)/                    the package (section 4.2)
  backend/tests/                    pytest; conftest.py, identities.py, golden.py, sibling.py, walk_cases/, fixtures/
  verification/                     replay against python-microgrid (LGPL, unmodified) and a REopt reference case; never imported by the product
  bench/                            backtests and the digital counterfactual on recorded sites (reports regenerated with --update)
  docs/                             the documents set (section 2.3 and section 5)
  scripts/                          build_release.py, restore_backup.py, load_check.py, fetch_tariffs.py
  demo/                             companies.template.csv, users.template.csv, sites.template.csv (the seed's lists; the password list is git-ignored)
  supabase/                         SQL for review only, when supabase mode is wanted (copy Solarlytics' solar_000/001 files and rename)
  data/                             git-ignored: (pkg).db, platform.db, platform.secret, companies/, backup/, logs/, .env.founder
  .github/workflows/ci.yml          backend-linux, backend-windows, audit, verification (section 2.7)
  Dockerfile, .dockerignore, .env.production.example, requirements.txt, requirements-dev.txt, CHANGELOG.md, CLAUDE.md, README.md, .gitattributes, .gitignore
(pkg)-frontend/                     the web app only (React, TypeScript, Vite), checked out beside as (pkg)-web or (pkg)-frontend
  frontend/                         src/, tests/ (Node's runner on the pure modules), package-lock.json
  scripts/start_local.py            the launcher (section 2.5), scripts/tests/test_start_local.py, scripts/e2e/ (the browser check)
  docs/ACCOUNTS.md, docs/TROUBLESHOOTING.md   the same two files as the backend's
  .claude/launch.json               one configuration, --scratch, never the owner's ports or data
```

### 4.2 The package layout (`backend/(pkg)/`)

| Module | Lines (estimate) | What it holds | From where |
|---|---|---|---|
| `__init__.py` | 3 | `__version__ = "0.1.0"` | Solarlytics `__init__.py` |
| `api.py` | about 450 | The FastAPI app with `dependencies=[Depends(_ints_the_database_holds), Depends(_body_not_too_deep)]`; the rotating log; `ALLOWED_HOSTS` and `identity.check_startup`; the CORS lists with the company header renamed; `ERROR_CODES`, `SIGN_IN_CODES`, `SECURITY_HEADERS`, `MAX_BODY`; `_error_body`, `_refusal`, `_too_many`, `_fail`; `PUBLIC_PATHS`, `LOCAL_POSTS`, `IDENTITY_PATHS`, `FOUNDER_PREFIX`; `guard`, `_enter/_leave`, `_guarded`; `_VersionedPath`, `_BodyLimit`, the middleware order; the exception handlers; `GET /api/health`, `GET /api/auth/state`, `GET /api/users`, `PUT /api/users/{id}`; the mounting loop over `ROUTES` with `app.add_api_route`; the optional SPA catch-all with `(PREFIX)_WEB_DIST`. Nothing else: every feature is a `routes_*.py` | `api.py` lines 1-560, 705-720, 1183-1185, 4897-4942, 6786-6835, cut down |
| `schemas.py` | 60 + models | `Refusal(HTTPException)` with `code` and `extra`; `_label`, `_message`, `check(model, body)` that turns pydantic errors into one plain sentence; then the EMS request models (`SiteCreate`, `TariffSet`, `TelemetryBatch`, `PlanRequest`, `OverrideRequest`) as `Strict` models | `schemas.py` 27-98 and 313-330 |
| `store.py` | 130 + the EMS schema | The ContextVars (`_ORG`, `_DB`, `_ACTOR`), `DB_PATH` from `(PREFIX)_DB`, `set_company/use_company`, `set_org/use_org`, `set_actor`, `_now`, `_stamp`, `backup(tag)`, `_connect` (WAL, copy before a migration, `BEGIN IMMEDIATE`), `write_transaction`, `audit`, `list_audit`; `_migrate` with the EMS chain from version 1: `members`, `role_presets`, `api_keys`, `audit` (the four tables the lifted package needs, with the v17 and v20 DDL), then `sites`, `tariffs`, `tariff_versions`, `devices`, `telemetry_days` (daily aggregates), `plans`, `plan_steps`, `events`, `overrides`, `forecast_runs`, `savings_days`, `settings`, `sequences` | `store.py` 1-131, 475-526, 956-990, 1402-1420; DDL at 420-423 and 445-457 |
| `telemetry.py` | new | The 1-minute series store: one file per company beside the workspace (`companies/(id)-telemetry.db` or a Parquet folder; the decision in section 6), keyed `(site_id, ts)`, with `ingest(rows)` idempotent on `(site_id, ts, channel)`, gap flags, the grid-present flag, and `read(site, since, until, step)` | new (Product 1 report, data model lines 188; WP3) |
| `tenancy/` | about 4,300 | The lifted package: `auth.py` (EMS roles), `identity.py`, `platform.py`, `companies.py`, `store_members.py`, `routes_local.py`, `founder.py`, `routes_founder.py`, `store_founder.py` (EMS counts), `mfa.py`, `apikeys.py` (EMS scopes), `backups.py`, `seed_demo.py`, `ratelimit.py` (the `Limiter` copied out of `routes_accept.py`), `errors.py` (optional: `Refusal` and `fail()` so the package never imports `api`) | section 1.7 |
| `version.py` | 42 | The engine stamp `(version)+(fingerprint)`, header `X-(Product)-Engine` | verbatim |
| `jobs.py` | 266 | The thread-pool jobs scoped per company and owner, for what-ifs, backtests and reports | verbatim, `optimizer.Cancelled` renamed |
| `scheduler.py` | new, about 150 | The 15-minute loop: for every enabled company and every site with a plan due, pull state, forecasts and the tariff vectors, solve, store and publish the plan; one thread like `backups.start_daily`, on only with `(PREFIX)_SCHEDULER=1` (the launcher and the image set it; the tests never), skipping a site whose last solve is still running, and never raising out of the loop. No APScheduler (not in the env; not needed: `threading.Event.wait`) | the pattern of `backups.py` 143-161 |
| `tariffs/` | new | `catalog.py` (the DISCO TOU categories, seasons, windows, ICP flat incremental price, MDI fixed charges, net billing vs legacy net metering, versioned with `valid_from` and the SRO source), `bill.py` (the 1-minute settlement engine and the monthly bill rebuild, golden-tested against pilot bills, WP1's 2 percent), `vectors.py` (price and limit vectors per 15-minute step) | new; REopt `electric_tariff.jl` pattern |
| `forecast/` | new | `pv.py` (pvlib clear-sky times a learned attenuation, then a GBDT residual with quantiles), `load.py` (per-site day-type model), `outage.py` (per-feeder hazard by hour and season from the fleet's grid-present flags), `weather.py` (Open-Meteo or Solcast client behind `(PREFIX)_OFFLINE`), `skill.py` (MAE and skill against persistence, WP5) | new; pvlib is not in the env (section 6) |
| `battery.py` | new | SoC with confidence, usable kWh, SoH, BMS trust score, the wear-cost curve (PWL, BLAST-Lite derived) | new (component 3 of the architecture) |
| `optimise/` | new | `model.py` (the MILP: battery charge and discharge, grid import and export, genset on/off with fuel slope and intercept, min up and down times, reserve SoC before a predicted outage, MDI and ICP terms, wear cost; `scipy.optimize.milp` wrapping HiGHS, with `highspy` as the later option), `mpc.py` (the 48-hour receding horizon, warm starts, the re-solve on events), `plan.py` (the signed plan: 192 steps, versioned, validity, fallback rules), `modes.py` (Mode A: a day's inverter TOU slots with SoC targets, from the plan; Mode B: 15-minute setpoints for Product 2) | new; EMHASS and evcc-io/optimizer structure (both MIT), REopt patterns ported (Apache-2.0) |
| `verify.py` | new | The counterfactual replay of the inverter's own mode at 1-minute resolution, the switchback A/B bookkeeping, the bill rebuild and the daily and monthly rupee and diesel figures (WP4, WP9) | new |
| `routes_sites.py`, `routes_tariffs.py`, `routes_telemetry.py`, `routes_plans.py`, `routes_reports.py`, `routes_platform.py` (org and keys only) | new | One module per feature, each `router = APIRouter()`, each route `auth.need(permission)` first, each with a walk case; telemetry and plan read-back through an API key with the scopes `telemetry:write` and `plans:read` (Product 2 and OEM-cloud connectors) | the pattern of `routes_owner.py` 1-40 |
| `data.py`, `rules.py` | new | The shipped defaults (tariff tables with their source and date, equipment curves), the settings bounds | the pattern of Solarlytics' `data.py` and `rules.py` (not read in detail here) |

What is deliberately not there: an ORM (Solarlytics uses `sqlite3` with `Row` factories and hand-written DDL; SQLAlchemy and alembic are not in the env), a message queue, a second service. The optimiser runs in the scheduler thread and in the job pool; forecasts are trained in a job.

### 4.3 Settings

Keep Solarlytics' way: module constants read from `os.environ` at import for what never changes in a process (`identity.MODE`, the rate limits) and functions read at each call for what a test or a scratch run moves (the data folder, the founder's four). Put every name in `.env.production.example` with the `test_deploy_docs.py` scan kept as is (it greps the package for `(PREFIX)_` literals, so nothing can be read that the example does not name). Names, with Solarlytics' meaning unchanged unless said:

`(PREFIX)_AUTH_MODE`, `_LOCAL_JWT_SECRET`, `_SUPABASE_URL`, `_SUPABASE_ANON_KEY`, `_SUPABASE_SERVICE_ROLE_KEY`, `_SUPABASE_JWT_SECRET`, `_JWT_AUDIENCE`, `_JWKS_TTL`, `_MEMBERSHIP_TTL`, `_FOUNDER_USERNAME`, `_FOUNDER_PASSWORD_HASH`, `_FOUNDER_TOTP_SECRET`, `_FOUNDER_SESSION_SECRET`, `_HOSTS`, `_CORS_ORIGINS`, `_CORS_ORIGIN_REGEX`, `_TRUST_PROXY`, `_PORT`, `_DB`, `_DATA_DIR`, `_DAILY_BACKUP`, `_LOGS`, `_WEB_DIST`, `_RATE_USER`, `_RATE_ADDRESS`, `_RATE_PUBLIC`, `_ERROR_WEBHOOK`, `_DOCS`, plus the EMS's own: `_SCHEDULER` (1 runs the 15-minute loop), `_OFFLINE` (no weather or tariff fetch), `_WEATHER_PROVIDER` and `_WEATHER_KEY` (Open-Meteo free tier for pilots, Solcast later), `_SOLVER` (`scipy` or `highspy`), `_SOLVE_SECONDS` (the MILP time limit, 30 by default per WP6), `_TELEMETRY_DIR` (where the per-company series files live when not beside the workspace). `FORWARDED_ALLOW_IPS` stays uvicorn's. The launcher's `(PREFIX)_API_PORT`, `_WEB_PORT`, `_PYTHON`, `_BACKEND_DIR`, the tests' `_FRONTEND_DIR`, `_SKIP_SIBLING`.

If the owner prefers Boxlytics' `pydantic-settings` object (`pydantic-settings 2.15.0` is in the `ops` env), use it only for the EMS's own numeric settings and keep the lifted package's `os.environ` reads untouched; do not pass a `list[str]` through it (the JSON-array trap, section 3.1).

### 4.4 How the lifted sign-in plugs in

1. Copy the fifteen files into `backend/(pkg)/tenancy/` with the edits of section 1.7, and `routes_accept.Limiter` as `tenancy/ratelimit.py`. Replace every `SOLARLYTICS_` with `(PREFIX)_`, `solarlytics-local` with `(pkg)-local`, `slk_` with the new key prefix, `X-Solarlytics-Company` with `X-(Product)-Company`, the logger name, `mfa.ISSUER`.
2. `store.py` of the host provides the context functions and the four tables in its first migration (the host's `_migrate(con, version)` runs `tenancy`'s DDL at `version < 1`).
3. `api.py` of the host is the cut-down guard; it imports `tenancy.auth`, `identity`, `companies`, `apikeys`, `platformdb`, `store_members`, `backups` and at its end mounts `tenancy.routes_local` (local mode only), `tenancy.routes_founder`, `routes_platform` and the EMS route modules through the same loop. `GET /api/users` and `PUT /api/users/{id}` stay in `api.py` because the seed calls `api.users_edit`.
4. `backups.start_daily()` and the EMS `scheduler.start()` are called from a start-up hook: Solarlytics does it in `@app.on_event("startup") def _daily_backups()` (`api.py` 607-609); a `lifespan` context (what Boxlytics does, `main.py` 17-42) is the current FastAPI form and also gives a clean shutdown for the scheduler thread and the job pool.
5. `seed_demo.py` makes the demo companies, people and the founder's four settings into `.env.founder`; add a `sites.template.csv` so the demo has a site with a tariff and a week of replayed telemetry.
6. The frontend repository lifts the web app's `/login`, `/admin`, `session.ts`, `companyswitch.tsx` and `NotSetUp.tsx` against the identical routes and refusal codes (UNVERIFIED: the web repository is not on this machine; `docs/split/DOC.md` line 85 and `ACCOUNTS.md` 4-7 name the files).
7. The tests: copy `conftest.py` (lines 1-28 are the environment, 58-102 the fixtures), `identities.py`, `test_identity.py`, `test_platform.py`, `test_companies.py` (less the legacy linking), `test_founder.py` (less `read_file`'s Solarlytics counts), `test_signin_guard.py` (less share links), `test_auth.py`'s first five tests, `test_seed_demo.py`, `test_company_files.py`, `test_deploy_docs.py`, `test_hygiene.py`'s first four. Re-aim `apikeys` scope tests at the EMS resources.
8. Day one check: `python -m pytest -q -n auto` green in `backend/`; `python -m (pkg).seed_demo` on a scratch folder; the launcher on scratch ports; sign in at `/login` with a demo account; `/admin` with the founder's code; `GET /api/auth/state` names the company and the role.

### 4.5 Dependencies and the Python

`requirements.txt` (exact pins, dated): `fastapi`, `starlette`, `pydantic`, `uvicorn`, `numpy`, `scipy` (for `scipy.optimize.milp`, which wraps HiGHS; scipy 1.17.1 is in the `ops` env), `pandas` (the series), `PyJWT`, `cryptography` (the sign-in), `threadpoolctl`. To decide with the forecasting research (not this task): `pvlib` (BSD-3, not in the env), `scikit-learn` or `xgboost` (both in the env; `lightgbm` and `skforecast` are not), `highspy` (MIT, not in the env; `scipy.optimize.milp` is enough for 192 steps with a dozen binaries per step, UNVERIFIED against WP6's 30-second case: measure in `bench/`). Dev: `pytest`, `pytest-xdist`, `httpx` (the TestClient), `mpmath` if any exact check needs it. Verification only: `python-microgrid` (LGPL-3.0, unmodified), `NREL-PySAM` (BSD-3), `CityLearn` (MIT). Never: Predbat, OpenLEMS, emoncms, the OpenEMS UI, energy-py-linear (Product 1 report, licence rules lines 365-374).

The backend needs its own venv (`.scratch/venv`, git-ignored, as `solarlytics-backend` has): the `ops` env lacks PyJWT and cryptography, and installing into it is the owner's call (the task says install nothing). `CLAUDE.md` names the Python and `(PREFIX)_PYTHON` points the launcher at the venv.

### 4.6 What the routes look like (the first product pages, before the admin dashboard)

The owner asked to "focus on core product pages and tabs" before the admin dashboard. The founder console comes free with the lift, so it is not extra work; the product routes, in build order:

1. `GET/POST /api/sites`, `GET/PUT /api/sites/{id}` (static: location, PV kWp, battery kWh and kW, chemistry, genset kVA and fuel curve, DISCO, tariff category, feeder, critical load, inverter make and its TOU slot capability).
2. `GET /api/tariffs`, `GET /api/tariffs/{id}/versions`, `POST /api/sites/{id}/tariff` (choose a category and version; the bill engine's `POST /api/sites/{id}/bill/rebuild` for WP1's acceptance).
3. `POST /api/sites/{id}/telemetry` (a batch, API key or session; 1-minute rows; idempotent), `GET /api/sites/{id}/telemetry?from&to&step`, `POST /api/sites/{id}/telemetry/import` (a CSV for pilots).
4. `GET /api/sites/{id}/forecast` (PV, load and outage quantiles for 48 hours; `POST .../forecast/train` as a job).
5. `POST /api/sites/{id}/plan` (a job; returns the plan id), `GET /api/sites/{id}/plans/{id}` (192 steps, the objective, the binding constraints, the inverter slots for Mode A), `GET /api/sites/{id}/plans/current`, `POST /api/sites/{id}/overrides` (a manual reserve or a genset hold, audited).
6. `GET /api/sites/{id}/savings?month` (the counterfactual, the A/B weeks and the bill-level figure), `GET /api/companies/me/report?month` (the PDF's data; the PDF is drawn in the web app as Solarlytics does).
7. `GET /api/health`, `/api/auth/state`, `/api/local/*`, `/api/founder/*`, `/api/platform/org`, `/api/platform/keys` from the lift.

Every route: `auth.need(...)` first, a `Strict` model or a `_object(inp, allowed, what)` check, 422 in a sentence, a walk case, no cost figure to a role that may not see one (the EMS's equivalent: a viewer never sees the fuel price or the fee).

---

## 5. Templates in the owner's style

Each template keeps the headings of the Solarlytics original and gives one example row in the EMS's words. Replace `(Product)` and the dates.

### 5.1 `docs/BUILD_HANDOFF.md`

```
# (Product) build handoff

Written 6 November 2026 for the session that builds (Product) from the plan into a pilot-ready app. Read this file first, every session. It tells you what to build, from which documents, in what order, under which rules, and how to leave the work so the next session can resume.

## 1. The job

Build the cloud optimiser of Product 1: the tariff engine, the forecasters, the MILP economic MPC on 15-minute steps over 48 hours, re-solved every 15 minutes, the savings verification against the inverter's own mode at 1-minute resolution, and plan delivery as daily inverter TOU slots (Mode A) first. The owner wants, in their words: (1) ...; (2) ...

## 2. Documents, in order of authority

| # | File | Role |
|---|---|---|
| 1 | `docs/PLAN.md` (the end-to-end plan the owner agreed on (date)) | Master document. Scope, item ids, priorities, build order. Wins every conflict |
| 2 | `docs/BUILD_PROGRESS.md` | Progress checklist, generated from the master. You keep it current |
| 3 | `docs/BUILD_HANDOFF.md` | This file |
| 4 | `plan-work/research/*.md` | The research notes behind the plan (open-source review, licences, Product 2 interface, this backend audit). Read the one an item cites |
| 5 | `docs/PRODUCT_BRIEF.md`, `docs/EQUATIONS.md`, `docs/ASSUMPTIONS.md`, `docs/COMPETITORS.md`, `docs/DESIGN_LOG.md` | Product documentation. Keep them true as you change the code |

## 3. Paths and environment

| What | Where or how |
|---|---|
| Backend repository | `C:/Users/SaadTahir/Videos/(pkg)-backend` (GitHub saad-tahir/(pkg)-backend) |
| Web repository | `C:/Users/SaadTahir/Videos/(pkg)-frontend`, beside it |
| Python | the backend's own venv `.scratch/venv` made from `requirements-dev.txt`; `python` on PATH is the Store stub and the `ops` env lacks PyJWT |
| Start the app | `python scripts/start_local.py --scratch` in the web repository (API 8151, web 5293); the owner's ports are 8051 and 5193 |
| Backend tests | `cd backend` then `python -m pytest -q -n auto` |
| Workspace data | `data/` (git-ignored): `(pkg).db`, `platform.db`, `companies/`, `backup/`, `.env.founder` |

## 4. Standing rules (do not break these)

1. Clean-room for everything but what the licence table in DESIGN_LOG allows (MIT, BSD, Apache-2.0 with notices kept; patterns only from EPL, LGPL, GPL, AGPL and Predbat).
2. Hands off other repositories: `boxlytics`, `boxlytics-backend`, `solarlytics-backend`, `solarlytics-frontend`, `panelytics`, `retailytics`, `packlytics` are read-only.
3. The lifted sign-in module (from solarlytics-backend at `c853130`) is OnyxLeaf's own; its founder token format is a port of Boxlytics' `founder-auth.ts` at `c429bef` with the owner's permission. Both are recorded in DESIGN_LOG; do not re-derive them.
4. Copyleft and benchmark tools stay in `verification/` and `bench/`: the product never imports python-microgrid, CityLearn or PySAM.
5. Protect the owner's data: never delete or reseed `data/`; a copy before any schema change; tests on a temp data folder.
6. Git: work on a branch, never merge, push only when the owner asks; the owner opens the pull requests; never add a remote.
7. Honesty about numbers: every tariff value carries its SRO, date and confidence; every forecast skill and saving is reported against a stated baseline at 1-minute settlement; `[verify]` on anything recalled.
8. Never claim more than the code does: no "guaranteed savings", no "optimal" on a page.
9. Fixes come with tests: a safety rule (reserve SoC, genset min-run, export cap) gets a test that fails on the old code; a rupee figure gets a golden test worked by hand.
10. Do not delete working features.

## 5. Build order

Step 0. Safety net: the two repositories made, the lift of the sign-in module green, a baseline commit on `main`, branch `v1-build`.
Step 1. WP1 tariff and regime model (golden: three pilot bills within 2 percent). Step 2. WP3 data pipeline and the telemetry store. Step 3. WP4 the 1-minute bill engine and the counterfactual replay. Step 4. WP5 forecasters v1. Step 5. WP6 the optimiser. Step 6. WP7 the backtest and go/no-go report. Step 7. WP8 Mode A executor and WP9 reporting. Then the pilot (WP10).

Before starting each step, write a short plan (items, files, data model changes, tests) at the top of that step's entry in the session log, then build.

## 6. Decisions already made

| # | Question | Decision for this build |
|---|---|---|
| 1 | Tenancy and data | One server, `platform.db` plus one SQLite file per company, as Solarlytics; the 1-minute series in a second file per company |
| 2 | Solver | `scipy.optimize.milp` (HiGHS) first; `highspy` only if a case misses the 30-second limit in `bench/` |
| 3 | Sign-in | Local mode first (the backend is the sign-in service); supabase mode's SQL ships for review only |
| 4 | Delivery | Mode A (daily TOU slots) for the pilot; Mode B through Product 2 later |

## 7. What to take from the research notes

| Note | What to use |
|---|---|
| `plan-work/research/U2-backends.md` | The lift of the sign-in module, the conventions, the skeleton (section 4) |
| `plan-work/research/R1-evcc-optimizer.md`, `R2-emhass.md` | The formulation patterns, with the licence rules of the Product 1 report |

## 8. Session protocol (how to resume)

At the start of every session: read this file, then the session log at the top of `docs/BUILD_PROGRESS.md`, then `git log --oneline -15` on `v1-build`; run the backend tests and `npx tsc --noEmit`; fix anything failing first; continue at the "Next item" named in the last log line.
For each item: read its row in the master; write the test first where the item is a rule or a rupee figure; implement backend, API and page together; run all checks; update `EQUATIONS.md`, `ASSUMPTIONS.md`, `DESIGN_LOG.md` or `PRODUCT_BRIEF.md` where the change touches them; tick the item (`[x]` only when code, tests and docs are done; `[~]` if partly).
At the end of every session: add a line to the session log (date, items finished, anything left half done, the next item), and commit on `v1-build`.
Stop and ask the owner when: a step needs an owner task (a pilot site, a DISCO bill, a weather API key, a lawyer); a change would move a verified plan or saving beyond what the change explains; a change would delete or overwrite saved data; or you are about to touch anything outside the two repositories.

## 9. Done means

| Level | Software-checkable test |
|---|---|
| Step 1 done | Three pilot bills rebuilt within 2 percent, each a golden test |
| Pilot-ready | WP1 to WP9 ticked; the 192-step genset case solves under 30 s; zero hard-constraint breaches over 1,000 replayed days; a monthly report reproduced from raw data |
```

### 5.2 `docs/BUILD_PROGRESS.md`

```
# (Product) build progress

Generated on (date) from `docs/PLAN.md` (the work packages and their items). The building agent keeps this file current: tick an item only when its code, tests and docs are done; add a dated line to the log below at the end of every work session.

Legend: `[x]` done, `[~]` partly done, `[ ]` not started, `[owner]` a task for the owner (not code), `[skip]` out of scope. Phase: A pilot-ready, B first paying site, C Saudi and Product 2.

## Session log

| Date | Session summary | Next item |
|---|---|---|
| 2026-11-xx | Step 0 done: both repositories made; the sign-in module lifted from solarlytics-backend `c853130` into `backend/(pkg)/tenancy/` with the edits of U2 section 1.7; 1,180 backend tests pass on the lift alone (`-n auto`, 2 min 10 s); the demo seed and the launcher on scratch ports 8151/5293; baseline commit `abc1234` on `main`, branch `v1-build` created. Nothing of the EMS built yet | Step 1, WP1-01 (the tariff catalogue's shape and its first SRO) |

### Step 1 plan (written (date), before WP1-01)

Items WP1-01 to WP1-06; files `tariffs/catalog.py`, `tariffs/bill.py`, `data.py` (the shipped tables), `routes_tariffs.py`; schema v2 adds `tariffs` and `tariff_versions`; tests `test_tariffs_wp1.py` with three bills worked by hand in the docstring.

## WP1 Tariff and regime model

| Status | Item | Source | Note |
|---|---|---|---|
| [ ] | WP1-01 Tariff categories and TOU windows per DISCO, versioned with valid_from and the SRO | Product 1 report, tariff tables; SRO 279(I)/2026 | |
| [~] | WP1-02 The 1-minute settlement engine (import, export at the net-billing rate, MDI, fixed charges, taxes) | WP1, WP4 | Import and export done; MDI pending the pilot's bill |
| [owner] | WP1-05 Three pilot sites' last three bills | WP2 | |
```

### 5.3 `docs/DESIGN_LOG.md`

The first section, as Solarlytics' "8 October 2026: version 0.1" (lines 5-56), then one section per batch in the `DESIGN_LOG_GUIDE.md` shape:

```
# Design log

A dated record of how (Product) was built and what it draws on.

## (date): version 0.1

**Clean-room and reuse statement.** The tariff engine, forecasters, optimiser, battery model and verification were written new for (Product) from (date). The sign-in, the per-company databases, the founder console and the backups were lifted from OnyxLeaf's own `solarlytics-backend` at commit `c853130` (files: `identity.py`, `platform.py`, `companies.py`, `routes_local.py`, `founder.py`, `routes_founder.py`, `store_founder.py`, `store_members.py`, `mfa.py`, `auth.py`, `apikeys.py`, `backups.py`, `seed_demo.py`, the `Limiter` of `routes_accept.py`, and lines 1-560, 705-720, 1183-1185, 4897-4942 and 6786-6835 of `api.py`), with the edits listed in `plan-work/research/U2-backends.md` section 1.7. The founder's session token format is a port of Boxlytics' `supabase/functions/_shared/founder-auth.ts` at `boxlytics@c429bef`, allowed by the owner's decisions of 24 October 2026. No code was copied from EMHASS, evcc-io/optimizer, REopt.jl, OpenEMS, Predbat, OpenLEMS or python-microgrid; where a formulation pattern follows one of them the equation's row in EQUATIONS.md names it.

**Method sources.** All public: (a list, each with what was taken)

**Data shipped with the product.**

| Data | Source | Terms |
| --- | --- | --- |
| DISCO TOU tariff categories and rates, Pakistan | NEPRA SRO 279(I)/2026 and the DISCO schedules read on (date) | Public; `[verify]` until a pilot bill confirms each |

**Decisions.**

| Decision | Reason |
| --- | --- |
| One SQLite file per company plus a telemetry file beside it, not PostgreSQL | The owner's chosen shape for Solarlytics; one server process; a daily copy by SQLite's own backup; the series file keeps the workspace file small |

**Third-party software.**

| Package | Use | Licence |
| --- | --- | --- |
| scipy (`optimize.milp`, HiGHS) | The MILP solver | BSD-3-Clause (scipy); MIT (HiGHS) |
| PyJWT, cryptography | The sign-in's tokens | MIT; Apache-2.0 or BSD |
| python-microgrid | Verification folder only; never imported by the product | LGPL-3.0 |

## Step 1 of the v1 build: the tariff and regime model ((date))

**Why.** WP1-01 to WP1-04 and the owner's request for a bill that reproduces a pilot site's last three bills within 2 percent.

**What changed.**
- **The tariff catalogue.** `tariffs/catalog.py`: a category is `(disco, code, valid_from, seasons, windows, rates, mdi, fixed, taxes, source)`; `vectors(site, start, steps)` gives the 15-minute import and export price vectors. Shipped values in `data.py`, each with its SRO and date, `[verify]` until confirmed.
- **The settlement engine.** `tariffs/bill.py` `settle(series_1min, category, regime)`: import and export netted per the regime's rule (net billing at the published rate; legacy net metering as units), MDI on the 30-minute peak `[verify]`, fixed charges, the taxes of the category.

**Behaviour removed or changed.** None (the first version).

**Not done yet.** The ICP flat incremental price (WP1-03) waits for the owner's reading of the SRO; KSA tariffs (WP12).

**Tests.** 48 backend tests in `test_tariffs_wp1.py`: three bills worked by hand in the docstring (B-IESCO-B3-Aug-2026, within 0.4 percent), the window boundaries at 18:00 and 22:00 in each season, a 422 for an unknown category. Replay: none yet.

**Review.** One high (the export rate applied to gross, not net, units: fixed, test `test_export_is_netted_per_month`), two lows (wording). Nothing left open.
```

---

## 6. Open questions, decisions for the owner, and what is UNVERIFIED

1. **The product's name**, and with it the package, the env prefix, the key prefix, the token issuer, the company header and the GitHub repository names.
2. **Where the 1-minute telemetry lives.** A second SQLite file per company (keeps the lifted `use_company` machinery and the daily backup, but a multi-GB file is slow to copy whole) or Parquet or DuckDB files beside the workspace (fast analytics, no `Connection.backup`, a new backup rule). My recommendation: a second SQLite file keyed `(site_id, ts)` for the pilot (three sites, about 13 million rows a year), with daily aggregates in the workspace file, and the decision revisited at ten sites.
3. **The EMS roles and permissions** (section 1.7, item 1): `admin`, `operator`, `analyst`, `viewer` is a proposal.
4. **The solver**: `scipy.optimize.milp` (HiGHS inside scipy 1.17.1, present in the `ops` env) against `highspy` directly (not installed) or CVXPY/PuLP (not installed, and not needed for one hand-written model). UNVERIFIED that the 192-step genset case solves under 30 s with scipy's wrapper; measure first in `bench/`.
5. **The venv**: the backend needs PyJWT and cryptography; install into `ops` or make `.scratch/venv` (Solarlytics' way). The task forbade installing anything; the owner decides.
6. **Ports**: 8051/5193 and scratch 8151/5293 are proposals; the owner has 8041/5192 (Solarlytics), 8060 (its release), 8000/5173 (Boxlytics), 8001/5173 (Retailytics); Panelytics' ports were not checked.
7. **Whether the frontend's sign-in pages lift cleanly** (UNVERIFIED): the web repository is not on this machine; the backend's records name `/login`, `/admin`, `session.ts`, `founderAdmin.store.ts`, `companyswitch.tsx`, `NotSetUp.tsx` and the launcher. A sibling task on the frontend should confirm from `C:/Users/SaadTahir/Videos/solarlytics-backup-2026-10-28` (the memory note says it holds the web folder copy) or from GitHub.
8. Closed: `backups.start_daily()` is called in `api.py` 607-609 (`@app.on_event("startup")`); the exception handlers are at 554 (`StarletteHTTPException`), 571 (`RequestValidationError`) and 599 (`layout.TooLarge`, a Solarlytics one to drop).
9. Closed: `imports.py` uses the sign-in modules only for the workspace export and import (`platformdb.index_find/index_add` for share links at 846 and 1022, `store_members.set_preset` for the roles of an imported file at 1102); `schemas.py` does not use them (the grep matched the word "identity" in a docstring at 487). Neither needs lifting.
10. **`sibling.py` on `main`** names `solarlytics-web` (lines 23-31 today); the memory note says a branch `fix/web-repo-name` (`ddeadca`) prefers `solarlytics-frontend`. The EMS should name its sibling folder once and pin it.
11. **Supabase mode** is built in the module and shipped as SQL for review; the owner's decision for Solarlytics was local mode first. The EMS can carry the mode unused, as Solarlytics does, at no cost.
12. **A Streamlit fleet monitor** (Boxlytics' `dashboard.py` pattern) is not in the first build; the founder console is the admin dashboard.

## 7. Licence notes

- `solarlytics-backend` has no LICENSE file in its root listing (`ls -la` on 6 November 2026 shows none); it is OnyxLeaf's own private repository (`saad-tahir/solarlytics-backend`), so reusing its code in another OnyxLeaf product is the owner's own decision and raises no third-party licence question. Record the reuse by file and commit in the EMS DESIGN_LOG as `CLAUDE.md` line 10 asks for Boxlytics reuse.
- The founder token format (`founder.py` 14-17, 143-202) is a port of Boxlytics' `supabase/functions/_shared/founder-auth.ts` at `boxlytics@c429bef`; the owner allowed reusing Boxlytics' sign-in parts for Solarlytics (memory `solarlytics-scope-decisions.md`, "Reuse: the owner allowed reusing Boxlytics code as is for these critical parts"). Carrying that port into a third product is, strictly, a second reuse of Boxlytics-derived code; name it in DESIGN_LOG and let the owner confirm. Boxlytics belongs to another party (`CLAUDE.md` line 10; memory `boxlytics-repos-hands-off.md`).
- Boxlytics-backend's root `README.md` line 148 says "Extends existing research code. Refer to `orinal_code/` for original licensing terms"; no LICENSE file is in its root. Nothing of it is proposed for copying; patterns only (section 3).
- The open-source rules the EMS follows are the Product 1 report's lines 365-374 (MIT, BSD, Apache-2.0: copy and modify, keep notices, no "REopt" trademark; MPL-2.0 unmodified; LGPL-3.0 in test tooling only; EPL-2.0 patterns; GPL-3.0 and EUPL-1.2 learn only; AGPL-3.0 never; Predbat and evcc's sponsor-only files never) and the guide's lines 150-153.
- `mfa.py` was "written from the texts of the two RFCs" (lines 2-15): no third-party code.
