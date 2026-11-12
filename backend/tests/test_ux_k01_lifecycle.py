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

from gridlytics import lifecycle, money, routes_sites, sites, store, telemetry, verify
from test_fix_api import ROLES, world  # noqa: F401  (world is a fixture)

PKT = timezone(timedelta(hours=5))
KEYS = {"stage", "words", "next", "target", "all_done", "stages"}


def _at(*args) -> int:
    return int(datetime(*args, tzinfo=PKT).timestamp())


def _today() -> date:
    return datetime.now(PKT).date()


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


def test_the_lifecycle_carries_no_money(world):
    _plan(world, int(datetime.now(timezone.utc).timestamp()) - 600)
    for role in ROLES:
        lc = _lc(world, role)
        assert not any(money.is_money(k) for k in json.dumps(lc).replace('"', " ").split() if k.isidentifier())
        assert "Rs" not in json.dumps(lc)


def test_a_machine_key_reads_the_sites_settings_without_its_lifecycle(world):
    """UX K1 check (audit security-roles-07): `sites:read` is the site's settings; the site's data, its pilot and its statements are in no scope. The
    track speaks of all three ("Pilot week 2 of 6 · today runs your own settings", "October statement made", "No data since 19:41"), so a key's site
    answer leaves it out, as it was before K1, while a person's carries it."""
    now = int(datetime.now(timezone.utc).timestamp())
    with world.use(), store.connect() as con:
        sites.update(world.site, {"data_source": {"kind": "edge", "grid_signal_confirmed": True}}, "test")
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
        sites.set_status(world.site, "active")
        con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?)", (world.site, "{}", store._now(), store._now()))
        con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm) VALUES (?,?,?,?)",
                    (world.site, (_today() - timedelta(days=3)).isoformat(), (_today() + timedelta(days=3)).isoformat(), "native"))
    _readings(world, now - 3 * 86400, now - 7200)
    assert "Pilot week" in _by(_lc(world))["verifying"]["words"]
    kh = world.key(["sites:read"])
    r = world.anon.get(f"/api/sites/{world.site}", headers=kh)
    assert r.status_code == 200, r.text
    assert "lifecycle" not in r.json() and r.json()["id"] == world.site
    assert "Pilot" not in r.text and "No data since" not in r.text


# ------------------------------------------------------------------------------------------------ the stages' words, at a fixed time


def _facts(**over) -> dict:
    f = {"active": True, "setup_step": None, "native": True, "battery": True, "source_kind": "edge", "tier": "advisory_writes", "tier_block": None,
         "first_ts": _at(2026, 9, 1), "last_ts": _at(2026, 11, 12, 9, 59), "first_plan_ts": _at(2026, 9, 2), "failing": None, "activated_ts": None,
         "forecast_stage": 1, "preliminary": False, "control_key": "following", "control_label": "Following plan (table applied)",
         "pilot_started": False, "pilot": None, "statement_made": True}
    return {**f, **over}


def _pilot(blocks: list, arm: str | None = None, today: date = date(2026, 11, 12)) -> dict:
    """The facts of a started pilot whose switchback blocks are `blocks` ((start, end) pairs), today's block running `arm`."""
    rows = [{"block_start": a.isoformat(), "block_end": b.isoformat(), "arm": arm if a <= today <= b else "plan"} for a, b in blocks]
    return {"pilot_started": True, "pilot": verify.pilot_progress_of(rows, today)}


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
    lc = _assemble(_facts(**_pilot(blocks, "plan")))
    ver = _by(lc)["verifying"]
    assert (ver["state"], ver["words"], ver["next"]) == ("now", "Pilot week 3 of 8 · today runs the plan", "open_pilot")
    assert (lc["stage"], lc["words"]) == ("verifying", "Pilot week 3 of 8")
    assert _by(_assemble(_facts(**_pilot(blocks, "native"))))["verifying"]["words"] == "Pilot week 3 of 8 · today runs your own settings"
    later = [(date(2026, 11, 16), date(2026, 11, 22)), (date(2026, 11, 23), date(2026, 11, 29))]
    assert _by(_assemble(_facts(**_pilot(later))))["verifying"]["words"] == "Pilot starts on Mon 16 Nov"
    over = [(date(2026, 9, 1), date(2026, 10, 26))]
    assert (_by(_assemble(_facts(**_pilot(over))))["verifying"]["state"]) == "done"
    assert _by(_assemble(_facts(pilot_started=True, pilot=None)))["verifying"]["words"] == "Pilot started"
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
    # a CSV source without the inverter's mode stays at monitor until a file carries it (PUT /tier's no_inverter_mode): the upload is the step
    dl = _by(_assemble(_facts(tier="monitor", tier_block="no_inverter_mode", source_kind="csv")))["delivering"]
    assert (dl["state"], dl["words"], dl["next"]) == ("waiting", "Monitor only until a file carries the inverter's mode", "upload_csv")
    dl = _by(_assemble(_facts(tier="monitor", tier_block="above_capable", source_kind="edge")))["delivering"]
    assert (dl["state"], dl["words"], dl["next"]) == ("waiting", "Waiting for the edge device to say what it can control", None)


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


# ------------------------------------------------------------------------------------------------ the check of batch k1


def _active_csv_site(world) -> None:
    """Mill One active on a CSV source (which sites._source_capable makes capable of advisory writes as it is chosen) with its inverter."""
    with world.use():
        sites.update(world.site, {"data_source": {"kind": "csv", "grid_signal_confirmed": True}}, "test")
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
        sites.set_status(world.site, "active")
        assert sites.get(world.site)["tier_capable"] == "advisory_writes"


def test_a_csv_site_is_offered_the_raise_of_its_tier_only_when_put_tier_takes_it(world):
    """UX K1 check: the track offered `raise_tier` to an administrator at a CSV site none of whose files carries inverter_mode_active, which PUT /tier
    refuses (no_inverter_mode). The track and the refusal now read one rule (routes_sites._tier_refusal): such a site says it is monitor-only until a
    file carries the mode, and offers the upload; once one does, the raise is offered and PUT /tier takes it."""
    _active_csv_site(world)
    now = int(datetime.now(timezone.utc).timestamp())
    _readings(world, now - 3 * 86400, now - 60)
    r = world.c["operator"].post(f"/api/sites/{world.site}/native", json={"work_mode": "tou", "slots": [{"start": "00:00", "mode": "self_use"}]})
    assert r.status_code == 200, r.text
    dl = _by(_lc(world))["delivering"]
    assert (dl["state"], dl["words"], dl["next"], dl["target"]) == ("waiting", "Monitor only until a file carries the inverter's mode", "upload_csv", None)
    assert _by(_lc(world, "analyst"))["delivering"]["next"] is None            # uploading is upload_data's
    assert _by(_row(world))["delivering"] == dl                               # the fleet row says the same
    r = world.c["admin"].put(f"/api/sites/{world.site}/tier", json={"tier": "advisory_writes", "signoff": "Installer: A. Khan"})
    assert (r.status_code, r.json()["code"]) == (422, "no_inverter_mode"), r.text
    # a file carrying the inverter's mode: the raise is offered, and taken
    with world.use():
        telemetry.write_rows([(world.site, now - 120, "csv", 1.0, "self_use")], ("site_id", "ts", "source_id", "grid_present", "inverter_mode_active"))
    dl = _by(_lc(world))["delivering"]
    assert (dl["words"], dl["next"], dl["target"]) == ("Monitor tier: recommendations only", "raise_tier", "d")
    r = world.c["admin"].put(f"/api/sites/{world.site}/tier", json={"tier": "advisory_writes", "signoff": "Installer: A. Khan"})
    assert r.status_code == 200, r.text
    assert _by(r.json()["lifecycle"])["delivering"]["state"] == "now"


def test_the_raise_of_the_tier_waits_for_the_native_capture_as_put_tier_does(world):
    """The same rule's no_capture: an edge site capable of closed loop without the inverter's native settings is sent to record them (step E)."""
    with world.use(), store.connect() as con:
        sites.update(world.site, {"data_source": {"kind": "edge", "grid_signal_confirmed": True}}, "test")
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
        sites.set_status(world.site, "active")
        con.execute("UPDATE sites SET tier_capable = 'closed_loop' WHERE id = ?", (world.site,))
    dl = _by(_lc(world))["delivering"]
    assert (dl["next"], dl["target"]) == ("record_native", "e")
    r = world.c["admin"].put(f"/api/sites/{world.site}/tier", json={"tier": "advisory_writes", "signoff": "x"})
    assert (r.status_code, r.json()["code"]) == (422, "no_capture"), r.text
    with world.use():
        assert routes_sites._tier_refusal(sites.get(world.site), "advisory_writes").code == "no_capture"
        assert routes_sites._tier_refusal(sites.get(world.site), "monitor") is None


def test_recommending_promises_the_first_plan_only_just_after_activation(world):
    """UX K1 check: "The first plan arrives within 15 minutes" was said for good at a monitor site with no plan, however long ago it was activated and
    however many ticks had failed (plan §0.3.2 says it at the moment of activation). Now: within half an hour of the activation; while the planning
    runs fail, what fails (as the Plan page's `failing`); after that, the Plan page says why, and the stage links to it."""
    with world.use():
        sites.update(world.site, {"data_source": {"kind": "edge", "grid_signal_confirmed": True}}, "test")
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
    r = world.c["admin"].post(f"/api/sites/{world.site}/activate")
    assert r.status_code == 200, r.text
    rec = _by(r.json()["lifecycle"])["recommending"]
    assert (rec["state"], rec["words"], rec["next"]) == ("now", "The first plan arrives within 15 minutes", None)
    # activated two hours ago, and no plan since
    two_hours = datetime.fromtimestamp(int(datetime.now(timezone.utc).timestamp()) - 7200, timezone.utc).isoformat(timespec="seconds")
    with world.use(), store.connect() as con:
        con.execute("UPDATE audit SET at = ? WHERE action = 'activated a site' AND target = ?", (two_hours, world.site))
    rec = _by(_lc(world))["recommending"]
    assert (rec["words"], rec["next"]) == ("No plan yet: the Plan page says why", "open_plan")
    assert _by(_lc(world, "viewer"))["recommending"]["words"] == rec["words"]
    # the planning runs failing: what fails, as the Plan page's `failing` reads it
    def runs(*rows):
        with world.use(), store.connect() as con:
            con.execute("DELETE FROM runs WHERE site_id = ?", (world.site,))
            for tick, status, reason in rows:
                con.execute("INSERT INTO runs (site_id, tick, started, status, reason) VALUES (?,?,?,?,?)", (world.site, tick, tick, status, reason))
    runs(("2026-11-12T08:00:00+00:00", "failed", "solve_failed"), ("2026-11-12T08:15:00+00:00", "error", "tick_error"),
         ("2026-11-12T08:30:00+00:00", "failed", "solve_failed"))
    assert _by(_lc(world))["recommending"]["words"] == "No plan yet: the last 3 planning runs failed"
    runs(("2026-11-12T08:00:00+00:00", "active", ""), ("2026-11-12T08:15:00+00:00", "error", "no_tariff_version"))
    assert _by(_lc(world))["recommending"]["words"] == "No plan yet: a grid connection's tariff has no version in force"
    runs(("2026-11-12T08:15:00+00:00", "error", "tick_error"))
    assert _by(_lc(world))["recommending"]["words"] == "No plan yet: the last planning run failed"
    # even within the half hour, a failing run is said, never the promise
    with world.use(), store.connect() as con:
        con.execute("UPDATE audit SET at = ? WHERE action = 'activated a site' AND target = ?", (store._now(), world.site))
    assert _by(_lc(world))["recommending"]["words"] == "No plan yet: the last planning run failed"


def test_delivering_is_not_done_while_the_plan_does_not_reach_the_inverter():
    """UX K1 check: Delivering read done ("Delivering · closed loop") under a pause, a release to native, the clock demotion and the edge's own
    reports, so the Overview folded the track into the chip while control was handed back. Those are now the current stage, in the control chip's
    words, with the step that looks into them; the switchback's own native-block day, a forced charge and a hold stay done."""
    for key, label, nxt in (("paused", "Control paused until 18:00", "open_plan"), ("native", "Released to native until Fri 13 Nov, 09:00", "open_plan"),
                            ("clock_demoted", "Control demoted: the inverter clock is 6 min off", "open_plan"),
                            ("edge_fallback", "Edge on fallback: the inverter runs its own settings", "check_source"),
                            ("edge_disabled", "Edge disabled control", "check_source"), ("edge_refused", "Edge refused the plan", "check_source")):
        lc = _assemble(_facts(tier="closed_loop", control_key=key, control_label=label))
        dl = _by(lc)["delivering"]
        assert (dl["state"], dl["words"], dl["next"]) == ("now", label, nxt), key
        assert (lc["all_done"], lc["stage"]) == (False, "delivering"), key
        # who may not check the source is sent to the plan, which says what the edge reports
        assert _by(_assemble(_facts(tier="closed_loop", control_key=key, control_label=label), perms=ALL - {"edit_sites"}))["delivering"]["next"] == \
            "open_plan", key
    for key, label in (("native_block", "Switchback: native settings today"), ("force_charge", "Forced charge to 90 % until 18:00"),
                       ("hold", "Hold until 18:00"), ("following", "Following plan")):
        lc = _assemble(_facts(tier="closed_loop", control_key=key, control_label=label))
        assert (_by(lc)["delivering"]["state"], lc["all_done"]) == ("done", True), key


def test_the_tracks_pilot_week_is_the_one_function_the_pilot_band_reads(world):
    """UX K1 check: "Pilot week N of M" was a formula of the track's own; it is verify.pilot_progress, which the Pilot band (K28) reads too, so the
    two say the same week, block and arm."""
    today = _today()
    start = today - timedelta(days=today.weekday()) - timedelta(days=14)        # a Monday two weeks ago: week 3 of a 12-week calendar
    with world.use(), store.connect() as con:
        sites.update(world.site, {"data_source": {"kind": "edge", "grid_signal_confirmed": True}}, "test")
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
        sites.set_status(world.site, "active")
        con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?)", (world.site, "{}", store._now(), store._now()))
        for i in range(12):
            a = start + timedelta(days=7 * i)
            con.execute("INSERT INTO switchback (site_id, block_start, block_end, arm) VALUES (?,?,?,?)",
                        (world.site, a.isoformat(), (a + timedelta(days=6)).isoformat(), "native" if i % 2 else "plan"))
    with world.use():
        p = verify.pilot_progress(world.site, today)
    assert (p["phase"], p["week"], p["weeks"], p["block"], p["blocks"], p["arm"]) == ("running", 3, 12, 3, 12, "plan")
    ver = _by(_lc(world))["verifying"]
    assert ver["words"] == f"Pilot week {p['week']} of {p['weeks']} · today runs the plan" == "Pilot week 3 of 12 · today runs the plan"
    assert verify.pilot_progress_of([], today) is None


def test_the_fleet_rows_collecting_day_is_the_tracks_day(world):
    """UX K1 check: the fleet row's `collecting_day` and the track's "Collecting data: day N of 14" were worked out twice; both are now
    lifecycle.collecting_day, and they agree on either side of the fourteenth day."""
    now = 1_800_000_000
    assert [lifecycle.collecting_day(f, now) for f in (None, now - 60, now - int(13.99 * 86400), now - 14 * 86400)] == [0, 1, 14, None]
    with world.use():
        sites.update(world.site, {"data_source": {"kind": "edge", "grid_signal_confirmed": True}}, "test")
        sites.put_asset(world.site, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
        sites.set_status(world.site, "active")
    t = int(datetime.now(timezone.utc).timestamp())
    for days, day, words in ((13.99, 14, "Collecting data: day 14 of 14"), (14.01, None, "14 days of data")):
        with world.use(), telemetry.connect() as con:
            con.execute("DELETE FROM minute WHERE site_id = ?", (world.site,))
        _readings(world, t - int(days * 86400), t - 60)
        row = next(r for r in world.c["admin"].get("/api/fleet").json()["sites"] if r["id"] == world.site)
        assert (row["collecting_day"], _by(row["lifecycle"])["collecting"]["words"]) == (day, words), days


def test_the_site_answer_reads_the_inverter_mode_without_waiting_for_the_telemetry_write_lock(world):
    """UX K review: the track's tier rule (routes_sites._tier_refusal) reads whether any file of a CSV site carries the inverter's mode on every site
    answer and every fleet row, where before only PUT /tier read it. The read took the telemetry file's one write lock (telemetry.connect, BEGIN
    IMMEDIATE), so while an import, an edge batch or the tick held it the site's page and Fleet waited up to the 30-second busy timeout (and then
    failed), and each page load stopped every writer for the length of the scan. It is a read (telemetry.reading, audit performance-live-03): with a
    writer holding the lock, both answers come at once and still say monitor-only."""
    import sqlite3
    import threading

    _active_csv_site(world)
    now = int(datetime.now(timezone.utc).timestamp())
    _readings(world, now - 3 * 86400, now - 60)
    with world.use():
        path = store.telemetry_path()
    writer = sqlite3.connect(path, timeout=1, isolation_level=None)
    got: dict = {}

    def read():
        got["site"] = world.c["admin"].get(f"/api/sites/{world.site}")
        got["fleet"] = world.c["admin"].get("/api/fleet")

    try:
        writer.execute("BEGIN IMMEDIATE")                     # another writer holds the telemetry file's write lock, as a long CSV import does
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(10)
        answered = not reader.is_alive()
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    reader.join(60)
    assert answered, "the site answer waited for the telemetry file's write lock"
    assert got["site"].status_code == 200 and got["fleet"].status_code == 200, (got["site"].text[:200], got["fleet"].text[:200])
    words = "Monitor only until a file carries the inverter's mode"
    assert _by(got["site"].json()["lifecycle"])["delivering"]["words"] == words
    assert _by(next(r for r in got["fleet"].json()["sites"] if r["id"] == world.site)["lifecycle"])["delivering"]["words"] == words


def test_a_site_allowed_a_higher_tier_before_its_first_plan_is_still_waiting_for_it():
    """UX K review: Recommending read "Done: the plan runs at the advisory tier" at a site whose tier was raised before any plan was made (PUT /tier
    asks for the native capture, not for a plan), so the track said a plan ran that never was. With no plan made, Recommending is the current stage at
    every tier, in the same words as at the monitor tier; once a plan is made, the tier above monitor makes it done."""
    for tier in ("advisory_writes", "closed_loop"):
        lc = _assemble(_facts(tier=tier, first_plan_ts=None, activated_ts=NOW - 600, statement_made=False))
        rec = _by(lc)["recommending"]
        assert (rec["state"], rec["words"], rec["next"]) == ("now", "The first plan arrives within 15 minutes", None), tier
        assert (lc["stage"], lc["words"], lc["all_done"]) == ("recommending", "Recommending", False), tier
        assert _by(lc)["reporting"]["words"] == "First statement a month after the first plan"
        rec = _by(_assemble(_facts(tier=tier, first_plan_ts=None, failing={"reason": "tick_error", "ticks": 3})))["recommending"]
        assert (rec["state"], rec["words"], rec["next"]) == ("now", "No plan yet: the last 3 planning runs failed", "open_plan"), tier
        rec = _by(_assemble(_facts(tier=tier)))["recommending"]
        assert (rec["state"], rec["words"]) == ("done", f"Done: the plan runs at the {lifecycle.TIER_WORDS[tier]} tier"), tier
