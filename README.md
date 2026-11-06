# gridlytics-backend

The API and engine of Gridlytics (working name), OnyxLeaf's TOU-aware, outage-aware and genset-aware energy dispatch optimiser for Pakistani commercial
and industrial sites. The plan is `C:\Users\SaadTahir\Videos\solarytics\EMS_BUILD_PLAN.md`; the web app is the sibling repository `gridlytics-frontend`.

## What is here

| Module | What it does | Plan |
|---|---|---|
| `api.py`, `schemas.py`, `routes_*.py` | FastAPI app, the request guard (hosts, origins, JSON-only, body limit, rate limits, site-bound API keys), every route under `/api` and `/api/v1` | Part 2.8, 9.3 |
| `identity`, `platform`, `companies`, `routes_local`, `founder`, `routes_founder`, `store_founder`, `store_members`, `auth`, `apikeys`, `mfa`, `backups`, `jobs`, `version`, `seed_demo` | Sign-in, tenancy and the founder console, lifted from solarlytics-backend at c853130 (see `docs/DESIGN_LOG.md`) | Part 10.4 |
| `store.py` | The company workspace file (SQLite, WAL, copy before migration) and the company context | Part 3.2 |
| `telemetry.py` | The company telemetry file: 1-minute rows, forecast issues, ingest, CSV import and export, aggregates | Part 3.3, 7.5 |
| `sites.py` | Sites, connections, assets, carried state, overrides, immutable native-settings captures | Part 3.2, 4.8, 8.1 |
| `tariff_data.py`, `tariffs.py` | Versioned, sourced tariff objects (SRO 279 of 2026 and the rest), the 13-step 1-minute bill engine, the optimiser's price vectors on the bearable-cost basis | Part 6 |
| `solar.py`, `twin.py` | Clear-sky PV model; the digital twin (plant, inverter native logic, outages, genset) used for demo, replay and the plan guard | Part 7.4 |
| `forecast.py` | PV, load and outage forecasters (stages 0 and 1), scoring | Part 5 |
| `optimise.py` | The MILP on scipy/HiGHS: balance, battery, reserve as expected value, genset unit commitment with carried state, MDI with the open block, the ICP cases, wear and calendar cost, terminal value; reasons per step | Part 4 |
| `battery.py` | Battery state service v1 (estimate, sigma, trust, wear price, alerts) | Part 4.6 |
| `plans.py`, `delivery.py` | The 15-minute tick, plan storage, the Mode A slot compiler per brand grammar, Mode B steps, Ed25519 signing, the ems-edge/1.0 message models | Part 2.4, 7.1-7.3 |
| `verify.py` | Savings: counterfactual replay against the native capture and the tuned table, ledger by lever, perfect-foresight bound, switchback, bill rebuild | Part 8 |
| `alerts.py`, `reports.py`, `demo.py`, `scheduler.py` | Alerts and genset requests; ReportLab PDF and CSV reports; the four demo twin sites; the APScheduler tick and nightly jobs | Part 8.5, 9.6, 10.6 |

## Run it

The Python is the repository's own venv (`.scratch\venv`, made from the conda `ops` interpreter): every command below means it.

```bash
.scratch/venv/Scripts/python.exe -m pip install -r requirements-dev.txt
```

Seed the accounts and the demo company (the passwords go to `demo/DEMO_ACCOUNTS.local.md`, git-ignored; the founder console's settings to `data/.env.founder`):

```bash
cd backend && ../.scratch/venv/Scripts/python.exe -m gridlytics.seed_demo
```

Start everything with the web app's launcher (`gridlytics-frontend/scripts/start_local.py`, ports 8051 and 5195; `--scratch` uses 8151 and 5295 on a scratch
data folder). To run the API alone:

```bash
cd backend && GRIDLYTICS_SCHEDULER=1 ../.scratch/venv/Scripts/python.exe -m uvicorn gridlytics.api:app --host 127.0.0.1 --port 8051
```

`GRIDLYTICS_SCHEDULER=1` turns on the 15-minute tick (forecast, solve, compile, sign) and the nightly jobs (aggregates, savings, forecast scores); the demo
sites' twins advance to the current minute on every tick.

## Tests

```bash
cd backend && ../.scratch/venv/Scripts/python.exe -m pytest tests -q -n 4
```

158 passed and 7 skipped on 6 November 2026. The skipped seven are lifted Solarlytics tests that still assert Solarlytics' own data (`docs/DESIGN_LOG.md`).
`test_engine.py` holds the marginal-price check (the bill engine and the optimiser agree on a kWh's price), the optimiser's invariants and goldens, signing
and the compiler's grammar; `test_api_walk.py` walks every page's endpoints as owner, operator, analyst and viewer on the demo company, checks the viewer
gets no rupees, and that a site-bound edge key reaches only its own site.

## Rules this repository keeps

- Never merge; work on `v1-build`; the owner opens pull requests and pushes. No remote is added by the building agent.
- Never delete or reseed `data\` once it is in use; the tests and scratch runs use their own folders.
- Clean-room: constructions named in `THIRD_PARTY_NOTICES.md`; no AGPL or GPL code (Predbat and the AGPL Huawei library are not used).
