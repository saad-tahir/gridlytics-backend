"""UX backend ask K12 (plan H11; D9.2, D2.3, D2.9, D4.2; step 8, the Overview): `GET /sites/:id/now` adds what the status band and the This month
tiles read.

- `plan`: the plan in force, its id, when it was made and until when it stands (ISO 8601 in Karachi time), its status in words and its forecasts'
  stage, and `latest_attempt`, the newest solve when it failed or its guard held it after this plan was made; null when no plan is in force.
- `plan_expired_at`: when the last plan ran out, where that is why no plan is in force (a pause, a release or a native block is the control chip's).
- `applied_today`: who marked today's table applied, when and how, and whether a table issued after the mark names another plan, by the control
  chip's own rule (plans.applied_mark), so "Today's table applied by Bilal at 07:42." and the chip never disagree.
- `month.peak_import_kwh`, `pv_self_pct`, `outages`, `outages_covered`, and the swap tiles' `unserved_kwh` and `battery_cycles`: the month's energy,
  null without its data, never a zero; a day stored without its peak import (a CSV upload's) is aggregated again with the tariff's peak flags, once.

Words, times and energy, no money: every reader reads them (the role walk proves it)."""
from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timezone

from test_fix_api import _fake_plan, _now, world  # noqa: F401  (world is a fixture)

from gridlytics import plans, routes_sites, sites, store, telemetry, twin
from gridlytics.routes_common import PKT, karachi, today

KARACHI = "+05:00"


def _iso(t: int) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def _plan_row(sid: str, status: str, made: int, until: int, reason: str = "", slots: dict | None = None) -> str:
    pid = "plan-" + secrets.token_hex(4)
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, reason, summary, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, sid, _iso(made), _iso(made), _iso(made), _iso(until), "a", status, reason,
                     json.dumps({"status_label": "Solved within tolerance", "stage": 0}), "[]", json.dumps(slots or {"days": [], "fallback": None})))
    return pid


# ------------------------------------------------------------------------------------------------ K12: the band's plan facts

def test_the_band_reads_the_plan_in_force_with_its_times_in_karachi_time(world):
    a, v, sid = world.c["admin"], world.c["viewer"], world.site
    now = a.get(f"/api/sites/{sid}/now").json()
    assert now["plan"] is None and now["plan_expired_at"] is None                 # no plan made yet: nothing ran out either
    pid = _fake_plan(world)                                                      # made at this quarter hour, valid for an hour
    t0 = _now() // 900 * 900
    for c in (a, v):
        got = c.get(f"/api/sites/{sid}/now").json()
        assert got["plan"] == {"id": pid, "generated_at": karachi(t0), "valid_until": karachi(t0 + 3600), "status_label": "Following", "stage": None,
                               "latest_attempt": None}
        assert got["plan"]["generated_at"].endswith(KARACHI) and got["plan_expired_at"] is None
    # the newest solve failed after it: the plan in force stays, and the band says the newer solve did not make one
    with world.use():
        bad = _plan_row(sid, "failed", t0 + 60, t0 + 60 + 86400, reason="infeasible")
    late = a.get(f"/api/sites/{sid}/now").json()["plan"]
    assert late["id"] == pid and late["latest_attempt"] == {"id": bad, "status": "failed", "reason": "infeasible", "generated_at": karachi(t0 + 60)}
    # the plan ran out and none replaced it: no plan in force, and when it ran out
    with world.use(), store.connect() as con:
        con.execute("UPDATE plans SET valid_until = ? WHERE id = ?", (_iso(t0 - 600), pid))
    got = v.get(f"/api/sites/{sid}/now").json()
    assert got["plan"] is None and got["plan_expired_at"] == karachi(t0 - 600) and got["plan_expired_at"].endswith(KARACHI)
    # released to native: the control chip says why no plan is in force, not the expiry
    until = datetime.fromtimestamp(_now() + 7200, PKT).isoformat()
    assert a.post(f"/api/sites/{sid}/overrides", json={"kind": "force_native", "until": until}).status_code == 200
    got = a.get(f"/api/sites/{sid}/now").json()
    assert got["plan"] is None and got["plan_expired_at"] is None and got["control"]["key"] == "native"


# ------------------------------------------------------------------------------------------------ K12: today's apply mark

def test_the_band_says_who_marked_today_s_table_applied_by_the_control_chip_s_own_rule(world):
    a, op, v, sid = world.c["admin"], world.c["operator"], world.c["viewer"], world.site
    day = today().isoformat()
    t0 = _now() // 900 * 900
    with world.use():
        sites.set_tier_capable(sid, "advisory_writes")
        sites.set_tier_allowed(sid, "advisory_writes", "test")
        pid = _plan_row(sid, "active", t0, t0 + 86400, slots={"days": [{"date": day, "slots": []}], "fallback": None})
    got = a.get(f"/api/sites/{sid}/now").json()
    assert got["applied_today"] is None and got["control"]["key"] == "unconfirmed"
    r = op.post(f"/api/sites/{sid}/plan/apply", json={"plan_id": pid, "method": "vendor_app"})
    assert r.status_code == 200, r.text
    with world.use():
        row = plans.applied_rows(sid)[0]
    for c in (a, v):                                                             # who and when are no money: a viewer reads them too
        got = c.get(f"/api/sites/{sid}/now").json()
        assert got["applied_today"] == {"by": row["by"], "at": karachi(row["created"]), "method": "vendor_app", "plan_id": pid, "superseded": False}
        assert got["control"]["key"] == "following"
    assert row["by"] and got["applied_today"]["at"].endswith(KARACHI)
    assert abs(datetime.fromisoformat(got["applied_today"]["at"]).timestamp() - _now()) < 120
    # a table issued after the mark for another plan: the mark is of the table keyed before it, by the chip's own rule
    with world.use():
        other = _plan_row(sid, "superseded", t0, t0 + 86400)
        plans._write_issue(sid, day, other, "hold", _now() + 120)
    got = a.get(f"/api/sites/{sid}/now").json()
    assert got["applied_today"]["superseded"] is True and got["applied_today"]["plan_id"] == pid
    assert got["control"] == {"key": "unconfirmed", "label": "New table issued: key it in and mark it"}
    # a site nobody marked
    assert a.get(f"/api/sites/{world.other}/now").json()["applied_today"] is None


# ------------------------------------------------------------------------------------------------ K12: the This month tiles

def _minute_rows(sid: str, day: date, until_h: float = 24.0, outage: tuple[int, int, float] | None = None) -> None:
    """A day of readings at Mill One: 50 kW imported 03:00 to 04:00 (off-peak), 100 kW 19:00 to 20:00 (the peak window), solar 60 kW 10:00 to 12:00
    with 15 kW of it exported, the battery discharging 50 kW 13:00 to 14:00; `outage` (start minute, minutes, unserved kW) with the grid down."""
    t0 = twin.day_start_utc(day)
    rows = []
    for m in range(int(until_h * 60)):
        imp = 50.0 if 180 <= m < 240 else 100.0 if 1140 <= m < 1200 else 0.0
        pv, exp = (60.0, 15.0) if 600 <= m < 720 else (0.0, 0.0)
        bat = 50.0 if 780 <= m < 840 else 0.0
        grid, unserved = 1.0, 0.0
        if outage and outage[0] <= m < outage[0] + outage[1]:
            grid, unserved, imp = 0.0, outage[2], 0.0
        rows.append((sid, t0 + 60 * m, "edge", grid, imp * 1000 / 60, exp * 1000 / 60, pv * 1000, bat * 1000, unserved * 1000))
    telemetry.write_rows(rows, ("site_id", "ts", "source_id", "grid_present", "grid_import_wh", "grid_export_wh", "pv_power_w", "battery_power_w",
                                "unserved_w"))


def test_this_month_reads_the_peak_window_import_solar_used_on_site_and_outages_covered(world, monkeypatch):
    """The 3rd of October, 14:00: the 1st and 2nd whole, the 3rd until 14:00. The 1st's day row is stored without its peak import (a CSV upload's
    day is aggregated without the tariff's peak flags), so the month's figure needs it aggregated again, once."""
    a, v, sid = world.c["admin"], world.c["viewer"], world.site               # Mill One: B-2(b) at FESCO, a 200 kWh pack
    d1, d2, d3 = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
    now = twin.day_start_utc(d3) + 14 * 3600
    monkeypatch.setattr(routes_sites, "now_ts", lambda: now)
    monkeypatch.setattr(routes_sites, "today", lambda: d3)
    with world.use():
        conn = sites.connections(sid)[0]
        mask = plans.peak_mask(conn, twin.minutes_of(d1))
        assert mask[1140:1200].all() and not mask[180:240].any()               # 19:00 to 20:00 is in October's peak window, 03:00 to 04:00 is not
        _minute_rows(sid, d1, outage=(720, 30, 0.0))                           # an outage the battery and the solar carried
        _minute_rows(sid, d2, outage=(720, 20, 5.0))                           # one that left 5 kW of critical load unserved
        _minute_rows(sid, d3, until_h=14.0)
        telemetry.aggregate_day(sid, d1)                                       # as a CSV upload stores it: no peak import
        telemetry.aggregate_day(sid, d2, plans.peak_mask(conn, twin.minutes_of(d2)))
        assert "peak_import_kwh" not in telemetry.days(sid, d1.isoformat(), d1.isoformat())[0]
    month = a.get(f"/api/sites/{sid}/now").json()["month"]
    with world.use():
        rows = telemetry.days(sid, d1.isoformat(), d3.isoformat())
    assert [r.get("peak_import_kwh") for r in rows] == [100.0, 100.0, 0.0]    # the 1st aggregated again with its flags, and written back
    unserved = round(sum(r["unserved_kwh"] for r in rows), 1)
    assert unserved > 0
    energy = {k: month[k] for k in ("peak_import_kwh", "pv_self_pct", "outages", "outages_covered", "unserved_kwh", "battery_cycles")}
    # solar: 3 x 120 kWh made, 3 x 30 exported: 75 % used on site; the battery: 3 x 50 kWh out of a 200 kWh pack, 0.75 of a cycle
    assert energy == {"peak_import_kwh": 200.0, "pv_self_pct": 75.0, "outages": 2, "outages_covered": 1, "unserved_kwh": unserved,
                      "battery_cycles": 0.75}
    assert {k: v.get(f"/api/sites/{sid}/now").json()["month"][k] for k in energy} == energy     # energy and counts: a viewer reads them too
    # the month's import and solar are the same day rows' sums as before
    assert month["pv_kwh"] == 360.0 and month["import_kwh"] == round(sum(r["import_kwh"] for r in rows), 1)
    # off-grid: no peak window and no outage, its unserved load the swap tile's figure
    with world.use():
        _minute_rows(world.other, d1)
        telemetry.aggregate_day(world.other, d1)
    assert a.put(f"/api/sites/{world.other}/connection", json={"regime": "off_grid"}).status_code == 200
    other = a.get(f"/api/sites/{world.other}/now").json()["month"]
    assert (other["peak_import_kwh"], other["outages"], other["outages_covered"]) == (None, None, None)
    assert other["unserved_kwh"] == 0.0 and other["pv_self_pct"] == 75.0 and other["battery_cycles"] is None      # no battery at Site Two


def test_this_month_without_a_reading_has_no_figure_never_a_zero(world):
    month = world.c["admin"].get(f"/api/sites/{world.site}/now").json()["month"]
    assert {k: month[k] for k in ("peak_import_kwh", "pv_self_pct", "outages", "outages_covered", "unserved_kwh", "battery_cycles")} == dict.fromkeys(
        ("peak_import_kwh", "pv_self_pct", "outages", "outages_covered", "unserved_kwh", "battery_cycles"))


# ------------------------------------------------------------------------------------------------ K12 check

def test_with_no_plan_in_force_the_band_reads_the_newest_solve_its_guard_held_or_that_failed(world):
    """K12 check: the first solve held back by its guard (or one that failed) left /now with nothing to say: no plan was in force, so there was no
    `plan.latest_attempt`, and nothing ran out. The top-level `latest_attempt` now carries that solve wherever no plan is in force, for the band's warn
    line and "Re-solve now" (D2.3, D2.13); a plan in force keeps its own (`plan.latest_attempt`), and a pause, a release or a native block is the
    control chip's."""
    a, v, sid = world.c["admin"], world.c["viewer"], world.site
    t0 = _now() // 900 * 900
    assert a.get(f"/api/sites/{sid}/now").json()["latest_attempt"] is None      # no solve yet
    with world.use():
        held = _plan_row(sid, "held", t0 - 900, t0 + 86400, reason="guard_soc_floor")
    want = {"id": held, "status": "held", "reason": "guard_soc_floor", "generated_at": karachi(t0 - 900)}
    for c in (a, v):                                                            # words and a time: a viewer reads it too
        got = c.get(f"/api/sites/{sid}/now").json()
        assert (got["plan"], got["plan_expired_at"], got["latest_attempt"]) == (None, None, want)
        assert got["latest_attempt"]["generated_at"].endswith(KARACHI)
    with world.use(), store.connect() as con:
        con.execute("UPDATE plans SET status = 'failed', reason = 'infeasible' WHERE id = ?", (held,))
    assert a.get(f"/api/sites/{sid}/now").json()["latest_attempt"] == {**want, "status": "failed", "reason": "infeasible"}
    # a plan in force: the top-level field is null, and a newer failed solve is the plan's own latest_attempt
    pid = _fake_plan(world)
    got = a.get(f"/api/sites/{sid}/now").json()
    assert got["plan"]["id"] == pid and got["plan"]["latest_attempt"] is None and got["latest_attempt"] is None
    with world.use():
        bad = _plan_row(sid, "failed", t0 + 60, t0 + 86400, reason="timeout")
    got = a.get(f"/api/sites/{sid}/now").json()
    assert got["plan"]["latest_attempt"]["id"] == bad and got["latest_attempt"] is None
    # the plan ran out and the solves since failed: no plan in force, when it ran out, and the newest failed solve
    with world.use(), store.connect() as con:
        con.execute("UPDATE plans SET valid_until = ? WHERE id = ?", (_iso(t0 - 600), pid))
    got = a.get(f"/api/sites/{sid}/now").json()
    assert got["plan"] is None and got["plan_expired_at"] == karachi(t0 - 600)
    assert got["latest_attempt"] == {"id": bad, "status": "failed", "reason": "timeout", "generated_at": karachi(t0 + 60)}
    # released to native: the control chip says why no plan is in force
    until = datetime.fromtimestamp(_now() + 7200, PKT).isoformat()
    assert a.post(f"/api/sites/{sid}/overrides", json={"kind": "force_native", "until": until}).status_code == 200
    got = a.get(f"/api/sites/{sid}/now").json()
    assert (got["plan"], got["plan_expired_at"], got["latest_attempt"], got["control"]["key"]) == (None, None, None, "native")


def test_this_month_has_no_unserved_load_or_cycles_where_no_day_row_carries_them_never_a_zero():
    """K12 check: a month whose day rows carry no `unserved_kwh` and no `discharge_kwh` (rows stored without them) read 0 kWh unserved and 0 battery
    cycles, a missing key counted as 0. Each is now None where no day row of the month carries it, as the fleet's totals are; a row that carries it
    gives the month its figure."""
    site = {"id": "s", "connections": [{"data": {"regime": "net_metering"}}]}
    rows = [{"day": "2026-11-01", "pv_kwh": 100.0, "export_kwh": 0.0}, {"day": "2026-11-02", "pv_kwh": 80.0}]
    got = routes_sites._month_energy(site, rows, {"e_nom_kwh": 200.0}, (None, None))
    assert (got["unserved_kwh"], got["battery_cycles"], got["pv_self_pct"]) == (None, None, 100.0)
    rows[1].update(unserved_kwh=2.5, discharge_kwh=50.0)
    got = routes_sites._month_energy(site, rows, {"e_nom_kwh": 200.0}, (None, None))
    assert (got["unserved_kwh"], got["battery_cycles"]) == (2.5, 0.25)
    # a day of zero is a figure: 0, not None
    rows[1].update(unserved_kwh=0.0, discharge_kwh=0.0)
    got = routes_sites._month_energy(site, rows, {"e_nom_kwh": 200.0}, (None, None))
    assert (got["unserved_kwh"], got["battery_cycles"]) == (0.0, 0.0)


# ------------------------------------------------------------------------------------------------ K12, the senior review

def test_the_newest_solve_that_made_no_plan_is_one_rule_beside_a_plan_in_force_and_without_one(world, monkeypatch):
    """UX K review: the band's warn line reads the newest solve that failed or was held, as `plan.latest_attempt` beside a plan in force
    (plans.current) and as the top-level `latest_attempt` where none is (the route). Each had its own query and its own list of statuses, and only the
    route's told two solves stored in the same second apart by the order they were stored. Both now read plans.newest_unplanned."""
    a, sid = world.c["admin"], world.site
    t0 = _now() // 900 * 900
    one = {"id": "made-up", "status": "held", "reason": "guard_soc_floor", "generated_at": _iso(t0 + 120)}
    monkeypatch.setattr(plans, "newest_unplanned", lambda site_id, con=None: dict(one))
    want = {**one, "generated_at": karachi(t0 + 120)}
    got = a.get(f"/api/sites/{sid}/now").json()
    assert (got["plan"], got["latest_attempt"]) == (None, want)                   # no plan in force: the top-level one
    pid = _fake_plan(world)
    got = a.get(f"/api/sites/{sid}/now").json()
    assert got["plan"]["id"] == pid and got["plan"]["latest_attempt"] == want and got["latest_attempt"] is None
    with world.use():
        assert plans.current(sid)["latest_attempt"] == one                        # the alerts' plan_failed reads the same


def test_the_newest_solve_of_one_second_is_the_one_stored_last(world):
    """Two solves stored in the same second (a re-solve beside the scheduled tick): the one stored last is the newer, for the plan's
    `latest_attempt` as for the Overview's (UX K review)."""
    sid = world.site
    t0 = _now() // 900 * 900
    with world.use():
        failed = _plan_row(sid, "failed", t0 - 60, t0 + 86400, reason="infeasible")
        held = _plan_row(sid, "held", t0 - 60, t0 + 86400, reason="guard_soc_floor")
        assert plans.newest_unplanned(sid)["id"] == held
        with store.connect() as con:
            con.execute("UPDATE plans SET status = 'active', reason = '' WHERE id = ?", (held,))
        assert plans.newest_unplanned(sid) is None                                # the newest made a plan: nothing failed after it
        assert plans.current(sid)["id"] == held and "latest_attempt" not in plans.current(sid)
    assert failed


def test_today_and_this_month_count_the_outages_covered_by_one_rule(world, monkeypatch):
    """UX K review: today's "Outages covered" (`today_kpis`) and This month's (`month.outages`, `month.outages_covered`) each counted the outages and
    those with no critical load unserved with its own copy of the rule. Both now read routes_sites._outage_counts."""
    a, v, sid = world.c["admin"], world.c["viewer"], world.site
    d = date(2026, 10, 3)
    monkeypatch.setattr(routes_sites, "now_ts", lambda: twin.day_start_utc(d) + 14 * 3600)
    monkeypatch.setattr(routes_sites, "today", lambda: d)
    with world.use():
        _minute_rows(sid, d, until_h=14.0)
    spans = []
    monkeypatch.setattr(routes_sites, "_outage_counts", lambda site_id, t0, t1: spans.append((t0, t1)) or (7, 3))
    routes_sites._MONTH_OUTAGES.items.clear()
    for c in (a, v):
        got = c.get(f"/api/sites/{sid}/now").json()
        assert (got["today_kpis"]["outages"], got["today_kpis"]["outages_covered"]) == (7, 3)
        assert (got["month"]["outages"], got["month"]["outages_covered"]) == (7, 3)
    starts = {t0 for t0, _ in spans}
    assert starts == {twin.day_start_utc(d), twin.day_start_utc(date(2026, 10, 1))}  # today from its midnight, the month from the 1st, Karachi time
