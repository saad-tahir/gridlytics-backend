"""UX backend ask K13 (plan H11; D9.3, D1.7; step 8, Fleet): each day of a fleet row's `spark` (and of the Overview's, the same function's) carries
the energy it claims beside its rupees: `peak_kwh_avoided` (the grid import moved out of the peak window against the native replay) and
`litres_saved` (the fuel avoided, in the genset's unit), by the one rule of the month's `mtd_peak_kwh_avoided` and `mtd_litres_saved`, so the
month's bars add up to its figure. A day that claims nothing reads 0, as its `claimed_rs` does; a site without a grid or a genset reads null. No money:
the Energy form's 7-day bars are drawn for every reader, a viewer's included."""
from __future__ import annotations

from datetime import timedelta

from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_fix_r7_fleet_overview import _claimed_day, _put_days, _row

from gridlytics.routes_common import today


def test_each_day_s_bar_carries_the_energy_it_claims_and_the_month_s_bars_add_up_to_its_figure(world):
    a, v, sid = world.c["admin"], world.c["viewer"], world.site               # Mill One: a grid connection and a diesel genset
    t = today()
    days = [t - timedelta(days=k) for k in range(3, -1, -1)]
    stored = {days[0].isoformat(): _claimed_day(700.0, 400.0, 520.0, 12.5),                              # claims 120 kWh and 12.5 L
              days[1].isoformat(): _claimed_day(500.0, 100.0, 900.0, 30.0, fidelity="recommendation_only"),   # claims nothing
              days[2].isoformat(): {**_claimed_day(300.0, 50.0, 80.0, 4.0), "claim_withheld": "import_unknown", "claimed_rs": 0.0},  # withheld
              days[3].isoformat(): _claimed_day(200.0, 200.0, 245.5, None)}                               # an unmetered genset's litres: unknown
    with world.use():
        _put_days(sid, stored)
    want = [(days[0].isoformat(), 120.0, 12.5), (days[1].isoformat(), 0.0, 0.0), (days[2].isoformat(), 0.0, 0.0), (days[3].isoformat(), 45.5, None)]
    for c in (a, v):
        fleet = c.get("/api/fleet").json()
        spark = _row(fleet, sid)["spark"]
        assert [(p["day"], p["peak_kwh_avoided"], p["litres_saved"]) for p in spark] == want
        assert c.get(f"/api/sites/{sid}/now").json()["spark"] == spark            # the Overview's spark is the same function's
        # the bars of this month's days add up to the month's figure (one rule)
        first = t.replace(day=1).isoformat()
        assert round(sum(p["peak_kwh_avoided"] for p in spark if p["day"] >= first), 1) == _row(fleet, sid)["mtd_peak_kwh_avoided"]
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
