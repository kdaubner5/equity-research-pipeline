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
- `metrics_layer/roic.py` (Issue #2, umgesetzt): `"ic_non_positive"`, `"insufficient_years"`
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
Der Masterplan legt die Richtung explizit fest: `CFO / Net Income ≥ 1,0` → sauber bestanden (kein Flag); `0,8 ≤ x < 1,0` → bestanden mit Flag (Grauzone); `< 0,8` → durchgefallen. Das Grauzonen-Band liegt damit **unterhalb** der sauberen Bestehens-Schwelle (1,0), nicht symmetrisch unentschieden um sie herum — dasselbe Muster wie beim bereits implementierten ROIC-Grauzonenband (10–12 %, unterhalb der sauberen 12 %-Schwelle) und strukturell analog zu Net Debt/EBITDA (dort liegt das Flag-Band ebenfalls auf der schlechteren Seite der harten Schwelle, siehe dessen Entscheidung 1 oben — bei Net Debt/EBITDA ist "schlechter" nur numerisch höher statt niedriger, weil dort niedriger besser ist).

Offen bleibt ausschließlich die **Bucket-Klassifizierungslogik selbst** (die konkrete if/else-Implementierung, die einen rohen Wert einem der drei Fälle zuordnet) — das ist Aufgabe des Gates-Layers (Issue #5). `metrics_layer/cash_conversion.py` gibt weiterhin nur den rohen 3-Jahres-Mittelwert zurück und klassifiziert nicht selbst.

**How to apply:** `metrics_layer/cash_conversion.py` implementiert weiterhin keine Schwellenwert-Logik — das bleibt Issue #5. Testfixtures: `SPCL20` (3-Jahres-Schnitt = 0,90, Grauzone), `OSCP21` (0,80, untere Grauzonen-Grenze — Übergang Grauzone → durchgefallen) und `DUFF22` (1,00, obere Grauzonen-Grenze — Übergang sauber bestanden → Grauzone); siehe `tests/fixtures/README.md`.
