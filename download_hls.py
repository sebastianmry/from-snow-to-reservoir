"""
FROM SNOW TO RESERVOIR - Download OPERA DSWx-HLS to the tile store
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Optical water + snow/ice product (Landsat-8/9 + Sentinel-2).
Downloads B01_WTR only; cloud masking uses the WTR layer's own 253 flag,
so the separate B09_CLOUD layer is not needed.

    python download_hls.py
"""

from download_common import run

HLS = {
    "short_name":      "OPERA_L3_DSWX-HLS_V1",
    "layers":          ["B01_WTR"],
    "subfolder":       "hls",
}

if __name__ == "__main__":
    run(HLS)
