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
