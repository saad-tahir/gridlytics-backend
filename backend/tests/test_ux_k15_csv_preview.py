"""UX backend ask K15 (plan H11; D9.5, C11.4 stage 2 "HOW THE FIRST ROWS WILL BE READ"; step 9d, Set-up › Data and devices; owner's question Q47).

`POST /api/sites/{id}/telemetry/mapping/preview` reads a file's head (the first 64 KB the page already sends for the guess) with the column mapping
as the person has set it, and answers its first five rows as the upload would store them: each minute in Karachi time, each key in its stored unit
and the product's sign (battery discharge and grid import positive), the grid's and the genset's state as true or false, an impossible reading
blank. Nothing is written: no minute, no import, no mapping, no audit line. The head is read by the upload's own rules (one function), so what the
preview refuses the upload refuses in the same words, and a file the preview reads is stored as the preview said."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import identities
from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import sites, store, telemetry
from gridlytics import platform as platformdb

PKT = timezone(timedelta(hours=5))
URL = "/api/sites/{}/telemetry/mapping/preview"
#: A vendor's logger file: day-first dates in Karachi time, power in kW with the grid's sign the other way (export positive), the grid's state as on/off
VENDOR = ("Date Time,Grid,P_grid,Load,PV,Batt,SoC %\n"
          "01/03/2026 00:00,on,-1.2,30.1,0,3.4,62\n"
          "01/03/2026 00:01,on,-1.0,30.0,0,3.5,61.5\n"
          "01/03/2026 00:02,off,0,29.8,0,30.2,60\n"
          "01/03/2026 00:03,off,0,29.9,0,30.0,58.5\n"
          "01/03/2026 00:04,on,-2.5,30.2,0,0,58\n"
          "01/03/2026 00:05,on,-2.4,30.1,0,0,58\n")
MAPPING = {"columns": {"Date Time": {"key": "ts"}, "Grid": {"key": "grid_present"},
                       "P_grid": {"key": "grid_power_w", "unit": "kW", "reverse": True}, "Load": {"key": "load_power_w", "unit": "kW"},
                       "PV": {"key": "pv_power_w", "unit": "kW"}, "Batt": {"key": "battery_power_w", "unit": "kW"},
                       "SoC %": {"key": "battery_soc_bms_pct", "unit": "%"}},
           "ts_format": "dmy"}


def _preview(w, body, role="operator", site=None):
    return w.c[role].post(URL.format(site or w.site), json=body)


def _nothing_written(w, sid=None):
    sid = sid or w.site
    with w.use():
        assert telemetry.latest(sid) is None
        assert telemetry.imports(sid) == []
        assert telemetry.saved_mapping(sid) is None
        assert not [a["action"] for a in store.list_audit() if "telemetry" in a["action"] or "CSV" in a["action"]]


def test_the_first_five_rows_come_back_in_karachi_time_and_product_units_and_nothing_is_written(world):
    r = _preview(world, {"head": VENDOR, "mapping": MAPPING, "tz_offset_h": 5})
    assert r.status_code == 200, r.text
    got = r.json()
    rows = got["rows"]
    assert len(rows) == 5 and [x["at"] for x in rows] == [f"2026-03-01T00:0{i}:00+05:00" for i in range(5)], rows
    assert rows[0]["values"] == {"grid_present": True, "grid_power_w": 1200.0, "load_power_w": 30100.0, "pv_power_w": 0.0, "battery_power_w": 3400.0,
                                 "battery_soc_bms_pct": 62.0}, rows[0]
    assert rows[2]["values"]["grid_present"] is False and rows[2]["values"]["battery_power_w"] == 30200.0
    assert "-0.0" not in r.text                                                       # a reversed nought reads 0
    assert rows[4]["values"]["grid_power_w"] == 2500.0
    assert got["keys"] == ["grid_present", "grid_power_w", "load_power_w", "pv_power_w", "battery_power_w", "battery_soc_bms_pct"]
    assert (got["read"], got["skipped"], got["first_unreadable"], got["step_min"], got["ts_format"]) == (6, 0, None, 1, "dmy"), got
    assert any("inverter_mode_active" in n for n in got["notes"]), got["notes"]       # monitor only, as the upload notes it
    assert got["warnings"] == []
    _nothing_written(world)


def test_the_preview_shows_a_slip_of_the_mapping_at_once(world):
    """A reversed sign shows as the grid exporting; watts read as kilowatts as a thousand times the figure (plan C11.4)."""
    plain = {**MAPPING, "columns": {**MAPPING["columns"], "P_grid": {"key": "grid_power_w", "unit": "kW"}}}
    assert _preview(world, {"head": VENDOR, "mapping": plain, "tz_offset_h": 5}).json()["rows"][0]["values"]["grid_power_w"] == -1200.0
    watts = {**MAPPING, "columns": {**MAPPING["columns"], "Load": {"key": "load_power_w", "unit": "W"}}}
    assert _preview(world, {"head": VENDOR, "mapping": watts, "tz_offset_h": 5}).json()["rows"][0]["values"]["load_power_w"] == 30.1
    # UTC chosen for a Karachi file: the same rows five hours later in Karachi time
    utc = _preview(world, {"head": VENDOR, "mapping": MAPPING}).json()
    assert utc["rows"][0]["at"] == "2026-03-01T05:00:00+05:00" and utc["tz_offset_h"] == 0


def test_what_the_preview_reads_is_what_the_upload_stores(world):
    """One rule for both: the file previewed, then uploaded with the same mapping, is stored minute by minute as the preview said."""
    body = {"head": VENDOR, "mapping": MAPPING, "tz_offset_h": 5}
    rows = _preview(world, body).json()["rows"]
    op = world.c["operator"]
    assert op.put(f"/api/sites/{world.site}/telemetry/mapping", json=MAPPING).status_code == 200
    up = op.post(f"/api/sites/{world.site}/telemetry/upload?tz_offset_h=5&mapping=saved", content=VENDOR, headers={"Content-Type": "text/csv"})
    assert up.status_code == 200, up.text
    with world.use():
        t0 = int(datetime(2026, 3, 1, tzinfo=PKT).timestamp())
        stored = telemetry.raw(world.site, t0, t0 + 300, tuple(rows[0]["values"]))
    assert [int(t) for t in stored["ts"]] == [t0 + 60 * i for i in range(5)]
    for i, row in enumerate(rows):
        for key, v in row["values"].items():
            s = float(stored[key][i])
            assert (s >= 0.5) == v if key == "grid_present" else abs(s - v) < 1e-6, (i, key, s, v)


def test_a_head_cut_in_the_middle_of_a_line_reads_its_whole_lines_only(world):
    cut = VENDOR[: VENDOR.index("01/03/2026 00:05") + 12]          # "01/03/2026 0" of the last line
    got = _preview(world, {"head": cut, "mapping": MAPPING, "tz_offset_h": 5}).json()
    assert got["read"] == 5 and got["skipped"] == 0 and len(got["rows"]) == 5, got


def test_impossible_readings_and_unreadable_times_read_as_the_upload_reads_them(world):
    head = ("ts,grid_voltage_v,load_power_w,battery_soc_bms_pct\n"
            "2026-03-01T00:00:00+05:00,231,-9000,62\n"
            "yesterday,230,1000,60\n"
            "2026-03-01T00:01:00+05:00,0,N/A,140\n")
    got = _preview(world, {"head": head}).json()
    assert (got["read"], got["skipped"], got["first_unreadable"]) == (2, 1, "yesterday"), got
    first, second = (x["values"] for x in got["rows"])
    # the grid's state from its voltage (180 V and up), a load below -500 W and a state of charge over 100 % stored as missing
    assert first == {"grid_voltage_v": 231.0, "grid_present": True, "load_power_w": None, "battery_soc_bms_pct": 62.0}, first
    assert second == {"grid_voltage_v": 0.0, "grid_present": False, "load_power_w": None, "battery_soc_bms_pct": None}, second
    assert any("load_power_w cell was below" in w for w in got["warnings"]), got["warnings"]


def test_what_the_upload_refuses_the_preview_refuses_in_the_same_words(world):
    op, sid = world.c["operator"], world.site
    no_ts = "when,grid_present\n2026-03-01 00:00,1\n"
    no_grid = "ts,pv_power_w\n2026-03-01T00:00:00Z,5\n"
    unreadable = "ts,grid_present\nsoon,1\n"
    future = "ts,grid_present\n2099-01-01T00:00:00Z,1\n"
    for head in (no_ts, no_grid, unreadable, future):
        up = op.post(f"/api/sites/{sid}/telemetry/upload", content=head, headers={"Content-Type": "text/csv"})
        pre = _preview(world, {"head": head})
        assert up.status_code == pre.status_code == 422, (head, up.text, pre.text)
        assert pre.json()["detail"] == up.json()["detail"], (pre.json()["detail"], up.json()["detail"])
    twice = {"columns": {"P_grid": {"key": "grid_power_w"}, "Load": {"key": "grid_power_w"}}}
    r = _preview(world, {"head": VENDOR, "mapping": twice})
    assert r.status_code == 422 and r.json()["detail"].startswith("Columns P_grid and Load are both read as grid_power_w"), r.text
    for body, said in (({}, "The start of the file is missing"), ({"head": ""}, "The start of the file is needed"),
                       ({"head": VENDOR, "tz_offset_h": 30}, "The time zone offset (hours from UTC) must be at most 23"),
                       ({"head": VENDOR, "tz_offset_h": "5"}, "The time zone offset (hours from UTC) must be a number"),
                       ({"head": VENDOR, "mapping": {"columns": {"PV": {"key": "pv_power_w", "unit": "kWh"}}}}, "Column PV cannot be read in kWh"),
                       ({"head": VENDOR, "colour": "red"}, "Unknown field 'colour'")):
        r = _preview(world, body)
        assert r.status_code == 422 and said in r.json()["detail"], (body, r.text)
    _nothing_written(world)


def test_only_the_roles_that_upload_preview_and_another_company_s_site_is_no_site_here(world):
    body = {"head": VENDOR, "mapping": MAPPING, "tz_offset_h": 5}
    for role, status in (("admin", 200), ("operator", 200), ("analyst", 403), ("viewer", 403)):
        r = _preview(world, body, role)
        assert r.status_code == status, (role, r.text)
        if status == 403:
            assert "Rs" not in r.json()["detail"], r.text
    assert world.anon.post(URL.format(world.site), json=body, headers=world.key(["telemetry:write", "sites:read"])).status_code == 403
    other = identities.company("Another company", own_file=True)
    with store.use_company(platformdb.company_file(other)):
        theirs = sites.create({"name": "Their Mill", "disco": "LESCO"}, "t")["id"]
    for role in ("admin", "operator"):
        assert _preview(world, body, role).status_code == 200, role
        r = _preview(world, body, role, site=theirs)
        assert (r.status_code, r.json()["detail"]) == (404, "No such site"), (role, r.text)
