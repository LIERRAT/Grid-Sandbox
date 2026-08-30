# Weather-Driven Scenario Generator for Extreme-Weather Grid Reliability

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Status: research prototype](https://img.shields.io/badge/status-research%20prototype-blue)

A weather-driven scenario generator for extreme-weather grid reliability, built on
an ERCOT-scale synthetic grid.

**What it does:** you give it a real extreme cold event, and it turns that weather
into hour-by-hour scenarios across an ERCOT-scale synthetic grid — with nodal load,
renewable output, battery storage scheduling, unit commitment, and generator
availability, all driven by the same weather. The idea is that the same storm pushes
load up and cuts renewables down at the same time, and only where the weather
actually hits. It's meant to be the upstream scenario layer that extreme-temperature
reliability assessments like NERC TPL-008 may need, ahead of the studies grid
operators run themselves.

## Example inputs and outputs

One input from a mid-January window:

https://github.com/user-attachments/assets/5c4f198e-29e2-4e81-9585-e8671a3bf9f3

*Temperature across Texas.*

Three outputs from the same window:

https://github.com/user-attachments/assets/1635f5ff-0d6b-4716-8b8e-fb1bc6874117

*Nodal price (LMP) across Texas. You can watch congestion and scarcity show up as
prices split apart across locations, hour by hour.*

https://github.com/user-attachments/assets/227690f4-eb69-4669-8a49-95d2c6ed3b3f

*Load shed. What I find striking is that it stays quiet most of the time, then lights
up in a few specific spots only in the hours the system gets tight — instead of
failing everywhere at once.*

https://github.com/user-attachments/assets/507fffc7-8eb2-497c-819b-f814195bcbdb

*Generation curtailment.*

All demonstrations sit on top of a full per-node, per-hour dataset underneath.

## How it works

The pipeline takes two root inputs — a real weather event and a synthetic grid —
and drives every downstream quantity from the *same* weather, so that load, renewables,
and outages move together and only where the storm actually lands.

1. **Weather** — hourly fields from a real cold event (ECMWF ERA5) are mapped onto the
   grid's substations.
2. **Grid prep** — the synthetic grid (Texas A&M Texas2k) is enriched with generator
   technology types and cost curves; wind sites are assigned IEC classes from local
   mean wind speed.
3. **Time-series simulation**, all weather-driven:
   - **Load** — temperature-sensitive residential / commercial / industrial demand at
     each node.
   - **Wind / solar / hydro** — output from IEC power curves (with air-density
     correction), a `pvlib` PV model (with snow losses), and hydro capacity factors.
   - **Generator availability** — a three-state (available / derate / outage) Markov
     model with temperature-dependent cold-weather derating.
4. **Dispatch** — storage scheduling (QP peak-shaving) → security-constrained unit
   commitment (SCUC) → hourly DC optimal power flow with value-of-lost-load load
   shedding and reserves.
5. **Output** — per-node, per-hour LMP, load shed, dispatch, and storage state.

<img width="1651" height="1254" alt="image" src="https://github.com/user-attachments/assets/f61ca22c-e927-4af5-9ea7-9de885e57cb7" />

## Scope and limitations

To be clear about what this is, and what it is not:

- It's a **research prototype on a synthetic grid**, and it covers **active power only**.
- Dispatch runs on a **DC optimal power flow**, so the LMPs are an **illustrative
  representation** of congestion and scarcity — **not real or settlement-accurate
  market prices**. There is no reactive power, voltage, or stability side.
- It is **not a compliance tool**, and **not a market or trading model**.

Think of it as a sandbox for studying how localized weather turns into localized
stress, and how that stress shows up in both physical shortfall and price.

## Repository structure

The pipeline is split into five stages, numbered by run order. Each stage takes the
previous stage's output plus the weather, and produces one piece of the final
hour-by-hour scenario. The same weather event drives every stage.

| Folder | What it does |
|---|---|
| `0_grid_prep` | One-time static setup before any weather is applied: tags each bus with its county, and assigns every generator a technology type and cost curve. |
| `1_weather` | Maps the raw weather file (ERA5 GRIB) onto the grid, producing an hourly weather value at each substation. |
| `2_resources` | Turns weather into hourly supply: wind, solar, and hydro output per plant, plus conventional generator availability (cold-weather outages and derates). |
| `3_load` | Turns weather into hourly demand: temperature-sensitive nodal load that rises as temperatures drop. |
| `4_dispatch` | Runs the grid hour by hour — storage scheduling, unit commitment, and DC optimal power flow (DC-OPF). |

Data files live alongside the stage that uses them; each stage's data provenance is documented in that stage's README.

## Dependencies

| Component | Needs |
|---|---|
| Data prep, weather, resources, load | Python (`pandas`, `numpy`, `pvlib`, `earthkit-data`, `xarray`, `geopandas`, `requests`, `scipy`) |
| Dispatch | MATLAB + [MATPOWER](https://matpower.org/) 8.1 + [Gurobi](https://www.gurobi.com/) (free academic license) |

## Data sources

Two core inputs, plus reference datasets — all public; obtain each from its source
(nothing is redistributed here):

- **Synthetic grid** — [Texas A&M Electric Grid Datasets](https://electricgrids.engr.tamu.edu/) (Texas2k).
- **Weather** — [ECMWF Climate Data Store](https://cds.climate.copernicus.eu/) (ERA5).
- **Reference** — NASA POWER (wind climatology), NREL SLOPE / ResStock / ComStock
  (load composition and shapes), ORNL (industrial load), EIA (fuel prices, heat rates).

## Status and roadmap

- [x] Core pipeline end to end (weather → dispatch → outputs)
- [ ] Path/config cleanup for portability
- [ ] Sample dataset for an end-to-end runnable demo
- [ ] Stage-by-stage documentation
- [ ] Fuel-logistics (gas supply) constraints

## Citation

If you use this work, please cite it — see `CITATION.cff` (to be added).

## License

Released under the MIT License. See [`LICENSE`](LICENSE).

## Acknowledgements

Huge thanks to the engineers and researchers across NERC, ERCOT, EPRI, NREL, and
industry who took time to reality-check a student's questions along the way. You
shaped this more than you know.

## About / contact

This project is why I now want to build my career in power systems. I'm actively
looking for a **power systems internship for Spring or Summer 2027** — open on the
specific area, as long as it's grid-related. If your team takes interns, or you know
a team that does, I'd really appreciate a pointer or an introduction.

Feedback is genuinely welcome, and I'm always happy to connect.

<!-- Add your contact: email / LinkedIn / GitHub profile link -->

