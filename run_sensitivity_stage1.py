"""Empfindlichkeitstest der Stufe-1-Schwellen auf einer KOPIE des Run-Archivs.

Zeigt, wie viele Firmen ihr Urteil (bestanden, bestanden mit Marken, durchgefallen, nicht beurteilbar) wechseln, wenn eine
Schwelle um einige Prozent strenger oder lockerer waere. Rechnet aus den gespeicherten Kennzahlen mit derselben Funktion
wie der Lauf; ruft nichts ab, schreibt nichts, benutzt kein SimFin. Die Datei wird schreibgeschuetzt und unveraenderlich
geoeffnet; das echte data/run_archive.db wird verweigert (CLAUDE.md), auch ueber einen Link.

Aufruf (auf einer gesicherten Kopie, nur die eine .db-Datei kopiert):
    python run_sensitivity_stage1.py <pfad/zur/kopie.db> [--run <Zeitstempel des Stufe-1-Laufs>] [--steps=-20,-10,10,20]

Exit-Code: 0 = fertig; 1 = fertig, aber die Neuberechnung der Ausgangslage stimmt bei mindestens einer Firma nicht mit dem
gespeicherten Urteil ueberein (dann sind die Ergebnisse mit Vorsicht zu lesen); 2 = Abbruch.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from config_layer.thresholds import load_thresholds  # noqa: E402
from gates_layer import sensitivity  # noqa: E402
from store_layer import db as archive_db  # noqa: E402

REAL_ARCHIVE_PATH = archive_db.DEFAULT_DB_PATH
DEFAULT_STEPS = "-20,-10,10,20"
MAX_LISTED = 12

_STATUS_DE = {
    "passed": "bestanden",
    "passed_with_flags": "bestanden mit Marken",
    "failed": "durchgefallen",
    "not_evaluable": "nicht beurteilbar",
}


def _is_real_archive(path: Path) -> bool:
    try:
        if path.resolve() == REAL_ARCHIVE_PATH.resolve():
            return True
        return REAL_ARCHIVE_PATH.exists() and path.exists() and os.path.samefile(path, REAL_ARCHIVE_PATH)
    except OSError:
        return True  # im Zweifel verweigern


def _open_read_only(path: Path):
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _parse_steps(text):
    try:
        steps = [float(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: Schritte als Zahlen mit Dezimalpunkt, durch Komma getrennt (zum Beispiel -20,-10,10,20)") from None
    if not steps or any(s <= -100 for s in steps):
        raise argparse.ArgumentTypeError("Schritte müssen größer als -100 sein und mindestens einer ist nötig")
    return steps


def _status(name):
    return _STATUS_DE.get(name, name)


def _fmt(step):
    return f"{step:+g} %"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Empfindlichkeitstest der Stufe-1-Schwellen (nur lesen, auf einer Kopie)")
    parser.add_argument("db", type=Path, help="gesicherte KOPIE des Run-Archivs (nicht das echte)")
    parser.add_argument("--run", default=None, help="Zeitstempel des Stufe-1-Laufs (Standard: der jüngste)")
    parser.add_argument("--steps", type=_parse_steps, default=_parse_steps(DEFAULT_STEPS), help="relative Änderung der Schwelle in Prozent, Komma-getrennt")
    args = parser.parse_args(argv)

    path = args.db
    if _is_real_archive(path):
        print("ABBRUCH: Das ist das echte Run-Archiv; es wird nicht geöffnet. Bitte eine gesicherte Kopie angeben.")
        return 2
    if not path.is_file():
        print(f"ABBRUCH: Datei nicht gefunden: {path}")
        return 2
    for suffix in ("-wal", "-journal"):
        if Path(str(path) + suffix).exists():
            print(f"ABBRUCH: Neben der Kopie liegt eine Datei mit der Endung {suffix}; die Kopie ist unvollständig. Bitte nur die eine .db-Datei kopieren.")
            return 2

    try:
        connection = _open_read_only(path)
        try:
            runs = sensitivity.available_run_timestamps(connection)
            if not runs:
                print("ABBRUCH: Im Archiv ist kein Stufe-1-Lauf (Tabelle runs ist leer).")
                return 2
            run = args.run or runs[-1]
            rows = sensitivity.load_stage1_rows(connection, run)
        finally:
            connection.close()
    except sensitivity.SensitivityError as exc:
        print(f"ABBRUCH: {exc}")
        return 2
    except sqlite3.Error as exc:
        print(f"ABBRUCH: Archiv nicht lesbar ({exc})")
        return 2

    thresholds = load_thresholds()
    result = sensitivity.run_sensitivity(rows, thresholds, args.steps)

    print(f"Datei: {path} (nur gelesen, nichts wurde verändert)")
    print(f"Stufe-1-Lauf: {run}")
    print(f"Firmen im Test: {result.companies} (beiseite gelegt: {result.set_aside} mit ausgeschlossenem Geschäftsmodell)")
    print("Ausgangslage (aktuelle Schwellen aus config/thresholds.yaml), neu gerechnet:")
    for name in sensitivity.STATUSES:
        print(f"  {_status(name)}: {result.baseline_counts[name]} von {result.companies}")
    exit_code = 0
    if result.baseline_mismatches:
        exit_code = 1
        print(
            f"ACHTUNG: Die Neuberechnung stimmt nicht mit dem gespeicherten Urteil überein bei {len(result.baseline_mismatches)} "
            f"von {result.companies} Firmen (wurde der Lauf mit anderen Schwellen gerechnet?):"
        )
        for ticker, stored, new in result.baseline_mismatches[:MAX_LISTED]:
            print(f"  {ticker}: gespeichert {_status(stored)}, neu {_status(new)}")
        if len(result.baseline_mismatches) > MAX_LISTED:
            print(f"  (und {len(result.baseline_mismatches) - MAX_LISTED} weitere)")

    print("\nWirkung je Variante (eine Schwelle wird um den Prozentsatz verändert, die anderen bleiben):")
    for cell in result.cells:
        label = f"{cell.parameter} {_fmt(cell.step_percent)}"
        if cell.skipped:
            print(f"  {label}: übersprungen ({cell.skipped})")
            continue
        names = sorted(cell.changed)
        listed = ", ".join(f"{t} ({_status(cell.changed[t][0])} → {_status(cell.changed[t][1])})" for t in names[:MAX_LISTED])
        more = f" (und {len(names) - MAX_LISTED} weitere)" if len(names) > MAX_LISTED else ""
        print(f"  {label}: {len(names)} von {result.companies} wechseln" + (f": {listed}{more}" if names else ""))

    ranking = {}
    for cell in result.cells:
        if cell.skipped:
            continue
        ranking[cell.parameter] = max(ranking.get(cell.parameter, 0), len(cell.changed))
    print("\nWelche Schwellen steuern die Auswahl am stärksten? (größte Zahl wechselnder Firmen innerhalb der Schritte)")
    for parameter, count in sorted(ranking.items(), key=lambda item: (-item[1], item[0])):
        print(f"  {parameter}: bis zu {count} von {result.companies}")

    print(
        "\nGrenzen: Verändert werden nur die Gate-Schwellen, nicht die Rechenparameter der Kennzahlen (Steuersatz, Obergrenze "
        "des ROIC). Für Überlebende ist die Nettomarge nicht gespeichert, strengere Marge-Varianten fehlen deshalb. Der Test "
        "zeigt, welche Zahlen die Auswahl steuern, nicht, welche richtig sind."
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
