import pandas as pd
import numpy as np


def main():
    # 1. 读取表格数据，确定采样点，过滤变压器分支
    df = pd.read_csv(r"C:\Users\jason\OneDrive\Documents\VSC\Earthkit\branch2025_weatherSampling.csv")
    F_lat = df['F_lat'].values
    F_lon = df['F_lon'].values
    T_lat = df['T_lat'].values
    T_lon = df['T_lon'].values
    m = np.ceil(np.abs(np.subtract(F_lat, T_lat))/0.25)
    n = np.ceil(np.abs(np.subtract(F_lon, T_lon))/0.25)
    df['Sample_Pnts'] = np.maximum(m, n).astype(int)
    df = df[df['Sample_Pnts'] > 0] 
    # 2. 核心操作：根据 Sample_Pnts 裂变（展开）行数
    # 例如 Sample_Pnts 为 2，这一行就会被复制成两行
    df_expanded = df.loc[df.index.repeat(df['Sample_Pnts'])].reset_index(drop=True)

    # 3. 生成 Sample_ind (即公式里的 i)
    # 对每个 BRANCH_ID 分组，生成从 1 到 N 的序号
    df_expanded['Sample_ind'] = df_expanded.groupby('BRANCH_I').cumcount() + 1

    # 4. 根据公式计算 Sample_Lat 和 Sample_Lon
    N = df_expanded['Sample_Pnts']
    i = df_expanded['Sample_ind']

    # n等分点计算公式： 生成采样坐标
    df_expanded['Sample_Lat'] = df_expanded['F_lat'] + (df_expanded['T_lat'] - df_expanded['F_lat']) * (i - 0.5) / N
    df_expanded['Sample_Lon'] = df_expanded['F_lon'] + (df_expanded['T_lon'] - df_expanded['F_lon']) * (i - 0.5) / N

    # 1. 限制小数位 (优化 CSV 文件大小)
    # 针对生成的经纬度列，四舍五入到 3 位小数
    df_expanded['F_lat'] = df_expanded['F_lat'].round(3)
    df_expanded['F_lon'] = df_expanded['F_lon'].round(3)
    df_expanded['T_lat'] = df_expanded['T_lat'].round(3)
    df_expanded['T_lon'] = df_expanded['T_lon'].round(3)
    df_expanded['Sample_Lat'] = df_expanded['Sample_Lat'].round(3)
    df_expanded['Sample_Lon'] = df_expanded['Sample_Lon'].round(3)

    # 导出为新的 CSV，方便你随时查看或后续调用
    print(df_expanded.head(20))
    #df_expanded.to_csv('branch2025_weatherSamplingCoordinates.csv', index=False)
    
    
if __name__ == "__main__":
    main()