"""The pilot's measures and its go/no-go (round 7, audit pages-D-12 with brief-vs-build-02 and flow-savings-pilot-bill-13, and brief-vs-build-13).

`measures` is plan Part 8.3's KPI table over a period, the parts the Savings page's money figures leave out: reserve adequacy (reserve.adequacy, as the
Outages page reads it), the constraint violations (state of charge under the reserve at an outage's start, unserved critical load, the genset under its
minimum load, export above the cap, import during an outage), control health (the plan days by fidelity, writes per register per day, read-back failures,
fallback events and writes refused as off the whitelist), the genset's run hours and starts and the battery's equivalent full cycles on each arm, and
the BMS trust score (battery.state's).

`scorecard` reads the pilot's pre-registered criteria (the Pilot page's tab D, stored with the set-up) against those measures and the ledger from the
pilot's start, with plan Part 15.3's gates the criteria did not hold: the genset's run hours down at least 15 % against the tuned rule at a genset site,
equivalent full cycles a day at most 1.1 times the tuned baseline's, the counterfactual and the switchback in agreement, no write off the whitelist;
and the decision statistic (the lower 80 % bound of the daily verified increment over the tuned baseline, times 30, against the fee) with the verdict
it gives once 8 weeks of switchback have run. Each gate is met, not met, not measured yet, or does not apply here. The monthly pilot report prints the
same gates (reports._pilot)."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import auth, battery, reports, reserve, sites, store, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))

#: a minute counts against a limit only past this margin, kW (a meter's rounding, a minute's average of a changing flow)
LIMIT_MARGIN_KW = 0.5
#: the genset's minimum-load margin as a share of its rating, where that is more than LIMIT_MARGIN_KW
GEN_MARGIN_SHARE = 0.02
#: the constraint violations read the stored minutes: over at most this many days (a longer period gives the rest of the measures)
VIOLATION_DAYS_MAX = 120
#: plan 15.3: the decision statistic against the fee: success at 3 times, stop under 1.5 times after 8 weeks, between them extend once by 4 weeks
SUCCESS_MULTIPLE, STOP_MULTIPLE = 3.0, 1.5
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


# ------------------------------------------------------------------------------------------------ the go/no-go (plan 15.3)

def _gate(gid: str, status: str, target=None, result=None, money: bool = False, fee: bool = False, **detail) -> dict:
    # the flags are named so the money filter keeps them ("money" and "fee" are money keys it takes out)
    return {"id": gid, "status": status, "target": target, "result": result, "money_gate": money, "fee_gate": fee, **detail}


def _at_least(x: float | None, target: float) -> str:
    return "not_yet" if x is None else ("met" if x >= target else "not_met")


def _at_most(x: float | None, target: float) -> str:
    return "not_yet" if x is None else ("met" if x <= target else "not_met")


def _window(site_id: str, today: date) -> tuple[date | None, bool, dict]:
    """The pilot's set-up and the first day it is read from: its start (Karachi day), else its calendar's first block, else none."""
    from .routes_savings import _pilot_data
    with store.connect() as con:
        r = con.execute("SELECT * FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
    data = _pilot_data(r)
    if r is not None and r["started"]:
        return verify.local_day(r["started"]), True, data
    blocks = verify.switchback_blocks(site_id)
    return (date.fromisoformat(blocks[0]["block_start"]) if blocks else None), False, data


def scorecard(site_id: str, today: date, start: date | None = None, end: date | None = None) -> dict:
    """The pilot's gates read against its results from its start to today (pages-D-12). Each gate: {id, status (met, not_met, not_yet,
    not_applicable), target (the pre-registered value), result (the measured one), money_gate (a money figure: none to a role without costs),
    fee_gate (read against the fee: none to a role without the fee)} with the facts its words need. `verdict` is plan 15.3's: not_yet until 8 weeks of switchback have
    run and the statistic is measured (`so_far` what it reads now), then go (at least 3 times the fee, no gate not met), review (the statistic clears
    the bar but a gate is not met), extend (between 1.5 and 3 times: once, by 4 weeks) or stop (under 1.5 times). `start` and `end` set another
    window (the monthly pilot report's); the end is never after today."""
    site = sites.full(site_id) or {}
    first, started, pilot = _window(site_id, today)
    # the monthly pilot report reads the same gates over its own window (reports._pilot): from the start it chose, to the end of its month
    start = start or first
    today = min(end or today, today)
    c = pilot["criteria"]
    assets = site.get("assets") or {}
    tier = site.get("tier", "monitor")
    out = {"site_id": site_id, "started": started, "from": start.isoformat() if start else None, "to": today.isoformat(), "criteria": c}
    if start is None or start > today:
        return {**out, "gates": [], "verdict": "not_yet", "verdict_reasons": ["not_started"], "measures": None}
    summ = verify.summary(site_id, start.isoformat(), today.isoformat(), see_fee=True)
    m = measures(site_id, start, today, summ)
    days = summ.get("days") or []
    nat = summ["baselines"].get("native", {})
    claiming = [d for d in days if d.get("claims")]
    cost = sum(float(d.get("native_cost_rs") or 0.0) for d in claiming)
    saving_pct = round(float(nat.get("claimed_rs") or 0.0) / cost * 100, 1) if claiming and cost > 0 else None
    gates = [_gate("saving", _at_least(saving_pct, c["min_saving_pct"]), c["min_saving_pct"], saving_pct, money=True, days=len(claiming))]
    caused = m["violations"]["unserved_caused_by_plan_kwh"]
    gates.append(_gate("unserved", _at_most(caused, c["max_unserved_kwh"]), c["max_unserved_kwh"], caused,
                       measured_kwh=m["violations"]["unserved_critical_kwh"]))
    res = m["reserve"]
    gates.append(_gate("reserve", "not_applicable" if res is None else _at_least(res["pct"], c["reserve_adequacy_pct"]), c["reserve_adequacy_pct"],
                       None if res is None else res["pct"], outages=None if res is None else res["outages"]))
    ctl = m["control"]
    # at the monitor tier the plan only recommends: no plan day, so fidelity does not apply there
    gates.append(_gate("fidelity", "not_applicable" if tier == "monitor" and not ctl["plan_days"] else _at_least(ctl["fidelity_pct"], c["fidelity_pct"]),
                       c["fidelity_pct"],
                       ctl["fidelity_pct"], plan_days=ctl["plan_days"]))
    fee = summ.get("fee") or {}
    fee_rs = float(fee.get("fee_rs_per_month") or 0.0)
    # no multiple without a fee, nor without a scored month to rest on (the fee check's `months`)
    multiple = round(float(fee.get("verified_saving_rs_per_month") or 0.0) / fee_rs, 2) if fee_rs > 0 and fee.get("months", True) else None
    gates.append(_gate("fee_multiple", _at_least(multiple, c["fee_multiple"]), c["fee_multiple"], multiple, money=True, fee=True, fee_set=fee_rs > 0,
                       preliminary=bool(fee.get("preliminary"))))
    fc = reports.forecast_skill(site_id, start, today)
    gates.append(_gate("forecast_pv", "not_applicable" if not assets.get("pv") else _at_most(fc["pv_nrmse"], c["forecast_pv_nrmse_max"]),
                       c["forecast_pv_nrmse_max"], None if fc["pv_nrmse"] is None else round(fc["pv_nrmse"], 3), days=fc["pv_days"]))
    # plan 15.3's gates the criteria did not hold (R8 2.13 tab D: "the genset gate hidden without a genset")
    g = m["genset"]
    if g is None:
        gates.append(_gate("genset_hours", "not_applicable", c["genset_hours_cut_pct"]))
    else:
        h = g["hours"] or {}
        tuned, actual = h.get("tuned"), h.get("actual")
        cut = round((tuned - actual) / tuned * 100, 1) if tuned and actual is not None else None
        gates.append(_gate("genset_hours", _at_least(cut, c["genset_hours_cut_pct"]), c["genset_hours_cut_pct"], cut, hours_actual=actual,
                           hours_tuned=tuned, days=g["days"]))
    b = m["battery"]
    if b is None:
        gates.append(_gate("cycles", "not_applicable", c["cycles_ratio_max"]))
    else:
        e = b["efc_per_day"] or {}
        ratio = round(e["actual"] / e["tuned"], 2) if e.get("tuned") and e.get("actual") is not None else None
        gates.append(_gate("cycles", _at_most(ratio, c["cycles_ratio_max"]), c["cycles_ratio_max"], ratio, efc_actual=e.get("actual"),
                           efc_tuned=e.get("tuned"), days=b["days"]))
    sb = verify.switchback_result(site_id)
    ag = (sb.get("result") or {}).get("agreement")
    gates.append(_gate("agreement", "not_applicable" if sb.get("result_withheld") == "monitor_tier" else
                       ("not_yet" if ag is None else ("met" if ag["agrees"] else "not_met")), result=None if ag is None else bool(ag["agrees"]),
                       difference_rs_per_day=None if ag is None else ag.get("difference_rs_per_day"),
                       tolerance_rs_per_day=None if ag is None else ag.get("tolerance_rs_per_day")))
    gates.append(_gate("whitelist", "not_applicable" if tier != "closed_loop" else ("met" if ctl["off_whitelist"] == 0 else "not_met"), 0,
                       ctl["off_whitelist"] if tier == "closed_loop" else None))
    # the decision statistic (plan 15.3) and its verdict
    bound, n_verified = reports.verified_bound(days)
    weeks = float((summ.get("final_conditions") or {}).get("switchback_weeks") or 0.0)
    stat = round(bound / fee_rs, 2) if bound is not None and fee_rs > 0 else None
    so_far = None if stat is None else ("success" if stat >= SUCCESS_MULTIPLE else "stop" if stat < STOP_MULTIPLE else "extend")
    gates.append(_gate("statistic", "not_yet" if stat is None else ("met" if stat >= SUCCESS_MULTIPLE else "not_met"), SUCCESS_MULTIPLE, stat,
                       money=True, fee=True, verified_days=n_verified, bound_rs_per_month=None if bound is None else round(bound, 0),
                       fee_set=fee_rs > 0, stop_below=STOP_MULTIPLE))
    reasons = []
    if weeks < verify.SWITCHBACK_WEEKS_FOR_FINAL:
        reasons.append("under_8_weeks")
    if stat is None:
        reasons.append("no_fee" if fee_rs <= 0 else "too_few_verified_days")
    failing = [x["id"] for x in gates if x["status"] == "not_met" and x["id"] != "statistic"]
    if reasons:
        verdict = "not_yet"
    elif so_far == "success":
        verdict = "review" if failing else "go"
    else:
        verdict = so_far
    return {**out, "gates": gates, "verdict": verdict, "verdict_reasons": reasons, "so_far": so_far, "failing": failing,
            "switchback_weeks": round(weeks, 1), "switchback_weeks_needed": verify.SWITCHBACK_WEEKS_FOR_FINAL, "preliminary": bool(summ.get("preliminary")),
            "measures": m}


def for_reader(card: dict) -> dict:
    """The scorecard as the reader may see it (the owner's rule: no money, rate, fee or money ratio to a role without them): a money gate's figures
    and verdict to a role with costs only (the others read that it is hidden for their role); a gate read against the fee, and the verdict, to a role
    that sees the fee only (plan 9.4: an operator reads costs but not the fee)."""
    costs, fee = auth.can("see_costs"), auth.can("see_fee")
    if costs and fee:
        return card
    gates = []
    for g in card.get("gates") or []:
        if g.get("fee_gate") and not fee:
            continue
        if g.get("money_gate") and not costs:
            gates.append({"id": g["id"], "money_gate": True, "hidden": True})
            continue
        gates.append(g)
    out = {**card, "gates": gates}
    if not fee:
        out.update(verdict=None, so_far=None, verdict_reasons=[], fee_hidden=True)
        out["criteria"] = {k: v for k, v in (card.get("criteria") or {}).items() if k != "fee_multiple"}
    out["failing"] = [x for x in card.get("failing") or [] if x in {g["id"] for g in gates if not g.get("hidden")}]
    return out
