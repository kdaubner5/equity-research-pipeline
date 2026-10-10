# Evidenz-Check der Filterstruktur

Stand: 09.10.2026. Frage von Konstantin: „Ist die Filterstruktur richtig, wissenschaftlich belegt und kein Humbug?“ Dieses Dokument trennt, was die Forschung stützt, von dem, was nur eine Faustregel oder ein Platzhalter ist. Es ändert keine Regel und keine Schwelle; Entscheidungen bleiben bei Konstantin (`docs/decisions.md`).

**Kurzfazit.** Zwei Bausteine (Rentabilität, günstige Bewertung) haben in der Forschung klare Belege; zwei weitere (Cash-Deckung der Gewinne, Meiden von hohem Ausfallrisiko) haben verwandte, aber nicht identische Belege. Die *konkreten Zahlen* (12 %, 2,3, 1,0, 6 % und so weiter) und die Umsetzung mit kostenlosen Daten sind **nicht** wissenschaftlich belegt; sie sind Startwerte oder Faustregeln. Ob die Auswahl hier einen Vorsprung bringt, ist **offen**, und der Forward-Log kann es mit wenigen Firmen nur schwer zeigen. Das System behauptet das auch nicht: Der Masterplan sagt, die Rendite entstehe nicht durch den Screen allein (Abschnitt 1); der Trichter soll gesunde Firmen vorsortieren, die Stufe 3 und Konstantin dann prüfen.

## Begriffe

NOPAT: Betriebsgewinn nach Steuern. CFO: operativer Cashflow. FCF: freier Cashflow (CFO minus Investitionen). Perzentil: Platz in der Vergleichsgruppe von 0 (günstigste Firma) bis 1 (teuerste). Peer-Gruppe: Vergleichsfirmen derselben Branche. Tracking Error: die übliche Abweichung einer Auswahl von einem Vergleichsindex. Midstream: Öl- und Gaspipelines. Weitere Begriffe: `docs/masterplan.md`, Abschnitt 13.

## Wie man die Tabelle liest

- **Herkunft** sagt, woher die Zahl im Repo kommt (Masterplan, Platzhalter, Startwert). „Keine Quelle genannt“ heißt: Im Repo steht dazu keine Begründung; es ist **keine** Aussage, dass es eine gäbe oder nicht gäbe.
- **Forschungsstand** nennt Arbeiten, die die *Richtung* der Kennzahl stützen, nicht die Schwelle und oft nicht dieselbe Kennzahl; wo die Arbeit eine verwandte Größe misst, steht „verwandt, nicht identisch“. Titel, Zeitschrift, Jahr und die kurzen Befunde wurden am 09.10.2026 über eine Websuche bestätigt; die Volltexte wurden nicht gelesen. Die Befunde stammen aus US-Daten und größtenteils aus Zeiträumen vor 2010; für kleine und mittlere Firmen und nach Kosten sind sie nicht geprüft.

## Stufe 1: Gesundheit (`config/thresholds.yaml`)

| Baustein | Schwellen | Herkunft | Forschungsstand (Art der Kennzahl) | Schwächen und offene Messung |
|---|---|---|---|---|
| ROIC, Dreijahresschnitt (NOPAT = Betriebsgewinn nach Steuern; Invested Capital = Eigenkapital + Nettoschulden) | über 12 % sauber, 10 % bis 12 % Marke, unter 10 % durchgefallen; Hilfswerte: Invested Capital mindestens 10 % der Bilanzsumme, einzelnes Jahr über 60 % ungültig | Masterplan §6; Zahlen ohne genannte Quelle; fester Steuersatz 25 % | Profitabilität sagt Renditen voraus (Novy-Marx 2013 misst Bruttogewinn / Vermögen; Asness/Frazzini/Pedersen 2019 bündeln Profitabilität, Wachstum und Sicherheit); verwandt, nicht identisch mit ROIC bei festem Steuersatz | 10 % bis 12 % sind Faustregel in der Nähe typischer Kapitalkosten; fester Steuersatz verzerrt Firmen mit anderem Satz; Empfindlichkeit offen (PR #94) |
| Net Debt/EBITDA, letztes Jahr | unter 2,3 sauber, 2,3 bis unter 2,5 Marke, ab 2,5 durchgefallen | Masterplan §6; keine Quelle genannt | Firmen mit hohem *modelliertem Ausfallrisiko* (Modell aus Gewinn, Verschuldung, Schwankung und Kursverlauf) erzielten seit 1981 niedrigere Renditen bei höherem Risiko (Campbell/Hilscher/Szilagyi 2008). Das stützt das Meiden von Ausfallrisiko, nicht die Kennzahl Net Debt/EBITDA und nicht die Grenze 2,3/2,5; Net Debt/EBITDA ist ein grober Stellvertreter | Dieselbe Grenze für alle Branchen; Versorger, Telekom, Midstream fallen strukturell heraus (Masterplan §11); die enge Grauzone (0,2) macht das Urteil bei Grenzfällen instabil |
| Cash Conversion, Dreijahresschnitt (CFO = operativer Cashflow, geteilt durch den Gewinn) | ab 1,0 sauber, 0,8 bis unter 1,0 Marke, unter 0,8 durchgefallen | Masterplan §6; keine Quelle genannt | Firmen, deren Gewinn aus Bilanzposten statt aus Cash stammt (hohe „Accruals“: Gewinn minus operativer Cashflow, bezogen auf das Vermögen), erzielten niedrigere Folgerenditen (Sloan 1996); verwandt, nicht identisch mit CFO / Gewinn | Gilt nur bei positivem Gewinn; Wachstumsfirmen und Zykliker können legitim darunter liegen |
| Nettomarge, Dreijahresschnitt | ab 2 % | Masterplan §6: Untergrenze, damit ein winziger Gewinn den Cash-Test nicht leer besteht | Keine eigene Quelle; Hilfsregel zum Cash-Test | Reine Faustregel; für Überlebende nicht im Archiv gespeichert (Empfindlichkeit dort nicht prüfbar) |

## Stufe 2: Bewertung im Vergleich (`config/stage2.yaml`)

| Baustein | Schwellen | Herkunft | Forschungsstand (Art der Kennzahl) | Schwächen und offene Messung |
|---|---|---|---|---|
| EV/EBITDA, Perzentil in der Peer-Gruppe | günstigstes Drittel sauber, bis 0,40 Marke, darüber durchgefallen; Peer-Gruppe mindestens 8 | Drittel und Gruppengröße aus dem Masterplan; **0,40 ist ein Platzhalter** (Q2) | Der Enterprise Multiple (Unternehmenswert / EBITDA) sagt Renditen voraus (Loughran/Wellman 2011; der von ihnen gebildete Faktor hatte in ihrer Stichprobe eine Prämie von etwa 5,3 % im Jahr, brutto und nicht der Ertrag einer Auswahl) | Qualitätsfirmen handeln teurer und fallen seltener durch (Masterplan §11); kleine Gruppen machen Grenzfälle instabil; Gruppen nach SimFin-Sektorcode (Fehlzuordnung im Pilot); keine Größenachse |
| EV/EBITDA absolut (nur ohne Peer-Gruppe) | unter 8 sauber, ab 10 durchgefallen | **Platzhalter** (Q1) | wie oben | Ohne jede Quelle; gilt nur für die Firmen ohne Peer-Gruppe |
| FCF-Rendite, Dreijahresschnitt | über 6 % sauber, 5 % bis 6 % Marke, unter 5 % durchgefallen | 6 % aus dem Masterplan; **5 % ist ein Platzhalter** (Q2) | Cashflow im Verhältnis zum Preis ist ein klassisches Value-Maß (Fama/French, Lakonishok/Shleifer/Vishny; aus dem Gedächtnis, nicht frisch geprüft) | Dreijahresschnitt glättet, hinkt aber; Definition des FCF verschieden zwischen SimFin und Yahoo (JBI, PLAB, MDXG) |
| Plausibilitätsgrenzen und Datenprüfungen | EV/EBITDA über 100 und FCF-Rendite über 50 % ungültig (Startwerte ohne Messquelle); Aktienzahl-Prüfung über das Dividendenverhältnis (verdächtig über 6,0, Band 9 bis 15; Startwerte aus der Messung vom 07.10.2026: Median 3,92, 50 von 758 prüfbaren Firmen über 6); Toleranz im Schulden-Abgleich 5 % des Schuldenstands (0,1 % der Bilanzsumme als Boden); jüngstes Geschäftsjahr höchstens 1 Jahr alt; Gegenrechnung der sonstigen Investitionen ab dem Doppelten | Startwerte, nicht kalibriert | Technische Datenprüfung, keine Renditethese | Fängt Datenfehler nur teilweise (XPER wurde von Hand gefunden) |

## Trend-Guard und Vorfilter

| Baustein | Schwellen | Herkunft | Anmerkung |
|---|---|---|---|
| Trend-Guard T1 bis T4 (3 Jahre) | T1 Umsatz und T2 EBITDA-Marge unter 0,90, T4 Marge über 1,25 (jeweils Verhältnis letztes Jahr zum Dreijahresmittel); T3 hat keine eigene Schwelle, sie nimmt die ROIC-Grenze von 10 % aus Stufe 1 | **Platzhalter** (Entscheidung Q7) | Nur Marken, zählen nicht im Rang. Fängt den Gewinngipfel-Fall nur als Sprung gegenüber dem Dreijahresmittel |
| Vorfilter | Marktkapitalisierung 300 Mio. bis 10 Mrd. $, Median-Dollarumsatz mindestens 1 Mio. $ pro Tag, Kurs höchstens 5 Kalendertage alt, Median über 60 Handelstage | Startwerte, laut Konfigurationsdatei nicht an den Pilotfirmen kalibriert | Größenklasse und Liquidität sind Schutz vor illiquiden Fallen; die Obergrenze soll laut Masterplan entfallen (Roadmap C.8) |

## Strukturelle Schwächen, unabhängig von einzelnen Zahlen

1. **Auswahl nach Datenlage.** Die Urteilsquote lag am 30.09.2026 bei 484 von 1.290 (37,5 %) (Masterplan §7); die Überlebenden sind eher Firmen mit einfachen Bilanzen und sauberen Daten. Das ist gemessen, aber noch nicht behoben (Roadmap Phase C).
2. **Einheitliche Schwellen für alle Nichtfinanzwerte.** Branchenprofile mit eigenen Schwellen sind erst später vorgesehen (Roadmap G.4); bis dahin bevorzugt der Filter schuldenarme, kapitalleichte Modelle (Masterplan §11).
3. **Adverse Selektion.** Ein Filter auf „billig und gesund“ findet auch „billig aus gutem Grund“; die Standard-Vermutung bei jedem Kandidaten ist deshalb Skepsis (Masterplan §11), und Stufe 3 prüft von Hand.
4. **Veraltete Forschungsprämien.** Nach der Veröffentlichung sind Renditevorsprünge im Schnitt kleiner geworden: McLean/Pontiff (2016) fanden bei 97 Merkmalen etwa 26 % weniger Rendite außerhalb der Stichprobe und etwa 58 % weniger nach der Veröffentlichung; die Differenz von etwa 32 Prozentpunkten schreiben sie dem Handeln der Anleger nach der Veröffentlichung zu, die 26 % der Auswahl der Merkmale in der Stichprobe. Das ist eine Warnung vor Erwartungen, kein Beleg gegen die einzelne Kennzahl.
5. **Ein Durchschnittseffekt über viele Aktien sagt wenig über wenige.** Eine gleich gewichtete Auswahl von 10 bis 40 Firmen weicht von einem Index allein durch Zufall um etwa 5 bis 10 Prozentpunkte im Jahr ab (Schätzung aus der Schwankung einzelner Aktien von etwa 30 bis 35 % im Jahr, geteilt durch die Wurzel der Anzahl; ungeprüft). Ein Vorsprung von 3 Prozentpunkten pro Jahr (Startwert X im Abbruchkriterium der Messlatte) wäre in zwei Jahren kaum von Zufall zu trennen. Das ist eine grobe, ungeprüfte Überlegung; der Masterplan (Abschnitt 8) nennt die Grenze der Aussagekraft des Forward-Logs ebenfalls.

## Was wir messen können und was nicht

| Maßnahme | Was sie zeigt | Was sie nicht zeigt |
|---|---|---|
| Empfindlichkeitstest Stufe 1 (PR #94) | welche Gate-Schwellen die Menge der Überlebenden steuern | ob die Schwellen richtig sind; Stufe 2 und Rang |
| Empfindlichkeitstest Stufe 2 (offen) | welche Bewertungsschwellen die Kandidatenliste steuern | dasselbe |
| Lückenkarte (Roadmap C.1) | wo die Datenlücke nach Branche und Größe sitzt, also wen der Filter systematisch verliert | ob die Verlierer die besseren Firmen waren |
| Forward-Log mit Messlatte (Auswahl gegen S&P 500 in Dollar, Depot gegen MSCI World in Euro, zusätzlich Option C = Korb aus den 10 bestplatzierten Firmen nach Stufe 2 als Zusatz für das Urteil) | ob die Auswahl im Echtbetrieb besser oder schlechter als die Vergleiche war | Ursache; bei wenigen Firmen und zwei Jahren kaum Signifikanz |
| Rückblick-Test mit vorhandenen SimFin-Daten (**nicht geplant**: Der Masterplan, Abschnitt 2, und die Roadmap schließen einen eigenen Backtest aus; nur nach einer Entscheidung von Konstantin) | Hinweis, ob frühere Jahrgänge der Auswahl besser abgeschnitten hätten | wegen nachträglich korrigierter Zahlen und fehlender Zeitstempel eher zu optimistisch |

## Was ich vorschlage (Entscheidung bei Konstantin)

1. Den Empfindlichkeitstest für Stufe 1 (offener PR #94) auf einer Archivkopie laufen lassen, danach Stufe 2 bauen.
2. Vor einer Änderung einer Schwelle immer zuerst die Wirkung messen (Empfindlichkeit), nie nach Gefühl ändern. Schwellenänderungen bleiben eine Entscheidung (CLAUDE.md).
3. Den Echtbetrieb als Beobachtung behandeln: archivieren, vergleichen, klein bleiben, bis es mindestens 24 Monate und eine ehrliche Auswertung gibt.
4. Die Platzhalter nach und nach durch Messungen ersetzen oder bewusst als Faustregeln stehen lassen und so benennen.

## Quellen (am 09.10.2026 per Websuche bestätigt: Titel, Journal, Jahr, Kurzbefund; Volltexte nicht gelesen)

- Novy-Marx, R. (2013): The Other Side of Value: The Gross Profitability Premium. Journal of Financial Economics 108(1), 1–28.
- Sloan, R. G. (1996): Do Stock Prices Fully Reflect Information in Accruals and Cash Flows about Future Earnings? The Accounting Review 71(3), 289–315.
- Loughran, T., Wellman, J. W. (2011): New Evidence on the Relation between the Enterprise Multiple and Average Stock Returns. Journal of Financial and Quantitative Analysis 46(6), 1629–1650.
- Campbell, J. Y., Hilscher, J., Szilagyi, J. (2008): In Search of Distress Risk. Journal of Finance 63(6), 2899–2939.
- McLean, R. D., Pontiff, J. (2016): Does Academic Research Destroy Stock Return Predictability? Journal of Finance (Jahr bestätigt; Band und Seiten nicht geprüft).
- Asness, C. S., Frazzini, A., Pedersen, L. H. (2019): Quality Minus Junk. Review of Accounting Studies 24(1), 34–112.
- Nicht frisch geprüft (aus dem Gedächtnis): Fama/French (1992, 1993, 2015) und Lakonishok/Shleifer/Vishny (1994); diese beiden sind die Quelle für die Aussage zum FCF-Verhältnis in der Tabelle zu Stufe 2.
