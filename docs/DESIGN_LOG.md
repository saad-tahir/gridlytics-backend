# Design log

Newest first. Each entry says what was decided, why, and where the plan (`EMS_BUILD_PLAN.md`) says otherwise.

## 2026-11-12 Round 7 UX backend asks

The owner approved the UI/UX redesign (the web repository's `docs/UI_UX_MASTER_PLAN.md`); its Part H11 lists the backend asks K1 to K35 the redesigned
pages use, each with a fallback. They are built on branch `audit/ux-k`, one or more commits per ask ("UX K<nn>: ..."), each answer recorded in the web
repository's `docs/API_CONTRACT.md` under "UX backend asks (plan H11)" and typed at the end of `frontend/src/types/api.ts`. Where an ask needs an open
question of the owner, the recommended answer ships and is logged here and in the web repository's `docs/BUILD_NOTES.md`.

### Batch k1: K1, K4, K24, K25 (12 Nov 2026)

- **K1, the lifecycle** (plan §0.3.1, B21.2 item 3, C19.2 item 8, D9.1): `lifecycle.py` works out the stage track (Set-up, Collecting data,
  Recommending, Delivering, Verifying, Reporting; each done, now, waiting or not used, with its words and one next step's key) from a few indexed reads,
  and every site answer and fleet row carry it. Decisions the plan left open:
  - **Set-up's step** is the first of A to C that activation still asks for: the activation's refusal and the track read one rule
    (`routes_sites._activation_missing`), so "Draft: step C of 6" and "Add the grid connection and tariff (tab C) before activating" never disagree. D
    has a default for every field and E is not needed to activate, so neither holds a draft back; with A to C done the line is "Set-up: not
    activated" (see the check below on plan C2.3).
  - **Collecting data** counts 14 days from the first reading, as the fleet's `collecting_day` does; while collecting, no reading for 30 minutes (the
    fleet's online rule) reads "No data since 09:10".
  - **Recommending** is done once the tier is above monitor; at monitor it stays the current stage, so a monitor site's track never folds. The plan's
    "only an administrator raises the tier" holds: the track only names the step (`raise_tier` for `change_tier`, once the native capture exists).
  - **Delivering** is never "not used": an edge says what it can control when it connects and a CSV source supports advisory writes once a file
    carries the inverter's mode, so a monitor-only source reads as waiting, in words that say for what (read from `PUT /tier`'s own refusal: see the
    check below).
  - **Verifying** is the pilot: not used until it starts, then "Pilot week 3 of 8 · today runs the plan" from the switchback calendar (weeks from the
    first block's first day, `verify.pilot_progress`), done once the last block has ended. The go/no-go decision is K28's.
  - **Reporting** is reached when the whole previous calendar month (Pakistan time) ran on a plan, counted from the site's first plan made (every plan
    row is kept for good: `plans.prune` trims steps and tables only); it is done once that month's monthly savings report (the statement) exists.
  - **The current stage** is the first that is "now", else the furthest of the first four that is done, so a site whose every stage in use is done
    reads "Delivering · advisory", as the plan's chip does. `all_done` (the track folds) is every stage done or not used.
  - **Next steps by permission**, decided by the server: a viewer reads states only, with the energy report as the one action (plan §0.3.1, D2.4); a
    machine key is not served the lifecycle (see the check below). The site switcher's list (`GET /api/sites`) does not carry it: that answer is
    polled every minute on every page and was made cheap on purpose (round 7, performance-live-08); the switcher's popover reads `GET /fleet` as it
    opens (plan B17.5), which carries it.
- **K4, the role matrix** (plan G9 item 1): `GET /api/meta` answers `role_matrix`, `{permission: [roles]}` straight from `auth.PERMISSIONS`, the roles in
  `auth.ROLES` order, the same for every reader. No money.
- **K24, a gas genset's fuel log** (plan E6 item 6, A8.5): `POST /genset/fuel` takes, for a captive-gas or RLNG genset, `mmbtu` as billed, or `hm3`
  with `calorific_mmbtu_per_hm3` from the bill, stored as MMBtu (hm³ x the calorific value, to three decimals); never an assumed calorific value. The
  calorific value is held to 2 to 5 MMBtu per hm³ (pipeline gas of about 950 BTU per cubic foot is 3.35, RLNG about 3.7, a lean captive field about
  2.6), which refuses a figure typed in BTU per cubic foot or MJ per m³. A diesel genset's log stays in litres and refuses the gas forms. `litres` sent
  for a gas genset is still read as MMBtu, as the web app of before sent it (round 7 sweeps' fuel unit). The stored amount stays under `litres`, as
  every fuel figure of a gas genset does (`savings_export.fuel_unit`), with `unit` and `entry` beside it; `fuel_logged` adds only the logs in the
  genset's unit. The request is a strict model (`FuelLogIn`): a yes or no, or text, for a number is refused. Owner's question Q9 (the fuel log in
  MMBtu as billed or from a meter reading): the recommended yes ships.
- **K25, the telemetry CSV in Karachi time** (plan E6 item 7): `ts_karachi` (ISO 8601 with +05:00) is the first column, `ts_utc` the second, then the
  stored series as before. Owner's question Q58 (the telemetry CSV leads with Karachi time): the recommended yes ships.

### Batch k1, the check (12 Nov 2026)

Independent checkers read batch k1 against the plan, the rules and the code; what they found and how it was settled ("UX K01 check" and "UX K24
check" commits):

- **A machine key is not served the lifecycle.** `sites:read` is the site's settings (audit security-roles-07: the site's data, the pilot and the
  savings are in no scope), and the track's words spoke of all three ("Pilot week 2 of 6 · today runs your own settings", "October statement made",
  "No data since 19:41"). `_site_answer` leaves `lifecycle` out for a key, as before K1; a person's answer carries it. The role walk proves it.
- **Delivering offers the raise of the tier only when `PUT /tier` takes it.** A CSV source is capable of advisory writes once chosen
  (`sites._source_capable`), but `PUT /tier` refuses it until a file carries `inverter_mode_active` (`no_inverter_mode`), so the track offered
  `raise_tier` to a refusal. The refusals moved into `routes_sites._tier_refusal(site, tier)` (`above_capable`, `no_inverter_mode`, `no_capture`),
  which both `PUT /tier` and the track read (the advisory tier's code is the fact `tier_block`): such a site reads "Monitor only until a file carries
  the inverter's mode" and offers the CSV upload. This replaces the line above that a monitor-only source is told apart by `tier_capable` alone.
- **"The first plan arrives within 15 minutes" only in the half hour after activation** (plan §0.3.2 says it at that moment; the audit line
  "activated a site", else the site's creation). While the planning runs fail, Recommending says so as the Plan page's `failing` does
  (`plans.tick_failing`: "No plan yet: the last 3 planning runs failed", "No plan yet: a grid connection's tariff has no version in force"); after the
  half hour with no failing run, "No plan yet: the Plan page says why". Each links to the plan.
- **Delivering is the current stage while the plan does not reach the inverter for now,** though the tier allows it: a pause, a release to native,
  the clock demotion and what the edge reports (`plans.edge_view`), in the control chip's own words, linking to the plan (or, for the edge's
  reports, to Set-up › Data and devices for `edit_sites`). The track no longer folds into "Delivering · closed loop" while control is handed back. The
  switchback's native-block day (the pilot's own), a forced charge and a hold stay done.
- **One function for the pilot's week:** `verify.pilot_progress(site, today)` (week of weeks, block of blocks, today's arm, the phase), which the
  track reads and the Pilot band (K28) will read, so the two never say different weeks. The week is the calendar week from the first block's first
  day, as before; the weeks that count toward the 8 a final figure needs (`switchback_weeks`: blocks ended and painted before they began) stay a
  separate figure, with its own words on the Pilot page.
- **One function for the day of collection:** `lifecycle.collecting_day(first, now)` gives the fleet row's `collecting_day` and the track's
  "Collecting data: day 6 of 14".
- **`activate` once A to C pass, which departs from plan C2.3** ("Review opens when A to D are complete"). D has a default for every field,
  stored when the site is made, so the server cannot tell whether D was saved from the form. The rule stays the form's: the web app's `activate`
  link opens Review when its form has D complete, else the first step not complete (C2.5). Recorded in the web repository's API contract for lane
  A. The line stays "Set-up: not activated", the plan's own words for a draft on Fleet (its state table: "Draft: step C of 6" or "Set-up: not
  activated"), though a checker read them as not the plan's.
- **K24's refusals and the day.** A meter reading that gives 0 MMBtu after rounding to three decimals is refused ("That reading gives 0 MMBtu: check
  the figures"); the diesel sentence names its limit ("Litres must be a positive number under 100,000"); a field of the wrong type is named in words
  (`Field(title=...)`, which `schemas.message` now honours for a top-level field: "The meter reading (hm³) must be a number"); the day is a real
  YYYY-MM-DD, never after today in Karachi (plan E2.2.7, with the server as the authority) nor before 2015 (the telemetry's earliest), where text after
  the date was cut off and stored before.
- **The derived MMBtu while typing.** `POST /genset/fuel?dry_run=true` runs the same checks and answers as the save would, with `dry_run: true`, and
  logs and audits nothing, so the dialog's "= 413.39 MMBtu (derived)" is the server's figure (plan C1.4, A8.5; H12 risk 9: preview figures only
  from API routes). A query on the existing route, not a new route.
- **Each fact's cost.** The fleet reads, per site in the track, at most: the tick failures (a site with no plan), its pilot's blocks (a started
  pilot), and for a monitor site the tier's refusal, whose CSV check scans the site's minutes for the inverter's mode until it finds one, as `PUT
  /tier` always did.

### Batch k2: K2, K3 (12 Nov 2026)

- **K3, the contact page's request for access** (plan B21.2 item 1, B4): `POST /api/public/access-requests`, in `api.PUBLIC_POSTS`. It keeps a
  request to the founder and answers 202 with no body whatever the email; it looks up no account and makes none (the owner's rule: preassigned
  accounts only, no self-signup, no invitation). The rules and the store are `access_requests.py`, the routes `routes_access.py`, both Gridlytics' own;
  the request is a strict model (`AccessRequestIn`) refused in one plain sentence per field. Decisions the plan left open:
  - **The topics are B4's four** (`access`, `person`, `support`, `other`, the names of B2.1's `/contact?topic=`), not H5.2's shorthand of three: B4 is
    the page's own section and People's "Ask OnyxLeaf to add someone" opens `?topic=person` (G2.2.1). The company is needed for `access` and `person`,
    the message for `support` and `other` (B4: "for the first two topics", "for the last two"). The work email is checked for shape only, with a dot in
    its domain (unlike an account's email, which takes the demo's `dev@localhost`).
  - **The honeypot is the field `website`**: any value at all (text, a number, a list) is a machine's, answered the same 202 and kept nowhere, and
    it is read before the model, so the rest of such a body is never judged.
  - **8 KB is the body as sent, on this path only** (`api.BODY_CAPS`; a chunked body is counted as it arrives, `/api/v1/...` too); every other path
    keeps `MAX_BODY`.
  - **Five an hour per address** (`platform.client_address`, which believes a proxy's header only where the server says it runs behind one), in the
    API's in-memory limiter. A send kept, and one with the honeypot filled, count; a 422 does not, so a person putting a field right does not use up the
    hour. The 429 carries `Retry-After`, `retry_at` (when a send works again, rounded up to a whole minute, ISO 8601 with +05:00) and the sentence
    "Too many requests from this connection; try again after 14:01" (" tomorrow" past midnight): B4 wants an absolute time, and the server's is the
    one the limiter keeps. `GRIDLYTICS_RATE_ACCESS_REQUESTS` changes the five. Behind a proxy the server is not told of (`GRIDLYTICS_TRUST_PROXY`),
    every visitor counts as the one address "forwarded", as for the sign-in lockout, and five an hour would be the whole site's: the deployment guide's
    proxy step matters here too.
  - **A signed-in sender is recorded** (`sent_by`: email, name, company, Gridlytics role), which the plan does not ask: People's "Ask OnyxLeaf to add
    someone" (Q75) is the company's administrator asking for an account, and the founder must tell that request from anyone's before making one (G4.6).
    A token that is not good is a visitor's, as on every public path; a site key is refused (no site in the path).
- **K2, the founder's Access requests** (plan B21.2 item 2, B20.4, G4.6): `GET /api/founder/access-requests?state=&offset=` (newest first, 300 a
  page from `offset` as the founder's audit pages, and `counts` of every state for the filter, the menu's "n new" and the Overview's NEEDS YOU row) and
  `PATCH /api/founder/access-requests/{id}` (`{state?, note?}`), behind `routes_founder._session` before a field is read, as every founder route.
  - **The states are B20.4's: new, contacted, done, declined.** H5.1's table says "Set up" for the third; B20.4 is the page's own section, and "done"
    fits a request for help with an account or something else, which is never set up.
  - **Each change is in the founder's audit** ("changed an access request", the requester's email as its target, as an account's changes are
    targeted by email; `{request, topic, state: {from, to}, note: {from, to}}`, only what changed); a change that changes nothing writes nothing.
    (The note's text and the email past the keeping, no longer: see the check below.)
  - **Times** are kept in UTC (B21.2) and answered in Pakistan time (`received_at`, `updated_at`, ISO 8601 with +05:00). `about` is the table's
    "About the site" made by the server from the facts ("2 to 5 sites · Deye · LESCO"), so the console holds no second list of their words.
- **The store is schema 4 of the registry** (`platform.db`, `access_requests`, its state held by a CHECK), never a company's file: the requests come
  from no company. `platform._connect` copies the registry to `backup/` (`platform-...-schema-v3.db`) before the migration runs, as for schemas 2 and
  3; `platform.py` is a lifted file, and `backend/lift_manifest.json` records the change (Solarlytics has no contact form, so nothing to port back).
- **Keeping** (B21.2 item 4): the owner's question Q30 (how long access requests are kept) has no recommended answer in the plan. The privacy
  notice's example, 12 months, ships (`KEEP_DAYS` = 365 from when a request came): a request past it is removed at the next send or the founder's next
  read, with a line "removed access requests past their keeping time" (actor `server`) in the founder's audit. One number to change when the owner
  decides; the privacy notice's section 6 should then say the same.
- Owner's questions: Q25 (the form with a founder console inbox, not a `mailto:` only) and Q75 (People's request lands in Access requests as "Another
  person") ship their recommended yes; Q30 as above.

### Batch k2, the check (12 Nov 2026)

Independent checkers read K2 and K3; each finding was tried against the code and every one held. What changed:

- **An optional field left empty** is taken as `""` or null alike, as the contract said: the server refused null for the text fields, `""` for the
  choices (a select left on its empty choice) and either for the equipment. A `mode="before"` validator turns a text field's null into `""`, a
  choice's `""` into null and the equipment's null or `""` into none; a needed field sent null is told what to give ("Give your full name").
- **The founder's audit and the keeping** (the owner's question Q30 still open). Each change's line kept the requester's email as its target and the
  note's text, from and to, for good, past the 12 months the contract, the deployment guide and the privacy notice's example promise. The note's
  text is now never copied (the line says the note was `added`, `changed` or `cleared`), and when a request is removed past its keeping, the lines of
  its changes lose their target, the email; they keep the request's id, its topic and the state from and to. The founder's audit is otherwise never
  changed: blanking a removed requester's email is its one exception, so that "kept 12 months" is true of everything that names the requester. The
  registry's daily copies hold a removed request for their 14 days (DEPLOY.md says so; the privacy notice should too).
- **The housekeeping** (B21.2 item 4: "applied by the registry's housekeeping"): the hourly check of the daily backup removes the requests past their
  keeping before it copies the registry, so a quiet registry does not keep them and its copy does not take them again; a send and the founder's read
  still do too. The check never makes a registry.
- **The rate.** The API's one limiter pruned every key by the caller's window once it held over 20,000 keys: the next minute's check forgot an
  address's hour, and the sign-in limiter's five minutes. `ratelimit.Limiter` now keeps each key's own window (a lifted file; `lift_manifest.json`
  records why). An IPv6 sender is counted by its /64, which one connection holds whole. Every 429 of the send carries `retry_at`, the API-wide
  limits' (per address, per address without a session) too, so the page's "Try again after 14:01" always has its time.
- **Field shapes.** The email refuses control and invisible format characters and the characters a plainly written address never holds (`<>()[]\,;:"`);
  every text field refuses the direction controls (U+202A to U+202E, U+2066 to U+2069), which reorder what the founder's console shows; the one-line
  fields refuse the C1 controls and the line and paragraph separators. The zero-width joiners stay allowed in a name, as Urdu is typed with them. A
  phone may start with a bracket ("(042) 35761234") and holds six digits at least. The founder list's offset is ASCII digits ("²" was a 500, "٣" read
  as 3).
- **One sentence.** `schemas.message` joins a refusal's clauses as one sentence, each after the first starting lower case unless its first word is an
  abbreviation or a name with a mark ("Give your full name; give a work email, such as name@company.com"); `alerts.py`'s two rewordings of its
  message read either case. The topic is checked with the other fields, so a form missing several is told each at once, not one after another.
- **Tests.** `test_founder`'s sweeps (the session's 401, the 503 until set up, the bodies' walk) read every `/api/founder` route the app serves: the
  access requests, in `routes_access.py`, were outside them, and a founder route added outside `routes_founder.py` would have been too. The API walk
  checks that each role's session, the analyst's too, gets the founder's 401.
- **Not done.** A ceiling on the requests kept a day across every address: a sender with many addresses can still fill the registry, 8 KB a request,
  until the keeping removes them. Its number, and what a person is told past it, are the owner's call. A route to delete one request on demand (a
  privacy request) is still not asked by the plan.

### Batches k1 and k2, the senior review (12 Nov 2026)

The review of the whole lane so far ("UX K review" commits) found two faults in K1, both fixed with a test that fails without the fix:

- **The tier rule's CSV check took the telemetry file's write lock.** `routes_sites._has_inverter_mode` read the minutes through `telemetry.connect()`
  (BEGIN IMMEDIATE), harmless while only `PUT /tier` asked it. Since K1's check the track asks it on every site answer and every fleet row of an active
  CSV site at the monitor tier, so the site's page and Fleet waited for a CSV import, an edge batch or the tick (up to the 30-second busy timeout, then
  failed), and each page load held every writer off for the scan (about 40 ms for a year of minutes). It reads through `telemetry.reading()` now, as
  every page read has since performance-live-03. The scan itself stays, as the line on each fact's cost above says.
- **Recommending is not done before the first plan.** At a site allowed advisory or closed loop before any plan was made (`PUT /tier` needs the native
  capture, not a plan), Recommending read "Done: the plan runs at the advisory tier". With no plan made it is now the current stage at every tier, in
  the words the monitor tier has ("The first plan arrives within 15 minutes", "No plan yet: ..."); a tier above monitor makes it done once a plan
  exists. This refines the k1 line "Recommending is done once the tier is above monitor".

### Batch k3: K14, K17, K19 (12 Nov 2026)

The Plan section's asks (plan step 7; K19 also step 8's Overview). No schema change: each new field is in the plan's stored `summary` (JSON) or made
when the answer is.

- **K14, the tariff a plan was priced on** (plan D9.4, D3.1.4): `summary.tariff` = `{disco, category, version_id, name, effective_from, versions}`
  (`plans.plan_tariff`), stored with the plan, a failed solve's too. Decisions the plan left open:
  - **"The tariff version" is the schedule version** (the rate card), as a savings day's `tariff_version` is (verify, `savings_export.tariff_dates`)
    and as the plan's line names it ("FESCO B-3, version in force from 1 Jul 2026"). The calendar, tax and prosumer versions are not named: the line
    is one version, and the savings ledger and its CSV already speak of the schedule's.
  - **The version in force on the plan's first Karachi day** is the line's; `versions` lists every schedule version the horizon's days were priced on,
    in order, with the first and last day each priced (`tariffs.schedules_used`, from the price vectors' own day runs, so the line and the prices
    never disagree): one to three of them, since 48 hours from any time but midnight touch three Karachi days (see the check below). Each day names
    its own version: two versions that repeat a card price alike and share one run of `tariffs.day_versions`, which now keeps each day's schedule
    by its day (`schedule_of`) beside the run's first. A version awaiting its notification prices nothing and is not named; `tariff_notices` says
    so, as before.
  - **Null off-grid** (no connection is priced: the vectors are zeros); absent from a plan stored before (the page leaves the line out, the plan's
    fallback). Words, ids and dates, no rate: a viewer and a machine key read it whole (its test and the role walk prove it).
- **K17, the control answer for a viewer** (plan D9.7, D3.0): confirmed, no code changed. `GET /control` needs no permission, and its one money, the
  gain of each issued table (`issued[].gain_rs`) and of a held re-issue (`reissue.gain_rs`), is stripped by name (the `_rs` suffix) by the API's filter
  for every reader without `see_costs` and every machine key. `test_ux_k17_control_viewer` proves on a real plan, with a table issued for a gain and a
  re-issue held back, that the viewer's and a key's answers are the administrator's with every money key removed and nothing else changed
  (`money.for_key`), with no rupee in words and none of the administrator's figures; the role walk checks the same at every demo site. Owner's
  question Q20 (the viewer reads Plan and the Inverter table, energy only, no actions): the recommended yes ships; a viewer's mark, re-solve and every
  control action are refused (403), as before.
- **K19, the fuel unit** (plan D9.9, D2.9, D3.1.3): `GET /now` and every plan's `summary` carry `fuel_unit` ("L", "MMBtu" for captive gas and RLNG,
  null without a genset, and in a plan null too where the genset's start control is "none": see the check below; `sites.genset_fuel_unit`, which
  `savings_export.fuel_unit` and the fuel log now read too, so the CSVs and the answers follow one rule). The
  figures stay under `litres`, as the K24 log's and every gas figure before them do. Two decisions:
  - **Beside `breakdown`, not in it.** The plan says "the plan breakdown"; `summary.breakdown` holds the solver's figures, every one a number, which the
    web app types `Record<string, number>` and sums by name. `summary.fuel_unit` is the unit of `breakdown.litres` and of each step's `litres`.
  - **A plan's unit is its genset's fuel when it was made**, stored with it; a plan stored before reads it from the fuel it planned on
    (`summary.fuel.basis` "piped" is a gas genset's and only a gas genset's: `plans.PIPED_FUELS` is `sites.GAS_FUELS`), so the plan in force when this
    ships, and the versions under it, read right at once. A plan older than the stored fuel (round 7, engine-optimiser-05) has none: the tile shows
    run hours only, the plan's fallback. Never the genset's fuel now, which may have changed since (the K24 rule: a figure keeps its own unit).
  - Owner's question Q8 (no unit switch anywhere; fuel follows the fuel): the recommended yes ships. The unit is the site's genset's, the same for
    every reader; nothing asks a reader for a unit.

### Batch k3, the check (12 Nov 2026)

Independent checkers read K14, K17 and K19; each finding was tried against the code and every one held ("UX K14 check", "UX K17 check" and "UX K19
check" commits). No answer the page reads changed shape. What changed:

- **A genset the plan cannot start.** A plan's `summary.fuel` is null not only without a genset but where its start control is "none" (the SiteFields
  default): build_problem leaves such a genset out, and every fuel figure of the plan is 0. A plan stored before K19 at such a site read back
  `fuel_unit: null` from that null fuel, while a plan made now said "L", so one site's plan versions would have taken the no-genset tile and "Genset
  fuel 0 L" by turns. A plan's `fuel_unit` is now null wherever the plan planned no genset, as its `fuel` is: the stored rule and the fill-in give one
  answer (the test reads a fresh plan back with its unit taken off), and the page's no-genset tile ("Grid import or Export", D3.1.3) serves a genset
  the plan cannot start, never "Genset fuel 0 L". `/now`'s `fuel_unit` stays the genset's own whoever starts it: its day and month figures are what
  the genset burnt.
- **Up to three tariff versions.** A 48-hour horizon from any time but midnight touches three Karachi days, so with a new schedule from each of the
  next two midnights `versions` holds three. The backend was right; the contract, the type's comment and `plan_tariff`'s docstring said one or two,
  and a page built to them would have dropped the third. The page draws a line "From <first day>: <name>" for each version after the first.
- **One rule for the fuel's unit.** The fuel log (K24) worked its unit out with its own copy of the rule; it now reads `sites.genset_fuel_unit`, and
  `plans.PIPED_FUELS` is `sites.GAS_FUELS` itself, not a second list that must equal it.
- **The role walk's K17 check** compared the viewer's and the key's answers with the administrator's filtered one, which still holds when a gain is
  named so that the filter leaves it. It now also requires the administrator's gains to be named `gain_rs`, the walk to have seen one, and the
  viewer's and the key's issued rows and held re-issue to hold no number at all.

### Batch k4: K12, K13, K18 (13 Nov 2026)

The Overview's and Fleet's asks (plan step 8). No schema change and no new route: `GET /sites/:id/now` and `GET /fleet` gain fields, every one made
when the answer is.

- **K12, the band's facts and the This month tiles** (plan D9.2, D2.3, D2.9, D4.2): `GET /now` adds
  - `plan`: the plan in force (`id`, `generated_at` and `valid_until` in Karachi time, `status_label`, `stage`) and `latest_attempt`, the newest solve
    when it failed or its guard held it after that plan was made (the band's "The 14:30 solve was held back by the guard ... The 14:15 plan stays in
    force.", D2.13), which `plans.current` already read; null when no plan is in force. `plan_status` and `stage` stay for the page of before.
  - `plan_expired_at`: when the last plan ran out, only where that is why none is in force (`plans.held_info`'s `plan_expired`, D2.13's "The last
    plan ran out at 14:15"); a pause, a release or a native block is the control chip's, so it is null then.
  - `applied_today`: `{by, at, method, plan_id, superseded}` from `plans.applied_mark`, the latest mark of today, which the control chip now reads too,
    so "Today's table applied by Bilal at 07:42." and "New table issued: key it in and mark it" come from one rule. `by` is the name the mark
    stored (the applied log already shows it to every reader); `at` is Karachi time.
  - In `month`: `peak_import_kwh`, `pv_self_pct`, `outages`, `outages_covered` (D9.2's three), and the swap tiles' `unserved_kwh` (off-grid) and
    `battery_cycles` (no genset, off-grid), which D2.9's swap rules need for This month and D9.2 did not list. Each is null without its data, never a
    zero (audit pages-A-09's rule for today's tiles); the peak import is also null off-grid and while any day of the month has no peak figure, never a
    part of the month. Outages are counted over the month so far (an outage across midnight once), as today's are, reused for a minute while the
    site's newest reading is the same.
  - **A day stored without its peak import is aggregated again.** A CSV upload aggregates its days without the tariff's peak flags (only the nightly
    job and `/now`'s today pass them), so a CSV site's month had no peak figure. `/now` aggregates such a day again with the flags and writes it back
    once, as it does today's row (round 7, tests-and-perf-10); a day whose tariff calendar cannot be read stays without it.
- **K13, the energy spark** (plan D9.3, D1.7): each day of a fleet row's `spark` carries `peak_kwh_avoided` (the grid import moved out of the peak
  window against the native replay) and `litres_saved` (the fuel avoided, in the genset's unit), beside `saving_rs` and `claimed_rs`. Decisions:
  - **The energy the day claims, by the month's own rule** (`routes_sites._claimed_energy`, read by the bars and by `mtd_peak_kwh_avoided` and
    `mtd_litres_saved`), so a bar and the month's figure agree on which days claim and what (they add up only to within rounding, over the days the
    spark shows: the check below). A day that claims nothing (recommendation only, a claim withheld) reads 0,
    as its `claimed_rs` does: the Energy bars draw the claim, as the Money bars do (role-analyst-14), never the replay's noise. Null where the site has
    no grid (kWh) or no genset (fuel), or where a claiming day lacks the figure (an unmetered genset's litres).
  - **`litres_saved` too**, which the plan does not ask: the off-grid farm has no peak import, so without it its Energy bars would be empty; the
    page draws `peak_kwh_avoided` as the plan says and may draw litres where a row has no grid. Neither is money: a viewer's bars are these and the day.
  - **The Overview's `spark` is the same function's**, now with the site's own grid and genset (it read every site as having both, which only the
    fleet row's month figures had told apart).
- **K18, the grid bill so far** (plan D9.8, D2.10): `month.grid_bill_rs` and `month.grid_bill_rs_basis` (`first_day`, `last_day`, `reading`,
  `billing_month`, `days`, `completeness`, `estimated`, `metering`, `computed`), both money names, stripped for a viewer and a machine key by the
  API's filter (and not worked out for a reader without `see_costs`). The figure is `verify.bill_so_far`: `verify.rebuild_bill`, the Bills page's own
  rebuild, priced by `tariffs.rebuild`, so the band and the Bills page cannot differ in method. Decisions the plan left open:
  - **"The month" is the bill's**: the billing period today falls in (`tariffs.billing_period_for`, the Savings page's billing period and the MDI
    month: from the day after the connection's reading day, else the calendar month), unless a bill entered since was read off that day (the check
    below: `verify.bill_so_far_period`).
  - **To the end of yesterday.** The rebuild fills a minute no reading covers from the period's own profile, so a day still running would be billed
    whole, its hours to come estimated. The last whole Karachi day is billed: on the period's first day there is no figure.
  - **The bill's own billing month.** `tariffs.rebuild` took the billing month (the adders, the ICP rule and baseline, the seasonal month) from the
    last day billed, which is the reading date for an entered bill but not for a bill so far: with the meter read on the 25th, a bill so far on 30 Aug
    would have taken August's adders for September's bill. `tariffs.rebuild` and `verify.rebuild_bill` take `billed_on` (the reading date), the
    period's last day by default, so every bill before is unchanged (tested).
  - **As the DISCO would print it if read at midnight**: `total_rs` (charges, taxes and credits, as the Bills page compares with the printed total),
    with the credit the bill before carried (`_carried_into`, as entering the bill takes it), fixed charges prorated by the days so far (the engine's
    rule past 4 days off 30), and s.235 on the amount so far. The connection's metering factor is applied where it is calibrated
    (`metering_scalars`), as the savings ledger prices both arms; the stored rebuild of an entered bill stays uncalibrated (the fit reads it), so
    the two differ by the factor where one is applied, which `metering` says.
  - **Gaps** are filled from the period's profile, as the Bills page's rebuild fills them; below `BILL_COMPLETENESS_MIN` (90 %) of the days' minutes
    read, `estimated` is true (the Bills page gives no verdict there). With no grid reading at all in the days so far there is no figure: a bill of
    fixed charges alone would be no estimate.
  - **Null** off-grid, with no connection, for a connection the readings do not meter, before a whole day of the period, with no grid reading, and
    where the tariff cannot price the days (logged; the band then holds the saving alone, never a failed Overview).
  - **Kept for 15 minutes** (`BILL_SO_FAR_REUSE_S`) per site and day while the company's audit trail and what the bill reads besides the minutes
    (`verify.bill_so_far_inputs`, the check below) are unchanged: the rebuild reads up to a month of minutes (about 150 ms) and the Overview polls
    every 30 seconds. A bill entered, a tariff, a connection or a metering change is read at once; a late reading for a past day within the 15
    minutes.
  - Owner's question Q49 ("Grid bill so far" on the Overview's money band, if the backend can give it): the recommended yes ships.

### Batch k4, the check (13 Nov 2026)

Independent checkers read K12, K13 and K18; each finding was tried against the code, and all seven held ("UX K12 check", "UX K13 check" and "UX K18
check" commits). What changed:

- **K18: the bill so far follows the DISCO's real reading.** It started the day after the connection's reading day and took the credit only of a
  bill ending exactly the day before. A DISCO reads the meter a day or more off that day, so with the August bill read on the 27th (reading day
  the 25th) the bill so far billed 26 and 27 Aug a second time and dropped the credit that bill carried forward. `verify.bill_so_far_period`: where
  a bill entered for the connection was read since the billing period began (its reading date at most today), the bill so far starts the day
  after that reading, closes at the reading day after the one that bill was read for (the reading day nearest its reading date: read on 22 Sep
  for the 25 Sep reading, it closes on 25 Oct), and takes that bill's credit. Once that closing reading day has passed with no bill entered for it,
  the reading day stands again; with no bill entered, as before. The rest of the product (the month-to-date counters, the MDI month, the Savings
  page's billing period) keeps the connection's reading day: the bill so far is the one figure meant to match the bill the DISCO will print.
  Golden: the August bill read on the 27th with Rs 5,000 carried, 28 and 29 Aug billed in September, Rs 87,972.68, worked line by line.
- **K18: kept under what it reads.** Entering, correcting or deleting a bill writes its audit line before the credits are chained (`_rechain`) and
  the metering factor fitted again (`refit_metering`), so a `/now` read in that gap kept the old credit or the uncalibrated factor under the new
  audit line for 15 minutes. The kept figure's key now holds `verify.bill_so_far_inputs` (its first day and closing reading, the credit carried
  into it, the metering factor and whether it is applied), and the bill is worked from those same inputs, so a key can never hold a figure of
  other inputs. Tested by reading `/now` from inside the refit.
- **K18: the gate tested.** The cache test read a viewer after the administrator, on a warm store whose key is not the role's, so it passed with
  the `see_costs` gate removed. A viewer now reads first on an empty store and the rebuild is not called.
- **K12: `latest_attempt` where no plan is in force.** With the site's first solve held back by the guard (or failed), `/now` said nothing: no plan,
  so no `plan.latest_attempt`, and nothing ran out. The band would have said "The first plan arrives within 15 minutes" with no "Re-solve now"
  (D2.3, D2.13). The top-level `latest_attempt` ({id, status, reason, generated_at}) is the newest solve where no plan is in force and it failed or
  was held; null beside a plan in force (the plan's own says it) and under a pause, a release or a native block (the chip's). D9.2 named only the
  plan in force's facts, but D2.3's primary action needs this state.
- **K12: no zero without a figure.** The month's `unserved_kwh` and `battery_cycles` counted a day row without the key as 0, so a month of rows
  stored without them read "0 kWh unserved" and "0 cycles". Each is now the sum over the rows that carry it, None where none does (as the fleet's
  totals are); a day of 0 is still 0.
- **K13: the bars and the month figure.** They share one rule of which days claim and what, but each bar is rounded to 0.1 on its own and the spark
  is the last 7 scored days, while the month figure is every claiming day summed and rounded once. The contract and the type said the bars add up
  to the month figure; they now say so only to within rounding, over the days shown, and the page draws neither as the other's sum. No figure
  changed (the Savings page's month figure is the same sum). The K13 test uses figures that do not round cleanly and a month of 10 claiming days.
- **The fixtures** a contract test asked for are answers the server could give: `now` and `viewer_now` mark the plan in force applied (by Bilal at
  07:42, as their control chip "Following plan (table applied)" says) and carry `latest_attempt` null, and each fleet row's spark days add up to its
  month figures. Recorded in the web repository's BUILD_NOTES.

### Batches k3 and k4, the senior review (13 Nov 2026)

The review of K12, K13, K14, K17, K18 and K19 with their checks ("UX K review" commits). Both suites passed on the batches as committed. Two figures
were each worked out by two copies of one rule; each is one rule now, with a test that fails without it. No answer changed shape.

- **The newest solve that put no plan in force.** `plans.current` gave a plan's `latest_attempt` (which the alerts' plan_failed also reads) with its
  own query and list of statuses, and `/now`'s top-level `latest_attempt` (K12 check) had another; only the second told two solves stored in the same
  second apart (a re-solve beside the scheduled tick). Both read `plans.newest_unplanned` now (`UNPLANNED`: failed or held; of one second, the one
  stored last).
- **Outages covered.** Today's tile (`today_kpis`) and This month's (`month.outages`, `outages_covered`, K12) each counted the outages and those with
  no critical load unserved. Both read `routes_sites._outage_counts` now.
- **Checked and found right, no change:** the grid bill so far and its basis are money names, not worked out for a reader without `see_costs`, absent
  for a viewer and a machine key (the role walk); the control answer reaches a viewer and a key with its gains stripped and nothing else; every new
  time is Karachi time; no new route, no schema change, and the new kept answers are keyed by the company's file; the contract and the types match
  the answers field by field. `/now` on the seeded demo costs 50 to 100 ms on a cold read against 50 to 60 ms before these batches, and 15 to 20 ms
  warm against 13 to 16 ms.
- **Left for the owner, not changed:**
  - A connection without a reading day is billed by the calendar month (the product's rule: the month-to-date counters, the MDI month and the
    Savings billing period read it so). Where its bills are read mid-month, the bill so far follows a bill entered until the month's end, then starts
    again on the 1st, and the days from that reading to the month's end are on no bill so far until the next bill is entered. Asking the reading day
    in set-up, or taking it from the bills entered, would close this for every figure at once.
  - At a site on a flat tariff (no time-of-use window) the peak-window import reads 0 kWh, today's as before and This month's alike, since its peak
    mask holds no minute; the page may want its swap tile there.

### Batch k5: K32 (13 Nov 2026)

The tariff editor's backend (plan F11 item 6, F2.1, F2.2, F2.5; step 13): the validate route and the drafts first, then the previews, the
withdrawal, the template flag and the agreement's end. One schema change: version 8 of a company's file (`store._V8`, `_V8_COLUMNS`), copied to
`backup/` before it is made, as every migration of the file is (`store._connect`); `test_ux_k32_tariff_editor` holds the copy and the migration.

- **Validate per kind** (`POST /api/tariffs/versions/validate`, HANDOFF's "per-kind body schemas or a validate endpoint"): the library's checks
  are one collector now, `tariffs.body_problems`, which lists every problem with its field (`("body", "cards", "B-3", "peak")`); `validate_body`,
  `propose` (`version_problems`) and approval refuse with its first, in the words they used before, so the raw route, approval and the editor
  cannot disagree. The editor's draft adds what the editor asks and the raw route never did: the source reference is required (plan F2.5.1), a web
  address is http or https, a kind and key are ticked once, and a schedule must keep a card for every category the company's connections are on
  (approving one without is refused, so it is said before it is submitted). Each problem names the editor's step (A to D, decision 10) and its
  version. The answer also sets each version beside the one in force on its start with what it changes, field by field with a percent for a number,
  so Review's WHAT CHANGED and step B's RATES AGAINST THE VERSION IN FORCE need no arithmetic in the browser (HANDOFF's "and a diff").
- **Drafts in a table of their own** (`tariff_drafts`), not a status of `tariff_versions` as F11 item 6's wording has it. A draft is the editor's
  whole work: one version a kind, partial bodies while typed, its start and source, and the editor's state. A half-typed body in `tariff_versions`
  would sit one status filter away from every lookup, and the shipped library's seeding (`tariffs._shipped_todo`) reads every row there by its
  start, so a draft at a shipped start would stop a newer build's shipped version reaching the company. The plan's intent holds: saved by its
  author as they type (`PUT /api/tariffs/drafts/{id}`, an id the editor chooses, as a site draft's), seen by its author and by administrators,
  never counted in `pending`, answered with `status: "draft"`. Its author is their sign-in id, as a site draft's (round 3's rule). Saving is not
  audited (as a site draft's is not: the versions it becomes are); discarding is, since an administrator may discard another person's. Only the
  author writes or submits a draft; an administrator resuming someone else's saves it under an id of their own (409 `draft_taken` otherwise).
- **Submit**: every version waits for approval, all or none (`tariffs.propose_many`, one write transaction, each audited "proposed a tariff
  version"); the draft is then gone. The stored source is the raw-JSON dialog's shape (`kind manual`, `ref`, `url`, `retrieved_on` the day
  submitted, `verified`).
- **Owner's question Q65** ("Publish now" by an administrator, or a second person approves): the recommended answer ships, "What changes" first
  and **a second person approves**. There is no publish route, and approval refuses a version's own author (403 `own_version`): a tariff version
  now keeps its author's sign-in id (`tariff_versions.by_id`). A version with no author's id (the build's, a watcher's, one made before) is
  approved by any administrator, as before. This changes the approve route: an administrator who drafts can no longer approve their own; in a
  company with one administrator, the analyst drafts and the administrator approves, or a second administrator is added. Left for the owner if a
  one-administrator company must publish alone.
- **Preview and simulate price the draft as if approved** (`tariffs.what_if`): inside the block every lookup (`resolve`, the day runs, the adders
  of a billing month, the notices) reads the library as approving the draft would leave it, by `approve`'s own rules, and nothing is written; the
  lookups are unchanged outside it (the overlay is read only when a draft of that kind and key is in it). So the week's prices are the optimiser's
  own vectors (`tariffs.vectors`, as `GET /sites/:id/prices`, with the billing month's imports for the ICP case) and the simulated bill is the
  Bills page's own rebuild (`verify.rebuild_bill`, with the bill's period, printed power factor and the credit the bill before carried): for an
  entered bill, the version in force's figure is the stored rebuild's (tested), and a draft's figure is what approving it gives (tested against the
  prices after a real approval). A draft must pass every check to be priced; a version waiting for approval is priced by any `see_costs` reader
  (the Versions drawer's "Simulate on a site"), a draft only by `edit_tariffs`. `simulate` takes a billing month of readings where no bill is
  entered, only once that month's reading day has passed; `reaches` says whether any of the draft's versions priced a day of the bill.
- **Withdraw** (`POST /api/tariffs/versions/{id}/withdraw`): a waiting version, by its author or an administrator, status "withdrawn" with when,
  by whom and why (`withdrawn_at`, `withdrawn_by`, `withdrawn_note`), audited; never in force, never waiting, never approved. `redraft` makes a
  draft of it for its author: H6.8's "send back" (`approve_tariff`), which F11 did not list, as an administrator's withdrawal.
- **The template flag**: a version the build ships (`by` "shipped") is `template` in every version row and in `/api/tariffs`' `schedule`. The
  mark goes once the company's own version is in force; a bill entered and rebuilt within 2 % does not clear it (F11 asks only the shipped flag).
- **The agreement's end** (R8 2.3): the Tariff alert `regime_expiring_<connection>` was already raised `agreement_days` (60 by default) before an
  agreement ends (round 7, pages-D-16); `/api/tariffs`' agreements add `ends_in_days` over the same window from the day shown, read from the
  site's own rules, so "Ends in 45 days" and the alert agree (tested at 0, 45, 60 and 61 days and with a site's window of 90). No state value was
  added: the page's existing states stay as they are. Owner's question Q69 (agreements under In force): the recommended yes; the answer already
  carries them on the day shown.
- **Money**: every route of the editor needs `see_costs` at least and drafting `edit_tariffs` (a viewer and a machine key are refused each one); the
  rupee figures carry money names all the same (`c_imp`, `c_exp`, `rates`, `bill`, `*_rs`, `*_rs_per_kwh`), and the role walk proves the filter
  would keep none of them. A draft's and a version's bodies hold rates under `body`, as the library's answers always have, behind those
  permissions.

### Batch k5, the check (13 Nov 2026)

Independent checkers read K32; each finding was tried against the code. Fourteen held, in whole or in part, and were fixed with a test that fails
without the fix (`test_ux_k32_check`, and the API and role walks); none was wrong ("UX K32 check" commits). What changed:

- **A draft is submitted once.** The submit read the draft, checked it, made its versions in one transaction and removed the draft in another, so
  two submits of one draft (a double click, a retry) both made versions (the checker's probe: one draft, two waiting versions). The draft is now
  taken in the transaction that makes its versions (`tariffs.propose_many`'s `claim`, `tariff_drafts.claim`): a second submit finds it gone (404,
  as reading it now gives), and a draft saved again after it was read and checked is not submitted as it was (409 `draft_changed`), so the
  versions made are always the draft that was checked, and a newer draft is never removed under its author.
- **A withdrawn draft of the company's is no decision about its start.** The shipped library's seeding (`_shipped_todo`) read every row at a (kind,
  key, start) as the company's own decision, so a version the company drafted and withdrew stopped a newer build's shipped version at that start for
  good (the probe: no FCA on December's bills). The reason the drafts got a table of their own applied to the new status too: the seeding now leaves
  out a withdrawn row the company drafted. A shipped version the company withdrew stays withdrawn, and a newer build's correction of it is not added
  either: the company's withdrawal stands, as its closing of a shipped row always has.
- **Money names on every rupee of the editor.** The validate answer's field changes (a rate before and after, its percent) and a draft's body (its
  rates under any key, such as an adder's `category_rates`) had no money name; the routes' permissions kept them from a viewer and a key, but the
  lane's rule is that the names do. They are now `tariff_changes` (added to `money.MONEY_KEYS`) and `tariff_body` (already one), and the role walk
  proves the filter leaves no figure of the validate answer, the drafts list, a draft or a saved draft (`money.for_key` keeps no number of them).
  This replaces the batch's "a draft's body holds rates under `body`"; a version's own row keeps `body`, as the library's answers always have,
  behind `see_costs`.
- **The agreement's end agrees with the alert in every state.** The Tariff alert says "ends on ..." for an agreement that has not started yet but
  ends within the window; `ends_in_days` was given only for one in force. It now follows the alert's own rule: any state but ended.
- **The simulated bill beside the stored rebuild.** The version in force is priced now, while a bill's stored rebuild is fixed when it is entered
  or corrected, so after an approval or readings filled in since, the editor's "Rebuilt on the version in force" and the Bills page's rebuilt total
  differ. The contract no longer says they are equal; `bill.stored_rs` carries the stored one (null for a month) so the page can say when they
  differ. Tested across a correction approved after the bill was entered.
- **The editor's words.** The problems the validate route, a submit and a preview list are in the editor's units and words where the library's
  name a stored key or a fraction (`tariff_drafts.editor_words`, by the field: "The floor for B-3 is a percent of the sanctioned load, from 0 to
  100", "GST is a percent from 0 to under 100", "Season 2 needs a peak window: a start and a different end, in 24-hour time"; plan F2.5.2 and
  F2.5.3, "Percent entry, fraction stored"). The raw route and approval keep the library's own words. The strict models' refusals are plain
  sentences: "A draft's versions must have at most 7 items", "Version 2's kind must be one of ...", "The start must be at most 10 characters"
  (`schemas.message`: a list's length in words; a list item named by its model's item word).
- **Sending back.** `redraft` needs `approve_tariff` (H6.8: "approve or send back"); an author withdraws their own version but does not send it
  back to themself. The draft is made in the withdrawal's transaction (a failure leaves the version waiting) and the audit line names it
  (`sent_back_as`, `sent_back_to`).
- **A taken draft id** is refused in words that do not say whose it is ("This draft id is taken: save the draft under a new id"). The 409 still says
  the id is in use: the editor chooses random ids, so nothing of another person's draft is learnt.
- **Small things.** The submit's message says "They wait" for more than one version. A version row's `created` is served in Karachi time and a new
  version's `known_from` is Karachi's day (the UTC day named the day before until 05:00). Every route of the editor is in the API walk (simulate,
  submit, withdraw, a draft read and discarded) and the role walk (submit and discard refused to a viewer, a key and an operator; the operator's
  simulation of a waiting version read); an analyst's discard, submit, preview and simulation of an administrator's draft each read 404.

### Batch k6: K5, K6, K7, K9, K10, K11 (13 Nov 2026)

Set-up's asks (plan C19.2; step 9). No schema change: the entry forms are fields of an asset's stored JSON, read with their form where a battery or
genset was saved before, so no migration or copy of the company's file was needed.

- **K7, the entry forms** (plan A8.2 to A8.4, C5.4, C5.5): `BatteryFields` keeps `entry_unit` ("kwh" or "ah"), and in the Ah form `capacity_ah` and
  `nominal_v` (12 to 1,000 V), whose product is `nameplate_kwh`, the one figure the engine reads; `GensetFields` keeps `rating_entry` ("kva" or "kw")
  beside the kVA and power factor it already stored. The rule is the genset's of round 7 (flow-onboarding-16), applied to both: the canonical figure
  is worked out from the entry form where the save leaves it out and refused where it disagrees. Decisions the plan left open:
  - **The other form's figures are not kept.** The kWh form stores no Ah and no voltage, and the kW form no kVA, whatever the save sends: the page
    may hold the other form's figures while the person switches, and a refusal there would stop a save the person sees as plain; keeping them would
    store an Ah and a kWh that no longer agree once the kWh is retyped. The stored kWh and kW never move when the form is switched (A8.2, tested
    three times round).
  - **A save that leaves the form out** reads as the form it gives (a capacity in Ah, a kVA), else kWh or kW, so the web app of before saves as it
    did. A battery or genset stored before reads so too (`sites.with_entry_form`), and the optimistic check and the audit compare the stored asset as
    it is read, so a save sent back with the form it was read with is no conflict and records no change of form.
  - **The form is not money**: an operator saves it (`edit_sites`).
  - **"Review and the PDF repeat them"** (C19.2): Review reads the stored form from the site answer. No PDF prints the battery's or the genset's rating
    today, so there is nothing to repeat yet.
  - Every refusal of a number field now says a whole bound as a whole number ("at least 12", where "at least 12.0" was said) (`schemas`).

- **K11, the steps' words** (plan C19.2 item 7, C9.3): the activation's refusal names the Set-up step ("the inverter (step B)"), as the
  redesigned Set-up calls its screens, where it named the wizard's tab, and its 422 carries `missing` ({step, words} a part, in the steps' order,
  `_activation_missing`'s own list), so Review links each "step B" without reading the sentence; the stage track's "Draft: step C of 6" reads the
  same list. The set-up warning on a connection priced on the other DISCOs' windows says "(step C)" where it said "(tab C, or Site settings
  Tariff)": an active site's Site settings become Set-up's summary of the same steps (plan C10). The Pilot page's own tabs ("fix it in tab C" of
  the switchback rules) are that page's and are not Set-up's: left as they are.

- **K6, Set-up's check** (plan C19.2 item 2, A8.2, C5.4 to C5.6, C7.4, C9.2): `POST /api/sites/{id}/validate` (`setup_check.py`) takes a step's
  section as the form holds it (`site`, merged over the stored fields; `assets`, each whole as its save sends it, null for one unticked;
  `connection`, as `PUT /connection` sends it, its `id` naming a saved one), checks it by the models a save is checked by, over the site as stored,
  and writes nothing (no audit line). Decisions:
  - **Who reads it:** every role that opens Set-up (see_costs: administrator, operator, analyst; the analyst reads the steps and Review's derived
    figures); a viewer, who has no Set-up (plan C4), and a machine key are refused. A new permission would have changed the role matrix (K4) for one
    route.
  - **Problems on their fields.** Every error of every section, each with its step (A to D: the site's fields by `SITE_STEPS`, the equipment B, the
    connection C), its dotted field and its path, in the steps' order. A model's own check that concerns one field raises `schemas.FieldProblem`
    and is placed on it (the battery's window on `soc_min`, a kVA that disagrees on `rated_kva`, GST claimed without an STRN on
    `tax.gst_recoverable`, the agreement's order on `agreement_end`, Tariff F on `season_months`); its refusal on a save reads as before. A DISCO
    group the site's DISCO contradicts is a problem on `disco_group`, as the save refuses it. The site, connection and equipment models' fields now
    carry titles, so a refusal names a field as the form does ("The charge power (kW) must be more than 0", where "The p ch kw must be more than 0.0"
    was said), the nested ones too (`schemas._title_at`: "The reserve floor (% state of charge) must be at most 100"). A share the form types as a
    percent is told in percent (`PERCENT_WORDS`: "The state of health is a percent above 30, up to 100"), the tariff editor's rule (UX K32 check);
    the save's refusal keeps the stored fraction's bounds.
  - **The figures** (`derived`), each the engine's own rule: the battery's nominal kWh (its nameplate: Ah x V / 1,000 in the Ah form) and usable kWh
    at its window (nominal x state of health x (highest minus lowest state of charge), plan A8.4), its entry form and percents; the genset's usable
    kW (its rating: kVA x the power factor) and its fuel an hour at half and full load by its curve (b x rating + a x output, litres or MMBtu,
    `estimated: true`); the reserve: the floor held with the grid up (`plans.delivered_floor_pct`, so a floor typed under the battery's cut-off plus
    2 points says the floor that holds), the energy the battery gives the load from it down to its cut-off, after its discharge losses (x eta_d,
    as the optimiser's energy balance has it), and the minutes of the critical load's highest hour that carries. The plan's own example ("about 5.1
    kWh", C7.4) leaves the losses out; the server's 4.86 kWh is what reaches the load. The wear cost per kWh discharged (`optimise.wear_cost`, the
    price the plan charges) is served for see_money_settings only, under `wear_cost_rs_per_kwh`, added to `money.MONEY_SETTING_KEYS` so the
    filter strips it for an operator and a key all the same (the role walk proves it).
  - **A figure that cannot be worked out is null, never 0,** and `not_derived` says why in its section's first problem (plan C5.8: "Usable: could
    not be worked out: <server's sentence>").
  - **Money settings an operator is not shown** are taken as stored where the form sent their defaults, as the asset's save takes them
    (`routes_sites._with_held_money`, now shared by both), so an operator's check reads the wear the plan prices.
  - `setup_warnings` are `sites.setup_warnings` of the site as it would be (a section with problems counts as stored).

- **K5, the price preview** (plan C19.2 item 1, C6.2, C6.7, C6.8): `POST /api/sites/{id}/prices/preview?day=` prices the connection's fields as
  step C holds them, before they are saved and before the site has any connection. Decisions:
  - **The body is the connection's save** (`PUT /connection`'s body: the fields, with `id`, `if_unchanged` and `idempotency_key` taken and the
    last two ignored), checked as the save checks it, a group the site's DISCO contradicts refused alike, so the picture never shows prices a save
    would refuse. `id` names the saved connection being edited: its own bill's peak window (a site override) and its billing month's readings then
    apply, as `GET /prices?connection=` has them. The day is GET's (`day`, today by default, 2015 to a year ahead).
  - **One function prices both routes** (`routes_sites._prices`, out of `GET /prices`): the preview is `GET /prices`' answer for the fields as typed
    (tested equal after the save), plus `preview: true`, `as_of` (the server's stamp in Karachi time, the picture's "As of 14:12"), `time_of_use`
    (step C's derived line: "Time of use: the other DISCOs' windows (follows FESCO)", "Time of use: K-Electric's windows") and `peak_windows`.
    `GET /prices` answers as before.
  - **The peak windows** are the calendar the connection is priced on, on the day: a site override's own window where its bill shows one, else the
    DISCO group's time-of-use version in force (`source`, `version_id`, `effective_from`), each season with its months and its window in words
    ("Dec to Feb: peak 17:00 to 21:00", a run of months across the year's end too), and `by_month`, January first, the share of each hour in the
    window (the Tariffs page's `peakShareByHour`: a half hour is 0.5), null for a month in no season. Windows only: no money, so the page may keep
    the strip where it hides the prices.
  - **No rate card in force** for the category on the day (plan C6.8's warn state) is 422 `no_tariff_version`, "No B-2(b) rate card is in force on 1
    Jun 2025, so the plan cannot price this connection: check Tariffs"; any other day the library cannot price keeps GET's refusal.
  - see_costs, as `GET /prices` (the prices are money: `c_imp`, `c_exp`, the MDI's `rate_rs_per_kw`, the ICP's `rate`); a viewer and a machine key
    are refused. Nothing is saved or audited (exempt in the audit walk).

- **K9, the source test's first rows** (plan C19.2 item 5, C4.5, R8 2.2; optional, built): `POST /sites/:id/source/test` adds `sample`, the
  newest five minutes received (oldest first), each `{at, values}` with every point the test lists, in the units the product stores them in (the
  point's name says it: W, %, V), a yes or no for grid presence and the genset's running, the inverter's mode as sent, null where the minute has
  none. The newest, not the window's first: the card is read while the source is being connected, so the rows that show it sends now say more than
  a day-old minute; the plan's "first rows" is the card's label. **Times in Pakistan time**: C19.2 asks for the rows "with UTC times", which the
  owner's rule (every time meant for people in Pakistan time) overrides; `first_at` and `last_at` are Karachi ISO (+05:00) too, where they were UTC
  (the same moments: the web app formats them with `pktDateTime`). Still `edit_sites`, as the test was; no money.

- **K10, the company's tax defaults** (plan C19.2 item 6, C6.4; optional, built): `GET /api/meta` answers `company_tax_defaults`, the four tax flags
  (`strn`, `gst_recoverable`, `s235_adjustable`, `atl`) of the grid connection the company saved last, with its site and `words` ("As at Mill B"),
  so step C's four boxes start from them in place of the server's default. **Owner's question Q35** (the tax status per connection, prefilled from
  the company's other sites, or moved to Company settings): the recommended answer ships, per connection, prefilled; the server keeps storing it
  per connection (`sites.TaxFlags`). Decisions: a demo site's connection is not the company's, nor a removed site's, nor an off-grid one (it bears
  no bill's taxes); the province is left out, since it follows the new site's DISCO (`sites._from_disco`); "last saved" is the connection's
  `updated` stamp (to the second), then the order rows were made; for the roles that open Set-up (see_costs), null for a viewer (who has no
  Set-up) and while there is no such connection. On `/api/meta` rather than a new site's answer: step C of a site's second connection needs it too.
  No money (flags only).

### Batch k6, the check (13 Nov 2026)

Independent checkers read K5, K6, K7, K9, K10 and K11; each finding was tried against the code, and all seven held (all low). Each was fixed with a
test that fails without the fix (`test_ux_k06_check`, and the K5, K6, K9 and K10 tests), checked on the trees before the fix and before each route
("UX K05/K06/K09/K10 check" commits). What changed:

- **The reserve's floor is the plan's** (K6). Set-up's reserve took the delivered floor (`plans.delivered_floor_pct`) while the plan caps it at the
  battery's highest state of charge (`soc_floor_grid`), so a floor typed at 100 % with the top at 95 % counted 43.78 kWh where the plan holds 41.34.
  One helper now gives the plan's floor (`plans.grid_floor`) to both, and a floor typed above the highest state of charge is a site warning ("The
  reserve floor (100 %) is above the battery's highest state of charge (95 %): the plan holds the reserve at 95 %"), on every site answer. Golden:
  (0.95 - 0.10) x 51.2 x 0.95 = 41.34 kWh, 41.3 minutes of 60 kW.
- **The reserve reads the site as it would be, as the warnings do** (K6). A typed inverter with a problem gave the reserve no inverter (the
  battery's own 100 kW) while the warnings in the same answer read the stored 30 kW; and any problem in the site's fields (an empty name) held the
  reserve back with that unrelated sentence. Now a field with a problem counts as stored (`setup_check._site_section`: the site's fields that have
  none over the stored ones), the warnings and the reserve read that one site, and only a problem in the reserve's own fields (the critical load,
  the reserve policy) or the battery holds it back. This replaces the batch's "a section with problems counts as stored" for the site's fields.
- **A negative hour of the critical load is refused** (K6, a gap from before the batch): each hour is 0 to 100,000 kW
  (`sites.SiteFields.critical_load_kw`, plan C7.3: "0 or more; 24 values"), on save ("The critical load (kW) must be at least 0") and in the check
  (on "site.critical_load_kw.5", step D).
- **The check agrees with the save for the reader's role** (K6). An operator's check of a changed reserve floor, value of lost load or battery
  replacement cost was ok, with the reserve worked out from the floor the operator cannot set, and the save then refused it with 403. Nothing
  leaked (no money reached the operator; nothing was written), but Set-up said a step was fine that the role cannot save. The check now adds the
  save's own refusal as a problem on each field it concerns, by the saves' own rules (`routes_sites._validate_refusals`: `_limits_changed` field
  by field without `manage_sites`; `_need_money_settings`' comparison without `edit_money_settings`, the equipment's after `_with_held_money`; an
  unticked asset whose money settings an administrator set, as `asset_delete`), in their words (`auth.need`'s, given without raising it; `auth.py`
  is lifted from Solarlytics and is left as reviewed, so the test holds each sentence equal to the save's own 403): "Changing the site's control
  limits is for an administrator". A limit or money setting sent as stored is no change. A role problem on the reserve's own fields holds the
  reserve back, as any problem there does. Taking the stored value instead was not chosen: the save refuses, it does not take the stored value,
  and the check's answer is the save's.
- **The peak windows are the engine's rule** (K5). The strip repeated the calendar's choice and the in-peak test. `tariffs.calendar_version` (the
  calendar a connection is priced on, with its source and version; `calendar_for` now reads it) and `tariffs.peak_share_by_hour` (each hour's share
  of its four 15-minute steps that `is_peak_local` marks peak, as the price vector marks them) make it; the same figures for every window on the
  quarter hours (every shipped one), and a window that did not start on one is drawn as the prices treat it. Tested: with the in-peak rule moved,
  the strip and the day's `peak` move together; a site override's own window is the strip's.
- **"Saved last" is the order of the saves** (K10). The connection's `updated` stamp is to the second and the rows' order is the order they were
  made, so a re-save in the same second as another site's newer row lost, and moving the plant's mark onto one connection stamped the other,
  which was not saved. The order is now the audit trail's "saved a connection" lines (the newest whose connection is still the company's grid
  connection at a live site that is no demo; one with no line, by its stamp as before); `saved_at` is that save's moment. This replaces the batch's
  "last saved is the connection's updated stamp (to the second), then the order rows were made". Tested with two saves in one second and with
  the plant's mark moved; another company's connection saved later is never this company's.
- **The isolation tests prove the route** (K5, K6, K9). Each first reads the company's own site (200), then another company's site with a
  connection (and a battery for the check) for every role that reads the route: 404 "No such site", the route's own refusal, not an unserved path.
  The K9 role test reads the rows themselves (`sample`) for the administrator and the operator, so it fails without K9.

### Batches k5 and k6, the senior review (13 Nov 2026)

The review of K32 (k5) and K5, K6, K7, K9, K10 and K11 (k6) with their checks ("UX K review" commits). Both suites passed on the batches as
committed (backend 1753 passed; web gates clean). Two defects were found and fixed, each with a test that fails without its fix
(`test_ux_k_review_k5_k6`); no answer changed shape.

- **A figure too large to work out was a server error** (K32). The library takes a rate card's rates with no upper bound (a finite number, 0 or
  more), so a draft at 1e308 Rs/kWh passes every check. The validate answer then held a change of inf % (what a version changes, against a rate of
  2), the preview a week's mean price of inf, and the simulated bill an energy line of inf (with a total of 0: the bill engine's arithmetic does
  not carry inf to the total); none of these can be written as JSON, and each was a 500. The change in percent is one rule now
  (`tariff_drafts.change_pct`: what a version changes, the preview's `rates` and the simulated `bill`, where there were two copies), null where it
  cannot be worked out; a preview or a simulation whose prices, means or bill lines pass what a float holds is refused (422) in one sentence: "This
  version's prices cannot be worked out: a rate in it is too large. Check the rates typed".
- **Discarding a draft is one step with its audit line** (K32). The discard removed the draft and then wrote its line in a second step: a line that
  failed left the draft gone and unrecorded (an administrator may discard another person's draft), and a second discard of the same draft (a double
  click, or one after its author submitted it) answered 200 and audited a discard of nothing. The draft goes and its line is written in one
  transaction now, and a draft gone since it was read is 404 "No such tariff draft", with no line.
- **The contract's claim for the check's role refusals** (K6 check) said the check is "never ok where the save is then refused". It holds for the
  roles that save Set-up (`edit_sites`: an administrator and an operator), which the check now follows field by field. An analyst saves no step
  (every Set-up save is `edit_sites`) and reads Set-up only (plan C20: "the analyst (all read only)"), so the analyst's page sends `{}` or the
  stored sections; a changed section from an analyst is checked as typed, without the save's 403. The contract now says so.
- **Checked and found right, no change:** every new answer's rupees are under money names (`c_imp`, `c_exp`, `rates`, `bill`, `tariff_changes`,
  `tariff_body`, `wear_cost_rs_per_kwh`), and the role walk shows a viewer and a key refused each editor and Set-up route with no rupee in the
  refusal, and the filter keeping no figure; drafting needs `edit_tariffs`, approving and sending back `approve_tariff`, a waiting version's prices
  `see_costs`; another company's site, draft or bill is 404 (each company has its own file, and the routes read the site through `full_or_404`);
  schema 8 is the next free number, made after the copy `store._connect` takes, with its migration test; every new time is Karachi time; the what-if
  overlay (`tariffs.what_if`) writes nothing and no lookup keeps what it read (no cache in `tariffs`, `verify.month_totals` or `plans.peak_mask`);
  the contract and the types match the answers. Probes of odd bodies (cards that are not objects, seasons that are not lists, fields of the wrong
  type for every kind, an unknown connection, a malformed Set-up section of each kind) found no other server error.
- **Left for the owner, not changed:**
  - The library takes a rate card's rates and fixed charges, an ICP rate and the PTV fee with no upper bound (an adder's rate is capped at Rs 100/kWh).
    A version approved at an absurd rate would price every plan and bill at it; the editor now refuses to preview or simulate one that overflows,
    but validate, submit and approval still take it. A bound per field (for example Rs 1,000/kWh for an energy rate) is a rule of the library and
    is the owner's to set.
  - With Q65's answer a company with one administrator cannot approve that administrator's own versions (recorded at batch k5).

### Batch k7: K8, K15, K16, K33, K34, K35 (13 Nov 2026)

- **K8, whether the box's key was made** (plan C19.2 item 4, C12.2): `GET /sites/:id/edge` answers `key: {exists, made_at}`, whether an edge
  device's key, not revoked, exists for the site and when the newest was made (Karachi time), read by every reader of the edge's state (the
  operator too, who may not list keys). A partner tool's key is not the box's: only `kind` edge counts, as only an edge device's key sends the box's
  heartbeats. Nothing of the key itself is answered (no prefix, id or name); the checklist's "by AK" is left out, since who made a key is the key
  list's (`manage_users`), not something an operator needs to tick item 1.
- **K15, the CSV upload's preview** (plan D9.5, C11.4 stage 2): `POST /sites/:id/telemetry/mapping/preview` (`upload_data`, as the upload and the
  guess) takes the file's head (the first 64 KB the page already reads for the guess), the mapping as set (`PUT /telemetry/mapping`'s shape; left
  out, the columns are read by their names) and `tz_offset_h` (as the upload; the page sends 5 for Karachi time), and answers the first five rows
  as the upload will store them: `at` in Karachi time, `values` each key in its stored unit and the product's sign, the grid's and the genset's
  state as true or false, an impossible or unreadable reading null. Nothing is written. The upload's header reading, row order (the last row of a
  repeated minute), grid signal, step and value checks are one function each now (`telemetry._layout`, `_in_order`, `_grid_read`, `_step_of`,
  `_grid_from_voltage`), read by `import_csv` and `preview_csv` alike, so the preview refuses what the upload refuses, in the same words, and a file
  previewed then uploaded is stored as the preview said (tested minute by minute). Decisions the plan left open:
  - **Stored order, not file order.** "As Gridlytics will store them": the rows in time order, a repeated minute's last row. For a logger's file
    (in time order) this is the file's order.
  - **An energy column is the row's own figure.** A 15-minute row's 3 kWh is stored as 200 Wh in each of its minutes; the preview shows the row's
    3,000 Wh with `step_min` 15, the figure a person checks against the file.
  - **A head cut in the middle of a line** reads its last line only when it has as many cells as the header (the guess's own rule).
  - **Values to six decimals**, so 30.1 kW reads 30100, not a float's 30100.000000000004.
  - Owner's question Q47 (the CSV upload moves to Set-up › Data and devices with a server preview of the first rows): the recommended yes ships.
- **K16, the alert thresholds' defaults and ranges** (plan D9.6, D6.3): every rule of every rules answer (`GET` and `PUT /api/alerts/rules`, a
  site's `rules` in `GET` and `PUT /api/alerts/rules/sites/:id`) carries `limits`, each threshold's `{default, min, max}` straight from
  `alerts.THRESHOLDS`, so the editor writes "Default 15 · 5 to 1,440" and a save holds the same range. The Data category's staleness has no default
  figure ("by the data source"): `by_source` gives each kind of source's minutes before a warning (an edge 30, the maker's cloud 20, a CSV upload
  1,440). The limits are the server's: a rule sent back with them (the editor's body, or any client that sends the rule as read) is taken and the
  limits never saved. Inside each rule rather than beside the categories, because the answer is `{category: rule}` and the pages read its keys as
  categories. No money: a viewer reads the five energy kinds' limits.
- **K33, the email alert kinds per reader** (plan G9 item 2, G3.4): `GET /api/platform/notifications` answers `categories` as the inbox gives them
  (`alerts.reader_categories()`: a viewer Data, Reserve, Battery, Genset and Forecast; an operator eight, no Product; an administrator or an
  analyst nine), and a save refuses a kind the person does not receive ("You do not receive Savings alerts"). Owner's question Q77 (email choices
  kept, folded under "Not sending yet", the email checkbox dropped, only the kinds received offered): the recommended yes ships.
  - **A settled rule wins over the plan here.** G3.4 was written on the inventory's "the server stores it and no code reads it", and makes the
    card's ticks the `follow` list, with a save of a kind turning `email` on. Since round 7's sweeps (the rest of role-analyst-32) the inbox and
    its badge list only the kinds in `follow` (`alerts.followed_categories`; the contract's "The inbox follows a person's notification choices"),
    so ticks that set `follow` would hide every unticked kind from the person's inbox, which G3.4 itself rejects ("a person could unfollow Reserve
    and miss a critical alert ... the inbox and the counts must stay complete"). The email card's ticks are therefore their own list,
    `email_kinds`: a save of it with at least one kind sets `email` true (the plan's rule), with none false, and leaves `follow` as it was. Today's
    page keeps `follow` and `email` ("Email them to me as well": the kinds followed, by email too); a choice made before K33 reads as G3.4's
    compatibility rule says (the kinds by email are the kinds followed while `email` is true, none while it is false). A kind kept from a role
    held before is not answered and not read.
  - **Left for the owner:** the redesigned My account has no control for the inbox's own `follow` list (G3.4 has none, since it believed nothing
    read it). Whether the inbox keeps following it (today's page and the inbox's "Show every kind" still set and lift it), or lists every kind the
    role receives, is the owner's call.
- **K34, filters on both audit trails** (plan G9 item 3, G2.4, G4.5): one module, `audit_trail.py`, holds each trail's groups (the "Kind of change"
  select's: People and keys, Sites and set-up, Plans and control, Tariffs and money, Savings and pilot, Reports and exports, Demo; the founder's
  Sign-ins, Refused sign-ins, Companies, Accounts and memberships, Backups, Access requests) as one map from an action's words to its group,
  registered as an SQLite function so a page and its count are one query each; a test reads every `store.audit(...)` and `founder_audit(...)` call
  of the backend and its scripts and fails on an action of no group. Filters: who, the kind (`group`), the site (the line's target, or the site its
  detail names) or the company, From and To (Karachi days, both included), and a search of who, what, of and the detail (a site's name finds its
  lines). Owner's question Q79 (the audit filters, which need this): built, as the owner wants the whole plan.
  - **The company trail's filtered page is a new path,** `GET /api/audit/search` (`{entries, total, offset, limit, groups, people}`), where the plan
    names `GET /api/audit` answering `{entries, total}`: today's Audit trail tab reads `GET /api/audit` as a list, and this lane merges before step
    14 rebuilds the tab, so changing that answer's shape would break the page in between (the lane's rule: each ask's fallback keeps the page of
    before working). `GET /api/audit` is unchanged; step 14 moves to the new path and the old one can go after it.
  - **`people`** is who is in the trail (the Who select: "the people in the trail, plus Gridlytics (demo set-up)"), one `who` each: the person's id,
    `demo` for the demo set-up whoever loaded it, else the product's own word (`system`, `edge`, `shipped`).
  - **CSV:** `GET /api/audit/export.csv` with the same filters (the administrator's: `manage_users` to read the trail and `export` to download it),
    every matching line (at most 100,000), times in Karachi time with their offset, the detail as JSON, a cell a spreadsheet would read as a formula
    quote-prefixed; audited "exported the audit trail" with the filters given (Reports and exports).
  - **The founder's trail keeps each line's company** (platform.db schema 5, `founder_audit.company_id`, the copy to `backup/` first as every
    registry migration): each console line of a company is written with it; the lines before are filled from the company's name in them (a
    company's own lines and a backup's by their target, a membership's or an account's by the detail's `company`), by today's names and the
    names before a rename. A name two companies share, or none has, leaves a line of no company: it is listed unfiltered only. `GET
    /api/founder/audit` keeps `{entries}` and gains `total`, `offset`, `limit` and `groups`; each line its `group`, `company_id` and
    `company_name`; its `at` is Karachi time now, where it was UTC (the same moment).
  - Every time the new answers give is Karachi time; the company trail's `at` in `GET /api/audit` stays as today's page reads it.
- **K35, each company's last backup** (plan G9 item 4): `GET /api/founder/companies` (and every company answer of the console) gives each company's
  `last_backup`, when its newest copy was made (the `modified` of the copy the Backups page lists first), in Karachi time, null with none, the
  backup folder listed once for all companies (`backups.newest_copies`). The batch names it "founder or administrator only": the company's
  administrator (`manage_users`) reads the same for their own company in `GET /api/platform/org`; no other role and no key is given the field.

### Batch k7, the check (13 Nov 2026)

Independent checkers read K8, K15, K16, K33, K34 and K35; each finding was tried against the code, and all eight held (two of them the same
finding from two checkers). Each was fixed with a test that fails without the fix (the K15, K33 and K34 tests), checked on the tree before the fix
("UX K15/K33/K34 check" commits). K8, K16 and K35 had none. What changed:

- **The preview refuses a header that names a column twice** (K15, medium). The upload's route refused such a file before reading a row, and the
  preview did not ask, so it answered five clean rows (the repeated name's last column) of a file the upload then refused. The check is
  `telemetry.refuse_named_twice` now, read by the upload's route and by `preview_csv` alike, in the upload's order (the mapping, the header's names,
  then the file), with its words: "The header names load_power_w more than once: give each column once and upload again".
- **A row's `at` is the minute it is stored on** (K15). The upload stores a row on its minute (`_write_days`: the time to the minute), and the
  preview answered the file's second, so a logger that writes seconds read 00:00:30 where 00:00:00 is stored.
- **A role without `upload_data` is refused whatever JSON the body holds** (K15). The route took the body as an object, so the framework refused a
  viewer's list or text with its own words before the role was read; it takes any JSON now and checks the role first, and an administrator's or
  an operator's body that is no object is refused in a sentence ("Send the file's head as a set of named values: head, mapping and
  tz_offset_h"). A body that is not JSON at all is still the framework's 422, before any route runs; the contract says "any JSON body".
- **A save of `follow` keeps the kinds the role does not give** (K33, medium). Since K33 a viewer is offered five kinds, and today's My account
  page sends `follow` with every save, the email checkbox's too; the save kept only the five, so once the person's role was raised the inbox and
  its badge hid every kind the new role adds (a critical Control alert of an operator). The kinds not offered keep what was kept for them now:
  followed until chosen (as before K33, when every kind was offered), unfollowed where an earlier role's choice unfollowed them. The answer is
  still the kinds received. Storing the kinds left out instead of those kept was not chosen: the inbox's reader (`alerts.followed_categories`)
  and every choice saved before read `follow` as the kinds kept.
- **`email` is on while a kind received is chosen for email** (K33). A person whose email kinds were all of a role held before (an administrator
  who ticked Savings alone, made a viewer) read `email: true` with `email_kinds: []`; `email` is worked out from the kinds answered now, on the
  card's rule. Nothing was sent either way (email does not send yet).
- **A page's offset is ASCII digits, at most nine** (K34, found by two checkers). `audit_trail.offset_of` took whatever `int()` reads: an offset
  past what SQLite holds was a server error on both trails, and "٣", "1_0", "+3" and " 5 " were read as numbers. The founder's lists' rule
  (`routes_access._OFFSET`, the K2 check) holds for both trails now: "The offset is a whole number from 0". Today's `GET /api/audit` (an `int`
  query, unchanged by K34) still gives a server error for an offset past what SQLite holds; it predates the batch and goes when step 14 moves to
  `/api/audit/search`.
- **A company's name given up and taken by a later company is no company's** (K34, schema 5's filling). The lines before schema 5 were filled by
  today's names and the names before a rename, so when a company was renamed and a later company took its old name, the first company's lines of
  that name were given to the later one. Only a name one company alone has held (its name now, and its names before each rename, read newest
  first so a chain of renames is followed) is filled now; a name two companies have held, at once or in turn, leaves its lines of no company
  (listed unfiltered only). This replaces the batch's "A name two companies share, or none has". Telling the two apart by the order of the
  lines (the later company's lines are after its "made a company") was not chosen: a line written out of that order would be given to the wrong
  company, and a line of no company is only missing from a filter, never shown under the wrong one.

### Batch k8: K20, K21, K23, K26, K27 (13 Nov 2026)

- **K23, stage progress per kind** (plan E6 item 4, E1.3.4): each issue saves `stage_progress`, `{have, need, unit: "days"}`, counted by the stage
  rule itself as it decides the stage: solar, the whole days of the site's own output in the 14 local days before the issue's day; load, the whole
  days of unshed readings in the last 21; outages, the whole days of grid readings in the last 28 at the site and on its feeder. The thresholds are
  one table the rules read (`forecast.STAGE1_DAYS`: 3, 3 and 7), so "2 of 3 days" and the stage cannot disagree. Every forecast page answers the
  stored issue's (the Outages answer's `forecast` too); null for an issue stored before K23 (the next 15-minute issue carries it), a kind that does
  not apply and no issue. `have` is not capped at `need`, and an outage count is floored (6.9 days of readings is "6 of 7", stage 0, as the rule).
- **K21, each issue's energy per Karachi day** (plan E6 item 2, E1.1.6): every PV and load page carries `issue.daily_kwh`: per Karachi day the 48
  hours reach, the day read at the issue's median and at its 10 % and 90 % levels (kWh to 0.1), the steps' span in Karachi time and whether the day
  is whole. Worked out when the page is read, from the stored quantiles, so a past issue (K20) and every issue stored before carry it too.
  - **Against the plan's wording, by the engine as it is:** E6 asks for "a range from the issue's own scenarios", but an issue holds seven
    quantiles per step and no scenarios (no part of the engine draws any). The server reads each day at one level, every step of the day at that
    level, which is how the issues are built: a solar issue is the clear-sky curve times one clearness for the day, drawn from the month's or the
    site's days (`forecast.pv`: every quantile is cs x the ratio x the day's clearness at that level, capped at the inverter), a load issue at stage 0
    one factor times the template, at stage 1 each step's median plus its spread times the same z. So the day's energy at a level is its steps at
    that level; summing independent steps' quantiles would shrink the range to almost nothing, which the plan's warning is about. Tested: a stage-0
    November solar day reads 0.89, 0.466 and 1.0 of its clear-sky energy, the clearness levels themselves. Where conformal bands set a step's spread
    (stage 1 with enough stored errors), the errors are not known to be independent through a day, so one level per day stays the wide reading.
- **K20, a past issue by its time** (plan E6 item 1, D3.3, D3.4, E3): `GET /sites/:id/forecasts/:kind?at=` answers the issue held at that time,
  with `at`, `exact` and `latest_belief_time`. Decisions:
  - **The issue held then, not only an exact match.** Issues are pruned (whole for 3 days, hourly for 35, then each day's day-ahead issue), so an
    older plan's quarter-hour issue is gone; the page opens the issue held at that time, `exact` false, and the band says which it is. An exact
    match is the plan's own issue.
  - **Both forms the pages hold:** a plan's `belief_time` (ISO, UTC as plans answer it) and the forecast page's (epoch seconds); a time with no
    offset is Karachi time. Digits are matched as ASCII, at most ten, and the ISO form by pattern, so "٣" or "²" are refused (K34's check found
    int() reading them); a time more than a minute ahead is refused; each refusal is one sentence (422 `invalid`).
  - **A past issue as it stood:** its actuals over its whole horizon so far, its Preliminary count from the verified days before its own Karachi
    day (`forecast.verified_days(before=)`), an outage issue's announced windows from the schedule in force at its time (`forecast.schedule(at=)`).
  - Nothing kept from then: 404 `no_issue`, "No solar forecast issued at or before 14:15 on 12 Nov 2026 is kept"; another company's site is "No
    such site", as on every site route. Every role reads it; a machine key does not (the page's own scope rule).
- **K26, alerts by kind** (plan E6 item 8, E1.3.8): `GET /api/alerts?kind=` keeps one rule's alerts, within the categories the reader receives
  (a viewer's `?kind=savings_negative` lists nothing). The kind is a rule's name in form (lower-case ASCII letters, digits and underscores, at most
  64, starting with a letter), since rules are named per kind of event and connection (`edge_<event>`, `forecast_stage_<kind>`,
  `regime_expiring_<id>`); one in no such form is refused in one sentence, an unknown one lists nothing. Noted for the page, unchanged here: an
  alert is raised once while its condition holds, so a day that busts while the day before's alert is still open moves that alert's `detail` to
  the later day; a run of missed days reads as its last (the daily scores keep every day).
- **K27, the savings summary's additions** (plan F11 item 1, F1.1.3 to F1.1.9): one function per figure, which `verify.summary` and the monthly
  statement both read (`verify.peak_import_avoided`, `fuel_saved`, `taxes_recovered`, `preliminary_by`, `period_tariff_notices`, with
  `savings_export.plan_at_start`); the statement's Summary gains "Peak-window import avoided", its ledger "Taxes you recover (not part of the
  saving)", its fuel row reads `fuel_saved`, and a tariff notice the month reaches is printed. Decisions:
  - **Peak-window import avoided** counts every scored day whose import is known (the days of "On all scored days", as `litres_saved` does), not
    only the claiming days: it is the replay's energy evidence, and a withheld day of unmetered import is no reading. null off-grid.
  - **Taxes recovered, from the arms:** the ledger's grid levers are bearable rupees, so each claiming day's peak-window and off-peak levers and
    MDI share times the share of a bearable rupee its connection recovers that day (`tariffs.recoverable_share`: GST where `gst_recoverable`, the
    s.235 advance where `s235_adjustable`, on the bill amount with GST, further and extra tax, over the bearable stack). On the claim, the band's
    figure. Export credits carry none (net billing credits them after tax; a legacy agreement's netted units are left out too, so the figure is
    a little low at a legacy net-metering site), nor do the genset, wear and carry-over levers (no DISCO bill taxes them). Not from rebuilt bills:
    a period is not a billing period, and rebuilding both arms' bills on every read would take minutes.
  - **The capture version per day** was answered since round 7 (`native_capture_version`, and the page's `native_captures` with each version's
    first and last day): nothing new. **The plan per day** is the plan in force when the Karachi day began (`plan_ids`' rule: a held or failed
    run is no plan in force).
  - **The tuned table's rows** are served in the product's grammar (start, mode, floor, target, grid charge). The vendor's layout of F1.1.6
    (program, power W) is not: a brand's programs are compiled from a plan's slots (`delivery.compile_mode_a`), which a static rule has not got.
  - **Tariff notices:** `tariffs.awaiting_notices` at the period's last day, as rebuilt bills carry them; words and dates, every reader.
  - **Preliminary by two rules** (owner's question Q60; plan F0.1 decision 15): Q60 gives no recommended answer (the owner's call), while F0.1
    decision 15 recommends that each figure says which rule it follows. Both are answered (`preliminary_rules`: the screen's, 28 verified days and
    the native capture; the report's, also 8 weeks of switchback and every FCA billed), and `preliminary` keeps the one rule of before (the
    report's), so no page or document changes until the owner chooses.
  - Money: `taxes_recovered_rs` is a `_rs` name, stripped for a viewer and a key (the role walk proves it); every other addition is energy,
    settings or words, read alike by every reader.

## 2026-11-08 Round 6: chained savings replays (HANDOFF 8.A.1)

Before, each day's native and tuned replays started from the actual arm's state at 00:00, and the day's carry-over valued the whole end-of-day gap in
stored energy at v_end. At the off-grid farm the per-day ledger summed over 11 days missed a continuous replay by -6.2k to +7.9k Rs, and the
carry-over lever swung by tens of thousands of rupees against the genset lever.

- **Chained replays.** Each baseline's replay of a day starts from that baseline's own end state of the day before, as the twin carries it: state
  of charge, genset on, minutes run, minutes into an outage, a rescue run (the twin keeps no tank). The state is stored with the day's row
  (`chain.start`, `chain.end`).
- **Carry-over is the change over the day in the valued gap:** v_end x (actual - baseline end energy), less the day before's v_end x (actual -
  baseline start energy). Summed over a chain, this leaves one end-of-period energy term, as a continuous replay has. A genset run across midnight
  is one start on each arm (`_genset_cost(on_before)`). The perfect-foresight bound subtracts the same start term.
- **Where the chain restarts** from the actual arm's measured state (`verify._chain_starts`):
  - the first scored day, after a day with no row (`gap`), and after a row stored before chaining;
  - the first day of a switchback block that changes arm (plan 8.2, below);
  - after a day that claims nothing **and** whose replay ran over unread minutes (`unread_inputs`): load or PV blank, or grid presence blank on a
    grid connection (`chain.end.unread_min`).
- **Why that last restart (verifier finding R6-withheld-day-replay-leak).** Chained through, a withheld day's replay ran on filled inputs (an
  unread load counts as zero). It handed the next day, whose data was complete, a native state that was too full. On farm s7 a 19:00-23:00
  dropout raised the next two days' claims by Rs 1,140. With the restart, the next day's claim rests on nothing the dropout hid: it is the claim
  that day makes when the withheld day is not scored at all.
  - A day that claims nothing on fully read inputs keeps the chain: a monitor site, a table not applied, or a day withheld for its grid meter,
    battery or genset alone. Native's replay of such a day is valid, so the days around it claim exactly their complete-data claims (0 change on
    3 seeds with the battery and genset blank for an evening).
  - **Known limit:** after an unread dropout, native's true state at the next midnight is unknown, including its genset run, which dominates at
    the farm. With the restart, the next day's claim moved by -802, +1,405 and +1,590 against its complete-data claim on three fresh farm seeds,
    and by -685 on the verifier's s7. Chained through, the moves were +2,879, +1,378, +1,640 and +1,140. Other start states were probed (the
    battery's floor or full, the actual's charge less the last valid gap, native's last genset run). None did better on every seed: s32 ranged
    from +0.35k to +3.9k. Filling an unread load with the load forecast's equivalent day for the replay would be the real fix; it is a later item.
- **Recompute.** `compute_day` reads the stored row of the day before. With `forward` (the default), it re-scores the later stored days whose
  start no longer matches, up to 62 days or the first day with no row. `compute_range` (POST /savings/recompute) scores its days in order, then
  walks forward once. `summary` lists `chain_stale_days`: days out of step because a day before them was re-scored on its own, or lies beyond the
  62-day walk. The web app shows "Recompute needed". Each day row in the summary carries `baseline_start` (`actual` or `previous_day`).
- **Chained at every site, grid-connected too.** On fresh paired rebuilds, HEAD's per-day sum missed the continuous replay by +18.2k at the
  Karachi feed mill (s31; the verifier's s5: +20.8k). The chained ledger missed it by -1 Rs.
- **A switchback site is held to the segmented replay (plan 8.2).** Each block's baseline is native from the block's measured start. The plan
  block's last day credits the energy it hands to the native block at v_end, and the report shows that credit beside the saving.
  - That hand-over credit can exceed what native makes of the energy in a replay straight through both blocks: Faisalabad s5 3,263 credited
    against 1,771 realised; s31 4,276 against about 2,320. So the ledger sits above the pure continuous replay (s5 +1,492, s31 +1,955) and on
    the segmented one (+0.02, -1).
  - HEAD was -85 (s5) and +6,737 (s31) against the pure replay, and -1,577 and +4,782 against the segmented one. Its -85 at s5 came from daily
    errors that happened to offset; HEAD already credited the hand-over at v_end.
  - Valuing the hand-over at what the native block realises would leave the plan block's last day provisional until the native block ends.
    That is a later item if pilots show it matters.
  - The native block's days chain within the block from its measured start. The inverter ran native there, so they score about 0 and claim
    nothing. Chaining through the plan-to-native boundary would show the unrealised part of the hand-over as a loss on days that claim nothing,
    and change no claim, so it was not done.
- **Lever label:** "Carry-over (change in stored energy)", in reports.py and in the web app (it was "stored energy at day end").
- **Evidence.** Fresh paired 14-day rebuilds on 8 Nov, on an idle machine, with the default 3-hourly re-plans. Each site was built once with the
  change (the ledger does not feed the build) and scored by HEAD's ledger and by the change's on two copies of the same telemetry. Native
  baseline, Rs, over the 11 scored days 28 Oct to 7 Nov:

  | Site, seed | HEAD ledger | Change | Continuous (end carry) | HEAD minus cont. | Change minus cont. |
  |---|---|---|---|---|---|
  | Farm s31 | 16,030 | 24,716 | 24,716 (7,138) | -8,686 | 0 |
  | Farm s32 | 21,952 | 23,290 | 23,290 (4,767) | -1,338 | 0 |
  | Farm s33 | 23,620 | 18,691 | 18,691 (5,016) | +4,929 | 0 |
  | Karachi feed mill s31 | 16,648 | -1,570 | -1,568 (9,853) | +18,216 | -1 |
  | Faisalabad mill s31 (switchback) | 37,200 | 32,417 | 30,462 pure; 32,418 segmented | +6,737; +4,782 | +1,955; -1 |

  - Farm levers, change against HEAD: genset 14,109 / 15,204 / 10,317 against -3,976 / -18,366 / -37,018, and carry-over 7,138 / 4,767 / 5,016
    against 16,361 / 36,061 / 53,282.
  - The MDI lever is the same on both sides: Karachi 37,537.5, Faisalabad 23,412.5.
  - `backend/tests/test_fix_r6_savings.py`: 9 tests. They cover a three-day farm and a grid site whose daily sums equal the continuous replay,
    recomputing one day, the first day of a period, the switchback block, a dropout on a withheld day, a day withheld on read inputs, and a
    non-claiming day found to have a dropout later.

## 2026-11-07 Round 5: the genset's minimum-load surplus, and a re-planned demo history

- **Surplus charging with the grid down.** In an outage, and at every off-grid step, the set's output follows the load as it does on site: it is
  capped at max(f_min × S, served load − PV), and its minimum-load surplus charges the battery (`battery.a_gen_surplus`,
  `optimise.genset_surplus_steps`, `genset_follow_cap`). So a night below the set's minimum runs the set instead of shedding. At the demo farm,
  planned shed fell from 230-340 kWh to about 0 and rescue starts by about half, in every paired rebuild. Grid-up steps keep no genset charging, and
  full-power charging (`a_gen`) stays off while `delivery.GEN_CHARGE_SUPPORTED` is False. This also closes the "PV charges while the set carries
  the load" loophole that round 4's dropped row aimed at, without its solve cost. The threshold replay counts the same surplus.
- **Solve time.** Two rows the balance implies when the set is fully on or off (`surplus_charge`, `off_discharge`) tighten the relaxation without
  removing any plan; the tests check this on six cases. On the second day, grid-down runs keep clock hour marks (about Rs 37 per two-day plan). Farm
  solves are back near their old times (3-hour-mark states: 2.1 s mean, 4.7 s worst). Neither applies at a soc_threshold site that starts the set in
  the horizon, because there they slowed the solve.
- **Known limit:** the 120 L soc_threshold farm's first solve now stops at its 10 s limit, within 0.5 % of the best plan. This is recorded in the r2
  test.
- **Off-grid terminal value:** the plan uses the ledger's rule (`plans.offgrid_c_next`: the genset's marginal cost at `verify.night_load_ratio`),
  so the plan and the ledger value stored energy the same way.
- **The demo history re-plans every 3 hours** (`demo.REPLAN_HOURS`, `GRIDLYTICS_DEMO_REPLAN_HOURS`), as live ticks re-plan every 15 minutes. A plan
  made at midnight and run all day carried every forecast miss to the evening: on one seed the farm burnt 91 L on a day native burnt 47 L. In the
  3-hourly paired rebuild, the change gained Rs 7.4k in the continuous replay.
- **Open, closed by round 6 (chained replays): the per-day carry-over at off-grid sites is noisy.** The per-day ledger minus the continuous 11-day replay ranged from -6.2k to +7.9k
  across seeds. The carry-over lever swings by tens of thousands of rupees and largely offsets the genset lever. Possible fixes:
  - price carry-over at what the native arm actually saves the next day: replay that day natively from both end states, holding the day as
    provisional until then;
  - or score off-grid days in multi-day blocks.

  Customers see the per-day ledger, so this matters before a pilot at an off-grid site.

## 2026-11-07 Round 4: the handoff's open work, checked on public data

### The PV model against public data

`backend/tests/fixtures/public` holds one year (2020) of hourly PVGIS 5.2 and NASA POWER data for the four demo cities, with each source and
licence in its `SOURCE.md`. `public_data.py` fetches the data by hand; tests never call the network.

- **Clear sky:** Haurwitz times a monthly haze factor. Each month's factor is the highest ratio of measured GHI to Haurwitz on the clearest fifth of
  that month's days, across the four sites and both providers. It replaces the single 0.88, which put clear winter days 15-20 % above "clear sky".
- **Glass reflection loss** (Martin and Ruiz, 0.16) is modelled: 2.4-2.6 % against PVGIS's 2.6 %.
- **Fit to PVGIS:** driven by PVGIS's GHI, the model is within 2.2 % of PVGIS's annual output at all four sites.
- **`MONTH_K`** is each month's mean clearness against the new clear sky. It is fitted to PVGIS's 2005-2020 monthly output, with the GHI averaged
  with NASA POWER's climatology, because NASA sees Punjab's winter fog and ERA5 does not. Stage 0's annual error went from -6..-12 % to within
  ±5.2 %.
- **PV quantiles** are capped at `min(cs × max(1, pr), kw_ac)`: a plant fitted above the nominal clear sky keeps its clear days, but is never
  forecast above its inverter's AC rating.
- **Later item, not done:** blending yesterday's clearness 50/50 with the 14-day median beats the median alone by 4-9 points on this data. It was not
  done because the twin's daily clearness is drawn independently each day, so the blend would make the demo's forecasts worse.

### Savings

- **Masked minutes with a read load** price each arm on its own battery and genset flows against that load. This replaces round 3's known limit
  (grid and genset columns blank together could over-claim). Minutes with no load reading keep the charge-only rule.
- **A blank genset** is read from the balance where the grid flow is known. Where the balance cannot be closed and the set could have run (an outage,
  a parallel set with the grid up, or the set running at the edge of the stretch), the day is withheld: `claim_withheld: genset_unknown`.
- **A state-of-charge spread** across an import-price edge is priced at its least value: discharge on the cheapest minutes, charge on the dearest.
- **Off-grid end-of-day energy** is valued at the genset's cost at its after-dark load ratio (`verify.night_load_ratio`), not at 75 % load.
- **The demo history plans 48 hours** (192 steps), as live ticks do. In paired continuous replays the 48-hour plans beat 24-hour ones every time.

### Optimiser and delivery

- **Off-grid soc_threshold sites:** every first plan is checked for dodged starts, whether optimal or time-limited. A plan with fuel left over gets the
  shed-rule re-solve. Otherwise it is repaired: the site's controller is replayed over it, respecting the daily run-hour cap, and the problem is
  solved again with the genset fixed. The label says "Repaired to the site's controller". This retires round 3's known limit and its
  "a plan that burns the whole tank is kept" rule.
- **Charge slots** charge at their mean planned rate only up to their last planned charge step, recorded as `charge_end_ts`.
- **Time limits:** every plan records `time_limited`, `mip_gap` and `time_limit_s`. A relaxed retry after a timed-out first solve is flagged too.
  `plans.time_limited_recent(site_id, hours)` counts them. The limits themselves are unchanged.

## 2026-11-07 The demo's negative savings: plan and delivery out of step

The end-to-end walk on a freshly seeded demo showed the fleet losing money: Rs -13,409 for the month, from the Karachi feed mill and the off-grid
farm. Two read-only investigators traced it to three product defects. In each, the plan asked for something the compiled slot table cannot make a device
do. The ledger was pricing fairly: the per-day ledger landed within about Rs 1,000 of an 11-day continuous replay.

- **Charging from the genset:** the optimiser planned short full-power genset runs that charged the battery. No brand grammar has a
  charge-from-genset slot, and on site the genset input only follows the load. At the farm the plan expected 1,675 kWh of genset charging and the twin
  delivered 66 kWh. The battery sat near its floor after each run, and 30 of 42 starts were rescues (native made 22). The plan now never counts on it:
  `a_gen` is `gen_charge_allowed` and `delivery.GEN_CHARGE_SUPPORTED`, which is False. The site flag stays stored for the day a brand supports it.
  A second row was tried and dropped: "off-grid, charge only from PV beyond the load". It closes a small loophole (PV charging while the genset
  carries the load), worth about Rs 2,400 over 11 days at the farm. But it made the 120 L farm solve take 23 s against 6 s, and a tick has 10 s.
- **The 2-slot compile:** to fit Sungrow's 2 slots, `compile_mode_a` dropped the run that moved the least energy. A hold run moves none, so it always
  folded into its neighbour with more energy: the evening discharge. That discharge then started at 15:00-17:00 instead of the plan's 20:45, emptied the
  battery off-peak, and had nothing left for the 18:30-22:30 peak. A run that does not discharge now folds into a neighbour that does not discharge,
  where there is one. (Part 7.1 asks for boundaries ranked by a twin-priced value; this rule fixes the case at hand, and the full ranking stays a
  later item.)
- **The charge slot's demand cap:** a merged charge slot charges at its mean planned rate across its whole span, including the morning block that sets
  the month's maximum demand. Its `grid_cap_w` was the larger of the largest planned import and the MDI cap, so it never stopped the charge, and the
  monthly MDI rose by 16-19 kW. A charging slot is now capped at the plan's MDI cap alone.

Replays from the investigators: the farm goes from about -17,900 to +7,400 to +8,300 Rs over 11 days, and the Karachi period from -42,200 to about
+54,300 (daily levers plus MDI). Days without an outage stay honestly negative, about -1,100 to -1,250 each. That is the premium for holding the
outage reserve, which the outage days repay.

Karachi rebuilt from scratch for 14 days, one configuration at a time:

| Configuration | Total | MDI lever |
|---|---|---|
| Old compiler | +21.1k | +5.6k |
| Compiler fixes with genset charging planned | +35.8k | -24.1k |
| Both fixes | +39.3k | +3.4k |

**Wall-clock limits:** plan quality depends on CPU load, because the solve limits (5 s in the demo build, 10 s per tick) are wall-clock time. A demo
seeded while the test suite ran in parallel made visibly worse plans. Seed the demo on an otherwise idle machine.

## 2026-11-07 Review fixes, round 3, and the web app's backend asks

### Optimiser

- **Hold against force_charge around an outage:** a Hold (reserve) binds only on grid-up window steps before the window's first outage step. A
  force_charge binds on every grid-up step of its window, including after the grid returns, so one set during an outage charges once the grid is back.
  Off-grid, both bind on every window step. The shed sum for both starts at the window's first step, so load shed in an outage inside the window counts
  against the rows that follow. Without that, rows after the outage, priced at VoLL per step, made the plan shed the outage's critical load to keep the
  battery full.
- **Off-grid soc_threshold sites:** the first solve has no shed rule, for solve time. A time-limited plan that sheds with the set off, the tank not
  empty and fuel left at the horizon's end is solved once more with a shed rule, and the summary says so (`solver.shed_rule_resolve`).
- **Known limit, accepted for v1 (superseded by round 4: every such plan is now repaired to the controller):** a 10 s plan whose time-limited incumbent burns the whole tank (300 to 310 L of the 400 L farm) can still shed with
  the set off while fuel remains. Solved to optimality, those plans shed nothing, but that takes 75 to 90 s. Off-grid soc_threshold control is not the
  default and no demo site uses it. A tick there can take up to three solver limits (about 30 s): a first solve, the relaxed retry and the shed-rule
  re-solve. Repairing this by replaying the threshold controller over the plan is a later item.
- **Legacy net metering at ICP sites:** the ICP uncertain-case rows count net import (G - X), as the bill nets exports per period.

### Telemetry, sites and forecasts

- Forecast stage rules count minutes of data, not 1-minute rows: samples times the source's median interval (at most 15 minutes). This applies to load,
  PV and outage.
- `SiteFields` carries `load_peak_kw`, `off_grid` and `data_source`. A demo site's stored `twin` is kept unchanged on update and cannot be changed
  through the API. The server, not the client, records who confirmed the grid signal and when.
- Imports carry `from_ts`, `to_ts` and `file_name`. The columns are added by `telemetry._imports_columns` outside the schema version, so only columns are
  added and no rows change.
- A new telemetry file is set up under a process lock, and the WAL switch is retried on locked or busy.
- `sites.put_state` reads and writes in one write transaction, as `routes_common.state_change` does, so a tick, an outage event and a tier sign-off never
  write over each other's fields.

### API

- **OPT-15 re-solve:** the pending re-solve is read, merged and cleared in one transaction. The wait fallback runs only for a stored telemetry batch
  that has a reading from a minute after the event's minute. `since` restarts when the deciding event changes.
- **GET /plan** answers `{plan: null, held: {reason, until, plan_id}}` as soon as a pause or release is in force, before the next tick.
- **Drafts** are owned by the member's id. A draft id another member holds answers 409 `draft_taken`.
- **Connections:** a save is idempotent on the client's key, or else on regime, tariff category and meter reference. The last connection of an active,
  grid-connected site cannot be removed (409).
- **Alerts:** Control and Savings alerts (plan 9.3), `plan_failed` included, wait for an active plan and 7 days of telemetry.
- **Reports:** `preliminary` is stored when a report is made (workspace schema version 2, with a backup before the migration). The energy-only report
  has its own permission, held by all four roles.
- The tier sign-off is kept in `site_state.tier_signoff`, with the audit line as the fallback for earlier changes.
- The savings answer names its litres' source (`energy.litres_source`: meter, curve or null) for ranges up to about two months.

### Savings: blank columns

- **Unresolved minutes** are only those whose balance cannot be closed: load unread, genset unknown, or no state of charge on either side. A battery
  flow taken from two states of charge more than 30 minutes apart is rebuilt on the balance for every arm, but those minutes count toward the 5 %
  unresolved cap, so a long gap is withheld rather than smeared.
- **Blank battery power:** the actual arm's flow comes from the balance (metered grid with known load and genset), else from the change in state of
  charge left over. That change may span any number of masked minutes but at most 30 others. Where the flow stays unknown, no arm's masked charge is
  priced and the day claims nothing (`claim_withheld: battery_flow_unknown`). Withholding only the wear lever would credit the baseline's whole wear on
  a day the actual arm cycled more.
- **Known limit (superseded by round 4: masked minutes now price each arm's own flows against the read load):** with the grid and genset columns blank for up to 5 % of a day (the genset unknown), the masked minutes price no arm's load. On a site
  whose baseline discharges in that gap while the actual holds, the day can still over-claim (a reproduced case: +3,839 against a metered +560). It
  needs both columns blank at once, and it predates round 3. A tighter cap or symmetric discharge pricing on masked minutes is a later item.

### Demo

- The twin writes only minutes before the clock's current minute. The next advance deletes twin minutes left at or after the clock.

## 2026-11-07 Review fixes, rounds 1 and 2

An adversarial review of v1 confirmed 69 findings, 13 of them high (`.scratch/review/*.json`). Rounds 1 (`507a4db`) and 2 (`32eafe5`) fixed them in
file-disjoint groups, each change checked by a separate verifier. The rules those rounds settle:

### Optimiser and plan

- **Manual floor (Hold, force_charge):** at a grid-connected site a Hold binds only on grid-up window steps before the window's first outage step. The
  reserve is used in that outage and is not refilled inside the window. At an off-grid site it binds on every window step. Shed load never counts
  towards keeping a floor.
- **grid_charge_allowed = False:** the battery charges only from the forecast PV beyond the load on grid-up steps (plus the genset where
  `gen_charge_allowed`). Plan steps carry `grid_charge_permitted`, so delivery never compiles grid charging there. The compiler has no charge-from-genset
  mode: genset charging with the grid up compiles as `charge_pv_only`, which is the safe direction.
- **soc_threshold genset control:** the controller's starts are treated like AMF panel starts. They are exempt from the run-hour cap and from min up/down,
  and happen in an outage only at or under the threshold. The fuel cap stays physical, and threshold rows lapse where the set cannot run. In grid
  outages a per-step shed rule stops shedding being used to dodge a start. Off-grid sites have no such rows, to keep solve time down (they tripled the
  farm solve time). Fuel burnt before step t is a running-sum variable.
- **Pause and release to native:** while either is in force, no plan is in force, even when the solve fails. A tick stores its plan as `held` (reason
  `control_paused` or `released_to_native`), the previous plan is superseded, and the edge gets a signed `release` message with no slots. `active_at`
  answers None for any instant inside a pause.
- **Observed grid state:** a tick reads it up to its actual time. A same-quarter re-solve never replaces an issue that observed an outage with a blind
  one, unless telemetry shows the grid back.
- **Billing month:** connections carry an optional `reading_day` (1-31; null means the calendar month). The month-to-date counters, M0, rest of month,
  `in_month` and the MDI month end follow the bill's period. Legacy net metering passes its per-period exports. Its export is priced 0.01 Rs/kWh below
  the import it nets, so equal prices cannot give circulating flows (the ledger adds the 0.01 back).
- **Signed messages** carry no money in the signed, stored or served body. A `plan_messages` row is written only by the edge's key-only pull.

### Tariffs and bills

- Adders that list their billing months ignore `effective_to`. A correction with the same start supersedes the old version. Tariff and fuel starts must
  be canonical `YYYY-MM-DD`.
- Bills can be entered with the printed reading dates (`previous_reading_date`) and are stored as the first day billed (`rebuild(...,
  period_start_exclusive=True)`).
- ICP sites: the ledger and the prices page price from the billing month's scaled period totals, not a 1e9 placeholder.

### Telemetry and forecasts

- **Untrusted input (plan Part 2.8):** times must lie between 2015-01-01 and a day after now. `ingest` skips and counts rows outside that range
  (`out_of_range`). The edge route refuses such a batch with 422 so the stream never stalls. A numeric cell above `CELL_MAX` (1e9) or a non-finite value
  is stored as missing and counted. A CSV upload refuses repeated header names, any out-of-range time and a span over 400 days, stores nothing on a
  refusal, and aggregates only the days that received rows. CSV exports quote text cells that start with `=`, `+`, `-` or `@`.
- **Missing columns:** where a minute has neither energy column, import and export come from `grid_power_w`. `g["soc"]` falls back to the BMS reading,
  while `g["soc_est"]` keeps the estimate alone. Battery trust compares the estimate with the BMS only where both were measured.
- **Load censoring (plan 5.2):** outages are read over known samples. A gap of up to 15 minutes, or a grid-up blip of at most 2 samples, does not end
  an outage. The reference is the median of the last 15 or fewer known grid-up samples within 60 minutes. The 40 % drop is tested on the first known
  samples after the loss, and the whole outage is censored.
- A stored non-finite day row is healed on read. Where its minutes are gone, it becomes null, never zero.
- `forecast_scores` has one `baseline_value` column, so the climatology Brier is returned but not stored.

### Savings

- **Advisory confirmation needs evidence:** without battery power, or with no scorable test and a table that cannot be told apart from native, the day
  stays `pending` and claims nothing. `not_applied` (closer to native, with a 2 % of battery margin) is decided before `confirmed`. This is a
  simplified form of Part 7.6.
- **Import completeness:** blank grid minutes in the actual arm are rebuilt from the balance. Where that is impossible, the minute is priced on neither
  arm. A day whose metered import share is under 0.5, or with more than 5 % unresolved, is withheld from the claim, the verified days, the MDI months and
  the switchback regression.
- **Unmetered genset fuel** is unknown (`genset_unknown`, litres null), never zero.
- **Native replay:** `backup` mode fills the battery from PV to soc_max, and `backup_soc_pct` is only the grid-charge level. `feed_in_first` is modelled
  too. CSV and BMS-only sites start their replays from the measured state of charge, not 50 %.
- "Preliminary" waits for the switchback weeks and the FCA, so every demo site shows it today.

### API and security

- `/api/edge/*` and the signed plan message take a site API key only. A person's session gets 403 `key_required`. A key holds no role permission and
  never gets money fields, whatever role it was made with.
- **Edge events are stored once:** the sequence claim and the events are written in one company-file transaction.
- **OPT-15:** an outage event re-solves an active site once telemetry shows the event's minute, or after 120 s. It runs at most once per 30 s, and a
  pending re-solve is dropped after 15 minutes. Resume re-solves at once.
- **Money settings:** changing voll, the battery's replacement cost or warranty, or the genset's start, run-hour and O&M costs needs
  `edit_money_settings`. So does deleting a battery or genset whose money settings an administrator changed.
- **Genset requests:** one run holds one pending request. A start that moves at least a step earlier withdraws the request ("Moved earlier to HH:MM")
  and asks again. While no plan is in force, every open request is withdrawn.
- The CSV upload parses on a worker thread, so the event loop stays free.

## 2026-11-06 v1 build: what was built and where it differs from the plan

### The lift (Part 10.4)

- Sign-in, tenancy and the founder console are lifted from `saad-tahir/solarlytics-backend` at commit `c853130` (the owner's own code): `identity`,
  `platform`, `companies`, `routes_local`, `founder`, `routes_founder`, `store_founder`, `store_members`, `seed_demo`, `backups`, `mfa`, `auth`, `apikeys`,
  `version`, `jobs`, with `solarlytics` renamed to `gridlytics` and the roles changed to admin, operator, analyst and viewer. `ratelimit.py` is the
  `Limiter` class from Solarlytics' `routes_accept.py`; `schemas.py` keeps its refusal shape. The founder password accepts Boxlytics' plain sha256 with a
  warning, as the lifted code did.
- **Deviation:** the plan put the lifted modules in a `tenancy/` subpackage. They stay flat in `gridlytics/` so their `from . import store` imports work
  unchanged; a later move is mechanical.
- `apikeys` is site-bound (`site_id`, `kind` edge or partner); the guard refuses a key on another site's path with 403 `wrong_site`
  (`/api/sites/{id}/...` and `/api/edge/{id}/...`).
- **Lifted tests:** 141 of Solarlytics' sign-in, platform, company and founder tests pass unchanged after the renames; 7 that assert Solarlytics' own data
  (projects, role presets, its program-routes table) are skipped by name in `tests/conftest.py`, and `test_signin_guard.py` (it imports Solarlytics'
  sharing module) was not carried. Re-aiming them is a follow-up.

### Engine

- **Forecasters (Part 5):** stages 0 and 1 only. The PV chain is a small numpy model (NOAA sun position, Haurwitz clear sky scaled for haze, isotropic
  transposition, PVWatts-style DC/AC) instead of pvlib, so the API has no heavy dependency; no NWP feed yet (Open-Meteo needs the owner's paid plan, O5).
  Stage 1 fits a performance ratio and a clearness distribution from the last 14 days, with smart persistence for the first two hours. Load stage 1 is
  equivalent-day persistence with the last-two-hours ratio. The outage model is Beta(8, 2) schedule adherence plus an hourly unscheduled hazard shrunk to
  a DISCO prior with seven days' weight, and an empirical median duration; the logistic and boosted models are later items.
- **Forecast storage:** one JSON body per issue in the telemetry file's `forecast_issues` table, not a row per target time; scoring reads the issue.
- **Telemetry:** the `minute` table as specified; the 15-minute `quarter` table is not materialised (reads aggregate on the fly by resolution; a year of a
  site still answers in milliseconds); `telemetry_days` is written by the import, the twin and the nightly job.
- **Optimiser (Part 4):** the formulation as written, on scipy's `milp` only (no highspy warm start, no structural cache: a 192-step genset case solves
  in about 0.05 s on the twin sites). At sites whose genset starts on an AMF panel the reserve covers at most two steps (the start delay and a failed
  start), priced at the genset's marginal cost. The terminal value applies to the energy above the floor (`E_use <= E[T] - E_min`).
- **Mode A compiler (Part 7.1):** boundaries are dropped by the energy they move, not by a twin-priced value; every slot carries `grid_cap_w` (the plan's
  MDI cap) so the device peak-shaves to it, and a grid-charging slot charges at its mean planned rate. Brand grammars are a dict in `delivery.py`, not
  JSON files yet. The wrong-plan guard replays the table through the twin and refuses a plan whose state of charge crosses the floor or that imports in
  a forecast outage.
- **Battery service:** reads the estimate column (or the BMS) from telemetry and grows sigma from the last anchor; coulomb counting belongs to the edge.
- **Savings (Part 8):** the actual arm against the native capture and a tuned table (a grid search over eight static TOU tables on the observation week,
  cached per month), priced per minute on the bearable basis with the dated fuel price and the same wear charge; carry-over credited at the terminal value;
  the perfect-foresight bound is a 15-minute hindsight solve of the day. MDI is a monthly lever from the arms' monthly peaks. Days in the native blocks
  of a switchback calendar are labelled `native_block`; a day claims its saving only when its fidelity is `confirmed`.
- **Reports:** ReportLab PDF and CSV for the monthly savings report, the energy-only (viewer-safe) report and the daily operations sheet.
- **Demo (Part 9.6):** four sites as the plan describes; the history defaults to 21 days (`GRIDLYTICS_DEMO_DAYS`; the plan says 90) so a seed takes a
  couple of minutes. The history runs the native settings for three observation days, then each day's plan; the monitor-tier mall keeps its native
  settings (a recommendation changes nothing on site), so it shows no claimed saving, honestly.
- **CSV upload:** the API takes the file as a raw `text/csv` body (python-multipart is not a dependency); a multipart body is parsed minimally.
- **Scheduler:** APScheduler in the API process behind `GRIDLYTICS_SCHEDULER=1`; tick at :00/:15/:30/:45 plus 20 s, nightly at 00:40 PKT.

### Money on the wire

- The viewer filter strips every key in `money.MONEY_KEYS` and every key ending `_rs`, `_rs_per_kwh`, `_rs_per_kw` or `_pkr`; the API walk test proves a
  viewer gets none of the plan's prices, costs, savings, levers or fuel prices.
