import pandas as pd
import numpy as np

def main():

    # 风电场在正常情况下的发电表现 regular performance curve

    # ---------------------------------------------------------
    # Step 1: 整理表格
    # ---------------------------------------------------------
    weather_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus_weather_data_25010115.csv")  # 包含 substation/bus, 10u, 10v, 2t, sp 等
    bus_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus2025_data.csv")  # 包含 bus_number, substation_id
    gen_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\generator2025_data.csv")  # 包含 gen_id, bus_number, resource_type, capacity
    iec_windfarms = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\wind_farms_iec_classified.csv")
    wind_gens = gen_df[gen_df['FUEL_TYPE'] == 'WND (Wind)'].copy()
    wind_gens = pd.merge(wind_gens, bus_df, on='BUS_I', how='left')
    wind_gens = pd.merge(wind_gens, weather_df, on='Substation_Number', how='left')
    wind_gens = pd.merge(wind_gens, iec_windfarms, on='Substation_Number', how='left')




    # ---------------------------------------------------------
    # Step 3: 修正时间，温度，空气密度
    # ---------------------------------------------------------
    wind_gens["datetime"] = wind_gens['date'] + ' ' + wind_gens['time']
    
    # temperature in celcius
    wind_gens['2m_temp_celcius'] = wind_gens['2t'] - 273.15

    # air density correction: V_norm = V_meas * (rho_meas / rho_0)^(1/3); gas_density = P / (R_spec * T)
    # gas constant for dry air: R_spec = 287.05 J/(kg·K)
    # standard air density: rho_0 = 1.225 kg/m^3
    wind_gens['rho_actual'] = wind_gens['sp'] / (287.05 * wind_gens['2t'])
    wind_gens['adj_wind_speed'] = wind_gens['100Wind'] * (wind_gens['rho_actual'] / 1.225)**(1/3)

    # ---------------------------------------------------------
    # Step 4: 定义功率曲线，插值
    # ---------------------------------------------------------
    # IEC Class 2 Wind Turbine Power Curve
    speed_bins = np.arange(0, 26) # 0 到 25
    iec_class_2_norm = [
        0, 0, 0, 0.0052, 0.0423, 0.1031, 0.1909, 0.3127, 0.4731, 0.6693,
        0.8554, 0.9641, 0.9942, 0.9994, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1
    ]
    iec_class_3_norm = [
        0, 0, 0, 0.0054, 0.053, 0.1351, 0.2508, 0.4033, 0.5952, 0.7849,
        0.9178, 0.9796, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0
    ]

    # linear interpolation
    # 参数 right=0 意味着一旦等效风速超过 25m/s (表格最大值)，停机保护，出力直接为 0
    wind_gens['norm_power'] = np.where(wind_gens['IEC_Class'] == 'Class 2', np.interp(wind_gens['adj_wind_speed'], speed_bins, iec_class_2_norm, right=0), np.interp(wind_gens['adj_wind_speed'], speed_bins, iec_class_3_norm, right=0))
    wind_gens['simulated_PG'] = wind_gens['norm_power'] * wind_gens['PMAX']


    # ---------------------------------------------------------
    # Step 5: 定义停机逻辑
    # ---------------------------------------------------------
    # 如果 GEN_STATUS 为 0 (脱网/故障)，则理论出力也设为 0
    wind_gens['simulated_PG'] = np.where(wind_gens['GEN_STATUS'] == 1, wind_gens['simulated_PG'], 0.0)

    # 如果温度低于-20摄氏度 (低温停机), 出力设为 0
    wind_gens['simulated_PG'] = np.where(wind_gens['2m_temp_celcius'] < -20, 0.0, wind_gens['simulated_PG'])


    # ---------------------------------------------------------
    # Step 6: 数据输出
    # ---------------------------------------------------------
    wind_gens = wind_gens[['datetime','date','time','BUS_I', 'Substation_Number', 'GEN_STATUS', '2m_temp_celcius', 'adj_wind_speed','PMAX', 'norm_power', 'simulated_PG']]
    
    print(wind_gens.head())
    wind_gens.to_csv("wind_generation_time_series.csv", index=False)


if __name__ == "__main__":
    main()