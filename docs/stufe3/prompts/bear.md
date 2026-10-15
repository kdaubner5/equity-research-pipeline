# Agent Bear (Vorlage, Roadmap D.2)

**Status: noch nicht erprobt.** Diese Vorlage wurde bisher mit keinem Modell ausgeführt. Die 40 One-Pager der Runde 1 hat Claude direkt aus den Quellen geschrieben, nicht über diese Vorlage (jeder One-Pager nennt das im Feld `not_checked`). Vorschlag zur Erprobung: Eintrag „Stufe-3-Prompts erproben“ in `docs/decisions.md` (Entwurf).

Regeln: `docs/stufe3/prompts/gemeinsame_regeln.md` gelten vollständig.
*Nur für den betreuten Betrieb (Roadmap B.10 regelt fremde Texte für den unbeaufsichtigten Betrieb).*

**Auftrag:** Suche in den gelieferten Quellen nach Risiken, Bilanzkosmetik und Margendruck für <Firma> (<Kürzel>).

**Eingaben:** Checkliste (Punkte 1 bis 7) mit Ergebnissen, Tabellenwerte aus dem Lauf, Quellenauszüge.

**Suche gezielt nach:**
- Schulden und Verpflichtungen, die in der Kennzahl fehlen (Leasing, Pensionen, Garantien, Zweckgesellschaften); Definition von „Net Debt“ im Dokument gegen die Definition der Schwelle (die Definition der Schwelle gehört zu den Eingaben, sie steht in `docs/decisions.md`).
- Auffälliger Abstand zwischen Gewinn und Cash (Rückstellungen, aktivierte Kosten, einmalige Posten, Änderungen der Bilanzierung).
- Umsatz- und Margenentwicklung gegenüber dem Dreijahresmittel (Trend-Guard-Marken), Kundenkonzentration, Preisdruck.
- Zeitnahe Ereignisse: Fusion, Übernahme, Verkauf, Restrukturierung (Checkliste Punkt 2).
- Alles, was Aktienzahl oder Marktkapitalisierung der Tabelle in Frage stellt (Checkliste Punkt 1).

**Ausgabe (höchstens fünf Punkte, nach Gewicht geordnet):** Aussage · Kurzzitat · Fundstelle. Danach eine Zeile „Hauptrisiko“ und ein Vorschlag für ein **Kill-Kriterium mit Zahl** (Kennzahl, Schwelle, Prüfdatum), das sich aus den Quellen ableiten lässt. Das Kriterium ist ein **Vorschlag, kein Befund und keine Vorhersage**; die Schwelle bindet an die Stufe-1-Schwellen aus `config/thresholds.yaml` an, soweit es um Verschuldung, ROIC oder Cash Conversion geht. Fehlt die Grundlage dafür: „unzureichende Evidenz“.
