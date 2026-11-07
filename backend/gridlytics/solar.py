"""Solar geometry, a clear-sky model and a PVWatts-style plant model in plain numpy (clean-room; plan Part 5.1 stage 0).

pvlib is the plan's library for the full chain; v1 ships this small model instead so the engine has no heavy dependency (DESIGN_LOG records it).
The sun position follows the NOAA spreadsheet's equations; the clear-sky irradiance is Haurwitz's, scaled per month for Pakistan's dust, haze and
humidity; the transposition is isotropic, with the module glass's reflection loss by angle of incidence (Martin and Ruiz, 2001, for beam, sky diffuse
and ground-reflected light at Brandemuehl and Beckman's effective angles); the cell temperature is the simple NOCT form.
Checked against free public data (PVGIS and NASA POWER, tests/test_public_data.py): driven by PVGIS's own GHI, a 100 kWp system's annual output is
within 2.5 % of PVGIS's at the four demo locations.
"""
from __future__ import annotations

import numpy as np

#: monthly mean air temperature in the Punjab plains (degrees C), and the diurnal half-swing; used where no weather feed exists
MONTH_TEMP_C = np.array([13.0, 16.0, 21.0, 27.0, 32.0, 34.0, 31.5, 30.5, 29.5, 25.5, 19.5, 14.5])
DIURNAL_HALF_SWING_C = 6.0
#: Haurwitz's clear sky scaled per calendar month (Pakistan time, January first) for the season's aerosol and water vapour. Each value is the upper
#: envelope of free public hourly GHI: the largest, over the four demo locations and two providers (PVGIS-ERA5 and NASA POWER, 2020;
#: tests/fixtures/public), of the GHI on the month's clearest fifth of days over Haurwitz's, rounded up to 0.01. The dry season's air (October to
#: February) is clearer than Haurwitz's standard atmosphere; the monsoon's (June to September) is wetter and dustier. v1 had one factor, 0.88, which put
#: the clear sky 15-20 % below the measured GHI of clear winter days, so a forecast capped at clear sky could never reach a clear winter day's output.
HAZE_BY_MONTH = np.array([1.06, 1.05, 1.02, 0.99, 0.96, 0.91, 0.87, 0.88, 0.93, 1.02, 1.03, 1.06])
#: the year-round factor, for a clear sky asked without a month (the monthly table's mean)
HAZE_FACTOR = float(np.round(HAZE_BY_MONTH.mean(), 3))


def sun_position(ts_utc: np.ndarray, lat: float, lon: float) -> tuple[np.ndarray, np.ndarray]:
    """Solar zenith and azimuth (degrees, azimuth clockwise from north) for epoch seconds UTC."""
    ts = np.asarray(ts_utc, dtype=float)
    days = ts / 86400.0
    year_frac = 2 * np.pi / 365.2422 * ((days % 365.2422) - 0.5)  # NOAA's fractional year; the epoch's phase error is under a day
    eqtime = 229.18 * (0.000075 + 0.001868 * np.cos(year_frac) - 0.032077 * np.sin(year_frac) - 0.014615 * np.cos(2 * year_frac)
                       - 0.040849 * np.sin(2 * year_frac))
    decl = (0.006918 - 0.399912 * np.cos(year_frac) + 0.070257 * np.sin(year_frac) - 0.006758 * np.cos(2 * year_frac)
            + 0.000907 * np.sin(2 * year_frac) - 0.002697 * np.cos(3 * year_frac) + 0.00148 * np.sin(3 * year_frac))
    minutes_utc = (ts % 86400) / 60.0
    true_solar = minutes_utc + eqtime + 4.0 * lon
    ha = np.radians(true_solar / 4.0 - 180.0)
    phi = np.radians(lat)
    cosz = np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(ha)
    cosz = np.clip(cosz, -1.0, 1.0)
    zen = np.degrees(np.arccos(cosz))
    sinz = np.sqrt(np.maximum(1e-9, 1 - cosz ** 2))
    cos_az = (np.sin(phi) * cosz - np.sin(decl)) / (np.cos(phi) * sinz)
    az = np.degrees(np.arccos(np.clip(cos_az, -1.0, 1.0)))
    az = np.where(ha > 0, (az + 180.0) % 360.0, (540.0 - az) % 360.0)
    return zen, az


def clear_sky_ghi(zenith_deg: np.ndarray, months: np.ndarray | int | None = None) -> np.ndarray:
    """Clear-sky GHI (W/m2) at a solar zenith: Haurwitz's, scaled by the month's haze factor (calendar months 1-12, Pakistan time), or by the
    year-round factor where no month is given."""
    cosz = np.cos(np.radians(zenith_deg))
    out = np.where(cosz > 0.01, 1098.0 * cosz * np.exp(-0.057 / np.maximum(cosz, 0.01)), 0.0)
    haze = HAZE_FACTOR if months is None else HAZE_BY_MONTH[np.asarray(months, dtype=int) - 1]
    return out * haze


def local_month(ts_utc) -> np.ndarray:
    """The calendar month (1-12) in Pakistan time of each epoch second."""
    d = (np.asarray(ts_utc, dtype=np.int64) + 5 * 3600).astype("datetime64[s]")
    return (d.astype("datetime64[M]").astype(np.int64) % 12 + 1).astype(int)


def air_temp(ts_utc: np.ndarray, month: np.ndarray | None = None) -> np.ndarray:
    """Climatological air temperature at each instant: the month's mean plus a diurnal swing peaking at 15:00 local."""
    ts = np.asarray(ts_utc, dtype=float)
    local_h = ((ts + 5 * 3600) % 86400) / 3600.0
    if month is None:
        month = local_month(ts) - 1
    return MONTH_TEMP_C[month] + DIURNAL_HALF_SWING_C * np.cos((local_h - 15.0) / 24.0 * 2 * np.pi)


#: Martin and Ruiz's angular loss coefficient for glass-covered crystalline silicon (the value PVGIS uses)
IAM_AR = 0.16


def iam(cos_aoi: np.ndarray) -> np.ndarray:
    """The share of light the module glass lets through at an angle of incidence, relative to normal incidence (Martin and Ruiz, 2001)."""
    return (1.0 - np.exp(-np.clip(cos_aoi, 0.0, 1.0) / IAM_AR)) / (1.0 - np.exp(-1.0 / IAM_AR))


def pv_power(ts_utc: np.ndarray, lat: float, lon: float, pv: dict, clearness: np.ndarray | float = 1.0, months: np.ndarray | None = None) -> np.ndarray:
    """AC power (kW) of a plant at each instant for a clearness index (1.0 = clear sky)."""
    zen, az = sun_position(ts_utc, lat, lon)
    months = local_month(ts_utc) if months is None else np.asarray(months)
    ghi = clear_sky_ghi(zen, months) * clearness
    kt = np.asarray(clearness, dtype=float) * np.ones_like(ghi)
    diffuse_share = np.clip(np.where(kt > 0.8, 0.15, 0.15 + (0.8 - kt) * 1.0), 0.15, 1.0)
    dhi = ghi * diffuse_share
    cosz = np.cos(np.radians(zen))
    dni = np.where(cosz > 0.05, (ghi - dhi) / np.maximum(cosz, 0.05), 0.0)
    tilt, paz = np.radians(pv.get("tilt", 15.0)), np.radians(pv.get("azimuth", 180.0))
    zr, ar = np.radians(zen), np.radians(az)
    cos_aoi = np.cos(zr) * np.cos(tilt) + np.sin(zr) * np.sin(tilt) * np.cos(ar - paz)
    beam = dni * np.maximum(cos_aoi, 0.0)
    sky = dhi * (1 + np.cos(tilt)) / 2
    ground = ghi * 0.2 * (1 - np.cos(tilt)) / 2
    poa = np.maximum(0.0, beam + sky + ground)
    # light lost to reflection off the glass: the beam at its own angle, the sky and the ground at their effective angles (degrees, by tilt)
    b = np.degrees(tilt)
    th_sky, th_ground = np.radians(59.7 - 0.1388 * b + 0.001497 * b * b), np.radians(90.0 - 0.5788 * b + 0.002693 * b * b)
    poa_eff = np.maximum(0.0, beam * iam(cos_aoi) + sky * iam(np.cos(th_sky)) + ground * iam(np.cos(th_ground)))
    t_cell = air_temp(ts_utc, months - 1) + poa * 0.03
    dc = pv["kwp_dc"] * poa_eff / 1000.0 * (1 + pv.get("gamma_pdc", -0.0035) * (t_cell - 25.0)) * (1 - pv.get("losses_pct", 14.0) / 100.0)
    return np.clip(dc * 0.97, 0.0, pv["kw_ac"])
