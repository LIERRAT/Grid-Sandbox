import pandas as pd
import numpy as np

def main():

    ## regular performance curve of hydroelectric generators

    # Step 1: organizing tables    
    weather_df = pd.read_csv("bus_weather_data_25010115.csv")  
    bus_df = pd.read_csv("bus2025_data.csv")  
    gen_df = pd.read_csv("generator2025_data_modified.csv") 
    hydro_gens = gen_df[gen_df['FUEL_TYPE'] == 'WAT (Water)'].copy()
    hydro_gens = pd.merge(hydro_gens, bus_df, on='BUS_I', how='left')
    hydro_gens = pd.merge(hydro_gens, weather_df, on='Substation_Number', how='left')

    # step 2: data processing
    hydro_annual_cf = 0.09 
    hydro_gens['simulated_PG'] = hydro_gens['PMAX'] * hydro_annual_cf
    hydro_gens["datetime"] = hydro_gens['date'] + ' ' + hydro_gens['time']
    
    # step 3: output
    hydro_gens = hydro_gens[['datetime', 'date','time','BUS_I', 'GEN_I', 'Substation_Number', 'PMAX', 'simulated_PG']]
    print(hydro_gens.head())
    hydro_gens.to_csv("hydro_generation_time_series.csv", index=False)


if __name__ == "__main__":
    main()
