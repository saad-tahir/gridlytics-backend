# Public data fixtures: sources

Fetched 2026-11-07 by `python -m gridlytics.public_data fetch` (backend/gridlytics/public_data.py). The tests read these files and never call
the network. One year (2020) of hourly values per demo location, and each location's monthly climatology.

| File | Columns |
|---|---|
| `pvgis_<site>_2020.csv` | `ts` (epoch s UTC, the hour's middle), `p_w` (AC output of the 100 kWp system, W), `poa` (in-plane irradiance: beam, diffuse and reflected, W/m2), `ghi`, `dhi` (horizontal global and diffuse, W/m2), `h_sun` (sun height, degrees), `t2m` (air temperature, C) |
| `power_<site>_2020.csv` | `ts` (epoch s UTC, the hour's middle), `ghi` (all-sky GHI, W/m2), `ghi_clear` (clear-sky GHI, W/m2), `t2m` (air temperature, C); a blank cell is missing |
| `monthly_<site>.csv` | `month`, `pvgis_e_m_kwh` and `pvgis_sd_m_kwh` (the 100 kWp system's mean monthly output over PVGIS's years and its year-to-year standard deviation), `pvgis_h_i_m` (in-plane irradiation, kWh/m2), `pvgis_ghi_m` (mean monthly horizontal irradiation, kWh/m2), `power_ghi_m` (NASA POWER's mean daily GHI times the month's mean length, kWh/m2) |
| `pvgis_totals.csv` | per site: `e_y_kwh`, `sd_y_kwh` (the system's mean yearly output and its year-to-year standard deviation), and PVGIS's losses in percent: `l_aoi_pct` (angle of incidence), `l_spec_pct` (spectral), `l_tg_pct` (temperature and low irradiance), `l_total_pct` |

Night hours (no sun and no irradiance) are left out of the hourly files: read a missing hour as zero.

## Providers and licences

- **PVGIS 5.2**, European Commission, Joint Research Centre (https://re.jrc.ec.europa.eu/pvg_tools/). Free reuse with attribution under the
  Commission's reuse policy (Decision 2011/833/EU): "Source: PVGIS (c) European Union, 2001-2024". The radiation database for these
  locations is PVGIS-ERA5, which contains modified Copernicus Climate Change Service information. System asked: 100 kWp crystalline
  silicon, free-standing, 15 degrees tilt, facing due south (aspect 0), 14 % system loss, calculated horizon.
- **NASA POWER** hourly point API (https://power.larc.nasa.gov/). Free, no restrictions; acknowledgement asked: "These data were obtained
  from the NASA Langley Research Center (LaRC) POWER Project funded through the NASA Earth Science/Applied Science Program."

## Requests

- `faisalabad`, pvgis: 4408 rows; radiation database PVGIS-ERA5, elevation 190.0 m
  - <https://re.jrc.ec.europa.eu/api/v5_2/seriescalc?lat=31.42&lon=73.08&startyear=2020&endyear=2020&components=1&outputformat=json&usehorizon=1&pvcalculation=1&pvtechchoice=crystSi&mountingplace=free&peakpower=100&loss=14&angle=15&aspect=0>
  - <https://re.jrc.ec.europa.eu/api/v5_2/seriescalc?lat=31.42&lon=73.08&startyear=2020&endyear=2020&components=1&outputformat=json&usehorizon=1&pvcalculation=0&angle=0&aspect=0>
- `faisalabad`, power: 4510 rows
  - <https://power.larc.nasa.gov/api/temporal/hourly/point?parameters=ALLSKY_SFC_SW_DWN%2CCLRSKY_SFC_SW_DWN%2CT2M&community=RE&latitude=31.42&longitude=73.08&start=20200101&end=20201231&format=JSON&time-standard=UTC>
- `faisalabad`, monthly: PVGIS years 2005-2020, E_y 157353.09 kWh, SD_y 2754.97 kWh, losses: angle of incidence -2.66 %, spectral 0.42 %, temperature and low irradiance -11.77 %, total -25.83 %; NASA POWER climatology 25-year Meteorological and Solar Monthly & Annual Climatologies (January 2001 - December 2025)
  - <https://re.jrc.ec.europa.eu/api/v5_2/PVcalc?lat=31.42&lon=73.08&outputformat=json&usehorizon=1&pvtechchoice=crystSi&mountingplace=free&peakpower=100&loss=14&angle=15&aspect=0>
  - <https://re.jrc.ec.europa.eu/api/v5_2/MRcalc?lat=31.42&lon=73.08&horirrad=1&outputformat=json&startyear=2005&endyear=2020>
  - <https://power.larc.nasa.gov/api/temporal/climatology/point?parameters=ALLSKY_SFC_SW_DWN&community=RE&latitude=31.42&longitude=73.08&format=JSON>
- `lahore`, pvgis: 4408 rows; radiation database PVGIS-ERA5, elevation 214.0 m
  - <https://re.jrc.ec.europa.eu/api/v5_2/seriescalc?lat=31.52&lon=74.36&startyear=2020&endyear=2020&components=1&outputformat=json&usehorizon=1&pvcalculation=1&pvtechchoice=crystSi&mountingplace=free&peakpower=100&loss=14&angle=15&aspect=0>
  - <https://re.jrc.ec.europa.eu/api/v5_2/seriescalc?lat=31.52&lon=74.36&startyear=2020&endyear=2020&components=1&outputformat=json&usehorizon=1&pvcalculation=0&angle=0&aspect=0>
- `lahore`, power: 4512 rows
  - <https://power.larc.nasa.gov/api/temporal/hourly/point?parameters=ALLSKY_SFC_SW_DWN%2CCLRSKY_SFC_SW_DWN%2CT2M&community=RE&latitude=31.52&longitude=74.36&start=20200101&end=20201231&format=JSON&time-standard=UTC>
- `lahore`, monthly: PVGIS years 2005-2020, E_y 154437.94 kWh, SD_y 2778.93 kWh, losses: angle of incidence -2.66 %, spectral 0.47 %, temperature and low irradiance -11.42 %, total -25.5 %; NASA POWER climatology 25-year Meteorological and Solar Monthly & Annual Climatologies (January 2001 - December 2025)
  - <https://re.jrc.ec.europa.eu/api/v5_2/PVcalc?lat=31.52&lon=74.36&outputformat=json&usehorizon=1&pvtechchoice=crystSi&mountingplace=free&peakpower=100&loss=14&angle=15&aspect=0>
  - <https://re.jrc.ec.europa.eu/api/v5_2/MRcalc?lat=31.52&lon=74.36&horirrad=1&outputformat=json&startyear=2005&endyear=2020>
  - <https://power.larc.nasa.gov/api/temporal/climatology/point?parameters=ALLSKY_SFC_SW_DWN&community=RE&latitude=31.52&longitude=74.36&format=JSON>
- `karachi`, pvgis: 4421 rows; radiation database PVGIS-ERA5, elevation 10.0 m
  - <https://re.jrc.ec.europa.eu/api/v5_2/seriescalc?lat=24.86&lon=67.01&startyear=2020&endyear=2020&components=1&outputformat=json&usehorizon=1&pvcalculation=1&pvtechchoice=crystSi&mountingplace=free&peakpower=100&loss=14&angle=15&aspect=0>
  - <https://re.jrc.ec.europa.eu/api/v5_2/seriescalc?lat=24.86&lon=67.01&startyear=2020&endyear=2020&components=1&outputformat=json&usehorizon=1&pvcalculation=0&angle=0&aspect=0>
- `karachi`, power: 4504 rows
  - <https://power.larc.nasa.gov/api/temporal/hourly/point?parameters=ALLSKY_SFC_SW_DWN%2CCLRSKY_SFC_SW_DWN%2CT2M&community=RE&latitude=24.86&longitude=67.01&start=20200101&end=20201231&format=JSON&time-standard=UTC>
- `karachi`, monthly: PVGIS years 2005-2020, E_y 168198.77 kWh, SD_y 2056.43 kWh, losses: angle of incidence -2.56 %, spectral -0.10 %, temperature and low irradiance -9.79 %, total -24.47 %; NASA POWER climatology 25-year Meteorological and Solar Monthly & Annual Climatologies (January 2001 - December 2025)
  - <https://re.jrc.ec.europa.eu/api/v5_2/PVcalc?lat=24.86&lon=67.01&outputformat=json&usehorizon=1&pvtechchoice=crystSi&mountingplace=free&peakpower=100&loss=14&angle=15&aspect=0>
  - <https://re.jrc.ec.europa.eu/api/v5_2/MRcalc?lat=24.86&lon=67.01&horirrad=1&outputformat=json&startyear=2005&endyear=2020>
  - <https://power.larc.nasa.gov/api/temporal/climatology/point?parameters=ALLSKY_SFC_SW_DWN&community=RE&latitude=24.86&longitude=67.01&format=JSON>
- `rahim_yar_khan`, pvgis: 4396 rows; radiation database PVGIS-ERA5, elevation 83.0 m
  - <https://re.jrc.ec.europa.eu/api/v5_2/seriescalc?lat=28.42&lon=70.3&startyear=2020&endyear=2020&components=1&outputformat=json&usehorizon=1&pvcalculation=1&pvtechchoice=crystSi&mountingplace=free&peakpower=100&loss=14&angle=15&aspect=0>
  - <https://re.jrc.ec.europa.eu/api/v5_2/seriescalc?lat=28.42&lon=70.3&startyear=2020&endyear=2020&components=1&outputformat=json&usehorizon=1&pvcalculation=0&angle=0&aspect=0>
- `rahim_yar_khan`, power: 4537 rows
  - <https://power.larc.nasa.gov/api/temporal/hourly/point?parameters=ALLSKY_SFC_SW_DWN%2CCLRSKY_SFC_SW_DWN%2CT2M&community=RE&latitude=28.42&longitude=70.3&start=20200101&end=20201231&format=JSON&time-standard=UTC>
- `rahim_yar_khan`, monthly: PVGIS years 2005-2020, E_y 167994.7 kWh, SD_y 2130.76 kWh, losses: angle of incidence -2.56 %, spectral 0.07 %, temperature and low irradiance -11.86 %, total -26.09 %; NASA POWER climatology 25-year Meteorological and Solar Monthly & Annual Climatologies (January 2001 - December 2025)
  - <https://re.jrc.ec.europa.eu/api/v5_2/PVcalc?lat=28.42&lon=70.3&outputformat=json&usehorizon=1&pvtechchoice=crystSi&mountingplace=free&peakpower=100&loss=14&angle=15&aspect=0>
  - <https://re.jrc.ec.europa.eu/api/v5_2/MRcalc?lat=28.42&lon=70.3&horirrad=1&outputformat=json&startyear=2005&endyear=2020>
  - <https://power.larc.nasa.gov/api/temporal/climatology/point?parameters=ALLSKY_SFC_SW_DWN&community=RE&latitude=28.42&longitude=70.3&format=JSON>
