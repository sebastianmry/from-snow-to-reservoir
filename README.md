# FROM SNOW TO RESERVOIR
### Live-Monitoring von Schneeschmelze, Gletschern und Talsperren im Kaukasus

Automatisierte Geodatenprozessierung SoSe26 | Sebastian Macherey | Matrikelnummer 112741

---

## Motivation

Georgiens Stromversorgung haengt zu etwa 80 % von der Wasserkraft ab. Saisonale Schneedecke und Gletscher des Grossen Kaukasus steuern den Zufluss in die Talsperren — der Klimawandel veraendert dieses System fundamental. Beide untersuchten Stauseen liegen unmittelbar an den von Russland kontrollierten Gebieten Abchasien und Suedossetien.

## Areas of Interest

| AOI | Bbox (min_lon, min_lat, max_lon, max_lat) | Staudamm (lon, lat) | Objekte |
|-----|------------------------------------------|---------------------|---------|
| Enguri | 41.70, 42.55, 42.80, 43.15 | 42.032, 42.753 | Enguri Dam (271 m), starke Vergletscherung rund um Mestia, grenzt an Abchasien |
| Zhinvali | 44.30, 42.00, 45.15, 42.80 | 44.771, 42.133 | Zhinvali Dam (Trinkwasser Tbilisi), Gergeti-Gletscher / Kazbek (5 047 m), grenzt an Suedossetien |

## Daten

- **OPERA DSWx-HLS (Level-3):** Wasserklassifikation (B01_WTR) + Wolkenmaske (B09_CLOUD) aus Landsat-8/9 und Sentinel-2, ~2-3 Tage Revisit
- **OPERA DSWx-S1 (Level-3):** Radar-basierte Wasserklassifikation (B01_WTR), ~6-Tage Revisit *(temporaer pausiert)*
- **Randolph Glacier Inventory v7 (RGI), Region 12:** Gletscher-Polygone fuer den Kaukasus (NSIDC, via `download_glaciers.py`)
- **HydroRIVERS v10:** Flussnetz (HydroSHEDS), gefiltert auf das Einzugsgebiet oberhalb des Staudamms (via `download_rivers.py`)

Zeitraum: August 2024 bis heute

## Pipeline

```
NASA Earthdata (earthaccess)
        |
        v
download_to_drive.py     # In-Memory Clip auf AOI, alle MGRS-Kacheln, Upload nach Google Drive
        |                # DSWx-HLS: B01_WTR (Wasser/Schnee) + B09_CLOUD (Wolken)
        v
extract_timeseries.py    # Pro Datum: alle Kacheln zu AOI-Mosaik mergen (EPSG:4326),
        |                # Raster-Vektor-Verschneidung mit RGI-Gletschern, Qualitaetsfilter
        v
app.py (Streamlit)       # Interaktive Karte (Folium) + Zeitreihen (Plotly)

Statische Geodaten (einmalig):
  download_glaciers.py   # RGI v7 Region 12 Gletscherpolygone -> static_data/
  download_rivers.py     # HydroRIVERS, Einzugsgebiet oberhalb Staudamm -> static_data/
```

### Mosaik-Ansatz

Stausee und Gletscher koennen in unterschiedlichen MGRS-Kacheln liegen (z.B. Zhinvali: Stausee im Sueden, Gletscher im Norden). Daher laedt `download_to_drive.py` **alle** Kacheln der AOI (Dateiname mit MGRS-Kachel-ID), und `extract_timeseries.py` fuegt pro Datum alle Kacheln zu einem vollstaendigen AOI-Mosaik in EPSG:4326 zusammen (auch ueber UTM-Zonengrenzen hinweg). Das garantiert volle Gebietsabdeckung und glaettet das Kachel-Rauschen.

## Qualitaetsfilter

| Filter | Schwellenwert | Begruendung |
|--------|--------------|-------------|
| AOI-Abdeckung | >= 80% valide Pixel | Verwirft Szenen ohne vollstaendige Gebietsabdeckung (relativ zur ganzen AOI dank Mosaik) |
| Bewoelkung | <= 30% Wolken im AOI | Wissenschaftlich gaengiger Grenzwert fuer optische Fernerkundung |

## Berechnete Metriken (pro Datum und AOI)

| Spalte | Beschreibung |
|--------|-------------|
| `water_area_km2` | Offene Wasserflaeche im Stausee (DSWx-Klassen 1-5) |
| `seasonal_snow_km2` | Schneebedeckung ausserhalb der RGI-Gletscherpolygone |
| `snow_on_glacier_km2` | Schneebedeckung innerhalb der RGI-Gletscherpolygone |
| `bare_ice_km2` | Blankes Gletschereis (Gletscherflaeche minus Schneebedeckung) — Schmelzindikator |
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

## Workflow

```bash
# 1. Statische Geodaten einmalig laden
python download_glaciers.py     # RGI v7 Gletscher (NSIDC, NASA-Login)
python download_rivers.py       # HydroRIVERS Einzugsgebiet (oeffentlich)

# 2. Satellitendaten herunterladen und prozessieren
python download_to_drive.py     # OPERA DSWx-HLS -> Google Drive
python extract_timeseries.py    # Mosaik + Zeitreihen -> *_timeseries.parquet

# 3. Dashboard starten
streamlit run app.py
```

`cleanup_drive.py` entfernt bei Bedarf alte HLS-Dateien ohne MGRS-Kachel-ID aus dem Drive (Dry-Run ohne Argument, `--delete` zum Anwenden).

## Skripte

| Skript | Funktion |
|--------|----------|
| `download_to_drive.py` | OPERA DSWx-HLS Granules suchen, auf AOI clippen, alle MGRS-Kacheln nach Google Drive laden (Resume-Logik) |
| `extract_timeseries.py` | Pro Datum Kachel-Mosaik bilden, mit RGI-Gletschern verschneiden, Zeitreihen als CSV + Parquet speichern |
| `download_glaciers.py` | RGI v7 Region 12 Gletscherpolygone von NSIDC laden (via earthaccess) |
| `download_rivers.py` | HydroRIVERS laden, auf Einzugsgebiet oberhalb des Staudamms filtern (Fliessnetz-Topologie), auf AOI clippen |
| `app.py` | Streamlit-Dashboard: Folium-Karte (AOI, Gletscher, Fluesse, Staudamm) + Plotly-Zeitreihen |
| `cleanup_drive.py` | Alte HLS-Dateien ohne MGRS-ID aus dem Drive entfernen |

## Tech Stack

Python 3.11 | earthaccess | rasterio | rioxarray | geopandas | shapely | pydrive2 | pandas | pyarrow | tqdm | streamlit | plotly | folium | streamlit-folium

## Lizenz

MIT License
