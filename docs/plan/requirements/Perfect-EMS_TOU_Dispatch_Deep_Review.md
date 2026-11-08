# Product 1 — TOU-Aware Energy Management System (EMS) for Pakistan: Research Report

*Prepared for OnyxLeaf (Saad), 30 October 2026. Scope: Product 1 only (EMS with TOU-aware dispatch). Product 2 (vendor-agnostic inverter controller) is treated as a supplied interface, not researched here.*

---

## Scope

This report covers the EMS/optimisation layer only: the software that decides *when* energy should flow *from which source*, and proves in rupees that those decisions beat what the hardware would have done on its own. Reading of the meeting notes: Enerlyticslab is the local reference point and is believed to "only observe and dispatch power"; OnyxLeaf's product should reproduce observation/dispatch **and** add Time-of-Use-aware optimisation — timing and source selection across PV, battery (BESS), grid and genset — with demand from off-grid and remote sites, and "saving money" as the niche. ML/AI (forecasting + optimisation) is the intended differentiator, consistent with OnyxLeaf's "predict and optimise" pattern.

**In scope:**
- Decision engine: TOU-aware dispatch plans (charge/discharge windows, grid-charge decisions, genset start/stop recommendations, load-shedding reserve).
- Forecasting: site-level PV forecast, load forecast, tariff calendar, and outage-probability modelling for Pakistan conditions.
- Savings measurement and reporting in PKR against a defensible baseline.
- C&I sites, remote/off-grid sites (telecom towers, tube wells), and premium residential as a secondary segment.

**Out of scope (Product 2's job):** protocol drivers, inverter/BMS/meter integration, vendor quirks, local comms. This report assumes Product 2 supplies **normalised telemetry** (PV power, load, SOC, grid status, genset status, at ≥1-min resolution) and **writable setpoints** (charge/discharge power or mode, grid-charge enable, min-SOC, genset start/stop where wired). Every optimisation feature below must degrade gracefully when only a subset of setpoints is writable — on many sites the first deliverable will be "recommendations + monitoring," with closed-loop control following where Product 2 has write access.

Also out of scope: wholesale-market trading/VPP aggregation (no retail price market for prosumers in Pakistan today), utility-scale SCADA, and hardware manufacture.

---

## Refined problem statement

Pakistani commercial, industrial and remote-site operators who have installed solar + batteries + gensets are leaving money on the table because their inverters run naive, static self-consumption rules, while the economics just shifted sharply in favour of *smart timing*: NEPRA's February 2026 Prosumer Regulations cut the export credit to the national average energy purchase price (estimated Rs 8–11/unit) while grid import costs exceed Rs 50/unit for many consumers ([The News, Feb 2026](https://www.thenews.pk/print/1398311-govt-ends-net-metering-shifts-rooftop-solar-to-net-billing), [toolsfluent guide](https://www.toolsfluent.com/blog/net-metering-pakistan-complete-guide)), TOU tariffs price evening peak above off-peak (e.g. Rs 46.85 vs Rs 40.53/kWh residential TOU at HAZECO — [HAZECO tariff page](https://hazeco.com.pk/services/tariff)), BESS imports grew 220% to 4.6 GWh in 2025 ([ESS News](https://www.ess-news.com/2026/07/15/pakistan-imports-4-6-gwh-of-bess-in-2025/)), and over 2,000 of 12,665 feeders see 10+ hours of daily load shedding ([The News, Feb 2026](https://www.thenews.pk/print/1398879-more-than-2-000-feeders-face-over-10-hours-of-daily-loadshedding-na-told)). The buyer is the facility owner/energy manager (C&I), the towerco/telco energy team (remote sites), or the installer/O&M firm that white-labels the product; the pain is measurable: wasted battery cycles, gensets running when stored solar could have covered the load, batteries empty at outage onset, and no PKR-denominated proof of savings. The falsifiable core claim: **forecast-driven dispatch can beat the inverter's built-in self-consumption/TOU mode by a margin that exceeds the product's fee** — published evidence puts forecast-based control's uplift over rule-based control at ~13% under ideal assumptions but shows it can shrink by ~69% or even reverse under realistic net-billing accounting ([arXiv 2510.25373, Oct 2025](https://arxiv.org/html/2510.25373v1)) — so the pilot plan below is designed to measure exactly this increment on real Pakistani sites, and to kill the product if it is too small.

---

## Market and demand

The demand signals are strong and recent; the monetisable increment per site is the open question.

| Signal | Figure | Date | Source |
|---|---|---|---|
| Net metering → net billing | Export credited at NAEPP (~Rs 11 proposed; estimates Rs 8.13–11) vs import tariffs up to Rs 50+/unit; 5-yr contracts; quarterly credit settlement | Notified 9 Feb 2026 (SRO 251(I)/2026) | [The News](https://www.thenews.pk/print/1398311-govt-ends-net-metering-shifts-rooftop-solar-to-net-billing), [Tribune](https://tribune.com.pk/story/2591628/1), [toolsfluent](https://www.toolsfluent.com/blog/net-metering-pakistan-complete-guide) |
| Approval simplification ≤25 kW | DISCO approval without NEPRA concurrence (SRO 1330(I)/2026) | 6 Aug 2026 | [toolsfluent](https://www.toolsfluent.com/blog/net-metering-pakistan-complete-guide) |
| Buyback rate under review | PM directed Power Division & NEPRA to review/verify buyback tariff | 22 Oct 2026 | [The News](https://www.thenews.pk/print/1398311-govt-ends-net-metering-shifts-rooftop-solar-to-net-billing) |
| BESS imports | 4.6 GWh in 2025, +220% YoY; 7.6 GWh cumulative since 2018 (residential 3.1, industrial 2.0, commercial 1.0, telecom 1.4 GWh); surge continued H1 2026 | Jul 2026 report | [ESS News / Renewables First](https://www.ess-news.com/2026/07/15/pakistan-imports-4-6-gwh-of-bess-in-2025/) |
| TOU tariff spread (residential example) | HAZECO A-1 ≥5 kW: peak Rs 46.85, off-peak Rs 40.53/kWh; peak 6–10 PM (SRO-1168/2025) | Current schedule | [HAZECO](https://hazeco.com.pk/services/tariff) |
| Peak window policy | NEPRA directed DISCOs to end peak hours at 11 PM; peak currently ~7–11 PM; rooftop solar eroding peak demand | Aug 2025 | [Daily Pakistan](https://en.dailypakistan.com.pk/21-Aug-2025/peak-electricity-hours-cut-to-11pm-to-curb-losses-in-power-sector) |
| Industrial TOU (new, optional) | Power Division concluded 3 nationwide consultations on a multi-slab, opt-in industrial TOU tariff; AMI flagged as prerequisite | 1 Apr 2026 | [The News](https://www.thenews.pk/print/1407397-pakistan-wraps-tou-tariff-talks-keeps-regime-optional-for-industry) |
| Load shedding | 2,000+ of 12,665 feeders face >10 h/day outages (QESCO 604/814, PESCO 642/1,376, SEPCO 407/707…) | Feb 2026, NA Question-Hour | [The News](https://www.thenews.pk/print/1398879-more-than-2-000-feeders-face-over-10-hours-of-daily-loadshedding-na-told) |
| Telecom towers | 55,000+ towers; up to 12 h/day load shedding; batteries last 1–2 h before gensets; 9,200+ diesel-theft incidents in 11 months; ~20,000 towers reported fuel-related outages | Jul 2026, Senate/PTA | [PhoneWorld](https://www.phoneworld.com.pk/pakistan-telecom-tower-diesel-theft-9200-incidents-senate-pta-load-shedding/) |
| Tower solar+BESS financing | Rs 3 bn (~$10.8 m) AAA-rated green sukuk by Infralectric (Brillanz Group) with InfraZamin for AI-enabled telecom-tower energy; linked 25 MWh ESaaS project targets >5 m litres diesel/yr and ~13,500 t CO₂/yr savings | Launched Sep 2025 (bank partnership announcements Apr 2026) | [Arab News PK](https://www.arabnews.pk/pakistan/pakistan-launches-first-green-sukuk-to-cut-telecom-emissions-modernize-energy-use-2616788) |
| Diesel genset cost | ~2.5–3 L/h at 5–6 kW on a 10 kVA genset at PKR 280/L ⇒ roughly Rs 117–168/kWh fuel-only (vendor figures, promotional source) | Aug 2026 | [Maxpower](https://maxpower.com.pk/news/diesel-generator-vs-solar-system/) |

**What net billing does to kWh value:** under the 2015 regime an exported unit offset an imported unit 1:1; now an exported unit earns ~Rs 8–11 while an imported unit costs Rs 40–60+. Every kWh a controller can shift from "export cheap, re-import dear" to "store and self-consume at peak" is worth roughly **Rs 30–50**. That spread is the product's raw material — and it is why batteries are booming and why the analyst report explicitly names net-billing reform as a driver of storage adoption ([ESS News](https://www.ess-news.com/2026/07/15/pakistan-imports-4-6-gwh-of-bess-in-2025/)).

**Aug–Oct 2026 policy watch (asked for explicitly):** the ≤25 kW approval simplification (SRO 1330(I)/2026, 6 Aug 2026) and the PM's 22 Oct 2026 directive to review the buyback tariff are the two material changes found ([toolsfluent](https://www.toolsfluent.com/blog/net-metering-pakistan-complete-guide), [The News](https://www.thenews.pk/print/1398311-govt-ends-net-metering-shifts-rooftop-solar-to-net-billing)). The buyback review cuts both ways: a higher buyback would narrow the store-vs-export spread; the uncertainty itself argues for a controller that re-optimises when tariffs change. A World Bank role in the industrial TOU work could **not** be verified — the article found credits the Power Division only ([The News](https://www.thenews.pk/print/1407397-pakistan-wraps-tou-tariff-talks-keeps-regime-optional-for-industry)); treat the "World Bank-backed" framing from the meeting notes as unverified.

---

## Enerlyticslab

Everything findable points to a small, young Karachi company doing white-label **monitoring** — consistent with the meeting-note characterisation "only observes and dispatches," with the "dispatch" part unconfirmed in public materials.

**What was found (all opened pages):**
- **Website:** [enerlyticslab.com](https://enerlyticslab.com/) serves an essentially empty JS shell — title "Enerlytics Lab" plus SEO meta keywords ("energy monitoring software, energy management software, energy dashboard, carbon accounting, carbon credit trading…") and no body content; subpages (/products, /about, /contact) return the same shell. No pricing, team, or customer pages are publicly rendered.
- **Legal entity & founder signal:** the mobile apps are published by **ENERLYTICS TECH LAB (PVT) LTD**, Karachi; the iOS app's copyright line reads "© 2025 **Samir Ahmed**" ([Apple App Store](https://apps.apple.com/us/app/enerlytics-pro/id6755328009), [Google Play](https://play.google.com/store/apps/details?id=com.app.enerlyticsrevamp&hl=en_CA)). This is almost certainly the "founder Sameer" from the meeting notes, but no LinkedIn profile could be opened to confirm (the [company LinkedIn page](https://www.linkedin.com/company/enerlyticslab) is robots-blocked to fetching).
- **Product, as evidenced by the apps:** "Enerlytics" / "Enerlytics Pro" are dashboard apps — "real-time data visualization, performance tracking, and business insights," interactive dashboards, smart notifications, trend analysis, report downloads, multi-site selection. **No control or dispatch features are listed.** Version cadence is fast: v1.0 on 19 Nov 2025 through v12.0 on 28 Sep 2026 ([Apple](https://apps.apple.com/us/app/enerlytics-pro/id6755328009)). Android shows only **10+ downloads** ([Google Play](https://play.google.com/store/apps/details?id=com.app.enerlyticsrevamp&hl=en_CA)) — tiny public footprint, consistent with B2B web-first use.
- **Customers:** a subdomain **wateen.enerlyticslab.com** titled "AI Energy Asset Management Platform" surfaced in search (the page itself is fetch-blocked), implying a white-label deployment for **Wateen** — the Wateen Telecom subsidiary whose [energy-efficiency page](https://wateen.com/energy-efficiency/) advertises EMS platforms "to evaluate, control, and optimize energy supply and demand" with real-time alerts into Wateen's NOC and O&M dispatch. If Enerlyticslab powers that, their "dispatch" is plausibly *O&M ticket/alert dispatch* and/or basic remote control through Wateen's NOC — not optimisation.
- **Solar Pakistan 2026:** the exhibition itself is well documented (350+ companies, Karachi — [The News](https://www.thenews.pk/print/1441982-solar-pakistan-2026-opens-in-karachi-with-over-350-companies)), but no exhibitor listing naming Enerlyticslab could be found; their attendance stands as **unverified** (meeting-notes claim).
- **Tech stack hints:** JS single-page app on the web, native/hybrid mobile apps, multi-tenant subdomain-per-customer pattern. Nothing deeper is visible.

**What "observe and dispatch only" means concretely:** on the evidence, Enerlyticslab ingests telemetry from sites, renders dashboards/KPIs/alerts, and (at most) relays manual or threshold-triggered commands and O&M dispatch. There is no public evidence of tariff models, forecasting, or an optimiser. That is precisely the layer OnyxLeaf plans to add.

**Partner-or-competitor call: treat as a probable competitor, approach as a possible channel partner — but do not build the plan around them.** Reasoning: (a) they are upstream of OnyxLeaf's optimiser and lack it today, so a "we add the brain to your eyes" partnership is technically natural; (b) but their SEO keywords ("energy management software") and their "AI Energy Asset Management Platform" branding for Wateen show they *intend* to claim the same ground, and an optimisation layer is an obvious roadmap item for them; (c) they already own the relationship with at least one large distribution partner (Wateen) and sit in Karachi's solar ecosystem. The cheap move: one exploratory conversation (the founder is reachable via the meeting-notes relationship) to test whether they would expose telemetry/control APIs; meanwhile design OnyxLeaf's product so Enerlyticslab is just one more telemetry source behind Product 2's normalised interface. The "Enerlytics ambiguity" is listed under Risks.

---

## Commercial landscape

No one found sells a Pakistan-first, mixed-brand, TOU/outage-aware dispatch optimiser for C&I and remote sites; global tools are either tied to their own hardware, aimed at European dynamic-tariff homes, or priced/positioned for utility-scale.

| Player | What it does | Customers | Pricing (found) | Tied to own HW? | Gap for OnyxLeaf |
|---|---|---|---|---|---|
| [Enerlyticslab](https://enerlyticslab.com/) (PK) | Monitoring dashboards, alerts, multi-site apps; white-label ([Play](https://play.google.com/store/apps/details?id=com.app.enerlyticsrevamp&hl=en_CA)) | Wateen (apparent, via subdomain); B2B | Not public | No (software) | No optimiser, no tariff/forecast engine |
| Other PK monitoring firms ([ensun listing](https://ensun.io/search/energy-management/pakistan)): TEAMpk, Enernatural, eTech Control, ICSS Engineering, DEWMAK | Metering/monitoring, audits; ICSS makes solar/grid/genset changeover controllers | Local C&I | Not public | Mostly services/HW | None do forecast-driven TOU dispatch |
| [Elum Energy](https://elum-energy.com/) (FR) | ePowerControl plant controller + ePowerMonitor platform; solar-diesel & solar-storage integration, zero-export, microgrids | C&I, EPCs, IPPs, off-grid hybrid | Quote-only | Own controller HW required | Closest functional analogue; not PK-tariff-aware, hardware sale + integrator-led, no rupee savings reporting |
| ComAp (CZ) | Genset & hybrid microgrid controllers | Genset OEMs/integrators | — | Own HW | **Page could not be opened; not verified this cycle** |
| [gridX](https://www.gridx.ai/) (DE) | XENON white-label HEMS/EVCI platform; 55+ OEMs, 500+ devices; dynamic-tariff optimisation | European utilities, energy retailers, CPOs; 12 countries | Not public | gridBox gateway (own edge HW) | Europe-only compliance focus; no genset/outage logic; validates white-label model |
| [Stem Athena](https://www.stem.com/athena/) (US) | "Enterprise energy optimization platform," AI-driven storage services | Enterprise/C&I, FTM (page is thin) | Not public | HW-agnostic services | US markets/demand charges; heavy enterprise sales motion |
| FlexGen (US) | Utility-scale BESS EMS (HybridOS) | Utility-scale | — | — | **Not opened/verified this cycle**; wrong scale anyway |
| [Fluence](https://fluenceenergy.com/fluence-iq-digital-platform/) | Mosaic (bidding, 13.3+ GW) and Nispera (APM, 15.5+ GW) | Utility-scale asset owners/operators | Not public | Platform sold with/around Fluence storage | Wholesale-market scale; irrelevant to behind-the-meter PK |
| [Victron Dynamic ESS](https://www.victronenergy.com/live/drafts:dynamic_ess) | Cloud-scheduled battery charge/discharge on price + PV forecast + consumption patterns; green/trade modes; runs via VRM + GX device; reverts to plain ESS after 12 h offline | Victron owners (EU dynamic tariffs) | Free with Victron | **Yes** (Victron only) | Blueprint for the optimisation UX, but single-brand, no genset, needs day-ahead price APIs that don't exist in PK |
| [Tesla Powerwall Savings mode](https://www.tesla.com/support/energy/powerwall/mobile-app/time-based-control) | TOU load-shifting: charge when cheap, discharge at peak, per utility rate plan | Powerwall homes | Bundled | **Yes** | Residential, own HW, no PK presence |
| [Huawei FusionSolar](https://solar.huawei.com/en/professionals/all-products/fusionsolar-smart-pv-management-system) | Multi-plant monitoring, O&M ticket dispatching, energy-flow views; architecture built on Huawei SmartLogger/inverters | Plant operators, O&M | Not public | **Effectively yes** | Monitoring-centric; TOU battery optimisation not evidenced on page; brand-locked |
| Sungrow iSolarCloud / EMS300CP | Monitoring + plant EMS; hybrid inverters have built-in TOU windows | Sungrow fleet | — | **Yes** | **EMS product page could not be opened; treat details as unverified**. Built-in modes are static (see Economics) |
| SMA, sonnen, Enphase, SolarEdge ONE | Vendor energy-management suites; Enphase IQ/sonnen/SolarEdge do TOU self-consumption in home ecosystems | Own-brand fleets | — | **Yes** | **Pages not opened/failed this cycle**; all brand-locked, none PK-aware |
| [SwitchDin](https://www.switchdin.com/) (AU) | Vendor-agnostic DER orchestration ("any asset, any manufacturer, any operator," 2,000+ OEM devices); edge control; tariff/wholesale/peak-shaving optimisation | Utilities, isolated grids, C&I, aggregators (Australia-centric) | Not public | Edge SW on gateways | The closest philosophical peer; Australia-regulated markets, enterprise pricing; PK is open field |
| [Hybrid Greentech](https://hybridgreentech.com/) (DK) | VPP + balance-responsible-party trading of batteries/flex; in-house trading & control platform | Nordic asset owners | Not public | No | Wholesale trading focus; irrelevant to PK retail tariffs |
| [SolarAssistant](https://solar-assistant.io/shop) | Local monitoring (Raspberry Pi) for Growatt/Voltronic/Luxpower/Deye-class inverters; closed source | Residential/prosumer, strong in load-shedding markets | **US$59 software**; $179–229 with Pi | No (multi-brand reader) | **The residential price anchor.** Monitoring + settings, not optimisation |

Two structural observations. First, almost every polished TOU-optimisation product (Victron, Tesla, Enphase, sonnen, Huawei, Sungrow) is **hardware-locked** — which is precisely why Product 2's vendor-agnostic control layer plus this EMS is a coherent wedge in Pakistan's mixed Deye/Growatt/Sungrow/Huawei installed base. Second, the hardware-agnostic players (SwitchDin, gridX, Stem) are structured around **regulated flexibility markets** (CSIP-AUS, EU dynamic tariffs, US demand charges) that don't exist in Pakistan; their economics don't transplant, but their architectures (edge gateway + cloud optimiser + white-label apps) do.

---

## Open-source codebases

The optimisation problem is already solved in open source several times over; the defensible work is Pakistan-specific forecasting, genset/outage logic, and savings accounting. Figures below are as shown on pages opened 30 Oct 2026; where a page did not show a number, that is stated rather than guessed.

| Repo | License | Language | Popularity/activity (as shown) | One-liner |
|---|---|---|---|---|
| [EMHASS](https://github.com/davidusb-geek/emhass) | MIT | Python | 668★, 162 forks, 3,125 commits | LP/MPC home energy optimiser for Home Assistant |
| [Predbat](https://github.com/springfall2008/batpred) | **Custom: personal use only** | Python (inferred) | 329★, 150 forks, 3,783 commits | HA battery prediction/charging planner, many inverters |
| [OpenEMS](https://github.com/OpenEMS/openems) | EPL-2.0 (Edge/Backend), AGPL-3.0 (UI) | Java (inferred) | 1.6k★, 680 forks, 6,722 commits | Industrial-grade Edge/Backend/UI EMS (FENECON) |
| [evcc](https://github.com/evcc-io/evcc) | MIT + paid sponsor components | Go | 7.5k★, 10,236 commits | EV charging + home EMS, huge device/tariff template library |
| [quartz-solar-forecast](https://github.com/openclimatefix/open-source-quartz-solar-forecast) | MIT | Python | 158★, 526 commits | Site-level ML PV forecast, 0–48 h |
| [pvlib-python](https://github.com/pvlib/pvlib-python) | BSD-3-Clause | Python | ~1.7k★, ~1,965 commits | Reference PV performance/irradiance modelling |
| [MyEMS](https://github.com/MyEMS/myems) | On page: license file linked, type not shown | Python/React/Angular | 733★, 6,030 commits, v6.9.0 | ISO 50001 metering/monitoring EMS; optimisation enterprise-only |
| [OpenLEMS](https://github.com/OpenLoaf/OpenLEMS) | AGPL-3.0 | Go (+C++ prediction) | 52★, 690 commits | Edge EMS for BESS/microgrids with TOU peak-shaving/arbitrage |
| [pymgrid](https://github.com/topics/microgrid) | (listed via topic page) | Python | 217★ | Microgrid simulation — useful for strategy backtesting |

**EMHASS** ([repo](https://github.com/davidusb-geek/emhass), [docs](https://emhass.readthedocs.io/en/latest/)) — MIT, Python. Formulates day-ahead/receding-horizon scheduling as a linear program over electricity prices, PV forecast and battery; handles controllable loads, with thermal models that treat water heaters/heat pumps as thermal storage; recent versions run on CVXPY with the **HiGHS** solver (~0.1 s per iteration per the docs); load forecasting uses scikit-learn/skforecast; deploys as a HA add-on or standalone Docker. **Reuse:** the single best starting point for OnyxLeaf's optimiser core — the LP formulation (cost-minimisation over a horizon with battery SOC dynamics, import/export price asymmetry, deferrable loads) maps directly onto net-billing Pakistan, and MIT allows closed-source commercial reuse. **Gaps:** single-home scale and Home Assistant-shaped I/O; no genset model, no outage/reserve constraint, no multi-tenant cloud, no degradation-aware cycling in the core; stars/commits shown but release dates were not visible on the fetched page.

**Predbat** ([repo](https://github.com/springfall2008/batpred)) — 329★, 3,783 commits, HACS-distributed, supports GivEnergy/Solis/Huawei/SolarEdge/FoxESS/Sofar/Tesla and more, with a paid "PredBat Cloud." **Critical caveat:** the license is *not* open source — copyright Trefor Southwell, "may be used at no cost for **personal use only**." **Reuse:** none of the code commercially; but it is the best *product spec* in existence for battery planning UX (plan tables, 5-/30-min slots, tariff integrations, per-inverter quirk handling) and proof that users pay for a hosted version of a free tool. **Gaps for us:** UK-tariff-centric (Octopus), HA-dependent, and legally unusable as code.

**OpenEMS** ([repo](https://github.com/OpenEMS/openems)) — 1.6k★, 6,722 commits on `develop`. Three-part architecture: **Edge** (on-site, device comms + control algorithms), **Backend** (cloud aggregation/monitoring of fleets), **UI**. Dozens of controllers including `controller.ess.timeofusetariff` and `controller.ess.gridoptimizedcharge`; bridges for Modbus/M-Bus/MQTT; batteries (BYD, Pylontech, FENECON…) and battery inverters supported. Backed by the OpenEMS Association; it is the stack behind FENECON's commercial FEMS. **Licensing nuance that matters:** Edge/Backend are **EPL-2.0** (weak copyleft — commercial closed extensions are workable with care), the UI is **AGPL-3.0** (viral for SaaS). **Reuse:** the Edge/Backend split and controller-priority architecture are the right mental model for Product 1+2 together; conceivably adopt Edge outright if the team accepts Java/OSGi. **Gaps:** heavyweight for a Pakistani startup's velocity; European device catalogue; TOU controller details not shown on the fetched page.

**evcc** ([repo](https://github.com/evcc-io/evcc)) — 7.5k★, MIT with excluded sponsor-token components, Go, 10,236 commits. EV-charging-centric home EMS with an exceptional **YAML device-template system** (meters, inverters, batteries across SunSpec/Modbus and dozens of brands) and a `tariff` subsystem. **Reuse:** the template approach is the pattern Product 2 should copy for mixed-brand Pakistan (Deye/Growatt templates exist in its ecosystem); Go edge binary is a good fit for cheap gateways. **Gaps:** EV-first orientation, no genset, EU tariff APIs.

**Home Assistant energy stack** (context for the two above): HA provides the integration layer (Solcast/forecast.solar integrations, inverter integrations, energy dashboard) that EMHASS and Predbat ride on. **Reuse:** fastest possible pilot rig — HA + EMHASS on one site answers "how big is the increment?" in weeks without writing a platform. **Gap:** not a multi-tenant commercial product; fine for pilots, wrong for scale.

**Forecasting options:**
- [quartz-solar-forecast](https://github.com/openclimatefix/open-source-quartz-solar-forecast) (Open Climate Fix): MIT, 158★; gradient-boosting on NWP (GFS/ICON/ECMWF, 9 weather variables), trained on 25,000 UK sites; MAE ~13% of capacity excluding nighttime over 0–36 h; README explicitly warns it was trained/tested only in the UK. **Reuse:** architecture and feature set, not the weights — retrain on Pakistani telemetry (dust/haze/smog regimes differ sharply from the UK).
- [forecast.solar](https://forecast.solar/): free keyless public API (today+1 day, hourly), paid tiers €16–200/yr up to 15-min resolution, built on PVGIS + weather services. Cheap pilot-grade input; API was flagged "temporarily out of service for maintenance" when checked.
- [Solcast](https://solcast.com/pricing): commercial irradiance/PV forecasts, live/forecast −7 d to +14 d, 5-min updates on top tiers; all commercial plans quote-based; the fetched pricing page shows only limited free trials (a hobbyist tier was not visible on it).
- [Open-Meteo](https://open-meteo.com/): free for non-commercial use up to 10,000 calls/day (CC BY 4.0), 30+ NWP models (ECMWF, ICON, GFS…), 1–11 km resolution; commercial use needs a paid plan. The right raw-NWP feed for a self-trained PK model.
- [pvlib-python](https://github.com/pvlib/pvlib-python): BSD-3, ~1.7k★, the standard physics toolbox (from Sandia's PVLIB; NumFOCUS-affiliated) for clear-sky baselines, POA transposition and PV performance — the physical prior underneath an ML forecaster.

**Optimisation libraries:** EMHASS's own migration to **CVXPY + HiGHS** ([docs](https://emhass.readthedocs.io/en/latest/)) is the strongest signal: HiGHS (MIT-licensed solver) handles this problem class in ~0.1 s. PuLP (Python LP modeller) and Pyomo (general algebraic modelling, supports MILP/MPC workflows) are the alternatives; their GitHub metrics were not fetched this cycle (API access unavailable), so no star counts are quoted — all are permissively licensed and battle-tested, and any of the three removes solver risk entirely.

**Found via topic searches** ([energy-management-system](https://github.com/topics/energy-management-system), [microgrid](https://github.com/topics/microgrid)):
- **MyEMS** ([repo](https://github.com/MyEMS/myems)) — 733★, 6,030 commits, v6.9.0, monthly releases; Python API + Modbus TCP acquisition + React/Angular UIs; ISO 50001 metering/monitoring for buildings/factories. Optimisation/VPP/"AI optimization" are **enterprise-edition only**. Reuse: proof that open-core EMS monetises; possibly its acquisition/cleaning pipeline. Gap: it is the "observe" layer OnyxLeaf is trying to leapfrog, not the optimiser.
- **OpenLEMS** ([repo](https://github.com/OpenLoaf/OpenLEMS)) — AGPL-3.0, Go+C++, 52★, 690 commits; plugin-based **edge EMS for BESS/microgrids**: Modbus TCP/RTU, CANBus, GPIO drivers; PCS/BMS/meter/PV integration; **built-in TOU tariff config for peak shaving and arbitrage**; MQTT/Modbus export; web UI. The closest open codebase to OnyxLeaf's remote-site use case — but AGPL makes direct incorporation into closed SaaS poisonous; usable as a design reference or in an isolated, source-published edge component only.
- Smaller finds: pymgrid (217★, microgrid simulation — good for backtesting dispatch policies), microgrid-ems-drl (57★, DRL dispatch research), ol-ems (58★, online-learning MPC), vessim (88★, co-simulation). Research-grade, not products.
- **SolarAssistant** ([shop](https://solar-assistant.io/shop)) — closed source, **US$59** one-time software (SD image for your own Pi; $179/$229 with hardware), cable-level support for Growatt/Luxpower/Voltronic/Deye-class gear. This is the residential price anchor: Pakistani prosumers already pay ~Rs 16,500 one-time for *monitoring alone*.

---

## Build vs reuse

Take the LP formulation and forecasting scaffolding from MIT/BSD projects, copy architectures (not code) from the AGPL/proprietary ones, and build the Pakistan layer — which is most of the value — in-house.

**Take:**
- **Optimiser core:** EMHASS-style LP as the starting formulation (MIT — fork or re-implement freely), solved with **HiGHS** via CVXPY or PuLP. Day-one problem: 15-min slots over 24–48 h, variables = battery charge/discharge, grid import, genset energy; objective = PKR cost (TOU import price, NAEPP export price, genset Rs/kWh, battery throughput cost); constraints = SOC dynamics, inverter power limits, **reserve-SOC-before-predicted-outage**, genset min-run/min-off.
- **PV physics:** pvlib (BSD-3) for clear-sky/POA priors; **Quartz-style GBM** retrained on own telemetry with **Open-Meteo** NWP features (free tier for pilots, paid for production); forecast.solar as a zero-effort pilot feed.
- **Patterns, not code:** OpenEMS's Edge/Backend split and controller-priority chain; evcc's device-template YAML (for Product 2); Predbat's plan-visualisation UX; Victron Dynamic ESS's two-mode simplicity and its 12-h offline fallback behaviour.

**License implications (the AGPL warning, made explicit):**
- Safe for closed SaaS: EMHASS (MIT), evcc core (MIT), pvlib (BSD-3), quartz (MIT), HiGHS/PuLP/CVXPY (permissive).
- Caution: OpenEMS Edge/Backend are EPL-2.0 — usable commercially, but modifications to EPL files must be offered back; keep proprietary logic in separate bundles if adopted. OpenEMS **UI is AGPL** — do not reuse.
- Avoid incorporating: **OpenLEMS (AGPL-3.0)** — network use triggers source disclosure of the whole derived service; **Predbat — not open source at all** (personal use only); copying its code into a commercial product would be infringement. Study both, copy neither.

**What remains to build (the moat):**
1. **Pakistan forecasting:** PV models that learn dust/smog/monsoon soiling regimes (UK-trained weights explicitly unreliable here per [OCF's own README](https://github.com/openclimatefix/open-source-quartz-solar-forecast)); load forecasting per site; **outage forecasting** from each feeder's observed shedding pattern — no OSS project has this, and 2,000+ feeders with >10 h/day outages ([The News](https://www.thenews.pk/print/1398879-more-than-2-000-feeders-face-over-10-hours-of-daily-loadshedding-na-told)) make it the most distinctive input.
2. **Genset dispatch:** run-hour minimisation, fuel Rs/kWh modelling, start/stop advisory — absent from every home-EMS project; only Elum/ComAp-class controllers do it, in hardware.
3. **Tariff engine:** DISCO TOU calendars (e.g. [HAZECO](https://hazeco.com.pk/services/tariff)), seasonal/SRO updates, net-billing NAEPP settlement rules, MDI/fixed charges for the coming opt-in industrial TOU ([The News](https://www.thenews.pk/print/1407397-pakistan-wraps-tou-tariff-talks-keeps-regime-optional-for-industry)).
4. **Rupee reporting:** counterfactual baselines, verified-savings statements, diesel-litres-saved — the sales document. Nothing reusable exists.
5. Multi-tenant cloud, white-label installer portal, and the Product 2 interface contract.

---

## Product scope and methodology

A thin edge, a cloud optimiser, and an honest measurement loop — optimising on a 15-minute grid with a receding horizon.

**Architecture (edge vs cloud):**
- **Edge (via Product 2):** telemetry normalisation, local schedule execution, and a hard-coded safety fallback (revert to inverter self-consumption mode + conservative min-SOC) when the cloud is unreachable — mirroring Victron's documented revert-after-12-h pattern ([Dynamic ESS docs](https://www.victronenergy.com/live/drafts:dynamic_ess)), but with a much shorter threshold given Pakistani connectivity. The edge holds the latest 48-h plan so internet loss ≠ dumb behaviour.
- **Cloud:** forecasters (PV/load/outage), tariff engine, optimiser, fleet dashboards, savings reports, alerting. Re-optimise every 15 min (receding horizon) and on events (outage detected, genset started, forecast bust >X%).
- Rationale: optimisation is light (sub-second LP) but model training and fleet learning are cloud jobs; C&I buyers want portfolio dashboards anyway; and keeping the edge thin keeps Product 2's surface small.

**Optimizer choice:** **LP first, MILP only where needed, wrapped in receding-horizon MPC.** LP (HiGHS) covers battery/TOU/export economics exactly as EMHASS demonstrates ([docs](https://emhass.readthedocs.io/en/latest/)). Genset on/off and min-run constraints need binaries → a small MILP, still trivial for HiGHS at 96 slots. Full stochastic MPC or DRL (as in the research repos) is not justified until data shows forecast uncertainty is the binding constraint; instead handle uncertainty with reserve margins and frequent re-solves. This is also the honest reading of [arXiv 2510.25373](https://arxiv.org/html/2510.25373v1): sophistication beyond good forecasts + frequent re-solving buys little.

**Forecasting approach:**
- **PV:** pvlib clear-sky × learned attenuation as the day-zero model; graduate to a GBM on Open-Meteo NWP features fine-tuned per site after 4–6 weeks of telemetry; target the ~13%-of-capacity MAE that Quartz reports for mature models ([Quartz README](https://github.com/openclimatefix/open-source-quartz-solar-forecast)).
- **Load:** per-site skforecast-style ML with day-type/holiday features (EMHASS's approach, retrained locally).
- **Tariff:** deterministic calendar per DISCO + alerts on SRO changes (the Oct 2026 buyback review shows tariffs will keep moving).
- **Outage:** per-feeder empirical model (hazard by hour-of-day/season/announced schedules), updated from the fleet's own grid-status telemetry — every deployed site is a sensor.

**Battery-life-aware dispatch:** price throughput in the objective (PKR/kWh cycled ≈ battery replacement cost ÷ warranted kWh throughput — the arXiv study's €0.084/kWh method, re-based to Pakistani LiFePO₄ prices), cap daily cycles, respect temperature derating from BMS telemetry. This prevents the optimiser "winning" the TOU spread by burning battery life — and it is a stated differentiator vs naive slot-timers.

**Baseline-measurement methodology (how to prove savings over the inverter's own mode):** the hardware's built-in mode is the *real* competitor — Deye-class inverters already offer slot-based TOU with per-slot SOC targets and grid-charge enables, and owners already hand-tune them ([Powerforum thread](https://powerforum.co.za/topic/12521-deye-hybrid-system-time-of-use-settings/)). Three layers of proof, in increasing strength:
1. **Digital counterfactual:** every day, simulate the site's metered load/PV under the inverter's static mode (its exact slot settings, captured at onboarding) and under the executed plan; report the PKR delta. Compute costs at **1-minute resolution against actual net-billing settlement rules** — the arXiv paper shows 15–60-min averaging inflates MPC benefits by ~69% ([arXiv](https://arxiv.org/html/2510.25373v1)); OnyxLeaf must not mislead itself or customers with averaged accounting.
2. **A/B over time:** alternating weeks of inverter-mode vs OnyxLeaf control on the same site, weather-normalised.
3. **Bill-level truth:** DISCO bill comparison per settlement period — what the CFO actually sees.

**Data model (minimum viable):** per site — static (location, PV kWp, battery kWh/power/chemistry, genset kVA & fuel curve, DISCO + tariff category, feeder ID); time-series at 1-min (PV W, load W, SOC, grid import/export W, grid-present flag, genset run flag/fuel if metered); plans (96×15-min setpoint vectors, versioned); events (outages, genset starts, overrides); financial (tariff calendar, NAEPP rate, diesel price, computed daily savings vs counterfactual). Grid-present flag + timestamps is the seed of the outage forecaster; make it non-optional in Product 2's contract.

---

## Validation and pilot plan

Three sites, ~10 weeks, one number out the other end: verified Rs/month uplift over the inverter's own mode.

- **Site mix (2–3 C&I + 1 remote):** (a) a TOU-metered industrial/commercial consumer in IESCO/LESCO territory with existing hybrid PV+BESS; (b) a commercial site with genset backup on a load-shedding-prone feeder; (c) if accessible via a towerco/ISP relationship, one telecom/remote site — the segment where batteries last 1–2 h against 12-h shedding and diesel is both costly and stolen ([PhoneWorld](https://www.phoneworld.com.pk/pakistan-telecom-tower-diesel-theft-9200-incidents-senate-pta-load-shedding/)).
- **Protocol per site:** 2 weeks instrumented baseline (inverter's own mode, telemetry only) → 2 weeks OnyxLeaf control → repeat the A/B once (total ~8 weeks) to wash out weather; run the digital counterfactual throughout so even baseline weeks produce comparison data. Pre-register the metrics to keep ourselves honest.
- **Metrics:** PKR/day savings vs counterfactual and vs A/B baseline (1-min settlement accounting); peak-window grid import (kWh, 6–11 PM); genset run-hours and litres; battery throughput per saved rupee; % of outages entered with SOC ≥ target; forecast MAEs (PV %, load %); plan-execution fidelity (did the inverter actually follow setpoints — Product 2's KPI).
- **Success gates:** C&I: savings ≥ 3× the intended monthly fee; genset sites: ≥15% run-hour reduction; reserve: ≥90% of outages covered without deeper cycling than baseline.
- **Who hosts / channel:** recruit through installer/EPC partners — the meeting notes name **EnerCon, Greenfuels and NeoSat** as candidate channels (relationship claims from the meeting, **not independently verified** in this research; an "EnerCon Systems" appears in the [ENF Solar directory](https://www.enfsolar.com/enercon-systems) but was not confirmed as the same firm). Installers get a white-label dashboard and a differentiated O&M upsell; OnyxLeaf gets site access and a scalable channel — the model gridX proves in Europe ([gridx.ai](https://www.gridx.ai/)).

---

## Pricing hypotheses

Anchor to the rupee savings the report itself computes, with found price points bounding the range.

Found anchors: **SolarAssistant US$59 one-time** for residential monitoring ([shop](https://solar-assistant.io/shop)); forecast.solar **€16–200/yr** for forecast data alone ([forecast.solar](https://forecast.solar/)); Elum, SwitchDin, gridX, Stem all quote-only enterprise pricing (no public numbers — a gap OnyxLeaf can exploit with transparent pricing); Predbat free-personal + paid cloud shows hobbyists convert to hosted ([repo](https://github.com/springfall2008/batpred)).

| Segment | Hypothesis | Anchor logic |
|---|---|---|
| C&I (50–500 kWp + BESS) | **Rs 15,000–50,000/site/month SaaS**, or 20–30% of verified savings (gain-share) in year 1 | A site shifting 100 kWh/day across a Rs 30–40 store-vs-export/peak spread ≈ Rs 90k–120k/month gross; fee at ≤⅓ of verified savings sells itself; gain-share de-risks the unproven increment |
| Remote/telecom (per site, fleet) | **Rs 3,000–8,000/site/month** at fleet scale, sold to towerco/ESaaS operators | Value = diesel litres saved + theft-exposure reduction; 25 MWh ESaaS projects targeting 5 m litres/yr ([Arab News](https://www.arabnews.pk/pakistan/pakistan-launches-first-green-sukuk-to-cut-telecom-emissions-modernize-energy-use-2616788)) show operators monetise exactly this; must undercut per-site cost of in-house NOC analytics |
| Residential premium (via installers) | **Rs 15,000–25,000 one-time + Rs 500–1,000/month** optional cloud | SolarAssistant's $59 (~Rs 16.5k) proves one-time willingness for monitoring alone; optimisation justifies the subscription only if residential TOU spread widens (see Risks) |
| Installer white-label | Per-site wholesale at 50–60% of list | gridX/SwitchDin model; channel margin funds distribution |

Rule: never price above one-third of *verified, counterfactual-accounted* savings — the measurement methodology is also the pricing engine.

---

## Risks and kill criteria

The product dies if the increment over free built-in modes is too small to bill for; everything else is manageable.

| Risk | Evidence | Kill / mitigation criterion |
|---|---|---|
| **Increment over inverter's own TOU mode too small** | Deye-class inverters already do slot-based TOU with SOC targets ([Powerforum](https://powerforum.co.za/topic/12521-deye-hybrid-system-time-of-use-settings/)); literature's ~12.9% MPC-over-RBC uplift shrinks ~69% under 1-min net-billing accounting and sometimes reverses ([arXiv](https://arxiv.org/html/2510.25373v1)) | **Kill (for the pure-optimisation pitch) if pilots show <1.5× fee in verified savings on 2 of 3 sites.** Fallback: value shifts to outage-reserve + genset reduction + fleet reporting, which static modes cannot do (users hand-retune on cloudy days per the forum thread) |
| **TOU spread too narrow for residential** | HAZECO residential TOU spread is only Rs 6.32/kWh (46.85 vs 40.53) ([HAZECO](https://hazeco.com.pk/services/tariff)) — ~Rs 60–90/day for a 10–15 kWh evening shift | Keep residential as channel filler, not a business; revisit if industrial-style multi-slab TOU reaches homes |
| **Enerlytics ambiguity** | They own a local monitoring footprint (Wateen subdomain) and brand themselves "AI Energy Asset Management"; could add an optimiser or react to OnyxLeaf | Decide partner-vs-compete within 60 days via direct conversation; do not share optimiser details; ensure Product 2 makes OnyxLeaf independent of their telemetry |
| **Policy whiplash** | Buyback rate under PM-ordered review since 22 Oct 2026 ([The News](https://www.thenews.pk/print/1398311-govt-ends-net-metering-shifts-rooftop-solar-to-net-billing)); industrial TOU still unfinalised ([The News](https://www.thenews.pk/print/1407397-pakistan-wraps-tou-tariff-talks-keeps-regime-optional-for-industry)) | Tariff engine must hot-swap rates; note a *higher* buyback shrinks the store-vs-export spread — model sensitivity before committing revenue forecasts |
| **Write-access reality** | Many installed inverters expose poor/no remote control (Product 2 risk, but it caps Product 1) | If <50% of target fleet accepts closed-loop control, pivot to "advisory + auto-retune settings" tier |
| **Savings attribution disputes** | Counterfactuals are contestable | Pre-agreed measurement protocol in contract; A/B weeks on demand |
| **Payment/collection in PKR B2B; rupee volatility** | Standard Pakistan SaaS risk | Annual prepay discounts via installer channel |

---

## Open questions

1. Does Enerlyticslab actually control/dispatch hardware today, or only monitor + alert? (Their public apps show monitoring only; the Wateen deployment's control depth is unknown.)
2. What exactly will the notified NAEPP buyback be after the PM-ordered review — Rs 8.13, Rs 11, or something else — and will it apply retroactively to Feb–Oct 2026 net-billing signups?
3. Final shape and rates of the opt-in industrial multi-slab TOU tariff (and whether AMI rollout gates it by DISCO).
4. What fraction of Pakistan's installed hybrid-inverter base (Deye/Growatt/Sungrow/Huawei mix, by share) accepts third-party setpoints — the Product 2 dependency that caps Product 1's addressable market.
5. Are EnerCon / Greenfuels / NeoSat real, willing channel partners with C&I O&M books? (Unverified this cycle.)
6. Telecom segment entry: direct to towercos (Engro/edotco-class), via Infralectric-style ESaaS operators, or via ISPs/Wateen — and does Wateen's Enerlyticslab relationship close that door?
7. C&I metering reality: do target sites have TOU/AMI meters whose interval data OnyxLeaf can read for bill-level verification?
8. Diesel price and genset fuel-curve data quality: can fuel savings be metered (flow sensors) or only estimated on pilot sites?
9. Is there appetite for a gain-share contract (percentage of verified savings) vs flat SaaS in Pakistani C&I?

---

## Saudi Arabia and GCC

In the GCC the EMS case is **not** retail TOU arbitrage — tariffs are flat, subsidised and slab-based almost everywhere — it is diesel-hybrid optimisation for off-grid/remote and temporary sites, plus (selectively) Oman's hourly cost-reflective tariff and future KSA demand-side reform.

**KSA tariffs — flat, no retail TOU.** Saudi consumption tariffs are volumetric slabs set by the regulator (ECRA, now the Water & Electricity Regulatory Authority): residential 18 halalas/kWh up to 6,000 kWh/month and 30 above; commercial 20/30; industrial flat 18; agriculture 16/20 (effective 1 Jan 2018 — [Argaam](https://www.argaam.com/en/article/articledetail/id/519195)). Rates rise from 1 Jan 2027 to 21/33 halalas residential with SAR 70–100 fixed monthly fees ([KSAexpats calculator](https://ksaexpats.com/saudi-electricity-bill-calculator/)); typical all-in prices are ~SAR 0.20/kWh residential and ~SAR 0.257 business (~US$0.053/0.069, Jun 2025 — [GlobalPetrolPrices](https://www.globalpetrolprices.com/Saudi-Arabia/electricity_prices/)). **No time-of-use or demand-charge component appears on any page opened**; a special "Intensive Electricity Consumption Tariff" exists for very large users ([Qanoniah regulation text](https://qanoniah.com/en/File/kxV01WJv87EA0ra79r2jqQ3lp-Rules,-Regulations,-and-Executive-Procedures-Necessary-for-the-Implementation-of-the-Intensive-Electricity-Consumption-Tariff), details not extracted — unverified). Consequence: **behind-the-meter battery TOU arbitrage has essentially zero spread in KSA** — a battery cycled against a flat 18–30 halala tariff earns nothing on timing, and at ~Rs 15/kWh-equivalent prices even self-consumption economics are far weaker than Pakistan's.

**KSA small-scale PV regulation.** The regulatory framework covers systems of 1 kW–2 MW for all consumer classes under a **net-billing** regime (ECRA, Jul 2020 — [pv magazine](http://www.pv-magazine.com/2020/07/13/saudi-arabia-introduces-new-provisions-for-rooftop-pv/)); the export credit rate was not found on pages opened (unverified) — but with retail rates this low, exports are near-worthless and rooftop adoption is thin regardless.

**Where the money is: diesel and off-grid.** Aramco raised diesel to **SAR 1.79/L from 1 Jan 2026** (+8%; it was SAR 0.63/L in 2022 — a ~3× rise in four years), hitting cement, paper and utility companies' costs directly ([AGBI, Jan 2026](https://www.agbi.com/oil-and-gas/2026/01/saudi-companies-face-cost-increase-as-aramco-ups-diesel-price/)). At SAR 1.79/L a genset's fuel-only cost is roughly SAR 0.45–0.55/kWh — **2–3× the grid tariff**, so every remote/off-grid site (no grid to subsidise it) has real optimisation value. A market study puts KSA off-grid solar at **~US$1.42 bn (2025) → US$4.58 bn (2035), ~12.4% CAGR**, with solar-diesel hybrids 45% of 2026 architecture share and C&I + agriculture + telecom-tower powering named verticals ([Nexdigm](https://www.nexdigm.com/market-research/report-store/ksa-off-grid-solar-energy-market/) — single-vendor research, treat figures as indicative). Vision-2030-adjacent demand is explicit: the state targets displacing >1 m barrels of oil/day with gas and renewables ([AGBI](https://www.agbi.com/oil-and-gas/2026/01/saudi-companies-face-cost-increase-as-aramco-ups-diesel-price/)), KSA is deploying grid-scale BESS at enormous scale (2 GWh Bisha commissioned Q1 2025; ~48 GWh targeted by 2030 — [Industrial Info](https://www.industrialinfo.com/news/article/saudi-arabia-aims-to-deploy-48-gwh-bess-capacity-by-2030--340453); grid-scale, not behind-the-meter), and NEOM/giga-project microgrid design work is already contracted to tools like Xendee (search result only; page not opened — unverified).

**KSA local players and route in.** Desert Technologies (Jeddah) launched **Nurun Digital Energy** and the **Sahara** 35–60 kW off-grid solar container line aimed at military camps, telecom towers, oil & gas, labour camps and farms across GCC/Africa — hardware-led, with no EMS software described ([SolarQuarter, Nov 2022](https://solarquarter.com/2022/11/03/desert-technologies-group-launches-nurun-digital-energy-and-launches-its-new-product-sahara/)) — a natural OEM-software partner rather than a competitor. Alfanar and ACWA Power were not researched beyond search listings (unverified). Regulatory route: foreign software entities enter via a MISA licence (not verified against a primary source this cycle); hardware controllers imported into KSA need SASO conformity — "specified electronic devices" require an **IECEE Recognition Certificate** issued against accredited CB test reports before a Certificate of Conformity ([SGS, 2024](https://www.sgs.com/en-fr/news/2024/06/pca-2024-q2-saudi-arabia-imports-what-is-saso-iecee-recognition-certification)); whether a third-party EMS gateway falls in scope needs confirmation — but this burden lands on Product 2's hardware, not the cloud EMS.

**Rest of GCC, briefly:**

| Market | Tariff reality | EMS angle |
|---|---|---|
| UAE (Dubai) | DEWA slabs 29–44 fils/kWh incl. fuel surcharge; **Shams Dubai is true net metering** (credits at own slab rate, no cash, no expiry), 1 MW/plot cap; no TOU/demand charges found ([SurgePV guide](https://www.surgepv.com/solar-compliance/uae/guides/dewa-shams-dubai)) | Net metering kills storage arbitrage; the market is **temporary/off-grid power**: Enerwhere (Dubai) runs 200 kW–20 MW solar-hybrid minigrids as a service for construction, mining, hospitality ([pv magazine PR](https://www.pv-magazine.com/press-releases/enerwhere-signs-long-term-agreement-to-power-mine-in-the-uae-with-solar-hybrid-micro-grid/)) — a model customer/partner for the optimiser |
| Oman | **The GCC's one real price signal:** Cost-Reflective Tariff for ~10,000 large govt/commercial/industrial users (>150 MWh/yr), priced **differently for every hour of the day**, effective 1 Jan 2017 ([Oman Observer](https://www.omanobserver.om/ampArticle/78837)) | Hourly-price optimisation is directly sellable to CRT customers; plus Tanweer (Rural Areas EC) runs 34 diesel plants and procured 11 solar+diesel+BESS hybrids (146 MW, 28 MWac BESS) under a 15-yr PPA ([Oman Observer](https://www.omanobserver.om/oman-to-adopt-battery-storage-for-hybrid-power-projects/)) — IPP-operated hybrid dispatch is an EMS buyer |
| Bahrain | EWA slabs: subsidised nationals 3–32 fils/kWh, non-domestic 22–32 fils; no TOU ([EWA tariff page](https://www.ewa.bh/en/tariff)) | Too small/flat; skip |
| Kuwait | ~0.7 US¢/kWh since 1966, covering ~5% of supply cost ([Baker Institute, Jan 2025](https://www.bakerinstitute.org/research/distortionary-effects-kuwaits-cheap-electricity-and-case-just-reform)) | The most subsidised grid on earth; zero behind-the-meter case |
| Qatar | Not researched beyond search listings this cycle — **unverified/no finding** | — |

**Vendor-agnostic EMS players active in GCC:** Elum Energy markets "monitoring and control solutions tailored for the MENA region and Pakistan," exhibits at WFES Abu Dhabi and names MENA sales contacts, but the page shows Paris/Casablanca phone numbers and **no Dubai/GCC office was verified** ([Elum WFES 2023 post](https://elum-energy.com/blog/world-future-energy-summit-2023/)). Enerwhere is an asset owner-operator, not a software vendor. No Saudi EMS-software startup equivalent to Enerlyticslab surfaced in searches (absence of evidence, not proof).

**Conclusion — what changes vs Pakistan.** The Pakistan thesis (TOU spread + net-billing asymmetry + load shedding) does **not** transplant: GCC grids are flat-priced, subsidised and reliable. The GCC version of the product is the **genset/off-grid optimisation half only** — solar-diesel-BESS dispatch, fuel-per-kWh minimisation and fleet reporting for remote sites, construction camps, mining and telecom, sold to ESCOs/operators (Enerwhere-style), container-solar OEMs (Desert Technologies-style) and Oman CRT customers — where the "tariff engine" is replaced by a fuel-price engine and, in Oman only, an hourly price feed. Rising Saudi diesel prices move in OnyxLeaf's favour annually. Strategically: prove the genset-reduction module in Pakistan's telecom/remote segment first — it is the exact capability the GCC buys — and treat KSA/UAE as a year-2 expansion via hardware partners, not a direct-sales market; revisit if KSA introduces TOU or demand charges under its ongoing tariff reform (2027 hike announced, structure still flat).

---

## Sources

**Enerlyticslab / Pakistan ecosystem**
- https://enerlyticslab.com/ (homepage; empty SPA shell, meta keywords only)
- https://play.google.com/store/apps/details?id=com.app.enerlyticsrevamp&hl=en_CA (Enerlytics Android app)
- https://apps.apple.com/us/app/enerlytics-pro/id6755328009 (Enerlytics Pro iOS; © 2025 Samir Ahmed)
- https://wateen.com/energy-efficiency/ (Wateen Energy EMS/NOC page)
- https://ensun.io/search/energy-management/pakistan (PK EMS company listing)
- https://www.thenews.pk/print/1441982-solar-pakistan-2026-opens-in-karachi-with-over-350-companies (Solar Pakistan 2026)
- https://energetica-india.net/news/solar-energy-management-tool-for-a-leading-data-and-voice-communication-provider-in-pakistan (meeco/Multinet; checked, not Enerlytics)

**Pakistan policy, tariffs, market**
- https://www.thenews.pk/print/1398311-govt-ends-net-metering-shifts-rooftop-solar-to-net-billing (net billing; Oct 22 review)
- https://tribune.com.pk/story/2591628/1 (Prosumer Regulations detail)
- https://www.toolsfluent.com/blog/net-metering-pakistan-complete-guide (SRO 251(I)/2026, SRO 1330(I)/2026, NAEPP estimates Rs 8.13–11)
- https://hazeco.com.pk/services/tariff (TOU rates, peak window)
- https://en.dailypakistan.com.pk/21-Aug-2025/peak-electricity-hours-cut-to-11pm-to-curb-losses-in-power-sector (peak-hour policy)
- https://www.thenews.pk/print/1407397-pakistan-wraps-tou-tariff-talks-keeps-regime-optional-for-industry (industrial TOU)
- https://www.thenews.pk/print/1398879-more-than-2-000-feeders-face-over-10-hours-of-daily-loadshedding-na-told (load shedding by DISCO)
- https://www.ess-news.com/2026/07/15/pakistan-imports-4-6-gwh-of-bess-in-2025/ (BESS imports)
- https://www.phoneworld.com.pk/pakistan-telecom-tower-diesel-theft-9200-incidents-senate-pta-load-shedding/ (towers, theft, shedding)
- https://www.arabnews.pk/pakistan/pakistan-launches-first-green-sukuk-to-cut-telecom-emissions-modernize-energy-use-2616788 (green sukuk, ESaaS)
- https://maxpower.com.pk/news/diesel-generator-vs-solar-system/ (genset cost, vendor source)

**Commercial landscape**
- https://elum-energy.com/ · https://www.victronenergy.com/live/drafts:dynamic_ess · https://www.tesla.com/support/energy/powerwall/mobile-app/time-based-control · https://solar.huawei.com/en/professionals/all-products/fusionsolar-smart-pv-management-system · https://www.switchdin.com/ · https://www.gridx.ai/ · https://www.stem.com/athena/ · https://hybridgreentech.com/ · https://fluenceenergy.com/fluence-iq-digital-platform/ · https://solar-assistant.io/shop
- https://powerforum.co.za/topic/12521-deye-hybrid-system-time-of-use-settings/ (Deye built-in TOU behaviour)

**Open source & forecasting**
- https://github.com/davidusb-geek/emhass · https://emhass.readthedocs.io/en/latest/
- https://github.com/springfall2008/batpred (non-OSS license)
- https://github.com/OpenEMS/openems · https://github.com/evcc-io/evcc
- https://github.com/openclimatefix/open-source-quartz-solar-forecast · https://github.com/pvlib/pvlib-python
- https://github.com/MyEMS/myems · https://github.com/OpenLoaf/OpenLEMS
- https://github.com/topics/energy-management-system · https://github.com/topics/microgrid
- https://forecast.solar/ · https://solcast.com/pricing · https://open-meteo.com/

**Saudi Arabia and GCC**
- https://www.argaam.com/en/article/articledetail/id/519195 (SEC/ECRA tariff slabs, 2018)
- https://ksaexpats.com/saudi-electricity-bill-calculator/ (current + 2027 KSA rates)
- https://www.globalpetrolprices.com/Saudi-Arabia/electricity_prices/ (KSA avg prices)
- http://www.pv-magazine.com/2020/07/13/saudi-arabia-introduces-new-provisions-for-rooftop-pv/ (1 kW–2 MW net-billing framework)
- https://qanoniah.com/en/File/kxV01WJv87EA0ra79r2jqQ3lp-Rules,-Regulations,-and-Executive-Procedures-Necessary-for-the-Implementation-of-the-Intensive-Electricity-Consumption-Tariff (intensive-consumption tariff, not extracted)
- https://www.agbi.com/oil-and-gas/2026/01/saudi-companies-face-cost-increase-as-aramco-ups-diesel-price/ (diesel SAR 1.79/L, Jan 2026)
- https://www.nexdigm.com/market-research/report-store/ksa-off-grid-solar-energy-market/ (KSA off-grid market sizing)
- https://www.industrialinfo.com/news/article/saudi-arabia-aims-to-deploy-48-gwh-bess-capacity-by-2030--340453 (KSA grid BESS)
- https://solarquarter.com/2022/11/03/desert-technologies-group-launches-nurun-digital-energy-and-launches-its-new-product-sahara/ (Desert Technologies/Nurun/Sahara)
- https://www.sgs.com/en-fr/news/2024/06/pca-2024-q2-saudi-arabia-imports-what-is-saso-iecee-recognition-certification (SASO IECEE)
- https://www.surgepv.com/solar-compliance/uae/guides/dewa-shams-dubai (DEWA tariffs, Shams Dubai net metering)
- https://www.pv-magazine.com/press-releases/enerwhere-signs-long-term-agreement-to-power-mine-in-the-uae-with-solar-hybrid-micro-grid/ (Enerwhere)
- https://www.omanobserver.om/ampArticle/78837 (Oman CRT hourly tariff)
- https://www.omanobserver.om/oman-to-adopt-battery-storage-for-hybrid-power-projects/ (Tanweer 11-site hybrid+BESS)
- https://www.ewa.bh/en/tariff (Bahrain EWA tariffs)
- https://www.bakerinstitute.org/research/distortionary-effects-kuwaits-cheap-electricity-and-case-just-reform (Kuwait tariff)
- https://elum-energy.com/blog/world-future-energy-summit-2023/ (Elum MENA presence)

**Economics of the increment**
- https://arxiv.org/html/2510.25373v1 (MPC vs rule-based under net billing; 12.9% literature figure; ~69% shrink at 1-min accounting)
