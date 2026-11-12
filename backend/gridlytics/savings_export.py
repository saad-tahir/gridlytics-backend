"""The claim columns of the savings exports (plan 8.5: "the data behind every figure downloadable as CSV with the tariff version, the capture version
and the plan ids, so a dispute can be re-run"; round 7, audit pages-D-06 with engine-savings-07, flow-savings-pilot-bill-16, engine-money-live-13 and
flow-reports-alerts-members-keys-04, and the export half of engine-savings-06).

GET /sites/:id/savings/export.csv and the monthly savings report's CSV both write these columns from verify.summary's day rows, so each adds up to the
page: `claimed_native_rs` is the day's part of the page's claim (its claim less the carry-over no later day has used yet, with its hand-over's change
and its share of its billing month's MDI lever; the column sums to `baselines.native.claimed_rs`), and the saving column plus `mdi_share_native_rs`
over the days whose saving is known sums to `baselines.native.saving_rs`. A day that claims nothing says why (`not_claimed_reason`: the reason its
claim is withheld for missing data, or its fidelity: not applied, pending, a native block, a recommendation), and a withheld day which levers it still
priced. Each day names the tariff version it was priced at with the day that version took effect, its native capture version and the plans in force
over its Pakistan day (`plan_ids`). Money: both exports are for a role with `export`; the report's money columns only in the monthly savings report.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from . import store, twin

#: The ledger's levers per day (the month's MDI lever is shared out by day in `mdi_share_native_rs`)
LEVERS = ("peak_window", "offpeak_energy", "export", "genset", "wear", "carry_over")

#: The claim columns, in order, after each export's own columns
CLAIM_COLUMNS = ["claims", "claimed_native_rs", "not_claimed_reason", "levers_kept", "carry_over_provisional_native_rs", "mdi_month",
                 "mdi_share_native_rs", "mdi_provisional", *(f"{k}_native_rs" for k in LEVERS), "peak_kw_native", "tariff_version",
                 "tariff_version_from", "native_capture_version"]


def cell(v):
    """A value as the CSV writes it: blank for none, true or false for a flag."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def withheld(day: dict) -> str | None:
    """Why a priced day's claim is withheld for missing data (verify's `claim_withheld`; an unknown import on a row stored before it had one)."""
    return day.get("claim_withheld") or ("import_unknown" if day.get("import_unknown") else None)


def tariff_dates(days: list[dict]) -> dict[str, str]:
    """The day each tariff version the days were priced at took effect ("YYYY-MM-DD"), by its id."""
    ids = sorted({str(d["tariff_version"]) for d in days if d.get("tariff_version")})
    if not ids:
        return {}
    with store.connect() as con:
        rows = con.execute(f"SELECT id, effective_from FROM tariff_versions WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: str(r["effective_from"])[:10] for r in rows}


def claim_cells(day: dict, dates: dict[str, str]) -> list:
    """A day's claim columns (CLAIM_COLUMNS) from its verify.summary row; `dates` from tariff_dates."""
    levers = day.get("levers") or {}
    kept = day.get("levers_kept")
    return [cell(v) for v in (
        bool(day.get("claims")), day.get("claimed_in_total_native_rs"),
        day.get("not_claimed_reason"), ";".join(kept) if kept else None, day.get("carry_over_provisional_native_rs"), day.get("mdi_month"),
        day.get("mdi_share_native_rs"), day.get("mdi_provisional"), *(levers.get(k) for k in LEVERS), day.get("peak_kw_native"),
        day.get("tariff_version"), dates.get(str(day.get("tariff_version") or "")), day.get("native_capture_version"))]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


def plan_ids(site_id: str, d0: str, d1: str) -> dict[str, list[str]]:
    """The plans in force over each Pakistan day d0..d1 ("YYYY-MM-DD"), oldest first: the plan in force when the day began (the latest made before
    its midnight in Pakistan time) and every plan made during it; a failed or held run is no plan in force (flow-reports-alerts-members-keys-04:
    the report listed the first four plans whose UTC date was the day, missing those made before 05:00 PKT and counting the evening's under the
    wrong day)."""
    first, last = date.fromisoformat(d0[:10]), date.fromisoformat(d1[:10])
    if last < first:
        return {}
    t0, t1 = twin.day_start_utc(first), twin.day_start_utc(last + timedelta(days=1))
    ok = "status IN ('active', 'superseded')"
    with store.connect() as con:
        start = con.execute(f"SELECT id FROM plans WHERE site_id = ? AND {ok} AND belief_time <= ? ORDER BY belief_time DESC, generated_at DESC, "
                            "rowid DESC LIMIT 1", (site_id, _iso(t0))).fetchone()
        made = con.execute(f"SELECT id, belief_time FROM plans WHERE site_id = ? AND {ok} AND belief_time > ? AND belief_time < ? "
                           "ORDER BY belief_time, generated_at, rowid", (site_id, _iso(t0), _iso(t1))).fetchall()
    current = start["id"] if start else None
    out: dict[str, list[str]] = {}
    i, d = 0, first
    while d <= last:
        begin, end = _iso(twin.day_start_utc(d)), _iso(twin.day_start_utc(d + timedelta(days=1)))
        # the plan of before is in force at midnight, unless one was made at midnight itself
        ids = [current] if current and not (i < len(made) and made[i]["belief_time"] == begin) else []
        while i < len(made) and made[i]["belief_time"] < end:
            ids.append(made[i]["id"])
            current = made[i]["id"]
            i += 1
        out[d.isoformat()] = ids
        d += timedelta(days=1)
    return out
