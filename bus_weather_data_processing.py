import earthkit.data as ekd
import xarray as xr
import pandas as pd
import numpy as np
import pytz
import warnings
    
def main():
    TEXAS_TZ = pytz.timezone("America/Chicago")
    INSTANT_PARAMS = ["sd",  "2t", "2d", "10v","10u","100v","100u", "sp"]
    ACCUM_PARAMS   = ["ssrd", "10fg"]
    ALL_PARAMS     = INSTANT_PARAMS + ACCUM_PARAMS

    # ── 1. 读取 grib 并构建 Dataset ───────────────────────────────────────────────

    ds_raw = ekd.from_source("file", "25010115.grib")

    def extract_param(ds, shortName):
        fields = ds.sel(shortName=shortName)
        records = []
        for f in fields:
            date_str = str(f.metadata("validityDate"))
            time_int = int(f.metadata("validityTime"))
            dt_utc = pd.Timestamp(
                year=int(date_str[:4]),
                month=int(date_str[4:6]),
                day=int(date_str[6:8]),
                hour=time_int // 100,
                minute=time_int % 100,
            )
            records.append((dt_utc, f.to_numpy()))

        lats, lons = fields[0].grid_points()
        lat_vals = np.unique(lats)[::-1]   # 北→南
        lon_vals = np.unique(lons)

        times = [r[0] for r in records]
        data  = np.stack([r[1] for r in records], axis=0)

        return xr.DataArray(
            data,
            dims=["time", "latitude", "longitude"],
            coords={
                "time": pd.DatetimeIndex(times),
                "latitude": lat_vals,
                "longitude": lon_vals,
            },
            name=shortName,
        )

    arrays = {}
    master_lat = None
    master_lon = None

    for i, param in enumerate(ALL_PARAMS):
        print(f"Processing {param}...")
        da = extract_param(ds_raw, param)
        
        # 1. 强制对齐经纬度：以第一个变量（例如 'sd'）的经纬度为基准，覆盖后续所有变量的微小浮点误差
        if i == 0:
            master_lat = da.latitude
            master_lon = da.longitude
        else:
            da = da.assign_coords({
                "latitude": master_lat,
                "longitude": master_lon
            })
            
        # 2. 统一处理时间乱序：不仅是 ssrd，新加的 tp, 10fg 等所有变量都统一按时间轴排序
        arrays[param] = da.sortby("time")

    xr_ds = xr.Dataset(arrays)

    # ── 2. 读取你的坐标 CSV ───────────────────────────────────────────────────────

    coords_df = pd.read_csv("coordinates.csv")   # 需有 lat, lon 两列
    lats = coords_df["lat"].values
    lons = coords_df["lon"].values

    # ── 3. 经度对齐（-180~180 → 0~360）──────────────────────────────────────────

    if xr_ds.longitude.max() > 180:
        lons = lons % 360

    # ── 4. 矢量化提取 ─────────────────────────────────────────────────────────────

    points_data = xr_ds.sel(
        latitude=xr.DataArray(lats, dims="points"),
        longitude=xr.DataArray(lons, dims="points"),
        method="nearest"
    )

    result_df = points_data.to_dataframe().reset_index()
    result_df["original_lat"] = lats[result_df["points"].values]
    result_df["original_lon"] = lons[result_df["points"].values]


    # ── 5. NaN 修补 ───────────────────────────────────────────────────────────────

    warnings.filterwarnings("ignore", message="Mean of empty slice")
    print("开始扫描并修补缺失数据...")

    nan_coords = (
        result_df[result_df["2t"].isna()][["original_lat", "original_lon"]]
        .drop_duplicates()

    )
    print(f"发现 {len(nan_coords)} 个存在缺测的边界坐标...")

    for _, row in nan_coords.iterrows():
        target_lat = row["original_lat"]
        target_lon = row["original_lon"]

        lat_max, lat_min = target_lat + 0.15, target_lat - 0.15
        search_lon = target_lon % 360 if xr_ds.longitude.max() > 180 else target_lon
        lon_min, lon_max = search_lon - 0.15, search_lon + 0.15

        nearby_grid = xr_ds.sel(
            latitude=slice(lat_max, lat_min),
            longitude=slice(lon_min, lon_max)
        )
        nearby_valid_data = nearby_grid.mean(dim=["latitude", "longitude"], skipna=True)
        patch_df = nearby_valid_data.to_dataframe().reset_index()


        mask = (
            (result_df["original_lat"] == target_lat) &
            (result_df["original_lon"] == target_lon)
        )
        

        # ★ 关键修改：时间列名从 forecast_reference_time+step 改为 time
        time_cols = ["time"]

        target_subset = result_df.loc[mask, time_cols].copy()
        target_subset["row_id"] = target_subset.index

        for var in ALL_PARAMS:
            merged = pd.merge(
                target_subset,
                patch_df[time_cols + [var]],
                on=time_cols,
                how="left"
            )
            merged = merged.set_index("row_id")
            result_df.loc[mask, var] = merged[var]

    remaining_nans = result_df["2t"].isna().sum()
    if remaining_nans > 0:
        print(f"修补完成，仍有 {remaining_nans} 行缺失（可能完全位于深海）。")
    else:
        print("修补完美完成！")

    # ── 6. UTC → 德州时间，导出 ──────────────────────────────────────────────────

    result_df["time"] = (
        pd.to_datetime(result_df["time"])
        .dt.tz_localize("UTC")
        .dt.tz_convert(TEXAS_TZ)
        .dt.tz_localize(None) 
    )

    # 建立 points → Substation_Number 的映射
    substation_map = dict(zip(coords_df.index, coords_df["Substation_Number"]))

    # 替换 points 列
    result_df["points"] = result_df["points"].map(substation_map)
    result_df = result_df.rename(columns={"points": "Substation_Number"}) 
 
    
    # 整理输出列顺序
    result_df = result_df.rename(columns={"time": "datetime"})
    result_df ['datetime'] = pd.to_datetime(result_df['datetime'])
    # 过滤时间，小数位
    result_df = result_df[result_df["datetime"] >= "2025-01-01"]
    result_df['date'] = result_df['datetime'].dt.date
    result_df['time'] = result_df['datetime'].dt.strftime('%H:%M')
    
    #整合风速
    result_df["10Wind"] = np.sqrt(result_df["10u"]**2 + result_df["10v"]**2)
    result_df["100Wind"] = np.sqrt(result_df["100u"]**2 + result_df["100v"]**2)
    result_df["sd"] = result_df["sd"].round(4)
    result_df["2t"] = result_df["2t"].round(4)
    result_df["2d"] = result_df["2d"].round(4)
    result_df["10Wind"] = result_df["10Wind"].round(4)
    result_df["100Wind"] = result_df["100Wind"].round(4)
    result_df["sp"] = result_df["sp"].round(4)
    result_df["ssrd"] = result_df["ssrd"].round(4)
    
    
    result_df = result_df[["date", "time", "Substation_Number", "2t", "2d", "10Wind", "100Wind", "sp", "sd", "ssrd"]]
    print(result_df.head(15))
    result_df.to_csv("bus_weather_data_25010115.csv", index=False)
    
    
if __name__ == "__main__":
    main()