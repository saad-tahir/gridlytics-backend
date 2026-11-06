"""The tariff and regime engine and the 1-minute bill engine (plan Part 6; R9 sections 11 to 13).

* **Versions.** Every tariff object (schedule, TOU calendar, monthly adder, tax rules, the ICP, the prosumer regimes) is an effective-dated, immutable row of
  `tariff_versions`, resolved by date (adders by billing month), each with its source and a content hash. A new notification is proposed as `pending` and goes
  live only when an administrator approves it (`approve`), which closes the version before it.
* **The bill engine** (`rebuild`) prices a connection's 1-minute import and export series over a billing period in R9's thirteen steps and returns a line-item
  bill plus the aggregates verification needs. The counterfactual runs the same function on the replayed series; the saving is the difference line by line.
* **The vectors** (`vectors`) give the optimiser, for 192 steps of 15 minutes, the marginal rupee per kWh the bill engine itself would charge, on the
  connection's bearable-cost basis (GST and the s.235 advance excluded where the company recovers them; plan Part 4, the marginal-price rule).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np

from . import store, tariff_data

PKT = timezone(timedelta(hours=5))


def _hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ versions

def ensure_shipped() -> None:
    """Seed the shipped versions and fuel prices into the company's file the first time it is used."""
    with store.connect() as con:
        if con.execute("SELECT COUNT(*) FROM tariff_versions").fetchone()[0] == 0:
            for v in tariff_data.SHIPPED:
                con.execute("INSERT INTO tariff_versions (id, kind, key, effective_from, effective_to, known_from, status, supersedes, body, source, hash, "
                            "created, by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (store.new_id(), v["kind"], v["key"], v["effective_from"], None, v["known_from"], v["status"], None, json.dumps(v["body"]),
                             json.dumps(v["source"]), _hash(v["body"]), store._now(), "shipped"))
        if con.execute("SELECT COUNT(*) FROM fuel_prices").fetchone()[0] == 0:
            for kind, day, price in tariff_data.FUEL_PRICES:
                con.execute("INSERT INTO fuel_prices (kind, valid_from, price, source) VALUES (?,?,?,?)",
                            (kind, day, price, json.dumps(tariff_data.FUEL_SOURCE[kind])))


def _ver(r) -> dict:
    d = dict(r)
    d["body"], d["source"] = json.loads(d["body"]), json.loads(d["source"])
    return d


def versions(kind: str | None = None) -> list[dict]:
    ensure_shipped()
    with store.connect() as con:
        if kind:
            rows = con.execute("SELECT * FROM tariff_versions WHERE kind = ? ORDER BY key, effective_from", (kind,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM tariff_versions ORDER BY kind, key, effective_from").fetchall()
    return [_ver(r) for r in rows]


def resolve(kind: str, key: str, on: date | str) -> dict | None:
    """The active version of (kind, key) in force on a date: the latest effective_from not after it."""
    ensure_shipped()
    day = on.isoformat() if isinstance(on, date) else str(on)[:10]
    with store.connect() as con:
        r = con.execute("SELECT * FROM tariff_versions WHERE kind = ? AND key = ? AND status = 'active' AND effective_from <= ? "
                        "ORDER BY effective_from DESC LIMIT 1", (kind, key, day)).fetchone()
    return _ver(r) if r else None


def propose(kind: str, key: str, effective_from: str, body: dict, source: dict, by: str) -> dict:
    """A new version from a watcher or a person, pending until an administrator approves it."""
    if kind not in ("schedule", "tou", "adder", "tax", "icp", "prosumer"):
        raise ValueError("Unknown tariff kind")
    try:
        date.fromisoformat(effective_from)
    except ValueError:
        raise ValueError("The version's start must be a date (YYYY-MM-DD)") from None
    if not isinstance(body, dict) or not isinstance(source, dict) or source.get("verified") not in ("primary", "secondary", "unverified"):
        raise ValueError("A version carries a body and a source with verified: primary, secondary or unverified")
    ensure_shipped()
    vid = store.new_id()
    with store.connect() as con:
        con.execute("INSERT INTO tariff_versions (id, kind, key, effective_from, effective_to, known_from, status, supersedes, body, source, hash, created, by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (vid, kind, key, effective_from, None, store._now()[:10], "pending", None, json.dumps(body),
                                                         json.dumps(source), _hash(body), store._now(), by))
    store.audit(by, "proposed a tariff version", f"{kind}/{key}", {"effective_from": effective_from})
    return next(v for v in versions(kind) if v["id"] == vid)


def approve(version_id: str, by: str) -> dict:
    """Make a pending version active; the active version of the same key it follows is closed the day before (the only mutation allowed, logged)."""
    with store.connect() as con:
        r = con.execute("SELECT * FROM tariff_versions WHERE id = ?", (version_id,)).fetchone()
        if r is None:
            raise KeyError(version_id)
        if r["status"] not in ("pending", "awaiting_notification"):
            raise ValueError("Only a pending version can be approved")
        prev = con.execute("SELECT id FROM tariff_versions WHERE kind = ? AND key = ? AND status = 'active' AND effective_from < ? "
                           "ORDER BY effective_from DESC LIMIT 1", (r["kind"], r["key"], r["effective_from"])).fetchone()
        day_before = (date.fromisoformat(r["effective_from"]) - timedelta(days=1)).isoformat()
        if prev:
            con.execute("UPDATE tariff_versions SET effective_to = ? WHERE id = ? AND effective_to IS NULL", (day_before, prev["id"]))
        con.execute("UPDATE tariff_versions SET status = 'active', supersedes = ? WHERE id = ?", (prev["id"] if prev else None, version_id))
    store.audit(by, "approved a tariff version", f"{r['kind']}/{r['key']}", {"version_id": version_id, "effective_from": r["effective_from"]})
    return next(v for v in versions(r["kind"]) if v["id"] == version_id)


def fuel_price(kind: str, on: date | str) -> tuple[float, dict]:
    ensure_shipped()
    day = on.isoformat() if isinstance(on, date) else str(on)[:10]
    with store.connect() as con:
        r = con.execute("SELECT price, source FROM fuel_prices WHERE kind = ? AND valid_from <= ? ORDER BY valid_from DESC LIMIT 1", (kind, day)).fetchone()
        if r is None:
            r = con.execute("SELECT price, source FROM fuel_prices WHERE kind = ? ORDER BY valid_from LIMIT 1", (kind,)).fetchone()
    return (float(r["price"]), json.loads(r["source"])) if r else (0.0, {})


def add_fuel_price(kind: str, valid_from: str, price: float, by: str) -> None:
    if kind not in ("hsd", "gas_captive", "lng_captive") or not (0 < price < 100000):
        raise ValueError("A fuel price needs a kind (hsd, gas_captive, lng_captive) and a positive price")
    date.fromisoformat(valid_from)
    with store.connect() as con:
        con.execute("INSERT INTO fuel_prices (kind, valid_from, price, source) VALUES (?,?,?,?) ON CONFLICT(kind, valid_from) DO UPDATE SET "
                    "price = excluded.price, source = excluded.source", (kind, valid_from, price, json.dumps({"kind": "manual", "ref": by, "verified": "primary"})))
    store.audit(by, "set a fuel price", kind, {"valid_from": valid_from, "price": price})


def fuel_prices(kind: str | None = None) -> list[dict]:
    ensure_shipped()
    with store.connect() as con:
        rows = con.execute("SELECT * FROM fuel_prices" + (" WHERE kind = ?" if kind else "") + " ORDER BY kind, valid_from",
                           ((kind,) if kind else ())).fetchall()
    return [{**dict(r), "source": json.loads(r["source"])} for r in rows]


# ------------------------------------------------------------------------------------------------ calendar and cards

def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def peak_window(calendar: dict, month: int) -> tuple[int, int]:
    for s in calendar["seasons"]:
        if month in s["months"]:
            return _minutes(s["peak"][0]), _minutes(s["peak"][1])
    return (-1, -1)


def is_peak_local(calendar: dict, local: datetime) -> bool:
    start, end = peak_window(calendar, local.month)
    m = local.hour * 60 + local.minute
    return start <= m < end if start <= end else (m >= start or m < end)


def calendar_for(conn: dict, on) -> dict:
    v = resolve("tou", "ke" if conn["data"].get("disco_group") == "ke" else "xwdisco", on)
    return v["body"] if v else {"seasons": []}


def card_for(category: str, on) -> tuple[dict, dict]:
    """The rate card of a category in force on a date, and the schedule version it came from."""
    v = resolve("schedule", "national", on)
    if v is None or category not in v["body"]["cards"]:
        raise LookupError(f"No rate card for {category} on {str(on)[:10]}")
    return v["body"]["cards"][category], v


def _energy_rate(card: dict, peak: bool) -> float:
    if card.get("tou") and card.get("peak") is not None:
        return card["peak"] if peak else card["off_peak"]
    return card["flat"]


def tax_rules(on) -> dict:
    v = resolve("tax", "national", on)
    return v["body"] if v else {}


def adders_for(billing_month: str) -> list[dict]:
    """The active adders billed in a billing month ("YYYY-MM"): QTA, FCA and the debt service surcharge."""
    ensure_shipped()
    out = []
    for v in versions("adder"):
        if v["status"] != "active":
            continue
        months = v["body"]["billing_months"]
        if months == "all" or billing_month in months:
            out.append({"key": v["key"], "id": v["id"], **v["body"]})
    return out


def icp_rule(on) -> dict | None:
    v = resolve("icp", "national", on)
    if v is None:
        return None
    b = v["body"]
    month = str(on)[:7]
    return b if b["first_month"] <= month <= b["last_month"] else None


def prosumer(regime: str, on) -> dict | None:
    v = resolve("prosumer", "net_billing" if regime == "net_billing" else "net_metering_legacy", on)
    return v["body"] if v else None


def icp_baseline(conn: dict, month: int, year: int, period: str) -> float | None:
    """The connection's ICP baseline for a calendar month and period, escalated 2.8 percent a package year; None when not eligible."""
    icp = conn["data"].get("icp") or {}
    if not icp.get("eligible"):
        return None
    table = icp.get("peak" if period == "peak" else "off_peak") or {}
    base = table.get(str(month))
    if base is None:
        return None
    # package year n: Dec 2025-Nov 2026 is year 1
    n = 1 + max(0, (year * 12 + month - (2025 * 12 + 12)) // 12)
    return float(base) * (1.028 ** n)


# ------------------------------------------------------------------------------------------------ the bill engine (R9 section 13)

def _tax_factor_energy(conn: dict, on) -> dict:
    """The multipliers the bill applies to variable charges, split into what the company bears and what it recovers."""
    data = conn["data"]
    tax = tax_rules(on)
    cls = tariff_data.category_class(data["tariff_category"])
    prov = (data.get("tax") or {}).get("province", "punjab")
    ed = tax.get("electricity_duty", {}).get(prov, {}).get(cls, 0.015)
    gst = tax.get("gst", 0.18)
    flags = data.get("tax") or {}
    further = tax.get("further_tax_no_strn", 0.03) if not flags.get("strn", True) and cls in ("industrial", "commercial") else 0.0
    s235 = tax.get("s235", {}).get("industrial" if cls == "industrial" else "commercial", {}).get("rate_above", 0.05)
    return {"ed": ed, "gst": gst, "further": further, "s235_marginal": s235, "gst_recoverable": bool(flags.get("gst_recoverable", True)),
            "s235_adjustable": bool(flags.get("s235_adjustable", True)), "class": cls}


def rebuild(ts_utc: np.ndarray, import_kw: np.ndarray, export_kw: np.ndarray, conn: dict, period_start: date, period_end: date,
            energy_scalar: float = 1.0, mdi_scalar: float = 1.0) -> dict:
    """The connection's bill over [period_start, period_end] (meter-reading dates, end inclusive) from 1-minute series.

    `ts_utc` holds epoch seconds of each minute's start (UTC); `import_kw` and `export_kw` the mean power of that minute. Returns the line items, the totals
    (as billed, and as the company bears them) and the aggregates verification reads."""
    data = conn["data"]
    on = period_end
    card, schedule = card_for(data["tariff_category"], on)
    cal = calendar_for(conn, on)
    tf = _tax_factor_energy(conn, on)
    billing_month = period_end.strftime("%Y-%m")
    t0 = datetime(period_start.year, period_start.month, period_start.day, tzinfo=PKT).timestamp()
    t1 = (datetime(period_end.year, period_end.month, period_end.day, tzinfo=PKT) + timedelta(days=1)).timestamp()
    mask = (ts_utc >= t0) & (ts_utc < t1)
    ts, imp, exp = ts_utc[mask], np.nan_to_num(import_kw[mask]) * energy_scalar, np.nan_to_num(export_kw[mask]) * energy_scalar
    # 1. period tagging, by the calendar month of each minute in Pakistan time
    peak = np.zeros(len(ts), dtype=bool)
    if card.get("tou") and card.get("peak") is not None:
        for i, t in enumerate(ts):
            peak[i] = is_peak_local(cal, datetime.fromtimestamp(float(t), PKT))
    # 2. energy by period
    e_imp = {"peak": float(imp[peak].sum() / 60.0), "off_peak": float(imp[~peak].sum() / 60.0)}
    e_exp = {"peak": float(exp[peak].sum() / 60.0), "off_peak": float(exp[~peak].sum() / 60.0)}
    # 3. maximum demand: 30-minute fixed blocks aligned to the hour
    interval = int(data.get("mdi_interval_min", 30))
    mdi = 0.0
    if len(ts):
        blocks = ((ts - ts[0] // 3600 * 3600) // (interval * 60)).astype(np.int64)
        sums = np.bincount(blocks - blocks.min(), weights=imp)
        counts = np.bincount(blocks - blocks.min())
        means = sums[counts > 0] / interval
        mdi = float(means.max()) * mdi_scalar if len(means) else 0.0
    sl = float(data["sanctioned_load_kw"])
    # legacy net metering nets by period first (R9 section 8.3; marked unverified)
    regime = data.get("regime", "none")
    surplus_kwh = 0.0
    if regime == "net_metering_legacy":
        for p in ("peak", "off_peak"):
            net = e_imp[p] - e_exp[p]
            surplus_kwh += max(0.0, -net)
            e_imp[p] = max(0.0, net)
    # 4. the ICP split per period by monthly totals
    icp = icp_rule(on)
    incremental = {"peak": 0.0, "off_peak": 0.0}
    if icp and data["tariff_category"] in icp["categories"]:
        for p in ("peak", "off_peak"):
            base = icp_baseline(conn, period_end.month, period_end.year, p)
            if base is not None:
                incremental[p] = max(0.0, e_imp[p] - base)
    inc_total = incremental["peak"] + incremental["off_peak"]
    # 5. energy charges
    lines = []
    energy = 0.0
    for p in ("peak", "off_peak") if card.get("tou") and card.get("peak") is not None else ("off_peak",):
        units = e_imp[p] if card.get("tou") and card.get("peak") is not None else e_imp["peak"] + e_imp["off_peak"]
        inc = incremental[p] if card.get("tou") and card.get("peak") is not None else inc_total
        base_units = units - inc
        rate = _energy_rate(card, p == "peak")
        energy += base_units * rate + inc * (icp["rate"] if icp else 0.0)
        lines.append({"line": f"Energy {p.replace('_', '-')}" if card.get("tou") and card.get("peak") is not None else "Energy", "units_kwh": round(base_units, 3),
                      "rate": rate, "amount_rs": round(base_units * rate, 2)})
        if inc > 0:
            lines.append({"line": f"ICP incremental {p.replace('_', '-')}", "units_kwh": round(inc, 3), "rate": icp["rate"], "amount_rs": round(inc * icp["rate"], 2)})
    # 6. fixed charges by basis, with Tariff F's seasonal multiplier
    days = (period_end - period_start).days + 1
    prorate = days / 30.0 if abs(days - 30) > 4 else 1.0
    if card["fixed_basis"] == "per_kw_month":
        billing_demand = max(card["floor_share"] * sl, mdi)
        fixed = card["fixed"] * billing_demand * prorate
    elif card["fixed_basis"] == "sanctioned_kw_month":
        billing_demand = sl
        fixed = card["fixed"] * sl * prorate
    else:
        billing_demand = 0.0
        fixed = card["fixed"] * prorate
    if period_end.month in (data.get("season_months") or []):
        fixed *= float(data.get("seasonal_multiplier", 1.0))
    lines.append({"line": "Fixed charges", "billing_demand_kw": round(billing_demand, 2), "mdi_kw": round(mdi, 2), "rate": card["fixed"],
                  "amount_rs": round(fixed, 2)})
    # 7. adders by billing month, with their exemptions
    total_units = e_imp["peak"] + e_imp["off_peak"]
    adders = 0.0
    for a in adders_for(billing_month):
        units = total_units
        if "icp_units" in a.get("exempt", []) or (a["key"] == "fca" and a["rate"] < 0 and a.get("negative_exempt_icp")):
            units = total_units - inc_total
        amt = a["rate"] * units
        adders += amt
        lines.append({"line": a["name"], "units_kwh": round(units, 3), "rate": a["rate"], "amount_rs": round(amt, 2)})
    # 8. electricity duty on the variable subtotal
    duty = tf["ed"] * (energy + adders)
    lines.append({"line": "Electricity duty", "rate": tf["ed"], "amount_rs": round(duty, 2)})
    # 9. export credit
    credit = 0.0
    exported = e_exp["peak"] + e_exp["off_peak"]
    if regime == "net_billing":
        pro = prosumer("net_billing", on) or {"naepp": 0.0}
        credit = exported * pro["naepp"]
    elif regime == "net_metering_legacy":
        credit = surplus_kwh * _energy_rate(card, False)
    # 10. GST (the credit is netted after taxes: DISCO practice, R9 section 13 step 10, to confirm on a prosumer bill)
    gst = tf["gst"] * (energy + fixed + adders + duty)
    lines.append({"line": "GST", "rate": tf["gst"], "amount_rs": round(gst, 2)})
    further = tf["further"] * (energy + fixed + adders + duty)
    if further:
        lines.append({"line": "Further tax", "rate": tf["further"], "amount_rs": round(further, 2)})
    # 11. income tax s.235 on the bill amount
    tax = tax_rules(on)
    slab = tax.get("s235", {}).get("industrial" if tf["class"] == "industrial" else "commercial")
    bill_amount = energy + fixed + adders + duty + gst + further
    s235 = 0.0
    if slab and tf["class"] in ("industrial", "commercial"):
        if bill_amount > slab["mid_upto"]:
            s235 = slab["base_above"] + slab["rate_above"] * (bill_amount - slab["mid_upto"])
        elif bill_amount > slab["nil_upto"]:
            s235 = slab["mid_rate"] * bill_amount
    lines.append({"line": "Income tax (s.235)", "amount_rs": round(s235, 2)})
    # 12. flat items
    flat = float(tax.get("ptv", {}).get(tf["class"], 0))
    if data.get("disco_group") == "ke" and tf["class"] in ("industrial", "commercial"):
        flat += float(tax.get("muct_ke", 0))
    if flat:
        lines.append({"line": "Flat fees (PTV, MUCT)", "amount_rs": round(flat, 2)})
    if credit:
        lines.append({"line": "Export credit", "units_kwh": round(exported if regime == "net_billing" else surplus_kwh, 3), "amount_rs": round(-credit, 2)})
    total = max(0.0, bill_amount + s235 + flat - credit)
    recovered = (gst if tf["gst_recoverable"] else 0.0) + (s235 if tf["s235_adjustable"] else 0.0)
    return {"lines": lines, "total_rs": round(total, 2), "bearable_rs": round(total - recovered, 2), "recovered_taxes_rs": round(recovered, 2),
            "energy_rs": round(energy + adders, 2), "demand_rs": round(fixed, 2), "credits_rs": round(credit, 2),
            "import_kwh": e_imp, "export_kwh": e_exp, "icp_incremental_kwh": incremental, "mdi_kw": round(mdi, 2),
            "schedule_version": schedule["id"], "schedule_hash": schedule["hash"], "billing_month": billing_month, "days": days}


# ------------------------------------------------------------------------------------------------ the optimiser's vectors (plan Part 6.4)

def energy_factor(conn: dict, on) -> float:
    """The multiplier from the base rate to the bearable marginal rupee per kWh of variable charges."""
    tf = _tax_factor_energy(conn, on)
    f = (1.0 + tf["ed"]) * (1.0 + tf["further"])
    if not tf["gst_recoverable"]:
        f *= 1.0 + tf["gst"]
    if not tf["s235_adjustable"]:
        f *= 1.0 + tf["s235_marginal"]
    return f


def fixed_factor(conn: dict, on) -> float:
    tf = _tax_factor_energy(conn, on)
    f = 1.0 + tf["further"]
    if not tf["gst_recoverable"]:
        f *= 1.0 + tf["gst"]
    if not tf["s235_adjustable"]:
        f *= 1.0 + tf["s235_marginal"]
    return f


def vectors(conn: dict | None, t0_utc: datetime, steps: int = 192, mtd_import: dict | None = None, rest_net_import: dict | None = None,
            g_max_kw: float = 1e6) -> dict:
    """The 15-minute price vectors for the optimiser from `t0_utc`: import and export prices (Rs/kWh, bearable basis), the peak flag, the MDI terms and
    the ICP case per period. A site with no connection (off-grid) gets zero prices and grid availability 0."""
    h = 0.25
    out = {"c_imp": np.zeros(steps), "c_exp": np.zeros(steps), "peak": np.zeros(steps, dtype=bool), "month": np.zeros(steps, dtype=int),
           "local_hour": np.zeros(steps), "grid": np.ones(steps), "icp": {}, "mdi": None}
    times = [t0_utc + timedelta(minutes=15 * i) for i in range(steps)]
    for i, t in enumerate(times):
        loc = t.astimezone(PKT)
        out["month"][i] = loc.month
        out["local_hour"][i] = loc.hour + loc.minute / 60.0
    if conn is None or conn["data"].get("regime") == "off_grid":
        out["grid"][:] = 0.0
        return out
    data = conn["data"]
    on = t0_utc.astimezone(PKT).date()
    card, _ = card_for(data["tariff_category"], on)
    cal = calendar_for(conn, on)
    ef = energy_factor(conn, on)
    billing_month = on.strftime("%Y-%m")
    add_all = sum(a["rate"] for a in adders_for(billing_month))
    add_icp = sum(a["rate"] for a in adders_for(billing_month) if a["key"] == "fca" and a["rate"] > 0)
    icp = icp_rule(on)
    regime = data.get("regime", "none")
    # ICP case per period (plan Part 4.3): above, below, or the uncertain middle that gets a month-level binary
    for p in ("peak", "off_peak"):
        base = icp_baseline(conn, on.month, on.year, p) if icp and data["tariff_category"] in icp["categories"] else None
        if base is None:
            continue
        q = (mtd_import or {}).get(p, 0.0) + (rest_net_import or {}).get(p, 0.0)
        steps_in_p = sum(1 for t in times if is_peak_local(cal, t.astimezone(PKT)) == (p == "peak"))
        swing = g_max_kw * steps_in_p * h
        case = "above" if q - base > swing else "below" if base - q > swing else "uncertain"
        out["icp"][p] = {"case": case, "baseline_kwh": base, "q_rest_kwh": q - (mtd_import or {}).get(p, 0.0) + (mtd_import or {}).get(p, 0.0),
                         "mtd_kwh": (mtd_import or {}).get(p, 0.0), "q_kwh": q, "rate": (icp["rate"] + add_icp) * ef}
    for i, t in enumerate(times):
        loc = t.astimezone(PKT)
        pk = is_peak_local(cal, loc) if card.get("tou") and card.get("peak") is not None else False
        out["peak"][i] = pk
        p = "peak" if pk else "off_peak"
        rate = _energy_rate(card, pk)
        icp_case = out["icp"].get(p, {}).get("case")
        if icp_case == "above":
            out["c_imp"][i] = (icp["rate"] + add_icp) * ef
        else:
            out["c_imp"][i] = (rate + add_all) * ef
        if regime == "net_billing":
            out["c_exp"][i] = (prosumer("net_billing", on) or {"naepp": 0.0})["naepp"]
        elif regime == "net_metering_legacy":
            out["c_exp"][i] = _energy_rate(card, False) * ef
        else:
            out["c_exp"][i] = 0.0
    if card["fixed_basis"] == "per_kw_month":
        out["mdi"] = {"rate_rs_per_kw": card["fixed"] * fixed_factor(conn, on), "floor_kw": card["floor_share"] * float(data["sanctioned_load_kw"]),
                      "interval_steps": max(1, int(data.get("mdi_interval_min", 30)) // 15)}
    return out


def provenance() -> list[dict]:
    """Every shipped and added number's source and status, for the tariff page's provenance view."""
    return [{"id": v["id"], "kind": v["kind"], "key": v["key"], "effective_from": v["effective_from"], "effective_to": v["effective_to"], "status": v["status"],
             "name": v["body"].get("name", v["key"]), "source": v["source"], "hash": v["hash"]} for v in versions()]
