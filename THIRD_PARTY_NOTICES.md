# Third-party notices

## Code lifted from the owner's own repositories

- `saad-tahir/solarlytics-backend` at `c853130`: the sign-in, tenancy and founder-console modules listed in `docs/DESIGN_LOG.md`, and their tests. Same
  owner; no licence obligation.

## Constructions followed clean-room (no code copied)

| Source | Licence | What was followed |
|---|---|---|
| evcc-io/optimizer | MIT | The structure of the optimiser module, the result semantics (optimal, feasible, not solved), the idea of an explanation per step |
| EMHASS | MIT | Define once, update parameters; the single-recovery idea for a state of charge outside the band; the perfect-foresight bound |
| REopt.jl (NREL) | BSD-3 / Apache-2.0 | Genset unit commitment with minimum up and down times, the fuel curve intercept, the demand-charge epigraph (with R3's corrections) |
| HOMER's fuel-curve convention | published method | `fuel = a * P + b * S * u` |
| BLAST-Lite (NREL) | BSD-3 | The LFP calendar term's temperature slope used for the calendar holding cost |
| NOAA solar calculator | public domain | Sun position equations |
| Haurwitz (1945) | published method | Clear-sky irradiance |
| kellerza/sunsynk, wills106, ha-solarman, mkaiser, mpp-solar | Apache-2.0 / MIT | Facts about brand slot grammars (slot counts, fields), written as data |

Not used anywhere: Predbat (GPL), the AGPL Huawei library, python-microgrid in the product (LGPL; verification only, later).

## Runtime dependencies

FastAPI, Starlette, Pydantic, Uvicorn (MIT/BSD); NumPy, SciPy with HiGHS (BSD/MIT); pandas (BSD); PyJWT (MIT); cryptography (Apache-2.0/BSD);
APScheduler (MIT); ReportLab (BSD); httpx (BSD).
