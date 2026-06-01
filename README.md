# FROM SNOW TO RESERVOIR
### Satellite monitoring of the snow–glacier–reservoir water chain in the Georgian Greater Caucasus

Von Sebastian Macherey · [github.com/sebastianmry/from-snow-to-reservoir](https://github.com/sebastianmry/from-snow-to-reservoir)

---

## Motivation

Georgiens Stromversorgung haengt zu etwa 80 % von der Wasserkraft ab. Saisonale Schneedecke und Gletscher des Grossen Kaukasus steuern den Zufluss in die Talsperren — der Klimawandel veraendert dieses System fundamental. Beide untersuchten Stauseen liegen unmittelbar an den von Russland kontrollierten Gebieten Abchasien und Suedossetien.

## Areas of Interest

| AOI | Bbox (min_lon, min_lat, max_lon, max_lat) | Staudamm (lon, lat) | Objekte |
|-----|------------------------------------------|---------------------|---------|
| Enguri | 41.70, 42.55, 42.80, 43.15 | 42.032, 42.753 | Enguri Dam (271 m), starke Vergletscherung rund um Mestia, grenzt an Abchasien |
| Zhinvali | 44.30, 42.00, 45.15, 42.80 | 44.771, 42.133 | Zhinvali Dam (Trinkwasser Tbilisi), Gergeti-Gletscher / Kazbek (5 047 m), grenzt an Suedossetien |

## Daten

- **OPERA DSWx-HLS (Level-3):** Optische Wasser-/Schneeklassifikation (B01_WTR) aus Landsat-8/9 und Sentinel-2, ~2-3 Tage Revisit. Wolkenmaskierung ueber das WTR-eigene Flag (Wert 253) — kein separater B09-Layer noetig.
- **OPERA DSWx-S1 (Level-3):** Radar-basierte Wasserklassifikation (B01_WTR), wolkenunabhaengig. Auf einen konsistenten relativen Orbit reduziert (~12-Tage-Reihe).
- **Randolph Glacier Inventory v7 (RGI), Region 12:** Gletscher-Polygone fuer den Kaukasus (NSIDC, via `download_glaciers.py`)
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
  download_glaciers.py     # RGI v7 Region 12 Gletscherpolygone -> static_data/
  download_rivers.py       # HydroRIVERS, Einzugsgebiet oberhalb Staudamm -> static_data/
  download_reservoirs.py   # HydroLAKES Stausee-Seed -> static_data/
  derive_reservoir.py      # S1-Wasserausdehnung -> echter Stausee-Footprint -> static_data/
```

Drive-Ordnerstruktur: `OPERA_DSWx/{hls,s1}/{enguri,zhinvali}/`

### Mosaik-Ansatz

Stausee und Gletscher koennen in unterschiedlichen MGRS-Kacheln liegen (z.B. Zhinvali: Stausee im Sueden, Gletscher im Norden). Daher laedt `download_hls.py` / `download_s1.py` **alle** Kacheln der AOI (Dateiname mit MGRS-Kachel-ID), und `extract_timeseries.py` fuegt pro Datum alle Kacheln zu einem vollstaendigen AOI-Mosaik in EPSG:4326 zusammen (auch ueber UTM-Zonengrenzen hinweg). Das Mosaik wird exakt auf die AOI-Box **geclippt** (sonst erzeugen Kacheln aus verschiedenen UTM-Zonen NoData-Ecken, die die Abdeckung verfaelschen). Das garantiert volle Gebietsabdeckung und glaettet das Kachel-Rauschen.

### S1-Orbit-Dedup

SAR-Wasserklassifikation haengt von der Aufnahmegeometrie ab (Layover/Shadow je nach auf-/absteigendem Orbit). Mehrere Orbits in einer Zeitreihe erzeugen einen kuenstlichen Saegezahn. `extract_timeseries.py` reduziert S1 daher auf **einen** konsistenten relativen Orbit. Statt automatischer Auswahl ist der Orbit pro AOI **fest verankert** (`s1_anchor`: Enguri 2024-08-29, Zhinvali 2024-08-25 — die durchgehend vollflaechig abgedeckten Tracks): es werden nur Tage dieser 12-Tage-Wiederholphase (`ordinal % 12`) behalten, sodass auch nur dieser Orbit heruntergeladen wird (~1/4 der Tage statt alle). Partielle Szenen (< 90% AOI) werden zusaetzlich verworfen. Ergebnis: saubere, geometrisch konsistente ~12-Tage-Reihe.

### Reservoir-Footprint aus S1

Die `reservoir_area_km2` misst Wasser **nur innerhalb des Stausees**, getrennt von der AOI-weiten Wasserflaeche (die auch Fluesse enthaelt). Da HydroLAKES die Seen stark unterschaetzt, leitet `derive_reservoir.py` den Footprint aus den eigenen S1-Daten ab: ueber alle vollflaechigen Szenen wird eine **Wasser-Haeufigkeitskarte** akkumuliert; Pixel mit Wasser in >= 25% der Aufnahmen (occurrence-basiert, vgl. Pekel et al. 2016) bilden den Stausee, reduziert auf die mit dem HydroLAKES-Seed verbundene Komponente. Der Schwellenwert ist sensitivitaetsgeprueft (Flaeche aendert sich nur ±5–9% ueber 0,10–0,50, kein Fluss-Leck). Ergebnis: Enguri 9,86 km², Zhinvali 11,19 km² (vs. real ~13 / ~11,5). Das Reservoir-Signal ist ~5x (Enguri) bis ~10x (Zhinvali) ruhiger als die AOI-Gesamtwasserflaeche und zeigt den saisonalen Speichergang. Hinweis: Enguri ist ein tiefer Schluchtspeicher — grosser Pegelhub bei kleiner Flaechenaenderung. Ein absoluter Wasserstand liesse sich daraus nur mit Bathymetrie ableiten; frei verfuegbare DEMs (Copernicus GLO-30) haben den Stausee als flache Wasserflaeche aufgenommen (keine Bathymetrie), und Satelliten-Altimetrie deckt diese kleinen Bergstauseen nicht ab. Daher wird der Speicher ueber die Flaeche ueberwacht, nicht ueber einen absoluten Pegel.

### Cache & Resume

`extract_timeseries.py` speichert jedes Datum-Ergebnis (auch geskippte) in `static_data/cache/{site}_{s1,hls}.json`. Re-Laeufe ueberspringen bereits berechnete Daten — der teure Drive-Download passiert nur einmal. `--refresh` ignoriert den Cache, `--skip-s1` / `--skip-hls` laufen nur einen Sensor.

### Footprint-Vorfilter

Vor dem Download wird pro Datum die Vereinigung der Kachel-Footprints gegen das AOI geprueft. Nur Tage deren Kacheln zusammen >= 99% des AOI abdecken werden ueberhaupt heruntergeladen — Teilabdeckungs-Tage fallen weg, bevor Bandbreite verschwendet wird (reine Geometrie-Rechnung, Sekunden).

## Qualitaetsfilter

| Filter | Schwellenwert | Begruendung |
|--------|--------------|-------------|
| AOI-Abdeckung (HLS) | >= 95% valide Pixel | Verwirft Szenen ohne vollstaendige Gebietsabdeckung (relativ zur ganzen AOI dank Mosaik) |
| Bewoelkung (HLS) | <= 30% Wolken im AOI | Wissenschaftlich gaengiger Grenzwert fuer optische Fernerkundung; Wolke = WTR-Flag 253 |
| AOI-Abdeckung (S1) | >= 70% Sicherheitsnetz, >= 90% fuer Orbit-Auswahl | Entfernt Teil-Orbits; nur vollflaechige Szenen gehen in die Reihe |

### Wasser aus S1, Schnee aus HLS

Optisches HLS ueber-detektiert Wasser stark (Geländeschatten/Eis werden als Wasser fehlklassifiziert) — auch an wolkenfreien Tagen. Daher: **Wasserflaeche ausschliesslich aus S1** (Radar, wolkenunabhaengig, robust), **Schnee/Gletscher aus HLS**. Die beiden Sensoren ergaenzen sich: HLS fuer das Schneesignal, S1 fuer die lueckenlose Wasser-Zeitreihe.

## Berechnete Metriken (pro Datum und AOI)

| Spalte | Beschreibung |
|--------|-------------|
| `water_area_km2` | Offene Wasserflaeche im AOI (DSWx-Klassen 1-5). HLS-Spalte; fuers Wasser-Signal stattdessen `water_km2` aus der S1-Reihe verwenden |
| `seasonal_snow_km2` | Schneebedeckung ausserhalb der RGI-Gletscherpolygone |
| `snow_on_glacier_km2` | Schneebedeckung innerhalb der RGI-Gletscherpolygone |
| `bare_ice_km2` | Blankes Gletschereis (Gletscherflaeche minus Schneebedeckung) — Schmelzindikator |
| `glacier_total_km2` | Gesamtflaeche der RGI-Polygone im AOI |
| `cloud_cover_percent` | Anteil bewoelkter Pixel im AOI |
| `valid_px_pct` | Anteil valider (nicht-NoData) Pixel im AOI |

S1-Reihe (`*_s1_timeseries.parquet`):

| Spalte | Beschreibung |
|--------|-------------|
| `water_km2` | Offene Wasserflaeche im gesamten AOI (DSWx-Klassen 1-5) aus Radar — das Wasser-Signal |
| `reservoir_area_km2` | Wasserflaeche **nur im Stausee** (S1-abgeleiteter Footprint), ohne Fluesse — ruhiger, pegelrelevant |
| `valid_px_pct` | Anteil valider Pixel im AOI |

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
python download_reservoirs.py   # HydroLAKES Stausee-Seed (oeffentlich)

# 2. Satellitendaten herunterladen und prozessieren
python download_hls.py          # OPERA DSWx-HLS (optisch) -> Google Drive
python download_s1.py           # OPERA DSWx-S1 (Radar)    -> Google Drive
python derive_reservoir.py      # S1-Wasserausdehnung -> echter Stausee-Footprint (einmalig nach S1-Download)
python extract_timeseries.py    # Mosaik + Zeitreihen -> *_timeseries.parquet (HLS) + *_s1_timeseries.parquet (S1)
                                # Optionen: --skip-s1 / --skip-hls / --refresh

# 3. Dashboard starten
streamlit run app.py
```

## Skripte

| Skript | Funktion |
|--------|----------|
| `download_hls.py` | OPERA DSWx-HLS (optisch, nur B01_WTR — Wolke ueber Flag 253) nach Google Drive laden |
| `download_s1.py` | OPERA DSWx-S1 (Radar, B01_WTR) nach Google Drive laden |
| `download_common.py` | Gemeinsame Logik beider Downloads: Auth, Drive, Footprint-Vorfilter, Clipping, MGRS-Namen (wird nicht direkt ausgefuehrt) |
| `extract_timeseries.py` | Pro Datum Kachel-Mosaik bilden, S1 auf verankerten Orbit filtern, mit RGI-Gletschern + Stausee-Footprint verschneiden (`reservoir_area_km2`), Zeitreihen als CSV + Parquet speichern (mit Per-Datum-Cache) |
| `probe_orbits.py` | Diagnose (read-only): S1-Orbit-Metadaten pruefen (Satellit, Phase) — validiert das Orbit-Dedup |
| `download_glaciers.py` | RGI v7 Region 12 Gletscherpolygone von NSIDC laden (via earthaccess) |
| `download_rivers.py` | HydroRIVERS laden, auf Einzugsgebiet oberhalb des Staudamms filtern (Fliessnetz-Topologie), auf AOI clippen |
| `download_reservoirs.py` | HydroLAKES laden, Stausee-Seed-Polygon extrahieren (Ansatzpunkt fuer derive_reservoir.py) |
| `derive_reservoir.py` | Echten Stausee-Footprint aus S1-Wasserhaeufigkeit ableiten (occurrence-basiert, seed-verankert) -> reservoirs.geojson |
| `app.py` | Streamlit-Dashboard: Folium-Karte (AOI, Gletscher, Fluesse, Staudamm) + Plotly-Zeitreihen |

## Tech Stack

Python 3.11 | earthaccess | rasterio | rioxarray | geopandas | shapely | scipy | pydrive2 | pandas | pyarrow | tqdm | streamlit | plotly | folium | streamlit-folium

## Lizenz

MIT License
