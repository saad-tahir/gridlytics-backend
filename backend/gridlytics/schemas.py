"""Refusals and the strict request models' plain-sentence errors (the shape lifted from solarlytics-backend schemas.py lines 27-98).

Every request model of Gridlytics is strict: types enforced, unknown fields refused, and a refusal is one plain sentence in `detail` (plan Part 10.2).
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError


class Refusal(HTTPException):
    """A refusal with a machine-readable code beside its message: the answer is {detail, code, request_id}."""

    def __init__(self, status_code: int, detail, code: str | None = None, headers: dict | None = None, extra: dict | None = None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.extra = extra or {}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


_WORDS = {"string_type": "{label} must be text", "string_too_long": "{label} must be at most {max_length} characters", "missing": "{label} is missing",
          "int_type": "{label} must be a whole number", "int_parsing": "{label} must be a whole number", "int_from_float": "{label} must be a whole number",
          "float_type": "{label} must be a number", "float_parsing": "{label} must be a number", "bool_type": "{label} must be yes or no (true or false)",
          "bool_parsing": "{label} must be yes or no (true or false)", "list_type": "{label} must be a list",
          "string_pattern_mismatch": "{label} is not in the form expected", "dict_type": "{label} must be a set of named values",
          "model_attributes_type": "{label} must be a set of named values", "model_type": "{label} must be a set of named values",
          "greater_than_equal": "{label} must be at least {ge}", "less_than_equal": "{label} must be at most {le}",
          "greater_than": "{label} must be more than {gt}", "less_than": "{label} must be less than {lt}", "literal_error": "{label} must be one of {expected}",
          "enum": "{label} must be one of {expected}"}


def _label(loc: tuple, titles: dict | None = None, items: dict | None = None) -> str:
    """The field a refusal names: "The cycle cap per day" from its key, or its own words where the model gives a top-level field a title (`Field(title=
    "The meter reading (hm³)")`), so a field whose key is a unit or a code reads as people say it (UX K24 check). A field inside a list item is named
    by the item where the model names its list's items (`Field(json_schema_extra={"item": "version"})`): "Version 2's kind" (UX K32 check)."""
    item = (items or {}).get(loc[0])
    if item and len(loc) > 1 and isinstance(loc[1], int):
        head, rest = f"{item[:1].upper()}{item[1:]} {loc[1] + 1}", loc[2:]
    else:
        head, rest = (titles or {}).get(loc[0]) or "The " + str(loc[0]).replace("_", " "), loc[1:]
    return head + "".join(f"'s {str(p).replace('_', ' ')}" for p in rest if not isinstance(p, int))


def _clause(text: str) -> str:
    """A refusal's clause after the first, as it reads inside the one sentence: its first letter lower-cased when its first word is a plain word ("Give",
    "The", "A"), never an abbreviation or a name with a mark ("DISCO", "MMBtu", "Punjab's"). So a form missing two fields is told "Give your full name;
    give a work email", one sentence (UX K3 check)."""
    word = text.split(" ", 1)[0]
    plain = word.isascii() and word.isalpha() and word[:1].isupper() and (word[1:].islower() if len(word) > 1 else word != "I")
    return text[:1].lower() + text[1:] if plain else text


def message(exc: ValidationError, model: type[BaseModel] | None = None) -> str:
    fields = model.model_fields if model is not None else {}
    titles = {k: f.title for k, f in fields.items() if f.title}
    items = {k: f.json_schema_extra["item"] for k, f in fields.items() if isinstance(f.json_schema_extra, dict) and f.json_schema_extra.get("item")}
    parts = []
    for e in exc.errors()[:4]:
        loc = tuple(e["loc"])
        text = e["msg"].removeprefix("Value error, ")
        label = _label(loc, titles, items) if loc else ""
        if not loc or e["type"] == "value_error":
            parts.append(text)
        elif e["type"] == "extra_forbidden":
            parts.append(f"Unknown field '{'.'.join(str(p) for p in loc)}'")
        elif e["type"] == "string_too_short":
            n = (e.get("ctx") or {}).get("min_length", 1)
            parts.append(f"{label} is needed" if n <= 1 else f"{label} must be at least {n} characters")
        elif e["type"] in ("too_long", "too_short"):
            # a list's length (UX K32 check): pydantic's own words are "List should have at most 7 items after validation, not 8"
            ctx = e.get("ctx") or {}
            n = ctx.get("max_length") if e["type"] == "too_long" else ctx.get("min_length")
            parts.append(f"{label} must have {'at most' if e['type'] == 'too_long' else 'at least'} {n} item{'' if n == 1 else 's'}")
        elif e["type"] in _WORDS:
            parts.append(_WORDS[e["type"]].format(label=label, **(e.get("ctx") or {})))
        else:
            parts.append(f"{label}: {text}")
    more = len(exc.errors()) - 4
    return "; ".join(parts[:1] + [_clause(p) for p in parts[1:]]) + (f"; and {more} more" if more > 0 else "")


def check(model: type[BaseModel], body: Any, **context) -> Any:
    """Validate `body` as `model`; a problem is a 422 whose detail is one sentence."""
    try:
        return model.model_validate(body, context=context)
    except ValidationError as exc:
        raise Refusal(422, message(exc, model), "invalid") from exc


# ------------------------------------------------------------------------------------------------ the pilot set-up (UI-13; round 7, audit pages-D-04)

from datetime import date as _date  # noqa: E402  (this section's own imports, so the shared head of the file stays as it was)

from pydantic import Field, field_validator, model_validator  # noqa: E402


def _iso_day(v: str | None, what: str) -> str | None:
    """A date written YYYY-MM-DD that is a real day, else a refusal naming `what`."""
    if v is None:
        return v
    try:
        if len(v) != 10:
            raise ValueError
        _date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{what} must be a real date written YYYY-MM-DD") from None
    return v


class PilotCriteriaEdit(Strict):
    """The success criteria a change sets (tab D): any of them, each a number in the range the tab allows; the ones not sent keep their values."""

    min_saving_pct: float = Field(None, ge=0, le=100)
    max_unserved_kwh: float = Field(None, ge=0, le=1_000_000)
    reserve_adequacy_pct: float = Field(None, ge=0, le=100)
    fidelity_pct: float = Field(None, ge=0, le=100)
    fee_multiple: float = Field(None, ge=0, le=100)
    forecast_pv_nrmse_max: float = Field(None, ge=0, le=1)
    # plan 15.3's gates (round 7, audit pages-D-12): the genset's run hours cut against the tuned rule, and the cycles a day against the tuned baseline's
    genset_hours_cut_pct: float = Field(None, ge=0, le=100)
    cycles_ratio_max: float = Field(None, ge=0, le=10)


class PilotClosure(Strict):
    """A declared holiday closure (R8 2.13, tab C: "no arm during a declared holiday closure"): its first and last day, both closed, and a name;
    at most 60 days (round 7, audit pages-D-19)."""

    start: str
    end: str
    label: str = Field("", max_length=80)

    @field_validator("start", "end")
    @classmethod
    def _day(cls, v):
        return _iso_day(v, "A closure's day")

    @model_validator(mode="after")
    def _span(self):
        n = (_date.fromisoformat(self.end) - _date.fromisoformat(self.start)).days
        if n < 0:
            raise ValueError("A closure's last day must be on or after its first day")
        if n >= 60:
            raise ValueError("A closure must be at most 60 days long")
        return self


class PilotEdit(Strict):
    """`PUT /sites/:id/pilot`: the fields of the set-up a person may change. The sign-off is written by the start alone and the amendments by the
    server, so neither is a field here (an unknown field is refused). `reason` is the amendment's, needed once the pilot has started (round 7,
    audit pages-D-17: "later edits create an amendment with a reason")."""

    baseline_start: str | None = None
    baseline_days: int = Field(None, ge=7, le=90)
    criteria: PilotCriteriaEdit = None
    reason: str = Field("", max_length=500)
    #: a started pilot adopts a later native capture by its version, as an amendment (round 7, audit flow-savings-pilot-bill-12)
    capture_version: int = Field(None, ge=1)
    closures: list[PilotClosure] = Field(None, max_length=20)

    @field_validator("baseline_start")
    @classmethod
    def _start(cls, v):
        return _iso_day(v, "The baseline window's first day")


class PilotStart(Strict):
    """`POST /sites/:id/pilot/start`: the full name of who signs the pilot off (round 7, audit flow-savings-pilot-bill-09)."""

    name: str = Field(min_length=1, max_length=120)


class SwitchbackPaint(Strict):
    """`PUT /sites/:id/switchback`: the calendar's first day, its pairs of blocks (`fortnights`: a fortnight of 7-day blocks, six days of 3-day
    ones) and the block length (round 7, audit security-roles-06, flow-savings-pilot-bill-25). The route refuses a first day before tomorrow and
    a length the site's control tier does not run."""

    start: str
    fortnights: int = Field(4, ge=1, le=26, strict=True)
    block_days: int = Field(7, strict=True)

    @field_validator("start")
    @classmethod
    def _first(cls, v):
        return _iso_day(v, "The calendar's first day")

    @field_validator("block_days")
    @classmethod
    def _length(cls, v):
        if v not in (3, 7):
            raise ValueError("A block must be 3 or 7 days long")
        return v


# ------------------------------------------------------------------------------------------------ the tariff editor (UX backend ask K32)

from typing import Literal  # noqa: E402

TariffKind = Literal["schedule", "tou", "adder", "tax", "icp", "prosumer", "override"]


class TariffDraftSource(Strict):
    """Where a draft's versions were read (step A): the SRO, NEPRA decision or bill (`ref`), its web address, and how far it is checked."""

    ref: str = Field("", max_length=200)
    url: str = Field("", max_length=500)
    verified: Literal["primary", "secondary", "unverified"] | None = None


class TariffDraftVersion(Strict):
    """One version of a draft: its kind, its key (the adder's, the calendar's DISCO group, the regime, or a site override's connection; the
    national kinds' one key is filled in) and its body as the editor holds it, checked only when the draft is checked or submitted."""

    kind: TariffKind
    key: str = Field("", max_length=80)
    body: dict = Field(default_factory=dict)


class TariffDraft(Strict):
    """A tariff draft as the editor saves it (`PUT /api/tariffs/drafts/{id}`) and checks it (`POST /api/tariffs/versions/validate`): its start
    (YYYY-MM-DD, or empty while drafted), its source, one version a kind it changes (at most seven), and the editor's own state (`editor`: its step,
    what it started from), kept as sent."""

    effective_from: str = Field("", max_length=10, title="The start")
    source: TariffDraftSource = Field(default_factory=TariffDraftSource)
    versions: list[TariffDraftVersion] = Field(default_factory=list, max_length=7, title="A draft's versions", json_schema_extra={"item": "version"})
    editor: dict = Field(default_factory=dict)


class _WhatIf(Strict):
    """What a preview prices as if approved: the draft as typed, a saved draft, or a version waiting for approval (exactly one)."""

    draft: TariffDraft | None = None
    draft_id: str | None = Field(None, max_length=64)
    version_id: str | None = Field(None, max_length=64)

    @model_validator(mode="after")
    def _one(self):
        if sum(x is not None for x in (self.draft, self.draft_id, self.version_id)) != 1:
            raise ValueError("Send one of draft (the draft as typed), draft_id (a saved draft) or version_id (a version waiting for approval)")
        return self


class TariffPreviewIn(_WhatIf):
    """`POST /api/tariffs/preview`: a week of 15-minute prices at a site (its plant's connection, or the one named) from `start` (the draft's start
    by default), under the version in force and under the draft."""

    site_id: str = Field(min_length=1, max_length=64)
    connection_id: str | None = Field(None, max_length=64)
    start: str | None = None
    days: int = Field(7, ge=1, le=7, strict=True)

    @field_validator("start")
    @classmethod
    def _start(cls, v):
        return _iso_day(v, "The first day")


class TariffSimulateIn(_WhatIf):
    """`POST /api/tariffs/simulate`: one of a site's bills (`bill_id`), or a billing month of its readings where none is entered (`month`,
    YYYY-MM, at its plant's connection or the one named), rebuilt under the version in force and under the draft."""

    site_id: str = Field(min_length=1, max_length=64)
    bill_id: str | None = Field(None, max_length=64)
    month: str | None = Field(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    connection_id: str | None = Field(None, max_length=64)

    @model_validator(mode="after")
    def _bill_or_month(self):
        if (self.bill_id is None) == (self.month is None):
            raise ValueError("Send one of bill_id (an entered bill) or month (a billing month, YYYY-MM)")
        return self


class TariffWithdrawIn(Strict):
    """`POST /api/tariffs/versions/{id}/withdraw`: why (kept with the version and in the audit trail), and whether it goes back to its author as a
    draft (`redraft`, an administrator's "send back")."""

    reason: str = Field("", max_length=300)
    redraft: bool = Field(False, strict=True)
