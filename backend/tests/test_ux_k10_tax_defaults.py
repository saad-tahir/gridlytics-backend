"""UX backend ask K10 (plan H11; C19.2 item 6, C6.4; the owner's question Q35, recommended answer: per connection, prefilled; step 9b, Set-up C).

`GET /api/meta` answers `company_tax_defaults`: the four tax flags (STRN, GST claimed back, s.235 adjusted, ATL) of the grid connection the company
saved last, with its site, so step C's DUTY AND TAXES boxes start from them and say "As at Mill B" in place of PRELIMINARY. Not from a demo site, a
removed site or an off-grid connection; the province follows the new site's DISCO and is not among them. For the roles that open Set-up (see_costs);
null for a viewer, and while the company has no such connection."""
from __future__ import annotations

from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import sites, store

NONE_RECOVERED = {"strn": True, "gst_recoverable": False, "s235_adjustable": False, "atl": False, "province": "punjab"}


def _defaults(w, role="admin"):
    return w.c[role].get("/api/meta").json()["company_tax_defaults"]


def test_the_last_saved_connection_s_tax_status_is_the_company_s_default(world):
    # Mill One's connection (saved by the fixture, the server's default: a registered taxpayer that recovers everything)
    d = _defaults(world)
    assert {k: d[k] for k in ("strn", "gst_recoverable", "s235_adjustable", "atl")} == {"strn": True, "gst_recoverable": True, "s235_adjustable": True,
                                                                                      "atl": True}
    assert (d["site_name"], d["words"], d["site_id"]) == ("Mill One", "As at Mill One", world.site)
    assert d["saved_at"].endswith("+05:00") and "province" not in d
    # a connection saved later, at another site, bearing its taxes: the company's default now
    r = world.c["admin"].put(f"/api/sites/{world.other}/connection", json={"tariff_category": "B-2(b)", "sanctioned_load_kw": 300,
                                                                            "tax": NONE_RECOVERED})
    assert r.status_code == 200, r.text
    d = _defaults(world)
    assert (d["strn"], d["gst_recoverable"], d["s235_adjustable"], d["atl"], d["words"]) == (True, False, False, False, "As at Secret Site Two")
    assert d["connection_id"] == r.json()["connections"][0]["id"]


def test_a_demo_site_a_removed_site_and_an_off_grid_connection_are_not_the_company_s(world):
    with world.use():
        demo = sites.create({"name": "Demo mill", "disco": "LESCO"}, "t", demo_link="demo")["id"]
        sites.put_connection(demo, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400, "tax": {"strn": False, "gst_recoverable": False}}, "t")
        farm = sites.create({"name": "Farm", "site_type": "farm"}, "t")["id"]
        sites.put_connection(farm, {"tariff_category": "D-2(a)", "sanctioned_load_kw": 50, "regime": "off_grid",
                                    "tax": {"strn": False, "gst_recoverable": False}}, "t")
    assert _defaults(world)["words"] == "As at Mill One"
    with world.use():
        gone = sites.create({"name": "Gone mill"}, "t")["id"]
        sites.put_connection(gone, {"tariff_category": "B-2(b)", "sanctioned_load_kw": 400, "tax": {"strn": False, "gst_recoverable": False}}, "t")
    assert _defaults(world)["words"] == "As at Gone mill" and _defaults(world)["strn"] is False
    world.c["admin"].delete(f"/api/sites/{gone}")
    assert _defaults(world)["words"] == "As at Mill One"
    # with no grid connection of its own left, the company has no default
    with world.use(), store.connect() as con:
        con.execute("DELETE FROM connections WHERE site_id = ?", (world.site,))
    assert _defaults(world) is None


def test_the_roles_that_open_set_up_read_it_and_a_viewer_does_not(world):
    for role, has in (("admin", True), ("operator", True), ("analyst", True), ("viewer", False)):
        d = _defaults(world, role)
        assert (d is not None) is has, (role, d)
    # a machine key reaches no company setting, the meta answer included (apikeys: outside every scope)
    assert world.anon.get("/api/meta", headers=world.key(["sites:read", "plans:read"])).status_code == 403
