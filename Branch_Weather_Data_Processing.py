import earthkit.data as ekd
import xarray as xr
import pandas as pd
import numpy as np
import pytz
import warnings
    
def main():
    TEXAS_TZ = pytz.timezone("America/Chicago")
    
    # ★ 修改点 1：更新你需要提取的参数列表
    INSTANT_PARAMS = ["ptype", "10v", "10u", "2t"]
    ACCUM_PARAMS   = ["10fg", "tp"]
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
        
        # 1. 强制对齐经纬度
        if i == 0:
            master_lat = da.latitude
            master_lon = da.longitude
        else:
            da = da.assign_coords({
                "latitude": master_lat,
                "longitude": master_lon
            })
            
        # 2. 统一处理时间乱序
        arrays[param] = da.sortby("time")

    xr_ds = xr.Dataset(arrays)

    # ── 2. 读取分支坐标 CSV ───────────────────────────────────────────────────────

    coords_df = pd.read_csv("branch2025_weatherSamplingCoordinates.csv")  
    
    lats = coords_df["Sample_Lat"].values
    lons = coords_df["Sample_Lon"].values

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
    result_df["Sample_Lat"] = lats[result_df["points"].values]
    result_df["Sample_Lon"] = lons[result_df["points"].values]


    # ── 5. NaN 修补 ───────────────────────────────────────────────────────────────

    warnings.filterwarnings("ignore", message="Mean of empty slice")
    print("开始扫描并修补缺失数据...")

    # ★ 修改点 2：将写死的 "2t" 改为动态读取 ALL_PARAMS 的第一个参数来判断缺测
    check_var = ALL_PARAMS[0]
    
    nan_coords = (
        result_df[result_df[check_var].isna()][["Sample_Lat", "Sample_Lon"]]
        .drop_duplicates()
    )
    print(f"发现 {len(nan_coords)} 个存在缺测的边界坐标...")

    for _, row in nan_coords.iterrows():
        target_lat = row["Sample_Lat"]
        target_lon = row["Sample_Lon"]

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
            (result_df["Sample_Lat"] == target_lat) &
            (result_df["Sample_Lon"] == target_lon)
        )
        
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

    # ★ 修改点 3：最后的缺测检查同样使用动态变量
    remaining_nans = result_df[check_var].isna().sum()
    if remaining_nans > 0:
        print(f"修补完成，仍有 {remaining_nans} 行缺失（可能完全位于深海）。")
    else:
        print("修补完美完成！")

    # ── 6. UTC → 德州时间及属性合并 ──────────────────────────────────────────────

    result_df["time"] = (
        pd.to_datetime(result_df["time"])
        .dt.tz_localize("UTC")
        .dt.tz_convert(TEXAS_TZ)
        .dt.tz_localize(None) 
    )

    # 提取分支属性并合并
    coords_meta = coords_df[['BRANCH_I', 'Sample_Pnts', 'Sample_ind']].copy()
    coords_meta.index.name = 'points'
    
    # 基于 points 索引将分支属性拼接到气象数据上
    result_df = result_df.merge(coords_meta, on='points', how='left')
    
    result_df["10Wind"] = np.sqrt(result_df['10u']**2 + result_df['10v']**2)
    result_df["2t"] = result_df["2t"] - 273.15
    
    

    # 整理输出列顺序
    result_df = result_df.rename(columns={"time": "datetime"})
    result_df["Sample_I"] = ((result_df.index) % 4777 + 1)
    result_df ['datetime'] = pd.to_datetime(result_df['datetime'])
    # 过滤时间，小数位
    result_df = result_df[result_df["datetime"] >= "2025-01-01"]
    result_df['date'] = result_df['datetime'].dt.date
    result_df['time'] = result_df['datetime'].dt.strftime('%H:%M')
    result_df["tp"] = result_df["tp"].round(6)
    result_df["2t"] = result_df["2t"].round(3)
    result_df["10Wind"] = result_df["10Wind"].round(3)

    result_df = pd.merge(
        result_df,
        coords_df, 
        on=['BRANCH_I', 'Sample_ind'], 
        how='left'
    )
    result_df = result_df.rename(columns={'Sample_Pnts_y': 'Sample_Pnts'})
    output_cols = ["date", "time", "Sample_I","BRANCH_I",'Sample_Pnts', "Tow_Patched", "ptype", "10Wind", "2t", "tp"]
    result_df = result_df[output_cols]
    
    print(result_df.head(20))
    result_df.to_csv("branch_weather_data_25010115.csv", index=False)
    
if __name__ == "__main__":
    main()
