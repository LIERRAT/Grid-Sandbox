import pandas as pd
import numpy as np

def main():
    df_bus = pd.read_csv("SLOPE_load_composition_bus_level.csv")
    df_curve = pd.read_csv("RCI_Electricity_Load_Curves.csv")
    df_weather = pd.read_csv("bus_weather_data_25010115.csv")

    
    # 1. 提取真实的 datetime 对象，方便后续进行时间的数学加减
    real_time = pd.to_datetime(df_weather['date'] + ' ' + df_weather['time'])
    df_weather['real_dt'] = real_time
    df_weather['time_str'] = real_time.dt.strftime('%a %I:%M %p') # 保留原有的查表字符串
    
    df_load = df_weather[['date', 'time_str', 'real_dt', 'Substation_Number', '2t', '2d']].copy()
    
    # ---------------------------------------------------------
    # 2. 为每个节点生成随机的多样性参数 (Diversity Parameters)
    # ---------------------------------------------------------
    np.random.seed(42) # 固定种子，确保每次运行的基准场景一致
    unique_buses = df_bus['Substation_Number'].unique()
    
    # 居民参数 (Residential): 随机提前或延后 -2 到 +2 小时，振幅 85% 到 115%
    res_shift_hours = np.random.randint(-2, 3, size=len(unique_buses))
    res_amp_scales = np.random.uniform(0.85, 1.15, size=len(unique_buses))

    # 商业参数 (Commercial): 同理，你可以根据需要调整这里的上下限
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
    # 3. 计算用于查表的“偏移时间” (Shifted Time)
    # ---------------------------------------------------------
    # 居民偏移
    df_load['res_shifted_dt'] = df_load['real_dt'] + pd.to_timedelta(df_load['res_time_shift'], unit='h')
    df_load['res_shifted_time_str'] = df_load['res_shifted_dt'].dt.strftime('%a %I:%M %p')

    # 商业偏移
    df_load['com_shifted_dt'] = df_load['real_dt'] + pd.to_timedelta(df_load['com_time_shift'], unit='h')
    df_load['com_shifted_time_str'] = df_load['com_shifted_dt'].dt.strftime('%a %I:%M %p')
    
    # ---------------------------------------------------------
    # 4. 分离合并 Load Curves
    # ---------------------------------------------------------
    
    # A. 工业负荷 (Industrial)：使用真实时间字符串匹配
    df_load = df_load.merge(
        df_curve[['Timestamp (EST)', 'Baseline Ins Electricity','TX BTM DGPV NCF']].rename(columns={'Timestamp (EST)': 'time_str'}), 
        on='time_str', 
        how='left'
    )

    # B. 居民负荷 (Residential)：使用居民偏移后的时间字符串匹配
    df_load = df_load.merge(
        df_curve[['Timestamp (EST)', 'Baseline Res Electricity']].rename(columns={'Timestamp (EST)': 'res_shifted_time_str'}), 
        on='res_shifted_time_str', 
        how='left'
    )
    
    # C. 商业负荷 (Commercial)：使用商业偏移后的时间字符串匹配
    df_load = df_load.merge(
        df_curve[['Timestamp (EST)', 'Baseline Comm Electricity']].rename(columns={'Timestamp (EST)': 'com_shifted_time_str'}), 
        on='com_shifted_time_str', 
        how='left'
    )
    
    # 防御性处理：如果时间偏移超出了 df_curve 的星期范围导致 NaN，用相邻时间填充
    df_load['Baseline Res Electricity'] = df_load['Baseline Res Electricity'].ffill().bfill()
    df_load['Baseline Comm Electricity'] = df_load['Baseline Comm Electricity'].ffill().bfill()
    
    # ---------------------------------------------------------
    # 5. 合并物理节点数据
    # ---------------------------------------------------------
    df_load = df_load.merge(
        df_bus[['Substation_Number', 'County', 'Area', 'BUS_I', 'P_Res_Load', 'P_Com_Load', 'P_Ins_Load', 'Bus BTM PV Res Capacity', 'Bus BTM PV Comm Capacity']],
        on='Substation_Number',
        how='left'
    )

    df_load = df_load.dropna()
    # ---------------------------------------------------------
    # 6. 计算温度敏感度，设定冬季负荷缩放系数
    # ---------------------------------------------------------

    # 转换为摄氏度
    df_load['hour_temp'] = df_load['2t'] - 273.15
    df_load['hour_dewtemp'] = df_load['2d'] - 273.15
    
    # 1. 生成 12 小时区块标签 (向下取整)
    # 注意：这里改成了 'quart_day_block'，避免和后面的温度均值列名冲突
    df_load['quart_day_block'] = df_load['real_dt'].dt.floor('6h')
    
    df_quart_day_temp = df_load.groupby(['quart_day_block', 'BUS_I'])['hour_temp'].mean().reset_index(name='quart_day_avg_temp')
    df_quart_day_dewtemp = df_load.groupby(['quart_day_block', 'BUS_I'])['hour_dewtemp'].mean().reset_index(name='quart_day_avg_dewtemp')
    
    # 4. 按 'quart_day_block' 和 'BUS_I' 将计算结果合并回主表
    df_load = df_load.merge(df_quart_day_temp, on=['quart_day_block', 'BUS_I'], how='left')
    df_load = df_load.merge(df_quart_day_dewtemp, on=['quart_day_block', 'BUS_I'], how='left')

    # ---> 新增：计算 Dewpoint Depression (露点差) <---
    df_load['quart_day_avg_dew_dep'] = df_load['quart_day_avg_temp'] - df_load['quart_day_avg_dewtemp']

    # ==========================================
    # 1. 气象变量计算：由干球温度与露点计算 RH 及体感温度
    # ==========================================
    # 计算饱和水汽压 (saturation vp)
    df_load['saturation_vp'] = 6.11 * (10 ** ((7.5 * df_load['quart_day_avg_temp']) / (237.3 + df_load['quart_day_avg_temp'])))

    # 计算实际水汽压 (actual vp)
    df_load['actual_vp'] = 6.11 * (10 ** ((7.5 * df_load['quart_day_avg_dewtemp']) / (237.3 + df_load['quart_day_avg_dewtemp'])))

    # 计算室外相对湿度 (outdoor RH, %)
    df_load['outdoor_rh'] = (df_load['actual_vp'] / df_load['saturation_vp']) * 100

    # ==========================================
    # 2. 住宅侧温度敏感性
    # ==========================================
    res_sweetspot_lower_end = 7.01
    res_sweetspot_higher_end = 22.29

    # 使用体感温度定义 4 段条件 (Conditions)
    cond_extreme_heating = (df_load['quart_day_avg_temp'] <= 3) & (df_load['outdoor_rh'] >= 70)  ### 

    res_temp_sensitivity_conditions = [
        # 条件 1: 极端体感低温 (湿冷环境，电辅热激增)
        cond_extreme_heating,
        
        # 条件 2: 常规体感供暖
        (df_load['quart_day_avg_temp'] < res_sweetspot_lower_end) & ~cond_extreme_heating,
        
        # 条件 3: 体感舒适区 (Sweetspot)
        (df_load['quart_day_avg_temp'] >= res_sweetspot_lower_end) & (df_load['quart_day_avg_temp'] < res_sweetspot_higher_end),
        
        # 条件 4: 体感制冷
        df_load['quart_day_avg_temp'] >= res_sweetspot_higher_end 
    ]

    # 定义对应条件下的 4 段计算公式 (Choices)
    # 将自变量全部从原始温度 T 替换为体感温度 AT
    res_base_temp = 15.556

    res_heating_sst = -1.4 / 11.112
    # 适当放大了极端段的负荷斜率敏感度（从 -2 调整至 -2.8 左右，以更好拟合真实的 SOUTH 峰值）
    res_extreme_heating_sst = -1.8 / 11.112 
    res_cooling_sst = 1.65 / 11.112

    res_temp_sensitivity_choices = [
        res_extreme_heating_sst *  df_load['quart_day_avg_temp'] - res_extreme_heating_sst * res_base_temp, # 对应极端体感低温
        res_heating_sst * df_load['quart_day_avg_temp'] - res_heating_sst * res_base_temp,                 # 对应常规体感供暖
        1,                                                                      # 对应体感舒适区
        res_cooling_sst * df_load['quart_day_avg_temp'] - res_cooling_sst * res_base_temp                  # 对应体感制冷
    ]

    # 得到最终的住宅温度敏感性系数
    df_load['res_day_temp_sensitivity'] = np.select(
        res_temp_sensitivity_conditions, 
        res_temp_sensitivity_choices
    )

    # commercial temperature sensitivity ref:

    # 1. 定义 3 段条件 (Conditions)
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
        df_load['quart_day_avg_temp'] <= com_base_temp, # heating sensitivity y = -0.93(x - 17.8) + 3.5 -> -0.93x + 20.054 -> -0.113x + 2.49
        df_load['quart_day_avg_temp'] > com_base_temp, # cooling sensitivity y = 0.86(x - 6.7) + 3.1 -> 0.86x - 2.662 -> 0.104x - 0.323
    ]

    # 2. 定义对应条件下的 3 段计算公式 (Choices)
    com_temp_sensitivity_choices = [
        (com_heating_sst * df_load['quart_day_avg_temp'] + com_heating_intercept) / com_base_load,                                       
        (com_cooling_sst * df_load['quart_day_avg_temp'] + com_cooling_intercept) / com_base_load
    ]

    df_load['com_day_temp_sensitivity'] = np.select(com_temp_sensitivity_conditions, com_temp_sensitivity_choices) 

    # ---------------------------------------------------------
    # 7. 计算最终功率（分别乘上各自的振幅缩放系数）
    # ---------------------------------------------------------
    winterization_factor = 0.7  # 冬季负荷的整体缩放
    heating_factor_com = 0.26  # 供暖负荷 taken from Comstock Texas Jan - Feb weekly HVAC electricity consumption / total electricity consumption
    heating_factor_res = 0.45   # 供暖负荷 taken from ResStock Texas Jan - Feb weekly HVAC electricity consumption / total electricity consumption

    #time series residential and commercial load with temperature sensitivity
    df_load['PRes'] = df_load['P_Res_Load'] * df_load['Baseline Res Electricity'] * df_load['res_amp_scale'] * winterization_factor * df_load['res_day_temp_sensitivity']
    df_load['PCom'] = df_load['P_Com_Load'] * df_load['Baseline Comm Electricity'] * df_load['com_amp_scale'] * winterization_factor 
    df_load['PCom_HVAC'] = df_load['PCom'] * heating_factor_com 
    df_load['PCom'] = df_load['PCom'] - df_load['PCom_HVAC'] + df_load['PCom_HVAC'] * df_load['com_day_temp_sensitivity'] # 仅调整供暖部分的负荷
    
    
    
    # residential and commercial BTM rooftop PV
    df_load['BTM_PV_Res'] = df_load['Bus BTM PV Res Capacity'] * df_load['TX BTM DGPV NCF']
    df_load['BTM_PV_Comm'] = df_load['Bus BTM PV Comm Capacity'] * df_load['TX BTM DGPV NCF']
    df_load['PRes'] = df_load['PRes'] - df_load['BTM_PV_Res']  
    df_load['PCom'] = df_load['PCom'] - df_load['BTM_PV_Comm']
    
    #industrial load
    df_load['PIns'] = df_load['P_Ins_Load'] * df_load['Baseline Ins Electricity']
    
    # ---------------------------------------------------------
    # 7.5 最终曲线平滑处理 (Curve Smoothing)
    # ---------------------------------------------------------
    # 第一步：必须确保数据按物理节点和真实时间严格排序
    df_load = df_load.sort_values(['BUS_I', 'real_dt']).reset_index(drop=True)

    # 第二步：设定平滑窗口（window=3 代表取前后各 1 小时加上当前小时，共 3 小时取平均）
    # 使用 center=True 保证峰值的时间点不会发生偏移（不滞后）
    smooth_window = 4

    
    # 对居民和商业负荷分别进行中心化滑动平均
    df_load['PRes'] = df_load.groupby('BUS_I')['PRes'].transform(
        lambda x: x.rolling(window=smooth_window, center=True, min_periods=1).mean()
    )    
    df_load['PCom'] = df_load.groupby('BUS_I')['PCom'].transform(
        lambda x: x.rolling(window=smooth_window, center=True, min_periods=1).mean()
    )

    # 第三步：重新加总得到平滑后的冬季总负荷
    df_load['PD_winterized'] = df_load['PRes'] + df_load['PCom'] + df_load['PIns']
    
    # ---------------------------------------------------------
    # 8. 清理并打包数据
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
