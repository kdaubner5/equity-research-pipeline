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
