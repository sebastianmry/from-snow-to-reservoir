"""
FROM SNOW TO RESERVOIR - Streamlit Dashboard
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Stage 3 of the pipeline: interactive visualization of HLS timeseries data.

Run with:
    streamlit run app.py
"""

import json
from datetime import date, timedelta
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
    cfg["display_label"]: {
        "key": cfg["name"],
        "clip_box": cfg["clip_box"],
        "center": cfg["center"],
        "dam": cfg["dam"],
        "dam_label": cfg["dam_label"],
        "zoom": cfg["zoom"],
    }
    for cfg in _AOI_CONFIG.values()
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
    t = np.linspace(0, 4 * np.pi, len(dates))

    water      = 12 + 3 * np.sin(t * 0.5 + 1) + rng.normal(0, 0.4, len(dates))
    seas_snow  = np.clip(60 + 50 * np.sin(t + np.pi) + rng.normal(0, 5, len(dates)), 0, None)
    glac_snow  = np.clip(30 + 20 * np.sin(t + np.pi) + rng.normal(0, 3, len(dates)), 0, None)
    bare_ice   = np.clip(20 - 15 * np.sin(t + np.pi) + rng.normal(0, 2, len(dates)), 0, None)
    cloud      = np.clip(rng.uniform(0, 45, len(dates)), 0, 30)

    # Sprinkle some NaN cloud gaps
    gap_idx = rng.choice(len(dates), size=20, replace=False)
    for col in [water, seas_snow, glac_snow, bare_ice]:
        col[gap_idx] = np.nan

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
    """Load HLS parquet timeseries (snow / glacier). Returns (df, is_mock)."""
    path = Path(f"{aoi_key}_timeseries.parquet")
    if path.exists():
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True), False
    return make_mock_data(aoi_key), True


@st.cache_data(show_spinner=False)
def load_s1_timeseries(aoi_key: str) -> tuple[pd.DataFrame, bool]:
    """Load DSWx-S1 parquet timeseries (water surface). Returns (df, is_mock).

    Water comes from S1, not HLS: optical HLS massively over-detects water
    (terrain shadow / ice misclassified), so the reservoir water signal uses
    the cloud-independent radar product (column water_km2).
    """
    path = Path(f"{aoi_key}_s1_timeseries.parquet")
    if path.exists():
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"])
        return df.sort_values("date").reset_index(drop=True), False
    # Mock fallback: a smooth ~12-day water series
    rng = np.random.default_rng(seed=99 if aoi_key == "enguri" else 13)
    dates = pd.date_range("2024-08-01", periods=55, freq="12D")
    t = np.linspace(0, 4 * np.pi, len(dates))
    base = 24 if aoi_key == "enguri" else 40
    water = base + 6 * np.sin(t * 0.5 + 1) + rng.normal(0, 0.6, len(dates))
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
    with open(path, "w") as f:
        json.dump(rivers, f)
    return path


@st.cache_data(show_spinner=False)
def load_rivers(aoi_key: str) -> list[dict] | None:
    path = ensure_rivers()
    if path is None:
        return None
    with open(path) as f:
        gj = json.load(f)
    return [ft for ft in gj["features"] if ft["properties"]["aoi"] == aoi_key]


@st.cache_data(show_spinner=False)
def load_glaciers(clip_box: tuple) -> gpd.GeoDataFrame | None:
    candidates = list(STATIC_DIR.rglob("RGI2000-v7.0-G-12_caucasus*middle_east.shp"))
    if not candidates:
        return None
    try:
        min_lon, min_lat, max_lon, max_lat = clip_box
        gdf = gpd.read_file(candidates[0], bbox=(min_lon, min_lat, max_lon, max_lat))
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        if gdf.empty:
            return None
        # Clean the name column: keep only real names; blank out empty values and
        # catalogue IDs (e.g. "198b", "193a") - a real name has a run of >=3
        # letters, an ID does not. Unicode-aware so Cyrillic names are kept.
        if "glac_name" in gdf.columns:
            import re
            def _clean_name(v):
                s = "" if v is None else str(v).strip()
                if s.lower() in ("", "nan", "none"):
                    return ""
                return s if re.search(r"[^\W\d_]{3,}", s) else ""
            gdf["glac_name"] = gdf["glac_name"].map(_clean_name)
        return gdf
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
        gdf = gpd.read_file(path)
        gdf = gdf[gdf["aoi"] == aoi_key]
        if gdf.empty:
            return None
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        return gdf
    except Exception:
        return None


@st.cache_data(show_spinner=False)
def load_reservoir(aoi_key: str) -> gpd.GeoDataFrame | None:
    """S1-derived reservoir footprint polygon (derive_reservoir.py)."""
    path = STATIC_DIR / "reservoirs.geojson"
    if not path.exists():
        return None
    try:
        gdf = gpd.read_file(path)
        gdf = gdf[gdf["aoi"] == aoi_key]
        if gdf.empty:
            return None
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")
        return gdf
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
    d = OVERLAY_DIR / aoi_key / sensor
    bounds_f = d / "bounds.json"
    if not d.exists() or not bounds_f.exists():
        return None
    try:
        bounds = json.loads(bounds_f.read_text())["bounds"]
    except Exception:
        return None
    dates = sorted(p.stem for p in d.glob("*.png"))
    if not dates:
        return None
    return {"bounds": bounds, "dates": dates}


@st.cache_data(show_spinner=False)
def load_overlay_uri(aoi_key: str, sensor: str, date_str: str) -> str | None:
    """Read one overlay PNG as a base64 data URI (so it embeds straight into the
    folium map without needing a served file)."""
    png = OVERLAY_DIR / aoi_key / sensor / f"{date_str}.png"
    if not png.exists():
        return None
    import base64
    return "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()


# ─────────────────────────────────────────────
# MAP
# ─────────────────────────────────────────────

def _river_weight(ord_flow) -> float:
    """Line width from flow order (lower order = larger river = thicker).
    Gradation keeps big rivers prominent and small brooks (order 7-8) thin."""
    try:
        o = int(ord_flow)
    except (TypeError, ValueError):
        o = 6
    return min(4.0, max(0.6, (9 - o) * 0.7))


def _chaikin(coords: list, iters: int = 2) -> list:
    """Chaikin corner-cutting: smooths a polyline for display only."""
    for _ in range(iters):
        if len(coords) < 3:
            break
        new = [coords[0]]
        for i in range(len(coords) - 1):
            p, q = coords[i], coords[i + 1]
            new.append([0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]])
            new.append([0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]])
        new.append(coords[-1])
        coords = new
    return coords


def smooth_river_features(features: list[dict]) -> list[dict]:
    """Return copies of river features with Chaikin-smoothed geometry. This only
    changes how the lines are drawn; the underlying HydroRIVERS topology and flow
    order (used for the catchment filter and line width) are untouched."""
    out = []
    for f in features:
        geom = f["geometry"]
        gtype = geom["type"]
        if gtype == "LineString":
            new_geom = {"type": "LineString", "coordinates": _chaikin(geom["coordinates"])}
        elif gtype == "MultiLineString":
            new_geom = {"type": "MultiLineString",
                        "coordinates": [_chaikin(line) for line in geom["coordinates"]]}
        else:
            new_geom = geom
        out.append({"type": "Feature", "properties": f["properties"], "geometry": new_geom})
    return out


def river_label_point(features: list[dict]) -> tuple[float, float] | None:
    """A point on the main stem (longest line of the lowest flow order) to
    anchor the river-name label, so the name always sits on the actual river."""
    if not features:
        return None
    orders = [f["properties"].get("ORD_FLOW", 9) for f in features]
    min_ord = min(orders)
    best, best_len = None, -1.0
    for f in features:
        if f["properties"].get("ORD_FLOW", 9) != min_ord:
            continue
        g = shape(f["geometry"])
        if g.length > best_len:
            best, best_len = g, g.length
    if best is None:
        return None
    pt = best.interpolate(0.5, normalized=True)
    return (pt.y, pt.x)


def build_map(aoi: dict, rivers: list[dict] | None, glaciers: gpd.GeoDataFrame | None,
              reservoir: gpd.GeoDataFrame | None = None,
              catchment: gpd.GeoDataFrame | None = None) -> folium.Map:
    min_lon, min_lat, max_lon, max_lat = aoi["clip_box"]
    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2

    m = folium.Map(
        location=[center_lat, center_lon],
        tiles="CartoDB positron",
    )
    # Fit exactly to the AOI so it is always centered regardless of AOI size
    m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

    # Reservoir centre for placing the reservoir-name label on the lake itself.
    res_label_anchor = None
    if reservoir is not None and not reservoir.empty:
        c = reservoir.geometry.union_all().centroid
        res_label_anchor = (c.y, c.x)

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
        ).add_to(m)
    else:
        folium.Rectangle(
            bounds=[[min_lat, min_lon], [max_lat, max_lon]],
            color="#5d6d7e",
            weight=1.5,
            dash_array="6,6",
            fill=False,
            tooltip="Area of interest (AOI)",
        ).add_to(m)

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
        glacier_group.add_to(m)

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
        river_group.add_to(m)

        # Persistent reservoir-name label on the lake itself (e.g. Zhinvali
        # Reservoir), falling back to the main-stem midpoint if no reservoir polygon.
        anchor = res_label_anchor if res_label_anchor else river_label_point(rivers)
        name = RESERVOIR_NAME.get(aoi["key"])
        if anchor and name:
            folium.Marker(
                location=list(anchor),
                icon=folium.DivIcon(
                    icon_size=(90, 18),
                    icon_anchor=(45, 9),
                    html=(
                        '<div style="font-size:11px;font-weight:600;color:#1a5276;'
                        'background:rgba(255,255,255,0.65);border-radius:3px;'
                        'text-align:center;white-space:nowrap;font-style:italic;">'
                        f'{name}</div>'
                    ),
                ),
            ).add_to(m)

    # Reservoir footprint (S1-derived) - the actual lake polygon. The headline
    # feature, so make it pop: vivid blue fill + crisp dark outline on top of the
    # paler river/water layers.
    if reservoir is not None and not reservoir.empty:
        area = reservoir.iloc[0].get("area_km2")
        tip = f"Reservoir footprint (S1)" + (f": {area:.2f} km²" if area is not None else "")
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
            tooltip=tip,
        ).add_to(m)

    folium.LayerControl().add_to(m)
    return m


def build_overlay_map(aoi: dict, png_uri: str, bounds: list,
                      catchment: gpd.GeoDataFrame | None,
                      reservoir: gpd.GeoDataFrame | None,
                      glaciers: gpd.GeoDataFrame | None = None) -> folium.Map:
    """Light-weight map for the scene browser: basemap, catchment contour, the
    chosen pre-rendered raster, and (for HLS) the RGI glacier outlines so glaciers
    are always clearly bounded - whether currently snow-covered or bare ice."""
    m = folium.Map(tiles="CartoDB positron")
    m.fit_bounds(bounds)

    if catchment is not None and not catchment.empty:
        folium.GeoJson(
            catchment.__geo_interface__,
            style_function=lambda _: {
                "color": "#5d6d7e", "weight": 2.0, "fill": False,
            },
        ).add_to(m)

    folium.raster_layers.ImageOverlay(
        image=png_uri, bounds=bounds, opacity=0.9, zindex=10,
    ).add_to(m)

    # RGI glacier outlines (violet, no fill) so the glacier extent reads clearly
    # against the cyan snow field / over the bare-ice raster.
    if glaciers is not None and not glaciers.empty:
        folium.GeoJson(
            glaciers.__geo_interface__,
            style_function=lambda _: {
                "color": "#5e4b8b", "weight": 1.0, "fill": False, "opacity": 0.9,
            },
        ).add_to(m)

    # Thin reservoir outline on top, for orientation against the water raster.
    if reservoir is not None and not reservoir.empty:
        folium.GeoJson(
            reservoir.__geo_interface__,
            style_function=lambda _: {
                "color": "#0b3d66", "weight": 1.5, "fill": False,
            },
        ).add_to(m)
    return m


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

def chart_water(df: pd.DataFrame) -> go.Figure:
    """Water surface from DSWx-S1. SAR is cloud-independent, so the series is
    gap-free. Shows the reservoir-only footprint (reservoir_area_km2) as the
    main, level-relevant signal and the AOI-wide water (water_km2, incl. rivers)
    as a fainter reference line."""
    fig = go.Figure()

    has_res = "reservoir_area_km2" in df.columns and df["reservoir_area_km2"].notna().any()

    # AOI-wide water (includes rivers/other) - reference, drawn fainter
    fig.add_trace(go.Scatter(
        x=df["date"],
        y=df["water_km2"],
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
    if has_res:
        fig.add_trace(go.Scatter(
            x=df["date"],
            y=df["reservoir_area_km2"],
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
        margin=dict(t=40, b=20, l=60, r=20),
        xaxis=dict(showgrid=True, gridcolor="#f0f0f0"),
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0"),
    )
    return fig


def chart_snow(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()

    # Prefer the coverage/cloud-corrected seasonal snow when available, so partial
    # (swath-edge) dates are not biased low against full-coverage dates.
    seas_col = ("seasonal_snow_km2_est" if "seasonal_snow_km2_est" in df.columns
                else "seasonal_snow_km2")
    snow_cols = [seas_col, "snow_on_glacier_km2", "bare_ice_km2"]

    # Cloud gap shading (same logic, based on first snow column)
    gap_mask = df[snow_cols[0]].isna()
    in_gap = False
    gap_start = None
    for i, is_gap in enumerate(gap_mask):
        if is_gap and not in_gap:
            gap_start = df["date"].iloc[i]
            in_gap = True
        elif not is_gap and in_gap:
            fig.add_vrect(
                x0=gap_start, x1=df["date"].iloc[i],
                fillcolor="lightgray", opacity=0.3, line_width=0,
            )
            in_gap = False

    for col in snow_cols:
        fig.add_trace(go.Scatter(
            x=df["date"],
            y=df[col],
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
        margin=dict(t=40, b=20, l=60, r=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        xaxis=dict(showgrid=True, gridcolor="#f0f0f0"),
        yaxis=dict(showgrid=True, gridcolor="#f0f0f0"),
    )
    return fig


# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="From Snow to Reservoir",
    page_icon="🏔",
    layout="wide",
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
        icon="⏳",
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

def _slice(d: pd.DataFrame) -> pd.DataFrame:
    return d[(d["date"] >= pd.Timestamp(date_range[0])) &
             (d["date"] <= pd.Timestamp(date_range[1]))].copy()

df    = _slice(df_hls_full)   # HLS: snow / glacier
df_s1 = _slice(df_s1_full)    # S1: water

# ── KPI tiles ────────────────────────────────
# Water from S1; snow / glacier from HLS
latest_w   = df_s1.iloc[-1] if not df_s1.empty else None
max_water  = df_s1["water_km2"].max() if not df_s1.empty else None

latest_h    = df.iloc[-1] if not df.empty else None
# Use the coverage-corrected seasonal snow when present (comparable across dates).
_seas_col = ("seasonal_snow_km2_est" if "seasonal_snow_km2_est" in df.columns
             else "seasonal_snow_km2")
max_snow    = (df[_seas_col] + df["snow_on_glacier_km2"]).max() if not df.empty else None
latest_snow = (
    latest_h[_seas_col] + latest_h["snow_on_glacier_km2"]
    if latest_h is not None else None
)

col1, col2, col3, col4 = st.columns(4)

with col1:
    res_series = (df_s1["reservoir_area_km2"] if "reservoir_area_km2" in df_s1.columns
                  else pd.Series(dtype=float))
    has_res = res_series.notna().any()
    if has_res:
        # Current = last date with a valid lake reading; max over valid dates.
        # (False-drawdown dates are already NaN via the reservoir guard.)
        max_res    = res_series.max()
        latest_res = res_series.dropna().iloc[-1]
        st.metric(
            "Reservoir area (S1, current)",
            f"{latest_res:.2f} km²",
            delta=f"Max: {max_res:.2f} km²",
            delta_color="off",
        )
    elif latest_w is not None:
        st.metric(
            "Water area (S1, current)",
            f"{latest_w['water_km2']:.2f} km²",
            delta=f"Max: {max_water:.2f} km²",
            delta_color="off",
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
        )
    else:
        st.metric("Total snow (HLS, current)", "No data")

with col3:
    if latest_h is not None:
        st.metric("Bare glacier ice", f"{latest_h['bare_ice_km2']:.1f} km²")
    else:
        st.metric("Bare glacier ice", "No data")

with col4:
    st.metric(
        "Scenes in range",
        f"{len(df_s1)} S1 (water)",
        delta=f"{len(df)} HLS (snow)",
        delta_color="off",
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

    caps = []
    if catchment is not None:
        caps.append("Catchment (HydroBASINS)")
    if glaciers is not None:
        # Count only glaciers inside the basin, matching the catchment-clipped map.
        if catchment is not None and not catchment.empty:
            n_glac = int(glaciers.geometry.intersects(catchment.geometry.union_all()).sum())
        else:
            n_glac = len(glaciers)
        caps.append(f"{n_glac} RGI v7 glacier polygons")
    else:
        caps.append("RGI glacier data not found")
    if reservoir is not None:
        res_area = reservoir.iloc[0].get("area_km2")
        caps.append(f"Reservoir footprint (S1){f': {res_area:.2f} km²' if res_area is not None else ''}")
    else:
        caps.append("Reservoir footprint not found, run derive_reservoir.py")
    st.caption(" · ".join(caps))

    m = build_map(aoi, rivers, glaciers, reservoir, catchment)
    st_folium(m, height=430, use_container_width=True)

with chart_col:
    st.subheader("Time series")
    tab1, tab2 = st.tabs(["Water area", "Snow & ice"])

    with tab1:
        st.plotly_chart(chart_water(df_s1), width="stretch")

    with tab2:
        st.plotly_chart(chart_snow(df), width="stretch")

# ── Scene browser (pre-rendered raster overlays) ─────────
st.divider()
st.subheader("Scenes over time")

sensor_label = st.radio(
    "Dataset", list(OVERLAY_SENSORS.keys()), horizontal=True,
    help="S1 (radar, cloud independent) shows water; HLS (optical) shows snow and ice.",
)
sensor = OVERLAY_SENSORS[sensor_label]
ov = load_overlay_index(aoi["key"], sensor)

if ov is None:
    st.info(
        "No scenes have been rendered for this area and sensor yet. "
        "Run `python render_overlays.py` (it reads the GeoTIFFs from the tile "
        "store and writes coloured PNGs into static_data/overlays/)."
    )
else:
    dates = ov["dates"]
    chosen = st.select_slider(
        "Date", options=dates, value=dates[-1],
        format_func=lambda d: f"{d[6:8]}.{d[4:6]}.{d[0:4]}",
    )
    uri = load_overlay_uri(aoi["key"], sensor, chosen)
    if uri is None:
        st.warning("Scene not readable.")
    else:
        # Clip the glacier outlines to the catchment so they end exactly at the
        # basin boundary - matching the catchment-masked raster and the
        # catchment-relative statistics (glaciers outside don't feed this reservoir).
        glac_arg = None
        if sensor == "hls" and glaciers is not None:
            glac_arg = (gpd.clip(glaciers, catchment)
                        if catchment is not None and not catchment.empty
                        else glaciers)
        ov_map = build_overlay_map(
            aoi, uri, ov["bounds"], catchment, reservoir, glaciers=glac_arg,
        )
        st_folium(ov_map, height=430, use_container_width=True,
                  key=f"overlay_{aoi['key']}_{sensor}")
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
    df_hls_view = df.drop(columns=["water_area_km2"], errors="ignore")
    st.dataframe(
        df_hls_view.sort_values("date", ascending=False).reset_index(drop=True),
        width="stretch", hide_index=True,
    )
