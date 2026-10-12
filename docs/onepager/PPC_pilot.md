# One-Pager PPC (Pilot, Stufe 3 Runde 1, Nr. 5 der Kandidatenliste)

**Status: Pilot, Entwurf des Agenten.** Geschrieben nach `docs/stufe3/one_pager_vorlage.md` und `docs/stufe3/checkliste.md`, **direkt vom Agenten und nicht über die Prompts Bear/Bull/Synthese** (diese sind weiter unerprobt). Nur öffentliche Unterlagen der SEC (Originaltext, nicht über ein Zusammenfassungswerkzeug gelesen). Keine Prognose, kein Kursziel. **Begriffe:** EBITDA = Gewinn vor Zinsen, Steuern und Abschreibungen; EV/EBITDA = Unternehmenswert geteilt durch EBITDA (niedriger = günstiger); FCF = freier Cashflow (operativer Cashflow minus Investitionen in Anlagen); Gates = die Prüfschwellen der Stufen; Trend-Guard = Prüfung auf fallenden Umsatz, fallende Marge oder fallenden ROIC; Peer-Gruppe = Vergleichsfirmen derselben Branche; Durchfall-Schwelle = Grenze, ab der eine Firma ausscheidet. **Zeitbezüge:** Geschäftsjahr = das abgeschlossene Jahr bis 28.12.2025; Dreijahresmittel = Durchschnitt der letzten drei Geschäftsjahre; letzte zwölf Monate = Geschäftsjahr 2025 plus erstes Halbjahr 2026 minus erstes Halbjahr 2025. Das Verdikt ist ein Vorschlag, die Entscheidung trifft Konstantin.

---

**Firma:** Pilgrim's Pride Corp (PPC) · **SimFin-ID:** 423305 · **Stand der Daten:** Lauf vom 10.10.2026, Kurs vom 23.09.2026 · **One-Pager vom:** 12.10.2026
**Lauf (Archiv):** Stufe 1 `2026-10-10T12:27:35` / Stufe 2 `2026-10-10T12:27:44` / Trend-Guard `2026-10-10T12:27:50` (laut Export von Konstantins lokaler Sitzung vom 12.10.2026, im Repo nicht belegt) · **Regelstand:** Commit `33ed39b`, clean (laut Bericht zu A.3)
**Rang in Stufe 2:** 6 von 41 (Nr. 5 der Kandidatenliste; Platz 2 von 17 in der Peer-Gruppe) · **Profil:** non_financial · **Peer-Gruppe:** 102002 (fein, 17 Firmen; passt: nicht geprüft)

## 1. Kernthese (zwei bis drei Sätze)
PPC besteht die Gates auf den Zahlen der Geschäftsjahre: operative Marge 2025 8,7 %, freier Cashflow 2025 rund 661 Mio. $, Verschuldung 1,17 Mal EBITDA (Archiv, Geschäftsjahr; auf den letzten zwölf Monaten 1,77). **Die Zahlen des ersten Halbjahres 2026 zeigen einen scharfen Einbruch:** operatives Ergebnis 228,5 Mio. $ nach 916,8 Mio. $ im Vorjahreszeitraum, bei fast gleichem Umsatz, und der freie Cashflow der letzten zwölf Monate liegt bei rund 304 Mio. $ (4,6 % des Marktwerts). Stufe 2 rechnet EV/EBITDA und Verschuldung mit Geschäftsjahren und die FCF-Rendite als Dreijahresmittel, der Trend-Guard ebenfalls mit Geschäftsjahren; ein Halbjahreseinbruch fließt dort nicht ein (Belege: `docs/decisions.md`, `config/stage2.yaml`, `src/metrics_layer/trend.py`).

## 2. Katalysator
- **Nächster Bericht:** das 10-Q für das dritte Quartal 2026. Termin nicht bestätigt; die Berichte der Vorjahre kamen am 31.10.2024 und am 30.10.2025 (Quelle: SEC-Liste der Einreichungen), also voraussichtlich Ende Oktober 2026.
- **Investitionsprogramm:** „We anticipate spending between $900 million and $950 million on the acquisition of property, plant and equipment in 2026.“ (10-K 2025, Abschnitt Liquidität). Im ersten Halbjahr 2026 flossen 465,2 Mio. $ ab (10-Q Q2 2026, Kapitalflussrechnung).
- **Rechtsfälle:** Die Kartellverfahren der Geflügelbranche laufen weiter; die Steuerverfahren in Großbritannien sind per Vergleich vom 22.05.2026 beendet (siehe Punkt 4 und 5).
- Sonst kein erkennbarer Katalysator in den Quellen.

## 3. Zahlen

Alle Beträge in Mio. $, wenn nicht anders angegeben. „Tabellenwert“ = Wert aus dem Archiv (Stufe 1/2). „Prüfwert“ = eigene Rechnung des Agenten aus den SEC-Unterlagen; **Die Tabelle und die Prüfwerte sind nicht dieselbe Größe:** Stufe 2 rechnet die FCF-Rendite als Mittel der drei letzten Geschäftsjahre (`config/stage2.yaml`, `fcf_yield.years = 3`; `docs/decisions.md`), mit der Investitionszeile „Change in Fixed Assets & Intangibles“; der Prüfwert hier ist Cashflow aus dem operativen Geschäft minus Auszahlungen für Sachanlagen für ein Jahr beziehungsweise die letzten zwölf Monate. Das Dreijahresmittel selbst ist hier nicht nachgerechnet.

| Kennzahl | Tabellenwert (Stufe 1/2) | Prüfwert | Quelle und Datum | Abweichung |
|---|---|---|---|---|
| Aktienzahl | 238.108.524 | 238.108.524 | 10-Q Q2 2026, Deckblatt, Stand 28.06.2026 | keine |
| Marktkapitalisierung | 6.578,9 (27,63 $ × 238,1 Mio., Kurs vom 23.09.2026) | 6.578,9 mit demselben Kurs; aktueller Kurs unbekannt | Archiv (Vorfilter) | – |
| Freier Cashflow | – | Geschäftsjahr 2025: 1.371,7 − 711,1 = 660,6; letzte zwölf Monate bis 28.06.2026: 1.221,4 − 917,0 = 304,4 | 10-K 2025; 10-Q Q2 2026; 10-Q Q2 2025 (Rechnung unten) | – |
| FCF-Rendite | 11,9 % | Geschäftsjahr 2025: 10,0 %; letzte zwölf Monate: 4,6 % (304,4 / 6.578,9) | wie oben; `run_guidance_calc.py` | −7,3 Prozentpunkte (letzte zwölf Monate gegen Tabelle) |
| EV/EBITDA | 4,3 | letzte zwölf Monate: 6,4 (EV 9.076,0 / EBITDA 1.408,8); Geschäftsjahr 2025: 4,4 (EV / 2.068,6) | Rechnung unten | +2,1 (zwölf Monate gegen Tabelle) |
| Net Debt/EBITDA | 1,17 | letzte zwölf Monate: 1,77 (2.497,1 / 1.408,8); Abstand zur Durchfall-Schwelle 2,5: 0,73 | 10-Q Q2 2026 (Schulden und Kasse am 28.06.2026) | +0,60 |
| ROIC (3 Jahre) | 16,4 % | nicht neu gerechnet | – | – |

**Rechnung (offen):**
- Letzte zwölf Monate = Geschäftsjahr 2025 + erstes Halbjahr 2026 − erstes Halbjahr 2025. Cashflow aus dem operativen Geschäft: 1.371,650 + 471,846 − 622,103 = 1.221,393. Sachanlagen-Auszahlungen: 711,066 + 465,189 − 259,283 = 916,972. EBITDA: 2.068,565 + 472,486 − 1.132,282 = 1.408,769 (EBITDA wie vom Unternehmen definiert, ohne Bereinigungen; bereinigt wäre es 2.268,370 + 668,127 − 1.220,147 = 1.716,350).
- Nettoschulden: Langfristige Schulden einschließlich kurzfristigem Teil 2.885,9 minus Kasse 388,8 = 2.497,1 (Brutto-Nennwert der Schulden laut Anmerkung Schulden, einschließlich 1,1 Mio. $ Finanzierungsleasing, vor 23,7 Mio. $ aktivierten Finanzierungskosten; ohne Operating-Leasing, ohne gesperrte Kasse von 9,5, ohne Minderheitsanteile; Effekt auf Net Debt/EBITDA unter 0,02). EV = 6.578,9 + 2.497,1 = 9.076,0.
- Der Marktwert nutzt den Kurs vom 23.09.2026; der heutige Kurs fehlt dem Agenten.
- **Zeitbezug:** Das EBITDA der letzten zwölf Monate liegt vor dem Einbruch im zweiten Quartal 2026 nur teilweise; das Quartal allein ist schwächer (Betriebsergebnis zweites Quartal 2026: 66,0 Mio. $ nach 512,3 Mio. $).

## 4. Bear (Risiken, Bilanzkosmetik, Margendruck)
1. **Gewinneinbruch im ersten Halbjahr 2026.** Bruttogewinn im zweiten Quartal 339,8 Mio. $ nach 715,3 Mio. $; Betriebsergebnis im ersten Halbjahr 228,5 Mio. $ nach 916,8 Mio. $ (10-Q Q2 2026, Gewinn- und Verlustrechnung); „We generated a consolidated operating margin of 2.5%.“ (10-Q Q2 2026, MD&A) gegenüber 8,7 % im Geschäftsjahr 2025 (10-K, Betriebsergebnis 1.613,5 bei Umsatz 18.497,6).
2. **Preise.** „U.S. commodity market prices for most chicken products during the three months ended June 28, 2026, remained below prior-year levels and the historical five-year average.“ und „estimated industry ready-to-cook production increased approximately 4.5% during the second quarter of 2026“ (10-Q Q2 2026, MD&A). Das Angebot steigt, der Preis liegt unter dem Fünfjahresschnitt.
3. **Freier Cashflow bei hohen Investitionen.** Im ersten Halbjahr 2026: Cashflow aus dem operativen Geschäft 471,8 Mio. $, Auszahlungen für Sachanlagen 465,2 Mio. $, Differenz 6,7 Mio. $ (nach 362,8 Mio. $ im Vorjahreszeitraum). Für das Gesamtjahr sind 900 bis 950 Mio. $ Investitionen angekündigt (10-K 2025). Bei dem Cashflow des Geschäftsjahrs 2025 (1.371,7) blieben dann bei 925 Mio. $ (Mitte der Spanne) rund 447 Mio. $, etwa ein Drittel.
4. **Rechtsfälle.** „To date, PPC has incurred expenses of $ 838.5 million, including $ 155.4 million in the six months ended June 28, 2026, to cover settlements with various Broiler Opt Outs.“ (10-Q Q2 2026, Anmerkung zu Rechtsfällen). Im EBITDA-Abgleich stehen Vergleiche von 158,9 Mio. $ im ersten Halbjahr 2026 als Bereinigung.
5. **Mehrheitsaktionär.** „JBS USA beneficially owns a majority of our common stock and has the ability to control the vote on most matters brought before the holders of our common stock.“ (10-K 2025, Risikofaktoren) und eine laufende Aktionärsklage gegen den Mehrheitsaktionär (10-K 2025, Rechtsfälle; ein Vergleich „immaterial in amount“ am 10.04.2026 vorläufig vereinbart, 10-Q Q2 2026).

## 5. Bull (unterschätzte Investitionsrenditen, Preissetzungsmacht, abweichende Sichtweisen)
1. **Starkes Geschäftsjahr 2025.** Umsatz 18.497,6 Mio. $ (+3,5 %), Betriebsergebnis 1.613,5 Mio. $ (+7,1 %), Nettogewinn 1.083,3 Mio. $, Cashflow aus dem operativen Geschäft 1.371,7 Mio. $ (10-K 2025).
2. **Bilanz und Kapitalrückgabe.** „In 2025, the Board of Directors authorized to pay approximately $2 billion of special dividends due to the significant cash generation.“ (10-K 2025); die Schulden sanken im ersten Halbjahr 2026 von 3.122,2 auf 2.885,9 Mio. $ (10-Q Q2 2026, Anmerkung Schulden), und es stehen laut Liquiditätstabelle 1.192,2 Mio. $ freie Kreditlinien zur Verfügung.
3. **Tilgung teurer Anleihen.** Rückkauf von 250 Mio. $ der 6,25-%-Anleihen 2033 am 14.04.2026 für 264,2 Mio. $ (10-Q Q2 2026); tilgt Anleihen mit 6,25 % Zins; der vorzeitige Rückkauf kostete 17,6 Mio. $ Verlust (Kapitalflussrechnung, „Loss on early extinguishment of debt“).
4. **Steuerstreit in Großbritannien beendet.** „The parties signed a settlement agreement on May 22, 2026, in which HMRC agreed that PPC’s appeal should be upheld without variation.“ (10-Q Q2 2026, Anmerkung zu Rechtsfällen).
5. **Abweichende Sichtweise (nicht aus einer Quelle):** keine. Ob der Einbruch zyklisch ist und die Investitionen später Ertrag bringen, lassen die Quellen nicht erkennen: **unzureichende Evidenz**.

## 6. Hauptrisiko und Kill-Kriterium
**Hauptrisiko (ein Satz):** Der Geflügelpreis bleibt unter dem Fünfjahresschnitt, während die Investitionen hoch bleiben, und der freie Cashflow bleibt dünn.
**Kill-Kriterium (mit Zahl), Vorschlag des Agenten:** Im nächsten 10-Q (Q3 2026, voraussichtlich Ende Oktober): Net Debt/EBITDA der letzten zwölf Monate **mindestens 2,5** (Durchfall-Schwelle aus `config/thresholds.yaml`) **oder** freier Cashflow der letzten zwölf Monate **negativ** (Wert 0, keine Schwelle aus dem Repo, Vorschlag des Agenten). Die Untergrenze von 5 % FCF-Rendite aus `config/stage2.yaml` (`fcf_yield.fail_below`) gilt für das Dreijahresmittel; auf die letzten zwölf Monate angewendet wird sie schon heute unterschritten (4,6 %) und taugt deshalb nicht als Auslöser. Prüfdatum: Veröffentlichung des 10-Q.

## 7. Einstiegsszenario (Vorschlag des Agenten, Entscheidung durch Konstantin; kein Kursziel, keine Prognose)

**Zeithorizont:** ☒ mittelfristig (etwa 1 bis 3 Jahre)  ☐ langfristig. Begründung: Das Ergebnis hängt an Geflügelpreisen und am Investitionszyklus; die Quellen nennen keine Zeiträume über 2026 hinaus. Die Jahresgrenzen sind ein Vorschlag des Agenten.

**Einstiegsgrenze** (Rechnung mit `run_guidance_calc.py`; Zahlen der letzten zwölf Monate bis 28.06.2026, **keine Guidance**):

| Grenze nach | Preis je Aktie | Grundlage (Zahl, Quelle, Datum) |
|---|---|---|
| FCF-Rendite (Zielrendite 6 % aus `config/stage2.yaml`) | **21,31 $** | FCF der letzten zwölf Monate 304,4 Mio. $, 238,1 Mio. Aktien (10-Q Q2 2026). Die 6 % sind eine Stufe-2-Regel auf Dreijahresbasis, hier auf einen Einjahreswert angewendet; die Regel verlangt strikt mehr als 6 %, der Preis muss also etwas unter 21,31 $ liegen |
| EV/EBITDA (Drittelgrenze der Peer-Gruppe 102002) | **unzureichende Evidenz** | Der Wert der Drittelgrenze der Peer-Gruppe steht nicht im Archiv-Export und liegt dem Agenten nicht vor |
| **Bindend (die strengere, also die niedrigere)** | **21,31 $ (vorläufig, die zweite Grenze fehlt, die Grenze kann zu hoch sein)** | |
| Kurs 23.09.2026 | 27,63 $ | liegt 29,7 % über der Grenze; der heutige Kurs fehlt |

**Einstiegsbedingung:**
☐ Bedingung erfüllt
☒ abwarten bis zu einem Ereignis laut Quelle: Veröffentlichung des 10-Q für das dritte Quartal 2026 (voraussichtlich Ende Oktober 2026, Termin nicht bestätigt); neu rechnen mit den dann aktuellen Zahlen
☐ abwarten, bis der Kurs unter die Grenze fällt
☐ nicht einsteigen

**Was dieser Abschnitt nicht sagt:** wohin der Kurs geht. Die Grenze beschreibt nur den Abstand des Kurses vom 23.09.2026 zu den eigenen Bewertungsregeln. Sie ist keine Kauforder und wird bei neuen Zahlen neu gerechnet. Der Ausstieg bleibt das Kill-Kriterium. Die Größe legt Stufe 4 fest.

## 8. Marken und Beobachtungen
- **Trend-Guard:** keine Marken (`trend_clear`; Umsatzverhältnis 1,03, Margenverhältnis 1,20, ROIC letztes Jahr 20,1 %). **Der Trend-Guard rechnet mit Geschäftsjahreszahlen und erkennt den Einbruch des ersten Halbjahres 2026 deshalb noch nicht.**
- **Datenqualität:** Infomarke `share_count_unchecked` (Aktienzahl in Stufe 2 nicht gegengeprüft; laut `docs/decisions.md` entsteht die Marke, wenn keine nutzbare Dividende für die Gegenprobe da ist, obwohl PPC 2025 Sonderdividenden zahlte); die Aktienzahl im Archiv stimmt mit dem Deckblatt des 10-Q überein (238.108.524). FCF-Rendite mit sonstigen Investitionen: 11,9 % (gleich).
- **Unternehmensereignisse (Punkt 2 der Checkliste):** Anleihenrückkauf 14.04.2026, Vergleich in Großbritannien 22.05.2026, tentativer Vergleich der Aktionärsklage 10.04.2026, laufende Kartellverfahren (10-Q Q2 2026).
- **Sonstiges:** Sonderdividenden von zusammen rund 2 Mrd. $ im Jahr 2025 (6,30 $ je Aktie am 17.04.2025 und 2,10 $ je Aktie am 03.09.2025; 10-K 2025). Das Unternehmen hat keine feste Dividendenpolitik.

## 9. Verdikt (Vorschlag des Agenten, Entscheidung durch Konstantin)
Ausgang: ☐ weiterverfolgen  ☒ zurückstellen  ☐ verwerfen  ☐ unzureichende Evidenz
Begründung in einem Satz: PPC besteht Stufe 2 auf Geschäftsjahreszahlen, aber das Betriebsergebnis ist im ersten Halbjahr 2026 um drei Viertel eingebrochen und die FCF-Rendite der letzten zwölf Monate (4,6 %) läge unter 5 %, würde man die Stufe-2-Grenze auf diesen Zeitbezug anwenden (ein Vergleichsmaßstab, kein Durchfall); bis zum 10-Q für das dritte Quartal bleibt der Name zurückgestellt.

**Kauf (nur Konstantin, Stufe 4):** ☐ nein  ☐ ja, Größe: _____ · Entscheidung am: _____
*Der Agent füllt dieses Feld nicht aus.*

## Quellenliste
- Pilgrim's Pride Corp, Form 10-K für das Geschäftsjahr bis 28.12.2025, eingereicht 12.02.2026, Aktennummer 0000802481-26-000011 (`ppc-20251228.htm`): Umsatz, Betriebsergebnis, Cashflow, Investitionsankündigung 2026, Risikofaktoren (Mehrheitsaktionär), Sonderdividenden.
- Form 10-Q für das Quartal bis 28.06.2026, eingereicht 30.07.2026, Aktennummer 0000802481-26-000051 (`ppc-20260628.htm`): Halbjahreszahlen 2026, Schulden, Liquidität, Rechtsfälle, MD&A.
- Form 10-Q für das Quartal bis 29.06.2025, eingereicht 31.07.2025, Aktennummer 0000802481-25-000110 (`ppc-20250629.htm`): Halbjahreszahlen 2025 (EBITDA, Cashflow-Vergleich).
- Archiv-Export von Konstantins lokaler Sitzung vom 12.10.2026 (`export_A.csv`, im Repo nicht belegt) für alle Tabellenwerte.

**Nicht geprüft:** Ob Peer-Gruppe 102002 passt; die Drittelgrenze der Peer-Gruppe; der heutige Kurs; das Dreijahresmittel der FCF-Rendite (nicht nachgerechnet; die Abweichung von −7,3 Prozentpunkten zur Tabelle erklärt sich teilweise durch den anderen Zeitbezug); die Prompts Bear/Bull/Synthese (nicht verwendet); Zitate wörtlich aus den heruntergeladenen Originaldateien (in `scratchpad`, nicht im Repo) übernommen, Seitenzahlen nicht angegeben.
