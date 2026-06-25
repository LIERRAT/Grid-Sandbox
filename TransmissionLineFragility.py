import numpy as np
import pandas as pd
from scipy.interpolate import RegularGridInterpolator
from numba import njit

def main():
    # 1. read branch weather data
    path = r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\branch_weather_data_25010115.csv"
    weather_df = pd.read_csv(path)
    
    # 2. ice thickness
    weather_df = update_ice_thickness(weather_df)
    
    # 3. individual tower fragility 
    weather_df['Individual_fragility'] = calculate_failure_prob(weather_df['10Wind'], weather_df['cumulative_ice_mm'])
    
    # 4. aggregate to branch fragility
    weather_df['sample_survival_prob'] = (1 - weather_df['Individual_fragility']) ** weather_df['Tow_Patched']
    branch_agg = weather_df.groupby(['BRANCH_I', 'date', 'time'])['sample_survival_prob'].prod().reset_index()
    branch_agg['branch_fragility'] = 1 - branch_agg['sample_survival_prob']
    
    # 6. organize the table
    weather_df = weather_df.merge(
        branch_agg[['BRANCH_I', 'date', 'time', 'branch_fragility']], 
        on=['BRANCH_I', 'date', 'time'], 
        how='left'
    )

    weather_df = weather_df[["date", "time", "BRANCH_I", "Sample_I","Sample_Pnts","2t","tp", "10Wind","ptype", "cumulative_ice_mm", "Individual_fragility", "branch_fragility"]]
    branch_fragility_summary = weather_df[["date", "time", "BRANCH_I", "branch_fragility", "Sample_Pnts"]].drop_duplicates()
    branch_fragility_summary["branch_fragility"] = branch_fragility_summary["branch_fragility"].round(3)
    
    # 7. validation
    #print(weather_df[((weather_df["Sample_I"] == 872)) & ((weather_df["date"] == "2025-01-09"))].head(30))
    #print (weather_df[weather_df["cumulative_ice_mm"] > 0.5].head(30))\
    branch_fragility_summary = branch_fragility_summary[branch_fragility_summary["branch_fragility"] > 0]
    print(branch_fragility_summary.info())
    branch_fragility_summary.to_csv("branch_fragility_summary.csv", index=False)
    
    

# 1. 将判断逻辑独立出来，单独加上 @njit
@njit
def get_melt_rate(cumulative_ice_mm, temperature):
    #reference: Research on the DC Ice-Melting Model
    # 靠近 -1度 的情况 (温度 >= -3)
    
    if temperature >= -3.0:  
        if cumulative_ice_mm < 15.0:
            return 1.0    # 对应 -1度, 厚度 10
        elif cumulative_ice_mm < 25.0:
            return 0.8    # 对应 -1度, 厚度 20
        else:
            return 0.5    # 对应 -1度, 厚度 30
            
    # 靠近 -5度 的情况 (-3 > 温度 >= -7.5)
    elif temperature >= -7.5: 
        if cumulative_ice_mm < 15.0:
            return 0.5    # 对应 -5度, 厚度 10
        else:
            return 0.05   # 对应 -5度, 厚度 20 或 30 都是 0.05
            
    # 靠近 -10度 的情况 (温度 < -7.5)
    else:           
        return 0.05       # 对应 -10度, 无论厚度多少全是 0.05


# 2. 累加函数主逻辑，代码结构变得非常清晰
@njit
def fast_melting_accumulation(increments, temp):
    n = len(increments)
    output = np.zeros(n)
    current_ice = 0.0
    
    for i in range(n):
        # 1. 先把本小时新结的冰加上去，这是本小时的观测值
        total_this_hour = current_ice + increments[i]
        output[i] = total_this_hour
        
        # reference: Research on the DC Ice-Melting Model 
        melt_rate = get_melt_rate(output[i], temp[i])   
        
        # 2. 为下一小时计算余量：这小时的总量扣除融化部分
        current_ice = total_this_hour * (1.0 - melt_rate)
        
        if current_ice < 0:
            current_ice = 0.0
            
    return output


# 3. DataFrame 处理逻辑
def update_ice_thickness(weather_df):
    # 1. 计算每小时增量
    tp = weather_df['tp']
    wind = weather_df['10Wind']
    ptype = weather_df['ptype']
    weather_df["ice_thickness_mm"] = calculate_hourly_ice_thickness(tp, wind, ptype)
    
    # 删除之前冗余的 temp = np.median(...)，避免无用计算

    # 2. 依照采样点排序
    weather_df = weather_df.sort_values(by=['Sample_I', 'date', 'time'])
    
    # 3. 按 Sample_I 分组并应用融化逻辑
    group_ids = weather_df['Sample_I'].values
    ice_inc = weather_df['ice_thickness_mm'].values
    temp = weather_df['2t'].values
    
    # 【重点修复】：用前后错位比对代替 np.diff，完美兼容字符串和数字类型的 Sample_I
    is_boundary = group_ids[:-1] != group_ids[1:]
    boundaries = np.where(is_boundary)[0] + 1
    boundaries = np.concatenate(([0], boundaries, [len(ice_inc)]))
    cumulative_ice = np.zeros(len(ice_inc))
    
    for i in range(len(boundaries) - 1):
        start, end = boundaries[i], boundaries[i+1]
        cumulative_ice[start:end] = fast_melting_accumulation(ice_inc[start:end], temp[start:end])
    
    weather_df['cumulative_ice_mm'] = cumulative_ice
    weather_df['cumulative_ice_mm'] = np.where(weather_df['cumulative_ice_mm'] < 0.001, 0, weather_df['cumulative_ice_mm'])
    
    # 4. 恢复原始索引顺序
    return weather_df.sort_index()


def calculate_hourly_ice_thickness(total_precip_m, wind_speed_10m, precip_type):
    #reference: CRREL simple model for estimating Ice Accretion thickness
    # Ptype: 3 = Freezing rain; 5 = Snow; 6 = Wet snow; 7 = Mixture of rain and snow
    # 1. 将降水量单位从 m 转换为 mm
    p_j = total_precip_m * 1000 
    
    # 2. 向量化计算所有行的理论积冰量
    base_ice_increment = 0.35 * p_j * np.sqrt(1 + (wind_speed_10m / 5)**2)
    
    # 3. 使用 np.where 应用条件：
    # 如果 precip_type == 3，返回计算出的 base_ice_increment
    # 否则返回 0.0
    actual_ice_increment = np.where(precip_type == 3, base_ice_increment, 0.0)
    
    return actual_ice_increment

def calculate_failure_prob(wind_speed, ice_thickness):
    #reference "Characterizing probability of failure of transmission tower systems under multiple climatic hazards: Wind and ice"
    #units: ice_mm, wind_m/s
    
    # 1. 失效概率矩阵
    ice_axes = np.array([0, 40, 80])
    wind_axes = np.array([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    values = np.array([
        [0, 0, 0.02, 0.15, 0.28, 0.54, 0.8, 0.875, 0.95, 0.975, 1],
        [0, 0.01, 0.03, 0.15, 0.55, 0.9, 1, 1, 1, 1, 1],
        [0.01, 0.205, 0.4, 0.65, 0.9, 0.95, 1, 1, 1, 1, 1]
    ])
    
    # 2. 插值
    interpolator = RegularGridInterpolator((ice_axes, wind_axes), values, bounds_error=False, fill_value=None)
    
    # 3. 截断越界值
    w_clipped = np.clip(wind_speed, 0, 100)
    i_clipped = np.clip(ice_thickness, 0, 80)
    
    # 4. 组合坐标点并进行二维插值
    points = np.column_stack((i_clipped, w_clipped))
    probs = interpolator(points)
    
    # 5. 确保输出在 0 到 1 之间
    return np.clip(probs, 0.0, 1.0)

if __name__ == "__main__":
    main()
