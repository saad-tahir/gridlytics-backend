"""UX backend ask K32, the batch's check (plan H11; F11 item 6, F2.2, F2.5, H6.8): the checkers' findings on the tariff editor's backend, each held by a
test that fails without its fix.

- A draft is claimed in the transaction that makes its versions: a second submit of the same draft (a double click, a retry) makes nothing, and a draft
  changed after it was checked is not submitted as it was.
- A version the company drafted and withdrew does not stop a newer build's shipped version at its start reaching the company (the seeding reads it as
  no decision); a shipped version the company withdrew stays withdrawn.
- The editor's rupee figures carry money names: what a version changes (`tariff_changes`) and a draft's body (`tariff_body`).
- An agreement that has not started yet but ends within the alert's window says in how many days, as the alert does.
- A simulated entered bill carries the stored rebuild beside the version in force now (`stored_rs`).
- The editor's field words are in its own units and words (a floor and the taxes are percents); the raw route keeps its own.
- Sending a version back (`redraft`) is an administrator's, made in the withdrawal's transaction and audited with the draft it made.
- The draft id another person holds is refused without saying whose; another person's draft is no draft of an analyst's on every route.
- A version row's times are Karachi time; the strict models' refusals are plain sentences."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_fix_r2_savings import _flat_rows
from test_fix_savings import make_site
from test_fix_tariffs import SRC, company  # noqa: F401  (company is a fixture)
from test_ux_k32_tariff_editor import A3, FCA_DEC, FCA_SEP, _actions, _draft, _fca

from gridlytics import alerts, money, plans, routes_tariffs, sites, store, tariff_data, tariff_drafts, tariffs, verify

PKT = timezone(timedelta(hours=5))


def _numbers(obj) -> list:
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _numbers(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _numbers(v)]
    return [obj] if isinstance(obj, (int, float)) and not isinstance(obj, bool) else []


def _pending(w) -> int:
    return w.c["admin"].get("/api/tariffs").json()["pending"]


# ------------------------------------------------------------------------------------------------ a draft submitted once

def test_a_draft_submitted_twice_makes_its_versions_once(world, monkeypatch):
    """Two submits of one draft (a double click, a retry) each read the draft before either removed it: the second finds it gone in the transaction
    that would make its versions, and makes none (404, as a read of it now gives)."""
    an = world.c["analyst"]
    assert an.put("/api/tariffs/drafts/race", json=_draft(_fca(FCA_DEC), start="2026-12-01")).status_code == 200
    real, seen = routes_tariffs._draft_or_404, {}

    def read_before_either_submit(draft_id):
        if draft_id not in seen:
            seen[draft_id] = real(draft_id)
        return seen[draft_id]

    monkeypatch.setattr(routes_tariffs, "_draft_or_404", read_before_either_submit)
    before = _pending(world)
    first = an.post("/api/tariffs/drafts/race/submit")
    assert first.status_code == 200, first.text
    second = an.post("/api/tariffs/drafts/race/submit")
    assert second.status_code == 404 and second.json()["code"] == "not_found", second.text
    assert _pending(world) == before + 1
    assert [a["action"] for a in _actions(world)].count("proposed a tariff version") == 1


def test_a_draft_changed_after_it_was_checked_is_not_submitted_as_it_was(world, monkeypatch):
    """The author types on while a submit is under way: the versions made are the draft that was checked, or none. The submit that read the older
    draft is refused (409), and the newer draft is kept, not removed under it."""
    an = world.c["analyst"]
    assert an.put("/api/tariffs/drafts/typed", json=_draft(_fca(FCA_DEC), start="2026-12-01")).status_code == 200
    real, stale = routes_tariffs._draft_or_404, {}
    monkeypatch.setattr(routes_tariffs, "_draft_or_404", lambda i: stale.setdefault(i, real(i)))
    assert an.get("/api/tariffs/drafts/typed").status_code == 200       # the read the submit is about to use
    assert an.put("/api/tariffs/drafts/typed", json=_draft(_fca({**FCA_DEC, "rate": 1.45}), start="2026-12-01")).status_code == 200
    before = _pending(world)
    r = an.post("/api/tariffs/drafts/typed/submit")
    assert r.status_code == 409 and r.json()["code"] == "draft_changed", r.text
    assert r.json()["detail"] == "The draft changed while it was being submitted: check it and submit it again"
    assert _pending(world) == before
    monkeypatch.setattr(routes_tariffs, "_draft_or_404", real)
    assert an.get("/api/tariffs/drafts/typed").json()["tariff_body"]["versions"][0]["body"]["rate"] == 1.45


# ------------------------------------------------------------------------------------------------ the shipped library and a withdrawn version

NEWER_FCA_DEC = {"kind": "adder", "key": "fca", "effective_from": "2026-12-01", "known_from": "2026-12-05", "status": "active",
                 "body": {"name": "FCA October 2026 (shipped by a newer build)", "rate": 1.38, "billing_months": ["2026-12"]},
                 "source": {"kind": "nepra_decision", "ref": "newer build", "url": "", "retrieved_on": "2026-12-05", "verified": "primary"}}


def test_a_version_the_company_withdrew_does_not_stop_a_newer_builds_shipped_version_at_its_start(company, monkeypatch):
    tariffs.ensure_shipped()
    v = tariffs.propose("adder", "fca", "2026-12-01", {"name": "FCA Dec draft", "rate": 9.99, "billing_months": ["2026-12"]}, SRC, "Ayesha", "ayesha-id")
    tariffs.withdraw(v["id"], "Ayesha", "ayesha-id", "wrong figure")
    monkeypatch.setattr(tariff_data, "SHIPPED", [*tariff_data.SHIPPED, NEWER_FCA_DEC])
    tariffs.ensure_shipped()
    got = sorted((x["by"], x["status"], x["body"]["rate"]) for x in tariffs.versions("adder") if x["key"] == "fca" and x["effective_from"] == "2026-12-01")
    assert got == [("Ayesha", "withdrawn", 9.99), ("shipped", "active", 1.38)]
    assert [(a["key"], a["rate"]) for a in tariffs.adders_for("2026-12") if a["key"] == "fca"] == [("fca", 1.38)]


def test_a_shipped_version_the_company_withdrew_stays_withdrawn_and_a_correction_of_it_is_not_added(company, monkeypatch):
    """The company's withdrawal of a shipped waiting version is its decision: a newer build seeds the version again neither as it was nor corrected."""
    tariffs.ensure_shipped()
    shipped = next(v for v in tariffs.versions("adder") if v["status"] == "pending" and v["by"] == "shipped")
    tariffs.withdraw(shipped["id"], "Bilal", "admin-id", "not notified", administrator=True)
    tariffs._SEEDED.clear()
    tariffs.ensure_shipped()
    entry = next(v for v in tariff_data.SHIPPED if (v["kind"], v["key"], v["effective_from"]) == (shipped["kind"], shipped["key"],
                                                                                                   shipped["effective_from"]))
    corrected = {**entry, "body": {**entry["body"], "rate": entry["body"]["rate"] + 0.5}}
    monkeypatch.setattr(tariff_data, "SHIPPED", [corrected if v is entry else v for v in tariff_data.SHIPPED])
    tariffs.ensure_shipped()
    same = [(v["status"], v["by"]) for v in tariffs.versions("adder") if (v["key"], v["effective_from"]) == (shipped["key"], shipped["effective_from"])]
    assert same == [("withdrawn", "shipped")]


# ------------------------------------------------------------------------------------------------ money names

def test_what_a_version_changes_and_a_drafts_body_carry_money_names(world):
    """Rates are money (R8 2.3): the validate answer's field changes (`tariff_changes`) and a draft's body (`tariff_body`, its rates under any key,
    such as an adder's rate by category) are under money names, so the filter leaves a reader without see_costs none of their figures."""
    an = world.c["analyst"]
    got = an.post("/api/tariffs/versions/validate", json=_draft(_fca())).json()
    v = got["versions"][0]
    assert "changes" not in v and [(c["field"], c["before"], c["after"], c["change_pct"]) for c in v["tariff_changes"]] == [
        ("body.name", "FCA July 2026", "FCA July 2026 (corrected)", None), ("body.rate", 2.0581, 2.5581, 24.3)]
    assert money.is_money("tariff_changes") and _numbers(money.for_key(got)) == []
    body = _draft([{"kind": "adder", "key": "fca", "body": {**FCA_SEP, "category_rates": {"agricultural": 0.77}}}])
    saved = an.put("/api/tariffs/drafts/named", json=body).json()
    assert "body" not in saved and saved["tariff_body"]["versions"][0]["body"]["category_rates"] == {"agricultural": 0.77}
    for answer in (saved, an.get("/api/tariffs/drafts/named").json(), an.get("/api/tariffs/drafts").json()):
        assert _numbers(money.for_key(answer)) == [], answer


# ------------------------------------------------------------------------------------------------ the agreement's end

def test_an_agreement_not_started_but_ending_within_the_window_says_so_as_the_alert_does(world):
    admin, sid = world.c["admin"], world.site
    today = datetime.now(PKT).date()
    now = int(datetime.now(timezone.utc).timestamp())
    with world.use():
        conn = sites.connections(sid)[0]
        sites.put_connection(sid, {**conn["data"], "regime": "net_billing", "agreement_start": (today + timedelta(days=10)).isoformat(),
                                   "agreement_end": (today + timedelta(days=40)).isoformat()}, "test", conn_id=conn["id"])
        alerts.evaluate(sid, now)
        raised = any(a["rule"] == f"regime_expiring_{conn['id']}" for a in alerts.listing("active", sid))
    row = next(a for a in admin.get("/api/tariffs").json()["agreements"] if a["connection_id"] == conn["id"])
    assert (row["state"], row["ends_in_days"], raised) == ("not_started", 40, True)


# ------------------------------------------------------------------------------------------------ the stored rebuild beside a simulation

def test_a_simulated_bill_carries_the_stored_rebuild_beside_the_version_in_force_now(world, monkeypatch):
    """The version in force is priced now: after a correction is approved, it differs from the rebuild stored when the bill was entered, which the
    Bills page shows, and `stored_rs` carries that one so the page can say they differ."""
    an, admin = world.c["analyst"], world.c["admin"]
    monkeypatch.setattr(plans, "tick", lambda *a, **k: None)
    with world.use():
        sid = make_site(name="Stored A3", conn=A3, battery=False, genset=False)
        _flat_rows(sid, date(2026, 8, 26), 31, 30.0)
        bill = verify.add_bill(sid, {"period_start": "2026-08-26", "period_end": "2026-09-25", "total_rs": 1450000.0}, "test")
    stored = bill["rebuilt"]["total_rs"]
    s = an.post("/api/tariffs/simulate", json={"site_id": sid, "bill_id": bill["id"], "draft": _draft(_fca())}).json()["bill"]
    assert s["stored_rs"] == s["in_force_rs"] == stored
    an.put("/api/tariffs/drafts/sep", json=_draft(_fca()))
    vid = an.post("/api/tariffs/drafts/sep/submit").json()["submitted"][0]["id"]
    assert admin.post(f"/api/tariffs/versions/{vid}/approve").status_code == 200
    ke = [{"kind": "tou", "key": "ke", "body": {"seasons": [{"months": list(range(1, 13)), "peak": ["17:00", "21:00"]}]}}]
    s = an.post("/api/tariffs/simulate", json={"site_id": sid, "bill_id": bill["id"], "draft": _draft(ke)}).json()["bill"]
    page = next(b for b in admin.get(f"/api/sites/{sid}/bills").json() if b["id"] == bill["id"])
    assert s["stored_rs"] == stored == page["rebuilt"]["total_rs"]
    assert s["in_force_rs"] == pytest.approx(1461657.83, abs=0.01) and s["in_force_rs"] != s["stored_rs"]
    m = an.post("/api/tariffs/simulate", json={"site_id": sid, "month": "2026-09", "draft": _draft(ke)}).json()["bill"]
    assert m["stored_rs"] is None


# ------------------------------------------------------------------------------------------------ the editor's words

def test_the_editor_hears_its_fields_in_its_own_units_and_words_and_the_raw_route_keeps_its_own(world):
    """Plan F2.5.2 and F2.5.3: the floor and the taxes are entered as percents ("Percent entry, fraction stored"), the peak windows as times, the
    rates as Rs/kWh; the editor's problems say so, by the field. The raw route (`POST /api/tariffs/versions`) refuses in the library's words."""
    cid = None
    with world.use():
        cid = sites.connections(world.site)[0]["id"]
    card = {"fixed_basis": "monthly", "fixed": 1250, "floor_share": 25, "tou": True, "peak": 49.31, "off_peak": "x"}
    body = _draft([{"kind": "schedule", "key": "", "body": {"cards": {"B-2(b)": card}}},
                   {"kind": "tou", "key": "xwdisco", "body": {"seasons": [{"months": list(range(1, 13)), "peak": ["17:00"]}]}},
                   {"kind": "tax", "key": "", "body": {"gst": 18, "further_tax_no_strn": 0.03}},
                   {"kind": "override", "key": cid, "body": {"electricity_duty": 1.5, "mdi_block": "rolling"}}])
    got = world.c["analyst"].post("/api/tariffs/versions/validate", json=body).json()
    words = {(p["version"], p["field"]): p["message"] for p in got["problems"]}
    assert words[(0, "body.cards.B-2(b).fixed_basis")] == ("Choose how the fixed charge for B-2(b) is charged: per kW of billing demand, per "
                                                         "sanctioned kW or per consumer, a month")
    assert words[(0, "body.cards.B-2(b).floor_share")] == "The floor for B-2(b) is a percent of the sanctioned load, from 0 to 100"
    assert words[(0, "body.cards.B-2(b).off_peak")] == "The off-peak rate for B-2(b) is a number of Rs/kWh, 0 or more"
    assert words[(1, "body.seasons.0.peak")] == "Season 1 needs a peak window: a start and a different end, in 24-hour time"
    assert words[(2, "body.gst")] == "GST is a percent from 0 to under 100"
    assert words[(3, "body.electricity_duty")] == "The site's electricity duty is a percent from 0 to under 100"
    assert words[(3, "body.mdi_block")] == "Choose the MDI blocks: fixed or sliding"
    assert not any("_" in m.split(":")[0] for m in words.values()), words
    # the raw route's words are the library's own
    raw = tariffs.version_problems("schedule", "national", "2026-12-01", {"cards": {"B-2(b)": card}}, SRC)
    assert dict(raw)[("body", "cards", "B-2(b)", "floor_share")] == "The card for B-2(b) needs floor_share between 0 and 1"


def test_the_drafts_refusals_are_plain_sentences(world):
    an = world.c["analyst"]

    def detail(body):
        r = an.put("/api/tariffs/drafts/words", json=body)
        assert r.status_code == 422, r.text
        return r.json()["detail"]

    assert detail({"versions": [{"kind": "adder"}] * 8}) == "A draft's versions must have at most 7 items"
    assert detail({"versions": [{"kind": "fuel"}]}).startswith("Version 1's kind must be one of ")
    assert detail({"versions": [{"kind": "adder"}, {"kind": "adder", "body": []}]}) == "Version 2's body must be a set of named values"
    assert detail({"effective_from": "2026-12-01-01"}) == "The start must be at most 10 characters"


# ------------------------------------------------------------------------------------------------ sending back, a taken id, another person's draft

def test_sending_a_version_back_is_an_administrators_made_with_the_withdrawal_and_audited_with_its_draft(world, monkeypatch):
    an, admin = world.c["analyst"], world.c["admin"]

    def propose(key):
        r = an.post("/api/tariffs/versions", json={"kind": "adder", "key": key, "effective_from": "2026-12-01",
                                                   "body": {"rate": 0.4, "billing_months": ["2026-12"]}, "source": SRC})
        assert r.status_code == 200, r.text
        return r.json()

    # its author withdraws it, but does not send it back to themself (H6.8: "approve or send back", approve_tariff)
    own = propose("dss")
    r = an.post(f"/api/tariffs/versions/{own['id']}/withdraw", json={"redraft": True})
    assert r.status_code == 403 and r.json()["detail"] == "Sending a tariff version back to its author is for an administrator", r.text
    assert next(v for v in admin.get("/api/tariffs/versions?kind=adder").json() if v["id"] == own["id"])["status"] == "pending"
    # the draft is made in the withdrawal's transaction: when it cannot be made, the version still waits
    def broken(*a, **k):
        raise RuntimeError("disk full")

    with monkeypatch.context() as m:
        m.setattr(tariff_drafts, "make", broken)
        r = admin.post(f"/api/tariffs/versions/{own['id']}/withdraw", json={"reason": "Use the notified rate", "redraft": True})
    assert r.status_code == 500
    assert next(v for v in admin.get("/api/tariffs/versions?kind=adder").json() if v["id"] == own["id"])["status"] == "pending"
    r = admin.post(f"/api/tariffs/versions/{own['id']}/withdraw", json={"reason": "Use the notified rate", "redraft": True})
    assert r.status_code == 200, r.text
    line = next(a for a in _actions(world) if a["action"] == "withdrew a tariff version")
    assert line["detail"] == {"version_id": own["id"], "effective_from": "2026-12-01", "reason": "Use the notified rate",
                              "sent_back_as": r.json()["draft_id"], "sent_back_to": own["by"]}


def test_a_taken_draft_id_is_refused_without_saying_whose_and_another_persons_draft_is_none_of_an_analysts(world):
    an, admin, op = world.c["analyst"], world.c["admin"], world.c["operator"]
    assert admin.put("/api/tariffs/drafts/admins", json=_draft(_fca())).status_code == 200
    r = an.put("/api/tariffs/drafts/admins", json=_draft(_fca()))
    assert r.status_code == 409 and r.json() == {**r.json(), "code": "draft_taken", "detail": "This draft id is taken: save the draft under a new id"}
    with world.use():
        sid = make_site(name="Owner A3", conn=A3, battery=False, genset=False)
        _flat_rows(sid, date(2026, 8, 26), 31, 30.0)
    assert an.delete("/api/tariffs/drafts/admins").status_code == 404
    assert an.post("/api/tariffs/drafts/admins/submit").status_code == 404
    assert an.post("/api/tariffs/preview", json={"site_id": sid, "draft_id": "admins", "days": 1}).status_code == 404
    assert an.post("/api/tariffs/simulate", json={"site_id": sid, "month": "2026-09", "draft_id": "admins"}).status_code == 404
    assert admin.get("/api/tariffs/drafts/admins").status_code == 200
    # an operator simulates a waiting version on a site (the Versions drawer), its rupees under money names
    with world.use():
        shipped = next(v for v in tariffs.versions("adder") if v["status"] == "pending" and v["by"] == "shipped")
    r = op.post("/api/tariffs/simulate", json={"site_id": sid, "month": "2026-09", "version_id": shipped["id"]})
    assert r.status_code == 200, r.text
    assert money.is_money("bill") and r.json()["bill"]["in_force_rs"] > 0
    kept = money.for_key(r.json())
    assert "bill" not in kept and not any(x > 1000 for x in _numbers(kept)), kept


# ------------------------------------------------------------------------------------------------ Karachi time on a version row

def test_a_version_rows_times_are_karachi_time(world, monkeypatch):
    v = world.c["analyst"].post("/api/tariffs/versions", json={"kind": "adder", "key": "fca", "effective_from": "2026-12-01",
                                                               "body": {"rate": 1.38, "billing_months": ["2026-12"]}, "source": SRC}).json()
    assert v["created"].endswith("+05:00"), v["created"]
    # half past one in the morning in Karachi is the day before in UTC: the version is known from Karachi's day
    monkeypatch.setattr(store, "_now", lambda: "2026-11-13T20:30:00+00:00")
    with world.use():
        got = tariffs.propose("adder", "fca", "2026-12-01", {"rate": 1.38, "billing_months": ["2026-12"]}, SRC, "t")
    assert got["known_from"] == "2026-11-14"
    assert routes_tariffs._row(got)["created"] == "2026-11-14T01:30:00+05:00"
