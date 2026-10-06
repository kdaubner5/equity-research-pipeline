# Bash-Guard (Stufe 2, Issue #60)

Ein Hook, der **vor jedem Bash-Aufruf** läuft und Befehle ablehnt, die auf `master` oder `main` pushen, dorthin mergen, dort löschen oder sie lokal verändern würden. Er ist die technische Antwort auf die Befunde S2 bis S4 des Kritiker-Berichts zu Stufe 1 und auf die Befunde der Kritiker-Läufe zu PR #61.

**Er ist ein Hilfsmittel, keine Sicherheitsgrenze.** Der Hook prüft den Befehlstext, nicht das, was die Shell danach tut. Jede Textprüfung lässt sich umgehen (Skriptdateien, pytest, verschleierte Strings, Edit/Write auf den Hook selbst). Die wirksame Grenze gegen Push auf `master` ist Branch-Schutz auf GitHub oder ein zweites Konto (Issue #60).

Zum Berechtigungsmodus: Laut Dokumentation (Permissions-Seite, „Extend permissions with hooks“) laufen PreToolUse-Hooks vor den Berechtigungsregeln, und Exit-Code 2 blockiert den Aufruf auch dann, wenn eine Allow-Regel ihn erlauben würde. Für `auto` ist das ausdrücklich belegt. Für `bypassPermissions` steht in den gelesenen Seiten kein wörtlicher Satz, aber die Wirkung ist **gemessen**: In einer Sitzung im Modus `bypassPermissions` (Angabe aus Konstantins Auftrag; der Agent kann den Modus von innen nicht prüfen) lehnte der Hook sechs von sechs Dry-Run-Befehlen ab (Testprotokoll in `docs/decisions.md`, Nr. 1 bis 6), und ein reiner `echo`-Text lief durch. Im Modus `auto` und im Standardmodus ist der Hook nicht gemessen; die Messung gilt für die damals installierte Claude-Code-Version (nicht geprüft) und beweist nicht, dass jede Schreibweise abgelehnt wird.

## Bestandteile

| Datei | Aufgabe |
|---|---|
| `src/hooks/bash_guard.py` | der Hook (liest JSON von stdin, Exit 2 = abgelehnt, Exit 0 = weiter) |
| `config/bash_guard.yaml` | alle Muster, Optionen, Grenzen und Zeitbudgets (geschützte Branches, verbotene Optionen, Pfadmuster, `gh`-Regeln, Tiefen- und Zeitlimit); keine Muster im Code |
| `.claude/settings.json` | trägt den Hook ein (`hooks.PreToolUse`, Matcher `Bash`, `timeout` 30) |
| `tests/test_bash_guard.py` | pytest-Tests für jede bekannte Schreibweise, auch Zeile 2 und 3 des Testprotokolls; führt den eingetragenen Befehl wie Claude Code über `sh -c` aus |

Der Eintrag in `settings.json` hängt `|| exit 2` an. Stürzt der Interpreter ab oder fehlt das venv, blockiert der Aufruf, statt durchzulaufen (Exit-Codes außer 2 blockieren sonst nicht; ein Test führt das mit fehlendem und mit abstürzendem Interpreter aus). Im Skript selbst führt jeder Fehler (kaputte Konfiguration, ungültige Eingabe ohne `tool_name`, nicht parsebarer Befehl, Verschachtelung zu tief, Befehl zu lang, überschrittenes Zeitbudget) ebenfalls zu Exit 2. Folge: Ein Fehler im Hook sperrt **jeden** Bash-Aufruf; repariert werden kann dann nur mit dem Edit-/Write-Werkzeug.

**Zeitbudget:** Claude Code blockiert einen Aufruf **nicht**, wenn der Hook in sein eigenes Zeitlimit läuft (`timeout` in `settings.json`, 30 s). Der Hook gibt sich deshalb ein kleineres Budget (`limits.analysis_seconds`, 10 s; die Konfiguration muss es unter `limits.hook_timeout_seconds` halten, ein Test vergleicht diesen Wert mit dem Eintrag) und lehnt ab, wenn es überschritten ist. Git-Abfragen (aktueller Branch, Aliase) werden je Befehl nur einmal ausgeführt und begrenzt (`limits.git_lookup_seconds`). Das Budget wird an Kontrollpunkten geprüft; ein Beweis, dass es nicht zu durchbrechen ist, ist das nicht.

## Was abgelehnt wird

Der Hook zerlegt den Befehl mit einem Shell-Tokenizer (Anführungszeichen, `;`, `&&`, `||`, `|`, Klammern, Zeilenumbrüche, Kommentare, `$(…)`, Backticks, Heredocs, Umleitungen) und prüft jedes Segment. Er sucht `git`, `gh`, `curl`/`wget`/HTTPie, Shells und Interpreter an jeder Stelle des Segments (auch hinter `env`, `timeout`, `nice`, `nohup`, `time`, `sudo`, `exec`, `command`, `setsid`, `stdbuf`) und analysiert Befehlsstrings, die an Wrapper wie `env -S`, `su -c`, `ssh`, `watch`, `tmux`, `flock`, `script` übergeben werden.

- **`git push`:** Ziel `master`/`main` in jeder Schreibweise, die er auflösen kann (`refs/heads/…`, `HEAD:master`, `agent/x:master`, `ma''ster`, Groß-/Kleinschreibung); `+ref`, `:ref` (Löschen); `--force*`, `-f`, `-uf`, `--delete`, `-d`, `--mirror`, `--all`, `--tags`, `--follow-tags`, `--prune`, `--receive-pack`, `--exec` (auch als eindeutige Abkürzung, z. B. `--forc`); Ziele außerhalb von `refs/heads/`; Muster und Variablen im Refspec oder an der Stelle des Remotes (`'refs/heads/*:…'`, `$BRANCH`, `${B:-master}`, `$(git remote)`); ein Push ohne Ziel (`git push`, `git push origin`, `git push origin HEAD`), wenn der aktuelle Branch geschützt ist (der Hook fragt `git symbolic-ref` ab; schlägt das fehl, wird abgelehnt).
- **Wechsel von Branch oder Verzeichnis im selben Aufruf:** Nach `git checkout/switch <name>`, `symbolic-ref`, `worktree` oder `cd`/`pushd` in derselben Zeile gilt der Branch als unbekannt; ein späteres `git push`/`commit`/`merge` ohne Refspec wird abgelehnt (mit explizitem Refspec bleibt es erlaubt; `git checkout -b agent/x && git push -u origin HEAD` ist erlaubt). `git -C <Ordner>`, den es beim Aufruf noch nicht gibt, wird abgelehnt.
- **Umschreibungen:** `git -C …`, `git -c …`, `--git-dir`, `git-push` (Bindestrich), `/usr/bin/git`, `"git"`, `g''it`, `\git`, Umleitungen mitten im Befehl (`git push 2>&1 origin master`), `bash -c`, `sh -c --`, `bash -lc`, `eval`, verschachtelt (bis `max_depth`), Heredocs in Shells, `echo '…' | bash`, `bash <<<`, Shell ohne Skript oder mit `/dev/stdin` (liest stdin), `xargs`/`parallel`, `find -exec`, Befehlswörter aus Variablen, NUL-Bytes, Git-Aliase (`git config --get alias.<name>` wird aufgelöst), Kommentare mit Backslash am Zeilenende, `alias`, `trap`.
- **Git-Konfiguration und Umgebung:** `git -c`, `--config-env` und `git config` (Schreiben) nur für Schlüssel auf der Allow-Liste (`user.`, `color.`, `core.quotepath`, …); alles andere (Aliase, `remote.*`, `push.*`, Programme wie `core.editor`) ist abgelehnt; Umgebungsvariablen wie `GIT_CONFIG*`, `GIT_DIR`, `GIT_PAGER`, `GIT_EDITOR`, `GH_PAGER`, `PAGER`, `BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `LD_PRELOAD` vor einem Befehl oder mit `export`/`declare`/`env`; Optionen, die ein Programm starten (`git rebase -x`, `git difftool -x`, `git grep -O`, `--upload-pack`, `git archive --exec`), `git submodule foreach`, `git bisect run`, `git filter-branch`, `git remote add/set-url/remove`.
- **Lokale Änderungen an `master`/`main`:** auf einem geschützten Branch `git commit`, `merge`, `reset`, `rebase`, `cherry-pick`, `revert`, `am` und `git pull` ohne `--ff-only`; mit einem geschützten Branchnamen im Befehl `git branch -f/-d/-D/-m/-M/-c/-C`, `git switch -f/-C`, `git checkout -f/-B`, `git update-ref`, `git worktree`, `git fetch +…:master`; immer `git send-pack`. Erlaubt bleibt Fast-Forward (`git pull --ff-only`, `git fetch`).
- **`git add` / `git commit`:** `-A`, `--all`, `-u`, `-f`, `-i`, `-e`, `.`, `..`, `./`, `src/..`, absolute Pfade, die das Arbeitsverzeichnis enthalten, leere Pfade, Muster (`*`, `?`, `[`), Pathspec-Magie (`:!x`, `:(exclude)x`, `:/`), `--pathspec-from-file`, `.env*` und `data/` (Groß-/Kleinschreibung egal), nicht aufgelöste Variablen, `git add` hinter `xargs`; `git commit -a`/`--all` (auch abgekürzt) und die gleichen Pfadregeln für Pfade bei `git commit`. Echte Optionen wie `--allow-empty` bleiben erlaubt.
- **`gh`:** `gh pr merge` (auch mit `-R`/`--repo`), `gh alias set/import`, `gh extension install`, `gh config set`; **jede** schreibende `gh api`-Anfrage (`-X`, `--method`, `-XPUT`, implizit durch `-f/-F/--field/--raw-field/--input`, durch `X-HTTP-Method-Override`; `config/bash_guard.yaml`: `gh.api.refuse_all_writes`) und jeder `gh api`-Aufruf mit nicht aufgelöster Variablen, wenn die Methode nicht ausdrücklich GET ist. Lesende `gh api`-Aufrufe (GET) bleiben erlaubt.
- **Rohes HTTP:** `curl`/`wget`/HTTPie mit Schreibmarker (auch in Clustern wie `-sXPUT`, `-sd`, bei HTTPie `PUT`, `a=b`) gegen `github.com`/`api.github.com`/`githubusercontent.com`; jede solche Anfrage mit nicht aufgelöster Variablen (Host, URL, Option), wenn die Methode nicht ausdrücklich GET ist; `curl -K/--config` (Optionen aus Datei).
- **Inline-Code:** `python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`, `awk`, `sed` und Heredocs für diese Interpreter, wenn der Code einen Ausführungsmarker (`subprocess`, `os.system`, Backticks, `qx`, `passthru`, `getline`, Pipe in einen String, sed-`e`, `urllib`, `requests.`, `fetch(`, …) **und** ein `git … push`, `gh … merge`, `gh … api` oder `github.com` enthält (Heuristik).
- **Der Hook selbst und git-Konfiguration:** Bash-Befehle, die `.claude/`, `src/hooks/`, `config/bash_guard.yaml`, `.git/` (Konfiguration, Hooks), `~/.gitconfig` oder `~/.config/git` schreiben (`sed -i`, `cp`, `mv`, `rm`, `tee`, `truncate`, `chmod`, `ln`, Umleitungen `>`, `>>`). Lesen und `git add` dieser Dateien bleibt erlaubt.

Erlaubt bleiben die Formen, die der Agent braucht: `git push origin agent/<thema>` (auch mit `-u`, `--dry-run`), gezieltes `git add <Dateien>`, `git commit -m …`, `gh pr create/view/list/diff`, `gh issue comment/view/list`, `gh api` mit GET, pytest. Text, der nur zufällig nach einem Befehl aussieht (`echo "git push origin master"`, Commit- und PR-Meldungen, `grep`, Heredocs für `cat`, `$(…)` in einfachen Anführungszeichen), wird nicht beanstandet.

## Bekannte Falschmeldungen (der Hook lehnt im Zweifel ab)

- `git checkout <name> && git push` (ohne Refspec) und `cd … && git commit`: Branch unbekannt; mit `git push origin <branch>` bleibt es erlaubt.
- Ein Kommentar, der einen Befehl enthält (`git push origin agent/x # not master`): Der Hook liest den Kommentartext mit.
- `gh api …/pulls/$N` und `curl https://…/$VAR`: nicht aufgelöste Variable bei unbekannter Methode.
- `git add` mit einem Ordner `data` in jeder Tiefe oder Dateinamen mit `[`.
- Befehle über 50000 Zeichen, mehr als 8 Ebenen `bash -c`.
- `python -c` mit Ausführungsmarker und einem `git`-Wort samt `push` über eine Zeilengrenze.

## Was der Hook NICHT sieht (bekannte Grenzen)

1. **Skriptdateien und pytest.** `bash script.sh`, `python x.py`, `make` und jede Testdatei unter `tests/` (auch `conftest.py`) führen beliebigen Code aus, den der Hook nicht liest. Das schließt nur eine Sandbox oder ein Review der Dateien.
2. **Verschleierter Code.** Strings, die ein Interpreter zusammenbaut (`"gi"+"t"`), kodierte Befehle, Inline-Code ohne die genannten Marker, Positionsparameter und Funktionen, deren Inhalt der Hook nicht auflöst (er lehnt sie in den bekannten Formen ab, nicht in allen).
3. **Konfiguration von git in Dateien, die schon vorhanden sind.** `remote.<name>.push`, `remote.<name>.mirror`, `push.default`, `help.autocorrect` in `.git/config` oder `~/.gitconfig` können ein `git push` ohne Ziel verändern; der Hook prüft nur den aktuellen Branch. (Neu schreiben per Bash, `git -c`, `git config` und `GIT_CONFIG*` ist abgelehnt.)
4. **Edit-/Write-Werkzeug auf den Hook.** `.claude/settings.json`, `src/hooks/` und `config/` stehen auf „Rückfrage“; im Modus `bypassPermissions` fragt niemand, und der Matcher des Hooks ist nur `Bash`. Ein Agent kann den Hook so abschalten. Schutz bieten nur Einstellungen auf Nutzerebene des Servers, verwaltete Einstellungen oder Branch-Schutz (Issue #60); ein Matcher für Edit/Write wäre eine Klasse-C-Entscheidung.
5. **Andere Werkzeuge als Bash.** MCP-Werkzeuge (`mcp__github__merge_pull_request` usw.) werden durch die Deny-Regeln in `settings.json` gesperrt, nicht durch diesen Hook.
6. **Weitere `gh`-Befehle** (`gh pr edit --base`, `gh pr close`, `gh pr review`, `gh workflow run`, `gh repo edit/delete`, `gh release`, `gh secret`) und **Löschen** (`git rm -r`, `git clean -fdx`, `git reset --hard` auf einem Feature-Branch, `rm -rf`) sind nicht abgelehnt.
7. **Reichweite.** Der Hook gilt nur dort, wo `.claude/settings.json` mit dem Eintrag geladen wird, und nur, wenn die Sitzung im Projektordner startet (Sitzung in `src/` lädt ihn nicht). Neue Sitzungen auf `master` haben ihn erst nach dem Merge; für jeden Checkout bräuchte es zusätzlich `~/.claude/settings.json` des Nutzers `claude` (Issue #60). `disableAllHooks` oder verwaltete Einstellungen schalten Projekt-Hooks ab.
8. **Version.** Die installierte Claude-Code-Version auf dem Server ist nicht geprüft.

## Ändern und erweitern

Neue Befehle, Optionen, Pfadmuster, zusätzliche geschützte Branches oder Zeitbudgets stehen in `config/bash_guard.yaml` (Klasse C). Zu jeder Änderung gehört ein Test in `tests/test_bash_guard.py`. Ein fehlender oder falscher Schlüssel in der YAML-Datei lässt den Hook **jeden** Bash-Aufruf ablehnen (fail closed); `tests/test_bash_guard.py` prüft, dass die echte Datei lädt.

## Prüfen

```
~/venvs/equity/bin/python -m pytest tests/test_bash_guard.py -q
echo '{"tool_name":"Bash","tool_input":{"command":"git status"},"cwd":"."}' | ~/venvs/equity/bin/python src/hooks/bash_guard.py; echo $?   # 0
```

Eine abgelehnte Beispielzeile gibt Exit 2 und eine Begründung auf stderr aus. In einer beaufsichtigten Sitzung im Modus `bypassPermissions` hat Claude Code den Hook aufgerufen und die Dry-Run-Befehle 1 bis 6 des Testprotokolls (`docs/decisions.md`) blockiert; Nr. 7 (`gh api -X PUT …`) und der Standardmodus sowie `auto` stehen aus.
