"""UX backend ask K25 (plan H11; E6 item 7): the telemetry CSV leads with Karachi time.

The owner's rule (Asia/Karachi for every date and time meant for people) and [req] 1 ("exports use ISO 8601 with offset"): `GET
/sites/:id/telemetry/export.csv` wrote UTC only (`ts_utc`). Its first column is now `ts_karachi`, ISO 8601 with +05:00 ("2026-11-11T14:16:00+05:00"),
and `ts_utc` follows it for machines; the stored series follow as before. The open question Q58 ("The telemetry CSV leads with Karachi time?") ships
its recommended answer, yes."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from gridlytics import store, telemetry
from test_fix_api import world  # noqa: F401  (world is a fixture)
from test_fix_data import _site, company  # noqa: F401  (company is a fixture)

PKT = timezone(timedelta(hours=5))


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_the_export_leads_with_karachi_time_and_keeps_utc_beside_it(company):
    sid = _site("Export")
    # 20:30 UTC on 1 Nov is 01:30 on 2 Nov in Karachi: the day moves with the offset
    t = int(datetime(2026, 11, 1, 20, 30, tzinfo=timezone.utc).timestamp())
    telemetry.ingest(sid, [{"ts": t + 60 * i, "grid_present": 1, "load_power_w": 1000.0 + i} for i in range(3)], "edge")
    rows = _rows(telemetry.export_csv(sid, t, t + 3600))
    assert rows[0][:3] == ["ts_karachi", "ts_utc", "grid_present"]
    assert rows[0][2:] == list(telemetry.COLUMNS[3:])            # the stored series follow, in their order, as before
    assert [r[:2] for r in rows[1:]] == [["2026-11-02T01:30:00+05:00", "2026-11-01T20:30:00Z"],
                                          ["2026-11-02T01:31:00+05:00", "2026-11-01T20:31:00Z"],
                                          ["2026-11-02T01:32:00+05:00", "2026-11-01T20:32:00Z"]]
    assert rows[1][rows[0].index("load_power_w")] == "1000.0"
    # both columns name the same instant
    for r in rows[1:]:
        assert datetime.fromisoformat(r[0]) == datetime.fromisoformat(r[1].replace("Z", "+00:00"))
    # an empty range still says what its columns are
    assert _rows(telemetry.export_csv(sid, t - 7200, t - 3600)) == [rows[0]]


def test_the_export_route_answers_the_karachi_column_first_and_is_still_audited(world):
    sid = world.site
    t = int(datetime(2026, 11, 11, 14, 16, tzinfo=PKT).timestamp())
    with world.use():
        telemetry.ingest(sid, [{"ts": t, "grid_present": 1, "pv_power_w": 4000.0}], "edge")
    r = world.c["analyst"].get(f"/api/sites/{sid}/telemetry/export.csv", params={"start": str(t - 60), "end": str(t + 60)})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    rows = _rows(r.text)
    assert rows[0][:2] == ["ts_karachi", "ts_utc"]
    assert rows[1][:2] == ["2026-11-11T14:16:00+05:00", "2026-11-11T09:16:00Z"]
    with world.use():
        assert any(a["action"] == "exported telemetry" for a in store.list_audit())
    # who may export is unchanged: an operator and a viewer are refused
    for role in ("operator", "viewer"):
        assert world.c[role].get(f"/api/sites/{sid}/telemetry/export.csv").status_code == 403, role
