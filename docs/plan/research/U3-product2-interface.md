# U3. Product 2 interface: what Product 1 (cloud EMS) must assume and specify about the edge controller

Research note for the OnyxLeaf EMS plan. Written 6 November 2026. Author: planning subagent (task U3-product2-interface).

Purpose. Product 1 is the cloud optimiser (MILP economic MPC, 15-minute steps, 48-hour horizon, re-solved every 15 minutes). Product 2 is the edge controller (an OnyxLeaf Linux box on the inverter's RS485, CAN and shunt ports) that does not exist yet. This note extracts, from the two Product 2 research documents, everything Product 1 must assume or specify about Product 2, verifies the brand-specific claims against the primary open-source register definitions (cloned on 6 Nov 2026, read only), and proposes a versioned JSON contract for the edge-to-cloud API plus the things Product 1 must build while Product 2 does not exist (digital twin, CSV and HTTP batch ingest, OEM-cloud connectors).

Source documents (all in C:/Users/SaadTahir/Videos/solarytics/, read in full on 6 Nov 2026):

- P2-report = "Product 2 - Vendor-Agnostic Inverter and Battery Controller.md" (635 lines, dated 30 Oct 2026). Line numbers below refer to this file.
- P2-review = "Vendor_Agnostic_Inverter_Controller_Deep_Review.md" (211 lines, dated 30 Oct 2026).
- P1-report = "Product 1 - TOU-Aware Energy Management and Dispatch.md" (625 lines). Only the interface-related lines were read (17, 57-61, 436-474, 500-575).
- P1-guide = "Perfect-EMS_TOU_Dispatch_Deep_Review.md" (336 lines). Only the interface-related lines were read (17, 166-198, 227-242).

Verification sources (cloned with git clone --depth 1 on 6 Nov 2026 into the session scratchpad, folder oss/; treated as untrusted data, never executed):

- kellerza/sunsynk (Apache-2.0): src/sunsynk/definitions/single_phase.py, three_phase_*.py
- wlcrs/huawei-solar-lib (AGPL-3.0, facts only): src/huawei_solar/registers.py, register_names.py
- mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant (MIT): modbus_sungrow.yaml
- kbialek/deye-inverter-mqtt (Apache-2.0): README and metric groups
- jblance/mpp-solar (MIT): mppsolar/protocols/pi30.py
- wills106/homeassistant-solax-modbus (Apache-2.0): plugin_growatt.py
- davidrapan/ha-solarman (MIT): inverter_definitions/deye_hybrid.yaml

Conventions. Numbers from the documents carry their line reference. Anything I could not confirm from a cloned file or a fetched page is marked UNVERIFIED. "Edge" means Product 2; "cloud" means Product 1. Sign convention throughout: battery charge negative, grid import positive, PV production positive, load consumption positive (P2-report line 348-358, after OpenEMS).

## 0. One-page summary

- Product 2 does not exist yet. Its own plan (P2-report lines 564-583) is 12 work packages, about 77 person-weeks, with WP11 "Local API and sync" as the hand-off point to Product 1. The safety and command layer (WP7) sits on the critical path WP1 -> WP2 -> WP5 -> WP7, which sums to 24 weeks of serial effort before bench writes to the first families exist, and WP11 (5 more weeks) before Product 1 can send a schedule end to end. Field-proven writes come only after WP12 (10 weeks of pilots). Realistic earliest date for Product 1 to rely on edge writes: about 7 months after Product 2 starts for the bench, about 10 months for pilot sites (derived, see section 7; the person-week figures are the report's own rough estimates).
- Therefore Product 1 must be built to run without Product 2 for its whole first year: ingest from CSV, HTTP batches, OEM clouds or Enerlytics; deliver plans as daily inverter TOU slots (Mode A) written by whatever path exists (an OEM app, an installer, or a thin per-brand writer); and test the Mode B (15-minute setpoint) path against a simulated edge that speaks the contract in section 5.
- The edge is the safety authority. Product 1 asks for abstract outcomes (battery power or mode, SoC floor and target, export limit, grid-charge permission) with a validity window; Product 2 maps them to whitelisted registers, enforces ranges, write budgets and revert timers, reads back every write, and reverts the inverter to its own mode on plan expiry or link loss (P2-report lines 451-460 and 470-476; P1-report lines 462-464).
- Telemetry is normalised to a SunSpec-named, OpenEMS-signed data model (P2-report lines 346-358). Product 1 needs it at 1-minute resolution with a grid-present flag, PV, load, grid and battery power, BMS SoC, and genset status (P1-report lines 471-474; P1-guide line 188). The edge samples at 1 to 5 s (P2-report lines 396, 507, 522) and aggregates.
- Battery state: both documents claim the independent SoC estimator (P2-report lines 393-402 and WP8; P1-report component 3 line 455) and P1-report open question 8 (line 551) asks who owns it. Recommendation in section 3.6: Product 2 owns the 1 Hz estimator because it owns the shunt; Product 1 owns a cloud re-estimate from 1-minute data for sites without an edge box, and the contract carries both numbers plus a trust score and a source tag.
- Genset: read-only in Product 2 release 1 (P2-report line 62: "Start, stop and synchronisation (Elum, ComAp and Enercon own this)"). Product 1's planned "genset start request" output (P1-report line 457) is therefore advisory only unless the owner changes Product 2's scope (open question 6).
- Per-brand Mode A capacity, verified from the cloned definitions on 6 Nov 2026: Deye 6 slots (time, power, SoC, voltage, flags); Huawei LUNA2000 14 TOU periods (start, end, charge or discharge, weekdays) plus 10 fixed charge/discharge periods with power; Growatt SPH 3 battery-first plus 3 grid-first windows (TL-XH: 9 windows); Sungrow SH has no daily slot table in the public map, only an EMS mode with forced charge/discharge power, min/max SoC and a feed-in limit; Voltronic PI30 has no slots at all, only output and charger priority commands. Details in section 2.

## 1. Telemetry schema Product 1 must assume from Product 2

### 1.1 What the documents say

- P2-report lines 346-358 define the common data model: Grid (P per phase and total, Q, V, f, import and export energy; import positive), PV (P per MPPT and total, energy; production positive), Battery (P, V, I, BMS SoC, OnyxLeaf SoC with uncertainty, SoH, temperatures, cell extremes, limits, capacity; charge negative), Load (P, energy; consumption positive), Inverter (status, alarms, rated power, mode), Controls (mode, battery setpoint, export limit, TOU slots, reserve SoC, release), Meta (identity, firmware, template ID, quality flags, trust score). "Names follow SunSpec; signs follow OpenEMS."
- P2-report lines 442-446: software layers include "Normaliser into the common data model, with quality flags" and "Validation monitor: ranges, monotonic counters, physics checks, canary reads after firmware changes".
- Cadence on the edge: battery EKF at 1 Hz (line 396); capture protocol logs 1 to 5 s data (line 507); 30-day soak demands "at least 99.5% data completeness at 5 s" (line 522). Cloud sync is "outbound mutual-TLS MQTT, store and forward" (line 447).
- P1-report line 17: Product 1 assumes "normalised telemetry (PV power, load, SOC, grid status, genset status, at >=1-min resolution)". Lines 471-474 list the per-site data requirement: 1-minute PV, load, grid import and export, battery power, voltage, current, temperature, BMS SoC and SoH; genset status, output, fuel log or tank level, and rating. Line 462: "Product 2 to Product 1: normalised 1-minute telemetry and a per-device list of writable primitives".
- P1-guide line 188 (minimum viable data model): "time-series at 1-min (PV W, load W, SOC, grid import/export W, grid-present flag, genset run flag/fuel if metered); plans (96x15-min setpoint vectors, versioned); events (outages, genset starts, overrides)".
- Savings verification is scored at 1-minute resolution (P1-report line 468), so 1 minute is the contract cadence for stored series; sub-minute data stays on the edge except for events.

### 1.2 Telemetry point table (contract v1.0)

Cadence column: "1 min" means the edge sends a 1-minute aggregate (mean for powers, last for SoC and states, min and max where noted); "event" means sent when it changes, with a timestamp. Source column names the device the edge normally reads the point from; the edge decides, Product 1 only sees the normalised key. Required means Product 1 refuses to run closed-loop control on a site whose capabilities message does not list the point.

| Key | Unit | Cadence | Required | Typical source device | Notes and source |
| --- | --- | --- | --- | --- | --- |
| ts | ISO 8601 UTC, plus site tz in metadata | per row | required | edge clock (NTP; RTC on box, P2-report line 432) | Row timestamp is the start of the minute. |
| grid_present | bool | 1 min (value at end of minute) plus min over the minute | required | inverter grid-status register, or grid meter V and f | Deye single-phase register 194 "Grid connected" (sunsynk single_phase.py line 194). P1-guide line 188 names the flag explicitly. Also sent as outage events, section 5.5. |
| grid_power_w | W, import positive | 1 min mean | required | grid meter (SDM630, DTSU666) or inverter CT register | Deye reg 169 Grid power, 172 Grid CT power (sunsynk lines 53-58). Sungrow 5601 meter W (P2-report line 147). |
| grid_import_wh, grid_export_wh | Wh, cumulative counters | 1 min (last) | optional (required if no grid_power_w) | meter | Counters let the cloud rebuild bills exactly; monotonic check on the edge (P2-report line 443). |
| grid_voltage_v, grid_frequency_hz | V, Hz | 1 min mean | optional | meter or inverter | Needed by physics checks and by the outage detector when no explicit flag exists. |
| grid_power_l1_w, l2, l3 | W | 1 min mean | optional | meter | Three-phase C&I sites; MDI is per site not per phase, so optional. |
| pv_power_w | W, production positive | 1 min mean | required | inverter (sum of MPPTs) | Deye regs 186-189 PV1-4 power (sunsynk lines 96-124). |
| pv_energy_wh | Wh cumulative | 1 min (last) | optional | inverter | Deye reg 108 Day PV energy (0.1 kWh) is daily, not cumulative; the edge converts. |
| load_power_w | W, consumption positive | 1 min mean | required | inverter load register or load meter | Deye reg 178 Load power (sunsynk line 66); Sungrow 13008 load (P2-report line 147). On sites where load is derived (PV + grid + battery), the edge must flag quality = derived. |
| load_power_backup_w | W | 1 min mean | optional | inverter | Sungrow "Total backup power" (mkaiser YAML line 598). Separates critical load for the outage reserve rule. |
| battery_power_w | W, charge negative | 1 min mean | required | inverter or BMS | Deye reg 190 (sunsynk line 31); Sungrow 5214-15 (P2-report line 147). Sign per OpenEMS (P2-report line 352). |
| battery_voltage_v, battery_current_a | V, A (charge negative) | 1 min mean | required for sites with a battery | shunt if fitted, else inverter or BMS | Deye regs 183, 191 (sunsynk lines 29, 32). Shunt current accuracy 0.4 percent, 0.01 A (P2-report line 378). |
| battery_temp_c | deg C | 1 min mean | optional | BMS or NTC | Deye reg 182 (sunsynk line 28). Temperature derating in the optimiser (P1-guide line 181). |
| battery_soc_bms_pct | percent | 1 min (last) | required for sites with a battery | BMS via inverter or CAN 0x355 | Deye reg 184 (sunsynk line 30); Pylontech CAN 0x355 SoC and SoH (P2-report line 138). Known to be wrong by 10 to 30 points on cheap LFP packs (P2-report lines 362-372). |
| battery_soc_est_pct, battery_soc_sigma_pct | percent | 1 min (last) | optional (required when control uses a reserve rule) | edge estimator (EKF, P2-report lines 395-400) | "Publish SoC with its uncertainty so Product 1 can hold a margin" (line 400). |
| battery_trust_score | 0 to 1 | 1 min (last) | optional | edge estimator | "Compute a trust score from the gap between BMS SoC and the estimate; flag gaps above, say, 10 points" (line 399). |
| battery_soc_source | enum: bms, edge_ekf, cloud_ekf | 1 min | optional | edge | Tells Product 1 which number is authoritative (section 3.6). |
| battery_soh_pct | percent | daily (last) | optional | BMS or estimator | CAN 0x355 SoH; estimator anchors (line 398). |
| battery_capacity_wh_usable | Wh | daily | optional | estimator or nameplate | Drives the SoC-to-kWh conversion in the optimiser. |
| battery_charge_limit_a, battery_discharge_limit_a | A | 1 min (last) | optional | BMS (CAN 0x351 limits, line 138) | Deye regs 314-315 (sunsynk lines 34-35). Lets the optimiser cap power per step. |
| battery_cell_v_min, cell_v_max, cell_t_max | V, deg C | 1 min | optional | BMS | Estimator anchors and safety alarms. |
| inverter_state | enum (SunSpec 103 St style) | event plus 1 min | required | inverter | Deye reg 59 "Overall state" (sunsynk line 189); Sungrow running-state bits 0x0800 forced, 0x4000 external EMS (mkaiser lines 1852, 1866). |
| inverter_mode_active | enum: self_use, tou_native, forced_charge, forced_discharge, external_ems, backup, unknown | event plus 1 min | required for control | inverter control registers read back | Needed for the counterfactual baseline (which mode the inverter would have run) and for fallback confirmation. |
| inverter_alarms | list of codes | event | optional | inverter | Normalised code plus raw vendor code. |
| inverter_rated_power_w | W | capabilities | required | template | Bounds for power setpoints. |
| export_limit_active_w | W | event plus 1 min | required for control | inverter | Read-back of the feed-in limit (Sungrow 13074, Huawei 47416). |
| genset_running | bool | event plus 1 min | required at genset sites | genset controller Modbus, or AUX/gen input power above threshold | P2-report line 62: read status over Modbus only. Deye reg 166 "Gen power" (sunsynk line 83) is a fallback proxy. |
| genset_power_w | W | 1 min mean | optional | genset controller or inverter gen input | Deye three-phase regs 667-671 Gen power (three_phase_common.py line 103). |
| genset_fuel_l or genset_tank_pct | L, percent | 1 min or event | optional | genset controller, tank sensor, or manual log | P1-report line 474. Manual fuel logs come through the HTTP batch path, not the edge. |
| genset_runtime_h, genset_starts | h, count | daily | optional | genset controller | Deye "Generator Operating Time" (ha-solarman deye_hybrid.yaml line 1408). |
| meta.template_id, meta.firmware, meta.quality_flags | string, string, bitfield | capabilities plus event | required | edge | Template change or firmware change must raise an event; P2-report line 443 canary reads. |
| meta.clock_offset_s | s | 1 min | optional | edge | Pakistani sites lose NTP during outages; the cloud needs to know when timestamps drifted. |

Quality flags per row (bitfield, section 5.2): 0x01 derived (not measured), 0x02 stale (last good value repeated), 0x04 out_of_range_clamped, 0x08 sentinel_seen (0xFFFF or 0x7FFF, P2-report line 124), 0x10 physics_check_failed, 0x20 clock_unsynced, 0x40 partial_minute (fewer than 50 percent of samples).

### 1.3 What Product 1 should NOT expect from the edge

- Reactive power, per-MPPT detail and cell-level data are optional; Product 1 must run on the required set only (P1-report line 17: "Every optimisation feature below must degrade gracefully when only a subset of setpoints is writable" applies to reads as well).
- Sub-minute series: the edge keeps 1 to 5 s samples locally for the estimator and for installer diagnostics; the cloud receives 1-minute aggregates plus events. A debug mode may request a 1 s window for a bounded time (section 5.7).
- Tariff, prices and forecasts never come from the edge; they are Product 1's own inputs.

## 2. Command schema: what Product 1 may ask the edge to do

### 2.1 Design rule

P1-report line 463: "Product 1 to Product 2: a 48-hour schedule of abstract setpoints with validity time and fallback rules. Product 2 enforces write whitelists, rate limits and revert timers." P2-report line 342: templates map "intents" to write sequences: "set mode, battery power, export limit, TOU slot, release". P2-report line 472: "the cloud sends signed schedules, never raw writes". So the command vocabulary is a small set of abstract primitives; the edge template decides which registers implement each, and the capabilities message (section 5.3) tells Product 1 which primitives a given device supports and with what limits.

### 2.2 Abstract primitives (both modes)

| Primitive | Fields | Semantics | Mapped on Deye | Mapped on Huawei LUNA2000 | Mapped on Sungrow SH | Mapped on Growatt SPH/TL-XH | Mapped on Voltronic PI30 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| battery_mode | enum: self_use, hold, charge, discharge, native | Coarse intent for the step. hold = no discharge (keep SoC); native = release to the inverter's own mode. | Program "charge" flag bits and work mode (regs 274-279 bits 0-4; 244 work mode) | Working mode 47086 (0 adaptive, 1 fixed, 2 max self-consumption, 5 TOU, 6 third-party) plus forcible 47100 (0 stop, 1 charge, 2 discharge) | EMS mode 13050 (0 self-consumption, 2 forced, 3 external EMS, 4 VPP) plus command 13051 (0xCC stop, 0xAA charge, 0xBB discharge) | Priority 1044 and time-window mode bits (load first, battery first, grid first) | POP 00/01/02 output priority, PCP 00-03 charger priority |
| battery_power_w | signed W (charge negative), with abs max from capabilities | Power target for the step; the edge clamps to template min/max/step and BMS limits. | Program power regs 256-261 (max rated), max charge/discharge current 0xD2/0xD3 | Forcible charge power 47247, discharge power 47249 (U32 W); fixed periods 47200 carry power per period | 13052 "0 to 5000 W for SH*K, 0 to 100 percent for SH*.0RT" (mkaiser lines 1173-1176) | Battery-first charge rate 1090 (percent), grid-first discharge rate 1070 (percent); EMS charging rate 3047 (TL-XH) | MCHGC max charge current (A), MUCHGC utility charge current; no discharge power control |
| soc_floor_pct | percent | Do not discharge below this; the outage reserve. | Program capacity regs 268-273 (min = battery low cap reg 219); shutdown/low SoC 0xD9-0xDB | Backup power SoC 47102 (0.1 percent); grid charge cutoff 47088 | Min SoC 13059, reserved SoC for backup 13100 | Load-first min SoC 608, grid-first min SoC 1071, EMS discharging stop SoC 3048 (off grid) | PBDV back-to-battery voltage (voltage, not SoC) |
| soc_target_pct | percent | Charge up to this (and stop). | Program capacity in a "charge" program | Forcible target SoC 47101 with setting mode 47246 SOC; grid charge cutoff 47088 | Max SoC 13058 | Battery-first max SoC 1091; charging stop SoC 3048 | PBCV back-to-utility voltage |
| grid_charge_allowed | bool, with optional max W | Permit charging from the grid in this step. | Program charge flag 1 "Allow Grid"; grid charge enable (3-phase reg 130), current 128 | Charge from grid 47087; power of charge from grid 47242, max 47244 | EMS mode forced charge | AC charge enable (window mode battery first); VPP AC charge max power 30215 persistent or 30216 volatile | PCP 00 utility first |
| gen_charge_allowed | bool | Permit charging from the genset. | Program charge flag 2 "Allow Gen" | not applicable (UNVERIFIED) | not applicable | not applicable | PCP (utility and genset share the AC input) |
| export_limit_w | W, 0 = zero export; null = release | Cap grid export (net billing makes export nearly worthless, P2-report line 9). | Work mode 244 (1 zero export to load, 2 zero export to CT), max sell power 245, zero export power 0xCE (-200 to 500 W band) | Active power control mode 47415 (5 zero power, 6 limited W, 7 limited percent), max feed 47416 W or 47418 percent | Feed-in limit 13074 W with mode 13087 (0xAA enable); SG string 5010-11 | Limit grid export 122 (0 disabled, 1 meter 1, 2 meter 2, 3 CT), grid export limit 123 (-100 to 100 percent, scale 0.1) | not applicable (off-grid family) |
| pv_curtail_pct | percent of rated | Limit PV output (rare in Pakistan: only when export is forbidden and the battery is full). | Max solar power reg 53 (W) | Active power fixed derating 40126 | 5007-08 power limit (SG string) | Active power rate (UNVERIFIED register) | not applicable |
| genset_request | enum: none, prefer_start, prefer_stop | Advisory only in Product 2 release 1 (P2-report line 62). The edge logs it; a genset controller or an operator acts. | Deye three-phase has "Gen Signal On" reg 140 bit 1 and gen peak-shaving power 292; whether Product 2 whitelists it is an owner decision (open question 6) | none | none | none | none |
| release | none | Return all controls to the inverter's own mode immediately (explicit fallback). | Use Timer 248 bit 0 off, restore captured native settings | Working mode back to the captured value, forcible 47100 = 0 | EMS mode 13050 = 0, command 13051 = 0xCC | Restore window table captured at onboarding | POP/PCP back to captured values |

Register sources (all read on 6 Nov 2026 from the cloned repos): Deye from kellerza/sunsynk src/sunsynk/definitions/single_phase.py lines 242-355 and __init__.py lines 15-26, and davidrapan/ha-solarman inverter_definitions/deye_hybrid.yaml lines 1095-1300 and 1663-2180; Huawei from wlcrs/huawei-solar-lib src/huawei_solar/registers.py lines 515-610 and register_definitions/periods.py lines 21-46, 141-204, 263-277, register_values.py lines 82-157; Sungrow from mkaiser modbus_sungrow.yaml lines 1147-1240, 1271-1274, 2173-2226; Growatt from wills106 plugin_growatt.py lines 177-200, 640-740, 841-863, 960-972, 1296-1308, 2455-2670, 2798-2816; Voltronic from jblance/mpp-solar mppsolar/protocols/pi30.py lines 23-37, 111-135, 182-200, 314-330.

### 2.3 Mode B: 15-minute setpoint schedule

- Shape: one plan per site, 48 hours, 192 steps of 900 s, re-issued every 15 minutes by the cloud (P1-report line 446). Each step carries the primitives above as a step object; a missing field means "no change from the previous step". The first step is the one the edge executes now; later steps are the local fallback if the cloud goes quiet.
- The edge executor "follows the current step, corrects residuals with a 1-minute rule layer, and reverts to the inverter's own mode when the plan expires" (P1-report line 446). The 1-minute rule layer is Product 2 code (P1-report line 457: "Command set defined by P1, run on Product 2 edge"); the contract therefore also carries rule parameters: residual band (W) within which the edge does not re-write, and the maximum write rate per primitive.
- Brand feasibility of Mode B (true setpoint every 15 minutes or faster):
  - Sungrow SH: yes, this is what EMS mode 3 "External EMS" and the forced charge/discharge command exist for; mkaiser status bit 0x4000 "Running in External EMS mode" (line 1866). The command and power registers (13051, 13052) are treated as volatile by the community package (it refreshes them from automations), UNVERIFIED in the vendor PDF.
  - Huawei: yes via forcible charge/discharge (47100, 47101, 47246-47249) under "third-party control" working mode 6, but writes need the installer login (P2-report line 150; P2-review line 133), and the dongle is slow (10 s timeout, P2-report line 125). Per-step writes are feasible; per-minute writes are not.
  - Deye: no clean volatile setpoint. The community writes program power and capacity registers (EEPROM-class, P2-report line 131 analogue for SolaX; kbialek README line 399 "Writing invalid values may damage the inverter"). Mode B on Deye must be implemented as "rewrite the current program slot" with a write budget, or deferred until Deye's newer "VPP" or active-power-regulation registers are bench-tested (kbialek active power regulation 0-120 percent, README line 289, which limits output not battery flow). UNVERIFIED whether a volatile battery setpoint exists on Deye.
  - Growatt: SPH only through window tables (EEPROM); TL-XH has 30216 "non persistent value to limit EEPROM writes" for AC charge max power only (plugin line 860). Partial Mode B.
  - Voltronic: no.
  - Solis (second wave): yes by design: dispatch writes with a failsafe of 1 to 1440 minutes (default 30) and power 0 to 240,000 W in 100 W steps, firmware flag register 34502 = 0xAA55 required; "The inverter reverts to normal operation by itself if no dispatch write arrives within this window" (Pho3niX90/solis_modbus discussion 495, fetched 6 Nov 2026; register numbers of the 44100 block UNVERIFIED).
  - SunSpec 704 devices (SMA, Fronius, Western brands): yes with built-in reversion: WSetRvrtTms "Set active power reversion time" (Secs), WSetRvrt, WMaxLimPctRvrtTms (sunspec/models model_704.json, fetched 6 Nov 2026). Battery model 802 has CtrlHb "Controller Heartbeat ... incremented every second with periodic resets to zero" (model_802.json).
- Consequence for Product 1: Mode B is a Sungrow, Huawei, Solis and SunSpec feature in the first year. For Deye and Growatt, Product 1 should emit Mode A plans even after Product 2 exists, and the edge converts a Mode B plan to slots where needed (section 2.5).

### 2.4 Mode A: daily inverter TOU-slot plan

- Shape: one plan per site per day (or per 48 hours), a list of slots with start time, end time, battery_mode, battery_power_w, soc_target_pct or soc_floor_pct, grid_charge_allowed, gen_charge_allowed, export_limit_w, and weekday mask where the brand supports it. The cloud compresses its 192-step optimum into at most N slots, N from the capabilities message. Rationale: "A few writes a day avoid the flash wear SMA warns of" (P1-report line 448).
- Compression is Product 1's job (it knows the economics); conformance to the brand's slot grammar is the edge's job (it knows the registers). Product 1 must still know N and the grammar to compress sensibly, which is why the capabilities message carries the slot_model object (section 5.3).

Per-brand slot grammar, verified 6 Nov 2026:

| Brand family | Slot count | Per-slot fields | Grammar constraints | Persistence | Source |
| --- | --- | --- | --- | --- | --- |
| Deye single-phase (and Sunsynk, rebadges) | 6 programs | time (HHMM packed U16, 0000-2359), power W (0 to rated), capacity percent (min = battery low cap), voltage (0.01 V, alternative to SoC), flags U16: bits 0-1 charge source (0 none, 1 grid, 2 gen, 3 grid and gen), bits 2-4 mode (0 none, 1 general, 2 backup, 3 charge), sell bit | Programs are a ring: Prog1 time min is Prog6 time, each next time must be greater than or equal to the previous (sunsynk single_phase.py lines 286-298); the 6 slots cover the full day, so "no change" slots still count. Use Timer (reg 248 bit 0) must be on. Deye three-phase uses regs 148-153 time, 154-159 power (HV x10), 166-171 capacity, 172-177 flags (three_phase_common.py lines 337-382). | EEPROM-class holding registers (assume; UNVERIFIED by vendor doc). kbialek writes the whole table in one go after buffering (README lines 329-335). | sunsynk single_phase.py lines 286-355; kbialek docs/metric_group_deye_hybrid_timeofuse.md; ha-solarman deye_hybrid.yaml lines 1717-2180 |
| Deye sell bit conflict | - | - | sunsynk uses bit 6 (0x40) for "sell" (single_phase.py line 328-334, "see #635"); kbialek documents bit 5 (value 32) "confirmed on deye_sg01hp3" and flags 1 grid, 2 gen, 32 sell (timeofuse doc). Conflict is firmware or model dependent. UNVERIFIED; the template must carry a firmware variant. | - | - |
| Huawei SUN2000 with LUNA2000 | 14 TOU periods (HUAWEI_LUNA2000_TOU_PERIODS = 14, periods.py line 141) plus 10 fixed charge/discharge periods (CHARGE_DISCHARGE_PERIODS = 10, line 271) plus 14 capacity-control (peak shaving) periods (PEAK_SETTING_PERIODS = 14, line 342) | TOU period: start_time and end_time in minutes since midnight, charge_flag (0 charge, 1 discharge), days_effective 7 booleans Sunday to Saturday (periods.py lines 29-43); fixed period: start, end, power W (lines 263-268) | start less than end, both within 0-1440, no multi-day span (lines 192-204); block at 47255 is 43 registers written as one unit (line 149); working mode 47086 must be 5 (TOU LUNA2000) for TOU periods or 1 (fixed) for fixed periods; "Excess PV energy use in TOU" 47299 chooses fed-to-grid vs charge | Holding registers, persistence UNVERIFIED; installer login required for writes (P2-report line 150) | huawei-solar-lib registers.py lines 515-593; periods.py |
| Sungrow SH hybrid | 0 native slots in the public map used by mkaiser | EMS mode 13050, command 13051, power 13052, max SoC 13058, min SoC 13059, feed-in limit 13074 and mode 13087, reserved SoC for backup 13100, backup mode 13075 | Mode A on Sungrow is emulated by the edge: it switches EMS mode and forced command at slot boundaries (2 to 6 writes a day). Whether SH firmware 2024+ exposes a "load adjustment" or time-period table is UNVERIFIED (the V1.1.9 PDF is "not for redistribution", P2-report line 147). | Community treats 13050-13052 as settings that persist across restarts (mkaiser automations re-apply); UNVERIFIED EEPROM class | mkaiser modbus_sungrow.yaml lines 1147-1240, 2173-2226 |
| Growatt SPH (GEN3 in plugin) | 3 battery-first windows (1100-1108) plus 3 grid-first windows (1080-1088); 3 more of each at 1019-1034 on some models (plugin lines 1313-1378) | begin HHMM, end HHMM, enable | Window tables are separate per priority mode; outside any window the inverter runs load-first. Rates and SoC limits are global, not per window (1090 charge rate, 1091 max SoC, 1070 discharge rate, 1071 min SoC) | EEPROM-class (Growatt 30215 persistent vs 30216 volatile shows the vendor distinguishes, plugin line 860) | plugin_growatt.py lines 2455-2670, 1014-1030 |
| Growatt TL-XH / MOD / MIN (GEN4) | 9 windows (3038-3058, pairs) | begin HHMM packed with enable bit 15 (32768) and mode bits: 8192 battery first, 16384 grid first, 0 load first; end HHMM | Pair must be written in one FC16 write (P2-report line 127; plugin write_method WRITE_MULTI_MODBUS, lines 640-737); end greater than or equal to begin (plugin lines 196-198) | EEPROM-class | plugin_growatt.py lines 177-200, 642-737 |
| Voltronic PI30 (Axpert, Inverex legacy, etc.) | 0 slots | POP (00 utility first, 01 solar first, 02 SBU), PCP (00 utility first, 01 solar first, 02 solar and utility, 03 solar only), MCHGC and MUCHGC current limits, PBCV and PBDV voltage thresholds | Mode A emulated by the edge as time-triggered POP and PCP changes; PSAVE "Save EEPROM changes" exists, which implies settings can be changed without committing to EEPROM (UNVERIFIED whether POP alone persists) | EEPROM when PSAVE is sent (P2-report line 156) | mpp-solar pi30.py lines 111-135, 182-200, 314-325 |
| Solis (second wave) | 6 scheduled periods on the inverter (solis_modbus discussion 495) | charge/discharge windows with power | Must be re-pushed within the failsafe window to stay active | Dispatch block is volatile by design (maintainer claim, unsourced) | fetched 6 Nov 2026 |
| SolaX (second wave) | Remote control at 0x7C, volatile, needs unlock (P2-report line 154) | - | "EEPROM life is limited by a number of write cycles which is often no more than 100000 write operations ... you should not make more than 27 modifications a day" (wills106 docs/automation-examples.md, fetched 6 Nov 2026) | 0x612 EEPROM vs 0x669 RAM (P2-report line 131) | - |

Mode A plan limits Product 1 should encode per site (from the capabilities message): max_slots, slot_time_resolution_min (Deye and Huawei 1 minute; Growatt 1 minute), fields_supported (power, soc_target, charge_source, weekday_mask), max_table_writes_per_day (engineering default 4, hard cap 100 per register per day, P2-report line 457 and 519), min_slot_minutes (default 30 so that a slot outlives one MPC period and read-back).

### 2.5 Converting the 192-step optimum into slots (Product 1 side)

- Group consecutive steps with the same battery_mode and grid_charge_allowed, merge runs shorter than min_slot_minutes into their neighbour by cost, then keep the N-1 most valuable boundaries (value = cost difference between keeping and dropping the boundary, re-evaluated by the bill engine). This is Product 1 code; it needs no brand knowledge beyond N and the field list.
- Each slot carries an SoC target rather than a power when the brand supports it (Deye capacity, Huawei forcible SoC target, Sungrow max/min SoC), because SoC targets are robust to forecast error while power targets are not (P1-report line 400: peak shaving is most robust to error).
- The plan always ends with a "native" slot or an explicit release so that the inverter returns to self-use if the next day's plan never arrives (fallback rule, section 3.4).

## 3. Safety contract between Product 1 and Product 2

### 3.1 What the documents require

P2-report lines 451-460 (safety rules), verbatim list:
1. "Whitelist writable registers per template; refuse all others, and never touch grid-protection settings."
2. "Tag each writable register volatile, dispatch or EEPROM; treat unknown as EEPROM."
3. "Prefer registers with revert timers: SolaX remote control, Growatt 30216, Solis dispatch (1 to 1,440 min), SunSpec reversion."
4. "Refresh setpoints as a heartbeat; set fallback timers explicitly, since Fronius defaults to 0, meaning never."
5. "Write on change only; cap EEPROM-class writes at, say, 100 a day per register (an engineering default)."
6. "Read back every write; respect atomic groups and unlock steps."
7. "On a crash, stop writing and let the inverter revert; never leave forced discharge latched."
8. "Log every write with who, why, and before and after values."

P2-report lines 470-476 (cyber design implications): "Enforce limits at the edge; the cloud sends signed schedules, never raw writes"; "Open no inbound ports"; "Stagger fleet commands and require two-person approval for fleet-wide changes".

P2-review line 135 (fail-safe design): write allow-list per device map with min/max/step/rate limits; watchdog or dead-man pattern, "every external setpoint has a TTL; on controller crash, comms loss, or cloud loss, the inverter is reverted to its own standalone mode"; read-back verification after every write with alarm on mismatch; two-tier product "monitoring-only mode by default, control unlocked per-site after a conformance check"; command audit log shipped to cloud.

Acceptance marks (P2-report lines 514-523): "Zero off-whitelist writes; 100% read-back match; EEPROM writes at most 100 a day per register"; "Fallback after controller power or link loss: Inverter back in own mode within 5 minutes". P1-report WP8 (line 570): "72-hour bench test: 100% read-back success; fallback within one control period on link loss; write budget kept". Comparable products: Eniris reverts to a preset within 600 s of link loss (P2-report line 230); Victron Dynamic ESS reverts after 12 h, which P1-guide line 169 calls too long for Pakistan.

### 3.2 What Product 1 may ask for

- Only the primitives in section 2.2, expressed in physical units, each with a validity window (valid_from, valid_until) and a plan-level expiry no later than 48 hours after issue.
- Values inside the device's declared range, step and sign; the cloud validates against the capabilities message before sending, so that a rejected plan is a bug, not a routine.
- At most one plan per site per 15 minutes under normal operation, plus at most one "release" and one "emergency hold" out of band per hour (engineering defaults, to be tuned).
- Never: raw register addresses, grid-protection or firmware settings, battery chemistry settings (charge voltages, equalisation), genset start or stop as a command (advisory enum only), or any write to an unlisted device.

### 3.3 What Product 2 enforces, and what Product 1 must assume it will do

| Rule | Edge behaviour | Product 1 consequence |
| --- | --- | --- |
| Whitelist | Primitives map only to template-listed registers with min, max, step, persistence class, atomic group, unlock step (P2-report line 341) | Product 1 keeps a per-site capabilities snapshot and refuses to plan with primitives not listed |
| Range clamp | Out-of-range values are clamped to the register range and reported in the ack as "clamped" | Product 1 re-plans with the achieved value, never assumes the requested one |
| Write budget | Per-register daily counters; EEPROM-class default 100 a day hard cap, Mode A table writes default 4 a day; when a budget is exhausted the edge keeps the last applied value and reports BUDGET_EXHAUSTED | Product 1's compression step must stay far below the budget; it must also expect that a mid-day re-plan may not be applied on Deye or Growatt |
| Write on change | Identical values are not re-written; the heartbeat refresh applies only to volatile or dispatch registers | Product 1 sends full plans; the diff is computed on the edge |
| Atomic groups and unlock | Growatt pair writes (3038-3039) in one FC16; Huawei installer login; SolaX unlock; all handled by the template | Product 1 sees only success, failure, or partial with a reason code |
| Read-back | Every write is read back; mismatch raises READBACK_MISMATCH and the edge retries once, then releases that primitive | Product 1 treats a mismatch as "unknown state" and re-plans from telemetry, not from the plan |
| Heartbeat and revert timer | Volatile setpoints are refreshed at the template's refresh interval (Sungrow 1 s period noted at P2-report line 147; Solis failsafe default 30 min; SunSpec WSetRvrtTms) and the revert timer is set to at most 2 control periods (30 min) | Product 1 must re-issue plans every 15 minutes; a missed cycle degrades gracefully, two missed cycles revert the site |
| Plan expiry | When valid_until passes with no newer plan, the edge executes the plan's fallback rule, default "inverter_native" (restore the settings captured at onboarding) | Product 1 must always include fallback rules and must prefer plans whose tail is safe (full reserve before the evening peak and known outage windows) |
| Link loss | If the cloud is unreachable, the edge keeps executing the cached plan to its end, then falls back; the cached plan is at most 48 h (P1-report line 446, Schneider 48-hour local fallback) | Product 1 plans 48 h even though it only trusts the first 15 minutes |
| Controller crash or power loss | Watchdog MCU can cut the write path (P2-report line 434); on restart the edge does not resume a plan older than one control period without a fresh signature check | Product 1 must detect an edge reboot (events, section 5.5) and re-issue |
| Local override | An installer or owner change on the inverter is detected by canary reads and reported as OVERRIDE_LOCAL; the edge stops writing that primitive until the cloud acknowledges | Product 1 pauses control on that site and shows the override in the UI |
| Template or firmware change | Canary reads detect a map change (P2-report line 443, 538); control is disabled (fail closed to read-only, line 441) | Product 1 switches the site to advisory mode automatically |
| Signatures | Plans are signed by the cloud (Ed25519 recommended; the documents say only "signed schedules") and carry site_id, plan_id, issued_at; the edge rejects unsigned, replayed or foreign-site plans | Product 1 holds the signing key in its secrets store and rotates per site keys on handover |
| Staggering | Fleet-wide plan pushes are staggered by the cloud; the edge applies at most one plan per minute | Product 1 scheduler spreads 15-minute solves across the window |
| Audit log | who, why, before, after for every write (P2-report line 460), shipped to the cloud (P2-review line 135) | Product 1 stores the log per site and uses it in savings disputes and in "plan-execution fidelity" KPIs (P1-guide line 198) |

### 3.4 Fallback rules the plan must carry

Each plan carries a fallback object:
- on_expiry: inverter_native (default) or hold_last_step (only for volatile-capable brands, bounded by max_hold_minutes).
- on_link_loss_after_s: default 300 (matches the 5-minute acceptance mark); after this the edge still executes the cached plan but flags "unattended"; after plan expiry it reverts.
- min_soc_floor_pct: an absolute floor the edge enforces regardless of plan content (defence against a wrong plan draining the reserve before an outage).
- max_export_w_floor: the export cap that applies even in fallback (net-billing sites should never export more than the plan ever allowed).
- native_settings_ref: the id of the onboarding capture of the inverter's own settings, so the edge can restore exactly what the installer configured.

### 3.5 Control tiers per site

Product 1 must model three tiers, because Product 2 unlocks them one at a time (P2-review line 135 and P2-report line 559 open question 8):
1. monitor: telemetry only; Product 1 produces recommendations and a counterfactual saving estimate.
2. advisory_writes: Mode A plans are produced but applied by a human (installer app, OEM app) and confirmed by read-back telemetry; Product 1 scores fidelity.
3. closed_loop: Mode A or B plans applied by the edge; conformance check passed on the bench for that template and firmware; installer signed the control-critical points (10 to 30, P2-report line 315).

### 3.6 Battery state and who owns it

- P2-report lines 393-402 and WP8: Product 2 measures shunt current, voltage and temperature, runs a 1-RC or 2-RC EKF at 1 Hz, resets at anchors, tracks SoH, computes a trust score and "Publish[es] SoC with its uncertainty so Product 1 can hold a margin". Acceptance: SoC within 5 points at anchors over 30 days; BMS gaps over 10 points flagged (line 521). Expected 3 to 8 percent field error between resets (line 402).
- P1-report component 3 (line 455): Product 1 "Battery state service" from pack voltage, current, temperature and BMS registers, outputs SoC with confidence, usable kWh, SoH, BMS trust score and a wear-cost curve, built from an EKF on an equivalent circuit plus BLAST-Lite. WP3 (line 565) requires a SoC service with trust score within 3 person-weeks. Open question 8 (line 551): "Does the independent SoC and SoH check belong to Product 1 or Product 2?"
- Recommendation: split by data availability. The edge owns the real-time estimate because only it has 1 Hz shunt data; the cloud owns a daily re-estimate at 1-minute resolution (needed anyway for sites fed by OEM clouds or Enerlytics, which have no shunt) and the wear-cost curve (an optimiser input, not a device property). The contract carries battery_soc_bms_pct, battery_soc_est_pct, battery_soc_sigma_pct, battery_trust_score and battery_soc_source; Product 1 uses the estimate with the lowest sigma and widens its reserve by 2 sigma. When the two estimators disagree by more than 10 points the cloud raises a site alert rather than silently picking one.

### 3.7 Genset

- Product 2 release 1 reads genset status over Modbus and does not start, stop or synchronise (P2-report line 62); open question 7 (line 558) asks whether start and stop are needed in release 1.
- Product 1's optimiser outputs "genset start request" (P1-report line 457) and values genset decisions as the largest stream at remote sites (line 285). Until Product 2 or a genset controller (Elum, ComAp, Enercon, Deye "Gen Signal On" relay) accepts commands, Product 1 delivers genset decisions as notifications (SMS or app) to the operator and scores them by whether genset_running followed within a window.
- The genset status point must therefore be required at genset sites, and Product 1 should treat a missing genset signal as "unknown", not "off".

## 4. What Product 1 must build while Product 2 does not exist

P1-report line 548 asks the owner: "When will Product 2 write to the first two brands, and may the pilot use inverter TOU slots or OEM cloud APIs before then?" Section 7 estimates the answer at 7 to 10 months after Product 2 starts. Product 1's pilots (4 weeks of 1-minute data, then 8 weeks of switchback, P1-report line 468) start earlier, so Product 1 needs its own data and delivery paths. P1-report line 453 already lists "1-minute telemetry from Product 2, Enerlytics API or OEM clouds; outage events; fuel logs", and P1-guide line 229 says "ensure Product 2 makes OnyxLeaf independent of their [Enerlytics] telemetry", which cuts both ways: Product 1 must not depend on either source alone.

### 4.1 Ingest adapters, all producing the same normalised rows as section 1.2

| Adapter | What it accepts | Cadence | Use | Build notes |
| --- | --- | --- | --- | --- |
| Edge MQTT (future Product 2) | Contract messages of section 5 over MQTT with mutual TLS | 1 min batches, events | Production path | Broker-side auth by per-device certificate; topic acl per site |
| HTTP batch ingest | POST of the same telemetry batch JSON over HTTPS with an API key per site | any | Partners' gateways (NeoTAQ, Enercon, GFE), a thin Raspberry Pi logger built before Product 2, Enerlytics push | Same validator as MQTT; idempotent by (site_id, seq) |
| CSV upload | File per site with a documented column set (section 4.2) | daily or one-off | Historical backfill for the 4-week baseline, OEM-cloud exports (iSolarCloud, FusionSolar, ShinePhone, Solarman), installer spreadsheets, fuel logs | Mapping UI: user maps columns to keys, picks units and sign; mapping saved per source; duplicates and gaps reported |
| OEM-cloud pull connectors | Solarman (Deye, Solis, Sofar via IGEN loggers), Huawei FusionSolar NetEco API, Sungrow iSolarCloud API, Growatt server API | 5 min typical (UNVERIFIED per OEM; Solarman and Growatt community APIs are unofficial) | Read-only telemetry on pilot sites before the edge exists | Rate-limited and brittle: Solarman token flaw and Deye hardcoded account were security incidents (P2-report line 465); never store OEM passwords in plain text; prefer installer-granted API keys; mark data quality = oem_cloud |
| Enerlytics API | Whatever Samir Ahmad's team exposes (no public API documented, P1-report line 203) | unknown | Option A partnership (P1-report line 232) | Treat as an HTTP batch source with their own column mapping |
| Thin logger (optional, 1 to 2 weeks of work) | A Raspberry Pi running pymodbus (BSD-3) or pysolarmanv5 (MIT) with a hand-written per-site register list, pushing 1-minute rows to the HTTP batch endpoint | 1 min | Pilot sites where no OEM cloud gives 1-minute data | This is NOT Product 2 and must not grow into it; read-only, no writes, no template engine. Licence-clean (MIT, BSD) |

### 4.2 CSV column specification (ingest v1)

One row per minute per site; header row required; extra columns ignored but logged. UTC timestamps or a declared timezone in the upload form.

ts, grid_present, grid_power_w, grid_import_wh, grid_export_wh, pv_power_w, load_power_w, battery_power_w, battery_voltage_v, battery_current_a, battery_temp_c, battery_soc_bms_pct, genset_running, genset_power_w, genset_fuel_l, inverter_mode_active, quality_flags

Rules: missing values blank, not zero; sign convention as section 1; the importer derives grid_present from grid_voltage_v if the flag is absent (threshold 180 V, UNVERIFIED as a universal rule); rows with duplicate ts keep the last; gaps longer than 5 minutes become data-gap events.

### 4.3 Digital twin: a simulated edge that speaks the contract

Purpose: let Product 1's executor, safety logic, read-back handling, fallback timers, Mode A compression and Mode B refresh be built and tested end to end before any hardware exists, and double as the counterfactual engine for savings verification (the twin can run "the inverter's own mode" on the same inputs). The documents ask for a replay simulator (P1-report WP4, PySAM-based, line 566) and an executor bench test (WP8, line 570); the twin unifies the two.

Components (Python, in the backend repository, package name suggestion ems_twin):
1. Contract endpoint: an MQTT client or an in-process adapter that receives plans, validates signature and schema, returns acks and read-backs, emits telemetry batches and events, exactly as section 5.
2. Brand behaviour profiles, data-driven (YAML or JSON per family, mirroring Product 2's template idea at P2-report lines 332-344): slot_model (count, fields, grammar), primitives supported, register persistence class and write budget, refresh interval and revert timer, write latency (Huawei dongle slow), unlock requirement, atomic groups, known quirks (Deye ring order; Growatt pair writes; Sungrow percent vs W by model).
3. Plant model at 1-minute steps: PV from a clear-sky or measured profile with cloud noise; load from measured or synthetic profiles; battery with usable capacity, charge and discharge limits, round-trip efficiency, temperature derating and a BMS SoC error model (bias plus drift, resets at full charge, per P2-report lines 362-372); inverter native mode logic (self-use with configurable TOU slots and SoC targets) so that the baseline is realistic; grid with scheduled and random outages (hazard by hour, P1-guide line 179); genset with auto-start on outage below a SoC threshold, fuel curve, min run time.
4. Fault injection: link loss, plan expiry, edge reboot, read-back mismatch, budget exhaustion, local override, template change, clock drift, sentinel values, partial minutes. Each maps to an event code in section 5.5 so Product 1's handling is testable.
5. Replay mode: feed recorded 1-minute site data (from the CSV path) instead of the synthetic plant, so that the twin reproduces what the inverter's own mode did (WP4 acceptance: daily grid import within 3 percent and battery throughput within 5 percent of measured on 90 percent of days, P1-report line 566).
6. Fleet mode: N twins with staggered clocks to test the scheduler and staggering rules.

Acceptance for the twin itself: the same contract test suite (section 5.8) passes against the twin and, later, against a real edge on the bench; any difference is a defect in one of them.

### 4.4 Mode A delivery before Product 2 (interim writers)

Three interim paths, in order of preference, all producing an "applied plan" record for fidelity scoring:
1. Installer app or OEM app by hand: Product 1 shows the slot table per brand (Deye 6 programs, Huawei 14 periods, Growatt windows) in the exact vendor layout; the installer keys it in; read-back is confirmed from telemetry (inverter_mode_active and battery behaviour). This is control tier 2 (section 3.5).
2. OEM cloud write APIs: FusionSolar and iSolarCloud expose some settings to installer accounts (UNVERIFIED for TOU tables; P2-report line 225 mentions remote inverter tests only for Enerlytics). Worth a 1-week spike per OEM; not to be relied on.
3. A per-brand thin writer on the pilot Raspberry Pi for one or two brands (Deye via sunsynk-style definitions, Sungrow via the mkaiser register list), with the P2 safety rules hard-coded for that one template, daily writes only, read-back, and a manual release button. This is a stopgap that borrows Product 2's rules without its template engine; it must be retired when Product 2 WP7 lands. Decision for the owner (open question 2).

### 4.5 What Product 1 should not build

- No register maps, transport drivers, identification engine or installer onboarding tool: these are Product 2 WP3 to WP6 and WP10. Building them twice splits the team and invites licence contamination from the GPL and AGPL repos (P2-report lines 281-294).
- No "works with all inverters" marketing on the Product 1 side; coverage is a Product 2 list.

## 5. Proposed edge-to-cloud contract, version ems-edge/1.0

### 5.1 Transport and envelope

- Primary transport: MQTT 5 over TLS 1.2 or better with mutual authentication (per-edge client certificate), outbound from the edge only (P2-report lines 447, 473). QoS 1, persistent sessions, store-and-forward on the edge for at least 7 days of 1-minute rows (about 10,000 rows times roughly 40 points, under 10 MB).
- Secondary transport: HTTPS POST of the same JSON documents to /api/v1/edge/{message_type}, for partners' gateways and for the CSV and batch path. Same schemas, same validator.
- Topic layout: ems/v1/{site_id}/{edge_id}/telemetry, .../events, .../capabilities, .../ack, .../heartbeat (edge to cloud); ems/v1/{site_id}/{edge_id}/plan, .../command (cloud to edge). The edge subscribes only to its own two topics.
- Common envelope on every message: contract ("ems-edge/1.0"), msg_type, msg_id (UUID), site_id, edge_id, sent_at (UTC ISO 8601), seq (monotonic per edge per type), and, for cloud-to-edge messages, sig (Ed25519 signature over the canonical JSON of the body, base64) and key_id.
- Versioning: semantic. 1.x changes add optional fields only; a 2.0 changes shapes. The edge reports the contract versions it accepts in capabilities; the cloud sends the highest common version.
- Time: all timestamps UTC; the site timezone lives in site metadata (Asia/Karachi for all Pakistani sites; Asia/Riyadh later). Mode A slot times are local wall-clock because inverters hold local time, and the plan states the timezone explicitly.

### 5.2 telemetry (edge to cloud)

Columnar to keep batches small. One batch per minute in normal operation; up to 1,440 rows per batch when catching up after link loss.

```json
{
  "contract": "ems-edge/1.0",
  "msg_type": "telemetry",
  "msg_id": "9d0f4c1e-5f1f-4b56-9b7c-3f5f2b1c0a11",
  "site_id": "pk-lhr-0007",
  "edge_id": "edge-00a1b2",
  "sent_at": "2026-11-06T13:05:02Z",
  "seq": 48213,
  "interval_s": 60,
  "ts_start": "2026-11-06T13:03:00Z",
  "rows": 2,
  "columns": ["grid_present", "grid_present_min", "grid_power_w", "pv_power_w", "load_power_w", "battery_power_w", "battery_voltage_v", "battery_current_a", "battery_temp_c", "battery_soc_bms_pct", "battery_soc_est_pct", "battery_soc_sigma_pct", "battery_trust_score", "inverter_mode_active", "export_limit_active_w", "genset_running", "genset_power_w", "quality_flags"],
  "data": [
    [true, true, 1850, 12400, 15900, -1650, 52.1, -31.4, 31.0, 78, 71.5, 2.0, 0.62, "tou_native", 0, false, 0, 0],
    [true, false, 2100, 12100, 15800, -1500, 52.0, -28.8, 31.1, 78, 71.9, 2.0, 0.62, "tou_native", 0, false, 0, 2]
  ],
  "aggregation": {"grid_present": "last", "grid_present_min": "min", "powers": "mean", "soc": "last"},
  "sample_period_s": 5
}
```

Rules: columns may vary per edge but must be a subset of section 1.2 keys; required keys present on every batch; nulls for missing samples; quality_flags per section 1.2; the cloud acknowledges by seq so the edge can purge its store.

### 5.3 capabilities (edge to cloud, on boot, on template change, and at least daily)

```json
{
  "contract": "ems-edge/1.0",
  "msg_type": "capabilities",
  "msg_id": "...",
  "site_id": "pk-lhr-0007",
  "edge_id": "edge-00a1b2",
  "sent_at": "2026-11-06T00:00:10Z",
  "seq": 17,
  "edge": {"sw_version": "p2-0.4.2", "hw": "cm5-carrier-a", "contracts_accepted": ["ems-edge/1.0"], "clock_synced": true, "control_tier": "closed_loop", "control_enabled_by": "installer:ali.raza", "control_enabled_at": "2026-10-20T09:12:00Z"},
  "devices": [
    {
      "device_id": "inv1",
      "role": "hybrid_inverter",
      "family": "deye_lv_3ph",
      "template_id": "deye_sg04lp3@fw1098+",
      "firmware": "1098-2021",
      "rated_power_w": 12000,
      "points": ["grid_present", "grid_power_w", "pv_power_w", "load_power_w", "battery_power_w", "battery_voltage_v", "battery_current_a", "battery_soc_bms_pct", "inverter_mode_active", "genset_power_w"],
      "primitives": {
        "battery_mode": {"values": ["self_use", "hold", "charge", "native"], "persistence": "eeprom", "writes_per_day_max": 4},
        "battery_power_w": {"min": -12000, "max": 12000, "step": 10, "persistence": "eeprom", "writes_per_day_max": 4},
        "soc_floor_pct": {"min": 10, "max": 100, "step": 1, "persistence": "eeprom", "writes_per_day_max": 4},
        "soc_target_pct": {"min": 10, "max": 100, "step": 1, "persistence": "eeprom", "writes_per_day_max": 4},
        "grid_charge_allowed": {"persistence": "eeprom", "writes_per_day_max": 4},
        "gen_charge_allowed": {"persistence": "eeprom", "writes_per_day_max": 4},
        "export_limit_w": {"min": 0, "max": 12000, "step": 10, "persistence": "eeprom", "writes_per_day_max": 4},
        "release": {}
      },
      "slot_model": {"mode_a_supported": true, "max_slots": 6, "ring": true, "fields": ["start_local", "battery_power_w", "soc_target_pct", "charge_source"], "time_resolution_min": 1, "min_slot_minutes": 30, "weekday_mask": false, "table_writes_per_day_max": 4},
      "mode_b": {"supported": false, "reason": "no volatile setpoint register in template"},
      "native_settings_ref": "capture-2026-10-20T09:00:00Z"
    },
    {
      "device_id": "bat1",
      "role": "battery",
      "family": "pylontech_can",
      "template_id": "pylon_can_v1.3",
      "capacity_wh_nameplate": 30720,
      "points": ["battery_soc_bms_pct", "battery_soh_pct", "battery_charge_limit_a", "battery_discharge_limit_a", "battery_cell_v_min", "battery_cell_v_max"],
      "shunt_fitted": true,
      "estimator": {"type": "ekf_2rc", "version": "0.3", "anchors_last_30d": 11}
    },
    {
      "device_id": "gen1",
      "role": "genset",
      "family": "deepsea_modbus",
      "template_id": "UNVERIFIED",
      "points": ["genset_running", "genset_power_w", "genset_fuel_l"],
      "primitives": {},
      "control": "read_only"
    },
    {
      "device_id": "met1",
      "role": "grid_meter",
      "family": "eastron_sdm630",
      "points": ["grid_power_w", "grid_import_wh", "grid_export_wh", "grid_voltage_v", "grid_frequency_hz"]
    }
  ]
}
```

Rules: Product 1 stores every capabilities message (history matters for savings disputes); a change in template_id, firmware, control_tier or primitives triggers re-validation of the active plan; control_tier is edge-asserted, never set by the cloud.

### 5.4 plan (cloud to edge), signed

```json
{
  "contract": "ems-edge/1.0",
  "msg_type": "plan",
  "msg_id": "...",
  "site_id": "pk-lhr-0007",
  "edge_id": "edge-00a1b2",
  "sent_at": "2026-11-06T13:00:05Z",
  "seq": 9021,
  "key_id": "site-pk-lhr-0007-k2",
  "sig": "base64-ed25519-over-body",
  "body": {
    "plan_id": "pk-lhr-0007:2026-11-06T13:00Z:v1",
    "supersedes": "pk-lhr-0007:2026-11-06T12:45Z:v1",
    "issued_at": "2026-11-06T13:00:04Z",
    "valid_from": "2026-11-06T13:00:00Z",
    "valid_until": "2026-11-08T13:00:00Z",
    "timezone": "Asia/Karachi",
    "capabilities_seq": 17,
    "mode": "A",
    "targets": [{"device_id": "inv1"}],
    "fallback": {"on_expiry": "inverter_native", "on_link_loss_after_s": 300, "min_soc_floor_pct": 25, "max_export_w_floor": 0, "native_settings_ref": "capture-2026-10-20T09:00:00Z"},
    "mode_a": {
      "days": [
        {"date_local": "2026-11-06", "slots": [
          {"start_local": "00:00", "battery_mode": "hold", "soc_floor_pct": 40, "grid_charge_allowed": false, "export_limit_w": 0},
          {"start_local": "05:30", "battery_mode": "charge", "soc_target_pct": 95, "battery_power_w": -6000, "grid_charge_allowed": false, "export_limit_w": 0},
          {"start_local": "10:00", "battery_mode": "self_use", "soc_floor_pct": 60, "grid_charge_allowed": false, "export_limit_w": 0},
          {"start_local": "17:00", "battery_mode": "hold", "soc_floor_pct": 85, "grid_charge_allowed": false, "export_limit_w": 0},
          {"start_local": "18:30", "battery_mode": "self_use", "soc_floor_pct": 20, "grid_charge_allowed": false, "export_limit_w": 0},
          {"start_local": "23:00", "battery_mode": "hold", "soc_floor_pct": 20, "grid_charge_allowed": true, "battery_power_w": -3000, "soc_target_pct": 40, "export_limit_w": 0}
        ]},
        {"date_local": "2026-11-07", "slots": ["... same shape ..."]}
      ],
      "apply_policy": {"write_if_changed_only": true, "max_table_writes_per_day": 4, "apply_window_local": "00:05-23:50"}
    },
    "mode_b": null,
    "notes": {"solver": "highs-1.9", "objective_pkr": 41250.5, "outage_risk_next_6h": 0.31, "reason": "evening peak 17:00-21:00 TOU, outage hazard 0.31"}
  }
}
```

Mode B variant: "mode": "B", "mode_b": {"step_s": 900, "t0": "2026-11-06T13:00:00Z", "steps": [{"battery_power_w": -4000, "soc_floor_pct": 30, "export_limit_w": 0, "grid_charge_allowed": false}, ...192 entries...], "refresh": {"heartbeat_s": 60, "revert_after_s": 1800, "residual_band_w": 300}}. A Mode B plan may also carry mode_a as the fallback schedule for slot-only devices; the edge picks per device from slot_model and mode_b.supported.

Rules: valid_until at most 48 h after issued_at; a plan whose capabilities_seq is older than the edge's current capabilities is rejected with CAPS_STALE; the edge applies at most one plan per minute and only the newest plan_id; supersedes lets the edge discard older queued plans after link loss.

### 5.5 events (edge to cloud)

```json
{
  "contract": "ems-edge/1.0",
  "msg_type": "events",
  "msg_id": "...",
  "site_id": "pk-lhr-0007",
  "edge_id": "edge-00a1b2",
  "sent_at": "2026-11-06T13:07:41Z",
  "seq": 5532,
  "events": [
    {"ts": "2026-11-06T13:07:12Z", "type": "outage_start", "source": "met1", "detail": {"grid_voltage_v": 0, "last_good_ts": "2026-11-06T13:07:07Z"}},
    {"ts": "2026-11-06T13:07:40Z", "type": "genset_start", "source": "gen1", "detail": {"trigger": "unknown", "soc_at_start_pct": 61}},
    {"ts": "2026-11-06T13:05:00Z", "type": "plan_applied", "source": "inv1", "detail": {"plan_id": "pk-lhr-0007:2026-11-06T13:00Z:v1", "writes": 2}},
    {"ts": "2026-11-06T12:50:11Z", "type": "readback_mismatch", "source": "inv1", "detail": {"primitive": "soc_floor_pct", "requested": 40, "readback": 35, "action": "released"}},
    {"ts": "2026-11-06T12:30:00Z", "type": "override_local", "source": "inv1", "detail": {"register": "prog3_capacity", "before": 60, "after": 50}}
  ]
}
```

Event types (v1.0): outage_start, outage_end, genset_start, genset_stop, plan_received, plan_rejected, plan_applied, plan_expired, fallback_engaged, fallback_cleared, readback_mismatch, write_failed, budget_exhausted, override_local, template_change_detected, firmware_change_detected, control_disabled, control_enabled, edge_reboot, link_lost (sent after reconnect with the gap), clock_unsynced, battery_trust_low (trust under 0.5 or gap over 10 points), physics_check_failed, data_gap, installer_session_start, installer_session_end.

### 5.6 ack and read-back (edge to cloud, after each plan or command)

```json
{
  "contract": "ems-edge/1.0",
  "msg_type": "ack",
  "msg_id": "...",
  "site_id": "pk-lhr-0007",
  "edge_id": "edge-00a1b2",
  "sent_at": "2026-11-06T13:05:03Z",
  "seq": 2210,
  "plan_id": "pk-lhr-0007:2026-11-06T13:00Z:v1",
  "status": "partial",
  "received_at": "2026-11-06T13:00:09Z",
  "applied_at": "2026-11-06T13:05:00Z",
  "per_device": [
    {"device_id": "inv1", "status": "partial", "writes_used_today": {"tou_table": 2},
     "applied": [
       {"primitive": "slot_table", "requested_hash": "sha256:ab12", "written": {"prog": [["00:00", 0, 40, 0], ["05:30", 6000, 95, 0], ["10:00", 0, 60, 0], ["17:00", 0, 85, 0], ["18:30", 0, 20, 0], ["23:00", 3000, 40, 1]]}, "readback_ok": true},
       {"primitive": "export_limit_w", "requested": 0, "written": 0, "readback": 0, "readback_ok": true}
     ],
     "rejected": [
       {"primitive": "gen_charge_allowed", "reason": "NOT_SUPPORTED", "detail": "template has no gen input on this model"}
     ]}
  ]
}
```

Reason codes (v1.0): SIG_INVALID, CAPS_STALE, EXPIRED, SCHEMA, OFF_WHITELIST, NOT_SUPPORTED, RANGE_CLAMPED, STEP_ROUNDED, BUDGET_EXHAUSTED, UNLOCK_FAILED, DEVICE_OFFLINE, READBACK_MISMATCH, ATOMIC_GROUP_FAILED, CONTROL_DISABLED, OVERRIDE_ACTIVE, RATE_LIMITED, SLOT_GRAMMAR (e.g. Deye ring order violated), TIMEZONE_MISMATCH.

### 5.7 command (cloud to edge, out of band, signed) and heartbeat

command types: release (all primitives to native now), hold (emergency: set soc_floor to current SoC, zero export, no discharge, bounded by max_minutes), request_capabilities, request_debug_window (1 s telemetry for up to 15 minutes), request_native_capture (re-read and store the inverter's own settings), set_control_tier is NOT a command (edge-asserted only).

heartbeat (edge to cloud every 60 s; also serves as the liveness source for the 5-minute fallback mark): {"msg_type": "heartbeat", "edge_id": ..., "uptime_s": ..., "active_plan_id": ..., "plan_valid_until": ..., "control_state": "executing|fallback|released|disabled", "queue_depth": ..., "clock_offset_s": ...}.

### 5.8 Contract test suite (shared by the twin and the real edge)

1. Schema validation of every message type with JSON Schema 2020-12 files stored in the backend repository under contracts/ems-edge/1.0/.
2. Signature round trip, replay rejection (same plan_id twice), foreign site rejection.
3. Plan lifecycle: receive, ack, apply at the right minute, read-back, expiry fallback, supersede.
4. Mode A grammar per brand profile: Deye ring, Huawei 14-period cap and no overnight span, Growatt 3-window cap and pair write, Sungrow emulation, Voltronic emulation.
5. Budgets: 5th table write in a day is rejected with BUDGET_EXHAUSTED and the previous table remains.
6. Link loss: 10 minutes silent, cached plan continues, heartbeat gap reported, store-and-forward delivers all rows in order.
7. Overrides and template change disable control and raise events.
8. Telemetry completeness and quality flags over a simulated 30 days (99.5 percent at the source, 100 percent of minutes present or flagged).

## 6. The edge box, offline behaviour, cloud sync, onboarding, brands

### 6.1 Edge box (what Product 1 can assume about the runtime)

- Hardware (P2-report lines 29-31, 427-434): Raspberry Pi CM5 or CM4 class Linux module on an industrial carrier; 2 isolated RS485 ports, 1 isolated CAN port, shunt ADC or VE.Direct input, pack voltage and NTC inputs; 12 to 48 V DC supply, RTC, supercap; industrial eMMC, read-only root, A/B partitions; Ethernet, Wi-Fi and a PTA-approved LTE Cat 1 or Cat 4 module; secure element for keys; an MCU watchdog "that can cut the write path". Prototype US$120 to 200 (line 410); custom carrier US$90 to 150 BOM at 1,000 units (line 425). Bench rig: Pi 5 with the Waveshare isolated RS485 CAN HAT (line 421).
- Consequence: the edge can run a small Python or Go service, hold days of data, and keep time through outages. It cannot be assumed to have more than a few hundred MB of RAM for Product 1 code, and the documents put no optimiser on the edge (P1-guide line 171: "keeping the edge thin keeps Product 2's surface small"). Product 1's only edge-resident logic is the executor's 1-minute rule layer, which is specified by Product 1 and implemented in Product 2 (P1-report line 457).
- Software containerised so partner gateways can host it later (P2-report line 31). Product 1 should therefore not assume OnyxLeaf hardware specifically, only the contract.

### 6.2 Offline behaviour

- Local-first: "The controller must work indefinitely offline" (P2-review line 145). The cached 48-hour plan is executed to its end, then the fallback applies (P1-report line 446).
- Store-and-forward of telemetry and events; the cloud acknowledges by seq (section 5.2). Pakistani connectivity gaps are expected; P2-review line 145 names "local buffering for Pakistan's connectivity gaps".
- Clock: RTC on the box; NTP when online; clock_offset_s reported; Mode A slots are wall-clock so a drifted clock shifts the whole day, which is why the edge must refuse a plan if clock_synced is false and the drift exceeds 2 minutes (engineering default).
- Fallback is to the inverter's own mode, captured at onboarding (native_settings_ref). Product 1 keeps a copy of that capture because it is also the counterfactual baseline (P1-guide line 184: "its exact slot settings, captured at onboarding").

### 6.3 Cloud sync and security

- Outbound-only mutual TLS MQTT, store and forward (P2-report line 447); no inbound ports, per-device keys, on-demand support tunnels (line 473); secure boot and signed A/B updates, SBOM per release (line 474); staggered fleet commands and two-person approval for fleet-wide changes (line 475); installers may disable OEM dongles where OnyxLeaf takes control (line 476).
- Context: SUN:DOWN found 46 vulnerabilities in Sungrow, Growatt and SMA (March 2025); Solarman token flaw; Deye Cloud hardcoded account (lines 464-465). EU CRA reporting from 11 Sep 2026 (line 467). SunSpec cyber requirements: signed updates, TLS 1.2+, login lockout, 90-day logs (line 468).
- Product 1 obligations: a broker with per-site ACLs; signing keys per site in a secrets store with rotation; an audit log retained at least 90 days (SunSpec figure) and preferably for the life of the savings contract; a fleet command path with two-person approval in the admin UI (later phase, consistent with the owner's "admin dashboard later" instruction).

### 6.4 Installer onboarding

- Flow (P2-report lines 22-27, 315, 502-510, WP10 line 579): transport discovery, identification, template load, read-only validation for a period, capture of the vendor dongle's polls and app writes, installer confirmation of 10 to 30 control-critical points, then whitelisted write tests with power cycle to learn persistence, golden dump saved. Targets: listed device in 15 minutes, unlisted device's critical points in 60 minutes (line 523).
- Pilot practice: each site runs read-only for two weeks, then Product 2 commands modes and export limits (line 529). Terms on liability, data rights and write scope are signed first (line 527).
- What Product 1 needs from onboarding: the capabilities message, the native settings capture (for baseline and fallback), site metadata (tariff category, DISCO, feeder, PV kWp, battery kWh and chemistry, genset kVA and fuel curve, critical load), and the installer's identity for the audit trail. These form Product 1's site onboarding form; the edge-independent fields must be fillable by hand for sites without an edge.

### 6.5 First brands and the Pakistani brand mix

- P2-report first brands (lines 76-88): Sungrow SH hybrid and SG string (external EMS mode); Growatt SPH, SPA, MIN/MOD TL-XH, then SPF; Deye platform (Sunsynk and rebadges); Huawei SUN2000 with LUNA2000 (most frequent brand on named Pakistani C&I sites); Voltronic PI30-family off-grid units; meters Eastron SDM630 and Chint DTSU666; batteries Pylontech-style CAN and Pylon RS485, then JK and Seplos. Second wave: GoodWe, Solis, Sofar, SolaX, SMA; fingerprint Inverex, Fronus and Knox on site.
- P2-review build order (line 143) differs: 1 Voltronic, 2 Deye, 3 Growatt, 4 Sungrow, 5 Solis via Solarman. It is residential-weighted (Voltronic rebadges are "the volume segment"), while the P2-report order is C&I-weighted. For Product 1's target (C&I 20 kW to 500 kW with a battery on a TOU or net-billing tariff, P2-report line 70; P1-report line 71) the relevant first two write targets are Deye three-phase and either Huawei or Sungrow. Voltronic off-grid units have no export and no slots, so they are Mode A by emulation only and low value for TOU; they matter for genset-heavy remote sites.
- Brand mix evidence (P2-report lines 165-182): no brand-level market share data; distributors Premier Energy (GoodWe, Huawei, Infini, Tranergy) and Nizam (GoodWe, SAJ, TBB); named C&I sites run Huawei, SMA and Sungrow; GIKI runs 20 multi-brand inverters; Fronus shows INVT and SofarSolar OEM certificates and a SolaX deal; Inverex NitroX may be Solis-based (unverified). WP1 device census of 30+ sites is Product 2's first work package (line 570); Product 1's WP2 site audit (P1-report line 564) should share the same census form.
- Retail LFP prices Oct 2026 (Rs/kWh): Sunwoda 44,800; Inverex 55,900; Dyness 56,000; Pylontech 67,200 (P2-report line 173), useful for the wear-cost curve.

### 6.6 Product 2 work packages and when writes could exist

P2-report lines 564-583, 12 work packages, about 77 person-weeks, "Effort figures are rough estimates":

| WP | Name | Depends on | Effort (person-weeks) | Relevance to Product 1 |
| --- | --- | --- | --- | --- |
| WP1 | Device census | none | 4 | Shared census form with P1 WP2 |
| WP2 | Bench and captures | WP1 | 6 | Golden dumps also seed the twin's brand profiles |
| WP3 | Schema, model, test harness | none | 4 | Defines the common data model keys; section 1.2 should be adopted here |
| WP4 | Transport and scheduler | WP3 | 6 | - |
| WP5 | Template library v1 | WP2, WP4 | 8 | First families readable |
| WP6 | Identification engine | WP5 | 4 | - |
| WP7 | Safety and command layer | WP5 | 6 | First bench writes from abstract commands; acceptance "Zero off-whitelist writes; fallback within 5 minutes" |
| WP8 | Battery trust module | WP4 | 8 | Edge SoC estimate and trust score |
| WP9 | Edge hardware and security | none | 8 | 5 pilot units, mutual TLS, signed OTA |
| WP10 | Installer tool | WP6, WP7 | 8 | Capabilities and native capture originate here |
| WP11 | Local API and sync | WP7, WP8, WP9 | 5 | "Product 1 reads SoC and sends a schedule end to end"; the hand-off point |
| WP12 | Field pilots | WP10, WP11 | 10 | 3 sites pass all marks over 30 days |

Order stated (line 583): WP1, WP3 and WP9 start together; WP2 and WP4 follow; WP5 gates WP6, WP7 and WP8; WP10 and WP11 next; WP12 last.

Derived timing (my arithmetic, assuming each WP is staffed by one person and dependent WPs run strictly after their inputs; calendar weeks from Product 2's start):
- Bench writes to the first families (end of WP7): WP1 4 + WP2 6 + WP5 8 + WP7 6 = week 24. With WP3 and WP4 in parallel (4 + 6 = week 10, before WP5 needs them) they are off the critical path.
- End-to-end schedule from Product 1 to a bench inverter (end of WP11): week 29 (WP8 and WP9 finish earlier: WP8 at week 10 + 8 = 18; WP9 at week 8).
- Field-proven writes at 3 pilot sites (end of WP12): WP10 ends week 32 (after WP6 at 28 and WP7 at 24), then WP12 10 weeks = week 42, i.e. about 10 months.
- With two engineers on the critical path (WP2 and WP5 halved), bench writes at about week 17 and WP11 at about week 22; this is a best case and UNVERIFIED by any staffing plan (P2-report line 562 asks what team and budget exist).
- Answer to P1-report open question 5 (line 548): the first two brands can accept bench writes at roughly month 6 and field writes at roughly month 10 after Product 2 starts; before that, Product 1 pilots use inverter TOU slots applied by installers or OEM apps (section 4.4) and read telemetry from OEM clouds, Enerlytics or a thin logger (section 4.1).

## 7. Open questions for the owner

1. Who owns the independent SoC and SoH estimator: Product 2 (edge EKF at 1 Hz with a shunt, WP8) or Product 1 (cloud EKF from 1-minute data, WP3)? This note recommends both with a source tag (section 3.6); the owner must settle which number the savings contract uses.
2. May Product 1 build a thin per-brand writer for the pilots (Deye and Sungrow, daily Mode A table writes only, read-back, manual release) before Product 2 WP7 exists, or must all writes wait for Product 2? Building it accelerates WP10 field switchback (P1-report line 572) by months but creates a second code path to retire.
3. Genset: is "genset start request" a real command in release 1 (requires Product 2 to whitelist a relay such as Deye "Gen Signal On" register 140, or a ComAp or Deep Sea controller write) or a notification to the operator? P2-report line 62 says read-only; P1-report line 457 assumes a start request.
4. Which two brands are first for writes? P2-report says Sungrow and Growatt were "named in the meeting" (lines 80-81); P2-review says Voltronic then Deye (line 143); Product 1's C&I target points at Deye three-phase plus Huawei or Sungrow. The census (WP1) should decide, but the bench purchase list (P2-report lines 490-498) needs the owner's call now.
5. Control tiers and liability: which tier can a pilot site enter on day one, and who signs the write scope (installer, owner, OnyxLeaf)? P2-report line 527 requires signed terms before pilots; the Product 1 pilot plan (P1-report line 490) assumes full control in phase 3.
6. Transport decision: MQTT over mutual TLS (as the P2 documents say) or HTTPS batch only for the first year? MQTT needs a broker and certificate management in the Product 1 backend from the start; HTTPS batch is simpler and enough for partner gateways and the thin logger.
7. Is the Enerlytics option A (engine partner via API, P1-report line 232) still live? If yes, their API shape should drive the HTTP batch ingest spec of section 4.1; if no, the CSV and OEM-cloud paths carry the pilots.
8. OEM-cloud connectors: which OEM accounts (FusionSolar, iSolarCloud, Solarman, ShinePhone) will the pilot installers grant, and is pulling data through installer accounts acceptable under their terms? This decides whether the connectors are worth building before the edge exists.
9. Write budgets: accept the engineering defaults (4 Mode A table writes a day, 100 EEPROM writes per register per day hard cap, 30-minute revert timer, 5-minute link-loss fallback mark), or set different ones per brand after the bench captures?
10. Data retention and audit: how long must per-write audit logs and 1-minute telemetry be kept for a savings dispute (this note assumes 90 days minimum and the life of the contract preferred)?
11. Should the contract be published to partners (NeoTAQ, Enercon, GFE gateways could feed the HTTP batch path) or kept private? Publishing creates a channel; keeping it private protects the edge product.
12. Time zone and daylight rules: Pakistan has no DST; Saudi Arabia neither. Confirm no site will sit in a DST zone before freezing the wall-clock slot format.

## 8. Verification log and UNVERIFIED items

Verified on 6 Nov 2026 by reading cloned repository files (commit and date from git log -1 of each shallow clone):
- kellerza/sunsynk f1ecbc28 (2026-10-24), Apache-2.0: Deye program registers 250-279 and ring order, options, work mode and sell power, three-phase addresses, telemetry registers (single_phase.py, three_phase_common.py, definitions/__init__.py).
- kbialek/deye-inverter-mqtt 7743bd29 (2026-11-05), Apache-2.0: TOU topics and register table, buffered write workflow, work mode values, damage warning (README.md lines 289-399; docs/metric_group_deye_hybrid_timeofuse.md).
- davidrapan/ha-solarman ac33d9a1 (2026-11-04), MIT: deye_hybrid.yaml configurable points, Work Mode register 0xF4, Energy Pattern 0xF3, Time of Use 0xF8, program entries 1762-2180.
- wlcrs/huawei-solar-lib b383fd47 (2026-11-04), AGPL-3.0 (facts only, no code reuse): registers.py storage control block 47004-47299 and active power control 47415-47422; periods.py period counts 14, 10, 14 and period encodings; register_values.py enums.
- mkaiser/Sungrow-SHx-Inverter-Modbus-Home-Assistant 23479a9a (2026-10-30), MIT: EMS mode 13050 values, command 13051 values, power 13052 note, SoC and feed-in registers, status bits.
- wills106/homeassistant-solax-modbus 0056f9fb (2026-11-04), Apache-2.0: plugin_growatt.py windows, packed time encoding, export limit registers, 30215 vs 30216 persistence comment.
- jblance/mpp-solar eafdd432 (2026-04-12), MIT: pi30.py POP, PCP, MCHGC, MUCHGC, PBCV, PBDV, PSAVE definitions.
- Fetched pages: sunspec/models model_704.json (reversion points) and model_802.json (CtrlHb, SoC, limits); wills106 docs/automation-examples.md (EEPROM 100,000 cycles, 27 modifications a day); Pho3niX90/solis_modbus discussion 495 (failsafe 1 to 1440 min, default 30; power 0 to 240,000 W step 100; firmware flag 34502 = 0xAA55).

UNVERIFIED (to be resolved on the bench or by the owner):
- Deye sell bit position (0x40 per sunsynk vs 32 per kbialek) and whether program registers are EEPROM-class.
- Whether Deye exposes any volatile battery setpoint usable for Mode B.
- Whether Sungrow SH firmware after 2024 exposes a native time-period table; whether 13050-13052 are volatile or EEPROM.
- Huawei register persistence and the exact installer-login procedure over Modbus.
- Voltronic: whether POP and PCP changes persist without PSAVE.
- Solis dispatch block register numbers (44100 block) and the "volatile by design" claim.
- Growatt TL-XH active power rate register; SPH window persistence class.
- OEM-cloud API availability, cadence and terms for FusionSolar, iSolarCloud, Solarman and ShinePhone.
- Genset controller families on Pakistani sites (Deep Sea, ComAp, others) and their Modbus maps.
- All timing in section 6.6, which rests on the report's rough person-week estimates and a one-person-per-WP assumption.
- The 180 V threshold for deriving grid_present from voltage, the 2-minute clock-drift refusal, and all other engineering defaults named as such.

End of note.
