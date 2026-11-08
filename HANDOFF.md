# Gridlytics: developer handoff

Read this first. It is written for a developer who has the two repositories and nothing else: no chat history, no earlier notes. Everything you
need to set up, run, test and continue the work is here or in a file this page names. Last updated 7 November 2026.

## 1. What the product is

Gridlytics (working name) is OnyxLeaf's energy-dispatch optimiser for commercial and industrial sites in Pakistan. A site has some of: solar PV, a
battery, a diesel or gas genset, and a grid connection with time-of-use (TOU) tariffs, monthly maximum-demand (MDI) charges and load-shedding
outages. Every 15 minutes the backend does five things:

1. It forecasts PV, load and the chance of an outage for the next 48 hours.
2. It solves a mixed-integer linear programme for the cheapest schedule for the battery and genset, priced by a 1-minute bill engine.
3. It compiles that schedule into the slot table a given inverter brand accepts ("Mode A"), or into per-step setpoints ("Mode B").
4. It signs the result for an on-site edge device.
5. It measures the saving against a replay of the inverter's own native settings.

The web app shows a fleet of sites, each site's plan, telemetry, forecasts, outages, battery, genset, savings, tariffs and alerts. It also has
onboarding, pilot set-up (a switchback calendar), reports and a founder console.

The original requirements and the full specification are in this repository under `docs/plan/` (see section 11 and `docs/plan/README.md`):
- the Product 1 brief and its deep review;
- `EMS_BUILD_PLAN.md`, the full specification;
- the tracker;
- the research the plan cites.

The plan is long. Section numbers like "Part 4.5" in code comments refer to it.

## 2. Repositories and state

| Repository | GitHub (private; ask the owner for access) | Branches | Contents |
|---|---|---|---|
| `gridlytics-backend` | https://github.com/saad-tahir/gridlytics-backend | `main` (a baseline commit), `v1-build` (all the work) | Python 3.12 API and engine |
| `gridlytics-frontend` | https://github.com/saad-tahir/gridlytics-frontend | `main` (a baseline commit), `v1-build` (all the work) | React 19 web app |

`git log` on `v1-build` is the authority on what is in; section 12 below logs the work after this page was written. On the build machine the
repositories live in `C:\Users\SaadTahir\Videos\`.

### Get the code

The two repositories must sit side by side in one folder: the launcher and the tests find each other as `../gridlytics-backend` and
`../gridlytics-frontend`.

```bash
mkdir gridlytics && cd gridlytics
git clone -b v1-build https://github.com/saad-tahir/gridlytics-backend.git
git clone -b v1-build https://github.com/saad-tahir/gridlytics-frontend.git
```

Then follow section 3 from inside the `gridlytics` folder.

### How changes come in

- Branch from `v1-build`: one branch per piece of work, e.g. `fix/offgrid-carry-over`.
- Commit with tests. Run both suites (section 3) before you push.
- Push your branch, and open a pull request into `v1-build`.
- The owner reviews and merges. Nobody pushes to `main` or `v1-build` directly, and nobody merges their own pull request.
- `main` gets `v1-build` when the owner decides v1 is ready.
- Tests at the last update (7 Nov, after round 5): backend 495 passed and 7 skipped; web app 257 passed, typecheck and build clean. A scripted
  browser check visited 405 pages across four roles, a phone width (375 px) and the founder console, with 0 failures.

## 3. Set up a machine

The build machine is Windows 11 using Git Bash, and every command on this page is a Git Bash command. Linux or macOS work the same, with `bin/` in
place of `Scripts\`. **Each command block starts in the folder that holds both repositories.**

### Backend

1. Install Python 3.12 (3.12.11 was used). Any CPython 3.12 works. If `python3.12` and `py -3.12` do not exist, use the interpreter's full path.
   On the build machine that is `C:\Users\SaadTahir\anaconda3\envs\ops\python.exe`. Its PATH `python` is the Microsoft Store alias, which does not
   work.
2. Make the repository's own venv in `.scratch\venv`. The launcher looks for it there. The install needs internet (PyPI).

   ```bash
   cd gridlytics-backend
   python3.12 -m venv .scratch/venv          # on Windows: py -3.12 -m venv .scratch/venv, or <full path to python 3.12> -m venv .scratch/venv
   .scratch/venv/Scripts/python.exe -m pip install -r requirements-dev.txt
   ```

   The pins are in `requirements.txt`: fastapi 0.141.1, pydantic 2.13.4, numpy 2.4.3, scipy 1.17.1 (the MILP runs on its HiGHS), pandas 3.0.2,
   APScheduler 3.11.0, reportlab 4.4.4, cryptography 46.0.3. Dev adds pytest 8.4.2 and pytest-xdist.
3. Run the tests: `cd gridlytics-backend/backend && ../.scratch/venv/Scripts/python.exe -m pytest tests -q -n 4`. They take about 2 minutes.
   - 7 tests are skipped on purpose: they were lifted from Solarlytics and assert its own data (the list is in `tests/conftest.py`).
   - About 20 "Mean of empty slice" warnings are expected (numpy on days without data).

### Web app

1. Install Node 22 (`.nvmrc` says 22.13.0).
2. Install and check:

   ```bash
   cd gridlytics-frontend/frontend
   npm ci
   npm run gates          # typecheck, tests (vitest run) and build
   ```

   `npm test` runs vitest once (it is already `vitest run`); `npm run build` type-checks again.

### Seed the data folder and run the app

The app keeps each company in SQLite files under a data folder. There are two:

| Folder | Ports (API / web) | Use |
|---|---|---|
| `gridlytics-backend/data` | 8051 / 5195 | The owner's folder. Never delete or reseed it once it is in use. |
| `gridlytics-backend/.scratch/data` | 8151 / 5295 | Scratch, for you. Reseed it freely. |

Seed a scratch folder with the demo accounts and four demo sites (14 days of history; it takes a few minutes, so run it on an otherwise idle
machine, see trap 1 in section 9):

```bash
cd gridlytics-backend/backend
DATA="$(cd ..; pwd -W 2>/dev/null || pwd)/.scratch/data"    # pwd -W gives a Windows path (C:/...) in Git Bash; Python cannot read /c/...
mkdir -p "$DATA"
GRIDLYTICS_DATA_DIR="$DATA" ../.scratch/venv/Scripts/python.exe -m gridlytics.seed_demo --demo-days 14 --out "$DATA/DEMO_ACCOUNTS.local.md"
```

The seed writes to the data folder (all git-ignored; never paste their contents anywhere):
- the account passwords, and the founder console's login and code secret, to `DEMO_ACCOUNTS.local.md`;
- the founder console's settings to `.env.founder`;
- the platform's signing secret to `platform.secret`.

Start the API and the web app together, from the frontend repository:

```bash
cd gridlytics-frontend
../gridlytics-backend/.scratch/venv/Scripts/python.exe scripts/start_local.py --scratch
```

Open `http://127.0.0.1:5295` and sign in as `owner@onyxleaf.demo` (or operator, analyst or viewer `@onyxleaf.demo`) with the password from the
accounts file. The founder console is at `/admin`. Without `--scratch` the launcher uses the owner's folder and ports.

The launcher:
- runs the scheduler (the 15-minute tick and the nightly jobs);
- starts its children without console windows;
- refuses a second server on the same data folder, using a lock file `gridlytics-data-<hash>.lock` in the system temp folder;
- runs in the foreground until Ctrl+C, which stops both servers. If you start it in the background, stop the whole process tree (on Windows
  `taskkill /T /F /PID <launcher pid>`), or uvicorn and Vite keep running;
- takes other ports from `GRIDLYTICS_API_PORT` and `GRIDLYTICS_WEB_PORT`. It refuses a busy port, and with `--scratch` it refuses the owner's 8051
  and 5195. The browser check's `--web` must then match.

### Calling the API from a script

`POST /api/local/session` with `{"email": ..., "password": ...}` answers an `access_token`. Send it as `Authorization: Bearer <token>`. For
example, with httpx:

```python
import httpx
c = httpx.Client(base_url="http://127.0.0.1:8151")
tok = c.post("/api/local/session", json={"email": "owner@onyxleaf.demo", "password": PASSWORD}).json()["access_token"]
print(c.get("/api/fleet", headers={"Authorization": f"Bearer {tok}"}).json())
```

`GET /api/health` needs no sign-in.

### The browser check

With the scratch app running:

```bash
cd gridlytics-frontend
node scripts/e2e/pages.mjs --web 5295 --roles owner,operator,analyst,viewer --phone --founder --accounts ../gridlytics-backend/.scratch/data/DEMO_ACCOUNTS.local.md
```

It signs in through the real page, opens every page in every role, and reports page errors, 5xx answers and any 4xx refusals. A viewer's 403s on
money routes are by design. It refuses the owner's ports.

## 4. How the backend is built

The package is flat: `backend/gridlytics/*.py`. The README's table names every module. The ones you will touch most:

| Module | Role |
|---|---|
| `api.py` | FastAPI app and request guard: allowed hosts and origins, JSON only, body limit, rate limits, site-bound API keys (`glk_...`). A key on another site's path gets 403 `wrong_site`. Every route also answers under `/api/v1`. |
| `store.py` | One SQLite workspace file per company (WAL), selected per request by a ContextVar (`store.use_company(path)`), and migrations with a copy taken first. |
| `telemetry.py` | A second SQLite file per company (`<stem>-telemetry.db`): 1-minute rows (`minute`), forecast issues as JSON, sequence numbers for edge idempotency. |
| `sites.py` | Sites, connections, assets, carried state (`put_state` is one write transaction), overrides (hold, pause, force charge...) and native-settings captures. |
| `tariffs.py`, `tariff_data.py` | Versioned tariffs, the 13-step 1-minute bill (`rebuild`), and the optimiser's price vectors (`vectors`). |
| `forecast.py` | PV (clear sky with a fitted performance ratio), load (equivalent-day persistence with censoring of shed minutes) and outage (schedule adherence and an hourly hazard). |
| `optimise.py` | The MILP on `scipy.optimize.milp` (HiGHS) at T = 192 steps of 15 minutes: reserve as an expected value, genset unit commitment, MDI blocks, ICP cases, wear and calendar cost, terminal value. `explain()` gives a reason per step. |
| `plans.py` | The tick: build the problem, solve, store the plan, supersede the last one, and produce the signed message. |
| `delivery.py` | The Mode A compiler per brand grammar (deye, huawei, growatt, sungrow, voltronic, solis, other), Mode B, Ed25519 signing, and the ems-edge/1.0 message models. |
| `twin.py` | The digital twin: a plant model with the inverter's native logic, outages and genset, simulated per minute. It drives the demo and the savings replays. |
| `verify.py` | Savings. The actual arm is priced per minute against a native replay and a tuned static table, giving a ledger by lever (peak window, off-peak, export, genset, wear, carry-over, MDI), switchback and bill rebuild. |
| `alerts.py`, `reports.py`, `demo.py`, `scheduler.py` | Alerts and genset requests; ReportLab PDF and CSV reports; the four demo sites; the APScheduler jobs. |
| `routes_*.py` | One file per area. `routes_common.py` holds the helpers, and `out()` applies the money filter. |

Conventions:
- **Time:** UTC epoch seconds everywhere. The UI shows Pakistan time (UTC+5, no daylight saving).
- **Roles:** admin, operator, analyst, viewer. Checks go through `auth.need(perm, doing)`, `auth.can`, `auth.actor()`; `GET /api/meta` gives the
  permission map to the web app.
- **Money:** `money.for_reader` strips every money field (keys in `MONEY_KEYS`, suffixes `_rs`, `_rs_per_kwh`, `_rs_per_kw`, `_pkr`) for a viewer,
  and for every API key.
- **Strict models:** request models forbid unknown fields. A refusal is a `Refusal(status, sentence, code)` with a one-sentence message the page shows.
- **Deviations from the plan** go in `docs/DESIGN_LOG.md`, newest first. Read it before changing the engine: it records every rule settled so far and
  the accepted limits.
- **Tests:** `test_engine.py` checks the engine's invariants (the bill engine and optimiser agree on a kWh's price, solver invariants, signing,
  compiler grammar). `test_api_walk.py` walks every page's routes in four roles. `test_fix_*.py`, `test_fix_r2_*.py` and `test_fix_r3_*.py` hold one
  regression test per review finding, so read them to see how a past defect was reproduced. Tests that need a company use `tmp_path`, set
  `GRIDLYTICS_DATA_DIR` and use `store.use_company(...)`.

## 5. How the web app is built

React 19, TypeScript, Vite, Tailwind 4, TanStack Query, zustand, Radix, ECharts. Code is in `frontend/src`:
- `features/<area>` holds the pages.
- `lib/api` holds the typed client and query keys.
- `lib/format.ts` holds the number rules ("Rs 1,234,567", kW with one decimal under 100).
- `lib/time.ts` holds Pakistan time.

Docs (in `gridlytics-frontend/docs`):
- `HANDOFF.md`: the web app's state and next work.
- `API_CONTRACT.md`: each screen's routes and fields.
- `BACKEND_ASKS.md`: what the web app still wants from the API.
- `GATES.md`: the checks and their last results.
- `BUILD_NOTES.md`: the build log, with its traps.

Rules the pages keep:
- Never write "optimal", "guaranteed" or "maximum savings".
- Every site page carries a last-data stamp.
- Role differences are said in words ("Hidden for your role").
- The server is the authority: a page hides what `GET /api/meta` says a role cannot do.

## 6. How the work was done, and where the history is

1. A planning run wrote the plan, a handoff and the tracker (section 11).
2. The backend was built first, then the web app.
3. An adversarial review then found 69 defects in the backend, 13 of them high severity. They were fixed in three rounds (commits `507a4db`,
   `32eafe5`, `7cf7861`), each fix checked by a separate verifier. The rules settled in those rounds are in `docs/DESIGN_LOG.md`.
4. A review of the web app found 20 bugs, fixed in `3e6465b`.
5. An end-to-end walk on a fresh demo then showed the fleet losing money. The cause was three plans the slot table could not carry out, fixed in
   `fc49e3a` (see DESIGN_LOG, "The demo's negative savings").

The review's raw finding lists and verifier notes are in `.scratch/`, which git ignores, so you may not have them. You do not need them: the open
items are all below.

## 7. Rules for anyone continuing

- Never delete, reseed or write test data into `gridlytics-backend/data` (the owner's folder). Use `.scratch/data` or a temporary folder.
- Work on your own branch off `v1-build` and open a pull request (section 2, "How changes come in"). Never push to `main` or `v1-build`, and
  never merge your own pull request. Agent commits in the history end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- **Clean-room code:** no AGPL or GPL code (Predbat and the AGPL Huawei library were deliberately not used). The constructions taken from MIT or
  Apache projects (evcc-io/optimizer, EMHASS, REopt.jl) are named in `THIRD_PARTY_NOTICES.md`. Code lifted from the owner's own Solarlytics
  repositories is marked there too.
- **Secrets:** never print or commit a password, token or key. The accounts file and `.env.founder` are git-ignored.
- **Test data:** use the demo twin's generated data, or free public data saved as fixtures with their source and licence (for example PVGIS, NASA
  POWER or the Open-Meteo archive). Do not wait for real site data.

## 8. Open work, in priority order

Each item says where to start and how to know it is done. Section 12 lists what was done after this page was first written.

### A. Engine and savings defects (known, measured)

Round 4 (`2b0fce1`) closed the earlier items 2 and 3, the charge-slot half of 4, and made 5 visible; round 5 (`012cb4f`, `f973b3b`) closed the
farm's genset item. See DESIGN_LOG "Round 4" and "Round 5". What is left:

1. **Off-grid per-day savings are noisy (round 5 finding).** At the off-grid demo farm, the per-day ledger differs from a continuous 11-day
   replay by -6.2k to +7.9k Rs across seeds. The carry-over lever swings by tens of thousands of rupees and largely offsets the genset lever.
   - Start in `verify.compute_day` (`carry_over`, `v_end`).
   - Option: price carry-over at what the native arm actually saves the next day. Replay that day natively from both end states, holding the
     day as provisional until then.
   - Option: score off-grid days in multi-day blocks.
   - Done when the per-day ledger tracks the continuous replay within about Rs 1k over 11 days on paired farm rebuilds, for several seeds.
     Customers see the per-day ledger, so this comes before a pilot at an off-grid site.
2. **The 2-slot compile, properly.** The plan asks for boundaries ranked by a twin-priced value. v1 ranks them by energy moved, with a rule that a
   non-discharging run folds into a non-discharging neighbour (`delivery.compile_mode_a`).
3. **Plan quality still depends on CPU load:** the limits are wall-clock time. Plans now record `time_limited` and `mip_gap`, and
   `plans.time_limited_recent` counts them. A deterministic work limit, or a re-solve when the machine is idle, would remove the dependence.
4. **Forecast blend (later):** on the public data, blending yesterday's clearness 50/50 with the 14-day median beats the median by 4-9 points. Do it
   once the twin's clearness has day-to-day memory, or on real data only.

### B. Features the API has but the pages do not use yet

Round 4 wired all five earlier items: resume a draft, remove a connection, the connection idempotency key, the tier sign-off and the bill's
printed reading dates, plus a fuel price's source.

Round 5 shows the newer fields:
- the "genset data missing" withheld claim;
- the plan's time-limit and controller-repair notes;
- a charge slot's end time;
- discarding a draft.

One ask has no route: an active site cannot be set back to observing.

### C. Features not built (tracker items `[ ]` or `[~]`)

- **Tariff editor (UI-03):** versions are drafted as raw JSON. The plan wants a lettered-tab editor with a heatmap preview and "simulate on a site".
  The backend needs per-kind body schemas or a validate endpoint, reject or withdraw of a pending version, and a diff.
- **Report schedules (UI-14);** **alert delivery** by email, SMS or WhatsApp (channels are stored, nothing sends; PLT-11 is a later item).
- **Forecasting:**
  - Weather feed (FOR-02): Open-Meteo, which the owner has to license for commercial use.
  - Stage 2 models (FOR-05, FOR-07).
- **Verification:**
  - Backtest and verification harnesses (VER-05, VER-06, VER-07).
  - The real-data check (VER-14), using public data until a partner site's month exists.
  - The metering factor (VER-13).
- **Deployment:** CI (DEP-01), performance gates (DEP-02), security review (DEP-03), release build (DEP-04), observability (DEP-05), Docker
  (DEP-06), backups (DEP-07).
- **Data and docs:** the Parquet export (DAT-07), and the documentation set (DOC-02, DOC-04 to DOC-07).
- The full list, with acceptance tests, is the tracker `EMS_BUILD_PROGRESS.md` (section 11): `[ ]` not started, `[~]` built in a v1 form short of
  its full acceptance.

### D. Owner tasks (not code)

- **GitHub:** create the repositories, push `v1-build`, set up CI tokens and a Linux host.
- **Data and agreements:** inverter brands and native settings per partner site; a month of real 1-minute data with its bill; the fee and VoLL per
  site; the lawyer's wording for savings claims.
- **Owner's data folder:** `gridlytics-backend/data` still holds the demo built on 6 November by the code from before the review, so its savings
  figures are wrong. The demo can be reloaded from the web app (Fleet, demo sites), or by an agent once the owner agrees.

## 9. Traps already met

1. **Wall-clock solve limits:** a demo seeded while the test suite ran in parallel made visibly worse plans. Seed and benchmark on an idle machine.
   The demo history re-plans every 3 hours, so a 14-day seed takes about 10 minutes on the build machine; the web app's default 21 days takes
   about 15. `GRIDLYTICS_DEMO_REPLAN_HOURS=6` (or 24) is faster, but makes plans worse.
2. **Git Bash path conversion:** Git Bash rewrites arguments that start with `/`. Set `MSYS_NO_PATHCONV=1` when passing such paths to scripts.
3. **Line endings:** files must be LF (`.gitattributes` says `* text=auto eol=lf`). Windows tools may write CRLF; git normalises it on commit.
4. **No console windows on Windows:** start background servers without a console (the launcher uses CREATE_NO_WINDOW; for Python use `pythonw`).
5. **SQLite concurrency:** WAL, and every read-modify-write of site state goes through one `BEGIN IMMEDIATE` transaction (`sites.put_state`,
   `routes_common.state_change`). A new company's telemetry file is created under a process lock.
6. **Timing-sensitive tests:** a few optimiser tests time a farm solve. They pass on an idle machine and under `-n 4`, but can flake on a slow one.
   Re-run before chasing them.
7. **Password parsing:** the accounts file is a Markdown table; read the cell after the email, not a fixed column.
8. **Preview tools:** `gridlytics-frontend/.claude/launch.json` (tracked) starts the scratch launcher for Claude Code's preview pane. The build
   machine also has `C:\Users\SaadTahir\Videos\.claude\launch.json` with absolute paths, which is machine-local and in neither repository.
9. **The accounts file holds the founder's secrets too.** Printing the file to look at its layout prints them. Read only the cell you need.

## 10. A first hour for a new developer

1. Set up both repositories (section 3) and run both test suites.
2. Seed `.scratch/data` and start the scratch app. Sign in as the owner and look at Fleet, then one site's Overview, Plan and Savings.
3. Read `docs/DESIGN_LOG.md`, the newest three entries.
4. Pick an item from section 8. Add a regression test that fails first, fix it, run both suites, commit on `v1-build`, and add a line to the
   DESIGN_LOG if you settled a rule.

## 11. The requirements and the plan (`docs/plan/`)

Copied into the repository on 8 November 2026. `docs/plan/README.md` says what each file is and the order to read them in.

| Folder | What |
|---|---|
| `docs/plan/requirements/` | The original requirements: the Product 1 brief (TOU-aware energy management and dispatch) and its deep review, as `.md`, `.pdf` and `.docx`, plus the Product 2 brief (the on-site edge controller) and its review. |
| `docs/plan/build-plan/` | `EMS_BUILD_PLAN.md` (the full specification), `EMS_BUILD_PROGRESS.md` (the tracker: every item, its acceptance test and status, and a dated session log), the planning run's handoff and status. |
| `docs/plan/research/`, `docs/plan/review/` | The research the plan cites (R1-R9, U1-U3) and the four reviews the plan passed. |

The owner's planning folder `C:\Users\SaadTahir\Videos\solarytics\` holds the originals, and the tracker there is the live copy. Where the plan and
`docs/DESIGN_LOG.md` disagree, the DESIGN_LOG is current.

## 12. Log of work after this page was written

| Date | What | Commits |
|---|---|---|
| 2026-11-07 | Handoff written | `eb74527` |
| 2026-11-07 | Handoff checked by a fresh agent from a clean clone; ten gaps closed | `a90ea58` |
| 2026-11-07 | Round 4 done: the PV model checked on PVGIS and NASA POWER data; savings over blank genset columns; threshold-genset repair; charge-slot end; time-limited plans recorded; web app wiring of 8.B. Backend 474 tests, web app 247 | `2b0fce1`, `d7396fd` |
| 2026-11-07 | Small API asks met: DELETE /api/drafts/:id, tier_signoff on POST and PUT site answers, Idempotency-Key allowed by CORS | `57b3d10` |
| 2026-11-07 | Round 5 done: the genset's minimum-load surplus charges the battery in the plan (planned shed at the farm about 0, rescue starts about halved, solve times back near their old level); the off-grid terminal value uses the ledger's rule; the demo re-plans every 3 hours; the web app shows round 4's new fields. Backend 495 tests, web app 257, browser check 405 visits with 0 failures. Scratch demo month: Rs 18,739 | `012cb4f`, `f973b3b`, `54a00d8` |
