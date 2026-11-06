"""The viewer's money filter (plan Part 10.5): server-side and by name, never a client-side hide.

Every money field of every answer is named in `MONEY_KEYS` or ends in `_rs` / `_rs_per_kwh` / `_rs_per_kw`; a viewer (a role without `see_costs`) gets every
answer with those keys removed, at any depth, and the fee keys need `see_fee` as well. Reason texts are written from templates whose money parameters are
omitted for a viewer (optimise/explain.py), so no rupee figure travels inside a sentence either. `test_role_walk.py` compares the admin's and the viewer's
answer to every route and fails on any money value the viewer's answer still holds.
"""
from __future__ import annotations

from . import auth

MONEY_KEYS = frozenset({
    "price", "prices", "rate", "rates", "cost", "costs", "fee", "voll", "c_imp", "c_exp", "c_fuel", "c_cyc", "c_start", "c_runhr", "c_om",
    "objective", "objective_rs", "saving", "savings", "saving_rs", "ledger", "rate_mdi", "replacement_cost_per_kwh", "fuel_price", "diesel_price",
    "export_credit", "import_price", "export_price", "tariff_body", "bill", "rebuilt", "real", "cost_breakdown", "wear_rs", "v_end", "terminal_value",
    "fee_rs", "gain_share", "price_per_l", "value_rs", "money", "energy_charge", "fixed_charge", "taxes", "adders", "total", "fee_ratio",
    "levers", "mdi_rate", "rs_per_kwh", "c_short", "naepp", "lines", "cards", "fuel_prices",
})
FEE_KEYS = frozenset({"fee", "fee_rs", "gain_share", "fee_ratio"})
SUFFIXES = ("_rs", "_rs_per_kwh", "_rs_per_kw", "_pkr")


def is_money(key: str) -> bool:
    return key in MONEY_KEYS or key.endswith(SUFFIXES)


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


def for_reader(obj):
    """The answer as the current reader may see it."""
    cost_ok, fee_ok = auth.can("see_costs"), auth.can("see_fee")
    if cost_ok and fee_ok:
        return obj
    return _strip(obj, fee_ok, cost_ok)


def for_key(obj):
    """The answer as a machine key may see it: no money at all (plan Part 10.5)."""
    return _strip(obj, False, False)
