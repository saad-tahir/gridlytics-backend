"""Demo sites (plan Part 9.6): load the four twin sites into the current company, watch the build, remove them. Loading and removing are for an
administrator (edit_workspace); every member reads the build's state."""
from __future__ import annotations

from fastapi import APIRouter

from . import auth, demo, store

router = APIRouter()


@router.get("/api/demo")
def status():
    """The build's state, and `days`: the days of history it makes, which the pages name (audit pages-A-04: they promised 90). The build's own; for a
    build recorded before its days were kept, its site-days per demo site; with none, what a load would build now."""
    st = demo.status()
    st.pop("trace", None)
    days = st.get("days") or (int(st["total"]) // max(1, len(demo.DEMO_SITES)) if st.get("total") else demo.default_days())
    return {**st, "days": days, "sites": [{"id": s["id"], "name": s["name"]} for s in demo.demo_sites()]}


@router.post("/api/demo")
def load():
    # plan Part 9.6: the demo sites are "made by an admin only" (audit role-operator-07: an operator loaded them through edit_sites)
    auth.need("edit_workspace", "Loading the demo sites")
    st = demo.load(auth.actor())
    st.pop("trace", None)
    # the build runs on: each site it adds is audited as it is made, and the request itself here (round 7, audit security-roles-04)
    store.audit(auth.actor(), "loaded the demo sites", "", {"state": st.get("state")})
    return st


@router.delete("/api/demo")
def remove():
    auth.need("edit_workspace", "Removing the demo sites")
    n = demo.remove(auth.actor())
    store.audit(auth.actor(), "removed the demo sites", "", {"sites": n})
    return {"removed": n}
