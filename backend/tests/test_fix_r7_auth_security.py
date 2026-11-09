"""Round 7 (audit), package auth-security: one regression test per finding, named by the audit's id.

Each test works in a data folder of its own (the `world` fixture of test_fix_api.py: a company file with two sites and a member of every role)."""
from __future__ import annotations

import json

from test_fix_api import world  # noqa: F401  (world is a fixture)


def _keys(obj) -> set[str]:
    """Every key at any depth of an answer."""
    if isinstance(obj, dict):
        return set(obj) | {k for v in obj.values() for k in _keys(v)}
    if isinstance(obj, list):
        return {k for v in obj for k in _keys(v)}
    return set()


# ------------------------------------------------------------------------------------------------ security-roles-01 (tracker-and-docs-02)

def test_the_events_route_gives_no_fuel_cost_to_a_viewer_or_a_key(world):
    sid = world.site
    assert world.c["operator"].post(f"/api/sites/{sid}/genset/fuel", json={"litres": 50, "day": "2026-11-05", "cost_rs": 14000, "note": "tanker"}).status_code == 200
    # the administrator still sees what the fuel cost
    mine = world.c["admin"].get(f"/api/sites/{sid}/events").json()
    assert [e["detail"]["cost_rs"] for e in mine if e["type"] == "fuel_log"] == [14000]
    # a viewer and a site key holding sites:read see the litres, never the rupees
    seen = world.c["viewer"].get(f"/api/sites/{sid}/events")
    keyed = world.anon.get(f"/api/sites/{sid}/events", headers=world.key(["sites:read"]))
    for r in (seen, keyed):
        assert r.status_code == 200, r.text
        logs = [e for e in r.json() if e["type"] == "fuel_log"]
        assert [e["detail"]["litres"] for e in logs] == [50]
        assert "cost_rs" not in _keys(r.json()) and "14000" not in json.dumps(r.json())


# ------------------------------------------------------------------------------------------------ security-roles-02 (the filter is the API's)

def test_every_answer_is_filtered_for_a_viewer_and_a_count_is_not_money(world):
    """The guard filters every JSON answer (test_role_walk walks every route); a bare "total" is a count, so the viewer's alert badge keeps it."""
    from gridlytics import money
    assert not money.is_money("total") and money.is_money("total_rs")
    for role in ("admin", "viewer"):
        counts = world.c[role].get("/api/alerts/counts")
        assert counts.status_code == 200 and isinstance(counts.json()["total"], int), (role, counts.text)
    # the routes that never called out() are filtered all the same: the native capture's answer of a viewer, through a key as well
    sid = world.site
    assert world.c["viewer"].get(f"/api/sites/{sid}/native").status_code == 200
    assert world.anon.get(f"/api/sites/{sid}/overrides", headers=world.key(["sites:read"])).status_code == 200


# ------------------------------------------------------------------------------------------------ role-operator-06 (pages-C-20)

#: The money settings and the figures made from them alone (the wear price is the replacement cost over the warranty)
MONEY_SETTINGS = {"voll", "voll_rs_per_kwh", "replacement_cost_per_kwh", "c_cyc", "c_start", "c_start_rs", "c_runhr", "c_runhr_rs", "c_om", "c_om_rs_per_kwh",
                  "c_short"}


def test_an_operator_sees_costs_but_not_the_money_settings(world):
    sid, a, op, an = world.site, world.c["admin"], world.c["operator"], world.c["analyst"]
    assert a.put(f"/api/sites/{sid}", json={"voll_rs_per_kwh": 650}).status_code == 200
    battery = {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}
    assert a.put(f"/api/sites/{sid}/assets/battery", json={**battery, "replacement_cost_per_kwh": 61000}).status_code == 200
    assert a.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 150, "c_start_rs": 1700, "c_runhr_rs": 450, "c_om_rs_per_kwh": 9}).status_code == 200
    pages = (f"/api/sites/{sid}", f"/api/sites/{sid}/battery", f"/api/sites/{sid}/genset", "/api/sites")
    for url in pages:
        assert op.get(url).status_code == 200, url
        assert MONEY_SETTINGS & _keys(op.get(url).json()) == set(), url
    # the administrator and the analyst read them (plan 9.4: "Analyst: everything including money settings")
    for c in (a, an):
        assert {"replacement_cost_per_kwh", "c_cyc"} <= _keys(c.get(f"/api/sites/{sid}/battery").json())
        assert {"voll_rs_per_kwh", "c_start_rs"} <= _keys(c.get(f"/api/sites/{sid}").json())
    # costs stay the operator's: the genset's estimated fuel cost and the fuel price
    genset = op.get(f"/api/sites/{sid}/genset").json()
    assert "fuel_cost_rs" in genset["kpis"] and genset["fuel_price"]["price_per_l"] is not None
    assert {r: c.get("/api/meta").json()["permissions"]["see_money_settings"] for r, c in world.c.items()} == \
        {"admin": True, "operator": False, "analyst": True, "viewer": False}


def test_an_operator_saves_the_equipment_without_the_money_settings_it_cannot_see(world):
    """The settings form fills a field the answer left out with its default and sends the asset back whole: the stored money settings are kept, and the
    operator's change of what it may edit is saved."""
    sid, a, op = world.site, world.c["admin"], world.c["operator"]
    assert a.put(f"/api/sites/{sid}/assets/battery", json={"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100, "replacement_cost_per_kwh": 61000}).status_code == 200
    assert a.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 150, "c_start_rs": 1700, "c_runhr_rs": 450, "c_om_rs_per_kwh": 9}).status_code == 200
    r = op.put(f"/api/sites/{sid}/assets/battery", json={"nameplate_kwh": 220, "p_ch_kw": 100, "p_dis_kw": 100, "replacement_cost_per_kwh": 52000,
                                                         "warranted_cycles": 6000, "warranted_depth": 0.8})
    assert r.status_code == 200, r.text
    r = op.put(f"/api/sites/{sid}/assets/genset", json={"rated_kw": 160, "c_start_rs": 1500, "c_runhr_rs": 400, "c_om_rs_per_kwh": 8})
    assert r.status_code == 200, r.text
    assets = a.get(f"/api/sites/{sid}").json()["assets"]
    assert (assets["battery"]["nameplate_kwh"], assets["battery"]["replacement_cost_per_kwh"]) == (220, 61000)
    assert (assets["genset"]["rated_kw"], assets["genset"]["c_start_rs"], assets["genset"]["c_runhr_rs"], assets["genset"]["c_om_rs_per_kwh"]) == (160, 1700, 450, 9)
    # a figure other than the form's default is a change, and a change is still an administrator's
    r = op.put(f"/api/sites/{sid}/assets/battery", json={"nameplate_kwh": 220, "p_ch_kw": 100, "p_dis_kw": 100, "replacement_cost_per_kwh": 40000})
    assert r.status_code == 403 and "administrator" in r.json()["detail"]


# ------------------------------------------------------------------------------------------------ role-viewer-19

def _saving_day(saving: float, litres: float) -> dict:
    return {"saving_rs": saving, "claimed_rs": saving, "levers": {"peak_window": saving}, "fidelity": "confirmed", "litres_saved": litres,
            "actual": {"cost_rs": 30000.0, "litres": 40.0, "peak_kw": 180.0}, "baseline": {"cost_rs": 30000.0 + saving, "litres": 40.0 + litres},
            "peak_kw_actual": 180.0, "peak_kw_baseline": 190.0, "mdi_rate": 0.0, "unserved_kwh_actual": 0.0, "unserved_kwh_baseline": 0.0}


def test_a_viewer_reads_the_litres_saved_and_the_verified_days_but_no_rupee(world):
    from gridlytics import money, store
    sid = world.site
    with world.use(), store.connect() as con:
        for day, (saving, litres) in {"2026-11-02": (4321.0, 12.5), "2026-11-03": (2345.0, 7.5)}.items():
            for base in ("native", "tuned"):
                con.execute("INSERT OR REPLACE INTO savings_days (site_id, day_local, baseline, data, computed) VALUES (?,?,?,?,?)",
                            (sid, day, base, json.dumps(_saving_day(saving, litres)), store._now()))
    url = f"/api/sites/{sid}/savings?start=2026-11-01&end=2026-11-05"
    mine = world.c["admin"].get(url).json()
    assert mine["baselines"]["native"]["saving_rs"] == 6666.0
    seen = world.c["viewer"].get(url).json()
    assert seen["hidden_for_role"] is True
    native = seen["baselines"]["native"]
    assert (native["litres_saved"], native["verified_days"]) == (20.0, 2)
    assert seen["final_conditions"]["verified_days"] == 2
    assert [(d["day"], d["litres_actual"], d["litres_native"]) for d in seen["days"]] == [("2026-11-02", 40.0, 52.5), ("2026-11-03", 40.0, 47.5)]
    assert {k for k in _keys(seen) if money.is_money(k)} == set()
    assert not {4321.0, 2345.0, 6666.0, 30000.0} & {v for d in seen["days"] for v in d.values()} and "6666" not in json.dumps(seen)
