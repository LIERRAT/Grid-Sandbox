import pandas as pd
import numpy as np
from collections import defaultdict

def get_rates(fuel, technology):
    # return lam_D, lam_O, mu_D, mu_O
    if technology == "fired_combustion":            # NG CT
        return 0.000293026, 0.001352,    0.071981777, 0.075551871
    if technology == "combined_cycle":              # NG CC
        return 0.000946219, 0.000474131, 0.124529317, 0.106373223
    if technology == "steam_turbine" and (fuel == "NG (Natural Gas)"):  # NG steam
        return 0.000567465, 0.000492591, 0.026282168, 0.030109599
    if technology == "steam_turbine" and (fuel == "BIT (Bituminous Coal)"):  # coal steam
        return 0.00407494,  0.000699684, 0.091274616, 0.025695313
    if fuel == "WAT (Water)":                       # hydro
        return 0.000241383, 0.000348837, 0.024249061, 0.043408049
    if fuel == "NUC (Nuclear)":                     # nuclear
        return 0.000576, 0.000114, 0.005113, 0.007813
    if fuel == "DFO (Distillate Fuel Oil)":         # fuel oil
        return 0.000033, 0.000210, 0.00247, 0.024025
    
    raise ValueError(f"no rates for fuel={fuel}, tech={technology}")

def simulate_generator(temps, fuel, technology, pmax, lam_D, lam_O, mu_D, mu_O, hours, rng=None):
    """
    三态序贯 MC: Available <-> Derate, Available <-> Outage (D/O 之间不转移)
    lam_D, lam_O: Available->Derate / Available->Outage 转移率 (1/h)
    mu_D,  mu_O : Derate->Available / Outage->Available 修复率 (1/h)
    返回: 长度 hours+3 的数组 [cap_0..cap_{h-1}, down_hours, n_derate, n_outage]
    """
    if rng is None:
        rng = np.random.default_rng()

    _temp_pts = np.array([-15,-10,-5,0,5,10,15,20,25,30,35])
    _CC = np.array([0.15,0.075,0.05,0.033,0.03,0.03,0.03,0.033,0.033,0.04,0.075])
    _CT = np.array([0.2,0.1,0.05,0.03,0.03,0.03,0.03,0.03,0.03,0.03,0.06])
    _ST = np.array([0.13,0.11,0.1,0.1,0.09,0.09,0.09,0.09,0.1,0.13,0.14])
    _HD = np.array([0.07,0.03,0.025,0.025,0.025,0.025,0.025,0.025,0.025,0.025,0.08])
    _NU = np.array([0.025,0.025,0.025,0.025,0.025,0.025,0.025,0.025,0.05,0.06,0.125])

    def derate_ratio(temp):
        if technology == "combined_cycle":   pts = _CC
        elif technology == "fired_combustion": pts = _CT
        elif technology == "steam_turbine":  pts = _ST
        elif fuel == "WAT (Water)":          pts = _HD
        elif fuel == "NUC (Nuclear)":          pts = _NU
        elif fuel == "DFO (Distillate Fuel Oil)": pts = _CT
        return np.interp(temp, _temp_pts, pts)

    capacity = np.full(hours + 3, pmax, dtype=float)
    capacity[-3] = 0.0
    n_derate = n_outage = 0
    lam_tot = lam_D + lam_O

    # 初始状态: 三态稳态分布  π_A:π_D:π_O = 1 : λ_D/μ_D : λ_O/μ_O
    wD, wO = lam_D / mu_D, lam_O / mu_O
    Z = 1.0 + wD + wO
    u = rng.random()
    state = "Available" if u < 1/Z else ("Derate" if u < (1+wD)/Z else "Outage")

    t = 0.0
    while t < hours:
        if state == "Available":
            # 竞争风险: 离开可用态时间 ~ Exp(λ_D+λ_O)
            t = min(t - np.log(rng.random()) / lam_tot, hours)
            if t >= hours:
                break
            if rng.random() < lam_D / lam_tot:
                state = "Derate"; n_derate += 1
            else:
                state = "Outage"; n_outage += 1
        else:
            repair_rate = mu_D if state == "Derate" else mu_O
            t_end = min(t - np.log(rng.random()) / repair_rate, hours)
            start = int(t)
            end   = min(max(int(t_end), start + 1), hours)   # 至少 1 小时，且不越界
            capacity[-3] += (end - start)
            if state == "Outage":
                capacity[start:end] = 0.0
            else:
                seg = temps[start:end]
                min_temp = seg.min() if len(seg) else temps[start]
                capacity[start:end] = pmax * (1.0 - derate_ratio(min_temp)) 
            t = t_end
            state = "Available"

    capacity[-2], capacity[-1] = n_derate, n_outage
    return capacity

def main():
    # 天然气发电站，煤电发电站，和水电发电站在正常情况下的发电表现 regular performance curve

    # ---------------------------------------------------------
    # Step 1: 整理表格
    # ---------------------------------------------------------
    weather_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus_weather_data_25010115.csv")  # 包含 substation/bus, 10u, 10v, 2t, sp 等
    bus_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus2025_data.csv")  # 包含 bus_number, substation_id
    gen_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\generator2025_data_modified.csv")  # 包含 gen_id, bus_number, resource_type, capacity
    thermal_gens = gen_df[(gen_df['FUEL_TYPE'] == 'NG (Natural Gas)') | (gen_df['FUEL_TYPE'] == 'BIT (Bituminous Coal)') | (gen_df['FUEL_TYPE'] == 'NUC (Nuclear)') | (gen_df['FUEL_TYPE'] == 'DFO (Distillate Fuel Oil)')].copy()
    hydro_annual_cf = 0.09
    hydro_gens = gen_df[(gen_df['FUEL_TYPE'] == 'WAT (Water)')].copy()
    hydro_gens["PMAX"] = hydro_gens["PMAX"] * hydro_annual_cf  

    # 纵向拼接成一张机组表，保留单一的 PMAX 列
    thermal_hydro_gens = pd.concat([thermal_gens, hydro_gens], ignore_index=True)
    thermal_hydro_gens = pd.merge(thermal_hydro_gens, bus_df, on='BUS_I', how='left')
    thermal_hydro_gens = pd.merge(thermal_hydro_gens, weather_df, on='Substation_Number', how='left')
    thermal_hydro_gens["temp_C"] = thermal_hydro_gens["2t"] - 273.15
    thermal_hydro_gens = thermal_hydro_gens[["date", "time", "BUS_I", "GEN_I", "Substation_Number","GEN_STATUS", "PMAX", "FUEL_TYPE", "GENERATOR_TYPE", "temp_C"]].copy()
    
    hours  = 354 #200000
    trials = 1
    rng = np.random.default_rng(0)          # 单一 rng，结果可复现

    nd = no = ndhr = 0
    gen_ids = thermal_hydro_gens["GEN_I"].unique()
    gen_long_parts = []  
    gen_fault_record = []
    

    stat = defaultdict(lambda: {"nd":0.0,"no":0.0,"lost":0.0,"caph":0.0,"n":0})

    for trial in range(trials):
        for gid in gen_ids:
            gen_row = thermal_hydro_gens[thermal_hydro_gens["GEN_I"] == gid].iloc[0:hours]
            temps      = gen_row["temp_C"].values # np.full(hours, 5) #
            pmax       = gen_row["PMAX"].iloc[0]
            fuel       = gen_row["FUEL_TYPE"].iloc[0]
            technology = gen_row["GENERATOR_TYPE"].iloc[0]
            lam_D, lam_O, mu_D, mu_O = get_rates(fuel, technology)
            cap_series = simulate_generator(temps, fuel, technology, pmax, lam_D, lam_O, mu_D, mu_O, hours, rng=rng)

            nd   += cap_series[-2]
            no   += cap_series[-1]
            ndhr += cap_series[-3]
            lost_mwh     = (pmax - cap_series[:hours]).sum()   # 等效损失容量
            if (cap_series[-1] + cap_series[-2]) > 0:
                gen_long_parts.append(pd.DataFrame({
                    "BUS_I":    gen_row["BUS_I"].iloc[0],
                    "GEN_I":    gid,
                    "PMAX":     pmax,
                    "hour":     np.arange(hours),
                    "capacity": cap_series[:hours],
                    # "trial":  trial,      # 想区分不同 trial 就打开这行
                }))
                gen_fault_record.append((gid, fuel, technology, pmax,cap_series[-2], cap_series[-1], cap_series[-3]))
                        
            
            # 循环体内，算完 lost_mwh 之后加：
            k = (fuel, technology)
            stat[k]["nd"]   += cap_series[-2]
            stat[k]["no"]   += cap_series[-1]
            stat[k]["lost"] += lost_mwh
            stat[k]["caph"] += hours * pmax * 0.63
            stat[k]["n"]    += 1

    # 循环结束后：
    print(f"\n{'type':<40}{'台次':>7}{'降额/台':>9}{'停机/台':>6}{'WEFOR':>9}")
    for k,v in stat.items():
        n = v["n"]
        print(f"{str(k):<40}{n:>7}{v['nd']/n:>9.2f}{v['no']/n:>9.2f}{v['lost']/v['caph']:>9.4f}")

    thermal_hydro_time_series_df = pd.concat(gen_long_parts, ignore_index=True)
    
    # 1) 从 weather_df 第一行取起始时间
    start = pd.to_datetime(
        str(weather_df["date"].iloc[0]) + " " + str(weather_df["time"].iloc[0])
    )   # -> 2025-01-01 00:00:00

    # 2) 起始时间 + hour 小时
    dt = start + pd.to_timedelta(thermal_hydro_time_series_df["hour"], unit="h")

    # 3) 拆成 date 列和 time 列
    thermal_hydro_time_series_df["date"] = (dt.dt.month.astype(str) + "/" + dt.dt.day.astype(str) + "/" + dt.dt.year.astype(str)) # 1/1/2025
    thermal_hydro_time_series_df["time"] = (dt.dt.hour.astype(str) + ":" + dt.dt.minute.astype(str).str.zfill(2)) # 0:00

    thermal_hydro_time_series_df = thermal_hydro_time_series_df[["BUS_I", "GEN_I", "PMAX", "date", "time", "capacity"]]
    thermal_hydro_time_series_df.to_csv("conventional_generation_down_timeseries.csv", index=False)
    
    gen_fault_record_df = pd.DataFrame(gen_fault_record, columns=["GEN_ID", "FUEL_TYPE", "GENERATOR_TYPE", "PMAX", "Derate", "Outage", "Duration"])
    gen_fault_record_df.to_csv("gen_fault_record.csv", index=False)

    print(f"平均降额次数: {nd/trials}, 平均停机次数: {no/trials}, 平均降额+停机小时占比: {ndhr/(len(gen_ids)*trials*hours)}, 单次机组数: {len(gen_ids)}, 测试次数: {trials}")
    
if __name__ == "__main__":
    main()
