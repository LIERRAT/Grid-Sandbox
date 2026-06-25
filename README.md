# Grid-Sandbox: A Weather-Driven Integrated Assessment Framework for Power Grid Resilience Analysis

## Project Status: Active Research
Grid-Sandbox is currently under active development. This repository serves as a research documentation and technical progress tracker. 

## The Problem We Are Solving
Current studies on power grid winter resilience often struggle to adequately simulate system performance during extreme cold events. In real-world extreme weather conditions, the natural gas supply, power generation, transmission, and demand sectors exhibit complex coupled responses, which existing assessment models fail to fully capture. 

Consequently, it remains challenging for Utilities and Planning Authorities to validate and address critical system reliability issues for NERC. Key unresolved challenges include:
* Addressing the partial system impact of extreme cold weather expected in only a portion of the studied area.
* Guaranteeing sufficient generation and reserves for upcoming winter conditions.
* Effectively including planned outages, generating unit limitations, and the likely loss of fuel sources in winter assessments.
* Determining how effective investing in winterization for specific generation assets would be in mitigating system-level common-mode failures.

To address these industry pain points, Grid-Sandbox introduces a weather-driven hybrid response framework. By mapping ERA5 meteorological time-series data onto the synthetic grid buses, the project serves as a comprehensive system-level stress testing and assessment platform. 

Since it does not rely on proprietary measured data, this model is highly portable and can be tested on various synthetic power grids.

## Project Workflow
The framework operates through a four-stage time-series simulation methodology:

### Stage 1: Weather Data Input
The simulation is driven by high-resolution meteorological data (ECMWF's ERA5). Variables including temperature, wind speed, solar irradiance, humidity, and snow depth are ingested, cleaned, and spatially mapped to the specific nodes of the synthetic grid.

### Stage 2: Performance Models
Weather stressors are mapped onto grid operations using State Machines and Non-homogenous Markov Chains to simulate dynamic performance across three main sectors:
* **Generation Performance:** Quantifies the probabilistic risk of outages and derates for diverse energy sources, including Wind, Solar, Nuclear, Natural Gas, Coal, and Hydroelectric, based on environmental thresholds.
* **Load Response:** Decouples the grid load into residential, commercial, and industrial profiles. It applies load curves and temperature sensitivity models to reflect demand surges and integrates behind-the-meter (BTM) rooftop solar capacity.
* **Transmission Availability:** Evaluates the probabilistic risk of transmission line collapses driven by physical stressors like ice accretion and winds.
* **Fuel Availability:** TBD

### Stage 3: Simulation
Utilizes MATPOWER to run Sequential Time-series Power Flow (DCOPF) simulations. This stage integrates all dynamic node states and executes emergency load-shedding algorithms when capacity limits are breached.

### Stage 4: Data Analysis & Scenarios
Evaluates overall system resilience using metrics such as EUE. The platform allows for the testing of preventive scenarios—such as targeted generation weatherization or emergency load response programs to quantify the system-level impact of specific resilience investments.


