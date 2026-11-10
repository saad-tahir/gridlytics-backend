"""The ems-edge/1.0 HTTPS path (plan Part 7.3 and 7.5): telemetry batches, heartbeats, events, acks and capabilities from a site-bound API key.

The guard has already checked the key, its scope and that the site in the path is the key's own (403 wrong_site otherwise); a person's session never
reaches these routes (the guard refuses it with 403 key_required, and each route checks again). Idempotent by (site, stream, sequence, content): a
message is acknowledged as a duplicate, and not written twice, only when the same message (the same sequence and the same digest of its content, sent_at
and msg_id aside) was stored before (`edge_seen`). A batch below the last sequence that was never stored (a retry after a later batch went through) is
stored, and so is a counter that restarted (a replaced box, a reboot that lost it). A message counts as received only once it is stored, so a batch
that fails is never later acknowledged as a duplicate; an events batch is marked in the transaction that stores it. A malformed batch (an unknown or repeated column, a
ts outside 2015 to a day from now) is refused with 422 every time it is sent. An outage event re-solves the plan once telemetry shows it (OPT-15).
A message is at most 1 MiB however it is sent (413 too_large, DAT-03), and its free-form parts are bounded by the contract's models.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request
from pydantic import Field, ValidationError

from . import alerts, auth, contract, delivery, forecast, money, plans, sites, store, telemetry
from .routes_common import full_or_404, now_ts, out, put_state, site_or_404, state_change
from .schemas import Refusal, Strict, check

router = APIRouter()
log = logging.getLogger("gridlytics.edge")

#: The largest sequence number taken: the seq table keeps a 64-bit integer, and a JSON number above 2**53 is no longer exact everywhere
SEQ_MAX = contract.SEQ_MAX

#: A telemetry row's ts must lie between this instant and a day from now (as the CSV upload's window): a ts that is not finite, or one past SQLite's
#: 64-bit integer (1e20), would otherwise fail the write on every retry of its batch, and a millisecond or unset clock would store rows decades away
TS_EARLIEST = int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp())
TS_AHEAD_S = 86400

#: Events that put the site in an unknown state: control pauses until a person resumes it (U3's names for them act alike: contract.event_meaning). A
#: firmware change may move the register map under the template as a template change does (R6 C8): the cloud stops writing until a person resumes
PAUSE_EVENTS = ("readback_mismatch", "local_override", "template_change", "firmware_change")
#: Events with which the edge says it stopped running the plan, and the event that ends each state (R6 E18, 4.4): kept in the site's state
#: (`edge_reports`, the newest report by its ts wins) for the control state, and read from the events table for the day's fidelity
#: (plans.edge_fidelity)
REPORT_EVENTS = {"control_disabled": ("disabled", True), "control_enabled": ("disabled", False),
                 "fallback_engaged": ("fallback", True), "fallback_cleared": ("fallback", False)}
PAUSE_REASON = "Edge reported an unknown device state"

#: Event re-solves (plan Part 2.4, OPT-15: an outage event re-solves within a minute). The tick reads the grid state from telemetry, and only the whole
#: minutes before its own time (forecast.issue, minute_grid), so the re-solve waits until the event's minute has ended and the telemetry from that minute
#: on shows what the event says (`_shown`), and a site re-solves on events at most once every RESOLVE_GAP_S, as the manual re-solve; a re-solve not yet
#: due stays pending in the site's state and the next telemetry batch runs it. Only a telemetry batch, once stored, runs it without `_shown`, after
#: RESOLVE_WAIT_S from the event that decides it (`since` restarts when a newer event changes what telemetry must show) and only once the stored
#: telemetry holds a reading from a minute after the event's (`_caught_up`: a backlog sent oldest first after a stall does not run it on its chunks
#: from before the event), so a re-solve never runs on telemetry older than its event. The pending re-solve is read, merged and cleared in one write
#: transaction on the site's state (`_state_change`):
#: a batch clears only the pending re-solve it ran, never one an event stored meanwhile. It runs after the answer is sent, and a failed tick is
#: logged, never answered to the edge.
RESOLVE_EVENTS = ("outage_start", "outage_end")
RESOLVE_GAP_S = 30
RESOLVE_WAIT_S = 120
RESOLVE_STALE_S = 900
#: A tick takes the grid as down now when the last minute with a grid reading before it reads 0 and is at most this many minutes before the last whole
#: minute (forecast.outage's observed state: the newest batch may still be in flight); forecast's own constant where it names one
OBSERVED_LAG_MIN = int(getattr(forecast, "OBSERVED_LAG_MIN", 2))
#: How far back `_shown` reads: a re-solve is pending for at most RESOLVE_STALE_S, and only the newest grid reading decides
SHOWN_WINDOW_S = 3600
_last_event_solve: dict[str, float] = {}
_solve_lock = threading.Lock()


#: The largest edge message (plan DAT-03, R7 4: 413 above 1 MiB), however it is sent: one site key cannot fill the workspace file or the site's
#: carried state, which every tick and page reads (audit engine-delivery-23; the API's own cap for every other path is api.MAX_BODY)
EDGE_MAX_BODY = 1_048_576
TOO_LARGE = "An edge message is at most 1 MiB: send the rows or events in smaller batches"
#: The edge routes' body in the API description (they read it themselves, `_edge_body`)
_JSON_BODY = {"requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}}}


async def _edge_body(request: Request) -> dict:
    """The edge message's JSON object, read up to EDGE_MAX_BODY: a Content-Length over it is refused before a byte is read, and a body sent in chunks
    is counted as it arrives and refused once over, never read whole or parsed (413 too_large). A body that is not a JSON object is a 422 in one
    sentence, as a message that breaks the contract is."""
    try:
        announced = int(request.headers.get("content-length") or 0)
    except ValueError:
        announced = 0
    if announced > EDGE_MAX_BODY:
        raise Refusal(413, TOO_LARGE, "too_large")
    chunks, seen = [], 0
    async for chunk in request.stream():
        seen += len(chunk)
        if seen > EDGE_MAX_BODY:
            raise Refusal(413, TOO_LARGE, "too_large")
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        raise Refusal(422, "The message is not JSON", "SCHEMA") from None
    if not isinstance(body, dict):
        raise Refusal(422, "The message must be a JSON object", "SCHEMA")
    return body


def _check(model, body, site_id: str | None = None):
    """The message validated by its contract model (422 SCHEMA with the first error otherwise); an envelope site_id that is not the path's site (the
    key's own: the guard checked it) is refused with 403 wrong_site (DAT-03)."""
    try:
        msg = model.model_validate(body)
    except ValidationError as exc:
        e = exc.errors()[0]
        where = ".".join(str(x) for x in e["loc"])
        text = str(e["msg"]).removeprefix("Value error, ")
        raise Refusal(422, f"{where}: {text}" if where and not text.startswith(f"{where}:") else text, "SCHEMA") from None
    if getattr(msg, "seq", 0) > SEQ_MAX:
        raise Refusal(422, f"seq: at most {SEQ_MAX}", "SCHEMA")
    if site_id is not None and getattr(msg, "site_id", None) not in (None, site_id):
        raise Refusal(403, "The message names another site than the one its key is for", "wrong_site")
    return msg


def _key_only(detail: str = "The edge paths take the site's API key") -> dict:
    """The site API key the request carries; a person's session is refused (plan Part 7.5: the HTTPS path takes the contract JSON with a site API key)."""
    user = auth.current.get() or {}
    if not user.get("api_key"):
        raise HTTPException(status_code=403, detail=detail)
    return user


#: How long a stored message's sequence and digest are kept: well past the edge's 7-day store-and-forward (U3 5.1), so every retry it can still send
#: is recognised. Older rows are pruned now and then (one message in SEEN_PRUNE_EVERY)
SEEN_KEEP_DAYS = 30
SEEN_PRUNE_EVERY = 64


def _digest(msg) -> str:
    """The digest of an edge message's content: the whole message but sent_at and msg_id, which an edge may stamp again on a retry. Two messages of a
    stream with the same sequence and digest are the same message (audit engine-delivery-03)."""
    body = msg.model_dump(exclude={"sent_at", "msg_id"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _was_stored(con, site_id: str, stream: str, seq: int, digest: str) -> bool:
    """Whether this very message (stream, sequence and digest) was stored before."""
    return con.execute("SELECT 1 FROM edge_seen WHERE site_id = ? AND stream = ? AND seq = ? AND digest = ?",
                       (site_id, stream, seq, digest)).fetchone() is not None


def _mark_stored(con, site_id: str, stream: str, seq: int, digest: str) -> None:
    """Records the message as stored, in the caller's transaction (the one that stored it, where there is one)."""
    con.execute("INSERT OR IGNORE INTO edge_seen (site_id, stream, seq, digest, received_at) VALUES (?,?,?,?,?)",
                (site_id, stream, seq, digest, store._now()))
    if seq % SEEN_PRUNE_EVERY == 0:
        old = (datetime.now(timezone.utc) - timedelta(days=SEEN_KEEP_DAYS)).isoformat(timespec="seconds")
        con.execute("DELETE FROM edge_seen WHERE site_id = ? AND stream = ? AND received_at < ?", (site_id, stream, old))


def _state_change(site_id: str, change) -> tuple[dict, object]:
    """routes_common.state_change: the site's carried state read, changed and written in one write transaction (sites.put_state takes no lock)."""
    return state_change(site_id, change)


def _put_state(site_id: str, **changes) -> dict:
    """sites.put_state's change, made in one write transaction (`state_change`); the state as it now stands."""
    return put_state(site_id, **changes)


def _clear_pending(site_id: str, pend: dict) -> tuple[dict, bool]:
    """Clears the site's pending re-solve only when the stored one is still `pend` (read, compared and cleared in one transaction): an event stored
    meanwhile replaced it, and that newer re-solve stays pending. The state as it now stands, and whether it was cleared."""
    def change(cur):
        if cur.get("resolve_pending") == pend:
            return {"resolve_pending": None}, True
        return None, False
    return _state_change(site_id, change)


def _seen(site_id: str) -> dict:
    """Records that the edge was heard from now; the site's carried state, as it now stands."""
    return _put_state(site_id, edge_last_seen=now_ts())


def _row_time(v) -> float | None:
    """A row's ts in epoch seconds as `telemetry.ingest` reads it (a number, or an ISO 8601 string, UTC unless it says otherwise); None for a row ingest
    skips and counts (no ts, or text that is not a time); infinity for an ISO time that has no epoch second."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except OverflowError:                       # an integer past a float's range (JSON integers are unbounded)
            return math.inf
    if isinstance(v, str):
        try:
            t = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        try:
            return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
        except (OverflowError, OSError, ValueError):
            return math.inf
    return None


def _check_times(columns: list[str], rows: list[list]) -> None:
    """Refuses (422) a batch with a row whose ts is not finite or lies outside 2015 to a day from now, before anything is written: such a batch is answered
    the same way every time it is sent, so the edge drops it instead of retrying it for ever."""
    col = columns.index("ts")
    latest = now_ts() + TS_AHEAD_S
    bad = []
    for n, row in enumerate(rows):
        t = _row_time(row[col] if col < len(row) else None)
        if t is not None and (not math.isfinite(t) or t < TS_EARLIEST or t > latest):
            bad.append(n)
    if bad:
        first = rows[bad[0]][col]
        raise Refusal(422, f"{len(bad)} row{'s have' if len(bad) != 1 else ' has'} a ts that is not a time from 2015 to a day from now (the first is row "
                           f"{bad[0] + 1}: {first!r}); ts is epoch seconds or ISO 8601, UTC", "SCHEMA")


def _event_minute(e) -> int:
    """An event's minute in epoch seconds; now for a time that does not read."""
    try:
        t = datetime.fromisoformat(e.ts.strip().replace("Z", "+00:00"))
        return int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()) // 60 * 60
    except (ValueError, OverflowError, OSError):
        return now_ts() // 60 * 60


def _event_resolve(site_id: str) -> None:
    """The out-of-cycle tick after an outage event; the events are stored and acknowledged already, so a failure is logged, not answered."""
    try:
        r = plans.tick(site_id)
        log.info("event re-solve for site %s: %s", site_id, r.get("status"))
    except Exception:
        log.exception("event re-solve failed for site %s", site_id)


def _shown(site_id: str, pend: dict, now: int) -> bool:
    """True when a tick at `now` reads what the pending event says. The tick reads telemetry only up to the last whole minute before now, so the event's
    minute must have ended; and the last minute with a grid reading, from the event's minute on, must read the grid down after outage_start (recent
    enough for the tick to take it as the state now) or present after outage_end. A row that still reads the grid present in the minute it was lost (lost
    50 seconds in, the minute's reading taken before) does not show the outage: the next minute's row does."""
    got = _readings(site_id, pend, now)
    if got is None:
        return False                                  # the event's minute has not ended: a tick now would not read it
    grid, known, _ = got
    if not len(known):
        return False
    down = float(grid[known[-1]]) == 0.0
    if pend.get("event") == "outage_start":
        return down and len(grid) - 1 - int(known[-1]) <= OBSERVED_LAG_MIN
    return not down


def _readings(site_id: str, pend: dict, now: int) -> tuple[np.ndarray, np.ndarray, bool] | None:
    """The minute grid a tick at `now` reads from the pending event's minute on (at most SHOWN_WINDOW_S of it), the indexes of its minutes with a grid
    reading, and whether the first of those minutes comes after the event's own minute; None while the event's minute has not ended."""
    minute = int(pend.get("minute", 0))
    cut = now // 60 * 60
    if cut <= minute:
        return None
    start = max(minute, cut - SHOWN_WINDOW_S)
    grid = telemetry.minute_grid(site_id, start, cut)["grid"]
    return grid, np.flatnonzero(~np.isnan(grid)), start > minute


def _caught_up(site_id: str, pend: dict, now: int) -> bool:
    """True when the stored telemetry a tick at `now` reads holds a grid reading from a minute after the event's own minute: telemetry has reached past
    the event (a reading in the event's minute may have been taken before it). Only then does the RESOLVE_WAIT_S fallback run a re-solve that telemetry
    does not show: a backlog chunk that ends before the event, stored after a stall, never runs it."""
    got = _readings(site_id, pend, now)
    if got is None:
        return False
    _, known, after = got
    return bool(len(known)) and (after or int(known[-1]) >= 1)


def _resolve_pending(site_id: str, state: dict | None, background: BackgroundTasks, active: bool = True, wait_over: bool = False) -> str | None:
    """Runs the site's pending event re-solve when it is due: 'scheduled' (it runs once the answer is sent), 'waiting_for_telemetry' (the telemetry a
    tick reads does not show the event yet: see `_shown`), 'rate_limited' (the site re-solved on an event less than RESOLVE_GAP_S ago), or None.
    `wait_over` (a telemetry batch, once stored) also runs it when RESOLVE_WAIT_S have passed since its event without telemetry showing it, once the
    stored telemetry reaches past the event's minute (`_caught_up`: a backlog chunk older than the event does not); an events batch never does, so a
    re-solve never runs on telemetry older than the event that decides it. A pending re-solve is dropped when the site is no
    longer active (the scheduler does not tick it either) or once RESOLVE_STALE_S have passed (a scheduled tick has re-solved since). It is cleared
    only while the stored one is still the one decided on here (`_clear_pending`); one an event replaced meanwhile is decided on in its turn."""
    for _ in range(3):
        pend = (state or {}).get("resolve_pending")
        if not isinstance(pend, dict):
            return None
        now = now_ts()
        since = int(pend.get("since", now))
        if not active or now - since > RESOLVE_STALE_S:
            state, cleared = _clear_pending(site_id, pend)
            if cleared:
                return None
            continue                                  # a newer event replaced it: decide on that one
        if not _shown(site_id, pend, now) and not (wait_over and now - since >= RESOLVE_WAIT_S and _caught_up(site_id, pend, now)):
            return "waiting_for_telemetry"
        with _solve_lock:
            before = _last_event_solve.get(site_id)
            if before is not None and time.monotonic() - before < RESOLVE_GAP_S:
                return "rate_limited"
            stamp = _last_event_solve[site_id] = time.monotonic()
        cleared = False
        try:
            state, cleared = _clear_pending(site_id, pend)
        finally:
            if not cleared:                           # nothing runs: the gap is not taken
                with _solve_lock:
                    if _last_event_solve.get(site_id) == stamp:
                        if before is None:
                            _last_event_solve.pop(site_id, None)
                        else:
                            _last_event_solve[site_id] = before
        if cleared:
            background.add_task(_event_resolve, site_id)
            return "scheduled"
    return "waiting_for_telemetry"


@router.post("/api/edge/{site_id}/telemetry", openapi_extra=_JSON_BODY)
def edge_telemetry(site_id: str, background: BackgroundTasks, inp: dict = Depends(_edge_body)):
    _key_only()
    site = site_or_404(site_id)
    msg = _check(delivery.TelemetryBatch, inp, site_id)
    _seen(site_id)
    # the batch is checked before its sequence is looked at, and the sequence is recorded only after the rows are written: a refused or failed batch is
    # answered as such every time it is sent, never acknowledged later as a duplicate (the minute table is idempotent by site and minute, so a batch
    # written twice by two concurrent sends stores the same rows). U3's form (data stamped from ts_start every interval_s) is read as rows with a ts
    columns, rows = msg.table()
    ignored = [c for c in columns if c in contract.POINTS_NOT_STORED and c not in telemetry.ALL]
    unknown = [c for c in columns if c != "ts" and c not in telemetry.ALL and c not in ignored]
    if unknown:
        raise Refusal(422, f"Unknown columns: {', '.join(unknown)}", "SCHEMA")
    twice = sorted({c for c in columns if columns.count(c) > 1})
    if twice:
        raise Refusal(422, f"Columns named more than once: {', '.join(twice)}", "SCHEMA")
    if "grid_present" not in columns and "grid_voltage_v" not in columns:
        raise Refusal(422, "Every batch carries grid_present (or grid_voltage_v)", "SCHEMA")
    _check_times(columns, rows)
    digest = _digest(msg)
    with store.connect() as con:
        if _was_stored(con, site_id, "telemetry", msg.seq, digest):
            return {"ack": msg.seq, "duplicate": True}
    records = [{c: v for c, v in zip(columns, row) if c not in ignored} for row in rows]
    res = telemetry.ingest(site_id, records, "edge")
    with store.connect() as con:
        _mark_stored(con, site_id, "telemetry", msg.seq, digest)
    if "inverter_clock_offset_s" in columns:
        # the inverter's own clock (R6 C10 b): its newest reading in the batch moves the clock ladder as the heartbeat's does
        k = columns.index("inverter_clock_offset_s")
        seen = [float(r[k]) for r in rows if k < len(r) and isinstance(r[k], (int, float)) and not isinstance(r[k], bool) and math.isfinite(r[k])]
        if seen:
            _clock_update(site_id, seen[-1], "inverter")
    answer = {"ack": msg.seq, **res, **({"ignored_columns": ignored} if ignored else {})}
    # an outage event waiting for this batch (the minute that shows the grid lost or back) re-solves now; the batch is stored either way. The pending
    # re-solve is read now, after the batch is stored, not from the state at the request's start: an event stored meanwhile is the one that decides
    try:
        resolve = _resolve_pending(site_id, sites.state(site_id), background, site.get("status") == "active", wait_over=True)
    except Exception:
        log.exception("could not start the event re-solve for site %s", site_id)
        resolve = "failed"
    if resolve is not None:
        answer["resolve"] = resolve
    return answer


@router.post("/api/edge/{site_id}/heartbeat", openapi_extra=_JSON_BODY)
def edge_heartbeat(site_id: str, inp: dict = Depends(_edge_body)):
    _key_only()
    site_or_404(site_id)
    msg = _check(delivery.Heartbeat, inp, site_id)
    _put_state(site_id, edge_last_seen=now_ts(), edge_heartbeat=msg.model_dump(), edge_heartbeat_at=now_ts())
    try:
        # a fallback test in progress reads the edge's state from its heartbeats (round 7, audit flow-telemetry-plan-control-19)
        plans.fallback_test_observe(site_id, msg.control_state, now_ts())
    except Exception:
        log.exception("could not record the fallback test's progress for site %s", site_id)
    level, worst = _clock_update(site_id, msg.clock_offset_s, "edge")
    return {"ack": msg.seq, "warning": _clock_warning(level, worst)}


def _clock_warning(level: str, offset_s: float) -> str | None:
    off = f"the clock is {abs(offset_s):.0f} s off"
    return {"unsynced": f"clock_offset_120s: {off}; the cloud raised clock_unsynced. Set the clock",
            "widen": f"clock_offset_300s: {off}; the next plan message starts every discharge slot 15 minutes early and ends it 15 minutes late. "
                     "Set the inverter clock",
            "demoted": f"clock_offset_900s: {off}; control is demoted and no plan is sent until the offset is under {plans.CLOCK_WARN_S} s. "
                       "Set the inverter clock"}.get(level)


def _clock_update(site_id: str, offset_s: float, source: str) -> tuple[str, float]:
    """Records a clock offset (the edge's from its heartbeat, the inverter's from telemetry's inverter_clock_offset_s) and moves the site along R6
    C10's ladder on the worst of them (plans.clock_level; audit engine-delivery-10): clock_unsynced is raised as an event when the offset first goes
    over 120 s, and an audit line says when control is demoted (900 s) and restored (back under 120 s). The level now, and the worst offset."""
    def change(cur):
        c = dict(cur.get("edge_clock") or {})
        offsets = {**(c.get("offsets") or {}), source: round(float(offset_s), 1)}
        src, worst = max(offsets.items(), key=lambda kv: abs(kv[1]))
        before = c.get("level", "ok")
        level = plans.clock_level(worst, before == "demoted")
        return {"edge_clock": {"offsets": offsets, "offset_s": worst, "source": src, "level": level, "at": now_ts()}}, (before, level, worst)
    _, (before, level, worst) = _state_change(site_id, change)
    if before == "ok" and level != "ok":
        with store.connect() as con:
            con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)",
                        (site_id, store._now(), "clock_unsynced", "cloud", json.dumps({"offset_s": worst, "from": source})))
    if level == "demoted" and before != "demoted":
        store.audit("edge", "demoted control: the clock is off", site_id, {"offset_s": worst, "from": source})
    elif before == "demoted" and level != "demoted":
        store.audit("edge", "restored control: the clock is set", site_id, {"offset_s": worst, "from": source})
    return level, worst


def _pause_in(con, site_id: str) -> None:
    """A seven-day pause_control override, written in the caller's transaction (the row sites.add_override writes, with its audit line)."""
    now = store._now()
    until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds")
    con.execute("INSERT INTO overrides (id, site_id, kind, value, from_ts, until_ts, author, reason, created) VALUES (?,?,?,?,?,?,?,?,?)",
                (store.new_id(), site_id, "pause_control", None, now, until, "edge", PAUSE_REASON, now))
    store.audit("edge", "set an override", site_id, {"kind": "pause_control", "value": None, "until": until}, con=con)


@router.post("/api/edge/{site_id}/events", openapi_extra=_JSON_BODY)
def edge_events(site_id: str, background: BackgroundTasks, inp: dict = Depends(_edge_body)):
    """An event batch, stored once. The message is marked stored (`edge_seen`) in the same workspace-file transaction (BEGIN IMMEDIATE) that stores
    the events and any pause they call for, so two identical batches sent at once, or a retry after a failure, never store the events or the pause
    twice; a batch that fails stores nothing and is stored when sent again. An outage starting or ending re-solves the plan (OPT-15)."""
    _key_only()
    site = site_or_404(site_id)
    msg = _check(delivery.EventBatch, inp, site_id)
    _seen(site_id)
    digest = _digest(msg)
    with store.write_transaction() as con:
        if _was_stored(con, site_id, "events", msg.seq, digest):
            return {"ack": msg.seq, "duplicate": True}
        for e in msg.events:
            # stored as sent (U3's name or this contract's), with the device the edge names as its source
            con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)",
                        (site_id, e.ts, e.type, f"edge:{e.source}" if e.source else "edge", json.dumps(e.detail)))
        _mark_stored(con, site_id, "events", msg.seq, digest)
        # read-back mismatch, a local override or a template change put the site in unknown state: pause control until a person resumes it
        if any(contract.event_meaning(e.type) in PAUSE_EVENTS for e in msg.events):
            _pause_in(con, site_id)
    resolve = None
    outs = [e for e in msg.events if e.type in RESOLVE_EVENTS]
    if outs and site.get("status") == "active":
        cands = [(_event_minute(e), n, e.type) for n, e in enumerate(outs)]
        # the events are stored and acknowledged already: a failure to merge or start the re-solve is logged and answered as 'failed', never a 500 the
        # edge would retry (its retry is acknowledged as a duplicate, and the re-solve would be lost without a word)
        try:
            state, _ = _state_change(site_id, lambda cur: ({"resolve_pending": _merge_pending(cur.get("resolve_pending"), cands, now_ts())}, None))
            resolve = _resolve_pending(site_id, state, background)
        except Exception:
            log.exception("could not start the event re-solve for site %s", site_id)
            resolve = "failed"
    reports = [e for e in msg.events if e.type in REPORT_EVENTS]
    if reports:
        try:
            _state_change(site_id, lambda cur: (_merge_reports(cur.get("edge_reports"), reports), None))
        except Exception:
            log.exception("could not record the edge's control reports for site %s", site_id)
    answer = {"ack": msg.seq, "stored": len(msg.events), "resolve": resolve}
    unknown = msg.unknown_types()
    if unknown:
        answer["unknown_types"] = unknown       # stored; a newer edge's event types never void its batch (1.x adds optional things only)
    return answer


def _merge_reports(held, events) -> dict | None:
    """The site's `edge_reports` after an events batch: per state (disabled, fallback) the newest report by its ts, {on, at, cause}; a report older than
    the one held (sent late) changes nothing. None when nothing changes."""
    cur = dict(held) if isinstance(held, dict) else {}
    changed = False
    for e in sorted(events, key=_event_minute):
        key, on = REPORT_EVENTS[e.type]
        at = _event_minute(e)
        if at < int((cur.get(key) or {}).get("at", -1)):
            continue
        cause = e.detail.get("cause") or e.detail.get("reason")
        cur[key] = {"on": on, "at": at, "cause": str(cause)[:120] if on and cause else None}
        changed = True
    return {"edge_reports": cur} if changed else None


def _merge_pending(held, cands: list[tuple[int, int, str]], now: int) -> dict:
    """The pending re-solve after an events batch, from the one held (read in the same write transaction) and the batch's outage events as (minute,
    order, type). The newest event decides what telemetry must show (an outage that started and ended reads the grid present); a tie goes to the later
    one sent, and the held one loses a tie. `since` (from which RESOLVE_WAIT_S counts) is kept while the deciding minute and event are the held ones,
    and restarts when a newer event changes what telemetry must show: a re-solve held while telemetry stalled is not run at once on the next event, before any telemetry
    shows it. A held one older than RESOLVE_STALE_S is a leftover (the edge's telemetry stopped): the batch starts a fresh re-solve."""
    held = held if isinstance(held, dict) else {}
    if held and now - int(held.get("since", now)) > RESOLVE_STALE_S:
        held = {}
    if held:
        cands = [*cands, (int(held.get("minute", 0)), -1, held.get("event"))]
    minute, _, event = max(cands)
    same = bool(held) and (minute, event) == (int(held.get("minute", 0)), held.get("event"))
    return {"minute": minute, "event": event, "since": min(int(held.get("since", now)), now) if same else now}


@router.post("/api/edge/{site_id}/ack", openapi_extra=_JSON_BODY)
def edge_ack(site_id: str, inp: dict = Depends(_edge_body)):
    _key_only()
    site_or_404(site_id)
    msg = _check(delivery.Ack, inp, site_id)
    _seen(site_id)
    digest = _digest(msg)
    with store.write_transaction() as con:
        if _was_stored(con, site_id, "ack", msg.seq, digest):
            return {"ack": msg.seq, "duplicate": True}
        con.execute("INSERT INTO acks (id, site_id, plan_id, status, body, received_at) VALUES (?,?,?,?,?,?)",
                    (store.new_id(), site_id, msg.plan_id, msg.status, json.dumps(msg.model_dump()), store._now()))
        _mark_stored(con, site_id, "ack", msg.seq, digest)
    return {"ack": msg.seq}


@router.post("/api/edge/{site_id}/capabilities", openapi_extra=_JSON_BODY)
def edge_caps(site_id: str, inp: dict = Depends(_edge_body)):
    _key_only()
    site_or_404(site_id)
    msg = _check(delivery.Capabilities, inp, site_id)
    _seen(site_id)
    digest = _digest(msg)
    sent = _row_time(msg.sent_at)
    sent = int(sent) if sent is not None and math.isfinite(sent) else now_ts()
    with store.write_transaction() as con:
        if _was_stored(con, site_id, "capabilities", msg.seq, digest):
            cur = con.execute("SELECT tier_capable FROM sites WHERE id = ?", (site_id,)).fetchone()
            return {"ack": msg.seq, "duplicate": True, "tier_capable": cur["tier_capable"] if cur else None}
        con.execute("INSERT OR REPLACE INTO capabilities (site_id, seq, digest, received_at, sent_ts, body) VALUES (?,?,?,?,?,?)",
                    (site_id, msg.seq, digest, store._now(), sent, json.dumps(msg.model_dump())))
        _mark_stored(con, site_id, "capabilities", msg.seq, digest)
        # the newest message by its sent_at is the current one, whatever its sequence (a counter that restarted sends 0 on boot); a late retry of an
        # older message is kept for the record and sets nothing
        newer = con.execute("SELECT 1 FROM capabilities WHERE site_id = ? AND sent_ts > ? LIMIT 1", (site_id, sent)).fetchone()
    if newer is None:
        sites.set_tier_capable(site_id, msg.control_tier)
    return {"ack": msg.seq, "tier_capable": (sites.get(site_id) or {}).get("tier_capable", msg.control_tier)}


def _without_money(site_id: str, msg: dict) -> dict:
    """The signed message with every money field taken out of its body, signed again when anything was taken out (plan Part 10.5: plans served to a key
    carry no money fields; the signature covers the body, so the body is never changed after signing)."""
    body = {k: v for k, v in msg.items() if k != "signature"}
    clean = money.for_key(body)
    return msg if clean == body else delivery.sign(site_id, clean)


@router.get("/api/sites/{site_id}/plans/current/message")
def plan_message(site_id: str):
    """The signed plan message: what an edge (a plans:read key) pulls. Only a key pulls it, so each pull recorded in plan_messages is a real send (a
    person reads the plan at /plan). 409 `not_closed_loop` below the closed-loop tier (nothing is sent to the edge of a monitor or advisory site);
    404 `plan_expired` when the last plan is past its valid_until, `no_plan` before the first plan; 409 `signing_key_missing` (and a critical Control
    alert) when the site's signing key is not in the keys folder (audit engine-delivery-13)."""
    _key_only("The signed plan message is pulled by the site's edge with its API key; the plan itself is at /plan")
    site_or_404(site_id)
    try:
        msg = plans.signed_message(site_id)
        if msg is not None:
            msg = _without_money(site_id, msg)
    except delivery.KeyMissing as exc:
        # never a new key in its place (the edge holds the old public key): the edge is refused in words, and the site's people are told at once
        log.error("site %s: %s", site_id, exc)
        alerts._open(site_id, KEY_ALERT, "Control", "critical", "The plan-signing key is missing, so no plan reaches the edge: restore the keys "
                     "folder from the backup, or an administrator replaces the key", {"key_id": exc.key_id})
        raise Refusal(409, str(exc), "signing_key_missing") from None
    except plans.NotClosedLoop as exc:
        # only a closed-loop site's edge applies plans: below it the edge gets no table to write (and no send is counted)
        raise Refusal(409, str(exc), "not_closed_loop") from None
    except plans.CapsMismatch as exc:
        raise Refusal(409, f"{exc}, so the plan is not sent", "caps_mismatch") from None
    except plans.ClockDemoted as exc:
        raise Refusal(409, str(exc), "clock_unsynced") from None
    if msg is None:
        held = plans.held_info(site_id) or {}
        if held.get("reason") == plans.EXPIRED_REASON:
            raise Refusal(404, "The last plan has run out and no newer plan is in force, so the edge runs its fallback", "plan_expired")
        raise Refusal(404, "No active plan yet", "no_plan")
    _key_restored(site_id)
    return msg


@router.get("/api/sites/{site_id}/edge")
def edge_status(site_id: str):
    s = full_or_404(site_id)
    st = s.get("state") or {}
    with store.connect() as con:
        cap = con.execute("SELECT * FROM capabilities WHERE site_id = ? ORDER BY sent_ts IS NULL, sent_ts DESC, received_at DESC LIMIT 1",
                          (site_id,)).fetchone()
        acks = con.execute("SELECT plan_id, status, body, received_at FROM acks WHERE site_id = ? ORDER BY received_at DESC LIMIT 20", (site_id,)).fetchall()
        sent = con.execute("SELECT plan_id, key_id, sent_at FROM plan_messages WHERE site_id = ? ORDER BY sent_at DESC LIMIT 20", (site_id,)).fetchall()
    last = st.get("edge_last_seen")
    return out({"last_seen": last, "online": bool(last and now_ts() - last < 300), "heartbeat": st.get("edge_heartbeat"),
                "capabilities": json.loads(cap["body"]) if cap else None, "acks": [{**dict(a), "body": json.loads(a["body"])} for a in acks],
                "sent": [dict(r) for r in sent], "public_key": delivery.public_key(site_id) if auth.can("edit_sites") else None,
                # what the edge reports when it is not running the plan in force, and the clock-offset ladder's step (audit engine-delivery-10, 12)
                "reports": plans.edge_view(site_id), "clock": plans.clock_info(site_id)})


# ------------------------------------------------------------------------------------------------ the site's signing key (audit engine-delivery-13)

#: The alert raised while the site's signing key is missing (the edge's pull is refused) and cleared when an administrator replaces the key
KEY_ALERT = "signing_key_missing"


def _key_restored(site_id: str) -> None:
    """A message was signed: an open missing-key alert is closed (the keys folder was restored). One read a pull; a write only when one is open."""
    with store.connect() as con:
        held = con.execute("SELECT 1 FROM alerts WHERE site_id = ? AND rule = ? AND state IN ('open','acknowledged') LIMIT 1",
                           (site_id, KEY_ALERT)).fetchone()
    if held:
        alerts._clear(site_id, KEY_ALERT)


class KeyFields(Strict):
    """POST /edge/key: why the key is replaced (kept in the audit trail)."""
    reason: str = Field(min_length=1, max_length=300)


@router.post("/api/sites/{site_id}/edge/key")
def edge_key_replace(site_id: str, inp: dict = Body(default={})):
    """A new signing key for the site, the key in force retired (plan Part 2.8: when the key is lost, and on a handover): an administrator's, with a
    reason, never a key's. The permission, then the body are checked (a 403, then a 422 in one sentence). The answer is the new public key, the key
    ids retired and what to do next: the edge refuses the plans signed with the new key until its public key is provisioned there."""
    site_or_404(site_id)
    auth.need("change_tier", "Replacing the site's signing key")
    f = check(KeyFields, inp)
    new, retired = delivery.replace_key(site_id, auth.actor(), f.reason)
    alerts._clear(site_id, KEY_ALERT)
    return {"public_key": new, "retired": retired,
            "detail": "A new signing key is made and the old one retired: provision the new public key on the site's edge, which refuses the plans "
                      "signed with it until then"}
