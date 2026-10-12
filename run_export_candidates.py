"""Exportiert die Eingangswerte der Kandidatenliste Runde 1 aus einer KOPIE des Run-Archivs (Roadmap D.4/D.5).

Nur lesen: Die Kopie wird schreibgeschuetzt und unveraenderlich ("immutable") geoeffnet, es entstehen keine Nebendateien.
Das echte data/run_archive.db wird verweigert, auch ueber einen Link (CLAUDE.md). Die Kopie muss eine einzelne Datei sein
(ohne -wal- oder -journal-Datei daneben).

Die Liste der Namen kommt aus docs/kandidaten/runde1.md (Spalten Nr., Rang, Ticker, Tranche). Die Laeufe werden aus dem
Stufe-2-Lauf abgeleitet (stage2_runs: Stufe-1-Lauf und Vorfilter-Lauf; trend_runs: der Trend-Guard-Lauf mit demselben
Stufe-1-Lauf). Fehlende Zeilen werden nicht verschwiegen, sondern in der Spalte "luecken" genannt.

Aufruf (auf einer Kopie, die Konstantin zuvor mit Pruefsumme gesichert hat), Beispiel fuer PowerShell:
    C:\\Users\\<Benutzer>\\anaconda3\\python.exe run_export_candidates.py data\\kopie_nach_A3.db 2026-10-10T12:27:44 --tranche A --out data\\export_A.csv

Mit --peers-out DATEI.csv entsteht eine zweite Datei mit allen Firmen derselben Peer-Gruppen (fuer den Vergleich mit der
Konkurrenz im One-Pager).

Der Zeitstempel ist der des Stufe-2-Laufs (Spalte run_timestamp in der Tabelle stage2_runs). Die CSV-Datei hat Komma als
Trenner und Punkt als Dezimalzeichen (nicht fuer das deutsche Excel gedacht, sondern zum Einfuegen in den Chat).
Ohne --out kommt die CSV auf den Bildschirm; in PowerShell 5 ist Umleiten mit ">" unbrauchbar, bitte --out nehmen.

Exit-Code: 0 = ausgegeben; 2 = Abbruch (echtes Archiv, Datei fehlt, Lauf nicht gefunden, Zieldatei existiert schon,
mehrere Trend-Guard-Laeufe, doppelter Ticker in der Liste). Alle ABBRUCH-Meldungen gehen nach stderr.
"""

import argparse
import csv
import io
import re
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from check_archive_schema import _is_real_archive, _open_read_only  # noqa: E402

CANDIDATE_LIST = _REPO_ROOT / "docs" / "kandidaten" / "runde1.md"

FIELDNAMES = [
    "nr", "tranche", "ticker", "simfin_id",
    "price_date", "close_price_usd", "shares_outstanding", "market_cap_usd", "sector_code",
    "stage1_status", "stage1_flags", "profile", "borderline_sector",
    "roic_3yr", "net_debt_ebitda", "cash_conversion_3yr", "stage1_data_quality_flags",
    "stage2_rank", "stage2_status", "stage2_flags", "stage2_info_flags",
    "ev_ebitda", "ev_ebitda_percentile", "peer_level", "peer_group", "peer_rank", "peer_n",
    "fcf_yield", "ev_ebitda_normalized", "share_count_ratio", "fcf_yield_incl_other_investing",
    "trend_status", "trend_marks", "revenue_ratio", "ebitda_margin_ratio", "roic_last_year",
    "stage2_invalid_reasons", "trend_invalid_reasons",
    "stage2_run", "stage1_run", "prefilter_run", "trend_run",
    "luecken",
]

_ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([A-Z0-9.\-]+)\s*\|\s*([ABC])\s*\|")


class ExportError(Exception):
    pass


def read_candidates(path=CANDIDATE_LIST, tranche=None):
    """(nr, ticker, tranche) je Zeile der Tabelle in runde1.md, in Listenreihenfolge."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            rows.append((int(match.group(1)), match.group(3), match.group(4)))
    if not rows:
        raise ExportError(f"Keine Kandidatenzeilen in {path} gefunden.")
    tickers = [r[1] for r in rows]
    if len(set(tickers)) != len(tickers):
        raise ExportError(f"Doppelter Ticker in {path}.")
    if tranche:
        rows = [r for r in rows if r[2] == tranche]
        if not rows:
            raise ExportError(f"Tranche {tranche!r} hat keine Zeilen.")
    return rows


def _one(connection, sql, params):
    connection.row_factory = sqlite3.Row
    return connection.execute(sql, params).fetchone()


def export_rows(connection, stage2_timestamp, candidates):
    connection.row_factory = sqlite3.Row
    stage2_run = _one(connection, "SELECT * FROM stage2_runs WHERE run_timestamp = ?", (stage2_timestamp,))
    if stage2_run is None:
        raise ExportError(f"Stufe-2-Lauf {stage2_timestamp!r} nicht in stage2_runs.")
    stage1_ts = stage2_run["stage1_run_timestamp"]
    prefilter_ts = stage2_run["prefilter_run_timestamp"]
    trend_runs = connection.execute(
        "SELECT run_timestamp FROM trend_runs WHERE stage1_run_timestamp = ? AND prefilter_run_timestamp = ? ORDER BY run_timestamp",
        (stage1_ts, prefilter_ts),
    ).fetchall()
    if len(trend_runs) > 1:
        raise ExportError(
            "Mehrere Trend-Guard-Läufe gehören zu diesem Stufe-1- und Vorfilter-Lauf: "
            + ", ".join(r["run_timestamp"] for r in trend_runs)
            + ". Welcher gilt, muss Konstantin sagen."
        )
    trend_ts = trend_runs[0]["run_timestamp"] if trend_runs else None

    rows = []
    for nr, ticker, tranche in candidates:
        gaps = []
        s2 = _one(connection, "SELECT * FROM stage2_results WHERE run_timestamp = ? AND ticker = ?", (stage2_timestamp, ticker))
        s1 = _one(connection, "SELECT * FROM run_results WHERE run_timestamp = ? AND ticker = ?", (stage1_ts, ticker))
        simfin_id = s2["simfin_id"] if s2 else None
        pre = None
        if simfin_id is not None:
            pre = _one(
                connection,
                "SELECT * FROM universe_prefilter WHERE run_timestamp = ? AND simfin_id = ?",
                (prefilter_ts, simfin_id),
            )
        tr = None
        if trend_ts:
            tr = _one(connection, "SELECT * FROM trend_results WHERE run_timestamp = ? AND ticker = ?", (trend_ts, ticker))
        if s2 is None:
            gaps.append("stage2_results fehlt")
        if s1 is None:
            gaps.append("run_results fehlt")
        if s2 is not None and pre is None:
            gaps.append("universe_prefilter fehlt")
        if pre is not None and pre["ticker"] != ticker:
            gaps.append(f"Ticker im Vorfilter weicht ab: {pre['ticker']}")
        if pre is not None and pre["status"] != "passed":
            gaps.append(f"Vorfilter-Status {pre['status']}")
        if pre is not None:
            for column in ("close_price_usd", "shares_outstanding", "market_cap_usd", "price_date"):
                value = pre[column]
                if value is None or value == "":
                    gaps.append(f"{column} leer")
                elif column != "price_date" and value <= 0:
                    gaps.append(f"{column} nicht positiv")
        if s2 is not None:
            for column in ("rank_position", "ev_ebitda", "fcf_yield"):
                if s2[column] is None:
                    gaps.append(f"stage2 {column} leer" + (f" ({s2['invalid_reasons']})" if s2["invalid_reasons"] else ""))
        if tr is not None and s2 is not None and tr["simfin_id"] != s2["simfin_id"]:
            gaps.append("simfin_id in Trend-Guard weicht ab")
        if tr is not None:
            for column in ("revenue_ratio", "ebitda_margin_ratio", "roic_last_year"):
                if tr[column] is None:
                    gaps.append(f"trend {column} leer")
        if trend_ts is None:
            gaps.append("trend_runs fehlt")
        elif tr is None:
            gaps.append("trend_results fehlt")

        def g(row, key):
            return row[key] if row is not None else None

        rows.append({
            "nr": nr, "tranche": tranche, "ticker": ticker, "simfin_id": simfin_id,
            "price_date": g(pre, "price_date"), "close_price_usd": g(pre, "close_price_usd"),
            "shares_outstanding": g(pre, "shares_outstanding"), "market_cap_usd": g(pre, "market_cap_usd"),
            "sector_code": g(pre, "sector_code"),
            "stage1_status": g(s1, "status"), "stage1_flags": g(s1, "flags"), "profile": g(s1, "profile"),
            "borderline_sector": g(s1, "borderline_sector"),
            "roic_3yr": g(s1, "roic_3yr"), "net_debt_ebitda": g(s1, "net_debt_ebitda"),
            "cash_conversion_3yr": g(s1, "cash_conversion_3yr"),
            "stage1_data_quality_flags": g(s1, "data_quality_flags"),
            "stage2_rank": g(s2, "rank_position"), "stage2_status": g(s2, "status"), "stage2_flags": g(s2, "flags"),
            "stage2_info_flags": g(s2, "info_flags"),
            "ev_ebitda": g(s2, "ev_ebitda"), "ev_ebitda_percentile": g(s2, "ev_ebitda_percentile"),
            "peer_level": g(s2, "peer_level"), "peer_group": g(s2, "peer_group"), "peer_rank": g(s2, "peer_rank"),
            "peer_n": g(s2, "peer_n"), "fcf_yield": g(s2, "fcf_yield"),
            "ev_ebitda_normalized": g(s2, "ev_ebitda_normalized"), "share_count_ratio": g(s2, "share_count_ratio"),
            "fcf_yield_incl_other_investing": g(s2, "fcf_yield_incl_other_investing"),
            "trend_status": g(tr, "status"), "trend_marks": g(tr, "marks"), "revenue_ratio": g(tr, "revenue_ratio"),
            "ebitda_margin_ratio": g(tr, "ebitda_margin_ratio"), "roic_last_year": g(tr, "roic_last_year"),
            "stage2_invalid_reasons": g(s2, "invalid_reasons"),
            "trend_invalid_reasons": "; ".join(
                f"{k}: {tr[k]}" for k in ("revenue_invalid_reason", "margin_invalid_reason", "roic_invalid_reason")
                if tr is not None and tr[k]
            ),
            "stage2_run": stage2_timestamp, "stage1_run": stage1_ts, "prefilter_run": prefilter_ts, "trend_run": trend_ts,
            "luecken": "; ".join(gaps),
        })
    header = {
        "stage2": stage2_timestamp, "stage1": stage1_ts, "prefilter": prefilter_ts, "trend": trend_ts,
    }
    return header, rows


PEER_FIELDNAMES = [
    "candidate", "peer_group", "peer_level", "ticker", "status", "ev_ebitda", "ev_ebitda_percentile", "fcf_yield",
    "peer_rank", "peer_n", "invalid_reasons",
]


def export_peers(connection, stage2_timestamp, rows):
    """Alle Stufe-2-Zeilen derselben Peer-Gruppe (gleiche Gruppe und Ebene) wie je ein Kandidat, fuer den Vergleich
    mit der Konkurrenz. Kandidaten ohne Stufe-2-Zeile oder ohne Peer-Gruppe erscheinen nicht (die Luecke steht schon in
    der Export-Tabelle)."""
    connection.row_factory = sqlite3.Row
    peers = []
    for row in rows:
        if row["peer_group"] in (None, "") or row["peer_level"] in (None, ""):
            continue
        found = connection.execute(
            "SELECT ticker, status, ev_ebitda, ev_ebitda_percentile, fcf_yield, peer_rank, peer_n, invalid_reasons, "
            "peer_group, peer_level FROM stage2_results WHERE run_timestamp = ? AND peer_group = ? AND peer_level = ? "
            "ORDER BY ticker",
            (stage2_timestamp, row["peer_group"], row["peer_level"]),
        ).fetchall()
        for f in found:
            peers.append({"candidate": row["ticker"], **{k: f[k] for k in f.keys()}})
    return peers


def render_csv(rows, fieldnames=None):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames or FIELDNAMES, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buffer.getvalue()


def _err(message):
    print(message, file=sys.stderr)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("copy_path")
    parser.add_argument("stage2_timestamp")
    parser.add_argument("--tranche", choices=["A", "B", "C"])
    parser.add_argument("--out")
    parser.add_argument("--peers-out", help="zweite CSV mit allen Firmen derselben Peer-Gruppen (Vergleich mit der Konkurrenz)")
    parser.add_argument("--list", dest="list_path", default=str(CANDIDATE_LIST))
    args = parser.parse_args(argv)

    path = Path(args.copy_path)
    if _is_real_archive(path):
        _err("ABBRUCH: Das ist das echte Run-Archiv; es wird nicht geöffnet. Bitte eine gesicherte Kopie angeben.")
        return 2
    if not path.is_file():
        _err(f"ABBRUCH: Datei nicht gefunden: {path}")
        return 2
    for suffix in ("-wal", "-journal"):
        if Path(str(path) + suffix).exists():
            _err(f"ABBRUCH: Neben der Kopie liegt eine Datei mit der Endung {suffix}; die Kopie ist unvollständig.")
            return 2
    for target in (args.out, args.peers_out):
        if target and Path(target).exists():
            _err(f"ABBRUCH: Zieldatei existiert schon (wird nicht überschrieben): {target}")
            return 2
    try:
        candidates = read_candidates(args.list_path, args.tranche)
        connection = _open_read_only(path)
        try:
            header, rows = export_rows(connection, args.stage2_timestamp, candidates)
            peers = export_peers(connection, args.stage2_timestamp, rows) if args.peers_out else None
        finally:
            connection.close()
    except sqlite3.OperationalError as exc:
        _err(f"ABBRUCH: Tabelle oder Spalte fehlt, die Kopie ist vermutlich veraltet oder unvollständig: {exc}")
        return 2
    except (ExportError, sqlite3.Error) as exc:
        _err(f"ABBRUCH: {exc}")
        return 2

    text = render_csv(rows)
    if args.out:
        try:
            with open(args.out, "x", encoding="utf-8", newline="") as handle:
                handle.write(text)
        except OSError as exc:
            _err(f"ABBRUCH: Zieldatei nicht schreibbar oder schon vorhanden: {exc}")
            return 2
        print(f"{len(rows)} Zeilen nach {args.out} geschrieben (Kopie nur gelesen).")
    else:
        print(text, end="")
    if args.peers_out:
        try:
            with open(args.peers_out, "x", encoding="utf-8", newline="") as handle:
                handle.write(render_csv(peers, PEER_FIELDNAMES))
        except OSError as exc:
            _err(f"ABBRUCH: Peer-Datei nicht schreibbar oder schon vorhanden: {exc}")
            return 2
        print(f"{len(peers)} Peer-Zeilen nach {args.peers_out} geschrieben.")
    gaps = [r for r in rows if r["luecken"]]
    print(
        f"Läufe: Stufe 2 {header['stage2']}, Stufe 1 {header['stage1']}, Vorfilter {header['prefilter']}, "
        f"Trend-Guard {header['trend']}. Zeilen mit Lücken: {len(gaps)} von {len(rows)}.",
        file=sys.stderr if not args.out else sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
