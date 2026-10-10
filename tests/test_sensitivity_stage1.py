"""Empfindlichkeitstest der Stufe-1-Schwellen auf einer KOPIE des Archivs (nur lesen): Wie aendert sich das Urteil je
Firma, wenn eine Schwelle strenger oder lockerer waere? Rechnet aus den gespeicherten Kennzahlen mit derselben
Gate-Funktion wie der Lauf (evaluate_stage1_gates)."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

import run_sensitivity_stage1 as cli
from config_layer.thresholds import load_thresholds
from gates_layer import sensitivity as sens
from store_layer import db as archive_db

TS = "2026-10-07T10:00:00"


def thresholds():
    return load_thresholds()


def make_archive(path, survivors, eliminated, run=TS):
    archive_db.init_db(path)
    connection = sqlite3.connect(path)
    for ticker, roic, nde, cc, status, flags in survivors:
        connection.execute(
            "INSERT INTO run_results (run_timestamp, ticker, roic_3yr, net_debt_ebitda, cash_conversion_3yr, status, flags, "
            "rank_position, data_quality_flags, profile, borderline_sector) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run, ticker, roic, nde, cc, status, flags, 1, "", "non_financial", 0),
        )
    for ticker, status, roic, nde, cc, margin, reasons in eliminated:
        connection.execute(
            "INSERT INTO eliminated_results (run_timestamp, ticker, status, roic, net_debt_ebitda, cash_conversion, "
            "net_income_margin, invalid_reasons, data_quality_flags, profile, borderline_sector) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run, ticker, status, roic, nde, cc, margin, reasons, "", "non_financial", 0),
        )
    connection.execute("INSERT INTO runs (run_timestamp, companies_evaluated, survivors_count, config_hash) VALUES (?,?,?,?)",
                       (run, len(survivors) + len(eliminated), len(survivors), "x"))
    connection.commit()
    connection.close()


SURVIVORS = [
    ("CLEAN", 0.20, 1.0, 1.2, "passed", ""),
    ("GREYROIC", 0.11, 1.0, 1.2, "passed_with_flags", "roic"),
    ("EDGE", 0.125, 2.4, 1.0, "passed_with_flags", "net_debt_ebitda"),
]
ELIMINATED = [
    ("LOWROIC", "failed", 0.095, 1.0, 1.2, 0.05, ""),
    ("HIGHDEBT", "failed", 0.20, 2.6, 1.2, 0.05, ""),
    ("NOCASH", "failed", 0.20, 1.0, 0.7, 0.05, ""),
    ("THINMARGIN", "failed", 0.20, 1.0, 1.2, 0.01, ""),
    ("NODATA", "not_evaluable", None, None, None, None, "net_debt_unreliable"),
    ("BANK", "unsupported_business_model", None, None, None, None, ""),
]


@pytest.fixture
def rows(tmp_path):
    path = tmp_path / "kopie.db"
    make_archive(path, SURVIVORS, ELIMINATED)
    connection = sqlite3.connect(path)
    yield sens.load_stage1_rows(connection, TS)
    connection.close()


def test_rows_are_loaded_from_both_tables_and_unsupported_ones_are_set_aside(rows):
    by = {r.ticker: r for r in rows}
    assert set(by) == {"CLEAN", "GREYROIC", "EDGE", "LOWROIC", "HIGHDEBT", "NOCASH", "THINMARGIN", "NODATA", "BANK"}
    result = sens.run_sensitivity(rows, thresholds(), steps=(10,))
    assert result.companies == 8 and result.set_aside == 1
    assert by["CLEAN"].stored_status == "passed" and by["LOWROIC"].stored_status == "failed"


def test_baseline_reproduces_the_stored_status_for_every_company(rows):
    result = sens.run_sensitivity(rows, thresholds(), steps=(-10, 10))
    assert result.baseline_mismatches == []
    assert result.baseline_counts == {"passed": 1, "passed_with_flags": 2, "failed": 4, "not_evaluable": 1}


def test_a_mismatch_between_stored_and_recomputed_status_is_reported_not_hidden(tmp_path):
    path = tmp_path / "kopie.db"
    make_archive(path, [("LIAR", 0.20, 1.0, 1.2, "failed", "")], [])  # gespeichert failed, Werte waeren passed
    connection = sqlite3.connect(path)
    result = sens.run_sensitivity(sens.load_stage1_rows(connection, TS), thresholds(), steps=(10,))
    connection.close()
    assert [m[0] for m in result.baseline_mismatches] == ["LIAR"]


def test_a_stricter_roic_floor_moves_the_grey_roic_company_to_failed(rows):
    result = sens.run_sensitivity(rows, thresholds(), steps=(10, 15))
    assert result.cell("roic.fail_below", 10).changed == {}  # 0.10 -> 0.11; GREYROIC liegt bei 0.11 (nicht strikt darunter)
    cell = result.cell("roic.fail_below", 15)  # 0.115: GREYROIC (0.11) faellt durch
    assert cell.changed == {"GREYROIC": ("passed_with_flags", "failed")}


def test_looser_roic_floor_rescues_a_failed_company(rows):
    cell = sens.run_sensitivity(rows, thresholds(), steps=(-10,)).cell("roic.fail_below", -10)  # 0.10 -> 0.09; LOWROIC 0.095 nicht mehr unter
    assert cell.changed["LOWROIC"][1] in ("passed_with_flags", "passed")


def test_debt_threshold_variants_change_edge_cases(rows):
    result = sens.run_sensitivity(rows, thresholds(), steps=(-10,))
    cell = result.cell("net_debt_ebitda.beide", -10)  # beide Schwellen 2.3/2.5 -> 2.07/2.25: EDGE (2.4) faellt durch
    assert cell.changed["EDGE"] == ("passed_with_flags", "failed")
    assert result.cell("net_debt_ebitda.fail_at_or_above", -10).skipped  # 2.25 laege unter der sauberen Grenze 2.3
    cell = sens.run_sensitivity(rows, thresholds(), steps=(10,)).cell("net_debt_ebitda.fail_at_or_above", 10)  # 2.5 -> 2.75
    assert cell.changed["HIGHDEBT"][1] != "failed"


def test_cash_conversion_and_margin_variants(rows):
    cell = sens.run_sensitivity(rows, thresholds(), steps=(-20,)).cell("cash_conversion.fail_below", -20)  # 0.8 -> 0.64
    assert cell.changed["NOCASH"][1] != "failed"
    cell = sens.run_sensitivity(rows, thresholds(), steps=(-50,)).cell("net_income_margin.fail_below", -50)  # 0.02 -> 0.01 (nicht strikt darunter)
    assert cell.changed["THINMARGIN"][1] != "failed"


def test_stricter_margin_variants_are_skipped_because_survivors_have_no_stored_margin(rows):
    result = sens.run_sensitivity(rows, thresholds(), steps=(20,))
    cell = result.cell("net_income_margin.fail_below", 20)
    assert cell.skipped and "Überlebende" in cell.skipped


def test_invalid_combinations_are_skipped_with_a_reason(rows):
    result = sens.run_sensitivity(rows, thresholds(), steps=(-50,))
    cell = result.cell("roic.clean_pass_above", -50)  # 0.12 -> 0.06 liegt unter fail_below 0.10: ungueltig
    assert cell.skipped and "roic" in cell.skipped


def test_not_evaluable_company_without_values_never_changes(rows):
    result = sens.run_sensitivity(rows, thresholds(), steps=(-20, -10, 10, 20))
    for cell in result.cells:
        assert "NODATA" not in cell.changed


def test_loading_a_missing_run_names_the_available_ones(tmp_path):
    path = tmp_path / "kopie.db"
    make_archive(path, SURVIVORS, ELIMINATED)
    connection = sqlite3.connect(path)
    with pytest.raises(sens.SensitivityError, match=TS):
        sens.load_stage1_rows(connection, "1999-01-01T00:00:00")
    connection.close()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_cli_prints_a_table_and_does_not_change_the_copy(tmp_path, capsys):
    path = tmp_path / "kopie.db"
    make_archive(path, SURVIVORS, ELIMINATED)
    before = sha(path)
    code = cli.main([str(path), "--steps=-20,20"])
    out = capsys.readouterr().out
    assert code == 0 and "roic.fail_below" in out and "GREYROIC" in out and "nichts wurde verändert" in out
    assert sha(path) == before


def test_cli_refuses_the_real_archive_a_missing_file_and_a_database_without_stage1_data(tmp_path, monkeypatch, capsys):
    real = tmp_path / "run_archive.db"
    make_archive(real, SURVIVORS, ELIMINATED)
    monkeypatch.setattr(cli, "REAL_ARCHIVE_PATH", real)
    assert cli.main([str(real)]) == 2 and "echte" in capsys.readouterr().out.lower()
    assert cli.main([str(tmp_path / "gibt_es_nicht.db")]) == 2
    empty = tmp_path / "leer.db"
    archive_db.init_db(empty)
    assert cli.main([str(empty)]) == 2


def test_cli_reports_a_baseline_mismatch_with_exit_1(tmp_path, capsys):
    path = tmp_path / "kopie.db"
    make_archive(path, [("LIAR", 0.20, 1.0, 1.2, "failed", "")], [])
    assert cli.main([str(path)]) == 1
    assert "stimmt nicht" in capsys.readouterr().out


def test_selection_changes_are_separated_from_rank_only_changes(rows):
    result = sens.run_sensitivity(rows, thresholds(), steps=(-10,))
    cell = result.cell("roic.clean_pass_above", -10)  # 0.12 -> 0.108: GREYROIC (0.11) wird sauber: nur Rang
    assert set(cell.rank_only) == {"GREYROIC"} and cell.selection_changes == 0
    cell = result.cell("roic.fail_below", -10)  # LOWROIC kommt zu den Ueberlebenden
    assert set(cell.selection_in) == {"LOWROIC"} and not cell.selection_out and not cell.rank_only
    cell = sens.run_sensitivity(rows, thresholds(), steps=(-10,)).cell("net_debt_ebitda.beide", -10)
    assert set(cell.selection_out) == {"EDGE"}


def test_duplicate_tickers_in_both_tables_stop_the_test(tmp_path):
    path = tmp_path / "kopie.db"
    make_archive(path, [("DUP", 0.2, 1.0, 1.2, "passed", "")], [("DUP", "failed", 0.05, 1.0, 1.2, 0.05, "")])
    connection = sqlite3.connect(path)
    with pytest.raises(sens.SensitivityError, match="DUP"):
        sens.load_stage1_rows(connection, TS)
    connection.close()


def test_cli_output_separates_selection_and_rank_and_warns_about_comparing_steps(tmp_path, capsys):
    path = tmp_path / "kopie.db"
    make_archive(path, SURVIVORS, ELIMINATED)
    cli.main([str(path), "--steps=-10"])
    out = capsys.readouterr().out
    assert "Auswahl +1 / -0" in out and "nur Rang 1" in out
    assert "gleichen Prozentsatz" in out or "denselben Prozentsatz" in out
    assert "Beide Schwellen einer Kennzahl gemeinsam" in out and "Stufe 2" in out


def test_cli_wrong_run_and_bad_steps_exit_2(tmp_path, capsys):
    path = tmp_path / "kopie.db"
    make_archive(path, SURVIVORS, ELIMINATED)
    assert cli.main([str(path), "--run", "1999-01-01T00:00:00"]) == 2
    for bad in ("--steps=0,abc", "--steps=-100", "--steps="):
        with pytest.raises(SystemExit) as info:
            cli.main([str(path), bad])
        assert info.value.code == 2


def test_cli_refuses_links_and_side_files(tmp_path, monkeypatch, capsys):
    import os

    real = tmp_path / "run_archive.db"
    make_archive(real, SURVIVORS, ELIMINATED)
    monkeypatch.setattr(cli, "REAL_ARCHIVE_PATH", real)
    link = tmp_path / "symlink.db"
    link.symlink_to(real)
    hard = tmp_path / "hardlink.db"
    os.link(real, hard)
    assert cli.main([str(link)]) == 2 and cli.main([str(hard)]) == 2
    copy = tmp_path / "kopie.db"
    copy.write_bytes(real.read_bytes())
    (tmp_path / "kopie.db-wal").write_bytes(b"x")
    assert cli.main([str(copy)]) == 2 and "unvollständig" in capsys.readouterr().out


def test_cli_with_a_run_without_companies_does_not_divide_by_zero(tmp_path, capsys):
    path = tmp_path / "kopie.db"
    make_archive(path, [], [])
    assert cli.main([str(path), "--steps=10"]) == 0
    out = capsys.readouterr().out
    assert "Firmen im Test: 0" in out and "0 von 0" in out


def test_the_latest_of_several_runs_is_the_default(tmp_path, capsys):
    path = tmp_path / "kopie.db"
    make_archive(path, SURVIVORS, ELIMINATED, run="2026-09-24T00:00:00")
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO runs (run_timestamp, companies_evaluated, survivors_count, config_hash) VALUES (?,?,?,?)",
                       ("2026-10-07T00:00:00", 0, 0, "x"))
    connection.commit()
    connection.close()
    cli.main([str(path), "--steps=10"])
    assert "Stufe-1-Lauf: 2026-10-07T00:00:00" in capsys.readouterr().out
