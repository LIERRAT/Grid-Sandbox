import numpy as np
import pandas as pd
import pvlib


def main():
    # 1. 读表
    weather_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus_weather_data_25010115.csv")  
    bus_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus2025_data.csv")  
    gen_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\generator2025_data_modified.csv")  

    # 2. 筛选光伏节点，并只合并静态空间信息
    solar_gens = gen_df[gen_df['FUEL_TYPE'] == 'SUN (Solar)'].copy()
    solar_gens = pd.merge(solar_gens, bus_df, on='BUS_I', how='left')
    
   # 【改动点】准备一个空列表，用来收集所有电站的长表 DataFrame
    all_profiles_list = []
    
    # 3. 遍历所有光伏发电机组
    for idx, row in solar_gens.iterrows():
        bus_id = row['BUS_I']
        gen_id = row['GEN_I']
        sub_id = row['Substation_Number']
        lat = row['Latitude'] 
        lon = row['Longitude']
        pmax_mw = row['PMAX'] 
        gen_status = row['GEN_STATUS']
        

        # 4. 从总天气表中切片出该 substation 的局部天气
        local_weather = weather_df[weather_df['Substation_Number'] == sub_id].copy()
        local_weather['datetime'] = local_weather['date'] + ' ' + local_weather['time']

        # 5. 执行物理模型计算
        p_final = simulate_utility_scale_plant(lat, lon, pmax_mw, local_weather)

        # 1. 确保你的列是真正的 datetime 格式（如果已经是，这步可以省略）
        local_weather ['datetime'] = pd.to_datetime(local_weather['datetime'])

        # 2. 使用 .dt 访问器提取日期和时间
        local_weather['date'] = local_weather['datetime'].dt.date
        local_weather['time'] = local_weather['datetime'].dt.time

        # 6. 构建光伏电站的DataFrame
        gen_output = pd.DataFrame({
            'datetime': local_weather['datetime'], # 时间戳
            'date': local_weather['date'],          # 日期
            'time': local_weather['time'],          # 时间
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
        
        

        # 将当前电站的 DataFrame 追加到列表中
        all_profiles_list.append(gen_output)

    # ==========================================
    # 输出结果整合
    # ==========================================
    
    # 纵向拼接所有的电站数据 (相当于 SQL 的 UNION ALL)
    final_long_df = pd.concat(all_profiles_list, ignore_index=True)
    
    # 将夜间或报错产生的缺失值填充为 0
    final_long_df['simulated_PG'] = final_long_df['simulated_PG'].fillna(0)
    final_long_df['norm_power'] = final_long_df['norm_power'].fillna(0)
    
    print(final_long_df.head(30))
    
    # 导出时去掉 index 即可得到干净的表格
    final_long_df.to_csv("solar_generation_time_series.csv", index=False)


# 核心仿真函数

def simulate_utility_scale_plant(lat, lon, pmax_mw, weather_data):
    # 为避免修改原数据导致循环错乱，操作其副本
    weather_df = weather_data.copy()
        
    # 将字符串转为时间戳
    weather_df['datetime'] = pd.to_datetime(weather_df['datetime'])
    weather_df.set_index('datetime', inplace=True)
    weather_df.index = weather_df.index.tz_localize('America/Chicago')

    # ---------------------------------------------------------
    # Step 2: 气象特征的物理单位换算
    # ---------------------------------------------------------
    weather_df['ssrd'] = weather_df['ssrd'] / 3600.0
    weather_df['temp'] = weather_df['2t'] - 273.15
    weather_df['snow_depth'] = weather_df['sd'].clip(lower=0)
    weather_df['wind'] = weather_df['10Wind']
    
    # 估算新增降雪量 (snowfall)：NREL模型核心驱动力是“降落的雪”，若缺数据则用深度差近似
    weather_df['snowfall_approx'] = weather_df['sd'].diff().clip(lower=0).fillna(0)

    # ---------------------------------------------------------
    # Step 3: 系统容量对齐 (AC to DC)
    # ---------------------------------------------------------
    dc_ac_ratio = 1.3
    capacity_ac_watts = pmax_mw * 1e6
    capacity_dc_watts = capacity_ac_watts * dc_ac_ratio
    
   
    # ---------------------------------------------------------
    # Step 4. 计算太阳位置
    # ---------------------------------------------------------
    solpos = pvlib.solarposition.get_solarposition(weather_df.index, lat, lon)
    
    # ---------------------------------------------------------
    # Step 5. 辐射分解 DIRINT (替代 ERBS，改善冬季低太阳角 DNI 高估)
    # ---------------------------------------------------------
    pressure = 101325   # elevation(米)；没有就用 101325.0

    dni = pvlib.irradiance.dirint(
        ghi=weather_df['ssrd'],
        solar_zenith=solpos['apparent_zenith'],
        times=weather_df.index,
        pressure=pressure
    ).fillna(0)

    # DHI 由闭合关系反推：GHI = DHI + DNI·cos(zenith)
    cos_zen = np.cos(np.radians(solpos['apparent_zenith'])).clip(lower=0)
    dhi = (weather_df['ssrd'] - dni * cos_zen).clip(lower=0)
    
    # ---------------------------------------------------------
    # Step 6.配置 single axis tracker
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
    # Step 7. 计算POA辐射
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
    # Step 7.5: 夜间 NaN 处理 (防雷机制)
    # ---------------------------------------------------------
    # 跟踪器在夜间无有效角度，会导致 POA 产生 NaN，必须强行补 0
    poa['poa_global'] = poa['poa_global'].fillna(0)
    
    # 为了后续 NREL 积雪模型能正常运行，面板倾角在夜间也需要填补
    # (假设夜间面板平放，即 tilt=0)
    tracker_data['surface_tilt'] = tracker_data['surface_tilt'].fillna(0)
    
    # ---------------------------------------------------------
    # Step 8. 光伏电池模块的温度模型
    # ---------------------------------------------------------
    
    # the source of parameters: pvlib.temperature.sapm_cell API reference - OPEN RACK, Glass to Glass
    
    temp_cell = pvlib.temperature.sapm_cell(
        poa['poa_global'], weather_df['temp'], weather_df['wind'],
        a=-3.47, b=-0.0594, deltaT=3 
    )
    
    # ---------------------------------------------------------
    # Step 9. DC 发电量 (PVWatts)
    # ---------------------------------------------------------
    
    # the source of parameters: pvlib.pvsystem.pvwatts_dc API reference
    
    p_dc = pvlib.pvsystem.pvwatts_dc(
        poa['poa_global'], temp_cell, capacity_dc_watts,
        gamma_pdc=-0.004
    )

    # ---------------------------------------------------------
    # Step 9.5. 系统损失 (PVWatts 损失栈；雪除外——雪在 Step 11 单独算)
    # ---------------------------------------------------------
    
    # the source of parameters: pvlib.pvsystem.pvwatts_losses API reference
    
    system_losses_pct = pvlib.pvsystem.pvwatts_losses(
        soiling=2,          
        shading=0,          # edit: 跟踪器 backtrack 已处理行间遮挡，置 0 避免重复计
        snow=0,             # edit: 单独在 Step 11 处理，设 0 避免重复计算
        mismatch=2,
        wiring=2,
        connections=0.5,
        lid=1.5,            # 光致衰减
        nameplate_rating=1,
        age=0,              # 机队平均 N 年可设 ~0.5*N
        availability=3      # 强迫停运/部分不可用；若严格对标 HSL 潜力可调低到 0~1
    )
    p_dc = p_dc * (1 - system_losses_pct / 100.0)   # 损失施加在 DC 侧

    # ---------------------------------------------------------
    # Step 10. 逆变器 (PVWatts 逆变器：含效率曲线 + 切峰)
    # ---------------------------------------------------------
    
    # the source of parameters: pvlib.inverter.pvwatts API reference
    
    eta_inv_nom = 0.96
    p_ac = pvlib.inverter.pvwatts(
        pdc=p_dc,
        pdc0=capacity_ac_watts / eta_inv_nom,  # 令 AC 上限严格 = capacity_ac_watts
        eta_inv_nom=eta_inv_nom
    )
    p_ac = p_ac.fillna(0).clip(lower=0)   # 夜间/极低 DC 时防 NaN 和负值
    
    # ---------------------------------------------------------
    # Step 11. 积雪损失后处理
    # ---------------------------------------------------------
    snow_coverage = pvlib.snow.coverage_nrel(
        snowfall=weather_df['snowfall_approx'],     # 使用近似的逐时新增降雪
        poa_irradiance=poa['poa_global'], 
        temp_air=weather_df['temp'], 
        surface_tilt=tracker_data['surface_tilt'],
        snow_depth=weather_df['snow_depth'],        # 加入地面积雪辅助判断
        threshold_depth=1.0                         # 超过1cm开始起效
    )

    p_final = p_ac * (1 - snow_coverage)
    
    return [p_final, weather_df['snowfall_approx'], snow_coverage, poa['poa_global'], temp_cell, weather_df['wind']]

if __name__ == "__main__":
    main()
