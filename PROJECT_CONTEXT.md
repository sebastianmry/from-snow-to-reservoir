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
Verarbeitet HLS (optisch) UND S1 (Radar). Pro Datum werden alle MGRS-Kacheln zu einem
EPSG:4326-Mosaik verschmolzen, exakt auf die clip_box zugeschnitten und gepaddet.

- **Mosaik-Clip (wichtig!):** Nach `merge_arrays` wird auf die clip_box GECLIPPT, dann
  gepaddet. Ohne den Clip erzeugen Kacheln aus verschiedenen UTM-Zonen (Enguri quert
  37/38) eine zu grosse Bounding-Box mit NoData-Ecken -> Coverage faelschlich ~92% statt
  ~99%. Der Clip macht `valid_px_pct` AOI-relativ.
- **HLS-Qualitaetsfilter:**
  * `MIN_VALID_PCT = 95%`: HLS-Szenen mit < 95% gueltiger AOI-Abdeckung werden uebersprungen.
  * `MAX_CLOUD_PCT = 30%`: HLS-Szenen mit > 30% Wolken werden uebersprungen.
  * Wolkenmaske = WTR-Eigenflag **Wert 253** (cloud/cloud-shadow). KEIN separater B09-Layer
    mehr (war bit-kodiert und inkonsistent). `download_hls.py` laedt daher nur noch B01_WTR.
- **S1-Filter + Orbit-Dedup:**
  * `S1_MIN_VALID_PCT = 70%`: loses Sicherheitsnetz (entfernt 10%-Teil-Orbit bei Zhinvali).
  * `dedup_single_orbit()`: reduziert auf EINEN konsistenten relativen Orbit (gleiche
    Aufnahmegeometrie) -> saubere ~12-Tage-Reihe ohne Sägezahn. Ablauf: (1) partielle
    Szenen `< S1_FULL_COVER_PCT = 90%` rauswerfen (z.B. Enguri 79%-Teil-Orbit), (2) volle
    Szenen nach 12-Tage-Phase (`ordinal % 12`) gruppieren, (3) den Track behalten, der den
    Zeitraum am besten abdeckt. Begruendung: SAR-Wasserklassifikation haengt von Layover/
    Shadow ab (orbitabhaengig) -> ein Track = konsistent. Phase statt fester Coverage-Zahl,
    weil der Clip-Fix die vollen Orbits alle auf ~99% bringt.
  * Ergebnis: Enguri 49, Zhinvali 52 Szenen (von 178/128 Roh-Szenen).
- **Verschneidungs-Logik (Raster & Vektor):**
  * Wasserfläche (AOI-weit): Pixelwerte 1-5 im `B01_WTR`-Band.
  * Saisonale Schneedecke: Pixelwert 252 *außerhalb* der RGI-Gletscherpolygone.
  * Schneebedeckter Gletscher: Pixelwert 252 *innerhalb* der RGI-Gletscherpolygone.
  * Blankes Gletschereis: RGI-Gesamtfläche abzüglich schneebedeckter Gletscherfläche.
- **RGI-Lookup:** `find_rgi()` sucht das Shapefile in `static_data/` (via `download_glaciers.py`).
- **Per-Datum-Cache:** `static_data/cache/{site}_{s1,hls}.json` speichert jedes Datum-Ergebnis
  (auch geskippte, mit Grund). Re-Laeufe ueberspringen gecachte Daten -> Sekunden statt
  ~30 Min. `--refresh` ignoriert den Cache. `--skip-s1` / `--skip-hls` fuer Teil-Laeufe.
- **Vorfilter:** `MIN_TILES = 2` ueberspringt Einzelkachel-Tage vor dem Download (decken die
  zonenuebergreifende AOI nie ab).
- **Output-Dateien (lokal, NICHT im Drive):**
  * HLS: `{site}_timeseries.csv/.parquet` (water, snow, glacier, cloud, valid_px_pct)
  * S1:  `{site}_s1_timeseries.csv/.parquet` (water_km2, valid_px_pct)

### WICHTIG: Arbeitsteilung Wasser vs. Schnee (belegt 2026-05-31)
- **Wasser/Pegel -> NUR S1.** Optisches HLS ueber-detektiert Wasser massiv (Geländeschatten/
  Eis als Wasser fehlklassifiziert), auch an wolkenfreien Tagen: Zhinvali HLS ~150-200 km²
  vs. S1 ~40-48 km² fuer denselben Stausee. S1 (Radar) ist wolkenunabhaengig und robust.
- **Schnee/Eis -> HLS.** `seasonal_snow_km2` ist saisonal plausibel (Winter fast vollflaechig
  ~3000-4000 km² ueber das ~6000 km² AOI, Sommer niedrig). S1 hat kein Schnee-Signal.
- **HLS-Abdeckung duenn** (Kaukasus sehr wolkig): Enguri 60/381, Zhinvali 39/265 gueltige Tage.
  Genau deshalb fuellt die dichte, lueckenlose S1-Reihe das Wasser-Signal.

### S1-Orbit-Metadaten (probe_orbits.py)
- Relative Orbitnummer steht NICHT in der DSWx-S1 UMM (`AdditionalAttributes` leer). Nur der
  Satellit (S1A/S1C) ist aus dem Dateinamen ablesbar.
- S1C ab ~Mai 2025 -> ~doppelte Ueberflugdichte, aber teils eigene Tracks. Eine saubere
  6-Tage-Verdichtung (S1A+S1C zusammen) ist ohne echte Orbitnummern nicht moeglich, ohne
  Geometrien zu mischen. Daher bleibt es bei einem Track / ~12 Tagen.

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

## 3. Aktueller Bearbeitungsstand (Stand: 2026-06-01)

- **Stufe 1 fertig:** HLS + S1 Download Enguri + Zhinvali komplett im Drive (MGRS-getaggt).
- **Stufe 2 fertig:** HLS- UND S1-Zeitreihen berechnet. Coverage-Bug (Mosaik-Clip) gefixt,
  Wolkenmaske auf WTR-253 umgestellt, S1 mit Orbit-Dedup (ein Track, ~12 Tage). Per-Datum-
  Cache eingebaut. Arbeitsteilung: Wasser=S1, Schnee=HLS (siehe Abschnitt oben).
- **Stufe 3 fertig (Kern):** Dashboard mit echten Daten. `app.py` nutzt jetzt die
  Quellentrennung (Wasser aus `{site}_s1_timeseries`, Schnee/Gletscher aus
  `{site}_timeseries`); Gletscher-Glob gefixt (Wildcard, Polygone laden). S1-Wasser
  plausibel (~22-26 km² Enguri, ~40 km² Zhinvali) mit Abwaerts-Trend.
  Rest-Rauschen: S1-Wasser zappelt ±1-2 km² (SAR-Speckle/Wind + bei Enguri zwei volle
  Tracks im selben Phasen-Bucket) - wird durch Reservoir-Polygon/Glaettung sauberer.
- **Reservoir-Flaeche fertig (2026-06-01):** S1-abgeleitete Stausee-Polygone
  (`derive_reservoir.py`) + `reservoir_area_km2` in den S1-Parquets, Orbit fest verankert.
  Details siehe Abschnitt "Reservoir-Polygone + Fuellwerte" unten.

### GEPLANT (naechster Schritt, vom User bestaetigt 2026-06-01): Raster-Overlay (TIFs als PNG) im Dashboard mit Zeit-Durchschau
- User-Wunsch konkret: die TIFs als eingefaerbte PNGs in der App pro Datum zeigen
  (Schnee/Eis/Wasser-Farben) - und zwar fuer BEIDE Sensoren: S1 (Wasser) UND HLS
  (Schnee/Eis), nicht nur S1.
- Karte soll die eigentlichen GeoTIFFs anzeigen und gestylt darstellen, sodass man per Datums-Slider durch die Szenen blaettern und die Veraenderungen ueber die Zeit sehen kann (Schnee/Eis/Wasser im Jahresverlauf).
- ANGEREICHERTE Version: `B01_WTR` (Mosaik) pro gewaehltem Datum aus Drive laden, live mit RGI-Maske verschneiden, einfaerben:
  Wasser (1-5) = blau, saisonaler Schnee = weiss, Schnee-auf-Gletscher = hellblau, blankes Gletschereis = tuerkis/grau.
- Als Folium `ImageOverlay` auf die Karte legen; Datums-Auswahl steuert das gezeigte Raster.

**Implementierungs-Ansatz (Stabilitaet):** Vorrendern statt live rechnen. Ein separates `render_overlays.py` (laeuft einmal nach `extract_timeseries.py`) erzeugt pro gefiltertem Datum ein kleines, eingefaerbtes PNG nach `static_data/overlays/{aoi}/{date}.png` (+ Bounds-Sidecar). Die App laedt nur fertige PNGs via `folium.ImageOverlay` - kein Rasterrechnen zur Laufzeit, stabil und fluessig durchblaetterbar. Hebel: Aufloesung reduzieren, nur gefilterte Tage rendern, `@st.cache_data`, batch-weise mit Speicherfreigabe, PNGs lokal (kein Drive-Zugriff zur Laufzeit).

### Reservoir-Polygone + Fuellwerte (CODE FERTIG, Lauf ausstehend)
- **Problem geklaert:** HydroLAKES sitzt zwar korrekt am Damm (Enguri 0,58 km, Zhinvali
  0,34 km), unterschaetzt aber stark (Enguri 4,86 km² geodaetisch vs. real ~13 km² - erfasst
  nur das untere Becken, nicht die langen Talarme). Es gibt KEINEN besseren HydroLAKES-
  Kandidaten (naechste Alternativen >9 km entfernt, winzig). Umwaehlen hilft nicht.
- **Loesung (umgesetzt): Reservoir-Huelle aus den eigenen S1-Daten ableiten.** Neues
  `derive_reservoir.py` (einmaliger S1-Durchlauf, RAM-schonend - nur ein Mosaik gleichzeitig):
  pro Datum S1-B01_WTR-Mosaik (nur Voll-Coverage >= S1_FULL_COVER_PCT) auf festes Grid via
  reproject_match, akkumuliert eine Wasser-Haeufigkeitskarte (water_obs/valid_obs). Persistente
  Wassermaske = freq >= FREQ_THRESH (0,25); per scipy connected-component nur die mit dem
  HydroLAKES-Seed (+ Damm-Pixel) verbundene Komponente behalten; Closing + fill_holes; ->
  Polygon nach `static_data/reservoirs.geojson` (+ `{site}_s1_waterfreq.tif` zum Sichtpruefen).
  Offline mit synthetischem Array getestet (verbundene Komponente korrekt, fremder Wasserkoerper
  verworfen). ECHTER LAUF ueber Drive steht noch aus.
- `download_reservoirs.py`: Output jetzt `reservoirs_hydrolakes.geojson` (nur noch SEED fuer
  derive_reservoir). CRS-Warnung gefixt (distance() auf UTM 38N statt geografisch).
- `extract_timeseries.py`: `reservoir_area_km2` = S1-Wasserpixel (1-5) **innerhalb** des
  Reservoir-Polygons (rasterisiert pro Datum), getrennt von der AOI-Gesamtwasserflaeche. Spalte
  erscheint automatisch sobald reservoirs.geojson existiert. Cache-Eintraege ohne die Spalte
  gelten als stale und werden neu berechnet (kein --refresh noetig). Soll das ±1-2 km²
  Rest-Rauschen beseitigen (orbit-/speckle-robuster).
- **GELAUFEN + verifiziert (2026-06-01):** derive_reservoir.py -> reservoirs.geojson
  (Enguri 9,86 km², Zhinvali 11,19 km²; in QGIS gegen waterfreq.tif geprueft, Form trifft
  Talverlauf, laeuft nicht in Zufluesse aus -> FREQ_THRESH bei 0,25 bleibt). Dann
  extract_timeseries.py --skip-hls -> reservoir_area_km2 in den S1-Parquets.
- **Validierung gegen Literatur:** Zhinvali 11,19 km² vs. real ~11,5 km² = 97 % Treffer
  (Punktlandung -> Methode bestaetigt). Enguri 9,86 km² vs. ~13 km² = ~76 %: die ~13 km² sind
  die VOLLSTAU-Flaeche; unser Wert ist die im Zeitraum Aug2024-Mai2026 regelmaessig (>=25 %)
  ueberflutete Flaeche. Max gemessene reservoir_area_km2 bei Enguri = 8,09 km² (an KEINEM Tag
  hoeher) -> Enguri war durchgehend deutlich abgesenkt, Vollstau nie erreicht; auch FREQ=0,10
  gibt nur 10,39 km² (obere Talarme im Zeitfenster nie nass). Differenz ist also REALE
  Absenkung, kein Fehler. Fuer Bericht so formulieren: "regelmaessig ueberflutete Flaeche im
  Beobachtungszeitraum", nicht "Stauraum bei Vollstau". Methodisch sauber nachweisbar.
- **Ergebnis:** Reservoir-Signal viel ruhiger als AOI-Gesamtwasser. Zhinvali 9,9x ruhiger
  (8,42-10,91 km², klarer Jahresgang Absenkung Winter/Fruehjahr -> Auffuellung ab Mai),
  Enguri 4,6x ruhiger. WICHTIG: Enguri-Reservoirflaeche aendert sich kaum (~1 km² Spanne),
  weil Jvari ein tiefer Schluchtspeicher ist (steile Waende: grosser Pegelhub, kleine
  Flaechenaenderung). -> Bei Enguri ist die Flaeche ein schwacher Speicher-Proxy; ein absoluter
  Pegel waere noetig, ist aber mangels Bathymetrie nicht ableitbar (siehe Abschnitt unten).
- **Orbit-Verankerung (neu in extract_timeseries.py):** Pro AOI `s1_anchor` (ein Datum des
  gewuenschten Orbits). Die S1-Sektion behaelt NUR Tage dieser 12-Tage-Phase (orbit_phase()),
  laedt also nur diesen einen Orbit (~1/4 der Tage hier) statt alle. Anker = die bisher
  genutzte Reihe: enguri 20240829 (49 volle Szenen), zhinvali 20240825 (52). Ersetzt die
  fruehere Auto-Auswahl durch dedup (die je nach Coverage schwanken konnte). dedup bleibt als
  Teil-Szenen-Filter. Cache-Rows werden vor dem Schreiben auf die Anker-Phase beschraenkt.
- **app.py FERTIG (2026-06-01):** `load_reservoir()` liest reservoirs.geojson; Footprint-Polygon
  auf der Folium-Karte (blau, Flaechen-Tooltip) + Caption; Wasser-Chart zeigt reservoir_area_km2
  als Hauptlinie und AOI-water_km2 als blasse Referenz; KPI-Kachel zeigt Stausee-Flaeche.
  Headless mit streamlit AppTest geprueft (beide AOIs, 0 Exceptions). Bekannt/offen: das ganze
  Dashboard nutzt noch `use_container_width` (deprecated, nur Warnung) - spaeter modernisieren.

### Wasserpegel (DEM/INFLOS) - GETESTET und VERWORFEN (2026-06-01)
Der INFLOS-Pegelansatz (S1-Uferlinie x Copernicus DEM GLO-30) wurde implementiert,
getestet und dann **bewusst wieder entfernt** - keine Pegelstaende im Projekt.
- **Grund:** Das Copernicus DEM (TanDEM-X 2011-2015) hat beide Stauseen ALS Wasser
  aufgenommen und die Oberflaeche eingeebnet -> **keine Bathymetrie**. Belegt mit DEM-Werten
  im Footprint: Zhinvali komplett flach 805,5 m (p5=p95=805,5; std 1,7) -> Pegel konstant
  805,5 fuer JEDES Datum (degeneriert). Enguri Boden bei 425 m + echte Schluchtwaende darueber
  -> nur 425-440 m, Tiefststaende zensiert. Das ist INFLOS-Annahme D (Bathymetrie nicht
  ableitbar); INFLOS ist fuer Ueberflutungen auf trockenem Gelaende, nicht fuer aufgestaute Seen.
- **Altimetrie geprueft (Alternative fuer echten Pegel):** ICESat-2 ATL13 via earthaccess -
  die bbox-Suche matcht nur die grobe Orbit-Ausdehnung; die gesampelten Paesse kreuzen die AOI
  gar nicht (0 Punkte in der Region). Kleine Bergstauseen werden von ICESat-2/Jason/Sentinel-3
  kaum getroffen. Verworfen (echtes Track-Subsetting via icepyx waere noetig, unsicherer Ertrag).
- **Entfernt:** download_dem.py geloescht; shoreline_level/load_dem/dem_has_relief und
  reservoir_level_m aus extract_timeseries.py; Pegel-Tab aus app.py. `reservoir_area_km2`
  bleibt der Speicher-Indikator. Re-Lauf `extract_timeseries.py --skip-hls` (gecacht, schnell)
  regeneriert die Parquets ohne die Pegel-Spalte.
- **Fazit fuer Bericht:** Speicher wird ueber die S1-FLAECHE ueberwacht. Absoluter Pegel ist mit
  frei verfuegbaren DEMs nicht ableitbar (keine Bathymetrie); Altimetrie deckt diese kleinen
  Bergstauseen nicht ab. Das ist eine dokumentierte Datengrenze, kein Methodenfehler.

### ERLEDIGT: S1 (SAR) reaktiviert
- `download_s1.py` laedt alle MGRS-Kacheln (volle AOI), `extract_timeseries.py` mosaikiert
  pro Datum und reduziert via `dedup_single_orbit()` auf einen konsistenten Orbit.
- Saisonales Signal sichtbar: Zhinvali-Wasser faellt von ~48 km² (Herbst) auf ~28-35 km²
  (Fruehjahr) - echte Stausee-Absenkung, wolkenunabhaengig erfasst.

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
