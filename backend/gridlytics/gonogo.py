"""The pilot's measures (round 7, audit brief-vs-build-13).

`measures` is plan Part 8.3's KPI table over a period, the parts the Savings page's money figures leave out: reserve adequacy (reserve.adequacy, as the
Outages page reads it), the constraint violations (state of charge under the reserve at an outage's start, unserved critical load, the genset under its
minimum load, export above the cap, import during an outage), control health (the plan days by fidelity, writes per register per day, read-back failures,
fallback events and writes refused as off the whitelist), the genset's run hours and starts and the battery's equivalent full cycles on each arm, and
the BMS trust score (battery.state's)."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import battery, reports, reserve, sites, store, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))

#: a minute counts against a limit only past this margin, kW (a meter's rounding, a minute's average of a changing flow)
LIMIT_MARGIN_KW = 0.5
#: the genset's minimum-load margin as a share of its rating, where that is more than LIMIT_MARGIN_KW
GEN_MARGIN_SHARE = 0.02
#: the constraint violations read the stored minutes: over at most this many days (a longer period gives the rest of the measures)
VIOLATION_DAYS_MAX = 120
#: the plan days of a fidelity class (the days a plan was issued to run; a native block or a recommendation is not one)
PLAN_DAY_CLASSES = ("confirmed", "partial", "not_applied", "pending")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _rows(site_id: str, d0: date, d1: date) -> dict[str, dict[str, dict]]:
    """The stored savings days of the period, by day and baseline."""
    with store.connect() as con:
        rows = con.execute("SELECT day_local, baseline, data FROM savings_days WHERE site_id = ? AND day_local >= ? AND day_local <= ?",
                           (site_id, d0.isoformat(), d1.isoformat())).fetchall()
    out: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        out[r["day_local"]][r["baseline"]] = json.loads(r["data"])
    return dict(out)


def _violations(site: dict, d0: date, d1: date, now: int) -> dict | None:
    """Plan 8.3's constraint violations read from the stored minutes, in minutes: import during an outage and export above the cap (a grid site), the
    genset running under its minimum load (a genset site). None past VIOLATION_DAYS_MAX days. A limit the site does not have is None."""
    if (d1 - d0).days + 1 > VIOLATION_DAYS_MAX:
        return None
    t0, t1 = twin.day_start_utc(d0), min(twin.day_start_utc(d1) + 86400, now)
    cols = ("grid_present", "grid_import_wh", "grid_export_wh", "grid_power_w", "genset_power_w", "genset_running")
    d = telemetry.raw(site["id"], t0, t1, cols) if t1 > t0 else {"ts": np.array([], dtype=np.int64), **{c: np.array([]) for c in cols}}
    ts = d["ts"]
    step = float(np.median(np.diff(ts)) / 60) if len(ts) > 1 else 1.0
    step = max(1.0, min(step, 60.0))
    minutes = lambda mask: int(round(float(np.sum(mask)) * step))  # noqa: E731
    conns = site.get("connections") or []
    regime = (conns[0].get("data") or {}).get("regime") if conns else "off_grid"
    on_grid = bool(conns) and regime != "off_grid"
    plant = twin.plant_of(site)
    gp = d["grid_power_w"] / 1000
    imp = np.where(np.isnan(d["grid_import_wh"]), np.maximum(gp, 0.0), d["grid_import_wh"] * 60 / 1000)
    exp = np.where(np.isnan(d["grid_export_wh"]), np.maximum(-gp, 0.0), d["grid_export_wh"] * 60 / 1000)
    out = {"minutes_read": minutes(np.ones(len(ts), dtype=bool)), "import_during_outage_min": None, "export_above_cap_min": None, "export_cap_kw": None,
           "genset_below_min_load_min": None, "genset_min_load_kw": None}
    if on_grid:
        down = d["grid_present"] < 0.5
        out["import_during_outage_min"] = minutes(down & (np.nan_to_num(imp) > LIMIT_MARGIN_KW))
        cap = float(plant.x_max)
        out["export_cap_kw"] = round(cap, 1)
        out["export_above_cap_min"] = minutes(np.nan_to_num(exp) > cap + LIMIT_MARGIN_KW)
    gen = (site.get("assets") or {}).get("genset")
    if gen:
        p = d["genset_power_w"] / 1000
        running = (np.nan_to_num(d["genset_running"]) > 0.5) | (np.nan_to_num(p) > 0.0)
        floor = float(plant.gen_fmin) * float(plant.gen_kw)
        margin = max(LIMIT_MARGIN_KW, GEN_MARGIN_SHARE * float(plant.gen_kw))
        out["genset_min_load_kw"] = round(floor, 1)
        out["genset_below_min_load_min"] = minutes(running & ~np.isnan(p) & (p < floor - margin))
    return out


def _acks(site_id: str, t0: int, t1: int) -> dict:
    """The edge's acknowledgements over [t0, t1): the writes each register took per Karachi day (an item written or clamped, a device's applied
    primitive), the read-backs that did not match what was written, the writes that failed, and those the edge refused as off its whitelist."""
    with store.connect() as con:
        rows = con.execute("SELECT body, received_at FROM acks WHERE site_id = ? AND received_at >= ? AND received_at < ?",
                           (site_id, _iso(t0), _iso(t1))).fetchall()
    writes: dict[tuple[str, str], int] = defaultdict(int)
    readback = failed = off = 0
    for r in rows:
        try:
            body = json.loads(r["body"])
            day = verify.local_day(r["received_at"]).isoformat()
        except (ValueError, TypeError):
            continue
        # a plan the edge refused whole as off its whitelist names the code on the ack itself
        off += int(body.get("reason") == "OFF_WHITELIST")
        for it in body.get("items") or []:
            if not isinstance(it, dict):
                continue
            if it.get("status") in ("ok", "clamped"):
                writes[(day, str(it.get("primitive")))] += 1
            failed += int(it.get("status") == "failed")
            readback += int(it.get("read_back") is not None and it.get("written") is not None and it["read_back"] != it["written"])
            off += int("OFF_WHITELIST" in (it.get("codes") or []))
        for dev in body.get("per_device") or []:
            if not isinstance(dev, dict):
                continue
            for a in dev.get("applied") or []:
                writes[(day, str(a.get("primitive")))] += 1
                readback += int(a.get("readback_ok") is False)
            off += sum(1 for x in dev.get("rejected") or [] if isinstance(x, dict) and x.get("reason") == "OFF_WHITELIST")
    per_register: dict[str, list[int]] = defaultdict(list)
    for (_, reg), n in writes.items():
        per_register[reg].append(n)
    return {"acks": len(rows), "writes": sum(writes.values()), "days_with_writes": len({d for d, _ in writes}),
            "writes_per_register_per_day": {k: round(sum(v) / len(v), 1) for k, v in sorted(per_register.items())},
            "writes_per_register_per_day_max": max(writes.values()) if writes else None,
            "readback_failures": readback, "write_failures": failed, "off_whitelist": off}


def _arms(rows: dict[str, dict[str, dict]], e_nom: float) -> dict:
    """The genset's run hours and starts, and the battery's equivalent full cycles, on the measured day and each baseline's replay, over the days the
    plan claims (its control in force, none withheld): plan 15.3 reads the genset and cycling gates on those days, against the tuned rule."""
    days = [by for _, by in sorted(rows.items()) if verify._day_claims(by.get("native") or {}) and (by.get("tuned") or {}).get("baseline")]
    out = {"days": len(days), "genset_hours": None, "genset_starts": None, "efc_per_day": None}
    if not days:
        return out

    def total(arm: str, key: str) -> float | None:
        vals = []
        for by in days:
            row = by["native"]["actual"] if arm == "actual" else by[arm]["baseline"]
            if row.get(key) is None:
                return None  # a day scored before the field was stored: no total
            vals.append(float(row[key]))
        return sum(vals)

    out["genset_hours"] = {a: (None if (v := total(a, "genset_hours")) is None else round(v, 1)) for a in ("actual", "native", "tuned")}
    out["genset_starts"] = {a: total(a, "genset_starts") for a in ("actual", "native", "tuned")}
    if e_nom > 0:
        out["efc_per_day"] = {a: (None if (v := total(a, "discharge_kwh")) is None else round(v / e_nom / len(days), 3)) for a in ("actual", "native", "tuned")}
    return out


def measures(site_id: str, d0: date, d1: date, summ: dict | None = None, now: int | None = None) -> dict:
    """Plan 8.3's KPI table over the local days d0..d1, the parts the money figures leave out (brief-vs-build-13): reserve adequacy, the constraint
    violations, control health, and the genset's and the battery's use on each arm. A measure the site has no equipment for is None."""
    site = sites.full(site_id) or {}
    now = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    t0, t1 = twin.day_start_utc(d0), min(twin.day_start_utc(d1) + 86400, now)
    summ = summ if summ is not None else verify.summary(site_id, d0.isoformat(), d1.isoformat())
    days = summ.get("days") or []
    rows = _rows(site_id, d0, d1)
    assets = site.get("assets") or {}
    res = reserve.adequacy(site, t0, t1) if reserve.has_battery(site) and t1 > t0 else None
    claiming = [by["native"] for by in rows.values() if verify._day_claims(by.get("native") or {})]
    caused = sum(max(0.0, float((r.get("actual") or {}).get("unserved_kwh") or 0.0) - float((r.get("baseline") or {}).get("unserved_kwh") or 0.0))
                 for r in claiming)
    tdays = telemetry.days(site_id, d0.isoformat(), d1.isoformat())
    unserved = round(sum(float(t.get("unserved_kwh") or 0.0) for t in tdays), 2) if tdays else None
    violations = {"soc_below_reserve_at_outage_start": None if res is None else len(res["short"]), "unserved_critical_kwh": unserved,
                  "unserved_caused_by_plan_kwh": round(caused, 2) if claiming else None}
    minute = _violations(site, d0, d1, now)
    violations.update(minute or {"minutes_read": None})
    violations["minutes_checked"] = minute is not None
    control = reports.control_health(site_id, days, d0, d1)
    plan_days = sum(n for k, n in control["days"].items() if k in PLAN_DAY_CLASSES)
    control = {**control, "plan_days": plan_days, "fidelity_pct": round(100.0 * control["days"].get("confirmed", 0) / plan_days, 1) if plan_days else None,
               **_acks(site_id, t0, t1), "fallback_events": int(control["events"].get("fallback_engaged", 0)),
               "readback_mismatch_events": int(control["events"].get("readback_mismatch", 0))}
    bat = assets.get("battery")
    arms = _arms(rows, twin.plant_of(site).e_nom if bat else 0.0)
    # the brief's BMS trust (the gap between the BMS's state of charge and the independent estimate, as the Battery page scores it) over the 30 days to
    # the period's end; only the score and its basis (the state also carries the wear price, a money setting)
    st = (battery.state(site, t1) or {}) if bat and t1 > t0 else {}
    return {"from": d0.isoformat(), "to": d1.isoformat(), "tier": site.get("tier", "monitor"),
            "reserve": None if res is None else {"outages": res["outages"], "met": res["met"], "pct": res["pct"], "short": res["short"]},
            "violations": violations, "control": control,
            "genset": None if not assets.get("genset") else {"days": arms["days"], "hours": arms["genset_hours"], "starts": arms["genset_starts"]},
            "battery": None if not bat else {"days": arms["days"], "efc_per_day": arms["efc_per_day"],
                                             "bms_trust": {"trust": st.get("trust"), "basis": st.get("trust_basis"), "to": d1.isoformat()}}}
