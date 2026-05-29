# FROM SNOW TO RESERVOIR
### Live-Monitoring von Schneeschmelze, Gletschern und Talsperren im Kaukasus

Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey | Matrikelnummer 112741

---

## Motivation

Georgiens Stromversorgung haengt zu etwa 80 % von der Wasserkraft ab. Saisonale Schneedecke und Gletscher des Grossen Kaukasus steuern den Zufluss in die Talsperren — der Klimawandel veraendert dieses System fundamental. Beide untersuchten Stauseen liegen unmittelbar an den von Russland kontrollierten Gebieten Abchasien und Suedossetien.

## Areas of Interest

| AOI | Bbox (W, S, E, N) | Groesse | Objekte |
|-----|-------------------|---------|---------|
| Enguri | 41.70, 42.55, 42.70, 43.05 | ~4 500 km² | Enguri Dam (271 m), Shkhara-Gletscher (5 193 m), grenzt an Abchasien |
| Zhinvali | 44.55, 42.00, 45.10, 42.55 | ~2 750 km² | Zhinvali Dam (Trinkwasser Tbilisi), Gergeti/Kazbek (5 047 m), grenzt an Suedossetien |

## Daten

- **OPERA DSWx-S1 (Level-3):** Water Extent + Snow/Ice Klassifikation via NASA earthaccess, ~6-Tage Revisit
- **OPERA RTC-S1:** Radiometric Terrain Corrected Sentinel-1, Wet Snow Detection
- **Sentinel-2 NDWI** (Copernicus WMS): optische Validierung
- **Copernicus DEM GLO-30:** 30 m Hoehenmodell, hypsometrische Volumenberechnung
- **Randolph Glacier Inventory v7 (RGI):** Gletscher-Polygone, Volume-Area Scaling

Zeitraum: August 2024 bis heute (live, automatisch aktualisiert)

## Pipeline

```
NASA Earthdata (earthaccess)
        |
        v
download_to_drive.py        # Granules suchen, auf AOI clippen, nach Google Drive hochladen
        |
        v
filter_tiffs_by_coverage.py # Dateien mit < 50% valider Pixel entfernen
        |
        v
Streamlit App               # Zeitreihen, Karte, AI-Report (in Entwicklung)
```

## Setup

```bash
conda create -n georgia-sar python=3.11
conda activate georgia-sar
pip install -r requirements.txt
```

Google Drive Authentifizierung (einmalig):
- `client_secrets.json` und `settings.yaml` benoetigt (nicht im Repo — siehe Google Drive API Setup)

NASA Earthdata Login:
- Konto unter [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov) anlegen
- Beim ersten Start wird nach Username/Passwort gefragt (wird in `_netrc` gespeichert)

## Skripte

### `download_to_drive.py`
Laedt OPERA DSWx-S1 Granules direkt aus NASA S3, clippt auf die AOI-Bboxen und laedt die geclippten GeoTIFFs nach Google Drive hoch. Bereits vorhandene Dateien werden uebersprungen (Skip-Logik).

```bash
python download_to_drive.py
```

### `filter_tiffs_by_coverage.py`
Berechnet den Anteil valider Pixel (NoData = 255) pro Datei und loescht Dateien unterhalb eines Schwellenwerts aus Google Drive.

```bash
python filter_tiffs_by_coverage.py --threshold 50 --folder-id <FOLDER_ID>
# Dry-Run (keine Loeschung):
python filter_tiffs_by_coverage.py --threshold 50 --folder-id <FOLDER_ID> --dry-run
```

### `list_dates.py`
Listet alle verfuegbaren Aufnahmedaten pro Site aus Google Drive.

```bash
python list_dates.py
```

## Aktueller Datenstand

- **Enguri:** 110 Aufnahmen (Aug 2024 – Mai 2026), ~6-Tage Revisit
- **Zhinvali:** 101 Aufnahmen (Aug 2024 – Mai 2026), ~6-Tage Revisit
- Coverage-Filter: >= 50% valide Pixel (entfernt MGRS-Randkacheln)

## Tech Stack

Python 3.11 | earthaccess | rasterio | rioxarray | xarray | geopandas | scipy | numpy | pydrive2 | streamlit | plotly | anthropic SDK | GitHub | Streamlit Cloud

## Lizenz

MIT License
