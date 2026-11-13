"""UX backend ask K9 (plan H11; C19.2 item 5, C4.5, R8 2.2; step 9a, Set-up A's source test, and Data and devices).

`POST /api/sites/{id}/source/test` gains `sample`: the newest five minutes received, oldest first, each with its moment in Pakistan time and the
value of every point the test lists, so the result card can show "first rows" beside the points. The test's own moments (`first_at`, `last_at`) are
in Pakistan time too, where they were UTC."""
from __future__ import annotations

from datetime import datetime

from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import telemetry


def test_the_newest_five_minutes_received_with_every_point_the_test_lists(world):
    sid, op = world.site, world.c["operator"]
    with world.use():
        now = telemetry._now_ts() // 60 * 60
        rows = [{"ts": now - 60 * i, "grid_present": 1, "pv_power_w": 900 + i, "load_power_w": 1500, "battery_soc_bms_pct": 62.5}
                for i in range(1, 31)]
        rows[0] = {**rows[0], "grid_present": 0, "battery_soc_bms_pct": None}        # the newest minute: an outage, and no state of charge read
        telemetry.ingest(sid, rows, "csv")
    t = op.post(f"/api/sites/{sid}/source/test").json()
    assert (t["grid_signal"], t["rows"]) == ("found", 30), t
    assert t["points"] == ["grid_present", "pv_power_w", "load_power_w", "battery_soc_bms_pct"]
    sample = t["sample"]
    assert len(sample) == 5
    # oldest first, ending on the newest minute, each moment in Pakistan time
    assert [datetime.fromisoformat(r["at"]).timestamp() for r in sample] == [now - 60 * i for i in range(5, 0, -1)]
    assert all(r["at"].endswith("+05:00") for r in sample) and sample[-1]["at"] == t["last_at"]
    assert t["first_at"].endswith("+05:00") and datetime.fromisoformat(t["first_at"]).timestamp() == now - 60 * 30
    # every point the test lists, in the units it is stored in (W, %), a yes or no for grid presence, null where the minute has none
    assert all(list(r["values"]) == t["points"] for r in sample)
    assert sample[0]["values"] == {"grid_present": True, "pv_power_w": 905.0, "load_power_w": 1500.0, "battery_soc_bms_pct": 62.5}
    assert sample[-1]["values"] == {"grid_present": False, "pv_power_w": 901.0, "load_power_w": 1500.0, "battery_soc_bms_pct": None}


def test_fewer_minutes_than_five_are_shown_whole_and_none_says_none(world):
    sid, a = world.site, world.c["admin"]
    t = a.post(f"/api/sites/{sid}/source/test").json()
    assert (t["grid_signal"], t["sample"]) == ("no_data", []), t
    with world.use():
        now = telemetry._now_ts() // 60 * 60
        telemetry.ingest(sid, [{"ts": now - 120, "grid_voltage_v": 231.4, "pv_power_w": 0}, {"ts": now - 60, "grid_voltage_v": 229.0, "pv_power_w": 0}],
                         "csv")
    t = a.post(f"/api/sites/{sid}/source/test").json()
    assert len(t["sample"]) == 2 and "grid_voltage_v" in t["points"], t
    assert [r["values"]["grid_voltage_v"] for r in t["sample"]] == [231.4, 229.0]


def test_the_test_and_its_rows_are_for_a_role_that_edits_the_site(world):
    for role, want in (("admin", 200), ("operator", 200), ("analyst", 403), ("viewer", 403)):
        assert world.c[role].post(f"/api/sites/{world.site}/source/test").status_code == want, role
