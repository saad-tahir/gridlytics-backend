# Round 7 audit: results by package (implementation and review)

Generated on 13 November 2026 from the implementation and review reports of every round 7 package (stages 1 to 3). Each finding's final status is
the implementer's, corrected by the package review where the review changed it. The commits name each finding id (`git log --grep <id>`). The
**design log notes** are the reviews' notes for `docs/DESIGN_LOG.md` round 7 (the docs package writes them in); **remaining** are what each review left.
The audit itself (findings, plan, handover) is on `main`: `docs/audit-r7/`.

## Totals

| Status | Findings |
|---|---|
| implemented | 358 |
| partial | 19 |
| already_fixed | 8 |
| deferred | 2 |
| implemented (was implemented with a part left) | 1 |
| implemented (product half, on audit/storage-perf) | 1 |
| implemented (header half) | 1 |

## alerts-reports

Statuses: implemented 22, partial 2

Not fully implemented:

- `pages-D-16` (partial): The agreement rule took `(end - today).days <= threshold`, which is true for any past day, so an agreement that ended in 2020 read 'ends on' for ever. alerts._agreements now raises a critical alert once the end day has p
- `security-roles-08` (partial): Report generation is now bounded per person, for every role. In POST /api/reports: one person may generate 30 reports an hour (counted in auth's limiter, key ('reports', company, person); GRIDLYTICS_RATE_REPORTS changes 

Remaining after review:

- security-roles-08 stays partial. Its second half is GET /api/sites/{id}/plan/compare, which runs two twin simulations per call with no permission, cache or tighter limit. That route is in routes_plans.plan_compare (delivery-twin in stage 1, storage-perf in stage 2), and the delivery-twin branch has not limited it. Reports also do not run as jobs (202 plus polling, with a request-hash cache, plan 10.6); generation is bounded per person instead. The one-at-a-time set, the two rendering slots and the hourly count live in one process's memory and are lost on restart, so several uvicorn workers wou
- pages-D-16 stays partial: the Tariffs page's 'agreement ended' state and what the price vector does after an agreement ends belong to the tariffs package.
- Sending alerts by email, SMS or WhatsApp is not built, so quiet hours and escalation are stored but do nothing. This stays deferred (HANDOFF 'alert delivery'), and the rule editor now says so.
- The seven-day rule is enforced only in the web dialog. The server still accepts a monthly report for a site with fewer than 7 days of data; such a report is marked Preliminary and says which days its data covers.
- Design consequence of flow-...-09: a rule whose detail has no dating key (unserved, reserve_short, the battery_* rules) stays held after a person closes it until its condition clears completely. A second outage within the same day therefore raises no new alert while the first is closed.
- Audit line 'generated a energy only report' (reports.generate's store.audit) is flow-reports-alerts-members-keys-27, assigned to savings-ui-crosscut in stage 2, and is left as it is.
- Outside this package: routes_sites.fleet_row still shows category 'Off-grid' for a grid site whose connection is not saved yet (fleet/sites package); battery.py's hot_high_soc title still says 'in the last day' (optimiser-plans); at 375 px the rule table scrolls sideways inside its card (parity-ui).

Design log notes:

- **Alert categories by role** (security-roles-03): every member receives Data, Reserve, Battery, Genset and Forecast. Control, Tariff and Savings need `see_costs`; Product needs `see_fee`. An API key gets the energy categories only. `GET /api/alerts/counts` answers `categories[]`. Acting on an alert of a category the reader does not receive answers 404. Both GET rules answers carry only the categories the reader receives.
- **New alert rules** (pages-D-14, pages-D-27, brief-vs-build-18, engine-forecast-10):
  - Data: data_gap, edge data events, and a site that never sent data (it waits from activation under its source's limits: OEM 20/60 min, CSV 24/48 h, otherwise 30/120 min).
  - Control: edge_*, edge_offline, plan_expired.
  - Genset: genset_unrequested, genset_low_load, genset_request_missed.
  - Forecast: forecast stage changes, and forecast_bust, which is a PV error above `bust_pct` (30 %) of the clear-sky ceiling per 30-minute window, held for `bust_hours` (2 h).
  - Tariff: bill_residual; tariff_awaiting, tariff_lapsed, adder_missing_fca and adder_missing_qta (from the 15th of the month); fuel_price_old (diesel 14 days, gas 183). All are read from the stored versions, with no scraping.
  - Savings: savings_negative and savings_withheld. Product: fee_above_third. Control, Savings and Product alerts still wait for an active plan and 7 days of data (`settled`).
- **Thresholds and site overrides** (pages-D-29): `alerts.THRESHOLDS` gives each threshold a default and bounds. The company's rules live in setting `alert_rules`; a site's overrides live in `alert_rules:site:<id>`, which keeps only what differs from the company's rules. New routes are GET and PUT /api/alerts/rules/sites/{id}. Quiet hours are 'HH:MM-HH:MM' in Karachi time, and escalation is 1 to 10,080 minutes.
- **Rule edits are validated** (flow-...-10): PUT rules is checked against the strict request model `alerts.RuleIn`. The body must be {category: rule}; a null category is refused, and every refusal is a 422 of one sentence.
- **Alert history** (pages-D-28): `alerts.history` is a JSON list of {at UTC, action, by, note, assignee?, held?}. `note` keeps the latest note a person wrote.
- **Assignment** (role-operator-31): `alerts.assigned_id` is new. An alert may be assigned only to a member who holds `ack_alerts`, is not switched off and has signed in, listed by the new GET /api/alerts/assignees (needs `ack_alerts`).
- **Closed alerts** (flow-...-08): a closed or cleared alert refuses every action with 409 `alert_closed`.
- **A person's close holds** (flow-...-09): no new alert is raised while the same condition holds. A new one is raised only when the condition clears, a `CONDITION_KEYS` value in the detail changes, or the severity rises. The clearing is recorded as a `condition_cleared` event.
- **Agreements** (pages-D-16): an agreement whose end has passed is a critical alert that says 'ended on'. Alert titles name their day and Karachi time.
- **Genset requests** (pages-C-11): a request is answered once; a second answer gets 409 `request_answered`, and an answer to a withdrawn request gets 409 `request_withdrawn`.
- **Schema version 5** (round 7, renumbered at integration): reports.generated_by, alerts.history and alerts.assigned_id.
- **Report kinds and access** (pages-D-07, role-operator-20): `company_rollup` takes no site and its row keeps site_id ''; `pilot` needs `export` and `see_fee`. A kind is listed and downloaded only by the permission that generates it (`REPORT_PERMISSION`), so an operator no longer reads the monthly savings report or the company roll-up.
- **Report content** (pages-D-07, pages-D-08): the monthly savings report carries plan 8.5's sections, with every figure taken from verify (never recomputed). Rupees round half toward +infinity, as the page's Math.round does. Download names are `gridlytics-<site>-<kind>-<period>.<ext>`.
- **Daily operations sheet** (pages-D-24): its period is a day (YYYY-MM-DD), today or earlier. Today's sheet prints dated tables for the rest of today and for tomorrow from the plan in force now. A past day's sheet prints the plan in force at that day's start, then what happened.
- **Report periods** (flow-...-13): a month must be exactly YYYY-MM and not after the current month. A period that ends before `reports.data_since` (the earlier of the day the site was added and its first day of data, as Pakistan days) is refused. The check runs at the route (`reports.check_period`), and refused requests are not counted against the person.
- **Generation limits** (security-roles-08): one person may generate 30 reports an hour (`GRIDLYTICS_RATE_REPORTS`), one at a time. The server renders at most 2 at once and a further request waits up to 20 s. Each refusal is a 429 with Retry-After. Only the newest 3 copies of each (kind, site, period) are kept.
- **Report file paths** (parity-architecture-11): rows store paths relative to the data folder ('reports/<company file stem>/<id>.pdf|csv'), resolved through `platform.data_dir()`. Older absolute rows are resolved by their last three path parts. Nothing outside data_dir/reports is ever served.
- **Report header** (flow-...-16): a grid site with no saved connection reads '<DISCO> grid connection, not saved yet.' A site is called off-grid only when its connection's regime is off_grid, or, with no connection, when the site is marked off-grid or is a farm.
- **Web: the seven-day rule** (pages-D-25, R8 2.14): the generate dialog offers only the daily operations sheet for a site with fewer than 7 days of data (fleet `collecting_day` <= 7). This is web-only; the server marks such a report Preliminary.
- **Web: wording** (flow-...-29, flow-...-11): the Reports page says in words which kinds the role's list holds. The rule editor marks Email, SMS and WhatsApp 'Not sending yet', and quiet hours and escalation 'No effect yet'.
- **Shared function ownership**: `routes_savings.fleet_savings` carries `chain_stale_days` per site and `stale_sites`.

## auth-security

Statuses: implemented 19, partial 2, deferred 1

Not fully implemented:

- `parity-supabase-auth-08` (partial): One address could send 1,200 sign-in requests a minute (the public rate), each costing a PBKDF2 hash. Tries for an email with no account were never recorded, so they were never locked out, and that told which emails have
- `role-analyst-32` (partial): My account (UI-18) now has the two missing parts. Sessions: GET /api/local/sessions lists the person's open sessions with id, current, started, last_active, ends, address and device; the address and browser are kept per 
- `security-roles-16` (deferred): The owner's non-negotiable rule is that a viewer never sees money anywhere. The per-viewer see_costs grant from plan Part 10.5 that the batch built let a viewer see rupees, so it was removed at review. It needs an owner 

Remaining after review:

- security-roles-16 is deferred to the owner: whether a named viewer may be granted see_costs (plan 10.5) despite the rule that a viewer never sees money. The removed implementation is in commits 3ab651f and 0908c44 if the owner wants it back.
- role-analyst-32 is partial (owner and other-package scope). Report schedules (UI-14) are deferred by the plan. Notification choices are stored (GET/PUT /api/platform/notifications, setting notify:<user_id>) but nothing honours them yet: the inbox and badge belong to alerts-reports and shell-nav, and there is no email or SMS sender (DLV-07, whose provider and cost are an owner decision). The page says so.
- parity-supabase-auth-08 is partial (family-wide decision). PBKDF2 stays at 200,000 iterations, the hash format and cost shared with Solarlytics. Raising it to OWASP's 600,000 needs rehash-on-sign-in and a decision for the whole family. The 30 sign-ins per 5 minutes figure (Supabase Auth's default) and NIST's 100-failure cap are marked [recalled; verify] in code.
- HANDOFF and DESIGN_LOG are untouched (stage 1 rule). The owner's instructions should say that founder-made accounts and new company owners now choose their own password at first sign-in, unless the founder sends one_time false.

Design log notes:

- **The money filter belongs to the API**: `api._without_money` runs `money.filter_json` on every JSON answer to a reader who may not see every money field (viewer, operator, API key). `routes_common.out()` stays as belt and braces. A bare `total` is a count, not money. `backend/tests/test_role_walk.py` walks every GET route as admin, operator, viewer and a site key.
- **New permission `see_money_settings`** (administrator, analyst). Without it, `money.MONEY_SETTING_KEYS` (VoLL, replacement cost, c_cyc, the genset's c_start/c_runhr/c_om, c_short) are stripped. An operator sees costs but not the money settings or the fee (plan 9.4). `asset_put` keeps a stored money setting that a hidden-field form sends back at its default.
- **No per-member grants.** The owner's rule (a viewer never sees money) overrides plan 10.5's "a named viewer may be granted see_costs". This is deferred to the owner, and `PUT /api/users` refuses `grants` with 422.
- **A viewer's savings answer** is the full answer through the filter, with `hidden_for_role: true`. A viewer keeps the litres, verified days and day counts.
- **One-time passwords** (plan 9.3; platform.db schema 2, `accounts.must_change`):
  - An administrator (`manage_users`, local mode) can `POST /api/users/{id}/password` (19 characters from an unambiguous alphabet, shown once) and `POST /api/users/{id}/unlock`. Neither works for the owner (409 owner), for oneself (422) or for someone also in another company (409 other_company).
  - The founder's reset, a founder-made account and a new company owner's first password are one-time unless `one_time` is false.
  - Every path except the identity paths answers 403 `password_change_required` until the person changes the password. In the web app the routing state `must-change` shows only the password form and Sign out.
- **Sessions**:
  - An access token's `session_id` is its refresh-token family. `identity.verify` refuses an ended session with 401 `session_ended`. Sign-out with only the access token works.
  - `sessions_from` keeps fractions of a second. A token naming no session is refused in the second of the change. `new_refresh(checked=)` gives no session to a sign-in that a reset overtook.
  - Refresh rows are kept `ACCESS_MINUTES` past `session_ends`.
  - platform.db schema 3 adds `refresh_tokens.started/address/agent`. My account's sessions: `GET /api/local/sessions`, `POST /api/local/sessions/end` and `POST /api/local/sessions/end-others`.
- **Lockout and throttling**:
  - `MAX_TRIES` 5 per (email, address).
  - `ACCOUNT_TRIES` 20 per email across all addresses in `LOCK_MINUTES` 15, except from an address that signed in within `SESSION_DAYS`.
  - `LOCK_CLEARED` events (from an administrator or the founder) reset the count.
  - Refusals for unknown emails are recorded (`account_id` '') and pruned once they stop counting.
  - `RATE_LIMITS['sign_in']` 30 per `RATE_WINDOWS` 300 s per address (`GRIDLYTICS_RATE_SIGN_IN`).
  - An untrusted forwarded header is logged once.
- **Notification choices**: company-file setting `notify:<user_id>` `{follow, email}` via `GET/PUT /api/platform/notifications`. They are not honoured yet.
- **API keys**:
  - `apikeys.RESOURCES` narrowed: `plans:read` covers /plan, /plan/compare, /plans, /plans/:id and /control; `sites:read` covers /sites/:id, /native and /overrides; site data is in no scope.
  - `KINDS` edge/partner. `EDGE_ONLY` (capabilities, events, ack, heartbeat, signed plan message) is for edge keys only.
- **Members**:
  - In local mode `GET /api/users` lists only the registry's members. A removed or suspended place answers 409 `not_a_member` on PUT.
  - OnyxLeaf adds and removes people (no invite or add).
  - In Supabase mode the list adds the project's `rpc/company_list_members`, called with the caller's own token for the owner or an admin, capped at 1 MB.
  - Name, email or password changes are refused with "OnyxLeaf changes...".
- **Supabase mode on the web**:
  - `VITE_AUTH_MODE=supabase` plus `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`.
  - Password sign-in through Supabase Auth's HTTP API with no package. No sign-up, magic link, invite or provider.
  - `/reset-password` exists only in that mode, for a recovery link that OnyxLeaf sends.
- **Data folder**: the API log goes to `platform.data_dir()/logs`, and the launcher sets `GRIDLYTICS_DB` alongside `GRIDLYTICS_DATA_DIR`.
- **Web**:
  - Routing state `refused` (no_seat, member_disabled, company_disabled) shows the server's sentence in place of the app.
  - Admin-only tabs wait for `/meta` and say who they are for.
  - Selects that write stage the value and send it on Save; promoting someone to administrator asks first.
  - The one-time key dialog stays open until the key is copied.
  - One h1 on each sign-in page.
  - Audit trail details are in words and Karachi time (audit-words.ts).
- **Ownership** (minimal edits in others' files): companies.py (one line), routes_founder.py and store_founder.py (set-password, plus one_time on the create routes at review), test_founder.py (one_time false in helpers), env.ts, router.tsx, login-page.tsx, outage-log.tsx, founder-login.tsx.

## control-edge

Statuses: implemented 18, partial 1

Not fully implemented:

- `engine-delivery-13` (partial): Two problems: the signing key was found by an absolute path saved when the key was made, and when that file was missing a new key was made silently. A new key would sign plans that no edge with the old public key accepts

Remaining after review:

- engine-delivery-13 stays partial: (1) backing up the keys folder belongs to founder-console's backups.py; (2) a signed rotate_key command the edge acts on needs a U3 revision, which is the owner's decision. Until then a replaced key is provisioned on the edge as at enrolment.
- engine-delivery-12 stays partial: R6 C5's optimiser half is not built. Values the edge reports as actually applied (partial or rejected acks) should re-enter the next solve. That needs build_problem and tick, which belong to optimiser-plans. The control state, the UI and the fidelity halves are done. C6 and C8 are covered more strictly: the first mismatch, template change or firmware change pauses control until a person resumes it.
- test_fix_r4_savings.py::test_an_off_grid_days_carry_over_is_valued_at_the_gensets_cost_at_its_night_load (savings package) was reported flaky by the implementer. It passed in both full runs of this review.
- plans.write_log reads every write-log event of the site and filters the day in Python, because events.ts is the edge's own text. That is fine at pilot scale; an epoch column or index would help later (storage-perf, stage 2).
- A Hold above the present state of charge in the evening peak is held by the guard (the solve sheds load with the grid up to keep its MDI cap). The person is now told so, but the hold does not reach the plan until a plannable solve. optimiser-plans' removal of grid-up shedding (noted in test_fix_r2_optimiser) should make it plannable; re-check at integration.

Design log notes:

- **What reaches the device**: only a closed-loop site's edge is sent a plan; below closed loop `signed_message` raises NotClosedLoop and the edge route answers 409 `not_closed_loop`; nothing is recorded as a send.
- **Wrong-plan guard** (Part 7.1 step 8, R6 C2): it returns the first rule broken: `slot_floor_below_fallback`, `replayed_soc_below_floor` (tolerance 0.05), `unserved_in_outage`, `mdi_cap_exceeded` (the plan's own draw plus load it sheds with the grid up, against `res["M"]`, tolerance 1 kW), `sheds_load_with_grid_up`, `genset_slot_with_grid`, `export_not_allowed`. A refused plan is held and the previous one stays in force. A control action's `resolve.reason` names that rule, or the pause or release (review).
- **Plan expiry**: a plan past its `valid_until` is not in force, not served and not keyed in; `held.reason` is `plan_expired` and the edge route answers 404 `plan_expired`.
- **Switchback native-block day** = a release (`switchback_native`) through `control_release`, after a person's pause or release.
- **Edge protocol**: a message is a duplicate only when the same (site, stream, seq, digest without sent_at and msg_id) was stored (`edge_seen`, kept 30 days, schema v3); the current capabilities are the newest by sent_at, all kept; the models live in `contract.py`, exported as JSON Schema by `scripts/export_contract.py` with a drift test; the plan is fitted to the inverter's writable primitives (409 `caps_mismatch` when it has too many slots).
- **Edge reports**: firmware_change pauses control for 7 days, like a template change; control_disabled and fallback are kept in `edge_reports`; a heartbeat counts for 300 s; a closed-loop day's fidelity comes from `plans.edge_fidelity`.
- **Clock ladder**: over 120 s `clock_unsynced`; over 300 s discharge and export slots are widened 15 minutes each side; over 900 s control is demoted (409 `clock_unsynced`) until the offset is under 120 s; the worst of the edge's and the inverter's offset counts.
- **Control actions**: strict bodies, checked action, then permission, then body; hours 0.25 to 72, soc_pct 1 to 100; every action re-solves at once; a resume needs hold_release, plus resume_control while a pause is in force; chip ends are shown in Pakistan time, with the day when not today.
- **Overrides as actions**: genset_hold and charge_block need hold_release; force_charge needs pin_genset (administrator); `placeable`; 409 `no_equipment`; the forced-charge chip outranks a hold's.
- **Advisory tables**: `table_issues` (schema v4); nightly job at 23:05 Pakistan time; a re-issue only when the twin prices it Rs 500 better, under the day's table budget, at least 1 h after the last; a hold or resume issues at once; the write meter counts table writes on the Karachi day, never pulls; the edge gets apply_policy (max 4 table writes) and write_budget (3 table, 2 event).
- **"It took" card**: 30 min after the first peak discharge slot of today's keyed table (`early_check` on the applied row); it never sets the day's class, fidelity or claim.
- **Demo sites**: their control chip is computed as a customer site's, never forced.
- **Closed-loop page**: "No edge device yet" while no edge has reported; GET /control/readback and GET /control/writes (no money).
- **Fallback test**: an administrator's (resume_control), closed loop only, edge heard within 5 min; a 15-minute release; it passes when a heartbeat shows native during the release and the plan within 30 min after; an audit line records it.
- **Signing keys**: kept by file name in `<data>/keys/<company stem>/` (a stored absolute path is read by its name); created 0600 with O_EXCL, folders 0700; never regenerated on their own; a missing key gives 409 `signing_key_missing` plus a critical Control alert, closed on the next signed pull; POST /sites/:id/edge/key (administrator, change_tier, with a reason) retires the old key, keeps its file and writes an audit line; GET /edge `public_key.missing`.
- **Signature**: over the RFC 8785 canonical JSON (`contract.canonical_json`); `verify` also accepts the old json.dumps form; `signing-vectors.json` is published.
- **Edge message limits**: 1 MiB per message (413 `too_large`); free-form parts at most 4096 bytes of JSON and 64 members; an ack item's text value at most 120 characters; a telemetry row at most 64 cells.

## crossmodule

Statuses: implemented 12, deferred 1

Not fully implemented:

- `followup:advisory-keyed-profile` (deferred): Not implemented. At advisory Sungrow and Solis sites, and at Growatt sites beyond its three windows, a planned hold still replays as self-use (delivery.as_keyed), so table_safety (reserve_short_on_repeat) can still hold 

Remaining after review:

- followup:advisory-keyed-profile stays deferred, and I accept the reason as an owner/product decision. At advisory Sungrow and Solis sites, and at Growatt sites outside its windows, a planned hold replays as self-use. The alternatives are a per-step self-use complementarity (about 192 to 384 binaries against the 10 s tick) or holding the reserve all day through the brand's single Min SoC (giving up peak discharge). Today's behaviour is safe: the guard holds the plan and the previous plan stays in force.
- Mode B floor (the stage-1 integration note on _mode_b_floor): the message's soc_floor_pct is min(reserve_pct, soc_end), which can be below the delivered grid-up floor the plan holds (delivered_floor_pct). The twin's setpoint execution follows the step floor. Whether the edge enforces the fallback floor while running Mode B is a control-edge / U3 contract question, so it is not changed here.
- Solve time: with genset charging, the demo farm's 192-step solve stops at the tick's 10 s limit. The implementer measured a gap of 1 to 4 % (under KEEP_GAP 5 %), so the plans are kept but flagged 'Solved within the time limit'. A legacy-ICP site in its uncertain case where the ICP rate is the cheaper adds about 192 direction binaries.
- When the plant connection moves to another connection, bills already entered keep the rebuild stored at entry: the old plant's bills stay rebuilt, and the new plant's stay 'not rebuilt' until each is corrected. A re-rebuild on the move is not done.
- summary.achieved is stored and typed, but no page shows it yet.
- The intra-day bust check runs on every edge telemetry batch: a few forecast issue rows and a 30-minute minute grid. That is small, but it is one more read on the ingest path.

Design log notes:

- **One plant connection per site** (tracker-and-docs-01, pages-D-09). `sites.ConnectionFields.plant`: the connection the inverter, battery and genset are on, whose meter the site's readings are. Marking one unmarks the others, and a save that leaves the field out keeps it. With none marked, the first saved connection is the plant. `sites.connections()` lists the plant connection first with a top-level `plant` flag, and `sites.plant_connection()` returns it, so every engine path that read `[0]` (plan, ledger, alerts, reports, twin, /now) prices it. Per-connection rebuilds need per-connection meter data, which v1 does not have.
- **Bills per connection.** Bills take `connection_id` (the plant's by default; a correction may move a bill; an unknown id is a one-sentence 422). A non-plant connection's bill is kept but not rebuilt: `rebuilt.metered=false`, verdict `not_metered`. The bill_residual alert skips such bills and the PDF prints 'Not rebuilt'. Overlap is checked per connection. Each bill answer carries `connection` and `metering`.
- **Metering factor (VER-13)** (engine-savings-09). `verify.fit_metering` fits, per connection, an energy scalar (sum of printed peak and off-peak units over sum of rebuilt gross imports) and an MDI scalar (printed over rebuilt MDI). It uses at least 2 bills before the latest with at least 90 % meter data, and holds the latest bill out to test the fit.
  - The band is 0.97 to 1.03 (`METERING_BAND`). A factor outside it is `out_of_band`: not applied, raised as the Data alert `metering_factor`.
  - A `calibrated` factor is applied to both arms through `_minute_prices` k_energy/k_mdi (including the month's ICP totals and the perfect-foresight bound), even when the held-out test fails, as the spec says. The 2 % gate closes only when the held-out bill passes.
  - Fits are refitted on every bill entry, correction or deletion and kept in `site_state.metering` by connection id; there is no migration. Stored rebuilds stay on the meter's scale.
  - The price digest and month fingerprint include the factor only when it is not 1, so older digests are unchanged.
- **Punjab self-generation duty** (engine-tariffs-09). `tariffs.selfgen_duty(site, day, conn)`:
  - Province: the plant connection's `tax.province`, else the DISCO's.
  - Category: industrial or commercial; a site without a connection goes by site type (a farm is agricultural).
  - Size: the set's kVA (`rated_kva`, else `rated_kw / power_factor`, default 0.8) must be above 500.
  - Rate: the tax version in force's `punjab_selfgen_per_kwh`.
  - Read by build_problem, verify._genset_cost, the ledger's terminal value, the bound and routes_plans._short_price. `plans.SELFGEN_DUTY_RS_PER_KWH` and the latitude > 28 rule are removed.
- **Mode B executed by the twin** (engine-delivery-17, brief-vs-build-08). New `twin.setpoints_policy` and mode `setpoint` in `simulate`:
  - Each step's battery_power_w is followed within the battery's power and the step's floor and target.
  - A charge beyond the PV surplus draws on the grid only where `grid_charge_allowed`.
  - The battery never discharges into curtailment or above `export_limit_w`. The outage branch is unchanged.
  - `delivery.executes_mode_b` (closed loop and `control_mode` mode_b) decides when the guard, plan_compare and demo._policy_for replay setpoints. The Mode A table stays the fallback for slot-only devices and is still checked.
  - The mode_b message adds `step_s`, `battery_mode`, `soc_target_pct`, `gen_charge_allowed`, `export_limit_w`, and `genset_request` as an enum.
- **Genset charging per brand** (engine-plans-live-18). `delivery.GEN_CHARGE_BRANDS = ("deye", "voltronic")` and `gen_charge_supported(brand)`. `a_gen` is set per brand; `GEN_CHARGE_SUPPORTED` stays False as the default for other brands. Compiled slots carry `gen_charge`, Voltronic keys SNU, `as_keyed` clears the flag for other brands, and the twin charges from a running set towards the slot's target. This supersedes the round-5 rule for these two brands.
- **Event re-solves** (brief-vs-build-15):
  - `RESOLVE_EVENTS` adds genset_start and genset_stop, which wait for the `gen_on` column to show them (`RESOLVE_SHOWS`).
  - Intra-day forecast bust (`alerts.intraday_bust`): judged over the last 30 minutes, each minute against the PV issue in force at it. At least 20 minutes must be read, and the latest issue must be read for 5 minutes and also miss by the site's Forecast `bust_pct` of the clear-sky ceiling. It re-solves once per latest issue (`site_state.bust_resolved_issue`), within the event rate limit, and answers `resolve_reason: forecast_bust`.
  - Approving a tariff version that starts within `RESOLVE_HORIZON_DAYS = 2` re-solves every active grid site in the background and answers `re_solving`.
- **Legacy ICP on gross imports** (engine-tariffs-10, optimiser half). `tariffs.vectors` decides a legacy site's ICP case on gross imports; a period ending the month in surplus is `below`. Each period carries `gross`, `cap_kwh` and `other_inc_kwh`. The uncertain-case rows read gross imports, with the net-import cap and one import-or-export binary per in-month grid-up step, but only where the ICP rate is the cheaper (review). Where it is dearer, the whole gross increment is priced at the ICP rate, so the solve is never infeasible. This supersedes round 3's 'ICP rows count net import'.
- **MDI override in the plan and ledger** (engine-tariffs-08 h). `optimise.mdi_windows` builds fixed or sliding windows, over all hours or the peak only, from `p['mdi']` block and scope. They drive the MDI rows, the guard's MDI check, the month's peak so far (`month_to_date` M0) and `price_arm` (through `tariffs._mdi`). The price digest includes them only when not the default.
- **Native-arm bill** (pages-D-10 rest). `verify.native_bill` produces `rebuilt.native_arm`, the bill priced on the ledger's native replay of each scored day; unscored days take the meter's readings. It carries `days_replayed` and `difference_rs`, and is see_costs only.
- **Acting on the edge's ack** (engine-delivery-12, optimiser half; R6 C5). At closed-loop sites, the edge's latest ack within `ACHIEVED_WINDOW_S` (2 days) feeds the next solve:
  - A refused grid or genset charging permission is planned as off.
  - A cut battery power caps P_ch or P_dis.
  - A floor clamped upwards raises the grid-up floor.
  - A clamped export limit caps X_max.
  - Rounding is not a limit: within the larger of 100 W and 1 %, within 1 point of state of charge, or STEP_ROUNDED without RANGE_CLAMPED. Neither is a value written above the one asked (review).
  - The plan summary records the adjustments as `achieved`.
- **Compare with native: end value** (engine-plans-live-10). The plan summary stores `end_price {c_next_rs_per_kwh, basis}`. The compare arms add `end_kwh` and `end_value_rs`, and the answer adds `end_value_rs_per_kwh`, worked out as max(0, eta_d × c_next − wear). Without a stored end price, c_next is the dearest import price of the horizon's last day. Money keys are hidden by money.py.
- **Perfect-foresight bound** (engine-optimiser-14 follow-up). Both arms pay the month charge at the solve's own lower bound (`M_lb`, `mdi_month_rs`) plus their own rise above it, on the metering factor's scale. A time-limited solve still gives no bound.
- **Ownership.** Shared functions this package now owns: `plans.build_problem` (plant connection, per-brand a_gen, `_apply_achieved`) and `plans.achieved`; `verify` fit_metering, native_bill and the bill-per-connection path; `tariffs.selfgen_duty`; `optimise.mdi_windows`; the `alerts.intraday_bust` and `_metering_factor` rules; `twin.setpoints_policy`; `delivery.executes_mode_b` and `gen_charge_supported`. It made minimal edits to routes_tariffs.approve, reports._bills, test_fix_r5_genset.py (one assertion), the web compare-drawer, slots-view, plan-views, versions-table and alert-words. There is no store.py migration.

## delivery-twin

Statuses: implemented 11, partial 1

Not fully implemented:

- `engine-delivery-21` (partial): Brand profiles now apply in the replay of a table a person keys (TWN-02). delivery.as_keyed(brand, slots, compiled) uses the same register facts as the vendor tables (R6 3.1 to 3.4) and records, per slot, only the fields

Remaining after review:

- engine-delivery-21 (low) stays partial. Brand profiles now apply to every replay of a keyed table. Still missing: (1) DLV-00, the brand data files delivery/brands/*.json with provenance blocks and bench_confirmed per value. DESIGN_LOG settles 'brand grammars are a dict in delivery.py, not JSON files yet', and DLV-00 is a tracker item of its own whose values need provenance lines and, for bench confirmation, hardware or OEM documents. (2) TWN-02's acceptance 'capabilities messages per profile validate': the capabilities schema is control-edge's (contracts/ems-edge/1.0 on its branch), so this ca
- Behaviour the owner should know about: at an advisory (keyed) Sungrow, Solis or Growatt site, a planned hold now replays as self-use (as_keyed). The safe-to-repeat check (table_safety, reserve_short_on_repeat) can then hold more plans for these brands until the optimiser plans within each brand's profile (build_problem, stage 2). No test or demo site hit this; the demo's Sungrow site is closed loop.
- A small fidelity gap, left as is: in a Huawei charge slot with an early charge end, the twin's hold tail does not store surplus PV past the target, which Huawei's 'Excess PV in TOU: charge the battery' would. This only matters for a daytime grid charge with surplus PV.
- Mode B execution in the twin (engine-delivery-17) and genset charging (engine-plans-live-18) remain stage 2's, as the plan says. The twin-priced boundary ranking (HANDOFF 8.A.2) stays out.

Design log notes:

- **Vendor tables (Part 7.1 step 4, DLV-02):** every brand keys in its own layout. Each row's keys are exactly `grammar.fields`, in order. A per-row SoC keys a charging slot's target and any other slot's floor. `GET /control` adds `vendor_slots` (the compiled slots each row keys), `vendor_settings` (values keyed once: `{key, label, value, unit}`) and `vendor_notes` (one sentence for each thing the brand cannot hold as planned).
  - Grammar changes: Deye keys all 6 programs of its ring, self-use at the rated power, a hold at 0, and a `sell` flag. Huawei keys charge and discharge periods only, every day; a hold is a gap; an end of 00:00 is keyed as 23:59. Huawei keeps one grid-charge cut-off (the highest grid-charge target), one discharge cut-off (the lowest floor of a discharging slot), and the MDI cap as a capacity-control power.
  - Growatt's `max_slots` is 6 (3 Battery First and 3 Grid First windows, with Load First between them). Sungrow has 2 forced-charge windows and self-consumption at a Min SoC of at most 50 %. Voltronic has SUB/SBU and SNU/OSO changes. Solis keys a current, which is the power divided by `limits.battery_v` (null until the battery's nominal voltage is known).
  - Brand grammars stay a dict in `delivery.py`; DLV-00's JSON files are still to come. None of the register facts (R6 3.1 to 3.4, R8 2.15) is bench-confirmed.
- **MDI cap (supersedes the R? rule 'every slot carries grid_cap_w'):** a slot carries `grid_cap_w` only where `cap_held` is true: the brand has a peak-shaving primitive (Huawei only), or an edge runs the table at the closed-loop tier. Elsewhere, `_translate_mdi` gives a discharging slot room to keep the cap: the need is max(0, Lhi − PVlo − cap), where subtracting PVlo deviates from the plan's max(0, Lhi − cap). The slot's power is raised and its floor lowered, never below the clamp or the reserve, and the slot records `mdi_shave_kw`. The compiled table carries `mdi {cap_kw, held, not_enforceable}`: the demand the keyed table reaches, averaged over the connection's MDI interval (default 30 min), when it is more than 0.5 kW above the cap. `compile_mode_a` without a site keeps v1's behaviour (cap held, no profile); `plans.tick` passes the site.
- **Slot reduction:** equal neighbours are merged after every drop. A short run that moves energy is widened to the brand's minimum length instead of folded into a neighbour, a deviation from step 1. Slots carry `genset_runs`, and `twin.slots_policy` runs the set only in them: 0 elsewhere in the slot, -1 only for slots stored before.
- **Fallback block (Part 7.1 step 6 rules over the opening line's 'revert after 3 hours'; DLV-03):** the block is `{on_expiry, on_link_loss_after_s: 300, min_soc_floor_pct, max_export_w_floor (W), native_settings_ref "site_id:vN"}`, with no revert timer. The floor is max(BMS low + 2, owner's reserve, 10 %); `FLOOR_MIN_PCT` 10 is new. No slot floor is below it. A release message's `valid_until` is at most 48 h after issue.
- **Safe tail (R6 I1):** a discharging last slot stops at max(the reserve at the day's end, the fallback floor). `compiled.safety` reports per day `floor_below_fallback`, `tail_below_reserve`, or `reserve_short_on_repeat`. The last means tomorrow's whole-day table, repeated, reaches an outage more than 2 points below min(reserve, planned SoC); this check runs on whole-day tables only. A table that is not safe gives the new guard reason `unsafe_table`. A part-day table gets a note saying what runs round midnight.
- **Twin:**
  - New functions `fallback_policy` and `state_changes` (`FALLBACK_STATES`: plan, unattended, native, repeat), `repeated_slots`, and a policy key `export_cap`.
  - Peak shaving stops at the slot's own floor; it used to go down to the BMS floor.
  - The native replay charges at each captured slot's `max_charge_kw`.
  - `charge_end_ts` is enforced as a hold tail (no grid charge, floor raised to the target) where the table or an edge can stop the charge. Otherwise `as_keyed.charge_end_ts` is null and the slot reads 'planned to finish charging by HH:MM'.
  - An export slot discharges at most the load plus min(the plant's export cap, the fallback cap). Battery output nobody takes is never booked as curtailed PV.
- **Brand profiles (TWN-02, partial):** `delivery.as_keyed` is set on each slot wherever a site is given and the tier is not closed loop. It holds only the fields the brand runs differently (`mode`, floors and cut-offs, `grid_charge`, `max_discharge_w`, `pv_full`, `charge_end_ts`). Every replay follows it: the guard, the safety check, the MDI report, compare, the demo and fidelity.
- **Compare with native (R8 2.9):** the API answers `{compare: null, reason}` (no_plan, no_battery, no_capture, or too_little_data, meaning under `COMPARE_MIN_DATA_S` = 86,400 s of telemetry). It never compares against an assumed self-use table. The answer names the capture replayed in `native {version, captured_at}`.
  - Each arm carries `reserve_short_kwh`: Σ p_start × max(0, reserve_pct − end-of-step SoC) × E_nom, over grid-up steps with p_start ≥ 0.01.
  - Each arm also carries `reserve_short_rs`, priced at `short_rs_per_kwh`. `short_basis` is 'genset', the all-in cost at 75 % load where a genset starts, or 'lost_load' (VoLL). This copies build_problem's c_short rule.
  - Viewers and API keys get the kWh only. The `_rs` keys are stripped, and the drawer gives a viewer no pricing words.
- **Ownership and migrations:** no migrations and no new permissions. Edits outside owned files were kept small:
  - `plans._guard`: the `unsafe_table` check.
  - `plans.tick`: `site=site`.
  - `plans._release_message`: `fallback_block` and the 48 h cap.
  - Web `plan-held.tsx`: a GUARD_REASONS entry.

## demo-seed

Statuses: implemented 6, partial 1

Not fully implemented:

- `tracker-and-docs-21` (partial): seed_demo's docstring and --from help named demo/companies.template.csv and demo/users.template.csv, but neither was in the repository. Both are now in the backend's demo/ folder, with placeholder people on example.com i

Remaining after review:

- tracker-and-docs-21 stays partial. The PLT-05 line in the plan tracker still says [x] for 'sites.template.csv'. It is in /home/user/plan (build-plan/EMS_BUILD_PROGRESS.md and EMS_BUILD_PLAN.md), outside both repositories, so the docs package or the owner has to change it. Everything inside the repositories is done.
- Demo sites built before this branch keep their old story until an administrator removes them and loads them again. That old story is: no data_source, every site tier_capable closed_loop, no edge records, and no installer marks for past days. A recompute of their past advisory days would now show not_applied. No runtime migration was added, because the owner's data folder must not be touched. After integration the owner should remove and reload the demo.
- Low: a demo site's spec is found by its name (demo._spec, and build_history's existing switchback lookup). If an administrator renames the Faisalabad mill or the farm, the demo installer stops marking its tables, and on unmarked days that site runs its native settings. A sturdier version would store `marks` in the site's data.twin, which an update cannot change, and fall back to the name for older builds. It was left as is because it follows the existing pattern and renaming a demo site is unlikely.
- Low, docs: the older rows of the web app's docs/API_CONTRACT.md (line 39 and the table row at line 280) still say a load needs edit_sites. The shared file is append-only, so the appended '## Round 7 (audit): demo-seed' section records the change. The docs package should update those rows.
- Not this package's ids, still open in files this package owns: the Demo tab's sentence that the demo sites 'are rebuilt every night so their history ends yesterday' is untrue, because no nightly rebuild exists (tracker-and-docs-20, docs package). The Fleet empty state still says '90 days' and still offers 'load the four demo sites' to an operator who can no longer load them (fleet-page.tsx, fleet-overview's pages-A-17).

Design log notes:

- **Demo data sources (round 7, audit role-owner-15):** every demo site records `data.data_source` with its grid signal confirmed. The Karachi feed mill's source is an edge; the other three use the inverter maker's cloud. A cloud or CSV source supports advisory writes (`demo.SOURCE_TIER`, the same as onboarding's SOURCE_CAPABLE). An edge's tier comes from its capabilities message, which the demo writes for the mill, dated from the start of its history. Native capture sources: mill `read_back`, mall `photo`, farm `entered`. Only an edge reads back.
- **The twin is the closed-loop demo site's edge (plan 2.5):** after each span it runs, it updates `edge_last_seen` and a heartbeat (`following_plan` or `native`). It writes an `applied` acknowledgement the first time it runs a plan. GET /edge counts a demo site's edge as online for 20 minutes after its last report (`demo.EDGE_FRESH_S`); a real edge still gets 5 minutes. `demo.remove` also deletes `acks` and `capabilities`.
- **One keyed table a day at an advisory demo site (audit engine-delivery-18; narrows round 5's "the demo history re-plans every 3 hours" to closed loop):** the inverter runs the latest table marked applied for the day. If none is marked, it runs the plan in force at the day's start (the nightly table) where the demo's installer keys tables (the spec's `marks`); otherwise it runs its native settings. The table runs as compiled, genset flags included. The closed-loop site still runs every new plan.
- **The demo installer's marks:** `Demo installer`, method `vendor_app`, one mark a day, within every brand's write budget. The farm is marked the same day. The Faisalabad mill is marked the next morning, so its own day is never marked yet. Observation days, switchback native blocks and days the plan has no table for get no mark. A mark is dated when it was due.
- **verify.compute_day:** a demo site is no longer confirmed by construction; only closed loop is. A demo advisory day is confirmed only from its mark and telemetry (plan 7.6), as at any advisory site.
- **alerts:** the demo mill's apply_unconfirmed alert holds only while today has no applied mark.
- **The demo farm stores `data.off_grid = true`** (audit engine-plans-live-20).
- **twin.truth carries the previous day's last outage past midnight** (audit role-operator-29). It replays the previous day's draws with the same seed and order; windows and trips are both carried. A day whose previous night ended with the grid up is unchanged. Only truth() changed; twin.simulate is untouched.
- **The demo build lock is per company file** (`demo._lock_for(store.db_path())`; audit security-roles-15).
- **GET /api/demo answers `days`** (audit pages-A-04). Its source, in order: the build's own figure; for an older status, total divided by the number of demo sites; otherwise `demo.default_days()`. That is GRIDLYTICS_DEMO_DAYS when it is a whole number above 0, else 21. A load asked for fewer than 1 day builds the default. The Demo tab names the figure ('21 days of simulated history'), or says 'simulated history' with no number.
- **Loading the demo sites needs `edit_workspace`** (administrator only, plan 9.6 "made by an admin only"; the demo half of audit role-operator-07), as removing them already did. The web app shows "Load demo sites" only to that permission. No new permission was added.
- **Seed templates:** `demo/companies.template.csv` and `demo/users.template.csv` are in the repository, with placeholder people on example.com. seed_demo names only those, and a second run is tested to keep what the first made (audit tracker-and-docs-21).
- No store migration. Minimal edits to shared functions owned by others: routes_edge.edge_status (control-edge), verify.compute_day's demo branch (savings-engine), alerts.evaluate's demo branch (alerts-reports).

## deploy-ci

Statuses: implemented 14

Remaining after review:

- No CI run has happened. actionlint could not be run (its download was denied), so the workflows are checked only by YAML parsing and by shape tests. The first real run happens when the owner pushes and adds the FRONTEND_READ_TOKEN and BACKEND_READ_TOKEN secrets; until then the tests that read the other repository skip, with a warning.
- The shared venv (/home/user/gridlytics-backend/.scratch/venv) still has the old pins (fastapi 0.141.1, PyJWT 2.10.1, cryptography 46.0.3 and others, without httpx2). The suite passes on both pin sets. After integration, run pip install -r requirements-dev.txt in it.
- The web lift_manifest.json's upstream_sha256 values are null because no solarlytics-frontend checkout is on this machine. Local changes are held now; the upstream comparison runs only once a checkout is beside the repository.
- As shipped, vercel.json's connect-src names the placeholder https://api-host.invalid, so a Vercel build stops on purpose until the owner writes the API host there (DEPLOY.md). The base image is pinned to a release, not yet to a digest (owner checklist).
- fetch_tariffs.py and fetch_schedules.py (plan 10.1) are deliberately not built: the planner's note says not to scrape NEPRA or OGRA.
- parity-ui and docs (stage 3) have no branches yet. The planner meant deploy-ci to run after parity-ui; it ran first, so their work must pass the new lint and format gates (see cross_package).

Design log notes:

- **CI (DEP-01), new `.github/workflows/ci.yml` in both repositories.** Read-only permissions; actions pinned to major v7. A push or pull request cancels the run it replaces; weekly and by-hand runs are never cancelled.
  - Backend jobs:
    - `backend-linux` on every push and pull request: Python 3.12, `pytest -n auto -rs`, gridlytics-frontend checked out beside it with `FRONTEND_READ_TOKEN`.
    - `backend-windows` on a pull request into main, by hand and weekly.
    - `audit`: `pip-audit -r requirements.txt`.
    - `licences`: pip-licenses against the plan 14.3 allow-list.
    - `bench` (`pytest -m slow`, alone on a runner) and `image` (build, health check, no server header, uid 10001) on a push of v1-build, by hand and weekly.
  - Web jobs:
    - `frontend`: typecheck, lint, format:check, coverage, a local build with `deploy_check.mjs --built`, and the launcher's tests.
    - `frontend-windows`.
    - `audit`: `npm audit --omit=dev --audit-level=low`.
    - `browser` (weekly and by hand): a throwaway folder seeded with 14 demo days, `start_local.py --scratch`, pages.mjs for every role with `--phone --founder --acceptance`.
  - Without a token, the run warns and sets `GRIDLYTICS_SKIP_SIBLING=1`; each skipped test names the missing token. The only secrets are the two read-only tokens.
- **Hosted API (DEP-06).**
  - `Dockerfile`: python:3.12.16-slim-bookworm, the API only (package, pins, restore script), uid 10001, `/data` volume, `GRIDLYTICS_SCHEDULER=1` set in the image, one uvicorn process run with `--proxy-headers --no-server-header`, HEALTHCHECK on `/api/health`. No sign-in mode is baked in.
  - Port 8061 for the image and the release. The compose network is 172.30.61.0/24; its gateway 172.30.61.1 is the address `FORWARDED_ALLOW_IPS` names.
  - Rule: every GRIDLYTICS_ setting the package reads (plus `GRIDLYTICS_PORT` and `FORWARDED_ALLOW_IPS`) is in `.env.production.example` and in docs/DEPLOY.md, and nothing stale is. Secrets are empty, and Supabase settings are commented out. test_deploy_docs enforces it (meets PLT-02).
  - docs/DEPLOY.md belongs to deploy-ci.
- **Release and load scripts (DEP-04, DEP-02).**
  - `scripts/build_release.py` builds `release/gridlytics-<version>/` and its zip: local-mode web build, `run.py` on 127.0.0.1:8061 with its own `data/` and `data/.env.founder`, and a `restore_backup.py` rewritten to the release's paths. Its `RESTORE_FIND` line is a contract with founder-console's restore_backup.py.
  - `scripts/load_check.py` refuses ports 8051, 5195, 8061, 8041, 5192, 8060, 5193 and any busy port. It uses a temporary data folder, turns the rate limits off and runs no scheduler.
  - No fetch_tariffs or fetch_schedules (no scraping of NEPRA or OGRA).
- **Pins.**
  - The shared sign-in stack is pinned as solarlytics-backend c853130 pins it, and is raised in both products together: fastapi 0.150.0, starlette 1.7.0, pydantic 2.13.5, uvicorn 0.54.0, PyJWT 2.15.1, cryptography 50.0.2. Dev: pytest 9.1.1, pytest-xdist 3.8.0, httpx2 2.13.1.
  - The solver pair (scipy 1.17.1, numpy 2.4.3) stays held, with the reason written in requirements.txt.
  - Rule (tested): every runtime pin is imported or run, and every import is pinned. pandas is removed.
  - Web: date-fns and @radix-ui/react-select are removed. @radix-ui/react-popover is now used, so it stays.
- **Vercel.**
  - `frontend/vercel.json`: every path except `/assets/` is rewritten to the page. It sends a CSP (scripts from the page's own host only, `frame-ancestors 'none'`), nosniff, `X-Frame-Options: DENY` and `Referrer-Policy: no-referrer`, and caches `/assets/` immutable for a year.
  - The connect-src placeholder `https://api-host.invalid` stops the build until the owner sets the API host.
  - `deploy_check.mjs` runs before the build (mode, https `/api` address, no secret `VITE_` setting, connect-src) and after it (`--built`).
  - Rule: router.tsx compares `import.meta.env.VITE_AUTH_MODE` directly, so a local build carries no Supabase sign-in.
- **Ignore rules.**
  - Backend: `.env`, `.env.*`, `!.env*.example`, `platform.secret` at any level, `*.pem` (no private key in the repository), `demo/*.local.md`.
  - Web: `.pytest_cache/`, `!.env*.example`, `data/platform.secret`, `demo/*.local.md`.
  - The launcher reads `.env.founder` from the data folder only, never from beside it.
- **Launcher.**
  - On Linux and macOS each child leads its own process group; stopping sends SIGTERM to the group, then SIGKILL after 5 s.
  - SIGHUP and SIGTERM end the launcher as Ctrl+C does, so its servers stop with it.
  - The API runs with `--no-server-header`.
- **Lint and format gate (web).**
  - ESLint 10 flat config (the plan's ESLint 9 is out of support): typescript-eslint recommended, react-hooks rules-of-hooks and exhaustive-deps, simple-import-sort, `--max-warnings 0`, unused disable comments reported.
  - Prettier 3, width 150, with the Tailwind plugin; the recorded API fixtures are left as written.
  - Coverage of at least 60 % of lines.
  - `npm run gates` is typecheck, lint, format:check, coverage, build.
  - Rules left off, with reasons in BUILD_NOTES: react-refresh only-export-components and the React Compiler rules.
  - Format pass aee4a86 is listed in `.git-blame-ignore-revs` (merge, never rebase).
- **Lifted-code drift.**
  - `backend/lift_manifest.json` (solarlytics-backend c853130, 27 files: 3 identical, 23 adapted, 1 extract) with test_lift_drift.py.
  - Web `lift_manifest.json` (solarlytics-frontend d582a5f, 14 files) with lift-drift.test.ts.
  - Rule: a change to a lifted file must record its new sha256 and a reason (and whether Solarlytics needs it). With a checkout beside the repository, the tests also catch upstream changes since the lift.
  - Shared onyxleaf-platform / @onyxleaf/auth packages are an owner decision (BACKEND_ASKS).
- **Edits to other packages' files, kept minimal.**
  - router.tsx (parity-ui): one line.
  - tests/sibling.py (tests-ci): the skip reason.
  - test_fix_r7_storage_perf.py (storage-perf): the reserve pinned so the test no longer depends on the hour.
  - field.tsx, telemetry-page.tsx, authstate.ts: eslint-disable comments, each with its reason.
  - use-onboarding.ts: isSaved memoised.

## fleet-overview

Statuses: implemented 15

Remaining after review:

- The Fleet's 'Diesel avoided this month' tile is shown even when no site in scope has a genset, and then reads a dash (with the kWh moved off the peak in its sub). It is a small UI choice and is left as it is
- on_plan still counts a hold at an advisory_writes site whose table is not confirmed today, or at a closed-loop site whose edge is offline, because control_state (control-edge's) names the hold before those states. Only the clear monitor-tier case is fixed here
- The Overview Battery tile's mark uses the snapshot's soc_source, while the tile's figure comes from battery.state's last known minute. These differ only when the latest row has no SoC, and then no mark is shown. At integration, telemetry-genset-battery's routes_telemetry._soc_source could be used instead
- GET /sites/:id/outages keeps off_grid=true for any site with no saved connection; only the page wording distinguishes the two cases. forecasts-outages' new forecast.off_grid(site) uses the same no-connection rule, so the Forecasts panels of a grid site with no saved connection will still say 'off grid' after integration
- fleet_row calls verify.summary and sites.assets for every site on each 60-second fleet poll (left to storage-perf)
- components/charts/recharts-a11y.test.tsx will fail at integration until telemetry-genset-battery's new ComposedChart in genset-cards.tsx gets accessibilityLayer={false}

Design log notes:

- **Month-to-date saving (Fleet, Overview):** it is `verify.summary(site, 1st, today)` with the native baseline's `claimed_rs`, MDI lever included, the same figure as the Savings page's "This month". It is null while no day of the month is scored. `/fleet` `summary.mtd_saving_rs` is the sum of the rows, and null when no row has a figure (a real 0 stays 0).
- **Method and computation date** go with the figure as money keys: `mtd_saving_rs_method`, `mtd_saving_rs_computed`, `month.saving_rs_method`, `month.saving_rs_computed` (`counterfactual`, ISO UTC). money.py strips them for viewers and API keys by the `_rs_` rule. The page shows them in Karachi time ("computed 9 Nov 2026, 05:40"; on Fleet, "last computed").
- **Off-grid is the site's own fact:** `routes_sites._off_grid(site, conn)` is a connection of regime off_grid, or, with no connection, a farm or `data.off_grid`. `lib/site-rules.isOffGrid` and `gridSettled` mirror it. A grid site whose connection is not saved yet has fleet `category: null` ("Grid connection not saved") and counts in `in_outage`. The Outages page says its connection is not saved yet. The outages API flag keeps meaning "nothing to track".
- **7-day bars:** `spark[].claimed_rs` (a money key) sits beside `saving_rs`, and the bars draw the claim. All rows share one scale, from zero and at least Rs 1,000 tall. A day under Rs 0.5 draws no bar. The bars are plain SVG named with each day's figure.
- **Viewer substitutes:** `mtd_peak_kwh_avoided` and `mtd_litres_saved` per row and in the summary, read by every role. They count only claiming days (fidelity confirmed or partial, claim not withheld), and are null off-grid (kWh), without a genset (litres) or with no such day.
- **Plan and handed-back states:** `on_plan` = a plan exists, control is in ON_PLAN (following, hold, force_charge), and the tier is not monitor. `on_native` per row = control in HANDED_BACK (native, paused, native_block). The summary and the web app both count the rows. `has_genset` per row.
- **Overview today_kpis**, Pakistan day: `pv_self_pct` = 1 − export/PV; `battery_cycles` = discharge / (nameplate × SoH), as the Battery page counts them; `outages` and `outages_covered`, where covered means no unserved critical load (an unmeasured unserved reading counts as 0, as in the outage log). Each is null without its data, and outages are null off-grid.
- **/now additions:** `genset_requests` lists the pending unanswered start requests, newest first, at most 3. `now.soc_source` is `estimate` or `bms` (null without a SoC), shown as "(estimated)" or "(BMS)". Overview chart `peak[]`: `plans.peak_mask` per actual step; all false off-grid, without a connection or when the tariff cannot be read.
- **Permissions on the new actions:** Overview Hold and Release need hold_release and are not offered at the monitor tier (R8 2.4). Answering genset requests and acknowledging alerts need ack_alerts. Fleet row actions: Release (hold_release) and Pause (resume_control), neither at the monitor tier, all through the Devices and control ActionDialog. The tariff footer asks `/prices` only for a role with see_costs; a viewer gets the peak window from the chart's flags.
- **Stale reading:** a Fleet Now value older than 30 minutes (freshness "old") is greyed under "Offline since …".
- **Times:** `lib/time.pktWhen` gives HH:MM today, "Tomorrow HH:MM", else "Sun 8 Nov, 21:00", for next events up to 48 h ahead and the Fleet's Next column.
- **Accessibility:** every recharts chart passes `accessibilityLayer={false}` (a source-scanning test enforces it). Search, filter, range and period results are announced through role=status lines that stay on the page (Fleet, alerts inbox, Telemetry, Outages).
- **Activation:** a site that is not active is told it gets no plan from the 15-minute tick, and is offered Activate (POST /activate, edit_sites) on the Overview, Plan and Fleet pages.
- No store.py migration, no BACKEND_ASKS entry, HANDOFF.md and DESIGN_LOG.md not touched.

## forecasts-outages

Statuses: implemented 13, partial 1

Not fully implemented:

- `engine-forecast-16` (partial): Before this fix, the stage-1 PV bands were 14 daily clearness quantiles applied the same way at every step, and the load bands were a standard deviation over 7 values with a 6 % floor. Nothing measured how well the bands

Remaining after review:

- engine-forecast-16 stays partial: the stage-2 xgboost models (FOR-05 PV, FOR-07 direct multi-horizon load) wait on the owner's Open-Meteo licence (O5) and on weeks of real-site data. CRPS is not reported separately; the pinball loss averaged over the seven quantiles stands in for it.
- brief-vs-build-09 stays partial: season and weekday effects in the outage hazard need real feeder data (stage 2), and the CCMS and K-Electric schedule scrapers (FOR-08) need an owner decision.
- Flaky test from before this package: backend/tests/test_fix_r4_savings.py::test_an_off_grid_days_carry_over_is_valued_at_the_gensets_cost_at_its_night_load fails about 1 run in 5, also with this package's backend files at the base. twin.truth seeds its random numbers from the site id, which is random in each run. The test belongs to the savings or tests-ci owners, not this package.
- pk_calendar.py has the announced lunar dates for 2024 and 2025 only. Later years are estimated from the arithmetic Islamic calendar and flagged estimated, including 2026's dates, which have already passed. The owner should extend OBSERVED as dates are announced.
- frontend/src/lib/api/endpoints.ts still types the refresh answer's stages as Record<string, number>, though a kind that does not apply now answers null. The stage card guards with typeof. The line was left alone because savings-engine edits the line next to it; integration can change it to number | null.
- The conformal error table is cached per site, kind and local day. Telemetry back-filled later for past days is only used from the next local day (by design, like a nightly refit).

Design log notes:

- **PV stage 1** fits the performance ratio and the clearness fan on the 14 whole local (Pakistan) days before the tick's own day (`_local_midnight`). The last-hour blend still reads up to t0.
- **Available PV:** the fit, the blend, the scores and the page's actuals read output plus `curtail_w` (`forecast.available_pv`), not curtailed output.
- **3 to 6 fitted days:** the prior keeps its shape, and its median moves from MONTH_K towards the site's median clearness with weight days / 7.
- **Scores:**
  - PV's baseline is smart persistence: clear sky times yesterday's clearness at the step; two days before for 24-48 h; the last hour's clearness for 0-2 h, which is reported only.
  - Load's baseline is the same slot a week before. Load is scored on unshed minutes, and the baseline drops its censored minutes too.
  - The model is scored over the baseline's slots. Each kind gets a row per horizon class (0-2, 2-6, 6-24, 24-48 h) in `forecast_scores.horizon_class`.
  - `last7` means the last 7 calendar days in Pakistan time. Skill is 1 minus the mean score over the mean baseline of the days that have one.
- **Band calibration** (`coverage_80` with target 0.8 ± 0.05, `coverage_90` with target 0.9, `pinball`) is stored as day-ahead rows of `forecast_scores` under their own metrics. No migration. These rows are kept out of `days`.
- **Conformal bands (stage 1):**
  - Errors come from the stored stage-1 issues every third local hour over the 14 days before the issue's day: PV over clear sky on daylight steps only, load over the median on unshed minutes only.
  - They are binned by horizon class and, for PV, by the sky of the last daylight hour: overcast < 0.4, intermediate < 0.8, else clear.
  - A class needs 48 errors from 3 days (PV) or 7 days (load). A thin sky class falls back to every sky. The median is unchanged.
  - The table is cached per site, kind and local day. Issues carry `bands{method, sky, classes, sky_classes}`.
- **Forecast page:**
  - `past`: the 96 steps before the belief time, with their actuals and the issue held a day before.
  - `weather_feed: false` until FOR-02 (the owner's Open-Meteo licence).
  - The now line marks the present, not the belief time.
- **Preliminary:** a forecast is Preliminary until the site has 28 verified days (`telemetry_days` with completeness ≥ 0.5) and the forecast is stage 1. `forecast.preliminary()` is the shared rule, and the answer carries `verified_days` and `verified_days_needed`.
- **Kinds that do not apply:** `not_applicable` is `no_pv` or `off_grid`. Such a kind has no stage, page or score, and old rows are left out. The off-grid outage issue is still saved (stage 0) for the plan's same-quarter rule. The plan summary's `stages` leaves such a kind out.
- **Schedule adherence:**
  - Beta(8, 2), updated per window occurrence on its local day; an occurrence counts only when seen whole with at least 50 % of its minutes known.
  - `adherence` is null without a schedule, and `adherence_windows` counts the windows seen.
- **Outage events** come from the censoring's runs (15-minute gaps and 2-sample flickers bridged), for the hazard and for the observed outage's start.
- **Announced windows:**
  - An outage starting up to `ANNOUNCED_EARLY_MIN` = 15 minutes before a window, and still down when it opens, is that window's shedding.
  - This applies in the log (`scheduled`, default cause) and, since the review, in the unscheduled hazard.
  - `announced[]` blocks are given over the outage chart's span.
- **Rural cold start:** `COLD_START_DOWN_HOURS` = 10 h a day for QESCO, PESCO, SEPCO, HESCO and TESCO while no schedule is entered. The part the trip prior does not cover is spread in 2-hour blocks (`SHED_BLOCK_STEPS` 8) with weight 7 / (days + 7), flagged `shed_prior`.
- **Feeder pooling:**
  - Sites pool when they share the DISCO, the unit kind and the code (trimmed, any case), at most 20 sites, within the company's own file. A unit without a code is not pooled.
  - Review rule: real sites pool real sites only, demo sites demo sites only, and off-grid sites never.
  - The issue carries `days_known` and `pooled{sites, days}`.
- **New permission** `refresh_forecasts` = {admin, analyst} (plan 9.4, R8 2.8). The refresh saves through `plans._saved_outage` and `plans._keep_observed_outage`, called read only.
- **Critical load learnt from outages:**
  - Per local hour: the 75th percentile of the load while the battery alone carried the site. That means grid down; genset off (unknown counts as running at a site with a genset); state of charge known and more than 5 points above `soc_min`; nothing unserved; not censored.
  - An hour is learnt from at least 30 minutes on at least 2 days. Other hours keep the entered profile.
  - The issue carries `crit_learned_hours` and `crit_zero`.
- **Load day types:**
  - New module `pk_calendar.py`, a dated table: federal fixed holidays; the lunar dates announced in 2024 and 2025; other years estimated from the arithmetic Islamic calendar and flagged estimated.
  - New `SiteFields` `off_days` (default ['sun']) and `half_days` (default []), in the JSON `data` column, so no migration.
  - Fallbacks: Eid → holiday → off day; Ramadan or half day → working day. Off-type days draw the template's Sunday shape. The load issue carries `calendar[]`.
  - The two-hours ratio compares only with history before t0 − 2 h.
- **Reserve adequacy:**
  - New module `reserve.py`. An outage's target is the floor at the end of the step before its step, from the plan in force then.
  - An outage is forecastable when the plan gave p_start ≥ 0.01 or modelled the step as grid down. Met means at or at most 2 points under the target.
  - `reserve.adequacy(site, t0, t1)` serves the pilot gate. The Outages KPIs add `genset_hours`, `battery_hours`, `reserve_outages`, `reserve_met` and `reserve_adequacy_pct`. None of it is money.
- **Outages heatmap** adds `dates` and `by_date[date][hour]`: outage minutes by Karachi date and hour, null where there was no reading or the hour has not begun.
- **Ownership:** this package made minimal edits to plans.tick (optimiser-plans), auth.PERMISSIONS (auth-security), sites.SiteFields and native-capture-form.tsx (sites-onboarding). No store.py migration.

## founder-console

Statuses: implemented 14, already_fixed 1

Not fully implemented:

- `founder-console-11` (already_fixed): Already committed earlier on this branch; I checked both commits. Backend: store_founder no longer returns the lifted commercial-plan field (_plan, 'plan'), and the dispatch-plan counts (plans_issued, plans_month, plans_

Remaining after review:

- No test fails. Every finding in the four batches is implemented. The halves of the late findings parity-architecture-11, parity-architecture-16 and performance-live-07 that belong to this package (the backup halves) are done; their other halves belong to other packages.
- The wording of the founder's passwords depends on the sign-in mode, which the console cannot see. Set a password says 'It is a one-time password'. That is true in local sign-in once auth-security's 70c8a10 is integrated, but not in the optional Supabase mode. The first-password hints in Make an account and Make a company still only say 'who can change it under My account', although auth-security's 73103bc makes that first password one-time by default in local mode. Neither is false in the default local mode after integration, but the creation hints are incomplete.
- founder-console-04 departs from EMS_BUILD_PLAN.md:839 ('everyone starts as viewer'): a company Admin is now a Gridlytics administrator by default (not pinned). The plan's package notes allowed this choice, and it is recorded in docs/BACKEND_ASKS.md as decided, but the owner should confirm it.

Design log notes:

- **Founder sessions end on the server** (founder-console-09): `POST /api/founder/logout {everywhere?}`. platform.db `account_events` (email = the founder) keeps `founder signed out` {session: sha256 of the token, until}, never the token itself, and `founder sessions from` {from, password mark, why}. The mark is an HMAC of the password hash under the session secret. The first time the server sees a new password hash or session secret, every earlier session ends, once. Signing out everywhere sets from = now + 1. A session that has ended is refused 401 `founder_session_ended`, with a sentence that says why. No migration.
- **The session secret must be at least 32 characters** (security-roles-12): `founder.SESSION_SECRET_CHARS = identity.SECRET_CHARS`. A shorter secret stops the API at import (`founder.check_startup`) and makes every founder route answer 503. `python -m gridlytics.founder session-secret` makes one. If no secret is set, the console is off and the API still starts.
- **A backup is one set** (founder-console-01/-12, parity-architecture-11/-16):
  - Its parts are `<id>-<stamp>-<kind>.db`, `<id>-telemetry-<stamp>-<kind>.db`, `<id>-keys-<stamp>-<kind>/` (files chmod 600) and `<id>-reports-<stamp>-<kind>/`. They are listed, pruned and restored together.
  - List rows are {name, kind, modified (UTC with offset), bytes (workspace + telemetry + reports), workspace_bytes, telemetry, telemetry_bytes, keys, reports, reports_bytes}.
  - A restore puts back the workspace, puts back the telemetry (an empty file when the copy had none), and adds only the keys and reports the folder lacks, keeping newer ones. It makes a restore point first.
  - `scripts/restore_backup.py` (backend repository) lists copies and restores one. It takes the launcher's data-folder lock, exits with status 2 and a sentence on refusal, and prints Pakistan time.
- **The daily copy** takes the default database only when it lies in the data folder in use (performance-live-07).
- **Company role Admin becomes a Gridlytics administrator by default** (founder-console-04): `companies.default_role` and `role_follows`. The owner is pinned; an admin is not. A member starts as a viewer. A role that was set, or carried over as legacy, stays. This changes EMS_BUILD_PLAN 10.5's 'everyone starts as viewer'; it is recorded in BACKEND_ASKS and the owner should confirm it.
- **The founder's role changes take effect at once** (founder-console-16): `store_founder.follow_roles` runs on a membership add and on a company-role change. The membership answers and the users list carry `gridlytics_role`, `role_source` (added in review) and `last_seen`.
- **The only owner who can sign in** (founder-console-10): disabling or archiving such an owner is refused 409 unless `leave_without_owner: true`, and the founder's audit then records `left_without_owner`. Company rows: `owner` is an owner who can sign in when there is one, and carries `disabled`; `admins` counts only administrators whose account is not disabled.
- **Archive, never delete** (founder-console-13):
  - The state is kept as platform.db `account_events` 'founder archive' {kind, id, archived}, so no migration was needed.
  - Archiving a company switches it off (note 'Archived') and makes a `before-archive` copy that is never pruned. Archiving an account disables it and switches off its member rows ('account archived').
  - An archived company cannot be switched on, and an archived account cannot be enabled (409).
  - The overview counts archived companies and accounts only under `archived`.
- **New company-row fields**: `sites_closed_loop` (both capable of and allowed closed-loop control, not deleted; founder-console-05) and `archived`.
- **Removed fields**: `solarlytics_role` (founder-console-18), the commercial `plan` on company rows and `plans` in the overview (founder-console-11). `plans_issued`, `plans_month` and `plans_30` count only the dispatch plans of sites in use.
- **Pending or suspended memberships** keep the codes `not_a_member` and `no_company`, but the sentence now names the membership's state and says the company's owner can ask OnyxLeaf to make it active (parity-supabase-auth-09).
- **supabase/** (parity-supabase-auth-03): `grid_000_preflight.sql`, `grid_001_accounts_and_companies.sql`, `grid_001_rollback.sql` and the owner's README, ported from Solarlytics c853130 with the SQL unchanged. They are for review only and applied by hand by the owner. `test_supabase_sql.py` checks that everything the lifted code calls exists in the SQL.
- **Founder console web app**:
  - Phone cards use Tailwind max-sm utilities defined in `founder-ui.tsx` (`CARDS`, `card()`), so the shared stylesheet is not touched.
  - `PageHead` sets the tab title to '<Page> · Founder console · Gridlytics', and the layout starts with a skip link.
  - The audit Detail says each change as before and after, and keeps server-written names whole (fixed in review).
  - Company role and membership are changed only in a dialog, saved on its button, and the dialog's prediction uses `role_source`.
- **Ownership**:
  - `founder.py`, `routes_founder.py` (except the set-password route), `store_founder.py` (except `set_password`), `backups.py` and `companies.py`'s role and refusal helpers belong to founder-console.
  - Outside its files: one expression in `api._not_seen` and one line in `store.backup`.

## optimiser-plans

Statuses: implemented 31, partial 1

Not fully implemented:

- `brief-vs-build-05` (partial): Confirmed: `battery_temp_c` was ingested but never limited battery power, and battery.state only raised four of the seven alerts. What changed:
- battery.state applies a generic LFP derating curve (`derate_factors`: no c

Remaining after review:

- brief-vs-build-05 stays partial. The 'imbalance' alert still never fires: it needs the BMS's cell voltages (U3's optional battery_cell_v_min and battery_cell_v_max). Ingesting them means new columns in the minute table of every company's telemetry file (an ALTER of a WITHOUT ROWID table, in telemetry.py, which belongs to telemetry-genset-battery), and no source sends them yet. That is an owner decision on extending the telemetry storage. The derating uses a generic, unverified LFP curve, because the BMS's own charge and discharge limits are not ingested either.
- flow-telemetry-plan-control-09: Faisalabad's plan is now cheaper than native, and no demo plan holds 95 % any more. In a 5-day demo built in review, Lahore's plan holds about 46 %, against an AMF failed-start reserve of 36 kWh at a 58 % chance of an outage in 41 h. 'Compare with native' can still show Lahore dearer than native, because delivery-twin's plan_compare gives no value to the energy each arm holds at the horizon's end. That part belongs to delivery-twin.
- engine-optimiser-08 and engine-plans-live-19: the deterministic work limit and the idle re-solve (HANDOFF 8.A.3) and the scheduler's serial solves are left out, as the plan's notes say (stage 2 and control-edge or storage-perf).
- Design caveat: inverter.kw_ac is treated as the one AC path for PV and the battery: PV - K + D - C stays within ±kW AC each step, which is right for a hybrid or DC-coupled inverter. At an AC-coupled site whose inverter asset is the battery's converter only, this over-limits PV. Onboarding's words for the field should say which inverter it means (sites-onboarding).
- plans.past_steps calls active_at for each of the 96 steps (a full plan row is read and parsed each time): about 0.21 s per site on the demo. This is acceptable, but it is a candidate for storage-perf's caching.
- The backend worktree holds an ignored, empty data/logs/gridlytics.log (created 10 Nov 15:08, before this review, by an earlier run). It was not touched; it is not the owner's data folder.

Design log notes:

- **No load shed with the grid up**: on a grid-up step, U <= max(0, L - G_max - min(PV, inverter kW AC)). build_problem raises VoLL to at least the horizon's dearest import price. `unserved.detail.grid` is up, down or none.
- **Per-step MDI rows**: every grid-up step has G[t] + (Lhi - L)[t] <= M (M2 from month_end_step on), as well as the block rows. Every Mode A slot carries the cap as grid_cap_w.
- **Order tie-break**: EPS_ORDER = Rs 0.002/kWh across the horizon, on grid-up steps only: discharge earlier, charge later.
- **Reasons from the plan's figures**: new codes charge_for_reserve, save_for_later, carry_load and cycle_cap, appended to optimise.REASONS and reasons.ts. hold_for_outage is named where a reserve row binds before the next charge or discharge. Wear is named only for a positive gap under c_cyc. New detail keys: within_h, use_t, use_rs_per_kwh, use_reason, gap_rs_per_kwh, wear_rs_per_kwh, cover, seconds, p_fail, margin_kwh, cycles, grid. delivery.step_intents compiles save_for_later as hold.
- **Grid-up SoC floor**: plans.delivered_floor_pct = max(BMS soc_min + 2, reserve_policy.min_soc_pct), the same rule as compile_mode_a and the fallback, held on every grid-up step. An outage keeps soc_min. A run that starts under the floor recovers once (one binary per clock hour, no discharge until back at the floor). The terminal E_use counts only energy above the floor. reserve_pct carries the floor. This supersedes DESIGN_LOG's 'E_use <= E[T] - E_min'.
- **Inverter**: assets.inverter.kw_ac is treated as the one AC path. PV - K + D - C stays within ±kW AC each step, and the reserve's deliverable power is min(Lc - PVlo, P_dis(t), kW AC - min(PVlo, Lc)). This suits a hybrid or DC-coupled inverter; an AC-coupled battery converter is not modelled separately.
- **AMF reserve**: constants AMF_START_DELAY_S = 60, AMF_FAIL_TO_START = 0.05 and AMF_FAILED_START_STEPS = 2. The start delay's row is priced at p(1 - p_fail)VoLL; the failed start's row (2 steps) at p·p_fail·VoLL. There is no reserve row where g[t+1] = 0 (the outage's balance rows price it). A step's reserve_pct is the row's target less its slack. This supersedes DESIGN_LOG's 'AMF reserve priced at the genset's marginal cost'.
- **Reserve policy**: p_threshold decides R_next, using p_next (the chance of an outage starting on the horizon's last day; R_next is capped by P_dis and kW AC), the genset_off reason (with detail.p) and the grid-tied terminal c_next. min_soc_pct acts through the delivered floor.
- **Battery service**: a full charge anchors at min(98 %, soc_max - 1 pt) where soc_max >= 95 % (upper knee). Sigma grows to now, plus 1 point per 10 minutes once a reading is older than STALE_MINUTES = 15. Trust is null with trust_basis bms_only when no minute carries both an independent estimate and the BMS reading. anchor_due is set when sigma > 15 or there is no anchor in 30 days, on a fresh reading. The live tick only asks for one charge to the top at a clock-hour end; the shortfall is priced at the dearest c_imp/eta_c + Rs 1, and the deadline anchor_by is kept in the site's JSON state.
- **Start under the cut-off**: soc_min is recovered once (cut_rec binaries). The terminal slack E_gap is priced at the dearest c_imp/eta_c + Rs 1, so such a start is never infeasible, and terminal_credit_rs can be positive.
- **Derating and alerts**: a generic LFP curve, unverified for any pack (charge 0 / 10 / 45 / 55 °C, discharge -20 / 0 / 50 / 60 °C), applied to a temperature reading no older than 15 minutes and held for the plan's first 8 steps (P_ch_t, P_dis_t). derate fires when the discharge limit is under 50 % for more than 30 minutes. capacity_drop fires when battery_soh_pct (median of the last day against the first day of the 30 read) falls more than 3 points. imbalance is still open: it needs cell voltages.
- **Cycle cap**: cycle_cap_per_day × E_nom per Karachi day binds grid-up discharge only, less battery.state's discharged_today_kwh on the first day. Outage discharge is never capped.
- **MDI and month to date**: M_exp is the second-highest local-day peak of the 7 days before t0. M >= max(M0, floor, M_exp) and M2 >= max(floor, M_exp) (optimise.mdi_floors). An MDI block needs 80 % of its minutes known and takes their mean. The month's imports and exports are the known minutes scaled to the period's minutes so far. New breakdown lines mdi_month_rs and calendar_rs; mdi_rise_rs is measured above the bound, M2 included.
- **Terminal value**: c_cal_hold is the sum of b_cal from the horizon's end to the next peak (off-grid: to the first step PV does not cover). The grid-tied c_next is the genset's cost where p_next >= p_threshold, never behind an AMF panel; terminal.c_next_basis records which.
- **Genset and off-grid**: the follow cap applies on every grid-down step (only a_gen runs uncapped). charge_pv_surplus rows apply without genset charging. discharge_net rows stop simultaneous charge and discharge with the grid down. A chosen run may dump (W, dump_on). A curtailed kW pays the calendar cost of holding its charge to the local day's end plus 2·EPS_ORDER. A dump costs more than a curtailment.
- **Fuel**: plans.fuel_level gives carried (state.fuel_l and fuel_ts plus fills logged, less the litres burnt), assumed_full, unknown (tank_l 0) or piped (PIPED_FUELS = gas_captive, lng_captive). tick writes the level before solving, never moving it back. Plans carry summary.fuel. demo._top_up is demo-seed's file.
- **Time limits**: KEEP_GAP = 0.05 and KEEP_MAX_AGE_S = 2 h. A time-limited solve far from its bound (or with an unknown gap) is held as time_limited_gap, with summary.time_limit_kept, while a recent plan within 5 % on the same observed outage and overrides stays in force. The relaxed retry at a manual-start site is repaired to UT/DT (notes min_run_repair or min_run_repair_failed). Plans carry summary.time_limits_24h.
- **Plan answers and routes**: every step has flags[] (override kinds). GET /api/sites/{id}/plan/past?hours=1..48 is added in routes_plans.py (plans.past_steps, money through out()). tick returns reason and in_force. A failed solve stores a code (FAILURE_CODES: solve_time_limit, solve_infeasible, solve_unbounded, solve_recheck_failed, problem_refused, solve_failed); the solver's text stays in summary.solver.message.
- **Storage**: no store.py migration. The new site-state keys (fuel_l, fuel_ts, anchor_by) live in the existing JSON state.
- **Web**: ReasonChip is a button with an sr-only sentence and a tap-to-open popover. SlotTable has one tab stop with arrow-key moves, a tbody per Karachi day, tinted past rows (no opacity) with sr-only ', past' and ', now', and a Flags column. The past 24 hours are fetched every 60 s on the Table view of the plan in force. The Slots view writes an all-day slot as '24:00 (all day)'. The legend counts 'steps'. format.numTrim is added.

## savings-engine

Statuses: implemented 22, partial 2

Not fully implemented:

- `pages-D-17` (partial): Amendments carried no reason. Once the pilot has started, PUT /pilot now needs a reason (at most 500 characters; 422 'reason_needed' without it), and the reason is stored on the amendment and in its audit line. The page 
- `pr-review-02` (partial): The problem: one recompute re-scored up to 62 later days, across closed months, with no trace, and answered {days: 1}. Now compute_range_detail scores the range and walks forward. _walk_forward replaces _recompute_forwar

Remaining after review:

- engine-savings-06 is partial. The CSV and PDF columns (claimed figure, withheld reason, levers kept) are routes_savings.savings_csv (stage 2) and reports.py (alerts-reports). The API already carries every field.
- pr-review-02 is partial. Marking a stored monthly report out of date belongs in reports.py (alerts-reports); it can compare restated_days[].at with the report's generated_at. Whether to freeze closed months instead of flagging and auditing them is an open owner decision.
- pages-D-17 is partial (the amendments notice on the Savings page), and so is the Savings-page half of pages-D-11 (the ledger title at a monitor-tier site and the switchback panel's empty state). Both are stage 2's features/savings. The API and the exported Amendments component are ready.
- A rule the owner should see: a claiming day's carry-over credit counts as used at v_end when the next day is a chained plan day that is withheld for missing data. That credit counts in the claim, while the withheld day's offsetting debit counts nowhere. Days the plan did not run already keep such a credit provisional. I left this as designed (the golden test of batch 1 encodes it) rather than change claim semantics in review.
- Tuned rows stored before engine-money-live-11 keep their old figures, and show rule_kind null, until the period is recomputed. The fit's cache key changed, so any new score fits again.
- GET /sites/:id/pilot does not go through money.for_reader (out()). No money key is in it today, and the fee multiple is removed by _pilot_for_reader for any reader without see_fee and for API keys. Wrapping it in out() would be a cheap guard; not a defect now.

Design log notes:

- **Claims (engine-savings-02):** a closed-loop or demo day's fidelity is its share of minutes under a plan. A minute counts when no pause or release to native is in force, a plan has been made and has not expired, and, at a closed-loop site's own edge, there is no edge fallback and telemetry is heard. At least 95 % is confirmed, at least 50 % partial, below that not_applied (CONTROL_CONFIRMED, CONTROL_PARTIAL). The row stores `control`.
- **Unread inputs (pr-review-11):** a blank PV or load reading is first closed from the minute's energy balance (`_balance_fill`). A day with more than 5 % of its minutes still unread (load, or PV in daylight; REPLAY_UNREAD_MAX = GRID_UNRESOLVED_MAX) is `claim_withheld: inputs_unread`.
- **Chain (pr-review-03):** the first plan day after days the plan did not run (NOT_PLAN_ARM: recommendation_only, native_block, not_applied, pending) restarts from the measured state (reset `arm_change`).
- **Headline (role-owner-02, engine-savings-06):** `claimed_rs` = claiming days' savings, less the carry-over still provisional, plus their MDI shares. A claiming day's end credit at v_end stays provisional until a later scored day uses it. A chained withheld next day counts as using it. `saving_rs` = every day whose saving is known. A withheld day adds to no total. New fields: `levers_claimed_rs`, `claiming_days`, `withheld_days`, `unclaimed_days`, and per day `claims`, `not_claimed_reason`, `levers_kept`.
- **Hand-over (brief-vs-build-11, engine-savings-08/11):** a native block day also replays native from the plan block's end state (`handover` on the row). Energy handed to days the plan did not run is claimed at what those days realised with it, provisional until the run ends (`_handover`). At a plan-to-native boundary the switchback credit is the realised value. A native-to-plan boundary is credited against the plan days' mean end energy.
- **MDI (flow-savings-pilot-bill-01, pages-D-03, engine-savings-04/05):** the lever is the billing month's, built from every day of the month. A day with no row, or a withheld day, puts its measured peak on both arms. It is priced at the rate, floor and tariff version of the month's last scored day. Each day carries 1/days_in_month, and the lever is provisional until the month closes. Rows store `tariff_version`.
- **Month settle (engine-savings-10):** compute_day(forward) settles the day's billing month once it is over, and the month before it. A fingerprint is kept in the setting `savings_month_settled:<site>:<p0>`. A day is re-scored when its `price_basis` digest, its native capture, late data or its chain changed. Rows store `price_basis`, `month_totals_projected` and `inputs_completeness`.
- **Re-scoring (pr-review-02):** three audit lines: 'recomputed savings days', 're-scored later savings days' and "re-scored a closed month's savings days". A changed claim in a month already closed when the day was last scored is kept in the row's `restated[]`. The summary answers `restated_days`; POST recompute answers `{days, rescored_days, restated_days}`. Flagging was chosen over freezing closed months, which stays an owner decision.
- **Switchback (flow-savings-pilot-bill-03, parity-architecture-23):** a result needs SWITCHBACK_MIN_DAYS_PER_ARM = 6 days of evidence on each arm. A plan day counts only while the plan claimed, and a withheld day counts for neither arm. The result carries the standard error, degrees of freedom and Student-t lower 80 % bound. It is Preliminary until 8 weeks of blocks painted before they began have run. The agreement check is 15 % or Rs 300 a day, whichever is larger. `result_withheld` is too_few_days, monitor_tier or no_calendar.
- **Tuned baseline (engine-savings-03, engine-money-live-11):** candidates are the native capture, a self-use table at the capture's backup floor, then the 8 TOU tables. A later candidate wins only if strictly cheaper. At genset sites with a battery, HOMER cycle charging is also tried at 60, 80 and 95 % SoC (twin policy `cycle_soc`). The fit values end energy at v_end. The cache key gains the suffix `:cc:nc`. The summary adds `cycle_charge_soc_pct`, `rule_kind` and `rule_fit`; `rule_fit`'s costs are money.
- **Perfect-foresight bound (engine-optimiser-14):** the day's saving plus max(0, headroom). The headroom covers energy, genset, wear, unserved load at VoLL, and the MDI rise above max(M0, floor), less end energy at v_end. It uses the ledger's prices and starts from the actual arm's state. A solve that is time-limited (BOUND_TIME_LIMIT_S = 5) or not optimal gives no bound. The period bound covers claiming days that have a bound, on the claim's own terms. `earlier_method_days` lists rows from before chaining (pr-review-04).
- **Pilot record:** PUT /pilot takes the strict schemas.PilotEdit; the sign-off and the amendments are the server's. Once started, a change needs a reason (at most 500 characters) and records each field's before and after; a save that changes nothing is a 422 `no_change`. Audit lines: 'changed the pilot set-up' and 'amended the pilot'. `criteria.fee_multiple` needs see_fee (PILOT_FEE_FIELDS). Closures: at most 20, each at most 60 days, label at most 80 characters.
- **Start (flow-savings-pilot-bill-09/11/12, pages-D-11):** the pilot starts once (409 `already_started`) with a strict {name}. Gates, in order: tier, capture, baseline, calendar, rules (422 monitor_tier, no_capture, no_baseline, no_calendar, calendar_rules), checked inside the start's write transaction. The start pins the capture (`capture_pins` in the pilot data), and verify.native_for_day replays the pins. A later capture is adopted only by an amendment {capture_version, reason}.
- **Calendar:** BLOCK_DAYS_BY_TIER is monitor none, advisory_writes 7, closed_loop 3 or 7; BLOCK_DAYS_OPTIONS is 3 and 7. PUT /switchback takes the strict SwitchbackPaint and needs a start after today, Pakistan time. Arms come from `secrets`. The paint record is in the setting `switchback:<site>`, written in the same transaction as the blocks and audited with the arms. Only `painted_before_start` blocks count towards the 8 weeks. A locked legacy calendar counts blocks that began after the start's day. Rule checks are the server's alone (verify.calendar_checks); the web only words them, and a guard test keeps rule numbers out of the web app.
- **Tab A:** BASELINE_DAY_COMPLETE = 0.95. `baseline_quality` and `kpis` are part of GET /pilot.
- **.ics:** RFC 5545 TEXT escaping, DTSTAMP in UTC, lines folded at 75 octets, Content-Disposition with a file name.
- **Wording (review):** a refusal or a form problem states the rule it applies ('must ...'), never a statement the request has just broken.
- **Storage:** no store.py migration; new settings keys `switchback:<site>`, `savings_month_settled:<site>:<p0>` and the tuned cache key suffix.
- **Ownership:** minimal edits outside the package: twin.py `cycle_soc` (delivery-twin), demo.py `painted_on` (demo-seed), features/savings and the routes_savings recompute route (stage 2).

## savings-page

Statuses: implemented 25

Remaining after review:

- Overview's native-block sentence sits under the 'Saving this month' tile, which a viewer sees as hidden, so a viewer reads it on Fleet rows only.
- Owner decision, kept as the follow-up asked: the server refuses only the monthly savings report for a site with under 7 days of data. Energy-only and pilot reports are still made, although the web dialog hides them.
- Role lists still written into page text outside this package's new code: the Savings actions line 'Exporting is for... Recomputing is for...', the ledger's hidden note, criteria-tab's fee multiple, telemetry, forecasts, and the generate dialog. They could read useWhoCan; this is sweeps' cross-page work.
- GET /savings/kpis runs verify.summary again on every Savings page load, alongside GET /savings. It works, but storage-perf may want to reuse the result.
- genset_starts and the new savings-day fields (tariff_version, native_capture_version, peak_kw_native, claimed_in_total_native_rs) exist only on days scored after this change. Older rows give nulls until they are recomputed; nothing is backfilled.

Design log notes:

- **Pilot go/no-go (gonogo.scorecard, GET /sites/:id/pilot/scorecard)**: each pre-registered criterion is read from the pilot's start (its Karachi day) to today as met, not_met, not_yet or not_applicable. Plan 15.3's gates are added. PILOT_DEFAULT and PilotCriteriaEdit gain `genset_hours_cut_pct` 15 (run hours against the tuned rule, on claiming days) and `cycles_ratio_max` 1.1 (EFC a day against the tuned baseline's). Older set-ups read these defaults. The other added gates: counterfactual and switchback agreement, writes off the whitelist (closed loop only), and the statistic (lower 80 % bound of the verified tuned increment x 30, against the fee). Verdict: not_yet until 8 switchback weeks have run and the statistic is measured (with a `so_far` reading). Then go at 3 times the fee or more with no gate not_met, review at 3 times or more with a gate not_met, extend once between 1.5 and 3 times, stop under 1.5 times. Reader rules (gonogo.for_reader): without see_costs a money gate is a `{hidden}` stub. Without see_fee the fee gates, the verdict and the `fee_multiple` criterion are removed. API keys are refused. The monthly pilot report prints the same gates through gonogo.
- **Safety and control-health KPIs (gonogo.measures, GET /sites/:id/savings/kpis, no money, every role)**: reserve adequacy (reserve.adequacy) and unserved energy the plan caused beyond the native replay on claiming days. Minute checks: import during an outage, export above the cap, genset under its minimum load. The minute checks read at most VIOLATION_DAYS_MAX = 120 days, with a margin of LIMIT_MARGIN_KW 0.5 or 2 % of genset rating. A row's energy counts over its own interval (coarse sources, review fix). Writes per register a day come from the edge's acks; read-back, write, off-whitelist (a whole plan refused counts) and fallback events; BMS trust over 30 days. The ledger stores `genset_starts` per arm (verify._gen_starts: a run already going at midnight is not a start). There is no backfill.
- **Audit (security-roles-04)**: every mutating route writes an audit line. `store.changes(before, after)` records `{field: {before, after}}` under the field's own name, so the money filter hides money values (fee, VoLL, battery and genset costs, fuel price) from readers without the right. test_audit_walk.py places every mutating route in WALKED, ELSEWHERE or EXEMPT; a new route fails the walk until it is placed. set_tier_capable takes `by` (the edge writes `by='edge'`).
- **Savings exports (savings_export.py)**: both CSVs keep their old columns and append `claim_withheld` plus CLAIM_COLUMNS. `claimed_native_rs` adds up to `baselines.native.claimed_rs`, and the saving plus `mdi_share_native_rs` add up to `saving_rs`. `plan_ids` lists the plans in force over each Karachi day: the active or superseded plan at midnight plus every plan made during the day. verify.summary day rows gain claimed_in_total_native_rs, peak_kw_native, tariff_version and native_capture_version.
- **Recompute as a job**: POST /savings/recompute runs on the jobs.py pool in its own thread at nice RECOMPUTE_NICE 10, one job per site. The same person asking for the same period joins the running job; anyone else gets a 409 sentence. The answer is the outcome within RECOMPUTE_WAIT_S 10 s, otherwise 202 with {status, job_id, done, total}. New GET /sites/:id/savings/recompute/:job_id. compute_range_detail takes `progress`. jobs.py is storage-perf's (start gains cache and abandon, get takes owner=None, plus find and FAILED).
- **GET /api/meta `who_can`**: every permission's roles in the 403 refusals' words (auth.roles_words). Pages read role names here (useWhoCan) and never write a list of roles; an older backend's meta names none.
- **Reports**: listing and get add `out_of_date` and `restated_days` for savings kinds (monthly savings, company roll-up, pilot). A report is out of date when a day of its period has `restated[].at` later than its generated_at. The download name ends in '-out-of-date' rather than a header, because the CORS expose list belongs to auth-security. REPORT_DAYS_NEEDED = 7: check_period refuses a monthly savings report for a site whose first reading is under 7 days old (422, one sentence). Other kinds are still made, pending the owner's decision.
- **Savings answer additions**: each day's `outage_hours` (from its telemetry day, null without one); `native_captures` (each capture the days were replayed on, with settings, by, source, has_photo and the days it priced) and `native_latest_version`; per baseline `verified` {days, claimed_rs, levers_claimed_rs} (a viewer gets days only). The fee check gains baseline, months, scaled and preliminary; `within_one_third` is null with no scored day. The fee notice says 'verified' only when the check is not preliminary.
- **GET /sites/:id/savings/periods (every role, no money)**: the billing period today falls in and the last one, from the first grid connection's reading_day via tariffs.billing_period_for (the calendar month when no reading day is saved, null off-grid). The pilot window runs from the sign-off's Karachi day to the switchback calendar's last day, today at the latest.
- **Fleet and Overview**: `mtd_native_block_days` on fleet rows and `month.native_block_days` on GET /now count this month's native-block days, a figure every role may read. Wording rules: at a monitor-tier site the ledger is titled an estimate and the Switchback tab does not invite a calendar. A day that claims nothing is drawn faded with a dashed outline. Daily-chart ticks are round rupee values (1, 2, 2.5 or 5 times 10^n) that always include zero.
- **Ownership of shared functions**: reports._pilot and _energy_and_days (alerts-reports' file) call gonogo and savings_export. verify.summary (savings-engine's) gains additive fields only. routes_sites.meta, fleet_row, _mtd_saving and site_now (storage-perf's in stage 2) gain additive lines only. No store migration.

## shell-nav

Statuses: implemented 10, already_fixed 3, implemented (was implemented with a part left) 1, implemented (header half) 1

Not fully implemented:

- `pages-A-12` (implemented (was implemented with a part left)): Before this change the Site group's switcher was a Radix Select that listed bare names, and it was still a dropdown when the company had one site. The breadcrumb stopped at the page. site-switcher.tsx is now a popover co
- `pages-A-13` (already_fixed): Fixed in an earlier commit on this branch, which I checked and kept. companyLinkActive() in nav.ts marks Members on ?view=members only and Settings on every other tab. This batch adds a `listed` parameter, so Settings is
- `pages-C-18` (already_fixed): Fixed in an earlier commit on this branch, which I checked and kept. Outages has needs: 'grid', and siteOffGrid() decides from the first connection's regime, or, when there is no connection, from data.off_grid or a farm 
- `role-owner-34` (already_fixed): Fixed in an earlier commit on this branch, which I checked and kept. Pilot set-up is now in the Site group, not under Company. The control plane is called 'Control', clearly apart from 'Devices and data', in the menu, th
- `role-viewer-09` (implemented (header half)): A duplicate of pages-A-03: the header no longer says Off-grid for a site whose connection is missing (e19386c). The Outages page's wording is forecasts-outages'.

Remaining after review:

- frontend-a11y-22: sonner shows at most 3 toasts at once, so a kept error can sit behind newer toasts until they go (it shows on hover). mark-applied.tsx (control-edge's) still leaves a rejected file's name in its input. Neither part was asked by the finding, which wanted error toasts to persist.
- usePageFocus focuses <main> when a lazy page's chunk is not loaded yet, and focus does not move on to the heading once it appears. The tab title still announces the page, and the plan accepts this ('or to <main> while the page has none').
- role-operator-24 (a duplicate of pages-A-13): its 'The API keys is not available for your role' wording and the 403 calls from the Members, API keys and Audit tabs are fixed on auth-security's branch (57bb800), not here. They need that branch at integration.
- pages-D-26 (a duplicate of pages-A-05): the menu no longer lists Pilot set-up for a viewer, but GET /api/sites/{id}/pilot still has no permission check. Adding one is savings-engine's job (pilot_get).
- frontend/README.md line 32 still says 'env (zod)'. The README belongs to the docs package (stage 3): it should say env.ts and build-settings.ts parse the settings with plain code, and that zod is used only by the site forms.
- A supabase build is accepted by env.ts, but auth.store.ts still uses localAuth whatever the mode, so it would post to /api/local/session. That is auth-security's to finish.

Design log notes:

- **Menu by role (web, nav.ts `shownTo`/`useMenuAccess`):** a viewer is offered only the energy pages: Fleet, Alerts and Reports, and a site's Overview, Plan, Telemetry, Forecasts, Outages, Battery, Genset and Savings. A viewer gets no Company group and no "Company settings" in the account menu. Plan stays listed because GET /plan answers a viewer with the money stripped (R8 2.9). Tariffs needs `see_costs`; Members needs `manage_users`. Until /api/meta answers, only a viewer's pages are listed, so no link appears and then vanishes. A null role (the open workspace) gets every page.
- **Off-grid is the site's own fact (web):** it is a connection of regime `off_grid`, or no connection at a farm or a site whose `data.off_grid` is true (`siteOffGrid`, the server's `_grid_settled`). It hides Outages in the menu and words the site header. A grid site whose connection is not saved yet reads "Grid connection not saved" and keeps Outages. fleet-overview's `isOffGrid` is the same rule; keep one at integration.
- **Site menu:** Pilot set-up is in the Site group. The control plane is named "Control" in the menu, tab title and breadcrumb; links to it read "Control page"; it is distinct from "Devices and data".
- **Company pages are no site's:** Fleet, Alerts, Reports, Tariffs, settings, account and Add a site show no tier chip and no site short menu. The phone menu there is Fleet, Alerts and Reports. `/sites/new?site=<id>` makes the new site the one in view. A site that answers 404 is no site: no live links to it, and it is not remembered.
- **Titles and focus:** each route names its page in `handle.title`. A site page's tab reads "Page · Site · Gridlytics", an unknown site "No such site", an unknown address "Page not found". After a move to another page (not on first load, not on a ?view= change), focus goes to the page's h1, or to <main> while the page has none. "Skip to main content" is the first Tab stop. Every lazy route has a HydrateFallback.
- **Breadcrumb:** "Company > Site > Page > sub-view". The company is the active membership's name, linking to /fleet ("Fleet" while no company is known). The sub-view is named by the page through `useSubViewCrumb` (stores/crumb.store.ts): plain `?view=` tabs, the plan's view ("Slots" at the advisory tier when none is named), the savings method, or a lettered tab ("C. Switchback calendar").
- **Shell states:** loading and failure are said as such, never as empty. The switcher says "Loading the sites…" or "could not be read" with Try again; the last-data stamp says "loading…" or "could not be read"; the bell names no count while loading and shows "?" on failure. "No sites yet" and "none yet" are kept for real empty answers.
- **Site switcher:** a searchable combobox (name, city, DISCO). Each site has a state dot (the last-data thresholds: 5 and 30 min) and "Last data N min ago". Choosing a site keeps the page. With one site it shows just the name, with no dropdown.
- **Toasts:** `toast.error` and `toast.warning` stay until closed. This is set once in app/providers.tsx (keepProblemToasts); a call that names its own `duration` keeps it. Success and info toasts still go after 4 s.
- **Tap targets:** 44 px on a phone (breadcrumb links, top-bar buttons, short-menu items). Cut-off tab rows scroll inside a ScrollStrip that shows a fade and an arrow at each edge that hides more. The phone menu's current item is marked by a bar and bold text, not by colour alone.
- **Build settings (app/build-settings.ts, env.ts):** each VITE_ setting is checked on its own; one that cannot be used keeps its default and is reported once with console.error.
  - VITE_AUTH_MODE is exactly `local` (also when empty) or `supabase`.
  - VITE_API_BASE_URL is https (http only on 127.0.0.1, localhost or ::1) with no query, fragment or password, or a path on the page's own host.
  - supabase mode needs VITE_SUPABASE_URL (the project root, https) and VITE_SUPABASE_ANON_KEY.
  - A secret key (sb_secret_, or a JWT with role service_role) is refused in either mode and never repeated in the message.
  - `vite build` stops on any problem; the dev server and vitest only warn.
  - env.ts exports API_BASE, apiUrl, AUTH_MODE, SUPABASE_URL, SUPABASE_ANON_KEY and BUILD_PROBLEMS, the same names as Solarlytics' config/runtime.ts.
  - zod is not in the code loaded before the first page; eager-bundle.test.ts guards this.
- **No API, schema or migration changes** in this package. The only shared files touched are docs/API_CONTRACT.md (appended under "Round 7 (audit): shell-nav") and the root .env.example.

## sites-onboarding

Statuses: implemented 21, partial 1

Not fully implemented:

- `flow-onboarding-22` (partial): Several parts of plan 9.3's tab content were missing. Tab A had numeric coordinates and a self-declared grid-presence tick only. Tab D had mode, reserve, cycle cap and critical load only. grid_charge_allowed, gen_charge_

Remaining after review:

- flow-onboarding-22 stays partial, as the implementer reported. Two parts are left, and both are outside this package's code. (1) An embedded, draggable map pin needs the owner to choose a map provider (tiles, a library, and their network and privacy terms); for now there is an OpenStreetMap link. (2) A live probe of an OEM cloud or an edge before its data flows needs OEM-cloud connectors or edge enrolment, which do not exist yet; the test reads the data already received.
- An off-grid site still has to tick 'This source reports grid presence' in tab A, and activation asks for grid_signal_confirmed even for off-grid sites. That tick was required in the web app before round 7; flow-onboarding-08 now enforces it on the server too, and test_fix_r3_api's off-grid test was changed to add a confirmed source. Its data is rightly not required to carry grid presence. Suggested follow-up: ask only for the source kind when the site is off-grid, in both the server and tab A.
- Review's Activate runs its own saveAll first. No test fails without it, because the Review lock and tab D's 'Save and review' already cover it (both are tested).
- Two sentences describe behaviour of other packages: the inverter-cap sentence in setup_warnings (optimiser-plans 551a36b) and the feeder-pool wording (forecasts-outages ecde23c and ec0525e). They hold only once those packages are merged.
- test_fix_r4_savings off-grid carry-over is load-sensitive: it failed once under -n 4 and passed alone and in the next full run. It belongs to another package; it is not caused by this branch.

Design log notes:

- **New permission `manage_sites` (administrator only)**, appended last in auth.PERMISSIONS. It is needed for:
  - POST /sites, POST /sites/:id/activate, PUT and DELETE /drafts/:id;
  - a PUT /sites/:id that changes a control limit (`SITE_LIMIT_FIELDS`: control_mode, reserve_policy, cycle_cap_per_day, critical_load_kw, grid_charge_allowed, gen_charge_allowed).

  A limit is compared as SiteFields would store it, so a limit sent back unchanged is no change, while a partial reserve_policy is one. The other site fields, equipment, connections, the native capture and overrides stay on edit_sites. The battery's SoC floor and ceiling are equipment and stay edit_sites.
- **Optimistic saves:** PUT /sites/:id, PUT /sites/:id/connection (with id) and PUT /sites/:id/assets/:role take an optional `if_unchanged`. When it no longer matches what is stored, the answer is 409 `site_changed` with `changed`, nothing is written, and the check and the write share one write transaction. The wizard keeps `form.base` in its draft.
- **Activation (422 incomplete) asks for:**
  - a data-source kind with grid_signal_confirmed;
  - for a grid-connected site whose stored data has arrived, grid presence in that data (`sites.source_test` reads the last day: SOURCE_TEST_S = 86400 s);
  - every connection's tariff category known to CARDS_279;
  - each connection's DISCO group matching the site's DISCO.

  Off-grid sites need no grid presence in their data, but still need the tick (open question).
- **tier_capable follows the data source (sites.SOURCE_CAPABLE):**
  - csv and oem_cloud reach advisory_writes;
  - a CSV site above monitor needs one stored minute with inverter_mode_active (otherwise 422 no_inverter_mode);
  - a save that keeps the kind never lowers it;
  - switching to edge takes the edge's last capabilities message, else monitor.
- **ConnectionFields rules:**
  - the tariff category is required and must be known;
  - agreement dates are YYYY-MM-DD, with the end not before the start;
  - the ICP baseline is keyed by calendar month 1 to 12, 0 to 100,000,000 kWh;
  - Tariff F's season is at least six distinct months, on industrial (B-) categories only;
  - strn false means gst_recoverable false, and an explicit true is refused;
  - disco_group and province default from the site's DISCO (DISCO_PROVINCES; K-Electric is 'ke' and Sindh), and a group that contradicts the DISCO is refused.
- **GensetFields rules:**
  - upper bounds (GENSET_UPPER): Rs 1,000,000 a start or a running hour, Rs 10,000 a kWh of upkeep, a 1,000,000 L tank;
  - each fuel's curve (FUEL_CURVES): diesel 0.273 / 0.033 L; captive gas and RLNG 0.0085 / 0.0010 MMBtu with tank 0;
  - unit checks: HSD_A_MIN 0.1 L/kWh, GAS_A_MAX 0.05, GAS_B_MAX 0.01 MMBtu;
  - `rated_kva` (null for a kW-only rating) and `power_factor` (0.5 to 1, 0.8 by default); rated_kw = kVA × PF, tolerance max(0.05 kW, 0.1 %).
- **SiteFields.genset_start_soc_pct** (above 0 to 100, or null). The shared function `sites.genset_start_soc` (owned by sites.py) gives the site's threshold, else the reserve floor plus 5 points. plans.build_problem and twin.plant_of both read it.
- **Native capture rules:**
  - each slot starts at a time from 00:00 to 23:59, and no two start together;
  - the model allows at most 14 slots (NATIVE_MAX_SLOTS), and the route allows at most the site's brand `max_slots`;
  - POST /native refuses source read_back, since only the edge reads settings back.
- **`setup_warnings` in every site answer:** these sentences are warnings and never refuse a save. They cover:
  - PV AC above the inverter's kW AC, or above 1.5 × DC (PV_AC_DC_MAX);
  - battery charge or discharge power above the inverter's kW AC;
  - an export cap above the inverter or the sanctioned load;
  - a critical load above what the battery can give (through the inverter), or above the peak load;
  - no feeder code at a grid site;
  - Mode B with a CSV or cloud source;
  - a connection's DISCO group that contradicts a DISCO changed later.

  They contain no money.
- **New route POST /api/sites/:id/source/test** (edit_sites, read-only). It answers kind, grid_signal (found, missing or no_data), rows, first_at and last_at (ISO UTC; the web app shows them in Karachi time), points and a sentence.
- **GET /meta adds** `icp_categories` and `fallback {on_expiry: inverter_native, revert_after_hours: FALLBACK_HOURS}`.
- **No store migration:** every new field lives in the existing JSON columns.
- **Web app:**
  - Review opens only once tabs A to D are complete; tab D's 'Save and review' and Activate save every open tab first;
  - the genset's start method is asked in tab B with the genset, with no default;
  - tab C defaults follow the site's DISCO and kind and are marked Preliminary; the sanctioned load has no default;
  - a new site has no DISCO and no Lahore pin (the pin comes from the city typed);
  - any site not connected to a DISCO may be off-grid;
  - the value of lost load is hidden without see_costs and never sent back then;
  - the CSV text names the inverter_mode_active column on purpose, because it is the file header the person must add.

## storage-perf

Statuses: implemented 17, already_fixed 1

Not fully implemented:

- `performance-live-12` (already_fixed): The Fleet page no longer loads Recharts: since stage 1 (commit 43fcdf3, fleet-overview), spark-bars.tsx draws plain SVG and has no Recharts import. I checked this on the production build: the static chunk graph of fleet-

Remaining after review:

- jobs.py runs every kind of job (re-solves, reports and savings-page's recomputes) on one 2-worker pool in the process. A control action's re-solve does not check the 30 s re-solve limit. So several quick actions on one site queue up, and the second holds a worker while it waits for the site's lock. At v1 scale this only delays other jobs past the 10 s or 20 s wait (they then answer 202). If it matters, a pool for re-solves alone is a later step.
- GET /fleet still runs its per-site queries one by one (now on one connection). Batching fleet_row's reads across sites is a later step. Only GET /fleet uses store.shared().
- tests-and-perf-22 (a duplicate of performance-live-10) also suggested trimming /overview/chart to the fields the chart draws. Not done: that is fleet-overview's answer shape, and with gzip the chart's minute poll is about 4 KB.
- The battery estimate is reused for 60 s, so reading_age_min and stale can lag by up to a minute. The month-to-date saving is reused for 5 minutes and does not follow new telemetry for the MDI lever within that time. Both limits are by design.

Design log notes:

- **Plan retention (spec Part 3.3)**: `plans.prune`, run nightly at the end of `scheduler.nightly_company`. A plan row and its summary are kept forever. Past `KEEP_WHOLE_DAYS` = 90, a plan keeps its steps and table whole only when it is: the active plan; the first plan of its hour; applied, issued as a day's table or sent to an edge; or in force at a Pakistan-time midnight. Any other plan keeps only the steps of its own span in force plus the step after it (what `reserve.target_at` reads), and its table is dropped. Trims are done 200 rows a transaction, and each prune starts where the last one stopped.
- **Migration 6 (renumber at integration; sweeps also took 6)**: indexes `plans_belief(site_id, belief_time)`, `plan_messages_site(site_id, sent_at)` and `acks_site(site_id, received_at)`; a new table `ticks` (one row per scheduled tick: start, end, seconds, sites, failed, workers, slowest site, overran); new columns `runs.error` (server-only text, at most 500 chars) and `runs.seconds`.
- **The `runs` table is the scheduler's alone**: only scheduled ticks write it (`scheduler._run_row`). A manual re-solve no longer overwrites it. A tick that raised writes status `error` with reason `no_tariff_version` or `tick_error` (`scheduler.error_code`); a solve that made no plan writes `failed`.
- **Failing ticks are visible**: `plans.tick_failing` gives GET /plan the field `failing {status, reason, since, at, ticks}` (no money). For a raised tick, `plan_failed` is raised at once without waiting for `settled`. The founder console shows `sites_failing`. `GRIDLYTICS_ERROR_WEBHOOK` (https, or http only on localhost) receives a JSON record for each failed tick: event, company, site, tick, reason and error type, never the error text.
- **Tick capacity**: a company's sites are ticked on a thread pool of `GRIDLYTICS_TICK_WORKERS` workers (1 to 8), by default the number of cores less one, capped at 1 to 4. A tick that ends after the next quarter plus 20 s raises `tick_overrun` (Control, warning, 'Planning run late') on every active site; the next tick that ends in time clears it. A tick reads its site's minutes once (`telemetry.window`, 32 days plus 1 h), as one float block.
- **Words**: in user copy a scheduler tick is a 'planning run'. The codes `no_tariff_version` and `tick_error` are always shown in words (`TICK_ERROR_WORDS` in the web app's `features/plan/solve-job.ts`).
- **jobs.py is the shared job pool**: re-solves, reports and (savings-page) recomputes, with 2 workers per process. New options: `cache=False` (no result cache), `abandon=False` (runs to its end unread), `find` (a company's job by request key), `get(owner=None)` (company-wide jobs), and a generic `FAILED` sentence.
- **Re-solve as a job**: POST /plan/solve and every control action's re-solve wait `SOLVE_WAIT_S` = 10 s, else answer 202 `{status: queued|running, job_id}`. GET /sites/:id/jobs/:job_id returns the caller's own job (404 otherwise). One re-solve per site runs at a time. A raised solve answers `failed` with a reason code instead of a 500.
- **Reports as jobs (Part 10.6)**: the request waits `REPORT_WAIT_S` = 20 s, else answers 202. GET /reports/jobs/:job_id belongs to the company, and is a 404 for a kind the caller may not read. The request-hash cache is keyed on the request plus its basis (the newest audited change, savings day, plan, table issue, mark, override, bill and minute, and the Pakistan day). A refusal in words is a 422; anything else is a 500 `report_failed`.
- **Plan compare stays open to every member, viewer included (without rupees)**, as R8 2.9 draws it. Its replay is cached (64 entries; keyed by plan, capture, plant and price) and made once even when several ask at the same time.
- **Light reads and reuse limits**: GET /sites/:id/header (no money) is the header's 30 s poll. GET /sites gains `last_data`, and the shell polls it instead of /fleet. Reuse limits: the battery estimate 60 s while the newest reading and the battery are unchanged; the month's litres 60 s; the month-to-date saving 300 s, keyed on the newest `savings_days.computed` and the newest audit id. Today's day row is aggregated again whenever a newer minute arrives, with its peak flags, and day rows carry `last_ts`.
- **Plan reads**: `plans.InForce` reads a range's plans once; `active_at` is InForce for a single instant. `history` and the fleet's current plan read without the steps and table blobs. Native capture readers select `NATIVE_COLUMNS` and get `has_photo` from the photo's length, never the photo itself. `write_log` reads by date through the index, a day either side.
- **store connections**: a workspace file already seen in WAL mode at the current schema is opened without PRAGMAs; a restore resets this (`forget_ready`). `store.shared()` gives one connection per request and is used only by GET /fleet.
- **Compression**: GZipMiddleware is the outermost layer, after the money filter. It packs answers of 1000 bytes or more at level 6, and leaves PDFs and images alone. `_WholeBody` joins any answer with a known length up to 4 MiB into one message.
- **Web bundles**: ECharts registers only the line and bar series plus the grid, tooltip, axis pointer, legend, mark line and mark area components (570 kB). The chunk-size warning is set at 600 kB. GATES.md records the chunk sizes.
- **Fuel prices**: a newer build's shipped prices are added to an existing company's file for the days it has no price for, never over a price the company entered; this is audited.
- **routes_plans._held reads plans.held_info only**: the pre-held_info fallback is removed.

## sweeps

Statuses: implemented 23, already_fixed 2, partial 1

Not fully implemented:

- `followup:a11y-21-telemetry-outages-status` (already_fixed): Already in r7-stage1 from the fleet-overview review (341bc84). Telemetry has a role=status line that stays on the page and says what the range now shows (its resolution, or that the range has no reading). Outages says ho
- `followup:telemetry-strip-iso-day` (already_fixed): Already done on this branch in batch 6. The data-quality strip's screen-reader text and its tooltip write the day in words ("10 Nov 2026: 100 % so far (today)"), and the shared completeness test reads it that way. The De
- `role-owner-37` (partial): The unconfirmed slot table alert now says why in plain words: nothing reads the inverter's settings back at the advisory tier, so the day's saving waits until the table is marked applied on the Control page. Before, it s

Remaining after review:

- Two other CSVs still label a gas genset's MMBtu as litres with no unit: the monthly report CSV (litres column) and the Savings CSV (routes_savings.savings_csv). Their headers belong to savings-page, which rewrote them in 2a99d83, so they were left alone to avoid a conflict. Add a fuel_unit column at integration or in savings-page, as the roll-up now has.
- Schema migration clash: this branch's store.py uses SCHEMA_VERSION 6 with _V6_COLUMNS (reports.engine), and storage-perf uses SCHEMA_VERSION 6 with _V6 and _V6_COLUMNS (runs.error, runs.seconds). One must become version 7 with its own constant names at integration. Back up before the schema change.
- Two batch-7 behaviours worth knowing (design choices, not defects): the inbox and its badge now count only the kinds of alert a person follows (one who follows none sees 0, even with critical alerts open; the inbox has "Show every kind"), and Telemetry's plan overlay is now off until the reader ticks it.

Design log notes:

- **Fuel unit per genset** (frontend-states-formats-01): a captive-gas or RLNG genset's `litres` figures are MMBtu (its curve and price are per MMBtu). `GET /fleet` rows add `fuel_kind`. The summary's `mtd_litres_saved` sums diesel sites only and `mtd_mmbtu_saved` sums gas sites; the two are never added together. Reports name the fuel ("Gas saved", MMBtu). The company roll-up CSV gives `fuel_unit` after `litres_saved` (review). Web: `lib/site-facts.ts` (`siteFacts`, `useSiteFacts`) and `lib/format.ts` (`fuelName`, `fuelAmount`) are the one place pages read a site's fuel and off-grid status.
- **Off-grid rule** (role-operator-10): one rule everywhere: `routes_sites._off_grid` = `forecast.off_grid` = `reports._on_grid` = web `isOffGrid`. An off-grid site has no outage, grid import, demand or MDI. The Savings answer gives `energy.outage_hours` and `energy.peak_kw` as null. Energy and monthly reports drop the grid rows and the Outages section (the CSV keeps the columns, blank). The web hides grid tiles, columns and outage shading, and the Fleet reads "No grid". `POST /activate` asks an off-grid site for a data source only, with no grid-presence signal.
- **Long-period fuel** (frontend-states-formats-02): over more than 62 days, `verify.litres_used(simulate=False)` runs no minute replay. If the genset ran with neither its power nor its fuel read, `energy.litres` is null and `litres_source` is `"not_counted"`, never 0.
- **Inbox follows notification choices** (rest of role-analyst-32): `GET /alerts` and `GET /alerts/counts` count only the kinds the person follows (`notify:<user_id>` `follow`; every kind until they choose). Counts add `following`. `?every=true` lists every kind the role receives. Fleet counts, a site's own pages and API keys read every kind.
- **Removing a site** (flow-reports-alerts-members-keys-14): `sites.delete` revokes the site's keys (`apikeys.revoke_site`, which goes through `revoke` so the platform index drops them) and resolves its open and acknowledged alerts (`alerts.site_removed`, note "The site was removed"). It audits "removed a site" with the site's name as target and `keys_revoked`. Reports stay listed and downloadable. `sites.names()` includes removed sites and gives `site_name` and `site_removed` to reports, keys and alerts.
- **Engine stamp on stored results** (parity-architecture-12): `version.stamp()` is recorded when a result is computed:
  - a plan's `summary.engine`, solved or failed;
  - each savings-day row's `engine` (`days[].engine`, null before);
  - a bill's `rebuilt.engine`, set again when a carried credit settles it;
  - a report's `engine` column (schema version 6, to renumber at integration).

  Every PDF page has a footer naming the engine, wrapped to the page width inside the bottom margin. Savings reports name the engines of their days (three, then "N other engines"). Report CSVs end every row with `report_engine`, and savings reports add `day_engine`. version.py's docstring now describes Gridlytics.
- **Audit wording**: `reports.made_words` gives each report kind its own article ("generated an energy-only report"). The dead apikeys `need` and `SCOPE_ACTIONS` (Solarlytics quote scopes) are gone; a key carries scopes, not role permissions.
- **CSV date and time in two columns**: new mapping key `ts_time` (`MAPPING_KEYS = CSV_COLUMNS + ts_time`). The import joins it to the ts column's date, in the mapping's date order. The guess pairs a column of dates with an unread column of times, in either order. A `ts_time` without a ts column is refused in one sentence.
- **Alert wording**: hot_high_soc names its 24 hours in Karachi time. apply_unconfirmed explains the advisory tier's missing read-back in plain words.
- **Web rules**:
  - A send button is never `disabled` for a form problem. `SendButton` stays focusable with `aria-disabled`, is described by the reason, and focuses the first invalid field; only `busy` disables it. `ProblemText` carries problems that belong to no single field, and `PhotoField` says a refused photo under its field.
  - An explanation never lives only in a `title`: a `Badge`'s title is in its screen-reader text and a focusable tooltip, and `Tip` opens on tap.
  - A table's key column is `th scope="row"`, and each row action names its row.
  - `Field` writes a date input's day in words (`dayInWords`).
  - Mutations refresh what they change: the fleet (exact key), `afterAlertAction`, the org's seats, and the cached outage cause.
  - `usePermissionGate` stops pages from calling routes the role is refused (tariffs, bills, prices without see_costs).
  - Telemetry's plan overlay is opt-in (`planAhead`, at most 48 h), and battery power is drawn as bars.
  - The inverter AC size means the shared hybrid inverter; an AC-coupled battery converter is not modelled.

## tariffs

Statuses: implemented 18, partial 2

Not fully implemented:

- `engine-tariffs-10` (partial): The bill now applies the notified ICP rule (SRO 2409(I)/2025, R9 5) to a legacy net-metering site's days. The baseline is compared with imported units. A period is eligible only if it ends the month as a net import. The 
- `pages-D-10` (partial): The backend now compares the typed units_peak, units_off_peak and mdi_kw with the rebuilt peak and off-peak imports (before netting) and the rebuilt MDI. Each bill row carries reconciliation[] (printed, rebuilt, differen

Remaining after review:

- engine-tariffs-10 stays partial. The bill now applies the notified ICP rule (SRO 2409(I)/2025). The optimiser's legacy-ICP rows (optimise.py 476-494, optimiser-plans' file) and the ICP case in tariffs.vectors (q_net) still count net imports per period, as DESIGN_LOG round 3 settled. The cap-and-pro-rata rule needs a MILP reformulation in that package.
- pages-D-10 stays partial. Printed peak and off-peak units and MDI are compared line by line, with residuals logged and a per-line 2 % check. The native-arm bill per billing period (R8 2.12, which the verifier called secondary) is not built: the native arm's per-minute series exist only inside the ledger's per-day replays (savings-engine's verify.compute_day).
- Pre-existing flaky test, not this package's: test_fix_r4_savings::test_an_off_grid_days_carry_over_is_valued_at_the_gensets_cost_at_its_night_load. demo.create_sites gives random site ids, and the twin seeds its days by id.
- Shipped fuel prices are still seeded only into a file with no fuel price, so a newer build's later OGRA prices would not reach an existing company file. This is outside these findings.
- Figures R9 marks for the owner to confirm on real bills (O3): the legacy surplus at NAPPP 25.32 against the off-peak rate, DSS 43 paisa for D-2, the extra-tax slab bounds (shipped unpriced), the MDI block mode, and the conventions the goldens pin (s.235 on the bill including GST, further and extra tax; the extra-tax slab chosen by the pre-tax amount; the export credit netted after tax). A real bill that differs now becomes a site override.
- The site override's MDI block mode and scope are applied to the bill only. The optimiser's MDI rows and the ledger's MDI lever (verify._minute_prices) still use fixed blocks over all hours (see cross_package).

Design log notes:

- **Export agreements:** exports earn a credit only between agreement_start and agreement_end (both inclusive). Before the start, after a net-billing end, or with an unreadable date, the regime is "none" and exports earn nothing. After a legacy net-metering end the site is billed under net billing (NAEPP, no netting). `tariffs.regime_on` is the one rule for the vector, `plans.build_problem` (X_max), the bill and the ledger. `GET /tariffs` lists `agreements[]` with state and `billed_as`.
- **FCA:** any adder whose key starts with fca or fpa is an FCA (`tariffs.is_fca`). It prices ICP units as the bill does, keeps the negative-FCA exemption and counts for `fca_billed`.
- **Legacy surplus:** credited at the notified NAPPP (prosumer version `surplus_rate: "nappp"`, shipped from 2026-01-01 at Rs 25.32). The off-peak, peak or a fixed rate is a per-version override that a legacy bill confirms.
- **Adders:** `category_rates` by tariff category or class; `exempt` may name a category or class. The shipped DSS is 0.43 for agricultural (secondary, confirm on a D-2 bill).
- **Shipped corrections:** `ensure_shipped` applies a newer build's correction of a shipped body with the same start (supersedes the old shipped row, with an audit line), never a company's own correction.
- **Seeding check:** a lookup checks the shipped library once per company file (path, device, inode) and process (`tariffs._SEEDED`); `ensure_shipped` called directly always checks. `add_fuel_price` seeds first.
- **TOU calendars:** a calendar must give every month exactly one season with a non-empty peak window.
- **Bill rebuild:** each day is priced with the versions in force that day (`tariffs.day_versions` runs, lines named by their days). Adders, ICP, s.235 and flat fees stay on the billing month (the reading date's).
- **Credit carry-forward:** an excess export credit is carried to the next bill (`credit_carried_rs`, with "Credit brought forward" and "Credit carried to the next bill" lines). `bearable_rs` is charges less recovered taxes less the period's own credit, so it can be negative. Entering, correcting or deleting a bill re-settles the bills that follow (`verify._rechain`).
- **New bill steps:**
  - PF penalty: 2 % of an MDI-based fixed charge per point below 90, from the printed `power_factor_pct`.
  - Punjab duty: Rs 0.04/kWh on grid imports for industrial and commercial connections above 500 kVA (`sanctioned_kva`, else `sanctioned_load_kw`).
  - Extra tax off the Active Taxpayer List: from the tax version's `extra_tax_non_atl` slabs, listed in `unpriced` when the version has none.
  - Per prosumer version: `credit_before_tax` and `tou_export_schedule`.
  - Hashes: `versions[]` carries every version's hash, plus a `series_hash`.
  - Line order follows R9 section 7.
- **ICP at legacy sites (bill only):** a period is eligible only with net import; increments are counted on imported units, capped at the month's net import and split pro rata (SRO 2409(I)/2025). The optimiser rows keep round 3's net-import rule until optimiser-plans changes them.
- **Bill comparison:**
  - Gaps: only minutes no grid reading covers are filled (a reading covers its source's interval), from the period's own profile (same quarter hour, Sunday or not).
  - Verdict: below 90 % completeness the verdict is `not_comparable`.
  - Line by line: printed peak and off-peak units (against gross imports) and MDI are compared, each line with its own 2 % check (`BILL_CHECK_PCT`, `BILL_COMPLETENESS_MIN`). Residuals go in the audit line.
- **Bills:**
  - One bill per period per connection: an overlapping bill is a 422.
  - New routes: `PUT` and `DELETE /api/sites/{id}/bills/{bill}`, which need upload_data and write audit lines that keep the old values.
  - Bill answers go through `out()`.
  - No store.py migration: the bills table's JSON columns hold the new fields.
- **Awaiting notification:** a version awaiting its notification whose start has passed is flagged (`tariffs.awaiting_notices`, words and dates only, readable by every role) in the vector, the plan summary and prices answer (`tariff_notices`), a rebuilt bill and the library (`notices`). Prices keep using the version in force.
- **Version names:** every library version has a readable name (`tariffs.version_name`), never a raw key.
- **Fuel price before the first entry:** the first price stands in, flagged (`stands_in`, `first_from`, `note`). The ledger withholds a day whose genset fuel, or whose off-grid carry-over, rests on it (`claim_withheld: "no_fuel_price"`, not a verified day).
- **Site overrides (review):** tariff_versions kind `override`, keyed by a connection id (R9 12.6), drafted with edit_tariffs and approved with approve_tariff. It sets `electricity_duty`, `adders {key: rate}`, `calendar`, `mdi_block` (fixed or sliding) and `mdi_scope` (all_hours or peak) for that connection only. It reaches the bill, the vector and plans, `energy_factor` and `calendar_for` (`tariffs.site_override`). `GET /tariffs` gains `connections[]`; a rebuilt bill carries `mdi_settings`; `vec['mdi']` carries `block` and `scope`.
- **Shared functions this package owns, used elsewhere:** `regime_on`, `agreement_state`, `is_fca`, `fca_billed`, `awaiting_notices`, `site_override`, `version_name`, `fuel_price`'s `stands_in` flag.

## telemetry-genset-battery

Statuses: implemented 20, partial 2

Not fully implemented:

- `pages-C-16` (partial): The answer's curve now carries measured[]: runs whose litres were all metered and output all read, at least 15 minutes long, as (load_ratio, litres_per_hour) points. It also carries fuel_logged{litres, entries}, the fuel
- `performance-live-06` (partial): save_forecast kept every issue for 3 days and then every hourly issue for ever: about 27 KB per site per belief time, roughly 240 MB a site-year. Its prune now keeps every issue for ISSUE_ALL_DAYS = 3 and the hourly ones

Remaining after review:

- pages-C-16 stays partial because it needs an owner or product decision. The policy card cannot show 'no-start hours' or 'prefer stop when SoC above X' (R8 2.11): neither the genset model nor the optimiser has these settings. The fuel log is set against the run log but does not refit a and b itself (fills are not burn).
- performance-live-06 stays partial. The 13-month minute-row archive to Parquet (DAT-07) is planned work. The 35-day hourly window departs from spec Part 3.3 ('every belief time 90 days, then hourly') and needs the owner's confirmation.
- test_fix_r4_savings.py::test_an_off_grid_days_carry_over_is_valued_at_the_gensets_cost_at_its_night_load failed once under -n 4 load and passed alone and in the next full run. It is timing-sensitive savings replay code, outside this package.
- Wording: the telemetry data-quality strip's sr-only text still reads the ISO day ('2026-11-10: 100 % so far (today)'). This was there before this package and the strip test relies on it; the strip is shared with ui-charts-a11y. It should become pktDay at integration or in stage 2.
- Minor copy: the refit panel says 'this period has N' metered runs, but the meter refit reads the last 30 days.
- Performance note for storage-perf: GET /now now calls verify.litres_used over the month on every poll. For a genset with only a run flag, that also builds a month's minute_grid and _actual_sim.
- Not done, follow-ups from batch 3: separate Date and Time columns are not combined by the mapping, and the CSV upload stays on Telemetry rather than Devices and data (UI-06).

Design log notes:

- **CSV overlap merges per column.** Edge batches and CSV uploads write the minute table as an upsert (`COALESCE(excluded.c, minute.c)`). A missing or blank cell never erases a stored value. Only the twin's own `write_rows` replaces whole rows.
- **CSV interval.** The logger step is the most common gap between rows (the shorter one on a tie). Rows 2-60 minutes apart are stored on the minutes they cover, readings held over them and interval energy (`grid_import_wh`, `grid_export_wh`, `genset_fuel_l`) divided by the step. Rows more than 60 minutes apart are refused. Gaps count only steps longer than max(5 min, step).
- **CSV plausibility.** PV or load below -500 W and a BMS SoC outside 0-100 % are stored as missing. A load or midday PV (10:00-14:00 Karachi) that never reaches 1,000 W over at least an hour is warned as probably kW. A SoC that never goes above 1 is warned as probably a fraction. PV above max(1 kW, 5 % of peak) for 30 minutes or more between 20:00 and 05:00 Karachi time is warned as a probable time-zone slip. An import with warnings has status `done_with_warnings`.
- **grid_present.** on/off, true/false and yes/no are read as 1/0. A file in which no row's grid_present or grid_voltage_v can be read is refused. Duplicate minutes keep the file's last row and are counted.
- **Time zones and times.** The web app sends Karachi time (tz_offset_h=5) by default; the API default stays UTC (plan 7.5). Times are read as ISO 8601, epoch seconds, or epoch milliseconds (values from 1e11), and dd/mm or mm/dd only as the mapping says.
- **Upload cap.** UPLOAD_MAX_BYTES = 25 MB equals api.MAX_BODY, with one sentence on the page, the API and the importer.
- **CSV column mapping (UI-06).** Column to key, unit (W/kW/MW, Wh/kWh/MWh, %/fraction, V/kV), reversed sign for battery and grid power and battery current, and date order. Saved per site in data_sources (kind 'csv'), so no migration. New routes: GET /telemetry/mapping (any member), PUT /telemetry/mapping and POST /telemetry/mapping/guess (upload_data), and upload ?mapping=saved.
- **One streamed parse.** An upload is parsed once, streamed into columns, and written one Karachi day per transaction, each day aggregated after its write.
- **Read transactions.** Telemetry reads use `telemetry.reading()` (a deferred transaction, never the write lock); only writes use `telemetry.connect()` (BEGIN IMMEDIATE). Nothing is written inside reading().
- **Series cap.** At most 2,000 points a series (SERIES_MAX_POINTS) for every role. Resolutions are 1m, 5m (new), 15m, 1h and 1d. An explicit resolution past the cap is coarsened, and the answer adds `requested_resolution` and `max_points`.
- **Forecast-issue retention.** Every issue is kept for 3 days, the hourly ones for 35 days, then only each Karachi day's day-ahead issue (the latest at or before midnight and at most 6 h before it). This departs from spec 3.3 ('every belief time 90 days, then hourly') and is pending the owner's confirmation.
- **Genset runs and outages, counted in minutes.** They are found over known readings: a blank reading bridges while two readings are at most RUN_GAP_S apart (15 min, or two source intervals), and a known off or grid-up reading ends them. Each row stands for the time to the next row, at most the source's own interval. The outage log, the alerts and the genset page share these functions.
- **Genset reading.** A run is read from the running flag, else the power, else the litres, as the Savings page reads it. Litres are metered where a meter reads, else from the fuel curve a × output + b × rating; the output comes from the reading, else the last reading carried forward, else the site's balance clipped to f_min..rating. `litres_source` is meter or curve.
- **No genset reading means unknown.** Without any genset reading in the period, hours, starts and litres are null, never 0 (`signal.present` false).
- **Fuel priced per day.** Each run's fuel is priced at tariffs.fuel_price of its own Karachi start day (`price_per_l`, `fuel_cost_rs`, `range_per_l`, all money).
- **Genset KPI row.** `hours_below_min` and `fuel_rs_per_kwh` (fuel only; money) are added. ?month=YYYY-MM is a Karachi month from 2015 to 9998, answered with `previous`, plus `daily[]`. A run is `ongoing` only in a period that ends now.
- **Fuel-curve refit.** New route POST /genset/curve/refit: weighted least squares with b ≥ 0, 0 < a ≤ 5 and b ≤ 1. Inputs are data-sheet points, or metered runs of the last 30 days (at least 3, at loads at least 10 % of the rating apart, each run at least 15 minutes with every litre metered and the output read). A preview is open to any member; saving with `apply` needs edit_sites.
- **Battery month and lifetime.** The battery page answers `month` (this Karachi month: cycles, `days_with_data` = minutes with a battery power reading / 1,440, cycles_per_day only from 1 day of readings, hot-and-full hours, `wear_rs` money) and `lifetime` (stored day rows before the month plus the month, since the first day that discharged). It also gives `battery.soh_source` 'entered' and `battery.soc_source` estimate or bms, and `grid_present` on the 48-hour series and the plan steps.
- **Today's completeness.** It is the share of minutes from Karachi midnight to 15 minutes ago (QUALITY_GRACE_S), marked `in_progress`. One colour rule on Telemetry and on Devices and data (lib/quality.ts): green from 95 %, amber (warn) from 80 %, red below, a dashed cell for no data.
- **Ownership.** telemetry.genset_runs and telemetry.outages are the single source for runs and outages (owned by telemetry.py). verify.litres_used remains savings-engine's and feeds the Overview month.

## tests-ci

Statuses: implemented 14, implemented (product half, on audit/storage-perf) 1, already_fixed 1

Not fully implemented:

- `tests-and-perf-20` (implemented (product half, on audit/storage-perf)): test_the_idempotency_key_header_is_allowed_cross_origin asserted a constant; it now sends the browser's OPTIONS preflight from the web app's origin asking for Idempotency-Key, checks the allow-headers answer, sends the P
- `tracker-and-docs-22` (already_fixed): Done by an earlier attempt of this package on this branch, verified here. source-rules.test.ts's banned-word pattern now includes 'optimal' (BANNED_IN_TEXT), and a test proves every banned word is flagged in strings and 

Remaining after review:

- Engine defect held as a strict xfail (tests-and-perf-15): at the advisory tier, a Solis table keyed by a person costs more than the native settings on the golden arbitrage day (it keeps -12 % of the plan's saving), against DLV-01. delivery.as_keyed runs Solis in self-use outside its slots. This depends on crossmodule's deferred OWNER DECISION 'advisory-keyed-profile'. Once fixed, the test XPASSes and the suite fails until the mark is removed.
- test_sibling's fixture-contract and typed-fields tests read a demo seeded at the current time. A field served only at some hours, or only in some states, is seen only by a run that meets it. outage_now was the one case found and is now covered deterministically. The other `unknown`-typed fields I checked (summary.guard is a string, a slot's genset is a boolean) carry no nested fields.
- TWN-01, test_fix_r4_savings.py:269 and test_fix_r5_genset.py:312 still draw fresh random demo ids. They check invariants and passed in every full run.
- Contract tests take 'today' from datetime.now(PKT), so a run that crosses Pakistan midnight could flake. This is rare and was not changed.
- VER-02 at the off-grid farm is not exact: the native replay's soc_threshold genset drifts up to 3 minutes, 0.15 L and 1.1 kWh on chained days. It is pinned by VER02_ID_SEED=3 and FARM_CHAINED_DRIFT; crossmodule owns the verify replays.

Design log notes:

- **Cross-repository tests (parity-architecture-09, tests-and-perf-18):** the backend reads the web repository only through `backend/tests/sibling.py`. That is `GRIDLYTICS_FRONTEND_DIR`, else `gridlytics-frontend` beside the backend; a folder holding both repositories is refused. The web app reads the backend only through `frontend/src/test/sibling.ts` (`GRIDLYTICS_BACKEND_DIR`, else `gridlytics-backend` beside it). `GRIDLYTICS_SKIP_SIBLING=1` is for a developer's quick run only, never CI. A file missing from the other repository always fails.
- **Fixtures follow the API:** `backend/tests/fixture_contract.py` names each web fixture's route and reader (`FIXTURES`). `test_sibling.py` fails when a fixture lacks a served field or holds one whose name the backend no longer has, and when a typed answer's field is missing from `types/api.ts`. `python backend/tests/fixture_contract.py [--write]` re-captures the shape from a scratch seed, keeping values. It always uses its own scratch database and data folder, never the shell's.
- **Older answers and the role map:** a page test that needs an older shape builds it with `frontend/src/test/older.ts` `without()`. `metaFor` reads `fixtures/meta_roles.json` (GET /api/meta's permissions per role); there is no hand-kept role map.
- **Copies checked across repositories:** the `Permission` union equals `auth.PERMISSIONS`. The authstate table covers `api.SIGN_IN_CODES`. The headers the web app sends and reads are in `CORS_ALLOW_HEADERS` and `CORS_EXPOSE_HEADERS`. The money field lists equal `routes_sites`. The viewer fixtures hold nothing `money.is_money` strips. TROUBLESHOOTING and ACCOUNTS are identical in both repositories.
- **No test is skipped by name:** the seven lifted Solarlytics tests are re-aimed, and test_hygiene enforces it. The DESIGN_LOG 'Lifted tests' follow-up is closed.
- **`slow` marker (conftest.py):** opt-in wall-clock benchmarks run with `-m slow` or `GRIDLYTICS_SLOW_TESTS=1` on an idle machine. Each keeps a default-suite counterpart on work that no machine's load changes.
- **Optimiser tests on a work budget (tests-and-perf-12):** `backend/tests/budget.py` sets HiGHS `node_limit` (TICK_NODES = 200; the 120 L farm 20) with a 600 s safety stop. A spent budget is reported as the time limit (status 1). Each solve's CPU time must be under 30 s (3 times the tick's 10 s). There are no default-suite wall-clock asserts on solves.
- **DAT-01 / DAT-08 (review):** the default suite asserts that every read of the `minute` table is a primary-key SEARCH (site_id, ts range), never a SCAN; the day read has no sort and returns 1,440 rows; the 30-day range is bucketed in SQL to at most SERIES_MAX_POINTS rows. The 10 ms and 200 ms figures are slow benchmarks.
- **Demo ids in tests:** tests whose figures depend on the twin's days give the demo seeded ids by setting `store.new_id` from `random.Random(seed)` (the off-grid carry-over test; VER-02 with `VER02_ID_SEED=3`). The off-grid farm's chained replay days get `FARM_CHAINED_DRIFT` (genset start/stop drift of a few minutes); every other figure is held to 0.05.
- **Part 7.4 contract suite:** it lives at `backend/tests/contract/`, one file per spec test, in the default suite. The cloud half runs through the edge's routes with a site API key. The edge half is `contract_kit.ReferenceEdge` (Ed25519 over RFC 8785 via `contract.canonical_json`, own site, rising seq, CAPS_STALE, EXPIRED, slot grammar) until DLV-04's executor exists. The Solis keyed DLV-01 shortfall is a strict xfail linked to the advisory-keyed-profile owner decision.
- **Company isolation (tests-and-perf-14):** `test_isolation.py` walks every `{site_id}` route. Another company's site answers 404 (403 for the signed plan message to any person). A change aimed at it leaves both company files byte-for-byte unchanged. Edge keys get `wrong_site`. Keys and reports stay in `keys/<stem>` and `reports/<stem>`, and concurrent requests do not leak the ContextVar.
- **Product changes:**
  - api.py (auth-security's file, minimal): a broken `GRIDLYTICS_CORS_ORIGINS` JSON array or a broken `GRIDLYTICS_CORS_ORIGIN_REGEX` stops the start with the setting named (new `api._cors_regex`).
  - routes_telemetry: the upload's file name always goes to `telemetry.import_csv` (signature shim removed).
  - routes_tariffs.fuel_add: calls `tariffs._canonical_day` directly (getattr shim removed).
- **Web types:** fields the API already served are now typed (grid_charge_permitted, import_completeness, mdi.block/scope, month_export_kwh, horizon_steps, control_release, scored_at, month_totals_projected and, at review, `PlanSummary.outage_now` as {since, elapsed_min, expected_remaining_min} | null). No API shape changed, so API_CONTRACT has no tests-ci section.
- **Browser check:**
  - The accounts list is `--accounts`, else `GRIDLYTICS_ACCOUNTS`, else `../gridlytics-backend/.scratch/data/DEMO_ACCOUNTS.local.md`; the owner's `gridlytics-backend/data` is refused.
  - `npm run e2e` includes `--acceptance` (UI-02 site created and activated, UI-06 CSV upload, VER-10 report figures equal the page's).
  - The 375x812 pass covers `?view=` pages, five dialogs and the installer's Slots, vendor table, Mark-as-applied and it-took path.
- **Source-rules lint:** it bans 'optimal' in page text, as format.ts BANNED does.

## ui-charts-a11y

Statuses: implemented 13, partial 2

Not fully implemented:

- `frontend-states-formats-04` (partial): Multi-day chart axes showed only HH:MM, so day ticks read '00:00'. Their ticks were also placed in the browser's own time zone. TimeSeriesChart, FanChart and the plan Gantt now plot Karachi's wall clock read as UTC (pktA
- `role-owner-22` (partial): The y-axis unit ('kW', '%') and the 'now' marker label overlapped at the top-left of the forecast and outage charts. The unit now sits 20 px above a 26 px top margin (AXIS_TOP and AXIS_NAME_GAP in time-axis.ts), clear of

Remaining after review:

- role-analyst-30 (duplicate of frontend-states-formats-04): Telemetry's '24 hours' range still spans 72 h because of the 48 h plan overlay. It needs an owner decision (overlay off by default, or clipped) in telemetry-chart.tsx (telemetry-genset-battery).
- role-viewer-14 (duplicate of role-owner-22): 'Battery power' and 'SoC (estimated)' are both solid teal on the Telemetry main chart. telemetry-series.ts (telemetry-genset-battery) should dash one or draw battery power as bars.
- role-operator-27 step 5 (duplicate of role-owner-29): the Plan 'Reasons in this plan' hint still says 'hover a chip', and the Radix tooltip does not open on a tap. This is frontend-a11y-12 ('no way to read the reason on a phone'), owned by optimiser-plans and not yet on their branch.
- Raw tables outside the shared Table do not stack on a phone: the founder console's .table-wrap tables (founder-console) and the switchback calendar (savings-engine/pilot).
- The headless-Chrome page check (scripts/e2e/pages.mjs) has not been run against this branch. Only vitest SSR/jsdom tests and the implementers' scratch-page Chrome checks cover the layout.

Design log notes:

- **Contrast tokens (WCAG 2.2 AA).**
  - `--color-muted` is now #5b6576: at least 4.5:1 on bg, surface, surface-2, brand-soft, line and every soft tone, and still lighter than ink-2.
  - New `--color-line-strong` #7a8597 is the border of form controls (inputClass, selectClass, textareaClass, founder `.fc` inputs and selects, the site switcher trigger), at 3:1 or more. `--color-line` stays for dividers and card edges.
  - Source colours: PV #c27c0e, export #0284c7, muted series #7a8597. Every solid SOURCE colour reaches 3:1 on white, and the @theme tokens equal SOURCE.
  - Checked by styles/contrast.test.ts, lib/colors.test.ts and components/ui/field.test.tsx through src/test/contrast.ts, which reads index.css.
- **Data-quality strip.** A 0 % day is red like any day below 80 %. A day with no data is a dashed outlined cell. (telemetry-genset-battery's lib/quality.ts carries the same rule.)
- **EnergyFlow.** Labels are drawn in ink at 13 units and notes at 12 (at least 10 px on a phone card). The offline notice is a banner (figcaption) above the figure, never across it.
- **Chart time axes in Karachi.**
  - Charts plot each instant at its Karachi wall clock read as UTC (`pktAxisMs`; `pktFromAxisMs` reads back), with `useUTC: true`.
  - A Karachi midnight tick reads 'Fri 6 Nov' (`pktAxisLabel`).
  - `karachiTimeAxis` asks for one tick per 80 px of measured plot width (`useBoxWidth`).
  - `AXIS_TOP` 26 and `AXIS_NAME_GAP` 20 keep an axis unit clear of the 'now' label.
  - An axis with no shown series is not drawn.
- **Chart text alternative (components/charts/chart-text.tsx).**
  - Every chart's image has `aria-describedby` pointing to a summary: the span and now in Karachi time; each series's highest, lowest and last value with unit and time; the series not drawn; the shaded spans.
  - A 'Show as a table' button (aria-expanded) opens all the data as a table, one Karachi time per row as the row header, with 'Name (unit)' column headers.
  - Tooltips give every value its unit: the series's `unit`, else the last word of the axis label.
  - The plan Gantt has no canvas legend. HTML series buttons (role=group, aria-pressed) are passed to ECharts as `legend.selected`, and the now line and outage bands ride on a data-less series.
- **ARIA names.** aria-label only on elements with a role. The reason chip's sentence is sr-only text. The DEMO ribbon's toggle says 'Fold/Show the demo notice' with aria-expanded and aria-controls.
- **Shared Table (components/ui/table.tsx).**
  - `STACK_BELOW` = 640 px. A table that overflows a narrower box shows each row as a card, each cell under its column's name (`data-label` from the last header row). Explicit ARIA table roles keep it read as a table. The CSS sits in index.css under `@media screen`, outside any layer.
  - A wider box that still overflows becomes a focusable region named '<caption>, scrolls sideways', with edge fades and the line 'More columns to the side: scroll sideways to see them.'
  - `layout="scroll"` opts out of stacking: only the plan's slot table, per R8 section 565. `framed={false}` drops the box's border.
  - `Th scope="row"` is a row header (for frontend-a11y-17).
  - Td keeps a single figure, or a tabular-nums cell, on one line; `whitespace-normal` from the call site still wins.
- **KPI tiles.**
  - `keepUnits` (appended to lib/format.ts) joins a number and its unit, and 'Rs' and its figure, with no-break spaces.
  - The Preliminary mark wraps below the label instead of squeezing it.
  - KpiRow is `repeat(auto-fit, minmax(9rem, 1fr))`; a call-site `xl:grid-cols-N` still applies.
- **HeatmapGrid.**
  - `heatmapFill` strength is 0.08 + 0.92 x |v| / max|v|.
  - Negative values (export) are drawn in a second colour: PV amber against blue, blue against an amber series, or the `negativeColor` prop.
  - A colour-scale legend gives the lowest and highest values in the unit, 'through 0' when some values are negative, plus a 'No data' swatch. `legend={false}` where the card has its own key (the TOU calendar).
  - A no-data cell is dashed and outlined.
- **Segmented.** ARIA radio-group keyboard: one tab stop on the checked option, arrows wrap, Home and End go to the ends, and moving selects.
- **Lettered wizards (components/workflow).**
  - Each panel opens with a heading 'Step N of M: <label>' (tabIndex -1). `UseLetteredTabs` gains `headingId`.
  - After a step change the focus moves to that heading in a layout effect. It does not move when the page opens, nor while the focus is on a tab.
  - The panel stays a tab stop and shows the page's focus outline.
  - LetteredNav sends `onNext` once while it runs and marks Next and Previous aria-disabled meanwhile; buttonClass styles aria-disabled like disabled.
  - The tab row scrolls the active tab into view on every step change (`stripScroll`, 16 px pad), scrolling whichever box holds the row sideways (review e38c085).
- **No API, schema, permission, money-filter or migration change. No backend change.**

