# Agent Synthese (Vorlage, Roadmap D.2)

**Status: noch nicht erprobt.** Diese Vorlage wurde bisher mit keinem Modell ausgeführt. Die 40 One-Pager der Runde 1 hat Claude direkt aus den Quellen geschrieben, nicht über diese Vorlage (jeder One-Pager nennt das im Feld `not_checked`). Vorschlag zur Erprobung: Eintrag „Stufe-3-Prompts erproben“ in `docs/decisions.md` (Entwurf).

Regeln: `docs/stufe3/prompts/gemeinsame_regeln.md` gelten vollständig.
*Nur für den betreuten Betrieb (Roadmap B.10 regelt fremde Texte für den unbeaufsichtigten Betrieb).*

**Auftrag:** Schreibe den One-Pager für <Firma> (<Kürzel>) nach `docs/stufe3/one_pager_vorlage.md` aus den Ausgaben von Bear und Bull, der Checkliste mit Ergebnissen und den Werten des Rechenwerkzeugs.

**Pflichtinhalt:** Kernthese (zwei bis drei Sätze, ohne Vorhersage), Katalysator als angekündigte oder eingetretene Ereignisse laut Quellen mit Datum (keine erwarteten), Zahlentabelle mit Quelle und Datum je Zeile, Bear- und Bull-Punkte, Hauptrisiko in einem Satz, Kill-Kriterium mit Zahl, Einstiegsszenario (Zeithorizont als Vorschlag, die Einstiegsgrenze aus der Ausgabe von `run_guidance_calc.py` unverändert übernommen, Einstiegsbedingung; **kein Kursziel und keine Prognose**; fehlen Aktienzahl, Zielvielfaches oder Kurs, steht dort „unzureichende Evidenz“), Marken und Beobachtungen, Quellenliste.

**Regeln für die Zusammenführung:**
- Widersprechen sich Bear und Bull oder eine Quelle der Tabelle, steht der Widerspruch im One-Pager, nicht eine Auflösung.
- Die Verdikt-Zeile ist ein **Vorschlag** mit einem Satz Begründung; die Entscheidung trifft Konstantin. Das Kauf-Feld bleibt leer.
- Reicht die Evidenz für eine Pflichtzeile nicht, steht dort „unzureichende Evidenz“. Fehlt sie für mehrere Pflichtzeilen oder für eine, auf der die Kernthese beruht, lautet der Verdikt-Vorschlag „unzureichende Evidenz“; eine feste Anzahl ist nicht beschlossen.
