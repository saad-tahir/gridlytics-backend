"""UX backend ask K32 (plan H11; F11 item 6, F2.1, F2.2, F2.5; step 13, Tariffs and the tariff editor).

- `POST /api/tariffs/versions/validate` checks a draft as the editor holds it, every problem at once, each tied to its field and the editor's step,
  by the library's own checks (tariffs.body_problems), and sets each version beside the one in force on its start with what it changes.
- Drafts (`/api/tariffs/drafts`, schema version 8) are saved as their author types, seen by their author and administrators, never waiting; one is
  submitted as versions waiting for approval, all or none, and a second person approves (the owner's question Q65, recommended answer).
- `POST /api/tariffs/preview` and `/simulate` price a week and rebuild a bill under the draft beside the version in force (tariffs.what_if: the
  library as approving the draft would leave it, nothing written). The goldens are worked by hand from the SRO rates.
- A waiting version is withdrawn by its author or an administrator ("withdrawn", sent back as a draft on request); shipped versions are marked
  `template`; an export agreement ending within the alert window says in how many days (`ends_in_days`), as the Tariff alert does."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import identities
import pytest
from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_fix_r2_savings import _flat_rows
from test_fix_savings import make_site
from test_fix_tariffs import SRC, company  # noqa: F401  (company is a fixture)

from gridlytics import alerts, money, plans, sites, store, tariffs, verify

PKT = timezone(timedelta(hours=5))
#: The FCA billed on September 2026's bills, corrected: Rs 0.50/kWh above the shipped 2.0581 (FCA July 2026)
FCA_SEP = {"name": "FCA July 2026 (corrected)", "rate": 2.5581, "billing_months": ["2026-09"], "exempt": ["lifeline", "prepaid", "evcs"],
           "negative_exempt_icp": True}
FCA_DEC = {"name": "FCA October 2026", "rate": 1.38, "billing_months": ["2026-12"], "exempt": ["lifeline", "prepaid", "evcs"], "negative_exempt_icp": True}
A3 = {"tariff_category": "A-3", "sanctioned_load_kw": 40, "regime": "flat", "reading_day": 25}


def _draft(versions, start="2026-09-01", ref="NEPRA decision of 4 Sep 2026", verified="primary"):
    return {"effective_from": start, "source": {"ref": ref, "url": "", "verified": verified}, "versions": versions, "editor": {"step": "C"}}


def _fca(body=FCA_SEP):
    return [{"kind": "adder", "key": "fca", "body": body}]


def _actions(w) -> list[dict]:
    with w.use():
        return store.list_audit(limit=1000)


# ------------------------------------------------------------------------------------------------ validate

def test_the_validate_route_says_every_problem_tied_to_its_field_and_its_step(world):
    an = world.c["analyst"]
    body = {"effective_from": "2026-12-1", "source": {"ref": "", "url": "ftp://x", "verified": None},
            "versions": [{"kind": "adder", "key": "fca", "body": {"rate": "1.38", "billing_months": ["2026-13"]}},
                         {"kind": "tou", "key": "xwdisco", "body": {"seasons": [{"months": [12, 1, 2], "peak": ["17:00", "21:00"]},
                                                                                {"months": [3], "peak": ["18:00", "18:00"]}]}},
                         {"kind": "schedule", "key": "", "body": {"cards": {"B-3": {"fixed_basis": "per_kw_month", "fixed": 1250, "floor_share": 0.25,
                                                                                     "tou": True, "peak": 49.31}}}}]}
    r = an.post("/api/tariffs/versions/validate", json=body)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["ok"] is False
    assert [(p["version"], p["kind"], p["field"], p["step"]) for p in got["problems"]] == [
        (None, None, "effective_from", "A"), (None, None, "source.ref", "A"), (None, None, "source.url", "A"), (None, None, "source.verified", "A"),
        (0, "adder", "body.rate", "C"), (0, "adder", "body.billing_months", "C"),
        (1, "tou", "body.seasons.1.peak", "B"), (1, "tou", "body.seasons", "B"),
        # the schedule's one key is filled in; Mill One's connection is on B-2(b), which the schedule leaves without a card: approving it is refused
        (2, "schedule", "body.cards.B-3.off_peak", "B"), (2, "schedule", "body.cards", "B")]
    words = {p["field"]: p["message"] for p in got["problems"]}
    assert words["source.ref"] == "Give the source reference: the SRO, NEPRA decision or bill it was read from"
    assert words["body.seasons"] == ("A TOU calendar must give every month one season: April, May, June, July, August, September, October and November "
                                     "have none")
    assert words["body.cards"].startswith("The schedule has no rate card for B-2(b)")
    assert got["problems"][6]["path"] == ["body", "seasons", 1, "peak"] and got["problems"][2]["key"] is None
    # stores nothing
    assert an.get("/api/tariffs/drafts").json()["drafts"] == []


def test_a_draft_that_passes_is_set_beside_the_version_in_force_with_what_it_changes(world):
    r = world.c["analyst"].post("/api/tariffs/versions/validate", json=_draft(_fca()))
    got = r.json()
    assert got["ok"] is True and got["problems"] == [], got
    v = got["versions"][0]
    assert (v["kind"], v["key"], v["name"], v["effective_from"]) == ("adder", "fca", "FCA July 2026 (corrected)", "2026-09-01")
    assert (v["in_force"]["name"], v["in_force"]["effective_from"]) == ("FCA July 2026", "2026-09-01")
    assert [(c["field"], c["before"], c["after"], c["change_pct"]) for c in v["tariff_changes"]] == [
        ("body.name", "FCA July 2026", "FCA July 2026 (corrected)", None), ("body.rate", 2.0581, 2.5581, 24.3)]


def test_the_library_refuses_with_the_first_problem_its_editor_says(company):
    """The checks are one rule: `POST /api/tariffs/versions` (propose) and approval refuse with the first of the problems the validate route lists."""
    body = {"rate": 200, "billing_months": "monthly", "exempt": "lifeline"}
    found = tariffs.version_problems("adder", "fca", "2026-12-01", body, SRC)
    assert [p for p, _ in found] == [("body", "rate"), ("body", "billing_months"), ("body", "exempt")]
    with pytest.raises(ValueError) as exc:
        tariffs.propose("adder", "fca", "2026-12-01", body, SRC, "t")
    assert str(exc.value) == found[0][1] == "An adder needs a rate in Rs/kWh (a number)"
    assert tariffs.version_problems("fuel", "", "1 Dec", None, {})[:3] == [
        (("kind",), "Unknown tariff kind"), (("key",), "A version needs its key"), (("effective_from",), "The version's start must be a date written YYYY-MM-DD")]
    # a site override's calendar is checked as a calendar, under its own field
    got = tariffs.body_problems("override", "c1", {"calendar": {"seasons": [{"months": [1], "peak": ["17:00", "21:00"]}]}, "mdi_block": "rolling"})
    assert [(p, tariffs.step_of("override", p)) for p, _ in got] == [(("body", "calendar", "seasons"), "B"), (("body", "mdi_block"), "C")]


def test_checking_and_drafting_are_for_those_who_draft(world):
    for who in ("operator", "viewer"):
        c = world.c[who]
        assert c.post("/api/tariffs/versions/validate", json=_draft(_fca())).status_code == 403, who
        assert c.get("/api/tariffs/drafts").status_code == 403, who
        assert c.put("/api/tariffs/drafts/mine", json=_draft(_fca())).status_code == 403, who
    r = world.c["operator"].post("/api/tariffs/versions/validate", json=_draft(_fca()))
    assert r.json()["detail"] == "Checking a tariff draft is for an administrator or an analyst"
    key = world.key(["sites:read", "plans:read"])
    assert world.anon.get("/api/tariffs/drafts", headers=key).status_code == 403


# ------------------------------------------------------------------------------------------------ drafts

def test_a_draft_is_saved_as_its_author_types_seen_by_its_author_and_administrators_and_never_waiting(world):
    an, admin = world.c["analyst"], world.c["admin"]
    waiting = admin.get("/api/tariffs").json()["pending"]
    half = {"effective_from": "", "versions": [{"kind": "adder", "key": "fca", "body": {"rate": 1.38}}], "editor": {"step": "C", "started_from": "in_force"}}
    r = an.put("/api/tariffs/drafts/fca-dec", json=half)
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["id"], d["status"], d["name"], d["mine"], d["kinds"], d["effective_from"]) == ("fca-dec", "draft", "FCA", True, ["adder"], None)
    assert d["updated"].endswith("+05:00") and d["created"].endswith("+05:00") and d["author"]
    assert d["tariff_body"]["editor"] == {"step": "C", "started_from": "in_force"} and d["tariff_body"]["source"] == {"ref": "", "url": "", "verified": None}
    # typed on: the same id saves over it
    r = an.put("/api/tariffs/drafts/fca-dec", json=_draft(_fca(FCA_DEC), start="2026-12-01"))
    assert r.json()["name"] == "FCA October 2026" and r.json()["effective_from"] == "2026-12-01"
    assert [x["id"] for x in an.get("/api/tariffs/drafts").json()["drafts"]] == ["fca-dec"]
    # an administrator sees everyone's, says whose, and may not write over another person's
    listed = admin.get("/api/tariffs/drafts").json()["drafts"]
    assert [(x["id"], x["mine"], x["author"]) for x in listed] == [("fca-dec", False, d["author"])]
    assert admin.get("/api/tariffs/drafts/fca-dec").json()["tariff_body"]["versions"][0]["body"]["rate"] == 1.38
    r = admin.put("/api/tariffs/drafts/fca-dec", json=_draft(_fca()))
    assert r.status_code == 409 and r.json()["code"] == "draft_taken", r.text
    # an administrator's own draft is not the analyst's to read
    assert admin.put("/api/tariffs/drafts/admins", json=_draft(_fca())).status_code == 200
    assert an.get("/api/tariffs/drafts/admins").status_code == 404
    assert [x["id"] for x in an.get("/api/tariffs/drafts").json()["drafts"]] == ["fca-dec"]
    # never waiting
    assert admin.get("/api/tariffs").json()["pending"] == waiting
    # a strict body, refused in one sentence; an id of the editor's own form
    r = an.put("/api/tariffs/drafts/x", json={"colour": "red"})
    assert r.status_code == 422 and r.json()["detail"] == "Unknown field 'colour'"
    assert an.put("/api/tariffs/drafts/x", json={"versions": [{"kind": "fuel"}]}).status_code == 422
    assert an.put("/api/tariffs/drafts/x", json={"versions": [{"kind": "adder"}] * 8}).status_code == 422
    r = an.put("/api/tariffs/drafts/a%20b", json=half)
    assert r.status_code == 422 and "letters, digits" in r.json()["detail"]
    r = an.put("/api/tariffs/drafts/big", json={**half, "editor": {"notes": "x" * 210_000}})
    assert r.status_code == 422 and r.json()["detail"] == "A draft holds at most 200 KB"
    # discarded by an administrator: audited, and gone
    r = admin.delete("/api/tariffs/drafts/fca-dec")
    assert r.status_code == 200, r.text
    line = next(a for a in _actions(world) if a["action"] == "discarded a tariff draft")
    assert line["target"] == "fca-dec" and line["detail"]["name"] == "FCA October 2026" and line["detail"]["mine"] is False
    assert an.get("/api/tariffs/drafts/fca-dec").status_code == 404 and an.delete("/api/tariffs/drafts/fca-dec").status_code == 404


def test_a_draft_submitted_waits_for_approval_and_a_second_person_approves_it(world):
    """The owner's question Q65 (recommended): "What changes" first, and a second person approves. Nothing publishes in one step: an administrator's
    own version waits for another administrator."""
    an, admin = world.c["analyst"], world.c["admin"]
    second = identities.client("second-admin@fix.test", "admin", world.company, "member", raise_server_exceptions=False)
    an.put("/api/tariffs/drafts/d1", json=_draft(_fca({"rate": 1.38}), start="2026-12-01", ref=""))
    r = an.post("/api/tariffs/drafts/d1/submit")
    assert r.status_code == 422, r.text
    assert [p["field"] for p in r.json()["problems"]] == ["source.ref", "body.billing_months"]
    assert r.json()["detail"] == "2 things to fix first: Give the source reference: the SRO, NEPRA decision or bill it was read from"
    two = _fca(FCA_DEC) + [{"kind": "adder", "key": "nj", "body": {"rate": 0.1, "billing_months": "all", "exempt": ["icp_units"]}}]
    assert an.put("/api/tariffs/drafts/d1", json=_draft(two, start="2026-12-01")).status_code == 200
    # submitted by its author only
    r = admin.post("/api/tariffs/drafts/d1/submit")
    assert r.status_code == 403 and r.json()["code"] == "not_author", r.text
    waiting = admin.get("/api/tariffs").json()["pending"]
    r = an.post("/api/tariffs/drafts/d1/submit")
    assert r.status_code == 200, r.text
    got = r.json()
    assert [(v["kind"], v["key"], v["status"], v["effective_from"], v["mine"]) for v in got["submitted"]] == [
        ("adder", "fca", "pending", "2026-12-01", True), ("adder", "nj", "pending", "2026-12-01", True)]
    assert got["submitted"][0]["source"]["kind"] == "manual" and got["submitted"][0]["source"]["ref"] == "NEPRA decision of 4 Sep 2026"
    assert "by_id" not in got["submitted"][0] and got["submitted"][0]["template"] is False and got["submitted"][0]["withdrawn"] is None
    assert got["pending"] == waiting + 2
    assert got["message"] == "Submitted: FCA October 2026 and NJ, from 1 Dec 2026. They wait for an administrator's approval."
    assert an.get("/api/tariffs/drafts/d1").status_code == 404
    assert [a["action"] for a in _actions(world)][:2] == ["proposed a tariff version"] * 2
    # the analyst's version: any administrator approves it
    r = admin.post(f"/api/tariffs/versions/{got['submitted'][0]['id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["mine"] is False, r.text
    # an administrator's own version: refused to its author, approved by a second administrator
    admin.put("/api/tariffs/drafts/d2", json=_draft(_fca({**FCA_DEC, "name": "FCA November 2026", "billing_months": ["2027-01"]}), start="2027-01-01"))
    mine = admin.post("/api/tariffs/drafts/d2/submit").json()["submitted"][0]
    assert mine["mine"] is True
    r = admin.post(f"/api/tariffs/versions/{mine['id']}/approve")
    assert r.status_code == 403 and r.json()["code"] == "own_version", r.text
    assert r.json()["detail"] == "A second person approves a tariff version: you drafted this one, so another administrator approves it"
    raw = admin.post("/api/tariffs/versions", json={"kind": "adder", "key": "fpa_x", "effective_from": "2027-01-01",
                                                    "body": {"rate": 0.2, "billing_months": ["2027-01"]}, "source": SRC}).json()
    assert raw["mine"] is True and admin.post(f"/api/tariffs/versions/{raw['id']}/approve").json()["code"] == "own_version"
    r = second.post(f"/api/tariffs/versions/{mine['id']}/approve")
    assert r.status_code == 200 and r.json()["status"] == "active", r.text
    # a version with no author's id (the build's, a watcher's, a row of before) is approved by any administrator
    shipped = next(v for v in admin.get("/api/tariffs/versions?kind=adder").json() if v["status"] == "pending" and v["by"] == "shipped")
    assert admin.post(f"/api/tariffs/versions/{shipped['id']}/approve").status_code == 200


# ------------------------------------------------------------------------------------------------ withdraw

def test_a_waiting_version_is_withdrawn_by_its_author_or_an_administrator(world):
    an, admin, op = world.c["analyst"], world.c["admin"], world.c["operator"]

    def propose(c, key):
        r = c.post("/api/tariffs/versions", json={"kind": "adder", "key": key, "effective_from": "2026-12-01",
                                                  "body": {"name": f"{key} for December", "rate": 0.4, "billing_months": ["2026-12"]}, "source": SRC})
        assert r.status_code == 200, r.text
        return r.json()

    v = propose(an, "fca")
    waiting = admin.get("/api/tariffs").json()["pending"]
    for c in (op, world.c["viewer"]):
        assert c.post(f"/api/tariffs/versions/{v['id']}/withdraw", json={}).status_code == 403
    r = an.post(f"/api/tariffs/versions/{v['id']}/withdraw", json={"reason": "The rate is from the wrong month"})
    assert r.status_code == 200, r.text
    w = r.json()
    assert w["status"] == "withdrawn" and w["draft_id"] is None
    assert w["withdrawn"]["by"] == v["by"] and w["withdrawn"]["reason"] == "The rate is from the wrong month" and w["withdrawn"]["at"].endswith("+05:00")
    assert admin.get("/api/tariffs").json()["pending"] == waiting - 1
    row = next(x for x in admin.get("/api/tariffs/versions?kind=adder").json() if x["id"] == v["id"])
    assert row["status"] == "withdrawn" and row["withdrawn"] == w["withdrawn"]
    assert admin.post(f"/api/tariffs/versions/{v['id']}/approve").json()["detail"] == "Only a pending version can be approved"
    r = an.post(f"/api/tariffs/versions/{v['id']}/withdraw", json={})
    assert r.status_code == 422 and r.json()["detail"] == "Only a version waiting for approval can be withdrawn"
    line = next(a for a in _actions(world) if a["action"] == "withdrew a tariff version")
    assert line["detail"] == {"version_id": v["id"], "effective_from": "2026-12-01", "reason": "The rate is from the wrong month"}
    # another person's version: not the analyst's to withdraw
    theirs = propose(admin, "qta")
    r = an.post(f"/api/tariffs/versions/{theirs['id']}/withdraw", json={})
    assert r.status_code == 403 and r.json()["code"] == "not_author", r.text
    # sent back by an administrator: withdrawn, and a draft of it for its author
    back = propose(an, "dss")
    r = admin.post(f"/api/tariffs/versions/{back['id']}/withdraw", json={"reason": "Use the notified rate", "redraft": True})
    assert r.status_code == 200 and r.json()["draft_id"], r.text
    d = an.get(f"/api/tariffs/drafts/{r.json()['draft_id']}").json()
    assert d["mine"] is True and d["author"] == back["by"] and d["effective_from"] == "2026-12-01"
    assert d["tariff_body"]["versions"] == [{"kind": "adder", "key": "dss", "body": back["body"]}] and d["tariff_body"]["source"]["ref"] == "test"
    # an approved version is not withdrawn
    active = next(x for x in admin.get("/api/tariffs/versions?kind=adder").json() if x["status"] == "active")
    assert admin.post(f"/api/tariffs/versions/{active['id']}/withdraw", json={}).status_code == 422
    assert admin.post("/api/tariffs/versions/no-such/withdraw", json={}).status_code == 404
    assert an.post(f"/api/tariffs/versions/{theirs['id']}/withdraw", json={"reason": "x" * 301}).status_code == 422


# ------------------------------------------------------------------------------------------------ preview and simulate (goldens)

def test_golden_a_drafts_week_of_prices_beside_the_version_in_force(world, monkeypatch):
    """A-3 general services, a flat card, FESCO (Punjab), STRN and ATL (GST and s.235 recovered: the bearable basis carries the duty alone), the meter
    read on the 25th. 1 to 7 Sep 2026 are on September's bill: FCA July 2026 2.0581, QTA Q2 CY2026 0.5194, DSS 3.23.

    In force:  (42.48 + 2.0581 + 0.5194 + 3.23) x 1.015 = 48.2875 x 1.015 = 49.0118 -> Rs 49.01/kWh every step
    The draft: FCA corrected to 2.5581: 48.7875 x 1.015 = 49.5193 -> Rs 49.52/kWh
    Change: 0.51 / 49.01 = 1.04 % -> 1.0 %. No export regime: export earns nothing either way."""
    an = world.c["analyst"]
    monkeypatch.setattr(plans, "tick", lambda *a, **k: None)          # the approval below re-solves the active sites: not this test's subject
    with world.use():
        sid = make_site(name="Preview A3", conn=A3, battery=False, genset=False)
    r = an.post("/api/tariffs/preview", json={"site_id": sid, "draft": _draft(_fca())})
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["start"], p["days"], p["step_minutes"], p["tariff_category"]) == ("2026-09-01", 7, 15, "A-3")
    assert len(p["times"]) == 672 and p["times"][0] == "2026-09-01T00:00:00+05:00" and p["times"][-1] == "2026-09-07T23:45:00+05:00"
    assert set(p["in_force"]["c_imp"]) == {49.01} and set(p["draft"]["c_imp"]) == {49.52}
    assert set(p["in_force"]["c_exp"]) == set(p["draft"]["c_exp"]) == {0.0} and not any(p["draft"]["peak"])
    assert p["rates"] == [{"what": "off_peak_import", "words": "Import", "in_force_rs_per_kwh": 49.01, "draft_rs_per_kwh": 49.52, "change_pct": 1.0}]
    assert p["changed_steps"] == 672 and p["peak_moved_steps"] == 0
    assert p["what_if"] == {"source": "draft", "id": None, "name": "FCA July 2026 (corrected)"}
    assert [s["name"] for s in p["draft"]["schedules"]] == ["Uniform schedule CY2026 (SRO 279(I)/2026)"]
    # what the preview prices as if approved is what approving it prices: submitted, approved by an administrator, the day's own prices
    an.put("/api/tariffs/drafts/sep", json=_draft(_fca()))
    vid = an.post("/api/tariffs/drafts/sep/submit").json()["submitted"][0]["id"]
    pending = an.post("/api/tariffs/preview", json={"site_id": sid, "version_id": vid, "days": 1, "start": "2026-09-02"}).json()
    assert set(pending["draft"]["c_imp"]) == {49.52} and pending["what_if"]["source"] == "version_id"
    assert world.c["admin"].post(f"/api/tariffs/versions/{vid}/approve").status_code == 200
    day = world.c["admin"].get(f"/api/sites/{sid}/prices?day=2026-09-02").json()
    assert set(day["c_imp"]) == {49.52} == set(pending["draft"]["c_imp"])


def test_golden_a_bill_rebuilt_under_the_version_in_force_and_under_a_draft(world):
    """The same A-3 site, a steady 30 kW from 26 Aug to the 25 Sep reading: 31 days, 22,320 kWh, September's bill (a per-consumer fixed charge of
    Rs 1,000, 31 days being within 4 of 30: not prorated).

                          In force (FCA 2.0581)                        The draft (FCA 2.5581)
    Energy and adders     22,320 x 48.2875 = 1,077,777.00              22,320 x 48.7875 = 1,088,937.00
    Fixed charges         1,000.00                                     1,000.00
    Electricity duty      1.5 % = 16,166.655                           1.5 % = 16,334.055
    GST 18 %              x 1,094,943.655 = 197,089.858                x 1,106,271.055 = 199,128.790
    s.235 (commercial)    1,950 + 12 % x (1,292,033.513 - 20,000)      1,950 + 12 % x (1,305,399.845 - 20,000)
                          = 154,594.022                                = 156,197.981
    PTV fee               60.00                                        60.00
    Total                 1,446,687.53                                 1,461,657.83   (+14,970.30, +1.0 %)"""
    an = world.c["analyst"]
    with world.use():
        sid = make_site(name="Simulate A3", conn=A3, battery=False, genset=False)
        _flat_rows(sid, date(2026, 8, 26), 31, 30.0)
        bill = verify.add_bill(sid, {"period_start": "2026-08-26", "period_end": "2026-09-25", "total_rs": 1450000.0}, "test")
    stored = bill["rebuilt"]["total_rs"]
    assert stored == pytest.approx(1446687.53, abs=0.01)
    r = an.post("/api/tariffs/simulate", json={"site_id": sid, "bill_id": bill["id"], "draft": _draft(_fca())})
    assert r.status_code == 200, r.text
    s = r.json()
    b = s["bill"]
    # the version in force's figure is the stored rebuild's: the Bills page's own rebuild of the bill
    assert b["in_force_rs"] == stored and b["draft_rs"] == pytest.approx(1461657.83, abs=0.01) and b["printed_rs"] == 1450000.0
    assert b["difference_rs"] == pytest.approx(14970.30, abs=0.01) and b["change_pct"] == 1.0
    assert (s["bill_id"], s["billing_month"], s["first_day"], s["last_day"], s["days"], s["completeness"], s["estimated"], s["reaches"]) == (
        bill["id"], "2026-09", "2026-08-26", "2026-09-25", 31, 1.0, False, True)
    fca = {ln["line"]: ln["amount_rs"] for ln in b["draft_lines"] if ln["line"].startswith("FCA")}
    assert fca == {"FCA July 2026 (corrected)": pytest.approx(57096.79, abs=0.01)}
    assert {ln["line"]: ln["amount_rs"] for ln in b["in_force_lines"] if ln["line"].startswith("FCA")} == {"FCA July 2026": pytest.approx(45936.79, abs=0.01)}
    # a billing month where no bill is entered: the same period, the same figures, nothing printed
    m = an.post("/api/tariffs/simulate", json={"site_id": sid, "month": "2026-09", "draft": _draft(_fca())}).json()
    assert (m["bill"]["in_force_rs"], m["bill"]["draft_rs"], m["bill"]["printed_rs"], m["bill_id"]) == (b["in_force_rs"], b["draft_rs"], None, None)
    # a draft that prices none of the bill's days changes nothing, and says so
    ke = [{"kind": "tou", "key": "ke", "body": {"seasons": [{"months": list(range(1, 13)), "peak": ["17:00", "21:00"]}]}}]
    other = an.post("/api/tariffs/simulate", json={"site_id": sid, "bill_id": bill["id"], "draft": _draft(ke)}).json()
    assert other["reaches"] is False and other["bill"]["draft_rs"] == other["bill"]["in_force_rs"] and other["bill"]["change_pct"] == 0.0
    # refusals: a month not read yet, a month with no readings, a bill of another site, a viewer
    today = datetime.now(PKT).date()
    r = an.post("/api/tariffs/simulate", json={"site_id": sid, "month": f"{today.replace(day=1) + timedelta(days=40):%Y-%m}", "draft": _draft(_fca())})
    assert r.status_code == 422 and "is not read yet" in r.json()["detail"], r.text
    r = an.post("/api/tariffs/simulate", json={"site_id": sid, "month": "2026-07", "draft": _draft(_fca())})
    assert r.status_code == 422 and "no grid readings" in r.json()["detail"], r.text
    assert an.post("/api/tariffs/simulate", json={"site_id": world.site, "bill_id": bill["id"], "draft": _draft(_fca())}).status_code == 404
    assert an.post("/api/tariffs/simulate", json={"site_id": sid, "draft": _draft(_fca())}).status_code == 422
    assert world.c["viewer"].post("/api/tariffs/simulate", json={"site_id": sid, "bill_id": bill["id"], "draft": _draft(_fca())}).status_code == 403


def test_previews_are_for_see_costs_a_draft_for_those_who_draft_and_a_site_of_another_company_is_none(world):
    an, op = world.c["analyst"], world.c["operator"]
    with world.use():
        sid = make_site(name="Roles A3", conn=A3, battery=False, genset=False)
        shipped = next(v for v in tariffs.versions("adder") if v["status"] == "pending" and v["by"] == "shipped")
    # an operator reads a waiting version's prices (Versions' drawer), but prices no draft
    r = op.post("/api/tariffs/preview", json={"site_id": sid, "version_id": shipped["id"], "start": "2026-11-10", "days": 1})
    assert r.status_code == 200 and set(r.json()["draft"]["c_imp"]) != set(r.json()["in_force"]["c_imp"]), r.text
    assert op.post("/api/tariffs/preview", json={"site_id": sid, "draft": _draft(_fca())}).status_code == 403
    assert world.c["viewer"].post("/api/tariffs/preview", json={"site_id": sid, "version_id": shipped["id"]}).status_code == 403
    key = world.key(["sites:read", "plans:read"], site=sid)
    assert world.anon.post("/api/tariffs/preview", headers=key, json={"site_id": sid, "version_id": shipped["id"]}).status_code == 403
    # one of draft, draft_id and version_id; a draft with anything to fix is not priced
    r = an.post("/api/tariffs/preview", json={"site_id": sid})
    assert r.status_code == 422 and "Send one of draft" in r.json()["detail"]
    r = an.post("/api/tariffs/preview", json={"site_id": sid, "draft": _draft(_fca(), ref="")})
    assert r.status_code == 422 and r.json()["problems"][0]["field"] == "source.ref"
    active = next(v for v in an.get("/api/tariffs/versions?kind=adder").json() if v["status"] == "active")
    assert an.post("/api/tariffs/preview", json={"site_id": sid, "version_id": active["id"]}).status_code == 422
    # a saved draft is priced by its id, its author's or an administrator's
    an.put("/api/tariffs/drafts/saved", json=_draft(_fca()))
    assert world.c["admin"].post("/api/tariffs/preview", json={"site_id": sid, "draft_id": "saved", "days": 1}).json()["what_if"] == {
        "source": "draft_id", "id": "saved", "name": "FCA July 2026 (corrected)"}
    # another company's site is no site here
    other = identities.company("Another company", own_file=True)
    from gridlytics import platform as platformdb
    with store.use_company(platformdb.company_file(other)):
        theirs = sites.create({"name": "Their Mill", "disco": "LESCO"}, "t")["id"]
        sites.put_connection(theirs, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "t")
    r = an.post("/api/tariffs/preview", json={"site_id": theirs, "draft": _draft(_fca())})
    assert r.status_code == 404, r.text
    # an off-grid site has no tariff to price
    with world.use():
        conn = sites.connections(world.other)
    assert conn == [] and an.post("/api/tariffs/preview", json={"site_id": world.other, "draft": _draft(_fca())}).status_code == 422


def test_previews_carry_their_rupees_under_money_names(world):
    """Rates are money (R8 2.3): every route of the editor is refused to a reader without see_costs, and its rupee figures carry money names all the
    same, so the API's filter would take every one out (money.for_key keeps no number of them)."""
    an = world.c["analyst"]
    with world.use():
        sid = make_site(name="Names A3", conn=A3, battery=False, genset=False)
        _flat_rows(sid, date(2026, 8, 26), 31, 30.0)
    p = an.post("/api/tariffs/preview", json={"site_id": sid, "draft": _draft(_fca()), "days": 1}).json()
    s = an.post("/api/tariffs/simulate", json={"site_id": sid, "month": "2026-09", "draft": _draft(_fca())}).json()
    assert {"c_imp", "c_exp", "rates", "bill"} <= {k for k in [*p["in_force"], *p, *s] if money.is_money(k)}
    for answer in (p, s):
        kept = money.for_key(answer)
        numbers = json.dumps(kept)
        assert "49.01" not in numbers and "49.52" not in numbers and "1446687" not in numbers, kept


# ------------------------------------------------------------------------------------------------ the template flag and the agreement's end

def test_shipped_versions_are_marked_templates_and_a_version_the_company_drafted_is_not(world):
    admin = world.c["admin"]
    got = admin.get("/api/tariffs?on=2026-11-13").json()
    assert got["schedule"]["template"] is True and got["schedule"]["status"] == "active"
    assert all(v["template"] is True for v in got["versions"]) and all("by_id" not in v for v in got["versions"])
    v = world.c["analyst"].post("/api/tariffs/versions", json={"kind": "adder", "key": "fca", "effective_from": "2026-12-01",
                                                                "body": {"rate": 1.38, "billing_months": ["2026-12"]}, "source": SRC}).json()
    assert v["template"] is False
    rows = {x["id"]: x for x in admin.get("/api/tariffs").json()["versions"]}
    assert rows[v["id"]]["template"] is False and rows[v["id"]]["mine"] is False and rows[v["id"]]["withdrawn"] is None
    assert {x["id"]: x["template"] for x in admin.get("/api/tariffs/versions").json()}[v["id"]] is False


def test_an_agreement_ending_within_the_alert_window_says_in_how_many_days_as_the_alert_does(world):
    """R8 2.3: the Tariff alert regime_expiring_<connection> is raised 60 days before an export agreement ends (the site's rules' agreement_days); the
    In force view's agreements say "Ends in n days" (`ends_in_days`) over the same window, from the day shown."""
    admin, sid = world.c["admin"], world.site
    today = datetime.now(PKT).date()
    now = int(datetime.now(timezone.utc).timestamp())
    with world.use():
        conn = sites.connections(sid)[0]

    def agree(end: date):
        with world.use():
            sites.put_connection(sid, {**conn["data"], "regime": "net_billing", "agreement_start": "2024-01-01", "agreement_end": end.isoformat()}, "test",
                                 conn_id=conn["id"])
            alerts.evaluate(sid, now)
            raised = any(a["rule"] == f"regime_expiring_{conn['id']}" for a in alerts.listing("active", sid))
        row = next(a for a in admin.get("/api/tariffs").json()["agreements"] if a["connection_id"] == conn["id"])
        return row["state"], row["ends_in_days"], raised

    assert agree(today + timedelta(days=45)) == ("active", 45, True)
    assert agree(today + timedelta(days=60)) == ("active", 60, True)
    assert agree(today + timedelta(days=61)) == ("active", None, False)
    assert agree(today) == ("active", 0, True)
    assert agree(today - timedelta(days=1))[:2] == ("ended", None)
    # the site's own window, as its rules set it
    r = admin.put(f"/api/alerts/rules/sites/{sid}", json={"Tariff": {"thresholds": {"agreement_days": 90}}})
    assert r.status_code == 200, r.text
    assert agree(today + timedelta(days=61)) == ("active", 61, True)
    # another day shown: from that day
    row = next(a for a in admin.get(f"/api/tariffs?on={(today + timedelta(days=30)).isoformat()}").json()["agreements"]
               if a["connection_id"] == conn["id"])
    assert row["ends_in_days"] == 31


# ------------------------------------------------------------------------------------------------ the schema

def test_the_drafts_and_the_authors_are_a_migration_with_its_copy_made_first(tmp_path, monkeypatch):
    """Schema 8 of a company's file: a file of schema 7 holding a waiting version is copied to backup/ before the drafts table and the author's and
    withdrawal columns are made; the version keeps no author's id, so any administrator approves it."""
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    path = tmp_path / "old.db"
    with monkeypatch.context() as m:
        m.setattr(store, "_V8", [])
        m.setattr(store, "_V8_COLUMNS", ())
        with closing(sqlite3.connect(path)) as con, con:
            store._migrate(con, 0)
            con.execute("PRAGMA user_version = 7")
            con.execute("INSERT INTO tariff_versions (id, kind, key, effective_from, effective_to, known_from, status, supersedes, body, source, hash, "
                        "created, by) VALUES ('old1','adder','nj','2026-12-01',NULL,'2026-11-01','pending',NULL,?,?,'h','2026-11-01T00:00:00+00:00',"
                        "'Ayesha')", (json.dumps({"rate": 0.1, "billing_months": "all"}), json.dumps(SRC)))
    assert not list((tmp_path / "backup").glob("*.db"))
    with store.use_company(path):
        old = next(v for v in tariffs.versions("adder") if v["id"] == "old1")
        approved = tariffs.approve("old1", "Bilal", "some-admin-id")
    assert (old["by"], old["by_id"], old["withdrawn_at"], old["withdrawn_by"], old["withdrawn_note"]) == ("Ayesha", "", None, "", "")
    assert approved["status"] == "active"
    with closing(sqlite3.connect(path)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION == 8
        assert con.execute("SELECT COUNT(*) FROM tariff_drafts").fetchone()[0] == 0
    copies = list((tmp_path / "backup").glob("old-*-schema-v7.db"))
    assert len(copies) == 1
    with closing(sqlite3.connect(copies[0])) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 7
        assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'tariff_drafts'").fetchone()[0] == 0
        assert con.execute("SELECT status FROM tariff_versions WHERE id = 'old1'").fetchone()[0] == "pending"


def test_a_what_if_leaves_the_library_as_it_was(company):
    """Pricing as if a version were approved writes nothing, and outside the block every lookup reads the library as it stands."""
    tariffs.ensure_shipped()
    before = [(v["id"], v["status"], v["effective_to"]) for v in tariffs.versions()]
    v = {"id": "draft:1", "kind": "adder", "key": "fca", "effective_from": "2026-09-01", "effective_to": None, "status": "pending", "body": FCA_SEP,
         "source": SRC, "hash": tariffs._hash(FCA_SEP), "created": "", "by": ""}
    with tariffs.what_if([v]):
        assert [(a["key"], a["rate"]) for a in tariffs.adders_for("2026-09")][0] == ("fca", 2.5581)
        # a correction for the same start: the version it corrects is in force on no day; the next start closes it
        assert tariffs.resolve("adder", "fca", "2026-09-15")["id"] == "draft:1"
        assert tariffs.resolve("adder", "fca", "2026-10-15")["id"] != "draft:1"
    assert [(a["key"], a["rate"]) for a in tariffs.adders_for("2026-09")][0] == ("fca", 2.0581)
    assert [(v["id"], v["status"], v["effective_to"]) for v in tariffs.versions()] == before
    # a placeholder awaiting the notification a schedule answers no longer waits, and the schedule prices from its start
    cy27 = {**tariffs.resolve("schedule", "national", "2026-12-31"), "id": "draft:2", "effective_from": "2027-01-01", "status": "pending"}
    assert [n["kind"] for n in tariffs.awaiting_notices("2027-01-05")] == ["schedule"]
    with tariffs.what_if([cy27]):
        assert tariffs.awaiting_notices("2027-01-05") == [] and tariffs.resolve("schedule", "national", "2027-01-05")["id"] == "draft:2"
        assert tariffs.resolve("schedule", "national", "2026-12-31")["effective_to"] == "2026-12-31"
    assert [n["kind"] for n in tariffs.awaiting_notices("2027-01-05")] == ["schedule"]
