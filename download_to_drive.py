"""
FROM SNOW TO RESERVOIR – Direct Cloud Download → Google Drive
Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey

Ablauf:
  1. earthaccess öffnet OPERA DSWx-S1 Granules direkt aus NASA S3 (kein Full-Download)
  2. rioxarray clippt auf AOI-Bbox → nur relevante Pixel
  3. Clipped GeoTIFF wird direkt auf Google Drive hochgeladen

Voraussetzungen:
    pip install earthaccess rioxarray rasterio pydrive2 tqdm python-dotenv

Google Drive Setup (einmalig):
    → Siehe setup_google_drive.md
"""

import io
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import earthaccess
import numpy as np
import rioxarray as rxr
from dotenv import load_dotenv
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from tqdm import tqdm

load_dotenv()

# ─────────────────────────────────────────────
# KONFIGURATION
# ─────────────────────────────────────────────

AOI_1 = {
    "name": "enguri",
    "label": "Enguri Talsperre + Svaneti",
    # (West, Süd, Ost, Nord) für earthaccess search
    "bbox": (41.70, 42.55, 42.70, 43.05),
    # (min_lon, min_lat, max_lon, max_lat) für rioxarray clip
    "clip_box": (41.70, 42.55, 42.70, 43.05),
}

AOI_2 = {
    "name": "zhinvali",
    "label": "Zhinvali Talsperre + Gergeti",
    "bbox": (44.55, 42.00, 45.10, 42.55),
    "clip_box": (44.55, 42.00, 45.10, 42.55),
}

DATE_START = "2024-08-01"
DATE_END   = datetime.today().strftime("%Y-%m-%d")

COLLECTION  = "OPERA_L3_DSWX-S1_V1"

# Welche DSWx-S1 Layer herunterladen?
# B01_WTR  = Water Classification (Open Water, Partial, etc.)
# B03_SNOW = Snow/Ice Classification  ← wichtigster Layer für uns
# B01_WTR enthält alle Klassen inkl. Snow/Ice (Wert 252) und Open Water (Wert 1)
# B03_CONF optional für Qualitätsfilterung
LAYERS_TO_KEEP = ["B01_WTR", "B03_CONF"]

# Google Drive Ordner-Name (wird angelegt falls nicht vorhanden)
DRIVE_ROOT_FOLDER = "opera_dswx_s1"


# ─────────────────────────────────────────────
# GOOGLE DRIVE AUTH
# ─────────────────────────────────────────────

def get_drive() -> GoogleDrive:
    """Authentifiziert mit Google Drive. Beim ersten Mal öffnet sich ein Browser-Fenster."""
    gauth = GoogleAuth()
    # Saved credentials laden (nach erstem Login automatisch)
    gauth.LoadCredentialsFile("gdrive_credentials.json")

    if gauth.credentials is None:
        # Erster Start: Browser-Auth
        gauth.LocalWebserverAuth()
    elif gauth.access_token_expired:
        gauth.Refresh()
    else:
        gauth.Authorize()

    gauth.SaveCredentialsFile("gdrive_credentials.json")
    return GoogleDrive(gauth)


def get_or_create_folder(drive: GoogleDrive, name: str, parent_id: str = "root") -> str:
    """Gibt Folder-ID zurück, legt Ordner an falls er nicht existiert."""
    query = (
        f"title='{name}' and mimeType='application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed=false"
    )
    results = drive.ListFile({"q": query}).GetList()
    if results:
        return results[0]["id"]

    folder = drive.CreateFile({
        "title": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [{"id": parent_id}],
    })
    folder.Upload()
    return folder["id"]


def file_exists_in_drive(drive: GoogleDrive, filename: str, folder_id: str) -> bool:
    """Prüft ob Datei bereits in Drive existiert (Skip-Logik)."""
    query = f"title='{filename}' and '{folder_id}' in parents and trashed=false"
    return len(drive.ListFile({"q": query}).GetList()) > 0


def upload_bytes_to_drive(
    drive: GoogleDrive,
    data: bytes,
    filename: str,
    folder_id: str,
    mime: str = "image/tiff",
) -> str:
    """Lädt Bytes-Objekt als Datei in Google Drive hoch. Gibt File-ID zurück."""
    f = drive.CreateFile({
        "title": filename,
        "parents": [{"id": folder_id}],
        "mimeType": mime,
    })
    f.content = io.BytesIO(data)
    f.Upload()
    return f["id"]


# ─────────────────────────────────────────────
# DATEN-VERARBEITUNG
# ─────────────────────────────────────────────

def extract_date_from_filename(filename: str) -> str:
    """Extrahiert YYYYMMDD aus OPERA Dateinamen."""
    m = re.search(r"_(\d{8})T", filename)
    return m.group(1) if m else "unknown"


def extract_layer_from_filename(filename: str) -> str:
    """Extrahiert Layer-Kürzel z.B. 'B01_WTR' aus Dateinamen."""
    for layer in ["B01_WTR", "B02_BWTR", "B03_SNOW", "B04_INUN", "B05_CONF", "B09_Q"]:
        if layer in filename:
            return layer
    return "UNKNOWN"


def process_granule(fs, url: str, clip_box: tuple) -> bytes | None:
    """
    Öffnet einen GeoTIFF direkt aus S3 via fsspec,
    clippt auf clip_box (min_lon, min_lat, max_lon, max_lat),
    gibt geclippte Bytes zurück oder None bei Fehler.
    """
    try:
        with fs.open(url) as f:
            da = rxr.open_rasterio(f, masked=True)

        # Clip auf AOI
        clipped = da.rio.clip_box(
            minx=clip_box[0],
            miny=clip_box[1],
            maxx=clip_box[2],
            maxy=clip_box[3],
            crs="EPSG:4326",
        )

        if clipped.size == 0:
            return None

        # In-Memory als GeoTIFF exportieren
        buf = io.BytesIO()
        clipped.rio.to_raster(buf, driver="GTiff", compress="deflate")
        return buf.getvalue()

    except Exception as e:
        print(f"   ERROR: {Path(url).name}: {e}")
        return None


# ─────────────────────────────────────────────
# HAUPT-PIPELINE
# ─────────────────────────────────────────────

def process_aoi(aoi: dict, drive: GoogleDrive, root_folder_id: str):
    """Komplett-Pipeline für ein AOI: Suche → Clip → Drive Upload."""

    print(f"\n{'='*60}")
    print(f"AOI: {aoi['label']}")
    print(f"{'='*60}")

    # Drive-Ordner anlegen: opera_dswx_s1 / enguri /
    aoi_folder_id = get_or_create_folder(drive, aoi["name"], root_folder_id)

    # 1. Granules suchen
    print(f"Suche Granules {DATE_START} -> {DATE_END}...")
    granules = earthaccess.search_data(
        short_name=COLLECTION,
        bounding_box=aoi["bbox"],
        temporal=(DATE_START, DATE_END),
        count=-1,
    )
    print(f"   → {len(granules)} Granules gefunden")

    if not granules:
        print("   Keine Daten verfügbar.")
        return

    # 2. Direct S3 Filesystem öffnen
    fs = earthaccess.get_fsspec_https_session()  # funktioniert ohne AWS-Account

    # Alle Asset-URLs der gewünschten Layer sammeln
    urls_to_process = []
    for granule in granules:
        for link in granule.data_links():
            fname = Path(link).name
            layer = extract_layer_from_filename(fname)
            if layer in LAYERS_TO_KEEP:
                urls_to_process.append((link, fname, layer))

    print(f"   → {len(urls_to_process)} Layer-Files zu verarbeiten ({', '.join(LAYERS_TO_KEEP)})")

    # 3. Verarbeiten + hochladen
    uploaded = 0
    skipped  = 0

    for url, fname, layer in tqdm(urls_to_process, desc=f"  {aoi['name']}"):
        date_str  = extract_date_from_filename(fname)
        out_name  = f"{aoi['name']}_{date_str}_{layer}_clipped.tif"

        # Skip wenn schon in Drive
        if file_exists_in_drive(drive, out_name, aoi_folder_id):
            skipped += 1
            continue

        # Clip
        data = process_granule(fs, url, aoi["clip_box"])
        if data is None:
            continue

        # Upload
        upload_bytes_to_drive(drive, data, out_name, aoi_folder_id)
        uploaded += 1

    print(f"\n{aoi['name']}: {uploaded} hochgeladen, {skipped} uebersprungen (bereits in Drive)")


def main():
    print("=" * 60)
    print("FROM SNOW TO RESERVOIR – Download → Google Drive")
    print(f"Zeitraum: {DATE_START} → {DATE_END}")
    print(f"Layer: {', '.join(LAYERS_TO_KEEP)}")
    print("=" * 60)

    # NASA Login
    print("\nNASA Earthdata Login...")
    try:
        earthaccess.login(strategy="netrc")
    except Exception:
        print("   Kein _netrc gefunden - bitte Username/Passwort eingeben:")
        earthaccess.login(strategy="interactive", persist=True)
    print("NASA Login OK")

    print("\nGoogle Drive Login...")
    drive = get_drive()
    print("Google Drive OK")

    root_id = get_or_create_folder(drive, DRIVE_ROOT_FOLDER)
    print(f"Drive-Ordner: {DRIVE_ROOT_FOLDER} (ID: {root_id})")

    # AOIs verarbeiten
    process_aoi(AOI_1, drive, root_id)
    process_aoi(AOI_2, drive, root_id)

    print("\nFertig. Daten liegen in Google Drive unter:", DRIVE_ROOT_FOLDER)


if __name__ == "__main__":
    main()
