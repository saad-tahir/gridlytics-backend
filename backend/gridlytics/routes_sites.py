"""Sites, the fleet, the overview snapshot, connections, assets, tiers, native captures, overrides and wizard drafts (UI-01, 02, 04, 17)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, Body, HTTPException

from . import alerts, auth, battery, delivery, plans, sites, store, tariff_data, tariffs, telemetry
from .routes_common import PKT, bad, full_or_404, now_ts, out, site_or_404, today
from .schemas import Refusal

router = APIRouter()


@router.get("/api/meta")
def meta():
    return {"tiers": sites.TIERS, "discos": sites.DISCOS, "disco_groups": sites.DISCO_GROUPS, "site_types": sites.SITE_TYPES, "brands": sites.BRANDS,
            "brand_grammars": {k: {kk: vv for kk, vv in v.items()} for k, v in delivery.BRANDS.items()},
            "categories": sorted(tariff_data.CARDS_279.keys()), "regimes": ["net_billing", "net_metering_legacy", "none", "flat", "off_grid"],
            "override_kinds": sites.OVERRIDE_KINDS, "role": (auth.current.get() or {}).get("role"),
            "permissions": {p: auth.can(p) for p in auth.PERMISSIONS}}


def control_state(site: dict, last: dict | None, now: int) -> dict:
    ovs = sites.overrides(site["id"])
    kinds = {o["kind"]: o for o in ovs}
    tier = site.get("tier", "monitor")
    age = (now - last["ts"]) / 60 if last else None
    if "pause_control" in kinds:
        return {"key": "paused", "label": "Control paused", "until": kinds["pause_control"]["until_ts"]}
    if "force_native" in kinds:
        return {"key": "native", "label": "Released to native", "until": kinds["force_native"]["until_ts"]}
    if "reserve" in kinds:
        until = datetime.fromisoformat(kinds["reserve"]["until_ts"]).astimezone(PKT).strftime("%H:%M")
        return {"key": "hold", "label": f"Hold until {until}", "until": kinds["reserve"]["until_ts"]}
    if tier == "closed_loop" and age is not None and age > 5 and not site.get("demo_link"):
        return {"key": "edge_offline", "label": "Edge offline"}
    if tier == "monitor":
        return {"key": "recommendation", "label": "Recommendation only"}
    if tier == "advisory_writes":
        with store.connect() as con:
            ok = con.execute("SELECT 1 FROM applied WHERE site_id = ? AND day_local = ?", (site["id"], today().isoformat())).fetchone()
        return {"key": "following" if ok or site.get("demo_link") else "unconfirmed",
                "label": "Following plan (table applied)" if ok or site.get("demo_link") else "Table not confirmed today"}
    return {"key": "following", "label": "Following plan"}


def _snapshot(last: dict | None) -> dict | None:
    if not last:
        return None
    w = lambda k: None if last.get(k) is None else round(last[k] / 1000, 2)
    return {"ts": last["ts"], "pv_kw": w("pv_power_w"), "load_kw": w("load_power_w"), "grid_kw": w("grid_power_w"), "battery_kw": w("battery_power_w"),
            "genset_kw": w("genset_power_w"), "soc_pct": last.get("battery_soc_est_pct"), "grid_present": last.get("grid_present"),
            "genset_running": last.get("genset_running"), "mode": last.get("inverter_mode_active")}


def _mtd_saving(site_id: str) -> tuple[float | None, list]:
    t = today()
    with store.connect() as con:
        rows = con.execute("SELECT day_local, data FROM savings_days WHERE site_id = ? AND baseline = 'native' AND day_local >= ? ORDER BY day_local",
                           (site_id, (t - timedelta(days=40)).isoformat())).fetchall()
    month = t.strftime("%Y-%m")
    mtd = None
    spark = []
    for r in rows:
        d = json.loads(r["data"])
        if r["day_local"].startswith(month):
            mtd = (mtd or 0.0) + d.get("claimed_rs", 0.0)
        spark.append({"day": r["day_local"], "saving_rs": d.get("saving_rs")})
    return (round(mtd, 0) if mtd is not None else None), spark[-7:]


def fleet_row(s: dict, now: int, counts: dict) -> dict:
    last = telemetry.latest(s["id"])
    first = telemetry.first_ts(s["id"])
    plan = plans.current(s["id"])
    nn = plans.now_and_next(plan, now)
    mtd, spark = _mtd_saving(s["id"])
    days_data = (now - first) / 86400 if first else 0
    conn = (sites.connections(s["id"]) or [None])[0]
    return {"id": s["id"], "name": s["name"], "status": s["status"], "tier": s["tier"], "demo": bool(s.get("demo_link")), "city": s["data"].get("city"),
            "disco": s["data"].get("disco"), "category": conn["data"]["tariff_category"] if conn else "Off-grid",
            "control": control_state(s, last, now), "last_data": last["ts"] if last else None, "now": _snapshot(last),
            "next_event": (nn or {}).get("next", [None])[0] if nn and nn.get("next") else None, "open_alerts": counts.get(s["id"], 0),
            "mtd_saving_rs": mtd, "spark": spark, "collecting_day": None if days_data >= 14 else int(days_data) + 1 if first else 0}


@router.get("/api/fleet")
def fleet():
    now = now_ts()
    c = alerts.counts()
    rows = [fleet_row(s, now, c["by_site"]) for s in sites.listing()]
    online = sum(1 for r in rows if r["last_data"] and now - r["last_data"] < 1800)
    return out({"summary": {"sites": len(rows), "online": online, "alerts": c["total"], "critical": c["by_severity"].get("critical", 0),
                            "mtd_saving_rs": round(sum(r["mtd_saving_rs"] or 0 for r in rows), 0),
                            "in_outage": sum(1 for r in rows if r["now"] and r["now"]["grid_present"] == 0 and r["category"] != "Off-grid")},
                "sites": rows})


#: The money settings among a site's and its equipment's fields (plan Part 10.5: battery prices and wear, the genset's rupee costs, the value of lost load):
#: they price the optimiser's choices and the savings ledger's wear lever, so changing one is edit_money_settings (an administrator's), not edit_sites
SITE_MONEY_FIELDS = ("voll_rs_per_kwh", "calendar_cost_on")
ASSET_MONEY_FIELDS = {"battery": ("replacement_cost_per_kwh", "warranted_cycles", "warranted_depth"),
                      "genset": ("c_start_rs", "c_runhr_rs", "c_om_rs_per_kwh")}


def _defaults(model, names) -> dict:
    return {k: model.model_fields[k].default for k in names if k in model.model_fields}


def _need_money_settings(inp: dict, before: dict, names, doing: str) -> None:
    """Refuse a change of any money setting without edit_money_settings; sending a money setting unchanged (a form sent back whole) is not a change."""
    if isinstance(inp, dict) and any(k in inp and inp[k] != before.get(k) for k in names):
        auth.need("edit_money_settings", doing)


@router.get("/api/sites")
def sites_list():
    return out(sites.listing())


@router.post("/api/sites")
def site_create(inp: dict = Body(...)):
    auth.need("edit_sites", "Adding a site")
    _need_money_settings(inp, _defaults(sites.SiteFields, SITE_MONEY_FIELDS), SITE_MONEY_FIELDS, "Setting the site's value of lost load or wear costing")
    try:
        s = sites.create(inp, auth.actor())
    except ValueError as exc:
        raise bad(exc) from None
    return out(sites.full(s["id"]))


@router.get("/api/sites/{site_id}")
def site_get(site_id: str):
    s = full_or_404(site_id)
    s.pop("photo", None)
    return out(s)


@router.put("/api/sites/{site_id}")
def site_update(site_id: str, inp: dict = Body(...)):
    auth.need("edit_sites", "Editing a site")
    s = site_or_404(site_id)
    _need_money_settings(inp, {**_defaults(sites.SiteFields, SITE_MONEY_FIELDS), **(s.get("data") or {})}, SITE_MONEY_FIELDS,
                         "Changing the site's value of lost load or wear costing")
    try:
        sites.update(site_id, inp, auth.actor())
    except ValueError as exc:
        raise bad(exc) from None
    return out(sites.full(site_id))


@router.delete("/api/sites/{site_id}")
def site_delete(site_id: str):
    auth.need("edit_workspace", "Removing a site")
    site_or_404(site_id)
    sites.delete(site_id, auth.actor())
    return {"deleted": site_id}


@router.post("/api/sites/{site_id}/activate")
def site_activate(site_id: str):
    auth.need("edit_sites", "Activating a site")
    s = sites.full(site_id) or {}
    if not s:
        raise HTTPException(status_code=404, detail="No such site")
    missing = []
    if not (s.get("assets") or {}).get("inverter"):
        missing.append("the inverter (tab B)")
    if not s.get("connections") and s["data"].get("site_type") != "farm" and not s["data"].get("off_grid"):
        missing.append("the grid connection and tariff (tab C)")
    if missing:
        raise Refusal(422, "Add " + " and ".join(missing) + " before activating", "incomplete")
    sites.set_status(site_id, "active")
    store.audit(auth.actor(), "activated a site", site_id)
    return out(sites.full(site_id))


@router.put("/api/sites/{site_id}/connection")
def connection_put(site_id: str, inp: dict = Body(...)):
    auth.need("edit_sites", "Editing the grid connection")
    site_or_404(site_id)
    conn_id = inp.pop("id", None)
    try:
        if inp.get("tariff_category") and inp["tariff_category"] not in tariff_data.CARDS_279:
            raise ValueError(f"Unknown tariff category {inp['tariff_category']}")
        sites.put_connection(site_id, inp, auth.actor(), conn_id)
    except ValueError as exc:
        raise bad(exc) from None
    return out(sites.full(site_id))


@router.put("/api/sites/{site_id}/assets/{role}")
def asset_put(site_id: str, role: str, inp: dict = Body(...)):
    auth.need("edit_sites", "Editing the site's equipment")
    site_or_404(site_id)
    inp.pop("id", None)
    names = ASSET_MONEY_FIELDS.get(role, ())
    if names:
        cur = sites.assets(site_id).get(role)
        before = {**_defaults(sites.ASSET_MODELS[role], names), **(cur or {})}
        if cur is not None:
            # a money setting left out keeps its stored value (the asset is saved whole, so it would otherwise fall back to its default)
            inp.update({k: cur[k] for k in names if k not in inp and k in cur})
        _need_money_settings(inp, before, names, "Changing the equipment's money settings (prices and wear)")
    try:
        sites.put_asset(site_id, role, inp, auth.actor())
    except ValueError as exc:
        raise bad(exc) from None
    return out(sites.full(site_id))


@router.delete("/api/sites/{site_id}/assets/{role}")
def asset_delete(site_id: str, role: str):
    auth.need("edit_sites", "Removing equipment")
    site_or_404(site_id)
    sites.remove_asset(site_id, role, auth.actor())
    return out(sites.full(site_id))


@router.put("/api/sites/{site_id}/tier")
def tier_put(site_id: str, inp: dict = Body(...)):
    auth.need("change_tier", "Changing the control tier")
    s = site_or_404(site_id)
    tier = inp.get("tier")
    if tier not in sites.TIERS:
        raise Refusal(422, "The tier is monitor, advisory_writes or closed_loop", "invalid")
    if sites.TIERS.index(tier) > sites.TIERS.index(s["tier_capable"]):
        raise Refusal(422, f"This site's data source supports {s['tier_capable'].replace('_', ' ')} at most", "above_capable")
    if tier != "monitor" and not sites.native_latest(site_id):
        raise Refusal(422, "Capture the inverter's native settings before control is allowed: they are the fallback and the baseline", "no_capture")
    try:
        sites.set_tier_allowed(site_id, tier, auth.actor(), str(inp.get("signoff", ""))[:200])
    except ValueError as exc:
        raise bad(exc) from None
    return out(sites.full(site_id))


@router.get("/api/sites/{site_id}/native")
def native_get(site_id: str):
    site_or_404(site_id)
    return {"latest": sites.native_latest(site_id), "versions": sites.native_versions(site_id)}


@router.post("/api/sites/{site_id}/native")
def native_post(site_id: str, inp: dict = Body(...)):
    auth.need("edit_sites", "Capturing the native settings")
    site_or_404(site_id)
    try:
        return sites.capture_native(site_id, inp, auth.actor())
    except ValueError as exc:
        raise bad(exc) from None


@router.get("/api/sites/{site_id}/overrides")
def overrides_get(site_id: str, all: bool = False):
    site_or_404(site_id)
    return sites.overrides(site_id, active_only=not all)


@router.post("/api/sites/{site_id}/overrides")
def overrides_post(site_id: str, inp: dict = Body(...)):
    site_or_404(site_id)
    kind = inp.get("kind")
    auth.need(sites.OVERRIDE_PERMISSION.get(kind, "hold_release"), "Setting this override")
    try:
        return sites.add_override(site_id, inp, auth.actor())
    except ValueError as exc:
        raise bad(exc) from None


@router.delete("/api/sites/{site_id}/overrides/{oid}")
def overrides_delete(site_id: str, oid: str):
    """Clearing an override needs the permission that setting its kind needs (plan Part 10.5): resuming after a pause is resume_control and lifting a forced
    charge pin_genset, both an administrator's, so an operator cannot undo either by deleting it."""
    auth.need("hold_release", "Clearing an override")
    site_or_404(site_id)
    with store.connect() as con:
        held = con.execute("SELECT kind FROM overrides WHERE id = ? AND site_id = ? AND cleared IS NULL", (oid, site_id)).fetchone()
    if held is None:
        raise HTTPException(status_code=404, detail="No such override")
    auth.need(sites.OVERRIDE_PERMISSION.get(held["kind"], "hold_release"), "Clearing this override")
    sites.clear_override(site_id, oid, auth.actor())
    return sites.overrides(site_id)


@router.get("/api/sites/{site_id}/now")
def site_now(site_id: str):
    """The overview's snapshot: the energy flow now, the plan's current step and the next three reasons, today's and the month's figures."""
    s = full_or_404(site_id)
    now = now_ts()
    last = telemetry.latest(site_id)
    plan = plans.current(site_id)
    t = today()
    tdays = telemetry.days(site_id, t.replace(day=1).isoformat(), t.isoformat())
    td = next((d for d in tdays if d["day"] == t.isoformat()), None)
    if td is None and last:
        td = telemetry.aggregate_day(site_id, t)
    mtd, spark = _mtd_saving(site_id)
    a = s.get("assets") or {}
    caps = {"pv_kw": (a.get("pv") or {}).get("kw_ac"), "battery_kw": (a.get("battery") or {}).get("p_dis_kw"), "genset_kw": (a.get("genset") or {}).get("rated_kw"),
            "grid_kw": (s["connections"][0]["data"]["sanctioned_load_kw"] if s.get("connections") else None)}
    bs = battery.state(s, now)
    sum_ = lambda k: round(sum((d.get(k) or 0) for d in tdays), 1)
    return out({"site": {k: s[k] for k in ("id", "name", "tier", "status", "demo_link")}, "control": control_state(s, last, now), "now": _snapshot(last),
                "caps": caps, "now_next": plans.now_and_next(plan, now), "plan_status": (plan or {}).get("summary", {}).get("status_label"),
                "stage": (plan or {}).get("summary", {}).get("stage"), "today": td,
                "month": {"import_kwh": sum_("import_kwh"), "pv_kwh": sum_("pv_kwh"), "genset_hours": sum_("genset_hours"), "litres": sum_("litres"),
                          "outage_hours": round(sum((d.get("outage_minutes") or 0) for d in tdays) / 60, 1),
                          "peak_kw": max([d.get("peak_kw") or 0 for d in tdays] or [0]), "saving_rs": mtd},
                "spark": spark, "battery": {k: (bs or {}).get(k) for k in ("soc_pct", "sigma_pct", "trust", "usable_kwh")} if bs else None,
                "alerts": alerts.listing("active", site_id)[:5], "last_data": last["ts"] if last else None,
                "first_data": telemetry.first_ts(site_id)})


@router.get("/api/sites/{site_id}/overview/chart")
def overview_chart(site_id: str, hours: int = 24):
    """The actual-versus-plan chart: the last day's 15-minute actuals and the active plan's steps for the next day."""
    site_or_404(site_id)
    now = now_ts()
    hours = max(6, min(int(hours), 72))
    actual = telemetry.series(site_id, now - hours * 3600, now, ["pv_power_w", "load_power_w", "grid_power_w", "battery_power_w", "battery_soc_est_pct",
                                                                   "genset_power_w", "grid_present"], "15m")
    plan = plans.current(site_id)
    steps = [st for st in (plan or {}).get("steps", []) if st["t"] < now + hours * 3600]
    return out({"actual": actual, "plan": steps, "now": now})


@router.get("/api/drafts/{draft_id}")
def draft_get(draft_id: str):
    return sites.draft_get(auth.actor(), draft_id) or {}


@router.put("/api/drafts/{draft_id}")
def draft_put(draft_id: str, inp: dict = Body(...)):
    auth.need("edit_sites", "Saving a draft")
    if len(json.dumps(inp)) > 200_000:
        raise Refusal(422, "The draft is too large", "invalid")
    return sites.draft_put(auth.actor(), draft_id, inp)


@router.get("/api/sites/{site_id}/prices")
def site_prices(site_id: str, day: str | None = None):
    """The day's 15-minute price vector on the site's bearable-cost basis, as the optimiser sees it (the wizard and tariff pages draw it)."""
    auth.need("see_costs", "Seeing prices")
    s = full_or_404(site_id)
    from .routes_common import parse_day
    d = parse_day(day, today())
    conn = (s.get("connections") or [None])[0]
    t0 = datetime(d.year, d.month, d.day, tzinfo=PKT)
    try:
        v = tariffs.vectors(conn, t0.astimezone(timezone.utc), 96, None, None, 1e9)
    except LookupError as exc:
        raise bad(exc) from None
    return {"day": d.isoformat(), "t": [int(t0.timestamp()) + 900 * i for i in range(96)], "c_imp": [round(float(x), 2) for x in v["c_imp"]],
            "c_exp": [round(float(x), 2) for x in v["c_exp"]], "peak": [bool(x) for x in v["peak"]], "mdi": v["mdi"],
            "icp": {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in val.items()} for k, val in v["icp"].items()},
            "energy_factor": round(tariffs.energy_factor(conn, d), 4) if conn else None}
