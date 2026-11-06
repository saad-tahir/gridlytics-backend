"""Alerts (UI-16): the inbox, counts, actions and the rule editor."""
from __future__ import annotations

from fastapi import APIRouter, Body

from . import alerts, auth
from .routes_common import bad, out
from .schemas import Refusal

router = APIRouter()


@router.get("/api/alerts")
def inbox(state: str | None = "active", site_id: str | None = None, category: str | None = None):
    return out(alerts.listing(state or None, site_id, category))


@router.get("/api/alerts/counts")
def counts():
    c = alerts.counts()
    c["tariff_pending"] = alerts.tariff_pending_count() if auth.can("see_costs") else 0
    return c


@router.get("/api/alerts/rules")
def rules_get():
    return alerts.rules()


@router.put("/api/alerts/rules")
def rules_put(inp: dict = Body(...)):
    auth.need("edit_workspace", "Changing the alert rules")
    try:
        return alerts.set_rules(inp, auth.actor())
    except ValueError as exc:
        raise bad(exc) from None


@router.post("/api/alerts/{alert_id}/{action}")
def act(alert_id: str, action: str, inp: dict = Body(default={})):
    auth.need("ack_alerts", "Acting on an alert")
    if action not in ("acknowledge", "assign", "close"):
        raise Refusal(404, "The actions are acknowledge, assign and close", "not_found")
    try:
        return out(alerts.act(alert_id, action, auth.actor(), str(inp.get("note", ""))[:300], str(inp.get("assignee", ""))))
    except KeyError:
        raise Refusal(404, "No such alert", "not_found") from None
    except ValueError as exc:
        raise bad(exc) from None
