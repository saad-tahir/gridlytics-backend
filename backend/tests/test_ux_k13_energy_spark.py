"""UX backend ask K13 (plan H11; D9.3, D1.7; step 8, Fleet): each day of a fleet row's `spark` (and of the Overview's, the same function's) carries
the energy it claims beside its rupees: `peak_kwh_avoided` (the grid import moved out of the peak window against the native replay) and
`litres_saved` (the fuel avoided, in the genset's unit), by the one rule of the month's `mtd_peak_kwh_avoided` and `mtd_litres_saved`: each bar is
its day's figure to 0.1, the month's the sum of its days to 0.1, so the bars of the month's days shown add up to their days' share of it to within
rounding (the spark shows the last 7 scored days, the month figure every claiming day of the month). A day that claims nothing reads 0, as its
`claimed_rs` does; a site without a grid or a genset reads null. No money: the Energy form's 7-day bars are drawn for every reader, a viewer's
included."""
from __future__ import annotations

from datetime import timedelta

from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_fix_r7_fleet_overview import _claimed_day, _put_days, _row

from gridlytics.routes_common import today


def test_each_day_s_bar_carries_the_energy_it_claims_and_the_month_s_bars_add_up_to_its_figure(world):
    a, v, sid = world.c["admin"], world.c["viewer"], world.site               # Mill One: a grid connection and a diesel genset
    t = today()
    days = [t - timedelta(days=k) for k in range(3, -1, -1)]
    # figures that do not round cleanly to 0.1, so the bars and the month figure are each rounded once, on their own (K13 check)
    stored = {days[0].isoformat(): _claimed_day(700.0, 400.0, 520.04, 12.5),                             # claims 120.04 kWh and 12.5 L
              days[1].isoformat(): _claimed_day(500.0, 100.0, 900.0, 30.0, fidelity="recommendation_only"),   # claims nothing
              days[2].isoformat(): {**_claimed_day(300.0, 50.0, 80.0, 4.0), "claim_withheld": "import_unknown", "claimed_rs": 0.0},  # withheld
              days[3].isoformat(): _claimed_day(200.0, 200.0, 245.54, None)}                              # an unmetered genset's litres: unknown
    with world.use():
        _put_days(sid, stored)
    want = [(days[0].isoformat(), 120.0, 12.5), (days[1].isoformat(), 0.0, 0.0), (days[2].isoformat(), 0.0, 0.0), (days[3].isoformat(), 45.5, None)]
    for c in (a, v):
        fleet = c.get("/api/fleet").json()
        spark = _row(fleet, sid)["spark"]
        assert [(p["day"], p["peak_kwh_avoided"], p["litres_saved"]) for p in spark] == want
        assert c.get(f"/api/sites/{sid}/now").json()["spark"] == spark            # the Overview's spark is the same function's
        # one rule: the month's figure is its claiming days' sum rounded once (120.04 + 45.54 = 165.58, 165.6), each bar its day's rounded
        # (120.0, 45.5): the bars of this month's days come to the month's figure to within 0.05 kWh a bar, not exactly
        first = t.replace(day=1).isoformat()
        month = [p["peak_kwh_avoided"] for p in spark if p["day"] >= first]
        claimed_in_month = [120.04 if days[0] >= t.replace(day=1) else 0.0, 45.54]
        assert _row(fleet, sid)["mtd_peak_kwh_avoided"] == round(sum(claimed_in_month), 1)
        assert abs(sum(month) - _row(fleet, sid)["mtd_peak_kwh_avoided"]) <= 0.05 * len(month) + 1e-9
    # a viewer's bars are energy only: no rupee beside them
    assert [set(p) for p in _row(v.get("/api/fleet").json(), sid)["spark"]] == [{"day", "peak_kwh_avoided", "litres_saved"}] * 4
    assert [p["claimed_rs"] for p in _row(a.get("/api/fleet").json(), sid)["spark"]] == [700.0, 0.0, 0.0, 200.0]
    # no genset: no litres, a claiming day's or not; off-grid: no peak import
    with world.use():
        _put_days(world.other, {days[0].isoformat(): _claimed_day(50.0, 10.0, 30.0, 2.0),
                                days[1].isoformat(): _claimed_day(0.0, 0.0, 0.0, 0.0, fidelity="recommendation_only")})
    spark = _row(a.get("/api/fleet").json(), world.other)["spark"]
    assert [(p["peak_kwh_avoided"], p["litres_saved"]) for p in spark] == [(20.0, None), (0.0, None)]
    assert a.put(f"/api/sites/{world.other}/connection", json={"regime": "off_grid"}).status_code == 200
    spark = _row(a.get("/api/fleet").json(), world.other)["spark"]
    assert [(p["peak_kwh_avoided"], p["litres_saved"]) for p in spark] == [(None, None), (None, None)]


def test_the_spark_shows_the_last_7_scored_days_while_the_month_figure_counts_every_claiming_day(world, monkeypatch):
    """K13 check: the contract said this month's bars add up to the month's figure. From the 8th scored day of a month they cannot: the spark is the
    last 7 scored days, and each bar is rounded on its own. The rule is one, over the days each shows."""
    from datetime import date

    from gridlytics import routes_sites
    a, sid = world.c["admin"], world.site
    t = date(2026, 10, 12)
    monkeypatch.setattr(routes_sites, "today", lambda: t)
    # ten claiming days, 1 to 10 Oct, each 10.04 kWh moved off the peak and 1.26 L avoided
    with world.use():
        _put_days(sid, {date(2026, 10, k).isoformat(): _claimed_day(100.0, 100.0, 110.04, 1.26) for k in range(1, 11)})
    row = _row(a.get("/api/fleet").json(), sid)
    assert [p["day"] for p in row["spark"]] == [date(2026, 10, k).isoformat() for k in range(4, 11)]
    assert {(p["peak_kwh_avoided"], p["litres_saved"]) for p in row["spark"]} == {(10.0, 1.3)}
    assert (row["mtd_peak_kwh_avoided"], row["mtd_litres_saved"]) == (100.4, 12.6)              # every claiming day, summed and then rounded
    assert a.get(f"/api/sites/{sid}/now").json()["spark"] == row["spark"]
