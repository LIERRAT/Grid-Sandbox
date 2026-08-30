"""
geo_animation_extra.py
--------------------------------------------------------------------------
Unified geo-animation: all metrics through ONE render(), same
"Texas map scatter + time animation -> mp4" form. Supersedes
syngrid2025_results_heatmap.py (the original three are included here, so
you no longer run two scripts).

Original three (unchanged config):
  - lmp         nodal LMP                   out.lmp             (Th x nbus)
  - curtailment per-bus curtailment         out.curtbybus     (Th x nbus)
  - loadshed    per-bus load shed           out.shedbybus        (Th x nbus)

New three (data available):
  - load        per-bus load                out.loadPD          (Th x nbus)
  - temperature per-bus 2 m temperature     weather CSV -> bus  (Th x nbus)
  - windspeed   per-bus wind speed          weather CSV -> bus  (Th x nbus)

Not yet (need model-side export of per-bus generation):
  - wind / solar / thermal generation  -> require out.genByBus (see note at bottom)

Alignment (verified):
  - out.<field> column j  <->  bus2025_data.csv row j   (2751 buses)
  - each bus carries a Substation_Number (1..1736); temperature/windspeed are
    per substation and are broadcast to that substation's buses, so all metrics
    share the SAME 2751 scatter and the SAME render().

Deps: pip install scipy pandas geopandas matplotlib   (+ system ffmpeg for mp4)
--------------------------------------------------------------------------
"""

import numpy as np, pandas as pd, geopandas as gpd, scipy.io as sio
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt, matplotlib.animation as animation

# ========================== config ==========================
MAT      = "syngrid2025_timeseries_results.mat"
CSV      = "bus2025_data.csv"
COUNTIES = "tx_counties.json"
WEATHER  = "bus_weather_data_25010115.csv"

# which metrics to render (each produces one mp4). All six share one render().
# After re-running the edited Ver8 (with genByBus_* saved), add:
#   'gen_wind', 'gen_solar', 'gen_thermal'
METRICS  = ['lmp', 'curtByBus', 'shedByBus', 'load', 'temperature', 'windspeed']
WIND_LVL = '100Wind'          # '100Wind' (hub-height, ties to turbines) or '10Wind'
VOLL     = float(np.asarray(sio.loadmat(r"syngrid2025_timeseries_results.mat",
                            struct_as_record=False, squeeze_me=True)['out'].VOLL))
 
CFG = {
    # ---- original three (unchanged from syngrid2025_results_heatmap.py) ----
    'lmp':         dict(source='mat', field='lmp',
                        label='LMP ($/MWh)', cmap='RdYlGn_r',
                        vmin=0, vmax=VOLL/5, extend='both', unit='$/MWh', accum_unit='$/MWh*h'),
    'curtByBus':   dict(source='mat', field='curtByBus',
                        label='Curtailment (MW)', cmap='Reds',
                        vmin=0, vmax=None, extend='max', unit='MW', accum_unit='MWh'),
    'shedByBus':   dict(source='mat', field='shedByBus',
                        label='Load shed (MW)', cmap='Reds',
                        vmin=0, vmax=None, extend='max', unit='MW', accum_unit='MWh'),
    # ---- new three ----
    # per-bus load (from out.loadPD): higher = more demand, white->red
    'load':        dict(source='mat', field='loadPD',
                        label='Bus load (MW)', cmap='Reds',
                        vmin=0, vmax=None, extend='max', unit='MW', accum_unit='MWh'),
    # temperature (Celsius): cold=blue, hot=red, fixed diverging scale
    'temperature': dict(source='weather', field='2t', to_celsius=True,
                        label='Temperature (C)', cmap='RdBu_r',
                        vmin=-15, vmax=35, extend='both', unit='C', accum_unit=None),
    # wind speed (m/s): calm=light, strong=dark green
    'windspeed':   dict(source='weather', field=WIND_LVL, to_celsius=False,
                        label='Wind speed (m/s)', cmap='YlGn',
                        vmin=0, vmax=None, extend='max', unit='m/s', accum_unit=None),

}
 
FPS     = 6
DOTSIZE = 8
# =========================================================
 
# ---- shared data loaded once ----
m   = sio.loadmat(MAT, struct_as_record=False, squeeze_me=True)
out = m['out']
bus = pd.read_csv(CSV)
lon, lat = bus['Longitude'].values, bus['Latitude'].values
nbus = len(bus)
txc = gpd.read_file(COUNTIES)
 
# weather -> Th x nbus, built once and reused (substation broadcast to its buses)
_weather_cache = {}
def weather_matrix(field, to_celsius=False):
    if field in _weather_cache:
        return _weather_cache[field]
    w = pd.read_csv(WEATHER)
    piv = w.pivot_table(index='Substation_Number', columns=['date', 'time'], values=field)
    M = piv.loc[bus['Substation_Number'].values].values.T          # Th x nbus
    if to_celsius:
        M = M - 273.15
    _weather_cache[field] = M
    return M
 
 
def get_matrix(cfg):
    """Return the Th x nbus array for a metric, from out.mat or from weather CSV."""
    if cfg['source'] == 'mat':
        if not hasattr(out, cfg['field']):
            raise KeyError(f"out.{cfg['field']} not in this .mat -- re-run the edited "
                           f"Ver8 (with genByBus_* saved) to enable this metric.")
        Z = np.asarray(getattr(out, cfg['field']), float)
    else:
        Z = weather_matrix(cfg['field'], cfg.get('to_celsius', False))
    assert Z.shape[1] == nbus, f"{cfg['field']}: {Z.shape[1]} cols != {nbus} buses"
    return Z
 
 
def render(metric):
    """Build one metric's animation and export mp4. Independent figure, closed at end."""
    cfg = CFG[metric]
    Z   = get_matrix(cfg)
    Th  = Z.shape[0]
 
    # fixed color scale across frames (cross-frame comparability); sparse -> 99th pct
    vmin, vmax = cfg['vmin'], cfg['vmax']
    if vmax is None:
        nz = Z[np.isfinite(Z) & (Z > 0)]
        vmax = float(np.percentile(nz, 99)) if nz.size else 1.0
    print(f'[{metric}] {Th} frames, {nbus} buses | scale {vmin}..{vmax:.1f} | cmap={cfg["cmap"]}')
 
    # per-hour system aggregate; accum only meaningful for extensive (MW) metrics
    unit, aunit = cfg['unit'], cfg['accum_unit']
    if aunit:                                          # extensive: sum & running total
        hourly = np.nansum(Z, axis=1)
        accum  = np.nancumsum(hourly)
        agg_is_sum = True
    else:                                              # intensive (C, m/s): report mean
        hourly = np.nanmean(Z, axis=1)
        accum  = None
        agg_is_sum = False
 
    fig, ax = plt.subplots(figsize=(9, 8), dpi=120)
    txc.boundary.plot(ax=ax, linewidth=0.3, edgecolor='0.78', zorder=1)
    sc = ax.scatter(lon, lat, c=Z[0], s=DOTSIZE, cmap=cfg['cmap'],
                    vmin=vmin, vmax=vmax, edgecolors='none', zorder=2)
    cb = fig.colorbar(sc, ax=ax, shrink=0.6, pad=0.01, extend=cfg['extend'])
    cb.set_label(cfg['label'])
    ax.set_xlim(lon.min()-0.4, lon.max()+0.4)
    ax.set_ylim(lat.min()-0.4, lat.max()+0.4)
    ax.set_xlabel('Longitude'); ax.set_ylabel('Latitude')
    ax.set_aspect(1/np.cos(np.deg2rad(31)))           # rough equal-area for TX latitude
    ttl = ax.set_title('')
    name = cfg['label'].split(' (')[0]
 
    def update(h):
        z = Z[h]
        sc.set_array(z)
        if agg_is_sum:
            line = (f'sum {hourly[h]:,.0f} {unit}  accum {accum[h]:,.0f} {aunit}')
        else:
            line = (f'mean {hourly[h]:.1f} {unit}')
        ttl.set_text(
            f'{name}  -  hour {h+1}/{Th}\n'
            f'min {np.nanmin(z):.1f}  max {np.nanmax(z):.1f}  {line}'
        )
        return sc, ttl
 
    out_mp4 = f'{metric}_animation.mp4'
    anim = animation.FuncAnimation(fig, update, frames=Th, blit=False)
    anim.save(out_mp4, writer=animation.FFMpegWriter(fps=FPS, bitrate=3000))
    plt.close(fig)
    print('  saved', out_mp4)
 
 
if __name__ == '__main__':
    for metric in METRICS:
        render(metric)
    print('done:', ', '.join(f'{x}_animation.mp4' for x in METRICS))
 
# --------------------------------------------------------------------------
# To also animate per-bus wind / solar / thermal generation, export per-bus
# generation from Ver8. At the end of each hourly DCOPF loop, accumulate the
# solved dispatch pg onto buses, split by fuel, e.g.:
#
#   nb = size(mpc.bus,1);
#   genByBus_wind(tt,:) = accumarray(mpc.gen(isWind,GEN_BUS), pg(isWind), [nb 1])';
#   genByBus_sol (tt,:) = accumarray(mpc.gen(isSol ,GEN_BUS), pg(isSol ), [nb 1])';
#   genByBus_th  (tt,:) = accumarray(mpc.gen(isNG|isCoal|isNuc|isOil,GEN_BUS), ...
#                                     pg(isNG|isCoal|isNuc|isOil), [nb 1])';
# then  out.genByBus_wind = genByBus_wind;  (etc.)
#
# Once present, add matching CFG entries with source='mat', field='genByBus_wind', ...
# and they will render through the same engine.
# --------------------------------------------------------------------------
 
