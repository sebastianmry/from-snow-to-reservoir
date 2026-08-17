# Project Context: FROM SNOW TO RESERVOIR
Satellite monitoring of the snow–glacier–reservoir water chain in the Georgian Greater Caucasus

## 1. Study area
Hydrological monitoring of two key catchments in the Georgian Greater Caucasus, linking
snowmelt, glacier change and reservoir storage from open satellite data. The area of
interest (AOI) for each site is the **catchment above the dam** (HydroBASINS lev12, built
by `download_catchments.py`). All sites are defined once in `aoi_config.py`, the single
source of truth that every script imports (clip box, dam pour point, S1 orbit anchor,
display fields).

### AOI 1: Enguri (Western Georgia / Svaneti)
- `clip_box` (catchment bbox + 0.02° buffer): (41.8467, 42.7294, 43.1658, 43.2783)
- Catchment area: ~3,139 km² (HydroBASINS lev12, converged from lev09)
- Dam / pour point: (42.032, 42.753); `s1_anchor` 20240830 (phase 7), 54 S1 scenes (as of 2026-08-17, growing weekly)
- Heavily glaciated Svaneti headwaters; the box reaches to ~43.17 E to include the eastern
  Ushguli source rivers.

### AOI 2: Zhinvali (Eastern Georgia / Kazbegi)
- `clip_box` (catchment bbox + 0.02° buffer): (44.3133, 42.0008, 45.245, 42.6283)
- Catchment area: ~2,089 km² (HydroBASINS lev12, converged from lev10)
- Dam / pour point: (44.771, 42.133); `s1_anchor` 20240825, 56 S1 scenes (as of 2026-08-17, growing weekly)
- Aragvi basin up to ~42.61 N. The Kazbek/Gergeti glaciers drain north into the Terek and
  are correctly excluded.

---

## 2. Pipeline architecture

### Stage 1: `download_hls.py` / `download_s1.py` (data acquisition)
- Two thin entry scripts (HLS optical, S1 radar) over shared logic in `download_common.py`.
- Source: NASA OPERA DSWx products via `earthaccess`. Both download the `B01_WTR` water layer.
- **Footprint pre-filter:** for each date the union of the tile footprints is checked against
  the AOI; only dates covering at least 99% of the AOI (`FOOTPRINT_MIN_COVER`) are downloaded.
  Pure geometry, saves bandwidth.
- **Orbit filter (S1 only):** after the footprint filter, S1 keeps only the dates of the
  AOI's anchored 12-day repeat phase (`orbit_filter: True`), so it pulls one consistent
  relative orbit (~1/4 of the dates) instead of all of them.
- Store layout: `OPERA_DSWx/{hls,s1}/{enguri,zhinvali}/`, filenames tagged with the MGRS tile id.
- Hardened transfer: authenticated `requests` session with a hard read timeout, exponential
  backoff and retry on 429/5xx, resume by filename comparison.

### Stage 2: `extract_timeseries.py` (scientific analysis)
Processes HLS (optical) and S1 (radar). For each date all MGRS tiles are reprojected to
EPSG:4326, merged into one mosaic, clipped to the `clip_box` and padded to the full AOI.

- **Mosaic clip:** clipping to the AOI box (then padding) makes `valid_px_pct` AOI-relative;
  without it, tiles from different UTM zones (Enguri crosses 37/38) leave NoData corners.
- **Catchment mask:** `load_catchment` + `_catchment_mask` count all statistics only inside
  the drainage basin, so `valid_px_pct` / `cloud_cover_percent` are catchment-relative (the
  large clip box does not push every scene below threshold, and glaciers that drain elsewhere
  are excluded).
- **HLS quality filter:** `MIN_VALID_PCT = 85%` (catchment-relative coverage) and
  `MAX_CLOUD_PCT = 30%`. The cloud mask is the WTR layer's own flag (value 253); no separate
  cloud band is needed.
- **S1 coverage gate (reservoir-decoupled):** a date enters the series if **either** the
  whole catchment is fully imaged (`valid_px_pct >= S1_MIN_VALID_PCT = 90%`) **or** the
  reservoir itself is fully observed (`reservoir_valid_pct >= RESERVOIR_MIN_COVER = 95%`). The
  Enguri anchor was reselected to phase 7 (2026-07-03 re-probe), which fully images the
  catchment on all 54 cycles, so its basin-wide `water_km2` is now gap-free; the previous
  phase-6 anchor missed the eastern Svaneti headwaters on most cycles (catchment coverage
  ~59%) and left `water_km2` NaN on those dates. The gate still runs as defense-in-depth: on
  any rare partial-catchment date it keeps the fully observed reservoir (`reservoir_area_km2`)
  while setting the non-comparable `water_km2` to NaN. This is global (`RESERVOIR_MIN_COVER`),
  so it applies consistently to both AOIs.
- **Orbit dedup (`dedup_single_orbit`):** SAR water classification depends on the acquisition
  geometry (layover and shadow vary with the orbit), so the series is reduced to one
  consistent relative orbit. The S1 section keeps only the dates of the anchored 12-day phase
  (`orbit_phase`), then the dedup drops genuinely partial scenes (except those whose reservoir
  is fully observed) and keeps one track. The result is a clean ~12-day series free of the
  ascending/descending sawtooth.
- **Class logic (raster & vector):**
  * AOI-wide open water: `B01_WTR` pixel values 1–5.
  * Seasonal snow: value 252 *outside* the RGI glacier polygons.
  * Snow-covered glacier: value 252 *inside* the RGI glacier polygons.
  * Bare glacier ice: total RGI glacier area minus the snow-covered glacier area.
- **Snow normalisation:** `seasonal_snow_frac` (snow share of the observed, cloud-free
  non-glacier basin) and `seasonal_snow_km2_est` (that share scaled to the full non-glacier
  basin) make partial-coverage days comparable. `seasonal_snow_km2_est` is the main snow column.
- **Reservoir guard:** if less than 95% of the reservoir footprint is validly observed on a
  date, `reservoir_area_km2` and `water_km2` are set to NaN, so a NoData gap over the lake
  cannot look like a false drawdown.
- **Per-date cache:** `static_data/cache/{site}_{s1,hls}.json` stores every date result
  (including skips, with their stats), so re-runs take seconds. `--refresh` ignores the cache;
  `--recompute` re-reads only the result-relevant dates after a logic/threshold change;
  `--skip-s1` / `--skip-hls` run one sensor.
- **Outputs (local, not in the tile store):**
  * HLS: `{site}_timeseries.csv/.parquet` (water, snow, glacier, cloud, coverage).
  * S1: `{site}_s1_timeseries.csv/.parquet` (`water_km2`, `reservoir_area_km2`,
    `reservoir_valid_pct`, `valid_px_pct`).

### Division of labour: water from S1, snow from HLS
- **Water / storage → S1 only.** Optical HLS heavily over-detects water (terrain shadow and
  ice misclassified as water) even on clear days, while S1 radar is cloud-independent and
  robust. The continuous ~12-day S1 series carries the water signal.
- **Snow / ice → HLS.** `seasonal_snow_km2_est` is seasonally plausible; S1 has no snow signal.
  HLS coverage is sparse because the Caucasus is very cloudy (the real limiter is cloud, not
  swath), so the optical series has genuine multi-month gaps in cloudy seasons.

### Reservoir footprint from S1 (`derive_reservoir.py`)
HydroLAKES sits correctly at each dam but strongly underestimates the lakes (it captures only
the lower basin, not the long valley arms), so the footprint is derived from the project's own
S1 data: a one-pass water-occurrence map over all full-coverage scenes; pixels wet in at least
25% of acquisitions (occurrence-based, Pekel et al. 2016) form the reservoir, reduced to the
component connected to the HydroLAKES seed and the dam pixel, then closed and hole-filled.
Output: `static_data/reservoirs.geojson` (+ a water-frequency GeoTIFF for inspection).

`reservoir_area_km2` then counts S1 water pixels (1–5) **inside** the reservoir polygon,
separate from the AOI-wide water (which also includes rivers). The reservoir signal is much
quieter than the AOI total.

- Footprint areas: Enguri ~9.3 km², Zhinvali ~11.2 km². Zhinvali matches the real ~11.5 km²
  closely. Enguri's ~9.3 km² is the area regularly flooded during the observation window
  (Aug 2024 – present); the literature ~13 km² is the full-pool area, which was not reached in
  this period (Enguri stayed drawn down), so the difference is a real drawdown, not an error.
- Reservoir ranges: Enguri ~6.5–7.9 km² (flat: Jvari is a deep gorge reservoir, large level
  change for small area change, so area is a weak storage proxy here); Zhinvali ~8.4–10.7 km²
  with a clear seasonal cycle (drawdown over winter/spring, refill from May).
- Storage is monitored through area, not an absolute level: freely available DEMs
  (Copernicus GLO-30) captured the reservoirs as a flat water surface (no bathymetry), and
  satellite altimetry does not cover these small mountain reservoirs. This is a documented data
  limit, not a method error.

### Stage 3: `render_overlays.py` (pre-rendered scene overlays)
Runs once after `extract_timeseries.py`. For each sensor it reads the date list from the
finished parquet (scenes match the charts exactly), loads the tiles, mosaics them, downsamples
to at most 900 px, colours them and writes `static_data/overlays/{site}/{sensor}/{date}.png`
(+ a bounds sidecar). S1 colours: water blue. HLS colours: seasonal snow cyan, snow-on-glacier
mid violet, bare glacier ice dark violet; water is not rendered on the HLS overlay, since the
optical water class over-detects (terrain shadow, glacial-river sediment) and the S1 view
already covers water. Cloud (253) / NoData / outside-catchment stay transparent throughout.
Resume-safe; `--refresh` re-renders. Pre-rendering keeps the dashboard fast (no raster work at
runtime).

### Stage 4: `app.py` (Streamlit dashboard)
- Streamlit + Folium + Plotly, three pages via `st.navigation` (`page_overview`,
  `page_time_series`, `page_scene_browser`). The sidebar is global and shared across all three:
  the AOI picker, the time range slider, and an info popover pinned to the bottom.
- **Overview page:** title and catchment-area fact line beside the static catchment map
  (catchment outline, RGI glacier polygons, HydroRIVERS rivers, dam pin, derived reservoir
  footprint with an on-map label) in one column, with three KPI cards (reservoir area, total
  snow, total ice) in a column beside it.
- **Time series page:** two tabs with Plotly charts. Reservoir area is the main water line with
  AOI-wide `water_km2` as a faint reference; snow/ice is a stacked area using
  `seasonal_snow_km2_est`; cloud gaps show as gaps, not interpolated.
- **Scene Browser page:** a sensor radio (Water S1 / Snow & ice HLS) and a date slider load the
  matching pre-rendered PNG overlay on a per-scene Folium map, with a colour-swatch legend and a
  raw-data expander (both time series tables) underneath.
- The app reads only local files at runtime (parquets, GeoJSONs, RGI shapefile, overlay PNGs),
  so no secrets are needed in the cloud.

### Supporting scripts
- `download_catchments.py` — HydroBASINS lev12, pour point at the dam, upstream BFS
  (`HYBAS_ID`/`NEXT_DOWN`), dissolved into `catchments.geojson`.
- `download_glaciers.py` — RGI v7 Region 12 (Caucasus & Middle East) from NSIDC, clipped to
  each AOI.
- `download_rivers.py` — HydroRIVERS v10 Europe, clipped to both AOIs, large rivers only
  (`ORD_FLOW <= 8`) → `static_data/georgia_rivers.geojson`.
- `download_reservoirs.py` — HydroLAKES seed for `derive_reservoir.py`.
- `probe_coverage.py` — read-only S1 orbit selector. Stage A screens footprint coverage into
  candidate 12-day phases; stage B (`--sample N`, `--only-phase P`) downloads sample dates and
  measures the real catchment-relative `valid_px_pct` with the same machinery as the run, so
  the anchor is chosen on true pixel coverage rather than footprint. `--compare-orbit` checks a
  neighbouring orbit's `reservoir_area_km2` against the series as a precision test.
- `storage.py` — one local filesystem tile store (`LocalStore` under `PIPELINE_LOCAL_DIR`,
  default `./opera_local`), so the pipeline runs identically locally and in CI.

---

## 3. Deployment & automation
- **Live:** Streamlit Community Cloud — https://from-snow-to-reservoir.streamlit.app/
  (public repo, `main`, `app.py`).
- **Self-contained repo:** `.gitignore` is ignore-all-then-un-ignore. The small runtime
  artefacts (parquets, GeoJSONs, RGI shapefile, overlay PNGs) are committed; the bulk raw
  geodata stays out and is re-fetched on demand. `requirements.txt` pins the lean app deps;
  `requirements-pipeline.txt` pins the full pipeline deps.
- **Auto-update (`.github/workflows/update-data.yml`):** weekly (Mon 03:17 UTC) plus manual
  dispatch — download → extract → render, then changed parquets and overlay PNGs are committed
  back and Streamlit re-deploys. Tiles live in `runner.temp` (never committed). A guard step
  refuses to commit a regressed (shorter) time series. Only secret: the Earthdata credentials.

---

## 4. Technical stack
- `earthaccess` — NASA granule search and download
- `rioxarray` / `rasterio` — in-memory raster processing and clipping
- `geopandas` — vector data (RGI, basins, rivers, reservoirs)
- `pandas` / `pyarrow` — parquet output for Streamlit
- `tqdm` — progress display
- Tile store: local filesystem under `PIPELINE_LOCAL_DIR` (default `./opera_local`)
- Conda environment: `georgia-sar`

The dashboard is a three-page `st.navigation` app per AOI: Overview (map, headline KPIs),
Time series (water and snow/ice charts) and Scene Browser (date-slider raster viewer).
