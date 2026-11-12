# Deploying Gridlytics

How a hosted copy of Gridlytics is run (plan Part 2.7, 10.1 and 10.3, DEP-06 and DEP-07; round 7, audit parity-architecture-02). The shape is Solarlytics'
(`solarlytics-backend` at `c853130`, `docs/DEPLOY.md` and stage S0b), renamed and cut to what Gridlytics has:

| Delivery | What it is | Who it suits |
|---|---|---|
| The release folder | `python scripts/build_release.py` makes `release/gridlytics-<version>/` and its zip: the API and the web app (built in local mode from the web repository beside, `gridlytics-frontend`, or the folder `--web` names) on one port, http://127.0.0.1:8061, with a data folder of its own; `Start Gridlytics.cmd` runs it on one Windows machine | One office, one machine, nobody reaching it from outside |
| The container | `Dockerfile` at the root: the API only, its data folder on a volume; `deploy/docker-compose.yml` runs it | The API of a hosted copy |
| The web app on Vercel and the API in the container | The web repository's (`gridlytics-frontend`) `frontend/` on Vercel at the app's host (its `frontend/vercel.json`); the container at the API's host | A hosted copy on two host names: the hosted delivery |

All run the same code at the same pinned versions (`requirements.txt` here, `frontend/package-lock.json` in the web repository), on SQLite in write-ahead
mode. What only the owner can do (a Linux host, the domain names, certificates, the server's secrets, the Vercel project, and for supabase mode the Supabase
project) is in the steps below and in the checklist at the end; nothing here creates them, and agents never do them.

## The release folder

`python scripts/build_release.py` (Python 3.12, and Node 22 with npm to build the web app) makes `release/gridlytics-<version>/` and
`release/gridlytics-<version>.zip` (both git-ignored). `--web <folder>` names the web repository when it is not `gridlytics-frontend` beside this one;
`--dist <folder>` takes a web app already built in local mode instead of building one. The folder holds:

- `app/backend/gridlytics/` and `app/frontend/dist/` (the page, which `run.py` names to the API in `GRIDLYTICS_WEB_DIST`, so the page and `/api` share one
  port), and `requirements.txt` (this repository's pins);
- `run.py`, which starts it on `127.0.0.1:8061` (`GRIDLYTICS_PORT` changes it) in local mode, with its own `data/`, the daily copies and the 15-minute
  scheduler in its one process, and reads the founder console's four settings from `data/.env.founder` when one is there; `Start Gridlytics.cmd` makes a
  private Python environment on the first start and runs it;
- `restore_backup.py`, the restore of a company's copy, set to the release's own data folder;
- `README.txt` with the same steps for the person who runs it.

People sign in with the accounts the founder console makes, as on a hosted copy. The build makes no git tag: it prints the tag for the owner to make.

## The two sign-in modes

The server's `GRIDLYTICS_AUTH_MODE` says where people sign in; the web app is built for the same mode (`VITE_AUTH_MODE`). The mode is never baked into the
image: the server's environment sets it.

| Mode | Where people sign in | What it needs |
|---|---|---|
| `local` (the default) | The backend itself (`/api/local/*`): accounts, companies and memberships are in `platform.db`, the passwords hashed by the app | `GRIDLYTICS_LOCAL_JWT_SECRET` on any host other than this machine |
| `supabase` (optional; the owner applies it) | Gridlytics' own Supabase project, with an email and a password only; the backend checks the project's tokens and reads the memberships from it | The project (below), its URL and anon key on the server and in the web app, and its service-role key on the server only |

In both modes the accounts are preassigned: only the people the founder console or a company's administrator adds, each with an email and a first password,
get in. There is no sign-up, no Google or other provider, no magic link and no invitation. Each company has its own database files. Gridlytics never uses
Boxlytics' or Solarlytics' Supabase project, their accounts or their functions.

## The container

```bash
docker build -t gridlytics:0.1.0 .
docker run -d --name gridlytics --restart unless-stopped \
  -p 127.0.0.1:8061:8061 \
  -v gridlytics-data:/data \
  --env-file /etc/gridlytics/.env.production \
  gridlytics:0.1.0
```

or, with the same image, `deploy/docker-compose.yml`, which reads `.env.production` at the repository's root:

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f api
```

- **Port.** The app listens on 8061 inside the container (`GRIDLYTICS_PORT`). Publish it on the loopback address only (`127.0.0.1:8061`, as both commands
  above do) and let the TLS proxy below reach it; never publish it on a public address without TLS in front.
- **The environment** is a copy of `.env.production.example` filled in on the server (The server's environment, below), readable by the owner only.
- **GRIDLYTICS_HOSTS and the signing key.** The host names the API answers to, comma-separated: any other Host header is refused (against DNS rebinding).
  Put the public name here (`api.grid.example.com`). Without it the app answers only to `localhost` and `127.0.0.1`. In local mode a name other than this
  machine's needs `GRIDLYTICS_LOCAL_JWT_SECRET` (32 characters or more): without it the server refuses to start, and the container's log
  (`docker logs gridlytics`) says "Local sign-in on a host other than this machine (...) needs GRIDLYTICS_LOCAL_JWT_SECRET". The key signs every access
  token: keep it across restarts, as changing it signs everyone out.
- **The proxy's address: FORWARDED_ALLOW_IPS and GRIDLYTICS_TRUST_PROXY.** The image runs uvicorn with `--proxy-headers`: it takes the client's address
  and scheme from `X-Forwarded-For` and `X-Forwarded-Proto`, but only on connections from `FORWARDED_ALLOW_IPS` (uvicorn's own setting). Inside a
  container the proxy's connections do not come from 127.0.0.1 but from Docker's network gateway: with `deploy/docker-compose.yml` that is `172.30.61.1`
  (the compose file fixes its network), with `docker run` on Docker's default network it is that network's gateway (`docker network inspect bridge`,
  usually `172.17.0.1`), and with the API run on the host without Docker it is `127.0.0.1`. Never `*`. `GRIDLYTICS_TRUST_PROXY=1` tells the sign-in
  lockout to count tries by the address the proxy gives; without it, every try through the proxy counts under one address, and the log says so once.
- **The volume.** `/data` holds everything the app keeps, and the app (user 10001) writes nowhere else:
  - `platform.db`: the registry (the companies, each with its files, seats and whether Gridlytics is on; the founder's audit; the requests for access
    sent from the contact page, kept 12 months; in local mode the accounts, the memberships, the sessions and the record of sign-ins);
  - `companies/<company id>.db` and `companies/<company id>-telemetry.db`: one company's workspace and its telemetry; a request opens its own company's
    files only; `companies/backup/` holds their copies;
  - `keys/<company id>/`: the signing keys of the company's sites (the edge checks each plan's signature with them);
  - `reports/<company id>/`: the reports the company made;
  - `gridlytics.db`: the default workspace, which holds no company's work on a hosted copy; `backup/` (its copies and the registry's) and
    `logs/gridlytics.log`;
  - `platform.secret`: local mode's signing key, made only when `GRIDLYTICS_LOCAL_JWT_SECRET` is not set, which a hosted copy never is without.
- **Health.** `GET /api/health` answers `{"status": "ok", "version": "..."}` without a session; the image's HEALTHCHECK calls it, so `docker ps` shows
  the container healthy or not.
- **One uvicorn process per data folder.** The image runs one uvicorn process (no `--workers`) with `GRIDLYTICS_SCHEDULER=1`, so the 15-minute loop (the
  plans, the alerts, the nightly jobs) runs once. Never run a second container on the same volume, or scale the compose service past one: every plan would
  be solved twice, and a person's first sign-in into a company and the seats are ordered within one process only. To serve more, run more hosts with their
  own data (plan Part 10.6 has the path to 1,000 sites).
- **No `server` header.** The image's command adds `--no-server-header`, so the answers do not say `server: uvicorn`. The app sets its own security headers
  (no sniffing, no framing, no referrer, and a content security policy for scripts, frames, forms and the base address).
- **GRIDLYTICS_DOCS=1** opens the interactive API pages at `/docs`; leave it unset on a hosted copy.
- **No web app inside the image.** The image holds the API only and builds no page; the web app is the `gridlytics-frontend` repository's (The web app on
  Vercel, below). To serve a web build from the container's own host instead, in local mode, build it in the web repository
  (`VITE_AUTH_MODE=local npm run build` in `frontend/`), mount its `dist/` read only and name it:
  `-v /srv/gridlytics-web/dist:/web:ro -e GRIDLYTICS_WEB_DIST=/web`. Unset, the API answers `/api` only.
- **Building the image.** `docker build` reads this repository only (`.dockerignore` leaves out the data folder, `.scratch/`, the tests, every `.env` file
  and the demo seed's password list). The base is pinned to a release (`python:3.12.16-slim-bookworm`); pin it to its digest too once the registry is
  reached, and rebuild it for its security updates (the owner's checklist).

### The server's environment

Every setting the server reads is in `.env.production.example` at the root, with its default; `backend/tests/test_deploy_docs.py` fails when one the
server reads is missing there. On the server, copy it to `.env.production` (git-ignored), fill it in there, and keep it readable by the owner only. Make
each secret on the server, for example with `python -c "import secrets; print(secrets.token_urlsafe(48))"`, and never write one in this repository or in
the web app's settings (`frontend/.env.example` in the web repository: every `VITE_` value is written into the page).

| Setting | Mode | What |
|---|---|---|
| `GRIDLYTICS_AUTH_MODE` | both | `local` or `supabase`; the web app's `VITE_AUTH_MODE` says the same |
| `GRIDLYTICS_LOCAL_JWT_SECRET` | local | The access tokens' signing key, 32 characters or more; needed on any host other than this machine |
| `GRIDLYTICS_SUPABASE_URL`, `GRIDLYTICS_SUPABASE_ANON_KEY` | supabase | The project's https address and its anon (publishable) key, the same as the web app's |
| `GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY` | supabase | The project's service-role key, on the server only: the founder console makes the accounts with it |
| `GRIDLYTICS_SUPABASE_JWT_SECRET` | supabase | Only while the project signs its tokens with the legacy JWT secret |
| `GRIDLYTICS_JWT_AUDIENCE`, `GRIDLYTICS_JWKS_TTL`, `GRIDLYTICS_MEMBERSHIP_TTL` | supabase | `authenticated`; 600 s for the project's public keys; 60 s for a person's memberships |
| `GRIDLYTICS_FOUNDER_USERNAME`, `GRIDLYTICS_FOUNDER_PASSWORD_HASH`, `GRIDLYTICS_FOUNDER_TOTP_SECRET`, `GRIDLYTICS_FOUNDER_SESSION_SECRET` | both | The founder console's login (below) |
| `GRIDLYTICS_HOSTS` | both | The API's host names |
| `GRIDLYTICS_CORS_ORIGINS` | both | The web app's origins on another host (`https://grid.example.com`), comma-separated or a JSON array; never `*` |
| `GRIDLYTICS_CORS_ORIGIN_REGEX` | both | A pattern for Vercel's preview origins; best on the staging API only |
| `FORWARDED_ALLOW_IPS`, `GRIDLYTICS_TRUST_PROXY` | both | Where the TLS proxy's connections come from (above), and `1` behind it |
| `GRIDLYTICS_PORT` | both | The port inside the container (8061) |
| `GRIDLYTICS_DATA_DIR`, `GRIDLYTICS_DB` | both | The data folder (`/data` in the image) and the default workspace's file in it |
| `GRIDLYTICS_DAILY_BACKUP`, `GRIDLYTICS_RESET_BACKUP` | both | The daily copies (`1` in the image) and the copy before a workspace's settings are put back to the shipped ones |
| `GRIDLYTICS_LOGS`, `GRIDLYTICS_ERROR_WEBHOOK` | both | The error log (`0` turns it off) and an https address a record of each failed tick is posted to |
| `GRIDLYTICS_WEB_DIST` | both | A built web app the API serves on its own host too; unset, no page |
| `GRIDLYTICS_SCHEDULER`, `GRIDLYTICS_TICK_WORKERS` | both | `1` runs the 15-minute loop in this process (the image sets it; one process per data folder); how many sites a tick solves at once |
| `GRIDLYTICS_RATE_USER`, `GRIDLYTICS_RATE_ADDRESS`, `GRIDLYTICS_RATE_PUBLIC` | both | Requests a minute per person, per address, and per address to the public paths |
| `GRIDLYTICS_RATE_SIGN_IN`, `GRIDLYTICS_RATE_REPORTS` | both | Sign-ins from one address in 5 minutes; reports a company may generate in an hour |
| `GRIDLYTICS_RATE_ACCESS_REQUESTS` | both | Requests for access the contact page may send from one address in an hour (5; `0` turns the limit off) |
| `GRIDLYTICS_DOCS` | both | `1` opens the API pages at `/docs`; off on a hosted copy |
| `GRIDLYTICS_DEMO_DAYS`, `GRIDLYTICS_DEMO_REPLAN_HOURS` | both | The demo sites' days of history and how often it re-plans, for a company's administrator who loads them from Fleet |

### The founder console

The founder console is Gridlytics' own: its pages are the web app's `/admin` (`https://grid.example.com/admin`), and its calls go to the backend's
`/api/founder/*`, with a login of its own. It makes the companies and the accounts: companies (make one with its owner, rename, seats, Gridlytics on or
off, disable), users (make an account with a first password, reset a password, disable, add to a company, remove), backups (per company), the audit,
and the access requests the contact page sends (read them and set their state; nothing there makes an account). Every action is audited in
`platform.db`. It answers 503 until its four settings are set:

1. **The username:** `GRIDLYTICS_FOUNDER_USERNAME`, for example `founder`.
2. **The password's hash:** run `python -m gridlytics.founder hash` (it asks for the password twice and prints a PBKDF2 hash; in the container:
   `docker exec -it gridlytics python -m gridlytics.founder hash`). Put the hash in `GRIDLYTICS_FOUNDER_PASSWORD_HASH`, and the password in a password
   manager. The hash holds `$` signs (`pbkdf2_sha256$...`): `docker run --env-file` takes each `NAME=value` as written, quotes and all, so there it goes in
   unquoted; with `deploy/docker-compose.yml` (whose `env_file` reads `$` as the start of a variable) and in a shell, put it in single quotes
   (`GRIDLYTICS_FOUNDER_PASSWORD_HASH='pbkdf2_sha256$...'`). Use one of the two ways and write the file for it.
3. **The code's secret:** run `python -m gridlytics.founder totp-secret`. It prints a Base32 secret for `GRIDLYTICS_FOUNDER_TOTP_SECRET` and an
   `otpauth://` link for the authenticator app. `python -m gridlytics.founder code` prints the current code, to check the app agrees.
4. **The session key:** `python -m gridlytics.founder session-secret` prints a new one for `GRIDLYTICS_FOUNDER_SESSION_SECRET`. It signs the console's
   8-hour sessions; changing it ends them all.

Restart the container, open `/admin`, give the username and the password, then the six-digit code. Wrong passwords and wrong codes are counted and locked
out, and a code is taken once.

### The first company and its owner

People are added by OnyxLeaf, never by sign-up or invitation:

1. In the founder console, make the company with its owner account (an email, a name and a first password), switch Gridlytics on for it and set its
   seats.
2. Make the other accounts with a first password each and add each to the company.
3. Give each person their first password by a channel of your own. They sign in at `https://grid.example.com/login` and change it under My account.
4. The company's owner signs in and gives the roles (administrator, operator, analyst, viewer) under Company settings; the company's administrators can
   make accounts there too, with a first password.
5. The owner adds the company's sites (Fleet, Add a site).

For demonstrations in local mode, `python -m gridlytics.seed_demo` makes demo companies, accounts and a founder login with new random passwords, written
only to a git-ignored file. It is not for a live copy.

### TLS

Any reverse proxy that terminates TLS and forwards to `127.0.0.1:8061` will do. Two examples for the API's host (the domain and the e-mail are the
owner's):

```caddyfile
# Caddy: certificates from Let's Encrypt, renewed by Caddy itself
api.grid.example.com {
    request_body {
        max_size 26MB          # the app's own limit is 25 MB a request
    }
    reverse_proxy 127.0.0.1:8061
    header Strict-Transport-Security "max-age=31536000"
}
```

```nginx
# nginx, with a certificate from certbot (certbot --nginx -d api.grid.example.com)
server {
    listen 443 ssl http2;
    server_name api.grid.example.com;
    ssl_certificate     /etc/letsencrypt/live/api.grid.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.grid.example.com/privkey.pem;
    client_max_body_size 26m;          # the app's own limit is 25 MB a request
    add_header Strict-Transport-Security "max-age=31536000" always;
    location / {
        proxy_pass http://127.0.0.1:8061;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;       # a large export or report
    }
}
server { listen 80; server_name api.grid.example.com; return 301 https://$host$request_uri; }
```

The app counts a body sent in chunks (with no length) itself and refuses it past 25 MB; the proxy's limit stops the bytes earlier. The proxy must pass
`Host` as the client sent it (the app checks it against `GRIDLYTICS_HOSTS`) and `X-Forwarded-For` (the sign-in lockout's address). Send
Strict-Transport-Security once the domain is settled, as above. No route streams events today; when the plan's live stream (SSE) is built, its location
needs `proxy_buffering off`.

## The database

Gridlytics keeps one SQLite workspace and one telemetry file per company, in write-ahead mode, and one registry beside them (`platform.db`). It does not run
on a database server. "A managed database" for a hosted copy therefore means managed storage for the volume: a block volume with snapshots, in the region
the customers' data may be kept in. A schema change copies the file first (a copy that is never pruned).

## Backups

Three layers (plan Part 3.3, DEP-07):

1. **Daily, in the app.** With `GRIDLYTICS_DAILY_BACKUP=1` (the image sets it) every company's files are copied once a day with SQLite's own backup,
   consistent while the app writes, together with the company's signing keys (`keys`) and its reports (`reports`) as one set, into
   `/data/companies/backup/`; the registry and the default workspace into `/data/backup/`. The last 14 daily sets are kept. The founder console's backups
   page lists them and makes one on demand.
2. **Nightly, off the machine.** Copy `/data/backup/`, `/data/companies/backup/` and `platform.db` to object storage with versioning, every night (for
   example restic or rclone from the host, reading the volume's folder). Keep the server's `.env.production` in the password manager, not with the copies.
3. **A restore drill every quarter**, on a staging copy.

**Restoring a company** is a step by hand, with the API stopped: `scripts/restore_backup.py` puts back one set (the workspace, its telemetry, its keys and
its reports) and copies the files as they are first, so a restore can be undone. In the container:

```bash
docker compose -f deploy/docker-compose.yml stop api
docker compose -f deploy/docker-compose.yml run --rm api python /app/scripts/restore_backup.py                       # the companies
docker compose -f deploy/docker-compose.yml run --rm api python /app/scripts/restore_backup.py --company <id or name>  # its copies
docker compose -f deploy/docker-compose.yml run --rm api python /app/scripts/restore_backup.py --company <id or name> <copy>
docker compose -f deploy/docker-compose.yml start api
```

## The web app on Vercel

The web app is the `gridlytics-frontend` repository's `frontend/`, a static page: Vercel serves it at the app's host, and it calls the API at the API's host.
`frontend/vercel.json` there holds what Vercel needs; the steps are the owner's.

1. **The project.** In Vercel, add a project from the web repository. Set the **Root Directory** to `frontend` and leave the framework as Vite:
   `frontend/vercel.json` sets the install command (`npm ci`), the build command and the output folder (`dist`). Set the **Node.js Version** (Settings,
   Build and Deployment) to 22.x, as the web repository's `.nvmrc` and its CI use.
2. **The variables** (Settings, Environment Variables), for Production, and for Preview if previews are used. They are listed in `frontend/.env.example`
   there. Every one of them is written into the page, so none is a secret:

   | Variable | Value |
   |---|---|
   | `VITE_AUTH_MODE` | `local` or `supabase`, as the server's `GRIDLYTICS_AUTH_MODE` |
   | `VITE_API_BASE_URL` | `https://api.grid.example.com/api` (for Preview: the staging API's) |
   | `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY` | supabase mode only: the project's address and its anon (publishable) key, the same as the server's |

3. **The content security policy.** `frontend/vercel.json` sends it with every page, and its `connect-src` must name the hosts the page calls. As shipped
   it names a placeholder: write the API's origin (`https://api.grid.example.com`) in place of `https://api-host.invalid`, and in supabase mode the
   project's too (`https://<project ref>.supabase.co`). A staging API's origin may stand beside the live one, for the previews. Commit the change in the
   web repository.
4. **What the build checks.** The build command runs `node scripts/deploy_check.mjs` first: it stops the build when `VITE_AUTH_MODE` is not `local` or
   `supabase`, when `VITE_API_BASE_URL` is not an https address ending in `/api`, when supabase mode lacks its address or key, when a key is a secret one
   or a `VITE_` variable is named like a secret, and when `connect-src` lacks a host this build calls, still names the placeholder, or names a wildcard or
   plain http; it prints the line to write. After `npm run build`, `node scripts/deploy_check.mjs --built dist` stops the deployment when the build is not
   the one checked: another API address, a secret key written into it, or the other sign-in mode's code in it.
5. **What vercel.json sends.** Every address that is not a built asset (`/`, `/login`, `/fleet`, `/sites/<id>/plan`, the founder console's `/admin/*`,
   `/reset-password` in supabase mode) is the page itself, so a deep link or a refresh works; a missing file under `/assets/` stays a 404. Every answer
   carries the policy (scripts from the page's own host only, no framing, no plugins), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` and
   `Referrer-Policy: no-referrer` (the API's own values); the hashed assets under `/assets/` are kept for a year. Vercel sends HSTS for its own hosts.
6. **The domain.** Settings, Domains: add the app's host (`grid.example.com`); the DNS record is below. Vercel makes its certificate.
7. **Previews.** Each branch gets a preview address. Build the previews against the staging API (the Preview variables), whose
   `GRIDLYTICS_CORS_ORIGIN_REGEX` matches them, for example `https://gridlytics-[a-z0-9-]+-<team slug>\.vercel\.app`; a `vercel.app` name can be taken by
   anyone, so keep the pattern off the live API. In supabase mode add the previews' reset address to the project's Redirect URLs.

On the server, `GRIDLYTICS_CORS_ORIGINS` names the app's origin (`https://grid.example.com`) so the page may call the API from the browser.

## Gridlytics' own Supabase project

Supabase mode only; local mode needs none of this. The project is Gridlytics' own, which the owner creates and applies by hand: never Boxlytics' project,
never Solarlytics', their accounts or their functions. `supabase/README.md` has each step in full; in short:

1. **Create the project** in the owner's own Supabase organization.
2. **Check, apply, check again:** `supabase/sql/grid_000_preflight.sql`, then `supabase/migrations/grid_001_accounts_and_companies.sql`, then the
   preflight again, in the SQL Editor, after review. Never `supabase db push`.
3. **Turn off sign-ups and Google** (Authentication, Sign In / Providers): turn off **Allow new users to sign up**; keep Email with passwords; turn off
   **Google** and every other provider; keep anonymous sign-ins and magic links off.
4. **The URL configuration:** the Site URL is the web app's host; the Redirect URLs are its password-reset page only (`/reset-password`).
5. **The keys:** the URL and the anon key go to the server (`GRIDLYTICS_SUPABASE_URL`, `GRIDLYTICS_SUPABASE_ANON_KEY`) and to the web app's build
   (`VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`); the service-role key goes to the server only (`GRIDLYTICS_SUPABASE_SERVICE_ROLE_KEY`), never to the
   web app, a `VITE_` setting or a commit. The web app never gets the service-role key, the JWT secret or the database password.

Companies and people are then added only through the founder console, which writes them to the project and to `platform.db` with the same ids.

## DNS

The domain names are the owner's decision; these examples use `grid.example.com` for the web app and `api.grid.example.com` for the API.

1. **The app's host:** add it in Vercel (Settings, Domains), then at the DNS provider make the record Vercel shows: a `CNAME` to Vercel's target for a
   subdomain (`cname.vercel-dns.com`), or Vercel's `A` record for a bare domain.
2. **The API's host:** an `A` record (and `AAAA` for IPv6) to the server's public address. The TLS proxy above takes its certificate once the name
   resolves.
3. **Then name the hosts everywhere they are named:** on the server `GRIDLYTICS_HOSTS=api.grid.example.com` and
   `GRIDLYTICS_CORS_ORIGINS=https://grid.example.com`, then restart the container; on Vercel `VITE_API_BASE_URL=https://api.grid.example.com/api` and
   `connect-src` in `frontend/vercel.json`, then redeploy; in supabase mode, the project's Site URL and Redirect URLs.

## Updates and monitoring

- **An update:** take a backup from the founder console, then `docker compose -f deploy/docker-compose.yml up -d --build` on the new version. The API
  migrates each file as it opens it, after copying it.
- **Load:** `python scripts/load_check.py --threads 8 --seconds 60` measures a server of its own on a throwaway data folder (the demo company, and the four
  demo sites with `--demo-days`), never the owner's data or a server already running: requests a second, and each page's request times (median, 95th
  percentile, slowest). Run it on an idle machine, before a hosted copy is sized.
- **Monitoring:** watch `GET /api/health` through the proxy with an uptime service; `/data/logs/gridlytics.log` holds the errors; set
  `GRIDLYTICS_ERROR_WEBHOOK` for a record of each failed tick in an error tracker; the founder console shows the ticks and the failures.

## The owner's checklist for a hosted copy

Nothing below is built or bought by the code; each is the owner's, in this order:

- [ ] The repositories on GitHub, with the read-only tokens their CI uses to check out each other (`FRONTEND_READ_TOKEN` in the backend's,
      `BACKEND_READ_TOKEN` in the web app's); agents never add remotes or push.
- [ ] The domain names: the app's host and the API's host (and staging's).
- [ ] The sign-in mode. For supabase mode: Gridlytics' own Supabase project, `supabase/migrations` applied and checked, sign-ups and Google turned off, the
      URLs and the keys (above). Local mode needs none.
- [ ] A Linux host with Docker, and a block volume that takes snapshots, in the region the customers' data may be kept in.
- [ ] The server's environment: `.env.production` filled in from `.env.production.example`, its secrets made on the server, readable by the owner only.
- [ ] The founder console's settings, with the code's secret in an authenticator app on the founder's phone.
- [ ] TLS: a reverse proxy with a certificate (Caddy, or nginx with certbot, as above), and `FORWARDED_ALLOW_IPS` set to where its connections come from.
- [ ] The container running, and `GET /api/health` answering through the proxy.
- [ ] The Vercel project: root `frontend`, its variables, `connect-src` in `frontend/vercel.json`, the domain.
- [ ] DNS: the app's host and the API's host.
- [ ] The first company made in the founder console with its owner, Gridlytics switched on for it; the other accounts made and their first passwords given.
- [ ] Off-machine backups: object storage with versioning, a nightly copy, and a restore tested on staging.
- [ ] A staging copy on its own host name.
- [ ] Uptime monitoring of `/api/health`, and who is called when it fails.
- [ ] The base image pinned to its digest (`python:3.12.16-slim-bookworm@sha256:...`) once the registry is reached, and rebuilt for its security updates.
