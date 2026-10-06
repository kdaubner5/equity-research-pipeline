# Bash-Guard (Stufe 2, Issue #60)

Ein Hook, der **vor jedem Bash-Aufruf** läuft und Befehle ablehnt, die auf `master` oder `main` pushen, dorthin mergen oder dort löschen würden. Er ist die technische Antwort auf die Befunde S2 bis S4 des Kritiker-Berichts (Umschreibungen von `git push`, „alles stagen“, `gh api` mit Merge). Er arbeitet unabhängig vom Berechtigungsmodus: Claude Code führt Hooks laut Dokumentation in `default`, `auto`, `dontAsk` und `bypassPermissions` aus; Exit-Code 2 blockiert den Aufruf.

## Bestandteile

| Datei | Aufgabe |
|---|---|
| `src/hooks/bash_guard.py` | der Hook (liest JSON von stdin, Exit 2 = abgelehnt, Exit 0 = weiter) |
| `config/bash_guard.yaml` | alle Muster, Optionen und Grenzen (geschützte Branches, verbotene Optionen, Pfadmuster, `gh`-Regeln, Tiefenlimit); keine Muster im Code |
| `.claude/settings.json` | trägt den Hook ein (`hooks.PreToolUse`, Matcher `Bash`) |
| `tests/test_bash_guard.py` | pytest-Tests für jede bekannte Schreibweise, auch Zeile 2 und 3 des Testprotokolls |

Der Eintrag in `settings.json` hängt `|| exit 2` an. Stürzt der Interpreter ab oder fehlt das venv, blockiert der Aufruf, statt durchzulaufen (Exit-Codes außer 2 blockieren sonst nicht). Im Skript selbst führt jeder Fehler (kaputte Konfiguration, ungültige Eingabe, nicht parsebarer Befehl, Verschachtelung zu tief, Befehl zu lang) ebenfalls zu Exit 2.

## Was abgelehnt wird

Der Hook zerlegt den Befehl mit einem Shell-Tokenizer (Anführungszeichen, `;`, `&&`, `||`, `|`, Klammern, Zeilenumbrüche, `$(…)`, Backticks, Heredocs) und prüft jedes Segment. Er sucht `git`, `gh`, `curl`/`wget`, Shells und Interpreter an jeder Stelle des Segments, also auch hinter `env`, `timeout`, `nice`, `nohup`, `time`, `sudo`, `exec`, `command`, `setsid`, `stdbuf`.

- **`git push`:** Ziel `master`/`main` in jeder Schreibweise (`refs/heads/…`, `HEAD:master`, `agent/x:master`, `ma''ster`, Groß-/Kleinschreibung); `+ref`, `:ref` (Löschen); `--force*`, `-f`, `-uf`, `--delete`, `-d`, `--mirror`, `--all`, `--tags`, `--follow-tags`, `--prune` (auch als eindeutige Abkürzung, z. B. `--forc`); Tag-Ziele; Muster und Variablen im Ziel (`'refs/heads/*:…'`, `$BRANCH`, `$(…)`); ein Push ohne Ziel (`git push`, `git push origin`, `git push origin HEAD`), wenn der aktuelle Branch geschützt ist (der Hook fragt `git symbolic-ref` ab; schlägt das fehl, wird abgelehnt).
- **Umschreibungen:** `git -C …`, `git -c …`, `--git-dir`, `/usr/bin/git`, `"git"`, `g''it`, `\git`, `bash -c`, `sh -c`, `bash -lc`, `eval`, verschachtelt (bis `max_depth`), Heredocs in Shells, `echo '…' | bash`, `bash <<<`, Shell ohne Skript (liest stdin), `xargs`/`parallel`, `find -exec`, Befehlswörter aus Variablen, Git-Aliase (`git config --get alias.<name>` wird aufgelöst; Aliase definieren ist abgelehnt).
- **`git add`:** `-A`, `--all`, `-u`, `-f`, `.`, `..`, `./`, Muster (`*`, `?`, `[`), `:/`, `:(top)`, `--pathspec-from-file`, `.env*`, `data/`, nicht aufgelöste Variablen. **`git commit -a`/`--all`.**
- **`gh`:** `gh pr merge` (auch mit `-R`/`--repo`), `gh alias set/import`; `gh api` mit schreibender Methode (`-X`, `--method`, `-XPUT`, oder implizit durch `-f/-F/--field/--raw-field/--input`), wenn ein Argument `merge`, einen geschützten Branch, `graphql`, `--input` oder `=@datei` enthält.
- **Rohes HTTP:** `curl`/`wget`/`http` mit Schreibmarker gegen `api.github.com`.
- **Inline-Code:** `python -c`, `node -e`, `perl -e`, `ruby -e` und Heredocs für diese Interpreter, wenn der Code einen Ausführungsmarker (`subprocess`, `os.system`, `child_process`, …) **und** ein `git … push`, `gh … merge`, `gh … api` oder `api.github.com` enthält (Heuristik).

Erlaubt bleiben die Formen, die der Agent braucht: `git push origin agent/<thema>` (auch mit `-u`, `--dry-run`), gezieltes `git add <Dateien>`, `git commit -m …`, `gh pr create/view/list/diff`, `gh issue comment/view/list`, `gh api` mit GET, pytest. Text, der nur zufällig nach einem Befehl aussieht (`echo "git push origin master"`, Commit-Meldungen, `grep`, Heredocs für `cat`), wird nicht beanstandet.

## Was der Hook NICHT sieht (bekannte Grenzen)

1. **Skriptdateien und pytest.** `bash script.sh`, `python x.py`, `make` und jede Testdatei unter `tests/` (auch `conftest.py`) führen beliebigen Code aus, den der Hook nicht liest (Befund S1). Das schließt nur eine Sandbox oder ein Review der Dateien.
2. **Verschleierter Code.** Strings, die ein Interpreter zusammenbaut (`"gi"+"t"`), kodierte Befehle, `python3 -c` ohne Ausführungsmarker.
3. **Konfiguration von git.** `remote.<name>.push`, `remote.<name>.mirror`, `push.default`, `help.autocorrect` können ein `git push` ohne Ziel verändern; der Hook prüft nur den aktuellen Branch. Pushes über einen anderen Weg (`git send-pack`, `git update-ref` auf Remote-Verzeichnisse, geänderte Remote-URL) sind nicht erfasst.
4. **Lokale Änderungen an `master`.** `git commit`, `git merge`, `git reset` auf einem ausgecheckten `master` sind nicht abgelehnt; die Regel „`master` wird nie lokal verändert“ bleibt in diesem Punkt Text.
5. **Der Hook selbst.** Wer `.claude/settings.json`, `src/hooks/bash_guard.py` oder `config/bash_guard.yaml` ändert (auch per `sed -i`, `python`), kann den Hook abschalten. Die Ask-Regeln greifen im Modus `bypassPermissions` nicht. Schutz bieten nur Einstellungen auf Nutzerebene des Servers, verwaltete Einstellungen oder Branch-Schutz (Issue #60).
6. **Andere Werkzeuge als Bash.** MCP-Werkzeuge (`mcp__github__merge_pull_request` usw.) werden weiter durch die Deny-Regeln in `settings.json` gesperrt, nicht durch diesen Hook.
7. **Falschmeldungen.** Der Tokenizer kennt nicht, ob ein `$(…)` in einfachen Anführungszeichen steht (dort ist es Text); er lehnt vorsichtshalber ab.
8. **Reichweite.** Der Hook gilt nur dort, wo `.claude/settings.json` mit dem Eintrag geladen wird (Checkout mit der Datei). Neue Sitzungen auf `master` haben ihn erst nach dem Merge; für jeden Checkout bräuchte es zusätzlich `~/.claude/settings.json` des Nutzers `claude` (Issue #60).

## Ändern und erweitern

Neue Befehle, Optionen, Pfadmuster oder zusätzliche geschützte Branches stehen in `config/bash_guard.yaml` (Klasse C). Zu jeder Änderung gehört ein Test in `tests/test_bash_guard.py`. Ein fehlender oder falscher Schlüssel in der YAML-Datei lässt den Hook **jeden** Bash-Aufruf ablehnen (fail closed); `tests/test_bash_guard.py` prüft, dass die echte Datei lädt.

## Prüfen

```
~/venvs/equity/bin/python -m pytest tests/test_bash_guard.py -q
echo '{"tool_name":"Bash","tool_input":{"command":"git status"},"cwd":"."}' | ~/venvs/equity/bin/python src/hooks/bash_guard.py; echo $?   # 0
```

Eine abgelehnte Beispielzeile gibt Exit 2 und eine Begründung auf stderr aus. Ob Claude Code den Hook in der echten Sitzung aufruft und blockiert (auch in `bypassPermissions`/`auto`), zeigt erst ein Lauf in einer beaufsichtigten Sitzung mit den ungefährlichen Dry-Run-Befehlen aus `docs/decisions.md` (Testprotokoll, Zeilen 1 bis 7); dieser Lauf steht aus.
