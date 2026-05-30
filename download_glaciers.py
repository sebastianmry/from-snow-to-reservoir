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

RGI_NSIDC_BASE = "https://daacdata.apps.nsidc.org/pub/DATASETS/nsidc0770_rgi_v7/"
RGI_FILENAME   = "RGI2000-v7.0-G-12_caucasus-middle_east.zip"

# Candidate URL paths to try in order
RGI_URL_CANDIDATES = [
    RGI_NSIDC_BASE + "regional_files/RGI2000-v7.0-G/" + RGI_FILENAME,
    RGI_NSIDC_BASE + "RGI2000-v7.0-G/" + RGI_FILENAME,
    RGI_NSIDC_BASE + "data/" + RGI_FILENAME,
    RGI_NSIDC_BASE + RGI_FILENAME,
]
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

    session = earthaccess.get_requests_https_session()

    # Browse the RGI2000-v7.0-G/ folder (glacier outlines) and list actual files
    import re as _re
    g_folder_url = RGI_NSIDC_BASE + "regional_files/RGI2000-v7.0-G/"
    print(f"  Browsing {g_folder_url}")

    working_url = None
    try:
        r = session.get(g_folder_url, timeout=30)
        # Find all zip filenames in the directory listing
        zip_files = _re.findall(r'href="([^"]+\.zip)"', r.text)
        print(f"  Zip files in folder: {len(zip_files)}")
        # Find the Region 12 file (contains "-12_" or "caucasus")
        for zf in zip_files:
            fname = zf.split("/")[-1]
            if "-12_" in fname or "caucasus" in fname.lower():
                working_url = g_folder_url + fname
                print(f"  Found Region 12: {fname}")
                break
        if not working_url:
            print("  Region 12 not found. All zip files:")
            for zf in zip_files:
                print(f"    {zf.split('/')[-1]}")
    except Exception as e:
        print(f"  Could not browse folder: {e}")

    if not working_url:
        print("\nERROR: Could not find RGI Region 12 zip on NSIDC.")
        print("Manual download: https://nsidc.org/data/nsidc-0770/versions/7")
        print("Unpack Region 12 zip into static_data/")
        return None

    try:
        response = session.get(working_url, stream=True, timeout=300)
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        downloaded = 0
        with open(RGI_ZIP, "wb") as out:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                out.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"\r  {downloaded / 1e6:.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        print(f"\nDownload complete ({downloaded / 1e6:.1f} MB)")
    except Exception as e:
        print(f"ERROR downloading: {e}")
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
