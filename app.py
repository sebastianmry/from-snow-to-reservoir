"""
Stage 3 of the pipeline: interactive visualization of HLS timeseries data.

Run with:
    streamlit run app.py
"""

import base64
import json
import re
from pathlib import Path

import folium
import geopandas as gpd
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from shapely.geometry import Point, shape
from streamlit_folium import st_folium

# Main inflow river per AOI (HydroRIVERS has no names; curated). Shown as a
# tooltip on the main stem; the Aragvi feeds the Zhinvali reservoir, so river and
# reservoir names differ.
MAIN_RIVER = {"enguri": "Enguri", "zhinvali": "Aragvi"}
# Reservoir name per AOI - the persistent on-map label sits on the lake itself.
RESERVOIR_NAME = {"enguri": "Enguri", "zhinvali": "Zhinvali"}

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

from aoi_config import AOIS as _AOI_CONFIG, STATIC_DIR

# Dashboard view, keyed by display label, built from the central AOI config.
AOIS = {
    aoi_cfg["display_label"]: {
        "key": aoi_cfg["name"],
        "clip_box": aoi_cfg["clip_box"],
        "center": aoi_cfg["center"],
        "dam": aoi_cfg["dam"],
        "dam_label": aoi_cfg["dam_label"],
        "zoom": aoi_cfg["zoom"],
    }
    for aoi_cfg in _AOI_CONFIG.values()
}

SNOW_COLORS = {
    "seasonal_snow_km2":     "#a8d8ea",
    "seasonal_snow_km2_est": "#a8d8ea",
    "snow_on_glacier_km2":   "#4a90d9",
    "bare_ice_km2":          "#1a3a5c",
}

SNOW_LABELS = {
    "seasonal_snow_km2":     "Seasonal snow",
    "seasonal_snow_km2_est": "Seasonal snow (coverage corrected)",
    "snow_on_glacier_km2":   "Snow on glacier",
    "bare_ice_km2":          "Bare glacier ice",
}


# ─────────────────────────────────────────────
# DATA LOADING
# ─────────────────────────────────────────────

def make_mock_data(aoi_key: str) -> pd.DataFrame:
    """Generate realistic mock timeseries for UI testing before real data arrives."""
    rng = np.random.default_rng(seed=42 if aoi_key == "enguri" else 7)
    dates = pd.date_range("2024-08-01", periods=200, freq="3D")
    time_axis = np.linspace(0, 4 * np.pi, len(dates))

    seas_snow  = np.clip(60 + 50 * np.sin(time_axis + np.pi) + rng.normal(0, 5, len(dates)), 0, None)
    glac_snow  = np.clip(30 + 20 * np.sin(time_axis + np.pi) + rng.normal(0, 3, len(dates)), 0, None)
    bare_ice   = np.clip(20 - 15 * np.sin(time_axis + np.pi) + rng.normal(0, 2, len(dates)), 0, None)
    cloud      = np.clip(rng.uniform(0, 45, len(dates)), 0, 30)

    # Sprinkle some NaN cloud gaps
    gap_indices = rng.choice(len(dates), size=20, replace=False)
    for series in [seas_snow, glac_snow, bare_ice]:
        series[gap_indices] = np.nan

    return pd.DataFrame({
        "date":                pd.to_datetime(dates),
        "seasonal_snow_km2":   np.round(seas_snow, 1),
        "snow_on_glacier_km2": np.round(glac_snow, 1),
        "bare_ice_km2":        np.round(bare_ice, 1),
        "cloud_cover_percent": np.round(cloud, 1),
        "valid_px_pct":        np.round(rng.uniform(80, 100, len(dates)), 1),
    })


@st.cache_data(show_spinner=False)
def load_timeseries(aoi_key: str) -> tuple[pd.DataFrame, bool]:
    """Load HLS parquet timeseries (snow / glacier). Returns (timeseries_df, is_mock)."""
    path = Path(f"{aoi_key}_timeseries.parquet")
    if path.exists():
        timeseries_df = pd.read_parquet(path)
        timeseries_df["date"] = pd.to_datetime(timeseries_df["date"])
        return timeseries_df.sort_values("date").reset_index(drop=True), False
    return make_mock_data(aoi_key), True


@st.cache_data(show_spinner=False)
def load_s1_timeseries(aoi_key: str) -> tuple[pd.DataFrame, bool]:
    """Load DSWx-S1 parquet timeseries (water surface). Returns (timeseries_df, is_mock).

    Water comes from S1, not HLS: optical HLS massively over-detects water
    (terrain shadow / ice misclassified), so the reservoir water signal uses
    the cloud-independent radar product (column water_km2).
    """
    path = Path(f"{aoi_key}_s1_timeseries.parquet")
    if path.exists():
        timeseries_df = pd.read_parquet(path)
        timeseries_df["date"] = pd.to_datetime(timeseries_df["date"])
        return timeseries_df.sort_values("date").reset_index(drop=True), False
    # Mock fallback: a smooth ~12-day water series
    rng = np.random.default_rng(seed=99 if aoi_key == "enguri" else 13)
    dates = pd.date_range("2024-08-01", periods=55, freq="12D")
    time_axis = np.linspace(0, 4 * np.pi, len(dates))
    base = 24 if aoi_key == "enguri" else 40
    water = base + 6 * np.sin(time_axis * 0.5 + 1) + rng.normal(0, 0.6, len(dates))
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "water_km2": np.round(water, 2),
        "valid_px_pct": np.round(rng.uniform(98, 100, len(dates)), 1),
    }), True


# ─────────────────────────────────────────────
# STATIC GEODATA
# ─────────────────────────────────────────────

def ensure_rivers() -> Path | None:
    """Return path to rivers GeoJSON, generating a simplified version if missing."""
    STATIC_DIR.mkdir(exist_ok=True)
    path = STATIC_DIR / "georgia_rivers.geojson"
    if path.exists():
        return path

    # Simplified main river lines for both AOIs
    # Enguri: flows west from Mestia toward the dam and Black Sea
    # Aragvi: flows south from Kazbegi toward Zhinvali reservoir
    rivers = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Enguri", "aoi": "enguri"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [42.80, 43.05], [42.60, 42.98], [42.40, 42.90],
                        [42.20, 42.82], [42.03, 42.75], [41.90, 42.68],
                        [41.75, 42.60],
                    ],
                },
            },
            {
                "type": "Feature",
                "properties": {"name": "Aragvi", "aoi": "zhinvali"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [44.65, 42.75], [44.68, 42.65], [44.70, 42.55],
                        [44.72, 42.45], [44.74, 42.35], [44.77, 42.25],
                        [44.77, 42.13],
                    ],
                },
            },
            {
                "type": "Feature",
                "properties": {"name": "Iori", "aoi": "zhinvali"},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [45.10, 42.70], [45.00, 42.60], [44.90, 42.50],
                        [44.85, 42.40], [44.80, 42.30], [44.77, 42.13],
                    ],
                },
            },
        ],
    }
    with path.open("w") as rivers_file:
        json.dump(rivers, rivers_file)
    return path


@st.cache_data(show_spinner=False)
def load_rivers(aoi_key: str) -> list[dict] | None:
    path = ensure_rivers()
    if path is None:
        return None
    with path.open() as rivers_file:
        rivers_geojson = json.load(rivers_file)
    return [feature for feature in rivers_geojson["features"]
            if feature["properties"]["aoi"] == aoi_key]


@st.cache_data(show_spinner=False)
def load_glaciers(clip_box: tuple) -> gpd.GeoDataFrame | None:
    candidates = list(STATIC_DIR.rglob("RGI2000-v7.0-G-12_caucasus*middle_east.shp"))
    if not candidates:
        return None
    try:
        min_lon, min_lat, max_lon, max_lat = clip_box
        glaciers_gdf = gpd.read_file(candidates[0], bbox=(min_lon, min_lat, max_lon, max_lat))
        if glaciers_gdf.crs and glaciers_gdf.crs.to_epsg() != 4326:
            glaciers_gdf = glaciers_gdf.to_crs("EPSG:4326")
        if glaciers_gdf.empty:
            return None
        # Clean the name column: keep only real names; blank out empty values and
        # catalogue IDs (e.g. "198b", "193a") - a real name has a run of >=3
        # letters, an ID does not. Unicode-aware so Cyrillic names are kept.
        if "glac_name" in glaciers_gdf.columns:
            def _clean_name(value):
                name = "" if value is None else str(value).strip()
                if name.lower() in ("", "nan", "none"):
                    return ""
                return name if re.search(r"[^\W\d_]{3,}", name) else ""
            glaciers_gdf["glac_name"] = glaciers_gdf["glac_name"].map(_clean_name)
        return glaciers_gdf
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def load_catchment(aoi_key: str) -> gpd.GeoDataFrame | None:
    """HydroBASINS drainage-basin polygon for the AOI (download_catchments.py).
    The analysis is masked to this basin, so it doubles as the true AOI contour."""
    path = STATIC_DIR / "catchments.geojson"
    if not path.exists():
        return None
    try:
        catchment_gdf = gpd.read_file(path)
        catchment_gdf = catchment_gdf[catchment_gdf["aoi"] == aoi_key]
        if catchment_gdf.empty:
            return None
        if catchment_gdf.crs and catchment_gdf.crs.to_epsg() != 4326:
            catchment_gdf = catchment_gdf.to_crs("EPSG:4326")
        return catchment_gdf
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def load_reservoir(aoi_key: str) -> gpd.GeoDataFrame | None:
    """S1-derived reservoir footprint polygon (derive_reservoir.py)."""
    path = STATIC_DIR / "reservoirs.geojson"
    if not path.exists():
        return None
    try:
        reservoir_gdf = gpd.read_file(path)
        reservoir_gdf = reservoir_gdf[reservoir_gdf["aoi"] == aoi_key]
        if reservoir_gdf.empty:
            return None
        if reservoir_gdf.crs and reservoir_gdf.crs.to_epsg() != 4326:
            reservoir_gdf = reservoir_gdf.to_crs("EPSG:4326")
        return reservoir_gdf
    except Exception:
        return None


# ─────────────────────────────────────────────
# RASTER OVERLAYS (pre-rendered PNGs, see render_overlays.py)
# ─────────────────────────────────────────────

OVERLAY_DIR = STATIC_DIR / "overlays"
# Display label -> sensor subfolder
OVERLAY_SENSORS = {"Water (S1)": "s1", "Snow & ice (HLS)": "hls"}


@st.cache_data(show_spinner=False)
def load_overlay_index(aoi_key: str, sensor: str) -> dict | None:
    """Available pre-rendered scenes for one AOI+sensor: the date list and the
    shared geographic bounds. Returns None if render_overlays.py has not run."""
    overlay_dir = OVERLAY_DIR / aoi_key / sensor
    bounds_path = overlay_dir / "bounds.json"
    if not overlay_dir.exists() or not bounds_path.exists():
        return None
    try:
        bounds = json.loads(bounds_path.read_text())["bounds"]
    except Exception:
        return None
    dates = sorted(png_path.stem for png_path in overlay_dir.glob("*.png"))
    if not dates:
        return None
    return {"bounds": bounds, "dates": dates}


@st.cache_data(show_spinner=False)
def load_overlay_uri(aoi_key: str, sensor: str, date_str: str) -> str | None:
    """Read one overlay PNG as a base64 data URI (so it embeds straight into the
    folium map without needing a served file)."""
    png_path = OVERLAY_DIR / aoi_key / sensor / f"{date_str}.png"
    if not png_path.exists():
        return None
    return "data:image/png;base64," + base64.b64encode(png_path.read_bytes()).decode()


# ─────────────────────────────────────────────
# MAP
# ─────────────────────────────────────────────

def _river_weight(ord_flow) -> float:
    """Line width from flow order (lower order = larger river = thicker).
    Gradation keeps big rivers prominent and small brooks (order 7-8) thin."""
    try:
        order = int(ord_flow)
    except (TypeError, ValueError):
        order = 6
    return min(4.0, max(0.6, (9 - order) * 0.7))


def _chaikin(coords: list, iters: int = 2) -> list:
    """Chaikin corner-cutting: smooths a polyline for display only."""
    for _ in range(iters):
        if len(coords) < 3:
            break
        smoothed = [coords[0]]
        for i in range(len(coords) - 1):
            point, next_point = coords[i], coords[i + 1]
            smoothed.append([0.75 * point[0] + 0.25 * next_point[0],
                             0.75 * point[1] + 0.25 * next_point[1]])
            smoothed.append([0.25 * point[0] + 0.75 * next_point[0],
                             0.25 * point[1] + 0.75 * next_point[1]])
        smoothed.append(coords[-1])
        coords = smoothed
    return coords


def smooth_river_features(features: list[dict]) -> list[dict]:
    """Return copies of river features with Chaikin-smoothed geometry. This only
    changes how the lines are drawn; the underlying HydroRIVERS topology and flow
    order (used for the catchment filter and line width) are untouched."""
    smoothed_features = []
    for feature in features:
        geom = feature["geometry"]
        geom_type = geom["type"]
        if geom_type == "LineString":
            new_geom = {"type": "LineString", "coordinates": _chaikin(geom["coordinates"])}
        elif geom_type == "MultiLineString":
            new_geom = {"type": "MultiLineString",
                        "coordinates": [_chaikin(line) for line in geom["coordinates"]]}
        else:
            new_geom = geom
        smoothed_features.append({"type": "Feature", "properties": feature["properties"],
                                  "geometry": new_geom})
    return smoothed_features


def river_label_point(features: list[dict]) -> tuple[float, float] | None:
    """A point on the main stem (longest line of the lowest flow order) to
    anchor the river-name label, so the name always sits on the actual river."""
    if not features:
        return None
    orders = [feature["properties"].get("ORD_FLOW", 9) for feature in features]
    min_order = min(orders)
    longest_line, longest_length = None, -1.0
    for feature in features:
        if feature["properties"].get("ORD_FLOW", 9) != min_order:
            continue
        line_geom = shape(feature["geometry"])
        if line_geom.length > longest_length:
            longest_line, longest_length = line_geom, line_geom.length
    if longest_line is None:
        return None
    label_point = longest_line.interpolate(0.5, normalized=True)
    return (label_point.y, label_point.x)


def _shrink_attribution(fmap: folium.Map):
    """Leaflet's basemap attribution ("Leaflet | (c) OpenStreetMap ... (c) CARTO")
    inherits the page's 16px body font by default, much larger than the rest of the
    app's UI text. Scale it down to match the swatch-legend captions."""
    fmap.get_root().header.add_child(folium.Element(
        '<style>.leaflet-control-attribution { font-size: 11px; line-height: 1.4; }</style>'
    ))


def _add_basemap_layers(fmap: folium.Map, default: str = "plain"):
    """Three base layers, all always added, switchable via the
    folium.LayerControl() added once all overlays are on the map; `default`
    ("terrain" / "plain" / "satellite") picks which one starts checked.

    "plain": CartoDB Dark Matter - flat dark grey, quiet like Positron but
    without reading as stark white; the default for the scene-browser raster
    overlays.
    "terrain": Stadia's Stamen Terrain - hillshaded relief, showing the
    catchment's topography. Reads an optional stadia_api_key from st.secrets
    (see .streamlit/secrets.toml, gitignored, and the same key under
    Streamlit Cloud's app settings for the deployed site); without a key it
    falls back to Stadia's keyless tier, which only resolves on localhost.
    "satellite": Esri World Imagery, a photographic alternative; the default
    for the AOI overview map.

    The catchment/glacier/river/reservoir layers drawn on top get a white
    casing (see build_map()/build_overlay_map()) so they stay readable
    against Terrain/Satellite without needing to dim the basemap itself."""
    # Added Dark Matter, Satellite, Terrain - the LayerControl lists base
    # layers in add order, and that's the order they should read in the picker.
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        attr='© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors © CARTO',
        name="Dark Matter",
        show=(default == "plain"),
    ).add_to(fmap)
    folium.TileLayer(
        tiles=("https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}"),
        attr=("Tiles © Esri — Source: Esri, Maxar, Earthstar "
              "Geographics, and the GIS User Community"),
        name="Satellite",
        show=(default == "satellite"),
    ).add_to(fmap)
    # Bare xyzservices lookup can't take a key param, so build the URL by hand
    # from the real template it resolves ({variant}/{ext} filled in) rather
    # than a hand-typed template, which leaves a dangling literal {ext}.
    stadia_key = st.secrets.get("stadia_api_key")
    stadia_url = "https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}{r}.png"
    if stadia_key:
        stadia_url += f"?api_key={stadia_key}"
    folium.TileLayer(
        tiles=stadia_url,
        attr=('© <a href="https://www.stadiamaps.com/" target="_blank">Stadia Maps</a> '
              '© <a href="https://www.stamen.com/" target="_blank">Stamen Design</a> '
              '© <a href="https://openmaptiles.org/" target="_blank">OpenMapTiles</a> '
              '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'),
        name="Terrain",
        show=(default == "terrain"),
    ).add_to(fmap)


def build_map(aoi: dict, rivers: list[dict] | None, glaciers: gpd.GeoDataFrame | None,
              reservoir: gpd.GeoDataFrame | None = None,
              catchment: gpd.GeoDataFrame | None = None) -> folium.Map:
    min_lon, min_lat, max_lon, max_lat = aoi["clip_box"]
    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2

    # zoomSnap=0.25 lets fit_bounds land on a fractional zoom level. Without it,
    # Leaflet only snaps to integer zooms, so an elongated AOI like Enguri is
    # stuck between "one level too far out" and "one level too close". Quarter
    # steps give a fit that actually frames it.
    fmap = folium.Map(
        location=[center_lat, center_lon],
        tiles=None,
        zoomSnap=0.25,
    )
    _add_basemap_layers(fmap, default="satellite")
    # Fit to the AOI. With zoomSnap=0.25 this lands on a fractional zoom, so the
    # frame can sit between integer levels. A small outward pad (negative shrink)
    # zooms out a touch so the whole catchment contour stays inside the frame with
    # a little margin. The clip_box only clears the catchment by a 0.02 deg buffer,
    # so an inward shrink would crop the western basin boundary.
    shrink = -0.04
    pad_lat = (max_lat - min_lat) * shrink
    pad_lon = (max_lon - min_lon) * shrink
    fmap.fit_bounds([[min_lat + pad_lat, min_lon + pad_lon],
                     [max_lat - pad_lat, max_lon - pad_lon]])

    # Load Montserrat for the on-map reservoir label - a clean geographic-label
    # typeface regardless of which base layer (topo or satellite) is active.
    fmap.get_root().header.add_child(folium.Element(
        '<link href="https://fonts.googleapis.com/css2?'
        'family=Montserrat:wght@500;600&display=swap" rel="stylesheet">'
    ))
    _shrink_attribution(fmap)

    # Reservoir centre for placing the reservoir-name label on the lake itself.
    res_label_anchor = None
    if reservoir is not None and not reservoir.empty:
        reservoir_centroid = reservoir.geometry.union_all().centroid
        res_label_anchor = (reservoir_centroid.y, reservoir_centroid.x)

    # AOI = the drainage basin above the dam (HydroBASINS catchment). Draw its
    # contour; the dashed bbox is only a fallback when the catchment is missing.
    # Wrapped in a named FeatureGroup (like glaciers/rivers below) rather than
    # naming the GeoJson directly - folium's LayerControl reads FeatureGroup
    # names far more reliably than a bare named GeoJson.
    if catchment is not None and not catchment.empty:
        catchment_group = folium.FeatureGroup(name="Catchment (HydroBASINS)")
        # Thin white halo under the grey-blue line - grey-blue holds its own
        # mid-luminance contrast against both the near-black Dark Matter
        # basemap and near-white snow/glacier terrain in Satellite, while the
        # halo covers the few backgrounds close to grey-blue itself.
        folium.GeoJson(
            catchment.__geo_interface__,
            style_function=lambda _: {
                "color": "#ffffff", "weight": 4.0, "opacity": 0.9, "fillOpacity": 0,
            },
        ).add_to(catchment_group)
        folium.GeoJson(
            catchment.__geo_interface__,
            style_function=lambda _: {
                "color": "#5d6d7e",
                "weight": 3.0,
                "fillColor": "#5d6d7e",
                "fillOpacity": 0.04,
            },
        ).add_to(catchment_group)
        catchment_group.add_to(fmap)
    else:
        folium.Rectangle(
            bounds=[[min_lat, min_lon], [max_lat, max_lon]],
            color="#5d6d7e",
            weight=1.5,
            dash_array="6,6",
            fill=False,
            tooltip="Area of interest (AOI)",
        ).add_to(fmap)

    # Glacier polygons - cool light violet so they stay distinct from the blue
    # water layers and the white basemap. Split into named/unnamed: only named
    # glaciers get a tooltip, so hovering an unnamed one shows nothing.
    if glaciers is not None:
        glacier_style = lambda _: {
            "fillColor": "#cfc6e8",
            "color": "#7e6fb8",
            "weight": 1.3,
            "fillOpacity": 0.9,
        }
        # Clip to the catchment: glaciers outside the basin don't drain into this
        # reservoir and aren't in the statistics, so showing them only confuses.
        if catchment is not None and not catchment.empty:
            glaciers = gpd.clip(glaciers, catchment)
        has_name = "glac_name" in glaciers.columns
        named = glaciers[glaciers["glac_name"] != ""] if has_name else glaciers
        unnamed = glaciers[glaciers["glac_name"] == ""] if has_name else glaciers.iloc[0:0]

        # Single legend entry, but keep named/unnamed as separate GeoJson so only
        # named glaciers carry a tooltip. Both go into one FeatureGroup -> one toggle.
        glacier_group = folium.FeatureGroup(name="RGI v7 glaciers")
        if not unnamed.empty:
            folium.GeoJson(unnamed.__geo_interface__,
                           style_function=glacier_style).add_to(glacier_group)
        if not named.empty:
            folium.GeoJson(
                named.__geo_interface__,
                style_function=glacier_style,
                tooltip=folium.GeoJsonTooltip(fields=["glac_name"], labels=False),
            ).add_to(glacier_group)
        glacier_group.add_to(fmap)

    # River lines - GeoJson handles both LineString and MultiLineString.
    # Width scales with flow order (larger rivers thicker, small tributaries thin).
    # Like the glaciers: one legend entry, but split so only the main stem (low
    # flow order = big rivers) carries the river-name tooltip on hover; small
    # tributaries stay un-labelled.
    if rivers:
        # Clip to the catchment, like the glaciers above: the source data is
        # only bbox-filtered per AOI, so a segment can dangle past the actual
        # drainage divide - most visibly right below the dam, where a stub of
        # "river" pokes out past the reservoir with nothing connecting it.
        if catchment is not None and not catchment.empty:
            rivers_gdf = gpd.GeoDataFrame.from_features(rivers, crs="EPSG:4326")
            rivers_gdf = gpd.clip(rivers_gdf, catchment)
            # The catchment isn't pinned exactly to the dam wall - HydroBASINS'
            # pour point can sit a little past it - so a short river stub just
            # downstream can still fall inside the clip above. Cut anything
            # within 1.5 km of the dam point rather than
            # buffering the reservoir itself, since the shoreline sits well
            # over 1.5 km from the dam at several points and that buffer would
            # erase real inflow rivers elsewhere. 1.5 km covers the stub at
            # both dams (Zhinvali ~1.4 km, Enguri under 1 km); real inflow
            # segments within that radius already fall inside the reservoir
            # polygon and get clipped by the mask step below regardless.
            if aoi.get("dam"):
                dam_point = gpd.GeoSeries([Point(aoi["dam"])], crs="EPSG:4326")
                dam_buffered = dam_point.to_crs("EPSG:32638").buffer(1500).to_crs("EPSG:4326").iloc[0]
                mask_gdf = gpd.GeoDataFrame(geometry=[dam_buffered], crs="EPSG:4326")
                rivers_gdf = gpd.overlay(rivers_gdf, mask_gdf, how="difference")
            # Also drop whatever falls inside the reservoir polygon itself, so
            # no river line is left showing through/under the lake fill.
            if reservoir is not None and not reservoir.empty:
                res_mask = gpd.GeoDataFrame(geometry=[reservoir.geometry.union_all()], crs="EPSG:4326")
                rivers_gdf = gpd.overlay(rivers_gdf, res_mask, how="difference")
            rivers = json.loads(rivers_gdf.to_json())["features"]
        river_style = lambda feat: {
            "color": "#2980b9",
            "weight": _river_weight(feat["properties"].get("ORD_FLOW")),
            "opacity": 0.85 if feat["properties"].get("ORD_FLOW", 6) <= 6 else 0.55,
        }
        smoothed = smooth_river_features(rivers)
        MAIN_ORD = 5  # ORD_FLOW <= 5 = the large main-stem rivers
        main_feats = [f for f in smoothed if f["properties"].get("ORD_FLOW", 9) <= MAIN_ORD]
        trib_feats = [f for f in smoothed if f["properties"].get("ORD_FLOW", 9) > MAIN_ORD]
        river_name = MAIN_RIVER.get(aoi["key"])

        river_group = folium.FeatureGroup(name="Rivers (HydroRIVERS)")
        if trib_feats:
            folium.GeoJson(
                {"type": "FeatureCollection", "features": trib_feats},
                style_function=river_style,
            ).add_to(river_group)
        if main_feats:
            folium.GeoJson(
                {"type": "FeatureCollection", "features": main_feats},
                style_function=river_style,
                tooltip=river_name if river_name else None,
            ).add_to(river_group)
        river_group.add_to(fmap)

        # Persistent reservoir-name label on the lake itself (e.g. Zhinvali
        # Reservoir), falling back to the main-stem midpoint if no reservoir polygon.
        anchor = res_label_anchor if res_label_anchor else river_label_point(rivers)
        name = RESERVOIR_NAME.get(aoi["key"])
        if anchor and name:
            folium.Marker(
                location=list(anchor),
                icon=folium.DivIcon(
                    icon_size=(298, 35),
                    icon_anchor=(149, 18),
                    html=(
                        # Stadia-style basemap label: thin grey-blue text with
                        # a crisp white outline (not a blurred glow) and wide
                        # letter-spacing, matching the "Zhinvali Reservoir"
                        # look on Stadia's terrain tiles (Metropolis-style
                        # geometric sans - approximated here with a light
                        # system-font weight since Metropolis isn't loaded).
                        # Grey-blue ties the label to the catchment-boundary
                        # color instead of flat black.
                        '<div style="font-size:15px;font-weight:400;'
                        'letter-spacing:1.5px;color:#5d6d7e;'
                        "font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;"
                        'text-align:center;white-space:nowrap;'
                        '-webkit-text-stroke:1px #fff;paint-order:stroke fill;'
                        'text-shadow:0 0 1px #fff, 0 0 1px #fff;">'
                        f'{name} Reservoir</div>'
                    ),
                ),
            ).add_to(fmap)

    # Reservoir footprint (S1-derived) - the actual lake polygon. The headline
    # feature, so make it pop: vivid blue fill + crisp dark outline on top of the
    # paler river/water layers.
    if reservoir is not None and not reservoir.empty:
        reservoir_group = folium.FeatureGroup(name="Reservoir footprint (S1)")
        folium.GeoJson(
            reservoir.__geo_interface__,
            style_function=lambda _: {
                "fillColor": "#1f6fc0",
                "color": "#0b3d66",
                "weight": 3.5,
                "fillOpacity": 0.78,
            },
            highlight_function=lambda _: {"weight": 4.5, "fillOpacity": 0.9},
            tooltip="Reservoir footprint",
        ).add_to(reservoir_group)
        reservoir_group.add_to(fmap)

    # Must be added last - it only picks up layers already on the map. Also
    # exposes the glacier/river FeatureGroups as overlay checkboxes for free.
    folium.LayerControl(position="topright", collapsed=True).add_to(fmap)
    return fmap


def _reservoir_zoom_bounds(reservoir: gpd.GeoDataFrame, pad: float = 0.2) -> list | None:
    """Padded [[lat_min, lon_min], [lat_max, lon_max]] around the reservoir, so the
    water scene browser opens zoomed onto the dam instead of the whole catchment.
    pad is a fraction of the footprint span added on every side."""
    if reservoir is None or reservoir.empty:
        return None
    min_lon, min_lat, max_lon, max_lat = reservoir.total_bounds
    # Guard against a degenerate (near-point) footprint with a small floor.
    dlon = max((max_lon - min_lon) * pad, 0.01)
    dlat = max((max_lat - min_lat) * pad, 0.01)
    return [[min_lat - dlat, min_lon - dlon], [max_lat + dlat, max_lon + dlon]]


def build_overlay_map(aoi: dict, png_uri: str, bounds: list,
                      catchment: gpd.GeoDataFrame | None,
                      reservoir: gpd.GeoDataFrame | None,
                      zoom_to_reservoir: bool = False) -> folium.Map:
    """Light-weight map for the scene browser: basemap, catchment contour and
    the chosen pre-rendered raster.

    For the S1 water scenes (zoom_to_reservoir), the view opens framed on the
    reservoir footprint, since that is where the radar water signal lives."""
    # zoomSnap=0.25 (fractional zoom) so the fit can frame between integer levels.
    fmap = folium.Map(tiles=None, zoomSnap=0.25)
    _shrink_attribution(fmap)
    _add_basemap_layers(fmap, default="plain")
    res_bounds = _reservoir_zoom_bounds(reservoir) if zoom_to_reservoir else None
    if res_bounds:
        fmap.fit_bounds(res_bounds)
    else:
        # HLS scenes: expand the fit bounds a touch so the map opens zoomed out
        # slightly from the full overlay extent, leaving breathing room around it.
        (lat_min, lon_min), (lat_max, lon_max) = bounds
        pad_lat = (lat_max - lat_min) * 0.03
        pad_lon = (lon_max - lon_min) * 0.03
        fmap.fit_bounds([[lat_min - pad_lat, lon_min - pad_lon],
                         [lat_max + pad_lat, lon_max + pad_lon]])

    # Wrapped in named FeatureGroups (not bare GeoJson) so folium's LayerControl
    # reads a proper label instead of the auto-generated "macro_element_div_N".
    if catchment is not None and not catchment.empty:
        catchment_group = folium.FeatureGroup(name="Catchment (HydroBASINS)")
        # White casing under the grey line - see build_map()'s identical fix
        # for why: readable against busy satellite imagery, not just flat basemaps.
        folium.GeoJson(
            catchment.__geo_interface__,
            style_function=lambda _: {
                "color": "#ffffff", "weight": 4.5, "opacity": 0.9, "fill": False,
            },
        ).add_to(catchment_group)
        folium.GeoJson(
            catchment.__geo_interface__,
            style_function=lambda _: {
                "color": "#5d6d7e", "weight": 2.0, "fill": False,
            },
        ).add_to(catchment_group)
        catchment_group.add_to(fmap)

    # zoom_to_reservoir is only set for the S1 water scenes (see the caller), so
    # it doubles as the sensor flag here for the layer's LayerControl label.
    folium.raster_layers.ImageOverlay(
        image=png_uri, bounds=bounds, opacity=0.9, zindex=10,
        name="Water scene (S1)" if zoom_to_reservoir else "Snow & ice scene (HLS)",
    ).add_to(fmap)

    # Thin reservoir outline on top, for orientation against the water raster.
    # S1-only: it is an S1-derived footprint, so it has no business on the
    # HLS snow/ice map - zoom_to_reservoir doubles as the sensor flag here too.
    if zoom_to_reservoir and reservoir is not None and not reservoir.empty:
        reservoir_group = folium.FeatureGroup(name="Reservoir footprint (S1)")
        folium.GeoJson(
            reservoir.__geo_interface__,
            style_function=lambda _: {
                "color": "#0b3d66", "weight": 1.5, "fill": False,
            },
        ).add_to(reservoir_group)
        reservoir_group.add_to(fmap)

    folium.LayerControl(position="topright", collapsed=True).add_to(fmap)
    return fmap


def _swatch_box(color: str, label: str, border: str = "rgba(0,0,0,0.25)") -> str:
    """One legend chip: a filled square (polygon/area layers)."""
    return (
        '<span style="display:inline-flex;align-items:center;white-space:nowrap;">'
        f'<span style="width:14px;height:14px;border-radius:3px;background:{color};'
        f'border:1px solid {border};margin-right:6px;"></span>{label}</span>'
    )


def _swatch_line(color: str, label: str, width: str = "2px") -> str:
    """One legend chip: a horizontal rule (line/outline layers)."""
    return (
        '<span style="display:inline-flex;align-items:center;white-space:nowrap;">'
        f'<span style="width:14px;height:0;border-top:{width} solid {color};'
        f'margin-right:6px;"></span>{label}</span>'
    )


def _render_legend_row(chips: str, bottom_margin: int = 18):
    # Grid layout keeps every column's left edge fixed across rows; a flex row would
    # size each chip to its own label, so a longer label on one row would shift the
    # next column out of alignment with the row above.
    st.markdown(
        f'<div style="display:grid;grid-template-columns:repeat(2, minmax(180px, 1fr));'
        f'gap:6px 16px;font-size:0.85rem;margin:2px 0 {bottom_margin}px;">{chips}</div>',
        unsafe_allow_html=True,
    )


# Overlay legend swatches - colours match the rendered PNG classes (render_overlays.py).
_OVERLAY_LEGEND = {
    "s1": [("#1f6fc0", "Water")],
    "hls": [
        ("#5ac8e6", "Seasonal snow"),
        ("#8e7cc3", "Snow on glacier"),
        ("#5e4b8b", "Bare glacier ice"),
    ],
}


# Sensor-specific product explainer, attached to the "Dataset" radio's own
# help=. Just the pixel-level detail unique to viewing a raw scene - product
# name, optical/radar and revisit rate are already on the Time series tabs
# (Water components / Snow and ice components), so they're not repeated here.
_SENSOR_HELP = {
    "s1": "Mainly captures open water such as the reservoir; narrow "
          "mountain rivers fall below the pixel size.",
    "hls": "Captures snow, glacier ice and water in one product.",
}


def render_overlay_legend(sensor: str):
    """Compact colour-swatch legend under the scene-browser map. Covers every
    layer build_overlay_map() actually draws, not just the raster classes -
    the catchment outline is on that map too."""
    chips = _swatch_line("#5d6d7e", "Catchment (HydroBASINS)")
    chips += "".join(_swatch_box(color, label) for color, label in _OVERLAY_LEGEND.get(sensor, []))
    if sensor == "s1":
        chips += _swatch_line("#0b3d66", "Reservoir footprint (S1)")
    _render_legend_row(chips)


def render_aoi_legend(has_catchment: bool, has_glaciers: bool, has_rivers: bool,
                      has_reservoir: bool):
    """Compact colour-swatch legend under the AOI overview map. Colours match the
    layer styling in build_map(); only entries for layers actually drawn appear."""
    chips = ""
    if has_catchment:
        chips += _swatch_line("#5d6d7e", "Catchment (HydroBASINS)")
    if has_glaciers:
        chips += _swatch_box("#cfc6e8", "RGI v7 glaciers", border="#7e6fb8")
    if has_rivers:
        chips += _swatch_line("#2980b9", "Rivers (HydroRIVERS)")
    if has_reservoir:
        chips += _swatch_box("#1f6fc0", "Reservoir footprint (S1)", border="#0b3d66")
    if chips:
        _render_legend_row(chips, bottom_margin=2)


# ─────────────────────────────────────────────
# CHARTS
# ─────────────────────────────────────────────

def chart_water(timeseries_df: pd.DataFrame) -> go.Figure:
    """Water surface from DSWx-S1. SAR is cloud-independent, so the series is
    gap-free. Shows the reservoir-only footprint (reservoir_area_km2) as the
    main, level-relevant signal and the AOI-wide water (water_km2, incl. rivers)
    as a fainter reference line."""
    fig = go.Figure()

    has_reservoir = ("reservoir_area_km2" in timeseries_df.columns
                     and timeseries_df["reservoir_area_km2"].notna().any())

    # Both series are drawn as continuous lines (connectgaps): the rare date whose
    # SAR scene clips the reservoir NaNs both water_km2 and reservoir_area_km2 (a
    # partial lake would read as a false low). The neighbouring cycles bracket it
    # with near-identical values, so bridging that single date reads as the true
    # trend rather than an interpolation artefact. No marker is drawn on the NaN
    # date, so it stays clear that no measurement exists there.
    # AOI-wide water (includes rivers/other) - reference, drawn fainter.
    fig.add_trace(go.Scatter(
        x=timeseries_df["date"],
        y=timeseries_df["water_km2"],
        mode="lines+markers",
        name="AOI water total (incl. rivers)",
        line=dict(color="#aab7c4", width=1.5, dash="dot"),
        marker=dict(size=3),
        connectgaps=True,
        hovertemplate="%{x|%d.%m.%Y}<br>%{y:.2f} km²<extra>AOI total</extra>",
    ))

    # Reservoir-only footprint - the headline signal, as a single clean line. The
    # data layer already NaNs dates where the lake is under-observed, so no false
    # drawdown can reach the line even with connectgaps on.
    if has_reservoir:
        fig.add_trace(go.Scatter(
            x=timeseries_df["date"],
            y=timeseries_df["reservoir_area_km2"],
            mode="lines+markers",
            name="Reservoir area (footprint)",
            line=dict(color="#1a5276", width=2.5),
            marker=dict(size=4),
            connectgaps=True,
            hovertemplate="%{x|%d.%m.%Y}<br><b>%{y:.2f} km²</b><extra>Reservoir</extra>",
        ))

    fig.update_layout(
        xaxis_title=None,
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family=FONT_STACK, color="#2c3e50"),
        height=450,
        # Title lives outside the chart (see page_time_series), matching
        # the "Scenes over time" fact-line style. Height matches the map box
        # beside it. Autoexpand off with a fixed bottom margin keeps the legend
        # centred below the axis whether it wraps to one row or two.
        margin=dict(t=20, b=110, l=60, r=60, autoexpand=False),
        legend=dict(orientation="h", yanchor="middle", y=-0.23, xanchor="center", x=0.5,
                    font=dict(size=13, color="#2c3e50")),
        # Explicit tick colour: Streamlit's Plotly theme sets a near-white tickfont
        # that would otherwise win over layout.font on the white chart background.
        xaxis=dict(showgrid=True, gridcolor="#f0f0f0", linecolor="#d6dbdf",
                   tickfont=dict(color="#000000")),
        # standoff pushes the title away from the tick labels - without it the
        # rotated "Area (km²)" text crowds right up against the numbers.
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0", linecolor="#d6dbdf",
                   tickfont=dict(color="#000000"),
                   title=dict(text="Area (km²)", standoff=18, font=dict(color="#000000"))),
    )
    return fig


def chart_snow(timeseries_df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()

    # Prefer the coverage/cloud-corrected seasonal snow when available, so partial
    # (swath-edge) dates are not biased low against full-coverage dates.
    seasonal_col = ("seasonal_snow_km2_est" if "seasonal_snow_km2_est" in timeseries_df.columns
                    else "seasonal_snow_km2")
    snow_cols = [seasonal_col, "snow_on_glacier_km2", "bare_ice_km2"]

    # Cloud gap shading (same logic, based on first snow column)
    gap_mask = timeseries_df[snow_cols[0]].isna()
    in_gap = False
    gap_start = None
    for index, is_gap in enumerate(gap_mask):
        if is_gap and not in_gap:
            gap_start = timeseries_df["date"].iloc[index]
            in_gap = True
        elif not is_gap and in_gap:
            fig.add_vrect(
                x0=gap_start, x1=timeseries_df["date"].iloc[index],
                fillcolor="lightgray", opacity=0.3, line_width=0,
            )
            in_gap = False

    for col in snow_cols:
        fig.add_trace(go.Scatter(
            x=timeseries_df["date"],
            y=timeseries_df[col],
            mode="lines",
            name=SNOW_LABELS[col],
            stackgroup="snow",
            line=dict(width=0.5),
            fillcolor=SNOW_COLORS[col],
            connectgaps=False,
            hovertemplate="%{y:.1f} km²<extra>" + SNOW_LABELS[col] + "</extra>",
        ))

    fig.update_layout(
        xaxis_title=None,
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family=FONT_STACK, color="#2c3e50"),
        height=450,
        # Title lives outside the chart (see page_time_series), matching
        # the "Scenes over time" fact-line style. Height matches the map box
        # beside it. The three legend items can wrap to up to three rows in a
        # narrow column; autoexpand off with a fixed bottom margin keeps the
        # legend centred below the axis regardless of the row count.
        margin=dict(t=20, b=110, l=60, r=60, autoexpand=False),
        legend=dict(orientation="h", yanchor="middle", y=-0.23, xanchor="center", x=0.5,
                    font=dict(size=13, color="#2c3e50")),
        # Explicit tick colour: Streamlit's Plotly theme sets a near-white tickfont
        # that would otherwise win over layout.font on the white chart background.
        xaxis=dict(showgrid=True, gridcolor="#f0f0f0", linecolor="#d6dbdf",
                   tickfont=dict(color="#000000")),
        # standoff pushes the title away from the tick labels - without it the
        # rotated "Area (km²)" text crowds right up against the numbers.
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0", linecolor="#d6dbdf",
                   tickfont=dict(color="#000000"),
                   title=dict(text="Area (km²)", standoff=18, font=dict(color="#000000"))),
    )
    return fig


# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="From Snow to Reservoir",
    layout="wide",
)

# Shared font for the whole dashboard, so the dark shell, the light map and the
# charts read as one typographic system (Arimo, per the design guide).
FONT_STACK = "'Arimo', 'Helvetica Neue', Arial, sans-serif"

# ── Theme polish ─────────────────────────────
# Additive CSS only: a single typographic system, a clear heading hierarchy, and
# the KPI tiles / light data panels framed as deliberate cards on the dark shell
# (Bach et al.: consistency, grouped layout, no visual clutter).
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Arimo:wght@400;500;600;700&display=swap');

    /* Streamlit injects its own theme stylesheet after this block, so it wins
       ties on headings/captions even though the selector below already
       matches them - !important is needed here, not just a broader selector.
       The Material icon glyphs (info button, sidebar collapse arrow) render
       via a ligature font and must keep their own font-family or they show
       literal words ("info") instead of the icon. */
    html, body, [class*="css"], .stApp, button, input, textarea, select,
    .stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
    .stApp p, .stApp label, .stApp [data-testid="stCaptionContainer"],
    .stApp [data-testid="stMarkdownContainer"] {
        font-family: 'Arimo', 'Helvetica Neue', Arial, sans-serif !important;
    }
    .stApp [data-testid="stIconMaterial"] {
        font-family: 'Material Symbols Rounded' !important;
    }

    /* The page title is the only heading tag left in the app now (every
       section/card label below it is a plain styled div, not
       st.header/st.subheader) - pin its size explicitly rather than
       trusting Streamlit's default h1 scale. */
    .stApp h1 { font-size: 2.5rem !important; }

    /* Trim Streamlit's oversized default top padding. It reserves ~6rem for the
       top-right toolbar, but the title sits on the left, so a tighter top gap
       reads cleaner without colliding with the toolbar. */
    .block-container { padding-top: 1.2rem !important; }

    /* The sidebar's own top gap comes from two places depending on the
       Streamlit version: the content wrapper's top padding and a header band
       that holds the collapse arrow. Trim both, with !important to beat
       Streamlit's higher-specificity defaults. */
    [data-testid="stSidebarUserContent"],
    [data-testid="stSidebarContent"],
    section[data-testid="stSidebar"] .block-container {
        padding-top: 0.5rem !important;
    }
    [data-testid="stSidebarHeader"] {
        padding-top: 0 !important;
        padding-bottom: 0 !important;
        height: auto !important;
        min-height: 0 !important;
    }
    /* The rule under the nav links (Overview/Scene Browser) sits well below
       them by default - pull it closer. */
    [data-testid="stSidebarNavSeparator"] {
        padding-top: 9px !important;
        margin-bottom: 28px !important;
    }
    /* Compact sidebar: holds only the AOI picker and the time range slider.
       Streamlit sets the width as an inline style (user-resizable), so this
       needs !important to win as the starting width. Fixed rather than
       resizable: the collapse control doesn't work with the width forced
       open like this, so it's hidden below rather than left as a dead button. */
    section[data-testid="stSidebar"] {
        width: 300px !important;
        min-width: 300px !important;
    }
    section[data-testid="stSidebar"] button[data-testid="stBaseButton-headerNoPadding"] {
        display: none;
    }

    /* Row spacing between the sidebar's widgets (AOI picker, time range
       slider). 1.25rem on top of the widgets' own intrinsic 16px gives 36px
       between rows - more breathing room than the 16px gap above the AOI
       picker, on purpose. */
    section[data-testid="stSidebar"] [data-testid="stElementContainer"] {
        margin-bottom: 1.25rem;
    }
    section[data-testid="stSidebar"] nav {
        margin-bottom: 1.25rem;
    }

    /* Time range is a two-handle slider whose left handle sits exactly on the
       dataset's earliest date, so the floating current-value tooltip above it
       and the static min-bound label on the track below it show the identical
       date twice, stacked. The tooltip is the one that also updates when
       dragged, so keep that and drop the redundant static label. */
    div[data-testid="stSlider"]:has([aria-label="Time range"]) [data-testid="stSliderTickBar"] {
        display: none;
    }

    /* Info popover: a plain icon button, not a bordered pill, so it reads as
       a small dark-mode-native control instead of a light default widget.
       The info icon sits in the button's first (visible) child div; the
       auto-added dropdown chevron sits in the second, aria-hidden one -
       hide that wrapper outright rather than the icon glyph itself. */
    button[data-testid="stPopoverButton"] {
        background: transparent !important;
        border: none !important;
        box-shadow: none !important;
        padding: 4px 6px !important;
    }
    button[data-testid="stPopoverButton"]:hover {
        background: rgba(255, 255, 255, 0.08) !important;
    }
    button[data-testid="stPopoverButton"] div[aria-hidden="true"] {
        display: none;
    }
    button[data-testid="stPopoverButton"] [data-testid="stIconMaterial"] {
        color: #2980b9;
        font-size: 1.4rem;
    }

    .stApp h1 { font-weight: 700; letter-spacing: -0.01em; }

    /* KPI tiles framed as grouped cards instead of floating on the dark ground.
       The two cards sit side by side in a column pair (see the sidebar code),
       so the card fills its column instead of using a fixed pixel width. */
    div[data-testid="stElementContainer"]:has(> div[data-testid="stMetric"]) {
        text-align: center;
    }
    div[data-testid="stMetric"] {
        width: 100%;
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.10);
        border-radius: 14px;
        padding: 14px 12px;
        text-align: center;
        /* Equal height across both cards; the card without a delta pill would
           otherwise render shorter than the rest. */
        min-height: 124px;
    }
    div[data-testid="stMetric"] label { opacity: 0.75; }
    /* Center the label, value and delta in each card. The label is a CSS grid by
       default, so it needs flex-centering to actually move; value and delta are
       flex rows that only need justify-content. */
    div[data-testid="stMetric"] [data-testid="stMetricLabel"] {
        display: flex; justify-content: center; align-items: center;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"],
    div[data-testid="stMetric"] [data-testid="stMetricDelta"],
    div[data-testid="stMetric"] div:has(> [data-testid="stMetricDelta"]) {
        justify-content: center;
    }
    /* The delta shows a range (min-max), not a trend, so the built-in
       up/down triangle is misleading - hidden in favour of the "↔" written
       into the delta text itself. Square corners to match the outer card;
       extra margin-top pulls it further away from the value above. */
    div[data-testid="stMetric"] [data-testid^="stMetricDeltaIcon"] {
        display: none;
    }
    div[data-testid="stMetric"] [data-testid="stMetricDelta"] {
        border-radius: 0;
        margin-top: 12px;
    }
    /* delta text supports markdown, so a blank line between the range and
       the mean renders as two stacked paragraphs instead of one long line -
       kept tight (they're one reading unit) while the pill above is spaced
       further from the metric value. */
    div[data-testid="stMetric"] [data-testid="stMetricDelta"] p {
        margin: 0;
        line-height: 1.5;
    }
    div[data-testid="stMetric"] [data-testid="stMetricDelta"] p + p {
        margin-top: 2px;
    }

    /* Light data panels (folium map + plotly charts) as crisp cards, so the
       bright surfaces read as intentional content, not stray white holes. */
    iframe { border-radius: 12px; }
    div[data-testid="stPlotlyChart"] {
        background: #ffffff;
        border-radius: 12px;
        padding: 6px 8px;
        border: 1px solid rgba(255, 255, 255, 0.10);
    }

    /* AOI picker: sized like a search bar (to its content) rather than
       stretched across the full column. min-width covers the longer of the
       two AOI labels so it doesn't visibly resize when switching. */
    div[data-testid="stSelectbox"] {
        width: fit-content !important;
        min-width: 260px;
    }
    div[data-testid="stSelectbox"] > div,
    div[data-testid="stSelectbox"] [data-baseweb="select"] {
        width: fit-content !important;
        min-width: 260px;
    }

    /* Fact-line titles with a help tooltip (Catchment area, Time series):
       the text container defaults to full row width, which pushes the "?"
       icon all the way to the row's right edge instead of right after the
       text - shrink it to content so the icon sits flush against the text,
       like the Dataset radio's label + tooltip. */
    div[data-testid="stMarkdown"]:has([data-testid="stTooltipIcon"])
        [data-testid="stMarkdownContainer"] {
        width: fit-content;
    }

    /* Subtitle: pulled up closer to the title. A margin on the markdown div
       itself doesn't reduce this gap (it's absorbed inside the block's own
       flex item), so the negative margin sits on the keyed wrapper instead,
       which is the actual flex item spaced against the title above it. */
    div.st-key-subtitle_wrap {
        margin-top: -13px;
    }

    /* KPI panel: nudged down so "Latest satellite data" lines up with the
       title's baseline. Both boxes start at the same row top, but the
       title's larger font/line-height pushes its own glyphs further down
       within its box than this smaller heading's glyphs sit within its own -
       this closes that gap so the two look level despite the size
       difference. */
    div.st-key-kpi_slot {
        margin-top: 24px;
    }

    /* Info popover: pinned to the very bottom of the sidebar panel.
       stSidebarUserContent only grows to fit its own content (not the full
       sidebar height), so margin-top:auto has nothing to push against there
       - anchor to stSidebarContent (which is the full-height panel) instead
       via absolute positioning. */
    div[data-testid="stSidebarContent"] {
        position: relative;
    }
    div.st-key-info_popover_wrap {
        position: absolute;
        left: 10px;
        bottom: 20px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# Title/subtitle on the left, a KPI slot on the right at the same top edge.
# page_overview() reopens both columns later - the map goes under the title
# in title_col, the cards go into kpi_slot - so map and cards end up level
# with each other, beside the title, on the Overview page. Empty on the
# other two pages, since only page_overview() writes into them.
title_col, kpi_slot_col = st.columns([3, 2])
with title_col:
    st.title("From Snow to Reservoir")
    # Custom-spaced in place of st.caption: a plain caption sits right under the
    # title with barely any gap, so the title/subtitle pair reads as one block
    # that runs straight into "Area of interest" below it. Wrapped in a keyed
    # container (div.st-key-subtitle_wrap below) rather than relying on the
    # inner div's own margin-top, since that margin is absorbed inside this
    # block's flex item and doesn't reduce the gap to the title above it.
    with st.container(key="subtitle_wrap"):
        st.markdown(
            '<div style="margin-bottom:26px;color:rgba(250,250,250,0.6);font-size:14px;">'
            "Satellite monitoring of the snow, glacier and reservoir water chain in "
            "the Georgian Greater Caucasus</div>",
            unsafe_allow_html=True,
        )
with kpi_slot_col:
    kpi_slot = st.container(key="kpi_slot")

# ── Global controls (sidebar, shared by every page) ──────────
# AOI and time range govern both pages' data, so they live in the sidebar
# rather than on either page individually - pick once, both pages reflect it.
with st.sidebar:
    aoi_label = st.selectbox("Area of interest", list(AOIS.keys()))
aoi = AOIS[aoi_label]

catchment = load_catchment(aoi["key"])

# Snow / glacier come from HLS (optical), water comes from S1 (radar).
with st.spinner("Loading time series..."):
    df_hls_full, is_mock_hls = load_timeseries(aoi["key"])
    df_s1_full,  is_mock_s1  = load_s1_timeseries(aoi["key"])

if is_mock_hls or is_mock_s1:
    st.warning(
        "Parquet file(s) not present yet, so the dashboard shows partly synthetic "
        "demo data. Run extract_timeseries.py for real values.",
    )

min_date = min(df_hls_full["date"].min(), df_s1_full["date"].min()).date()
max_date = max(df_hls_full["date"].max(), df_s1_full["date"].max()).date()

with st.sidebar:
    date_range = st.slider(
        "Time range",
        min_value=min_date,
        max_value=max_date,
        value=(min_date, max_date),
        format="DD.MM.YYYY",
    )
    # Pinned to the very bottom of the sidebar panel (see the keyed wrapper's
    # absolute positioning below) - an icon-only "About" button rather than
    # competing with the AOI/time-range picker for attention at the top.
    info_wrap = st.container(key="info_popover_wrap")
    with info_wrap, st.popover("", icon=":material/info:"):
        st.caption(
            "This project is open science. You can find my full code, "
            "documentation & workflow on my [GitHub]"
            "(https://github.com/sebastianmry/from-snow-to-reservoir)."
        )
        st.caption(
            "Data and licences: OPERA DSWx-S1 and DSWx-HLS (NASA Earthdata); glacier "
            "outlines RGI 7.0 (CC-BY 4.0); catchment, rivers and reservoir seed from "
            "HydroSHEDS, i.e. HydroBASINS, HydroRIVERS and HydroLAKES (free with "
            "attribution); basemap © OpenStreetMap contributors and © CARTO, terrain "
            "layer © Stadia Maps, © Stamen Design, © OpenMapTiles, satellite layer "
            "© Esri, Maxar, Earthstar Geographics."
        )

def _slice(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[(frame["date"] >= pd.Timestamp(date_range[0])) &
                 (frame["date"] <= pd.Timestamp(date_range[1]))].copy()

hls_df = _slice(df_hls_full)   # HLS: snow / glacier
df_s1  = _slice(df_s1_full)    # S1: water

# Scene counts, reused by both the Overview KPI cards and the Scene Browser.
_s1_index  = load_overlay_index(aoi["key"], "s1")
_hls_index = load_overlay_index(aoi["key"], "hls")

# Map source layers, reused by both the Overview map and the Scene Browser map.
with st.spinner("Loading map data..."):
    rivers    = load_rivers(aoi["key"])
    glaciers  = load_glaciers(tuple(aoi["clip_box"]))
    reservoir = load_reservoir(aoi["key"])


def _kpi_card_html(label: str, value: str, date_str: str, range_text: str, mean_text: str) -> str:
    """HTML for one KPI card matching st.metric's look, with the reading's
    date set right above the value, and the range/mean pill below it. Fixed
    width (not max-width) and centred so it stays a compact, punchy number
    card regardless of how wide its containing column is - a flex child with
    only max-width and auto margins shrinks to its content's width instead of
    stretching up to that cap, since auto margins override align-items:
    stretch, so this needs an explicit width now that the Key metrics panel
    is one flex column (see page_overview()). Returns a string (rather than
    calling st.markdown itself) so the panel can compose the heading, scene
    count and both cards into a single HTML block."""
    return (
        '<div style="background:rgba(255,255,255,0.04);border:1px solid '
        'rgba(255,255,255,0.10);border-radius:14px;padding:14px 12px;'
        'text-align:center;min-height:124px;width:min(260px, 100%);margin:0 auto;">'
        f'<div style="font-size:14px;opacity:0.75;">{label}</div>'
        f'<div style="font-size:14px;color:rgba(250,250,250,0.6);'
        f'margin-top:2px;">{date_str}</div>'
        f'<div style="font-size:26px;font-weight:700;margin-top:2px;">{value}</div>'
        '<div style="font-size:14px;color:rgba(250,250,250,0.6);'
        'background:rgba(128,132,149,0.2);border-radius:0;padding:6px 10px;'
        f'margin-top:6px;line-height:1.5;">↔ {range_text}<br>Ø {mean_text}</div>'
        '</div>'
    )


def _kpi_card_no_data_html(label: str) -> str:
    """Same card footprint as _kpi_card_html, for the rare case a reading
    genuinely has no data - keeps the panel's layout stable either way."""
    return (
        '<div style="background:rgba(255,255,255,0.04);border:1px solid '
        'rgba(255,255,255,0.10);border-radius:14px;padding:14px 12px;'
        'text-align:center;min-height:124px;width:min(260px, 100%);margin:0 auto;'
        'display:flex;flex-direction:column;justify-content:center;">'
        f'<div style="font-size:14px;opacity:0.75;">{label}</div>'
        '<div style="font-size:20px;margin-top:6px;opacity:0.6;">No data</div>'
        '</div>'
    )


# AOI map's st_folium height.
MAP_HEIGHT = 420


def page_overview():
    """AOI map beside the KPI cards, both level with the title. Neither
    renders into this function's own page flow directly - the map goes into
    title_col (under the subtitle) and the cards into kpi_slot, both opened
    in the header block above, so this page has no content of its own left
    at the top level; only the time-series charts live on other pages."""
    dam_name = RESERVOIR_NAME.get(aoi["key"], aoi["dam_label"])
    if catchment is not None and not catchment.empty:
        catchment_area_km2 = catchment.to_crs("EPSG:32638").area.sum() / 1e6
        catchment_text = (
            f"Catchment area above the {dam_name} dam: "
            f'<span style="font-weight:700;">{catchment_area_km2:.0f} km²</span>'
        )
    else:
        catchment_text = f"Catchment area above the {dam_name} dam not found"

    latest_s1 = df_s1.iloc[-1] if not df_s1.empty else None
    water_series = df_s1["water_km2"] if not df_s1.empty else pd.Series(dtype=float)
    min_water, max_water, mean_water = (
        water_series.min(), water_series.max(), water_series.mean())

    # Use the coverage-corrected seasonal snow when present (comparable across dates).
    seasonal_col = ("seasonal_snow_km2_est" if "seasonal_snow_km2_est" in hls_df.columns
                    else "seasonal_snow_km2")
    snow_series = (hls_df[seasonal_col] + hls_df["snow_on_glacier_km2"]
                  if not hls_df.empty else pd.Series(dtype=float))
    min_snow, max_snow, mean_snow = (snow_series.min(), snow_series.max(), snow_series.mean())
    latest_snow = snow_series.iloc[-1] if not snow_series.empty else None

    # Bare glacier ice is its own KPI card (not folded into "Total snow"
    # above), since it's a distinct signal - exposed ice, no snow cover.
    ice_series = hls_df["bare_ice_km2"] if not hls_df.empty else pd.Series(dtype=float)
    min_ice, max_ice, mean_ice = (ice_series.min(), ice_series.max(), ice_series.mean())
    latest_ice = ice_series.iloc[-1] if not ice_series.empty else None

    # Reuses title_col (opened in the header block above) so the map sits
    # under the subtitle, level with the KPI cards in kpi_slot beside it.
    with title_col:
        st.markdown(
            f'<div style="color:#fafafa;font-size:14px;">{catchment_text}</div>',
            unsafe_allow_html=True,
            help=(
                "Drainage basin upstream of the dam, delineated from HydroSHEDS "
                "HydroBASINS sub-basin polygons."
            ),
        )
        # Explicit spacer element: a margin on the markdown div above does not
        # reliably translate into extra space in Streamlit's block layout here.
        st.markdown('<div style="height:32px;"></div>', unsafe_allow_html=True)

        aoi_map = build_map(aoi, rivers, glaciers, reservoir, catchment)
        # returned_objects=[] stops st_folium from sending map-interaction data
        # back on every pan/zoom/scroll, which otherwise triggers a Streamlit
        # rerun and the transient dimming overlay. The return value is unused
        # here anyway.
        st_folium(aoi_map, height=MAP_HEIGHT, use_container_width=True, returned_objects=[])
        render_aoi_legend(
            has_catchment=catchment is not None and not catchment.empty,
            has_glaciers=glaciers is not None and not glaciers.empty,
            has_rivers=bool(rivers),
            has_reservoir=reservoir is not None and not reservoir.empty,
        )

    reservoir_series = (df_s1["reservoir_area_km2"]
                        if "reservoir_area_km2" in df_s1.columns
                        else pd.Series(dtype=float))
    has_reservoir = reservoir_series.notna().any()
    if has_reservoir:
        # Current = last date with a valid lake reading; range/mean over
        # valid dates. (False-drawdown dates are already NaN via the
        # reservoir guard.)
        min_reservoir, max_reservoir, mean_reservoir = (
            reservoir_series.min(), reservoir_series.max(), reservoir_series.mean())
        valid_reservoir = reservoir_series.dropna()
        latest_reservoir = valid_reservoir.iloc[-1]
        latest_reservoir_date = df_s1.loc[valid_reservoir.index[-1], "date"]
        card1_html = _kpi_card_html(
            "Reservoir area (S1)", f"{latest_reservoir:.2f} km²",
            latest_reservoir_date.strftime("%d/%m/%Y"),
            f"{min_reservoir:.2f}–{max_reservoir:.2f} km²", f"{mean_reservoir:.2f} km²",
        )
    elif latest_s1 is not None:
        card1_html = _kpi_card_html(
            "Water area (S1)", f"{latest_s1['water_km2']:.2f} km²",
            latest_s1["date"].strftime("%d/%m/%Y"),
            f"{min_water:.2f}–{max_water:.2f} km²", f"{mean_water:.2f} km²",
        )
    else:
        card1_html = _kpi_card_no_data_html("Water area (S1)")

    if latest_snow is not None:
        latest_snow_date = hls_df["date"].iloc[-1]
        card2_html = _kpi_card_html(
            "Total snow (HLS)", f"{latest_snow:.0f} km²",
            latest_snow_date.strftime("%d/%m/%Y"),
            f"{min_snow:.0f}–{max_snow:.0f} km²", f"{mean_snow:.0f} km²",
        )
    else:
        card2_html = _kpi_card_no_data_html("Total snow (HLS)")

    if latest_ice is not None:
        latest_ice_date = hls_df["date"].iloc[-1]
        card3_html = _kpi_card_html(
            "Total ice (HLS)", f"{latest_ice:.1f} km²",
            latest_ice_date.strftime("%d/%m/%Y"),
            f"{min_ice:.1f}–{max_ice:.1f} km²", f"{mean_ice:.1f} km²",
        )
    else:
        card3_html = _kpi_card_no_data_html("Total ice (HLS)")

    # Reuses kpi_slot (opened beside the title in the header block above),
    # so the cards sit level with the title.
    with kpi_slot:
        # The heading, scene count and three cards render as one HTML block
        # (not five separate st.markdown calls) so flexbox can size and
        # space them evenly.
        st.markdown(
            # gap:20px is the base spacing (used between the cards); the
            # heading-to-scene-count and scene-count-to-first-card gaps are
            # each pulled in by 4px (margin-top:-4px on the flex items
            # themselves) to a tighter 16px.
            '<div style="display:flex;flex-direction:column;gap:20px;">'
            '<div style="text-align:center;font-size:14px;font-weight:700;'
            f'color:#fafafa;">Latest satellite data</div>'
            '<div style="text-align:center;font-size:14px;font-weight:400;'
            f'margin-top:-4px;color:#fafafa;">'
            f'{len(_s1_index["dates"]) if _s1_index else 0} S1 · '
            f'{len(_hls_index["dates"]) if _hls_index else 0} HLS valid scenes</div>'
            f'<div style="margin-top:-4px;">{card1_html}</div>'
            f'{card2_html}'
            f'{card3_html}'
            '</div>',
            unsafe_allow_html=True,
        )


def page_time_series():
    """Water, snow and ice trends over the selected AOI and time range, on its
    own page so the Overview stays a compact map-plus-KPI summary."""
    # Same fact-line style/spacing as the catchment fact on Overview and
    # "Scenes over time" on Scene Browser, so all three pages open the same way.
    # Tooltip is UI mechanics only, like Scene Browser's - the S1/HLS product
    # details live in the two tabs' own "?" tooltips right below.
    st.markdown(
        '<div style="color:#fafafa;font-size:14px;font-weight:700;">'
        "Time series</div>",
        unsafe_allow_html=True,
        help="Pick a tab to view how water, snow, or ice area changed "
             "over the selected time range.",
    )
    st.markdown('<div style="height:16px;"></div>', unsafe_allow_html=True)

    tab1, tab2 = st.tabs(["Water area", "Snow & ice"])

    # Chart titles as single fact lines above the chart (matching the
    # "Scenes over time" style) instead of Plotly's built-in in-chart title.
    # One line each - any extra number or methodology detail goes in the "?"
    # tooltip instead of a second line, so the chart never sits under two
    # stacked titles.
    with tab1:
        st.markdown(
            '<div style="color:#fafafa;font-size:14px;">'
            "Water components</div>",
            unsafe_allow_html=True,
            help="OPERA DSWx-S1 surface water extent, radar-derived so it "
                 "sees through cloud; ~12 day revisit per Sentinel-1 pass.",
        )
        st.markdown('<div style="height:16px;"></div>', unsafe_allow_html=True)
        st.plotly_chart(chart_water(df_s1), width="stretch")

    with tab2:
        st.markdown(
            '<div style="color:#fafafa;font-size:14px;">'
            "Snow and ice components</div>",
            unsafe_allow_html=True,
            help="Stacked as seasonal snow, snow on glacier, and bare "
                 "glacier ice. OPERA DSWx-HLS snow/ice classification, "
                 "optical-derived (Landsat 8/9 + Sentinel-2); ~2-3 day "
                 "revisit, though usable scenes are fewer in practice due "
                 "to cloudy imagery.",
        )
        st.markdown('<div style="height:16px;"></div>', unsafe_allow_html=True)
        st.plotly_chart(chart_snow(hls_df), width="stretch")


def page_scene_browser():
    """Pick a sensor and a date, see the pre-rendered raster for that exact
    scene, with the raw tables underneath for anyone who wants the numbers."""
    # Matches the catchment-fact style on the Overview page (page_overview)
    # rather than a heading, so it reads as a small fact line, not a section
    # title. Tooltip is UI mechanics only - the Dataset radio right below
    # carries its own per-sensor product explainer.
    st.markdown(
        '<div style="color:#fafafa;font-size:14px;font-weight:700;">'
        "Scenes over time</div>",
        unsafe_allow_html=True,
        help="Pick a sensor and date to view the raster's classified "
             "extent in that scene.",
    )
    # Explicit spacer element: a margin on the markdown div above does not
    # reliably translate into extra space in Streamlit's block layout here.
    # Matches the title-to-content gap on Time series, since both titles
    # carry the same tooltip icon.
    st.markdown('<div style="height:16px;"></div>', unsafe_allow_html=True)

    sensor_label = st.radio(
        "Dataset", list(OVERLAY_SENSORS.keys()), horizontal=True,
        help="Water (S1): " + _SENSOR_HELP["s1"] + " Snow & ice (HLS): " + _SENSOR_HELP["hls"],
    )
    sensor = OVERLAY_SENSORS[sensor_label]
    overlay_index = _s1_index if sensor == "s1" else _hls_index

    if overlay_index is None:
        st.info(
            "No scenes have been rendered for this area and sensor yet. "
            "Run `python render_overlays.py` (it reads the GeoTIFFs from the tile "
            "store and writes coloured PNGs into static_data/overlays/)."
        )
    else:
        dates = overlay_index["dates"]
        # Explicit spacer element: a margin on the radio widget above does not
        # reliably translate into extra space in Streamlit's block layout here.
        st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
        chosen_date = st.select_slider(
            "Date", options=dates, value=dates[-1],
            format_func=lambda d: f"{d[6:8]}.{d[4:6]}.{d[0:4]}",
        )
        overlay_uri = load_overlay_uri(aoi["key"], sensor, chosen_date)
        if overlay_uri is None:
            st.warning("Scene not readable.")
        else:
            overlay_map = build_overlay_map(
                aoi, overlay_uri, overlay_index["bounds"], catchment, reservoir,
                zoom_to_reservoir=(sensor == "s1"),
            )
            st_folium(overlay_map, height=430, use_container_width=True,
                      key=f"overlay_{aoi['key']}_{sensor}", returned_objects=[])
        render_overlay_legend(sensor)

    with st.expander("Show raw data"):
        st.caption("Water (DSWx-S1)")
        st.dataframe(
            df_s1.sort_values("date", ascending=False).reset_index(drop=True),
            width="stretch", hide_index=True,
        )
        st.caption("Snow / glaciers (DSWx-HLS)")
        hls_view_df = hls_df.drop(columns=["water_area_km2"], errors="ignore")
        st.dataframe(
            hls_view_df.sort_values("date", ascending=False).reset_index(drop=True),
            width="stretch", hide_index=True,
        )


pg = st.navigation([
    st.Page(page_overview, title="Overview", icon=":material/map:", default=True),
    st.Page(page_time_series, title="Time series", icon=":material/show_chart:"),
    st.Page(page_scene_browser, title="Scene Browser", icon=":material/layers:"),
])
pg.run()
