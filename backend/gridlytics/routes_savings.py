"""Savings (UI-12), the pilot set-up (UI-13), switchback, bills and the fee check; the fleet roll-up."""
from __future__ import annotations

import json
from datetime import date, timedelta

from fastapi import APIRouter, Body
from fastapi.responses import PlainTextResponse

from . import auth, sites, store, telemetry, verify
from .routes_common import bad, full_or_404, out, parse_day, site_or_404, today
from .schemas import Refusal

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


@router.post("/api/sites/{site_id}/savings/recompute")
def recompute(site_id: str, inp: dict = Body(default={})):
    auth.need("run_jobs", "Recomputing savings")
    site_or_404(site_id)
    t = today()
    d0 = parse_day(inp.get("start"), t - timedelta(days=7))
    d1 = parse_day(inp.get("end"), t - timedelta(days=1))
    if (d1 - d0).days > 62:
        raise Refusal(422, "Recompute at most 62 days at a time", "invalid")
    n = verify.compute_range(site_id, d0, d1)
    return {"days": n}


@router.get("/api/sites/{site_id}/savings/export.csv")
def savings_csv(site_id: str, start: str | None = None, end: str | None = None):
    auth.need("export", "Exporting savings")
    site_or_404(site_id)
    t = today()
    d0, d1 = parse_day(start, t.replace(day=1)).isoformat(), parse_day(end, t).isoformat()
    summ = verify.summary(site_id, d0, d1)
    lines = ["day,saving_native_rs,saving_tuned_rs,fidelity,actual_cost_rs,native_cost_rs,litres_actual,litres_native,peak_kw,peak_import_kwh"]
    for d in summ["days"]:
        lines.append(",".join("" if d.get(k) is None else str(d[k]) for k in ("day", "saving_native_rs", "saving_tuned_rs", "fidelity", "actual_cost_rs",
                                                                               "native_cost_rs", "litres_actual", "litres_native", "peak_kw", "peak_import_kwh")))
    store.audit(auth.actor(), "exported savings", site_id, {"start": d0, "end": d1})
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{site_id}-savings.csv"'})


@router.get("/api/sites/{site_id}/switchback")
def switchback(site_id: str):
    site_or_404(site_id)
    return out(verify.switchback_result(site_id))


@router.put("/api/sites/{site_id}/switchback")
def switchback_paint(site_id: str, inp: dict = Body(...)):
    auth.need("edit_sites", "Painting the switchback calendar")
    site_or_404(site_id)
    try:
        start = date.fromisoformat(str(inp.get("start")))
        fortnights = int(inp.get("fortnights", 4))
        block = int(inp.get("block_days", 7))
        if not (1 <= fortnights <= 26) or block not in (3, 7):
            raise ValueError("Use 1 to 26 fortnights of 3- or 7-day blocks")
    except (ValueError, TypeError) as exc:
        raise bad(exc) from None
    if fortnights * 2 * block > 26 * 14:
        raise Refusal(422, "The calendar is longer than a year", "invalid")
    return verify.plan_switchback(site_id, start, fortnights, block, auth.actor())


@router.get("/api/sites/{site_id}/bills")
def bills(site_id: str):
    auth.need("see_costs", "Seeing bills")
    site_or_404(site_id)
    return verify.bills(site_id)


@router.post("/api/sites/{site_id}/bills")
def bill_add(site_id: str, inp: dict = Body(...)):
    auth.need("upload_data", "Entering a bill")
    site_or_404(site_id)
    try:
        return verify.add_bill(site_id, inp, auth.actor())
    except ValueError as exc:
        raise bad(exc) from None


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
    store.set_setting(f"fee:{site_id}", fee)
    store.audit(auth.actor(), "set the fee", site_id, {"fee_rs_per_month": fee})
    return {"fee_rs_per_month": fee}


# ------------------------------------------------------------------------------------------------ pilot (UI-13)

PILOT_DEFAULT = {"baseline_start": None, "baseline_days": 14, "criteria": {"min_saving_pct": 8.0, "max_unserved_kwh": 0.0, "reserve_adequacy_pct": 90.0,
                                                                          "fidelity_pct": 80.0, "fee_multiple": 3.0, "forecast_pv_nrmse_max": 0.35},
                 "signoff": None, "amendments": []}


@router.get("/api/sites/{site_id}/pilot")
def pilot_get(site_id: str):
    site_or_404(site_id)
    with store.connect() as con:
        r = con.execute("SELECT * FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
    data = json.loads(r["data"]) if r else dict(PILOT_DEFAULT)
    return {"pilot": data, "started": r["started"] if r else None, "capture": sites.native_latest(site_id), "switchback": verify.switchback_blocks(site_id)}


@router.put("/api/sites/{site_id}/pilot")
def pilot_put(site_id: str, inp: dict = Body(...)):
    auth.need("start_pilot", "Editing the pilot")
    site_or_404(site_id)
    cur = pilot_get(site_id)
    data = {**cur["pilot"], **{k: v for k, v in inp.items() if k in PILOT_DEFAULT}}
    if cur["started"]:
        data["amendments"] = (cur["pilot"].get("amendments") or []) + [{"at": store._now(), "by": auth.actor(), "changes": list(inp.keys())}]
    with store.connect() as con:
        con.execute("INSERT INTO pilots (site_id, data, updated) VALUES (?,?,?) ON CONFLICT(site_id) DO UPDATE SET data = excluded.data, updated = excluded.updated",
                    (site_id, json.dumps(data), store._now()))
    return pilot_get(site_id)


@router.post("/api/sites/{site_id}/pilot/start")
def pilot_start(site_id: str, inp: dict = Body(default={})):
    auth.need("start_pilot", "Starting the pilot")
    site_or_404(site_id)
    if not sites.native_latest(site_id):
        raise Refusal(422, "Capture the inverter's native settings first (tab B)", "no_capture")
    if not verify.switchback_blocks(site_id):
        raise Refusal(422, "Paint the switchback calendar first (tab C)", "no_calendar")
    cur = pilot_get(site_id)
    data = {**cur["pilot"], "signoff": {"by": auth.actor(), "at": store._now(), "name": str(inp.get("name", ""))[:120]}}
    with store.connect() as con:
        con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?) ON CONFLICT(site_id) DO UPDATE SET data = excluded.data, "
                    "started = excluded.started, updated = excluded.updated", (site_id, json.dumps(data), store._now(), store._now()))
        con.execute("UPDATE switchback SET locked = 1 WHERE site_id = ?", (site_id,))
    store.audit(auth.actor(), "started the pilot", site_id)
    return pilot_get(site_id)


@router.get("/api/sites/{site_id}/pilot/calendar.ics")
def pilot_ics(site_id: str):
    s = site_or_404(site_id)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Gridlytics//Switchback//EN"]
    for b in verify.switchback_blocks(site_id):
        d0 = date.fromisoformat(b["block_start"])
        d1 = date.fromisoformat(b["block_end"]) + timedelta(days=1)
        lines += ["BEGIN:VEVENT", f"UID:{site_id}-{b['block_start']}@gridlytics", f"DTSTART;VALUE=DATE:{d0:%Y%m%d}", f"DTEND;VALUE=DATE:{d1:%Y%m%d}",
                  f"SUMMARY:{s['name']}: {'plan' if b['arm'] == 'plan' else 'native settings'} block", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return PlainTextResponse("\r\n".join(lines) + "\r\n", media_type="text/calendar")


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
        rows.append({"site_id": s["id"], "name": s["name"], "saving_rs": n.get("saving_rs"), "claimed_rs": n.get("claimed_rs"),
                     "litres_saved": n.get("litres_saved"), "verified_days": n.get("verified_days"), "preliminary": summ["preliminary"]})
    return out({"month": m, "sites": rows, "total_claimed_rs": round(sum(r["claimed_rs"] or 0 for r in rows), 0)})


_ = full_or_404
