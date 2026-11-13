"""Set-up's check of the steps as typed (UX backend ask K6; plan C19.2 item 2, A8.2, C5.4 to C5.6, C7.4, C9.2): `POST /api/sites/{id}/validate`.

A step's section, as the form holds it, is checked by the models a save is (sites.SiteFields, the asset models, sites.ConnectionFields), over the site as
stored, and nothing is written. The answer lists every problem tied to its field and its step (`problems`), what the draft says against itself
(`setup_warnings`, sites.setup_warnings over the site as it would be), and the figures the server works out from it (`derived`): the battery's nominal and
usable kWh, the genset's usable kW and its fuel an hour at half and full load (Estimated), the energy held at the reserve floor and the minutes of the
critical load it carries, and the battery's wear cost per kWh (for see_money_settings only). The web app shows them as typed; it no longer works them
out (the server is the authority, [req] 0.19)."""
from __future__ import annotations

from pydantic import Field, ValidationError

from . import optimise, plans, schemas, sites
from .schemas import Strict

#: The Set-up step each of the site's own fields is on (plan C2.2: A site and data source, B equipment, C tariff and connection, D control limits);
#: a field not listed is step A's
SITE_STEPS = {"genset_start_control": "b", "genset_start_soc_pct": "b", "enclosure_temp_offset_c": "b", "off_grid": "c",
              "control_mode": "d", "reserve_policy": "d", "cycle_cap_per_day": "d", "critical_load_kw": "d", "grid_charge_allowed": "d",
              "gen_charge_allowed": "d", "outage_load_mode": "d", "voll_rs_per_kwh": "d", "calendar_cost_on": "d"}
ASSET_ROLES = ("pv", "inverter", "battery", "genset")
#: What a save's body carries beside the fields, which the check takes and ignores, so the page sends the save's own body: the saved connection's id
#: (which names the connection checked), the optimistic check's values and a new connection's key
SAVE_ONLY = ("id", "if_unchanged", "idempotency_key")
#: A share the form types as a percent ("Percent entry, fraction stored", as the tariff editor's: UX K32 check), in the form's words where its range or
#: its type is wrong: the save's own refusal says the stored fraction's bounds
PERCENT_WORDS = {("battery", "soh"): "The state of health is a percent above 30, up to 100",
                 ("battery", "eta_c"): "The charge efficiency is a percent above 70, up to 100",
                 ("battery", "eta_d"): "The discharge efficiency is a percent above 70, up to 100",
                 ("battery", "soc_min"): "The lowest state of charge is a percent from 0 to under 90",
                 ("battery", "soc_max"): "The highest state of charge is a percent above 10, up to 100",
                 ("battery", "warranted_depth"): "The warranted depth is a percent above 10, up to 100",
                 ("genset", "f_min"): "The minimum loading is a percent from 0 to 100",
                 ("pv", "gamma_pdc"): "The temperature coefficient is a percent per °C from -1 to 0",
                 ("site", "p_threshold"): "The outage chance that holds the reserve is a percent from 0 to 100"}
_RANGE = {"greater_than", "greater_than_equal", "less_than", "less_than_equal", "float_type", "float_parsing"}
#: The load a genset's fuel an hour is given at, as shares of its rating (plan C5.5: "At half load about 68 L/h; at full load about 122 L/h")
HALF, FULL = 0.5, 1.0
#: The site's fields the reserve's figures read (reserve_figures): a problem in one of them holds the figures back; a problem elsewhere does not
RESERVE_FIELDS = ("critical_load_kw", "reserve_policy")


class SetupAssetsIn(Strict):
    """Step B's equipment: each asset whole, as its save sends it; null for one unticked; left out, as stored."""
    pv: dict | None = None
    inverter: dict | None = None
    battery: dict | None = None
    genset: dict | None = None

    def sent(self) -> dict:
        return {role: getattr(self, role) for role in ASSET_ROLES if role in self.model_fields_set}


class SetupCheckIn(Strict):
    """A step's section as the form holds it, or several (Review sends none, and reads the site as stored): the site's own fields (A, the genset's
    start in B, the off-grid card in C, D's limits), merged over the stored ones; each asset of B whole as its save sends it, null for one unticked;
    the connection of C as `PUT /sites/:id/connection` sends it (its `id` names a saved one, else it is a new one)."""
    site: dict | None = Field(None, title="The site's fields")
    assets: SetupAssetsIn | None = Field(None, title="The equipment")
    connection: dict | None = Field(None, title="The grid connection")


class UnknownConnection(LookupError):
    """The section's connection id names no connection of the site."""


def _problem(step: str, path: tuple, message: str) -> dict:
    return {"step": step, "field": ".".join(str(p) for p in path), "path": list(path), "message": message}


def _section(model, fields: dict, prefix: tuple, step_of, words_of: str | None = None) -> tuple[dict | None, list[dict]]:
    """The section as the model stores it, else None and every problem, each on its field (`prefix` + the model's path) and its step."""
    try:
        return model.model_validate(fields).model_dump(), []
    except ValidationError as exc:
        out = []
        for loc, sentence, kind in schemas.problems(exc, model):
            leaf = next((p for p in reversed(loc) if isinstance(p, str)), None)
            words = PERCENT_WORDS.get((words_of, leaf)) if kind in _RANGE and words_of else None
            out.append(_problem(step_of(loc), prefix + loc, words or sentence))
        return None, out


def _without(fields: dict) -> dict:
    return {k: v for k, v in fields.items() if k not in SAVE_ONLY}


def _site_section(stored: dict, sent: dict | None) -> tuple[dict | None, list[dict]]:
    """The site's fields as the save would store them: the sent ones over the stored ones, and every problem. A stored key the model does not know (a
    demo site's `twin`) sent back unchanged is no field of the form, as sites.update takes it. With problems, the site as it would be is the sent
    fields that have none over the stored ones (a field with a problem counts as stored, as a section with problems does for the warnings), so the
    warnings and the reserve read the fields typed beside an empty name (UX K6 check); None where even that cannot be read."""
    known = {k: v for k, v in stored.items() if k in sites.SiteFields.model_fields}
    sent = {k: v for k, v in _without(sent or {}).items() if k in sites.SiteFields.model_fields or stored.get(k) != v}
    step_of = lambda loc: SITE_STEPS.get(str(loc[0]), "a") if loc else "a"  # noqa: E731
    data, found = _section(sites.SiteFields, {**known, **sent}, ("site",), step_of, "site")
    if data is None:
        bad = {p["path"][1] for p in found if len(p["path"]) > 1}
        if bad and all(p["path"][1:2] for p in found):
            data, _ = _section(sites.SiteFields, {**known, **{k: v for k, v in sent.items() if k not in bad}}, ("site",), step_of)
    return data, found


def battery_figures(b: dict, money_settings: bool) -> dict:
    """The battery's nominal kWh (its nameplate: Ah x V / 1,000 in the Ah form) and the usable kWh at its window (nominal x state of health x (highest
    minus lowest state of charge); plan A8.4), with its entry form; the wear cost per kWh discharged (optimise.wear_cost, the price the plan charges)
    for see_money_settings only."""
    nominal = float(b["nameplate_kwh"])
    out = {"nominal_kwh": round(nominal, 2), "usable_kwh": round(nominal * float(b["soh"]) * (float(b["soc_max"]) - float(b["soc_min"])), 2),
           "entry_unit": b.get("entry_unit", "kwh"), "capacity_ah": b.get("capacity_ah"), "nominal_v": b.get("nominal_v"),
           "soh_pct": round(float(b["soh"]) * 100, 1), "soc_min_pct": round(float(b["soc_min"]) * 100, 1), "soc_max_pct": round(float(b["soc_max"]) * 100, 1)}
    if money_settings:
        out["wear_cost_rs_per_kwh"] = round(optimise.wear_cost(b), 2)
    return out


def genset_figures(g: dict) -> dict:
    """The genset's usable kW (its rating: kVA x the power factor in the kVA form; plan A8.3) and its fuel an hour at half and full load by its curve
    (b x rating + a x output: litres of diesel, MMBtu of gas), Estimated: the curve is typical until it is refitted from the genset's own runs."""
    kw, a, b = float(g["rated_kw"]), float(g["a"]), float(g["b"])
    unit = sites.genset_fuel_unit(g)
    return {"usable_kw": round(kw, 2), "rating_entry": g.get("rating_entry", "kw"), "rated_kva": g.get("rated_kva"), "power_factor": g.get("power_factor"),
            "fuel_kind": g.get("fuel_kind", "hsd"), "fuel_unit": unit,
            "fuel_per_hour": {"half_load": round(b * kw + a * kw * HALF, 2), "full_load": round(b * kw + a * kw * FULL, 2), "unit": f"{unit}/h",
                              "estimated": True}}


def reserve_figures(data: dict, b: dict, inverter: dict | None) -> dict:
    """What the reserve floor holds for an outage (plan C7.4): the floor the plan keeps with the grid up (plans.grid_floor, the plan's own: the reserve
    floor, never under the battery's cut-off plus 2 points or 10 %, and never above its highest state of charge), the energy the battery gives the load
    between it and the battery's own cut-off (where an outage takes it to, after its discharge losses), and the minutes of the critical load's highest
    hour that carries; the power the battery gives (its discharge power, or the inverter's AC size where that is smaller)."""
    floor = plans.grid_floor({"data": data, "assets": {"battery": b}})
    low = float(b["soc_min"])
    held = max(0.0, floor - low) * float(b["nameplate_kwh"]) * float(b.get("soh", 1.0)) * float(b.get("eta_d", 0.95))
    crit = max([float(x) for x in data.get("critical_load_kw") or [] if isinstance(x, (int, float))] or [0.0])
    gives = min(float(b["p_dis_kw"]), float(inverter["kw_ac"])) if inverter else float(b["p_dis_kw"])
    return {"floor_pct": round(floor * 100, 1), "floor_set_pct": float((data.get("reserve_policy") or {}).get("min_soc_pct", 20.0)),
            "battery_low_pct": round(low * 100, 1),
            "held_kwh": round(held, 2), "critical_kw": crit, "minutes": round(held / crit * 60.0, 1) if crit > 0 else None, "battery_kw": round(gives, 2)}


def run(s: dict, site_in: dict | None, assets_in: dict | None, connection_in: dict | None, money_settings: bool,
        refused: list[dict] | None = None) -> dict:
    """The check of `s` (full() of the site) with the sections as typed. `refused` are the problems the reader's role would meet on saving them (a
    change the save refuses with 403, each on its field: routes_sites._validate_refusals). Raises UnknownConnection for a connection id the site does
    not have."""
    problems: list[dict] = list(refused or [])
    data, found = _site_section(s.get("data") or {}, site_in)
    problems += found
    site_read = data is not None
    data = data if site_read else {k: v for k, v in (s.get("data") or {}).items()}
    held = {r: {k: v for k, v in a.items() if k != "id"} for r, a in (s.get("assets") or {}).items()}
    assets, failed = dict(held), {}
    for role, fields in (assets_in or {}).items():
        if fields is None:
            assets.pop(role, None)                       # unticked: the site as it would be without it
            continue
        got, found = _section(sites.ASSET_MODELS[role], _without(fields), ("assets", role), lambda loc: "b", role)
        problems += found
        if got is None:
            failed[role] = found[0]["message"]
        else:
            assets[role] = got
    conns = list(s.get("connections") or [])
    if connection_in is not None:
        cid = connection_in.get("id")
        if cid and not any(c["id"] == cid for c in conns):
            raise UnknownConnection(cid)
        got, found = _section(sites.ConnectionFields, sites._from_disco(_without(connection_in), data.get("disco")), ("connection",), lambda loc: "c")
        if got is not None and sites.group_problem(got["disco_group"], data.get("disco")):
            found, got = [_problem("c", ("connection", "disco_group"), sites.group_problem(got["disco_group"], data.get("disco")))], None
        problems += found
        if got is not None:
            mine = {"id": cid, "data": got}
            conns = [mine if c["id"] == cid else c for c in conns] if cid else conns + [mine]
    site = {**s, "data": data, "assets": assets, "connections": conns}
    derived, not_derived = {"battery": None, "genset": None, "reserve": None}, {}
    bat = assets.get("battery") if "battery" not in failed else None
    if "battery" in failed:
        not_derived["battery"] = not_derived["reserve"] = failed["battery"]
    elif bat:
        derived["battery"] = battery_figures(bat, money_settings)
        # the reserve reads the critical load and the reserve policy: only a problem in one of them holds it back (any, where the site as it would be
        # cannot be read at all); the inverter is the site's as it would be, the stored one where the typed one has a problem, as for the warnings
        held_back = [p["message"] for p in problems if p["path"][0] == "site" and (not site_read or p["path"][1:2] and p["path"][1] in RESERVE_FIELDS)]
        if held_back:
            not_derived["reserve"] = held_back[0]
        else:
            derived["reserve"] = reserve_figures(data, bat, assets.get("inverter"))
    if "genset" in failed:
        not_derived["genset"] = failed["genset"]
    elif assets.get("genset"):
        derived["genset"] = genset_figures(assets["genset"])
    order = {"a": 0, "b": 1, "c": 2, "d": 3}
    problems.sort(key=lambda p: order[p["step"]])
    return {"ok": not problems, "problems": problems, "setup_warnings": sites.setup_warnings(site), "derived": derived, "not_derived": not_derived}
