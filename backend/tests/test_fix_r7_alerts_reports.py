"""Regression tests of the round-7 audit's alerts-and-reports findings: a viewer receives only the energy categories of alerts (security-roles-03,
pages-D-15), the rules the plan lists exist (pages-D-14), a site that never sent data raises a Data alert (pages-D-27), the forecast bust is the plan's
(engine-forecast-10), the tariff library's staleness raises a Tariff alert (brief-vs-build-18), and the rule editor validates quiet hours and keeps
thresholds and per-site overrides that the evaluator reads (pages-D-29).

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own, so no person or row reaches the shared test database."""
from __future__ import annotations

import json
import re
import types
from datetime import datetime, timedelta, timezone

import pytest

from gridlytics import alerts, sites, store, telemetry
from gridlytics import platform as platformdb

PKT = timezone(timedelta(hours=5))
ROLES = ("admin", "operator", "analyst", "viewer")


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    import identities
    co = identities.company("R7 alerts company", own_file=True)
    db = platformdb.company_file(co)
    db.parent.mkdir(parents=True, exist_ok=True)
    with store.use_company(db):
        one = sites.create({"name": "Mill One", "disco": "FESCO", "genset_start_control": "manual", "critical_load_kw": [20.0] * 24}, "test")["id"]
        sites.put_connection(one, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "test")
        sites.put_asset(one, "inverter", {"brand": "deye", "kw_ac": 100}, "test")
        sites.put_asset(one, "battery", {"nameplate_kwh": 200, "p_ch_kw": 100, "p_dis_kw": 100}, "test")
        sites.put_asset(one, "genset", {"rated_kw": 150}, "test")
    clients = {}
    for role in ROLES:
        owner = role == "admin"
        clients[role] = identities.client(f"{role}@r7.test", None if owner else role, co, "owner" if owner else "member", raise_server_exceptions=False)
    w = types.SimpleNamespace(db=db, site=one, c=clients, company=co)
    w.use = lambda: store.use_company(db)
    return w


def _rules_of(rows: list[dict]) -> set[str]:
    return {a["rule"] for a in rows}


# ------------------------------------------------------------------------------------------------ security-roles-03 (and pages-D-15)

#: one open alert of every category, as the evaluator would raise them
EVERY_CATEGORY = {"Data": "data_stale", "Control": "plan_failed", "Reserve": "reserve_short", "Battery": "battery_derate", "Genset": "genset_request_1",
                  "Forecast": "forecast_bust", "Tariff": "regime_expiring_c1", "Savings": "savings_negative", "Product": "fee_above_third"}


def test_a_viewer_receives_only_the_energy_categories_of_alerts(world):
    sid = world.site
    with world.use():
        for cat, rule in EVERY_CATEGORY.items():
            alerts._open(sid, rule, cat, "warning", f"A {cat} alert", {})
        assert len(alerts.listing("active", sid)) == 9          # no reader (the scheduler, a job): every category
    energy = {"Data", "Reserve", "Battery", "Genset", "Forecast"}
    seen = {"viewer": energy, "operator": set(EVERY_CATEGORY) - {"Product"}, "analyst": set(EVERY_CATEGORY), "admin": set(EVERY_CATEGORY)}
    for role, cats in seen.items():
        c = world.c[role]
        inbox = c.get("/api/alerts?state=active").json()
        assert {a["category"] for a in inbox} == cats, role
        counts = c.get("/api/alerts/counts").json()
        assert counts["total"] == len(cats) and counts["by_site"] == {sid: len(cats)}, (role, counts)
        assert set(counts["categories"]) == cats, role
        # a category the reader does not receive is not listed even when asked for by name
        assert len(c.get("/api/alerts?state=active&category=Product").json()) == int("Product" in cats), role
        assert len(c.get("/api/alerts?state=active&category=Tariff").json()) == int("Tariff" in cats), role
        now = c.get(f"/api/sites/{sid}/now").json()
        assert {a["category"] for a in now["alerts"]} <= cats, role
        fleet = c.get("/api/fleet").json()
        assert fleet["summary"]["alerts"] == len(cats), role
        assert next(r for r in fleet["sites"] if r["id"] == sid)["open_alerts"] == len(cats), role
    # acting on an alert of a category the reader does not receive answers as if it did not exist
    with world.use():
        product = next(a for a in alerts.listing("active", sid) if a["category"] == "Product")
    assert world.c["operator"].post(f"/api/alerts/{product['id']}/acknowledge", json={}).status_code == 404


# ------------------------------------------------------------------------------------------------ pages-D-27

def _activated_at(sid: str, ts: int) -> None:
    """The site activated at `ts`, as POST /activate records it (status and its audit line)."""
    sites.set_status(sid, "active")
    with store.connect() as con:
        con.execute("INSERT INTO audit (at, actor, action, target) VALUES (?,?,?,?)",
                    (datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"), "test", "activated a site", sid))


def test_a_site_that_never_sent_data_raises_a_data_alert_after_its_sources_limit(world):
    sid = world.site
    now = _now()
    with world.use():
        _activated_at(sid, now - 20 * 60)
        assert telemetry.latest(sid) is None
        alerts.evaluate(sid, now)                           # 20 minutes after activation: an edge or batch source may take 30
        assert "data_stale" not in _rules_of(alerts.listing("active", sid))
        alerts.evaluate(sid, now + 15 * 60)                 # 35 minutes
        stale = [a for a in alerts.listing("active", sid) if a["rule"] == "data_stale"]
        assert len(stale) == 1 and stale[0]["category"] == "Data" and stale[0]["severity"] == "warning", stale
        assert "No data has arrived" in stale[0]["title"] and stale[0]["detail"]["last_ts"] is None, stale
        alerts.evaluate(sid, now + 3 * 3600)                # past the critical limit
        assert [a["severity"] for a in alerts.listing("active", sid) if a["rule"] == "data_stale"] == ["critical"]
        # the first reading clears it
        telemetry.ingest(sid, [{"ts": (now + 3 * 3600) // 60 * 60 - 60, "grid_present": 1}], "edge")
        alerts.evaluate(sid, now + 3 * 3600)
        assert "data_stale" not in _rules_of(alerts.listing("active", sid))


def test_a_csv_site_that_never_uploaded_waits_a_day(world):
    sid = world.site
    now = _now()
    with world.use():
        sites.update(sid, {"data_source": {"kind": "csv"}}, "test")
        _activated_at(sid, now - 23 * 3600)
        alerts._stale(sid, None, now)
        assert "data_stale" not in _rules_of(alerts.listing("active", sid))
        alerts._stale(sid, None, now + 2 * 3600)
        stale = [a for a in alerts.listing("active", sid) if a["rule"] == "data_stale"]
        assert len(stale) == 1 and "No data has arrived" in stale[0]["title"], stale


# ------------------------------------------------------------------------------------------------ pages-D-29

def test_the_rule_editor_refuses_quiet_hours_that_are_not_two_times_and_keeps_canonical_ones(world):
    admin = world.c["admin"]
    for bad_value in ("whenever", "22:00", "25:00-07:00", "22:00-22:00", "22.00-07.00", 2200):
        r = admin.put("/api/alerts/rules", json={"Data": {"channels": ["in_app"], "quiet_hours": bad_value}})
        assert r.status_code == 422 and "Quiet hours" in r.json()["detail"], (bad_value, r.text)
    r = admin.put("/api/alerts/rules", json={"Data": {"channels": ["in_app", "email"], "quiet_hours": " 22:00 - 07:00 "}})
    assert r.status_code == 200, r.text
    assert r.json()["Data"]["quiet_hours"] == "22:00-07:00" and r.json()["Data"]["channels"] == ["in_app", "email"]
    assert admin.get("/api/alerts/rules").json()["Data"]["quiet_hours"] == "22:00-07:00"
    # escalation and channels are checked too, in one sentence each
    assert admin.put("/api/alerts/rules", json={"Data": {"escalate_after_min": 0}}).status_code == 422
    assert admin.put("/api/alerts/rules", json={"Data": {"escalate_after_min": "soon"}}).status_code == 422
    assert admin.put("/api/alerts/rules", json={"Data": {"channels": ["pager"]}}).status_code == 422
    assert admin.put("/api/alerts/rules", json={"Data": {"colour": "red"}}).status_code == 422
    assert admin.put("/api/alerts/rules", json={"Weather": {}}).status_code == 422
    # quiet hours across midnight, in Karachi time, for the sender to come
    rule = admin.get("/api/alerts/rules").json()["Data"]
    at = lambda hh, mm: int(datetime(2026, 11, 9, hh, mm, tzinfo=PKT).timestamp())  # noqa: E731
    assert alerts.in_quiet_hours(rule, at(23, 30)) and alerts.in_quiet_hours(rule, at(6, 59))
    assert not alerts.in_quiet_hours(rule, at(7, 0)) and not alerts.in_quiet_hours(rule, at(12, 0))


def test_an_old_free_text_quiet_hours_reads_as_none(world):
    with world.use():
        store.set_setting("alert_rules", {"Data": {"channels": ["in_app"], "quiet_hours": "whenever", "escalate_after_min": None}})
    assert world.c["admin"].get("/api/alerts/rules").json()["Data"]["quiet_hours"] is None


def test_thresholds_are_kept_per_company_and_per_site_and_the_evaluator_reads_them(world):
    sid = world.site
    admin = world.c["admin"]
    rules = admin.get("/api/alerts/rules").json()
    assert rules["Data"]["thresholds"]["stale_minutes"] is None              # by the data source
    assert rules["Reserve"]["thresholds"]["unserved_kwh"] == 0.5 and rules["Tariff"]["thresholds"]["agreement_days"] == 60
    # unknown names and values out of range are refused in a sentence
    r = admin.put("/api/alerts/rules", json={"Reserve": {"thresholds": {"colour": 1}}})
    assert r.status_code == 422 and "colour" in r.json()["detail"], r.text
    r = admin.put("/api/alerts/rules", json={"Data": {"thresholds": {"stale_minutes": 1}}})
    assert r.status_code == 422 and "stale minutes" in r.json()["detail"], r.text
    # the company's rule: data stale after 60 minutes at every site
    assert admin.put("/api/alerts/rules", json={"Data": {"thresholds": {"stale_minutes": 60}}}).json()["Data"]["thresholds"]["stale_minutes"] == 60
    now = _now()
    with world.use():
        telemetry.ingest(sid, [{"ts": (now - 45 * 60) // 60 * 60, "grid_present": 1}], "edge")
        alerts.evaluate(sid, now)                          # 45 minutes: past the edge's 30, under the company's 60
        assert "data_stale" not in _rules_of(alerts.listing("active", sid))
    # the site overrides it with 40: only what differs from the company's rules is kept for the site
    r = admin.put(f"/api/alerts/rules/sites/{sid}", json={"Data": {"thresholds": {"stale_minutes": 40}}, "Reserve": rules["Reserve"]})
    assert r.status_code == 200, r.text
    assert r.json()["overrides"] == {"Data": {"thresholds": {"stale_minutes": 40}}}, r.json()
    assert r.json()["rules"]["Data"]["thresholds"]["stale_minutes"] == 40 and r.json()["rules"]["Reserve"] == rules["Reserve"]
    assert admin.get(f"/api/alerts/rules/sites/{sid}").json()["overrides"] == {"Data": {"thresholds": {"stale_minutes": 40}}}
    with world.use():
        alerts.evaluate(sid, now)
        assert "data_stale" in _rules_of(alerts.listing("active", sid))
    # an empty body puts the site back on the company's rules
    assert admin.put(f"/api/alerts/rules/sites/{sid}", json={}).json()["overrides"] == {}
    with world.use():
        alerts.evaluate(sid, now)
        assert "data_stale" not in _rules_of(alerts.listing("active", sid))
    # only an administrator changes them; everyone reads them; an unknown site is 404
    for role in ("operator", "analyst", "viewer"):
        assert world.c[role].put(f"/api/alerts/rules/sites/{sid}", json={}).status_code == 403, role
        assert world.c[role].get(f"/api/alerts/rules/sites/{sid}").status_code == 200, role
    assert admin.get("/api/alerts/rules/sites/nosuchsite").status_code == 404
    assert admin.put("/api/alerts/rules/sites/nosuchsite", json={}).status_code == 404


def test_the_reserve_and_agreement_thresholds_change_what_is_raised(world):
    sid = world.site
    now = _now()
    end = (datetime.fromtimestamp(now, PKT).date() + timedelta(days=90)).isoformat()
    with world.use():
        conn = sites.connections(sid)[0]
        sites.put_connection(sid, {**conn["data"], "regime": "net_billing", "agreement_end": end}, "test", conn_id=conn["id"])
        alerts.evaluate(sid, now)
        assert not any(r.startswith("regime_expiring_") for r in _rules_of(alerts.listing("active", sid)))
        store.set_setting(f"alert_rules:site:{sid}", {"Tariff": {"thresholds": {"agreement_days": 120}}})
        alerts.evaluate(sid, now)
        assert any(r.startswith("regime_expiring_") for r in _rules_of(alerts.listing("active", sid)))


# ------------------------------------------------------------------------------------------------ engine-forecast-10

def _pv_day(sid: str, day, forecast_kw, actual_kw, cs_kw) -> None:
    """A day-ahead PV issue at the day's local midnight (q50 = `forecast_kw`, the clear-sky ceiling `cs_kw`, per 15-minute step) and the measured PV
    (`actual_kw` per 15-minute step, written per minute)."""
    import numpy as np
    from gridlytics import twin
    t0 = twin.day_start_utc(day)
    body = {"cs": [float(x) for x in cs_kw] * 2, **{q: [float(x) for x in forecast_kw] * 2 for q in ("q05", "q10", "q25", "q50", "q75", "q90", "q95")}}
    telemetry.save_forecast(sid, "pv", t0, 1, "test", body)
    rows = [{"ts": t0 + 60 * m, "grid_present": 1, "pv_power_w": float(actual_kw[m // 15]) * 1000, "load_power_w": 30000.0} for m in range(1440)]
    telemetry.ingest(sid, rows, "edge", now=t0 + 86400 + 600)
    _ = np


def _clear_sky_day() -> list[float]:
    """A clear-sky ceiling of a 100 kW plant: zero at night, a half-sine from 06:30 to 17:30."""
    import math
    out = []
    for i in range(96):
        h = i / 4 + 0.125
        out.append(100 * math.sin(math.pi * (h - 6.5) / 11) if 6.5 <= h <= 17.5 else 0.0)
    return out


def test_a_forecast_bust_is_the_plans_and_a_fog_morning_raises_it(world):
    sid = world.site
    day = datetime.now(PKT).date() - timedelta(days=1)
    cs = _clear_sky_day()
    fog = [x * 0.35 if 8 <= i / 4 < 12 else x * 0.95 for i, x in enumerate(cs)]   # a fog morning: four hours at a third of a clear sky
    with world.use():
        sites.put_asset(sid, "pv", {"kwp_dc": 120, "kw_ac": 100}, "test")
        _pv_day(sid, day, [x * 0.95 for x in cs], fog, cs)
        rows = alerts.forecast_bust(sites.full(sid), day)
        assert rows and rows["hours"] >= 3.5 and rows["nrmse_pct"] < 60, rows    # the old rule (daily nRMSE by nameplate > 0.6) never saw it
        alerts.nightly(sid, day)
        bust = [a for a in alerts.listing("active", sid) if a["rule"] == "forecast_bust"]
        assert len(bust) == 1 and bust[0]["category"] == "Forecast", bust
        assert "30 %" in bust[0]["title"] and "4 hours" in bust[0]["title"] and day.strftime("%-d %B") in bust[0]["title"], bust[0]["title"]
        assert "Yesterday" not in bust[0]["title"]
        # a threshold the site sets higher (6 hours) is not reached: the alert clears
        store.set_setting(f"alert_rules:site:{sid}", {"Forecast": {"thresholds": {"bust_hours": 6}}})
        alerts.nightly(sid, day)
        assert "forecast_bust" not in _rules_of(alerts.listing("active", sid))


def test_an_ordinary_day_raises_no_bust(world):
    sid = world.site
    day = datetime.now(PKT).date() - timedelta(days=2)
    cs = _clear_sky_day()
    with world.use():
        sites.put_asset(sid, "pv", {"kwp_dc": 120, "kw_ac": 100}, "test")
        _pv_day(sid, day, [x * 0.9 for x in cs], [x * 0.8 for x in cs], cs)   # off by a tenth of the ceiling all day
        assert alerts.forecast_bust(sites.full(sid), day)["hours"] == 0
        alerts.nightly(sid, day)
        assert "forecast_bust" not in _rules_of(alerts.listing("active", sid))


# ------------------------------------------------------------------------------------------------ pages-D-14

def _minutes(t0: int, t1: int, **cols) -> list[dict]:
    return [{"ts": t, "grid_present": 1, **cols} for t in range(t0 // 60 * 60, t1 // 60 * 60, 60)]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ready(monkeypatch):
    """The site counts as settled (an active plan and 7 days of data), so Control, Savings and Product rules may raise."""
    monkeypatch.setattr(alerts, "settled", lambda site_id, plan, now: True)


def test_a_gap_in_the_data_of_more_than_an_hour_raises_a_data_alert(world):
    sid = world.site
    now = _now() // 60 * 60
    with world.use():
        telemetry.ingest(sid, _minutes(now - 6 * 3600, now - 4 * 3600) + _minutes(now - 90 * 60, now), "edge", now=now)
        alerts.evaluate(sid, now)
        gap = [a for a in alerts.listing("active", sid) if a["rule"] == "data_gap"]
        assert len(gap) == 1 and gap[0]["category"] == "Data" and gap[0]["detail"]["hours"] == 2.5, gap
        assert gap[0]["detail"]["gap_from"] == now - 4 * 3600 - 60 + 60 and gap[0]["detail"]["gap_to"] == now - 90 * 60, gap[0]["detail"]
        # a site whose rule allows three hours keeps quiet
        store.set_setting(f"alert_rules:site:{sid}", {"Data": {"thresholds": {"gap_hours": 3}}})
        alerts.evaluate(sid, now)
        assert "data_gap" not in _rules_of(alerts.listing("active", sid))


def test_the_edges_events_raise_control_and_data_alerts(world, monkeypatch):
    sid = world.site
    now = _now()
    _ready(monkeypatch)
    with world.use():
        telemetry.ingest(sid, _minutes(now - 600, now), "edge", now=now)
        with store.connect() as con:
            for ts, kind in ((now - 3600, "readback_mismatch"), (now - 3000, "clock_unsynced"), (now - 2400, "fallback_engaged"),
                             (now - 1800, "fallback_cleared"), (now - 2 * 86400, "budget_exhausted")):
                con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)", (sid, _iso(ts), kind, "edge", "{}"))
        alerts.evaluate(sid, now)
        got = {a["rule"]: a for a in alerts.listing("active", sid)}
        assert got["edge_readback_mismatch"]["category"] == "Control" and got["edge_clock_unsynced"]["category"] == "Data", got.keys()
        assert "edge_fallback_engaged" not in got                   # cleared by the later fallback_cleared
        assert "edge_budget_exhausted" not in got                   # two days ago: outside the last day
        with store.connect() as con:
            con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)", (sid, _iso(now - 60), "fallback_engaged", "edge", "{}"))
        alerts.evaluate(sid, now)
        assert [a["severity"] for a in alerts.listing("active", sid) if a["rule"] == "edge_fallback_engaged"] == ["critical"]


def test_an_expired_plan_and_a_silent_edge_raise_control_alerts(world, monkeypatch):
    sid = world.site
    now = _now()
    _ready(monkeypatch)
    plan = {"id": "p-old", "valid_until": _iso(now - 600).replace("Z", "+00:00"), "steps": [], "summary": {}}
    monkeypatch.setattr(alerts.plans, "current", lambda site_id: plan)
    with world.use():
        telemetry.ingest(sid, _minutes(now - 600, now), "edge", now=now)
        sites.set_tier_capable(sid, "closed_loop")
        sites.set_tier_allowed(sid, "closed_loop", "test")
        sites.put_state(sid, edge_last_seen=now - 40 * 60)
        alerts.evaluate(sid, now)
        got = {a["rule"]: a for a in alerts.listing("active", sid)}
        assert got["plan_expired"]["category"] == "Control" and got["plan_expired"]["severity"] == "critical", got.keys()
        assert got["edge_offline"]["category"] == "Control" and "40 minutes" in got["edge_offline"]["title"], got.get("edge_offline")
        sites.put_state(sid, edge_last_seen=now - 60)
        plan["valid_until"] = _iso(now + 86400).replace("Z", "+00:00")
        alerts.evaluate(sid, now)
        assert not {"plan_expired", "edge_offline"} & _rules_of(alerts.listing("active", sid))


def test_the_genset_rules_unrequested_low_load_and_a_request_nobody_answered(world, monkeypatch):
    sid = world.site
    now = _now() // 60 * 60
    t0 = now - 3 * 3600
    steps = [{"t": t0 + 900 * i, "genset_on": False, "genset_kw": 0.0, "reason": "idle", "detail": {}, "local": ""} for i in range(16)]
    plan = {"id": "p1", "steps": steps, "summary": {}, "valid_until": _iso(now + 86400).replace("Z", "+00:00")}
    monkeypatch.setattr(alerts.plans, "current", lambda site_id: plan)
    monkeypatch.setattr(alerts.plans, "active_at", lambda site_id, ts: plan)
    with world.use():
        # the grid is up and the plan runs no genset, yet the set ran the last 16 minutes at 20 kW (its minimum load is 30 % of 150 kW)
        telemetry.ingest(sid, _minutes(now - 3 * 3600, now - 16 * 60, genset_running=0)
                         + _minutes(now - 16 * 60, now, genset_running=1, genset_power_w=20000.0), "edge", now=now)
        # a request to start it an hour ago was neither answered nor followed
        start = now - 3600
        with store.connect() as con:
            con.execute("INSERT INTO notifications (id, site_id, ts, kind, payload) VALUES (?,?,?,?,?)",
                        ("n1", sid, store._now(), "genset_request", json.dumps({"key": f"genset_request:{start}", "start": start, "end": start + 3600,
                                                                               "kw": 90.0, "reason": "genset_on", "detail": {}, "plan_id": "p0"})))
        alerts.evaluate(sid, now)
        got = {a["rule"]: a for a in alerts.listing("active", sid)}
        assert got["genset_unrequested"]["category"] == "Genset", got.keys()
        assert got["genset_low_load"]["category"] == "Genset" and "45 kW" in got["genset_low_load"]["title"], got["genset_low_load"]["title"]
        missed = got["genset_request_missed"]
        assert missed["category"] == "Genset" and missed["detail"]["requested_start"] == start, missed
        # answered: no longer missed
        alerts.answer_request(sid, "n1", "not_now", "operator@r7.test")
        alerts.evaluate(sid, now)
        assert "genset_request_missed" not in _rules_of(alerts.listing("active", sid))


def test_a_bill_the_rebuild_misses_by_more_than_the_threshold_raises_a_tariff_alert(world):
    sid = world.site
    now = _now()
    with world.use():
        cid = sites.connections(sid)[0]["id"]
        with store.connect() as con:
            con.execute("INSERT INTO bills (id, site_id, connection_id, period_start, period_end, real, rebuilt, difference_pct, created) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", ("b1", sid, cid, "2026-09-01", "2026-09-30", "{}", "{}", -3.4, store._now()))
        alerts.evaluate(sid, now)
        got = [a for a in alerts.listing("active", sid) if a["rule"] == "bill_residual"]
        assert len(got) == 1 and got[0]["category"] == "Tariff" and "3.4 %" in got[0]["title"], got
        assert got[0]["detail"] == {"period_start": "2026-09-01", "period_end": "2026-09-30", "difference_pct": -3.4}   # no bill id
        with store.connect() as con:
            con.execute("INSERT INTO bills (id, site_id, connection_id, period_start, period_end, real, rebuilt, difference_pct, created) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", ("b2", sid, cid, "2026-10-01", "2026-10-31", "{}", "{}", 1.1, store._now()))
        alerts.evaluate(sid, now)                          # the latest bill is within 2 %
        assert "bill_residual" not in _rules_of(alerts.listing("active", sid))


def test_a_forecast_stage_change_is_said(world):
    sid = world.site
    now = _now() // 900 * 900
    with world.use():
        telemetry.ingest(sid, _minutes(now - 600, now), "edge", now=now)
        telemetry.save_forecast(sid, "load", now - 30 * 3600, 0, "test", {"q50": [1.0]})
        telemetry.save_forecast(sid, "load", now - 900, 1, "test", {"q50": [1.0]})
        alerts.evaluate(sid, now)
        got = [a for a in alerts.listing("active", sid) if a["rule"] == "forecast_stage_load"]
        assert len(got) == 1 and got[0]["category"] == "Forecast" and "stage 0 to stage 1" in got[0]["title"], got


def _savings_row(sid: str, day: str, **data) -> None:
    row = {"saving_rs": 0.0, "claimed_rs": 0.0, "fidelity": "confirmed", "claim_withheld": None, **data}
    with store.connect() as con:
        con.execute("INSERT OR REPLACE INTO savings_days (site_id, day_local, baseline, data, computed) VALUES (?,?,?,?,?)",
                    (sid, day, "native", json.dumps(row), store._now()))


def test_the_savings_and_product_rules_after_the_nightly_jobs(world, monkeypatch):
    sid = world.site
    day = datetime.now(PKT).date() - timedelta(days=1)
    _ready(monkeypatch)
    from gridlytics import verify
    monkeypatch.setattr(verify, "summary", lambda site_id, d0, d1, see_fee=False: {"fee": {"within_one_third": False}})
    with world.use():
        store.set_setting(f"fee:{sid}", 50000.0)
        _savings_row(sid, day.isoformat(), saving_rs=-850.0, claimed_rs=-850.0)
        alerts.nightly(sid, day)
        got = {a["rule"]: a for a in alerts.listing("active", sid)}
        assert got["savings_negative"]["category"] == "Savings" and "Rs" not in got["savings_negative"]["title"], got.keys()
        assert f"{day.day} {day:%B}" in got["savings_negative"]["title"]
        assert got["fee_above_third"]["category"] == "Product" and "Rs" not in got["fee_above_third"]["title"]
        _savings_row(sid, day.isoformat(), saving_rs=1200.0, claimed_rs=0.0, claim_withheld="import_unknown")
        monkeypatch.setattr(verify, "summary", lambda site_id, d0, d1, see_fee=False: {"fee": {"within_one_third": True}})
        alerts.nightly(sid, day)
        got = {a["rule"]: a for a in alerts.listing("active", sid)}
        assert "savings_negative" not in got and "fee_above_third" not in got, got.keys()
        assert got["savings_withheld"]["category"] == "Savings" and "not metered" in got["savings_withheld"]["title"], got["savings_withheld"]
        assert got["savings_withheld"]["detail"] == {"day": day.isoformat()}                 # the reason is said in the title, not as its code
    # the viewer sees none of them; the analyst sees the Product one (the fee is theirs to read)
    assert not [a for a in world.c["viewer"].get("/api/alerts").json() if a["category"] in ("Savings", "Product")]


# ------------------------------------------------------------------------------------------------ brief-vs-build-18

def _at(y: int, m: int, d: int, hh: int = 12) -> int:
    return int(datetime(y, m, d, hh, tzinfo=PKT).timestamp())


def _tariff_alerts(sid: str) -> dict:
    return {a["rule"]: a for a in alerts.listing("active", sid) if a["category"] == "Tariff"}


def test_a_passed_awaiting_notification_date_and_an_old_fuel_price_raise_tariff_alerts(world):
    sid = world.site
    with world.use():
        from gridlytics import tariffs
        tariffs.ensure_shipped()
        # 9 November 2026: the CY2027 placeholder is not due yet and the diesel price is three days old
        alerts.tariff_staleness(sites.full(sid), _at(2026, 11, 9), alerts.site_rules(sid))
        assert not {"tariff_awaiting", "fuel_price_old"} & set(_tariff_alerts(sid))
        # 5 January 2027: NEPRA's CY2027 schedule was due on 1 January and is not in the library; the diesel price is two months old
        alerts.tariff_staleness(sites.full(sid), _at(2027, 1, 5), alerts.site_rules(sid))
        got = _tariff_alerts(sid)
        assert got["tariff_awaiting"]["severity"] == "warning" and "1 January 2027" in got["tariff_awaiting"]["title"], got.get("tariff_awaiting")
        assert "CY2027" in got["tariff_awaiting"]["title"] and "notification" in got["tariff_awaiting"]["title"]
        assert "6 November 2026" in got["fuel_price_old"]["title"] and "diesel" in got["fuel_price_old"]["title"].lower(), got.get("fuel_price_old")
        # an FCA and a QTA for the month's bills are looked for from the 15th
        assert not any(r.startswith("adder_missing_") for r in got)
        alerts.tariff_staleness(sites.full(sid), _at(2027, 1, 20), alerts.site_rules(sid))
        got = _tariff_alerts(sid)
        assert "January 2027" in got["adder_missing_fca"]["title"] and "adder_missing_qta" in got, got.keys()
        # the notified schedule approved: the awaiting alert clears; a fresh fuel price clears the other
        tariffs.add_fuel_price("hsd", "2027-01-19", 371.0, "test")
        with store.connect() as con:
            con.execute("UPDATE tariff_versions SET status = 'superseded' WHERE status = 'awaiting_notification'")
        alerts.tariff_staleness(sites.full(sid), _at(2027, 1, 20), alerts.site_rules(sid))
        assert not {"tariff_awaiting", "fuel_price_old"} & set(_tariff_alerts(sid))


def test_a_version_in_force_that_ended_with_nothing_after_it_raises_a_tariff_alert(world):
    sid = world.site
    with world.use():
        from gridlytics import tariffs
        tariffs.ensure_shipped()
        with store.connect() as con:
            con.execute("UPDATE tariff_versions SET effective_to = '2026-12-31' WHERE kind = 'tou' AND key = 'xwdisco'")
        alerts.tariff_staleness(sites.full(sid), _at(2026, 12, 20), alerts.site_rules(sid))
        assert "tariff_lapsed" not in _tariff_alerts(sid)
        alerts.tariff_staleness(sites.full(sid), _at(2027, 1, 5), alerts.site_rules(sid))
        lapsed = _tariff_alerts(sid)["tariff_lapsed"]
        assert "31 December 2026" in lapsed["title"] and "time-of-use" in lapsed["title"], lapsed["title"]
        # the evaluator runs the check each tick, and a viewer does not receive Tariff alerts
        alerts.evaluate(sid, _at(2027, 1, 5))
        assert "tariff_lapsed" in _tariff_alerts(sid)
    assert not [a for a in world.c["viewer"].get("/api/alerts").json() if a["category"] == "Tariff"]


def test_a_captive_gas_price_is_old_after_half_a_year_not_a_fortnight(world):
    sid = world.site
    with world.use():
        g = sites.full(sid)["assets"]["genset"]
        sites.put_asset(sid, "genset", {"rated_kw": g["rated_kw"], "fuel_kind": "gas_captive"}, "test")
        alerts.tariff_staleness(sites.full(sid), _at(2026, 11, 9), alerts.site_rules(sid))     # the shipped price is of 1 July 2026
        assert "fuel_price_old" not in _tariff_alerts(sid)
        alerts.tariff_staleness(sites.full(sid), _at(2027, 1, 20), alerts.site_rules(sid))
        assert "captive gas" in _tariff_alerts(sid)["fuel_price_old"]["title"]


def test_the_rules_a_reader_reads_are_those_of_the_categories_they_receive(world):
    sid = world.site
    viewer = world.c["viewer"].get("/api/alerts/rules").json()
    assert set(viewer) == {"Data", "Reserve", "Battery", "Genset", "Forecast"}, viewer.keys()   # no bill tolerance or fee rule for a viewer
    assert set(world.c["viewer"].get(f"/api/alerts/rules/sites/{sid}").json()["rules"]) == set(viewer)
    assert "Product" not in world.c["operator"].get("/api/alerts/rules").json()
    assert len(world.c["admin"].get("/api/alerts/rules").json()) == 9


# ------------------------------------------------------------------------------------------------ reports: role-analyst-25

def _past_month(back: int = 1) -> str:
    """A whole month before this one ("YYYY-MM"), `back` months back."""
    d = datetime.now(PKT).date().replace(day=1)
    for _ in range(back):
        d = (d - timedelta(days=1)).replace(day=1)
    return d.strftime("%Y-%m")


def test_report_rupees_round_as_the_page_does_and_claimed_never_reads_above_saving():
    from gridlytics import reports
    # the page's rs() is Math.round: half toward +infinity, and never "Rs -0"; Python's format rounds half to even (1900.5 read "Rs 1,900")
    assert reports.rs(1900.5) == "Rs 1,901" and reports.rs(1901.13) == "Rs 1,901"
    assert reports.rs(2.5) == "Rs 3" and reports.rs(-1900.5) == "-Rs 1,900" and reports.rs(-0.4) == "Rs 0" and reports.rs(None) == "-"
    assert reports.rs(1234567.49) == "Rs 1,234,567" and reports.rs(float("nan")) == "-"


def test_report_downloads_are_named_after_the_site_kind_and_month(world):
    sid, month = world.site, _past_month()
    with world.use():
        sites.update(sid, {"name": "Faisalabad Textile Mill (demo)"}, "test")
    a = world.c["admin"]
    rep = a.post("/api/reports", json={"kind": "energy_only", "site_id": sid, "period": month})
    assert rep.status_code == 200, rep.text
    for which in ("pdf", "csv"):
        r = a.get(f"/api/reports/{rep.json()['id']}/{which}")
        assert r.status_code == 200
        assert f'filename="gridlytics-faisalabad-textile-mill-demo-energy-only-{month}.{which}"' in r.headers["content-disposition"], r.headers
    # a name with no Latin letters or digits falls back to the site's id, so two sites never share a file name
    with world.use():
        sites.update(sid, {"name": "مل"}, "test")
    rep = a.post("/api/reports", json={"kind": "energy_only", "site_id": sid, "period": month}).json()
    r = a.get(f"/api/reports/{rep['id']}/pdf")
    assert f'filename="gridlytics-{sid}-energy-only-{month}.pdf"' in r.headers["content-disposition"], r.headers


# ------------------------------------------------------------------------------------------------ reports: the monthly report's content

def _story(monkeypatch) -> list[str]:
    """Each PDF built from here on, as its text: a paragraph per line and a table row per line with its cells joined by " | " (the PDF itself is still
    built, so a story ReportLab cannot lay out still fails)."""
    from reportlab.platypus import Paragraph, Table
    from gridlytics import reports

    def text(f) -> str:
        if isinstance(f, Paragraph):
            return f.getPlainText()
        if isinstance(f, Table):
            return "\n".join(" | ".join(text(c) if not isinstance(c, (str, int, float)) else str(c) for c in row) for row in f._cellvalues)
        if isinstance(f, (list, tuple)):
            return "\n".join(text(x) for x in f)
        inner = getattr(f, "_content", None) or getattr(f, "_flowables", None)
        return text(inner) if inner else ""

    built: list[str] = []
    real = reports.SimpleDocTemplate

    class Capture(real):
        def build(self, flowables, *a, **k):
            built.append("\n".join(t for t in (text(f) for f in flowables) if t))
            return super().build(flowables, *a, **k)

    monkeypatch.setattr(reports, "SimpleDocTemplate", Capture)
    return built


def _baseline(**over) -> dict:
    b = {"saving_rs": 1900.5, "claimed_rs": 1901.13, "levers": {"peak_window": 1500.0, "genset": 250.0, "mdi": 150.5}, "litres_saved": 72.4,
         "verified_days": 2, "perfect_foresight_rs": 4000.0, "mdi_months": {}, "unserved_kwh_actual": 0.0, "unserved_kwh_baseline": 2.0,
         "genset_unknown_days": 0, "import_unknown_days": 0}
    return {**b, **over}


def _day_row(day: str, **over) -> dict:
    d = {"day": day, "saving_native_rs": 950.25, "saving_tuned_rs": 400.0, "fidelity": "confirmed", "actual_cost_rs": 50000.0, "native_cost_rs": 50950.25,
         "litres_actual": None, "litres_native": None, "peak_kw": 300.0, "peak_import_kwh": 100.0, "levers": {"genset": 125.0},
         "import_unknown": False, "claim_withheld": None, "baseline_start": "chain"}
    return {**d, **over}


def _fake_summary(month: str, days: list[dict], native: dict | None = None, tuned: dict | None = None, **over) -> dict:
    out = {"from": f"{month}-01", "to": f"{month}-28", "computed": "2026-11-01T00:00:00+00:00", "days": days,
           "baselines": {"native": native or _baseline(), "tuned": tuned or _baseline(saving_rs=800.0, claimed_rs=780.0, perfect_foresight_rs=None)},
           "chain_stale_days": [], "preliminary": False,
           "final_conditions": {"verified_days": 30, "verified_days_needed": 28, "native_capture": True, "switchback_weeks": 8.0,
                                "switchback_weeks_needed": 8, "fca_billed": True},
           "blocked": None, "method": "Counterfactual replay at 1-minute resolution against the native capture and a tuned table"}
    return {**out, **over}


def _with_summary(monkeypatch, summ: dict, tdays: list[dict] | None = None) -> None:
    from gridlytics import verify
    monkeypatch.setattr(verify, "summary", lambda site_id, d0, d1, see_fee=False: summ)
    if tdays is not None:
        monkeypatch.setattr(telemetry, "days", lambda site_id, d0, d1: [dict(d) for d in tdays])


def _tday(day: str, **over) -> dict:
    return {"day": day, "import_kwh": 1200.0, "export_kwh": 0.0, "pv_kwh": 800.0, "load_kwh": 2100.0, "genset_kwh": 100.0, "genset_hours": 1.5,
            "litres": None, "outage_minutes": 120, "unserved_kwh": 0.0, "peak_kw": 310.0, **over}


def test_the_monthly_report_says_not_metered_for_an_unmetered_genset_and_marks_withheld_days(world, monkeypatch):
    """pages-D-08: verify.summary sums litres_saved `or 0`, so a month whose genset was never metered read "Diesel saved 0 L"; the page says "not
    metered". A day whose claim is withheld printed its saving beside "confirmed" with no mark, and the CSV had no column for it."""
    from gridlytics import reports
    sid, month = world.site, _past_month()
    d1, d2 = f"{month}-03", f"{month}-04"
    days = [_day_row(d1, levers={"genset": None}, claim_withheld="genset_unknown"),
            _day_row(d2, levers={"genset": None}, claim_withheld="import_unknown", import_unknown=True)]
    unmetered = _baseline(litres_saved=0.0, genset_unknown_days=2, levers={"peak_window": 1500.0, "mdi": 0.0})
    _with_summary(monkeypatch, _fake_summary(month, days, native=unmetered, tuned=_baseline(litres_saved=0.0, genset_unknown_days=2)),
                  [_tday(d1), _tday(d2)])
    built = _story(monkeypatch)
    with world.use():
        rep = reports.generate("monthly_savings", sid, month, "test")
        csv_text = reports.file_path(rep["id"], "csv").read_text(encoding="utf-8")
    text = built[-1]
    assert "0 L" not in text and "not metered" in text, text
    # each withheld day is marked in the daily rows, with its reason in words, and the rows say such a day claims nothing
    rows = [ln for ln in text.splitlines() if "Rs 950" in ln]
    assert len(rows) == 2 and all("claim withheld" in ln.lower() for ln in rows), rows
    assert "genset data missing" in rows[0].lower() and "grid import unknown" in rows[1].lower(), rows
    assert "claims nothing" in text
    head = csv_text.splitlines()[0].split(",")
    assert "claim_withheld" in head, head
    lines = [ln.split(",") for ln in csv_text.splitlines()[1:]]
    assert [ln[head.index("claim_withheld")] for ln in lines] == ["genset_unknown", "import_unknown"]


def test_the_monthly_reports_litres_are_marked_estimated_and_never_minus_zero(world, monkeypatch):
    from gridlytics import reports
    sid, month = world.site, _past_month()
    day = f"{month}-05"
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(day)], native=_baseline(litres_saved=-0.04), tuned=_baseline(litres_saved=72.44)),
                  [_tday(day)])
    built = _story(monkeypatch)
    with world.use():
        reports.generate("monthly_savings", sid, month, "test")
    text = built[-1]
    assert not re.search(r"-0(\.0)? L", text), text
    assert "0.0 L" in text and "72.4 L" in text and "Estimated" in text, text


def test_the_preliminary_sentence_names_the_conditions_that_apply_and_the_method_names_the_genset_assumption(world, monkeypatch):
    """engine-savings-13 (and flow-reports-alerts-members-keys-17): the PDF printed one fixed sentence that left out the native capture and the 8 weeks
    of switchback verify.summary also requires, and said the baseline is purely the inverter's settings although at a manual-start genset site the
    native replay assumes an operator rule the capture does not record; the ledger summed every day without saying so."""
    from gridlytics import reports
    sid, month = world.site, _past_month()
    day = f"{month}-05"
    conds = {"verified_days": 4, "verified_days_needed": 28, "native_capture": True, "switchback_weeks": 1.0, "switchback_weeks_needed": 8,
             "fca_billed": True}
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(day)], preliminary=True, final_conditions=conds), [_tday(day)])
    built = _story(monkeypatch)
    with world.use():
        rep = reports.generate("monthly_savings", sid, month, "test")
    assert rep["preliminary"] is True
    text = built[-1]
    prelim = next(ln for ln in text.splitlines() if ln.startswith("Preliminary"))
    assert "28 verified days (4 so far)" in prelim and "8 weeks of switchback (1 so far)" in prelim, prelim
    assert "fuel" not in prelim and "month is not over" not in prelim and "native settings" not in prelim, prelim
    assert "started by hand" in text and "assumption" in text and "with its fidelity" in text, text
    assert "every scored day" in text, text                   # the ledger says it sums the days that claim nothing as well
    # every condition that applies, and only those: no capture, the FCA not billed
    conds = {**conds, "verified_days": 30, "switchback_weeks": 9.0, "native_capture": False, "fca_billed": False}
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(day)], preliminary=True, final_conditions=conds))
    with world.use():
        sites.update(sid, {"genset_start_control": "auto_on_grid_loss"}, "test")
        reports.generate("monthly_savings", sid, month, "test")
    text = built[-1]
    prelim = next(ln for ln in text.splitlines() if ln.startswith("Preliminary"))
    assert "native settings" in prelim and "fuel charges adjustment" in prelim and "verified days" not in prelim and "switchback" not in prelim, prelim
    assert "started by hand" not in text                      # an automatic start is the inverter's own, in the capture


def test_a_report_made_while_days_are_out_of_step_says_so_and_the_fleet_roll_up_carries_the_flag(world, monkeypatch):
    """pr-review-06: the report and GET /api/fleet/savings ignored verify.summary's chain_stale_days, so a PDF made while days were out of step printed
    totals that do not add up with no word of it; the Savings page alone said "Recompute needed"."""
    from datetime import date
    from gridlytics import reports
    sid, month = world.site, _past_month()
    d1, d2 = f"{month}-05", f"{month}-06"
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(d1), _day_row(d2)], chain_stale_days=[d1, d2]), [_tday(d1), _tday(d2)])
    built = _story(monkeypatch)
    with world.use():
        reports.generate("monthly_savings", sid, month, "test")
    text = built[-1]
    stale = next((ln for ln in text.splitlines() if "Recompute needed" in ln), "")
    first = date.fromisoformat(d1)
    assert "2 days" in stale and f"{first:%a} {first.day} {first:%b}" in stale and "may not add up" in stale, text
    fleet = world.c["admin"].get(f"/api/fleet/savings?month={month}").json()
    row = next(r for r in fleet["sites"] if r["site_id"] == sid)
    assert row["chain_stale_days"] == [d1, d2] and fleet["stale_sites"] == 1, fleet
    # a site whose days are in step says so too
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(d1)]))
    with world.use():
        reports.generate("monthly_savings", sid, month, "test")
    assert "Recompute needed" not in built[-1]
    fleet = world.c["admin"].get(f"/api/fleet/savings?month={month}").json()
    assert fleet["sites"][0]["chain_stale_days"] == [] and fleet["stale_sites"] == 0


# ------------------------------------------------------------------------------------------------ role-viewer-17

def test_the_agreement_alert_says_its_date_and_connection_in_words(world):
    """role-viewer-17: the title read "The net metering legacy agreement ends on 2026-12-23" and the drawer showed the connection's internal id."""
    from datetime import date
    sid, now = world.site, _now()
    end = datetime.fromtimestamp(now, PKT).date() + timedelta(days=30)
    with world.use():
        conn = sites.connections(sid)[0]
        sites.put_connection(sid, {**conn["data"], "regime": "net_metering_legacy", "agreement_end": end.isoformat(), "meter_ref": "FS-24-11-0042"},
                             "test", conn_id=conn["id"])
        alerts.evaluate(sid, now)
        got = next(a for a in alerts.listing("active", sid) if a["rule"].startswith("regime_expiring_"))
    assert got["title"] == f"The legacy net metering agreement ends on {end.day} {end:%B %Y}", got["title"]
    assert conn["id"] not in json.dumps(got["detail"]) and got["detail"]["connection"] == "FS-24-11-0042", got["detail"]
    assert date.fromisoformat(got["detail"]["agreement_end"]) == end
    # a connection without a meter reference is named by its tariff and regime
    with world.use():
        sites.put_connection(sid, {**conn["data"], "regime": "net_billing", "agreement_end": end.isoformat(), "meter_ref": ""}, "test", conn_id=conn["id"])
        alerts.evaluate(sid, now)
        got = next(a for a in alerts.listing("active", sid) if a["rule"].startswith("regime_expiring_"))
    assert got["detail"]["connection"] == "B-2(b), net billing", got["detail"]
    assert got["title"].startswith("The net billing agreement ends on"), got["title"]


def test_the_energy_report_says_the_regime_and_the_days_in_words(world, monkeypatch):
    """role-viewer-17: the energy report's header read "FESCO B-2(b), none." and its rows ISO dates."""
    from datetime import date
    from gridlytics import reports
    sid, month = world.site, _past_month()
    day = f"{month}-06"
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(day)]), [_tday(day)])
    built = _story(monkeypatch)
    with world.use():
        for kind in ("energy_only", "monthly_savings"):
            reports.generate(kind, sid, month, "test")
            text = built[-1]
            header = text.splitlines()[1]
            assert "none" not in header and "no export" in header, header
            d = date.fromisoformat(day)
            assert day not in text and f"{d:%a} {d.day} {d:%b}" in text, text
    assert not re.search(r"\b(not_applied|native_block|recommendation_only|import_unknown|genset_unknown)\b", built[-1]), built[-1]
    assert not re.search(r"\| confirmed\b", built[-1]) and "| Confirmed" in built[-1], built[-1]   # the daily rows' fidelity in words


# ------------------------------------------------------------------------------------------------ pages-D-07 (and tracker-and-docs-14, flow-reports-alerts-members-keys-03, -05)

def _plan_8_5_world(monkeypatch, sid: str, month: str) -> dict:
    """A month with every part plan 8.5 lists: two days of data, an MDI month, two outages (one carried, one with unserved load), forecast scores,
    an edge read-back mismatch, a switchback calendar with a carry-over credit and an entered bill."""
    from datetime import date
    from gridlytics import verify
    d1, d2 = f"{month}-08", f"{month}-09"
    native = _baseline(mdi_months={month: {"actual_kw": 310.0, "baseline_kw": 340.0, "floor_kw": 0.0}}, unserved_kwh_actual=1.5)
    summ = _fake_summary(month, [_day_row(d1), _day_row(d2, fidelity="partial")], native=native,
                         tuned=_baseline(saving_rs=800.0, claimed_rs=780.0, perfect_foresight_rs=None, levers={"peak_window": 600.0, "mdi": 200.0}))
    _with_summary(monkeypatch, summ, [_tday(d1), _tday(d2)])
    t1 = int(datetime(int(month[:4]), int(month[5:7]), 8, 10, tzinfo=PKT).timestamp())
    monkeypatch.setattr(telemetry, "outages", lambda site_id, a, b: [
        {"start": t1, "end": t1 + 3600, "minutes": 60, "soc_start_pct": 80.0, "genset_minutes": 0, "unserved_kwh": 0.0},
        {"start": t1 + 86400, "end": t1 + 86400 + 7200, "minutes": 120, "soc_start_pct": 30.0, "genset_minutes": 30, "unserved_kwh": 1.5}])
    blocks = [{"site_id": sid, "block_start": f"{month}-01", "block_end": f"{month}-07", "arm": "plan", "locked": 1},
              {"site_id": sid, "block_start": f"{month}-08", "block_end": f"{month}-14", "arm": "native", "locked": 1}]
    monkeypatch.setattr(verify, "switchback_result", lambda site_id: {
        "blocks": blocks, "days": 14, "carry_over_credits": [{"boundary": f"{month}-08", "from_arm": "plan", "to_arm": "native", "e_end_plan_kwh": 120.0,
                                                               "e_end_native_kwh": 80.0, "v_end": 30.0, "credit_rs": 1200.0}],
        "result": {"saving_rs_per_day_unadjusted": 950.0, "saving_rs_per_day_adjusted": 900.0, "plan_days": 7, "native_days": 7,
                   "carry_over_credit_rs": 1200.0, "carry_over_rs_per_plan_day": 171.43, "saving_rs_per_day_unadjusted_before_carry_over": 778.57,
                   "saving_rs_per_day_adjusted_before_carry_over": 728.57}})
    monkeypatch.setattr(verify, "bills", lambda site_id: [{"id": "b1", "site_id": sid, "period_start": f"{month}-01", "period_end": f"{month}-28",
                                                           "real": {"total_rs": 1000000.0}, "rebuilt": {"total_rs": 1025000.0}, "difference_pct": 2.5}])
    with store.connect() as con:
        con.execute("INSERT INTO forecast_scores (site_id, kind, day_local, horizon_class, metric, value, baseline_value) VALUES (?,?,?,?,?,?,?)",
                    (sid, "pv", d1, "day_ahead", "nrmse", 0.18, None))
        con.execute("INSERT INTO forecast_scores (site_id, kind, day_local, horizon_class, metric, value, baseline_value) VALUES (?,?,?,?,?,?,?)",
                    (sid, "load", d1, "day_ahead", "nrmse", 0.10, 0.20))
        con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)",
                    (sid, datetime.fromtimestamp(t1, timezone.utc).isoformat(), "readback_mismatch", "edge", "{}"))
    return {"d1": date.fromisoformat(d1), "summ": summ}


def test_the_monthly_report_carries_what_plan_8_5_lists(world, monkeypatch):
    """pages-D-07: the PDF had a headline, a native-only ledger, the method, energy and daily rows: no KPI grid (diesel, peak, outage coverage,
    forecast skill, control health), no tuned ledger, no switchback calendar or carry-over credits, no bill comparison, no assumptions note, no
    one-page summary with an annex, and no word that the month's data covers only part of it."""
    from datetime import date
    from gridlytics import reports
    sid, month = world.site, _past_month()
    with world.use():
        _plan_8_5_world(monkeypatch, sid, month)
        built = _story(monkeypatch)
        reports.generate("monthly_savings", sid, month, "test")
    text = built[-1]
    lines = text.splitlines()
    order = [next(i for i, ln in enumerate(lines) if ln == h) for h in ("Summary", "Key figures", "Ledger by lever", "Switchback", "Bill comparison",
                                                                         "Annex: energy and daily rows")]
    assert order == sorted(order), order
    assert "covers 2 of the month's" in text, text                           # the data covers part of the month, and the report says so
    grid = {ln.split(" | ")[0]: ln for ln in lines if " | " in ln}
    assert {"Diesel", "Peak (MDI)", "Outage coverage", "Forecast skill", "Control health"} <= set(grid), grid.keys()
    assert "310 kW" in grid["Peak (MDI)"] and "340 kW" in grid["Peak (MDI)"], grid["Peak (MDI)"]
    assert "2 outages" in grid["Outage coverage"] and "1 carried without unserved critical load" in grid["Outage coverage"], grid["Outage coverage"]
    assert "18.0 %" in grid["Forecast skill"] and "50 % better than" in grid["Forecast skill"], grid["Forecast skill"]
    assert "1 confirmed" in grid["Control health"] and "1 partly followed" in grid["Control health"], grid["Control health"]
    assert "1 read-back mismatch" in grid["Control health"], grid["Control health"]
    ledger = next(ln for ln in lines if ln.startswith("Lever | "))
    assert ledger == "Lever | Against the native settings | Against a tuned table", ledger
    assert any(ln.startswith("Peak-window import avoided | Rs 1,500 | Rs 600") for ln in lines), text
    assert any("Carry-over credit" in ln and "Rs 1,200" in ln for ln in lines), text
    first = date.fromisoformat(f"{month}-01")
    assert any(ln.startswith(f"{first:%a} {first.day} {first:%b} | ") and ln.endswith("The plan") for ln in lines), text
    assert any(ln.endswith("The native settings") for ln in lines), text
    assert "Rs 900 a day" in text and "Rs 950 a day" in text, text           # the switchback result, adjusted and unadjusted
    bill = next(ln for ln in lines if "Rs 1,000,000" in ln)
    assert "Rs 1,025,000" in bill and "2.5 %" in bill, bill
    assert "GST" in text                                                     # the assumptions note: the bearable-cost basis
    assert month not in text.replace(f"{month}-", "")                          # no "YYYY-MM" month in the words


def test_a_company_roll_up_report_lists_every_site_and_the_total(world, monkeypatch):
    """pages-D-07 (and tracker-and-docs-14, flow-reports-alerts-members-keys-03): plan 8.5 asks for a roll-up per company; POST /api/reports needed a
    site."""
    from gridlytics import verify
    sid, month = world.site, _past_month()
    with world.use():
        two = sites.create({"name": "Mill Two", "disco": "LESCO"}, "test")["id"]
    by_site = {sid: _fake_summary(month, [_day_row(f"{month}-02")], preliminary=True),
               two: _fake_summary(month, [], native=_baseline(saving_rs=500.0, claimed_rs=400.0), chain_stale_days=[f"{month}-03"])}
    monkeypatch.setattr(verify, "summary", lambda site_id, d0, d1, see_fee=False: by_site[site_id])
    built = _story(monkeypatch)
    for role in ("viewer", "operator"):
        assert world.c[role].post("/api/reports", json={"kind": "company_rollup", "period": month}).status_code == 403, role
    r = world.c["analyst"].post("/api/reports", json={"kind": "company_rollup", "period": month})
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["site_id"] == "" and row["kind"] == "company_rollup" and row["preliminary"] is True, row
    text = built[-1]
    assert "Mill One" in text and "Mill Two" in text, text
    total = next(ln for ln in text.splitlines() if ln.startswith("All sites"))
    assert "Rs 2,301" in total, total                                      # the claims summed: 1,901.13 + 400
    assert "Recompute needed" in text and "Mill Two" in next(ln for ln in text.splitlines() if "Recompute needed" in ln)
    prelim = next(ln for ln in text.splitlines() if ln.startswith("Preliminary."))
    assert "The figures of Mill One are preliminary" in prelim, prelim         # the site's name as it is written
    d = world.c["analyst"].get(f"/api/reports/{row['id']}/pdf")
    assert d.status_code == 200 and f'filename="gridlytics-company-rollup-{month}.pdf"' in d.headers["content-disposition"]
    csv_rows = world.c["analyst"].get(f"/api/reports/{row['id']}/csv").text.splitlines()
    assert csv_rows[0].startswith("site_id,site,") and len(csv_rows) == 3, csv_rows
    assert not [x for x in world.c["viewer"].get("/api/reports").json() if x["kind"] == "company_rollup"]


def test_a_pilot_report_fills_the_pre_registered_criteria_and_is_titled_by_tier(world, monkeypatch):
    """pages-D-07 (and flow-reports-alerts-members-keys-03): POST /api/reports {kind: pilot} answered 422; plan 8.5 asks for the pilot write-up with
    the pre-registered criteria of Part 15.3 filled in, and R8 2.14 titles it "Counterfactual-only report" below the advisory tier."""
    sid, month = world.site, _past_month()
    with world.use():
        _plan_8_5_world(monkeypatch, sid, month)
        store.set_setting(f"fee:{sid}", 100.0)
    built = _story(monkeypatch)
    assert world.c["operator"].post("/api/reports", json={"kind": "pilot", "site_id": sid, "period": month}).status_code == 403
    r = world.c["admin"].post("/api/reports", json={"kind": "pilot", "site_id": sid, "period": month})
    assert r.status_code == 200, r.text
    text = built[-1]
    assert text.splitlines()[0] == "Counterfactual-only report: Mill One", text.splitlines()[0]
    lines = text.splitlines()
    crit = {ln.split(" | ")[0]: ln for ln in lines if " | " in ln}
    for name in ("Saving at least", "Unserved critical energy at most", "Reserve adequacy at least", "Plan fidelity at least",
                 "Verified saving at least", "Solar forecast error at most"):
        assert any(k.startswith(name) for k in crit), (name, crit.keys())
    fid = next(v for k, v in crit.items() if k.startswith("Plan fidelity at least"))
    assert "50 %" in fid and "falls short" in fid, fid                       # 1 of the 2 plan days confirmed, against 80 %
    stat = next(v for k, v in crit.items() if k.startswith("Lower 80 % bound"))
    assert "fee" in stat, stat
    assert "Switchback" in lines and "Forecast accuracy" in lines and "Control health" in lines
    with world.use():
        sites.set_tier_capable(sid, "advisory_writes")
        sites.set_tier_allowed(sid, "advisory_writes", "test")
    world.c["admin"].post("/api/reports", json={"kind": "pilot", "site_id": sid, "period": month})
    assert built[-1].splitlines()[0] == "Pilot report: Mill One"
    # the pilot report carries the fee: a role that does not see the fee neither lists nor downloads it
    rid = r.json()["id"]
    assert not [x for x in world.c["operator"].get("/api/reports").json() if x["kind"] == "pilot"]
    assert world.c["operator"].get(f"/api/reports/{rid}/pdf").status_code == 403
    assert world.c["analyst"].get(f"/api/reports/{rid}/pdf").status_code == 200


def test_a_started_pilots_report_reads_from_its_start_and_scores_the_part_15_3_statistic(world, monkeypatch):
    """pages-D-07: the write-up runs from the pilot's start, says who signed it off, and fills Part 15.3's statistic (the lower 80 % bound of the daily
    verified saving over the tuned table, times 30) against the fee."""
    from datetime import date
    from gridlytics import reports, verify
    sid, month = world.site, _past_month()
    days = [_day_row(f"{month}-{d:02d}", saving_tuned_rs=v) for d, v in ((10, 400.0), (11, 420.0), (12, 380.0))]
    seen = {}

    def summary(site_id, d0, d1, see_fee=False):
        seen.update(d0=d0, see_fee=see_fee)
        return _fake_summary(month, days, fee={"fee_rs_per_month": 2000.0, "verified_saving_rs_per_month": 12000.0})
    monkeypatch.setattr(verify, "summary", summary)
    with world.use():
        started = datetime(int(month[:4]), int(month[5:7]), 10, 9, tzinfo=PKT)
        with store.connect() as con:
            con.execute("INSERT INTO pilots (site_id, data, started, updated) VALUES (?,?,?,?)",
                        (sid, json.dumps({"criteria": {"fee_multiple": 3.0}, "signoff": {"name": "Ayesha Khan"}, "amendments": []}),
                         started.astimezone(timezone.utc).isoformat(), store._now()))
        store.set_setting(f"fee:{sid}", 2000.0)
    built = _story(monkeypatch)
    with world.use():
        reports.generate("pilot", sid, month, "test")
    text = built[-1]
    assert seen == {"d0": f"{month}-10", "see_fee": True}, seen
    assert f"Started on 10 {date(2000, int(month[5:7]), 1):%B} {month[:4]}, signed off by Ayesha Khan" in text, text
    fee = next(ln for ln in text.splitlines() if ln.startswith("Verified saving at least"))
    assert "6.0 times the fee" in fee and "meets it" in fee, fee
    stat = next(ln for ln in text.splitlines() if ln.startswith("Lower 80 % bound"))
    # mean 400, sd 20, n 3: 400 - t(0.8, 2) x 20 / sqrt(3) = 400 - 1.0607 x 11.547 = 387.75 a day, x 30 = Rs 11,633 a month, 5.8 times a fee of 2,000
    assert "Rs 11,633 a month, 5.8 times the fee" in stat and "meets the success gate" in stat, stat


def test_an_off_grid_sites_monthly_report_says_there_is_no_grid_to_compare(world, monkeypatch):
    from gridlytics import reports
    sid, month = world.site, _past_month()
    day = f"{month}-05"
    _with_summary(monkeypatch, _fake_summary(month, [_day_row(day)]), [_tday(day)])
    built = _story(monkeypatch)
    with world.use():
        conn = sites.connections(sid)[0]
        sites.put_connection(sid, {**conn["data"], "regime": "off_grid"}, "test", conn_id=conn["id"])
        reports.generate("monthly_savings", sid, month, "test")
    text = built[-1]
    assert "No grid connection: there are no grid outages to carry." in text and "there is no DISCO bill to compare" in text, text
    assert "off-grid" in text.splitlines()[1], text.splitlines()[1]


# ------------------------------------------------------------------------------------------------ pages-D-24 (and flow-reports-alerts-members-keys-01, -19)

def _day_words(d) -> str:
    return f"{d:%A} {d.day} {d:%B %Y}"


def _slot(start: str, end: str, mode: str, floor: float, target: float) -> dict:
    return {"start": start, "end": end, "mode": mode, "soc_floor_pct": floor, "soc_target_pct": target, "intent": f"{mode} {floor:.0f}-{target:.0f}"}


def _put_plan(sid: str, pid: str, belief: int, days: dict, status: str = "active") -> None:
    """A plan row made at `belief` (epoch seconds) whose compiled table holds `days` ({"YYYY-MM-DD": [slot, ...]})."""
    from gridlytics import plans
    slots = {"brand": "deye", "days": [{"date": d, "slots": s} for d, s in days.items()], "fallback": {"on_expiry": "inverter_native"}}
    with store.connect() as con:
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, summary, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, sid, plans._iso(belief), plans._iso(belief), plans._iso(belief), plans._iso(belief + 172800),
                                                       "A", status, "{}", "[]", json.dumps(slots)))


def test_todays_daily_sheet_prints_the_dated_tables_of_today_and_tomorrow_not_the_plans_partial_last_day(world, monkeypatch):
    """flow-reports-alerts-members-keys-01: the sheet printed `days[-1:]`, the 48-hour plan's partial last day (the day after tomorrow, "hold 20 %"
    until 13:15), undated, as "Tomorrow's slot table", under a header of the whole month."""
    sid = world.site
    today = datetime.now(PKT).date()
    tomorrow, after = today + timedelta(days=1), today + timedelta(days=2)
    with world.use():
        _put_plan(sid, "p-now", _now() - 600, {
            today.isoformat(): [_slot("17:15", "00:00", "discharge", 20, 20)],
            tomorrow.isoformat(): [_slot("00:00", "17:45", "charge_grid", 20, 95), _slot("17:45", "00:00", "discharge", 20, 95)],
            after.isoformat(): [_slot("00:00", "13:15", "hold", 20, 20)]})
    built = _story(monkeypatch)
    r = world.c["operator"].post("/api/reports", json={"kind": "daily_operations", "site_id": sid})   # no period: today's sheet
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["period"] == today.isoformat() and row["preliminary"] is None, row
    text = built[-1]
    head = text.splitlines()[1]
    assert f"For {_day_words(today)}." in head and " to " not in head, head                       # the day, not "01 November 2026 to 30 November 2026"
    assert f"Today, {_day_words(today)}: the rest of the day" in text, text
    assert f"Tomorrow, {_day_words(tomorrow)}: the table to key in" in text, text
    assert "00:00 | 17:45 | Charge from grid | 20 | 95" in text and "17:45 | 00:00 | Discharge | 20 | 95" in text, text
    assert "13:15" not in text and after.isoformat() not in text and "Hold" not in text, text      # the day after tomorrow is not printed
    assert "charge_grid |" not in text, text                                                      # the mode in words
    csv_text = world.c["operator"].get(f"/api/reports/{row['id']}/csv").text
    dates = {line.split(",")[0] for line in csv_text.splitlines()[1:]}
    assert dates == {today.isoformat(), tomorrow.isoformat()}, csv_text
    named = world.c["operator"].get(f"/api/reports/{row['id']}/pdf").headers["content-disposition"]
    assert f"daily-operations-{today.isoformat()}.pdf" in named, named


def test_a_past_days_sheet_prints_the_plan_in_force_at_its_start_and_what_happened(world, monkeypatch):
    sid = world.site
    today = datetime.now(PKT).date()
    day = today - timedelta(days=3)
    start = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    with world.use():
        _put_plan(sid, "p-eve", start - 900, {
            (day - timedelta(days=1)).isoformat(): [_slot("23:45", "00:00", "self_use", 20, 90)],
            day.isoformat(): [_slot("00:00", "06:00", "charge_grid", 20, 90), _slot("06:00", "00:00", "self_use", 30, 90)],
            (day + timedelta(days=1)).isoformat(): [_slot("00:00", "23:45", "self_use", 20, 90)]}, status="superseded")
        _put_plan(sid, "p-noon", start + 12 * 3600, {day.isoformat(): [_slot("12:00", "00:00", "hold", 55, 55)]}, status="superseded")
        _put_plan(sid, "p-now", _now() - 600, {today.isoformat(): [_slot("17:15", "00:00", "export", 40, 40)]})
    monkeypatch.setattr(telemetry, "days", lambda site_id, d0, d1: [_tday(day.isoformat(), outage_minutes=90, genset_hours=1.5)] if d0 <= day.isoformat() <= d1 else [])
    built = _story(monkeypatch)
    r = world.c["analyst"].post("/api/reports", json={"kind": "daily_operations", "site_id": sid, "period": day.isoformat()})
    assert r.status_code == 200, r.text
    text = built[-1]
    assert f"For {_day_words(day)}." in text.splitlines()[1], text
    assert f"{_day_words(day)}: the table as planned at the start of the day" in text, text
    assert "06:00 | 00:00 | Self-use | 30 | 90" in text and "Hold" not in text and "Export" not in text, text   # the plan in force then, not later ones
    assert "Tomorrow" not in text, text
    assert "What happened that day" in text and "1.5 h" in text and "1,200 kWh" in text, text


def test_a_daily_sheet_is_asked_for_a_day_today_or_before_and_says_when_no_plan_was_in_force(world, monkeypatch):
    sid = world.site
    a = world.c["admin"]
    today = datetime.now(PKT).date()
    for period, words in ((today.strftime("%Y-%m"), "is for a day"), ("2026-02-30", "not a day of the calendar"),
                          ((today + timedelta(days=1)).isoformat(), "today or a day before")):
        r = a.post("/api/reports", json={"kind": "daily_operations", "site_id": sid, "period": period})
        assert r.status_code == 422 and words in r.json()["detail"], (period, r.text)
    assert a.get("/api/reports").json() == []
    built = _story(monkeypatch)
    r = a.post("/api/reports", json={"kind": "daily_operations", "site_id": sid, "period": today.isoformat()})
    assert r.status_code == 200, r.text
    assert "No plan was in force" in built[-1] and "Start | End" not in built[-1], built[-1]


# ------------------------------------------------------------------------------------------------ role-operator-20 (and flow-reports-alerts-members-keys-18)

def test_a_report_is_listed_and_downloaded_by_the_roles_that_may_generate_its_kind(world):
    """The monthly savings CSV was a savings export the operator was refused on the Savings page (export: an administrator or an analyst), but its
    download checked only see_costs. One rule now: who may generate a kind lists and downloads it."""
    sid, month = world.site, _past_month()
    today = datetime.now(PKT).date().isoformat()
    a = world.c["admin"]
    made = {}
    for kind, body in (("monthly_savings", {"site_id": sid, "period": month}), ("company_rollup", {"period": month}),
                       ("daily_operations", {"site_id": sid, "period": today}), ("energy_only", {"site_id": sid, "period": month}),
                       ("pilot", {"site_id": sid, "period": month})):
        r = a.post("/api/reports", json={"kind": kind, **body})
        assert r.status_code == 200, (kind, r.text)
        made[kind] = r.json()["id"]
    may = {"admin": set(made), "analyst": set(made), "operator": {"daily_operations", "energy_only"}, "viewer": {"energy_only"}}
    for role, kinds in may.items():
        c = world.c[role]
        assert {r["kind"] for r in c.get("/api/reports").json()} == kinds, role
        for kind, rid in made.items():
            for which in ("pdf", "csv"):
                got = c.get(f"/api/reports/{rid}/{which}")
                assert got.status_code == (200 if kind in kinds else 403), (role, kind, which, got.status_code)
    refused = world.c["operator"].get(f"/api/reports/{made['monthly_savings']}/csv").json()["detail"]
    assert refused == "The monthly savings report is for an administrator or an analyst", refused

