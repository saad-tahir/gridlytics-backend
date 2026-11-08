# Market-lens review of EMS_BUILD_PLAN.md

Adversarial review, 6 November 2026. Lens: the Pakistani market, tariff and regulatory facts, the economics of the value streams, the pilot design and kill criteria, and what a C&I energy manager, an installer or a towerco would need and pay for. Scope: Part 0, Part 1, Part 6, Part 8, Part 9.6, Part 12 (Stages 1 and 6), Part 15.

Sources checked against: `plan-work/research/R9-pakistan-tariffs.md` (R9), `Perfect-EMS_TOU_Dispatch_Deep_Review.md` (the guideline), `Product 1 - TOU-Aware Energy Management and Dispatch.md` (the report). Line numbers refer to `EMS_BUILD_PLAN.md` as re-assembled with the engine and build reviews' fixes (1,344 lines, read in full for the parts in scope before any finding below was written). Two facts neither source settles were checked on the web on 6 November 2026 and are cited where used: the load-shedding exemption of industrial feeders and the planned shift of shedding from feeders to transformers.

The file is appended part by part; the ranked findings list (B), the three credibility changes (C), the verdict (D) and the ranked index (E) follow the running notes (A).

---

## A. Running notes by part

### A.0 What the re-assembly already fixed (not findings)

The current plan answers these points that the first read of the 1,285-line version had flagged: the ICP priced by monthly totals per period with the three cases (4.3, 6.1, TAR-03); the reserve as an expected-value row priced at p_start times the shortfall cost, with the Rs 106 figure now stated as p times 142 minus 35.68 (1.1, 4.3); the terminal value net of wear; the measured day as the optimiser arm with the baselines replayed and fidelity as a gate, not a multiplier (8.2, 8.4); a tuned static table at every site (8.1, VER-12); 7-day blocks in tier 2 with no SoC reset and priced carry-over (8.2, 15.3); four hand-worked bills instead of engine-made fixtures (6.3, TAR-06); the export credit grossed up where the bill nets it before taxes (6.4); `outage_load_mode` for who carries the plant in an outage (4.1, 4.3); O12's real month as the non-circular check. What follows is what still refutes.

### A.1 Part 0 and Part 1

Checks out: the four stream figures (lines 65 to 70) reproduce the report's worked example (100 x 35.68 - 111.1 x 22.83 = 1,031.6; 3,568 - 111.1 x 8.13 = 2,665; 100 x 142.2 - 2,536.7 = 11,683); the spread 12.85 and the net arbitrage 4.8 to 10.3 (35.68 - 22.83/0.9 = 10.31; 43.82 - 35.15/0.9 = 4.76); the gross-up 5.7 to 12.3 is 1.015 x 1.18 times those (but see B-1 on whether the buyer bears it); D15 follows the report's segment 1 and the departure from the guideline's telecom site is justified by the report's own evidence.

Still refuted: the all-in prices and savings include taxes the first buyer recovers (B-1); the genset stream assumes the product decides whether the genset runs (B-3); "on a feeder with load shedding" is the wrong qualifier for an industrial buyer and the feeder key expires in June 2027 (B-4); the named buyers are MWh-scale integrated-system owners, not the B-2 site the plan builds for (B-10); no zero-export regime (B-11); diesel-only fuel (B-12); the fee band does not scale with battery size (B-15); line 68 still quotes the press export figure (B-21).

### A.2 Part 6

Checks out against R9: every rate, every fixed charge and basis, the 30-minute MDI, the two TOU calendars, the QTA and FCA mechanics and exemptions, the ICP rule and its exemptions, net billing at Rs 8.13 with quarterly revision, SRO 547(I)/2026 for legacy sites, daily OGRA pricing, the HOMER curve arithmetic (0.405 and 0.306 L/kWh), the 4-day proration rule. The thirteen steps of 6.3 follow R9 section 13 in order.

Still refuted: the marginal price includes GST and the section 235 slab (B-1); the metering factor is fitted and tested on the same bills (B-6); only SRO 279(I)/2026 ships although O3 asks for 12 bills across three schedules (B-8); nobody keeps the data current into 2027 (B-9); one binding per site (B-16); legacy netting shown as verified (B-19); Tariff F dropped (B-20).

### A.3 Part 8 and Part 7.6

Checks out: the three layers of proof, the tuned baseline, 1-minute pricing, the perfect-foresight bound, the Preliminary rule tied to billed FCAs, every input kept with its belief time.

Still refuted: the block switchback cannot attribute the MDI and ICP lines, which are monthly (B-2); the stratification by scheduled-outage days cannot be done before the fortnight's schedule exists (B-17); the metering factor (B-6); the taxes in the ledger (B-1).

### A.4 Part 9.6

The three demo sites carry a tariff error (a net-billing regime that expires), a mall with no genset and a 15 kW critical load, an industrial feeder with evening shedding, a read-back alert in a tier without read-back, and no K-Electric site (B-13, B-4).

### A.5 Part 12, Stages 1 and 6

Checks out: the four hand-worked bills as the Stage 1 exit; TAR-12's provenance per number; VER-14's real-data gates; the pre-registered gates in VER-10.

Still refuted: TAR-06's "calibrated rebuild" (B-6); TAR-12's single schedule version (B-8); VER-08's stratification (B-17); VER-10 drops the owner's review of the first report (B-18); the MDI and ICP lines in UI-12's ledger have no switchback source (B-2).

### A.6 Part 15

Checks out: the pre-registered gates now include the genset 15 percent, the 90 percent reserve coverage and the agreement tolerance; the risk table matches the report's.

Still refuted: O2, O3, O8 and O9 are timed in the wrong order and the pilot cannot start when the software is ready (B-5); two kill rules and one unmeasurable gate (B-7); no task keeps the tariff data current (B-9); no SMS or WhatsApp channel for the only lever tier 2 has (B-14); pricing (B-15); the dropped WP9 and WP11 steps (B-18).

---

## B. Ranked findings

Severity: high = a wrong bill, a wrong saving claim, a wrong market bet, or a pilot that cannot prove the claim; medium = a real defect with a workaround; low = wording or precision. Each finding gives the plan's location, its claim, the defect, the evidence, and a replacement short enough to paste.

### B-1. High. Taxes the first buyer recovers are priced into every decision and claimed as savings

- **Location**: Part 1.1 line 65 ("Rs 5.7 to 12.3 after duty and GST, which the engine prices"); Part 4.1 line 299 (prices "all-in ... the adders, duty and tax share that scale with energy"); Part 6.4 line 550 ("including the section 235 slab rate where the site sits in the slab"; "c_exp = NAEPP x (1 + ED)(1 + GST)"); Part 8.4 ("state the GST treatment"); Part 15.3 and 15.4 (the fee as 3x, 1.5x and one third of "verified savings").
- **Claim**: the optimiser and the ledger price what the bill shows, all-in, and the fee is a share of that figure.
- **Defect**: the first buyer is a B-2 or B-3 manufacturer, a company with an STRN on the active taxpayer list. It recovers the 18 percent GST on the electricity bill as input tax (Sales Tax Act 1990 section 7; the FBR's electricity input adjustment requires the STRN on the bill, which is why the 3 percent further tax exists only for consumers without one) and adjusts the section 235 advance tax against its income tax (ITO 2001 section 235 is adjustable for companies). Neither is a marginal cost to it. For an industrial site the plan's all-in factor is about 1.015 x 1.18 x 1.05 = 1.26 against 1.015 for what the company bears: the ledger, the fee check and the 3x and 1.5x gates run about 24 percent high, the CFO's ex-GST number will not match the product's, and diesel (whose sales tax the same company recovers) is compared on a different basis unless grossed up the same way. A commercial site (A-2) often cannot adjust federal GST against provincial sales tax on services, so the treatment is per site, not global.
- **Evidence**: R9 section 7 (further tax: "consumers without a Sales Tax Registration Number (STRN)"; "Advance income tax, s.235"); the report's fair-accounting rule "state the GST treatment", which the plan copies without deciding it; the plan's own TAR-10 form asks "the taxes charged" but never whether they are recovered.
- **Replacement**: "Two site flags from the bill form: `gst_recoverable` (default yes for B-1 to B-5 and C with an STRN, no for A-2 and A-3 unless stated) and `s235_adjustable` (default yes for companies). The price vectors, the savings ledger, the fee check and every pilot gate use the site's bearable cost: GST and section 235 are excluded where recoverable and shown as a separate 'taxes you recover' line; duty, surcharges, further and extra tax stay in. Diesel and wear are priced on the same basis. The report states the basis in its first line."

### B-2. High. The switchback cannot attribute the MDI and ICP lines, so two of the four streams have no field proof

- **Location**: Part 8.2 item 2 line 646 ("the counterfactual runs on every day of both arms, so block length costs no comparison data"); 8.3 line 657 ("Peak: Monthly MDI in kW; the fixed-charge difference"); 15.3 phase 2 and the gate "the counterfactual and the switchback agree within 15 percent"; UI-12 ("the ledger by lever ... switchback blocks").
- **Claim**: 7-day blocks inside fortnights measure the increment on every lever, and the two estimates must agree.
- **Defect**: the fixed charge is set by the single highest 30-minute block of the billing period and the ICP incremental units by the period's monthly totals; every billing month contains both arms, so the switchback has no MDI or ICP saving to report and the agreement check fails on exactly those lines. The "hidden stream" of Part 1.1 (Rs 1,250 per kW per month; the report's Rs 375,000 a year for a 25 kW cut) and the ICP collapse of the spread then rest on the counterfactual alone, the contestable layer the switchback exists to confirm.
- **Evidence**: R9 section 3 (maximum demand is "the maximum of the demand obtained in any month measured over successive periods each of 30 minutes' duration"), sections 5 and 13 step 4 (ICP by monthly totals per period); the plan's own 4.3 ("the ICP prices each period's monthly total, not a time-ordered exhaustion").
- **Replacement**: "The switchback measures the energy, export, fuel and reserve lines. The MDI and ICP lines come from the full-month counterfactual (both baselines replayed over the whole billing period) and are labelled 'counterfactual' on the ledger. At MDI-bound and ICP sites phase 3 runs as two month-long blocks (native month, optimiser month) aligned to the meter-reading dates, so the fixed-charge and ICP differences appear on two real bills. The agreement check and the carry-over credit apply to the energy, export, fuel and reserve lines only."

### B-3. High. The genset-displacement stream assumes the optimiser decides whether the genset runs; at auto-start sites it does not

- **Location**: Part 4.1 Genset row (line 304) and 4.3 (u[t] free; `outage_load_mode`); D10 line 48 ("advisory ... notifications"); Part 1.1 item 1 (Rs 11,685 per outage day); 15.3 gate ("genset run-hours fall at least 15 percent"); O2 and O4 (lines 1255, 1257); the twin (line 607: "a genset with auto-start on outage below a SoC threshold").
- **Claim**: the battery carries the critical load and the genset stays off when that is cheaper; the genset decision is a notification scored by whether the genset ran.
- **Defect**: at most C&I sites the AMF/ATS panel starts the genset on grid loss regardless of the battery. In tier 2 the product cannot prevent that run, so the Rs 11,685 day exists only where the start is SoC-triggered (a NeoTAQ, Elum or Victron-style controller, a Deye gen-start relay) or manual. No site field, selection rule or owner task records how the genset starts. At an auto-start site the plan says "genset_off(battery cheaper)", the site runs the genset anyway, the fidelity gate discards the day, and the 15 percent run-hour gate cannot be measured. The twin models the battery-first case only, so no test exercises the common case.
- **Evidence**: the report (component 6: "genset start request"; its Pakistan players table: GFE, Elum and NeoTAQ controllers run "genset start/stop on SoC thresholds", i.e. the threshold is the lever where such a controller exists; its open question 7 on who carries liability for a control-caused outage); the plan's own D10.
- **Replacement**: "Site field `genset_start_control` (auto_on_grid_loss, soc_threshold, manual) in SIT-01 and O4. With auto_on_grid_loss, u[t] is fixed to 1 for every outage step, the genset stream is reported as 'not controllable at this site' and the optimiser's genset value is loading and recharge timing only. O2 pilot rule: at least two of three sites have soc_threshold or manual start, and the site agreement lets the installer set the controller's start SoC to the product's reserve recommendation. The twin gets an auto-start profile and a golden proves the fixed-u plan."

### B-4. High. "On a feeder with load shedding" is the wrong qualifier for an industrial first buyer, and the feeder key itself expires in June 2027

- **Location**: Part 1.2 line 78; D15 line 53; Part 5.3 line 449 ("the feeder id captured at onboarding ... all sites on a feeder are sensors"); 9.6 line 746 ("Faisalabad (FESCO, 11 kV feeder with evening shedding"); 15.1 ("Target outage and genset sites first").
- **Claim**: the first buyer, a textile mill on B-2 or B-3, sits on a feeder with load shedding and stacks the outage-reserve stream.
- **Defect**: scheduled shedding follows feeder losses under the National Electricity Policy 2021, and industrial feeders are exempt: about 72 percent of Karachi's feeders are exempt "including all industrial feeders" (National Assembly, 27 October 2026, the policy applied across all DISCOs); IESCO's 744 of 1,035 feeders under 10 percent losses have zero shedding, with 2 hours at 10 to 20 percent losses, 4 at 30 to 40, 6 at 40 to 80 and 8 at 80 percent. The reserve rule of 4.3 is worth about zero where p_start is near zero, so the product's main forecast-dependent increment is thinnest at the buyer the plan names, and a FESCO industrial consumer would not believe the demo's "evening shedding". From June 2027 the Power Division plans to shed at individual transformers instead of feeders (Leghari, 19 October 2026), which breaks the per-feeder model, the feeder pooling and the CCMS feeder page as the schedule source during the pilot's own months.
- **Evidence**: Express Tribune, 28 October 2026 (72 percent of Karachi feeders exempt including all industrial feeders); APP (IESCO chief: loss bands and hours); ProPakistani, 19 October 2026 (transformer-level load shedding from June 2027); the report's own "rural MEPCO areas 12 to 16 hours a day and Lahore 3 to 4 hours".
- **Replacement**: "First-buyer qualifier: a site whose own log shows at least 20 outage hours a month (scheduled or trips), or a feeder in the DISCO's 10 percent-plus loss band, or a genset that ran 40 hours or more last month; the bill, the DISCO's feeder list and the genset log decide, not the tariff category. The outage model and the fleet-as-sensors pooling key on an `outage_unit` (the feeder code now; the distribution-transformer id from June 2027, captured from the bill or the meter), and the schedule sources add transformer-level lists when DISCOs publish them. The demo's Faisalabad plant gets unscheduled trips plus a 2-hour economic window on a mixed feeder, or becomes a MEPCO rice mill on a 30 to 40 percent loss feeder."

### B-5. High. The owner tasks are timed so the pilot cannot start when the software is ready and the gates are set before the fee exists

- **Location**: 15.2 O2 line 1255 ("Stage 6 (shadow)"), O3 line 1256 ("Stage 1"), O8 line 1261 ("Pilot"), O9 line 1262 ("Pilot"); 15.3 line 1276 ("gates ... written into the pilot template before the pilot starts"); 1.4 ("Forecasts ready ... then the pilot's phase 0").
- **Claim**: sites at Stage 6, their bills at Stage 1, the fee and the liability wording at the pilot.
- **Defect**: the bills of sites not yet signed cannot arrive five stages before the sites; the success gate is "3 times the planned monthly fee" but the fee is decided after the gate is pre-registered; the site agreement (O2) carries "the liability wording" that O9 delivers later; phase 0 plus shadow need six weeks of real site data before phase 2, and as timed they start only after Stage 8, so the pilot slips at least six weeks beyond "pilot-ready". The report's WP2 requires "3+ sites signed, covering 2+ inverter brands and 1+ genset site"; O2 says "preferably a genset".
- **Evidence**: the report's work-package order ("WP1 and WP2 start together") and WP2's acceptance; the plan's own pre-registration sentence in 15.3.
- **Replacement**: "O2: sites signed by the Stage 1 exit (three sites, two brands, at least one genset site with controllable start), their 1-minute collection started at the Stage 2 exit over CSV or HTTPS so phase 0 and shadow run during Stages 3 to 7. O3: at the Stage 1 start. O9: before the O2 agreements are signed. O8: the fee per site and the VoLL fixed in the pilot template and locked by UI-13's sign-off before phase 2 (Stage 6)."

### B-6. Medium. The metering factor is fitted on the same bills it is tested on

- **Location**: Part 6.3 line 544 ("a per-site metering factor (import kWh per period, export kWh, MDI kW) is fitted from the real bills against the telemetry totals ... and applied to both arms"); 8.2 item 3 line 647; TAR-06 acceptance ("each pilot site's last three bills within 2 percent on the calibrated rebuild"); VER-13.
- **Claim**: the calibrated rebuild matches the last three bills within 2 percent.
- **Defect**: four factors (peak kWh, off-peak kWh, export kWh, MDI kW) fitted to three bills and then tested on those bills pass at any error; in phase 0 (four weeks) at most one bill overlaps the telemetry, so per-period factors are unidentifiable. The gate then proves nothing, and a CFO will ask why the product's meter needs a per-line fudge. The plan's own rule that "a fixture produced by the engine is not a golden" applies equally to a factor fitted on the test bills.
- **Evidence**: R9 section 13 (the rebuild runs on the 1-minute series with the real reading dates) and section 15 item 9; the plan's 6.3 acceptance sentence.
- **Replacement**: "The metering factor is one scalar per connection for energy (the site CT against the DISCO register) and one for MDI, fitted on at least two overlapping bills and tested on a third held out; a factor outside 0.97 to 1.03 is a data-quality finding (CT ratio, 11 kV-side metering, a missing circuit), not a calibration. Until three overlapping bills exist the rebuild runs uncalibrated and the 2 percent gate reads 'open' in GATES."

### B-7. Medium. Two kill rules, one unmeasurable gate and two site counts

- **Location**: 15.1 line 1235 ("Kill the pure-optimisation pitch if verified savings are under 1.5 times the fee on 2 of 3 sites") against 15.3 line 1276 ("Kill: the increment below the fee at every site"); 15.3 ("throughput per saved rupee is no higher than the baseline's"; "grid-tied increments fall below 2 percent of the energy bill"); 1.4 ("8+ weeks of switchback at 2 sites") against "2 of 3 sites".
- **Claim**: the go/no-go is written against the kill criteria.
- **Defect**: VER-07's template cannot hold two kill rules; a site at 1.2 times the fee is neither success nor kill; "throughput per saved rupee" has no baseline value (the baseline saves nothing against itself; the report's gate is "without deeper cycling than baseline"); "energy bill" is undefined (grid variable charges, or grid plus fuel, with or without taxes); the success count needs three sites while "pilot done" needs two.
- **Evidence**: the report's success and kill criteria and the guideline's 1.5x rule, both copied without reconciling.
- **Replacement**: "One rule set per site on the lower bound of the 80 percent interval of the daily increment over the tuned baseline, ex-recoverable taxes, times 30: success at 3x the fee at 2 of 3 sites; kill the optimisation pitch below 1.5x at 2 of 3 sites; between the two, extend phase 2 by four weeks once, then decide. Cycling gate: equivalent full cycles per day in the optimiser arm at most 1.1 times the tuned baseline's. 'Energy bill' means the connection's variable charges ex-recoverable taxes plus fuel. Three sites in the switchback; two may finish."

### B-8. Medium. The bill window the plan asks for spans three schedule versions; only one ships

- **Location**: 6.1 row 1 ("the 2026 version stays for 2026 rebuilds"); TAR-12 ("the shipped 2026 data from SRO 279(I)/2026"); O3 ("last 12 bills"); 1.4 ("last three bills").
- **Claim**: the 2026 version rebuilds 2026 bills.
- **Defect**: O3's 12 bills (about November 2025 to October 2026) fall under three schedules: the FY2025-26 uniform schedule to 31 December 2025, SROs 41-52(I)/2026 from 1 January 2026 (B-2 36.68/27.41) and SRO 279(I)/2026 from 12 February 2026 with a proration question in February; the MDI floor was 50 percent before February 2026. A January 2026 bill rebuilt with the February rates fails the 2 percent gate for a reason the engine cannot see. The pilot's own "last three bills" in 2027 will be under the CY2027 schedule, which does not exist yet.
- **Evidence**: R9 section 1.1 (the legal chain; "let the owner confirm from a February 2026 bill whether the DISCO prorated the month"), section 3 (the floor change), section 14 (27.41 to 22.83).
- **Replacement**: "TAR-12 ships three schedule versions (FY2025-26 with the 50 percent floor, SRO 41-52(I)/2026 from 1 January 2026, SRO 279(I)/2026 from 12 February 2026 with a February proration flag) and an empty CY2027 version with status 'awaiting notification'. O3's 12 bills feed the MDI history and the ICP baseline; the 2 percent gate runs on bills from March 2026 onward and is re-run on the first 2027 bill once O13 lands."

### B-9. Medium. Nobody is tasked with keeping the tariff data current through the pilot

- **Location**: 6.1 row 1 ("a January 2027 version is planned for before go-live"); TAR-09 ("a person approving each proposed version"); 15.2 (no such task); 8.5 ("Preliminary until ... the FCA of every month in the period has been billed").
- **Claim**: watchers propose and a person approves.
- **Defect**: between the build's end and the pilot's bills, the CY2027 uniform schedule (determinations expected December 2026, SROs from 1 January 2027, possibly with different fixed charges and a February revision as in 2026), the QTA for December to February, three to six monthly FCAs, the NAEPP revision and the ICP review outcome all arrive; without a named owner and a deadline the 2027 bills cannot be rebuilt, the 2 percent gate is moot and every report stays Preliminary.
- **Evidence**: R9 section 10 and section 12 item 7 (publication cadence); R9 section 6.3 (QTA Q3 "not yet determined"; the September FCA reserved).
- **Replacement**: "O13: the owner (or an operator) enters and approves the CY2027 uniform schedule and any February revision, each quarter's QTA, each month's FCA, the NAEPP revision and the ICP review outcome within five working days of notification, from January 2027 through the pilot; TAR-09 raises an alert that blocks the monthly report until the month's adders are approved."

### B-10. Medium. The first-buyer evidence describes a different buyer from the one targeted

- **Location**: 1.2 line 78 ("named buyers exist"; "100+ kWh LFP battery"); D15 line 53.
- **Claim**: Liberty Mills, Sapphire, Gadoon, Lucky Cement and Shabbir Tiles prove the segment.
- **Defect**: those are B-3 and B-4 mills with 20 to 30 MWh bought as integrated systems whose controllers already do reserve and daytime genset switch-off (Reon REFLEX at Lucky Cement; Huawei and SMA with five gensets at Haideri under GFE); they are reached through the integrator and would use the product as a forecasting and verification layer above an existing controller. The plan's demo, Mode A tables and installer-keyed delivery fit a different buyer: the 200 kWh to 1 MWh Deye, Sungrow, Huawei or Growatt hybrid install sold by GFE or Enercon with no plant controller. B-2 is 25 to 500 kW at 400 V (R9 section 1.2); a 30 MWh mill is not on it.
- **Evidence**: the report's "First target buyer" and its Pakistan players table (Reon, GFE, Enercon deployments and methods).
- **Replacement**: "First buyer: the installer-channel B-2 or B-3 site with 200 kWh to 1 MWh of hybrid-inverter storage and a genset, where no plant controller exists and the installer keys the table. Segment 2: the MWh-scale mills with Reon, Huawei, Elum or NeoTAQ controllers, reached through the integrator, with the product as the forecast, reserve and verification layer above their controller (Mode B through the controller's interface, after Product 2)."

### B-11. Medium. No "not a prosumer, zero export" regime, the common case above 1 MWp

- **Location**: 1.3 line 88 ("under net billing or legacy net metering"); 3.2 line 225 (regime enum: net_billing, net_metering_legacy, flat, off_grid); 4.9 goldens; 9.6.
- **Claim**: every grid-tied site is a prosumer of one of two kinds.
- **Defect**: the Prosumer Regulations cap distributed generation at 1 MW and at the sanctioned load, require a load-flow study above 250 kW and stop at 80 percent transformer loading, so most mills with multi-MW PV are not prosumers and run zero-export; their PV-shift value is curtailment avoided at the full import price (Rs 35 to 48 per kWh all-in), larger than the net-billing spread, with no agreement dates, no export credit and Sunday curtailment as the dominant day. The plan's goldens, demos and ledger never price it; R9's schema already has `ProsumerRegime.kind = none`.
- **Evidence**: R9 section 8.1 (regulations 3(2), 3(3), 3(5)); section 11 (the enum); the report's named mills (1 to 30 MWh).
- **Replacement**: "Regime `none` (not a prosumer; export cap 0 or the DISCO's agreed cap; no agreement dates); a golden and a demo variant where Sunday PV is curtailed and the battery absorbs it; a 'curtailment avoided' lever on the ledger priced at the all-in import rate."

### B-12. Medium. The fuel model is diesel-only; Punjab mills run gas engines

- **Location**: 4.1 Genset row (a in L/kWh, b in L/h per rated kW, c_fuel the diesel price); 6.1 Fuel curve and Diesel rows; TAR-08; 9.6 (the textile demo's 250 kVA diesel set).
- **Claim**: the genset burns diesel priced by OGRA.
- **Defect**: the intervenors in NEPRA's QTA decision cite the captive gas levy as the reason industry shifted to the grid, i.e. the textile first buyer's "genset" is often a gas engine on captive gas or RLNG at a few tens of rupees per kWh, close to the grid's peak rate, so the dispatch answer is the opposite of diesel's and the fuel line reads in MMBtu. R9's schema carries `fuel: hsd | lng_captive | gas_captive`; the plan dropped it.
- **Evidence**: R9 section 14 (last row: "the captive gas levy is the reason the ICP intervenors cited for industry shifting to grid [P, SRO 1501 para 6]"); R9 section 11 (FuelPrice.fuel).
- **Replacement**: "The genset object carries `fuel` (hsd, gas_captive, lng_captive) with a price per unit (Rs per litre or per MMBtu, effective-dated) and a consumption curve in units per kWh and per rated kW-hour; TAR-08 adds a gas price table (the captive gas tariff and levy; SNGPL and SSGC) under the same watcher; the ledger's fuel line reads in the site's fuel; a demo variant with a 1 MW gas engine."

### B-13. Medium. The demo sites would not pass an energy manager's first look

- **Location**: 9.6 line 746.
- **Claim**: three realistic sites a Pakistani buyer recognises.
- **Defect**: (a) "an A-2(c) mall ... net billing with the regime expiring inside the demo year": a net-billing agreement runs five years from 2026 and cannot expire in the demo year; only a 2015 net-metering agreement can; (b) a Lahore mall with "no genset" and "a 15 kW critical load on a UPS": malls carry lifts, fire pumps and anchor-tenant HVAC on gensets through 3 to 4 hours of shedding; (c) the Faisalabad plant's "evening shedding" on an industrial feeder (B-4) and "a resolved read-back alert" in a tier whose delivery has "no read-back" (7.6); (d) no K-Electric site, although Enercon's clients (Al Karam, Sindh Feeds, IPAK) are in Karachi with KE's own calendar, Sindh duty, MUCT and PDF schedules.
- **Evidence**: R9 sections 8.1 and 8.3 (five-year and seven-year terms), 2.2 and 8.4 (K-Electric); the report's Pakistan players table; the plan's 7.6.
- **Replacement**: "Mall: a 2019 net-metering agreement expiring in month 7 of the demo and rolling to net billing; two 400 kVA gensets on an AMF panel; 180 kW of critical load. Textile plant: unscheduled trips plus a 2-hour economic window; the alert an 'apply not confirmed' alert. Fourth site: a K-Electric B-3 feed mill in Korangi on a zero-shedding industrial feeder with trips (KE calendar, Sindh duty, MUCT, the KE schedule PDF)."

### B-14. Medium. Genset and reserve advice has no channel the site's people read

- **Location**: D10 line 48 ("notifications (app, SMS later)"); 9.8 ("SMS or WhatsApp for genset requests and who pays"); DLV-07; the `notifications` table (line 252).
- **Claim**: the optimiser's genset start and stop decisions are notifications scored by whether the genset ran.
- **Defect**: in tier 2 the genset request, the apply reminder and the "reserve short before tonight's window" alert are the product's only levers at 19:00; an in-app notification reaches nobody on the shop floor, so the scored outcome measures the channel, not the advice, and the report's "did the inverter follow setpoints" KPI becomes "did anyone see the message".
- **Evidence**: the plan's 7.6 (confirmation from telemetry only); the report's pilot KPI "plan-execution fidelity".
- **Replacement**: "DLV-07 includes an SMS and WhatsApp sender (a local gateway, Twilio as the fallback) for genset requests, apply reminders and reserve alerts, with a one-tap Done or Not now reply that writes `notifications.outcome`; the per-message cost is a company setting; 9.8's question is closed as 'both, SMS as the fallback'."

### B-15. Medium. The pricing hypotheses anchor on the battery's gross value and do not scale with battery size

- **Location**: 15.4 line 1280 ("Rs 15,000 to 50,000 per site per month, or 20 to 30 percent of verified savings"); the guideline's pricing table ("100 kWh/day across a Rs 30-40 spread, about Rs 90k-120k/month gross"); the report ("these figures value the battery, not the optimiser").
- **Claim**: the C&I fee band is Rs 15,000 to 50,000 per site per month.
- **Defect**: the band comes from the battery's gross value, which the report explicitly says is not the optimiser's increment. On the report's own levers a 100 kWh site's forecast-dependent increment is about Rs 1,000 to 2,000 a day (skipping a needless overnight charge Rs 14.7 x 100; a cloudy-day pre-charge up to Rs 1,031; the reserve decision p x 142 minus 35.68 per kWh), about Rs 30,000 to 60,000 a month; one third is Rs 10,000 to 20,000, so Rs 50,000 needs a 250 kWh-plus site and the band says nothing about size. The report's WP11 "price feedback from 5+ site owners" is not among the owner tasks.
- **Evidence**: the report's "Worked value example" and "Which part is the optimiser's increment"; the guideline's rule "never price above one-third of verified, counterfactual-accounted savings".
- **Replacement**: "Fee hypothesis in Rs per usable kWh of battery per month (Rs 100 to 200: Rs 10,000 to 20,000 for 100 kWh, Rs 50,000 to 100,000 for 500 kWh), capped at one third of the verified increment over the tuned baseline ex-recoverable taxes, pre-registered per site; the gain-share alternative 20 to 30 percent of the same figure; O8 adds written price feedback from five site owners before phase 2 (the report's WP11)."

### B-16. Medium. One tariff binding per site; bills, MDI, ICP and the section 235 slab are per connection

- **Location**: 3.2 (`sites` with one tariff_category and regime; `site_tariffs` per site; `assets` role grid_meter); 6.3; 8.2 item 3.
- **Claim**: a site has a tariff assignment.
- **Defect**: mills commonly hold two or three DISCO connections (plant, colony or office, a B-2 and a B-3 after a load extension), each with its own reading dates, MDI, ICP baseline, sanctioned load and section 235 slab; the rebuild, the 2 percent gate and the MDI stream are per connection, and a site-level binding silently sums them, so the fixed charge and the slab are wrong for every multi-connection buyer.
- **Evidence**: R9 section 7 (section 235 on "the monthly bill amount (per meter)"), section 5 (the baseline per consumer and reference), section 11 (SiteTariffBinding is "what applies to one metered connection").
- **Replacement**: "A site holds one or more `connections`, each with its tariff binding, reading dates, MDI incumbent, ICP baseline, tax flags and bills; telemetry points map to a connection; the rebuild, the gate and the MDI and ICP lines run per connection and the ledger sums them."

### B-17. Low. The switchback's outage stratification cannot be done in advance, and the weather normalisation is unnamed

- **Location**: 8.2 item 2 line 646 ("stratified so each arm gets the same count of scheduled-outage days"); VER-08 line 1072.
- **Claim**: blocks are assigned at random within fortnights, stratified by scheduled-outage days.
- **Defect**: with one 7-day block per arm per fortnight the assignment is a coin flip per fortnight, and the second week's schedule is not known when the flip is made (CCMS pages change daily; the April 2026 national load management was announced days ahead); the stratification is unachievable as written, and "weather-normalised" names no method.
- **Evidence**: R9 section 8.4 (K-Electric reviews its load-shed zoning every three months); the report's "weather-normalised comparison" without a method; the plan's 5.3 (schedules fetched hourly and versioned, i.e. they change).
- **Replacement**: "Assign by coin flip per fortnight; normalise after the fact by regressing the daily cost on PV kWh, load kWh and outage hours with both arms pooled, and report the adjusted and unadjusted deltas with the arms' outage hours side by side."

### B-18. Low. Two acceptance steps the report requires are dropped

- **Location**: VER-10 acceptance ("a report downloads through the browser check and its figures equal the page") against the report's WP9 ("Reproduces savings from raw data; reviewed by one site owner"); O7 and O8 against WP11 ("Written positions from Enerlytics and one installer; price feedback from 5+ site owners").
- **Claim**: the report generator is accepted on a software check; the channel and pricing track is a conversation.
- **Defect**: the one review that tells the owner whether the ledger is readable by its buyer, and the one check that tells him whether anyone will pay the band in 15.4, are gone from the acceptance criteria and the owner tasks.
- **Evidence**: the report's WP9 and WP11 rows.
- **Replacement**: "VER-10: the first monthly report of each pilot site is reviewed with its energy manager in phase 1 and the review notes are filed in the pilot record. O7: written positions from Enerlytics and one installer before phase 2. O8: written price feedback from five site owners before phase 2."

### B-19. Low. Legacy net-metering practice is presented as verified

- **Location**: 6.1 line 515 ("DISCO practice nets by period and values the surplus at the off-peak rate", in the "Verified fact" column).
- **Claim**: a verified fact.
- **Defect**: R9 marks the by-period netting UNVERIFIED ("confirm on a legacy bill, in particular whether peak exports offset peak imports 1:1") and lists it among the things to confirm from bills; the provenance view (TAR-12, TAR-13) would show a secondary guess as primary, and the legacy golden (TAR-02) would be built on it.
- **Evidence**: R9 section 8.3 and section 15 item 6.
- **Replacement**: "Legacy net metering: agreements before 9 February 2026 keep the 2015 mechanism until expiry (SRO 547(I)/2026, primary); the netting practice (by period or in total; surplus at the off-peak rate) is secondary and unverified until a legacy bill confirms it; NEPRA's accounting used Rs 25.32."

### B-20. Low. Tariff F (seasonal industrial) is missing from the schedule object

- **Location**: 6.2 (the schedule: "rate cards per category with fixed charges, their basis and floor, minimum charges, the PF rule"); 1.2 (rice among the first-buyer industries).
- **Claim**: the schedule object carries every fixed-charge rule.
- **Defect**: rice shellers and other seasonal industries sit on Tariff F (fixed charges at 125 percent of the industrial rate, minimum six consecutive months in any twelve), which R9 section 4 and its schema's `seasonal_multiplier` carry and the plan drops; a Tariff F site's fixed charge rebuilds 20 percent low and the MDI lever is understated by the same share.
- **Evidence**: R9 section 1.2 (row F), section 4, section 11 (RateCard.fixed_charge.seasonal_multiplier).
- **Replacement**: "Rate cards carry `seasonal_multiplier` (1.25 for Tariff F) and the season months; the bill engine applies it in step 6 and prorates outside the season; a golden for a rice mill on Tariff F."

### B-21. Low. Part 1.1 still quotes the press export figure

- **Location**: 1.1 line 68 ("exports credited at about Rs 8 to 11") against line 65 ("the verified Rs 8.13 export credit") and 6.1 ("the press's Rs 10 to 11 is unsupported").
- **Claim**: exports are credited at about Rs 8 to 11.
- **Defect**: the same page gives two export values; a reader who prices the PV-shift stream from item 2 gets Rs 23 to 35 per kWh instead of the Rs 27.55 to 35.69 the verified credit gives.
- **Evidence**: R9 section 8.2 ("Use Rs 8.13/kWh as the modelled export credit ... flag Rs 10-11 as press").
- **Replacement**: "exports credited at Rs 8.13 (NEPRA's April to June 2026 accounting; quarterly revisable)".

---

## C. The three changes that would most raise the product's credibility with a Pakistani C&I buyer (same scope)

1. **Make every rupee the buyer's rupee** (B-1, B-2, B-6, B-8, B-9, B-16). Per-site recoverable-tax flags so the prices, the ledger, the fee and the gates are ex-GST and ex-section-235 where the company recovers them; bills per connection with the three 2025-26 schedule versions and an owner-kept 2027 schedule; the MDI and ICP lines shown from two real monthly bills (month-long blocks in phase 3) rather than from a switchback that cannot carry them; a metering factor that is one scalar with a held-out bill. A textile CFO checks the product's number against the ex-GST electricity line in the management accounts; today the two cannot agree.

2. **Pick and model sites by their outage and genset reality, not their tariff category** (B-3, B-4, B-10, B-11, B-12). The outage-hours or genset-hours qualifier with an outage key that survives the June 2027 move to transformer-level shedding; `genset_start_control` with u fixed at auto-start sites and the pilot rule that two of three sites have a controllable start; regime `none` and a gas-fuel option so both the installer-channel mill and the 1 MW-plus mill are priced as they are. The product's increment lives in the reserve and the genset; the plan must refuse sites where neither is in play.

3. **Reach the people who act, and show a site they recognise** (B-5, B-13, B-14, B-15). SMS and WhatsApp for genset requests, apply reminders and reserve alerts with a Done reply; the demo sites corrected (a legacy agreement that expires, gensets at the mall, trips at the plant, a K-Electric site); the fee in Rs per kWh of battery pre-registered with written price feedback from five owners; the sites signed at Stage 1 so phase 0 runs during the build and the gates are set against a fee that exists.

## D. Verdict

After the re-assembly the proof machinery is stronger than anything a Pakistani C&I buyer has been shown: an immutable native capture, a tuned static table as the comparator, the measured day priced by a bill engine with hand-worked goldens, block switchback with priced carry-over, and one real month of data before the pilot. What still stands between the plan and a sellable product is not the engine but site reality and the money basis. If the pilot sites are chosen by category (a textile mill on an exempt industrial feeder with an AMF panel that starts the genset on every trip), the forecast-dependent increment collapses to the TOU and PV-timing levers, roughly Rs 500 to 1,500 a day per 100 kWh before wear, and the pure-optimisation pitch fails its own 1.5x kill test. If they are chosen by their outage and genset logs, with a controllable genset start, a zero-export or net-billing regime priced correctly and the ledger ex-recoverable taxes, the report's own arithmetic puts the increment at Rs 30,000 to 60,000 a month per 100 kWh, which clears a Rs 10,000 to 20,000 fee at 2 of 3 sites with the MDI line proven on two real bills. So: sellable after the pilot as "verified outage-reserve, genset and MDI savings for sites that have them, delivered as the installer's own slot table and reported in the CFO's numbers", not as a TOU optimiser; and only if B-1 to B-5 are fixed before the pilot template is signed.

## E. Ranked index

| # | Severity | Finding |
|---|---|---|
| B-1 | High | GST and section 235, which the registered first buyer recovers, are priced into every decision and claimed as savings |
| B-2 | High | The block switchback cannot attribute the monthly MDI and ICP lines; two of four streams have no field proof |
| B-3 | High | The genset-displacement stream assumes the product decides whether the genset runs; auto-start sites are unmodelled |
| B-4 | High | "On a feeder with load shedding" is the wrong industrial qualifier (industrial feeders exempt, loss-based hours) and the feeder key expires in June 2027 |
| B-5 | High | O2, O3, O8 and O9 are timed so the pilot slips six-plus weeks and the gates are pre-registered against a fee that does not exist |
| B-6 | Medium | The metering factor is fitted and tested on the same bills, voiding the 2 percent gate |
| B-7 | Medium | Two kill rules, an unmeasurable cycling gate, an undefined "energy bill", two site counts |
| B-8 | Medium | O3's 12 bills span three schedule versions; only SRO 279(I)/2026 ships |
| B-9 | Medium | No owner task keeps the CY2027 schedule, QTA, FCAs and the ICP outcome current through the pilot |
| B-10 | Medium | The named buyers are MWh-scale integrated-system owners, not the 100 kWh B-2 site the plan builds for |
| B-11 | Medium | No "not a prosumer, zero export" regime, the common case above 1 MWp |
| B-12 | Medium | Diesel-only fuel; Punjab mills run gas engines on captive gas |
| B-13 | Medium | Demo sites: a net-billing regime that expires, a mall without gensets, evening shedding on an industrial feeder, no K-Electric site |
| B-14 | Medium | Genset and reserve advice has no SMS or WhatsApp channel in v1 |
| B-15 | Medium | The fee band anchors on the battery's gross value and ignores battery size; WP11's price feedback dropped |
| B-16 | Medium | One tariff binding per site although bills, MDI, ICP and the slab are per connection |
| B-17 | Low | Outage stratification cannot be done before the fortnight's schedule exists; no normalisation method |
| B-18 | Low | WP9's owner review of the first report and WP11's written positions are dropped |
| B-19 | Low | Legacy by-period netting shown as a verified fact (R9: unverified) |
| B-20 | Low | Tariff F seasonal multiplier missing from the schedule object |
| B-21 | Low | Part 1.1 line 68 still quotes the press export figure of Rs 8 to 11 |

Findings file: `C:/Users/SaadTahir/Videos/solarytics/plan-work/review/market-lens.md`.
