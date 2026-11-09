# My own reading of PR fix/offgrid-carry-over (both repos), 9 Nov 00:45 UTC

Backend: 2 commits on top of v1-build 74b2cc7 (b31ccad round 6 chained replays; ab83058 farm benchmark opt-in). 6 files, +701/-43: verify.py (+273), test_fix_r6_savings.py (+362, 9 tests), DESIGN_LOG round 6 (+69), HANDOFF 8.A.1 closed, reports.py lever label, test_fix_r5_genset.py skip marker.
Frontend: 1 commit 499cebb: API_CONTRACT round-6 section, GATES, HANDOFF, levers.ts label, savings-page.tsx StaleNotice (chain_stale_days), 2 vitest, types.

What it does: each baseline replay (native, tuned) starts from its own end state of the day before (soc, gen_on, gen_elapsed, outage_min, gen_rescue) instead of the actual arm's 00:00 state; carry-over = change of valued gap (v_end*(gap_end) - v_prev*(gap_start)) so days telescope to one continuous replay; restarts: first day, gap, no_state (pre-chain rows), unread_inputs after a non-claiming day with blank load/PV/grid minutes, switchback block boundary; genset run across midnight = one start (gen_before); bound subtracts carry_start; compute_day(forward=True) re-scores later stored days whose chain start no longer matches, up to 62 days; compute_range walks forward once; summary adds chain_stale_days + per-day baseline_start; evidence table: ledger minus continuous replay = 0 / -1 Rs on farm s31-33, Karachi s31; Faisalabad switchback +1,955 vs pure, -1 vs segmented.

Strengths: the right fix for HANDOFF 8.A.1; clear invariants; good tests (3-day farm & grid site sum = continuous; recompute one day; first day; native block; dropout on withheld day; withheld on read load; later dropout → stale; switchback hand-over); docs updated in both repos; API additive and typed.

Concerns / review comments:
1. Synchronous forward walk: compute_day(forward=True) can re-score up to 62 later days inline (each = 2 twin replays + optional bound). POST /savings/recompute and any single-day recompute from the API/tick may take minutes and block the request or the 15-min tick. Ask: run the forward walk in the scheduler (job) or cap per request, return 202 + chain_stale_days.
2. Switchback segmented replay assumes the inverter really ran native in native blocks. The live tick does not apply native blocks (engine-savings-01, HIGH; only the demo twin follows the calendar) — so on a real site the "native block scores ~0" claim is false until that fix lands. Merge is fine, but note the dependency in DESIGN_LOG round 6 and HANDOFF.
3. Hand-over credit at v_end can exceed realised (Faisalabad 3,263 vs 1,771): the ledger over-claims on switchback sites by that gap; the PR records it as a known limit. Ask for the plan block's last day to be marked provisional in the UI ("hand-over credited, not yet realised") or withheld until the native block ends — a credibility point with a Pakistani owner.
4. unread_inputs restart heuristic: moves next-day claim by -0.8k..+1.6k; documented; acceptable with the "Preliminary" flag; the real fix (fill unread load from the forecast's equivalent day) should be a tracked item (HANDOFF 8 lists it).
5. _start_matches compares floats to 1e-9 after a JSON round trip: fine (repr round-trips), but `v_rs_per_kwh` of pre-chain rows defaults 0.0 — OK.
6. Backward compatibility: rows stored before round 6 have no `chain`; treated as from-actual → not stale; first new day after upgrade restarts (no_state). Good; mention in HANDOFF upgrade note: run POST /savings/recompute for the current period after deploying.
7. Performance of summary(): _chain_stale runs per GET /savings; cheap (dict walk), fine.
8. Tests: 504 backend / 259 web claimed on the branch; I have not yet run them on the PR branch (planned in a scratch worktree once the live reviewers finish, to avoid CPU contention).
9. Frontend: StaleNotice wording good; viewer sees "An administrator, operator or analyst can recompute" (no money) OK; `baseline_start` typed but not shown — fine.
10. Nit: lever label change "Carry-over (change in stored energy)" — also update the PDF glossary/help text if any mentions "stored energy at day end" (grep both repos).

Verdict (mine, before the pr-review agent): approve with comments; merge after (1) is at least documented with a cap, and after tests pass on the branch; follow-ups filed for (2) and (3).
