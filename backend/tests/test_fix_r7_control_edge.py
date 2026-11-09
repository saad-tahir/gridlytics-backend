"""Round 7 (audit), package control-edge: what reaches the device and the control state.

Batch 1, what reaches the device:
- a site below closed loop gets no plan at its edge (audit flow-telemetry-plan-control-03, engine-plans-live-11);
- the wrong-plan guard can refuse a plan: the fallback floor, unserved critical load in a forecast outage, the plan's own MDI cap, load shed with the
  grid up, a genset slot with the grid present, an export slot where nothing may be exported, a slot floor under the fallback (audit
  engine-delivery-01, engine-plans-live-05);
- a plan past its valid_until is not in force, not served and not keyed in (audit engine-delivery-24);
- on a day in a native block of the switchback calendar the live site runs its native settings: the tick holds the plan, the edge gets a release,
  the pages say so (audit engine-savings-01, brief-vs-build-01, flow-savings-pilot-bill-05).

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import delivery, forecast, plans, sites, store
from test_fix_api import _fake_plan, world  # noqa: F401  (world is a fixture)
from test_fix_optimiser import _now_t0, _site, _until

PKT = timezone(timedelta(hours=5))


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


def _closed_loop(sid: str) -> None:
    sites.set_tier_capable(sid, "closed_loop")
    sites.set_tier_allowed(sid, "closed_loop", "t", "test")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _messages(sid: str) -> int:
    with store.connect() as con:
        return con.execute("SELECT count(*) FROM plan_messages WHERE site_id = ?", (sid,)).fetchone()[0]


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-03, engine-plans-live-11

@pytest.mark.parametrize("tier", ["monitor", "advisory_writes"])
def test_a_site_below_closed_loop_sends_its_edge_no_plan(company, tier):
    sid = _site(genset=False)
    sites.set_tier_capable(sid, "closed_loop")
    sites.set_tier_allowed(sid, tier, "t", "test")
    assert sites.get(sid)["tier"] == tier
    assert plans.tick(sid)["status"] == "active"
    with pytest.raises(plans.NotClosedLoop) as no:
        plans.signed_message(sid)
    assert no.value.tier == tier
    assert _messages(sid) == 0                                    # nothing was sent, so nothing is counted as a table write
    # a pause at such a site sends nothing either: the edge holds no plan to release
    sites.add_override(sid, {"kind": "pause_control", "until": _until(2), "reason": "test"}, "t")
    with pytest.raises(plans.NotClosedLoop):
        plans.signed_message(sid)
    assert _messages(sid) == 0


def test_a_closed_loop_site_sends_its_edge_the_signed_plan(company):
    sid = _site(genset=False)
    _closed_loop(sid)
    assert plans.tick(sid)["status"] == "active"
    msg = plans.signed_message(sid)
    assert msg["type"] == "plan" and msg["mode_a"]["days"]
    assert delivery.verify(msg, delivery.public_key(sid)["public_key"])
    assert _messages(sid) == 1


def test_the_edge_route_refuses_a_plan_below_closed_loop_and_serves_it_at_closed_loop(world):
    sid = world.site
    _fake_plan(world)
    kh = world.key(["plans:read"], role="viewer")
    r = world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh)
    assert r.status_code == 409 and r.json()["code"] == "not_closed_loop", r.text
    assert "monitor" in r.json()["detail"] and "no plan is sent" in r.json()["detail"]
    with world.use():
        assert _messages(sid) == 0
        _closed_loop(sid)
    r = world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh)
    assert r.status_code == 200 and r.json()["type"] == "plan", r.text


# ------------------------------------------------------------------------------------------------ engine-delivery-01, engine-plans-live-05

T = 96


def _guard_case(sid: str, load=300.0, pv=0.0, crit=50.0, g=None, soc0=0.6, x_max=0.0, mdi=None) -> tuple[dict, dict]:
    """A hand-made problem for plans._guard at the test site (200 kWh, 100 kW battery): flat forecasts over one day of quarters."""
    full = sites.full(sid)
    b = full["assets"]["battery"]
    e_nom = b["nameplate_kwh"] * b.get("soh", 1.0)
    p = {"T": T, "ts": _now_t0() + 900 * np.arange(T), "PV": np.full(T, float(pv)), "L": np.broadcast_to(np.asarray(load, dtype=float), (T,)).copy(),
         "Lc": np.full(T, float(crit)),
         "g": np.ones(T) if g is None else np.asarray(g, dtype=float), "X_max": float(x_max), "off_grid": False,
         "battery": {"E_nom": e_nom, "E0": soc0 * e_nom, "soc_min": b["soc_min"], "soc_max": b["soc_max"]}}
    if mdi:
        p["mdi"] = mdi
    return full, p


def _slot(p: dict, i0: int, i1: int, mode="self_use", floor=20.0, target=100.0, cap_kw=None, genset=None, max_dis_kw=None) -> dict:
    return {"start_ts": int(p["ts"][i0]), "end_ts": int(p["ts"][i1 - 1] + 900), "mode": mode, "soc_floor_pct": floor, "soc_target_pct": target,
            "max_discharge_w": None if max_dis_kw is None else max_dis_kw * 1000, "max_charge_w": None, "grid_charge": mode == "charge_grid",
            "grid_cap_w": None if cap_kw is None else cap_kw * 1000, "genset": genset, "export_allowed": mode == "export"}


def _table(*slots, floor=20.0) -> dict:
    return {"days": [{"date": "2026-11-09", "slots": list(slots)}], "fallback": {"on_expiry": "inverter_native", "min_soc_floor_pct": floor}}


def _steps(p: dict, unserved=None, imp=None) -> list[dict]:
    return [{"t": int(p["ts"][i]), "grid_present": float(p["g"][i]), "unserved_kw": float(unserved[i]) if unserved is not None else 0.0,
             "grid_import_kw": float(imp[i]) if imp is not None else float(p["L"][i])} for i in range(T)]


def test_the_guard_passes_a_table_that_keeps_every_rule(company):
    full, p = _guard_case(_site(genset=False))
    assert plans._guard(full, p, _table(_slot(p, 0, T, "self_use", floor=20.0)), _steps(p)) is None


def test_the_guard_refuses_a_slot_floor_under_the_fallback(company):
    full, p = _guard_case(_site(genset=False))
    assert plans._guard(full, p, _table(_slot(p, 0, T, "hold", floor=12.0)), _steps(p)) == "slot_floor_below_fallback"


def test_the_guard_refuses_a_table_that_drains_the_battery_under_the_fallback_floor_with_the_grid_up(company):
    # a slot whose grid cap is below the load: the device shaves the peak from the battery down to its own floor (10 %), through the 20 % fallback
    full, p = _guard_case(_site(genset=False), load=300.0, soc0=0.5)
    table = _table(_slot(p, 0, T, "self_use", floor=20.0, cap_kw=200.0))
    assert plans._guard(full, p, table, _steps(p)) == "replayed_soc_below_floor"


def test_the_guard_refuses_a_table_that_leaves_critical_load_unserved_in_a_forecast_outage(company):
    # an export slot empties the battery to its 20 % floor before a 3-hour outage of 50 kW critical load the plan meant to carry
    g = np.ones(T)
    g[40:52] = 0.0
    full, p = _guard_case(_site(genset=False), load=60.0, crit=50.0, g=g, soc0=0.9, x_max=200.0)
    table = _table(_slot(p, 0, 40, "export", floor=20.0), _slot(p, 40, T, "self_use", floor=20.0))
    assert plans._guard(full, p, table, _steps(p)) == "unserved_in_outage"
    # the plan's own table, which holds the battery before the outage, carries the critical load and passes
    held = _table(_slot(p, 0, 40, "hold", floor=90.0), _slot(p, 40, T, "self_use", floor=20.0))
    assert plans._guard(full, p, held, _steps(p)) is None


def test_the_guard_refuses_a_plan_whose_own_grid_draw_exceeds_its_mdi_cap(company):
    # load 250 kW, 300 kW from step 80 to 88, and an MDI cap of 260 kW: a plan that discharges 40 kW there keeps its draw under the cap and passes;
    # one that meets the cap by shedding 40 kW with the grid up is refused (the meter reads the load the site serves), as is one whose own
    # import over a 30-minute block exceeds the cap
    mdi = {"rate": 1500.0, "M0": 0.0, "floor": 0.0, "n_mdi": 2, "G_elapsed": 0.0, "n_elapsed": 0, "month_end_step": T}
    load = np.full(T, 250.0)
    load[80:88] = 300.0
    full, p = _guard_case(_site(genset=False), load=load, soc0=0.9, mdi=mdi)
    imp = np.minimum(load, 260.0)
    table = _table(_slot(p, 0, 80, "hold", floor=90.0, cap_kw=260.0), _slot(p, 80, 88, "discharge", floor=20.0, cap_kw=260.0, max_dis_kw=40.0),
                   _slot(p, 88, T, "hold", floor=50.0, cap_kw=260.0))
    assert plans._guard(full, p, table, _steps(p, imp=imp), {"M": [260.0]}) is None
    shed = np.where(load > 260.0, load - 260.0, 0.0)
    assert plans._guard(full, p, table, _steps(p, imp=imp, unserved=shed), {"M": [260.0]}) == "mdi_cap_exceeded"
    over = imp.copy()
    over[82:84] = 290.0
    assert plans._guard(full, p, table, _steps(p, imp=over), {"M": [260.0]}) == "mdi_cap_exceeded"
    # one 15-minute step over the cap inside a block whose mean is under it is not a new maximum demand
    i = next(k for k in range(80, 86) if int(p["ts"][k]) % 1800 == 0)       # the first quarter of a 30-minute block
    one = imp.copy()
    one[i], one[i + 1] = 280.0, 230.0
    assert plans._guard(full, p, table, _steps(p, imp=one), {"M": [260.0]}) is None
    # at a site with no MDI the shed is still refused, for itself
    assert plans._guard(full, {k: v for k, v in p.items() if k != "mdi"}, table, _steps(p, imp=imp, unserved=shed), {}) == "sheds_load_with_grid_up"


def test_the_guard_refuses_a_plan_that_sheds_load_with_the_grid_up(company):
    # the plan shows 30 kW unserved on grid-up steps: the site serves its load whenever the grid is up, so it would import that instead
    full, p = _guard_case(_site(genset=False), load=300.0)
    unserved = np.zeros(T)
    unserved[60:72] = 30.0
    assert plans._guard(full, p, _table(_slot(p, 0, T, "self_use")), _steps(p, unserved=unserved)) == "sheds_load_with_grid_up"
    # load the grid cannot carry (above the connection's limit) is shed by the site too, so the replay sheds it and the plan is not refused
    big = 600.0
    full, p = _guard_case(_site(genset=False), load=big, soc0=0.15)
    over = np.full(T, big - 500.0)               # sanctioned 400 kW x 1.25
    assert plans._guard(full, p, _table(_slot(p, 0, T, "self_use")), _steps(p, unserved=over)) is None


def test_the_guard_refuses_a_genset_slot_where_the_grid_is_expected_present(company):
    sid = _site(genset_control="manual")
    full, p = _guard_case(sid, load=200.0)
    table = _table(_slot(p, 0, 40, "self_use"), _slot(p, 40, 48, "self_use", genset=True), _slot(p, 48, T, "self_use"))
    assert plans._guard(full, p, table, _steps(p)) == "genset_slot_with_grid"
    # the same slot over a forecast outage is what a genset slot is for
    g = np.ones(T)
    g[40:48] = 0.0
    full, p = _guard_case(sid, load=200.0, g=g)
    table = _table(_slot(p, 0, 40, "self_use"), _slot(p, 40, 48, "self_use", genset=True), _slot(p, 48, T, "self_use"))
    assert plans._guard(full, p, table, _steps(p)) is None


def test_the_guard_refuses_an_export_slot_where_nothing_may_be_exported(company):
    full, p = _guard_case(_site(genset=False), load=50.0, soc0=0.9, x_max=0.0)
    table = _table(_slot(p, 0, 70, "export", floor=60.0), _slot(p, 70, T, "self_use"))
    assert plans._guard(full, p, table, _steps(p)) == "export_not_allowed"


def test_a_tick_passes_the_solves_mdi_cap_to_the_guard(company, monkeypatch):
    sid = _site(genset=False)
    seen = {}
    real = plans._guard

    def spy(site, p, compiled, steps_out, res=None):
        seen["res"] = res
        return real(site, p, compiled, steps_out, res)

    monkeypatch.setattr(plans, "_guard", spy)
    r = plans.tick(sid)
    assert r["status"] == "active" and r["summary"]["guard"] is None
    assert seen["res"] is not None and "M" in seen["res"]


# ------------------------------------------------------------------------------------------------ engine-delivery-24

def _expired_plan(sid: str, hours_ago: float = 1.0) -> str:
    """An active plan of two days whose valid_until passed `hours_ago` hours ago (the ticks stopped)."""
    t0 = (_now() - int((48 + hours_ago) * 3600)) // 900 * 900
    steps = [{"t": t0 + 900 * i, "local": "00:00", "reason": "idle", "detail": "", "genset_on": False, "peak": False, "discharge_kw": 0.0,
              "charge_kw": 0.0} for i in range(192)]
    pid = "expired-" + sid[:6]
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, summary, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, sid, _iso(t0), _iso(t0), _iso(t0), _iso(t0 + 192 * 900), "a", "active",
                                                       json.dumps({"status_label": "Solved within tolerance"}), json.dumps(steps),
                                                       json.dumps({"days": [{"date": "2026-11-07", "slots": []}], "fallback": None})))
    return pid


def test_a_plan_past_its_valid_until_is_not_in_force_nor_served(company):
    sid = _site(genset=False)
    _closed_loop(sid)
    pid = _expired_plan(sid)
    assert plans.current(sid) is None
    vu = plans._ts(plans.get(sid, pid)["valid_until"])
    h = plans.held_info(sid)
    assert h == {"reason": "plan_expired", "until": None, "plan_id": pid, "since": plans.get(sid, pid)["valid_until"]}
    assert plans.signed_message(sid) is None and _messages(sid) == 0
    assert plans.active_at(sid, vu - 60)["id"] == pid                # in force up to its end
    assert plans.active_at(sid, vu + 60) is None                     # and not after it
    # the next good tick puts a plan in force again
    assert plans.tick(sid)["status"] == "active" and plans.current(sid)["id"] != pid and plans.held_info(sid) is None


def test_the_plan_and_control_pages_and_the_edge_see_an_expired_plan_as_no_plan(world):
    sid = world.site
    with world.use():
        _closed_loop(sid)
        pid = _expired_plan(sid)
    a = world.c["admin"]
    body = a.get(f"/api/sites/{sid}/plan").json()
    assert body["plan"] is None and body["held"]["reason"] == "plan_expired" and body["held"]["plan_id"] == pid
    assert body["held"]["until"] is None and plans._ts(body["held"]["since"]) < _now()        # when it ran out
    ctl = a.get(f"/api/sites/{sid}/control").json()
    assert ctl["compiled"] is None and ctl["plan_id"] is None        # no expired table offered for keying in
    kh = world.key(["plans:read"], role="viewer")
    r = world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh)
    assert r.status_code == 404 and r.json()["code"] == "plan_expired", r.text


# ------------------------------------------------------------------------------------------------ engine-savings-01, brief-vs-build-01, flow-savings-pilot-bill-05

def _block(sid: str, start: date, end: date, arm: str, locked: int = 1) -> None:
    with store.connect() as con:
        con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm, locked) VALUES (?,?,?,?,?)",
                    (sid, start.isoformat(), end.isoformat(), arm, locked))


def _today() -> date:
    return datetime.now(PKT).date()


def test_a_native_block_day_releases_the_live_site_to_its_native_settings(company):
    sid = _site(genset=False)
    _closed_loop(sid)
    assert plans.tick(sid)["status"] == "active"
    today = _today()
    _block(sid, today - timedelta(days=2), today + timedelta(days=1), "native")
    rel = plans.control_release(sid)
    assert rel["reason"] == "switchback_native" and rel["kind"] == "switchback" and rel["override_id"] is None
    end = datetime(*(today + timedelta(days=2)).timetuple()[:3], tzinfo=PKT)          # the block's last day ends at midnight Pakistan time
    assert plans._ts(rel["until"]) == int(end.timestamp())
    r = plans.tick(sid)
    assert r["status"] == "held"
    with store.connect() as con:
        assert con.execute("SELECT reason FROM plans WHERE id = ?", (r["plan_id"],)).fetchone()["reason"] == "switchback_native"
    assert plans.current(sid) is None                                                  # the plan block's plan stops being in force
    assert plans.active_at(sid, _now()) is None
    h = plans.held_info(sid)
    assert h["reason"] == "switchback_native" and h["plan_id"] == r["plan_id"] and plans._ts(h["until"]) == int(end.timestamp())
    msg = plans.signed_message(sid)
    assert msg["type"] == "release" and msg["release"]["reason"] == "switchback_native" and msg["mode_a"]["days"] == []
    assert delivery.verify(msg, delivery.public_key(sid)["public_key"])


def test_a_plan_block_day_runs_the_plan_and_a_persons_pause_comes_before_the_calendar(company):
    sid = _site(genset=False)
    today = _today()
    _block(sid, today, today + timedelta(days=6), "plan")
    assert plans.control_release(sid) is None and plans.tick(sid)["status"] == "active"
    other = _site(genset=False)
    _block(other, today, today + timedelta(days=6), "native")
    sites.add_override(other, {"kind": "pause_control", "until": _until(2), "reason": "test"}, "t")
    assert plans.control_release(other)["reason"] == "control_paused"


def test_the_plan_and_control_pages_say_a_native_block_day(world):
    sid = world.site
    with world.use():
        _block(sid, _today(), _today() + timedelta(days=3), "native")
    a = world.c["admin"]
    body = a.get(f"/api/sites/{sid}/plan").json()
    assert body["plan"] is None and body["held"]["reason"] == "switchback_native"
    ctl = a.get(f"/api/sites/{sid}/control").json()["control"]
    assert ctl["key"] == "native_block" and plans._ts(ctl["until"]) is not None
    assert "native" in ctl["label"].lower()
