"""
Add a County column to the bus data by spatial join against a Texas county shapefile.

Assumes the bus table already carries Latitude / Longitude (and Substation_Number).
Input and output are the same bus file: the County column is added in place.

Requires: geopandas
"""


import pandas as pd
import geopandas as gpd

# ---- paths (edit to your locations) ----
BUS_DATA   = "bus2025_raw_data.csv"          # bus table WITH Latitude / Longitude
COUNTY_SHP = "Texas_County_Boundaries_Detailed.shp"
OUT_PATH   = "bus2025_data.csv"          # write back in place (County column added)

# column names in the bus file
LAT_COL = "Latitude"
LON_COL = "Longitude"
# name of the county field in the shapefile (check your shapefile; often CNTY_NM / NAME)
SHP_COUNTY_COL = "CNTY_NM"


def main():
    nodes = pd.read_csv(BUS_DATA)

    nodes_gdf = gpd.GeoDataFrame(
        nodes,
        geometry=gpd.points_from_xy(nodes[LON_COL], nodes[LAT_COL]),
        crs="EPSG:4326",                 # standard GPS lat/lon
    )

    counties = gpd.read_file(COUNTY_SHP).to_crs("EPSG:4326")

    joined = gpd.sjoin(nodes_gdf, counties, how="left", predicate="within")

    # keep original bus columns + the county name, drop spatial/join scratch columns
    nodes["County"] = joined[SHP_COUNTY_COL].values

    missing = nodes["County"].isna().sum()
    if missing:
        print(f"warning: {missing} buses did not fall inside any county "
              f"(check coordinates / shapefile coverage)")

    nodes.to_csv(OUT_PATH, index=False)
    print(f"[done] wrote {OUT_PATH} with County column "
          f"({nodes['County'].nunique()} counties)")


if __name__ == "__main__":
    main()
