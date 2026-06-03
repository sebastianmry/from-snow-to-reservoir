# FROM SNOW TO RESERVOIR
### Satellite monitoring of the snow–glacier–reservoir water chain in the Georgian Greater Caucasus

© Sebastian Macherey · [github.com/sebastianmry/from-snow-to-reservoir](https://github.com/sebastianmry/from-snow-to-reservoir)

---

## Motivation

Georgiens Stromversorgung haengt zu etwa 80 % von der Wasserkraft ab. Saisonale Schneedecke und Gletscher des Grossen Kaukasus steuern den Zufluss in die Talsperren — der Klimawandel veraendert dieses System fundamental. Beide untersuchten Stauseen liegen unmittelbar an den von Russland kontrollierten Gebieten Abchasien und Suedossetien.

## Areas of Interest

Die AOIs sind **das Einzugsgebiet (Catchment) oberhalb des jeweiligen Staudamms**, abgeleitet aus HydroBASINS (Damm als Pour-Point, Upstream-Sub-Basins unioniert, siehe `download_catchments.py`). Der Satellitendownload nutzt die **Bounding-Box** des Catchments (+ kleiner Puffer); die Analyse-Statistik wird zusaetzlich auf das **Catchment-Polygon maskiert**, sodass Schnee/Gletscher/Wasser nur innerhalb der Wasserscheide zaehlen.

| AOI | Catchment-Flaeche | Clip-Box (min_lon, min_lat, max_lon, max_lat) | Staudamm (lon, lat) | Objekte |
|-----|-------------------|----------------------------------------------|---------------------|---------|
| Enguri | ~3 139 km² | 41.847, 42.729, 43.166, 43.278 | 42.032, 42.753 | Enguri Dam (271 m), starke Vergletscherung Svaneti (oestliche Quellfluesse bis Ushguli), grenzt an Abchasien |
| Zhinvali | ~2 089 km² | 44.313, 42.001, 45.245, 42.628 | 44.771, 42.133 | Zhinvali Dam (Trinkwasser Tbilisi); Kazbek/Gergeti-Gletscher liegen **ausserhalb** (entwaessern in den Terek), grenzt an Suedossetien |

## Daten

- **OPERA DSWx-HLS (Level-3):** Optische Wasser-/Schneeklassifikation (B01_WTR) aus Landsat-8/9 und Sentinel-2, ~2-3 Tage Revisit. Wolkenmaskierung ueber das WTR-eigene Flag (Wert 253) — kein separater B09-Layer noetig.
- **OPERA DSWx-S1 (Level-3):** Radar-basierte Wasserklassifikation (B01_WTR), wolkenunabhaengig. Auf einen konsistenten relativen Orbit reduziert (~12-Tage-Reihe).
- **Randolph Glacier Inventory v7 (RGI), Region 12:** Gletscher-Polygone fuer den Kaukasus (NSIDC, via `download_glaciers.py`)
- **HydroBASINS v1c (lev12):** Sub-Basin-Polygone (HydroSHEDS) zur Ableitung des Einzugsgebiets oberhalb des Staudamms (Pour-Point-Delineation, via `download_catchments.py`) -> definiert AOI-Box + Analyse-Maske
- **HydroRIVERS v10:** Flussnetz (HydroSHEDS), gefiltert auf das Einzugsgebiet oberhalb des Staudamms (via `download_rivers.py`)
- **HydroLAKES v1.0:** Stausee-Polygone (HydroSHEDS) als *Seed* via `download_reservoirs.py`. HydroLAKES unterschaetzt die Seen stark (Enguri 4,9 km² statt real ~13) — daher nur Ansatzpunkt; der echte Footprint wird per `derive_reservoir.py` aus der S1-Wasserausdehnung abgeleitet.

Zeitraum: August 2024 bis heute

## Pipeline

```
NASA Earthdata (earthaccess)
        |
        v
download_hls.py / download_s1.py   # Footprint-Vorfilter, In-Memory Clip auf AOI,
        |  (download_common.py)    # alle MGRS-Kacheln, Upload nach Google Drive
        v
extract_timeseries.py    # Pro Datum: alle Kacheln zu AOI-Mosaik mergen (EPSG:4326),
        |                # Raster-Vektor-Verschneidung mit RGI-Gletschern, Qualitaetsfilter
        v
app.py (Streamlit)       # Interaktive Karte (Folium) + Zeitreihen (Plotly)

Statische Geodaten (einmalig):
  download_catchments.py   # HydroBASINS -> Einzugsgebiet-Polygon + AOI-Box -> static_data/
  download_glaciers.py     # RGI v7 Region 12 Gletscherpolygone -> static_data/
  download_rivers.py       # HydroRIVERS, Einzugsgebiet oberhalb Staudamm -> static_data/
  download_reservoirs.py   # HydroLAKES Stausee-Seed -> static_data/
  derive_reservoir.py      # S1-Wasserausdehnung -> echter Stausee-Footprint -> static_data/

S1-Orbit-Auswahl (einmalig pro AOI-Box, read-only / nur Stichprobe):
  probe_coverage.py        # Stufe A: Footprint-Coverage -> Kandidaten-Phasen (gratis)
                           # Stufe B (--sample N): N Testdateien je Phase laden,
                           #   echte valid_px_pct messen -> bester Orbit = s1_anchor
```

Die zentrale AOI-Definition (Clip-Box + S1-Anchor je AOI) liegt in `aoi_config.py` — eine einzige Quelle, aus der alle Skripte importieren.

### Einzugsgebiet als AOI (Catchment)

Statt einer groben Box ist das AOI das **Einzugsgebiet oberhalb des Damms**. `download_catchments.py` laedt HydroBASINS (lev12), lokalisiert das Sub-Basin am Damm (Pour-Point) und unioniert per Fliess-Topologie (`HYBAS_ID`/`NEXT_DOWN`) alle stromaufwaerts gelegenen Sub-Basins zu einem Catchment-Polygon (`static_data/catchments.geojson`). Dessen Bounding-Box (+ Puffer) ist die `clip_box` fuer den Download; das Polygon maskiert die Statistik in `extract_timeseries.py` — `valid_px_pct` wird dadurch **catchment-relativ** (Nenner = Catchment-Pixel, nicht die ganze Box). Das loest drei Dinge: (1) trimmt irrelevante Box-Ecken, (2) garantiert die volle Wasserscheide inkl. aller Zufluesse (Enguri: oestliche Svaneti-Quellfluesse, die die alte Box abschnitt), (3) macht Schnee/Gletscher hydrologisch sinnvoll — die Kazbek/Gergeti-Gletscher fallen bei Zhinvali korrekt raus (entwaessern in den Terek/Norden).

Drive-Ordnerstruktur: `OPERA_DSWx/{hls,s1}/{enguri,zhinvali}/`

### Mosaik-Ansatz

Stausee und Gletscher koennen in unterschiedlichen MGRS-Kacheln liegen (z.B. Zhinvali: Stausee im Sueden, Gletscher im Norden). Daher laedt `download_hls.py` / `download_s1.py` **alle** Kacheln der AOI (Dateiname mit MGRS-Kachel-ID), und `extract_timeseries.py` fuegt pro Datum alle Kacheln zu einem vollstaendigen AOI-Mosaik in EPSG:4326 zusammen (auch ueber UTM-Zonengrenzen hinweg). Das Mosaik wird exakt auf die AOI-Box **geclippt** (sonst erzeugen Kacheln aus verschiedenen UTM-Zonen NoData-Ecken, die die Abdeckung verfaelschen). Das garantiert volle Gebietsabdeckung und glaettet das Kachel-Rauschen.

### S1-Orbit-Dedup

SAR-Wasserklassifikation haengt von der Aufnahmegeometrie ab (Layover/Shadow je nach auf-/absteigendem Orbit). Mehrere Orbits in einer Zeitreihe erzeugen einen kuenstlichen Saegezahn. `extract_timeseries.py` reduziert S1 daher auf **einen** konsistenten relativen Orbit. Statt automatischer Auswahl ist der Orbit pro AOI **fest verankert** (`s1_anchor`: Enguri 2024-08-29, Zhinvali 2024-08-25). Es werden nur Tage dieser 12-Tage-Wiederholphase (`ordinal % 12`) behalten, sodass auch nur dieser Orbit heruntergeladen wird (~1/4 der Tage statt alle). Partielle Szenen (< 90% AOI) werden zusaetzlich verworfen. Ergebnis: saubere, geometrisch konsistente ~12-Tage-Reihe (Enguri 50, Zhinvali 52 Szenen).

**Orbit-Wahl (`probe_coverage.py`).** Zuerst Stufe A/B (Footprint + echte Pixel-Coverage an Testdateien) → Kandidaten-Phasen mit ~99% Abdeckung. Aber **Coverage ≠ Mess-Qualitaet fuers Reservoir**: ein zweiter Check (`--compare-orbit`) misst `reservoir_area_km2` einiger Tage eines Nachbar-Orbits und legt sie neben die bestehende Reihe. Der **1-Tag-Nachbarorbit** ist dabei ein Praezisions-Test — bei 1 Tag Abstand aendert sich der Pegel kaum, jede Differenz ist also reines Orbit-/Geometrie-Rauschen. Ergebnis: Reservoir-Fehlerschranke ~±0,2 km² (Zhinvali) bzw. ~±0,4 km² (Enguri). Fuer Enguri zeigte das, dass die per Coverage gewaehlte Phase im Herbst systematisch unter-detektierte (Geometrie-Artefakt) — daher Anchor auf den stabileren Orbit (2024-08-29) gesetzt.

### Reservoir-Footprint aus S1

Die `reservoir_area_km2` misst Wasser **nur innerhalb des Stausees**, getrennt von der AOI-weiten Wasserflaeche (die auch Fluesse enthaelt). Da HydroLAKES die Seen stark unterschaetzt, leitet `derive_reservoir.py` den Footprint aus den eigenen S1-Daten ab: ueber alle vollflaechigen Szenen wird eine **Wasser-Haeufigkeitskarte** akkumuliert; Pixel mit Wasser in >= 25% der Aufnahmen (occurrence-basiert, vgl. Pekel et al. 2016) bilden den Stausee, reduziert auf die mit dem HydroLAKES-Seed verbundene Komponente. Der Schwellenwert ist sensitivitaetsgeprueft (Flaeche aendert sich nur ±5–9% ueber 0,10–0,50, kein Fluss-Leck). Ergebnis (Catchment-AOI): Enguri 9,32 km², Zhinvali 11,20 km² (vs. real ~13 / ~11,5; Zhinvali = Punktlandung). Das Reservoir-Signal ist ~5x (Enguri) bis ~10x (Zhinvali) ruhiger als die AOI-Gesamtwasserflaeche und zeigt den saisonalen Speichergang (Zhinvali deutlich: ~8,5 km² Fruehjahr -> ~11 km² Herbst).

**Reservoir-Guard:** Ist an einem Datum weniger als 95% des Stausee-Footprints valide beobachtet (`reservoir_valid_pct`), wird `reservoir_area_km2` (und `water_km2`) auf `NaN` gesetzt — eine NoData-Luecke ueber dem See wuerde sonst als Scheinabsenkung erscheinen (so geschehen Enguri 2025-04-27, nur 45% des Sees beobachtet). Die Robustheit liegt damit in der Datenebene; das Dashboard zeigt eine einzelne Reservoir-Linie, NaN-Tage erscheinen als Luecke (kein Ueberbruecken). Hinweis: Enguri ist ein tiefer Schluchtspeicher — grosser Pegelhub bei kleiner Flaechenaenderung. Ein absoluter Wasserstand liesse sich daraus nur mit Bathymetrie ableiten; frei verfuegbare DEMs (Copernicus GLO-30) haben den Stausee als flache Wasserflaeche aufgenommen (keine Bathymetrie), und Satelliten-Altimetrie deckt diese kleinen Bergstauseen nicht ab. Daher wird der Speicher ueber die Flaeche ueberwacht, nicht ueber einen absoluten Pegel.

### Cache & Resume

`extract_timeseries.py` speichert jedes Datum-Ergebnis (auch geskippte, **mit den Stats**) in `static_data/cache/{site}_{s1,hls}.json`. Re-Laeufe ueberspringen bereits berechnete Daten — der teure Drive-Download passiert nur einmal. `--refresh` ignoriert den Cache, `--skip-s1` / `--skip-hls` laufen nur einen Sensor. `--recompute` liest nur die ergebnisrelevanten Tage neu (`ok` + unter den *aktuellen* Schwellen neu qualifizierende) und uebernimmt Wolken-/Unter-Schwelle-Skips aus dem Cache **ohne** Drive-Read — nach einer Logik-/Schwellen-Aenderung deutlich schneller (z.B. Zhinvali: 50 statt 249 Tage gelesen).

### Footprint-Vorfilter

Vor dem Download wird pro Datum die Vereinigung der Kachel-Footprints gegen das AOI geprueft. Nur Tage deren Kacheln zusammen >= 99% des AOI abdecken werden ueberhaupt heruntergeladen — Teilabdeckungs-Tage fallen weg, bevor Bandbreite verschwendet wird (reine Geometrie-Rechnung, Sekunden).

## Qualitaetsfilter

| Filter | Schwellenwert | Begruendung |
|--------|--------------|-------------|
| Catchment-Abdeckung (HLS) | >= 85% valide Pixel | Catchment-relativ. 85 (nicht 95), weil der oestliche Enguri-Zipfel oft am S2/Landsat-Swath-Rand liegt (~12% NoData trotz vorhandener Kacheln). Tiefer bringt kaum mehr — die restlichen Teiltage sind meist zusaetzlich bewoelkt. Echter Limiter ist die Bewoelkung (~70-75% der Tage) |
| Bewoelkung (HLS) | <= 30% Wolken im Catchment | Wissenschaftlich gaengiger Grenzwert fuer optische Fernerkundung; Wolke = WTR-Flag 253 |
| Catchment-Abdeckung (S1) | >= 90% | Entfernt Teil-Orbits; nur vollflaechige Szenen gehen in die Reihe |
| Reservoir-Abdeckung (S1) | >= 95% des Footprints | Sonst `reservoir_area_km2`/`water_km2` = NaN (keine Scheinabsenkung bei NoData ueber dem See) |

### Wasser aus S1, Schnee aus HLS

Optisches HLS ueber-detektiert Wasser stark (Geländeschatten/Eis werden als Wasser fehlklassifiziert) — auch an wolkenfreien Tagen. Daher: **Wasserflaeche ausschliesslich aus S1** (Radar, wolkenunabhaengig, robust), **Schnee/Gletscher aus HLS**. Die beiden Sensoren ergaenzen sich: HLS fuer das Schneesignal, S1 fuer die lueckenlose Wasser-Zeitreihe.

**HLS-Coverage: Sentinel-2 vs. Landsat (belegt).** Die fluktuierende HLS-Abdeckung (manche Tage ~99%, andere ~70%) ist sensor-, nicht zufallsbedingt: **Sentinel-2** (Swath 290 km) deckt das langgestreckte, zonenquerende Enguri-Becken zuverlaessig voll ab (jeder reine S2-Tag ≥ 85%), **Landsat-8/9** (Swath 185 km) nur teilweise (Median ~73%). Coverage-Auswertung pro Sensor: S2A 100%, S2C 99,7%, S2B 88%, L8 73,5%, L9 74,3%. Der 85%-Coverage-Filter behaelt damit faktisch die S2-abgedeckten Tage und verwirft die Landsat-only-Teilszenen — kein Eingriff noetig, aber eine dokumentierte optische Datengrenze (verschaerft in juengeren Monaten durch mehr Landsat-/S2C-Einzelpassagen).

## Berechnete Metriken (pro Datum und AOI)

| Spalte | Beschreibung |
|--------|-------------|
| `water_area_km2` | Offene Wasserflaeche im AOI (DSWx-Klassen 1-5). HLS-Spalte; fuers Wasser-Signal stattdessen `water_km2` aus der S1-Reihe verwenden |
| `seasonal_snow_km2` | Schneebedeckung ausserhalb der RGI-Gletscherpolygone (roh, absolut) |
| `seasonal_snow_frac` | Schneeanteil der **beobachteten** (validen, wolkenfreien) Nicht-Gletscher-Beckenflaeche — coverage-/wolken-robust |
| `seasonal_snow_km2_est` | `seasonal_snow_frac` x volle Nicht-Gletscher-Beckenflaeche — coverage-korrigierte saisonale Schneeflaeche (fuellt unbeobachtete Flaeche mit der beobachteten Schneerate; **Hauptspalte fuers Schneesignal**) |
| `snow_on_glacier_km2` | Schneebedeckung innerhalb der RGI-Gletscherpolygone |
| `bare_ice_km2` | Blankes Gletschereis (Gletscherflaeche minus Schneebedeckung) — Schmelzindikator |
| `glacier_total_km2` | Gesamtflaeche der RGI-Polygone im Catchment |
| `obs_land_pct` | Anteil der Nicht-Gletscher-Beckenflaeche, der an dem Tag valide+wolkenfrei beobachtet wurde (Vertrauensmass fuer die Schnee-Schaetzung) |
| `cloud_cover_percent` | Anteil bewoelkter Pixel im Catchment |
| `valid_px_pct` | Anteil valider (nicht-NoData) Pixel im Catchment |

S1-Reihe (`*_s1_timeseries.parquet`):

| Spalte | Beschreibung |
|--------|-------------|
| `water_km2` | Offene Wasserflaeche im gesamten Catchment (DSWx-Klassen 1-5) aus Radar — das Wasser-Signal (NaN, wenn Reservoir-Guard greift) |
| `reservoir_area_km2` | Wasserflaeche **nur im Stausee** (S1-abgeleiteter Footprint), ohne Fluesse — ruhiger, pegelrelevant (NaN, wenn See < 95% beobachtet) |
| `reservoir_valid_pct` | Anteil valide beobachteter Pixel **im Stausee-Footprint** (Basis des Reservoir-Guards) |
| `valid_px_pct` | Anteil valider Pixel im Catchment |

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
python download_catchments.py   # HydroBASINS Einzugsgebiet -> catchments.geojson + clip_box
                                # (clip_box-Werte in aoi_config.py eintragen)
python download_glaciers.py     # RGI v7 Gletscher (NSIDC, NASA-Login)
python download_rivers.py       # HydroRIVERS Einzugsgebiet (oeffentlich)
python download_reservoirs.py   # HydroLAKES Stausee-Seed (oeffentlich)

# 1b. Pro AOI-Box einmalig den S1-Orbit-Anchor bestimmen (zweistufig)
python probe_coverage.py             # Stufe A: nur Footprint-Screen (gratis)
python probe_coverage.py --sample 3  # Stufe B: 3 Testdateien je Kandidaten-Phase laden,
                                     #   echte valid_px_pct messen -> besten s1_anchor je AOI
                                     #   in aoi_config.py eintragen, DANN download_s1.py

# 2. Satellitendaten herunterladen und prozessieren
python download_hls.py          # OPERA DSWx-HLS (optisch) -> Google Drive
python download_s1.py           # OPERA DSWx-S1 (Radar)    -> Google Drive
python derive_reservoir.py      # S1-Wasserausdehnung -> echter Stausee-Footprint (einmalig nach S1-Download)
python extract_timeseries.py    # Mosaik + Zeitreihen -> *_timeseries.parquet (HLS) + *_s1_timeseries.parquet (S1)
                                # Optionen: --skip-s1 / --skip-hls / --refresh / --recompute

# 3. Dashboard starten
streamlit run app.py
```

## Skripte

| Skript | Funktion |
|--------|----------|
| `download_hls.py` | OPERA DSWx-HLS (optisch, nur B01_WTR — Wolke ueber Flag 253) nach Google Drive laden |
| `download_s1.py` | OPERA DSWx-S1 (Radar, B01_WTR) nach Google Drive laden. `orbit_filter`: laedt NUR den verankerten Orbit (`s1_anchor`, eine 12-Tage-Phase) -> ~1/4 der Tage |
| `download_common.py` | Gemeinsame Logik beider Downloads: Auth, Drive, Footprint-Vorfilter, **S1-Orbit-Vorfilter** (`orbit_phase`, nur Anchor-Phase), Clipping, MGRS-Namen. Robuster Download via requests-Session mit hartem Read-Timeout (kein Haengen) + Retry/Backoff fuer transiente 5xx/429 (wird nicht direkt ausgefuehrt) |
| `extract_timeseries.py` | Pro Datum Kachel-Mosaik bilden, auf das Catchment-Polygon maskieren (`valid_px_pct` catchment-relativ), S1 auf verankerten Orbit filtern, mit RGI-Gletschern + Stausee-Footprint verschneiden (`reservoir_area_km2`), Zeitreihen als CSV + Parquet speichern (mit Per-Datum-Cache) |
| `probe_orbits.py` | Diagnose (read-only): S1-Orbit-Metadaten pruefen (Satellit, Phase) — validiert das Orbit-Dedup |
| `probe_coverage.py` | Zweistufiger S1-Orbit-Selektor (vor Re-Download). Stufe A: Footprint-Coverage -> Kandidaten-Phasen. Stufe B (`--sample N`): echte `valid_px_pct` an Testdateien -> bester Orbit. `--compare-orbit YYYYMMDD`: misst `reservoir_area_km2` eines Nachbar-Orbits gegen die bestehende Reihe (Densification-/Praezisions-Check, 1-Tag-Nachbar = Geometrie-Rauschen). Auch Coverage-je-Sensor (S2 vs Landsat) ableitbar |
| `aoi_config.py` | Zentrale AOI-Definition (clip_box, dam, s1_anchor, Display-Felder) — Single Source of Truth, von allen Skripten importiert (kein Skript zum Ausfuehren) |
| `download_catchments.py` | HydroBASINS-Einzugsgebiet oberhalb des Damms ableiten (Pour-Point, Upstream-Union) -> catchments.geojson + neue clip_box |
| `download_glaciers.py` | RGI v7 Region 12 Gletscherpolygone von NSIDC laden (via earthaccess) |
| `download_rivers.py` | HydroRIVERS laden, auf Einzugsgebiet oberhalb des Staudamms filtern (Fliessnetz-Topologie), auf AOI clippen |
| `download_reservoirs.py` | HydroLAKES laden, Stausee-Seed-Polygon extrahieren (Ansatzpunkt fuer derive_reservoir.py) |
| `derive_reservoir.py` | Echten Stausee-Footprint aus S1-Wasserhaeufigkeit ableiten (occurrence-basiert, seed-verankert) -> reservoirs.geojson |
| `app.py` | Streamlit-Dashboard: Folium-Karte (AOI, Gletscher, Fluesse, Staudamm) + Plotly-Zeitreihen |

## Tech Stack

Python 3.11 | earthaccess | rasterio | rioxarray | geopandas | shapely | scipy | pydrive2 | pandas | pyarrow | tqdm | streamlit | plotly | folium | streamlit-folium

## Lizenz

MIT License
