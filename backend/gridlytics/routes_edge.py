"""The ems-edge/1.0 HTTPS path (plan Part 7.3 and 7.5): telemetry batches, heartbeats, events, acks and capabilities from a site-bound API key.

The guard has already checked the key, its scope and that the site in the path is the key's own (403 wrong_site otherwise); a person's session never
reaches these routes (the guard refuses it with 403 key_required, and each route checks again). Idempotent by (site, stream, sequence): a repeated
sequence is acknowledged and not written twice. A sequence counts as received only once its batch is stored, so a batch that fails is never later
acknowledged as a duplicate; an events batch claims its sequence in the transaction that stores it. A malformed batch (an unknown or repeated column, a
ts outside 2015 to a day from now) is refused with 422 every time it is sent. An outage event re-solves the plan once telemetry shows it (OPT-15).
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException
from pydantic import ValidationError

from . import auth, delivery, forecast, money, plans, sites, store, telemetry
from .routes_common import full_or_404, now_ts, out, put_state, site_or_404, state_change
from .schemas import Refusal

router = APIRouter()
log = logging.getLogger("gridlytics.edge")

#: The largest sequence number taken: the seq table keeps a 64-bit integer, and a JSON number above 2**53 is no longer exact everywhere
SEQ_MAX = 2 ** 53 - 1

#: A telemetry row's ts must lie between this instant and a day from now (as the CSV upload's window): a ts that is not finite, or one past SQLite's
#: 64-bit integer (1e20), would otherwise fail the write on every retry of its batch, and a millisecond or unset clock would store rows decades away
TS_EARLIEST = int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp())
TS_AHEAD_S = 86400

#: Events that put the site in an unknown state: control pauses until a person resumes it
PAUSE_EVENTS = ("readback_mismatch", "local_override", "template_change")
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


def _check(model, body):
    try:
        msg = model.model_validate(body)
    except ValidationError as exc:
        e = exc.errors()[0]
        raise Refusal(422, f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}", "SCHEMA") from None
    if getattr(msg, "seq", 0) > SEQ_MAX:
        raise Refusal(422, f"seq: at most {SEQ_MAX}", "SCHEMA")
    return msg


def _key_only(detail: str = "The edge paths take the site's API key") -> dict:
    """The site API key the request carries; a person's session is refused (plan Part 7.5: the HTTPS path takes the contract JSON with a site API key)."""
    user = auth.current.get() or {}
    if not user.get("api_key"):
        raise HTTPException(status_code=403, detail=detail)
    return user


def _seq_last(site_id: str, stream: str) -> int:
    """The last sequence the telemetry file records for the stream (-1 for none)."""
    with telemetry.reading() as con:
        r = con.execute("SELECT last FROM seq WHERE site_id = ? AND stream = ?", (site_id, stream)).fetchone()
    return int(r["last"]) if r else -1


def _seq_check(site_id: str, stream: str, seq: int) -> bool:
    """True when the sequence is new for the stream (nothing is recorded here: `telemetry.next_seq` records it once the message is stored)."""
    return seq > _seq_last(site_id, stream)


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


def _check_times(msg) -> None:
    """Refuses (422) a batch with a row whose ts is not finite or lies outside 2015 to a day from now, before anything is written: such a batch is answered
    the same way every time it is sent, so the edge drops it instead of retrying it for ever."""
    col = msg.columns.index("ts")
    latest = now_ts() + TS_AHEAD_S
    bad = []
    for n, row in enumerate(msg.rows):
        t = _row_time(row[col] if col < len(row) else None)
        if t is not None and (not math.isfinite(t) or t < TS_EARLIEST or t > latest):
            bad.append(n)
    if bad:
        first = msg.rows[bad[0]][col]
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


@router.post("/api/edge/{site_id}/telemetry")
def edge_telemetry(site_id: str, background: BackgroundTasks, inp: dict = Body(...)):
    _key_only()
    site = site_or_404(site_id)
    msg = _check(delivery.TelemetryBatch, inp)
    _seen(site_id)
    # the batch is checked before its sequence is looked at, and the sequence is recorded only after the rows are written: a refused or failed batch is
    # answered as such every time it is sent, never acknowledged later as a duplicate (the minute table is idempotent by site and minute, so a batch
    # written twice by two concurrent sends stores the same rows)
    unknown = [c for c in msg.columns if c != "ts" and c not in telemetry.ALL]
    if "ts" not in msg.columns or unknown:
        raise Refusal(422, f"Unknown columns: {', '.join(unknown)}" if unknown else "The batch needs a ts column", "SCHEMA")
    twice = sorted({c for c in msg.columns if msg.columns.count(c) > 1})
    if twice:
        raise Refusal(422, f"Columns named more than once: {', '.join(twice)}", "SCHEMA")
    if "grid_present" not in msg.columns and "grid_voltage_v" not in msg.columns:
        raise Refusal(422, "Every batch carries grid_present (or grid_voltage_v)", "SCHEMA")
    _check_times(msg)
    if not _seq_check(site_id, "telemetry", msg.seq):
        return {"ack": msg.seq, "duplicate": True}
    records = [dict(zip(msg.columns, row)) for row in msg.rows]
    res = telemetry.ingest(site_id, records, "edge")
    telemetry.next_seq(site_id, "telemetry", msg.seq)
    answer = {"ack": msg.seq, **res}
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


@router.post("/api/edge/{site_id}/heartbeat")
def edge_heartbeat(site_id: str, inp: dict = Body(...)):
    _key_only()
    site_or_404(site_id)
    msg = _check(delivery.Heartbeat, inp)
    _put_state(site_id, edge_last_seen=now_ts(), edge_heartbeat=msg.model_dump())
    warn = None
    if abs(msg.clock_offset_s) >= 900:
        warn = "clock_offset_900s: control demoted until the clock is set"
    elif abs(msg.clock_offset_s) >= 300:
        warn = "clock_offset_300s: peak slots are widened by 15 minutes"
    return {"ack": msg.seq, "warning": warn}


def _pause_in(con, site_id: str) -> None:
    """A seven-day pause_control override, written in the caller's transaction (the row sites.add_override writes, with its audit line)."""
    now = store._now()
    until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds")
    con.execute("INSERT INTO overrides (id, site_id, kind, value, from_ts, until_ts, author, reason, created) VALUES (?,?,?,?,?,?,?,?,?)",
                (store.new_id(), site_id, "pause_control", None, now, until, "edge", PAUSE_REASON, now))
    store.audit("edge", "set an override", site_id, {"kind": "pause_control", "value": None, "until": until}, con=con)


@router.post("/api/edge/{site_id}/events")
def edge_events(site_id: str, background: BackgroundTasks, inp: dict = Body(...)):
    """An event batch, stored once. The sequence is claimed in the same workspace-file transaction (BEGIN IMMEDIATE) that stores the events and any
    pause they call for, so two identical batches sent at once, or a retry after a failure, never store the events or the pause twice; a batch that
    fails stores nothing and is stored when sent again. An outage starting or ending re-solves the plan (OPT-15)."""
    _key_only()
    site = site_or_404(site_id)
    msg = _check(delivery.EventBatch, inp)
    _seen(site_id)
    floor = _seq_last(site_id, "events")      # the sequence the telemetry file recorded before the claim moved into the workspace file
    claim = f"edge_seq:{site_id}:events"
    with store.write_transaction() as con:
        r = con.execute("SELECT value FROM settings WHERE key = ?", (claim,)).fetchone()
        if msg.seq <= max(floor, int(json.loads(r["value"])) if r else -1):
            return {"ack": msg.seq, "duplicate": True}
        for e in msg.events:
            con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)", (site_id, e.ts, e.type, "edge", json.dumps(e.detail)))
        con.execute("INSERT INTO settings (key, value, updated) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated = excluded.updated", (claim, json.dumps(msg.seq), store._now()))
        # read-back mismatch, a local override or a template change put the site in unknown state: pause control until a person resumes it
        if any(e.type in PAUSE_EVENTS for e in msg.events):
            _pause_in(con, site_id)
    try:
        telemetry.next_seq(site_id, "events", msg.seq)    # kept in step for any reader of the telemetry file's sequences; the claim above decides
    except Exception:
        log.warning("could not record the events sequence of site %s in the telemetry file", site_id, exc_info=True)
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
    return {"ack": msg.seq, "stored": len(msg.events), "resolve": resolve}


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


@router.post("/api/edge/{site_id}/ack")
def edge_ack(site_id: str, inp: dict = Body(...)):
    _key_only()
    site_or_404(site_id)
    msg = _check(delivery.Ack, inp)
    _seen(site_id)
    with store.connect() as con:
        con.execute("INSERT INTO acks (id, site_id, plan_id, status, body, received_at) VALUES (?,?,?,?,?,?)",
                    (store.new_id(), site_id, msg.plan_id, msg.status, json.dumps(msg.model_dump()), store._now()))
    return {"ack": msg.seq}


@router.post("/api/edge/{site_id}/capabilities")
def edge_caps(site_id: str, inp: dict = Body(...)):
    _key_only()
    site_or_404(site_id)
    msg = _check(delivery.Capabilities, inp)
    with store.connect() as con:
        con.execute("INSERT OR REPLACE INTO capabilities (site_id, seq, received_at, body) VALUES (?,?,?,?)",
                    (site_id, msg.seq, store._now(), json.dumps(msg.model_dump())))
    sites.set_tier_capable(site_id, msg.tier)
    _seen(site_id)
    return {"ack": msg.seq, "tier_capable": msg.tier}


def _without_money(site_id: str, msg: dict) -> dict:
    """The signed message with every money field taken out of its body, signed again when anything was taken out (plan Part 10.5: plans served to a key
    carry no money fields; the signature covers the body, so the body is never changed after signing)."""
    body = {k: v for k, v in msg.items() if k != "signature"}
    clean = money.for_key(body)
    return msg if clean == body else delivery.sign(site_id, clean)


@router.get("/api/sites/{site_id}/plans/current/message")
def plan_message(site_id: str):
    """The signed plan message: what an edge (a plans:read key) pulls. Only a key pulls it, so each pull recorded in plan_messages is a real send (a
    person reads the plan at /plan)."""
    _key_only("The signed plan message is pulled by the site's edge with its API key; the plan itself is at /plan")
    site_or_404(site_id)
    msg = plans.signed_message(site_id)
    if msg is None:
        raise Refusal(404, "No active plan yet", "no_plan")
    return _without_money(site_id, msg)


@router.get("/api/sites/{site_id}/edge")
def edge_status(site_id: str):
    s = full_or_404(site_id)
    st = s.get("state") or {}
    with store.connect() as con:
        cap = con.execute("SELECT * FROM capabilities WHERE site_id = ? ORDER BY seq DESC LIMIT 1", (site_id,)).fetchone()
        acks = con.execute("SELECT plan_id, status, body, received_at FROM acks WHERE site_id = ? ORDER BY received_at DESC LIMIT 20", (site_id,)).fetchall()
        sent = con.execute("SELECT plan_id, key_id, sent_at FROM plan_messages WHERE site_id = ? ORDER BY sent_at DESC LIMIT 20", (site_id,)).fetchall()
    last = st.get("edge_last_seen")
    return out({"last_seen": last, "online": bool(last and now_ts() - last < 300), "heartbeat": st.get("edge_heartbeat"),
                "capabilities": json.loads(cap["body"]) if cap else None, "acks": [{**dict(a), "body": json.loads(a["body"])} for a in acks],
                "sent": [dict(r) for r in sent], "public_key": delivery.public_key(site_id) if auth.can("edit_sites") else None})
