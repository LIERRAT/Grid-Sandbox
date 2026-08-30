# Data

This document explains where every data input comes from, how the derived inputs
are produced, and what may or may not be redistributed. The model reads a set of
CSV inputs; some are downloaded from public sources, and some are produced by the
preparation scripts in this repository from those sources.

> **License note.** The code license in `LICENSE` does **not** cover data. Each
> external source below carries its own terms — confirm them before redistributing
> any raw or derived file. In particular, raw ERA5 weather is **not** redistributed
> here; only a means of obtaining it is provided.

---

## Core sources

| Source | What it provides | Redistributable here? |
|---|---|---|
| **Texas A&M Texas2k** synthetic grid | Base bus / generator / branch topology and parameters | Per TAMU terms — `TODO confirm` |
| **ECMWF ERA5** (Climate Data Store) | Hourly weather fields for the event window | **No** — obtain via CDS; not shipped |
| NASA POWER | Annual mean wind speed for wind-site IEC classification | Public API; values cached in-repo |
| NREL SLOPE | County sector electricity consumption (→ RCI split) | `TODO confirm` |
| NREL ResStock / ComStock | Residential / commercial weekly load shapes | `TODO confirm` |
| ORNL | Industrial weekly load shape | `TODO confirm` |
| EIA (861M, EPM, 923, Table 8.2) | Small-scale PV totals; fuel prices; heat rates | Public |
| U.S. Census | County population (→ BTM PV allocation) | Public |

`TODO` for each source: dataset version / vintage, download date, and URL.

---

## Pipeline data flow

Raw inputs are prepared into model inputs by the scripts in `0_grid_prep`,
`1_weather`, and `3_load`. The dependency chain:

```
bus data (with coordinates, from PowerWorld export)
  └─ 0_grid_prep/Coordinates2County.py  ── + Texas county shapefile
       → bus2025_data.csv                (adds County)

bus2025_data.csv
 + RCI_ratio_by_county.csv               (county sector shares, from SLOPE)
 + BTM_PV_county_level.csv               (county BTM PV, EIA-861M × population)
  └─ 3_load/load_decomposition.py
       → SLOPE_load_composition_bus_level.csv   (nodal load split + BTM PV)

generator (raw)
  └─ 0_grid_prep/Generator_Type_Cost_Config.py  ── + NASA POWER (wind IEC)
       → generator2025_data_modified.csv  (generator type + cost; wind IEC class)

25010115.grib (ERA5)
 + coordinates.csv                        (substation lat/lon)
  └─ 1_weather/bus_weather_data_processing.py
       → bus_weather_data_25010115.csv

  [bus_weather + bus2025 + generator_modified]
  └─ 2_resources/*  →  wind / solar / hydro / availability time series
  [bus_weather + SLOPE_load_composition + RCI_Electricity_Load_Curves]
  └─ 3_load/LoadModelingTimeSeries.py  →  winterized_load_time_series_bus_level.csv

  [all of the above]
  └─ 4_dispatch/Syngrid2025.m  →  results (.mat)
```

---

## Input files

### From raw grid data

**`bus2025_data.csv`** — bus table with geography.
Base electrical bus data (Texas2k) with substation coordinates merged in from the
Texas2k PowerWorld export, plus a `County` column added by
`0_grid_prep/Coordinates2County.py` (spatial join against a Texas county
shapefile). `Substation_Number` is a run-length index from the PowerWorld export
(numbered by BUS_I order), not a canonical substation ID — carried as-is for
downstream join consistency.
*The coordinate merge itself is not scripted here; coordinates are taken as given
from the PowerWorld source.*

**`generator2025_data_modified.csv`** — generator table with type + cost.
Produced by `0_grid_prep/Generator_Type_Cost_Config.py` from the raw Texas2k
generator table:
- Gas units get a technology type (combined-cycle / combustion / steam) from a
  capacity-based mixture model fit to EIA-860.
- Storage units get a duration (1–6 h) and rated energy from an EIA-860 empirical
  distribution.
- Wind units get an IEC class (`GENERATOR_TYPE`) from local mean wind speed
  (NASA POWER, 50 m → 100 m power-law extrapolation), cached in
  `wind_iec_cache.csv`.
- Cost coefficients are rebuilt from EIA fuel prices and heat rates.

### Load composition (derived)

**`RCI_ratio_by_county.csv`** — county sector split.
Each county's residential / commercial / industrial share of **total** electricity
consumption from NREL SLOPE, so `R + C + I = 1` per county. Used to split each
bus's demand by sector. (Not to be confused with `RCI_Electricity_Load_Curves.csv`,
which is a time-shape, not a spatial split — see below.)

**`BTM_PV_county_level.csv`** — county behind-the-meter PV capacity.
County residential / commercial small-scale PV capacity, obtained by splitting the
EIA-861M Texas state total by county population share (U.S. Census).

**`SLOPE_load_composition_bus_level.csv`** — nodal load composition (model input).
Produced by `3_load/load_decomposition.py` from the three files above. For each
load bus (`PD > 0`): sector loads are `P_x = PD × ratio`; county BTM PV is
allocated to buses by each bus's `PD` share within the county. The script
self-checks that ratios sum to 1, that the split reconstructs `PD`, and that BTM PV
is conserved per county.
> An earlier hand-built version of this file had an inconsistent commercial ratio
> in most counties (commercial load under-counted); this script corrects it.

### Load shapes and weather

**`RCI_Electricity_Load_Curves.csv`** — weekly hourly load shapes.
Normalized residential / commercial / industrial weekly profiles (168 h) from
ResStock / ComStock / ORNL, plus a BTM distributed-PV capacity-factor column. This
is the **time** dimension of load (a repeating weekly shape); the county file above
is the **spatial** split. See the separate data card for column detail.

**`25010115.grib`** — raw ERA5 weather (not shipped).
Hourly fields for the event window from ECMWF CDS. Obtain via CDS; not
redistributed in this repository.

**`bus_weather_data_25010115.csv`** — per-substation hourly weather (model input).
Produced by `1_weather/bus_weather_data_processing.py` by sampling the ERA5 grid at
substation coordinates (`coordinates.csv`) and converting units / time zone.

---

## Notes on large and non-redistributable files

- Raw ERA5 (`*.grib`) and model results (`*.mat`) are large and/or not
  redistributable and are excluded via `.gitignore`. Obtain ERA5 from CDS; results
  are regenerated by running the pipeline.
- `wind_iec_cache.csv` is a reproducible cache of NASA POWER lookups; committing it
  lets others run the generator prep step without re-querying the API.
