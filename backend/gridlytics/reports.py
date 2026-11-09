"""Reports (plan Part 8.5): the monthly savings report per site, the energy-only (viewer-safe) report and the daily operations sheet, as PDF
(ReportLab, decision D13) and CSV, rendered from the same rows the API serves. Never the words guaranteed or optimal; Preliminary until 28 verified days.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import plans, platform as platformdb, sites, store, telemetry, twin, verify

PKT = timezone(timedelta(hours=5))
KINDS = ("monthly_savings", "energy_only", "daily_operations", "company_rollup", "pilot")
#: The kinds that cover the whole company, not one site: POST /api/reports takes no site for them and the row keeps an empty site_id
COMPANY_KINDS = ("company_rollup",)
TITLES = {"monthly_savings": "Monthly savings report", "energy_only": "Monthly energy report", "daily_operations": "Daily operations sheet",
          "company_rollup": "Company roll-up", "pilot": "Pilot report"}
LEVER_NAMES = {"peak_window": "Peak-window import avoided", "offpeak_energy": "Off-peak energy (charging)", "export": "Export credit",
               "genset": "Genset fuel and running cost", "wear": "Battery wear", "carry_over": "Carry-over (change in stored energy)",
               "mdi": "Maximum demand (MDI)"}
#: A connection's regime in words (the Site settings' words; audit role-viewer-17: the header printed the raw value "none")
REGIME_WORDS = {"net_billing": "net billing (2026 rules)", "net_metering_legacy": "legacy net metering", "none": "no export (no net metering)",
                "flat": "flat rate", "off_grid": "off-grid"}
#: A day's fidelity class in words, as the Savings page's badges say it (savings-days-table.tsx FIDELITY)
FIDELITY_WORDS = {"confirmed": "Confirmed", "partial": "Partly followed", "pending": "Awaiting confirmation", "not_applied": "Not applied",
                  "native_block": "Native block", "recommendation_only": "Recommendation only"}
#: Why a priced day claims nothing (verify.compute_day's `claim_withheld`), in the page's words (savings-days-table.tsx WITHHELD)
WITHHELD_WORDS = {"import_unknown": "grid import unknown", "battery_flow_unknown": "battery data missing", "genset_unknown": "genset data missing"}


def _text(value) -> str:
    """A value as plain text inside a ReportLab Paragraph: &, < and > escaped, so it is never read as markup."""
    return escape(str(value))


def _round(x: float, digits: int = 0) -> float:
    """x rounded as the web app's Math.round does: half toward +infinity (1900.5 to 1901, -1900.5 to -1900), so a report reads what the page
    reads; Python's format rounds half to even, which printed a saving of Rs 1,900.5 as "Rs 1,900" beside a claim of "Rs 1,901"."""
    scale = 10 ** digits
    v = x * scale
    r = math.floor(v)
    if v - r >= 0.5:
        r += 1
    return r / scale


def _finite(x) -> float | None:
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def rs(x) -> str:
    """Whole rupees as the page writes them (lib/format.ts rs): "Rs 1,234", "-Rs 1,234", never "Rs -0"; a dash when missing."""
    v = _finite(x)
    if v is None:
        return "-"
    r = int(_round(v)) or 0
    return ("-" if r < 0 else "") + "Rs " + f"{abs(r):,}"


def smart(x, unit: str = "") -> str:
    """One decimal under 100, whole figures from 100, as the page's smart() (lib/format.ts): rounded half away from zero, and a figure that rounds to
    zero reads "0.0", never "-0"; a dash when missing."""
    v = _finite(x)
    if v is None:
        return "-"
    one = _round(abs(v), 1)
    text = f"{one:,.1f}" if one < 100 else f"{_round(abs(v)):,.0f}"
    return ("-" if v < 0 and text.strip("0.,") else "") + text + unit


def _day_fidelity(s: dict) -> str:
    """A day's fidelity in words, and its claim marked when it is withheld: the saving beside it is not in the claimed total."""
    words = FIDELITY_WORDS.get(s.get("fidelity") or "", "-") if s.get("fidelity") else "-"
    why = s.get("claim_withheld") or ("import_unknown" if s.get("import_unknown") else None)
    if why:
        words += f"; claim withheld ({WITHHELD_WORDS.get(why, str(why).replace('_', ' '))})"
    return words


def _litres_saved(b: dict, scored: int, has_genset: bool) -> str:
    """The diesel a baseline's ledger saved, as the page's ledger says it (savings-ledger.tsx): "not metered" when the genset's fuel was unknown on
    every scored day (verify.summary sums such days as nothing, so its 0.0 is no figure), else the litres; "no genset" at a site without one."""
    if not has_genset:
        return "no genset"
    unmetered = int(b.get("genset_unknown_days") or 0)
    if b.get("litres_saved") is None or (scored > 0 and unmetered >= scored):
        return "not metered"
    return smart(b.get("litres_saved"), " L")


def _weeks(x) -> str:
    v = _finite(x) or 0.0
    return f"{v:g}" if v == int(v) else f"{v:.1f}"


def preliminary_sentence(summ: dict, month_over: bool) -> str:
    """Why the figures are preliminary: only the conditions that still apply, as the Savings page lists them (verify.summary's final_conditions,
    plan 8.5): 28 verified days, a native capture, 8 weeks of switchback, the FCA of every month billed; and the month being over."""
    c = summ.get("final_conditions") or {}
    left = []
    if c:
        if (c.get("verified_days") or 0) < (c.get("verified_days_needed") or 0):
            left.append(f"{c['verified_days_needed']} verified days ({c.get('verified_days') or 0} so far)")
        if not c.get("native_capture", True):
            left.append("a capture of the inverter's native settings")
        if (_finite(c.get("switchback_weeks")) or 0.0) < (c.get("switchback_weeks_needed") or 0):
            left.append(f"{c['switchback_weeks_needed']} weeks of switchback ({_weeks(c.get('switchback_weeks'))} so far)")
        if not c.get("fca_billed", True):
            left.append("the fuel charges adjustment of every month in the period is billed")
    if not month_over:
        left.append("the month is over")
    if not left:  # an answer without the conditions: the plan's rule in full
        left = ["28 verified days, a native capture, 8 weeks of switchback, the fuel charges adjustment of every month billed, and the month over"]
    return "Preliminary. These figures stay preliminary until " + "; ".join(left) + "."


def _short_day(day: str) -> str:
    """"Fri 6 Nov" for "2026-11-06" (the Savings page's shortDay)."""
    try:
        d = date.fromisoformat(str(day)[:10])
    except ValueError:
        return str(day)
    return f"{d:%a} {d.day} {d:%b}"


def stale_sentence(stale: list[str]) -> str:
    """The Savings page's "Recompute needed" notice for verify.summary's chain_stale_days: the days scored before an earlier day was re-scored, whose
    figures do not add up with the days before them until the period is recomputed; empty when every day is in step."""
    if not stale:
        return ""
    n = len(stale)
    shown = ", ".join(_short_day(d) for d in stale[:6]) + (f" and {n - 6} more" if n > 6 else "")
    return (f"Recompute needed: {n} day{'s' if n != 1 else ''} ({shown}) {'were' if n != 1 else 'was'} scored before an earlier day was re-scored, so "
            f"the totals in this report may not add up until {'they are' if n != 1 else 'it is'} recomputed.")


def _genset_assumption(site: dict) -> str:
    """At a site whose genset is started by hand, the native replay's genset is an operator rule the product assumes (twin.simulate), not part of the
    capture (plan 8.2: the genset stream is reported with its fidelity, not claimed as blinded)."""
    if not (site.get("assets") or {}).get("genset") or (site.get("data") or {}).get("genset_start_control", "manual") != "manual":
        return ""
    return (" At this site the genset is started by hand, so the replay of the native settings also assumes an operator rule: someone starts the genset "
            "about ten minutes after the battery reaches its floor in an outage and stops it once the battery is charged. That rule is the product's "
            "assumption, not part of the capture, so the genset's part of the saving is reported with its fidelity, not as a blinded measurement.")


def num(x, fmt: str = ",.0f", unit: str = "", scale: float = 1.0) -> str:
    """A quantity as text, or a dash when it is missing (a day row may hold None: a value that was not finite, or never measured)."""
    if x is None or isinstance(x, bool):
        return "-"
    try:
        v = float(x) / scale
    except (TypeError, ValueError):
        return "-"
    if v != v or v in (float("inf"), float("-inf")):
        return "-"
    return f"{v:{fmt}}{unit}"


def file_name(r: dict, which: str) -> str:
    """The name a download is saved under: the site, the kind and the month ("gridlytics-faisalabad-textile-mill-monthly-savings-2026-11.pdf"), so
    two sites' reports of a month never share a name; a site name with no Latin letters or digits gives the site's id instead."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(r.get("site_name") or "").lower()).strip("-")[:60].strip("-") or str(r.get("site_id") or "")
    kind = str(r.get("kind") or "report").replace("_", "-")
    return "-".join(x for x in ("gridlytics", slug, kind, str(r.get("period") or "")) if x) + f".{which}"


def _dir() -> Path:
    d = platformdb.data_dir() / "reports" / store.db_path().stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def _period(period: str) -> tuple[date, date]:
    y, m = int(period[:4]), int(period[5:7])
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def _table(rows, widths=None, header=True, right=True):
    """A table; `right` aligns every column but the first to the right (figures), else all to the left (words)."""
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    style = [("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (-1, -1), "RIGHT" if right else "LEFT")]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(style))
    return t


class _Doc:
    """A report being built: the PDF's story and the CSV's rows. Every text goes through _text (escaped), so nothing a person typed is read as markup."""

    def __init__(self) -> None:
        self.styles = getSampleStyleSheet()
        self.cell_style = ParagraphStyle("cell", parent=self.styles["Normal"], fontSize=8, leading=10)
        self.story: list = []
        self.buf = io.StringIO()
        self.csv = csv.writer(self.buf)

    def title(self, text: str) -> None:
        self.story.append(Paragraph(_text(text), self.styles["Title"]))

    def h2(self, text: str) -> None:
        self.story.append(Paragraph(_text(text), self.styles["Heading2"]))

    def p(self, text: str, lead: str = "") -> None:
        """A paragraph; `lead`, when the text starts with it, in bold ("Preliminary.")."""
        body = _text(text)
        if lead and text.startswith(lead):
            body = f"<b>{_text(lead)}</b>{_text(text[len(lead):])}"
        self.story.append(Paragraph(body, self.styles["Normal"]))

    def cell(self, text) -> Paragraph:
        """A table cell that wraps."""
        return Paragraph(_text(text), self.cell_style)

    def head(self, *labels: str) -> list:
        """A header row whose labels wrap (bold), for a table of narrow columns."""
        return [Paragraph(f"<b>{_text(x)}</b>", self.cell_style) for x in labels]

    def table(self, rows: list, widths=None, right: bool = True) -> None:
        self.story.append(_table(rows, widths, right=right))

    def gap(self, n: float = 3) -> None:
        self.story.append(Spacer(1, n * mm))


def _month_words(month: str) -> str:
    """"October 2026" for "2026-10"."""
    try:
        return f"{date(int(month[:4]), int(month[5:7]), 1):%B %Y}"
    except (ValueError, IndexError):
        return str(month)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _pct(x, digits: int = 1) -> str:
    v = _finite(x)
    return "-" if v is None else f"{_round(v, digits):.{digits}f} %"


def coverage_sentence(tdays: list[dict], p0: date, p1: date, today: date) -> str:
    """Which days of the period the data covers, when it is not all of them (audit flow-reports-alerts-members-keys-05: an October report held data
    from the 25th only and did not say so)."""
    days = sorted(str(d.get("day")) for d in tdays if d.get("day"))
    total = (p1 - p0).days + 1
    if not days:
        return f"No data was received for {p0:%B %Y}: this report has no measured figures."
    n = len(days)
    span = f"{_short_day(days[0])} to {_short_day(days[-1])}" if n > 1 else _short_day(days[0])
    if p1 >= today:
        return f"The month is not over: the data covers {n} of the month's {total} days so far ({span})."
    if n < total:
        return f"The data covers {n} of the month's {total} days ({span}): the figures are for those days only."
    return ""


def _connection_words(site: dict) -> str:
    conn = (site.get("connections") or [None])[0]
    if not conn:
        return "Off-grid."
    data = conn["data"]
    regime = REGIME_WORDS.get(data.get("regime"), str(data.get("regime")).replace("_", " "))
    return f"{site['data'].get('disco') or ''} {data.get('tariff_category') or ''}, {regime}.".strip()


def _on_grid(site: dict) -> bool:
    conn = (site.get("connections") or [None])[0]
    return bool(conn) and conn["data"].get("regime") != "off_grid"


# ------------------------------------------------------------------------------------------------ the key figures (plan 8.3, the report's KPI grid)

def diesel_line(site: dict, tdays: list[dict], nat: dict, scored: int, d0: date, d1: date) -> str:
    """The genset's running hours, the litres it used (metered, or Estimated from the fuel curve, as verify.litres_used reads them for the page) and
    the litres saved against the native settings."""
    if not (site.get("assets") or {}).get("genset"):
        return "No genset at this site."
    hours = sum(_finite(d.get("genset_hours")) or 0.0 for d in tdays)
    parts = [f"{smart(hours, ' h')} of running"]
    if 0 <= (d1 - d0).days <= 62:
        try:
            lu = verify.litres_used(site["id"], d0.isoformat(), d1.isoformat())
        except Exception:  # noqa: BLE001  (a label beside the figures; never fail the report for it)
            lu = {}
        if lu.get("litres_source"):
            parts.append(f"{smart(lu.get('litres'), ' L')} used ({'metered' if lu['litres_source'] == 'meter' else 'Estimated from the fuel curve'})")
    saved = _litres_saved(nat, scored, True)
    parts.append("litres saved not metered" if saved == "not metered" else f"{saved} saved against the native settings (Estimated)")
    return "; ".join(parts) + "."


def peak_line(nat: dict, money: bool) -> str:
    """Each bill month's maximum demand (the highest 30-minute block), actual against the native replay, and the MDI lever (plan 8.3 Peak)."""
    months = nat.get("mdi_months") or {}
    if not months:
        return "No maximum demand was recorded in the month."
    parts = []
    for k, v in sorted(months.items()):
        floor = _finite(v.get("floor_kw")) or 0.0
        parts.append(f"{_month_words(k)}: {smart(v.get('actual_kw'), ' kW')} actual against {smart(v.get('baseline_kw'), ' kW')} with the native "
                     "settings" + (f" (billing floor {smart(floor, ' kW')})" if floor > 0 else ""))
    if money and (nat.get("levers") or {}).get("mdi") is not None:
        parts.append(f"MDI lever {rs(nat['levers']['mdi'])}")
    return "; ".join(parts) + "."


def outage_cover(site: dict, d0: date, d1: date) -> dict | None:
    """The period's outages (telemetry.outages, as the Outages page reads them): how many, how long, how many the site carried without unserved
    critical load, with the genset, and entered at or above the reserve the plan in force asked for (the Reserve alert's test). None off-grid."""
    if not _on_grid(site):
        return None
    t0 = twin.day_start_utc(d0)
    t1 = min(twin.day_start_utc(d1) + 86400, int(datetime.now(timezone.utc).timestamp()))
    outs = telemetry.outages(site["id"], t0, t1) if t1 > t0 else []
    has_bat = bool((site.get("assets") or {}).get("battery"))
    with_reserve = entered_ok = 0
    if has_bat:
        for o in outs:
            plan = plans.active_at(site["id"], o["start"])
            st = next((s for s in (plan or {}).get("steps", []) if s["t"] <= o["start"] < s["t"] + 900), None)
            if st and st.get("reserve_pct") and o.get("soc_start_pct") is not None:
                with_reserve += 1
                entered_ok += int(o["soc_start_pct"] >= st["reserve_pct"])
    return {"outages": len(outs), "hours": sum(o.get("minutes") or 0 for o in outs) / 60,
            "carried": sum(1 for o in outs if (o.get("unserved_kwh") or 0.0) <= 0.01), "with_genset": sum(1 for o in outs if o.get("genset_minutes")),
            "with_reserve": with_reserve, "entered_ok": entered_ok, "unserved_kwh": sum(o.get("unserved_kwh") or 0.0 for o in outs)}


def outage_line(c: dict | None) -> str:
    if c is None:
        return "No grid connection: there are no grid outages to carry."
    if not c["outages"]:
        return "No outages in the period."
    parts = [f"{_plural(c['outages'], 'outage')}, {smart(c['hours'], ' h')} in all", f"{c['carried']} carried without unserved critical load"]
    if c["with_genset"]:
        parts.append(f"{c['with_genset']} with the genset running")
    if c["with_reserve"]:
        parts.append(f"{c['entered_ok']} of {c['with_reserve']} entered at or above the planned reserve")
    if c["unserved_kwh"] > 0.01:
        parts.append(f"{smart(c['unserved_kwh'], ' kWh')} of critical load unserved")
    return "; ".join(parts) + "."


def forecast_skill(site_id: str, d0: date, d1: date) -> dict:
    """The day-ahead forecast scores stored for the period (forecast.score_day): solar nRMSE by capacity, load nRMSE by mean load with its skill against
    same-day-last-week persistence, the outage Brier score against the schedule alone."""
    with store.connect() as con:
        rows = con.execute("SELECT kind, metric, value, baseline_value FROM forecast_scores WHERE site_id = ? AND day_local >= ? AND day_local <= ? "
                           "AND horizon_class = 'day_ahead'", (site_id, d0.isoformat(), d1.isoformat())).fetchall()
    mean = lambda xs: sum(xs) / len(xs) if xs else None  # noqa: E731
    pv = [r["value"] for r in rows if r["kind"] == "pv" and r["metric"] == "nrmse"]
    load = [r for r in rows if r["kind"] == "load" and r["metric"] == "nrmse"]
    paired = [(r["value"], r["baseline_value"]) for r in load if r["baseline_value"]]
    out = [r for r in rows if r["kind"] == "outage" and r["metric"] == "brier"]
    skill = None
    if paired and mean([b for _, b in paired]):
        skill = 1 - mean([v for v, _ in paired]) / mean([b for _, b in paired])
    return {"pv_nrmse": mean(pv), "pv_days": len(pv), "load_nrmse": mean([r["value"] for r in load]), "load_days": len(load), "load_skill": skill,
            "outage_brier": mean([r["value"] for r in out]), "outage_baseline": mean([r["baseline_value"] for r in out if r["baseline_value"] is not None])}


def forecast_line(f: dict) -> str:
    parts = []
    if f["pv_nrmse"] is not None:
        parts.append(f"solar day-ahead error {_pct(f['pv_nrmse'] * 100)} of capacity over {_plural(f['pv_days'], 'day')}")
    if f["load_nrmse"] is not None:
        load = f"load error {_pct(f['load_nrmse'] * 100)} of the mean load"
        if f["load_skill"] is not None:
            load += f", {_pct(abs(f['load_skill']) * 100, 0)} {'better' if f['load_skill'] >= 0 else 'worse'} than the same day a week before"
        parts.append(load)
    if f["outage_brier"] is not None:
        parts.append(f"outage forecast Brier score {f['outage_brier']:.3f}"
                     + (f" against {f['outage_baseline']:.3f} for the schedule alone" if f["outage_baseline"] is not None else ""))
    if not parts:
        return "No forecast was scored in the period."
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]


#: The edge's events a control-health line counts, in words (alerts.EDGE_EVENTS)
EDGE_WORDS = {"readback_mismatch": ("read-back mismatch", "read-back mismatches"), "write_failed": ("failed write", "failed writes"),
              "fallback_engaged": ("fallback to the native settings", "fallbacks to the native settings"),
              "local_override": ("change made on site", "changes made on site"), "budget_exhausted": ("day the write budget ran out", "days the write budget ran out"),
              "plan_rejected": ("plan refused by the edge", "plans refused by the edge")}
#: The plan days' fidelity classes in a sentence, in the page's order
FIDELITY_ORDER = (("confirmed", "confirmed"), ("partial", "partly followed"), ("pending", "awaiting confirmation"), ("not_applied", "not applied"),
                  ("native_block", "on a native block"), ("recommendation_only", "recommendation only"))


def control_health(site_id: str, days: list[dict], d0: date, d1: date) -> dict:
    """Plan 8.3 Control health: the scored days by fidelity class, the mean execution score of the days marked applied, and the edge's events."""
    counts: dict[str, int] = {}
    for d in days:
        if d.get("fidelity"):
            counts[d["fidelity"]] = counts.get(d["fidelity"], 0) + 1
    t0, t1 = twin.day_start_utc(d0), twin.day_start_utc(d1) + 86400
    with store.connect() as con:
        scores = [r["fidelity"] for r in con.execute("SELECT fidelity FROM applied WHERE site_id = ? AND day_local >= ? AND day_local <= ? AND fidelity "
                                                     "IS NOT NULL", (site_id, d0.isoformat(), d1.isoformat()))]
        rows = con.execute(f"SELECT type, ts FROM events WHERE site_id = ? AND source = 'edge' AND type IN ({','.join('?' * len(EDGE_WORDS))})",
                           (site_id, *EDGE_WORDS)).fetchall()
    events: dict[str, int] = {}
    for r in rows:
        try:
            t = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if t0 <= t < t1:
            events[r["type"]] = events.get(r["type"], 0) + 1
    return {"days": counts, "score": (sum(scores) / len(scores)) if scores else None, "scored_applied": len(scores), "events": events}


def control_line(c: dict) -> str:
    parts = []
    days = [f"{c['days'][k]} {w}" for k, w in FIDELITY_ORDER if c["days"].get(k)]
    parts.append(("Plan days: " + ", ".join(days)) if days else "No plan day scored")
    if c["score"] is not None:
        parts.append(f"execution score {_pct(c['score'] * 100, 0)} on average over {_plural(c['scored_applied'], 'applied day')}")
    ev = [_plural(n, *EDGE_WORDS[k]) for k, n in c["events"].items() if n]
    if ev:
        parts.append("edge: " + ", ".join(ev))
    return "; ".join(parts) + "."


# ------------------------------------------------------------------------------------------------ sections

ARM_WORDS = {"plan": "The plan", "native": "The native settings"}


def _ledger(doc: _Doc, nat: dict, tun: dict, scored: int) -> None:
    """The ledger by lever against both baselines (plan 8.1: every saving is reported against both), as the Savings page's ledger."""
    keys = [k for k in LEVER_NAMES if k in (nat.get("levers") or {}) or k in (tun.get("levers") or {})]
    keys += [k for k in list(nat.get("levers") or {}) + list(tun.get("levers") or {}) if k not in keys and k not in LEVER_NAMES]
    keys = list(dict.fromkeys(keys))

    def cell(b: dict, k: str) -> str:
        v = (b.get("levers") or {}).get(k)
        if v is None and k == "genset" and int(b.get("genset_unknown_days") or 0) > 0:
            return "not metered"
        return rs(v)

    rows = [["Lever", "Against the native settings", "Against a tuned table"]]
    rows += [[LEVER_NAMES.get(k, k.replace("_", " ").capitalize()), cell(nat, k), cell(tun, k)] for k in keys]
    rows += [["Saving, all days", rs(nat.get("saving_rs")), rs(tun.get("saving_rs"))],
             ["Claimed (verified days only)", rs(nat.get("claimed_rs")), rs(tun.get("claimed_rs"))]]
    if nat.get("perfect_foresight_rs") is not None:
        rows.append(["Perfect foresight (not achievable, for reference)", rs(nat["perfect_foresight_rs"]), "-"])
    doc.table(rows, [95 * mm, 42 * mm, 42 * mm])
    doc.p("The ledger sums every scored day of the period, the days that claim nothing included (a table not applied, a native block, a withheld "
          "claim); the claimed figures count the verified days only.")


def _switchback(doc: _Doc, site_id: str, d0: date, d1: date, whole: bool = False) -> None:
    """Plan 8.5: the switchback calendar with the carry-over credits, and its result (verify.switchback_result, as the Savings page's Switchback tab)."""
    sb = verify.switchback_result(site_id)
    blocks = sb.get("blocks") or []
    if not blocks:
        doc.p("No switchback calendar is painted for this site, so the figures rest on the counterfactual replay alone.")
        return
    shown = blocks if whole else [b for b in blocks if b["block_start"] <= d1.isoformat() and b["block_end"] >= d0.isoformat()]
    if shown:
        doc.table([["From", "To", "Arm"]] + [[_short_day(b["block_start"]), _short_day(b["block_end"]), ARM_WORDS.get(b["arm"], str(b["arm"]))]
                                             for b in shown], [35 * mm, 35 * mm, 50 * mm], right=False)
    else:
        doc.p("No switchback block falls in this period.")
    credits = [c for c in sb.get("carry_over_credits") or [] if whole or d0.isoformat() <= str(c.get("boundary")) <= d1.isoformat()]
    if credits:
        doc.gap(2)
        doc.table([["Switch to native on", "Plan arm ended with", "Native arm ended with", "Valued at", "Carry-over credit to the plan"]]
                  + [[_short_day(c["boundary"]), smart(c.get("e_end_plan_kwh"), " kWh"), smart(c.get("e_end_native_kwh"), " kWh"),
                      f"Rs {_round(_finite(c.get('v_end')) or 0.0, 2):.2f}/kWh", rs(c.get("credit_rs"))] for c in credits],
                  [35 * mm, 35 * mm, 35 * mm, 30 * mm, 45 * mm])
    res = sb.get("result")
    doc.gap(2)
    if res:
        doc.p(f"Over the calendar so far ({_plural(int(res.get('plan_days') or 0), 'plan day')}, {_plural(int(res.get('native_days') or 0), 'native day')}): "
              f"the plan saves {rs(res.get('saving_rs_per_day_adjusted'))} a day adjusted for sun, load and outages "
              f"({rs(res.get('saving_rs_per_day_unadjusted'))} a day unadjusted). Carry-over credit: {rs(res.get('carry_over_credit_rs'))} in all, "
              f"{rs(res.get('carry_over_rs_per_plan_day'))} a plan day, included in both.")
    else:
        doc.p(f"No switchback result yet: it needs at least six scored days on both arms ({_plural(int(sb.get('days') or 0), 'day')} scored so far).")


def _bills(doc: _Doc, site: dict, d0: date, d1: date) -> None:
    """Plan 8.5: the bill comparison, the DISCO bills entered for the period against the bill engine's rebuild (verify.bills)."""
    if not _on_grid(site):
        doc.p("No grid connection: there is no DISCO bill to compare.")
        return
    rows = [b for b in verify.bills(site["id"]) if str(b.get("period_start")) <= d1.isoformat() and str(b.get("period_end")) >= d0.isoformat()]
    if not rows:
        doc.p("No DISCO bill covering this period has been entered yet: the comparison is made when one is (Savings, Bill comparison).")
        return
    doc.table([["Billing period", "Printed bill", "Rebuilt by Gridlytics", "Difference"]]
              + [[f"{_short_day(b['period_start'])} to {_short_day(b['period_end'])}", rs((b.get("real") or {}).get("total_rs")),
                  rs((b.get("rebuilt") or {}).get("total_rs")), _pct(b.get("difference_pct"))] for b in rows], [60 * mm, 35 * mm, 40 * mm, 25 * mm])


def _method(doc: _Doc, site: dict, summ: dict) -> None:
    doc.p("Method: " + str(summ.get("method") or "counterfactual replay against the native capture and a tuned table") + ".", lead="Method:")
    doc.p("Assumptions: every rupee is on the connection's bearable-cost basis, with recoverable GST and adjustable section 235 tax left out where "
          "the connection's tax settings say so; fuel is priced at the dated pump price, with metered litres where a sensor exists and the fitted "
          "fuel curve where not; both arms carry the same battery wear charge." + _genset_assumption(site), lead="Assumptions:")


def _energy_and_days(doc: _Doc, site: dict, summ: dict, tdays: list[dict], money: bool) -> None:
    """The energy table and the daily rows (the PDF's annex) with the CSV's rows."""
    site_id = site["id"]
    doc.h2("Energy")
    tot = lambda k: sum(float(d.get(k) or 0) for d in tdays if num(d.get(k)) != "-")  # noqa: E731
    doc.table([["Quantity", "Month"], ["Grid import", f"{tot('import_kwh'):,.0f} kWh"], ["Grid export", f"{tot('export_kwh'):,.0f} kWh"],
               ["Solar", f"{tot('pv_kwh'):,.0f} kWh"], ["Load", f"{tot('load_kwh'):,.0f} kWh"], ["Genset", f"{tot('genset_kwh'):,.0f} kWh, "
                                                                                                    f"{tot('genset_hours'):,.1f} h"],
               ["Outage time", f"{tot('outage_minutes') / 60:,.1f} h"], ["Unserved critical load", f"{tot('unserved_kwh'):,.1f} kWh"],
               ["Peak 30-minute import", f"{max([float(d.get('peak_kw') or 0) for d in tdays if num(d.get('peak_kw')) != '-'] or [0]):,.1f} kW"]],
              [80 * mm, 50 * mm])
    doc.gap()
    doc.h2("Daily rows")
    head = ["Day", "Import kWh", "Solar kWh", "Outage h", "Genset h"] + (["Saving vs native", "Fidelity"] if money else [])
    by_day = {d["day"]: d for d in summ["days"]}
    rows = [head]
    doc.csv.writerow(["day", "import_kwh", "export_kwh", "pv_kwh", "load_kwh", "genset_hours", "litres", "outage_minutes", "peak_kw"] +
                     (["saving_native_rs", "saving_tuned_rs", "fidelity", "plan_ids", "claim_withheld"] if money else []))
    for d in tdays:
        s = by_day.get(d["day"], {})
        rows.append([_short_day(d["day"]), num(d.get("import_kwh")), num(d.get("pv_kwh")), num(d.get("outage_minutes"), ".1f", scale=60),
                     num(d.get("genset_hours"), ".1f")] +
                    ([rs(s.get("saving_native_rs")), doc.cell(_day_fidelity(s))] if money else []))
        ids = ""
        if money:
            with store.connect() as con:
                ids = ";".join(r[0] for r in con.execute("SELECT id FROM plans WHERE site_id = ? AND substr(belief_time,1,10) = ? AND status != 'failed'",
                                                          (site_id, d["day"])).fetchall()[:4])
        doc.csv.writerow([d["day"]] + [d.get(k) for k in ("import_kwh", "export_kwh", "pv_kwh", "load_kwh", "genset_hours", "litres", "outage_minutes",
                                                          "peak_kw")]
                         + ([s.get("saving_native_rs"), s.get("saving_tuned_rs"), s.get("fidelity"), ids,
                             s.get("claim_withheld") or ("import_unknown" if s.get("import_unknown") else None)] if money else []))
    doc.table(rows, [22 * mm, 22 * mm, 22 * mm, 18 * mm, 18 * mm] + ([28 * mm, 50 * mm] if money else []))
    if money and any(s.get("claim_withheld") or s.get("import_unknown") for s in summ["days"]):
        doc.p("A day marked “claim withheld” claims nothing: its saving is shown for reference and is not in the claimed total, nor is it a "
              "verified day.")
    cap = sites.native_latest(site_id)
    doc.gap()
    doc.p(f"Native capture version {cap['version'] if cap else 'none'}; tariff versions as dated in the CSV annex." + (
        " The savings are measured against a replay of the inverter's own settings on the same day's sun, load and outages (Method above)." if money
        else " The energy figures are as measured."))


def _prelim_and_stale(doc: _Doc, summ: dict, over: bool, money: bool) -> bool:
    prelim = bool(summ.get("preliminary") or not over)
    if prelim:
        doc.p(preliminary_sentence(summ, over), lead="Preliminary.")
        doc.gap()
    stale = stale_sentence(summ.get("chain_stale_days") or []) if money else ""
    if stale:  # the page's notice, so a report made while days are out of step does not print totals that do not add up without a word
        doc.p(stale, lead="Recompute needed:")
        doc.gap()
    return prelim


# ------------------------------------------------------------------------------------------------ the kinds

def _monthly(doc: _Doc, site: dict, kind: str, p0: date, p1: date, today: date, inputs: dict) -> bool:
    """The monthly savings report (plan 8.5: a one-page summary, then the annex of daily rows) or the energy-only report (no money)."""
    site_id = site["id"]
    money = kind == "monthly_savings"
    tdays = telemetry.days(site_id, p0.isoformat(), p1.isoformat())
    cover = coverage_sentence(tdays, p0, p1, today)
    if cover:
        doc.p(cover)
        doc.gap()
    summ = verify.summary(site_id, p0.isoformat(), p1.isoformat(), see_fee=False)
    inputs["computed"] = summ.get("computed")
    prelim = _prelim_and_stale(doc, summ, p1 < today, money)
    nat, tun = summ["baselines"].get("native", {}), summ["baselines"].get("tuned", {})
    scored = len(summ.get("days") or [])
    end = min(p1, today)
    if money:
        has_genset = bool((site.get("assets") or {}).get("genset"))
        doc.h2("Summary")
        doc.p("The headline against both baselines: the inverter's own settings as captured (native) and the best static table of its brand (tuned).")
        doc.table([["Headline", "Against the native settings", "Against a tuned table"],
                   ["Saving, all days", rs(nat.get("saving_rs")), rs(tun.get("saving_rs"))],
                   ["Claimed (verified days only)", rs(nat.get("claimed_rs")), rs(tun.get("claimed_rs"))],
                   ["Diesel saved (Estimated)", _litres_saved(nat, scored, has_genset), _litres_saved(tun, scored, has_genset)],
                   ["Verified days", str(nat.get("verified_days", 0)), str(tun.get("verified_days", 0))]], [95 * mm, 42 * mm, 42 * mm])
        unmetered = max(int(nat.get("genset_unknown_days") or 0), int(tun.get("genset_unknown_days") or 0))
        if has_genset and 0 < unmetered:
            doc.p(f"The genset's fuel was not metered on {unmetered} of {scored} days: those days add nothing to the litres or the genset lever.")
        doc.gap()
        doc.h2("Key figures")
        doc.table([["Figure", "This month"],
                   ["Diesel", doc.cell(diesel_line(site, tdays, nat, scored, p0, end))],
                   ["Peak (MDI)", doc.cell(peak_line(nat, True))],
                   ["Outage coverage", doc.cell(outage_line(outage_cover(site, p0, end)))],
                   ["Forecast skill", doc.cell(forecast_line(forecast_skill(site_id, p0, end)))],
                   ["Control health", doc.cell(control_line(control_health(site_id, summ.get("days") or [], p0, end)))]],
                  [35 * mm, 145 * mm], right=False)
        doc.gap()
        doc.h2("Ledger by lever")
        _ledger(doc, nat, tun, scored)
        doc.gap()
        doc.h2("Switchback")
        _switchback(doc, site_id, p0, p1)
        doc.gap()
        doc.h2("Bill comparison")
        _bills(doc, site, p0, p1)
        doc.gap()
        _method(doc, site, summ)
        doc.story.append(PageBreak())
        doc.h2("Annex: energy and daily rows")
    else:
        doc.h2("Outages")
        doc.p(outage_line(outage_cover(site, p0, end)))
        doc.gap()
    _energy_and_days(doc, site, summ, tdays, money)
    return prelim


def _company_rollup(doc: _Doc, p0: date, p1: date, today: date, inputs: dict) -> bool:
    """Plan 8.5's roll-up per company: each site's saving against both baselines, its claim, litres and verified days, and the company's totals, all
    from verify.summary as the fleet roll-up (GET /api/fleet/savings) reads them."""
    rows, out_rows, prelim_names, stale_names = [], [], [], []
    tot = {"nat": 0.0, "tun": 0.0, "claimed": 0.0, "litres": 0.0, "verified": 0}
    doc.csv.writerow(["site_id", "site", "saving_native_rs", "saving_tuned_rs", "claimed_rs", "litres_saved", "verified_days", "preliminary",
                      "chain_stale_days"])
    computed = []
    for s in sites.listing():
        summ = verify.summary(s["id"], p0.isoformat(), p1.isoformat(), see_fee=False)
        computed.append(summ.get("computed") or "")
        nat, tun = summ["baselines"].get("native", {}), summ["baselines"].get("tuned", {})
        scored = len(summ.get("days") or [])
        has_genset = bool(sites.assets(s["id"]).get("genset"))
        stale = list(summ.get("chain_stale_days") or [])
        if summ.get("preliminary"):
            prelim_names.append(s["name"])
        if stale:
            stale_names.append(s["name"])
        status = "No scored days" if not scored else ("Preliminary" if summ.get("preliminary") else "Final") + ("; recompute needed" if stale else "")
        rows.append([doc.cell(s["name"]), rs(nat.get("saving_rs")), rs(tun.get("saving_rs")), rs(nat.get("claimed_rs")),
                     _litres_saved(nat, scored, has_genset), str(nat.get("verified_days", 0)), doc.cell(status)])
        doc.csv.writerow([s["id"], s["name"], nat.get("saving_rs"), tun.get("saving_rs"), nat.get("claimed_rs"), nat.get("litres_saved"),
                          nat.get("verified_days"), bool(summ.get("preliminary")), ";".join(stale)])
        tot["nat"] += _finite(nat.get("saving_rs")) or 0.0
        tot["tun"] += _finite(tun.get("saving_rs")) or 0.0
        tot["claimed"] += _finite(nat.get("claimed_rs")) or 0.0
        tot["verified"] += int(nat.get("verified_days") or 0)
        if has_genset and _litres_saved(nat, scored, True) != "not metered":
            tot["litres"] += _finite(nat.get("litres_saved")) or 0.0
        out_rows.append(s["id"])
    inputs["computed"] = max(computed) if computed else None
    over = p1 < today
    prelim = bool(prelim_names) or not over
    if prelim:
        why = ([f"the figures of {', '.join(prelim_names)} are preliminary (each site's monthly report says why)"] if prelim_names else []) + \
              ([] if over else ["the month is not over"])
        said = "; ".join(why)
        doc.p("Preliminary. " + said[:1].upper() + said[1:] + ".", lead="Preliminary.")  # (not capitalize(): it lowercases the sites' names)
        doc.gap()
    if stale_names:
        doc.p(f"Recompute needed: {', '.join(stale_names)} {'has' if len(stale_names) == 1 else 'have'} days out of step, so "
              f"{'its' if len(stale_names) == 1 else 'their'} figures may not add up until they are recomputed.", lead="Recompute needed:")
        doc.gap()
    doc.h2("Sites")
    if not out_rows:
        doc.p("The company has no sites yet.")
        return prelim
    doc.table([doc.head("Site", "Saving vs native", "Saving vs tuned", "Claimed", "Diesel saved (Estimated)", "Verified days", "Status")] + rows
              + [["All sites", rs(tot["nat"]), rs(tot["tun"]), rs(tot["claimed"]), smart(tot["litres"], " L"), str(tot["verified"]), ""]],
              [45 * mm, 23 * mm, 23 * mm, 23 * mm, 22 * mm, 15 * mm, 29 * mm])
    doc.gap()
    doc.p("Each site is measured against a replay of its own inverter settings (native) and the best static table of its brand (tuned), on the same "
          "day's sun, load and outages; the claimed figure counts the verified days only. The totals add the sites' figures as each site's report "
          "states them.")
    return prelim


#: The pilot's pre-registered gates in words (the Pilot page's criteria-tab.tsx), with how each reads
TIER_WORDS = {"monitor": "monitor (the plan only recommends)", "advisory_writes": "advisory (a person applies each day's table)",
              "closed_loop": "closed loop (the edge writes the table)"}


def _reading(ok: bool | None) -> str:
    return "not measured yet" if ok is None else ("meets it" if ok else "falls short")


def verified_bound(days: list[dict]) -> tuple[float | None, int]:
    """Plan 15.3's statistic: the lower 80 % confidence bound of the daily verified increment over the tuned baseline (the tuned saving of each verified
    day), times 30; None with fewer than two verified days."""
    xs = [float(d["saving_tuned_rs"]) for d in days if d.get("fidelity") == "confirmed" and not d.get("claim_withheld") and not d.get("import_unknown")
          and _finite(d.get("saving_tuned_rs")) is not None]
    if len(xs) < 2:
        return None, len(xs)
    from scipy import stats
    mean = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))
    return (mean - float(stats.t.ppf(0.8, len(xs) - 1)) * sd / math.sqrt(len(xs))) * 30, len(xs)


def _pilot(doc: _Doc, site: dict, p0: date, p1: date, today: date, inputs: dict) -> tuple[bool, date]:
    """Plan 8.5's pilot write-up: the pre-registered criteria (the Pilot page's, stored when it started) filled in with the results from the pilot's
    start to the end of the chosen month, the switchback table, forecast accuracy and control health. Below the advisory tier it is a
    counterfactual-only report (R8 2.14). It carries the fee (the fee multiple), so only a role that sees the fee reads it (routes_reports)."""
    from .routes_savings import PILOT_DEFAULT
    site_id = site["id"]
    with store.connect() as con:
        r = con.execute("SELECT data, started FROM pilots WHERE site_id = ?", (site_id,)).fetchone()
    pilot = json.loads(r["data"]) if r else dict(PILOT_DEFAULT)
    criteria = {**PILOT_DEFAULT["criteria"], **(pilot.get("criteria") or {})}
    blocks = verify.switchback_blocks(site_id)
    start = p0
    try:
        if r and r["started"]:
            start = datetime.fromisoformat(str(r["started"]).replace("Z", "+00:00")).astimezone(PKT).date()
        elif blocks:
            start = date.fromisoformat(blocks[0]["block_start"])
    except ValueError:
        start = p0
    if start > p1:
        start = p0
    end = min(p1, today)
    summ = verify.summary(site_id, start.isoformat(), p1.isoformat(), see_fee=True)
    inputs.update(computed=summ.get("computed"), start=start.isoformat())
    prelim = _prelim_and_stale(doc, summ, p1 < today, True)
    doc.h2("Set-up")
    sign = pilot.get("signoff") or {}
    if r and r["started"]:
        when = datetime.fromisoformat(str(r["started"]).replace("Z", "+00:00")).astimezone(PKT)
        doc.p(f"Started on {when.day} {when:%B %Y}" + (f", signed off by {sign['name']}" if sign.get("name") else "") + ". The criteria below were fixed "
              "then" + (f" and amended {_plural(len(pilot.get('amendments') or []), 'time')} since." if pilot.get("amendments") else "."))
    else:
        doc.p("The pilot has not started: the criteria below are those on the Pilot page now, and can still change.")
    doc.p(f"Tier: {TIER_WORDS.get(site.get('tier'), str(site.get('tier')))}. " + (
        "Below the advisory tier no table is applied, so this is a counterfactual-only report: the plan's days claim nothing." if site.get("tier") == "monitor"
        else ""))
    doc.gap()
    doc.h2("Pre-registered criteria against the results")
    nat, tun = summ["baselines"].get("native", {}), summ["baselines"].get("tuned", {})
    days = summ.get("days") or []
    cost = sum(_finite(d.get("native_cost_rs")) or 0.0 for d in days)
    saving_pct = (_finite(nat.get("saving_rs")) or 0.0) / cost * 100 if cost > 0 else None
    cover = outage_cover(site, start, end)
    adequacy = (cover["entered_ok"] / cover["with_reserve"] * 100) if cover and cover["with_reserve"] else None
    plan_days = sum(1 for d in days if d.get("fidelity") in ("confirmed", "partial", "not_applied", "pending"))
    fidelity = sum(1 for d in days if d.get("fidelity") == "confirmed") / plan_days * 100 if plan_days else None
    fee = summ.get("fee") or {}
    fee_rs = _finite(fee.get("fee_rs_per_month")) or 0.0
    multiple = (_finite(fee.get("verified_saving_rs_per_month")) or 0.0) / fee_rs if fee_rs > 0 else None
    fc = forecast_skill(site_id, start, end)
    unserved = _finite(nat.get("unserved_kwh_actual"))
    bound, n_verified = verified_bound(days)
    c = criteria
    rows = [["Criterion", "Pre-registered", "Result", "Reading"],
            ["Saving at least (of the native arm's cost)", _pct(c["min_saving_pct"], 1), _pct(saving_pct),
             _reading(None if saving_pct is None else saving_pct >= c["min_saving_pct"])],
            ["Unserved critical energy at most", smart(c["max_unserved_kwh"], " kWh"), smart(unserved, " kWh"),
             _reading(None if unserved is None or not days else unserved <= c["max_unserved_kwh"])],
            ["Reserve adequacy at least (outages entered at or above the planned reserve)", _pct(c["reserve_adequacy_pct"], 0), _pct(adequacy, 0),
             _reading(None if adequacy is None else adequacy >= c["reserve_adequacy_pct"])],
            ["Plan fidelity at least (plan days confirmed applied)", _pct(c["fidelity_pct"], 0), _pct(fidelity, 0),
             _reading(None if fidelity is None else fidelity >= c["fidelity_pct"])],
            ["Verified saving at least (times the fee)", f"{c['fee_multiple']:g} times the fee",
             "no fee set" if multiple is None else f"{multiple:.1f} times the fee ({rs(fee.get('verified_saving_rs_per_month'))} a month against "
                                                    f"{rs(fee_rs)})", _reading(None if multiple is None else multiple >= c["fee_multiple"])],
            ["Solar forecast error at most (nRMSE)", _pct(c["forecast_pv_nrmse_max"] * 100, 0) + " of capacity",
             _pct(None if fc["pv_nrmse"] is None else fc["pv_nrmse"] * 100), _reading(None if fc["pv_nrmse"] is None else fc["pv_nrmse"] <=
                                                                                     c["forecast_pv_nrmse_max"])]]
    if bound is None:
        stat, reading = f"needs two verified days ({n_verified} so far)", "not measured yet"
    elif fee_rs <= 0:
        stat, reading = f"{rs(bound)} a month; no fee set to compare with", "not measured yet"
    else:
        stat = f"{rs(bound)} a month, {bound / fee_rs:.1f} times the fee"
        reading = "meets the success gate" if bound >= 3 * fee_rs else ("below the stop gate" if bound < 1.5 * fee_rs else
                                                                         "between the gates: extend once by 4 weeks")
    rows.append(["Lower 80 % bound of the daily verified saving over the tuned table, times 30",
                 "at least 3 times the fee to succeed; under 1.5 times the fee after 8 weeks stops the pilot", stat, reading])
    doc.table([[doc.cell(x) for x in row] for row in rows], [62 * mm, 45 * mm, 45 * mm, 28 * mm], right=False)
    doc.p("A gate marked “not measured yet” has no figure in the period. The figures run from the pilot's start to the end of the month "
          "chosen; Preliminary marks carry through.")
    doc.gap()
    doc.h2("Switchback")
    _switchback(doc, site_id, start, p1, whole=True)
    doc.gap()
    doc.h2("Forecast accuracy")
    doc.p(forecast_line(fc))
    doc.gap()
    doc.h2("Control health")
    doc.p(control_line(control_health(site_id, days, start, end)))
    doc.gap()
    _method(doc, site, summ)
    doc.csv.writerow(["day", "saving_native_rs", "saving_tuned_rs", "fidelity", "claim_withheld", "verified"])
    for d in days:
        doc.csv.writerow([d["day"], d.get("saving_native_rs"), d.get("saving_tuned_rs"), d.get("fidelity"),
                          d.get("claim_withheld") or ("import_unknown" if d.get("import_unknown") else None),
                          d.get("fidelity") == "confirmed" and not d.get("claim_withheld") and not d.get("import_unknown")])
    return prelim, start


#: The Mode A slot modes in words, as the Plan page's slot table says them (slot-words.ts)
MODE_WORDS = {"hold": "Hold", "charge_grid": "Charge from grid", "charge_pv_only": "Charge from solar only", "discharge": "Discharge",
              "self_use": "Self-use", "export": "Export", "backup": "Backup"}


def day_words(d: date) -> str:
    """"Monday 9 November 2026"."""
    return f"{d:%A} {d.day} {d:%B %Y}"


def sheet_day(period: str | None, today: date) -> date:
    """The day a daily operations sheet is for: "YYYY-MM-DD", today or a day before it (audit pages-D-24: the sheet was asked for a month, which it
    ignored)."""
    text = str(period or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("The daily operations sheet is for a day: give it as YYYY-MM-DD")
    try:
        day = date.fromisoformat(text)
    except ValueError:
        raise ValueError("That day is not a day of the calendar") from None
    if day > today:
        raise ValueError("The daily operations sheet is for today or a day before: today's sheet carries tomorrow's table")
    return day


def period_bounds(kind: str, period: str, today: date) -> tuple[date, date]:
    """The first and last day a report of this kind covers: the day of a daily operations sheet, else the month ("YYYY-MM"). Raises ValueError."""
    if kind not in KINDS:
        raise ValueError(f"A report is one of {', '.join(KINDS)}")
    if kind == "daily_operations":
        day = sheet_day(period, today)
        return day, day
    try:
        return _period(period)
    except (ValueError, IndexError):
        raise ValueError("The period is a month as YYYY-MM") from None


def _slot_table(doc: _Doc, day: dict | None, heading: str) -> None:
    """One day of the plan's compiled table, dated in its heading, in the vendor's layout; its rows in the CSV under the day's date."""
    doc.h2(heading)
    slots = (day or {}).get("slots") or []
    if not slots:
        doc.p("The plan holds no table for this day.")
        return
    rows = [["Start", "End", "Mode", "Floor %", "Target %", "What it does"]]
    for s in slots:
        rows.append([s["start"], s["end"], MODE_WORDS.get(s["mode"], str(s["mode"]).replace("_", " ").capitalize()), f"{s['soc_floor_pct']:.0f}",
                     f"{s['soc_target_pct']:.0f}", s["intent"]])
        doc.csv.writerow([day["date"], s["start"], s["end"], s["mode"], s["soc_floor_pct"], s["soc_target_pct"], s["intent"]])
    doc.table(rows)
    if slots[-1]["end"] != "00:00":
        doc.p(f"The plan reaches only to {slots[-1]['end']} of this day; the next plan carries the table on.")


def _what_happened(doc: _Doc, site: dict, day: date) -> None:
    """A past day's measured energy, genset running and outages (R8 2.14: the daily sheet sets the plan beside what happened); the grid's rows only
    at a site with a grid connection."""
    doc.h2("What happened that day")
    rows = telemetry.days(site["id"], day.isoformat(), day.isoformat())
    if not rows:
        doc.p("No data was received for the day.")
        return
    d, grid = rows[0], _on_grid(site)
    table = [["Imported from the grid", num(d.get("import_kwh"), unit=" kWh")], ["Sent to the grid", num(d.get("export_kwh"), unit=" kWh")]] if grid else []
    table += [["Solar", num(d.get("pv_kwh"), unit=" kWh")], ["Load", num(d.get("load_kwh"), unit=" kWh")]]
    if (site.get("assets") or {}).get("genset"):
        table.append(["Genset", f"{num(d.get('genset_kwh'), unit=' kWh')}, running {num(d.get('genset_hours'), '.1f', ' h')}"])
    if grid:
        table.append(["Grid outages", num(d.get("outage_minutes"), ".1f", " h", scale=60)])
    table += [["Load not served", num(d.get("unserved_kwh"), ".1f", " kWh")], ["Highest demand", num(d.get("peak_kw"), unit=" kW")]]
    doc.table(table)


def _daily_sheet(doc: _Doc, site: dict, day: date, today: date, now_ts: int) -> None:
    """The daily operations sheet of `day` (audit pages-D-24 and flow-reports-alerts-members-keys-01: it printed the 48-hour plan's partial last
    day, undated, as tomorrow's table). Today's sheet prints the plan in force now as two dated day-tables, today's remainder and tomorrow (plan
    Part 7.1 step 6), tomorrow's being the one to key in; a past day's prints the day's table as the plan in force at its start had it, then what
    happened that day."""
    doc.csv.writerow(["date", "start", "end", "mode", "soc_floor_pct", "soc_target_pct", "intent"])
    ref = now_ts if day >= today else int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    plan = plans.active_at(site["id"], ref)
    if plan is None:
        doc.p("No plan was in force " + ("now" if day >= today else "at the start of the day") + ": the site had no plan yet, or control was paused "
              "or released to the inverter's own settings, so this sheet has no table.")
    else:
        try:
            made = datetime.fromisoformat(str(plan.get("generated_at"))).astimezone(PKT)
            doc.p(f"From the plan in force {'now' if day >= today else 'at the start of the day'}, made at {made:%H:%M} on {day_words(made.date())}.")
        except (TypeError, ValueError):
            pass
        days = {d.get("date"): d for d in ((plan.get("slots") or {}).get("days") or [])}
        if day >= today:
            nxt = day + timedelta(days=1)
            _slot_table(doc, days.get(day.isoformat()), f"Today, {day_words(day)}: the rest of the day")
            _slot_table(doc, days.get(nxt.isoformat()), f"Tomorrow, {day_words(nxt)}: the table to key in, in the vendor's layout")
        else:
            _slot_table(doc, days.get(day.isoformat()), f"{day_words(day)}: the table as planned at the start of the day")
    if day < today:
        _what_happened(doc, site, day)


def generate(kind: str, site_id: str, period: str, by: str) -> dict:
    """Render a report as PDF and CSV and keep its row. A company kind (COMPANY_KINDS) covers every site and is kept with an empty site_id."""
    now = datetime.now(PKT)
    today = now.date()
    p0, p1 = period_bounds(kind, period, today)
    site = None
    if kind in COMPANY_KINDS:
        site_id = ""
    else:
        site = sites.full(site_id)
        if site is None:
            raise KeyError(site_id)
    rid = store.new_id()
    doc = _Doc()
    title = TITLES[kind]
    if kind == "pilot" and site and site.get("tier") == "monitor":
        title = "Counterfactual-only report"  # R8 2.14: the pilot report needs advisory or closed loop
    # every text a person typed (the site's name, the connection's fields) is escaped before it reaches a Paragraph, which reads its text as markup: an
    # <img> tag there would make the server read a local file or fetch a URL into the report (plan Part 2.8: untrusted text is never rendered as markup)
    doc.title(f"{title}: {site['name']}" if site else title)
    sub_at = len(doc.story)
    made = f"Generated {now:%d %b %Y %H:%M} PKT by Gridlytics."
    inputs = {"kind": kind, "site": site_id, "period": period}
    prelim, start = None, p0
    if kind == "company_rollup":
        doc.p(f"{p0:%d %B %Y} to {p1:%d %B %Y}. Every site of the company. {made}")
        doc.gap(4)
        prelim = _company_rollup(doc, p0, p1, today, inputs)
    elif kind == "pilot":
        prelim, start = _pilot(doc, site, p0, p1, today, inputs)
    elif kind in ("monthly_savings", "energy_only"):
        prelim = _monthly(doc, site, kind, p0, p1, today, inputs)
    else:
        _daily_sheet(doc, site, p0, today, int(now.timestamp()))
    if site:
        span = f"For {day_words(p0)}." if kind == "daily_operations" else f"{start:%d %B %Y} to {p1:%d %B %Y}."
        doc.story[sub_at:sub_at] = [Paragraph(_text(f"{span} {_connection_words(site)} {made}"), doc.styles["Normal"]), Spacer(1, 4 * mm)]
    pdf_path = _dir() / f"{rid}.pdf"
    csv_path = _dir() / f"{rid}.csv"
    SimpleDocTemplate(str(pdf_path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                      title=title, author="Gridlytics").build(doc.story)
    csv_path.write_text(doc.buf.getvalue(), encoding="utf-8", newline="")  # the csv writer ends rows with CRLF already: no blank line between rows
    h = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:16]
    with store.connect() as con:
        # who generated it, for the list's "By" column (audit pages-D-25)
        con.execute("INSERT INTO reports (id, site_id, kind, period, generated_at, inputs_hash, pdf_path, csv_path, status, preliminary, generated_by) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (rid, site_id, kind, period, store._now(), h, str(pdf_path), str(csv_path), "done",
                                                       None if prelim is None else int(prelim), str(by or "")[:120]))
    replaced = _prune(kind, site_id, period)
    store.audit(by, f"generated a {kind.replace('_', ' ')} report", site_id, {"period": period, "report": rid, **({"replaced": replaced} if replaced else {})})
    return get(rid)


#: The copies of one report (its kind, site and period) kept, newest first: an older copy is removed, row and files, when a newer one is made
#: (audit security-roles-08: nothing capped the files a loop of POST /api/reports wrote). Each generation and download stays in the audit trail
KEEP_COPIES = 3


def _stored(path: str) -> Path:
    """A report file's path as the row keeps it."""
    return Path(path)


def _prune(kind: str, site_id: str, period: str) -> list[str]:
    """Remove the copies of this report beyond the newest KEEP_COPIES, rows and files; the ids removed, oldest first."""
    with store.connect() as con:
        old = con.execute("SELECT id, pdf_path, csv_path FROM reports WHERE kind = ? AND site_id = ? AND period = ? "
                          "ORDER BY generated_at DESC, rowid DESC LIMIT -1 OFFSET ?", (kind, site_id, period, max(1, int(KEEP_COPIES)))).fetchall()
        for r in old:
            con.execute("DELETE FROM reports WHERE id = ?", (r["id"],))
    for r in old:
        for p in (r["pdf_path"], r["csv_path"]):
            if p:
                _stored(p).unlink(missing_ok=True)
    return [r["id"] for r in reversed(old)]


def _row_out(r) -> dict:
    """A report row as the API answers it (never its files' paths). `preliminary` is what the PDF said: true or false for a monthly report, null for the
    daily operations sheet; a monthly report made before it was kept (schema version 2) reads true when its month was not over when it was made, else
    null (not known). `generated_by` is who generated it, "" for a report made before it was kept (schema version 5)."""
    d = {k: v for k, v in dict(r).items() if k not in ("pdf_path", "csv_path")}
    d["generated_by"] = d.get("generated_by") or ""
    p = d.get("preliminary")
    if p is not None:
        d["preliminary"] = bool(p)
    elif d.get("kind") in ("monthly_savings", "energy_only", "company_rollup", "pilot"):
        try:
            made = datetime.fromisoformat(d["generated_at"]).astimezone(PKT).date()
            d["preliminary"] = True if _period(d["period"])[1] >= made else None
        except (KeyError, TypeError, ValueError, IndexError):
            d["preliminary"] = None
    else:
        d["preliminary"] = None
    return d


def listing(site_id: str | None = None) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM reports" + (" WHERE site_id = ?" if site_id else "") + " ORDER BY generated_at DESC, rowid DESC LIMIT 200",
                           ((site_id,) if site_id else ())).fetchall()
    names = {s["id"]: s["name"] for s in sites.listing()}
    return [{**_row_out(r), "site_name": names.get(r["site_id"], "")} for r in rows]


def get(rid: str) -> dict | None:
    """One report as the list shows it, its site's name included."""
    with store.connect() as con:
        r = con.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
    if r is None:
        return None
    site = sites.get(r["site_id"]) if r["site_id"] else None
    return {**_row_out(r), "site_name": (site or {}).get("name", "")}


def file_path(rid: str, which: str) -> Path | None:
    with store.connect() as con:
        r = con.execute("SELECT pdf_path, csv_path FROM reports WHERE id = ?", (rid,)).fetchone()
    if r is None:
        return None
    p = _stored(r["pdf_path"] if which == "pdf" else r["csv_path"])
    return p if p.exists() else None
