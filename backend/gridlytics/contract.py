"""The ems-edge/1.0 contract (plan DAT-02, U3 section 5): the messages an edge sends the cloud and the plan message the cloud signs for it, as strict
pydantic models. They are the one validator: the HTTPS edge routes (routes_edge) check every message with them, plans.signed_message checks the plan
message before it signs it, and the JSON Schema files in contracts/ems-edge/1.0/ are generated from them (`schema_files`, written by
scripts/export_contract.py; a test fails when a file drifts from the models).

Both forms of a message validate: U3's (its envelope msg_type, msg_id, site_id and edge_id; its event names with a per-event source; the partial ack
with per_device; the heartbeat states executing and fallback; telemetry as data with ts_start and interval_s) and the shorter one the twin sends (rows
with a ts column, items, active_plan). Under 1.x semantic versioning an event type the cloud does not know is stored and named in the answer, never a
reason to refuse the batch. The deviations from U3 are listed in contracts/ems-edge/1.0/README.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT = "ems-edge/1.0"
#: The largest sequence number taken: a JSON number above 2**53 is no longer exact everywhere
SEQ_MAX = 2 ** 53 - 1
TIERS = ("monitor", "advisory_writes", "closed_loop")

#: U3 1.2 telemetry points the cloud does not keep (R6 adds inverter_clock_offset_s): a batch may carry them (U3 5.2: an edge's columns are any subset
#: of 1.2's keys); they are skipped and named in the answer (`ignored_columns`)
POINTS_NOT_STORED = ("grid_present_min", "grid_power_l1_w", "grid_power_l2_w", "grid_power_l3_w", "pv_energy_wh", "load_power_backup_w",
                     "battery_trust_score", "battery_soc_source", "battery_capacity_wh_usable", "battery_charge_limit_a", "battery_discharge_limit_a",
                     "battery_cell_v_min", "battery_cell_v_max", "battery_cell_t_max", "inverter_state", "inverter_alarms", "inverter_rated_power_w",
                     "genset_tank_pct", "genset_runtime_h", "genset_starts", "inverter_clock_offset_s", "clock_offset_s", "template_id", "firmware")

#: The event types of this contract
EVENT_TYPES = ("outage_start", "outage_end", "genset_start", "genset_stop", "plan_received", "plan_rejected", "plan_applied", "plan_expired",
               "fallback_engaged", "fallback_cleared", "readback_mismatch", "write_failed", "budget_exhausted", "local_override", "template_change",
               "firmware_change", "control_disabled", "control_enabled", "reboot", "link_lost", "clock_unsynced", "battery_trust_low",
               "physics_check_failed", "data_gap", "installer_session")
#: U3 5.5's names for the same events: both are taken, stored as sent, and acted on alike (`event_meaning`)
EVENT_ALIASES = {"override_local": "local_override", "template_change_detected": "template_change", "firmware_change_detected": "firmware_change",
                 "edge_reboot": "reboot", "installer_session_start": "installer_session", "installer_session_end": "installer_session"}
KNOWN_EVENTS = (*EVENT_TYPES, *EVENT_ALIASES)
#: U3 5.6's reason codes (and R6's DEVICE_BUSY and NATIVE_REF_STALE)
REASON_CODES = ("SIG_INVALID", "CAPS_STALE", "EXPIRED", "SCHEMA", "OFF_WHITELIST", "NOT_SUPPORTED", "RANGE_CLAMPED", "STEP_ROUNDED", "BUDGET_EXHAUSTED",
                "UNLOCK_FAILED", "DEVICE_OFFLINE", "READBACK_MISMATCH", "ATOMIC_GROUP_FAILED", "CONTROL_DISABLED", "OVERRIDE_ACTIVE", "RATE_LIMITED",
                "SLOT_GRAMMAR", "TIMEZONE_MISMATCH", "DEVICE_BUSY", "NATIVE_REF_STALE")
#: The heartbeat's control states: this contract's and U3 5.7's
CONTROL_STATES = ("following_plan", "executing", "native", "fallback", "hold", "released", "disabled")
#: The heartbeat states in which the edge runs the cloud's plan
FOLLOWING_STATES = ("following_plan", "executing")


def event_meaning(kind: str) -> str:
    """One name per event: U3's name read as this contract's (override_local is local_override)."""
    return EVENT_ALIASES.get(kind, kind)


def epoch(text: str | None) -> int | None:
    """An ISO 8601 time in epoch seconds (UTC unless it says otherwise); None when it does not read."""
    if not text:
        return None
    try:
        t = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
        return int((t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


Name = Annotated[str, Field(min_length=1, max_length=64)]
Time = Annotated[str, Field(max_length=40)]
Cell = bool | int | float | Annotated[str, Field(max_length=64)] | None


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(_Strict):
    """The common envelope (U3 5.1). msg_type, msg_id, site_id and edge_id are optional; a site_id that is not the key's site is refused (403)."""
    contract: Literal["ems-edge/1.0"] = CONTRACT
    msg_id: Name | None = None
    site_id: Name | None = None
    edge_id: Name | None = None
    seq: int = Field(ge=0, le=SEQ_MAX)
    sent_at: Time


class TelemetryBatch(Envelope):
    """One-minute rows (U3 5.2). Either `rows` with a ts column, or U3's `data` with ts_start and interval_s (and `rows` as its count)."""
    msg_type: Literal["telemetry"] | None = None
    columns: list[Annotated[str, Field(max_length=60)]] = Field(min_length=1, max_length=64)
    rows: Annotated[list[list[Cell]], Field(max_length=1440)] | Annotated[int, Field(ge=0, le=1440)] | None = None
    data: Annotated[list[list[Cell]], Field(max_length=1440)] | None = None
    ts_start: Time | None = None
    interval_s: int | None = Field(None, ge=1, le=86400)
    aggregation: dict[Annotated[str, Field(max_length=60)], Annotated[str, Field(max_length=20)]] | None = Field(None, max_length=64)
    sample_period_s: float | None = Field(None, gt=0, le=3600)
    quality: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self):
        if self.data is not None and isinstance(self.rows, list):
            raise ValueError("rows: send the rows as rows or as data, not both")
        if self.data is None and not isinstance(self.rows, list):
            raise ValueError("rows: the batch carries its rows (or U3's data)")
        if self.data is not None and isinstance(self.rows, int) and self.rows != len(self.data):
            raise ValueError(f"rows: {self.rows} rows announced, {len(self.data)} in data")
        if "ts" not in self.columns:
            if self.ts_start is None or self.interval_s is None:
                raise ValueError("columns: the batch needs a ts column, or ts_start and interval_s")
            if epoch(self.ts_start) is None:
                raise ValueError("ts_start: not an ISO 8601 time")
        return self

    def table(self) -> tuple[list[str], list[list]]:
        """The columns and rows with a ts column: U3's rows are stamped ts_start + n * interval_s."""
        rows = self.data if self.data is not None else self.rows
        if "ts" in self.columns:
            return list(self.columns), [list(r) for r in rows]
        t0 = epoch(self.ts_start)
        return ["ts", *self.columns], [[t0 + n * self.interval_s, *r] for n, r in enumerate(rows)]


class Heartbeat(Envelope):
    """Every 60 s (U3 5.7). active_plan_id and plan_valid_until are U3's names for active_plan and valid_until."""
    msg_type: Literal["heartbeat"] | None = None
    uptime_s: int = Field(ge=0, le=10 ** 10)
    active_plan: Annotated[str, Field(max_length=120)] | None = None
    active_plan_id: Annotated[str, Field(max_length=120)] | None = None
    valid_until: Time | None = None
    plan_valid_until: Time | None = None
    control_state: Literal[CONTROL_STATES] = "native"
    queue_depth: int = Field(0, ge=0, le=10 ** 9)
    clock_offset_s: float = Field(0.0, ge=-1e9, le=1e9, allow_inf_nan=False)


class Event(_Strict):
    ts: Time
    type: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", json_schema_extra={"examples": list(KNOWN_EVENTS)},
                      description="One of the known event types (examples); an unknown one is stored and named in the answer")
    source: Name | None = None
    detail: dict = Field(default_factory=dict)


class EventBatch(Envelope):
    msg_type: Literal["events"] | None = None
    events: list[Event] = Field(max_length=500)

    def unknown_types(self) -> list[str]:
        return sorted({e.type for e in self.events if e.type not in KNOWN_EVENTS})


class AckItem(_Strict):
    device: Annotated[str, Field(max_length=60)]
    primitive: Annotated[str, Field(max_length=60)]
    written: float | str | None = None
    read_back: float | str | None = None
    status: Literal["ok", "clamped", "failed"]
    codes: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=10)


class AckApplied(_Strict):
    primitive: Annotated[str, Field(max_length=60)]
    requested: Any = None
    requested_hash: Annotated[str, Field(max_length=100)] | None = None
    written: Any = None
    readback: Any = None
    readback_ok: bool | None = None


class AckRejected(_Strict):
    primitive: Annotated[str, Field(max_length=60)]
    reason: Annotated[str, Field(max_length=40)]
    detail: Annotated[str, Field(max_length=300)] | None = None


class AckDevice(_Strict):
    device_id: Name
    status: Literal["ok", "applied", "partial", "rejected", "failed"]
    writes_used_today: dict[Annotated[str, Field(max_length=60)], int] = Field(default_factory=dict, max_length=20)
    applied: list[AckApplied] = Field(default_factory=list, max_length=50)
    rejected: list[AckRejected] = Field(default_factory=list, max_length=50)


class Ack(Envelope):
    """After each plan or command (U3 5.6): `items` (this contract's) or `per_device` (U3's), and a reason code for a rejected plan."""
    msg_type: Literal["ack"] | None = None
    plan_id: Annotated[str, Field(max_length=120)]
    status: Literal["received", "applied", "partial", "rejected", "expired"]
    reason: Annotated[str, Field(max_length=40)] | None = None
    detail: Annotated[str, Field(max_length=300)] | None = None
    received_at: Time | None = None
    applied_at: Time | None = None
    items: list[AckItem] = Field(default_factory=list, max_length=200)
    per_device: list[AckDevice] = Field(default_factory=list, max_length=20)


def ack_outcome(body: dict) -> tuple[str, list[str]]:
    """What an ack (as stored) says of its plan: 'rejected', 'expired', 'partial' (a part not written or clamped) or 'ok', and the reason codes it
    gives."""
    codes = [body["reason"]] if body.get("reason") else []
    partial = body.get("status") == "partial"
    for it in body.get("items") or []:
        codes += list(it.get("codes") or [])
        partial |= it.get("status") in ("clamped", "failed")
    for d in body.get("per_device") or []:
        codes += [r.get("reason") for r in d.get("rejected") or [] if r.get("reason")]
        partial |= d.get("status") in ("partial", "rejected", "failed") or bool(d.get("rejected"))
    status = body.get("status")
    outcome = status if status in ("rejected", "expired") else "partial" if partial else "ok"
    return outcome, list(dict.fromkeys(codes))


class Primitive(_Strict):
    """A writable primitive of a device (U3 5.3): its values or range and step, its persistence class and its daily write budget."""
    values: list[Annotated[str, Field(max_length=40)]] | None = Field(None, max_length=20)
    min: float | None = None
    max: float | None = None
    step: float | None = Field(None, gt=0)
    persistence: Annotated[str, Field(max_length=20)] | None = None
    writes_per_day_max: int | None = Field(None, ge=0, le=10000)


class SlotModel(_Strict):
    mode_a_supported: bool = True
    max_slots: int | None = Field(None, ge=1, le=48)
    ring: bool | None = None
    fields: list[Annotated[str, Field(max_length=40)]] = Field(default_factory=list, max_length=20)
    time_resolution_min: int | None = Field(None, ge=1, le=60)
    min_slot_minutes: int | None = Field(None, ge=1, le=1440)
    weekday_mask: bool | None = None
    table_writes_per_day_max: int | None = Field(None, ge=0, le=1000)


class ModeB(_Strict):
    supported: bool
    reason: Annotated[str, Field(max_length=300)] | None = None


class Device(_Strict):
    """A device behind the edge (U3 5.3). `primitives` names what the cloud may write on it; a device that names none is read only."""
    device_id: Name
    role: Annotated[str, Field(max_length=40)]
    family: Annotated[str, Field(max_length=80)] | None = None
    template_id: Annotated[str, Field(max_length=80)] | None = None
    firmware: Annotated[str, Field(max_length=80)] | None = None
    rated_power_w: float | None = Field(None, ge=0)
    capacity_wh_nameplate: float | None = Field(None, ge=0)
    points: list[Annotated[str, Field(max_length=60)]] = Field(default_factory=list, max_length=80)
    primitives: dict[Annotated[str, Field(max_length=60)], Primitive] = Field(default_factory=dict, max_length=40)
    slot_model: SlotModel | None = None
    mode_b: ModeB | None = None
    native_settings_ref: Annotated[str, Field(max_length=80)] | None = None
    shunt_fitted: bool | None = None
    estimator: dict | None = None
    control: Annotated[str, Field(max_length=40)] | None = None


class EdgeInfo(_Strict):
    sw_version: Annotated[str, Field(max_length=40)]
    hw: Annotated[str, Field(max_length=60)] | None = None
    contracts_accepted: list[Annotated[str, Field(max_length=20)]] = Field(default_factory=list, max_length=10)
    clock_synced: bool = True
    control_tier: Literal[TIERS] | None = None
    control_enabled_by: Annotated[str, Field(max_length=80)] | None = None
    control_enabled_at: Time | None = None


#: The roles of a device whose writable primitives a plan targets
INVERTER_ROLES = ("hybrid_inverter", "inverter", "battery_inverter")


class Capabilities(Envelope):
    """On boot, on a template change and at least daily (U3 5.3). The tier is edge-asserted: `tier`, or U3's edge.control_tier."""
    msg_type: Literal["capabilities"] | None = None
    edge_version: Annotated[str, Field(max_length=40)] | None = None
    edge: EdgeInfo | None = None
    clock_synced: bool = True
    tier: Literal[TIERS] | None = None
    devices: list[Device] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _edge(self):
        if self.edge_version is None and self.edge is None:
            raise ValueError("edge_version: the edge's software version (or U3's edge block) is required")
        if self.tier and self.edge and self.edge.control_tier and self.tier != self.edge.control_tier:
            raise ValueError("tier: tier and edge.control_tier disagree")
        return self

    @property
    def control_tier(self) -> str:
        return self.tier or (self.edge.control_tier if self.edge else None) or "monitor"


# ------------------------------------------------------------------------------------------------ the plan message (cloud to edge, signed)

class PlanSlot(BaseModel):
    """A Mode A slot as the compiler writes it (delivery.compile_mode_a); further fields the compiler adds are carried."""
    model_config = ConfigDict(extra="allow")
    start: Time
    end: Time
    start_ts: int
    end_ts: int
    mode: Annotated[str, Field(max_length=40)]
    soc_floor_pct: float | None = None
    soc_target_pct: float | None = None
    max_discharge_w: float | None = None
    max_charge_w: float | None = None
    grid_charge: bool = False
    export_allowed: bool = False


class PlanDay(_Strict):
    date: Annotated[str, Field(max_length=10)]
    slots: list[PlanSlot] = Field(max_length=96)


class ModeA(_Strict):
    days: list[PlanDay] = Field(max_length=4)


class Target(_Strict):
    device_id: Name


class Signature(_Strict):
    alg: Literal["Ed25519"]
    key_id: Name
    value: Annotated[str, Field(max_length=200)]


class PlanMessage(_Strict):
    """The signed plan message an edge pulls (GET /api/sites/{id}/plans/current/message): a plan, or a release while control is paused or released
    to native or on a native-block day. U3 5.4's envelope fields are at the top level beside the body's (this contract signs the whole message but
    `signature`, not a nested body: README)."""
    contract: Literal["ems-edge/1.0"]
    msg_type: Literal["plan"]
    msg_id: Name
    type: Literal["plan", "release"]
    plan_id: Annotated[str, Field(max_length=120)] | None
    supersedes: Annotated[str, Field(max_length=120)] | None = None
    site_id: Name
    edge_id: Name | None = None
    seq: int = Field(ge=1, le=SEQ_MAX)
    sent_at: Time
    issued_at: Time
    valid_from: Time
    valid_until: Time | None
    timezone: Literal["Asia/Karachi"]
    capabilities_seq: int | None = None
    targets: list[Target] = Field(default_factory=list, max_length=20)
    mode: Literal["a", "b"]
    fallback: dict | None = None
    mode_a: ModeA
    mode_b: dict | None = None
    notes: dict = Field(default_factory=dict)
    release: dict | None = None
    capabilities_fit: dict | None = None
    clock: dict | None = None
    signature: Signature | None = None


#: The schema files of contracts/ems-edge/1.0/, by file name
MODELS = {"telemetry": TelemetryBatch, "heartbeat": Heartbeat, "events": EventBatch, "ack": Ack, "capabilities": Capabilities, "plan": PlanMessage}


def schema_files() -> dict[str, str]:
    """Each message's JSON Schema (2020-12) as the text of its file in contracts/ems-edge/1.0/ (`<name>.schema.json`)."""
    out = {}
    for name, model in MODELS.items():
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"ems-edge/1.0/{name}.schema.json",
                  **model.model_json_schema()}
        out[f"{name}.schema.json"] = json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return out
