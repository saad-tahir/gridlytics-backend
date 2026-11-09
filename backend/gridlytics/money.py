"""The money filter (plan Part 10.5): server-side and by name, never a client-side hide.

Every money field of every answer is named in `MONEY_KEYS` or ends in `_rs` / `_rs_per_kwh` / `_rs_per_kw` / `_pkr` (or carries `_rs_` inside, or starts
`rs_`); a reader without `see_costs` (a viewer) gets every answer with those keys removed, at any depth, and the fee keys need `see_fee` as well. A machine key
gets no money field at all, whatever role it was made with.

The filter is the API's, not each route's (audit security-roles-02): the guard (api.py) runs every JSON answer to a reader who may not see every money field
through `filter_json`, so a route that forgets `routes_common.out()` still sends no rupee. `out()` stays where a route calls it (the same filter, twice, changes
nothing). A plan step's reason is a code with its figures in a detail whose money figures carry money names (`optimise.explain`: `rs_per_kwh`), so no rupee
figure travels inside a sentence either. `backend/tests/test_role_walk.py` reads every GET route as the administrator, an operator, a viewer and a site key,
and fails on any money key, any rupee figure in a sentence, or any money value of the administrator's answer that the viewer's answer still holds.
"""
from __future__ import annotations

import json

from . import auth

# "total" is not here: Gridlytics' money totals end in _rs (a bill's total_rs), and a bare "total" is a count (the alerts' badge, the demo's progress)
MONEY_KEYS = frozenset({
    "price", "prices", "rate", "rates", "cost", "costs", "fee", "voll", "c_imp", "c_exp", "c_fuel", "c_cyc", "c_start", "c_runhr", "c_om",
    "objective", "objective_rs", "saving", "savings", "saving_rs", "ledger", "rate_mdi", "replacement_cost_per_kwh", "fuel_price", "diesel_price",
    "export_credit", "import_price", "export_price", "tariff_body", "bill", "rebuilt", "real", "cost_breakdown", "wear_rs", "v_end", "terminal_value",
    "fee_rs", "gain_share", "price_per_l", "value_rs", "money", "energy_charge", "fixed_charge", "taxes", "adders", "fee_ratio",
    "levers", "mdi_rate", "rs_per_kwh", "c_short", "naepp", "lines", "cards", "fuel_prices",
    "saving_rs_per_day_unadjusted", "saving_rs_per_day_adjusted",
})
FEE_KEYS = frozenset({"fee", "fee_rs", "gain_share", "fee_ratio"})
SUFFIXES = ("_rs", "_rs_per_kwh", "_rs_per_kw", "_pkr")
#: Text whose absence from a JSON answer proves no key of it is money (every money key holds one of these: a name of MONEY_KEYS or FEE_KEYS, or the
#: characters of a rupee suffix); JSON never escapes a letter or "_", so a key holding one holds it in the answer's text too
_MONEY_TEXT = tuple(sorted({k.encode() for k in MONEY_KEYS | FEE_KEYS} | {b"_rs", b"rs_", b"_pkr"}))
_FEE_TEXT = tuple(sorted(k.encode() for k in FEE_KEYS))


def is_money(key: str) -> bool:
    """A money key by name: listed, ending in a rupee suffix, or carrying rupees inside it (`*_rs_per_day`, `*_rs_per_month`, `rs_per_*`)."""
    return key in MONEY_KEYS or key.endswith(SUFFIXES) or "_rs_" in key or key.startswith("rs_")


def _strip(obj, fee_ok: bool, cost_ok: bool):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str):
                if k in FEE_KEYS and not fee_ok:
                    continue
                if not cost_ok and is_money(k):
                    continue
            out[k] = _strip(v, fee_ok, cost_ok)
        return out
    if isinstance(obj, list):
        return [_strip(v, fee_ok, cost_ok) for v in obj]
    return obj


def _rights() -> tuple[bool, bool]:
    """What the current reader may see: (costs, the fee). A machine key sees neither (plan Part 10.5)."""
    if (auth.current.get() or {}).get("api_key"):
        return False, False
    return auth.can("see_costs"), auth.can("see_fee")


def for_reader(obj):
    """The answer as the current reader may see it; a machine key gets no money at all (plan Part 10.5: plans served to a key carry no money fields)."""
    cost_ok, fee_ok = _rights()
    if cost_ok and fee_ok:
        return obj
    return _strip(obj, fee_ok, cost_ok)


def for_key(obj):
    """The answer as a machine key may see it: no money at all (plan Part 10.5)."""
    return _strip(obj, False, False)


def filtered_reader() -> bool:
    """Whether the current reader's answers lose anything to the filter (a viewer, an operator, a machine key); an administrator's and an analyst's never do."""
    cost_ok, fee_ok = _rights()
    return not (cost_ok and fee_ok)


def filter_json(raw: bytes) -> bytes | None:
    """A JSON answer's text as the current reader may see it, or None when it is theirs unchanged: an answer in which no money name appears is decided on its
    text alone (a page of telemetry is never parsed), any other is parsed, filtered (`for_reader`) and written again as FastAPI writes JSON. An answer that
    says it is JSON and cannot be read as JSON raises ValueError: it is never sent unchecked."""
    cost_ok, fee_ok = _rights()
    if cost_ok and fee_ok or not raw.strip():
        return None
    if not any(t in raw for t in (_FEE_TEXT if cost_ok else _MONEY_TEXT)):
        return None
    data = json.loads(raw)
    return json.dumps(_strip(data, fee_ok, cost_ok), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
