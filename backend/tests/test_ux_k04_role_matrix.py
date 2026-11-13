"""UX backend ask K4 (plan H11; G9 item 1): the role matrix in GET /api/meta.

The redesigned Company settings draws the role guide as a matrix (G2.1.2) and People's consequence line (G2.2.3) from `role_matrix`, so neither can
drift from what the server allows: `{permission: [roles]}` straight from `auth.PERMISSIONS`, the roles in `auth.ROLES` order. It holds no money and is
the same for every reader, a viewer included."""
from __future__ import annotations

import json

from gridlytics import auth, money
from test_fix_api import ROLES, world  # noqa: F401  (world is a fixture)


def test_the_meta_answers_the_role_matrix_straight_from_the_permission_table(world):
    want = {p: [r for r in auth.ROLES if r in who] for p, who in auth.PERMISSIONS.items()}
    assert want["see_costs"] == ["admin", "operator", "analyst"] and want["energy_report"] == ["admin", "operator", "analyst", "viewer"]
    for role in ROLES:
        meta = world.c[role].get("/api/meta").json()
        assert meta["role_matrix"] == want, role
        # the reader's own permissions are its column of the matrix
        assert {p: role in roles for p, roles in meta["role_matrix"].items()} == meta["permissions"], role


def test_the_role_matrix_holds_no_money_and_reaches_a_viewer_whole(world):
    viewer = world.c["viewer"].get("/api/meta").json()["role_matrix"]
    assert viewer == world.c["admin"].get("/api/meta").json()["role_matrix"]
    assert not any(money.is_money(k) for k in viewer)
    # the viewer's column holds no permission about money: the role guide's money rows read "No" for a viewer
    for p in ("see_costs", "see_fee", "see_money_settings", "edit_money_settings", "export"):
        assert "viewer" not in viewer[p], p
    # the text of the answer carries no rupee figure (the words a refusal uses stay in who_can)
    assert "Rs" not in json.dumps(viewer)


def test_every_permission_and_role_is_in_the_matrix_and_nothing_else():
    from gridlytics.routes_sites import meta
    tokens = auth.current.set({"id": "u", "name": "Test", "role": "viewer"})
    try:
        matrix = meta()["role_matrix"]
    finally:
        auth.current.reset(tokens)
    assert list(matrix) == list(auth.PERMISSIONS)
    assert {r for roles in matrix.values() for r in roles} <= set(auth.ROLES)
    assert all(roles == sorted(roles, key=list(auth.ROLES).index) for roles in matrix.values())
