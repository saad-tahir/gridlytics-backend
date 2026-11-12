"""Sites, the fleet, the overview snapshot, connections, assets, tiers, native captures, overrides and wizard drafts (UI-01, 02, 04, 17)."""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, Body, Header, HTTPException
from pydantic import ValidationError

from . import alerts, auth, battery, delivery, demo, money, plans, sites, store, store_members, tariff_data, tariffs, telemetry, verify
from .routes_common import PKT, bad, full_or_404, now_ts, out, put_state, site_or_404, today
from .schemas import Refusal

router = APIRouter()

#: The prices page's days: from the earliest telemetry and rate cards the product reads to a year from today
PRICES_EARLIEST = date(2015, 1, 1)
PRICES_AHEAD_DAYS = 366


@router.get("/api/meta")
def meta():
    return {"tiers": sites.TIERS, "discos": sites.DISCOS, "disco_groups": sites.DISCO_GROUPS, "site_types": sites.SITE_TYPES, "brands": sites.BRANDS,
            "brand_grammars": {k: {kk: vv for kk, vv in v.items()} for k, v in delivery.BRANDS.items()},
            "categories": sorted(tariff_data.CARDS_279.keys()), "regimes": ["net_billing", "net_metering_legacy", "none", "flat", "off_grid"],
            "override_kinds": sites.OVERRIDE_KINDS, "role": (auth.current.get() or {}).get("role"),
            "permissions": {p: auth.can(p) for p in auth.PERMISSIONS},
            # the categories the ICP applies to, so tab C asks for the ICP baseline only there (audit engine-tariffs-05)
            "icp_categories": _icp_categories(),
            # what every plan falls back to when no newer one arrives (delivery.fallback_block: no revert timer, as a keyed table stays and repeats each
            # day; an edge keeps the plan it holds through a lost link to its end, then the inverter returns to its native settings; the floor is at least
            # the site's reserve floor), for tab D to show (plan 9.3 tab D "fallback", Part 7.1 step 6; audit flow-onboarding-22, engine-delivery-14)
            "fallback": {"on_expiry": "inverter_native", "on_link_loss_after_s": delivery.LINK_LOSS_AFTER_S},
            # who holds each permission, in the refusals' words ("an administrator, an operator or an analyst"), so a page that says who may act reads
            # it here rather than listing roles of its own (round 7, audit pr-review-07)
            "who_can": {p: auth.roles_words(p) for p in auth.PERMISSIONS}}


def _icp_categories() -> list[str]:
    """The tariff categories of the ICP version in force today (none in force: the shipped one's)."""
    v = tariffs.resolve("icp", "national", today())
    body = v["body"] if v else next(x["body"] for x in tariff_data.SHIPPED if x["kind"] == "icp")
    return list(body.get("categories") or [])


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def until_words(iso: str, now: int) -> str:
    """An end time in Pakistan time for a control label: "19:07" on the day of `now`, else with its day, "Tue 10 Nov, 19:07" (a hold runs up to 72
    hours and the edge's pause seven days, so a time alone can be tomorrow's or next week's)."""
    end = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    end = (end if end.tzinfo else end.replace(tzinfo=timezone.utc)).astimezone(PKT)
    if end.date() == datetime.fromtimestamp(now, PKT).date():
        return f"{end:%H:%M}"
    return f"{_WEEKDAYS[end.weekday()]} {end.day} {_MONTHS[end.month - 1]}, {end:%H:%M}"


def control_state(site: dict, last: dict | None, now: int) -> dict:
    """The control chip: `key`, `label` (in words, an end with its day when it is not today) and, for a pause, a release, a forced charge or a hold,
    `until`."""
    ovs = sites.overrides(site["id"])
    kinds = {o["kind"]: o for o in ovs}
    tier = site.get("tier", "monitor")
    age = (now - last["ts"]) / 60 if last else None
    if "pause_control" in kinds:
        until = kinds["pause_control"]["until_ts"]
        return {"key": "paused", "label": f"Control paused until {until_words(until, now)}", "until": until}
    if "force_native" in kinds:
        until = kinds["force_native"]["until_ts"]
        # a fallback test is a release that says what it is for (round 7, audit flow-telemetry-plan-control-19)
        what = "Fallback test: released to native" if kinds["force_native"].get("reason") == plans.FALLBACK_TEST_REASON else "Released to native"
        return {"key": "native", "label": f"{what} until {until_words(until, now)}", "until": until}
    block = plans.switchback_release(site["id"], now)
    if block:
        # a native block of the switchback calendar: the inverter runs its own settings all day, so no table is keyed in or followed
        return {"key": "native_block", "label": "Switchback: native settings today", "until": block["until"]}
    if "force_charge" in kinds:
        # a forced charge says so (round 7, audit flow-telemetry-plan-control-17: the chip read "Recommendation only"); it outranks a hold's floor
        o = kinds["force_charge"]
        target = 100.0 if o["value"] is None else float(o["value"])
        return {"key": "force_charge", "label": f"Forced charge to {target:.0f} % until {until_words(o['until_ts'], now)}", "until": o["until_ts"]}
    if "reserve" in kinds:
        until = kinds["reserve"]["until_ts"]
        return {"key": "hold", "label": f"Hold until {until_words(until, now)}", "until": until}
    # a demo site says its state as a customer site does (round 7, audit role-owner-08): its chip, its alerts and its applied log agree
    if tier == "closed_loop" and not sites.state(site["id"]).get("edge_last_seen"):
        # closed loop runs through an edge device: with none ever heard from nothing reaches the inverter, whatever the readings say (round 7, audit
        # role-operator-11: the Karachi demo site read "Following plan")
        return {"key": "edge_offline", "label": "No edge device yet"}
    # an edge reports every minute, so its readings are fresh for 5 minutes; a demo site's edge is the twin, which the 15-minute schedule runs, so
    # for demo.EDGE_FRESH_S, as GET /edge says it is online (demo-seed): no data for longer reads "Edge offline" at a demo site too
    if tier == "closed_loop" and age is not None and age > (demo.EDGE_FRESH_S / 60 if site.get("demo_link") else 5):
        return {"key": "edge_offline", "label": "Edge offline"}
    if tier == "closed_loop":
        clock = plans.clock_info(site["id"])
        if clock and clock["level"] == "demoted":
            return {"key": "clock_demoted", "label": f"Control demoted: the inverter clock is {plans.clock_words(clock['offset_s'])} off"}
        # what the edge itself reports (a fallback, control it disabled, the plan refused or applied in part) comes before "Following plan"
        edge = plans.edge_view(site["id"], now)
        if edge:
            return edge
        if clock and clock["level"] == "widen":
            return {"key": "following", "label": f"Following plan (set the inverter clock: it is {plans.clock_words(clock['offset_s'])} off)"}
    if tier == "monitor":
        return {"key": "recommendation", "label": "Recommendation only"}
    if tier == "advisory_writes":
        day = today().isoformat()
        with store.connect() as con:
            marks = con.execute("SELECT plan_id, created FROM applied WHERE site_id = ? AND day_local = ?", (site["id"], day)).fetchall()
        ok = bool(marks)
        issued = plans.issued_for(site["id"], day) if ok else None
        if issued and issued["plan_id"] not in {m["plan_id"] for m in marks} and issued["issued_at"] > max(m["created"] for m in marks):
            # today's table was issued again after the mark (round 7, audit pages-B-02): what the inverter runs is the table keyed before it
            return {"key": "unconfirmed", "label": "New table issued: key it in and mark it"}
        return {"key": "following" if ok else "unconfirmed", "label": "Following plan (table applied)" if ok else "Table not confirmed today"}
    return {"key": "following", "label": "Following plan"}


def _snapshot(last: dict | None) -> dict | None:
    if not last:
        return None
    w = lambda k: None if last.get(k) is None else round(last[k] / 1000, 2)
    # the estimate where the source sends one, else the BMS's own reading (a CSV history or an OEM cloud often carries only the BMS)
    soc = last.get("battery_soc_est_pct") if last.get("battery_soc_est_pct") is not None else last.get("battery_soc_bms_pct")
    # which of the two it is, so Fleet and Overview mark it as the Battery page does (plan 9.7; round 7, frontend-states-formats-19)
    source = "estimate" if last.get("battery_soc_est_pct") is not None else "bms" if last.get("battery_soc_bms_pct") is not None else None
    return {"ts": last["ts"], "pv_kw": w("pv_power_w"), "load_kw": w("load_power_w"), "grid_kw": w("grid_power_w"), "battery_kw": w("battery_power_w"),
            "genset_kw": w("genset_power_w"), "soc_pct": soc, "grid_present": last.get("grid_present"),
            "genset_running": last.get("genset_running"), "mode": last.get("inverter_mode_active"), "soc_source": source}


#: How the month-to-date saving on Fleet and Overview is worked out (plan 9.7: every money figure with its method and computation date): the
#: counterfactual replay against the native capture, as the Savings page's ledger (verify.summary)
MTD_SAVING_METHOD = "counterfactual"


def _mtd_saving(site_id: str, grid: bool = True, genset: bool = True) -> tuple[float | None, str | None, list, dict]:
    """The month's claimed saving against the native settings, exactly as the Savings page's "This month" reads it (verify.summary from the 1st to
    today: the claimed days plus the month's MDI lever), when its days were computed, and the last 7 scored days for the sparkline (each day's claimed
    saving and the replay's figure). The figure is None while no day of the month is scored, never a zero (audit flow-savings-pilot-bill-02: the
    daily claims alone left out the MDI lever, so Fleet and Overview read Rs 1,956 where Savings read Rs 43,594).

    Last, the month's energy behind the claim, which is not money, so a viewer reads it in place of the rupees (R8 2.1, "kWh shifted and litres instead";
    audit pages-A-11): the grid import moved out of the peak window against the native replay and the diesel avoided, summed over the days that claim
    their saving (a recommendation-only day or one whose claim is withheld adds nothing, as it adds no rupee). Each is None with no such day, at a site
    without a grid (`grid`) or a genset (`genset`), or where no claimed day carries the figure (an unmetered genset's litres)."""
    t = today()
    summ = verify.summary(site_id, t.replace(day=1).isoformat(), t.isoformat())
    scored = any(d.get("saving_native_rs") is not None for d in summ["days"])
    claimed = (summ["baselines"].get("native") or {}).get("claimed_rs")
    mtd = round(claimed, 0) if scored and claimed is not None else None
    with store.connect() as con:
        rows = con.execute("SELECT day_local, data FROM savings_days WHERE site_id = ? AND baseline = 'native' AND day_local >= ? ORDER BY day_local",
                           (site_id, (t - timedelta(days=40)).isoformat())).fetchall()
    # each day's claimed saving beside the replay's own figure: the bars draw the claim, as the month column does, so a recommendation-only day's
    # replay noise (the Lahore mall's -Rs 0.09) is not drawn as a loss (audit role-analyst-14, flow-savings-pilot-bill-21, engine-money-live-14)
    spark = []
    month = t.replace(day=1).isoformat()
    peak = litres = None
    block = 0
    for r in rows:
        d = json.loads(r["data"])
        spark.append({"day": r["day_local"], "saving_rs": d.get("saving_rs"), "claimed_rs": d.get("claimed_rs")})
        # the month's days of a switchback native block, which claim nothing: Fleet and Overview say why such a month reads Rs 0 (round 7, audit
        # flow-savings-pilot-bill-18)
        block += int(r["day_local"] >= month and d.get("fidelity") == "native_block")
        if r["day_local"] < month or d.get("fidelity") not in verify.CLAIMING or d.get("claim_withheld"):
            continue
        a, n = (d.get("actual") or {}).get("peak_import_kwh"), (d.get("baseline") or {}).get("peak_import_kwh")
        if grid and a is not None and n is not None:
            peak = (peak or 0.0) + n - a
        if genset and d.get("litres_saved") is not None:
            litres = (litres or 0.0) + d["litres_saved"]
    energy = {"peak_kwh_avoided": None if peak is None else round(peak, 1), "litres_saved": None if litres is None else round(litres, 1)}
    energy["native_block_days"] = block
    return mtd, (summ["computed"] or None) if scored else None, spark[-7:], energy


def _month_native_block_days(site_id: str, t: date) -> int:
    """The days of t's month so far that were a native block of the switchback (a savings day of fidelity native_block): they claim nothing, so the
    overview's month saving says why it reads Rs 0 (round 7, audit flow-savings-pilot-bill-18). The same count as _mtd_saving's for the fleet."""
    with store.connect() as con:
        rows = con.execute("SELECT data FROM savings_days WHERE site_id = ? AND baseline = 'native' AND day_local >= ? AND day_local <= ? AND data LIKE ?",
                           (site_id, t.replace(day=1).isoformat(), t.isoformat(), '%"native_block"%')).fetchall()
    return sum(1 for r in rows if json.loads(r["data"]).get("fidelity") == "native_block")


#: The control states in which a plan is in force at the inverter (R8 2.1's "sites on a plan"), and those that hand the inverter back to its native
#: settings (a release or a pause: "sites on native mode"). control_state is control-edge's: its round-7 keys are named here already, a forced charge
#: (the plan in force, with the charge in it) and a native block of the switchback calendar (handed back for the day)
ON_PLAN = ("following", "hold", "force_charge")
HANDED_BACK = ("native", "paused", "native_block")


def fleet_row(s: dict, now: int, counts: dict) -> dict:
    last = telemetry.latest(s["id"])
    first = telemetry.first_ts(s["id"])
    plan = plans.current(s["id"])
    nn = plans.now_and_next(plan, now)
    days_data = (now - first) / 86400 if first else 0
    conn = (sites.connections(s["id"]) or [None])[0]
    # off-grid is the site's own fact, as tab C and POST /activate read it (_grid_settled): a connection of regime off_grid, or none at a farm or a
    # site whose data says off_grid. A grid site whose connection is not saved yet has no category yet (null), and its outages count (audit pages-A-03)
    off_grid = _off_grid(s, conn)
    has_genset = "genset" in sites.assets(s["id"])
    mtd, computed, spark, energy = _mtd_saving(s["id"], grid=not off_grid, genset=has_genset)
    category = "Off-grid" if off_grid else conn["data"]["tariff_category"] if conn is not None else None
    control = control_state(s, last, now)
    return {"id": s["id"], "name": s["name"], "status": s["status"], "tier": s["tier"], "demo": bool(s.get("demo_link")), "city": s["data"].get("city"),
            "disco": s["data"].get("disco"), "category": category,
            "control": control, "last_data": last["ts"] if last else None, "now": _snapshot(last),
            "next_event": (nn or {}).get("next", [None])[0] if nn and nn.get("next") else None, "open_alerts": counts.get(s["id"], 0),
            "mtd_saving_rs": mtd, "mtd_saving_rs_method": MTD_SAVING_METHOD, "mtd_saving_rs_computed": computed, "spark": spark,
            "collecting_day": None if days_data >= 14 else int(days_data) + 1 if first else 0,
            # the month's energy behind the claim: not money, so a viewer reads it in place of the rupees (R8 2.1; audit pages-A-11)
            "mtd_peak_kwh_avoided": energy["peak_kwh_avoided"], "mtd_litres_saved": energy["litres_saved"],
            # the month's native-block days, which claim nothing (not money; round 7, audit flow-savings-pilot-bill-18)
            "mtd_native_block_days": energy.get("native_block_days", 0),
            # R8 2.1's summary and filters (audit pages-A-17): a plan in force at the inverter (following it or holding; not a monitor site's
            # recommendation, a table not confirmed today, a release, a pause or an offline edge), and a genset on site. control_state names a hold
            # before the tier, so the tier is read here too: a hold at the monitor tier reaches no inverter
            "on_plan": plan is not None and control["key"] in ON_PLAN and s["tier"] != "monitor", "has_genset": has_genset,
            # handed back to the native settings: the row says so, so the web app counts a fleet's real sites by the summary's own rule (review)
            "on_native": control["key"] in HANDED_BACK}


@router.get("/api/fleet")
def fleet():
    now = now_ts()
    c = alerts.counts()
    rows = [fleet_row(s, now, c["by_site"]) for s in sites.listing()]
    online = sum(1 for r in rows if r["last_data"] and now - r["last_data"] < 1800)
    stamps = [r["mtd_saving_rs_computed"] for r in rows if r["mtd_saving_rs_computed"]]
    # the sum of the sites' figures; None while no site has a scored day this month, never Rs 0 (audit frontend-states-formats-14)
    figures = [r["mtd_saving_rs"] for r in rows if r["mtd_saving_rs"] is not None]
    total = lambda k: (round(sum(r[k] for r in rows if r[k] is not None), 1) if any(r[k] is not None for r in rows) else None)  # noqa: E731
    return out({"summary": {"sites": len(rows), "online": online, "alerts": c["total"], "critical": c["by_severity"].get("critical", 0),
                            "mtd_saving_rs": round(sum(figures), 0) if figures else None, "mtd_saving_rs_method": MTD_SAVING_METHOD,
                            "mtd_saving_rs_computed": max(stamps) if stamps else None,
                            "in_outage": sum(1 for r in rows if r["now"] and r["now"]["grid_present"] == 0 and r["category"] != "Off-grid"),
                            "mtd_peak_kwh_avoided": total("mtd_peak_kwh_avoided"), "mtd_litres_saved": total("mtd_litres_saved"),
                            "on_plan": sum(1 for r in rows if r["on_plan"]), "on_native": sum(1 for r in rows if r["on_native"])},
                "sites": rows})


#: The money settings among a site's and its equipment's fields (plan Part 10.5: battery prices and wear, the genset's rupee costs, the value of lost load):
#: they price the optimiser's choices and the savings ledger's wear lever, so changing one is edit_money_settings (an administrator's), not edit_sites
SITE_MONEY_FIELDS = ("voll_rs_per_kwh", "calendar_cost_on")
ASSET_MONEY_FIELDS = {"battery": ("replacement_cost_per_kwh", "warranted_cycles", "warranted_depth"),
                      "genset": ("c_start_rs", "c_runhr_rs", "c_om_rs_per_kwh")}


#: The site's control limits (plan Part 9.4: the administrator "edits limits", the operator works within them; audit role-operator-07): the reserve floor
#: and its outage threshold, which the plan never crosses, the cycle cap, the control mode, the critical load and the battery's charge sources
SITE_LIMIT_FIELDS = ("control_mode", "reserve_policy", "cycle_cap_per_day", "critical_load_kw", "grid_charge_allowed", "gen_charge_allowed")


def _defaults(model, names) -> dict:
    return {k: model.model_fields[k].default for k in names if k in model.model_fields}


def _limits_changed(inp: dict, data: dict) -> bool:
    """Whether the save changes a control limit (SITE_LIMIT_FIELDS): the limits as sites.update would store them, against the stored ones, so a limit sent
    as stored (the form sent back whole, 20 for a stored 20.0, a key a site saved before it existed lacks sent at its default) is no change, while a
    reserve policy sent in part changes the part it leaves out (filled with its default). A limit the model refuses counts as a change: the role is
    checked first, and an administrator's save then gets the field's 422."""
    sent = {k: inp[k] for k in SITE_LIMIT_FIELDS if isinstance(inp, dict) and k in inp}
    if not sent:
        return False
    held = {k: data[k] for k in SITE_LIMIT_FIELDS if k in data}
    as_stored = lambda fields: sites.SiteFields.model_validate({"name": "-", **fields}).model_dump(include=set(sent))  # noqa: E731
    try:
        return as_stored({**held, **sent}) != as_stored(held)
    except ValidationError:
        return True


def _need_money_settings(inp: dict, before: dict, names, doing: str) -> None:
    """Refuse a change of any money setting without edit_money_settings; sending a money setting unchanged (a form sent back whole) is not a change."""
    if isinstance(inp, dict) and any(k in inp and inp[k] != before.get(k) for k in names):
        auth.need("edit_money_settings", doing)


@router.get("/api/sites")
def sites_list():
    return out(sites.listing())


@router.post("/api/sites")
def site_create(inp: dict = Body(...)):
    auth.need("manage_sites", "Adding a site")
    _need_money_settings(inp, _defaults(sites.SiteFields, SITE_MONEY_FIELDS), SITE_MONEY_FIELDS, "Setting the site's value of lost load or wear costing")
    try:
        s = sites.create(inp, auth.actor())
    except ValueError as exc:
        raise bad(exc) from None
    return _site_answer(s["id"])  # every site answer carries tier_signoff, as GET does


@router.get("/api/sites/{site_id}")
def site_get(site_id: str):
    """The site with its connections, assets, state and native capture, and `tier_signoff` ({tier, signoff, by, at} of the latest tier change, or
    null)."""
    return _site_answer(site_id)


def _if_unchanged(inp: dict, held_may_be_none: bool = True):
    """The save's `if_unchanged` (sites.check_unchanged), taken out of the body: the stored values the client's form was based on, or null when it read
    none stored (a connection or an asset only: a site always exists). Left out, the save is not checked (sites.UNCHECKED)."""
    if "if_unchanged" not in inp:
        return sites.UNCHECKED
    expect = inp.pop("if_unchanged")
    if not isinstance(expect, dict) and not (expect is None and held_may_be_none):
        raise Refusal(422, "if_unchanged is the set of stored values the form was based on", "invalid")
    return expect


def _changed(exc: sites.Changed) -> Refusal:
    """A save based on values changed since (audit flow-onboarding-05): 409, with the fields that changed."""
    return Refusal(409, str(exc), "site_changed", extra={"changed": exc.changed})


@router.put("/api/sites/{site_id}")
def site_update(site_id: str, inp: dict = Body(...)):
    """Saves the site's fields (a partial set). With `if_unchanged` (the stored fields the form was based on), a save whose fields were changed since is
    refused with 409 `site_changed` (and `changed`, the fields) instead of writing over the newer values."""
    auth.need("edit_sites", "Editing a site")
    s = site_or_404(site_id)
    expect = _if_unchanged(inp, held_may_be_none=False)
    if _limits_changed(inp, s.get("data") or {}):
        auth.need("manage_sites", "Changing the site's control limits")
    _need_money_settings(inp, {**_defaults(sites.SiteFields, SITE_MONEY_FIELDS), **(s.get("data") or {})}, SITE_MONEY_FIELDS,
                         "Changing the site's value of lost load or wear costing")
    try:
        sites.update(site_id, inp, auth.actor(), expect)
    except sites.Changed as exc:
        raise _changed(exc) from None
    except ValueError as exc:
        raise bad(exc) from None
    return _site_answer(site_id)


@router.delete("/api/sites/{site_id}")
def site_delete(site_id: str):
    auth.need("edit_workspace", "Removing a site")
    site_or_404(site_id)
    sites.delete(site_id, auth.actor())
    return {"deleted": site_id}


def _grid_settled(site: dict, connections: list[dict]) -> bool:
    """Tab C is complete: the site has a grid connection (one of regime off_grid says the site is off-grid), or it is off-grid without one (a farm, or
    a site whose data says off_grid)."""
    data = site.get("data") or {}
    return bool(connections) or data.get("site_type") == "farm" or bool(data.get("off_grid"))


@router.post("/api/sites/{site_id}/source/test")
def site_source_test(site_id: str):
    """The data source's test (plan 9.3 tab A, R8 2.2 "Test connection"; audit flow-onboarding-22): the minutes received in the last day of data, the
    points they carry and whether grid presence is among them (sites.source_test). Nothing is changed: activation refuses a grid-connected site whose
    data carries no grid presence."""
    auth.need("edit_sites", "Testing the data source")
    site_or_404(site_id)
    return sites.source_test(site_id)


@router.post("/api/sites/{site_id}/activate")
def site_activate(site_id: str):
    auth.need("manage_sites", "Activating a site")
    s = sites.full(site_id) or {}
    if not s:
        raise HTTPException(status_code=404, detail="No such site")
    missing = []
    src = (s.get("data") or {}).get("data_source") or {}
    if not (isinstance(src, dict) and src.get("kind") in sites.DATA_SOURCE_KINDS and src.get("grid_signal_confirmed")):
        # plan 9.3 tab A: without a grid-presence signal outages cannot be seen and the reserve cannot be planned (audit flow-onboarding-08: a site with no
        # source, or an unconfirmed one, was activated and planned)
        missing.append("the data source, with its grid-presence signal confirmed (tab A)")
    elif sites.grid_connected(s) and sites.source_test(site_id)["grid_signal"] == "missing":
        # the source's test (plan 9.3: "refuses a source without a grid-present signal"; audit flow-onboarding-22): data arrived, and none of it says
        # whether the grid is up, whatever the tick says
        missing.append("the grid-presence signal to the data the source sends (the data received has neither grid_present nor the grid voltage; tab A)")
    if not (s.get("assets") or {}).get("inverter"):
        missing.append("the inverter (tab B)")
    if not _grid_settled(s, s.get("connections") or []):
        missing.append("the grid connection and tariff (tab C)")
    elif any((c.get("data") or {}).get("tariff_category") not in tariff_data.CARDS_279 for c in s.get("connections") or []):
        # a connection stored before the category was checked (audit flow-onboarding-07): the tick could price nothing and the site never planned
        missing.append("the tariff category of each grid connection (tab C)")
    elif any(sites.group_problem((c.get("data") or {}).get("disco_group"), (s.get("data") or {}).get("disco")) for c in s.get("connections") or []):
        # the site's DISCO changed after its connection was saved: it would be priced with the other DISCO's peak windows (audit flow-onboarding-02)
        missing.append("the DISCO group of each grid connection, to match the site's DISCO (tab C)")
    if missing:
        raise Refusal(422, "Add " + " and ".join(missing) + " before activating", "incomplete")
    sites.set_status(site_id, "active")
    store.audit(auth.actor(), "activated a site", site_id)
    return _site_answer(site_id)


#: A new connection's save is idempotent (a retried first save never inserts a second row): by the client's key when it sends one (the Idempotency-Key
#: header or `idempotency_key` in the body), else by its regime, tariff category and meter reference. Saves of new connections are decided one at a time
_connection_lock = threading.Lock()
IDEMPOTENCY_KEY_MAX = 100
_MATCH_FIELDS = ("regime", "tariff_category", "meter_ref")


def _idem_setting(site_id: str, key: str) -> str:
    return f"connection_idempotency:{site_id}:{key}"


def _same_connection(site_id: str, inp: dict, key: str | None) -> str | None:
    """The id of the connection a save without an id repeats: the one saved with the same idempotency key, or (without a key) the one with the same
    regime, tariff category and meter reference; None for a new connection."""
    held = sites.connections(site_id)
    if key:
        cid = store.get_setting(_idem_setting(site_id, key))
        return cid if any(c["id"] == cid for c in held) else None
    want = {k: inp.get(k, sites.ConnectionFields.model_fields[k].default) for k in _MATCH_FIELDS}
    return next((c["id"] for c in held if all((c.get("data") or {}).get(k) == v for k, v in want.items())), None)


@router.put("/api/sites/{site_id}/connection")
def connection_put(site_id: str, inp: dict = Body(...), idempotency_key: str | None = Header(default=None)):
    """Saves a connection: with `id`, that one; without, a new one, unless the save repeats one already made (`_same_connection`), which is then saved
    in place, so a retried first save never makes a duplicate. Two connections on one site differ in regime, tariff or meter reference, or are saved
    with idempotency keys of their own. With `id` and `if_unchanged` (that connection's stored fields the form was based on, or null when it read
    none), a save whose connection was changed since is refused with 409 `site_changed`."""
    auth.need("edit_sites", "Editing the grid connection")
    site_or_404(site_id)
    conn_id = inp.pop("id", None)
    key = inp.pop("idempotency_key", None) or idempotency_key
    if key is not None and (not isinstance(key, str) or not 1 <= len(key.strip()) <= IDEMPOTENCY_KEY_MAX):
        raise Refusal(422, f"The idempotency key is text of 1 to {IDEMPOTENCY_KEY_MAX} characters", "invalid")
    key = key.strip() if key else None
    # if_unchanged (the stored fields of the connection `id` the form was read from) goes with that id: a new connection has nothing stored to compare
    expect = _if_unchanged(inp)
    if expect is not sites.UNCHECKED and not conn_id:
        raise Refusal(422, "if_unchanged goes with the id of the saved connection it was read from", "invalid")
    try:
        # the tariff category is checked by ConnectionFields (an empty or unknown one is refused, audit flow-onboarding-07)
        if conn_id:
            sites.put_connection(site_id, inp, auth.actor(), conn_id, expect)
        else:
            with _connection_lock:
                same = _same_connection(site_id, inp, key)
                saved = sites.put_connection(site_id, inp, auth.actor(), same)
                if key:
                    store.set_setting(_idem_setting(site_id, key), saved["id"])
    except sites.Changed as exc:
        raise _changed(exc) from None
    except ValueError as exc:
        raise bad(exc) from None
    return _site_answer(site_id)


@router.delete("/api/sites/{site_id}/connection/{conn_id}")
def connection_delete(site_id: str, conn_id: str):
    """Removes a connection (edit_sites, audited). An active site keeps tab C complete: its last connection is removed only when the site is off-grid
    without one (a farm, or data off_grid), else 409 (deactivate the site, or add the new connection first)."""
    auth.need("edit_sites", "Removing a grid connection")
    s = full_or_404(site_id)
    held = s.get("connections") or []
    if not any(c["id"] == conn_id for c in held):
        raise HTTPException(status_code=404, detail="No such connection")
    rest = [c for c in held if c["id"] != conn_id]
    if s.get("status") == "active" and not _grid_settled(s, rest):
        raise Refusal(409, "An active site needs a grid connection: add the new connection before removing this one", "connection_required")
    with store.connect() as con:
        con.execute("DELETE FROM connections WHERE id = ? AND site_id = ?", (conn_id, site_id))
        con.execute("DELETE FROM settings WHERE key LIKE ? AND value = ?", (_idem_setting(site_id, "%"), json.dumps(conn_id)))
    store.audit(auth.actor(), "removed a connection", site_id, {"connection_id": conn_id})
    return _site_answer(site_id)


@router.put("/api/sites/{site_id}/assets/{role}")
def asset_put(site_id: str, role: str, inp: dict = Body(...)):
    """Saves the site's asset of this role. With `if_unchanged` (the stored asset's fields the form was based on, or null when it read none), a save
    whose asset was changed, added or removed since is refused with 409 `site_changed`."""
    auth.need("edit_sites", "Editing the site's equipment")
    site_or_404(site_id)
    inp.pop("id", None)
    expect = _if_unchanged(inp)
    if isinstance(expect, dict):
        expect.pop("id", None)
    names = ASSET_MONEY_FIELDS.get(role, ())
    if names:
        cur = sites.assets(site_id).get(role)
        defaults = _defaults(sites.ASSET_MODELS[role], names)
        before = {**defaults, **(cur or {})}
        if cur is not None:
            # a money setting left out keeps its stored value (the asset is saved whole, so it would otherwise fall back to its default); so does one the
            # role is not shown (an operator, audit role-operator-06) sent at its default, which the form put in place of the value it never received
            hidden = money.unseen(names)
            inp.update({k: cur[k] for k in names if k in cur and (k not in inp or k in hidden and inp[k] == defaults.get(k))})
        _need_money_settings(inp, before, names, "Changing the equipment's money settings (prices and wear)")
    try:
        sites.put_asset(site_id, role, inp, auth.actor(), expect)
    except sites.Changed as exc:
        raise _changed(exc) from None
    except ValueError as exc:
        raise bad(exc) from None
    return _site_answer(site_id)


def _money_set(role: str, cur: dict | None) -> bool:
    """True when a stored battery or genset carries a money setting other than its default (one an administrator set)."""
    names = ASSET_MONEY_FIELDS.get(role, ())
    if not names or not cur:
        return False
    defaults = _defaults(sites.ASSET_MODELS[role], names)
    return any(k in cur and cur[k] != defaults.get(k) for k in names)


@router.delete("/api/sites/{site_id}/assets/{role}")
def asset_delete(site_id: str, role: str):
    """Removing a battery or genset whose money settings an administrator set is edit_money_settings too: re-adding it would take the defaults, so an
    operator could otherwise reset the prices and wear by deleting and re-creating it (plan Part 10.5)."""
    auth.need("edit_sites", "Removing equipment")
    site_or_404(site_id)
    if _money_set(role, sites.assets(site_id).get(role)):
        auth.need("edit_money_settings", "Removing equipment whose money settings (prices and wear) an administrator set")
    sites.remove_asset(site_id, role, auth.actor())
    return _site_answer(site_id)


def _has_inverter_mode(site_id: str) -> bool:
    """Whether any minute of the site carries the inverter's mode: a CSV source without the inverter_mode_active column is for monitor only (plan Part 7.5,
    telemetry.import_csv says so on import), while one with it supports advisory writes (sites.SOURCE_CAPABLE; audit flow-onboarding-03)."""
    with telemetry.connect() as con:
        return con.execute("SELECT 1 FROM minute WHERE site_id = ? AND inverter_mode_active IS NOT NULL LIMIT 1", (site_id,)).fetchone() is not None


@router.put("/api/sites/{site_id}/tier")
def tier_put(site_id: str, inp: dict = Body(...)):
    auth.need("change_tier", "Changing the control tier")
    s = site_or_404(site_id)
    tier = inp.get("tier")
    if tier not in sites.TIERS:
        raise Refusal(422, "The tier is monitor, advisory_writes or closed_loop", "invalid")
    if sites.TIERS.index(tier) > sites.TIERS.index(s["tier_capable"]):
        raise Refusal(422, f"This site's data source supports {s['tier_capable'].replace('_', ' ')} at most", "above_capable")
    if tier != "monitor" and ((s.get("data") or {}).get("data_source") or {}).get("kind") == "csv" and not _has_inverter_mode(site_id):
        raise Refusal(422, "No imported file of this site carries inverter_mode_active, the inverter's mode that the savings comparison needs: until one does, "
                      "a CSV source supports monitor only", "no_inverter_mode")
    if tier != "monitor" and not sites.native_latest(site_id):
        raise Refusal(422, "Capture the inverter's native settings before control is allowed: they are the fallback and the baseline", "no_capture")
    signoff = str(inp.get("signoff", "") or "")[:200]
    try:
        sites.set_tier_allowed(site_id, tier, auth.actor(), signoff)
    except ValueError as exc:
        raise bad(exc) from None
    # the installer's sign-off is kept on the site as well as in the audit, so settings can show it (GET /api/sites/:id answers it as tier_signoff);
    # read, changed and written in one write transaction, so an outage event's pending re-solve stored meanwhile is kept (sites.put_state takes no lock)
    put_state(site_id, tier_signoff={"tier": tier, "signoff": signoff, "by": auth.actor(), "at": store._now()})
    return _site_answer(site_id)


def _tier_signoff(site_id: str, state: dict | None) -> dict | None:
    """The latest change of the allowed tier with its sign-off, {tier, signoff, by, at}: kept in the site's state since round 3, and read from the audit
    trail for a change made before; None when the tier was never changed."""
    held = (state or {}).get("tier_signoff")
    if isinstance(held, dict):
        return held
    with store.connect() as con:
        r = con.execute("SELECT at, actor, detail FROM audit WHERE action = ? AND target = ? ORDER BY id DESC LIMIT 1",
                        ("changed a site's allowed control tier", site_id)).fetchone()
    if r is None:
        return None
    d = json.loads(r["detail"] or "{}")
    return {"tier": d.get("tier"), "signoff": d.get("signoff", ""), "by": r["actor"], "at": r["at"]}


def _site_answer(site_id: str) -> dict:
    """Every answer that is the site: what GET gives, with `tier_signoff` and `setup_warnings` (sites.setup_warnings: what the equipment, connections
    and critical load say against each other, and a missing feeder code; audit flow-onboarding-09 and -17)."""
    s = full_or_404(site_id)
    s.pop("photo", None)
    s["tier_signoff"] = _tier_signoff(site_id, s.get("state"))
    s["setup_warnings"] = sites.setup_warnings(s)
    return out(s)


@router.get("/api/sites/{site_id}/native")
def native_get(site_id: str):
    site_or_404(site_id)
    return {"latest": sites.native_latest(site_id), "versions": sites.native_versions(site_id)}


def _native_slots_fit(site_id: str, inp: dict) -> None:
    """A capture holds no more slots than the site's inverter table: its brand's `max_slots` (delivery.BRANDS, GET /api/meta brand_grammars; audit
    role-operator-16: fourteen were accepted for a six-program Deye). Without a recorded inverter the most any brand holds applies (sites.NativeCapture)."""
    slots = inp.get("slots") if isinstance(inp, dict) else None
    brand = ((sites.assets(site_id).get("inverter")) or {}).get("brand")
    gram = delivery.BRANDS.get(brand) if brand else None
    if gram and isinstance(slots, list) and len(slots) > gram["max_slots"]:
        whose = "An inverter's" if brand == "other" else f"A {gram['label']} inverter's"
        raise Refusal(422, f"{whose} table holds at most {gram['max_slots']} slots", "invalid")


#: A capture sent by a person (POST /native needs edit_sites, which no API key holds) was typed in or taken from a photograph: only the edge device reads the
#: settings back (audit flow-savings-pilot-bill-12, the form's half: a typed capture was marked "read back by the edge device", the most trusted source)
NATIVE_NOT_READ_BACK = ("Only the edge device reads the inverter's settings back: a capture sent here was typed in or taken from a photograph of the screen, "
                        "so say which")


@router.post("/api/sites/{site_id}/native")
def native_post(site_id: str, inp: dict = Body(...)):
    auth.need("edit_sites", "Capturing the native settings")
    site_or_404(site_id)
    if isinstance(inp, dict) and inp.get("source") == "read_back":
        raise Refusal(422, NATIVE_NOT_READ_BACK, "invalid")
    _native_slots_fit(site_id, inp)
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


def _off_grid(site: dict, conn: dict | None) -> bool:
    """Off-grid as the site's own fact (tab C and POST /activate read it so, _grid_settled): a connection of regime off_grid, or none at a farm or at a
    site whose data says off_grid."""
    return conn["data"].get("regime") == "off_grid" if conn is not None else _grid_settled(site, [])


def _today_kpis(site: dict, td: dict | None, bs: dict | None, now: int, day: date) -> dict:
    """The Overview's KPI tiles of the plan (R8 2.4) for today in Pakistan time: the share of the solar made that was used on site (not exported), the
    battery's equivalent full cycles (its discharge over the pack's capacity at its present health, nameplate times state of health, as the battery
    page counts them) and the outages with how many of them the site rode through with no critical load unserved. A figure without its data (no
    reading today, no solar yet, no battery, an off-grid site's outages) is None, never a zero (audit pages-A-09)."""
    td = td or {}
    pv, exp, dis = td.get("pv_kwh"), td.get("export_kwh"), td.get("discharge_kwh")
    self_pct = round(100 * min(1.0, max(0.0, (pv - (exp or 0)) / pv)), 1) if pv else None
    e_nom = (bs or {}).get("e_nom_kwh")
    cycles = round(dis / e_nom, 2) if dis is not None and e_nom else None
    outages = covered = None
    if td and not _off_grid(site, (site.get("connections") or [None])[0]):
        log = telemetry.outages(site["id"], int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp()), now)
        outages, covered = len(log), sum(1 for o in log if not o["unserved_kwh"])
    return {"pv_self_pct": self_pct, "battery_cycles": cycles, "outages": outages, "outages_covered": covered}


def _waiting_genset_requests(site_id: str, limit: int = 3) -> list[dict]:
    """The genset start requests nobody has answered yet, newest first: the Overview's "Log the genset start" quick action answers them as the genset
    page does (POST /sites/:id/genset/requests/:nid; audit pages-A-09)."""
    return [r for r in alerts.requests_for(site_id) if r["status"] == "pending" and not r.get("outcome")][:limit]


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
    mtd, computed, spark, _ = _mtd_saving(site_id)
    a = s.get("assets") or {}
    caps = {"pv_kw": (a.get("pv") or {}).get("kw_ac"), "battery_kw": (a.get("battery") or {}).get("p_dis_kw"), "genset_kw": (a.get("genset") or {}).get("rated_kw"),
            "grid_kw": (s["connections"][0]["data"]["sanctioned_load_kw"] if s.get("connections") else None)}
    bs = battery.state(s, now)
    sum_ = lambda k: round(sum((d.get(k) or 0) for d in tdays), 1)
    from . import verify
    # the month's litres as the Savings page reads them, with their source (meter or curve; audit R7 pages-A-06: the overview said "estimated" always)
    fuel = verify.litres_used(site_id, t.replace(day=1).isoformat(), t.isoformat()) if a.get("genset") else {"litres": 0.0, "litres_source": None}
    return out({"site": {k: s[k] for k in ("id", "name", "tier", "status", "demo_link")}, "control": control_state(s, last, now), "now": _snapshot(last),
                "caps": caps, "now_next": plans.now_and_next(plan, now), "plan_status": (plan or {}).get("summary", {}).get("status_label"),
                "stage": (plan or {}).get("summary", {}).get("stage"), "today": td,
                "month": {"import_kwh": sum_("import_kwh"), "pv_kwh": sum_("pv_kwh"), "genset_hours": sum_("genset_hours"), "litres": fuel["litres"],
                          "litres_source": fuel["litres_source"],
                          "outage_hours": round(sum((d.get("outage_minutes") or 0) for d in tdays) / 60, 1),
                          "peak_kw": max([d.get("peak_kw") or 0 for d in tdays] or [0]), "saving_rs": mtd,
                          "saving_rs_method": MTD_SAVING_METHOD, "saving_rs_computed": computed,
                          # the month's native-block days, which claim nothing (not money; round 7, audit flow-savings-pilot-bill-18)
                          "native_block_days": _month_native_block_days(site_id, t)},
                # the Battery tile's SoC is battery.state's (the last minute with a reading), marked by its own source, not the newest row's (now.soc_source)
                "spark": spark, "battery": {**{k: bs.get(k) for k in ("soc_pct", "sigma_pct", "trust", "usable_kwh")},
                                            "soc_source": telemetry.soc_source(site_id, bs)} if bs else None,
                "alerts": alerts.listing("active", site_id)[:5], "last_data": last["ts"] if last else None,
                "first_data": telemetry.first_ts(site_id), "today_kpis": _today_kpis(s, td, bs, now, t),
                "genset_requests": _waiting_genset_requests(site_id)})


@router.get("/api/sites/{site_id}/overview/chart")
def overview_chart(site_id: str, hours: int = 24):
    """The actual-versus-plan chart: the last day's 15-minute actuals and the active plan's steps for the next day."""
    s = full_or_404(site_id)
    now = now_ts()
    hours = max(6, min(int(hours), 72))
    actual = telemetry.series(site_id, now - hours * 3600, now, ["pv_power_w", "load_power_w", "grid_power_w", "battery_power_w", "battery_soc_est_pct",
                                                                   "genset_power_w", "grid_present", "battery_soc_bms_pct"], "15m")
    # the chart's SoC line is the estimate, and the BMS's reading where no estimate was sent (BMS-only sources would otherwise show no SoC)
    ser = actual.get("series") or {}
    est, bms = ser.get("battery_soc_est_pct"), ser.get("battery_soc_bms_pct")
    if est is not None and bms is not None:
        ser["battery_soc_est_pct"] = [e if e is not None else b for e, b in zip(est, bms)]
    plan = plans.current(site_id)
    steps = [st for st in (plan or {}).get("steps", []) if st["t"] < now + hours * 3600]
    # the TOU peak window of each actual step, as the plan's steps carry theirs, for the chart's shading (R8 2.4; audit pages-A-09); none off-grid
    conn = (s.get("connections") or [None])[0]
    ts = actual.get("ts") or []
    peak = [False] * len(ts)
    if ts and conn is not None and not _off_grid(s, conn):
        try:
            peak = [bool(x) for x in plans.peak_mask(conn, np.asarray(ts, dtype=np.int64))]
        except (LookupError, ValueError):
            pass                                      # a tariff the calendar cannot read: the chart goes without its shading, never a 500
    return out({"actual": actual, "plan": steps, "now": now, "peak": peak})


#: The drafts `GET /api/drafts` lists, newest first
DRAFTS_LISTED = 50


def _draft_owner() -> str:
    """Whose drafts the caller's are: the member's sign-in id, not the display name two members of one company may share (auth.actor() where a caller
    has no id). Drafts stored under the display name before they were kept by id are taken over once (`_claim_named_drafts`)."""
    uid = str((auth.current.get() or {}).get("id") or "")
    if not uid:
        return auth.actor()
    _claim_named_drafts(uid, auth.actor())
    return uid


def _claim_named_drafts(uid: str, name: str) -> None:
    """Drafts stored under the member's display name (round 3 and before) become the member's own, when exactly one member of the company bears that
    name and the name is no member's id. When two members share it nobody can tell whose they are, so they stay with the name and are listed to neither."""
    if not name or name == uid:
        return
    with store.connect() as con:
        if con.execute("SELECT 1 FROM site_drafts WHERE person = ? LIMIT 1", (name,)).fetchone() is None:
            return
    people = store_members.members()
    if any(m["user_id"] == name for m in people) or [m["user_id"] for m in people if m.get("name") == name] != [uid]:
        return
    with store.connect() as con:
        con.execute("UPDATE site_drafts SET person = ? WHERE person = ?", (uid, name))


@router.get("/api/drafts")
def drafts_list():
    """The caller's own wizard drafts, newest first ({draft_id, site_id, body, updated}), so "Add a site" can offer to resume one."""
    owner = _draft_owner()
    with store.connect() as con:
        rows = con.execute("SELECT draft_id, site_id, body, updated FROM site_drafts WHERE person = ? ORDER BY updated DESC LIMIT ?",
                           (owner, DRAFTS_LISTED)).fetchall()
    return [{**dict(r), "body": json.loads(r["body"])} for r in rows]


@router.get("/api/drafts/{draft_id}")
def draft_get(draft_id: str):
    return sites.draft_get(_draft_owner(), draft_id) or {}


@router.put("/api/drafts/{draft_id}")
def draft_put(draft_id: str, inp: dict = Body(...), site_id: str | None = None):
    """Saves the caller's draft (the body is the wizard's whole form). An optional `site_id` (the query parameter, or `site_id` in the body) records the
    site the draft made, once it exists; a draft saved without one keeps the site it has. A query `site_id` that names no site is refused (422); the
    body's is the form's own field, which the wizard sends in every autosave, so one that names no site (deleted meanwhile) is not recorded and the
    save stands. A draft id another member's draft holds is refused (409), never written over; the check and the write are one write transaction."""
    auth.need("manage_sites", "Saving a draft")
    if len(json.dumps(inp)) > 200_000:
        raise Refusal(422, "The draft is too large", "invalid")
    if site_id:
        if sites.get(site_id) is None:
            raise Refusal(422, "The draft's site_id names no site", "invalid")
        link = site_id
    else:
        named = inp.get("site_id")
        link = named if isinstance(named, str) and named and sites.get(named) is not None else None
    owner = _draft_owner()
    with store.write_transaction() as con:
        r = con.execute("SELECT person FROM site_drafts WHERE draft_id = ?", (draft_id,)).fetchone()
        if r is not None and r["person"] != owner:
            raise Refusal(409, "Another person's draft has this id: save the draft under a new id", "draft_taken")
        con.execute("INSERT INTO site_drafts (draft_id, site_id, person, body, updated) VALUES (?,?,?,?,?) ON CONFLICT(draft_id) DO UPDATE SET "
                    "body = excluded.body, updated = excluded.updated, site_id = COALESCE(excluded.site_id, site_drafts.site_id)",
                    (draft_id, link, owner, json.dumps(inp), store._now()))
    return sites.draft_get(owner, draft_id)


@router.delete("/api/drafts/{draft_id}")
def draft_delete(draft_id: str):
    """Discards the caller's own draft ("Add a site" offers it next to Resume). Another member's draft, or none, answers 404: a draft is private to
    the person who started it."""
    auth.need("manage_sites", "Discarding a draft")
    owner = _draft_owner()
    with store.write_transaction() as con:
        n = con.execute("DELETE FROM site_drafts WHERE draft_id = ? AND person = ?", (draft_id, owner)).rowcount
    if not n:
        raise Refusal(404, "You have no draft with this id", "not_found")
    return {"deleted": draft_id}


@router.get("/api/sites/{site_id}/prices")
def site_prices(site_id: str, day: str | None = None, connection: str | None = None):
    """The day's 15-minute price vector on the site's bearable-cost basis, as the optimiser sees it (the wizard and tariff pages draw it), for the
    site's first connection or the one `connection` names (404 for one the site does not have); `connection_id` says which.
    At an ICP site the billing month's imports per period settle each period's case, as the savings ledger prices them (verify.month_totals, the
    metered minutes scaled up to the whole month, with no swing): an above-baseline period shows the ICP rate, not the TOU rate. At a legacy
    net-metered site the month's exports per period are netted first, as the ledger does."""
    auth.need("see_costs", "Seeing prices")
    s = full_or_404(site_id)
    from .routes_common import parse_day
    d = parse_day(day, today())
    # the day lies between 2015 (the earliest telemetry and rate cards) and a year from today: a day outside has no prices, and one before 1970 or past
    # about the year 3000 has no local time on Windows (datetime.fromtimestamp raises OSError there)
    if not PRICES_EARLIEST <= d <= today() + timedelta(days=PRICES_AHEAD_DAYS):
        raise Refusal(422, f"Prices are shown for days from {PRICES_EARLIEST.isoformat()} to a year from today", "invalid")
    if connection:
        conn = next((c for c in s.get("connections") or [] if c["id"] == connection), None)
        if conn is None:
            raise Refusal(404, "This site has no such connection", "not_found")
    else:
        conn = (s.get("connections") or [None])[0]
    t0 = datetime(d.year, d.month, d.day, tzinfo=PKT)
    month_q = month_x = None
    try:
        if conn is not None and conn["data"].get("regime") != "off_grid":
            from . import verify
            tot = verify.month_totals(site_id, conn, d)
            month_q = tot["import"]
            if conn["data"].get("regime") == "net_metering_legacy":
                month_x = tot["export"]
        v = tariffs.vectors(conn, t0.astimezone(timezone.utc), 96, month_q, None, 0.0 if month_q is not None else 1e9, month_x, None)
    except (LookupError, ValueError) as exc:
        raise bad(exc) from None
    except (OSError, OverflowError):
        raise Refusal(422, "That day has no prices", "invalid") from None
    return {"day": d.isoformat(), "connection_id": conn["id"] if conn else None, "t": [int(t0.timestamp()) + 900 * i for i in range(96)], "c_imp": [round(float(x), 2) for x in v["c_imp"]],
            "c_exp": [round(float(x), 2) for x in v["c_exp"]], "peak": [bool(x) for x in v["peak"]], "mdi": v["mdi"],
            "icp": {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv) for kk, vv in val.items()} for k, val in v["icp"].items()},
            "energy_factor": round(tariffs.energy_factor(conn, d), 4) if conn else None,
            "month_import_kwh": {k: round(float(x), 1) for k, x in month_q.items()} if month_q is not None else None,
            "month_export_kwh": {k: round(float(x), 1) for k, x in month_x.items()} if month_x is not None else None,
            "tariff_notices": v.get("tariff_notices", [])}  # a version awaiting its notification the day reaches (tariffs, engine-tariffs-06)
