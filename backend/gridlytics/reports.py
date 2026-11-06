"""Reports (plan Part 8.5): the monthly savings report per site, the energy-only (viewer-safe) report and the daily operations sheet, as PDF
(ReportLab, decision D13) and CSV, rendered from the same rows the API serves. Never the words guaranteed or optimal; Preliminary until 28 verified days.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
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
               "genset": "Genset fuel and running cost", "wear": "Battery wear", "carry_over": "Carry-over (stored energy at day end)",
               "mdi": "Maximum demand (MDI)"}


def _text(value) -> str:
    """A value as plain text inside a ReportLab Paragraph: &, < and > escaped, so it is never read as markup."""
    return escape(str(value))


def rs(x) -> str:
    if x is None:
        return "-"
    return ("-" if x < 0 else "") + "Rs " + f"{abs(x):,.0f}"


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
    if kind in ("monthly_savings", "energy_only"):
        summ = verify.summary(site_id, p0.isoformat(), p1.isoformat(), see_fee=False)
        inputs["computed"] = summ.get("computed")
        prelim = summ["preliminary"] or (p1 >= today)
        if prelim:
            story.append(Paragraph("<b>Preliminary.</b> Fewer than 28 verified days, the month is not over, or a fuel adjustment of the period is not yet billed.",
                                   styles["Normal"]))
            story.append(Spacer(1, 3 * mm))
        if kind == "monthly_savings":
            nat, tun = summ["baselines"].get("native", {}), summ["baselines"].get("tuned", {})
            story.append(Paragraph("Headline", styles["Heading2"]))
            story.append(_table([["Against", "Saving", "Claimed (applied days)", "Diesel saved", "Verified days"],
                                 ["The inverter's own settings (native capture)", rs(nat.get("saving_rs")), rs(nat.get("claimed_rs")),
                                  f"{nat.get('litres_saved', 0):,.0f} L", str(nat.get("verified_days", 0))],
                                 ["The best static table (tuned baseline)", rs(tun.get("saving_rs")), rs(tun.get("claimed_rs")),
                                  f"{tun.get('litres_saved', 0):,.0f} L", str(tun.get("verified_days", 0))]], [70 * mm, 25 * mm, 35 * mm, 25 * mm, 22 * mm]))
            story.append(Spacer(1, 3 * mm))
            story.append(Paragraph("Ledger by lever, against the native capture", styles["Heading2"]))
            rows = [["Lever", "Rupees"]] + [[LEVER_NAMES.get(k, k), rs(v)] for k, v in (nat.get("levers") or {}).items()]
            if nat.get("perfect_foresight_rs") is not None:
                rows.append(["Perfect foresight (not achievable, for reference)", rs(nat["perfect_foresight_rs"])])
            story.append(_table(rows, [110 * mm, 40 * mm]))
            story.append(Spacer(1, 2 * mm))
            story.append(Paragraph("Method: " + _text(summ["method"]) + ".", styles["Normal"]))
        story.append(Paragraph("Energy", styles["Heading2"]))
        tot = lambda k: sum((d.get(k) or 0) for d in tdays)
        story.append(_table([["Quantity", "Month"], ["Grid import", f"{tot('import_kwh'):,.0f} kWh"], ["Grid export", f"{tot('export_kwh'):,.0f} kWh"],
                             ["Solar", f"{tot('pv_kwh'):,.0f} kWh"], ["Load", f"{tot('load_kwh'):,.0f} kWh"], ["Genset", f"{tot('genset_kwh'):,.0f} kWh, "
                                                                                                                  f"{tot('genset_hours'):,.1f} h"],
                             ["Outage time", f"{tot('outage_minutes') / 60:,.1f} h"], ["Unserved critical load", f"{tot('unserved_kwh'):,.1f} kWh"],
                             ["Peak 30-minute import", f"{max([d.get('peak_kw') or 0 for d in tdays] or [0]):,.1f} kW"]], [80 * mm, 50 * mm]))
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("Daily rows", styles["Heading2"]))
        money = kind == "monthly_savings"
        head = ["Day", "Import kWh", "Solar kWh", "Outage h", "Genset h"] + (["Saving vs native", "Fidelity"] if money else [])
        by_day = {d["day"]: d for d in summ["days"]}
        rows = [head]
        w.writerow(["day", "import_kwh", "export_kwh", "pv_kwh", "load_kwh", "genset_hours", "litres", "outage_minutes", "peak_kw"] +
                   (["saving_native_rs", "saving_tuned_rs", "fidelity", "plan_ids"] if money else []))
        for d in tdays:
            s = by_day.get(d["day"], {})
            rows.append([d["day"], f"{d['import_kwh']:,.0f}", f"{d['pv_kwh']:,.0f}", f"{d['outage_minutes'] / 60:.1f}", f"{d['genset_hours']:.1f}"] +
                        ([rs(s.get("saving_native_rs")), s.get("fidelity") or "-"] if money else []))
            ids = ""
            if money:
                with store.connect() as con:
                    ids = ";".join(r[0] for r in con.execute("SELECT id FROM plans WHERE site_id = ? AND substr(belief_time,1,10) = ? AND status != 'failed'",
                                                              (site_id, d["day"])).fetchall()[:4])
            w.writerow([d["day"], d["import_kwh"], d["export_kwh"], d["pv_kwh"], d["load_kwh"], d["genset_hours"], d["litres"], d["outage_minutes"], d["peak_kw"]]
                       + ([s.get("saving_native_rs"), s.get("saving_tuned_rs"), s.get("fidelity"), ids] if money else []))
        story.append(_table(rows))
        cap = sites.native_latest(site_id)
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(f"Native capture version {_text(cap['version']) if cap else 'none'}; tariff versions as dated in the CSV annex. "
                               "Figures are measured against a replay of the inverter's own settings on the same day's sun, load and outages.", styles["Normal"]))
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
    csv_path.write_text(buf_csv.getvalue(), encoding="utf-8")
    h = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:16]
    with store.connect() as con:
        con.execute("INSERT INTO reports (id, site_id, kind, period, generated_at, inputs_hash, pdf_path, csv_path, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (rid, site_id, kind, period, store._now(), h, str(pdf_path), str(csv_path), "done"))
    store.audit(by, f"generated a {kind.replace('_', ' ')} report", site_id, {"period": period})
    return get(rid)


def listing(site_id: str | None = None) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM reports" + (" WHERE site_id = ?" if site_id else "") + " ORDER BY generated_at DESC LIMIT 200",
                           ((site_id,) if site_id else ())).fetchall()
    names = {s["id"]: s["name"] for s in sites.listing()}
    return [{k: v for k, v in {**dict(r), "site_name": names.get(r["site_id"], "")}.items() if k not in ("pdf_path", "csv_path")} for r in rows]


def get(rid: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
    return {k: v for k, v in dict(r).items() if k not in ("pdf_path", "csv_path")} if r else None


def file_path(rid: str, which: str) -> Path | None:
    with store.connect() as con:
        r = con.execute("SELECT pdf_path, csv_path FROM reports WHERE id = ?", (rid,)).fetchone()
    if r is None:
        return None
    p = Path(r["pdf_path"] if which == "pdf" else r["csv_path"])
    return p if p.exists() else None
