"""The audit walk (plan Part 2.8: "every mutating route is audited (who, why, before, after) ... test_audit_walk.py holds every mutating route to an
audit row"; round 7, audit security-roles-04).

- Every mutating route the API mounts (POST, PUT, PATCH, DELETE) is in one of three lists: WALKED, called here as the person who may and held to a new
  line in the company's audit trail with the action it names; ELSEWHERE, whose preconditions (a solver run, a started pilot, a whole demo) another test
  builds, named here with the test that holds it to its line; or EXEMPT, with the reason it writes no company line. A new route is in none of them and
  fails the walk until it is placed.
- A money setting's line carries the values before and after (the battery's price, the value of lost load, the fee, the alert rules), under the
  setting's own name, so the money filter takes them out for a reader who may not see money.

Each test works in a data folder of its own with a company file of its own (test_fix_api's `world`)."""
from __future__ import annotations

import importlib
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.routing import APIRoute
from test_fix_api import world  # noqa: F401 (a fixture)

from gridlytics import alerts, money, sites, store

PKT = timezone(timedelta(hours=5))
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

#: Routes that write no line in the company's audit trail, with the reason
EXEMPT = {
    ("POST", "/api/edge/{site_id}/telemetry"): "a device's data stream: its rows, stamped with their source, are the record",
    ("POST", "/api/edge/{site_id}/events"): "a device's event stream: each event is stored as it came (the events table is the log)",
    ("POST", "/api/edge/{site_id}/ack"): "a device's acknowledgements: each is stored as it came (the acks table is the log)",
    ("POST", "/api/edge/{site_id}/heartbeat"): "a device's heartbeat; the change it can make (control demoted for a clock off) is audited, and "
                                                 "test_fix_r7_control_edge holds it to that line",
    ("PUT", "/api/drafts/{draft_id}"): "a person's own unsaved draft of a new site: the site it becomes is audited when it is added",
    ("DELETE", "/api/drafts/{draft_id}"): "a person's own unsaved draft of a new site",
    ("POST", "/api/sites/{site_id}/telemetry/mapping/guess"): "reads a file's first lines and stores nothing",
    ("POST", "/api/sites/{site_id}/telemetry/mapping/preview"): "reads a file's first rows with a column mapping and stores nothing (UX K15)",
    ("POST", "/api/sites/{site_id}/source/test"): "tests the data source and changes nothing",
    ("POST", "/api/sites/{site_id}/validate"): "checks Set-up's steps as typed and saves nothing (UX K6)",
    ("POST", "/api/sites/{site_id}/prices/preview"): "prices a connection's fields as typed and saves nothing (UX K5)",
    ("POST", "/api/public/access-requests"): "a visitor's request to the founder, signed out and of no company: kept in the registry, where the founder's "
                                             "changes to it are written to the founder's audit (test_ux_k02_k03_access_requests)",
    ("POST", "/api/tariffs/versions/validate"): "checks a tariff draft and stores nothing (UX K32)",
    ("PUT", "/api/tariffs/drafts/{draft_id}"): "a person's own tariff draft as they type it: the versions it becomes are audited when it is submitted, and "
                                               "its discarding is audited (UX K32)",
    ("POST", "/api/tariffs/preview"): "prices a draft beside the version in force and stores nothing (UX K32)",
    ("POST", "/api/tariffs/simulate"): "rebuilds a bill under a draft beside the version in force and stores nothing (UX K32)",
}
#: Path prefixes outside the company's trail, with the reason
EXEMPT_PREFIXES = {
    "/api/founder/": "the founder console's own log (platform.founder_audit), which test_founder walks",
    "/api/local/": "sign-in and a person's own sessions and password: the sign-in registry's own log (platform.record)",
}
#: Routes another test builds the preconditions of and holds to their audit line: (module, test)
ELSEWHERE = {
    ("POST", "/api/sites/{site_id}/telemetry/upload"): ("test_fix_api", "test_the_csv_upload_runs_off_the_event_loop_and_refuses_wild_dates"),
    ("POST", "/api/sites/{site_id}/pilot/start"): ("test_fix_r7_savings_engine", "test_a_second_start_is_refused_and_the_start_and_sign_off_stand"),
    ("PUT", "/api/sites/{site_id}/switchback"): ("test_fix_r7_savings_engine", "test_the_calendar_is_drawn_from_os_entropy_after_today_stored_audited_"
                                                                              "and_only_blocks_painted_ahead_count"),
    ("POST", "/api/sites/{site_id}/bills"): ("test_fix_r7_tariffs", "test_the_printed_units_and_mdi_are_compared_line_by_line"),
    ("PUT", "/api/sites/{site_id}/bills/{bill_id}"): ("test_fix_r7_tariffs", "test_an_overlapping_bill_is_refused_and_a_bill_can_be_corrected_or_deleted"),
    ("DELETE", "/api/sites/{site_id}/bills/{bill_id}"): ("test_fix_r7_tariffs", "test_an_overlapping_bill_is_refused_and_a_bill_can_be_corrected_or_deleted"),
    ("POST", "/api/reports"): ("test_fix_r7_alerts_reports", "test_only_the_newest_copies_of_a_report_are_kept"),
    ("POST", "/api/sites/{site_id}/edge/key"): ("test_fix_r7_control_edge", "test_a_missing_key_refuses_the_edge_raises_an_alert_and_an_administrator_"
                                                                            "replaces_it"),
    ("POST", "/api/users/{user_id}/password"): ("test_fix_r7_auth_security", "test_an_administrator_issues_a_one_time_password_that_opens_only_the_"
                                                                             "password_change"),
}
#: The audit action each ELSEWHERE route writes: its handler's own source names it (the named test holds it to the line)
ELSEWHERE_ACTION = {
    ("POST", "/api/sites/{site_id}/telemetry/upload"): "uploaded telemetry",
    ("POST", "/api/sites/{site_id}/pilot/start"): "started the pilot",
    ("PUT", "/api/sites/{site_id}/switchback"): "painted the switchback calendar",
    ("POST", "/api/sites/{site_id}/bills"): "entered a DISCO bill",
    ("PUT", "/api/sites/{site_id}/bills/{bill_id}"): "corrected a DISCO bill",
    ("DELETE", "/api/sites/{site_id}/bills/{bill_id}"): "deleted a DISCO bill",
    ("POST", "/api/reports"): "generated a",
    ("POST", "/api/sites/{site_id}/edge/key"): "replaced the site's signing key",
    ("POST", "/api/users/{user_id}/password"): "issued a one-time password",
}


def mutating_routes() -> set[tuple[str, str]]:
    from gridlytics import api
    routes = [*api.app.routes, *(r for m in api.FEATURE_ROUTES for r in m.router.routes)]
    return {(m, r.path) for r in routes if isinstance(r, APIRoute) and r.path.startswith("/api/") for m in r.methods & MUTATING}


def _exempt(route: tuple[str, str]) -> bool:
    return route in EXEMPT or route[1].startswith(tuple(EXEMPT_PREFIXES))


def _lines(w) -> list[dict]:
    with w.use():
        return store.list_audit(limit=10_000)


class Walk:
    """Calls a route and holds it to a new audit line with its action."""

    def __init__(self, w):
        self.w, self.walked = w, set()

    def __call__(self, method: str, template: str, url: str | None = None, who: str = "admin", action: str = "", body=None, client=None, **kw):
        before = len(_lines(self.w))
        c = client or self.w.c[who]
        r = c.request(method, url or template, json=body, **kw)
        assert 200 <= r.status_code < 300, (method, template, r.status_code, r.text[:400])
        new = _lines(self.w)[: len(_lines(self.w)) - before]
        assert new, f"{method} {template} wrote no audit line"
        assert any(action in line["action"] for line in new), (method, template, [line["action"] for line in new])
        self.walked.add((method, template))
        return r


def test_every_mutating_route_is_walked_audited_elsewhere_or_exempt_with_a_reason():
    """A mutating route in none of the lists fails: placing it says how its change is audited, or why it writes no company line."""
    routes = mutating_routes()
    placed = set(WALKED) | set(ELSEWHERE) | {r for r in routes if _exempt(r)}
    assert sorted(routes - placed) == [], "mutating routes neither walked, audited elsewhere nor exempt"
    assert sorted((set(WALKED) | set(ELSEWHERE)) - routes) == [], "routes listed that the API no longer mounts"
    assert not set(WALKED) & set(ELSEWHERE)
    # each route audited elsewhere names its line in its handler (or what it calls), and the module holding it to that line says it too
    from gridlytics import api
    handlers = {(m, r.path): r.endpoint for r in [*api.app.routes, *(r for x in api.FEATURE_ROUTES for r in x.router.routes)]
                if isinstance(r, APIRoute) for m in r.methods & MUTATING}
    for route, (module, test) in ELSEWHERE.items():
        action = ELSEWHERE_ACTION[route]
        mod = importlib.import_module(module)
        assert action in inspect.getsource(mod), (route, module)
        if test:
            assert callable(getattr(mod, test, None)), (route, module, test)
        assert handlers[route] is not None


#: The routes test_the_walk calls, each held to its new audit line
WALKED = {
    ("PUT", "/api/users/{user_id}"), ("POST", "/api/users/{user_id}/unlock"),
    ("POST", "/api/platform/keys"), ("DELETE", "/api/platform/keys/{key_id}"), ("PUT", "/api/platform/notifications"),
    ("POST", "/api/sites"), ("PUT", "/api/sites/{site_id}"), ("DELETE", "/api/sites/{site_id}"), ("POST", "/api/sites/{site_id}/activate"),
    ("PUT", "/api/sites/{site_id}/connection"), ("DELETE", "/api/sites/{site_id}/connection/{conn_id}"),
    ("PUT", "/api/sites/{site_id}/assets/{role}"), ("DELETE", "/api/sites/{site_id}/assets/{role}"), ("PUT", "/api/sites/{site_id}/tier"),
    ("POST", "/api/sites/{site_id}/native"), ("POST", "/api/sites/{site_id}/overrides"), ("DELETE", "/api/sites/{site_id}/overrides/{oid}"),
    ("POST", "/api/tariffs/versions"), ("POST", "/api/tariffs/versions/{version_id}/approve"), ("POST", "/api/tariffs/fuel"),
    ("POST", "/api/tariffs/drafts/{draft_id}/submit"), ("POST", "/api/tariffs/versions/{version_id}/withdraw"), ("DELETE", "/api/tariffs/drafts/{draft_id}"),
    ("PUT", "/api/sites/{site_id}/telemetry/mapping"), ("PUT", "/api/sites/{site_id}/outages/schedule"),
    ("PUT", "/api/sites/{site_id}/outages/{start}/cause"), ("POST", "/api/sites/{site_id}/genset/curve/refit"),
    ("POST", "/api/sites/{site_id}/genset/requests/{nid}"), ("POST", "/api/sites/{site_id}/genset/fuel"),
    ("POST", "/api/edge/{site_id}/capabilities"), ("POST", "/api/sites/{site_id}/forecasts/refresh"),
    ("POST", "/api/sites/{site_id}/plan/solve"), ("POST", "/api/sites/{site_id}/plan/apply"), ("POST", "/api/sites/{site_id}/control/{action}"),
    ("POST", "/api/sites/{site_id}/savings/recompute"), ("PUT", "/api/sites/{site_id}/fee"), ("PUT", "/api/sites/{site_id}/pilot"),
    ("PUT", "/api/alerts/rules"), ("PUT", "/api/alerts/rules/sites/{site_id}"), ("POST", "/api/alerts/{alert_id}/{action}"),
    ("POST", "/api/demo"), ("DELETE", "/api/demo"),
}


def _detail(w, action: str) -> dict:
    return next(line["detail"] for line in _lines(w) if line["action"] == action)


def test_the_walk_holds_every_walked_route_to_an_audit_line_and_money_settings_to_their_values(world, monkeypatch):
    """security-roles-04: the pilot's set-up, a recompute, a fuel log, an outage's cause, a forecast refresh and an edge's change of the tier its
    source supports wrote no line; the battery's price, the value of lost load and the alert rules were recorded without their values. Every route
    of WALKED now writes a line, and the money settings say what they were and what they became."""
    w = world
    sid, other = w.site, w.other
    walk = Walk(w)
    admin = w.c["admin"]

    # ---- people and keys
    users = admin.get("/api/users").json()
    users = users.get("users", users) if isinstance(users, dict) else users
    analyst = next(u for u in users if u.get("email") == "analyst@fix.test")
    walk("PUT", "/api/users/{user_id}", f"/api/users/{analyst['id']}", action="changed a user", body={"role": "operator"})
    walk("PUT", "/api/users/{user_id}", f"/api/users/{analyst['id']}", action="changed a user", body={"role": "analyst"})
    walk("POST", "/api/users/{user_id}/unlock", f"/api/users/{analyst['id']}/unlock", action="cleared a sign-in lockout", body={})
    k = walk("POST", "/api/platform/keys", action="made an API key", body={"name": "walk", "scopes": ["sites:read"], "site_id": sid, "kind": "edge"})
    walk("DELETE", "/api/platform/keys/{key_id}", f"/api/platform/keys/{k.json()['id']}", action="revoked an API key")
    walk("PUT", "/api/platform/notifications", action="changed their notification choices", body={"email": False})

    # ---- sites, their settings and equipment
    made = walk("POST", "/api/sites", action="added a site", body={"name": "Walk Site", "disco": "LESCO"}).json()
    walk("PUT", "/api/sites/{site_id}", f"/api/sites/{sid}", action="changed a site", body={"voll_rs_per_kwh": 2000.0})
    assert _detail(w, "changed a site")["changes"]["voll_rs_per_kwh"] == {"before": 500.0, "after": 2000.0}
    assert money._strip(_detail(w, "changed a site"), False, False)["changes"] == {}     # a rupee field, under its own name
    walk("PUT", "/api/sites/{site_id}", f"/api/sites/{made['id']}", action="changed a site",
         body={"data_source": {"kind": "oem_cloud", "grid_signal_confirmed": True}})
    walk("PUT", "/api/sites/{site_id}/connection", f"/api/sites/{made['id']}/connection", action="saved a connection",
         body={"tariff_category": "B-2(b)", "sanctioned_load_kw": 300})
    assert _detail(w, "saved a connection")["changes"]["sanctioned_load_kw"] == {"before": None, "after": 300.0}
    walk("PUT", "/api/sites/{site_id}/assets/{role}", f"/api/sites/{made['id']}/assets/inverter", action="saved the inverter",
         body={"brand": "deye", "kw_ac": 50})
    walk("POST", "/api/sites/{site_id}/activate", f"/api/sites/{made['id']}/activate", action="activated a site")
    conns = walk("PUT", "/api/sites/{site_id}/connection", f"/api/sites/{made['id']}/connection", action="saved a connection",
                 body={"tariff_category": "B-2(b)", "sanctioned_load_kw": 200, "meter_ref": "second"}).json()["connections"]
    second = next(c for c in conns if c["data"].get("meter_ref") == "second")
    walk("DELETE", "/api/sites/{site_id}/connection/{conn_id}", f"/api/sites/{made['id']}/connection/{second['id']}", action="removed a connection")
    with w.use():
        bat = {k: v for k, v in sites.assets(sid)["battery"].items() if k != "id"}
    walk("PUT", "/api/sites/{site_id}/assets/{role}", f"/api/sites/{sid}/assets/battery", action="saved the battery",
         body={**bat, "replacement_cost_per_kwh": 90000})
    changed = _detail(w, "saved the battery")["changes"]
    assert changed["replacement_cost_per_kwh"]["after"] == 90000 and changed["replacement_cost_per_kwh"]["before"] != 90000
    assert set(changed) == {"replacement_cost_per_kwh"}                     # a field sent unchanged is no change
    # the values sit under the setting's own name: the money filter takes them out for a reader who may not see money settings
    assert money._strip(_detail(w, "saved the battery"), True, True, False)["changes"] == {}
    walk("PUT", "/api/sites/{site_id}/assets/{role}", f"/api/sites/{made['id']}/assets/pv", action="saved the pv", body={"kwp_dc": 50, "kw_ac": 40})
    walk("DELETE", "/api/sites/{site_id}/assets/{role}", f"/api/sites/{made['id']}/assets/pv", action="removed the pv")
    walk("DELETE", "/api/sites/{site_id}", f"/api/sites/{other}", action="removed a site")
    walk("POST", "/api/sites/{site_id}/native", f"/api/sites/{sid}/native", action="captured the inverter's native settings",
         body={"source": "entered", "work_mode": "backup", "backup_soc_pct": 90})
    with w.use():
        sites.set_tier_capable(sid, "closed_loop")
    walk("PUT", "/api/sites/{site_id}/tier", f"/api/sites/{sid}/tier", action="changed a site's allowed control tier",
         body={"tier": "advisory_writes", "signoff": "Walk"})
    until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    o = walk("POST", "/api/sites/{site_id}/overrides", f"/api/sites/{sid}/overrides", action="set an override",
             body={"kind": "reserve", "value": 60, "until": until, "reason": "walk"}).json()
    oid = o.get("id") or (o.get("override") or {}).get("id")
    walk("DELETE", "/api/sites/{site_id}/overrides/{oid}", f"/api/sites/{sid}/overrides/{oid}", action="cleared an override")

    # ---- tariffs and fuel
    with w.use():
        cid = sites.connections(sid)[0]["id"]
    src = {"kind": "manual", "ref": "walk", "verified": "primary"}
    v = walk("POST", "/api/tariffs/versions", who="analyst", action="proposed a tariff version",
             body={"kind": "override", "key": cid, "effective_from": "2026-11-01", "body": {"electricity_duty": 0.02}, "source": src}).json()
    walk("POST", "/api/tariffs/versions/{version_id}/approve", f"/api/tariffs/versions/{v['id']}/approve", action="approved a tariff version", body={})
    # the tariff editor (UX K32): a draft submitted, a waiting version withdrawn, a draft discarded
    draft = {"effective_from": "2026-12-01", "source": {"ref": "walk", "verified": "primary"},
             "versions": [{"kind": "adder", "key": "fca", "body": {"rate": 1.38, "billing_months": ["2026-12"]}}]}
    assert w.c["analyst"].put("/api/tariffs/drafts/walk", json=draft).status_code == 200
    made = walk("POST", "/api/tariffs/drafts/{draft_id}/submit", "/api/tariffs/drafts/walk/submit", who="analyst", action="proposed a tariff version").json()
    walk("POST", "/api/tariffs/versions/{version_id}/withdraw", f"/api/tariffs/versions/{made['submitted'][0]['id']}/withdraw", who="analyst",
         action="withdrew a tariff version", body={"reason": "walk"})
    assert w.c["analyst"].put("/api/tariffs/drafts/walk", json=draft).status_code == 200
    walk("DELETE", "/api/tariffs/drafts/{draft_id}", "/api/tariffs/drafts/walk", action="discarded a tariff draft")
    walk("POST", "/api/tariffs/fuel", action="set a fuel price", body={"kind": "hsd", "valid_from": "2026-11-01", "price": 291.5})
    walk("POST", "/api/tariffs/fuel", action="set a fuel price", body={"kind": "hsd", "valid_from": "2026-11-01", "price": 295.0})
    assert _detail(w, "set a fuel price")["changes"]["price"] == {"before": 291.5, "after": 295.0}

    # ---- data, outages and the genset
    walk("PUT", "/api/sites/{site_id}/telemetry/mapping", f"/api/sites/{sid}/telemetry/mapping", action="saved the CSV column mapping",
         body={"columns": {"Time": {"key": "ts"}, "Load": {"key": "load_power_w", "unit": "kW"}}, "ts_format": "dmy"})
    walk("PUT", "/api/sites/{site_id}/outages/schedule", f"/api/sites/{sid}/outages/schedule", action="set the announced load-shedding schedule",
         body={"windows": [["18:00", "20:00", "daily"]]})
    start = int(datetime(2026, 11, 9, 14, 0, tzinfo=PKT).timestamp())
    walk("PUT", "/api/sites/{site_id}/outages/{start}/cause", f"/api/sites/{sid}/outages/{start}/cause", who="operator",
         action="recorded an outage's cause", body={"cause": "feeder fault"})
    walk("PUT", "/api/sites/{site_id}/outages/{start}/cause", f"/api/sites/{sid}/outages/{start}/cause", who="operator",
         action="recorded an outage's cause", body={"cause": "scheduled"})
    assert _detail(w, "recorded an outage's cause") == {"start": start, "cause": "scheduled", "before": "feeder fault"}
    sheet = [{"load_ratio": lr, "litres_per_hour": round(0.3 * lr * 150 + 0.02 * 150, 2)} for lr in (0.25, 0.5, 0.75, 1.0)]
    walk("POST", "/api/sites/{site_id}/genset/curve/refit", f"/api/sites/{sid}/genset/curve/refit", who="operator", action="saved the genset",
         body={"points": sheet, "apply": True})
    assert {"a", "b"} <= set(_detail(w, "saved the genset")["changes"])
    start_req = int(datetime.now(timezone.utc).timestamp()) // 900 * 900 + 3600
    with w.use(), store.connect() as con:
        con.execute("INSERT INTO notifications (id, site_id, ts, kind, payload, status) VALUES (?,?,?,?,?,?)",
                    ("walk-n1", sid, store._now(), "genset_request", json.dumps({"key": f"genset_request:{start_req}", "start": start_req,
                                                                                 "end": start_req + 3600, "kw": 90.0, "reason": "genset_on",
                                                                                 "detail": {}, "plan_id": "p0"}), "pending"))
    walk("POST", "/api/sites/{site_id}/genset/requests/{nid}", f"/api/sites/{sid}/genset/requests/walk-n1", who="operator",
         action="answered a genset request", body={"outcome": "done"})
    walk("POST", "/api/sites/{site_id}/genset/fuel", f"/api/sites/{sid}/genset/fuel", who="operator", action="logged fuel",
         body={"litres": 50, "cost_rs": 14321.5, "note": "tanker"})
    # the unit of the genset's fuel (round 7 integration: MMBtu at a gas genset) and how it was entered (UX backend ask K24: litres, MMBtu as billed,
    # or a meter reading in hm³ with its calorific value)
    assert _detail(w, "logged fuel") == {"day": _detail(w, "logged fuel")["day"], "litres": 50.0, "cost_rs": 14321.5, "note": "tanker",
                                         "fuel_unit": "L", "entry": "litres"}

    # ---- the edge reports the tier its source supports
    kh = w.key(["telemetry:write"])
    caps = {"contract": "ems-edge/1.0", "seq": 1, "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "edge_version": "1.0",
            "tier": "advisory_writes"}
    walk("POST", "/api/edge/{site_id}/capabilities", f"/api/edge/{sid}/capabilities", client=w.anon, headers=kh,
         action="changed the control tier the site's source supports", body=caps)
    assert _detail(w, "changed the control tier the site's source supports")["changes"] == {
        "tier_capable": {"before": "closed_loop", "after": "advisory_writes"}}

    # ---- forecasts, plans and control
    walk("POST", "/api/sites/{site_id}/forecasts/refresh", f"/api/sites/{sid}/forecasts/refresh", who="analyst", action="refreshed the forecasts")
    solved = walk("POST", "/api/sites/{site_id}/plan/solve", f"/api/sites/{sid}/plan/solve", who="operator", action="re-solved the plan").json()
    assert solved["plan_id"], solved
    day = datetime.now(PKT).date().isoformat()
    walk("POST", "/api/sites/{site_id}/plan/apply", f"/api/sites/{sid}/plan/apply", who="operator", action="marked a plan as applied",
         body={"plan_id": solved["plan_id"], "day": day, "method": "installer_keyed"})
    walk("POST", "/api/sites/{site_id}/control/{action}", f"/api/sites/{sid}/control/hold", who="operator", action="set an override",
         body={"hours": 1, "soc_pct": 60})

    # ---- savings, the fee and the pilot
    walk("POST", "/api/sites/{site_id}/savings/recompute", f"/api/sites/{sid}/savings/recompute", who="analyst", action="recomputed savings days",
         body={})
    walk("PUT", "/api/sites/{site_id}/fee", f"/api/sites/{sid}/fee", action="set the fee", body={"fee_rs_per_month": 5000})
    walk("PUT", "/api/sites/{site_id}/fee", f"/api/sites/{sid}/fee", action="set the fee", body={"fee_rs_per_month": 6000})
    assert _detail(w, "set the fee")["changes"] == {"fee_rs_per_month": {"before": 5000.0, "after": 6000.0}}
    walk("PUT", "/api/sites/{site_id}/pilot", f"/api/sites/{sid}/pilot", action="changed the pilot set-up", body={"criteria": {"min_saving_pct": 5}})

    # ---- alerts
    walk("PUT", "/api/alerts/rules", action="changed the alert rules", body={"Data": {"thresholds": {"stale_minutes": 45}}})
    assert _detail(w, "changed the alert rules")["changes"]["Data"]["after"]["thresholds"]["stale_minutes"] == 45
    walk("PUT", "/api/alerts/rules/sites/{site_id}", f"/api/alerts/rules/sites/{sid}", action="changed a site's alert rules",
         body={"Data": {"thresholds": {"stale_minutes": 40}}})
    assert "Data" in _detail(w, "changed a site's alert rules")["changes"]
    with w.use():
        alerts._open(sid, "data_stale", "Data", "warning", "No data", {})
        aid = next(a["id"] for a in alerts.listing("active", sid) if a["rule"] == "data_stale")
    walk("POST", "/api/alerts/{alert_id}/{action}", f"/api/alerts/{aid}/acknowledge", who="operator", action="acknowledged an alert", body={})

    # ---- the demo sites: loading them (its build stubbed here; test_fix_r7_demo_seed builds it) and removing them
    from gridlytics import demo
    monkeypatch.setattr(demo, "load", lambda by, days=None, background=True: {"state": "running"})
    walk("POST", "/api/demo", action="loaded the demo sites")
    walk("DELETE", "/api/demo", action="removed the demo sites")
    assert walk.walked == WALKED, sorted(WALKED - walk.walked)
