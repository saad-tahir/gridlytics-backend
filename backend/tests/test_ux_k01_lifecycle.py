"""UX backend ask K1 (plan H11; §0.3.1, B21.2 item 3, C19.2 item 8, D9.1): `lifecycle` on the site and on each fleet row.

The redesigned app draws the owner's staged journey as a track of six stages (Set-up, Collecting data, Recommending, Delivering, Verifying, Reporting),
each Done, Now, Waiting or Not used, with its words and one next step by the reader's permissions; Fleet's Stage column and filter and the site
switcher show the current stage in words. Without the field the browser derived four stages and could not show Verifying and Reporting, and Fleet would
have needed a query per site. GET /sites/:id (and every answer that is the site) and every GET /fleet row now carry it: the stage, its words, its next
step's key and the six stages. A viewer reads states only, with the energy report as the one action; nothing in it is money."""
from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta, timezone

from gridlytics import lifecycle, money, sites, store, telemetry
from test_fix_api import ROLES, world  # noqa: F401  (world is a fixture)

PKT = timezone(timedelta(hours=5))
KEYS = {"stage", "words", "next", "target", "all_done", "stages"}


def _at(*args) -> int:
    return int(datetime(*args, tzinfo=PKT).timestamp())


def _lc(world, role: str = "admin") -> dict:
    r = world.c[role].get(f"/api/sites/{world.site}")
    assert r.status_code == 200, r.text
    return r.json()["lifecycle"]


def _by(lc: dict) -> dict:
    return {st["key"]: st for st in lc["stages"]}


def _row(world, role: str = "admin") -> dict:
    rows = world.c[role].get("/api/fleet").json()["sites"]
    return next(r for r in rows if r["id"] == world.site)["lifecycle"]


def _plan(world, belief: int, status: str = "active", stage: int | None = 0, valid_s: int = 3600) -> None:
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")  # noqa: E731
    with world.use(), store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, summary, steps) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", ("plan-" + secrets.token_hex(4), world.site, iso(belief), iso(belief), iso(belief),
                                                     iso(belief + valid_s), "a", status, json.dumps({"stage": stage, "status_label": "Following"}), "[]"))


def _readings(world, *stamps: int) -> None:
    with world.use():
        telemetry.write_rows([(world.site, t, "edge", 1.0, 1000.0) for t in stamps], ("site_id", "ts", "source_id", "grid_present", "pv_power_w"))


# ------------------------------------------------------------------------------------------------ through the API


def test_a_draft_says_the_first_set_up_step_activation_still_asks_for(world):
    lc = _lc(world)
    assert set(lc) == KEYS and [st["key"] for st in lc["stages"]] == list(lifecycle.STAGES)
    assert all(set(st) == {"key", "state", "words", "next", "target"} for st in lc["stages"])
    # Mill One has a connection, a battery and a genset, but no data source and no inverter: step A first
    assert (lc["stage"], lc["words"], lc["next"], lc["target"], lc["all_done"]) == ("setup", "Draft: step A of 6", "continue_setup", "a", False)
    st = _by(lc)
    assert [st[k]["state"] for k in lifecycle.STAGES] == ["now", "waiting", "waiting", "waiting", "not_used", "waiting"]
    assert st["collecting"]["words"] == "Starts when the site is activated"
    assert (st["verifying"]["words"], st["verifying"]["next"]) == ("No pilot (optional)", "start_pilot")
    assert st["reporting"]["words"] == "First statement a month after the first plan"
    # the same on the site's fleet row
    assert _row(world) == lc
    # by permission: an operator continues the steps it may edit; an analyst waits for an administrator; a viewer reads states only
    assert (_lc(world, "operator")["next"], _by(_lc(world, "operator"))["verifying"]["next"]) == ("continue_setup", None)
    assert (_lc(world, "analyst")["next"], _lc(world, "analyst")["target"]) == ("ask_to_activate", None)
    viewer = _lc(world, "viewer")
    assert (viewer["next"], viewer["words"]) == ("energy_report", "Draft: step A of 6")
    assert [s["next"] for s in viewer["stages"]] == [None] * 5 + ["energy_report"]
    assert [s["state"] for s in viewer["stages"]] == [s["state"] for s in lc["stages"]]
    # a data source with its grid signal confirmed: step B (the inverter) is next; then every step activation asks for is done
    with world.use():
        sites.update(world.site, {"data_source": {"kind": "edge", "grid_signal_confirmed": True}}, "test")
    assert (_lc(world)["words"], _lc(world)["target"]) == ("Draft: step B of 6", "b")
    with world.use():
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
    lc = _lc(world)
    assert (lc["words"], lc["next"], lc["target"]) == ("Set-up: not activated", "activate", "review")
    assert (_lc(world, "operator")["next"], _lc(world, "operator")["target"]) == ("ask_to_activate", None)


def test_activating_starts_collecting_data_and_the_days_are_counted_in_karachi_time(world):
    with world.use():
        sites.update(world.site, {"data_source": {"kind": "edge", "grid_signal_confirmed": True}}, "test")
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
    r = world.c["admin"].post(f"/api/sites/{world.site}/activate")
    assert r.status_code == 200, r.text
    lc = r.json()["lifecycle"]                                    # every answer that is the site carries it
    st = _by(lc)
    assert (st["setup"]["state"], st["setup"]["words"], st["setup"]["next"]) == ("done", "Active · native settings not recorded", "record_native")
    assert (lc["stage"], lc["words"], lc["next"]) == ("collecting", "Waiting for the edge device", "check_source")
    assert st["recommending"]["words"] == "The first plan arrives within 15 minutes"
    assert (st["delivering"]["state"], st["delivering"]["words"]) == ("waiting", "Waiting for the edge device to say what it can control")
    now = int(datetime.now(timezone.utc).timestamp())
    _readings(world, now - int(5.5 * 86400), now - 60)
    assert (_lc(world)["words"], _lc(world)["stage"]) == ("Collecting data: day 6 of 14", "collecting")
    assert _lc(world, "viewer")["next"] == "energy_report" and _lc(world, "analyst")["next"] is None
    # no reading for two hours: since when, in Pakistan time
    with world.use(), telemetry.connect() as con:
        con.execute("DELETE FROM minute WHERE site_id = ? AND ts > ?", (world.site, now - 7200))
    _readings(world, now - 7200)
    at = datetime.fromtimestamp(now - 7200, PKT)
    want = f"{at:%H:%M}" if at.date() == datetime.now(PKT).date() else f"{at:%a} {at.day} {at:%b}, {at:%H:%M}"
    assert _lc(world)["words"] == f"No data since {want}"
    # a CSV source offers its upload to whoever may upload
    with world.use():
        sites.update(world.site, {"data_source": {"kind": "csv", "grid_signal_confirmed": True}}, "test")
    assert _lc(world, "operator")["next"] == "upload_csv" and _lc(world, "analyst")["next"] is None


def test_fourteen_days_of_data_and_a_plan_make_a_recommendation_and_the_tier_is_raised_by_a_person(world):
    with world.use():
        sites.update(world.site, {"data_source": {"kind": "edge", "grid_signal_confirmed": True}}, "test")
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
        sites.set_status(world.site, "active")
    now = int(datetime.now(timezone.utc).timestamp())
    _readings(world, now - 20 * 86400, now - 60)
    lc = _lc(world)
    assert (_by(lc)["collecting"]["state"], _by(lc)["collecting"]["words"]) == ("done", "14 days of data")
    assert (lc["stage"], lc["words"], _by(lc)["recommending"]["words"]) == ("recommending", "Recommending", "The first plan arrives within 15 minutes")
    _plan(world, now - 40 * 86400, status="superseded")
    _plan(world, now - 600, stage=0)
    lc = _lc(world)
    rec = _by(lc)["recommending"]
    assert (rec["state"], rec["words"]) == ("now", "Recommendation · forecasts at Stage 0, Preliminary")
    # the native settings are not recorded: whoever may record them is sent there, the others to the plan
    assert (lc["next"], lc["target"]) == ("record_native", "e")
    assert _lc(world, "analyst")["next"] == "open_plan" and _lc(world, "viewer")["next"] == "energy_report"
    r = world.c["operator"].post(f"/api/sites/{world.site}/native", json={"work_mode": "tou", "slots": [{"start": "00:00", "mode": "self_use"}]})
    assert r.status_code == 200, r.text
    assert _lc(world)["next"] == "compare_native"                 # a site with a battery compares the plan with the inverter's own settings
    assert _by(_lc(world))["setup"]["words"] == "Active"
    # the edge has not said what it can control: delivering waits for it, whoever reads
    assert _by(_lc(world))["delivering"]["next"] is None
    with world.use(), store.connect() as con:
        con.execute("UPDATE sites SET tier_capable = 'closed_loop' WHERE id = ?", (world.site,))
    dl = _by(_lc(world))["delivering"]
    assert (dl["state"], dl["words"], dl["next"], dl["target"]) == ("waiting", "Monitor tier: recommendations only", "raise_tier", "d")
    assert _by(_lc(world, "operator"))["delivering"]["next"] == "ask_to_raise_tier"
    assert _by(_lc(world, "viewer"))["delivering"]["next"] is None
    # raising the tier is a person's act (PUT /tier); the track never does it
    with world.use():
        assert sites.get(world.site)["tier"] == "monitor"
    r = world.c["admin"].put(f"/api/sites/{world.site}/tier", json={"tier": "advisory_writes", "signoff": "Installer: A. Khan"})
    assert r.status_code == 200, r.text
    lc = r.json()["lifecycle"]
    assert _by(lc)["recommending"]["state"] == "done"
    assert (_by(lc)["delivering"]["state"], _by(lc)["delivering"]["words"], _by(lc)["delivering"]["next"]) == ("now", "Today's table not marked applied",
                                                                                                         "mark_applied")
    assert (lc["stage"], lc["words"]) == ("delivering", "Delivering · advisory")
    assert _by(_lc(world, "analyst"))["delivering"]["next"] is None   # marking a table applied is apply_plan's


def test_the_lifecycle_carries_no_money_and_a_key_gets_no_next_step(world):
    _plan(world, int(datetime.now(timezone.utc).timestamp()) - 600)
    for role in ROLES:
        lc = _lc(world, role)
        assert not any(money.is_money(k) for k in json.dumps(lc).replace('"', " ").split() if k.isidentifier())
        assert "Rs" not in json.dumps(lc)
    kh = world.key(["sites:read"])
    r = world.anon.get(f"/api/sites/{world.site}", headers=kh)
    assert r.status_code == 200, r.text
    lc = r.json()["lifecycle"]
    assert lc["next"] is None and all(st["next"] is None for st in lc["stages"])


# ------------------------------------------------------------------------------------------------ the stages' words, at a fixed time


def _facts(**over) -> dict:
    f = {"active": True, "setup_step": None, "native": True, "battery": True, "source_kind": "edge", "tier": "advisory_writes",
         "tier_capable": "closed_loop", "first_ts": _at(2026, 9, 1), "last_ts": _at(2026, 11, 12, 9, 59), "first_plan_ts": _at(2026, 9, 2),
         "forecast_stage": 1, "preliminary": False, "control_key": "following", "control_label": "Following plan (table applied)",
         "pilot_started": False, "blocks": [], "arm_today": None, "statement_made": True}
    return {**f, **over}


ALL = set(lifecycle.NEXT) | {"manage_sites", "edit_sites", "upload_data", "change_tier", "apply_plan", "start_pilot", "export", "energy_report"}
NOW = _at(2026, 11, 12, 10, 0)                                   # Thursday 12 November 2026, 10:00 in Karachi


def _assemble(f: dict, perms=ALL, now: int = NOW, states_only: bool = False) -> dict:
    return lifecycle.assemble(f, now, lambda p: p in perms, states_only)


def test_a_site_whose_every_stage_in_use_is_done_folds_to_delivering():
    lc = _assemble(_facts())
    assert [s["state"] for s in lc["stages"]] == ["done", "done", "done", "done", "not_used", "done"]
    assert (lc["stage"], lc["words"], lc["next"], lc["all_done"]) == ("delivering", "Delivering · advisory", None, True)
    assert _by(lc)["reporting"]["words"] == "October statement made" and _by(lc)["reporting"]["target"] == "2026-10"
    assert _assemble(_facts(tier="closed_loop"))["words"] == "Delivering · closed loop"


def test_reporting_waits_for_a_full_month_on_a_plan_then_asks_for_its_statement():
    # first plan on 3 October: November is the first full month, so the first statement comes after 30 November
    lc = _assemble(_facts(first_plan_ts=_at(2026, 10, 3, 12, 0), statement_made=False))
    rep = _by(lc)["reporting"]
    assert (rep["state"], rep["words"], rep["next"], rep["target"]) == ("waiting", "First statement after 30 Nov", "energy_report", "2026-11")
    assert lc["all_done"] is False and lc["stage"] == "delivering"
    # a plan from the first minutes of 1 October (Karachi time): October ran on a plan, its statement is ready
    lc = _assemble(_facts(first_plan_ts=_at(2026, 10, 1, 0, 10), statement_made=False))
    rep = _by(lc)["reporting"]
    assert (rep["state"], rep["words"], rep["next"], rep["target"]) == ("now", "October statement ready", "generate_statement", "2026-10")
    assert (lc["stage"], lc["words"], lc["next"], lc["target"]) == ("reporting", "October statement ready", "generate_statement", "2026-10")
    # 30 September 19:30 UTC is 1 October in Karachi: the month is counted in Pakistan time
    assert _by(_assemble(_facts(first_plan_ts=int(datetime(2026, 9, 30, 19, 30, tzinfo=timezone.utc).timestamp()), statement_made=False)))[
        "reporting"]["state"] == "now"
    # who may not generate a statement is offered the energy report of the month
    assert _by(_assemble(_facts(statement_made=False), perms=ALL - {"export"}))["reporting"]["next"] == "energy_report"
    # across a year's end: a first plan on 15 December is first reported after 31 January of the next year
    rep = _by(_assemble(_facts(first_plan_ts=_at(2026, 12, 15)), now=_at(2026, 12, 20)))["reporting"]
    assert rep["words"] == "First statement after 31 Jan 2027"
    rep = _by(_assemble(_facts(first_plan_ts=_at(2026, 11, 1), statement_made=False), now=_at(2027, 1, 5)))["reporting"]
    assert (rep["words"], rep["target"]) == ("December statement ready", "2026-12")


def test_the_pilot_says_its_week_and_todays_arm():
    blocks = [(date(2026, 10, 26) + timedelta(days=7 * i), date(2026, 11, 1) + timedelta(days=7 * i)) for i in range(8)]
    lc = _assemble(_facts(pilot_started=True, blocks=blocks, arm_today="plan"))
    ver = _by(lc)["verifying"]
    assert (ver["state"], ver["words"], ver["next"]) == ("now", "Pilot week 3 of 8 · today runs the plan", "open_pilot")
    assert (lc["stage"], lc["words"]) == ("verifying", "Pilot week 3 of 8")
    assert _by(_assemble(_facts(pilot_started=True, blocks=blocks, arm_today="native")))["verifying"]["words"] == \
        "Pilot week 3 of 8 · today runs your own settings"
    later = [(date(2026, 11, 16), date(2026, 11, 22)), (date(2026, 11, 23), date(2026, 11, 29))]
    assert _by(_assemble(_facts(pilot_started=True, blocks=later)))["verifying"]["words"] == "Pilot starts on Mon 16 Nov"
    over = [(date(2026, 9, 1), date(2026, 10, 26))]
    assert (_by(_assemble(_facts(pilot_started=True, blocks=over)))["verifying"]["state"]) == "done"
    # not started: optional, and offered to whoever may start one
    ver = _by(_assemble(_facts(), perms=ALL - {"start_pilot"}))["verifying"]
    assert (ver["state"], ver["words"], ver["next"]) == ("not_used", "No pilot (optional)", None)


def test_delivering_asks_for_the_table_and_an_offline_edge():
    lc = _assemble(_facts(control_key="unconfirmed", control_label="Table not confirmed today"))
    dl = _by(lc)["delivering"]
    assert (dl["state"], dl["words"], dl["next"]) == ("now", "Today's table not marked applied", "mark_applied")
    assert (lc["stage"], lc["words"], lc["all_done"]) == ("delivering", "Delivering · advisory", False)
    dl = _by(_assemble(_facts(control_key="unconfirmed", control_label="New table issued: key it in and mark it")))["delivering"]
    assert dl["words"] == "New table issued: key it in and mark it"
    dl = _by(_assemble(_facts(tier="closed_loop", control_key="edge_offline", control_label="Edge offline")))["delivering"]
    assert (dl["state"], dl["words"], dl["next"]) == ("now", "Delivering · closed loop · edge offline", "check_source")
    # a tier allowed before the site is activated delivers nothing yet
    dl = _by(_assemble(_facts(active=False, setup_step="b", control_key="unconfirmed", control_label="Table not confirmed today")))["delivering"]
    assert (dl["state"], dl["words"], dl["next"]) == ("waiting", "Starts when the site is activated", None)
    # a CSV source without the inverter's mode stays at monitor until a file carries it
    dl = _by(_assemble(_facts(tier="monitor", tier_capable="monitor", source_kind="csv")))["delivering"]
    assert (dl["state"], dl["words"], dl["next"]) == ("waiting", "Monitor only until a file carries the inverter's mode", None)


def test_a_viewer_reads_states_only_with_the_energy_report_as_its_one_action():
    f = _facts(control_key="unconfirmed", control_label="Table not confirmed today", statement_made=False, first_plan_ts=_at(2026, 9, 1))
    admin = _assemble(f)
    viewer = _assemble(f, perms={"energy_report"}, states_only=True)
    assert [s["state"] for s in viewer["stages"]] == [s["state"] for s in admin["stages"]]
    assert [s["words"] for s in viewer["stages"]] == [s["words"] for s in admin["stages"]]
    assert [s["next"] for s in viewer["stages"]] == [None] * 5 + ["energy_report"]
    assert (viewer["stage"], viewer["next"], viewer["target"]) == ("delivering", "energy_report", "2026-10")
    key = _assemble(f, perms=set(), states_only=True)
    assert key["next"] is None and all(s["next"] is None for s in key["stages"])


def test_collecting_counts_fourteen_days_from_the_first_reading():
    lc = _assemble(_facts(first_ts=NOW - int(5.5 * 86400), last_ts=NOW - 60, tier="monitor", first_plan_ts=None))
    assert (lc["stage"], lc["words"], lc["next"]) == ("collecting", "Collecting data: day 6 of 14", "check_source")
    lc = _assemble(_facts(first_ts=None, last_ts=None, source_kind="oem_cloud", tier="monitor", first_plan_ts=None))
    assert lc["words"] == "Waiting for the first data"
    lc = _assemble(_facts(first_ts=NOW - 3 * 86400, last_ts=_at(2026, 11, 10, 9, 10), tier="monitor", first_plan_ts=None))
    assert lc["words"] == "No data since Tue 10 Nov, 09:10"
    lc = _assemble(_facts(first_ts=NOW - 3 * 86400, last_ts=_at(2026, 11, 12, 9, 10), tier="monitor", first_plan_ts=None))
    assert lc["words"] == "No data since 09:10"
