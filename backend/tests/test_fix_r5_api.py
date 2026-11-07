"""Round 5, the web app's last small asks: discarding a draft, the tier sign-off on every site answer, and the Idempotency-Key header allowed
cross-origin."""
from __future__ import annotations

from gridlytics import api
from test_fix_api import world  # noqa: F401  (world is a fixture)


def test_a_person_discards_their_own_draft_and_nobody_else_s(world):
    a, op = world.c["admin"], world.c["operator"]
    assert a.put("/api/drafts/d-r5", json={"name": "Half a site"}).status_code == 200
    assert op.delete("/api/drafts/d-r5").status_code == 404                 # another member's draft is private, so it is not found
    assert world.c["viewer"].delete("/api/drafts/d-r5").status_code == 403  # a viewer edits no sites
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
    assert "Idempotency-Key" in api.CORS_ALLOW_HEADERS
