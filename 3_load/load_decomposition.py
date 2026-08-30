"""
Build SLOPE_load_composition_bus_level.csv from raw data (fully reproducible).

Replaces an earlier undocumented manual (Excel) process. No prior output file is
used as input — the composition is built end to end from three raw sources.

Pipeline (matches the documented provenance):
  1. Load buses  = buses with PD > 0 (from the grid bus table).
  2. RCI split   = each sector's share of county electricity use (SLOPE),
                   R + C + I = 1 per county;  P_x = PD * ratio.
  3. BTM PV      = county BTM PV capacity (EIA-861M state total split to counties
                   by population) allocated to buses by PD share within the county.

Inputs
  BUS_DATA  : bus table with BUS_I, PD, QD, BUS_AREA, County, Substation_Number
  RCI_RATIO : county -> R/C/I ratio (share of total, sums to 1)
  BTM_PV    : county -> BTM PV Res/Comm capacity (MW)

Output
  OUT_PATH  : bus-level load composition, input to the load model.
"""
import pandas as pd
import numpy as np

# ---- paths (relative, repo-friendly) ----
BUS_DATA  = r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\bus2025_data.csv"
RCI_RATIO = r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\RCI_ratio_by_county.csv"
BTM_PV    = r"C:\Users\jason\OneDrive\Documents\Rutgers\25-26\Smart Grids\research material\Texas2k_series2025\BTM_PV_county_level.csv"
OUT_PATH  = r"C:\Users\jason\OneDrive\Documents\VSC\Earthkit\SLOPE_load_composition_bus_level.csv"

ATOL = 1e-6

# ERCOT weather zone names by BUS_AREA code (from the Texas2k case)
AREA_NAME = {
    1: "Far West", 2: "North", 3: "West", 4: "South",
    5: "North Central", 6: "South Central", 7: "Coast", 8: "East",
}


def _cty_key(s):
    """Normalized county key: lowercase, no spaces/dots (De Witt == DeWitt)."""
    return s.astype(str).str.strip().str.lower().str.replace(r"[ .]", "", regex=True)


def load_rci_ratio(path):
    """County -> R/C/I ratio; drops Excel 'Unnamed' cols; must sum to 1 per county."""
    r = pd.read_csv(path)
    r = r[[c for c in r.columns if not str(c).startswith("Unnamed")]]
    r = r.rename(columns={"County Name": "County",
                          "R ratio": "R Ratio", "C ratio": "C Ratio", "I ratio": "I Ratio"})
    r = r.dropna(subset=["County"])
    r["County"] = r["County"].astype(str).str.strip()
    r = r[r["County"] != ""]
    for c in ["R Ratio", "C Ratio", "I Ratio"]:
        r[c] = pd.to_numeric(r[c], errors="coerce")
    r = r.dropna(subset=["R Ratio", "C Ratio", "I Ratio"]).drop_duplicates("County")
    s = r[["R Ratio", "C Ratio", "I Ratio"]].sum(axis=1)
    bad = r.loc[~np.isclose(s, 1.0, atol=1e-3), "County"].tolist()
    assert not bad, f"RCI ratios do not sum to 1 for: {bad[:10]}"
    r["_k"] = _cty_key(r["County"])
    return r


def load_btm_pv(path):
    """County -> BTM PV Res/Comm capacity (MW); drops Excel scratch columns."""
    b = pd.read_csv(path)
    b = b[["County", "BTM PV Res Cap", "BTM PV Comm Cap"]].copy()
    b = b.dropna(subset=["County"])
    b["County"] = b["County"].astype(str).str.strip()
    b = b[b["County"] != ""]
    for c in ["BTM PV Res Cap", "BTM PV Comm Cap"]:
        b[c] = pd.to_numeric(b[c], errors="coerce")
    b = b.dropna(subset=["BTM PV Res Cap", "BTM PV Comm Cap"]).drop_duplicates("County")
    b["_k"] = _cty_key(b["County"])
    return b


def main():
    bus   = pd.read_csv(BUS_DATA)
    ratio = load_rci_ratio(RCI_RATIO)
    btm   = load_btm_pv(BTM_PV)

    # 1) load buses = PD > 0
    df = bus.loc[bus["PD"] > 0, ["BUS_I", "PD", "QD", "BUS_AREA",
                                 "County", "Substation_Number"]].copy()
    df["Area"] = df["BUS_AREA"].map(AREA_NAME)
    df["_k"] = _cty_key(df["County"])

    # 2) RCI split
    df = df.merge(ratio[["_k", "R Ratio", "C Ratio", "I Ratio"]], on="_k", how="left")
    miss = sorted(df.loc[df["R Ratio"].isna(), "County"].unique())
    assert not miss, f"counties missing from RCI ratio table: {miss}"
    df["P_Res_Load"] = df["PD"] * df["R Ratio"]
    df["P_Com_Load"] = df["PD"] * df["C Ratio"]
    df["P_Ins_Load"] = df["PD"] * df["I Ratio"]

    # 3) BTM PV: county capacity -> bus by PD share within county
    df = df.merge(btm[["_k", "BTM PV Res Cap", "BTM PV Comm Cap"]], on="_k", how="left")
    miss_pv = sorted(df.loc[df["BTM PV Res Cap"].isna(), "County"].unique())
    assert not miss_pv, f"counties missing from BTM PV table: {miss_pv}"
    pd_share = df["PD"] / df.groupby("_k")["PD"].transform("sum")
    df["Bus BTM PV Res Capacity"]  = df["BTM PV Res Cap"]  * pd_share
    df["Bus BTM PV Comm Capacity"] = df["BTM PV Comm Cap"] * pd_share

    # ---- self-checks ----
    assert np.allclose(df["R Ratio"] + df["C Ratio"] + df["I Ratio"], 1.0, atol=1e-3)
    assert np.allclose(df["P_Res_Load"] + df["P_Com_Load"] + df["P_Ins_Load"],
                       df["PD"], atol=1e-4), "load split does not sum to PD"
    # county BTM PV fully allocated across its buses
    chk = df.groupby("_k").agg(
        res_bus=("Bus BTM PV Res Capacity", "sum"),
        res_cty=("BTM PV Res Cap", "first")).reset_index()
    assert np.allclose(chk["res_bus"], chk["res_cty"], atol=1e-4), "BTM PV not conserved per county"

    # ---- output ----
    cols = ["BUS_I", "PD", "QD", "Substation_Number", "County", "Area",
            "R Ratio", "C Ratio", "I Ratio",
            "P_Res_Load", "P_Com_Load", "P_Ins_Load",
            "Bus BTM PV Res Capacity", "Bus BTM PV Comm Capacity"]
    out = df[cols].copy()
    for c in ["P_Res_Load", "P_Com_Load", "P_Ins_Load",
              "Bus BTM PV Res Capacity", "Bus BTM PV Comm Capacity"]:
        out[c] = out[c].round(6)
    out.to_csv(OUT_PATH, index=False)
    print(f"[done] wrote {OUT_PATH}  ({len(out)} load buses, {df['_k'].nunique()} counties)")
    print("self-checks passed: R+C+I=1, split sums to PD, BTM PV conserved per county")


if __name__ == "__main__":
    main()