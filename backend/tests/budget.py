"""A deterministic work budget for the optimiser's tests (round 7, audit tests-and-perf-12).

The optimiser stops each solve at a wall-clock limit (the tick's 10 s), so whether a solve finishes inside it, and how good its incumbent is when
it stops, depends on the machine's speed and load: a test that asserted "optimal inside 10 s" or "within 0.5 % of its bound at the limit" passed on
an idle machine and failed at random under `pytest -n 4`, on a slower CI runner or beside a demo seed (HANDOFF trap 6). Under `use`, every solve
of the test is given a node budget instead (HiGHS's node_limit, which scipy's milp takes) and a wall-clock limit far beyond any solve's (a safety
stop only): the solver does the same work, and so returns the same plan, whatever else runs on the machine. A solve that spends its budget is
reported to the optimiser exactly as one that spent its time (scipy's status 1 with its incumbent: `time_limited`, the threshold repair and the
shed-rule re-solve follow as they do in the tick), so the tests read the product's own handling of a solve that stopped at its limit.

What a node budget does not bound is the work at the root node, which is most of these 192-step solves' time (the 120 L farm: about 6 s of root and
178 nodes in the tick's 10 s, on an idle machine). Each solve's CPU time on this process's own clock is recorded (`Budget.solves`), which the
machine's other work does not stretch as it stretches the wall clock, and a test holds it under CPU_SECONDS, three times the tick's limit: a change
that makes the root an order of magnitude slower still fails the default suite. The wall-clock claims themselves (inside the tick's 10 s, within
1.8 times the model without the surplus) are the opt-in benchmarks' (the `slow` marker, conftest.py), run alone on an idle machine (HANDOFF section 9).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from gridlytics import optimise

#: The tick's wall-clock limit of a solve (plans.tick, scheduler)
TICK_SECONDS = 10.0
#: The nodes a test's solve may explore: about what the tick's 10 s explore on an idle machine for the 192-step farm (178 at 120 L)
TICK_NODES = 200
#: The wall-clock stop given with a budget: never reached by a solve that keeps to its budget, only by one that hangs
SAFETY_SECONDS = 600.0
#: A solve's CPU time on this process's clock: three times the tick's limit (an idle machine spends 0.1 to 8 s here)
CPU_SECONDS = 3 * TICK_SECONDS


@dataclass
class Budget:
    nodes: int
    #: each solve of the test, in order: the limit the optimiser gave it (`limit_s`), scipy's status as the optimiser read it, the nodes explored,
    #: whether it spent its node budget, and its CPU and wall-clock seconds (for a failure's message; the wall clock is never asserted here)
    solves: list[dict] = field(default_factory=list)

    def cpu_ok(self) -> bool:
        return all(s["cpu_s"] < CPU_SECONDS for s in self.solves)


def use(monkeypatch, nodes: int = TICK_NODES) -> Budget:
    """Give every solve of the test `nodes` branch-and-bound nodes instead of its wall-clock limit (see the module's words)."""
    budget, real = Budget(nodes), optimise.milp

    def milp(*args, **kwargs):
        options = dict(kwargs.get("options") or {})
        asked = options.get("time_limit")
        options.update(time_limit=SAFETY_SECONDS, node_limit=int(nodes))
        kwargs["options"] = options
        cpu0, wall0 = time.process_time(), time.perf_counter()
        res = real(*args, **kwargs)
        cpu, wall = time.process_time() - cpu0, time.perf_counter() - wall0
        # scipy reports HiGHS's node limit as status 4 ("Solution limit reached"); the optimiser reads a limit as status 1, the time limit's
        spent = int(res.status) == 4 and res.x is not None and "limit" in str(res.message).lower()
        if spent:
            res.status, res.message = 1, f"Node limit reached ({nodes} nodes, the tests' work budget)"
        budget.solves.append({"limit_s": asked, "status": int(res.status), "nodes": int(getattr(res, "mip_node_count", 0) or 0), "spent": spent,
                              "cpu_s": round(cpu, 3), "wall_s": round(wall, 3)})
        return res

    monkeypatch.setattr(optimise, "milp", milp)
    return budget
