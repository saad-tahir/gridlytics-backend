"""Demo sites (plan Part 9.6): load the four twin sites into the current company, watch the build, remove them."""
from __future__ import annotations

from fastapi import APIRouter

from . import auth, demo

router = APIRouter()


@router.get("/api/demo")
def status():
    st = demo.status()
    st.pop("trace", None)
    return {**st, "sites": [{"id": s["id"], "name": s["name"]} for s in demo.demo_sites()]}


@router.post("/api/demo")
def load():
    auth.need("edit_sites", "Loading the demo sites")
    st = demo.load(auth.actor())
    st.pop("trace", None)
    return st


@router.delete("/api/demo")
def remove():
    auth.need("edit_workspace", "Removing the demo sites")
    return {"removed": demo.remove(auth.actor())}
