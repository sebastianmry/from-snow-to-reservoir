"""
FROM SNOW TO RESERVOIR - RGI v7 Glacier Data Download
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Downloads the Randolph Glacier Inventory v7 Region 12 (Caucasus and Middle East)
shapefile from NSIDC via authenticated NASA Earthdata HTTPS session.

Output: static_data/RGI2000-v7.0-G-12_caucasus-middle_east.shp (+ sidecar files)

Run once before extract_timeseries.py:
    python download_glaciers.py
"""

import earthaccess
import zipfile
from pathlib import Path

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

STATIC_DIR = Path("static_data")

RGI_NSIDC_URL = (
    "https://daacdata.apps.nsidc.org/pub/DATASETS/nsidc0770_rgi_v7/"
    "regional_files/RGI2000-v7.0-G/"
    "RGI2000-v7.0-G-12_caucasus-middle_east.zip"
)
RGI_ZIP      = STATIC_DIR / "rgi_region12.zip"
RGI_SHP_GLOB = "RGI2000-v7.0-G-12_caucasus-middle_east.shp"


# ─────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────

def download_rgi() -> Path | None:
    """Download and unpack RGI v7 Region 12 from NSIDC.
    Uses NASA Earthdata credentials (same account as download_to_drive.py).
    Returns path to .shp file or None on failure.
    """
    STATIC_DIR.mkdir(exist_ok=True)

    existing = list(STATIC_DIR.rglob(RGI_SHP_GLOB))
    if existing:
        print(f"RGI shapefile already present: {existing[0]}")
        return existing[0]

    print(f"Downloading RGI v7 Region 12 from NSIDC...")
    print(f"URL: {RGI_NSIDC_URL}")

    try:
        # earthaccess.get_fsspec_https_session() handles NASA Earthdata
        # OAuth token refresh and redirects automatically
        fs = earthaccess.get_fsspec_https_session()
        with fs.open(RGI_NSIDC_URL) as f:
            data = f.read()
        with open(RGI_ZIP, "wb") as out:
            out.write(data)
        print(f"Download complete ({len(data) / 1e6:.1f} MB)")
    except Exception as e:
        print(f"ERROR downloading RGI: {e}")
        print("Alternative: download manually from https://nsidc.org/data/nsidc-0770/versions/7")
        print("             and unpack Region 12 zip into static_data/")
        return None

    print("Unpacking...")
    with zipfile.ZipFile(RGI_ZIP, "r") as zf:
        zf.extractall(STATIC_DIR)
    RGI_ZIP.unlink()

    existing = list(STATIC_DIR.rglob(RGI_SHP_GLOB))
    if existing:
        print(f"Done. Shapefile ready: {existing[0]}")
        return existing[0]

    print("ERROR: Shapefile not found after unpack.")
    return None


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 50)
    print("RGI v7 Region 12 - Glacier Data Download")
    print("=" * 50)

    print("\nNASA Earthdata Login...")
    try:
        earthaccess.login(strategy="netrc")
    except Exception:
        print("  No _netrc found - enter credentials:")
        earthaccess.login(strategy="interactive", persist=True)
    print("Login OK\n")

    shp = download_rgi()
    if shp:
        print("\nReady for extract_timeseries.py")
    else:
        print("\nFailed - glacier stats will be skipped in extract_timeseries.py")


if __name__ == "__main__":
    main()
