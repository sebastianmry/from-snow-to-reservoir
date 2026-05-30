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
    print(f"URL: {RGI_NSIDC_URL}")

    session = earthaccess.get_requests_https_session()

    # Try candidate URLs until one works
    working_url = None
    for url in RGI_URL_CANDIDATES:
        print(f"  Trying: {url}")
        try:
            r = session.head(url, timeout=30, allow_redirects=True)
            if r.status_code == 200:
                working_url = url
                print(f"  Found!")
                break
            else:
                print(f"  -> {r.status_code}")
        except Exception as e:
            print(f"  -> ERROR: {e}")

    if not working_url:
        # Browse base directory to find correct structure
        print(f"\n  Browsing NSIDC directory to find correct path...")
        try:
            r = session.get(RGI_NSIDC_BASE, timeout=30)
            print(f"  Directory listing ({r.status_code}):")
            for line in r.text.splitlines():
                if "RGI" in line or "rgi" in line or "href" in line.lower():
                    print(f"    {line.strip()}")
        except Exception as e:
            print(f"  Could not browse directory: {e}")
        print("\nERROR: Could not find RGI zip. Check directory listing above.")
        print("Manual download: https://nsidc.org/data/nsidc-0770/versions/7")
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
