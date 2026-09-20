# Entscheidungen

Kurzprotokoll von Design-Entscheidungen, die nicht direkt aus dem Code ersichtlich sind. Neue Einträge unten anhängen, mit Datum.

## 2026-09-14: Net Debt/EBITDA – Grauzonen-Band und Stichtagslogik

**Kontext:** Issue #3 (Net Debt/EBITDA) benötigt dieselbe Grauzonen-Logik wie Cash Conversion (Flag statt hartem Ja/Nein), aber mit einer offenen Frage zum Zeitbezug (Stichtag vs. 3-Jahres-Schnitt wie bei ROIC).

**Entscheidung 1 – Grauzonen-Band 2,3–2,5x:**
- `< 2,3x` → bestanden
- `2,3x ≤ x < 2,5x` → bestanden mit Flag (Grauzone)
- `≥ 2,5x` → durchgefallen

Die harte Schwelle aus Stufe 1 (`< 2,5x` = bestanden) bleibt die Ja/Nein-Grenze zwischen "durchgefallen" und "nicht durchgefallen". Das Flag-Band liegt **unterhalb** dieser Schwelle, nicht symmetrisch um sie herum, weil bei Net Debt/EBITDA niedriger besser ist – analog zur Cash-Conversion-Logik, wo das Flag-Band ebenfalls auf der "schlechteren" Seite der harten Schwelle liegt.

**Entscheidung 2 – Stichtagswert, kein 3-Jahres-Schnitt:**
Net Debt/EBITDA wird ausschließlich aus dem letzten verfügbaren Fiskaljahr (t) berechnet, nicht als 3-Jahres-Durchschnitt wie ROIC. Begründung: Verschuldung ist ein Bestandswert (Stichtag), kein Flussgrößen-Durchschnitt – ein Mittelwert über 3 Jahre würde eine reale aktuelle Überschuldung durch bessere Vorjahre verschleiern (oder umgekehrt).

**How to apply:** Die Formel in Issue #3 liest ausschließlich Periode t (letztes Jahr) für `net_debt` und `ebitda`, mittelt nicht. Die Gates-Layer-Implementierung (Issue #5) wendet das 2,3–2,5x-Band exakt wie oben an.

**Entscheidung 3 – EBITDA≤0-Guard, MetricResult-Konvention:**
Ist `ebitda` in der Stichtags-Periode (letztes Fiskaljahr) ≤ 0, ist die Kennzahl nicht sinnvoll berechenbar (Division durch ≤0 bzw. das Verhältnis kehrt sich inhaltlich um). Guard greift vor der Division, Rückgabe `MetricResult(value=None, invalid_reason="ebitda_non_positive")` — gleiches Muster wie bei ROIC (siehe Abschnitt "Verbindliches Rückgabeformat" unten).

**How to apply:** `metrics_layer/net_debt_ebitda.py` prüft `ebitda <= 0` vor der Division und gibt bei Verletzung `MetricResult(value=None, invalid_reason="ebitda_non_positive")` zurück; sonst `MetricResult(value=net_debt/ebitda)`.

**Entscheidung 4 – leere Perioden-Liste vs. doppelte Fiskaljahre (Review-Fund):**
Analog zur ROIC-Abgrenzung (Exception vs. `invalid_reason`, siehe unten): Eine **leere** Perioden-Liste ist eine gültige, wenn auch extreme Dateneigenschaft (z. B. Datenausfall) → `MetricResult(value=None, invalid_reason="no_periods")`. Enthält die Liste dagegen **zwei Perioden mit demselben Fiskaljahr** (z. B. ein ursprüngliches 10-K und ein korrigierendes 10-K/A, nicht dedupliziert), ist das ein Aufrufer-Fehler — `max()` würde sonst je nach Listenreihenfolge still ein beliebiges der beiden Duplikate wählen. Das wirft `MetricInputError`, nicht `invalid_reason`.

**How to apply:** `metrics_layer/net_debt_ebitda.py` prüft zuerst auf leere Liste (`invalid_reason="no_periods"`), dann auf doppelte `fiscal_year`-Werte (`MetricInputError`), erst danach `max(periods, key=fiscal_year)`.

## 2026-09-14: ROIC – effektiver Steuersatz, Mittelwertbildung und Invested-Capital-Guard

**Kontext:** Issue #2 (ROIC, 3-Jahres-Schnitt) braucht klare Regeln für die Steuersatz-Quelle, die Art der Durchschnittsbildung über 3 Jahre und den Umgang mit einem negativen/null Invested Capital, bevor `metrics_layer/roic.py` implementiert wird.

**Entscheidung 1 – effektiver Steuersatz je Firma/Jahr:**
NOPAT nutzt den in der Fixture/Datenquelle hinterlegten `tax_rate` je Firma und Fiskaljahr (den tatsächlichen effektiven Steuersatz), keinen pauschalen oder normalisierten Satz (z. B. keinen fixen 21%-Marginalsatz). NOPAT = `ebit × (1 − tax_rate)`.

**Entscheidung 2 – 3-Jahres-ROIC = Mittelwert der Jahres-ROICs:**
Der 3-Jahres-ROIC ist der arithmetische Mittelwert der drei **einzeln** berechneten Jahres-ROICs (`ROIC_2022`, `ROIC_2023`, `ROIC_2024`), **nicht** die Summe der drei NOPAT-Werte geteilt durch die Summe der drei Invested-Capital-Werte. Diese beiden Methoden liefern bei schwankendem Invested Capital unterschiedliche Ergebnisse; es gilt ausschließlich die Mittelwert-der-Verhältnisse-Methode.

**Entscheidung 3 – Invested-Capital≤0-Guard, ganze Firma geflaggt:**
Invested Capital = `equity + net_debt`. Ist Invested Capital in **irgendeinem** der drei Jahre ≤ 0, wird der **gesamte** 3-Jahres-ROIC dieser Firma als ungültig/geflaggt zurückgegeben – nicht nur das betroffene Jahr aus dem Mittelwert ausgeschlossen und über die verbleibenden 2 Jahre gemittelt. Der Guard greift bereits vor der Division (kein Berechnungsversuch mit ungültigem Nenner), analog zum `net_income > 0`-Guard bei Cash Conversion (Issue #4).

**How to apply:** `metrics_layer/roic.py` berechnet pro Jahr `ROIC = ebit × (1 − tax_rate) / (equity + net_debt)`, prüft je Jahr den Guard vor der Division, und mittelt nur, wenn alle drei Jahre einen gültigen Wert liefern. Testfixture für den Guard-Fall: `NAKA17` (siehe `tests/fixtures/README.md`).

**Entscheidung 4 – weniger als 3 Fiskaljahre => geflaggt, kein Mittelwert aus 2 Jahren:**
Liegen für eine Firma weniger als 3 gültige Fiskaljahre vor (z. B. kürzlicher Börsengang), wird der 3-Jahres-ROIC als ungültig/geflaggt zurückgegeben — analog zum Invested-Capital≤0-Guard. Es wird **nicht** aus 2 (oder 1) verfügbaren Jahren gemittelt und nicht auf 3 Jahre hochgerechnet. Der Guard prüft die Anzahl der Perioden vor jeder Berechnung, nicht nachträglich per try/except.

**How to apply:** `metrics_layer/roic.py` prüft `len(periods) == 3` als ersten Schritt in `roic_3yr_average()`, vor der Iteration über die Perioden. Testfixture: eine Firma mit nur 2 Fiskaljahren.

**Entscheidung 5 – mehr als 3 Perioden ist ein Aufrufer-Fehler, kein Flag:**
Erhält `roic_3yr_average()` **mehr** als 3 Perioden, ist das keine Firmen-Eigenschaft (wie z. B. zu wenige Jahre bei einem Börsengang), sondern ein Fehler des Aufrufers — typischerweise nicht deduplizierte Mehrfach-Filings desselben Fiskaljahrs (z. B. eine 10-K/A-Korrektur neben dem ursprünglichen 10-K). Das gehört nicht als `invalid_reason` ins `MetricResult` (das ist für gültige, aber ungünstige Dateneigenschaften einer Firma reserviert), sondern wird als Exception behandelt: `MetricInputError` (definiert in `metrics_layer/result.py`). `insufficient_years` bleibt dagegen ein normales `MetricResult`-Flag, weil zu wenige Jahre real und erwartbar sind (Börsengang) — kein Aufrufer-Fehler.

**How to apply:** `metrics_layer/roic.py` wirft `MetricInputError`, wenn `len(periods) > 3`. `data_layer`-Adapter (Issue #6, sobald vorhanden) müssen Mehrfach-Filings pro Fiskaljahr vor dem Aufruf auf die kanonische Version deduplizieren — siehe Kommentar auf Issue #6.

## 2026-09-14: Verbindliches Rückgabeformat für alle Stufe-1-Metriken — MetricResult statt nacktem None

**Kontext:** ROIC (Issue #2), Net Debt/EBITDA (Issue #3) und Cash Conversion (Issue #4) haben alle Guard-Fälle, die aktuell (bzw. im Fall von #3/#4: geplant) einen nackten `None` zurückgeben. Ein nackter `None` sagt nichts darüber aus, *warum* eine Metrik ungültig ist (Invested Capital ≤ 0? Zu wenig Fiskaljahre? net_income ≤ 0?) — das erschwert Debugging und den Rang-Output im Gates-Layer (Issue #5), der unterschiedliche Ausfallgründe unterschiedlich behandeln oder zumindest anzeigen können muss.

**Entscheidung:** Jede Stufe-1-Metrik-Funktion gibt ein `MetricResult` zurück (definiert in `metrics_layer/result.py`, ein `@dataclass(frozen=True)` mit den Feldern `value: float | None` und `invalid_reason: str | None`):
- Gültiges Ergebnis: `MetricResult(value=<float>)` (`invalid_reason` bleibt `None`)
- Ungültiges Ergebnis: `MetricResult(value=None, invalid_reason=<kurzer, stabiler String>)`

`invalid_reason` ist ein kurzer, stabiler, maschinenlesbarer String (snake_case), kein Freitext — z. B. `"ic_non_positive"`, `"insufficient_years"`, `"net_income_non_positive"`. Jede Guard-Bedingung bekommt ihren eigenen, unterscheidbaren Reason-String, damit z. B. "IC≤0" und "zu wenig Jahre" bei ROIC nicht beide als undifferenziertes `"invalid"` erscheinen.

**How to apply — verbindlich für alle Stufe-1-Metriken, auch die noch nicht implementierten:**
- `metrics_layer/roic.py` (Issue #2, umgesetzt): `"ic_non_positive"`, `"insufficient_years"`, `"undefined_tax_rate"`
- `metrics_layer/net_debt_ebitda.py` (Issue #3, umgesetzt): `"ebitda_non_positive"`
- `metrics_layer/cash_conversion.py` (Issue #4, umgesetzt): `"ni_non_positive"`, `"insufficient_years"`

Kein `None` als alleiniger Rückgabewert irgendeiner Stufe-1-Metrik-Funktion — immer `MetricResult`.

**Abgrenzung zu Exceptions:** `invalid_reason` ist ausschließlich für gültige, aber ungünstige *Dateneigenschaften einer Firma* (z. B. zu wenig Jahre, negatives Invested Capital, EBITDA ≤ 0). Ein fehlerhafter *Aufruf* der Funktion selbst (z. B. zu viele Perioden durch nicht deduplizierte Mehrfach-Filings, siehe Entscheidung 5 oben) ist kein `invalid_reason`, sondern eine `MetricInputError`-Exception. Diese Abgrenzung gilt für alle Stufe-1-Metriken.

## 2026-09-14: Cash Conversion – 3-Jahres-Schnitt, NI≤0-Guard, Grauzone

**Kontext:** Issue #4 (Cash Conversion, `cfo / net_income`) braucht denselben Zeitbezug-Entscheid wie Net Debt/EBITDA (Stichtag vs. 3-Jahres-Schnitt), den Guard für den Fall `net_income ≤ 0`, und die Einordnung der Grauzone 0,8–1,0, bevor `metrics_layer/cash_conversion.py` implementiert wird.

**Entscheidung 1 – 3-Jahres-Schnitt der Jahres-Verhältnisse, kein Stichtag:**
Cash Conversion ist der arithmetische Mittelwert der drei **einzeln** berechneten Jahres-Verhältnisse `cfo/net_income` (nicht `sum(cfo) / sum(net_income)`) — dieselbe Methode wie bei ROIC (siehe Entscheidung 2 im ROIC-Abschnitt oben), aus demselben Grund: Cash Conversion ist eine Ertragskennzahl (Flussgröße über ein Jahr), kein Bilanz-Zustand zu einem Stichtag wie Net Debt/EBITDA, das laut seiner Entscheidung 2 bewusst nicht gemittelt wird.

**How to apply:** `metrics_layer/cash_conversion.py` berechnet pro Jahr `cfo / net_income`, mittelt danach über die drei Jahresverhältnisse.

**Entscheidung 2 – NI≤0-Guard flaggt den gesamten 3-Jahres-Wert:**
Ist `net_income` in **irgendeinem** der drei Jahre ≤ 0, wird der **gesamte** 3-Jahres-Wert dieser Firma als ungültig/geflaggt zurückgegeben (`invalid_reason="ni_non_positive"`) — nicht nur das betroffene Jahr aus dem Mittelwert ausgeschlossen und über die verbleibenden 2 Jahre gemittelt. Der Guard greift vor der Division, analog zum Invested-Capital≤0-Guard bei ROIC (Entscheidung 3 oben) und zum EBITDA≤0-Guard bei Net Debt/EBITDA (Entscheidung 3 im Net-Debt/EBITDA-Abschnitt).

**How to apply:** `metrics_layer/cash_conversion.py` prüft `net_income <= 0` je Jahr vor der Division und gibt bei Verletzung sofort `MetricResult(value=None, invalid_reason="ni_non_positive")` für den gesamten 3-Jahres-Wert zurück. Testfixtures: `MDYN06`, `HOOL07`, `WONK08` (siehe `tests/fixtures/README.md`) — alle drei haben `net_income > 0` in 2022/2023 und verletzen den Guard nur in 2024, decken also denselben "1 von 3 Jahren" Fall ab wie `NAKA17` bei ROIC.

**Entscheidung 3 – Perioden-Guards analog ROIC (insufficient_years / MetricInputError):**
Cash Conversion hat dieselbe "3 einzeln geguardete Jahre"-Struktur wie ROIC (anders als der Stichtags-Ansatz bei Net Debt/EBITDA) und übernimmt deshalb dieselben Perioden-Guards: Weniger als 3 Perioden flaggt `invalid_reason="insufficient_years"` (kein Mittelwert aus 2 Jahren), mehr als 3 Perioden wirft `MetricInputError` (Aufrufer-Fehler, kein Flag) — siehe ROIC-Entscheidungen 4 und 5 oben, hier unverändert übernommen.

**How to apply:** `metrics_layer/cash_conversion.py` prüft `len(periods) > 3` (→ `MetricInputError`) und `len(periods) < 3` (→ `invalid_reason="insufficient_years"`) vor der Iteration, exakt wie `roic_3yr_average()`.

**Entscheidung 3b – doppelte Fiskaljahre bei Perioden-Anzahl 3 (Review-Fund):** Wie bei Net Debt/EBITDA (siehe dessen Entscheidung 4) reicht eine reine Anzahl-Prüfung nicht: 3 Perioden, von denen zwei dasselbe `fiscal_year` tragen (ein echtes drittes Jahr fehlt dabei), bestehen sowohl die `>3`- als auch die `<3`-Prüfung und würden sonst unbemerkt mit doppeltem Gewicht für das duplizierte Jahr gemittelt. `cash_conversion_3yr_average()` prüft deshalb zusätzlich `len(fiscal_years) != len(set(fiscal_years))` (unabhängig von der Perioden-Anzahl) und wirft `MetricInputError`, bevor über die Jahre iteriert wird.

**Update 2026-09-14 (fix-stage1-consistency):** Derselbe Fix wurde in `roic_3yr_average()` nachgezogen — der Check ist jetzt konsistent in allen drei Stufe-1-Metriken vorhanden (`roic.py`, `net_debt_ebitda.py`, `cash_conversion.py`).

**Entscheidung 4 – MetricResult-Muster:**
`cash_conversion.py` folgt von Anfang an dem verbindlichen `MetricResult(value, invalid_reason)`-Rückgabeformat (siehe Abschnitt "Verbindliches Rückgabeformat" oben) — kein nackter `None`, `invalid_reason` ∈ `{"ni_non_positive", "insufficient_years"}`.

**Entscheidung 5 – Grauzone 0,8–1,0, Richtung durch Masterplan festgelegt:**
[`docs/masterplan.md`](masterplan.md) (§4, Stufe 1) legt die Richtung explizit fest: `CFO / Net Income ≥ 1,0` → sauber bestanden (kein Flag); `0,8 ≤ x < 1,0` → bestanden mit Flag (Grauzone); `< 0,8` → durchgefallen. Beide Bandgrenzen sind nach unten inklusiv (`≥ 1,0` bzw. `≥ 0,8`), es gibt keine dritte, symmetrisch offene Grenze. Das Grauzonen-Band liegt damit **unterhalb** der sauberen Bestehens-Schwelle (1,0), nicht symmetrisch unentschieden um sie herum — dasselbe Muster wie beim ROIC-Grauzonenband (10–12 %, unterhalb der sauberen 12 %-Schwelle; auch dort ist bislang nur das Band selbst dokumentiert und fixture-getestet, nicht die Klassifizierungslogik) und strukturell analog zu Net Debt/EBITDA (dort liegt das Flag-Band ebenfalls auf der schlechteren Seite der harten Schwelle, siehe dessen Entscheidung 1 oben — bei Net Debt/EBITDA ist "schlechter" nur numerisch höher statt niedriger, weil dort niedriger besser ist).

Offen bleibt ausschließlich die **Bucket-Klassifizierungslogik selbst** (die konkrete if/else-Implementierung, die einen rohen Wert einem der drei Fälle zuordnet) — das ist Aufgabe des Gates-Layers (Issue #5). `metrics_layer/cash_conversion.py` gibt weiterhin nur den rohen 3-Jahres-Mittelwert zurück und klassifiziert nicht selbst.

**How to apply:** `metrics_layer/cash_conversion.py` implementiert weiterhin keine Schwellenwert-Logik — das bleibt Issue #5. Testfixtures: `SPCL20` (3-Jahres-Schnitt = 0,90, Grauzone), `OSCP21` (0,80 — fällt selbst noch unter `0,8 ≤ x`, also Grauzone/Flag, **nicht** durchgefallen) und `DUFF22` (1,00 — fällt selbst schon unter `x ≥ 1,0`, also sauber bestanden, **nicht** mehr Grauzone); siehe `tests/fixtures/README.md`.

## 2026-09-14: Grenzwert-Konvention für alle Grauzonen-Bänder

**Kontext:** Die Randwert-Zuordnung (liegt der exakte Schwellenwert selbst noch in der Grauzone oder schon im Nachbar-Bucket?) wurde bisher pro Metrik einzeln hergeleitet — zuletzt für Cash Conversion (siehe Entscheidung 5 oben). Damit Issue #5 (Gates-Layer) das nicht für jede Metrik neu erraten muss, wird hier die allgemeine Regel festgehalten, aus der sich die Randwert-Zuordnung für jede Stufe-1-Metrik mechanisch ableiten lässt.

**Regel:** Für jede vom Masterplan (oder einer bereits getroffenen Entscheidung) explizit mit einem Vergleichsoperator benannte Schwelle gilt: Ein **inklusiver** Operator (`≥`/`≤`) ordnet den exakten Schwellenwert dem Bucket zu, das die Bedingung erfüllt; ein **strikter** Operator (`>`/`<`) ordnet ihn dem angrenzenden Bucket zu, das die Bedingung *nicht* erfüllt. Das gilt unabhängig davon, welche der beiden Grauzonen-Grenzen die benannte Schwelle markiert — die Grenze zwischen "sauber bestanden" und Grauzone (ROIC, Cash Conversion) oder die Grenze zwischen Grauzone und "durchgefallen" (Net Debt/EBITDA, siehe unten). Die Grauzone grenzt in jedem Fall exklusiv an die benannte Schwelle an, nie inklusiv auf beiden Seiten gleichzeitig — für jeden Randwert gibt es also genau eine korrekte Zuordnung, nicht zwei mögliche.

- **ROIC:** Masterplan-Schwelle `> 12 %` (strikt) markiert die Grenze *sauber bestanden ↔ Grauzone* ⇒ 12 % selbst erfüllt `> 12 %` nicht und ist damit Flag (Grauzone), nicht sauber bestanden. Beleg: `INGN23` (exakt 12,0 % in jedem der 3 Jahre, Flag) — siehe `tests/fixtures/README.md`. Die andere Grenze (*Grauzone ↔ durchgefallen*, bei 10 %) benennt der Masterplan nicht mit einem eigenen Vergleichsoperator — analog zur unteren Net-Debt/EBITDA-Grenze (2,3×, siehe unten) gilt hier deshalb dieselbe Standard-Richtung wie bei den beiden anderen Metriken: die Grauzone ist auf der "schlechteren" Seite inklusiv, die harte Schwelle exklusiv. `< 10 %` durchgefallen, `10 % ≤ x ≤ 12 %` Flag — 10 % selbst also noch Flag, nicht durchgefallen. Beleg: `SIRI24` (exakt 10,0 % in jedem der 3 Jahre, Flag) — siehe `tests/fixtures/README.md`.
- **Cash Conversion:** Masterplan-Schwelle `≥ 1,0` (inklusiv) markiert dieselbe Grenzart wie bei ROIC, *sauber bestanden ↔ Grauzone* ⇒ 1,0 selbst erfüllt `≥ 1,0` und ist damit sauber bestanden, nicht mehr Grauzone. Beleg: `DUFF22` (1,00, sauber bestanden) und `OSCP21` (0,80, Flag) — siehe `tests/fixtures/README.md`.
- **Net Debt/EBITDA:** Hier markiert die benannte Schwelle die *andere* Grenzart — `< 2,5×` (strikt) ist laut Entscheidung 1 oben die Grenze *Grauzone ↔ durchgefallen* (nicht *sauber bestanden ↔ Grauzone*; die saubere Bestehens-Grenze liegt separat bei 2,3×, nicht im Masterplan-Text benannt) ⇒ 2,5× selbst erfüllt `< 2,5×` nicht und ist damit durchgefallen, nicht mehr Grauzone. Beleg: `GEKO12` (2,50×, durchgefallen) und `VULC14` (2,30×, Flag) — siehe `tests/fixtures/README.md`.

**How to apply:** Issue #5 (Gates-Layer) übernimmt pro Metrik die explizit benannte Schwelle (aus Masterplan oder Entscheidung) samt ihrem Vergleichsoperator; zuerst klären, *welche* der beiden Grauzonen-Grenzen sie markiert (siehe die drei Beispiele oben), dann bestimmt der Operator (strikt `>`/`<` vs. inklusiv `≥`/`≤`) mechanisch, welchem Bucket der exakte Randwert zufällt — keine metrikspezifische Neubewertung der Richtung nötig. Neue Stufe-1-Metriken übernehmen dieselbe Konvention.

## 2026-09-15: Gates-Layer — Kombinationslogik, invalid_reason-Vorrang, Rang statt Score

**Kontext:** Issue #5 braucht, bevor `gates_layer/stage1_gates.py` geschrieben wird, eine verbindliche Antwort auf drei Fragen, die einzeln pro Metrik nicht beantwortbar sind: Wie kombinieren sich die drei Einzelergebnisse (ROIC, Net Debt/EBITDA, Cash Conversion) zu einem Gesamtstatus? Wie wird ein `invalid_reason` einer oder mehrerer Metriken behandelt? Wie sieht der geforderte "Rang statt Ja/Nein" (siehe `docs/masterplan.md` §4) konkret aus, ohne einen zusammengesetzten Score einzuführen?

**Entscheidung 1 – Kombinationslogik: härtestes Einzelergebnis gewinnt:**
Jede der drei Metriken wird unabhängig in genau einen von drei Buckets klassifiziert (bestanden / Flag(Grauzone) / durchgefallen), nach der Grenzwert-Konvention oben. Der Gesamtstatus der Firma:
- Mindestens eine Metrik "durchgefallen" ⇒ Firma insgesamt `Status = durchgefallen`, unabhängig vom Zustand der anderen beiden — eine durchgefallene Metrik disqualifiziert, egal wie gut die übrigen sind.
- Keine durchgefallene, aber mindestens eine im Flag-Band ⇒ `Status = bestanden mit Flag(s)`. Die Flag-Liste enthält **alle** zutreffenden Metriken (nicht nur die erste gefundene), in fester Reihenfolge `roic`, `net_debt_ebitda`, `cash_conversion` — `BLKW13` (ROIC- und Net-Debt/EBITDA-Grauzone gleichzeitig) ist der Testfall dafür.
- Keine durchgefallene, keine im Flag-Band ⇒ `Status = bestanden`, `flags = []`.

Die Flag-Liste wird immer vollständig berechnet, auch wenn der Status wegen einer dritten, durchgefallenen Metrik ohnehin `durchgefallen` ist — das dient ausschließlich der Transparenz in den ausgegebenen Rohdaten (man sieht z. B., dass eine durchgefallene Firma zusätzlich in einer anderen Dimension knapp war), ändert aber nichts am Status selbst.

**Entscheidung 2 – `invalid_reason` hat Vorrang vor Bucket-Klassifizierung, eigener Status:**
Hat mindestens eine der drei `MetricResult`-Eingaben einen gesetzten `invalid_reason` (aus `ic_non_positive`, `undefined_tax_rate`, `ebitda_non_positive`, `ni_non_positive`, `insufficient_years`; `no_periods` fällt in dieselbe Kategorie, auch wenn es in den Fixtures nicht vorkommt), wird die Firma **nicht** in bestanden/Flag/durchgefallen einsortiert — sie bekommt den eigenen, exklusiven Status `nicht bewertbar`, mit der Liste aller aufgetretenen Gründe (es können mehrere Metriken gleichzeitig ungültig sein, z. B. `insufficient_years` bei einem Börsengang trifft sowohl ROIC als auch Cash Conversion, da beide 3-Jahres-Schnitte sind). `nicht bewertbar` wird nirgends mit bestandenen/geflaggten Firmen vermischt — insbesondere landet eine solche Firma **nicht** im Rang (siehe Entscheidung 3).

**Entscheidung 3 – kein zusammengesetzter Score, Rang = Gruppierung nach Flag-Anzahl:**
Es gibt keine gewichtete oder summierte Kennzahl über die drei Metriken. Der Rang-Output gruppiert ausschließlich die Firmen mit Status `bestanden` oder `bestanden mit Flag(s)`, nach Anzahl der Flags aufsteigend (0 vor 1 vor 2, mechanisch nach `len(flags)`, nicht hartkodiert auf maximal 2), innerhalb einer Gruppe alphabetisch nach Ticker. Durchgefallene und nicht bewertbare Firmen erscheinen **nicht** im Rang-Ergebnis — wer sie sehen will, greift auf die einzelnen `GateResult`-Objekte zu, die weiterhin für jede Firma (auch durchgefallen/nicht bewertbar) alle drei Rohwerte mitführen, wo vorhanden.

**Entscheidung 4 – Schwellen bleiben in Python-Konstanten, nicht YAML:**
`docs/masterplan.md` §6/§7 sieht Config-getriebene Schwellen (YAML) vor — das ist aber laut Roadmap explizit erst **Phase 1.1** ("Härtung"), während Issue #5 zu Phase 1.0 gehört. `gates_layer/stage1_gates.py` verwendet deshalb Modul-Konstanten (dieselben Werte wie in der Grenzwert-Konvention oben), keine YAML-Datei. Das weicht bewusst von der allgemeinen `CLAUDE.md`-Konvention ("Schwellenwerte gehören in YAML-Dateien") für diese eine Phase ab; die Migration ist expliziter Bestandteil von Phase 1.1, kein Nachholbedarf jetzt.

**Entscheidung 5 – Statuswerte als kurze, stabile Strings:**
Analog zum `invalid_reason`-Muster (siehe Abschnitt "Verbindliches Rückgabeformat" oben) sind die vier Gesamtstatus-Werte kurze, stabile, maschinenlesbare snake_case-Strings, kein Freitext und kein Enum-Typ: `"passed"`, `"passed_with_flags"`, `"failed"`, `"not_evaluable"`. Die einzelnen Flags in der Flag-Liste sind ebenfalls Strings, benannt wie die Metrik-Module: `"roic"`, `"net_debt_ebitda"`, `"cash_conversion"`.

**How to apply:** `gates_layer/stage1_gates.py` definiert `evaluate_stage1_gates(roic, net_debt_ebitda, cash_conversion) -> GateResult` (nimmt drei `MetricResult`-Objekte, wendet zuerst den `invalid_reason`-Vorrang aus Entscheidung 2 an, sonst die Bucket-Klassifizierung + Kombinationslogik aus Entscheidung 1) und `rank_companies(gate_results) -> list[tuple[str, GateResult]]` (Gruppierung/Sortierung aus Entscheidung 3). `GateResult` trägt `status`, `flags`, `invalid_reasons` und alle drei Rohwerte (`roic`, `net_debt_ebitda`, `cash_conversion`, je `float | None`).

## 2026-09-15: Store-Layer (Issue #6) — Scope, Schema, CSV-Export

**Kontext:** `docs/masterplan.md` §3 beschriftet den Store-Layer im Architektur-Diagramm als "SQLite ── Cache + Forward-Log". Issue #6 muss vor der Implementierung klären, was davon jetzt (Phase 1.0) gebaut wird und was erst später (Phase 1.1) folgt, welche Firmen überhaupt archiviert werden, und wie Schema + CSV-Export konkret aussehen.

**Entscheidung 1 – Scope: nur Lauf-Archiv/Forward-Log, kein Rohdaten-Caching:**
Issue #6 umfasst ausschließlich das Lauf-Archiv (Forward-Log) — also das Wegschreiben der Gates-Layer-Ergebnisse pro Lauf. Das Caching von Rohdaten (EDGAR/secfinapi-Antworten, um wiederholte Abrufe zu vermeiden) ist laut Roadmap (`docs/masterplan.md` §7) explizit Teil von **Phase 1.1** ("Härtung": *"Caching-Layer"*), nicht Teil dieses Issues. `store_layer/db.py` enthält deshalb keinerlei Rohdaten-Cache-Logik — das ist eine bewusste Abgrenzung, kein Vergessen.

**Entscheidung 2 – archiviert wird nur, wer überlebt:**
Das SQLite-Archiv speichert ausschließlich Firmen mit Status `passed` oder `passed_with_flags` pro Lauf — mit Zeitstempel, allen drei Rohwerten (ROIC, Net Debt/EBITDA, Cash Conversion), Status, Flags und Rang-Position. Durchgefallene und nicht bewertbare Firmen werden **nicht** archiviert. Begründung: `docs/masterplan.md` §8 spricht explizit von "überlebende Ticker" im Archiv ("Jeder Lauf schreibt Datum + überlebende Ticker + deren Kennzahlen ins SQLite-Archiv"), nicht von allen gescreenten Firmen. Das ist zugleich genau die Filterung, die `gates_layer.rank_companies()` bereits leistet (siehe Entscheidung 3 im Gates-Layer-Abschnitt oben) — `store_layer` erfindet die Überlebenden-Definition nicht neu, sondern nutzt `rank_companies()` als einzige Quelle dafür, wer ein Überlebender ist und welche Rang-Position er hat.

**Entscheidung 3 – Schema: eine flache Tabelle `run_results`:**
Bei diesem Umfang (ein Lauf pro Cron-Ausführung, keine komplexen Lauf-Metadaten) reicht eine einzige Tabelle, kein separates `runs`-Table:

```sql
CREATE TABLE run_results (
    run_timestamp   TEXT NOT NULL,   -- ISO-Datum+Zeit, z. B. "2026-09-15T14:30:00"
    ticker          TEXT NOT NULL,
    roic_3yr        REAL NOT NULL,
    net_debt_ebitda REAL NOT NULL,
    cash_conversion_3yr REAL NOT NULL,
    status          TEXT NOT NULL,   -- "passed" | "passed_with_flags"
    flags           TEXT NOT NULL,   -- kommagetrennt, z. B. "roic,net_debt_ebitda"; "" bei 0 Flags
    rank_position   INTEGER NOT NULL,
    PRIMARY KEY (run_timestamp, ticker)
)
```

`run_timestamp` ist Datum **und** Uhrzeit (nicht nur Datum), falls an einem Tag mehrfach gelaufen wird (z. B. manuelle Re-Runs während der Entwicklung). Der Composite Key `(run_timestamp, ticker)` verhindert doppelte Zeilen für dieselbe Firma im selben Lauf. `rank_position` ist die 1-basierte Position in der von `rank_companies()` zurückgegebenen Gesamtliste (nicht pro Flag-Gruppe neu bei 1 beginnend) — z. B. bei 6 Firmen mit 0 Flags und 3 mit 1 Flag steht die erste 1-Flag-Firma auf Position 7, nicht auf Position 1.

**Entscheidung 4 – Insert, kein Upsert:** `save_run_results()` fügt Zeilen nur per `INSERT` ein, nicht `INSERT OR REPLACE`. Ein Forward-Log ist ein historisches Protokoll — ein zweiter `save_run_results()`-Aufruf mit demselben `run_timestamp` (z. B. ein Programmierfehler, der denselben Lauf zweimal loggt) soll laut per Composite-Key erzwungener `UNIQUE`-Verletzung laut auffallen (`sqlite3.IntegrityError`), statt eine bestehende historische Zeile still zu überschreiben.

**Entscheidung 5 – CSV-Export: ein Snapshot pro Lauf, gitignorter Ordner:**
`export_survivors_csv()` schreibt dieselbe gefilterte/gerankte Liste als CSV, ein File pro Lauf, Dateiname mit Zeitstempel: `survivors_<run_timestamp ohne ":">.csv` (z. B. `survivors_2026-09-15T143000.csv` — Doppelpunkte raus, da sie unter Windows in Dateinamen ungültig sind). Landet in `data/runs/`, DB-Datei in `data/run_archive.db` — beide unter dem neuen, gitignorten `data/`-Ordner, damit weder historische Lauf-CSVs noch die SQLite-Datei versehentlich eingecheckt werden.

**Entscheidung 6 – akzeptierter Kompromiss: ein Lauf ohne Überlebende ist nicht von "nie gelaufen" unterscheidbar (Review-Fund):**
Da nur Überlebende archiviert werden (Entscheidung 2) und es bewusst keine separate `runs`-Tabelle gibt (Entscheidung 3), hinterlässt ein realer Lauf mit 0 Überlebenden keine Zeile in `run_results` — genau wie ein `run_timestamp`, für den nie ein Lauf stattfand. Eine reine `run_results`-Abfrage kann diese beiden Fälle nicht unterscheiden. Bei diesem Umfang (Issue #6, ~20–30 Ticker, seltene Cron-Läufe) ist das ein bewusst akzeptierter Kompromiss, kein Versehen — eine separate `runs`-Tabelle nur für dieses Signal wäre verfrühte Komplexität. Sollte das relevant werden, ist der natürliche Ort dafür der Cron-Wrapper aus Issue #9 (z. B. eigenes Lauf-Log), nicht `store_layer/db.py`.

**Revidiert (2026-09-15, `fix-zero-survivor-tracking`):** Dieser Kompromiss wurde zurückgenommen — siehe den neuen Abschnitt "Lauf-Nachweis unabhängig von Survivor-Zahl" unten. Ein 0-Survivor-Lauf ist laut Masterplan Abschnitt 4 ("Kein Zwangskauf") ein explizit erwartetes, gültiges Ergebnis, kein Rand- oder Fehlerfall — dafür braucht es doch einen Nachweis innerhalb von `store_layer/db.py` selbst, nicht erst später in Issue #9.

**Entscheidung 7 – Default-Pfade als Python-Konstanten, kein Widerspruch zur YAML-Konvention (Review-Fund):**
`DEFAULT_DB_PATH`/`DEFAULT_CSV_DIR` sind Python-Konstanten, nicht in `config/*.yaml` — anders als bei den Gates-Layer-Schwellen (siehe oben, Entscheidung 4 im Gates-Layer-Abschnitt) ist das hier aber keine bewusste Phase-1.1-Verschiebung, sondern schlicht unnötig: Beide Werte sind bereits über die Funktionsparameter `db_path`/`output_dir` von jedem Aufrufer überschreibbar, ohne `store_layer/db.py` anzufassen — genau das Problem, das die `CLAUDE.md`-Konvention ("Schwellenwerte... niemals hart im Code") verhindern soll (eine Neukalibrierung ohne Code-Änderung), ist hier bereits gelöst. Die Konstanten sind zusätzlich an das Projekt-Root verankert (`Path(__file__).resolve().parent.parent.parent`), nicht an das Arbeitsverzeichnis zur Laufzeit — relevant, weil dieser Code laut Masterplan als Cron-Batch-Job auf dem VPS läuft und ein Cron-Arbeitsverzeichnis nicht garantiert das Repo-Root ist; sonst würde ein Cron-Lauf und ein manueller Lauf aus dem Repo-Root heraus je eine andere physische Archiv-Datei beschreiben.

**How to apply:** `store_layer/db.py` definiert `init_db(db_path=DEFAULT_DB_PATH)` (Schema anlegen, idempotent via `CREATE TABLE IF NOT EXISTS`), `save_run_results(run_timestamp, gate_results, db_path=DEFAULT_DB_PATH)` und `export_survivors_csv(run_timestamp, gate_results, output_dir=DEFAULT_CSV_DIR) -> Path`. `gate_results` ist das volle `Dict[str, GateResult]` aller gescreenten Firmen (wie von `gates_layer.rank_companies()` erwartet) — beide Funktionen rufen intern `rank_companies()` auf, filtern also selbst nicht nochmal separat. `db_path`/`output_dir` sind überschreibbare Parameter (nicht hart auf `DEFAULT_DB_PATH`/`DEFAULT_CSV_DIR` verdrahtet), damit Tests isolierte Pfade (z. B. `tmp_path`) nutzen können, ohne echte Archiv-Dateien zu berühren.

## 2026-09-15: Lauf-Nachweis unabhängig von Survivor-Zahl (Store-Layer, Nachtrag)

**Kontext:** Die Gates-Layer/Store-Layer-Entscheidung 6 oben akzeptierte bewusst, dass ein 0-Survivor-Lauf in `run_results` keine Spur hinterlässt — nicht von einem nie stattgefundenen Lauf (z. B. ausgefallener Cron-Job) unterscheidbar. Das ist kein Rand- oder Fehlerfall: `docs/masterplan.md` §4 ("Kein Zwangskauf") sagt explizit, dass bei weniger als 8 qualifizierten Titeln **Cash gehalten** wird, statt den Standard zu senken — ein 0- oder Wenig-Survivor-Lauf ist also ein erwartetes, korrektes Ergebnis des Trichters, kein Ausfall. Ein Betriebs-Log, das diese beiden Fälle nicht unterscheiden kann, widerspricht der in Masterplan §8 verlangten "ehrlichen" Mess-Schicht: dort muss erkennbar sein, *dass* jeder Lauf stattfand, unabhängig vom Ergebnis.

**Entscheidung 1 – zweite, schlanke Tabelle `runs`, eine Zeile pro Aufruf:**
```sql
CREATE TABLE runs (
    run_timestamp        TEXT PRIMARY KEY,
    companies_evaluated  INTEGER NOT NULL,
    survivors_count      INTEGER NOT NULL
)
```
`save_run_results()` schreibt bei **jedem** Aufruf genau eine Zeile in `runs` — unabhängig davon, ob `survivors_count` 0 ist —, zusätzlich zu den (ggf. null) Survivor-Zeilen in `run_results`. `companies_evaluated` ist `len(gate_results)` (alle gescreenten Firmen, nicht nur Überlebende), `survivors_count` die Anzahl der Zeilen, die auch in `run_results` landen. Kein separates `runs`-Objekt/Funktion nötig — `save_run_results()` bleibt der einzige Schreibpfad für beide Tabellen.

**Entscheidung 2 – beide Inserts in einer Transaktion:**
Der `runs`-Insert und die `run_results`-Inserts laufen in derselben SQLite-Connection/Transaktion (ein `commit()` für beide) - `save_run_results()` nutzte auch vorher schon eine einzige Connection für `run_results`, das ist hier unverändert; neu ist lediglich, dass der zusätzliche `runs`-Insert in dieselbe Connection/Transaktion aufgenommen wurde statt eine eigene zu öffnen. Damit bleibt die `INSERT`-ohne-Upsert-Garantie aus Entscheidung 4 oben konsistent über beide Tabellen: schlägt der `run_results`-Insert fehl (z. B. doppelter `run_timestamp`), wird auch kein verwaister `runs`-Eintrag ohne zugehörige Survivor-Zeilen zurückgelassen, und umgekehrt.

**Entscheidung 3 – `export_survivors_csv()` schreibt immer eine Datei, auch bei 0 Überlebenden:**
Geprüft (kein Codeänderung nötig, war bereits korrekt): Die Funktion öffnet die CSV-Datei und schreibt den Header bedingungslos, bevor sie über die (ggf. leere) Survivor-Liste iteriert — bei 0 Überlebenden entsteht also bereits heute eine gültige, nur aus der Kopfzeile bestehende CSV-Datei, keine fehlende Datei. Dieselbe Begründung wie bei Entscheidung 1: das Fehlen einer erwarteten Datei muss "kein Lauf" bedeuten, nicht "Lauf ohne Ergebnisse".

**How to apply:** `init_db()` legt zusätzlich zu `run_results` die Tabelle `runs` an (beide `CREATE TABLE IF NOT EXISTS`, idempotent). `save_run_results()` öffnet eine einzige Connection, schreibt den `runs`-Eintrag und die `run_results`-Zeilen (auch wenn letztere eine leere Liste sind) und committet einmal gemeinsam. `export_survivors_csv()` bleibt unverändert, da bereits korrekt.

## 2026-09-16: EDGAR-Pilot (Issue #7, Teil 1) — Zugriffsweg, Compliance, Fixture-Scope

**Kontext:** Issue #7 baut den echten EDGAR-Abruf (siehe `docs/masterplan.md` §7-Roadmap: "Dummy-JSON... → dann echter EDGAR-Abruf mit ~20–30 US-Tickern"). Bevor die eigentliche Tag-Mapping-Logik entsteht, klärt dieser Pilot (Teil 1, nur Erkundung, keine Mapping-Logik) den Zugriffsweg und prüft an 3–4 realen Small/Mid-Cap-Tickern, unter welchen exakten `us-gaap`-Tags die benötigten Konzepte tatsächlich auftauchen — Grundlage für die in §7 der Masterplan-Roadmap ("Datenqualitäts-Timebox") beschriebene Abdeckungsprüfung.

**Entscheidung 1 – Zugriffsweg: SEC-`companyfacts`-Endpoint, CIK-Mapping nur über die offizielle SEC-Datei:**
Abruf über `https://data.sec.gov/api/xbrl/companyfacts/CIK{10-stellig, nullgepolstert}.json` (bereits in `docs/masterplan.md` §7, "Datenqualitäts-Timebox", Punkt 1 als erster Schritt festgelegt: löst gängige US-GAAP-Konzepte standardisiert auf, bevor firmeneigene Custom-Tag-Extensions ins Spiel kommen). Das Ticker→CIK-Mapping kommt ausschließlich aus der offiziellen SEC-Datei `https://www.sec.gov/files/company_tickers.json` — CIKs werden nicht geraten oder aus dem Gedächtnis genommen, da ein falsch erratener CIK zu Daten einer völlig falschen Firma führen würde, ohne dass das ohne Weiteres auffällt.

**Entscheidung 2 – Pflicht-User-Agent und Rate-Limiting (SEC-Fair-Use):**
Jeder Request trägt einen aussagekräftigen `User-Agent`-Header mit Name und Kontakt-E-Mail — SEC blockt Requests ohne einen solchen Header (bzw. mit generischen/Standard-User-Agents) laut eigener Fair-Use-Policy. Im Pilot: keine Parallelisierung, kleine Verzögerung zwischen aufeinanderfolgenden Requests. Das ist eine Compliance-Anforderung des Datenanbieters, keine interne Performance-Optimierung — sie gilt später auch für den vollen Abruf (Issue #7 Teil 2), nicht nur für den Pilot.

**Entscheidung 3 – `tests/fixtures/edgar_raw/` sind statische Testdaten, kein Runtime-Cache:**
Die im Pilot tatsächlich abgerufenen rohen `companyfacts`-JSON-Antworten werden unverändert unter `tests/fixtures/edgar_raw/` abgelegt und eingecheckt. Das ist bewusst **kein** Vorgriff auf den in `docs/masterplan.md` §7 (Phase 1.1, "Härtung") geplanten Runtime-Caching-Layer für Rohdaten (der lebt außerhalb des Repos, z. B. unter `data/`, analog zu `store_layer`s gitignortem `data/`-Ordner) — sondern reine Test-Fixtures nach demselben Muster wie `tests/fixtures/companies/*.json` (Dummy-Firmendaten für die Gates-/Metrics-Layer-Tests), nur mit echten statt synthetischen Rohdaten. Zweck: Tests gegen die spätere Mapping-Logik laufen offline und deterministisch, ohne bei jedem Testlauf live gegen SEC zu fragen.

**How to apply:** Ticker→CIK-Auflösung liest `company_tickers.json` und sucht per Ticker (case-insensitive), nicht per fest codiertem CIK. Der `companyfacts`-Abruf nutzt einen `User-Agent` der Form `"<Name> <Kontakt-E-Mail>"` und wartet zwischen Requests. Rohantworten landen 1:1 (kein Reformatieren) unter `tests/fixtures/edgar_raw/<TICKER>.json`. Die eigentliche Tag-Mapping-Entscheidung (welcher `us-gaap`-Tag für welches Konzept, Umgang mit Custom-Extensions) ist explizit **nicht** Teil dieses Eintrags — folgt erst nach Sichtung der Pilot-Ergebnisse.

## 2026-09-16: ROIC – undefinierter Steuersatz bei Vorsteuerergebnis≤0 (Nachtrag)

**Kontext:** Der Invested-Capital≤0-Guard (ROIC-Abschnitt oben, Entscheidung 3) deckt einen ungültigen Nenner ab, aber nicht den Zähler: `NOPAT = ebit × (1 − tax_rate)` setzt einen sinnvoll definierten `tax_rate` voraus. Ist das Vorsteuerergebnis einer Firma in einem Fiskaljahr ≤ 0, ist der effektive Steuersatz für dieses Jahr rechnerisch undefiniert (Division durch ≤0 auf der Ebene, die `tax_rate` erst erzeugt hat) — nicht 0 (das würde NOPAT fälschlich mit dem vollen EBIT gleichsetzen) und nicht geschätzt/normalisiert (siehe Entscheidung 1 im ROIC-Abschnitt: es gilt ausschließlich der tatsächliche effektive Satz je Firma/Jahr, kein pauschaler Ersatzwert).

**Entscheidung – `tax_rate is None` als Sentinel, eigener `invalid_reason`, ganze Firma geflaggt:**
Ein Aufrufer, der für ein Fiskaljahr keinen sinnvollen effektiven Steuersatz ermitteln kann (Vorsteuerergebnis ≤ 0), übergibt `tax_rate=None` statt eines geschätzten Werts. `roic_for_period()` prüft das **vor** der NOPAT-Berechnung (kein Berechnungsversuch mit undefiniertem Faktor) und gibt `MetricResult(value=None, invalid_reason="undefined_tax_rate")` zurück — ein eigener, von `ic_non_positive` unterscheidbarer Reason-String, da beide Guards unabhängige Ursachen haben (Zähler vs. Nenner) und im Rang-Output getrennt sichtbar bleiben sollen. Wie beim Invested-Capital-Guard flaggt ein einziges betroffenes Jahr den **gesamten** 3-Jahres-ROIC dieser Firma — `roic_3yr_average()` bricht beim ersten Jahr mit gesetztem `invalid_reason` ab und mittelt nicht über die verbleibenden gesunden Jahre. Treffen in einem Jahr **beide** Bedingungen gleichzeitig zu (undefinierter `tax_rate` **und** Invested Capital ≤ 0), gewinnt `"undefined_tax_rate"` — der Steuersatz-Guard prüft zuerst (er bewacht den NOPAT-Zähler, noch vor der Division durch Invested Capital), nicht willkürlich per Code-Reihenfolge, sondern weil ein undefinierter Zähler die Berechnung unabhängig vom Nenner sinnlos macht.

**How to apply:** `metrics_layer/roic.py`, `roic_for_period()` prüft `tax_rate is None` vor dem Invested-Capital-Guard (beide Prüfungen laufen vor jeder Division). Testfixture: `DHRM25` (siehe `tests/fixtures/README.md`) — 2022/2023 gesund, 2024 `tax_rate: null` in der JSON-Fixture (entspricht `None` nach `json.load`).

## 2026-09-16: EDGAR-Adapter (Issue #7, Teil 2) — Tag-Fallback-Regeln und Mapping ins interne Schema

**Kontext:** Der Pilot (Teil 1, siehe oben) hat an 4 realen Small/Mid-Cap-Tickern (MOV, NPK, LAKE, FOSL) geprüft, unter welchen `us-gaap`-Tags die benötigten Konzepte tatsächlich auftauchen. Ergebnis: EBIT, Steuerkomponenten, Equity, Cash und CFO lassen sich mit je einem festen Tag sauber ableiten; Revenue, D&A und (bei mindestens einer Firma) NetIncome brauchen eine Fallback-Kette statt eines fixen Tag-Namens; fehlende Schulden-Tags bedeuten bei mindestens 2 von 4 Firmen echte Schuldenfreiheit, nicht fehlende Daten. Bevor `data_layer/edgar_client.py` geschrieben wird, legt dieser Eintrag die konkreten Fallback-Regeln fest.

**Entscheidung 1 – Fallback-Auswertung pro Fiskaljahr, nicht pro Firma:**
Für ein Konzept mit mehreren Kandidaten-Tags wird die Prioritätsliste **für jedes Fiskaljahr einzeln** ausgewertet (erster Tag in der Liste, der für das konkrete Fiskaljahresende Daten hat, gewinnt) — nicht einmal pro Firma auf einen einzigen Tag festgelegt. Begründung: `NPK` wechselt den D&A-Tag mitten in der eigenen Zeitreihe (`DepreciationDepletionAndAmortization` bis FY2023, danach `Depreciation`) — eine Firma-weite Tag-Festlegung würde für die neueren Jahre fälschlich keinen Wert liefern, obwohl einer über den nächsten Kandidaten verfügbar wäre.

**Entscheidung 2 – Mehrfach-Datenpunkte pro Fiskaljahresende:** Ein `companyfacts`-Tag enthält denselben Fiskaljahres-Endwert oft mehrfach (identischer `end`, weil derselbe Wert als Vorjahresvergleich in späteren Filings erneut auftaucht, siehe Pilot-Fund bei FOSLs `ProfitLoss`). Bei mehreren Punkten mit demselben `end`-Datum gewinnt der mit dem spätesten `filed`-Datum (falls Werte doch abweichen, z. B. durch eine Restatement-Korrektur, gilt die zuletzt eingereichte Fassung als maßgeblich).

**Entscheidung 3 – Anzahl gelieferter Fiskaljahre: die 3 jüngsten, verankert an EBIT:**
`OperatingIncomeLoss` (EBIT) ist laut Pilot bei allen 4 Firmen durchgehend und aktuell vorhanden — es verankert, welche Fiskaljahresenden überhaupt existieren. Der Adapter liefert die **3 jüngsten** solcher Jahresenden (weniger, falls eine Firma kürzer als 3 Jahre berichtet — analog zu `ORBT18`s `insufficient_years`-Fall bei den Dummy-Fixtures), sortiert aufsteigend nach Fiskaljahr, passend zum 3-Jahres-Schnitt-Bedarf von ROIC und Cash Conversion.

**Entscheidung 4 – Tag-Zuordnung je Konzept:**
| Konzept | Tag-Priorität | Flag bei Fallback-Nutzung |
|---|---|---|
| EBIT | `OperatingIncomeLoss` (einzelner Tag) | — |
| Steuer | `IncomeTaxExpenseBenefit` / `IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest` (Vorsteuerergebnis) | — (kein Fallback; bei Vorsteuerergebnis ≤ 0 wird `tax_rate = None`, löst den `undefined_tax_rate`-Guard aus [[Branch 1]] aus) |
| EBITDA/D&A | `DepreciationDepletionAndAmortization` > `DepreciationAmortizationAndAccretionNet` > `DepreciationAndAmortization` > `Depreciation` | `da_incomplete_depreciation_only`, wenn der letzte Kandidat (`Depreciation`) gezogen wurde — dieser Tag kann nachweislich unvollständig sein (Pilot-Fund bei NPK: unterschiedliche Werte für denselben Zeitraum je nach Tag) |
| Equity | `StockholdersEquity` (einzelner Tag) | — |
| Cash | `CashAndCashEquivalentsAtCarryingValue` > `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` | `cash_includes_restricted_cash`, wenn der zweite Kandidat gezogen wurde — schließt zweckgebundene liquide Mittel ein, andere Basis als der Primärtag |
| CFO | `NetCashProvidedByUsedInOperatingActivities` (einzelner Tag) | — |
| NetIncome | `NetIncomeLoss` > `ProfitLoss` | `net_income_includes_noncontrolling_interest`, wenn `ProfitLoss` gezogen wurde — schließt Minderheitsanteile ein, ist nicht identisch mit `NetIncomeLoss` (Pilot-Fund bei FOSL: `NetIncomeLoss` endet FY2011, aktueller Bottom-Line-Wert steht unter `ProfitLoss`) |
| Schulden-Komponenten (`LongTermDebtNoncurrent`, `LongTermDebtCurrent`, `ShortTermBorrowings`) | je Komponente einzeln, kein Fallback | `debt_component_missing_defaulted_zero`, wenn mindestens eine der drei Komponenten fehlt (fehlender Tag = **0**, siehe Entscheidung 5) |
| Revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` > `Revenues` > `SalesRevenueNet` > `RevenueFromContractWithCustomerIncludingAssessedTax` | `revenue_including_assessed_tax`, wenn der letzte Kandidat gezogen wurde — andere Umsatzbasis (inkl. statt exkl. Steuern) |

**Entscheidung 5 – fehlender Schulden-Tag = 0, akzeptiertes Restrisiko, sichtbar gemacht für Stufe 3:**
Fehlt `LongTermDebtNoncurrent`, `LongTermDebtCurrent` oder `ShortTermBorrowings` für ein Fiskaljahr, wird die jeweilige Komponente als **0** gewertet, nicht als ungültig/fehlend — Pilot-Fund: MOV und NPK haben in aktuellen Jahren keinen dieser Tags, gegengeprüft über verwandte Finanzierungs-Tags (`ProceedsFromBankDebt`/`RepaymentsOfBankDebt` = 0 bei MOV) als echte Schuldenfreiheit, nicht als Tagging-Lücke. Anders als ursprünglich hier festgehalten, bleibt das kein signalloser Fall: Fehlt mindestens einer der drei Tags für ein Fiskaljahr, wird `debt_component_missing_defaulted_zero` gesetzt — dieselbe Flag-Konvention wie bei den vier anderen Fallback-Fällen aus Entscheidung 4 (D&A, Cash, NetIncome, Revenue), nicht länger eine Ausnahme davon. Bei vollem Universum (16–26 Ticker, Issue #8) wird die Pilot-Gegenprobe über verwandte Finanzierungs-Tags **nicht** für jede einzelne Firma wiederholt — das bleibt ein akzeptiertes Restrisiko (eine Firma mit echten, aber falsch/uneinheitlich getaggten Schulden würde fälschlich als schuldenfrei erscheinen), aber eines, das jetzt über das Flag sichtbar gemacht für Stufe 3 ist, statt stillschweigend vergraben zu bleiben. Net Debt = Summe der (ggf. 0) Schulden-Komponenten minus Cash; ein Ergebnis < 0 (Netto-Cash-Position wie bei MOV) ist ein gültiges, kein fehlerhaftes Ergebnis.

**Entscheidung 6 – Revenue wird aktuell von keiner Stufe-1-Metrik konsumiert:**
Revenue fließt trotz Fallback-Kette und Flag in keine Stufe-1-Metrik ein (weder ROIC noch Net Debt/EBITDA noch Cash Conversion referenzieren `revenue`) — es wird nur mitgeführt, weil das interne Schema (Issue #1) das Feld vorsieht. Feinschliff der Revenue-Basis (exkl. vs. inkl. Steuern) ist auf den Trend-Guard (Stufe 1.1) bzw. Stufe 2 (relative Bewertung, EV/EBITDA) verschoben, wo Revenue erstmals tatsächlich verwendet wird.

**Entscheidung 7 – Schema-Erweiterung: `data_quality_flags` pro Fiskaljahr:**
Das interne Schema aus Issue #1 (`fiscal_year`, `revenue`, `ebit`, `ebitda`, `tax_rate`, `equity`, `net_debt`, `net_income`, `cfo`) bekommt ein zusätzliches, optionales Feld `data_quality_flags` (Liste von kurzen, stabilen snake_case-Strings, analog zur `invalid_reason`-Konvention) pro Fiskaljahr. Es ist rein additiv: Die Dummy-Test-Fixtures (`tests/fixtures/companies/*.json`) brauchen es nicht und bekommen es nicht nachträglich angehängt; `metrics_layer`/`gates_layer` lesen weiterhin nur ihre benannten Felder per Dictionary-Zugriff und ignorieren zusätzliche Schlüssel. Es ist ausschließlich ein Signal für Menschen/spätere Stufen (z. B. Stufe 3 One-Pager), nicht (noch) Input für eine Gate-Entscheidung.

**Korrekturen aus der Implementierung (gegenüber dem ursprünglichen Plan dieses Eintrags):**
- **D&A braucht einen 4. Kandidaten-Tag:** `DepreciationAndAmortization` fehlte in der ursprünglichen 3-Tag-Liste, ist aber bei MOV der einzige aktuelle D&A-Tag (weder `DepreciationDepletionAndAmortization` noch `DepreciationAmortizationAndAccretionNet` haben bei MOV aktuelle Daten) und existiert bei FOSL **zusätzlich** zu `DepreciationAmortizationAndAccretionNet` mit einem abweichenden Wert für denselben Zeitraum (13.053.000 vs. 12.822.000 für FOSLs FY2025). Eingeordnet als 3. Priorität (nach `DepreciationAmortizationAndAccretionNet`, vor `Depreciation`) — eine bewusste, aber nicht empirisch zwingende Reihenfolge zwischen den beiden mittleren Kandidaten, da kein Pilot-Fall beide gleichzeitig mit unterschiedlicher Vollständigkeit zeigt.
- **Cash braucht doch einen Fallback:** Die Annahme "einzelner Tag" traf auf LAKE nicht zu — `CashAndCashEquivalentsAtCarryingValue` endet bei LAKE nach FY2024, danach wird ausschließlich der kombinierte Tag `CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents` genutzt (schließt zweckgebundene liquide Mittel ein). Ohne diesen Fallback wäre LAKE für FY2025/2026 nicht mappbar gewesen.
- **Fiskaljahr-Label kommt aus dem `fy`-Feld des Anker-Tags (Minimum über Duplikate), nicht aus dem Kalenderjahr des Periodenendes:** SEC-`companyfacts` zitiert denselben Periodenwert oft mehrfach mit *unterschiedlichem* `fy` (das `fy`-Feld markiert, aus welchem Jahresbericht ein Datenpunkt stammt, nicht das Fiskaljahr des Werts selbst — ein Wert taucht mit steigendem `fy` erneut als Vorjahresvergleich in jedem späteren 10-K auf). Das **kleinste** `fy` über alle Duplikate eines Periodenendes ist das ursprüngliche, korrekte Fiskaljahr. Naive Ableitung aus dem Kalenderjahr des Endedatums hätte FOSLs FY2025 (Ende 2026-01-03, 52/53-Wochen-Kalender) fälschlich als 2026 gelabelt.
- **Quartals-Stub-Perioden mit `fp="FY"`:** Einzelne Quartals-Kontexte innerhalb eines 10-K können ebenfalls `form="10-K"`/`fp="FY"` tragen (beobachtet bei MOV). Zusätzlicher Dauer-Filter (Periodenlänge 350–380 Tage bei Datenpunkten mit `start`-Feld; Stichtagswerte ohne `start` sind ausgenommen) verhindert, dass eine Quartalszahl fälschlich als Jahreswert übernommen wird.
- **Fehlende Schulden-Tags werden jetzt geflaggt statt signallos (Review-Fund):** `debt_component_missing_defaulted_zero` wird gesetzt, wenn mindestens einer der drei Schulden-Tags fehlt und auf 0 defaultet — siehe die korrigierte Entscheidung 5 oben, dieselbe Flag-Konvention wie D&A/Cash/NetIncome/Revenue in Entscheidung 4.
- **`currency`-Parameter war wirkungslos, jetzt explizit auf USD beschränkt (Review-Fund):** `build_company_record()`s `currency`-Parameter änderte nie, unter welchem `units`-Schlüssel gelesen wird (jeder Tag-Lookup im Modul liest hart `USD`) — ein `currency="EUR"`-Aufruf hätte unverändert USD-Werte zurückgegeben, nur mit falschem Label. Da laut `docs/masterplan.md` weder ein Nicht-USD-Universum noch eine Umrechnungs-Datenquelle im Scope ist, wird keine echte Mehrwährungs-Unterstützung gebaut — `build_company_record()` wirft stattdessen `EdgarMappingError`, wenn `currency` etwas anderes als `"USD"` ist: der bisher nur behauptete, nie eingelöste Support ist damit explizit falsch statt still falsch.
- **`fy=None` bei Anker-Tag-Duplikaten wirft jetzt `EdgarMappingError` statt rohem `TypeError` (Review-Fund):** `_anchor_period_ends()`s `min(p["fy"] for p in raw[end])` crashte bisher unbehandelt, wenn ein Duplikat-Punkt kein `fy` trägt — bei allen 4 Piloten war `fy` durchgehend gesetzt, ist aber vom SEC-Schema nicht garantiert. Eine `None`-Prüfung vor `min()` wirft jetzt denselben `EdgarMappingError` wie die übrigen "kann nicht gemappt werden"-Fälle in der Datei.

**Offen für Issue #8 (nicht in diesem Eintrag behoben, aus Code-Review vor dem Merge):**
- Drei hartcodierte Schwellen/Parameter (`SEC_REQUEST_DELAY_SECONDS`, das 350–380-Tage-Duration-Fenster in `_is_full_year_duration`) folgen noch nicht der `CLAUDE.md`-YAML-Konvention und haben — anders als bei Gates-/Store-Layer — noch keine dokumentierte Ausnahmebegründung.
- `_is_full_year_duration()` würde bei einem USD-Punkt ohne `end`-Feld mit rohem `KeyError` statt `EdgarMappingError` crashen (im SEC-Schema praktisch garantiert vorhanden, bei keinem Piloten beobachtet — geringe Priorität).
- `_resolve_tag_value()`/`_annual_points_by_end()` bauen den Such-Index eines Tags bei jedem Aufruf neu auf, statt ihn einmal pro Tag zu berechnen und über die Perioden-Schleife wiederzuverwenden (Performance, kein Korrektheitsproblem bei den aktuell 3 Jahren × ~20 Tickern).
- Die vier Resolve→Raise→Flag-Blöcke (D&A, Cash, NetIncome, Revenue) in `map_companyfacts_to_periods()` sind nahezu identisch und könnten in einen gemeinsamen Helper zusammengefasst werden (Wartbarkeit, kein Bug).

**How to apply:** `data_layer/edgar_client.py` implementiert `resolve_cik(ticker, ...)` (Netzwerk, `company_tickers.json`), `fetch_companyfacts(cik, ...)` (Netzwerk, `companyfacts`-Endpoint) und als reine, netzwerkfreie Funktion `map_companyfacts_to_periods(raw_companyfacts, max_years=3)`, die die obige Fallback-Logik anwendet und eine Liste von Perioden im internen Schema (inkl. `data_quality_flags`) zurückgibt — diese Funktion ist es, die gegen die committeten Rohdaten in `tests/fixtures/edgar_raw/` getestet wird, ganz ohne Netzwerkzugriff.

## 2026-09-20: Net-Debt-Zuverlässigkeits-Guard — `net_debt_unreliable` für ROIC und Net Debt/EBITDA

**Kontext:** Eine SimFin-v3-Sondierung (auf einem separaten, noch offenen Branch) hat gezeigt, dass SimFins Bilanz-Schuldenfelder (`Short Term Debt`/`Long Term Debt`) bei rund 43 % der getesteten Ticker `null` sind, obwohl die Financing-Cashflow-Zeile (`Cash From (Repayment of) Debt`) im selben Fiskaljahr einen Wert ungleich 0 ausweist — ein interner Widerspruch zwischen zwei Statements derselben Firma/desselben Jahres, kein einfacher "Tag fehlt = 0"-Fall wie bei EDGARs `debt_component_missing_defaulted_zero`. Eine Gegenprobe über alle Fälle (Betrag der Debt-Cashflow-Zeile relativ zu EBITDA desselben Jahres) zeigt kein einheitlich kleines Muster: Werte reichen von <1 % bis über 200 % von EBITDA — ein pauschales "im Zweifel 0 annehmen" wäre für die großen Fälle grob falsch. Dieser Eintrag legt fest, wie ein zukünftiger `simfin_client.py`-Adapter (noch nicht geschrieben) mit dieser Unsicherheit umgeht, und wie `metrics_layer` (bereits jetzt) auf einen entsprechend markierten `net_debt`-Wert reagiert.

**Entscheidung 1 – 10 %-Schwelle für die Datenlayer-Klassifizierung (zukünftig, in `simfin_client.py`):**
`|Cash From (Repayment of) Debt| / |EBITDA|` desselben Fiskaljahres ist die Referenzgröße:
- **`≥ 10 %`** des EBITDA **oder** `EBITDA ≤ 0` (der Nenner selbst ist dann nicht verlässlich, das Verhältnis also nicht aussagekräftig — automatisch als groß/unzuverlässig behandelt, nicht als kleiner Wert missverstanden) → `net_debt` für **dieses eine Fiskaljahr** wird nicht verwendet, sondern auf den Sentinel `None` gesetzt ("unzuverlässig"), analog zum bestehenden `tax_rate=None`-Muster bei `undefined_tax_rate`.
- **`< 10 %`** → `net_debt` bleibt der reguläre Zahlenwert, kein Sentinel; ein informativer `data_quality_flags`-Eintrag (Name folgt bei der `simfin_client.py`-Implementierung, analog zu `debt_component_missing_defaulted_zero`) macht den Widerspruch sichtbar, ändert aber nichts an der Verwendung des Werts.

Diese Schwelle betrifft ausschließlich die künftige `simfin_client.py`-Mapping-Logik (Rohdaten → internes Schema) und wird in diesem Branch **nicht** implementiert — hier geht es nur um Entscheidung 2 (wie `metrics_layer` reagiert, wenn `net_debt` bereits `None` ankommt, unabhängig davon, welcher Adapter das gesetzt hat).

**Entscheidung 2 – gemeinsamer `invalid_reason` "net_debt_unreliable" für ROIC und Net Debt/EBITDA, mit unterschiedlicher Zeitfenster-Wirkung:**
`net_debt` fließt laut Masterplan in **zwei** Stufe-1-Metriken ein: direkt in Net Debt/EBITDA, und über Invested Capital (`equity + net_debt`) in ROIC. Ein `net_debt=None` in einem Fiskaljahr wird in beiden Metriken als eigener `invalid_reason="net_debt_unreliable"` behandelt (nicht als `ic_non_positive` — das wäre eine andere, unabhängige Ursache: ein tatsächlich negatives Invested Capital vs. ein schlicht nicht vertrauenswürdiger `net_debt`-Wert) — aber mit unterschiedlicher Reichweite, weil beide Metriken laut ihren jeweils bereits getroffenen Entscheidungen (siehe oben) unterschiedliche Zeitfenster verwenden:
- **ROIC (3-Jahres-Schnitt):** `net_debt=None` in **irgendeinem** der 3 Jahre flaggt den **gesamten** 3-Jahres-ROIC als `net_debt_unreliable` — analog zum bestehenden `ic_non_positive`-Guard (`NAKA17`) und `undefined_tax_rate`-Guard (`DHRM25`): eine einzelne betroffene Periode disqualifiziert den gesamten Durchschnitt, es wird nicht über die verbleibenden 2 gesunden Jahre gemittelt.
- **Net Debt/EBITDA (Stichtag, nur das jüngste Fiskaljahr):** `net_debt=None` wirkt sich **nur** aus, wenn es das **jüngste** Fiskaljahr betrifft (das Stichtagsjahr). Ist ein älteres, für Net Debt/EBITDA gar nicht genutztes Jahr betroffen, hat das laut der bereits getroffenen Stichtag-Entscheidung (siehe oben, Net-Debt/EBITDA Entscheidung 2) keinerlei Auswirkung auf diese Metrik — dieselbe Isolierung, die auch `TYRL16` bereits demonstriert (ältere, schlechtere Jahre beeinflussen den Stichtagswert nicht).

**Entscheidung 3 – Guard-Reihenfolge:**
In `roic_for_period()` wird `net_debt is None` **nach** dem bestehenden `tax_rate is None`-Check und **vor** der Berechnung von `invested_capital = equity + net_debt` geprüft (sonst würde `equity + None` einen rohen `TypeError` werfen statt kontrolliert zu flaggen — derselbe Grund, aus dem der `tax_rate`-Check bereits vor der NOPAT-Berechnung sitzt). In `net_debt_ebitda_for_period()` wird `net_debt is None` **vor** dem bestehenden `ebitda <= 0`-Check geprüft (beide sind unabhängige Bedingungen auf unabhängigen Variablen, aber die Datenverfügbarkeit von `net_debt` selbst wird als grundlegender behandelt als ein bereits berechenbarer Schwellenwert-Guard — dieselbe Priorisierungslogik wie in ROIC).

**How to apply:** `metrics_layer/roic.py`, `roic_for_period()`: neuer Guard `if net_debt is None: return MetricResult(value=None, invalid_reason="net_debt_unreliable")`, platziert wie in Entscheidung 3. `metrics_layer/net_debt_ebitda.py`, `net_debt_ebitda_for_period()`: neuer Guard `if net_debt is None: return MetricResult(value=None, invalid_reason="net_debt_unreliable")`, vor dem `ebitda <= 0`-Check. Neue Test-Fixtures `MURK26` (net_debt=None im Stichtagsjahr) und `FOGG27` (net_debt=None in einem älteren, nicht-Stichtags-ROIC-Jahr) in `tests/fixtures/companies/`, dokumentiert in `tests/fixtures/README.md`. Die 10 %-Schwelle aus Entscheidung 1 ist erst bei der `simfin_client.py`-Implementierung umzusetzen, nicht Teil dieses Branches.

## 2026-09-20: Masterplan v1.1 übernommen

**Kontext:** `docs/masterplan_v1.1.md` wurde neben die bestehende `docs/masterplan.md` gelegt (nicht als Ersatz committet). Eine vorherige Diagnose-Session hat den vollständigen Diff festgehalten und mehrere Stellen identifiziert, die bereits gebauten/gemergten Code betreffen oder ihm direkt widersprechen. Dieser Eintrag übernimmt `masterplan_v1.1.md` als neue `docs/masterplan.md` (alte Version bleibt über Git-Historie erhalten) und trifft die notwendigen Abgrenzungsentscheidungen für die vier Umsetzungs-Branches. Nur die ROIC-Steuersatz-Vereinfachung wird in diesem ersten Branch (`adopt-masterplan-v1.1`) auch tatsächlich implementiert — die übrigen Punkte sind hier nur entschieden/dokumentiert, ihre Umsetzung folgt in eigenen, späteren Branches.

Bewusst **nicht** Teil dieses Eintrags: v1.1s neuer ROIC-`Gültigkeit`-Guard (Invested Capital ≥ 10 % der Bilanzsumme, sonst n/a) und der 60 %-Deckel pro Einzeljahr im 3-Jahres-Schnitt. Beide brauchen ein neues, in keinem bestehenden Adapter geführtes Datenfeld (Bilanzsumme/Total Assets) und gehören inhaltlich zum zweiten der vier Branches ("IC/Bilanzsummen-Guard") — sie bekommen dort ihren eigenen, ausführlichen Entscheidungs-Eintrag, nicht hier vorweggenommen.

**Entscheidung 1 – ROIC: fixer 25 %-Steuersatz ersetzt effektiven Satz, `undefined_tax_rate`-Guard entfällt:**
`NOPAT = EBIT × (1 − 25 %)` mit einem festen, normierten Satz statt des bisherigen effektiven Steuersatzes (`tax_expense / pretax_income` aus den Rohdaten). Begründung (aus dem Masterplan übernommen): Verlustvorträge bei Small Caps drücken den effektiven Satz oft nahe 0 %, was den NOPAT systematisch überzeichnen würde — ein pauschaler Satz ist hier ehrlicher als ein formal "exakter", aber verzerrter tatsächlicher Wert. Damit entfällt der bisherige `undefined_tax_rate`-Guard vollständig: Er existierte ausschließlich, weil `tax_rate` bei Vorsteuerergebnis ≤ 0 nicht definierbar war — ohne Division durch das Vorsteuerergebnis kann diese Undefiniertheit nicht mehr auftreten. `roic_for_period()` nimmt `tax_rate` nicht mehr als Parameter entgegen.

**Entscheidung 2 – Rang: Perzentil-Rang-Durchschnitt nur als Tiebreaker, nicht als Primärsignal:**
Der in `masterplan_v1.1.md` vorgeschlagene "Rang (v1.1)" (Durchschnitt der Perzentil-Ränge über ROIC/Net-Debt-EBITDA/Cash-Conversion) wird **nicht** als primäres Ranking-Signal übernommen — die Gates-Layer-Entscheidung 3 ("kein zusammengesetzter Score", Gruppierung nach Flag-Anzahl) bleibt die Haupt-Ordnung, unverändert. Begründung: Der Masterplan warnt selbst explizit vor "falscher Präzision" bei rauschigen Small-Cap-Inputs (siehe Abschnitt 4, Positionsgrößen-Regeln) — ein auf zwei Nachkommastellen genauer Perzentil-Score über drei mit Messrauschen behafteten Kennzahlen wäre exakt diese falsche Präzision, wenn er das primäre Signal wäre. **Als Tiebreaker *innerhalb* einer bereits gleichwertigen Flag-Anzahl-Gruppe** ist der Perzentil-Rang-Durchschnitt dagegen eine echte, akzeptierte Verbesserung gegenüber der aktuellen rein alphabetischen Sortierung innerhalb einer Gruppe (`rank_companies()`, Gates-Layer-Entscheidung 3) — er verfeinert nur die Reihenfolge unter bereits gleich klassifizierten Firmen, ersetzt aber nicht die Klassifizierung selbst.

**Entscheidung 3 – Cash Conversion: NI ≥ 2 % vom Umsatz ist ein neues Stufe-1-Kriterium, keine Guard-Erweiterung:**
`Net Income > 0` bleibt unverändert der bestehende Guard (Ergebnis "nicht bewertbar" bei Verletzung, `invalid_reason="ni_non_positive"`, wie bisher). `Net Income ≥ 2 % des Umsatzes` ist dagegen laut `masterplan_v1.1.md` ein **neues, eigenständiges primäres Stufe-1-Kriterium** wie ROIC oder Net Debt/EBITDA, keine Erweiterung des bestehenden Nenner-Schutz-Guards — eine Verletzung ergibt `Status = durchgefallen` (analog zu Net Debt/EBITDAs harter Schwelle), nicht `invalid_reason`/"nicht bewertbar". Die Umsetzung gehört deshalb in den Gates-Layer (`stage1_gates.py`, neue eigene Schwelle/Klassifizierung), nicht in `cash_conversion.py` selbst — dessen Rückgabe bleibt der rohe 3-Jahres-Schnitt, unklassifiziert, wie bei allen anderen Stufe-1-Metriken. Folgt in einem eigenen, späteren Branch.

**Entscheidung 4 – Store-Layer: Archiv-Scope-Erweiterung auf den gesamten Trichter, Kurs-Snapshot vertagt:**
Der in `masterplan_v1.1.md` Abschnitt 8 geforderte volle Trichter-Scope (Überlebende *und* Ausgeschiedene je Stufe, samt Kennzahlen/Flags/Kurs) widerspricht direkt der bisherigen Store-Layer-Entscheidung 2 ("archiviert wird nur, wer überlebt") — diese wird hiermit durch die v1.1-Vorgabe ersetzt, Umsetzung (Schema-Migration in `store_layer/db.py`) folgt in einem eigenen, späteren Branch. Zwei Teile werden dabei bewusst **nicht** mitgezogen:
- **"Kurs am Lauftag"** braucht einen Price-Client, der noch nicht existiert (Kursdaten sind laut Datenquellen-Tabelle für Stufe 2 vorgesehen, aber noch nicht gebaut) — verschoben auf ein eigenes Issue, sobald dieser Client existiert.
- **Stufe-3/4-Spalten** (Verdikt, Kauf/Nicht-Kauf, Positionsgröße) bleiben draußen, bis Stufe 3/4 tatsächlich gebaut werden — Anti-Vorwegnahme-Prinzip, `masterplan.md` Abschnitt 9 ("Keine Vorwegnahme. Kein Code für Stufe N+1, bevor Stufe N fehlerfrei läuft").

**Entscheidung 5 – `thresholds.yaml` wird jetzt nicht gebaut:**
Die neue Kopfzeile in `masterplan_v1.1.md` ("Konfigurationswerte stehen in `thresholds.yaml`; im Zweifel gilt die Config") beschreibt einen **künftigen** Zustand, keinen aktuellen — die Datei existiert nicht und wird in diesem Branch **nicht** angelegt. Laut Roadmap (Abschnitt 7) ist Config-getriebene Schwellen-Verwaltung explizit Teil von **Phase 1.1** ("Härtung"), nicht Phase 1.0, in der sich das Projekt laut Status noch befindet. Das ändert nichts an der bereits dokumentierten, bewussten Phase-1.0-Ausnahme von der `CLAUDE.md`-YAML-Konvention (Gates-Layer-Entscheidung 4) — die Kopfzeile ist Vorgriff/Absichtserklärung für Phase 1.1, keine sofortige Anforderung.

**How to apply:** `docs/masterplan.md` wird durch den Inhalt von `docs/masterplan_v1.1.md` ersetzt (alte Version über Git-Historie abrufbar), `masterplan_v1.1.md` danach gelöscht. Nur Entscheidung 1 (ROIC-Steuersatz) wird in diesem Branch (`adopt-masterplan-v1.1`) umgesetzt: `metrics_layer/roic.py` verliert den `tax_rate`-Parameter und den `undefined_tax_rate`-Guard-Codepfad vollständig, `NOPAT_TAX_RATE = 0.25` als Modul-Konstante (dieselbe Phase-1.0-Ausnahme wie die Gates-Layer-Schwellen). Die Fixture `DHRM25` diente ausschließlich der Demonstration dieses jetzt entfallenen Guards (2022/2023 durchgehend gesund, keine andere Guard-Bedingung verletzt) und wird komplett entfernt (`tests/fixtures/companies/DHRM25.json`, zugehörige Tests in `test_roic.py`/`test_stage1_gates.py`, README-Abschnitt). Entscheidungen 2–5 werden erst in den jeweils folgenden drei Branches umgesetzt (IC/Bilanzsummen-Guard, Cash-Conversion-Schwellen-Umsetzung, Store-Layer-Erweiterung).
