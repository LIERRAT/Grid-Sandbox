import numpy as np
import pandas as pd
import pvlib


def main():
    # 1. Read the tables
    weather_df = pd.read_csv("bus_weather_data_25010115.csv")  
    bus_df = pd.read_csv("bus2025_data.csv")  
    gen_df = pd.read_csv("generator2025_data_modified.csv")  

    # 2. Filter the solar nodes and merge only the static spatial information
    solar_gens = gen_df[gen_df['FUEL_TYPE'] == 'SUN (Solar)'].copy()
    solar_gens = pd.merge(solar_gens, bus_df, on='BUS_I', how='left')
    
   # [Change] Prepare an empty list to collect the long-format DataFrame of every plant
    all_profiles_list = []
    
    # 3. Iterate over all solar generating units
    for idx, row in solar_gens.iterrows():
        bus_id = row['BUS_I']
        gen_id = row['GEN_I']
        sub_id = row['Substation_Number']
        lat = row['Latitude'] 
        lon = row['Longitude']
        pmax_mw = row['PMAX'] 
        gen_status = row['GEN_STATUS']
        

        # 4. Slice the local weather for this substation out of the overall weather table
        local_weather = weather_df[weather_df['Substation_Number'] == sub_id].copy()
        local_weather['datetime'] = local_weather['date'] + ' ' + local_weather['time']

        # 5. Run the physical model computation
        p_final = simulate_utility_scale_plant(lat, lon, pmax_mw, local_weather)

        # 1. Make sure the column is a true datetime type (if it already is, this step can be skipped)
        local_weather ['datetime'] = pd.to_datetime(local_weather['datetime'])

        # 2. Use the .dt accessor to extract date and time
        local_weather['date'] = local_weather['datetime'].dt.date
        local_weather['time'] = local_weather['datetime'].dt.time

        # 6. Build the DataFrame for the solar plant
        gen_output = pd.DataFrame({
            'datetime': local_weather['datetime'], # timestamp
            'date': local_weather['date'],          # date
            'time': local_weather['time'],          # time
            'BUS_I': bus_id,
            'GEN_I': gen_id,
            'Substation_Number': sub_id,
            'GEN_STATUS': gen_status,
            'snowfall_approx_mm': p_final[1].values,
            'snow_coverage_percentage': p_final[2].values,
            'poa_irradiance_w_m2': p_final[3].values,
            'cell_temp_celsius': p_final[4].values,
            'wind_speed_m_s': p_final[5].values,
            'simulated_PG': p_final[0].values / 1e6,
            'PMAX': pmax_mw,
            'norm_power': np.where(pmax_mw > 0, p_final[0].values / (pmax_mw * 1e6), 0)
        })
        
        

        # Append the current plant's DataFrame to the list
        all_profiles_list.append(gen_output)

    # ==========================================
    # Consolidate the output results
    # ==========================================
    
    # Vertically concatenate all plant data (equivalent to SQL's UNION ALL)
    final_long_df = pd.concat(all_profiles_list, ignore_index=True)
    
    # Fill missing values produced at night or by errors with 0
    final_long_df['simulated_PG'] = final_long_df['simulated_PG'].fillna(0)
    final_long_df['norm_power'] = final_long_df['norm_power'].fillna(0)
    
    print(final_long_df.head(30))
    
    # Drop the index on export to get a clean table
    final_long_df.to_csv("solar_generation_time_series.csv", index=False)


# Core simulation function

def simulate_utility_scale_plant(lat, lon, pmax_mw, weather_data):
    # Operate on a copy to avoid modifying the original data and corrupting the loop
    weather_df = weather_data.copy()
        
    # Convert the strings to timestamps
    weather_df['datetime'] = pd.to_datetime(weather_df['datetime'])
    weather_df.set_index('datetime', inplace=True)
    weather_df.index = weather_df.index.tz_localize('America/Chicago')

    # ---------------------------------------------------------
    # Step 2: Physical unit conversion of the meteorological features
    # ---------------------------------------------------------
    weather_df['ssrd'] = weather_df['ssrd'] / 3600.0
    weather_df['temp'] = weather_df['2t'] - 273.15
    weather_df['snow_depth'] = weather_df['sd'].clip(lower=0)
    weather_df['wind'] = weather_df['10Wind']
    
    # Estimate the new snowfall: the core driver of the NREL model is "falling snow"; if data is missing, approximate it from the depth difference
    weather_df['snowfall_approx'] = weather_df['sd'].diff().clip(lower=0).fillna(0)

    # ---------------------------------------------------------
    # Step 3: Capacity alignment (AC to DC)
    # ---------------------------------------------------------
    dc_ac_ratio = 1.3
    capacity_ac_watts = pmax_mw * 1e6
    capacity_dc_watts = capacity_ac_watts * dc_ac_ratio
    
   
    # ---------------------------------------------------------
    # Step 4. Compute the solar position
    # ---------------------------------------------------------
    solpos = pvlib.solarposition.get_solarposition(weather_df.index, lat, lon)
    
    # ---------------------------------------------------------
    # Step 5. Irradiance decomposition with DIRINT (replaces ERBS, improves the winter low-sun-angle DNI overestimation)
    # ---------------------------------------------------------
    pressure = 101325   # elevation (meters); if unavailable, use 101325.0

    dni = pvlib.irradiance.dirint(
        ghi=weather_df['ssrd'],
        solar_zenith=solpos['apparent_zenith'],
        times=weather_df.index,
        pressure=pressure
    ).fillna(0)

    # DHI is back-solved from the closure relation: GHI = DHI + DNI·cos(zenith)
    cos_zen = np.cos(np.radians(solpos['apparent_zenith'])).clip(lower=0)
    dhi = (weather_df['ssrd'] - dni * cos_zen).clip(lower=0)
    
    # ---------------------------------------------------------
    # Step 6. Configure the single-axis tracker
    # ---------------------------------------------------------
    tracker_data = pvlib.tracking.singleaxis(
        apparent_zenith=solpos['apparent_zenith'],
        solar_azimuth=solpos['azimuth'],
        axis_tilt=0, 
        axis_azimuth=180, 
        max_angle=60, 
        backtrack=True, 
        gcr=0.35 
    )
    
    # ---------------------------------------------------------
    # Step 7. Compute the POA irradiance
    # ---------------------------------------------------------
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=tracker_data['surface_tilt'],
        surface_azimuth=tracker_data['surface_azimuth'],
        dni=dni, 
        ghi=weather_df['ssrd'],
        dhi=dhi,
        solar_zenith=solpos['apparent_zenith'],
        solar_azimuth=solpos['azimuth']
    )
    
    # ---------------------------------------------------------
    # Step 7.5: Nighttime NaN handling (safeguard mechanism)
    # ---------------------------------------------------------
    # The tracker has no valid angle at night, which produces NaN in the POA; these must be forced to 0
    poa['poa_global'] = poa['poa_global'].fillna(0)
    
    # So the NREL snow model can run correctly afterward, the panel tilt must also be filled at night
    # (assume the panels lie flat at night, i.e. tilt=0)
    tracker_data['surface_tilt'] = tracker_data['surface_tilt'].fillna(0)
    
    # ---------------------------------------------------------
    # Step 8. Temperature model of the PV cell module
    # ---------------------------------------------------------
    
    # the source of parameters: pvlib.temperature.sapm_cell API reference - OPEN RACK, Glass to Glass
    
    temp_cell = pvlib.temperature.sapm_cell(
        poa['poa_global'], weather_df['temp'], weather_df['wind'],
        a=-3.47, b=-0.0594, deltaT=3 
    )
    
    # ---------------------------------------------------------
    # Step 9. DC generation (PVWatts)
    # ---------------------------------------------------------
    
    # the source of parameters: pvlib.pvsystem.pvwatts_dc API reference
    
    p_dc = pvlib.pvsystem.pvwatts_dc(
        poa['poa_global'], temp_cell, capacity_dc_watts,
        gamma_pdc=-0.004
    )

    # ---------------------------------------------------------
    # Step 9.5. System losses (PVWatts loss stack; snow excluded — snow is handled separately in Step 11)
    # ---------------------------------------------------------
    
    # the source of parameters: pvlib.pvsystem.pvwatts_losses API reference
    
    system_losses_pct = pvlib.pvsystem.pvwatts_losses(
        soiling=2,          
        shading=0,          # edit: the tracker's backtrack already handles inter-row shading, set to 0 to avoid double-counting
        snow=0,             # edit: handled separately in Step 11, set to 0 to avoid double-counting
        mismatch=2,
        wiring=2,
        connections=0.5,
        lid=1.5,            # light-induced degradation
        nameplate_rating=1,
        age=0,              # for a fleet averaging N years, set to ~0.5*N
        availability=3      # forced outage / partial unavailability; for a strict HSL-potential benchmark, lower to 0~1
    )
    p_dc = p_dc * (1 - system_losses_pct / 100.0)   # apply losses on the DC side

    # ---------------------------------------------------------
    # Step 10. Inverter (PVWatts inverter: includes efficiency curve + clipping)
    # ---------------------------------------------------------
    
    # the source of parameters: pvlib.inverter.pvwatts API reference
    
    eta_inv_nom = 0.96
    p_ac = pvlib.inverter.pvwatts(
        pdc=p_dc,
        pdc0=capacity_ac_watts / eta_inv_nom,  # make the AC cap strictly = capacity_ac_watts
        eta_inv_nom=eta_inv_nom
    )
    p_ac = p_ac.fillna(0).clip(lower=0)   # guard against NaN and negative values at night / very low DC

    # ---------------------------------------------------------
    # Step 11. Snow-loss post-processing
    # ---------------------------------------------------------
    snow_coverage = pvlib.snow.coverage_nrel(
        snowfall=weather_df['snowfall_approx'],     # use the approximate hourly new snowfall
        poa_irradiance=poa['poa_global'], 
        temp_air=weather_df['temp'], 
        surface_tilt=tracker_data['surface_tilt'],
        snow_depth=weather_df['snow_depth'],        # add ground snow depth as an auxiliary criterion
        threshold_depth=1.0                         # takes effect above 1 cm
    )

    p_final = p_ac * (1 - snow_coverage)
    
    return [p_final, weather_df['snowfall_approx'], snow_coverage, poa['poa_global'], temp_cell, weather_df['wind']]

if __name__ == "__main__":
    main()
