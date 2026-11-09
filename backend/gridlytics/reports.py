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
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import plans, platform as platformdb, sites, store, telemetry, verify

PKT = timezone(timedelta(hours=5))
KINDS = ("monthly_savings", "energy_only", "daily_operations")
LEVER_NAMES = {"peak_window": "Peak-window import avoided", "offpeak_energy": "Off-peak energy (charging)", "export": "Export credit",
               "genset": "Genset fuel and running cost", "wear": "Battery wear", "carry_over": "Carry-over (change in stored energy)",
               "mdi": "Maximum demand (MDI)"}
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


def _table(rows, widths=None, header=True):
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    style = [("FONTSIZE", (0, 0), (-1, -1), 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (-1, -1), "RIGHT")]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
    t.setStyle(TableStyle(style))
    return t


def generate(kind: str, site_id: str, period: str, by: str) -> dict:
    if kind not in KINDS:
        raise ValueError(f"A report is one of {', '.join(KINDS)}")
    site = sites.full(site_id)
    if site is None:
        raise KeyError(site_id)
    try:
        p0, p1 = _period(period)
    except (ValueError, IndexError):
        raise ValueError("The period is a month as YYYY-MM") from None
    rid = store.new_id()
    styles = getSampleStyleSheet()
    story = []
    buf_csv = io.StringIO()
    w = csv.writer(buf_csv)
    today = datetime.now(PKT).date()
    title = {"monthly_savings": "Monthly savings report", "energy_only": "Monthly energy report", "daily_operations": "Daily operations sheet"}[kind]
    # every text a person typed (the site's name, the connection's fields) is escaped before it reaches a Paragraph, which reads its text as markup: an
    # <img> tag there would make the server read a local file or fetch a URL into the report (plan Part 2.8: untrusted text is never rendered as markup)
    story.append(Paragraph(f"{title}: {_text(site['name'])}", styles["Title"]))
    conn = (site.get("connections") or [None])[0]
    sub = f"{p0:%d %B %Y} to {p1:%d %B %Y}. " + (f"{_text(site['data'].get('disco') or '')} {_text(conn['data']['tariff_category'])}, "
                                                   f"{_text(conn['data']['regime'].replace('_', ' '))}. " if conn else "Off-grid. ")
    sub += f"Generated {datetime.now(PKT):%d %b %Y %H:%M} PKT by Gridlytics."
    story.append(Paragraph(sub, styles["Normal"]))
    story.append(Spacer(1, 4 * mm))
    tdays = telemetry.days(site_id, p0.isoformat(), p1.isoformat())
    inputs = {"kind": kind, "site": site_id, "period": period}
    prelim = None
    if kind in ("monthly_savings", "energy_only"):
        summ = verify.summary(site_id, p0.isoformat(), p1.isoformat(), see_fee=False)
        inputs["computed"] = summ.get("computed")
        prelim = bool(summ["preliminary"] or (p1 >= today))
        if prelim:
            story.append(Paragraph(_text(preliminary_sentence(summ, p1 < today)).replace("Preliminary.", "<b>Preliminary.</b>", 1), styles["Normal"]))
            story.append(Spacer(1, 3 * mm))
        stale = stale_sentence(summ.get("chain_stale_days") or []) if kind == "monthly_savings" else ""
        if stale:  # the page's notice, so a report made while days are out of step does not print totals that do not add up without a word
            story.append(Paragraph(_text(stale).replace("Recompute needed:", "<b>Recompute needed:</b>", 1), styles["Normal"]))
            story.append(Spacer(1, 3 * mm))
        if kind == "monthly_savings":
            nat, tun = summ["baselines"].get("native", {}), summ["baselines"].get("tuned", {})
            scored, has_genset = len(summ.get("days") or []), bool((site.get("assets") or {}).get("genset"))
            story.append(Paragraph("Headline", styles["Heading2"]))
            story.append(_table([["Against", "Saving", "Claimed (applied days)", "Diesel saved (Estimated)", "Verified days"],
                                 ["The inverter's own settings (native capture)", rs(nat.get("saving_rs")), rs(nat.get("claimed_rs")),
                                  _litres_saved(nat, scored, has_genset), str(nat.get("verified_days", 0))],
                                 ["The best static table (tuned baseline)", rs(tun.get("saving_rs")), rs(tun.get("claimed_rs")),
                                  _litres_saved(tun, scored, has_genset), str(tun.get("verified_days", 0))]], [70 * mm, 25 * mm, 35 * mm, 25 * mm, 22 * mm]))
            unmetered = max(int(nat.get("genset_unknown_days") or 0), int(tun.get("genset_unknown_days") or 0))
            if has_genset and 0 < unmetered:
                story.append(Paragraph(f"The genset's fuel was not metered on {unmetered} of {scored} days: those days add nothing to the litres or the "
                                       "genset lever.", styles["Normal"]))
            story.append(Spacer(1, 3 * mm))
            story.append(Paragraph("Ledger by lever, against the native capture", styles["Heading2"]))
            rows = [["Lever", "Rupees"]] + [[LEVER_NAMES.get(k, k), rs(v)] for k, v in (nat.get("levers") or {}).items()]
            if nat.get("perfect_foresight_rs") is not None:
                rows.append(["Perfect foresight (not achievable, for reference)", rs(nat["perfect_foresight_rs"])])
            story.append(_table(rows, [110 * mm, 40 * mm]))
            story.append(Paragraph("The ledger sums every scored day of the month, the days that claim nothing included (a table not applied, a native "
                                   "block, a withheld claim); the claimed figure above counts the verified days only.", styles["Normal"]))
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph("Method: " + _text(summ["method"]) + ".", styles["Normal"]))
        story.append(Paragraph("Energy", styles["Heading2"]))
        tot = lambda k: sum(float(d.get(k) or 0) for d in tdays if num(d.get(k)) != "-")
        story.append(_table([["Quantity", "Month"], ["Grid import", f"{tot('import_kwh'):,.0f} kWh"], ["Grid export", f"{tot('export_kwh'):,.0f} kWh"],
                             ["Solar", f"{tot('pv_kwh'):,.0f} kWh"], ["Load", f"{tot('load_kwh'):,.0f} kWh"], ["Genset", f"{tot('genset_kwh'):,.0f} kWh, "
                                                                                                                  f"{tot('genset_hours'):,.1f} h"],
                             ["Outage time", f"{tot('outage_minutes') / 60:,.1f} h"], ["Unserved critical load", f"{tot('unserved_kwh'):,.1f} kWh"],
                             ["Peak 30-minute import", f"{max([float(d.get('peak_kw') or 0) for d in tdays if num(d.get('peak_kw')) != '-'] or [0]):,.1f} kW"]],
                            [80 * mm, 50 * mm]))
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("Daily rows", styles["Heading2"]))
        money = kind == "monthly_savings"
        head = ["Day", "Import kWh", "Solar kWh", "Outage h", "Genset h"] + (["Saving vs native", "Fidelity"] if money else [])
        by_day = {d["day"]: d for d in summ["days"]}
        rows = [head]
        w.writerow(["day", "import_kwh", "export_kwh", "pv_kwh", "load_kwh", "genset_hours", "litres", "outage_minutes", "peak_kw"] +
                   (["saving_native_rs", "saving_tuned_rs", "fidelity", "plan_ids", "claim_withheld"] if money else []))
        for d in tdays:
            s = by_day.get(d["day"], {})
            rows.append([d["day"], num(d.get("import_kwh")), num(d.get("pv_kwh")), num(d.get("outage_minutes"), ".1f", scale=60),
                         num(d.get("genset_hours"), ".1f")] +
                        ([rs(s.get("saving_native_rs")), _day_fidelity(s)] if money else []))
            ids = ""
            if money:
                with store.connect() as con:
                    ids = ";".join(r[0] for r in con.execute("SELECT id FROM plans WHERE site_id = ? AND substr(belief_time,1,10) = ? AND status != 'failed'",
                                                              (site_id, d["day"])).fetchall()[:4])
            w.writerow([d["day"]] + [d.get(k) for k in ("import_kwh", "export_kwh", "pv_kwh", "load_kwh", "genset_hours", "litres", "outage_minutes", "peak_kw")]
                       + ([s.get("saving_native_rs"), s.get("saving_tuned_rs"), s.get("fidelity"), ids,
                           s.get("claim_withheld") or ("import_unknown" if s.get("import_unknown") else None)] if money else []))
        story.append(_table(rows))
        if money and any(s.get("claim_withheld") or s.get("import_unknown") for s in summ["days"]):
            story.append(Paragraph("A day marked \u201cclaim withheld\u201d claims nothing: its saving is shown for reference and is not in the claimed "
                                   "total, nor is it a verified day.", styles["Normal"]))
        cap = sites.native_latest(site_id)
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(f"Native capture version {_text(cap['version']) if cap else 'none'}; tariff versions as dated in the CSV annex. "
                               "Figures are measured against a replay of the inverter's own settings on the same day's sun, load and outages."
                               + _text(_genset_assumption(site)), styles["Normal"]))
    else:
        plan = plans.current(site_id)
        story.append(Paragraph("Tomorrow's slot table, in the vendor's layout", styles["Heading2"]))
        from . import delivery
        comp = (plan or {}).get("slots") or {"days": []}
        rows = [["Start", "End", "Mode", "Floor %", "Target %", "What it does"]]
        w.writerow(["date", "start", "end", "mode", "soc_floor_pct", "soc_target_pct", "intent"])
        for d in comp["days"][-1:]:
            for s in d["slots"]:
                rows.append([s["start"], s["end"], s["mode"], f"{s['soc_floor_pct']:.0f}", f"{s['soc_target_pct']:.0f}", s["intent"]])
                w.writerow([d["date"], s["start"], s["end"], s["mode"], s["soc_floor_pct"], s["soc_target_pct"], s["intent"]])
        story.append(_table(rows))
        _ = delivery
    pdf_path = _dir() / f"{rid}.pdf"
    csv_path = _dir() / f"{rid}.csv"
    SimpleDocTemplate(str(pdf_path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                      title=title, author="Gridlytics").build(story)
    csv_path.write_text(buf_csv.getvalue(), encoding="utf-8", newline="")  # the csv writer ends rows with CRLF already: no blank line between rows
    h = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:16]
    with store.connect() as con:
        con.execute("INSERT INTO reports (id, site_id, kind, period, generated_at, inputs_hash, pdf_path, csv_path, status, preliminary) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (rid, site_id, kind, period, store._now(), h, str(pdf_path), str(csv_path), "done",
                                                     None if prelim is None else int(prelim)))
    store.audit(by, f"generated a {kind.replace('_', ' ')} report", site_id, {"period": period})
    return get(rid)


def _row_out(r) -> dict:
    """A report row as the API answers it (never its files' paths). `preliminary` is what the PDF said: true or false for a monthly report, null for the
    daily operations sheet; a monthly report made before it was kept (schema version 2) reads true when its month was not over when it was made, else
    null (not known)."""
    d = {k: v for k, v in dict(r).items() if k not in ("pdf_path", "csv_path")}
    p = d.get("preliminary")
    if p is not None:
        d["preliminary"] = bool(p)
    elif d.get("kind") in ("monthly_savings", "energy_only"):
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
        rows = con.execute("SELECT * FROM reports" + (" WHERE site_id = ?" if site_id else "") + " ORDER BY generated_at DESC LIMIT 200",
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
    p = Path(r["pdf_path"] if which == "pdf" else r["csv_path"])
    return p if p.exists() else None
