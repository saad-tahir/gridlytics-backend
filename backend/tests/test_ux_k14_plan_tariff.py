"""UX backend ask K14 (plan H11; D9.4, D3.1.4): the tariff version a plan was priced with, in its `summary`.

The Plan view's "Inputs this plan used" reads "FESCO B-3, version in force from 1 Jul 2026" (plan D3.1.4), and nothing in a plan said which tariff
priced it. `summary.tariff` now names the site's DISCO, the plant connection's category and the schedule version (the rate card) in force on the plan's
first Karachi day, with every schedule version the horizon's days were priced with (`versions`: one for each stretch of days on one version, so up
to three, since a 48-hour horizon that does not start at midnight touches three Karachi days), each with its first and last Karachi day. "The tariff version" is the schedule version, as a savings day's `tariff_version` is
(verify, savings_export). An off-grid plan is priced on no tariff: `tariff` is null. Words, ids and dates only: every reader, a viewer and a machine
key included, reads it whole."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from gridlytics import money, plans, sites, store, tariffs, twin
from test_fix_api import world  # noqa: F401  (world is a fixture)

PKT = timezone(timedelta(hours=5))
#: 07:00 on Tuesday 10 Nov 2026 in Karachi: a 48-hour horizon from it prices the Karachi days 10, 11 and 12 Nov
T = twin.day_start_utc(date(2026, 11, 10)) + 7 * 3600
#: The words of a rupee figure (the role walk's own pattern)
RUPEES = ("Rs ", "Rs.", "PKR", "rupee")


@pytest.fixture
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _site(disco: str = "FESCO", category: str = "B-2(b)", **conn) -> str:
    sid = sites.create({"name": "Mill", "disco": disco, "critical_load_kw": [50.0] * 24, "genset_start_control": "none"}, "t")["id"]
    sites.put_connection(sid, {"tariff_category": category, "sanctioned_load_kw": 400, **conn}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 200, "kw_ac": 180}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "t")
    return sid


def _schedule(on: str) -> dict:
    v = tariffs.resolve("schedule", "national", on)
    assert v is not None, on
    return v


def _named(v: dict, first: str, last: str) -> dict:
    return {"version_id": v["id"], "name": v["body"]["name"], "effective_from": v["effective_from"], "first_day": first, "last_day": last}


def test_a_plan_names_the_schedule_version_it_was_priced_with(company):
    sid = _site()
    r = plans.tick(sid, at=T)
    assert r["status"] in ("active", "held"), r.get("reason")
    v = _schedule("2026-11-10")
    assert v["body"]["name"] == "Uniform schedule CY2026 (SRO 279(I)/2026)" and v["effective_from"] == "2026-02-12"
    want = {"disco": "FESCO", "category": "B-2(b)", "version_id": v["id"], "name": "Uniform schedule CY2026 (SRO 279(I)/2026)",
            "effective_from": "2026-02-12", "versions": [_named(v, "2026-11-10", "2026-11-12")]}
    assert r["summary"]["tariff"] == want
    # stored with the plan, and read back as stored
    assert plans.get(sid, r["plan_id"])["summary"]["tariff"] == want
    assert [p["summary"].get("tariff") for p in plans.history(sid)] == [want]


def test_a_horizon_across_a_new_schedules_first_day_names_both_and_the_one_in_force_at_its_start(company):
    """A schedule approved from 11 Nov: the plan's first Karachi day is priced on the schedule of before, its next two on the new one (tariffs.vectors
    prices each day with the versions in force that day). The line names the version in force at the plan's start; `versions` both, in order."""
    sid = _site()
    old = _schedule("2026-11-10")
    body = {**old["body"], "name": "Uniform schedule, a test notification from 11 Nov 2026"}
    new = tariffs.propose("schedule", "national", "2026-11-11", body, {"verified": "unverified", "title": "a test"}, "test")
    tariffs.approve(new["id"], "test")
    r = plans.tick(sid, at=T)
    tariff = r["summary"]["tariff"]
    assert (tariff["version_id"], tariff["name"], tariff["effective_from"]) == (old["id"], old["body"]["name"], "2026-02-12")
    assert tariff["versions"] == [_named(old, "2026-11-10", "2026-11-10"),
                                  {"version_id": new["id"], "name": "Uniform schedule, a test notification from 11 Nov 2026",
                                   "effective_from": "2026-11-11", "first_day": "2026-11-11", "last_day": "2026-11-12"}]


def test_a_horizon_across_two_new_schedules_names_all_three_versions(company):
    """A 48-hour horizon from 07:00 touches three Karachi days, so with a new schedule from each of the next two midnights it is priced on three
    versions: `versions` holds all three, in order, and the page draws a line for each after the first (UX K14 check: the contract said one or two)."""
    sid = _site()
    old = _schedule("2026-11-10")
    eleventh = tariffs.propose("schedule", "national", "2026-11-11", {**old["body"], "name": "A schedule from 11 Nov"}, {"verified": "unverified"}, "t")
    tariffs.approve(eleventh["id"], "test")
    twelfth = tariffs.propose("schedule", "national", "2026-11-12", {**old["body"], "name": "A schedule from 12 Nov"}, {"verified": "unverified"}, "t")
    tariffs.approve(twelfth["id"], "test")
    r = plans.tick(sid, at=T)
    assert r["status"] in ("active", "held"), r.get("reason")
    tariff = r["summary"]["tariff"]
    assert (tariff["version_id"], tariff["name"]) == (old["id"], old["body"]["name"])
    assert tariff["versions"] == [_named(old, "2026-11-10", "2026-11-10"),
                                  {"version_id": eleventh["id"], "name": "A schedule from 11 Nov", "effective_from": "2026-11-11",
                                   "first_day": "2026-11-11", "last_day": "2026-11-11"},
                                  {"version_id": twelfth["id"], "name": "A schedule from 12 Nov", "effective_from": "2026-11-12",
                                   "first_day": "2026-11-12", "last_day": "2026-11-12"}]
    assert plans.get(sid, r["plan_id"])["summary"]["tariff"] == tariff


def test_a_horizon_cut_short_by_a_relaxed_retry_names_only_the_days_it_holds(company):
    """A relaxed retry solves 24 hours of the 48 the prices were made for (optimise.shorten): its tariff names the versions of the days its steps start
    in, and ends on the last of them."""
    from gridlytics import forecast, optimise
    sid = _site()
    old = _schedule("2026-11-10")
    new = tariffs.propose("schedule", "national", "2026-11-12", {**old["body"], "name": "A schedule from 12 Nov"}, {"verified": "unverified"}, "test")
    tariffs.approve(new["id"], "test")
    full = sites.full(sid)
    p = plans.build_problem(full, T, forecast.issue(full, T, save=False), 192)
    assert [(v["first_day"], v["last_day"]) for v in p["tariff"]["versions"]] == [("2026-11-10", "2026-11-11"), ("2026-11-12", "2026-11-12")]
    short = optimise.shorten(p, 96)                                          # 07:00 on 10 Nov to 06:45 on 11 Nov
    cut = plans.tariff_over(short["tariff"], short["ts"])
    assert cut["versions"] == [_named(old, "2026-11-10", "2026-11-11")] and cut["version_id"] == old["id"]
    assert plans.tariff_over(p["tariff"], p["ts"]) == p["tariff"] and plans.tariff_over(None, p["ts"]) is None


def test_a_k_electric_site_on_another_category_is_named_so(company):
    sid = _site(disco="K-Electric", category="B-3")              # the connection's save takes K-Electric's time-of-use windows from the DISCO
    tariff = plans.tick(sid, at=T, steps=96)["summary"]["tariff"]
    assert (tariff["disco"], tariff["category"], tariff["version_id"]) == ("K-Electric", "B-3", _schedule("2026-11-10")["id"])
    assert [(v["first_day"], v["last_day"]) for v in tariff["versions"]] == [("2026-11-10", "2026-11-11")]    # 24 hours from 07:00


def test_an_off_grid_plan_is_priced_on_no_tariff(company):
    sid = sites.create({"name": "Farm", "critical_load_kw": [8.0] * 24, "genset_start_control": "manual", "off_grid": True}, "t")["id"]
    sites.put_connection(sid, {"regime": "off_grid"}, "t")
    sites.put_asset(sid, "pv", {"kwp_dc": 60, "kw_ac": 50}, "t")
    sites.put_asset(sid, "battery", {"nameplate_kwh": 100, "p_ch_kw": 40, "p_dis_kw": 40}, "t")
    sites.put_asset(sid, "genset", {"rated_kw": 48, "fuel_kind": "hsd", "tank_l": 400}, "t")
    r = plans.tick(sid, at=T, steps=96)
    assert "tariff" in r["summary"] and r["summary"]["tariff"] is None


def test_every_reader_reads_the_tariff_line_whole_and_it_holds_no_money(world):
    """The line is words, ids and dates: the money filter leaves it whole for a viewer and a machine key (plan D3.1.4: "Inputs this plan used" is for
    every role), and it holds no money key and no rupee."""
    with world.use():
        r = plans.tick(world.site)
        assert r["status"] == "active", r.get("reason")
        on = datetime.now(PKT).date().isoformat()
        v = _schedule(on)
    key = world.key(["sites:read", "plans:read"])
    seen = {}
    for reader in ("admin", "operator", "analyst", "viewer", "key"):
        got = (world.anon.get(f"/api/sites/{world.site}/plan", headers=key) if reader == "key"
               else world.c[reader].get(f"/api/sites/{world.site}/plan"))
        assert got.status_code == 200, (reader, got.text)
        seen[reader] = got.json()["plan"]["summary"]["tariff"]
        assert seen[reader] == seen["admin"], reader
    tariff = seen["admin"]
    assert (tariff["disco"], tariff["category"], tariff["version_id"], tariff["effective_from"]) == ("FESCO", "B-2(b)", v["id"], v["effective_from"])
    assert tariff["versions"][0]["first_day"] == on

    def keys(obj):
        if isinstance(obj, dict):
            for k, x in obj.items():
                yield k
                yield from keys(x)
        elif isinstance(obj, list):
            for x in obj:
                yield from keys(x)

    assert not [k for k in keys(tariff) if money.is_money(k)]
    assert not any(w in json.dumps(tariff) for w in RUPEES)
