"""Vollstaendige Peer-Gruppen der Kandidaten fuer den Vergleich mit der Konkurrenz (Roadmap D.4, Wunsch von Konstantin).

Warum es das braucht: Das Archiv speichert Stufe-2-Zeilen nur fuer die Firmen, die Stufe 1 bestanden haben (126 von 1.821).
Die Peer-Gruppe eines Kandidaten (zum Beispiel 17 Firmen) besteht aber aus allen Firmen derselben Branche mit gueltigem
EV/EBITDA. Dieses Skript rechnet die Gruppe mit denselben Funktionen wie run_stage2.py aus dem Zwischenspeicher neu:
KEIN Abruf bei SimFin, nichts wird gespeichert (nur die eine CSV-Datei), die Archiv-KOPIE wird nur gelesen. Das echte
data/run_archive.db wird verweigert.

Aufruf (PowerShell, im Projektordner; python ist der Pfad zu deinem Anaconda-Python). Die beiden Ordner sind dieselben, die du bei
run_stage2.py mit --fundamentals-cache-dir und --universe-cache-dir angegeben hast, OHNE das Datum am Ende (das Skript haengt es
selbst an):
    python run_export_peers.py data\\kopie_nach_A3.db 2026-10-10T12:27:44 --fundamentals-cache-dir ORDNER1 --universe-cache-dir ORDNER2 --tranche A --out data\\peers_voll_A.csv
Mit --list DATEI kann eine andere Kandidatenliste verwendet werden (Standard: docs/kandidaten/runde1.md).

Sicherheitsnetz: Das Skript rechnet die Groesse der Peer-Basis nach und vergleicht sie mit der im Archiv gespeicherten Zahl
(stage2_runs.peer_base_size). Weicht sie ab, bricht es mit einer Meldung ab und schreibt nichts, weil die Peer-Gruppen dann
nicht dieselben wie in Stufe 2 waeren.

Die Ausgabe hat dieselben Spalten wie run_export_candidates.py --peers-out und passt direkt zu render_onepager_pdf.py. Neu:
status ist "nicht_in_stufe_2" bei Firmen, die Stufe 1 nicht bestanden haben (sie sind trotzdem Teil der Peer-Gruppe).

Ausgang: 0 = geschrieben; 2 = Abbruch (echtes Archiv, Datei fehlt, Lauf nicht gefunden, Zieldatei existiert schon).
"""

import argparse
import csv
import sqlite3
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

import run_stage2  # noqa: E402
from check_archive_schema import _is_real_archive, _open_read_only  # noqa: E402
from config_layer.sector_profiles import SectorProfilesConfigError, load_sector_profiles  # noqa: E402
from config_layer.stage2 import Stage2ConfigError, compute_stage2_config_hash, load_stage2_config  # noqa: E402
from config_layer.thresholds import ThresholdsConfigError, load_thresholds  # noqa: E402
from data_layer.simfin_price_cache import PriceCacheError, load_price_cache_index  # noqa: E402
from gates_layer.peer_groups import LEVEL_ABSOLUTE, LEVEL_COARSE, assign_peers, peer_position  # noqa: E402
from gates_layer.stage2_gates import _peer_base_multiples  # noqa: E402
from metrics_layer.valuation import fcf_yield_average  # noqa: E402
from run_export_candidates import PEER_FIELDNAMES, read_candidates, render_csv  # noqa: E402


class PeerExportError(Exception):
    pass


def peer_rows(inputs, config, as_of_year, candidates, statuses, problems=None):
    """(Zeilen, Hinweise): je Kandidat alle Firmen seiner Peer-Gruppe (gleiche Ebene und Gruppe wie bei Stufe 2).

    inputs: {ticker: ValuationInput} wie in run_stage2; statuses: {ticker: Stufe-2-Status} der gespeicherten Zeilen.
    Ein Kandidat ohne Eintrag in der Peer-Basis oder ohne Peer-Gruppe (absolute Ebene) liefert keine Zeilen, aber einen Hinweis
    mit der Ursache. problems: {ticker: (sector_code, Grund)} der Firmen ohne nutzbare Daten; solche in der Branche des
    Kandidaten werden im Hinweis genannt (sie fehlen in der Gruppe).
    """
    base, ev_results = _peer_base_multiples(inputs, config, as_of_year)
    problems = problems or {}
    divisor = config.peer_groups.coarse_level_divisor
    rows, notes = [], []
    for candidate in candidates:
        if candidate not in base:
            if candidate in problems:
                cause = f"keine nutzbaren Fundamentaldaten ({problems[candidate][1]})"
            elif candidate not in inputs:
                cause = "nicht im Vorfilter-Lauf"
            elif candidate in ev_results:
                cause = f"EV/EBITDA ungueltig ({ev_results[candidate].invalid_reason})"
            else:
                cause = "Profil nicht unterstuetzt oder ohne Branchenkennzahl"
            notes.append(f"{candidate}: nicht in der Peer-Basis, Ursache: {cause}")
            continue
        sector, multiple = base[candidate]
        assignment = assign_peers(candidate, sector, multiple, base, config.peer_groups)
        if assignment.level == LEVEL_ABSOLUTE:
            notes.append(f"{candidate}: keine Peer-Gruppe (absolute Ebene)")
            continue
        if assignment.level == LEVEL_COARSE:
            members = [t for t, (s, _) in base.items() if s // divisor == assignment.group_key]
        else:
            members = [t for t, (s, _) in base.items() if s == assignment.group_key]
        values = [base[t][1] for t in members]
        lost = sorted(
            f"{t} ({reason})" for t, (s, reason) in problems.items()
            if (s // divisor == assignment.group_key if assignment.level == LEVEL_COARSE else s == assignment.group_key)
        )
        if lost:
            notes.append(f"{candidate}: in der Gruppe fehlen Firmen ohne nutzbare Daten: " + ", ".join(lost))
        for ticker in sorted(members):
            vi = inputs[ticker]
            fcf = fcf_yield_average(vi.periods, vi.capex_by_year, vi.market_cap_usd, as_of_year=as_of_year, config=config)
            position = peer_position(base[ticker][1], values)
            rows.append({
                "candidate": candidate, "peer_group": assignment.group_key, "peer_level": assignment.level, "ticker": ticker,
                "status": statuses.get(ticker, "nicht_in_stufe_2"), "ev_ebitda": base[ticker][1],
                "ev_ebitda_percentile": float(position.percentile), "fcf_yield": fcf.value,
                "peer_rank": position.rank, "peer_n": position.n, "invalid_reasons": fcf.invalid_reason or "",
            })
    return rows, notes


def _read_run(connection, stage2_timestamp):
    connection.row_factory = sqlite3.Row
    run = connection.execute("SELECT * FROM stage2_runs WHERE run_timestamp = ?", (stage2_timestamp,)).fetchone()
    if run is None:
        raise PeerExportError(f"Stufe-2-Lauf {stage2_timestamp!r} nicht in stage2_runs.")
    statuses = {r["ticker"]: r["status"] for r in connection.execute(
        "SELECT ticker, status FROM stage2_results WHERE run_timestamp = ?", (stage2_timestamp,))}
    return run, statuses


def build_inputs(db_path, prefilter_ts, as_of, fundamentals_cache_dir, universe_cache_dir):
    """Dieselben Eingaben wie run_stage2.run (nur lesen): ({ticker: ValuationInput}, {ticker: (sector_code, Grund)})."""
    thresholds = load_thresholds(run_stage2.DEFAULT_THRESHOLDS_PATH)
    profiles = load_sector_profiles(run_stage2.DEFAULT_SECTOR_PROFILES_PATH)
    _, rows = run_stage2.load_prefilter_rows(db_path, prefilter_ts)
    by_ticker = {}
    for simfin_id, ticker, _, _ in rows:
        if ticker is not None:
            by_ticker.setdefault(ticker, []).append(simfin_id)
    duplicates = {t: ids for t, ids in by_ticker.items() if len(ids) > 1}
    if duplicates:
        raise PeerExportError(f"Ticker mit mehreren simfin_id im Vorfilter-Lauf (wie bei run_stage2 ein Abbruch): {duplicates}")
    unknown, null_count = profiles.find_uncovered(sector_code for _, _, sector_code, _ in rows)
    if unknown or null_count:
        raise PeerExportError(f"Branchenprofile unvollstaendig: nicht gelistete Codes {unknown}, Zeilen ohne Code: {null_count}")
    cache_dir = Path(fundamentals_cache_dir) / as_of.isoformat()
    price_index = load_price_cache_index(Path(universe_cache_dir) / as_of.isoformat() / "prices")
    inputs, problems = {}, {}
    for simfin_id, ticker, sector_code, market_cap in rows:
        if ticker is None:
            continue
        assignment = profiles.assignment(sector_code)
        vi, problem, _ = run_stage2.build_input(
            ticker, simfin_id, sector_code, market_cap, assignment, cache_dir=cache_dir,
            reconciliation=thresholds.data_quality_reconciliation,
            price_entry=price_index.entries.get(simfin_id),
        )
        if problem is not None:
            problems[ticker] = (sector_code, problem)
        else:
            inputs[ticker] = vi
    return inputs, problems


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("copy_path")
    parser.add_argument("stage2_timestamp")
    parser.add_argument("--fundamentals-cache-dir", type=Path, required=True)
    parser.add_argument("--universe-cache-dir", type=Path, required=True,
                        help="ohne diesen Ordner waere die Aktienzahl-Pruefung ungeprueft und die Peer-Gruppen wuerden von Stufe 2 abweichen")
    parser.add_argument("--tranche", choices=["A", "B", "C"])
    parser.add_argument("--out", required=True)
    parser.add_argument("--list", dest="list_path", default=None)
    args = parser.parse_args(argv)

    path = Path(args.copy_path)
    if _is_real_archive(path):
        print("ABBRUCH: Das ist das echte Run-Archiv; es wird nicht geöffnet. Bitte eine gesicherte Kopie angeben.", file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"ABBRUCH: Datei nicht gefunden: {path}", file=sys.stderr)
        return 2
    for suffix in ("-wal", "-journal"):
        if Path(str(path) + suffix).exists():
            print(f"ABBRUCH: Neben der Kopie liegt eine Datei mit der Endung {suffix}; die Kopie ist unvollständig.", file=sys.stderr)
            return 2
    if Path(args.out).exists():
        print(f"ABBRUCH: Zieldatei existiert schon (wird nicht überschrieben): {args.out}", file=sys.stderr)
        return 2
    try:
        candidates = [t for _, t, _ in (read_candidates(args.list_path, args.tranche) if args.list_path else read_candidates(tranche=args.tranche))]
        connection = _open_read_only(path)
        try:
            run, statuses = _read_run(connection, args.stage2_timestamp)
        finally:
            connection.close()
        config = load_stage2_config(run_stage2.DEFAULT_STAGE2_PATH)
        as_of = date.fromisoformat(run["as_of"])
        inputs, problems = build_inputs(path, run["prefilter_run_timestamp"], as_of, args.fundamentals_cache_dir, args.universe_cache_dir)
        base_size = len(_peer_base_multiples(inputs, config, as_of.year)[0])
        if base_size != run["peer_base_size"]:
            raise PeerExportError(
                f"Die nachgerechnete Peer-Basis hat {base_size} Firmen, im Archiv stehen {run['peer_base_size']}. "
                "Die Gruppen waeren nicht dieselben wie in Stufe 2 (andere Ordner, Regeln oder Daten?). Es wird nichts geschrieben."
            )
        if compute_stage2_config_hash(run_stage2.DEFAULT_STAGE2_PATH) != run["config_hash"]:
            print("Warnung: config/stage2.yaml hat sich seit dem Stufe-2-Lauf geaendert (anderer config_hash).", file=sys.stderr)
        rows, notes = peer_rows(inputs, config, as_of.year, candidates, statuses, problems)
        with open(args.out, "x", encoding="utf-8", newline="") as handle:
            handle.write(render_csv(rows, PEER_FIELDNAMES))
    except (PeerExportError, run_stage2.Stage2RunError, Stage2ConfigError, ThresholdsConfigError, SectorProfilesConfigError,
            PriceCacheError, sqlite3.Error, OSError, ValueError, AttributeError) as exc:
        print(f"ABBRUCH: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"{len(rows)} Zeilen für {len(candidates)} Kandidaten nach {args.out} geschrieben (nur gelesen, nichts gespeichert, 0 SimFin-Abrufe).")
    print(f"Lauf: Stufe 2 {args.stage2_timestamp}, Daten vom {as_of.isoformat()}, Peer-Basis {base_size} Firmen (wie im Archiv). "
          f"Firmen ohne nutzbare Fundamentaldaten: {len(problems)}.")
    for note in notes:
        print("Hinweis: " + note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
