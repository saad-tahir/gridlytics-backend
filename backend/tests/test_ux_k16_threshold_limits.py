"""UX backend ask K16 (plan H11; D9.6, D6.3; step 14, Alerts › Rules).

Every rule in the rules answers (`GET` and `PUT /api/alerts/rules`, and a site's `rules` in `GET` and `PUT /api/alerts/rules/sites/{id}`) carries
`limits`: each of its thresholds' default and allowed range ({default, min, max}), straight from `alerts.THRESHOLDS`, so the rule editor writes
"Default 15 · 5 to 1,440" beside the field. No data source's staleness default is a figure ("by the data source"): `stale_minutes` names each
kind of source's minutes (`by_source`). The limits are the server's and are never saved: a rule sent back with them is taken, and they are left
out of what is stored."""
from __future__ import annotations

from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import alerts, store

ROLES = ("admin", "operator", "analyst", "viewer")


def _limits(cat: str) -> dict:
    out = {}
    for name, (default, lo, hi) in alerts.THRESHOLDS.get(cat, {}).items():
        out[name] = {"default": default, "min": lo, "max": hi}
    if cat == "Data":
        out["stale_minutes"]["by_source"] = {"edge": 30, "oem_cloud": 20, "csv": 1440}
    return out


def test_every_rule_carries_its_thresholds_defaults_and_ranges_for_every_reader(world):
    for role in ROLES:
        rules = world.c[role].get("/api/alerts/rules").json()
        assert rules and (role != "admin" or set(rules) == set(alerts.CATEGORIES)), role
        for cat, rule in rules.items():
            assert rule["limits"] == _limits(cat), (role, cat, rule["limits"])
            assert set(rule["limits"]) == set(rule["thresholds"]), (role, cat)
        site = world.c[role].get(f"/api/alerts/rules/sites/{world.site}").json()
        assert {c: r["limits"] for c, r in site["rules"].items()} == {c: _limits(c) for c in rules}, role
        assert all("limits" not in o for o in site["overrides"].values()), role
    # the plan's own example: the edge's silence, default 15, from 5 to 1,440 minutes
    control = world.c["admin"].get("/api/alerts/rules").json()["Control"]["limits"]["edge_offline_minutes"]
    assert control == {"default": 15, "min": 5, "max": 1440}
    # a category with nothing to set has no limits; a viewer reads only the five energy kinds' (no bill tolerance)
    assert world.c["admin"].get("/api/alerts/rules").json()["Savings"]["limits"] == {}
    assert set(world.c["viewer"].get("/api/alerts/rules").json()) == set(alerts.ENERGY_CATEGORIES)


def test_the_range_the_answer_gives_is_the_range_a_save_holds_to(world):
    admin = world.c["admin"]
    for cat, rule in admin.get("/api/alerts/rules").json().items():
        for name, lim in rule["limits"].items():
            for bad in (lim["min"] - 0.01, lim["max"] + 1):
                r = admin.put("/api/alerts/rules", json={cat: {"thresholds": {name: bad}}})
                assert r.status_code == 422, (cat, name, bad, r.text)
                assert f"from {lim['min']:g} to {lim['max']:g}" in r.json()["detail"], r.json()["detail"]
            for good in (lim["min"], lim["max"]):
                r = admin.put("/api/alerts/rules", json={cat: {"thresholds": {name: good}}})
                assert r.status_code == 200 and r.json()[cat]["thresholds"][name] == good, (cat, name, good, r.text)


def test_the_answers_of_a_save_carry_the_limits_too(world):
    admin, sid = world.c["admin"], world.site
    r = admin.put("/api/alerts/rules", json={"Genset": {"thresholds": {"unrequested_minutes": 20}}})
    assert r.status_code == 200, r.text
    assert r.json()["Genset"]["limits"]["unrequested_minutes"] == {"default": 10, "min": 1, "max": 120}
    r = admin.put(f"/api/alerts/rules/sites/{sid}", json={"Forecast": {"thresholds": {"bust_pct": 40}}})
    assert r.status_code == 200, r.text
    assert r.json()["rules"]["Forecast"]["limits"]["bust_pct"] == {"default": 30, "min": 5, "max": 100}
    assert r.json()["overrides"] == {"Forecast": {"thresholds": {"bust_pct": 40.0}}}


def test_a_rule_sent_back_with_its_limits_is_taken_and_the_limits_are_never_stored(world):
    """The editor (or any client) may send the rule as it read it: the limits are the server's, never a setting, and a changed one changes nothing."""
    admin, sid = world.c["admin"], world.site
    read = admin.get("/api/alerts/rules").json()
    sent = {c: {**r, "limits": {"made_up": {"default": 1, "min": 0, "max": 2}}} for c, r in read.items()}
    sent["Control"]["thresholds"] = {"edge_offline_minutes": 20}
    r = admin.put("/api/alerts/rules", json=sent)
    assert r.status_code == 200, r.text
    assert r.json()["Control"]["thresholds"]["edge_offline_minutes"] == 20
    assert r.json()["Control"]["limits"] == _limits("Control")
    r = admin.put(f"/api/alerts/rules/sites/{sid}", json={"Reserve": read["Reserve"]})
    assert r.status_code == 200 and r.json()["overrides"] == {}, r.text
    with world.use():
        assert all("limits" not in rule for rule in store.get_setting("alert_rules").values())
        assert store.get_setting(f"alert_rules:site:{sid}") == {}
    # a limits field that is not a set of named values is still the server's to ignore, but a threshold out of range is still refused
    r = admin.put("/api/alerts/rules", json={"Control": {"limits": "anything", "thresholds": {"edge_offline_minutes": 2}}})
    assert r.status_code == 422 and "from 5 to 1440" in r.json()["detail"], r.text
