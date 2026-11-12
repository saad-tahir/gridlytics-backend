"""Contract test 8 (EMS_BUILD_PLAN.md Part 7.4, U3 5.8 item 8; tracker DAT-03): telemetry completeness and quality flags.

Thirty simulated days of the twin's one-minute telemetry, sent as an edge sends it (a batch a day here, U3 5.2's rows with nulls for missing
samples and quality_flags per U3 1.2): at least 99.5 percent of minutes carry every reading at the source, and every minute is present in the
cloud or flagged, with its flags kept as sent and each day's completeness at 100 %. And DAT-03's own acceptance: batches from 50 twins within a
minute read platform.db once per key (the 60-second cache of key to company) and trip no address limiter, though they all come from one address.
Round 7, audit tests-and-perf-15."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from contract_kit import PKT, closed_loop, edge, iso, now  # noqa: F401  (edge is a fixture)
from gridlytics import api, sites, telemetry, twin
from gridlytics import platform as platformdb
from test_fix_api import world  # noqa: F401  (world is a fixture)

#: the readings each minute carries (U3 1.2's keys the cloud keeps), in telemetry.COLUMNS' order as twin.telemetry_rows writes them
SENT = [c for c in telemetry.COLUMNS[3:] if c not in ("grid_import_wh", "grid_export_wh", "battery_soc_est_pct", "genset_fuel_l", "unserved_w", "curtail_w")]
PARTIAL_MINUTE, STALE = 0x40, 0x02                                             # U3 1.2's flags: fewer than half the samples; the last value repeated


def test_thirty_simulated_days_arrive_with_every_minute_present_or_flagged_and_99_5_percent_complete_at_the_source(edge):
    with edge.use():
        site = sites.full(edge.sid)
    plant = twin.plant_of(site)
    today = datetime.now(PKT).date()
    rng = np.random.default_rng(30)
    soc, sent_rows, flagged, missing = 0.5, 0, 0, 0
    for n, day in enumerate(today - timedelta(days=30 - k) for k in range(30)):
        tr = twin.truth(site, day)
        sim = twin.simulate(tr, twin.native_policy(site, tr["ts"]), plant, soc)
        soc = float(sim["soc"][-1])
        names = list(telemetry.COLUMNS[3:])
        rows = []
        for r in twin.telemetry_rows(edge.sid, tr, sim, rng_seed=n):
            vals = dict(zip(names, r[3:]))
            rows.append([int(r[1]), *[vals[c] for c in SENT], 0])
        # the source's faults: a minute in 250 with too few samples (nulls, flagged partial), one in 500 a repeated value (flagged stale)
        for i in np.flatnonzero(rng.random(len(rows)) < 1 / 250):
            rows[i] = [rows[i][0], *[None] * len(SENT), PARTIAL_MINUTE]
            missing += 1
        for i in np.flatnonzero(rng.random(len(rows)) < 1 / 500):
            if rows[i][-1] == 0:
                rows[i][-1] = STALE
        flagged += sum(1 for r in rows if r[-1])
        sent_rows += len(rows)
        r = edge.post("telemetry", {"contract": "ems-edge/1.0", "msg_type": "telemetry", "seq": n + 1, "sent_at": iso(now()),
                                    "columns": ["ts", *SENT, "quality_flags"], "rows": rows})
        assert r.status_code == 200, (day, r.text[:300])
    assert sent_rows == 30 * 1440 and 1 - missing / sent_rows >= 0.995, missing   # 99.5 % at the source
    t0 = int(datetime(today.year, today.month, today.day, tzinfo=PKT).timestamp()) - 30 * 86400
    with edge.use():
        got = telemetry.raw(edge.sid, t0, t0 + 30 * 86400, columns=(*SENT, "quality_flags"))
        for k in range(30, 0, -1):                                              # the nightly job's day rows, which the quality strips read
            telemetry.aggregate_day(edge.sid, today - timedelta(days=k))
        days = telemetry.quality_days(edge.sid, today - timedelta(days=30), today - timedelta(days=1), now())
    ts = got["ts"]
    assert len(ts) == 30 * 1440 and np.all(np.diff(ts) == 60)                  # every minute present, once, in order
    flags = np.array([int(f or 0) for f in got["quality_flags"]])
    complete = np.all([~np.isnan(np.asarray(got[c], dtype=float)) for c in SENT if c != "inverter_mode_active"], axis=0)
    assert np.all(complete | (flags != 0))                                     # every minute present with its readings, or flagged
    assert int((~complete).sum()) == missing and np.all(flags[~complete] == PARTIAL_MINUTE)
    assert int((flags != 0).sum()) == flagged and complete.mean() >= 0.995
    assert len(days) == 30 and all(d["completeness"] == 1.0 for d in days), days[:3]


def test_fifty_twins_batches_in_a_minute_read_platform_db_once_per_key_and_trip_no_address_limiter(edge, monkeypatch):
    w = edge.world
    with w.use():
        ids = [edge.sid] + [sites.create({"name": f"Twin {k}", "disco": "LESCO"}, "t")["id"] for k in range(49)]
        for sid in ids:
            closed_loop(sid)
    keys = {sid: w.key(["telemetry:write"], site=sid) for sid in ids}
    reads = []
    find = platformdb.index_find
    monkeypatch.setattr(platformdb, "index_find", lambda *a, **k: reads.append(a) or find(*a, **k))
    clock = {"t": 1000.0}
    monkeypatch.setattr(api.time, "monotonic", lambda: clock["t"])
    api._KEY_CACHE.clear()
    m = now() // 60 * 60 - 120

    def minute(seq: int) -> list[int]:
        codes = []
        for sid in ids:
            body = {"contract": "ems-edge/1.0", "seq": seq, "sent_at": iso(now()), "columns": ["ts", "grid_present", "pv_power_w"],
                    "rows": [[m + 60 * seq, 1, 900]]}
            codes.append(w.anon.post(f"/api/edge/{sid}/telemetry", headers=keys[sid], json=body).status_code)
            codes.append(w.anon.post(f"/api/edge/{sid}/heartbeat", headers=keys[sid],
                                     json={"contract": "ems-edge/1.0", "seq": seq, "sent_at": iso(now()), "uptime_s": 60 * seq}).status_code)
        return codes

    assert set(minute(1)) == {200}                                              # 100 messages from one address: no 429
    assert len(reads) == 50                                                     # each key's company read once
    clock["t"] += 50                                                            # the next batches, within the cache's minute
    assert set(minute(2)) == {200} and len(reads) == 50                         # no platform.db read
