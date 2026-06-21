# Datenbereinigung und Qualitätssicherung

Die Pipeline bereinigt die Satellitenzeitreihen auf mehreren Ebenen, bevor ein Wert
in das Dashboard gelangt. Die Logik sitzt überwiegend in `extract_timeseries.py`.

**Wolkenmaskierung (HLS, optisch):** Das Verfahren maskiert Wolken und
Wolkenschatten über das 253-Flag des WTR-Layers. Szenen mit mehr als 30 Prozent
Wolkenanteil verwirft es vollständig, NoData-Pixel (255) fließen nie in die
Statistik ein.

**Gültige Abdeckung statt Bounding Box:** Der Anteil gültiger Pixel
(`valid_px_pct`) bezieht sich auf das Einzugsgebiet, nicht auf die rechteckige
Kachel. Dadurch bleibt der Abdeckungsfilter inhaltlich aussagekräftig.

**Korrektur der Teilabdeckung:** Szenen am Swath-Rand decken das Gebiet nur
teilweise ab und würden die Schneefläche systematisch unterschätzen. Die
coverage-korrigierten Spalten (`*_est`) rechnen diesen Bias heraus, sodass Tage
mit Teilabdeckung mit Tagen voller Abdeckung vergleichbar werden.

**Orbit-Bereinigung (Sentinel-1, Radar):** Echte Teilszenen entfernt das Verfahren
zuerst (Schwelle 90 Prozent AOI-Abdeckung). Anschließend dedupliziert es die
Szenen nach der 12-tägigen Wiederholungsphase, um ein Vermischen aufsteigender und
absteigender Orbits zu vermeiden. Den versetzten S1C-Track schließt es aus.

**Schutz gegen falsche Pegeleinbrüche:** Liegt die gültige Abdeckung des
Reservoir-Umrisses an einem Datum unter 95 Prozent, setzt die Pipeline sowohl
`reservoir_area_km2` als auch `water_km2` auf NaN. Im Diagramm entsteht so eine
echte Lücke statt eines scheinbaren Pegelabfalls, da der Parameter
`connectgaps=False` die Lücke sichtbar lässt.

**Attributbereinigung der Gletscher:** Das Dashboard filtert leere Werte und
Katalog-Kennungen (etwa „198b") aus den RGI-Namen, sodass nur echte
Gletschernamen als Tooltip erscheinen.

Bewusst verzichtet das Projekt auf statistische Glättung oder
Ausreißer-Interpolation. Die Robustheit entsteht in der Daten- und Maskenebene,
nicht durch nachträgliche Kurvenkorrektur.
