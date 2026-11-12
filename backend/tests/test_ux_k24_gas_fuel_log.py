"""UX backend ask K24 (plan H11; E6 item 6, A8.5, E2.2.7): a gas genset's fuel log in MMBtu, as billed or from a meter reading.

`POST /sites/:id/genset/fuel` took `litres` only. A captive-gas or RLNG genset's fuel is billed in MMBtu, and its meter reads hm³ with the calorific
value printed on the bill. The log now takes, for a gas genset, `mmbtu` (as billed) or `hm3` with `calorific_mmbtu_per_hm3` (the bill's figure, never
an assumed one), and stores MMBtu: hm³ x the calorific value. A diesel genset's log stays in litres. Each log row and the genset page's `fuel_logged`
carry the unit, and a meter reading keeps its hm³ and calorific value beside the MMBtu it gave. The open question Q9 (the fuel log in MMBtu as billed or
from a meter reading) ships its recommended answer, yes.

`litres` sent for a gas genset is read as MMBtu, as the web app of before sent its MMBtu (round 7 sweeps' fuel unit), so that form keeps working."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from gridlytics import sites, store
from test_fix_api import world  # noqa: F401  (world is a fixture)

PKT = timezone(timedelta(hours=5))


def _gas(world, kind: str = "gas_captive") -> None:
    with world.use():
        sites.put_asset(world.site, "genset", {"rated_kw": 1000, "fuel_kind": kind}, "test")


def _post(world, body: dict, role: str = "operator"):
    return world.c[role].post(f"/api/sites/{world.site}/genset/fuel", json=body)


def _logs(world, role: str = "admin") -> list[dict]:
    r = world.c[role].get(f"/api/sites/{world.site}/genset/fuel")
    assert r.status_code == 200, r.text
    return r.json()["logs"]


def test_a_gas_gensets_fuel_is_logged_in_mmbtu_as_billed(world):
    _gas(world)
    r = _post(world, {"mmbtu": 412.5, "day": "2026-11-05", "cost_rs": 1299375, "note": "October bill"})
    assert r.status_code == 200, r.text
    assert r.json() == {"logged": 412.5, "unit": "MMBtu", "entry": "billed", "day": "2026-11-05", "hm3": None, "calorific_mmbtu_per_hm3": None}
    row = _logs(world)[0]
    assert (row["litres"], row["unit"], row["entry"], row["mmbtu"], row["hm3"], row["calorific_mmbtu_per_hm3"]) == (412.5, "MMBtu", "billed", 412.5,
                                                                                                                 None, None)
    assert row["cost_rs"] == 1299375 and row["note"] == "October bill" and row["day"] == "2026-11-05"


def test_a_meter_reading_in_hm3_is_stored_as_mmbtu_with_the_bills_calorific_value(world):
    """Golden: 123.4 hm³ at 3.35 MMBtu per hm³ is 413.39 MMBtu; 120 hm³ at 3.4333 is 411.996 MMBtu (kept to three decimals)."""
    _gas(world, "lng_captive")
    r = _post(world, {"hm3": 123.4, "calorific_mmbtu_per_hm3": 3.35, "day": "2026-11-06"})
    assert r.status_code == 200, r.text
    assert r.json() == {"logged": 413.39, "unit": "MMBtu", "entry": "meter", "day": "2026-11-06", "hm3": 123.4, "calorific_mmbtu_per_hm3": 3.35}
    assert _post(world, {"hm3": 120, "calorific_mmbtu_per_hm3": 3.4333, "day": "2026-11-07"}).json()["logged"] == 411.996
    rows = _logs(world)
    assert [(x["mmbtu"], x["hm3"], x["calorific_mmbtu_per_hm3"], x["entry"]) for x in rows] == [(411.996, 120, 3.4333, "meter"),
                                                                                               (413.39, 123.4, 3.35, "meter")]
    # the audit line names the unit and keeps the reading it came from
    with world.use():
        line = next(a for a in store.list_audit() if a["action"] == "logged fuel")
    detail = line["detail"] if isinstance(line["detail"], dict) else json.loads(line["detail"])
    assert (detail["litres"], detail["fuel_unit"], detail["hm3"], detail["calorific_mmbtu_per_hm3"]) == (411.996, "MMBtu", 120, 3.4333), detail


def test_the_genset_page_sums_the_logged_fuel_in_the_gensets_unit(world):
    _gas(world)
    today = datetime.now(PKT).date().isoformat()
    assert _post(world, {"mmbtu": 100.0, "day": today}).status_code == 200
    assert _post(world, {"hm3": 10, "calorific_mmbtu_per_hm3": 3.5, "day": today}).status_code == 200
    for role in ("admin", "viewer"):
        page = world.c[role].get(f"/api/sites/{world.site}/genset", params={"days": 3}).json()
        assert page["fuel_logged"] == {"litres": 135.0, "entries": 2, "unit": "MMBtu"}, (role, page["fuel_logged"])


def test_a_diesel_genset_keeps_its_litres_and_says_so(world):
    r = _post(world, {"litres": 50, "day": "2026-11-05", "cost_rs": 14000, "note": "tanker"})
    assert r.status_code == 200, r.text
    assert r.json() == {"logged": 50.0, "unit": "L", "entry": "litres", "day": "2026-11-05", "hm3": None, "calorific_mmbtu_per_hm3": None}
    row = _logs(world)[0]
    assert (row["litres"], row["unit"], row["entry"], row["mmbtu"]) == (50, "L", "litres", None)
    # MMBtu and a meter reading are a gas genset's: refused in a sentence for a diesel one, and nothing is logged
    for body in ({"mmbtu": 10}, {"hm3": 10, "calorific_mmbtu_per_hm3": 3.4}):
        r = _post(world, body)
        assert r.status_code == 422 and r.json()["detail"] == "This genset burns diesel: log its fuel in litres", r.text
    assert len(_logs(world)) == 1


def test_litres_sent_for_a_gas_genset_are_read_as_mmbtu_as_the_web_app_of_before_sent_them(world):
    _gas(world)
    r = _post(world, {"litres": 4.5})
    assert r.status_code == 200, r.text
    assert (r.json()["logged"], r.json()["unit"], r.json()["entry"]) == (4.5, "MMBtu", "billed")
    assert _logs(world)[0]["mmbtu"] == 4.5


def test_a_fuel_log_is_refused_in_one_plain_sentence(world):
    _gas(world)
    cases = {
        "Log the fuel as litres, as MMBtu, or as a meter reading in hm³ with the bill's calorific value": [{}, {"day": "2026-11-05"}],
        "Log the fuel one way only: litres, MMBtu, or a meter reading in hm³": [{"mmbtu": 5, "hm3": 2, "calorific_mmbtu_per_hm3": 3.4},
                                                                            {"litres": 5, "mmbtu": 5}],
        "A meter reading in hm³ needs the calorific value printed on the bill (MMBtu per hm³)": [{"hm3": 10}],
        "The calorific value goes with a meter reading in hm³": [{"mmbtu": 10, "calorific_mmbtu_per_hm3": 3.4}],
        "The calorific value is the bill's MMBtu per hm³, from 2 to 5": [{"hm3": 10, "calorific_mmbtu_per_hm3": 950},
                                                                         {"hm3": 10, "calorific_mmbtu_per_hm3": 38.5},
                                                                         {"hm3": 10, "calorific_mmbtu_per_hm3": 0}],
        "MMBtu must be a positive number under 100,000": [{"mmbtu": 0}, {"mmbtu": -3}, {"mmbtu": 100000}],
        "The meter reading must be a positive number of hm³ under 100,000": [{"hm3": 0, "calorific_mmbtu_per_hm3": 3.4}],
        "That reading gives 175,000 MMBtu, more than one log takes (under 100,000): check the figures": [{"hm3": 50000, "calorific_mmbtu_per_hm3": 3.5}],
    }
    for detail, bodies in cases.items():
        for body in bodies:
            r = _post(world, body)
            assert r.status_code == 422, (body, r.text)
            assert r.json()["detail"] == detail, (body, r.json()["detail"])
    # types are kept: a yes or no, or text, is not a number; an unknown field is refused
    for body in ({"mmbtu": True}, {"mmbtu": "lots"}, {"hm3": "12", "calorific_mmbtu_per_hm3": 3.4}, {"mmbtu": 5, "cost_rs": True},
                 {"mmbtu": 5, "cost_rs": -1}, {"mmbtu": 5, "cost_rs": 1e12}, {"mmbtu": 5, "kg": 3}, {"mmbtu": 5, "day": "5 Nov"}):
        r = _post(world, body)
        assert r.status_code == 422 and r.json()["code"] == "invalid", (body, r.text)
    assert _logs(world) == []


def test_who_may_log_fuel_and_read_its_cost_is_unchanged(world):
    _gas(world)
    for role in ("analyst", "viewer"):
        assert _post(world, {"mmbtu": 5}, role).status_code == 403, role
    assert _post(world, {"mmbtu": 5, "cost_rs": 15750}, "admin").status_code == 200
    viewer = _logs(world, "viewer")
    assert viewer and all("cost_rs" not in x for x in viewer)
    assert viewer[0]["mmbtu"] == 5 and viewer[0]["unit"] == "MMBtu"
