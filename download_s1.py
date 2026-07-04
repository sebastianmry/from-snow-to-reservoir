"""
FROM SNOW TO RESERVOIR - Download OPERA DSWx-S1 to the tile store
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Radar-based water product (Sentinel-1). Cloud-independent, used as the
basis for a continuous reservoir water-level time series.
Downloads B01_WTR (water class).

    python download_s1.py
"""

from download_common import run

S1 = {
    "short_name":      "OPERA_L3_DSWX-S1_V1",
    "layers":          ["B01_WTR"],
    "subfolder":       "s1",
    # Download only the AOI's anchored relative orbit (one 12-day phase), so we
    # pull ~1/4 of the dates instead of every orbit. The same orbit the series
    # is anchored to in extract_timeseries.py (aoi_config.s1_anchor).
    "orbit_filter":    True,
}

if __name__ == "__main__":
    run(S1)
