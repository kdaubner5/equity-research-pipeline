# Bash-Guard (Stufe 2, Issue #60)

Ein Hook, der **vor jedem Bash-Aufruf** läuft und Befehle ablehnt, die auf `master` oder `main` pushen, dorthin mergen, dort löschen oder sie lokal verändern würden. Er ist die technische Antwort auf die Befunde S2 bis S4 des Kritiker-Berichts zu Stufe 1 und auf die Befunde aus Lauf 1 zu PR #61. Er arbeitet unabhängig vom Berechtigungsmodus: Claude Code führt Hooks laut Dokumentation in `default`, `auto`, `dontAsk` und `bypassPermissions` aus; Exit-Code 2 blockiert den Aufruf.

## Bestandteile

| Datei | Aufgabe |
|---|---|
| `src/hooks/bash_guard.py` | der Hook (liest JSON von stdin, Exit 2 = abgelehnt, Exit 0 = weiter) |
| `config/bash_guard.yaml` | alle Muster, Optionen, Grenzen und Zeitbudgets (geschützte Branches, verbotene Optionen, Pfadmuster, `gh`-Regeln, Tiefen- und Zeitlimit); keine Muster im Code |
| `.claude/settings.json` | trägt den Hook ein (`hooks.PreToolUse`, Matcher `Bash`, `timeout` 30) |
| `tests/test_bash_guard.py` | pytest-Tests für jede bekannte Schreibweise, auch Zeile 2 und 3 des Testprotokolls; führt den eingetragenen Befehl wie Claude Code über `sh -c` aus |

Der Eintrag in `settings.json` hängt `|| exit 2` an. Stürzt der Interpreter ab oder fehlt das venv, blockiert der Aufruf, statt durchzulaufen (Exit-Codes außer 2 blockieren sonst nicht; ein Test führt das mit fehlendem und mit abstürzendem Interpreter aus). Im Skript selbst führt jeder Fehler (kaputte Konfiguration, ungültige Eingabe ohne `tool_name`, nicht parsebarer Befehl, Verschachtelung zu tief, Befehl zu lang, überschrittenes Zeitbudget) ebenfalls zu Exit 2.

**Zeitbudget:** Claude Code blockiert einen Aufruf **nicht**, wenn der Hook in sein eigenes Zeitlimit läuft (`timeout` in `settings.json`, 30 s). Der Hook gibt sich deshalb ein kleineres Budget (`limits.analysis_seconds`, 10 s) und lehnt ab, wenn es überschritten ist. Git-Abfragen (aktueller Branch, Aliase) werden je Befehl nur einmal ausgeführt und begrenzt (`limits.git_lookup_seconds`).

## Was abgelehnt wird

Der Hook zerlegt den Befehl mit einem Shell-Tokenizer (Anführungszeichen, `;`, `&&`, `||`, `|`, Klammern, Zeilenumbrüche, `$(…)`, Backticks, Heredocs, Umleitungen) und prüft jedes Segment. Er sucht `git`, `gh`, `curl`/`wget`, Shells und Interpreter an jeder Stelle des Segments (auch hinter `env`, `timeout`, `nice`, `nohup`, `time`, `sudo`, `exec`, `command`, `setsid`, `stdbuf`) und analysiert Befehlsstrings, die an Wrapper wie `env -S`, `su -c`, `ssh`, `watch`, `tmux`, `flock`, `script` übergeben werden.

- **`git push`:** Ziel `master`/`main` in jeder Schreibweise, die er auflösen kann (`refs/heads/…`, `HEAD:master`, `agent/x:master`, `ma''ster`, Groß-/Kleinschreibung, `%`-kodiert bei `gh api`); `+ref`, `:ref` (Löschen); `--force*`, `-f`, `-uf`, `--delete`, `-d`, `--mirror`, `--all`, `--tags`, `--follow-tags`, `--prune`, `--receive-pack`, `--exec` (auch als eindeutige Abkürzung, z. B. `--forc`); Ziele außerhalb von `refs/heads/` (Tags, `refs/notes`, `refs/pull`); Muster und Variablen im Refspec (`'refs/heads/*:…'`, `$BRANCH`, `${B:-master}`, `$(…)`); ein Push ohne Ziel (`git push`, `git push origin`, `git push origin HEAD`), wenn der aktuelle Branch geschützt ist (der Hook fragt `git symbolic-ref` ab; schlägt das fehl, wird abgelehnt).
- **Umschreibungen:** `git -C …`, `git -c …`, `--git-dir`, `git-push` (Bindestrich), `/usr/bin/git`, `"git"`, `g''it`, `\git`, Umleitungen mitten im Befehl (`git push 2>&1 origin master`), `bash -c`, `sh -c`, `bash -lc`, `eval`, verschachtelt (bis `max_depth`), Heredocs in Shells, `echo '…' | bash`, `bash <<<`, Shell ohne Skript (liest stdin), `xargs`/`parallel`, `find -exec`, Befehlswörter aus Variablen, NUL-Bytes, Git-Aliase (`git config --get alias.<name>` wird aufgelöst).
- **Git-Konfiguration:** `git -c`/`git config` mit `alias.*`, `remote.*`, `push.*`, `branch.*`, `url.*`, `core.sshcommand`, `core.hookspath` u. a. (Lesen mit `--get`, `--list` bleibt erlaubt); Umgebungsvariablen `GIT_CONFIG*`, `GIT_DIR`, `GIT_WORK_TREE`, `GIT_EXEC_PATH`, `GIT_SSH*`, `GIT_PROXY`, `GIT_ASKPASS` vor einem Befehl.
- **Lokale Änderungen an `master`/`main`:** auf einem geschützten Branch `git commit`, `merge`, `reset`, `rebase`, `cherry-pick`, `revert`, `am` und `git pull` ohne `--ff-only`; mit einem geschützten Branchnamen im Befehl `git branch -f/-d/-D/-m/-M/-c/-C`, `git switch -f/-C`, `git checkout -f/-B`, `git update-ref`, `git fetch +…:master`; immer `git send-pack`. Erlaubt bleibt Fast-Forward (`git pull --ff-only`, `git fetch`).
- **`git add` / `git commit`:** `-A`, `--all`, `-u`, `-f`, `-i`, `-e`, `.`, `..`, `./`, `src/..`, absolute Pfade, die das Arbeitsverzeichnis enthalten, leere Pfade, Muster (`*`, `?`, `[`), Pathspec-Magie (`:!x`, `:(exclude)x`, `:/`), `--pathspec-from-file`, `.env*` und `data/` (Groß-/Kleinschreibung egal), nicht aufgelöste Variablen; `git commit -a`/`--all` (auch abgekürzt) und die gleichen Pfadregeln für Pfade bei `git commit`. Echte Optionen wie `--allow-empty` bleiben erlaubt.
- **`gh`:** `gh pr merge` (auch mit `-R`/`--repo`), `gh alias set/import`; **jede** schreibende `gh api`-Anfrage (`-X`, `--method`, `-XPUT`, oder implizit durch `-f/-F/--field/--raw-field/--input`; `config/bash_guard.yaml`: `gh.api.refuse_all_writes`). Lesende `gh api`-Aufrufe (GET) bleiben erlaubt.
- **Rohes HTTP:** `curl`/`wget`/`http` mit Schreibmarker gegen `github.com`/`api.github.com`/`githubusercontent.com` oder gegen einen Host aus einer Variablen; `curl -K/--config` (Optionen aus Datei).
- **Inline-Code:** `python -c`, `node -e`, `perl -e`, `ruby -e`, `awk` und Heredocs für diese Interpreter, wenn der Code einen Ausführungsmarker (`subprocess`, `os.system`, `urllib`, `requests.`, `fetch(`, …) **und** ein `git … push`, `gh … merge`, `gh … api` oder `github.com` enthält (Heuristik).
- **Der Hook selbst:** Bash-Befehle, die `.claude/`, `src/hooks/` oder `config/bash_guard.yaml` schreiben (`sed -i`, `cp`, `mv`, `rm`, `tee`, `truncate`, `chmod`, `ln`, Umleitungen `>`, `>>`). Lesen und `git add` dieser Dateien bleibt erlaubt.

Erlaubt bleiben die Formen, die der Agent braucht: `git push origin agent/<thema>` (auch mit `-u`, `--dry-run`), gezieltes `git add <Dateien>`, `git commit -m …`, `gh pr create/view/list/diff`, `gh issue comment/view/list`, `gh api` mit GET, pytest. Text, der nur zufällig nach einem Befehl aussieht (`echo "git push origin master"`, Commit- und PR-Meldungen, `grep`, Heredocs für `cat`, `$(…)` in einfachen Anführungszeichen), wird nicht beanstandet.

## Was der Hook NICHT sieht (bekannte Grenzen)

1. **Skriptdateien und pytest.** `bash script.sh`, `python x.py`, `make` und jede Testdatei unter `tests/` (auch `conftest.py`) führen beliebigen Code aus, den der Hook nicht liest (Befund S1 der ersten Prüfung). Das schließt nur eine Sandbox oder ein Review der Dateien.
2. **Verschleierter Code.** Strings, die ein Interpreter zusammenbaut (`"gi"+"t"`), kodierte Befehle, `python3 -c` ohne Ausführungsmarker.
3. **Konfiguration von git in Dateien.** `remote.<name>.push`, `remote.<name>.mirror`, `push.default`, `help.autocorrect` in `.git/config` oder `~/.gitconfig` können ein `git push` ohne Ziel verändern; der Hook prüft nur den aktuellen Branch. (Setzen über `git -c`, `git config` und `GIT_CONFIG*` ist abgelehnt.) Pushes über andere Wege (geänderte Remote-URL per Datei, andere Programme) sind nicht erfasst.
4. **Der Hook selbst, über Wege außerhalb von Bash.** Das Edit-/Write-Werkzeug auf `.claude/settings.json`, `src/hooks/` und `config/` steht auf „Rückfrage“; im Modus `bypassPermissions` fragt niemand. Python- oder Skriptdateien, die diese Dateien schreiben, erfasst der Hook nicht. Schutz bieten nur Einstellungen auf Nutzerebene des Servers, verwaltete Einstellungen oder Branch-Schutz (Issue #60).
5. **Andere Werkzeuge als Bash.** MCP-Werkzeuge (`mcp__github__merge_pull_request` usw.) werden weiter durch die Deny-Regeln in `settings.json` gesperrt, nicht durch diesen Hook.
6. **Weitere `gh`-Befehle.** `gh pr edit --base`, `gh pr close`, `gh pr review`, `gh pr update-branch`, `gh workflow run`, `gh repo edit/delete/sync`, `gh release create` sind nicht abgelehnt (der Auftrag nennt `gh pr merge` und `gh api`).
7. **Falschmeldungen.** Der Hook liest einfache Anführungszeichen als Text (dort laufen weder `$(…)` noch Backticks); ein Skript in `bash -c '…'` oder `eval '…'` wird dagegen als Code analysiert. Im Zweifel lehnt der Hook ab: Ein Befehl, den er nicht sicher zerlegen kann, wird abgelehnt, auch wenn er harmlos wäre. Bekannte Fälle: ein `git`-Wort mit `push` im selben Inline-Code (`python -c`) über eine Zeilengrenze; ein Ordner `data` in jeder Tiefe bei `git add`; Dateinamen mit `[` bei `git add`.
8. **Reichweite.** Der Hook gilt nur dort, wo `.claude/settings.json` mit dem Eintrag geladen wird (Checkout mit der Datei). Neue Sitzungen auf `master` haben ihn erst nach dem Merge; für jeden Checkout bräuchte es zusätzlich `~/.claude/settings.json` des Nutzers `claude` (Issue #60).
9. **Version.** Dass Claude Code den Hook in `bypassPermissions`/`auto` ausführt, steht in der Dokumentation (Stand 2026-10-06); die installierte Version auf dem Server ist nicht geprüft.

## Ändern und erweitern

Neue Befehle, Optionen, Pfadmuster, zusätzliche geschützte Branches oder Zeitbudgets stehen in `config/bash_guard.yaml` (Klasse C). Zu jeder Änderung gehört ein Test in `tests/test_bash_guard.py`. Ein fehlender oder falscher Schlüssel in der YAML-Datei lässt den Hook **jeden** Bash-Aufruf ablehnen (fail closed); `tests/test_bash_guard.py` prüft, dass die echte Datei lädt.

## Prüfen

```
~/venvs/equity/bin/python -m pytest tests/test_bash_guard.py -q
echo '{"tool_name":"Bash","tool_input":{"command":"git status"},"cwd":"."}' | ~/venvs/equity/bin/python src/hooks/bash_guard.py; echo $?   # 0
```

Eine abgelehnte Beispielzeile gibt Exit 2 und eine Begründung auf stderr aus. Ob Claude Code den Hook in der echten Sitzung aufruft und blockiert (auch in `bypassPermissions`/`auto`), zeigt erst ein Lauf in einer beaufsichtigten Sitzung mit den ungefährlichen Dry-Run-Befehlen aus `docs/decisions.md` (Testprotokoll, Zeilen 1 bis 7); dieser Lauf steht aus.
