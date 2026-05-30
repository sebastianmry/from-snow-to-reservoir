# Project Context: FROM SNOW TO RESERVOIR (Live-Monitoring Georgien)

## 1. Projektziel & Geographie
Hydrologisches Monitoring von zwei Schlüssel-Regionen im Großen Kaukasus (Georgien) zur Analyse der Verbindung von Schneeschmelze, Gletscherrückgang und Talsperren-Wasserständen.

### AOI 1: Enguri (West-Georgien / Svaneti)
- Bounding Box (clip_box): (41.70, 42.55, 42.80, 43.15) [Format: min_lon, min_lat, max_lon, max_lat]
- Reservoir Center Point: (42.032, 42.753)
- Fokus: Riesiges Wassereinzugsgebiet, starke Vergletscherung rund um Mestia.

### AOI 2: Zhinvali (Ost-Georgien / Kazbegi)
- Bounding Box (clip_box): (44.30, 42.00, 45.15, 42.80)
- Reservoir Center Point: (44.771, 42.133)
- Fokus: Beinhaltet den Gergeti-Gletscher am Mount Kazbek im Norden und die Stufe zum Zhinvali-Stausee im Süden.

---

## 2. Pipeline-Architektur

### Stufe 1: `download_to_drive.py` (Datenbeschaffung)
- **Quelle:** NASA OPERA DSWx Produkte via `earthaccess`.
- **Aktueller Status:** S1 (Radar) ist temporär pausiert. Fokus liegt komplett auf HLS (Optisch).
- **Spezifikation:** Lädt nur `B01_WTR` und `B09_CLOUD`.
- **Eigenschaften:** Robustes Error-Handling (Exponential Backoff bei Verbindungsabbrüchen), Google Drive Anbindung via `pydrive2`.
- **Hardware-Schonung:** Läuft stabil auf `MAX_WORKERS = 2` zur Vermeidung von RAM-Abstürzen auf dem Laptop.
- **Resume-Logik:** Überspringt bereits im Drive existierende Dateien sauber anhand des Namens.

### Stufe 2: `extract_timeseries.py` (Wissenschaftliche Auswertung)
- **Gletscher-Integrierung:** Lädt das Randolph Glacier Inventory (RGI v7, Region 12 - Kaukasus) automatisch via Python aus dem Netz nach `static_data/`, falls nicht lokal vorhanden.
- **Qualitaetsfilter:**
  * `MIN_VALID_PCT = 80%`: Szenen mit weniger als 80% gültiger AOI-Abdeckung werden übersprungen.
  * `MAX_CLOUD_PCT = 30%`: Szenen mit mehr als 30% Wolkenbedeckung werden übersprungen.
- **Verschneidungs-Logik (Raster & Vektor):**
  * Stausee-Wasserfläche: Pixelwerte 1-5 im `B01_WTR`-Band.
  * Saisonale Schneedecke: Pixelwert 252 (Schnee) *außerhalb* der RGI-Gletscherpolygone.
  * Schneebedeckter Gletscher: Pixelwert 252 (Schnee) *innerhalb* der RGI-Gletscherpolygone.
  * Blankes Gletschereis: Gesamtfläche der RGI-Polygone abzüglich der schneebedeckten Gletscherfläche (Indikator für sommerliche Gletscherschmelze).
- **Output-Spalten:** `date, water_area_km2, seasonal_snow_km2, snow_on_glacier_km2, bare_ice_km2, glacier_total_km2, cloud_cover_percent, valid_px_pct`
- **Output-Dateien:** `enguri_timeseries.csv/.parquet`, `zhinvali_timeseries.csv/.parquet`

### Stufe 3: Visualisierung / Dashboard (Zukünftig)
- **Framework:** Streamlit.
- **Features:** Interaktive Karten (Folium/Mapbox), animierte Zeitreihen-Plots.
- **Zusatz-Feature:** Einbindung von statischen Fluss-Vektordaten (OSM/HydroSHEDS) zur optischen Hervorhebung der Zuflüsse aus den Bergen in die Stauseen.

---

## 3. Aktueller Bearbeitungsstand (Stand: 2026-05-30)

- **Stufe 1 laeuft:** HLS-Download fuer Enguri aktiv (~18% abgeschlossen, 295/1658 Dateien). S1 temporaer deaktiviert nach Laptop-Absturz.
- **Stufe 2 fertig implementiert:** Alle Qualitaetsfilter, Gletscherverschneidung und Parquet-Output sind bereit.
- **Stufe 3 (Dashboard) noch nicht begonnen.**

### Bekannte Eigenheit: Drive-Ordnerstruktur
`download_to_drive.py` legt die Ordner `hls/` und `s1/` direkt im Drive-Root an (nicht unter `DRIVE_ROOT_FOLDER_ID`). `extract_timeseries.py` sucht diese Ordner ebenfalls unter `"root"`. Beide Skripte sind konsistent - nicht aendern solange Daten vorhanden sind.

---

## 4. Technischer Stack
- `earthaccess` — NASA Granule-Suche und Download
- `rioxarray` / `rasterio` — In-Memory Rasterverarbeitung und Clipping
- `geopandas` — RGI Vektordaten laden und auf AOI zuschneiden
- `pydrive2` — Google Drive Upload/Download
- `pandas` / `pyarrow` — Parquet-Output fuer Streamlit
- `tqdm` — Fortschrittsanzeige
- Conda-Environment: `georgia-sar`
- Google Drive Root Folder ID: `1EdYn2RbULuEYj8dnPbK9Zshia6G50ssE`
