"""The senior review of the backend lane's batches k5 (K32, the tariff editor) and k6 (K5, K6, K7, K9, K10, K11; plan H11): what the review found
and fixed, each held by a test that fails without its fix.

- A figure too large to work out never becomes a server error. A rate the library takes (a rate card's rate, a PTV fee: finite numbers with no upper
  bound) can still give a change in percent, a price or a bill past what a float holds; the answer then cannot be written as JSON and the editor met
  a 500. The change in percent is one rule (`tariff_drafts.change_pct`, for what a version changes and for the preview's and the simulation's rates)
  and is null where it cannot be worked out; a preview or a simulation whose prices or bill cannot be worked out is refused in one sentence.
- Discarding a tariff draft is one step with its audit line: the draft goes and the line is written in one transaction, so a discard whose audit line
  fails leaves the draft, and a second discard of the same draft (a double click) finds none (404) and writes no second line."""
from __future__ import annotations

import copy
from datetime import date

import pytest
from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_fix_r2_savings import _flat_rows
from test_fix_savings import make_site
from test_ux_k32_tariff_editor import A3, FCA_DEC, _actions, _draft, _fca

from gridlytics import store, tariff_drafts, tariffs, verify

TOO_LARGE = "This version's prices cannot be worked out: a rate in it is too large. Check the rates typed"


# ------------------------------------------------------------------------------------------------ a figure too large to work out


def test_a_change_in_percent_is_one_rule_and_null_where_it_cannot_be_worked_out():
    assert tariff_drafts.change_pct(2.0581, 2.5581) == 24.3
    assert tariff_drafts.change_pct(49.01, 49.52) == 1.0
    assert tariff_drafts.change_pct(10.0, 10.0) == 0.0
    # nothing to compare: no figure before, none after, a figure of nought before, or a figure that is not a number
    assert tariff_drafts.change_pct(None, 1.0) is None and tariff_drafts.change_pct(1.0, None) is None
    assert tariff_drafts.change_pct(0, 5.0) is None and tariff_drafts.change_pct("1", 2.0) is None and tariff_drafts.change_pct(True, 2.0) is None
    # past what a float holds: null, never inf
    assert tariff_drafts.change_pct(2.0, 1e308) is None and tariff_drafts.change_pct(-1e308, 1e308) is None


def test_a_rate_too_large_to_price_is_refused_in_words_and_never_a_server_error(world):
    """A schedule whose A-3 flat rate is 1e308 Rs/kWh passes the library's checks (a finite number, 0 or more). Before the review the validate answer
    held a change of inf %, the preview a week's mean price of inf and the simulated bill an energy charge of inf: each was a 500."""
    an = world.c["analyst"]
    with world.use():
        sid = make_site(name="Huge A3", conn=A3, battery=False, genset=False)
        _flat_rows(sid, date(2026, 8, 26), 31, 30.0)
        bill = verify.add_bill(sid, {"period_start": "2026-08-26", "period_end": "2026-09-25", "total_rs": 1450000.0}, "test")
        schedule = copy.deepcopy(tariffs.resolve("schedule", "national", "2026-09-01")["body"])
    schedule["cards"]["A-3"]["flat"] = 1e308
    huge = _draft([{"kind": "schedule", "key": "national", "body": schedule}])
    r = an.post("/api/tariffs/versions/validate", json=huge)
    assert r.status_code == 200, r.text
    flat = next(c for c in r.json()["versions"][0]["tariff_changes"] if c["field"] == "body.cards.A-3.flat")
    assert flat["after"] == 1e308 and flat["change_pct"] is None
    r = an.post("/api/tariffs/preview", json={"site_id": sid, "draft": huge, "days": 1})
    assert r.status_code == 422 and r.json()["detail"] == TOO_LARGE, r.text
    r = an.post("/api/tariffs/simulate", json={"site_id": sid, "bill_id": bill["id"], "draft": huge})
    assert r.status_code == 422 and r.json()["detail"] == TOO_LARGE, r.text
    # the same site and bill under an ordinary draft still answer (the goldens are test_ux_k32_tariff_editor's)
    ok = an.post("/api/tariffs/simulate", json={"site_id": sid, "bill_id": bill["id"], "draft": _draft(_fca())})
    assert ok.status_code == 200 and ok.json()["bill"]["change_pct"] == 1.0, ok.text


# ------------------------------------------------------------------------------------------------ discarding a draft


def test_discarding_a_draft_is_one_step_with_its_audit_line(world, monkeypatch):
    an, ad = world.c["analyst"], world.c["admin"]
    assert an.put("/api/tariffs/drafts/gone", json=_draft(_fca(FCA_DEC), start="2026-12-01")).status_code == 200
    # the audit line cannot be written: nothing is discarded
    real = store.audit

    def failing(actor, action, *a, **k):
        if action == "discarded a tariff draft":
            raise RuntimeError("the audit trail is unavailable")
        return real(actor, action, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(store, "audit", failing)
        assert ad.delete("/api/tariffs/drafts/gone").status_code == 500
    assert an.get("/api/tariffs/drafts/gone").status_code == 200
    # discarded once, by the administrator: one line; a second discard (a double click) finds none and writes none
    assert ad.delete("/api/tariffs/drafts/gone").status_code == 200
    second = ad.delete("/api/tariffs/drafts/gone")
    assert second.status_code == 404 and second.json()["detail"] == "No such tariff draft", second.text
    lines = [a for a in _actions(world) if a["action"] == "discarded a tariff draft"]
    assert len(lines) == 1 and lines[0]["target"] == "gone"
    assert an.get("/api/tariffs/drafts/gone").status_code == 404


@pytest.mark.parametrize("who", ["analyst", "admin"])
def test_a_discard_that_loses_the_race_to_a_submit_discards_nothing(world, monkeypatch, who):
    """The draft is read (it is there), then submitted by its author before the discard's own transaction: the discard finds none (404) and writes no
    line, where before it answered 200 and audited a discard of nothing."""
    from gridlytics import routes_tariffs
    an = world.c["analyst"]
    assert an.put("/api/tariffs/drafts/race", json=_draft(_fca(FCA_DEC), start="2026-12-01")).status_code == 200
    held = routes_tariffs._draft_or_404

    def read_then_submit(draft_id):
        d = held(draft_id)
        if not getattr(read_then_submit, "done", False):
            read_then_submit.done = True
            assert an.post(f"/api/tariffs/drafts/{draft_id}/submit").status_code == 200
        return d

    monkeypatch.setattr(routes_tariffs, "_draft_or_404", read_then_submit)
    r = world.c[who].delete("/api/tariffs/drafts/race")
    assert r.status_code == 404, r.text
    assert not [a for a in _actions(world) if a["action"] == "discarded a tariff draft"]
