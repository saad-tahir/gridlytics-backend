# Continue from here (13 November 2026)

The round 7 audit fixes and the UI/UX redesign paused here at the owner's request. **The full guide is in the web repository:**
`gridlytics-frontend/docs/ux-handover/README.md`, on the same branch (`claude/inspiring-wozniak-bk8mis`). It covers:
- what is done and what is left, step by step;
- the owner's rules;
- the step recipe and the quality bar;
- the environment, the test lock and every script;
- the 30 open owner decisions (`OWNER_DECISIONS.md` beside it).

## Backend-specific pointers

- **`docs/ROUND7_RESULTS.md`:** every audit finding's final status by package, with what each review left and the reviews' design-log notes. The
  `docs` package writes DESIGN_LOG round 7 from those notes.
- **`docs/DESIGN_LOG.md`, "Round 7 UX backend asks":** the redesign's backend asks, one section per batch (k1 to k9) and per review.
- **What is left in the backend lane:**
  - batch **k10**: K30, the fee's history by month (a schema change, with its migration and the backup step); K31, the new report kinds,
    covers, `twin_id` and `out_of_date`;
  - the senior review of **k9 and k10**.

  K22 is deferred by the plan until after step 15.
- **Tests:** always through the machine-wide lock (`flock /tmp/gridlytics-fulltest.lock`). The solver tests are time-limited and fail falsely
  under load. Full suite: `pytest backend/tests -q -n 4 -p no:cacheprovider` with the pinned venv (`requirements-dev.txt`).
- **Data:** never write to `data/`; use `.scratch/<folder>`.
- **The audit itself** (findings, plan, original handover) is on `main` under `docs/audit-r7/`.
