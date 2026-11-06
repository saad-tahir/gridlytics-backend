"""Demo mode is the twin (plan Part 9.6 and 7.4): four twin sites a Pakistani energy manager would recognise, with generated history run through
the real forecasters, the real optimiser and the real savings engine, kept live by the scheduler so no page is empty on day one.

The history runs the inverter's native settings for the first three days (the observation days before the first plan), then follows each day's plan
compiled to the brand's slot table, except on the native blocks of the switchback calendar.
"""
from __future__ import annotations

import os
import threading
import traceback
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import alerts, forecast, plans, sites, store, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))
DEMO_LINK = "demo"
OBSERVE_DAYS = 3

DEMO_SITES = [
    {"key": "textile",
     "site": {"name": "Faisalabad textile mill (demo)", "city": "Faisalabad", "address": "Jhang Road industrial area", "lat": 31.42, "lon": 73.08,
              "disco": "FESCO", "site_type": "industrial_2_shift", "outage_unit": {"kind": "feeder", "code": "411205", "name": "Jhang Road Industrial"},
              "genset_start_control": "manual", "outage_load_mode": 0, "critical_load_kw": [70.0] * 24, "control_mode": "mode_a",
              "notes": "Two-shift spinning; manual genset start; switchback calendar running."},
     "extra": {"load_peak_kw": 330, "twin": {"windows": [["14:00", "16:00"]], "adherence": 0.8, "trips_per_day": 0.8, "trip_minutes": 70,
                                             "unconfirmed_alert": True}},
     "connection": {"meter_ref": "FS-24-11-0042", "tariff_category": "B-2(b)", "sanctioned_load_kw": 500, "regime": "none"},
     "assets": {"pv": {"kwp_dc": 300, "kw_ac": 250}, "inverter": {"brand": "deye", "model": "SUN-50K-SG01HP3 x5", "kw_ac": 250},
                "battery": {"nameplate_kwh": 215, "p_ch_kw": 100, "p_dis_kw": 100},
                "genset": {"rated_kw": 200, "f_min": 0.3, "fuel_kind": "hsd", "min_up_h": 1.0, "tank_l": 1500}},
     "tier": "advisory_writes", "native": {"source": "photo", "work_mode": "self_use", "backup_soc_pct": 20},
     "schedule": [["14:00", "16:00", "weekdays"]], "switchback": True},
    {"key": "mall",
     "site": {"name": "Lahore mall (demo)", "city": "Lahore", "address": "Gulberg III", "lat": 31.51, "lon": 74.35, "disco": "LESCO", "site_type": "retail",
              "outage_unit": {"kind": "feeder", "code": "113307", "name": "Gulberg Commercial"}, "genset_start_control": "auto_on_grid_loss",
              "outage_load_mode": 1, "critical_load_kw": [180.0] * 24, "control_mode": "advisory",
              "notes": "Two 400 kVA gensets behind an auto-start panel; legacy net metering agreement from 2019."},
     "extra": {"load_peak_kw": 420, "twin": {"windows": [], "trips_per_day": 0.45, "trip_minutes": 55}},
     "connection": {"meter_ref": "LH-19-07-7781", "tariff_category": "A-2(c)", "sanctioned_load_kw": 650, "regime": "net_metering_legacy",
                    "export_cap_kw": 180, "agreement_start": "2019-07-01", "agreement_end": None},
     "assets": {"pv": {"kwp_dc": 200, "kw_ac": 180}, "inverter": {"brand": "huawei", "model": "SUN2000-100KTL x2", "kw_ac": 200},
                "battery": {"nameplate_kwh": 100, "p_ch_kw": 50, "p_dis_kw": 50},
                "genset": {"rated_kw": 640, "f_min": 0.25, "fuel_kind": "hsd", "min_up_h": 0.5, "tank_l": 4000}},
     "tier": "monitor", "native": {"source": "read_back", "work_mode": "self_use", "backup_soc_pct": 30}, "schedule": [], "bill": True},
    {"key": "feedmill",
     "site": {"name": "Karachi feed mill (demo)", "city": "Karachi", "address": "SITE area", "lat": 24.9, "lon": 67.0, "disco": "K-Electric",
              "site_type": "industrial_3_shift", "outage_unit": {"kind": "feeder", "code": "", "name": "SITE-11"}, "genset_start_control": "manual",
              "outage_load_mode": 1, "critical_load_kw": [250.0] * 24, "control_mode": "mode_b",
              "notes": "1 MW gas engine on captive gas; zero export."},
     "extra": {"load_peak_kw": 900, "twin": {"windows": [], "trips_per_day": 0.5, "trip_minutes": 80}},
     "connection": {"meter_ref": "KE-B3-55120", "disco_group": "ke", "tariff_category": "B-3", "sanctioned_load_kw": 1500, "regime": "none",
                    "tax": {"province": "sindh"}},
     "assets": {"pv": {"kwp_dc": 500, "kw_ac": 450}, "inverter": {"brand": "sungrow", "model": "SC250HX", "kw_ac": 500},
                "battery": {"nameplate_kwh": 500, "p_ch_kw": 250, "p_dis_kw": 250},
                "genset": {"rated_kw": 1000, "f_min": 0.4, "fuel_kind": "gas_captive", "a": 0.0085, "b": 0.0010, "c_start_rs": 4000,
                           "c_runhr_rs": 1500, "c_om_rs_per_kwh": 6.0, "min_up_h": 2.0, "min_down_h": 1.0, "tank_l": 0}},
     "tier": "closed_loop", "native": {"source": "read_back", "work_mode": "self_use", "backup_soc_pct": 25}, "schedule": []},
    {"key": "farm",
     "site": {"name": "Rahim Yar Khan farm (demo)", "city": "Rahim Yar Khan", "address": "Chak 92", "lat": 28.42, "lon": 70.3, "disco": None,
              "site_type": "farm", "genset_start_control": "manual", "outage_load_mode": 1, "critical_load_kw": [8.0] * 24, "control_mode": "advisory",
              "notes": "Off-grid tube-well and cold store; tank-level metering."},
     "extra": {"load_peak_kw": 42, "twin": {}},
     "connection": None,
     "assets": {"pv": {"kwp_dc": 80, "kw_ac": 70}, "inverter": {"brand": "voltronic", "model": "Axpert MKS x6", "kw_ac": 66},
                "battery": {"nameplate_kwh": 120, "p_ch_kw": 40, "p_dis_kw": 40, "soc_min": 0.15},
                "genset": {"rated_kw": 48, "f_min": 0.3, "fuel_kind": "hsd", "min_up_h": 1.0, "tank_l": 400}},
     "tier": "advisory_writes", "native": {"source": "entered", "work_mode": "self_use", "backup_soc_pct": 25}, "schedule": []},
]

_lock = threading.Lock()


def status() -> dict:
    return store.get_setting("demo_status", {"state": "none"})


def _set_status(**kw) -> None:
    cur = status()
    cur.update(kw)
    store.set_setting("demo_status", cur)


def demo_sites() -> list[dict]:
    return [s for s in sites.listing() if s.get("demo_link") == DEMO_LINK]


def create_sites(by: str) -> list[str]:
    ids = []
    today = datetime.now(PKT).date()
    for spec in DEMO_SITES:
        s = sites.create(spec["site"], by, demo_link=DEMO_LINK)
        sid = s["id"]
        data = {**s["data"], **spec["extra"]}
        with store.connect() as con:
            import json
            con.execute("UPDATE sites SET data = ?, status = 'active' WHERE id = ?", (json.dumps(data), sid))
        if spec["connection"]:
            conn = dict(spec["connection"])
            if spec["key"] == "mall":
                conn["agreement_end"] = (today + timedelta(days=45)).isoformat()
            sites.put_connection(sid, conn, by)
        for role, fields in spec["assets"].items():
            sites.put_asset(sid, role, fields, by)
        sites.set_tier_capable(sid, "closed_loop")
        sites.set_tier_allowed(sid, spec["tier"], by, "demo")
        sites.capture_native(sid, spec["native"], by)
        if spec["schedule"]:
            forecast.set_schedule(sid, spec["schedule"], "entered", by)
        ids.append(sid)
    return ids


def _policy_for(site: dict, day: date, ts: np.ndarray, plan: dict | None) -> tuple[dict, np.ndarray]:
    arm = verify.arm_on(site["id"], day)
    if plan is None or arm == "native" or site.get("tier") == "monitor":  # monitor recommends; the inverter keeps its own settings
        pol = twin.native_policy(site, ts)
    else:
        slots = [s for d in (plan.get("slots") or {}).get("days", []) for s in d["slots"]]
        pol = twin.slots_policy(slots, ts)
        steps = plan.get("steps") or []
        gmap = {s["t"]: s["genset_on"] for s in steps}
        if site["data"].get("genset_start_control") == "manual":
            pol["genset"] = np.array([1 if gmap.get(int(t) // 900 * 900) else 0 for t in ts]) if gmap else pol["genset"]
    return pol, pol["mode"]


def _simulate_span(site: dict, t_from: int, t_to: int, plan: dict | None) -> int:
    """Run the twin from t_from to t_to (minute marks) following a plan, write the minutes and aggregate the days touched."""
    last = telemetry.latest(site["id"])
    soc0 = (last["battery_soc_est_pct"] / 100.0) if last and last.get("battery_soc_est_pct") is not None else 0.5
    gen0 = bool(last and last.get("genset_running"))
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
        sim = twin.simulate(part, pol, plant, soc0, gen0)
        written += telemetry.write_rows(twin.telemetry_rows(site["id"], part, sim, rng_seed=int(t // 60), inverter_mode=modes))
        soc0 = float(sim["soc"][-1]) if plant.e_nom else 0.0
        gen0 = bool(sim["gen_on_end"])
        telemetry.aggregate_day(site["id"], day, plans.peak_mask(conn, twin.minutes_of(day)) if conn else None)
        t = end
    return written


def build_history(site_id: str, days: int, progress=None) -> None:
    site = sites.full(site_id)
    today = datetime.now(PKT).date()
    start = today - timedelta(days=days)
    spec = next((s for s in DEMO_SITES if s["site"]["name"] == site["name"]), {})
    if spec.get("switchback"):
        verify.plan_switchback(site_id, start + timedelta(days=OBSERVE_DAYS + 4), 4, 7, "demo", seed=7)
    for i in range(days):
        day = start + timedelta(days=i)
        t0 = twin.day_start_utc(day)
        plan = None
        if i >= OBSERVE_DAYS:
            r = plans.tick(site_id, at=t0, steps=96, time_limit=5.0)
            plan = plans.get(site_id, r["plan_id"]) if r.get("plan_id") else None
        site = sites.full(site_id)
        _simulate_span(site, t0, t0 + 86400, plan)
        if progress:
            progress(i + 1, days)
    # today: the morning plan, the minutes so far, then a full 48-hour plan from now
    now = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    t0 = twin.day_start_utc(today)
    r = plans.tick(site_id, at=t0, steps=96, time_limit=5.0)
    _simulate_span(sites.full(site_id), t0, now, plans.get(site_id, r["plan_id"]) if r.get("plan_id") else None)
    plans.tick(site_id, at=now)
    for i in range(OBSERVE_DAYS, days):
        day = start + timedelta(days=i)
        verify.compute_day(site_id, day)
        forecast.score_day(sites.full(site_id), day)
    alerts.evaluate(site_id)


def load(by: str, days: int | None = None, background: bool = True) -> dict:
    """Create the demo sites in the current company and build their history (in a background thread unless asked otherwise)."""
    if demo_sites():
        return {"state": "ready", "message": "The demo sites are already here"}
    if not _lock.acquire(blocking=False):
        return {"state": "running", "message": "The demo is already being built"}
    days = int(days or os.environ.get("GRIDLYTICS_DEMO_DAYS", "21"))
    db = store.db_path()
    _set_status(state="running", progress=0, total=days * len(DEMO_SITES), started=store._now(), message="Creating the demo sites")

    def run():
        try:
            with store.use_company(db):
                ids = create_sites(by)
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
            _lock.release()

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
            for table in ("plans", "savings_days", "alerts", "notifications", "switchback", "runs", "bills", "applied", "forecast_scores", "telemetry_days"):
                con.execute(f"DELETE FROM {table} WHERE site_id = ?", (sid,))
    store.set_setting("demo_status", {"state": "none"})
    return len(ids)


def advance(site_id: str, now: int | None = None) -> int:
    """Keep a demo site live: run the twin from its last minute to now under the active plan."""
    site = sites.full(site_id)
    if site is None or site.get("demo_link") != DEMO_LINK:
        return 0
    now = int(now or datetime.now(timezone.utc).timestamp()) // 60 * 60
    last = telemetry.latest(site_id)
    start = (last["ts"] + 60) if last else now - 3600
    start = max(start, now - 3 * 86400)
    if start >= now:
        return 0
    return _simulate_span(site, start, now, plans.current(site_id))
