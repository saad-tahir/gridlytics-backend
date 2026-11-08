# The original requirements and the build plan

Everything Gridlytics was planned and built from, copied into the repository on 8 November 2026 so a developer has it with the code. Paths inside
these files that start `C:\Users\SaadTahir\Videos\solarytics\` refer to the owner's machine. The same files are here: `EMS_BUILD_*.md` are in
`build-plan/`, and `plan-work/research` and `plan-work/review` are in `research/` and `review/`.

## Read in this order

1. **`requirements/Product 1 - TOU-Aware Energy Management and Dispatch`** (`.md`, with the original `.pdf` and `.docx`): the product brief this
   build implements. It covers the problem, the market, and what the product must do. `product1-architecture.png` is its architecture picture.
2. **`requirements/Perfect-EMS_TOU_Dispatch_Deep_Review.md`** (the original as `EMS_TOU_Dispatch_Deep_Review.pdf` and `.docx`): the deep review of
   Product 1, with its corrections and risks.
3. **`build-plan/EMS_BUILD_PLAN.md`**: the full specification the code follows (Parts 1-16). Code comments cite it as "Part 4.5" and the like:
   - optimiser: Part 4;
   - forecasting: Part 5;
   - tariffs: Part 6;
   - delivery and twin: Part 7;
   - savings: Part 8;
   - pages: Part 9;
   - backend and frontend: Parts 10-11;
   - build order: Part 12.
4. **`build-plan/EMS_BUILD_PROGRESS.md`**: the tracker. Every item has an acceptance test and a status:
   - `[x]` done;
   - `[~]` built in a v1 form short of its full acceptance;
   - `[ ]` not started;
   - `[owner]` an owner task;
   - `[later]` after v1.

   Its session log at the top says what each work session did.
5. **`requirements/Product 2 - Vendor-Agnostic Inverter and Battery Controller`** (with its deep review): the companion product, the on-site edge
   controller. Gridlytics talks to it through the ems-edge/1.0 contract (`delivery.py`, `routes_edge.py`; plan Part 7.3).

## The rest

| Folder or file | What it is |
|---|---|
| `build-plan/EMS_BUILD_HANDOFF.md`, `build-plan/START_HERE.md` | The planning run's handoff to the building agent. It is out of date on state: the build is done. Today's handoff is `HANDOFF.md` at the repository root. |
| `build-plan/PLANNING_STATUS.md` | The planning run's own status note. |
| `research/R1-R9` | Research the plan cites: R1 evcc optimizer, R2 EMHASS, R3 REopt formulations, R4 reference architectures, R5 forecasting, R6 executor and battery, R7 platform stack, R8 UX of the pages, R9 Pakistan tariffs. `R1-target-milp-prototype.py` is the prototype MILP. |
| `research/U1-U3` | Audits of the owner's earlier products (Boxlytics frontend; the Solarlytics and Boxlytics backends; the Product 2 interface), from which the sign-in, tenancy and founder console were lifted. |
| `research/_journal_results.md` | The research run's raw results journal. |
| `review/*-lens.md` | The four reviews the plan passed before the build (build, engine, market, and UX, licence and security). |

## How the code differs from the plan

Every deliberate difference, and every rule settled during the build and its review rounds, is in `docs/DESIGN_LOG.md`, newest first. Where the
plan and the DESIGN_LOG disagree, the DESIGN_LOG is current.
