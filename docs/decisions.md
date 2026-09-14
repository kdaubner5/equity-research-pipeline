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

## 2026-09-14: Verbindliches Rückgabeformat für alle Stufe-1-Metriken — MetricResult statt nacktem None

**Kontext:** ROIC (Issue #2), Net Debt/EBITDA (Issue #3) und Cash Conversion (Issue #4) haben alle Guard-Fälle, die aktuell (bzw. im Fall von #3/#4: geplant) einen nackten `None` zurückgeben. Ein nackter `None` sagt nichts darüber aus, *warum* eine Metrik ungültig ist (Invested Capital ≤ 0? Zu wenig Fiskaljahre? net_income ≤ 0?) — das erschwert Debugging und den Rang-Output im Gates-Layer (Issue #5), der unterschiedliche Ausfallgründe unterschiedlich behandeln oder zumindest anzeigen können muss.

**Entscheidung:** Jede Stufe-1-Metrik-Funktion gibt ein `MetricResult` zurück (definiert in `metrics_layer/result.py`, ein `@dataclass(frozen=True)` mit den Feldern `value: float | None` und `invalid_reason: str | None`):
- Gültiges Ergebnis: `MetricResult(value=<float>)` (`invalid_reason` bleibt `None`)
- Ungültiges Ergebnis: `MetricResult(value=None, invalid_reason=<kurzer, stabiler String>)`

`invalid_reason` ist ein kurzer, stabiler, maschinenlesbarer String (snake_case), kein Freitext — z. B. `"ic_non_positive"`, `"insufficient_years"`, `"net_income_non_positive"`. Jede Guard-Bedingung bekommt ihren eigenen, unterscheidbaren Reason-String, damit z. B. "IC≤0" und "zu wenig Jahre" bei ROIC nicht beide als undifferenziertes `"invalid"` erscheinen.

**How to apply — verbindlich für alle Stufe-1-Metriken, auch die noch nicht implementierten:**
- `metrics_layer/roic.py` (Issue #2, umgesetzt): `"ic_non_positive"`, `"insufficient_years"`
- `metrics_layer/net_debt_ebitda.py` (Issue #3, ausstehend): folgt demselben Muster von Anfang an, z. B. `"ebitda_non_positive"` falls relevant
- `metrics_layer/cash_conversion.py` (Issue #4, ausstehend): folgt demselben Muster von Anfang an, z. B. `"net_income_non_positive"` statt des bisher nur informell beschriebenen Guards

Kein `None` als alleiniger Rückgabewert irgendeiner Stufe-1-Metrik-Funktion — immer `MetricResult`.
