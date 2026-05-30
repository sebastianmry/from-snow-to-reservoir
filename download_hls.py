"""
FROM SNOW TO RESERVOIR - Download OPERA DSWx-HLS to Google Drive
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Optical water + snow/ice product (Landsat-8/9 + Sentinel-2).
Downloads B01_WTR (water/snow class) and B09_CLOUD (cloud mask).

    python download_hls.py
"""

from download_common import run

HLS = {
    "short_name":      "OPERA_L3_DSWX-HLS_V1",
    "layers":          ["B01_WTR", "B09_CLOUD"],
    "drive_subfolder": "hls",
}

if __name__ == "__main__":
    run(HLS)
