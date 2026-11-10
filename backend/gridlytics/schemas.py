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


def _label(loc: tuple) -> str:
    head = "The " + str(loc[0]).replace("_", " ")
    return head + "".join(f"'s {str(p).replace('_', ' ')}" for p in loc[1:] if not isinstance(p, int))


def message(exc: ValidationError) -> str:
    parts = []
    for e in exc.errors()[:4]:
        loc = tuple(e["loc"])
        text = e["msg"].removeprefix("Value error, ")
        if not loc or e["type"] == "value_error":
            parts.append(text)
        elif e["type"] == "extra_forbidden":
            parts.append(f"Unknown field '{'.'.join(str(p) for p in loc)}'")
        elif e["type"] == "string_too_short":
            n = (e.get("ctx") or {}).get("min_length", 1)
            parts.append(f"{_label(loc)} is needed" if n <= 1 else f"{_label(loc)} must be at least {n} characters")
        elif e["type"] in _WORDS:
            parts.append(_WORDS[e["type"]].format(label=_label(loc), **(e.get("ctx") or {})))
        else:
            parts.append(f"{_label(loc)}: {text}")
    more = len(exc.errors()) - 4
    return "; ".join(parts) + (f"; and {more} more" if more > 0 else "")


def check(model: type[BaseModel], body: Any, **context) -> Any:
    """Validate `body` as `model`; a problem is a 422 whose detail is one sentence."""
    try:
        return model.model_validate(body, context=context)
    except ValidationError as exc:
        raise Refusal(422, message(exc), "invalid") from exc


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
        raise ValueError(f"{what} is a date written YYYY-MM-DD") from None
    return v


class PilotCriteriaEdit(Strict):
    """The success criteria a change sets (tab D): any of them, each a number in the range the tab allows; the ones not sent keep their values."""

    min_saving_pct: float = Field(None, ge=0, le=100)
    max_unserved_kwh: float = Field(None, ge=0, le=1_000_000)
    reserve_adequacy_pct: float = Field(None, ge=0, le=100)
    fidelity_pct: float = Field(None, ge=0, le=100)
    fee_multiple: float = Field(None, ge=0, le=100)
    forecast_pv_nrmse_max: float = Field(None, ge=0, le=1)


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
            raise ValueError("A closure's last day is on or after its first day")
        if n >= 60:
            raise ValueError("A closure is at most 60 days")
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
    ones) and the block length (round 7, audit security-roles-06). The route refuses a first day before tomorrow."""

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
            raise ValueError("A block is 3 or 7 days long")
        return v
