import pandas as pd
import numpy as np

# =====================================================================
# Paths (relative, repo-friendly — point elsewhere or move to a config module)
# =====================================================================
WEATHER_DATA = "bus_weather_data_25010115.csv"          # substation/bus-level weather (includes 100Wind, 2t, sp)
BUS_DATA     = "bus2025_data.csv"
GEN_DATA     = "generator2025_data_modified.csv" # Wind turbine GENERATOR_TYPE is already the IEC class

def main():

    # Wind farm generation performance under normal conditions (regular performance curve)

    # ---------------------------------------------------------
    # Step 1: Prepare the tables
    #   IEC class is no longer read separately from wind_farms_iec_classified.csv;
    #   instead it is taken directly from the GENERATOR_TYPE column of the gen table
    #   (assigned by Generator_Type_Cost_Config.py)
    # ---------------------------------------------------------
    weather_df = pd.read_csv(WEATHER_DATA)   # substation/bus, 10u, 10v, 2t, sp, etc.
    bus_df     = pd.read_csv(BUS_DATA)       # bus_number, substation_id
    gen_df     = pd.read_csv(GEN_DATA)       # gen_id, bus_number, resource_type, capacity, GENERATOR_TYPE

    wind_gens = gen_df[gen_df['FUEL_TYPE'] == 'WND (Wind)'].copy()

    # GENERATOR_TYPE is the IEC class ("Class 1/2/3" / "Unknown")
    wind_gens['IEC_Class'] = wind_gens['GENERATOR_TYPE']

    wind_gens = pd.merge(wind_gens, bus_df, on='BUS_I', how='left')
    wind_gens = pd.merge(wind_gens, weather_df, on='Substation_Number', how='left')

    # ---------------------------------------------------------
    # Step 3: Correct time, temperature, and air density
    # ---------------------------------------------------------
    wind_gens["datetime"] = wind_gens['date'] + ' ' + wind_gens['time']

    # temperature in celcius
    wind_gens['2m_temp_celcius'] = wind_gens['2t'] - 273.15

    # air density correction: V_norm = V_meas * (rho_meas / rho_0)^(1/3); rho = P / (R_spec * T)
    # R_spec = 287.05 J/(kg·K);  rho_0 = 1.225 kg/m^3
    wind_gens['rho_actual'] = wind_gens['sp'] / (287.05 * wind_gens['2t'])
    wind_gens['adj_wind_speed'] = wind_gens['100Wind'] * (wind_gens['rho_actual'] / 1.225)**(1/3)

    # ---------------------------------------------------------
    # Step 4: Define the power curve and interpolate
    # ---------------------------------------------------------
    speed_bins = np.arange(0, 26)  # 0..25
    iec_class_2_norm = [
        0, 0, 0, 0.0052, 0.0423, 0.1031, 0.1909, 0.3127, 0.4731, 0.6693,
        0.8554, 0.9641, 0.9942, 0.9994, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1
    ]
    iec_class_3_norm = [
        0, 0, 0, 0.0054, 0.053, 0.1351, 0.2508, 0.4033, 0.5952, 0.7849,
        0.9178, 0.9796, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0
    ]

    # right=0: equivalent wind speed > 25 m/s (end of table) -> cut-out protection, output = 0
    # Note: currently only Class 2 vs the rest is distinguished; Class 1 (high-wind sites) currently
    #       uses the Class 3 curve, which is conservative. If a dedicated Class 1 curve is added later,
    #       simply extend the mapping here.
    is_class2 = wind_gens['IEC_Class'] == 'Class 2'
    wind_gens['norm_power'] = np.where(
        is_class2,
        np.interp(wind_gens['adj_wind_speed'], speed_bins, iec_class_2_norm, right=0),
        np.interp(wind_gens['adj_wind_speed'], speed_bins, iec_class_3_norm, right=0),
    )
    wind_gens['simulated_PG'] = wind_gens['norm_power'] * wind_gens['PMAX']

    # ---------------------------------------------------------
    # Step 5: Define the shutdown logic
    # ---------------------------------------------------------
    # GEN_STATUS is 0 (off-grid/fault) -> output 0
    wind_gens['simulated_PG'] = np.where(wind_gens['GEN_STATUS'] == 1, wind_gens['simulated_PG'], 0.0)
    # Low-temperature shutdown: temperature < -20°C -> output 0
    wind_gens['simulated_PG'] = np.where(wind_gens['2m_temp_celcius'] < -20, 0.0, wind_gens['simulated_PG'])

    # ---------------------------------------------------------
    # Step 6: Data output
    # ---------------------------------------------------------
    wind_gens = wind_gens[[
        'datetime', 'date', 'time', 'BUS_I', 'GEN_I', 'Substation_Number',
        'GEN_STATUS', 'IEC_Class', '2m_temp_celcius', 'adj_wind_speed',
        'PMAX', 'norm_power', 'simulated_PG'
    ]]

    print(wind_gens.head())
    wind_gens.to_csv("wind_generation_time_series.csv", index=False)


if __name__ == "__main__":
    main()
