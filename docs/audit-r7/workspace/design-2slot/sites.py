import sys, json
sys.path.insert(0, "/tmp/claude-0/-home-user/f64857cc-0ca7-5cbb-b665-d2b474fb2fb8/scratchpad/audit")
from api import client
c = client("owner")
for sid in ("6d4db49bf87e", "e9941b41c70b", "37b2fd882dcb", "47895916bed4"):
    s = c.get(f"/api/sites/{sid}").json()
    site = s.get("site", s)
    inv = (site.get("assets") or {}).get("inverter") or {}
    bat = (site.get("assets") or {}).get("battery") or {}
    conn = (site.get("connections") or [{}])[0]
    print(sid, site.get("name"), "| brand", inv.get("brand"), "| tier", site.get("tier"), "| bat", bat.get("nameplate_kwh"), bat.get("p_ch_kw"), bat.get("p_dis_kw"),
          "| regime", (conn.get("data") or {}).get("regime"), (conn.get("data") or {}).get("tariff_category"))
    p = c.get(f"/api/sites/{sid}/plan").json().get("plan")
    if not p:
        print("  no plan in force"); continue
    sl = p.get("slots") or {}
    print("  plan", p["id"], "steps", len(p["steps"]), "status", p["status"], "guard", p["summary"].get("guard"), "obj", p["summary"].get("objective_rs"), "mdi_cap", p["summary"].get("mdi_cap_kw"))
    for d in sl.get("days", []):
        print("  ", d["date"], [(x["start"], x["end"], x["mode"], x["soc_floor_pct"], x["soc_target_pct"]) for x in d["slots"]])
    h = c.get(f"/api/sites/{sid}/plans?limit=200").json()
    hist = h.get("plans", h) if isinstance(h, dict) else h
    print("  history rows", len(hist), "statuses", {st: sum(1 for x in hist if x["status"] == st) for st in set(x["status"] for x in hist)})
