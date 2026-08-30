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
    WEATHER_RAW = "25010115.grib" # <<< input the weather data from CDS
    # ── 1. Read the grib and build the Dataset ───────────────────────────────────────────────

    ds_raw = ekd.from_source("file", WEATHER_RAW)

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
        lat_vals = np.unique(lats)[::-1]   # north -> south
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
        
        # 1. Force lat/lon alignment: use the first variable's (e.g. 'sd') lat/lon as the reference, overriding the tiny floating-point discrepancies of all later variables
        if i == 0:
            master_lat = da.latitude
            master_lon = da.longitude
        else:
            da = da.assign_coords({
                "latitude": master_lat,
                "longitude": master_lon
            })
            
        # 2. Uniformly handle out-of-order time: not just ssrd, but all newly added variables like tp, 10fg are sorted along the time axis
        arrays[param] = da.sortby("time")

    xr_ds = xr.Dataset(arrays)

    # ── 2. Read your coordinate CSV ───────────────────────────────────────────────────────

    coords_df = pd.read_csv("coordinates.csv")   # must have the two columns lat, lon
    lats = coords_df["lat"].values
    lons = coords_df["lon"].values

    # ── 3. Longitude alignment (-180~180 -> 0~360) ──────────────────────────────────────────

    if xr_ds.longitude.max() > 180:
        lons = lons % 360

    # ── 4. Vectorized extraction ─────────────────────────────────────────────────────────────

    points_data = xr_ds.sel(
        latitude=xr.DataArray(lats, dims="points"),
        longitude=xr.DataArray(lons, dims="points"),
        method="nearest"
    )

    result_df = points_data.to_dataframe().reset_index()
    result_df["original_lat"] = lats[result_df["points"].values]
    result_df["original_lon"] = lons[result_df["points"].values]


    # ── 5. NaN patching ───────────────────────────────────────────────────────────────

    warnings.filterwarnings("ignore", message="Mean of empty slice")
    print("Starting to scan and patch missing data...")

    nan_coords = (
        result_df[result_df["2t"].isna()][["original_lat", "original_lon"]]
        .drop_duplicates()

    )
    print(f"Found {len(nan_coords)} boundary coordinates with missing values...")

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
        

        # * Key change: the time column name changed from forecast_reference_time+step to time
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
        print(f"Patching complete; {remaining_nans} rows are still missing (possibly located entirely over deep ocean).")
    else:
        print("Patching completed perfectly!")

    # ── 6. UTC -> Texas time, export ──────────────────────────────────────────────────

    result_df["time"] = (
        pd.to_datetime(result_df["time"])
        .dt.tz_localize("UTC")
        .dt.tz_convert(TEXAS_TZ)
        .dt.tz_localize(None) 
    )

    # Build the points -> Substation_Number mapping
    substation_map = dict(zip(coords_df.index, coords_df["Substation_Number"]))

    # Replace the points column
    result_df["points"] = result_df["points"].map(substation_map)
    result_df = result_df.rename(columns={"points": "Substation_Number"}) 
 
    
    # Arrange the output column order
    result_df = result_df.rename(columns={"time": "datetime"})
    result_df ['datetime'] = pd.to_datetime(result_df['datetime'])
    # Filter time, round decimals
    result_df = result_df[result_df["datetime"] >= "2025-01-01"]
    result_df['date'] = result_df['datetime'].dt.date
    result_df['time'] = result_df['datetime'].dt.strftime('%H:%M')
    
    # Combine wind speed components
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
