"""Kandidatenlisten lesen (src/config_layer/candidates.py) und Schluessel gezielt aus Dateien holen (env_file.py)."""

import pytest

from config_layer.candidates import CandidateListError, collect_candidate_tickers, read_candidate_tickers
from data_layer import env_file

TABLE = """# Kopf
Text mit | Strich
| Nr. | Rang | Ticker | Tranche |
|---|---|---|---|
| 1 | 1 | FHI | A |
| 2 | 2 | JBI | A |

**Gestrichen:** | 3 | 4 | XPER | A |
"""


def test_only_table_rows_with_a_number_in_the_first_column_count(tmp_path):
    f = tmp_path / "runde1.md"
    f.write_text(TABLE, encoding="utf-8")
    assert read_candidate_tickers(f) == ["FHI", "JBI"]


@pytest.mark.parametrize("row", ["| 1 | 1 | fhi | A |", "| 1 | 1 | | A |", "| 1 | 1 | F HI | A |", "| 1 | 1 | FHI | A |\n| 2 | 2 | FHI | A |"])
def test_unusable_or_duplicate_tickers_are_errors(tmp_path, row):
    f = tmp_path / "runde1.md"
    f.write_text("| Nr. | Rang | Ticker |\n" + row + "\n", encoding="utf-8")
    with pytest.raises(CandidateListError):
        read_candidate_tickers(f)


def test_a_file_without_rows_or_a_missing_file_is_an_error_not_an_empty_list(tmp_path):
    f = tmp_path / "runde1.md"
    f.write_text("nur Text\n", encoding="utf-8")
    with pytest.raises(CandidateListError, match="no candidate rows"):
        read_candidate_tickers(f)
    with pytest.raises(CandidateListError):
        read_candidate_tickers(tmp_path / "fehlt.md")


def test_several_rounds_are_merged_and_a_repeated_ticker_counts_once(tmp_path):
    folder = tmp_path / "docs" / "kandidaten"
    folder.mkdir(parents=True)
    (folder / "runde1.md").write_text(TABLE, encoding="utf-8")
    (folder / "runde2.md").write_text("| Nr. | Rang | Ticker |\n| 1 | 1 | JBI |\n| 2 | 2 | NEU |\n", encoding="utf-8")
    assert collect_candidate_tickers(tmp_path, "docs/kandidaten/runde*.md") == ["FHI", "JBI", "NEU"]
    with pytest.raises(CandidateListError, match="no candidate list"):
        collect_candidate_tickers(tmp_path, "docs/anders/*.md")


def test_key_is_read_from_one_file_only_for_its_own_name_and_other_keys_stay_out_of_the_environment(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_bytes(b"\xef\xbb\xbfSIMFIN_API_KEY=simfin_geheim\nexport ALPHAVANTAGE_API_KEY='av_geheim'\n")
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    monkeypatch.delenv("SIMFIN_API_KEY", raising=False)
    assert env_file.resolve_api_key([f]) == "av_geheim"
    import os
    assert "SIMFIN_API_KEY" not in os.environ


def test_the_environment_wins_then_the_first_file_with_the_key(tmp_path, monkeypatch):
    own, shared = tmp_path / ".env.alphavantage", tmp_path / ".env"
    own.write_text("ALPHAVANTAGE_API_KEY=eigene_datei\n", encoding="utf-8")
    shared.write_text("ALPHAVANTAGE_API_KEY=gemeinsame_datei\n", encoding="utf-8")
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    assert env_file.resolve_api_key([own, shared]) == "eigene_datei"
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "aus_umgebung")
    assert env_file.resolve_api_key([own, shared]) == "aus_umgebung"


def test_missing_key_names_the_variable_but_never_a_value(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("SIMFIN_API_KEY=simfin_geheim\n", encoding="utf-8")
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as err:
        env_file.resolve_api_key([f])
    assert "ALPHAVANTAGE_API_KEY" in str(err.value) and "simfin_geheim" not in str(err.value)


def test_the_default_key_files_are_the_own_file_first_and_both_are_ignored_by_git():
    from pathlib import Path
    assert [p.name for p in env_file.DEFAULT_KEY_FILES] == [".env.alphavantage", ".env"]
    ignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in ignore and ".env.*" in ignore


def test_a_numbered_row_with_too_few_cells_or_a_table_without_ticker_header_is_an_error(tmp_path):
    f = tmp_path / "runde1.md"
    f.write_text("| Nr. | Rang | Ticker |\n| 1 | 1 |\n", encoding="utf-8")
    with pytest.raises(CandidateListError, match="fewer than 3"):
        read_candidate_tickers(f)
    f.write_text("| Nr. | Rang | Tranche |\n| 1 | 1 | A |\n", encoding="utf-8")
    with pytest.raises(CandidateListError, match="header"):
        read_candidate_tickers(f)


def test_an_unreadable_key_file_gives_a_clean_error_without_a_value(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    folder = tmp_path / ".env"
    folder.mkdir()
    with pytest.raises(RuntimeError, match="nicht lesbar"):
        env_file.resolve_api_key([folder])
