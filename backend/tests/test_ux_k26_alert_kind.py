"""UX backend ask K26 (plan H11; E6 item 8, E1.3.8): the inbox filtered by kind, `GET /api/alerts?kind=forecast_bust`.

The Accuracy view's missed-forecasts list reads only the "Forecast missed" alerts, resolved ones too: `?kind=forecast_bust&state=&every=true`. The kind
is an alert's rule; it filters within the categories the reader receives (a viewer receives Forecast, never Savings), and a kind in no form of a rule's
name is refused in one sentence. Test data: alerts raised by the module's own `_set`, in company files of their own."""
from __future__ import annotations

import types

import pytest

from gridlytics import alerts, store

KIND_WORDS = "The kind of alert is a rule's name in lower case, such as forecast_bust"


@pytest.fixture()
def api_world(tmp_path, monkeypatch):
    import identities
    from gridlytics import platform as platformdb, sites
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    made = {}
    for name in ("K26 company", "K26 other company"):
        co = identities.company(name, own_file=True)
        db = platformdb.company_file(co)
        db.parent.mkdir(parents=True, exist_ok=True)
        with store.use_company(db):
            sid = sites.create({"name": f"{name} mill", "disco": "FESCO"}, "test")["id"]
        made[name] = (co, db, sid)
    co, db, sid = made["K26 company"]
    clients = {role: identities.client(f"{role}@k26.test", None if role == "admin" else role, co, "owner" if role == "admin" else "member",
                                       raise_server_exceptions=False) for role in ("admin", "operator", "viewer")}
    return types.SimpleNamespace(db=db, site=sid, other=made["K26 other company"], c=clients)


def _bust(sid: str, day: str, hours: float) -> None:
    alerts._set(True, sid, "forecast_bust", "Forecast", "info", f"The day-ahead PV forecast for {day} was off for {hours:g} hours",
                {"day": day, "hours": hours})


def _raise(w) -> None:
    with store.use_company(w.db):
        _bust(w.site, "2026-11-08", 3.5)
        alerts._set(False, w.site, "forecast_bust", "Forecast", "info", "", {})        # cleared: resolved by the product
        _bust(w.site, "2026-11-10", 2.0)                                                # raised again on another day: open
        alerts._set(True, w.site, "forecast_stage_pv", "Forecast", "info", "The solar forecast moved to stage 1", {"from_stage": 0, "to_stage": 1})
        alerts._set(True, w.site, "savings_negative", "Savings", "warning", "A day saved less than nothing", {"day": "2026-11-10"})
    _, other_db, other_site = w.other
    with store.use_company(other_db):
        _bust(other_site, "2026-11-09", 5.0)


def test_the_missed_forecasts_are_every_forecast_bust_alert_of_the_site_resolved_ones_too(api_world):
    w = api_world
    _raise(w)
    for role in ("admin", "operator", "viewer"):
        r = w.c[role].get("/api/alerts", params={"kind": "forecast_bust", "state": "", "every": "true", "site_id": w.site})
        assert r.status_code == 200, (role, r.text)
        rows = r.json()
        assert sorted((a["detail"]["day"], a["state"]) for a in rows) == [("2026-11-08", "resolved"), ("2026-11-10", "open")], role
        assert {a["rule"] for a in rows} == {"forecast_bust"} and {a["category"] for a in rows} == {"Forecast"}, role
    # without the kind, the same address lists the stage alert too; the active inbox only the open bust
    every = w.c["admin"].get("/api/alerts", params={"state": "", "every": "true", "site_id": w.site}).json()
    assert {a["rule"] for a in every} == {"forecast_bust", "forecast_stage_pv", "savings_negative"}
    active = w.c["admin"].get("/api/alerts", params={"kind": "forecast_bust", "every": "true"}).json()
    assert [a["detail"]["day"] for a in active] == ["2026-11-10"]


def test_a_kind_filters_within_the_categories_the_reader_receives(api_world):
    """A viewer receives the energy categories: `?kind=savings_negative` lists nothing for a viewer, the administrator's alert for the administrator;
    an unknown rule lists nothing for anyone, and another company's alerts are never listed."""
    w = api_world
    _raise(w)
    q = {"kind": "savings_negative", "state": "", "every": "true"}
    assert [a["rule"] for a in w.c["admin"].get("/api/alerts", params=q).json()] == ["savings_negative"]
    assert w.c["viewer"].get("/api/alerts", params=q).json() == []
    assert w.c["admin"].get("/api/alerts", params={**q, "kind": "no_such_rule"}).json() == []
    days = [a["detail"].get("day") for a in w.c["admin"].get("/api/alerts", params={**q, "kind": "forecast_bust"}).json()]
    assert "2026-11-09" not in days and sorted(days) == ["2026-11-08", "2026-11-10"]
    _, _, other_site = w.other
    assert w.c["admin"].get("/api/alerts", params={**q, "kind": "forecast_bust", "site_id": other_site}).json() == []


@pytest.mark.parametrize("kind", ["Forecast_Bust", "forecast bust", "forecast-bust", "_bust", "fórecast_bust", "a" * 65, "1st_rule"])
def test_a_kind_in_no_form_of_a_rule_s_name_is_refused_in_one_sentence(api_world, kind):
    r = api_world.c["viewer"].get("/api/alerts", params={"kind": kind})
    assert r.status_code == 422 and r.json()["detail"] == KIND_WORDS, (kind, r.text)


def test_the_listing_takes_the_kind_with_its_other_filters():
    assert alerts.alert_kind(None) is None and alerts.alert_kind("") is None and alerts.alert_kind("edge_fallback_engaged") == "edge_fallback_engaged"
    with pytest.raises(ValueError, match="rule's name"):
        alerts.alert_kind(["forecast_bust"])
