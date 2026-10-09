# One-Pager (Stufe 3), Vorlage (Roadmap D.1)

Ein One-Pager je Finalist, Ziel etwa eine Seite (Wunsch, kein Zwang; bei mehr Stoff gewinnt die Vollständigkeit der Quellen). Jede Zahl mit Quelle. Wo die Evidenz fehlt, steht **„unzureichende Evidenz“**, nie eine Schätzung. Checkliste: `docs/stufe3/checkliste.md`.

---

**Firma:** <Name> (<Kürzel>) · **SimFin-ID:** <ID> · **Stand der Daten:** <Datum des Laufs> · **One-Pager vom:** <Datum>
**Lauf (Archiv):** <Zeitstempel Stufe 1 / Stufe 2 / Trend-Guard> · **Regelstand:** <Commit, clean/dirty> (diese Kopfzeilen kommen aus dem Archiv, Roadmap A.3; Ergänzung gegenüber dem Masterplan)
**Rang in Stufe 2:** <Rang von N> · **Profil:** <Profil> · **Peer-Gruppe:** <Gruppe> (passt: ja/nein, Punkt 5)

## 1. Kernthese (zwei bis drei Sätze)
<Warum besteht die Firma die Gates, und was ist an ihr auffällig oder unterschätzt? Nur aus Primärquellen, ohne Vorhersage.>

## 2. Katalysator
<Welche Ereignisse nennen die Quellen mit Datum (zum Beispiel Zahlen, Verkauf, Restrukturierung)? Oder „kein erkennbarer Katalysator“. Keine Kursprognose.>

## 3. Zahlen (mit dem Python-Werkzeug der Roadmap D.2, solange es nicht gebaut ist von Hand mit offener Rechnung)

| Kennzahl | Tabellenwert (Stufe 1/2) | Prüfwert | Quelle und Datum (Guidance: Datum des Transkripts) | Abweichung |
|---|---|---|---|---|
| Aktienzahl | | | 10-K Deckblatt / Yahoo | |
| Marktkapitalisierung | | | | |
| Freier Cashflow | | | | |
| FCF-Rendite | | Guidance-Basis: | Transkript, Datum | |
| EV/EBITDA | | Guidance-Basis: | Transkript, Datum | |
| Net Debt/EBITDA | | jüngstes Quartal: | 10-Q, Datum | Abstand zur Schwelle: |
| ROIC (3 Jahre) | | | | |

## 4. Bear (Risiken, Bilanzkosmetik, Margendruck)
<Drei bis fünf Punkte mit Zitat und Fundstelle, oder „unzureichende Evidenz“.>

## 5. Bull (unterschätzte Investitionsrenditen, Preissetzungsmacht, abweichende Sichtweisen)
<Drei bis fünf Punkte mit Zitat und Fundstelle, oder „unzureichende Evidenz“.>

## 6. Hauptrisiko und Kill-Kriterium
**Hauptrisiko (ein Satz):** <...>
**Kill-Kriterium (mit Zahl):** <Kennzahl, Schwelle, Prüfdatum>

## 7. Einstiegsszenario (Vorschlag des Agenten, Entscheidung durch Konstantin; kein Kursziel, keine Prognose)

**Zeithorizont:** ☐ mittelfristig (etwa 1 bis 3 Jahre)  ☐ langfristig (etwa 3 bis 5 Jahre oder länger). Begründung in einem Satz aus den Quellen (zum Beispiel: ein Ereignis mit Datum innerhalb von drei Jahren spricht für mittelfristig; eine langsam wirkende Entwicklung für langfristig). Die Jahresgrenzen sind ein Vorschlag des Agenten, Konstantin legt sie fest.

**Einstiegsgrenze (Rechnung mit `run_guidance_calc.py`, Roadmap D.2):** der Preis je Aktie, bis zu dem die Firma die eigenen Bewertungsgrenzen mit den *heutigen* Zahlen noch erfüllt.

| Grenze nach | Preis je Aktie | Grundlage (Zahl, Quelle, Datum) |
|---|---|---|
| FCF-Rendite (Zielrendite aus `config/stage2.yaml`) | | |
| EV/EBITDA (Zielvielfaches = Peer-Grenze des Laufs) | | |
| **Bindend (die niedrigere)** | | |
| Aktueller Kurs (Datum) | | Abstand zur Grenze in %: |

**Einstiegsbedingung (eine Zeile ankreuzen, mit Begründung):**
☐ jetzt: Kurs unter der Grenze und kein offenes Ereignis aus Punkt 2 der Checkliste
☐ abwarten bis zu einem Ereignis laut Quelle, mit Datum (zum Beispiel nächster 10-Q): <Ereignis, Datum>
☐ abwarten, bis der Kurs unter die Grenze fällt
☐ nicht einsteigen

**Was dieser Abschnitt nicht sagt:** wohin der Kurs geht. Die Grenze beschreibt nur den Abstand des heutigen Kurses zu den eigenen Bewertungsregeln; mit anderen Zahlen (neues Quartal, neue Guidance) ergibt sich eine andere Grenze. Der Ausstieg bleibt das Kill-Kriterium (Abschnitt 6), nicht ein Kurs. Die Größe legt Stufe 4 fest (Konstantin); sie wird beim Kauf einmal festgelegt (`docs/masterplan.md`, Stufe 4), ein Aufstocken in Stufen gehört deshalb nicht in das Szenario.

## 8. Marken und Beobachtungen
- Trend-Guard-Marken: <T1 Umsatz fällt, T2 Marge fällt, T3 ROIC unter Grenze, T4 Margengipfel, oder keine>
- Datenqualitäts-Flags und veraltete Daten: <...>
- Unternehmensereignisse (Punkt 2): <...>
- Sonstiges (zum Beispiel Dividendenmuster, Grenzfall „borderline“): <...>

## 9. Verdikt (Vorschlag des Agenten, Entscheidung durch Konstantin)
Ausgang: ☐ weiterverfolgen  ☐ zurückstellen  ☐ verwerfen  ☐ unzureichende Evidenz
Begründung in einem Satz: <...>

**Kauf (nur Konstantin, Stufe 4):** ☐ nein  ☐ ja, Größe: <...> · Entscheidung am: <Datum>
*Der Agent füllt dieses Feld nicht aus.* Stufe 4 verlangt für einen Kauf einen vollständigen One-Pager mit Kernthese, Katalysator und Kill-Kriterium; Größe und Kappungen gelten nur nach den Stufe-4-Regeln (`docs/masterplan.md`, Abschnitt 6).

**Quellenliste:** <Dokument, Abschnitt/Seite, Datum, Link oder Aktenzeichen>
