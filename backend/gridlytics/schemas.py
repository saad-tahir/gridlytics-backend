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
