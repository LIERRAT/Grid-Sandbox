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

# ---- Sampling controls ----
SEED       = 42            # fixed seed, reproducible
DIST_KIND  = "lognormal"   # "lognormal" (default, naturally positive/right-skewed, matches the EIA sample shape)
                           # or "normal" (truncated at >0; high-CV NGCT produces many low values, not recommended)

# ---- Wind turbine IEC classification controls (formerly IEC_CLASS.py, merged in here) ----
IEC_SLEEP  = 0.35          # NASA POWER call interval (s), to avoid rate limiting
SHEAR_EXP  = 0.14          # power-law wind shear exponent (open Texas terrain), 50m -> 100m extrapolation


def _draw(mean, sd, n, rng):
    """Sample by arithmetic mean & sd. lognormal uses moment-matching parameterization to guarantee the expected mean/sd."""
    if DIST_KIND == "lognormal":
        sigma = np.sqrt(np.log(1.0 + (sd / mean) ** 2))
        mu    = np.log(mean) - 0.5 * sigma ** 2
        return rng.lognormal(mu, sigma, n)
    elif DIST_KIND == "normal":
        return np.clip(rng.normal(mean, sd, n), 1e-3, None)
    raise ValueError(DIST_KIND)


# =====================================================================
# Wind turbine IEC classification (the two functions from the original IEC_CLASS.py, moved in as-is)
# =====================================================================
def get_mean_wind_speed_100m(lat, lon):
    """
    Get the annual mean wind speed at 100m height. GWA restricts batch access, so use NASA POWER open climate data:
    take the 50m annual mean wind speed (WS50M), then extrapolate to the 100m hub height via a power law.
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
        print(f"  [IEC] wind speed retrieval failed ({lat}, {lon}): {e}")
        return None


def assign_iec_class(wind_speed):
    """Wind speed -> IEC Class (logic corresponding to Table 1/2 of the report)."""
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
    Assign an IEC class to wind turbine rows, writing it into GENERATOR_TYPE.
    - Coordinates come from the bus table (BUS_I -> Latitude/Longitude/Substation_Number)
    - Deduplicate by Substation_Number, calling NASA POWER only once per site
    - Results are persisted to IEC_CACHE, hit directly next time, no repeated API calls
    Returns: dict {GEN_I: IEC_Class}
    """
    wind_mask = gen_df["FUEL_TYPE"] == "WND (Wind)"
    wind = gen_df.loc[wind_mask, ["GEN_I", "BUS_I"]].merge(
        bus_df[["BUS_I", "Substation_Number", "Latitude", "Longitude"]],
        on="BUS_I", how="left",
    )

    # Unique sites (those with coordinates)
    sites = (
        wind.dropna(subset=["Latitude", "Longitude"])
            .drop_duplicates("Substation_Number")
            .set_index("Substation_Number")
    )

    # Read the cache
    if os.path.exists(IEC_CACHE):
        cache = pd.read_csv(IEC_CACHE).set_index("Substation_Number")
        print(f"[IEC] cache hit for {len(cache)} sites ({IEC_CACHE})")
    else:
        cache = pd.DataFrame(columns=["Mean_Wind_Speed_100m", "IEC_Class"])
        cache.index.name = "Substation_Number"

    # Only query the API for sites not covered by the cache
    todo = [s for s in sites.index if s not in cache.index]
    print(f"[IEC] need to query {len(todo)} / {len(sites)} wind sites ...")
    for i, sub in enumerate(todo, 1):
        lat, lon = sites.loc[sub, "Latitude"], sites.loc[sub, "Longitude"]
        print(f"  [IEC] {i}/{len(todo)} site {sub}: ({lat:.3f}, {lon:.3f})")
        ws = get_mean_wind_speed_100m(lat, lon)
        cache.loc[sub] = [ws, assign_iec_class(ws)]
        time.sleep(IEC_SLEEP)

    # Save back to the cache
    cache.reset_index().to_csv(IEC_CACHE, index=False)

    # site -> class, map back to each wind turbine
    sub2class = cache["IEC_Class"].to_dict()
    wind["IEC_Class"] = wind["Substation_Number"].map(sub2class).fillna("Unknown")
    n_unknown = (wind["IEC_Class"] == "Unknown").sum()
    if n_unknown:
        print(f"[IEC] warning: {n_unknown} wind turbines have no valid classification (missing coordinates or API failure) -> 'Unknown'")
    print("[IEC] classification distribution:\n", wind["IEC_Class"].value_counts().to_string())
    return dict(zip(wind["GEN_I"], wind["IEC_Class"]))


def main():
    gen_df = pd.read_csv(GEN_RAW)   # includes gen_id, bus_number, resource_type, capacity
    bus_df = pd.read_csv(BUS_DATA)  # coordinates + substation, used for wind turbine IEC classification

    ## ---------------------------------------------------------
    ##
    ## Assign the technology type (GENERATOR_TYPE)
    ##
    ## ---------------------------------------------------------

    # ---------------------------------------------------------
    # Natural gas units: assign a type by rated capacity using an empirical probability distribution: steam_turbine / combined_cycle / fired_combustion
    # ---------------------------------------------------------
    thermal_gens = gen_df[gen_df['FUEL_TYPE'] == 'NG (Natural Gas)'].copy()

    # --- cluster parameters derived from EIA-860 analysis ---
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
    # Wind turbines: IEC class as GENERATOR_TYPE (the original IEC_CLASS.py is merged here)
    #   - Class 1/2/3 / Unknown written into GENERATOR_TYPE, used by the wind time series to pick the power curve
    # ---------------------------------------------------------
    geni2iec = classify_wind_gens(gen_df, bus_df)
    wind_mask = gen_df["FUEL_TYPE"] == "WND (Wind)"
    gen_df.loc[wind_mask, "GENERATOR_TYPE"] = gen_df.loc[wind_mask, "GEN_I"].map(geni2iec)

    # =========================================================
    # Storage units: assign a storage duration (1..6h) by power, then compute the rated energy
    # =========================================================
    storage_gens = gen_df[gen_df["FUEL_TYPE"] == "MWH (Electricity use for Energy Storage)"].copy()

    duration_hours  = np.array([1, 2, 3, 4, 5, 6])
    duration_counts = np.array([313, 346, 63, 370, 31, 8], dtype=float)
    duration_probs  = duration_counts / duration_counts.sum()

    rng = np.random.default_rng(42)   # fixed seed
    storage_gens["Storage_Duration_h"] = rng.choice(
        duration_hours, size=len(storage_gens), p=duration_probs
    )
    storage_gens["Storage_MWH"] = storage_gens["PMAX"].to_numpy(dtype=float) * storage_gens["Storage_Duration_h"]
    gen_df = gen_df.merge(
        storage_gens[["GEN_I", "Storage_Duration_h", "Storage_MWH"]], on="GEN_I", how="left"
    )

    ## ---------------------------------------------------------
    ##
    ## Update the generation cost (GENERATOR_COST)
    ##
    ## ---------------------------------------------------------

    # --- fuel price cf ($/MMBtu) ---
    FUEL_PRICE = {
        "NG":  4.18,    # EIA EPM Table 4.13.A, Mar 2025
        "BIT": 2.02,    # EIA EPM Table 4.10.A, Mar 2025
        "NUC": 0.58,    # EIA nuclear data & statistics
        "DFO": 17.13,   # EIA EPM Table 4.2
        "OBL": 100,     # undefined, placeholder
        "OTH": 100,     # undefined, placeholder
    }

    # --- (A) The four categories with an EIA-923 TX distribution: heat rate ~ dist(mean, sd), MMBtu/MWh ---
    HEAT_RATE_DIST_MMBTU_PER_MWH = {
        ("NG",  "combined_cycle"):   {"mean": 6.891,  "sd": 1.169},
        ("NG",  "fired_combustion"): {"mean": 9.867,  "sd": 3.862},
        ("NG",  "steam_turbine"):    {"mean": 11.552, "sd": 2.569},
        ("BIT", None):               {"mean": 11.136, "sd": 0.867},
    }

    # --- (B) Fuels without distribution data: point values (EIA Table 8.2, Btu/kWh) ---
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

    # (A) Distribution sampling — order is fixed to guarantee reproducibility
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

    # (B) Point values
    for (fuel, gtype), btu_per_kwh in HEAT_RATE_BTU_PER_KWH.items():
        b1 = btu_per_kwh * BTU_PER_KWH_TO_MMBTU_PER_MWH
        mask = (gen_df["FUEL_SHORT"] == fuel)
        gen_df.loc[mask, "heat_rate_b1"]  = b1
        gen_df.loc[mask, "fuel_price_cf"] = FUEL_PRICE[fuel]

    # gencost polynomial: c1 = cf*b1 ; c2 = cf*b2 ; non-conventional units naturally stay at 0
    gen_df["c1"] = (gen_df["fuel_price_cf"] * gen_df["heat_rate_b1"]).round(4)
    gen_df["c2"] = (gen_df["fuel_price_cf"] * gen_df["heat_rate_b2"]).round(6)

    gen_df = gen_df.drop(columns=["FUEL_SHORT"])
    gen_df.to_csv("generator2025_data_modified.csv", index=False)


if __name__ == "__main__":
    main()
