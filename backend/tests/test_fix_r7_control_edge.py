"""Round 7 (audit), package control-edge: what reaches the device and the control state.

Batch 1, what reaches the device:
- a site below closed loop gets no plan at its edge (audit flow-telemetry-plan-control-03, engine-plans-live-11);
- the wrong-plan guard can refuse a plan: the fallback floor, unserved critical load in a forecast outage, the plan's own MDI cap, load shed with the
  grid up, a genset slot with the grid present, an export slot where nothing may be exported, a slot floor under the fallback (audit
  engine-delivery-01, engine-plans-live-05);
- a plan past its valid_until is not in force, not served and not keyed in (audit engine-delivery-24);
- on a day in a native block of the switchback calendar the live site runs its native settings: the tick holds the plan, the edge gets a release,
  the pages say so (audit engine-savings-01, brief-vs-build-01, flow-savings-pilot-bill-05).

Batch 2, the edge protocol:
- a message is a duplicate only when the same message (its stream, sequence and content) was stored before: a batch below the high-water mark that
  was never stored is stored, a reset counter is stored, and the newest capabilities message is the current one (audit engine-delivery-03);
- the models take U3's own contract: the envelope, its event names, unknown event types, partial acks, heartbeat states, the telemetry shape, and the
  plan message carries supersedes, capabilities_seq, targets and msg_id (audit engine-delivery-11);
- the contract is exported as JSON Schema files pinned by a drift test, the capabilities name each device's writable primitives, and the signed
  plan carries only what the device can write (audit brief-vs-build-12, tracker-and-docs-04);
- what the edge reports is acted on: rejected or partial acks, control_disabled, firmware_change and the heartbeat's state show in the control
  state, pause control where they leave the device unknown, and a closed-loop day the edge did not follow claims nothing (audit engine-delivery-12);
- the clock-offset ladder is real: clock_unsynced at 120 s, the peak slots widened by 15 minutes at 300 s, control demoted until the offset is under
  120 s at 900 s (audit engine-delivery-10).

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from gridlytics import delivery, forecast, plans, sites, store, telemetry
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


# ------------------------------------------------------------------------------------------------ engine-delivery-03 (batch 2)

def _env(seq: int, sent_at: str = "2026-11-06T10:00:00Z", **body) -> dict:
    return {"contract": "ems-edge/1.0", "seq": seq, "sent_at": sent_at, **body}


def _tel(seq: int, minute: int, pv: float = 900.0, **env) -> dict:
    return _env(seq, columns=["ts", "grid_present", "pv_power_w"], rows=[[minute, 1, pv]], **env)


def test_a_batch_below_the_mark_that_was_never_stored_is_stored_and_a_true_repeat_is_a_duplicate(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    m = _now() // 60 * 60 - 600
    # seq 10 timed out before it was stored; seq 11 went through; the retry of 10 comes after it
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_tel(11, m + 60))
    assert r.status_code == 200 and r.json()["accepted"] == 1, r.text
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_tel(10, m, pv=700.0))
    assert r.status_code == 200 and r.json().get("accepted") == 1 and not r.json().get("duplicate"), r.text
    with world.use():
        assert telemetry.minute_grid(sid, m, m + 60)["pv"][0] == pytest.approx(0.7)
    # the same message again (a lost answer): acknowledged, not written twice
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_tel(10, m, pv=700.0))
    assert r.json() == {"ack": 10, "duplicate": True}, r.text
    # a retry sent at a later time is the same message: sent_at and msg_id do not make it a new one
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_tel(10, m, pv=700.0, sent_at="2026-11-06T10:05:00Z"))
    assert r.json().get("duplicate") is True, r.text


def test_a_reset_sequence_is_stored_not_acknowledged_as_a_duplicate(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    m = _now() // 60 * 60 - 900
    assert world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_tel(48213, m)).json()["accepted"] == 1
    gap = {"ts": _iso(m), "type": "data_gap"}
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_env(5532, events=[gap]))
    assert r.json()["stored"] == 1, r.text
    # the box was replaced (or lost its counter): it starts again at 0; its readings and its safety events are stored
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=_tel(0, m + 60))
    assert r.status_code == 200 and r.json().get("accepted") == 1 and not r.json().get("duplicate"), r.text
    mismatch = {"ts": _iso(m + 60), "type": "readback_mismatch", "detail": {"primitive": "soc_floor_pct"}}
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_env(0, events=[mismatch]))
    assert r.status_code == 200 and r.json().get("stored") == 1 and not r.json().get("duplicate"), r.text
    # its retry, sent again later, is the same message: one pause, one stored event
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_env(0, sent_at="2026-11-06T10:09:00Z", events=[mismatch]))
    assert r.json().get("duplicate") is True, r.text
    with world.use():
        assert [o["kind"] for o in sites.overrides(sid)] == ["pause_control"]
        with store.connect() as con:
            assert con.execute("SELECT count(*) FROM events WHERE site_id = ? AND type = 'readback_mismatch'", (sid,)).fetchone()[0] == 1


def test_an_ack_is_stored_once_however_often_it_is_sent(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    ack = _env(3, plan_id="p1", status="applied")
    assert world.anon.post(f"/api/edge/{sid}/ack", headers=kh, json=ack).json() == {"ack": 3}
    assert world.anon.post(f"/api/edge/{sid}/ack", headers=kh, json=ack).json() == {"ack": 3, "duplicate": True}
    with world.use(), store.connect() as con:
        assert con.execute("SELECT count(*) FROM acks WHERE site_id = ?", (sid,)).fetchone()[0] == 1


def test_the_newest_capabilities_message_is_current_whatever_its_sequence(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    url = f"/api/edge/{sid}/capabilities"
    assert world.anon.post(url, headers=kh, json=_env(17, "2026-11-06T00:00:10Z", edge_version="p2-0.4.2", tier="closed_loop")).status_code == 200
    # the edge restarted its counter: the boot message (seq 0) is the current one, and the old one stays in the history
    r = world.anon.post(url, headers=kh, json=_env(0, "2026-11-07T00:00:10Z", edge_version="p2-0.4.3", tier="advisory_writes"))
    assert r.status_code == 200 and r.json()["tier_capable"] == "advisory_writes", r.text
    # a late retry of an older message is stored for the record but does not set the tier
    r = world.anon.post(url, headers=kh, json=_env(16, "2026-11-05T00:00:10Z", edge_version="p2-0.4.1", tier="closed_loop"))
    assert r.status_code == 200 and r.json()["tier_capable"] == "advisory_writes", r.text
    with world.use():
        assert sites.get(sid)["tier_capable"] == "advisory_writes"
        with store.connect() as con:
            assert con.execute("SELECT count(*) FROM capabilities WHERE site_id = ?", (sid,)).fetchone()[0] == 3
    e = world.c["admin"].get(f"/api/sites/{sid}/edge").json()
    assert e["capabilities"]["edge_version"] == "p2-0.4.3" and e["capabilities"]["seq"] == 0
    # the same boot message again is a duplicate (and a second reset to 0 with new content is stored)
    assert world.anon.post(url, headers=kh, json=_env(0, "2026-11-07T00:00:10Z", edge_version="p2-0.4.3", tier="advisory_writes")).json()["duplicate"]


def test_a_schema_2_file_keeps_its_capabilities_history_when_migrated(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    path = tmp_path / "old.db"
    with closing(sqlite3.connect(path)) as con, con:
        for sql in store._V1:
            con.execute(sql)
        con.execute("ALTER TABLE reports ADD COLUMN preliminary INTEGER")
        con.execute("INSERT INTO capabilities (site_id, seq, received_at, body) VALUES ('s', 4, '2026-11-01T00:00:00+00:00', '{\"seq\": 4}')")
        con.execute("PRAGMA user_version = 2")
    with store.use_company(path), store.connect() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
        rows = [dict(r) for r in con.execute("SELECT site_id, seq, digest, sent_ts, body FROM capabilities")]
        assert rows == [{"site_id": "s", "seq": 4, "digest": "", "sent_ts": None, "body": '{"seq": 4}'}]
        assert con.execute("SELECT count(*) FROM edge_seen").fetchone()[0] == 0


# ------------------------------------------------------------------------------------------------ engine-delivery-11 (batch 2)

from pathlib import Path  # noqa: E402

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts" / "ems-edge" / "1.0"


def _example(name: str) -> dict:
    return json.loads((CONTRACTS / "examples" / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,model", [("telemetry", "TelemetryBatch"), ("capabilities", "Capabilities"), ("events", "EventBatch"),
                                        ("ack", "Ack"), ("heartbeat", "Heartbeat")])
def test_every_u3_section_5_example_validates(name, model):
    getattr(delivery, model).model_validate(_example(name))


def test_the_u3_telemetry_shape_is_stored(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    msg = _example("telemetry")
    t0 = _now() // 60 * 60 - 600
    msg.update(site_id=sid, ts_start=_iso(t0))
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=msg)
    assert r.status_code == 200 and r.json()["accepted"] == 2, r.text
    # the U3 1.2 points the cloud does not keep are named, not refused
    assert set(r.json()["ignored_columns"]) == {"grid_present_min", "battery_trust_score"}, r.text
    with world.use():
        g = telemetry.minute_grid(sid, t0, t0 + 120)
    assert list(g["pv"]) == pytest.approx([12.4, 12.1]) and list(g["grid"]) == pytest.approx([1.0, 1.0])
    # U3's row count must agree with its data
    bad = {**msg, "seq": msg["seq"] + 1, "rows": 3}
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=bad)
    assert r.status_code == 422 and "rows" in r.json()["detail"], r.text


def test_an_envelope_naming_another_site_is_refused(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    for path, body in (("telemetry", _tel(1, _now() // 60 * 60 - 60)), ("heartbeat", _env(1, uptime_s=5)),
                       ("events", _env(1, events=[{"ts": _iso(_now()), "type": "data_gap"}])), ("ack", _env(1, plan_id="p", status="received")),
                       ("capabilities", _env(1, edge_version="1", tier="closed_loop"))):
        r = world.anon.post(f"/api/edge/{sid}/{path}", headers=kh, json={**body, "site_id": world.other})
        assert r.status_code == 403 and r.json()["code"] == "wrong_site", (path, r.text)
        r = world.anon.post(f"/api/edge/{sid}/{path}", headers=kh, json={**body, "site_id": sid, "msg_type": "telemetry" if path != "telemetry" else "events"})
        assert r.status_code == 422, (path, r.text)                    # a message sent to the wrong path
    with world.use():
        assert sites.get(sid)["tier_capable"] == "monitor" and sites.overrides(sid) == []


def test_u3_event_names_act_and_an_unknown_type_does_not_void_the_batch(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    msg = _example("events")
    msg["site_id"] = sid
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=msg)
    assert r.status_code == 200 and r.json()["stored"] == 5, r.text
    with world.use():
        assert [o["kind"] for o in sites.overrides(sid)] == ["pause_control"]       # override_local is U3's local_override
        with store.connect() as con:
            assert con.execute("SELECT source FROM events WHERE site_id = ? AND type = 'genset_start'", (sid,)).fetchone()["source"] == "edge:gen1"
    later = _env(9, events=[{"ts": _iso(_now()), "type": "battery_contactor_open", "source": "bat1"}, {"ts": _iso(_now()), "type": "data_gap"}])
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=later)
    assert r.status_code == 200 and r.json()["stored"] == 2 and r.json()["unknown_types"] == ["battery_contactor_open"], r.text
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_env(10, events=[{"ts": _iso(_now()), "type": "Not a type!"}]))
    assert r.status_code == 422, r.text


def test_a_partial_ack_and_u3_heartbeat_states_are_taken(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    ack = {**_example("ack"), "site_id": sid}
    assert world.anon.post(f"/api/edge/{sid}/ack", headers=kh, json=ack).status_code == 200
    for n, state in enumerate(("executing", "fallback", "released", "disabled", "following_plan", "native")):
        r = world.anon.post(f"/api/edge/{sid}/heartbeat", headers=kh, json={**_example("heartbeat"), "site_id": sid, "seq": n, "control_state": state})
        assert r.status_code == 200, (state, r.text)
    with world.use(), store.connect() as con:
        assert con.execute("SELECT status FROM acks WHERE site_id = ?", (sid,)).fetchone()["status"] == "partial"


def test_the_signed_plan_carries_the_u3_envelope_and_names_what_it_supersedes(company):
    sid = _site(genset=False)
    _closed_loop(sid)
    with store.connect() as con:
        con.execute("INSERT INTO capabilities (site_id, seq, digest, received_at, sent_ts, body) VALUES (?,?,?,?,?,?)",
                    (sid, 17, "d", store._now(), _now(), json.dumps({**_example("capabilities"), "site_id": sid})))
    assert plans.tick(sid)["status"] == "active"
    first = plans.signed_message(sid)
    assert first["msg_type"] == "plan" and first["type"] == "plan" and first["site_id"] == sid and first["edge_id"] == "edge-00a1b2"
    assert len(first["msg_id"]) >= 16 and first["seq"] == 1 and first["sent_at"]
    assert first["capabilities_seq"] == 17 and first["targets"] == [{"device_id": "inv1"}] and first["supersedes"] is None
    delivery.PlanMessage.model_validate(first)
    with store.connect() as con:                     # a newer plan replaces it
        con.execute("UPDATE plans SET generated_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (first["plan_id"],))
    assert plans.tick(sid)["status"] == "active"
    second = plans.signed_message(sid)
    assert second["plan_id"] != first["plan_id"] and second["supersedes"] == first["plan_id"] and second["seq"] == 2
    assert second["msg_id"] != first["msg_id"]


# ------------------------------------------------------------------------------------------------ brief-vs-build-12, tracker-and-docs-04 (batch 2)

def test_the_published_schema_files_are_the_contract_models():
    from gridlytics import contract
    files = contract.schema_files()
    assert set(files) == {f"{n}.schema.json" for n in ("telemetry", "heartbeat", "events", "ack", "capabilities", "plan")}
    for name, text in files.items():
        assert (CONTRACTS / name).read_text(encoding="utf-8") == text, f"{name} drifted: run python scripts/export_contract.py"
        schema = json.loads(text)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    # the capabilities schema names each device's writable primitives, with their range, step, persistence and budget
    caps = json.loads(files["capabilities.schema.json"])
    assert set(caps["$defs"]["Primitive"]["properties"]) == {"values", "min", "max", "step", "persistence", "writes_per_day_max"}
    assert "primitives" in caps["$defs"]["Device"]["properties"]


def test_the_export_script_writes_the_files_and_its_check_finds_a_drift(tmp_path):
    import subprocess
    import sys
    script = CONTRACTS.parents[2] / "scripts" / "export_contract.py"
    run = lambda *a: subprocess.run([sys.executable, str(script), "--out", str(tmp_path), *a], capture_output=True, text=True)
    assert run().returncode == 0
    assert run("--check").returncode == 0
    (tmp_path / "ack.schema.json").write_text("{}\n", encoding="utf-8")
    r = run("--check")
    assert r.returncode == 1 and "ack.schema.json" in r.stderr
    assert (tmp_path / "ack.schema.json").read_text(encoding="utf-8") == "{}\n"     # --check writes nothing


def _caps(sid: str, primitives: dict | None, max_slots: int | None = None, seq: int = 17) -> None:
    body = _example("capabilities")
    inv = body["devices"][0]
    if primitives is None:
        inv.pop("primitives")
    else:
        inv["primitives"] = primitives
    if max_slots is not None:
        inv["slot_model"]["max_slots"] = max_slots
    with store.connect() as con:
        con.execute("INSERT INTO capabilities (site_id, seq, digest, received_at, sent_ts, body) VALUES (?,?,?,?,?,?)",
                    (sid, seq, f"d{seq}", store._now(), _now() + seq, json.dumps(body)))


def _plan_slots(sid: str, slots: list[dict]) -> str:
    t0 = _now() // 900 * 900
    day = datetime.fromtimestamp(t0, PKT).date()
    mid = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    out = []
    for s in slots:
        a, b = mid + s.pop("from_h") * 3600, mid + s.pop("to_h") * 3600
        out.append({"start": datetime.fromtimestamp(a, PKT).strftime("%H:%M"), "end": datetime.fromtimestamp(b, PKT).strftime("%H:%M"),
                    "start_ts": a, "end_ts": b, "soc_floor_pct": 20.0, "soc_target_pct": 100.0, "max_discharge_w": None, "max_charge_w": None,
                    "grid_charge": False, "charge_end_ts": None, "charge_end": None, "grid_cap_w": None, "gen_charge": False, "export_allowed": False,
                    "genset": None, "intent": "", **s})
    pid = "plan-" + store.new_id(6)
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, objective_rs, summary, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (pid, sid, _iso(t0), _iso(t0), _iso(t0), _iso(t0 + 86400), "a", "active", 1.0,
                                                         json.dumps({"status_label": "Following"}), "[]",
                                                         json.dumps({"brand": "deye", "grammar": {"min_slot_minutes": 30},
                                                                     "days": [{"date": day.isoformat(), "slots": out}],
                                                                     "fallback": {"on_expiry": "inverter_native", "min_soc_floor_pct": 20}})))
    return pid


THREE = [{"from_h": 0, "to_h": 5, "mode": "charge_grid", "grid_charge": True, "max_charge_w": 9000, "soc_target_pct": 95.0},
         {"from_h": 5, "to_h": 17, "mode": "self_use"},
         {"from_h": 17, "to_h": 24, "mode": "discharge", "max_discharge_w": 20000, "soc_floor_pct": 5.0}]


def test_the_signed_plan_carries_only_what_the_device_can_write(company):
    sid = _site(genset=False)
    _closed_loop(sid)
    _plan_slots(sid, [dict(s) for s in THREE])
    prims = {"battery_mode": {"values": ["self_use", "hold", "charge", "native"]}, "battery_power_w": {"min": -6000, "max": 6000, "step": 10},
             "soc_floor_pct": {"min": 10, "max": 100, "step": 1}, "soc_target_pct": {"min": 10, "max": 100, "step": 1}}
    _caps(sid, prims)
    msg = plans.signed_message(sid)
    a, _, c = msg["mode_a"]["days"][0]["slots"]
    assert a["grid_charge"] is False and a["max_charge_w"] == 6000                 # no grid-charge primitive; the charge power in range
    assert c["max_discharge_w"] == 6000 and c["soc_floor_pct"] == 10               # the floor raised to the device's lowest
    assert msg["capabilities_fit"] == {"device_id": "inv1", "capabilities_seq": 17, "not_writable": ["grid_charge_allowed"],
                                       "clamped": ["battery_power_w", "soc_floor_pct"]}
    # the plan page keeps the optimiser's table; only the message is fitted
    assert plans.current(sid)["slots"]["days"][0]["slots"][0]["grid_charge"] is True


def test_a_device_that_reports_no_primitives_gets_the_table_unchanged_and_too_many_slots_are_not_signed(company):
    sid = _site(genset=False)
    _closed_loop(sid)
    _plan_slots(sid, [dict(s) for s in THREE])
    _caps(sid, None)
    msg = plans.signed_message(sid)
    assert msg["mode_a"]["days"][0]["slots"][0]["grid_charge"] is True and msg.get("capabilities_fit") is None
    _caps(sid, {"grid_charge_allowed": {}, "battery_power_w": {"min": -20000, "max": 20000}}, max_slots=2, seq=18)
    with pytest.raises(plans.CapsMismatch) as no:
        plans.signed_message(sid)
    assert "3 slots" in str(no.value) and "2" in str(no.value)


# ------------------------------------------------------------------------------------------------ engine-delivery-12 (batch 2)

def _control(w, sid: str) -> dict:
    from gridlytics import routes_sites
    with w.use():
        return routes_sites.control_state(sites.get(sid), {"ts": _now()}, _now())


def test_a_firmware_or_template_change_pauses_control_under_either_name(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    for n, kind in enumerate(("firmware_change", "firmware_change_detected", "template_change_detected")):
        r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_env(n + 1, events=[{"ts": _iso(_now()), "type": kind}]))
        assert r.status_code == 200, r.text
        with world.use():
            assert [o["kind"] for o in sites.overrides(sid)] == ["pause_control"] * (n + 1), kind


def test_the_control_state_says_what_the_edge_reports(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    pid = _fake_plan(world)
    with world.use():
        _closed_loop(sid)
    assert _control(world, sid)["key"] == "following"
    beat = lambda n, state: world.anon.post(f"/api/edge/{sid}/heartbeat", headers=kh, json=_env(n, uptime_s=60, control_state=state))
    assert beat(1, "fallback").status_code == 200
    c = _control(world, sid)
    assert c["key"] == "edge_fallback" and "own settings" in c["label"], c
    beat(2, "executing")
    assert _control(world, sid)["key"] == "following"
    beat(3, "disabled")
    assert _control(world, sid)["key"] == "edge_disabled"
    beat(4, "following_plan")
    # control disabled by the edge (a fault): shown until the edge says it is enabled again
    ev = lambda n, kind, at, **d: world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_env(n, events=[{"ts": _iso(at), "type": kind, "detail": d}]))
    assert ev(1, "control_disabled", _now() - 60, cause="inverter fault").status_code == 200
    c = _control(world, sid)
    assert c["key"] == "edge_disabled" and "inverter fault" in c["label"], c
    ev(2, "control_disabled", _now() - 3600)                       # an older report sent late does not change it
    ev(3, "control_enabled", _now() - 30)
    assert _control(world, sid)["key"] == "following"
    # the edge refused the plan in force, or applied part of it
    ack = lambda n, status, **k: world.anon.post(f"/api/edge/{sid}/ack", headers=kh, json=_env(n, plan_id=pid, status=status, **k))
    assert ack(1, "rejected", reason="CAPS_STALE").status_code == 200
    c = _control(world, sid)
    assert c["key"] == "edge_refused" and "CAPS_STALE" in c["label"], c
    assert world.c["viewer"].get(f"/api/sites/{sid}/edge").json()["reports"] == c          # the edge card says it too
    ack(2, "partial", per_device=[{"device_id": "inv1", "status": "partial", "rejected": [{"primitive": "gen_charge_allowed", "reason": "NOT_SUPPORTED"}]}])
    c = _control(world, sid)
    assert c["key"] == "edge_partial" and "NOT_SUPPORTED" in c["label"], c
    ack(3, "applied")
    assert _control(world, sid)["key"] == "following"
    # an ack for another plan says nothing of the plan in force
    world.anon.post(f"/api/edge/{sid}/ack", headers=kh, json=_env(4, plan_id="older", status="rejected"))
    assert _control(world, sid)["key"] == "following"
    # a heartbeat older than five minutes no longer speaks for the edge
    with world.use():
        sites.put_state(sid, edge_heartbeat={"control_state": "fallback"}, edge_heartbeat_at=_now() - 600)
    assert _control(world, sid)["key"] == "following"


def test_a_closed_loop_day_the_edge_did_not_follow_claims_nothing(company):
    from test_fix_savings import make_site, twin_day
    from gridlytics import verify
    day = date(2026, 10, 14)
    sid = make_site(name="Closed", tier="closed_loop", control="none", genset=False)
    twin_day(sid, day, 0.6, seed=3)
    assert verify.compute_day(sid, day, with_bound=False)["native"]["fidelity"] == "confirmed"
    assert plans.edge_fidelity(sid, day) == "confirmed"
    noon = int(datetime(2026, 10, 14, 12, tzinfo=PKT).timestamp())

    def event(kind, at):
        with store.connect() as con:
            con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)", (sid, _iso(at), kind, "edge", "{}"))

    event("fallback_engaged", noon)
    assert plans.edge_fidelity(sid, day) == "not_applied"
    r = verify.compute_day(sid, day, with_bound=False)["native"]
    assert r["fidelity"] == "not_applied" and r["claimed_rs"] == 0.0
    # still in fallback the next day until the edge clears it; the day after claims again
    assert plans.edge_fidelity(sid, day + timedelta(days=1)) == "not_applied"
    event("fallback_cleared", noon + 86400 + 3600)
    assert plans.edge_fidelity(sid, day + timedelta(days=1)) == "not_applied"
    assert plans.edge_fidelity(sid, day + timedelta(days=2)) == "confirmed"
    # a plan the edge rejected that day, or applied in part
    with store.connect() as con:
        con.execute("INSERT INTO acks (id, site_id, plan_id, status, body, received_at) VALUES (?,?,?,?,?,?)",
                    ("a1", sid, "p", "partial", json.dumps({"status": "partial"}), _iso(noon + 2 * 86400)))
    assert plans.edge_fidelity(sid, day + timedelta(days=2)) == "partial"
    with store.connect() as con:
        con.execute("INSERT INTO acks (id, site_id, plan_id, status, body, received_at) VALUES (?,?,?,?,?,?)",
                    ("a2", sid, "p", "rejected", json.dumps({"status": "rejected"}), _iso(noon + 2 * 86400 + 60)))
    assert plans.edge_fidelity(sid, day + timedelta(days=2)) == "not_applied"


# ------------------------------------------------------------------------------------------------ engine-delivery-10 (batch 2)

def _day_slots(*spec, day=date(2026, 11, 6)) -> list[dict]:
    mid = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    hm = lambda t: datetime.fromtimestamp(t, PKT).strftime("%H:%M")
    out = []
    for a, b, mode in spec:
        s0, s1 = mid + int(a * 3600), mid + int(b * 3600)
        out.append({"start": hm(s0), "end": hm(s1), "start_ts": s0, "end_ts": s1, "mode": mode, "soc_floor_pct": 20.0, "soc_target_pct": 100.0,
                    "charge_end_ts": s1 if mode.startswith("charge") else None})
    return [{"date": day.isoformat(), "slots": out}]


def test_a_widened_table_starts_each_discharge_slot_15_minutes_early_and_ends_it_15_minutes_late():
    w = delivery.widen_peak_slots(_day_slots((0, 17, "hold"), (17, 21, "discharge"), (21, 24, "charge_grid")), 15, 30)
    assert [(s["start"], s["end"], s["mode"]) for s in w[0]["slots"]] == [("00:00", "16:45", "hold"), ("16:45", "21:15", "discharge"),
                                                                           ("21:15", "00:00", "charge_grid")]
    assert w[0]["slots"][2]["charge_end_ts"] == w[0]["slots"][2]["end_ts"]
    # a slot squeezed under the device's shortest slot gives its time to the discharge beside it
    w = delivery.widen_peak_slots(_day_slots((0, 16.5, "self_use"), (16.5, 17, "hold"), (17, 21, "discharge"), (21, 24, "self_use")), 15, 30)
    assert [(s["start"], s["end"]) for s in w[0]["slots"]] == [("00:00", "16:30"), ("16:30", "21:15"), ("21:15", "00:00")]
    # a table with no discharge slot is unchanged
    flat = _day_slots((0, 24, "self_use"))
    assert delivery.widen_peak_slots(flat, 15, 30) == flat


def test_the_clock_offset_ladder_warns_widens_and_demotes_until_the_clock_is_set(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    pk = world.key(["plans:read"], role="viewer")
    with world.use():
        _closed_loop(sid)
        _plan_slots(sid, [{"from_h": 0, "to_h": 17, "mode": "hold"}, {"from_h": 17, "to_h": 21, "mode": "discharge", "max_discharge_w": 5000},
                          {"from_h": 21, "to_h": 24, "mode": "self_use"}])
    beat = lambda n, off: world.anon.post(f"/api/edge/{sid}/heartbeat", headers=kh, json=_env(n, uptime_s=60, clock_offset_s=off, control_state="executing")).json()
    message = lambda: world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=pk)

    def unsynced() -> int:
        with world.use(), store.connect() as con:
            return con.execute("SELECT count(*) FROM events WHERE site_id = ? AND type = 'clock_unsynced' AND source = 'cloud'", (sid,)).fetchone()[0]

    assert beat(1, 30)["warning"] is None and unsynced() == 0
    assert beat(2, 150)["warning"].startswith("clock_offset_120s") and unsynced() == 1
    assert beat(3, -160)["warning"].startswith("clock_offset_120s") and unsynced() == 1      # raised once while it lasts
    m = message().json()
    assert m["mode_a"]["days"][0]["slots"][1]["start"] == "17:00" and m.get("clock") is None
    # over 300 s the next plan message widens the discharge slot, and the control state asks for the clock to be set
    w = beat(4, 400)["warning"]
    assert w.startswith("clock_offset_300s") and "15 minutes" in w
    m = message().json()
    hold, dis, rest = m["mode_a"]["days"][0]["slots"]
    assert (hold["end"], dis["start"], dis["end"], rest["start"]) == ("16:45", "16:45", "21:15", "21:15")
    assert m["clock"] == {"offset_s": 400.0, "widened_min": 15}
    assert "set the inverter clock" in _control(world, sid)["label"]
    assert world.c["viewer"].get(f"/api/sites/{sid}/edge").json()["clock"] == {"offset_s": 400.0, "level": "widen", "source": "edge"}
    # over 900 s control is demoted: no table is sent until the offset is under 120 s
    assert beat(5, 1000)["warning"].startswith("clock_offset_900s")
    r = message()
    assert r.status_code == 409 and r.json()["code"] == "clock_unsynced", r.text
    assert _control(world, sid)["key"] == "clock_demoted"
    assert beat(6, 200)["warning"].startswith("clock_offset_900s")
    assert message().status_code == 409
    assert beat(7, 20)["warning"] is None
    r = message()
    assert r.status_code == 200 and r.json().get("clock") is None and r.json()["mode_a"]["days"][0]["slots"][1]["start"] == "17:00"
    assert _control(world, sid)["key"] == "following"
    with world.use():
        acts = [a["action"] for a in store.list_audit()]
    assert "demoted control: the clock is off" in acts and "restored control: the clock is set" in acts


def test_the_inverters_own_clock_offset_counts_too(world):
    sid, kh = world.site, world.key(["telemetry:write"])
    m = _now() // 60 * 60 - 120
    batch = _env(1, columns=["ts", "grid_present", "inverter_clock_offset_s"], rows=[[m, 1, 30], [m + 60, 1, 1200]])
    r = world.anon.post(f"/api/edge/{sid}/telemetry", headers=kh, json=batch)
    assert r.status_code == 200 and r.json()["accepted"] == 2, r.text
    with world.use():
        assert plans.clock_info(sid) == {"offset_s": 1200.0, "level": "demoted", "source": "inverter"}
