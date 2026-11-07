"""Reports (UI-14): generate, list and download; a viewer may generate and read the energy-only report only.

Who generates what (auth.PERMISSIONS): the monthly savings report is `export` (it carries money), the energy-only report `energy_report` (every member, a
viewer included: it carries no money), the daily operations sheet `run_jobs`. Each listed row says whether its figures were `preliminary`.
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from . import auth, reports, store
from .routes_common import bad, site_or_404, today
from .schemas import Refusal

router = APIRouter()

#: The permission that generates each kind of report; another kind is refused by reports.generate after `run_jobs`
REPORT_PERMISSION = {"monthly_savings": "export", "energy_only": "energy_report", "daily_operations": "run_jobs"}


@router.get("/api/reports")
def listing(site_id: str | None = None):
    rows = reports.listing(site_id)
    if not auth.can("see_costs"):
        rows = [r for r in rows if r["kind"] == "energy_only"]
    return rows


@router.post("/api/reports")
def generate(inp: dict = Body(...)):
    kind = str(inp.get("kind", "monthly_savings"))
    auth.need(REPORT_PERMISSION.get(kind, "run_jobs"), "Generating this report")
    site_id = str(inp.get("site_id", ""))
    site_or_404(site_id)
    period = str(inp.get("period") or today().strftime("%Y-%m"))
    try:
        return reports.generate(kind, site_id, period, auth.actor())
    except ValueError as exc:
        raise bad(exc) from None


@router.get("/api/reports/{rid}/{which}")
def download(rid: str, which: str):
    if which not in ("pdf", "csv"):
        raise Refusal(404, "Reports download as pdf or csv", "not_found")
    r = reports.get(rid)
    if r is None:
        raise Refusal(404, "No such report", "not_found")
    if r["kind"] != "energy_only" and not auth.can("see_costs"):
        raise Refusal(403, "This report carries money figures; your role sees the energy-only report", "role")
    p = reports.file_path(rid, which)
    if p is None:
        raise Refusal(404, "The report's file is missing; generate it again", "not_found")
    # every export is audited (plan Part 2.8 and 10.5)
    store.audit(auth.actor(), f"downloaded a report ({which})", r["site_id"], {"report": rid, "kind": r["kind"], "period": r["period"]})
    return FileResponse(p, media_type="application/pdf" if which == "pdf" else "text/csv", filename=f"gridlytics-{r['kind']}-{r['period']}.{which}")
