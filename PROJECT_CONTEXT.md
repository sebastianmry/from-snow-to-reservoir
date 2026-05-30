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

### Stufe 1: `download_hls.py` / `download_s1.py` (Datenbeschaffung)
- **Aufteilung:** Zwei schlanke Start-Skripte (HLS optisch, S1 Radar); gemeinsame Logik in `download_common.py` (nie direkt ausgefuehrt).
- **Quelle:** NASA OPERA DSWx Produkte via `earthaccess`. HLS laedt `B01_WTR` + `B09_CLOUD`, S1 laedt `B01_WTR`.
- **Footprint-Vorfilter:** Pro Datum wird die Vereinigung der Kachel-Footprints gegen das AOI geprueft; nur Tage mit >= 99% AOI-Abdeckung werden ueberhaupt heruntergeladen (`FOOTPRINT_MIN_COVER`). Spart Bandbreite, reine Geometrie-Rechnung.
- **Drive-Struktur:** `OPERA_DSWx/{hls,s1}/{enguri,zhinvali}/`. Dateinamen mit MGRS-Kachel-ID.
- **Eigenschaften:** Exponential Backoff bei Verbindungsabbruechen, `MAX_WORKERS = 2` (RAM-Schonung), Resume-Logik via Dateiname-Vergleich (paginiert).

### Stufe 2: `extract_timeseries.py` (Wissenschaftliche Auswertung)
- **Qualitaetsfilter:**
  * `MIN_VALID_PCT = 95%`: Szenen mit weniger als 95% gültiger AOI-Abdeckung werden übersprungen.
  * `MAX_CLOUD_PCT = 30%`: Szenen mit mehr als 30% Wolkenbedeckung werden übersprungen.
- **Shape-Fix:** Wenn WTR- und CLOUD-Kachel unterschiedliche Pixelgroessen haben (MGRS-Kachel-Mismatch), wird CLOUD via `rasterio.warp.reproject` auf das WTR-Grid umprojiziert.
- **Verschneidungs-Logik (Raster & Vektor):**
  * Stausee-Wasserfläche: Pixelwerte 1-5 im `B01_WTR`-Band.
  * Saisonale Schneedecke: Pixelwert 252 (Schnee) *außerhalb* der RGI-Gletscherpolygone.
  * Schneebedeckter Gletscher: Pixelwert 252 (Schnee) *innerhalb* der RGI-Gletscherpolygone.
  * Blankes Gletschereis: Gesamtfläche der RGI-Polygone abzüglich der schneebedeckten Gletscherfläche (Indikator für sommerliche Gletscherschmelze).
- **RGI-Lookup:** `find_rgi()` sucht das Shapefile in `static_data/` (Download via separates `download_glaciers.py`).
- **Output-Spalten:** `date, water_area_km2, seasonal_snow_km2, snow_on_glacier_km2, bare_ice_km2, glacier_total_km2, cloud_cover_percent, valid_px_pct`
- **Output-Dateien:** `enguri_timeseries.csv/.parquet`, `zhinvali_timeseries.csv/.parquet` (lokal im Projektroot, NICHT im Drive)

### `download_glaciers.py` (separates Skript)
- Laedt RGI v7 Region 12 von NSIDC-0770 via `earthaccess.get_requests_https_session()` (NICHT fsspec - liefert HTML-Loginseite).
- Browst `regional_files/RGI2000-v7.0-G/` und matcht die Region-12-Datei am echten Dateinamen.
- WICHTIG: NSIDC-Dateiname nutzt Unterstriche: `RGI2000-v7.0-G-12_caucasus_middle_east.shp` (nicht Bindestrich). Glob nutzt Wildcard `caucasus*middle_east`.
- 204 Gletscherpolygone fuer Enguri, 154 fuer Zhinvali nach AOI-Clip.

### `download_rivers.py` (separates Skript)
- Laedt HydroRIVERS v10 Europe (~68 MB) von HydroSHEDS (oeffentlich, kein Login).
- Clippt auf beide AOIs, filtert grosse Fluesse (`ORD_FLOW <= 6`), schreibt `static_data/georgia_rivers.geojson` (461 Segmente, getaggt per AOI).

### Stufe 3: `app.py` Streamlit Dashboard (LAEUFT)
- **Framework:** Streamlit + Folium + Plotly.
- KPI-Kacheln (aktuelle Wasserflaeche/Schnee vs. historisches Max), Datums-Slider, AOI-Auswahl.
- Folium-Karte: AOI-Box, RGI-Gletscherpolygone, HydroRIVERS-Fluesse, Staudamm-Pin.
- Plotly-Charts: Wasserflaeche (Linie), Schnee/Eis (Stacked Area), Wolkenluecken als graue Balken.
- Mock-Daten-Fallback wenn Parquet noch fehlt.

---

## 3. Aktueller Bearbeitungsstand (Stand: 2026-05-30)

- **Stufe 1 fertig:** HLS-Download Enguri + Zhinvali komplett im Drive.
- **Stufe 2 fertig:** Zeitreihen mit Gletscherverschneidung berechnet, Werte plausibel (Sommer `bare_ice` hoch, Winter `snow` hoch).
- **Stufe 3 laeuft:** Dashboard funktioniert mit echten Daten, Gletscher- und Flussdaten geladen.

### GEPLANT (naechster Schritt): Raster-Overlay (TIFs) im Dashboard mit Zeit-Durchschau
- Karte soll die eigentlichen GeoTIFFs anzeigen und gestylt darstellen, sodass man per Datums-Slider durch die Szenen blaettern und die Veraenderungen ueber die Zeit sehen kann (Schnee/Eis/Wasser im Jahresverlauf).
- ANGEREICHERTE Version: `B01_WTR` (Mosaik) pro gewaehltem Datum aus Drive laden, live mit RGI-Maske verschneiden, einfaerben:
  Wasser (1-5) = blau, saisonaler Schnee = weiss, Schnee-auf-Gletscher = hellblau, blankes Gletschereis = tuerkis/grau.
- Als Folium `ImageOverlay` auf die Karte legen; Datums-Auswahl steuert das gezeigte Raster.
- Zu loesen: UTM->Lat/Lon Reprojektion, Caching pro Datum (Drive-Load langsam), evtl. vorgerenderte PNG-Overlays fuer fluessiges Durchblaettern.

### GEPLANT: Reservoir-Wasserflaeche + Wasserpegel
- `download_reservoirs.py` liefert exakte Stausee-Polygone (HydroLAKES) -> `static_data/reservoirs.geojson` (fertig).
- Naechster Code-Schritt: `extract_timeseries.py` um `reservoir_area_km2` erweitern = Wasserpixel (1-5) nur **innerhalb** des Stausee-Polygons, getrennt von der gesamten AOI-Wasserflaeche (die auch Fluesse enthaelt).
- Danach Wasserpegel: Flaeche -> Pegel ueber hypsometrische Kurve aus DEM (Copernicus DEM GLO-30); SAR (DSWx-S1) reaktivieren fuer wolkenunabhaengige Wasserausdehnung -> durchgehende Pegelzeitreihe (urspruengliche Projektidee).

### GEPLANT: S1 (SAR) reaktivieren - braucht ebenfalls Re-Download + Mosaik
S1 hatte dasselbe MGRS-Kachel-Problem wie HLS. Zum Reaktivieren:
1. S1-Collection in `COLLECTIONS` (download_to_drive.py) einkommentieren - der Loop wendet MGRS-Namen + volle AOI automatisch an.
2. Alte S1-Dateien im Drive loeschen (alte Namen ohne MGRS).
3. S1 neu herunterladen.
4. S1-Abschnitt in `extract_timeseries.py` auf Datum-Gruppierung + `mosaic_tiles` umstellen (nutzt aktuell noch per-Datei `extract_s1_stats`).

### GEPLANT (Stufe 4): Auto-Updates / Deployment - OPTION A
- GitHub Action taeglich: `download_to_drive.py` + `extract_timeseries.py`, Parquets auto-committen.
- GitHub Secrets fuer NASA + Google Drive credentials.
- Streamlit Cloud verbunden mit Repo, oeffentliche URL fuer die Doku.

### Mosaik-Refactor (umgesetzt)
- **Problem:** Bei Zhinvali liegen Stausee (Lat 42.13, Sued) und Gletscher (Lat 42.52+, Nord) in verschiedenen MGRS-Kacheln. Der alte `reservoir_is_covered`-Filter lud nur Sued-Kacheln -> Gletscherwerte komplett 0. Enguri war ok (Stausee+Gletscher in denselben Kacheln).
- **Loesung:** `download_to_drive.py` taggt Dateinamen mit MGRS-Kachel-ID und laedt ALLE AOI-Kacheln (kein reservoir-Filter). `extract_timeseries.py` merged pro Datum alle Kacheln zu einem EPSG:4326-Mosaik (volle AOI), berechnet Statistiken darauf. Loest auch das Wasser-Rauschen.
- **Achtung:** Dateinamen-Aenderung erfordert vollstaendigen Re-Download. Alte HLS-Dateien (ohne MGRS) ggf. vorher im Drive loeschen.

### Bekannte Eigenheiten
- **Drive-Ordnerstruktur:** `OPERA_DSWx/{hls,s1}/{enguri,zhinvali}/` (Parent-Ordner `DRIVE_PARENT = "OPERA_DSWx"`). Sowohl Download-Skripte als auch `extract_timeseries.py` nutzen diesen Parent.
- **Drive-Pagination:** `get_existing_filenames` nutzt `maxResults=1000` gegen das 100-Datei-Limit der API.

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
