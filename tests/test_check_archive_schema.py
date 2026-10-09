"""check_archive_schema.py: Schema einer KOPIE des Run-Archivs gegen das erwartete Schema pruefen (Roadmap A.2).
Nur lesen; das echte data/run_archive.db wird verweigert."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

import check_archive_schema as cas
from store_layer import db as archive_db
from store_layer import rule_state, stage2_store, trend_store


def build_current_archive(path):
    archive_db.init_db(path)
    stage2_store.init_stage2_tables(path)
    trend_store.init_trend_tables(path)
    connection = sqlite3.connect(path)
    try:
        rule_state.init_rule_state_tables(connection)
        connection.commit()
    finally:
        connection.close()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_expected_schema_contains_all_archive_tables():
    schema = cas.expected_schema()
    for table in ("run_results", "eliminated_results", "runs", "universe_prefilter", "stage2_runs", "stage2_results",
                  "trend_runs", "trend_results", "run_rule_state", "run_rule_files", "rule_file_contents"):
        assert table in schema, table
    assert "close_price_usd" in schema["universe_prefilter"]
    assert "prefilter_run_timestamp" in schema["runs"]


def test_a_current_archive_has_no_differences(tmp_path):
    copy = tmp_path / "kopie.db"
    build_current_archive(copy)
    diff = cas.compare(cas.expected_schema(), cas.read_schema(copy))
    assert diff.missing_tables == [] and diff.missing_columns == {} and diff.extra_tables == []
    assert not diff.needs_migration


def test_an_old_archive_without_new_columns_and_tables_is_reported(tmp_path):
    copy = tmp_path / "alt.db"
    connection = sqlite3.connect(copy)
    connection.execute("CREATE TABLE runs (run_timestamp TEXT PRIMARY KEY, config_hash TEXT)")
    connection.execute("CREATE TABLE universe_prefilter (run_timestamp TEXT, ticker TEXT)")
    connection.commit()
    connection.close()
    diff = cas.compare(cas.expected_schema(), cas.read_schema(copy))
    assert "stage2_runs" in diff.missing_tables and "run_rule_state" in diff.missing_tables
    assert "prefilter_run_timestamp" in diff.missing_columns["runs"]
    assert "close_price_usd" in diff.missing_columns["universe_prefilter"]
    assert diff.needs_migration


def test_row_counts_are_reported_and_the_file_is_not_changed(tmp_path, capsys):
    copy = tmp_path / "kopie.db"
    build_current_archive(copy)
    before = sha(copy)
    code = cas.main([str(copy)])
    out = capsys.readouterr().out
    assert code == 0 and "run_results" in out and sha(copy) == before


def test_exit_1_and_listing_when_a_migration_would_be_needed(tmp_path, capsys):
    copy = tmp_path / "alt.db"
    connection = sqlite3.connect(copy)
    connection.execute("CREATE TABLE runs (run_timestamp TEXT PRIMARY KEY, config_hash TEXT)")
    connection.commit()
    connection.close()
    before = sha(copy)
    code = cas.main([str(copy)])
    out = capsys.readouterr().out
    assert code == 1 and "stage2_runs" in out and "prefilter_run_timestamp" in out
    assert sha(copy) == before  # nichts wurde angelegt


def test_the_real_archive_path_is_refused_without_opening_it(tmp_path, monkeypatch, capsys):
    real = tmp_path / "data" / "run_archive.db"
    real.parent.mkdir()
    real.write_bytes(b"niemals oeffnen")
    monkeypatch.setattr(cas, "REAL_ARCHIVE_PATH", real)
    before = sha(real)
    code = cas.main([str(real)])
    assert code == 2 and "echte" in capsys.readouterr().out.lower()
    assert sha(real) == before


def test_a_link_to_the_real_archive_is_refused_too(tmp_path, monkeypatch):
    real = tmp_path / "run_archive.db"
    real.write_bytes(b"x")
    link = tmp_path / "kopie.db"
    link.symlink_to(real)
    monkeypatch.setattr(cas, "REAL_ARCHIVE_PATH", real)
    assert cas.main([str(link)]) == 2


def test_missing_file_and_non_database_exit_2(tmp_path, capsys):
    assert cas.main([str(tmp_path / "gibt_es_nicht.db")]) == 2
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"das ist keine datenbank" * 20)
    assert cas.main([str(junk)]) == 2
    assert cas.main([]) == 2


def test_it_never_creates_a_file(tmp_path):
    target = tmp_path / "neu.db"
    cas.main([str(target)])
    assert not target.exists()


def test_a_hardlink_to_the_real_archive_is_refused(tmp_path, monkeypatch):
    import os

    real = tmp_path / "run_archive.db"
    build_current_archive(real)
    hard = tmp_path / "andere.db"
    os.link(real, hard)
    monkeypatch.setattr(cas, "REAL_ARCHIVE_PATH", real)
    assert cas.main([str(hard)]) == 2


def test_a_real_copy_with_another_name_is_allowed(tmp_path, monkeypatch):
    real = tmp_path / "run_archive.db"
    build_current_archive(real)
    copy = tmp_path / "kopie.db"
    copy.write_bytes(real.read_bytes())
    monkeypatch.setattr(cas, "REAL_ARCHIVE_PATH", real)
    assert cas.main([str(copy)]) == 0


def test_relative_path_to_the_real_archive_is_refused(tmp_path, monkeypatch):
    real = tmp_path / "data" / "run_archive.db"
    real.parent.mkdir()
    build_current_archive(real)
    (tmp_path / "x").mkdir()
    monkeypatch.setattr(cas, "REAL_ARCHIVE_PATH", real)
    monkeypatch.chdir(tmp_path / "x")
    assert cas.main(["../data/run_archive.db"]) == 2


def test_missing_columns_in_stage2_and_trend_tables_are_reported_as_manual_migration(tmp_path, capsys):
    copy = tmp_path / "alt.db"
    build_current_archive(copy)
    connection = sqlite3.connect(copy)
    connection.execute("DROP TABLE stage2_results")
    connection.execute("CREATE TABLE stage2_results (run_timestamp TEXT, ticker TEXT)")
    connection.commit()
    connection.close()
    diff = cas.compare(cas.expected_schema(), cas.read_schema(copy))
    assert "stage2_results" in diff.manual_missing_columns and not diff.auto_missing_columns
    assert cas.main([str(copy)]) == 1
    assert "NICHT automatisch" in capsys.readouterr().out


def test_init_db_really_upgrades_exactly_the_columns_listed_as_automatic(tmp_path):
    copy = tmp_path / "alt.db"
    build_current_archive(copy)
    connection = sqlite3.connect(copy)
    for table, columns in cas.AUTO_UPGRADED_COLUMNS.items():
        for column in columns:
            connection.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
    connection.commit()
    connection.close()
    before = cas.compare(cas.expected_schema(), cas.read_schema(copy))
    assert before.auto_missing_columns and not before.manual_missing_columns
    archive_db.init_db(copy)  # nur auf dieser Wegwerf-Datei
    after = cas.compare(cas.expected_schema(), cas.read_schema(copy))
    assert not after.needs_migration


def test_a_missing_column_that_init_db_does_not_upgrade_is_reported_as_manual_even_in_an_upgraded_table(tmp_path):
    copy = tmp_path / "alt.db"
    build_current_archive(copy)
    connection = sqlite3.connect(copy)
    connection.execute('ALTER TABLE "universe_prefilter" DROP COLUMN "reason"')
    connection.commit()
    connection.close()
    diff = cas.compare(cas.expected_schema(), cas.read_schema(copy))
    assert diff.manual_missing_columns == {"universe_prefilter": ["reason"]}
    archive_db.init_db(copy)
    assert "reason" in cas.compare(cas.expected_schema(), cas.read_schema(copy)).missing_columns["universe_prefilter"]


def test_wal_or_journal_next_to_the_copy_aborts_and_reading_creates_no_side_files(tmp_path, capsys):
    copy = tmp_path / "kopie.db"
    build_current_archive(copy)
    connection = sqlite3.connect(copy)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.commit()
    connection.close()
    for extra in ("kopie.db-wal", "kopie.db-shm"):
        (tmp_path / extra).unlink(missing_ok=True)
    cas.main([str(copy)])
    assert not (tmp_path / "kopie.db-wal").exists() and not (tmp_path / "kopie.db-shm").exists()
    (tmp_path / "kopie.db-journal").write_bytes(b"x")
    assert cas.main([str(copy)]) == 2 and "unvollständig" in capsys.readouterr().out


def test_error_messages_tell_not_a_database_from_other_problems(tmp_path, capsys):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"das ist keine datenbank" * 20)
    assert cas.main([str(junk)]) == 2
    assert "keine SQLite-Datenbank" in capsys.readouterr().out


def test_empty_file_is_reported_as_all_tables_missing_and_path_with_spaces_works(tmp_path, capsys):
    folder = tmp_path / "mein ordner #1"
    folder.mkdir()
    empty = folder / "leer.db"
    empty.write_bytes(b"")
    assert cas.main([str(empty)]) == 1
    out = capsys.readouterr().out
    assert "0 von" in out and "FEHLT" in out and "nichts wurde verändert" in out


def test_extra_tables_and_columns_are_printed(tmp_path, capsys):
    copy = tmp_path / "kopie.db"
    build_current_archive(copy)
    connection = sqlite3.connect(copy)
    connection.execute("CREATE TABLE notizen (text TEXT)")
    connection.execute("ALTER TABLE runs ADD COLUMN extra_spalte TEXT")
    connection.commit()
    connection.close()
    assert cas.main([str(copy)]) == 0
    out = capsys.readouterr().out
    assert "notizen" in out and "extra_spalte" in out


def test_summary_states_found_of_expected_tables(tmp_path, capsys):
    copy = tmp_path / "kopie.db"
    build_current_archive(copy)
    cas.main([str(copy)])
    expected = len(cas.expected_schema())
    assert f"{expected} von {expected} erwarteten Tabellen" in capsys.readouterr().out
