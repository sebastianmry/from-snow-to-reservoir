"""
FROM SNOW TO RESERVOIR - Download OPERA DSWx-S1 to Google Drive
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Radar-based water product (Sentinel-1). Cloud-independent, used as the
basis for a continuous reservoir water-level time series.
Downloads B01_WTR (water class).

    python download_s1.py
"""

from download_common import run

S1 = {
    "short_name":      "OPERA_L3_DSWX-S1_V1",
    "layers":          ["B01_WTR"],
    "drive_subfolder": "s1",
}

if __name__ == "__main__":
    run(S1)
