"""UX backend ask K20 (plan H11; E6 item 1, D3.3, D3.4, E1.1.3; the Forecasts section's step 10): a past issue by its time.

`GET /api/sites/{id}/forecasts/{kind}?at=<belief_time>` answers the issue held at that time (the latest issued at or before it that is still kept),
so Plan's "The forecast this plan used" opens the issue itself, not the latest. `at` is the plan's own `belief_time` (ISO 8601; with no offset,
Karachi time) or the forecast page's (epoch seconds); a form of neither, or a time still to come, is refused in one sentence, and a time from before
every kept issue is a 404 that says so. Test data: synthetic issues and telemetry; nothing here calls the network."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from test_ux_k23_stage_progress import PKT, _issue, _site, _ts, company  # noqa: F401  (company is a fixture)

from gridlytics import forecast, store, telemetry, twin

# ------------------------------------------------------------------------------------------------ K20: a past issue by its time

def test_a_past_issue_is_the_one_held_at_that_time_with_the_latest_named_beside_it(company):
    sid = "k20"
    site = _site(sid)
    now = int(datetime.now(timezone.utc).timestamp())
    b = forecast.align(now)
    older, old = b - 2 * 3600, b - 3600
    _issue(sid, "load", older, q50=1.0)
    _issue(sid, "load", old, q50=2.0)
    _issue(sid, "load", b, q50=3.0)
    got = forecast.page(site, "load", old)
    assert got["belief_time"] == old and got["issue"]["q50"][0] == 2.0
    assert got["at"] == old and got["exact"] is True and got["latest_belief_time"] == b
    between = forecast.page(site, "load", old + 600)                     # 10 minutes after: the issue held then is the same one
    assert between["belief_time"] == old and between["exact"] is False and between["at"] == old + 600
    assert forecast.page(site, "load", older - 1) is None                # from before every kept issue: none
    latest = forecast.page(site, "load")
    assert latest["belief_time"] == b and "at" not in latest and "exact" not in latest
    # its actuals run over its horizon up to now: the hour after it and this quarter so far
    a = old - 3600
    telemetry.write_rows([(sid, t, "edge", 3000.0) for t in range(a, now // 60 * 60, 60)], ("site_id", "ts", "source_id", "load_power_w"))
    got = forecast.page(site, "load", old)
    assert len(got["actual"]) == (now // 60 * 60 - old) // forecast.STEP_S and got["actual"][0] == pytest.approx(3.0)


def test_a_past_issue_s_preliminary_count_and_announced_windows_are_those_of_its_own_day(company):
    """A past issue says Preliminary as it stood on its own Karachi day (the verified days before it), and an outage issue the windows of the
    schedule in force at its time, not today's."""
    sid = "k20-day"
    site = _site(sid)
    d = date(2026, 11, 10)
    bt = twin.day_start_utc(d) + 12 * 3600
    with store.connect() as con:
        for day in ("2026-11-08", "2026-11-09", "2026-11-11", "2026-11-12"):
            con.execute("INSERT INTO telemetry_days (site_id, day_local, data) VALUES (?,?,?)", (sid, day, '{"completeness": 1.0}'))
        con.execute("INSERT INTO outage_schedules (id, site_id, fetched_at, source, slots, hash) VALUES (?,?,?,?,?,?)",
                    ("s1", sid, "2026-11-09T00:00:00+00:00", "manual", '[["17:00", "19:00", "daily"]]', "a"))
        con.execute("INSERT INTO outage_schedules (id, site_id, fetched_at, source, slots, hash) VALUES (?,?,?,?,?,?)",
                    ("s2", sid, "2026-11-11T00:00:00+00:00", "manual", '[["08:00", "10:00", "daily"]]', "b"))
    _issue(sid, "load", bt)
    _issue(sid, "outage", bt)
    past = forecast.page(site, "load", bt)
    assert past["verified_days"] == 2 and forecast.page(site, "load")["verified_days"] == 4
    out = forecast.page(site, "outage", bt)
    starts = sorted({datetime.fromtimestamp(w["from"], PKT).strftime("%H:%M") for w in out["announced"]})
    assert starts == ["17:00"], starts
    assert sorted({datetime.fromtimestamp(w["from"], PKT).strftime("%H:%M") for w in forecast.page(site, "outage")["announced"]}) == ["08:00"]


@pytest.fixture()
def api_world(tmp_path, monkeypatch):
    """Two company files of their own, each with a grid site, and a client per role of the first company."""
    import types

    import identities
    from gridlytics import platform as platformdb, sites
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "workspace.db")
    made = {}
    for name in ("K20 company", "K20 other company"):
        co = identities.company(name, own_file=True)
        db = platformdb.company_file(co)
        db.parent.mkdir(parents=True, exist_ok=True)
        with store.use_company(db):
            sid = sites.create({"name": "Forecast mill", "disco": "FESCO"}, "test")["id"]
            sites.put_connection(sid, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400}, "test")
            sites.put_asset(sid, "pv", {"kwp_dc": 200, "kw_ac": 180}, "test")
        made[name] = (co, db, sid)
    co, db, sid = made["K20 company"]
    clients = {role: identities.client(f"{role}@k20.test", None if role == "admin" else role, co, "owner" if role == "admin" else "member",
                                       raise_server_exceptions=False) for role in ("admin", "operator", "analyst", "viewer")}
    return types.SimpleNamespace(db=db, site=sid, other=made["K20 other company"], c=clients, use=lambda: store.use_company(db))


def test_every_role_opens_a_past_issue_by_the_plan_s_belief_time_or_the_page_s(api_world):
    w = api_world
    bt = forecast.align(int(datetime.now(timezone.utc).timestamp())) - 3600
    with w.use():
        _issue(w.site, "pv", bt)
        _issue(w.site, "pv", bt + 3600)
    iso_utc = datetime.fromtimestamp(bt, timezone.utc).isoformat(timespec="seconds")     # a plan's belief_time, as plans answer it
    karachi = datetime.fromtimestamp(bt, PKT).strftime("%Y-%m-%dT%H:%M")                 # no offset: Karachi time
    for role in ("admin", "operator", "analyst", "viewer"):
        for at in (iso_utc, str(bt), karachi, iso_utc.replace("+00:00", "Z")):
            r = w.c[role].get(f"/api/sites/{w.site}/forecasts/pv", params={"at": at})
            assert r.status_code == 200, (role, at, r.text[:200])
            body = r.json()
            assert body["belief_time"] == bt and body["exact"] is True and body["latest_belief_time"] == bt + 3600, (role, at)
            assert body["issue"]["daily_kwh"] and body["stage_progress"] is None, (role, at)


@pytest.mark.parametrize("at,words", [
    ("yesterday", "The time of a forecast is a date and time such as 2026-11-12T14:15:00+05:00, or seconds since 1970"),
    ("١٧٠٠٠٠٠٠٠٠", "The time of a forecast is a date and time such as 2026-11-12T14:15:00+05:00, or seconds since 1970"),
    ("99999999999", "The time of a forecast is a date and time such as 2026-11-12T14:15:00+05:00, or seconds since 1970"),
    ("2026-02-30T10:00", "The time of a forecast is a date and time such as 2026-11-12T14:15:00+05:00, or seconds since 1970"),
    (" 1700000000", "The time of a forecast is a date and time such as 2026-11-12T14:15:00+05:00, or seconds since 1970"),
    ("2026-11-12T14:15:00 05:00", "The time of a forecast is a date and time such as 2026-11-12T14:15:00+05:00, or seconds since 1970"),
    ("2999-01-01T00:00:00+05:00", "The time of a forecast must not be in the future"),
])
def test_a_time_in_no_form_or_still_to_come_is_refused_in_one_sentence(api_world, at, words):
    r = api_world.c["viewer"].get(f"/api/sites/{api_world.site}/forecasts/pv", params={"at": at})
    assert r.status_code == 422 and r.json()["detail"] == words and r.json()["code"] == "invalid", r.text


def test_a_time_before_every_kept_issue_says_so_and_another_company_s_site_is_not_found(api_world):
    w = api_world
    with w.use():
        _issue(w.site, "load", _ts(2026, 11, 12, 14, 30))
    r = w.c["viewer"].get(f"/api/sites/{w.site}/forecasts/load", params={"at": "2026-11-12T14:15:00+05:00"})
    assert r.status_code == 404 and r.json()["code"] == "no_issue", r.text
    assert r.json()["detail"] == "No load forecast issued at or before 14:15 on 12 Nov 2026 is kept"
    _, other_db, other_site = w.other
    with store.use_company(other_db):
        _issue(other_site, "load", _ts(2026, 11, 12, 14))
    for role in ("admin", "viewer"):
        r = w.c[role].get(f"/api/sites/{other_site}/forecasts/load", params={"at": "2026-11-12T14:15:00+05:00"})
        assert r.status_code == 404 and "load" not in r.text.lower().replace("no such site", ""), (role, r.text)
