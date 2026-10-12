# Übersicht Stufe 3, Runde 1, Tranche A (Nr. 1 bis 15)

Stand: 12.10.2026. Alle Verdikte sind Vorschläge des Agenten (Entwurf); die Entscheidung trifft Konstantin. Kurse sind die Archivkurse vom 23.09.2026, ein aktueller Kurs liegt nicht vor. „FCF 12 Mon.“ ist der freie Cashflow der letzten zwölf Monate geteilt durch den Börsenwert (Rechnung des Agenten aus SEC-Zahlen); „FCF Stufe 2“ ist der Archivwert (3-Jahres-Mittel). Quelle aller Zahlen: je Name die Datei `docs/onepager/<TICKER>.yaml` und die SEC-Unterlagen (10-K, 10-Q, 8-K).

| Nr. | Ticker | Vorschlag | FCF 12 Mon. | FCF Stufe 2 | Wichtigster Grund |
|---|---|---|---|---|---|
| 1 | FHI | WEITERVERFOLGEN | 8,4 % | 8,9 % | Rekordvermögen, kaum Schulden; halber Umsatz hängt an Geldmarktfonds |
| 2 | JBI | ZURÜCKSTELLEN | 14,2 % | 26,0 % | Betriebsergebnis -45 % im 1. Halbjahr; bereinigte Verschuldung 2,7 |
| 3 | TASK | WEITERVERFOLGEN | 15,7 % | 12,4 % | billig; seit Sonderdividende (333 Mio. $) mit neuem Kredit, Wachstum bremst |
| 4 | SLVM | ZURÜCKSTELLEN | -0,8 % | 13,4 % | Übergangsjahr: Betriebsergebnis null, freier Cashflow negativ |
| 5 | PPC | ZURÜCKSTELLEN | 4,6 % | 11,9 % | Gewinneinbruch 1. Halbjahr; JBS-Tauschvorschlag offen |
| 6 | UFPI | ZURÜCKSTELLEN | 5,8 % | 10,9 % | Cashflow sinkt seit 3 Jahren; 12-Monats-Rendite unter 6 % |
| 7 | VC | WEITERVERFOLGEN | 6,4 % | 9,5 % | Nettokasse, günstig; Ergebnis 2026 schwächer, Ford 26 % |
| 8 | DFIN | WEITERVERFOLGEN | 12,7 % | 8,0 % | Software wächst, Marge steigt, geringe Schulden |
| 9 | BYD | ZURÜCKSTELLEN | 0,7 % | 8,5 % | Wachstums-Investitionen, Cashflow-Einbruch, Verschuldung steigt auf rund 2,2 |
| 10 | MDXG | VERWERFEN | 6,3 % | 7,6 % | Umsatz -35 % (Medicare-Regeln), Übernahme mit 300 Mio. $ neuem Kredit |
| 11 | CBT | WEITERVERFOLGEN | 6,9 % | 9,8 % | solide Bilanz; bereinigter Gewinn sinkt, Chef- und Finanzchefwechsel |
| 12 | PATK | ZURÜCKSTELLEN | 5,6 % | 12,0 % | Fusion mit LCI Industries geplant; Verschuldung 3,0 laut Kreditvertrag |
| 13 | PLAB | WEITERVERFOLGEN | 6,2 % | 6,7 % | stabil, viel Kasse; hohe Investitionen, dünne Rendite |
| 14 | MGM | ZURÜCKSTELLEN | 15,4 % | 16,9 % | Mietverpflichtungen 23,8 Mrd. $ im Filter unsichtbar; Gewinn 2026 mit Verkaufsgewinn |
| 15 | WFRD | WEITERVERFOLGEN | 8,7 % | 9,0 % | geringe Schulden; Ergebnis durch Nahost-Konflikt gedrückt |

Verteilung: 7 weiterverfolgen, 7 zurückstellen, 1 verwerfen.

## Befunde über die Methode (für die Entscheidung des Projektinhabers)

1. **Der Filter sieht nur Geschäftsjahre.** Bei 9 von 15 Namen (JBI, TASK, SLVM, PPC, UFPI, BYD, MDXG, PATK, MGM) widersprechen die Zahlen des laufenden Jahres dem Bild aus Stufe 1 und 2: Betriebsergebnis, freier Cashflow oder Verschuldung haben sich seit dem letzten Geschäftsjahr deutlich verschlechtert. Bei 6 von 15 hat die 12-Monats-FCF-Rendite die Stufe-2-Grenze von 6 % unterschritten oder liegt knapp darüber (SLVM, PPC, UFPI, BYD, PATK, PLAB).
2. **Einschneidende Ereignisse nach dem Geschäftsjahr** (nur mit den 8-K zu finden): Sonderdividende mit Kredit (TASK), Fusion (PATK), Übernahme mit Kredit (MDXG), Rechtssitz-Wechsel und Zukauf (WFRD), Chef- und Finanzchefwechsel (CBT), Tauschvorschlag (PPC).
3. **Mietverpflichtungen** (MGM: 23,8 Mrd. $) und **Minderheitsanteile** (PLAB: 463 Mio. $; MGM China) fehlen in Net Debt/EBITDA und EV der Stufen 1 und 2.
4. **Vergleichsgruppen passen oft schlecht** (SimFin-Branchen sind grob; zum Beispiel stehen Mattel, Brunswick und Petco in „Travel & Leisure“, Visteon in „Engineering & Construction“, Photronics bei Chip-Herstellern). Das Archiv enthält zudem Datenfehler (NOV: EV/EBITDA 0,38).
5. **Datenmarke `debt_component_missing_defaulted_zero`** (Stufe 1) bei FHI, DFIN, BYD, MDXG, MGM: Ein Teil der Schulden fehlte und wurde auf 0 gesetzt. Bei FHI und DFIN stimmt der Wert grob mit den SEC-Zahlen überein, bei den anderen ist er nicht nachgerechnet.

Diese Befunde ändern keine Schwellen. Ob und wie der Filter angepasst wird (zum Beispiel ein Aktualitäts-Check mit den letzten zwölf Monaten, eine Miet-Behandlung), ist eine Entscheidung des Projektinhabers.
