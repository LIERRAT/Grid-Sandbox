import pandas as pd
import numpy as np

def main():

    # 水电机组在正常情况下的发电表现 regular performance curve

    # ---------------------------------------------------------
    # Step 1: 整理表格
    # ---------------------------------------------------------
    weather_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus_weather_data_25010115.csv")  # 包含 substation/bus, 10u, 10v, 2t, sp 等
    bus_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus2025_data.csv")  # 包含 bus_number, substation_id
    gen_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\generator2025_data_modified.csv")  # 包含 gen_id, bus_number, resource_type, capacity
    hydro_gens = gen_df[gen_df['FUEL_TYPE'] == 'WAT (Water)'].copy()
    hydro_gens = pd.merge(hydro_gens, bus_df, on='BUS_I', how='left')
    hydro_gens = pd.merge(hydro_gens, weather_df, on='Substation_Number', how='left')
    
    hydro_annual_cf = 0.09 
    hydro_gens['simulated_PG'] = hydro_gens['PMAX'] * hydro_annual_cf

    hydro_gens["datetime"] = hydro_gens['date'] + ' ' + hydro_gens['time']
    # ---------------------------------------------------------
    # Step 6: 数据输出
    # ---------------------------------------------------------
    hydro_gens = hydro_gens[['datetime', 'date','time','BUS_I', 'GEN_I', 'Substation_Number', 'PMAX', 'simulated_PG']]

    print(hydro_gens.head())
    hydro_gens.to_csv("hydro_generation_time_series.csv", index=False)


if __name__ == "__main__":
    main()