"""The ems-edge/1.0 HTTPS path (plan Part 7.3 and 7.5): telemetry batches, heartbeats, events, acks and capabilities from a site-bound API key.

The guard has already checked the key, its scope and that the site in the path is the key's own (403 wrong_site otherwise); a person's session never
reaches these routes (the guard refuses it with 403 key_required, and each route checks again). Idempotent by (site, stream, sequence): a repeated
sequence is acknowledged and not written twice. A sequence counts as received only once its batch is stored, so a batch that fails is never later
acknowledged as a duplicate.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, HTTPException
from pydantic import ValidationError

from . import auth, delivery, money, plans, sites, store, telemetry
from .routes_common import full_or_404, now_ts, out, site_or_404
from .schemas import Refusal

router = APIRouter()

#: The largest sequence number taken: the seq table keeps a 64-bit integer, and a JSON number above 2**53 is no longer exact everywhere
SEQ_MAX = 2 ** 53 - 1


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


def _seq_check(site_id: str, stream: str, seq: int) -> bool:
    """True when the sequence is new for the stream (nothing is recorded here: `telemetry.next_seq` records it once the message is stored)."""
    with telemetry.connect() as con:
        r = con.execute("SELECT last FROM seq WHERE site_id = ? AND stream = ?", (site_id, stream)).fetchone()
    return not (r and seq <= r["last"])


def _seen(site_id: str) -> None:
    sites.put_state(site_id, edge_last_seen=now_ts())


@router.post("/api/edge/{site_id}/telemetry")
def edge_telemetry(site_id: str, inp: dict = Body(...)):
    _key_only()
    site_or_404(site_id)
    msg = _check(delivery.TelemetryBatch, inp)
    _seen(site_id)
    # the batch is checked before its sequence is looked at, and the sequence is recorded only after the rows are written: a refused or failed batch is
    # answered as such every time it is sent, never acknowledged later as a duplicate (the minute table is idempotent by site and minute, so a batch
    # written twice by two concurrent sends stores the same rows)
    unknown = [c for c in msg.columns if c != "ts" and c not in telemetry.ALL]
    if "ts" not in msg.columns or unknown:
        raise Refusal(422, f"Unknown columns: {', '.join(unknown)}" if unknown else "The batch needs a ts column", "SCHEMA")
    if "grid_present" not in msg.columns and "grid_voltage_v" not in msg.columns:
        raise Refusal(422, "Every batch carries grid_present (or grid_voltage_v)", "SCHEMA")
    if not _seq_check(site_id, "telemetry", msg.seq):
        return {"ack": msg.seq, "duplicate": True}
    records = [dict(zip(msg.columns, row)) for row in msg.rows]
    res = telemetry.ingest(site_id, records, "edge")
    telemetry.next_seq(site_id, "telemetry", msg.seq)
    return {"ack": msg.seq, **res}


@router.post("/api/edge/{site_id}/heartbeat")
def edge_heartbeat(site_id: str, inp: dict = Body(...)):
    _key_only()
    site_or_404(site_id)
    msg = _check(delivery.Heartbeat, inp)
    sites.put_state(site_id, edge_last_seen=now_ts(), edge_heartbeat=msg.model_dump())
    warn = None
    if abs(msg.clock_offset_s) >= 900:
        warn = "clock_offset_900s: control demoted until the clock is set"
    elif abs(msg.clock_offset_s) >= 300:
        warn = "clock_offset_300s: peak slots are widened by 15 minutes"
    return {"ack": msg.seq, "warning": warn}


@router.post("/api/edge/{site_id}/events")
def edge_events(site_id: str, inp: dict = Body(...)):
    _key_only()
    site_or_404(site_id)
    msg = _check(delivery.EventBatch, inp)
    _seen(site_id)
    if not _seq_check(site_id, "events", msg.seq):
        return {"ack": msg.seq, "duplicate": True}
    with store.connect() as con:
        for e in msg.events:
            con.execute("INSERT INTO events (site_id, ts, type, source, detail) VALUES (?,?,?,?,?)", (site_id, e.ts, e.type, "edge", json.dumps(e.detail)))
    # read-back mismatch, a local override or a template change put the site in unknown state: pause control until a person resumes it
    if any(e.type in ("readback_mismatch", "local_override", "template_change") for e in msg.events):
        from datetime import datetime, timedelta, timezone
        until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        sites.add_override(site_id, {"kind": "pause_control", "until": until, "reason": "Edge reported an unknown device state"}, "edge")
    telemetry.next_seq(site_id, "events", msg.seq)
    return {"ack": msg.seq, "stored": len(msg.events)}


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
