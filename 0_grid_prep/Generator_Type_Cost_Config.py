import numpy as np
import pandas as pd
import time
import os
import requests
from scipy.stats import norm

# =====================================================================
# Paths (relative, repo-friendly — point elsewhere or move to a config module)
# =====================================================================
GEN_RAW   = "generator2025_data.csv"                 # raw Texas2k generator table
BUS_DATA  = "bus2025_data.csv"                        # bus table w/ Latitude/Longitude/Substation_Number
IEC_CACHE = "wind_iec_cache.csv"                      # auto-managed cache of NASA POWER lookups

# ---- 抽样控制 ----
SEED       = 42            # 固定种子, 可复现
DIST_KIND  = "lognormal"   # "lognormal" (默认, 天生正/右偏, 贴合 EIA 样本形状)
                           # 或 "normal" (截断于 >0; CV 大的 NGCT 会产生很多低值,不推荐)

# ---- 风机 IEC 分类控制 (原 IEC_CLASS.py 融合进来) ----
IEC_SLEEP  = 0.35          # NASA POWER 调用间隔 (s), 防限流
SHEAR_EXP  = 0.14          # 幂律风切变指数 (德州开阔地表), 50m -> 100m 外推


def _draw(mean, sd, n, rng):
    """按 arithmetic mean & sd 抽样。lognormal 用矩匹配参数化, 保证期望 mean/sd。"""
    if DIST_KIND == "lognormal":
        sigma = np.sqrt(np.log(1.0 + (sd / mean) ** 2))
        mu    = np.log(mean) - 0.5 * sigma ** 2
        return rng.lognormal(mu, sigma, n)
    elif DIST_KIND == "normal":
        return np.clip(rng.normal(mean, sd, n), 1e-3, None)
    raise ValueError(DIST_KIND)


# =====================================================================
# 风机 IEC 分类 (原 IEC_CLASS.py 的两个函数, 原样搬入)
# =====================================================================
def get_mean_wind_speed_100m(lat, lon):
    """
    获取 100m 高度年平均风速。GWA 限制批量接口, 改用 NASA POWER 开放气候数据:
    取 50m 年平均风速 (WS50M), 再以幂律外推到 100m 轮毂高度。
    """
    url = (
        "https://power.larc.nasa.gov/api/temporal/climatology/point"
        f"?parameters=WS50M&community=RE&longitude={lon}&latitude={lat}&format=JSON"
    )
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        ws_50m = data["properties"]["parameter"]["WS50M"]["ANN"]
        ws_100m = ws_50m * ((100 / 50) ** SHEAR_EXP)
        return round(ws_100m, 2)
    except Exception as e:
        print(f"  [IEC] 风速获取失败 ({lat}, {lon}): {e}")
        return None


def assign_iec_class(wind_speed):
    """风速 -> IEC Class (对应报告 Table 1/2 的逻辑)。"""
    if pd.isna(wind_speed) or wind_speed is None:
        return "Unknown"
    if wind_speed > 8.5:
        return "Class 1"
    elif 7.5 <= wind_speed <= 8.5:
        return "Class 2"
    else:
        return "Class 3"


def classify_wind_gens(gen_df, bus_df):
    """
    给风机行赋 IEC class, 写入 GENERATOR_TYPE。
    - 坐标来自 bus 表 (BUS_I -> Latitude/Longitude/Substation_Number)
    - 按 Substation_Number 去重, 每个站点只调用一次 NASA POWER
    - 结果落盘到 IEC_CACHE, 下次直接命中, 不重复打 API
    返回: dict {GEN_I: IEC_Class}
    """
    wind_mask = gen_df["FUEL_TYPE"] == "WND (Wind)"
    wind = gen_df.loc[wind_mask, ["GEN_I", "BUS_I"]].merge(
        bus_df[["BUS_I", "Substation_Number", "Latitude", "Longitude"]],
        on="BUS_I", how="left",
    )

    # 唯一站点 (有坐标的)
    sites = (
        wind.dropna(subset=["Latitude", "Longitude"])
            .drop_duplicates("Substation_Number")
            .set_index("Substation_Number")
    )

    # 读缓存
    if os.path.exists(IEC_CACHE):
        cache = pd.read_csv(IEC_CACHE).set_index("Substation_Number")
        print(f"[IEC] 命中缓存 {len(cache)} 个站点 ({IEC_CACHE})")
    else:
        cache = pd.DataFrame(columns=["Mean_Wind_Speed_100m", "IEC_Class"])
        cache.index.name = "Substation_Number"

    # 缓存未覆盖的站点才查 API
    todo = [s for s in sites.index if s not in cache.index]
    print(f"[IEC] 需查询 {len(todo)} / {len(sites)} 个风机站点 ...")
    for i, sub in enumerate(todo, 1):
        lat, lon = sites.loc[sub, "Latitude"], sites.loc[sub, "Longitude"]
        print(f"  [IEC] {i}/{len(todo)} 站点 {sub}: ({lat:.3f}, {lon:.3f})")
        ws = get_mean_wind_speed_100m(lat, lon)
        cache.loc[sub] = [ws, assign_iec_class(ws)]
        time.sleep(IEC_SLEEP)

    # 存回缓存
    cache.reset_index().to_csv(IEC_CACHE, index=False)

    # 站点 -> class, 映射回每台风机
    sub2class = cache["IEC_Class"].to_dict()
    wind["IEC_Class"] = wind["Substation_Number"].map(sub2class).fillna("Unknown")
    n_unknown = (wind["IEC_Class"] == "Unknown").sum()
    if n_unknown:
        print(f"[IEC] 警告: {n_unknown} 台风机无有效分类 (缺坐标或 API 失败) -> 'Unknown'")
    print("[IEC] 分类分布:\n", wind["IEC_Class"].value_counts().to_string())
    return dict(zip(wind["GEN_I"], wind["IEC_Class"]))


def main():
    gen_df = pd.read_csv(GEN_RAW)   # 包含 gen_id, bus_number, resource_type, capacity
    bus_df = pd.read_csv(BUS_DATA)  # 坐标 + substation, 供风机 IEC 分类用

    ## ---------------------------------------------------------
    ##
    ## 赋予技术类型 (GENERATOR_TYPE)
    ##
    ## ---------------------------------------------------------

    # ---------------------------------------------------------
    # 天然气机组：依额定容量按经验概率分布赋类型: steam_turbine / combined_cycle / fired_combustion
    # ---------------------------------------------------------
    thermal_gens = gen_df[gen_df['FUEL_TYPE'] == 'NG (Natural Gas)'].copy()

    # --- 由 EIA-860 分析得出的 cluster 参数 ---
    clusters = {
        "steam_turbine":    {"mean": 232.0, "std": 188.0, "weight": 0.1275},
        "combined_cycle":   {"mean": 167.0, "std": 90.5,  "weight": 0.3876},
        "fired_combustion": {"mean": 65.6,  "std": 42.5,  "weight": 0.4849},
    }

    labels  = list(clusters)
    means   = np.array([clusters[l]["mean"]   for l in labels])
    stds    = np.array([clusters[l]["std"]    for l in labels])
    weights = np.array([clusters[l]["weight"] for l in labels])
    weights = weights / weights.sum()

    x = thermal_gens["PMAX"].to_numpy(dtype=float)
    likelihood = np.stack(
        [weights[k] * norm.pdf(x, means[k], stds[k]) for k in range(len(labels))],
        axis=1
    )
    denom = likelihood.sum(axis=1, keepdims=True)
    denom[denom == 0] = 1e-300
    resp = likelihood / denom
    thermal_gens["GENERATOR_TYPE"] = [labels[k] for k in resp.argmax(axis=1)]

    print(thermal_gens.groupby("GENERATOR_TYPE")["PMAX"].describe()[["count", "mean", "std", "min", "max"]])
    gen_df = gen_df.merge(thermal_gens[["GEN_I", "GENERATOR_TYPE"]], on="GEN_I", how="left")

    # ---------------------------------------------------------
    # 风机：IEC class 作为 GENERATOR_TYPE (原 IEC_CLASS.py 融合于此)
    #   - Class 1/2/3 / Unknown 写入 GENERATOR_TYPE, 供 wind time series 选功率曲线
    # ---------------------------------------------------------
    geni2iec = classify_wind_gens(gen_df, bus_df)
    wind_mask = gen_df["FUEL_TYPE"] == "WND (Wind)"
    gen_df.loc[wind_mask, "GENERATOR_TYPE"] = gen_df.loc[wind_mask, "GEN_I"].map(geni2iec)

    # =========================================================
    # 储能机组: 按功率赋储能时长(1..6h), 再算额定能量
    # =========================================================
    storage_gens = gen_df[gen_df["FUEL_TYPE"] == "MWH (Electricity use for Energy Storage)"].copy()

    duration_hours  = np.array([1, 2, 3, 4, 5, 6])
    duration_counts = np.array([313, 346, 63, 370, 31, 8], dtype=float)
    duration_probs  = duration_counts / duration_counts.sum()

    rng = np.random.default_rng(42)   # 固定种子
    storage_gens["Storage_Duration_h"] = rng.choice(
        duration_hours, size=len(storage_gens), p=duration_probs
    )
    storage_gens["Storage_MWH"] = storage_gens["PMAX"].to_numpy(dtype=float) * storage_gens["Storage_Duration_h"]
    gen_df = gen_df.merge(
        storage_gens[["GEN_I", "Storage_Duration_h", "Storage_MWH"]], on="GEN_I", how="left"
    )

    ## ---------------------------------------------------------
    ##
    ## 更新发电成本 (GENERATOR_COST)
    ##
    ## ---------------------------------------------------------

    # --- fuel price cf ($/MMBtu) ---
    FUEL_PRICE = {
        "NG":  4.18,    # EIA EPM Table 4.13.A, Mar 2025
        "BIT": 2.02,    # EIA EPM Table 4.10.A, Mar 2025
        "NUC": 0.58,    # EIA nuclear data & statistics
        "DFO": 17.13,   # EIA EPM Table 4.2
        "OBL": 100,     # 未定义, 占位
        "OTH": 100,     # 未定义, 占位
    }

    # --- (A) 有 EIA-923 TX 分布的四类: 热耗 ~ 分布(mean, sd), MMBtu/MWh ---
    HEAT_RATE_DIST_MMBTU_PER_MWH = {
        ("NG",  "combined_cycle"):   {"mean": 6.891,  "sd": 1.169},
        ("NG",  "fired_combustion"): {"mean": 9.867,  "sd": 3.862},
        ("NG",  "steam_turbine"):    {"mean": 11.552, "sd": 2.569},
        ("BIT", None):               {"mean": 11.136, "sd": 0.867},
    }

    # --- (B) 无分布数据的燃料: 点值 (EIA Table 8.2, Btu/kWh) ---
    HEAT_RATE_BTU_PER_KWH = {
        ("NUC", None): 10443,
        ("DFO", None): 13083,
        ("OBL", None): 25000,
        ("OTH", None): 25000,
    }
    BTU_PER_KWH_TO_MMBTU_PER_MWH = 1.0 / 1000.0
    B2 = 0.0

    gen_df["FUEL_SHORT"]    = gen_df["FUEL_TYPE"].str.extract(r"^(\w+)")
    gen_df["fuel_price_cf"] = 0.0
    gen_df["heat_rate_b1"]  = 0.0
    gen_df["heat_rate_b2"]  = B2

    rng = np.random.default_rng(SEED)
    report = []

    # (A) 分布抽样 —— 顺序固定以保证可复现
    for (fuel, gtype), p in HEAT_RATE_DIST_MMBTU_PER_MWH.items():
        if gtype is None:
            mask = gen_df["FUEL_SHORT"] == fuel
        else:
            mask = (gen_df["FUEL_SHORT"] == fuel) & (gen_df["GENERATOR_TYPE"] == gtype)
        idx = gen_df.index[mask]
        if len(idx) == 0:
            continue
        hr = _draw(p["mean"], p["sd"], len(idx), rng)
        gen_df.loc[idx, "heat_rate_b1"]  = hr
        gen_df.loc[idx, "fuel_price_cf"] = FUEL_PRICE[fuel]
        report.append((f"{fuel}/{gtype or '-'}", len(idx), p, hr, 0))

    # (B) 点值
    for (fuel, gtype), btu_per_kwh in HEAT_RATE_BTU_PER_KWH.items():
        b1 = btu_per_kwh * BTU_PER_KWH_TO_MMBTU_PER_MWH
        mask = (gen_df["FUEL_SHORT"] == fuel)
        gen_df.loc[mask, "heat_rate_b1"]  = b1
        gen_df.loc[mask, "fuel_price_cf"] = FUEL_PRICE[fuel]

    # gencost 多项式: c1 = cf*b1 ; c2 = cf*b2 ; 非传统机组自然保持 0
    gen_df["c1"] = (gen_df["fuel_price_cf"] * gen_df["heat_rate_b1"]).round(4)
    gen_df["c2"] = (gen_df["fuel_price_cf"] * gen_df["heat_rate_b2"]).round(6)

    gen_df = gen_df.drop(columns=["FUEL_SHORT"])
    gen_df.to_csv("generator2025_data_modified.csv", index=False)


if __name__ == "__main__":
    main()
