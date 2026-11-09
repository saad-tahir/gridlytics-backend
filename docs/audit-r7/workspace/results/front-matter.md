# Gridlytics end-to-end audit (v1-build), with fixes, parity plan, designs and ship gate

Prepared for OnyxLeaf, 9 November 2026. Repositories: `saad-tahir/gridlytics-backend` (v1-build at 74b2cc7) and `saad-tahir/gridlytics-frontend` (v1-build at 0e3cc3f). Reference products, read only: Boxlytics (the attached `boxlytics-main.zip` and `boxlytics-backend-main.zip`), Solarlytics (`saad-tahir/solarlytics-backend`, `saad-tahir/solarlytics-frontend`).

This is one document to work from later. Every finding has steps, evidence, a verification record and a proposed code change with the regression test to write first. Nothing in either repository was changed by this audit; no issue, comment or pull request was opened.

## Part 0. Summary

{{COUNTS}}

**The product works end to end.** Both suites are green on a clean checkout, the scratch demo seeds four sites with 21 days of history, every page renders for every role at desktop and phone width, and the optimiser produces plans that charge in cheap hours, discharge in the peak window and hold a reserve before forecast outages. The code is unusually well documented (DESIGN_LOG, HANDOFF, plan package) and the role model is enforced on the server.

**It is not shippable to a paying customer yet**, for four reasons that the findings substantiate:

1. **Money and savings integrity.** The switchback design that justifies the savings claim is not applied by the live tick (only the demo twin follows the calendar); closed-loop days are "confirmed" by construction; the tuned baseline is void at off-grid sites; several per-step and ledger figures round or label wrongly. A Pakistani factory owner who checks the rebuilt bill against the DISCO bill will find differences the app cannot explain.
2. **Safety and validation at the control edge.** `POST /control/{action}` accepts an unvalidated dict and returns 500 on bad input; the Mode A slot tables drop `grid_cap_w` for vendor layouts and use generic rows for five of seven brands; the write budget resets on the UTC date, not the Karachi date.
3. **Role leaks and wording.** The viewer sees the fee multiple on `/pilot`, the sidebar lists pages the role cannot open, and several messages promise things that do not exist ("ask your administrator for a new password" with no admin route to set one).
4. **Product-family parity.** Gridlytics does not yet look like a sibling of Boxlytics (fonts, tokens, landing page, header and sidebar, legal pages, footer), and the deployment story (CI, Docker, env, backups) is absent. The sign-in model is right and must stay as it is: preassigned accounts, email and password only, no Google, no self-signup, exactly as Solarlytics.

**The open pull request** `fix/offgrid-carry-over` → `v1-build` (both repositories) is reviewed in Part 4 under its own findings and in Part 8; the verdict and the review text to post are there.

**How to use this document.** Part 2 is the ordered work plan (what to do first). Part 3 is the findings index; Part 4 the findings with their fixes; Part 5 the Boxlytics parity change plan; Part 6 the designs for HANDOFF section 8; Part 7 the ship gate and the end-to-end acceptance suite; Part 8 the pull-request review; Appendix D the candidate findings that verification refuted.

## Part 1. Method and setup record

### 1.1 What was done

| Step | Result |
|---|---|
| Clone both repositories at v1-build, side by side (`../gridlytics-backend` resolves through a symlink to the clone) | done |
| Backend: Python 3.12 venv in `.scratch/venv`, `pip install -e .[dev]` | fastapi 0.141.1, scipy 1.17.1, numpy 2.4.3, pandas 3.0.2 |
| Backend tests | 495 passed, 7 skipped, 60 s |
| Web app: Node 22, `npm ci`, `npm run gates` | 257 tests, typecheck and build clean |
| Seed the scratch demo (`.scratch/data`, never `data/`) | 4 demo sites, 21 days of telemetry, plans and ledgers |
| `python scripts/start_local.py --scratch` | API 127.0.0.1:8151, web 127.0.0.1:5295, 10 launcher checks OK |
| Repository browser check `npm run e2e` (Chromium through CDP) | 405 page visits, 0 failures; the 403s are the designed ones (viewer on tariffs, prices, bills, users, keys, audit; operator and analyst on users, keys, audit) |

### 1.2 How the review was run

- **Code reviewers** (one per dimension, read-only): pages A–D against EMS_BUILD_PLAN Part 9 and the brief; engine optimiser, forecast, tariffs, delivery, savings; brief-vs-build; security and roles; accessibility; loading/empty/error states and number/time formats; the open pull request; tracker and docs; tests and performance.
- **Live reviewers** against the running scratch app, with a Playwright helper that signs in through the real page and records, per page and width, the console, page errors, failed requests, 4xx/5xx API answers, horizontal overflow, headings, unnamed buttons, unlabeled inputs, landmarks, money words and banned words, plus a screenshot: every page as owner, operator, analyst and viewer at 1440 and 375 px; the founder console; the onboarding wizard; CSV upload, plan and control; savings, pilot and bills; reports, alerts, members and API keys; engine sanity on the four demo sites; tariff prices, bill rebuild and savings in rupees; performance.
- **Parity reviewers**: UI/UX against the real Boxlytics app; Supabase, auth and multi-company against Boxlytics and Solarlytics (under the owner's rule: preassigned accounts only); architecture and repository shape.
- **Verification**: every candidate finding was re-checked by a second agent against the code or the live app and against DESIGN_LOG, the plan and HANDOFF sections 8–9, which refutes what is by design or already an open item without new evidence; the severity was reset to what it means for a Pakistani owner, operator, analyst or viewer and for OnyxLeaf's credibility.
- **Fix proposals**: written by a third agent after reading the real code, as diff sketches with the regression test first. They are sketches to apply by hand on a branch off `v1-build`, one branch per piece of work, with both suites run before the pull request.

### 1.3 Limits of this audit

- Several agents shared one 4-CPU machine, so page-load times and plans solved during the audit may be slower or time-limited because of audit load; engine judgements use the seeded history.
- No GitHub API access from this session: nothing was posted. The review text for the pull request is in Part 8, ready to paste.
- The owner's real `data/` folder was never read or written; the audit used `.scratch/data` throughout.
- Findings were raised by automated reviewers and verified by automated verifiers; a handful may still be wrong in detail. Each gives the exact file, line, step or API call so the developer can check in a minute before changing anything.

### 1.4 Rules this document follows (from the owner)

- Branch off `v1-build` per piece of work; add tests; run both suites; pull request into `v1-build`; never push to `main` or `v1-build`; never touch `data/`; no secrets in commits.
- Boxlytics and Solarlytics are read only; reused code is recorded with its path and commit (THIRD_PARTY_NOTICES.md).
- Sign-in: preassigned accounts, email and password, as in `solarlytics-backend` (local mode by default; Supabase only as an optional mode the owner applies by hand, with sign-ups, Google and every other provider switched off). No Google login, no self-signup, no magic links, no invitations.
- A viewer never sees money.

## Part 2. Ordered work plan

The order below is by risk to money, safety and credibility, then by what unblocks the rest. Each line names the finding IDs or the part of this document to work from; effort is S (under half a day), M (one to three days), L (a week or more).

{{WORKPLAN}}
