"""Reports (UI-14): generate, list and download; a viewer may generate and read the energy-only report only.

Who generates what (auth.PERMISSIONS): the monthly savings report is `export` (it carries money), the energy-only report `energy_report` (every
member, a viewer included: it carries no money), the daily operations sheet `run_jobs` (its period is a day, YYYY-MM-DD, today's by default; every
other kind's a month, YYYY-MM, this one by default), the company roll-up and the pilot report `export`. A period is never in the future nor before the
site's data starts (reports.check_period; audit flow-reports-alerts-members-keys-13). Who generates a kind lists and downloads it (audit role-operator-20), so an
operator neither lists nor downloads the monthly savings report or the company roll-up, which are savings exports. Each listed row says whether its
figures were `preliminary`. The company roll-up covers every site (no site_id; its row keeps an empty one). The pilot report carries the fee (its
fee multiple), so a role without `see_fee` neither lists nor downloads it.

Generating is bounded per person (audit security-roles-08): each report renders its PDF and writes two files, so a script looping POST /api/reports
filled the disk and loaded the one API process, whose solves degrade under CPU load (HANDOFF 8.A.3). One person generates at most GENERATE_LIMIT
reports an hour and one at a time, at most RENDERING reports render at once on the server, and reports.generate keeps only the newest copies of a
report (reports.KEEP_COPIES). A report renders as a job (plan Part 10.6; jobs.py): the request waits REPORT_WAIT_S for it and answers the report, else
202 with the job, read at GET /api/reports/jobs/:job_id; the same request asked again while nothing it reads has changed answers the report already
made (the request-hash cache, `_basis`), and one asked while it renders joins it.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager

from fastapi import APIRouter, Body, Response
from fastapi.responses import FileResponse

from . import auth, jobs, reports, store
from .routes_common import bad, site_or_404, today
from .schemas import Refusal

router = APIRouter()

#: The permission that generates each kind of report; another kind is refused by reports.generate after `run_jobs`
REPORT_PERMISSION = {"monthly_savings": "export", "energy_only": "energy_report", "daily_operations": "run_jobs", "company_rollup": "export",
                     "pilot": "export"}
#: The kinds that carry the fee: read only by a role that sees it
FEE_KINDS = ("pilot",)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


#: (reports, seconds): what one person may generate in the window, counted in auth's rate limiter. A month's end at a company of six sites is
#: about 19 reports; a loop is stopped. GRIDLYTICS_RATE_REPORTS changes the count; 0 turns it off
GENERATE_LIMIT = (_env_int("GRIDLYTICS_RATE_REPORTS", 30), 3600)
#: Reports rendering at once on the server, and how long a further one waits for its turn before it is refused
RENDERING = 2
RENDER_WAIT_S = 20.0
_rendering = threading.BoundedSemaphore(RENDERING)
_busy: set[tuple] = set()
_busy_lock = threading.Lock()


def _person() -> tuple:
    """Who is generating: the company and the person (one key for the workspace in open mode)."""
    return (str((auth.company.get() or {}).get("id") or ""), str((auth.current.get() or {}).get("id") or "open"))


def _minutes(seconds: int) -> str:
    m = max(1, -(-int(seconds) // 60))
    return f"{m} minute{'s' if m != 1 else ''}"


@contextmanager
def _turn():
    """The person's turn to render a report: refused (429, with Retry-After) while their previous one renders, when they have generated
    GENERATE_LIMIT reports in the window, or when the server's RENDERING slots stay taken for RENDER_WAIT_S."""
    who = _person()
    with _busy_lock:
        if who in _busy:
            raise Refusal(429, "Your previous report is still being generated; try again when it is ready", "too_many_requests", headers={"Retry-After": "5"})
        _busy.add(who)
    try:
        count, window = GENERATE_LIMIT
        if count and (wait := auth.limiter().hit(("reports", *who), count, window)):
            raise Refusal(429, f"One person may generate {count} reports an hour; try again in {_minutes(wait)}", "too_many_requests",
                          headers={"Retry-After": str(wait)})
        if not _rendering.acquire(timeout=RENDER_WAIT_S):
            raise Refusal(429, "Other reports are being generated; try again in a minute", "too_many_requests", headers={"Retry-After": "30"})
        try:
            yield
        finally:
            _rendering.release()
    finally:
        with _busy_lock:
            _busy.discard(who)


def _may_read(kind: str) -> bool:
    """Whether the signed-in role lists and downloads reports of this kind: the permission that generates it (audit role-operator-20: the monthly
    savings CSV is a savings export, refused to an operator on the Savings page, yet its download checked only see_costs), and see_fee for a kind
    that carries the fee."""
    return auth.can(REPORT_PERMISSION.get(kind, "export")) and (kind not in FEE_KINDS or auth.can("see_fee"))


@router.get("/api/reports")
def listing(site_id: str | None = None):
    return [r for r in reports.listing(site_id) if _may_read(r["kind"])]


def _org() -> str:
    """The workspace a report job belongs to: the company (every member of it may read a report of a kind they may generate)."""
    return "reports:" + str((auth.company.get() or {}).get("id") or "")


#: how long POST /api/reports waits for its report before it answers 202 with the job (a report renders in a second or two; a company roll-up of
#: many sites longer)
REPORT_WAIT_S = 20.0


#: what a report reads and the column that moves when a row of it is added or changed: the newest of each, for the site (of every site for a company
#: report), is the request-hash cache's basis (_basis)
_BASIS = (("savings_days", "MAX(computed)"), ("plans", "MAX(generated_at)"), ("table_issues", "MAX(rowid)"), ("applied", "MAX(rowid)"),
          ("applied", "MAX(fidelity)"), ("overrides", "MAX(rowid)"), ("overrides", "MAX(cleared)"), ("bills", "MAX(rowid)"))


def _basis(site_id: str) -> list:
    """What a report's figures may change with, for the request-hash cache: the company's newest change of anything but a report (every settings,
    tariff, bill, pilot and recompute action is audited), the newest savings day computed, plan made, table issued, table marked or confirmed,
    override placed or cleared and bill of the site (of any site for a company report), its newest minute read, and the Pakistan day (a period's
    preliminary flag and the daily sheet move with it)."""
    from . import telemetry
    where, args = ("WHERE site_id = ?", (site_id,)) if site_id else ("", ())
    with store.connect() as con:
        out = [con.execute("SELECT MAX(id) FROM audit WHERE action NOT LIKE '%report%'").fetchone()[0]]
        out += [con.execute(f"SELECT {what} FROM {table} {where}", args).fetchone()[0] for table, what in _BASIS]
    from . import sites
    out.append([(telemetry.latest(sid) or {}).get("ts") for sid in ([site_id] if site_id else [x["id"] for x in sites.listing()])])
    return [*out, today().isoformat()]


def _report_answer(job: jobs.Job, response: Response) -> dict:
    """The report once its job has made it (or the report the cache answered, while it is still kept), else 202 with `{status, job_id}`."""
    if job.state == "done" and job.result is not None:
        r = reports.get(job.result["id"])
        if r is not None:
            return {**r, "job_id": job.id}
    if job.state in jobs.ACTIVE:
        response.status_code = 202
        return {"status": job.state, "job_id": job.id}
    raise Refusal(500 if job.state == "failed" else 409, job.error or "The report could not be generated; try again", "report_failed")


@router.post("/api/reports")
def generate(response: Response, inp: dict = Body(...)):
    kind = str(inp.get("kind", "monthly_savings"))
    auth.need(REPORT_PERMISSION.get(kind, "run_jobs"), "Generating this report")
    site_id = ""
    if kind not in reports.COMPANY_KINDS:  # the company roll-up covers every site
        site_id = str(inp.get("site_id", ""))
        site_or_404(site_id)
    # the daily operations sheet is for a day (audit pages-D-24), today's by default; the other kinds for a month, this one by default
    period = str(inp.get("period") or (today().isoformat() if kind == "daily_operations" else today().strftime("%Y-%m")))
    try:
        # a month as YYYY-MM (a day for the daily sheet), not in the future and not before the site's data (audit flow-reports-alerts-members-keys-13);
        # a request refused for its kind or period is not counted against the person
        reports.check_period(kind, site_id, period, today())
    except ValueError as exc:
        raise bad(exc) from None
    # the same report asked again with nothing it reads changed is the report already made, and one asked while it renders joins it: neither
    # renders nor counts again (round 7, storage-perf follow-up of security-roles-08; plan Part 10.6's request-hash cache)
    key = jobs.request_key("report", {"kind": kind, "site_id": site_id, "period": period}, _basis(site_id))
    org = _org()
    same = jobs.find(org, key)
    if same is not None and (same.state in jobs.ACTIVE or (same.result and reports.get(same.result["id"]))):
        jobs.get(same.id, org, None)
        return _report_answer(jobs.wait(same, REPORT_WAIT_S), response)
    who = auth.actor()
    turn = _turn()
    turn.__enter__()                       # the person's turn and the hourly count are taken now, so a refusal is the request's 429

    def run(progress, stopped) -> dict:
        try:
            return reports.generate(kind, site_id, period, who)
        finally:
            turn.__exit__(None, None, None)
    try:
        job = jobs.start("report", org, "".join(_person()), key, run, cache=False, abandon=False)
    except jobs.Refused as exc:
        turn.__exit__(None, None, None)
        raise Refusal(429, str(exc), "too_many_requests", headers={"Retry-After": "30"}) from None
    except BaseException:
        turn.__exit__(None, None, None)
        raise
    return _report_answer(jobs.wait(job, REPORT_WAIT_S), response)


@router.get("/api/reports/jobs/{job_id}")
def report_job(job_id: str, response: Response):
    """A report that outlasted POST /api/reports' wait: `{status: queued | running, job_id}` (202) while it renders, then the report as the list shows
    it. One the company does not have, or one forgotten (an hour after it ended, or after a restart), is a 404."""
    job = jobs.get(job_id, _org(), None)
    if job is None or job.kind != "report":
        raise Refusal(404, "No such report job: it ended over an hour ago, or the server restarted since", "not_found")
    if job.result is not None and not _may_read(job.result.get("kind", "")):
        raise Refusal(404, "No such report job: it ended over an hour ago, or the server restarted since", "not_found")
    return _report_answer(job, response)


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
