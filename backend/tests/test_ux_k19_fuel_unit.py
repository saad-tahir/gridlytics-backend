"""UX backend ask K19 (plan H11; D9.9, D2.9, D3.1.3): the genset's fuel unit with the fuel figures of `/now` and of the plan.

A captive-gas or RLNG genset's curve and price are per MMBtu, so every fuel figure of such a site is MMBtu, kept under the name `litres` (round 7
sweeps, `savings_export.fuel_unit`). Neither the Overview's snapshot nor a plan said so, so the redesigned tiles could show a gas site's fuel only as
hours (the plan's fallback). `GET /sites/:id/now` and a plan's `summary` now carry `fuel_unit`: "L" for diesel, "MMBtu" for captive gas and RLNG,
null without a genset. The plan's is stored when it is made (the genset's fuel then); a plan stored before reads it from the fuel it planned on
(`summary.fuel`, whose basis is "piped" for a gas genset and only for one). Not money: every reader reads it."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from gridlytics import plans, sites, store, twin
from test_fix_api import world  # noqa: F401  (world is a fixture)

PKT = timezone(timedelta(hours=5))
T = twin.day_start_utc(date(2026, 11, 10)) + 7 * 3600


@pytest.fixture
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _site(kind: str | None) -> str:
    sid = sites.create({"name": "Mill", "disco": "FESCO", "critical_load_kw": [50.0] * 24, "genset_start_control": "manual"}, "t")["id"]
    sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 200, "kw_ac": 180}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    if kind:
        sites.put_asset(sid, "genset", {"rated_kw": 150, "fuel_kind": kind, **({"tank_l": 400} if kind == "hsd" else {})}, "t")
    return sid


@pytest.mark.parametrize("kind,unit", [("hsd", "L"), ("gas_captive", "MMBtu"), ("lng_captive", "MMBtu"), (None, None)])
def test_a_plan_says_the_unit_of_its_genset_fuel(company, kind, unit):
    sid = _site(kind)
    r = plans.tick(sid, at=T, steps=96)
    assert r["status"] in ("active", "held"), r.get("reason")
    s = r["summary"]
    assert "fuel_unit" in s and s["fuel_unit"] == unit
    assert "litres" in s["breakdown"]                                    # the breakdown's fuel figure, in that unit
    assert plans.get(sid, r["plan_id"])["summary"]["fuel_unit"] == unit   # stored with the plan


def test_a_failed_solve_says_the_unit_too(company, monkeypatch):
    from gridlytics import optimise
    sid = _site("gas_captive")
    monkeypatch.setattr(optimise, "solve_with_policy", lambda p, time_limit=10.0: {"status": "not_solved", "solver_status": 1, "message": "made up",
                                                                                   "notes": [], "sol": None})
    r = plans.tick(sid, at=T, steps=96)
    assert r["status"] == "failed"
    with store.connect() as con:
        row = con.execute("SELECT summary FROM plans WHERE site_id = ?", (sid,)).fetchone()
    assert json.loads(row["summary"])["fuel_unit"] == "MMBtu"


def _stored(sid: str, summary: dict) -> str:
    """A plan row stored before K19: its summary as given."""
    pid = store.new_id()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, summary, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, sid, now, now, now, now, "a", "superseded", json.dumps(summary), "[]", "null"))
    return pid


def test_a_plan_stored_before_reads_its_unit_from_the_fuel_it_planned_on(company):
    """`summary.fuel` was stored by every plan since round 7 (engine-optimiser-05): "piped" is a gas genset's basis (plans.PIPED_FUELS is
    sites.GAS_FUELS), any other a diesel tank's, null no genset. A plan older than that says nothing (left out: the page falls back to hours)."""
    assert plans.PIPED_FUELS == sites.GAS_FUELS
    sid = _site("hsd")
    cases = {"piped": ({"fuel": {"basis": "piped", "litres": None, "tank_l": None}}, "MMBtu"),
             "carried": ({"fuel": {"basis": "carried", "litres": 120.0, "tank_l": 400.0}}, "L"),
             "unknown": ({"fuel": {"basis": "unknown", "litres": None, "tank_l": None}}, "L"),
             "none": ({"fuel": None}, None)}
    for name, (summary, unit) in cases.items():
        pid = _stored(sid, {**summary, "breakdown": {"litres": 3.0}})
        got = plans.get(sid, pid)["summary"]
        assert "fuel_unit" in got and got["fuel_unit"] == unit, name
    older = plans.get(sid, _stored(sid, {"breakdown": {"litres": 3.0}}))["summary"]
    assert "fuel_unit" not in older
    # the unit stored with a plan is never read again from its fuel
    kept = plans.get(sid, _stored(sid, {"fuel": {"basis": "piped"}, "fuel_unit": "L"}))["summary"]
    assert kept["fuel_unit"] == "L"
    # and the list of plans reads it as the plan does
    assert {p["id"]: p["summary"].get("fuel_unit", "absent") for p in plans.history(sid)}[pid] is None


def test_now_says_the_unit_of_its_fuel_figures_to_every_reader(world):
    for role in ("admin", "operator", "analyst", "viewer"):
        r = world.c[role].get(f"/api/sites/{world.site}/now")
        assert r.status_code == 200, (role, r.text)
        body = r.json()
        assert body["fuel_unit"] == "L", role                          # the world's mill has a diesel genset
        assert "litres" in body["month"]
    with world.use():
        sites.put_asset(world.site, "genset", {"rated_kw": 1000, "fuel_kind": "gas_captive"}, "test")
    for role in ("admin", "viewer"):
        assert world.c[role].get(f"/api/sites/{world.site}/now").json()["fuel_unit"] == "MMBtu", role
    other = world.c["admin"].get(f"/api/sites/{world.other}/now").json()    # no genset
    assert "fuel_unit" in other and other["fuel_unit"] is None
