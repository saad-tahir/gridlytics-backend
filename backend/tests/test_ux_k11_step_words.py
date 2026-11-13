"""UX backend ask K11 (plan H11; C19.2 item 7, C9.3; step 9a, Set-up's Review and activate).

The activation's refusal names the Set-up step that holds each missing part ("step A"), as the redesigned Set-up calls its screens, where it named
the wizard's tab ("tab A"), and carries `missing` ({step, words} for each part, in the steps' order), so Review links each "step A" to its step
without reading the sentence. The set-up warning on a connection priced on the wrong DISCO's windows names its step the same way. The lifecycle's
"Draft: step C of 6" reads the same rule, so the track and the refusal name the same step."""
from __future__ import annotations

import re

from test_fix_api import world  # noqa: F401  (world is a fixture)

from gridlytics import sites


def test_the_activation_refusal_names_each_set_up_step_and_lists_them(world):
    a = world.c["admin"]
    sid = a.post("/api/sites", json={"name": "Mill K11", "disco": "LESCO"}).json()["id"]
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.status_code == 422 and r.json()["code"] == "incomplete", r.text
    body = r.json()
    assert body["detail"] == ("Add the data source, with its grid-presence signal confirmed (step A) and the inverter (step B) and the grid connection "
                              "and tariff (step C) before activating")
    assert body["missing"] == [{"step": "a", "words": "the data source, with its grid-presence signal confirmed (step A)"},
                               {"step": "b", "words": "the inverter (step B)"}, {"step": "c", "words": "the grid connection and tariff (step C)"}]
    assert not re.search(r"\btabs?\b", body["detail"], re.IGNORECASE)
    # each step the sentence names is a step `missing` lists, so a link per "step X" never points elsewhere
    assert [m.lower() for m in re.findall(r"\bstep ([A-E])\b", body["detail"])] == [m["step"] for m in body["missing"]]
    # the stage track names the first of them, as it did (UX K1)
    lc = a.get(f"/api/sites/{sid}").json()["lifecycle"]
    assert (lc["stages"][0]["target"], lc["words"]) == ("a", "Draft: step A of 6")


def test_a_part_added_leaves_the_refusal_and_the_list_alike(world):
    a = world.c["admin"]
    sid = a.post("/api/sites", json={"name": "Mill K11 b", "disco": "LESCO", "data_source": {"kind": "csv", "grid_signal_confirmed": True}}).json()["id"]
    assert a.put(f"/api/sites/{sid}/assets/inverter", json={"brand": "deye", "kw_ac": 50}).status_code == 200
    r = a.post(f"/api/sites/{sid}/activate")
    assert r.json()["detail"] == "Add the grid connection and tariff (step C) before activating", r.text
    assert r.json()["missing"] == [{"step": "c", "words": "the grid connection and tariff (step C)"}]
    assert a.get(f"/api/sites/{sid}").json()["lifecycle"]["words"] == "Draft: step C of 6"
    # an off-grid farm needs no grid-presence signal, and its data source's words say so
    farm = a.post("/api/sites", json={"name": "Farm K11", "site_type": "farm"}).json()["id"]
    r = a.post(f"/api/sites/{farm}/activate")
    assert r.json()["missing"][0] == {"step": "a", "words": "the data source (step A)"}, r.text


def test_the_warning_on_a_connection_priced_on_the_other_discos_windows_names_step_c(world):
    a = world.c["admin"]
    r = a.put(f"/api/sites/{world.site}", json={"disco": "K-Electric"})
    warning = next(w for w in r.json()["setup_warnings"] if "time-of-use windows" in w)
    assert warning.endswith("save the connection again with K-Electric's (step C)"), warning
    with world.use():
        assert not [w for w in sites.setup_warnings(sites.full(world.site)) if re.search(r"\btab\b|Site settings", w)]
