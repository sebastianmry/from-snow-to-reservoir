# Project Context: FROM SNOW TO RESERVOIR
Satellite monitoring of the snow–glacier–reservoir water chain in the Georgian Greater Caucasus

## 1. Projektziel & Geographie
Hydrologisches Monitoring von zwei Schlüssel-Regionen im Großen Kaukasus (Georgien) zur Analyse der Verbindung von Schneeschmelze, Gletscherrückgang und Talsperren-Wasserständen.

AOI = Einzugsgebiet oberhalb des Damms (HydroBASINS lev12, siehe download_catchments.py).
Zentrale Definition in `aoi_config.py` (Single Source of Truth, alle Skripte importieren daraus).

### AOI 1: Enguri (West-Georgien / Svaneti)
- clip_box (= Catchment-bbox + 0.02° Puffer): (41.8467, 42.7294, 43.1658, 43.2783)
- Catchment-Fläche: ~3 139 km² (HydroBASINS lev12, konvergiert ab lev09)
- Dam / Pour-Point: (42.032, 42.753); s1_anchor 20240829 (Phase 6, 50 Szenen). Per Coverage war
  zunaechst Phase 7 (20240830) gewaehlt, aber der Reservoir-Praezisions-Check (--compare-orbit)
  zeigte Phase 7 unter-detektiert den See im Herbst (~6.5 vs stabile ~7.3 bei Phase 6 & 0) ->
  auf den stabileren Phase-6-Orbit gewechselt. Coverage != Mess-Qualitaet fuers Reservoir.
- Fokus: starke Vergletscherung Svaneti; neue Box reicht bis ~43.17 E (östliche Quellflüsse
  Ushguli, die die alte Box bei maxx=42.80 abschnitt).

### AOI 2: Zhinvali (Ost-Georgien / Kazbegi)
- clip_box (= Catchment-bbox + 0.02° Puffer): (44.3133, 42.0008, 45.245, 42.6283)
- Catchment-Fläche: ~2 089 km² (HydroBASINS lev12, konvergiert ab lev10)
- Dam / Pour-Point: (44.771, 42.133); s1_anchor 20240825 (Phase 1, 53 Szenen; stabil ggü. alt)
- Fokus: Aragvi-Becken bis ~42.61 N. Kazbek/Gergeti-Gletscher liegen AUSSERHALB (entwässern
  in den Terek/Norden) — hydrologisch korrekt ausgeschlossen.

---

## 2. Pipeline-Architektur

### Stufe 1: `download_hls.py` / `download_s1.py` (Datenbeschaffung)
- **Aufteilung:** Zwei schlanke Start-Skripte (HLS optisch, S1 Radar); gemeinsame Logik in `download_common.py` (nie direkt ausgefuehrt).
- **Quelle:** NASA OPERA DSWx Produkte via `earthaccess`. HLS laedt `B01_WTR` + `B09_CLOUD`, S1 laedt `B01_WTR`.
- **Footprint-Vorfilter:** Pro Datum wird die Vereinigung der Kachel-Footprints gegen das AOI geprueft; nur Tage mit >= 99% AOI-Abdeckung werden ueberhaupt heruntergeladen (`FOOTPRINT_MIN_COVER`). Spart Bandbreite, reine Geometrie-Rechnung.
- **Store-Struktur:** `OPERA_DSWx/{hls,s1}/{enguri,zhinvali}/`. Dateinamen mit MGRS-Kachel-ID.
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
- **Output-Dateien (lokal, NICHT im Tile-Store):**
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

## 3. Aktueller Bearbeitungsstand (Stand: 2026-06-02)

- **Stufe 1 fertig:** HLS + S1 Download Enguri + Zhinvali komplett im Store (MGRS-getaggt).
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

### Catchment-AOI: FERTIG (2026-06-02), Re-Download + Neuberechnung gelaufen
Umbau von Box-AOIs auf Einzugsgebiete (HydroBASINS). Status der 7 Schritte:
- **Schritt 0 (fertig):** `aoi_config.py` als Single Source of Truth (clip_box, dam, s1_anchor,
  Display-Felder). download_common / extract_timeseries / derive_reservoir / download_rivers /
  download_reservoirs / app.py importieren daraus. Verhaltensneutral verifiziert (alte Werte).
- **Schritt 1 (fertig):** `download_catchments.py` (HydroBASINS lev01-12 EU, ~361 MB einmalig).
  Pour-Point=Damm, Upstream-BFS (HYBAS_ID/NEXT_DOWN) -> dissolve -> catchments.geojson.
  Levels 7-12 verglichen: feine Level leaken NICHT stromabwärts (Level 7 tat das). Flächen
  konvergieren Enguri ab lev09 (3139 km²), Zhinvali ab lev10 (2089 km²). lev12 gewählt.
  Validiert: Reservoirs+Dämme innen, Kazbek (44.52/42.70) AUSSERHALB Zhinvali, Reservoir-Bounds
  passen in neue clip_box. Neue clip_box-Werte in aoi_config.py eingetragen.
- **Schritt 2 (Vektor fertig):** download_rivers.py (477 Segmente) + download_reservoirs.py
  (Seeds enguri 4.85 / zhinvali 5.65 km²) auf neue Box neu geclippt. glaciers braucht kein Re-Run.
- **Schritt 3 (fertig, zweistufig):** `probe_coverage.py`. Stufe A (gratis): Footprint-Coverage
  je Datum + 12-Tage-Phasen -> Kandidaten-Phasen (filtert kurze S1C-Spuren raus). Stufe B
  (`--sample N`): laedt N Testdateien je Kandidaten-Phase, mosaikiert + clippt + catchment-maskiert
  mit DERSELBEN Maschinerie wie der echte Lauf (extract_timeseries.mosaic_tiles/extract_s1_stats)
  und misst die ECHTE valid_px_pct -> objektiv bester Orbit. Grund: Footprint-Coverage trennt die
  vollen Orbits kaum (alle ~99%), aber die Pixel-Coverage kann je auf-/absteigend wegen SAR-
  Layover/Shadow abweichen. ERGEBNIS --sample 3 (echte valid_px_pct, catchment-relativ):
  * enguri: Footprint-Wahl war Phase 0 (20240823), ABER deren Startdatum nur 59.5% valid ->
    Phase 7 (20240830) gewinnt (mean 99.4%, min 99.4%, 51 Tage). Anchor auf 20240830 geaendert.
  * zhinvali: Phase 1 (20240825) bleibt (99.9%, 53 Tage). WICHTIG: Phase 7 dort nur ~7% valid
    (Footprint sagte >=99%!) - ohne Stufe B waere das ein Desaster gewesen.
  * Lehre: Orbit-Geometrie ist PRO AOI verschieden (Phase 7 = best bei enguri, worst bei
    zhinvali) -> Orbit muss pro AOI an echten Pixeln gewaehlt werden, nicht per Footprint.
- **Orbit-Filter im DOWNLOAD (neu):** download_s1.py hat `"orbit_filter": True`; download_common
  filtert nach dem Footprint-Vorfilter zusaetzlich auf die s1_anchor-Phase (orbit_phase()==0) ->
  laedt NUR den einen Orbit (~1/4 der Tage). Verifiziert im Lauf: Enguri 51/182, Zhinvali 53/183
  Tage. HLS NICHT gefiltert (braucht alle klaren Tage). Vorher zog der Download alle Orbits.
- **Download-Haertung (download_common.download_and_clip):** umgestellt von fsspec auf eine
  authentifizierte requests-Session (earthaccess.get_requests_https_session) mit HARTEM Read-
  Timeout HTTP_TIMEOUT=(15,180)s -> kein endloses Haengen mehr bei toten Sockets (fsspec hatte
  kein Read-Timeout, der Lauf blieb bei PODAAC-Problemen stehen). MAX_RETRIES=5, Backoff
  2/4/8/16/32s, RETRYABLE_STATUS={429,500,502,503,504}. probe_coverage.py Stufe B nutzt dieselbe
  requests-Session. Erfahrung Re-Download: PODAAC wirft sporadisch 5xx (transient -> Resume-Lauf
  holt sie nach) + vereinzelt 404 (zurueckgezogene/reprozessierte Granules -> bleiben weg, ok).
  Resume-sicher: download_*.py erneut starten ueberspringt vorhandene Store-Dateien.
- **Datenstand Re-Download (laufend):** S1 fertig (Enguri 204 Dateien/51 Tage, Zhinvali 272/53,
  0 Fehler). HLS laeuft (Enguri 1053/1059, 6 uebrig: 5x 502 transient + 1x 404). Danach
  Resume-Lauf fuer die 502er, dann derive_reservoir + extract_timeseries --refresh.
- **Schritt 6 (fertig):** Catchment-Polygon-Maske in extract_timeseries.py (`load_catchment` +
  `_catchment_mask`, reuse rasterize_glaciers). WICHTIG: valid_px_pct/cloud sind jetzt
  CATCHMENT-relativ (Nenner = Catchment-Pixel, sonst würde die große Box alles unter die
  90/95%-Schwelle drücken). Backward-kompatibel (ohne catchments.geojson exakt altes Verhalten).
  app.py: Catchment-Kontur statt bbox auf der Karte. AppTest beide AOIs 0 Exceptions.
- **Schritte 4/5 (GELAUFEN):** Store geleert, download_s1.py + download_hls.py (neue MGRS-
  Kacheln), derive_reservoir.py + extract_timeseries.py --refresh durch. Ergebnis: Parquets neu,
  reservoir_area_km2 + Schnee/Gletscher catchment-relativ neu berechnet. Final-Zahlen siehe
  "Reservoir-Flächen final (Catchment-AOI)" unten (Enguri 9,32 / Zhinvali 11,20 km²).
- DEM-Delineation war Fallback, NICHT nötig (HydroBASINS lev12 ausreichend).

### Wissenschaftliche Optimierungen (2026-06-02, GELAUFEN + verifiziert)
- **Orbit objektiv gewählt:** probe_coverage.py um Stufe B (`--sample N`) erweitert -> lädt N
  Testdateien je Kandidaten-Phase, misst ECHTE valid_px_pct (gleiche Maschinerie wie der Lauf).
  Ergebnis kippte die Footprint-Wahl: enguri Phase 0 (20240823) hatte am Startdatum nur 59.5%
  valid -> stattdessen Phase 7 (20240830, mean 99.4%). zhinvali blieb 20240825. WICHTIG: zhinvali
  Phase 7 hatte real nur ~7% valid (Footprint log ≥99%) - ohne Stufe B ein Desaster. Lehre:
  Orbit pro AOI an echten Pixeln wählen. Final-Reihe: enguri 51, zhinvali 52 Szenen.
- **HLS MIN_VALID_PCT 95 -> 85** (catchment-relativ). Diagnose: Enguri hat einen ~88%-Cluster
  (östl. Svaneti-Zipfel am S2/Landsat-Swath-Rand, alle Kacheln da = Aufnahmegeometrie, kein Gap).
  EHRLICH: brachte 24 -> 47 Tage (NICHT die anfangs geschätzten ~124 - die meisten 85-89%-Tage
  sind ZUSÄTZLICH bewölkt, Code prüft coverage vor cloud, sie wandern nur in den cloud-Bucket).
  Echter Limiter ist die Bewölkung: Enguri 210/300, zhinvali 187/249 Tage >30% Wolken. Tiefer als
  85 bringt ~nichts. zhinvali kein Cluster (cloud-limitiert), bleibt 49 HLS-Tage.
- **Schnee-Normalisierung:** neue Spalten seasonal_snow_frac (= Schnee / beobachtete Nicht-
  Gletscher-Beckenfläche) + seasonal_snow_km2_est (= frac × volle Beckenfläche) + obs_land_pct.
  Entzerrt coverage-/wolkenbedingte Teiltage (füllt Unbeobachtetes mit beobachteter Schneerate).
  Effekt moderat (~5-8%), weil ok-Tage eh 92-95% beobachtet sind = wenig Bias. App + KPI nutzen est.
- **Reservoir-Guard + water-Guard:** reservoir_valid_pct = valide Pixel IM See-Footprint; < 95% ->
  reservoir_area_km2 UND water_km2 = NaN (See ist Hauptwasserkörper, Wasser nicht normalisierbar).
  Fängt NoData-über-See als Scheinabsenkung. Verifiziert: enguri 2025-04-27 reservoir_valid_pct
  45.2% -> beide NaN (war also NoData-Loch, kein Wind). Genau 1 NaN-Tag je AOI.
- **App:** Reservoir-Linie wieder EINE Linie (User fand Tropfen+Median unruhig; der Daten-Guard
  liefert die Robustheit, NaN-Tage = Lücke). Schnee-Chart/KPI nutzen seasonal_snow_km2_est.
- **`--recompute` Flag (extract_timeseries):** liest nur ergebnisrelevante Tage neu (ok + unter
  AKTUELLEN Schwellen neu qualifizierende), übernimmt cloud-/unter-Schwelle-Skips MIT Stats aus
  dem Cache ohne Store-Read. Nach Logik-/Schwellen-Änderung viel schneller (zhinvali 50 statt 249).
  prepare_cache() + _needs_recompute(). NICHT --refresh nutzen, wenn nur Logik sich ändert!
- **Download-Robustheit:** requests-Session statt fsspec, HTTP_TIMEOUT (10,60)s gegen Hänger,
  MAX_RETRIES 3, Retry für 429/5xx. PODAAC warf während des Re-Downloads viele transiente 502 +
  vereinzelt 404 (zurückgezogene Granules). Resume-Lauf holt 502er nach; 404 bleiben weg.
- **HLS-Coverage S2 vs Landsat (belegt, --compare-orbit-Logik / Sensor-Auswertung):** Die
  fluktuierende HLS-Coverage ist sensorbedingt. Sentinel-2 (Swath 290 km) deckt das langgestreckte
  Enguri-Becken voll ab (jeder S2-Tag ≥85%), Landsat (185 km) nur teils. Median je Sensor: S2A 100,
  S2C 99.7, S2B 88, L8 73.5, L9 74.3. Der 85%-Filter behält faktisch die S2-Tage. Jüngere Monate
  schlechter = mehr Landsat-/S2C-Einzelpassagen. Dokumentierte optische Datengrenze, kein Bug.
- **Zweit-Orbit-/Präzisions-Check (probe_coverage --compare-orbit, NEU):** misst reservoir_area_km2
  von Stichprobentagen eines Nachbar-Orbits gegen die bestehende Reihe (nächster Tag). 1-Tag-
  Nachbarorbit = reines Geometrie-Rauschen -> Fehlerschranke ~±0.2 km² (zhinvali), ~±0.4 km²
  (enguri, bis 0.68 bei 1 Tag). 6-Tage-Orbit (Densification): Enguri Phase 0 vs 7 hatte +0.26
  Bias (Sägezahn) -> NICHT kombiniert. Zhinvali nicht verdichtbar (6-Tage-Orbit = die 7%-Phase).
  ERGEBNIS: Phase 7 (enguri) dippt im Herbst (~6.5), Phase 6 & 0 stabil ~7.3 -> Phase 7 = Artefakt.
- **Enguri-Anchor 20240830 -> 20240829 (Phase 6) gewechselt + S1 neu gerechnet:** Reservoir jetzt
  stabil ~6.9-7.6 km² (Spanne ~0.78), Herbst-Dip weg. Signal/Rausch: Zhinvali ~12:1 (echtes Signal,
  Hub ~2.4 km²), Enguri ~2:1 (nahe Rauschgrenze -> Fläche ist schwacher Proxy, empirisch bestätigt).
- **Reservoir-Flächen final (Catchment-AOI):** Footprint Enguri 9.32 km², Zhinvali 11.20 km².
  Zhinvali-Reservoir klarer Jahresgang ~8.5 (Frühjahr) -> ~11 (Herbst); Enguri flach ~7.0-7.6.
- Stand Daten: S1 komplett (enguri Phase 6 50 Tage, zhinvali 52). HLS Enguri 47 / zhinvali 49 Tage
  (Bewölkung limitiert; 502-Lücken nachgeholt, brachten 0 neue Tage = waren cloudy/partiell).
  Parquets neu, App verifiziert (0 Exceptions).

### Raster-Overlay (TIFs als PNG) im Dashboard mit Zeit-Durchschau - FERTIG (2026-06-03), gerendert + committet
- `render_overlays.py` umgesetzt: laeuft EINMAL nach extract_timeseries.py, liest pro
  Sensor (s1/hls) die Datumsliste aus dem fertigen Parquet (Szenen = exakt die Charts),
  laedt die Tiles aus dem Store, mosaikiert (mosaic_tiles), rechnet auf max 900 px herunter
  (MAX_DIM, nearest), faerbt ein und schreibt static_data/overlays/{site}/{sensor}/
  {YYYYMMDD}.png + bounds.json. Reuse aller extract_timeseries-Bausteine + Catchment-/
  RGI-Maske. Farben: Wasser blau, saisonaler Schnee weiss, Schnee-auf-Gletscher hellblau,
  blankes Gletschereis tuerkis, Wolke(253)/NoData/ausserhalb Catchment transparent.
  Resume-sicher (vorhandene PNGs uebersprungen, --refresh erzwingt neu). Filter:
  `python render_overlays.py [enguri|zhinvali] [s1|hls] [--refresh]`.
- app.py: Sektion "Szenen im Zeitverlauf" (Sensor-Radio + select_slider ueber die
  verfuegbaren Daten) laedt nur fertige PNGs als base64-data-URI in eine leichte
  folium-Karte (build_overlay_map: Basemap + Catchment-Kontur + ImageOverlay +
  Reservoir-Umriss). KEIN Rasterrechnen zur Laufzeit. Ohne gerenderte Overlays zeigt
  die Sektion einen Hinweis (AppTest verifiziert, 0 Exceptions). load_overlay_index /
  load_overlay_uri @st.cache_data.
- GELAUFEN: `python render_overlays.py` durch, alle 202 PNGs in static_data/overlays/
  ins Repo committet (enguri 51 s1 / 48 hls, zhinvali 53 / 50) -> Streamlit Cloud hat sie.
- (urspruenglicher Plan, weiterhin gueltig) User-Wunsch: BEIDE Sensoren S1 (Wasser)
  UND HLS (Schnee/Eis), nicht nur S1.
- Karte soll die eigentlichen GeoTIFFs anzeigen und gestylt darstellen, sodass man per Datums-Slider durch die Szenen blaettern und die Veraenderungen ueber die Zeit sehen kann (Schnee/Eis/Wasser im Jahresverlauf).
- ANGEREICHERTE Version: `B01_WTR` (Mosaik) pro gewaehltem Datum aus dem Store laden, live mit RGI-Maske verschneiden, einfaerben:
  Wasser (1-5) = blau, saisonaler Schnee = weiss, Schnee-auf-Gletscher = hellblau, blankes Gletschereis = tuerkis/grau.
- Als Folium `ImageOverlay` auf die Karte legen; Datums-Auswahl steuert das gezeigte Raster.

**Implementierungs-Ansatz (Stabilitaet):** Vorrendern statt live rechnen. Ein separates `render_overlays.py` (laeuft einmal nach `extract_timeseries.py`) erzeugt pro gefiltertem Datum ein kleines, eingefaerbtes PNG nach `static_data/overlays/{aoi}/{date}.png` (+ Bounds-Sidecar). Die App laedt nur fertige PNGs via `folium.ImageOverlay` - kein Rasterrechnen zur Laufzeit, stabil und fluessig durchblaetterbar. Hebel: Aufloesung reduzieren, nur gefilterte Tage rendern, `@st.cache_data`, batch-weise mit Speicherfreigabe, PNGs lokal (kein Store-Zugriff zur Laufzeit).

### Reservoir-Polygone + Fuellwerte (FERTIG, gelaufen + verifiziert)
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
  verworfen). ECHTER LAUF ueber den Store steht noch aus.
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
  Headless mit streamlit AppTest geprueft (beide AOIs, 0 Exceptions). Hinweis: die Plotly-Charts
  nutzen `width="stretch"` (Streamlit-Core-Migration erledigt). Die zwei `st_folium`-Aufrufe nutzen
  weiterhin `use_container_width` - das ist der EIGENE Parameter der streamlit-folium-Komponente
  (kein deprecateter Streamlit-Core-Parameter), `width` erwartet dort einen Pixel-Integer. Also
  korrekt so, kein offener Punkt.
- **Stausee-Label-Styling (2026-06-04):** Das persistente On-Map-Label heisst jetzt
  "{Name} Reservoir" (englischer Zusatz), schwarz, 9px, font-weight 600, KEIN Hintergrund-
  Kasten und nicht kursiv mehr - stattdessen weisser Text-Halo (text-shadow) fuer Lesbarkeit
  auf der blauen Seeflaeche. font-family Montserrat (via Google-Fonts-Link im Karten-Header
  geladen, Helvetica/Arial-Fallback), passend zur CartoDB-Positron-Basemap-Typografie.
  DivIcon-Box auf 150px verbreitert, damit der laengere Text mittig bleibt.

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

### ERLEDIGT (Stufe 4): Deployment + Auto-Updates
- **Live:** Streamlit Community Cloud, https://from-snow-to-reservoir.streamlit.app/
  (repo public, branch main, `app.py`). App liest zur Laufzeit NUR lokale Dateien
  (Parquets, GeoJSONs, RGI-Shapefile, Overlay-PNGs), daher KEINE Secrets in der Cloud.
- **Repo self-contained:** `.gitignore` ist ignore-all-then-un-ignore. Die ~12 MB
  Runtime-Artefakte sind eingecheckt, die 2.7 GB Rohdaten (HydroLAKES/RIVERS/BASINS)
  bleiben draussen. `requirements.txt` = schlanke App-Deps (Cloud), `requirements-pipeline.txt`
  = volle Pipeline-Deps. requirements.txt ist auf exakte Versionen GEPINNT (Stand der
  lokalen georgia-sar-Env) -> reproduzierbarer Cloud-Build, kein stiller Drift.
- **Python-Versionen (bewusst getrennt):** lokal Pipeline = 3.11 (conda georgia-sar),
  Cloud-App = 3.13/3.14 (in den Streamlit-Settings waehlbar). Kein Konflikt: die beiden
  Laufzeiten teilen sich nichts ausser versionsneutralen Datendateien (Parquet/GeoJSON/PNG).
  Der App-Code ist vorwaertskompatibel; einziges Restrisiko waere ein fehlendes Wheel fuer
  eine sehr neue Cloud-Python-Version (dann Python in den Settings auf 3.13 stellen).
- **Tile-Store (`storage.py`):** EIN lokaler Filesystem-Store (`LocalStore`, Ordner unter
  `PIPELINE_LOCAL_DIR`, Default `./opera_local`, keine Cloud-Auth -> laeuft headless lokal
  UND in CI gleich). Die frueher in download_common/extract_timeseries duplizierten
  Speicher-Helfer sind hier zentralisiert; download/extract/render/derive_reservoir laufen
  ueber das Store-Objekt (`ensure_folder`/`get_folder_id`/`existing_names`/`list_tifs`/
  `write`/`read_bytes`).
- **Auto-Update (`.github/workflows/update-data.yml`):** woechentlich (Mo 03:00 UTC) +
  manueller Dispatch: download -> extract -> render, dann
  werden geaenderte Parquets + Overlay-PNGs zurueck committet -> Streamlit re-deployt.
  Tiles liegen in `runner.temp` (nie committet); ein `actions/cache` von Tile-Store und
  Per-Date-Cache haelt die Laeufe inkrementell. Einziges Secret: `EARTHDATA_USERNAME` /
  `EARTHDATA_PASSWORD`. `search_granules()` retryt CMR-5xx (NASA-seitige transiente
  Fehler) 3x mit Backoff.
- **CI-Stolperstein IPv6 (gefixt 2026-06-04):** urs.earthdata.nasa.gov ist dual-stack
  (A + AAAA). GitHub-Hosted-Runner haben eine IPv6-Schnittstelle aber kein IPv6-Egress;
  Linux bevorzugt IPv6 -> Login lief auf die AAAA-Adresse und starb mit "[Errno 101]
  Network is unreachable" (kein Fallback auf IPv4). Fix: Workflow-Step "Force IPv4"
  deaktiviert IPv6 (sysctl) vor dem Download. Zusaetzlich haertet download_common.run()
  den Login: ohne TTY (CI) KEIN interaktiver Fallback mehr (der starb mit EOFError am
  input()-Prompt und verdeckte den echten Fehler), sondern klare RuntimeError-Meldung.
  requirements-pipeline.txt jetzt gepinnt (wie requirements.txt) -> reproduzierbarer Build.

### ERLEDIGT: Code-/Doku-Qualitaetspass (2026-06-04)
- **Coding-Styleguide ueber alle 13 Skripte** angewandt: explizite Namen (kein bare
  df/gdf/res/data, keine Einzelbuchstaben-Loops), Imports am Modulkopf, pathlib fuers
  File-Open, tote Imports raus. Verhalten unveraendert (aoi_config-SSOT, Sensor-Trennung,
  Reservoir-Guard, Pre-Render-UI bleiben), verifiziert per Import + Streamlit-AppTest
  (0 Exceptions). Etablierte Akronyme `wtr` (Bandname B01_WTR) und `rgba` bewusst behalten.
- **Text-Styleguide** auf die Prosa angewandt: README-Komposita korrekt mit Bindestrich
  (radar-based, cloud-independent, sub-basin, catchment-relative ...), keine en/em-Dashes.
  App-About: Kursname englisch ("Automated Geospatial Data Processing").
- **Aufgeraeumt:** Roh-Geodaten (HydroBASINS/LAKES/RIVERS, ~2.7 GB) lokal geloescht; nur
  committete abgeleitete Outputs + RGI-Shapefile bleiben. download_*-Skripte holen die
  Rohdaten bei Bedarf neu.

### Mosaik-Refactor (umgesetzt)
- **Problem:** Bei Zhinvali liegen Stausee (Lat 42.13, Sued) und Gletscher (Lat 42.52+, Nord) in verschiedenen MGRS-Kacheln. Der alte `reservoir_is_covered`-Filter lud nur Sued-Kacheln -> Gletscherwerte komplett 0. Enguri war ok (Stausee+Gletscher in denselben Kacheln).
- **Loesung:** Die Download-Skripte taggen Dateinamen mit MGRS-Kachel-ID und laden ALLE AOI-Kacheln (kein reservoir-Filter). `extract_timeseries.py` merged pro Datum alle Kacheln zu einem EPSG:4326-Mosaik (volle AOI), berechnet Statistiken darauf. Loest auch das Wasser-Rauschen.
- **Achtung:** Dateinamen-Aenderung erfordert vollstaendigen Re-Download. Alte HLS-Dateien (ohne MGRS) ggf. vorher im Store loeschen.

### Bekannte Eigenheiten
- **Store-Ordnerstruktur:** `OPERA_DSWx/{hls,s1}/{enguri,zhinvali}/` (Top-Level `DATA_ROOT = "OPERA_DSWx"` in aoi_config.py). Sowohl Download-Skripte als auch `extract_timeseries.py` / `render_overlays.py` / `derive_reservoir.py` nutzen diesen Root.

---

## 4. Technischer Stack
- `earthaccess` — NASA Granule-Suche und Download
- `rioxarray` / `rasterio` — In-Memory Rasterverarbeitung und Clipping
- `geopandas` — RGI Vektordaten laden und auf AOI zuschneiden
- `pandas` / `pyarrow` — Parquet-Output fuer Streamlit
- `tqdm` — Fortschrittsanzeige
- Tile-Store: lokales Filesystem unter `PIPELINE_LOCAL_DIR` (Default `./opera_local`)
- Conda-Environment: `georgia-sar`
