"""Round 5, the web app's last small asks: discarding a draft, the tier sign-off on every site answer, and the Idempotency-Key header allowed
cross-origin."""
from __future__ import annotations

from fastapi.testclient import TestClient

from gridlytics import api
from test_fix_api import world  # noqa: F401  (world is a fixture)

#: the web app's origin the tests serve (conftest.py's GRIDLYTICS_CORS_ORIGINS)
APP = "https://app.example.test"


def test_a_person_discards_their_own_draft_and_nobody_else_s(world):
    import identities
    a = world.c["admin"]
    other = identities.client("admin.two@fix.test", "admin", world.company, "member", raise_server_exceptions=False)
    assert a.put("/api/drafts/d-r5", json={"name": "Half a site"}).status_code == 200
    assert other.delete("/api/drafts/d-r5").status_code == 404              # another member's draft is private, so it is not found
    assert world.c["viewer"].delete("/api/drafts/d-r5").status_code == 403  # a viewer edits no sites
    assert world.c["operator"].delete("/api/drafts/d-r5").status_code == 403  # nor does an operator add them (audit role-operator-07)
    r = a.delete("/api/drafts/d-r5")
    assert r.status_code == 200 and r.json() == {"deleted": "d-r5"}
    assert all(d["draft_id"] != "d-r5" for d in a.get("/api/drafts").json())
    assert a.delete("/api/drafts/d-r5").status_code == 404


def test_every_site_answer_carries_the_tier_sign_off(world):
    a, sid = world.c["admin"], world.site
    assert a.put(f"/api/sites/{sid}/tier", json={"tier": "monitor", "signoff": "Installer B, 7 Nov"}).status_code == 200
    put = a.put(f"/api/sites/{sid}", json={"name": "Mill One renamed"})
    assert put.status_code == 200, put.text
    assert put.json()["tier_signoff"]["signoff"] == "Installer B, 7 Nov"
    made = a.post("/api/sites", json={"name": "Fresh site"})
    assert made.status_code == 200 and "tier_signoff" in made.json() and made.json()["tier_signoff"] is None


def test_the_idempotency_key_header_is_allowed_cross_origin():
    """The browser's own question, not the constant (round 7, audit tests-and-perf-20): the web app on its origin asks in a preflight to send an
    Idempotency-Key with a POST and is let, and the POST it then sends is answered to that origin. Asserting api.CORS_ALLOW_HEADERS stayed green
    whatever the middleware was given."""
    c = TestClient(api.app, raise_server_exceptions=False)
    asked = {"origin": APP, "access-control-request-method": "POST", "access-control-request-headers": "content-type, idempotency-key"}
    pre = c.options("/api/local/logout", headers=asked)
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == APP, pre.text
    assert "idempotency-key" in {h.strip().lower() for h in pre.headers["access-control-allow-headers"].split(",")}
    r = c.post("/api/local/logout", json={}, headers={"origin": APP, "Idempotency-Key": "logout-1"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == APP, r.text
    # another origin asking the same is not let
    assert c.options("/api/local/logout", headers={**asked, "origin": "https://evil.example"}).status_code == 400
