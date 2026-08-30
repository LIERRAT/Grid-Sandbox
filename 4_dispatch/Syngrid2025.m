%   Syngrid2025 — hourly dispatch engine (stage 4 of the pipeline)
%
%   Runs the ERCOT-scale synthetic grid hour by hour:
%   storage scheduling (QP) -> unit commitment (SCUC) -> DC-OPF with VOLL load shedding and reserves.
%
%   Inputs : case file + the time-series CSVs from stages 1-3 
%   (weather, wind/solar/hydro, generator availability, nodal load)
% 
%   Output : per-node, per-hour LMP, load shed, dispatch, storage state (.mat)
%
%   Requires: MATPOWER 8.1 + Gurobi
%   Full methodology and validation: see docs/project_overview.pdf



%% 0. Case + solver ------------------------------------------------------
define_constants;
casefile = "Texas2k_series25_case1_summerpeak.m";   % <<< synthetic grid model
generator_data = "generator2025_data_modified.csv";
solar_gen_data = "solar_generation_time_series.csv";
wind_gen_data = "wind_generation_time_series.csv";
load_data = "winterized_load_time_series_bus_level.csv";
hydro_data = "hydro_generation_time_series.csv";
conventional_generator_availability_data = "conventional_generation_down_timeseries.csv";
conventional_generator_unavailabilty_record = "gen_fault_record.csv";

mpc   = loadcase(casefile);
mpopt = mpoption('verbose',0,'out.all',0,'opf.dc.solver','GUROBI'); % needs GUROBI License 
ngen = size(mpc.gen,1);
nbus = size(mpc.bus,1);
fprintf('Case: %d buses, %d gens, %d branches.\n', nbus, ngen, size(mpc.branch,1));



%% 1. Fuel classification (from the modified generator table) -----------
GT = sortrows(readtable(generator_data), 'GEN_I');
fuel   = string(GT.FUEL_TYPE);
isWind = contains(fuel,'Wind');
isSol  = contains(fuel,'Solar');
isNG   = contains(fuel,'Natural Gas');
isCoal = contains(fuel,'Coal');
isHyd  = contains(fuel,'Water');
isNuc  = contains(fuel,'Nuclear');
isStor = contains(fuel,'Energy Storage');
isOil  = contains(fuel,'Fuel Oil');
isOth  = contains(fuel,'Other');

%% 1.5 ---- Generation cost reconfiguration ----
assert(height(GT) == ngen, 'GT row count must match mpc.gen (GEN_I = row index)');
NC = 3;                            % stored as quadratic [c2 c1 c0]
gc = zeros(ngen, 4+NC);
gc(:,MODEL)    = POLYNOMIAL;       % =2
gc(:,STARTUP)  = zeros(ngen, 1);
gc(:,SHUTDOWN) = zeros(ngen, 1);
gc(:,NCOST)    = NC;
gc(:,COST)     = GT.c2;            % quadratic term (essentially 0 here, i.e. linear)
gc(:,COST+1)   = GT.c1;            % linear term $/MWh
gc(:,COST+2)   = zeros(ngen, 1);            % no-load / constant term
mpc.gencost = gc;


%% 2. Read the four long-format series and pivot to hourly matrices ------
% ---- wind: defines the master hour vector ----
[Table_wind,datetime_wind] = readLong(wind_gen_data, {'datetime'}, 'yyyy-MM-dd HH:mm');
hours = unique(datetime_wind);
Hour = numel(hours);
[~,hour_wind] = ismember(datetime_wind,hours);
windMax = accumarray([hour_wind, Table_wind.GEN_I], Table_wind.simulated_PG, [Hour,ngen], [], NaN);

% ---- solar ----
[Table_solar,datetime_solar] = readLong(solar_gen_data, {'datetime'}, 'yyyy-MM-dd HH:mm:ss');
[tf,hour_solar] = ismember(datetime_solar,hours);
solMax  = accumarray([hour_solar, Table_solar.GEN_I], Table_solar.simulated_PG, [Hour,ngen], [], NaN);

% ---- thermal/hydro available capacity ----
[Table_thermal,datetime_thermal] = readLong(conventional_generator_availability_data, {'date','time'}, 'M/d/yyyy H:mm');
[tf,hour_thermal] = ismember(datetime_thermal,hours);
conventional_Cap   = accumarray([hour_thermal, Table_thermal.GEN_I], Table_thermal.capacity, [Hour,ngen], [], NaN);

% ---- hydro: own availability/generation series, treated like wind/solar ----
[Table_hydro,datetime_hydro] = readLong(hydro_data, {'datetime'}, 'yyyy-MM-dd HH:mm');
[tf,hour_hydro] = ismember(datetime_hydro,hours);
hydroMax = accumarray([hour_hydro, Table_hydro.GEN_I], Table_hydro.simulated_PG, [Hour,ngen], [], NaN);

% ---- battery storage (constants; SoC initialised in section 3) ----
storIdx = find(isStor);                  % storage rows in mpc.gen (121 units)
Erated  = GT.Storage_MWH(storIdx);       % energy cap (MWh), 10..2000
Prated  = GT.PMAX(storIdx);              % power cap (MW),  10..500
eta_c = 0.898;  eta_d = 0.898;           % sqrt(0.807); avg RTE=0.807 from EIA923 Y2025
% 8 storage rows carry PMIN=0.1..0.2 in the case (not 0). They are never
% dispatched as generators: the loop forces storage PMAX=PMIN=0 and injects
% all storage action as bus-load deltas instead.

% ---- load (bus level) ----
[Tl,dtl] = readLong(load_data, {'datetime'}, 'M/d/yyyy H:mm');
[tf,hl] = ismember(dtl,hours);
[tfb,brow] = ismember(Tl.BUS_I, mpc.bus(:,BUS_I));
loadPD = accumarray([hl, brow], Tl.PD_winterized, [Hour, nbus], [], 0);

% Rows each renewable/hydro series controls (finite entries)
windRows  = find(any(~isnan(windMax),1));
solRows   = find(any(~isnan(solMax ),1));
hydroRows = find(any(~isnan(hydroMax),1));

% ===== conventional fleet: row sets from FUEL MASKS, not NaN reverse-inference =
% conventional_Cap holds the ACTUAL available capacity (unplanned outage +
% derate) for exactly these fuels: NG, coal, nuclear, oil, hydro. Two rules,
% dictated by the data's semantics:
%   (1) a conventional gen with NO series never faulted -> it is fully
%       available at NAMEPLATE (not "missing", not zero).
%   (2) row membership comes from the FUEL MASK, so a data gap can NEVER
%       silently drop a unit.
% Division of labour: UC (pass 0) schedules on NAMEPLATE (GT.PMAX) + net load
% and NEVER reads conventional_Cap; the DCOPF loop uses conventional_Cap as the
% real-time ceiling. The two capacities are different objects; don't conflate.

% hydro that also carries a mechanical outage record (detected on the RAW cap,
% before the nameplate fill below)
hydOutRows = intersect(hydroRows, find(any(~isnan(conventional_Cap),1)));

% nameplate fallback: fill conventional gens wherever the outage/derate series
% is absent (missing hour == no fault == full availability)
convAll  = find(isNG | isCoal | isNuc | isOil | isHyd);   % every fuel in conventional_Cap
convMask = false(1,ngen);  convMask(convAll) = true;
fillNP   = repmat(GT.PMAX', Hour, 1);                     % nameplate, Hour x ngen
gap      = convMask & isnan(conventional_Cap);           % conv gen, hour with no value
conventional_Cap(gap) = fillNP(gap);

% non-hydro conventional = what the DCOPF caps and the UC schedules (NG/coal/nuc/oil)
conventional_Rows = setdiff(convAll, hydroRows);





%% 2.5 Storage knobs ----------------------------------------------------
Th        = 354;    % <<< hours to simulate (<= Hour).
dt        = 1;      % hours per step. EXPLICIT: switch to 0.25 for 15-min data.

% Assumption >>> Initial SoC for all BESS is empty
% Storm-banking still works -- storage charges in early valleys before peaks.
soc0Frac =  0.2 + 0.1 * rand(size(Erated));   % initial SoC as fraction of usable energy
% soc0 (in MWh) is finalised below once the load-following energy budget Elf is known.

nStor = numel(storIdx); 
[okb, storRow] = ismember(mpc.gen(storIdx,GEN_BUS), mpc.bus(:,BUS_I));
assert(all(okb), 'every storage gen bus must exist in mpc.bus');

% ===== Ver8b: OPERATING RESERVE knobs (thermal + storage) ==============
% operating reserve = thermal headroom (already done via zonal Rz in pass 0)
%                   + storage reserve (this block wires storage IN as an AS
%                     provider, the way ERCOT batteries carry RRS/ECRS).
% Method A (chosen): SPLIT each battery's rated power. A fraction AS_FRAC is
% reserved for ancillary services (locked, not available to load-following);
% the remaining (1-AS_FRAC) drives the peak-shave QP. This is exactly the
% 0.3~0.5 amplitude haircut, made explicit: the haircut IS the AS carve-out.
% Both knobs are PARAMETERISED with defaults -- tune AS_FRAC and RES_TOTAL only.
AS_FRAC   = 0.60;   % <<< storage rated power carved out for AS (ERCOT 55-70%, mid=60%)
RES_TOTAL = 8500;   % <<< target system operating reserve MW (thermal + storage)
% Ver11: of the AS carve-out, model ONLY Non-Spin as energy-deployable at hourly
% resolution. RRS/ECRS/Reg-Up/Reg-Dn hold CAPACITY (counted in UC RzStor) but
% deploy ~0 net energy over an hour, so they are never wired into the DCOPF as an
% energy source -- they simply sit reserved. The Non-Spin slice below is the only
% part that can be released to SCED energy, and only when the system is tight.
NONSPIN_FRAC = 0.40;  % <<< Non-Spin share of the AS carve-out (winter Projected AS Req ~40%)
NS_TRIGGER   = 2500;  % <<< arm Non-Spin when pre-dispatch sys operating reserve < this
% storage reserve power actually available per battery = AS_FRAC * rated power,
% but never more than what its energy can back for one dispatch step.
PresAS = AS_FRAC .* Prated;                         % nStor x1, MW pledged to AS (all products, UC capacity)
PresNS = NONSPIN_FRAC .* PresAS;                    % nStor x1, Non-Spin MW (the only DCOPF-deployable slice)
PlfAS  = (1 - AS_FRAC) .* Prated;                    % nStor x1, MW left for load-following
% CRITICAL (Ver10 fix): carve ENERGY in the SAME proportion as power. Carving
% power only (PlfAS) while leaving full Erated behind it inflates the load-
% following battery's DURATION by ~1/(1-AS_FRAC) (e.g. a 2h fleet -> 5h), which
% makes the peak-shave QP produce wide, multi-hour, railed humps that look
% nothing like the short, spiky historical ESR. Elf keeps LF duration realistic;
% the AS share's energy is not tracked (AS is modelled as firm in the DCOPF).
Elf = (1 - AS_FRAC) .* Erated;                       % nStor x1, MWh backing load-following
soc0 = Elf .* soc0Frac;                              % nStor x1, initial SoC within the LF band
fprintf(['[reserve] storage AS carve-out: %.0f%% of %.0f MW rated = %.0f MW pledged to ' ...
         'reserve; %.0f MW / %.0f MWh left for load-following (LF duration %.1f h)\n'], ...
         100*AS_FRAC, sum(Prated), sum(PresAS), sum(PlfAS), sum(Elf), sum(Elf)/max(sum(PlfAS),eps));
fprintf(['[reserve] of that, Non-Spin slice = %.0f%% x %.0f MW = %.0f MW DCOPF-deployable; ' ...
         'remaining %.0f MW (RRS/ECRS/Reg) held as UC capacity only (no hourly energy)\n'], ...
         100*NONSPIN_FRAC, sum(PresAS), sum(PresNS), sum(PresAS)-sum(PresNS));
% ======================================================================


% System net load (pre-curtailment availability). Drives the storage QP
% (charge in troughs / discharge in peaks) and the SCUC residual.
availWind = sum(windMax(1:Th,windRows), 2, 'omitnan');
availSol  = sum(solMax (1:Th,solRows ), 2, 'omitnan');
netLoad   = sum(loadPD(1:Th,:),2) - availWind - availSol;   % Th x 1 (MW)


tic

% ===== Ver8: storage scheduled first (before UC) -- per-zone QP by ERCOT weather zone (BUS_AREA) =====
% each zone's storage chases its own zone's net load; the zone's load/solar/wind
% peaks & troughs are geographically out of phase, so shave timing staggers and the
% aggregate peak cancels structurally -- no historical peak is used as a parameter.
% decorrelation comes from grid geography (zone load is an input, not a fit target). Each zone closes SoC to e0; pDis/pChg feed the UC residual + the single DCOPF.
busArea  = mpc.bus(:, BUS_AREA);                 % nbus x 1, ERCOT weather zone 1..8
storArea = busArea(storRow);                     % nStor x 1, zone each battery belongs to
% wind/solar gen buses -> zone (so zone net load nets out its own renewables)
[~,wBusRow] = ismember(mpc.gen(windRows,GEN_BUS), mpc.bus(:,BUS_I));
[~,sBusRow] = ismember(mpc.gen(solRows ,GEN_BUS), mpc.bus(:,BUS_I));
wArea = busArea(wBusRow);   sArea = busArea(sBusRow);

pDis = zeros(Th,nStor); pChg = zeros(Th,nStor); socTr = zeros(Th,nStor);
zonesWithStor = unique(storArea)';
assert(all(isfinite(storArea)) && numel(zonesWithStor) >= 2, ...
    ['zone mapping failed: storage did not split across >=2 BUS_AREA zones. ' ...
     'Check that mpc.bus(:,BUS_AREA) carries ERCOT weather zones (1..8).']);
storResByZone = zeros(1, max(zonesWithStor));        % raw-zone-id indexed reserve MW
for z = zonesWithStor
    inZ   = (storArea == z);                     % batteries in this zone
    busZ  = (busArea == z);                      % buses in this zone
    loadZ = sum(loadPD(1:Th, busZ), 2);
    windZ = sum(windMax(1:Th, windRows(wArea==z)), 2, 'omitnan');
    solZ  = sum(solMax (1:Th, solRows (sArea==z)), 2, 'omitnan');
    netLoadZ = loadZ - windZ - solZ;             % Th x 1, this zone's net load
    % Method A: only the load-following share (PlfAS) drives the peak-shave QP;
    % the AS share (PresAS) is held out for reserve (added to UC pass 0 below).
    [dZ,cZ,socZ,~] = storageSchedulePeakShave( ...
        netLoadZ, PlfAS(inZ), Elf(inZ), eta_c, eta_d, dt, soc0(inZ));
    pDis(:,inZ) = dZ;  pChg(:,inZ) = cZ;  socTr(:,inZ) = socZ;
    storResByZone(z) = sum(PresAS(inZ));         % reserve MW this zone's storage can provide
    fprintf('  [zone %d] %d units, %.0f MW LF: peak chg %.0f / dis %.0f MW | AS reserve %.0f MW\n', ...
            z, nnz(inZ), sum(PlfAS(inZ)), max(sum(cZ,2)), max(sum(dZ,2)), storResByZone(z));
end
storNet = sum(pDis - pChg, 2);                   % Th x 1, aggregate net output (unchanged downstream)
storBusAdj = zeros(Th,nbus);
for tt = 1:Th
    storBusAdj(tt,:) = accumarray(storRow,(pChg(tt,:)-pDis(tt,:))',[nbus,1],@sum,0)';
end
fprintf(['[storage] net-load-flatten schedule: peak chg %.0f MW, peak dis %.0f MW, ' ...
         'week %.0f MWh (%.0f/day)\n'], max(sum(pChg,2)), max(sum(pDis,2)), ...
         sum(pDis(:)), sum(pDis(:))/7);

runtime_Stor = toc;
fprintf('Solved BESS Modeling in %.0f s\n', runtime_Stor);

% =====================================================================



% ======================================================================
%  PASS 0 : SCUC (MILP)  ->  commit(Th x ngen) schedule
% ----------------------------------------------------------------------
%  UC is an UPSTREAM scheduler for the two DCOPF passes below -- NOT a
%  co-optimization. It only READS the availability/load matrices built
%  above and produces a logical `commit` table. The single DCOPF pass
%  below reads `commit` as a fixed (day-ahead) on/off schedule.
%
%  LAYERING (as agreed):
%   - UC decides on/off for the dispatchable thermal fleet: gas (CC/CT/ST),
%     coal, oil (DFO), and nuclear. Nuclear is IN the MILP but pinned
%     MUST-RUN (u=1, huge min up/down); coal is committable but very sticky;
%     oil is a fast peaker. Wind/solar/hydro stay on their own series and
%     never enter the MILP. The residual the fleet must serve is
%     load - wind - solar - hydro - storage_net (storage scheduled upstream).
%   - Commitment uses RATED PMAX (GT.PMAX) and rated reserve: the
%     scheduler does NOT get to see unplanned outage.
%   - The outage/derate series (thermal_hydroCap) is applied ONLY
%     downstream, inside the DCOPF loop, as the real-time hard cap. The
%     rated-vs-cap mismatch is what makes reserve bind / load shed --
%     exactly the physics of "committed capacity that can't deliver".
%   - Copper-plate power balance (no network in the MILP); network
%     feasibility is left to the DCOPF.
%
%  storage-in-residual is implemented above. Remaining >>>EXT: zonal reserve.
% ======================================================================

fprintf('\n[pass 0] thermal SCUC (MILP, copper-plate, rated PMAX)\n');

tic

% ---- UC unit set: gas (economic) + coal (economic, sticky) + nuclear -----
%   nuclear is included but forced MUST-RUN (u=1) -- it never cycles for
%   economics over a weekly horizon; refuelling outages come in via cap
%   downstream. coal is economically committable but very sticky.
ucRows   = find(isNG | isCoal | isNuc | isOil);      % rows the UC controls
nonUcConv = setdiff(conventional_Rows, ucRows);      % other/biomass/etc: old cap path
nU       = numel(ucRows);

isGas_u  = isNG(ucRows);
isCoal_u = isCoal(ucRows);
isNuc_u  = isNuc(ucRows);
isOil_u  = isOil(ucRows);                            % DFO peakers
gtypeU   = string(GT.GENERATOR_TYPE(ucRows));         % only meaningful for gas
isCC = isGas_u & (gtypeU == "combined_cycle");
isCT = isGas_u & (gtypeU == "fired_combustion");
isST = isGas_u & (gtypeU == "steam_turbine");

mustRun = isNuc_u;                                    % nuclear only

% ---- rated parameters -------------------------------------------------
PmaxU = GT.PMAX(ucRows);            % rated MW  (UC uses this, NOT cap)
PminU = GT.PMIN(ucRows);            % rated min-load MW

% ---- cost coefficients: read gencost, fall back to fuel/type defaults --
c1U = zeros(nU,1); c0U = zeros(nU,1); suU = zeros(nU,1);   % marginal / no-load / startup
for k = 1:nU
    r  = ucRows(k);
    md = mpc.gencost(r,MODEL);  nc = mpc.gencost(r,NCOST);
    cf = mpc.gencost(r, COST:COST+nc-1);
    if md == POLYNOMIAL
        % cf = [c_n ... c2 c1 c0]; linear = cf(end-1), quadratic = cf(end-2)
        lin = cf(end-1);
        quad = (numel(cf) >= 3) * cf(max(end-2,1));
        c1U(k) = lin + quad * PmaxU(k);      % average marginal cost at Pmax
        c0U(k) = cf(end);
    end
    suU(k) = mpc.gencost(r, STARTUP);
end

% startup $/MW-of-Pmax fallbacks: CT/oil cheap-fast, CC/ST mid, coal high, nuc n/a
suPerMW = 45*ones(nU,1);
suPerMW(isCC)=55; suPerMW(isCT)=35; suPerMW(isST)=70;
suPerMW(isCoal_u)=120; suPerMW(isNuc_u)=0; suPerMW(isOil_u)=30;
badsu = ~isfinite(suU) | suU <= 0; suU(badsu) = suPerMW(badsu).*PmaxU(badsu);
c0U(~isfinite(c0U)) = 0;

% ---- min up / down by fuel-type (hours) -------------------------------
minUp = 6*ones(nU,1);  minDn = 6*ones(nU,1);
minUp(isCT)=1;    minDn(isCT)=1;
minUp(isST)=8;    minDn(isST)=6;
minUp(isOil_u)=1;    minDn(isOil_u)=1;         % DFO: fast-start peaker
minUp(isCoal_u)=6;  minDn(isCoal_u)=6;      % coal: very sticky
minUp(isNuc_u)=Th;   minDn(isNuc_u)=Th;       % nuclear: never cycles (also mustRun)

% ---- residual: load minus wind/solar/hydro (nuc & coal are now IN the UC) --
loadTot  = sum(loadPD(1:Th,:), 2);
hydroAv  = sum(hydroMax(1:Th,hydroRows), 2, 'omitnan');
ucResid  = max(loadTot - availWind - availSol - hydroAv - storNet, 0);   % Th x 1  (storage in residual)

% ---- reserve requirement: ZONAL, thermal + STORAGE (Ver8b) ------------
% operating reserve is now met by BOTH thermal headroom AND storage AS carve-out.
% Per zone z:
%   Rz(z)      = zone's share of RES_TOTAL, split by peak load
%   RzStor(z)  = storage reserve MW physically present in zone z (PresAS summed)
%   RzThermal(z) = max(Rz - RzStor, 0)  -> the residual thermal must still carry
% Storage discharges FIRST as reserve (ERCOT: batteries dominate RRS/ECRS), so
% thermal only commits for the LEFTOVER. This is what lets UC stop over-committing
% coal/gas just to hold headroom -- the batteries hold most of it now.
% Zones are the SAME thermal-bearing zones as before; a zone's storage reserve is
% only counted if that zone also holds UC thermal (copper-plate UC can't route a
% pure storage pocket's reserve elsewhere -- same caveat as the load pockets).
[~,ucBusRow] = ismember(mpc.gen(ucRows,GEN_BUS), mpc.bus(:,BUS_I));
zoneRaw  = busArea(ucBusRow);                 % nU x1 raw ERCOT zone id per UC unit
zoneList = unique(zoneRaw)';                  % zones that actually hold UC thermal
nZ       = numel(zoneList);
[~,zoneU]= ismember(zoneRaw, zoneList);       % nU x1, contiguous zone index 1..nZ

zoneMask = false(nbus, nZ);
for j = 1:nZ, zoneMask(:,j) = (busArea == zoneList(j)); end
zoneLoad = loadPD(1:Th,:) * zoneMask;         % Th x nZ  hourly load per thermal zone
zonePeak = max(zoneLoad, [], 1)';             % nZ x1    peak load per zone
Rtot     = RES_TOTAL;                          % system operating reserve MW (param)
Rz       = Rtot * zonePeak / sum(zonePeak);   % nZ x1, total per-zone need, by peak share

% storage reserve available per THERMAL zone (align raw-zone storResByZone -> 1..nZ)
RzStor    = zeros(nZ,1);
for j = 1:nZ
    if zoneList(j) <= numel(storResByZone)
        RzStor(j) = storResByZone(zoneList(j));
    end
end
RzStor    = min(RzStor, Rz);                    % storage can't over-provide its zone's need
RzThermal = max(Rz - RzStor, 0);               % nZ x1, LEFTOVER that thermal must carry
fprintf(['[pass 0] operating reserve %d MW over %d zones | storage carries %.0f MW, ' ...
         'thermal carries %.0f MW\n'], Rtot, nZ, sum(RzStor), sum(RzThermal));
fprintf('         per-zone total [%s] | storage [%s] | thermal [%s] MW\n', ...
        strjoin(compose('%.0f', Rz'),' '), strjoin(compose('%.0f', RzStor'),' '), ...
        strjoin(compose('%.0f', RzThermal'),' '));

% ---- rolling-horizon solve (keep BLK, look ahead LA) ------------------
BLK = 24; LA = 12;
PEN = 9000;  PENr = 1000;           % $/MWh slacks: unserved >> reserve short

commitU = false(Th,nU);
u0     = double(mustRun);                         % must-run units start ON
onFor  = mustRun*Th;                              % pretend long-on for must-run
offFor = (~mustRun).*max(minDn);                  % others idle at t=0
t0 = 1;
while t0 <= Th
    t1  = min(t0+BLK-1, Th);                 % last hour to KEEP
    t1e = min(t0+BLK+LA-1, Th);              % last hour to SOLVE (with look-ahead)
    idx = t0:t1e;   keep = t1 - t0 + 1;
    try
        uSol = solveUCblock(ucResid(idx), RzThermal, zoneU, PmaxU, PminU, ...
                    c1U, c0U, suU, minUp, minDn, mustRun, u0, onFor, offFor, PEN, PENr);
    catch ME
        warning('[pass 0] MILP block %d-%d failed (%s) -> merit-order fallback', ...
                 t0, t1e, ME.message);
        % rare safety net: stays SYSTEM-level (uses the summed THERMAL reserve),
        % since the greedy merit order has no per-zone residual to allocate against.
        uSol = meritFallback(ucResid(idx), sum(RzThermal)*ones(numel(idx),1), PmaxU, PminU, c1U);
        uSol(:, mustRun) = 1;                 % keep nuclear on in the fallback too
    end
    commitU(t0:t1, :) = logical(uSol(1:keep, :));

    % carry min up/down state into next block (from the last KEPT hour)
    lastU = commitU(t1, :)';   u0 = double(lastU);
    for k = 1:nU
        h = 0; tt = t1;
        if lastU(k)
            while tt>=1 && commitU(tt,k), h=h+1; tt=tt-1; end
            onFor(k) = h; offFor(k) = 0;
        else
            while tt>=1 && ~commitU(tt,k), h=h+1; tt=tt-1; end
            offFor(k) = h; onFor(k) = 0;
        end
    end
    t0 = t1 + 1;
end

commit = true(Th, ngen);            % wind/solar/hydro/biomass/other untouched
commit(:, ucRows) = commitU;        % gas + coal + nuclear + oil carry UC on/off
fprintf(['[pass 0] committed  gas %.0f/%d  coal %.0f/%d  nuc %.0f/%d  oil %.0f/%d' ...
         ' (mean units on) | mean cap on %.0f MW\n'], ...
        mean(sum(commitU(:,isGas_u ),2)), nnz(isGas_u), ...
        mean(sum(commitU(:,isCoal_u),2)), nnz(isCoal_u), ...
        mean(sum(commitU(:,isNuc_u ),2)), nnz(isNuc_u), ...
        mean(sum(commitU(:,isOil_u ),2)), nnz(isOil_u), ...
        mean(sum(commitU .* PmaxU', 2)));

runtime_UC = toc;
fprintf('Solved Unit Commitment in %.0f s\n', runtime_UC);

%% 3. Single-pass DCOPF: storage FIXED to its net-load-flatten schedule ------
ws = warning('off','MATLAB:nearlySingularMatrix');
tic

cost=nan(Th,1); success=false(Th,1); loadServed=nan(Th,1);
windGen=nan(Th,1); solGen=nan(Th,1); windCut=nan(Th,1); solCut=nan(Th,1);
storDis=nan(Th,1); storChg=nan(Th,1); genFuel=nan(Th,9);
% Per-bus generation injection by fuel (Th x nbus), for R3.2 case-build export.
genByBus_wind = zeros(Th,nbus); genByBus_sol = zeros(Th,nbus);
genByBus_th   = zeros(Th,nbus); genByBus_hyd = zeros(Th,nbus);
lmp = nan(Th,nbus);  lmp_base = nan(Th,nbus);   % final / baseline nodal LMP
storBusAdj = zeros(Th,nbus);                    % storage load delta (+chg / -dis)
loadShed  = nan(Th,1);                          % MW load shed per hour (this pass)
shedByBus = zeros(Th,nbus);                     % per-bus load shed (this pass)
shed_base = nan(Th,1);                          % baseline shed (single pass: = final)
% pDis/pChg/socTr were produced by storageSchedulePeakShave above -- do NOT zero them here, or the schedule is wiped
pChgAct = zeros(Th,nStor); pDisAct = zeros(Th,nStor);   % OPF-realized

% Number of polynomial-coefficient columns physically present in gencost.
% We repurpose the storage rows as dispatchable loads (PMIN<0) in pass 2 and
% write a LINEAR charge bid, so we need the c1 slot regardless of whether the
% table is stored as NCOST=2 (linear) or NCOST=3 (quadratic).
ncoef = size(mpc.gencost,2) - COST + 1;
assert(ncoef >= 2, 'gencost needs >= 2 coefficient columns for a linear charge bid');

%% ---- Load-shed virtual generators (VOLL method) ----------------------
% One dispatchable "shed gen" per LOAD bus. A positive injection = load
% relief, i.e. curtailed demand at that bus. It is priced at VOLL; because
% DCOPF minimises cost and VOLL >> any real marginal cost, the solver only
% sheds when it MUST (generation/transmission cannot serve the load), and with
% a uniform VOLL it minimises TOTAL MW shed. PMAX is refreshed each hour to the
% bus's remaining servable load. Rows are appended at the END so every mask
% built on the original ngen rows (isWind/isSol/storIdx/...) stays valid, and
% the fuel masks never pick up a shed gen.
VOLL        = 5000;                              % $/MWh, uniform value of lost load
loadBusRows = find(any(loadPD > 0, 1));          % rows into mpc.bus that ever carry load
nShed       = numel(loadBusRows);
shedIdx     = ngen + (1:nShed)';                 % their rows in the augmented mpc.gen

shedGen = zeros(nShed, size(mpc.gen,2));
shedGen(:,GEN_BUS)    = mpc.bus(loadBusRows, BUS_I);
shedGen(:,MBASE)      = mpc.baseMVA;
shedGen(:,GEN_STATUS) = 1;
shedGen(:,VG)         = 1;
shedGen(:,PMAX)       = 0;                        % set per hour inside the loop
shedGen(:,PMIN)       = 0;
mpc.gen = [mpc.gen; shedGen];

shedCost = zeros(nShed, size(mpc.gencost,2));
shedCost(:,MODEL)        = POLYNOMIAL;
shedCost(:,NCOST)        = ncoef;                % keep table width; quadratic term stays 0
shedCost(:,COST+ncoef-2) = VOLL;                 % linear coefficient c1 = VOLL
mpc.gencost = [mpc.gencost; shedCost];

% MATPOWER carries per-generator side tables (gentype/genfuel/gen_name) that
% ext2int reorders ALONGSIDE mpc.gen. They still have the original ngen rows,
% so growing mpc.gen without growing them makes ext2int index out of bounds.
% Pad every gen-length side table by nShed rows (class-adaptive).
for f = {'gentype','genfuel','gen_name'}
    fn = f{1};
    if isfield(mpc, fn) && size(mpc.(fn),1) == ngen
        v = mpc.(fn);
        if iscell(v)
            mpc.(fn) = [v; repmat({'LS'}, nShed, 1)];       % load-shed tag
        elseif isstring(v)
            mpc.(fn) = [v; repmat("LS", nShed, 1)];
        elseif ischar(v)
            mpc.(fn) = char([cellstr(v); repmat({'LS'}, nShed, 1)]);
        else
            mpc.(fn) = [v; zeros(nShed, size(v,2))];         % numeric per-gen vector
        end
    end
end
fprintf('Load-shed gens appended: %d load buses (VOLL = $%g/MWh)\n', nShed, VOLL);

%% ---- Charge-relief virtual generators (option B: curtailable charge) ---
% Charge stays a FIXED bus load (added to PD each hour, so it must be balanced),
% but one "charge-relief" gen per storage BUS lets DCOPF back the charge off
% ONLY under congestion. A positive injection here = charge NOT served this hour.
% Priced at VOLL_CHG, chosen so:
%   real marginal cost  <<  VOLL_CHG  <  VOLL(native, $5000)
% => charge is served by default (relief is expensive: quasi-hard constraint), yet native load
%    is always protected first (native shed only after charge has fully yielded).
% Raise VOLL_CHG toward VOLL to make charge nearly firm; lower it to let charge
% yield to merely-expensive (not infeasible) dispatch too.
VOLL_CHG   = 2500;                                % $/MWh, "value" of served charge
chgBusRows = unique(storRow);                     % distinct storage buses
nChg       = numel(chgBusRows);
chgReliefIdx = (ngen + nShed) + (1:nChg)';        % rows in augmented mpc.gen

chgGen = zeros(nChg, size(mpc.gen,2));
chgGen(:,GEN_BUS)    = mpc.bus(chgBusRows, BUS_I);
chgGen(:,MBASE)      = mpc.baseMVA;
chgGen(:,GEN_STATUS) = 1;
chgGen(:,VG)         = 1;
chgGen(:,PMAX)       = 0;                          % set per hour inside the loop
chgGen(:,PMIN)       = 0;
mpc.gen = [mpc.gen; chgGen];

chgCost = zeros(nChg, size(mpc.gencost,2));
chgCost(:,MODEL)        = POLYNOMIAL;
chgCost(:,NCOST)        = ncoef;
chgCost(:,COST+ncoef-2) = VOLL_CHG;               % linear coefficient c1 = VOLL_CHG
mpc.gencost = [mpc.gencost; chgCost];

% pad the per-gen side tables again (they are now ngen+nShed long) by nChg
for f = {'gentype','genfuel','gen_name'}
    fn = f{1};
    if isfield(mpc, fn) && size(mpc.(fn),1) == ngen + nShed
        v = mpc.(fn);
        if iscell(v)
            mpc.(fn) = [v; repmat({'CR'}, nChg, 1)];        % charge-relief tag
        elseif isstring(v)
            mpc.(fn) = [v; repmat("CR", nChg, 1)];
        elseif ischar(v)
            mpc.(fn) = char([cellstr(v); repmat({'CR'}, nChg, 1)]);
        else
            mpc.(fn) = [v; zeros(nChg, size(v,2))];
        end
    end
end
fprintf('Charge-relief gens appended: %d storage buses (VOLL_CHG = $%g/MWh)\n', nChg, VOLL_CHG);

%% ---- Emergency storage-reserve gens (Ver9: deploy AS carve-out before shed) ---
% Real ERCOT releases reserved AS capacity into SCED energy BEFORE shedding firm
% load, along a price curve (ORDC pre-2025 / ASDC under RTC+B). We reproduce that with one
% emergency-discharge gen per storage unit (1:1 with storIdx, same bus), priced
% at AS_DEPLOY: above real marginal cost (so it only fires when a pocket is tight)
% but below VOLL (so it ALWAYS beats shedding native load). SCED deploys it
% endogenously in the same solve -- no separate shed-detection pass needed.
AS_DEPLOY = 800;                                  % $/MWh: real cost < AS_DEPLOY < VOLL (tunable)
emgIdx = (ngen + nShed + nChg) + (1:nStor)';       % rows in augmented mpc.gen (1:1 with storIdx)

emgGen = zeros(nStor, size(mpc.gen,2));
emgGen(:,GEN_BUS)    = mpc.gen(storIdx, GEN_BUS);  % same bus as its battery
emgGen(:,MBASE)      = mpc.baseMVA;
emgGen(:,GEN_STATUS) = 1;
emgGen(:,VG)         = 1;
emgGen(:,PMAX)       = 0;                           % set per hour inside the loop
emgGen(:,PMIN)       = 0;
mpc.gen = [mpc.gen; emgGen];

emgCost = zeros(nStor, size(mpc.gencost,2));
emgCost(:,MODEL)        = POLYNOMIAL;
emgCost(:,NCOST)        = ncoef;
emgCost(:,COST+ncoef-2) = AS_DEPLOY;               % linear coefficient c1 = AS_DEPLOY
mpc.gencost = [mpc.gencost; emgCost];

for f = {'gentype','genfuel','gen_name'}           % pad side tables (now ngen+nShed+nChg long)
    fn = f{1};
    if isfield(mpc, fn) && size(mpc.(fn),1) == ngen + nShed + nChg
        v = mpc.(fn);
        if iscell(v),        mpc.(fn) = [v; repmat({'ER'}, nStor, 1)];
        elseif isstring(v),  mpc.(fn) = [v; repmat("ER", nStor, 1)];
        elseif ischar(v),    mpc.(fn) = char([cellstr(v); repmat({'ER'}, nStor, 1)]);
        else,                mpc.(fn) = [v; zeros(nStor, size(v,2))];
        end
    end
end
fprintf('Non-Spin storage gens appended: %d units (AS_DEPLOY = $%g/MWh, %.0f MW deployable, armed when reserve < %d MW)\n', ...
        nStor, AS_DEPLOY, sum(PresNS), NS_TRIGGER);

% mapping used to aggregate curtailment by bus (windRows/solRows are all <= ngen, indexing original gen rows)
[~,wbrow] = ismember(mpc.gen(windRows,GEN_BUS), mpc.bus(:,BUS_I));
[~,sbrow] = ismember(mpc.gen(solRows ,GEN_BUS), mpc.bus(:,BUS_I));
% bus-row maps for per-bus generation export (thermal = NG+Coal+Nuc+Oil; hydro separate)
thRows   = find(isNG | isCoal | isNuc | isOil);
[~,tbrow] = ismember(mpc.gen(thRows  ,GEN_BUS), mpc.bus(:,BUS_I));
[~,hbrow] = ismember(mpc.gen(find(isHyd),GEN_BUS), mpc.bus(:,BUS_I));


% ===== Ver7: single DCOPF -- storage fixed to the net-load-following schedule built above =====
cost(:)=NaN; success(:)=false; loadServed(:)=NaN;
windGen(:)=NaN; solGen(:)=NaN; windCut(:)=NaN; solCut(:)=NaN;
storDis(:)=NaN; storChg(:)=NaN; genFuel(:)=NaN;
loadShed(:)=NaN; shedByBus(:)=0;
curtByBus = zeros(Th,nbus);
lmp = nan(Th,nbus);
pChgAct(:)=0; pDisAct(:)=0;
emgDeploy = zeros(Th,1); emgByStor = zeros(Th,nStor);  % Ver9: emergency reserve deployed (MW)
reserveProxyTr = nan(Th,1); nsArmed = false(Th,1);     % Ver11: pre-dispatch reserve (MW) & NS arm flag
% Real SoC (pure version): recursed hour-by-hour from the DCOPF's actual charge/discharge,
% allowed to drift from the planned socTr with no tracking compensation. SoC bounds act only as
% hard per-hour charge/discharge limits (mirrors single-interval ERCOT SCED: start SoC as input, SoC limits as constraints, time evolution = result of actual dispatch, not a look-ahead product).
socReal   = soc0(:);                 % nStor x1
socRealTr = nan(Th, nStor);          % real SoC trajectory (vs. planned socTr)
fprintf('\n[DCOPF] single pass — storage FIXED to net-load-flatten schedule (no price-taker, no 2-pass)\n');

for tt = 1:Th
    m = mpc;
    m.gen(windRows,PMAX) = windMax(tt,windRows)';  m.gen(windRows,PMIN) = 0;
    m.gen(solRows ,PMAX) = solMax (tt,solRows )';  m.gen(solRows ,PMIN) = 0;

    % UC thermal: on/off from SCUC (now on storage-adjusted residual) × real-time cap
    on   = commit(tt, ucRows)';
    capU = conventional_Cap(tt, ucRows)';
    nanU = isnan(capU);  capU(nanU) = GT.PMAX(ucRows(nanU));
    m.gen(ucRows,PMAX)       = capU .* on;
    m.gen(ucRows,PMIN)       = min(m.gen(ucRows,PMIN), capU) .* on;
    m.gen(ucRows,GEN_STATUS) = on;

    cap = conventional_Cap(tt,nonUcConv)';
    m.gen(nonUcConv,PMAX) = cap;
    m.gen(nonUcConv,PMIN) = min(m.gen(nonUcConv,PMIN), cap);

    m.gen(hydroRows ,PMAX) = hydroMax(tt,hydroRows)';
    m.gen(hydOutRows,PMAX) = min(hydroMax(tt,hydOutRows)', conventional_Cap(tt,hydOutRows)');
    m.gen(hydroRows ,PMIN) = 0;

    m.bus(:,PD) = loadPD(tt,:)';

    % ===== storage: FIXED to schedule. discharge = fixed gen injection; charge = fixed bus load =====
    % (avoids the ambiguity of a "negative-Pmax gen" being treated as a dispatchable load in MATPOWER.)
    disS = pDis(tt,:)';   chgS = pChg(tt,:)';           % nStor x1, both >= 0
    disCap = min(disS, socReal.*eta_d./dt);             % discharge cap = min(schedule, what real SoC allows)
    m.gen(storIdx,PMIN)       = 0;                       % discharge: down-adjustable (0<=p<=disCap) -- curtailed by DCOPF under congestion
    m.gen(storIdx,PMAX)       = disCap;                  % cap = scheduled discharge after SoC constraint
    m.gen(storIdx,PG)         = disCap;                  % warm start
    m.gen(storIdx,GEN_STATUS) = 1;
    m.gencost(storIdx,MODEL)  = POLYNOMIAL;
    m.gencost(storIdx,NCOST)  = ncoef;
    m.gencost(storIdx,COST:COST+ncoef-1) = 0;
    % give discharge a tiny negative price: with no congestion it strictly prefers full output
    % (= scheduled discharge), curtailed only when a line constraint binds -- avoids fighting $0 renewable curtailment for shave when unconstrained (which would break the already well-matched Net Output).
    m.gencost(storIdx, COST+ncoef-2) = -1e-2;           % linear term $/MWh
    % charge: added as fixed load to the storage bus PD (served by DCOPF with the cheapest local source = otherwise-curtailed solar)
    chgCap = min(chgS, max(Elf - socReal,0)./(eta_c.*dt));  % charge cap = min(schedule, real SoC headroom, LF energy band)
    chgByBus = accumarray(storRow, chgCap, [nbus,1]);
    m.bus(:,PD) = m.bus(:,PD) + chgByBus;
    % charge-relief gen: cap = this hour's scheduled charge at the bus; DCOPF backs charge off only under congestion (quasi-hard constraint)
    reliefCap = chgByBus(chgBusRows);
    m.gen(chgReliefIdx,PMAX)       = reliefCap;
    m.gen(chgReliefIdx,PMIN)       = 0;
    m.gen(chgReliefIdx,GEN_STATUS) = 1;
    m.gen(chgReliefIdx(reliefCap <= 1e-9), GEN_STATUS) = 0;

    % load-shed gens: cap = servable NATIVE load this hour (charge load excluded, cannot be shed)
    servable = max(loadPD(tt,loadBusRows)', 0);
    m.gen(shedIdx,PMAX)       = servable;
    m.gen(shedIdx,PMIN)       = 0;
    m.gen(shedIdx,GEN_STATUS) = 1;
    m.gen(shedIdx(servable <= 1e-9), GEN_STATUS) = 0;

    % ===== Non-Spin reserve (Ver11): condition-gated FIRM deployment =====
    % Only the Non-Spin slice (PresNS = NONSPIN_FRAC*PresAS) is deployable as
    % energy at hourly resolution; RRS/ECRS/Reg stay reserved as UC capacity and
    % never appear here. Mirrors ERCOT's operator Non-Spin deployment on PRC<3200:
    % the slice is ARMED (status=1) only when the PRE-DISPATCH system operating
    % reserve falls below NS_TRIGGER, and DCOPF then dispatches only as much as the
    % pocket needs (priced AS_DEPLOY, above thermal marginal, below VOLL -> deployed
    % before any native load is shed, partial deployment emerges endogenously).
    %
    % Pre-dispatch reserve proxy (the trigger is a DCOPF INPUT, so it must NOT use
    % post-solve PG): total available capacity minus net-of-storage demand. Includes
    % FULL renewable availability (dispatchable-down headroom counts as capacity) and
    % EXCLUDES the Non-Spin slice itself (that is what we are deciding to arm). PRC is
    % online headroom, so offline NS is correctly left out of the proxy.
    % NOTE: hydOutRows is a SUBSET of hydroRows (already capped in m.gen above), and
    % nonUcConv/ucRows are disjoint non-hydro sets -- so these terms do not overlap.
    availCapTT  = sum(m.gen(ucRows,PMAX)) + sum(m.gen(nonUcConv,PMAX)) ...
                + sum(m.gen(hydroRows,PMAX)) ...
                + sum(m.gen(windRows,PMAX)) + sum(m.gen(solRows,PMAX));   % MW available to serve/hold
    netDemandTT = sum(m.bus(:,PD)) - sum(disCap);        % load + charging - storage LF discharge
    reserveProxy = availCapTT - netDemandTT;             % pre-dispatch system operating reserve (MW)
    armNS = (reserveProxy < NS_TRIGGER);                 % ERCOT PRC<3200 analogue
    reserveProxyTr(tt) = reserveProxy;  nsArmed(tt) = armNS;
    % FIRM slice (SoC-independent) per your choice: assume Non-Spin battery energy
    % is ample, so armed MW = PresNS, unarmed = 0. KNOWN LIMITATION (unchanged): with
    % no energy budget a MULTI-HOUR sub-NS_TRIGGER stretch is under-shed / LMP
    % under-priced because this slice never depletes -- now bounded to tight hours by
    % the gate. To fix later, clip emgCap by socReal and drain it.
    emgCap    = PresNS .* armNS;                                 % nStor x1, FIRM MW when armed, else 0
    m.gen(emgIdx,PMIN)       = 0;
    m.gen(emgIdx,PMAX)       = emgCap;
    m.gen(emgIdx,GEN_STATUS) = 1;
    m.gen(emgIdx(emgCap <= 1e-9), GEN_STATUS) = 0;

    rr = rundcopf(m, mpopt);
    success(tt) = rr.success;
    if rr.success
        lmp(tt,:) = rr.bus(:,LAM_P)';  pg = rr.gen(:,PG);  cost(tt) = rr.f;
        shedMW = max(rr.gen(shedIdx, PG), 0);
        loadShed(tt) = sum(shedMW);  shedByBus(tt, loadBusRows) = shedMW';
        windGen(tt) = sum(pg(isWind));  solGen(tt) = sum(pg(isSol));
        windCut(tt) = sum(max(windMax(tt,windRows)' - pg(windRows), 0));
        solCut(tt)  = sum(max(solMax(tt,solRows)'  - pg(solRows ), 0));
        gap = [max(windMax(tt,windRows)' - pg(windRows), 0);
               max(solMax (tt,solRows )' - pg(solRows ), 0)];
        curtByBus(tt,:) = accumarray([wbrow; sbrow], gap, [nbus,1])';
        % discharge = OPF actual output (curtailable); charge = amount entering PD (=chgCap) minus what the relief gen backed off
        % Ver10: SoC is drained ONLY by load-following discharge (disLF). The AS
        % carve-out deployment (emgAct) is a FIRM reserve that self-manages its own
        % stock -- it does NOT touch socReal. disAct (=disLF+emgAct) is reported for
        % fuel-mix / totals only. This keeps the DCOPF SoC ledger consistent with
        % the UC assumption that storage holds its AS reserve every hour.
        emgAct = max(rr.gen(emgIdx,PG), 0);                  % AS carve-out deployed this hour (MW)
        emgByStor(tt,:) = emgAct';  emgDeploy(tt) = sum(emgAct);
        disLF  = max(pg(storIdx),0);                          % load-following discharge (drains SoC)
        disAct = disLF + emgAct;                              % total discharge (report only)
        chgRelief = max(rr.gen(chgReliefIdx,PG), 0);          % charge MW backed off per storage bus
        reliefByBus = zeros(nbus,1); reliefByBus(chgBusRows) = chgRelief;
        frac     = chgCap ./ max(chgByBus(storRow), eps);     % split the cutback back to units by share (chgByBus: see charge block above)
        chgAct   = max(chgCap - reliefByBus(storRow).*frac, 0);
        % real SoC recursion (pure version: strictly from DCOPF actual charge/discharge, order = charge then discharge, consistent with the QP)
        % note: AS deployment (emgAct) does NOT draw down SoC -- only load-following discharge disLF does.
        socReal = min(socReal + chgAct.*eta_c.*dt, Elf);      % LF energy band (AS energy not tracked)
        socReal = max(socReal - disLF.*dt./eta_d, 0);         % <- disLF, excludes AS deployment
        socRealTr(tt,:) = socReal';
        pDisAct(tt,:) = disAct';  pChgAct(tt,:) = chgAct';
        storDis(tt) = sum(disAct);  storChg(tt) = sum(chgAct);
        genFuel(tt,:) = [sum(pg(isWind)) sum(pg(isSol)) sum(pg(isNG)) ...
                         sum(pg(isCoal)) sum(pg(isHyd)) sum(pg(isNuc)) ...
                         (storDis(tt)-storChg(tt)) sum(pg(isOil)) sum(pg(isOth))];
        % per-bus generation injection by fuel (same accumarray pattern as curtByBus)
        genByBus_wind(tt,:) = accumarray(wbrow, pg(windRows),      [nbus,1])';
        genByBus_sol (tt,:) = accumarray(sbrow, pg(solRows ),      [nbus,1])';
        genByBus_th  (tt,:) = accumarray(tbrow, pg(thRows  ),      [nbus,1])';
        genByBus_hyd (tt,:) = accumarray(hbrow, pg(find(isHyd)),   [nbus,1])';
        loadServed(tt) = sum(loadPD(tt,:)) - loadShed(tt);
    end
    if mod(tt,25)==0, fprintf('  ...%d/%d hours\n', tt, Th); end
end
lmp_base  = lmp;                 % single pass: baseline := final (fallback for out)
shed_base = loadShed;            % same as above

% ===== storage macro self-check: fleet-wide charge/discharge curves =====
chgH = sum(pChgAct,2,'omitnan');  disH = sum(pDisAct,2,'omitnan');   % fleet MW per hour
fprintf('\n[macro] peak charge %.0f MW | peak discharge %.0f MW | net output range [%+.0f, %+.0f] MW\n', ...
        max(chgH), max(disH), min(disH-chgH), max(disH-chgH));
fprintf('[macro] weekly charge %.0f MWh (%.0f/day) | weekly discharge %.0f MWh (%.0f/day) | end SoC %.0f%% full\n', ...
        sum(chgH), sum(chgH)/7, sum(disH), sum(disH)/7, 100*sum(socTr(end,:))/max(sum(Elf),eps));
    fprintf('[macro] target: peak ~±3000 MW, daily cycling, end SoC not stuck full (no params, magnitude set by ΣP/ΣE)\n');

% ===== Non-Spin arm/deploy self-check (Ver11) =====
% Max consecutive armed run flags the firm-slice risk: a long sub-NS_TRIGGER
% stretch is where "battery energy ample" over-supplies and under-sheds. If this
% is large, revisit the SoC/duration budget deferred in the emgCap block.
if any(nsArmed)
    d = diff([0; nsArmed(:); 0]);  runLen = find(d==-1) - find(d==1);
    maxRun = max(runLen);
else
    maxRun = 0;
end
fprintf(['\n[non-spin] armed %d/%d h (%.0f%%) | max consecutive armed run %d h | ' ...
         'deployed %.0f MWh over week (peak %.0f MW) | min pre-dispatch reserve %.0f MW\n'], ...
        nnz(nsArmed), Th, 100*nnz(nsArmed)/Th, maxRun, ...
        sum(emgDeploy), max(emgDeploy), min(reserveProxyTr));

runtime = toc;
warning(ws);





%% 4. Collect + report ---------------------------------------------------
out = struct();
out.hours = hours(1:Th);   out.success = success;   out.cost = cost;
out.loadServed = loadServed;
out.loadPD = loadPD;
out.windGen = windGen;     out.windCut = windCut;
out.solGen  = solGen;      out.solCut  = solCut;
out.storDis = storDis;     out.storChg = storChg;   out.socTr = socTr;
out.emgDeploy = emgDeploy;   out.emgByStor = emgByStor;   out.AS_DEPLOY = AS_DEPLOY;  % Ver9: emergency reserve (MW/h & per-unit)
out.reserveProxy = reserveProxyTr;  out.nsArmed = nsArmed;   % Ver11: pre-dispatch reserve (MW) & NS arm flag
out.NONSPIN_FRAC = NONSPIN_FRAC;    out.NS_TRIGGER = NS_TRIGGER;   out.PresNS = PresNS;
out.socRealTr = socRealTr;   % real SoC trajectory (recursed from DCOPF actual charge/discharge)
out.pChgWant = pChg;       out.pDisWant = pDis;      % desired (from greedy plan)
out.pChgAct  = pChgAct;    out.pDisAct  = pDisAct;   % realized (OPF / SoC-capped)
out.genFuel = genFuel;
% per-bus generation injection by fuel (Th x nbus) -- R3.2 active-power case-build inputs
out.genByBus_wind = genByBus_wind;   out.genByBus_sol = genByBus_sol;
out.genByBus_th   = genByBus_th;     out.genByBus_hyd = genByBus_hyd;
out.lmp     = lmp;         out.lmp_base = lmp_base;   % final & baseline nodal LMP
out.loadShed   = loadShed;   out.shed_base = shed_base;  % final & baseline shed (MW/h)
out.shedByBus  = shedByBus;                              % per-bus shed, final pass (Th x nbus)
out.VOLL       = VOLL;
out.storBusAdj = storBusAdj;
out.fuelOrder = {'wind','solar','natural gas','coal','hydro','nuclear','energy storage','fuel oil','other'};
out.curtByBus = curtByBus;

% ===== extended diagnostics: conventional capacity, UC schedule, faults ====
% (added so the results file is self-contained for post-hoc fuel analysis)

% ---- conventional generator capacity (hourly, derate+outage applied) ------
% conventional_Cap is Th x ngen with NaN for gens that have no availability
% series. Store the compact slice over gens that DO, plus a key mapping each
% column back to its mpc.gen row, and per-gen static info for grouping.
capRows = find(any(~isnan(conventional_Cap(1:Th,:)),1));   % gens with availability data
out.conv = struct();
out.conv.rows      = capRows(:);                       % col j <-> mpc.gen row capRows(j)
out.conv.genId     = GT.GEN_I(capRows);
out.conv.fuel      = string(GT.FUEL_TYPE(capRows));
out.conv.type      = string(GT.GENERATOR_TYPE(capRows));
out.conv.Pmax      = GT.PMAX(capRows);                 % rated MW (nameplate)
out.conv.Pmin      = GT.PMIN(capRows);
out.conv.capHourly = conventional_Cap(1:Th, capRows);  % Th x nConv, MW available

% ---- unit commitment schedule (SCUC on/off) -------------------------------
% commit is Th x ngen (true for non-UC rows). Store the UC-controlled slice
% with its mapping, plus the full logical table for convenience.
out.uc = struct();
out.uc.rows       = ucRows(:);                         % rows the SCUC controlled
out.uc.genId      = GT.GEN_I(ucRows);
out.uc.fuel       = string(GT.FUEL_TYPE(ucRows));
out.uc.type       = string(GT.GENERATOR_TYPE(ucRows));
out.uc.Pmax       = GT.PMAX(ucRows);
out.uc.commit     = logical(commit(1:Th, ucRows));     % Th x nUC on/off
out.uc.commitFull = logical(commit(1:Th, :));          % Th x ngen (all rows)

% ---- fault / outage event record (per-event, from the separate generator) -
% NB: conventional_generator_unavailabilty_record is only a PATH until now;
% read the event table here so the results file carries it too.
try
    out.faultRecord = readtable(conventional_generator_unavailabilty_record);
catch ME
    warning('could not read fault record (%s): %s', ...
            conventional_generator_unavailabilty_record, ME.message);
    out.faultRecord = table();
end

fprintf('\n===== SUMMARY =====\n');
fprintf('Solved BESS Modeling in %.0f s\n', runtime_Stor);
fprintf('Solved Unit Commitment in %.0f s\n', runtime_UC);
fprintf('Solved %d/%d hours in %.0f s\n', nnz(success), Th, runtime);
fprintf('Total system cost : %.3e $\n', sum(cost,'omitnan'));
fprintf('Wind  gen/curtail : %.0f / %.0f MWh (curtailed %.1f%%)\n', sum(windGen,'omitnan'), sum(windCut,'omitnan'), 100*sum(windCut,'omitnan')/sum(windGen,'omitnan'));
fprintf('Solar gen/curtail : %.0f / %.0f MWh (curtailed %.1f%%)\n', sum(solGen ,'omitnan'), sum(solCut ,'omitnan'), 100*sum(solCut,'omitnan')/sum(solGen,'omitnan'));
sd = sum(storDis,'omitnan'); sc = sum(storChg,'omitnan');
fprintf('Stor  dis/charge  : %.0f / %.0f MWh\n', sd, sc);
% how much charge the DCOPF served vs what the schedule wanted (charge can now be curtailed under congestion)
scWant = sum(pChg(:));
fprintf('Charge want/served: %.0f / %.0f MWh  (curtailed %.1f%%)\n', ...
        scWant, sc, 100*max(scWant-sc,0)/max(scWant,eps));
% how much discharge the DCOPF served vs what the schedule wanted (discharge can now be curtailed under congestion)
sdWant = sum(pDis(:));
fprintf('Discharge want/served: %.0f / %.0f MWh  (curtailed %.1f%%)\n', ...
        sdWant, sd, 100*max(sdWant-sd,0)/max(sdWant,eps));
% real SoC (recursed from actual DCOPF charge/discharge) vs plan (QP socTr), end-of-horizon
socEndReal = sum(socRealTr(find(~all(isnan(socRealTr),2),1,'last'),:),'omitnan');
fprintf('SoC end real/plan : %.0f / %.0f MWh  (drift %.0f MWh)\n', ...
        socEndReal, sum(socTr(end,:)), socEndReal - sum(socTr(end,:)));

% load shedding: baseline (pass 1, storage OFF) vs final (pass 2)
shFin = sum(loadShed ,'omitnan');  shBas = sum(shed_base,'omitnan');
fprintf('Load shed base/final: %.0f / %.0f MWh  (delta %.0f MWh)\n', ...
        shBas, shFin, shBas - shFin);
fprintf('Shed hours base/final: %d / %d\n', ...
        nnz(shed_base > 1), nnz(loadShed > 1));
% NB: cost(tt)=rr.f INCLUDES the VOLL*shed penalty in shed hours. Net
% production cost = sum(cost) - VOLL*shFin.
fprintf('Shed penalty in cost: %.3e $  (net gen cost = %.3e $)\n', ...
        VOLL*shFin, sum(cost,'omitnan') - VOLL*shFin);

% correctness sentinels (warn if a fixed bug reappears)
if min(socTr(:)) < -1e-6, warning('SoC negative — clamp failed'); end
if max(socRealTr(:)) > max(Elf)+1e-6 || min(socRealTr(:)) < -1e-6
    warning('socReal out of [0,Elf] — SoC clamp failed'); end
if any(loadShed(:) < -1e-6), warning('negative load shed — check shed gen PMIN'); end



if any(~success), fprintf('UNSOLVED hours: %s\n', mat2str(find(~success)')); end

save('syngrid2025_timeseries_results.mat','out','-v7');







%% ===================== local functions =================================
function v = pctl(x, p)
% Nearest-rank percentile (p in [0,1]); avoids a Statistics Toolbox call.
    x = sort(x(:));  n = numel(x);
    if n == 0, v = NaN; return; end
    k = min(n, max(1, round(p*(n-1)) + 1));
    v = x(k);
end

function [T, dt] = readLong(path, dtcols, fmt)
% Read a long-format CSV, forcing the datetime column(s) to string, and
% return the table plus a parsed datetime vector.
    opts = detectImportOptions(path, 'TextType','string');
    for k = 1:numel(dtcols)
        opts = setvartype(opts, dtcols{k}, 'string');
    end
    T = readtable(path, opts);
    if numel(dtcols)==1, s = T.(dtcols{1});
    else,                s = T.(dtcols{1}) + " " + T.(dtcols{2});
    end
    dt = datetime(s, 'InputFormat', fmt);
end


%% ===================== local functions (pass 0 / SCUC) ================
function s = availSum(capMat, rated, rows)
% Per-hour available MW for a set of rows: use the outage/derate cap where
% finite, else the rated PMAX (unit simply absent from the availability table).
    if isempty(rows), s = zeros(size(capMat,1),1); return; end
    sub  = capMat(:, rows);                 % Th x |rows|, NaN where absent
    fill = repmat(rated(rows)', size(sub,1), 1);
    sub(isnan(sub)) = fill(isnan(sub));
    s = sum(sub, 2);
end

function u = meritFallback(resid, R, Pmax, ~, c1)
% No-MILP safety net: greedy merit-order commitment per hour (no min up/down).
% Commits cheapest units until rated capacity covers resid + reserve.
    [~,ord] = sort(c1,'ascend');
    Tb = numel(resid); nG = numel(Pmax);
    u = zeros(Tb,nG);
    for t = 1:Tb
        need = resid(t) + R(t); cum = 0;
        for k = ord'
            if cum >= need, break; end
            u(t,k) = 1; cum = cum + Pmax(k);
        end
    end
end

function uOut = solveUCblock(resid, Rz, zoneU, Pmax, Pmin, c1, c0, su, ...
                             minUp, minDn, mustRun, u0, onFor, offFor, PEN, PENr)
% Copper-plate thermal SCUC for ONE rolling block, solved with Gurobi.
% VECTORIZED build (no per-constraint growth): the whole A/rhs/sense is
% assembled from a handful of matrix operations, ~10-50x faster to set up.
%
% Vars per (unit i, hour t): p (MW, cont), u (on/off, BIN), v (start,[0,1]),
% w (stop,[0,1]). Plus per-hour unserved slack s, and a reserve-short slack sr
% PER ZONE PER HOUR (Ver8 zonal reserve).
% min  sum(c1*p + c0*u + su*v) + PEN*s + PENr*sum_z sr
% s.t. (A) power balance  (B) ZONAL reserve (one row per zone-hour)
%      (C) p<=Pmax*u  (D) p>=Pmin*u  (E) start/stop  (F) min-up  (G) min-down.
% (B) forces each zone's committed thermal to carry its own reserve share Rz(z),
% so a load pocket can no longer borrow headroom from a rich zone on paper.
% zoneU(i) in 1..nZ is unit i's zone; Rz(z) is that zone's reserve MW.
% mustRun(i)=true forces u=1 every hour (nuclear). RATED Pmax throughout
% (unplanned outage is applied downstream in the loop). nZ=1 reduces exactly
% to the old system-level reserve.
    N = numel(Pmax);  T = numel(resid);  nP = N*T;
    Pmax=Pmax(:); Pmin=Pmin(:); c1=c1(:); c0=c0(:); su=su(:);
    minUp=minUp(:); minDn=minDn(:); u0=u0(:); onFor=onFor(:); offFor=offFor(:);
    mustRun=logical(mustRun(:));
    zoneU = zoneU(:);  Rz = Rz(:);  nZ = numel(Rz);   % nZ zones, per-zone reserve Rz

    % variable index blocks (N x T each, column-major: i fastest then t)
    Pidx  = reshape(1:nP, N, T);
    Uidx  = nP   + Pidx;
    Vidx  = 2*nP + Pidx;
    Widx  = 3*nP + Pidx;
    Sidx  = 4*nP + (1:T);                            % unserved slack, per hour
    SRidx = reshape(4*nP + T + (1:nZ*T), nZ, T);     % reserve-short slack, per zone-hour
    nv    = 4*nP + T + nZ*T;

    % ---- objective ----
    obj = zeros(nv,1);
    obj(Pidx(:)) = repmat(c1, T, 1);
    obj(Uidx(:)) = repmat(c0, T, 1);
    obj(Vidx(:)) = repmat(su, T, 1);
    obj(Sidx)     = PEN;
    obj(SRidx(:)) = PENr;

    % ---- bounds + var types ----
    lb = zeros(nv,1);  ub = inf(nv,1);  vtype = repmat('C',1,nv);
    vtype(Uidx(:)) = 'B';
    ub(Vidx(:)) = 1;  ub(Widx(:)) = 1;              % v,w relaxed to [0,1]
    % carried min up/down obligation -- GATED by current on/off state u0.
    % (fix: only an ON unit can owe "stay on"; only an OFF unit owes "stay off")
    fon  =    u0  .* max(minUp - onFor,  0);         % force ON  first fon hours
    foff = (1-u0) .* max(minDn - offFor, 0);         % force OFF first foff hours
    tmat = repmat(1:T, N, 1);                        % N x T, entry = hour t
    lb(Uidx(tmat <= fon))  = 1;
    ub(Uidx(tmat <= foff)) = 0;
    if any(mustRun), lb(Uidx(mustRun,:)) = 1; end    % nuclear: on every hour

    % ---- constraint row offsets (B is now nZ*T rows, not T) ----
    nB = nZ*T;                                       % zonal reserve rows
    oA=0; oB=T; oC=T+nB; oD=T+nB+nP; oE=T+nB+2*nP; oF=T+nB+3*nP; oG=T+nB+4*nP;
    nRows = T + nB + 5*nP;
    I={}; J={}; V={};   % triplet chunks, vertcat'd once at the end

    % (A) sum_i p + s >= resid
    rA = repmat(oA+(1:T), N, 1);
    I{end+1}=rA(:);       J{end+1}=Pidx(:);   V{end+1}=ones(nP,1);
    I{end+1}=(oA+(1:T))'; J{end+1}=Sidx(:);   V{end+1}=ones(T,1);
    % (B) ZONAL: for each zone z, hour t:
    %     sum_{i in z} Pmax_i*u_{i,t} - sum_{i in z} p_{i,t} + sr_{z,t} >= Rz(z)
    rBmat = reshape(oB+(1:nB), nZ, T);              % nZ x T reserve-row indices
    rBu   = rBmat(zoneU, :);                        % N x T: each unit -> its zone's row
    I{end+1}=rBu(:);    J{end+1}=Uidx(:);   V{end+1}=repmat(Pmax,T,1);
    I{end+1}=rBu(:);    J{end+1}=Pidx(:);   V{end+1}=-ones(nP,1);
    I{end+1}=rBmat(:);  J{end+1}=SRidx(:);  V{end+1}=ones(nB,1);
    % (C) p - Pmax*u <= 0
    rC = (oC+(1:nP))';
    I{end+1}=rC; J{end+1}=Pidx(:); V{end+1}=ones(nP,1);
    I{end+1}=rC; J{end+1}=Uidx(:); V{end+1}=-repmat(Pmax,T,1);
    % (D) p - Pmin*u >= 0
    rD = (oD+(1:nP))';
    I{end+1}=rD; J{end+1}=Pidx(:); V{end+1}=ones(nP,1);
    I{end+1}=rD; J{end+1}=Uidx(:); V{end+1}=-repmat(Pmin,T,1);
    % (E) u_t - u_{t-1} - v + w = 0   (t=1 uses carried u0 on rhs)
    rE = reshape(oE+(1:nP), N, T);
    I{end+1}=rE(:);            J{end+1}=Uidx(:);        V{end+1}=ones(nP,1);
    I{end+1}=rE(:);            J{end+1}=Vidx(:);        V{end+1}=-ones(nP,1);
    I{end+1}=rE(:);            J{end+1}=Widx(:);        V{end+1}=ones(nP,1);
    rEp = rE(:,2:T);           Up = Uidx(:,1:T-1);
    I{end+1}=rEp(:);           J{end+1}=Up(:);          V{end+1}=-ones(numel(rEp),1);
    % (F) min-up: sum_{L=0}^{minUp-1} v(i,t-L) - u(i,t) <= 0
    rF = reshape(oF+(1:nP), N, T);
    I{end+1}=rF(:); J{end+1}=Uidx(:); V{end+1}=-ones(nP,1);
    for L = 0:(max(minUp)-1)
        ii = find(minUp >= L+1);
        if isempty(ii) || (T-L) < 1, continue; end
        rb = rF(ii, L+1:T);  cb = Vidx(ii, 1:T-L);
        I{end+1}=rb(:); J{end+1}=cb(:); V{end+1}=ones(numel(rb),1);
    end
    % (G) min-down: sum_{L=0}^{minDn-1} w(i,t-L) + u(i,t) <= 1
    rG = reshape(oG+(1:nP), N, T);
    I{end+1}=rG(:); J{end+1}=Uidx(:); V{end+1}=ones(nP,1);
    for L = 0:(max(minDn)-1)
        ii = find(minDn >= L+1);
        if isempty(ii) || (T-L) < 1, continue; end
        rb = rG(ii, L+1:T);  cb = Widx(ii, 1:T-L);
        I{end+1}=rb(:); J{end+1}=cb(:); V{end+1}=ones(numel(rb),1);
    end

    A = sparse(cell2mat(I(:)), cell2mat(J(:)), cell2mat(V(:)), nRows, nv);

    % ---- rhs / sense in the SAME block order (A,B,C,D,E,F,G) ----
    rhsE = zeros(N,T);  rhsE(:,1) = u0;
    % B rhs = Rz repeated over hours, column-major (zone fastest) to match rBmat(:)
    rhs  = [ resid(:); repmat(Rz,T,1); zeros(nP,1); zeros(nP,1); rhsE(:); zeros(nP,1); ones(nP,1) ];
    sense = [ repmat('>',1,T), repmat('>',1,nB), repmat('<',1,nP), ...
              repmat('>',1,nP), repmat('=',1,nP), repmat('<',1,nP), repmat('<',1,nP) ];

    model.A = A;  model.rhs = rhs;  model.sense = sense;
    model.obj = obj;  model.lb = lb;  model.ub = ub;
    model.vtype = vtype;  model.modelsense = 'min';
    params.OutputFlag = 0;  params.MIPGap = 0.01;  params.TimeLimit = 120;
    res = gurobi(model, params);
    if ~isfield(res,'x'), error('gurobi:noSolution %s', res.status); end

    uOut = round(reshape(res.x(Uidx(:)), N, T))';    % T x N
end


function [pDis,pChg,socTr,storNet] = storageSchedulePeakShave( ...
        NL, Prated, Erated, eta_c, eta_d, dt, soc0)
% parameter-free storage schedule (Ver7): flatten system net load NL. Aggregate into one equivalent battery and solve a QP:
%   min sum_t ( NL_t + c_t - d_t - mean(NL) )^2
%   s.t. SoC dynamics, 0<=c,d<=SumPrated, 0<=e<=SumErated, e_T=e_0 (return to start at cycle end)
% then split to units by power share (causal SoC clamping). Magnitude set physically by SumP/SumE, no tuning.
    Th=numel(NL); nStor=numel(Prated);
    Pag=sum(Prated); Eag=sum(Erated); e0=sum(soc0);
    a = NL(:) - mean(NL,'omitnan');
    T=Th; ic=1:T; id=T+1:2*T; ie=2*T+1:3*T; n=3*T;
    % objective 1/2 x'Hx + f'x, per-term = (c-d+a)^2
    H = sparse([ic ic id id],[ic id ic id], ...
               [2*ones(1,T) -2*ones(1,T) -2*ones(1,T) 2*ones(1,T)], n, n);
    f = zeros(n,1); f(ic)=2*a; f(id)=-2*a;
    % SoC dynamics equality: e_t - e_{t-1} - eta_c*dt*c_t + dt/eta_d*d_t = 0, e_0=e0
    r=[];co=[];v=[]; b=zeros(T,1);
    for t=1:T
        r=[r t t t]; co=[co ie(t) ic(t) id(t)]; v=[v 1 -eta_c*dt dt/eta_d];
        if t>1, r=[r t]; co=[co ie(t-1)]; v=[v -1]; else, b(t)=e0; end
    end
    A=sparse(r,co,v,T,n);
    xmin=zeros(n,1); xmax=[Pag*ones(T,1);Pag*ones(T,1);Eag*ones(T,1)];
    xmin(ie(T))=e0; xmax(ie(T))=e0;                 % return to e0 at cycle end
    x=[];
    try
        x = qps_master(H,f,A,b,b,xmin,xmax,[],struct('verbose',0));   % uses the configured Gurobi
    catch ME
        warning('storage QP failed (%s) -> storage OFF', ME.message);
    end
    if isempty(x)
        pDis=zeros(Th,nStor); pChg=zeros(Th,nStor);
        socTr=repmat(soc0(:)',Th,1); storNet=zeros(Th,1); return;
    end
    cAg=max(x(ic),0); dAg=max(x(id),0);
    shP=Prated./Pag;                                % split to units by power share
    pChg=zeros(Th,nStor); pDis=zeros(Th,nStor); socTr=zeros(Th,nStor); soc=soc0(:);
    for t=1:Th
        q=min(cAg(t).*shP, max(Erated-soc,0)./(eta_c.*dt)); q=max(q,0);
        soc=min(soc+q.*eta_c.*dt, Erated);
        p=min(dAg(t).*shP, soc.*eta_d./dt);                 p=max(p,0);
        soc=max(soc-p.*dt./eta_d, 0);
        pChg(t,:)=q'; pDis(t,:)=p'; socTr(t,:)=soc';
    end
    storNet=sum(pDis-pChg,2);                        % Th x1 net output (+dis / -chg)
end
