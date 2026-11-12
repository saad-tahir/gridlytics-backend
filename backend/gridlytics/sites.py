"""Sites, their connections (one per DISCO meter), assets, carried state, overrides, native-settings captures and wizard drafts (plan Part 3.2).

A site's own fields, each connection's and each asset's are strict pydantic models (`SiteFields`, `ConnectionFields`, the asset models): a bad field is a 422
in one sentence. The control tier is two facts (plan Part 2.3): `tier_capable`, asserted by the data source (an edge's capabilities;
advisory writes for a CSV or cloud source, SOURCE_CAPABLE; else `monitor`), and
`tier_allowed`, set by an administrator; the effective tier is the lower.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from . import store, tariff_data, telemetry
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
    #: the battery state of charge (%) at which a soc_threshold controller starts the genset (audit flow-onboarding-11: never asked, so the plan assumed
    #: the reserve floor plus 5 points whatever the controller did); None: that assumption (genset_start_soc), which the web app marks Preliminary
    genset_start_soc_pct: float | None = Field(None, gt=0, le=100)
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


def genset_start_soc(data: dict) -> float:
    """The state of charge (a share) at which the site's soc_threshold controller starts the genset, as the plan (plans.build_problem) and the twin
    (twin.plant_of) model it: the site's own threshold, else the reserve floor plus 5 points."""
    own = (data or {}).get("genset_start_soc_pct")
    if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0:
        return float(own) / 100.0
    return float(((data or {}).get("reserve_policy") or {}).get("min_soc_pct", 20.0)) / 100.0 + 0.05


#: The highest tier a source that is not an edge device supports by itself (plan Part 2.3, 7.5 and 7.6; audit flow-onboarding-03: a CSV or cloud site
#: stayed at monitor for good, although plans reach it as slot tables a person keys in). An edge device's own capabilities message sets its tier.
SOURCE_CAPABLE = {"csv": "advisory_writes", "oem_cloud": "advisory_writes"}


def _source_capable(con, site_id: str, data: dict, before: dict | None, held: str = "monitor") -> str | None:
    """`tier_capable` as the saved data source sets it, or None to keep the stored one (`held`). A CSV or cloud source chosen (on a new site, or in place
    of another kind): SOURCE_CAPABLE; kept by a later save (a note, the name): never lowered, only raised to SOURCE_CAPABLE (a site saved at monitor
    before this rule), so what the site's edge asserted meanwhile stays until the kind is changed. A change to an edge device from another kind: what
    that site's edge last asserted (its latest capabilities message), else monitor until it reports. No source (a site added before sources were
    recorded, the demo's), or an edge kept: unchanged."""
    kind = (data.get("data_source") or {}).get("kind")
    old = ((before or {}).get("data_source") or {})
    old_kind = old.get("kind") if isinstance(old, dict) else None
    if kind in SOURCE_CAPABLE:
        if kind == old_kind and held in TIERS and TIERS.index(held) > TIERS.index(SOURCE_CAPABLE[kind]):
            return None
        return SOURCE_CAPABLE[kind]
    if kind == "edge" and old_kind in SOURCE_CAPABLE:
        r = con.execute("SELECT body FROM capabilities WHERE site_id = ? ORDER BY seq DESC LIMIT 1", (site_id,)).fetchone()
        tier = (json.loads(r["body"]) or {}).get("tier") if r else None
        return tier if tier in TIERS else "monitor"
    return None


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


#: The largest ICP baseline a month and period may hold (kWh): a 100 MW sanctioned load (ConnectionFields' bound) for a whole month is 74,400,000
ICP_BASELINE_MAX = 100_000_000


class IcpBaseline(Strict):
    eligible: bool = False
    #: kWh per calendar month (1-12) and period of the reference year Dec 2023 to Nov 2024, before the 2.8 percent escalation
    peak: dict[str, float] = Field(default_factory=dict)
    off_peak: dict[str, float] = Field(default_factory=dict)

    @field_validator("peak", "off_peak")
    @classmethod
    def _months(cls, v: dict[str, float]) -> dict[str, float]:
        """Keyed by calendar month, '1' to '12' (tariffs.icp_baseline reads str(month); audit engine-tariffs-05: any key and any figure were stored), each
        from 0 kWh up."""
        out: dict[str, float] = {}
        for k, kwh in v.items():
            month = int(k) if str(k).strip().isdigit() else 0
            if not 1 <= month <= 12:
                raise ValueError("The ICP baseline is by calendar month, 1 to 12")
            if not 0 <= kwh <= ICP_BASELINE_MAX:
                raise ValueError(f"An ICP baseline is from 0 to {ICP_BASELINE_MAX:,} kWh")
            out[str(month)] = kwh
        return out


class TaxFlags(Strict):
    gst_recoverable: bool = True
    s235_adjustable: bool = True
    strn: bool = True
    atl: bool = True
    province: Literal["punjab", "sindh", "kp", "balochistan", "ict"] = "punjab"

    @model_validator(mode="after")
    def _registered(self):
        """Input GST is claimed back only under a sales-tax registration (plan Part 6.1; audit engine-tariffs-05: {strn: false} alone kept
        gst_recoverable true, so a no-STRN site's price left its GST out). Without an STRN the GST is borne; saying it is recovered is refused."""
        if not self.strn and self.gst_recoverable:
            if "gst_recoverable" in self.model_fields_set:
                raise ValueError("A connection without a sales-tax registration (STRN) cannot recover the bill's GST")
            self.gst_recoverable = False
        return self


#: The province of each DISCO's customers, for the electricity duty (R9 sections 7 and 8.4), the usual one first: K-Electric serves Karachi (Sindh) and
#: Hub and Lasbela (Balochistan); IESCO serves Islamabad (ICT) and the Rawalpindi circle (Punjab)
DISCO_PROVINCES = {"IESCO": ("ict", "punjab"), "LESCO": ("punjab",), "GEPCO": ("punjab",), "FESCO": ("punjab",), "MEPCO": ("punjab",), "PESCO": ("kp",),
                   "HESCO": ("sindh",), "SEPCO": ("sindh",), "QESCO": ("balochistan",), "TESCO": ("kp",), "HAZECO": ("kp",),
                   "K-Electric": ("sindh", "balochistan")}


def group_for_disco(disco: str | None) -> str:
    """The time-of-use calendar a DISCO's customers are billed on (R9 section 2): K-Electric's own, or the one of the other DISCOs."""
    return "ke" if disco == "K-Electric" else "xwdisco"


def _from_disco(fields, disco: str | None):
    """A connection's DISCO group and province, where the save leaves them out, from the site's DISCO (audit flow-onboarding-02: a K-Electric site took
    the other DISCOs' peak windows and Punjab's duty by default). A site with no DISCO keeps ConnectionFields' defaults."""
    if not disco or disco not in DISCO_PROVINCES or not isinstance(fields, dict):
        return fields
    out = dict(fields)
    out.setdefault("disco_group", group_for_disco(disco))
    tax = out.get("tax")
    if tax is None:
        out["tax"] = {"province": DISCO_PROVINCES[disco][0]}
    elif isinstance(tax, dict) and "province" not in tax:
        out["tax"] = {**tax, "province": DISCO_PROVINCES[disco][0]}
    return out


def group_problem(group: str | None, disco: str | None) -> str | None:
    """The sentence when a connection's DISCO group contradicts the site's DISCO (the DISCO group is not printed on the bill, so it follows the DISCO),
    else None; a site with no DISCO may use either."""
    if not disco or group == group_for_disco(disco):
        return None
    if disco == "K-Electric":
        return "This site's DISCO is K-Electric: its connection uses K-Electric's time-of-use windows"
    return f"This site's DISCO is {disco}: its connection uses the other DISCOs' time-of-use windows, not K-Electric's"


class ConnectionFields(Strict):
    meter_ref: str = Field("", max_length=40)
    disco_group: Literal[DISCO_GROUPS] = "xwdisco"
    tariff_category: str = Field("B-2(b)", max_length=20)
    regime: Literal["net_billing", "net_metering_legacy", "none", "flat", "off_grid"] = "none"
    #: the net-metering or net-billing agreement's first and last day, YYYY-MM-DD (audit flow-onboarding-18: any text was stored, and an end before the start)
    agreement_start: str | None = Field(None, max_length=10)
    agreement_end: str | None = Field(None, max_length=10)
    sanctioned_load_kw: float = Field(400.0, gt=0, le=100000)
    export_cap_kw: float = Field(0.0, ge=0, le=100000)
    mdi_interval_min: Literal[15, 30] = 30
    icp: IcpBaseline = IcpBaseline()
    tax: TaxFlags = TaxFlags()
    seasonal_multiplier: float = Field(1.0, ge=1.0, le=1.5)
    #: Tariff F's season (R9 1.2: seasonal industrial consumers, at least six months), calendar months 1 to 12; empty: not on Tariff F
    season_months: list[int] = Field(default_factory=list)
    #: the meter's reading day of the month (from the onboarding bill): a day after it belongs to the next month's bill (tariffs.billing_period_for);
    #: unset, the bill is the calendar month. 29 to 31 read as the month's last day in a shorter month
    reading_day: int | None = Field(None, ge=1, le=31)
    #: the site's inverter, battery and genset are on this connection, and the site's meter readings (its telemetry's grid import and export) are
    #: this meter's (round 7 stage 2, audit tracker-and-docs-01): the plan, the savings ledger and the bill rebuild price this connection. A site has
    #: one such connection: the first saved, unless another is marked (marking one unmarks the others). A save that leaves it out keeps it as stored
    plant: bool = False

    @field_validator("agreement_start", "agreement_end", mode="before")
    @classmethod
    def _agreement_day(cls, v, info):
        """A calendar day written YYYY-MM-DD, or blank (None; an empty text is blank too)."""
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        which = "start" if info.field_name == "agreement_start" else "end"
        day = v.strip() if isinstance(v, str) else ""
        try:
            if len(day) != 10 or day[4] != "-" or day[7] != "-":
                raise ValueError
            date.fromisoformat(day)
        except ValueError:
            raise ValueError(f"The agreement {which} is a date (YYYY-MM-DD)") from None
        return day

    @model_validator(mode="after")
    def _agreement_order(self):
        if self.agreement_start and self.agreement_end and self.agreement_end < self.agreement_start:
            raise ValueError("The agreement must end after it starts")
        return self

    @field_validator("season_months")
    @classmethod
    def _season(cls, v: list[int]) -> list[int]:
        """Tariff F's season (audit engine-tariffs-05: any numbers were stored): distinct calendar months, six at least, as the tariff bills."""
        if any(not 1 <= m <= 12 for m in v):
            raise ValueError("A season month is 1 to 12")
        if len(set(v)) != len(v):
            raise ValueError("Each season month is listed once")
        if v and len(v) < 6:
            raise ValueError("Tariff F's season runs at least six months")
        return v

    @model_validator(mode="after")
    def _tariff_f(self):
        if self.season_months and tariff_data.category_class(self.tariff_category) != "industrial":
            raise ValueError("Tariff F (a season) is for industrial connections")
        return self

    @field_validator("tariff_category")
    @classmethod
    def _category(cls, v: str) -> str:
        """A category the rate cards know (audit flow-onboarding-07): an empty one was stored, and the site then never planned and raised no alert."""
        if not v:
            raise ValueError("Choose the tariff category printed on the bill")
        if v not in tariff_data.CARDS_279:
            raise ValueError(f"Unknown tariff category {v}")
        return v


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


#: The upper bound of the genset's money figures and tank, with the sentence a larger one is refused in (the web app's site-form.ts has the same bounds)
GENSET_UPPER = {"c_start_rs": (1_000_000, "The cost per start is at most Rs 1,000,000"),
                "c_runhr_rs": (1_000_000, "The cost per running hour is at most Rs 1,000,000"),
                "c_om_rs_per_kwh": (10_000, "The upkeep per kWh is at most Rs 10,000"),
                "tank_l": (1_000_000, "The tank holds at most 1,000,000 L")}


def _num(x: float) -> str:
    """A figure in a sentence: thousands separated, at most one decimal ("5,000", "266.4")."""
    return f"{x:,.1f}".rstrip("0").rstrip(".")


#: Each fuel's typical curve and tank, taken where a save leaves them out (audit flow-onboarding-10: a captive gas engine kept the diesel curve, 0.273 litres
#: a kWh read as MMBtu, so its fuel cost came out about 35 times too high and the plan never ran it). Diesel is in litres; captive gas and RLNG are in
#: MMBtu, the unit their price is set in (tariff_data's fuel prices), with the demo's gas engine's curve (0.0085 MMBtu a kWh, about 40 % efficient, and
#: 0.0010 MMBtu an hour per rated kW at no load). Both gases are piped: no tank, which the plan reads as no fuel limit.
FUEL_CURVES = {"hsd": {"a": 0.273, "b": 0.033, "tank_l": 2000.0},
               "gas_captive": {"a": 0.0085, "b": 0.0010, "tank_l": 0.0},
               "lng_captive": {"a": 0.0085, "b": 0.0010, "tank_l": 0.0}}
GAS_FUELS = ("gas_captive", "lng_captive")
#: A curve in the other fuel's unit: a diesel slope under 0.1 L a kWh would burn under one kWh of diesel for a kWh out, and a gas slope over 0.05 MMBtu
#: a kWh (under 7 % efficient) or a no-load term over 0.01 MMBtu an hour per rated kW is a diesel curve in litres
HSD_A_MIN, GAS_A_MAX, GAS_B_MAX = 0.1, 0.05, 0.01


class GensetFields(Strict):
    rated_kw: float = Field(gt=0, le=50000)
    f_min: float = Field(0.3, ge=0.0, le=1.0)
    fuel_kind: Literal["hsd", "gas_captive", "lng_captive"] = "hsd"
    #: HOMER's convention: fuel units per hour = b * rated_kw * on + a * output_kw (litres of diesel, or MMBtu of captive gas or RLNG: FUEL_CURVES)
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
    #: the nameplate's rating in kVA and the power factor it is rated at (audit flow-onboarding-16: sets are sold and labelled in kVA, so a 250 kVA set
    #: typed as 250 kW was overstated by a quarter): the plan counts on rated_kw, which is rated_kva x power_factor (taken from them when left out).
    #: None: a rating given in kW only, as before
    rated_kva: float | None = Field(None, gt=0, le=62500)
    power_factor: float = Field(0.8, ge=0.5, le=1.0)

    @field_validator("c_start_rs", "c_runhr_rs", "c_om_rs_per_kwh", "tank_l")
    @classmethod
    def _upper(cls, v: float, info) -> float:
        """The money figures and the tank are bounded, as the other fields are (audit flow-onboarding-18: Rs 1e12 a start and a 1e12 L tank were
        stored)."""
        hi, words = GENSET_UPPER[info.field_name]
        if v > hi:
            raise ValueError(words)
        return v

    @model_validator(mode="before")
    @classmethod
    def _fuel_defaults(cls, v):
        """The fuel's own curve and tank where the save leaves them out (FUEL_CURVES), not diesel's whatever the fuel."""
        if isinstance(v, dict) and v.get("fuel_kind") in FUEL_CURVES:
            v = {**{k: x for k, x in FUEL_CURVES[v["fuel_kind"]].items() if k not in v}, **v}
        return v

    @model_validator(mode="before")
    @classmethod
    def _kw_from_kva(cls, v):
        """A rating given in kVA alone: its kW at the power factor (0.8 where it is left out)."""
        if isinstance(v, dict) and "rated_kw" not in v and isinstance(v.get("rated_kva"), (int, float)) and not isinstance(v.get("rated_kva"), bool):
            pf = v.get("power_factor", 0.8)
            if isinstance(pf, (int, float)) and not isinstance(pf, bool):
                v = {**v, "rated_kw": round(float(v["rated_kva"]) * float(pf), 3)}
        return v

    @model_validator(mode="after")
    def _kva_agrees(self):
        """The kW the plan counts on is the nameplate's kVA times its power factor."""
        if self.rated_kva is not None:
            kw = self.rated_kva * self.power_factor
            if abs(self.rated_kw - kw) > max(0.05, 0.001 * kw):
                raise ValueError(f"A genset's rating in kW is its kVA times its power factor: {_num(self.rated_kva)} kVA at {self.power_factor:g} is "
                                 f"{_num(kw)} kW")
        return self

    @model_validator(mode="after")
    def _fuel_units(self):
        """A curve in the fuel's own unit, and no tank on piped gas."""
        if self.fuel_kind in GAS_FUELS:
            if self.a > GAS_A_MAX:
                raise ValueError(f"A gas genset's fuel curve is in MMBtu: its slope (a) is at most {GAS_A_MAX} MMBtu per kWh")
            if self.b > GAS_B_MAX:
                raise ValueError(f"A gas genset's fuel at no load (b) is at most {GAS_B_MAX} MMBtu an hour per rated kW")
            if self.tank_l > 0:
                raise ValueError("A genset on piped gas has no tank: its tank is 0 L")
        elif self.a < HSD_A_MIN:
            raise ValueError(f"A diesel genset's fuel curve is in litres: its slope (a) is at least {HSD_A_MIN} L per kWh")
        return self


ASSET_MODELS = {"pv": PvFields, "inverter": InverterFields, "battery": BatteryFields, "genset": GensetFields}


def _now() -> str:
    return store._now()


# ------------------------------------------------------------------------------------------------ saves based on older values (audit flow-onboarding-05)

#: A save that carries no `if_unchanged`: written without the check, as before
UNCHECKED = object()

ASSET_NAMES = {"pv": "PV array", "inverter": "inverter", "battery": "battery", "genset": "genset"}
_FIELD_WORDS = {"plant": "plant connection", "lat": "latitude", "lon": "longitude", "load_peak_kw": "typical peak load", "sanctioned_load_kw": "sanctioned load",
                "export_cap_kw": "export cap", "mdi_interval_min": "MDI interval", "kwp_dc": "DC size", "kw_ac": "AC size", "gamma_pdc": "temperature coefficient",
                "losses_pct": "losses", "nameplate_kwh": "nameplate energy", "soh": "state of health", "p_ch_kw": "charge power", "p_dis_kw": "discharge power",
                "eta_c": "charge efficiency", "eta_d": "discharge efficiency", "soc_min": "lowest state of charge", "soc_max": "highest state of charge",
                "rated_kw": "rating", "f_min": "minimum loading", "tank_l": "tank", "min_up_h": "minimum run", "min_down_h": "minimum rest",
                "rated_kva": "rating in kVA", "power_factor": "power factor",
                "genset_start_control": "genset start method", "genset_start_soc_pct": "genset start threshold"}


class Changed(Exception):
    """A save based on stored values that were changed since: the route answers 409 `site_changed` with this sentence and the fields (`changed`)."""

    def __init__(self, sentence: str, changed: list[str]):
        super().__init__(sentence)
        self.changed = changed


def _listing(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def check_unchanged(held: dict | None, expect, what: str) -> None:
    """The optimistic check of the site editors: `expect` is the save's `if_unchanged`, the stored values its form was based on as the client last read
    them (an object of fields), or None when it read none stored. When they are no longer what is held, someone changed them since (in Site settings, or
    another person), and the save raises Changed rather than writing over the newer values. UNCHECKED (left out of the save) checks nothing."""
    if expect is UNCHECKED:
        return
    if expect is not None and not isinstance(expect, dict):
        raise ValueError("if_unchanged is the set of stored values the form was based on")
    again = "load the current values, or save yours over them"
    if expect is None:
        if held is not None:
            raise Changed(f"{what} was added after this form was loaded: {again}", [])
        return
    if held is None:
        raise Changed(f"{what} was removed after this form was loaded: {again}", [])
    changed = sorted(k for k, v in expect.items() if held.get(k) != v)
    if changed:
        words = [_FIELD_WORDS.get(k, k.replace("_", " ")) for k in changed]
        raise Changed(f"{what}'s {_listing(words)} changed after this form was loaded: {again}", changed)


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
        capable = _source_capable(con, sid, data, None) or "monitor"
        con.execute("INSERT INTO sites (id, name, status, tier_capable, tier_allowed, demo_link, data, created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, data["name"], "observing", capable, "monitor", demo_link, json.dumps(data), now, now))
    store.audit(by, "added a site", data["name"], {"site_id": sid})
    return get(sid)


def update(site_id: str, fields: dict, by: str, if_unchanged=UNCHECKED) -> dict:
    """The site's fields changed by `fields` (a partial set, or the form sent back whole). A stored key SiteFields does not know (a demo site's `twin`,
    which the demo writes) is carried through unvalidated and unchanged: the request may send it back as stored, never change or add it.
    `if_unchanged` (check_unchanged) is compared with the stored fields in the same write transaction as the write, so no save lands in between."""
    with store.write_transaction() as con:
        r = con.execute("SELECT * FROM sites WHERE id = ? AND deleted IS NULL", (site_id,)).fetchone()
        if r is None:
            raise KeyError(site_id)
        before = store.row(r).get("data") or {}
        check_unchanged(before, if_unchanged, "The site")
        stored = dict(before)
        carried = {k: stored.pop(k) for k in list(stored) if k not in SiteFields.model_fields}
        incoming = dict(fields)
        for k in [k for k in incoming if k in carried]:
            if incoming[k] != carried[k]:
                raise ValueError(f"The site's {k.replace('_', ' ')} is kept as stored and cannot be changed here")
            incoming.pop(k)
        data = check(SiteFields, {**stored, **incoming}).model_dump()
        _stamp_source(data, before, by)
        data = {**data, **carried}
        capable = _source_capable(con, site_id, data, before, r["tier_capable"]) or r["tier_capable"]
        con.execute("UPDATE sites SET name = ?, data = ?, tier_capable = ?, updated = ? WHERE id = ?",
                    (data["name"], json.dumps(data), capable, _now(), site_id))
        store.audit(by, "changed a site", data["name"], {"site_id": site_id, "fields": sorted(fields)}, con=con)
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
    """Remove a site: its row is marked removed and kept, for the records that name it (its reports keep its name, `names`). What acts on it goes
    with it (round 7, audit flow-reports-alerts-members-keys-14): its API keys are revoked (a key is bound to one site, and a removed site's key stayed
    active, its requests answered 404) and its open alerts are resolved (nothing is left to act on them; they stayed in the inbox and its badge). The
    audit line names the site and the keys revoked."""
    from . import alerts, apikeys  # imported here: both import this module, or what imports it
    with store.connect() as con:
        r = con.execute("SELECT name FROM sites WHERE id = ? AND deleted IS NULL", (site_id,)).fetchone()
        con.execute("UPDATE sites SET deleted = ? WHERE id = ? AND deleted IS NULL", (_now(), site_id))
    if r is None:
        return
    revoked = apikeys.revoke_site(site_id, by)
    alerts.site_removed(site_id)
    store.audit(by, "removed a site", r["name"], {"site_id": site_id, "keys_revoked": [k["name"] for k in revoked]})


def names() -> dict[str, dict]:
    """Every site's name by id, a removed site's too, each {"name", "removed"}: for the records that outlive a site (a report names the site it was
    made for, marked removed; round 7, audit flow-reports-alerts-members-keys-14)."""
    with store.connect() as con:
        rows = con.execute("SELECT id, name, deleted FROM sites").fetchall()
    return {r["id"]: {"name": r["name"], "removed": r["deleted"] is not None} for r in rows}


# ------------------------------------------------------------------------------------------------ connections and assets

def connections(site_id: str) -> list[dict]:
    """The site's connections, the plant's first (round 7 stage 2, audit tracker-and-docs-01): the one marked `plant`, else the first saved, then the
    rest as saved. Every engine path that reads a site's connection reads the first (the plan, the ledger, the bill rebuild, the twin, the alerts and
    the reports), which is the meter the site's readings are and the connection the plant's flows reach; a second connection was stored, shown with
    its own prices and then ignored while the first was priced in its place. Each row's `plant` (beside `data`) says which one that is."""
    with store.connect() as con:
        rows = con.execute("SELECT * FROM connections WHERE site_id = ? ORDER BY CASE WHEN json_extract(data, '$.plant') = 1 THEN 0 ELSE 1 END, "
                           "created, rowid", (site_id,)).fetchall()
    out = [store.row(r) for r in rows]
    for i, c in enumerate(out):
        c["plant"] = i == 0
    return out


def plant_connection(site: dict | None) -> dict | None:
    """The connection the site's plant and meter readings are on (`connections`' first), or None for a site without one."""
    return ((site or {}).get("connections") or [None])[0]


def put_connection(site_id: str, fields: dict, by: str, conn_id: str | None = None, if_unchanged=UNCHECKED) -> dict:
    """Saves the connection `conn_id` (or a new one). `if_unchanged` is checked against that connection's stored fields (none stored: None) in the
    write's own transaction. The DISCO group and the province follow the site's DISCO where the save leaves them out, and a group that contradicts the
    DISCO is refused (audit flow-onboarding-02)."""
    cid = conn_id or store.new_id()
    now = _now()
    with store.write_transaction() as con:
        site = con.execute("SELECT data FROM sites WHERE id = ?", (site_id,)).fetchone()
        disco = (json.loads(site["data"]) or {}).get("disco") if site else None
        data = check(ConnectionFields, _from_disco(fields, disco)).model_dump()
        problem = group_problem(data["disco_group"], disco)
        if problem:
            raise ValueError(problem)
        held = con.execute("SELECT data FROM connections WHERE id = ? AND site_id = ?", (conn_id, site_id)).fetchone() if conn_id else None
        check_unchanged(json.loads(held["data"]) if held else None, if_unchanged, "The grid connection")
        if held and not (isinstance(fields, dict) and "plant" in fields):
            data["plant"] = bool((json.loads(held["data"]) or {}).get("plant", False))  # a save that leaves it out keeps it
        if data["plant"]:
            # one connection carries the plant: marking this one unmarks the site's others
            con.execute("UPDATE connections SET data = json_set(data, '$.plant', json('false')), updated = ? WHERE site_id = ? AND id != ? "
                        "AND json_extract(data, '$.plant') = 1", (now, site_id, cid))
        if held:
            con.execute("UPDATE connections SET data = ?, updated = ? WHERE id = ?", (json.dumps(data), now, conn_id))
        else:
            con.execute("INSERT INTO connections (id, site_id, data, created, updated) VALUES (?,?,?,?,?)", (cid, site_id, json.dumps(data), now, now))
        store.audit(by, "saved a connection", site_id, {"connection_id": cid, "category": data["tariff_category"]}, con=con)
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


def put_asset(site_id: str, role: str, fields: dict, by: str, if_unchanged=UNCHECKED) -> dict:
    """Saves the site's asset of this role. `if_unchanged` is checked against the stored asset's fields (none stored: None) in the write's own
    transaction."""
    if role not in ASSET_MODELS:
        raise ValueError(f"An asset is one of {', '.join(ASSET_MODELS)}")
    data = check(ASSET_MODELS[role], fields).model_dump()
    now = _now()
    with store.write_transaction() as con:
        held = con.execute("SELECT id, data FROM assets WHERE site_id = ? AND role = ?", (site_id, role)).fetchone()
        check_unchanged(json.loads(held["data"]) if held else None, if_unchanged, f"The {ASSET_NAMES[role]}")
        if held:
            con.execute("UPDATE assets SET data = ?, updated = ? WHERE id = ?", (json.dumps(data), now, held["id"]))
        else:
            con.execute("INSERT INTO assets (id, site_id, role, data, created, updated) VALUES (?,?,?,?,?,?)",
                        (store.new_id(), site_id, role, json.dumps(data), now, now))
        store.audit(by, f"saved the {role}", site_id, con=con)
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


# ------------------------------------------------------------------------------------------------ what the setup says against itself

#: A PV AC size above this many times its DC size is a typing slip worth a word: the array never gives the AC side more than its DC
PV_AC_DC_MAX = 1.5


def setup_warnings(site: dict) -> list[str]:
    """Sentences on what the site's equipment, connections and critical load say against each other, and on a missing feeder code, for every site
    answer (audit flow-onboarding-09: a 5 kW inverter took a 5,000 kW battery, PV 900 kW AC from 10 kWp DC and an export cap far above the sanctioned
    load with no word; flow-onboarding-17: a blank feeder code was saved with no word). They do not refuse a save: a site may have PV on inverters of
    its own, or a battery rated above its inverter, and the plan keeps PV and battery power within the inverter's kW AC whatever is typed. `site` is
    full() of a site."""
    out: list[str] = []
    kw = lambda x: f"{_num(float(x))} kW"  # noqa: E731
    a, d = site.get("assets") or {}, site.get("data") or {}
    pv, bat = a.get("pv") or {}, a.get("battery") or {}
    inv = float((a.get("inverter") or {}).get("kw_ac") or 0) or None
    if inv and pv.get("kw_ac", 0) > inv:
        out.append(f"The PV's AC size ({kw(pv['kw_ac'])}) is above the inverter's ({kw(inv)}): the plan passes no more than {kw(inv)} of PV and battery "
                   "power through it")
    if pv.get("kwp_dc") and pv.get("kw_ac", 0) > PV_AC_DC_MAX * pv["kwp_dc"]:
        out.append(f"The PV's AC size ({kw(pv['kw_ac'])}) is more than one and a half times its DC size ({_num(pv['kwp_dc'])} kWp): check both")
    for key, name in (("p_ch_kw", "charge power"), ("p_dis_kw", "discharge power")):
        if inv and bat.get(key, 0) > inv:
            out.append(f"The battery's {name} ({kw(bat[key])}) is above the inverter's AC size ({kw(inv)}): the plan keeps it within {kw(inv)}")
    conns = [c.get("data") or {} for c in site.get("connections") or []]
    for c in conns:
        cap = float(c.get("export_cap_kw") or 0)
        if inv and cap > inv:
            out.append(f"The export cap ({kw(cap)}) is above the inverter's AC size ({kw(inv)}): the site cannot export more than the inverter passes")
        if c.get("sanctioned_load_kw") and cap > c["sanctioned_load_kw"]:
            out.append(f"The export cap ({kw(cap)}) is above the sanctioned load ({kw(c['sanctioned_load_kw'])}): a net-metering or net-billing agreement "
                       "allows no more than the sanctioned load")
        # the site's DISCO changed after the connection was saved (group_problem; activation asks for it, an active site is told here): it is priced on
        # the other calendar's peak windows until the connection is saved again (audit flow-onboarding-02)
        if c.get("regime") != "off_grid" and group_problem(c.get("disco_group"), d.get("disco")):
            ke = group_for_disco(d.get("disco")) == "ke"
            held, wanted = ("the other DISCOs'", "K-Electric's") if ke else ("K-Electric's", "the other DISCOs'")
            out.append(f"The grid connection is priced on {held} time-of-use windows, but this site's DISCO is {d['disco']}: save the connection again "
                       f"with {wanted} (tab C, or Site settings Tariff)")
    crit = max([float(x) for x in d.get("critical_load_kw") or [] if isinstance(x, (int, float))] or [0.0])
    if crit > 0 and bat.get("p_dis_kw"):
        by_inverter = bool(inv) and inv < bat["p_dis_kw"]
        if crit > (inv if by_inverter else bat["p_dis_kw"]):
            what = f"the battery can give through the inverter ({kw(inv)})" if by_inverter else f"the battery's discharge power ({kw(bat['p_dis_kw'])})"
            out.append(f"The critical load ({kw(crit)} at most) is more than {what}: it cannot carry it alone in an outage")
    if crit > 0 and d.get("load_peak_kw") and crit > d["load_peak_kw"]:
        out.append(f"The critical load ({kw(crit)} at most) is above the typical peak load ({kw(d['load_peak_kw'])}): check both")
    # the outage model pools the company's sites on one feeder or transformer code (forecast: sites.outage_unit, "the key the outage model pools on")
    on_grid = not d.get("off_grid") and (any(c.get("regime") != "off_grid" for c in conns) if conns else bool(d.get("disco")))
    if on_grid and not str((d.get("outage_unit") or {}).get("code") or "").strip():
        out.append("No feeder or transformer code is given: the outage forecast learns from this site's own outages only, not from the company's other "
                   "sites on its feeder")
    # Mode B is set-points an edge device writes (plan Part 7.2): a CSV or cloud source has no writer, so its plans arrive as slot tables (audit
    # flow-onboarding-22: chosen for a CSV site without a word)
    if d.get("control_mode") == "mode_b" and (d.get("data_source") or {}).get("kind") in SOURCE_CAPABLE:
        out.append(MODE_B_WITHOUT_EDGE)
    return list(dict.fromkeys(out))


MODE_B_WITHOUT_EDGE = ("Mode B (set-points written by the edge device) needs an edge device: with a CSV or cloud source the plan reaches the inverter as "
                       "slot tables a person keys in, so choose Mode A or advisory")


# ------------------------------------------------------------------------------------------------ the data source's test (audit flow-onboarding-22)

#: The points a source test looks for in the data received (plan 9.3 tab A and R8 2.2, "Test connection": the rows received and the detected points),
#: grid presence first: without it outages cannot be seen and the reserve cannot be planned
SOURCE_POINTS = ("grid_present", "grid_voltage_v", "grid_power_w", "pv_power_w", "load_power_w", "battery_power_w", "battery_soc_bms_pct",
                 "genset_running", "inverter_mode_active")
GRID_POINTS = ("grid_present", "grid_voltage_v")
#: The grid points in the sentence's words (the points themselves are listed in `points`)
GRID_POINT_WORDS = {"grid_present": "the grid-present signal", "grid_voltage_v": "the grid voltage"}
#: How far back from the last minute received the test reads: a day of minutes
SOURCE_TEST_S = 86400
SOURCE_NO_DATA = "No data has arrived from this site yet: test the source again once its first readings are in"
SOURCE_NO_GRID = ("The data received carries no grid presence (grid_present or the grid voltage): outages cannot be seen and the reserve cannot be "
                  "planned. Add the point to the source, or choose a source that reports it")


def _iso(ts) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def source_test(site_id: str) -> dict:
    """What the site's data source has sent (plan 9.3 tab A: a connection test that refuses a source without a grid-present signal; until then the
    wizard had only the person's tick): the minutes received in the day up to the last one, the points among SOURCE_POINTS that carry a value, and
    `grid_signal`: "found" (grid_present or the grid voltage is among them), "missing" (data arrived without either) or "no_data" (nothing yet: a site
    is set up before its data flows, so this is no refusal). Times are ISO, UTC."""
    kind = (((get(site_id) or {}).get("data") or {}).get("data_source") or {}).get("kind")
    last = telemetry.latest(site_id)
    if not last:
        return {"kind": kind, "grid_signal": "no_data", "rows": 0, "first_at": None, "last_at": None, "points": [], "sentence": SOURCE_NO_DATA}
    got = telemetry.raw(site_id, int(last["ts"]) - SOURCE_TEST_S + 60, int(last["ts"]) + 60, columns=SOURCE_POINTS)
    ts = got["ts"]

    def carried(name: str) -> bool:
        return any(v not in (None, "") for v in got[name]) if name == "inverter_mode_active" else any(not math.isnan(v) for v in got[name])

    points = [p for p in SOURCE_POINTS if carried(p)]
    grid = [p for p in GRID_POINTS if p in points]
    return {"kind": kind, "grid_signal": "found" if grid else "missing", "rows": len(ts), "first_at": _iso(ts[0]), "last_at": _iso(ts[-1]),
            "points": points,
            "sentence": f"Grid presence is in the data received ({' and '.join(GRID_POINT_WORDS[p] for p in grid)}), so outages can be seen" if grid
            else SOURCE_NO_GRID}


def grid_connected(site: dict) -> bool:
    """Whether the site (full()) draws on a grid it can lose: not off-grid, with a connection of another regime than off_grid."""
    d = site.get("data") or {}
    return not d.get("off_grid") and any((c.get("data") or {}).get("regime") != "off_grid" for c in site.get("connections") or [])


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

#: A slot's start: a time of day from 00:00 to 23:59 (audit flow-onboarding-14: '99:99' was stored, and the replay read it as a minute that never comes)
SLOT_START = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
#: The most slots any brand's table holds (Huawei's fourteen periods, delivery.BRANDS); a site's own inverter may hold fewer (routes_sites.native_post)
NATIVE_MAX_SLOTS = 14


class NativeSlot(Strict):
    start: str
    mode: Literal["self_use", "hold", "charge_grid", "charge_pv_only", "discharge", "export"] = "self_use"
    soc_floor_pct: float = Field(20.0, ge=0, le=100)
    soc_target_pct: float = Field(100.0, ge=0, le=100)
    grid_charge: bool = False
    max_charge_kw: float | None = None

    @field_validator("start")
    @classmethod
    def _start(cls, v: str) -> str:
        if not SLOT_START.match(v):
            raise ValueError("Each slot starts at a time from 00:00 to 23:59")
        return v


class NativeCapture(Strict):
    source: Literal["read_back", "photo", "entered"] = "entered"
    work_mode: Literal["self_use", "tou", "backup", "feed_in_first"] = "self_use"
    backup_soc_pct: float = Field(20.0, ge=0, le=100)
    grid_charge_allowed: bool = False
    slots: list[NativeSlot] = Field(default_factory=list)
    photo: str = Field("", max_length=7_000_000)

    @field_validator("slots", mode="before")
    @classmethod
    def _count(cls, v):
        if isinstance(v, list) and len(v) > NATIVE_MAX_SLOTS:
            raise ValueError(f"An inverter's table holds at most {NATIVE_MAX_SLOTS} slots")
        return v

    @model_validator(mode="after")
    def _distinct(self):
        starts = [s.start for s in self.slots]
        if len(set(starts)) < len(starts):
            raise ValueError("Two slots cannot start at the same time")
        return self


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
