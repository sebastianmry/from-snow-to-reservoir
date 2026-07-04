"""
FROM SNOW TO RESERVOIR - Central AOI configuration (single source of truth)
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Every script (download_*, extract_timeseries, derive_reservoir, app) imports its
AOI definition from here, so the bounding box / clip box lives in exactly ONE
place. The clip_box is the rectangle actually sent to the satellite clip; it is
derived from the catchment above each dam (see
download_catchments.py) plus a small buffer. The catchment POLYGON itself lives
in static_data/catchments.geojson and is used to mask the analysis statistics
(extract_timeseries.py) so that snow/glacier/water are counted only inside the
reservoir's drainage basin.

Per-AOI fields:
  name        - short site key, also the per-site subfolder name in the store
  label       - long descriptive label (download logs)
  dam         - (lon, lat) of the dam = catchment pour-point / reservoir outlet
  clip_box    - (min_lon, min_lat, max_lon, max_lat) box for download + clip
  s1_anchor   - one date (YYYYMMDD) of the chosen Sentinel-1 relative orbit; the
                S1 series keeps only dates sharing this orbit's 12-day phase
                (see probe_coverage.py / extract_timeseries.orbit_phase)
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
        # Catchment above the dam (HydroBASINS lev12, 3139 km2) + 0.02 deg buffer.
        # Replaces the old coarse box (41.70,42.55,42.80,43.15) which cut off the
        # eastern Svaneti headwaters (up to ~43.17 E).
        "clip_box": (41.8467, 42.7294, 43.1658, 43.2783),
        # Orbit phase 7 (anchor 20240830). Re-probed 2026-07-03 (probe_coverage.py
        # enguri --sample 5 + --compare-orbit): phase 7 fully images the catchment
        # on 49 of 50 cycles, so its AOI-wide water_km2 is essentially gap-free.
        # Phase 6 (the previous anchor 20240829) still misses the eastern Svaneti
        # headwaters on most cycles (only 21 of 51 cycles carry a valid water_km2),
        # so its basin-wide water line is full of NaN gaps. Both orbits agree on
        # the reservoir itself (full-year mean 7.23 vs 7.15 km2; autumn 7.35 vs
        # 7.27 km2), which retires the 2024 concern that phase 7 under-reads the
        # lake in autumn - that dip is gone in the reprocessed data. So phase 7 is
        # now strictly better than phase 6: gap-free catchment water AND an equally
        # good reservoir series, at the same single-orbit 12-day cadence as
        # Zhinvali (kept symmetric on purpose - no reason to combine orbits here,
        # combining only adds gappy phase-6 dates back to the water_km2 line).
        # The reservoir-decoupled gate in extract_timeseries.py still runs as
        # defense-in-depth for the rare partial-catchment date.
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
        # Catchment above the dam (HydroBASINS lev12, 2089 km2) + 0.02 deg buffer.
        # Excludes the Kazbek/Gergeti glaciers (drain north into the Terek, not
        # the Zhinvali); old box reached to 42.80 N and wrongly included them.
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
