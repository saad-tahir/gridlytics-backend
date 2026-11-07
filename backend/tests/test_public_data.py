"""The non-circular check of the PV model with free public data (tracker VER-14, done with public data until a partner site's month exists).

The fixtures in tests/fixtures/public were fetched once by hand (`python -m gridlytics.public_data fetch`; SOURCE.md names every request, the date
fetched and the licence): one year (2020) of hourly PVGIS-ERA5 output and irradiance for a 100 kWp system at 15 degrees facing due south with 14 %
system loss, NASA POWER's hourly GHI over the same year, and PVGIS's 2005-2020 monthly climatology, at the four demo locations. No test calls the
network.

Errors when the round-4 fix landed (before -> after):
- driven by PVGIS's own GHI, the annual output: Faisalabad +2.6 -> -0.2 %, Lahore +2.4 -> -0.3 %, Karachi +0.3 -> -2.2 %, Rahim Yar Khan +2.0 -> -0.6 %.
  The glass's reflection loss by angle of incidence was missing (0 against PVGIS's 2.6 %); the model's is now 2.4 to 2.6 %.
- Stage 0 (clear sky times the month's clearness) against PVGIS's 2005-2020 mean: annual -7.4 / -5.7 / -10.6 / -11.9 % -> -0.7 / +1.2 / -3.6 / -5.2 %;
  the worst month -27.8 % (Karachi, December) -> -9.5 % (Rahim Yar Khan, January).
- The clear sky on the clearest fifth of each month's days: 28-48 % of hours more than 10 % above it -> at most 0.1 %.
- Stage 1's day-ahead median against same-hour persistence (hourly, daytime, nRMSE by nameplate): skill +0.12 / +0.17 / +0.02 / -0.24 ->
  +0.16 / +0.21 / +0.12 / -0.13.
"""
from __future__ import annotations

import ast
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from gridlytics import forecast, solar, store, telemetry

FIX = Path(__file__).parent / "fixtures" / "public"
#: the demo locations, as public_data.SITES names them (the fetch module is not imported here)
SITES = {"faisalabad": (31.42, 73.08), "lahore": (31.52, 74.36), "karachi": (24.86, 67.01), "rahim_yar_khan": (28.42, 70.30)}
#: the system asked of PVGIS: 100 kWp, 14 % system loss, 15 degrees tilt, due south; AC nameplate at DC so nothing clips
PV_SYSTEM = {"kwp_dc": 100.0, "kw_ac": 100.0, "tilt": 15.0, "azimuth": 180.0, "losses_pct": 14.0}
PKT = timezone(timedelta(hours=5))


def _read(name: str) -> dict:
    """A fixture CSV as arrays: numbers as floats (a blank cell is NaN), the `site` column as text."""
    with (FIX / name).open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    return {k: np.array([r[k] for r in rows]) if k == "site" else np.array([float(r[k]) if r[k] != "" else np.nan for r in rows])
            for k in rows[0]}


def _monthly(values: np.ndarray, months: np.ndarray) -> np.ndarray:
    return np.array([np.nansum(values[months == m]) for m in range(1, 13)])


def _pct(a, b):
    return 100.0 * (np.asarray(a, dtype=float) / np.asarray(b, dtype=float) - 1.0)


def _driven_by_ghi(key: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The 2020 hours of PVGIS's file with the clearness that gives the model PVGIS's own GHI at each hour's middle."""
    lat, lon = SITES[key]
    g = _read(f"pvgis_{key}_2020.csv")
    ts = g["ts"].astype(np.int64)
    months = solar.local_month(ts)
    zen, _ = solar.sun_position(ts, lat, lon)
    cs = solar.clear_sky_ghi(zen, months)
    clearness = np.clip(np.where(cs > 1.0, g["ghi"] / np.maximum(cs, 1e-6), 0.0), 0.0, 1.5)
    return ts, months, clearness, g["p_w"] / 1000.0, cs


@pytest.fixture()
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIDLYTICS_DATA_DIR", str(tmp_path))
    db = tmp_path / "companies" / "c.db"
    db.parent.mkdir(parents=True)
    with store.use_company(db):
        yield db


# ------------------------------------------------------------------------------------------------ the fixtures

def test_the_fixtures_name_their_sources_and_nothing_at_runtime_fetches():
    src = (FIX / "SOURCE.md").read_text(encoding="utf-8")
    for needle in ("PVGIS", "NASA POWER", "Fetched 20", "Decision 2011/833/EU", "Copernicus", "re.jrc.ec.europa.eu/api/v5_2/seriescalc",
                   "re.jrc.ec.europa.eu/api/v5_2/PVcalc", "power.larc.nasa.gov/api/temporal/hourly/point", "no restrictions"):
        assert needle in src, needle
    y0, y1 = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp(), datetime(2021, 1, 1, tzinfo=timezone.utc).timestamp()
    for key in SITES:
        assert f"`{key}`, pvgis" in src and f"`{key}`, power" in src and f"`{key}`, monthly" in src
        for name in (f"pvgis_{key}_2020.csv", f"power_{key}_2020.csv"):
            ts = _read(name)["ts"]
            assert len(ts) > 4300 and y0 <= ts.min() < y0 + 86400 and y1 - 86400 < ts.max() < y1, name  # a whole year of daylight hours
        assert _read(f"monthly_{key}.csv")["month"].tolist() == list(range(1, 13))
    assert sorted(_read("pvgis_totals.csv")["site"].tolist()) == sorted(SITES)
    # no runtime module imports the fetch module
    users = []
    for path in Path(solar.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, (ast.Import, ast.ImportFrom)) else []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            if any(n.split(".")[-1] == "public_data" for n in names):
                users.append(path.name)
    assert users == []


# ------------------------------------------------------------------------------------------------ the plant model: solar.pv_power

@pytest.mark.parametrize("key", list(SITES))
def test_pv_power_driven_by_pvgis_ghi_matches_pvgis_output(key):
    """Given PVGIS's own GHI hour by hour, the model's decomposition, isotropic transposition, reflection loss, climatological cell temperature and
    losses give the year within 4 % of PVGIS's output and every month within 6 %: the isotropic sky against PVGIS's anisotropic one and the
    Punjab air temperature against ERA5's each account for up to 2 %, and an hour is sampled at its middle."""
    lat, lon = SITES[key]
    ts, months, clearness, ref_kw, _ = _driven_by_ghi(key)
    model = _monthly(solar.pv_power(ts, lat, lon, PV_SYSTEM, clearness, months), months)
    ref = _monthly(ref_kw, months)
    err, annual = _pct(model, ref), float(_pct(model.sum(), ref.sum()))
    assert abs(annual) <= 4.0 and np.all(np.abs(err) <= 6.0), (key, round(annual, 1), np.round(err, 1).tolist())


@pytest.mark.parametrize("key", list(SITES))
def test_the_glass_reflection_loss_matches_pvgis_s(key, monkeypatch):
    """The light the module glass reflects at oblique incidence (Martin and Ruiz): over the year the model loses within half a point of what PVGIS
    reports for the same system and place (2.56-2.66 %). v1 had no reflection loss, so it ran about 2 % above PVGIS at three of the four sites."""
    lat, lon = SITES[key]
    ts, months, clearness, _, _ = _driven_by_ghi(key)
    with_loss = solar.pv_power(ts, lat, lon, PV_SYSTEM, clearness, months).sum()
    monkeypatch.setattr(solar, "iam", lambda cos_aoi: np.ones_like(np.asarray(cos_aoi, dtype=float)))
    without = solar.pv_power(ts, lat, lon, PV_SYSTEM, clearness, months).sum()
    tot = _read("pvgis_totals.csv")
    l_aoi = float(tot["l_aoi_pct"][tot["site"] == key][0])
    assert float(_pct(with_loss, without)) == pytest.approx(l_aoi, abs=0.5), (key, float(_pct(with_loss, without)), l_aoi)


@pytest.mark.parametrize("key", list(SITES))
def test_stage0_clear_sky_times_the_month_s_clearness_matches_the_pvgis_climatology(key, company):
    """Stage 0 is a clear-sky-times-clearness model: the clear-sky output of the month times its climatological clearness (forecast.MONTH_K, the stage-0
    median). Against PVGIS's 2005-2020 mean output it is within 10 % a year at each site and 12 % in each month: one national table serves sites
    seven degrees of latitude apart, where Punjab's winter fog (which NASA POWER sees and ERA5 does not) and Sindh's clear winters differ, and PVGIS's
    own year-to-year spread of a month is 2-11 %."""
    lat, lon = SITES[key]
    t0 = int(datetime(2021, 1, 1, tzinfo=PKT).timestamp())
    ts = np.arange(t0, t0 + 365 * 86400, 300, dtype=np.int64)
    months = solar.local_month(ts)
    clear = _monthly(solar.pv_power(ts, lat, lon, PV_SYSTEM, 1.0, months), months) * 300 / 3600
    model = clear * np.array([forecast.MONTH_K[m] for m in range(1, 13)])
    ref = _read(f"monthly_{key}.csv")["pvgis_e_m_kwh"]
    err, annual = _pct(model, ref), float(_pct(model.sum(), ref.sum()))
    assert abs(annual) <= 10.0 and np.all(np.abs(err) <= 12.0), (key, round(annual, 1), np.round(err, 1).tolist())
    # and the forecaster's stage 0 is that model: its median is the clear sky times the month's clearness
    site = {"id": f"s0-{key}", "data": {"lat": lat, "lon": lon}, "assets": {"pv": PV_SYSTEM}, "connections": []}
    f = forecast.pv(site, int(datetime(2026, 6, 15, tzinfo=PKT).timestamp()))
    assert f["stage"] == 0 and np.allclose(f["q"]["q50"], f["cs"] * forecast.MONTH_K[6])


# ------------------------------------------------------------------------------------------------ the clear sky: solar.clear_sky_ghi

@pytest.mark.parametrize("provider", ["pvgis", "power"])
@pytest.mark.parametrize("key", list(SITES))
def test_the_clear_sky_is_the_upper_envelope_of_the_clearest_days(key, provider):
    """On the clearest fifth of each month's days (sun above 15 degrees) the measured GHI is at most 2 % above the clear sky over the month, and under
    1 % of those hours are more than 10 % above it. v1's single haze factor (0.88) put 28-48 % of those hours more than 10 % above, so a forecast
    capped at clear sky could not reach a clear winter day's output. NASA POWER's own clear sky is never above the model's by more than 2 % in a
    month."""
    lat, lon = SITES[key]
    d = _read(f"{provider}_{key}_2020.csv")
    ts = d["ts"].astype(np.int64)
    months = solar.local_month(ts)
    day = (ts + 5 * 3600) // 86400
    zen, _ = solar.sun_position(ts, lat, lon)
    cs = solar.clear_sky_ghi(zen, months)
    up = (zen < 75) & ~np.isnan(d["ghi"])
    clear = np.zeros(len(ts), dtype=bool)
    month_ratio = []
    for m in range(1, 13):
        days = np.unique(day[up & (months == m)])
        r = np.array([d["ghi"][up & (day == x)].sum() / cs[up & (day == x)].sum() for x in days])
        top = up & (months == m) & np.isin(day, days[r >= np.quantile(r, 0.8)])
        clear |= top
        month_ratio.append(d["ghi"][top].sum() / cs[top].sum())
    over = float(np.mean(d["ghi"][clear] > 1.10 * cs[clear]))
    assert max(month_ratio) <= 1.02 and over < 0.01, (key, provider, np.round(month_ratio, 3).tolist(), round(over, 3))
    if provider == "power":
        ok = up & ~np.isnan(d["ghi_clear"])
        ratio = [cs[ok & (months == m)].sum() / d["ghi_clear"][ok & (months == m)].sum() for m in range(1, 13)]
        assert min(ratio) >= 0.98, (key, np.round(ratio, 3).tolist())


# ------------------------------------------------------------------------------------------------ the forecaster: forecast.pv stage 1

def _ingest_pvgis(site_id: str) -> dict:
    """PVGIS's 2020 hours as the site's PV telemetry, one row every 15 minutes (each hour's mean output held over its four quarters; night hours,
    left out of the file, as zero). Returns the hourly output (kW) by the hour's start."""
    g = _read(f"pvgis_{site_id}_2020.csv")
    hourly = {int(t) - 1800: float(p) / 1000.0 for t, p in zip(g["ts"], g["p_w"])}
    y0 = int(datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp())
    quarters = np.arange(y0, y0 + 366 * 86400, 900, dtype=np.int64)
    telemetry.write_rows([(site_id, int(q), "edge", 1000.0 * hourly.get(int(q) // 3600 * 3600, 0.0)) for q in quarters],
                         ("site_id", "ts", "source_id", "pv_power_w"))
    return {int(h): hourly.get(int(h), 0.0) for h in quarters[::4]}


def test_stage1_forecaster_on_public_pv_against_persistence(company):
    """A site whose telemetry is PVGIS's output, forecast day-ahead at local midnight on six days a month: every issue reaches stage 1, the hourly
    daytime nRMSE (by nameplate) of the median is under 8 % at every site, and across the four sites it beats same-hour persistence (mean skill at least
    0.05). Rahim Yar Khan stays below persistence (skill above -0.2): ERA5's aerosol is climatological, so a desert's clearness barely moves from one
    day to the next and yesterday is hard to beat on this data; a weather feed (FOR-02) is the remedy, not the clear-sky chain."""
    days = [int(datetime(2020, m, d, tzinfo=PKT).timestamp()) for m in range(1, 13) for d in (16, 18, 20, 22, 24, 26)]
    skills, report = [], {}
    for key, (lat, lon) in SITES.items():
        actual = _ingest_pvgis(key)
        site = {"id": key, "data": {"lat": lat, "lon": lon}, "assets": {"pv": PV_SYSTEM}, "connections": []}
        se_f = se_p = 0.0
        n = 0
        for t0 in days:
            f = forecast.pv(site, t0)
            assert f["stage"] == 1, (key, t0)
            hours = t0 + 3600 * np.arange(24)
            zen, _ = solar.sun_position(hours + 1800, lat, lon)
            ok = zen < 85
            a = np.array([actual[int(h)] for h in hours])
            p = np.array([actual[int(h) - 86400] for h in hours])
            q50 = f["q"]["q50"][:96].reshape(24, 4).mean(axis=1)
            se_f += float(np.sum((q50[ok] - a[ok]) ** 2))
            se_p += float(np.sum((p[ok] - a[ok]) ** 2))
            n += int(ok.sum())
        nf, npers = np.sqrt(se_f / n) / PV_SYSTEM["kw_ac"], np.sqrt(se_p / n) / PV_SYSTEM["kw_ac"]
        skills.append(1.0 - nf / npers)
        report[key] = (round(float(nf), 4), round(float(npers), 4), round(float(skills[-1]), 3))
    assert all(v[0] < 0.08 for v in report.values()), report
    assert min(skills) > -0.2 and float(np.mean(skills)) >= 0.05, report


def test_a_plant_better_than_the_nominal_model_keeps_its_clear_days(company):
    """A plant whose clear days run 8 % above the nominal model fits a ratio of 1.08; its forecast reaches 1.08 times the model's clear sky. v1 capped
    every quantile at the nominal clear sky, so such a plant's clear days were forecast 8 % short."""
    sid = "pv-above"
    pv_asset = {"kwp_dc": 100.0, "kw_ac": 120.0, "tilt": 15.0, "azimuth": 180.0}
    site = {"id": sid, "data": {"lat": 28.42, "lon": 70.30}, "assets": {"pv": pv_asset}, "connections": []}
    t0 = int(datetime(2026, 11, 16, tzinfo=PKT).timestamp())
    ts = np.arange(t0 - 14 * 86400, t0, 300, dtype=np.int64)
    pv = solar.pv_power(ts, 28.42, 70.30, pv_asset, 1.0, solar.local_month(ts)) * 1.08
    telemetry.write_rows([(sid, int(a), "edge", float(b) * 1000) for a, b in zip(ts, pv)], ("site_id", "ts", "source_id", "pv_power_w"))
    out = forecast.pv(site, t0)
    assert out["stage"] == 1 and out["pr"] == pytest.approx(1.08, abs=0.01)
    noon = 12 * 4  # 12:00 Pakistan time on the forecast day
    assert out["q"]["q50"][noon] == pytest.approx(1.08 * out["cs"][noon], rel=0.01)
    assert out["q"]["q95"][noon] == pytest.approx(1.08 * out["cs"][noon], rel=0.01)


def test_a_clipping_plant_with_a_ratio_above_one_is_never_forecast_above_its_inverter(company):
    """A 150 kWp / 100 kW plant that runs 8 % above the nominal model and clips at 100 kW: its fitted ratio is above 1, so the cap of ratio times the
    clear sky (which is itself already clipped at 100 kW) would let the fan reach about 104 kW at midday. No quantile may exceed the inverter's AC
    rating, while an unclipped morning step still reaches the ratio times the clear sky."""
    sid = "pv-clip"
    pv_asset = {"kwp_dc": 150.0, "kw_ac": 100.0, "tilt": 15.0, "azimuth": 180.0}
    site = {"id": sid, "data": {"lat": 28.42, "lon": 70.30}, "assets": {"pv": pv_asset}, "connections": []}
    t0 = int(datetime(2026, 3, 16, tzinfo=PKT).timestamp())
    ts = np.arange(t0 - 14 * 86400, t0, 300, dtype=np.int64)
    unbounded = solar.pv_power(ts, 28.42, 70.30, {**pv_asset, "kw_ac": 1e9}, 1.0, solar.local_month(ts))
    pv = np.minimum(100.0, 1.08 * unbounded)
    telemetry.write_rows([(sid, int(a), "edge", float(b) * 1000) for a, b in zip(ts, pv)], ("site_id", "ts", "source_id", "pv_power_w"))
    out = forecast.pv(site, t0)
    assert out["stage"] == 1 and out["pr"] > 1.0, out["pr"]
    for name, q in out["q"].items():
        assert float(np.max(q)) <= 100.0 + 1e-9, (name, float(np.max(q)))
    # a morning step whose clear sky times the ratio stays well under the AC rating
    morning = [i for i in range(24, 48) if 20.0 < out["cs"][i] * out["pr"] < 80.0]
    assert morning, out["cs"][24:48]
    i = morning[-1]
    assert out["q"]["q50"][i] == pytest.approx(out["pr"] * out["cs"][i], rel=0.02)
