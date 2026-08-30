import pandas as pd
import numpy as np

def main():
    df_bus = pd.read_csv("SLOPE_load_composition_bus_level.csv")
    df_curve = pd.read_csv("RCI_Electricity_Load_Curves.csv")
    df_weather = pd.read_csv("bus_weather_data_25010115.csv")

    
    # 1. Extract real datetime objects to make later time arithmetic (add/subtract) easy
    real_time = pd.to_datetime(df_weather['date'] + ' ' + df_weather['time'])
    df_weather['real_dt'] = real_time
    df_weather['time_str'] = real_time.dt.strftime('%a %I:%M %p')  # Keep the original lookup string

    df_load = df_weather[['date', 'time_str', 'real_dt', 'Substation_Number', '2t', '2d']].copy()
    
    # ---------------------------------------------------------
    # 2. Generate random diversity parameters for each bus (node)
    # ---------------------------------------------------------
    np.random.seed(42)  # Fix the seed to ensure a consistent baseline scenario on every run
    unique_buses = df_bus['Substation_Number'].unique()
    
    # Residential parameters: random shift of -2 to +2 hours, amplitude 85% to 115%
    res_shift_hours = np.random.randint(-2, 3, size=len(unique_buses))
    res_amp_scales = np.random.uniform(0.85, 1.15, size=len(unique_buses))

    # Commercial parameters: same idea; adjust the bounds here as needed
    com_shift_hours = np.random.randint(-2, 3, size=len(unique_buses))
    com_amp_scales = np.random.uniform(0.85, 1.15, size=len(unique_buses))
    
    df_diversity = pd.DataFrame({
        'Substation_Number': unique_buses,
        'res_time_shift': res_shift_hours,
        'res_amp_scale': res_amp_scales,
        'com_time_shift': com_shift_hours,
        'com_amp_scale': com_amp_scales
    })
    
    df_load = df_load.merge(df_diversity, on='Substation_Number', how='left')
    
    # ---------------------------------------------------------
    # 3. Compute the "shifted time" used for the lookup
    # ---------------------------------------------------------
    # Residential shift
    df_load['res_shifted_dt'] = df_load['real_dt'] + pd.to_timedelta(df_load['res_time_shift'], unit='h')
    df_load['res_shifted_time_str'] = df_load['res_shifted_dt'].dt.strftime('%a %I:%M %p')

    # Commercial shift
    df_load['com_shifted_dt'] = df_load['real_dt'] + pd.to_timedelta(df_load['com_time_shift'], unit='h')
    df_load['com_shifted_time_str'] = df_load['com_shifted_dt'].dt.strftime('%a %I:%M %p')
    
    # ---------------------------------------------------------
    # 4. Merge the load curves separately
    # ---------------------------------------------------------
    
    # A. Industrial load: match using the real time string
    df_load = df_load.merge(
        df_curve[['Timestamp (EST)', 'Baseline Ins Electricity','TX BTM DGPV NCF']].rename(columns={'Timestamp (EST)': 'time_str'}), 
        on='time_str', 
        how='left'
    )

    # B. Residential load: match using the residential-shifted time string
    df_load = df_load.merge(
        df_curve[['Timestamp (EST)', 'Baseline Res Electricity']].rename(columns={'Timestamp (EST)': 'res_shifted_time_str'}), 
        on='res_shifted_time_str', 
        how='left'
    )
    
    # C. Commercial load: match using the commercial-shifted time string
    df_load = df_load.merge(
        df_curve[['Timestamp (EST)', 'Baseline Comm Electricity']].rename(columns={'Timestamp (EST)': 'com_shifted_time_str'}), 
        on='com_shifted_time_str', 
        how='left'
    )
    
    # Defensive handling: if the time shift falls outside the weekday range of df_curve and produces NaN, fill with adjacent times
    df_load['Baseline Res Electricity'] = df_load['Baseline Res Electricity'].ffill().bfill()
    df_load['Baseline Comm Electricity'] = df_load['Baseline Comm Electricity'].ffill().bfill()
    
    # ---------------------------------------------------------
    # 5. Merge the physical node data
    # ---------------------------------------------------------
    df_load = df_load.merge(
        df_bus[['Substation_Number', 'County', 'Area', 'BUS_I', 'P_Res_Load', 'P_Com_Load', 'P_Ins_Load', 'Bus BTM PV Res Capacity', 'Bus BTM PV Comm Capacity']],
        on='Substation_Number',
        how='left'
    )

    df_load = df_load.dropna()
    # ---------------------------------------------------------
    # 6. Compute temperature sensitivity and set the winter load scaling factors
    # ---------------------------------------------------------

    # Convert to Celsius
    df_load['hour_temp'] = df_load['2t'] - 273.15
    df_load['hour_dewtemp'] = df_load['2d'] - 273.15
    
    # 1. Generate 12-hour block labels (floor rounding)
    # Note: renamed to 'quart_day_block' to avoid a name clash with the temperature-mean column later
    df_load['quart_day_block'] = df_load['real_dt'].dt.floor('6h')
    
    df_quart_day_temp = df_load.groupby(['quart_day_block', 'BUS_I'])['hour_temp'].mean().reset_index(name='quart_day_avg_temp')
    df_quart_day_dewtemp = df_load.groupby(['quart_day_block', 'BUS_I'])['hour_dewtemp'].mean().reset_index(name='quart_day_avg_dewtemp')
    
    # 4. Merge the computed results back into the main table by 'quart_day_block' and 'BUS_I'
    df_load = df_load.merge(df_quart_day_temp, on=['quart_day_block', 'BUS_I'], how='left')
    df_load = df_load.merge(df_quart_day_dewtemp, on=['quart_day_block', 'BUS_I'], how='left')

    # ---> New: compute the dewpoint depression <---
    df_load['quart_day_avg_dew_dep'] = df_load['quart_day_avg_temp'] - df_load['quart_day_avg_dewtemp']

    # ==========================================
    # 1. Meteorological variable computation: derive RH and apparent temperature from dry-bulb temperature and dewpoint
    # ==========================================
    # Compute the saturation vapor pressure (saturation vp)
    df_load['saturation_vp'] = 6.11 * (10 ** ((7.5 * df_load['quart_day_avg_temp']) / (237.3 + df_load['quart_day_avg_temp'])))

    # Compute the actual vapor pressure (actual vp)
    df_load['actual_vp'] = 6.11 * (10 ** ((7.5 * df_load['quart_day_avg_dewtemp']) / (237.3 + df_load['quart_day_avg_dewtemp'])))

    # Compute the outdoor relative humidity (outdoor RH, %)
    df_load['outdoor_rh'] = (df_load['actual_vp'] / df_load['saturation_vp']) * 100

    # ==========================================
    # 2. Residential-side temperature sensitivity
    # ==========================================
    res_sweetspot_lower_end = 7.01
    res_sweetspot_higher_end = 22.29

    # Define 4 segment conditions using the apparent temperature
    cond_extreme_heating = (df_load['quart_day_avg_temp'] <= 3) & (df_load['outdoor_rh'] >= 70)  ###

    res_temp_sensitivity_conditions = [
        # Condition 1: extreme apparent cold (cold-and-damp environment, electric resistance heating surges)
        cond_extreme_heating,
        
        # Condition 2: normal apparent heating
        (df_load['quart_day_avg_temp'] < res_sweetspot_lower_end) & ~cond_extreme_heating,
        
        # Condition 3: apparent comfort zone (sweetspot)
        (df_load['quart_day_avg_temp'] >= res_sweetspot_lower_end) & (df_load['quart_day_avg_temp'] < res_sweetspot_higher_end),
        
        # Condition 4: apparent cooling
        df_load['quart_day_avg_temp'] >= res_sweetspot_higher_end 
    ]

    # Define the 4-segment formulas (choices) corresponding to the conditions above
    # Replace the independent variable throughout, from raw temperature T to apparent temperature AT
    res_base_temp = 15.556

    res_heating_sst = -1.4 / 11.112
    # Slightly amplified the load slope sensitivity of the extreme segment (adjusted from -2 to about -2.8, to better fit the real SOUTH peak)
    res_extreme_heating_sst = -1.8 / 11.112 
    res_cooling_sst = 1.65 / 11.112

    res_temp_sensitivity_choices = [
        res_extreme_heating_sst *  df_load['quart_day_avg_temp'] - res_extreme_heating_sst * res_base_temp,  # corresponds to extreme apparent cold
        res_heating_sst * df_load['quart_day_avg_temp'] - res_heating_sst * res_base_temp,                   # corresponds to normal apparent heating
        1,                                                                                                    # corresponds to apparent comfort zone
        res_cooling_sst * df_load['quart_day_avg_temp'] - res_cooling_sst * res_base_temp                    # corresponds to apparent cooling
    ]

    # Get the final residential temperature sensitivity coefficient
    df_load['res_day_temp_sensitivity'] = np.select(
        res_temp_sensitivity_conditions, 
        res_temp_sensitivity_choices
    )

    # commercial temperature sensitivity ref:

    # 1. Define 3 segment conditions
    com_base_temp = 12.7
    com_heating_bload = 3.5
    com_cooling_bload = 3.1
    com_base_load = 8.25
    com_heating_sst = -0.93
    com_cooling_sst = 0.86
    com_heating_changepoint = 17.8
    com_cooling_changepoint = 6.7
    com_heating_intercept = -com_heating_sst * com_heating_changepoint + com_heating_bload
    com_cooling_intercept = -com_cooling_sst * com_cooling_changepoint + com_cooling_bload

    com_temp_sensitivity_conditions = [
        df_load['quart_day_avg_temp'] <= com_base_temp,  # heating sensitivity y = -0.93(x - 17.8) + 3.5 -> -0.93x + 20.054 -> -0.113x + 2.49
        df_load['quart_day_avg_temp'] > com_base_temp,   # cooling sensitivity y = 0.86(x - 6.7) + 3.1 -> 0.86x - 2.662 -> 0.104x - 0.323
    ]

    # 2. Define the 3-segment formulas (choices) corresponding to the conditions above
    com_temp_sensitivity_choices = [
        (com_heating_sst * df_load['quart_day_avg_temp'] + com_heating_intercept) / com_base_load,                                       
        (com_cooling_sst * df_load['quart_day_avg_temp'] + com_cooling_intercept) / com_base_load
    ]

    df_load['com_day_temp_sensitivity'] = np.select(com_temp_sensitivity_conditions, com_temp_sensitivity_choices) 

    # ---------------------------------------------------------
    # 7. Compute the final power (multiplying each by its own amplitude scaling factor)
    # ---------------------------------------------------------
    winterization_factor = 0.7  # Overall scaling of the winter load
    heating_factor_com = 0.26   # Heating load taken from Comstock Texas Jan - Feb weekly HVAC electricity consumption / total electricity consumption
    heating_factor_res = 0.45   # Heating load taken from ResStock Texas Jan - Feb weekly HVAC electricity consumption / total electricity consumption

    # Time-series residential and commercial load with temperature sensitivity
    df_load['PRes'] = df_load['P_Res_Load'] * df_load['Baseline Res Electricity'] * df_load['res_amp_scale'] * winterization_factor * df_load['res_day_temp_sensitivity']
    df_load['PCom'] = df_load['P_Com_Load'] * df_load['Baseline Comm Electricity'] * df_load['com_amp_scale'] * winterization_factor 
    df_load['PCom_HVAC'] = df_load['PCom'] * heating_factor_com 
    df_load['PCom'] = df_load['PCom'] - df_load['PCom_HVAC'] + df_load['PCom_HVAC'] * df_load['com_day_temp_sensitivity']  # Adjust only the heating portion of the load
    
    
    
    # Residential and commercial BTM rooftop PV
    df_load['BTM_PV_Res'] = df_load['Bus BTM PV Res Capacity'] * df_load['TX BTM DGPV NCF']
    df_load['BTM_PV_Comm'] = df_load['Bus BTM PV Comm Capacity'] * df_load['TX BTM DGPV NCF']
    df_load['PRes'] = df_load['PRes'] - df_load['BTM_PV_Res']  
    df_load['PCom'] = df_load['PCom'] - df_load['BTM_PV_Comm']
    
    # Industrial load
    df_load['PIns'] = df_load['P_Ins_Load'] * df_load['Baseline Ins Electricity']
    
    # ---------------------------------------------------------
    # 7.5 Final curve smoothing
    # ---------------------------------------------------------
    # Step 1: the data must be strictly sorted by physical node and real time
    df_load = df_load.sort_values(['BUS_I', 'real_dt']).reset_index(drop=True)

    # Step 2: set the smoothing window (window=3 means averaging over 1 hour before, 1 hour after, and the current hour, i.e. 3 hours total)
    # Use center=True to ensure the peak's timestamp does not shift (no lag)
    smooth_window = 4

    
    # Apply a centered moving average to the residential and commercial loads separately
    df_load['PRes'] = df_load.groupby('BUS_I')['PRes'].transform(
        lambda x: x.rolling(window=smooth_window, center=True, min_periods=1).mean()
    )    
    df_load['PCom'] = df_load.groupby('BUS_I')['PCom'].transform(
        lambda x: x.rolling(window=smooth_window, center=True, min_periods=1).mean()
    )

    # Step 3: re-sum to get the smoothed total winter load
    df_load['PD_winterized'] = df_load['PRes'] + df_load['PCom'] + df_load['PIns']
    
    # ---------------------------------------------------------
    # 8. Clean up and package the data
    # ---------------------------------------------------------
    df_load = df_load.rename(columns={'time_str': 'time'})
    df_load['time'] = df_load['time'].str[4:]
    df_load['datetime'] = df_load['date'] + ' ' + df_load['time']  
    df_load_package = df_load[['date', 'time','datetime', 'Substation_Number', 'BUS_I', 'County', 'Area', 'PRes', 'PCom', 'PIns','BTM_PV_Res', 'BTM_PV_Comm', 'PD_winterized','quart_day_avg_temp' , 'outdoor_rh']].copy()
    df_load_package['PRes'] = df_load_package['PRes'].round(3)
    df_load_package['PCom'] = df_load_package['PCom'].round(3)
    df_load_package['PIns'] = df_load_package['PIns'].round(3)
    df_load_package['PD_winterized'] = df_load_package['PD_winterized'].round(3)
    df_load_package['BTM_PV_Res'] = df_load_package['BTM_PV_Res'].round(3)
    df_load_package['BTM_PV_Comm'] = df_load_package['BTM_PV_Comm'].round(3)
    df_load_package['quart_day_avg_temp'] = df_load_package['quart_day_avg_temp'].round(3)
    df_load_package['outdoor_rh'] = df_load_package['outdoor_rh'].round(3)


    print(df_load_package.info())
    print(df_load_package.head(40))
    df_load_package.to_csv('winterized_load_time_series_bus_level.csv', index=False)


    
if __name__ == "__main__":
    main()
