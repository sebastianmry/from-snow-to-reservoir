"""
Extract water area time series from OPERA DSWx-S1 B01_WTR GeoTIFFs in Google Drive.

OPERA DSWx-S1 B01_WTR pixel values:
    0   = Not water
    1   = Open water
    3   = Inundated vegetation
    250 = HAND masked (Height Above Nearest Drainage)
    251 = Layover/shadow masked (SAR artefact)
    254 = Ocean masked
    255 = NoData

Note: Snow/Ice is NOT in B01_WTR for DSWx-S1 (SAR-based). Snow requires B03_BWTR layer.

Output: CSV per site with columns: date, open_water_km2, inundated_veg_km2, hand_masked_km2, layover_km2, valid_px_pct

Usage:
    python extract_timeseries.py
"""

import io
import re
import csv
from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

FOLDER_ID = "1EdYn2RbULuEYj8dnPbK9Zshia6G50ssE"
NODATA = 255

PIXEL_VALUES = {
    "not_water":        0,
    "open_water":       1,
    "inundated_veg":    3,
    "hand_masked":      250,
    "layover":          251,
    "ocean":            254,
}

OUTPUT_DIR = Path(".")


def authenticate() -> GoogleDrive:
    gauth = GoogleAuth()
    gauth.LoadCredentialsFile("gdrive_credentials.json")
    if gauth.credentials is None:
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()
    gauth.SaveCredentialsFile("gdrive_credentials.json")
    return GoogleDrive(gauth)


def list_files_in_folder(drive: GoogleDrive, folder_id: str) -> list[dict]:
    results = []
    entries = drive.ListFile(
        {"q": f"'{folder_id}' in parents and trashed=false"}
    ).GetList()
    for entry in entries:
        if entry["mimeType"] == "application/vnd.google-apps.folder":
            results.extend(list_files_in_folder(drive, entry["id"]))
        elif entry["title"].endswith(".tif"):
            results.append(entry)
    return results


def compute_areas(drive_file) -> dict | None:
    """Read TIF in-memory and compute area in km2 per class."""
    content = drive_file.GetContentIOBuffer()
    data_bytes = io.BytesIO(content.read())

    with rasterio.open(data_bytes) as src:
        arr = src.read(1)
        res_x = abs(src.transform.a)
        res_y = abs(src.transform.e)
        if src.crs and src.crs.is_geographic:
            # CRS in degrees -> convert to km using center latitude
            center_lat = (src.bounds.top + src.bounds.bottom) / 2
            lat_rad = np.radians(center_lat)
            px_km2 = (res_x * 111.32 * np.cos(lat_rad)) * (res_y * 110.574)
        else:
            # CRS in meters (e.g. UTM) -> convert m2 to km2
            px_km2 = (res_x * res_y) / 1e6

    valid_mask = arr != NODATA
    total_valid = int(np.sum(valid_mask))
    total_px = arr.size

    return {
        "open_water_km2":    float(np.sum(arr == PIXEL_VALUES["open_water"]) * px_km2),
        "inundated_veg_km2": float(np.sum(arr == PIXEL_VALUES["inundated_veg"]) * px_km2),
        "hand_masked_km2":   float(np.sum(arr == PIXEL_VALUES["hand_masked"]) * px_km2),
        "layover_km2":       float(np.sum(arr == PIXEL_VALUES["layover"]) * px_km2),
        "valid_px_pct":      round(total_valid / total_px * 100, 2),
    }


def parse_metadata(filename: str) -> tuple[str, str] | None:
    """Extract (site, date) from filename like enguri_20240822_B01_WTR_clipped.tif"""
    m = re.match(r"(enguri|zhinvali)_(\d{8})_", filename)
    if not m:
        return None
    return m.group(1), m.group(2)


def main():
    print("Authenticating with Google Drive...")
    drive = authenticate()

    print("Listing TIF files...")
    all_files = list_files_in_folder(drive, FOLDER_ID)
    print(f"Found {len(all_files)} files\n")

    sites: dict[str, list] = {"enguri": [], "zhinvali": []}

    for i, f in enumerate(all_files, 1):
        meta = parse_metadata(f["title"])
        if not meta:
            continue
        site, date_str = meta
        print(f"[{i:>3}/{len(all_files)}] {f['title']} ... ", end="", flush=True)

        try:
            areas = compute_areas(f)
            date = datetime.strptime(date_str, "%Y%m%d").date()
            row = {"date": date, **areas}
            sites[site].append(row)
            print(f"water={areas['open_water_km2']:.2f} km2  veg={areas['inundated_veg_km2']:.2f} km2")
        except Exception as e:
            print(f"ERROR: {e}")

    # Write CSVs
    fieldnames = ["date", "open_water_km2", "inundated_veg_km2", "hand_masked_km2", "layover_km2", "valid_px_pct"]

    for site, rows in sites.items():
        if not rows:
            continue
        rows.sort(key=lambda r: r["date"])
        out_path = OUTPUT_DIR / f"{site}_timeseries.csv"
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nSaved: {out_path} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
