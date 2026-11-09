"""Reports (UI-14): generate, list and download; a viewer may generate and read the energy-only report only.

Who generates a kind lists and downloads it (audit role-operator-20), so an operator neither lists nor downloads the monthly savings report or the
company roll-up, which are savings exports. Who generates what (auth.PERMISSIONS): the monthly savings report is `export` (it carries money), the energy-only report `energy_report` (every member, a
viewer included: it carries no money), the daily operations sheet `run_jobs` (its period is a day, YYYY-MM-DD, today's by default; every other kind's a
month), the company roll-up and the pilot report `export`. Each listed row says
whether its figures were `preliminary`. The company roll-up covers every site (no site_id; its row keeps an empty one). The pilot report carries the
fee (its fee multiple), so a role without `see_fee` neither lists nor downloads it.
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse

from . import auth, reports, store
from .routes_common import bad, site_or_404, today
from .schemas import Refusal

router = APIRouter()

#: The permission that generates each kind of report; another kind is refused by reports.generate after `run_jobs`
REPORT_PERMISSION = {"monthly_savings": "export", "energy_only": "energy_report", "daily_operations": "run_jobs", "company_rollup": "export",
                     "pilot": "export"}
#: The kinds that carry the fee: read only by a role that sees it
FEE_KINDS = ("pilot",)


def _may_read(kind: str) -> bool:
    """Whether the signed-in role lists and downloads reports of this kind: the permission that generates it (audit role-operator-20: the monthly
    savings CSV is a savings export, refused to an operator on the Savings page, yet its download checked only see_costs), and see_fee for a kind
    that carries the fee."""
    return auth.can(REPORT_PERMISSION.get(kind, "export")) and (kind not in FEE_KINDS or auth.can("see_fee"))


@router.get("/api/reports")
def listing(site_id: str | None = None):
    return [r for r in reports.listing(site_id) if _may_read(r["kind"])]


@router.post("/api/reports")
def generate(inp: dict = Body(...)):
    kind = str(inp.get("kind", "monthly_savings"))
    auth.need(REPORT_PERMISSION.get(kind, "run_jobs"), "Generating this report")
    site_id = ""
    if kind not in reports.COMPANY_KINDS:  # the company roll-up covers every site
        site_id = str(inp.get("site_id", ""))
        site_or_404(site_id)
    # the daily operations sheet is for a day (audit pages-D-24), today's by default; the other kinds for a month, this one by default
    period = str(inp.get("period") or (today().isoformat() if kind == "daily_operations" else today().strftime("%Y-%m")))
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
    if not auth.can(REPORT_PERMISSION.get(r["kind"], "export")):
        raise Refusal(403, f"The {reports.TITLES.get(r['kind'], 'report').lower()} is for {auth.roles_words(REPORT_PERMISSION.get(r['kind'], 'export'))}",
                      "role")
    if r["kind"] in FEE_KINDS and not auth.can("see_fee"):
        raise Refusal(403, "The pilot report carries the fee, which your role does not see", "role")
    p = reports.file_path(rid, which)
    if p is None:
        raise Refusal(404, "The report's file is missing; generate it again", "not_found")
    # every export is audited (plan Part 2.8 and 10.5)
    store.audit(auth.actor(), f"downloaded a report ({which})", r["site_id"], {"report": rid, "kind": r["kind"], "period": r["period"]})
    # named after the site, the kind and the month (audit role-analyst-25): two sites' reports of a month are two files
    return FileResponse(p, media_type="application/pdf" if which == "pdf" else "text/csv", filename=reports.file_name(r, which))
