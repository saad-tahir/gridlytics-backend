"""UX backend ask K33 (plan H11; G9 item 2, G3.4; step 14, My account's EMAIL ALERTS card; owner's question Q77).

`GET /api/platform/notifications` offers the kinds of alert the reader receives (`alerts.reader_categories()`, as the inbox gives them), not every
kind: a viewer the five energy kinds, an operator eight (no Product: no `see_fee`), an administrator or an analyst all nine. A save refuses a kind the
person does not receive ("You do not receive Savings alerts"). The email card's ticks are `email_kinds`, the kinds the person wants by email: a save
that gives at least one sets `email` true, an empty one false. They are apart from `follow`, the kinds the inbox and its badge list (round 7 sweeps,
the rest of role-analyst-32), so choosing what to receive by email never hides an alert from the person's inbox (plan G3.4: "the inbox and the counts
must stay complete"). Before K33 the kinds by email were `follow` when `email` was true, and none when it was false (plan G3.4's compatibility rule):
an answer reads a person's choices of before so."""
from __future__ import annotations

from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import alerts, store

ENERGY = ["Data", "Reserve", "Battery", "Genset", "Forecast"]


def _get(w, role):
    r = w.c[role].get("/api/platform/notifications")
    assert r.status_code == 200, (role, r.text)
    return r.json()


def test_each_reader_is_offered_exactly_the_kinds_the_inbox_gives_them(world):
    assert _get(world, "viewer")["categories"] == ENERGY
    assert _get(world, "operator")["categories"] == [c for c in alerts.CATEGORIES if c != "Product"]
    assert _get(world, "admin")["categories"] == list(alerts.CATEGORIES) == _get(world, "analyst")["categories"]
    for role in ("admin", "operator", "analyst", "viewer"):
        got = _get(world, role)
        # before a choice, every kind received is followed in the inbox and none goes by email
        assert got["follow"] == got["categories"] and got["email"] is False and got["email_kinds"] == [], (role, got)
        assert got["categories"] == world.c[role].get("/api/alerts/counts").json()["categories"], role


def test_a_kind_the_person_does_not_receive_is_refused_in_words(world):
    v = world.c["viewer"]
    before = _get(world, "viewer")
    for body, said in (({"email_kinds": ["Savings"]}, "You do not receive Savings alerts"),
                       ({"email_kinds": ["Data", "Tariff", "Product"]}, "You do not receive Tariff or Product alerts"),
                       ({"follow": ["Data", "Control"]}, "You do not receive Control alerts"),
                       ({"email_kinds": ["Weather"]}, "Choose the kinds of alert to email from those you receive: Data, Reserve, Battery, Genset, Forecast"),
                       ({"follow": ["Weather"]}, "Follow is a list of the kinds of alert you receive: Data, Reserve, Battery, Genset, Forecast"),
                       ({"email_kinds": "Data"}, "Choose the kinds of alert to email"),
                       ({"email_kinds": ["Data"], "email": False}, "Send the kinds of alert to email, or whether to email, not both"),
                       ({"email_kinds": ["Data"], "sms": True}, "Unknown field 'sms'")):
        r = v.put("/api/platform/notifications", json=body)
        assert r.status_code == 422 and said in r.json()["detail"], (body, r.text)
        assert "Rs" not in r.json()["detail"]
    assert _get(world, "viewer") == before
    # an operator does not receive Product (the fee's and the contract's notices), an administrator does
    r = world.c["operator"].put("/api/platform/notifications", json={"email_kinds": ["Product"]})
    assert r.status_code == 422 and r.json()["detail"] == "You do not receive Product alerts", r.text
    assert world.c["admin"].put("/api/platform/notifications", json={"email_kinds": ["Product"]}).status_code == 200


def test_a_save_of_kinds_to_email_sets_email_and_never_changes_the_inbox(world):
    """Ticking a kind for email is a choice of email alone: `follow` (the inbox's) is untouched, so no alert leaves the inbox."""
    op, sid = world.c["operator"], world.site
    with world.use():
        alerts._open(sid, "data_stale", "Data", "warning", "No data for 40 minutes", {})
        alerts._open(sid, "sigma_high", "Battery", "warning", "The battery estimate may be off", {})
    r = op.put("/api/platform/notifications", json={"email_kinds": ["Reserve", "Data"]})
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["email_kinds"] == ["Data", "Reserve"] and got["email"] is True and got["follow"] == got["categories"], got
    assert _get(world, "operator") == got
    assert op.get("/api/alerts/counts").json()["total"] == 2 and op.get("/api/alerts/counts").json()["following"] is None
    assert sorted(a["category"] for a in op.get("/api/alerts").json()) == ["Battery", "Data"]
    # an empty choice sends nothing by email
    got = op.put("/api/platform/notifications", json={"email_kinds": []}).json()
    assert got["email_kinds"] == [] and got["email"] is False and got["follow"] == got["categories"], got
    # what is kept, and the audit line of the change
    with world.use():
        kept = store.get_setting(alerts.notify_key(_user_id(world, "operator")))
        line = next(a for a in store.list_audit() if a["action"] == "changed their notification choices")
    assert kept["email_kinds"] == [] and kept["email"] is False, kept
    assert line["detail"]["changes"]["email_kinds"] == {"before": ["Data", "Reserve"], "after": []}, line


def test_the_choices_of_before_read_as_the_email_card_shows_them(world):
    """Today's page saves `follow` and `email` ("Email them to me as well"): the kinds by email are then the kinds followed, or none when email is off;
    such a save after the email card's puts the card's kinds back on that rule."""
    v = world.c["viewer"]
    got = v.put("/api/platform/notifications", json={"follow": ["Data", "Genset"], "email": True}).json()
    assert (got["follow"], got["email"], got["email_kinds"]) == (["Data", "Genset"], True, ["Data", "Genset"]), got
    got = v.put("/api/platform/notifications", json={"email": False}).json()
    assert (got["follow"], got["email"], got["email_kinds"]) == (["Data", "Genset"], False, []), got
    got = v.put("/api/platform/notifications", json={"email_kinds": ["Forecast"]}).json()
    assert (got["follow"], got["email"], got["email_kinds"]) == (["Data", "Genset"], True, ["Forecast"]), got
    got = v.put("/api/platform/notifications", json={"follow": ["Data"]}).json()     # the inbox's list alone: the email card's kinds stay
    assert (got["follow"], got["email"], got["email_kinds"]) == (["Data"], True, ["Forecast"]), got
    got = v.put("/api/platform/notifications", json={"email": True}).json()
    assert (got["follow"], got["email"], got["email_kinds"]) == (["Data"], True, ["Data"]), got


def test_a_kind_kept_from_a_role_held_before_is_not_offered_or_read(world):
    """A person whose role lost a kind (an administrator made a viewer) is answered only the kinds received now, and the inbox counts no other."""
    with world.use():
        store.set_setting(alerts.notify_key(_user_id(world, "viewer")), {"follow": ["Data", "Savings"], "email": True, "email_kinds": ["Savings", "Reserve"]})
    got = _get(world, "viewer")
    assert (got["follow"], got["email"], got["email_kinds"]) == (["Data"], True, ["Reserve"]), got
    assert world.c["viewer"].get("/api/alerts/counts").json()["following"] == ["Data"]


def _user_id(w, role) -> str:
    users = w.c["admin"].get("/api/users").json()
    users = users.get("users", users) if isinstance(users, dict) else users
    return next(u["id"] for u in users if u["email"] == f"{role}@fix.test")
