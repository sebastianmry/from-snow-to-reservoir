# FROM SNOW TO RESERVOIR
### Live-Monitoring von Schneeschmelze, Gletschern und Talsperren im Kaukasus


---

## Motivation

Georgiens Stromversorgung haengt zu etwa 80 % von der Wasserkraft ab. Saisonale Schneedecke und Gletscher des Grossen Kaukasus steuern den Zufluss in die Talsperren — der Klimawandel veraendert dieses System fundamental. Beide untersuchten Stauseen liegen unmittelbar an den von Russland kontrollierten Gebieten Abchasien und Suedossetien.

## Areas of Interest

| AOI | Bbox (min_lon, min_lat, max_lon, max_lat) | Objekte |
|-----|------------------------------------------|---------|
| Enguri | 41.70, 42.55, 42.80, 43.15 | Enguri Dam (271 m), starke Vergletscherung rund um Mestia, grenzt an Abchasien |
| Zhinvali | 44.30, 42.00, 45.15, 42.80 | Zhinvali Dam (Trinkwasser Tbilisi), Gergeti-Gletscher / Kazbek (5 047 m), grenzt an Suedossetien |

## Daten

- **OPERA DSWx-HLS (Level-3):** Wasserklassifikation (B01_WTR) + Wolkenmaske (B09_CLOUD) aus Landsat-8/9 und Sentinel-2, ~2-3 Tage Revisit
- **OPERA DSWx-S1 (Level-3):** Radar-basierte Wasserklassifikation (B01_WTR), ~6-Tage Revisit *(temporaer pausiert)*
- **Randolph Glacier Inventory v7 (RGI), Region 12:** Gletscher-Polygone fuer den Kaukasus (automatischer Download)

Zeitraum: August 2024 bis heute

## Pipeline

```
NASA Earthdata (earthaccess)
        |
        v
download_to_drive.py     # In-Memory Clip auf AOI, Resume-Logik, Upload nach Google Drive
        |                # DSWx-HLS: B01_WTR (Wasser/Schnee) + B09_CLOUD (Wolken)
        v
extract_timeseries.py    # Raster-Vektor-Verschneidung mit RGI-Gletscherpolygonen
        |                # Qualitaetsfilter: >= 80% AOI-Abdeckung, <= 30% Bewoelkung
        v
Streamlit App            # Zeitreihen, Karte, Gletscherstatistiken (in Entwicklung)
```

## Qualitaetsfilter

| Filter | Schwellenwert | Begruendung |
|--------|--------------|-------------|
| AOI-Abdeckung | >= 80% valide Pixel | Verwirft MGRS-Randkacheln ohne vollstaendige Gebietsabdeckung |
| Bewoelkung | <= 30% Wolken im AOI | Wissenschaftlich gaengiger Grenzwert fuer optische Fernerkundung |

## Berechnete Metriken (pro Datum und AOI)

| Spalte | Beschreibung |
|--------|-------------|
| `water_area_km2` | Offene Wasserflaeche im Stausee (DSWx-Klassen 1-5) |
| `seasonal_snow_km2` | Schneebedeckung ausserhalb der RGI-Gletscherpolygone |
| `snow_on_glacier_km2` | Schneebedeckung innerhalb der RGI-Gletscherpolygone |
| `bare_ice_km2` | Blankes Gletschereis (Gletscherflaeche minus Schneebdeckung) |
| `glacier_total_km2` | Gesamtflaeche der RGI-Polygone im AOI |
| `cloud_cover_percent` | Anteil bewoelkter Pixel im AOI |
| `valid_px_pct` | Anteil valider (nicht-NoData) Pixel im AOI |

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
Sucht OPERA DSWx-HLS Granules via NASA earthaccess, clippt sie in-memory auf die AOI-Bboxen und laedt sie nach Google Drive hoch. Resume-Logik ueberspringt bereits vorhandene Dateien anhand des Dateinamens.

```bash
cd "Pfad/zum/Projektordner"
python download_to_drive.py
```

### `extract_timeseries.py`
Laedt RGI v7 Gletscherdaten automatisch herunter (Zenodo), liest alle GeoTIFFs aus Google Drive in-memory, verschneidet Raster- mit Vektordaten und speichert Zeitreihen pro AOI.

```bash
python extract_timeseries.py
```

Output: `enguri_timeseries.csv/.parquet`, `zhinvali_timeseries.csv/.parquet`

## Aktueller Datenstand (Mai 2026)

- **DSWx-HLS Enguri:** Download laufend (~860 Granules, Aug 2024 - Mai 2026)
- **DSWx-HLS Zhinvali:** Download ausstehend
- **DSWx-S1:** Temporaer deaktiviert, wird nach Abschluss des HLS-Downloads fortgesetzt

## Tech Stack

Python 3.11 | earthaccess | rasterio | rioxarray | geopandas | pydrive2 | pandas | pyarrow | tqdm | streamlit | plotly | GitHub

## Lizenz

MIT License
