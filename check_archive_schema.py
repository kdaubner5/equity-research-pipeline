"""Prueft das Schema einer KOPIE des Run-Archivs gegen das erwartete Schema (Roadmap A.2, Schema pruefen).

Nur lesen: Die Datei wird schreibgeschuetzt geoeffnet, nichts wird angelegt oder migriert. Das echte
data/run_archive.db wird verweigert (CLAUDE.md: nicht oeffnen, migrieren oder beschreiben ohne ausdrueckliche
Anweisung von Konstantin mit Umfang und Befristung); auch ein Link darauf wird verweigert.

Das erwartete Schema entsteht frisch in einer temporaeren Datei aus den vorhandenen Anlege-Funktionen
(init_db, init_stage2_tables, init_trend_tables, init_rule_state_tables), nicht aus einer eigenen Liste.

Aufruf (auf einer Kopie, die Konstantin zuvor mit Pruefsumme gesichert hat):
    python check_archive_schema.py <pfad/zur/kopie.db>

Exit-Code: 0 = Schema aktuell; 1 = eine Migration wuerde Tabellen oder Spalten anlegen (Liste wird ausgegeben);
2 = Abbruch (Datei fehlt, keine Datenbank, oder es ist das echte Archiv).
"""

import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from store_layer import db as archive_db  # noqa: E402
from store_layer import rule_state, stage2_store, trend_store  # noqa: E402

REAL_ARCHIVE_PATH = archive_db.DEFAULT_DB_PATH


@dataclass
class SchemaDiff:
    missing_tables: List[str] = field(default_factory=list)
    missing_columns: Dict[str, List[str]] = field(default_factory=dict)
    extra_tables: List[str] = field(default_factory=list)
    extra_columns: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def needs_migration(self) -> bool:
        return bool(self.missing_tables or self.missing_columns)


def _columns(connection) -> Dict[str, List[str]]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    schema = {}
    for table in tables:
        schema[table] = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
    return schema


def expected_schema() -> Dict[str, List[str]]:
    """Das Schema, das die Anlege-Funktionen in einer frischen Datei erzeugen."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "erwartet.db"
        archive_db.init_db(path)
        stage2_store.init_stage2_tables(path)
        trend_store.init_trend_tables(path)
        connection = sqlite3.connect(path)
        try:
            rule_state.init_rule_state_tables(connection)
            connection.commit()
            return _columns(connection)
        finally:
            connection.close()


def _open_read_only(path: Path):
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def read_schema(path) -> Dict[str, List[str]]:
    connection = _open_read_only(Path(path))
    try:
        return _columns(connection)
    finally:
        connection.close()


def read_row_counts(path) -> Dict[str, int]:
    connection = _open_read_only(Path(path))
    try:
        return {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in _columns(connection)}
    finally:
        connection.close()


def compare(expected: Dict[str, List[str]], actual: Dict[str, List[str]]) -> SchemaDiff:
    diff = SchemaDiff()
    for table, columns in expected.items():
        if table not in actual:
            diff.missing_tables.append(table)
            continue
        missing = [column for column in columns if column not in actual[table]]
        if missing:
            diff.missing_columns[table] = missing
        extra = [column for column in actual[table] if column not in columns]
        if extra:
            diff.extra_columns[table] = extra
    diff.extra_tables = [table for table in actual if table not in expected]
    return diff


def _is_real_archive(path: Path) -> bool:
    try:
        return path.resolve() == REAL_ARCHIVE_PATH.resolve()
    except OSError:
        return False


def main(argv=None) -> int:
    argv = list(argv or [])
    if len(argv) != 1:
        print("ABBRUCH: Aufruf: python check_archive_schema.py <pfad/zur/kopie.db>")
        return 2
    path = Path(argv[0])
    if _is_real_archive(path):
        print("ABBRUCH: Das ist das echte Run-Archiv; es wird nicht geöffnet. Bitte eine gesicherte Kopie angeben.")
        return 2
    if not path.is_file():
        print(f"ABBRUCH: Datei nicht gefunden: {path}")
        return 2
    try:
        actual = read_schema(path)
        counts = read_row_counts(path)
    except sqlite3.Error as exc:
        print(f"ABBRUCH: keine lesbare SQLite-Datenbank ({type(exc).__name__})")
        return 2

    diff = compare(expected_schema(), actual)
    print(f"Datei: {path}")
    for table in sorted(counts):
        print(f"  {table}: {counts[table]} Zeilen")
    if diff.missing_tables:
        print("Fehlende Tabellen (würde eine Migration anlegen): " + ", ".join(diff.missing_tables))
    for table, columns in diff.missing_columns.items():
        print(f"Fehlende Spalten in {table} (würde eine Migration anlegen): " + ", ".join(columns))
    if diff.extra_tables:
        print("Zusätzliche Tabellen (nicht im erwarteten Schema): " + ", ".join(diff.extra_tables))
    for table, columns in diff.extra_columns.items():
        print(f"Zusätzliche Spalten in {table}: " + ", ".join(columns))
    if diff.needs_migration:
        print("Ergebnis: Migration nötig (nicht ausgeführt).")
        return 1
    print("Ergebnis: Schema aktuell.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
