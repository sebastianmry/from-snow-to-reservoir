"""
FROM SNOW TO RESERVOIR - Drive Cleanup (legacy HLS tiles)
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

After the mosaic refactor, HLS filenames carry an MGRS tile id:
    {aoi}_{date}_{TILE}_{layer}_clipped.tif   (e.g. enguri_20251205_T37TGH_B01_WTR_clipped.tif)

Older files lack the tile id:
    {aoi}_{date}_{layer}_clipped.tif          (e.g. enguri_20251205_B01_WTR_clipped.tif)

This script trashes ONLY the legacy (non-MGRS) HLS files so a clean
re-download leaves the folder with consistent, MGRS-tagged tiles.
New MGRS-tagged files are kept, so it is safe to run repeatedly.

Run before re-downloading:
    python cleanup_drive.py            # dry-run, lists what would be deleted
    python cleanup_drive.py --delete   # actually trashes the legacy files
"""

import re
import sys

from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

# Legacy = NO MGRS tile id between date and layer.
# Matches enguri_20251205_B01_WTR_clipped.tif but NOT
#         enguri_20251205_T37TGH_B01_WTR_clipped.tif
LEGACY_PATTERN = re.compile(
    r"^(enguri|zhinvali)_\d{8}_(B\d+[\w\-]+?)_clipped\.tif$"
)

SITES = ["enguri", "zhinvali"]


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


def get_folder_id(drive: GoogleDrive, name: str, parent_id: str) -> str | None:
    q = (
        f"title='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    r = drive.ListFile({"q": q}).GetList()
    return r[0]["id"] if r else None


def list_all_files(drive: GoogleDrive, folder_id: str) -> list:
    """All files in a folder, paginated (Drive returns max 100 per page)."""
    out = []
    for page in drive.ListFile({
        "q": f"'{folder_id}' in parents and trashed=false",
        "maxResults": 1000,
    }):
        out.extend(page)
    return out


def main():
    do_delete = "--delete" in sys.argv

    print("=" * 55)
    print("Drive Cleanup - legacy (non-MGRS) HLS tiles")
    print("DELETE mode" if do_delete else "DRY-RUN (use --delete to apply)")
    print("=" * 55)

    drive = authenticate()
    hls_root = get_folder_id(drive, "hls", "root")
    if not hls_root:
        print("No hls/ folder found - nothing to do.")
        return

    total_legacy = 0
    for site in SITES:
        folder = get_folder_id(drive, site, hls_root)
        if not folder:
            print(f"  {site}: folder not found, skipping")
            continue

        files = list_all_files(drive, folder)
        legacy = [f for f in files if LEGACY_PATTERN.match(f["title"])]
        kept = len(files) - len(legacy)
        print(f"\n  {site}: {len(files)} files total, "
              f"{len(legacy)} legacy, {kept} MGRS-tagged (kept)")

        for f in legacy:
            if do_delete:
                f.Trash()
                print(f"    trashed: {f['title']}")
            else:
                print(f"    would trash: {f['title']}")
        total_legacy += len(legacy)

    print("\n" + "=" * 55)
    if do_delete:
        print(f"Done. {total_legacy} legacy files moved to trash.")
    else:
        print(f"{total_legacy} legacy files would be trashed. "
              f"Re-run with --delete to apply.")


if __name__ == "__main__":
    main()
