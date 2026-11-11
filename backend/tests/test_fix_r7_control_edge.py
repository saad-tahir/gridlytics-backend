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

Batch 3, the control actions and the control state:
- hold, release, pause, resume and "mark as applied" read their bodies through strict models: the action, then the permission, then the body are
  checked, a bad field is a 422 in one sentence (never a 500, even for a viewer), nothing is written, and an applied day is a date of the plan's
  table (audit pages-B-01, security-roles-13, flow-telemetry-plan-control-20);
- every action takes effect at once: the plan is re-solved now, so a hold is in the plan and the edge's message straight away and under a pause or a
  release the plan of before is no longer active (audit flow-telemetry-plan-control-15, flow-telemetry-plan-control-21);
- an operator may resume from the hold or the release an operator may place; a pause stays an administrator's (audit flow-telemetry-plan-control-22);
- the hold, release and pause chips name their end, with its day when it is not today (audit flow-telemetry-plan-control-23).

Batch 4, the advisory tier:
- a demo site's chip says its real state: "Table not confirmed today" with nothing marked, "Edge offline" at a closed-loop site with no data (audit
  role-owner-08 and its duplicates);
- the write meter counts the Karachi day (audit pages-B-06);
- the table that reaches the inverter is issued once a day by the nightly job and re-issued only when the twin prices it Rs 500 better, the day's
  writes allow it and the last issue is an hour old (a hold or a resume at once); GET /control and the edge's message carry the issued table, the
  meter counts table writes, never pulls (audit pages-B-02, engine-delivery-09, brief-vs-build-06, flow-telemetry-plan-control-16);
- the "it took" card on today's keyed table, 30 minutes after its first discharge slot in the peak (audit pages-B-08).

Batch 5, the control page:
- a genset hold, a charging block and a forced charge are control actions: strict bodies, the permission of their kind, refused without the
  equipment, re-solved at once; a forced charge shows in the chip (audit flow-telemetry-plan-control-17, role-operator-08, tracker-and-docs-10);
- a closed-loop site whose edge never reported says so, and its read-back and write log are served (audit role-operator-11, role-analyst-12);
- the fallback test releases for 15 minutes and checks from the heartbeats that the edge handed the inverter back and took the plan again (audit
  flow-telemetry-plan-control-19).

Batch 6, edge message hardening and the signing keys:
- a new signing key is made owner-only from its first byte, in a keys folder that is the owner's alone (audit security-roles-11);
- a key is kept by its name in the keys folder, so a moved data folder signs with the same key; a missing key is an error, never silently
  replaced (the edge is refused, an alert is raised, a restored folder signs again); two first signings make one key; an administrator replaces a
  key with a reason (audit engine-delivery-13);
- the signature covers the RFC 8785 canonical JSON, which an edge in another language reproduces, with published test vectors (audit
  engine-delivery-22);
- an edge message is at most 1 MiB however it is sent, and its free-form parts are bounded (audit engine-delivery-23).

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
    # a slot the brand runs to a lower floor than the plan's (delivery.as_keyed: a global floor, here the battery's own 10 %): with the grid up the
    # replay serves the 300 kW load from the battery through the 20 % fallback
    full, p = _guard_case(_site(genset=False), load=300.0, soc0=0.5)
    table = _table({**_slot(p, 0, T, "self_use", floor=20.0), "as_keyed": {"soc_floor_pct": 10.0}})
    assert plans._guard(full, p, table, _steps(p)) == "replayed_soc_below_floor"
    # a grid cap below the load is no such table: the replay shaves the peak only down to the slot's floor (delivery-twin, engine-delivery-15; it
    # shaved to the battery's own floor before)
    assert plans._guard(full, p, _table(_slot(p, 0, T, "self_use", floor=20.0, cap_kw=200.0)), _steps(p)) is None


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
    # no edge has reported yet: nothing reaches the inverter (batch 5, audit role-operator-11; this read "following" before)
    assert _control(world, sid)["key"] == "edge_offline"
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
    # the edge's own clock is right: its heartbeat is told what the inverter's offset does
    w = world.anon.post(f"/api/edge/{sid}/heartbeat", headers=kh, json=_env(1, uptime_s=60, clock_offset_s=2)).json()["warning"]
    assert w.startswith("clock_offset_900s: the clock is 1200 s off"), w


# ================================================================================================ batch 3: control actions and the control state

def _no_ticks(monkeypatch) -> list[str]:
    """plans.tick replaced by a recorder (an action's re-solve is then counted, not run)."""
    ticks: list[str] = []

    def fake_tick(site_id, *a, **k):
        ticks.append(site_id)
        return {"status": "active", "plan_id": f"p-{len(ticks)}"}

    monkeypatch.setattr(plans, "tick", fake_tick)
    return ticks


# ------------------------------------------------------------------------------------------------ pages-B-01, security-roles-13, flow-telemetry-plan-control-20

HOURS = "The hours must be a number from 0.25 to 72"
SOC = "The state of charge to hold must be a percent from 1 to 100"


@pytest.mark.parametrize("action,role,body,said", [
    ("hold", "operator", {"hours": "four"}, HOURS),
    ("hold", "operator", {"hours": True}, HOURS),
    ("hold", "operator", {"soc_pct": "abc"}, SOC),
    ("hold", "operator", {"hours": 2, "soc_pct": 0}, SOC),                 # was silently the present state of charge
    ("hold", "operator", {"soc_pct": 140}, SOC),
    ("hold", "operator", {"hours": 500}, HOURS),                            # was clamped to 72 without a word
    ("hold", "operator", {"hours": 0.1}, HOURS),
    ("release", "operator", {"hours": [1]}, HOURS),
    ("hold", "operator", {"hours": 2, "colour": "red"}, "Unknown field 'colour'"),
    ("release", "operator", {"hours": 2, "soc_pct": 50}, "Unknown field 'soc_pct'"),
    ("hold", "operator", {"reason": "x" * 301}, "The reason must be at most 300 characters"),
    ("hold", "operator", {"reason": 7}, "The reason must be text"),
    ("pause", "admin", {"hours": {"n": 1}}, HOURS),
    ("resume", "admin", {"hours": 2}, "Unknown field 'hours'"),
])
def test_a_control_action_with_a_bad_field_is_one_sentence_and_does_nothing(world, monkeypatch, action, role, body, said):
    ticks = _no_ticks(monkeypatch)
    r = world.c[role].post(f"/api/sites/{world.site}/control/{action}", json=body)
    assert r.status_code == 422, (r.status_code, r.text)                    # a 500 "server_error" before
    assert r.json()["detail"].startswith(said) and r.json()["code"] == "invalid", r.json()
    with world.use():
        assert sites.overrides(world.site) == []
    assert ticks == []


def test_a_control_action_checks_the_action_then_the_permission_then_the_body(world, monkeypatch):
    _no_ticks(monkeypatch)
    sid, op, viewer = world.site, world.c["operator"], world.c["viewer"]
    assert op.post(f"/api/sites/{sid}/control/explode", json={"hours": "x"}).status_code == 404
    # a viewer's bad body is refused for the role, never a 500 (security-roles-13, flow-telemetry-plan-control-20)
    for action, body in (("hold", {"hours": "abc"}), ("hold", {"hours": {}}), ("release", {"hours": [1]}), ("resume", {"hours": "x"})):
        r = viewer.post(f"/api/sites/{sid}/control/{action}", json=body)
        assert r.status_code == 403, (action, r.status_code, r.text)
    assert op.post(f"/api/sites/{sid}/control/pause", json={"hours": [1]}).status_code == 403      # an operator does not pause
    r = op.post(f"/api/sites/{sid}/control/hold", json={"hours": 2, "soc_pct": 60})
    assert r.status_code == 200 and r.json()["control"]["key"] == "hold", r.text
    with world.use():
        (o,) = sites.overrides(sid)
    assert o["kind"] == "reserve" and o["value"] == 60.0 and o["reason"] == "Operator hold"
    # the old meanings stay: no state of charge holds the present level, no hours is 4 hours, a blank reason the default sentence
    r = op.post(f"/api/sites/{sid}/control/release", json={"reason": "  "})
    assert r.status_code == 200, r.text
    with world.use():
        rel = next(o for o in sites.overrides(sid) if o["kind"] == "force_native")
    assert rel["reason"] == "Released to native"
    assert 4 * 3600 - 60 <= plans._ts(rel["until_ts"]) - _now() <= 4 * 3600


def _plan_with_days(w, days: list[str]) -> str:
    t0 = _now() // 900 * 900
    with w.use(), store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, summary, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("p-r7", w.site, _iso(t0), _iso(t0), _iso(t0), _iso(t0 + 86400), "a", "active", "{}", "[]",
                                                       json.dumps({"brand": "deye", "days": [{"date": d, "slots": []} for d in days], "fallback": None})))
    return "p-r7"


def test_marking_a_plan_applied_takes_only_a_date_of_its_table(world):
    sid, op = world.site, world.c["operator"]
    today = datetime.now(PKT).date()
    days = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
    pid = _plan_with_days(world, days)
    for body, said in (({"plan_id": pid, "day": "yesterday"}, "The day is a date written YYYY-MM-DD"),          # stored as "yesterday" before
                       ({"plan_id": pid, "day": "2026-13-45"}, "The day is a date written YYYY-MM-DD"),
                       ({"plan_id": pid, "day": "2020-01-01"}, "This plan has no table for 2020-01-01"),
                       ({"plan_id": pid, "day": days[0], "by": "someone else"}, "Unknown field 'by'"),
                       ({"day": days[0]}, "The plan id is missing"),
                       ({"plan_id": pid, "day": 20261109}, "The day must be text"),
                       ({"plan_id": pid, "day": days[0], "method": "carrier_pigeon"}, "The method is one of")):
        r = op.post(f"/api/sites/{sid}/plan/apply", json=body)
        assert r.status_code == 422 and r.json()["detail"].startswith(said), (body, r.status_code, r.text)
    with world.use(), store.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM applied").fetchone()[0] == 0
    assert world.c["viewer"].post(f"/api/sites/{sid}/plan/apply", json={"plan_id": pid, "day": "junk"}).status_code == 403
    r = op.post(f"/api/sites/{sid}/plan/apply", json={"plan_id": pid, "day": days[1], "method": "vendor_app"})
    assert r.status_code == 200 and r.json()["day_local"] == days[1], r.text
    r = op.post(f"/api/sites/{sid}/plan/apply", json={"plan_id": pid})                              # the day defaults to Karachi's today
    assert r.status_code == 200 and r.json()["day_local"] == days[0], r.text
    # a photograph over the limit is refused, not cut short (which spoilt the image)
    big = "data:image/png;base64," + "A" * 7_000_000
    r = op.post(f"/api/sites/{sid}/plan/apply", json={"plan_id": pid, "day": days[0], "photo": big})
    assert r.status_code == 422, r.status_code
    with world.use():
        assert len(plans.applied_rows(sid)) == 2


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-15, flow-telemetry-plan-control-21

def _charged(sid: str, soc: float = 0.9) -> None:
    """The site's battery at `soc` (it has no readings, so the solve starts from the carried state): a hold at 80 % is then reachable without a charge
    at any hour. From 50 %, a hold at 80 % in the evening peak can be kept only by shedding load with the grid up, which the wrong-plan guard refuses
    (the re-solve is held and the plan of before stays in force), so a test of the hold's own effect read the time of day (review)."""
    sites.put_state(sid, soc=soc)


def test_a_hold_re_solves_at_once_so_the_plan_and_the_edge_take_it(world):
    sid, op = world.site, world.c["operator"]
    with world.use():
        _closed_loop(sid)
        _charged(sid)
        before = plans.tick(sid)
    assert before["status"] == "active"
    r = op.post(f"/api/sites/{sid}/control/hold", json={"hours": 2, "soc_pct": 80})
    assert r.status_code == 200, r.text
    resolve = r.json()["resolve"]                                          # no re-solve before: the hold waited for the next quarter
    assert resolve["status"] == "active" and resolve["plan_id"] != before["plan_id"], resolve
    with world.use():
        (o,) = sites.overrides(sid)
        plan = plans.current(sid)
        message = plans.signed_message(sid, record=False)
    assert plan["id"] == resolve["plan_id"] == message["plan_id"]
    until = plans._ts(o["until_ts"])
    held = [s for s in plan["steps"] if s["t"] < until]
    assert held and all(s["reserve_pct"] >= 80 for s in held), [(s["local"], s["reserve_pct"]) for s in held]
    assert any(s["reserve_pct"] < 80 for s in plan["steps"] if s["t"] >= until)


def test_a_hold_whose_re_solve_the_guard_holds_says_why_and_the_plan_of_before_stays(world, monkeypatch):
    """A hold the site cannot keep but by a plan the wrong-plan guard refuses (say, shedding load with the grid up to charge to it in the peak): the
    hold is placed, the re-solve is answered held with the guard's reason (it read None), and the plan of before stays in force (review)."""
    sid, op = world.site, world.c["operator"]
    with world.use():
        _closed_loop(sid)
        before = plans.tick(sid)
    assert before["status"] == "active"
    monkeypatch.setattr(plans, "_guard", lambda *a, **k: "sheds_load_with_grid_up")
    r = op.post(f"/api/sites/{sid}/control/hold", json={"hours": 2, "soc_pct": 80})
    assert r.status_code == 200, r.text
    resolve = r.json()["resolve"]
    assert resolve["status"] == "held" and resolve["reason"] == "sheds_load_with_grid_up" and resolve["plan_id"] != before["plan_id"], resolve
    assert r.json()["control"]["key"] == "hold"                                  # the hold stands
    with world.use():
        assert plans.current(sid)["id"] == before["plan_id"]                      # the plan of before is in force


def test_a_pause_or_release_re_solves_at_once_so_the_versions_list_holds_the_plan(world):
    sid, admin, op = world.site, world.c["admin"], world.c["operator"]
    for client, action, reason in ((admin, "pause", "control_paused"), (op, "release", "released_to_native")):
        with world.use():
            first = plans.tick(sid)
        assert first["status"] == "active"
        r = client.post(f"/api/sites/{sid}/control/{action}", json={"hours": 1})
        assert r.status_code == 200 and r.json()["resolve"]["status"] == "held", r.text
        assert r.json()["resolve"]["reason"] == reason, r.json()["resolve"]          # why it is held is said (review: it read None)
        rows = world.c["viewer"].get(f"/api/sites/{sid}/plans").json()
        # the plan of before is no longer badged active under "No plan in force": it is superseded, and the newest is held for the pause
        assert [x["status"] for x in rows[:2]] == ["held", "superseded"] and rows[0]["reason"] == reason, rows[:2]
        assert not any(x["status"] == "active" for x in rows)
        held = world.c["viewer"].get(f"/api/sites/{sid}/plan").json()["held"]
        assert held["reason"] == reason and held["plan_id"] == rows[0]["id"], held
        assert admin.post(f"/api/sites/{sid}/control/resume", json={}).json()["resolve"]["status"] == "active"


def test_every_action_re_solves_and_a_failed_re_solve_leaves_the_action_standing(world, monkeypatch):
    sid, admin = world.site, world.c["admin"]
    ticks = _no_ticks(monkeypatch)
    for n, action in enumerate(("hold", "release", "pause", "resume"), start=1):
        r = admin.post(f"/api/sites/{sid}/control/{action}", json={} if action == "resume" else {"hours": 2})
        assert r.status_code == 200 and r.json()["resolve"] == {"status": "active", "plan_id": f"p-{n}", "reason": None}, (action, r.text)
    assert ticks == [sid] * 4
    assert "resolve" not in admin.post(f"/api/sites/{sid}/control/resume", json={}).json() and len(ticks) == 4   # nothing to resume

    def broken(site_id, *a, **k):
        raise RuntimeError("solver crashed")

    monkeypatch.setattr(plans, "tick", broken)
    r = admin.post(f"/api/sites/{sid}/control/hold", json={"hours": 2, "soc_pct": 70})
    assert r.status_code == 200 and r.json()["control"]["key"] == "hold", r.text
    assert r.json()["resolve"]["status"] == "failed" and r.json()["resolve"]["reason"].startswith("The re-solve after the hold failed"), r.json()
    with world.use():
        assert [o["kind"] for o in sites.overrides(sid)] == ["reserve"]


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-22

def test_an_operator_may_resume_from_a_release_or_a_hold_but_not_from_a_pause(world, monkeypatch):
    sid, admin, op, viewer = world.site, world.c["admin"], world.c["operator"], world.c["viewer"]
    ticks = _no_ticks(monkeypatch)
    assert op.post(f"/api/sites/{sid}/control/release", json={"hours": 2}).status_code == 200
    assert op.post(f"/api/sites/{sid}/control/hold", json={"hours": 2}).status_code == 200
    assert viewer.post(f"/api/sites/{sid}/control/resume", json={}).status_code == 403
    r = op.post(f"/api/sites/{sid}/control/resume", json={})
    assert r.status_code == 200 and r.json()["control"]["key"] not in ("native", "hold") and "resolve" in r.json(), r.text
    with world.use():
        assert sites.overrides(sid) == []
    assert len(ticks) == 3
    # a pause is an administrator's to end (plan Part 10.5, resume_control), with a release beside it or not
    assert admin.post(f"/api/sites/{sid}/control/pause", json={"hours": 2}).status_code == 200
    assert op.post(f"/api/sites/{sid}/control/release", json={"hours": 2}).status_code == 200
    r = op.post(f"/api/sites/{sid}/control/resume", json={})
    assert r.status_code == 403 and r.json()["detail"] == "Resuming control after a pause is for an administrator", r.text
    with world.use():
        assert sorted(o["kind"] for o in sites.overrides(sid)) == ["force_native", "pause_control"]
    assert admin.post(f"/api/sites/{sid}/control/resume", json={}).status_code == 200


# ------------------------------------------------------------------------------------------------ flow-telemetry-plan-control-23

def test_an_end_is_said_with_its_day_when_it_is_not_today():
    from gridlytics import routes_sites
    now = int(datetime(2026, 11, 9, 10, 0, tzinfo=PKT).timestamp())
    assert routes_sites.until_words("2026-11-09T14:07:00+00:00", now) == "19:07"
    assert routes_sites.until_words("2026-11-10T14:07:00+00:00", now) == "Tue 10 Nov, 19:07"
    assert routes_sites.until_words("2026-11-09T19:30:00+00:00", now) == "Tue 10 Nov, 00:30"       # past midnight in Karachi, the 9th in UTC
    assert routes_sites.until_words("2026-11-16T09:00:00Z", now) == "Mon 16 Nov, 14:00"
    assert routes_sites.until_words("2027-01-01T02:00:00+00:00", now) == "Fri 1 Jan, 07:00"


def test_the_hold_release_and_pause_chips_name_their_end_with_its_day(world, monkeypatch):
    from gridlytics import routes_sites
    _no_ticks(monkeypatch)
    sid, admin = world.site, world.c["admin"]
    now = datetime.now(PKT)

    def said(iso: str) -> str:
        end = datetime.fromisoformat(iso).astimezone(PKT)
        return end.strftime("%H:%M") if end.date() == now.date() else f"{end:%a} {end.day} {end:%b}, {end:%H:%M}"

    c = admin.post(f"/api/sites/{sid}/control/hold", json={"hours": 30}).json()["control"]
    assert c["label"] == f"Hold until {said(c['until'])}" and "," in c["label"], c          # "Hold until 19:07" for tomorrow before
    c = admin.post(f"/api/sites/{sid}/control/release", json={"hours": 50}).json()["control"]
    assert c["key"] == "native" and c["label"] == f"Released to native until {said(c['until'])}", c
    c = admin.post(f"/api/sites/{sid}/control/pause", json={"hours": 0.5}).json()["control"]
    assert c["key"] == "paused" and c["label"] == f"Control paused until {said(c['until'])}", c
    with world.use():
        assert routes_sites.control_state(sites.get(sid), None, _now())["label"] == c["label"]


# ------------------------------------------------------------------------------------------------ batch 4: role-owner-08 (and its duplicates)

def _tier(sid: str, tier: str) -> None:
    sites.set_tier_capable(sid, tier)
    sites.set_tier_allowed(sid, tier, "t", "test")


def _day_table(day: date, discharge_from: int = 18) -> list[dict]:
    """A day's table: a hold, a discharge from `discharge_from` to 22:00, a hold to midnight."""
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    return [{"start": f"{a:02d}:00", "end": f"{b % 24:02d}:00", "start_ts": t0 + a * 3600, "end_ts": t0 + b * 3600, "mode": m, "soc_floor_pct": 20.0,
             "soc_target_pct": 20.0, "max_discharge_w": 50_000 if m == "discharge" else None, "max_charge_w": None, "grid_charge": False,
             "gen_charge": False, "grid_cap_w": None, "export_allowed": False, "genset": None, "intent": m}
            for a, b, m in ((0, discharge_from, "hold"), (discharge_from, 22, "discharge"), (22, 24, "hold"))]


def _today_plan(sid: str, day: date, status: str = "active", discharge_from: int = 18) -> str:
    """A plan row with a table for `day` (_day_table), valid for a day from now; an active one supersedes the active plan of before."""
    pid = store.new_id()
    slots = _day_table(day, discharge_from)
    with store.connect() as con:
        if status == "active":
            con.execute("UPDATE plans SET status = 'superseded' WHERE site_id = ? AND status = 'active'", (sid,))
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, summary, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, sid, store._now(), _iso(_now()), _iso(_now()), _iso(_now() + 86400), "a", status, "{}", "[]",
                                                       json.dumps({"brand": "deye", "days": [{"date": day.isoformat(), "slots": slots}], "fallback": None})))
    return pid


def test_a_demo_site_says_its_control_state_as_a_customer_site_does(company):
    """A demo site's chip is no longer forced to "Following plan (table applied)": with no table marked for today it reads "Table not confirmed
    today", as its alert and applied log say, and a closed-loop demo site with no data for hours reads "Edge offline" (audit role-owner-08,
    role-viewer-01, role-operator-03, role-analyst-04, flow-telemetry-plan-control-27, flow-savings-pilot-bill-28)."""
    from gridlytics import demo, routes_sites
    now = _now()
    adv = sites.create({"name": "Faisalabad textile mill (demo)", "disco": "FESCO"}, "t", demo_link="demo")["id"]
    loop = sites.create({"name": "Karachi feed mill (demo)", "disco": "FESCO"}, "t", demo_link="demo")["id"]
    _tier(adv, "advisory_writes")
    _tier(loop, "closed_loop")
    sites.put_state(loop, edge_last_seen=now - 4 * 3600)       # its edge has reported (one that never did reads "No edge device yet", batch 5)
    state = lambda sid, last: routes_sites.control_state(sites.get(sid), last, now)  # noqa: E731
    # before: "Following plan (table applied)" with nothing marked, and "Following plan" beside "No live data: the site is offline"
    assert state(adv, {"ts": now}) == {"key": "unconfirmed", "label": "Table not confirmed today"}
    assert state(loop, {"ts": now - 4 * 3600}) == {"key": "edge_offline", "label": "Edge offline"}
    assert state(loop, {"ts": now - 60})["key"] == "following"
    # a demo site's edge is the twin, which the 15-minute schedule runs: its readings are fresh for demo.EDGE_FRESH_S, as its edge card says
    # (demo-seed)
    assert state(loop, {"ts": now - 12 * 60})["key"] == "following"
    assert state(loop, {"ts": now - demo.EDGE_FRESH_S - 60})["key"] == "edge_offline"
    real = sites.create({"name": "Karachi feed mill", "disco": "FESCO"}, "t")["id"]
    _tier(real, "closed_loop")
    sites.put_state(real, edge_last_seen=now - 12 * 60)
    assert state(real, {"ts": now - 12 * 60}) == {"key": "edge_offline", "label": "Edge offline"}   # a real edge reports every minute
    today = datetime.now(PKT).date()
    plans.mark_applied(adv, _today_plan(adv, today), today.isoformat(), "Installer", "installer_keyed")
    assert state(adv, {"ts": now}) == {"key": "following", "label": "Following plan (table applied)"}


# ------------------------------------------------------------------------------------------------ batch 4: pages-B-06

def _sent(sid: str, at: datetime, day_modes: dict[str, str]) -> None:
    """A plan_messages row sent at `at` whose table holds one slot per day, of the mode given for that day."""
    days = [{"date": d, "slots": [{"start": "00:00", "end": "00:00", "mode": m}]} for d, m in day_modes.items()]
    body = {"type": "plan", "plan_id": "p", "mode_a": {"days": days}, "signature": {"key_id": "k"}}
    with store.connect() as con:
        con.execute("INSERT INTO plan_messages (id, site_id, plan_id, key_id, body, sent_at) VALUES (?,?,?,?,?,?)",
                    (store.new_id(), sid, "p", "k", json.dumps(body), at.astimezone(timezone.utc).isoformat(timespec="seconds")))


def test_the_write_meter_counts_a_karachi_day(world):
    """The meter's day is Karachi's: a send at 00:30 Pakistan time (19:30 UTC the day before) is today's, one at 23:30 last night is not (audit
    pages-B-06; before, the UTC stamp's date was compared with the Karachi date)."""
    sid = world.site
    today = datetime.now(PKT).date()
    yday = today - timedelta(days=1)
    at = lambda d, h, m: datetime(d.year, d.month, d.day, h, m, tzinfo=PKT)  # noqa: E731
    with world.use():
        _tier(sid, "closed_loop")
        _sent(sid, at(yday, 23, 30), {yday.isoformat(): "hold", today.isoformat(): "charge_grid"})
        _sent(sid, at(today, 0, 30), {today.isoformat(): "discharge"})
        _sent(sid, at(today, 9, 0), {today.isoformat(): "hold"})
    budget = world.c["admin"].get(f"/api/sites/{sid}/control").json()["budget"]
    assert budget["used_today"] == 2, budget                               # 1 before: the 00:30 send was counted on yesterday's date


# ------------------------------------------------------------------------------------------------ batch 4: pages-B-02 (engine-delivery-09, brief-vs-build-06, flow-telemetry-plan-control-16)

def _pkt(day: date, h: int = 0, m: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=PKT).timestamp())


def test_the_table_to_key_is_issued_once_a_day_and_re_issued_only_when_worth_it(world, monkeypatch):
    """Advisory tier (plan Part 7.1 step 7): the nightly job issues the day's table; a later plan re-issues it only when it is worth Rs 500 more, the
    day's table writes are not used up and the last issue is an hour old. GET /control serves the issued table (not each quarter's draft), counts
    the day's tables against the brand's writes, keeps the re-issue held back, hides its rupees from a viewer, says which table each mark keyed,
    and asks for a new key after a re-issue."""
    sid, day = world.site, datetime.now(PKT).date()
    at = lambda h, m=0: _pkt(day, h, m)  # noqa: E731
    gains = iter([200.0, 900.0, 900.0, 900.0, 900.0, 900.0])
    monkeypatch.setattr(plans, "reissue_gain", lambda *a: next(gains))
    with world.use():
        _tier(sid, "advisory_writes")
        nightly = _today_plan(sid, day, discharge_from=18)
        issue = plans.issue_nightly(sid, at(0) - 3600 + 300)                     # the 23:05 job the night before: the next day's table
        assert issue["reason"] == "nightly" and issue["plan_id"] == nightly and issue["day_local"] == day.isoformat()
        assert plans.issue_nightly(sid, at(0) - 3600 + 600) is None             # once a night
        later, decide = [], []
        for start, t in zip((19, 17, 16, 15, 14), (at(10), at(10, 15), at(10, 30), at(11, 30), at(12, 45))):
            later.append(_today_plan(sid, day, discharge_from=start))
            decide.append(plans.issue_tables(sid, t))
        # before: every quarter's plan replaced the table to key in, and nothing was counted against the writes
        assert [d["blocked"] for d in decide] == ["below_threshold", None, "too_soon", None, "budget_used"], decide
        assert [i["reason"] for i in plans.issued_tables(sid, day.isoformat())] == ["nightly", "event", "event"]
        current = _today_plan(sid, day, discharge_from=13)                          # this quarter's draft differs again
        assert plans.issue_tables(sid, at(13))["blocked"] == "budget_used"
        with store.connect() as con:                                             # the table issued at 10:15 was keyed at 10:20
            con.execute("INSERT INTO applied (id, site_id, plan_id, day_local, by, method, created) VALUES (?,?,?,?,?,?,?)",
                        ("m1", sid, later[1], day.isoformat(), "Installer", "installer_keyed", _iso(at(10, 20))))
    c = world.c["admin"].get(f"/api/sites/{sid}/control").json()
    assert c["plan_id"] == current
    assert c["issued"] == [{"day": day.isoformat(), "plan_id": later[3], "issued_at": _iso(at(11, 30)), "reason": "event", "gain_rs": 900.0}]
    assert c["compiled"]["days"][0]["slots"][1]["start"] == "15:00"            # the issued table, not the draft's 13:00
    assert c["vendor_view"] and c["budget"] == {"table_writes_per_day": 3, "used_today": 3}
    assert {k: c["reissue"][k] for k in ("plan_id", "day", "gain_rs", "issued", "blocked")} == \
        {"plan_id": current, "day": day.isoformat(), "gain_rs": 900.0, "issued": False, "blocked": "budget_used"}
    # the table was re-issued after the 10:20 mark: what the inverter runs is the earlier one
    assert c["control"] == {"key": "unconfirmed", "label": "New table issued: key it in and mark it"}
    (m1,) = c["applied"]
    assert m1["table"] == {"issued_at": _iso(at(10, 15)), "reason": "event", "superseded": True}
    v = world.c["viewer"].get(f"/api/sites/{sid}/control").json()
    assert v["issued"][0]["plan_id"] == later[3] and "gain_rs" not in json.dumps(v)
    r = world.c["operator"].post(f"/api/sites/{sid}/plan/apply", json={"plan_id": later[3], "day": day.isoformat()})
    assert r.status_code == 200, r.text
    c = world.c["admin"].get(f"/api/sites/{sid}/control").json()
    keyed = next(a for a in c["applied"] if a["plan_id"] == later[3])
    assert c["control"]["key"] == "following" and keyed["table"] == {"issued_at": _iso(at(11, 30)), "reason": "event", "superseded": False}
    assert c["budget"]["used_today"] == 3                                      # the issued table keyed: no further write


def test_a_re_issue_is_priced_by_replaying_both_tables_through_the_twin(company):
    """The gain of a re-issue: both tables replayed on the plan's forecast, the energy bill and the critical load left unserved priced; a table
    the same as the issued one is never re-issued."""
    sid = _site(genset=False)
    site, day = sites.full(sid), datetime.now(PKT).date()
    t0 = _pkt(day)
    steps = [{"t": t0 + 900 * i, "pv_forecast_kw": 0.0, "load_kw": 100.0, "critical_kw": 20.0, "grid_present": 1.0,
              "c_imp": 60.0 if 72 <= i < 88 else 30.0, "c_exp": 0.0, "soc_start_pct": 90.0} for i in range(96)]
    hold = [{**_day_table(day)[0], "end": "00:00", "end_ts": t0 + 86400}]
    plan = {"id": "p", "steps": steps, "slots": {"days": [{"date": day.isoformat(), "slots": _day_table(day)}]}}
    gain = plans.reissue_gain(site, plan, hold, day.isoformat())
    assert 5000 < gain < 10000, gain                                           # about 140 kWh discharged in the peak at Rs 60
    assert abs(plans.reissue_gain(site, plan, _day_table(day), day.isoformat())) < 1
    # an outage at 20:00: a table that drains the battery by 18:00 leaves the critical load unserved, priced at the value of lost load
    out = [{**s, "grid_present": 0.0 if 80 <= i < 88 else 1.0, "c_imp": 30.0} for i, s in enumerate(steps)]
    drained = _day_table(day, discharge_from=8)
    plan = {"id": "p", "steps": out, "slots": {"days": [{"date": day.isoformat(), "slots": hold}]}}
    assert plans.reissue_gain(site, plan, drained, day.isoformat()) > 500


def test_a_hold_issues_the_table_at_once_and_nothing_is_issued_under_a_release_or_at_monitor(world):
    sid, op = world.site, world.c["operator"]
    day = datetime.now(PKT).date().isoformat()
    with world.use():
        _tier(sid, "advisory_writes")
        _charged(sid)                                                              # a hold at 80 % the site keeps at any hour
        first = plans.tick(sid)
        assert plans.issue_tables(sid)["reason"] == "first"
    r = op.post(f"/api/sites/{sid}/control/hold", json={"hours": 2, "soc_pct": 80})
    assert r.status_code == 200, r.text
    with world.use():
        rows = plans.issued_tables(sid, day)
        # a hold needs the table now, whatever the gain, the budget or the spacing say
        assert [(i["reason"], i["plan_id"]) for i in rows] == [("first", first["plan_id"]), ("hold", r.json()["resolve"]["plan_id"])], rows
    assert r.json()["issued"][0]["plan_id"] == r.json()["resolve"]["plan_id"]
    assert op.post(f"/api/sites/{sid}/control/release", json={"hours": 1}).status_code == 200
    with world.use():
        assert plans.issue_tables(sid) is None and len(plans.issued_tables(sid, day)) == 2
        _tier(sid, "monitor")
    assert world.c["admin"].post(f"/api/sites/{sid}/control/resume", json={}).status_code == 200
    with world.use():
        assert plans.issue_tables(sid) is None and len(plans.issued_tables(sid, day)) == 2     # the monitor tier writes nothing


def test_the_edge_is_sent_the_issued_table_and_the_meter_counts_table_changes_not_pulls(world, monkeypatch):
    """Closed loop: the edge gets the table issued for the day (the plan in force's id, the other fields its own) with U3's apply_policy and R6's
    write budget, and the meter counts the sends that changed the table, not the pulls."""
    sid, day = world.site, datetime.now(PKT).date()
    with world.use():
        _tier(sid, "closed_loop")
        _today_plan(sid, day, discharge_from=18)
        assert plans.issue_tables(sid)["reason"] == "first"
        for _ in range(3):
            m = plans.signed_message(sid)
        assert m["mode_a"]["apply_policy"] == {"write_if_changed_only": True, "max_table_writes_per_day": 4, "apply_window_local": "00:05-23:50"}
        assert m["write_budget"] == {"table_writes_per_day": 3, "event_writes_per_day": 2}
        assert plans.writes_used(sid, day) == 1                                 # three pulls of one table (3 before)
        p2 = _today_plan(sid, day, discharge_from=16)
        monkeypatch.setattr(plans, "reissue_gain", lambda *a: 200.0)
        assert plans.issue_tables(sid)["blocked"] == "below_threshold"
        m = plans.signed_message(sid)
        assert m["plan_id"] == p2 and m["mode_a"]["days"][0]["slots"][1]["start"] == "18:00"      # the issued table, not p2's 16:00
        assert plans.writes_used(sid, day) == 1
        monkeypatch.setattr(plans, "reissue_gain", lambda *a: 900.0)
        monkeypatch.setattr(plans, "REISSUE_SPACING_S", 0)
        assert plans.issue_tables(sid)["issued"] is True
        for _ in range(2):
            m = plans.signed_message(sid)
        assert m["mode_a"]["days"][0]["slots"][1]["start"] == "16:00"
        assert plans.writes_used(sid, day) == 2
    assert world.c["admin"].get(f"/api/sites/{sid}/control").json()["budget"]["used_today"] == 2


def test_the_scheduler_issues_the_days_table_after_its_tick_and_tomorrows_at_23_05(company, monkeypatch):
    from gridlytics import alerts, scheduler
    sid = _site(genset=False)
    _tier(sid, "advisory_writes")
    sites.set_status(sid, "active")
    monkeypatch.setattr(alerts, "evaluate", lambda *a, **k: None)
    assert scheduler.tick_company(company) == {"sites": 1, "failed": 0}
    today = datetime.now(PKT).date()
    (first,) = plans.issued_tables(sid, today.isoformat())
    assert first["reason"] == "first" and first["plan_id"] == plans.current(sid)["id"]
    scheduler.nightly_tables_company(company)
    (nightly,) = plans.issued_tables(sid, (today + timedelta(days=1)).isoformat())
    assert nightly["reason"] == "nightly" and scheduler.NIGHTLY_TABLES_AT == (23, 5)


# ------------------------------------------------------------------------------------------------ batch 4: pages-B-08

D8 = date(2026, 10, 14)
T8 = int(datetime(D8.year, D8.month, D8.day, tzinfo=PKT).timestamp())
PEAK8 = (T8 + 18 * 3600, T8 + 22 * 3600)
DUE8 = PEAK8[0] + 1800


def _keyed(name: str) -> str:
    """An advisory site whose inverter, left to itself, holds the battery for outages (native backup at 90 %), a plan with D8's table (a hold, a
    discharge from 18:00 to 22:00, a hold) and a mark of it made at 10:00 that morning."""
    from test_fix_savings import make_site
    sid = make_site(name=name, tier="advisory_writes", genset=False, native={"source": "entered", "work_mode": "backup", "backup_soc_pct": 90})
    pid = store.new_id()
    table = [{**s, "soc_floor_pct": 90.0 if s["mode"] == "hold" and s["start"] == "00:00" else s["soc_floor_pct"]} for s in _day_table(D8)]
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (pid, sid, _iso(T8 - 3600), _iso(T8 - 3600), _iso(T8 - 3600), _iso(T8 + 86400), "a", "active", "[]",
                                                     json.dumps({"days": [{"date": D8.isoformat(), "slots": table}]})))
        con.execute("INSERT INTO applied (id, site_id, plan_id, day_local, by, method, created) VALUES (?,?,?,?,?,?,?)",
                    (store.new_id(), sid, pid, D8.isoformat(), "Installer", "installer_keyed", _iso(T8 + 10 * 3600)))
    return sid


def _readings(sid: str, bat_kw, battery: bool = True) -> None:
    """Grid-up minutes from 17:00 to 19:30: 150 kW of load, no PV, the battery's power by minute (kW, discharge positive) and its state of charge."""
    rows, soc = [], 90.0
    for t in range(T8 + 17 * 3600, T8 + 19 * 3600 + 1800, 60):
        b = bat_kw(t)
        soc -= b / 60 / 215 * 100
        rows.append((sid, t, "edge", 1.0, 0.0, 150_000.0, b * 1000 if battery else None, soc))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "pv_power_w", "load_power_w", "battery_power_w", "battery_soc_bms_pct"))


@pytest.fixture()
def peak8(monkeypatch):
    monkeypatch.setattr(plans, "peak_mask", lambda conn, ts: (np.asarray(ts) >= PEAK8[0]) & (np.asarray(ts) < PEAK8[1]))


def test_a_keyed_table_gets_its_took_card_30_minutes_after_its_first_peak_slot(company, peak8):
    """Plan 9.5 and 7.6: 30 minutes after the keyed table's first discharge slot in the peak, the readings say whether it took; the day's class and
    its claim stay the nightly check's."""
    followed = _keyed("Took")
    _readings(followed, lambda t: 100.0 if t >= PEAK8[0] else 0.0)
    early = plans.early_check(followed, PEAK8[0] + 20 * 60)
    assert early["verdict"] == "waiting" and early["due_ts"] == DUE8 and early["slot_start"] == "18:00", early
    # before: nothing until the nightly job at 00:40 (verify.compute_day was the only caller of confirm_applied)
    early = plans.early_check(followed, DUE8 + 60)
    assert early["verdict"] == "took" and early["max_discharge_kw"] == 100.0, early
    row = plans.applied_rows(followed)[0]
    assert row["early_check"]["verdict"] == "took" and row["confirmation"] == "pending"
    assert plans.early_check(followed, DUE8 + 3600) == early                    # a verdict is kept, not computed again
    # the inverter kept its native backup setting: the battery never discharged at 18:00
    ignored = _keyed("Did not take")
    _readings(ignored, lambda t: 0.0)
    assert plans.early_check(ignored, DUE8 + 60)["verdict"] == "did_not_take"


def test_no_battery_reading_waits_and_a_day_with_no_peak_discharge_says_so(company, peak8):
    blind = _keyed("Blind")
    _readings(blind, lambda t: 100.0, battery=False)
    assert {k: plans.early_check(blind, DUE8 + 60)[k] for k in ("verdict", "reason")} == {"verdict": "waiting", "reason": "no_battery_power"}
    late = _keyed("Marked late")
    with store.connect() as con:                                             # keyed at 20:00, after the discharge slot started
        con.execute("UPDATE applied SET created = ? WHERE site_id = ?", (_iso(T8 + 20 * 3600), late))
    assert plans.early_check(late, DUE8 + 3 * 3600)["verdict"] == "no_peak_slot"


def test_marking_todays_table_says_when_the_card_comes_and_the_tick_runs_the_check(world, monkeypatch):
    from gridlytics import scheduler
    sid, day = world.site, datetime.now(PKT).date()
    with world.use():
        _tier(sid, "advisory_writes")
        pid = _today_plan(sid, day)
    monkeypatch.setattr(plans, "peak_mask", lambda conn, ts: (np.asarray(ts) >= _pkt(day, 18)) & (np.asarray(ts) < _pkt(day, 22)))
    r = world.c["operator"].post(f"/api/sites/{sid}/plan/apply", json={"plan_id": pid, "day": day.isoformat()})
    assert r.status_code == 200, r.text
    early = r.json()["early_check"]
    if _now() < _pkt(day, 18) + 900:                                             # marked before the 18:00 slot: the card comes at 18:30
        assert early["verdict"] == "waiting" and early["due_ts"] == _pkt(day, 18) + 1800, early
    else:
        assert early["verdict"] in ("no_peak_slot", "waiting"), early
    assert world.c["viewer"].get(f"/api/sites/{sid}/control").json()["applied"][0]["early_check"]["verdict"] == early["verdict"]
    calls = []
    monkeypatch.setattr(plans, "issue_tables", lambda site_id: calls.append("issue") or 1 / 0)
    monkeypatch.setattr(plans, "early_check", lambda site_id: calls.append("early"))
    scheduler._after_tick(sid)
    assert calls == ["issue", "early"]                                         # a failed issue does not stop the check


# ------------------------------------------------------------------------------------------------ batch 5: flow-telemetry-plan-control-17 (role-operator-08, tracker-and-docs-10)

def test_a_genset_hold_a_charging_block_and_a_forced_charge_are_placed_as_control_actions(world, monkeypatch):
    """The genset hold, the charging block and the forced charge had solver bounds but no page placed them: they are control actions now, each behind
    the permission its override kind needs (a forced charge an administrator's), read through a strict body, refused where the site has not the
    equipment, and solved in at once as a hold is; a forced charge says so in the control chip (it read "Recommendation only")."""
    ticks = _no_ticks(monkeypatch)
    sid, admin, op, viewer = world.site, world.c["admin"], world.c["operator"], world.c["viewer"]
    r = op.post(f"/api/sites/{sid}/control/genset_hold", json={"hours": 3, "reason": "Fuel delivery late"})
    assert r.status_code == 200 and r.json()["resolve"]["status"] == "active", r.text
    r = op.post(f"/api/sites/{sid}/control/charge_block", json={"hours": 2})
    assert r.status_code == 200, r.text
    r = op.post(f"/api/sites/{sid}/control/force_charge", json={"hours": 2, "soc_pct": 90})
    assert r.status_code == 403 and r.json()["detail"] == "Forcing a charge is for an administrator", r.text
    assert viewer.post(f"/api/sites/{sid}/control/genset_hold", json={"hours": "x"}).status_code == 403
    r = admin.post(f"/api/sites/{sid}/control/force_charge", json={"hours": 2, "soc_pct": 90})
    assert r.status_code == 200, r.text
    c = r.json()["control"]
    assert c["key"] == "force_charge" and c["label"].startswith("Forced charge to 90 % until "), c
    assert ticks == [sid] * 3
    with world.use():
        ovs = {o["kind"]: o for o in sites.overrides(sid)}
    assert set(ovs) == {"genset_hold", "charge_block", "force_charge"}
    assert ovs["genset_hold"]["reason"] == "Fuel delivery late" and ovs["charge_block"]["reason"] == "Charging blocked"
    assert ovs["force_charge"]["value"] == 90.0 and ovs["genset_hold"]["value"] is None
    assert 3 * 3600 - 60 <= plans._ts(ovs["genset_hold"]["until_ts"]) - _now() <= 3 * 3600
    # left out, a forced charge charges to full; a hold placed after it does not hide it from the chip
    admin.delete(f"/api/sites/{sid}/overrides/{ovs['force_charge']['id']}")
    r = admin.post(f"/api/sites/{sid}/control/force_charge", json={})
    assert r.status_code == 200 and r.json()["control"]["label"].startswith("Forced charge to 100 % until "), r.text
    assert admin.post(f"/api/sites/{sid}/control/hold", json={"hours": 1}).json()["control"]["key"] == "force_charge"


@pytest.mark.parametrize("action,body,said", [
    ("force_charge", {"soc_pct": 0}, "The state of charge to charge to must be a percent from 1 to 100"),
    ("force_charge", {"soc_pct": "full"}, "The state of charge to charge to must be a percent from 1 to 100"),
    ("genset_hold", {"hours": 100}, HOURS),
    ("genset_hold", {"soc_pct": 50}, "Unknown field 'soc_pct'"),
    ("charge_block", {"hours": "two"}, HOURS),
])
def test_an_override_action_with_a_bad_field_is_one_sentence_and_does_nothing(world, monkeypatch, action, body, said):
    ticks = _no_ticks(monkeypatch)
    r = world.c["admin"].post(f"/api/sites/{world.site}/control/{action}", json=body)
    assert r.status_code == 422 and r.json()["detail"].startswith(said), r.text
    with world.use():
        assert sites.overrides(world.site) == []
    assert ticks == []


def test_an_override_the_site_has_no_equipment_for_is_refused(world, monkeypatch):
    ticks = _no_ticks(monkeypatch)
    other, admin = world.other, world.c["admin"]                     # no battery and no genset
    for action, said in (("genset_hold", "The site has no genset to hold off"), ("charge_block", "The site has no battery to block charging of"),
                         ("force_charge", "The site has no battery to charge")):
        r = admin.post(f"/api/sites/{other}/control/{action}", json={"hours": 1})
        assert r.status_code == 409 and r.json()["detail"] == said and r.json()["code"] == "no_equipment", r.text
    with world.use():
        assert sites.overrides(other) == []
    assert ticks == []
    # the control answer names the overrides the site has the equipment for, so the page offers only those
    assert world.c["operator"].get(f"/api/sites/{world.site}/control").json()["placeable"] == ["genset_hold", "charge_block", "force_charge"]
    assert world.c["operator"].get(f"/api/sites/{other}/control").json()["placeable"] == []


def test_a_forced_charge_and_a_genset_hold_reach_the_plan_at_once(world):
    """The re-solve at once takes the new override: the forced charge's floor is in the re-solved plan, as is the genset hold. (Whether the wrong-plan
    guard lets that plan into force is the guard's own matter: at some hours the solve sheds load with the grid up to keep its MDI cap while it
    charges, and the guard holds such a plan, so the plan is read by its id, whatever its status.)"""
    sid, admin = world.site, world.c["admin"]
    with world.use():
        plans.tick(sid)
    r = admin.post(f"/api/sites/{sid}/control/force_charge", json={"hours": 3, "soc_pct": 80})
    assert r.status_code == 200 and r.json()["resolve"]["status"] in ("active", "held"), r.text
    with world.use():
        (o,) = sites.overrides(sid)
        plan = plans.get(sid, r.json()["resolve"]["plan_id"])
    assert o["kind"] == "force_charge"
    assert [(x["kind"], x["value"]) for x in plan["summary"]["overrides"]] == [("force_charge", 80.0)], plan["summary"]["overrides"]
    until = plans._ts(o["until_ts"])
    assert any(s["reserve_pct"] >= 80 for s in plan["steps"] if s["t"] < until), [(s["local"], s["reserve_pct"]) for s in plan["steps"][:12]]
    # a genset hold keeps the genset off in its window
    r = admin.post(f"/api/sites/{sid}/control/genset_hold", json={"hours": 2})
    assert r.status_code == 200 and r.json()["resolve"]["status"] in ("active", "held"), r.text
    with world.use():
        plan = plans.get(sid, r.json()["resolve"]["plan_id"])
    assert "genset_hold" in [x["kind"] for x in plan["summary"]["overrides"]]
    assert not any(s["genset_on"] for s in plan["steps"] if s["t"] < _now() + 2 * 3600 - 900)


# ------------------------------------------------------------------------------------------------ batch 5: role-operator-11 (role-analyst-12)

def test_a_closed_loop_site_whose_edge_never_reported_says_so(world):
    """Closed loop needs an edge device: with none ever heard from, nothing reaches the inverter, so the chip no longer reads "Following plan" on
    the strength of the site's readings (the Karachi demo site did); once the edge reports it does."""
    sid, kh = world.site, world.key(["telemetry:write"])
    with world.use():
        _closed_loop(sid)
    assert _control(world, sid) == {"key": "edge_offline", "label": "No edge device yet"}
    assert world.anon.post(f"/api/edge/{sid}/heartbeat", headers=kh, json=_env(1, uptime_s=60, control_state="executing")).status_code == 200
    assert _control(world, sid)["key"] == "following"
    with world.use():
        _tier(sid, "advisory_writes")                                   # an advisory site keys its table in: no edge is needed
    assert _control(world, sid)["key"] == "unconfirmed"


def _ack_world(world):
    """A closed-loop site with a plan sent to its edge and acknowledged in part: an items ack and a per_device ack."""
    sid, kh = world.site, world.key(["telemetry:write", "plans:read"])
    pid = _fake_plan(world)
    with world.use():
        _closed_loop(sid)
    world.anon.post(f"/api/edge/{sid}/heartbeat", headers=kh, json=_env(1, uptime_s=60, control_state="executing", edge_id="edge-07"))
    assert world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh).status_code == 200
    ack = {"plan_id": pid, "status": "partial", "per_device": [{"device_id": "inv1", "status": "partial", "writes_used_today": {"tou_table": 1},
           "applied": [{"primitive": "soc_floor_pct", "requested": 40, "written": 40, "readback": 40, "readback_ok": True},
                       {"primitive": "max_charge_w", "requested": 9000, "written": 8000, "readback": 8000, "readback_ok": True}],
           "rejected": [{"primitive": "gen_charge_allowed", "reason": "NOT_SUPPORTED"}]}]}
    assert world.anon.post(f"/api/edge/{sid}/ack", headers=kh, json=_env(1, **ack)).status_code == 200
    items = {"plan_id": pid, "status": "applied", "items": [{"device": "inv1", "primitive": "export_limit_w", "written": 0, "read_back": 500,
                                                                "status": "failed", "codes": ["READBACK_MISMATCH"]}]}
    return sid, pid, kh, items


def test_the_read_back_says_per_primitive_what_was_asked_written_and_read_back(world):
    sid, pid, kh, items = _ack_world(world)
    rb = world.c["viewer"].get(f"/api/sites/{sid}/control/readback").json()
    assert rb["plan_id"] == pid and rb["sent_at"] and rb["ack"]["outcome"] == "partial" and rb["ack"]["codes"] == ["NOT_SUPPORTED"], rb
    lines = {ln["primitive"]: ln for ln in rb["primitives"]}
    assert lines["soc_floor_pct"] == {"device": "inv1", "primitive": "soc_floor_pct", "requested": 40, "written": 40, "read_back": 40, "match": True,
                                      "status": "ok", "codes": []}
    assert lines["max_charge_w"]["status"] == "clamped" and lines["max_charge_w"]["match"] is True      # written below what was asked
    assert lines["gen_charge_allowed"] == {"device": "inv1", "primitive": "gen_charge_allowed", "requested": None, "written": None, "read_back": None,
                                           "match": None, "status": "rejected", "codes": ["NOT_SUPPORTED"]}
    assert rb["fidelity_today"] == "partial"
    # this contract's items: a value read back that differs is a mismatch with its code
    assert world.anon.post(f"/api/edge/{sid}/ack", headers=kh, json=_env(2, **items)).status_code == 200
    rb = world.c["viewer"].get(f"/api/sites/{sid}/control/readback").json()
    assert rb["primitives"] == [{"device": "inv1", "primitive": "export_limit_w", "requested": None, "written": 0, "read_back": 500, "match": False,
                            "status": "failed", "codes": ["READBACK_MISMATCH"]}], rb
    # before any send there is nothing to read back
    with world.use(), store.connect() as con:
        con.execute("DELETE FROM plan_messages")
    assert world.c["viewer"].get(f"/api/sites/{sid}/control/readback").json() == {"plan_id": None, "sent_at": None, "ack": None, "primitives": [],
                                                                                    "fidelity_today": "partial"}


def test_the_write_log_lists_the_days_writes_who_why_and_the_result(world, monkeypatch):
    _no_ticks(monkeypatch)
    sid, pid, kh, items = _ack_world(world)
    op, admin = world.c["operator"], world.c["admin"]
    assert op.post(f"/api/sites/{sid}/control/hold", json={"hours": 2, "soc_pct": 60, "reason": "Feeder trip expected"}).status_code == 200
    with world.use():
        (hold,) = sites.overrides(sid)
    assert admin.delete(f"/api/sites/{sid}/overrides/{hold['id']}").status_code == 200
    # a repeat pull of the same table is no write; the release that follows is one
    assert world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh).status_code == 200
    assert world.anon.post(f"/api/edge/{sid}/ack", headers=kh, json=_env(2, **items)).status_code == 200
    log = world.c["analyst"].get(f"/api/sites/{sid}/control/writes").json()
    assert log["day"] == datetime.now(PKT).date().isoformat()
    rows = log["rows"]
    assert [r["at"] for r in rows] == sorted((r["at"] for r in rows), reverse=True)                 # newest first
    kinds = [r["kind"] for r in rows]
    assert kinds.count("sent") == 1, kinds                                                         # two pulls of one table: one write
    sent = next(r for r in rows if r["kind"] == "sent")
    assert sent["source"] == "cloud" and sent["plan_id"] == pid and sent["release_reason"] is None
    placed = next(r for r in rows if r["kind"] == "override_placed")
    assert placed == {**placed, "source": "person", "who": "Operator", "override_kind": "reserve", "value": 60.0,
                      "why": "Feeder trip expected"}, placed
    cleared = next(r for r in rows if r["kind"] == "override_cleared")
    assert cleared["who"] == "Admin" and cleared["override_kind"] == "reserve", cleared
    writes = [r for r in rows if r["kind"] == "write"]
    assert {(w["primitive"], w["status"], w["match"]) for w in writes} == {("soc_floor_pct", "ok", True), ("max_charge_w", "clamped", True),
                                                                           ("gen_charge_allowed", "rejected", None), ("export_limit_w", "failed", False)}
    assert all(w["source"] == "edge" and w["who"] == "edge-07" for w in writes), writes
    acks = [r for r in rows if r["kind"] == "ack"]
    assert sorted(a["outcome"] for a in acks) == ["partial", "partial"] and all(a["plan_id"] == pid for a in acks), acks
    # filtered by primitive and by day; a day with nothing written is empty; a bad day is one sentence
    only = world.c["viewer"].get(f"/api/sites/{sid}/control/writes", params={"primitive": "export_limit_w"}).json()["rows"]
    assert [(r["kind"], r["primitive"]) for r in only] == [("write", "export_limit_w")], only
    assert world.c["viewer"].get(f"/api/sites/{sid}/control/writes", params={"day": "2020-01-01"}).json() == {"day": "2020-01-01", "rows": []}
    r = world.c["viewer"].get(f"/api/sites/{sid}/control/writes", params={"day": "yesterday"})
    assert r.status_code == 422 and r.json()["detail"] == "The day is a date written YYYY-MM-DD", r.text
    # no money in the log: a table issue's rupee gain stays on the control answer, for a role that sees money
    assert "_rs" not in json.dumps(rows)


# ------------------------------------------------------------------------------------------------ batch 5: flow-telemetry-plan-control-19

def test_the_fallback_test_releases_and_checks_the_edge_hands_back_and_takes_the_plan_again(world, monkeypatch):
    """UI-15's "Test fallback" (an administrator's, closed loop only): it releases the inverter to its native settings for 15 minutes and watches
    the edge's heartbeats: the test passes when the edge reports the native settings during the release and the plan again after it, and fails when
    it does neither in time."""
    ticks = _no_ticks(monkeypatch)
    sid, admin, op, kh = world.site, world.c["admin"], world.c["operator"], world.key(["telemetry:write"])
    beat = lambda n, state: world.anon.post(f"/api/edge/{sid}/heartbeat", headers=kh, json=_env(n, uptime_s=60, control_state=state))  # noqa: E731
    # below closed loop there is nothing to test; an operator may not; an edge not heard from cannot be tested
    r = admin.post(f"/api/sites/{sid}/control/test_fallback", json={})
    assert r.status_code == 409 and r.json()["code"] == "not_closed_loop", r.text
    with world.use():
        _closed_loop(sid)
    assert op.post(f"/api/sites/{sid}/control/test_fallback", json={}).json()["detail"] == "Testing the fallback is for an administrator"
    assert admin.post(f"/api/sites/{sid}/control/test_fallback", json={"minutes": 5}).status_code == 422
    r = admin.post(f"/api/sites/{sid}/control/test_fallback", json={})
    assert r.status_code == 409 and r.json()["code"] == "edge_offline", r.text
    assert ticks == []
    beat(1, "executing")
    r = admin.post(f"/api/sites/{sid}/control/test_fallback", json={})
    assert r.status_code == 200 and r.json()["resolve"]["status"] == "active", r.text            # the release goes to the edge at once
    t = r.json()["fallback_test"]
    assert t["status"] == "running" and t["by"] == "Admin" and plans._ts(t["until"]) - plans._ts(t["started_at"]) == 15 * 60, t
    assert r.json()["control"]["label"].startswith("Fallback test: released to native until "), r.json()["control"]
    with world.use():
        (o,) = sites.overrides(sid)
        message = plans.signed_message(sid, record=False)
    assert o["kind"] == "force_native" and o["reason"] == "Fallback test" and message["type"] == "release"
    assert admin.post(f"/api/sites/{sid}/control/test_fallback", json={}).json()["code"] == "test_running"
    # the edge hands the inverter to its native settings, then (after the release) takes the plan again
    beat(2, "released")
    t = world.c["viewer"].get(f"/api/sites/{sid}/control").json()["fallback_test"]
    assert t["status"] == "released" and t["released_at"], t
    beat(3, "executing")                                                    # still inside the release: not yet a restore
    with world.use():
        assert plans.fallback_test(sid)["status"] == "released"
        later = plans._ts(t["until"]) + 60
        assert plans.fallback_test(sid, later)["status"] == "restoring"
        plans.fallback_test_observe(sid, "following_plan", later)
        done = plans.fallback_test(sid, later)
    assert done["status"] == "passed" and done["restored_at"], done
    with world.use():
        acts = [a["action"] for a in store.list_audit()]
    assert "started a fallback test" in acts and "passed a fallback test" in acts


def test_a_fallback_test_the_edge_does_not_follow_fails_and_says_why():
    t = {"started_at": "2026-11-10T14:00:00+00:00", "until": "2026-11-10T14:15:00+00:00", "released_at": None, "restored_at": None, "by": "Admin"}
    t0, until = plans._ts(t["started_at"]), plans._ts(t["until"])
    step = plans.fallback_test_step
    assert step(t, "executing", t0 + 60) is None                                       # still running the plan: nothing to record
    assert step(t, "native", t0 - 60) is None                                          # a report from before the test says nothing
    assert plans.fallback_test_status(t, until + 299)["status"] == "running"
    assert plans.fallback_test_status(t, until + 300) == {**t, "status": "failed", "cause": "not_released"}
    rel = {**t, **step(t, "fallback", t0 + 120)}
    assert rel["released_at"] == "2026-11-10T14:02:00+00:00"
    assert step(rel, "executing", until - 1) is None                                    # back to the plan inside the release is no restore
    assert step(rel, "native", until + 5 * 60 + 1) is None
    assert plans.fallback_test_status(rel, until + 1800)["cause"] == "not_restored"
    assert step({**t, "released_at": None}, "released", until + 301) is None            # a native report long after the release ends is no release
    assert plans.fallback_test_status({**rel, "restored_at": "2026-11-10T14:20:00+00:00"}, until + 9999)["status"] == "passed"


# ------------------------------------------------------------------------------------------------ batch 6: security-roles-11

def _key_rows(sid: str) -> list[dict]:
    with store.connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM site_keys WHERE site_id = ? ORDER BY created, key_id", (sid,))]


def test_a_new_signing_key_is_made_owner_only_and_a_failed_chmod_cannot_leave_it_open(company, monkeypatch):
    import os
    import stat
    sid = _site(genset=False)
    opened = []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, *a, **k):
        opened.append((str(path), flags, mode))
        return real_open(path, flags, mode, *a, **k)

    def no_chmod(*a, **k):
        raise OSError("chmod refused")

    monkeypatch.setattr(delivery.os, "open", spy_open)
    monkeypatch.setattr(delivery.os, "chmod", no_chmod)
    old = os.umask(0o022)
    try:
        kid = delivery.public_key(sid)["key_id"]
    finally:
        os.umask(old)
    path = delivery._key_dir() / f"{sid}-{kid}.pem"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600              # owner-only from the first byte: never written open and narrowed after
    made = [o for o in opened if o[0] == str(path)]
    assert made and made[0][1] & os.O_EXCL and made[0][1] & os.O_CREAT and made[0][2] == 0o600, opened
    assert stat.S_IMODE(delivery._key_dir().stat().st_mode) & 0o077 == 0     # and the keys folder is the owner's alone


# ------------------------------------------------------------------------------------------------ batch 6: engine-delivery-13

def test_the_signing_key_is_found_by_its_name_in_the_keys_folder_after_the_data_folder_moves(tmp_path, monkeypatch):
    import shutil
    first, second = tmp_path / "first", tmp_path / "second"
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(first))
    (first / "companies").mkdir(parents=True)
    with store.use_company(first / "companies" / "c.db"):
        sid = _site(genset=False)
        before = delivery.public_key(sid)
        assert [r["private_path"] for r in _key_rows(sid)] == [f"{sid}-{before['key_id']}.pem"]   # its name in the keys folder, never a path
    shutil.copytree(first, second)
    shutil.rmtree(first)                                                   # the folder moved (or was restored onto another machine)
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(second))
    with store.use_company(second / "companies" / "c.db"):
        msg = delivery.sign(sid, {"contract": "ems-edge/1.0", "plan_id": "p1"})
        assert msg["signature"]["key_id"] == before["key_id"]               # the same key signs: every edge given its public key still accepts
        assert delivery.verify(msg, before["public_key"])
        assert len(_key_rows(sid)) == 1
        # a key kept before round 7 holds the absolute path of its time (here a Windows one): it is read by its name in today's keys folder
        with store.connect() as con:
            con.execute("UPDATE site_keys SET private_path = ? WHERE site_id = ?", (rf"C:\Users\someone\gridlytics\data\keys\c\{sid}-{before['key_id']}.pem", sid))
        assert delivery.sign(sid, {"plan_id": "p2"})["signature"]["key_id"] == before["key_id"]


def test_a_missing_signing_key_is_an_error_and_never_silently_replaced(company):
    sid = _site(genset=False)
    before = delivery.public_key(sid)
    (delivery._key_dir() / f"{sid}-{before['key_id']}.pem").unlink()
    with pytest.raises(delivery.KeyMissing) as missing:
        delivery.sign(sid, {"plan_id": "p1"})
    assert missing.value.key_id == before["key_id"] and "restore" in str(missing.value)
    assert len(_key_rows(sid)) == 1                                        # no second key was made in its place
    now = delivery.public_key(sid)
    assert now["key_id"] == before["key_id"] and now["public_key"] == before["public_key"] and now["missing"] is True
    # a file that is not the recorded key is refused as well: it would sign plans no edge accepts
    other = delivery.Ed25519PrivateKey.generate().private_bytes(delivery.serialization.Encoding.PEM, delivery.serialization.PrivateFormat.PKCS8,
                                                                delivery.serialization.NoEncryption())
    (delivery._key_dir() / f"{sid}-{before['key_id']}.pem").write_bytes(other)
    with pytest.raises(delivery.KeyMissing):
        delivery.sign(sid, {"plan_id": "p1"})
    assert delivery.public_key(sid)["missing"] is True and len(_key_rows(sid)) == 1


def test_two_first_signings_at_once_make_one_key(company, monkeypatch):
    import threading
    sid = _site(genset=False)
    meet = threading.Barrier(2)
    real = delivery.Ed25519PrivateKey.generate

    def slow_generate():
        try:
            meet.wait(timeout=0.5)                     # both callers inside key making at once, if nothing keeps the second out
        except threading.BrokenBarrierError:
            pass
        return real()

    monkeypatch.setattr(delivery.Ed25519PrivateKey, "generate", staticmethod(slow_generate))
    got, errors = [], []
    db = store.db_path()

    def first_sign():
        try:
            with store.use_company(db):
                got.append(delivery.sign(sid, {"plan_id": "p1"})["signature"]["key_id"])
        except Exception as exc:                       # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=first_sign) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(set(got)) == 1 and len(_key_rows(sid)) == 1, (got, _key_rows(sid))


def test_a_missing_key_refuses_the_edge_raises_an_alert_and_an_administrator_replaces_it(world):
    sid = world.site
    _fake_plan(world)
    kh = world.key(["plans:read"], role="viewer")
    with world.use():
        _closed_loop(sid)
        before = delivery.public_key(sid)
    assert world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh).status_code == 200
    with world.use():
        (delivery._key_dir() / f"{sid}-{before['key_id']}.pem").unlink()
    r = world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh)
    assert r.status_code == 409 and r.json()["code"] == "signing_key_missing", r.text
    assert before["key_id"] in r.json()["detail"] and "restore" in r.json()["detail"]
    with world.use():
        assert len(_key_rows(sid)) == 1
        with store.connect() as con:
            alert = con.execute("SELECT category, severity, state FROM alerts WHERE site_id = ? AND rule = 'signing_key_missing'", (sid,)).fetchone()
    assert dict(alert) == {"category": "Control", "severity": "critical", "state": "open"}
    edge = world.c["admin"].get(f"/api/sites/{sid}/edge").json()
    assert edge["public_key"]["key_id"] == before["key_id"] and edge["public_key"]["missing"] is True
    # replacing the key is an administrator's, with a reason, in a strict body
    for role in ("operator", "analyst", "viewer"):
        r = world.c[role].post(f"/api/sites/{sid}/edge/key", json={"reason": "lost in a move"})
        assert r.status_code == 403 and r.json()["detail"] == "Replacing the site's signing key is for an administrator", (role, r.text)
    assert world.anon.post(f"/api/sites/{sid}/edge/key", headers=world.key(["plans:read"]), json={"reason": "x"}).status_code == 403
    for body, said in (({}, "reason"), ({"reason": "lost", "key_id": "k"}, "key_id"), ({"reason": ""}, "reason")):
        r = world.c["admin"].post(f"/api/sites/{sid}/edge/key", json=body)
        assert r.status_code == 422 and said in r.json()["detail"].lower(), (body, r.text)
    with world.use():
        assert len(_key_rows(sid)) == 1
    r = world.c["admin"].post(f"/api/sites/{sid}/edge/key", json={"reason": "The keys folder was lost in a move"})
    assert r.status_code == 200, r.text
    new = r.json()["public_key"]
    assert new["key_id"] != before["key_id"] and new["missing"] is False and "new public key" in r.json()["detail"]
    with world.use():
        rows = {k["key_id"]: k for k in _key_rows(sid)}
        assert rows[before["key_id"]]["retired"] and rows[new["key_id"]]["retired"] is None
        line = [a for a in store.list_audit() if a["action"] == "replaced the site's signing key"]
        assert line and line[0]["detail"]["retired"] == [before["key_id"]] and line[0]["detail"]["key_id"] == new["key_id"], line
        with store.connect() as con:
            assert con.execute("SELECT state FROM alerts WHERE site_id = ? AND rule = 'signing_key_missing'", (sid,)).fetchone()["state"] == "resolved"
    msg = world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh).json()
    assert msg["signature"]["key_id"] == new["key_id"] and delivery.verify(msg, new["public_key"])


def test_a_restored_keys_folder_signs_again_and_closes_the_alert(world):
    sid = world.site
    _fake_plan(world)
    kh = world.key(["plans:read"], role="viewer")
    with world.use():
        _closed_loop(sid)
        kid = delivery.public_key(sid)["key_id"]
        path = delivery._key_dir() / f"{sid}-{kid}.pem"
        kept = path.read_bytes()
        path.unlink()
    assert world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh).status_code == 409
    with world.use():
        path.write_bytes(kept)                                             # the keys folder restored from the backup
    msg = world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=kh).json()
    assert msg["signature"]["key_id"] == kid
    with world.use(), store.connect() as con:
        assert [r["state"] for r in con.execute("SELECT state FROM alerts WHERE site_id = ? AND rule = 'signing_key_missing'", (sid,))] == ["resolved"]
        assert con.execute("SELECT count(*) FROM site_keys WHERE site_id = ?", (sid,)).fetchone()[0] == 1

# ------------------------------------------------------------------------------------------------ batch 6: engine-delivery-22

#: RFC 8785 Appendix B: IEEE 754 doubles (as their bits) and the text JCS (ECMAScript's Number serialisation) writes for each
_RFC8785_NUMBERS = [("0000000000000000", "0"), ("8000000000000000", "0"), ("0000000000000001", "5e-324"), ("8000000000000001", "-5e-324"),
                    ("7fefffffffffffff", "1.7976931348623157e+308"), ("ffefffffffffffff", "-1.7976931348623157e+308"),
                    ("4340000000000000", "9007199254740992"), ("c340000000000000", "-9007199254740992"),
                    ("4430000000000000", "295147905179352830000"), ("44b52d02c7e14af5", "9.999999999999997e+22"), ("44b52d02c7e14af6", "1e+23"),
                    ("44b52d02c7e14af7", "1.0000000000000001e+23"), ("444b1ae4d6e2ef4e", "999999999999999700000"),
                    ("444b1ae4d6e2ef4f", "999999999999999900000"), ("444b1ae4d6e2ef50", "1e+21"), ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
                    ("3eb0c6f7a0b5ed8d", "0.000001"), ("41b3de4355555553", "333333333.3333332"), ("41b3de4355555554", "333333333.33333325"),
                    ("41b3de4355555555", "333333333.3333333"), ("41b3de4355555556", "333333333.3333334"), ("41b3de4355555557", "333333333.33333343"),
                    ("becbf647612f3696", "-0.0000033333333333333333"), ("43143ff3c1cb0959", "1424953923781206.2")]


def test_the_canonical_json_is_rfc_8785():
    import struct
    for bits, text in _RFC8785_NUMBERS:
        x = struct.unpack(">d", bytes.fromhex(bits))[0]
        assert delivery.canonical({"n": x}) == ('{"n":%s}' % text).encode(), bits
    # RFC 8785 3.2.2's example: numbers, a string with escapes and literals
    raw = '{"numbers": [333333333.33333329, 1E30, 4.50, 2e-3, 0.000000000000000000000000001], "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/", "literals": [null, true, false]}'
    assert delivery.canonical(json.loads(raw)).decode() == \
        '{"literals":[null,true,false],"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'
    # RFC 8785 3.2.3: members sorted by their names' UTF-16 code units (a name above U+FFFF sorts before U+FB33)
    names = {"\u20ac": "Euro Sign", "\r": "Carriage Return", "\ufb33": "Hebrew Letter Dalet With Dagesh", "1": "One", "\U0001F600": "Emoji: Grinning Face",
             "\u0080": "Control", "\u00f6": "Latin Small Letter O With Diaeresis"}
    assert [k for k in json.loads(delivery.canonical(names))] == ["\r", "1", "\u0080", "\u00f6", "\u20ac", "\U0001F600", "\ufb33"]
    # a whole-number float is a number like any other: 20.0 and 20 are the same bytes, as a JavaScript or Go edge writes them
    assert delivery.canonical({"a": 20.0, "b": 1e-05, "c": 0.1 + 0.2, "d": -0.0}) == b'{"a":20,"b":0.00001,"c":0.30000000000000004,"d":0}'
    assert delivery.canonical({"a": 20}) == delivery.canonical({"a": 20.0})
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            delivery.canonical({"x": bad})


def _js_parse(text: str):
    """The message as a JavaScript edge reads it: every number one IEEE double (20.0 and 20 are the same value)."""
    return json.loads(text, parse_int=float, parse_float=float)


def test_a_signed_plan_verifies_after_a_non_python_edge_parses_and_rebuilds_it(company):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    import base64
    sid = _site(genset=False)
    _closed_loop(sid)
    assert plans.tick(sid)["status"] == "active"
    msg = plans.signed_message(sid)
    text = json.dumps(msg)                                                   # what the API sends: Python writes 20.0 and 1e-05
    assert ".0," in text or ".0}" in text                                    # the plan carries whole-number floats (soc_floor_pct 20.0)
    got = _js_parse(text)
    sig = base64.b64decode(got.pop("signature")["value"])
    pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(delivery.public_key(sid)["public_key"]))
    pub.verify(sig, delivery.canonical(got))                                 # raises when the edge's rebuild is not the signed bytes
    assert msg["signature"]["alg"] == "Ed25519"
    # a message stored before round 7 (signed over Python's own json.dumps) still verifies for a dispute
    kid, key = delivery._site_key(sid)
    body = {"plan_id": "p0", "floor": 20.0}
    legacy = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()
    old = {**body, "signature": {"alg": "Ed25519", "key_id": kid, "value": base64.b64encode(key.sign(legacy)).decode()}}
    assert delivery.verify(old, delivery.public_key(sid)["public_key"])
    assert not delivery.verify({**old, "floor": 21.0}, delivery.public_key(sid)["public_key"])


def test_the_published_signing_vectors_are_the_code_and_verify():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    import base64
    from gridlytics import contract
    files = contract.vector_files()
    assert list(files) == ["signing-vectors.json"]
    assert (CONTRACTS / "signing-vectors.json").read_text(encoding="utf-8") == files["signing-vectors.json"], \
        "signing-vectors.json drifted: run python scripts/export_contract.py"
    v = json.loads(files["signing-vectors.json"])
    assert v["canonicalization"].startswith("RFC 8785")
    assert len(v["canonical"]) >= 5
    for case in v["canonical"]:
        assert delivery.canonical(_js_parse(case["json"])).decode() == case["canonical"], case["name"]
    s = v["signature"]
    sent = _js_parse(s["message_json"])
    sig = sent.pop("signature")
    assert sig["key_id"] == s["key_id"] and delivery.canonical(sent).decode() == s["signed_text"]
    Ed25519PublicKey.from_public_bytes(base64.b64decode(s["public_key"])).verify(base64.b64decode(sig["value"]), s["signed_text"].encode())
    # the example's fallback block has the fields a plan's has (delivery.fallback_block; delivery-twin, audit brief-vs-build-07: no revert timer)
    assert set(sent["fallback"]) == set(delivery.fallback_block({"id": "example-site", "native": {"version": 1}}, 20.0))


# ------------------------------------------------------------------------------------------------ batch 6: engine-delivery-23

def _env6(seq: int, **extra) -> dict:
    return {"contract": "ems-edge/1.0", "seq": seq, "sent_at": "2026-11-06T10:00:00Z", **extra}


def _stored_events(sid: str) -> int:
    with store.connect() as con:
        return con.execute("SELECT count(*) FROM events WHERE site_id = ? AND source LIKE 'edge%'", (sid,)).fetchone()[0]


def test_an_edge_message_over_1_mib_is_refused_with_413_and_nothing_is_stored(world):
    sid = world.site
    kh = {**world.key(["telemetry:write"]), "Content-Type": "application/json"}
    # 300 events of 3.5 kB each: every field within its bound, 1.05 MB in all
    events = [{"ts": "2026-11-06T10:00:00Z", "type": "data_gap", "detail": {"note": "x" * 3500}} for _ in range(300)]
    body = json.dumps(_env6(1, events=events))
    assert len(body.encode()) > 1_048_576
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, content=body)
    assert r.status_code == 413 and r.json()["code"] == "too_large" and "1 MiB" in r.json()["detail"], r.text[:300]
    # sent in chunks with no length named, it is counted as it arrives
    chunks = (body[i:i + 65536].encode() for i in range(0, len(body), 65536))
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, content=chunks)
    assert r.status_code == 413, r.text[:300]
    # a heartbeat padded past 1 MiB with spaces is refused before it is read whole
    beat = json.dumps(_env6(1, uptime_s=5)) + " " * 1_048_576
    r = world.anon.post(f"/api/edge/{sid}/heartbeat", headers=kh, content=beat)
    assert r.status_code == 413, r.text[:300]
    with world.use():
        assert _stored_events(sid) == 0 and not sites.state(sid).get("edge_heartbeat")
    # under the cap the same events are stored
    r = world.anon.post(f"/api/edge/{sid}/events", headers=kh, json=_env6(1, events=events[:200]))
    assert r.status_code == 200 and r.json()["stored"] == 200, r.text[:300]
    # a body that is not JSON is one sentence, as before
    r = world.anon.post(f"/api/edge/{sid}/heartbeat", headers=kh, content="{not json")
    assert r.status_code == 422 and r.json()["detail"], r.text


@pytest.mark.parametrize("path,body,said", [
    ("events", _env6(2, events=[{"ts": "2026-11-06T10:00:00Z", "type": "data_gap", "detail": {"note": "x" * 5000}}]), "events.0.detail"),
    ("events", _env6(2, events=[{"ts": "2026-11-06T10:00:00Z", "type": "data_gap", "detail": {f"k{i}": i for i in range(80)}}]), "events.0.detail"),
    ("telemetry", _env6(2, columns=["ts", "grid_present"], rows=[[1794000000, 1]], quality={"flags": ["x" * 100] * 50}), "quality"),
    ("ack", _env6(2, plan_id="p1", status="partial", per_device=[{"device_id": "inv1", "status": "partial",
                                                                   "applied": [{"primitive": "slot_table", "written": {"prog": ["x" * 5000]}}]}]),
     "per_device.0.applied.0.written"),
    ("ack", _env6(2, plan_id="p1", status="applied", items=[{"device": "inv1", "primitive": "soc", "written": "x" * 200, "status": "ok"}]),
     "items.0.written"),
    ("capabilities", _env6(2, edge_version="1.0", devices=[{"device_id": "inv1", "role": "hybrid_inverter", "estimator": {"blob": "x" * 5000}}]),
     "devices.0.estimator"),
    ("heartbeat", _env6(2, uptime_s=5, active_plan="p" * 5000), "active_plan"),
])
def test_free_form_fields_of_an_edge_message_are_bounded(world, path, body, said):
    sid = world.site
    kh = world.key(["telemetry:write"])
    r = world.anon.post(f"/api/edge/{sid}/{path}", headers=kh, json=body)
    assert r.status_code == 422 and r.json()["code"] == "SCHEMA" and r.json()["detail"].startswith(said), (path, r.status_code, r.text[:300])
    with world.use():
        assert _stored_events(sid) == 0 and not sites.state(sid).get("edge_heartbeat")
        with store.connect() as con:
            assert con.execute("SELECT count(*) FROM acks WHERE site_id = ?", (sid,)).fetchone()[0] == 0
            assert con.execute("SELECT count(*) FROM capabilities WHERE site_id = ?", (sid,)).fetchone()[0] == 0
