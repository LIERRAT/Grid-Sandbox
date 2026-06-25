import pandas as pd
import requests
import time

# --- 配置文件路径与参数 ---
INPUT_CSV = "wind_farms.csv"    # 包含坐标的输入文件
OUTPUT_CSV = "wind_farms_iec_classified.csv" # 处理后的输出文件

def get_mean_wind_speed(lat, lon):
    """
    获取 100m 高度年平均风速 (Mean Wind Speed at 100m)。
    由于 GWA 限制批量接口调用，此处采用 NASA POWER API 开放气候数据，
    并进行物理学外推。
    """
    # 请求参数：获取 50m 高度的年平均风速 (WS50M)
    url = f"https://power.larc.nasa.gov/api/temporal/climatology/point?parameters=WS50M&community=RE&longitude={lon}&latitude={lat}&format=JSON"
    
    try:
        # 设置10秒超时限制
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # 提取多年平均风速值 (Annual Mean)
        ws_50m = data['properties']['parameter']['WS50M']['ANN']
        
        # 利用风切变指数 (Wind Shear Exponent) 结合幂律 (Power Law) 外推至 100m 轮毂高度 (Hub Height)
        # 德州开阔地带的地表粗糙度对应的切变指数近似为 0.14
        ws_100m = ws_50m * ((100 / 50) ** 0.14)
        return round(ws_100m, 2)
        
    except Exception as e:
        print(f"数据获取失败 坐标 ({lat}, {lon}): {e}")
        return None

def assign_iec_class(wind_speed):
    """
    实施第二步：将风速精确映射到国际电工委员会标准 (IEC Class)
    对应报告中 Table 1 与 Table 2 的逻辑。
    """
    if pd.isna(wind_speed) or wind_speed is None:
        return "Unknown"
        
    if wind_speed > 8.5:
        return "Class 1"
    elif 7.5 <= wind_speed <= 8.5:
        return "Class 2"
    else:
        return "Class 3"

def main():
    print("开始处理风电场数据 (Wind Farm Data Processing)...")
    
    # 读取坐标数据
    df = pd.read_csv(INPUT_CSV)
    
    # 验证列名合法性
    if not {'lat', 'lon'}.issubset(df.columns):
        print("错误：CSV 文件必须包含小写的 'lat' 和 'lon' 列头。")
        return

    wind_speeds = []
    iec_classes = []
    
    # 遍历处理每一行坐标
    for index, row in df.iterrows():
        lat = row['lat']
        lon = row['lon']
        
        print(f"正在查询点位 {index + 1}/{len(df)}: 纬度 {lat}, 经度 {lon} ...")
        
        # 步骤一：获取风速 (Wind Speed Retrieval)
        ws = get_mean_wind_speed(lat, lon)
        wind_speeds.append(ws)
        
        # 步骤二：判定 IEC 类别 (IEC Class Determination)
        iec = assign_iec_class(ws)
        iec_classes.append(iec)
        
        # 加入 0.5 秒延迟，防止并发过高触发 API 限流保护 (Rate Limiting)
        time.sleep(0.35)

    # 汇总计算结果并追加至原数据框末尾
    df['Mean_Wind_Speed_100m'] = wind_speeds
    df['IEC_Class'] = iec_classes
    
    # 保存最终结果至 CSV
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n处理完毕！结果已成功保存至: {OUTPUT_CSV}")

if __name__ == "__main__":
    main()