# R9 — Pakistan tariff and regime data for the EMS tariff engine

Research notes, written 6 November 2026, for the OnyxLeaf EMS (TOU-, outage- and genset-aware dispatch optimiser for Pakistani C&I sites with PV + LFP battery + grid + diesel genset). This file is self-contained: it states what the tariff engine must encode, which numbers were verified against a primary source (NEPRA, a DISCO, OGRA, a gazette SRO) and which only against secondary reporting, and what the owner must confirm from real bills.

Conventions used below:
- **[P]** = verified against a primary source (NEPRA / DISCO / OGRA / K-Electric official page or PDF) on 6 Nov 2026.
- **[S]** = found only in secondary sources (press, tariff-guide blogs).
- **UNVERIFIED** = could not be checked against anything reliable in this run; treat as a placeholder.
- All rates are Rs/kWh unless stated; all fixed charges are Rs/kW/month unless stated.
- "Research documents" = `Perfect-EMS_TOU_Dispatch_Deep_Review.md` and `Product 1 - TOU-Aware Energy Management and Dispatch.md` in `C:/Users/SaadTahir/Videos/solarytics/`. Their numbers are quoted as "doc says ..." and then checked.

The file is appended section by section as the research proceeds (sections 0-17), so a reader can trust each section independently. Section 0 is the summary; 11-13 are the data model, versioning and bill-rebuild algorithm; 14 verifies the research documents; 15 lists what to confirm from real bills; 16 lists sources.

## 0. Headline findings (read this first)

1. The research documents' tariff table is correct. Every peak/off-peak rate and fixed charge they quote for A-1 TOU, A-2(b), A-2(c), A-3, B-1(b), B-2(b), B-3, B-4, C-1(c) and D-2(b) matches the GoP-applicable uniform schedule in NEPRA's decision of 11 Feb 2026 (Annex A-1 for XWDISCOs, Annex-C for K-Electric), notified as SRO 279(I)/2026 (12 Feb 2026). **[P]** Two corrections: A-3 and B-1(b) fixed charges are Rs 1,000 **per consumer** per month, not per kW; the "Geo reports Rs 1,250" for B-1(b) is wrong.
2. Peak windows are defined by NEPRA in the "Terms and Conditions of Tariff" annexed to each DISCO determination, not by a monthly DISCO notice: Dec-Feb 5-9 PM, Mar-May 6-10 PM, Jun-Aug 7-11 PM, Sep-Nov 6-10 PM, "to be duly adjusted in case of day light time saving". **[P]** (LESCO determination 14 Jul 2023, Annex-V; the same text is reused in every XWDISCO determination). K-Electric differs: Apr-Oct 6:30-10:30 PM, Nov-Mar 6:00-10:00 PM. **[P]** (KE tariff-structure page).
3. Maximum Demand is "the maximum of the demand obtained in any month measured over successive periods each of 30 minutes' duration" (15 minutes for arc furnaces). **[P]** Fixed charges for A-2, A-3, B and C are billed on the higher of 25% of sanctioned load or actual MDI for the month; A-1 TOU on the higher of 50% of sanctioned load or MDI. **[P]**
4. The Rs 0.5194/kWh QTA (SRO 1501(I)/2026, 7 Sep 2026) is billed Sep-Nov 2026 to all categories including K-Electric, uniformly on every kWh (no peak/off-peak split), **except** lifeline, prepaid and units billed under the ICP. **[P]**
5. ICP: Rs 22.98/kWh flat on incremental peak and off-peak units above a baseline; baseline = the consumer's consumption in the same month of Dec 2023-Nov 2024, escalated 2.8%/yr; 3 years from Dec 2025 billing month; positive FCA applies on incremental units, QTA, Debt Service Surcharge and negative FCA do not. **[P]** (SRO 2409(I)/2025 and the NEPRA decision of 9 Dec 2025). The 5 Oct 2026 review hearing has not produced a notified outcome as of 6 Nov 2026 **(UNVERIFIED / pending)**.
6. Net billing: regulation 14 of the Prosumer Regulations 2026 bills imports at the applicable tariff and credits exports at the "national average energy purchase price" (NAEPP); credit in excess of the bill rolls to the next cycle or is paid quarterly; 5-year agreements; DG capacity capped at sanctioned load; no new DG on a transformer at 80% loading. **[P]** NEPRA accounted net-billing exports at Rs 8.13/kWh and legacy net-metering exports at Rs 25.32/kWh for Apr-Jun 2026. **[P]** Legacy net-metering consumers keep the old rate and mechanism until their agreement expires (SRO 547(I)/2026, 2 Apr 2026, deemed effective 9 Feb 2026), unless they materially modify the system. **[P]**
7. Diesel is now priced **daily** by OGRA (since mid-July 2026), not fortnightly. HSD was Rs 400.67/L on 30 Oct 2026 (the documents' figure), Rs 402.96 by 2 Nov, then cut on 3, 4, 5 and 6 Nov to Rs 382.25/L. **[S]** (OGRA press releases as reported by APP, Dawn, Business Recorder; the OGRA site itself was not fetched).
8. "Until end-December 2026" is right in substance: the schedule is a calendar-year 2026 determination; the PYA-2025 column "shall cease on 31 Dec 2026" **[P]**; CY2027 rebasing petitions were heard 13-15 Oct 2026 and the next uniform schedule is expected from 1 Jan 2027 **[S]**.

## 1. The CY2026 uniform schedule (SRO 279(I)/2026)

### 1.1 Legal chain and dates (all [P] from the NEPRA decision TRF-100/2935-57 dated 11 Feb 2026, 51 pages, read page by page)

- 7 Jan 2026: NEPRA determined XWDISCO tariffs for **calendar year 2026** (rebasing moved from 1 July to 1 January; national average tariff Rs 33.38/kWh for CY2026).
- 12 Jan 2026: NEPRA decided the Federal Government's uniform-tariff motion; notified as SROs 41-52(I)/2026 dated 13 Jan 2026 (effective 1 Jan 2026).
- 4 Feb 2026: Cabinet approved a revised uniform schedule (fixed charges for domestic consumers, lower industrial variable rates) within the budgeted Rs 249 bn tariff differential subsidy.
- 10 Feb 2026: hearing; 11 Feb 2026: NEPRA decision (no objection to the Motion); revised Annex-II/III for every XWDISCO plus Annex-A, A-1, B, B-1 and C.
- Notified by the Federal Government as **SRO 279(I)/2026 dated 12 Feb 2026** (IESCO tariff guide, KE FAQ). Dawn (17 Feb) reports the new rates "effective 11 Feb 2026". **The tariff engine should treat 12 Feb 2026 as the effective date and let the owner confirm from a February 2026 bill whether the DISCO prorated the month.**
- The decision covers all ten XWDISCOs (PESCO, HESCO, GEPCO, QESCO, MEPCO, FESCO, LESCO, IESCO, SEPCO, TESCO) plus HAZECO, and K-Electric under the uniform tariff policy (Annex-C).

### 1.2 GoP-applicable rates (what the consumer is billed before adjustments and taxes) [P]

Source: Annex-C (K-Electric) and Annex-A-1 (XWDISCOs) of the 11 Feb 2026 decision; identical figures on the IESCO tariff guide page (iesco.com.pk/tariff-guide). Rs/kWh for variable charges; Rs/kW/month for fixed charges unless marked /consumer.

| Code | Category | Fixed charge | Basis of the fixed charge | Peak | Off-peak | Non-TOU flat |
|---|---|---|---|---|---|---|
| A-1(b) | Residential TOU (sanctioned load 5 kW and above) | 675 /kW/M | higher of 50% of sanctioned load or MDI (Feb 2026: 50% of SL where MDI unavailable) | 46.85 | 34.53 | - |
| A-2(a) | Commercial below 5 kW | 1,000 /consumer/M | per connection | - | - | 37.44 |
| A-2(b) | Commercial 5 kW and above, regular | 1,250 /kW/M | higher of 25% of sanctioned load or actual MDI | - | - | 39.76 |
| A-2(c) | Commercial TOU | 1,250 /kW/M | same | 43.82 | 35.15 | - |
| A-2(d) | EV charging station | nil | - | - | - | 23.57 (no FCA, see 6.3) |
| A-3 | General services | 1,000 /consumer/M | per connection | - | - | 42.48 |
| B-1 | Industrial up to 25 kW, non-TOU | 1,000 /consumer/M | per connection | - | - | 26.23 |
| B-1(b) | Industrial up to 25 kW, TOU | 1,000 /consumer/M | per connection | 35.74 | 25.48 | - |
| B-2(a) | Industrial 25-500 kW at 400 V, non-TOU | 1,250 /kW/M | higher of 25% SL or actual MDI | - | - | 26.16 |
| B-2(b) | Industrial 25-500 kW, TOU | 1,250 /kW/M | same | 35.68 | 22.83 | - |
| B-3 | Industrial at 11/33 kV (up to 5 MW), TOU | 1,250 /kW/M | same | 35.68 | 23.67 | (27.00 listed; all B-3 are TOU per T&C) |
| B-4 | Industrial at 66/132 kV, TOU | 1,250 /kW/M | same | 35.68 | 23.38 | (26.43 listed) |
| B-5 | Industrial at 220 kV and above | 1,250 /kW/M | same | 35.68 | 23.13 | - |
| C-1(a) | Bulk at 400/230 V below 5 kW | 2,000 /consumer/M | per connection | - | - | 43.39 |
| C-1(b) | Bulk at 400 V, 5-500 kW | 1,250 /kW/M | 25% SL or MDI | - | - | 40.63 |
| C-1(c) | Bulk at 400 V, 5-500 kW, TOU | 1,250 /kW/M | same | 46.31 | 37.54 | - |
| C-2(a)/(b) | Bulk at 11/33 kV | 1,250 /kW/M | same | 46.31 | 36.03 | 40.57 |
| C-3(a)/(b) | Bulk at 66/132 kV | 1,250 /kW/M | same | 46.31 | 35.76 | 40.77 |
| D-2(a) | Agricultural tube wells, non-TOU | 400 /kW/M | sanctioned load (T&C: D-2 billing demand = sanctioned load) | - | - | 28.90 |
| D-2(b) | Agricultural 5 kW and above, TOU | 400 /kW/M | sanctioned load | 29.54 | 28.69 | - |
| F | Seasonal industrial | 125% of the corresponding industrial fixed charge, minimum 6 months | | | | |
| E-2 | Temporary industrial | 5,000 /consumer/M | | | | 42.25 |

Notes from the schedule [P]:
- "Where Fixed Charges are applicable Rs/kW/Month, the charges shall be billed based on 25% of sanctioned Load or Actual MDI for the month which ever is higher" (printed under the A-2, A-3, B and C blocks of Annex A-1). The 2023 T&C had 50%; the Feb 2026 schedule lowered it to 25% for these categories; A-1 TOU keeps 50%.
- Minimum charges: A-1 Rs 75 (single-phase) / Rs 150 (three-phase) per month "even if no energy is consumed"; "for consumers where monthly Fixed charges are applicable, no minimum charges shall be applicable". Agriculture: Rs 2,000 per consumer per month minimum. For commercial and industrial consumers with Rs/kW fixed charges the practical minimum is therefore the fixed charge on 25% of sanctioned load. (The LESCO 2023 T&C listed A-2/A-3 minimums of Rs 175 single-phase / Rs 350 three-phase and a "B-1 and B-1(b) fixed minimum charge of Rs 350"; those are superseded by the Rs 1,000/consumer fixed charge.) K-Electric still lists Rs 175/350 for commercial where no fixed charge applies [P, KE FAQ].
- Prepaid tariffs exist for every class (industrial 35.72 flat); ignore unless a site is prepaid.
- Late payment surcharge: 10% of the amount billed excluding government taxes and duties [P, T&C].
- Power factor: in two-part tariffs the average PF at the point of supply must be at least 90%; below that, a penalty of a 2% increase in fixed charges per 1% drop below 90%, on the fixed charges "determined with reference to maximum demand during the month" [P, T&C].
- NEPRA-determined (cost-reflective) rates differ from GoP-applicable rates; the gap is subsidy or cross-subsidy (Annex-A/B). Example: the uniform determined B-2 TOU peak is 35.00 and off-peak 25.49 versus GoP 35.68 / 22.83. The engine should bill the GoP-applicable column but keep a field for the determined rate for scenario work.

### 1.3 Who is on which tariff (from the T&C, still in force) [P]
- All new and existing consumers with sanctioned load 5 kW and above in A-1, A-2, B-1, C-1 and D get TOU metering and the TOU tariff; B-2, B-3, B-4 and C-2/C-3 are TOU by definition. B-3 is 500 kW to 5 MW at 11/33 kV (or smaller loads that opt for 11/33 kV); B-4 is above 5 MW at 66/132 kV.
- Consumers below 5 kW may opt for TOU metering.
- Industrial consumers may switch to seasonal Tariff F at a new connection or the start of a season; once chosen it stays for at least one year.

### 1.4 Verification of the research documents' tariff table
All ten rows match the primary source. Corrections: (a) A-3 fixed charge is Rs 1,000 per consumer per month (no kW basis); (b) B-1(b) fixed charge is Rs 1,000 per consumer per month, Geo's Rs 1,250 is wrong; (c) B-2(b)/B-3/B-4 fixed Rs 1,250/kW/M is billed on the higher of 25% of sanctioned load or actual MDI (the "MDI basis not verified" caveat is resolved); (d) C-1(c) fixed charge is Rs 1,250/kW/M on the same basis; (e) B-3 and B-4 fixed charges marked "not legible" are Rs 1,250/kW/M.

## 2. Peak windows (TOU) by season

### 2.1 XWDISCOs [P]
NEPRA "Terms and Conditions of Tariff (for supply of electric power to consumers by licensees)", Part-I, General Definitions, clause 10 (LESCO determination TRF-603/604 of 14 Jul 2023, Annex-V page 1; the same annex is attached to every XWDISCO determination and is the basis of the "peak hours" printed on bills):

| Months (inclusive) | Peak | Off-peak |
|---|---|---|
| December to February | 5 PM to 9 PM | remaining 20 hours |
| March to May | 6 PM to 10 PM | remaining 20 hours |
| June to August | 7 PM to 11 PM | remaining 20 hours |
| September to November | 6 PM to 10 PM | remaining 20 hours |

"To be duly adjusted in case of day light time saving" (Pakistan has not used DST since 2009, so nothing applies today, but keep the hook).

There is no "official monthly peak-hours notice": the windows are fixed by the T&C and change only when NEPRA revises the T&C. NEPRA in Aug 2025 asked each DISCO to study "localised" peak/off-peak hours (Dawn, 3 Aug 2025) and APTMA in Jan 2026 asked for the window to be revisited; no revised T&C has been notified as of 6 Nov 2026 [S]. The Daily Pakistan (Aug 2025) story that "peak hours end at 11 PM" refers to the June-August 7-11 PM window, not a change. The research documents' seasonal table (from an unofficial GEPCO guide) is consistent with the T&C.

### 2.2 K-Electric [P]
KE tariff-structure page (ke.com.pk) and FAQs: **April to October 6:30 PM to 10:30 PM; November to March 6:00 PM to 10:00 PM**; remaining 20 hours off-peak. KE denied rumours of a change in July 2025 citing NEPRA's approved schedule. KE bills the same GoP-applicable rates (Annex-C).

### 2.3 Engine rule
Peak membership is a pure function of (DISCO group, local calendar month, local clock minute). Store it as an effective-dated "tou_calendar" object (section 11) so a NEPRA revision becomes a new version, not a code change. All timestamps must be Asia/Karachi (UTC+5, no DST).

## 3. Maximum demand (MDI), billing demand and related definitions [P]

From the same T&C (Annex-V, Part-I):
- "Maximum Demand", where applicable, "means the maximum of the demand obtained in any month measured over successive periods each of 30 minutes' duration", except arc furnaces (15 minutes). Units: kW ("Fixed Charge means the part of sale rate in a two-part tariff to be recovered on the basis of Billing Demand in kilowatt on monthly basis"). The K-Electric FAQ states the same 30-minute rule.
- "Billing Demand" (2023 text): 50% of the sanctioned load or actual maximum demand recorded in the month, whichever is higher, except agriculture D-2 where billing demand = sanctioned load. The Feb 2026 schedule changes the floor to **25%** for A-2, B and C and keeps 50% for A-1 TOU (section 1.2).
- "Sanctioned Load" = the load in kW applied for by the consumer and allowed/authorised by the company.
- Power factor = kWh / kVAh over the month (or kWh / sqrt(kWh^2 + kVARh^2)).
- Whether the 30-minute windows are fixed clock blocks (00:00-00:30, 00:30-01:00, ...) or sliding is not stated; DISCO and KE MDI meters use fixed demand-integration blocks in practice (UNVERIFIED: confirm from the meter's demand-interval setting and MDI reset at the site). Whether MDI is taken over all hours or only peak hours is also not stated; bills print one MDI value for the month, i.e. all hours [S, confirm on a bill].
- The ICP decision (section 5) adds: where a meter cannot record MDI, sanctioned load is used, and NEPRA load factors by category are used for benchmark consumption (B-2 50%, B-3 60%, B-4 50%, ice factories and cold storage 70%, flour mills 40%).

## 4. Other rules worth encoding [P]
- Tariff F seasonal industrial: fixed charges at 125% of the regular industrial rate, recovered only for the running season, minimum 6 consecutive months in any 12.
- Meter-reading date shifts of more than 4 days: fixed charges prorated by actual days (B-3/B-4/C/D conditions). Bills are "30 days or less reckoned from the date of last meter reading", so the bill period is not a calendar month; the rebuild must use the actual reading dates.
- Prosumer (net-billing) bills are also on a "billing cycle" of thirty days of metered energy (reg. 2(viii)).

## 5. Incremental Consumption Package (ICP) [P]

Primary sources read in full: NEPRA decision of 9 Dec 2025 (TRF-100, "Incremental Consumption Package for Industrial and Agriculture Consumers of XWDISCOs and K-Electric") and its notification SRO 2409(I)/2025 dated 10 Dec 2025. Cabinet approval: Case No. 758/Rule-19/2025/1030 of 5 Nov 2025.

Provisions the engine must encode:
- Rate: **PKR 22.98 per kWh** on "peak and off-peak incremental consumption over the corresponding reference period", for industrial (B-1..B-5) and private agricultural consumers, TOU and non-TOU, XWDISCOs and K-Electric. Reference months: **December 2023 to November 2024**. The research documents' "baseline" wording should be read as: baseline for billing month m is the consumer's own consumption in the same calendar month of the reference year, escalated by (1 + 2.8%)^n where n = package year (1, 2 or 3).
- Duration: three years "from the date of approval", applied "on billing month basis", automatic expiry. First billing month: December 2025 (Dawn: cleared for Dec-Feb billing). Expiry: November 2028 billing month (inference from "three years" and "billing month basis"; UNVERIFIED exact last month).
- Adders: "Positive Fuel Cost Adjustments (FCAs) shall be applicable to eligible consumers on incremental consumption. Further, Quarterly Tariff Adjustments (QTAs), Debt Service Surcharge (DSS) and negative FCAs shall not be applicable on incremental consumption." So ICP units carry: base Rs 22.98 + positive FCA (if any) + taxes/duties ("Duties and taxes shall be calculated on the consumer's payable amount"). They do not carry QTA, DSS (the Rs 3.23 surcharge) or negative FCA.
- Fixed charges are unaffected (still on 25% SL / MDI).
- Benchmark (baseline) consumption rules: existing consumer with reference consumption available: Reference Consumption for the particular month x (1 + 2.8%)^n. No reference data, new consumer, or connection after the reference period: Higher of (MDI for the relevant month or Sanctioned Load) x hours in the month x Load Factor of the category x (1 + 2.8%)^n. Sanctioned-load enhancement: reference consumption plus (Higher of MDI or new SL minus old SL) x hours x load factor x escalation. A change of tariff category or non-TOU to TOU after the reference month makes the consumer "new". Captive power plants (CPPs) are treated as new consumers. Detection units excluded; dial-adjustment corrections included. Defective/locked meter in the current month: not eligible.
- Net-metering consumers are eligible only if there is net import in the month in the respective peak and off-peak periods; their reference consumption is imported units only; incremental units are capped at (imports minus exports) in the month and distributed pro rata between peak and off-peak. Wheeling consumers excluded.
- Review triggers: aggregate industrial + agricultural incremental consumption above 25% over baseline triggers a review of the price; semi-annual reviews; "scheme will be terminated immediately if upward tariff adjustments are required for two consecutive reviews".
- Load factors table (NEPRA) for benchmark: A-1 single-phase 20%, three-phase 25%; A-2 20/25%; A-3 15/20%; B-1 40% (ice factories, cold storage, plastic moulding, rice shellers excepted); B-2 50% (same exceptions); ice factory / cold storage 70%; plastic moulding, rice sheller 50%; B-3 textile, steel furnaces and other continuous industry 60%; other normal industry (engineering works) 50%; flour mills B-2/B-3 40%; B-4 50%; agricultural tube wells 15% perennial / 50% non-perennial; SCARP 50%.

Status of the October 2026 review (all [S], no primary outcome found): NEPRA asked in September 2026 whether the package should be suspended or the rate raised to marginal cost; hearing held Monday 5 Oct 2026 (Tribune, 6 Oct). Power Division figures: weighted marginal cost Dec-May Rs 27.58/kWh, loss-adjusted Rs 31.83, six-month weighted average about Rs 32; break-even B-2 TOU Rs 29.05, B-3 Rs 30.03, B-4 Rs 29.62. Growth exceeded 25% in Jan (25.13%), Feb (26.20%) and Apr 2026 (34.81%). FPCCI argued for Rs 16. As of 6 Nov 2026 no decision or SRO revising Rs 22.98 was found on nepra.org.pk or in the press. **The engine must model the ICP rate and its validity window as versioned fields, and the optimiser must treat the marginal price of incremental kWh as Rs 22.98 + positive FCA + taxes while the package stands.**

Implication for the dispatch optimiser (new, not in the research documents): for an ICP-eligible site with consumption above baseline, the marginal grid price in **both** peak and off-peak is Rs 22.98 (+FCA, +taxes), not 35.68 / 22.83, until the month's consumption falls back below baseline. The TOU spread collapses for incremental units; the value of battery peak shifting then depends on whether peak consumption is above or below its own baseline in that month. The bill rebuild must therefore split kWh into (baseline, incremental) by period before pricing.

## 6. Periodic adjustments: QTA and FPA/FCA

### 6.1 Mechanism [P]
From the NEPRA T&C annexes (LESCO determination, Annex-I and I-A):
- FPA (also "FCA", monthly): Fuel Price variation = Actual fuel cost component (CPPA-G pool price for the month) minus Reference fuel cost component (Annex-IV monthly reference). "The fuel price adjustment determined by the Authority shall be shown separately in the bill of the consumer and the billing impact shall be worked out on the basis of consumption by the consumer in the respective month."
- QTA (quarterly): Actual PPP excluding fuel minus reference PPP excluding fuel, plus the impact of T&D losses on the FCA, summed for the quarter and divided by projected sales to give a Rs/kWh adder. Since 2023 NEPRA also approves a **uniform** QTA rate for all XWDISCOs and KE (policy guidelines of 22 Aug 2023, reproduced in SRO 1501).

### 6.2 How they apply by hour and category
- Both are flat Rs/kWh adders on every unit in the billing month, with no peak/off-peak distinction [P: SRO 1501 "at a uniform rate of Rs 0.5194/kWh, to be applicable to all consumer categories"]. The research document's statement that QTA leaves the TOU spread unchanged is correct.
- QTA exemptions [P, SRO 1501 paras 2 and 31]: lifeline consumers, units billed under the incremental consumption package, prepaid consumers.
- FCA exemptions [P for policy, S for the specific Oct 2026 decision]: positive FCA applies to all categories except lifeline, EV charging stations and prepaid (KE FAQ; Tribune 8 Oct 2026 on the Aug-2026 FCA); negative FCA is not passed to lifeline, protected residential, EVCS and prepaid (KE FAQ). ICP incremental units get positive FCA but not negative FCA (section 5).
- K-Electric: the same FCA has applied uniformly to KE and XWDISCOs since June 2025 (KE FAQ) and the QTA is extended to KE with the same application period (SRO 1501 para 31) [P].
- Timing: the FCA for month m is normally billed in month m+2 (July 2026 FCA Rs 2.0581/kWh billed in September 2026; August 2026 FCA Rs 1.11/kWh billed in October 2026). If bills for the month are already issued before notification, the amount is applied the next month [P, SRO 1501 para 4]. NEPRA may bundle several months' FCAs into one bill (KE FAQ).

### 6.3 Current values (6 Nov 2026)
| Adjustment | Rs/kWh | Billing months | Status |
|---|---|---|---|
| QTA Q1 CY2026 (Jan-Mar) | -1.9857 | Jun, Jul, Aug 2026 | [S] Business Recorder, hearing 19 May 2026 |
| QTA Q2 CY2026 (Apr-Jun) | +0.5194 | Sep, Oct, Nov 2026 | [P] SRO 1501(I)/2026 of 7 Sep 2026 (decision 4 Sep 2026; Rs 12,669 m; DISCO-wise rates range from -12.7961 TESCO to +3.5803 QESCO but a uniform 0.5194 is billed) |
| QTA Q3 CY2026 (Jul-Sep) | not yet determined | expected Dec 2026-Feb 2027 | UNVERIFIED: no filing or hearing notice found; NEPRA directed DISCOs to file immediately after quarter close |
| FCA July 2026 | +2.0581 | Sep 2026 | [S] Dawn 5 Sep 2026 |
| FCA Aug 2026 | +1.11 | Oct 2026 | [S] Tribune 8 Oct 2026 (ref 7.10 vs actual 8.21) |
| FCA Sep 2026 | +2.5624 requested (ref 7.4596 vs actual 10.0220) | Nov 2026 expected | [S] hearing 28/29 Oct 2026, decision reserved (Tribune 30 Oct); no decision found by 6 Nov |

The engine therefore needs a monthly "adders" table keyed by billing month, with each adder carrying its own exemption set (lifeline, prepaid, EVCS, ICP units, protected) and sign.

## 7. Surcharges, duties and taxes that scale bill lines

The Federal Power Division's "Electricity Billing" page (power.gov.pk) lists nine items; KE's tariff-structure page and FAQ give the current rates for KE; the Feb 2026 NEPRA decision (FPCCI comments) confirms the stack: "electricity duty, general sales tax (17-18%), financing cost surcharge, fuel price adjustment, PHL surcharge, extra tax, further tax, and sales tax". Rates below are the best current evidence; the base for each must be confirmed on a real bill (section 15).

| Line | Rate | Base (what it multiplies) | Who pays / exemptions | Source and status |
|---|---|---|---|---|
| Debt Service Surcharge (also printed as "FC Surcharge", "Financing Cost Surcharge" or "PHL surcharge") | Rs 3.23/kWh (43 paisa for protected residential up to 200 units and agricultural tube wells, 2023 structure) | every kWh billed, all hours; not on ICP incremental units | all categories; lifeline residential excluded | [S] NEPRA approval Mar 2023 (Dawn 1 Apr 2023); still Rs 3.23 in Jan 2026 (Profit 15 Jan 2026); ICP decision names it "Debt Service Surcharge (DSS)" [P]; KE FAQ cites SRO 293(I)/2023 "up to Rs 3.82". The Power Division page's "Rs 0.43/kWh FC surcharge" is stale (2019-era). UNVERIFIED: exact current per-category rates; confirm on bill. |
| Neelum-Jhelum surcharge | Rs 0.10/kWh | every kWh | all except lifeline | ECC approved revocation around 2020-21 but it was still on bills in a NEPRA complaint decision of Sep 2024 and a July 2026 column says it still appears. UNVERIFIED: confirm presence on a 2026 bill; model as an optional adder defaulting to 0. |
| Electricity duty (provincial) | 1.0% to 1.5% of variable charges (Power Division). KE/Sindh: domestic 1.5%, commercial 2%, industrial 1.5%, bulk 2%, agricultural 1% [P, KE page]. Punjab/KP/Balochistan per-category rates: UNVERIFIED (third-party calculators say 1.5% of energy charge for LESCO). | "variable charges": energy charge; whether FPA/QTA/surcharges are inside the base is UNVERIFIED (one guide says ED = 1.5% of energy + FPA + QTR + NJ) | all consumers | Power Division page [P-official but undated]; KE page [P] |
| Punjab additional electricity duty on heavy consumers | 4 paisa per unit | units consumed from grid, collected by DISCOs; and units self-generated by gensets/solar above 500 kVA, collected by electric inspectors (Electricity Duty Rules 2026) | industrial and commercial above 500 kVA only; domestic exempt | [S] Punjab Finance (Amendment) Bill 2025 (Nov 2025) and Rules 2026 (June 2026). Relevant to the genset model: self-generation at large Punjab sites attracts 4 paisa/kWh. |
| General Sales Tax | 18% (Finance Supplementary Act 2023 raised 17% to 18%; Power Division page still says 17%) | the bill value: energy + fixed charges + FPA + QTA + surcharges + electricity duty (ED is generally inside the GST base; fixed charges and PTV fee treatment UNVERIFIED) | all consumers; lifeline exempt per some guides (UNVERIFIED) | KE FAQ: "18% on the total bill" [P for KE]; [S] others |
| Further tax | 3% | bill value | consumers without a Sales Tax Registration Number (STRN); not domestic, agriculture, bulk, street-light | Power Division page [P-official] |
| Extra tax | 5% to 17% by bill-amount slab | bill amount slabs | industrial and commercial consumers not on the FBR Active Taxpayer List | Power Division page [P-official]; slab boundaries UNVERIFIED |
| Advance income tax, s.235 Income Tax Ordinance 2001 | Industrial/commercial: nil up to Rs 500; 10% of bill for Rs 500-20,000; above Rs 20,000: Rs 1,950 + 12% (commercial) or 5% (industrial) of the excess. Residential: 0% below Rs 25,000; 7.5% at Rs 25,000 or more for non-ATL (ATL exempt). | the monthly bill amount (per meter) | all; exemption certificates possible under s.235 | KE page and FAQ [P for KE]; PKRevenue TY2027 card confirms the same slabs under Finance Act 2026 [S]. Non-filer doubling under the Tenth Schedule: UNVERIFIED whether s.235 is excepted. |
| "Sales tax" on commercial consumers (fixed retailer tax) | 5% (bill up to Rs 20,000), 7.5% (above) | bill amount | commercial consumers (retailers) only | Power Division page [P-official]; PM "suspended" fixed sales tax through bills in Nov 2024 [S]; UNVERIFIED whether it still appears |
| PTV licence fee | Rs 35 domestic, Rs 60 commercial, flat per bill | - | domestic and commercial | Power Division page [P-official] |
| Municipal Utility Charges Tax (MUCT), Karachi only | Rs 750 per month commercial and industrial (from 1 Jul 2025); residential slabs Rs 0-300 | flat | KE consumers outside cantonments; agriculture, street lights and bulk exempt | KE FAQ [P] |
| Late payment surcharge | 10% of the amount billed excluding government taxes and duties | - | all | NEPRA T&C [P] |
| Bank/collection charges, meter rent, arrears, instalments | site-specific | - | - | take from the bill |

Ordering used by DISCO billing systems, as far as the public sources allow (confirm on a real bill): (1) energy charges by period x rate; (2) fixed charges; (3) FPA, QTA and surcharges on kWh; (4) electricity duty on (1)+(3) variable items; (5) GST on (1)+(2)+(3)+(4); (6) income tax s.235 on the bill; (7) further/extra tax where applicable; (8) flat fees (PTV, MUCT); (9) arrears, adjustments, LPS.

## 8. Net billing, legacy net metering and K-Electric

### 8.1 NEPRA (Prosumer) Regulations 2026, SRO 251(I)/2026 of 9 Feb 2026 [P, 17 pages read]
- Scope: solar, wind or biogas DG up to 1 MW; applicant is a 3-phase 400 V or 11 kV domestic, commercial, industrial, agricultural, general-services or bulk consumer (reg. 2(v), (ix)).
- Capacity: "shall not exceed the sanctioned load of the applicant's premises" (reg. 3(2)); NEPRA may revise by notification. 250 kW and above requires a load-flow study (reg. 3(3)). "The licensee shall not entertain any application if the distributed generation capacity connected to a particular distribution transformer has reached 80% of its rated capacity" (reg. 3(5) proviso).
- Concurrence fee: Rs 1,000/kW one-time (Schedule-IV). Amended by SRO 709(I)/2026 (28 Apr 2026): no fee and no NEPRA concurrence for 25 kW and below, deemed effective 9 Feb 2026 [P listing on nepra.org.pk Legal page; S for content via Dawn 28 Apr 2026]. SRO 1330(I)/2026 (6 Aug 2026): systems of 25 kW or below are approved by the licensee (DISCO/KE) instead of NEPRA concurrence [P text, from nepra.org.pk]. Fresh concurrence/approval is needed for any technical modification or if generation does not start within six months (reg. 4).
- Agreement term: five years from commissioning, renewable for five more "and so on" by mutual consent (reg. 7). Prosumer may terminate on 30 days' notice; licensee only with NEPRA approval (reg. 8).
- Billing (reg. 14): at the end of each billing cycle (30 days of metered energy), "(a) the kWh supplied by licensee to prosumer shall be billed in accordance with the applicable tariff; (b) the kWh supplied by prosumer to the licensee shall be billed in accordance with the national average energy purchase price." Explanation: net billing = licensee buys the DG output and bills consumption at the applicable tariff "after giving credit for electricity supplied by prosumer to the licensee at the national average energy purchase price". If the export credit exceeds the import bill, the net amount "shall be credited against prosumer's next billing cycle or shall be paid by the licensee to the prosumer quarterly" (reg. 14(2)). NEPRA "may revise the rate provided in sub-regulation (1) through the notification during the subsistence of the agreement and the rate so revised shall be deemed incorporated in the agreement" (reg. 14(3)).
- Metering: bidirectional meter or two meters (reg. 5(1)(a), 13). Nothing in the regulations defines TOU netting for exports; exports are priced at one NAEPP regardless of hour. Imports are at the applicable (TOU) tariff.
- Savings (reg. 21) as amended by SRO 547(I)/2026 (2 Apr 2026): agreements executed under the 2015 regulations continue to be billed "in accordance with the rate and mechanism provided in the repealed regulations till the expiry of the term of such agreement"; that protection ceases on any material modification that changes the facility's maximum output; deemed effective 9 Feb 2026 [P, read]. (The original 9 Feb text had moved existing consumers to the "national average power purchase price", i.e. Rs 8.13; this was reversed.)

### 8.2 The export rate: what NAEPP is and how it is set
- The regulation names the rate but not the number. The only primary figure found is SRO 1501(I)/2026 para 23: "Cost of Net-metering units and Net billing units exported to XWDISCOs system by consumers have been accounted for @ Rs 25.32/kWh and Rs 8.13/kWh as per the Notified National Average Power Purchase Price for the quarter April to June 2026 based on Power Purchase Price decision notified w.e.f. January 01, 2026." [P] Reading: NAEPP (energy-only component of the PPP) = Rs 8.13/kWh; NAPPP (energy + capacity etc.) = Rs 25.32/kWh for Q2 CY2026; both derive from the CY2026 PPP reference and are "notified" per quarter.
- Press reported "around Rs 10-11" at launch (Dawn 10 Feb 2026); Dawn 17 Feb 2026 and NEPRA's own Q2 accounting use Rs 8.13. **Use Rs 8.13/kWh as the modelled export credit for new prosumers (effective 9 Feb 2026), flag Rs 10-11 as press, and treat the rate as quarterly-revisable (the PPP reference for CY2027 will change it from 1 Jan 2027).** The owner must confirm the printed export rate on a new prosumer's bill, because DISCOs may be using a different notified figure.
- Policy watch [S]: PM directed a review of the buyback rate (22 Oct 2026, The News); the Power Division floated a TOU-based net-billing idea paying Rs 18-22/kWh for battery exports between 5 and 10 PM (ProPakistani 2 Aug 2026) — a proposal only, nothing notified. The engine must allow an export-price schedule by TOU window so this can be switched on without code changes.

### 8.3 Legacy net metering (2015 regulations), for sites with agreements executed before 9 Feb 2026
- Mechanism as commonly implemented by DISCOs (the 2015 regulations were not fetched this run; [S]): imported and exported kWh are netted within the billing month; a net import is billed at the applicable tariff; a net export is credited at the off-peak rate of the consumer's tariff and carried forward, with periodic cash settlement. For TOU consumers the DISCO practice has been to net peak against peak and off-peak against off-peak, then value any net surplus at the off-peak rate (UNVERIFIED; confirm on a legacy bill, in particular whether peak exports offset peak imports 1:1).
- NEPRA's Q2 CY2026 accounting values legacy exports at Rs 25.32/kWh [P]; Dawn quoted "Rs 26-27" as the old credit, consistent with an off-peak-rate credit.
- Legacy agreements run 7 years from their original date (press [S]); on expiry or material modification the site moves to net billing. The engine needs per-site fields: regime (net_metering_2015 | net_billing_2026 | none), agreement_start, agreement_end, and a "modification" flag.

### 8.4 K-Electric differences (what to override for Karachi sites)
- Rates: identical uniform schedule (Annex-C) [P]. FCA and QTA: identical and applied with the same period since June 2025 / Sep 2026 [P].
- Peak hours: 6:30-10:30 PM Apr-Oct, 6:00-10:00 PM Nov-Mar [P] (XWDISCOs: section 2.1).
- Electricity duty (Sindh): industrial 1.5%, commercial 2%, bulk 2%, agricultural 1% of variable charges [P].
- MUCT Rs 750/month for commercial and industrial [P]. Minimum charges where no fixed charge applies: commercial Rs 175 (1-phase) / Rs 350 (3-phase) [P].
- Net billing: the same NEPRA regulations and amendments apply to KE as licensee; NEPRA's July 2026 complaint decision directed a Karachi consumer to complete KE's formalities "for grant of prosumer/net-billing" [S]. KE FAQ: existing net-metering agreements continue until expiry unless materially modified; surplus "adjusted in future bills or paid quarterly"; after expiry surplus is valued at NAEPP (energy component only) [P, KE].
- Load shedding: KE runs its own ATCL-based load-shed zoning, reviewed every three months (KE FAQ) [P]; relevant to the outage forecaster, not the tariff engine.

## 9. Diesel price and genset fuel model

### 9.1 Pump price of HSD (ex-depot, Rs per litre)
Regime change: since mid-July 2026 OGRA sets petrol and HSD prices **daily** on international prices (the cabinet gave OGRA the task; Dawn 5 Nov 2026) [S]. The fortnightly cycle the research documents assume no longer holds.

| Effective | HSD Rs/L | Source |
|---|---|---|
| 30 Oct 2026 | 400.67 (+0.85) | ARY, 29 Oct [S] (matches the research documents) |
| 31 Oct - 2 Nov 2026 | 402.96 (+2.29) | APP/Gari [S] |
| 3 Nov 2026 | 395.20 (-7.76) | OGRA press release via APP and Business Recorder [S] |
| 4 Nov 2026 | 391.52 (-3.68) | Dawn [S] |
| 5 Nov 2026 | 387.81 (-3.71) | Dawn [S] |
| **6 Nov 2026** | **382.25 (-5.56)** | Dawn 5 Nov 2026, OGRA notification [S] |

None of these were read on ogra.org.pk itself (site not fetched), so all are [S], but they are OGRA notifications reported verbatim by the state news agency and two national dailies. Delivered price at site = ex-depot + dealer margin (already in the ex-depot "retail" figure) + transport; add 1-3% for delivery and a theft/shrinkage allowance from site data.

### 9.2 Fuel curve constants (HOMER convention) [P, HOMER Pro 3.15 docs]
F(t) = F0 x Y_gen + F1 x P_gen(t) when running, else 0; F0 = intercept coefficient (L/hr per kW rated), F1 = slope (L/hr per kW output).
- HOMER's documented example (50 kW genset: 8.48 L/h at 25 kW, 15.3 L/h at 50 kW): F1 = 0.273 L/hr/kW, F0 = 0.033 L/hr/kW_rated. These are example values, not library defaults. The commonly quoted HOMER generic-diesel library defaults (about 0.08 L/hr/kW_rated intercept and 0.25 L/hr/kW slope, minimum load ratio 25%) could not be confirmed from the fetched pages: UNVERIFIED; take F0 and F1 per genset from its data sheet (25/50/75/100% consumption points) and fit the line.
- Check against the research documents: with the example curve, specific consumption is 0.405 L/kWh at 25% load and 0.306 L/kWh at 100% (doc: 0.41 and 0.31; matches). At Rs 382.25/L that is Rs 155/kWh at 25% load and Rs 117/kWh at full load, fuel only; add Rs 5-15/kWh O&M (doc) and the Punjab 4 paisa/kWh self-generation duty where applicable.
- Encode per genset: rated kW, F0, F1, minimum load ratio, minimum run time, start cost (litres or Rs), and a price feed with daily effective dates.

## 10. End of the 2026 uniform schedule and what follows
- The schedule is a **calendar-year** tariff: NEPRA's CY2026 determinations (7 Jan 2026) and the uniform SROs (13 Jan and 12 Feb 2026) run 1 Jan-31 Dec 2026; every Annex-II states "The PYA 2025 column shall cease on 31 Dec 2026" [P]. There is no separate "expiry" SRO; the schedule is replaced by the CY2027 rebasing.
- CY2027 process [S]: ten XWDISCOs filed CY2027 petitions (indexation of the 2025-30 MYT, prior-year adjustments of about Rs 268 bn, revenue requirement Rs 2.64 trn); hearings 13-15 Oct 2026; NEPRA also said it will consider "whether fixed charges and their application to consumer categories should be revised". CPPA-G's PPP projection for 2027 (hearing 3 Nov 2026) is up to Rs 27.309/kWh versus the CY2026 reference, which would raise variable rates and the NAEPP/NAPPP export values. Expect: NEPRA determinations in Dec 2026, a Federal Government uniform-tariff motion, and new SROs effective 1 Jan 2027 (the 2026 pattern: determination 7 Jan, uniform SRO 13 Jan, revised 12 Feb). The engine must be ready for a January 2027 version with possibly different fixed charges, and must keep the 2026 version for bill rebuilds of 2026 months.
- Also expected by year-end: QTA Q3 CY2026 (billed Dec-Feb), FCA for Sep-Nov 2026 (billed Nov-Jan), and a decision on the ICP review.

## 11. Tariff data model (JSON schema sketch)

Design principles: (1) every object is effective-dated and immutable; (2) the national schedule, the TOU calendar, the monthly adders, the tax rules, the prosumer regime and the fuel price are **separate** versioned objects joined at bill time, because they change on different cadences (yearly, rarely, monthly/quarterly, yearly budget, per site, daily); (3) the site record says which objects apply and carries the site's own numbers (sanctioned load, category, DISCO, regime, ICP baseline); (4) every number carries a source and a verification flag so the UI can show "verified against SRO" versus "entered from bill".

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "onyxleaf/ems/tariff/v1",
  "$defs": {
    "Money": {"type": "number", "description": "PKR, 4 dp"},
    "Source": {
      "type": "object",
      "required": ["kind", "ref", "retrieved_on", "verified"],
      "properties": {
        "kind": {"enum": ["nepra_sro", "nepra_decision", "disco_page", "ke_page", "ogra", "fbr", "provincial_act", "press", "bill", "manual"]},
        "ref": {"type": "string", "description": "e.g. 'SRO 279(I)/2026 dated 12-02-2026, Annex-C'"},
        "url": {"type": "string"},
        "retrieved_on": {"type": "string", "format": "date"},
        "verified": {"enum": ["primary", "secondary", "unverified"]}
      }
    },
    "Validity": {
      "type": "object",
      "required": ["effective_from"],
      "properties": {
        "effective_from": {"type": "string", "format": "date"},
        "effective_to": {"type": ["string", "null"], "format": "date", "description": "null = open-ended; set when superseded"},
        "applies_to_billing_months": {"type": "array", "items": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}$"}, "description": "for adders billed by month rather than by consumption date"}
      }
    },
    "TouCalendar": {
      "type": "object",
      "required": ["id", "validity", "timezone", "seasons", "source"],
      "properties": {
        "id": {"type": "string", "examples": ["tou.xwdisco.2023", "tou.ke.2019"]},
        "validity": {"$ref": "#/$defs/Validity"},
        "timezone": {"const": "Asia/Karachi"},
        "dst_adjust": {"type": "boolean", "default": false},
        "seasons": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["months", "windows"],
            "properties": {
              "months": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 12}},
              "windows": {"type": "array", "items": {
                "type": "object",
                "required": ["period", "start", "end"],
                "properties": {
                  "period": {"enum": ["peak", "off_peak", "shoulder"]},
                  "start": {"type": "string", "pattern": "^[0-9]{2}:[0-9]{2}$"},
                  "end": {"type": "string", "pattern": "^[0-9]{2}:[0-9]{2}$", "description": "exclusive; may cross midnight"}
                }}}
            }
          }
        },
        "default_period": {"enum": ["off_peak"], "default": "off_peak"},
        "source": {"$ref": "#/$defs/Source"}
      }
    },
    "RateCard": {
      "description": "One consumer category in one national schedule version",
      "type": "object",
      "required": ["category_code", "metering", "energy_rates", "fixed_charge", "source"],
      "properties": {
        "category_code": {"type": "string", "examples": ["A-1(b)", "A-2(c)", "B-2(b)", "B-3", "C-1(c)", "D-2(b)"]},
        "category_group": {"enum": ["residential", "commercial", "general_services", "industrial", "bulk", "agricultural", "temporary", "seasonal", "other"]},
        "voltage_level": {"enum": ["lv_400", "mv_11_33", "hv_66_132", "ehv_220"]},
        "sanctioned_load_kw_min": {"type": "number"},
        "sanctioned_load_kw_max": {"type": ["number", "null"]},
        "metering": {"enum": ["tou", "flat"]},
        "energy_rates": {
          "type": "object",
          "properties": {
            "peak": {"$ref": "#/$defs/Money"},
            "off_peak": {"$ref": "#/$defs/Money"},
            "flat": {"$ref": "#/$defs/Money"},
            "nepra_determined_peak": {"$ref": "#/$defs/Money"},
            "nepra_determined_off_peak": {"$ref": "#/$defs/Money"}
          }
        },
        "fixed_charge": {
          "type": "object",
          "required": ["amount", "unit", "basis"],
          "properties": {
            "amount": {"$ref": "#/$defs/Money"},
            "unit": {"enum": ["per_kw_month", "per_consumer_month"]},
            "basis": {"enum": ["max_pct_sl_or_mdi", "sanctioned_load", "none"]},
            "sl_floor_pct": {"type": "number", "description": "25 for A-2/B/C, 50 for A-1 TOU"},
            "mdi_interval_min": {"type": "integer", "default": 30},
            "mdi_scope": {"enum": ["all_hours", "peak_only"], "default": "all_hours", "description": "all_hours per bills; confirm"},
            "mdi_block": {"enum": ["fixed", "sliding"], "default": "fixed", "description": "UNVERIFIED"},
            "seasonal_multiplier": {"type": "number", "default": 1.0, "description": "1.25 for Tariff F"}
          }
        },
        "minimum_charge": {
          "type": "object",
          "properties": {
            "single_phase": {"$ref": "#/$defs/Money"},
            "three_phase": {"$ref": "#/$defs/Money"},
            "per_consumer_month": {"$ref": "#/$defs/Money"},
            "waived_when_fixed_charge_applies": {"type": "boolean", "default": true}
          }
        },
        "power_factor_rule": {
          "type": "object",
          "properties": {"threshold_pct": {"type": "number", "default": 90}, "penalty_pct_per_pct": {"type": "number", "default": 2}, "applies_to": {"const": "fixed_charge"}}
        },
        "late_payment_surcharge_pct": {"type": "number", "default": 10},
        "source": {"$ref": "#/$defs/Source"}
      }
    },
    "Schedule": {
      "description": "A national (or KE) schedule of tariff version, e.g. SRO 279(I)/2026",
      "type": "object",
      "required": ["id", "jurisdiction", "validity", "rate_cards", "tou_calendar_id", "source"],
      "properties": {
        "id": {"type": "string", "examples": ["sot.xwdisco.2026-02-12", "sot.ke.2026-02-12"]},
        "supersedes": {"type": ["string", "null"]},
        "jurisdiction": {"enum": ["xwdisco_uniform", "ke"]},
        "validity": {"$ref": "#/$defs/Validity"},
        "rate_cards": {"type": "array", "items": {"$ref": "#/$defs/RateCard"}},
        "tou_calendar_id": {"type": "string"},
        "notes": {"type": "string"},
        "source": {"$ref": "#/$defs/Source"}
      }
    },
    "Adder": {
      "description": "Flat Rs/kWh item billed in given billing months: FCA, QTA, DSS/FC surcharge, NJ surcharge",
      "type": "object",
      "required": ["id", "kind", "rate_per_kwh", "validity", "exempt", "source"],
      "properties": {
        "id": {"type": "string", "examples": ["qta.cy2026.q2", "fca.2026-08", "dss.2023"]},
        "kind": {"enum": ["fca", "qta", "dss", "nj", "other"]},
        "rate_per_kwh": {"$ref": "#/$defs/Money", "description": "signed; negative = refund"},
        "consumption_month": {"type": "string", "description": "for FCA: the month whose fuel cost it reflects"},
        "validity": {"$ref": "#/$defs/Validity", "description": "use applies_to_billing_months"},
        "applies_by_period": {"enum": ["all_hours"], "default": "all_hours"},
        "exempt": {"type": "array", "items": {"enum": ["lifeline", "protected", "prepaid", "evcs", "icp_incremental_units", "net_metering_legacy"]}},
        "jurisdictions": {"type": "array", "items": {"enum": ["xwdisco_uniform", "ke"]}},
        "source": {"$ref": "#/$defs/Source"}
      }
    },
    "TaxRule": {
      "type": "object",
      "required": ["id", "kind", "validity", "applies_to_categories", "base", "source"],
      "properties": {
        "id": {"type": "string", "examples": ["gst.18", "ed.sindh.industrial", "ed.punjab.industrial", "it235.industrial.ty2027", "further_tax.3", "extra_tax.nonatl", "ptv.commercial", "muct.karachi.ci", "punjab_ed_4paisa"]},
        "kind": {"enum": ["pct_of_base", "per_kwh", "flat_per_bill", "slab_on_bill"]},
        "rate": {"type": "number"},
        "slabs": {"type": "array", "items": {"type": "object", "properties": {"from": {"type": "number"}, "to": {"type": ["number", "null"]}, "fixed": {"type": "number"}, "pct_over_from": {"type": "number"}}}},
        "base": {"type": "array", "items": {"enum": ["energy", "fixed", "fca", "qta", "dss", "nj", "electricity_duty", "gst", "self_generated_kwh"]}},
        "applies_to_categories": {"type": "array", "items": {"type": "string"}},
        "province": {"enum": ["punjab", "sindh", "kp", "balochistan", "ict", "ajk", "gb", null]},
        "conditions": {"type": "object", "properties": {"requires_stnr": {"type": "boolean"}, "requires_atl": {"type": "boolean"}, "min_sanctioned_kva": {"type": "number"}}},
        "validity": {"$ref": "#/$defs/Validity"},
        "source": {"$ref": "#/$defs/Source"}
      }
    },
    "IcpRule": {
      "type": "object",
      "required": ["id", "rate_per_kwh", "reference_period", "escalation_pct_per_year", "validity", "source"],
      "properties": {
        "id": {"const": "icp.2025"},
        "rate_per_kwh": {"const": 22.98},
        "eligible_categories": {"type": "array", "items": {"type": "string"}, "default": ["B-1", "B-1(b)", "B-2(a)", "B-2(b)", "B-3", "B-4", "B-5", "D-2(a)", "D-2(b)"]},
        "reference_period": {"type": "object", "properties": {"from": {"const": "2023-12"}, "to": {"const": "2024-11"}}},
        "escalation_pct_per_year": {"const": 2.8},
        "adders_on_incremental": {"type": "object", "properties": {"positive_fca": {"const": true}, "negative_fca": {"const": false}, "qta": {"const": false}, "dss": {"const": false}}},
        "load_factors": {"type": "object", "additionalProperties": {"type": "number"}},
        "validity": {"$ref": "#/$defs/Validity", "description": "2025-12 .. 2028-11 billing months unless revised"},
        "source": {"$ref": "#/$defs/Source"}
      }
    },
    "ProsumerRegime": {
      "type": "object",
      "required": ["id", "kind", "validity", "source"],
      "properties": {
        "id": {"type": "string", "examples": ["prosumer.net_billing.2026", "prosumer.net_metering.2015"]},
        "kind": {"enum": ["net_billing", "net_metering", "none"]},
        "export_price": {
          "type": "object",
          "properties": {
            "mode": {"enum": ["naepp_flat", "off_peak_rate_of_tariff", "tou_schedule"]},
            "rate_per_kwh": {"$ref": "#/$defs/Money", "description": "8.13 for Q2-CY2026 NAEPP"},
            "by_period": {"type": "object", "additionalProperties": {"$ref": "#/$defs/Money"}},
            "revision_cadence": {"enum": ["quarterly", "annual", "on_notification"]}
          }
        },
        "netting": {
          "type": "object",
          "properties": {
            "window": {"enum": ["none", "billing_cycle"]},
            "by_period": {"type": "boolean", "description": "legacy: peak vs peak, off-peak vs off-peak (UNVERIFIED)"},
            "surplus_valuation": {"enum": ["naepp", "off_peak_rate"]},
            "surplus_carry_forward": {"type": "boolean"},
            "cash_settlement": {"enum": ["quarterly", "annual", "none"]}
          }
        },
        "agreement_term_years": {"type": "integer"},
        "capacity_cap": {"enum": ["sanctioned_load"]},
        "transformer_cap_pct": {"type": "number", "default": 80},
        "validity": {"$ref": "#/$defs/Validity"},
        "source": {"$ref": "#/$defs/Source"}
      }
    },
    "FuelPrice": {
      "type": "object",
      "required": ["fuel", "effective_from", "price_per_litre", "source"],
      "properties": {
        "fuel": {"enum": ["hsd", "lng_captive", "gas_captive"]},
        "effective_from": {"type": "string", "format": "date"},
        "price_per_litre": {"$ref": "#/$defs/Money"},
        "delivery_adder_pct": {"type": "number", "default": 2},
        "source": {"$ref": "#/$defs/Source"}
      }
    },
    "SiteTariffBinding": {
      "description": "What applies to one metered connection; the engine resolves versions by date",
      "type": "object",
      "required": ["site_id", "disco", "province", "category_code", "sanctioned_load_kw", "schedule_jurisdiction", "tou_calendar_id", "prosumer_regime_id"],
      "properties": {
        "site_id": {"type": "string"},
        "disco": {"enum": ["LESCO", "IESCO", "FESCO", "GEPCO", "MEPCO", "PESCO", "HESCO", "SEPCO", "QESCO", "TESCO", "HAZECO", "KE"]},
        "province": {"enum": ["punjab", "sindh", "kp", "balochistan", "ict", "ajk", "gb"]},
        "category_code": {"type": "string"},
        "sanctioned_load_kw": {"type": "number"},
        "connected_kva": {"type": "number"},
        "schedule_jurisdiction": {"enum": ["xwdisco_uniform", "ke"]},
        "tou_calendar_id": {"type": "string"},
        "prosumer_regime_id": {"type": "string"},
        "agreement_start": {"type": "string", "format": "date"},
        "agreement_end": {"type": "string", "format": "date"},
        "dg_capacity_kw": {"type": "number"},
        "icp": {"type": "object", "properties": {"eligible": {"type": "boolean"}, "baseline_kwh_by_month": {"type": "object", "additionalProperties": {"type": "object", "properties": {"peak": {"type": "number"}, "off_peak": {"type": "number"}}}}, "package_year_start": {"type": "string", "format": "date"}}},
        "tax_flags": {"type": "object", "properties": {"has_stnr": {"type": "boolean"}, "on_atl": {"type": "boolean"}, "it235_exemption_cert": {"type": "boolean"}, "ed_rate_override_pct": {"type": "number"}}},
        "meter": {"type": "object", "properties": {"reading_day_of_month": {"type": "integer"}, "mdi_interval_min": {"type": "integer", "default": 30}, "bidirectional": {"type": "boolean"}}},
        "overrides": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "value": {}, "source": {"$ref": "#/$defs/Source"}}}, "description": "values taken from the site's real bills that differ from the national objects"}
      }
    }
  }
}
```

Seed values for the first version (all from sections 1-9): `sot.xwdisco.2026-02-12` and `sot.ke.2026-02-12` (identical rate cards, different tou_calendar_id); `tou.xwdisco.2023` (Dec-Feb 17:00-21:00, Mar-May 18:00-22:00, Jun-Aug 19:00-23:00, Sep-Nov 18:00-22:00); `tou.ke.2019` (Apr-Oct 18:30-22:30, Nov-Mar 18:00-22:00); adders `qta.cy2026.q1` -1.9857 (Jun-Aug 2026), `qta.cy2026.q2` +0.5194 (Sep-Nov 2026), `fca.2026-07` +2.0581 (Sep 2026), `fca.2026-08` +1.11 (Oct 2026), `dss.2023` +3.23 (open-ended), `nj` +0.10 (flag unverified); `icp.2025`; `prosumer.net_billing.2026` with export 8.13 (effective 2026-02-09, quarterly), `prosumer.net_metering.2015` (netting by billing cycle, surplus at off-peak rate, UNVERIFIED by_period); tax rules per section 7; fuel `hsd` 382.25 effective 2026-11-06.

## 12. Versioning approach

1. **Never edit in place.** A tariff object is written once with `effective_from` and `effective_to = null`. A change creates a new object with `supersedes = <old id>`; the loader closes the old one by setting its `effective_to` to the day before the new `effective_from` (the only mutation allowed, and it is itself logged).
2. **Resolution by date, not by "latest".** Every bill rebuild and every optimiser run resolves `(jurisdiction, category, date)` to the version in force on that date; monthly adders resolve by **billing month**, not by consumption date (an FCA for July is billed in September and belongs to the September bill).
3. **Three time axes** must be kept apart: consumption timestamp (1-minute data), billing month (meter-reading to meter-reading), and notification date (when the rate became known). Store `known_from` on each version so counterfactual replays can be run "as known at the time" (what the optimiser could have known) and "as finally billed" (what the bill will show). Savings verification uses "as finally billed"; the optimiser uses "as known".
4. **Retroactive notifications are normal here** (FCA notified after the month ends, the Prosumer amendment deemed effective 9 Feb 2026, QTA applied "in subsequent month" if bills were issued). The schema's `applies_to_billing_months` plus `known_from` handle this without rewriting history; a bill rebuilt before the FCA was known is re-run and both results are kept with their `known_from`.
5. **Provenance on every number** (`Source.verified`): primary, secondary, unverified. The UI must show the flag; the savings report must state which lines rest on unverified rates.
6. **Site overrides are versioned too**: when a real bill shows a different ED rate, surcharge, or peak-hour window, record an `overrides[]` entry with the bill as source, effective from that bill's period, rather than changing the national object.
7. **Publication cadence to plan for**: schedule yearly (1 Jan, with a possible February revision as in 2026); TOU calendar rarely; QTA quarterly (billed over 3 months); FCA monthly; DSS/NJ on policy change; ICP semi-annual review; NAEPP quarterly; provincial duties with provincial budgets (June); FBR rules with the Finance Act (1 July); fuel daily. Automate watchers on nepra.org.pk (Tariff/Notifications and Legal pages), ke.com.pk and OGRA, with human approval before a new version goes live.
8. **Identity of a version** = content hash of the canonical JSON; store the hash in each bill-rebuild result so a report can be reproduced exactly later.

## 13. Bill-rebuild algorithm at 1-minute resolution

Inputs per site and billing period: 1-minute series of grid import kW (P_imp), grid export kW (P_exp), optionally kVArh or power factor; the meter-reading dates (start exclusive, end inclusive); the SiteTariffBinding; the resolved Schedule, TouCalendar, Adders for the billing month, TaxRules, IcpRule, ProsumerRegime, and for the counterfactual the same series simulated for the inverter's own mode.

Algorithm (apply identically to actual and counterfactual series; the saving is the difference line by line):

1. **Period tagging.** For each minute t (Asia/Karachi), period(t) = peak if the local time is inside the window for month(t) in the TouCalendar, else off_peak. Windows are half-open [start, end). Month is the calendar month of t, not the billing month (a Sep-Nov window in a bill that spans 20 Nov-19 Dec changes at 1 Dec).
2. **Energy by period.** E_imp[p] = sum over t in p of P_imp(t)/60 kWh; E_exp[p] likewise. Flat-metered categories use one bucket. Round to the meter's resolution (whole kWh) only at the end, keep 1-minute precision internally.
3. **Maximum demand.** Aggregate P_imp into 30-minute fixed blocks aligned to the hour (configurable: interval, fixed/sliding, all-hours/peak-only); MDI = max block average kW over the billing period. If kVA is metered, also compute kVA demand for PF purposes. Billing demand = max(sl_floor_pct x sanctioned_load, MDI) for Rs/kW categories; = sanctioned load for D-2; not applicable for per-consumer fixed charges.
4. **ICP split (industrial/agricultural with icp.eligible).** For each period p: baseline_p = baseline_kwh_by_month[same month of reference year][p] x (1.028)^n; incremental_p = max(0, E_imp[p] - baseline_p); base_p = E_imp[p] - incremental_p. For net-metering sites cap total incremental at (imports - exports) and pro-rate across periods. Keep the split for the FCA and surcharge steps.
5. **Energy charges.** energy = sum_p base_p x rate[p] + sum_p incremental_p x 22.98. Legacy net-metering sites: first net E_exp against E_imp by period (if `netting.by_period`) or in total; bill the net import at rate[p]; carry or credit the net export (step 9).
6. **Fixed charges.** fixed = amount x billing_demand (per_kw_month) or amount (per_consumer_month); apply the Tariff F multiplier and prorate by days when the reading interval deviates by more than 4 days from 30. Minimum charge applies only when no fixed charge applies. PF penalty: if PF < 90%, fixed = fixed x (1 + 0.02 x (90 - PF)).
7. **Adders.** For each Adder whose `applies_to_billing_months` contains this billing month and whose jurisdiction matches: amount = rate x (E_imp_total - exempt_units), where exempt_units = incremental ICP units for QTA, DSS and negative FCA; zero for positive FCA; all units if the category is in the adder's exempt list. Adders are flat across periods.
8. **Variable subtotal** V = energy + adders. Electricity duty = ed_rate(province, category) x V (default base: energy + adders; make the base configurable; Punjab 4 paisa/kWh for >500 kVA as a per_kwh rule on imported kWh, plus on self-generated kWh if modelling the genset duty).
9. **Export credit.** net_billing: credit = E_exp_total x NAEPP (flat, no period), or by period if `export_price.mode = tou_schedule`. net_metering_2015: credit = net surplus kWh x off-peak rate (or as configured); carry-forward balance updated. If credit > (V + fixed + duty), the excess rolls to the next cycle (or quarterly cash); the bill does not go negative.
10. **GST** = 18% x (energy + fixed + adders + electricity duty) by default, with a configurable base and a flag for whether the export credit is netted before or after GST (UNVERIFIED on real prosumer bills: DISCO practice is to compute taxes on the gross import bill and then subtract the credit; confirm).
11. **Income tax s.235**: slab on the bill amount so far (industrial: 0 up to 500; 10% to 20,000; 1,950 + 5% above; commercial 12%). Further tax 3% if no STRN (not for bulk/agri/domestic); extra tax 5-17% by slab if not on ATL (non-ATL industrial/commercial). Residential 7.5% if bill >= 25,000 and non-ATL.
12. **Flat items**: PTV fee, MUCT (KE), meter rent, bank charges; arrears and LPS from the bill, not modelled.
13. **Outputs**: a line-item bill (energy by period, ICP units, fixed, each adder, ED, GST, IT, other taxes, export credit, flat fees, total) plus the three aggregates the verification needs: cost of imported energy, cost of demand, and credits. Store with the version hashes (section 12) and the series hash.
14. **Counterfactual and A/B.** Run steps 1-13 for the optimiser's actual series and for the replayed baseline series (inverter self-use/TOU mode simulated at 1-minute with the same PV and load), same billing dates, same versions. Savings = bill(baseline) - bill(actual), reported per line so the owner can see that fixed-charge savings come from MDI, energy savings from period shifting, ICP effects from baseline crossing, and export changes from NAEPP. For the diesel side, fuel cost = sum over genset-on minutes of (F0 x Y + F1 x P_gen)/60 x price(date) x (1 + delivery_adder) plus O&M per kWh; price resolves by day.
15. **Reconciliation.** When the real bill arrives, compare each line; log the residual per line; a persistent residual on a line (e.g. ED or surcharge) becomes a site override (section 12.6), never a silent re-tune.

## 14. Verification of the numbers the research documents quote

| Claim in the research documents | Verdict | Primary source read |
|---|---|---|
| SRO 279(I)/2026 (Feb 2026) is the national schedule for all XWDISCOs and KE "until end-December 2026" | Correct in substance: CY2026 schedule; PYA column ceases 31 Dec 2026; CY2027 replaces it from 1 Jan 2027 [S for 2027] | NEPRA decision 11 Feb 2026 (Annex-II notes) [P] |
| A-1 TOU 46.85 / 34.53, fixed Rs 675/kW/M on 50% SL or MDI | Correct | Annex-C and A-1 [P] |
| A-2(c) 43.82 / 35.15, fixed Rs 1,250/kW/M on 25% SL or MDI | Correct | [P] |
| A-2(b) 39.76, fixed Rs 1,250/kW/M | Correct | [P] |
| A-3 42.48, fixed Rs 1,000 | Rate correct; fixed charge is Rs 1,000 per consumer per month | [P] |
| B-1(b) 35.74 / 25.48, fixed Rs 1,000 (Geo: 1,250) | Rates correct; fixed = Rs 1,000 per consumer per month; Geo wrong | [P] |
| B-2(b) 35.68 / 22.83, fixed Rs 1,250/kW/M, MDI basis not verified | Correct; basis = higher of 25% SL or actual MDI (30-minute MD) | [P] |
| B-3 35.68 / 23.67; B-4 35.68 / 23.38; fixed not legible | Correct; fixed Rs 1,250/kW/M, same basis | [P] |
| C-1(c) 46.31 / 37.54, fixed not found | Correct; fixed Rs 1,250/kW/M, same basis | [P] |
| D-2(b) 29.54 / 28.69, Rs 2,000 minimum | Correct; plus fixed Rs 400/kW/M on sanctioned load | [P] |
| B-2 off-peak fell from 27.41 to 22.83 in Feb 2026 | Correct (Dawn 17 Feb 2026: B-2 off-peak 27.41 to 22.83, peak 36.68 to 35.68) | [S] press; the decision's "Before/After" table shows industry average 29.34 to 25.29 [P] |
| QTA +Rs 0.5194/kWh billed Sep-Nov 2026 under SRO 1501(I)/2026, applies to all hours | Correct; exemptions: lifeline, prepaid, ICP units; also KE | SRO 1501(I)/2026 [P] |
| Peak windows winter 5-9, spring/autumn 6-10, summer 7-11 PM (unofficial GEPCO guide) | Correct and now sourced: NEPRA T&C clause 10 | LESCO determination Annex-V [P] |
| KE 6:30-10:30 PM Apr-Oct; no official monthly notice found | Correct; add Nov-Mar 6-10 PM; windows are fixed by the T&C/KE schedule, there is no monthly notice | KE tariff page [P] |
| ICP flat Rs 22.98/kWh above baseline, all hours, from Dec 2025 for 3 years; consumption 25-35% above baseline; review hearing 5 Oct 2026 | Correct; baseline months are Dec 2023-Nov 2024 (+2.8%/yr); positive FCA applies, QTA/DSS/negative FCA do not; outcome of the review not found | SRO 2409(I)/2025 and decision 9 Dec 2025 [P]; Tribune 6 Oct 2026 [S] |
| Prosumer Regulations 9 Feb 2026; exports at NAEPP ~Rs 10-11; NEPRA accounting Rs 8.13 (net billing) and 25.32 (net metering) in Q2 2026; 5-year contracts; cap at sanctioned load; 80% transformer | All correct; Rs 8.13 is the figure to use; regulation 14 defines the mechanism; 16 Feb draft was finalised as SRO 547(I)/2026 on 2 Apr 2026 (the doc said "no source confirms it was finalised") | SRO 251(I)/2026 [P]; SRO 1501 para 23 [P]; SRO 547(I)/2026 [P] |
| SRO 1330(I)/2026 (6 Aug 2026): DISCO approval without NEPRA concurrence for 25 kW and below | Correct | nepra.org.pk Legal page and SRO text [P] |
| PM directed buyback review 22 Oct 2026 | Not re-verified this run (press only) [S] | - |
| pv magazine "Rs 13" export rate | Not supported by any primary source; disregard | - |
| ProPakistani 2 Aug 2026: Rs 22/unit night exports | A Power Division proposal (Rs 18-22/kWh, 5-10 PM, for battery owners); nothing notified [S] | ProPakistani [S] |
| Diesel Rs 400.67/L from 30 Oct 2026 | Correct for 30 Oct; superseded daily; Rs 382.25/L from 6 Nov 2026; OGRA now prices daily | ARY 29 Oct, Dawn 5 Nov [S] |
| HOMER example curve 0.41 L/kWh at 25% load, 0.31 at full load | Correct arithmetic from HOMER's documented example (F0 0.033, F1 0.273) | HOMER docs [P] |
| Genset fuel Rs 100-132/kWh at 0.25-0.33 L/kWh | At Rs 382.25/L: Rs 96-126/kWh; at Rs 400.67: Rs 100-132. Fine as a range | arithmetic |
| "No captive-gas tariff for Oct 2026 found" | Not researched this run; note that the captive gas levy is the reason the ICP intervenors cited for industry shifting to grid [P, SRO 1501 para 6] | - |

## 15. Uncertainties the owner must confirm from real bills (ranked by effect on the savings report)

1. **Electricity duty base and rate for the site's province** (Punjab/KP/Balochistan rates not found in a primary source; KE's are). Ask for a current B-2/B-3 bill and read the ED line against the energy line.
2. **The current Rs/kWh surcharge line(s)**: name as printed (FC surcharge / PHL / DSS), its rate (3.23?), and whether an NJ surcharge line (0.10) still appears.
3. **GST base**: does GST apply to fixed charges, ED and the PTV fee? Is the export credit subtracted before or after GST and income tax on prosumer bills?
4. **MDI practice**: 30-minute fixed blocks, all hours; whether the DISCO bills 25% of sanctioned load as the floor (Feb 2026 schedule) or still 50%; whether MDI is read from the meter or estimated.
5. **The export rate printed on a new net-billing bill** (Rs 8.13 vs "Rs 10-11"), and the settlement practice (credit carried vs quarterly payment, which DISCOs historically did not pay).
6. **Legacy net-metering netting**: by period or in total, and the rate used for surplus (off-peak rate of the consumer's category?), plus the agreement end date.
7. **ICP**: whether the site is enrolled; the DISCO's computed monthly baseline (peak and off-peak) for the reference months; whether the DISCO applies the 2.8% escalation; the review outcome when notified.
8. **Peak window actually printed on the bill** for the site's DISCO (should match section 2) and any DISCO-local variation after NEPRA's localisation study.
9. **Billing period dates** (meter-reading cycle) and any proration.
10. **FCA/QTA lines**: which months' FCA appear on which bill (DISCOs sometimes bundle), to calibrate the m+2 assumption.
11. **Income tax, further tax, extra tax** actually charged (depends on the company's STRN/ATL status and exemption certificates).
12. **K-Electric sites**: MUCT line, Sindh ED rates, and whether KE prints "PHL surcharge" at 3.23.
13. **Diesel delivered price** at site versus OGRA ex-depot (transport, dealer margin, shrinkage) and the Punjab 4-paisa self-generation duty for sites above 500 kVA.
14. **Genset fuel curve** from the data sheet (25/50/75/100% points) rather than HOMER's example.

## 16. Sources (retrieved 6 Nov 2026)

Primary (read directly, scanned PDFs rendered page by page):
- NEPRA decision TRF-100/2935-57, 11 Feb 2026, "Rationalization of tariff of XWDISCOs and K-Electric" (51 pp): https://nepra.org.pk/tariff/Tariff/Ex-WAPDA%20DISCOS/2026/TRF-100%20XWDISCOS%20AND%20KE%20RATIONALIZATION%20OF%20TARIFF%2011-02-2026%202935-57.pdf
- NEPRA SRO 1501(I)/2026, 7 Sep 2026, QTA Q2 CY2026 (14 pp): https://nepra.org.pk/tariff/Tariff/Notifications/2026/09%20Sep/SRO%201501(I)-2026%20Dated%2007-09-2026.pdf
- NEPRA SRO 2409(I)/2025, 10 Dec 2025, and decision of 9 Dec 2025 on the Incremental Consumption Package (8 pp read): https://ke.com.pk/wp-content/uploads/2025/12/SRO-2409I-2025-Dated-10-12-2025.pdf (mirror of https://nepra.org.pk/tariff/Tariff/Notifications/2025/12%20Dec/SRO%202409(I)-2025%20Dated%2010-12-2025.pdf)
- NEPRA (Prosumer) Regulations 2026, SRO 251(I)/2026, 9 Feb 2026 (17 pp): https://nepra.org.pk/Legislation/3-Reg/3.35%20NEPRA%20%20Prosumer%20Regulations%202026/NEPRA%20Prosumer%20Regulations%20(SRO%20251(I)2026)%2009-02-26.PDF
- SRO 547(I)/2026, 2 Apr 2026, amendment of regulation 21(2): https://nepra.org.pk/Legislation/3-Reg/3.35%20NEPRA%20%20Prosumer%20Regulations%202026/S.R.O%20547(1)%202026%20notification%20amendment%20in%20NEPRA%20(Prosumer)%20Regulations%2002-04-2026.PDF
- SRO 1330(I)/2026, 6 Aug 2026 (text via search): https://nepra.org.pk/Legislation/3-Reg/3.35%20NEPRA%20%20Prosumer%20Regulations%202026/SRO%201330(I)-2026%20Dated%2006-08-2026.pdf ; SRO 709(I)/2026, 28 Apr 2026 (fee), listed at https://nepra.org.pk/Legal.php
- NEPRA draft amendment (16 Feb 2026) of regulation 21: https://www.nepra.org.pk/Admission%20Notices/2026/02%20Feb/Notification_Amendment_Prosumer%20Regs-Final.pdf
- NEPRA LESCO interim tariff determination FY2023-24, 14 Jul 2023, Annex-I (FPA), I-A (QTA), V (Terms and Conditions: TOU hours, maximum demand, billing demand, PF): https://nepra.org.pk/tariff/Tariff/DISCOs/LESCO/2023/TRF-603%20&TRF%20604%20LESCO%20FY%202023-24%2014-07-2023%2018255-61.PDF
- IESCO tariff guide (SRO 279(I)/2026 schedule): https://www.iesco.com.pk/tariff-guide
- K-Electric tariff structure (peak hours, Sindh ED, s.235 slabs): https://ke.com.pk/?p=830 ; K-Electric FAQs (fixed charges, MDI 30 min, net billing, GST 18%, MUCT, FCA rules): https://ke.com.pk/faqs/
- Power Division "Electricity Billing" (taxes list): https://power.gov.pk/Detail/YThmNWQ5OGQtZGY5NC00NDk0LTgzN2MtZThhZTEzMTY5NTg2
- HOMER Pro 3.15 docs, generator fuel curve intercept and slope: https://homerenergy.com/products/pro/docs/3.15/generator_fuel_curve_intercept_coefficient.html and .../generator_fuel_curve_slope.html

Secondary (press and third-party):
- Dawn 10 Feb 2026 (Prosumer regs explained): https://www.dawn.com/news/1972203 ; Dawn 17 Feb 2026 (industrial cut, Rs 8.13): https://www.dawn.com/news/1973828 ; Dawn 28 Apr 2026 (25 kW fee abolished): https://www.dawn.com/news/1995812 ; Business Recorder 17 Feb 2026: https://www.brecorder.com/news/40407655 ; Dawn 3 Aug 2025 (localised peak hours): https://www.dawn.com/news/1928370
- Tribune 6 Oct 2026 (ICP review hearing): https://tribune.com.pk/story/2633172 ; The Nation 30 Sep 2026 (hearing notice): https://www.nation.com.pk/30-Sep-2026/nepra-hold-crucial-hearing-review-pm-s-incremental-consumption-package-oct-5
- Dawn 5 Sep 2026 (July FCA 2.0581 + QTA 0.5194): https://www.dawn.com/news/2027627 ; Tribune 8 Oct 2026 (Aug FCA 1.11): https://tribune.com.pk/story/2633704 ; Tribune 17 Oct and 30 Oct 2026 (Sep FCA 2.5624 reserved): https://tribune.com.pk/story/2635319 and https://tribune.com.pk/story/2637648 ; Business Recorder (Q1 CY2026 QTA -1.9857): https://www.brecorder.com/news/40424626
- Profit 15 Jan 2026 (Rs 3.23 surcharge to stay): https://profit.pakistantoday.com.pk/2026/01/15/rs3-23-per-unit-electricity-debt-surcharge-to-remain-for-up-to-six-years/ ; Dawn 1 Apr 2023 (surcharge structure): https://www.dawn.com/news/1745294 ; The Nation July 2026 column on NJ surcharge: https://www.thenews.pk/print/1426454-pay-again-or-let-the-river-come-home
- ProPakistani 4 Nov 2025 (Punjab 4 paisa duty): https://propakistani.pk/2025/11/04/govt-of-punjab-approves-new-electricity-duty-on-users/ ; ProPakistani 1 Jun 2026 (Electricity Duty Rules 2026): https://propakistani.pk/2026/06/01/punjab-reportedly-approves-tax-on-generators-and-solar-systems/
- PKRevenue (s.235 TY2027 card): https://pkrevenue.com/electricity-withholding-tax-rates-ty2027/ ; toolsfluent NEPRA tariff guide (secondary, taxes): https://www.toolsfluent.com/blog/nepra-tariff-2026-all-discos
- Diesel: ARY 29 Oct 2026: https://arynews.tv/petrol-price-in-pakistan-for-october-30-2026 ; APP 2 Nov 2026: https://www.app.com.pk/?p=1302857 ; Dawn 5 Nov 2026: https://www.dawn.com/news/2042367 ; Business Recorder 3-6 Nov 2026: https://www.brecorder.com/news/40447152 , 40447306, 40447490, 40447603
- CY2027: Pakera (hearing schedule 13-15 Oct 2026): https://pakera.pk/nepra-2027-tariff-hearings-power-companies/ ; Dawn 12 Oct 2026 (Rs 268 bn PYA): https://www.dawn.com/news/2036506 ; Geo (CPPA-G 2027 PPP Rs 27.309): https://www.geo.tv/latest/687399-power-purchase-price-may-rise-to-rs273-per-unit-in-2027-cppa-g
- ProPakistani 2 Aug 2026 (TOU net-billing proposal): https://propakistani.pk/2026/08/02/solar-owners-could-earn-rs-22-unit-for-sharing-electricity-at-night/ ; Soch Fact Check 25 Feb 2023 (KE peak hours): https://www.sochfactcheck.com/k-electric-karachi-has-not-increased-per-unit-electricity-prices-tariff-during-peak-hours/

Not reachable this run: hazeco.com.pk/services/tariff (404), ke.com.pk/?p=330 (404), tribune.com.pk/story/2575772 (403), geo.tv article (403), ogra.org.pk (not fetched), NEPRA Consumer Service Manual 2025 (scanned, 4.4 MB, not rendered), the 2015 Net Metering Regulations (not fetched).

## 17. Recommendations for the build plan (tariff engine and verification)

1. Seed the engine from the primary objects in section 11 and ship a "tariff provenance" view; do not hard-code any rate.
2. Implement the TOU calendar as data with the two calendars (XWDISCO, KE) and the DST hook; unit-test the Nov/Dec and Feb/Mar boundaries and the midnight-crossing 7-11 PM window.
3. Implement MDI as 30-minute fixed blocks over all hours with the 25%/50% floor as data; expose interval and block mode as site settings pending bill confirmation.
4. Model the ICP as a first-class price regime: baseline by month and period, incremental pricing at 22.98 + positive FCA, and the review-trigger risk; the optimiser must see the correct marginal price per period under the ICP.
5. Treat FCA and QTA as billing-month adders with exemption sets; build the m+2 lag into the "as known" versus "as billed" distinction.
6. Model surcharges and taxes as a configurable stack (section 7 table) with per-site overrides captured from bills; the first deliverable of onboarding should be a bill-parsing checklist that fills section 15.
7. Net billing: export at a flat NAEPP (8.13) with quarterly revision, optional TOU export schedule for the proposed night-export regime; legacy net metering with by-period netting as an option; carry-forward balances per site.
8. Fuel: a daily price table (OGRA) and a per-genset linear fuel curve; add the Punjab self-generation duty for >500 kVA sites.
9. Watchers: nepra.org.pk Tariff/Notifications, Legal (Prosumer amendments), ke.com.pk tariff page, OGRA press releases; a human approves each new version. Plan for the 1 Jan 2027 schedule change (possibly different fixed charges) before go-live.
10. Savings verification: compute bills line by line for actual and counterfactual series under identical versions; report fixed-charge (MDI), period-shift, ICP-crossing, export-credit and fuel lines separately; reconcile against the real bill and record residuals as overrides.
