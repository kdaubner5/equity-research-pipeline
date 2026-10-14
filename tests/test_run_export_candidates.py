"""run_export_candidates.py: Eingangswerte der Kandidatenliste aus einer KOPIE des Archivs lesen (nur lesen)."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

import run_export_candidates as rec
from store_layer import db as archive_db
from store_layer import rule_state, stage2_store, trend_store

S1, PRE, S2, TR = "2026-10-10T12:27:35", "2026-10-10T12:27:30", "2026-10-10T12:27:44", "2026-10-10T12:27:50"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(path, with_price=True, with_trend=True):
    archive_db.init_db(path)
    stage2_store.init_stage2_tables(path)
    trend_store.init_trend_tables(path)
    c = sqlite3.connect(path)
    try:
        rule_state.init_rule_state_tables(c)
        c.execute(
            "INSERT INTO universe_prefilter (run_timestamp, simfin_id, ticker, sector_code, market_cap_usd, price_date, status, reason, config_hash, close_price_usd, shares_outstanding) "
            "VALUES (?, 11, 'AAA', 100, 1.0e9, '2026-10-09', 'passed', NULL, 'h', ?, ?)",
            (PRE, 25.5 if with_price else None, 40.0e6 if with_price else None),
        )
        c.execute(
            "INSERT INTO run_results (run_timestamp, ticker, roic_3yr, net_debt_ebitda, cash_conversion_3yr, status, flags, rank_position, data_quality_flags, profile, borderline_sector) "
            "VALUES (?, 'AAA', 0.15, 1.2, 1.1, 'passed', '', 1, '', 'standard', 0)",
            (S1,),
        )
        c.execute(
            "INSERT INTO stage2_runs VALUES (?, ?, ?, '2026-10-10', 'h', 2, 1, 2)", (S2, S1, PRE)
        )
        c.execute(
            "INSERT INTO stage2_results (run_timestamp, ticker, simfin_id, status, flags, invalid_reasons, info_flags, rank_position, ev_ebitda, fcf_yield, peer_n) "
            "VALUES (?, 'AAA', 11, 'passed', '', '', '', 3, 6.5, 0.08, 12)",
            (S2,),
        )
        if with_trend:
            c.execute("INSERT INTO trend_runs VALUES (?, ?, ?, '2026-10-10', 'h', 1, 1)", (TR, S1, PRE))
            c.execute(
                "INSERT INTO trend_results (run_timestamp, ticker, simfin_id, status, marks, mark_count, revenue_ratio, ebitda_margin_ratio, roic_last_year) "
                "VALUES (?, 'AAA', 11, 'marked', 'T1', 1, 0.85, 0.95, 0.12)",
                (TR,),
            )
        c.commit()
    finally:
        c.close()


def write_list(path):
    path.write_text(
        "| Nr. | Rang | Ticker | Tranche | Status |\n|---|---|---|---|---|\n"
        "| 1 | 3 | AAA | A | passed |\n| 2 | 4 | BBB | B | passed |\n",
        encoding="utf-8",
    )


def test_read_candidates_parses_table_and_filters_tranche(tmp_path):
    lst = tmp_path / "liste.md"
    write_list(lst)
    assert rec.read_candidates(lst) == [(1, "AAA", "A"), (2, "BBB", "B")]
    assert rec.read_candidates(lst, "B") == [(2, "BBB", "B")]
    with pytest.raises(rec.ExportError):
        rec.read_candidates(lst, "C")


def test_the_real_candidate_list_has_40_rows_and_tranche_a_has_15():
    assert len(rec.read_candidates()) == 40
    assert len(rec.read_candidates(tranche="A")) == 15


def test_export_joins_all_four_runs_and_reports_gaps(tmp_path):
    db = tmp_path / "kopie.db"
    build(db)
    connection = sqlite3.connect(db)
    header, rows = rec.export_rows(connection, S2, [(1, "AAA", "A"), (2, "BBB", "B")])
    connection.close()
    assert header == {"stage2": S2, "stage1": S1, "prefilter": PRE, "trend": TR}
    a, b = rows
    assert a["close_price_usd"] == 25.5 and a["shares_outstanding"] == 40.0e6 and a["price_date"] == "2026-10-09"
    assert a["roic_3yr"] == 0.15 and a["stage2_rank"] == 3 and a["fcf_yield"] == 0.08 and a["trend_marks"] == "T1"
    assert a["luecken"] == ""
    assert "stage2_results fehlt" in b["luecken"] and "run_results fehlt" in b["luecken"] and b["close_price_usd"] is None


def test_empty_price_and_missing_trend_are_named_not_hidden(tmp_path):
    db = tmp_path / "kopie.db"
    build(db, with_price=False, with_trend=False)
    connection = sqlite3.connect(db)
    _, rows = rec.export_rows(connection, S2, [(1, "AAA", "A")])
    connection.close()
    gaps = rows[0]["luecken"]
    assert "close_price_usd leer" in gaps and "shares_outstanding leer" in gaps and "trend_runs fehlt" in gaps


def test_unknown_stage2_run_is_an_error(tmp_path):
    db = tmp_path / "kopie.db"
    build(db)
    connection = sqlite3.connect(db)
    with pytest.raises(rec.ExportError):
        rec.export_rows(connection, "2000-01-01T00:00:00", [(1, "AAA", "A")])
    connection.close()


def test_main_reads_only_and_writes_the_csv(tmp_path, capsys):
    db = tmp_path / "kopie.db"
    build(db)
    lst = tmp_path / "liste.md"
    write_list(lst)
    before = sha(db)
    out = tmp_path / "aus.csv"
    code = rec.main([str(db), S2, "--list", str(lst), "--out", str(out)])
    assert code == 0 and sha(db) == before
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("nr,tranche,ticker") and "AAA" in text
    assert not list(tmp_path.glob("*-wal")) and not list(tmp_path.glob("*-journal")) and not list(tmp_path.glob("*-shm"))
    assert "Zeilen mit Lücken: 1 von 2" in capsys.readouterr().out


def test_main_refuses_the_real_archive_and_an_existing_output(tmp_path, capsys):
    assert rec.main([str(archive_db.DEFAULT_DB_PATH), S2]) == 2
    assert "echte Run-Archiv" in capsys.readouterr().err
    db = tmp_path / "kopie.db"
    build(db)
    out = tmp_path / "aus.csv"
    out.write_text("x", encoding="utf-8")
    assert rec.main([str(db), S2, "--out", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == "x"


def test_main_refuses_a_copy_with_a_wal_file(tmp_path):
    db = tmp_path / "kopie.db"
    build(db)
    Path(str(db) + "-wal").write_bytes(b"")
    assert rec.main([str(db), S2]) == 2


def test_several_trend_runs_for_the_same_inputs_abort(tmp_path):
    db = tmp_path / "kopie.db"
    build(db)
    connection = sqlite3.connect(db)
    connection.execute("INSERT INTO trend_runs VALUES ('2026-10-11T00:00:00', ?, ?, '2026-10-11', 'h', 1, 1)", (S1, PRE))
    connection.commit()
    with pytest.raises(rec.ExportError, match="Mehrere Trend-Guard-Läufe"):
        rec.export_rows(connection, S2, [(1, "AAA", "A")])
    connection.close()


def test_duplicate_ticker_in_the_list_aborts(tmp_path):
    lst = tmp_path / "liste.md"
    lst.write_text("| 1 | 3 | AAA | A | x |\n| 2 | 4 | AAA | B | x |\n", encoding="utf-8")
    with pytest.raises(rec.ExportError, match="Doppelter Ticker"):
        rec.read_candidates(lst)


def test_non_positive_price_status_and_mismatches_are_named(tmp_path):
    db = tmp_path / "kopie.db"
    build(db)
    connection = sqlite3.connect(db)
    connection.execute("UPDATE universe_prefilter SET close_price_usd = 0, status = 'excluded', ticker = 'ZZZ'")
    connection.execute("UPDATE stage2_results SET ev_ebitda = NULL, invalid_reasons = 'ebitda_negative'")
    connection.execute("UPDATE trend_results SET simfin_id = 99, roic_last_year = NULL, roic_invalid_reason = 'roic_missing'")
    connection.commit()
    _, rows = rec.export_rows(connection, S2, [(1, "AAA", "A")])
    connection.close()
    gaps = rows[0]["luecken"]
    for text in ("close_price_usd nicht positiv", "Vorfilter-Status excluded", "Ticker im Vorfilter weicht ab: ZZZ",
                 "stage2 ev_ebitda leer (ebitda_negative)", "simfin_id in Trend-Guard weicht ab", "trend roic_last_year leer"):
        assert text in gaps, text
    assert rows[0]["stage2_invalid_reasons"] == "ebitda_negative"
    assert rows[0]["trend_invalid_reasons"] == "roic_invalid_reason: roic_missing"
    assert rows[0]["stage2_run"] == S2 and rows[0]["trend_run"] == TR


def test_old_archive_without_tables_gives_a_clear_abort(tmp_path, capsys):
    db = tmp_path / "alt.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE runs (run_timestamp TEXT)")
    connection.commit()
    connection.close()
    assert rec.main([str(db), S2]) == 2
    assert "veraltet oder unvollständig" in capsys.readouterr().err


def test_not_a_database_and_missing_output_folder_abort(tmp_path, capsys):
    junk = tmp_path / "x.db"
    junk.write_text("kein sqlite", encoding="utf-8")
    assert rec.main([str(junk), S2]) == 2
    db = tmp_path / "kopie.db"
    build(db)
    lst = tmp_path / "liste.md"
    write_list(lst)
    assert rec.main([str(db), S2, "--list", str(lst), "--out", str(tmp_path / "fehlt" / "aus.csv")]) == 2
    assert "nicht schreibbar" in capsys.readouterr().err


def test_a_link_to_the_real_archive_is_refused(tmp_path):
    real = archive_db.DEFAULT_DB_PATH
    link = tmp_path / "link.db"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("Symlinks nicht möglich")
    assert rec.main([str(link), S2]) == 2


def add_peers(db):
    c = sqlite3.connect(db)
    c.execute("UPDATE stage2_results SET peer_group = 100001, peer_level = 'fine' WHERE ticker = 'AAA'")
    for ticker, ev in (("PEER1", 5.0), ("PEER2", 9.0)):
        c.execute(
            "INSERT INTO stage2_results (run_timestamp, ticker, simfin_id, status, flags, invalid_reasons, info_flags, ev_ebitda, fcf_yield, peer_group, peer_level, peer_n) "
            "VALUES (?, ?, 99, 'passed', '', '', '', ?, 0.05, 100001, 'fine', 3)",
            (S2, ticker, ev),
        )
    c.execute(
        "INSERT INTO stage2_results (run_timestamp, ticker, simfin_id, status, flags, invalid_reasons, info_flags, ev_ebitda, peer_group, peer_level) "
        "VALUES (?, 'OTHER', 98, 'passed', '', '', '', 3.0, 100002, 'fine')",
        (S2,),
    )
    c.commit()
    c.close()


def test_peers_are_all_firms_of_the_same_group_and_level(tmp_path):
    db = tmp_path / "kopie.db"
    build(db)
    add_peers(db)
    connection = sqlite3.connect(db)
    _, rows = rec.export_rows(connection, S2, [(1, "AAA", "A")])
    peers = rec.export_peers(connection, S2, rows)
    connection.close()
    assert sorted(p["ticker"] for p in peers) == ["AAA", "PEER1", "PEER2"]
    assert all(p["candidate"] == "AAA" and p["peer_group"] == 100001 for p in peers)


def test_main_writes_the_peer_file_and_refuses_to_overwrite_it(tmp_path):
    db = tmp_path / "kopie.db"
    build(db)
    add_peers(db)
    lst = tmp_path / "liste.md"
    lst.write_text("| 1 | 3 | AAA | A | x |\n", encoding="utf-8")
    peers_out = tmp_path / "peers.csv"
    assert rec.main([str(db), S2, "--list", str(lst), "--peers-out", str(peers_out)]) == 0
    text = peers_out.read_text(encoding="utf-8")
    assert text.splitlines()[0].startswith("candidate,peer_group") and "PEER1" in text and "OTHER" not in text
    assert rec.main([str(db), S2, "--list", str(lst), "--peers-out", str(peers_out)]) == 2
