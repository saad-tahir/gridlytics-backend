
## Appendix C. Aspects outside the findings, and the audit's own limits

These are not defects in the code. They are things a developer or the owner has to know or do, found while setting up and reviewing, that no single finding covers.

### C.1 Repository and process

- **The plan package is not on `v1-build`.** `EMS_BUILD_PLAN.md`, `EMS_BUILD_PROGRESS.md`, the research notes (R1–R9, U1–U3), the review lenses and the requirement documents live only on the branch `docs/plan-package` of the backend repository. HANDOFF and DESIGN_LOG cite them by path. A developer who clones `v1-build` cannot open the files the code comments point to. Merge the package into `v1-build` under `docs/plan/` (or link the branch and commit from HANDOFF section 1).
- **The open pull request `fix/offgrid-carry-over` changes the savings ledger.** Several savings findings in Part 4 were measured on `v1-build`. After that PR merges, re-check the findings tagged "savings" against the branch: the carry-over lever label, the per-day ledger and the switchback replay change. Part 8 lists which.
- **Two clones side by side are required, and the launcher resolves `../gridlytics-backend`.** A developer who clones the backend under another folder name gets a launcher error. HANDOFF section 3 says so; repeat it in the frontend README's first lines.
- **No CI.** Both suites run only by hand. Part 7 gives the GitHub Actions files.
- **No issue tracker use.** The tracker is a Markdown file on a side branch. Once this document's findings are worked, consider turning each G-number into a GitHub issue with the label in Part 3 (`bug`, `ux`, `feature-gap`, `spec-mismatch`), so the tracker and the issues do not diverge.

### C.2 Data and environments

- **The owner's real data folder holds a stale demo** (HANDOFF 8.D). It was seeded before rounds 4–6 and its stored savings rows predate the chained replays. Re-seed it on the owner's machine after the fixes, or delete the demo company from the founder console, before showing anyone the app.
- **Back up `data/` before any schema change** (store.py migrations run on start). SQLite files are copied safely only with the scheduler stopped, or through SQLite's online backup API; a plain copy of a file being written can be torn.
- **Secrets.** `.env.founder`, `platform.secret` and the demo accounts file are in the data folder by design and git-ignored. A deployment must keep the data folder off any web root and readable only by the service user. Nothing in this audit printed or copied them.
- **The scratch demo is load sensitive.** Plans solved on a busy machine hit their time limits (HANDOFF 9; Part 6, solve limits). Judge plan quality on an idle machine or on the seeded history.

### C.3 Product and commercial

- **Real-site data has not been seen.** Every engine judgement in this audit rests on the twin's simulated telemetry. The first real inverter export (CSV) will exercise parsing, units, time zones and gaps in ways the demo cannot. Part 7's acceptance suite includes a CSV fixture set; ask the first customer for two weeks of raw exports before the pilot starts.
- **Inverter brands.** Mode A compiles tables for seven brands; the grammars for five were written from manuals, not from a keyed device. Each brand needs one installer session keying a real table and a photo of the result before it is offered (Part 4 lists the per-brand gaps).
- **Savings claims are a legal statement.** The ledger's wording, the fee card and the monthly PDF are what a customer will hold OnyxLeaf to. The owner should have the wording in the monthly statement reviewed once, and the fee clause in the contract should cite the method (native baseline, switchback, Preliminary until 8 weeks) exactly as the PDF prints it.
- **Tariff versions need an owner of record.** Every NEPRA determination, SRO and quarterly adjustment changes the prices. Someone must enter and approve each version (the editor in Part 6) within days of its notification, or every plan and saving after it is priced on the old tariff.

### C.4 Sign-in and accounts (owner's rule)

- Sign-in stays as it is: preassigned accounts, email and password, made by the founder console or a company administrator. No Google or other social sign-in, no self-signup, no magic links, no invitations. Part 5.10 lists the Boxlytics pieces that are deliberately not ported.
- The sign-in page tells people to ask their administrator for a new password. That must be true before go-live: a company administrator (or the founder console) needs a "set a new password" action, and the person should be made to change it at next sign-in. See the finding on the missing password-reset route.
- If the owner later wants Supabase as the account store, it is Solarlytics' optional supabase mode, with sign-ups, Google and every other provider switched off in the project, and the SQL applied by the owner by hand (Part 5.10).

### C.5 Limits of this audit

- Several agents shared a 4-CPU machine; slowness and time-limited plans during the audit may be audit load.
- The model running the reviewers hit its usage limit twice; every reviewer that failed was re-run from cache, so no dimension was skipped, but reviewers re-ran their screenshots from scratch.
- No GitHub access from this session: no issue, comment or pull request was created. The pull-request review text in Part 8 is ready to paste.
- The owner's `data/` folder and the Boxlytics and Solarlytics repositories were never modified.
