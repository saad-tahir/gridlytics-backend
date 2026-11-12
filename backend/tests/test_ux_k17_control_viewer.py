"""UX backend ask K17 (plan H11; D9.7, D3.0, owner's question Q20): the control answer reaches a viewer with every money key stripped.

The redesigned Plan section lists the Inverter table for a viewer, read only and in energy (plan D3.0; Q20, recommended yes "once the control answer is
tested with every money key stripped"). Its data is `GET /sites/:id/control`, which needs no permission to read (routes_plans.control) and carries one
rupee figure: the gain of each issued table (`issued[].gain_rs`) and of a held re-issue (`reissue.gain_rs`). The API's money filter strips them by name
(money.py: the `_rs` suffix) for every reader without `see_costs` and for every machine key.

Proved here on a real plan at the advisory tier with a table issued for a gain and a re-issue held back below its threshold: the viewer's answer and a
machine key's are the administrator's with every money key removed and nothing else changed (the issued days and plans, the held re-issue's decision,
the compiled table, the vendor's rows and notes, the applied log), they hold no money key at any depth, no rupee figure in words and none of the
administrator's rupee figures; the operator and the analyst (`see_costs`) read the gains. A viewer takes no action there: marking a table applied and
every control action are refused."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from gridlytics import money, plans, sites, store
from test_fix_api import world  # noqa: F401  (world is a fixture)

PKT = timezone(timedelta(hours=5))
RUPEES = re.compile(r"\bRs\.?\s*-?\d|\bPKR\s*-?\d|\brupees?\b", re.IGNORECASE)
#: The made-up gains: figures nothing else in the answer holds
ISSUED_GAIN, HELD_GAIN = 4321.0, 987.0


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def _money_paths(obj, at: str = "") -> set[str]:
    """The key paths of the money fields in an answer (`issued[].gain_rs`)."""
    if isinstance(obj, dict):
        return set().union(set(), *({f"{at}.{k}".lstrip(".")} if isinstance(k, str) and money.is_money(k) else _money_paths(v, f"{at}.{k}".lstrip("."))
                                    for k, v in obj.items()))
    if isinstance(obj, list):
        return set().union(set(), *(_money_paths(v, f"{at}[]") for v in obj))
    return set()


def _numbers(obj) -> set[float]:
    if isinstance(obj, bool):
        return set()
    if isinstance(obj, (int, float)):
        return {round(float(obj), 2)}
    if isinstance(obj, dict):
        return set().union(*(_numbers(v) for v in obj.values())) if obj else set()
    if isinstance(obj, list):
        return set().union(*(_numbers(v) for v in obj)) if obj else set()
    return set()


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _advisory_with_gains(world) -> tuple[str, str]:
    """The world's mill at the advisory tier with two real plans: today's table issued from the first for a gain (an 'event' re-issue), the second in
    force, and its re-issue held back below the threshold with its gain; the first table marked applied. Returns the two plans' ids."""
    sid = world.site
    with world.use():
        sites.set_tier_capable(sid, "advisory_writes")
        sites.set_tier_allowed(sid, "advisory_writes", "t", "test")
        first = plans.tick(sid)
        assert first["status"] == "active", first.get("reason")
        today = datetime.now(PKT).date().isoformat()
        now = int(datetime.now(timezone.utc).timestamp())
        plans._write_issue(sid, today, first["plan_id"], "event", now - 3600, ISSUED_GAIN)
        second = plans.tick(sid)
        assert second["status"] == "active", second.get("reason")
        sites.put_state(sid, table_reissue={"plan_id": second["plan_id"], "day": today, "gain_rs": HELD_GAIN, "issued": False,
                                            "blocked": "below_threshold", "at": plans._iso(now)})
        plans.mark_applied(sid, first["plan_id"], today, "Installer", "installer_keyed")
    return first["plan_id"], second["plan_id"]


def test_the_control_answer_reaches_a_viewer_and_a_key_with_every_money_key_stripped(world):
    first, second = _advisory_with_gains(world)
    url = f"/api/sites/{world.site}/control"
    key = world.key(["sites:read", "plans:read"])
    got = {}
    for reader in ("admin", "operator", "analyst", "viewer", "key"):
        r = world.anon.get(url, headers=key) if reader == "key" else world.c[reader].get(url)
        assert r.status_code == 200, (reader, r.text)
        got[reader] = r.json()
    admin = got["admin"]
    # the administrator reads the issue's gain and the held re-issue's, and so do the operator and the analyst (see_costs)
    today = datetime.now(PKT).date().isoformat()
    issued_today = next(i for i in admin["issued"] if i["day"] == today)
    assert (issued_today["plan_id"], issued_today["reason"], issued_today["gain_rs"]) == (first, "event", ISSUED_GAIN)
    assert (admin["plan_id"], admin["reissue"]["plan_id"], admin["reissue"]["gain_rs"], admin["reissue"]["blocked"]) == \
        (second, second, HELD_GAIN, "below_threshold")
    assert admin["compiled"]["days"] and admin["vendor_view"] and admin["applied"]
    # the answer's one money is those two gains (D9.7): every other field is energy, words, ids and times, and reaches the viewer whole
    assert _money_paths(admin) == {"issued[].gain_rs", "reissue.gain_rs"}
    for reader in ("operator", "analyst"):
        assert got[reader]["reissue"]["gain_rs"] == HELD_GAIN, reader
        assert next(i for i in got[reader]["issued"] if i["day"] == today)["gain_rs"] == ISSUED_GAIN, reader
    for reader in ("viewer", "key"):
        body = got[reader]
        # the administrator's answer with every money key removed, at any depth, and nothing else changed
        assert body == money.for_key(admin), reader
        assert not [k for k in _keys(body) if isinstance(k, str) and money.is_money(k)], reader
        assert "gain_rs" not in json.dumps(body), reader
        assert not [s for s in _strings(body) if RUPEES.search(s)], reader
        assert not {ISSUED_GAIN, HELD_GAIN} & _numbers(body), reader
        # the energy table is whole: which plan's table each day runs, the held re-issue's decision, the table, the vendor's rows, the applied log
        assert next(i for i in body["issued"] if i["day"] == today) == {"day": today, "plan_id": first, "issued_at": issued_today["issued_at"],
                                                                        "reason": "event"}, reader
        assert body["reissue"] == {k: v for k, v in admin["reissue"].items() if k != "gain_rs"}, reader
        assert body["compiled"] == admin["compiled"] and body["vendor_view"] == admin["vendor_view"], reader
        assert body["vendor_notes"] == admin["vendor_notes"] and body["applied"] == admin["applied"], reader


def test_a_viewer_reads_the_inverter_table_but_takes_no_action(world):
    """Q20: the viewer reads Plan and the Inverter table, energy only, with no actions: marking a table applied and every control action are refused."""
    first, _ = _advisory_with_gains(world)
    sid = world.site
    v = world.c["viewer"]
    r = v.post(f"/api/sites/{sid}/plan/apply", json={"plan_id": first})
    assert r.status_code == 403, r.text
    for action in ("hold", "release", "pause", "resume", "genset_hold", "charge_block", "force_charge", "test_fallback"):
        r = v.post(f"/api/sites/{sid}/control/{action}", json={})
        assert r.status_code == 403, (action, r.text)
    assert v.post(f"/api/sites/{sid}/plan/solve").status_code == 403
    # and nothing was written by the refusals: the applied log still holds the one mark
    with world.use():
        assert len(plans.applied_rows(sid)) == 1
        assert sites.overrides(sid) == []
    with world.use(), store.connect() as con:
        assert con.execute("SELECT count(*) FROM plans WHERE site_id = ?", (sid,)).fetchone()[0] == 2
