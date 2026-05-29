"""
Filter OPERA DSWx-S1 GeoTIFFs in Google Drive by valid pixel coverage.

Usage:
    python filter_tiffs_by_coverage.py --threshold 20 [--dry-run] [--folder-id FOLDER_ID]

Arguments:
    --threshold   Minimum % of valid pixels (non-255) required to keep a file (default: 20)
    --dry-run     Report only, do not delete anything
    --folder-id   Google Drive folder ID to search (if omitted, searches all accessible files)
"""

import argparse
import io
import sys
from collections import defaultdict

import numpy as np
import rasterio
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive


NODATA = 255
FILE_PATTERNS = ("enguri_", "zhinvali_")


def authenticate() -> GoogleDrive:
    gauth = GoogleAuth()
    gauth.LocalWebserverAuth()
    return GoogleDrive(gauth)


def list_files_in_folder(drive: GoogleDrive, folder_id: str) -> list[dict]:
    """Recursively list all .tif files under folder_id."""
    results = []

    entries = drive.ListFile(
        {"q": f"'{folder_id}' in parents and trashed=false"}
    ).GetList()

    for entry in entries:
        if entry["mimeType"] == "application/vnd.google-apps.folder":
            # Recurse into subfolder
            print(f"  Entering subfolder: {entry['title']}")
            results.extend(list_files_in_folder(drive, entry["id"]))
        elif entry["title"].endswith(".tif") or entry["title"].endswith(".tiff"):
            results.append(entry)

    return results


def list_tiff_files(drive: GoogleDrive, folder_id: str | None) -> list[dict]:
    if folder_id:
        all_tifs = list_files_in_folder(drive, folder_id)
    else:
        file_list = drive.ListFile(
            {"q": "trashed=false and (title contains '.tif')"}
        ).GetList()
        all_tifs = file_list

    filtered = [
        f for f in all_tifs
        if any(pat in f["title"] for pat in FILE_PATTERNS)
    ]
    return filtered


def compute_coverage(drive_file) -> float:
    """Download file in-memory and return fraction of valid (non-NODATA) pixels."""
    content = drive_file.GetContentIOBuffer()
    data_bytes = io.BytesIO(content.read())

    with rasterio.open(data_bytes) as src:
        arr = src.read(1)  # Band 1 (WTR layer)

    total_pixels = arr.size
    if total_pixels == 0:
        return 0.0

    valid_pixels = int(np.sum(arr != NODATA))
    return valid_pixels / total_pixels * 100.0


def parse_site(filename: str) -> str:
    for pat in FILE_PATTERNS:
        if pat in filename:
            return pat.rstrip("_")
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description="Filter GeoTIFFs by valid pixel coverage")
    parser.add_argument("--threshold", type=float, default=20.0,
                        help="Minimum %% valid pixels to keep (default: 20)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only, do not delete files")
    parser.add_argument("--folder-id", type=str, default=None,
                        help="Google Drive folder ID to search")
    args = parser.parse_args()

    threshold = args.threshold
    dry_run = args.dry_run

    print(f"Configuration:")
    print(f"  Coverage threshold : {threshold:.1f}%")
    print(f"  Mode               : {'DRY RUN (no deletions)' if dry_run else 'LIVE (will delete)'}")
    print(f"  Folder ID          : {args.folder_id or 'all accessible'}")
    print()

    print("Authenticating with Google Drive...")
    drive = authenticate()

    print("Listing TIFF files...")
    files = list_tiff_files(drive, args.folder_id)
    print(f"Found {len(files)} matching file(s)\n")

    if not files:
        print("No files found. Check folder ID and file naming.")
        sys.exit(0)

    results = []  # (filename, file_id, coverage_pct, keep)
    coverages_by_site: dict[str, list[float]] = defaultdict(list)

    for i, f in enumerate(files, 1):
        name = f["title"]
        site = parse_site(name)
        print(f"[{i:>3}/{len(files)}] {name} ... ", end="", flush=True)
        try:
            coverage = compute_coverage(f)
            keep = coverage >= threshold
            status = "KEEP" if keep else "DELETE"
            print(f"{coverage:6.2f}%  → {status}")
            results.append((name, f["id"], coverage, keep))
            coverages_by_site[site].append(coverage)
        except Exception as exc:
            print(f"ERROR: {exc}")
            results.append((name, f["id"], None, True))  # keep on error

    # Deletion
    deleted = 0
    kept = 0
    errors = 0

    print()
    if not dry_run:
        print("Deleting files below threshold...")
        for name, file_id, coverage, keep in results:
            if not keep and coverage is not None:
                try:
                    file_obj = drive.CreateFile({"id": file_id})
                    file_obj.Trash()  # Move to trash (recoverable); use .Delete() for permanent
                    deleted += 1
                    print(f"  Trashed: {name}")
                except Exception as exc:
                    print(f"  ERROR trashing {name}: {exc}")
                    errors += 1
            elif keep:
                kept += 1
    else:
        for _, _, coverage, keep in results:
            if keep:
                kept += 1
            elif coverage is not None:
                deleted += 1

    # Report
    print()
    print("=" * 60)
    print("REPORT")
    print("=" * 60)
    print(f"  Total files examined : {len(results)}")
    print(f"  Kept (>= {threshold:.1f}%)      : {kept}")
    print(f"  {'Would delete' if dry_run else 'Trashed'} (< {threshold:.1f}%)  : {deleted}")
    if errors:
        print(f"  Errors               : {errors}")
    print()

    for site, covs in sorted(coverages_by_site.items()):
        covs_arr = np.array(covs)
        below = int(np.sum(covs_arr < threshold))
        print(f"  Site: {site}")
        print(f"    Files         : {len(covs)}")
        print(f"    Min coverage  : {covs_arr.min():.2f}%")
        print(f"    Max coverage  : {covs_arr.max():.2f}%")
        print(f"    Mean coverage : {covs_arr.mean():.2f}%")
        print(f"    Median        : {np.median(covs_arr):.2f}%")
        print(f"    Below threshold: {below} ({below/len(covs)*100:.1f}%)")

        # Histogram buckets
        buckets = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        counts, _ = np.histogram(covs_arr, bins=buckets)
        print(f"    Coverage distribution:")
        for lo, hi, cnt in zip(buckets[:-1], buckets[1:], counts):
            bar = "#" * cnt
            print(f"      {lo:3d}-{hi:3d}%: {bar} ({cnt})")
        print()

    if dry_run:
        print("DRY RUN complete — no files were modified.")
    else:
        print("Done. Files moved to Google Drive Trash (recoverable for 30 days).")
        print("To permanently delete, replace .Trash() with .Delete() in the script.")


if __name__ == "__main__":
    main()
