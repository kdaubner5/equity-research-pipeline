# Zuordnung der Commit-Kennungen

Dieses Repo ist aus einem privaten Arbeitsrepo entstanden. Dabei wurde die Historie umgeschrieben (interne Dokumente und lizenzierte Rohdaten entfernt, Mailadressen vereinheitlicht). Dadurch tragen alle Commits neue Kennungen. Die Dokumente in `docs/` und die One-Pager nennen weiter die **alten** Kennungen des Arbeitsrepos (zum Beispiel als Regelstand eines Laufs). Diese Tabelle ordnet jede dort genannte Kennung dem Commit im öffentlichen Repo zu: `git show <neue Kennung>`.

Aufgenommen sind alle Kennungen, die in `docs/` oder in den READMEs vorkommen: 14 Commits. Erzeugt aus der Zuordnungsdatei von `git filter-repo` (`commit-map`).

| Alte Kennung (wie zitiert) | Neue Kennung | Betreff | Zitiert in |
|---|---|---|---|
| `0fd28de` | `5cbc720e74` | Phase 1.1: Schwellen nach config/thresholds.yaml (Issue #33) | `docs/decisions.md` |
| `2159ccb` | `5c6cf9bd16` | Phase 1.1: Post-Refactor-Evidenz in decisions.md, README-Verweis auf roic.nopat_tax_rate (Issue #33) | `docs/decisions.md` |
| `33ed39b` | `99637ef258` | Merge pull request #95 from kdaubner5/agent/evidenz-filter | `README.de.md`, `README.md`, `docs/decisions.md` und 42 weitere |
| `6a2e527` | `156761638c` | Autonomer Betrieb: Deny fuer master/main-Push, Muster korrigiert, Betriebsmodi dontAsk, Testprotokoll | `docs/decisions.md` |
| `774b087` | `433ff0dff5` | Merge pull request #67 from kdaubner5/agent/stufe2-plausibilitaet | `docs/decisions.md` |
| `7b4cdd2`, `7b4cdd2a3014ddcb6aca0bf7be911d6dc3010386` | `6579d48899` | Merge pull request #47 from kdaubner5/phase2.0d-status-precedence | `docs/decisions.md` |
| `7bc42ab` | `021027f7a5` | Phase 2.0a: Runner run_universe.py mit Fehlerisolation je Batch (Issue #39) | `docs/decisions.md` |
| `8b176e3` | `bf1dc170ed` | Merge pull request #101 from kdaubner5/agent/kurse-kandidaten | `docs/decisions.md` |
| `beeda72` | `825231ae5f` | Stufe 2 Plausibilitaet: Docstring und Entscheidungstext nach Kritiker-Lauf angeglichen | `docs/decisions.md` |
| `c3c24c1` | `dd5f52d518` | Merge pull request #70 from kdaubner5/agent/trend-guard-bau | `docs/decisions.md` |
| `ce56c7e` | `9c63c9f944` | Merge pull request #50 from kdaubner5/phase2.0c-volllauf-evidenz | `docs/decisions.md` |
| `dbbc26c` | `c18d570e0d` | Autonomer Betrieb: Stufe 1 - engere Berechtigungsliste, unbeaufsichtigt nicht freigegeben, Luecken, Testprotokoll | `docs/decisions.md` |
| `eea53e5` | `fd67becbc7` | Merge pull request #46 from kdaubner5/phase2.0c-stage1-full-universe | `docs/decisions.md` |
| `f7284e2` | `9d3e473bec` | Merge pull request #66 from kdaubner5/agent/stufe2-impl | `docs/decisions.md` |
