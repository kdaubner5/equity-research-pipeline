# Arbeitsweise

Diese Datei legt fest, wie im Projekt gearbeitet wird: Claude Code zerlegt Aufgaben selbst, formuliert seine Arbeitsaufträge selbst, lässt das Ergebnis unabhängig prüfen und legt dem Projektinhaber nur Berichte und große Entscheidungen vor. Die ständigen Kurzregeln stehen in `CLAUDE.md` (Abschnitt „Arbeitsweise"); hier steht die Ausführung. Der Projektinhaber ist kein Programmierer: Berichte sind in einfacher Sprache geschrieben, Fachbegriffe werden kurz erklärt, Zahlen stehen immer mit Bezugsgröße („12 von 29").

## A. Rollen

Die Rollen sind durch eine frische Sitzung bzw. einen Subagenten voneinander getrennt. Wer eine Rolle ausübt, übt die anderen im selben Auftrag nicht aus.

**Architekt**
- Zerlegt die Roadmap in Issues.
- Schreibt den Entscheidungsentwurf in `docs/decisions.md` **vor** der Umsetzung.
- Formuliert Vorab-Hypothesen mit erwarteten Zahlen.
- Ordnet die Risikoklasse zu (Abschnitt C).
- Schreibt den Arbeitsauftrag mit Stoppregeln (Abschnitt E).

**Builder**
- Schreibt zuerst die Tests, dann die Umsetzung.
- Verifiziert das Ergebnis, öffnet den PR und schreibt den Bericht (Berichtsformat, siehe `CLAUDE.md`).

**Kritiker** (frische Sitzung oder Subagent)
- Bekommt **nur** das Issue, den Entscheidungstext, den Diff und die Regeln, **nicht** die Begründung des Builders.
- Prüft nach der Prüfroutine (Abschnitt B) und postet die Befunde als PR-Kommentar.
- Der Builder behebt die Befunde, der Kritiker prüft erneut. Höchstens 2 Runden, danach Eskalation an den Projektinhaber.
- **Merge-Bedingung (nicht überlesen):** Ein Merge durch Claude Code setzt voraus, dass der Kritiker alle Punkte der Prüfroutine abgehakt hat, keine Befunde offen sind und der Kritiker-Bericht als PR-Kommentar steht; bei Klasse B zusätzlich, dass das Golden Master identisch ist. Zusätzlich braucht es die ausdrückliche Freigabe des Projektinhabers in `docs/decisions.md` (Abschnitt C).

## B. Prüfroutine des Kritikers

Jeder Punkt wird abgehakt oder begründet verneint.

1. **Unabhängig vom Bericht:** Branch frisch holen, Diffstat gegen die Basis ansehen. Entspricht der Umfang dem Auftrag? Sind nur erlaubte Dateien geändert?
2. **Tests selbst ausführen.** Die Anzahl erklären (Basis plus neue Tests); übersprungene Tests benennen.
3. **Zahlen nachrechnen.** Summen und Aufteilungen müssen aufgehen (z. B. 80 + 449 + 2 = 531). Prozentangaben brauchen eine Bezugsgröße; Nenner prüfen.
4. **Behauptungen in Bericht und Dokumentation gegen den Code prüfen.** Die Funktion lesen, nicht nur den Kommentar.
5. **Stille Fehlerpfade suchen:** unbekannter Wert führt zum Abbruch statt zu einem Standardwert; falsche Typen; Duplikate in anderer Schreibweise; Sonderzeichen in Pfaden; Wertebereiche.
6. **Entscheidungstreue:** Ist jede Abweichung vom freigegebenen Text benannt?
7. **Belegtes und Ungeprüftes getrennt?** Sind Vorab-Hypothesen vorhanden und mit dem Ist-Ergebnis verglichen? Keine Wertung ohne Zahl.
8. **Konventionen aus `CLAUDE.md`:** Schwellen nur in YAML, Formeln mit pytest getestet, Datenintegrität ohne Abkürzungen.
9. **Golden Master bzw. Regressionsbasis:** Ergebnis identisch, oder jede Änderung exakt erklärt.
10. **Kommentare und Docstrings:** Zahlenbeispiele nachrechnen.
11. **Schlussfrage:** „Was könnte hier falsch sein, das niemand geprüft hat?" Mindestens drei Punkte benennen.

## C. Risikoklassen

Die Zuordnung ist mechanisch, nicht Ermessenssache.

- **Klasse A:** Dokumentation, **neue** Tests, Analyse ohne Änderung an Code. „Code" meint hier `src/`, die Python-Skripte im Hauptordner, `requirements.txt` und `pyproject.toml` (siehe Klasse B). Ergänzungen in `docs/decisions.md` sind Klasse A.
- **Klasse B:** Code mit nachgewiesen identischem Ergebnis (Golden Master gleich). Dazu zählen:
  - Code in `src/`
  - die Python-Skripte im Hauptordner (z. B. `run_stage1_full.py`, `run_stage1.py`, `run_universe.py`, `run_fundamentals.py`)
  - `requirements.txt` und `pyproject.toml`
  - Änderungen an **bestehenden** Tests (nicht an neuen): mindestens Klasse B. Der Bericht begründet jede solche Änderung einzeln: welcher Test, was geändert wurde und warum das Ergebnis dadurch nicht verschleiert wird.
- **Klasse C:** Alles, was ändert, welche Firmen bestehen oder wie Status, CSV oder Datenbank aussehen. Dazu zählen:
  - `config/thresholds.yaml`
  - der Inhalt von `config/sector_profiles.yaml`
  - die Statuslogik in `gates_layer`
  - `metrics_layer`
  - Mapper-Regeln
  - Datenbank-Schema und Migrationen
  - CSV-Spalten
  - Datenquelle oder Tarif
- **Immer Klasse C** sind außerdem:
  - `CLAUDE.md` und `docs/arbeitsweise.md` (die Regeln der Autonomie selbst)
  - alles unter `.claude/` (Berechtigungslisten, Hooks, Einstellungen) und `.github/` (Workflows)
  - jede Änderung oder Löschung bereits freigegebener Entscheidungstexte in `docs/decisions.md` (Ergänzen ist erlaubt, Ändern nicht)

  Begründung: Der Agent darf seine eigenen Regeln nicht selbst ändern oder freigeben.

**Merge-Recht:**
- Klasse C wird nie ohne Freigabe des Projektinhabers gemergt, und zwar sowohl für den Entscheidungstext als auch für den Merge.
- Klasse A und B: Claude Code darf nur mergen, wenn **alle** drei Bedingungen erfüllt sind:
  - (a) Der Projektinhaber hat dieses Recht ausdrücklich in `docs/decisions.md` freigegeben (frühestens nach drei geprüften Testschichten).
  - (b) Der Kritiker hat alle Punkte der Prüfroutine (Abschnitt B) abgehakt, es sind keine Befunde offen, und der Kritiker-Bericht steht als PR-Kommentar.
  - (c) Bei Klasse B ist zusätzlich das Golden Master identisch.

  Fehlt eine Bedingung, mergt nur der Projektinhaber oder Claude Code auf dessen Ansage.

## D. Was der Projektinhaber entscheidet

- eine neue Phase oder Richtung
- Regeln, Schwellen und die Bedeutung der Status
- Datenquelle, Tarif und Kosten
- alles mit Depotbezug
- Merges der Klasse C
- die Regeln der Autonomie selbst

Alles andere entscheidet der Architekt, mit Begründung und Alternativen in `docs/decisions.md`, gekennzeichnet als „Agent-Entscheidung, Klasse X". Der Projektinhaber kann widersprechen.

## E. Stoppregeln

Bei einem Verstoß wird angehalten, ein Blocker-Bericht geschrieben und nicht improvisiert. Verstöße sind:

- Abweichung vom Entscheidungstext.
- Berührung von Klasse C ohne Freigabe.
- Tests rot nach 3 Versuchen.
- Das Ergebnis weicht von der Vorab-Hypothese ab (berichten, nicht anpassen).
- Die Aufgabe braucht etwas Verbotenes (echte Datenbank, SimFin-Call, Schwellenänderung).
- Unklarheit über das Ziel: als Frage in den Bericht schreiben und unabhängige Teilaufgaben weiterführen.

## F. Schichtbericht

Am Ende jeder Arbeitssitzung steht ein Bericht im Berichtsformat (`CLAUDE.md`), zusätzlich „Stand und nächste Schritte" in 5 Zeilen.
