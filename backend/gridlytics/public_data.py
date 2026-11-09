"""Free public PV and weather data for the non-circular check of the PV model (tracker VER-14, done with public data until a partner site's month
exists), fetched by hand only:

    python -m gridlytics.public_data fetch [--out DIR] [--year 2020]

It writes, per demo location, one year of hourly PVGIS and NASA POWER values and one file of monthly climatology, plus SOURCE.md, into
backend/tests/fixtures/public/ by default. Nothing at runtime imports this module, and no test calls it: the tests read the saved CSV files
(tests/test_public_data.py), so they never touch the network.

- PVGIS 5.2 (European Commission, Joint Research Centre), `seriescalc`: hourly PV output of a fixed free-standing c-Si system, with its in-plane
  irradiance, the sun height and the 2 m air temperature; asked a second time on a horizontal plane for the global and diffuse horizontal irradiance
  the same output was computed from. For Pakistan the radiation database is PVGIS-ERA5 (the reanalysis).
- NASA POWER (NASA Langley Research Center), hourly point API: all-sky and clear-sky global horizontal irradiance and the 2 m air temperature, from the
  satellite-based CERES/MERRA-2 chain, an independent source of irradiance.
- The monthly climatology: PVGIS `PVcalc` (the same system's mean monthly output over 2005-2020 with its year-to-year standard deviation, and the
  loss breakdown), PVGIS `MRcalc` (horizontal irradiation of every month 2005-2020) and the NASA POWER climatology API (mean daily GHI per month).
  A climatological model (the forecaster's stage 0) is compared with these, not with one year's weather.

Every time is UTC, written as epoch seconds at the hour's middle. PVGIS-ERA5 stamps an hour HH:30 already (its sun height matches the sun at that
instant); NASA POWER stamps it HH:00, its start, so its rows are moved by `TS_OFFSET_S["power"]`. Checked on the fetched year: the correlation of
either provider's GHI with the clear-sky model peaks with no further shift. Night hours are left out.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

#: the four demo locations (demo.DEMO_SITES), named as the fixture files are
SITES = {
    "faisalabad": (31.42, 73.08),
    "lahore": (31.52, 74.36),
    "karachi": (24.86, 67.01),
    "rahim_yar_khan": (28.42, 70.30),
}
YEAR = 2020  # the last year PVGIS 5.2's ERA5 series covers (2005-2020)
PVGIS_API = "https://re.jrc.ec.europa.eu/api/v5_2/"
PVGIS_URL = PVGIS_API + "seriescalc"
POWER_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
POWER_CLIMATOLOGY_URL = "https://power.larc.nasa.gov/api/temporal/climatology/point"
#: the PV system asked of PVGIS: 100 kWp, 14 % system loss, 15 degrees tilt, facing due south (PVGIS aspect 0 = south; azimuth 180 in solar.py)
PV_REQUEST = {"peakpower": 100, "loss": 14, "angle": 15, "aspect": 0}
POWER_PARAMETERS = ("ALLSKY_SFC_SW_DWN", "CLRSKY_SFC_SW_DWN", "T2M")
#: seconds from an hour's start to the time a fixture row carries (the hour's middle)
TS_OFFSET_S = {"pvgis": 1800, "power": 1800}
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "public"

PVGIS_COLUMNS = ("ts", "p_w", "poa", "ghi", "dhi", "h_sun", "t2m")
POWER_COLUMNS = ("ts", "ghi", "ghi_clear", "t2m")
TOTALS_COLUMNS = ("site", "e_y_kwh", "sd_y_kwh", "l_aoi_pct", "l_spec_pct", "l_tg_pct", "l_total_pct")
MONTHLY_COLUMNS = ("month", "pvgis_e_m_kwh", "pvgis_sd_m_kwh", "pvgis_h_i_m", "pvgis_ghi_m", "power_ghi_m")
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
_DAYS = (31, 28.25, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _get(url: str, params: dict) -> tuple[str, dict]:
    """GET a JSON document; returns the full request URL (for SOURCE.md) and the parsed body."""
    import httpx  # only here: the module is run by hand

    r = httpx.get(url, params=params, timeout=120.0, follow_redirects=True)
    r.raise_for_status()
    return str(r.url), r.json()


def pvgis_params(lat: float, lon: float, year: int, horizontal: bool = False) -> dict:
    p = {"lat": lat, "lon": lon, "startyear": year, "endyear": year, "components": 1, "outputformat": "json", "usehorizon": 1}
    if horizontal:
        p.update({"pvcalculation": 0, "angle": 0, "aspect": 0})
    else:
        p.update({"pvcalculation": 1, "pvtechchoice": "crystSi", "mountingplace": "free", **PV_REQUEST})
    return p


def _pvgis_ts(stamp: str) -> int:
    """PVGIS's 'YYYYMMDD:HHMM' (UTC) as epoch seconds."""
    return int(datetime.strptime(stamp, "%Y%m%d:%H%M").replace(tzinfo=timezone.utc).timestamp())


def fetch_pvgis(lat: float, lon: float, year: int = YEAR) -> tuple[list[dict], list[str], dict]:
    """One year of PVGIS hourly rows: the tilted system's output and in-plane irradiance, with the horizontal global and diffuse irradiance."""
    url_t, tilted = _get(PVGIS_URL, pvgis_params(lat, lon, year))
    url_h, flat = _get(PVGIS_URL, pvgis_params(lat, lon, year, horizontal=True))
    hz = {h["time"]: h for h in flat["outputs"]["hourly"]}
    rows = []
    for h in tilted["outputs"]["hourly"]:
        f = hz[h["time"]]
        ghi = f["Gb(i)"] + f["Gd(i)"] + f["Gr(i)"]
        poa = h["Gb(i)"] + h["Gd(i)"] + h["Gr(i)"]
        if h["H_sun"] <= 0 and h["P"] == 0 and poa == 0 and ghi == 0:
            continue  # a night hour: left out, read as zero
        rows.append({"ts": _pvgis_ts(h["time"]), "p_w": round(h["P"]), "poa": round(poa, 2), "ghi": round(ghi, 2), "dhi": f["Gd(i)"],
                     "h_sun": h["H_sun"], "t2m": h["T2m"]})
    return rows, [url_t, url_h], tilted["inputs"]


def fetch_power(lat: float, lon: float, year: int = YEAR) -> tuple[list[dict], list[str], dict]:
    """One year of NASA POWER hourly rows (UTC): all-sky and clear-sky GHI (W/m2, the hour's mean) and the 2 m air temperature."""
    params = {"parameters": ",".join(POWER_PARAMETERS), "community": "RE", "latitude": lat, "longitude": lon, "start": f"{year}0101",
              "end": f"{year}1231", "format": "JSON", "time-standard": "UTC"}
    url, body = _get(POWER_URL, params)
    par = body["properties"]["parameter"]
    fill = body.get("header", {}).get("fill_value", -999.0)
    rows = []
    for stamp in sorted(par["ALLSKY_SFC_SW_DWN"]):
        start = int(datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=timezone.utc).timestamp())

        def v(name):
            x = par[name].get(stamp)
            return "" if x is None or x == fill or x <= -998 else x

        if v("ALLSKY_SFC_SW_DWN") == 0 and v("CLRSKY_SFC_SW_DWN") == 0:
            continue  # a night hour: left out, read as zero
        rows.append({"ts": start + TS_OFFSET_S["power"], "ghi": v("ALLSKY_SFC_SW_DWN"), "ghi_clear": v("CLRSKY_SFC_SW_DWN"), "t2m": v("T2M")})
    return rows, [url], {"header": body.get("header", {}), "geometry": body.get("geometry", {})}


def fetch_monthly(lat: float, lon: float) -> tuple[list[dict], list[str], dict]:
    """The monthly climatology of a location: PVGIS's mean monthly output of the PV_REQUEST system over 2005-2020 (kWh) with its year-to-year standard
    deviation and in-plane irradiation (kWh/m2), the mean monthly horizontal irradiation over the same years (kWh/m2), and NASA POWER's mean monthly
    GHI (its mean daily value times the month's mean length, kWh/m2)."""
    url_pv, pvc = _get(PVGIS_API + "PVcalc", {"lat": lat, "lon": lon, "outputformat": "json", "usehorizon": 1, "pvtechchoice": "crystSi",
                                              "mountingplace": "free", **PV_REQUEST})
    url_mr, mr = _get(PVGIS_API + "MRcalc", {"lat": lat, "lon": lon, "horirrad": 1, "outputformat": "json", "startyear": 2005, "endyear": 2020})
    url_cl, cl = _get(POWER_CLIMATOLOGY_URL, {"parameters": "ALLSKY_SFC_SW_DWN", "community": "RE", "latitude": lat, "longitude": lon,
                                              "format": "JSON"})
    hh: dict[int, list[float]] = {}
    for r in mr["outputs"]["monthly"]:
        hh.setdefault(int(r["month"]), []).append(float(r["H(h)_m"]))
    ghi_day = cl["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"]
    rows = []
    for r in pvc["outputs"]["monthly"]["fixed"]:
        m = int(r["month"])
        rows.append({"month": m, "pvgis_e_m_kwh": r["E_m"], "pvgis_sd_m_kwh": r["SD_m"], "pvgis_h_i_m": r["H(i)_m"],
                     "pvgis_ghi_m": round(sum(hh[m]) / len(hh[m]), 2), "power_ghi_m": round(ghi_day[_MONTHS[m - 1]] * _DAYS[m - 1], 2)})
    info = {"totals": pvc["outputs"]["totals"]["fixed"], "pvgis_years": f"{pvc['inputs']['meteo_data'].get('year_min')}-"
            f"{pvc['inputs']['meteo_data'].get('year_max')}", "power_range": cl.get("header", {}).get("range")}
    return rows, [url_pv, url_mr, url_cl], info


def _write(path: Path, columns: tuple, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def fetch_all(out: Path = DEFAULT_OUT, year: int = YEAR) -> dict:
    """Fetch both providers for every demo location into `out`, with SOURCE.md naming each request, the date fetched and the licence."""
    out.mkdir(parents=True, exist_ok=True)
    fetched = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    notes, totals = [], []
    for key, (lat, lon) in SITES.items():
        rows, urls, inputs = fetch_pvgis(lat, lon, year)
        _write(out / f"pvgis_{key}_{year}.csv", PVGIS_COLUMNS, rows)
        meteo = inputs.get("meteo_data", {})
        notes.append((key, "pvgis", urls, f"{len(rows)} rows; radiation database {meteo.get('radiation_db')}, elevation "
                                           f"{inputs.get('location', {}).get('elevation')} m"))
        rows, urls, _ = fetch_power(lat, lon, year)
        _write(out / f"power_{key}_{year}.csv", POWER_COLUMNS, rows)
        notes.append((key, "power", urls, f"{len(rows)} rows"))
        rows, urls, info = fetch_monthly(lat, lon)
        _write(out / f"monthly_{key}.csv", MONTHLY_COLUMNS, rows)
        t = info["totals"]
        totals.append({"site": key, "e_y_kwh": t.get("E_y"), "sd_y_kwh": t.get("SD_y"), "l_aoi_pct": t.get("l_aoi"), "l_spec_pct": t.get("l_spec"),
                       "l_tg_pct": t.get("l_tg"), "l_total_pct": t.get("l_total")})
        notes.append((key, "monthly", urls, f"PVGIS years {info['pvgis_years']}, E_y {t.get('E_y')} kWh, SD_y {t.get('SD_y')} kWh, losses: "
                                            f"angle of incidence {t.get('l_aoi')} %, spectral {t.get('l_spec')} %, temperature and low irradiance "
                                            f"{t.get('l_tg')} %, total {t.get('l_total')} %; NASA POWER climatology {info['power_range']}"))
    _write(out / "pvgis_totals.csv", TOTALS_COLUMNS, totals)
    (out / "SOURCE.md").write_text(_source_md(notes, fetched, year), encoding="utf-8", newline="\n")
    return {"out": str(out), "files": len(notes)}


def _source_md(notes: list, fetched: str, year: int) -> str:
    lines = [
        "# Public data fixtures: sources",
        "",
        f"Fetched {fetched} by `python -m gridlytics.public_data fetch` (backend/gridlytics/public_data.py). The tests read these files and never call",
        "the network. One year (" + str(year) + ") of hourly values per demo location, and each location's monthly climatology.",
        "",
        "| File | Columns |",
        "|---|---|",
        "| `pvgis_<site>_" + str(year) + ".csv` | `ts` (epoch s UTC, the hour's middle), `p_w` (AC output of the 100 kWp system, W), `poa` "
        "(in-plane irradiance: beam, diffuse and reflected, W/m2), `ghi`, `dhi` (horizontal global and diffuse, W/m2), `h_sun` (sun height, degrees), "
        "`t2m` (air temperature, C) |",
        "| `power_<site>_" + str(year) + ".csv` | `ts` (epoch s UTC, the hour's middle), `ghi` (all-sky GHI, W/m2), `ghi_clear` (clear-sky GHI, W/m2), "
        "`t2m` (air temperature, C); a blank cell is missing |",
        "| `monthly_<site>.csv` | `month`, `pvgis_e_m_kwh` and `pvgis_sd_m_kwh` (the 100 kWp system's mean monthly output over PVGIS's years and its "
        "year-to-year standard deviation), `pvgis_h_i_m` (in-plane irradiation, kWh/m2), `pvgis_ghi_m` (mean monthly horizontal irradiation, kWh/m2), "
        "`power_ghi_m` (NASA POWER's mean daily GHI times the month's mean length, kWh/m2) |",
        "| `pvgis_totals.csv` | per site: `e_y_kwh`, `sd_y_kwh` (the system's mean yearly output and its year-to-year standard deviation), and PVGIS's "
        "losses in percent: `l_aoi_pct` (angle of incidence), `l_spec_pct` (spectral), `l_tg_pct` (temperature and low irradiance), `l_total_pct` |",
        "",
        "Night hours (no sun and no irradiance) are left out of the hourly files: read a missing hour as zero.",
        "",
        "## Providers and licences",
        "",
        "- **PVGIS 5.2**, European Commission, Joint Research Centre (https://re.jrc.ec.europa.eu/pvg_tools/). Free reuse with attribution under the",
        "  Commission's reuse policy (Decision 2011/833/EU): \"Source: PVGIS (c) European Union, 2001-2024\". The radiation database for these",
        "  locations is PVGIS-ERA5, which contains modified Copernicus Climate Change Service information. System asked: 100 kWp crystalline",
        "  silicon, free-standing, 15 degrees tilt, facing due south (aspect 0), 14 % system loss, calculated horizon.",
        "- **NASA POWER** hourly point API (https://power.larc.nasa.gov/). Free, no restrictions; acknowledgement asked: \"These data were obtained",
        "  from the NASA Langley Research Center (LaRC) POWER Project funded through the NASA Earth Science/Applied Science Program.\"",
        "",
        "## Requests",
        "",
    ]
    for key, provider, urls, what in notes:
        lines.append(f"- `{key}`, {provider}: {what}")
        for u in urls:
            lines.append(f"  - <{u}>")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gridlytics.public_data")
    ap.add_argument("command", choices=["fetch"])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--year", type=int, default=YEAR)
    a = ap.parse_args(argv)
    print(json.dumps(fetch_all(a.out, a.year)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
