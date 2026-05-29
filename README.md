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

- **OPERA DSWx-S1 (Level-3):** Wasserklassifikation (B01_WTR) via NASA earthaccess, ~6-Tage Revisit
- **OPERA DSWx-HLS (Level-3):** Schnee/Eis Klassifikation (B03_SNOW) aus Landsat-8/Sentinel-2
- **Copernicus DEM GLO-30:** 30 m Hoehenmodell, hypsometrische Volumenberechnung
- **Randolph Glacier Inventory v7 (RGI):** Gletscher-Polygone, Volume-Area Scaling

Zeitraum: August 2024 bis heute (live, automatisch aktualisiert)

## Pipeline

```
NASA Earthdata (earthaccess)
        |
        v
download_to_drive.py        # Clip auf AOI, Coverage-Filter >= 90%, Upload nach Google Drive
        |                   # DSWx-S1 B01_WTR (Wasser) + DSWx-HLS B03_SNOW (Schnee/Eis)
        v
extract_timeseries.py       # Flaechenberechnung pro Klasse -> CSV Zeitreihen
        |
        v
Streamlit App               # Zeitreihen, Karte, AI-Report (in Entwicklung)
```

`filter_tiffs_by_coverage.py` steht zusaetzlich fuer manuelle Nachbereinigung bestehender Drive-Ordner zur Verfuegung.

## Setup

```bash
conda create -n georgia-sar python=3.11
conda activate georgia-sar
pip install -r requirements.txt
```

Google Drive Authentifizierung (einmalig):
- `client_secrets.json` und `settings.yaml` benoetigt (nicht im Repo — siehe Google Drive API Dokumentation)

NASA Earthdata Login:
- Konto unter [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov) anlegen
- Beim ersten Start wird nach Username/Passwort gefragt (wird in `_netrc` gespeichert)

## Skripte

### `download_to_drive.py`
Laedt OPERA DSWx-S1 (B01_WTR) und DSWx-HLS (B03_SNOW) Granules direkt aus NASA, clippt auf AOI-Bboxen und laedt nach Google Drive hoch. Eingebauter Coverage-Filter (>= 90% valide Pixel) verwirft MGRS-Randkacheln direkt beim Download.

```bash
python download_to_drive.py
```

### `extract_timeseries.py`
Liest alle GeoTIFFs aus Google Drive in-memory, berechnet Flaechenanteile pro Pixelklasse (Open Water, Inundated Vegetation, etc.) und speichert Zeitreihen als CSV.

```bash
python extract_timeseries.py
```

### `filter_tiffs_by_coverage.py`
Nachtraeglicher Coverage-Filter fuer bestehende Drive-Ordner. Berechnet Anteil valider Pixel und loescht Dateien unterhalb des Schwellenwerts.

```bash
python filter_tiffs_by_coverage.py --threshold 90 --folder-id <FOLDER_ID>
# Dry-Run (keine Loeschung):
python filter_tiffs_by_coverage.py --threshold 90 --folder-id <FOLDER_ID> --dry-run
```

## Aktueller Datenstand

- **Enguri (DSWx-S1):** 104 Aufnahmen (Aug 2024 – Mai 2026), ~6-Tage Revisit, >= 99% Coverage
- **Zhinvali (DSWx-S1):** 99 Aufnahmen (Aug 2024 – Mai 2026), ~6-Tage Revisit, >= 99% Coverage
- **DSWx-HLS B03_SNOW:** Download laufend
- Coverage-Filter: >= 90% valide Pixel

## Tech Stack

Python 3.11 | earthaccess | rasterio | rioxarray | xarray | geopandas | scipy | numpy | pydrive2 | streamlit | plotly | anthropic SDK | GitHub | Streamlit Cloud

## Lizenz

MIT License
