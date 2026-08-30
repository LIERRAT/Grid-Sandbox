import pandas as pd
import geopandas as gpd

# 1. 读取你的节点坐标 CSV
nodes_df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus2025_data.csv")
# 转化为空间数据格式 (假设经度列叫 Lon, 纬度叫 Lat)
nodes_gdf = gpd.GeoDataFrame(
    nodes_df, 
    geometry=gpd.points_from_xy(nodes_df.Longitude, nodes_df.Latitude),
    crs="EPSG:4326" # 这是标准的 GPS 经纬度坐标系
)

# 2. 读取下载的德州 County Shapefile
counties_gdf = gpd.read_file(r"C:\Users\jason\Downloads\Texas_County_Boundaries_Detailed_-3993510293615756158\Texas_County_Boundaries_Detailed.shp")
counties_gdf = counties_gdf.to_crs("EPSG:4326") # 确保两个数据的坐标系一致

# 3. 空间连接 (Spatial Join)
# 这一步会自动找出每个节点在哪个 County 多边形里面
joined_gdf = gpd.sjoin(nodes_gdf, counties_gdf, how="left", predicate="within")

# 4. 导出结果
joined_gdf.to_csv('nodes_with_county.csv', index=False)