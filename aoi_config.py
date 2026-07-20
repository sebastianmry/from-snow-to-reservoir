"""
Every script (download_*, extract_timeseries, derive_reservoir, app) imports its
AOI definition from here, so the bounding box / clip box lives in exactly one
place. The clip_box is the rectangle sent to the satellite clip, derived from
the catchment above each dam (download_catchments.py) plus a small buffer. The
catchment polygon itself lives in static_data/catchments.geojson and masks the
analysis statistics in extract_timeseries.py, so snow/glacier/water are
counted only inside the drainage basin.

Per-AOI fields:
  name        - short site key, also the per-site subfolder name in the store
  label       - long descriptive label (download logs)
  dam         - (lon, lat) of the dam = catchment pour-point / reservoir outlet
  clip_box    - (min_lon, min_lat, max_lon, max_lat) box for download + clip
  s1_anchor   - one date (YYYYMMDD) fixing the Sentinel-1 orbit; only dates on
                its 12-day phase are kept (see probe_coverage.py)
  display_label, center, dam_label, zoom - dashboard presentation (app.py)
"""

from pathlib import Path

STATIC_DIR = Path("static_data")
CATCHMENTS_GEOJSON = STATIC_DIR / "catchments.geojson"

# Top-level folder in the tile store holding the hls/ and s1/ subfolders
DATA_ROOT = "OPERA_DSWx"

# Canonical AOI table, keyed by site name.
AOIS = {
    "enguri": {
        "name": "enguri",
        "label": "Enguri Reservoir + Svaneti",
        "dam": (42.032, 42.753),
        # Catchment above the dam (HydroBASINS lev12, 3139 km2) + 0.02 deg buffer,
        # sized to reach the eastern Svaneti headwaters (up to ~43.17 E).
        "clip_box": (41.8467, 42.7294, 43.1658, 43.2783),
        # Orbit phase 7 images the catchment on 50 of 51 cycles (probe_coverage.py
        # enguri --sample 5 + --compare-orbit), so its AOI-wide water_km2 is
        # essentially gap-free, at the same single-orbit 12-day cadence as
        # Zhinvali. The reservoir-decoupled gate in extract_timeseries.py still
        # runs as defense-in-depth for the rare partial-catchment date.
        "s1_anchor": "20240830",
        "display_label": "Enguri (Western Georgia)",
        "center": (42.884, 42.753),
        "dam_label": "Enguri Dam (271 m)",
        "zoom": 9,
    },
    "zhinvali": {
        "name": "zhinvali",
        "label": "Zhinvali Reservoir + Gergeti",
        "dam": (44.771, 42.133),
        # Catchment above the dam (HydroBASINS lev12, 2089 km2) + 0.02 deg buffer,
        # excluding the Kazbek/Gergeti glaciers (drain north into the Terek, not
        # the Zhinvali).
        "clip_box": (44.3133, 42.0008, 45.245, 42.6283),
        "s1_anchor": "20240825",
        "display_label": "Zhinvali (Eastern Georgia)",
        "center": (44.725, 42.40),
        "dam_label": "Zhinvali Dam",
        "zoom": 9,
    },
}

# bbox (used by earthaccess search + footprint pre-filter) equals the clip_box.
for aoi in AOIS.values():
    aoi["bbox"] = aoi["clip_box"]

# Convenience views
AOI_LIST = [AOIS["enguri"], AOIS["zhinvali"]]
AOI_1 = AOIS["enguri"]
AOI_2 = AOIS["zhinvali"]
