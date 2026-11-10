"""Sites, their connections (one per DISCO meter), assets, carried state, overrides, native-settings captures and wizard drafts (plan Part 3.2).

A site's own fields, each connection's and each asset's are strict pydantic models (`SiteFields`, `ConnectionFields`, the asset models): a bad field is a 422
in one sentence. The control tier is two facts (plan Part 2.3): `tier_capable`, asserted by the data source (an edge's capabilities, or `monitor`), and
`tier_allowed`, set by an administrator; the effective tier is the lower.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, model_validator

from . import store
from .schemas import Strict, check

TIERS = ("monitor", "advisory_writes", "closed_loop")
DISCO_GROUPS = ("xwdisco", "ke")
DISCOS = ("IESCO", "LESCO", "GEPCO", "FESCO", "MEPCO", "PESCO", "HESCO", "SEPCO", "QESCO", "TESCO", "HAZECO", "K-Electric")
SITE_TYPES = ("office", "retail", "industrial_1_shift", "industrial_2_shift", "industrial_3_shift", "cold_store", "tower", "hospital", "school", "farm")
BRANDS = ("deye", "huawei", "sungrow", "growatt", "voltronic", "solis", "other")


class OutageUnit(Strict):
    kind: Literal["feeder", "transformer"] = "feeder"
    code: str = Field("", max_length=40)
    name: str = Field("", max_length=120)


class ReservePolicy(Strict):
    p_threshold: float = Field(0.2, ge=0.0, le=1.0)
    min_soc_pct: float = Field(20.0, ge=0.0, le=100.0)


DATA_SOURCE_KINDS = ("edge", "csv", "oem_cloud")


class DataSource(Strict):
    """Where the site's data comes from and whether its grid-present signal was confirmed (plan Part 9.3 tab A). `confirmed_by` and `at` are stamped by
    the server when a confirmation is saved (see _stamp_source); what a request sends for them is not kept."""
    kind: Literal[DATA_SOURCE_KINDS]
    grid_signal_confirmed: bool = False
    confirmed_by: str = Field("", max_length=120)
    at: str | None = Field(None, max_length=40)


class SiteFields(Strict):
    name: str = Field(min_length=1, max_length=120)
    address: str = Field("", max_length=300)
    city: str = Field("", max_length=80)
    lat: float = Field(31.5, ge=23.0, le=37.5)
    lon: float = Field(74.3, ge=60.5, le=77.9)
    timezone: Literal["Asia/Karachi"] = "Asia/Karachi"
    disco: Literal[DISCOS] | None = None
    site_type: Literal[SITE_TYPES] = "industrial_2_shift"
    outage_unit: OutageUnit = OutageUnit()
    genset_start_control: Literal["auto_on_grid_loss", "soc_threshold", "manual", "none"] = "none"
    outage_load_mode: Literal[0, 1] = 0
    critical_load_kw: list[float] = Field(default_factory=lambda: [0.0] * 24, min_length=24, max_length=24)
    voll_rs_per_kwh: float = Field(500.0, ge=0.0, le=10000.0)
    reserve_policy: ReservePolicy = ReservePolicy()
    cycle_cap_per_day: float = Field(1.5, ge=0.1, le=5.0)
    control_mode: Literal["advisory", "mode_a", "mode_b"] = "advisory"
    enclosure_temp_offset_c: float = Field(7.0, ge=0.0, le=25.0)
    calendar_cost_on: bool = True
    #: the battery's charge sources (plan Part 4.1, a_grid and a_gen): the grid when it is up, and the genset's output beyond the load
    grid_charge_allowed: bool = True
    gen_charge_allowed: bool = True
    notes: str = Field("", max_length=2000)
    #: the site's typical peak load (kW) from onboarding, which the stage-0 load template scales (twin.peak_load_kw); unset, it is derived from the
    #: connection or the PV
    load_peak_kw: float | None = Field(None, gt=0, le=100000)
    #: a site without any grid connection (plan Part 9.3), stored for activation and settings
    off_grid: bool = False
    data_source: DataSource | None = None
    #: the site's weekly off days and half days (plan Part 5.2's day types for the load forecast; Pakistan's public holidays, Eid and Ramadan come from
    #: pk_calendar): three-letter weekdays, Sunday off by default
    off_days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(default_factory=lambda: ["sun"], max_length=7)
    half_days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(default_factory=list, max_length=7)


def _stamp_source(data: dict, before: dict | None, by: str) -> None:
    """Who confirmed the data source's grid-present signal, and when, from the save itself: a new confirmation (or one for another kind of source) is
    stamped with the person saving and the time now; a confirmation saved before keeps its stamp; an unconfirmed source carries none."""
    src = data.get("data_source")
    if not src:
        return
    if not src.get("grid_signal_confirmed"):
        src["confirmed_by"], src["at"] = "", None
        return
    old = (before or {}).get("data_source") or {}
    if isinstance(old, dict) and old.get("grid_signal_confirmed") and old.get("kind") == src["kind"] and old.get("at"):
        src["confirmed_by"], src["at"] = str(old.get("confirmed_by") or "")[:120], str(old["at"])[:40]
    else:
        src["confirmed_by"], src["at"] = str(by or "")[:120], _now()


class IcpBaseline(Strict):
    eligible: bool = False
    #: kWh per calendar month (1-12) and period of the reference year Dec 2023 to Nov 2024, before the 2.8 percent escalation
    peak: dict[str, float] = Field(default_factory=dict)
    off_peak: dict[str, float] = Field(default_factory=dict)


class TaxFlags(Strict):
    gst_recoverable: bool = True
    s235_adjustable: bool = True
    strn: bool = True
    atl: bool = True
    province: Literal["punjab", "sindh", "kp", "balochistan", "ict"] = "punjab"


class ConnectionFields(Strict):
    meter_ref: str = Field("", max_length=40)
    disco_group: Literal[DISCO_GROUPS] = "xwdisco"
    tariff_category: str = Field("B-2(b)", max_length=20)
    regime: Literal["net_billing", "net_metering_legacy", "none", "flat", "off_grid"] = "none"
    agreement_start: str | None = None
    agreement_end: str | None = None
    sanctioned_load_kw: float = Field(400.0, gt=0, le=100000)
    export_cap_kw: float = Field(0.0, ge=0, le=100000)
    mdi_interval_min: Literal[15, 30] = 30
    icp: IcpBaseline = IcpBaseline()
    tax: TaxFlags = TaxFlags()
    seasonal_multiplier: float = Field(1.0, ge=1.0, le=1.5)
    season_months: list[int] = Field(default_factory=list)
    #: the meter's reading day of the month (from the onboarding bill): a day after it belongs to the next month's bill (tariffs.billing_period_for);
    #: unset, the bill is the calendar month. 29 to 31 read as the month's last day in a shorter month
    reading_day: int | None = Field(None, ge=1, le=31)


class PvFields(Strict):
    kwp_dc: float = Field(gt=0, le=50000)
    kw_ac: float = Field(gt=0, le=50000)
    tilt: float = Field(15.0, ge=0, le=60)
    azimuth: float = Field(180.0, ge=0, lt=360)
    gamma_pdc: float = Field(-0.0035, ge=-0.01, le=0.0)
    losses_pct: float = Field(14.0, ge=0, le=40)


class InverterFields(Strict):
    brand: Literal[BRANDS] = "deye"
    model: str = Field("", max_length=80)
    kw_ac: float = Field(gt=0, le=50000)
    firmware: str = Field("", max_length=40)


class BatteryFields(Strict):
    nameplate_kwh: float = Field(gt=0, le=100000)
    soh: float = Field(1.0, gt=0.3, le=1.0)
    p_ch_kw: float = Field(gt=0, le=50000)
    p_dis_kw: float = Field(gt=0, le=50000)
    eta_c: float = Field(0.95, gt=0.7, le=1.0)
    eta_d: float = Field(0.95, gt=0.7, le=1.0)
    soc_min: float = Field(0.10, ge=0.0, lt=0.9)
    soc_max: float = Field(0.95, gt=0.1, le=1.0)
    replacement_cost_per_kwh: float = Field(52000.0, ge=0, le=500000)
    warranted_cycles: float = Field(6000.0, ge=100, le=20000)
    warranted_depth: float = Field(0.8, gt=0.1, le=1.0)

    @model_validator(mode="after")
    def _band(self):
        if self.soc_min >= self.soc_max:
            raise ValueError("The battery's lowest state of charge must be below its highest")
        return self


class GensetFields(Strict):
    rated_kw: float = Field(gt=0, le=50000)
    f_min: float = Field(0.3, ge=0.0, le=1.0)
    fuel_kind: Literal["hsd", "gas_captive", "lng_captive"] = "hsd"
    #: HOMER's convention: fuel units per hour = b * rated_kw * on + a * output_kw (litres, or m3 / MMBtu for gas)
    a: float = Field(0.273, gt=0, le=5)
    b: float = Field(0.033, ge=0, le=1)
    c_start_rs: float = Field(1500.0, ge=0)
    c_runhr_rs: float = Field(400.0, ge=0)
    c_om_rs_per_kwh: float = Field(8.0, ge=0)
    min_up_h: float = Field(1.0, ge=0, le=24)
    min_down_h: float = Field(0.5, ge=0, le=24)
    cap_hours_per_day: float = Field(24.0, ge=0, le=24)
    tank_l: float = Field(2000.0, ge=0)
    parallel_allowed: bool = False


ASSET_MODELS = {"pv": PvFields, "inverter": InverterFields, "battery": BatteryFields, "genset": GensetFields}


def _now() -> str:
    return store._now()


# ------------------------------------------------------------------------------------------------ sites

def _site_out(r) -> dict:
    d = store.row(r)
    d["tier"] = effective_tier(d)
    return d


def effective_tier(site: dict) -> str:
    """The lower of what the source can do and what the administrator allows."""
    return TIERS[min(TIERS.index(site.get("tier_capable", "monitor")), TIERS.index(site.get("tier_allowed", "monitor")))]


def listing(include_demo: bool = True) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM sites WHERE deleted IS NULL ORDER BY name").fetchall()
    out = [_site_out(r) for r in rows]
    return out if include_demo else [s for s in out if not s.get("demo_link")]


def get(site_id: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM sites WHERE id = ? AND deleted IS NULL", (site_id,)).fetchone()
    return _site_out(r) if r else None


def create(fields: dict, by: str, site_id: str | None = None, demo_link: str | None = None) -> dict:
    data = check(SiteFields, fields).model_dump()
    _stamp_source(data, None, by)
    sid = site_id or store.new_id()
    now = _now()
    with store.connect() as con:
        con.execute("INSERT INTO sites (id, name, status, tier_capable, tier_allowed, demo_link, data, created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, data["name"], "observing", "monitor", "monitor", demo_link, json.dumps(data), now, now))
    store.audit(by, "added a site", data["name"], {"site_id": sid})
    return get(sid)


def update(site_id: str, fields: dict, by: str) -> dict:
    """The site's fields changed by `fields` (a partial set, or the form sent back whole). A stored key SiteFields does not know (a demo site's `twin`,
    which the demo writes) is carried through unvalidated and unchanged: the request may send it back as stored, never change or add it."""
    site = get(site_id)
    if site is None:
        raise KeyError(site_id)
    stored = dict(site.get("data") or {})
    carried = {k: stored.pop(k) for k in list(stored) if k not in SiteFields.model_fields}
    incoming = dict(fields)
    for k in [k for k in incoming if k in carried]:
        if incoming[k] != carried[k]:
            raise ValueError(f"The site's {k.replace('_', ' ')} is kept as stored and cannot be changed here")
        incoming.pop(k)
    data = check(SiteFields, {**stored, **incoming}).model_dump()
    _stamp_source(data, site.get("data"), by)
    data = {**data, **carried}
    with store.connect() as con:
        con.execute("UPDATE sites SET name = ?, data = ?, updated = ? WHERE id = ?", (data["name"], json.dumps(data), _now(), site_id))
    store.audit(by, "changed a site", data["name"], {"site_id": site_id, "fields": sorted(fields)})
    return get(site_id)


def set_status(site_id: str, status: str) -> None:
    with store.connect() as con:
        con.execute("UPDATE sites SET status = ?, updated = ? WHERE id = ?", (status, _now(), site_id))


def set_tier_capable(site_id: str, tier: str) -> None:
    if tier not in TIERS:
        raise ValueError(tier)
    with store.connect() as con:
        con.execute("UPDATE sites SET tier_capable = ?, updated = ? WHERE id = ?", (tier, _now(), site_id))


def set_tier_allowed(site_id: str, tier: str, by: str, signoff: str = "") -> dict:
    if tier not in TIERS:
        raise ValueError("The tier is monitor, advisory_writes or closed_loop")
    with store.connect() as con:
        con.execute("UPDATE sites SET tier_allowed = ?, updated = ? WHERE id = ?", (tier, _now(), site_id))
    store.audit(by, "changed a site's allowed control tier", site_id, {"tier": tier, "signoff": signoff})
    return get(site_id)


def delete(site_id: str, by: str) -> None:
    with store.connect() as con:
        con.execute("UPDATE sites SET deleted = ? WHERE id = ?", (_now(), site_id))
    store.audit(by, "removed a site", site_id)


# ------------------------------------------------------------------------------------------------ connections and assets

def connections(site_id: str) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT * FROM connections WHERE site_id = ? ORDER BY created", (site_id,)).fetchall()
    return [store.row(r) for r in rows]


def put_connection(site_id: str, fields: dict, by: str, conn_id: str | None = None) -> dict:
    data = check(ConnectionFields, fields).model_dump()
    cid = conn_id or store.new_id()
    now = _now()
    with store.connect() as con:
        if conn_id and con.execute("SELECT 1 FROM connections WHERE id = ? AND site_id = ?", (conn_id, site_id)).fetchone():
            con.execute("UPDATE connections SET data = ?, updated = ? WHERE id = ?", (json.dumps(data), now, conn_id))
        else:
            con.execute("INSERT INTO connections (id, site_id, data, created, updated) VALUES (?,?,?,?,?)", (cid, site_id, json.dumps(data), now, now))
    store.audit(by, "saved a connection", site_id, {"connection_id": cid, "category": data["tariff_category"]})
    return next(c for c in connections(site_id) if c["id"] == cid)


def assets(site_id: str) -> dict:
    """The site's assets by role: {'pv': {...}, 'inverter': {...}, 'battery': {...}, 'genset': {...}} (one each in v1)."""
    with store.connect() as con:
        rows = con.execute("SELECT * FROM assets WHERE site_id = ? ORDER BY created", (site_id,)).fetchall()
    out: dict = {}
    for r in rows:
        d = store.row(r)
        out[d["role"]] = {**d["data"], "id": d["id"]}
    return out


def put_asset(site_id: str, role: str, fields: dict, by: str) -> dict:
    if role not in ASSET_MODELS:
        raise ValueError(f"An asset is one of {', '.join(ASSET_MODELS)}")
    data = check(ASSET_MODELS[role], fields).model_dump()
    now = _now()
    with store.connect() as con:
        held = con.execute("SELECT id FROM assets WHERE site_id = ? AND role = ?", (site_id, role)).fetchone()
        if held:
            con.execute("UPDATE assets SET data = ?, updated = ? WHERE id = ?", (json.dumps(data), now, held["id"]))
        else:
            con.execute("INSERT INTO assets (id, site_id, role, data, created, updated) VALUES (?,?,?,?,?,?)",
                        (store.new_id(), site_id, role, json.dumps(data), now, now))
    store.audit(by, f"saved the {role}", site_id)
    return assets(site_id)[role]


def remove_asset(site_id: str, role: str, by: str) -> None:
    with store.connect() as con:
        con.execute("DELETE FROM assets WHERE site_id = ? AND role = ?", (site_id, role))
    store.audit(by, f"removed the {role}", site_id)


def full(site_id: str) -> dict | None:
    """The site with its connections, assets, state and the latest native capture: what the engine reads."""
    site = get(site_id)
    if site is None:
        return None
    return {**site, "connections": connections(site_id), "assets": assets(site_id), "state": state(site_id), "native": native_latest(site_id)}


# ------------------------------------------------------------------------------------------------ carried state (plan Part 4.8)

def state(site_id: str) -> dict:
    with store.connect() as con:
        r = con.execute("SELECT data FROM site_state WHERE site_id = ?", (site_id,)).fetchone()
    return json.loads(r["data"]) if r else {}


def put_state(site_id: str, **changes) -> dict:
    """Merges `changes` into the site's carried state. The read and the write are one write transaction (BEGIN IMMEDIATE), so two writers at once
    (a tick's last_tick, an outage event's pending re-solve, a tier sign-off) never write over each other's fields."""
    now = _now()
    with store.write_transaction() as con:
        r = con.execute("SELECT data FROM site_state WHERE site_id = ?", (site_id,)).fetchone()
        cur = json.loads(r["data"]) if r else {}
        cur.update(changes)
        con.execute("INSERT INTO site_state (site_id, data, updated) VALUES (?,?,?) ON CONFLICT(site_id) DO UPDATE SET data = excluded.data, "
                    "updated = excluded.updated", (site_id, json.dumps(cur, default=str), now))
    return cur


# ------------------------------------------------------------------------------------------------ overrides

OVERRIDE_KINDS = ("reserve", "genset_hold", "charge_block", "pause_control", "force_native", "force_charge")
OVERRIDE_PERMISSION = {"reserve": "hold_release", "genset_hold": "hold_release", "charge_block": "hold_release", "pause_control": "resume_control",
                       "force_native": "hold_release", "force_charge": "pin_genset"}


class OverrideFields(Strict):
    kind: Literal[OVERRIDE_KINDS]
    value: float | None = Field(None, ge=0, le=100)
    until: str
    reason: str = Field("", max_length=300)


def overrides(site_id: str, active_only: bool = True) -> list[dict]:
    now = _now()
    with store.connect() as con:
        if active_only:
            rows = con.execute("SELECT * FROM overrides WHERE site_id = ? AND cleared IS NULL AND until_ts > ? ORDER BY created", (site_id, now)).fetchall()
        else:
            rows = con.execute("SELECT * FROM overrides WHERE site_id = ? ORDER BY created DESC LIMIT 200", (site_id,)).fetchall()
    return [dict(r) for r in rows]


def add_override(site_id: str, fields: dict, author: str) -> dict:
    o = check(OverrideFields, fields)
    try:
        until = datetime.fromisoformat(o.until.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("The override's end time must be an ISO date and time") from None
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if until <= datetime.now(timezone.utc):
        raise ValueError("The override must end in the future")
    oid = store.new_id()
    with store.connect() as con:
        con.execute("INSERT INTO overrides (id, site_id, kind, value, from_ts, until_ts, author, reason, created) VALUES (?,?,?,?,?,?,?,?,?)",
                    (oid, site_id, o.kind, o.value, _now(), until.astimezone(timezone.utc).isoformat(timespec="seconds"), author, o.reason, _now()))
    store.audit(author, "set an override", site_id, {"kind": o.kind, "value": o.value, "until": o.until})
    return next(x for x in overrides(site_id, False) if x["id"] == oid)


def clear_override(site_id: str, oid: str, author: str) -> None:
    with store.connect() as con:
        con.execute("UPDATE overrides SET cleared = ? WHERE id = ? AND site_id = ?", (_now(), oid, site_id))
    store.audit(author, "cleared an override", site_id, {"override_id": oid})


# ------------------------------------------------------------------------------------------------ native-settings captures (immutable, plan Part 8.1)

class NativeSlot(Strict):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    mode: Literal["self_use", "hold", "charge_grid", "charge_pv_only", "discharge", "export"] = "self_use"
    soc_floor_pct: float = Field(20.0, ge=0, le=100)
    soc_target_pct: float = Field(100.0, ge=0, le=100)
    grid_charge: bool = False
    max_charge_kw: float | None = None


class NativeCapture(Strict):
    source: Literal["read_back", "photo", "entered"] = "entered"
    work_mode: Literal["self_use", "tou", "backup", "feed_in_first"] = "self_use"
    backup_soc_pct: float = Field(20.0, ge=0, le=100)
    grid_charge_allowed: bool = False
    slots: list[NativeSlot] = Field(default_factory=list, max_length=14)
    photo: str = Field("", max_length=7_000_000)


def native_latest(site_id: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM native_settings WHERE site_id = ? ORDER BY version DESC LIMIT 1", (site_id,)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["body"] = json.loads(d["body"])
    d["has_photo"] = bool(d.pop("photo"))
    return d


def native_versions(site_id: str) -> list[dict]:
    with store.connect() as con:
        rows = con.execute("SELECT site_id, version, captured_at, by, source FROM native_settings WHERE site_id = ? ORDER BY version DESC",
                           (site_id,)).fetchall()
    return [dict(r) for r in rows]


def capture_native(site_id: str, fields: dict, by: str) -> dict:
    """A new capture; never an edit of an old one (a later installer change is a new version)."""
    cap = check(NativeCapture, fields)
    if cap.photo and not cap.photo.startswith(("data:image/png;base64,", "data:image/jpeg;base64,")):
        raise ValueError("The photograph must be a PNG or JPEG image")
    body = cap.model_dump(exclude={"photo"})
    with store.connect() as con:
        v = (con.execute("SELECT MAX(version) FROM native_settings WHERE site_id = ?", (site_id,)).fetchone()[0] or 0) + 1
        con.execute("INSERT INTO native_settings (site_id, version, captured_at, by, source, body, photo) VALUES (?,?,?,?,?,?,?)",
                    (site_id, v, _now(), by, cap.source, json.dumps(body), cap.photo))
    store.audit(by, "captured the inverter's native settings", site_id, {"version": v, "source": cap.source})
    return native_latest(site_id)


# ------------------------------------------------------------------------------------------------ wizard drafts

def draft_get(person: str, draft_id: str) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM site_drafts WHERE draft_id = ? AND person = ?", (draft_id, person)).fetchone()
    return {**dict(r), "body": json.loads(r["body"])} if r else None


def draft_put(person: str, draft_id: str, body: dict) -> dict:
    if len(json.dumps(body)) > 200_000:
        raise ValueError("A draft holds at most 200 KB")
    with store.connect() as con:
        con.execute("INSERT INTO site_drafts (draft_id, site_id, person, body, updated) VALUES (?,?,?,?,?) ON CONFLICT(draft_id) DO UPDATE SET "
                    "body = excluded.body, updated = excluded.updated", (draft_id, None, person, json.dumps(body), _now()))
    return draft_get(person, draft_id)
