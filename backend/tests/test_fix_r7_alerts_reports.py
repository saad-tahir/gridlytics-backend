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
