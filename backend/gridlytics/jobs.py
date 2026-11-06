"""Background jobs (master PS-17): the optimizer's search run in a small thread pool, so that a long search does not hold a page's request open.

A job is started by a POST that returns its id, read with a GET (candidates done of the total, and the result once it is done) and stopped with a DELETE. Jobs and
their results are kept in memory, not in the database: a restart of the server drops them, and a page that finds its job gone starts the search again. The
result of a search is kept by the hash of its request and of the settings it depends on, so the same search asked again is answered at once.

What a job holds is scoped to the workspace and to the user who started it: another user, or another workspace, gets "not found" for it, as for an id that never
was. A workspace is a company's database and an organisation in it (one server keeps a database per company, each with its organisation "default", decisions-v2
section 5): the limits, the reads and the cache are each company's own, and the search runs in a copy of the request's context, so it works in the request's
company. A result is kept as the search made it, costs included, and each reader's role is applied when the result is read (`api._costless`), not when it is made:
so a result that an administrator's search left in the cache is never read by a sales user with its costs.
"""

from __future__ import annotations

import contextvars
import hashlib
import itertools
import json
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor

from . import store


class Cancelled(Exception):
    """A job stopped by its owner or found abandoned (raised by the work through `job.cancel`)."""

#: Searches that run at once (the rest wait their turn), the jobs one workspace may have waiting or running, and one user's share of those
WORKERS = 2
MAX_ACTIVE_PER_WORKSPACE = 4
MAX_ACTIVE_PER_USER = 2
#: How long a finished job is kept for its page to read, how many are kept, and how many finished searches the result cache holds
KEEP_SECONDS = 3600
MAX_KEPT = 60
CACHE_ENTRIES = 16
#: A search nobody has read for this long is taken as abandoned (its tab was closed) and stopped, so it never holds a worker or a place in the limits (review R-172).
#: Well over the slowest a page reads it: a browser may wake a page in a background tab only once a minute.
ABANDON_SECONDS = 180
ABANDONED = f"Nobody read this search for {ABANDON_SECONDS // 60} minutes (its page was closed), so it was stopped"

STATES = ("queued", "running", "done", "cancelled", "failed")
_order = itertools.count()
ACTIVE = ("queued", "running")


class Refused(Exception):
    """A job that cannot be started now (too many already waiting or running)."""


def _company() -> str:
    """The company the current request works in, named by its database file (the default file when none is set)."""
    return str(store.db_path())


class Job:
    def __init__(self, kind: str, org: str, owner: str, key: str, expected: int = 0, company: str = ""):
        self.id = uuid.uuid4().hex[:16]
        self.seq = next(_order)                # the order the searches were asked in (two can share a clock tick)
        self.kind, self.org, self.owner, self.key = kind, org, owner, key
        self.company = company                 # the company's database it was started in: its workspace is (company, org)
        self.state = "queued"
        self.done = 0
        self.total = expected                  # the most it will do, as the page counted it, until the search says better
        self.started: float | None = None
        self.result: dict | None = None
        self.error: str | None = None
        self.cached = False
        self.created = time.time()
        self.last_read = self.created          # when its owner last read it (the start counts): a search nobody reads is stopped (`abandoned`)
        self.finished: float | None = None
        self.cancel = threading.Event()
        self.future: Future | None = None

    def abandoned(self, now: float | None = None) -> bool:
        """Whether the search still waits or runs although nobody has read it for ABANDON_SECONDS (review R-172)."""
        return self.state in ACTIVE and (now if now is not None else time.time()) - self.last_read > ABANDON_SECONDS

    def view(self, with_result: bool = False) -> dict:
        out = {"id": self.id, "kind": self.kind, "state": self.state, "done": self.done, "total": self.total, "cached": self.cached, "error": self.error,
               "elapsed_seconds": round((self.finished or time.time()) - self.created, 2)}
        if self.state == "queued":
            out.update(queue_place(self))
        if with_result:
            out["result"] = self.result if self.state == "done" else None
        return out


_lock = threading.Lock()
_jobs: dict[str, Job] = {}
#: The checks a search makes a second, from the searches that finished last (smoothed), for the wait the queue says; None until one has
_rate: float | None = None


def queue_place(job: Job) -> dict:
    """Where a waiting search stands (review R-173): the searches the pool takes before it (every one waiting or running that was asked for first, in any workspace, as
    the pool is first come first served) and, once a search has finished to measure the pace by, about how long until a worker is free for it."""
    with _lock:
        ahead = [j for j in _jobs.values() if j.state in ACTIVE and not j.cancel.is_set() and j.seq < job.seq]
    out: dict = {"ahead": len(ahead)}
    if _rate:
        left = sum(max(j.total - j.done, 0) for j in ahead)
        out["eta_seconds"] = round(left / (_rate * WORKERS))
    return out
#: Finished searches by (company, request key): one company's search never answers another's, nor says that it was made
_cache: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
_pool: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="gridlytics-job")
    return _pool


def request_key(kind: str, request: dict, basis: object) -> str:
    """The hash a search is cached by: what was asked, and everything it was worked on (the settings the structure's answer depends on, the profile). Two requests
    with the same key are the same search."""
    text = json.dumps({"kind": kind, "request": request, "basis": basis}, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _forget(now: float) -> None:
    """Finished jobs older than KEEP_SECONDS go, and the oldest finished ones past MAX_KEPT. Called with the lock held."""
    finished = sorted((j for j in _jobs.values() if j.state not in ACTIVE), key=lambda j: j.finished or j.created)
    for j in finished:
        if now - (j.finished or j.created) > KEEP_SECONDS or len(finished) - finished.index(j) > MAX_KEPT:
            _jobs.pop(j.id, None)


def _stop_abandoned(now: float) -> None:
    """Every search nobody has read for ABANDON_SECONDS is stopped: one waiting never starts, one running ends at its next candidate. Called with the lock held."""
    for j in _jobs.values():
        if j.abandoned(now) and not j.cancel.is_set():
            j.cancel.set()
            j.error = ABANDONED
            if j.future is not None and j.future.cancel():
                j.state, j.finished = "cancelled", now


def start(kind: str, org: str, owner: str, key: str, run, expected: int = 0) -> Job:
    """Start a job, or answer it at once from the cache, or join the same search this user already has running. `run(progress, cancelled)` makes the result;
    `expected` is the most it will check, for the wait a search queued behind it is told (review R-173)."""
    now = time.time()
    company = _company()
    cached = (company, key)
    with _lock:
        _forget(now)
        _stop_abandoned(now)
        # a job that has been cancelled is not joined (it ends as cancelled at its next candidate), and no longer counts toward the limits
        same = next((j for j in _jobs.values() if j.company == company and j.org == org and j.owner == owner and j.key == key and j.state in ACTIVE
                     and not j.cancel.is_set()), None)
        if same is not None:
            return same
        job = Job(kind, org, owner, key, expected, company)
        hit = _cache.get(cached)
        if hit is not None:
            _cache.move_to_end(cached)
            job.state, job.result, job.cached, job.finished = "done", hit, True, now
            job.done = job.total = hit.get("evaluated", 0)
            _jobs[job.id] = job
            return job
        mine = [j for j in _jobs.values() if j.company == company and j.org == org and j.state in ACTIVE and not j.cancel.is_set()]
        if len(mine) >= MAX_ACTIVE_PER_WORKSPACE:
            # how many are the person's own, which they can see and cancel (review R-172)
            own = sum(1 for j in mine if j.owner == owner)
            raise Refused(f"This workspace already has {len(mine)} searches waiting or running ({own or 'none'} of them yours); wait for one to finish, or cancel one of yours "
                          "under Your searches on the Structure optimizer")
        if sum(1 for j in mine if j.owner == owner) >= MAX_ACTIVE_PER_USER:
            raise Refused(f"You already have {MAX_ACTIVE_PER_USER} searches waiting or running; wait for one to finish, or cancel one")
        _jobs[job.id] = job

    def progress(done: int, total: int) -> None:
        job.done, job.total = done, total

    def stopped() -> bool:
        """Cancelled, or abandoned: nobody has read it for ABANDON_SECONDS (review R-172)."""
        if not job.cancel.is_set() and job.abandoned():
            job.error = ABANDONED
            job.cancel.set()
        return job.cancel.is_set()

    def work() -> None:
        global _rate
        if stopped():                      # cancelled (or abandoned) in the moment between a worker taking it and starting
            job.state, job.finished = "cancelled", time.time()
            return
        job.state, job.started = "running", time.time()
        try:
            result = run(progress, stopped)
        except Cancelled:
            job.state = "cancelled"
        except ValueError as exc:
            job.state, job.error = "failed", str(exc)
        except Exception:        # the server's log has the trace; the person is told the search could not be made
            job.state, job.error = "failed", "The search could not be completed"
            import logging
            logging.getLogger("gridlytics").exception("job %s (%s) failed", job.id, kind)
        else:
            with _lock:
                job.result, job.state = result, "done"
                took = time.time() - (job.started or job.created)
                if job.done > 0 and took > 0:
                    _rate = job.done / took if _rate is None else 0.5 * _rate + 0.5 * job.done / took
                _cache[cached] = result
                while len(_cache) > CACHE_ENTRIES:
                    _cache.popitem(last=False)
        finally:
            job.finished = time.time()

    # a pool's thread has no context of its own: the copy carries the request's company (and organisation) into the search
    job.future = _executor().submit(contextvars.copy_context().run, work)
    return job


def get(job_id: str, org: str, owner: str) -> Job | None:
    """A job of this user in this workspace, or None: another's is as good as absent. Reading it says someone still waits for it (`abandoned`)."""
    with _lock:
        job = _jobs.get(job_id)
    if job is None or job.company != _company() or job.org != org or job.owner != owner:
        return None
    job.last_read = time.time()
    return job


def mine(org: str, owner: str) -> list[Job]:
    """This user's searches in this workspace, the newest first: those waiting or running and those finished and still kept (review R-172: a page that lost a search's
    id, after a reload, can find and cancel it)."""
    with _lock:
        _stop_abandoned(time.time())
        company = _company()
        return sorted((j for j in _jobs.values() if j.company == company and j.org == org and j.owner == owner), key=lambda j: -j.seq)


def cancel(job_id: str, org: str, owner: str) -> Job | None:
    """Stop a waiting or running job (it ends as cancelled, at the next candidate for a running one); a finished job is forgotten. None when there is no such job."""
    job = get(job_id, org, owner)
    if job is None:
        return None
    with _lock:
        if job.state in ACTIVE:
            job.cancel.set()
            if job.future is not None and job.future.cancel():      # still waiting for a worker: it never starts
                job.state, job.finished = "cancelled", time.time()
        else:
            _jobs.pop(job.id, None)
    return job


def wait(job: Job, timeout: float = 60.0) -> Job:
    """Block until the job has ended (for tests and scripts)."""
    end = time.time() + timeout
    while job.state in ACTIVE and time.time() < end:
        time.sleep(0.01)
    return job


def reset() -> None:
    """Forget every job and the cache (tests)."""
    global _rate
    _rate = None
    with _lock:
        for j in _jobs.values():
            j.cancel.set()
        _jobs.clear()
        _cache.clear()
