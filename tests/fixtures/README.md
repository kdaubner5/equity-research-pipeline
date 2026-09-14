# Test-Fixtures: Internes Firmen-Feldformat

Die Dateien unter `companies/` sind Dummy-Testdaten für 10 fiktive Firmen (branchendivers), je mit 3 Fiskaljahren (2022–2024). Sie bilden **kein** Provider-Schema (EDGAR, secfinapi, SimFin) ab, sondern ein eigenes internes Format. Die Übersetzung echter Provider-Formate in dieses interne Format passiert später in den `data_layer`-Adaptern.

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

## Grauzonen-/Schwellen-Fixtures (Gates-Layer, Issue #5)

Gezielt auf die ROIC-Grauzone (10–12%) bzw. die Net-Debt/EBITDA-Schwelle (~2,5x) platziert, isoliert und in Kombination:

| company_id | ROIC (3-Jahres-Schnitt) | Net Debt/EBITDA | Fall |
|---|---|---|---|
| `APRT11` | ~11,1% (jedes Jahr 10,98–11,24%) | ~0,7x (gesund) | ROIC-Grauzone isoliert |
| `GEKO12` | ~18,0% (gesund) | exakt 2,50x (jedes Jahr) | Net-Debt/EBITDA-Schwelle isoliert |
| `BLKW13` | ~11,0% (jedes Jahr ~11%) | exakt 2,50x (jedes Jahr) | beide Grauzonen gleichzeitig |

Zum Vergleich: `PIED09` (Ø ROIC 10,06%) und `INIT10` (Ø ROIC 11,73%) liegen zufällig ebenfalls nahe der ROIC-Grauzone, wurden aber nicht gezielt dafür konstruiert und sind auf keiner anderen Dimension kontrolliert — für belastbare Gates-Layer-Tests `APRT11`/`GEKO12`/`BLKW13` verwenden.
