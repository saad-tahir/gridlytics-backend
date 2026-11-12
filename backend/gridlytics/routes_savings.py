"""Savings (UI-12), the pilot set-up (UI-13), switchback, bills and the fee check; the fleet roll-up."""
from __future__ import annotations

import contextvars
import copy
import csv
import io
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, Response
from fastapi.responses import PlainTextResponse

from . import auth, gonogo, jobs, savings_export, sites, store, tariffs, telemetry, verify
from .routes_common import bad, full_or_404, out, parse_day, site_or_404, today
from .schemas import PilotEdit, PilotStart, Refusal, SwitchbackPaint, check

router = APIRouter()


@router.get("/api/sites/{site_id}/savings")
def savings(site_id: str, start: str | None = None, end: str | None = None):
    site_or_404(site_id)
    t = today()
    d0 = parse_day(start, t.replace(day=1))
    d1 = parse_day(end, t)
    summ = verify.summary(site_id, d0.isoformat(), d1.isoformat(), see_fee=auth.can("see_fee"))
    tdays = telemetry.days(site_id, d0.isoformat(), d1.isoformat())
    summ["energy"] = {k: round(sum((d.get(k) or 0) for d in tdays), 1) for k in ("import_kwh", "export_kwh", "pv_kwh", "load_kwh", "genset_kwh", "litres",
                                                                                  "genset_hours", "throughput_kwh", "unserved_kwh")}
    summ["energy"]["outage_hours"] = round(sum((d.get("outage_minutes") or 0) for d in tdays) / 60, 1)
    summ["energy"]["peak_kw"] = max([d.get("peak_kw") or 0 for d in tdays] or [0])
    # each day's outage hours (energy, not money), so the page can say which losing days had no outage: such a day pays for holding the outage
    # reserve, which the outage days repay (round 7, audit flow-savings-pilot-bill-17); null where the day has no telemetry row
    outages = {d["day"]: round((d.get("outage_minutes") or 0) / 60, 1) for d in tdays}
    for d in summ["days"]:
        d["outage_hours"] = outages.get(d["day"])
    # the baseline the page is priced against: the native captures its days were replayed on (round 7, audit flow-savings-pilot-bill-19)
    summ["native_captures"] = _native_captures(site_id, summ["days"], d1)
    summ["native_latest_version"] = (sites.native_latest(site_id) or {}).get("version")
    # the genset's litres as the actual arm reads them (metered, else the fitted curve) and where they came from, so the page can mark curve litres
    # "Estimated"; the day rows hold metered litres only. Read per minute, so capped at about two months (a longer range keeps the metered sum and
    # names no source)
    summ["energy"]["litres_source"] = None
    if 0 <= (d1 - d0).days <= 62:
        try:
            lu = verify.litres_used(site_id, d0.isoformat(), d1.isoformat())
            if lu.get("litres_source"):
                summ["energy"].update(litres=round(float(lu["litres"]), 1), litres_source=lu["litres_source"])
        except Exception:  # noqa: BLE001  (the figure is a label; never fail the page for it)
            pass
    if not auth.can("see_costs"):
        # a viewer reads the energy, the litres (saved and burnt) and the day counts, none of them money; the filter takes out every rupee figure, the
        # levers and the fee (audit role-viewer-19)
        summ["hidden_for_role"] = True
    return out(summ)


def _native_captures(site_id: str, days: list[dict], d1: date) -> list[dict]:
    """Each native capture the period's days were replayed on (the version each scored day stores), with its settings (work mode, backup state of
    charge, grid charging, slots), who recorded it, how and when, and the first and last day and the number of days it priced, oldest first. A
    period whose days carry no version (rows stored before round 7) names the capture in force at its end, with no days. Settings, not money: every
    role reads them, as on Devices and data (round 7, audit flow-savings-pilot-bill-19)."""
    by_version: dict[int, list[str]] = {}
    for d in days:
        v = d.get("native_capture_version")
        if isinstance(v, int) and not isinstance(v, bool):
            by_version.setdefault(v, []).append(d["day"])
    out = []
    if by_version:
        with store.connect() as con:
            rows = {r["version"]: r for r in con.execute(
                f"SELECT version, captured_at, by, source, body, photo IS NOT NULL AND photo != '' AS has_photo FROM native_settings "
                f"WHERE site_id = ? AND version IN ({','.join('?' * len(by_version))})", (site_id, *by_version))}
        for v in sorted(by_version):
            r = rows.get(v)
            if r is None:
                continue
            ds = sorted(by_version[v])
            out.append({"version": v, "captured_at": r["captured_at"], "by": r["by"], "source": r["source"], "body": json.loads(r["body"]),
                        "has_photo": bool(r["has_photo"]), "first_day": ds[0], "last_day": ds[-1], "days": len(ds)})
        return out
    cap = verify.native_for_day(site_id, d1)
    if cap is not None:
        out.append({**{k: cap.get(k) for k in ("version", "captured_at", "by", "source", "body", "has_photo")}, "first_day": None, "last_day": None,
                    "days": None})
    return out


@router.get("/api/sites/{site_id}/savings/periods")
def savings_periods(site_id: str):
    """The Savings page's periods only the server can date (R8 2.12: "day, month, billing period, pilot window"; round 7, audit pages-D-23): the
    billing period today falls in (from the day after the last meter reading to today, with the reading that will close it) and the last whole one,
    each with its bill's month, from the first grid connection's reading day as the ledger's MDI months are (tariffs.billing_period_for: the
    calendar month without one), none at an off-grid site or one with no connection saved; and the pilot window, from the start's Pakistan day to
    the switchback calendar's last day (today at the latest), none before the pilot starts. No money: every role reads it."""
    site_or_404(site_id)
    t = today()
    conns = sites.connections(site_id)
    conn = conns[0] if conns and conns[0]["data"].get("regime") != "off_grid" else None
    billing = last = None
    if conn is not None:
        a, reading = tariffs.billing_period_for(conn, t)
        pa, pb = tariffs.billing_period_for(conn, a - timedelta(days=1))
        billing = {"start": a.isoformat(), "end": t.isoformat(), "reading": reading.isoformat(), "month": tariffs.billing_month_for(conn, t),
                   "reading_day": tariffs._reading_day(conn)}
        last = {"start": pa.isoformat(), "end": pb.isoformat(), "month": tariffs.billing_month_for(conn, pb)}
    pilot = None
    with store.connect() as con:
        r = con.execute("SELECT started FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
    if r is not None and r["started"]:
        start = verify.local_day(r["started"])
        blocks = verify.switchback_blocks(site_id)
        cal_end = max((date.fromisoformat(b["block_end"]) for b in blocks), default=None)
        end = min(t, cal_end) if cal_end else t
        pilot = {"start": start.isoformat(), "end": max(start, end).isoformat(), "calendar_end": cal_end.isoformat() if cal_end else None}
    return {"billing": billing, "last_billing": last, "pilot": pilot}


#: How long POST /savings/recompute waits for its job before it answers 202 with the job to read (round 7, audit pr-review-05 with tests-and-perf-11):
#: a few days recompute within it; a month of replays and bound solves, with the later days the walk re-scores, runs on as a job
RECOMPUTE_WAIT_S = 10.0
#: The CPU niceness the recompute runs at where the system has one (Linux, macOS): the scheduler's ticks, whose solves are limited by the wall
#: clock (HANDOFF 8.A.3), take the processor before it
RECOMPUTE_NICE = 10
#: The recompute of each site waiting or running, by (company file, site id): one at a time per site
_recomputes: dict[tuple[str, str], tuple[jobs.Job, date, date]] = {}
_recomputes_lock = threading.Lock()


def _owner() -> str:
    """Whose job a recompute is: the signed-in person's id (their name where there is none)."""
    return str((auth.current.get() or {}).get("id") or auth.actor())


def _low_priority(fn):
    """fn() in a thread of its own at RECOMPUTE_NICE (a Linux thread has its own nice value; a pool worker keeps its own for the next job), in the
    caller's context (its company), its answer or its exception passed back."""
    got: dict = {}

    def body() -> None:
        if hasattr(os, "setpriority") and hasattr(threading, "get_native_id"):
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), RECOMPUTE_NICE)
            except OSError:  # not allowed here: it runs at the pool's own priority
                pass
        try:
            got["value"] = fn()
        except BaseException as exc:  # noqa: BLE001  (raised again in the job's thread, which reports it)
            got["error"] = exc
    t = threading.Thread(target=contextvars.copy_context().run, args=(body,), name="gridlytics-recompute", daemon=True)
    t.start()
    t.join()
    if "error" in got:
        raise got["error"]
    return got["value"]


def _recompute_answer(job: jobs.Job, d0: date, d1: date) -> dict:
    """A recompute job's answer: once done, what it did (`days`, `rescored_days`, `restated_days`) with `status` done; while it waits or runs,
    `{status: queued | running, done, total}` (days of the range scored so far); failed, `{status: failed, error}`. Each with `job_id`, `start`
    and `end`."""
    base = {"job_id": job.id, "start": d0.isoformat(), "end": d1.isoformat()}
    if job.state == "done" and job.result is not None:
        return {**job.result, "status": "done", **base}
    if job.state in jobs.ACTIVE:
        return {"status": job.state, "done": job.done, "total": job.total, **base}
    error = job.error if job.error and job.error != jobs.FAILED else "The recompute could not be completed; the server's log has the cause"
    return {"status": "failed", "error": error, **base}


@router.post("/api/sites/{site_id}/savings/recompute")
def recompute(site_id: str, response: Response, inp: dict = Body(default={})):
    """Score the period's finished days again (run_jobs; at most 62 days), then the later days whose chain they moved. It runs as a job (jobs.py), one
    at a time per site, in a thread of lower priority than the scheduler's ticks (round 7, audit pr-review-05 and tests-and-perf-11: up to 62 days of
    replays and bound solves, and as many later days, ran inside the request). The answer is the job's outcome when it ends within RECOMPUTE_WAIT_S,
    else 202 with `{status: queued | running, job_id, done, total}`, read at GET /sites/:id/savings/recompute/:job_id. A second recompute of the site
    while one runs is refused (409), unless it is the same person's same period, which answers that job."""
    auth.need("run_jobs", "Recomputing savings")
    site_or_404(site_id)
    t = today()
    d0 = parse_day(inp.get("start"), t - timedelta(days=7))
    d1 = parse_day(inp.get("end"), t - timedelta(days=1))
    if (d1 - d0).days > 62:
        raise Refusal(422, "Recompute at most 62 days at a time", "invalid")
    who, owner, place = auth.actor(), _owner(), (str(store.db_path()), site_id)

    def run(progress, stopped) -> dict:
        # round 7 (audit pr-review-02): the later days the forward walk re-scored, and those whose claim changed in a closed month, beside the count
        done = _low_priority(lambda: verify.compute_range_detail(site_id, d0, d1, by=who, progress=progress))
        if not done["days"] and not done["rescored_days"]:
            # a recompute that scored no day (no data in the range) writes no line of its own: the request is audited all the same (round 7, audit
            # security-roles-04)
            store.audit(who, verify.RECOMPUTED, site_id, {"start": d0.isoformat(), "end": d1.isoformat(), "days": []})
        return done
    with _recomputes_lock:
        running = _recomputes.get(place)
        if running is not None and running[0].state in jobs.ACTIVE:
            job, r0, r1 = running
            if job.owner != owner or (r0, r1) != (d0, d1):
                raise Refusal(409, f"A recompute of this site from {r0:%d %b %Y} to {r1:%d %b %Y} is running; try again when it ends", "busy")
        else:
            try:
                job = jobs.start("recompute", site_id, owner, f"recompute:{site_id}:{d0}:{d1}:{time.time_ns()}", run,
                                 expected=max((d1 - d0).days + 1, 0), cache=False, abandon=False)
            except jobs.Refused as exc:
                raise Refusal(429, str(exc), "rate_limited") from None
            _recomputes[place] = (job, d0, d1)
    job = jobs.wait(job, RECOMPUTE_WAIT_S)
    if job.state in jobs.ACTIVE:
        response.status_code = 202
    return _recompute_answer(job, d0, d1)


@router.get("/api/sites/{site_id}/savings/recompute/{job_id}")
def recompute_job(site_id: str, job_id: str):
    """A recompute of the caller's (POST /savings/recompute): `{status: queued | running, done, total}` while it runs, then its outcome. Another
    person's, another site's or one forgotten (an hour after it ended, or after a restart) is a 404."""
    auth.need("run_jobs", "Recomputing savings")
    site_or_404(site_id)
    job = jobs.get(job_id, site_id, _owner())
    place = _recomputes.get((str(store.db_path()), site_id))
    if job is None or job.kind != "recompute":
        raise Refusal(404, "No such recompute: it ended over an hour ago, or the server restarted since", "not_found")
    d0, d1 = (place[1], place[2]) if place and place[0] is job else (None, None)
    ans = _recompute_answer(job, d0 or date.min, d1 or date.min)
    if d0 is None:
        ans.update(start=None, end=None)
    return ans


@router.get("/api/sites/{site_id}/savings/kpis")
def savings_kpis(site_id: str, start: str | None = None, end: str | None = None):
    """Plan Part 8.3's measures the money figures leave out, over the Savings page's period (round 7, audit brief-vs-build-13): reserve adequacy, the
    constraint violations (the minutes read over at most gonogo.VIOLATION_DAYS_MAX days), control health, the genset's run hours and starts and the
    battery's cycles on each arm, and the BMS trust score (gonogo.measures). No money: every role reads them."""
    site_or_404(site_id)
    t = today()
    d0, d1 = parse_day(start, t.replace(day=1)), parse_day(end, t)
    if d1 < d0:
        raise Refusal(422, "The period's last day must be on or after its first", "invalid")
    return out(gonogo.measures(site_id, d0, d1))


#: The savings CSV's own columns, as before round 7; the claim columns (savings_export.CLAIM_COLUMNS) follow them
CSV_COLUMNS = ("day", "saving_native_rs", "saving_tuned_rs", "fidelity", "actual_cost_rs", "native_cost_rs", "litres_actual", "litres_native", "peak_kw",
               "peak_import_kwh")


@router.get("/api/sites/{site_id}/savings/export.csv")
def savings_csv(site_id: str, start: str | None = None, end: str | None = None):
    """The period's days as CSV (export: administrator, analyst): each day's saving, costs, litres and peak, then whether it claims and its part of the
    page's claim, the reason it claims nothing, its levers and MDI share, the native settings' peak, its tariff and capture versions and the plans in
    force over its Pakistan day (savings_export; round 7, audit pages-D-06: the rows summed to a figure the page never showed, and a dispute could
    not be re-run from them)."""
    auth.need("export", "Exporting savings")
    site_or_404(site_id)
    t = today()
    d0, d1 = parse_day(start, t.replace(day=1)).isoformat(), parse_day(end, t).isoformat()
    summ = verify.summary(site_id, d0, d1)
    dates = savings_export.tariff_dates(summ["days"])
    ids = savings_export.plan_ids(site_id, d0, d1) if summ["days"] else {}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([*CSV_COLUMNS, "claim_withheld", *savings_export.CLAIM_COLUMNS, "plan_ids"])
    for d in summ["days"]:
        w.writerow([savings_export.cell(d.get(k)) for k in CSV_COLUMNS] + [savings_export.cell(savings_export.withheld(d))]
                   + savings_export.claim_cells(d, dates) + [";".join(ids.get(d["day"], []))])
    store.audit(auth.actor(), "exported savings", site_id, {"start": d0, "end": d1})
    return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{site_id}-savings.csv"'})


@router.get("/api/sites/{site_id}/switchback")
def switchback(site_id: str):
    site_or_404(site_id)
    return out(verify.switchback_result(site_id))


#: the control tiers in words, for a refusal
TIER_WORDS = {"monitor": "monitor", "advisory_writes": "advisory writes", "closed_loop": "closed loop"}


@router.put("/api/sites/{site_id}/switchback")
def switchback_paint(site_id: str, inp: dict = Body(...)):
    """Tab C: paint the calendar (edit_sites), with a strict model (schemas.SwitchbackPaint). Round 7: a monitor-tier site only recommends the plan,
    so it has no switchback (audit pages-D-11); the block length is one the site's control tier runs (verify.BLOCK_DAYS_BY_TIER; audit
    flow-savings-pilot-bill-25); the first day is after today, so no arm is drawn over days already run (audit security-roles-06)."""
    auth.need("edit_sites", "Painting the switchback calendar")
    site = site_or_404(site_id)
    req = check(SwitchbackPaint, inp)
    tier = site.get("tier", "monitor")
    allowed = verify.BLOCK_DAYS_BY_TIER.get(tier, ())
    if not allowed:
        raise Refusal(422, "A monitor-tier site only recommends the plan, so no switchback can run there: its saving is the counterfactual's alone",
                      "monitor_tier")
    if req.block_days not in allowed:
        raise Refusal(422, f"At the {TIER_WORDS.get(tier, tier)} tier each block must be {' or '.join(str(n) for n in allowed)} days long: the "
                           "installer changes the table once per switch", "invalid")
    start = date.fromisoformat(req.start)
    if start <= today():
        raise Refusal(422, "The calendar must start after today: no arm is drawn for a day that has begun", "invalid")
    return verify.plan_switchback(site_id, start, req.fortnights, req.block_days, auth.actor())


@router.get("/api/sites/{site_id}/bills")
def bills(site_id: str):
    auth.need("see_costs", "Seeing bills")
    site_or_404(site_id)
    return out(verify.bills(site_id))


@router.post("/api/sites/{site_id}/bills")
def bill_add(site_id: str, inp: dict = Body(...)):
    auth.need("upload_data", "Entering a bill")
    site_or_404(site_id)
    try:
        return out(verify.add_bill(site_id, inp, auth.actor()))
    except ValueError as exc:
        raise bad(exc) from None


@router.put("/api/sites/{site_id}/bills/{bill_id}")
def bill_correct(site_id: str, bill_id: str, inp: dict = Body(...)):
    """Correct a bill entered with a mistake (the fields of entering one); who may enter a bill may correct it, with an audit line."""
    auth.need("upload_data", "Correcting a bill")
    site_or_404(site_id)
    try:
        return out(verify.correct_bill(site_id, bill_id, inp, auth.actor()))
    except LookupError:
        raise Refusal(404, "No such bill", "not_found") from None
    except ValueError as exc:
        raise bad(exc) from None


@router.delete("/api/sites/{site_id}/bills/{bill_id}")
def bill_delete(site_id: str, bill_id: str):
    """Withdraw a bill entered by mistake, with an audit line."""
    auth.need("upload_data", "Deleting a bill")
    site_or_404(site_id)
    try:
        return verify.delete_bill(site_id, bill_id, auth.actor())
    except LookupError:
        raise Refusal(404, "No such bill", "not_found") from None


@router.put("/api/sites/{site_id}/fee")
def fee_put(site_id: str, inp: dict = Body(...)):
    auth.need("edit_money_settings", "Setting the fee")
    site_or_404(site_id)
    try:
        fee = float(inp.get("fee_rs_per_month", 0))
        if fee < 0:
            raise ValueError("The fee cannot be negative")
    except (TypeError, ValueError) as exc:
        raise bad(exc) from None
    held = store.get_setting(f"fee:{site_id}", None)
    store.set_setting(f"fee:{site_id}", fee)
    # the fee before and after (round 7, audit security-roles-04: a money setting's line says what it was)
    store.audit(auth.actor(), "set the fee", site_id, {"fee_rs_per_month": fee, "changes": store.changes(
        {"fee_rs_per_month": None if held is None else float(held)}, {"fee_rs_per_month": fee})})
    return {"fee_rs_per_month": fee}


# ------------------------------------------------------------------------------------------------ pilot (UI-13)

#: the criteria tab D prefills (R8 2.13): plan 15.3's gates, the genset's run hours cut against the tuned rule and the cycles a day against the tuned
#: baseline's among them (round 7, audit pages-D-12: they were missing; a set-up stored before reads them at these values)
PILOT_DEFAULT = {"baseline_start": None, "baseline_days": 14, "criteria": {"min_saving_pct": 8.0, "max_unserved_kwh": 0.0, "reserve_adequacy_pct": 90.0,
                                                                          "fidelity_pct": 80.0, "fee_multiple": 3.0, "forecast_pv_nrmse_max": 0.35,
                                                                          "genset_hours_cut_pct": 15.0, "cycles_ratio_max": 1.1},
                 "signoff": None, "amendments": [], "closures": []}


def _pilot_data(r) -> dict:
    """The stored set-up over the defaults. A row stored before the request model (round 7, audit pages-D-04) may hold criteria that are not a set of
    numbers (null crashed the page); each criterion that is not a number reads as its default."""
    stored = json.loads(r["data"]) if r else {}
    data = {**copy.deepcopy(PILOT_DEFAULT), **(stored if isinstance(stored, dict) else {})}
    got = data.get("criteria") if isinstance(data.get("criteria"), dict) else {}
    data["criteria"] = {k: float(got[k]) if isinstance(got.get(k), (int, float)) and not isinstance(got.get(k), bool) else v
                        for k, v in PILOT_DEFAULT["criteria"].items()}
    if not isinstance(data.get("closures"), list):
        data["closures"] = []
    # an amendment recorded before round 7 names the keys sent, with no reason and no values (audit flow-savings-pilot-bill-30, pages-D-17)
    data["amendments"] = [{**a, "reason": a.get("reason"), "changes": list(a.get("changes") or []), "values": list(a.get("values") or [])}
                          for a in (data["amendments"] if isinstance(data.get("amendments"), list) else []) if isinstance(a, dict)]
    return data


#: the pilot's fields only a role that sees the fee reads, in the set-up and in its amendments (owner's rule: no money ratio without the fee)
PILOT_FEE_FIELDS = frozenset({"criteria.fee_multiple"})


def _pilot_for_reader(ans: dict) -> dict:
    """The pilot answer as the reader may see it: without `see_fee`, no fee multiple in the criteria nor in an amendment's changes and values."""
    if auth.can("see_fee"):
        return ans
    p = ans["pilot"]
    crit = {k: v for k, v in p["criteria"].items() if f"criteria.{k}" not in PILOT_FEE_FIELDS}
    ams = [{**a, "changes": [c for c in a["changes"] if c not in PILOT_FEE_FIELDS],
            "values": [v for v in a["values"] if v.get("field") not in PILOT_FEE_FIELDS]} for a in p["amendments"]]
    return {**ans, "pilot": {**p, "criteria": crit, "amendments": ams}}


def _pilot_values(before: dict, after: dict) -> list[dict]:
    """Each changed field of the set-up with its value before and after ("criteria.min_saving_pct": 8.0 to 5.0); a field sent unchanged is no change."""
    out = []
    for k in ("baseline_start", "baseline_days"):
        if before.get(k) != after.get(k):
            out.append({"field": k, "before": before.get(k), "after": after.get(k)})
    for k in PILOT_DEFAULT["criteria"]:
        a, b = before["criteria"].get(k), after["criteria"].get(k)
        if a != b:
            out.append({"field": f"criteria.{k}", "before": a, "after": b})
    if before.get("closures") != after.get("closures"):
        out.append({"field": "closures", "before": before.get("closures"), "after": after.get("closures")})
    return out


def _adopt_capture(site_id: str, version: int) -> tuple[list[dict], dict]:
    """A started pilot's pins with a later capture adopted, from the day it was captured (it stood for that day's end; never before the last pin),
    and the amendment's value. Only a capture newer than the one the pilot replays now can be adopted (round 7, audit flow-savings-pilot-bill-12)."""
    pins = verify.pilot_capture_pins(site_id)
    cur = pins[-1]["version"] if pins else None
    row = next((v for v in sites.native_versions(site_id) if v["version"] == version), None)
    if row is None:
        raise Refusal(422, f"There is no capture version {version}", "invalid")
    if cur is not None and version <= cur:
        raise Refusal(422, f"The pilot replays version {cur}: only a later capture can be adopted", "invalid")
    since = max(verify.local_day(row["captured_at"]).isoformat(), pins[-1]["from"] if pins else "")
    return pins + [{"version": version, "from": since}], {"field": "capture_version", "before": cur, "after": version}


@router.get("/api/sites/{site_id}/pilot")
def pilot_get(site_id: str):
    site_or_404(site_id)
    return _pilot_for_reader(_pilot_answer(site_id))


def _pilot_answer(site_id: str) -> dict:
    """The set-up, its start, the capture the pilot replays and the calendar. Once started, `capture` is the pinned capture in force now
    (verify.pilot_capture_pins) and `capture_newer` a later capture no amendment has adopted yet (round 7, audit flow-savings-pilot-bill-12)."""
    with store.connect() as con:
        r = con.execute("SELECT * FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
    data = _pilot_data(r)
    latest, capture, newer = sites.native_latest(site_id), None, None
    pins = verify.pilot_capture_pins(site_id) if r is not None and r["started"] else []
    if pins:
        data["capture_pins"] = pins
        capture = verify.native_for_day(site_id, max(today(), date.fromisoformat(pins[-1]["from"])))
        newer = latest if latest and capture and latest["version"] > capture["version"] else None
    tier = (sites.get(site_id) or {}).get("tier", "monitor")
    gates, checks = verify.pilot_gates(site_id, data, today(), started=bool(r is not None and r["started"]))
    rec = verify.paint_record(site_id)
    return {"pilot": data, "started": r["started"] if r else None, "capture": capture or latest, "capture_newer": newer,
            "switchback": verify.switchback_blocks(site_id),
            # tab A: the window's completeness so far and the KPIs the pilot measures (round 7, audit pages-D-19)
            "baseline_quality": verify.baseline_quality(site_id, data["baseline_start"], data["baseline_days"], today()),
            "kpis": verify.pilot_kpis(site_id),
            # tab C and the start (round 7, audit pages-D-11, flow-savings-pilot-bill-25, security-roles-06, flow-savings-pilot-bill-11): the
            # control tier and the block lengths it runs, the calendar's paint as stored, its rule checks and what the start still needs
            "tier": tier, "block_days_allowed": list(verify.BLOCK_DAYS_BY_TIER.get(tier, ())),
            "calendar": {k: rec.get(k) for k in PAINT_FIELDS} if rec else None, "calendar_checks": checks, "start_gates": gates}


#: the paint record's fields the pilot answers (the blocks it drew are the calendar's own)
PAINT_FIELDS = ("painted_at", "painted_on", "by", "draw", "paint_count", "start", "pairs", "block_days")

#: a calendar rule check in words, for the start's refusal
CHECK_WORDS = {"balanced": "both arms equally often", "paired": "one block of each arm in each pair", "joined": "blocks back to back",
               "length": f"one block length of {' or '.join(map(str, verify.BLOCK_DAYS_OPTIONS))} days",
               "tier-length": "a block length the control tier runs",
               "enough": f"at least {verify.SWITCHBACK_MIN_DAYS_PER_ARM} days on each arm", "after-baseline": "a start after the baseline window",
               "future": "a start after today", "closures": "no block on a holiday closure"}


@router.put("/api/sites/{site_id}/pilot")
def pilot_put(site_id: str, inp: dict = Body(...)):
    """Tabs A and D: the baseline window and the success criteria, typed and in range (schemas.PilotEdit). The sign-off and the amendments are the
    server's: the start writes the one, a change after the start appends to the other (round 7, audit pages-D-04, security-roles-05,
    flow-savings-pilot-bill-10). Each change is audited with its fields' values before and after; once the pilot has started it is an amendment
    that needs a reason and records those values, and a save that changes nothing is refused rather than recorded (audit flow-savings-pilot-bill-30,
    pages-D-17)."""
    auth.need("start_pilot", "Editing the pilot")
    site_or_404(site_id)
    edit = check(PilotEdit, inp)
    sent = edit.model_dump(exclude_unset=True)
    reason = sent.pop("reason", "").strip()
    with store.write_transaction() as con:
        r = con.execute("SELECT * FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
        data = _pilot_data(r)
        started = r["started"] if r else None
        if started and "baseline_start" in sent and sent["baseline_start"] is None:
            raise Refusal(422, "A started pilot keeps its baseline window: change its first day instead", "invalid")
        if "criteria" in sent:
            sent["criteria"] = {**data["criteria"], **sent["criteria"]}
        if "closures" in sent:  # tab C's declared holiday closures, in date order (round 7, audit pages-D-19)
            sent["closures"] = sorted((c.model_dump() for c in edit.closures), key=lambda c: (c["start"], c["end"]))
        adopt = sent.pop("capture_version", None)
        new = {**data, **sent}
        values = _pilot_values(data, new)
        if adopt is not None:
            if not started:
                raise Refusal(422, "Until the pilot starts it uses the latest capture; a capture is adopted only by a started pilot", "not_started")
            new["capture_pins"], value = _adopt_capture(site_id, adopt)
            values.append(value)
        if started:
            if not values:
                raise Refusal(422, "Nothing changed, so there is no amendment to record", "no_change")
            if not reason:
                raise Refusal(422, "Give the amendment's reason: it is recorded with the change", "reason_needed")
            new["amendments"] = data["amendments"] + [{"at": store._now(), "by": auth.actor(), "reason": reason,
                                                       "changes": [v["field"] for v in values], "values": values}]
        if values:
            con.execute("INSERT INTO pilots (site_id, data, updated) VALUES (?,?,?) ON CONFLICT(site_id) DO UPDATE SET data = excluded.data, "
                        "updated = excluded.updated", (site_id, json.dumps(new), store._now()))
            store.audit(auth.actor(), "amended the pilot" if started else "changed the pilot set-up", site_id,
                        {"reason": reason, "values": values} if started else {"values": values}, con=con)
    return pilot_get(site_id)


def _start_gates(site_id: str, r) -> None:
    """Refuse the start at the first gate not met (round 7, audit flow-savings-pilot-bill-11, pages-D-18, pages-D-11): a tier that runs the plan,
    the capture, the baseline window, the calendar and its rule checks, in that order (before: the capture and any calendar, so a past calendar at a
    monitor site with no window started). Called inside the start's write transaction, so a repaint cannot slip between the checks and the lock."""
    gates = dict((g["id"], g) for g in verify.pilot_gates(site_id, _pilot_data(r), today())[0])
    if not gates["tier"]["ok"]:
        raise Refusal(422, "A monitor-tier site only recommends the plan, so no switchback can run there: raise its control tier to advisory writes "
                           "before the pilot starts", "monitor_tier")
    if not gates["capture"]["ok"]:
        raise Refusal(422, "Capture the inverter's native settings first (tab B)", "no_capture")
    if not gates["baseline"]["ok"]:
        raise Refusal(422, "Choose the baseline window first (tab A)", "no_baseline")
    if not gates["calendar"]["ok"]:
        raise Refusal(422, "Paint the switchback calendar first (tab C)", "no_calendar")
    if not gates["rules"]["ok"]:
        failing = gates["rules"]["failing"]
        raise Refusal(422, f"The switchback calendar does not meet {len(failing)} of its rule checks ({'; '.join(CHECK_WORDS.get(c, c) for c in failing)}): "
                           "fix it in tab C", "calendar_rules")


@router.post("/api/sites/{site_id}/pilot/start")
def pilot_start(site_id: str, inp: dict = Body(default={})):
    """Sign off and start the pilot, once: the start time and the sign-off are the pre-registration, so a second start is refused rather than
    written over them (round 7, audit flow-savings-pilot-bill-09); a later change is an amendment (PUT)."""
    auth.need("start_pilot", "Starting the pilot")
    site_or_404(site_id)
    name = check(PilotStart, inp).name
    with store.write_transaction() as con:
        r = con.execute("SELECT * FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
        if r is not None and r["started"]:
            raise Refusal(409, "The pilot has already started: its start and sign-off stand, and a change is an amendment", "already_started")
        _start_gates(site_id, r)
        now = store._now()
        # the capture the pilot is signed off on is pinned: a later one stands for a pilot day only once an amendment adopts it (round 7, audit
        # flow-savings-pilot-bill-12)
        pin = {"version": sites.native_latest(site_id)["version"], "from": verify.local_day(now).isoformat()}
        data = {**_pilot_data(r), "signoff": {"by": auth.actor(), "at": now, "name": name}, "capture_pins": [pin]}
        con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?) ON CONFLICT(site_id) DO UPDATE SET data = excluded.data, "
                    "started = excluded.started, updated = excluded.updated", (site_id, json.dumps(data), now, now))
        con.execute("UPDATE switchback SET locked = 1 WHERE site_id = ?", (site_id,))
        store.audit(auth.actor(), "started the pilot", site_id, {"name": name, "capture_version": pin["version"]}, con=con)
    return pilot_get(site_id)


def _ics_text(value: str) -> str:
    """An iCalendar TEXT value (RFC 5545 3.3.11): backslash, semicolon and comma escaped, a line break written as \\n, other control characters
    dropped, so typed text such as a site's name cannot start a property or an event of its own (round 7, audit security-roles-14)."""
    v = value.replace("\r\n", "\n").replace("\r", "\n")
    v = "".join(ch for ch in v if ch == "\n" or ch == "\t" or not (ord(ch) < 32 or ord(ch) == 127))
    return v.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics_fold(line: str) -> list[str]:
    """A content line folded at 75 octets (RFC 5545 3.1): each continuation starts with a space, and no UTF-8 character is split."""
    out, cur, size, limit = [], "", 0, 75
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > limit:
            out.append(cur)
            cur, size, limit = " ", 1, 75
        cur += ch
        size += n
    return out + [cur]


@router.get("/api/sites/{site_id}/pilot/scorecard")
def pilot_scorecard(site_id: str):
    """The pilot's pre-registered gates read against its results from its start (round 7, audit pages-D-12, brief-vs-build-02,
    flow-savings-pilot-bill-13: the criteria were stored and never scored): each gate met, not met, not measured yet or not applying here, and plan
    15.3's verdict once 8 weeks of switchback have run (gonogo.scorecard). A money gate's figures only to a role with costs, and a gate read against
    the fee, with the verdict, only to a role that sees the fee (gonogo.for_reader)."""
    site_or_404(site_id)
    return out(gonogo.for_reader(gonogo.scorecard(site_id, today())))


@router.get("/api/sites/{site_id}/pilot/calendar.ics")
def pilot_ics(site_id: str):
    """The calendar for a calendar app (tab C). Round 7 (audit security-roles-14, flow-savings-pilot-bill-26): the site's name is escaped as
    TEXT, each event carries DTSTAMP (when the file was made, UTC), long lines are folded, and the file comes with its name."""
    s = site_or_404(site_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Gridlytics//Switchback//EN"]
    for b in verify.switchback_blocks(site_id):
        d0 = date.fromisoformat(b["block_start"])
        d1 = date.fromisoformat(b["block_end"]) + timedelta(days=1)
        summary = _ics_text(f"{s['name']}: {'plan' if b['arm'] == 'plan' else 'native settings'} block")
        lines += ["BEGIN:VEVENT", f"UID:{site_id}-{b['block_start']}@gridlytics", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{d0:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{d1:%Y%m%d}", f"SUMMARY:{summary}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(part for line in lines for part in _ics_fold(line)) + "\r\n"
    return PlainTextResponse(body, media_type="text/calendar", headers={"Content-Disposition": f'attachment; filename="{site_id}-switchback.ics"'})


@router.get("/api/fleet/savings")
def fleet_savings(month: str | None = None):
    t = today()
    m = month or t.strftime("%Y-%m")
    try:
        d0 = date(int(m[:4]), int(m[5:7]), 1)
    except (ValueError, IndexError):
        raise Refusal(422, "The month is YYYY-MM", "invalid") from None
    d1 = (d0.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    rows = []
    for s in sites.listing():
        summ = verify.summary(s["id"], d0.isoformat(), d1.isoformat())
        n = summ["baselines"].get("native", {})
        # chain_stale_days: the site's days out of step until recomputed, so its figures may not add up (audit pr-review-06)
        rows.append({"site_id": s["id"], "name": s["name"], "saving_rs": n.get("saving_rs"), "claimed_rs": n.get("claimed_rs"),
                     "litres_saved": n.get("litres_saved"), "verified_days": n.get("verified_days"), "preliminary": summ["preliminary"],
                     "chain_stale_days": list(summ.get("chain_stale_days") or [])})
    return out({"month": m, "sites": rows, "total_claimed_rs": round(sum(r["claimed_rs"] or 0 for r in rows), 0),
                "stale_sites": sum(1 for r in rows if r["chain_stale_days"])})


_ = full_or_404
