# R6 — Executor (Mode A TOU slot plans, fallback and safety rules) and Battery State Service

Research notes for the OnyxLeaf EMS plan (Product 1: TOU-, outage- and genset-aware dispatch optimiser for Pakistani C&I sites with PV + LFP + unreliable grid + diesel genset). Written 6 November 2026. This file is self-contained: a reader who has not seen the brief should be able to use it alone.

Scope of this note (task R6):

1. Executor reference code read from four shallow clones (Victron `dbus-systemcalc-py` and `dbus_generator`, kellerza `sunsynk`, wlcrs `huawei-solar-lib`), plus public Sungrow and Growatt register documentation.
2. A per-brand **Mode A "TOU slot plan" schema** with slot counts, resolution, value ranges and write cadence.
3. The **fallback and safety rule list** in plain words (re-implementation of the Victron load-shedding and genset start/stop logic, adapted to Pakistan).
4. The **battery-state service design** (inputs, algorithm, outputs: SoC with confidence band, usable kWh, SoH, BMS trust score).
5. The **wear-cost curve derivation** with Pakistani LFP prices.
6. **Licence notes** per source.

Conventions: file paths are given relative to the scratchpad clone root `C:/Users/SaadTahir/AppData/Local/Temp/claude/C--Users-SaadTahir-Videos/1143ee09-5b9b-42cf-b77e-04f40d670b5a/scratchpad/oss/` (these clones are throwaway; the line numbers below are from the commits listed in section 1). Anything not checked against a primary source is marked UNVERIFIED.

## 0. Summary of the answers (read this first)

- **Mode A is feasible on Deye/Sunsynk, Huawei LUNA2000 and Growatt SPH/TL-XH from day one, and on Sungrow SH only through its "compulsory" mode or the External-EMS heartbeat mode (no slot table exists on Sungrow).** The four brands expose four different "slot" models: Deye = 6 ordered time points with per-slot power, SoC target and charge-source flags; Huawei = up to 14 charge/discharge periods with a weekday mask and no power or SoC per period; Growatt = 3 windows each for Grid-First, Battery-First and Load-First with one SoC limit per mode; Sungrow = 2 forced-charging windows with target SoC, or live setpoints under a 20 s heartbeat. The optimiser must therefore emit an abstract 15-minute plan and a per-brand "compiler" must reduce it to what the inverter can hold (section 3.6).
- **Write cadence:** one full slot-table write per day (after the late-evening re-solve) plus at most two "event" rewrites (outage forecast change, genset start). Sungrow's protocol says its RW registers "do not support frequent periodic reading and setting" when forwarded through WiNet-S/Logger1000; Deye and Growatt persist slot registers to non-volatile memory (OEM write-count limits are UNVERIFIED, but the SMA warning in the guideline and a forum figure of 10^5 to 10^6 EEPROM cycles both argue for a budget of about 5 table writes per day).
- **Fallback:** re-implement Victron's two MIT state machines in Python: `LoadShedding` (prepare, pre-emptive disconnect, power fail, reconnect delay, recovery) and the genset `StartStop` condition stack (SoC, AC load, battery current, battery voltage, inverter temperature, overload, stop-on-AC, tank level, quiet hours, minimum run time, warm-up, cool-down, loss-of-communication). Section 4 gives the full rule list in words with the Victron defaults as starting values.
- **Battery state:** BMS SoC is a noisy input. The service runs coulomb counting on measured current (with efficiency and self-discharge terms) and an EKF on a 2-RC equivalent circuit, and only *trusts* voltage outside the LFP plateau (roughly below 15 % and above 90 % SoC, where dV/dSoC is large). It anchors at charge termination (CV tail current below C/20) and at the low knee, reports SoC with a confidence band, usable kWh, SoH from full-swing capacity tests plus partial-swing regression, and a BMS trust score in [0, 1] (section 5).
- **Wear cost:** a throughput price of **Rs 9 to 13 per kWh discharged** (Rs 52,000/kWh installed LFP, 6,000 cycles at 80-90 % DoD, 80 % end of life); the range is Rs 6 to 23 across the price and cycle-life spread. BLAST-Lite's LFP model makes the depth-of-discharge dependence weak (about +35 % from 0 to 100 % DoD) but puts a real price on calendar ageing at high SoC and high temperature: holding a pack at 90-100 % SoC at 45 C costs about Rs 9.5 per kWh of pack per day versus Rs 3.3 at 20 % SoC, which is why the optimiser should carry a per-step "SoC holding" cost in summer and not only a per-kWh cycling cost (section 6).
- **Licences:** everything to be *re-implemented* (Victron MIT, kellerza Apache-2.0, ha-solarman MIT, mkaiser MIT, solax-modbus Apache-2.0) is fine with attribution; the one to *avoid importing* is `huawei-solar` (AGPL-3.0): read its register tables, write our own Modbus client. BLAST-Lite, PySAM and PyBatterySE are BSD-3; `rainflow` is MIT; `ffpack` is GPL-3 and must not be used (section 7).

## 1. Sources read for this note

| Source | Commit / version read | Date | Licence (file read) | What was used |
| --- | --- | --- | --- | --- |
| victronenergy/dbus-systemcalc-py (clone `dbus-systemcalc-py/`) | 6428ee2b54d0fb67e4e6dcc6dba99d3b227f4661 | 2026-11-02 | MIT, `LICENSE`, "Copyright (c) 2014 mpvader" | `delegates/loadshedding.py` (383 lines) |
| victronenergy/dbus_generator (clone `dbus_generator/`) | b4de4cd5ce7f0dff20cfdff06cab91d9a63c4834 | 2026-05-27 | MIT, `LICENSE`, same copyright line | `startstop.py` (1,448 lines), settings table in `dbus_generator.py` lines 115-187 |
| kellerza/sunsynk (clone `sunsynk/`) | f1ecbc28e3ccf2b9f87b1ccee85a41628567da43, `pyproject.toml` version 1.3.0 | 2026-10-24 | Apache-2.0, `LICENSE` | `src/sunsynk/definitions/{__init__,single_phase,three_phase_common}.py`, `rwsensors.py`, `helpers.py` |
| wlcrs/huawei-solar-lib (clone `huawei-solar-lib/`) | b383fd479f8c87c8447e057a702d9431b5f42a2a; latest GitHub release v3.0.7 (2026-08-11) | 2026-11-04 | **AGPL-3.0**, `LICENSE.md` | `src/huawei_solar/registers.py`, `register_definitions/periods.py`, `register_values.py`, `register_names.py`, `README.md` |
| mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant (pre-existing clone `mkaiser-sungrow/`) | 23479a9ac1e78a3f8b4975046d6bb0a7ab751a2b | 2026-10-30 | MIT, "Copyright (c) 2025 Martin Kaiser" | `modbus_sungrow.yaml` lines 1147-1180, 1230-1290, 2173-2260 |
| Sungrow "Communication Protocol of Residential Hybrid Inverter" V1.1.11 (PDF hosted on forum.iobroker.net; copy at scratchpad `sungrow_pdf/sungrow_v1111.pdf`) | V1.1.11 | 2025-11-17 | Proprietary; cover says it shall not be distributed or forwarded: read for facts only, do not redistribute | Table 4 (pp. 12-14), section 3.5 and Table 9 (pp. 20-21), Appendix 6 (p. 33) |
| wills106/homeassistant-solax-modbus (pre-existing clone `solax-modbus/`) | 0056f9fb2485d1fbaa9fef29272ce686eefe7cf0 | 2026-11-04 | Apache-2.0 | `custom_components/solax_modbus/plugin_growatt.py` (Growatt SPH/TL-XH registers) |
| davidrapan/ha-solarman (pre-existing clone `ha-solarman/`) | ac33d9a1ac7663f2763d0e34eb0ee70c570c08af | 2026-11-04 | MIT, "Copyright 2024 David Rapan" | `inverter_definitions/deye_hybrid.yaml` lines 1660-2180 (value ranges and bit masks) |
| kbialek/deye-inverter-mqtt (pre-existing clone `deye-inverter-mqtt/`) | 7743bd29cda3e4b2821f9f64249d09aa859174cd | 2026-11-05 | Apache-2.0 | `src/deye_sensors_deye_hybrid.py` lines 270-560 (cross-check of 250-279) and GitHub issue #170 (HV three-phase 146-177, power unit 10 W) |
| NREL/BLAST-Lite (clone `blast-lite/`) | 991591726c07c74b5d8131e9090fee190d2e5d57, `pyproject.toml` version 1.1.3 | 2026-11-02 | BSD-3-Clause, `LICENSE` "Copyright (c) 2023, Alliance for Energy Innovation, LLC"; `NOTICE` carries the DOE contract clause | `blast/models/lfp_gr_250AhPrismatic_2019.py`, `lfp_gr_SonyMurata3Ah_2018.py`, `degradation_model.py`, `utils/rainflow.py`, `README.md` Caveats |
| NREL/pysam | latest release v7.1.1 (2026-04-28), PyPI `nrel-pysam`, Python 3.9-3.14 | - | BSD-3-Clause (GitHub licence badge) | `BatteryStateful` docs page, `Examples/BatteryStateful.ipynb` |
| iamlikeme/rainflow | PyPI `rainflow` 3.2.0 (2023-04-17), Python >= 3.7 | - | MIT | API `count_cycles`, `extract_cycles`; the same code is vendored in BLAST-Lite `blast/utils/rainflow.py` |
| tue-battery/PyBatterySE | GitHub licence API: BSD-3-Clause (`LICENSE.txt`); PyPI `pybatteryse` 2.0.0 | - | BSD-3-Clause | EKF/PF SoC estimation on LPV models, LFP example dataset |
| rlabbe/filterpy | GitHub `LICENSE` read 2026-11-06 | - | MIT, "Copyright (c) 2015 Roger R. Labbe Jr" | generic `ExtendedKalmanFilter` class (section 5.4) |
| pybamm-team/PyBaMM | GitHub licence API and docs parameter_sets page, 2026-11-06 | - | BSD-3-Clause, "Copyright (c) 2018, the PyBaMM team." | Prada2013 LFP parameter set (bench OCV, section 5.4) |
| Gunnstein/fatpack, ffpack 0.3.3 (PyPI) | GitHub `LICENSE`; PyPI JSON, 2026-11-06 | - | ISC; GPL-3.0 classifier | rainflow alternatives (section 6.7) |
| Xu, Zhao, Zheng, Litvinov, Kirschen, IEEE TPWRS 2018 (arXiv 1707.04567v2) | PDF read 2026-11-06 | - | IEEE | cycle-depth stress function and piecewise-linear segments, eq. 1, 4, 5, 7, 24 (section 6.2) |
| SolarCitizen "Lithium battery price in Pakistan" | updated 2026-10-16 | - | - | Rs 45,000-65,000/kWh installed, typical Rs 52,000; bank price table |
| BloombergNEF battery price survey 2025-12-09 (via energy-storage.news, pv-magazine) | - | - | - | stationary pack US$70/kWh, LFP pack average US$81/kWh, turnkey BESS US$117/kWh |
| Victron Dynamic ESS docs (victronenergy.com/live/drafts:dynamic_ess) | fetched 2026-11-06 | - | - | "run out of its schedule after 12 hours and revert to standard ESS operation" |
| wlcrs/huawei_solar wiki "Time-of-Use control" | fetched 2026-11-06 | - | - | TOU service semantics, 23:59 end-time quirk, 5-minute FusionSolar refresh |

Tooling gaps: `gh` is not installed and the `ops` env has no pypdf/pdfminer; the Sungrow PDF was read page by page with the Read tool, GitHub metadata through the public REST API.

## 2. Victron rule logic to re-implement (MIT, keep the notice)

### 2.1 `delegates/loadshedding.py`: scheduled-outage handling

What it is: a `SystemCalcDelegate` that, given up to four scheduled outage windows, (a) pre-charges the battery before the window, (b) disconnects the AC input a few minutes *before* the scheduled cut so the inverter is already islanded when the grid drops, (c) waits for the grid to actually fail, (d) after power returns waits a stability margin before reconnecting, and (e) releases control.

| Setting (dbus path under `/Settings/LoadSheddingApi`) | Default | Range | Meaning | Line |
| --- | --- | --- | --- | --- |
| `NUM_SCHEDULES` | 4 | - | four (start, duration) windows: start as Unix timestamp, duration in seconds | 9, 115-119 |
| `INTERVAL` | 5 s | - | timer tick | 10 |
| `/Mode` | 0 | 0-1 | off/on | 98 |
| `/PreparationTime` | 3600 s | - | how long before the window to begin charging to `MinSoc` | 101 |
| `/DisconnectMargin` | 300 s | - | disconnect this long *before* the scheduled start; the window is extended by the same amount | 103, 159-162 |
| `/ReconnectMargin` | 1800 s | - | if the grid never fails, reconnect this long after the scheduled start ("early recovery") | 105, 330-332 |
| `/StabilityMargin` | 60 s | - | after power returns, wait this long before reconnecting | 107, 336-350 |
| `/MinSoc` | 0 | 0-100 | SoC to hold going into the outage | 109 |

States (`ACTIVE`, lines 14-21): 0 Off, 1 Preparing, 2 Pre-emptive disconnect, 3 Power fail, 4 Reconnect delay, 5 Recovery.

Transition logic (`_on_timer`, lines 290-372), in words:

1. Build the list of windows whose end is still in the future; each window starts `DisconnectMargin` early and lasts `duration + DisconnectMargin` (lines 153-167). Publish `NextDisconnect`.
2. If now is inside a window and control can be acquired: cancel preparation overrides (`ForceCharge=0`, `MaxDischargePower=-1`), go to state 2 and disconnect the grid input (`set_ignore_ac(True)` on every grid/shore input, lines 253-263). In state 2, if AC is no longer available go to 3 (power fail); if AC is still available `ReconnectMargin + DisconnectMargin` after the scheduled start, go to 5 and reconnect (the DISCO did not cut). In state 3, when AC comes back start the stability timer and go to 4; firmware without AC detection falls back to the time-based recovery. In state 4, count down `StabilityMargin`, then reconnect and go to 5.
3. Else, if a window starts within `PreparationTime`: reconnect if still disconnected, go to state 1 and call `prepare()`.
4. Else: reconnect if needed, clear overrides, release control, state 0.

`prepare()` (lines 265-288) is the SoC-holding rule worth copying in spirit:

- if `soc + 1.0 < minsoc`: `ForceCharge = 1`, no discharge limit;
- else if `soc < minsoc`: keep whatever ForceCharge was (1 % hysteresis); else `ForceCharge = 0`;
- if force-charging or `soc > minsoc + 4.0`: no discharge limit;
- else limit discharge to roughly PV power so SoC is held: `scale = min(1.0, 0.8 + max(0, soc - minsoc) * 0.05)`, `MaxDischargePower = max(1, round(pvpower * scale))`, i.e. at exactly minsoc only 80 % of PV may be passed to loads from the battery side, rising to 100 % at minsoc + 4 %.

What it does *not* do (gaps for Pakistan): no outage *probability* (windows are deterministic), no genset interplay, no critical-load energy calculation (MinSoc is a flat number), and `MultiRs.prepare()` is a no-op because changing the ESS mode is a flash write (comment at lines 60-62: "that is a flash write. So for now, not supported"), the same flash-wear concern the guideline raises.

### 2.2 `dbus_generator/startstop.py`: genset auto start/stop

Structure: an ordered `_condition_stack` (lines 339-350, comment "Order is important. Conditions are evaluated in the order listed"): `soc`, `acload`, `batterycurrent`, `batteryvoltage`, `inverterhightemp`, `inverteroverload`, `stoponac1`, `stoponac2`; plus `tanklevel` evaluated separately (line 350). Each condition declares `boolean` (alarm-type) and `timed` (needs start/stop timers) flags (lines 93-267).

Generic evaluation (`_evaluate_condition`, lines 918-963), in words:

- Each value condition has a *start value* and a *stop value*; direction is inferred: if start > stop (AC load, current) the genset starts when value >= start and stops when value <= stop; if start < stop (SoC, voltage) it starts when value <= start and stops when value >= stop. Boolean conditions use 1/0.
- Timed conditions must hold for `<name>starttimer` seconds before starting and `<name>stoptimer` seconds before stopping (hysteresis in time as well as value).
- During quiet hours the `qh_` variants of the start/stop values apply (lines 921, 1062-1082; quiet hours default 21:00 to 06:00, i.e. 75600 s to 21600 s, settings lines 122-123).
- A condition that cannot be read keeps the genset running for up to `RETRIES_ON_ERROR` ticks (lines 929-933); if an enabled condition's retries reach the limit, `connection_lost` is set and `onlosscommunication` decides: 1 = start, 2 = keep running if already running (lines 729-737).

Precedence (`_evaluate_startstop_conditions`, lines 650-758):

1. Manual start wins (unless stopped by tank level). 2. Test run (periodic exercise, settings lines 174-180: interval in days, duration, optional "run till battery full"). 3. Stop-on-AC-input is evaluated first: if grid is present on AC-in-1/2 and the genset was not started manually or by test run, every other condition is reset and the genset stops (log line 700 "AC input available, stopping"). 4. Value conditions in stack order; the first one reached names `RunningByCondition`. 5. Loss of communication. 6. Tank level: `value <= tanklevelstop` stops it and raises an alarm; `tanklevelpreventstart` blocks restarts until the tank is above it (lines 905-916). 7. A stop is only honoured after `minimumruntime` minutes since start, except for manual (lines 756-758).

Warm-up / cool-down (`_start_generator` lines 1300-1336, `_stop_generator` 1338-1387): on start, if `warmuptime > 0` the genset AC input is *ignored* (load stays on battery) for `warmuptime` seconds, then released (WARMUP to RUNNING); the overload condition can skip warm-up (`inverteroverloadskipwarmup`). On stop, if `cooldowntime > 0` the load is taken off first (COOLDOWN), the engine is stopped `cooldowntime` later (STOPPING), and the AC input is re-enabled only after a further `generatorstoptime`, so the inverter never re-syncs to a dying engine. Ranges: warm-up 0-1800 s, cool-down 0-600 s, stop time 0-600 s (settings lines 185-187).

Default values to seed the rule table (from `dbus_generator.py` lines 125-166): SoC start 80 / stop 90 (quiet hours 90/90); battery voltage start 11.5 V / stop 12.4 V with 20 s timers (12 V system, scale by nominal); battery current start 10.5 A / stop 5.5 A (quiet 20.5/15.5) with 20 s timers; AC load start 1600 W / stop 800 W (quiet 1900/1200) with 20 s timers, measurement selectable between total consumption, inverter output or highest phase; inverter high-temperature and overload alarms with 20 s timers; minimum run time 0 (0-86400 min).

Mapping to the EMS: the optimiser decides the *planned* genset on/off per 15 minutes; this rule stack is the *local* safety net that (a) starts the genset when SoC or voltage falls through the floor during an outage the plan did not foresee, (b) stops it the moment the grid returns (stop-on-AC), (c) enforces minimum run, warm-up, cool-down and quiet hours regardless of the plan, and (d) blocks starts on low fuel.

## 3. Per-brand TOU registers and the Mode A "TOU slot plan" schema

All addresses below are Modbus *register numbers as the source lists them*. kellerza/sunsynk, ha-solarman and deye-inverter-mqtt use the raw Deye holding-register address; huawei-solar-lib uses the Huawei document address; the Sungrow document says "Visit all registers by subtracting 1 from the register address" (p. 5), and mkaiser's YAML already does so (`13049 # reg 13050`); the Growatt numbers are solax-modbus's `register=` values (document numbering, UNVERIFIED against Growatt's own PDF).

### 3.1 Deye / Sunsynk (single-phase LV: SUN-xK-SG0x LP1/LP3; three-phase LV/HV: SG04LP3, SG01HP3)

Source: `sunsynk/src/sunsynk/definitions/single_phase.py` lines 259-355 and `three_phase_common.py` lines 326-422; option tables in `definitions/__init__.py` lines 15-26; time encoding `helpers.py` `SSTime` lines 128-152 (register value = hours*100 + minutes, so 17:30 is 1730). Value ranges from `ha-solarman/.../deye_hybrid.yaml` lines 1762-2180 (time 0000-2359, power 0-12000 W on that profile, SoC 0-100).

| Field | Single-phase LV register | Three-phase register | Encoding and range | Notes |
| --- | --- | --- | --- | --- |
| Slot time 1-6 | 250-255 | 148-153 | HHMM as integer, 0000-2359; slot n+1 must be later than slot n and slot 1 wraps after slot 6 (`TimeRWSensor.min/max` chain, `single_phase.py` 286-298) | Each time is the *start* of the slot; it runs until the next slot's start |
| Slot power 1-6 | 256-261 | 154-159 | W, 0 to rated power (`max=RATED_POWER`, regs 16-17 x 0.1) | On HV three-phase the unit is 10 W (deye-inverter-mqtt issue #170: "unit of Time of Use Power 1-6 is 10W, in contrast to W in the LV settings"). Meaning: maximum battery discharge power during the slot |
| Slot voltage 1-6 | 262-267 | 160-165 | 0.01 V, between `BATTERY_LOW_VOLT` and `BATTERY_FLOAT_VOLT` | Only used when the battery is voltage-controlled (no BMS); ignore for lithium |
| Slot capacity (SoC) 1-6 | 268-273 | 166-171 | %, min = `BATTERY_LOW_CAP` (reg 219, max 50) up to 100 | Target SoC: discharge stops at this SoC; with grid/gen charge allowed the battery is charged *up to* it |
| Slot charge flags 1-6 | 274-279 bits 0-1 | 172-177 bits 0-1 | 0 "No Grid or Gen", 1 "Allow Grid", 2 "Allow Gen", 3 "Allow Grid & Gen" (`PROG_CHARGE_OPTIONS`) | The grid-charge enable per slot |
| Slot mode 1-6 | same regs, bits 2-4 (mask 0x1C) | same, mask 0x1C | 0 None, 4 General, 8 Backup, 12 Charge (`PROG_MODE_OPTIONS`) | Semantics UNVERIFIED beyond the names |
| Slot sell 1-6 | same regs, bit 6 (0x40) | same, bit 5 (0x20) | on/off | "sell" = allow export during the slot (note the different bit between profiles) |
| Use Timer (TOU enable) | 248 bit 0 | 146 bit 0 | 0/1 | Master enable; on three-phase reg 146 bits 1-7 are Mon..Sun enables (`three_phase_common.py` 353-366); ha-solarman shows the same byte on 0x00F8 (248) for its hybrid profile, with 0x00FF = all week + enabled |
| Priority Load | 243 bit 0 | 141 | 0 Battery First, 1 Load First | Energy pattern |
| Load Limit | 244 | 142 | 0 Allow Export, 1 Essentials, 2 Zero Export | Export control |
| Max Sell power | 245 | - | W, 0-rated | Export cap |
| Solar Export | 247 bit 0 | - | 0/1 | Must be on for any export |
| Battery low / shutdown / restart capacity | 219 / 217 / 218 | - | %, shutdown <= low <= 50 | The floors the executor must never undercut |
| Grid / Gen peak shaving | 280 (bits), 292-293 (W) | - | enable bits and power | Useful for MDI capping |

Mode A plan on Deye: six (start_time, max_discharge_W, soc_target, grid_charge, gen_charge, sell) tuples that cover 24 h in ascending order, written with one FC16 block write for regs 250-279 (single-phase) or 148-177 (three-phase) after reading them back. Resolution: 1 minute on the clock, but six boundaries per day. Deye manuals say the times "must [go] from small to large" (via the deye-modbus-ha search result; UNVERIFIED wording).

### 3.2 Huawei SUN2000 + LUNA2000 (via SDongle, EMMA, or the inverter's WLAN AP)

Source: `huawei-solar-lib/src/huawei_solar/registers.py` lines 517-593, 667-692; `register_definitions/periods.py` lines 28-46, 141-259, 262-321, 324-454; `register_values.py` lines 82-147.

| Register (address, type) | Name in the lib | Encoding and limits |
| --- | --- | --- |
| 47086 U16 RW | `STORAGE_WORKING_MODE_SETTINGS` (`StorageWorkingModesC`) | 0 Adaptive, 1 Fixed charge/discharge, 2 Maximise self-consumption, 3 TOU (LG), 4 Fully fed to grid, **5 TOU (LUNA2000)**, 6 Third-party control |
| 47255-47297 (43 regs) RW | `STORAGE_HUAWEI_LUNA2000_TIME_OF_USE_CHARGING_AND_DISCHARGING_PERIODS` | count (U16) then **14 periods** of (start_min U16, end_min U16, charge_flag U8 {0 charge, 1 discharge}, days U8 bitmask bit0 = Sunday .. bit6 = Saturday). Lib validation (`periods.py` 179-222): 0 <= start < end <= 1440, no overlap per weekday. Field quirk (wlcrs wiki): end the last period at 23:59 not 00:00 ("unknown error" otherwise). Huawei manuals (LUNA2000-5/30-S0 and 2.0MWh, via search) also say "A maximum of 14 time segments can be set" and that outside set segments the battery does not discharge |
| 47299 U16 RW | `STORAGE_EXCESS_PV_ENERGY_USE_IN_TOU` | 0 fed to grid, 1 charge |
| 47087 U16 RW | `STORAGE_CHARGE_FROM_GRID_FUNCTION` | bool; must be 1 for grid-charge periods |
| 47088 U16 RW, gain 10 | `STORAGE_GRID_CHARGE_CUTOFF_STATE_OF_CHARGE` | %, 0.1 % resolution: the SoC at which grid charging stops (the *only* SoC target in TOU mode, global not per period) |
| 47242 / 47244 U32 RW | `STORAGE_POWER_OF_CHARGE_FROM_GRID` / `..._MAXIMUM_...` | W |
| 47075 / 47077 U32 RW | `STORAGE_MAXIMUM_CHARGING_POWER` / `..._DISCHARGING_POWER` | W |
| 47081 / 47082 U16 RW, gain 10 | charging / discharging cutoff capacity | % |
| 47102 U16 RW, gain 10 | `STORAGE_BACKUP_POWER_STATE_OF_CHARGE` | % reserve for backup |
| 47200-47240 (41 regs) RW | `STORAGE_FIXED_CHARGING_AND_DISCHARGING_PERIODS` | count then 10 x (start_min, end_min, power_W I32); used with working mode 1; this is the per-period *power* table TOU mode lacks |
| 47083 / 47084 | forced charge/discharge period (min) and power (W); 47100 forcible command {0 stop, 1 charge, 2 discharge}; 47246 target mode {0 time, 1 SoC}; 47247/47249 forcible charge/discharge power; 47101 forcible SoC | the "manual override" primitives for event rewrites |
| 47956-48019 (64 regs) RW | `STORAGE_CAPACITY_CONTROL_PERIODS` with 47954 mode and 47955 peak-shaving SoC | 14 x (start, end, peak_power_W I32, days U8); lib requires every weekday to be covered 00:00 to 23:59 contiguously (`periods.py` 396-432): the MDI-capping primitive |
| 47783 U16 RW, gain 10 | `STORAGE_RECHARGE_TARGET_SOC` | 40-60 % only (comment at `register_names.py` 435-447) |
| 37760 / 37004 U16 RO, gain 10 | pack SoC / unit-1 SoC | the BMS SoC reading |

Write prerequisites: the lib performs an "installer-level" login sequence (README line 30) before parameter writes; FusionSolar's own parameter refresh is 5 minutes, and users report app changes and Modbus changes overwriting each other (wlcrs wiki). Plan: write the 14-period table at most once a day; use 47100/47246/47247 for event overrides; read 47255 back after every write.

### 3.3 Sungrow SH (SH5-10RT, SH5-25T, SH3-10RS, MG5-10RL) via RS485 or WiNet-S/S2 Ethernet (Modbus TCP 502)

Source: Sungrow protocol V1.1.11 Table 4 (pp. 12-14) and Table 9 (p. 20-21); mkaiser YAML for the 0-based addresses.

| Register (document number) | Name | Values |
| --- | --- | --- |
| 13050 U16 RW | EMS mode selection | 0 Self-consumption (default), 2 Compulsory mode, 3 External EMS mode, 4 VPP ("the same as External EMS mode") |
| 13051 U16 RW | Charge/discharge command | 0xAA charge, 0xBB discharge, 0xCC stop |
| 13052 U16 RW | Charge/discharge power | 1 W, "0-100 % of BDC rated power (RO register 5628)"; mkaiser notes SH*K units take W, SH*.0RT units "0 to 100 %" per datasheet but W in practice on an SH10RT |
| 13058 / 13059 U16 RW | Max SoC / Min SoC | 50.0-100.0 % / 0.0-50.0 %, 0.1 % |
| 13100 U16 RW | Reserved SoC for backup | 0-100 % |
| 13080 U16 RW | External EMS heartbeat | 0-1000 s, default 20 s. Protocol p. 21: when 13050 is 3 or 4 "Deliver the heartbeat value within the timeout period ... If the heartbeat value is not delivered within the timeout period, the inverter returns to self-consumption mode. For example, if External EMS Heartbeat is set to 20s, you are advised to set the value to 20 every 10 seconds on average." |
| 13074 / 13087 / 13088 | Feed-in limitation value (W) / enable (0xAA/0x55) / ratio (0.1 %) | export cap |
| 13089 / 13090 | Active power limitation enable / ratio (0.1 %) | output cap |
| 13075 | Off-grid option 0xAA/0x55 | backup enable |
| 33208-33219 U16 RW | Forced charging: enable (0xAA/0x55), valid days (0 weekday, 1 every day), **two** windows of start h/min, end h/min and target SoC 0-100 % | the only *scheduled* primitive; protocol p. 21 item 4: "If you need to redefine the battery discharge and charge time period based on the electricity price difference ... you can set forced charging and its time period (register 33208-33219)" |
| 33047 / 33048 U16 RW | Max charging / discharging power, 0.01 kW | power caps |
| 13017 | Forced startup under low-SoC standby 0xAA | restart after deep discharge at night (grid-connected only) |

Constraints stated in the document: "The RW (Read-Write) registers ... do not support frequent periodic reading and setting when it is forwarded through communication modules such as WiNet-S/WiNet-S2/Logger1000" (p. 5 item 6); minimum request period 1000 ms (item 7); for master/slave multi-inverter systems the parameters are issued to the master only (p. 21). Community reports (loxforum, 2025) that External EMS mode broke after a firmware update are UNVERIFIED.

Mode A on Sungrow therefore reduces to: Max/Min SoC + reserved SoC + two forced-charge windows with target SoC + feed-in limit, written once a day. Peak-time discharge limiting is *not* expressible without the heartbeat mode; Product 2 (edge) is needed for anything finer, and it must send the heartbeat every 10 s and fall back to self-consumption automatically when the edge dies (this is Sungrow's built-in fail-safe and is the model for our own).

### 3.4 Growatt SPH / TL-XH (GEN3/GEN4 in solax-modbus's naming)

Source: `solax-modbus/custom_components/solax_modbus/plugin_growatt.py`. Three "priority" windows per mode, each a (begin HHMM, end HHMM, enable) triple:

| Mode | Window 1 | Window 2 | Window 3 | Mode-level SoC |
| --- | --- | --- | --- | --- |
| Grid First (discharge to grid/loads) | 1080 begin, 1081 end, 1082 enable | 1083-1085 | 1086-1088 | 1071 "Grid First battery minimum SoC" (%) ; discharge power limit 1070 (UNVERIFIED) |
| Battery First (charge, optionally from grid) | 1100, 1101, 1102 | 1103-1105 | 1106-1108 | 1091 "Battery First stop SoC" (%), 1092 "Charge from grid" 0/1, 1090 charge power limit (%) |
| Load First (self-use) | 1110, 1111, 1112 | 1113-1115 | 1116-1118 | - |

Lines: sensors 2455-2710 (addresses), selects 1391-1500 (enables), number entities around 1003-1030 for 1071/1090/1091. Encoding: `value_function_sofartime` = HHMM packed high byte hour, low byte minute (UNVERIFIED which packing Growatt uses; the 0xaha docs cited in search say HHMM 0-2359). On TL-XH GEN4 units a newer scheme packs enable and mode bits into the begin register (`encode_time_begin`, lines 180-200: +32768 enable, +8192 Battery First, +16384 Grid First) with the end in the next register, and the plugin validates `end >= begin`. Field reports: if no window is active the inverter defaults to Load First; register meanings differ between SPH and TL-XH firmware (search results). Official Growatt register PDF: not found online; UNVERIFIED.

Mode A on Growatt: up to 3 Battery-First windows (charge to 1091 %, grid charge per 1092 and power per 1090) and up to 3 Grid-First windows (discharge down to 1071 %); the rest of the day is Load First. No per-window power or SoC.

### 3.5 Victron (reference only)

Victron's ESS has `/Settings/CGwacs/BatteryLife/MinimumSocLimit`, `Hub4/.../MaxDischargePower`, scheduled charging via five `ScheduledCharge` slots (not read here) and the Dynamic ESS cloud schedule; Dynamic ESS "will run out of its schedule after 12 hours and revert to standard ESS operation" and recalculates "every few minutes how quickly it should (dis)charge to reach the target SOC at the end of the hour" (Victron docs). Keep as the UX and fallback precedent, not as a Mode A target.

### 3.6 The abstract Mode A plan and the per-brand compiler

```yaml
# TouSlotPlan v1  (what the optimiser emits; brand-agnostic)
site_id: str
device_id: str
plan_id: uuid
issued_at: iso8601            # cloud time
valid_from: iso8601           # start of the first slot (local time at site)
valid_until: iso8601          # valid_from + 24 h; the inverter keeps the last table after this
tz: "Asia/Karachi"
source_horizon_h: 48          # MILP horizon the plan was cut from
slots:                        # 1..N contiguous, covering 24 h, each a 15-minute multiple
  - start: "17:00"            # HH:MM local
    end:   "21:00"
    mode: discharge | hold | charge_pv_only | charge_grid | charge_gen | export
    soc_floor_pct: 20         # never discharge below (max of reserve, BMS low cap)
    soc_target_pct: 100       # charge up to (grid/gen) or down to (discharge)
    max_discharge_w: 25000
    max_charge_w: 15000
    grid_charge: true|false
    gen_charge: true|false
    export_allowed: true|false
    export_cap_w: 0
reserve:                       # from the outage-reserve constraint
  critical_kwh_next_outage: 36.0
  min_soc_until: "23:45"
  min_soc_pct: 45
mdi_cap_kw: 180               # month-to-date peak the plan must not exceed
fallback:                      # what the edge/inverter does if no new plan arrives
  revert_after_h: 3
  revert_mode: self_consumption
  revert_min_soc_pct: 30
write_budget:                  # executor enforces
  table_writes_per_day: 3
  event_writes_per_day: 2
signature: ed25519
```

Compiler rules per brand (the executor's job, in order):

1. **Merge** adjacent 15-minute steps with the same (mode, flags, target) into slots; **rank** the remaining boundaries by rupee value (peak-window start and end, outage-prep start, PV-surplus start) and **drop** the least valuable boundaries until the count fits: Deye 6, Growatt 3 + 3, Huawei 14, Sungrow 2 charge windows.
2. **Quantise** boundaries: Deye and Huawei 1 minute; Growatt 1 minute; Sungrow 1 minute (hour and minute registers). Keep every boundary on a 15-minute mark anyway so the verification replay lines up.
3. **Translate** targets: Deye uses per-slot `Capacity` = soc_target (discharge floor or charge ceiling) and `charge` flags; Huawei has one global grid-charge cut-off SoC (47088) so take the *maximum* soc_target of all grid-charge periods and express discharge floors via 47082 and backup SoC 47102; Growatt uses 1091 for the charge stop and 1071 for the discharge floor; Sungrow uses Max/Min SoC (13058/13059) plus the forced-charge target SoC.
4. **Clamp** every SoC to [BMS low cap + 2 %, 100 %] and every power to the inverter and BMS limits read at boot; never write a floor below the fallback `revert_min_soc_pct`.
5. **Read back** the whole table after writing and diff it; on mismatch retry once, then raise `PLAN_WRITE_MISMATCH` and keep the previous table.
6. **Budget**: at most `table_writes_per_day` full-table writes and `event_writes_per_day` single-register override writes (Huawei 47100/47246, Sungrow 13051/13052 in compulsory mode, Deye slot SoC only). The nightly write happens right after the 23:00 re-solve; an event rewrite is allowed only if the expected rupee gain exceeds a threshold (suggest Rs 500) *and* the budget is not exhausted.
7. **Validity**: Mode A tables persist on the inverter, so "plan expiry" is implemented by making the nightly table itself safe (floors, reserve) rather than by relying on a revert timer. The 3-hour revert applies only to Mode B setpoints through Product 2 (Sungrow's 20 s heartbeat is the hard version of this).


## 4. Fallback and safety rules in plain words

This section restates, in plain words, what the plan must always carry and what each side does when something goes wrong. It adopts the safety contract in `U3-product2-interface.md` section 3 (the rules table in 3.3, the fallback object in 3.4, the three control tiers in 3.5) and the `ems-edge/1.0` plan, event and ack messages in U3 section 5. Nothing below contradicts U3; where this note sharpens U3 the item is marked **R6 refinement** and the list of refinements is collected in 4.7 so that U3 can be updated in one pass.

Vocabulary: "cloud" is Product 1 (the optimiser and its executor service); "edge" is Product 2 when it exists, or the interim thin writer of U3 4.4 item 3; "installer" is the human applying a Mode A table in the OEM app (control tier 2); "native" is the inverter's own standalone behaviour with the settings captured at onboarding.

### 4.1 What every plan must carry (plan invariants)

The cloud refuses to sign, and the edge refuses to apply, a plan that lacks any of the following. They map one-to-one onto the `fallback` object of U3 3.4 and 5.4, plus three fields of the plan body.

| # | Invariant | Rule in words | Where it lives |
| --- | --- | --- | --- |
| I1 | **Safe tail slot** | The plan's last slot of each day, and the state at `valid_until`, must be one the inverter can sit in indefinitely: `battery_mode` self_use or hold, `grid_charge_allowed` only if the floor cannot otherwise be met, `export_limit_w` at the export floor, and `soc_floor_pct` at least the outage reserve for the next known or likely outage window. Formally: for every slot boundary from the last planned re-solve to `valid_until`, the planned SoC trajectory must stay at or above `reserve.min_soc_pct` (TouSlotPlan v1, section 3.6) and the final slot's floor must equal `max(reserve.min_soc_pct, fallback.min_soc_floor_pct)`. Mode A tables persist on the inverter after expiry, so this invariant *is* the expiry behaviour on slot-only brands. **R6 refinement**: U3 3.3 says "prefer plans whose tail is safe"; here it is a hard check with the formula above. | `mode_a.days[-1].slots[-1]`, `reserve` |
| I2 | **Absolute SoC floor** | `fallback.min_soc_floor_pct`: enforced by the edge regardless of any slot's content. Set by the cloud to `max(BMS low cap + 2, backup reserve the owner configured, 10)` and never changed by more than 10 points between consecutive plans without an owner-visible reason code (`reserve_changed`). | `fallback.min_soc_floor_pct` |
| I3 | **Export floor** | `fallback.max_export_w_floor`: the export cap that applies in every slot and in fallback. Default 0 W for sites without a net-metering licence; equal to the licensed export limit otherwise. No slot may carry `export_limit_w` above it. | `fallback.max_export_w_floor` |
| I4 | **Native-settings reference** | `fallback.native_settings_ref`: the id of the onboarding capture of the inverter's own settings. **R6 refinement**: the capabilities message (U3 5.3) should carry `native_capture_id`; the cloud must copy that id into the plan and the edge rejects a plan whose ref does not match its current capture (reason code `NATIVE_REF_STALE`, to be added to the U3 5.6 list). This stops a plan reverting to settings an installer has since changed on purpose. | `fallback.native_settings_ref` |
| I5 | **Expiry** | `valid_until` at most 48 h after `issued_at` (U3 3.2). For Mode A the plan carries two day-tables; day 2 is the "fallback day" and must itself satisfy I1. On expiry with no newer plan the edge executes `fallback.on_expiry` (default `inverter_native`). | `valid_from`, `valid_until`, `fallback.on_expiry` |
| I6 | **Link-loss mark** | `fallback.on_link_loss_after_s` (default 300 s, five missed 60-second heartbeats). After it the edge flags the site unattended and keeps executing the cached plan to `valid_until`, then reverts. | `fallback.on_link_loss_after_s` |
| I7 | **Identity and lineage** | `plan_id`, `supersedes`, `capabilities_seq`, `timezone`, Ed25519 signature with per-site `key_id`. | plan envelope |
| I8 | **Write policy** | `apply_policy.max_table_writes_per_day` (edge cap 4) and the cloud's own TouSlotPlan `write_budget` (3 table + 2 event, section 3.6), which must be at or under the edge cap. | `mode_a.apply_policy` |

### 4.2 Cloud-side rules (Product 1)

C1. **Validate before signing.** Every primitive is checked against the site's latest capabilities snapshot (ranges, steps, slot counts, slot grammar per brand, persistence class). A plan the edge rejects for `SCHEMA`, `OFF_WHITELIST`, `RANGE_CLAMPED` or `SLOT_GRAMMAR` is logged as a cloud bug, not retried.

C2. **Replay before issue (the wrong-plan guard).** Before signing, the executor replays the compiled table through the digital twin (U3 4.3) from the current measured SoC, the load and PV forecast and the outage hazard. It refuses to issue when the replayed SoC crosses the absolute floor, when export exceeds the export floor, when the month-to-date MDI cap (`mdi_cap_kw`) is exceeded by the plan's own grid draw, or when a genset slot coincides with an expected-grid-present period. **R6 refinement** (U3 has no pre-issue replay).

C3. **Change sanity.** If the new plan moves more than three slot boundaries by more than two hours relative to yesterday's plan while the forecast inputs changed by less than 10 %, the plan is held for owner review (tier 2 and 3) and the previous plan stays in force. A negative expected saving against the "native" counterfactual also holds the plan.

C4. **Cadence and staggering.** One plan per site per 15 minutes at most; the Mode A nightly table is issued after the 23:00 re-solve inside the apply window; fleet pushes are spread across the 15-minute window; at most one `release` and one `hold` out of band per hour (U3 3.2).

C5. **Act on the ack, not the request.** `partial` or `rejected` acks re-enter the optimiser as the achieved values; the cloud never assumes the requested value was applied.

C6. **Read-back mismatch.** One `readback_mismatch` event: treat the device state as unknown, re-plan from telemetry, alert at "warning". Two mismatches on the same site in 24 h: demote the site to `advisory_writes` automatically and page the operator; re-enable only after a fresh capabilities message and a successful test write. **R6 refinement** (U3 3.3 defines the edge behaviour; this is the cloud's escalation).

C7. **Local override.** On `override_local` the cloud pauses control of that primitive (U3), shows before and after values in the UI, and offers two buttons: "adopt as native" (re-capture native settings, bump `native_capture_id`, re-plan) or "resume control" (re-issue the plan). Nothing is written until one is chosen.

C8. **Template or firmware change.** Site to `advisory_writes` automatically (U3); the executor marks every pending plan for that site as `superseded_by_caps_change` and re-runs the per-brand compiler against the new capabilities before any further write.

C9. **Edge reboot.** On `edge_reboot` the cloud waits for a fresh capabilities message, then re-issues the current plan with a new `plan_id` (the edge does not resume a plan older than one control period, U3 3.3).

C10. **Clock drift.** Mode A tables are local-time tables, so an inverter clock error shifts every boundary. The cloud computes the offset from (a) the heartbeat's `clock_offset_s` for the edge and (b) the inverter's own RTC where the template exposes it (Deye regs 22-24 hold date and time on the LV profile; other brands UNVERIFIED), reported by the edge as a telemetry point `inverter_clock_offset_s` (**R6 refinement**: add to U3 1.2). Rules: offset above 120 s raises `clock_unsynced`; above 300 s the compiler widens the asymmetric boundaries, starting every peak-avoidance or discharge slot 15 minutes early and ending it 15 minutes late (being early costs a little stored energy, being late costs the peak tariff), and the UI asks the installer to set the inverter clock; above 900 s the site is demoted to advisory until the clock is fixed.

C11. **Cloud silence.** If the executor has not issued a plan for a site for 24 h (solver outage, queue failure) the site is already in fallback by construction (I1, I5); a cloud-side watchdog pages the operator after 2 missed nightly tables.

C12. **Genset.** The plan carries the genset decision as an advisory enum (U3 3.7); the local genset condition stack of section 2.2 is never disabled or weakened by a plan. In particular a plan cannot raise the SoC-start threshold above the local setting or suppress stop-on-AC.

C13. **Audit and fidelity.** Every ack, event and installer confirmation is stored per site and feeds the fidelity score of 4.6 and the savings dispute trail.

### 4.3 Edge-side rules (Product 2, or the interim thin writer)

E1 to E15 are the U3 3.3 table in plain words: whitelist, range clamp with `clamped` in the ack, per-register daily budget with `BUDGET_EXHAUSTED`, write on change, atomic groups and unlock steps handled by the template, read back every write with one retry then release, heartbeat and revert timer at most two control periods for volatile setpoints, expiry to `fallback.on_expiry`, link loss as in I6, reboot without resuming stale plans, canary reads for override and template change (fail closed to read-only), signature and replay checks, one plan per minute, and a who-why-before-after log.

The following are **R6 refinements** that do not change any of E1 to E15:

E16. **Safe write order inside a table write.** Where the brand allows one block write (Deye regs 250-279 or 148-177 in one FC16; Huawei 47255-47297 in one block), the table is atomic. Where it does not (Growatt window triples and separate SoC registers; Sungrow forced-charge windows and Max/Min SoC as separate registers), the edge writes in the order: raise any floor that goes up, then times and flags, then lower any floor that goes down. At every intermediate state the inverter is at least as safe as before the write.

E17. **No-write windows.** The edge does not write a table while the inverter reports a fault or BMS alarm, within 60 s of a grid-state change (outage start or end), while the genset is in warm-up or cool-down (section 2.2), or outside `apply_policy.apply_window_local` (00:05 to 23:50, so the Deye slot ring is never rewritten across midnight). A write deferred for these reasons is retried every minute for up to 30 minutes, then acked `DEVICE_BUSY` (new reason code for U3 5.6).

E18. **Fault release.** On an inverter fault, BMS alarm, or over-temperature the edge releases to native, reports `control_disabled` with the cause, and does not resume until the fault clears and a fresh plan arrives.

E19. **Clock.** The edge reads the inverter RTC daily where exposed and reports the offset (C10); it never writes the RTC (installer action), and it uses its own NTP-disciplined clock for slot timing in Mode B.

E20. **Local safety stack runs regardless of plan.** The load-shedding state machine of section 2.1 (fed with the plan's outage expectations) and the genset condition stack of section 2.2 run on the edge independently of plan state; `release` and `hold` commands do not stop them.

### 4.4 What happens when (event matrix)

| Event | Edge does | Cloud does | Clears when |
| --- | --- | --- | --- |
| Plan expiry, no newer plan | Executes `fallback.on_expiry`; on slot-only brands the persisted day-2 table (I1) is already the safe state; raises `plan_expired`, then `fallback_engaged` | Alerts "site in fallback"; C11 watchdog | New valid plan acked (`fallback_cleared`) |
| Link loss | After 300 s marks unattended, keeps executing the cached plan to `valid_until`, then falls back; sends `link_lost` with the gap after reconnect | Marks site stale in UI after 5 minutes; no re-plan until telemetry resumes | Heartbeat resumes; cloud re-issues the current plan |
| Read-back mismatch | Retries once, then releases that primitive; `readback_mismatch` | C6: unknown state, re-plan from telemetry; second in 24 h demotes to advisory | Successful test write after fresh capabilities |
| Local override | Stops writing that primitive; `override_local` with before and after | C7: pause, show, two-button choice | Owner chooses adopt-as-native or resume |
| Template or firmware change | Canary mismatch: control disabled, read-only; `template_change_detected` | C8: advisory tier, recompile against new capabilities | Conformance check passed on the bench for the new template |
| Controller reboot | Does not resume a plan older than one control period; sends `edge_reboot` and capabilities | C9: re-issue with new plan_id | Ack received |
| Clock drift | Reports offset; above 120 s `clock_unsynced` | C10: widen boundaries above 300 s, demote above 900 s | Offset back under 120 s |
| Wrong plan (fails replay or sanity) | Never sees it | C2, C3: hold for review, keep previous plan | Owner approves or the next solve passes |
| Budget exhausted | Keeps last applied table; `budget_exhausted` | Re-plans assuming the applied table stays; no retries that day | Local midnight reset |
| Device busy or fault | E17, E18 | Alert; no re-plan until fault clears | Fault clear plus fresh plan |

### 4.5 Write-budget rules

- Counters live on the edge, per register and per day, reset at local midnight; the cloud keeps a shadow count from `writes_used_today` in each ack and will not issue a write it knows is over budget.
- Caps: EEPROM-class registers 100 writes a day per register (U3, engineering default); Mode A table writes 4 a day on the edge, of which the cloud uses at most 3 (nightly plus two event rewrites, TouSlotPlan `write_budget`); an event rewrite is issued only if the expected gain exceeds Rs 500 *and* the budget is not exhausted *and* the previous write is more than 60 minutes old.
- Which registers an "event rewrite" may touch: volatile or dispatch-class only where they exist (Sungrow 13051/13052 in compulsory mode; Huawei forcible-charge 47100/47246/47247, persistence class UNVERIFIED and to be measured on the bench by power-cycling after a write); on Deye and Growatt every TOU register is EEPROM-class, so an event rewrite *is* a table write and costs one of the 3.
- Why the caps are where they are: a forum figure of 10^5 to 10^6 erase cycles for the flash behind these registers (UNVERIFIED for any specific inverter) gives 10^5 / (5 a day) = 55 years at our cadence but 10^5 / (100 a day) = 2.7 years at the hard cap; the hard cap is a backstop against a runaway loop, not an operating point. Sungrow's protocol additionally forbids frequent periodic writes through WiNet-S (section 3.3).
- A fifth table write in one day is refused with `BUDGET_EXHAUSTED` and the previous table remains (U3 5.8 test 5).

### 4.6 Confirming an installer-applied table (control tier 2) from telemetry, and scoring fidelity

In tier 2 there is no register read-back; the cloud shows the compiled table in the vendor's own layout (Deye six programs, Huawei fourteen periods, Growatt windows, Sungrow two forced-charge windows plus Max/Min SoC), the installer keys it in and presses "applied" in the app, which records `installer_session_start/end` with the plan_id. Confirmation then comes from 1-minute telemetry only.

Signals used (all from the U3 1.2 telemetry points): `inverter_mode_active` where the OEM cloud or Modbus exposes it (Deye "Use Timer" bit and the running program number, Huawei working mode 47086 read-back through FusionSolar: UNVERIFIED whether the FusionSolar API returns it), `battery_power_w`, `battery_soc_bms_pct`, `grid_power_w`, `load_power_w`, `pv_power_w`.

Per-slot expectations derived from the plan (what should be observable):

1. **Boundary transitions.** At a boundary where the mode changes from hold to discharge (peak start), battery discharge power must rise above 10 % of rated within 30 minutes; at discharge to hold (peak end or floor reached) it must fall below 5 % of rated within 30 minutes; at the start of a grid-charge slot grid import must exceed load by at least 25 % of the planned charge power while SoC rises. Boundaries with no observable change (hold to hold, self_use to self_use) are not scored.
2. **Floor respected.** Minimum SoC inside a slot is at least `soc_floor_pct - 3` points.
3. **No forbidden behaviour.** No grid charging (grid import above load while SoC rises) in a slot with `grid_charge_allowed: false`; no export above `export_limit_w`; no discharge in a `hold` slot.
4. **Target reached.** In charge slots, SoC reaches `soc_target_pct - 5` points before the slot ends when PV or grid energy was available to do so (checked against the twin's replay).

Classification per day: **confirmed** when at least 80 % of scorable boundaries hit, no forbidden behaviour, and no floor breach; **not applied** when the day's behaviour matches the *previous* table or native mode better than the new one (same tests run against both); **partial** otherwise. "Not applied" for two consecutive days re-opens the installer task and stops the savings counter for the site.

Plan-execution fidelity score (per site per day, in [0, 1]):

```
F = 0.4 * boundary_hit_rate
  + 0.3 * (1 - floor_breach_fraction)            # fraction of slots with a breach below floor - 3
  + 0.3 * energy_fidelity
energy_fidelity = 1 - clip( sum_slots |E_batt_planned - E_batt_measured| / max(sum_slots |E_batt_planned|, 0.1 * usable_kWh), 0, 1)
```

E_batt is the signed battery energy per slot (discharge positive). The same formula is computed in tier 3, where additionally `readback_ok` must be true for every primitive; a read-back-OK table that the inverter then ignores (for example Deye stops following the program when SoC is at or below `BATTERY_LOW_CAP`, Huawei does not discharge outside any set period, Growatt reverts to Load First when no window is active) still scores low, which is the point of measuring from telemetry rather than from the ack. Fidelity below 0.6 for three days raises a site alert; the savings reported to the owner are the counterfactual saving multiplied by the energy-fidelity term so that an unapplied table never claims a saving.

### 4.7 Where this section refines U3 (for the next U3 revision)

1. I1: the safe tail is a checked invariant with a formula, not a preference.
2. I4: `native_capture_id` in capabilities, `NATIVE_REF_STALE` reason code.
3. C2: pre-issue replay through the twin; C3: change-sanity hold.
4. C6: two read-back mismatches in 24 h demote to advisory.
5. C7: two-button override resolution (adopt as native, resume).
6. C10 and E19: clock-drift thresholds (120 s warn, 300 s widen, 900 s demote) and the `inverter_clock_offset_s` telemetry point.
7. E16: write ordering inside non-atomic table writes; E17: no-write windows and `DEVICE_BUSY`; E18: fault release.
8. 4.5: an event rewrite on Deye and Growatt counts as a table write; the Rs 500 gain and 60-minute spacing rules.
9. 4.6: the tier-2 confirmation tests, the three-way classification and the fidelity formula.

## 5. Battery-state service design (cloud, 1-minute data)

### 5.0 Ownership (decision already taken, U3 3.6)

- The **edge** (Product 2) owns the real-time 1 Hz estimate where a shunt exists: 2-RC EKF, anchor resets, its own trust score, published as `battery_soc_est_pct`, `battery_soc_sigma_pct`, `battery_trust_score`, `battery_soc_source`.
- The **cloud** (Product 1, this service) owns: the daily re-estimate at 1-minute resolution for every site (including OEM-cloud-fed sites with no shunt and no edge), the 30-day trust score, SoH, the usable-kWh figure the optimiser consumes, the alerts, and the wear-cost curve (section 6).
- The optimiser takes the estimate with the lowest sigma (edge if present and fresher than 5 minutes, else cloud) and widens its reserve by 2 sigma (5.6). When edge and cloud estimates differ by more than 10 points the service raises a site alert rather than picking one.

### 5.1 Inputs (per site, per battery device, 1-minute rows from the U3 1.2 telemetry contract)

| Input | Unit | Source | Required | Notes |
| --- | --- | --- | --- | --- |
| `battery_voltage_v` | V | inverter or BMS register, OEM cloud | yes | pack terminal voltage; resolution matters (0.1 V on a 384 V pack is 0.8 mV per cell) |
| `battery_current_a` or `battery_power_w` | A or W | inverter (power) or shunt (current) | yes (one of) | sign convention: discharge positive; when only power is available, I = P / V and the service tags the row `current_source: derived` |
| `battery_temp_c` | C | BMS max cell or pack sensor | yes | if missing, substitute inverter ambient + 5 C and flag `temp_proxy` |
| `battery_soc_bms_pct` | % | BMS via inverter | yes | the prior; often 1 % resolution and jumpy |
| `battery_soh_bms_pct` | % | BMS | no | prior for SoH |
| `battery_charge_limit_a`, `battery_discharge_limit_a` | A | BMS (Deye 312-316 region, Huawei 37046-37048, Sungrow 13033-13034: UNVERIFIED addresses, read from templates) | no | pass-through to the optimiser's power bounds; a limit falling with temperature is also a derating signal |
| `cell_v_max`, `cell_v_min`, `cell_t_max` | V, C | BMS where published | no | spread = `cell_v_max - cell_v_min`; spread above 50 mV at rest near full is an imbalance flag |
| `battery_soc_est_pct`, `battery_soc_sigma_pct`, `battery_soc_source`, `battery_trust_score` | %, %, enum, [0,1] | edge, when it exists | no | the edge estimate is an input, never overwritten; the cloud compares |
| `nominal_kwh`, `nominal_ah`, `cells_series`, `chemistry`, `commission_date`, `warranty_cycles`, `warranty_dod`, `warranty_throughput_kwh` | config | onboarding | yes | LFP only in v1 |
| `soc_floor_pct` (reserve), `soc_ceiling_pct` | % | the plan in force | yes | for usable kWh |

Data hygiene before estimation: rows older than 2 minutes relative to their neighbours are gaps (coulomb counting integrates across a gap with the last known current only if the gap is under 5 minutes; longer gaps inflate sigma, 5.3); SoC jumps of more than 3 points in one minute at under C/4 current are BMS resets and are logged as `bms_jump` events, not integrated.

### 5.2 The LFP plateau problem (why voltage is not a SoC signal between the knees)

Between roughly 20 and 90 % SoC an LFP cell's open-circuit voltage moves only from about 3.29 V to 3.33 V (charge branch; figures generic, calibrate per pack: UNVERIFIED for any specific cell), about 0.5 mV per SoC point, flatter still in the middle where the brief's working figure of **1 mV of voltage error for about 5 points of SoC** applies. Two consequences:

1. A pack-level voltage reading with 0.1 V resolution on a 120-cell (384 V) pack is 0.8 mV per cell: at best 2 to 5 points of SoC, and that is before current-dependent IR drop (R0 of a few milliohm per cell times tens of amps is tens of mV).
2. LFP OCV hysteresis between the charge and discharge branches is 5 to 25 mV even after long rests (vendor figure, search result 6 Nov 2026, UNVERIFIED), which alone spans 10 to 40 points.

So in the plateau the service relies on current integration and uses voltage only at the **knees**: the upper knee above about 3.40 V per cell under low current (SoC 95 to 100) and the lower knee below about 3.20 V per cell at rest (SoC 10 to 15), where dV/dSoC is tens of mV per point. The EKF in 5.4 encodes this automatically through the OCV slope in its Jacobian: the Kalman gain is tiny on the plateau and large at the knees.

### 5.3 Method v1 (all sites): coulomb counting between anchors with the BMS as prior

State per device: `soc_cc` (coulomb-counted SoC since the last anchor), `t_anchor` (time of the last anchor), `ah_since_anchor` (unsigned throughput since the anchor), `soc_est`, `sigma`.

**Integration** (per 1-minute row, discharge positive):

```
soc_cc[k] = soc_cc[k-1] - 100 * eta(I) * I[k] * dt_h / Q_ah
eta(I) = 1.0 on discharge, 0.98 on charge (coulombic efficiency; LFP is 99+ % at cell level, 97-99 % at pack level after BMS and balancing losses: tune per site from anchor-to-anchor closure)
self-discharge: 2 % per month applied as 0.0028 points per hour of rest (UNVERIFIED generic figure; negligible over a day, kept so multi-day gaps close)
Q_ah = nominal_ah * SoH / 100
```

**Anchors** (reset `soc_cc` to the anchor value, reset sigma to `sigma_anchor`):

| Anchor | Condition on 1-minute data | SoC assigned | sigma_anchor |
| --- | --- | --- | --- |
| A1 full-charge plateau exit (preferred) | pack voltage per cell above 3.40 V (or inverter reports absorption/float, or BMS SoC = 100 with charge current cut to below C/20 for 5 minutes) | 100 (or 97 if only the plateau exit is seen, not the current cut-off) | 1.5 points (1.0 when the cut-off is seen) |
| A2 low-voltage knee | rest (abs(I) below C/50 for 10 minutes) and per-cell voltage below 3.20 V, or inverter low-SoC shutdown | from the OCV table at that rest voltage (typically 8 to 15) | 3 points |
| A3 BMS-declared full | BMS SoC = 100 and cell spread under 30 mV | 100 | 2 points |
| A4 edge anchor | the edge reports `anchor_reset` with its value | that value | the edge's sigma |

Anchors are the only place the voltage is believed; A1 is reached on most sunny days at PV-rich sites, which is why the cloud estimate stays usable without a shunt. Every anchor is written to `battery_anchor_log` with the BMS SoC at that moment (the trust score needs it).

**Fusion with the BMS prior**: between anchors the estimate is a precision-weighted blend,

```
w_bms = trust * (sigma_cc^2) / (sigma_cc^2 + sigma_bms^2)      # sigma_bms = 4 points by default, 8 when trust < 0.5
soc_est = (1 - w_bms) * soc_cc + w_bms * soc_bms
sigma = sqrt( (1 - w_bms)^2 * sigma_cc^2 + w_bms^2 * sigma_bms^2 )
```

so a trusted BMS pulls the estimate towards itself as the coulomb count ages, an untrusted one barely does.

**Sigma growth** (the honesty term; it is what the optimiser consumes):

```
sigma_cc(t) = sqrt( sigma_anchor^2 + (k_b * hours_since_anchor)^2 + (k_g * ah_since_anchor / Q_ah * 100)^2 + sigma_gap^2 )
k_b : current-offset term, points per hour = 100 * I_offset_A / Q_ah; default I_offset = 0.5 A for inverter-derived current, 0.05 A for a shunt
k_g : gain-error term, default 0.02 (2 %) for inverter-derived current, 0.005 for a shunt
sigma_gap : 1 point per 10 minutes of data gap integrated with held current, reset at the next anchor
```

Worked numbers for a 100 kWh, 384 V, 260 Ah pack with inverter-derived current: k_b = 100 * 0.5 / 260 = 0.19 points per hour, so 24 h without an anchor adds 4.6 points; one full cycle of throughput adds 2 points; from a 1.5-point anchor at 10:00 the sigma at the next morning's 06:00 is about sqrt(1.5^2 + 3.8^2 + 2^2) = 4.6 points, and after 3 cloudy days without a full charge about 15 points, which is the level at which the optimiser asks for an anchor (5.6). With a shunt the same 3 days give about 2.5 points.

### 5.4 Method v2 (sites with a shunt or a clean 1-minute current): equivalent-circuit EKF

Model (1-RC for 1-minute data; 2-RC only where 1 Hz edge data is replayed, since a 1-minute sample cannot resolve a sub-minute time constant):

```
states x = [soc, v1, (v2), h]           h = hysteresis state in [-1, 1] (Plett one-state model), needed for LFP
soc[k+1] = soc[k] - eta * I[k] * dt / Q
v1[k+1]  = exp(-dt/tau1) * v1[k] + R1 * (1 - exp(-dt/tau1)) * I[k]
h[k+1]   = exp(-abs(eta * I[k] * gamma * dt / Q)) * h[k] - (1 - exp(-abs(eta * I[k] * gamma * dt / Q))) * sign(I[k])
V[k]     = OCV(soc[k], T) + M * h[k] - v1[k] - R0 * I[k]         # per cell, times cells_series for the pack
```

EKF: predict with the state equations, update with the pack voltage, Jacobian H = [dOCV/dsoc, -1, M, ...]. On the plateau dOCV/dsoc is about 0.5 mV per point so the gain is small and the filter is a coulomb counter with a slow voltage correction; at the knees it snaps to the voltage. Process noise on soc from the k_b and k_g terms above; measurement noise from the voltage resolution (0.1 V pack = 0.8 mV per cell on a 120-cell pack, so R = (0.8 mV)^2 plus the model error, suggest 5 mV total). Joint capacity estimation (Q as a slow state) is the SoH path in v2.

Parameters: R0, R1, tau1, M, gamma from a commissioning test (a full charge at C/5 with 10-minute rests every 10 % and a full discharge the same way) fitted by least squares; the OCV(soc) table from the same test's rest points (the pseudo-OCV average of the two branches, with M carrying the half-gap). Until a site has its test, use a generic LFP table scaled by `cells_series` and double the measurement noise.

Open-source implementations to learn from or use (licences in section 7):

- **PyBatterySE** (tue-battery/PyBatterySE, BSD-3-Clause, `pip install pybatteryse`): EKF and particle filter SoC estimation on linear-parameter-varying battery models, SoC observability analysis via finite-horizon Gramians, ageing-aware joint estimation of capacity and parameters, with NMC and LFP example datasets. Models are identified with its companion PyBatteryID (licence not checked: UNVERIFIED). Usable as a library in the cloud service's bench and, if its API proves stable, in the daily job.
- **filterpy** (rlabbe/filterpy, MIT): a generic `ExtendedKalmanFilter` class; the 1-RC model and Jacobians are ours. Smallest dependency; the recommended production path for v2.
- **PyBaMM** (pybamm-team/PyBaMM, BSD-3-Clause): the `Prada2013` parameter set is PyBaMM's only LFP set (A123 LFP/graphite, Prada et al., J. Electrochem. Soc. 160(4) A616, 2013); the docs note the maintainers have not validated it and a discussion reports inconsistencies. Use it to generate a generic LFP OCV table and synthetic truth traces for the bench, never as the production OCV.
- **Plett's ESC toolbox** (textbook companion code, MATLAB and Octave): the equations above follow its enhanced self-correcting model, re-derived from the published equations; the code's licence could not be found (UNVERIFIED) so no file from it is copied.

### 5.5 Outputs (per device, per minute and per day)

| Output | Definition |
| --- | --- |
| `soc_est_pct`, `soc_sigma_pct` | 5.3 or 5.4; per minute |
| `soc_source` | `edge`, `cloud_cc`, `cloud_ekf` |
| `usable_kwh` | `nominal_kwh * soh / 100 * max(0, soc_est - 2 * sigma - soc_floor) / 100 * eta_dis` (eta_dis 0.95 pack-to-AC); and `headroom_kwh` = `nominal_kwh * soh / 100 * max(0, soc_ceiling - soc_est - 2 * sigma) / 100` |
| `soh_pct` | v1: capacity from anchor pairs, `Q_meas = ah_between_anchors / (soc_anchor_hi - soc_anchor_lo) * 100` for any two anchors within 7 days with no gap longer than 30 minutes between them; monthly median of the pairs, EWMA with the BMS SoH as prior (weight 0.3), floored at 50 % and never rising more than 1 point a month. v2: the EKF's joint capacity state |
| `trust_score` | 5.6, daily, over a rolling 30 days |
| `hours_hot_high_soc` | daily hours with `soc_est > 90` and `battery_temp_c > 35` (feeds the calendar term in section 6 and an alert) |
| `efc_day`, `cycles_day` | equivalent full cycles (sum of abs(dSoC) / 200) and rainflow cycle list (depth, mean SoC, count) per day, the inputs to section 6 |
| `anchor_log` | every anchor with type, value, BMS SoC at that moment |

### 5.6 Trust score (defined precisely)

Rolling 30-day window, per device, computed daily:

```
a_anchor  = clip(1 - mean_over_anchors( abs(soc_bms_at_anchor - soc_anchor) ) / 10, 0, 1)     # BMS agrees with the voltage anchors
a_cc      = clip(1 - rms_over_window( soc_bms - soc_cc ) / 10, 0, 1)                           # BMS agrees with the coulomb count between anchors
a_consist = fraction of minutes where abs(d(soc_bms)/dt + 100 * I * eta / Q_ah) < 2 points per hour and no jump > 3 points in a minute   # BMS is self-consistent with the current it reports
a_cover   = clip(number_of_anchors_in_window / 8, 0, 1)                                         # at least two anchors a week
trust     = 0.4 * a_anchor + 0.3 * a_cc + 0.2 * a_consist + 0.1 * a_cover
```

A device with fewer than two anchors in the window gets `a_anchor = 0.5` (unknown, not bad) and `a_cover` accordingly. Trust enters the BMS weight in 5.3 and the alert below. The U3 acceptance mark (edge SoC within 5 points at anchors over 30 days) is the same statistic as `a_anchor` applied to the edge estimate; the cloud computes it for the edge too and publishes both.

### 5.7 Alerts

| Alert | Condition | Action |
| --- | --- | --- |
| `battery_trust_low` | trust under 0.5 for 3 consecutive days | BMS weight drops to 0 in 5.3; UI card "BMS SoC unreliable"; installer visit suggested; the optimiser runs on the cloud estimate with its full sigma |
| `soc_disagreement` | abs(soc_bms - soc_est) over 10 points for more than 60 minutes, or abs(edge - cloud) over 10 points | site alert; no automatic choice (U3 3.6); the optimiser uses the higher-sigma union: floor widened to cover both |
| `hot_high_soc` | `hours_hot_high_soc` over 4 h/day on 7 of the last 14 days | wear-cost card; the optimiser's calendar term (section 6) is already pricing it, so the alert is for the owner (shade, ventilation, lower ceiling in summer) |
| `sigma_high` | sigma over 15 points | the plan requests an anchor (a PV charge-to-100 slot within 48 h; cheap on sunny days) and holds a conservative floor until then |
| `imbalance` | cell spread over 50 mV at rest near full on 3 days in a week | installer notice; SoH figure flagged |
| `capacity_drop` | SoH falls more than 3 points in a month | review; warranty log |
| `derate` | BMS discharge limit under 50 % of nominal for over 30 minutes | the optimiser's power bound follows the limit; alert if during a peak window |

### 5.8 How the optimiser uses sigma

- `soc_floor_eff = soc_floor + 2 * sigma` for every reserve and outage constraint; `usable_kwh` above already subtracts 2 sigma.
- Charge targets are not widened (overshooting a target costs nothing but a little calendar wear), discharge floors are.
- When sigma exceeds 15 points the plan enters conservative mode: no slot may plan to discharge below `soc_floor + 20`, and an anchor slot is inserted (5.7).
- Sigma from the cloud is capped at 25 points in the optimiser so that a long-dead BMS does not freeze the site; at the cap the site is also flagged for a shunt (Product 2).

### 5.9 Data model and schedule

Tables: `battery_state_1min(site_id, device_id, ts, v, i, t, soc_bms, soc_cc, soc_est, sigma, source, anchor_type)`, `battery_daily(site_id, device_id, date, soh, trust, a_anchor, a_cc, a_consist, a_cover, efc, hours_hot_high_soc, cycles_json)`, `battery_anchor_log(site_id, device_id, ts, type, soc_value, soc_bms, v_cell, i)`. The daily job runs after local midnight for the previous day (re-estimates the whole day with both ends' anchors, so it is a smoother, not a filter), and a light incremental pass runs every 15 minutes on the live data so the optimiser's `soc_est` and `sigma` are never older than one control period. Effort: the v1 service is the 3-person-week WP3 item in U3; v2 adds about 2 weeks plus the commissioning test procedure.

## 6. Wear-cost derivation with Pakistani numbers

The optimiser needs a rupee cost for using the battery so that it does not cycle for a saving smaller than the wear it causes. Three layers, each usable alone: (6.1) a flat throughput price, (6.2) a depth-of-discharge-aware piecewise-linear price after Xu et al., (6.4) a calendar term for holding high SoC in a hot enclosure. All figures use the SolarCitizen price survey of 16 October 2026 (installed LFP Rs 45,000 to 65,000 per kWh, typical Rs 52,000) and a warranty of 6,000 cycles at 80 % depth of discharge to 80 % remaining capacity (or the warranty's own throughput figure where the datasheet gives one, which is preferable because it already folds in the maker's depth assumption). Numbers were computed with `r6_wear.py` and `r6_wear2.py` (scratchpad, 6 November 2026) from the BLAST-Lite coefficients quoted in 6.3.

### 6.1 Flat throughput price c_cyc

```
c_cyc [Rs per kWh discharged] = replacement cost per usable kWh / warranted throughput per usable kWh
                               = price_per_kWh / (N_warranty * DoD_warranty)
```

| Price (Rs/kWh) | 6,000 cycles counted as full-capacity throughput (the brief's convention) | 6,000 cycles at 80 % DoD (throughput 4,800 kWh per kWh) | 8,000 cycles at 80 % |
| --- | --- | --- | --- |
| 45,000 | 7.5 | 9.4 | 7.0 |
| 52,000 | 8.7 | 10.8 | 8.1 |
| 65,000 | 10.8 | 13.5 | 10.2 |

So **Rs 7.5 to 10.8 per kWh** under the brief's convention and **Rs 9 to 13.5** if the warranty depth is honoured; the build should use the warranty's explicit throughput figure when printed on the datasheet and otherwise Rs 10.8 (52,000 / 4,800) as the default, with the two ends as a sensitivity. Note on "80 % end of life": the price is the whole pack because a pack at 80 % is replaced, not 20 % of it; dividing both the cost and the capacity window by 0.2 cancels, which is why the formula needs no EoL factor. For comparison, BloombergNEF's December 2025 survey puts a stationary LFP pack at US$70 to 81 per kWh at the factory gate (about Rs 20,000 to 23,000 at Rs 280 per US$: UNVERIFIED rate), so about half the Pakistani installed price is inverter-side, enclosure, import and installer margin; the installed price is the right one for the wear cost because the owner replaces the installed article.

### 6.2 Depth-aware piecewise-linear cost (Xu et al.)

Source: B. Xu, J. Zhao, T. Zheng, E. Litvinov, D. S. Kirschen, "Factoring the Cycle Aging Cost of Batteries Participating in Electricity Markets", IEEE Transactions on Power Systems vol. 33 no. 2, 2018 (arXiv 1707.04567v2, read 6 Nov 2026; the task brief cites it as "Xu et al. 2018" with a different title, this is the paper). Its construction:

- Cycle life loss from a SoC profile is the sum over rainflow-identified cycles of a cycle-depth stress function: `L = sum_i Phi(delta_i)` (eq. 1), with `Phi(delta)` the fraction of life consumed by one cycle of depth delta, i.e. `Phi(delta) = 1 / N(delta)` where N is cycles to end of life at that depth.
- Only discharging half-cycles are charged, each as a full cycle of the same depth; charging half-cycles are free (the paper's simplification, "amounts charged and discharged are almost identical on a daily basis").
- The marginal cost is made piecewise linear by dividing the depth range into J equal segments (eq. 4-5):

```
c_j = R / (eta_dis * E_rate) * J * [ Phi(j/J) - Phi((j-1)/J) ]        j = 1..J     [Rs per kWh]
```

  where R is the replacement cost (Rs), E_rate the rated energy (kWh), eta_dis the discharge efficiency. Each segment j is a "bucket" of energy e_{t,j} with capacity E_rate/J; the battery discharges from the shallowest (cheapest) bucket first because Phi is convex, and the cost in the objective is `sum_t sum_j c_j * p_dis_{t,j} * dt` (eq. 7) with bucket balance constraints (eq. 12-16). The paper's own example is an NMC 18650 cell with `Phi(delta) = 5.24e-4 * delta^2.03` (eq. 24, near-quadratic), 3,000 cycles at 80 % DoD, replacement US$300 per kWh.

For LFP the stress function is much flatter. Using the BLAST-Lite LFP cycle model (6.3) at 35 C and C/4, the loss per cycle of depth delta is proportional to `delta * (p4 + p5 * delta + p6 * Crate)`, which is linear in delta with a small quadratic term: cycle-life loss per kWh discharged rises only **18 % from very shallow to 100 % cycles at C/4** (35 % at Crate 0, the ratio (p4 + p5) / p4). Calibrated so that N(0.8) = 6,000 (6.3), for a 100 kWh pack at Rs 52,000 per kWh (R = Rs 5.2 million), eta_dis = 0.95:

| Depth delta | N(delta) cycles | Cost per cycle (Rs) | Rs per kWh discharged |
| --- | --- | --- | --- |
| 0.1 | 53,960 | 96 | 9.6 |
| 0.2 | 26,510 | 196 | 9.8 |
| 0.4 | 12,810 | 406 | 10.2 |
| 0.6 | 8,260 | 629 | 10.5 |
| 0.8 | 6,000 | 867 | 10.8 |
| 1.0 | 4,650 | 1,118 | 11.2 |

and the J = 5 segment prices are c_1..c_5 = **Rs 10.3, 11.0, 11.8, 12.5, 13.2 per kWh** (the 1/eta_dis factor lifts them above the table). Conclusion for the build: for LFP two or three segments are enough (Xu's 16-segment accuracy argument is for the quadratic NMC curve); the flat c_cyc of 6.1 is within 10 % of the depth-aware cost for any realistic day, so v1 of the optimiser may ship with the flat price plus the calendar term, and the segment model is a v2 refinement whose main benefit is discouraging the last 20 % of depth before an outage (where the segment price is 25 % higher than the first).

### 6.3 BLAST-Lite LFP model (what was read, how to calibrate it)

Source: `blast-lite/blast/models/lfp_gr_250AhPrismatic_2019.py` (class `Lfp_Gr_250AhPrismatic`, "Large format prismatic LFP-Gr cell from a large manufacturer, with >250 Ah capacity and an energy-to-power ratio of 6 h**-1"; data from doi 10.1016/j.est.2023.109042), and `degradation_model.py`. BLAST-Lite 1.1.3, BSD-3-Clause.

States: `qLoss_t` (calendar) and `qLoss_EFC` (cycle), capacity `q = 1 - qLoss_t - qLoss_EFC`. Rates per chunk of SoC trace (chunks are rainflow cycles of the SoC series, `_extract_stressors`):

```
kcal = p1 * exp(p2 / T_K) * exp(p3 * Ua(soc) / T_K)                    time-averaged over the chunk
kcyc = (p4 + p5 * dod + p6 * Crate) * (exp(p7 / T_K) + exp(-p8 / T_K))
qLoss_t   follows  k_cal * t_days^pcal        (power law, incremental update  dy = k p (y/k)^((p-1)/p) dx)
qLoss_EFC follows  k_cyc * EFC^pcyc
p1 = 8.37e4, p2 = -5.21e3, p3 = -3.56e3, pcal = 0.526
p4 = 4.38e-8, p5 = 1.55e-8, p6 = 1.68e-7, p7 = 2.19e3, p8 = 1.55e5, pcyc = 0.828
Ua(soc) = anode potential from the Safari-Delacourt graphite OCP with lithiation fraction Xa = 0.0085 + soc * (0.78 - 0.0085)
dod = max(soc) - min(soc) over the chunk; Crate = time-average of abs(d soc/dt) in 1/h; EFC = sum abs(d soc) / 2
```

Caveats stated in the file and README: cycling data were collected only at 0.8 to 1.0 depth of discharge (`experimental_range['dod'] = [0.8, 1]`), 10 to 45 C and charge rates at or below 0.65 C; the cycle model "learned the inverse temperature dependence of the calendar aging model", so kcyc *falls* with temperature (at 45 C it is 37 % below 25 C in these coefficients) and the model "will likely make very poor extrapolations outside" 10 to 45 C; all NREL models are cell-level, "assume that pack lifetime is 20-30 % less than cell lifetime", and are "qualitative". For Pakistan this means: trust the calendar term's temperature and SoC dependence (that is what the test matrix varied), use the cycle term only for its shape in depth, and set its level from the warranty or from measured SoH.

Calibration ("degradation_scalar" in the class constructor, applied to both states): two ways, in order of preference.

1. **From the observed trace** (monthly cloud job): feed the site's 1-minute SoC and temperature history (resampled to 15 minutes, BLAST-Lite is happy with hourly) to `simulate_battery_life`; compare the predicted `q` with the service's measured SoH trend (section 5.5); fit the scalar by least squares once at least 6 months and 2 points of measured fade exist. Also fit a `Crate` offset when the site's rates exceed the model's 0.65 C charge limit.
2. **From the warranty** until then: choose the cycle scalar so that the cycle-only life at the warranted depth equals the warranted cycles; for 6,000 cycles at 80 % with the above coefficients at 35 C and C/4 the scalar is 1.48 (the uncalibrated model gives 9,740 cycles). Keep the calendar scalar at 1 (warranty cycle counts already assume a lab temperature).

### 6.4 Calendar term for high SoC in a hot enclosure

The marginal calendar loss per day at age t is `kcal(T, soc) * [ (t+1)^pcal - t^pcal ]`; with the pack priced at `price * E` over a 20-point fade window, the holding cost per kWh of pack per 15-minute step is:

```
c_cal(T, soc, t) [Rs per kWh-pack per step] = price / 0.20 * kcal(T, soc) * [ (t+1)^0.526 - t^0.526 ] / 96
```

At year 3 (t = 1,095 days; the marginal rate falls with age, so the service recomputes it monthly from the real commissioning date) and Rs 52,000 per kWh:

| Cell or pack temperature | SoC 20 % | SoC 50 % | SoC 80 % | SoC 95 % |
| --- | --- | --- | --- | --- |
| 25 C | 0.013 | 0.026 | 0.036 | 0.039 |
| 35 C | 0.024 | 0.047 | 0.066 | 0.072 |
| 45 C | 0.044 | 0.084 | 0.116 | 0.126 |

(Rs per kWh of pack per 15-minute step; per day multiply by 96: Rs 1.2 to 12.1 per kWh per day, matching section 0.) The dependence on SoC comes through the graphite anode potential Ua, which drops steeply between 0 and 20 % and then slowly, so most of the SoC effect is between 20 and 60 %; the dependence on temperature is Arrhenius, roughly doubling per 10 C.

In the MILP the term is linearised in SoC per temperature band: `c_cal,t = a(T_t) + b(T_t) * soc_t` with `b` fitted between 20 and 95 % (at 45 C, b = (0.126 - 0.044) / 0.75 = 0.109 Rs per kWh-pack per step per unit SoC); the objective adds `sum_t b(T_t) * E * soc_t` (the `a` term is a constant). `T_t` is the forecast enclosure temperature (ambient forecast plus a per-site offset learned from `battery_temp_c`, typically 5 to 10 C in a container without cooling: UNVERIFIED). Only the slope matters to the optimiser; it is what makes "fill to 100 % at noon and sit there until 18:00 in June" cost Rs 400 a day on a 100 kWh pack (6.8), against which the plan can weigh a later PV charge or a lower ceiling when no outage is expected.

### 6.5 Putting it together in the objective

```
wear_cost = sum_t c_cyc_eff * P_dis,t * dt                      # 6.1, or sum_j c_j * p_dis,t,j * dt with Xu buckets (6.2)
          + sum_t b(T_t) * E * soc_t                              # 6.4 calendar slope
c_cyc_eff = c_cyc * f_temp(T_t)                                  # optional: +10 % above 40 C pack temperature (engineering guess, UNVERIFIED by the LFP model, whose cycle term is not trusted in temperature)
```

The wear cost is deliberately *not* multiplied by a discount factor; it is an opportunity cost in today's rupees. It is revisited quarterly against the battery service's measured SoH and the latest price survey.

### 6.6 Bench folder (BLAST-Lite and PySAM run offline, never in the request path)

`bench/wear/` in the backend repository (not imported by the FastAPI app):

- `inputs/<site>/soc_temp_15min.parquet` exported by the battery-state service (section 5.9).
- `blast_run.py`: loads `Lfp_Gr_250AhPrismatic(degradation_scalar=s)`, calls `simulate_battery_life(input_timeseries, threshold_time=..., is_conserve_energy_throughput=...)` on the site's real trace (and on the twin's planned trace for what-if), writes `outputs/<site>/q_trajectory.csv` and the fitted scalar. Runtime is minutes per site-year (numpy loops over rainflow chunks), fine for a monthly job and far too slow and too fragile for a 15-minute solve. The optimiser only ever reads the resulting `c_cyc`, `c_j` and `b(T)` tables.
- `pysam_replay.py`: PySAM `BatteryStateful` (nrel-pysam 7.1.1, BSD-3) with `chem = 1` (Li-ion), `life_model = 0` (calendar/cycle) using `cycling_matrix` built from the warranty curve of 6.2 (rows of DoD %, cycle number, capacity %) and `calendar_choice = 1` with `calendar_q0, calendar_a, calendar_b, calendar_c` fitted to the 6.4 kcal surface, `control_mode = 1` (power), `dt_hr = 1/60`, stepping the pack through a day's measured or planned power; compares its SoC and capacity-loss outputs with the service's, as a second opinion and as the twin's battery when replaying a plan (U3 4.3). `voltage_choice = 1` with a `voltage_matrix` from the commissioning OCV test makes it an LFP pack rather than the default NMC-shaped curve.
- `rainflow_check.py`: counts the day's cycles with `rainflow.extract_cycles` (6.7) and prints Xu's discharging-half-cycle list so a reviewer can see what the depth-aware cost charged for.
- A `README` stating the licences (section 7) and that nothing in the folder is a product dependency.

### 6.7 Rainflow-counting libraries

| Library | Licence | API used | Note |
| --- | --- | --- | --- |
| `rainflow` 3.2.0 (iamlikeme, PyPI) | MIT | `count_cycles(series, ndigits=None, nbins=None, binsize=None)` returns (range, count) pairs with half-cycles as 0.5; `extract_cycles(series)` yields (range, mean, count, i_start, i_end) so the direction (discharge if series[i_end] < series[i_start]) can be read off, which Xu's convention needs | ASTM E1049-85 section 5.4.4; the production choice |
| BLAST-Lite `blast/utils/rainflow.py` | BSD-3 (same code vendored) | identical | use whichever is already a dependency |
| `fatpack` (Gunnstein Froseth) | ISC (permissive, MIT-like) | `find_rainflow_ranges`, range-mean matrices | fine but heavier (fatigue toolbox) |
| `ffpack` 0.3.3 (PyPI) | **GPL-3.0** (PyPI classifier) | - | do not use in the product |

### 6.8 Worked example: 100 kWh LFP pack, Lahore, June

Pack: 100 kWh nominal, 384 V, 260 Ah, Rs 52,000 per kWh installed (R = Rs 5.2 million), warranty 6,000 cycles at 80 % DoD, commissioned 3 years ago, enclosure 45 C in the afternoon.

Day's SoC trace (reversal points): 40 % (06:00) -> 95 % (13:00, PV) -> 30 % (21:00, evening peak and a 2-hour outage) -> 60 % (02:00, off-peak grid charge) -> 45 % (05:00, night load) -> 95 % next day. Rainflow by hand (ASTM): the 60 -> 45 -> 95 sequence yields one full cycle of depth 15 points; the residue 40 -> 95 -> 30 -> 95 gives half-cycles of 55, 65, 65. Under Xu's convention the day is charged for two discharges: **65 points (65 kWh) and 15 points (15 kWh)**, 80 kWh in all.

1. Flat throughput price: 80 kWh x Rs 10.83 = **Rs 867**.
2. Depth-aware (calibrated LFP curve): cost of a 65 % cycle Rs 690 (N = 7,540) plus a 15 % cycle Rs 146 (N = 35,700) = **Rs 833** (4 % below flat; a day of many shallow cycles would be about 10 % below, a single 100 % cycle 3 % above).
3. Calendar: holding 100 kWh at 95 % SoC in a 45 C enclosure for 8 hours costs Rs 0.126 x 100 kWh x 32 steps = **Rs 403**, against Rs 269 at 50 % SoC and Rs 126 at 95 % in a 25 C room. The optimiser therefore sees Rs 134 of value in finishing the PV charge at 16:00 instead of 13:00 on a day with no outage risk before the peak, and nothing in winter.
4. Day total: about **Rs 1,240 of wear** against, for scale, a peak-window saving of 65 kWh x (Rs 48 peak minus Rs 30 off-peak: tariff figures illustrative, from the tariff note of this plan, UNVERIFIED here) = Rs 1,170 for the TOU shift alone before the outage value, which is why the cycling decision is close on tariff alone and is carried by the outage and genset value at most sites.

For the summary table: c_cyc = Rs 10.8 per kWh (range 7.5 to 13.5), segment prices Rs 10.3 to 13.2 per kWh, calendar slope b(45 C) = Rs 0.109 per kWh-pack per step per unit SoC (0.034 at 25 C), about Rs 4 per kWh of pack per day for sitting full in summer versus a quarter of that at 20 % SoC.

## 7. Licence notes per source used in sections 2 to 6

Every licence file below was read on 6 November 2026 either in the scratchpad clone (path given) or through the GitHub licence API or raw file; the quoted line is from that text. "Re-implement" means the product contains none of the source's files, only logic re-written from reading it; "facts only" means register addresses, value ranges and behaviour descriptions were taken, which are facts, not expression.

| Source | Licence and where read | Key line quoted | What the product takes | Attribution the product must carry |
| --- | --- | --- | --- | --- |
| victronenergy/dbus-systemcalc-py (`delegates/loadshedding.py`) | MIT, `dbus-systemcalc-py/LICENSE`, "Copyright (c) 2014 mpvader" | "The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software." | Re-implemented load-shedding state machine (2.1); no file copied | If any code is copied or closely translated: the MIT text with the mpvader copyright line in THIRD_PARTY_NOTICES. For a clean re-implementation attribution is not required; include a courtesy line "load-shedding logic modelled on Victron dbus-systemcalc-py (MIT)" |
| victronenergy/dbus_generator (`startstop.py`, `dbus_generator.py`) | MIT, `dbus_generator/LICENSE`, "Copyright (c) 2014 mpvader" | same line | Re-implemented genset condition stack and defaults (2.2) | same as above |
| kellerza/sunsynk (definitions, `rwsensors.py`, `helpers.py`) | Apache-2.0, `sunsynk/LICENSE` (standard text; head read, "Version 2.0, January 2004") | Section 4(a)-(b) of the standard text (read in the clone's copy): a redistributor "must give any other recipients of the Work or Derivative Works a copy of this License" and "must cause any modified files to carry prominent notices stating that You changed the files" | Facts only: Deye register map, option tables, HHMM encoding (3.1) | None required for facts; if any definition file is vendored: the Apache-2.0 text, the project's NOTICE if one exists (none seen in the clone root: UNVERIFIED), and change notices |
| wills106/homeassistant-solax-modbus (`plugin_growatt.py`) | Apache-2.0, `solax-modbus/LICENSE` | same section 4 | Facts only: Growatt window registers (3.4) | as above |
| kbialek/deye-inverter-mqtt | Apache-2.0, `deye-inverter-mqtt/LICENSE` | same section 4 | Facts only: HV three-phase register cross-check (3.1) | as above |
| davidrapan/ha-solarman (`deye_hybrid.yaml`) | MIT, `ha-solarman/license`, "Copyright (c) 2024 David Rapan <david@rapan.cz> and Contributors" (the file uses the copyright sign) | "Permission is hereby granted, free of charge, to any person obtaining a copy of this software..." | Facts only: value ranges and bit masks (3.1) | None for facts; MIT notice if the YAML is vendored for the twin |
| mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant (`modbus_sungrow.yaml`) | MIT, `mkaiser-sungrow/LICENSE`, "Copyright (c) 2025 Martin Kaiser" | same MIT permission line | Facts only: 0-based addresses, EMS mode values (3.3) | None for facts; MIT notice if vendored |
| jblance/mpp-solar | MIT, `mpp-solar/LICENSE`, "Copyright (c) 2017 jblance" | same MIT permission line | Not used in sections 2 to 6 of this note; read in the first run for Voltronic/Axpert command facts that U3 5.8's Voltronic emulation will need | MIT notice if any command table is vendored |
| wlcrs/huawei-solar-lib (`registers.py`, `periods.py`, `register_values.py`) | **AGPL-3.0**, `huawei-solar-lib/LICENSE.md` | Section 13: "if you modify the Program, your modified version must prominently offer all users interacting with it remotely through a computer network ... an opportunity to receive the Corresponding Source of your version" | **Facts only** (3.2): register addresses, period encoding, validation rules, working-mode enum. **No file, function or docstring may enter the product**; the Huawei Modbus client is written from the register facts and Huawei's own documentation | None (no code taken). Record in the OSS review that the library was read and nothing copied; keep the clone out of the product repositories |
| Sungrow "Communication Protocol of Residential Hybrid Inverter" V1.1.11 | Proprietary PDF; cover says it shall not be distributed or forwarded | - | Facts only (3.3) | None; do not redistribute the PDF; cite the document title and version in the template |
| NREL/BLAST-Lite 1.1.3 | BSD-3-Clause, `blast-lite/LICENSE`, "Copyright (c) 2023, Alliance for Energy Innovation, LLC"; `NOTICE` with the DOE contract clause | "Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer." NOTICE: "produced by the Alliance for Energy Innovation, LLC (Contractor) under Contract No. DE-AC36-08GO28308 with the U.S. Department of Energy" | Coefficients and equations (6.3) used in the bench; the package itself as a bench dependency | BSD-3 text and the NOTICE text in THIRD_PARTY_NOTICES of the bench folder; clause 3: do not use NREL's or the Alliance's name to endorse the product |
| NREL/PySAM 7.1.1 (`BatteryStateful`) | BSD-3-Clause, GitHub `LICENSE`, "Copyright (c) 2019, 2022 Alliance for Energy Innovation, LLC" | same first condition | Bench replay (6.6) | BSD-3 text in the bench notices; same non-endorsement rule |
| iamlikeme/rainflow 3.2.0 | MIT, `LICENSE.txt` (GitHub API), "Copyright (c) 2018 Piotr Janiszewski" | MIT permission line | Production dependency for cycle counting (6.7) | MIT text with that copyright line in THIRD_PARTY_NOTICES |
| Gunnstein/fatpack | ISC, GitHub `LICENSE`, "Copyright (c) 2017, Gunnstein Thomas Froseth" | "Permission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted" (conditioned on keeping the notice) | Not used (listed as an alternative) | ISC notice if ever used |
| ffpack 0.3.3 | GPL-3.0 (PyPI classifier "License :: OSI Approved :: GNU General Public License v3 (GPLv3)"; the `license` field is empty) | - | **Not used** | - |
| tue-battery/PyBatterySE 2.0.0 | BSD-3-Clause, `LICENSE.txt` (GitHub API, branch develop), "Copyright (c) 2026, A.M.A (Muiz) Sheikh" | standard BSD-3 first condition | EKF reference and possible bench dependency (5.4); its companion PyBatteryID licence UNVERIFIED | BSD-3 text with that copyright line if used; non-endorsement |
| rlabbe/filterpy | MIT, GitHub `LICENSE`, "Copyright (c) 2015 Roger R. Labbe Jr" | MIT permission line | Recommended production EKF class (5.4) | MIT text with that copyright line |
| pybamm-team/PyBaMM | BSD-3-Clause, GitHub licence API, "Copyright (c) 2018, the PyBaMM team." | standard BSD-3 first condition | Bench only: Prada2013 LFP OCV for synthetic traces (5.4); the parameter values themselves come from Prada et al. 2013 (journal paper, facts) | BSD-3 text in the bench notices; cite Prada et al. 2013 in the bench README |
| Plett, Battery Management Systems vol. II, ESC toolbox code | Licence UNVERIFIED (not found online on 6 Nov 2026) | - | Equations re-derived from the published model only; no code | Cite the book in the design doc |
| Xu et al. 2018 (IEEE TPWRS; arXiv 1707.04567) | IEEE copyright on the article; the arXiv preprint carries arXiv's non-exclusive licence (UNVERIFIED which) | - | Equations (6.2) as facts | Cite the paper in the design doc |
| Victron Dynamic ESS docs, wlcrs huawei_solar wiki, SolarCitizen price page, BloombergNEF summaries | Web pages, read for facts | - | Figures and behaviours (3.5, 3.2, 6.1) | Cite in docs |

Attribution mechanics for the build:

1. `THIRD_PARTY_NOTICES.md` at the root of the backend repository listing, verbatim, each MIT, BSD-3 and ISC notice for any package or file the product ships or imports (today: `rainflow`, `filterpy`; plus any vendored definition file), and the Apache-2.0 text plus NOTICE for any Apache-derived file; a separate `bench/wear/THIRD_PARTY_NOTICES.md` for BLAST-Lite, PySAM, PyBaMM, PyBatterySE.
2. A "sources read, nothing copied" list (Victron, kellerza, solax-modbus, deye-inverter-mqtt, ha-solarman, mkaiser, mpp-solar, huawei-solar-lib, Sungrow PDF) in the OSS review record, with the huawei-solar-lib AGPL entry explicitly marked "facts only, clone excluded from product repositories", so a future audit can see the decision.
3. BSD-3 clause 3 (no endorsement) applies to NREL, PyBaMM and PyBatterySE: marketing must not say "powered by NREL" or similar; "uses the open-source BLAST-Lite ageing model from NREL (BSD-3)" in technical documentation is fine.
4. Register maps and value ranges are facts and need no notice, but the per-brand template files should carry a comment naming the community source and licence from which each address was cross-checked, which is both good manners and the audit trail for a wrong address.
