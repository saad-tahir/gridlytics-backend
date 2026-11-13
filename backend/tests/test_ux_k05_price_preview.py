"""UX backend ask K5 (plan H11; C19.2 item 1, C6.2, C6.7; step 9b, Set-up C's TARIFF DAY and PEAK WINDOWS THROUGH THE YEAR).

`POST /api/sites/{id}/prices/preview` prices the connection's fields as step C holds them, before they are saved and before the site has any
connection: the answer of `GET /api/sites/{id}/prices` for those fields (the optimiser's own price vectors), with the DISCO group's peak windows month by
month, the time-of-use line ("Time of use: the other DISCOs' windows (follows FESCO)") and the server's stamp. Nothing is saved. The goldens are
worked by hand from the shipped rates (UX K32's A-3 golden)."""
from __future__ import annotations

import identities
from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import money, sites, store

#: A-3 (commercial, flat) at a 40 kW sanctioned load, billed from the 25th: September 2026's bill (the K32 golden)
A3 = {"tariff_category": "A-3", "sanctioned_load_kw": 40, "regime": "flat", "reading_day": 25}
B2B = {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400, "regime": "none"}


def _preview(w, body, day="2026-09-02", role="admin", site=None):
    return w.c[role].post(f"/api/sites/{site or w.other}/prices/preview", params={"day": day} if day else None, json=body)


def test_golden_a_connection_not_saved_yet_is_priced_as_the_save_would_price_it(world):
    """Secret Site Two has no connection. A-3 flat on 2 Sep 2026 (billing month September):
    energy and adders 42.48 + the month's adders = 48.2875 Rs/kWh; x 1.015 electricity duty = 49.0118; GST and s.235 recovered: Rs 49.01 every step.
    The same connection bearing its GST (STRN, GST not claimed back): 49.0118 x 1.18 = 57.834 -> Rs 57.83."""
    with world.use():
        assert sites.connections(world.other) == []
    r = _preview(world, A3)
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["day"], p["connection_id"], p["preview"], len(p["c_imp"])) == ("2026-09-02", None, True, 96)
    assert set(p["c_imp"]) == {49.01} and set(p["c_exp"]) == {0.0} and not any(p["peak"])
    assert p["as_of"].endswith("+05:00")
    bears = _preview(world, {**A3, "tax": {"strn": True, "gst_recoverable": False}}).json()
    assert set(bears["c_imp"]) == {57.83}
    # nothing was saved
    with world.use():
        assert sites.connections(world.other) == []
    # saved as it was previewed, GET /prices answers the same figures
    assert world.c["admin"].put(f"/api/sites/{world.other}/connection", json=A3).status_code == 200
    saved = world.c["admin"].get(f"/api/sites/{world.other}/prices", params={"day": "2026-09-02"}).json()
    for k in ("t", "c_imp", "c_exp", "peak", "mdi", "icp", "energy_factor", "tariff_notices"):
        assert saved[k] == p[k], k


def test_the_time_of_use_windows_follow_the_site_s_disco_month_by_month(world):
    """Mill One is FESCO's: the other DISCOs' calendar (Dec to Feb 17:00 to 21:00, Mar to May 18:00 to 22:00, Jun to Aug 19:00 to 23:00, Sep to Nov
    18:00 to 22:00). On 2 Sep 2026, B-2(b)'s peak is 18:00 to 22:00, steps 72 to 87."""
    p = _preview(world, B2B, site=world.site).json()
    assert [i for i, x in enumerate(p["peak"]) if x] == list(range(72, 88))
    assert p["time_of_use"] == {"disco_group": "xwdisco", "disco": "FESCO", "words": "Time of use: the other DISCOs' windows (follows FESCO)"}
    w = p["peak_windows"]
    assert (w["disco_group"], w["source"], w["effective_from"]) == ("xwdisco", "tou", "2023-07-14")
    assert [(s["months"], s["peak"], s["words"]) for s in w["seasons"]] == [
        ([12, 1, 2], ["17:00", "21:00"], "Dec to Feb: peak 17:00 to 21:00"), ([3, 4, 5], ["18:00", "22:00"], "Mar to May: peak 18:00 to 22:00"),
        ([6, 7, 8], ["19:00", "23:00"], "Jun to Aug: peak 19:00 to 23:00"), ([9, 10, 11], ["18:00", "22:00"], "Sep to Nov: peak 18:00 to 22:00")]
    assert len(w["by_month"]) == 12 and all(len(row) == 24 for row in w["by_month"])
    assert w["by_month"][0] == [0.0] * 17 + [1.0] * 4 + [0.0] * 3            # January: 17:00 to 21:00
    assert w["by_month"][8] == [0.0] * 18 + [1.0] * 4 + [0.0] * 2            # September: 18:00 to 22:00
    # a K-Electric site: K-Electric's own windows (Apr to Oct 18:30 to 22:30, half hours shaded as halves), whatever group the form sent
    ke = world.c["admin"].post("/api/sites", json={"name": "Karachi K5", "disco": "K-Electric"}).json()["id"]
    p = _preview(world, B2B, site=ke).json()
    assert p["time_of_use"]["words"] == "Time of use: K-Electric's windows"
    assert [(s["words"]) for s in p["peak_windows"]["seasons"]] == ["Apr to Oct: peak 18:30 to 22:30", "Nov to Mar: peak 18:00 to 22:00"]
    assert p["peak_windows"]["by_month"][8][18:23] == [0.5, 1.0, 1.0, 1.0, 0.5]
    assert [i for i, x in enumerate(p["peak"]) if x] == list(range(74, 90))
    # a site with no DISCO yet: the other DISCOs' windows, which the form may still change
    assert _preview(world, A3).json()["time_of_use"] == {"disco_group": "xwdisco", "disco": None, "words": "Time of use: the other DISCOs' windows"}


def test_the_fields_are_checked_as_the_save_checks_them(world):
    r = _preview(world, {**B2B, "disco_group": "ke"}, site=world.site)
    assert (r.status_code, r.json()["detail"]) == (422, "This site's DISCO is FESCO: its connection uses the other DISCOs' time-of-use windows, not "
                                                        "K-Electric's"), r.text
    r = _preview(world, {**B2B, "sanctioned_load_kw": 0})
    assert (r.status_code, r.json()["detail"]) == (422, "The sanctioned load (kW) must be more than 0"), r.text
    r = _preview(world, {**B2B, "tariff_category": ""})
    assert (r.status_code, r.json()["detail"]) == (422, "Choose the tariff category printed on the bill"), r.text
    r = _preview(world, {**B2B, "colour": "red"})
    assert (r.status_code, r.json()["detail"]) == (422, "Unknown field 'colour'"), r.text
    # the save's body is taken whole: the id names the saved connection being edited, the save-only fields are ignored
    with world.use():
        held = sites.connections(world.site)[0]
    r = _preview(world, {**B2B, "id": held["id"], "if_unchanged": held["data"], "idempotency_key": "k5"}, site=world.site)
    assert r.status_code == 200 and r.json()["connection_id"] == held["id"], r.text
    r = _preview(world, {**B2B, "id": "nope"}, site=world.site)
    assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
    r = _preview(world, B2B, day="2014-12-31")
    assert r.status_code == 422, r.text


def test_a_day_no_rate_card_covers_says_so_in_words(world):
    # the library's first uniform schedule starts on 1 Jul 2025: a day before it has no B-2(b) card in force (plan C6.8's warn state)
    r = _preview(world, B2B, day="2025-06-01")
    assert (r.status_code, r.json()["code"]) == (422, "no_tariff_version"), r.text
    assert r.json()["detail"] == "No B-2(b) rate card is in force on 1 Jun 2025, so the plan cannot price this connection: check Tariffs"


def test_the_preview_is_for_see_costs_and_names_its_rupees_as_money(world):
    for role, want in (("admin", 200), ("operator", 200), ("analyst", 200), ("viewer", 403)):
        r = _preview(world, A3, role=role)
        assert r.status_code == want, (role, r.text)
    r = _preview(world, A3, role="viewer")
    assert r.json()["detail"] == "Seeing prices is for an administrator, an operator or an analyst"
    r = world.anon.post(f"/api/sites/{world.site}/prices/preview", json=A3, headers=world.key(["sites:read", "plans:read"]))
    assert r.status_code == 403, r.text
    body = _preview(world, A3).json()
    kept = money.for_key(body)
    assert "c_imp" not in kept and "c_exp" not in kept and "mdi" in kept
    assert all(money.is_money(k) for k in ("c_imp", "c_exp"))
    # the windows and the time-of-use line carry no money: a page may draw them for any reader of the step
    assert kept["peak_windows"] == body["peak_windows"] and kept["time_of_use"] == body["time_of_use"]


def test_another_company_s_site_is_no_site_here(world):
    other = identities.company("Another company", own_file=True)
    from gridlytics import platform as platformdb
    with store.use_company(platformdb.company_file(other)):
        theirs = sites.create({"name": "Their Mill", "disco": "LESCO"}, "t")["id"]
    assert _preview(world, A3, site=theirs).status_code == 404
