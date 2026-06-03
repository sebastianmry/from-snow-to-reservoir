"""
FROM SNOW TO RESERVOIR - Central AOI configuration (single source of truth)
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Every script (download_*, extract_timeseries, derive_reservoir, app) imports its
AOI definition from here, so the bounding box / clip box lives in exactly ONE
place. The clip_box is the rectangle actually sent to the satellite clip; it is
derived from the catchment (Einzugsgebiet) above each dam (see
download_catchments.py) plus a small buffer. The catchment POLYGON itself lives
in static_data/catchments.geojson and is used to mask the analysis statistics
(extract_timeseries.py) so that snow/glacier/water are counted only inside the
reservoir's drainage basin.

Per-AOI fields:
  name        - short site key, also the Drive subfolder name
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

# Parent folder in Google Drive holding the hls/ and s1/ subfolders
DRIVE_PARENT = "OPERA_DSWx"

# Canonical AOI table, keyed by site name.
AOIS = {
    "enguri": {
        "name": "enguri",
        "label": "Enguri Talsperre + Svaneti",
        "dam": (42.032, 42.753),
        # Catchment above the dam (HydroBASINS lev12, 3139 km2) + 0.02 deg buffer.
        # Replaces the old coarse box (41.70,42.55,42.80,43.15) which cut off the
        # eastern Svaneti headwaters (up to ~43.17 E).
        "clip_box": (41.8467, 42.7294, 43.1658, 43.2783),
        # Orbit phase 6. probe_coverage --sample first picked phase 7 (20240830) on
        # pure coverage (99.4%), but the second-orbit / 1-day-neighbour reservoir
        # check (probe_coverage --compare-orbit) showed phase 7 systematically
        # UNDER-reads the lake in autumn (~6.5 km2) while phases 6 (20240829) and 0
        # (20240823) both read a stable ~7.3 km2 - i.e. phase 7's dip is a geometry
        # artefact, not real (Enguri is a deep gorge, ~flat area). Phase 6 gives the
        # cleaner/more stable reservoir series with full coverage. Coverage != lake
        # measurement quality.
        "s1_anchor": "20240829",
        "display_label": "Enguri (Western Georgia)",
        "center": (42.884, 42.753),
        "dam_label": "Enguri-Staudamm (271 m)",
        "zoom": 9,
    },
    "zhinvali": {
        "name": "zhinvali",
        "label": "Zhinvali Talsperre + Gergeti",
        "dam": (44.771, 42.133),
        # Catchment above the dam (HydroBASINS lev12, 2089 km2) + 0.02 deg buffer.
        # Excludes the Kazbek/Gergeti glaciers (drain north into the Terek, not
        # the Zhinvali); old box reached to 42.80 N and wrongly included them.
        "clip_box": (44.3133, 42.0008, 45.245, 42.6283),
        "s1_anchor": "20240825",
        "display_label": "Zhinvali (Eastern Georgia)",
        "center": (44.725, 42.40),
        "dam_label": "Zhinvali-Staudamm",
        "zoom": 9,
    },
}

# bbox (used by earthaccess search + footprint pre-filter) equals the clip_box.
for _a in AOIS.values():
    _a["bbox"] = _a["clip_box"]

# Convenience views
AOI_LIST = [AOIS["enguri"], AOIS["zhinvali"]]
AOI_1 = AOIS["enguri"]
AOI_2 = AOIS["zhinvali"]
