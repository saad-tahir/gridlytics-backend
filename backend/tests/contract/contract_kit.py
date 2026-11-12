"""The kit of the ems-edge/1.0 contract suite (EMS_BUILD_PLAN.md Part 7.4, U3 section 5.8; round 7, audit tests-and-perf-15).

The plan describes one suite, `backend/tests/contract/`, shared by the twin now and by a real edge later; the tracker ticked DAT-03 on its tests 1
and 8 and DLV-03 on its tests 2 and 3 while it did not exist. Its eight tests, one file each:

1. schema validation of every message (test_contract_1_schema.py);
2. the signature's round trip, replay and foreign-site rejection (test_contract_2_signature.py);
3. the plan lifecycle: receive, ack, apply at the right minute, read-back, expiry fallback, supersede (test_contract_3_lifecycle.py);
4. Mode A grammar per brand, and the share of the plan's saving each brand's table keeps (test_contract_4_grammar.py);
5. budgets: the write past the brand's table writes is refused and the previous table remains (test_contract_5_budgets.py);
6. link loss: ten minutes silent, the cached plan continues, store-and-forward delivers in order (test_contract_6_link_loss.py);
7. overrides and template changes disable control (test_contract_7_overrides.py);
8. thirty simulated days of telemetry, every minute present or flagged (test_contract_8_telemetry.py).

The cloud's half of each test runs through the HTTP routes an edge uses (a site API key, never a person's session). The edge's half is
`ReferenceEdge` below: the twin has no executor for the checks an edge makes before it applies a plan (DLV-04 is [~]), so the suite holds them
here, and an edge (the twin's, a partner's) is held to the same rules. Each test works in a company file of its own in a temporary folder."""
from __future__ import annotations

import base64
import json
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from gridlytics import contract, sites, store, twin

PKT = timezone(timedelta(hours=5))
#: the published contract (schema files and examples), at the repository's root
CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts" / "ems-edge" / "1.0"
#: the edge-to-cloud messages, each posted to /api/edge/{site_id}/{stream}
STREAMS = ("telemetry", "heartbeat", "events", "ack", "capabilities")


def now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def example(name: str) -> dict:
    """U3 section 5's example of a message, as the contract publishes it (examples/<name>.json)."""
    return json.loads((CONTRACT_DIR / "examples" / f"{name}.json").read_text(encoding="utf-8"))


def schema(name: str) -> dict:
    return json.loads((CONTRACT_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def stamped(name: str, site_id: str, seq: int, at: int | None = None, plan_id: str = "p1") -> dict:
    """The published example of an edge message re-stamped for a site and a time: its site, its sequence, sent now, its telemetry rows and events
    in the last minutes (the cloud refuses a row from the future or before 2015), an ack of `plan_id`."""
    at = int(at if at is not None else now()) // 60 * 60
    msg = {**example(name), "site_id": site_id, "seq": seq, "sent_at": iso(at)}
    msg.pop("msg_id", None)
    if name == "telemetry":
        msg["ts_start"] = iso(at - 60 * len(msg["data"]))
    elif name == "events":
        msg["events"] = [{**e, "ts": iso(at - 60 * (len(msg["events"]) - k))} for k, e in enumerate(msg["events"])]
    elif name == "ack":
        msg.update(plan_id=plan_id, received_at=iso(at - 60), applied_at=iso(at))
    elif name == "heartbeat":
        msg.update(active_plan_id=plan_id, plan_valid_until=iso(at + 86400))
    return msg


# ------------------------------------------------------------------------------------------------ the site, its keys and its plans

def closed_loop(site_id: str) -> None:
    sites.set_tier_capable(site_id, "closed_loop")
    sites.set_tier_allowed(site_id, "closed_loop", "t", "contract suite")


def table(day: date, spans: list[tuple[int, int, str]], floor: float = 20.0, target: float = 90.0) -> list[dict]:
    """A day's Mode A slots from (start hour, end hour, mode) spans, as the compiler writes them."""
    t0 = int(datetime(day.year, day.month, day.day, tzinfo=PKT).timestamp())
    return [{"start": f"{a:02d}:00", "end": f"{b % 24:02d}:00", "start_ts": t0 + a * 3600, "end_ts": t0 + b * 3600, "mode": m,
             "soc_floor_pct": floor, "soc_target_pct": target, "max_discharge_w": 50_000 if m in ("discharge", "export") else None,
             "max_charge_w": 40_000 if m.startswith("charge") else None, "grid_charge": m == "charge_grid", "gen_charge": False, "grid_cap_w": None,
             "export_allowed": m == "export", "genset": None, "intent": m} for a, b, m in spans]


def plan_row(site_id: str, days: dict[date, list[dict]], valid_s: int = 86400, fallback: dict | None = None) -> str:
    """An active plan with the given day tables, valid for `valid_s` from now; it supersedes the active plan before it (as a tick's does)."""
    pid = store.new_id()
    t = now()
    body = {"brand": "deye", "days": [{"date": d.isoformat(), "slots": s} for d, s in days.items()],
            "fallback": fallback or {"on_expiry": "inverter_native", "on_link_loss_after_s": 300, "min_soc_floor_pct": 20.0, "max_export_w_floor": 0,
                                     "native_settings_ref": None}}
    with store.connect() as con:
        con.execute("UPDATE plans SET status = 'superseded' WHERE site_id = ? AND status = 'active'", (site_id,))
        con.execute("INSERT INTO plans (id, site_id, generated_at, belief_time, valid_from, valid_until, mode, status, summary, steps, slots) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (pid, site_id, store._now(), iso(t), iso(t), iso(t + valid_s), "a", "active",
                                                       json.dumps({"status_label": "Following"}), "[]", json.dumps(body)))
    return pid


@pytest.fixture()
def edge(world):
    """The fixture company's mill at the closed-loop tier with an edge's two keys: `post(stream, body)` sends an edge message with its
    telemetry:write key, `pull()` the signed plan message with its plans:read key."""
    sid = world.site
    with world.use():
        closed_loop(sid)
    write, read = world.key(["telemetry:write"]), world.key(["plans:read"], role="viewer")
    e = types.SimpleNamespace(world=world, sid=sid, write=write, read=read, use=world.use)
    e.post = lambda stream, body, site=None: world.anon.post(f"/api/edge/{site or sid}/{stream}", headers=write, json=body)
    e.pull = lambda: world.anon.get(f"/api/sites/{sid}/plans/current/message", headers=read)
    return e


# ------------------------------------------------------------------------------------------------ the edge's checks

class ReferenceEdge:
    """What an edge checks of a plan message before it applies it (U3 3.3, 5.4 and 5.8 item 2; R6 C1), and what it then runs minute by minute.

    A message is taken only when it is this contract's plan message by its model; signed with Ed25519 over its RFC 8785 canonical JSON by the key
    the edge was given at enrolment (`key`: {key_id, public_key}), never another site's; for its own site; with a sequence above the last message
    it took (a replay of a message, or an older one delivered late, is refused); checked against the capabilities it reported last (CAPS_STALE);
    not past its valid_until (EXPIRED); and with no day holding more slots than its slot model (SLOT_GRAMMAR). Each check answers the reason an
    ack would carry. The signature is verified here with `cryptography` and contract.canonical_json, not with delivery.verify (which also accepts the
    pre-round-7 form), so the cloud's signing is checked as an edge in another language checks it."""

    def __init__(self, site_id: str, key: dict, max_slots: int | None = None, capabilities_seq: int | None = None):
        self.site_id, self.key, self.max_slots, self.capabilities_seq = site_id, key, max_slots, capabilities_seq
        self.last_seq = 0
        self.taken: list[dict] = []

    def _signed(self, msg: dict) -> bool:
        sig = msg.get("signature") or {}
        if sig.get("alg") != "Ed25519" or sig.get("key_id") != self.key["key_id"]:
            return False
        body = {k: v for k, v in msg.items() if k != "signature"}
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(self.key["public_key"])).verify(base64.b64decode(sig["value"]),
                                                                                               contract.canonical_json(body))
        except Exception:
            return False
        return True

    def refusal(self, msg: dict, at: int | None = None) -> str | None:
        """Why the edge would refuse `msg` at `at` (now by default), else None."""
        try:
            contract.PlanMessage.model_validate(msg)
        except ValidationError:
            return "SCHEMA"
        if not self._signed(msg):
            return "SIG_INVALID"
        if msg["site_id"] != self.site_id:
            return "foreign_site"
        if msg["seq"] <= self.last_seq:
            return "replayed"
        if self.capabilities_seq is not None and (msg.get("capabilities_seq") is None or msg["capabilities_seq"] < self.capabilities_seq):
            return "CAPS_STALE"
        until = contract.epoch(msg.get("valid_until"))
        if until is not None and until <= int(at if at is not None else now()):
            return "EXPIRED"
        if self.max_slots and any(len(d["slots"]) > self.max_slots for d in msg["mode_a"]["days"]):
            return "SLOT_GRAMMAR"
        return None

    def take(self, msg: dict, at: int | None = None) -> str | None:
        """The message taken (the newest the edge runs) unless refused; the refusal, else None."""
        why = self.refusal(msg, at)
        if why is None:
            self.last_seq = msg["seq"]
            self.taken.append(msg)
        return why

    def runs(self, site: dict, ts, link_lost_at: int | None = None):
        """What the edge runs at each minute of `ts` from the newest message it took: its slots from each slot's start minute, then at its
        valid_until the fallback block (the inverter's native settings, never below the block's floor), and after a lost link the cached plan to
        its end, flagged unattended (twin.fallback_policy, the twin's executor of DLV-04). Returns (policy, state per minute)."""
        msg = self.taken[-1]
        compiled = {"days": msg["mode_a"]["days"], "fallback": msg.get("fallback")}
        return twin.fallback_policy(site, compiled, ts, valid_until=contract.epoch(msg["valid_until"]), link_lost_at=link_lost_at)
