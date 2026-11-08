# R7. Platform stack for the OnyxLeaf EMS: storage, ingestion, jobs, solver, real-time UI, charts, PDF, auth, packaging

Research note for the OnyxLeaf EMS build plan. Written 6 November 2026 by a planning subagent (task R7-platform-stack). Self-contained: a reader who has not seen the brief can use it alone. Nothing in any existing repository was edited, committed, branched, pushed or run; the only code run was the benchmark scripts in `C:/Users/SaadTahir/Videos/solarytics/plan-work/bench/` and throw-away measurement scripts in the session scratchpad, all with the owner's `ops` Python (`C:/Users/SaadTahir/anaconda3/envs/ops/python.exe`, Python 3.12.11), nothing installed.

## 0. Context, what was read, and conventions

**The product.** OnyxLeaf (owner Saad, Pakistan) is planning a TOU-aware, outage-aware, genset-aware energy management and dispatch optimiser (an EMS, "Product 1") for Pakistani commercial and industrial sites that run PV + LFP battery + an unreliable grid + a diesel genset. Shape already decided by the two research documents in `C:/Users/SaadTahir/Videos/solarytics/` (`Perfect-EMS_TOU_Dispatch_Deep_Review.md`, the guideline, and `Product 1 - TOU-Aware Energy Management and Dispatch.md`, the supporting report): a cloud optimiser (MILP, economic MPC, 15-minute steps, 48-hour horizon = 192 steps, re-solved every 15 minutes, HiGHS), forecasters (PV, load, outage probability), a tariff engine (Pakistan TOU categories, net billing versus legacy net metering, the ICP flat incremental price, MDI fixed charges, diesel price), savings verification against the inverter's own mode at 1-minute resolution, and plan delivery first as daily inverter TOU slots (Mode A), later as 15-minute setpoints through the separate edge product (Product 2). Two repositories: a Python FastAPI backend and a React + TypeScript + Vite frontend, in the style of the owner's Boxlytics and Solarlytics.

**The question this note answers.** Which backend and frontend platform combination a small team should use for this SaaS, developed on Windows (conda env `ops`, Python 3.12, Node 22.13.0, no Docker on the laptop) and hosted on a Linux Docker host (like `boxlytics-backend` at `api.onyxleaf.co` behind nginx, the SPA on Vercel). Nine concerns, each with a decision table: (a) 1-minute time-series storage, (b) ingestion, (c) background jobs and the 15-minute scheduler, (d) the solver stack, (e) real-time UI updates, (f) charting, (g) PDF reports, (h) auth, (i) packaging and deployment. Plus the benchmark of the 192-step MILP on `scipy.optimize.milp`, a final recommended combination with pinned versions, and what changes at 1,000 sites.

**Read before writing** (6 November 2026): the guideline's "Build vs reuse" and "Product scope and methodology" sections (lines 141-192: LP first, MILP where needed, HiGHS; the data model at 1 minute; the Victron-style offline fallback); the supporting report's "Open-source codebases" (lines 309-374: the 22-repo table, EMHASS on CVXPY + HiGHS, evcc-io/optimizer on PuLP, FlexMeasures on Pyomo + HiGHS, the licence rules at 365-374), "Methods" (lines 376-437: the formulation sketch) and "Proposed scope and architecture" (lines 438-466: the seven components, Mode A and B); the sibling notes `plan-work/research/U2-backends.md` (how Solarlytics' backend, per-company SQLite, sign-in and founder console are built; sections 1.4-1.7, 2.5, 3.1-3.6, 4.5, 6, 7), `R1-evcc-optimizer.md` (sections 6 and 12: evcc's solver ladder and the 192-step experiment on scipy), `R4-architecture-refs.md` (sections 1.2-1.3 and 4-7: the FlexMeasures RQ job pattern, the API shape, the schema sketch) and `U1-boxlytics-frontend.md` (section 1 and 7.1: the Boxlytics stack and the proposed frontend pins); the manifests of the owner's products, read only: `C:/Users/SaadTahir/Videos/boxlytics/package.json`, `C:/Users/SaadTahir/Videos/solarlytics-backend/requirements.txt`, `Dockerfile`, `docs/DEPLOY.md` (head), `backend/solarlytics/docx.py` (head), `C:/Users/SaadTahir/Videos/boxlytics-backend/requirements.txt`.

**Verification.** Every version below was read from the PyPI JSON API (`https://pypi.org/pypi/<name>/json`) or the npm registry (`https://registry.npmjs.org/<name>`) on 6 November 2026 with a small script; bundle sizes from the bundlephobia API (`https://bundlephobia.com/api/size?package=...`) the same day; licence texts from the projects' own LICENSE files or documentation pages, quoted where it matters. Anything not checked against a primary source is marked UNVERIFIED. Prices are not part of this note.

**The machine the measurements were made on.** 13th Gen Intel Core i7-13650HX (14 cores, 20 logical processors, 2.6 GHz base), 31.7 GB RAM, Windows 11 Business 10.0.26300, Python 3.12.11, scipy 1.17.1 (bundling HiGHS 1.12.0, git 4f96ee8, per the solver's own banner), numpy 2.4.3, SQLite library 3.50.2 (the one inside the `ops` Python's `sqlite3`). A production VPS core is slower than this laptop's performance cores; treat the solve times below as a lower bound and the budget arithmetic as indicative.

**Conventions.** "Point" = one channel value at one minute; "row" = one (site, minute) record holding all channels. "Rs" = Pakistani rupees. Site-local time is Asia/Karachi (UTC+5, no DST). The product's prefix for environment names is written `(PREFIX)_` because the product's name is not decided (U2 section 6, item 1).

## 1. The benchmark: a 192-step dispatch MILP on `scipy.optimize.milp` (HiGHS inside scipy)

### 1.1 What was run

The scripts in `C:/Users/SaadTahir/Videos/solarytics/plan-work/bench/` were left by the cut-off first attempt of this task; they were read, run as they were, and extended by one file:

| File | What it does | Run as |
|---|---|---|
| `milp_bench.py` (237 lines) | Builds the model below as sparse matrices and solves it with `scipy.optimize.milp` at three gap settings, three repeats each, then the LP relaxation and a plan summary | `python -I milp_bench.py --repeat 3` (as given) |
| `milp_bench_variants.py` (69 lines) | The HiGHS log of one solve, then six variants: presolve off, the grid-direction binary relaxed, the start binary relaxed too, a 2-second time limit, a 1 % gap | `python -I -c "import sys,runpy; sys.path.insert(0, r'<bench>'); runpy.run_path(r'<bench>/milp_bench_variants.py', run_name='__main__')"` (the fix: `-I` leaves the script's folder off `sys.path`, so a bare `python -I milp_bench_variants.py` fails on `import milp_bench`; nothing in the scripts needed changing) |
| `milp_bench_seeds.py` (new, 60 lines) | Five input seeds with presolve on and off at the default gap, and a GIL test: one solve in a worker thread while the main thread measures its longest stall | same `runpy` form |

**The model** (from the docstring of `milp_bench.py`, verified against the code): 15-minute steps, 48 hours, T = 192. Per step, continuous: grid import split into base (TOU-priced) and incremental (ICP flat price), grid export, battery charge, battery discharge in two wear segments (cheap up to 0.5C, dearer above: a convex piecewise-linear wear cost), SOC, genset output, PV curtailment, unserved critical load (slack at a value of lost load), genset stop (implied integral). Per step, binary (3): genset on `u`, genset start `s`, grid direction `y` (no simultaneous import and export). Global: the monthly peak (MDI) variable with a lower bound at the peak to date, and one binary for "the ICP baseline is used up" (incremental kWh only above the baseline). Constraints: power balance; SOC dynamics with charge and discharge efficiencies 0.95; SOC and power bounds; import forced to zero in two outage windows (tonight 20:00-22:00, tomorrow 14:00-17:00) through an availability vector; genset minimum load 30 % of 160 kW and rated bound; start and stop logic; minimum up 1 h and down 30 min; a reserve SOC before each forecast outage sized to carry the critical load (45 % of load) through it; peak >= every import; ICP fill-first logic. Objective: TOU import (Rs 48 peak 18:00-22:00, Rs 36 off-peak) + ICP incremental (Rs 22/kWh) - export credit (Rs 10/kWh) + genset fuel (HOMER intercept 0.033 L/h per rated kW and slope 0.273 L/kWh at Rs 280/L) + start cost Rs 1,500 + run-hour cost Rs 400/h + battery wear (Rs 9 and 14/kWh) + MDI charge Rs 1,250/kW on any rise of the monthly peak + Rs 500/kWh unserved + a small curtailment penalty - a terminal value of Rs 30/kWh stored. Equipment: 300 kWp PV, load 140-320 kW with an evening peak, 400 kWh / 200 kW battery (SOC 10-95 %, start 60 %), 400 kW sanctioned import, 200 kW export cap, 200 kVA genset. The tariff numbers are round figures near NEPRA 2025-26 B3 TOU, chosen so that the binaries bind (the genset must run at least once; the peak window cannot be covered by the battery alone).

### 1.2 Results

**Size** (seed 7): 2,690 variables (14 per step x 192 + the peak + the ICP binary), 577 binaries (3 x 192 + 1), 2,683 constraints, 8,434 non-zeros; matrix build 5 ms. After HiGHS presolve: 2,425 rows, 2,320 columns (556 binary, 191 implied integer, 1,573 continuous), 8,873 non-zeros.

**Main run** (`milp_bench.py --repeat 3`, time limit 30 s, single process):

| Gap setting (`mip_rel_gap`) | Wall time min / median / max (s) | Status | Nodes | Reported gap | Objective (Rs, after the MDI constant) |
|---|---|---|---|---|---|
| default (scipy's 1e-4) | 3.479 / 5.387 / 5.533 | 0 optimal | 1 | 0.0 | 198,600 |
| 1e-3 | 5.450 / 5.542 / 5.561 | 0 optimal | 1 | 0.0 | 198,600 |
| 1e-2 | 5.435 / 5.505 / 5.519 | 0 optimal | 1 | 0.0 | 198,600 |
| LP relaxation (no integrality) | 0.036 | 0 | - | - | 154,432 |

The plan found: genset on for 8 steps (2 h) with 1 start, 5,573 kWh imported over the 48 hours (1,573 kWh of it at the ICP price), 0 kWh exported, 0 kWh unserved, monthly peak held at 230.0 kW (the peak to date, so no MDI increase), SOC at the end 279 kWh.

**The HiGHS log of one solve** (variant A, `disp=True`): the root LP takes 0.1 s (853 iterations); a randomised-rounding heuristic gives a first incumbent at 0.1 s with a 19.0 % gap; sub-MIP heuristics ("L" lines) improve it to a 14.4 % gap at 1.5 s and to a 6.6 % gap at 2.0 s; the bound meets the incumbent at 3.5 s and the solve ends with "Nodes 1", "Max sub-MIP depth 6", 9,508 LP iterations of which 7,960 in heuristics, 471 in separation and 12 in strong branching, 1,553 cuts, "Timing 3.47". HiGHS printed `WARNING: Problem has some excessively small column bounds`: the curtailment upper bound equals the PV forecast, which is 1e-14 kW at night; zeroing bounds below 1e-6 (done in `milp_bench_seeds.py`) removes the warning without changing the answer.

**Variants** (`milp_bench_variants.py`, gap 1e-3, 30 s limit, min / median of 3):

| Variant | Binaries | Time (s) | Status | Objective | Note |
|---|---|---|---|---|---|
| A as is | 577 | 5.575 / 5.579 | optimal, 1 node | 198,600 | |
| B presolve off | 577 | 3.975 / 4.028 | optimal, 1 node | 198,600 | faster on this model (see seeds below) |
| C grid-direction `y` relaxed to continuous | 385 | 3.673 / 3.738 | optimal, 1 node | 198,600 | 0 steps import and export together |
| D `y` and start `s` relaxed | 193 | 3.791 / 3.818 | optimal, 1 node | 198,600 | 0 fractional `s` values |
| E as D, time limit 2 s | 193 | 2.025 / 2.026 | 1 (time limit), 0 nodes | 198,600 | the incumbent at 2 s is already the optimum; reported gap 6.96 % because the bound had not closed |
| F as D, gap 1e-2 | 193 | 3.823 / 3.837 | optimal, 1 node | 198,600 | |

**Seeds** (`milp_bench_seeds.py`, default gap 1e-4, 30 s limit, tiny bounds zeroed): presolve on 2.17 / 5.40 / 5.52 / 5.67 / 4.86 s for seeds 0-4 (min / median / max 2.17 / 5.40 / 5.67); presolve off 3.91 / 5.57 / 4.13 / 4.77 / 4.28 s (3.91 / 4.28 / 5.57). Every instance proved optimal at the root node (nodes = 1, gap 0.0); objectives Rs 198,096 to 200,225.

**GIL** (same script): while a 5.5 s solve ran in a worker thread, the main thread's 1 ms sleep loop ran 2,776 iterations with a worst stall of **17 ms**. `scipy.optimize.milp` releases the GIL during the HiGHS call, so a solve in a thread does not freeze an asyncio event loop in the same process. (This decides concern (c).)

### 1.3 Readings

1. **The cost is root-node heuristics, not branching.** Every instance in every variant closed at the root (one node). The LP relaxation takes 36 ms; the 2-6 s are sub-MIP heuristics (84 % of the LP iterations) and cut separation. That is why relaxing two of the three binaries per step (C, D) saves only 1.8 s, and why the gap setting makes no difference: the bound closes to 0 anyway. A 2-second time limit already returned the optimal plan (E) with the proof missing.
2. **Production settings.** `time_limit` 10 s (pilot; 20 s at most), `mip_rel_gap` 1e-3 (Rs 200 on a Rs 200,000 two-day bill is below the forecast noise), accept `status == 1` (time limit) whenever `res.x is not None` and log `mip_gap` and `mip_node_count`; treat `status` 2 (infeasible) as a bug because every hard constraint has a slack (unserved load, curtailment) and should never happen; keep `presolve` at the default (it was slower by a second on four of five seeds here but that is model-specific and presolve is what protects against a badly scaled real-world instance).
3. **Variance.** 2.2-5.7 s across five load and PV seeds on this laptop's performance cores; expect 1.5-2x on a VPS vCPU (UNVERIFIED: not measured on the production host). A budget of 10 s per solve is safe; the 30 s acceptance figure in the Product 1 work packages (WP6) is generous.
4. **Agreement with R1.** R1's prototype (`R1-target-milp-prototype.py`, 3,652 variables, 768 binaries with a monotone ICP exhaustion chain) needed 8-10 s on this machine with the chain and 1-5 s without it. This benchmark keeps one ICP binary (fill-first: base kWh >= baseline x b, incremental <= big-M x b) and lands at 2-6 s. Both say the same thing: compute the ICP exhaustion step in the tariff engine from month-to-date metering and hand the MILP a price vector; keep the binary only for the few days a month when the switch falls inside the horizon.
5. **Budget.** One solve per site per 15 minutes at 5 s single-core means one core serves 180 sites at 100 % utilisation, 90 at a comfortable 50 %. Three pilot sites use under 1 % of one core. See section 13 for 1,000 sites.
6. **What scipy's wrapper cannot do** (from its documentation, read 6 November 2026: "milp is a wrapper of the HiGHS linear optimization software"): the `options` dict takes only `disp`, `node_limit`, `presolve`, `time_limit` and `mip_rel_gap` (default 1e-4); the result carries `x, fun, status, success, message, mip_node_count, mip_dual_bound, mip_gap`. No warm start (the previous plan as an incumbent would cut the heuristic time, which is the whole time), no absolute gap, no thread count, no callbacks for a wall-clock interrupt that keeps the incumbent, no MPS/LP export for debugging. These are the reasons to move to `highspy` later (section 6), not reasons to avoid scipy now.
7. **The direction and start binaries are redundant at these prices** (C and D: identical objectives, no violations), as R1 found with evcc's model: an export credit of Rs 10 against imports of Rs 36 and a 0.9025 round-trip efficiency never reward importing and exporting in one step, and a start variable with a positive cost takes the value max(0, u_t - u_{t-1}) on its own. Keep both as options switched on only when a site's prices or permissions make them bind.

## 2. Sizing arithmetic for 1-minute telemetry, and what was measured on SQLite

### 2.1 Points, rows, bytes

**Channels.** The guideline's minimum data model lists, at 1 minute: PV W, load W, SOC, grid import W, grid export W, grid-present flag, genset run flag and fuel if metered; the supporting report adds battery power, voltage, current, temperature, BMS SoC and SoH. Base case **10 channels** per site, upper case 16. Product 2 will send a normalised set; the number is a Product 2 contract item (U3), not this note's.

**Volume per site.** 1,440 minutes/day x 365 days = **525,600 rows per site-year**; x 10 channels = **5,256,000 points per site-year** (8.4 million with 16 channels).

**Bytes per point, measured** (script `sqlite_bench.py` in the session scratchpad, SQLite 3.50.2 through Python's `sqlite3`, WAL on, `synchronous=NORMAL`, this laptop's NVMe):

| Layout | Rows / points | Insert | File size | Bytes per row | Bytes per point |
|---|---|---|---|---|---|
| Wide, 1 site x 365 days: `telemetry(site_id, ts, c0..c9 REAL, PRIMARY KEY (site_id, ts)) WITHOUT ROWID` | 525,600 rows | 1.9 s (272,000 rows/s, batched by week) | 52.4 MB (46.8 MB after `VACUUM INTO`) | 100 (89 vacuumed) | 10.0 |
| Wide, 10 sites x 90 days | 1,296,000 rows | 5.5 s (234,000 rows/s) | 129.7 MB (115.9 MB vacuumed) | 100 | 10.0 |
| Narrow (one row per point), 1 site x 30 days: `points(site_id, ts, ch, v REAL, PRIMARY KEY (site_id, ch, ts)) WITHOUT ROWID` | 432,000 points | 1.5 s | 9.7 MB | 22.4 | 22.4 |
| Columnar proxy: the same site-year as float32 columns, zlib-compressed `.npz` (a stand-in for Parquet + zstd; pyarrow is not installed in `ops`) | 5,256,000 points | - | 10.2 MB (25.2 MB raw float32) | - | 1.94 |

So: **~50 MB per site-year in SQLite with a wide table (10 B/point), ~2.2x that with a narrow one, and ~10 MB per site-year in a compressed columnar file** (UNVERIFIED for real Parquet; the proxy compresses the same floats with zlib, and Parquet with zstd and dictionary encoding on rounded telemetry usually does as well or better; TimescaleDB's own page claims "90-98% compression in the columnstore").

**Fleet totals per year of 1-minute data (10 channels):**

| Sites | Points per year | SQLite wide | Columnar (proxy) |
|---|---|---|---|
| 3 (pilot) | 15.8 million | 157 MB | 31 MB |
| 10 | 52.6 million | 524 MB | 102 MB |
| 30 | 158 million | 1.6 GB | 0.3 GB |
| 100 | 526 million | 5.2 GB | 1.0 GB |
| 1,000 | 5.26 billion | 52 GB | 10 GB |

Multiply by 1.6 for 16 channels. For comparison, SQLite's documented ceiling is "a maximum database size of approximately 2.8e+14 bytes (281 terabytes)" (sqlite.org/limits.html): the size is never SQLite's problem; the backup copy and the single writer are (section 3).

**Retention with downsampling.** Keep 1-minute rows for 13 months (one full year of savings verification plus a month for bill reconciliation), then keep 15-minute aggregates (avg, min, max per channel, plus the counts the savings engine needs: kWh import and export, genset minutes, grid-absent minutes). That is 1/15 of the rows with about three times the columns, roughly 1/5 of the bytes: **~10 MB per site-year** on SQLite after the first 13 months, or ~2 MB columnar. Five years for 100 sites: 13 months hot (5.7 GB) + 4 years x 100 sites x 10 MB (4 GB) = under 10 GB. Daily aggregates (the savings statement's grain) are a few hundred rows per site-year and live in the workspace file for ever.

### 2.2 The query needs, measured

The product's reads, from the guideline and R8's page inventory: the last 48 hours fast (the live page, the plan-versus-actual chart, every 30-60 s), a day replay (the savings verification re-runs the inverter's own mode over one day of 1-minute data), monthly aggregates (the statement), the 15-minute series for the optimiser's inputs (recent history for the load forecaster), multi-year retention with downsampling, and one-minute ingestion of one row per site per minute.

Measured on the wide SQLite table above (single process, warm cache):

| Query | Rows touched | Time |
|---|---|---|
| Last 48 hours of one site, all channels | 2,880 | 5-7 ms |
| One day replay of one site, four channels | 1,440 | 2 ms |
| 15-minute resample of the last 7 days (`GROUP BY ts/900`) | 672 groups from 10,080 rows | 4-7 ms |
| Daily aggregates over a full site-year (`GROUP BY ts/86400`) | 365 groups from 525,600 rows | 468 ms |
| 30-day aggregates over a site-year | 13 groups from 525,600 rows | 326 ms |
| 60 single-row inserts each in its own transaction (the per-minute ingest pattern) | 60 | 5-7 ms total, 0.1 ms each |
| `VACUUM INTO` backup copy | 47 MB / 116 MB | 0.7 s / 1.7 s (~70 MB/s) |

Reading: every "hot" read is single-digit milliseconds on a (site_id, ts) clustered key; the two aggregate scans are the only slow ones and they are what a nightly roll-up job exists for: compute daily aggregates once per day into a `daily` table in the workspace file (365 rows per site-year), and build monthly figures from daily rows. Never scan a year of 1-minute rows in a request handler. Ingest cost is negligible at any pilot scale: 1,000 sites x 1 committed insert per minute is 17 inserts/s against a measured 10,000/s single-row rate.

**SQLite's concurrency rules that matter** (sqlite.org/wal.html, read 6 November 2026): "WAL provides more concurrency as readers do not block writers and a writer does not block readers"; "since there is only one WAL file, there can only be one writer at a time"; "All processes using a database must be on the same host computer; WAL does not work over a network filesystem"; "By default, SQLite does a checkpoint automatically when the WAL file reaches a threshold size of 1000 pages". One writer at a time per file is fine for one API process per data folder (U2 section 1.4: "One server process per data folder"); it is the reason the telemetry writes must be batched per request and the reason a second writing process (a separate ingest or scheduler process) must not open the same files for writing.

## 3. Concern (a): time-series storage for 1-minute telemetry per site

Facts checked on 6 November 2026 (sources in section 16):

- **SQLite** 3.50.2 in the `ops` Python; public domain; nothing to install on Windows or in the image; the owner's Solarlytics runs one `companies/<uuid>.db` per company in WAL mode with `Connection.backup` copies (U2 section 1.4).
- **PostgreSQL 18 + TimescaleDB 2.30.0.** TimescaleDB's own editions page: "TimescaleDB Apache 2 Edition is available under the Apache 2.0 license"; the Community Edition (hypercore/columnstore compression, continuous aggregates, retention policies, `time_bucket_gapfill`, `locf`, job management) is under the Timescale License: "You can install TimescaleDB Community Edition in your own on-premises or cloud infrastructure and run it for free", "You cannot sell TimescaleDB Community Edition as a service, even if you are the main contributor." Self-hosted installs: Debian, Ubuntu, Red Hat, Fedora, Rocky, Arch, macOS, Docker, and a Windows installer from the GitHub releases page ("To install TimescaleDB on Windows, you need: OpenSSL v3.x"). Compression claim: "90-98% compression in the columnstore". Python drivers: `psycopg` 3.3.8 (LGPL-3.0-only, 2026-11-04) or `asyncpg` 0.32.0 (Apache-2.0, 2026-10-06).
- **DuckDB** 2.0.0 (PyPI 2026-10-21, MIT, Python >= 3.11, cp312 and abi3 Windows wheels, `pip install duckdb` only). Its concurrency page (docs 2.0): in read-write mode "one process can both read and write to the database"; with `access_mode = 'READ_ONLY'` "multiple processes can read from the database, but no processes can write"; multi-process writing only "through the Quack remote protocol" (client-server) or DuckLake with a PostgreSQL catalog. **Parquet** through `pyarrow` 26.0.1 (Apache-2.0, 2026-11-03, cp312 Windows wheel) or `polars` 2.1.0 (MIT, 2026-10-25); `fastparquet` 2026.9.0 (Apache-2.0) as the lighter writer. None of these is in the `ops` env today.
- **QuestDB** 9.3.2: core Apache-2.0 (LICENSE.txt is the Apache 2.0 text; enterprise features are a separate product); a Java server, shipped with a trimmed JVM ("rt" package, `questdb-9.3.2-rt-windows-x86-64.tar.gz`, runs as a Windows service with `questdb.exe install`); ingestion over its ILP protocol with the `questdb` Python client 6.0.1 (Apache-2.0); SQL over PostgreSQL wire (port 8812) and a web console (9000).
- **InfluxDB 3 Core** 3.12: dual MIT / Apache-2.0 per InfluxData's announcement; Parquet storage; but "Core limits query time ranges to approximately 72 hours (both recent and historical)" (file-count based, default 432 files; the docs say "approximately"); historical queries beyond that need the commercial Enterprise edition. Python client `influxdb3-python` 0.21.0 (MIT).

### 3.1 Decision table (a)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **SQLite per company, as Solarlytics (telemetry in the same `companies/<id>.db`)** | Zero new infrastructure; the lifted `use_company` machinery, migrations and daily backups work unchanged; 10 B/point measured; every hot read 2-7 ms; Windows dev is a file | The workspace file grows by 50 MB per site-year, so the daily `Connection.backup` copy of every company file grows with telemetry (U2's blocker for `backups.daily_all`); a schema migration of the workspace rewrites a multi-GB file; one writer per file means ingest, scheduler and users all go through the one API process | Not as is: split the telemetry out of the workspace file |
| **SQLite per company, telemetry in a second file `companies/<id>-telemetry.db` (WAL, wide table, `WITHOUT ROWID`, keyed `(site_id, ts)`), aggregates in the workspace file** | Everything above, and the workspace file stays small and quick to copy and migrate; the telemetry file has one table and never migrates (new channels are new nullable columns); the hot 13 months per company is 50 MB per site-year; `VACUUM INTO` at ~70 MB/s copies a 1.5 GB (30-site) file in ~20 s; same `use_company` ContextVar pattern with a second path (U2 section 6 item 2 recommends exactly this) | Still one writer per file and one process per data folder; aggregates over a year of 1-minute rows take 0.3-0.5 s per site, so roll-ups must be pre-computed nightly; no compression (10 B/point against ~2 columnar); cross-company fleet analytics means opening many files (fine for the founder console's counts, not for ad-hoc SQL across the fleet) | **Yes, for the pilot and up to roughly 30-100 sites** |
| **PostgreSQL 18 + TimescaleDB 2.30 Community** | Hypertables, columnstore compression (claimed 90-98 %), continuous aggregates and retention policies do the downsampling and roll-ups in the database; many writers and readers; one SQL across the fleet; the FlexMeasures reference schema (R4 A2) is Postgres; the right end state at 1,000 sites | A server process to run and back up (pg_dump or base backups, not file copies); on the laptop either the EDB PostgreSQL installer plus the TimescaleDB Windows installer (OpenSSL 3) or Docker, which the laptop lacks; breaks the owner's per-company-file tenancy (tenancy becomes a `company_id` column plus RLS or careful queries), so the lifted sign-in module's `use_company` path and the founder console's file counts would be rewritten; the Community features are TSL (self-hosting allowed, "cannot sell ... as a service": the EMS is not selling the database as a service, so allowed, but it is not OSI open source); `psycopg` is LGPL-3.0 (dynamic use is fine; the product's own rules keep LGPL out of shipped code, so prefer `asyncpg`, Apache-2.0) | Not now; the 1,000-site path (section 13) |
| **DuckDB + Parquet files per site-month** | Columnar, ~2 B/point, analytics over years in seconds, reads Parquet partitions directly (`read_parquet('sites/*/2026-*.parquet')`), pip-only on Windows, MIT and Apache-2.0; Parquet is the archive format every other tool reads (including InfluxDB 3 and Timescale's export) | Single-writer per DuckDB file and per process; Parquet files are immutable, so the live minute-by-minute ingest needs a hot buffer anyway (SQLite or in-memory) and a periodic flush; late or corrected rows mean rewriting a file; no row-level updates for "outage flag corrected after the fact" without rewrite; two storage engines to test | **Yes as the cold archive and the analytics layer, later**: write a Parquet file per site-month from the SQLite telemetry file in the nightly job once the month is closed (and keep 13 months hot in SQLite); DuckDB for the data scientist's replays and for the fleet-wide studies; not needed in the first build |
| **QuestDB 9.3** | Very fast ILP ingest and time-series SQL (`SAMPLE BY`), Apache-2.0 core, Windows service with bundled JVM, Postgres-wire reads | A JVM server to run (memory and ops cost), a second query dialect, its own backup procedure, no per-company isolation, enterprise features (replication, RBAC) are commercial; nobody on the team knows it; overkill below millions of points per second | No |
| **InfluxDB 3 Core 3.12** | MIT/Apache-2.0, Parquet files, fast last-value queries, a Windows binary | "query time ranges to approximately 72 hours": the monthly statement, the day replay from last month and the multi-year retention are exactly what Core cannot query without the commercial Enterprise edition; a second query language (SQL or InfluxQL) and server | No |

**Decision (a).** Per-company SQLite with the telemetry in a second file per company, the daily and 15-minute aggregates in the workspace file, written by a nightly roll-up job; Parquet archive per site-month after 13 months (pyarrow) read by DuckDB when analytics need it; move to PostgreSQL + TimescaleDB only when a second writing process or fleet-wide SQL is needed (section 13). The schema:

```sql
-- companies/<id>-telemetry.db (WAL; one table; never migrated, only widened)
CREATE TABLE telemetry (
  site_id INTEGER NOT NULL, ts INTEGER NOT NULL,           -- ts = unix seconds, minute-aligned, UTC
  pv_w REAL, load_w REAL, grid_w REAL, batt_w REAL, soc REAL, grid_ok INTEGER, gen_on INTEGER,
  gen_w REAL, fuel_l REAL, v REAL, i REAL, temp_c REAL, soh REAL, quality INTEGER,
  PRIMARY KEY (site_id, ts)
) WITHOUT ROWID;
-- companies/<id>.db (the workspace): sites, devices, tariffs, plans, plan_steps, runs, events, daily, q15 (15-minute aggregates), reports ...
```

Ingest writes are `INSERT OR REPLACE` per minute row (idempotent on `(site_id, ts)`, so a resend from the edge is harmless, R4 A14); the nightly job writes `daily` and `q15` for the closed day and deletes 1-minute rows older than 13 months after the Parquet file of that month has been written and checked. The two files share the `use_company` ContextVar (a second path, `db_path_telemetry()`), the same `backup(tag)` (copies both), and the same restore script.

**Windows-dev friendliness, summarised.** SQLite and DuckDB/Parquet: nothing to install beyond pip wheels (cp312 win_amd64 wheels exist for duckdb 2.0.0, pyarrow 26.0.1, polars 2.1.0; SQLite is in the standard library). PostgreSQL + TimescaleDB: two installers and a Windows service, or Docker (absent); workable but a step the owner must take on every development machine. QuestDB: a service with a bundled JVM. InfluxDB 3 Core: a binary. The per-company-file design also keeps the owner's habit of copying a company's files for a bug report or a demo seed.

## 4. Concern (b): ingestion (HTTP batch POST and CSV upload now, MQTT later)

Facts checked on 6 November 2026:

- **Mosquitto** 2.1.2: "This project is dual licensed under the Eclipse Public License 2.0 OR the Eclipse Distribution License 1.0" (`SPDX-License-Identifier: EPL-2.0 OR BSD-3-Clause`); downloads include `mosquitto-2.1.2-install-windows-x64.exe`, Debian and Ubuntu packages, snap, Homebrew; a Docker image exists (`eclipse-mosquitto` on Docker Hub, not re-checked this run: UNVERIFIED).
- **EMQX** 6.x: `LICENSE` is the Business Source License 1.1 for "EMQX Version 5.9.0 or later", licensor "EMQ Technologies Incorporated", Additional Use Grant "You may make production use of a single node of the Licensed Work, provided your use does not include offering the Licensed Work to third parties on a hosted or embedded basis", Change Date "Four years from the date the Licensed Work is published", Change License "Apache License, Version 2.0". Supported platforms on its install page: Ubuntu 22.04/24.04, Debian 11-13, Rocky 8-10, Amazon Linux 2023, macOS 14-26 (arm64), Docker; **no Windows**.
- Python clients: `paho-mqtt` 2.2.0 (2026-11-04, "EPL-2.0 OR BSD-3-Clause", pure wheel) and `aiomqtt` 2.5.1 (2026-03-05, BSD, asyncio wrapper over paho). `python-multipart` 0.0.33 (Apache-2.0) is what FastAPI uses for file uploads.
- The FlexMeasures ingest shape (R4 sections 1.3 and 4, A5): `POST /sensors/<id>/data` with `{"values": [...], "start": ISO, "duration": "PT1H", "unit": "kW"}`, payload capped at 3 MiB, 202 when queued and 200 when processed synchronously; the OpenEMS edge pattern (R4 A14): per-device API key plus instance id, 1-minute live set, 5-minute aggregates with a random phase, resend by last-acknowledged timestamp in bounded chunks, setpoint TTL.

### 4.1 Decision table (b)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **HTTP batch POST from the edge (JSON, one request per site per minute carrying 1-60+ minute rows), API key per site** | Works through any NAT or mobile link with nothing but outbound HTTPS; FastAPI + pydantic validate the rows; idempotent on `(site_id, ts)` so resends are safe; the same route serves Product 2, an OEM-cloud poller and a Raspberry Pi script; measured write cost 0.1 ms per row; nginx and TLS already in place; testable with `httpx` | Polling-style latency (the edge chooses when to send; 1 minute is the design grain anyway); the API's guard reads `platform.db` per request (U2 risk) unless the key-to-company lookup is cached ~60 s for the ingest route; no server-to-edge push on the same channel (the edge polls `GET /sites/{id}/plans/current` on a schedule, which Mode A does once a day) | **Yes, now** |
| **CSV upload (multipart) for onboarding and backfill** | The four weeks of history every pilot site needs before the optimiser runs (Product 1 report, pilot design) come from inverter-cloud exports and Enerlytics-type CSVs; one route, column mapping per device template, 50 MB cap, a dry-run that reports the parsed range and gaps | Column dialects per vendor; time zones and DST-less local time to normalise; a 50 MB CSV of 1-minute rows is a 30-second parse that must run as a job, not inline | **Yes, now** (as a job; section 5) |
| **MQTT with Mosquitto (TLS, per-device username and password or client certificate), the API process subscribing** | Push with QoS 1 and persistent sessions: the edge publishes as it goes, the broker buffers while the API restarts; one broker serves thousands of devices on a small VPS; EPL/EDL licence is clean; a Windows installer for development; `aiomqtt` runs as a task in the API's lifespan so the single writer stays the API process | Another service to run, monitor and back up (its persistence file); device credential management; the edge needs an MQTT client (Product 2 has one in its plan; a Deye-slot writer over an OEM cloud does not); ordering and dedup must still be done on `(site_id, ts)` | **Later**, when Product 2's edge ships or when >50 devices push live data |
| **MQTT with EMQX** | Clustering, dashboard, rule engine, MQTT 5 features, auth backends | BSL 1.1: a single node is allowed in production unless "offering the Licensed Work to third parties on a hosted ... basis" (the EMS exposes a broker to customers' devices; whether that is "offering EMQX" is a legal reading, UNVERIFIED); no Windows package; much heavier than the team needs | No |
| **WebSocket from the edge to a gateway (OpenEMS pattern)** | Bidirectional on one connection; plan push and telemetry on the same socket | A gateway process with connection state to run, and a second writer or an internal hand-off to the API; the OpenEMS design is for a thick Java edge | Not for Product 1; a Product 2 decision (U3) |

**Decision (b).** HTTP batch POST (`POST /api/sites/{id}/telemetry`, body `{"rows": [{"ts": ..., "pv_w": ..., ...}, ...]}` or the compact `values/start/duration/unit` form per channel, both accepted; 200 with `{"accepted": n, "replaced": m, "last_ts": ...}`; 413 above 1 MiB; API key scoped `telemetry:write` for one site) and CSV upload (`POST /api/sites/{id}/imports` returning a job id) in the first build; Mosquitto with `aiomqtt` inside the API process later, behind the same validation and the same `INSERT OR REPLACE`. The edge contract (ack by last timestamp, chunked resend, TTL on plans) follows R4 A14 and U3.

## 5. Concern (c): background jobs and the 15-minute scheduler

Facts checked on 6 November 2026:

- **APScheduler** 3.11.3 (2026-06-28, MIT, pure wheel, Python >= 3.8); the 4.0 line is still pre-release (4.0.0a6, 2025-04-27, nothing since). Job stores: memory and SQLAlchemy; integrates with asyncio.
- **RQ** 3.0.0 (2026-10-28, BSD-2-Clause): "SpawnWorker now uses os.posix_spawn() and requires POSIX", "RQ now requires redis-py >= 5.0.1", "CronScheduler now evaluates cron strings in local timezone"; its workers page: "SimpleWorker: A worker implementation that executes jobs in the same process, without using fork()", "It requires POSIX; use SimpleWorker on Windows". `rq-win` 0.4.2 (2025-06-09) is the old Windows shim. FlexMeasures uses RQ (R4 section 1.2).
- **Redis** 8: "Starting with Redis 8, Redis Open Source is moving to a tri-licensing model" with "the Redis Source Available License v2 (RSALv2); or (b) the Server Side Public License v1 (SSPLv1); or (c) the GNU Affero General Public License v3 (AGPLv3)"; Redis 7.2 and earlier stay BSD-3. **Valkey** (the Linux Foundation fork): "BSD 3-Clause License"; Python client `valkey` 6.2.0 (MIT), and `redis-py` 8.1.0 (MIT) talks to it as well (wire-compatible; UNVERIFIED by a test here).
- **Celery** 5.7.0 (2026-11-05, BSD-3-Clause); its FAQ: "Does Celery support Windows? No. Since Celery 4.x, Windows is no longer supported due to lack of resources."
- **dramatiq** 2.2.1 (2026-09-02, **LGPL-3.0-or-later** per its classifier); Python >= 3.10; RabbitMQ or Redis; runs on Windows with spawn (`--use-spawn`), `--watch` is "Supported only on UNIX".
- **huey** 3.5.4 (2026-11-01; licence not stated on PyPI; its repository LICENSE is the MIT text, "Copyright (c) 2017 Charles Leifer"); storage backends Redis/Valkey, SQLite, in-memory, file, PostgreSQL; thread or greenlet workers; crontab periodic tasks.
- Also looked at: `arq` 0.28.0 (MIT, asyncio + Redis), `taskiq` 0.13.0 (MIT), `procrastinate` 3.10.0 (MIT, PostgreSQL-backed).
- The owner's rule: one server process per data folder (Solarlytics `Dockerfile`: "uvicorn runs one worker, and no two containers share the volume"; the launcher's lock file per data folder, U2 section 2.5); Solarlytics' in-memory `jobs.py` pool scoped per company with per-workspace and per-user limits (U2 section 3.3); Boxlytics' `ThreadPoolExecutor(max_workers=4)` with a `jobs` table (same section).
- Measured here: `scipy.optimize.milp` releases the GIL (section 1.2), so solves in a thread pool do not stall the API's event loop; one solve is 2-6 s.

### 5.1 Decision table (c)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **APScheduler 3.11 in the API process (`AsyncIOScheduler` started in the FastAPI lifespan), solves on a `ThreadPoolExecutor`, a durable `runs` table in each workspace file** | No new service; Windows and Linux identical; the single-writer rule holds by construction (only the API process writes); the GIL is released during HiGHS so 2-4 concurrent solves do not freeze requests; cron triggers at :00/:15/:30/:45 with `misfire_grace_time` and `coalesce`; the `runs` row (tick, site, started, finished, status, solver stats, plan id) makes a restart re-claim rather than re-run and gives the founder console its "last solve" column; interactive what-ifs and CSV imports reuse Solarlytics' `jobs.py` pattern on the same pool | Jobs die with the process (a restart mid-solve re-runs the tick: idempotent by design); one machine only; the scheduler and the API share CPU, so a burst of solves slows requests (bound the pool at 2-4 and keep request handlers cheap); APScheduler 4 is not coming soon (3.x is maintained) | **Yes, now** |
| **A plain loop process** (`python -m ems.scheduler`, `while True: sleep until next quarter`) | Simplest possible; own code, no library | A second process that must not write the data files: it would have to call the API over loopback for every write (plans, runs), doubling the surface; no misfire handling, no overlap guard, no cron parsing; everything APScheduler does for free | No (APScheduler in-process is the same thing with the guards written) |
| **RQ 3.0 + Valkey** | The FlexMeasures pattern (R4 A4: queues ingest/forecast/optimise/report, argument-hash job cache, per-site job index, callbacks); workers scale across machines; jobs survive API restarts; `rq-dashboard`; BSD licence; Valkey keeps the broker BSD | A broker to run and secure; on Windows only `SimpleWorker` (no forking), so development and production run different worker classes; workers are separate processes, so they must read the company files read-only and post results back to the API (single writer) or the rule is relaxed to "one writer per file per table"; serialisation of model inputs through Redis; at 3-30 sites it is machinery with no load to carry | **Later** (the 1,000-site path) |
| **Celery 5.7** | The best-known Python task queue; beat scheduler; many brokers | Windows unsupported by its maintainers (`--pool=solo` is a workaround, not support); heavy configuration; same multi-process writer problem as RQ with more moving parts | No |
| **dramatiq 2.2** | Simple API, reliable, Windows works with spawn | LGPL-3.0-or-later: the product's licence rules keep LGPL to test tooling only (Product 1 report lines 365-374); RabbitMQ or Redis needed anyway | No |
| **huey 3.5 with the SQLite backend** | No broker: the queue is a SQLite file; threads; crontab; MIT | A second writer on a SQLite file in the data folder (its own file, but in the same folder and process rules), and still a separate consumer process; smaller community; when a broker is wanted RQ is the better-trodden path | No (a near miss; keep in mind if the in-process pool ever needs to leave the API process without Redis) |

**Decision (c).** APScheduler 3.11.3 inside the API process. Concretely:

- `scheduler.py`: `AsyncIOScheduler(timezone="UTC")`, jobs `tick_15min` (`CronTrigger(minute="0,15,30,45", second=20)`: 20 s after the quarter so the edge's minute row for the quarter has arrived), `rollup_nightly` (02:30 Asia/Karachi), `backup_daily` (the lifted `backups.start_daily`), `forecast_refresh` (hourly), `archive_monthly`; `max_instances=1`, `coalesce=True`, `misfire_grace_time=300`.
- `tick_15min` lists the enabled sites of every company (reading `platform.db` once), writes a `runs` row per site with status `queued`, and submits `solve_site(company, site, tick)` to a `ThreadPoolExecutor(max_workers=(PREFIX)_SOLVER_THREADS, default 2)`; each solve reads inputs under `use_company`, calls the engine (section 6), writes the plan and the `runs` row (status `optimal | time_limit | failed`, `solve_s`, `mip_gap`, `objective`) in one transaction; failures keep the previous plan active until `valid_until` (R4 A10).
- Event-driven re-solves (outage detected, genset started, forecast bust, user override) go through the same function with a `reason`; a per-site `asyncio.Lock` prevents two solves of one site at once; the argument hash `(site, tick, config_hash)` de-duplicates a user-triggered solve that coincides with the tick (FlexMeasures' `job_cache` idea, in memory).
- Interactive jobs (what-if, backtest, CSV import, report render) use the lifted Solarlytics `jobs.py` (limits per workspace and per user, 180 s unread timeout) on the same executor; `GET /api/jobs/{id}` polls; section 7 adds an SSE nudge.
- Windows development: the launcher (U2 section 2.5) starts one uvicorn with the scheduler enabled by `(PREFIX)_SCHEDULER=1` (off in tests and in a `--scratch` copy, so two copies on one machine do not both solve).
- The budget check: 2 threads x 15 minutes / 10 s per solve = 180 solves per tick on one small VPS; the pilot needs 3.

## 6. Concern (d): the solver stack

Facts checked on PyPI on 6 November 2026 (latest version, upload date, licence field or classifier, Python floor, cp312 Windows wheel):

| Package | Version | Date | Licence | Python | cp312 win_amd64 wheel | Notes |
|---|---|---|---|---|---|---|
| `scipy` (installed: 1.17.1 with HiGHS 1.12.0) | 1.18.1 | 2026-08-21 | BSD-3-Clause | >= 3.12 | yes (and manylinux) | `scipy.optimize.milp` wraps HiGHS; five options only (section 1.3 item 6). Which HiGHS version 1.18.1 bundles: UNVERIFIED (1.17.1 prints 1.12.0) |
| `highspy` | 1.15.1 | 2026-07-02 | MIT (the HiGHS licence; the companion `highspy-extras` 1.15.1 is "Apache-2.0 AND BSD-3-Clause" because of its dependencies, per the release notes: "Note, that highspy-extras is distributed under the Apache 2.0 license, due to the dependencies' licensing") | >= 3.9 | yes, 60 wheels | The full HiGHS API: `passModel` with the same sparse matrices, `setSolution` (warm start), `setOptionValue` for every option (`mip_abs_gap`, `mip_heuristic_effort`, `threads`, `time_limit`), callbacks (`kCallbackMipInterrupt`, used by evcc for a wall-clock cut that keeps the incumbent, R1 section 6), `writeModel("x.mps")`, and a modelling helper API (`addVariable`, `addConstr`) |
| `cvxpy` | 1.9.3 | 2026-09-19 | Apache-2.0 | >= 3.11 | yes | HiGHS is listed in its solver table with a starred MIP mark ("Mixed-integer LP only"); pulls `clarabel` 0.11.1 (Apache-2.0), `scs` 3.3.1 (MIT), `osqp` 1.1.3 (Apache-2.0) as default solvers; EMHASS uses it (Product 1 report). Canonicalisation time for a 2,700-variable model: UNVERIFIED here (not installed; EMHASS reports ~0.1 s per iteration for its LP) |
| `PuLP` | 4.2.0 | 2026-10-22 | MIT | >= 3.12 | yes (a Rust core extension now) | 4.x is a breaking API ("Variables are created with prob.add_variable(...)", "prob.solve() returns an LpSolveStats object"); "CBC is not shipped inside the PuLP package", `pulp[cbc]` installs `cbcbox` 2.936 (EPL-2.0), `pulp[highs]` the highspy interface; evcc-io/optimizer uses PuLP 4.0.x (R1) |
| `Pyomo` | 6.10.1 | 2026-06-04 | BSD-3-Clause | >= 3.10 | pure | FlexMeasures uses it with HiGHS (R4); an algebraic modelling language with its own model-build time; the heaviest of the four and the most general |
| `ortools` | 9.15.6755 | 2026-01-14 | Apache-2.0 | >= 3.9 | yes | Not asked for; noted because its CP-SAT is the other free MIP engine; 100+ MB wheel; no |
| `mip` (python-mip) | 2.0.0 | 2026-08-18 | EPL-2.0 | >= 3.10 | pure | CBC bindings; EPL; no |

HiGHS itself: MIT (the solver's banner: "Copyright (c) 2025 HiGHS under MIT licence terms"); releases v1.15.1 (2 July 2026), v1.15.0 (28 June), v1.14.0 (6 April).

### 6.1 Decision table (d)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **`scipy.optimize.milp` with the model built as sparse matrices by own code (as `milp_bench.py` does)** | Already in the `ops` env and in every Python the team will have (scipy is a dependency of the forecasters anyway); BSD; measured 2-6 s on the 192-step model with 577 binaries, proven optimal at the root; the matrix builder is ~120 lines and is the product's own, so the "why this plan" explanation can name every row; nothing to canonicalise; a wheel on every platform; the same arrays go to `highspy.passModel` unchanged later | Five options and no warm start, no interrupt callback, no absolute gap, no MPS export; the HiGHS version is whatever scipy pins (1.12.0 in 1.17.1); hand-written matrices need a small test harness (a brute-force checker on 4-step instances, R4 R3) and a row-naming map for debugging | **Yes, now** |
| **`highspy` 1.15.1 directly** | The whole solver: warm start from the previous plan (the heuristics are the whole solve time, so an incumbent should cut it; UNVERIFIED by measurement here), `mip_abs_gap` in rupees, a wall-clock interrupt that keeps the incumbent, `threads`, MPS/LP dumps for a failing instance, a newer HiGHS than scipy carries; MIT; wheels for Windows and manylinux; the same matrices | One more dependency (plus `highspy-extras`, Apache-2.0); the Python API has moved between releases (`addVariable` name quirks in 1.14.0 notes); two code paths if scipy stays as a fallback | **Yes, second step**: add it when warm starts or callbacks are wanted, behind the same `solve(model)` interface, with an equivalence test against the scipy path (objective within 1e-6 on the test instances) |
| **CVXPY 1.9.3 + HiGHS** | Readable algebraic model (`cp.Variable(T)`, `cp.sum(...)`), DCP checking catches modelling mistakes, EMHASS's code is a worked example (MIT), easy to add a convex term later (a quadratic battery stress cost) | Apache-2.0 fine; but Python >= 3.11 and a tree of solver wheels (`clarabel`, `scs`, `osqp`) the product does not use; canonicalisation per solve (UNVERIFIED cost at this size); piecewise-linear and big-M logic is written the same way either way; one more abstraction between the plan and its explanation | No (not needed for one hand-written model; revisit only if a convex non-linear term appears) |
| **PuLP 4.2 + HiGHS** | evcc-io/optimizer's choice, so its soft-constraint patterns port directly (R1); MIT; lightweight modeller | The 4.x API just broke (October 2026) and will settle for a while; `pulp[highs]` goes through highspy anyway, so it adds a layer rather than a capability; the model is built variable by variable in Python (slower builds than the vectorised matrix builder: R1 notes "build" as tens of ms for evcc; the matrix build here is 5 ms) | No |
| **Pyomo 6.10 + HiGHS** | The most general modelling language; FlexMeasures' choice; BSD-3 | Heaviest build time and API; FlexMeasures itself keeps a direct `highspy` build "for speed" (R4 R3: "TWO MODELS TO KEEP IN SYNC") | No |

**Decision (d).** `scipy.optimize.milp` with the product's own matrix builder now; `highspy` 1.15.1 as the second step through the same interface. Modelling comfort for the 192-step MILP with ~3 binaries per step plus piecewise-linear terms: the matrix builder in `milp_bench.py` already expresses all of it (two-segment wear cost as two discharge variables with ascending costs, the genset fuel intercept on the on-binary, min-up/min-down as window rows, the peak as one scalar, the ICP fill-first with one binary) in 120 lines; the product version adds named rows (`row_ids["soc_dyn", t]`), a `Model` dataclass holding `c, A, lo, hi, lb, ub, integrality, names`, a `relax()` toggle per binary family (section 1.3 item 7), and a `Result` with the plan, per-step cost breakdown, duals of the LP relaxation (for "what the peak costs you") and the solver statistics logged to `runs`.

## 7. Concern (e): real-time UI updates (SSE, WebSocket or React Query polling)

Facts checked on 6 November 2026: `sse-starlette` 3.6.0 (2026-10-25, BSD-3-Clause, Python >= 3.11); its README: "Nginx buffers responses by default, delaying SSE events until ~16KB accumulates", fix "proxy_buffering off;" for the location or the `X-Accel-Buffering: no` header; default ping every 15 s ("Ping interval in seconds (0 disables keep-alive pings)"). Starlette 1.10.0 (BSD-3) has WebSocket routes built in; `websockets` 17.4 (BSD-3) is uvicorn's WebSocket implementation (uvicorn 0.54.0, BSD-3). `@tanstack/react-query` 5.106.1 (MIT; 45 KB min / 13 KB gzip) has `refetchInterval` and `refetchIntervalInBackground`; Boxlytics already uses it with a 5-minute `staleTime` (U1 section 1). The browser's `EventSource` cannot send an `Authorization` header (a property of the Web API, not of any library); `@microsoft/fetch-event-source` 2.0.1 (MIT) works around that but was last published 2021-04-25.

What actually changes on the screens: telemetry arrives once a minute per site; a plan changes once per 15 minutes (or on an event); a job (what-if, import, report) finishes after seconds to a minute; a device goes offline after a few missed minutes. Nothing is sub-second.

### 7.1 Decision table (e)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **React Query polling (`refetchInterval` 30-60 s on the live queries, 2 s on a running job)** | Nothing new on the server; works through every proxy and Vercel; same code path as the first load; retries, caching and focus-refetch for free; Boxlytics' pattern (`analysis.service.ts` polls a job every 2 s) | Wasted requests when nothing changed (cheap: a 48-hour read is 5 ms); up to one interval of staleness; dozens of open tabs multiply the load | **Yes, now, for everything** |
| **SSE (`sse-starlette`) on one `GET /api/events` stream per signed-in tab** | One-way push is exactly the need (plan updated, job done, site offline, alert); native `EventSource` reconnects by itself with `Last-Event-ID`; HTTP, so nginx and TLS just work with `proxy_buffering off`; in one process the publisher is an `asyncio.Queue` per connection fed by the scheduler and the ingest route | `EventSource` cannot carry the bearer token: either a short-lived signed token in the query string minted by `POST /api/events/token` (10 minutes, single use) or the fetch-based polyfill; HTTP/1.1 limits a browser to ~6 connections per origin (use HTTP/2 on nginx or one stream per tab); multi-process servers need a bus (Valkey pub/sub) later; nothing in the owner's products to copy | **Yes, second step**: the events invalidate React Query keys (`queryClient.invalidateQueries(["site", id, "plan"])`) so the polling interval can be relaxed to 2-5 minutes |
| **WebSocket** | Bidirectional; one connection per tab for everything | The UI sends nothing that a POST cannot carry; reconnect, heartbeat and auth are hand-written (`websockets` has them, the browser side does not); nginx needs the `Upgrade`/`Connection` headers; harder to test than HTTP; same multi-process bus problem | No for the UI; the edge gateway (Product 2) may use it later |

**Decision (e).** React Query polling in the first build (30 s on the live site page, 60 s on dashboards, 2 s on a running job, `refetchIntervalInBackground: false`), then one SSE stream per tab through `sse-starlette` to invalidate queries, with `proxy_buffering off` in the nginx location and the token-in-query-string pattern. No WebSocket for the UI.

## 8. Concern (f): frontend charting for 1-minute and 15-minute series and a 48-hour plan Gantt

Facts checked on 6 November 2026 (npm registry; bundlephobia API for sizes; project pages for licences and performance claims):

| Library | Version (date) | Licence | Minified / gzip | Renderer | React fit | Notes |
|---|---|---|---|---|---|---|
| `recharts` | 3.10.1 (2026-07-25; 3.11 canaries weekly) | MIT | 548 KB / 146 KB | SVG | Native React components | Boxlytics' library (4 uses, U1 section 1; U1 proposes `^3.10.1` for the EMS). Every point is a DOM node: secondary sources put the comfortable limit near 8,000-10,000 points per chart and report ~35 FPS at 10K points (vendor benchmark, indicative only) |
| `echarts` | 6.1.0 (2026-05-19) | Apache-2.0 | 1,089 KB / 359 KB for the full build; tree-shakeable (`echarts/core` + the line, bar, custom series and the components used; typically a third of the full size, UNVERIFIED for this exact set) | Canvas (SVG optional) | `echarts-for-react` 3.0.6 (MIT; a 3.0.7 exists outside the `latest` tag) or a 40-line own hook (`useEffect` + `echarts.init` + `setOption`, recommended) | Its handbook: "for larger amounts of data (>1k is an experience value), canvas renderer is always recommended"; line `sampling: 'lttb'`, `progressive` rendering, `large: true` for big series; `dataZoom` built in; a Gantt is the documented custom-series example ("Gantt chart of airport flights", `custom-gantt-flight`; not re-opened this run: UNVERIFIED) |
| `uplot` | 1.6.32 (2025-03-14) | MIT | 50 KB / 21 KB | Canvas | `uplot-react` 1.2.4 (MIT) or own hook | README: a chart of "166,650 data points" "in 25ms" from cold, "scaling linearly at ~100,000 pts/ms", "~50 KB min"; the fastest option by far for raw 1-minute streams; imperative API, plain styling, no Gantt, no built-in legend interactions beyond the basics |
| `plotly.js` | 4.2.0 (2026-10-29) | MIT | 4,543 KB / 1,376 KB full (`plotly.js-dist-min`); 1,134 KB / 372 KB basic bundle | SVG + WebGL (`scattergl`) | `react-plotly.js` 4.1.0 | The notebook library; too heavy for a SPA that only needs lines, bars and a Gantt; Node >= 22 to build |
| `@visx/xychart` | 4.0.0 (2026-06-11) | MIT | 148 KB / 49 KB | SVG | Low-level React + d3 primitives | Build-your-own charts; same SVG limits as Recharts; more work per chart |
| `lightweight-charts` (TradingView) | 5.2.1 | Apache-2.0 | 190 KB / 60 KB | Canvas | own hook | Finance-style time series only; no Gantt; not needed |
| `vis-timeline` | 8.5.6 | Apache-2.0 OR MIT | large (`moment` peer) | DOM | own hook | A Gantt/timeline widget; heavy peers; ECharts does the same in one custom series |

What the pages need (R8's inventory and the guideline): (1) a live site view: the last 48 hours of 1-minute PV, load, grid, battery, SOC (2,880 points x 5 series = 14,400 points) with the plan overlaid; (2) a telemetry explorer: a day (1,440) to a month (43,200) of 1-minute points per channel with zoom; (3) the 48-hour plan: 192 steps as stacked bars (grid, PV, battery, genset) and a Gantt of states per step (charge / hold / discharge / reserve hold / genset run / outage window / TOU peak window); (4) KPI and savings charts: daily and monthly bars, a few hundred points; (5) the report's charts, which are made on the server (section 9).

### 8.1 Decision table (f)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Recharts for everything** | One library, the owner's house style, React-native composition, 146 KB gzip | Breaks on the explorer (a week of 1-minute data is 10,080 points per series: the DOM-per-point model stalls at ~10K) unless every 1-minute view is downsampled to ~1,000-2,000 points server-side; no zoom-and-reload interaction built in (`Brush` only); a Gantt is a hack of stacked horizontal bars | Not alone |
| **ECharts for everything** | Canvas, `dataZoom`, LTTB sampling and progressive rendering handle 1-minute weeks without server downsampling; the Gantt is a custom series; Apache-2.0; one theme object | 359 KB gzip full (tree-shake it); imperative `setOption` in a hook rather than JSX, so the dashboard's small charts take more code than Recharts; not the house style | Not alone |
| **Recharts for the dashboard and report-style charts (15-minute plan bars, daily/monthly KPIs, a few hundred points) + ECharts for the two heavy views (the 1-minute explorer with `dataZoom` + LTTB, the 48-hour Gantt as a custom series), each behind one wrapper component with the theme applied once** | Each library does what it is good at; the Boxlytics conventions carry over for the small charts (U1's `charts/` folder: `TimeSeriesChart`, `SlotBarChart`, `SocLineChart`, `SavingsBarChart`); the explorer and the Gantt are two components, written once; both MIT/Apache-2.0 | Two chart libraries in one bundle (about 500 KB gzip together before tree-shaking; code-split the explorer and plan pages with `React.lazy` so the dashboard does not load ECharts); two theming paths (one colour token list feeds both) | **Yes** |
| **uPlot for the explorer instead of ECharts** | A fifth of ECharts' size, the fastest raw rendering; MIT | No Gantt, so ECharts (or a hand-drawn SVG Gantt) is still needed; styling and legend work; smaller community in React | Keep as the fallback if the explorer's bundle or frame rate becomes a complaint |
| **Plotly.js** | Everything, WebGL | 1.4 MB gzip; the wrong tool for a product SPA | No |
| **visx** | Full control, small | A chart library to write; SVG limits | No |

**Server-side downsampling regardless of library.** `GET /api/sites/{id}/telemetry?from&to&step=auto&max_points=2000` returns 1-minute rows for ranges up to 2 days and `q15`/LTTB-reduced rows above, so no page ever receives more than a few thousand points per series; the explorer re-queries on zoom. This keeps Recharts safe where it is used and keeps the JSON small (a day of 10 channels at 1 minute is ~300 KB raw; gzip on nginx halves it).

**Decision (f).** Recharts 3.10.1 + ECharts 6.1.0 (own hook; code-split), with the downsampling endpoint; uPlot as the named fallback for the explorer.

## 9. Concern (g): PDF reports

What the owner's products do: Boxlytics renders four reports in the browser with `jspdf` 3.0.4 (now 4.2.1 on npm, MIT, 394 KB / 127 KB gzip) in a 2,081-line `pdf-export.service.ts`; `html2canvas` 1.4.1 (MIT) is declared but never imported there (U1 section 1), and its last publish was 2022-01-22. Solarlytics makes its documents on the server: the quotation's Word file is written with the standard library ("A DOCX is a zip of XML parts ... written in a fixed order with a fixed time stamp ... byte for byte the same each time it is made", `backend/solarlytics/docx.py` lines 1-16), and the PDF is printed by the web app from the same customer view; the memory note `docx-to-pdf-export.md` records a Word COM path for DOCX-to-PDF on Windows, which does not exist in a Linux container.

Facts checked on 6 November 2026:

| Option | Version (date) | Licence | Windows dev | Linux Docker | Notes |
|---|---|---|---|---|---|
| `reportlab` | 5.1.0 (2026-10-22) | BSD | pure wheel | pure wheel | Programmatic PDF: paragraphs, tables, embedded PNG charts; deterministic output; no HTML/CSS |
| `weasyprint` | 70.0 (2026-09-08) | BSD | needs MSYS2: "Install MSYS2 keeping the default options", "execute pacman -S mingw-w64-ucrt-x86_64-pango"; "WeasyPrint is regularly marked as malware by different antivirus companies" | apt `libpango` in the image | HTML + CSS to PDF with page boxes, headers and footers; Jinja2 3.1.6 (BSD) templates |
| `playwright` (Python) | 1.64.1 (2026-10-22) | Apache-2.0 | `playwright install chromium` (~150 MB) | the same inside the image (+150-300 MB, UNVERIFIED size) | `page.pdf()` prints a React route of the SPA with the same chart components; needs the web app reachable from the backend |
| `matplotlib` | 3.11.2 (2026-09-11) | PSF-style (matplotlib licence) | wheel | wheel | Chart PNGs for ReportLab or WeasyPrint; 3.10.9 is in `ops` |
| `fpdf2` | 2.9.0 (2026-10-29) | LGPL-3.0-only | pure | pure | Excluded by the product's LGPL rule |
| `jspdf` (+ `jspdf-autotable` 5.0.9) | 4.2.1 (2026-03-17) | MIT | browser | browser | Client-side; the report then depends on the tab's data, not the API's; U1 section 7.1 says "defer" and "Savings reports should be rendered by the backend ... from the same numbers the API serves, so the PDF and the screen cannot disagree" |
| `@react-pdf/renderer` | 4.9.0 | MIT | browser or Node | Node | React components to PDF; a Node runtime in the backend container, which the backend does not have |

### 9.1 Decision table (g)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Backend PDF with ReportLab 5.1 + matplotlib PNG charts, one `reports/` module, rendered as a job and stored in the data folder** | Pure Python, BSD, identical on Windows and Linux; the statement is made from the API's own numbers (the savings engine's daily rows), so the PDF, the page and the API agree; deterministic bytes (same inputs, same file: the Solarlytics habit); a monthly job renders every company's statements overnight and the founder console can re-run one | Layout is code, not CSS; a designer cannot restyle it; tables and page furniture (headers, page numbers) are hand-built once (ReportLab's `platypus` does most of it) | **Yes, now** (the monthly savings statement, the daily plan-and-savings sheet, the pilot's weekly switchback report) |
| **WeasyPrint from Jinja2 HTML** | HTML/CSS layout; a designer can restyle; `@page` headers and footers | The Windows developer needs MSYS2 and Pango, and antivirus false positives on the owner's laptop (the memory notes record the owner's dislike of fragile tooling); every chart still has to be a PNG or inline SVG | No (second choice if the layout demands CSS) |
| **Playwright printing the SPA's report route** | The report looks exactly like the screen, same Recharts components; Apache-2.0 | Chromium in the backend image; the backend must call the web app (on Vercel or a bundled build) with a signed single-use token; slower (seconds per page); two moving parts for a nightly job | Later, only if "looks like the screen" is demanded |
| **jspdf in the browser (Boxlytics' way)** | No backend work; the owner's existing pattern | 2,000-line hand-drawn generators; numbers come from the page state; cannot run nightly or be emailed by the server; U1 advises against it | No |
| **DOCX export (lift the Solarlytics `docx.py` pattern)** | Customers who want to edit a statement get a Word file; stdlib only; deterministic | Not a PDF; PDF conversion on Linux would need LibreOffice (excluded by the owner's memory note) | Optional add-on for the statement; not in the first build |

**Decision (g).** ReportLab 5.1.0 + matplotlib 3.11.2 on the backend, one `reports/` package, rendered by the nightly job and on demand through the jobs pool, files kept under the company's data folder with a `reports` table row (hash, period, site, path); `GET /api/reports/{id}/pdf` streams it. No jspdf or html2canvas in the frontend.

## 10. Concern (h): auth (lift Solarlytics' local-mode JWT sign-in and founder console; Supabase mode later)

U2 section 1 is the audit; this section only decides. The module: `identity.py` (HS256 local tokens in the claim shape of a Supabase access token, one hour; refresh tokens rotated per use, families ended on reuse), `platform.py` (the registry `platform.db`: accounts, companies, memberships, PBKDF2 passwords, lockouts), `companies.py` (resolve the company per request, seats), `auth.py` (roles and permissions), `apikeys.py` (scoped keys with a prefix, found through an index), `founder.py` + `routes_founder.py` + `store_founder.py` (the two-step founder login with TOTP, HMAC tokens, audit), `backups.py`, `mfa.py` (RFC 4226/6238 from the RFC texts), `routes_local.py`, `seed_demo.py`; about 4,300 lines plus 450 lines of `api.py` guard and 5,000 lines of tests to carry (U2 section 1.7). Dependencies: `PyJWT` 2.15.1 (2026-09-28, MIT, pure) and `cryptography` 50.0.2 (2026-09-30, "Apache-2.0 OR BSD-3-Clause", abi3 Windows and manylinux wheels), neither in the `ops` env.

### 10.1 Decision table (h)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Lift the Solarlytics module as a package with U2's six edits (roles table, API-key scopes and prefix, founder console counts, a trimmed `routes_platform.py`, the seed, drop `link_company.py`), plus the 450-line guard cut of `api.py`** | OnyxLeaf's own code, written in October 2026, tested by ~5,900 lines; local mode needs no external service; per-company files match the tenancy (a site belongs to one company); the founder console is the admin dashboard; Supabase mode is already built and carried unused at no cost; the web app's sign-in pages exist in the Solarlytics frontend repository (not on this machine: UNVERIFIED that they lift cleanly, U2 item 7) | The guard reads `platform.db` on every request and resolves the company in a thread pool: fine for people, wasteful for one telemetry POST per site per minute (1,000 sites = 17 req/s, each a registry read); the roles are Solarlytics' and must be replaced; the Boxlytics-derived founder token format is a second reuse of Boxlytics code to record in DESIGN_LOG (U2 section 7) | **Yes** |
| **Write a new, smaller sign-in (FastAPI dependency + PyJWT, one `users` table)** | Less code to carry | Throws away the lockouts, refresh rotation, founder console, audit, seat rule, backups and their tests; the EMS would be the only OnyxLeaf product with a different sign-in | No |
| **Supabase mode first** | Hosted accounts, password reset emails, OAuth later | The project does not exist; the SQL has never been applied; the owner's Solarlytics decision was local mode first; no benefit for a pilot of a few companies | No (keep the mode in the code) |
| **A third-party library (`fastapi-users`, Authlib, Keycloak)** | Standard flows | Another dependency surface, a different token shape from the owner's other products, and still no founder console | No |

**Decisions inside the lift.**

- Roles: `admin` (everything in the company), `operator` (edit sites and tariffs, approve and override plans, write TOU slots), `analyst` (read telemetry, plans and reports; run what-ifs), `viewer` (dashboards and reports); permissions `edit_site`, `edit_tariff`, `approve_plan`, `override`, `manage_users`, `edit_workspace`, `see_reports`, `run_whatif` (U2's proposal, adopted).
- API keys: prefix of the product's choosing (not `slk_`), scopes `telemetry:write`, `sites:read`, `plans:read`, `reports:read`; one key per site for the edge, one per integrator (Enerlytics-type monitoring) per company. The ingest route checks the key through a 60-second in-process cache of `key hash -> (company id, site id, scopes)` invalidated on key revocation, so a telemetry POST costs no registry read; every other route keeps the per-request resolve.
- Rate limits: the lifted sliding-window `Limiter` (U2 section 1.7 item 1) with a separate budget for the ingest route (per key: 120/min, so a backfill of 60 rows per POST never trips it).
- The founder console reads per company: sites, devices online, days of telemetry, last solve time and status, plans issued this month, statements rendered; "back up now" copies both files of the company.
- Environment names: `(PREFIX)_AUTH_MODE`, `_LOCAL_JWT_SECRET`, `_FOUNDER_USERNAME/_PASSWORD_HASH/_TOTP_SECRET/_SESSION_SECRET`, `_HOSTS`, `_CORS_ORIGINS`, `_TRUST_PROXY`, `_DB`, `_DATA_DIR`, `_DAILY_BACKUP`, `_PORT`, plus the EMS's own `_SCHEDULER`, `_SOLVER_THREADS`, `_SOLVER_TIME_LIMIT`, `_WEATHER_API_KEY` (if R5 chooses a paid feed), with the Solarlytics test that greps every setting into `.env.production.example` (U2 section 1.5).
- Record in DESIGN_LOG: the lift (files and commit `c853130` of `solarlytics-backend`) and the Boxlytics-derived founder token format (`boxlytics@c429bef`), for the owner to confirm.

## 11. Concern (i): packaging and deployment (Dockerfile, compose, nginx, env files, backups)

What exists to copy from (both read-only): Solarlytics' `Dockerfile` (`python:3.12.7-slim-bookworm`, `pip install -r requirements.txt`, the package copied, a system user `uid 10001`, `VOLUME /data`, `HEALTHCHECK` on `/api/health`, `CMD uvicorn ... --proxy-headers --no-server-header`, one worker, "no two containers share the volume") and `docs/DEPLOY.md` (loopback-only port, TLS proxy, `FORWARDED_ALLOW_IPS`, `SOLARLYTICS_HOSTS`, the `$` in the founder hash quoted in env files, the first account before exposure); Boxlytics' `deploy/docker-compose.prod.yml` (one service, `restart: unless-stopped`, `env_file`, `127.0.0.1:8000:8000`, volumes, healthcheck), `nginx-boxlytics-backend.conf` (`client_max_body_size 20m`, `proxy_read_timeout 120s`, the forwarded headers) and the systemd unit (U2 section 3.4). Python 3.12's latest patch is 3.12.16 (25 October 2026, python.org); the `ops` env is 3.12.11.

### 11.1 Decision table (i)

| Item | Options | Verdict and the reason |
|---|---|---|
| Base image | `python:3.12.16-slim-bookworm` pinned by tag and digest; `python:3.13`; distroless | **3.12.16-slim-bookworm**: the same minor as the owner's `ops` env, so what is tested on Windows is what runs; Solarlytics' pattern; pin the digest in the Dockerfile comment as its DEPLOY.md asks |
| Processes in the container | one uvicorn worker with the scheduler inside (section 5); gunicorn + uvicorn workers; a separate worker container | **One uvicorn worker, scheduler inside**, exactly one container per data folder; `gunicorn` 26.2.0 is not needed with one worker |
| Compose | `docker compose` with `api` only; `api` + `mosquitto`; `api` + `valkey` + `worker` | **`api` only in the first build**, with commented-out `mosquitto` (section 4) and `valkey` + `worker` (section 13) services in the same file so the later steps are a line change; named volume `ems-data:/data`; `127.0.0.1:(port):(port)`; `env_file: /etc/(product)/.env.production`; the healthcheck from the Dockerfile |
| Reverse proxy | nginx on the host (as `api.onyxleaf.co`); Caddy | **nginx**, one server block: `client_max_body_size 64m` (CSV uploads), `proxy_read_timeout 120s`, the forwarded headers, gzip on for JSON, HTTP/2, and a `location /api/events` with `proxy_buffering off; proxy_cache off; proxy_http_version 1.1; proxy_set_header Connection "";` for SSE (section 7); TLS by certbot as Boxlytics does |
| The SPA | Vercel (as Boxlytics and Solarlytics); served by the API container (`(PREFIX)_WEB_DIST`) | **Vercel** for the hosted copy, `vercel.json` SPA rewrite, `VITE_API_BASE_URL` and `VITE_AUTH_MODE` set in the project; the container can also serve a build for an on-premises copy (Solarlytics' `WEB_DIST` pattern) |
| Env files | `.env.production.example` with every setting named and no secret, enforced by a test; pydantic-settings | **The Solarlytics way** (plain `os.environ` reads, the example file, the grep test): U2 section 3.1 records the pydantic-settings trap with comma lists; `python-dotenv` 1.2.4 only in the Windows launcher |
| Backups | in-app daily copies (`backups.py`: `Connection.backup`, 14 kept) ; `VACUUM INTO`; off-machine copies with `rclone` or `restic` to object storage; Parquet archive rsync | **All three layers**: (1) the lifted daily copy of both company files (measured ~70 MB/s, so a 30-site company's 13-month telemetry file of ~1.6 GB copies in ~25 s; run at 03:00 local after the roll-up), 14 kept, plus the pre-migration copy; (2) a host cron that syncs `/data/backup/` and `/data/archive/` (Parquet) off the machine nightly with `rclone` to an object store of the owner's choosing (UNVERIFIED which; Backblaze B2 or Hetzner Object Storage are the usual cheap ones) and keeps 30 daily + 12 monthly; (3) a quarterly restore drill with `scripts/restore_backup.py` on a staging copy, as DEPLOY.md prescribes. SQLite files must not be copied with `cp` while written; only `backup()`/`VACUUM INTO` copies are consistent |
| Logs and metrics | the rotating JSON log file (`/data/logs`, 2 MB x 5, Solarlytics); `prometheus-client` 0.26.0; an error webhook | **The log file + `/api/health` + the founder console's per-company "last solve"**; `(PREFIX)_ERROR_WEBHOOK` as Solarlytics has; Prometheus only when there is something to scrape it |
| CI | GitHub Actions (Solarlytics `ci.yml`: backend tests, frontend checks, image build) | **The same workflow** plus a job that runs `bench/milp_bench.py` with a 20 s assertion on the median, and `docker build` of the image (the owner cannot build images on the laptop: Docker is absent, so CI is where the Dockerfile is proven) |
| Windows development | the launcher pattern (`scripts/start_local.py`: both servers, no console windows, a lock file per data folder, scratch ports) | **Lift it**; ports proposed by U2: 8051 API and 5193 web, scratch 8151 and 5293 (UNVERIFIED free: Panelytics' ports were not checked); the backend's own venv (`.scratch/venv`, git-ignored) because `ops` lacks PyJWT, cryptography, APScheduler, sse-starlette, reportlab; `pythonw` for anything long-lived (memory `no-console-windows.md`) |
| Staging | a second container on the same host with its own data folder and host name | **Yes**, as DEPLOY.md describes; the demo seed makes its companies |

The `docker-compose.yml` sketch:

```yaml
services:
  api:
    build: .
    image: (product):0.1.0
    restart: unless-stopped
    env_file: /etc/(product)/.env.production
    ports: ["127.0.0.1:8060:8060"]
    volumes: ["ems-data:/data"]
    healthcheck: {test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8060/api/health', timeout=4).status==200 else 1)"], interval: 30s, timeout: 5s, retries: 3, start_period: 30s}
  # mosquitto:   # section 4, later: eclipse-mosquitto:2.1 with /mosquitto/config and /mosquitto/data volumes, 127.0.0.1:8883
  # valkey:      # section 13, later: valkey/valkey:8 with appendonly
  # worker:      # section 13, later: the same image, command "rq worker optimise forecast report --url redis://valkey:6379"
volumes:
  ems-data: {}
```

## 12. The recommended combination, with pinned versions (as of 6 November 2026)

**In one paragraph.** A FastAPI backend in one container with one uvicorn worker, per-company SQLite files (a workspace file and a telemetry file per company, WAL), APScheduler running the 15-minute tick and the nightly roll-up inside the process, solves on a thread pool through `scipy.optimize.milp` behind an interface that `highspy` will later implement too, HTTP batch ingest and CSV import now and Mosquitto later, the lifted Solarlytics sign-in and founder console, ReportLab PDFs rendered by jobs, SSE for nudges on top of React Query polling; a React 19 + Vite 8 + TypeScript SPA on Vercel with Recharts for the small charts and ECharts for the explorer and the plan Gantt, served data that is downsampled on the server; nginx and certbot on a Linux Docker host; Parquet archives and DuckDB when analytics outgrow SQLite; PostgreSQL + TimescaleDB and RQ + Valkey as the named 1,000-site path.

### 12.1 Backend `requirements.txt` (exact pins; every version is the PyPI `latest` on 6 November 2026 unless noted)

```
# Web
fastapi==0.162.0            # MIT, 2026-10-23
starlette==1.10.0           # BSD-3-Clause, 2026-10-24
pydantic==2.14.3            # MIT, 2026-11-05
uvicorn==0.54.0             # BSD-3-Clause, 2026-09-25
python-multipart==0.0.33    # Apache-2.0, 2026-10-31 (CSV uploads)
sse-starlette==3.6.0        # BSD-3-Clause, 2026-10-25 (second step; pin now)
# Numbers and the solver
numpy==2.5.4                # BSD-3-Clause, 2026-10-10
scipy==1.18.1               # BSD-3-Clause, 2026-08-21 (scipy.optimize.milp; HiGHS version inside: UNVERIFIED, 1.17.1 carries 1.12.0)
pandas==3.1.1               # BSD-3-Clause, 2026-11-04
threadpoolctl==3.8.0        # BSD-3-Clause, 2026-11-05 (cap BLAS threads in the solver pool; memory note "BLAS thread trap")
# Sign-in (the lifted module)
PyJWT==2.15.1               # MIT, 2026-09-28
cryptography==50.0.2        # Apache-2.0 OR BSD-3-Clause, 2026-09-30
# Jobs
APScheduler==3.11.3         # MIT, 2026-06-28
# Reports
reportlab==5.1.0            # BSD, 2026-10-22
matplotlib==3.11.2          # PSF-style, 2026-09-11
# Archive (optional until the first month closes)
pyarrow==26.0.1             # Apache-2.0, 2026-11-03
# Dev and tests (requirements-dev.txt)
pytest==9.2.0               # MIT, 2026-10-27
pytest-xdist==3.8.0         # MIT, 2025-07-01
httpx==0.28.1               # BSD-3-Clause, 2024-12-06 (the TestClient transport; 1.0 is still dev)
# Later, each behind a flag: highspy==1.15.1 (MIT) with highspy-extras==1.15.1 (Apache-2.0 AND BSD-3-Clause); aiomqtt==2.5.1 (BSD) over paho-mqtt==2.2.0 (EPL-2.0 OR BSD-3-Clause);
# rq==3.0.0 (BSD-2-Clause) with redis==8.1.0 (MIT) against Valkey; duckdb==2.0.0 (MIT).
# Forecasting pins (scikit-learn==1.9.1 BSD-3, xgboost==3.4.1 Apache-2.0, pvlib==0.16.1 BSD-3, skforecast==0.26.0 BSD-3) are R5's decision, listed here only as checked versions.
```

Two notes on the pins. First, the owner's `ops` env is behind on several (fastapi 0.141.1, scipy 1.17.1, numpy 2.4.3, pandas 3.0.2, uvicorn 0.52.3, starlette 1.6.0, pydantic 2.13.4): the backend's own venv (`.scratch/venv`) takes the pins above, as Solarlytics did ("pinned at the version the full checks passed with"), and the benchmark is re-run there as a test. Second, if the team prefers to pin to what the benchmark was measured on, `scipy==1.17.1` and `numpy==2.4.3` are the measured pair; nothing else in this note depends on the scipy minor.

### 12.2 Frontend `package.json` (caret ranges on the `latest` of 6 November 2026; U1 section 7.1 chose the TypeScript line)

```
"dependencies": {
  "react": "^19.3.1", "react-dom": "^19.3.1",                      // MIT, 2026-10-14
  "react-router-dom": "^7.18.5",                                   // MIT, 2026-10-27
  "@tanstack/react-query": "^5.106.1",                             // MIT, 2026-11-05
  "@tanstack/react-table": "^9.3.1",                               // MIT, 2026-10-12 (U1: v9 if DataTable is written fresh)
  "zustand": "^5.0.16",                                            // MIT, 2026-10-22
  "zod": "^4.6.7",                                                 // MIT
  "recharts": "^3.10.1",                                           // MIT, 2026-07-25
  "echarts": "^6.1.0",                                             // Apache-2.0, 2026-05-19 (own hook; tree-shaken imports)
  "lucide-react": "^1.69.0",                                       // ISC
  "date-fns": "^4.4.0",                                            // MIT (Asia/Karachi has no DST; format in site time)
  "clsx": "^2.1.1", "tailwind-merge": "^3.7.0"
},
"devDependencies": {
  "vite": "^8.3.6", "@vitejs/plugin-react": "^6.2.1",              // MIT; engines node ^20.19 || >=22.12
  "typescript": "~5.9.3",                                          // Apache-2.0; 7.0.2 is latest, U1 defers it
  "tailwindcss": "^4.3.4", "@tailwindcss/vite": "^4.3.4",          // MIT
  "vitest": "^5.1.0", "@vitest/coverage-v8": "^5.1.0",             // MIT; node ^22.12
  "@playwright/test": "^1.65.0",                                   // Apache-2.0
  "eslint", "typescript-eslint", "prettier" as U1 section 7.1
},
"engines": {"node": ">=22.12 <23"}   // the machine has 22.13.0
```

Not added: `jspdf`, `html2canvas`, `plotly.js`, `echarts-for-react` (a hook is 40 lines and avoids a dependency that lags ECharts majors), `@microsoft/fetch-event-source` (use native `EventSource` with the token-in-query pattern), `uplot` (named fallback), `vis-timeline`.

### 12.3 Why this combination

1. It is the owner's existing shape (SQLite per company, one process per data folder, the Solarlytics sign-in, Vercel + nginx + Docker) with the three EMS-specific additions that the measurements justify: a second SQLite file for telemetry (10 B/point, 2-7 ms hot reads), an in-process scheduler with threaded solves (the GIL is released; a solve is 2-6 s), and scipy's HiGHS (already installed, root-node optimal on the 192-step model).
2. Every dependency is MIT, BSD, Apache-2.0, PSF or public domain; nothing copyleft ships (section 14).
3. Windows development needs nothing but `pip` and `npm`: no database server, no broker, no Docker on the laptop; the one place the owner cannot test (the image) is built in CI.
4. Each later step is a line, not a rewrite: `highspy` behind the same interface; Mosquitto as a compose service plus one lifespan task; SSE on top of the existing queries; Parquet + DuckDB from the nightly job; RQ + Valkey and PostgreSQL + TimescaleDB when section 13's numbers arrive.

## 13. What would change at 1,000 sites

The arithmetic, with the measured numbers (sections 1 and 2), assuming 1,000 sites across roughly 200-300 companies, 10 channels, one solve per site per 15 minutes:

| Load | Pilot (3 sites) | 100 sites | 1,000 sites | What breaks and what replaces it |
|---|---|---|---|---|
| Solver CPU | 3 solves x 5 s per 15 min = 1.7 % of one core | 100 x 5 s = 500 CPU-s per 900 s window = 0.6 core | 5,000 CPU-s per 900 s = **5.6 cores at 100 %, ~8-12 vCPU on a VPS** with the 1.5-2x slowdown and 50 % headroom | A thread pool in one container cannot be the only solver. (1) Cut the work: re-solve on change only (a new forecast, a tariff period boundary, an event) and otherwise re-issue the plan's next step, which the OpenEMS state machine does (R4 A10); a 24-hour horizon halves the time (R1); warm starts through `highspy` cut the heuristic time (UNVERIFIED by measurement). (2) Scale out: `rq` 3.0.0 workers on `valkey` (compose `worker` service, 2-4 per machine, several machines), queues `optimise / forecast / report`, workers open the company files read-only and `POST /internal/plans` to the API (single writer kept) or the storage moves to PostgreSQL where many writers are fine |
| Ingest | 3 POST/min | 100 POST/min = 1.7 req/s | 1,000 POST/min = **17 req/s, 1,000 rows/s** | Trivial for SQLite's write rate (10,000 single-row/s measured, 230,000/s batched) but 17 registry reads/s through the guard: the 60-second key cache (section 10) is mandatory; nginx `limit_req` per key; the MQTT path (Mosquitto) becomes the better transport because 1,000 persistent sessions with QoS 1 buffer across API restarts |
| Hot storage (13 months of 1-minute rows) | 170 MB | 5.7 GB | **57 GB** in SQLite (wide); ~11 GB columnar | Disk is not the limit (a 500 GB volume), the daily copy is: 57 GB at ~70 MB/s is 14 minutes of I/O and a doubled disk every night. Options: (a) keep per-company files but back up incrementally (`sqlite3_rsync` in SQLite 3.47+, or copy only files changed since the last copy and snapshot the volume); (b) move telemetry to PostgreSQL 18 + TimescaleDB 2.30 Community with columnstore compression (claimed 90-98 %), continuous aggregates for `daily` and `q15`, a retention policy at 13 months, `pg_basebackup` + WAL archiving for backups; tenancy becomes a `company_id` column with RLS, the workspace stays in SQLite or moves too |
| Fleet analytics | the founder console's counts | DuckDB over the Parquet archive | Fleet-wide feeder outage model, tariff studies, the "every site is a sensor" outage forecaster across companies | Parquet + DuckDB is enough for batch studies; TimescaleDB if the product needs cross-company live queries (an installer portal with 50 companies) |
| Real-time UI | polling | polling + SSE in one process | SSE across several API processes | Valkey pub/sub as the SSE bus; or keep one API process per shard of companies (sharding by company is natural with per-company files) |
| Process model | one container | one container + staging | several API containers (sharded by company folder) or one Postgres-backed API with N uvicorn workers | The per-company-file design shards cleanly: `companies/` split across containers by hash, nginx routes by the `X-(PREFIX)-Company` header; the alternative is the Postgres move, after which `--workers 4` and many containers are plain |
| Reports | a few PDFs a month | 100 statements overnight at ~1 s each | 1,000 statements = ~20 minutes single-threaded | The `report` queue on RQ; ReportLab is CPU-light |
| Backups off-machine | MBs | ~6 GB/night | ~60 GB/night full, or incremental | Incremental (restic) rather than full copies; object storage lifecycle rules |

**The trigger points, in order.** (1) When a second writing process is wanted for any reason (workers, a second ingest path), move the queue to RQ + Valkey and keep the API as the single writer, or move storage to PostgreSQL + TimescaleDB; (2) when the nightly copy of telemetry files exceeds ~15 minutes or the disk doubles become the cost driver, move telemetry to TimescaleDB; (3) when solves exceed 50 % of the container's CPU, add workers; (4) when the SSE fan-out needs a second process, add Valkey pub/sub. None of these is a rewrite of the engine, the tariff code, the forecasters, the sign-in or the pages; the storage move touches the telemetry store module and the backup scripts.

## 14. Licence notes (consolidated)

Rules the product follows (Product 1 report lines 365-374 and the guideline's licence paragraph): MIT, BSD, Apache-2.0 may be copied and modified with notices kept; MPL-2.0 unmodified; LGPL-3.0 in test tooling only; EPL-2.0 patterns only; GPL-3.0 and EUPL-1.2 learn only; AGPL-3.0 never.

| Component | Licence (verbatim where quoted) | Status for a closed SaaS |
|---|---|---|
| SQLite | public domain | fine |
| HiGHS (inside scipy and as `highspy`) | "Copyright (c) 2025 HiGHS under MIT licence terms" (solver banner); `highspy` MIT; `highspy-extras` "Apache-2.0 AND BSD-3-Clause" ("distributed under the Apache 2.0 license, due to the dependencies' licensing", release notes v1.15.x) | fine |
| scipy, numpy, pandas, pvlib, scikit-learn, skforecast, starlette, uvicorn, httpx, sse-starlette, python-dotenv, threadpoolctl | BSD-3-Clause (numpy: "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0") | fine |
| fastapi, pydantic, PyJWT, APScheduler, pytest, redis-py, valkey-py, rq-win, huey (its LICENSE is the MIT text, "Copyright (c) 2017 Charles Leifer"), Recharts, React, Vite, TanStack, zustand, zod, jspdf, html2canvas, uPlot, Plotly.js, visx | MIT | fine |
| cryptography | "Apache-2.0 OR BSD-3-Clause" | fine |
| pyarrow, python-multipart, xgboost, asyncpg, playwright, tenacity, questdb client, ortools, TypeScript, Playwright | Apache-2.0 | fine |
| ECharts, lightweight-charts | Apache-2.0 | fine (keep the NOTICE) |
| matplotlib | PSF-style matplotlib licence | fine |
| reportlab, WeasyPrint | BSD | fine |
| lucide-react, d3 | ISC | fine |
| paho-mqtt | "EPL-2.0 OR BSD-3-Clause" (choose BSD) | fine |
| Mosquitto (broker, run unmodified as a service) | "This project is dual licensed under the Eclipse Public License 2.0 OR the Eclipse Distribution License 1.0" (`SPDX-License-Identifier: EPL-2.0 OR BSD-3-Clause`) | fine; not linked into the product |
| PostgreSQL | PostgreSQL License (permissive) | fine |
| TimescaleDB Apache 2 Edition | Apache-2.0 | fine |
| TimescaleDB Community Edition | Timescale License: "You can install TimescaleDB Community Edition in your own on-premises or cloud infrastructure and run it for free", "You cannot sell TimescaleDB Community Edition as a service" | allowed for internal use under the EMS; not open source; do not expose the database itself to customers |
| DuckDB | MIT | fine |
| QuestDB core | Apache-2.0 | fine (not chosen) |
| InfluxDB 3 Core | MIT or Apache-2.0 (dual, per InfluxData) | fine (not chosen; the 72-hour query window) |
| Redis 8 | tri-licence "RSALv2; or (b) SSPLv1; or (c) AGPLv3" (Redis 7.2 and earlier BSD-3) | avoid; use Valkey |
| Valkey | "BSD 3-Clause License" | fine |
| EMQX 5.9+ / 6.x | Business Source License 1.1, Additional Use Grant for "a single node ... provided your use does not include offering the Licensed Work to third parties on a hosted or embedded basis", Change License Apache-2.0 after four years | avoid |
| Celery | BSD-3-Clause | fine but unsupported on Windows; not chosen |
| dramatiq | LGPL-3.0-or-later (classifier) | excluded by the product's LGPL rule |
| psycopg 3 | LGPL-3.0-only | excluded by the rule; `asyncpg` (Apache-2.0) instead if Postgres comes |
| fpdf2 | LGPL-3.0-only | excluded |
| python-mip, cbcbox | EPL-2.0 | not chosen |
| vis-timeline | "(Apache-2.0 OR MIT)" | fine (not chosen) |
| The lifted Solarlytics module | OnyxLeaf's own (no LICENSE file in the repository); the founder token format is a port of Boxlytics' `founder-auth.ts` (`boxlytics@c429bef`), a second reuse to record in DESIGN_LOG for the owner's confirmation (U2 section 7) | owner's decision |

## 15. Open questions and everything marked UNVERIFIED

For the owner:

1. **The second SQLite file per company for telemetry** (section 3) against telemetry inside the workspace file: this note recommends the second file; it changes `backups.py` (copy two files) and adds a second ContextVar path to the lifted `store` module.
2. **The scheduler inside the API process** (section 5): accepted only with `(PREFIX)_SCHEDULER=1` on exactly one container per data folder; the `--scratch` launcher copy must run with it off.
3. **Solver pins**: `scipy==1.18.1` (latest) or `scipy==1.17.1` (measured). The CI benchmark job settles it either way.
4. **Ports** 8051/5193 (scratch 8151/5293) proposed by U2; Panelytics' ports were not checked.
5. **The object store for off-machine backups** (section 11) and the retention the owner wants.
6. **Whether to keep `y` and `s` as binaries** by default (section 1.3 item 7): redundant at Pakistani prices, needed if an export credit ever exceeds an import price or a site forbids grid charging.
7. **The frontend sign-in pages**: lift from the Solarlytics web repository (not on this machine).

UNVERIFIED in this note (each is marked where it appears):

- The HiGHS version bundled in scipy 1.18.1 (1.17.1 prints 1.12.0).
- Solve times on the production VPS (the 1.5-2x factor is an assumption); whether a `highspy` warm start from the previous plan shortens the root heuristics (expected, not measured).
- Real Parquet + zstd size per point (the 1.94 B/point figure is a zlib `.npz` proxy; pyarrow is not installed in `ops`); the exact tree-shaken ECharts bundle size for the series used; the Chromium footprint in a Playwright image.
- CVXPY canonicalisation time at this model size (not installed).
- redis-py 8.1 against Valkey 8 (wire-compatible by design; not tested here); RQ 3.0's minimum Redis server version (its changelog states only `redis-py >= 5.0.1`).
- Whether exposing a Mosquitto or EMQX broker to customers' devices counts as "offering the Licensed Work to third parties" under EMQX's BSL (a legal reading; Mosquitto has no such clause and is the recommendation).
- The `eclipse-mosquitto` Docker image (not re-checked); the ECharts `custom-gantt-flight` example (not re-opened).
- The Solarlytics web repository's sign-in pages lifting cleanly (U2 item 7).
- Which ports are free of the owner's other products.

## 16. Sources (all read on 6 November 2026)

Local, read-only:

- `C:/Users/SaadTahir/Videos/solarytics/Perfect-EMS_TOU_Dispatch_Deep_Review.md` (lines 98-192) and `Product 1 - TOU-Aware Energy Management and Dispatch.md` (lines 309-466, 557-576).
- `C:/Users/SaadTahir/Videos/solarytics/plan-work/research/U2-backends.md`, `R1-evcc-optimizer.md`, `R4-architecture-refs.md`, `U1-boxlytics-frontend.md`, `R8-ux-pages.md`.
- `C:/Users/SaadTahir/Videos/boxlytics/package.json`; `C:/Users/SaadTahir/Videos/solarlytics-backend/requirements.txt`, `Dockerfile`, `docs/DEPLOY.md` (head), `backend/solarlytics/docx.py` (head); `C:/Users/SaadTahir/Videos/boxlytics-backend/requirements.txt`.
- Benchmark scripts: `C:/Users/SaadTahir/Videos/solarytics/plan-work/bench/milp_bench.py`, `milp_bench_variants.py`, `milp_bench_seeds.py` (new); raw outputs were kept in the session scratchpad (`bench_main.txt`, `bench_variants.txt`, `bench_seeds.txt`, `sqlite_bench.txt`, `versions_pypi.txt`, `versions_npm.txt`), which is wiped between sessions (memory note `scratchpad-wipe-trap.md`); every number from them is copied into sections 1 and 2.

Registries and size API (one JSON request per package): `https://pypi.org/pypi/<name>/json`; `https://registry.npmjs.org/<name>`; `https://bundlephobia.com/api/size?package=<name>@<version>`.

Documentation and licence pages:

- scipy `milp`: https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html
- HiGHS releases: https://github.com/ERGO-Code/HiGHS/releases
- PuLP project page: https://pypi.org/project/PuLP/ ; CVXPY solvers: https://www.cvxpy.org/tutorial/solvers/index.html
- SQLite limits and WAL: https://www.sqlite.org/limits.html , https://www.sqlite.org/wal.html
- TimescaleDB editions and licence: https://www.tigerdata.com/docs/about/latest/timescaledb-editions ; install platforms: https://www.tigerdata.com/docs/self-hosted/latest/install ; hypercore compression claim: https://www.tigerdata.com/docs/use-timescale/latest/hypercore
- DuckDB concurrency: https://duckdb.org/docs/2.0/connect/concurrency.html
- QuestDB licence: https://github.com/questdb/questdb/blob/master/LICENSE.txt ; Windows quick start: https://questdb.com/docs/quick-start
- InfluxDB 3 Core: https://docs.influxdata.com/influxdb3/core/ , https://docs.influxdata.com/influxdb3/core/get-started/query/ , https://www.influxdata.com/blog/influxdb3-open-source-public-alpha , https://influxdata.com/blog/influxdb3-open-source-public-alpha-jan-27
- Mosquitto licence and downloads: https://github.com/eclipse-mosquitto/mosquitto/blob/master/LICENSE.txt , https://mosquitto.org/download/
- EMQX licence and platforms: https://github.com/emqx/emqx/blob/master/LICENSE , https://docs.emqx.com/en/emqx/latest/deploy/install.html
- Redis licence: https://github.com/redis/redis/blob/unstable/LICENSE.txt ; Valkey: https://github.com/valkey-io/valkey/blob/unstable/COPYING
- RQ workers and changelog: https://python-rq.org/docs/workers/ , https://github.com/rq/rq/blob/master/CHANGES.md
- Celery FAQ (Windows): https://docs.celeryq.dev/en/stable/faq.html ; dramatiq: https://dramatiq.io/installation.html and the Windows notes found by search (https://www.simplethread.com/queue-the-dramatiq-title/ , https://gitlab.com/bersace/flask-dramatiq/-/issues/10)
- huey: https://huey.readthedocs.io/en/latest/ , https://github.com/coleifer/huey/blob/master/LICENSE
- APScheduler: https://pypi.org/project/APScheduler/
- sse-starlette README (nginx buffering, ping): https://github.com/sysid/sse-starlette
- uPlot README (performance): https://github.com/leeoniya/uPlot ; ECharts renderer guidance: https://echarts.apache.org/handbook/en/best-practices/canvas-vs-svg/ ; ECharts sampling and progressive rendering (secondary sources found by search: https://www.mintlify.com/apache/echarts/guides/performance , https://dev.to/said96dev/optimizing-line-chart-performance-with-lttb-algorithm-21dj); Recharts performance (secondary, vendor): https://lightningchart.com/blog/react-charting-performance/ , https://dev.to/charttech/best-react-chart-libraries-ranked-by-performance-and-use-case-30cb
- WeasyPrint Windows install: https://doc.courtbouillon.org/weasyprint/stable/first_steps.html
- Python releases: https://www.python.org/downloads/
