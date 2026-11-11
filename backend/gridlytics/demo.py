"""Demo mode is the twin (plan Part 9.6 and 7.4): four twin sites a Pakistani energy manager would recognise, with generated history run through
the real forecasters, the real optimiser and the real savings engine, kept live by the scheduler so no page is empty on day one.

The history runs the inverter's native settings for the first three days (the observation days before the first plan), then the plans compiled to
the brand's slot table, except on the native blocks of the switchback calendar: at the closed-loop site each new plan (its edge, which is the twin,
runs it), at an advisory site the one table a day its installer keys in (the nightly one) and marks applied.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import alerts, delivery, forecast, plans, sites, store, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))
DEMO_LINK = "demo"
OBSERVE_DAYS = 3
#: the steps of each day's plan in the demo history: 48 hours at 15 minutes, as the live ticks plan (plans.tick's default). A 24-hour plan values the
#: battery at midnight by its terminal price alone and drains what a 48-hour plan keeps for the next night; in eight paired 14-day rebuilds of the demo
#: farm the 48-hour history beat the 24-hour one in the continuous replay every time (round 4)
HISTORY_STEPS = 192
#: how often the demo history re-plans within a day, in hours. Live ticks re-plan every 15 minutes; a plan made at midnight and run all day with no
#: re-plan carried every forecast miss to the evening (round 5: the farm burnt 91 L on a day native burnt 47 L). Re-planning every 3 hours
#: was measured closest to the live behaviour at a seed time the demo can afford (GRIDLYTICS_DEMO_REPLAN_HOURS changes it; 24 is the old daily plan)
REPLAN_HOURS = 3
#: the wall clock the twin is held behind (the tests move it)
_clock = time.time
#: What a data source other than an edge lets a site reach (plan Part 2.3: an edge asserts its own tier in its capabilities message; a cloud or CSV
#: source has no write path, so its plans reach the site as slot tables a person keys in, as onboarding records it)
SOURCE_TIER = {"oem_cloud": "advisory_writes", "csv": "advisory_writes"}
#: How long the closed-loop demo site's edge counts as online after its last report: the edge is the twin, which reports when the 15-minute schedule
#: runs it (scheduler.tick_company: advance), where a real edge's heartbeat comes every minute
EDGE_FRESH_S = 20 * 60
#: Who keys an advisory demo site's daily table into the vendor app and marks it applied (the applied log's `by`). A spec's `marks` says when the
#: mark comes: 'same_day' (the farm), or 'next_day' (the Faisalabad mill, whose 'apply not confirmed' alert is the plan's story: its own day's
#: table is never marked yet)
INSTALLER = "Demo installer"


def _clock_minute() -> int:
    """The current minute's start (UTC epoch seconds): the twin writes only the minutes before it, so no reading is stamped after now."""
    return int(_clock()) // 60 * 60


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


DEMO_SITES = [
    {"key": "textile",
     "site": {"name": "Faisalabad textile mill (demo)", "city": "Faisalabad", "address": "Jhang Road industrial area", "lat": 31.42, "lon": 73.08,
              "disco": "FESCO", "site_type": "industrial_2_shift", "outage_unit": {"kind": "feeder", "code": "411205", "name": "Jhang Road Industrial"},
              "genset_start_control": "manual", "outage_load_mode": 0, "critical_load_kw": [70.0] * 24, "control_mode": "mode_a",
              "notes": "Two-shift spinning; manual genset start; switchback calendar running.",
              "data_source": {"kind": "oem_cloud", "grid_signal_confirmed": True}},
     "extra": {"load_peak_kw": 330, "twin": {"windows": [["14:00", "16:00"]], "adherence": 0.8, "trips_per_day": 0.8, "trip_minutes": 70,
                                             "unconfirmed_alert": True}},
     "connection": {"meter_ref": "FS-24-11-0042", "tariff_category": "B-2(b)", "sanctioned_load_kw": 500, "regime": "none"},
     "assets": {"pv": {"kwp_dc": 300, "kw_ac": 250}, "inverter": {"brand": "deye", "model": "SUN-50K-SG01HP3 x5", "kw_ac": 250},
                "battery": {"nameplate_kwh": 215, "p_ch_kw": 100, "p_dis_kw": 100},
                "genset": {"rated_kw": 200, "f_min": 0.3, "fuel_kind": "hsd", "min_up_h": 1.0, "tank_l": 1500}},
     "tier": "advisory_writes", "native": {"source": "photo", "work_mode": "self_use", "backup_soc_pct": 20},
     "schedule": [["14:00", "16:00", "weekdays"]], "switchback": True, "marks": "next_day"},
    {"key": "mall",
     "site": {"name": "Lahore mall (demo)", "city": "Lahore", "address": "Gulberg III", "lat": 31.51, "lon": 74.35, "disco": "LESCO", "site_type": "retail",
              "outage_unit": {"kind": "feeder", "code": "113307", "name": "Gulberg Commercial"}, "genset_start_control": "auto_on_grid_loss",
              "outage_load_mode": 1, "critical_load_kw": [180.0] * 24, "control_mode": "advisory",
              "notes": "Two 400 kVA gensets behind an auto-start panel; legacy net metering agreement from 2019.",
              "data_source": {"kind": "oem_cloud", "grid_signal_confirmed": True}},
     "extra": {"load_peak_kw": 420, "twin": {"windows": [], "trips_per_day": 0.45, "trip_minutes": 55}},
     "connection": {"meter_ref": "LH-19-07-7781", "tariff_category": "A-2(c)", "sanctioned_load_kw": 650, "regime": "net_metering_legacy",
                    "export_cap_kw": 180, "agreement_start": "2019-07-01", "agreement_end": None},
     "assets": {"pv": {"kwp_dc": 200, "kw_ac": 180}, "inverter": {"brand": "huawei", "model": "SUN2000-100KTL x2", "kw_ac": 200},
                "battery": {"nameplate_kwh": 100, "p_ch_kw": 50, "p_dis_kw": 50},
                "genset": {"rated_kw": 640, "f_min": 0.25, "fuel_kind": "hsd", "min_up_h": 0.5, "tank_l": 4000}},
     "tier": "monitor", "native": {"source": "photo", "work_mode": "self_use", "backup_soc_pct": 30}, "schedule": [], "bill": True},
    {"key": "feedmill",
     "site": {"name": "Karachi feed mill (demo)", "city": "Karachi", "address": "SITE area", "lat": 24.9, "lon": 67.0, "disco": "K-Electric",
              "site_type": "industrial_3_shift", "outage_unit": {"kind": "feeder", "code": "", "name": "SITE-11"}, "genset_start_control": "manual",
              "outage_load_mode": 1, "critical_load_kw": [250.0] * 24, "control_mode": "mode_b",
              "notes": "1 MW gas engine on captive gas; zero export.", "data_source": {"kind": "edge", "grid_signal_confirmed": True}},
     "extra": {"load_peak_kw": 900, "twin": {"windows": [], "trips_per_day": 0.5, "trip_minutes": 80}},
     "connection": {"meter_ref": "KE-B3-55120", "disco_group": "ke", "tariff_category": "B-3", "sanctioned_load_kw": 1500, "regime": "none",
                    "tax": {"province": "sindh"}},
     "assets": {"pv": {"kwp_dc": 500, "kw_ac": 450}, "inverter": {"brand": "sungrow", "model": "SC250HX", "kw_ac": 500},
                "battery": {"nameplate_kwh": 500, "p_ch_kw": 250, "p_dis_kw": 250},
                "genset": {"rated_kw": 1000, "f_min": 0.4, "fuel_kind": "gas_captive", "a": 0.0085, "b": 0.0010, "c_start_rs": 4000,
                           "c_runhr_rs": 1500, "c_om_rs_per_kwh": 6.0, "min_up_h": 2.0, "min_down_h": 1.0, "tank_l": 0}},
     "tier": "closed_loop", "native": {"source": "read_back", "work_mode": "self_use", "backup_soc_pct": 25}, "schedule": [],
     "edge": {"edge_version": "twin 1.0 (demo)", "tier": "closed_loop"}},
    {"key": "farm",
     "site": {"name": "Rahim Yar Khan farm (demo)", "city": "Rahim Yar Khan", "address": "Chak 92", "lat": 28.42, "lon": 70.3, "disco": None,
              "site_type": "farm", "genset_start_control": "manual", "outage_load_mode": 1, "critical_load_kw": [8.0] * 24, "control_mode": "advisory",
              "off_grid": True, "notes": "Off-grid tube-well and cold store; tank-level metering.",
              "data_source": {"kind": "oem_cloud", "grid_signal_confirmed": True}},
     "extra": {"load_peak_kw": 42, "twin": {}},
     "connection": None,
     "assets": {"pv": {"kwp_dc": 80, "kw_ac": 70}, "inverter": {"brand": "voltronic", "model": "Axpert MKS x6", "kw_ac": 66},
                "battery": {"nameplate_kwh": 120, "p_ch_kw": 40, "p_dis_kw": 40, "soc_min": 0.15},
                "genset": {"rated_kw": 48, "f_min": 0.3, "fuel_kind": "hsd", "min_up_h": 1.0, "tank_l": 400}},
     "tier": "advisory_writes", "native": {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 25}, "schedule": [], "marks": "same_day"},
]

#: The demo build's lock, one per company file (audit security-roles-15: one lock for the process made a build in one company refuse another's with
#: "already being built", which also told it of the other company's build)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(db) -> threading.Lock:
    """The demo build lock of a company file (store.db_path() of the company)."""
    key = os.path.normcase(os.path.abspath(str(db)))
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def status() -> dict:
    return store.get_setting("demo_status", {"state": "none"})


def _set_status(**kw) -> None:
    cur = status()
    cur.update(kw)
    store.set_setting("demo_status", cur)


def demo_sites() -> list[dict]:
    return [s for s in sites.listing() if s.get("demo_link") == DEMO_LINK]


def _record_source(site_id: str, spec: dict, since: int) -> None:
    """The tier the demo site's data source supports (audit role-owner-15: every demo site said 'The data source supports Closed loop' with no source
    recorded, and the closed-loop mill had no edge). The mill's edge is the twin: its capabilities message asserts the tier, as an edge's does
    (routes_edge.edge_caps), dated from when its history starts; a cloud source supports what SOURCE_TIER says."""
    edge = spec.get("edge")
    if edge:
        body = {"contract": delivery.CONTRACT, "seq": 1, "sent_at": _iso(since), "edge_version": edge["edge_version"], "clock_synced": True,
                "tier": edge["tier"], "devices": []}
        with store.connect() as con:
            con.execute("INSERT OR REPLACE INTO capabilities (site_id, seq, received_at, body) VALUES (?,?,?,?)", (site_id, 1, _iso(since), json.dumps(body)))
        sites.set_tier_capable(site_id, edge["tier"])
        sites.put_state(site_id, edge_boot=int(since))
    else:
        sites.set_tier_capable(site_id, SOURCE_TIER[spec["site"]["data_source"]["kind"]])


def create_sites(by: str, since: int | None = None) -> list[str]:
    """The four demo sites, active, each with its data source and the tier it supports; `since` is when the history starts (the edge's first
    report), now by default."""
    ids = []
    today = datetime.now(PKT).date()
    since = int(since if since is not None else _clock_minute())
    for spec in DEMO_SITES:
        s = sites.create(spec["site"], by, demo_link=DEMO_LINK)
        sid = s["id"]
        data = {**s["data"], **spec["extra"]}
        with store.connect() as con:
            con.execute("UPDATE sites SET data = ?, status = 'active' WHERE id = ?", (json.dumps(data), sid))
        if spec["connection"]:
            conn = dict(spec["connection"])
            if spec["key"] == "mall":
                conn["agreement_end"] = (today + timedelta(days=45)).isoformat()
            sites.put_connection(sid, conn, by)
        for role, fields in spec["assets"].items():
            sites.put_asset(sid, role, fields, by)
        _record_source(sid, spec, since)
        sites.set_tier_allowed(sid, spec["tier"], by, "demo")
        sites.capture_native(sid, spec["native"], by)
        if spec["schedule"]:
            forecast.set_schedule(sid, spec["schedule"], "entered", by)
        ids.append(sid)
    return ids


def _released(site_id: str, ts: np.ndarray) -> np.ndarray:
    """The minutes a pause or a release to native (plans.control_release) covers: no plan is in force then, and the inverter runs its own settings,
    whatever plan the caller holds (the pre-pause plan stays current when the paused tick has not run or has failed)."""
    ts = np.asarray(ts, dtype=np.int64)
    out = np.zeros(len(ts), dtype=bool)
    if not len(ts) or not any(o.get("kind") in ("pause_control", "force_native") for o in sites.overrides(site_id)):
        return out
    quarters = ts // 900 * 900
    for q in np.unique(quarters):
        if plans.control_release(site_id, int(q)):
            out[quarters == q] = True
    return out


def _merge_policy(plan_pol: dict, native_pol: dict, native_minutes: np.ndarray) -> dict:
    """A per-minute policy that follows the native one on `native_minutes` and the plan's elsewhere."""
    n = len(native_minutes)
    defaults = {"grid_cap": np.full(n, np.inf), "export_first": np.zeros(n, dtype=bool)}
    out = {}
    for k in set(plan_pol) | set(native_pol):
        a = plan_pol.get(k, plan_pol["target"] if k == "grid_target" else defaults.get(k))
        b = native_pol.get(k, native_pol["target"] if k == "grid_target" else defaults.get(k))
        if a is None or b is None:
            continue
        out[k] = np.where(native_minutes, b, a)
    return out


def _spec(site: dict) -> dict:
    """The DEMO_SITES entry a demo site was made from (by its name), or {} for any other site."""
    return next((s for s in DEMO_SITES if s["site"]["name"] == site.get("name")), {})


def _keyed_plan(site: dict, day: date) -> dict | None:
    """The table an advisory (tier 2) demo site's inverter runs on a day (plan Part 7.6: a person keys a daily table in through the vendor app, a few
    writes a day at most; audit engine-delivery-18: the twin ran every 3-hourly, and live every 15-minute, plan the moment it was made, and each such
    day counted as confirmed). The table marked applied for the day (the latest mark: a person's own wins); else, where the demo's installer keys
    tables (the spec's `marks`), the nightly table, keyed before its mark comes: the table issued first for the day (_issued_first), else the plan
    in force at the day's start; else none: the inverter runs its native settings."""
    with store.connect() as con:
        r = con.execute("SELECT plan_id FROM applied WHERE site_id = ? AND day_local = ? ORDER BY created DESC, rowid DESC LIMIT 1",
                        (site["id"], day.isoformat())).fetchone()
    if r is not None:
        return plans.get(site["id"], r["plan_id"])
    if not _spec(site).get("marks"):
        return None
    return _issued_first(site["id"], day) or plans.active_at(site["id"], twin.day_start_utc(day))


def _issued_first(site_id: str, day: date) -> dict | None:
    """The plan of the table issued first for a day (round 7, control-edge: the nightly job's, plans.issue_nightly, else the day's first,
    plans.issue_tables; audit pages-B-02), the one table a day the demo's installer keys; None when none was issued or its plan has no table for
    the day."""
    first = next(iter(plans.issued_tables(site_id, day.isoformat())), None)
    plan = plans.get(site_id, first["plan_id"]) if first else None
    return plan if plan is not None and day.isoformat() in {d.get("date") for d in (plan.get("slots") or {}).get("days") or []} else None


def _mark_tables(site: dict, t_from: int, t_to: int) -> None:
    """The demo installer's 'Mark as applied' for the days the span from t_from to t_to reaches (plan Part 9.6's applied records): at an advisory demo
    site whose installer keys tables, each day's keyed table (the nightly one), one mark a day at most, within every brand's write budget; the farm's
    on its own day, the Faisalabad mill's on the next (so the mill's own day is never marked yet, as its alert says). A day with no plan in force at
    its start (an observation day), whose plan has no table for it, or on a native block of the switchback calendar gets no mark. The mark is dated
    when it was due (the day's start, or the next day's), or the span's start if that is later.
    The keyed table is the one issued for the day (round 7, control-edge: the table that reaches the inverter is the issued one, audit pages-B-02):
    a day the span reaches with nothing issued for it (the history, built before the scheduler runs) has the plan in force at its start issued as
    its first table, at its start, so the chip, the applied log and the write meter read one table a day, the one keyed."""
    late = {"same_day": 0, "next_day": 1}.get(_spec(site).get("marks"))
    if late is None or site.get("tier") != "advisory_writes":
        return
    sid = site["id"]
    first, last = datetime.fromtimestamp(int(t_from), PKT).date(), datetime.fromtimestamp(int(t_to), PKT).date()
    for k in range((last - first).days + 1 + late):
        day = first + timedelta(days=k - late)
        if plans.issued_for(sid, day.isoformat()) is None and verify.arm_on(sid, day) != "native":
            plan = plans.active_at(sid, twin.day_start_utc(day))
            if plan is not None:
                plans.issue_first(sid, plan, day.isoformat(), twin.day_start_utc(day))
    for k in range((last - first).days + 1):
        day = first + timedelta(days=k - late)
        due = twin.day_start_utc(day + timedelta(days=late))
        with store.connect() as con:
            if due > int(t_to) or con.execute("SELECT 1 FROM applied WHERE site_id = ? AND day_local = ? LIMIT 1", (sid, day.isoformat())).fetchone():
                continue
        if verify.arm_on(sid, day) == "native":
            continue
        plan = _issued_first(sid, day)
        if plan is None:
            continue
        row = plans.mark_applied(sid, plan["id"], day.isoformat(), INSTALLER, "vendor_app")
        with store.connect() as con:
            con.execute("UPDATE applied SET created = ? WHERE id = ?", (_iso(min(max(due, int(t_from)), int(t_to))), row["id"]))


def _policy_for(site: dict, day: date, ts: np.ndarray, plan: dict | None) -> tuple[dict, np.ndarray]:
    """The per-minute policy a demo site's inverter runs on `day`, and its modes. Its native settings at a monitor site (a recommendation changes
    nothing on site), on a native block of the switchback calendar, and on minutes a pause or a release covers; else the slots of the table in force:
    at an advisory site the table keyed in for the day (_keyed_plan), run as compiled, its genset flags included, which is what the table's
    confirmation replays (verify.confirm_applied); else the plan the caller holds, a manual genset following its runs step by step."""
    arm = verify.arm_on(site["id"], day)
    keyed = site.get("tier") == "advisory_writes"
    table = _keyed_plan(site, day) if keyed and arm != "native" else plan
    if table is None or arm == "native" or site.get("tier") == "monitor":  # monitor recommends; the inverter keeps its own settings
        pol = twin.native_policy(site, ts)
    else:
        slots = [s for d in (table.get("slots") or {}).get("days", []) for s in d["slots"]]
        pol = twin.slots_policy(slots, ts)
        steps = table.get("steps") or []
        gmap = {s["t"]: s["genset_on"] for s in steps}
        if site["data"].get("genset_start_control") == "manual" and not keyed:
            # 1 in a planned run, 0 outside one: "no planned run", under which the twin's operator still rescues a site about to go dark
            pol["genset"] = np.array([1 if gmap.get(int(t) // 900 * 900) else 0 for t in ts]) if gmap else pol["genset"]
        released = _released(site["id"], ts)
        if released.all():
            pol = twin.native_policy(site, ts)
        elif released.any():
            pol = _merge_policy(pol, twin.native_policy(site, ts), released)
    return pol, pol["mode"]


def _trailing(site_id: str, t_from: int) -> tuple[int, int]:
    """The genset's run length and the outage's length (minutes) up to t_from, from the last six hours of telemetry."""
    d = telemetry.raw(site_id, t_from - 6 * 3600, t_from, ("genset_running", "grid_present"))
    ts = d["ts"]

    def count(col: str, value: float) -> int:
        n = 0
        for k in range(len(ts) - 1, -1, -1):
            if int(ts[k]) != t_from - 60 * (n + 1) or d[col][k] != value:
                break
            n += 1
        return n

    return count("genset_running", 1.0), count("grid_present", 0.0)


def _simulate_span(site: dict, t_from: int, t_to: int, plan: dict | None) -> int:
    """Run the twin from t_from to t_to (minute marks) following a plan, write the minutes and aggregate the days touched.

    The genset's run so far, the outage's minutes so far and whether the run is a rescue carry over from the previous span (the live demo runs a span
    every 15 minutes), so a minimum run or an outage does not restart at every tick. The span ends at the current minute at the latest: a twin reading
    is never stamped after now."""
    t_to = min(int(t_to), _clock_minute())
    if t_to <= t_from:
        return 0
    last = telemetry.latest(site["id"])
    soc0 = (last["battery_soc_est_pct"] / 100.0) if last and last.get("battery_soc_est_pct") is not None else 0.5
    gen0 = bool(last and last.get("genset_running"))
    key = f"demo_twin:{site['id']}"
    carried = store.get_setting(key) or {}
    if carried.get("t") == t_from:
        elapsed0, outage0, rescue0 = int(carried.get("elapsed", 0)), int(carried.get("outage", 0)), bool(carried.get("rescue", False))
    else:
        elapsed0, outage0 = _trailing(site["id"], t_from)
        rescue0 = False
    if not gen0:
        elapsed0, rescue0 = 0, False
    plant = twin.plant_of(site)
    written = 0
    t = t_from
    conn = (site.get("connections") or [None])[0]
    while t < t_to:
        day = datetime.fromtimestamp(t, PKT).date()
        d0 = twin.day_start_utc(day)
        end = min(t_to, d0 + 86400)
        tr = twin.truth(site, day)
        a, b = (t - d0) // 60, (end - d0) // 60
        part = {k: (v[a:b] if isinstance(v, np.ndarray) else v) for k, v in tr.items()}
        pol, modes = _policy_for(site, day, part["ts"], plan)
        sim = twin.simulate(part, pol, plant, soc0, gen0, gen_elapsed0=elapsed0, outage_min0=outage0, gen_rescue0=rescue0)
        written += telemetry.write_rows(twin.telemetry_rows(site["id"], part, sim, rng_seed=int(t // 60), inverter_mode=modes))
        soc0 = float(sim["soc"][-1]) if plant.e_nom else 0.0
        gen0 = bool(sim["gen_on_end"])
        elapsed0, outage0, rescue0 = int(sim["gen_elapsed_end"]), int(sim["outage_min_end"]), bool(sim["gen_rescue_end"])
        telemetry.aggregate_day(site["id"], day, plans.peak_mask(conn, twin.minutes_of(day)) if conn else None)
        t = end
    if t_to > t_from:
        store.set_setting(key, {"t": int(t_to), "elapsed": elapsed0, "outage": outage0, "rescue": rescue0})
        if ((site.get("data") or {}).get("data_source") or {}).get("kind") == "edge":
            _edge_report(site, t_from, t_to, plan)
    return written


def _edge_report(site: dict, t_from: int, t_to: int, plan: dict | None) -> None:
    """The twin as the closed-loop demo site's edge (plan Part 2.5: the edge is Product 2, or the twin; audit role-owner-15: the mill showed 'No edge
    device' and 'Never connected' beside native settings 'Read back by the edge device'). After each span it runs: a heartbeat saying whether it runs
    the plan, and an 'applied' acknowledgement (the read-back) the first time it runs a plan. Stamped with the span's end, never after now."""
    sid = site["id"]
    last = int(t_to) - 60
    following = (plan is not None and site.get("tier") == "closed_loop" and verify.arm_on(sid, datetime.fromtimestamp(last, PKT).date()) != "native"
                 and not _released(sid, np.array([last])).any())
    boot = int(sites.state(sid).get("edge_boot") or t_from)
    beat = {"contract": delivery.CONTRACT, "seq": int(t_to) // 60, "sent_at": _iso(t_to), "uptime_s": max(0, int(t_to) - boot),
            "active_plan": plan["id"] if following else None, "valid_until": plan.get("valid_until") if following else None,
            "control_state": "following_plan" if following else "native", "queue_depth": 0, "clock_offset_s": 0.0}
    sites.put_state(sid, edge_last_seen=int(t_to), edge_heartbeat=beat, edge_heartbeat_at=int(t_to), edge_boot=boot)
    if not following:
        return
    with store.connect() as con:
        if con.execute("SELECT 1 FROM acks WHERE site_id = ? AND plan_id = ? LIMIT 1", (sid, plan["id"])).fetchone() is None:
            body = {"contract": delivery.CONTRACT, "seq": int(t_from) // 60, "sent_at": _iso(t_from), "plan_id": plan["id"], "status": "applied", "items": []}
            con.execute("INSERT INTO acks (id, site_id, plan_id, status, body, received_at) VALUES (?,?,?,?,?,?)",
                        (store.new_id(), sid, plan["id"], "applied", json.dumps(body), _iso(t_from)))


def _replan_seconds() -> int:
    try:
        h = float(os.environ.get("GRIDLYTICS_DEMO_REPLAN_HOURS", REPLAN_HOURS))
    except ValueError:
        h = REPLAN_HOURS
    return int(min(24.0, max(0.25, h)) * 3600) // 900 * 900


def _top_up(site_id: str, t: int) -> None:
    """The demo's operator keeps two days' diesel in the tank: a tank under half full, or under what the plan in force means to burn over its
    horizon and a tenth more, is refilled to the top, a fill logged at the quarter `t` falls in (and after the level the last tick carried). The plan rations the
    fuel the tank holds, carried between ticks from the fills logged less the fuel burnt (plans.fuel_level; round 7, engine-optimiser-05), and
    the twin logs no fills, so without this the farm's 400 L would run dry within a week of history and its plans would ration it."""
    import json

    site = sites.full(site_id)
    lvl = plans.fuel_level(site, t)
    if not lvl or lvl["basis"] != "carried":
        return
    plan = plans.active_at(site_id, t)
    planned = 1.1 * sum(float(x.get("litres") or 0.0) for x in (plan or {}).get("steps") or [])
    if lvl["litres"] >= max(lvl["tank_l"] / 2, min(planned, lvl["tank_l"])):
        return
    at = max(forecast.align(t), int((site.get("state") or {}).get("fuel_ts") or 0) + 1)
    detail = {"day": datetime.fromtimestamp(at, PKT).date().isoformat(), "litres": round(lvl["tank_l"] - lvl["litres"], 1),
              "note": "Demo: tank refilled"}
    with store.connect() as con:
        con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)",
                    (site_id, plans._iso(at), "fuel_log", "demo", json.dumps(detail)))


def _replanned_span(site_id: str, t_from: int, t_to: int, every: int) -> None:
    """Runs the twin from t_from to t_to under plans made every `every` seconds, each from the state the twin left (the history's stand-in for
    the live 15-minute tick)."""
    t = t_from
    while t < t_to:
        _top_up(site_id, t)
        r = plans.tick(site_id, at=t, steps=HISTORY_STEPS, time_limit=5.0)
        plan = plans.get(site_id, r["plan_id"]) if r.get("plan_id") else None
        _mark_tables(sites.full(site_id), t, t)        # an advisory site's installer marks the table keyed at the day's start, once
        end = min(t_to, t + every)
        _simulate_span(sites.full(site_id), t, end, plan)
        t = end


def build_history(site_id: str, days: int, progress=None) -> None:
    site = sites.full(site_id)
    today = datetime.fromtimestamp(_clock_minute(), PKT).date()
    start = today - timedelta(days=days)
    spec = next((s for s in DEMO_SITES if s["site"]["name"] == site["name"]), {})
    if spec.get("switchback"):
        # painted on the history's first day, before its blocks begin, so its weeks count as a real calendar's do (round 7, audit security-roles-06)
        verify.plan_switchback(site_id, start + timedelta(days=OBSERVE_DAYS + 4), 4, 7, "demo", seed=7, painted_on=start)
    every = _replan_seconds()
    for i in range(days):
        day = start + timedelta(days=i)
        t0 = twin.day_start_utc(day)
        if i < OBSERVE_DAYS:
            _simulate_span(sites.full(site_id), t0, t0 + 86400, None)
        else:
            _replanned_span(site_id, t0, t0 + 86400, every)
        if progress:
            progress(i + 1, days)
    # today: re-planned up to now, then a full 48-hour plan from now
    now = _clock_minute()
    _replanned_span(site_id, twin.day_start_utc(today), now, every)
    _top_up(site_id, now)
    plans.tick(site_id, at=now)
    for i in range(OBSERVE_DAYS, days):
        day = start + timedelta(days=i)
        verify.compute_day(site_id, day)
        forecast.score_day(sites.full(site_id), day)
    alerts.evaluate(site_id)


def default_days() -> int:
    """The days of history a load builds when none is asked for: GRIDLYTICS_DEMO_DAYS, else 21 (DESIGN_LOG, v1 build, Demo: the plan says 90,
    which took too long to seed). GET /api/demo names it on every read, so a setting that is not a whole number above 0 leaves the default."""
    try:
        n = int(os.environ.get("GRIDLYTICS_DEMO_DAYS", "21"))
    except ValueError:
        return 21
    return n if n > 0 else 21


def load(by: str, days: int | None = None, background: bool = True) -> dict:
    """Create the demo sites in the current company and build their history (in a background thread unless asked otherwise)."""
    if demo_sites():
        return {"state": "ready", "message": "The demo sites are already here"}
    db = store.db_path()
    lock = _lock_for(db)
    if not lock.acquire(blocking=False):
        return {"state": "running", "message": "The demo is already being built"}
    days = int(days) if days and int(days) > 0 else default_days()            # a count below 1 (the seed's --demo-days) builds the default
    _set_status(state="running", progress=0, total=days * len(DEMO_SITES), days=days, started=store._now(), message="Creating the demo sites")

    def run():
        try:
            with store.use_company(db):
                start = datetime.fromtimestamp(_clock_minute(), PKT).date() - timedelta(days=days)   # build_history's first day
                ids = create_sites(by, since=twin.day_start_utc(start))
                done = [0]
                for sid in ids:
                    def prog(i, n, sid=sid):
                        done[0] += 1
                        if done[0] % 3 == 0:
                            _set_status(progress=done[0], message=f"Building history ({done[0]} of {days * len(ids)} site-days)")
                    build_history(sid, days, prog)
                if (verify_bill := next((s for s in DEMO_SITES if s.get("bill")), None)):
                    _sample_bill(next(s for s in sites.listing() if s["name"] == verify_bill["site"]["name"])["id"], by)
                _set_status(state="ready", progress=days * len(ids), message="The demo sites are live", finished=store._now())
        except Exception as exc:  # the status says what failed; the log keeps the trace
            with store.use_company(db):
                _set_status(state="failed", message=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-2000:])
        finally:
            lock.release()

    if background:
        threading.Thread(target=run, name="demo-build", daemon=True).start()
    else:
        run()
    return status()


def _sample_bill(site_id: str, by: str) -> None:
    """An entered bill for the last complete 10 days, the rebuilt figure within 2 percent (the uploaded sample of Part 9.6)."""
    today = datetime.now(PKT).date()
    p1 = today - timedelta(days=2)
    p0 = p1 - timedelta(days=9)
    conn = sites.connections(site_id)[0]
    rebuilt = verify.rebuild_bill(site_id, conn, p0, p1)
    verify.add_bill(site_id, {"period_start": p0.isoformat(), "period_end": p1.isoformat(), "total_rs": round(rebuilt["total_rs"] * 1.012, 0),
                              "reference": "Sample bill (demo)"}, by)


def remove(by: str) -> int:
    ids = [s["id"] for s in demo_sites()]
    for sid in ids:
        sites.delete(sid, by)
        telemetry.delete_site(sid)
        with store.connect() as con:
            for table in ("plans", "savings_days", "alerts", "notifications", "switchback", "runs", "bills", "applied", "forecast_scores", "telemetry_days",
                          "acks", "capabilities"):
                con.execute(f"DELETE FROM {table} WHERE site_id = ?", (sid,))
            con.execute("DELETE FROM settings WHERE key = ?", (f"demo_twin:{sid}",))
    store.set_setting("demo_status", {"state": "none"})
    return len(ids)


def _drop_ahead(site_id: str, clock: int) -> int:
    """Remove a demo site's twin minutes stamped at or after `clock` (the current minute), and the day totals of the local days after the clock's own:
    they were written under a clock that has since moved back. The clock's day is re-aggregated from the minutes left. Returns the minutes removed."""
    with telemetry.connect() as con:
        n = con.execute("DELETE FROM minute WHERE site_id = ? AND ts >= ? AND source_id = 'twin'", (site_id, int(clock))).rowcount
    if n > 0:
        today = datetime.fromtimestamp(clock, PKT).date()
        with store.connect() as con:
            con.execute("DELETE FROM telemetry_days WHERE site_id = ? AND day_local > ?", (site_id, today.isoformat()))
        conn = (sites.connections(site_id) or [None])[0]
        telemetry.aggregate_day(site_id, today, plans.peak_mask(conn, twin.minutes_of(today)) if conn else None)
        with store.connect() as con:  # the carried genset and outage state belongs to the removed span: rebuilt from the minutes left
            con.execute("DELETE FROM settings WHERE key = ?", (f"demo_twin:{site_id}",))
    return max(0, int(n or 0))


def advance(site_id: str, now: int | None = None) -> int:
    """Keep a demo site live: run the twin from its last minute to now under the active plan. Minutes a pause or a release to native covers run
    the inverter's native settings (twin.native_policy), even while the pre-pause plan is still the current one (a paused tick that has not run yet
    or has failed). The twin never runs past the clock's current minute, whatever `now` says, and readings it stamped after that minute (written
    while the clock was ahead) are dropped first, so the site's last reading is never in the future."""
    site = sites.full(site_id)
    if site is None or site.get("demo_link") != DEMO_LINK:
        return 0
    clock = _clock_minute()
    now = min(int(now or clock) // 60 * 60, clock)
    _drop_ahead(site_id, clock)
    last = telemetry.latest(site_id)
    start = (last["ts"] + 60) if last else now - 3600
    start = max(start, now - 3 * 86400)
    if start >= now:
        return 0
    _mark_tables(site, start, now)
    return _simulate_span(site, start, now, plans.current(site_id))  # _policy_for runs the released minutes on the native settings
