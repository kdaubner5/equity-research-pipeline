# Test-Fixtures: Internes Firmen-Feldformat

Die Dateien unter `companies/` sind Dummy-Testdaten für 26 fiktive Firmen (branchendivers), je mit 3 Fiskaljahren (2022–2024), außer wo explizit anders vermerkt (`ORBT18`: nur 2 Jahre). Sie bilden **kein** Provider-Schema (EDGAR, secfinapi, SimFin) ab, sondern ein eigenes internes Format. Die Übersetzung echter Provider-Formate in dieses interne Format passiert später in den `data_layer`-Adaptern.

Design-Entscheidungen zu einzelnen Kennzahlen (z. B. Grauzonen-Bänder, Stichtag vs. 3-Jahres-Schnitt) stehen in [`docs/decisions.md`](../../docs/decisions.md) — dort zuerst nachsehen, bevor eine Formel implementiert wird.

## Format

```json
{
  "company_id": "ACME01",
  "name": "Acme Manufacturing Corp.",
  "sector": "Industrials",
  "currency": "USD",
  "periods": [
    { "fiscal_year": 2022, "revenue": 0, "ebit": 0, "ebitda": 0, "tax_rate": 0.0, "equity": 0, "net_debt": 0, "net_income": 0, "cfo": 0 }
  ]
}
```

Alle Beträge in Millionen USD.

| Feld | Bedeutung | Verwendet in |
|---|---|---|
| `revenue` | Umsatz | Kontext/Grauzonen |
| `ebit` | Operatives Ergebnis | ROIC (NOPAT), Cash-Conversion-Kontext |
| `ebitda` | EBIT + D&A | Net Debt/EBITDA |
| `tax_rate` | Effektiver Steuersatz | NOPAT = ebit × (1 − tax_rate) für ROIC |
| `equity` | Eigenkapital | Invested Capital = equity + net_debt für ROIC |
| `net_debt` | Nettoverschuldung | Net Debt/EBITDA, Invested Capital |
| `net_income` | Jahresüberschuss | Cash Conversion = cfo / net_income (Guard bei net_income ≤ 0) |
| `cfo` | Operativer Cashflow | Cash Conversion |

## Edge Cases (Cash Conversion Guard, Issue #4)

| company_id | Fall |
|---|---|
| `MDYN06` | `net_income == 0` (2024) |
| `HOOL07` | `net_income < 0` **und** `cfo < 0` (2024) |
| `WONK08` | `net_income < 0`, `cfo > 0` (2024) |

Alle übrigen Felder bleiben in diesen Fixtures realistisch, damit sie auch für ROIC (Issue #2) und Net Debt/EBITDA (Issue #3) nutzbar sind.

Alle drei haben `net_income > 0` in 2022 und 2023 und verletzen den Guard nur im Fiskaljahr 2024 — sie decken damit zugleich den Fall ab, dass `net_income ≤ 0` nur in **einem** von 3 Jahren auftritt (analog zu `NAKA17` beim Invested-Capital-Guard, siehe unten). Ein Test bestätigt, dass `cash_conversion_3yr_average` in diesem Fall den **gesamten** 3-Jahres-Wert flaggt statt das schlechte Jahr auszuschließen und über die 2 gesunden Jahre zu mitteln — dieselbe Prüfung wie `test_roic_invested_capital_guard_flags_whole_company` bei ROIC.

## Grauzonen-/Schwellen-Fixtures (Gates-Layer, Issue #5)

ROIC bleibt 3-Jahres-Schnitt, Grauzone 10–12%. Net Debt/EBITDA ist laut [`docs/decisions.md`](../../docs/decisions.md) ein **Stichtagswert** (nur Fiskaljahr 2024, das letzte) mit Grauzonen-Band **2,3–2,5x**: `< 2,3x` bestanden, `2,3x ≤ x < 2,5x` Flag, `≥ 2,5x` durchgefallen.

| company_id | ROIC (3-Jahres-Schnitt) | Net Debt/EBITDA (Stichtag 2024) | Fall |
|---|---|---|---|
| `APRT11` | ~11,0% (jedes Jahr 10,98–11,09%) | ~0,7x (gesund) | ROIC-Grauzone isoliert |
| `GEKO12` | ~17,3% (gesund) | exakt 2,50x | Net-Debt/EBITDA harte Schwelle isoliert (durchgefallen) |
| `BLKW13` | ~11,0% (jedes Jahr 10,85–11,00%) | 2,40x | beide Grauzonen gleichzeitig (beide: Flag) |
| `VULC14` | ~15,5% (gesund) | exakt 2,30x | Net-Debt/EBITDA untere Bandgrenze isoliert (Flag, nicht "bestanden") |
| `CYBR15` | ~14,4% (gesund) | exakt 2,60x | knapp oberhalb der harten Schwelle isoliert (klar durchgefallen) |
| `TYRL16` | ~12,8% (gesund, alle 3 Jahre) | 2022: 3,5x · 2023: 3,0x · 2024: **1,0x** | Stichtag-vs-Schnitt-Testfall (siehe unten) |
| `INGN23` | exakt 12,0% (jedes Jahr exakt 12,0%) | 0,50x (gesund) | ROIC obere Grauzonen-Grenze isoliert (Flag, nicht "sauber bestanden" — Schwelle ist `x > 12%`); Cash Conversion (3-Jahres-Schnitt ~1,29) ebenfalls gesund |
| `SIRI24` | exakt 10,0% (jedes Jahr exakt 10,0%) | 0,50x (gesund) | ROIC untere Grauzonen-Grenze isoliert (Flag, nicht "durchgefallen" — Schwelle ist `x < 10%`); Cash Conversion (3-Jahres-Schnitt ~1,28) ebenfalls gesund |

`VULC14`, `GEKO12` und `CYBR15` decken die Bandgrenze von beiden Seiten ab: 2,30x (Flag-Untergrenze), 2,50x (harte Schwelle selbst) und 2,60x (klar durchgefallen, bestätigt dass das Flag-Band nicht nach oben ausleiert). Jede der drei isoliert mit "sauberem", gesundem ROIC, damit ein Net-Debt/EBITDA-Ergebnis nicht von einem zufällig ebenfalls betroffenen ROIC-Wert überlagert wird.

### Stichtag- vs. Durchschnitts-Testfall: `TYRL16`

`TYRL16` hat in 2022/2023 ein Net Debt/EBITDA weit über der Schwelle (3,5x / 3,0x), im aktuellsten Jahr 2024 aber klar gesund (1,0x) — z. B. nach Entschuldung. Eine korrekte Stichtag-Implementierung (nur Jahr t = 2024) liefert **bestanden, kein Flag**. Eine fälschlich mittelnde Implementierung würde `(3,5 + 3,0 + 1,0) / 3 = 2,5` berechnen und **durchgefallen** ausgeben — ein stark abweichendes, leicht testbares Fehlverhalten. ROIC ist in `TYRL16` bewusst über alle 3 Jahre konstant gesund (~12,8 %), damit die ROIC-Dimension das Testergebnis nicht verwässert.

Zum Vergleich: `PIED09` (Ø ROIC 9,76%) und `INIT10` (Ø ROIC 11,83%) liegen zufällig ebenfalls nahe der ROIC-Grauzone, wurden aber nicht gezielt dafür konstruiert und sind auf keiner anderen Dimension kontrolliert — für belastbare Gates-Layer-Tests die oben gelisteten Fixtures verwenden.

## Invested-Capital≤0-Guard (ROIC, Issue #2)

| company_id | Fall |
|---|---|
| `NAKA17` | 2022/2023 gesund (IC 2800/2880, ROIC ~13,6%); 2024 Invested Capital = **-500** (equity=-2500 nach fremdfinanziertem Rückkauf, net_debt=2000) |

Testet den Guard aus [`docs/decisions.md`](../../docs/decisions.md): Invested Capital ≤ 0 in **einem** Jahr flaggt den **gesamten** 3-Jahres-ROIC als ungültig — eine Implementierung, die das schlechte Jahr stattdessen nur aus dem Mittel ausschließt und über die 2 gesunden Jahre mittelt, würde bei `NAKA17` fälschlich einen gültigen Wert (~13,6%) statt eines Flags liefern.

## Weniger als 3 Fiskaljahre (ROIC, Issue #2)

| company_id | Fall |
|---|---|
| `ORBT18` | Nur 2 Fiskaljahre (2023, 2024) — kürzlicher Börsengang, kein Fiskaljahr 2022 vorhanden |

Testet den Guard aus [`docs/decisions.md`](../../docs/decisions.md): Weniger als 3 Perioden flaggt den 3-Jahres-ROIC als ungültig, statt aus 2 Jahren zu mitteln oder auf 3 Jahre hochzurechnen.

## EBITDA≤0-Guard (Net Debt/EBITDA, Issue #3)

| company_id | Fall |
|---|---|
| `WEYL19` | 2022/2023 gesund (EBITDA 520/180); 2024 (Stichtagsjahr) **EBITDA = -180** nach Wertminderung/Restrukturierung. Invested Capital bleibt 2024 positiv (2600), isoliert also nur den EBITDA-Guard, ohne den ROIC-IC-Guard mit auszulösen |

Testet den Guard aus [`docs/decisions.md`](../../docs/decisions.md): EBITDA ≤ 0 in der Stichtags-Periode (letztes Fiskaljahr) liefert `MetricResult(value=None, invalid_reason="ebitda_non_positive")` statt einer sinnlosen Division.

## Net-Debt-Zuverlässigkeits-Guard (ROIC + Net Debt/EBITDA, `fix-net-debt-reliability-guard`)

| company_id | Fall |
|---|---|
| `MURK26` | 2022/2023 gesund (`net_debt` 800/780); 2024 (zugleich Stichtagsjahr **und** letztes der 3 ROIC-Jahre) `net_debt: null` |
| `FOGG27` | 2022 (ältestes der 3 ROIC-Jahre, **nicht** Stichtagsjahr) `net_debt: null`; 2023/2024 gesund (`net_debt` 780/800) |

Testet den Guard aus [`docs/decisions.md`](../../docs/decisions.md): `net_debt = None` liefert `invalid_reason="net_debt_unreliable"` in beiden Metriken, aber mit unterschiedlicher Zeitfenster-Wirkung. `MURK26` isoliert den Fall, in dem das betroffene Jahr sowohl für ROIC (eines der 3 Jahre) als auch für Net Debt/EBITDA (Stichtag) relevant ist — beide Metriken werden geflaggt. `FOGG27` ist der eigentliche Unterscheidungs-Testfall: Das betroffene Jahr (2022) ist zwar eines der 3 ROIC-Jahre (ROIC wird geflaggt, analog zu `NAKA17` — ein einzelnes schlechtes Jahr disqualifiziert den gesamten 3-Jahres-Schnitt), aber **nicht** das Stichtagsjahr von Net Debt/EBITDA (2024 bleibt unberührt und liefert einen gültigen Wert) — dieselbe Stichtag-Isolierung, die `TYRL16` bereits für einen schlechten (nicht unzuverlässigen) älteren Wert demonstriert.

## Grauzone (Cash Conversion, Issue #4)

Cash Conversion bleibt wie ROIC ein 3-Jahres-Schnitt (Mittelwert der Jahres-Verhältnisse `cfo/net_income`), Grauzonen-Band laut [`docs/decisions.md`](../../docs/decisions.md) **0,8–1,0**: `≥ 1,0` sauber bestanden, `0,8 ≤ x < 1,0` bestanden mit Flag (Grauzone), `< 0,8` durchgefallen — dieselbe Richtung wie beim ROIC-Grauzonenband (Flag-Band unterhalb der sauberen Schwelle).

| company_id | Cash Conversion (3-Jahres-Schnitt) | ROIC / Net Debt/EBITDA | Fall |
|---|---|---|---|
| `SPCL20` | 0,90 (jedes Jahr exakt 0,90) | ROIC ~15,2% (gesund), Net Debt/EBITDA (2024) ~1,11x (gesund) | Cash-Conversion-Grauzone isoliert, andere Dimensionen bewusst gesund |
| `OSCP21` | 0,80 (jedes Jahr exakt 0,80) | ROIC ~15,2% (gesund), Net Debt/EBITDA (2024) ~1,11x (gesund) | untere Grauzonen-Grenze isoliert (Grauzone/Flag, **nicht** durchgefallen — Band ist `0,8 ≤ x`) |
| `DUFF22` | 1,00 (jedes Jahr exakt 1,00) | ROIC ~15,2% (gesund), Net Debt/EBITDA (2024) ~1,11x (gesund) | obere Grauzonen-Grenze isoliert (sauber bestanden, **nicht** mehr Grauzone — Schwelle ist `x ≥ 1,0`) |

`OSCP21` und `DUFF22` teilen sich bewusst dasselbe `ebit`/`equity`/`net_debt`/`ebitda`-Profil mit `SPCL20` (nur `net_income`/`cfo` unterscheiden sich) — analog zu `VULC14`/`GEKO12`/`CYBR15` bei Net Debt/EBITDA, die die Bandgrenze von beiden Seiten mit isoliert gesunden übrigen Dimensionen abdecken.

**Richtung ist festgelegt, Klassifizierungslogik nicht:** Die Richtung der Grauzone ist durch den Masterplan/die Konvention festgelegt (siehe `docs/decisions.md`, Entscheidung 5 im Cash-Conversion-Abschnitt) — `≥ 1,0` sauber bestanden, `0,8–1,0` Flag, `< 0,8` durchgefallen, analog zum ROIC-Muster. Offen ist ausschließlich die tatsächliche **Bucket-Klassifizierungslogik selbst** (die if/else-Implementierung, die einen rohen Wert einem der drei Fälle zuordnet) — das bleibt Aufgabe des Gates-Layers (Issue #5); `metrics_layer/cash_conversion.py` gibt weiterhin nur den rohen Mittelwert zurück, ohne selbst zu klassifizieren.
