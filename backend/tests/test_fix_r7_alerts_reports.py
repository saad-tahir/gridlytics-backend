"""Regression tests of the round-7 audit's alerts-and-reports findings: a viewer receives only the energy categories of alerts (security-roles-03,
pages-D-15), the rules the plan lists exist (pages-D-14), a site that never sent data raises a Data alert (pages-D-27), the forecast bust is the plan's
(engine-forecast-10), the tariff library's staleness raises a Tariff alert (brief-vs-build-18), and the rule editor validates quiet hours and keeps
thresholds and per-site overrides that the evaluator reads (pages-D-29).

Each test works in a data folder of its own (GRIDLYTICS_DATA_DIR) with a company file of its own, so no person or row reaches the shared test database."""
from __future__ import annotations

import json
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
        assert rows and rows["hours"] >= 3.5 and rows["nrmse"] < 0.6, rows    # the old rule (daily nRMSE by nameplate > 0.6) never saw it
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
