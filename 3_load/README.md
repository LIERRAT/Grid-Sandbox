# Data

The model reads a set of CSV inputs. Some are downloaded from public sources; the
rest are produced from them by the preparation scripts in this repository. All
sources are public — nothing here is redistributed; obtain each from its source.

## Sources

| Source | Provides |
|---|---|
| Texas A&M Texas2k | Base bus / generator / branch topology |
| ECMWF ERA5 (CDS) | Hourly weather fields (`*.grib`) |
| NASA POWER | Mean wind speed for wind-site IEC class |
| NREL SLOPE | County sector electricity shares |
| NREL ResStock / ComStock, ORNL | Weekly residential / commercial / industrial load shapes |
| EIA (861M, EPM, 923, Table 8.2) | Small-scale PV totals, fuel prices, heat rates |
| U.S. Census | County population |

## Data flow

```
bus data (coordinates from PowerWorld export)
  └─ 0_grid_prep/Coordinates2County.py  + county shapefile
       → bus2025_data.csv                        (adds County)

bus2025_data.csv + RCI_ratio_by_county.csv + BTM_PV_county_level.csv
  └─ 3_load/load_decomposition.py
       → SLOPE_load_composition_bus_level.csv     (nodal load split + BTM PV)

raw generator table
  └─ 0_grid_prep/Generator_Type_Cost_Config.py  + NASA POWER
       → generator2025_data_modified.csv          (type + cost; wind IEC class)

25010115.grib + coordinates.csv
  └─ 1_weather/bus_weather_data_processing.py
       → bus_weather_data_25010115.csv

bus_weather + bus2025 + generator_modified
  └─ 2_resources/*        → wind / solar / hydro / availability series
bus_weather + SLOPE_load_composition + RCI_Electricity_Load_Curves
  └─ 3_load/LoadModelingTimeSeries.py  → winterized_load_time_series_bus_level.csv

  └─ 4_dispatch/Syngrid2025.m  → results (.mat)
```

## Files

**`bus2025_data.csv`** — bus table with geography. Texas2k bus data with PowerWorld
coordinates; `County` added by `Coordinates2County.py` (spatial join). The
coordinate merge is taken as given, not scripted. `Substation_Number` is a
run-length index from the PowerWorld export (by BUS_I order), carried as-is.

**`generator2025_data_modified.csv`** — from `Generator_Type_Cost_Config.py`. Gas
technology type from a capacity mixture model (EIA-860); storage duration from an
EIA-860 distribution; wind IEC class from NASA POWER (cached in `wind_iec_cache.csv`);
cost coefficients from EIA fuel prices and heat rates.

**`RCI_ratio_by_county.csv`** — each county's residential/commercial/industrial
share of total electricity (NREL SLOPE), `R + C + I = 1`. The *spatial* load split.
Not the same as `RCI_Electricity_Load_Curves.csv`.

**`BTM_PV_county_level.csv`** — county residential/commercial small-scale PV
capacity: EIA-861M state total split by county population (Census).

**`SLOPE_load_composition_bus_level.csv`** — from `load_decomposition.py`. For each
load bus (`PD > 0`): `P_x = PD × ratio`; county BTM PV allocated by each bus's `PD`
share within the county. Self-checks ratios sum to 1, split reconstructs `PD`, and
BTM PV is conserved per county.

**`RCI_Electricity_Load_Curves.csv`** — normalized weekly (168 h) load shapes
(ResStock / ComStock / ORNL) plus a BTM PV capacity-factor column. The *time* shape
of load.

**`bus_weather_data_25010115.csv`** — from `bus_weather_data_processing.py`: ERA5
sampled at substation coordinates, unit- and timezone-converted.

**`25010115.grib`** — raw ERA5, obtained from CDS.

## Notes

- `*.grib` and `*.mat` are excluded via `.gitignore`; get ERA5 from CDS, regenerate
  results by running the pipeline.
- `wind_iec_cache.csv` caches NASA POWER lookups so the generator step runs without
  re-querying the API.
