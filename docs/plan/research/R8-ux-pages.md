# R8. UX study of the closest products, and the page and tab inventory for the OnyxLeaf EMS core product

Research note for the OnyxLeaf EMS build plan. Written 6 November 2026 by a planning subagent (task R8-ux-pages). Self-contained: a reader who has not seen the brief can use it alone.

Purpose. OnyxLeaf (owner Saad, Pakistan) is planning a TOU-aware, outage-aware, genset-aware energy management and dispatch optimiser (an EMS) for Pakistani commercial and industrial sites with PV + LFP battery + unreliable grid + diesel genset. The product shape is already decided by the two research documents in `C:/Users/SaadTahir/Videos/solarytics/` (`Perfect-EMS_TOU_Dispatch_Deep_Review.md`, the guideline, and `Product 1 - TOU-Aware Energy Management and Dispatch.md`, the supporting report): a cloud MILP optimiser (15-minute steps, 48-hour horizon, re-solved every 15 minutes, HiGHS), PV/load/outage forecasters, a Pakistan tariff engine, 1-minute savings verification against the inverter's own mode, and plan delivery first as daily inverter TOU slots (Mode A), later as 15-minute setpoints via Product 2 (Mode B). This note does two things: (1) it studies the user experience of the closest products (study only, no code copied) and (2) it proposes the page and tab inventory of the OnyxLeaf core product (not the admin or founder console), following the owner's lettered-tab convention (A, B, C...) inside a page.

What was read before writing. Guideline lines 164-200 (architecture, forecasting, battery-life-aware dispatch, baseline methodology, data model, pilot plan); supporting report lines 185-236 (Enerlyticslab modules), 438-466 (7 components, Mode A and Mode B), 494-514 (KPIs and fair savings); sibling notes `plan-work/research/U1-boxlytics-frontend.md` section 8 (how a lettered-tab page is structured: tabs declared as data, one hook, one panel per tab, URL-synced `?tab=`, a Review tab locked until every lettered tab is complete) and `U3-product2-interface.md` sections 2.2-2.4 and 3 (primitives `battery_mode`, `battery_power_w`, `grid_charge_allowed`, `genset_request` advisory-only, `release`; Mode A slot limits per brand; write budget, read-back, plan expiry, link loss). Web sources are listed per product in Part 1 with the date fetched (6 November 2026 unless stated). Anything that could not be opened or confirmed is marked UNVERIFIED.

Conventions in this note. "Page" is a route in the left sidebar. "Tab" is a lettered sub-view inside a page (A, B, C...) when the tabs are sequential (a wizard or an editor), and a plain named sub-view when they are not (dashboards, plan, savings), following U1 section 8.2 point 7. "Rs" is Pakistani rupees. Times are site-local (Asia/Karachi, UTC+5, no DST).

---

## Part 1. UX study of the closest products

Study only. No code was read from any of these products; OpenEMS was examined through its documentation and FENECON's user manual only (its code is AGPL and was not opened). Each entry: key screens, plan visualisation, savings presentation, fleet listing, alerts, then sources with the date fetched. Products are grouped by how close they are to OnyxLeaf's problem.

### 1.1 Predbat (home battery planner for Home Assistant; the richest plan table in the field)

Key screens (built-in web interface): Dash (status and mode, a power-flow diagram of instantaneous PV, battery, grid and house power, a text summary of the plan, all output entities, a Debug panel with downloads), Plan, Charts, Config (every configuration entity with name, type, current value and default; values changed from default shaded pale red), Apps (apps.yaml as editable fields with validation error counts), Log (tabs Warnings, Errors, Info, All; newest first; colour by level), Compare (what you would pay on other tariffs using the same predicted load and PV).

Plan visualisation. One HTML table, one row per 30-minute slot by default (`plan_interval_minutes`), while the inverter is controlled in fixed 5-minute segments and the plan is recomputed every 5 to 10 minutes (`calculate_plan_every`, default 10) over a 48-hour forecast (`forecast_hours`). Columns: Time, Import rate, Export rate, State (Charging, Discharging or Demand, Export, Hold, Idle, with a trend arrow), Limit % (planned SoC at end of slot, e.g. "Charge 70%", a snail glyph for slow charge), PV kWh (Solcast), Load kWh, Clip kWh and Xload kWh (debug only), Car kWh, iBoost kWh, SoC % (start of slot, arrow for rising, level, falling), Cost (pence, arrow), Total (running cost since midnight, does not reset), optional CO2 columns, and in the History view a Debug column with a per-slot snapshot download. Cell colours encode thresholds, not series: import rate blue (zero or negative), green (below the import threshold), yellow (1 to 1.5 times threshold), red (above); state green for Charging, yellow Discharging, greys for Freeze states, pale blue Hold; SoC green at 50% or more, yellow at 20% or more, orange-red below 20%; cost orange-red at 10p or more, green when negative. Symbols on rates mark provenance: "?" rate not yet published so yesterday's used, a scale glyph for an estimated future rate, "=" overridden in config, a plus-minus sign for an offset, "$" for a saving-session adjustment; a manual-override glyph marks forced slots; a flag marks slots with warnings. Buttons switch between Plan (forward), History (midnight yesterday to now) and "Yesterday without Predbat" (a simulated day with the battery left alone). Clicking a slot opens a manual override (Demand, Charge, Export, Freeze Charge, Freeze Export, Clear).

Uncertainty. Three PV scenarios: central (Solcast 50%), PV10 (pessimistic, weight `pv_metric10_weight` default 0.15) and PV90 (optimistic, weight 0.15, only when `calculate_pv90_plan` is on; weights are rescaled if they sum above 1). Load scenarios `load_scaling10` 1.1 and `load_scaling90` 0.9. The Battery chart shows SoC under four scenarios: Base, Base10, Best, Best10 (Base = inverter left as it is, Best = Predbat's plan). Battery wear is a virtual cost per kWh (`metric_battery_cycle`, default 0p, 0 to 2p recommended); a soft keep level (`best_soc_keep`, weight 0.5) sits above a hard minimum.

The "why" of a slot. There is no per-slot reason column. Debug mode shows effective rates after battery and inverter losses in brackets so the user can see, for example, that a slot with an effective charge cost of 13.93p and effective export value of 17.31p was chosen because of the 3.38p per kWh margin. Predbat's `status` entity gives a one-line description of the current action with warnings in square brackets. Lesson: a reasons column is the one thing the best home planner lacks and users ask for; OnyxLeaf should ship it.

Savings presentation. Indirect: the Compare view (tariff vs tariff), the "Yesterday without Predbat" simulated plan, the running Total column, and the Cost chart (historic and predicted import, export and net). No bill rebuild, no A/B.

Fleet. None (single home).

Alerts. Log tabs, flag glyphs on plan rows, status text with bracketed warnings.

Sources (fetched 6 Nov 2026): springfall2008.github.io/batpred/predbat-plan-card/, /web-interface/, /customisation/, /what-does-predbat-do/, /output-data/.

### 1.2 Victron Dynamic ESS (VRM cloud planner for Victron systems)

Key screens. VRM Settings, Dynamic ESS: a five-step wizard (1 System: grid sell-back yes or no, export limit, Green or Trade mode, advanced "disable solar at low buy prices"; 2 Battery: capacity, max charge and discharge power, battery cost in EUR/kWh computed as purchase cost divided by cycles times capacity, charge and discharge restrictions by time; 3 and 4 Prices: dynamic day-ahead provider or a formula such as P x 1.21 + 0.11; 5 Confirmation summary). On the VRM dashboard a section graphs today's prices against the plan; from about 15:00 the next day's prices and plan appear too; a controls panel turns Dynamic ESS on or off (needs "Realtime updates").

Plan visualisation. Prices as bars, planned battery charge and discharge per hour, a target SoC per hour (the system re-solves every few minutes to hit that hour-end SoC: behind, it speeds up; ahead, it slows down), consumption and solar forecasts. Users on the community site asked for a 15-minute view, a 7-day SoC forecast and historical forecast data; they also complained that plan and actual did not match. Modes: Green (cover consumption and charge the battery first, sell surplus, charge when cheap; described as safer and more predictable) and Trade (sell whenever selling beats charging; higher gains, more risk), switchable at any time.

Offline fallback. If the VRM connection is lost for more than 12 hours the schedule runs out and the system reverts to standard ESS; schedules resume when the connection returns. If price data cannot be fetched the schedule also runs out. (The guideline, line 169, adopts the revert-after pattern with a much shorter threshold for Pakistan.)

Savings presentation. Not a distinct screen in the documentation read; prices and energy graphs only. UNVERIFIED whether the current VRM release has a savings widget.

Fleet. VRM's installation list (not specific to Dynamic ESS).

Alerts. Error codes shown as text: "No matching schedule available" (clock drift or SoC below minimum), "ESS functionality not available", "Battery capacity unset".

Sources (fetched 6 Nov 2026): victronenergy.com/live/drafts:dynamic_ess; community.victronenergy.com articles 270649 "Dynamic ESS on VRM" and 283506 "Dynamic ESS green mode"; github.com/victronenergy/dynamic-ess RELEASE-NOTES.md (target-SoC schedules with start, duration and feed_in flag). Screenshots themselves were not viewable; descriptions come from text and comments.

### 1.3 Tesla Powerwall app, Time-Based Control (now also called Savings mode)

Key screens. Settings, Powerwall, Operational Mode (Backup-only, Self-Powered, Time-Based Control with Balanced or Cost Saving), a Utility Rate Plan card where the user enters only the peak and off-peak windows (the rest becomes shoulder or part-peak), a Backup Reserve slider 0 to 80% (100% blocks the economic modes; Max Backup forces 100% for up to 72 hours), Storm Watch. The Power Flow screen animates home, solar, Powerwall and grid; the Energy Usage graph shades rate periods behind the bars.

Plan visualisation. None. The user never sees the plan; the app shows what the battery is doing now and what it did. Behaviour is explained in prose: when solar is insufficient and peak usage is forecast, it charges during off-peak and discharges at peak; forecasts of solar and usage are refreshed roughly every 15 minutes and the SoC target moves with them (commenter claim, UNVERIFIED). Balanced stores excess solar in off-peak and shoulder; Cost Saving stores all solar and may charge from the grid where allowed (AU, not US historically).

Savings presentation. The app's energy screens show kWh by source; a savings amount is not documented on the pages read. UNVERIFIED.

Fleet. None in the consumer app.

Alerts. Push notifications (Storm Watch, grid outage), not documented in detail on the pages read.

Sources (fetched 6 Nov 2026, several tesla.com support pages returned 403 and were read through search summaries): tesla.com/support/energy/powerwall/mobile-app/time-based-control, /time-based-control-user-guide, /backup-reserve, /powerwall-modes; epxhilon.blogspot.com 2018 TBC post; teslamotorsclub.com threads. Lesson: hiding the plan is fine for a homeowner, wrong for a C&I operator who must apply slots by hand in Mode A.

### 1.4 EMHASS (LP-based optimiser for Home Assistant)

Key screens. A single web page served on port 5000 with buttons for the actions `perfect-optim` (backtest on the last days of real PV and load, the theoretical best-case cost used as a benchmark), `dayahead-optim` (the production case on forecasts), `naive-mpc-optim` (receding horizon with a prediction horizon parameter), `publish-data`, `forecast-model-fit`, `-predict`, `-tune`, and a configuration page. After an optimisation the page draws one stacked time-series plot (load, PV, battery power, SoC, grid power, deferrable loads, unit load cost and production price, cost) and a results table of the same columns per step. The results are published to Home Assistant as sensors (forecast power per asset, SoC, cost function value, unit costs, deferrable load schedules, optimisation status) and automations act on them. A user reported that the table stays empty until an optimisation is pressed.

Plan visualisation. The plot above; no slot table with reasons; no Gantt.

Savings presentation. A single number per run (the cost function value; the basic PV study case quotes about minus 1.56 EUR for the day-ahead run against minus 3.75 EUR per day for perfect foresight). Lesson adopted by the supporting report (line 511): report realised-forecast results and a perfect-foresight upper bound separately.

Fleet, alerts. None.

Sources (fetched 6 Nov 2026): emhass.readthedocs.io/en/latest/ (intro), /study_cases/basic_pv.html, usage_guide.md (actions), github.com/davidusb-geek/emhass README. Exact entity names and the plot legend were not confirmed from the pages that loaded: UNVERIFIED in detail.

### 1.5 OpenEMS UI (open-source EMS; studied via docs and FENECON's FEMS manual only)

Key screens. Login and an edge list (the Backend aggregates many Edges), per edge: Live, History, Settings. Live is a grid of flat widgets that adapt to the edge's configuration: Energy Monitor (a radial energy-flow figure with production, consumption, battery charge or discharge, grid withdrawal or feed-in; grey bars show each element's maximum power; informational, not clickable), Storage (SoC and direction, advanced view with per-phase power and an emergency-reserve toggle), Grid (withdrawal and feed-in, advanced view with external limitation such as a ripple-control receiver), Production, Consumption (per load, e.g. a charging station), Autarky % and Self-consumption %, plus controller widgets (ESS Time-of-Use Tariff, Symmetric and Asymmetric Peak-Shaving, Timeslot Peak-Shaving, Grid Optimized Charge). Clicking a flat widget opens a modal with detail. History has a period selector that applies to the whole view, a chart of production, consumption, grid and storage with legend toggling, cumulative kWh tiles per widget, an SoC chart with the emergency reserve as a dashed line, and a per-day Excel REPORT download.

Plan visualisation. The Time-of-Use Tariff app adds a history view; FENECON's page says only that it exists. From OpenEMS release notes and screenshots described elsewhere the ToU widget shows a price curve with coloured state bands (charge from grid, delay discharge, balancing) and predicted SoC. UNVERIFIED in detail today.

Savings presentation. Autarky and self-consumption percentages, kWh totals; no money view in the standard widgets.

Fleet. The edge list in the Backend UI; FENECON portals add fleet features. UNVERIFIED detail.

Alerts. An Announcements widget; alerting settings exist in the UI (per-user alert subscriptions) but were not confirmed on the pages read.

Sources (fetched 6 Nov 2026): openems.github.io/openems.io/openems/latest/introduction.html (Live and History screenshots, controller list), /ui/architecture.html (UI adapts to EdgeConfig; widget types Flat, Modal, Chart), docs.fenecon.de/en/fems/fenecon_app/local_monitoring/standard_historical_widgets.html. Lesson: the radial energy-flow widget with max-power bars is the clearest overview pattern seen; adopt it for UI-04 with a genset node added.

### 1.6 Akkudoktor EOS (German open-source optimiser with EOSdash)

Key screens. EOSdash on port 8504 (API on 8503): a configuration view (recommended way to configure EOS), a Demo view, and since the 0.1.0 development line Plan and Prediction tabs plus a Solution view that "displays genetic optimization results and aggregated totals". Charts are rendered with Bokeh (the dash module lists a bokeh component). The optimiser runs automatically at intervals and emits an energy-management plan (hourly AC charge, DC charge, discharge-allowed vectors, EV SoC) and totals (overall balance in EUR, cost and revenue per hour, battery SoC per hour, grid import and export per hour).

Plan visualisation. Hourly bars and lines on one page; a PDF plot of the result was the original output. Not a slot table; no reasons.

Savings presentation. Overall balance in EUR for the horizon.

Fleet, alerts. None.

Sources (fetched 6 Nov 2026): github.com/Akkudoktor-EOS/EOS README, akkudoktor-eos.readthedocs.io CHANGELOG and dash module index, akkudoktor.net forum post "EOS neue Entwicklungsversion 0.1.0+dev". Tab names and chart contents partly UNVERIFIED (the docs pages for optimisation and integration returned 404 or an index only).

### 1.7 Enerlyticslab (Pakistan; the local monitoring incumbent)

The live site (enerlyticslab.com, /energy, /features) is JavaScript-rendered and returned only its title to the fetcher on 6 Nov 2026, so this entry relies on the supporting report's reading of the same pages (report lines 185-236). Modules as marketed: BESS (SoC, pack voltage spread, cycles, availability, round-trip efficiency, levelised cost of storage, SoH, predictive maintenance), Generator (engine speed, real-time fuel use in kg/kWh, blended cost in PKR/kWh, active power, load ratio, power factor, rotor-health prediction), Solar PV (PR, curtailment, mismatch and soiling losses, cleaning alerts, O&M SLA tracking, remote inverter tests), Grid (maximum demand, grid availability and load shedding, a load-shedding forecast, net-metering reports, energy mix, power quality), Utilities (pump, motor, compressor, chiller, boiler), Platform (predictive alerts, load and bill forecasting, carbon accounting, ISO 50001 and M&V support). A white-label portal appears to exist at wateen.enerlyticslab.com (robots-blocked).

Plan visualisation. None: no TOU battery scheduling, solver or setpoints are described. Savings presentation: KPI reports, blended PKR/kWh, bill forecasting, M&V use case. Fleet: implied by "site comparisons" style asset management, not confirmed. Alerts: predictive alerts and cleaning alerts.

Lesson: the metrics vocabulary a Pakistani buyer already knows (blended PKR/kWh, load ratio, grid availability, MDI, load-shedding forecast) should appear in OnyxLeaf's pages under the same names, and the increment (plan, verified saving) must be visibly new.

### 1.8 NeoTAQ neoSphere (Pakistan, UAE-origin datalogger vendor)

No official product page was reachable. A trade-show post describes three tiers: Monitor (real-time monitoring, alarms, live asset visibility, system health via a mobile app), Operate (custom reporting, asset-level analytics, site comparisons, financial-impact views), Intelligence (anomaly detection, failure prediction, performance optimisation, a "NeoBot" query assistant). A businessbytes.pk article (Green Fuel Energy partnership) says all NeoTAQ dataloggers (Modbus RTU, TCP, OPC UA) connect to the neoSphere cloud for centralised monitoring. Screens, plan view, savings view and alert design: UNVERIFIED. Sources (6 Nov 2026): pk.linkedin.com post via search summary; businessbytes.pk/?p=8445.

### 1.9 Elum Energy ePowerMonitor (solar-diesel hybrid monitoring, Africa and Asia)

Key screens (from the ePowerMonitor LITE user manual, a Grafana-style portal): left sidebar Homepage, Profile, Information Center; top bar with Favorite, Share or Export (direct link with locked time range and theme, public snapshot with expiry, JSON export), time-range picker with presets, auto-refresh (15 minutes to 1 day), a kiosk "cycle view". Panels: site selector, power-over-time graphs per component, a "last data" timestamp (dd/mm/yyyy HH:mm), a system-architecture panel with PV, Grid, Genset and Load icons where yellow animated flows are active and grey inactive, a site photo, and an Admin portal with Assets (grid type, PV kWp, tilt, module details, genset capacity, location), Devices (status, type, name, vendor), Gateways (id, last data received), Share access (Admin, Editor, Viewer; editors cannot add users). Multi-site: Assets, Devices and Gateways tabs across all sites with selectable columns. Any panel can be inspected (data points as CSV, query stats, JSON). The Pro tier adds alarms with site-specific labels and ranges on the dashboard. The companion ePowerControl SD curtails PV to keep minimum genset loading and zero export.

Plan visualisation: none (monitoring). Savings: production by source, not money, in the LITE manual. Fleet: multi-site tables. Alerts: Pro-tier alarms.

Sources (fetched 6 Nov 2026): elum-energy.com/?p=26886 (LITE manual), elum-energy.com/solar-diesel-hybrid-controller/, search summaries of ePowerLog pages. Lesson: the per-site "last data" stamp, the device and gateway tables, the three roles and the architecture icon panel are exactly what an installer expects; copy the vocabulary, not the look.

### 1.10 Schneider EcoStruxure Microgrid Advisor (cloud + on-site controller, C&I)

Key screens (from the 2017 handout, read as a PDF): top navigation Realtime, Consumption, Reports, Activities, Notifications; a site tab row (Overview, Control Load, Customer Load, Electric Vehicle, Battery, Wind, Solar, Natural Gas Generators); a "last refresh 4 min ago" stamp; a radial energy-flow overview with kW per asset (utility, solar, generator, battery, EV, HVAC, lighting) around a central node, a CO2 gauge; an Activity Highlights side panel (demand charge limit vs grid consumption as a bar, off-grid instances per year, a 3-hour weather strip with cloud cover, precipitation, pressure, wind and storm probability); a bottom bar with Active Mode chips (Monitoring, Demand Charge, Off-Grid Preparedness, Tariff Management, Demand Response, Off-Grid), an Autopilot toggle and an Off-Grid Preparedness toggle with storm start and stop times. Side panels: Financial Report (Baseline, Savings total broken into Optimum Start-Stop, Tariff Management, Demand Charge, Autoconsumption; Earnings total from feed-in; Adjusted Baseline) and Environmental Report (CO2 emissions, savings, adjusted). Features list: forecast and optimise when to consume, produce or store; "48-hour advanced automatic default operation schedules" kept locally if the server link is lost; export to Excel; OpenADR 2.0; later releases refresh screens automatically and add hour-ahead pricing.

Plan visualisation. Not shown in the handout; mode chips and the Autopilot toggle stand in for it. Savings presentation: the Financial Report panel, baseline vs savings by lever, is the clearest "savings by reason" layout found in this study. Fleet: "My sites" selector. Alerts: a Notifications count in the header and an Activities feed.

Sources (6 Nov 2026): download.se.com/doc/998-20080341_US/EcoStruxure Microgrid Advisor Handout.pdf (October 2017); solarpowerworldonline.com 2017 upgrade article via search. Current screens may differ: the handout is nine years old.

### 1.11 FlexMeasures (open-source flexibility platform, Seita)

Key screens. A dashboard with an asset map and list; an asset page that is "a data dashboard": Add Graph, overlay several sensors per graph, custom graph titles, a replay button that animates the data, a `sensors_to_show` attribute to script the layout, a flex-context editor (fields that steer scheduling, each a fixed value or a sensor); a sensor page with line plots and a weekday-by-hour heatmap ("time map": dates on y, time of day on x, so recurring morning peaks stand out); a status page per asset (data connectivity and recent scheduling jobs, tracking every sensor in the flex context); an audit log of who changed what. Changelog: overlays distinguish source by trace, sensor by colour, source type by dash pattern, so forecasts, schedules and measurements can share one chart.

Plan visualisation. A schedule is just another sensor series overlaid on the measurement. No reasons. Savings: none built in. Fleet: the map and list. Alerts: job status on the status page.

Sources (fetched 6 Nov 2026): flexmeasures.readthedocs.io/v0.25.0/views/asset-data.html; changelog via search. Lesson: the heatmap of weekday by hour is the right chart for outages and load patterns (UI-07, UI-05); the status page pattern (data connectivity plus job health) belongs in UI-06.

### 1.12 Stem Athena / PowerTrack (C&I storage, US)

Only press material was reachable. Athena was folded into PowerTrack: PowerTrack Optimizer (forecasting, real-time dispatch, value stacking), PowerTrack APM (portfolio and site commercial performance, revenue streams vs expectations in one view, warranty-obligation tracking) and the AlsoEnergy heritage Site Overview (current production with past-performance comparison) and Portfolio View. No screen descriptions of the dispatch plan, savings or alerts: UNVERIFIED. Sources (6 Nov 2026): businesswire.com 2 May 2024 PowerTrack APM release; stem.com/products; barchart.com PowerTrack Optimizer release.

### 1.13 Amber SmartShift (Australian retailer's battery automation)

Key screens. In the app's Devices tab a "Plan" section shows the next 8 hours: solar generation, household usage and price forecasts as toggleable series with grey bars marking planned battery actions; a "Show automation" toggle adds a description of "which actions SmartShift is planning and why"; manual controls Charge, Export or Preserve ("Charge, discharge or chill") for a set period, with a note of what the earnings would be if you take control; two automation modes (an older page says three). Earnings are shown as dollar figures per period, for example "$3.54 when you subtract energy costs" on a typical day and "$164" on a price-spike day. The product page's demo toggles LIVE and PLAN views across overnight, morning, afternoon and evening for a Typical and a Price Spike day.

Lesson: this is the only consumer product that labels the "why" next to each planned action; the owner should expect this as table stakes.

Sources (6 Nov 2026): amber.com.au/solar-and-battery (fetched), help.amber.com.au article 26704788728077 "Your Amber for Batteries automation plan" (403 to the fetcher; quoted through the search summary, so partly UNVERIFIED).

### 1.14 Cross-product patterns to carry into Part 2

1. Plan as a table of slots with rate, state, SoC and cost columns (Predbat) beats a plan as a chart (EMHASS, EOS, Victron) for an operator who must act on it; both views are needed, table first.
2. A per-slot reason is rare (Amber only); every product's users ask for it (Predbat debug mode, Victron complaints).
3. Uncertainty is shown as named scenarios (Predbat Base, Base10, Best, Best10; VRM's solar range "26 to 32 kWh"), not as error bars.
4. The best savings layout is a ledger: baseline, saving by lever, earnings, adjusted baseline (Schneider), plus a perfect-foresight bound reported separately (EMHASS study cases, supporting report line 511).
5. Every product exposes a manual override at slot level with an explicit glyph in the plan (Predbat), or a hold for a set period (Amber "preserve", Tesla Max Backup 72 h).
6. Offline behaviour is stated in the UI as a rule, not hidden (Victron 12 h, Schneider 48 h local schedule).
7. Fleet pages are tables of sites with last-data stamps and device or gateway sub-tables (Elum), or a map plus list (FlexMeasures); none of the home products has one.
8. Mode or tier is a visible chip or toggle (Schneider Active Mode and Autopilot; Predbat `select.predbat_mode` starting in Monitor; Tesla Operational Mode). New installs start in monitor mode everywhere.
9. A "last updated" or "last data" stamp is universal (Elum, Schneider, VRM "Last updated: Realtime").
10. Energy-flow overview: a radial figure with a node per asset and live kW (OpenEMS, Schneider, Elum's icon panel, Tesla Power Flow).

---

## Part 2. Page and tab inventory for the OnyxLeaf core product

### 2.0 How to read the inventory

Scope. The core product only: what an owner, operator, analyst or viewer of a customer company sees. The admin and founder consoles (companies, users, billing, templates, fleet-wide staggering, two-person approvals) are out of scope here and should be a separate inventory.

Every page below has the same eight fields. Purpose. Components (named so the build plan can turn each into a React component). Data (the API resources in words: the build plan turns them into FastAPI routes and Pydantic models; names are suggestions, shapes are the point). Charts (type per chart, so the chart library choice can be checked against the list). Empty and cold-start states (four cases that recur: no history yet, forecasts in stage 0, no plan yet, no edge). Roles (owner, operator, analyst, viewer). Tiers (monitor, advisory_writes, closed_loop, from U3 section 3.5). Live updates (polling cadence; the app uses TanStack Query with `refetchInterval` per resource; server-sent events are a later upgrade, not a day-one requirement).

Role rules used throughout (proposal; the owner confirms):
- owner: everything on the company's sites, including money settings (fuel price, OnyxLeaf fee, wear cost, tariff rates), control-tier changes, releases and holds, user invitations.
- operator: day-to-day: acknowledge alerts, mark Mode A slots as applied, log fuel and genset events, place holds and overrides within limits, upload CSVs; sees costs but cannot edit money settings or change the tier.
- analyst: read everything including money settings and raw exports, run counterfactuals and reports, edit nothing that affects control; can propose tariff edits for owner approval.
- viewer: dashboards and kWh only. A viewer never sees the diesel price, the OnyxLeaf fee, battery wear cost, tariff rates as money, cost columns or Rs KPIs; where a page is mostly money (Savings, Reports) the viewer sees the energy and reliability KPIs only (kWh shifted, litres avoided, genset hours, outages covered). Implemented as a server-side field filter on the resources, not a client-side hide, so the numbers never reach the browser.

Tier rules used throughout (U3 section 3.5):
- monitor: telemetry in, recommendations and counterfactual saving out; no plan is applied anywhere; every "apply" control is replaced by "recommended" wording.
- advisory_writes: Mode A plans are produced and a human applies them (installer app or OEM app); the product shows the slot table in the vendor's layout, asks for "mark as applied", and scores fidelity from read-back telemetry.
- closed_loop: the edge (Product 2) applies Mode A or Mode B plans; the product shows write logs, read-back, budgets, fallback state, and offers release and hold.
A site's tier is a chip in the page header of every site page (pattern 8 in section 1.14).

Forecast stages (guideline lines 175-179): stage 0 = pvlib clear-sky times a learned attenuation for PV, day-type persistence for load, DISCO-schedule plus feeder prior for outages, from day one; stage 1 = per-site GBM on NWP features after 4 to 6 weeks of telemetry. Every forecast chart carries a "Stage 0" or "Stage 1" badge and the word "Preliminary" until the site has 28 days of verified history.

Shared components (built once, used by many pages): SiteHeader (name, DISCO and tariff category, tier chip, control-state chip such as Following plan, Native mode, Hold until 21:00, Released, Edge offline; "last data 18:42:10" stamp); KpiTile (value, unit, delta vs previous period, "Preliminary" flag); SlotTable (the Predbat-style table, reused by Plan, Pilot and Devices and control); EnergyFlow (radial figure with nodes PV, Battery, Grid, Genset, Load, like OpenEMS and Schneider, with a hatched Grid node during an outage); TimeSeriesChart (one component with series toggles, 1-minute to daily resolution, a vertical "now" line, outage shading, TOU-window shading); ReasonChip (the plan reason, colour by category); EmptyState (icon, one sentence, one action); RoleGate and TierGate wrappers; LetteredTabs from U1 section 8.2 for the three wizards.

### 2.1 UI-01 Fleet

Purpose. The landing page after sign-in for a company with more than one site: which sites need attention, what each is doing now, and the month's saving so far.

Components. A summary strip (sites, sites with open alerts, sites on a plan, sites on native mode, month-to-date saving in Rs and litres avoided); a site table with columns Site, DISCO and category, Tier, Control state, Last data, Now (a mini energy-flow glyph: PV kW, load kW, battery SoC % with direction, grid present or out, genset on or off), Next event (next TOU peak, next forecast outage window, plan expiry), Open alerts (count with the worst severity colour), MTD saving (Rs, hidden from viewers), 7-day sparkline of daily saving; filters (DISCO, tier, state, has genset); a map toggle (sites as pins coloured by state) for companies with sites in several cities; a "New site" button (owner only).

Data. `GET /sites?include=status,kpis` returning for each site: identity (id, name, city, DISCO, tariff category, timezone), tier, control state with since-when, last telemetry time, latest snapshot (PV W, load W, SoC %, grid present, genset running), open alert counts by severity, next events (next peak window start, next outage window with probability, plan valid-until), MTD and 7-day savings (Rs, kWh shifted, litres avoided) with their verification status (preliminary or verified). One request; the server computes it from the latest snapshot table and the daily savings table so the page stays one round trip.

Charts. Sparklines (line, 7 points) per row; a small stacked bar in the summary strip for energy mix today across the fleet (PV, battery, grid, genset kWh).

Empty and cold-start. No sites: a single card "Add your first site" leading to UI-02, and a "Load demo sites" button (section 2.18) that is also offered on every empty state in the product. One site only: skip this page and land on UI-04 for that site, with Fleet still in the sidebar. No history yet (site created today): the row shows "Collecting data: 0 of 14 days" in the saving column. No plan yet: Next event shows "First plan after tariff and assets are confirmed". No edge: Control state "Monitor (no edge)".

Roles. Viewer sees the table without MTD saving in Rs (kWh shifted and litres instead). Owner sees "New site" and a per-row kebab (pause control, release, open settings).

Tiers. The Control state column is the only tier-dependent cell: monitor shows "Monitoring", advisory shows "Plan issued, awaiting apply" or "Applied by Ahmed 06:40", closed loop shows "Following plan" or "Fallback: native" or "Edge offline 00:12:35".

Live updates. Poll every 60 s; the Last data cell uses relative time and turns amber after 5 minutes of silence and red after 30.

### 2.2 UI-02 Site onboarding wizard (lettered tabs)

Purpose. Create a site in one sitting with everything the optimiser needs; drafts survive refresh; the Review tab is locked until A to D are complete (U1 section 8.2).

Tabs declared as data: A Site and connection; B PV, battery and genset; C Tariff and billing; D Control mode and limits; Review and activate (locked). URL `?tab=assets` etc.; one react-hook-form instance with a zod schema per tab; only the active panel mounts; autosave `PATCH /sites/{id}/draft` debounced.

Tab A, Site and connection. Fields: site name, company, address and map pin (lat and long drive the PV model), DISCO (LESCO, IESCO, FESCO, MEPCO, GEPCO, HESCO, SEPCO, QESCO, PESCO, TESCO, K-Electric), feeder name or id (the outage model is per feeder), timezone fixed to Asia/Karachi, contact for genset notifications (phone for SMS), data source choice: edge (Product 2, enter the enrolment code), OEM cloud (Deye or Solarman, Huawei FusionSolar, Growatt, Sungrow iSolarCloud: enter the plant id and a key), Enerlytics API, CSV upload only (ingest v1 columns from U3 section 4.2), or Demo (section 2.18). A "Test connection" button shows the first rows received and the detected points (grid-present flag required: the wizard refuses to complete if the source has no grid-present signal, per guideline line 188).

Tab B, PV, battery and genset. PV: kWp DC, inverter AC kW, tilt and azimuth or "roof: flat or pitched south", number of strings optional. Battery: brand and model (drives the Mode A grammar later), usable kWh, max charge and discharge kW, chemistry (LFP default), BMS reserve floor %, warranted throughput kWh or cycles and purchase price (owner only; computes wear cost Rs/kWh shown as a derived read-only value), temperature derating on or off. Genset: present or not, kVA and kW, minimum load %, fuel curve (three points l/h at 25, 50, 100 %, or "use default curve for this kVA"), fuel metering (none, flow meter, tank level, manual logs), start method (manual, ATS automatic, controller), critical load kW that must never be unserved. Each block has a "capabilities snapshot" placeholder that fills in once a device reports (UI-06).

Tab C, Tariff and billing. Pick a tariff from the company library (UI-03) or create one inline: category (A-1, A-2(a), A-2(b), A-2(c), B-1, B-2, B-3, B-4 and K-Electric equivalents), TOU on or off, sanctioned load kW and MDI basis, billing regime (none, net metering legacy, net billing with the NAEPP buyback rate, with the regime's expiry date), export cap kW, ICP baseline kWh per month if the site has an incremental package, fixed charges, GST treatment. A read-only preview shows today's price vector per 15 minutes as a step chart with the peak window shaded.

Tab D, Control mode and limits. Tier choice (monitor by default; advisory and closed loop are selectable only when the data source is an edge or an OEM cloud with write support, otherwise greyed with the reason); delivery mode (Mode A slots; Mode B setpoints only if the capabilities say so); limits: reserve policy (minimum SoC floor %, reserve before forecast outages as hours of critical load), max grid charge kW, export allowed and cap, genset policy (advisory only; "prefer not to start between 22:00 and 06:00"), daily cycle cap, max table writes per day (default 4), fallback on link loss (native after 5 minutes), plan expiry (48 h); the native-settings capture step: "Record the inverter's current TOU settings now" which stores the baseline the counterfactual replays (guideline line 184) and that the edge restores on fallback (U3 section 3.4 native_settings_ref).

Review and activate. Read-only summary of A to D in four cards with "Edit" links, the derived numbers (wear cost, price vector, reserve hours), the checklist of what will happen next (data collection starts, first forecasts in stage 0, first plan within 15 minutes in monitor tier), and the Activate button; progress of the activation mutation shows here.

Data. `POST /sites` from the draft; `GET /tariffs` for the picker; `POST /connections/test` for the probe; `POST /sites/{id}/native-capture` for the inverter settings capture; `GET /device-catalog` for brand, model and default fuel curves.

Charts. The price step chart in tab C (step line with shaded windows); a fuel-curve mini line in tab B.

Empty and cold-start. The wizard is itself the cold-start path; each tab has defaults so a site can be activated with only tab A's address and a data source, and the Review tab lists what was defaulted with a "Preliminary" mark on each.

Roles. Owner and operator can create sites; operator cannot see or edit the money fields in tab B (battery price, wear cost) and tab C (rates as money are visible, since an operator must understand the plan; the OnyxLeaf fee is never shown here). Analyst and viewer cannot open the wizard.

Tiers. Tab D sets the tier; it cannot be set above what the data source supports.

Live updates. None, except the Test connection probe (one request, up to 30 s with a spinner).

### 2.3 UI-03 Tariff and regime page with editor

Purpose. The company's tariff library: one record per DISCO, category and effective date, with the TOU calendar, net-billing rules and diesel price history that the optimiser and the bill rebuild share.

Components. A library table (name, DISCO, category, TOU, regime, effective from and to, used by N sites, source SRO reference, status Draft or Active or Superseded); a version timeline per tariff; the editor with lettered tabs because it is sequential: A Category and season windows (season dates, peak and off-peak hours per season and weekday type, rates per kWh per window, with a 24-hour by 12-month heatmap preview); B ICP and fixed charges (incremental consumption package base and rate, fixed charges, MDI charge Rs/kW and the MDI rule such as highest 30-minute demand in peak, FPA and other adders, GST); C Net-billing settlement (regime, buyback rate Rs/kWh, settlement period, export cap, carry-forward rules, regime expiry date and the alert that fires 60 days before); D Diesel (pump price history with dates, delivery adders, the default fuel curve library); Review (shows the resulting 15-minute price vector for a sample week and the differences from the previous version, then Publish). A "What changed" diff between versions; a "Simulate on site X" button that reruns last month's bill under the new tariff (feeds UI-12's bill comparison).

Data. `GET /tariffs`, `GET /tariffs/{id}/versions`, `PUT /tariffs/{id}/draft`, `POST /tariffs/{id}/publish`, `GET /tariffs/{id}/price-vector?from&to&step=15m` (the vector the optimiser consumes: import Rs/kWh, export Rs/kWh, MDI applicability flag per step, window label per step), `GET /diesel-prices`, `POST /diesel-prices` (date, Rs per litre, source).

Charts. Heatmap (hour by month) of the import rate; step chart of a sample day per season; a diesel price line over time.

Empty and cold-start. No tariffs: the library is seeded with templates for the common categories (marked "Template, verify against your bill"); the editor opens prefilled; the first site onboarding can finish on a template. Regime expiry passed: the tariff shows a red "Regime expired" badge and the optimiser's price vector falls back to the import rate with zero export value until the owner publishes a new version.

Roles. Owner edits and publishes; analyst edits drafts and proposes; operator reads; viewer does not see this page at all (rates are money).

Tiers. None.

Live updates. None (edit page); the library table refetches on focus.

### 2.4 UI-04 Site overview

Purpose. One screen that answers "what is the site doing now, what will it do next, and how is the month going", for a glance by an owner or a factory electrical manager.

Components. SiteHeader; the EnergyFlow radial with live kW on each node and maximum-power grey bars (OpenEMS pattern), the Grid node hatched during an outage and the Genset node pulsing while running; a "Now and next" card (current slot's intent in words, "Discharging to cover peak until 22:00, reserve kept at 35 % for the 23:30 outage window", the next three plan steps as ReasonChips, plan issued at and valid until); a KPI row for today and month to date (grid import kWh in peak, PV self-consumed %, battery cycles today, genset hours and litres, saving Rs MTD with Preliminary mark, outages covered of total); a 24-hour chart of actual vs plan (PV, load, battery power, grid, genset as stacked area, SoC as a line with the reserve line dashed, TOU windows shaded, outages hatched, plan steps as a thin band above); an alerts strip (top three open alerts); a mini plan Gantt for the next 12 hours linking to UI-09; tariff and weather footer (today's price windows, cloud cover for the next 6 hours).

Data. `GET /sites/{id}/snapshot` (latest 1-minute row plus control state and current plan step with reason); `GET /sites/{id}/timeseries?from=today&step=1m&series=pv,load,batt,grid,genset,soc,grid_present` ; `GET /sites/{id}/plans/current?summary=true` (steps with intent, reason code and text, SoC target, cost per step); `GET /sites/{id}/kpis?period=today,mtd`; `GET /sites/{id}/alerts?open=true&limit=3`; `GET /sites/{id}/forecasts/weather?hours=6`.

Charts. Radial energy flow (custom SVG); stacked area plus line (TimeSeriesChart); a 12-hour Gantt strip (custom SVG bands); KPI tiles with deltas.

Empty and cold-start. No history yet: the chart shows the last hours received and a banner "Collecting data: day 1 of 14. Forecasts are in stage 0 and marked Preliminary." Forecasts in stage 0: the Now and next card says "Plan based on clear-sky PV and typical load" and the saving KPI reads "Preliminary". No plan yet (tariff or assets incomplete): the Now and next card becomes a checklist with links to the missing wizard tab. No edge (monitor tier): the card is titled "Recommended now" and the plan band is labelled "Recommendation". Site offline: the radial greys out, the header shows "Last data 42 min ago", and the chart keeps the plan band so the operator sees what should be happening.

Roles. Viewer sees the KPI row without Rs tiles. Operator sees quick actions: Hold (keep SoC for N hours), Acknowledge alert, Log genset start. Owner adds Release to native.

Tiers. Monitor: "Recommended" wording, no quick actions except alert acknowledgement. Advisory: a "Today's slots not yet marked applied" nudge when the morning slot table has no confirmation. Closed loop: the control-state chip reflects edge state (Following plan, Fallback, Hold, Edge offline).

Live updates. Snapshot every 30 s (the 1-minute telemetry arrives in batches; 30 s keeps the "now" fresh without hammering); chart every 60 s; plan every 5 minutes; KPIs every 15 minutes.

### 2.5 UI-05 Telemetry

Purpose. The engineer's chart: any series at 1-minute resolution over any range, with outages and plan bands overlaid, and a weekday-by-hour heatmap for patterns.

Components. A range picker (today, yesterday, 7 days, 30 days, custom; Asia/Karachi); a series picker grouped by PV (DC W, AC W, per-string if available), Load (total, critical if metered), Battery (power, SoC BMS, SoC estimate with sigma band, voltage, current, temperature, trust score), Grid (import, export, present flag, voltage, frequency), Genset (running flag, kW, fuel rate l/h, tank level), Derived (net load, self-consumption %); the TimeSeriesChart with up to 8 series, two y-axes, zoom and brush, a "show plan band" and "show outages" toggle; a second view "Heatmap" (FlexMeasures time map: date on y, hour on x) for one series; a data-quality strip under the chart (gaps, stale points, clamped values per day as small bars); an Export button (CSV of the visible range, analyst and owner).

Data. `GET /sites/{id}/timeseries?from&to&step=1m|5m|15m|1h|1d&series=...` with server-side downsampling (the server picks the step when the range would exceed about 5,000 points per series and reports the step used); `GET /sites/{id}/quality?from&to` (gap and stale counts per day and per series); `GET /sites/{id}/events?types=outage,genset,override&from&to` for the overlays.

Charts. Multi-series line and area with dual axes; heatmap; small bars for quality.

Empty and cold-start. No history: the chart shows a "First data received at 09:14 today" marker and the heatmap says "Needs 7 days". CSV-only sites: a banner "Data arrives when you upload; last upload covered 1 Oct to 31 Oct" with a link to UI-06. No plan yet: the plan band toggle is disabled with a tooltip.

Roles. All roles see telemetry; export is owner and analyst only; fuel rate is a physical series, visible to all (the price is not).

Tiers. None, except that the plan band is labelled Recommendation in monitor tier.

Live updates. When the range includes now, poll every 60 s and append; otherwise static.

### 2.6 UI-06 Devices and data

Purpose. The plumbing page: where data comes from, whether it is arriving, what the devices can do, keys, and CSV upload; the FlexMeasures status page and the Elum device and gateway tables combined.

Components. Sources card list (one per source: type Edge, OEM cloud, Enerlytics, CSV; status Healthy, Stale, Error; last data time; points received today; a "Test" button; a "Rotate key" for OEM credentials, owner only); Devices table (name, brand and model, firmware, role inverter or BMS or meter or genset controller, template id and version, last seen, capabilities summary such as "6 TOU programs, SoC target, grid charge flag, export limit", control support Mode A or Mode B or none); a Capabilities drawer per device showing the capabilities message from U3 section 5.3 in plain words (primitives supported with min, max, step; slot model: max slots, resolution, fields, weekday mask; write budget per day; persistence class); a Points table (the telemetry contract points from U3 section 1.2 with Required or Optional, mapped or missing, last value, unit); CSV upload panel (drag a file, column mapping with the ingest v1 spec, a validation report: rows, time range, gaps, timezone check, duplicates; "Import" and a history of imports); the native-settings capture card (what was captured at onboarding, when, by whom, a "Recapture" button, used as the counterfactual baseline and the fallback target); an edge card when present (edge id, version, uptime, clock drift, last heartbeat, cached plan id and expiry, link-loss counter).

Data. `GET /sites/{id}/sources`, `POST /sites/{id}/sources/{sid}/test`, `GET /sites/{id}/devices`, `GET /devices/{id}/capabilities`, `GET /sites/{id}/points`, `POST /sites/{id}/uploads` (multipart) and `GET /sites/{id}/uploads`, `GET /sites/{id}/native-settings`, `GET /sites/{id}/edge`.

Charts. A 7-day bar of points received per day per source; nothing else.

Empty and cold-start. No source: a three-option chooser (Edge enrolment code, OEM cloud, CSV). No edge: the edge card reads "No edge device. Plans are delivered as daily slot tables (Mode A)". Capabilities unknown (OEM cloud with no write support): the drawer says "Read-only source: control tier limited to monitor or advisory".

Roles. Owner manages keys and recapture; operator uploads CSVs and tests sources; analyst reads and downloads validation reports; viewer does not see this page.

Tiers. Closed loop adds the edge card and the write-budget counters; monitor hides control support columns behind a "Control not enabled" note.

Live updates. Sources and edge card every 60 s; the rest on focus.

### 2.7 UI-07 Outages

Purpose. Everything about the grid's absence: the feeder's history, the announced schedule, the model's probability for the next 48 hours, and whether the battery was ready each time.

Components. KPI row (outages this month, hours without grid, hours covered by battery, hours on genset, outages entered with SoC at or above target as a percentage, the "reserve KPI" from guideline line 198); an outage heatmap (date on y, hour on x, cells coloured by minutes without grid; the single most useful chart for a load-shedding feeder); an outage log table (start, end, duration, SoC at start, reserve target at start, covered by battery or genset or unserved kWh, cause: scheduled, unscheduled, trip, maintenance; editable cause for the operator); the announced schedule editor (weekday and hour grid where the operator ticks the DISCO's published shedding slots, with a "paste from SMS" helper that parses "LESCO 18:00-20:00" style lines, UNVERIFIED feasibility until a sample is seen); the 48-hour probability chart (probability per 15-minute step as bars, with the announced slots as solid blocks and the plan's reserve SoC overlaid); a model card (per-feeder empirical hazard, sample size in days, Brier score for the last 30 days, "fleet-learned from N other sites on this feeder" when applicable).

Data. `GET /sites/{id}/outages?from&to` (events); `GET /sites/{id}/outages/stats?period`; `GET /sites/{id}/outage-schedule` and `PUT` (announced slots by weekday); `GET /sites/{id}/forecasts/outage?hours=48` (probability per step with the model stage and Brier score); `GET /feeders/{id}/model` (hazard by hour and season, sites contributing, last updated).

Charts. Heatmap; probability bars with overlaid line; KPI tiles; a small calendar strip.

Empty and cold-start. No history: the heatmap shows "No outages recorded yet" and the probability chart uses the announced schedule plus a DISCO-level prior, badged "Stage 0, prior from DISCO averages"; the model card says "0 of 30 days observed". Grid-present flag missing (CSV source without it): the whole page shows a blocking notice "This site has no grid-present signal; outages cannot be detected" with a link to UI-06 (the flag is non-optional in the edge contract, guideline line 188). Off-grid site: the page reduces to genset and reserve views and the probability chart is hidden.

Roles. All roles; the schedule editor is operator and owner; unserved kWh is shown to all, its cost only to owner, operator and analyst.

Tiers. None.

Live updates. Probability chart every 15 minutes (after each re-solve); log on focus; KPI tiles every 15 minutes.

### 2.8 UI-08 Forecasts

Purpose. Show what the optimiser believes about the next 48 hours and how good those beliefs have been, so an operator trusts (or distrusts) the plan for the right reasons.

Components. Three panels with plain tabs, PV, Load, Outage (Outage links to UI-07's chart to avoid duplication); each panel has a 48-hour fan chart (P10, P50, P90 band for PV and load; the plan uses a weighting like Predbat's PV10 weight, shown as "Plan weights the pessimistic case at 0.15") with actuals drawn over the past 24 hours; an accuracy card (daytime PV nRMSE and skill vs smart persistence; load nRMSE; outage Brier; all for 7 and 30 days; the supporting report's KPIs); a weather strip (Open-Meteo cloud cover, temperature, irradiance for the next 48 hours); a stage card (Stage 0 clear-sky times attenuation until N days of data; Stage 1 trained on M days, last retrain date; "retrain now" for analyst and owner); a "forecast bust" log (steps where actual left the P10 to P90 band by more than X %, which also triggers a re-solve per guideline line 170).

Data. `GET /sites/{id}/forecasts/pv?hours=48&quantiles=10,50,90` and the same for load (per 15-minute step with issue time and stage); `GET /sites/{id}/forecasts/accuracy?days=7,30`; `GET /sites/{id}/forecasts/weather?hours=48`; `GET /sites/{id}/forecasts/model` (stage, training window, last retrain); `POST /sites/{id}/forecasts/retrain`; `GET /sites/{id}/forecasts/busts?days=7`.

Charts. Fan chart (band plus median line plus actual line); accuracy sparklines; weather icons row.

Empty and cold-start. Day one: PV panel shows the clear-sky curve with a wide band and the badge "Stage 0: clear-sky model, Preliminary"; load shows a flat typical profile for the tariff category ("typical B-2 profile until 7 days of data"); accuracy cards read "Needs 7 days". Stage 0 to stage 1 transition: a banner "Model retrained on 42 days; accuracy improved from 21 % to 13 % nRMSE" for a week after each retrain. No weather (API down): the band widens and the strip says "Weather unavailable since 14:00; using persistence".

Roles. All roles; retrain is owner and analyst.

Tiers. None.

Live updates. Forecast series every 15 minutes; accuracy daily; weather hourly.

### 2.9 UI-09 Plan

Purpose. The product's centre: the 48-hour plan as a table and a Gantt, with the reason for every step, the SoC path against the reserve line, the cost per slot, and the overrides an operator may place. Table first, chart beside it (section 1.14 pattern 1).

Components. Plan header (plan id, issued at, valid until, solver status such as Solved within tolerance or Feasible after relaxation or Infeasible with fallback, solve time, forecast stage, objective breakdown: energy Rs, MDI Rs, fuel Rs, wear Rs, unserved penalty; "Preliminary" badge while forecasts are stage 0); a view switch with plain tabs: Table, Gantt, Slots (the Mode A compression, which is also where UI-15 shows the vendor layout); the Table (SlotTable): one row per 15-minute step for the next 48 hours with columns Time (HH:MM, Asia/Karachi, with day separators), Window (Peak, Off-peak, Shoulder), Import Rs/kWh, Export Rs/kWh, Outage p %, PV kWh (P50 with P10 in brackets on hover), Load kWh, Battery (intent as a coloured chip: Charge from PV, Charge from grid, Hold, Discharge, Native; with kW), Grid kWh (import positive, export negative), Genset (Off, Prefer start, Running, with kW), SoC % at step end with an arrow, Reserve % (the floor for that step), Cost Rs for the step, Cumulative Rs since midnight, Reason (ReasonChip plus one sentence), Flags (manual override glyph, clamped by edge, budget-limited, warning); row background tinted by window; the current step highlighted with a "now" marker; the past 24 hours available by scrolling up, greyed, with actuals beside plan; the Gantt: lanes Battery, Grid, Genset, PV curtailment over 48 hours as coloured bands, the SoC line with the dashed reserve line and the P10 SoC path as a faint second line, price as a step line above, outage probability as a hatched band, TOU windows shaded; a cost-per-slot bar strip under the Gantt; the Reasons legend (categories: Peak avoidance, Outage reserve, Cheap charge, Export value, MDI cap, Genset min-load, Battery wear cap, PV surplus, Critical load, Forecast guard, Operator hold); the Overrides panel: place a hold ("keep SoC at or above 60 % until 23:00"), force charge, force native for N hours, pin a genset decision; each override is a row with who, when, until, and it appears in the table as a glyph (Predbat pattern 5); "Re-solve now" (owner and operator, rate-limited to once per 5 minutes); a Compare drawer: plan vs "native mode replay" for the same 48 hours (the counterfactual applied forward) showing cost difference per slot, which is the forward-looking twin of UI-12's counterfactual; a Versions list (every 15-minute re-solve kept for 7 days, diff between any two).

Data. `GET /sites/{id}/plans/current` (plan object: id, issued_at, valid_from, valid_until, mode A or B, solver status, objective terms, steps[192] each with time, window, prices, outage probability, PV and load forecasts, battery intent and power, grid power, genset request, soc_target, soc_floor, cost, reason code and text, flags; the fallback object; the Mode A slot compression result); `GET /sites/{id}/plans?from&to` (versions); `GET /sites/{id}/plans/{pid}/diff/{pid2}`; `POST /sites/{id}/overrides` and `DELETE`; `POST /sites/{id}/plans/resolve`; `GET /sites/{id}/plans/current/compare?against=native`.

Charts. Table; Gantt (custom SVG bands and lines, 192 steps); cost bars; SoC fan (P50 and P10).

Empty and cold-start. No plan yet: a checklist card (tariff published, assets confirmed, data arriving, forecasts available) with the first one missing highlighted; once all are green a "Solve first plan" button. Forecasts in stage 0: the whole page carries a Preliminary ribbon and the reason chips include "Forecast guard: wider reserve while in stage 0". No history: the past-24-hour rows are absent and the Compare drawer is disabled with "Needs the native-settings capture and 1 day of data". No edge (monitor or advisory): the header says "Recommendation" or "Issued for manual apply", overrides are limited to holds (there is nothing to force), and the Slots tab is the primary view. Infeasible: the header turns amber, the fallback plan (native mode with reserve) is shown, and the reason panel lists the relaxed constraints.

Roles. Viewer sees the table without the Rs columns (cost, cumulative, prices as money are replaced by the window label) and no overrides; operator places holds and force-native, re-solves; owner also force-charges and pins genset decisions; analyst reads everything, compares, exports the plan as CSV.

Tiers. Monitor: recommendation wording, no overrides beyond annotation. Advisory: the Slots tab is primary and carries the "mark as applied" control (shared with UI-15). Closed loop: the table shows the edge's ack per step (applied, clamped, read-back mismatch) in the Flags column; overrides are real.

Live updates. Plan every 5 minutes (a new version arrives at most every 15); the "now" marker every 60 s; the actuals column every 60 s.

### 2.10 UI-10 Battery

Purpose. The battery's state, health and economics: what the optimiser is allowed to do with it and what it has cost.

Components. KPI row (SoC now with BMS vs estimate and trust score, usable kWh, SoH %, equivalent full cycles this month and lifetime, throughput kWh this month, hours above 90 % SoC in heat this month, wear cost Rs this month for non-viewers); an SoC chart for the selected range with the reserve floor dashed, the plan's SoC target as a faint line, temperature on the second axis, outages hatched; a daily cycles bar chart with the daily cap line; a cell or pack detail card (voltage spread, max cell temperature, BMS alarms) when the BMS exposes it; a BMS trust card (gap between BMS SoC and the independent estimate over 30 days, anchors found, "disagreement above 10 points raises an alert"); the limits card (reserve floor, max charge and discharge kW, daily cycle cap, temperature derating curve; editable by owner, with the note that the edge enforces min_soc_floor_pct regardless); an economics card (purchase price, warranted throughput, derived wear cost Rs/kWh, wear charged to both arms of the counterfactual; owner and analyst only).

Data. `GET /sites/{id}/battery/state` (latest), `GET /sites/{id}/battery/stats?period`, `GET /sites/{id}/timeseries?series=soc_bms,soc_est,soc_sigma,batt_temp,batt_power`, `GET /sites/{id}/battery/trust?days=30`, `GET /sites/{id}/battery/limits` and `PUT`, `GET /sites/{id}/battery/economics`.

Charts. Line with band (SoC estimate with sigma), bars (cycles), small table.

Empty and cold-start. No history: cycles and throughput say "Needs 1 day"; SoH shows the nameplate with "Not yet estimated"; the trust card says "Waiting for the first anchor (a full charge or rest period)". No BMS detail (OEM cloud with SoC only): the pack card is hidden and the trust card explains the estimate is cloud-side at 1-minute resolution.

Roles. Viewer: no Rs, no economics card; operator: no economics edits; owner edits limits and economics.

Tiers. Limits edits in closed loop show "takes effect at the next plan (within 15 minutes) and is pushed to the edge floor".

Live updates. State every 30 s; charts every 60 s.

### 2.11 UI-11 Genset

Purpose. Run hours, litres, Rs, and whether the optimiser's start and stop advice was followed; the page the diesel budget owner opens.

Components. KPI row (run hours and starts this month vs last, litres measured or estimated with the method badge, blended Rs/kWh of genset energy at the dated pump price, average load ratio %, hours below minimum load, litres avoided vs baseline with Preliminary mark); a run log table (start, stop, duration, kWh, average load %, litres, source of litres: meter, tank delta, fuel curve, manual; whether the plan requested it: Requested, Operator, Unrequested, Auto by ATS; the outage it covered if any); a fuel curve card (the three points, fitted curve vs measured points scatter, "refit from 30 days of meter data" when a meter exists); a fuel log entry form (date, litres bought, price per litre, tank reading; operator); the advice panel (pending genset requests from the plan with the reason and a "Done" or "Not now" response that feeds the fidelity score; the notification history by SMS or app); a policy card (minimum load, no-start hours, prefer stop when SoC above X, advisory-only note: "OnyxLeaf never starts or stops the genset itself").

Data. `GET /sites/{id}/genset/stats?period`, `GET /sites/{id}/genset/runs?from&to`, `GET /sites/{id}/genset/fuel-curve` and `POST /refit`, `POST /sites/{id}/genset/fuel-logs`, `GET /sites/{id}/genset/requests?status=pending|all`, `POST /sites/{id}/genset/requests/{rid}/respond`, `GET /sites/{id}/genset/policy` and `PUT`.

Charts. Bars (hours and litres per day), scatter plus fitted line (fuel curve), a Gantt-like run strip over the month.

Empty and cold-start. No genset: the page is hidden from the sidebar for the site. No fuel metering: litres carry the badge "Estimated from fuel curve" everywhere and the KPI says so. No runs yet: "No runs recorded; the genset signal is live" or, if the signal is missing, "Genset status unknown: add a run signal in Devices" (a missing signal is unknown, not off, U3 section 3.7).

Roles. Viewer: hours, litres and load ratio only, no Rs and no pump price; operator: logs fuel and responds to requests; owner: policy and pump price; analyst: refit and exports.

Tiers. Genset is advisory in every tier in release 1; the advice panel exists in all tiers; a future "controller connected" state would add a write log here.

Live updates. Pending requests every 60 s (they are time-critical); the rest on focus or every 5 minutes.

### 2.12 UI-12 Savings

Purpose. The proof page: how much the site saved against the inverter's own mode, by the three methods of increasing strength (counterfactual replay, switchback A/B, bill rebuild), with the honesty rules of the supporting report visible.

Components. A period picker (day, month, billing period, pilot window); a headline ledger in the Schneider style: Baseline cost (native mode replay: energy, MDI, fuel, wear), OnyxLeaf cost (same four lines), Saving (Rs and % of energy cost), broken down by lever (peak avoidance, outage reserve and genset avoided, cheap charging, export value, MDI), with the perfect-foresight upper bound shown separately and labelled "not achievable, for reference" (supporting report line 511); a method tabs row (plain tabs): Counterfactual (daily bars of saving with the method's assumptions listed: 1-minute replay, same tariff and adders both arms, wear charged to both, diesel at measured litres or fitted curve, dated pump price, GST treatment stated), Switchback (the calendar of native and OnyxLeaf weeks from UI-13, a weather-normalised comparison per pair of weeks with a confidence note, "4 of 8 weeks complete"), Bill comparison (the DISCO bill as entered or uploaded vs the rebuilt bill for both arms, line by line: units by window, MDI kW and charge, adders, net-billing credit, GST; a "rebuilt within 2 % of the actual bill" check that validates the tariff model itself); a KPI grid (the supporting report's KPI table: peak-window import kWh, genset hours and litres, MDI kW, forecast quality, constraint violations such as SoC below reserve at outage start or unserved critical load or export above cap, battery throughput per saved rupee, control health, BMS trust); the fee check card (saving vs the monthly OnyxLeaf fee as a ratio with the pilot gate "at least 3 times the fee"; owner only; this is the only place the fee appears); an export row (PDF report via UI-14, CSV of daily savings).

Data. `GET /sites/{id}/savings/summary?period` (ledger with method, verification status Preliminary or Verified, assumptions block); `GET /sites/{id}/savings/daily?from&to` (per day: baseline cost, plan cost, saving, by lever, flags such as data gap or override day); `GET /sites/{id}/savings/switchback` (weeks with arm, weather index, cost, saving, completeness); `GET /sites/{id}/bills` and `POST /sites/{id}/bills` (entered or uploaded DISCO bill); `GET /sites/{id}/savings/bill-rebuild?billing_period`; `GET /sites/{id}/kpis?period`; `GET /sites/{id}/fee` (owner only).

Charts. Waterfall (baseline to OnyxLeaf by lever); daily bars with a line for cumulative; paired bars for switchback weeks; a bill table; KPI tiles.

Empty and cold-start. No native-settings capture: the page is blocked with "Record the inverter's own settings first (Devices and data), otherwise there is no baseline" because the counterfactual replays those exact settings (guideline line 184). Fewer than 1 day of data: "First saving figure tomorrow after 06:00". Forecasts in stage 0 or fewer than 28 days: every figure carries "Preliminary". No switchback yet: the tab shows UI-13's call to action. No bill uploaded: the Bill tab shows the rebuilt bill alone with "Upload the DISCO bill to compare". Days with overrides or data gaps are flagged in the daily bars and excluded from the headline when the owner ticks "verified days only".

Roles. Viewer sees the KPI grid's energy and reliability tiles only (kWh shifted, litres avoided, genset hours, outages covered) and no ledger; operator and analyst see the ledger and methods; owner alone sees the fee card.

Tiers. Monitor: the ledger is titled "Estimated saving if the plan had been followed" and switchback is unavailable (no arm was ever applied); advisory: days without a "marked as applied" slot table are flagged "unconfirmed apply" and the fidelity score is shown next to the saving; closed loop: fidelity from read-back.

Live updates. Daily figures refresh after the nightly settlement job (the page shows "computed 06:10"); nothing polls faster than 15 minutes.

### 2.13 UI-13 Pilot set-up wizard (lettered tabs)

Purpose. Pre-register the pilot so the saving cannot be argued about afterwards: baseline window, captured native mode, the switchback calendar and the success criteria, in the order the protocol runs (guideline lines 196-199).

Tabs. A Baseline window (start and end dates of the instrumented baseline, usually 2 weeks; data completeness check for the window so far; the list of KPIs that will be computed during it); B Native-mode capture (shows the capture from UI-06 or triggers it; the inverter's slot settings in the vendor's layout, read-only; a confirmation "these are the settings the counterfactual replays"; a re-capture if the installer changed anything); C Switchback calendar (a calendar grid where the operator paints weeks Native or OnyxLeaf; default 2 and 2 repeated once; a rule check: arms alternate, each arm at least 7 days, no arm during a declared holiday closure; exportable as an .ics so the installer knows which Monday to switch); D Success criteria (the gates from guideline line 199 prefilled: saving at least 3 times the fee for C&I, genset run-hours down at least 15 % for genset sites, at least 90 % of outages covered without deeper cycling than baseline; editable thresholds; the pre-registered metric list with its definition and resolution; sign-off fields for OnyxLeaf and the customer); Review and start (summary, "Start pilot" which locks tabs A to D; later edits create an amendment with a reason, visible on UI-12).

Data. `GET /sites/{id}/pilot` and `PUT /sites/{id}/pilot/draft`, `POST /sites/{id}/pilot/start`, `POST /sites/{id}/pilot/amend`, `GET /sites/{id}/native-settings`, `GET /sites/{id}/quality?from&to` (completeness for tab A), `GET /sites/{id}/fee` (for the gate, owner only).

Charts. A completeness bar in tab A; the calendar in tab C.

Empty and cold-start. Most sites never open this page; it is reachable from Savings and from the site menu. If the native capture is missing, tab B blocks the wizard until it is done. If the site has no genset, the genset gate in tab D is hidden.

Roles. Owner starts and amends; analyst drafts; operator paints the calendar; viewer has no access.

Tiers. Monitor: the wizard explains that only the counterfactual arm is possible and the calendar is disabled; advisory and closed loop: full.

Live updates. None.

### 2.14 UI-14 Reports

Purpose. The documents that leave the product: a monthly savings report for the customer's CFO, a pilot report at the end of the switchback, an operator's daily sheet, and raw exports.

Components. A report list (type, period, generated at, by, status, download PDF or XLSX); a "Generate" dialog (type, period, sites, language English only for now, include or exclude money lines for a viewer-safe version); a preview pane rendering the report's sections in the browser before export (the same React components as the pages, printed via a print stylesheet; the PDF is rendered server-side from the same data, not from the browser); scheduled reports (monthly on the 3rd to a list of e-mail addresses; owner); report templates: Monthly savings (ledger, methods, KPI grid, assumptions and GST note, "Preliminary" marks carried through), Pilot report (pre-registered criteria vs results, the switchback table, forecast accuracy, control health), Daily operations sheet (yesterday's plan vs actual, outages, genset runs, alerts), Energy only (kWh by source, outages, genset hours; the viewer-safe one).

Data. `GET /reports?site`, `POST /reports` (type, period, options) returning a job id, `GET /reports/{id}` with status and download URL, `GET /reports/schedules` and `PUT`.

Charts. Reuse of UI-12 and UI-04 charts in print form.

Empty and cold-start. No reports: a card "Your first monthly report will be available on 3 December" with the Generate button for a partial-month preview badged Preliminary. Fewer than 7 days of data: Generate offers only the Daily operations sheet.

Roles. Owner and analyst generate everything; operator generates the daily sheet; viewer downloads the Energy only reports already generated for them.

Tiers. The Pilot report requires advisory or closed loop; otherwise it is a "Counterfactual-only report" with that title.

Live updates. The job status polls every 5 s while generating.

### 2.15 UI-15 Devices and control

Purpose. The control plane for one site: what can be written, the Mode A table in the vendor's own layout, the write log with read-back, the fallback state, and the release and hold controls; the page an installer stands in front of the inverter with.

Components. SiteHeader with the control-state chip large; a Capabilities card (from UI-06, summarised: supported primitives, slot model, write budget used today "2 of 4 table writes, 14 of 100 register writes", persistence class, template and firmware version, conformance check date for closed loop); the Mode A table rendered in the vendor's layout (Deye: 6 programs in a ring with Time, Power W, Capacity %, Charge source Grid or Gen, Mode General or Backup or Charge, Sell flag; Huawei: up to 14 TOU periods with Start, End, Charge or Discharge, Days, plus the working-mode value and the fixed periods if used; Growatt SPH: 3 battery-first and 3 grid-first windows with begin, end, enable and the global rates and SoC limits; Growatt TL-XH: 9 windows with begin, end, mode; Sungrow and Voltronic: the emulated slot list with the EMS mode or POP and PCP values the edge will set at each boundary); each row shows the plan's intent in words beside the register values, so the installer can type it into the vendor app; "Copy as text" and "Print" of the table; a "Mark as applied" control per day's table with the time, the person and an optional photo of the vendor app screen (advisory tier); a Read-back panel (for each slot or register: requested, read back, match or mismatch, when; the edge's ack codes OK, CLAMPED, READBACK_MISMATCH, BUDGET_EXHAUSTED, OVERRIDE_LOCAL; a fidelity score for the day as the share of steps where telemetry matched the intent); the Write log (who, why, before, after, when, result; filter by primitive and day; this is the audit log of U3 section 3.3 and the evidence in savings disputes); the Fallback card (rule: native after 5 minutes of link loss or at plan expiry; the native_settings_ref with the capture date; the current state Following plan, Fallback native, Hold, Released; the edge's cached plan expiry; "Test fallback" for owner in closed loop, which releases and verifies the restore); Controls: Hold (keep SoC at or above X % until a time, or "hold native for N hours"), Release to native (immediate, with a reason, confirmation dialog, owner and operator), Resume control (owner), Pause writes for this site (owner); an Overrides list shared with UI-09.

Data. `GET /sites/{id}/control/state`, `GET /sites/{id}/control/capabilities`, `GET /sites/{id}/plans/current/slots?layout=vendor` (the compressed slot table rendered against the device's slot model), `POST /sites/{id}/control/applied` (date, who, photo), `GET /sites/{id}/control/readback?date`, `GET /sites/{id}/control/writes?from&to`, `GET /sites/{id}/control/fallback`, `POST /sites/{id}/control/release`, `POST /sites/{id}/control/hold`, `POST /sites/{id}/control/resume`, `POST /sites/{id}/control/pause`.

Charts. A 24-hour strip showing intent vs read-back vs telemetry-inferred mode (three thin lanes) for fidelity at a glance.

Empty and cold-start. Monitor tier: the page shows the capabilities and the slot table titled "What the plan would set" with no apply control and a note on how to enable advisory writes. No capabilities yet (device not reported): "Waiting for the device to report its capabilities; for OEM-cloud sites this comes from the brand template" with the brand's generic slot model shown as a preview. No plan yet: the slot table area shows the checklist from UI-09. No edge: read-back is inferred from telemetry only ("inferred from behaviour, not read from registers") and the write log holds only human "applied" entries.

Roles. Viewer: no access. Operator: mark as applied, hold, release. Owner: all, including pause and resume and test fallback. Analyst: read-only including the write log export.

Tiers. This page changes most with tier: monitor (preview), advisory (slot table, mark as applied, inferred fidelity), closed loop (ack codes, read-back, budgets, fallback test, release and resume). The tier itself is changed in site settings, not here, and only by the owner after the conformance check for closed loop.

Live updates. Control state every 15 s while the page is open (an operator may be standing at the inverter); read-back and write log every 60 s.

### 2.16 UI-16 Alerts

Purpose. One list of what needs a human, with severity, ownership and a rule editor; the site-level alerts strip on UI-04 is a view of this.

Components. An inbox table (time, site, severity Critical or Warning or Info, category, title, state Open or Acknowledged or Resolved, assigned to, age); filters by site, severity, category and state; a detail drawer (what happened with numbers, what the product did automatically such as "switched to advisory mode" or "re-solved with wider reserve", suggested action, links to the page that shows it, acknowledge and resolve with a note, a timeline); the rule editor (per company with site overrides: thresholds and channels for each category; channels in-app, e-mail, SMS for genset requests and critical alerts; quiet hours; escalation after N minutes); a category list that comes from the documents: Data (source stale 15 minutes, gap above 1 hour, clock drift, CSV import failed), Control (read-back mismatch, write budget exhausted, local override detected, fallback entered, edge offline, plan expired without replacement, plan infeasible), Reserve (SoC below reserve at outage start, unserved critical load, outage not covered), Battery (BMS vs estimate gap above 10 points, temperature high, hours above 90 % SoC in heat, daily cycle cap hit), Genset (unrequested start, running below minimum load, fuel low, request not acted on within the window), Forecast (bust above X %, weather feed down, stage change), Tariff (regime expiry in 60 days, SRO change notice, bill rebuild differs from actual by more than 2 %), Savings (negative saving day, unconfirmed apply day), Product (fee or contract notices, owner only).

Data. `GET /alerts?site&severity&state&category&from` (paged), `GET /alerts/{id}`, `POST /alerts/{id}/ack`, `POST /alerts/{id}/resolve`, `POST /alerts/{id}/assign`, `GET /alert-rules?company&site` and `PUT`, `GET /alerts/counts` (for the sidebar badge and UI-01).

Charts. A 30-day bar of alerts per day by severity at the top of the inbox; nothing else.

Empty and cold-start. No alerts: "Nothing needs you. Data is arriving from 3 sites." Day one: the Data category will fire first (stale source during set-up); the default rule set suppresses Control and Savings alerts until a plan exists and 7 days of data are in.

Roles. Viewer sees Info and Warning alerts of the energy kind (outage, genset, data) but no Savings or Product alerts; operator acknowledges and resolves; owner edits rules and assignments; analyst reads all and exports.

Tiers. Control alerts exist only in advisory (unconfirmed apply, inferred mismatch) and closed loop (the full edge set).

Live updates. Counts every 30 s (sidebar badge); the inbox every 60 s; SMS and e-mail are server-side.

### 2.17 Pages not in the UI-01 to UI-16 list that the inventory needs

- Site settings (route `/sites/:id/settings`, plain tabs General, Assets, Tariff, Control tier, Notifications, Members): the post-onboarding editor of the same data as UI-02, reusing its panels without the wizard chrome; the control-tier change lives here with its confirmation flow (conformance check status, installer sign-off record) and is owner-only.
- Company settings (`/settings`): members and roles, the demo-sites switch, report schedules, API keys for the customer's own BI tool (owner).
- Account (`/account`): profile, password, sessions, notification preferences.
- Sign-in, sign-up by invitation, password reset, and the "No sites yet" first-run page (section 2.19).
- A global search (sites and alerts) in the top bar, optional in release 1.

### 2.18 Demo mode: three simulated sites with 90 days of history

Why. Every page above has an empty state, and a prospect who sees twelve empty states on day one does not come back. Demo mode makes every page live before any real telemetry exists, and it doubles as the digital twin the build plan needs for contract tests (U3 section 4.3). The demo data is generated by the same simulator that runs the counterfactual replay, so a demo site is not a mock-up: the plan, the savings ledger and the alerts are produced by the real optimiser, forecaster and settlement code running on synthetic 1-minute telemetry.

The three sites (names are placeholders; the owner may rename):

1. "Rahim Textiles, Faisalabad" (FESCO, B-2 TOU, 11 kV feeder with evening load shedding). 400 kW sanctioned load, 300 kWp PV, 215 kWh LFP at 100 kW, 250 kVA genset with a flow meter, ATS start. Two-shift load profile (06:00 to 22:00 heavy, night light), Friday half-day, two Eid closures inside the 90 days. Feeder outages: a scheduled 18:00 to 20:00 slot four days a week in the hot months plus random 30 to 90 minute trips. What the demo shows: peak-window import cut by discharging after 17:00 while keeping a reserve for the 18:00 slot, genset starts avoided on the days the battery covered the outage, MDI held under the sanctioned limit, a switchback calendar already 4 of 8 weeks in, a "genset ran unrequested" alert, a read-back mismatch alert resolved by the installer, and a monthly savings ledger with a 3.4 times fee ratio (a realistic, not flattering, number; the value is UNVERIFIED until the simulator is run and must not be quoted externally).

2. "Metro Mall, Lahore" (LESCO, A-2(c) commercial TOU, net billing at the current NAEPP buyback rate with the regime expiring inside the demo year). 250 kW load, 200 kWp PV, 100 kWh LFP, no genset (the mall has a UPS for critical load, modelled as a 15 kW critical load). Weekend-heavy load, long summer cooling peak. What the demo shows: export nearly worthless under net billing so the plan charges from PV surplus rather than exporting, cheap off-peak grid charging on cloudy days, the export cap respected, a "tariff regime expires in 60 days" alert, the bill comparison tab with an uploaded sample bill rebuilt within 2 %, and a viewer-role login that shows the page without any rupee figure.

3. "Chak 42 Farm, Rahim Yar Khan" (off-grid, no DISCO, MEPCO territory for reference only). 60 kW tube-well and cold-store load, 80 kWp PV, 120 kWh LFP, 60 kVA genset with tank-level metering only, manual start by the farm manager. What the demo shows: the genset page as the main page (advice panel with "prefer start at 04:30 to carry the irrigation pump at full load"), minimum-load violations before OnyxLeaf and none after, fuel estimated from the curve with the "Estimated" badge, outages page hidden, the reserve policy expressed in hours of critical load for the cold store, SMS-style request history, and a savings ledger in litres first and rupees second.

Mechanics. Demo sites belong to a demo company that every new company can clone with one click ("Load demo sites") from any empty state or from Company settings; the clone is marked with a DEMO ribbon on every page, its sites are excluded from the company's real KPIs and reports, its history is regenerated nightly so the 90-day window always ends yesterday, and it can be removed in one click. The plan, forecasts and alerts for demo sites run on the normal 15-minute schedule so the pages change during a sales call. The simulator is seeded, so two demos look the same on the same day. Roles work on demo sites exactly as on real ones, which makes the demo the role-permission test bed.

Build note. The simulator needs: a PV model from pvlib with cloud days drawn from a seasonal distribution; a load generator per site type (shift pattern, weekday and holiday calendar, temperature coupling for cooling); an outage generator per feeder (scheduled slots plus a hazard for trips); the genset fuel curve; a battery model with wear; the native-mode TOU settings captured for each demo site (so the counterfactual has a baseline); and the tariff versions for FESCO B-2 and LESCO A-2(c) plus a net-billing regime with an expiry date. All numbers in the demo tariffs must be labelled "illustrative" and dated.

### 2.19 First-time user flow, sign-in to first plan

Step 1, sign-in. The owner receives an invitation e-mail from OnyxLeaf (or from an installer partner's white-label), sets a password or uses Google, and lands on the "No sites yet" page: two cards, "Add your first site" and "Explore with demo sites", plus a one-line statement of what the product will and will not do (it plans and verifies; it never starts a genset by itself; it starts in monitor mode). Choosing demo takes them to the demo Fleet page within seconds and every later step still applies to their real site.

Step 2, site and connection (UI-02 tab A). The owner types the site name and address, picks the DISCO and feeder, and chooses how data arrives. For most pilots this is an OEM cloud (plant id and a key) or an edge enrolment code read off the Product 2 box; for a site with only a datalogger export it is CSV. "Test connection" shows the first rows and the detected points; if no grid-present flag is found the wizard says so and offers the CSV column mapping or the edge path, because without it there are no outages to plan around. The draft is saved as they go.

Step 3, assets (tab B). PV kWp, battery brand and usable kWh, genset kVA and fuel curve or "use default for this kVA", critical load. The brand choice shows the slot grammar the plan will later be compressed into ("Deye: 6 programs per day"), so the owner understands what Mode A will look like. The battery's price and warranted throughput are optional now and asked for again on the Battery page later; without them the wear cost uses a dated LFP default marked Preliminary.

Step 4, tariff (tab C). The owner picks the DISCO and category template, checks the peak hours against the printed bill, enters the sanctioned load and the billing regime, and sees the day's price vector drawn. If they have the last bill they upload it now; the bill-rebuild check on the Savings page will later tell them whether the tariff model reproduces it.

Step 5, control mode (tab D). Monitor is preselected and explained ("OnyxLeaf will plan and estimate savings; nothing is written to the inverter"). The owner sets the reserve policy in hours of critical load, the daily cycle cap, and clicks "Record the inverter's current settings now" so the baseline is captured; on an OEM cloud this reads the TOU table, on CSV it is a form the installer fills from the inverter screen. The wizard explains that advisory writes and closed loop are unlocked later from Site settings after the first week of data and, for closed loop, after the edge's conformance check.

Step 6, review and activate. The four cards, the derived numbers, and what happens next: "Data collection starts now. Forecasts begin in stage 0 (clear-sky PV, typical load) and are marked Preliminary. Your first plan will appear on the Plan page within 15 minutes as a recommendation. Savings figures start tomorrow and become Verified after 28 days." Activate.

Step 7, the first plan. The owner is taken to UI-04, where the energy flow fills in as the first telemetry arrives and the Now and next card shows the checklist turning green. Within 15 minutes the first plan is solved; UI-09 opens with the Preliminary ribbon, the table of 192 steps, reason chips that read "Forecast guard: wider reserve while in stage 0" and "Outage reserve: announced 18:00 slot", and the Slots tab showing the day's six Deye programs in the vendor's layout. In monitor tier nothing is applied; the owner can print the slot table and hand it to the installer, which is in effect advisory mode by hand, or wait a week and switch the tier. The next morning the Fleet row shows "Collecting data: day 2 of 14" and the first Preliminary daily saving.

### 2.20 Navigation

Sidebar groups (left, collapsible, icons with labels; Boxlytics style):
- Fleet: Fleet (UI-01), Alerts (UI-16, with a count badge), Reports (UI-14).
- Site (appears once a site is selected; group header is the site switcher): Overview (UI-04), Plan (UI-09), Telemetry (UI-05), Forecasts (UI-08), Outages (UI-07), Battery (UI-10), Genset (UI-11, hidden when the site has none), Savings (UI-12), Devices and data (UI-06), Devices and control (UI-15), Site settings.
- Company: Tariffs (UI-03), Pilot set-up (UI-13, listed under the site menu as well), Settings, Members.
- Footer: account menu, DEMO ribbon toggle when demo sites exist, help.
Viewer sees only Fleet, Alerts, Reports (energy only), Overview, Telemetry, Forecasts, Outages, Battery, Genset, Savings (energy KPIs only).

Site switcher. A combobox in the Site group header listing sites with their state dot and last-data age, searchable; switching keeps the current page (Plan on site A becomes Plan on site B), the route being `/sites/:siteId/plan`. The last site is remembered per user. With one site the switcher shows the site name without a dropdown.

Breadcrumbs. Company > Site > Page > sub-view, rendered in the page header under the SiteHeader chips, for example "OnyxLeaf Demo > Rahim Textiles > Plan > Slots"; lettered-tab wizards add the tab letter and label ("Site onboarding > B. PV, battery and genset"). The URL carries the same structure, with `?tab=` for wizards and `?view=` for plain tabs so links are shareable (U1 section 8.2 point 2).

Top bar. Site search, the alerts bell with count, the user's role shown as a chip (so a viewer understands why figures are missing), and the tier chip of the current site.

Mobile. The sidebar collapses to a bottom bar with Overview, Plan, Alerts, Genset (for the operator in the field); the slot table and the "mark as applied" control must work on a phone, because that is where the installer uses them.

### 2.21 Design rules

Units and formats.
- Money: "Rs 1,234,567" with a space after Rs, three-digit grouping (not lakh and crore grouping; set the number formatter explicitly rather than relying on the browser's en-PK locale, whose grouping differs between engines; UNVERIFIED which engines use lakh grouping for en-PK). No decimals above Rs 1,000; two decimals for Rs/kWh ("Rs 41.50/kWh"). Negative money as "-Rs 1,200", never in brackets. "PKR" only in exports and API fields.
- Energy and power: "kWh" and "kW" with one decimal below 100 and none above ("12.4 kWh", "312 kW"); MWh only in fleet totals above 10,000 kWh. Battery as "kWh usable" and "SoC 62 %". Litres as "L" ("1,240 L"), fuel rate "L/h", specific fuel "L/kWh".
- Percent with a space before the sign ("62 %"), no decimals except accuracy metrics ("nRMSE 13.2 %").
- Time: Asia/Karachi for everything a user sees, 24-hour clock, no DST; slot labels as the start time "18:15" with the slot length implicit, and ranges as "18:00 to 18:15" in tooltips; day separators in long tables as "Fri 7 Nov"; relative ages for freshness ("4 min ago") with the absolute time in a tooltip; dates as "7 Nov 2026" and in exports as ISO 8601 with offset.
- Rates and windows named as on the bill: Peak, Off-peak (and Shoulder where the category has it), never "on-peak".

Colours by energy source, used identically in every chart, chip and the radial figure: PV amber-yellow; Battery green (charge) and teal (discharge) or one green with direction shown by sign; Grid blue, with import darker and export lighter; Genset red-orange; Load slate grey; Unserved magenta hatched; Outage hatched grey background; TOU peak window a warm tint, off-peak a cool tint; Reserve line dashed red; Plan intent bands use the source colours at lower opacity; reason chips use a neutral palette by category, not the source colours, so a chip is never confused with a series. Check the palette for colour-blind contrast (amber vs red-orange is the risk pair; add icons on nodes and chips).

Marks and words.
- "Preliminary" is a visible mark (not just a tooltip) on every forecast, plan and saving figure until the site has 28 verified days and the forecast is stage 1; "Verified" replaces it per figure only when the method that produced it is complete (counterfactual with no data gaps, switchback with all pairs, bill rebuilt against an actual bill). "Estimated" marks litres from a fuel curve and SoC from an estimate rather than a BMS.
- Never the words "guaranteed", "optimal", "optimum" or "maximum savings" anywhere in the UI, reports or empty states; say "planned", "lowest-cost plan found", "within tolerance", "expected", "verified saving". The solver status reads "Solved within tolerance", not "Optimal". The perfect-foresight bound is labelled "not achievable, for reference".
- Every money figure has a method next to it (Counterfactual, Switchback, Bill) and a date of computation.
- Every page has a "last data" stamp; stale data greys the figures rather than hiding them.
- Advice is phrased as a request with a reason ("Prefer genset start at 04:30: irrigation load above battery power, SoC 38 %") and never as a command.
- Empty states have one sentence and one action, plus the demo offer.
- Tables over charts for anything an operator acts on; charts over tables for anything an owner glances at.
- Role and tier differences are shown, not silently removed: a viewer sees "Hidden for your role" placeholders where a money tile would be, a monitor-tier site sees "Recommendation" headers.
- Accessibility: ARIA tablist and tabpanel pairs that resolve (U1 section 8.1 notes Boxlytics breaks this), keyboard navigation of the slot table, 16 px side gutter on phones, no horizontal scroll except inside the slot table.

### 2.22 Open questions for the owner

1. Roles: is "analyst" a real buyer role at a Pakistani textile mill, or should it fold into owner with an "export" permission? The inventory keeps four roles because the fee and fuel-price hiding needs a viewer and the installer needs an operator.
2. Should the OnyxLeaf fee appear in the product at all (the Savings fee card), or only in invoices? The pilot gate is defined as a multiple of the fee, which argues for showing it to the owner.
3. Genset notifications by SMS need a provider and a cost; is WhatsApp acceptable instead, and who pays?
4. Which inverter brand is the first demo site, since the Slots tab must render a real grammar on day one (Deye is assumed).
5. Does a tariff template library shipped with the product create a liability if a rate is wrong? The inventory marks templates "verify against your bill" and dates them.
6. Lakh grouping ("12,34,567") vs three-digit grouping for rupees: the inventory chooses three-digit; the owner should confirm what his customers' CFOs expect.
7. Is a map on the Fleet page worth building in release 1 (most fleets will be under ten sites)?
8. Should the pilot wizard be visible to all customers or only to sites flagged as pilots?

### 2.23 Verification log

Fetched and read on 6 Nov 2026: Predbat docs (plan card, web interface, customisation, what-does-predbat-do, output-data); Victron Dynamic ESS draft manual and two community articles (text only, screenshots not viewable); Tesla support pages via search summaries only (direct fetch returned 403); EMHASS intro and usage guide (web UI specifics not confirmed); OpenEMS introduction and UI architecture pages and FENECON's standard widgets page; Akkudoktor EOS README and changelog via search; Enerlyticslab pages returned only a title (JS-rendered), so the supporting report's description was used; NeoTAQ via a LinkedIn post summary and businessbytes.pk; Elum ePowerMonitor LITE manual page; Schneider Microgrid Advisor 2017 handout PDF (read as images); FlexMeasures v0.25.0 asset-data page; Stem and PowerTrack press releases; Amber product page (help article 403, quoted via search).

UNVERIFIED items: Victron savings widget; Tesla in-app savings figure; EMHASS plot legend and entity names; OpenEMS ToU widget contents; EOSdash tab names; NeoTAQ screens; Stem screens; Amber help-article quotes; the demo fee ratio; en-PK number grouping behaviour; SMS parsing of DISCO schedules.

No code was copied from any product. The Predbat table column vocabulary and colour-threshold idea, the Schneider ledger layout, the OpenEMS radial figure and the FlexMeasures heatmap are design patterns, re-expressed here for OnyxLeaf's own components.
