"""
FROM SNOW TO RESERVOIR - Streamlit Dashboard
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

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
from streamlit_folium import st_folium

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

STATIC_DIR = Path("static_data")

AOIS = {
    "Enguri (West-Georgien)": {
        "key": "enguri",
        "clip_box": (41.70, 42.55, 42.80, 43.15),
        "center": (42.884, 42.753),
        "dam": (42.032, 42.753),
        "dam_label": "Enguri-Staudamm (271 m)",
        "zoom": 9,
    },
    "Zhinvali (Ost-Georgien)": {
        "key": "zhinvali",
        "clip_box": (44.30, 42.00, 45.15, 42.80),
        "center": (44.725, 42.40),
        "dam": (44.771, 42.133),
        "dam_label": "Zhinvali-Staudamm",
        "zoom": 9,
    },
}

SNOW_COLORS = {
    "seasonal_snow_km2":   "#a8d8ea",
    "snow_on_glacier_km2": "#4a90d9",
    "bare_ice_km2":        "#1a3a5c",
}

SNOW_LABELS = {
    "seasonal_snow_km2":   "Saisonaler Schnee",
    "snow_on_glacier_km2": "Schnee auf Gletscher",
    "bare_ice_km2":        "Blankes Gletschereis",
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
        return gdf if not gdf.empty else None
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
# MAP
# ─────────────────────────────────────────────

def build_map(aoi: dict, rivers: list[dict] | None, glaciers: gpd.GeoDataFrame | None,
              reservoir: gpd.GeoDataFrame | None = None) -> folium.Map:
    min_lon, min_lat, max_lon, max_lat = aoi["clip_box"]
    center_lat = (min_lat + max_lat) / 2
    center_lon = (min_lon + max_lon) / 2

    m = folium.Map(
        location=[center_lat, center_lon],
        tiles="CartoDB positron",
    )
    # Fit exactly to the AOI so it is always centered regardless of AOI size
    m.fit_bounds([[min_lat, min_lon], [max_lat, max_lon]])

    # AOI bounding box
    folium.Rectangle(
        bounds=[[min_lat, min_lon], [max_lat, max_lon]],
        color="#e67e22",
        weight=2,
        fill=True,
        fill_opacity=0.04,
        tooltip="Untersuchungsgebiet (AOI)",
    ).add_to(m)

    # Glacier polygons
    if glaciers is not None:
        folium.GeoJson(
            glaciers.__geo_interface__,
            name="RGI v7 Gletscher",
            style_function=lambda _: {
                "fillColor": "#ffffff",
                "color": "#a8d8ea",
                "weight": 1,
                "fillOpacity": 0.6,
            },
            tooltip=folium.GeoJsonTooltip(fields=["glac_name"] if "glac_name" in glaciers.columns else []),
        ).add_to(m)

    # River lines - GeoJson handles both LineString and MultiLineString,
    # and works for HydroRIVERS data as well as the simplified fallback
    if rivers:
        folium.GeoJson(
            {"type": "FeatureCollection", "features": rivers},
            name="Fluesse (HydroRIVERS)",
            style_function=lambda _: {
                "color": "#2980b9",
                "weight": 2,
                "opacity": 0.8,
            },
        ).add_to(m)

    # Reservoir footprint (S1-derived) - the actual lake polygon
    if reservoir is not None and not reservoir.empty:
        area = reservoir.iloc[0].get("area_km2")
        tip = f"Stausee-Footprint (S1)" + (f": {area:.2f} km²" if area is not None else "")
        folium.GeoJson(
            reservoir.__geo_interface__,
            name="Stausee-Footprint (S1)",
            style_function=lambda _: {
                "fillColor": "#2980b9",
                "color": "#1a5276",
                "weight": 1.5,
                "fillOpacity": 0.55,
            },
            tooltip=tip,
        ).add_to(m)

    # Dam marker
    dam_lon, dam_lat = aoi["dam"]
    folium.Marker(
        location=[dam_lat, dam_lon],
        popup=folium.Popup(aoi["dam_label"], max_width=200),
        tooltip=aoi["dam_label"],
        icon=folium.Icon(color="red", icon="tint", prefix="fa"),
    ).add_to(m)

    folium.LayerControl().add_to(m)
    return m


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
        name="AOI-Wasser gesamt (inkl. Fluesse)",
        line=dict(color="#aab7c4", width=1.5, dash="dot"),
        marker=dict(size=3),
        hovertemplate="%{x|%d.%m.%Y}<br>%{y:.2f} km²<extra>AOI gesamt</extra>",
    ))

    # Reservoir-only footprint - the headline signal
    if has_res:
        fig.add_trace(go.Scatter(
            x=df["date"],
            y=df["reservoir_area_km2"],
            mode="lines+markers",
            name="Stausee-Flaeche (Footprint)",
            line=dict(color="#1a5276", width=2.5),
            marker=dict(size=4),
            hovertemplate="%{x|%d.%m.%Y}<br><b>%{y:.2f} km²</b><extra>Stausee</extra>",
        ))

    fig.update_layout(
        title="Stausee-Wasserflaeche (DSWx-S1, ~12-Tage)",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
        xaxis_title=None,
        yaxis_title="Flaeche (km²)",
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

    snow_cols = ["seasonal_snow_km2", "snow_on_glacier_km2", "bare_ice_km2"]

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
        title="Schnee- und Eiskomponenten (gestapelt)",
        xaxis_title=None,
        yaxis_title="Flaeche (km²)",
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
st.caption("Live-Monitoring von Schneeschmelze, Gletschern und Talsperren im Kaukasus (Georgien)")

# ── Sidebar ──────────────────────────────────
with st.sidebar:
    st.header("Einstellungen")
    aoi_label = st.selectbox("Einzugsgebiet", list(AOIS.keys()))
    aoi = AOIS[aoi_label]

    st.divider()
    st.caption("Automatisierte Geodatenprozessierung SoSe26\nSebastian Macherey")

# ── Load data ────────────────────────────────
# Snow / glacier come from HLS (optical), water comes from S1 (radar).
with st.spinner("Lade Zeitreihen..."):
    df_hls_full, is_mock_hls = load_timeseries(aoi["key"])
    df_s1_full,  is_mock_s1  = load_s1_timeseries(aoi["key"])

if is_mock_hls or is_mock_s1:
    st.warning(
        "Parquet-Datei(en) noch nicht vorhanden - Dashboard zeigt teils synthetische "
        "Demo-Daten. extract_timeseries.py ausfuehren fuer echte Werte.",
        icon="⏳",
    )

# Date range slider (spanning both series)
min_date = min(df_hls_full["date"].min(), df_s1_full["date"].min()).date()
max_date = max(df_hls_full["date"].max(), df_s1_full["date"].max()).date()

date_range = st.sidebar.slider(
    "Zeitraum",
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
max_snow    = (df["seasonal_snow_km2"] + df["snow_on_glacier_km2"]).max() if not df.empty else None
latest_snow = (
    latest_h["seasonal_snow_km2"] + latest_h["snow_on_glacier_km2"]
    if latest_h is not None else None
)

col1, col2, col3, col4 = st.columns(4)

with col1:
    has_res = (latest_w is not None and "reservoir_area_km2" in df_s1.columns
               and pd.notna(latest_w.get("reservoir_area_km2")))
    if has_res:
        max_res = df_s1["reservoir_area_km2"].max()
        st.metric(
            "Stausee-Flaeche (S1, aktuell)",
            f"{latest_w['reservoir_area_km2']:.2f} km²",
            delta=f"Max: {max_res:.2f} km²",
            delta_color="off",
        )
    elif latest_w is not None:
        st.metric(
            "Wasserflaeche (S1, aktuell)",
            f"{latest_w['water_km2']:.2f} km²",
            delta=f"Max: {max_water:.2f} km²",
            delta_color="off",
        )
    else:
        st.metric("Wasserflaeche (S1, aktuell)", "Keine Daten")

with col2:
    if latest_snow is not None:
        st.metric(
            "Gesamtschnee (HLS, aktuell)",
            f"{latest_snow:.0f} km²",
            delta=f"Max: {max_snow:.0f} km²",
            delta_color="off",
        )
    else:
        st.metric("Gesamtschnee (HLS, aktuell)", "Keine Daten")

with col3:
    if latest_h is not None:
        st.metric("Blankes Gletschereis", f"{latest_h['bare_ice_km2']:.1f} km²")
    else:
        st.metric("Blankes Gletschereis", "Keine Daten")

with col4:
    st.metric(
        "Szenen im Zeitraum",
        f"{len(df_s1)} S1 (Wasser)",
        delta=f"{len(df)} HLS (Schnee)",
        delta_color="off",
    )

st.divider()

# ── Map + Charts ─────────────────────────────
map_col, chart_col = st.columns([1, 1], gap="large")

with map_col:
    st.subheader("Untersuchungsgebiet")
    with st.spinner("Lade Kartendaten..."):
        rivers    = load_rivers(aoi["key"])
        glaciers  = load_glaciers(tuple(aoi["clip_box"]))
        reservoir = load_reservoir(aoi["key"])

    caps = []
    if glaciers is not None:
        caps.append(f"{len(glaciers)} RGI v7 Gletscherpolygone")
    else:
        caps.append("RGI-Gletscherdaten nicht gefunden")
    if reservoir is not None:
        res_area = reservoir.iloc[0].get("area_km2")
        caps.append(f"Stausee-Footprint (S1){f': {res_area:.2f} km²' if res_area is not None else ''}")
    else:
        caps.append("Stausee-Footprint nicht gefunden - derive_reservoir.py ausfuehren")
    st.caption(" · ".join(caps))

    m = build_map(aoi, rivers, glaciers, reservoir)
    st_folium(m, height=430, use_container_width=True)

with chart_col:
    st.subheader("Zeitreihen")
    tab1, tab2 = st.tabs(["Wasserflaeche", "Schnee & Eis"])

    with tab1:
        st.plotly_chart(chart_water(df_s1), width="stretch")

    with tab2:
        st.plotly_chart(chart_snow(df), width="stretch")

# ── Data tables (collapsible) ─────────────────
with st.expander("Rohdaten anzeigen"):
    st.caption("Wasser (DSWx-S1)")
    st.dataframe(
        df_s1.sort_values("date", ascending=False).reset_index(drop=True),
        width="stretch", hide_index=True,
    )
    st.caption("Schnee / Gletscher (DSWx-HLS)")
    st.dataframe(
        df.sort_values("date", ascending=False).reset_index(drop=True),
        width="stretch", hide_index=True,
    )
