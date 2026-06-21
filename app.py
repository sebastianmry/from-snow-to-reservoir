"""
FROM SNOW TO RESERVOIR - Streamlit Dashboard
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

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
from shapely.geometry import shape
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

    water      = 12 + 3 * np.sin(time_axis * 0.5 + 1) + rng.normal(0, 0.4, len(dates))
    seas_snow  = np.clip(60 + 50 * np.sin(time_axis + np.pi) + rng.normal(0, 5, len(dates)), 0, None)
    glac_snow  = np.clip(30 + 20 * np.sin(time_axis + np.pi) + rng.normal(0, 3, len(dates)), 0, None)
    bare_ice   = np.clip(20 - 15 * np.sin(time_axis + np.pi) + rng.normal(0, 2, len(dates)), 0, None)
    cloud      = np.clip(rng.uniform(0, 45, len(dates)), 0, 30)

    # Sprinkle some NaN cloud gaps
    gap_indices = rng.choice(len(dates), size=20, replace=False)
    for series in [water, seas_snow, glac_snow, bare_ice]:
        series[gap_indices] = np.nan

    return pd.DataFrame({
        "date":                pd.to_datetime(dates),
        "water_area_km2":      np.round(water, 2),
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


def build_map(aoi: dict, rivers: list[dict] | None, glaciers: gpd.GeoDataFrame | None,
              reservoir: gpd.GeoDataFrame | None = None,
              catchment: gpd.GeoDataFrame | None = None) -> folium.Map:
    min_lon, min_lat, max_lon, max_lat = aoi["clip_box"]
    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2

    fmap = folium.Map(
        location=[center_lat, center_lon],
        tiles="CartoDB positron",
    )
    # Fit exactly to the AOI so it is always centered regardless of AOI size
    fmap.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

    # Load Montserrat so the on-map reservoir label matches the CartoDB Positron
    # basemap typography (its place labels use the Montserrat family).
    fmap.get_root().header.add_child(folium.Element(
        '<link href="https://fonts.googleapis.com/css2?'
        'family=Montserrat:wght@500;600&display=swap" rel="stylesheet">'
    ))

    # Reservoir centre for placing the reservoir-name label on the lake itself.
    res_label_anchor = None
    if reservoir is not None and not reservoir.empty:
        reservoir_centroid = reservoir.geometry.union_all().centroid
        res_label_anchor = (reservoir_centroid.y, reservoir_centroid.x)

    # AOI = the drainage basin above the dam (HydroBASINS catchment). Draw its
    # contour; the dashed bbox is only a fallback when the catchment is missing.
    if catchment is not None and not catchment.empty:
        folium.GeoJson(
            catchment.__geo_interface__,
            name="Catchment",
            style_function=lambda _: {
                "color": "#5d6d7e",
                "weight": 2.0,
                "fillColor": "#5d6d7e",
                "fillOpacity": 0.04,
            },
            tooltip="Catchment above the dam",
        ).add_to(fmap)
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
                    icon_size=(150, 18),
                    icon_anchor=(75, 9),
                    html=(
                        # Plain label, no background box: a white text halo keeps
                        # it legible over the blue reservoir fill instead.
                        '<div style="font-size:9px;font-weight:600;color:#000000;'
                        "font-family:'Montserrat','Helvetica Neue',Arial,sans-serif;"
                        'text-align:center;white-space:nowrap;'
                        'text-shadow:-1px -1px 1px #fff, 1px -1px 1px #fff, '
                        '-1px 1px 1px #fff, 1px 1px 1px #fff;">'
                        f'{name} Reservoir</div>'
                    ),
                ),
            ).add_to(fmap)

    # Reservoir footprint (S1-derived) - the actual lake polygon. The headline
    # feature, so make it pop: vivid blue fill + crisp dark outline on top of the
    # paler river/water layers.
    if reservoir is not None and not reservoir.empty:
        area_km2 = reservoir.iloc[0].get("area_km2")
        tooltip = "Reservoir footprint (S1)" + (f": {area_km2:.2f} km²"
                                                if area_km2 is not None else "")
        folium.GeoJson(
            reservoir.__geo_interface__,
            name="Reservoir footprint (S1)",
            style_function=lambda _: {
                "fillColor": "#1f6fc0",
                "color": "#0b3d66",
                "weight": 2.5,
                "fillOpacity": 0.78,
            },
            highlight_function=lambda _: {"weight": 3.5, "fillOpacity": 0.9},
            tooltip=tooltip,
        ).add_to(fmap)

    folium.LayerControl().add_to(fmap)
    return fmap


def _reservoir_zoom_bounds(reservoir: gpd.GeoDataFrame, pad: float = 0.5) -> list | None:
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
                      glaciers: gpd.GeoDataFrame | None = None,
                      zoom_to_reservoir: bool = False) -> folium.Map:
    """Light-weight map for the scene browser: basemap, catchment contour, the
    chosen pre-rendered raster, and (for HLS) the RGI glacier outlines so glaciers
    are always clearly bounded - whether currently snow-covered or bare ice.

    For the S1 water scenes (zoom_to_reservoir), the view opens framed on the
    reservoir footprint, since that is where the radar water signal lives."""
    fmap = folium.Map(tiles="CartoDB positron")
    res_bounds = _reservoir_zoom_bounds(reservoir) if zoom_to_reservoir else None
    fmap.fit_bounds(res_bounds if res_bounds else bounds)

    if catchment is not None and not catchment.empty:
        folium.GeoJson(
            catchment.__geo_interface__,
            style_function=lambda _: {
                "color": "#5d6d7e", "weight": 2.0, "fill": False,
            },
        ).add_to(fmap)

    folium.raster_layers.ImageOverlay(
        image=png_uri, bounds=bounds, opacity=0.9, zindex=10,
    ).add_to(fmap)

    # RGI glacier outlines (violet, no fill) so the glacier extent reads clearly
    # against the cyan snow field / over the bare-ice raster.
    if glaciers is not None and not glaciers.empty:
        folium.GeoJson(
            glaciers.__geo_interface__,
            style_function=lambda _: {
                "color": "#5e4b8b", "weight": 1.0, "fill": False, "opacity": 0.9,
            },
        ).add_to(fmap)

    # Thin reservoir outline on top, for orientation against the water raster.
    if reservoir is not None and not reservoir.empty:
        folium.GeoJson(
            reservoir.__geo_interface__,
            style_function=lambda _: {
                "color": "#0b3d66", "weight": 1.5, "fill": False,
            },
        ).add_to(fmap)
    return fmap


# Overlay legend swatches - colours match the rendered PNG classes (render_overlays.py).
_OVERLAY_LEGEND = {
    "s1": [("#1f6fc0", "Water")],
    "hls": [
        ("#5ac8e6", "Seasonal snow"),
        ("#8e7cc3", "Snow on glacier"),
        ("#5e4b8b", "Bare glacier ice"),
        ("#1f6fc0", "Water"),
    ],
}


def render_overlay_legend(sensor: str):
    """Compact colour-swatch legend under the scene-browser map."""
    items = _OVERLAY_LEGEND.get(sensor, [])
    chips = "".join(
        f'<span style="display:inline-flex;align-items:center;margin-right:16px;'
        f'white-space:nowrap;">'
        f'<span style="width:14px;height:14px;border-radius:3px;background:{color};'
        f'border:1px solid rgba(0,0,0,0.25);margin-right:6px;"></span>{label}</span>'
        for color, label in items
    )
    if sensor == "hls":
        chips += (
            '<span style="display:inline-flex;align-items:center;white-space:nowrap;">'
            '<span style="width:14px;height:0;border-top:2px solid #5e4b8b;'
            'margin-right:6px;"></span>Glacier boundary (RGI)</span>'
        )
    st.markdown(
        f'<div style="display:flex;flex-wrap:wrap;gap:6px 0;font-size:0.85rem;'
        f'margin-top:4px;">{chips}</div>',
        unsafe_allow_html=True,
    )
    if sensor == "s1":
        st.caption(
            "DSWx-S1 (radar, cloud independent, 30 m grid). SAR mainly captures open "
            "water such as the reservoir; narrow mountain rivers usually fall below the "
            "pixel size and are barely detected."
        )


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

    # AOI-wide water (includes rivers/other) - reference, drawn fainter
    fig.add_trace(go.Scatter(
        x=timeseries_df["date"],
        y=timeseries_df["water_km2"],
        mode="lines+markers",
        name="AOI water total (incl. rivers)",
        line=dict(color="#aab7c4", width=1.5, dash="dot"),
        marker=dict(size=3),
        hovertemplate="%{x|%d.%m.%Y}<br>%{y:.2f} km²<extra>AOI total</extra>",
    ))

    # Reservoir-only footprint - the headline signal, as a single clean line.
    # Robustness lives in the data layer: the reservoir guard already sets dates
    # where the lake is under-observed to NaN (connectgaps=False -> shown as a gap),
    # so no false drawdowns reach the line and no extra smoothing trace is needed.
    if has_reservoir:
        fig.add_trace(go.Scatter(
            x=timeseries_df["date"],
            y=timeseries_df["reservoir_area_km2"],
            mode="lines+markers",
            name="Reservoir area (footprint)",
            line=dict(color="#1a5276", width=2.5),
            marker=dict(size=4),
            connectgaps=False,
            hovertemplate="%{x|%d.%m.%Y}<br><b>%{y:.2f} km²</b><extra>Reservoir</extra>",
        ))

    fig.update_layout(
        title="Reservoir water area (DSWx-S1, ~12 day)",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        xaxis_title=None,
        yaxis_title="Area (km²)",
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family=FONT_STACK, color="#2c3e50"),
        margin=dict(t=40, b=20, l=60, r=20),
        xaxis=dict(showgrid=True, gridcolor="#f0f0f0", linecolor="#d6dbdf"),
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0", linecolor="#d6dbdf"),
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
        title="Snow and ice components (stacked)",
        xaxis_title=None,
        yaxis_title="Area (km²)",
        hovermode="x unified",
        plot_bgcolor="white",
        paper_bgcolor="white",
        font=dict(family=FONT_STACK, color="#2c3e50"),
        margin=dict(t=40, b=20, l=60, r=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        xaxis=dict(showgrid=True, gridcolor="#f0f0f0", linecolor="#d6dbdf"),
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0", linecolor="#d6dbdf"),
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

    html, body, [class*="css"], .stApp, button, input, textarea, select {
        font-family: 'Arimo', 'Helvetica Neue', Arial, sans-serif;
    }

    /* Heading hierarchy: distinct title, calmer section heads. */
    .stApp h1 { font-weight: 700; letter-spacing: -0.01em; }
    .stApp h2, .stApp h3 { font-weight: 600; letter-spacing: -0.005em; }

    /* KPI tiles framed as grouped cards instead of floating on the dark ground. */
    div[data-testid="stMetric"] {
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.10);
        border-radius: 12px;
        padding: 14px 18px;
    }
    div[data-testid="stMetric"] label { opacity: 0.75; }

    /* Light data panels (folium map + plotly charts) as crisp cards, so the
       bright surfaces read as intentional content, not stray white holes. */
    iframe { border-radius: 12px; }
    div[data-testid="stPlotlyChart"] {
        background: #ffffff;
        border-radius: 12px;
        padding: 6px 8px;
        border: 1px solid rgba(255, 255, 255, 0.10);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("From Snow to Reservoir")
st.caption("Satellite monitoring of the snow, glacier and reservoir water chain in the Georgian Greater Caucasus")

# ── Sidebar ──────────────────────────────────
with st.sidebar:
    st.header("Settings")
    aoi_label = st.selectbox("Area of interest", list(AOIS.keys()))
    aoi = AOIS[aoi_label]

    st.divider()
    st.caption(
        "© Sebastian Macherey · "
        "[GitHub](https://github.com/sebastianmry/from-snow-to-reservoir)"
    )

# ── Load data ────────────────────────────────
# Snow / glacier come from HLS (optical), water comes from S1 (radar).
with st.spinner("Loading time series..."):
    df_hls_full, is_mock_hls = load_timeseries(aoi["key"])
    df_s1_full,  is_mock_s1  = load_s1_timeseries(aoi["key"])

if is_mock_hls or is_mock_s1:
    st.warning(
        "Parquet file(s) not present yet, so the dashboard shows partly synthetic "
        "demo data. Run extract_timeseries.py for real values.",
    )

# Date range slider (spanning both series)
min_date = min(df_hls_full["date"].min(), df_s1_full["date"].min()).date()
max_date = max(df_hls_full["date"].max(), df_s1_full["date"].max()).date()

date_range = st.sidebar.slider(
    "Time range",
    min_value=min_date,
    max_value=max_date,
    value=(min_date, max_date),
    format="DD.MM.YYYY",
)

def _slice(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[(frame["date"] >= pd.Timestamp(date_range[0])) &
                 (frame["date"] <= pd.Timestamp(date_range[1]))].copy()

hls_df = _slice(df_hls_full)   # HLS: snow / glacier
df_s1  = _slice(df_s1_full)    # S1: water

# ── KPI tiles ────────────────────────────────
# Water from S1; snow / glacier from HLS
latest_s1   = df_s1.iloc[-1] if not df_s1.empty else None
max_water   = df_s1["water_km2"].max() if not df_s1.empty else None

latest_hls  = hls_df.iloc[-1] if not hls_df.empty else None
# Use the coverage-corrected seasonal snow when present (comparable across dates).
seasonal_col = ("seasonal_snow_km2_est" if "seasonal_snow_km2_est" in hls_df.columns
                else "seasonal_snow_km2")
max_snow    = ((hls_df[seasonal_col] + hls_df["snow_on_glacier_km2"]).max()
               if not hls_df.empty else None)
latest_snow = (
    latest_hls[seasonal_col] + latest_hls["snow_on_glacier_km2"]
    if latest_hls is not None else None
)

col1, col2, col3, col4 = st.columns(4)

with col1:
    reservoir_series = (df_s1["reservoir_area_km2"] if "reservoir_area_km2" in df_s1.columns
                        else pd.Series(dtype=float))
    has_reservoir = reservoir_series.notna().any()
    if has_reservoir:
        # Current = last date with a valid lake reading; max over valid dates.
        # (False-drawdown dates are already NaN via the reservoir guard.)
        max_reservoir    = reservoir_series.max()
        latest_reservoir = reservoir_series.dropna().iloc[-1]
        st.metric(
            "Reservoir area (S1, current)",
            f"{latest_reservoir:.2f} km²",
            delta=f"Max: {max_reservoir:.2f} km²",
            delta_color="off",
            help="Reservoir footprint from DSWx-S1 radar (cloud independent). "
                 "Current = most recent valid date in the selected range; max over "
                 "the same range.",
        )
    elif latest_s1 is not None:
        st.metric(
            "Water area (S1, current)",
            f"{latest_s1['water_km2']:.2f} km²",
            delta=f"Max: {max_water:.2f} km²",
            delta_color="off",
            help="AOI-wide open water from DSWx-S1 radar (includes rivers). "
                 "Current = most recent date in the selected range.",
        )
    else:
        st.metric("Water area (S1, current)", "No data")

with col2:
    if latest_snow is not None:
        st.metric(
            "Total snow (HLS, current)",
            f"{latest_snow:.0f} km²",
            delta=f"Max: {max_snow:.0f} km²",
            delta_color="off",
            help="Seasonal snow plus snow lying on glaciers, from optical HLS. "
                 "Coverage corrected where available, so partial scenes are not "
                 "biased low.",
        )
    else:
        st.metric("Total snow (HLS, current)", "No data")

with col3:
    if latest_hls is not None:
        st.metric(
            "Bare glacier ice", f"{latest_hls['bare_ice_km2']:.1f} km²",
            help="Exposed (snow free) glacier ice on the most recent HLS scene in "
                 "the selected range.",
        )
    else:
        st.metric("Bare glacier ice", "No data")

with col4:
    st.metric(
        "Scenes in range",
        f"{len(df_s1)} S1 (water)",
        delta=f"{len(hls_df)} HLS (snow)",
        delta_color="off",
        help="Number of acquisitions in the selected time range, by sensor.",
    )

st.divider()

# ── Map + Charts ─────────────────────────────
map_col, chart_col = st.columns([1, 1], gap="large")

with map_col:
    st.subheader("Area of interest")
    with st.spinner("Loading map data..."):
        rivers    = load_rivers(aoi["key"])
        glaciers  = load_glaciers(tuple(aoi["clip_box"]))
        reservoir = load_reservoir(aoi["key"])
        catchment = load_catchment(aoi["key"])

    captions = []
    if catchment is not None:
        captions.append("Catchment (HydroBASINS)")
    if glaciers is not None:
        # Count only glaciers inside the basin, matching the catchment-clipped map.
        if catchment is not None and not catchment.empty:
            n_glaciers = int(glaciers.geometry.intersects(catchment.geometry.union_all()).sum())
        else:
            n_glaciers = len(glaciers)
        captions.append(f"{n_glaciers} RGI v7 glacier polygons")
    else:
        captions.append("RGI glacier data not found")
    if reservoir is not None:
        reservoir_area = reservoir.iloc[0].get("area_km2")
        captions.append(f"Reservoir footprint (S1)"
                        f"{f': {reservoir_area:.2f} km²' if reservoir_area is not None else ''}")
    else:
        captions.append("Reservoir footprint not found, run derive_reservoir.py")
    st.caption(" · ".join(captions))

    aoi_map = build_map(aoi, rivers, glaciers, reservoir, catchment)
    # returned_objects=[] stops st_folium from sending map-interaction data back on
    # every pan/zoom/scroll, which otherwise triggers a Streamlit rerun and the
    # transient dimming overlay. The return value is unused here anyway.
    st_folium(aoi_map, height=430, use_container_width=True, returned_objects=[])

with chart_col:
    st.subheader("Time series")
    tab1, tab2 = st.tabs(["Water area", "Snow & ice"])

    with tab1:
        st.plotly_chart(chart_water(df_s1), width="stretch")

    with tab2:
        st.plotly_chart(chart_snow(hls_df), width="stretch")

# ── Scene browser (pre-rendered raster overlays) ─────────
st.divider()
st.subheader("Scenes over time")

sensor_label = st.radio(
    "Dataset", list(OVERLAY_SENSORS.keys()), horizontal=True,
    help="S1 (radar, cloud independent) shows water; HLS (optical) shows snow and ice.",
)
sensor = OVERLAY_SENSORS[sensor_label]
overlay_index = load_overlay_index(aoi["key"], sensor)

if overlay_index is None:
    st.info(
        "No scenes have been rendered for this area and sensor yet. "
        "Run `python render_overlays.py` (it reads the GeoTIFFs from the tile "
        "store and writes coloured PNGs into static_data/overlays/)."
    )
else:
    dates = overlay_index["dates"]
    chosen_date = st.select_slider(
        "Date", options=dates, value=dates[-1],
        format_func=lambda d: f"{d[6:8]}.{d[4:6]}.{d[0:4]}",
    )
    overlay_uri = load_overlay_uri(aoi["key"], sensor, chosen_date)
    if overlay_uri is None:
        st.warning("Scene not readable.")
    else:
        # Clip the glacier outlines to the catchment so they end exactly at the
        # basin boundary - matching the catchment-masked raster and the
        # catchment-relative statistics (glaciers outside don't feed this reservoir).
        glaciers_clipped = None
        if sensor == "hls" and glaciers is not None:
            glaciers_clipped = (gpd.clip(glaciers, catchment)
                                if catchment is not None and not catchment.empty
                                else glaciers)
        overlay_map = build_overlay_map(
            aoi, overlay_uri, overlay_index["bounds"], catchment, reservoir,
            glaciers=glaciers_clipped,
            zoom_to_reservoir=(sensor == "s1"),
        )
        st_folium(overlay_map, height=430, use_container_width=True,
                  key=f"overlay_{aoi['key']}_{sensor}", returned_objects=[])
    render_overlay_legend(sensor)

# ── Data tables (collapsible) ─────────────────
with st.expander("Show raw data"):
    st.caption("Water (DSWx-S1)")
    st.dataframe(
        df_s1.sort_values("date", ascending=False).reset_index(drop=True),
        width="stretch", hide_index=True,
    )
    st.caption("Snow / glaciers (DSWx-HLS)")
    # Drop the optical HLS water column: it massively over-detects water
    # (terrain shadow / ice misclassified); the water signal comes from S1.
    hls_view_df = hls_df.drop(columns=["water_area_km2"], errors="ignore")
    st.dataframe(
        hls_view_df.sort_values("date", ascending=False).reset_index(drop=True),
        width="stretch", hide_index=True,
    )

# ── About ─────────────────────────────────────
with st.expander("About this project"):
    st.caption(
        "This dashboard tracks the snow to glacier to reservoir water chain above "
        "two Georgian hydropower dams (Enguri and Zhinvali) from open satellite "
        "data. Reservoir and water area come from Sentinel-1 radar (DSWx-S1, "
        "cloud-independent); seasonal snow and glacier cover come from optical HLS "
        "(DSWx-HLS). Statistics are masked to each dam's upstream catchment "
        "(HydroBASINS). Built for the university course Automated Geospatial Data "
        "Processing."
    )
    st.markdown(
        "Live app: [from-snow-to-reservoir.streamlit.app]"
        "(https://from-snow-to-reservoir.streamlit.app/)  \n"
        "Source and methodology: [GitHub]"
        "(https://github.com/sebastianmry/from-snow-to-reservoir)"
    )
